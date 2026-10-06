"""Transactional outbox and delivery attempts on real PostgreSQL (spec 13, 22, 31 Outbox row)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import expire_event_lease, member, system

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import OutboxState, Role
from suv_deals.domain.notifications import FIXTURE_BLOCKER
from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import outbox
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors
from suv_deals.persistence.outbox import DeliveryOutcome

pytestmark = pytest.mark.db


def _payload(**extra: Any) -> dict[str, Any]:
    return {"schema_version": "1.0", "summary": "Synthetic research candidate.", **extra}


async def _event(
    conn: Conn,
    actor: ActorContext,
    *,
    dedup_key: str | None = None,
    aggregate_id: UUID | None = None,
    **kwargs: Any,
) -> tuple[UUID, bool]:
    values: dict[str, Any] = {
        "event_type": "review.pending",
        "event_version": 1,
        "aggregate_type": "review_case",
        "aggregate_id": aggregate_id or uuid.uuid4(),
        "aggregate_version": 1,
        "payload": _payload(),
        "dedup_key": dedup_key or unique("review.pending"),
    }
    values.update(kwargs)
    return await outbox.enqueue_event(conn, actor, **values)


def _state(seed: Seed, event_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select id, state, attempts, lease_token, blocker_code, last_error_code, send_attempted_at,"
        " provider_accepted_at, owner_seen_at, event_created_at, completed_at, payload_hash, is_fixture"
        " from ops.outbox where event_id = %s",
        (event_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_event_is_created_in_the_domain_transaction_and_rolls_back_with_it(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    dedup = unique("review.pending")

    class DomainFailure(Exception):
        pass

    with pytest.raises(DomainFailure):
        async with db.transaction(actor) as conn:
            event_id, created = await _event(conn, actor, dedup_key=dedup)
            assert created
            raise DomainFailure
    assert seed.scalar("select count(*) from ops.outbox where dedup_key = %s", (dedup,)) == 0
    async with db.transaction(actor) as conn:
        event_id, created = await _event(conn, actor, dedup_key=dedup)
    row = _state(seed, event_id)
    assert created and row["state"] == "pending" and row["attempts"] == 0
    assert row["event_created_at"] is not None and row["send_attempted_at"] is None
    assert len(row["payload_hash"]) == 64


async def test_duplicate_business_key_returns_the_existing_event(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    dedup = unique("review.pending")
    aggregate = uuid.uuid4()
    async with db.transaction(actor) as conn:
        first, created = await _event(conn, actor, dedup_key=dedup, aggregate_id=aggregate)
    async with db.transaction(actor) as conn:
        again, created_again = await _event(conn, actor, dedup_key=dedup, aggregate_id=aggregate)
    assert created and not created_again and again == first
    async with db.transaction(actor) as conn:
        with pytest.raises(IdempotencyConflict):
            await _event(conn, actor, dedup_key=dedup, aggregate_id=uuid.uuid4())
    assert seed.scalar("select count(*) from ops.outbox where dedup_key = %s", (dedup,)) == 1


async def test_payload_event_id_is_the_stable_event_id(db: Database, world_a: World) -> None:
    actor = system(world_a.workspace_id)
    event_id = uuid.uuid4()
    async with db.transaction(actor) as conn:
        stored, _ = await _event(conn, actor, payload=_payload(event_id=str(event_id)))
    assert stored == event_id
    async with db.transaction(actor) as conn:
        with pytest.raises(ValidationFailed):
            await _event(conn, actor, payload=_payload(event_id=str(uuid.uuid4())), event_id=uuid.uuid4())


async def test_payload_guard_and_scopes(db: Database, world_a: World) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        with pytest.raises(ValidationFailed):
            await _event(conn, actor, payload=_payload(contact="seller@example.com"))
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(viewer) as conn:
        with pytest.raises(Forbidden):
            await _event(conn, viewer)


async def test_fixture_events_are_blocked_and_never_claimed(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        fixture_id, _ = await _event(conn, actor, is_fixture=True, payload=_payload(fixture=True))
        with pytest.raises(ValidationFailed):
            await _event(conn, actor, payload=_payload(fixture=True))  # fixture payload, real flag
    row = _state(seed, fixture_id)
    assert row["state"] == "blocked" and row["blocker_code"] == FIXTURE_BLOCKER and row["is_fixture"]
    # A contaminated pending row (e.g. written outside this API) is refused at claim time.
    contaminated = seed.outbox(ws, payload={"schema_version": "1.0", "fixture": True})
    fixture_case = seed.review_case(ws, world_a.listing_id, world_a.revision_id, is_fixture=True)
    claimed = await outbox.claim_events(db, ws, "dispatcher-1", 60, 10)
    assert claimed == []
    assert seed.scalar("select state from ops.outbox where id = %s", (contaminated,)) == "blocked"
    assert seed.scalar("select blocker_code from ops.outbox where id = %s", (contaminated,)) == FIXTURE_BLOCKER
    # A non-fixture event for a fixture review case cannot even commit (deferred lineage check).
    with pytest.raises(ValidationFailed):
        async with mapped_errors(), db.transaction(actor) as conn:
            await _event(conn, actor, aggregate_id=fixture_case)
    # The fixture row can never be flipped to deliverable.
    with pytest.raises(Exception, match="SV004|immutable"):
        seed.conn.execute("update ops.outbox set is_fixture = false where event_id = %s", (fixture_id,))


async def test_claim_leases_due_events_with_fresh_tokens(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        ids = [(await _event(conn, actor))[0] for _ in range(3)]
        future = seed.scalar("select now() + interval '1 hour'")
        later, _ = await _event(conn, actor, available_at=future)
    first = await outbox.claim_events(db, ws, "dispatcher-a", 60, 2)
    second = await outbox.claim_events(db, ws, "dispatcher-b", 60, 10)
    assert len(first) == 2 and len(second) == 1
    assert {e.event_id for e in first + second} == set(ids) and later not in {e.event_id for e in second}
    assert len({e.lease_token for e in first + second}) == 3
    assert all(e.state == OutboxState.SENDING and e.attempts == 1 for e in first + second)
    assert await outbox.claim_events(db, ws, "dispatcher-c", 60, 10) == []


async def test_successful_delivery_records_receipt_and_separate_timestamps(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor)
    [event] = await outbox.claim_events(db, ws, "dispatcher-1", 60, 1)
    async with db.transaction(actor) as conn:
        started = await outbox.begin_send(conn, event)
    attempt_id = uuid.uuid4()
    async with db.transaction(actor) as conn:
        await outbox.record_attempt(
            conn, event, attempt_id, DeliveryOutcome.ACCEPTED, "receipt-123", 202, provider="mcp_events"
        )
        await outbox.mark_delivered(conn, event)
    row = _state(seed, event_id)
    assert row["state"] == "delivered" and row["completed_at"] is not None
    assert row["send_attempted_at"] == started
    assert row["provider_accepted_at"] >= row["send_attempted_at"] >= row["event_created_at"]
    assert row["owner_seen_at"] is None  # never inferred from delivery
    attempt = seed.conn.execute(
        "select attempt_number, external_receipt, response_code, uncertain, sent_at"
        " from ops.delivery_attempts where attempt_id = %s",
        (attempt_id,),
    ).fetchone()
    assert attempt == (1, "receipt-123", 202, False, started)
    # Owner-seen needs explicit trustworthy evidence from a system process.
    owner = member(ws, Role.OWNER)
    async with db.transaction(owner) as conn:
        with pytest.raises(Forbidden):
            await outbox.mark_owner_seen(
                conn, owner, event_id, evidence_source="provider_read_receipt", seen_at=started
            )
    async with db.transaction(actor) as conn:
        await outbox.mark_owner_seen(
            conn, actor, event_id, evidence_source="provider_read_receipt", seen_at=started + timedelta(seconds=1)
        )
    assert _state(seed, event_id)["owner_seen_at"] is not None


async def test_timeout_after_acceptance_stays_uncertain_and_is_not_resent(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor)
    [event] = await outbox.claim_events(db, ws, "dispatcher-1", 60, 1)
    async with db.transaction(actor) as conn:
        await outbox.begin_send(conn, event)
    # ... the provider call times out after the request was sent ...
    async with db.transaction(actor) as conn:
        with pytest.raises(ValidationFailed):
            await outbox.record_attempt(
                conn, event, uuid.uuid4(), DeliveryOutcome.UNCERTAIN, "r", None, "TIMEOUT", provider="slack"
            )
    async with db.transaction(actor) as conn:
        await outbox.record_attempt(
            conn,
            event,
            uuid.uuid4(),
            DeliveryOutcome.UNCERTAIN,
            None,
            None,
            "TIMEOUT_AFTER_SEND",
            provider="slack",
            error_detail="read timeout after 30s",
        )
        await outbox.mark_uncertain(conn, event, "TIMEOUT_AFTER_SEND")
    row = _state(seed, event_id)
    assert row["state"] == "uncertain" and row["lease_token"] is None
    # No blind resend: not claimable, not reaped, but visible.
    assert await outbox.claim_events(db, ws, "dispatcher-2", 60, 10) == []
    assert (await outbox.reap_expired_events(db, ws)).uncertain == ()
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(viewer) as conn:
        stats = await outbox.outbox_stats(conn, viewer)
        attention = await outbox.list_attention(conn, viewer)
    assert stats.uncertain == 1 and stats.counts[OutboxState.UNCERTAIN] == 1
    assert [e.event_id for e in attention] == [event_id]
    # Cancelling an uncertain event would hide a possible delivery.
    async with db.transaction(actor) as conn:
        with pytest.raises(VersionConflict):
            await outbox.cancel_stale(conn, actor, event_id, "stale_case_version")
    # Reconciliation from provider evidence (message lookup) resolves it.
    reviewer = member(ws, Role.REVIEWER)
    async with db.transaction(reviewer) as conn:
        with pytest.raises(Forbidden):
            await outbox.reconcile_uncertain(conn, reviewer, event_id, provider="slack", accepted=True)
    async with db.transaction(actor) as conn:
        state = await outbox.reconcile_uncertain(
            conn, actor, event_id, provider="slack", accepted=True, receipt="ts-1700000000.000100"
        )
    assert state == OutboxState.DELIVERED
    rows = seed.conn.execute(
        "select attempt_number, uncertain, external_receipt, error_code from ops.delivery_attempts"
        " where outbox_id = %s order by attempt_number",
        (row["id"],),
    ).fetchall()
    assert rows == [
        (1, True, None, "TIMEOUT_AFTER_SEND"),
        (2, False, "ts-1700000000.000100", "RECONCILED_ACCEPTED"),
    ]


async def test_reconciliation_without_delivery_allows_a_retry(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor)
    [event] = await outbox.claim_events(db, ws, "d", 60, 1)
    async with db.transaction(actor) as conn:
        await outbox.mark_uncertain(conn, event, "TIMEOUT_AFTER_SEND")
    async with db.transaction(actor) as conn:
        state = await outbox.reconcile_uncertain(
            conn, actor, event_id, provider="mcp_events", accepted=False, note="lookup: not received"
        )
    assert state == OutboxState.RETRY_WAIT
    [again] = await outbox.claim_events(db, ws, "d", 60, 1)
    assert again.event_id == event_id and again.attempts == 2


async def test_dispatcher_crash_after_send_becomes_uncertain_before_send_retries(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        sent_id, _ = await _event(conn, actor)
        unsent_id, _ = await _event(conn, actor)
    claimed = {e.event_id: e for e in await outbox.claim_events(db, ws, "dispatcher-dies", 60, 10)}
    async with db.transaction(actor) as conn:
        await outbox.begin_send(conn, claimed[sent_id])
    for event in claimed.values():
        expire_event_lease(seed, event.id)
    reaped = await outbox.reap_expired_events(db, ws, retry_delay_seconds=0)
    assert reaped.uncertain == (sent_id,) and reaped.retry == (unsent_id,)
    assert _state(seed, sent_id)["last_error_code"] == outbox.LOST_AFTER_SEND
    [retry] = await outbox.claim_events(db, ws, "dispatcher-2", 60, 10)
    assert retry.event_id == unsent_id and retry.attempts == 2
    # The previous lease's send marker does not make the new lease look "sent".
    expire_event_lease(seed, retry.id)
    assert (await outbox.reap_expired_events(db, ws, retry_delay_seconds=0)).retry == (unsent_id,)


async def test_lifecycle_updates_are_fenced_on_the_lease(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor)
    [old] = await outbox.claim_events(db, ws, "dispatcher-old", 60, 1)
    expire_event_lease(seed, old.id)
    await outbox.reap_expired_events(db, ws, retry_delay_seconds=0)
    [new] = await outbox.claim_events(db, ws, "dispatcher-new", 60, 1)
    assert new.event_id == event_id and new.lease_token != old.lease_token
    for action in (
        lambda c: outbox.begin_send(c, old),
        lambda c: outbox.mark_delivered(c, old),
        lambda c: outbox.mark_uncertain(c, old, "TIMEOUT"),
        lambda c: outbox.mark_retry(c, old, None, "HTTP_503"),
        lambda c: outbox.mark_blocked(c, old, "binding_disabled"),
        lambda c: outbox.mark_dead_letter(c, old, "HTTP_410"),
        lambda c: outbox.record_attempt(
            c, old, uuid.uuid4(), DeliveryOutcome.ACCEPTED, "x", 200, provider="slack"
        ),
    ):
        with pytest.raises(LeaseLost):
            async with db.transaction(actor) as conn:
                await action(conn)
    async with db.transaction(actor) as conn:
        await outbox.mark_delivered(conn, new)
    assert _state(seed, event_id)["state"] == "delivered"


async def test_retries_end_in_dead_letter_and_terminal_errors_stay_visible(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor, max_attempts=2)
        terminal_id, _ = await _event(conn, actor)
    for expected in (OutboxState.RETRY_WAIT, OutboxState.DEAD_LETTER):
        claimed = {e.event_id: e for e in await outbox.claim_events(db, ws, "d", 60, 10)}
        async with db.transaction(actor) as conn:
            await outbox.record_attempt(
                conn, claimed[event_id], uuid.uuid4(), DeliveryOutcome.RETRYABLE, None, 503, "HTTP_503",
                provider="mcp_events",
            )
            assert await outbox.mark_retry(conn, claimed[event_id], timedelta(0), "HTTP_503") == expected
            if terminal_id in claimed:
                await outbox.mark_dead_letter(conn, claimed[terminal_id], "HTTP_410")
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(viewer) as conn:
        stats = await outbox.outbox_stats(conn, viewer)
    assert stats.dead_letter == 2
    assert _state(seed, event_id)["completed_at"] is not None


async def test_stale_events_are_cancelled_with_an_audit_record(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        pending_id, _ = await _event(conn, actor)
        leased_id, _ = await _event(conn, actor)
    async with db.transaction(actor) as conn:
        await outbox.cancel_stale(conn, actor, pending_id, "stale_case_version")
    [leased] = await outbox.claim_events(db, ws, "d", 60, 10)
    assert leased.event_id == leased_id
    async with db.transaction(actor) as conn:
        await outbox.cancel_stale(conn, actor, leased_id, "stale_case_version", lease=leased)
    assert _state(seed, pending_id)["state"] == "cancelled"
    assert _state(seed, leased_id)["state"] == "cancelled"
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s and action = 'outbox.cancel_stale'",
            (ws,),
        )
        == 2
    )
    async with db.transaction(actor) as conn:
        with pytest.raises(NotFound):
            await outbox.cancel_stale(conn, actor, uuid.uuid4(), "stale_case_version")


async def test_exhausted_waiting_events_are_dead_lettered(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    exhausted = seed.outbox(ws, state="retry_wait", attempts=3, max_attempts=3)
    event_id = seed.scalar("select event_id from ops.outbox where id = %s", (exhausted,))
    reaped = await outbox.reap_expired_events(db, ws)
    assert reaped.exhausted == (event_id,)
    assert seed.scalar("select state from ops.outbox where id = %s", (exhausted,)) == "dead_letter"
