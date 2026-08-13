from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, desc
from sqlalchemy.orm import Session

from api.schemas import SavedSearchCreate, SavedSearchOut
from crawler.database import get_session
from crawler.models import SavedSearch

router = APIRouter()


@router.get("/saved", response_model=List[SavedSearchOut])
def list_saved_searches(db: Session = Depends(get_session)):
    return db.query(SavedSearch).order_by(desc(SavedSearch.created_at)).all()


@router.post("/saved", response_model=SavedSearchOut, status_code=201)
def create_saved_search(payload: SavedSearchCreate, db: Session = Depends(get_session)):
    search = SavedSearch(name=payload.name, filters=payload.filters)
    db.add(search)
    db.commit()
    db.refresh(search)
    return search


@router.delete("/saved/{search_id}", status_code=204)
def delete_saved_search(search_id: str, db: Session = Depends(get_session)):
    search = (
        db.query(SavedSearch)
        .filter(cast(SavedSearch.id, String) == search_id)
        .one_or_none()
    )
    if search is None:
        raise HTTPException(status_code=404, detail="Saved search not found")
    db.delete(search)
    db.commit()
