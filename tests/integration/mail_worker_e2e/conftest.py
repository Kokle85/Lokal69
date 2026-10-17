"""Fixtures for the mail-worker end-to-end tests (marker ``db``; spec 37.6-37.8).

The REAL Windows desktop worker code (``desktop/outlook-bridge``: ``BridgeWorker``,
``BridgeApiClient``, ``LocalStore``, ``SendIntentProcessor``, local matching) runs against the
REAL backend app (``api.app.create_app``) and a real PostgreSQL (``Database(set_role=
"suv_backend")``). Only Outlook is faked (``outlook_bridge.testing.FakeOutlook``: no COM, no
mailbox, nothing is ever sent) and the HTTP hop is in-process (`harness.LoopTransport`).

Everything is SYNTHETIC (``*.example`` / ``*.invalid`` addresses, fixture sources).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

DESKTOP = Path(__file__).resolve().parents[3] / "desktop" / "outlook-bridge"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))  # the desktop package ``outlook_bridge`` (not installed)

from tests.integration.db.helpers import Seed  # noqa: E402

from suv_deals.persistence.database import Database  # noqa: E402


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
