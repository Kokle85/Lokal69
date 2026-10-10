"""Regression tests for defects found in the independent WP7a review (real PostgreSQL).

Each test names the defect it pins down. Everything runs as ``suv_backend`` under RLS.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import expire_event_lease, member, system

from suv_deals.adapters.base import FetchOutcome
from suv_deals.crawling.policy_client import BudgetRequest
from suv_deals.crawling.rate_limits import Allow
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import AccessState, GateStatus, JobState, JobType, OutboxState, Role
from suv_deals.domain.notifications import FIXTURE_BLOCKER, FIXTURE_SUMMARY_PREFIX
from suv_deals.domain.sources import RateBudget
from suv_deals.errors import (
    AppError,
    DependencyUnavailable,
    ErrorCode,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    ValidationFailed,
)
from suv_deals.persistence import audit, gates, jobs, outbox
from suv_deals.persistence.budgets import DbBudgetGate
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.errors_map import (
    StatementTimeout,
    TransactionAborted,
    TransientConflict,
    map_db_error,
)
from suv_deals.persistence.outbox import DeliveryOutcome
from suv_deals.persistence.transactions import retry_transient

pytestmark = pytest.mark.db

SOURCE = "fixture_dealer_review"
BUDGET = RateBudget(min_delay_seconds=5, max_search_pages_per_run=20, daily_request_budget=100)


async def _event(
    conn: Conn, actor: ActorContext, payload: dict[str, Any], **kwargs: Any
) -> tuple[UUID, bool]:
    return await outbox.enqueue_event(
        conn,
        actor,
        event_type="review.pending",
        event_version=1,
        aggregate_type="review_case",
        aggregate_id=uuid.uuid4(),
        aggregate_version=1,
        payload={"schema_version": "1.0", **payload},
        dedup_key=unique("review.pending"),
        **kwargs,
    )


# --------------------------------------------------------------------------------------------
# Outbox: fixture detection must match domain.notifications exactly
# --------------------------------------------------------------------------------------------


async def test_fixture_marker_semantics_match_the_domain(db: Database, world_a: World, seed: Seed) -> None:
    """Defect: ``"fixture": null`` real events were blocked as FIXTURE_EVENT at claim time,
    while ``"fixture": 0`` and a ``[SYNTHETIC FIXTURE]`` summary were accepted as real events."""
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        real_id, created = await _event(conn, actor, {"fixture": None, "summary": "Synthetic candidate."})
        assert created
        for fixture_looking in (
            {"fixture": 0},
            {"fixture": "yes"},
            {"summary": f"{FIXTURE_SUMMARY_PREFIX} synthetic arithmetic test"},
            {"summary": f"  {FIXTURE_SUMMARY_PREFIX.lower()} padded"},
        ):
            with pytest.raises(ValidationFailed):
                await _event(conn, actor, fixture_looking)
    # Rows written outside the API with a fixture summary are refused at claim time.
    contaminated = seed.outbox(
        ws, payload={"schema_version": "1.0", "summary": f"{FIXTURE_SUMMARY_PREFIX} leaked row"}
    )
    claimed = await outbox.claim_events(db, ws, "dispatcher-1", 60, 10)
    assert [e.event_id for e in claimed] == [real_id]
    assert seed.scalar("select state from ops.outbox where id = %s", (contaminated,)) == "blocked"
    assert (
        seed.scalar("select blocker_code from ops.outbox where id = %s", (contaminated,)) == FIXTURE_BLOCKER
    )


async def test_the_claim_statement_itself_never_leases_a_fixture_looking_row(
    db: Database, world_a: World, seed: Seed
) -> None:
    """Defect: only the separate refusal statement looked at payload markers; a suspicious row it
    skipped (locked by another transaction at that instant) could still be leased by the claim
    statement right after. The claim now excludes such rows itself."""
    ws = world_a.workspace_id
    for payload in (
        {"schema_version": "1.0", "fixture": True},
        {"schema_version": "1.0", "summary": f"{FIXTURE_SUMMARY_PREFIX} leaked row"},
    ):
        seed.outbox(ws, payload=payload)
    async with db.transaction(workspace_id=ws) as conn:
        cur = await conn.execute(
            outbox._CLAIM_SQL,  # the claim alone, without the preceding refusal statement
            {
                "workspace_id": ws,
                "owner": "dispatcher-1",
                "lease": timedelta(seconds=60),
                "limit": 10,
                "fixture_prefix": FIXTURE_SUMMARY_PREFIX.upper(),
                "types": None,  # any event type (D1: the claim can be restricted to types)
            },
        )
        assert await cur.fetchall() == []


async def test_the_claim_can_be_restricted_to_event_types(db: Database, world_a: World, seed: Seed) -> None:
    """D1 item 4: the dispatcher leases the category signals first through the SAME claim
    (`claim_events(..., event_types=...)`) instead of a duplicate statement of its own; a payload
    without a ``summary`` (every signal) is leased, and an empty type list is refused."""
    ws = world_a.workspace_id
    review = seed.outbox(ws)
    signal = seed.outbox(
        ws,
        event_type="seller.reply.received",
        aggregate_type="seller_reply",
        dedup_key=unique("seller.reply.received"),
        payload={"schema_version": "1.0", "synthetic": True},
    )
    leased = await outbox.claim_events(db, ws, "dispatcher-1", 60, 10, event_types={"seller.reply.received"})
    assert [e.id for e in leased] == [signal]
    rest = await outbox.claim_events(db, ws, "dispatcher-1", 60, 10)
    assert [e.id for e in rest] == [review]
    with pytest.raises(ValidationFailed):
        await outbox.claim_events(db, ws, "dispatcher-1", 60, 10, event_types=())


async def test_a_fixture_event_cannot_swallow_a_real_events_dedup_key(db: Database, world_a: World) -> None:
    """Defect: a real event reusing a fixture event's dedup key got ``created=False`` and the
    blocked fixture's id back, so it was silently never delivered."""
    actor = system(world_a.workspace_id)
    dedup = unique("review.pending")
    aggregate = uuid.uuid4()

    async def enqueue(conn: Conn, *, fixture: bool) -> tuple[UUID, bool]:
        return await outbox.enqueue_event(
            conn,
            actor,
            event_type="review.pending",
            event_version=1,
            aggregate_type="review_case",
            aggregate_id=aggregate,
            aggregate_version=1,
            payload={"schema_version": "1.0", **({"fixture": True} if fixture else {})},
            dedup_key=dedup,
            is_fixture=fixture,
        )

    async with db.transaction(actor) as conn:
        _, created = await enqueue(conn, fixture=True)
        assert created
        with pytest.raises(IdempotencyConflict):
            await enqueue(conn, fixture=False)


async def test_record_attempt_cannot_backdate_a_send_before_the_lease(
    db: Database, world_a: World, seed: Seed
) -> None:
    """Defect: a caller-supplied ``sent_at`` older than the lease made the reaper believe no
    send had happened under this lease, so a possibly delivered event was blindly resent."""
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor, {"summary": "Synthetic candidate."})
    [event] = await outbox.claim_events(db, ws, "dispatcher-1", 60, 1)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
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
            sent_at=long_ago,
        )
    # The dispatcher dies before mark_uncertain; its lease expires.
    expire_event_lease(seed, event.id)
    reaped = await outbox.reap_expired_events(db, ws, retry_delay_seconds=0)
    assert reaped.uncertain == (event_id,) and reaped.retry == ()
    sent_at = seed.scalar("select sent_at from ops.delivery_attempts where outbox_id = %s", (event.id,))
    assert sent_at >= event.last_heartbeat_at


async def test_owner_seen_is_never_earlier_than_provider_acceptance(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        event_id, _ = await _event(conn, actor, {"summary": "Synthetic candidate."})
    [event] = await outbox.claim_events(db, ws, "dispatcher-1", 60, 1)
    async with db.transaction(actor) as conn:
        await outbox.mark_delivered(conn, event)
    async with db.transaction(actor) as conn:
        await outbox.mark_owner_seen(
            conn,
            actor,
            event_id,
            evidence_source="provider_read_receipt",
            seen_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    assert seed.scalar(
        "select owner_seen_at >= provider_accepted_at from ops.outbox where event_id = %s", (event_id,)
    )


async def test_concurrent_dispatchers_never_claim_the_same_event(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        expected = {(await _event(conn, actor, {"summary": "Synthetic candidate."}))[0] for _ in range(40)}

    async def dispatcher(name: str) -> list[UUID]:
        mine: list[UUID] = []
        while True:
            batch = await outbox.claim_events(db, ws, name, 120, 3)
            if not batch:
                return mine
            assert all(e.lease_owner == name and e.state == OutboxState.SENDING for e in batch)
            mine.extend(e.event_id for e in batch)

    claimed = await asyncio.gather(*(dispatcher(f"dispatcher-{i}") for i in range(5)))
    flat = [event_id for batch in claimed for event_id in batch]
    assert len(flat) == len(set(flat)), "an event was leased twice"
    assert set(flat) == expected
    assert seed.scalar(
        "select count(distinct lease_token) = count(*) from ops.outbox where workspace_id = %s", (ws,)
    )


# --------------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------------


async def test_unblock_grants_at_least_one_attempt(db: Database, world_a: World, seed: Seed) -> None:
    """Defect: unblocking a job that had used all attempts re-queued it with no attempt left, so
    it was never claimable and reconciliation dead-lettered it, defeating the operator action."""
    ws = world_a.workspace_id
    job_id = seed.job(ws, state="blocked", blocker_code="source_paused", attempts=1, max_attempts=1)
    owner = member(ws, Role.OWNER)
    async with db.transaction(owner) as conn:
        unblocked = await jobs.unblock(conn, owner, job_id, reason="source resumed by the owner")
    assert unblocked.state == JobState.QUEUED and unblocked.attempts < unblocked.max_attempts
    assert await jobs.reconcile_exhausted(db, ws) == ()
    job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 60)
    assert job is not None and job.id == job_id and job.attempts == 1


async def test_claim_lock_timeouts_are_transient_not_database_outages(
    db_url: str, world_a: World, seed: Seed
) -> None:
    """Defect: lock timeouts inside the module's own transactions surfaced as
    ``DependencyUnavailable("database unavailable")`` instead of a retryable conflict."""
    ws = world_a.workspace_id
    host = f"{unique('busy').replace('_', '-')}.synthetic.example"
    busy = Database(db_url, set_role="suv_backend", lock_timeout_ms=100)
    await busy.open()
    gate = DbBudgetGate(busy, ws, {SOURCE: BUDGET}, source_hosts={SOURCE: [host]})
    request = BudgetRequest(source_key=SOURCE, host=host, purpose="search", url=f"https://{host}/s")
    holder = await psycopg.AsyncConnection.connect(db_url)
    try:
        assert isinstance(await gate.acquire(request), Allow)
        await holder.execute("select id from ops.host_budgets where host = %s for update", (host,))
        with pytest.raises(TransientConflict):
            await gate.release(request, None)
    finally:
        await holder.rollback()
        await holder.close()
        await busy.close()


# --------------------------------------------------------------------------------------------
# Transactions and error mapping
# --------------------------------------------------------------------------------------------


async def test_retry_transient_never_reruns_an_ambiguous_failure() -> None:
    """Defect: every retryable AppError was retried, including a connection lost during COMMIT
    (the commit may have succeeded) and generic internal errors: a duplicate business effect."""
    calls: list[str] = []

    async def lost_at_commit() -> None:
        calls.append("x")
        raise DependencyUnavailable("database unavailable")

    with pytest.raises(DependencyUnavailable):
        await retry_transient(lost_at_commit, attempts=4, base_delay_seconds=0)
    assert len(calls) == 1

    calls.clear()

    async def internal() -> None:
        calls.append("x")
        raise AppError(ErrorCode.INTERNAL_ERROR, "Database operation failed")

    with pytest.raises(AppError):
        await retry_transient(internal, attempts=4, base_delay_seconds=0)
    assert len(calls) == 1

    calls.clear()

    async def slow_then_ok() -> str:
        calls.append("x")
        if len(calls) == 1:
            raise StatementTimeout()
        return "ok"

    assert await retry_transient(slow_then_ok, attempts=2, base_delay_seconds=0) == "ok"


def test_statement_after_a_swallowed_error_maps_to_transaction_aborted(db_conn: psycopg.Connection) -> None:
    with db_conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.Error):
            db_conn.execute("select 1/0")
        with pytest.raises(psycopg.Error) as excinfo:
            db_conn.execute("select 1")
    mapped = map_db_error(excinfo.value)
    assert isinstance(mapped, TransactionAborted) and not mapped.retryable


def test_statement_timeout_maps_to_a_retryable_dependency_error(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.Error) as excinfo:
        db_conn.execute("set statement_timeout = 50; select pg_sleep(1)")
    db_conn.execute("reset statement_timeout")
    mapped = map_db_error(excinfo.value)
    assert isinstance(mapped, StatementTimeout)
    assert mapped.code == ErrorCode.DEPENDENCY_UNAVAILABLE and mapped.retryable


# --------------------------------------------------------------------------------------------
# Audit and gates
# --------------------------------------------------------------------------------------------


async def test_audit_outcome_never_overwrites_caller_metadata(
    db: Database, world_a: World, seed: Seed
) -> None:
    """Defect: the audit outcome was written into ``metadata["outcome"]``, silently replacing
    the caller's own ``outcome`` (e.g. the review decision outcome)."""
    ws = world_a.workspace_id
    reviewer = member(ws, Role.REVIEWER)
    async with db.transaction(reviewer) as conn:
        audit_id = await audit.record(
            conn,
            reviewer,
            "review.submit",
            "review_case",
            uuid.uuid4(),
            metadata={"outcome": "shortlisted", "audit_outcome": "spoofed"},
            outcome="succeeded",
        )
    metadata = seed.scalar("select metadata from ops.audit_events where id = %s", (audit_id,))
    assert metadata["outcome"] == "shortlisted"
    assert metadata["audit_outcome"] == "succeeded"


async def test_only_the_owner_can_activate_a_gate(db: Database, world_a: World) -> None:
    """Activation is an owner decision (spec 32): a system process may record verification
    states, but never ``active``."""
    ws = world_a.workspace_id
    worker = system(ws)
    evidence = {"canary_event_id": str(uuid.uuid4())}
    async with db.transaction(worker) as conn:
        with pytest.raises(Forbidden):
            await gates.upsert_gate(
                conn, worker, "native_mcp_events", "dot support", "canary", GateStatus.ACTIVE, evidence
            )
        verified = await gates.upsert_gate(
            conn, worker, "native_mcp_events", "dot support", "canary", GateStatus.LIVE_VERIFIED, evidence
        )
    assert verified.status == GateStatus.LIVE_VERIFIED
    owner = member(ws, Role.OWNER)
    async with db.transaction(owner) as conn:
        active = await gates.upsert_gate(
            conn, owner, "native_mcp_events", "dot support", "canary", GateStatus.ACTIVE, evidence
        )
    assert active.status == GateStatus.ACTIVE and active.row_version == 2


# --------------------------------------------------------------------------------------------
# Budgets: workspace isolation
# --------------------------------------------------------------------------------------------


async def test_budget_gate_state_is_isolated_per_workspace(
    db: Database, world_a: World, world_b: World, seed: Seed
) -> None:
    host = f"{unique('iso').replace('_', '-')}.synthetic.example"
    gate_a = DbBudgetGate(db, world_a.workspace_id, {SOURCE: BUDGET}, source_hosts={SOURCE: [host]})
    gate_b = DbBudgetGate(db, world_b.workspace_id, {SOURCE: BUDGET}, source_hosts={SOURCE: [host]})
    request_a = BudgetRequest(source_key=SOURCE, host=host, purpose="search", url=f"https://{host}/s")
    assert isinstance(await gate_a.acquire(request_a), Allow)
    await gate_a.release(
        request_a,
        FetchOutcome(
            requested_url=f"https://{host}/s",
            success=False,
            access_state=AccessState.ACCESS_BLOCKED,
            http_status=403,
            error_code="captcha",
            bytes=10,
            fetched_at=datetime.now(UTC),
        ),
    )
    # Workspace B neither sees nor inherits A's block, usage or lease.
    assert await gate_b.host_state(SOURCE, host) is None
    assert (await gate_b.daily_usage(SOURCE)).requests == 0
    request_b = BudgetRequest(source_key=SOURCE, host=host, purpose="search", url=f"https://{host}/s")
    assert isinstance(await gate_b.acquire(request_b), Allow)
    assert (await gate_a.daily_usage(SOURCE)).requests == 1
    assert (
        seed.scalar("select count(*) from ops.host_budgets where host = %s", (host,)) == 2
    )  # one row per workspace
    owner_b = member(world_b.workspace_id, Role.OWNER)
    with pytest.raises(NotFound):  # a gate bound to workspace A refuses B's actor
        await gate_a.clear_access_block(owner_b, host, reason="cross-workspace attempt")
