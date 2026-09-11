"""Spider for GIZ country-office tender pages.

Where the notices come from
---------------------------
GIZ publishes its in-country procurement on per-country pages under
``giz.de/en/regions/``, rendered server-side by a Drupal view — the rows are in
the initial HTML, unauthenticated and unchallenged. Each row is a title, an
optional deadline and a ZIP of the tender documents; ``GIZ_SOURCE.md`` documents
the feed, GIZ's other channels, and why this one is the one crawled.

Incremental strategy
--------------------
There is none, because there is nothing to be incremental over: the page lists
every currently-open tender (a handful per country), shows no publication date
to filter on, and removes rows once they close. Every crawl is a full read of a
very small list, so ``incremental_days`` is accepted for signature parity with
the other spiders and deliberately ignored. New/updated/unchanged is decided by
the pipeline's content hash, as everywhere else.

Pagination
----------
The view renders the whole list in one response today. A Drupal pager is still
followed if one ever appears, so growth degrades into extra requests rather
than dropped notices.

Adding a country
----------------
Set ``GIZ_TENDER_PAGES`` to ``Country|URL`` pairs separated by commas, e.g.
``Indonesia|https://www.giz.de/en/regions/asia/indonesia/tenders,Viet Nam|...``.
The default is Indonesia, the office AMANA bids with.
"""
import os
from urllib.parse import urljoin

import scrapy
from scrapy.exceptions import CloseSpider

from crawler.giz_parsers import normalize_giz_row, parse_giz_listing
from crawler.items import ProcurementNoticeItem
from crawler.parsers import hash_response

#: Country name -> tenders page, as ``GIZ_TENDER_PAGES`` overrides it.
DEFAULT_PAGES = {
    "Indonesia": "https://www.giz.de/en/regions/asia/indonesia/tenders",
}

#: The Drupal view behind the list. Its presence is what separates "no tenders
#: at the moment" from "the page no longer carries the view at all".
VIEW_MARKER = "dd_country_tenders"


def _pages_from_env(raw: str) -> dict:
    """Parse ``Country|URL,Country|URL`` into the pages mapping."""
    pages = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        country, _, url = entry.partition("|")
        if country.strip() and url.strip().startswith("http"):
            pages[country.strip()] = url.strip()
    return pages


class GizSpider(scrapy.Spider):
    name = "giz"
    allowed_domains = ["giz.de"]

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
        # The site serves the same HTML to any client; the honest identity the
        # other spiders send works here too.
        "USER_AGENT": "TenderIntelligence/1.0 (+https://amana.id)",
        "LOG_LEVEL": "INFO",
    }

    def __init__(self, incremental_days: int = 7, pages: str = "", **kwargs):
        super().__init__(**kwargs)
        # Accepted so every spider can be launched with the same arguments;
        # unused because the feed offers nothing to filter by date (see the
        # module docstring).
        self.incremental_days = int(incremental_days)

        self.pages = (
            _pages_from_env(pages)
            or _pages_from_env(os.getenv("GIZ_TENDER_PAGES", ""))
            or dict(DEFAULT_PAGES)
        )

        self.pages_fetched = 0
        self.notices_found = 0
        self.countries_failed = 0

    def _initial_requests(self):
        self.logger.info(
            "Crawling GIZ country tender pages: %s", ", ".join(self.pages)
        )
        for country, url in self.pages.items():
            yield scrapy.Request(
                url,
                callback=self.parse,
                errback=self.page_failed,
                # The listing has no notice-level URL of its own, so the page
                # url doubles as the notice_url and must survive a pager hop.
                meta={"country": country, "page_url": url},
                dont_filter=True,
            )

    async def start(self):
        """Entry point for Scrapy >= 2.13."""
        for request in self._initial_requests():
            yield request

    def start_requests(self):
        """Entry point for Scrapy < 2.13. See the World Bank spider for why
        both exist."""
        yield from self._initial_requests()

    def parse(self, response):
        country = response.meta["country"]
        page_url = response.meta["page_url"]
        body = response.text

        rows = parse_giz_listing(body)
        self.pages_fetched += 1

        if not rows and VIEW_MARKER not in body:
            # The page answered but the tenders view is gone — a redesign or a
            # moved URL. Close rather than return, so the run records a failure
            # instead of a quiet "nothing to crawl".
            raise CloseSpider(
                f"GIZ tenders page for {country} ({response.url}) no longer "
                "carries the dd_country_tenders view; the page may have moved "
                "or been rebuilt. Update GIZ_TENDER_PAGES."
            )

        self.logger.info("%s: %d tender(s) listed", country, len(rows))

        for raw in rows:
            normalized = normalize_giz_row(raw, country, page_url)
            item = ProcurementNoticeItem()
            for key, value in normalized.items():
                item[key] = value
            item["content_hash"] = hash_response(raw)
            self.notices_found += 1
            yield item

        # No pager exists today; followed defensively if Drupal ever adds one.
        next_href = response.xpath(
            '//li[contains(@class, "pager__item--next")]//a/@href'
        ).get()
        if next_href:
            yield scrapy.Request(
                urljoin(response.url, next_href),
                callback=self.parse,
                errback=self.page_failed,
                meta={"country": country, "page_url": page_url},
                dont_filter=True,
            )

    def page_failed(self, failure):
        """Count the loss so the crawl is recorded as failed, and say which
        country's page it was — one office's outage should be visible without
        taking the other offices' notices down with it."""
        country = failure.request.meta.get("country", "?")
        self.countries_failed += 1
        # Also counted as a stored error so the CrawlRun's `errors` column
        # carries it; the pipeline initializes this counter on open_spider.
        self.notices_errored = getattr(self, "notices_errored", 0) + 1
        self.logger.error("GIZ %s tenders page failed: %s", country, failure.value)

    def closed(self, reason):
        self.logger.info(
            "GIZ crawl finished (%s): %d notices over %d page(s), %d page(s) failed.",
            reason,
            self.notices_found,
            self.pages_fetched,
            self.countries_failed,
        )
