"""Database session management and the Postgres advisory-lock helper.

The worker uses advisory locks rather than a Redis lock so that running two
worker processes (or a worker and a manual pipeline run from the API) can never
process the same batch twice.
"""

from __future__ import annotations

import contextlib
import zlib
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker code."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _lock_key(name: str) -> int:
    """Stable 63-bit key derived from a human-readable lock name."""
    return zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF


@contextlib.contextmanager
def advisory_lock(db: Session, name: str) -> Iterator[bool]:
    """Try to take a session-level advisory lock; yields whether it was acquired.

    Non-blocking on purpose: if another worker already holds the lock the caller
    should skip this tick rather than queue up behind it.
    """
    key = _lock_key(name)
    acquired = bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
    try:
        yield acquired
    finally:
        if acquired:
            db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            db.commit()
