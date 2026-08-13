from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, desc, func, or_
from sqlalchemy.orm import Session

from api.schemas import DashboardStatsOut, RecommendationOut, TenderOut, TenderScoreOut
from crawler.database import get_session
from crawler.models import CrawlRun, Tender, TenderScore

router = APIRouter()


@router.get("/stats", response_model=DashboardStatsOut)
def get_dashboard_stats(db: Session = Depends(get_session)):
    now = datetime.now(timezone.utc)

    def open_tenders():
        return db.query(Tender).filter(
            Tender.notice_status == "Published",
            Tender.submission_deadline >= now,
        )

    total_active = open_tenders().count()
    deadlines_week = open_tenders().filter(
        Tender.submission_deadline <= now + timedelta(days=7)
    ).count()
    deadlines_month = open_tenders().filter(
        Tender.submission_deadline <= now + timedelta(days=30)
    ).count()

    # Average across open opportunities only; expired ones would drag it down.
    avg_score, scored_count = (
        db.query(func.avg(TenderScore.overall_score), func.count(TenderScore.id))
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(
            or_(
                Tender.submission_deadline.is_(None),
                Tender.submission_deadline >= now,
            )
        )
        .one()
    )

    last_crawl = db.query(CrawlRun).order_by(desc(CrawlRun.started_at)).first()

    # Rank on Claude's assessment where present, else the heuristic score.
    effective = func.coalesce(TenderScore.llm_overall_score, TenderScore.overall_score)
    top_rows = (
        db.query(TenderScore, Tender)
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(Tender.submission_deadline >= now)
        .order_by(desc(effective))
        .limit(6)
        .all()
    )

    assessed_count = (
        db.query(TenderScore)
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(
            TenderScore.llm_scored_at.isnot(None),
            TenderScore.llm_error.is_(None),
            or_(
                Tender.submission_deadline.is_(None),
                Tender.submission_deadline >= now,
            ),
        )
        .count()
    )
    pursue_count = (
        db.query(TenderScore)
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(
            TenderScore.llm_recommendation == "pursue",
            or_(
                Tender.submission_deadline.is_(None),
                Tender.submission_deadline >= now,
            ),
        )
        .count()
    )

    # Deadline volume for the next four weeks, for the dashboard chart.
    histogram = []
    for week in range(4):
        start = now + timedelta(days=7 * week)
        end = start + timedelta(days=7)
        count = (
            db.query(Tender)
            .filter(
                Tender.notice_status == "Published",
                Tender.submission_deadline >= start,
                Tender.submission_deadline < end,
            )
            .count()
        )
        histogram.append({"label": f"Week {week + 1}", "count": count})

    country_rows = (
        db.query(Tender.project_country, func.count(Tender.id))
        .filter(
            Tender.notice_status == "Published",
            Tender.submission_deadline >= now,
            Tender.project_country.isnot(None),
        )
        .group_by(Tender.project_country)
        .order_by(desc(func.count(Tender.id)))
        .limit(8)
        .all()
    )

    return DashboardStatsOut(
        total_active_tenders=total_active,
        deadlines_this_week=deadlines_week,
        deadlines_next_30_days=deadlines_month,
        average_score=round(float(avg_score or 0), 1),
        scored_tenders=int(scored_count or 0),
        assessed_tenders=assessed_count,
        pursue_count=pursue_count,
        last_crawl=last_crawl.ended_at or last_crawl.started_at if last_crawl else None,
        last_crawl_status=last_crawl.status if last_crawl else None,
        top_opportunities=[
            RecommendationOut(
                tender=TenderOut.model_validate(tender),
                score=TenderScoreOut.model_validate(score),
            )
            for score, tender in top_rows
        ],
        deadline_histogram=histogram,
        top_countries=[{"country": c, "count": n} for c, n in country_rows],
    )


@router.get("/upcoming", response_model=List[TenderOut])
def get_upcoming_tenders(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_session),
):
    """Open notices with the nearest deadlines."""
    now = datetime.now(timezone.utc)
    return (
        db.query(Tender)
        .filter(
            Tender.notice_status == "Published",
            Tender.submission_deadline >= now,
            Tender.submission_deadline <= now + timedelta(days=days),
        )
        .order_by(Tender.submission_deadline)
        .limit(limit)
        .all()
    )
