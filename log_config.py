"""Shared logging configuration for the Funkle receptionist.

Owns ``LOG_DIR`` and ``configure_logging()`` so writers (``server.py``) and
readers (``calls.py``) always agree on where files live.

Env:
  LOG_DIR     Directory for server.log / transcripts.log.
              Default: ``<repo>/logs``. On Fly, set to ``/data/logs`` so
              files survive redeploys on the persistent volume.
  LOG_LEVEL   Minimum level for non-transcript sinks (stderr + server.log).
              Default: ``DEBUG`` locally; Fly sets ``INFO``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent

LOG_DIR = Path(os.getenv("LOG_DIR", str(ROOT / "logs"))).expanduser()


def _is_transcript(record) -> bool:
    return record["extra"].get("transcript") is True


class InterceptHandler(logging.Handler):
    """Forward stdlib logging (uvicorn, pipecat, etc.) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging() -> Path:
    """Install loguru sinks + stdlib interception. Idempotent enough for reload.

    Returns the resolved ``LOG_DIR`` (created if missing).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = os.getenv("LOG_LEVEL", "DEBUG").upper()

    logger.remove()

    # Console: everything except transcript-tagged lines (those get a
    # cleaner second sink below).
    logger.add(
        sys.stderr,
        filter=lambda r: not _is_transcript(r),
        level=level,
    )
    logger.add(
        sys.stderr,
        filter=_is_transcript,
        format="<green>{time:HH:mm:ss}</green> <cyan>[{extra[session]}]</cyan> "
               "<level>{extra[role]:>9}</level>: {message}",
        level="INFO",
    )

    # Full server log on disk. Size rotation + time retention keeps the
    # Fly volume from filling up.
    logger.add(
        LOG_DIR / "server.log",
        rotation="10 MB",
        retention="14 days",
        enqueue=True,
        level=level,
    )
    # Conversation turns only — format is a contract with calls.py.
    logger.add(
        LOG_DIR / "transcripts.log",
        filter=_is_transcript,
        format="{time:YYYY-MM-DD HH:mm:ss} [{extra[session]}] {extra[role]:>9}: {message}",
        rotation="5 MB",
        retention="30 days",
        enqueue=True,
        level="INFO",
    )

    # Capture uvicorn / library stdlib loggers into the same sinks.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False

    logger.info(f"Logging to {LOG_DIR} (level={level})")
    return LOG_DIR
