"""Concurrent reservations and dispatches of the same vehicle/seller pair (spec 37.5, 37.10).

Each racer runs on its own pooled connection (``asyncio.gather``); the workspace control row is
the first lock of every inquiry path, so exactly one reservation and exactly one send intent
can ever exist for a pair, whatever the interleaving.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    World,
    add_vehicle,
    alias,
    dispatch,
    lease,
    qualify,
    reserve,
    reserve_and_queue,
    reserve_existing,
    scalar,
)

from suv_deals.domain.enums import InquiryState
from suv_deals.errors import AppError
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db


async def _count(db: Database, world: World, query: str) -> int:
    return int(await scalar(db, world, query, {}))


async def test_two_concurrent_reservations_of_one_pair_yield_one(db: Database, world: World) -> None:
    record, decision, snapshot = await qualify(db, world)
    results = await asyncio.gather(
        reserve_existing(db, world, record, decision, snapshot),
        reserve_existing(db, world, record, decision, snapshot),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1 and winners[0].state == InquiryState.RESERVED
    assert len(losers) == 1 and isinstance(losers[0], AppError), losers
    assert (
        await _count(
            db,
            world,
            "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s"
            " and released_at is null",
        )
        == 1
    )


async def test_concurrent_reservations_through_two_aliases_yield_one(
    db: Database, seed: Seed, world: World
) -> None:
    """Two workers qualify the same listing via the seller's two linked aliases at once."""
    ws = world.workspace_id
    vat = alias("vat_id", "DE111222333", None, "same_vat_id")
    vehicle = await add_vehicle(db, seed, ws, aliases=[vat])
    target = world.with_vehicle(vehicle)
    results = await asyncio.gather(reserve(db, target), reserve(db, target), return_exceptions=True)
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1, results
    assert all(isinstance(r, AppError) for r in results if isinstance(r, BaseException)), results
    assert (
        await _count(
            db,
            world,
            "select count(*) from app.seller_inquiries where workspace_id = %(ws)s and state = 'reserved'",
        )
        == 1
    )


async def test_two_concurrent_dispatches_of_one_inquiry_commit_one_intent(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    results = await asyncio.gather(
        dispatch(db, world, record.id, attempt_lease=lease(owner_name="worker-a")),
        dispatch(db, world, record.id, attempt_lease=lease(owner_name="worker-b")),
        return_exceptions=True,
    )
    outcomes = sorted(r.outcome if not isinstance(r, BaseException) else type(r).__name__ for r in results)
    assert outcomes.count("proceed") == 1, results
    assert (
        await _count(
            db,
            world,
            "select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s",
        )
        == 1
    )


async def test_same_seller_second_vehicle_waits_and_racing_dispatches_send_once(
    db: Database, seed: Seed, world: World
) -> None:
    """The same seller on a second vehicle waits for the cooldown; racing dispatches send once."""
    ws = world.workspace_id
    vat = alias("vat_id", "DE444555666", None, "same_vat_id")
    first_vehicle = await add_vehicle(db, seed, ws, aliases=[vat])
    first = await reserve_and_queue(db, world.with_vehicle(first_vehicle))
    second_vehicle = await add_vehicle(db, seed, ws, aliases=[vat])
    assert second_vehicle.seller_entity_id == first_vehicle.seller_entity_id
    # The seller cooldown already refuses a second reservation for the same seller.
    with pytest.raises(AppError):
        await reserve_and_queue(db, world.with_vehicle(second_vehicle))
    results = await asyncio.gather(
        dispatch(db, world, first.id, attempt_lease=lease(owner_name="worker-a")),
        dispatch(db, world, first.id, attempt_lease=lease(owner_name="worker-b")),
    )
    assert sorted(r.outcome for r in results) == ["hold", "proceed"]
