"""Fixtures for the bounded-inquiry persistence tests (marker ``db``).

Arrangement uses the superuser test connection (``seed``); every operation under test runs
through ``Database(db_url, set_role="suv_backend")`` so RLS and the least-privilege grants apply
exactly as in production. Each test builds fresh synthetic workspaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import World, build_world

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
async def world(db: Database, seed: Seed) -> World:
    return await build_world(db, seed, "B1a inquiries A")


@pytest.fixture
async def world_b(db: Database, seed: Seed) -> World:
    return await build_world(db, seed, "B1a inquiries B")
