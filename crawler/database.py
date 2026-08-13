import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from crawler.models import Base

# Load the project-level .env before reading any settings, so running the
# crawler, the seeders and uvicorn all pick up the same configuration.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pguser:pgpass123@localhost:5433/tender-intelligence",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create any missing tables, for local development convenience.

    Disabled by setting AUTO_CREATE_TABLES=false, which is what deployments
    should do: there, `alembic upgrade head` owns the schema. If create_all runs
    first on a fresh database it builds the tables without an alembic_version
    row, and the subsequent migration then fails trying to create them again.
    """
    if os.getenv("AUTO_CREATE_TABLES", "true").lower() in ("false", "0", "no"):
        return
    Base.metadata.create_all(bind=engine)


def get_session():
    """FastAPI dependency yielding a session that is always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db() -> Session:
    """Return a plain session for scripts and pipelines to manage themselves."""
    return SessionLocal()
