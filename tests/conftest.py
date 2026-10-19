"""Shared pytest fixtures.

- `frozen_now`: deterministic UTC instant for unit tests.
- `db_url`: session-scoped, freshly migrated PostgreSQL database (marker `db`).
- `db_conn`: per-test autocommit connection as the superuser test role.
Tests marked `db` are skipped when no PostgreSQL is reachable, and the skip is
reported; they are never silently counted as passed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from tests.db_harness import create_migrated_database, db_available, drop_database

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "adapters" / "fixtures"


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    if not db_available():
        pytest.skip("PostgreSQL not reachable via TEST_DATABASE_ADMIN_URL")
    dbname, url = create_migrated_database()
    try:
        yield url
    finally:
        drop_database(dbname)


@pytest.fixture
def db_conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        yield conn
