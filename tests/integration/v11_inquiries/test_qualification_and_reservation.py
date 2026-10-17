"""Qualification and reservation under the standing authorization (spec 37.1-37.3, 37.10).

- a candidate whose CoC/registration documents are unknown is still reserved (the inquiry asks);
- an unknown recipient address or an unresolved language can never be reserved;
- there is no approval-wait state anywhere: ``qualifying -> reserved -> queued -> sending``
  happens without a click, and only the explicit owner setting can hold a dispatch for approval;
- the reservation binds exactly the current verified contact, sender binding version and
  authorization; a stale readiness decision or a contact changed since the read is refused.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    LANGUAGE_DE,
    SENDER_ADDRESS,
    UNRESOLVABLE_TEXT,
    World,
    add_vehicle,
    alias,
    dispatch,
    language_from,
    qualify,
    queue,
    readiness,
    record_contact,
    reserve,
    reserve_existing,
    scalar,
    seller_address,
    system,
)

from suv_deals.domain.enums import InquiryReadiness, InquiryState
from suv_deals.domain.inquiries import MAX_READINESS_AGE
from suv_deals.domain.seller_contacts import RecipientEvidenceKind
from suv_deals.errors import ValidationFailed, VersionConflict
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _ledger(db: Database, world: World) -> int:
    return int(
        await scalar(
            db,
            world,
            "select count(*) as n from ops.inquiry_quota_ledger where workspace_id = %(ws)s"
            " and released_at is null",
            {},
        )
    )


async def test_candidate_lacking_coc_is_still_reserved(db: Database, world: World) -> None:
    snapshot, decision = await readiness(db, world)
    assert decision.readiness == InquiryReadiness.INQUIRY_READY and decision.can_reserve_now
    # The CoC and registration documents are unknown: the inquiry ASKS for them.
    assert "coc" in decision.open_questions and "registration_documents" in decision.open_questions
    assert snapshot.contact is not None and snapshot.contact.status == "verified"

    record = await reserve(db, world)
    assert record.state == InquiryState.RESERVED
    assert record.language is not None and record.language.value == "de"
    assert record.template_id == "seller_initial_de_v1"
    assert record.recipient_address == world.vehicle.address
    assert record.recipient_contact_id == world.vehicle.contact_id
    assert record.sender_from_address == SENDER_ADDRESS and record.sender_binding_version == 2
    assert record.authorization_version == 1
    assert record.qualified_price_minor == 275000 and record.qualified_currency == "EUR"
    assert record.original_body is not None and world.vehicle.reference in record.original_body
    assert record.mk_preview_body and record.mk_preview_hash
    assert await _ledger(db, world) == 1
    audited = await scalar(
        db,
        world,
        "select count(*) as n from ops.audit_events where workspace_id = %(ws)s"
        " and action = 'seller_inquiry.reserve' and target_id = %(id)s",
        {"id": record.id},
    )
    assert audited == 1


async def test_unknown_address_cannot_be_reserved(db: Database, seed: Seed, world: World) -> None:
    vehicle = await add_vehicle(
        db, seed, world.workspace_id, address=None, kind=RecipientEvidenceKind.NO_EMAIL_FOUND
    )
    target = world.with_vehicle(vehicle)
    snapshot, decision = await readiness(db, target)
    assert decision.readiness == InquiryReadiness.NEEDS_FACTS
    assert "SELLER_EMAIL_UNAVAILABLE" in decision.codes()
    assert snapshot.contact is not None and snapshot.contact.status == "unavailable"

    record, decision, snapshot = await qualify(db, target)
    assert record.state == InquiryState.HELD_FACTS
    with pytest.raises(ValidationFailed):
        inquiries_repo.prepare_binding(snapshot, decision, inquiry_id=record.id)
    assert await _ledger(db, world) == 0


async def test_unresolved_or_missing_language_cannot_be_reserved(
    db: Database, seed: Seed, world: World
) -> None:
    unresolved = await add_vehicle(db, seed, world.workspace_id, language=language_from(UNRESOLVABLE_TEXT))
    target = world.with_vehicle(unresolved)
    record, decision, snapshot = await qualify(db, target)
    assert decision.readiness == InquiryReadiness.NEEDS_FACTS
    assert "LANGUAGE_UNRESOLVED" in decision.codes()
    assert record.state == InquiryState.HELD_FACTS
    with pytest.raises(ValidationFailed):
        inquiries_repo.prepare_binding(snapshot, decision, inquiry_id=record.id)

    never_evaluated = await add_vehicle(db, seed, world.workspace_id, language=None)
    _, decision = await readiness(db, world.with_vehicle(never_evaluated))
    assert "LANGUAGE_UNKNOWN" in decision.codes() and decision.readiness == InquiryReadiness.NEEDS_FACTS
    assert await _ledger(db, world) == 0


async def test_there_is_no_approval_wait_state(db: Database, world: World) -> None:
    assert not any("approv" in state.value for state in InquiryState)
    states = await scalar(
        db,
        world,
        "select pg_get_constraintdef(oid) as d from pg_constraint"
        " where conrelid = 'app.seller_inquiries'::regclass and conname = 'seller_inquiries_state_ck'",
        {},
    )
    assert "approv" not in states

    record = await reserve(db, world)
    queued = await queue(db, world, record.id)
    assert queued.state == InquiryState.QUEUED
    # Only the explicit owner setting can require a message approval: the dispatch then holds.
    held = await dispatch(db, world, record.id, approval_required=True)
    assert held.outcome == "hold" and held.attempt is None
    proceed = await dispatch(db, world, record.id)
    assert proceed.outcome == "proceed" and proceed.attempt is not None and proceed.message is not None
    assert proceed.message.rfc_message_id.startswith(f"<inquiry-{record.id}.1@")


async def test_reserve_is_once_per_identity_and_refuses_stale_inputs(db: Database, world: World) -> None:
    record, decision, snapshot = await qualify(db, world)
    stale = decision.model_copy(update={"as_of": decision.as_of - MAX_READINESS_AGE - timedelta(minutes=1)})
    with pytest.raises(ValidationFailed) as exc:
        await reserve_existing(db, world, record, stale, snapshot)
    assert "READINESS_STALE" in str(exc.value.details)

    reserved = await reserve_existing(db, world, record, decision, snapshot)
    assert reserved.state == InquiryState.RESERVED
    with pytest.raises(VersionConflict):
        await reserve_existing(db, world, record, decision, snapshot)
    # The identity has ONE record: opening it again returns the same row.
    async with unit_of_work(db, system(world.workspace_id)) as conn:
        again = await inquiries_repo.open_inquiry(
            conn, system(world.workspace_id), snapshot.identity, qualification_listing_id=world.listing_id
        )
    assert again.id == record.id and again.state == InquiryState.RESERVED
    assert await _ledger(db, world) == 1


async def test_contact_changed_after_the_read_is_refused(db: Database, world: World) -> None:
    record, decision, snapshot = await qualify(db, world)
    # The listing now shows another address: the verified contact is superseded.
    own = [alias("marketplace_seller_id", f"dealer-{world.vehicle.reference}", world.vehicle.source_key)]
    changed = await record_contact(
        db,
        world.workspace_id,
        world.vehicle,
        world.seller_entity_id,
        address=seller_address(),
        aliases=own,
        language=LANGUAGE_DE,
    )
    assert changed.status == "verified" and changed.id != world.vehicle.contact_id
    with pytest.raises(ValidationFailed) as exc:
        await reserve_existing(db, world, record, decision, snapshot)
    assert "RECIPIENT_NOT_CURRENT_VERIFIED_CONTACT" in str(exc.value.details)
    assert await _ledger(db, world) == 0
