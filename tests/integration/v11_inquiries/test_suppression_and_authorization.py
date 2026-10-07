"""Suppressions and the revocable standing authorization (spec 37.1, 37.5, 37.10).

- a suppression (seller, address, vehicle, source, sender, workspace) suppresses matching
  untransmitted inquiries at once (quota debit released) and blocks new ones; a seller key is
  normalised to the surviving entity of a merge; removal is explicit, by an owner, audited;
- a revoked authorization version suppresses every untransmitted inquiry with
  ``authorization_revoked``; transmitted ones keep their evidence; a later unrevoked version
  plus the owner's resume re-qualifies them (one audit event each).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    AUTHORIZATION,
    World,
    add_vehicle,
    dispatch,
    now_utc,
    owner,
    readiness,
    reserve,
    reserve_and_queue,
    scalar,
    send,
    system,
)

from suv_deals.domain.enums import InquiryReadiness, InquiryState, SuppressionReason
from suv_deals.domain.inquiries import SellerInquiryAuthorization
from suv_deals.errors import AppError, Forbidden, IdempotencyConflict, VersionConflict
from suv_deals.persistence import inquiries_repo, sellers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _state(db: Database, world: World, inquiry_id: object) -> str:
    return str(
        await scalar(
            db,
            world,
            "select state || coalesce(':' || suppression_reason, '') from app.seller_inquiries"
            " where id = %(id)s",
            {"id": inquiry_id},
        )
    )


async def _open_debits(db: Database, world: World) -> int:
    return int(
        await scalar(
            db,
            world,
            "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s"
            " and released_at is null",
            {},
        )
    )


async def test_seller_suppression_stops_pending_and_new_inquiries(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    queued = await reserve_and_queue(db, world)
    assert await _open_debits(db, world) == 1
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        added = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(world.seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
            evidence={"reply": "synthetic opt-out"},
        )
    assert added.created and added.suppressed_inquiry_ids == (queued.id,)
    assert await _state(db, world, queued.id) == "suppressed:seller_opt_out"
    assert await _open_debits(db, world) == 0  # never transmitted: the debit is released
    held = await dispatch(db, world, queued.id)
    assert held.outcome == "hold" and held.attempt is None

    # The seller is not eligible while the suppression is active.
    _, decision = await readiness(db, world)
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE
    assert {"SELLER_OPTED_OUT", "SUPPRESSED_SELLER_OPT_OUT"} & decision.codes()

    # Idempotent per active scope/key/reason.
    async with unit_of_work(db, actor) as conn:
        again = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(world.seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
        )
    assert not again.created and again.suppression.id == added.suppression.id

    # Removal: never by the system, only by an owner, with an audit event first.
    with pytest.raises(Forbidden):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.remove_suppression(conn, actor, added.suppression.id, reason="automatic")
    boss = owner(ws)
    async with unit_of_work(db, boss) as conn:
        removed = await inquiries_repo.remove_suppression(
            conn, boss, added.suppression.id, reason="seller asked to be contacted again (synthetic)"
        )
    assert removed.removed_at is not None and removed.removal_audit_id is not None
    audited = await scalar(
        db,
        world,
        "select action from ops.audit_events where id = %(id)s",
        {"id": removed.removal_audit_id},
    )
    assert audited == "email_suppression.remove"
    async with unit_of_work(db, actor) as conn:
        assert await inquiries_repo.active_suppressions(conn, actor, scope="seller") == []


async def test_address_and_vehicle_suppressions_match_case_and_relistings(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    first = await reserve(db, world)
    assert world.vehicle.address is not None
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        added = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="address",
            key=world.vehicle.address.upper(),
            reason=SuppressionReason.HARD_BOUNCE,
        )
    assert added.suppressed_inquiry_ids == (first.id,)
    assert await _state(db, world, first.id) == "suppressed:hard_bounce"

    second_vehicle = await add_vehicle(db, seed, ws)
    second = await reserve(db, world.with_vehicle(second_vehicle))
    async with unit_of_work(db, actor) as conn:
        by_vehicle = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="vehicle",
            key=f"listing_incarnation:{second_vehicle.listing_id}",
            reason=SuppressionReason.MANUAL,
        )
    assert by_vehicle.suppressed_inquiry_ids == (second.id,)
    with pytest.raises(AppError):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.add_suppression(
                conn, actor, scope="seller", key="seller_alias:not-linked", reason=SuppressionReason.MANUAL
            )


async def test_seller_suppression_names_the_surviving_entity_after_a_merge(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    other = await add_vehicle(db, seed, ws)
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        await sellers_repo.merge_sellers(
            conn,
            actor,
            survivor_id=world.seller_entity_id,
            absorbed_id=other.seller_entity_id,
            reason="same dealer (synthetic legal entity id)",
        )
        added = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(other.seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
        )
    assert added.suppression.scope_key == inquiries_repo.seller_suppression_key(world.seller_entity_id)
    _, decision = await readiness(db, world)
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE and "SELLER_OPTED_OUT" in decision.codes()


def _revoked(version: int) -> SellerInquiryAuthorization:
    data = AUTHORIZATION.model_dump(mode="json")
    data["version"] = version
    data["revocation"] = {
        "revoked": True,
        "revoked_at": (now_utc() - timedelta(seconds=1)).isoformat(),
        "revoked_by": "Synthetic owner",
        "reason": "owner revoked the standing authorization (synthetic)",
    }
    return SellerInquiryAuthorization.model_validate(data)


async def test_revoked_authorization_suppresses_untransmitted_inquiries(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    sent, _ = await send(db, world)
    queued = await reserve_and_queue(db, world.with_vehicle(await add_vehicle(db, seed, ws)))
    actor = system(ws)
    revocation = _revoked(2)
    async with unit_of_work(db, actor) as conn:
        record = await inquiries_repo.record_authorization(conn, actor, revocation, reason="revocation v2")
        # Versions are consecutive and append-only; an identical version is idempotent ...
        same = await inquiries_repo.record_authorization(conn, actor, revocation, reason="repeat")
    assert record.version == 2 and record.revoked_at is not None and same.id == record.id
    # ... while the same version with other content is a conflict.
    with pytest.raises(IdempotencyConflict):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.record_authorization(conn, actor, _revoked(2), reason="other content")
    with pytest.raises(VersionConflict):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.record_authorization(
                conn, actor, AUTHORIZATION.model_copy(update={"version": 5}), reason="gap"
            )

    assert await _state(db, world, queued.id) == "suppressed:authorization_revoked"
    assert await _state(db, world, sent.id) == "accepted"  # transmitted: evidence kept
    held = await dispatch(db, world, queued.id)
    assert held.outcome == "hold" and held.attempt is None
    _, decision = await readiness(db, world.with_vehicle(await add_vehicle(db, seed, ws)))
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE
    assert "AUTHORIZATION_REVOKED" in decision.codes()

    # A new unrevoked version and the owner's resume re-qualify the suppressed inquiry.
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.record_authorization(
            conn, actor, AUTHORIZATION.model_copy(update={"version": 3}), reason="re-authorized v3"
        )
        controls = await inquiries_repo.get_controls(conn, actor)
    assert controls is not None
    boss = owner(ws)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.resume(conn, boss, expected_version=controls.version, reason="re-authorized")
    assert await _state(db, world, queued.id) == InquiryState.QUALIFYING.value
