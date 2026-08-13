from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, asc, cast, desc, func, or_
from sqlalchemy.orm import Session, joinedload

from api.schemas import (
    FilterOptionsOut,
    TenderDetailOut,
    TenderListOut,
    TenderNoteCreate,
    TenderNoteOut,
    TenderScoreOut,
)
from crawler.database import get_session
from crawler.models import Tender, TenderNote, TenderScore

router = APIRouter()

# Only these columns may be sorted on, so the sort_by parameter cannot be used
# to order by arbitrary attributes.
SORTABLE = {
    "submission_deadline": Tender.submission_deadline,
    "noticedate": Tender.noticedate,
    "project_country": Tender.project_country,
    "notice_type": Tender.notice_type,
    "first_seen_at": Tender.first_seen_at,
}


@router.get("", response_model=TenderListOut)
def list_tenders(
    country: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    notice_type: Optional[str] = Query(None),
    procurement_group: Optional[str] = Query(None),
    status: Optional[str] = Query("Published"),
    deadline_within_days: Optional[int] = Query(None, ge=0, le=3650),
    active_only: bool = Query(False, description="Only notices whose deadline is still open"),
    min_score: Optional[float] = Query(None, ge=0, le=100),
    search: Optional[str] = Query(None),
    sort_by: str = Query("submission_deadline"),
    sort_order: str = Query("asc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    query = db.query(Tender)

    if country:
        query = query.filter(Tender.project_country.ilike(f"%{country}%"))
    if sector:
        query = query.filter(Tender.sector.ilike(f"%{sector}%"))
    if notice_type:
        query = query.filter(Tender.notice_type == notice_type)
    if procurement_group:
        query = query.filter(Tender.procurement_group == procurement_group)
    if status:
        query = query.filter(Tender.notice_status == status)

    if active_only:
        query = query.filter(Tender.submission_deadline >= func.now())
    if deadline_within_days is not None:
        query = query.filter(
            Tender.submission_deadline >= func.now(),
            Tender.submission_deadline
            <= func.now() + func.make_interval(0, 0, 0, deadline_within_days),
        )

    if min_score is not None:
        query = query.join(TenderScore).filter(TenderScore.overall_score >= min_score)

    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                Tender.bid_description.ilike(pattern),
                Tender.project_name.ilike(pattern),
                Tender.notice_id.ilike(pattern),
                Tender.contact_organization.ilike(pattern),
                # Search the stripped text rather than the raw HTML body.
                Tender.notice_text_clean.ilike(pattern),
            )
        )

    total = query.count()

    column = SORTABLE.get(sort_by, Tender.submission_deadline)
    direction = desc if sort_order.lower() == "desc" else asc
    # Notices without a deadline should not crowd out the actionable ones.
    query = query.order_by(direction(column).nullslast(), Tender.notice_id)

    items = query.offset(offset).limit(limit).all()

    return TenderListOut(total=total, limit=limit, offset=offset, items=items)


@router.get("/filters", response_model=FilterOptionsOut)
def get_filter_options(db: Session = Depends(get_session)):
    """Distinct values for populating the filter controls."""

    def distinct(column, limit=300):
        rows = (
            db.query(column)
            .filter(column.isnot(None), column != "")
            .distinct()
            .order_by(column)
            .limit(limit)
            .all()
        )
        return [row[0] for row in rows]

    # sector holds a comma-separated list, so split it back into labels.
    sectors = set()
    for value in distinct(Tender.sector, limit=500):
        for part in value.split(","):
            part = part.strip()
            if part:
                sectors.add(part)

    return FilterOptionsOut(
        countries=distinct(Tender.project_country),
        notice_types=distinct(Tender.notice_type),
        sectors=sorted(sectors),
        procurement_groups=distinct(Tender.procurement_group),
    )


@router.get("/{tender_id}", response_model=TenderDetailOut)
def get_tender(tender_id: str, db: Session = Depends(get_session)):
    tender = (
        db.query(Tender)
        .options(joinedload(Tender.score), joinedload(Tender.notes))
        .filter(cast(Tender.id, String) == tender_id)
        .one_or_none()
    )
    if tender is None:
        raise HTTPException(status_code=404, detail="Tender not found")

    detail = TenderDetailOut.model_validate(tender)
    detail.score = TenderScoreOut.model_validate(tender.score) if tender.score else None
    detail.notes = [TenderNoteOut.model_validate(n) for n in tender.notes]
    return detail


@router.get("/{tender_id}/score", response_model=TenderScoreOut)
def get_tender_score(
    tender_id: str,
    compute: bool = Query(False, description="Score on demand if not already scored"),
    db: Session = Depends(get_session),
):
    tender = db.query(Tender).filter(cast(Tender.id, String) == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail="Tender not found")

    if tender.score is not None:
        return tender.score

    if not compute:
        raise HTTPException(
            status_code=404,
            detail="This tender has not been scored yet. Retry with ?compute=true.",
        )

    # Building the engine loads the whole capability corpus, so this on-demand
    # path is for single lookups; bulk scoring goes through score_tenders.py.
    from api.scoring.runner import score_single

    return score_single(db, tender)


@router.get("/{tender_id}/notes", response_model=List[TenderNoteOut])
def list_tender_notes(tender_id: str, db: Session = Depends(get_session)):
    return (
        db.query(TenderNote)
        .filter(cast(TenderNote.tender_id, String) == tender_id)
        .order_by(desc(TenderNote.created_at))
        .all()
    )


@router.post("/{tender_id}/notes", response_model=TenderNoteOut, status_code=201)
def add_tender_note(
    tender_id: str, payload: TenderNoteCreate, db: Session = Depends(get_session)
):
    tender = db.query(Tender).filter(cast(Tender.id, String) == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail="Tender not found")

    note = TenderNote(
        tender_id=tender.id, note=payload.note, created_by=payload.created_by
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note
