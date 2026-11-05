import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

# Load the project-level .env before reading any settings, so the crawler and
# the scoring pass both pick up the same configuration.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pguser:pgpass123@localhost:5433/amana-workspace",
)

# Postgres schema holding the tender tables. In the shared AMANA Workspace
# database that is "tender" (owned by the workspace app's Drizzle migrations).
# Empty = server default (a legacy single-app database with tables in public).
DB_SCHEMA = os.getenv("DB_SCHEMA", "").strip()
if DB_SCHEMA and not re.fullmatch(r"[a-z_][a-z0-9_]*", DB_SCHEMA):
    raise ValueError(f"DB_SCHEMA must be a plain lower-case identifier, got {DB_SCHEMA!r}")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
)

if DB_SCHEMA:
    # ORM statements get schema-qualified table names (works through any pooler,
    # including Supabase's transaction pooler) ...
    engine = engine.execution_options(schema_translate_map={None: DB_SCHEMA})

    # ... and the search_path covers any hand-written SQL on top.
    @event.listens_for(engine, "connect", insert=True)
    def _set_search_path(dbapi_connection, _record):  # pragma: no cover - driver hook
        autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET SESSION search_path TO "{DB_SCHEMA}", public')
        cursor.close()
        dbapi_connection.autocommit = autocommit
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def check_connection():
    """Fail fast with a clear message if the database is unreachable.

    This service does **not** own the schema — the tender-intelligence app does,
    via its Drizzle migrations. There is deliberately no ``create_all`` here: two
    tools creating the same tables is how you end up with a database that neither
    of them can migrate. If a table is missing, run ``npm run db:migrate`` in amana-workspace.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def get_db() -> Session:
    """Return a plain session for scripts and pipelines to manage themselves."""
    return SessionLocal()
