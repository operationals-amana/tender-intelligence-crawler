"""Spider for the World Bank procurement notices API.

Incremental strategy
--------------------
The API rejects every date-range parameter we tried (``submission_date``,
``noticedate``, ``strdate``/``enddate`` all 400 or are silently ignored), so
there is no server-side "changed since" filter available.

What the API *does* guarantee is ordering: results come back strictly
newest-first by ``noticedate``. So an incremental crawl simply walks pages from
offset 0 and stops as soon as it reaches notices older than the cutoff. A full
crawl is the same walk with no cutoff.

Deep pagination fails somewhere past offset 100,000, which is well beyond any
sane incremental window but caps how far a "full" backfill can reach.
"""
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import scrapy

from crawler.items import ProcurementNoticeItem
from crawler.parsers import hash_response, normalize_notice_item, parse_noticedate

# Hard ceiling imposed by the API; requests past this return HTTP 400.
MAX_OFFSET = 100_000


class ProcurementSpider(scrapy.Spider):
    name = "procurement"
    base_url = "https://search.worldbank.org/api/v2/procnotices"
    allowed_domains = ["search.worldbank.org"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "crawler.pipelines.ValidationPipeline": 100,
            "crawler.pipelines.PostgresPipeline": 300,
        },
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 2.0,
        "DOWNLOAD_TIMEOUT": 120,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "USER_AGENT": "TenderIntelligence/1.0 (+https://amana.id)",
        "LOG_LEVEL": "INFO",
    }

    def __init__(
        self,
        incremental_days: int = 7,
        rows: int = 500,
        notice_status: str = "Published",
        notice_types: str = "",
        country: str = "",
        max_pages: int = 0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Scrapy passes CLI/`process.crawl` arguments through as strings.
        self.incremental_days = int(incremental_days)
        self.rows = max(1, min(int(rows), 1000))
        self.notice_status = notice_status or ""
        self.notice_types = [t.strip() for t in str(notice_types).split(",") if t.strip()]
        self.country = country or ""
        self.max_pages = int(max_pages)

        self.cutoff_date = None
        if self.incremental_days > 0:
            self.cutoff_date = (datetime.utcnow() - timedelta(days=self.incremental_days)).date()

        self.pages_fetched = 0
        self.notices_found = 0
        self.stopped_early = False

    def _build_url(self, offset: int) -> str:
        params = {"format": "json", "rows": str(self.rows), "os": str(offset)}
        if self.notice_status:
            params["notice_status"] = self.notice_status
        if self.country:
            params["project_ctry_name"] = self.country
        # notice_type_exact accepts a comma-separated list.
        if self.notice_types:
            params["notice_type_exact"] = ",".join(self.notice_types)
        return f"{self.base_url}?{urlencode(params)}"

    def _initial_request(self):
        if self.cutoff_date:
            self.logger.info(
                "Incremental crawl: notices published on/after %s (%d days back)",
                self.cutoff_date,
                self.incremental_days,
            )
        else:
            self.logger.info("Full crawl: walking all published notices")

        return scrapy.Request(
            self._build_url(0), callback=self.parse, meta={"offset": 0}, dont_filter=True
        )

    async def start(self):
        """Entry point for Scrapy >= 2.13."""
        yield self._initial_request()

    def start_requests(self):
        """Entry point for Scrapy < 2.13.

        Scrapy 2.13 replaced this with the async ``start()`` above and 2.17
        dropped it from the base class entirely. Defining both keeps the spider
        runnable across versions -- and note that the removal is silent: a
        spider that only defines ``start_requests`` on 2.17 simply crawls
        nothing instead of raising.
        """
        yield self._initial_request()

    def parse(self, response):
        data = response.json()
        notices = data.get("procnotices") or []
        total = int(data.get("total") or 0)
        offset = response.meta.get("offset", 0)

        self.pages_fetched += 1
        self.logger.info(
            "Page %d: %d notices (offset=%d, total=%d)",
            self.pages_fetched,
            len(notices),
            offset,
            total,
        )

        reached_cutoff = False

        for raw in notices:
            if not isinstance(raw, dict):
                continue

            # Results are newest-first, so the first notice older than the
            # cutoff means everything after it is older too.
            if self.cutoff_date:
                published = parse_noticedate(raw.get("noticedate"))
                if published and published < self.cutoff_date:
                    reached_cutoff = True
                    break

            normalized = normalize_notice_item(raw)
            if not normalized.get("notice_id"):
                continue

            item = ProcurementNoticeItem()
            for key, value in normalized.items():
                item[key] = value
            item["content_hash"] = hash_response(raw)
            self.notices_found += 1
            yield item

        if reached_cutoff:
            self.stopped_early = True
            self.logger.info(
                "Reached cutoff %s after %d notices; stopping pagination.",
                self.cutoff_date,
                self.notices_found,
            )
            return

        if not notices:
            return

        next_offset = offset + self.rows
        if next_offset >= total or next_offset >= MAX_OFFSET:
            self.logger.info("Pagination complete at offset %d.", next_offset)
            return
        if self.max_pages and self.pages_fetched >= self.max_pages:
            self.logger.info("Reached max_pages=%d; stopping.", self.max_pages)
            return

        yield scrapy.Request(
            self._build_url(next_offset),
            callback=self.parse,
            meta={"offset": next_offset},
            dont_filter=True,
        )

    def closed(self, reason):
        self.logger.info(
            "Crawl finished (%s): %d notices over %d pages.",
            reason,
            self.notices_found,
            self.pages_fetched,
        )
