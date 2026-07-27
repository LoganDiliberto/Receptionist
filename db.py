"""Database engine and session management for the salon store.

The whole app uses sync SQLAlchemy on a single SQLite file. Async callers
(the two bot tools in ``salon.py``) wrap each session in
``asyncio.to_thread`` — same pattern the openpyxl code used to use.

Connection string resolution, in order:
  1. ``DATABASE_URL`` env var, if set (SQLAlchemy convention).
  2. ``SALON_DB_PATH`` env var, if set — treated as a filesystem path.
  3. ``receptionist.db`` next to this file.

In production ``fly.toml`` sets ``SALON_DB_PATH=/data/receptionist.db``
so the database lives on the persistent volume alongside the (soon-to-be
legacy) salon workbook.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_DB = Path(__file__).parent / "receptionist.db"


def _resolve_database_url() -> str:
    if url := os.getenv("DATABASE_URL"):
        return url
    path = Path(os.getenv("SALON_DB_PATH", str(_DEFAULT_DB)))
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLAlchemy needs three slashes for a relative path, four for absolute.
    if path.is_absolute():
        return f"sqlite:///{path}"
    return f"sqlite:///{path}"


DATABASE_URL = _resolve_database_url()

# SQLite specifics:
# - check_same_thread=False lets the pooled connections cross threads (we
#   dispatch sync sessions from `asyncio.to_thread`, so different threads
#   will hit the pool).
# - StaticPool would serialize all access to one connection, which is fine
#   for our load but breaks WAL. Default pool + WAL PRAGMA is a better fit.
engine: Engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    """Turn on foreign keys (off by default in SQLite) and WAL mode.

    Foreign keys are essential — every JOIN in salon.py assumes referential
    integrity. WAL mode lets readers not block writers, which matters when
    the voice bot is booking during an admin edit.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session and commit or roll back on exit.

    All salon-side DB writes go through this so partial failures don't
    leak dirty data. Reads use the same context for consistency.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_engine() -> Engine:
    """Accessor for alembic's env.py — avoids importing the module-level global."""
    return engine


logger.info(f"Database: {DATABASE_URL}")
