"""One full pipeline cycle: crawl, link duplicates, score, assess, exit.

This is the service's entry point. It is a **cron job**, not a server: it runs
once, writes its results to Postgres and exits non-zero if the crawl failed. All
reading, filtering and CRUD happens in the tender-intelligence app, which owns
the same database.

    python cron.py                        # crawl last 7 days, score, assess
    python cron.py --days 0               # full backfill
    python cron.py --source adb           # refresh one feed only
    python cron.py --skip-llm             # heuristic scoring only, no API spend
    python cron.py --crawl-only           # just refresh the notices

The stages are deliberately sequential and independent: a failed crawl still
lets the scorer work through whatever is already in the table, and a failed
assessment does not lose the crawl. Each stage reports its own outcome and the
exit code reflects the worst of them.

Duplicate linking runs *between* the crawl and scoring, and not at either end of
it. It needs every feed's notices already stored, and scoring needs to know
which rows are duplicates before it spends a token on them.

Scrapy runs on Twisted, whose reactor cannot be restarted inside one process, so
this script must crawl at most once per invocation. That is why scheduling lives
outside it — in the platform's cron, or in ``run_crawler.py --schedule``, which
launches each crawl as a subprocess.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from crawler.database import check_connection, get_db


def _log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl, score and assess tenders")
    parser.add_argument(
        "--days",
        type=int,
        default=int(os.getenv("CRAWL_DAYS", "7")),
        help="Only crawl notices published in the last N days. 0 = full backfill.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=int(os.getenv("CRAWL_ROWS", "500")),
        help="Notices per API page (max 1000)",
    )
    parser.add_argument(
        "--source",
        default=os.getenv("CRAWL_SOURCE", "all"),
        help="Which feed to crawl: all, worldbank, adb or giz",
    )
    parser.add_argument("--crawl-only", action="store_true", help="Skip both scoring stages")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        default=os.getenv("SKIP_LLM", "").lower() in ("true", "1", "yes"),
        help="Run heuristic scoring but not Claude's assessment",
    )
    parser.add_argument(
        "--llm-limit",
        type=int,
        default=int(os.getenv("LLM_LIMIT", "300")),
        help="How many shortlisted tenders Claude assesses",
    )
    parser.add_argument(
        "--llm-concurrency",
        type=int,
        default=int(os.getenv("LLM_CONCURRENCY", "4")),
        help="Parallel Claude requests",
    )
    args = parser.parse_args()

    check_connection()
    failures = []

    # --- 1. crawl ---------------------------------------------------------
    sources = None if args.source in ("", "all") else [args.source]
    _log(
        f"Crawling {args.source} notices from the last {args.days} days..."
    )
    try:
        from run_crawler import run_crawl

        run_crawl(incremental_days=args.days, rows=args.rows, sources=sources)
    except Exception as exc:  # noqa: BLE001 - a failed crawl must not skip scoring
        failures.append(f"crawl: {exc}")
        _log(f"Crawl failed: {exc}")

    if args.crawl_only:
        return _finish(failures)

    # --- 2. duplicate linking --------------------------------------------
    # Before scoring, so a package advertised by both banks is read once.
    db = get_db()
    try:
        from crawler.dedup import link_duplicates

        _log("Linking notices that advertise the same opportunity...")
        try:
            link_duplicates(db)
        except Exception as exc:  # noqa: BLE001 - scoring still works unlinked
            failures.append(f"dedup: {exc}")
            _log(f"Duplicate linking failed: {exc}")
    finally:
        db.close()

    # --- 3. heuristic scoring --------------------------------------------
    db = get_db()
    try:
        from scoring.runner import run_llm_assessment, score_tenders

        _log("Scoring tenders against the AMANA capability profile...")
        try:
            score_tenders(db)
        except Exception as exc:  # noqa: BLE001
            # Roll back before moving on: a failed flush leaves the session
            # unusable, and without this the assessment stage below dies of
            # PendingRollbackError rather than of anything to do with itself —
            # which is exactly the coupling these stages are meant not to have.
            db.rollback()
            failures.append(f"scoring: {exc}")
            _log(f"Scoring failed: {exc}")

        # --- 4. Claude assessment ----------------------------------------
        if args.skip_llm:
            _log("Skipping the Claude assessment stage (--skip-llm).")
        elif not os.getenv("ANTHROPIC_API_KEY"):
            _log("ANTHROPIC_API_KEY is not set, skipping the Claude assessment stage.")
        else:
            _log(f"Assessing the top {args.llm_limit} shortlisted tenders with Claude...")
            try:
                run_llm_assessment(
                    db, limit=args.llm_limit, concurrency=args.llm_concurrency
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failures.append(f"assessment: {exc}")
                _log(f"Assessment failed: {exc}")
    finally:
        db.close()

    # --- 5. practice-group classification --------------------------------
    # The classifier lives in the tender-intelligence app (it is TypeScript,
    # shared with the app's own override endpoints), so the cycle ends by
    # asking the app to classify whatever was just written. Deliberately not
    # counted as a failed stage: the app also runs a daily catch-up cron, so a
    # missed call here delays classification rather than losing it.
    _trigger_classification()

    return _finish(failures)


def _trigger_classification() -> None:
    """POST to the app's classification endpoint, if one is configured.

    ``APP_CLASSIFY_URL`` is the app's ``/api/crawl/classify`` route;
    ``CLASSIFY_TRIGGER_TOKEN`` must match the app's value of the same name.
    The pass is idempotent and usually takes seconds, but a large catch-up can
    run to a few minutes — hence the generous timeout.
    """
    url = os.getenv("APP_CLASSIFY_URL")
    if not url:
        return

    request = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {os.getenv('CLASSIFY_TRIGGER_TOKEN', '')}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=330) as response:
            result = json.loads(response.read().decode("utf-8"))
        _log(
            "Practice-group classification: "
            f"{result.get('classified', 0)} of {result.get('read', 0)} changed "
            f"notices classified into {result.get('assignments', 0)} assignments."
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"Practice-group classification trigger failed (will catch up on the app's cron): {exc}")


def _finish(failures) -> int:
    if failures:
        _log(f"Finished with {len(failures)} failed stage(s): {'; '.join(failures)}")
        return 1
    _log("Finished: crawl, scoring and assessment all completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
