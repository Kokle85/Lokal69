"""Follow-ups of the reply ingest work package (database-backed part).

The candidate read takes its fixture flag from the listing's frozen lineage
(``app.listings.is_fixture``, migration 20261007000200), not from the source's CURRENT mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.read_queries.dataset import make_candidate_listing
from tests.integration.v11_replies.support import owner

from suv_deals.persistence.database import Database
from suv_deals.persistence.queries import candidates
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.views.common import WarningCode

pytestmark = pytest.mark.db


async def _summary_fixture_flag(db: Database, workspace_id: UUID, listing_id: UUID) -> tuple[bool, set[str]]:
    actor = owner(workspace_id)
    async with unit_of_work(db, actor) as conn:
        result = await candidates.get_candidate(conn, actor, listing_id)
    return result.data.summary.is_fixture, {w.code for w in result.warnings}


async def test_a_fixture_listing_stays_synthetic_after_its_source_goes_live(db: Database, seed: Seed) -> None:
    ws = seed.workspace("B1b fixture lineage")
    seed.profile(ws, "primary")
    key = unique("lineage_src").lower()
    source = seed.source(ws, source_key=key)  # fixture mode
    listing, _ = make_candidate_listing(seed, ws, source, key, created_at=None, now=datetime.now(UTC))
    assert seed.scalar("select is_fixture from app.listings where id = %s", (listing,)) is True
    seed.conn.execute("update app.sources set mode = 'official_api' where id = %s", (source,))
    flag, warnings = await _summary_fixture_flag(db, ws, listing)
    assert flag is True and WarningCode.FIXTURE_DATA in warnings


async def test_a_live_listing_is_not_a_fixture_because_its_source_switched_to_fixtures(
    db: Database, seed: Seed
) -> None:
    ws = seed.workspace("B1b live lineage")
    seed.profile(ws, "primary")
    key = unique("live_src").lower()
    source = seed.source(ws, source_key=key, mode="official_api")
    listing, _ = make_candidate_listing(seed, ws, source, key, created_at=None, now=datetime.now(UTC))
    assert seed.scalar("select is_fixture from app.listings where id = %s", (listing,)) is False
    seed.conn.execute("update app.sources set mode = 'fixture' where id = %s", (source,))
    flag, _warnings = await _summary_fixture_flag(db, ws, listing)
    assert flag is False
