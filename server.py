"""The crawler as a long-running service: a 12-hourly schedule and a trigger endpoint.

``cron.py`` runs one cycle and exits, which is all a platform cron scheduler
needs. This module is the other deployment shape — one always-on container that

  * runs the same full cycle every ``CRAWL_INTERVAL_HOURS`` (12 by default), at
    fixed clock times in ``CRAWL_TIMEZONE`` (Asia/Jakarta by default), and
  * exposes a small HTTP endpoint so the dashboard's "Start crawling" button can
    ask for a cycle now.

Both paths go through the same :class:`JobRunner`, which holds a **single slot**:
one cycle runs at a time, whether the schedule or a person asked for it. A
trigger that arrives mid-run is answered 409 with the running job's details
rather than queued, because the useful answer to "crawl now" while a crawl is
underway is "one is already going, here is how long it has been running" — a
queued second pass over the same feeds would find the same notices.

Why the standard library and not a web framework
------------------------------------------------
Three endpoints, no templating, no ORM layer over the wire, and traffic measured
in requests per day. ``http.server`` covers that without adding a framework and
an ASGI server to an image that exists to run Scrapy.

Why a subprocess and not a function call
----------------------------------------
Scrapy runs on Twisted, whose reactor cannot be restarted inside one process, so
a second in-process crawl would raise ``ReactorNotRestartable``. Each cycle is
therefore launched as ``python cron.py``, exactly as ``run_crawler.py
--schedule`` does it.

    CRAWLER_TRIGGER_TOKEN=... python server.py        # port 8080 by default

Endpoints
---------
``GET  /health``   liveness, unauthenticated — the only thing a platform probe needs
``GET  /status``   is a cycle running, when the next one is due, how the last one ended
``POST /crawl``    start a cycle now; 202 if it started, 409 if one is running
"""
# The image runs 3.11, but a developer's system Python may be older and
# `dict[str, Any]` in a signature is evaluated at import time before 3.9. This
# defers every annotation to a string, so the modern spelling stays readable
# without pinning where the module can be imported.
from __future__ import annotations

import errno
import hmac
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CRON_SCRIPT = os.path.join(HERE, "cron.py")

# Read the .env here rather than relying on `crawler.database` to do it on
# import. That import happens inside `main()`, which is *after* the settings
# below are read, so without this a token set only in .env looked unset and the
# server refused to start. Containers pass real environment variables and so
# never hit it; a developer running `python server.py` hits it every time.
# `load_dotenv` does not overwrite what is already set, so the real environment
# still wins.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"))
except ImportError:  # pragma: no cover - only when running outside the venv
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat(timespec="seconds") if moment else None


def _log(message: str) -> None:
    print(f"[{_stamp(_now())}] {message}", flush=True)


class JobRunner:
    """Runs one pipeline cycle at a time, and remembers how the last one went.

    ``start`` is safe to call from the HTTP threads and from the scheduler
    thread at once: the slot check and the launch happen under one lock, so two
    callers racing can never produce two crawls.
    """

    def __init__(self, days: int, rows: int):
        self._days = days
        self._rows = rows
        self._lock = threading.Lock()
        self._active = False
        self._trigger: Optional[str] = None
        self._started_at: Optional[datetime] = None
        self._last: Optional[dict[str, Any]] = None

    # -- state ------------------------------------------------------------

    def _running(self) -> bool:
        """True between the launch and the outcome being recorded.

        This is a flag rather than a ``poll()`` of the subprocess, and the
        difference matters: a poll goes false the instant the process exits,
        which is *before* the reaper has recorded how it went, so a status read
        landing in that gap would report a finished crawl with no result — and
        the dashboard would announce a clean finish for a run that failed. The
        flag is cleared in the same locked block that stores the outcome, so
        "no longer running" and "here is how it went" become one step.

        Call with the lock held.
        """
        return self._active

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    # -- launching --------------------------------------------------------

    def start(self, trigger: str) -> tuple[bool, dict[str, Any]]:
        """Launch a cycle unless one is already running.

        Returns ``(started, snapshot)``. When ``started`` is False the snapshot
        describes the crawl that is already in flight, which is what the caller
        reports back to the person who pressed the button.
        """
        with self._lock:
            if self._running():
                _log(f"Trigger from {trigger} ignored: a crawl is already running.")
                return False, self._snapshot_locked()

            command = [
                sys.executable,
                CRON_SCRIPT,
                "--days",
                str(self._days),
                "--rows",
                str(self._rows),
            ]
            _log(f"Starting a cycle ({trigger}): {' '.join(command)}")
            # stdout and stderr are inherited on purpose: the cycle's log is the
            # service's log, so a crawl started from the dashboard is read in the
            # same place as a scheduled one.
            process = subprocess.Popen(command, cwd=HERE)
            self._active = True
            self._trigger = trigger
            self._started_at = _now()
            started_at = self._started_at

            threading.Thread(
                target=self._reap,
                args=(process, trigger, started_at),
                daemon=True,
            ).start()
            return True, self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        """The state read, for callers that already hold the lock.

        The lock is not reentrant, so `start` cannot call `snapshot` to describe
        the job it just launched (or the one already running) — hence the split.
        """
        running = self._running()
        return {
            "running": running,
            "trigger": self._trigger if running else None,
            "started_at": _stamp(self._started_at) if running else None,
            "running_for_seconds": (
                int((_now() - self._started_at).total_seconds())
                if running and self._started_at
                else None
            ),
            "interval_hours": SCHEDULE_HOURS,
            # The dashboard counts down to this, so it is reported whether or
            # not a crawl is running: a manual crawl does not move the schedule.
            "next_run_at": _next_run_at(),
            "schedule_timezone": SCHEDULE_TZ,
            "last_run": self._last,
        }

    def _reap(self, process: subprocess.Popen, trigger: str, started_at: datetime) -> None:
        """Wait out one cycle, then free the slot and record how it went."""
        try:
            code = process.wait()
        except BaseException as exc:  # noqa: BLE001 - the slot must never leak
            # Nothing here is expected to raise, but if it somehow does the slot
            # would stay held for the life of the process and no crawl could
            # ever start again. Release it and record the run as failed.
            code = -1
            _log(f"Reaping the cycle failed: {exc}")

        ended_at = _now()
        outcome = {
            "trigger": trigger,
            "started_at": _stamp(started_at),
            "ended_at": _stamp(ended_at),
            "duration_seconds": int((ended_at - started_at).total_seconds()),
            "exit_code": code,
            # cron.py exits non-zero when any stage failed; it still wrote
            # whatever the healthy stages produced, so this is "check the log",
            # not "nothing happened".
            "status": "completed" if code == 0 else "failed",
        }
        # One locked step, so a status read can never catch "finished" without
        # the outcome that goes with it.
        with self._lock:
            self._active = False
            self._last = outcome
        _log(
            f"Cycle {outcome['status']} ({trigger}) in {outcome['duration_seconds']}s, "
            f"exit code {code}."
        )


# The runner is process-wide state: the HTTP handler class is instantiated per
# request, so it cannot own it.
SCHEDULE_HOURS = int(os.getenv("CRAWL_INTERVAL_HOURS", "12"))
# The team reads this dashboard in Jakarta, so the crawl times are set on their
# clock rather than on UTC: "midnight and noon" should mean their midnight.
# Left as a name rather than a tzinfo object: APScheduler resolves it, and
# `zoneinfo` would make this module unimportable on the 3.8 a developer may
# still have as their system Python.
SCHEDULE_TZ = os.getenv("CRAWL_TIMEZONE", "Asia/Jakarta")
RUNNER: Optional[JobRunner] = None
SCHEDULER: Optional[Any] = None
TOKEN = os.getenv("CRAWLER_TRIGGER_TOKEN", "").strip()


def _next_run_at() -> Optional[str]:
    """When the schedule fires next, as UTC ISO-8601, or None before start-up.

    Read from the scheduler rather than computed from the last run: those two
    agree only while nothing has been triggered by hand, and after a manual
    crawl the schedule is the one telling the truth.
    """
    if SCHEDULER is None:
        return None
    job = SCHEDULER.get_job("crawl_cycle")
    moment = getattr(job, "next_run_time", None) if job else None
    return _stamp(moment.astimezone(timezone.utc)) if moment else None


class Handler(BaseHTTPRequestHandler):
    server_version = "tender-intelligence-crawler"
    # Without this the class advertises the Python version to every caller.
    sys_version = ""

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """Check the shared secret, comparing in constant time.

        Accepts either `Authorization: Bearer <token>` or `X-Trigger-Token`,
        because one of the two is always the more natural fit for whatever is
        calling — a browser proxy, curl, a platform health check.
        """
        presented = self.headers.get("X-Trigger-Token", "")
        if not presented:
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                presented = header[7:]
        return hmac.compare_digest(presented.strip(), TOKEN)

    def _deny(self) -> None:
        self._send(401, {"detail": "Invalid or missing trigger token"})

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/health"):
            # Deliberately unauthenticated and deliberately not reporting job
            # state: this answers "is the service up", which is what a platform
            # probe asks, and it must not leak anything to an unauthenticated
            # caller.
            self._send(200, {"status": "ok"})
            return

        if path == "/status":
            if not self._authorized():
                self._deny()
                return
            self._send(200, RUNNER.snapshot())
            return

        self._send(404, {"detail": f"No such endpoint: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path != "/crawl":
            self._send(404, {"detail": f"No such endpoint: {path}"})
            return

        if not self._authorized():
            self._deny()
            return

        # Drain any body the caller sent. Nothing here reads it — the cycle's
        # shape comes from the service's own configuration, so a manual run and
        # a scheduled one do identical work — but leaving it unread breaks
        # keep-alive for the next request on the connection.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        started, state = RUNNER.start(trigger="manual")
        if not started:
            self._send(
                409,
                {
                    "detail": "A crawl is already running",
                    **state,
                },
            )
            return
        self._send(202, {"detail": "Crawl started", **state})

    # -- logging ----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Route access logs through the same stamped format as everything else."""
        _log(f"{self.address_string()} {format % args}")


def _scheduled_cycle() -> None:
    RUNNER.start(trigger="schedule")


def _build_trigger():
    """The recurrence, as fixed clock times where the cadence allows it.

    An interval trigger counts from the moment the scheduler starts, so the
    crawl times drift to wherever the last deploy happened to land — a service
    redeployed at 14:20 crawls at 02:20 and 14:20 until the next deploy moves it
    again. That is fine for a job nobody watches, but the dashboard now counts
    down to the next run, and a countdown to a time that moves on every deploy
    is worse than none.

    So when the cadence divides the day evenly — 12h, 8h, 6h, the realistic
    settings — it becomes a cron trigger on the hour, starting at midnight
    Jakarta time. Anything else (7h, say) has no fixed times to sit on and keeps
    the plain interval.
    """
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if 0 < SCHEDULE_HOURS <= 24 and 24 % SCHEDULE_HOURS == 0:
        hours = ",".join(str(hour) for hour in range(0, 24, SCHEDULE_HOURS))
        return CronTrigger(hour=hours, minute=0, timezone=SCHEDULE_TZ)
    return IntervalTrigger(hours=SCHEDULE_HOURS, timezone=SCHEDULE_TZ)


def main() -> int:
    global RUNNER, SCHEDULER

    if not TOKEN:
        print(
            "CRAWLER_TRIGGER_TOKEN is not set. The trigger endpoint would accept "
            "a crawl request from anyone that can reach this service, so it "
            "refuses to start without one. Set it here and to the same value in "
            "the tender-intelligence app.",
            file=sys.stderr,
        )
        return 1

    from crawler.database import check_connection

    check_connection()

    days = int(os.getenv("CRAWL_DAYS", "7"))
    rows = int(os.getenv("CRAWL_ROWS", "500"))
    RUNNER = JobRunner(days=days, rows=rows)

    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=SCHEDULE_TZ)
    scheduler.add_job(
        _scheduled_cycle,
        _build_trigger(),
        id="crawl_cycle",
        # The runner's single slot already prevents overlap; this keeps
        # APScheduler from stacking up missed ticks behind a long crawl.
        max_instances=1,
        coalesce=True,
        # A cycle runs for minutes and a deploy can land on top of a fire time.
        # Without this, a tick the restart made a few minutes late is dropped
        # and the feeds go a full period unread.
        misfire_grace_time=1800,
    )
    scheduler.start()
    SCHEDULER = scheduler

    port = int(os.getenv("PORT", "8080"))
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # Nearly always a forgotten instance from an earlier terminal, and the
        # bare traceback for it names a socket call rather than the problem.
        # Say what is wrong and how to see who holds the port.
        scheduler.shutdown(wait=False)
        print(
            f"Port {port} is already in use — something else is listening, most "
            f"likely another `python server.py` you started earlier. Find it "
            f"with `ss -ltnp | grep :{port}` and stop it, or set PORT to a free "
            f"port and point the app's CRAWLER_URL at that one.",
            file=sys.stderr,
        )
        return 1
    next_run = SCHEDULER.get_job("crawl_cycle").next_run_time
    _log(
        f"Listening on :{port}. Full cycle every {SCHEDULE_HOURS}h "
        f"(incremental {days}d), and on POST /crawl. Next run "
        f"{next_run.strftime('%Y-%m-%d %H:%M %Z') if next_run else 'unscheduled'}."
    )

    if os.getenv("CRAWL_ON_BOOT", "").lower() in ("true", "1", "yes"):
        # Off by default: a deploy should not spend an LLM budget on a crawl
        # nobody asked for, and a restart loop would repeat it.
        _log("CRAWL_ON_BOOT is set — running one cycle now.")
        RUNNER.start(trigger="boot")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("Shutting down.")
    finally:
        scheduler.shutdown(wait=False)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
