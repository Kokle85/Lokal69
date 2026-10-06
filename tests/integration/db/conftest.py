"""Fixtures for database integration tests (marker ``db``).

The session-scoped ``db_url`` fixture (tests/conftest.py) builds ONE migrated
database per test session; every test isolates itself by creating fresh
synthetic workspaces rather than truncating shared tables.
"""

from __future__ import annotations

import psycopg
import pytest
from tests.integration.db.helpers import Seed, World, build_world


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
def world_a(seed: Seed) -> World:
    return build_world(seed, "Workspace A")


@pytest.fixture
def world_b(seed: Seed) -> World:
    return build_world(seed, "Workspace B")
