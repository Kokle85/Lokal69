"""Fixtures for the spec v1.1 runtime-worker tests (marker ``db``; work package B2a).

Each test gets a fresh SYNTHETIC workspace and its own runtime (`build_runtime` with
``DATABASE_SET_ROLE=suv_backend``, the in-memory live-mode dealer pages, the synthetic taxonomy).
No test reaches a network: Gmail and Slack are ``httpx.MockTransport`` doubles.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import PipelineEnv
from tests.integration.v11_runtime.support import build_live_env


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
async def env(db_url: str, seed: Seed, tmp_path: Path) -> AsyncIterator[PipelineEnv]:
    built = await build_live_env(db_url, seed, tmp_path)
    try:
        yield built
    finally:
        await built.close()
