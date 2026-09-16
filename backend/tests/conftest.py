"""Test fixtures.

The environment is configured *before* `app.config` is imported, because the
settings object is a module-level singleton. Each test gets a clean database via
TRUNCATE rather than a schema rebuild -- it is an order of magnitude faster and
keeps the real constraints (which is the point, since idempotency here is
enforced by unique constraints).
"""

from __future__ import annotations

import datetime as dt
import os

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/trumpmarket_test"
)

# The suite drops and recreates every table, so it must never be able to point
# at a real database. DATABASE_URL is *overridden*, not defaulted: with
# `setdefault`, running `DATABASE_URL=... pytest` would wipe that database.
_test_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
_database_name = _test_url.rsplit("/", 1)[-1].split("?")[0]
if "test" not in _database_name.lower():
    raise RuntimeError(
        f"refusing to run the test suite against database {_database_name!r}: "
        "the suite drops every table, so its name must contain 'test'. "
        "Set TEST_DATABASE_URL to a throwaway database."
    )
os.environ["DATABASE_URL"] = _test_url
os.environ.setdefault("APP_AUTH_TOKEN", "test-token")
os.environ.setdefault("ENABLED_SOURCES", "mock")
os.environ.setdefault("MARKET_DATA_PROVIDER", "mock")
os.environ.setdefault("LLM_FAKE_MODE", "false")
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Base, User  # noqa: E402
from app.seed import loader  # noqa: E402

UTC = dt.timezone.utc


@pytest.fixture(scope="session", autouse=True)
def _schema():
    # pgvector is a hard requirement from Phase 2 on: event_embeddings uses the
    # `vector` type. Fail here with a clear message rather than deep in a query.
    with engine.begin() as connection:
        try:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:  # pragma: no cover - environment problem
            pytest.exit(
                "pgvector is required. Install postgresql-<version>-pgvector, or "
                f"use the pgvector/pgvector image. ({exc})",
                returncode=1,
            )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(_schema):
    session = SessionLocal()
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def seeded(db):
    """Reference data (tickers, aliases) plus the default user and watchlist."""
    loader.seed_reference_data(db)
    loader.seed_user(db)
    return db.query(User).first()


@pytest.fixture
def user(db):
    row = User(display_name="Test")
    db.add(row)
    db.commit()
    return row
