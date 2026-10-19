"""Send outcomes, uncertainty, reconciliation, fencing and staleness (spec 37.5, 37.10).

- a timeout or a crash after the hand-over makes the inquiry ``uncertain``: never re-queued,
  never resent blindly; an empty Sent Items search keeps it held with its reservation and debit;
  only positive evidence (Sent Items / provider hit, correlated inbound) resolves it;
- the reapers: an expired running attempt becomes ``uncertain``; an expired running
  ``seller_inquiry_send`` job is ``blocked`` with EMAIL_DELIVERY_UNCERTAIN and never requeued;
- fencing: a report under a lost lease token cannot overwrite; a late pre-submission report
  after the lease expired is recorded as uncertain; a finalised attempt only gains positive
  acceptance evidence;
- a guarded retry after a proven pre-submission failure uses attempt 2 and a new Message-ID;
- a price or availability change cancels a stale queued inquiry and releases its debit.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.pipeline.support import pipeline_settings
from tests.integration.v11_inquiries.support import (
    World,
    accepted,
    dispatch,
    now_utc,
    refused_before_submit,
    report,
    reserve_and_queue,
    scalar,
    system,
    uncertain,
)

from suv_deals.domain.enums import InquiryState, JobState, JobType
from suv_deals.domain.inquiries import ReconciliationEvidence, SendAttemptOutcome
from suv_deals.errors import EmailDeliveryUncertain, VersionConflict
from suv_deals.integrations.email_providers.base import SendDefiniteFailure, SendFailureReason
from suv_deals.persistence import inquiries_repo, jobs
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runtime import RuntimeContext

pytestmark = pytest.mark.db


def _expire(conn: psycopg.Connection, attempt_id: UUID) -> None:
    """TEST ARRANGEMENT ONLY: the attempt's lease ran out (the worker crashed after the hand-over)."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts set lease_expires_at = now() - interval '1 second'"
            " where attempt_id = %s",
            (attempt_id,),
        )


async def _state(db: Database, world: World, inquiry_id: UUID) -> str:
    return str(
        await scalar(
            db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
        )
    )


async def _debits(db: Database, world: World) -> int:
    return int(
        await scalar(
            db,
            world,
            "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s"
            " and released_at is null",
            {},
        )
    )


async def _attempts(db: Database, world: World) -> int:
    return int(
        await scalar(
            db, world, "select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s", {}
        )
    )


async def _reconcile(db: Database, world: World, inquiry_id: UUID, evidence: ReconciliationEvidence):  # type: ignore[no-untyped-def]
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.reconcile(conn, actor, inquiry_id, evidence=evidence)


async def test_timeout_after_hand_over_is_held_and_never_resent(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    outcome = await report(db, world, result, uncertain(result))
    assert outcome.applied and outcome.inquiry_state == InquiryState.UNCERTAIN
    assert outcome.attempt_outcome == SendAttemptOutcome.UNCERTAIN

    # No blind resend: the dispatcher holds, the retry policy refuses.
    again = await dispatch(db, world, record.id)
    assert again.outcome == "hold" and again.attempt is None
    with pytest.raises(VersionConflict):
        async with unit_of_work(db, system(world.workspace_id)) as conn:
            await inquiries_repo.retry(conn, system(world.workspace_id), record.id)

    # An empty Sent Items / provider search never releases anything.
    empty = await _reconcile(
        db, world, record.id, ReconciliationEvidence(sent_items="not_found", provider_search="not_found")
    )
    assert empty.decision.next_state is None and empty.inquiry_state == InquiryState.UNCERTAIN
    assert await _state(db, world, record.id) == "uncertain"
    assert await _debits(db, world) == 1 and await _attempts(db, world) == 1
    checks = await scalar(
        db,
        world,
        "select count(*) from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s"
        " and action = 'seller_inquiry.reconcile_check'",
        {"id": record.id},
    )
    assert checks == 1

    # Positive evidence resolves it, exactly once.
    found = await _reconcile(db, world, record.id, ReconciliationEvidence(sent_items="found"))
    assert found.inquiry_state == InquiryState.ACCEPTED
    with pytest.raises(VersionConflict):
        await _reconcile(db, world, record.id, ReconciliationEvidence(sent_items="found"))
    assert await _debits(db, world) == 1


async def test_crash_after_hand_over_reaped_to_uncertain_and_job_blocked(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None
    job_id = seed.job(
        ws,
        job_type=JobType.SELLER_INQUIRY_SEND.value,
        state="running",
        attempts=1,
        lease_owner="b1a-crashed-worker",
        lease_token=uuid.uuid4(),
        lease_expires_at=now_utc() - timedelta(seconds=5),
        payload={"inquiry_id": str(record.id)},
    )
    _expire(seed.conn, result.attempt.attempt_id)

    reaped = await inquiries_repo.reap_expired_attempts(db, ws)
    assert reaped == (result.attempt.attempt_id,)
    assert await _state(db, world, record.id) == "uncertain"
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        attempt = await inquiries_repo.get_attempt(conn, actor, result.attempt.attempt_id)
    assert (
        attempt.outcome == SendAttemptOutcome.UNCERTAIN and attempt.error_code == inquiries_repo.LEASE_EXPIRED
    )
    assert await inquiries_repo.reap_expired_attempts(db, ws) == ()

    job_reap = await jobs.reap_expired(db, ws, retry_delay_seconds=0)
    assert job_reap.blocked_uncertain == (job_id,) and job_reap.requeued == () and job_reap.total == 1
    assert job_reap.expired_by_type == {JobType.SELLER_INQUIRY_SEND: 1}
    async with unit_of_work(db, actor) as conn:
        job = await jobs.get_job(conn, actor, job_id)
    assert job.state == JobState.BLOCKED and job.blocker_code == "EMAIL_DELIVERY_UNCERTAIN"
    assert job.lease_owner is None and job.completed_at is None
    # Never requeued, however often the reaper runs.
    assert (await jobs.reap_expired(db, ws, retry_delay_seconds=0)).total == 0
    async with unit_of_work(db, actor) as conn:
        assert (await jobs.get_job(conn, actor, job_id)).state == JobState.BLOCKED
    assert await _attempts(db, world) == 1 and await _debits(db, world) == 1


async def test_other_expired_jobs_are_still_requeued(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    job_id = seed.job(
        ws,
        job_type=JobType.VALUATION.value,
        state="running",
        attempts=1,
        lease_owner="b1a-other-worker",
        lease_token=uuid.uuid4(),
        lease_expires_at=now_utc() - timedelta(seconds=5),
    )
    reaped = await jobs.reap_expired(db, ws, retry_delay_seconds=0)
    assert reaped.requeued == (job_id,) and reaped.blocked_uncertain == ()


async def test_lost_lease_report_cannot_overwrite(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    with pytest.raises(LeaseLost):
        await report(db, world, result, accepted(result), token=uuid.uuid4())
    assert await _state(db, world, record.id) == "sending"

    first = await report(db, world, result, uncertain(result))
    assert first.applied and first.inquiry_state == InquiryState.UNCERTAIN
    # A late, different report for the finalised attempt changes nothing ...
    late_failure = await report(db, world, result, refused_before_submit(result))
    assert not late_failure.applied and late_failure.inquiry_state == InquiryState.UNCERTAIN
    # ... except positive acceptance evidence, recorded once as reconciliation.
    late_accept = await report(db, world, result, accepted(result, sent_items=True))
    assert late_accept.applied and late_accept.reconciled
    assert await _state(db, world, record.id) == "accepted"
    repeat = await report(db, world, result, accepted(result, sent_items=True))
    assert not repeat.applied


async def test_late_pre_submission_report_after_expiry_is_uncertain(
    db: Database, seed: Seed, world: World
) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None
    _expire(seed.conn, result.attempt.attempt_id)
    outcome = await report(db, world, result, refused_before_submit(result))
    assert outcome.applied and outcome.inquiry_state == InquiryState.UNCERTAIN
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        attempt = await inquiries_repo.get_attempt(conn, actor, result.attempt.attempt_id)
    assert attempt.error_code == "LATE_PRE_SUBMISSION_REPORT" and attempt.pre_submission_proof is None


async def test_proven_pre_submission_failure_allows_one_guarded_retry(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    first = await dispatch(db, world, record.id)
    outcome = await report(db, world, first, refused_before_submit(first))
    assert outcome.inquiry_state == InquiryState.FAILED_DEFINITE
    assert outcome.attempt_outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE
    assert await _debits(db, world) == 1  # the debit stays with the inquiry

    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        retried = await inquiries_repo.retry(conn, actor, record.id)
    assert retried.state == InquiryState.QUEUED
    second = await dispatch(db, world, record.id)
    assert second.outcome == "proceed" and second.attempt is not None and second.message is not None
    assert second.attempt.attempt_number == 2 and second.attempt.fencing_token == 2
    assert second.message.rfc_message_id.startswith(f"<inquiry-{record.id}.2@")
    # A failure after the hand-over is never retried.
    await report(db, world, second, uncertain(second))
    with pytest.raises(VersionConflict):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.retry(conn, actor, record.id)


async def test_definite_rejection_is_not_retried(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None and result.message is not None
    rejected = SendDefiniteFailure(
        provider=result.attempt.provider,
        inquiry_id=record.id,
        attempt_id=result.attempt.attempt_id,
        rfc_message_id=result.message.rfc_message_id,
        raw_sha256=result.message.raw_sha256,
        pre_submission=False,
        reason=SendFailureReason.PROVIDER_REJECTED_INVALID,
        retryable=False,
    )
    outcome = await report(db, world, result, rejected)
    assert outcome.inquiry_state == InquiryState.FAILED_DEFINITE
    actor = system(world.workspace_id)
    with pytest.raises(EmailDeliveryUncertain):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.retry(conn, actor, record.id)


async def test_price_change_cancels_the_stale_queued_inquiry(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    record = await reserve_and_queue(db, world)
    # The listing is re-observed with another asking price (a new promoted revision).
    _, gen, obs = seed.detail_observation(ws, world.listing_id, promoted=True)
    revision = seed.revision(
        ws,
        world.listing_id,
        2,
        detail_generation=gen,
        observation_id=obs,
        semantic_hash=sha(unique("s")),
        asking_minor=259000,
    )
    seed.promote(ws, world.listing_id, revision, gen, obs)
    stale = await dispatch(db, world, record.id)
    assert stale.outcome == "cancelled" and stale.attempt is None
    assert await _state(db, world, record.id) == "cancelled"
    assert await _debits(db, world) == 0 and await _attempts(db, world) == 0


async def test_availability_change_cancels_pending_inquiries(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    record = await reserve_and_queue(db, world)
    seed.conn.execute(
        "update app.listings set availability = 'sold_claimed' where workspace_id = %s and id = %s",
        (ws, world.listing_id),
    )
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        cancelled = await inquiries_repo.cancel_stale_inquiries(conn, actor, listing_id=world.listing_id)
    assert cancelled == (record.id,)
    assert await _state(db, world, record.id) == "cancelled"
    assert await _debits(db, world) == 0
    held = await dispatch(db, world, record.id)
    assert held.outcome == "hold" and held.attempt is None


async def test_reconciliation_pass_blocks_the_send_job_and_holds_the_attempt(
    db: Database, db_url: str, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None
    job_id = seed.job(
        ws,
        job_type=JobType.SELLER_INQUIRY_SEND.value,
        state="running",
        attempts=1,
        lease_owner="b1a-crashed-worker",
        lease_token=uuid.uuid4(),
        lease_expires_at=now_utc() - timedelta(seconds=5),
    )
    _expire(seed.conn, result.attempt.attempt_id)
    reconciler = Reconciler(
        RuntimeContext(settings=pipeline_settings(db_url), db=db), ReconcileOptions(job_retry_delay_seconds=0)
    )
    dry = await reconciler.reconcile_workspace(ws, dry_run=True)
    assert dry.jobs_blocked_uncertain == 1 and dry.send_attempts_uncertain == 1 and dry.jobs_requeued == 0
    assert await _state(db, world, record.id) == "sending"

    applied = await reconciler.reconcile_workspace(ws)
    # (This synthetic workspace has no business configuration: the valuation sweep is not ours.)
    assert [e for e in applied.errors if not e.startswith("valuations:")] == [], applied.errors
    assert applied.jobs_blocked_uncertain == 1 and applied.send_attempts_uncertain == 1
    assert applied.jobs_requeued == 0 and applied.jobs_dead_lettered == 0
    assert await _state(db, world, record.id) == "uncertain"
    blocked = await scalar(
        db, world, "select state || ':' || blocker_code from ops.jobs where id = %(id)s", {"id": job_id}
    )
    assert blocked == "blocked:EMAIL_DELIVERY_UNCERTAIN"
    again = await reconciler.reconcile_workspace(ws)
    assert again.jobs_blocked_uncertain == 0 and again.send_attempts_uncertain == 0
    assert await _attempts(db, world) == 1
