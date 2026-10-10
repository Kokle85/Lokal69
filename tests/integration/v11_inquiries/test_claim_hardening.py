"""The worker claim and report as the last guards before ``.Send`` (work package C1, items 1 and 4).

- Fixture lineage (``app.listings.is_fixture``, frozen at ingest) is a FINAL ``intent_invalid``
  refusal of the claim, ahead of every other answer (also while the kill switch is on), so a
  fixture listing can never leave even when an intent exists for it (plan and dispatch refuse it
  in the runtime, ``tests/integration/v11_runtime/test_plan_and_send.py``).
- A ``refused_before_send`` report for an intent whose claim was GRANTED is no proof of
  non-submission (a stolen worker credential could report it after the real worker called
  ``.Send``): the attempt and the inquiry are held ``uncertain`` for owner reconciliation, the
  stored reports never reconcile it to ``failed_definite``, and the guarded retry refuses it. A
  refusal at claim time (claim not granted) or before any claim stays proof.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import World, add_vehicle, owner, scalar, system
from tests.integration.v11_inquiries.test_send_intents import (
    _claim,
    _intent,
    _report,
    _send_report,
    _worker,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryState
from suv_deals.domain.inquiries import SendAttemptOutcome, SenderBinding
from suv_deals.errors import EmailDeliveryUncertain
from suv_deals.integrations.email_providers.base import ReconcileProvenNotSubmitted, ReconcileWindow
from suv_deals.integrations.email_providers.outlook_local import (
    OutlookLocalProvider,
    OutlookRefusalReason,
    OutlookSubmissionState,
)
from suv_deals.persistence import inquiries_repo, send_intents_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _fixture_world(db: Database, seed: Seed, world: World) -> World:
    vehicle = await add_vehicle(db, seed, world.workspace_id, fixture=True)
    assert seed.scalar("select is_fixture from app.listings where id = %s", (vehicle.listing_id,)) is True
    return world.with_vehicle(vehicle)


# ---------------------------------------------------------------------------------------------
# Item 1: fixture lineage at the claim
# ---------------------------------------------------------------------------------------------


async def test_claim_refuses_fixture_lineage_as_final(db: Database, seed: Seed, world: World) -> None:
    fixture_world = await _fixture_world(db, seed, world)
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, fixture_world, worker)
    claim = await _claim(db, worker, intent.intent_id)
    assert not claim.proceed
    assert claim.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert claim.detail == "FIXTURE_LINEAGE"
    # Claims change no state; the refusal is audited (denied).
    assert await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    ) == ("sending")
    denied = await scalar(
        db,
        world,
        "select count(*) from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s"
        " and action = 'send_intent.claim' and metadata ->> 'detail' = 'FIXTURE_LINEAGE'"
        " and metadata ->> 'audit_outcome' = 'denied'",
        {"id": inquiry_id},
    )
    assert denied == 1


async def test_fixture_refusal_precedes_the_retryable_kill_switch(
    db: Database, seed: Seed, world: World
) -> None:
    """While paused a real intent gets the retryable ``kill_switch``; a fixture one is closed."""
    fixture_world = await _fixture_world(db, seed, world)
    worker = await _worker(db, world)
    _, intent = await _intent(db, fixture_world, worker)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.pause(conn, boss, expected_version=controls.version, reason="stop (synthetic)")
    claim = await _claim(db, worker, intent.intent_id)
    assert not claim.proceed and claim.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert claim.detail == "FIXTURE_LINEAGE"


async def test_real_lineage_world_claims_normally(db: Database, seed: Seed, world: World) -> None:
    """The shared synthetic world has REAL lineage (non-fixture source mode at ingest)."""
    assert seed.scalar("select is_fixture from app.listings where id = %s", (world.listing_id,)) is False
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    claim = await _claim(db, worker, intent.intent_id)
    assert claim.proceed and claim.refusal_reason is None


# ---------------------------------------------------------------------------------------------
# Item 4: a refusal after a GRANTED claim is no proof of non-submission
# ---------------------------------------------------------------------------------------------


async def test_refusal_after_granted_claim_is_held_uncertain(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed  # the real worker may now .Send
    # A (stolen) worker credential reports a pre-send refusal for the claimed intent.
    forged = await _send_report(
        db,
        worker,
        _report(
            intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.INTENT_EXPIRED
        ),
    )
    assert forged.applied and forged.inquiry_state == InquiryState.UNCERTAIN
    assert forged.attempt_outcome == SendAttemptOutcome.UNCERTAIN and not forged.reconciled
    attempt = await scalar(
        db,
        world,
        "select outcome || ':' || coalesce(error_code, '') || ':' || coalesce(pre_submission_proof, '-')"
        " from ops.email_delivery_attempts where attempt_id = %(id)s",
        {"id": intent.intent_id},
    )
    assert attempt == "uncertain:REFUSED_AFTER_GRANTED_CLAIM:-"
    # A repeated refusal report never reconciles it to failed_definite either.
    again = await _send_report(
        db,
        worker,
        _report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.KILL_SWITCH),
    )
    assert again.inquiry_state == InquiryState.UNCERTAIN and not again.reconciled
    # The provider's reconciliation (the reconcile job's path) does not prove it either.
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        record = await inquiries_repo.get_inquiry(conn, actor, inquiry_id)
        attempts = await inquiries_repo.list_attempts(conn, actor, inquiry_id)
    gateway = send_intents_repo.PersistentOutlookGateway(
        db, world.workspace_id, mailbox_binding_id=worker.mailbox_binding_id
    )
    assert record.sender_binding_id is not None and record.sender_binding_version is not None
    assert record.sender_account_id is not None and record.sender_from_address is not None
    assert record.sender_display_name is not None
    provider = OutlookLocalProvider(
        binding=SenderBinding(
            binding_id=record.sender_binding_id,
            binding_version=record.sender_binding_version,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id=record.sender_account_id,
            from_address=record.sender_from_address,
            display_name=record.sender_display_name,
            reply_to_address=record.sender_reply_to_address,
        ),
        mailbox_binding_id=worker.mailbox_binding_id,
        gateway=gateway,
    )
    outcome = await provider.reconcile(
        inquiry_id=inquiry_id,
        rfc_message_ids=[a.rfc_message_id for a in attempts if a.rfc_message_id],
        window=ReconcileWindow(start=attempts[0].send_intent_committed_at, end=attempts[0].lease_expires_at),
    )
    assert not isinstance(outcome, ReconcileProvenNotSubmitted)
    assert await gateway.claimed_intents(inquiry_id) == frozenset({intent.intent_id})


async def test_claim_time_refusal_stays_proof(db: Database, world: World) -> None:
    """The server refused the claim (kill switch): the worker never called ``.Send``."""
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.pause(conn, boss, expected_version=controls.version, reason="stop (synthetic)")
    refused_claim = await _claim(db, worker, intent.intent_id)
    assert not refused_claim.proceed
    report = await _send_report(
        db,
        worker,
        _report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.KILL_SWITCH),
    )
    assert report.inquiry_state == InquiryState.FAILED_DEFINITE


async def test_uncertain_reaped_intent_with_granted_claim_is_never_proven_unsent(
    db: Database, world: World
) -> None:
    """An expired intent the worker had claimed: a late ``intent_expired`` report is no proof."""
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed
    seed = world.seed
    with seed.conn.transaction():  # TEST ARRANGEMENT ONLY: the intent's lease ran out
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.email_delivery_attempts"
            " set lease_expires_at = send_intent_committed_at + interval '1 millisecond'"
            " where attempt_id = %s",
            (intent.intent_id,),
        )
    assert await inquiries_repo.reap_expired_attempts(db, world.workspace_id) == (intent.intent_id,)
    late = await _send_report(
        db,
        worker,
        _report(
            intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.INTENT_EXPIRED
        ),
    )
    assert late.inquiry_state == InquiryState.UNCERTAIN and not late.reconciled
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    )
    assert state == "uncertain"


async def test_guarded_retry_refuses_a_proof_recorded_after_a_granted_claim(
    db: Database, world: World
) -> None:
    """Defence in depth for rows written before C1: a pre-submission proof on an attempt whose
    claim was granted never re-queues the inquiry (owner reconciliation instead)."""
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed
    seed = world.seed
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.email_delivery_attempts set outcome = 'pre_submission_failure',"
            " pre_submission_proof = 'local_validation_failed_before_submit', finished_at = now()"
            " where attempt_id = %s",
            (intent.intent_id,),
        )
        seed.conn.execute(
            "update app.seller_inquiries set state = 'failed_definite' where id = %s", (inquiry_id,)
        )
    actor = system(world.workspace_id)
    with pytest.raises(EmailDeliveryUncertain) as exc:
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.retry(conn, actor, inquiry_id)
    assert exc.value.details["reason"] == "claim_granted_before_refusal"
