"""Regressions found by the independent B1a review (spec 37.3, 37.5, 37.10).

1. A ``queued`` inquiry re-queued after a proven pre-submission failure could not be cancelled or
   suppressed: dispatch answered ``cancelled``/``suppressed`` while the row stayed ``queued``,
   and suppressions, a revoked authorization, merges and stale-fact cancellation skipped it.
2. The Outlook claim (the revalidation right before ``.Send``) looked at the BOUND seller entity
   only: after a seller merge it missed the survivor's opt-out, the merged family's (possibly)
   transmitted inquiry about the same vehicle and the seller cooldown.
3. A send outcome naming another Message-ID than the attempt was recorded as this attempt's
   acceptance; a reported acceptance time could predate the send attempt.
4. The rolling caps counted an Outlook intent at its commit, not at the hand-over (up to the intent
   TTL later): intents committed while the worker was offline could leave together with later
   ones, above 2 per rolling 24 hours. A cap refusal at the claim also closed the pair's one
   inquiry for good instead of waiting.
5. Three appearances of one dealer's car on three sites with different addresses, linked only by
   a plausible (unreviewed) cluster, reserve exactly once.
6. Recording "not eligible" again for an identity whose record is already cancelled raised
   (its readiness columns are frozen) instead of keeping the cancellation.
7. The desktop worker's refusal of an intent whose validity had run out (recorded as uncertain:
   a late proof is a reconciliation) stayed uncertain until the worker repeated its report.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.v11_inquiries.support import (
    World,
    accepted,
    add_vehicle,
    alias,
    backdate,
    confirm_cluster,
    dispatch,
    inquiry,
    now_utc,
    owner,
    qualify,
    readiness,
    refused_before_submit,
    report,
    reserve_and_queue,
    reserve_existing,
    scalar,
    send,
    system,
)
from tests.integration.v11_inquiries.test_send_intents import _claim, _intent, _report, _send_report, _worker

from suv_deals.domain.enums import InquiryReadiness, InquiryState, SuppressionReason
from suv_deals.domain.inquiries import SendAttemptOutcome
from suv_deals.errors import AppError, ValidationFailed
from suv_deals.integrations.email_providers.outlook_local import OutlookRefusalReason, OutlookSubmissionState
from suv_deals.persistence import inquiries_repo, sellers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _count(db: Database, world: World, query: str, **params: object) -> int:
    return int(await scalar(db, world, query, dict(params)))


async def _state(db: Database, world: World, inquiry_id: UUID) -> str:
    return str(
        await scalar(
            db,
            world,
            "select state || coalesce(':' || suppression_reason, '') from app.seller_inquiries"
            " where id = %(id)s",
            {"id": inquiry_id},
        )
    )


async def _debits(db: Database, world: World) -> int:
    return await _count(
        db,
        world,
        "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s and released_at is null",
    )


async def _attempts(db: Database, world: World) -> int:
    return await _count(
        db, world, "select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s"
    )


async def _requeued_after_proven_failure(db: Database, world: World) -> UUID:
    """Reserve, queue, dispatch, prove the attempt never left, retry: ``queued`` with 1 attempt."""
    record = await reserve_and_queue(db, world)
    first = await dispatch(db, world, record.id)
    assert first.outcome == "proceed"
    outcome = await report(db, world, first, refused_before_submit(first))
    assert outcome.inquiry_state == InquiryState.FAILED_DEFINITE
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        retried = await inquiries_repo.retry(conn, actor, record.id)
    assert retried.state == InquiryState.QUEUED and retried.send_attempted_at is not None
    return record.id


def _new_price(seed: Seed, world: World, price_minor: int = 259000) -> None:
    _, gen, obs = seed.detail_observation(world.workspace_id, world.listing_id, promoted=True)
    revision = seed.revision(
        world.workspace_id,
        world.listing_id,
        2,
        detail_generation=gen,
        observation_id=obs,
        semantic_hash=sha(unique("semantic")),
        asking_minor=price_minor,
    )
    seed.promote(world.workspace_id, world.listing_id, revision, gen, obs)


async def _controls_version(db: Database, world: World) -> int:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        controls = await inquiries_repo.get_controls(conn, actor)
    assert controls is not None
    return controls.version


# --- 1. re-queued inquiries are really closed (or held) ------------------------------------------


async def test_requeued_inquiry_with_stale_facts_is_really_cancelled(
    db: Database, seed: Seed, world: World
) -> None:
    inquiry_id = await _requeued_after_proven_failure(db, world)
    _new_price(seed, world)
    stale = await dispatch(db, world, inquiry_id)
    assert stale.outcome == "cancelled" and stale.attempt is None
    assert "PRICE_CHANGED" in stale.decision.reasons
    assert await _state(db, world, inquiry_id) == "cancelled"  # was left "queued" before the fix
    # The attempted inquiry keeps its debit (the ledger guard), and nothing else was attempted.
    assert await _debits(db, world) == 1 and await _attempts(db, world) == 1
    again = await dispatch(db, world, inquiry_id)
    assert again.outcome == "hold" and again.decision.reasons == ("NOT_QUEUED",)
    # A once-attempted inquiry is never re-qualified (one inquiry per pair, database rule).
    actor = system(world.workspace_id)
    with pytest.raises(AppError):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.requalify(conn, actor, inquiry_id, reason="try again (synthetic)")


async def test_kill_switch_holds_a_requeued_inquiry_until_resume(db: Database, world: World) -> None:
    inquiry_id = await _requeued_after_proven_failure(db, world)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.pause(
            conn, boss, expected_version=await _controls_version(db, world), reason="pause (synthetic)"
        )
    held = await dispatch(db, world, inquiry_id)
    assert held.outcome == "hold" and held.attempt is None
    assert "KILL_SWITCH_ACTIVE" in held.decision.reasons
    assert await _state(db, world, inquiry_id) == "queued"  # not closed for good by a pause
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.resume(
            conn, boss, expected_version=await _controls_version(db, world), reason="resume (synthetic)"
        )
    second = await dispatch(db, world, inquiry_id)
    assert second.outcome == "proceed" and second.attempt is not None
    assert second.attempt.attempt_number == 2


async def test_suppression_and_cancel_reach_a_requeued_inquiry(
    db: Database, seed: Seed, world: World
) -> None:
    inquiry_id = await _requeued_after_proven_failure(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        added = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(world.seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
        )
    assert added.suppressed_inquiry_ids == (inquiry_id,)
    assert await _state(db, world, inquiry_id) == "suppressed:seller_opt_out"
    assert await _debits(db, world) == 1  # retained: the inquiry had an attempt
    assert (await dispatch(db, world, inquiry_id)).outcome == "hold"

    other = world.with_vehicle(await add_vehicle(db, seed, world.workspace_id))
    second = await _requeued_after_proven_failure(db, other)
    async with unit_of_work(db, actor) as conn:
        cancelled = await inquiries_repo.cancel_inquiry(conn, actor, second, reasons=["OWNER_CANCELLED"])
    assert cancelled.state == InquiryState.CANCELLED


async def test_merge_cancels_the_absorbed_sellers_requeued_inquiry(
    db: Database, seed: Seed, world: World
) -> None:
    absorbed = world.with_vehicle(await add_vehicle(db, seed, world.workspace_id))
    inquiry_id = await _requeued_after_proven_failure(db, absorbed)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        outcome = await sellers_repo.merge_sellers(
            conn,
            actor,
            survivor_id=world.seller_entity_id,
            absorbed_id=absorbed.seller_entity_id,
            reason="same dealer (synthetic)",
        )
    # Its seller can never dispatch again (database identity guard): it is closed, not left queued.
    assert outcome.cancelled_inquiry_ids == (inquiry_id,)
    assert await _state(db, world, inquiry_id) == "cancelled"


# --- 2. the claim re-checks the merged seller family ---------------------------------------------


async def test_claim_rechecks_the_merged_seller_family(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    worker = await _worker(db, world)
    other = await add_vehicle(db, seed, ws)
    contacted = await reserve_and_queue(db, world.with_vehicle(other))
    sent = await dispatch(db, world, contacted.id)
    await report(db, world, sent, accepted(sent))
    _, intent = await _intent(db, world, worker)  # committed, not yet claimed by the desktop
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        await sellers_repo.merge_sellers(
            conn,
            actor,
            survivor_id=other.seller_entity_id,
            absorbed_id=world.seller_entity_id,
            reason="same dealer: identical VAT id (synthetic)",
        )
    # The merged family was contacted moments ago: not now (retryable refusal, nothing closed).
    cooling = await _claim(db, worker, intent.intent_id)
    assert not cooling.proceed and cooling.refusal_reason == OutlookRefusalReason.KILL_SWITCH
    assert cooling.detail == "SELLER_COOLDOWN"
    # It is even the same car: this message must never leave.
    confirm_cluster(seed, ws, [world.listing_id, other.listing_id])
    duplicate = await _claim(db, worker, intent.intent_id)
    assert not duplicate.proceed and duplicate.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert duplicate.detail == "DUPLICATE_INQUIRY"
    # An opt-out recorded under the SURVIVING entity is honoured for the absorbed entity's intent.
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(other.seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
        )
    opted_out = await _claim(db, worker, intent.intent_id)
    assert not opted_out.proceed and opted_out.detail == "SUPPRESSED"
    assert await _attempts(db, world) == 2  # the committed intent; nothing was resent


# --- 3. outcomes belong to exactly the committed message -----------------------------------------


async def test_outcome_naming_another_message_is_refused(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    foreign = accepted(result).model_copy(update={"rfc_message_id": "<another-message@example.invalid>"})
    with pytest.raises(ValidationFailed):
        await report(db, world, result, foreign)
    assert await _state(db, world, record.id) == "sending"
    backdated = accepted(result).model_copy(update={"accepted_at": now_utc() - timedelta(days=30)})
    outcome = await report(db, world, result, backdated)
    assert outcome.inquiry_state == InquiryState.ACCEPTED
    stored = await inquiry(db, world, record.id)
    assert stored.accepted_at is not None and stored.send_attempted_at is not None
    assert stored.accepted_at >= stored.send_attempted_at


# --- 4. rolling caps count the hand-over ----------------------------------------------------------


def _handed_over(conn: psycopg.Connection, inquiry_id: UUID, *, committed: datetime, at: datetime) -> None:
    """TEST ARRANGEMENT ONLY: committed at ``committed``, handed over (attempt finished) at ``at``."""
    backdate(conn, inquiry_id, to=committed)
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts set finished_at = %(at)s where inquiry_id = %(id)s",
            {"id": inquiry_id, "at": at},
        )


async def test_rolling_cap_counts_the_hand_over_not_the_commit(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    backlog = await reserve_and_queue(db, world)
    backdate(seed.conn, backlog.id, to=now_utc() - timedelta(days=3))
    committed = now_utc() - timedelta(hours=25)
    handed_over = now_utc() - timedelta(hours=19)
    for _ in range(2):
        # Committed 25 h ago while the desktop worker was offline, handed over 19 h ago.
        record, _ = await send(db, world.with_vehicle(await add_vehicle(db, seed, ws)))
        _handed_over(seed.conn, record.id, committed=committed, at=handed_over)
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        usage = await inquiries_repo.quota_usage(conn, actor)
        window = await inquiries_repo.next_window_at(conn, actor)
    assert usage.count_24h == 2 and usage.count_15d == 3
    held = await dispatch(db, world, backlog.id)
    assert held.outcome == "hold" and "RATE_CAP_REACHED" in held.decision.reasons
    expected = handed_over + timedelta(hours=24)
    assert held.next_attempt_at is not None and abs(held.next_attempt_at - expected) < timedelta(minutes=1)
    assert window.next_at is not None and abs(window.next_at - expected) < timedelta(minutes=1)
    assert await _state(db, world, backlog.id) == "queued"


async def test_claim_refused_by_a_reduced_cap_waits_instead_of_closing(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.set_limits(
            conn,
            boss,
            expected_version=await _controls_version(db, world),
            max_per_24h=0,
            max_per_15d=5,
            seller_cooldown=timedelta(days=7),
            reason="owner reduces the daily cap (synthetic)",
        )
    claim = await _claim(db, worker, intent.intent_id)
    assert not claim.proceed and claim.refusal_reason == OutlookRefusalReason.KILL_SWITCH
    assert claim.detail == "RATE_CAP_REACHED"
    refused = await _send_report(
        db,
        worker,
        _report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.KILL_SWITCH),
    )
    assert refused.inquiry_state == InquiryState.FAILED_DEFINITE
    assert refused.attempt_outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE  # retryable, not final
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        retried = await inquiries_repo.retry(conn, actor, inquiry_id)
    assert retried.state == InquiryState.QUEUED
    assert (await dispatch(db, world, inquiry_id)).outcome == "hold"  # waits for the window


# --- 5. three sites, one dealer, a plausible (unreviewed) same-car link ---------------------------


def _unreviewed_cluster(seed: Seed, workspace_id: UUID, listings: list[UUID]) -> UUID:
    cluster = seed.insert_id(
        "app.vehicle_clusters",
        workspace_id=workspace_id,
        confidence="medium",
        review_status="unreviewed",
        match_basis={"synthetic": "same photos, unconfirmed"},
    )
    for listing in listings:
        seed.insert(
            "app.vehicle_cluster_members",
            workspace_id=workspace_id,
            cluster_id=cluster,
            listing_id=listing,
            confidence="medium",
        )
    return cluster


async def test_three_sites_different_addresses_reserve_once(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    vat = alias("vat_id", "DE246813579", None, "same_vat_id")
    vehicles = [await add_vehicle(db, seed, ws, aliases=[vat]) for _ in range(3)]
    assert len({v.address for v in vehicles}) == 3  # a different relay/address on every site
    assert len({v.seller_entity_id for v in vehicles}) == 1
    _unreviewed_cluster(seed, ws, [v.listing_id for v in vehicles])
    decided = [await qualify(db, world.with_vehicle(v)) for v in vehicles]
    reserved = 0
    for vehicle, (record, decision, snapshot) in zip(vehicles, decided, strict=True):
        try:
            await reserve_existing(db, world.with_vehicle(vehicle), record, decision, snapshot)
            reserved += 1
        except AppError:
            pass
    assert reserved == 1 and await _debits(db, world) == 1


# --- 6. a periodic re-evaluation of a not-eligible identity is a no-op ----------------------------


async def test_not_eligible_again_keeps_the_cancelled_record(db: Database, world: World) -> None:
    actor = system(world.workspace_id)
    first_snapshot, first = await readiness(db, world)
    async with unit_of_work(db, actor) as conn:
        record = await inquiries_repo.open_inquiry(
            conn, actor, first_snapshot.identity, qualification_listing_id=world.listing_id
        )
        record = await inquiries_repo.record_readiness(
            conn, actor, record.id, first.model_copy(update={"readiness": InquiryReadiness.NOT_ELIGIBLE})
        )
    assert record.state == InquiryState.CANCELLED
    _, later = await readiness(db, world)  # a fresh evaluation (another time and rationale)
    async with unit_of_work(db, actor) as conn:
        again = await inquiries_repo.record_readiness(
            conn, actor, record.id, later.model_copy(update={"readiness": InquiryReadiness.NOT_ELIGIBLE})
        )
    assert again.state == InquiryState.CANCELLED and again.row_version == record.row_version
    # An eligible re-evaluation still re-qualifies the never-transmitted record.
    async with unit_of_work(db, actor) as conn:
        requalified = await inquiries_repo.record_readiness(conn, actor, record.id, later)
    assert requalified.state == InquiryState.QUALIFYING


# --- 7. the worker's refusal of an expired intent resolves on the first report -------------------


def _intent_expired(conn: psycopg.Connection, attempt_id: UUID) -> None:
    """TEST ARRANGEMENT ONLY: the intent's validity ran out before the worker could claim it."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts set send_intent_committed_at = now() - interval '2 hours',"
            " lease_expires_at = now() - interval '1 minute' where attempt_id = %s",
            (attempt_id,),
        )


async def test_refusal_of_an_expired_intent_is_reconciled_at_once(
    db: Database, seed: Seed, world: World
) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    _intent_expired(seed.conn, intent.intent_id)
    refused = await _send_report(
        db,
        worker,
        _report(
            intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.INTENT_EXPIRED
        ),
    )
    # Recorded as uncertain (a late proof), then reconciled from the worker's own refusal: before
    # the fix the inquiry stayed uncertain until the worker happened to repeat its report.
    assert refused.applied and refused.reconciled
    assert refused.inquiry_state == InquiryState.FAILED_DEFINITE
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        attempt = await inquiries_repo.get_attempt(conn, actor, intent.intent_id)
        retried = await inquiries_repo.retry(conn, actor, inquiry_id)
    assert attempt.outcome == SendAttemptOutcome.UNCERTAIN
    assert attempt.reconciled_outcome == "proven_not_submitted"
    assert retried.state == InquiryState.QUEUED
