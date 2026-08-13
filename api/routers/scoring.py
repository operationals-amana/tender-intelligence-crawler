"""Endpoints for inspecting and re-running the opportunity scoring engine."""
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.scoring.engine import WEIGHTS
from crawler.database import get_db, get_session
from crawler.models import TenderScore

router = APIRouter()


@router.get("/weights")
def get_weights():
    """The dimension weights behind every overall score."""
    return {
        "weights": WEIGHTS,
        "dimensions": [
            {"key": "capability", "label": "Capability match", "weight": WEIGHTS["capability"],
             "description": "TF-IDF similarity between the notice and AMANA's capability domains, weighted by how deep the bench is in each."},
            {"key": "sector", "label": "Sector relevance", "weight": WEIGHTS["sector"],
             "description": "Share of the notice's sectors where AMANA has demonstrated experience, adjusted for procurement type."},
            {"key": "experience", "label": "Past experience", "weight": WEIGHTS["experience"],
             "description": "Similarity to delivered projects, with bonuses for the same country and known clients."},
            {"key": "deadline", "label": "Deadline urgency", "weight": WEIGHTS["deadline"],
             "description": "Rises as the submission deadline approaches, within a 90-day horizon."},
            {"key": "network", "label": "Network strength", "weight": WEIGHTS["network"],
             "description": "Country presence, association coverage and existing client relationships."},
        ],
    }


@router.get("/summary")
def get_scoring_summary(db: Session = Depends(get_session)):
    """Distribution of scores, for dashboard context."""
    total, avg, best = db.query(
        func.count(TenderScore.id),
        func.avg(TenderScore.overall_score),
        func.max(TenderScore.overall_score),
    ).one()

    bands = []
    for label, low, high in (
        ("Strong (75+)", 75, 101),
        ("Promising (60-74)", 60, 75),
        ("Possible (45-59)", 45, 60),
        ("Weak (<45)", -1, 45),
    ):
        count = (
            db.query(TenderScore)
            .filter(TenderScore.overall_score >= low, TenderScore.overall_score < high)
            .count()
        )
        bands.append({"band": label, "count": count})

    return {
        "scored_tenders": int(total or 0),
        "average_score": round(float(avg or 0), 1),
        "best_score": round(float(best or 0), 1),
        "bands": bands,
    }


def _rescore(rescore_all: bool, limit):
    from api.scoring.runner import score_tenders

    db = get_db()
    try:
        score_tenders(db, limit=limit, rescore_all=rescore_all, verbose=True)
    finally:
        db.close()


@router.post("/run")
def run_scoring(
    background_tasks: BackgroundTasks,
    rescore_all: bool = Query(False, description="Recompute existing scores too"),
    limit: int = Query(0, ge=0, description="Cap the number scored; 0 = no cap"),
):
    background_tasks.add_task(_rescore, rescore_all, limit or None)
    return {
        "status": "scoring_started",
        "message": "Scoring is running in the background.",
    }
