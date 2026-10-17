"""The ``outlook_local`` send route: committed intents, worker claim and report (spec 37.5, 37.6).

- ``dispatch_outlook`` commits ``queued -> sending`` and the running attempt (== the intent) in one
  transaction; the worker pulls exactly that intent (same Message-ID, MIME hash, recipient);
- the claim is a fresh revalidation right before ``.Send``: after the kill switch it answers
  ``proceed: false`` (``kill_switch``), never an error; claims change no state;
- reports finalise the attempt once (Sent Items -> accepted; Outbox -> uncertain); a later Sent
  Items report reconciles an uncertain attempt; repeats are no-ops;
- isolation: another workspace's (or another mailbox's) worker can neither see nor claim nor
  report the intent (RLS + mailbox binding).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    SENDER_ADDRESS,
    World,
    now_utc,
    owner,
    reserve_and_queue,
    scalar,
    system,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryState, Tristate
from suv_deals.domain.inquiries import SendAttemptOutcome
from suv_deals.errors import AppError, Forbidden
from suv_deals.integrations.email_providers.outlook_local import (
    IntentNotStored,
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
    OutlookSubmissionState,
)
from suv_deals.persistence import inquiries_repo, mail_workers_repo, send_intents_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.mail_workers_repo import WorkerIdentity
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

REQ = "req-b1a-worker"


async def _worker(db: Database, world: World) -> WorkerIdentity:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        issued = await mail_workers_repo.issue_mail_worker(
            conn, actor, sender_binding_id=world.sender_binding_id, label="Synthetic desktop worker"
        )
    async with db.transaction() as conn:
        return await mail_workers_repo.resolve_worker(conn, issued.credential.token.get_secret_value())


async def _intent(db: Database, world: World, worker: WorkerIdentity) -> tuple[UUID, OutlookSendIntent]:
    record = await reserve_and_queue(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        dispatched = await send_intents_repo.dispatch_outlook(
            conn,
            actor,
            record.id,
            mailbox_binding_id=worker.mailbox_binding_id,
            message_approval_required=False,
        )
    assert dispatched.result.outcome == "proceed" and dispatched.intent is not None
    return record.id, dispatched.intent


def _report(
    intent: OutlookSendIntent,
    state: OutlookSubmissionState,
    *,
    refusal: OutlookRefusalReason | None = None,
    mailbox: UUID | None = None,
) -> OutlookSendReport:
    confirmed = state == OutlookSubmissionState.SENT_ITEMS_CONFIRMED
    return OutlookSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=mailbox or intent.mailbox_binding_id,
        worker_id="synthetic-desktop-1",
        state=state,
        refusal_reason=refusal,
        account_smtp_address_used=SENDER_ADDRESS,
        observed_internet_message_id=intent.rfc_message_id if confirmed else None,
        outbox_pending=Tristate.YES
        if state == OutlookSubmissionState.SUBMITTED_TO_OUTBOX
        else Tristate.UNKNOWN,
        sent_items_present=confirmed,
        reported_at=now_utc(),
        sent_at=now_utc() if confirmed else None,
    )


async def _binding_states(db: Database, world: World, inquiry_id: UUID) -> str:
    return str(
        await scalar(
            db,
            world,
            "select string_agg(binding_state, ',' order by binding_version) from ops.mail_binding_sync"
            " where workspace_id = %(ws)s and inquiry_id = %(id)s",
            {"id": inquiry_id},
        )
    )


async def _pending(db: Database, worker: WorkerIdentity) -> send_intents_repo.SendIntentBatch:
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        return await send_intents_repo.list_pending(conn, worker, request_id=REQ)


async def _claim(db: Database, worker: WorkerIdentity, intent_id: UUID) -> send_intents_repo.ClaimResult:
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        return await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent_id,
            claim_attempt_id="claim-1",
            worker_id="synthetic-desktop-1",
            request_id=REQ,
        )


async def _send_report(
    db: Database, worker: WorkerIdentity, report: OutlookSendReport
) -> send_intents_repo.ReportResult:
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        return await send_intents_repo.report(conn, worker, report=report, request_id=REQ)


async def test_intent_claim_and_sent_items_report(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert intent.idempotency_key == f"send-{intent.intent_id}"
    assert intent.rfc_message_id.startswith(f"<inquiry-{inquiry_id}.1@")
    assert intent.to_address == world.vehicle.address and intent.from_address == SENDER_ADDRESS
    assert intent.not_after > now_utc()

    batch = await _pending(db, worker)
    assert not batch.kill_switch_active
    assert [i.intent_id for i in batch.intents] == [intent.intent_id]
    assert batch.intents[0] == intent  # deterministic: the same intent every time

    claim = await _claim(db, worker, intent.intent_id)
    assert claim.proceed and claim.refusal_reason is None
    result = await _send_report(db, worker, _report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED))
    assert result.applied and result.inquiry_state == InquiryState.ACCEPTED
    assert result.attempt_outcome == SendAttemptOutcome.ACCEPTED
    repeat = await _send_report(db, worker, _report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED))
    assert not repeat.applied and repeat.inquiry_state == InquiryState.ACCEPTED
    assert (await _pending(db, worker)).intents == ()
    # The stored report holds no address.
    stored = await scalar(
        db,
        world,
        "select provider_response::text from ops.email_delivery_attempts where attempt_id = %(id)s",
        {"id": intent.intent_id},
    )
    assert SENDER_ADDRESS not in stored and str(world.vehicle.address) not in stored

    gateway = send_intents_repo.PersistentOutlookGateway(
        db, world.workspace_id, mailbox_binding_id=worker.mailbox_binding_id
    )
    assert [i.intent_id for i in await gateway.intents_for(inquiry_id)] == [intent.intent_id]
    assert [r.state for r in await gateway.reports_for(inquiry_id)] == [
        OutlookSubmissionState.SENT_ITEMS_CONFIRMED
    ]
    with pytest.raises(IntentNotStored):
        await gateway.publish_intent(intent)  # finalised: no running attempt for it any more


async def test_claim_after_kill_switch_answers_proceed_false(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    gateway = send_intents_repo.PersistentOutlookGateway(
        db, world.workspace_id, mailbox_binding_id=worker.mailbox_binding_id
    )
    await gateway.publish_intent(intent)  # the committed intent is exactly this one

    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.pause(conn, boss, expected_version=controls.version, reason="stop (synthetic)")
    batch = await _pending(db, worker)
    assert batch.kill_switch_active and [i.intent_id for i in batch.intents] == [intent.intent_id]
    claim = await _claim(db, worker, intent.intent_id)
    assert not claim.proceed and claim.refusal_reason == OutlookRefusalReason.KILL_SWITCH
    assert (
        await scalar(
            db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
        )
        == "sending"
    )
    # The worker refuses before .Send and reports it: a proven pre-submission refusal.
    refused = await _send_report(
        db,
        worker,
        _report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.KILL_SWITCH),
    )
    assert refused.applied and refused.inquiry_state == InquiryState.FAILED_DEFINITE


async def test_outbox_report_is_uncertain_until_sent_items_evidence(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed
    pending = await _send_report(db, worker, _report(intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX))
    assert pending.applied and pending.inquiry_state == InquiryState.UNCERTAIN
    # The mailbox binding follows every send-path transition (the worker links replies with it).
    assert await _binding_states(db, world, intent.inquiry_id) == "active,uncertain"
    # A second claim of a finalised intent is refused (the intent is closed).
    closed = await _claim(db, worker, intent.intent_id)
    assert not closed.proceed and closed.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    confirmed = await _send_report(db, worker, _report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED))
    assert confirmed.applied and confirmed.reconciled and confirmed.inquiry_state == InquiryState.ACCEPTED
    assert await _binding_states(db, world, intent.inquiry_id) == "active,uncertain,active"


async def test_listing_change_after_dispatch_refuses_the_claim(
    db: Database, seed: Seed, world: World
) -> None:
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    seed.conn.execute(
        "update app.listings set availability = 'sold_claimed' where id = %s", (world.listing_id,)
    )
    claim = await _claim(db, worker, intent.intent_id)
    assert not claim.proceed and claim.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert claim.detail == "LISTING_CHANGED"


async def test_other_workspace_or_mailbox_cannot_see_claim_or_report(
    db: Database, world: World, world_b: World
) -> None:
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    stranger = await _worker(db, world_b)
    assert (await _pending(db, stranger)).intents == ()
    with pytest.raises(Forbidden):
        await _claim(db, stranger, intent.intent_id)
    with pytest.raises((Forbidden, AppError)):
        await _send_report(
            db,
            stranger,
            _report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED, mailbox=stranger.mailbox_binding_id),
        )
    # RLS: the other workspace's system principal does not even see the attempt.
    actor_b = system(world_b.workspace_id)
    with pytest.raises(AppError):
        async with unit_of_work(db, actor_b) as conn:
            await inquiries_repo.get_attempt(conn, actor_b, intent.intent_id)
    # The mailbox of another sender binding of the SAME workspace is another mailbox: refused.
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        other_sender = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id="synthetic-outlook-account-2",
            from_address="second-desk@synthetic-mail.example",
            display_name="Synthetic Second Desk",
            reason="second sending identity (synthetic)",
        )
    sibling = await _worker(db, replace(world, sender_binding_id=other_sender.id))
    assert sibling.mailbox_binding_id != worker.mailbox_binding_id
    assert (await _pending(db, sibling)).intents == ()
    with pytest.raises(Forbidden):
        await _claim(db, sibling, intent.intent_id)
    assert (await _claim(db, worker, intent.intent_id)).proceed


async def test_dispatch_outlook_requires_the_inquiry_senders_mailbox(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    record = await reserve_and_queue(db, world)
    actor = system(world.workspace_id)
    with pytest.raises(AppError):
        async with unit_of_work(db, actor) as conn:
            await send_intents_repo.dispatch_outlook(
                conn,
                actor,
                record.id,
                mailbox_binding_id=worker.mailbox_binding_id,
                message_approval_required=False,
                ttl=timedelta(minutes=1),
            )
    async with unit_of_work(db, actor) as conn:
        heartbeat = await send_intents_repo.PersistentOutlookGateway(
            db, world.workspace_id, mailbox_binding_id=worker.mailbox_binding_id
        ).latest_heartbeat(worker.mailbox_binding_id)
    assert heartbeat is None  # no heartbeat received yet
