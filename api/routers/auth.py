from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.auth import ACCESS_TOKEN_TTL_HOURS, authenticate, create_access_token, get_current_user
from api.schemas import LoginRequest, TokenResponse, UserOut
from crawler.database import get_session
from crawler.models import User

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_session)):
    user = authenticate(db, payload.email, payload.password)
    if user is None:
        # One message for both "no such user" and "wrong password" — telling
        # them apart would let anyone enumerate valid accounts.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user),
        expires_in=ACCESS_TOKEN_TTL_HOURS * 3600,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
