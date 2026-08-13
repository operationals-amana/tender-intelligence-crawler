import os
import subprocess
import sys
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from api.schemas import CrawlRunOut
from crawler.database import get_session
from crawler.models import CrawlError, CrawlRun

router = APIRouter()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@router.get("/history", response_model=List[CrawlRunOut])
def get_crawl_history(
    limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_session)
):
    return db.query(CrawlRun).order_by(desc(CrawlRun.started_at)).limit(limit).all()


@router.get("/errors")
def get_crawl_errors(
    limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_session)
):
    errors = (
        db.query(CrawlError).order_by(desc(CrawlError.occurred_at)).limit(limit).all()
    )
    return [
        {
            "id": str(e.id),
            "notice_id": e.notice_id,
            "error_type": e.error_type,
            "error_message": e.error_message,
            "occurred_at": e.occurred_at,
        }
        for e in errors
    ]


def _run_crawler_subprocess(days: int, rows: int):
    """Launch the crawler as its own process.

    Scrapy runs on the Twisted reactor, which cannot be started twice inside one
    process. Running it in the API worker would therefore succeed exactly once
    and raise ReactorNotRestartable on every later trigger, so each crawl gets a
    fresh interpreter.
    """
    subprocess.run(
        [
            sys.executable,
            os.path.join(PROJECT_ROOT, "run_crawler.py"),
            "--days",
            str(days),
            "--rows",
            str(rows),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )


@router.post("/trigger")
def trigger_crawl(
    background_tasks: BackgroundTasks,
    days: int = Query(7, ge=0, le=3650, description="Days back to crawl; 0 = full"),
    rows: int = Query(500, ge=1, le=1000),
):
    background_tasks.add_task(_run_crawler_subprocess, days, rows)
    return {
        "status": "crawl_triggered",
        "message": f"Crawl started in the background (last {days} days).",
    }
