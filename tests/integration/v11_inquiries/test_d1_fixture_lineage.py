"""D1 item 5: fixture lineage is refused by the persistence layer at reservation and dispatch.

The runtime refuses fixture lineage before it reserves or dispatches, and the desktop worker's
claim refuses it as a final ``intent_invalid``. An API provider (``gmail_api``) has no claim step,
so the repository itself is the last guard: `inquiries_repo.reserve` and `inquiries_repo.dispatch`
raise the typed `FixtureLineageRefused` (``VALIDATION_ERROR``, ``details.reason =
fixture_lineage``) before anything changes. Synthetic; nothing is sent.
"""

from __future__ import annotations

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    World,
    add_vehicle,
    dispatch,
    qualify,
    reserve_and_queue,
    reserve_existing,
    scalar,
)

from suv_deals.domain.enums import InquiryState
from suv_deals.errors import ErrorCode
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db


def _set_fixture(seed: Seed, listing_id: object) -> None:
    """TEST ARRANGEMENT ONLY: lineage is frozen at ingest; this simulates a fixture-lineage
    listing reaching a later guard (defence in depth)."""
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute("update app.listings set is_fixture = true where id = %s", (listing_id,))


async def test_reserve_refuses_fixture_lineage(db: Database, seed: Seed, world: World) -> None:
    vehicle = await add_vehicle(db, seed, world.workspace_id, fixture=True)
    fixture_world = world.with_vehicle(vehicle)
    record, decision, snapshot = await qualify(db, fixture_world, vehicle)
    with pytest.raises(inquiries_repo.FixtureLineageRefused) as caught:
        await reserve_existing(db, fixture_world, record, decision, snapshot)
    assert caught.value.code == ErrorCode.VALIDATION_ERROR
    assert caught.value.details == {"reason": "fixture_lineage", "problems": ["FIXTURE_LINEAGE"]}
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": record.id}
    )
    assert state != InquiryState.RESERVED.value
    debits = await scalar(
        db,
        world,
        "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s and inquiry_id = %(id)s",
        {"id": record.id},
    )
    assert debits == 0


async def test_dispatch_refuses_fixture_lineage_before_any_attempt(
    db: Database, seed: Seed, world: World
) -> None:
    record = await reserve_and_queue(db, world)
    _set_fixture(seed, record.qualification_listing_id)
    with pytest.raises(inquiries_repo.FixtureLineageRefused):
        await dispatch(db, world, record.id)
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": record.id}
    )
    assert state == InquiryState.QUEUED.value
    attempts = await scalar(
        db,
        world,
        "select count(*) from ops.email_delivery_attempts"
        " where workspace_id = %(ws)s and inquiry_id = %(id)s",
        {"id": record.id},
    )
    assert attempts == 0


async def test_real_lineage_still_reserves_and_dispatches(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.outcome == "proceed"
