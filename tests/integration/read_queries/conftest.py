"""Fixtures for the read-query service tests (marker ``db``).

Arrangement uses the superuser test connection (``seed``) and the repositories; every query under
test runs as ``suv_backend`` through ``Database(db_url, set_role="suv_backend")``, so RLS and the
least-privilege grants apply exactly as in production. Each test seeds fresh SYNTHETIC workspaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from pydantic import SecretStr
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import CURSOR_SECRET, SeededWorkspace, seed_workspace

from suv_deals.persistence.database import Database
from suv_deals.settings import Settings


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
async def data(db: Database, seed: Seed) -> SeededWorkspace:
    return await seed_workspace(db, seed, "A")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        build_id="synthetic-build.1",
        mcp_cursor_signing_secret=SecretStr(CURSOR_SECRET.decode()),
        event_bridge_enabled=True,
        event_bridge_provider="mcp_events",
        allow_external_notifications=False,
    )
