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

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/trumpmarket_test",
    ),
)
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
