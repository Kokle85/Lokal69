"""DB-backed host budget gate on real PostgreSQL (spec 9 adaptive backoff, 30 budgets).

Several gate instances stand in for several worker processes sharing ``ops.host_budgets``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import member

from suv_deals.adapters.base import FetchOutcome, FetchPurpose
from suv_deals.crawling.policy_client import BudgetGate, BudgetRequest
from suv_deals.crawling.rate_limits import Allow, BackoffPolicy, Deny, DenyReason, Wait, WaitReason
from suv_deals.domain.enums import AccessState, Role
from suv_deals.domain.sources import RateBudget
from suv_deals.errors import Forbidden
from suv_deals.persistence.budgets import ACCESS_BLOCK_SENTINEL, DbBudgetGate
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

HOST = "dealer-a.synthetic.example"
OTHER_HOST = "cdn.dealer-a.synthetic.example"
SOURCE = "fixture_dealer_de"
BUDGET = RateBudget(min_delay_seconds=5, max_search_pages_per_run=20, daily_request_budget=100)


def _gate(db: Database, world: World, **kwargs: Any) -> DbBudgetGate:
    values: dict[str, Any] = {"source_hosts": {SOURCE: [HOST, OTHER_HOST]}}
    values.update(kwargs)
    budgets = values.pop("budgets", {SOURCE: BUDGET})
    return DbBudgetGate(db, world.workspace_id, budgets, **values)


def _request(host: str = HOST, purpose: FetchPurpose = "search", source: str = SOURCE) -> BudgetRequest:
    return BudgetRequest(source_key=source, host=host, purpose=purpose, url=f"https://{host}/search?page=1")


def _outcome(state: AccessState, **kwargs: Any) -> FetchOutcome:
    return FetchOutcome(
        requested_url=f"https://{HOST}/search?page=1",
        success=state == AccessState.OK,
        access_state=state,
        bytes=kwargs.pop("bytes", 2048),
        fetched_at=datetime.now(UTC),
        **kwargs,
    )


def _row(seed: Seed, workspace_id: UUID, host: str = HOST) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select tokens, refilled_at, next_request_not_before, circuit_state, open_until,"
        " consecutive_failures, requests_today, bytes_today, retry_after_until, row_version"
        " from ops.host_budgets where workspace_id = %s and host = %s",
        (workspace_id, host),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _age(seed: Seed, workspace_id: UUID, seconds: int, host: str = HOST) -> None:
    """Simulate time passing for the bucket and any lease (keeps their relation intact)."""
    seed.conn.execute(
        "update ops.host_budgets set refilled_at = refilled_at - make_interval(secs => %s),"
        " next_request_not_before = next_request_not_before - make_interval(secs => %s)"
        " where workspace_id = %s and host = %s",
        (seconds, seconds, workspace_id, host),
    )


async def test_gate_implements_the_budget_gate_protocol(db: Database, world_a: World) -> None:
    assert isinstance(_gate(db, world_a), BudgetGate)


async def test_one_navigation_at_a_time_per_host_across_workers(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    workers = [_gate(db, world_a) for _ in range(5)]
    requests = [_request() for _ in workers]
    decisions = await asyncio.gather(*(g.acquire(r) for g, r in zip(workers, requests, strict=True)))
    allowed = [i for i, d in enumerate(decisions) if isinstance(d, Allow)]
    assert len(allowed) == 1, decisions
    waits = [d for d in decisions if not isinstance(d, Allow)]
    assert all(isinstance(d, Wait) and d.reason == WaitReason.NAVIGATION_IN_FLIGHT for d in waits)
    row = _row(seed, ws)
    assert row["next_request_not_before"] == row["refilled_at"] + timedelta(seconds=330)
    assert row["requests_today"] == 1
    holder, request = workers[allowed[0]], requests[allowed[0]]
    await holder.release(request, _outcome(AccessState.OK, bytes=4096))
    row = _row(seed, ws)
    assert row["next_request_not_before"] is None and row["bytes_today"] == 4096
    # The minimum delay still applies after the navigation finished (token bucket).
    after = await workers[0].acquire(_request())
    assert isinstance(after, Wait) and after.reason == WaitReason.MIN_DELAY
    _age(seed, ws, 10)
    again = _request()
    assert isinstance(await workers[1].acquire(again), Allow)
    await workers[1].release(again, None)  # abandoned (e.g. cancelled job): lease released
    assert _row(seed, ws)["next_request_not_before"] is None


async def test_a_stale_holder_never_clears_a_newer_lease(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    stale, fresh, third = _gate(db, world_a), _gate(db, world_a), _gate(db, world_a)
    stale_request, fresh_request = _request(), _request()
    assert isinstance(await stale.acquire(stale_request), Allow)
    _age(seed, ws, 400)  # the stale worker hung past its 330 s lease
    assert isinstance(await fresh.acquire(fresh_request), Allow)
    await stale.release(stale_request, _outcome(AccessState.OK))
    blocked = await third.acquire(_request())
    assert isinstance(blocked, Wait) and blocked.reason == WaitReason.NAVIGATION_IN_FLIGHT
    await fresh.release(fresh_request, _outcome(AccessState.OK))
    assert _row(seed, ws)["next_request_not_before"] is None


async def test_access_blocked_is_persisted_and_recommends_a_route_pause(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    gate = _gate(db, world_a)
    request = _request()
    assert isinstance(await gate.acquire(request), Allow)
    await gate.release(request, _outcome(AccessState.ACCESS_BLOCKED, http_status=403, error_code="captcha"))
    [pause] = gate.take_pause_recommendations()
    assert pause.reason == "access_blocked" and pause.host == HOST and pause.source_key == SOURCE
    assert pause.dedup_key == f"access_blocked:{SOURCE}:{HOST}" and pause.requires_explicit_action
    assert gate.take_pause_recommendations() == []
    result = gate.last_result(SOURCE, HOST)
    assert result is not None and result.plan.action.value == "stop"
    row = _row(seed, ws)
    assert row["circuit_state"] == "open" and row["open_until"] == ACCESS_BLOCK_SENTINEL
    _age(seed, ws, 3600)
    # Every worker (fresh process) sees the block; it never lifts on its own.
    denied = await _gate(db, world_a).acquire(_request())
    assert isinstance(denied, Deny) and denied.reason == DenyReason.ACCESS_BLOCKED and denied.until is None
    reviewer = member(ws, Role.REVIEWER)
    with pytest.raises(Forbidden):
        await gate.clear_access_block(reviewer, HOST, reason="looks fine")
    owner = member(ws, Role.OWNER)
    cleared = await gate.clear_access_block(owner, HOST, reason="permitted access restored by the provider")
    assert cleared.access_blocked_at is None
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s"
            " and action = 'host_budget.clear_access_block'",
            (ws,),
        )
        == 1
    )
    probe = await gate.acquire(_request())
    assert isinstance(probe, Allow) and probe.probe
    assert _row(seed, ws)["circuit_state"] == "half_open"


async def test_daily_budget_is_aggregated_across_the_sources_hosts(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    source_key = unique("budget_src").lower()
    seed.source(ws, source_key=source_key, allowed_hosts=[HOST, OTHER_HOST.upper()])
    tight = RateBudget(min_delay_seconds=5, max_search_pages_per_run=20, daily_request_budget=2)
    gate = DbBudgetGate(db, ws, {source_key: tight})  # hosts come from app.sources
    for host in (HOST, OTHER_HOST):
        request = _request(host, source=source_key)
        assert isinstance(await gate.acquire(request), Allow)
        await gate.release(request, _outcome(AccessState.OK, bytes=1000))
    usage = await gate.daily_usage(source_key)
    assert (usage.requests, usage.bytes) == (2, 2000)
    _age(seed, ws, 60)
    _age(seed, ws, 60, OTHER_HOST)
    denied = await gate.acquire(_request(HOST, source=source_key))
    assert isinstance(denied, Deny) and denied.reason == DenyReason.BUDGET_EXHAUSTED
    assert denied.until is not None and denied.until.time().hour == 0


async def test_transient_failures_open_the_persisted_circuit(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    policy = BackoffPolicy(circuit_failure_threshold=1)
    gate = _gate(db, world_a, policy=policy)
    request = _request()
    assert isinstance(await gate.acquire(request), Allow)
    await gate.release(request, _outcome(AccessState.TRANSIENT_ERROR, http_status=503, error_code="http_503"))
    row = _row(seed, ws)
    assert row["circuit_state"] == "open" and row["open_until"] < ACCESS_BLOCK_SENTINEL
    assert row["consecutive_failures"] == 1
    denied = await _gate(db, world_a, policy=policy).acquire(_request())
    assert isinstance(denied, Deny) and denied.reason == DenyReason.CIRCUIT_OPEN


async def test_retry_after_is_persisted_and_never_shortened(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    gate = _gate(db, world_a)
    request = _request()
    assert isinstance(await gate.acquire(request), Allow)
    await gate.release(request, _outcome(AccessState.RATE_LIMITED, http_status=429, retry_after_seconds=7200))
    row = _row(seed, ws)
    assert row["retry_after_until"] is not None and row["circuit_state"] == "closed"
    _age(seed, ws, 600)
    waited = await _gate(db, world_a).acquire(_request())
    assert isinstance(waited, Wait) and waited.reason == WaitReason.RETRY_AFTER
    assert waited.until == row["retry_after_until"]


async def test_unknown_source_and_run_caps_are_refused(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    gate = _gate(db, world_a, budgets={SOURCE: RateBudget(min_delay_seconds=5, max_search_pages_per_run=1)})
    unknown = await gate.acquire(_request(source="unknown_source"))
    assert isinstance(unknown, Deny) and unknown.reason == DenyReason.BUDGET_EXHAUSTED
    first = _request()
    assert isinstance(await gate.acquire(first), Allow)
    await gate.release(first, _outcome(AccessState.OK))
    _age(seed, ws, 60)
    capped = await gate.acquire(_request())
    assert isinstance(capped, Deny) and capped.reason == DenyReason.RUN_CAP_REACHED
    gate.reset_run(SOURCE)
    assert isinstance(await gate.acquire(_request()), Allow)
