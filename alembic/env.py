"""Alembic migration environment.

Wired to the same SQLAlchemy engine and metadata the running app uses so
autogenerate diffs against the live schema. Connection string comes from
db.py — never from alembic.ini — so DATABASE_URL / SALON_DB_PATH env
vars are the single source of truth in every environment.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Add the project root to sys.path so `import db, models` works when
# alembic is invoked as `alembic upgrade head` from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import db  # noqa: E402  (must come after sys.path munging)
import models  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLite + batch mode is required for any ALTER TABLE (SQLite can't
# ALTER columns in place; alembic emulates it via a rebuild-and-rename
# dance when render_as_batch=True).
_RENDER_BATCH = db.DATABASE_URL.startswith("sqlite")

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_RENDER_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with db.get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_RENDER_BATCH,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
