"""Run the World Bank procurement crawler.

Usage:
    python run_crawler.py                     # incremental, last 7 days
    python run_crawler.py --days 0            # full backfill
    python run_crawler.py --days 30 --rows 500
    python run_crawler.py --schedule          # run forever on an interval

Scheduling note: Scrapy runs on Twisted, whose reactor cannot be restarted in
the same process. ``--schedule`` therefore launches each crawl as a *subprocess*
rather than calling ``run_crawl`` in-process, which would raise
``ReactorNotRestartable`` on the second tick.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from crawler.database import get_db, init_db
from crawler.models import CrawlRun
from crawler.spiders.procurement_spider import ProcurementSpider


def run_crawl(
    incremental_days: int = 7,
    rows: int = 500,
    notice_types: str = "",
    country: str = "",
    max_pages: int = 0,
) -> str:
    """Execute one crawl and record it as a CrawlRun. Returns the run id."""
    init_db()
    db = get_db()

    crawl_run = CrawlRun(started_at=datetime.now(timezone.utc), status="running")
    db.add(crawl_run)
    db.commit()
    run_id = str(crawl_run.id)
    db.close()

    # The spider object is only reachable via signals once the crawl is over,
    # so capture it on spider_closed to read its counters afterwards.
    captured = {}

    def on_spider_closed(spider):
        captured["spider"] = spider

    settings = get_project_settings()
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(ProcurementSpider)
    crawler.signals.connect(on_spider_closed, signal=signals.spider_closed)

    status = "completed"
    error_message = None
    try:
        process.crawl(
            crawler,
            incremental_days=incremental_days,
            rows=rows,
            notice_types=notice_types,
            country=country,
            max_pages=max_pages,
            crawl_run_id=run_id,
        )
        process.start()  # blocks until the crawl finishes
    except Exception as exc:  # noqa: BLE001 - we want the run recorded either way
        status = "failed"
        error_message = str(exc)
        print(f"Crawl failed: {exc}", file=sys.stderr)

    spider = captured.get("spider")
    db = get_db()
    try:
        run = db.query(CrawlRun).filter(CrawlRun.id == run_id).one_or_none()
        if run:
            run.ended_at = datetime.now(timezone.utc)
            run.status = status
            if spider is not None:
                run.notices_found = getattr(spider, "notices_found", 0)
                run.notices_new = getattr(spider, "notices_new", 0)
                run.notices_updated = getattr(spider, "notices_updated", 0)
                run.errors = getattr(spider, "notices_errored", 0)
            elif error_message:
                run.errors = 1
            db.commit()

            print(
                f"Crawl {status}: found={run.notices_found} "
                f"new={run.notices_new} updated={run.notices_updated} errors={run.errors}"
            )
    finally:
        db.close()

    return run_id


def run_scheduler(interval_hours: int, days: int, rows: int):
    """Run the crawler on a fixed interval, each crawl in its own subprocess."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    script = os.path.abspath(__file__)

    def launch():
        print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scheduled crawl...")
        result = subprocess.run(
            [sys.executable, script, "--days", str(days), "--rows", str(rows)],
            cwd=os.path.dirname(script),
        )
        print(f"Scheduled crawl exited with code {result.returncode}")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(launch, "interval", hours=interval_hours, id="crawl_job")

    print(f"Scheduler started: crawling every {interval_hours}h (incremental {days}d).")
    launch()  # run once immediately rather than waiting a full interval

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


def main():
    parser = argparse.ArgumentParser(description="World Bank Procurement Crawler")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only crawl notices published in the last N days. 0 = full backfill.",
    )
    parser.add_argument("--rows", type=int, default=500, help="Notices per API page (max 1000)")
    parser.add_argument(
        "--notice-types",
        default="",
        help="Comma-separated notice_type_exact filter, e.g. 'Request for Expression of Interest'",
    )
    parser.add_argument("--country", default="", help="Filter by project country name")
    parser.add_argument("--max-pages", type=int, default=0, help="Stop after N pages (0 = no limit)")
    parser.add_argument("--schedule", action="store_true", help="Run continuously on an interval")
    parser.add_argument(
        "--interval-hours",
        type=int,
        default=int(os.getenv("CRAWL_INTERVAL_HOURS", "6")),
        help="Hours between crawls when using --schedule",
    )
    # Accepted for backwards compatibility; a bare run is already a single run.
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.schedule:
        run_scheduler(args.interval_hours, args.days, args.rows)
    else:
        run_crawl(
            incremental_days=args.days,
            rows=args.rows,
            notice_types=args.notice_types,
            country=args.country,
            max_pages=args.max_pages,
        )


if __name__ == "__main__":
    main()
