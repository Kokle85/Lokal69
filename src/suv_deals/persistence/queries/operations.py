"""Operational reads: health, overview, sources, settings and outbox attention (spec 20, 21
``deals_health``, 23 screens 1/6/7, 30, 32).

- `health_view` (``deals_health``): build id/version, readiness (database reachable, schema
  compatible = every migration this build needs is present, critical configuration), per-source
  coverage/status/parser state/last successful scan/gaps and the activation blockers of
  ``ops.activation_gates``. It degrades instead of failing when the database is unavailable and
  never contains secrets, connection strings or credential-bearing URLs (``HealthView`` refuses
  them as a backstop).
- `overview_view` (``GET /api/overview``): running/paused sources, last successful scan, coverage
  gaps (never scanned, incomplete, budget-limited, blocked, parser-unhealthy, paused), review counts
  per queue, deliveries needing attention and activation blockers. "No matching listings" is never
  confused with "source not scanned". Review counts are claim-aware: a case counts as ``claimed``
  only while its claim is active; an expired claim counts as its restore state (the prior decision
  while it cites the case's revision, else ``pending``), exactly as the claim reaper restores it.
- `sources_view` (``GET /api/sources``) is ``sources_repo.list_sources`` plus warnings.
- `settings_view` (``GET /api/settings``): profiles (the optional EUR 4,000 manual profile is
  always listed and labelled ``DISABLED ...`` while disabled), the MK band, the contribution
  threshold (``PROPOSED`` until approved), the re-alert policy, destination bindings (external ids
  only) and gate states. Read-only; changes are owner-only operations elsewhere.
- `outbox_attention_view` (``GET /api/outbox``): deliveries needing attention (uncertain, blocked,
  dead-letter, retry-wait), keyset-paginated, never payloads.

Activation blockers are gates that are neither ``active`` nor ``not_requested`` (an optional
capability nobody asked for blocks nothing). Scopes: ``deals:read``; the outbox needs
``reviews:read`` (route table). Every statement carries an explicit ``workspace_id`` predicate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import sql

import suv_deals
from suv_deals.api.schemas import OutboxQuery
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    CoverageMode,
    GateStatus,
    OutboxState,
    ProfileKey,
    Scope,
    TechnicalStatus,
)
from suv_deals.domain.pagination import MIN_SECRET_BYTES, filter_hash, validate_limit
from suv_deals.domain.profiles import MANUAL_PROFILE_MAX_EUR, PRIMARY_MIN_EUR, BusinessConfig, SearchProfile
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode, NotFound, ValidationFailed
from suv_deals.persistence import bindings_repo, config_repo, gates, sources_repo
from suv_deals.persistence.database import Conn, Database, fetch_all
from suv_deals.persistence.errors_map import StatementTimeout, mapped_errors
from suv_deals.persistence.gates import GateRecord
from suv_deals.persistence.queries._common import (
    CASE_STATE_SQL,
    CursorSecret,
    QueryResult,
    db_now,
    decode_keyset,
    encode_keyset,
    enum_or_none,
    parse_datetime,
    parse_uuid,
    rendering,
    require_secret,
    text_list,
    utc_or_none,
)
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings
from suv_deals.views.common import ResponseWarning, WarningCode, warning
from suv_deals.views.operations import (
    BuildInfo,
    ConfigRevisionRef,
    CoverageGapView,
    DeliveryCounts,
    DestinationBindingView,
    GateView,
    HealthView,
    OutboxItemView,
    OutboxPage,
    OverviewSourceItem,
    OverviewView,
    QueueCount,
    ReadinessCheck,
    ReviewCounts,
    SettingsView,
    SourceCoverageView,
    SourceListView,
    SourceRunState,
    redact_secrets,
)

OUTBOX_QUERY: Final = "outbox_attention"
ATTENTION_STATES: Final = ("uncertain", "blocked", "dead_letter", "retry_wait")
SCANNED_OUTCOMES: Final = ("complete", "budget_limited", "partial")
MAX_SOURCES: Final = 200
MAX_GAPS: Final = 500
MAX_GAP_REASONS: Final = 50
MAX_GAP_REASON_CHARS: Final = 300
UNCERTAIN_NOTICE: Final = (
    "The provider may or may not have accepted this delivery; it is held for reconciliation and is "
    "never resent blindly."
)
_BLOCKER_EXEMPT: Final = frozenset({GateStatus.ACTIVE, GateStatus.NOT_REQUESTED})
_BUILD_ID_RE: Final = re.compile(r"[^A-Za-z0-9._:+-]")
_VERSION_RE: Final = re.compile(r"[^A-Za-z0-9._+-]")


# --------------------------------------------------------------------------------------------
# Schema readiness
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaMarker:
    """An object that proves a migration is applied: a relation, optionally one of its columns.

    The migration ledger is not used: hosted Supabase records connector-applied migrations under
    their apply time, not the file name (docs/schema.md section 9), so objects are probed instead.
    """

    migration: str
    relation: str
    column: str | None = None


#: Objects this build reads, one per migration that introduced them (oldest first).
SCHEMA_MARKERS: Final[tuple[SchemaMarker, ...]] = (
    SchemaMarker("20261006000200_core_tables", "app.search_profiles"),
    SchemaMarker("20261006000300_queue_and_crawl_ops", "ops.source_schedules"),
    SchemaMarker("20261006000400_listings", "app.field_evidence"),
    SchemaMarker("20261006000500_market_and_valuation", "app.valuations"),
    SchemaMarker("20261006000600_reviews_and_notifications", "app.notification_preferences"),
    SchemaMarker("20261006000700_outbox_events_and_auth_ops", "ops.activation_gates"),
    SchemaMarker(
        "20261006000950_host_budgets_inflight_and_idempotency_scope", "ops.host_budgets", "in_flight_until"
    ),
    SchemaMarker(
        "20261006000950_host_budgets_inflight_and_idempotency_scope", "ops.idempotency_records_ws_key_uidx"
    ),
)

_MARKER_SQL: Final = """
select m.ord,
       pg_catalog.to_regclass(m.rel) is not null
       and (m.col is null or exists (
             select 1 from pg_catalog.pg_attribute a
              where a.attrelid = pg_catalog.to_regclass(m.rel) and a.attname = m.col
                and a.attnum > 0 and not a.attisdropped)) as present
  from unnest(%(rels)s::text[], %(cols)s::text[]) with ordinality as m(rel, col, ord)
 order by m.ord
"""


async def schema_markers_present(conn: Conn, markers: Sequence[SchemaMarker]) -> list[bool]:
    """Presence of each marker (catalog lookups only; no table data is read)."""
    if not markers:
        return []
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _MARKER_SQL,
            {"rels": [m.relation for m in markers], "cols": [m.column for m in markers]},
        )
    return [bool(r["present"]) for r in rows]


async def _schema_check(conn: Conn, markers: Sequence[SchemaMarker]) -> ReadinessCheck:
    present = await schema_markers_present(conn, markers)
    missing = sorted({m.migration for m, ok in zip(markers, present, strict=True) if not ok})
    if missing:
        return ReadinessCheck(
            name="schema", status="unavailable", detail=f"missing migrations: {', '.join(missing)}"[:300]
        )
    latest = max((m.migration for m in markers), default="none")
    return ReadinessCheck(
        name="schema", status="ok", detail=f"latest required migration present: {latest}"[:300]
    )


def _config_check(settings: Settings) -> ReadinessCheck:
    secret = settings.mcp_cursor_signing_secret
    if secret is None or len(secret.get_secret_value().encode("utf-8")) < MIN_SECRET_BYTES:
        return ReadinessCheck(
            name="config",
            status="not_configured",
            detail="pagination cursor signing secret is not configured",
        )
    if settings.app_env == "production" and settings.mcp_auth_mode == "dev_local":
        return ReadinessCheck(
            name="config", status="degraded", detail="development MCP authentication mode in production"
        )
    return ReadinessCheck(name="config", status="ok", detail=None)


def build_info(settings: Settings) -> BuildInfo:
    """Build id/version/environment; characters outside the safe set are replaced, never echoed."""
    build_id = _BUILD_ID_RE.sub("-", settings.build_id or "unknown")[:120] or "unknown"
    version = _VERSION_RE.sub("-", suv_deals.__version__)[:60] or "unknown"
    return BuildInfo(build_id=build_id, version=version, app_env=settings.app_env)


# --------------------------------------------------------------------------------------------
# Source coverage (shared by health, overview and the lifecycle view)
# --------------------------------------------------------------------------------------------

_SOURCES_SQL: Final = """
select s.id, s.source_key, s.display_name, s.country, s.role, s.enabled, s.paused, s.pause_reason,
       s.paused_at, s.technical_status, s.terms_status, s.terms_decision,
       runs.last_successful_scan_at, runs.last_complete_scan_at, coalesce(runs.scanned, false) as scanned,
       latest.outcome as latest_outcome, latest.coverage_mode as latest_coverage_mode,
       latest.gap_reasons as latest_gap_reasons,
       sched.last_complete_traversal_at, sched.incomplete_since,
       sched.coverage_mode as schedule_coverage_mode, sched.interval_seconds,
       (select coalesce(pg_catalog.jsonb_agg(distinct g.value), '[]'::jsonb)
          from ops.source_schedules sc
          cross join lateral pg_catalog.jsonb_array_elements_text(sc.gap_reasons) as g(value)
         where sc.workspace_id = s.workspace_id and sc.source_id = s.id) as schedule_gap_reasons
  from app.sources s
  left join lateral (
    select max(r.finished_at) filter (where r.outcome = any(%(scanned)s::text[])) as last_successful_scan_at,
           max(r.finished_at) filter (where r.outcome = 'complete') as last_complete_scan_at,
           bool_or(r.outcome = any(%(scanned)s::text[])) as scanned
      from ops.crawl_runs r
     where r.workspace_id = s.workspace_id and r.source_id = s.id) runs on true
  left join lateral (
    select r.outcome, r.coverage_mode, r.gap_reasons
      from ops.crawl_runs r
     where r.workspace_id = s.workspace_id and r.source_id = s.id and r.outcome <> 'running'
     order by r.started_at desc, r.id desc
     limit 1) latest on true
  left join lateral (
    select max(sc.last_complete_traversal_at) as last_complete_traversal_at,
           min(sc.incomplete_since) as incomplete_since,
           min(sc.interval_seconds) as interval_seconds,
           (pg_catalog.array_agg(sc.coverage_mode order by sc.updated_at desc, sc.id))[1] as coverage_mode
      from ops.source_schedules sc
     where sc.workspace_id = s.workspace_id and sc.source_id = s.id) sched on true
 where s.workspace_id = %(ws)s
   and (%(source_id)s::uuid is null or s.id = %(source_id)s::uuid)
 order by s.source_key
 limit %(limit)s
"""

_SCHEDULE_GAPS_SQL: Final = """
select s.source_key, p.profile_key, sc.partition_key, sc.incomplete_since, sc.gap_reasons,
       r.outcome as run_outcome
  from ops.source_schedules sc
  join app.sources s on s.workspace_id = sc.workspace_id and s.id = sc.source_id
  left join app.search_profiles p on p.workspace_id = sc.workspace_id and p.id = sc.profile_id
  left join lateral (
    select r.outcome from ops.crawl_runs r
     where r.workspace_id = sc.workspace_id and r.source_id = sc.source_id
       and r.profile_id = sc.profile_id and r.partition_key = sc.partition_key and r.outcome <> 'running'
     order by r.started_at desc, r.id desc
     limit 1) r on true
 where sc.workspace_id = %(ws)s
   and (sc.incomplete_since is not null or pg_catalog.jsonb_array_length(sc.gap_reasons) > 0)
 order by s.source_key, p.profile_key, sc.partition_key
 limit %(limit)s
"""


def source_state(row: Mapping[str, Any]) -> SourceRunState:
    """Paused, blocked, parser-unhealthy, disabled and never-scanned stay distinct (spec 30)."""
    if row["paused"]:
        return SourceRunState.PAUSED
    if row["technical_status"] == TechnicalStatus.ACCESS_BLOCKED.value:
        return SourceRunState.BLOCKED
    if row["technical_status"] == TechnicalStatus.PARSER_UNHEALTHY.value:
        return SourceRunState.PARSER_UNHEALTHY
    if not row["enabled"]:
        return SourceRunState.DISABLED
    return SourceRunState.RUNNING if row["scanned"] else SourceRunState.NOT_SCANNED


def _gap_reasons(row: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = list(
        text_list(row["schedule_gap_reasons"], limit=MAX_GAP_REASONS, max_chars=MAX_GAP_REASON_CHARS)
    )
    if row["latest_outcome"] not in (None, "complete"):
        for reason in text_list(
            row["latest_gap_reasons"], limit=MAX_GAP_REASONS, max_chars=MAX_GAP_REASON_CHARS
        ):
            if reason not in reasons:
                reasons.append(reason)
    return tuple(redact_secrets(r) for r in reasons[:MAX_GAP_REASONS])


async def source_rows(
    conn: Conn, actor: ActorContext, *, source_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Per-source status and scan facts (one statement; bounded to 200 sources, or one source)."""
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SOURCES_SQL,
            {
                "ws": actor.workspace_id,
                "source_id": source_id,
                "scanned": list(SCANNED_OUTCOMES),
                "limit": MAX_SOURCES,
            },
        )
    return [dict(r) for r in rows]


def coverage_view(row: Mapping[str, Any]) -> SourceCoverageView:
    mode = enum_or_none(CoverageMode, row["schedule_coverage_mode"]) or enum_or_none(
        CoverageMode, row["latest_coverage_mode"]
    )
    return SourceCoverageView(
        source_id=row["id"],
        source_key=row["source_key"],
        country=row["country"],
        role=row["role"],
        state=source_state(row),
        enabled=row["enabled"],
        paused=row["paused"],
        technical_status=row["technical_status"],
        terms_status=row["terms_status"],
        terms_decision=row["terms_decision"],
        coverage_mode=mode,
        last_successful_scan_at=utc_or_none(row["last_successful_scan_at"]),
        last_complete_traversal_at=utc_or_none(row["last_complete_traversal_at"]),
        incomplete_since=utc_or_none(row["incomplete_since"]),
        gap_reasons=_gap_reasons(row),
    )


def _source_warnings(states: Sequence[SourceRunState]) -> list[ResponseWarning]:
    warnings: list[ResponseWarning] = []
    if SourceRunState.PAUSED in states:
        warnings.append(warning(WarningCode.SOURCE_PAUSED))
    if SourceRunState.BLOCKED in states:
        warnings.append(warning(WarningCode.SOURCE_BLOCKED))
    if any(s in (SourceRunState.NOT_SCANNED, SourceRunState.PARSER_UNHEALTHY) for s in states):
        warnings.append(warning(WarningCode.COVERAGE_GAP))
    return warnings


# --------------------------------------------------------------------------------------------
# Gates and the activation bridge
# --------------------------------------------------------------------------------------------


def gate_view(record: GateRecord) -> GateView:
    return GateView(
        capability=record.capability,
        dependency=record.dependency,
        required_evidence=record.required_evidence,
        status=record.status,
        owner=record.owner,
        next_action=record.next_action,
        checked_at=record.checked_at,
    )


def activation_blockers(records: Sequence[GateRecord]) -> tuple[GateView, ...]:
    return tuple(gate_view(r) for r in records if r.status not in _BLOCKER_EXEMPT)[:100]


def _gate_warnings(records: Sequence[GateRecord]) -> list[ResponseWarning]:
    if any(r.status == GateStatus.BLOCKED for r in records):
        return [warning(WarningCode.ACTIVATION_BLOCKED)]
    return []


BridgeStatus = Literal["unavailable", "configured", "verified"]
NotificationRoute = Literal["none", "mcp_events", "slack"]


async def bridge_state(
    conn: Conn, actor: ActorContext, settings: Settings
) -> tuple[BridgeStatus, NotificationRoute]:
    """Event-bridge status (verified only with an enabled, approved, verified binding of the
    configured provider) and the active candidate-discovery route (``none`` while external
    notifications are not allowed)."""
    bindings = await bindings_repo.list_bindings(conn, actor)
    provider = settings.event_bridge_provider
    status: BridgeStatus = "unavailable"
    if settings.event_bridge_enabled and provider != "disabled":
        verified = any(
            b.provider == provider and b.enabled and b.approved_at is not None and b.verified_at is not None
            for b in bindings
        )
        status = "verified" if verified else "configured"
    route: NotificationRoute = "none"
    if settings.allow_external_notifications:
        for selection in await bindings_repo.active_routes(conn, actor):
            if selection.category == "candidate_discovery":
                route = selection.provider
                break
    return status, route


# --------------------------------------------------------------------------------------------
# deals_health
# --------------------------------------------------------------------------------------------


async def health_view(
    db: Database,
    actor: ActorContext,
    settings: Settings,
    *,
    markers: Sequence[SchemaMarker] = SCHEMA_MARKERS,
    clock: Clock | None = None,
) -> QueryResult[HealthView]:
    """``deals_health``: readiness, per-source coverage and activation blockers; no secrets.

    A database outage degrades the view (``ready: false``) instead of failing it. Source and gate
    data are only read when the schema is compatible.
    """
    actor.require(Scope.DEALS_READ)
    build = build_info(settings)
    config_check = _config_check(settings)
    as_of = (clock or SystemClock()).now()
    sources: tuple[SourceCoverageView, ...] = ()
    blockers: tuple[GateView, ...] = ()
    gate_records: list[GateRecord] = []
    bridge: BridgeStatus = "unavailable"
    route: NotificationRoute = "none"
    try:
        async with unit_of_work(db, actor) as conn:
            as_of = await db_now(conn)
            database = ReadinessCheck(name="database", status="ok", detail=None)
            schema = await _schema_check(conn, markers)
            if schema.status == "ok":
                coverage_rows = await source_rows(conn, actor)
                with rendering("source coverage"):
                    sources = tuple(coverage_view(r) for r in coverage_rows)
                gate_records = await gates.list_gates(conn, actor)
                blockers = activation_blockers(gate_records)
                bridge, route = await bridge_state(conn, actor, settings)
    except (DependencyUnavailable, StatementTimeout):
        database = ReadinessCheck(name="database", status="unavailable", detail="database not reachable")
        schema = ReadinessCheck(name="schema", status="unknown", detail="database not reachable")
        sources, blockers, gate_records = (), (), []
        bridge, route = "unavailable", "none"
    with rendering("health status"):
        view = HealthView(
            build=build,
            ready=all(c.status == "ok" for c in (database, schema, config_check)),
            readiness=(database, schema, config_check),
            source_network_enabled=settings.source_network_enabled,
            bridge_status=bridge,
            notification_route=route,
            sources=sources,
            activation_blockers=blockers,
        )
    warnings = _source_warnings([s.state for s in sources]) + _gate_warnings(gate_records)
    if not view.ready:
        warnings.insert(0, warning(WarningCode.DEPENDENCY_DEGRADED))
    return QueryResult(data=view, as_of=ensure_utc(as_of), warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------------------------

# Claim-aware state (``_common.CASE_STATE_SQL``): an expired claim counts as its restore state
# (the prior decision while it cites the case's revision, else pending), never as claimed.
_REVIEW_COUNTS_SQL: Final = sql.SQL("""
select c.profile_key, c.queue_label, {case_state} as state, count(*) as n
  from app.review_cases c
 where c.workspace_id = %(ws)s and c.state <> 'superseded'
 group by 1, 2, 3
 order by 1, 2, 3
""").format(case_state=sql.SQL(CASE_STATE_SQL))
_DELIVERY_COUNTS_SQL: Final = """
select o.state, count(*) as n
  from ops.outbox o
 where o.workspace_id = %(ws)s and o.state = any(%(states)s::text[])
 group by o.state
"""


def _source_gaps(row: Mapping[str, Any], state: SourceRunState) -> list[CoverageGapView]:
    key = row["source_key"]
    if state == SourceRunState.PAUSED:
        reason = row["pause_reason"]
        return [
            CoverageGapView(
                source_key=key,
                profile=None,
                partition_key=None,
                kind="paused",
                since=utc_or_none(row["paused_at"]),
                reasons=() if reason is None else (redact_secrets(str(reason)[:MAX_GAP_REASON_CHARS]),),
            )
        ]
    kinds: dict[SourceRunState, Literal["blocked", "parser_unhealthy", "not_scanned"]] = {
        SourceRunState.BLOCKED: "blocked",
        SourceRunState.PARSER_UNHEALTHY: "parser_unhealthy",
        SourceRunState.NOT_SCANNED: "not_scanned",
    }
    if state in kinds:
        return [
            CoverageGapView(
                source_key=key,
                profile=None,
                partition_key=None,
                kind=kinds[state],
                since=None,
                reasons=_gap_reasons(row),
            )
        ]
    if state == SourceRunState.RUNNING and row["latest_outcome"] == "blocked":
        return [
            CoverageGapView(
                source_key=key,
                profile=None,
                partition_key=None,
                kind="blocked",
                since=None,
                reasons=_gap_reasons(row),
            )
        ]
    return []


def _schedule_gap(row: Mapping[str, Any]) -> CoverageGapView:
    return CoverageGapView(
        source_key=row["source_key"],
        profile=enum_or_none(ProfileKey, row["profile_key"]),
        partition_key=str(row["partition_key"])[:80],
        kind="budget_limited" if row["run_outcome"] == "budget_limited" else "incomplete_scan",
        since=utc_or_none(row["incomplete_since"]),
        reasons=tuple(
            redact_secrets(r)
            for r in text_list(row["gap_reasons"], limit=MAX_GAP_REASONS, max_chars=MAX_GAP_REASON_CHARS)
        ),
    )


def review_counts(rows: Sequence[Mapping[str, Any]]) -> ReviewCounts:
    totals = {"pending": 0, "claimed": 0, "needs_information": 0, "watch": 0, "shortlisted": 0}
    queues: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        state, n = row["state"], int(row["n"])
        if state in totals:
            totals[state] += n
        queue = queues.setdefault((row["profile_key"], row["queue_label"]), {})
        queue[state] = queue.get(state, 0) + n
    by_queue = tuple(
        QueueCount(
            profile=ProfileKey(profile),
            queue_label=str(label)[:120],
            pending=counts.get("pending", 0),
            claimed=counts.get("claimed", 0),
            needs_information=counts.get("needs_information", 0),
        )
        for (profile, label), counts in sorted(queues.items())
    )[:10]
    return ReviewCounts(by_queue=by_queue, **totals)


async def overview_view(conn: Conn, actor: ActorContext, settings: Settings) -> QueryResult[OverviewView]:
    """Spec 23 screen 1 (``GET /api/overview``)."""
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    rows = await source_rows(conn, actor)
    async with mapped_errors():
        schedule_gaps = await fetch_all(
            conn, _SCHEDULE_GAPS_SQL, {"ws": actor.workspace_id, "limit": MAX_GAPS}
        )
        counts = await fetch_all(conn, _REVIEW_COUNTS_SQL, {"ws": actor.workspace_id, "now": now})
        deliveries = await fetch_all(
            conn, _DELIVERY_COUNTS_SQL, {"ws": actor.workspace_id, "states": list(ATTENTION_STATES)}
        )
    gate_records = await gates.list_gates(conn, actor)
    bridge, _route = await bridge_state(conn, actor, settings)
    items: list[OverviewSourceItem] = []
    gaps: list[CoverageGapView] = []
    states: list[SourceRunState] = []
    with rendering("overview"):
        for row in rows:
            state = source_state(row)
            states.append(state)
            reason = row["pause_reason"]
            items.append(
                OverviewSourceItem(
                    source_id=row["id"],
                    source_key=row["source_key"],
                    display_name=row["display_name"],
                    country=row["country"],
                    state=state,
                    last_successful_scan_at=utc_or_none(row["last_successful_scan_at"]),
                    pause_reason=None if reason is None else redact_secrets(reason),
                )
            )
            gaps.extend(_source_gaps(row, state))
        gaps.extend(_schedule_gap(r) for r in schedule_gaps)
        scans = [i.last_successful_scan_at for i in items if i.last_successful_scan_at is not None]
        delivery = {r["state"]: int(r["n"]) for r in deliveries}
        view = OverviewView(
            sources=tuple(items),
            running_sources=sum(1 for s in states if s == SourceRunState.RUNNING),
            paused_sources=sum(1 for s in states if s == SourceRunState.PAUSED),
            last_successful_scan_at=max(scans) if scans else None,
            coverage_gaps=tuple(gaps[:MAX_GAPS]),
            pending_reviews=review_counts(counts),
            failed_deliveries=DeliveryCounts(
                uncertain=delivery.get("uncertain", 0),
                blocked=delivery.get("blocked", 0),
                dead_letter=delivery.get("dead_letter", 0),
                retry_wait=delivery.get("retry_wait", 0),
            ),
            activation_blockers=activation_blockers(gate_records),
            bridge_status=bridge,
        )
    warnings = _source_warnings(states) + _gate_warnings(gate_records)
    if gaps and not any(w.code == WarningCode.COVERAGE_GAP for w in warnings):
        warnings.append(warning(WarningCode.COVERAGE_GAP))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------


async def sources_view(conn: Conn, actor: ActorContext) -> QueryResult[SourceListView]:
    """Spec 23 screen 6 (``GET /api/sources``): terms separate from technical status, parser
    health, robots handling, rate budget, recent runs, pause state and ``version``."""
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    view = await sources_repo.list_sources(conn, actor)
    warnings = _source_warnings([s.state for s in view.items])
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------

MANUAL_OPTION: Final = SearchProfile(
    key=ProfileKey.MANUAL_4000,
    label="Manual review: up to EUR 4,000 (disabled by default)",
    queue_label="Manual EUR 4,000 review queue",
    enabled=False,
    min_price_eur=PRIMARY_MIN_EUR,
    max_price_eur=MANUAL_PROFILE_MAX_EUR,
)


def with_manual_option(config: BusinessConfig) -> BusinessConfig:
    """The configuration as displayed: the optional EUR 4,000 manual profile is always listed
    (disabled and labelled as such when the configuration does not define it)."""
    if ProfileKey.MANUAL_4000 in config.profiles:
        return config
    return config.model_copy(update={"profiles": {**config.profiles, ProfileKey.MANUAL_4000: MANUAL_OPTION}})


def binding_view(binding: bindings_repo.DestinationBinding) -> DestinationBindingView:
    return DestinationBindingView(
        binding_id=binding.id,
        provider=binding.provider,
        label=binding.label[:120],
        enabled=binding.enabled,
        approval_recorded=binding.approved_at is not None and binding.approval_reference is not None,
        approved_at=binding.approved_at,
        verified_at=binding.verified_at,
        external_workspace_id=binding.external_workspace_id,
        external_channel_id=binding.external_channel_id,
        row_version=binding.row_version,
    )


async def settings_view(
    conn: Conn, actor: ActorContext, *, fallback_config: BusinessConfig | None = None
) -> QueryResult[SettingsView]:
    """Spec 23 screen 7 (``GET /api/settings``), read-only.

    The latest ``app.config_revisions`` row is shown; ``fallback_config`` (for example the YAML
    defaults) is used only while no revision has been recorded, with ``config_revision: null``.
    """
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    revision_ref: ConfigRevisionRef | None = None
    try:
        record, config = await config_repo.current_config(conn, actor)
        revision_ref = ConfigRevisionRef(
            config_revision_id=record.id,
            revision=record.revision,
            created_at=record.created_at,
            reason=record.reason[:2000],
        )
    except NotFound:
        if fallback_config is None:
            raise AppError(
                ErrorCode.INSUFFICIENT_DATA,
                "No business configuration has been recorded yet",
                retryable=False,
            ) from None
        config = fallback_config
    except ValidationFailed:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "The stored business configuration could not be read", retryable=False
        ) from None
    profiles = await config_repo.list_profiles(conn, actor)
    bindings = await bindings_repo.list_bindings(conn, actor)
    gate_records = await gates.list_gates(conn, actor)
    with rendering("settings"):
        view = SettingsView.from_config(
            with_manual_option(config),
            config_revision=revision_ref,
            destination_bindings=[binding_view(b) for b in bindings[:50]],
            gates=[gate_view(g) for g in gate_records[:100]],
            can_administer=actor.has(Scope.CONFIG_ADMIN),
            profile_rows={p.profile_key: (p.config_revision_id, p.row_version) for p in profiles},
        )
    warnings: list[ResponseWarning] = []
    if view.contribution_threshold.label == "PROPOSED":
        warnings.append(warning(WarningCode.THRESHOLD_PROPOSED))
    warnings.extend(_gate_warnings(gate_records))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Outbox attention
# --------------------------------------------------------------------------------------------

_OUTBOX_SQL: Final = """
select o.id, o.event_id, o.event_type, o.aggregate_type, o.aggregate_id, o.aggregate_version, o.state,
       o.attempts, o.max_attempts, o.destination_binding_id, o.last_error_code, o.blocker_code,
       o.event_created_at, o.send_attempted_at, o.provider_accepted_at, o.owner_seen_at, o.available_at,
       o.is_fixture
  from ops.outbox o
 where o.workspace_id = %(ws)s
   and o.state = any(%(states)s::text[])
   and o.event_created_at <= %(as_of)s
   and (%(after_created)s::timestamptz is null
        or (o.event_created_at, o.id) > (%(after_created)s::timestamptz, %(after_id)s::uuid))
 order by o.event_created_at, o.id
 limit %(limit)s
"""


def _outbox_item(row: Mapping[str, Any]) -> OutboxItemView:
    state = OutboxState(row["state"])
    return OutboxItemView(
        outbox_id=row["id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        state=state,
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        destination_binding_id=row["destination_binding_id"],
        last_error_code=row["last_error_code"],
        blocker_code=row["blocker_code"],
        event_created_at=ensure_utc(row["event_created_at"]),
        send_attempted_at=utc_or_none(row["send_attempted_at"]),
        provider_accepted_at=utc_or_none(row["provider_accepted_at"]),
        owner_seen_at=utc_or_none(row["owner_seen_at"]),
        available_at=ensure_utc(row["available_at"]),
        is_fixture=row["is_fixture"],
        uncertain_notice=UNCERTAIN_NOTICE if state == OutboxState.UNCERTAIN else None,
    )


async def outbox_attention_view(
    conn: Conn,
    actor: ActorContext,
    query: OutboxQuery,
    *,
    secret: CursorSecret,
) -> QueryResult[OutboxPage]:
    """``GET /api/outbox``: deliveries needing attention (oldest first), keyset-paginated, bound to
    the ``state`` filter; payloads are never read."""
    actor.require(Scope.REVIEWS_READ)
    keys = require_secret(secret)
    state, cursor = query.state, query.cursor
    size = validate_limit(query.limit)
    filters_hash = filter_hash(query.filters())
    now = await db_now(conn)
    as_of: datetime = now
    after_created: datetime | None = None
    after_id: Any = None
    if cursor is not None:
        position = decode_keyset(
            cursor,
            actor,
            query=OUTBOX_QUERY,
            filters_hash=filters_hash,
            now=now,
            secret=keys,
            parsers=(parse_datetime, parse_uuid),
        )
        after_created, after_id = position.sort
        as_of = position.as_of
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _OUTBOX_SQL,
            {
                "ws": actor.workspace_id,
                "states": [state] if state is not None else list(ATTENTION_STATES),
                "as_of": as_of,
                "after_created": after_created,
                "after_id": after_id,
                "limit": size + 1,
            },
        )
    page = rows[:size]
    with rendering("outbox delivery"):
        items = tuple(_outbox_item(r) for r in page)
    next_cursor = None
    if len(rows) > size and page:
        last = page[-1]
        next_cursor = encode_keyset(
            actor,
            query=OUTBOX_QUERY,
            filters_hash=filters_hash,
            last_sort_key=(ensure_utc(last["event_created_at"]), last["id"]),
            as_of=as_of,
            now=now,
            secret=keys,
        )
    warnings = [warning(WarningCode.FIXTURE_DATA)] if any(i.is_fixture for i in items) else []
    return QueryResult(
        data=OutboxPage(items=items), as_of=now, warnings=tuple(warnings), next_cursor=next_cursor
    )


__all__ = [
    "ATTENTION_STATES",
    "OUTBOX_QUERY",
    "SCHEMA_MARKERS",
    "SchemaMarker",
    "activation_blockers",
    "bridge_state",
    "build_info",
    "coverage_view",
    "gate_view",
    "health_view",
    "outbox_attention_view",
    "overview_view",
    "review_counts",
    "schema_markers_present",
    "settings_view",
    "source_rows",
    "source_state",
    "sources_view",
    "with_manual_option",
]
