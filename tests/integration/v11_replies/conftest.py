"""Fixtures for the mailbox-worker and seller-reply persistence tests (marker ``db``).

Arrangement uses the superuser test connection (``seed``); every operation under test runs
through ``Database(db_url, set_role="suv_backend")`` so RLS and the least-privilege grants apply
exactly as in production. Each test builds fresh synthetic workspaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld, inquiry_world

from suv_deals.persistence.database import Database


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
def iw(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "B1b replies A")


@pytest.fixture
def iw_b(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "B1b replies B")


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=8, lock_timeout_ms=5_000)
    await database.open()
    try:
        yield database
    finally:
        await database.close()
