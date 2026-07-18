"""
Database connection and session management.

This module answers one question: "how do I talk to the database?"

Usage from anywhere in the codebase:

    from njlead.db.session import get_session, init_db

    # Create all tables (run once at startup via `njlead init`)
    init_db(db_path)

    # Open a session to read/write rows
    with get_session() as session:
        session.add(some_row)
        session.commit()
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from njlead.db.models import Base

# The engine is the low-level connection to the SQLite file.
# It is created once and reused for the life of the process.
_engine = None
_SessionFactory = None


def _get_db_path() -> Path:
    """
    Return the path to leads.db.

    Checks the NJLEAD_DB environment variable first, then falls back
    to 'leads.db' in whatever directory you ran the command from.
    """
    env_path = os.environ.get("NJLEAD_DB")
    if env_path:
        return Path(env_path)
    return Path.cwd() / "leads.db"


def init_db(db_path: Path | None = None) -> Path:
    """
    Create the database file and all tables.

    Call this once (via `njlead init`) before ingesting any PDFs.
    It is safe to call again — SQLAlchemy won't overwrite existing tables.

    Returns the path to the database file.
    """
    global _engine, _SessionFactory

    if db_path is None:
        db_path = _get_db_path()

    # SQLite connection string format: sqlite:///absolute/path/to/file.db
    connection_url = f"sqlite:///{db_path.resolve()}"
    _engine = create_engine(connection_url, echo=False)

    # Create every table defined in models.py (skips tables that already exist)
    Base.metadata.create_all(_engine)

    # Idempotent light-touch migration: create_all() won't add new columns to
    # tables that already exist, so if this is an older leads.db from before
    # the LLM engine was wired in, the `schools` table won't have `lab_name`
    # yet. Rather than force a full rebuild, we add the column in place.
    # Checked with SQLite's PRAGMA table_info so re-running is a no-op.
    _ensure_school_lab_name_column(_engine)

    # Build the session factory so get_session() works
    _SessionFactory = sessionmaker(bind=_engine)

    return db_path


def _ensure_school_lab_name_column(engine) -> None:
    """
    Add `schools.lab_name` if the column is missing.

    SQLite doesn't complain if the column already exists — but ADD COLUMN
    itself will fail on a duplicate, so we look at PRAGMA table_info first
    and only issue the ALTER when needed.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(schools)")).fetchall()
        # PRAGMA returns (cid, name, type, notnull, dflt_value, pk); we want name
        existing_columns = {r[1] for r in rows}
        if "lab_name" not in existing_columns:
            conn.execute(text("ALTER TABLE schools ADD COLUMN lab_name VARCHAR(128)"))
            conn.commit()


def _ensure_connected() -> None:
    """
    Make sure init_db() has been called before trying to use the database.
    Raises a clear error if not, rather than a confusing SQLAlchemy crash.
    """
    if _engine is None or _SessionFactory is None:
        db_path = _get_db_path()
        if not db_path.exists():
            raise RuntimeError(
                f"Database not found at {db_path}.\n"
                "Run 'njlead init' first to create it."
            )
        # Database file exists but engine wasn't initialized — connect now
        init_db(db_path)


def get_session() -> Session:
    """
    Return a new database session.

    Use this as a context manager so the session is always closed cleanly:

        with get_session() as session:
            session.add(row)
            session.commit()

    If something goes wrong inside the 'with' block, the session is
    automatically rolled back (changes are discarded).
    """
    _ensure_connected()
    return _SessionFactory()
