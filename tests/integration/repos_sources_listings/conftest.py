"""Fixtures for the WP7b1 repository tests (marker ``db``).

Arrangement that the repositories cannot do themselves (users, raw tampering, time travel) uses
the superuser ``seed`` connection; every operation under test runs as ``suv_backend`` through
``Database(db_url, set_role="suv_backend")`` so RLS and least-privilege grants apply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_sources_listings.support import Env, build_env

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


@pytest.fixture
async def env(db: Database, seed: Seed) -> Env:
    return await build_env(db, seed, "Repos A")


@pytest.fixture
async def env_b(db: Database, seed: Seed) -> Env:
    return await build_env(db, seed, "Repos B")
