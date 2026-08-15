import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Load the project-level .env before reading any settings, so the crawler and
# the scoring pass both pick up the same configuration.
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


def check_connection():
    """Fail fast with a clear message if the database is unreachable.

    This service does **not** own the schema — the tender-intelligence app does,
    via its Drizzle migrations. There is deliberately no ``create_all`` here: two
    tools creating the same tables is how you end up with a database that neither
    of them can migrate. If a table is missing, run ``npm run db:migrate`` there.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def get_db() -> Session:
    """Return a plain session for scripts and pipelines to manage themselves."""
    return SessionLocal()
