"""Seller identity across sites and merges (spec 37.3, 37.5, 37.8, 37.10).

- the aliases of one dealer on three sites resolve to ONE persisted seller entity before any
  reservation, so the same vehicle advertised on three sites gets exactly ONE inquiry;
- entities linked separately are merged as soon as a shared alias (VAT id) appears, and the
  merge reconciles the reservations in the same transaction;
- an identity merge while two inquiries are queued can never produce a second send.
"""

from __future__ import annotations

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    World,
    accepted,
    add_vehicle,
    alias,
    confirm_cluster,
    dispatch,
    inquiry,
    link,
    qualify,
    readiness,
    record_contact,
    report,
    reserve,
    reserve_and_queue,
    scalar,
    system,
)

from suv_deals.domain.enums import InquiryReadiness, InquiryState
from suv_deals.errors import VersionConflict
from suv_deals.persistence import sellers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _reservations(db: Database, world: World) -> int:
    return int(
        await scalar(
            db,
            world,
            "select count(*) as n from ops.inquiry_quota_ledger where workspace_id = %(ws)s"
            " and released_at is null",
            {},
        )
    )


async def test_three_cross_site_aliases_are_one_seller_and_one_inquiry(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    vat = alias("vat_id", "DE123456789", None, "same_vat_id")
    vehicles = []
    for _ in range(3):
        # Each site shows its own marketplace seller id plus the dealer's VAT id.
        vehicles.append(await add_vehicle(db, seed, ws, aliases=[vat]))
    entity = vehicles[0].seller_entity_id
    assert {v.seller_entity_id for v in vehicles} == {entity}
    async with unit_of_work(db, system(ws)) as conn:
        aliases = await sellers_repo.list_aliases(conn, system(ws), entity)
    assert len(aliases) == 1  # the VAT id; every site linked to the same entity

    cluster = confirm_cluster(seed, ws, [v.listing_id for v in vehicles])
    first = await reserve(db, world.with_vehicle(vehicles[0]))
    assert first.state == InquiryState.RESERVED and first.vehicle_cluster_id == cluster
    for other in vehicles[1:]:
        # Same (vehicle, seller) identity: the ONE record is already reserved.
        with pytest.raises(VersionConflict):
            await qualify(db, world.with_vehicle(other))
    assert await _reservations(db, world) == 1


async def test_separately_linked_entities_merge_and_keep_one_inquiry(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    vehicles = [await add_vehicle(db, seed, ws) for _ in range(3)]
    assert len({v.seller_entity_id for v in vehicles}) == 3
    first = await reserve_and_queue(db, world.with_vehicle(vehicles[0]))
    assert first.state == InquiryState.QUEUED

    # A later observation shows one VAT id on all three sites: one dealer.
    vat = alias("vat_id", "DE987654321", None, "same_vat_id")
    own = [alias("marketplace_seller_id", f"dealer-{v.reference}", v.source_key) for v in vehicles]
    linked = await link(db, ws, [*own, vat])
    assert linked.entity_id == vehicles[0].seller_entity_id  # the oldest entity survives
    assert len(linked.merges) == 2
    async with unit_of_work(db, system(ws)) as conn:
        for v in vehicles[1:]:
            assert await sellers_repo.seller_root(conn, system(ws), v.seller_entity_id) == linked.entity_id

    confirm_cluster(seed, ws, [v.listing_id for v in vehicles])
    for other in vehicles[1:]:
        # Re-verify the contact against the surviving entity (contact evidence is immutable).
        await record_contact(
            db,
            ws,
            other,
            linked.entity_id,
            address=other.address,
            aliases=[*own, vat],
        )
        _, decision = await readiness(db, world.with_vehicle(other))
        assert decision.readiness != InquiryReadiness.INQUIRY_READY and not decision.can_reserve_now
        # The merged seller's queued inquiry for the same vehicle blocks a second one, and its
        # reservation keeps the seller cooldown effective for the survivor.
        assert decision.codes() & {"INQUIRY_IN_PROGRESS", "PRIOR_INQUIRY", "POSSIBLE_DUPLICATE_CONTACT"}
        assert "SELLER_COOLDOWN" in decision.codes()
    assert await _reservations(db, world) == 1
    assert (await inquiry(db, world, first.id)).state == InquiryState.QUEUED


async def test_identity_merge_while_queued_cannot_double_send(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    a = await add_vehicle(db, seed, ws)
    b = await add_vehicle(db, seed, ws)
    # Two listings and two seller entities, not yet known to be one vehicle of one dealer.
    first = await reserve_and_queue(db, world.with_vehicle(a))
    second = await reserve_and_queue(db, world.with_vehicle(b))
    assert first.identity_key != second.identity_key

    sending = await dispatch(db, world, first.id)
    assert sending.outcome == "proceed"
    # Evidence arrives: same dealer (merge) and same vehicle (confirmed cluster).
    async with unit_of_work(db, system(ws)) as conn:
        outcome = await sellers_repo.merge_sellers(
            conn,
            system(ws),
            survivor_id=a.seller_entity_id,
            absorbed_id=b.seller_entity_id,
            reason="same dealer: identical VAT id on both sites",
        )
    confirm_cluster(seed, ws, [a.listing_id, b.listing_id])
    assert outcome.survivor_id == a.seller_entity_id and second.id in outcome.cancelled_inquiry_ids
    assert (await inquiry(db, world, second.id)).state == InquiryState.CANCELLED

    held = await dispatch(db, world, second.id)
    assert held.outcome == "hold" and held.attempt is None
    await report(db, world, sending, accepted(sending))
    assert await _attempts(db, world) == 1
    assert await _reservations(db, world) == 1  # the cancelled one's debit was released
    # Neither listing can open another inquiry for this vehicle with this dealer.
    for vehicle in (a, b):
        _, decision = await readiness(db, world.with_vehicle(vehicle))
        assert decision.readiness != InquiryReadiness.INQUIRY_READY


async def test_merge_keeps_the_absorbed_send_effective_for_the_survivor(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    a = await add_vehicle(db, seed, ws)
    b = await add_vehicle(db, seed, ws)
    survivor_queued = await reserve_and_queue(db, world.with_vehicle(a))
    absorbed = await reserve_and_queue(db, world.with_vehicle(b))
    sent = await dispatch(db, world, absorbed.id)
    await report(db, world, sent, accepted(sent))

    async with unit_of_work(db, system(ws)) as conn:
        outcome = await sellers_repo.merge_sellers(
            conn,
            system(ws),
            survivor_id=a.seller_entity_id,
            absorbed_id=b.seller_entity_id,
            reason="same dealer: identical legal entity id",
        )
    assert outcome.cancelled_inquiry_ids == ()  # the absorbed inquiry was transmitted: it stays
    assert (await inquiry(db, world, absorbed.id)).state == InquiryState.ACCEPTED
    # The absorbed entity's send now counts for the survivor: the queued one waits (cooldown).
    held = await dispatch(db, world, survivor_queued.id)
    assert held.outcome == "hold" and held.next_attempt_at is not None
    assert (await inquiry(db, world, survivor_queued.id)).state == InquiryState.QUEUED
    assert await _attempts(db, world) == 1


async def _attempts(db: Database, world: World) -> int:
    return int(
        await scalar(
            db,
            world,
            "select count(*) as n from ops.email_delivery_attempts where workspace_id = %(ws)s",
            {},
        )
    )
