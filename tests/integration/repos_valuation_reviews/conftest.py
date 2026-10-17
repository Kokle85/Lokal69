"""Fixtures for the market/valuation/review repository tests (marker ``db``).

Arrangement uses the superuser test connection (``seed``); every operation under test runs as
``suv_backend`` through ``Database(db_url, set_role="suv_backend")`` so RLS and least-privilege
grants apply exactly as in production. Each test creates fresh synthetic workspaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import RealWorld, real_world

from suv_deals.persistence.database import Database


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
def world(seed: Seed) -> RealWorld:
    return real_world(seed, "Repos WP7b2 A")


@pytest.fixture
def other_world(seed: Seed) -> RealWorld:
    return real_world(seed, "Repos WP7b2 B")


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=8, lock_timeout_ms=5_000)
    await database.open()
    try:
        yield database
    finally:
        await database.close()
