"""Spider for Asian Development Bank business opportunities.

Where the notices come from
---------------------------
ADB's business-opportunities page and the tender listing behind it are served
through a challenge that this service cannot pass without a real browser. The
listing itself, however, is drawn in the browser from a hosted Solr index on a
different host, and that host is open: it answers with clean JSON, supports
filter queries and sorting, and is not challenged. That index is what this
spider reads, so no browser and no challenge-solving is involved.

The index is read-only and its token is the one ADB ships in its own public
front-end. It is treated as configuration (``ADB_SEARCH_TOKEN``) rather than a
constant, because a token embedded in a web page is a token that can be rotated;
a 401 or 403 here means it has been, and the spider says so rather than crawling
zero notices quietly.

Incremental strategy
--------------------
Unlike the World Bank feed, this index accepts a real date filter, so an
incremental crawl is a filter query on ``ds_date_posted`` rather than a walk
that stops at a cutoff. Pagination is a plain ``start``/``rows`` offset over a
result set sorted oldest-first — sorting *ascending* on purpose, so that a
notice published mid-crawl is appended past the end of the walk instead of
shifting every remaining page by one and skipping a row.

Two requests per consulting notice
----------------------------------
The index carries no notice body. Consulting notices link to their full text in
ADB's Consultant Management System, which is also unchallenged, so the spider
follows that link; goods and works notices publish theirs as a PDF behind the
challenge, and are stored with metadata only. That makes a consulting notice
cost two requests and everything else cost none beyond its page, which at
roughly 90 new notices a week is not a volume worth optimizing.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import scrapy
from scrapy.exceptions import CloseSpider

from crawler.adb_parsers import normalize_adb_doc, parse_csrn_page
from crawler.items import ProcurementNoticeItem
from crawler.parsers import hash_response

DEFAULT_ENDPOINT = (
    "https://searchcloud-2-ap-southeast-1.searchstax.com/29847/tenders-11959/emselect"
)

# The read-only token ADB's own tender page sends. Overridable, and refreshable
# from the browser's network log if ADB ever rotates it.
DEFAULT_TOKEN = "2a076eb3a48fd68fc78506c1a16a5d5000da76e4"

# Solr rejects very large page sizes and the index is small enough that it never
# matters; 200 keeps each response comfortably under a megabyte.
MAX_ROWS = 200


class AdbSpider(scrapy.Spider):
    name = "adb"
    allowed_domains = ["searchstax.com", "adb.org"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "crawler.pipelines.ValidationPipeline": 100,
            "crawler.pipelines.PostgresPipeline": 300,
        },
        "CONCURRENT_REQUESTS": 2,
        "DOWNLOAD_DELAY": 1.0,
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
        rows: int = 200,
        statuses: str = "",
        notice_types: str = "",
        country: str = "",
        max_pages: int = 0,
        fetch_detail: str = "true",
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Scrapy passes CLI/`process.crawl` arguments through as strings.
        self.incremental_days = int(incremental_days)
        self.rows = max(1, min(int(rows), MAX_ROWS))
        self.statuses = [s.strip() for s in str(statuses).split(",") if s.strip()]
        self.notice_types = [t.strip() for t in str(notice_types).split(",") if t.strip()]
        self.country = country or ""
        self.max_pages = int(max_pages)
        self.fetch_detail = str(fetch_detail).lower() not in ("false", "0", "no")

        self.endpoint = os.getenv("ADB_SEARCH_URL", DEFAULT_ENDPOINT)
        self.token = os.getenv("ADB_SEARCH_TOKEN", DEFAULT_TOKEN)

        # Pinned once, not written as Solr's `NOW-7DAYS`. Solr would re-evaluate
        # that on every page, and since the walk is sorted oldest-first, a lower
        # bound creeping forward mid-crawl drops rows off the *front* of the
        # result set and shifts every later offset down by one — skipping
        # notices the walk had not reached yet.
        self.posted_since = None
        if self.incremental_days > 0:
            since = datetime.now(timezone.utc) - timedelta(days=self.incremental_days)
            self.posted_since = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        self.pages_fetched = 0
        self.notices_found = 0
        self.details_fetched = 0
        self.details_failed = 0
        self.total_available = 0

    # -- request building --------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
            # The index is configured to answer for ADB's own site; the header
            # is sent for the same reason a browser would send it.
            "Origin": "https://www.adb.org",
        }

    def _filters(self) -> list:
        filters = []
        if self.posted_since:
            filters.append(f"ds_date_posted:[{self.posted_since} TO *]")
        if self.statuses:
            filters.append(" OR ".join(f'sm_fct_status:"{s}"' for s in self.statuses))
        if self.notice_types:
            filters.append(" OR ".join(f'sm_fct_type:"{t}"' for t in self.notice_types))
        if self.country:
            filters.append(f'sm_fct_country:"{self.country}"')
        return filters

    def _build_url(self, start: int) -> str:
        params = [
            ("q", "*"),
            ("wt", "json"),
            ("rows", str(self.rows)),
            ("start", str(start)),
            # Oldest first: see the module docstring. A notice published while
            # the walk is running lands after the last page rather than pushing
            # an unread row off the end of the current one.
            ("sort", "ds_date_posted asc, id asc"),
        ]
        params.extend(("fq", f) for f in self._filters())
        return f"{self.endpoint}?{urlencode(params)}"

    def _page_request(self, start: int):
        return scrapy.Request(
            self._build_url(start),
            callback=self.parse,
            headers=self._headers(),
            # A rejected token comes back as 401 or 403, either of which Scrapy
            # would otherwise drop before `parse` runs — leaving a crawl that
            # found nothing and never said why.
            meta={"start": start, "handle_httpstatus_list": [401, 403]},
            dont_filter=True,
        )

    def _initial_request(self):
        if self.posted_since:
            self.logger.info(
                "Incremental crawl: ADB notices posted since %s (%d days back)",
                self.posted_since,
                self.incremental_days,
            )
        else:
            self.logger.info("Full crawl: walking every ADB notice in the index")
        return self._page_request(0)

    async def start(self):
        """Entry point for Scrapy >= 2.13."""
        yield self._initial_request()

    def start_requests(self):
        """Entry point for Scrapy < 2.13. See the World Bank spider for why
        both exist."""
        yield self._initial_request()

    # -- parsing -----------------------------------------------------------

    def parse(self, response):
        if response.status in (401, 403):
            raise CloseSpider(
                "ADB search index rejected our token. It is the read-only token "
                "ADB embeds in its own tender page and it has probably been "
                "rotated: read the current one from the Authorization header of "
                "the request adb.org/projects/tenders makes to searchstax.com, "
                "and set it as ADB_SEARCH_TOKEN."
            )

        try:
            data = json.loads(response.text)
        except ValueError:
            # Closed rather than returned, so the run is recorded as failed. A
            # bare `return` here would end the walk mid-way and still report
            # "finished", which reads as "there was nothing more to crawl".
            raise CloseSpider(
                "ADB search index returned a non-JSON response "
                f"(HTTP {response.status}); the endpoint may have moved. "
                "Check ADB_SEARCH_URL."
            )

        block = data.get("response") or {}
        docs = block.get("docs") or []
        total = int(block.get("numFound") or 0)
        start = response.meta.get("start", 0)

        self.pages_fetched += 1
        self.total_available = total
        self.logger.info(
            "Page %d: %d notices (start=%d, total=%d)",
            self.pages_fetched,
            len(docs),
            start,
            total,
        )

        for doc in docs:
            if isinstance(doc, dict):
                yield from self._handle_doc(doc)

        if not docs:
            return

        next_start = start + self.rows
        if next_start >= total:
            self.logger.info("Pagination complete at start=%d.", next_start)
            return
        if self.max_pages and self.pages_fetched >= self.max_pages:
            self.logger.info("Reached max_pages=%d; stopping.", self.max_pages)
            return

        yield self._page_request(next_start)

    def _handle_doc(self, doc: dict):
        """Emit a notice, fetching its consulting detail page first if it has one."""
        csrn_url = doc.get("ss_csrn_url")

        if self.fetch_detail and csrn_url:
            yield scrapy.Request(
                csrn_url,
                callback=self.parse_detail,
                errback=self.detail_failed,
                meta={"doc": doc},
                dont_filter=True,
            )
            return

        # Not partial: a notice with no CSRN link has no body to miss.
        item = self._build_item(doc, None)
        if item is not None:
            yield item

    def parse_detail(self, response):
        doc = response.meta["doc"]
        detail = parse_csrn_page(response.body)
        got_detail = bool(detail.get("fields") or detail.get("text"))
        if got_detail:
            self.details_fetched += 1
        else:
            # The page answered but yielded nothing we could read — a login
            # wall or a redesign. Same situation as a failed fetch.
            self.details_failed += 1
            self.logger.warning(
                "CSRN detail page for %s parsed to nothing; storing metadata only.",
                doc.get("ss_url"),
            )
        item = self._build_item(doc, detail, partial=not got_detail)
        if item is not None:
            yield item

    def detail_failed(self, failure):
        """Store the notice without its body rather than losing it.

        The metadata alone still names the opportunity, its country, its sector
        and its deadline, which is enough to route and to open by hand. Dropping
        the notice because its detail page timed out would be a worse trade.
        """
        request = failure.request
        doc = request.meta.get("doc")
        self.details_failed += 1
        self.logger.warning(
            "CSRN detail fetch failed (%s); storing metadata only.", failure.value
        )
        if doc:
            item = self._build_item(doc, None, partial=True)
            if item is not None:
                yield item

    def _build_item(self, doc: dict, detail, partial: bool = False):
        normalized = normalize_adb_doc(doc, detail)
        if not normalized.get("notice_id"):
            self.logger.warning("ADB notice without a resolvable node id, dropping")
            return None

        item = ProcurementNoticeItem()
        for key, value in normalized.items():
            item[key] = value
        # Tells the pipeline the blanks in this item are missing, not empty, so
        # an update keeps the body an earlier crawl already stored.
        item["partial"] = partial
        # Hash the index document together with the detail page's fields, so a
        # notice whose body changes but whose listing row does not still counts
        # as updated.
        item["content_hash"] = hash_response(
            {"doc": doc, "detail": (detail or {}).get("fields") or {}}
        )
        self.notices_found += 1
        return item

    def closed(self, reason):
        self.logger.info(
            "ADB crawl finished (%s): %d notices over %d pages "
            "(%d detail pages read, %d failed).",
            reason,
            self.notices_found,
            self.pages_fetched,
            self.details_fetched,
            self.details_failed,
        )
