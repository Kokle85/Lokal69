"""Fixtures for the spec v1.1 section 37 database tests (marker ``db``).

The session-scoped ``db_url`` (tests/conftest.py) is one freshly migrated database; every test
builds its own synthetic workspaces, so tests never interfere with each other.
"""

from __future__ import annotations

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld, inquiry_world


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
def iw(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "V11 workspace A")


@pytest.fixture
def iw_b(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "V11 workspace B")
