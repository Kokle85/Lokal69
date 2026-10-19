"""The budget gate stores its lease and access block in the real ``ops.host_budgets`` columns of
migration 20261006000950 (``in_flight_until``/``in_flight_token``, ``access_blocked_at``/``_reason``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_sources_listings.support import HOST, Env

from suv_deals.adapters.base import FetchOutcome
from suv_deals.crawling.policy_client import BudgetRequest
from suv_deals.crawling.rate_limits import Allow, Deny, DenyReason, Wait, WaitReason
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget
from suv_deals.persistence.budgets import DbBudgetGate
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

BUDGET = RateBudget(min_delay_seconds=5, max_search_pages_per_run=20, daily_request_budget=100)


def _gate(db: Database, env: Env) -> DbBudgetGate:
    return DbBudgetGate(db, env.workspace_id, {env.source_key: BUDGET}, source_hosts={env.source_key: [HOST]})


def _request(env: Env) -> BudgetRequest:
    return BudgetRequest(source_key=env.source_key, host=HOST, purpose="search", url=f"https://{HOST}/search")


def _outcome(state: AccessState, **kwargs: Any) -> FetchOutcome:
    return FetchOutcome(
        requested_url=f"https://{HOST}/search?page=1&token=abc",
        success=state == AccessState.OK,
        access_state=state,
        bytes=1024,
        fetched_at=datetime.now(UTC),
        **kwargs,
    )


def _row(seed: Seed, workspace_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select in_flight_until, in_flight_token, access_blocked_at, access_blocked_reason, circuit_state"
        " from ops.host_budgets where workspace_id = %s and host = %s",
        (workspace_id, HOST),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_navigation_lease_uses_token_columns(db: Database, env: Env, seed: Seed) -> None:
    gate, other = _gate(db, env), _gate(db, env)
    request = _request(env)
    assert isinstance(await gate.acquire(request), Allow)
    row = _row(seed, env.workspace_id)
    assert row["in_flight_until"] is not None and row["in_flight_token"] is not None
    waiting = await other.acquire(_request(env))
    assert isinstance(waiting, Wait) and waiting.reason == WaitReason.NAVIGATION_IN_FLIGHT
    await gate.release(request, _outcome(AccessState.OK))
    row = _row(seed, env.workspace_id)
    assert row["in_flight_until"] is None and row["in_flight_token"] is None


async def test_access_block_uses_real_columns_and_clears_with_a_foreign_lease(
    db: Database, env: Env, seed: Seed
) -> None:
    gate = _gate(db, env)
    request = _request(env)
    assert isinstance(await gate.acquire(request), Allow)
    await gate.release(request, _outcome(AccessState.ACCESS_BLOCKED, http_status=403, error_code="captcha"))
    row = _row(seed, env.workspace_id)
    assert row["access_blocked_at"] is not None and row["access_blocked_reason"]
    assert "token=abc" not in row["access_blocked_reason"]
    denied = await _gate(db, env).acquire(_request(env))
    assert isinstance(denied, Deny) and denied.reason == DenyReason.ACCESS_BLOCKED
    # A lease still recorded from before the block (simulated) does not break the owner's clearing;
    # clearing starts over with a single half-open probe (the pure rule drops any lease).
    seed.conn.execute(
        "update ops.host_budgets set in_flight_until = now() + interval '5 minutes',"
        " in_flight_token = gen_random_uuid() where workspace_id = %s and host = %s",
        (env.workspace_id, HOST),
    )
    token = _row(seed, env.workspace_id)["in_flight_token"]
    cleared = await gate.clear_access_block(env.owner, HOST, reason="permitted access restored")
    assert cleared.access_blocked_at is None
    row = _row(seed, env.workspace_id)
    assert row["access_blocked_at"] is None and row["access_blocked_reason"] is None
    assert row["in_flight_token"] is None and row["in_flight_until"] is None and token is not None
    assert row["circuit_state"] == "half_open"
