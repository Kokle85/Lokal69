"""Fixtures for the WP9 runtime pipeline tests (marker ``db``).

Each test gets a fresh SYNTHETIC workspace and its own runtime (`build_runtime` with
``DATABASE_SET_ROLE=suv_backend``, the offline fixture crawl client and the synthetic taxonomy).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import PipelineEnv, build_env


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
async def env(db_url: str, seed: Seed) -> AsyncIterator[PipelineEnv]:
    built = await build_env(db_url, seed)
    try:
        yield built
    finally:
        await built.close()
