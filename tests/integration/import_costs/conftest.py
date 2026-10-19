"""Fixtures for the import-cost calculator service tests (marker ``db``).

Arrangement uses the superuser test connection (``seed``) and the repositories; the service under
test runs as ``suv_backend`` through ``Database(db_url, set_role="suv_backend")`` so RLS and the
least-privilege grants apply exactly as in production. Each test seeds fresh SYNTHETIC workspaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed

from suv_deals.persistence.database import Database


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=8, lock_timeout_ms=5_000)
    await database.open()
    try:
        yield database
    finally:
        await database.close()
