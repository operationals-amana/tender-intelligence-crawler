from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, desc, func, or_
from sqlalchemy.orm import Session

from api.schemas import RecommendationOut, TenderOut, TenderScoreOut
from crawler.database import get_session
from crawler.models import Tender, TenderScore

router = APIRouter()


def _open_tenders(query):
    """Restrict to notices that are still open (or carry no deadline)."""
    return query.filter(
        or_(
            Tender.submission_deadline.is_(None),
            Tender.submission_deadline >= func.now(),
        )
    )


@router.get("", response_model=List[RecommendationOut])
def list_recommendations(
    min_score: float = Query(0, ge=0, le=100),
    country: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    notice_type: Optional[str] = Query(None),
    recommendation: Optional[str] = Query(
        None, description="Filter on Claude's verdict: pursue, review or decline"
    ),
    assessed_only: bool = Query(
        True, description="Only tenders Claude has actually read"
    ),
    include_expired: bool = Query(False),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    """Opportunities ranked by fit, best first.

    Ranking prefers Claude's assessment where it exists and falls back to the
    heuristic score otherwise, so a shortlist that has been read properly always
    outranks one that has only been keyword-matched.
    """
    query = db.query(TenderScore, Tender).join(Tender, TenderScore.tender_id == Tender.id)

    if not include_expired:
        query = _open_tenders(query)
    if assessed_only:
        query = query.filter(TenderScore.llm_scored_at.isnot(None), TenderScore.llm_error.is_(None))
    if recommendation:
        query = query.filter(TenderScore.llm_recommendation == recommendation)
    if country:
        query = query.filter(Tender.project_country.ilike(f"%{country}%"))
    if sector:
        query = query.filter(Tender.sector.ilike(f"%{sector}%"))
    if notice_type:
        query = query.filter(Tender.notice_type == notice_type)

    # Rank on the LLM score when present, else the heuristic one.
    effective = func.coalesce(TenderScore.llm_overall_score, TenderScore.overall_score)
    if min_score:
        query = query.filter(effective >= min_score)

    rows = query.order_by(desc(effective), Tender.submission_deadline).offset(offset).limit(limit).all()

    return [
        RecommendationOut(
            tender=TenderOut.model_validate(tender),
            score=TenderScoreOut.model_validate(score),
        )
        for score, tender in rows
    ]


@router.get("/summary")
def recommendation_summary(db: Session = Depends(get_session)):
    """Counts by Claude's verdict, for the page header."""
    rows = (
        _open_tenders(
            db.query(TenderScore.llm_recommendation, func.count(TenderScore.id))
            .join(Tender, TenderScore.tender_id == Tender.id)
            .filter(TenderScore.llm_scored_at.isnot(None), TenderScore.llm_error.is_(None))
        )
        .group_by(TenderScore.llm_recommendation)
        .all()
    )
    counts = {verdict or "unknown": count for verdict, count in rows}

    assessed = sum(counts.values())
    avg = (
        _open_tenders(
            db.query(func.avg(TenderScore.llm_overall_score)).join(
                Tender, TenderScore.tender_id == Tender.id
            )
        )
        .filter(TenderScore.llm_scored_at.isnot(None))
        .scalar()
    )

    return {
        "assessed": assessed,
        "pursue": counts.get("pursue", 0),
        "review": counts.get("review", 0),
        "decline": counts.get("decline", 0),
        "average_llm_score": round(float(avg or 0), 1),
        "bands": [
            {"band": "Pursue", "count": counts.get("pursue", 0)},
            {"band": "Review", "count": counts.get("review", 0)},
            {"band": "Decline", "count": counts.get("decline", 0)},
        ],
    }


@router.get("/explain/{tender_id}", response_model=RecommendationOut)
def explain_recommendation(tender_id: str, db: Session = Depends(get_session)):
    """Full scoring rationale for one tender."""
    row = (
        db.query(TenderScore, Tender)
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(cast(Tender.id, String) == tender_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No score found for this tender")

    score, tender = row
    return RecommendationOut(
        tender=TenderOut.model_validate(tender),
        score=TenderScoreOut.model_validate(score),
    )
