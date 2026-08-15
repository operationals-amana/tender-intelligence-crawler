"""Run the procurement crawlers.

Most of the time you want ``cron.py`` instead: it runs the whole pipeline, of
which this is the first stage. This module is the crawl itself, exposed
separately so a crawl can be run or debugged on its own.

Usage:
    python run_crawler.py                     # every source, incremental, last 7 days
    python run_crawler.py --source adb        # one source only
    python run_crawler.py --days 0            # full backfill
    python run_crawler.py --days 30 --rows 500
    python run_crawler.py --schedule          # full cycle, forever, on an interval

Two feeds, one reactor
----------------------
Scrapy runs on Twisted, whose reactor cannot be started twice in a process. Both
spiders are therefore queued on a *single* ``CrawlerProcess`` and run
concurrently under one ``process.start()``. Calling ``run_crawl`` twice in one
process would raise ``ReactorNotRestartable`` on the second call, which is the
same reason ``--schedule`` launches each cycle as a subprocess.

Both feeds are recorded as one ``CrawlRun``. The run answers "did the crawl
work", and a crawl that refreshed one bank and failed the other did not work;
splitting it into two rows would let a broken feed hide behind a healthy one.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from crawler.database import check_connection, get_db
from crawler.models import CrawlRun
from crawler.sources import ADB, SOURCES, WORLD_BANK
from crawler.spiders.adb_spider import AdbSpider
from crawler.spiders.procurement_spider import ProcurementSpider

#: Spider class and per-source crawl arguments, keyed by source name. The two
#: feeds take different arguments — the World Bank one pages an API that has no
#: date filter, ADB's queries an index that does — so each source names the
#: arguments it accepts rather than sharing one signature that fits neither.
SPIDERS = {
    WORLD_BANK: ProcurementSpider,
    ADB: AdbSpider,
}


def _spider_kwargs(source: str, incremental_days: int, rows: int, country: str, max_pages: int):
    if source == ADB:
        return {
            "incremental_days": incremental_days,
            # The ADB index caps usefully lower than the World Bank API; its
            # spider clamps this itself, and the shared `--rows` is meant for
            # the feed that actually needs tuning.
            "rows": min(rows, 200),
            "country": country,
            "max_pages": max_pages,
        }
    return {
        "incremental_days": incremental_days,
        "rows": rows,
        "country": country,
        "max_pages": max_pages,
    }


def run_crawl(
    incremental_days: int = 7,
    rows: int = 500,
    notice_types: str = "",
    country: str = "",
    max_pages: int = 0,
    sources=None,
) -> str:
    """Execute one crawl of each source and record it as a CrawlRun.

    Returns the run id.
    """
    check_connection()
    selected = list(sources or SOURCES)
    unknown = [s for s in selected if s not in SPIDERS]
    if unknown:
        raise ValueError(f"Unknown source(s): {', '.join(unknown)}")

    db = get_db()
    crawl_run = CrawlRun(started_at=datetime.now(timezone.utc), status="running")
    db.add(crawl_run)
    db.commit()
    run_id = str(crawl_run.id)
    db.close()

    # Spider objects are only reachable via signals once a crawl is over, so
    # capture each on spider_closed to read its counters afterwards.
    #
    # `reason` is captured with it and is the only reliable failure signal here:
    # Scrapy swallows spider errors inside the reactor, so `process.start()`
    # returns normally even when a spider aborted, and `CloseSpider` fires this
    # signal just as a clean finish does. Anything other than "finished" — a
    # rejected ADB token, a cancelled crawl — is a failed run.
    captured = []

    def on_spider_closed(spider, reason):
        captured.append((spider, reason))

    settings = get_project_settings()
    process = CrawlerProcess(settings)

    status = "completed"
    error_message = None
    try:
        for source in selected:
            crawler = process.create_crawler(SPIDERS[source])
            crawler.signals.connect(on_spider_closed, signal=signals.spider_closed)
            kwargs = _spider_kwargs(source, incremental_days, rows, country, max_pages)
            # Only the World Bank spider filters on notice type upstream; ADB's
            # equivalent argument names different values, so it is not shared.
            if source == WORLD_BANK:
                kwargs["notice_types"] = notice_types
            process.crawl(crawler, crawl_run_id=run_id, **kwargs)
        process.start()  # blocks until every queued crawl finishes
    except Exception as exc:  # noqa: BLE001 - we want the run recorded either way
        status = "failed"
        error_message = str(exc)
        print(f"Crawl failed: {exc}", file=sys.stderr)

    db = get_db()
    try:
        run = db.query(CrawlRun).filter(CrawlRun.id == run_id).one_or_none()
        if run:
            run.ended_at = datetime.now(timezone.utc)
            run.status = status
            if captured:
                # Summed across the feeds: the run is one crawl of everything,
                # and the per-spider breakdown is in the log above.
                run.notices_found = sum(getattr(s, "notices_found", 0) for s, _ in captured)
                run.notices_new = sum(getattr(s, "notices_new", 0) for s, _ in captured)
                run.notices_updated = sum(
                    getattr(s, "notices_updated", 0) for s, _ in captured
                )
                run.errors = sum(getattr(s, "notices_errored", 0) for s, _ in captured)
            elif error_message:
                run.errors = 1

            # A crawl is only "completed" if every selected spider ran *and*
            # every one of them finished for the right reason. Without this a
            # rotated ADB token records a green run that found nothing, which is
            # the failure mode most likely to go unnoticed.
            aborted = [
                (s.name, reason) for s, reason in captured if reason != "finished"
            ]
            missing = len(selected) - len(captured)
            if status == "completed" and (aborted or missing):
                status = "failed"
                run.status = status
                run.errors = max(run.errors or 0, len(aborted) + missing)
                for name, reason in aborted:
                    print(f"Spider {name} aborted: {reason}", file=sys.stderr)
            db.commit()

            print(
                f"Crawl {run.status} ({', '.join(selected)}): found={run.notices_found} "
                f"new={run.notices_new} updated={run.notices_updated} errors={run.errors}"
            )
    finally:
        db.close()

    # Raised *after* the run is recorded, so the failure is both in the table and
    # in the exit code. `cron.py` catches it and carries on to scoring, which can
    # still work through what is already stored.
    if status == "failed":
        raise RuntimeError(
            error_message or f"crawl run {run_id} did not complete; see the log above"
        )

    return run_id


def run_scheduler(interval_hours: int, days: int, rows: int):
    """Run the full pipeline on a fixed interval, each cycle in its own process.

    This is the fallback for platforms with no cron scheduler of their own; where
    one exists, schedule ``python cron.py`` directly and skip the always-on
    container. Each tick runs ``cron.py``, not just the crawl, so scheduled runs
    and cron runs do exactly the same work.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "cron.py")

    def launch():
        print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scheduled cycle...")
        result = subprocess.run(
            [sys.executable, script, "--days", str(days), "--rows", str(rows)],
            cwd=here,
        )
        print(f"Scheduled cycle exited with code {result.returncode}")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(launch, "interval", hours=interval_hours, id="crawl_job")

    print(f"Scheduler started: full cycle every {interval_hours}h (incremental {days}d).")
    launch()  # run once immediately rather than waiting a full interval

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


def main():
    parser = argparse.ArgumentParser(description="Procurement notice crawler")
    parser.add_argument(
        "--source",
        default="all",
        help=f"Which feed to crawl: all, or one of {', '.join(SOURCES)}",
    )
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
    parser.add_argument(
        "--country",
        default="",
        help=(
            "Filter by project country name. Each bank names countries its own "
            "way, so a value that matches one may match nothing in the other "
            "(ADB writes 'Viet Nam' and 'China, People's Republic of'). Pair it "
            "with --source when that matters."
        ),
    )
    parser.add_argument("--max-pages", type=int, default=0, help="Stop after N pages (0 = no limit)")
    parser.add_argument("--schedule", action="store_true", help="Run the full pipeline continuously on an interval")
    parser.add_argument(
        "--interval-hours",
        type=int,
        default=int(os.getenv("CRAWL_INTERVAL_HOURS", "6")),
        help="Hours between crawls when using --schedule",
    )
    # Accepted for backwards compatibility; a bare run is already a single run.
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    sources = None if args.source in ("", "all") else [args.source]

    if args.schedule:
        run_scheduler(args.interval_hours, args.days, args.rows)
    else:
        run_crawl(
            incremental_days=args.days,
            rows=args.rows,
            notice_types=args.notice_types,
            country=args.country,
            max_pages=args.max_pages,
            sources=sources,
        )


if __name__ == "__main__":
    main()
