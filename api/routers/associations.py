from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.schemas import AssociationOut
from crawler.database import get_session
from crawler.models import Association

router = APIRouter()


@router.get("", response_model=List[AssociationOut])
def list_associations(
    category: Optional[str] = Query(None),
    sub_cluster: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=1000),
    db: Session = Depends(get_session),
):
    query = db.query(Association)
    if category:
        query = query.filter(Association.category == category)
    if sub_cluster:
        query = query.filter(Association.sub_cluster == sub_cluster)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            Association.name.ilike(pattern) | Association.abbreviation.ilike(pattern)
        )
    return query.order_by(Association.name).limit(limit).all()


@router.get("/categories")
def list_categories(db: Session = Depends(get_session)):
    """Category counts, for the directory's filter chips."""
    rows = (
        db.query(Association.category, func.count(Association.id))
        .group_by(Association.category)
        .order_by(func.count(Association.id).desc())
        .all()
    )
    return [{"category": c, "count": n} for c, n in rows]
