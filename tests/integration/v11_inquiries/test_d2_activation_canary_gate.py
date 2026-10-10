"""F3 / OPS-04 (wave D2): no real inquiry is reserved before the sender route's activation evidence.

Spec 32 lists live test evidence among the activation requirements, but before D2 nothing in the
code required it: ``set_mode('automatic')`` checked the sender and the authorization only, so a
real seller inquiry could be reserved (and sent) without any completed canary. Gating
``set_mode`` itself would deadlock (``canary send`` needs automatic mode), so the reservation is
gated instead:

- ``inquiries_repo.reserve`` refuses with problem ``ACTIVATION_CANARY_INCOMPLETE`` (``details.reason
  = activation_canary_incomplete``) until a ``reply_correlated`` canary exists for the sender
  binding's CURRENT version, and nothing changes (no debit, no state change);
- the readiness snapshot carries the flag (the plan job records readiness with the named
  reservation refusal);
- a re-verified sender (a new binding version) needs a new canary; a canary that is only
  ``accepted`` (no correlated reply) is not enough.
"""

from __future__ import annotations

import pytest
from tests.integration.v11_inquiries.support import (
    World,
    complete_activation_canary,
    qualify,
    reserve,
    reserve_existing,
    scalar,
    system,
)

from suv_deals.domain.enums import InquiryState
from suv_deals.errors import ValidationFailed
from suv_deals.persistence import canaries_repo, inquiries_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = [pytest.mark.db, pytest.mark.no_arranged_canary]


async def _debits(db: Database, world: World) -> int:
    return int(
        await scalar(
            db, world, "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s", {}
        )
    )


async def test_reservation_needs_a_completed_canary_of_the_current_sender_version(
    db: Database, world: World
) -> None:
    record, decision, snapshot = await qualify(db, world)
    assert snapshot.activation_canary_complete is False
    with pytest.raises(ValidationFailed) as refused:
        await reserve_existing(db, world, record, decision, snapshot)
    assert refused.value.details is not None
    assert refused.value.details["problems"] == [inquiries_repo.ACTIVATION_CANARY_INCOMPLETE]
    assert refused.value.details["reason"] == "activation_canary_incomplete"
    assert await _debits(db, world) == 0
    state = await scalar(db, world, "select state from app.seller_inquiries where workspace_id = %(ws)s", {})
    assert state != InquiryState.RESERVED

    await complete_activation_canary(db, world.workspace_id, world.sender_binding_id)
    reserved = await reserve(db, world)
    assert reserved.state == InquiryState.RESERVED and await _debits(db, world) == 1


async def test_an_accepted_canary_without_a_reply_is_not_enough(db: Database, world: World) -> None:
    actor = system(world.workspace_id)
    from tests.integration.v11_inquiries.test_send_intents import _worker  # noqa: PLC0415

    await _worker(db, world)
    async with unit_of_work(db, actor) as conn:
        canary = await canaries_repo.create_canary(
            conn,
            actor,
            sender_binding_id=world.sender_binding_id,
            target_address="owner-canary-test@example.invalid",
            purpose="SYNTHETIC canary without a reply",
        )
        await canaries_repo.record_canary_outcome(conn, actor, canary.id, outcome="accepted")
    _record, _decision, snapshot = await qualify(db, world)
    assert snapshot.activation_canary_complete is False
    with pytest.raises(ValidationFailed, match="activation canary"):
        await reserve(db, world)


async def test_a_reverified_sender_needs_a_new_canary(db: Database, world: World) -> None:
    await complete_activation_canary(db, world.workspace_id, world.sender_binding_id)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        binding = await sender_bindings_repo.get_binding(conn, actor, world.sender_binding_id)
        await sender_bindings_repo.record_verification(
            conn,
            actor,
            binding.id,
            expected_version=binding.version,
            verified=True,
            alias_verified=True,
            health="healthy",
            reason="re-verified after an identity change (synthetic)",
        )
        assert await inquiries_repo.activation_canary_complete(
            conn, world.workspace_id, binding.id, binding.version
        )
        assert not await inquiries_repo.activation_canary_complete(
            conn, world.workspace_id, binding.id, binding.version + 1
        )
    with pytest.raises(ValidationFailed, match="activation canary"):
        await reserve(db, world)
    await complete_activation_canary(db, world.workspace_id, world.sender_binding_id)
    assert (await reserve(db, world)).state == InquiryState.RESERVED
