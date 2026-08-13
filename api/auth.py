"""Authentication: password hashing, JWT issue/verify, and FastAPI dependencies.

Login exchanges an email and password for a signed JWT. The frontend stores the
token and sends it as a bearer header. Passwords are only ever persisted as
bcrypt hashes.

Set ``JWT_SECRET`` in the environment for any deployment — the development
default is a fixed string, which is fine locally and unsafe anywhere else.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import String, cast
from sqlalchemy.orm import Session

from crawler.database import get_session
from crawler.models import User

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-insecure-secret-change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL_HOURS = int(os.getenv("ACCESS_TOKEN_TTL_HOURS", "12"))

# tokenUrl is only used to render the interactive docs' auth box.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# bcrypt hashes at most 72 bytes and rejects anything longer outright. We call
# the library directly rather than through passlib, whose 1.7.4 release predates
# bcrypt 4/5 and misreports the length of even short passwords against them.
BCRYPT_MAX_BYTES = 72


def _password_bytes(password: str) -> bytes:
    """Encode a password for bcrypt, truncated to the algorithm's limit."""
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the database — a failed login, not a 500.
        return False


def create_access_token(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_TTL_HOURS)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role or "member",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def authenticate(db: Session, email: str, password: str) -> Optional[User]:
    user = (
        db.query(User).filter(User.email == email.strip().lower()).one_or_none()
    )
    if user is None or not user.is_active:
        # Still run a hash comparison so a missing account and a wrong password
        # take about the same time, rather than leaking which emails exist.
        verify_password(password, hash_password("dummy"))
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_session),
) -> User:
    """Resolve the bearer token to a user, or raise 401."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise unauthorized

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise unauthorized

    user_id = payload.get("sub")
    if not user_id:
        raise unauthorized

    user = db.query(User).filter(cast(User.id, String) == user_id).one_or_none()
    if user is None or not user.is_active:
        raise unauthorized
    return user


def get_optional_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_session),
) -> Optional[User]:
    """Like get_current_user but returns None instead of raising."""
    if not token:
        return None
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        return None
