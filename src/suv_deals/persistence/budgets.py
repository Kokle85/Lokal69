"""Database-backed per-host budget gate (spec sections 9 "Adaptive backoff", 30 budgets).

`DbBudgetGate` implements `crawling.policy_client.BudgetGate` over ``ops.host_budgets`` so the
token bucket, daily counters, circuit breaker, Retry-After and the "one navigation at a time per
host" lease survive restarts and are shared by every worker. All decisions are the pure
`crawling.rate_limits` functions (`decide`, `start_request`, `record_outcome`,
`abandon_request`, `clear_access_block`); this module only loads and stores their state.

Protocol (no network I/O inside a transaction):

1. `acquire` -- short transaction: lock the rows of every host of the source in host order
   (``FOR UPDATE``; a fixed order cannot deadlock), aggregate the source's daily usage across
   those hosts, `decide()`, and on ``Allow`` persist `start_request()` (token consumed, request
   counted, navigation lease taken). Time is database time (``clock_timestamp()``).
2. The caller performs the request outside any transaction.
3. `release` -- short transaction: lock the host row, fold the outcome in with
   `record_outcome()` (or `abandon_request()` when there is no outcome) and persist.

Equivalents for columns ``ops.host_budgets`` does not have (an additive migration adding
``in_flight_until``, ``in_flight_token`` and ``access_blocked_at`` is requested in the work
package report):

- Navigation lease: stored in ``next_request_not_before`` as exactly ``refilled_at +
  in_flight_lease_seconds`` (``start_request`` sets both in the same instant). On load, that
  exact equality identifies a lease; any other value is a backoff not-before. A lease that
  expires on its own (crashed worker) simply lets the next request through. On release the
  lease is cleared only if it is still the caller's own (same instant, microsecond precision);
  a stale holder never clears a newer worker's lease.
- Access block: stored as ``circuit_state = 'open'`` with ``open_until`` at a far-future
  sentinel (year 9999). Only `clear_access_block` (explicit owner action, audited) lifts it,
  and the next request is a single half-open probe.

An ``ACCESS_BLOCKED`` outcome yields a `RoutePauseRecommendation`; the gate keeps it for the
caller (`take_pause_recommendations`, `last_result`), who pauses the source route and opens one
deduplicated operational review item. Rows are per (workspace, host); a host shared by two
sources counts both sources' requests (conservative).
"""

from __future__ import annotations

import random
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from psycopg import sql

from suv_deals.adapters.base import FetchOutcome
from suv_deals.clock import ensure_utc
from suv_deals.crawling.policy_client import BudgetRequest
from suv_deals.crawling.rate_limits import (
    DEFAULT_BACKOFF,
    TOKEN_CAPACITY,
    Allow,
    BackoffPolicy,
    BudgetDecision,
    CircuitState,
    DailyUsage,
    Deny,
    DenyReason,
    HostBudgetState,
    OutcomeResult,
    RoutePauseRecommendation,
    RunUsage,
    abandon_request,
    clear_access_block,
    decide,
    effective_delay_seconds,
    record_outcome,
    start_request,
)
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.domain.sources import RateBudget
from suv_deals.errors import NotFound, ValidationFailed
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

ACCESS_BLOCK_SENTINEL: Final = datetime(9999, 1, 1, tzinfo=UTC)
_HOST_RE: Final = re.compile(r"^[a-z0-9.-]{1,253}$")
_INT32_MAX: Final = 2_147_483_647
_ROW_COLUMNS: Final = sql.SQL(
    "id, host, tokens, refilled_at, next_request_not_before, circuit_state, open_until,"
    " consecutive_failures, budget_day, requests_today, bytes_today, last_retry_after_seconds,"
    " retry_after_until, row_version, updated_at"
)


def _host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not _HOST_RE.fullmatch(host) or host[0] in ".-":
        raise ValidationFailed("budget host must be a lower-case DNS name")
    return host


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def state_from_row(row: Mapping[str, Any], source_key: str, policy: BackoffPolicy) -> HostBudgetState:
    """Map an ``ops.host_budgets`` row to the pure `HostBudgetState` (see module docstring)."""
    open_until = _utc(row["open_until"])
    blocked = row["circuit_state"] == CircuitState.OPEN.value and (
        open_until is not None and open_until >= ACCESS_BLOCK_SENTINEL
    )
    refilled = _utc(row["refilled_at"])
    not_before = _utc(row["next_request_not_before"])
    in_flight: datetime | None = None
    if (
        not_before is not None
        and refilled is not None
        and not_before == refilled + timedelta(seconds=policy.in_flight_lease_seconds)
    ):
        in_flight, not_before = not_before, None
    tokens = Decimal(row["tokens"])
    return HostBudgetState(
        source_key=source_key,
        host=row["host"],
        tokens=min(max(tokens, Decimal(0)), TOKEN_CAPACITY),
        refilled_at=refilled,
        circuit_state=CircuitState.CLOSED if blocked else CircuitState(row["circuit_state"]),
        open_until=None if blocked else open_until,
        consecutive_failures=int(row["consecutive_failures"]),
        budget_day=row["budget_day"],
        requests_today=int(row["requests_today"]),
        bytes_today=int(row["bytes_today"]),
        last_retry_after_seconds=row["last_retry_after_seconds"],
        retry_after_until=_utc(row["retry_after_until"]),
        next_request_not_before=not_before,
        in_flight_until=in_flight,
        access_blocked_at=_utc(row["updated_at"]) if blocked else None,
    )


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _row_params(
    state: HostBudgetState, budget: RateBudget | None, crawl_delay: Decimal | None
) -> dict[str, Any]:
    blocked = state.access_blocked_at is not None
    refill = None
    if budget is not None:
        refill = (Decimal(1) / effective_delay_seconds(budget, crawl_delay)).quantize(Decimal("0.00000001"))
        refill = max(refill, Decimal("0.00000001"))
    retry_after = state.last_retry_after_seconds
    return {
        "tokens": state.tokens.quantize(Decimal("0.0001")),
        "refilled_at": state.refilled_at,
        "next_request_not_before": _later(state.in_flight_until, state.next_request_not_before),
        "circuit_state": CircuitState.OPEN.value if blocked else state.circuit_state.value,
        "open_until": ACCESS_BLOCK_SENTINEL if blocked else state.open_until,
        "consecutive_failures": min(state.consecutive_failures, _INT32_MAX),
        "budget_day": state.budget_day,
        "requests_today": min(state.requests_today, _INT32_MAX),
        "bytes_today": state.bytes_today,
        "last_retry_after_seconds": None if retry_after is None else min(retry_after, _INT32_MAX),
        "retry_after_until": state.retry_after_until,
        "refill_per_second": refill,
        "daily_request_budget": None if budget is None else budget.daily_request_budget,
        "daily_byte_budget": None if budget is None else budget.daily_byte_budget,
    }


_PERSIST_SQL: Final = (
    "update ops.host_budgets set"
    " tokens = %(tokens)s,"
    " refilled_at = coalesce(%(refilled_at)s::timestamptz, refilled_at),"
    " next_request_not_before = %(next_request_not_before)s::timestamptz,"
    " circuit_state = %(circuit_state)s,"
    " open_until = %(open_until)s::timestamptz,"
    " consecutive_failures = %(consecutive_failures)s,"
    " budget_day = coalesce(%(budget_day)s::date, budget_day),"
    " requests_today = %(requests_today)s,"
    " bytes_today = %(bytes_today)s,"
    " last_retry_after_seconds = %(last_retry_after_seconds)s,"
    " retry_after_until = %(retry_after_until)s::timestamptz,"
    " refill_per_second = coalesce(%(refill_per_second)s::numeric, refill_per_second),"
    " daily_request_budget = coalesce(%(daily_request_budget)s::integer, daily_request_budget),"
    " daily_byte_budget = coalesce(%(daily_byte_budget)s::bigint, daily_byte_budget),"
    " row_version = row_version + 1"
    " where workspace_id = %(workspace_id)s and id = %(id)s"
)

_ENSURE_ROW_SQL: Final = (
    "insert into ops.host_budgets (workspace_id, host, capacity, refill_per_second, tokens,"
    " refilled_at, budget_day, daily_request_budget, daily_byte_budget)"
    " values (%(workspace_id)s, %(host)s, 1, %(refill)s, 1, clock_timestamp(),"
    " (clock_timestamp() at time zone 'UTC')::date, %(daily_requests)s, %(daily_bytes)s)"
    " on conflict (workspace_id, host) do nothing"
)

_LOCK_ROWS_SQL: Final = sql.SQL(
    "select {columns} from ops.host_budgets"
    " where workspace_id = %(workspace_id)s and host = any(%(hosts)s::text[])"
    " order by host for update"
).format(columns=_ROW_COLUMNS)

_LOCK_ONE_SQL: Final = sql.SQL(
    "select {columns} from ops.host_budgets"
    " where workspace_id = %(workspace_id)s and host = %(host)s for update"
).format(columns=_ROW_COLUMNS)

_READ_ONE_SQL: Final = sql.SQL(
    "select {columns} from ops.host_budgets where workspace_id = %(workspace_id)s and host = %(host)s"
).format(columns=_ROW_COLUMNS)


class DbBudgetGate:
    """`BudgetGate` backed by ``ops.host_budgets`` for one workspace (shared by all workers).

    Per-run caps (search pages / detail fetches per crawl run) are counted per gate instance,
    which a worker creates per run (`reset_run` starts a new run).
    """

    def __init__(
        self,
        db: Database,
        workspace_id: UUID,
        budgets: Mapping[str, RateBudget],
        *,
        policy: BackoffPolicy = DEFAULT_BACKOFF,
        rng: random.Random | None = None,
        crawl_delays: Mapping[str, Decimal] | None = None,
        source_hosts: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._db = db
        self._workspace_id = workspace_id
        self._budgets = dict(budgets)
        self._policy = policy
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._crawl_delays = {_host(h): d for h, d in (crawl_delays or {}).items()}
        self._source_hosts = (
            None
            if source_hosts is None
            else {key: frozenset(_host(h) for h in hosts) for key, hosts in source_hosts.items()}
        )
        self._usage: dict[str, RunUsage] = {}
        self._leases: dict[int, tuple[BudgetRequest, datetime]] = {}
        self._results: dict[tuple[str, str], OutcomeResult] = {}
        self._pauses: list[RoutePauseRecommendation] = []

    # ------------------------------------------------------------------ caller-facing state

    def run_usage(self, source_key: str) -> RunUsage:
        return self._usage.get(source_key, RunUsage())

    def reset_run(self, source_key: str) -> None:
        self._usage.pop(source_key, None)

    def last_result(self, source_key: str, host: str) -> OutcomeResult | None:
        return self._results.get((source_key, _host(host)))

    def take_pause_recommendations(self) -> list[RoutePauseRecommendation]:
        """Route pause recommendations (access blocked, oversized Retry-After) since the last call."""
        pauses, self._pauses = self._pauses, []
        return pauses

    # ------------------------------------------------------------------ BudgetGate protocol

    async def acquire(self, request: BudgetRequest) -> BudgetDecision:
        budget = self._budgets.get(request.source_key)
        if budget is None:
            return Deny(reason=DenyReason.BUDGET_EXHAUSTED)
        host = _host(request.host)
        delay = self._crawl_delays.get(host)
        usage = self.run_usage(request.source_key)
        refill = (Decimal(1) / effective_delay_seconds(budget, delay)).quantize(Decimal("0.00000001"))
        async with mapped_errors(), self._db.transaction(workspace_id=self._workspace_id) as conn:
            hosts = sorted(await self._hosts_of(conn, request.source_key) | {host})
            await conn.execute(
                _ENSURE_ROW_SQL,
                {
                    "workspace_id": self._workspace_id,
                    "host": host,
                    "refill": max(refill, Decimal("0.00000001")),
                    "daily_requests": budget.daily_request_budget,
                    "daily_bytes": budget.daily_byte_budget,
                },
            )
            rows = await fetch_all(conn, _LOCK_ROWS_SQL, {"workspace_id": self._workspace_id, "hosts": hosts})
            now = await _db_clock(conn)
            today = now.date()
            source_usage = DailyUsage(
                day=today,
                requests=sum(int(r["requests_today"]) for r in rows if r["budget_day"] == today),
                bytes=sum(int(r["bytes_today"]) for r in rows if r["budget_day"] == today),
            )
            row = next(r for r in rows if r["host"] == host)
            state = state_from_row(row, request.source_key, self._policy)
            decision = decide(
                state,
                now,
                budget,
                purpose=request.purpose,
                run_usage=usage,
                source_usage=source_usage,
                crawl_delay=delay,
            )
            if not isinstance(decision, Allow):
                return decision
            started = start_request(
                state,
                now,
                budget,
                purpose=request.purpose,
                run_usage=usage,
                source_usage=source_usage,
                crawl_delay=delay,
                policy=self._policy,
            )
            await conn.execute(
                _PERSIST_SQL,
                {**_row_params(started, budget, delay), "workspace_id": self._workspace_id, "id": row["id"]},
            )
        assert started.in_flight_until is not None
        self._leases[id(request)] = (request, started.in_flight_until)
        if request.purpose == "search":
            usage = usage.model_copy(update={"search_pages": usage.search_pages + 1})
        elif request.purpose == "detail":
            usage = usage.model_copy(update={"detail_fetches": usage.detail_fetches + 1})
        self._usage[request.source_key] = usage
        return decision

    async def release(self, request: BudgetRequest, outcome: FetchOutcome | None) -> None:
        entry = self._leases.pop(id(request), None)
        own_lease = entry[1] if entry is not None and entry[0] is request else None
        budget = self._budgets.get(request.source_key)
        host = _host(request.host)
        delay = self._crawl_delays.get(host)
        result: OutcomeResult | None = None
        async with mapped_errors(), self._db.transaction(workspace_id=self._workspace_id) as conn:
            row = await fetch_one(conn, _LOCK_ONE_SQL, {"workspace_id": self._workspace_id, "host": host})
            if row is None:
                return
            now = await _db_clock(conn)
            state = state_from_row(row, request.source_key, self._policy)
            ours = own_lease is not None and state.in_flight_until == own_lease
            # A newer holder's lease (ours expired and was re-taken) must survive our release.
            foreign_lease = None if ours else state.in_flight_until
            base = state.model_copy(update={"in_flight_until": own_lease if ours else None})
            if outcome is None or budget is None:
                updated = abandon_request(base, now)
            else:
                result = record_outcome(base, outcome, now, self._rng, budget=budget, policy=self._policy)
                updated = result.state
            if foreign_lease is not None:
                updated = updated.model_copy(update={"in_flight_until": foreign_lease})
            await conn.execute(
                _PERSIST_SQL,
                {**_row_params(updated, budget, delay), "workspace_id": self._workspace_id, "id": row["id"]},
            )
        if result is not None:
            self._results[(request.source_key, host)] = result
            if result.pause is not None:
                self._pauses.append(result.pause)

    # ------------------------------------------------------------------ reads and operator actions

    async def host_state(self, source_key: str, host: str) -> HostBudgetState | None:
        async with mapped_errors(), self._db.transaction(workspace_id=self._workspace_id) as conn:
            row = await fetch_one(
                conn, _READ_ONE_SQL, {"workspace_id": self._workspace_id, "host": _host(host)}
            )
        return None if row is None else state_from_row(row, source_key, self._policy)

    async def daily_usage(self, source_key: str) -> DailyUsage:
        """Source-wide usage for today's UTC day (database time), across all of its hosts."""
        async with mapped_errors(), self._db.transaction(workspace_id=self._workspace_id) as conn:
            hosts = sorted(await self._hosts_of(conn, source_key))
            now = await _db_clock(conn)
            row = await fetch_one(
                conn,
                "select coalesce(sum(requests_today), 0) as requests, coalesce(sum(bytes_today), 0) as bytes"
                " from ops.host_budgets where workspace_id = %(workspace_id)s"
                " and host = any(%(hosts)s::text[]) and budget_day = %(day)s",
                {"workspace_id": self._workspace_id, "hosts": hosts, "day": now.date()},
            )
        assert row is not None
        return DailyUsage(day=now.date(), requests=int(row["requests"]), bytes=int(row["bytes"]))

    async def clear_access_block(self, actor: ActorContext, host: str, *, reason: str) -> HostBudgetState:
        """Explicit, permitted owner action after access was restored; audited.

        The next request to the host is a single half-open probe.
        """
        actor.require(Scope.CONFIG_ADMIN)
        if actor.workspace_id != self._workspace_id:
            raise NotFound("Host budget not found")
        name = _host(host)
        async with mapped_errors(), self._db.transaction(actor) as conn:
            row = await fetch_one(conn, _LOCK_ONE_SQL, {"workspace_id": self._workspace_id, "host": name})
            if row is None:
                raise NotFound("Host budget not found")
            now = await _db_clock(conn)
            state = state_from_row(row, "operator", self._policy)
            cleared = clear_access_block(state, now).model_copy(update={"next_request_not_before": None})
            await conn.execute(
                _PERSIST_SQL,
                {**_row_params(cleared, None, None), "workspace_id": self._workspace_id, "id": row["id"]},
            )
            await audit.record(
                conn,
                actor,
                "host_budget.clear_access_block",
                "host_budget",
                row["id"],
                prior_version=int(row["row_version"]),
                new_version=int(row["row_version"]) + 1,
                reason=reason,
                metadata={"host": name, "was_blocked": state.access_blocked_at is not None},
            )
        return cleared

    # ------------------------------------------------------------------ helpers

    async def _hosts_of(self, conn: Conn, source_key: str) -> frozenset[str]:
        if self._source_hosts is not None:
            return self._source_hosts.get(source_key, frozenset())
        row = await fetch_one(
            conn,
            "select allowed_hosts from app.sources where workspace_id = %(workspace_id)s"
            " and source_key = %(source_key)s",
            {"workspace_id": self._workspace_id, "source_key": source_key},
        )
        if row is None:
            return frozenset()
        hosts: set[str] = set()
        for value in row["allowed_hosts"] or ():
            try:
                hosts.add(_host(str(value)))
            except ValidationFailed:
                continue  # patterns or invalid entries are not budget rows
        return frozenset(hosts)


async def _db_clock(conn: Conn) -> datetime:
    row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return ensure_utc(row["now"])


__all__ = [
    "ACCESS_BLOCK_SENTINEL",
    "DbBudgetGate",
    "state_from_row",
]
