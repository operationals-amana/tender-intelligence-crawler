"""Seed the initial application user.

Credentials come from the environment so a real password never lands in the
repository:

    SEED_USER_EMAIL     defaults to operationals@amana.id
    SEED_USER_PASSWORD  defaults to a generated one, printed once on creation
    SEED_USER_NAME      defaults to "AMANA Operations"

Re-running is safe: an existing user is left untouched, never re-hashed with a
new password.
"""
import os
import secrets

from api.auth import hash_password
from crawler.models import User

DEFAULT_EMAIL = "operationals@amana.id"
DEFAULT_NAME = "AMANA Operations"


def seed_users(db):
    email = (os.getenv("SEED_USER_EMAIL") or DEFAULT_EMAIL).strip().lower()

    existing = db.query(User).filter(User.email == email).one_or_none()
    if existing is not None:
        print(f"  User {email} already exists, leaving password unchanged")
        return

    password = os.getenv("SEED_USER_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)

    db.add(
        User(
            email=email,
            full_name=os.getenv("SEED_USER_NAME") or DEFAULT_NAME,
            hashed_password=hash_password(password),
            role="admin",
            is_active=True,
        )
    )
    db.flush()

    print(f"  Created user {email}")
    if generated:
        print(f"  Generated password (shown once, store it now): {password}")
    else:
        print("  Password taken from SEED_USER_PASSWORD")
