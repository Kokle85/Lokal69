"""Source registry, access decisions, schedules, crawl runs and fetch records (spec 5, 9, 25, 30).

Source registry (``app.sources``)
    `sync_sources_from_yaml` upserts every `SourceConfig` field. Terms status/decision and the
    technical status stay separate columns. A source is never enabled while
    `domain.sources.activation_problems()` reports anything for its *effective* configuration.
    Runtime safety states win over the YAML: an ``access_blocked`` or ``parser_unhealthy``
    technical status set at runtime is preserved (clearing it is an explicit owner action,
    `set_technical_status`), and pause fields are never touched by a sync. A source that
    disappeared from the YAML is disabled (never deleted). Every change bumps ``version`` and is
    audited with prior/new version.

    A blocking technical status forces ``enabled = false``: the ``sources_enable_gate_ck`` CHECK
    mirrors `activation_problems()`, so an enabled source can never be access-blocked or
    parser-unhealthy. Re-enabling after recovery is a separate explicit action (a later sync).

Pause and access blocks
    `pause_source` implements the ``sources_pause`` tool: ``sources:pause`` scope, optimistic
    ``expected_version``, an idempotency record (``ops.idempotency_records``, taken first), never an
    implicit resume or enable. `resume_source` is a separate owner action (``config:admin``).
    `record_access_block` (spec 5, 9: 401/403/CAPTCHA/login/paywall) stops all network work of the
    source (technical status ``access_blocked``) and opens exactly ONE deduplicated operational
    review item: the per-source activation gate ``source_access.<source_key>`` in status
    ``blocked`` (shown with the activation blockers of the overview and ``deals_health``). A repeated
    block of a blocked source changes nothing. Host-level blocks live in ``ops.host_budgets`` and
    are written by the budget gate (`persistence.budgets`).

Parser health (spec 25)
    `set_technical_status` records parser-health incidents. Suspected parser drift (``degraded``)
    and ``parser_unhealthy`` both pause new opportunity alerts for the source
    (`alert_pause_reason`: ``parser_degraded`` / ``parser_unhealthy``); for ``parser_unhealthy`` the
    listing pipeline also stores new revisions of that source as quarantined evidence instead of
    promoting them.

Scheduling (spec 9 "Scheduling", one short transaction, no network I/O)
    `advance_schedule`: (1) lock the schedule row; (2) confirm the source is enabled, unpaused,
    unblocked and has a proceed terms decision, the profile is enabled, no earlier slot job is still
    open (backlog), the schedule is not in backoff and today's request budget remains; (3) insert
    the slot's discovery job with `jobs.enqueue_slot` (unique per (source, profile, partition,
    slot)); (4) advance ``next_due_at`` in the same transaction, so both commit or neither does;
    (5) the caller commits before any network request. Slots come from database time. Missed slots
    after downtime are recorded as a coverage gap; they are never replayed as a traffic burst.

Watermarks and coverage (spec 9 "Discovery watermarks")
    `finish_crawl_run`: only a COMPLETE traversal advances the complete watermark (never
    backwards) or, for ``rolling_pages``, records the page depth and the last complete traversal
    (no timestamp watermark is ever fabricated). Budget-limited/partial runs keep the watermark,
    persist the resume cursor and record the gap; failed/blocked runs also back off.

Lock order (extends `transactions.LOCK_ORDER`):
    - API/MCP pause: ``ops.idempotency_records`` -> ``app.sources`` (FOR UPDATE) -> audit.
    - access block: ``app.sources`` (FOR UPDATE) -> ``ops.activation_gates`` -> audit.
    - scheduler: ``ops.source_schedules`` (FOR UPDATE) -> ``app.sources`` (FOR SHARE) ->
      ``ops.jobs`` (insert) -> audit.
    - run finish: ``ops.crawl_runs`` (FOR UPDATE) -> ``ops.source_schedules`` (FOR UPDATE).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from suv_deals.adapters.base import FetchOutcome, FetchPurpose, ParserHealth
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    AccessState,
    Completeness,
    CoverageMode,
    GateStatus,
    JobType,
    ProfileKey,
    Scope,
    SourceMode,
    TechnicalStatus,
    TermsDecision,
    TermsStatus,
)
from suv_deals.domain.sources import RateBudget, SourceConfig, activation_problems
from suv_deals.errors import (
    AppError,
    ErrorCode,
    Forbidden,
    NotFound,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.mcp.schemas import SourcesPauseInput
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit, gates, idempotency, jobs
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.views.operations import (
    CrawlRunView,
    ParserHealthView,
    RateBudgetView,
    RobotsView,
    SourceListView,
    SourcePauseResult,
    SourceRunState,
    SourceStatusView,
    TechnicalView,
    TermsView,
)

PAUSE_OPERATION: Final = "sources_pause"
ACCESS_GATE_PREFIX: Final = "source_access."
RUNTIME_BLOCKING_STATUSES: Final = frozenset(
    {TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY}
)
NETWORK_BLOCKING_STATUSES: Final = RUNTIME_BLOCKING_STATUSES | {TechnicalStatus.UNTESTED}
MAX_GAP_REASONS: Final = 50
MAX_GAP_REASON_CHARS: Final = 300
MAX_ROBOTS_BODY_BYTES: Final = 512 * 1024
MAX_BACKOFF: Final = timedelta(days=1)
_HOST_RE: Final = re.compile(r"^[a-z0-9.-]{1,253}$")
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_PARTITION_RE: Final = re.compile(r"^[A-Za-z0-9_:.-]{1,80}$")
_HEADER_ALLOWLIST: Final = frozenset(
    {"content-type", "retry-after", "last-modified", "etag", "x-robots-tag", "x-robots-status"}
)

_SOURCE_COLUMNS: Final = (
    "id",
    "workspace_id",
    "source_key",
    "display_name",
    "country",
    "role",
    "mode",
    "adapter",
    "adapter_version",
    "enabled",
    "technical_status",
    "terms_status",
    "terms_url",
    "terms_reviewed_at",
    "terms_decision",
    "terms_decision_actor",
    "terms_decision_note",
    "technical_denial_policy",
    "robots_policy",
    "allowed_hosts",
    "allowed_search_paths",
    "allowed_detail_paths",
    "source_timezone",
    "detail_mode",
    "config",
    "paused",
    "pause_reason",
    "paused_by",
    "paused_at",
    "last_live_smoke_at",
    "version",
    "created_at",
    "updated_at",
)
# Columns a YAML sync owns (everything else is runtime state: pause, smoke time, version).
_SYNC_COLUMNS: Final = (
    "display_name",
    "country",
    "role",
    "mode",
    "adapter",
    "adapter_version",
    "enabled",
    "technical_status",
    "terms_status",
    "terms_url",
    "terms_reviewed_at",
    "terms_decision",
    "terms_decision_actor",
    "terms_decision_note",
    "technical_denial_policy",
    "robots_policy",
    "allowed_hosts",
    "allowed_search_paths",
    "allowed_detail_paths",
    "source_timezone",
    "detail_mode",
    "config",
)
_TYPED_CONFIG_KEYS: Final = frozenset({"source_key", *_SYNC_COLUMNS} - {"config"})
_SCHEDULE_COLUMNS: Final = (
    "id",
    "workspace_id",
    "source_id",
    "profile_id",
    "partition_key",
    "interval_seconds",
    "next_due_at",
    "last_slot",
    "cursor",
    "run_id",
    "coverage_mode",
    "complete_watermark",
    "page_depth",
    "last_complete_traversal_at",
    "incomplete_since",
    "gap_reasons",
    "backoff_until",
    "consecutive_failures",
    "paused",
    "pause_reason",
    "row_version",
    "created_at",
    "updated_at",
)
_RUN_COLUMNS: Final = (
    "id",
    "workspace_id",
    "source_id",
    "profile_id",
    "partition_key",
    "job_id",
    "build_id",
    "adapter_version",
    "parser_version",
    "crawler_version",
    "coverage_mode",
    "started_at",
    "finished_at",
    "outcome",
    "pages_fetched",
    "cards_seen",
    "new_listings",
    "changed_listings",
    "detail_jobs_enqueued",
    "detail_jobs_deduplicated",
    "result_count_reported",
    "page_depth",
    "watermark_from",
    "watermark_to",
    "gap_reasons",
    "access_state",
    "error_code",
    "created_at",
    "updated_at",
)


def _cols(names: Sequence[str], alias: str | None = None) -> sql.Composable:
    if alias is None:
        return sql.SQL(", ").join(sql.Identifier(c) for c in names)
    return sql.SQL(", ").join(sql.Identifier(alias, c) for c in names)


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


class SourceRecord(BaseModel):
    """One ``app.sources`` row."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    source_key: str
    display_name: str
    country: str
    role: Literal["acquisition", "mk_comparable"]
    mode: SourceMode
    adapter: str
    adapter_version: str
    enabled: bool
    technical_status: TechnicalStatus
    terms_status: TermsStatus
    terms_url: str | None = None
    terms_reviewed_at: datetime | None = None
    terms_decision: TermsDecision
    terms_decision_actor: str | None = None
    terms_decision_note: str | None = None
    technical_denial_policy: str
    robots_policy: str
    allowed_hosts: tuple[str, ...]
    allowed_search_paths: tuple[str, ...]
    allowed_detail_paths: tuple[str, ...]
    source_timezone: str
    detail_mode: Literal["fetch", "card_only"]
    config: dict[str, Any]
    paused: bool
    pause_reason: str | None = None
    paused_by: UUID | None = None
    paused_at: datetime | None = None
    last_live_smoke_at: datetime | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    @field_validator("terms_reviewed_at", "paused_at", "last_live_smoke_at", "created_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    def source_config(self) -> SourceConfig:
        """The registry entry as a `SourceConfig` (typed columns win; ``enabled`` is False so the
        activation guard cannot reject it -- use `activation_problems` for the gate)."""
        data = {k: v for k, v in self.config.items() if k not in _TYPED_CONFIG_KEYS}
        data.update(
            {
                "source_key": self.source_key,
                "display_name": self.display_name,
                "country": self.country,
                "role": self.role,
                "mode": self.mode,
                "adapter": self.adapter,
                "adapter_version": self.adapter_version,
                "enabled": False,
                "technical_status": self.technical_status,
                "terms_status": self.terms_status,
                "terms_url": self.terms_url,
                "terms_reviewed_at": self.terms_reviewed_at,
                "terms_decision": self.terms_decision,
                "terms_decision_actor": self.terms_decision_actor,
                "terms_decision_note": self.terms_decision_note,
                "technical_denial_policy": self.technical_denial_policy,
                "robots_policy": self.robots_policy,
                "allowed_hosts": self.allowed_hosts,
                "allowed_search_paths": self.allowed_search_paths,
                "allowed_detail_paths": self.allowed_detail_paths,
                "source_timezone": self.source_timezone,
                "detail_mode": self.detail_mode,
            }
        )
        try:
            return SourceConfig.model_validate(data)
        except ValidationError as exc:
            raise ValidationFailed("the stored source configuration is invalid") from exc

    def activation_problems(self) -> list[str]:
        try:
            return activation_problems(self.source_config())
        except ValidationFailed:
            return ["stored source configuration is invalid"]

    def rate_budget(self) -> RateBudget:
        try:
            return RateBudget.model_validate(self.config.get("rate_budget") or {})
        except ValidationError:
            return RateBudget()

    def tracking_params(self) -> tuple[str, ...]:
        raw = self.config.get("tracking_params")
        if isinstance(raw, list | tuple) and all(isinstance(p, str) for p in raw):
            return tuple(raw)
        default: tuple[str, ...] = SourceConfig.model_fields["tracking_params"].default
        return default

    def budget_hosts(self) -> tuple[str, ...]:
        hosts = {h.strip().lower().rstrip(".") for h in self.allowed_hosts}
        return tuple(sorted(h for h in hosts if _HOST_RE.fullmatch(h) and h[0] not in ".-"))


class SourceSyncReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    not_enabled: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    preserved_runtime_status: dict[str, TechnicalStatus] = Field(default_factory=dict)
    terms_changed: tuple[str, ...] = ()
    disabled_missing: tuple[str, ...] = ()


class SourceRoute(BaseModel):
    """The request path a block applies to: a host and optionally the purpose and path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1, max_length=253)
    purpose: FetchPurpose | None = None
    path: str | None = Field(default=None, max_length=300)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        host = value.strip().lower().rstrip(".")
        if not _HOST_RE.fullmatch(host) or host[0] in ".-":
            raise ValueError("route host must be a DNS name")
        return host

    def label(self) -> str:
        parts = [self.host]
        if self.purpose:
            parts.append(self.purpose)
        if self.path:
            parts.append(redact(self.path)[:120])
        return ":".join(parts)


class AccessBlockResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    source_version: int
    review_item_id: UUID
    review_item_capability: str
    created: bool
    already_blocked: bool


class RobotsRevisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: UUID
    host: str
    content_hash: str | None
    changed: bool
    previous_hash: str | None
    affected_source_ids: tuple[UUID, ...]


class ScheduleRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    source_id: UUID
    profile_id: UUID
    partition_key: str
    interval_seconds: int
    next_due_at: datetime
    last_slot: datetime | None = None
    cursor: dict[str, Any] | None = None
    run_id: UUID | None = None
    coverage_mode: CoverageMode
    complete_watermark: datetime | None = None
    page_depth: int | None = None
    last_complete_traversal_at: datetime | None = None
    incomplete_since: datetime | None = None
    gap_reasons: tuple[str, ...] = ()
    backoff_until: datetime | None = None
    consecutive_failures: int
    paused: bool
    pause_reason: str | None = None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "next_due_at",
        "last_slot",
        "complete_watermark",
        "last_complete_traversal_at",
        "incomplete_since",
        "backoff_until",
        "created_at",
        "updated_at",
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_validator("gap_reasons", mode="before")
    @classmethod
    def _gaps(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(str(v) for v in value)
        return value

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self.interval_seconds)


ScheduleOutcome = Literal["enqueued", "already_scheduled", "not_due", "skipped"]


class ScheduleAdvance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: UUID
    outcome: ScheduleOutcome
    slot: datetime
    job_id: UUID | None = None
    skip_reason: str | None = None
    next_due_at: datetime


class CrawlRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    source_id: UUID
    profile_id: UUID | None = None
    partition_key: str
    job_id: UUID | None = None
    build_id: str | None = None
    adapter_version: str
    parser_version: str | None = None
    crawler_version: str | None = None
    coverage_mode: CoverageMode
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str
    pages_fetched: int
    cards_seen: int
    new_listings: int
    changed_listings: int
    detail_jobs_enqueued: int
    detail_jobs_deduplicated: int
    result_count_reported: int | None = None
    page_depth: int | None = None
    watermark_from: datetime | None = None
    watermark_to: datetime | None = None
    gap_reasons: tuple[str, ...] = ()
    access_state: AccessState | None = None
    error_code: str | None = None

    @field_validator("started_at", "finished_at", "watermark_from", "watermark_to")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_validator("gap_reasons", mode="before")
    @classmethod
    def _gaps(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(str(v) for v in value)
        return value

    @property
    def running(self) -> bool:
        return self.outcome == "running"


class RunOutcome(BaseModel):
    """How a discovery traversal ended (reported by the discovery worker)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    completeness: Completeness | Literal["cancelled"]
    # Watermark mode only: the newest provider modification time covered by this traversal.
    watermark_to: datetime | None = None
    # Resume point for an incomplete traversal (provider cursor / next URL / page number).
    cursor: dict[str, Any] | None = None
    gap_reasons: tuple[str, ...] = Field(default=(), max_length=20)
    access_state: AccessState | None = None
    error_code: str | None = Field(default=None, max_length=80)

    @field_validator("watermark_to")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class RunFinish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: CrawlRunRecord
    schedule: ScheduleRecord | None
    watermark_advanced: bool


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _require_system_or_admin(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system workers or the owner may perform this operation")


def _text(value: str | None, limit: int, *, minimum: int = 0, name: str) -> str | None:
    if value is None:
        return None
    cleaned = redact(str(value)).strip()
    if len(cleaned) < minimum:
        raise ValidationFailed(f"{name} must have at least {minimum} characters")
    return cleaned[:limit] or None


def _gap(reason: str) -> str:
    return (redact(reason).strip() or "unspecified gap")[:MAX_GAP_REASON_CHARS]


def _merge_gaps(existing: Sequence[str], new: Sequence[str]) -> list[str]:
    merged = list(existing)
    for reason in new:
        clean = _gap(reason)
        if merged and merged[-1] == clean:
            continue
        merged.append(clean)
    return merged[-MAX_GAP_REASONS:]


def _where(table: str, columns: Sequence[str], where: str, lock: str) -> sql.Composable:
    """``select <columns> from <table> where workspace_id = ... and <where><lock>``.

    ``table``, ``where`` and ``lock`` are fixed literals of this module (never caller input).
    """
    return sql.SQL(
        "select {columns} from {table} where workspace_id = %(workspace_id)s and {where}{lock}"
    ).format(
        columns=_cols(columns),
        table=sql.Identifier(*table.split(".")),
        where=sql.SQL(where),
        lock=sql.SQL(lock),
    )


def _source_select(where: str, *, lock: str = "") -> sql.Composable:
    return _where("app.sources", _SOURCE_COLUMNS, where, lock)


async def _load_source(conn: Conn, workspace_id: UUID, source_id: UUID, *, lock: str = "") -> SourceRecord:
    row = await fetch_one(
        conn, _source_select("id = %(id)s", lock=lock), {"workspace_id": workspace_id, "id": source_id}
    )
    if row is None:
        raise NotFound("Source not found")
    return SourceRecord.model_validate(row)


def _sync_values(cfg: SourceConfig) -> dict[str, Any]:
    return {
        "display_name": cfg.display_name,
        "country": cfg.country,
        "role": cfg.role,
        "mode": cfg.mode.value,
        "adapter": cfg.adapter,
        "adapter_version": cfg.adapter_version,
        "enabled": cfg.enabled,
        "technical_status": cfg.technical_status.value,
        "terms_status": cfg.terms_status.value,
        "terms_url": cfg.terms_url,
        "terms_reviewed_at": _utc(cfg.terms_reviewed_at),
        "terms_decision": cfg.terms_decision.value,
        "terms_decision_actor": cfg.terms_decision_actor,
        "terms_decision_note": cfg.terms_decision_note,
        "technical_denial_policy": cfg.technical_denial_policy,
        "robots_policy": cfg.robots_policy,
        "allowed_hosts": list(cfg.allowed_hosts),
        "allowed_search_paths": list(cfg.allowed_search_paths),
        "allowed_detail_paths": list(cfg.allowed_detail_paths),
        "source_timezone": cfg.source_timezone,
        "detail_mode": cfg.detail_mode,
        "config": cfg.model_dump(mode="json"),
    }


def _comparable(record: SourceRecord) -> dict[str, Any]:
    data = record.model_dump(mode="python")
    values = {name: data[name] for name in _SYNC_COLUMNS}
    for name in ("allowed_hosts", "allowed_search_paths", "allowed_detail_paths"):
        values[name] = list(values[name])
    for name in ("mode", "technical_status", "terms_status", "terms_decision"):
        values[name] = str(getattr(values[name], "value", values[name]))
    return values


def _effective_config(
    cfg: SourceConfig, existing: SourceRecord | None
) -> tuple[SourceConfig, TechnicalStatus | None]:
    """The YAML entry with runtime safety states applied (status preserved, enabled gated)."""
    preserved: TechnicalStatus | None = None
    status = cfg.technical_status
    if (
        existing is not None
        and existing.technical_status in RUNTIME_BLOCKING_STATUSES
        and cfg.technical_status != existing.technical_status
    ):
        preserved = status = existing.technical_status
    candidate = cfg.model_copy(update={"technical_status": status, "enabled": False})
    enabled = cfg.enabled and not activation_problems(candidate)
    return candidate.model_copy(update={"enabled": enabled}), preserved


# --------------------------------------------------------------------------------------------
# Registry sync and reads
# --------------------------------------------------------------------------------------------

_INSERT_SOURCE_SQL: Final = sql.SQL(
    "insert into app.sources (workspace_id, source_key, {columns})"
    " values (%(workspace_id)s, %(source_key)s, {values}) returning {returning}"
).format(
    columns=_cols(_SYNC_COLUMNS),
    values=sql.SQL(", ").join(sql.Placeholder(c) for c in _SYNC_COLUMNS),
    returning=_cols(_SOURCE_COLUMNS),
)
_UPDATE_SOURCE_SQL: Final = sql.SQL(
    "update app.sources set {assignments}, version = version + 1"
    " where workspace_id = %(workspace_id)s and id = %(id)s returning {returning}"
).format(
    assignments=sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder(c)) for c in _SYNC_COLUMNS
    ),
    returning=_cols(_SOURCE_COLUMNS),
)


def _sync_params(values: Mapping[str, Any]) -> dict[str, Any]:
    params = dict(values)
    params["config"] = Jsonb(params["config"])
    return params


async def sync_sources_from_yaml(
    conn: Conn, actor: ActorContext, configs: Sequence[SourceConfig]
) -> SourceSyncReport:
    """Upsert the registry from validated YAML entries (system deploy step or owner); audited."""
    _require_system_or_admin(actor)
    keys = [c.source_key for c in configs]
    if len(keys) != len(set(keys)):
        raise ValidationFailed("duplicate source_key in the source configuration")
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    terms_changed: list[str] = []
    not_enabled: dict[str, tuple[str, ...]] = {}
    preserved: dict[str, TechnicalStatus] = {}
    disabled_missing: list[str] = []
    ws = actor.workspace_id
    async with mapped_errors():
        rows = await fetch_all(
            conn, _source_select("true", lock=" order by source_key for update"), {"workspace_id": ws}
        )
        existing = {r["source_key"]: SourceRecord.model_validate(r) for r in rows}
        for cfg in sorted(configs, key=lambda c: c.source_key):
            current = existing.get(cfg.source_key)
            effective, kept = _effective_config(cfg, current)
            if kept is not None:
                preserved[cfg.source_key] = kept
            if cfg.enabled and not effective.enabled:
                not_enabled[cfg.source_key] = tuple(activation_problems(effective)) or (
                    "runtime safety state",
                )
            values = _sync_values(effective)
            if current is None:
                row = await fetch_one(
                    conn,
                    _INSERT_SOURCE_SQL,
                    {**_sync_params(values), "workspace_id": ws, "source_key": cfg.source_key},
                )
                assert row is not None
                record = SourceRecord.model_validate(row)
                created.append(cfg.source_key)
                await audit.record(
                    conn,
                    actor,
                    "source.sync_create",
                    "source",
                    record.id,
                    new_version=record.version,
                    metadata={"source_key": cfg.source_key, "enabled": record.enabled},
                )
                continue
            before = _comparable(current)
            changed = sorted(name for name in _SYNC_COLUMNS if before[name] != values[name])
            if not changed:
                unchanged.append(cfg.source_key)
                continue
            if {"terms_status", "terms_decision", "terms_url", "terms_reviewed_at"} & set(changed):
                terms_changed.append(cfg.source_key)
            row = await fetch_one(
                conn, _UPDATE_SOURCE_SQL, {**_sync_params(values), "workspace_id": ws, "id": current.id}
            )
            assert row is not None
            record = SourceRecord.model_validate(row)
            updated.append(cfg.source_key)
            await audit.record(
                conn,
                actor,
                "source.sync_update",
                "source",
                record.id,
                prior_version=current.version,
                new_version=record.version,
                metadata={
                    "source_key": cfg.source_key,
                    "changed_fields": changed,
                    "enabled": record.enabled,
                    "technical_status": record.technical_status.value,
                    "terms_decision": record.terms_decision.value,
                },
            )
        for key, current in sorted(existing.items()):
            if key in keys or not current.enabled:
                continue
            row = await fetch_one(
                conn,
                sql.SQL(
                    "update app.sources set enabled = false, version = version + 1"
                    " where workspace_id = %(workspace_id)s and id = %(id)s returning {returning}"
                ).format(returning=_cols(_SOURCE_COLUMNS)),
                {"workspace_id": ws, "id": current.id},
            )
            assert row is not None
            disabled_missing.append(key)
            await audit.record(
                conn,
                actor,
                "source.sync_disable_missing",
                "source",
                current.id,
                prior_version=current.version,
                new_version=int(row["version"]),
                reason="source no longer present in the source configuration",
                metadata={"source_key": key},
            )
    return SourceSyncReport(
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        not_enabled=not_enabled,
        preserved_runtime_status=preserved,
        terms_changed=tuple(terms_changed),
        disabled_missing=tuple(disabled_missing),
    )


async def get_source_record(conn: Conn, actor: ActorContext, source_id: UUID) -> SourceRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        return await _load_source(conn, actor.workspace_id, source_id)


async def get_source_by_key(conn: Conn, actor: ActorContext, source_key: str) -> SourceRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _source_select("source_key = %(key)s"),
            {"workspace_id": actor.workspace_id, "key": source_key},
        )
    if row is None:
        raise NotFound("Source not found")
    return SourceRecord.model_validate(row)


def run_state(record: SourceRecord, *, scanned: bool) -> SourceRunState:
    """Never scanned, paused, blocked, unhealthy and disabled stay distinct (spec 30)."""
    if record.paused:
        return SourceRunState.PAUSED
    if record.technical_status == TechnicalStatus.ACCESS_BLOCKED:
        return SourceRunState.BLOCKED
    if record.technical_status == TechnicalStatus.PARSER_UNHEALTHY:
        return SourceRunState.PARSER_UNHEALTHY
    if not record.enabled:
        return SourceRunState.DISABLED
    return SourceRunState.RUNNING if scanned else SourceRunState.NOT_SCANNED


async def _parser_health(conn: Conn, record: SourceRecord) -> ParserHealthView:
    row = await fetch_one(
        conn,
        "select metadata, occurred_at from ops.audit_events"
        " where workspace_id = %(workspace_id)s and target_type = 'source' and target_id = %(id)s"
        " and action = 'source.technical_status' order by occurred_at desc, id desc limit 1",
        {"workspace_id": record.workspace_id, "id": record.id},
    )
    health = None if row is None else row["metadata"].get("parser_health")
    status: Literal["healthy", "degraded", "unhealthy", "insufficient_sample", "unknown"] = "unknown"
    if record.technical_status == TechnicalStatus.PARSER_UNHEALTHY:
        status = "unhealthy"
    elif isinstance(health, dict) and health.get("status") in ("healthy", "degraded", "insufficient_sample"):
        status = health["status"]
    elif record.technical_status == TechnicalStatus.DEGRADED:
        status = "degraded"
    if not isinstance(health, dict):
        return ParserHealthView(
            status=status, sample_size=0, reasons=(), recommended_actions=(), checked_at=None
        )
    return ParserHealthView(
        status=status,
        sample_size=max(int(health.get("sample_size") or 0), 0),
        reasons=tuple(str(r)[:300] for r in health.get("reasons", ()))[:50],
        recommended_actions=tuple(str(a)[:120] for a in health.get("recommended_actions", ()))[:10],
        checked_at=_utc(row["occurred_at"]) if row is not None else None,
    )


async def _robots_view(conn: Conn, record: SourceRecord) -> RobotsView:
    hosts = list(record.budget_hosts())
    row = None
    if hosts:
        row = await fetch_one(
            conn,
            "select fetched_at, http_status, content_hash from ops.robots_revisions"
            " where workspace_id = %(workspace_id)s and host = any(%(hosts)s::text[])"
            " order by fetched_at desc, id desc limit 1",
            {"workspace_id": record.workspace_id, "hosts": hosts},
        )
    summary = record.config.get("robots_summary")
    return RobotsView(
        last_checked_at=None if row is None else _utc(row["fetched_at"]),
        revision_hash=None if row is None else row["content_hash"],
        fetch_status=None if row is None else row["http_status"],
        summary=summary[:2000] if isinstance(summary, str) else None,
    )


async def _budget_view(conn: Conn, record: SourceRecord) -> RateBudgetView:
    hosts = list(record.budget_hosts())
    rows = []
    if hosts:
        rows = await fetch_all(
            conn,
            "select budget_day, requests_today, bytes_today, circuit_state, access_blocked_at,"
            " next_request_not_before, retry_after_until,"
            " (clock_timestamp() at time zone 'UTC')::date as today"
            " from ops.host_budgets where workspace_id = %(workspace_id)s and host = any(%(hosts)s::text[])",
            {"workspace_id": record.workspace_id, "hosts": hosts},
        )
    if not rows:
        return RateBudgetView(
            budget=record.rate_budget(),
            requests_today=None,
            bytes_today=None,
            circuit_state="unknown",
            next_request_not_before=None,
            retry_after_until=None,
        )
    today_rows = [r for r in rows if r["budget_day"] == r["today"]]
    states = {r["circuit_state"] for r in rows}
    circuit: Literal["closed", "open", "half_open", "unknown"] = "closed"
    if "open" in states or any(r["access_blocked_at"] is not None for r in rows):
        circuit = "open"
    elif "half_open" in states:
        circuit = "half_open"
    not_before = [r["next_request_not_before"] for r in rows if r["next_request_not_before"] is not None]
    retry = [r["retry_after_until"] for r in rows if r["retry_after_until"] is not None]
    return RateBudgetView(
        budget=record.rate_budget(),
        requests_today=sum(int(r["requests_today"]) for r in today_rows),
        bytes_today=sum(int(r["bytes_today"]) for r in today_rows),
        circuit_state=circuit,
        next_request_not_before=_utc(max(not_before)) if not_before else None,
        retry_after_until=_utc(max(retry)) if retry else None,
    )


async def _recent_runs(conn: Conn, record: SourceRecord, limit: int = 5) -> list[CrawlRunView]:
    rows = await fetch_all(
        conn,
        sql.SQL(
            "select {columns}, p.profile_key from ops.crawl_runs r"
            " left join app.search_profiles p on p.workspace_id = r.workspace_id and p.id = r.profile_id"
            " where r.workspace_id = %(workspace_id)s and r.source_id = %(id)s"
            " order by r.started_at desc, r.id desc limit %(limit)s"
        ).format(columns=_cols(_RUN_COLUMNS, "r")),
        {"workspace_id": record.workspace_id, "id": record.id, "limit": limit},
    )
    views = []
    for row in rows:
        run = CrawlRunRecord.model_validate(row)
        views.append(
            CrawlRunView(
                run_id=run.id,
                profile=None if row["profile_key"] is None else ProfileKey(row["profile_key"]),
                partition_key=run.partition_key,
                coverage_mode=run.coverage_mode,
                started_at=run.started_at,
                finished_at=run.finished_at,
                outcome=run.outcome,
                pages_fetched=run.pages_fetched,
                cards_seen=run.cards_seen,
                new_listings=run.new_listings,
                changed_listings=run.changed_listings,
                detail_jobs_enqueued=run.detail_jobs_enqueued,
                detail_jobs_deduplicated=run.detail_jobs_deduplicated,
                access_state=run.access_state,
                error_code=run.error_code,
                gap_reasons=tuple(run.gap_reasons)[:50],
                adapter_version=run.adapter_version[:40],
                parser_version=run.parser_version,
            )
        )
    return views


async def _scanned(conn: Conn, record: SourceRecord) -> bool:
    row = await fetch_one(
        conn,
        "select exists (select 1 from ops.crawl_runs where workspace_id = %(workspace_id)s"
        " and source_id = %(id)s and outcome in ('complete', 'budget_limited', 'partial')) as scanned",
        {"workspace_id": record.workspace_id, "id": record.id},
    )
    return bool(row and row["scanned"])


async def _status_view(conn: Conn, record: SourceRecord) -> SourceStatusView:
    problems = record.activation_problems()
    return SourceStatusView(
        source_id=record.id,
        source_key=record.source_key,
        display_name=record.display_name,
        country=record.country,
        role=record.role,
        state=run_state(record, scanned=await _scanned(conn, record)),
        enabled=record.enabled,
        paused=record.paused,
        pause_reason=record.pause_reason,
        paused_at=record.paused_at,
        version=record.version,
        terms=TermsView(
            status=record.terms_status,
            decision=record.terms_decision,
            decision_actor=record.terms_decision_actor,
            decision_note=record.terms_decision_note,
            reviewed_at=record.terms_reviewed_at,
            terms_url=record.terms_url,
        ),
        technical=TechnicalView(
            status=record.technical_status,
            mode=record.mode,
            adapter=record.adapter,
            adapter_version=record.adapter_version,
            detail_mode=record.detail_mode,
            last_live_smoke_at=record.last_live_smoke_at,
            parser_health=await _parser_health(conn, record),
        ),
        robots=await _robots_view(conn, record),
        rate_budget=await _budget_view(conn, record),
        last_runs=tuple(await _recent_runs(conn, record)),
        activation_problems=() if record.enabled else tuple(p[:300] for p in problems)[:50],
    )


async def get_source(conn: Conn, actor: ActorContext, source_id: UUID) -> SourceStatusView:
    """``SourceStatusView`` for the dashboard/MCP (``deals:read``); `NotFound` across workspaces."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        record = await _load_source(conn, actor.workspace_id, source_id)
        return await _status_view(conn, record)


async def list_sources(conn: Conn, actor: ActorContext) -> SourceListView:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _source_select("true", lock=" order by source_key limit 200"),
            {"workspace_id": actor.workspace_id},
        )
        items = [await _status_view(conn, SourceRecord.model_validate(r)) for r in rows]
    return SourceListView(items=tuple(items))


# --------------------------------------------------------------------------------------------
# Pause, resume, access blocks, technical status
# --------------------------------------------------------------------------------------------


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed", retryable=False)


async def pause_source(  # noqa: PLR0917 - mirrors the sources_pause tool input
    conn: Conn,
    actor: ActorContext,
    source_id: UUID,
    expected_version: int,
    reason: str,
    idempotency_key: str,
) -> SourcePauseResult:
    """``sources_pause``: pause one source with a reason; never resumes or enables anything.

    Same key + same request returns the original result; a stale ``expected_version`` is a
    `VersionConflict`. Pausing an already paused source at the expected version is a no-op that
    reports ``already_paused=True``.
    """
    actor.require(Scope.SOURCES_PAUSE)
    try:
        request = SourcesPauseInput(
            source_id=source_id,
            expected_version=expected_version,
            reason=reason,
            idempotency_key=idempotency_key,
        )
    except ValidationError as exc:
        raise ValidationFailed("invalid sources_pause request") from exc
    request_hash = idempotency.request_hash_for(PAUSE_OPERATION, request)
    clean_reason = _text(request.reason, 2000, minimum=3, name="reason")
    assert clean_reason is not None
    async with mapped_errors():
        started = await idempotency.begin(conn, actor, PAUSE_OPERATION, request.idempotency_key, request_hash)
        if isinstance(started, idempotency.Replay):
            return SourcePauseResult.model_validate(started.result)
        if isinstance(started, idempotency.ReplayError):
            raise _replay_error(started.error_code)
        if isinstance(started, idempotency.InProgress):
            raise TransientConflict.in_progress()
        source = await _load_source(conn, actor.workspace_id, request.source_id, lock=" for update")
        if source.version != request.expected_version:
            raise VersionConflict("The source changed; reload and retry", current_version=source.version)
        if source.paused:
            assert source.paused_at is not None and source.pause_reason is not None
            result = SourcePauseResult(
                source_id=source.id,
                source_key=source.source_key,
                already_paused=True,
                version=source.version,
                paused_at=source.paused_at,
                reason=source.pause_reason,
            )
        else:
            row = await fetch_one(
                conn,
                "update app.sources set paused = true, pause_reason = %(reason)s, paused_by = %(by)s,"
                " paused_at = clock_timestamp(), version = version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s and version = %(version)s"
                " returning version, paused_at",
                {
                    "workspace_id": actor.workspace_id,
                    "id": source.id,
                    "version": source.version,
                    "reason": clean_reason,
                    "by": actor.principal_id,
                },
            )
            if row is None:  # pragma: no cover - the row is locked above
                raise VersionConflict("The source changed concurrently")
            result = SourcePauseResult(
                source_id=source.id,
                source_key=source.source_key,
                already_paused=False,
                version=int(row["version"]),
                paused_at=ensure_utc(row["paused_at"]),
                reason=clean_reason,
            )
            await audit.record(
                conn,
                actor,
                "source.pause",
                "source",
                source.id,
                prior_version=source.version,
                new_version=result.version,
                reason=clean_reason,
                metadata={"source_key": source.source_key},
            )
        await idempotency.complete(
            conn, actor, PAUSE_OPERATION, request.idempotency_key, result.model_dump(mode="json")
        )
    return result


async def resume_source(
    conn: Conn, actor: ActorContext, source_id: UUID, expected_version: int, *, reason: str
) -> SourceRecord:
    """Explicit owner action (``config:admin``): clear a pause. Never enables a disabled source."""
    actor.require(Scope.CONFIG_ADMIN)
    clean_reason = _text(reason, 2000, minimum=3, name="reason")
    async with mapped_errors():
        source = await _load_source(conn, actor.workspace_id, source_id, lock=" for update")
        if source.version != expected_version:
            raise VersionConflict("The source changed; reload and retry", current_version=source.version)
        if not source.paused:
            return source
        row = await fetch_one(
            conn,
            sql.SQL(
                "update app.sources set paused = false, pause_reason = null, paused_by = null,"
                " paused_at = null, version = version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s returning {returning}"
            ).format(returning=_cols(_SOURCE_COLUMNS)),
            {"workspace_id": actor.workspace_id, "id": source.id},
        )
        assert row is not None
        record = SourceRecord.model_validate(row)
        await audit.record(
            conn,
            actor,
            "source.resume",
            "source",
            source.id,
            prior_version=source.version,
            new_version=record.version,
            reason=clean_reason,
            metadata={"source_key": source.source_key, "prior_pause_reason": source.pause_reason},
        )
    return record


def access_gate_capability(source_key: str) -> str:
    return f"{ACCESS_GATE_PREFIX}{source_key}"


async def record_access_block(
    conn: Conn,
    actor: ActorContext,
    source_id: UUID,
    route: SourceRoute,
    evidence: str | Mapping[str, Any] | None,
) -> AccessBlockResult:
    """Stop the source's request paths after a 401/403/CAPTCHA/login/paywall (spec 5, 9, 30).

    Sets the technical status ``access_blocked`` (which disables network work and, by the
    activation CHECK, the ``enabled`` flag) and opens ONE operational review item, the activation
    gate ``source_access.<key>`` in status ``blocked``. A source that is already blocked gets no
    second item. Evidence is redacted and bounded.
    """
    _require_system_or_admin(actor)
    detail = (
        evidence
        if isinstance(evidence, str) or evidence is None
        else ", ".join(f"{k}={v}" for k, v in sorted(evidence.items()))
    )
    clean_evidence = _text(detail, 500, name="evidence") or "access_blocked"
    async with mapped_errors():
        source = await _load_source(conn, actor.workspace_id, source_id, lock=" for update")
        capability = access_gate_capability(source.source_key)
        existing_gate = await fetch_one(
            conn,
            "select id, status from ops.activation_gates where workspace_id = %(workspace_id)s"
            " and capability = %(capability)s",
            {"workspace_id": actor.workspace_id, "capability": capability},
        )
        already = source.technical_status == TechnicalStatus.ACCESS_BLOCKED
        if already and existing_gate is not None and existing_gate["status"] == GateStatus.BLOCKED.value:
            return AccessBlockResult(
                source_id=source.id,
                source_version=source.version,
                review_item_id=existing_gate["id"],
                review_item_capability=capability,
                created=False,
                already_blocked=True,
            )
        version = source.version
        if not already:
            row = await fetch_one(
                conn,
                "update app.sources set technical_status = 'access_blocked', enabled = false,"
                " version = version + 1 where workspace_id = %(workspace_id)s and id = %(id)s"
                " returning version",
                {"workspace_id": actor.workspace_id, "id": source.id},
            )
            assert row is not None
            version = int(row["version"])
        gate = await gates.upsert_gate(
            conn,
            actor,
            capability,
            f"Permitted access to source {source.source_key} (route {route.label()})",
            "Permitted access restored, then a low-rate live smoke passes;"
            " recovery is an explicit owner action",
            GateStatus.BLOCKED,
            {
                "source_id": str(source.id),
                "route": route.label(),
                "evidence": clean_evidence,
                "technical_status_before": source.technical_status.value,
                "enabled_before": source.enabled,
            },
            owner="owner",
            next_action=(
                "Review the block evidence; do not evade it. When permitted access is restored, clear the "
                "technical status (owner) and run a low-rate live smoke before re-enabling."
            ),
        )
        await audit.record(
            conn,
            actor,
            "source.access_blocked",
            "source",
            source.id,
            prior_version=source.version,
            new_version=version,
            reason=f"access blocked on route {route.label()}",
            metadata={
                "source_key": source.source_key,
                "route": route.label(),
                "evidence": clean_evidence,
                "enabled_before": source.enabled,
                "review_item_id": str(gate.id),
            },
        )
    return AccessBlockResult(
        source_id=source.id,
        source_version=version,
        review_item_id=gate.id,
        review_item_capability=capability,
        created=existing_gate is None or existing_gate["status"] != GateStatus.BLOCKED.value,
        already_blocked=already,
    )


async def set_technical_status(
    conn: Conn,
    actor: ActorContext,
    source_id: UUID,
    status: TechnicalStatus,
    *,
    reason: str,
    expected_version: int | None = None,
    parser_health: ParserHealth | None = None,
) -> SourceRecord:
    """Record a technical status change (parser-health incident, smoke result, recovery).

    Degrading changes (``degraded``, ``parser_unhealthy``, ``access_blocked``) may come from system
    checks; ``parser_unhealthy``/``access_blocked`` disable the source (activation CHECK) and pause
    new alerts. Leaving ``access_blocked`` or ``parser_unhealthy`` is an explicit owner action
    (``config:admin``). ``live_smoke_passed`` stamps ``last_live_smoke_at``. Never re-enables.
    """
    _require_system_or_admin(actor)
    status = TechnicalStatus(status)
    clean_reason = _text(reason, 1000, minimum=3, name="reason")
    async with mapped_errors():
        source = await _load_source(conn, actor.workspace_id, source_id, lock=" for update")
        if expected_version is not None and source.version != expected_version:
            raise VersionConflict("The source changed; reload and retry", current_version=source.version)
        if source.technical_status in RUNTIME_BLOCKING_STATUSES and status != source.technical_status:
            actor.require(Scope.CONFIG_ADMIN)
        health = None if parser_health is None else parser_health.model_dump(mode="json")
        if status == source.technical_status and health is None:
            return source
        blocking = status in RUNTIME_BLOCKING_STATUSES or status == TechnicalStatus.UNTESTED
        row = await fetch_one(
            conn,
            sql.SQL(
                "update app.sources set technical_status = %(status)s,"
                " enabled = case when %(blocking)s then false else enabled end,"
                " last_live_smoke_at = case when %(smoke)s then clock_timestamp()"
                "   else last_live_smoke_at end,"
                " version = version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s returning {returning}"
            ).format(returning=_cols(_SOURCE_COLUMNS)),
            {
                "workspace_id": actor.workspace_id,
                "id": source.id,
                "status": status.value,
                "blocking": blocking,
                "smoke": status == TechnicalStatus.LIVE_SMOKE_PASSED,
            },
        )
        assert row is not None
        record = SourceRecord.model_validate(row)
        metadata: dict[str, Any] = {
            "source_key": source.source_key,
            "prior_status": source.technical_status.value,
            "status": status.value,
            "enabled_before": source.enabled,
            "enabled": record.enabled,
            "alerts_paused": status in RUNTIME_BLOCKING_STATUSES,
        }
        if health is not None:
            metadata["parser_health"] = health
        await audit.record(
            conn,
            actor,
            "source.technical_status",
            "source",
            source.id,
            prior_version=source.version,
            new_version=record.version,
            reason=clean_reason,
            metadata=metadata,
        )
    return record


async def alert_pause_reason(conn: Conn, actor: ActorContext, source_id: UUID) -> str | None:
    """Why new opportunity alerts from this source must not be sent now (None = alerts allowed).

    Spec 25: on suspected parser drift (``degraded``) new opportunity alerts from the adapter pause
    just like for an unhealthy parser; prior evidence is kept and nothing is removed.
    """
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        source = await _load_source(conn, actor.workspace_id, source_id)
    if source.paused:
        return "source_paused"
    if source.technical_status == TechnicalStatus.PARSER_UNHEALTHY:
        return "parser_unhealthy"
    if source.technical_status == TechnicalStatus.DEGRADED:
        return "parser_degraded"
    if source.technical_status == TechnicalStatus.ACCESS_BLOCKED:
        return "access_blocked"
    return None


# --------------------------------------------------------------------------------------------
# Robots revisions and fetch attempts
# --------------------------------------------------------------------------------------------


def _clean_host(host: str) -> str:
    value = (host or "").strip().lower().rstrip(".")
    if not _HOST_RE.fullmatch(value) or value[0] in ".-":
        raise ValidationFailed("host must be a lower-case DNS name")
    return value


def _bounded_body(body: str | None) -> tuple[str | None, bool]:
    if body is None:
        return None, False
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_ROBOTS_BODY_BYTES:
        return body, False
    return encoded[:MAX_ROBOTS_BODY_BYTES].decode("utf-8", errors="ignore"), True


async def record_robots_revision(
    conn: Conn,
    actor: ActorContext,
    *,
    host: str,
    fetched_at: datetime,
    http_status: int | None,
    body: str | None,
    parse_ok: bool,
    user_agent: str | None = None,
) -> RobotsRevisionResult:
    """Store one robots.txt fetch (bounded body + SHA-256 of the full body); report changes.

    A changed robots revision invalidates stale activation evidence (spec 5): the affected
    sources get a ``source.robots_changed`` audit event for review; nothing is auto-enabled.
    """
    _require_system_or_admin(actor)
    name = _clean_host(host)
    if http_status is not None and not 100 <= http_status <= 599:
        raise ValidationFailed("http_status must be an HTTP status code")
    content_hash = None if body is None else hashlib.sha256(body.encode("utf-8")).hexdigest()
    stored, truncated = _bounded_body(body)
    async with mapped_errors():
        previous = await fetch_one(
            conn,
            "select content_hash from ops.robots_revisions where workspace_id = %(workspace_id)s"
            " and host = %(host)s order by fetched_at desc, id desc limit 1",
            {"workspace_id": actor.workspace_id, "host": name},
        )
        row = await fetch_one(
            conn,
            "insert into ops.robots_revisions (workspace_id, host, fetched_at, http_status, content_hash,"
            " body, body_truncated, parse_ok, user_agent)"
            " values (%(workspace_id)s, %(host)s, %(fetched_at)s, %(status)s, %(hash)s, %(body)s,"
            " %(truncated)s, %(parse_ok)s, %(ua)s) returning id",
            {
                "workspace_id": actor.workspace_id,
                "host": name,
                "fetched_at": ensure_utc(fetched_at),
                "status": http_status,
                "hash": content_hash,
                "body": stored,
                "truncated": truncated,
                "parse_ok": parse_ok,
                "ua": None if user_agent is None else user_agent[:300],
            },
        )
        assert row is not None
        previous_hash = None if previous is None else previous["content_hash"]
        changed = previous is not None and previous_hash != content_hash
        affected: list[UUID] = []
        if changed:
            sources = await fetch_all(
                conn,
                "select id, source_key, version from app.sources where workspace_id = %(workspace_id)s"
                " and %(host)s = any(allowed_hosts) order by source_key",
                {"workspace_id": actor.workspace_id, "host": name},
            )
            for source in sources:
                affected.append(source["id"])
                await audit.record(
                    conn,
                    actor,
                    "source.robots_changed",
                    "source",
                    source["id"],
                    prior_version=int(source["version"]),
                    new_version=int(source["version"]),
                    reason="robots.txt changed; stale activation evidence must be reviewed",
                    metadata={
                        "source_key": source["source_key"],
                        "host": name,
                        "previous_hash": previous_hash,
                        "content_hash": content_hash,
                    },
                )
    return RobotsRevisionResult(
        revision_id=row["id"],
        host=name,
        content_hash=content_hash,
        changed=changed,
        previous_hash=previous_hash,
        affected_source_ids=tuple(affected),
    )


def url_hash(url: str) -> str:
    """SHA-256 of a URL: raw URLs (which may embed secrets) are never stored in ops tables."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _safe_code(code: str | None) -> str | None:
    if code is None:
        return None
    if _CODE_RE.fullmatch(code):
        return code
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", code)[:80]
    return cleaned or None


async def record_fetch_attempt(
    conn: Conn,
    actor: ActorContext,
    *,
    source_id: UUID,
    purpose: FetchPurpose,
    outcome: FetchOutcome,
    job_id: UUID | None = None,
    crawl_run_id: UUID | None = None,
    snapshot_id: UUID | None = None,
) -> UUID:
    """Append the redacted fetch outcome (URL hashes only, allow-listed headers, safe codes)."""
    _require_system_or_admin(actor)
    try:
        host = _clean_host(urlsplit(outcome.requested_url).hostname or "")
    except ValueError as exc:
        raise ValidationFailed("the fetched URL has no valid host") from exc
    headers = {
        k.lower(): (redact(str(v)) or "")[:500]
        for k, v in outcome.response_headers.items()
        if k.lower() in _HEADER_ALLOWLIST
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.fetch_attempts (workspace_id, source_id, job_id, crawl_run_id, purpose,"
            " url_hash, final_url_hash, host, http_status, success, access_state, error_code, elapsed_ms,"
            " extraction_ms,"
            " bytes, redirect_count, retry_after_seconds, response_headers, crawler_version, snapshot_id,"
            " fetched_at)"
            " values (%(workspace_id)s, %(source_id)s, %(job_id)s, %(run_id)s, %(purpose)s, %(url_hash)s,"
            " %(final_hash)s, %(host)s, %(status)s, %(success)s, %(access_state)s, %(error_code)s,"
            " %(elapsed)s, %(extraction)s, %(bytes)s, %(redirects)s, %(retry_after)s, %(headers)s,"
            " %(crawler)s, %(snapshot_id)s, %(fetched_at)s) returning id",
            {
                "workspace_id": actor.workspace_id,
                "source_id": source_id,
                "job_id": job_id,
                "run_id": crawl_run_id,
                "purpose": purpose,
                "url_hash": url_hash(outcome.requested_url),
                "final_hash": None if outcome.final_url is None else url_hash(outcome.final_url),
                "host": host,
                "status": outcome.http_status
                if outcome.http_status and 100 <= outcome.http_status <= 599
                else None,
                "success": outcome.success and outcome.access_state == AccessState.OK,
                "access_state": outcome.access_state.value,
                "error_code": _safe_code(outcome.error_code),
                "elapsed": outcome.elapsed_ms,
                "extraction": outcome.extraction_ms,
                "bytes": outcome.bytes,
                "redirects": outcome.redirect_count,
                "retry_after": outcome.retry_after_seconds,
                "headers": Jsonb(headers),
                "crawler": None if outcome.crawler_version is None else outcome.crawler_version[:80],
                "snapshot_id": snapshot_id,
                "fetched_at": outcome.fetched_at,
            },
        )
    assert row is not None
    result: UUID = row["id"]
    return result


# --------------------------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------------------------


def _schedule_select(where: str, *, lock: str = "") -> sql.Composable:
    return _where("ops.source_schedules", _SCHEDULE_COLUMNS, where, lock)


async def ensure_schedule(
    conn: Conn,
    actor: ActorContext,
    source_id: UUID,
    profile_id: UUID,
    partition_key: str = "default",
    *,
    coverage_mode: CoverageMode,
    interval_seconds: int = 900,
) -> ScheduleRecord:
    """Create the schedule row of (source, profile, partition) if missing (due immediately)."""
    _require_system_or_admin(actor)
    if not _PARTITION_RE.fullmatch(partition_key):
        raise ValidationFailed("partition_key must be 1-80 characters of letters, digits and _ : . -")
    if not 60 <= interval_seconds <= 86_400:
        raise ValidationFailed("interval_seconds must be between 60 and 86400")
    params = {
        "workspace_id": actor.workspace_id,
        "source_id": source_id,
        "profile_id": profile_id,
        "partition_key": partition_key,
        "coverage_mode": CoverageMode(coverage_mode).value,
        "interval": interval_seconds,
    }
    async with mapped_errors():
        await conn.execute(
            "insert into ops.source_schedules (workspace_id, source_id, profile_id, partition_key,"
            " interval_seconds, next_due_at, coverage_mode)"
            " values (%(workspace_id)s, %(source_id)s, %(profile_id)s, %(partition_key)s, %(interval)s,"
            " now(), %(coverage_mode)s)"
            " on conflict (workspace_id, source_id, profile_id, partition_key) do nothing",
            params,
        )
        row = await fetch_one(
            conn,
            _schedule_select(
                "source_id = %(source_id)s and profile_id = %(profile_id)s"
                " and partition_key = %(partition_key)s"
            ),
            params,
        )
    assert row is not None
    return ScheduleRecord.model_validate(row)


async def get_schedule(conn: Conn, actor: ActorContext, schedule_id: UUID) -> ScheduleRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn, _schedule_select("id = %(id)s"), {"workspace_id": actor.workspace_id, "id": schedule_id}
        )
    if row is None:
        raise NotFound("Schedule not found")
    return ScheduleRecord.model_validate(row)


async def due_schedules(conn: Conn, actor: ActorContext, *, limit: int = 100) -> list[ScheduleRecord]:
    """Schedules due now (database time) whose source and profile are active (read-only)."""
    _require_system_or_admin(actor)
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            sql.SQL(
                "select {columns} from ops.source_schedules s"
                " join app.sources src on src.workspace_id = s.workspace_id and src.id = s.source_id"
                " join app.search_profiles p on p.workspace_id = s.workspace_id and p.id = s.profile_id"
                " where s.workspace_id = %(workspace_id)s and s.next_due_at <= now() and not s.paused"
                " and (s.backoff_until is null or s.backoff_until <= now())"
                " and src.enabled and not src.paused"
                " and src.technical_status not in ('untested', 'access_blocked', 'parser_unhealthy')"
                " and p.enabled"
                " order by s.next_due_at, s.id limit %(limit)s"
            ).format(columns=_cols(_SCHEDULE_COLUMNS, "s")),
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return [ScheduleRecord.model_validate(r) for r in rows]


async def _skip_reason(
    conn: Conn, actor: ActorContext, schedule: ScheduleRecord, now: datetime
) -> str | None:
    source_row = await fetch_one(
        conn,
        _source_select("id = %(id)s", lock=" for share"),
        {"workspace_id": actor.workspace_id, "id": schedule.source_id},
    )
    if source_row is None:  # pragma: no cover - composite FK
        return "source_missing"
    source = SourceRecord.model_validate(source_row)
    profile = await fetch_one(
        conn,
        "select enabled from app.search_profiles where workspace_id = %(workspace_id)s and id = %(id)s",
        {"workspace_id": actor.workspace_id, "id": schedule.profile_id},
    )
    if schedule.paused:
        return "schedule_paused"
    if source.paused:
        return "source_paused"
    if source.technical_status in NETWORK_BLOCKING_STATUSES:
        return f"technical_status_{source.technical_status.value}"
    if not source.enabled:
        return "source_disabled"
    if source.terms_decision not in (TermsDecision.PROCEED_ACKNOWLEDGED, TermsDecision.PROCEED_PERMITTED):
        return "terms_decision_missing"
    if source.activation_problems():
        return "activation_gate_failed"
    if profile is None or not profile["enabled"]:
        return "profile_disabled"
    if schedule.backoff_until is not None and schedule.backoff_until > now:
        return "backoff"
    backlog = await fetch_one(
        conn,
        "select 1 from ops.jobs where workspace_id = %(workspace_id)s and job_type = 'discovery'"
        " and source_id = %(source_id)s and profile_id = %(profile_id)s and partition_key = %(partition)s"
        " and state in ('queued', 'running', 'retry_wait') limit 1",
        {
            "workspace_id": actor.workspace_id,
            "source_id": schedule.source_id,
            "profile_id": schedule.profile_id,
            "partition": schedule.partition_key,
        },
    )
    if backlog is not None:
        return "backlog"
    hosts = list(source.budget_hosts())
    if hosts:
        usage = await fetch_one(
            conn,
            "select coalesce(sum(requests_today)"
            "   filter (where budget_day = (now() at time zone 'UTC')::date), 0) as requests,"
            " bool_or(access_blocked_at is not null) as blocked"
            " from ops.host_budgets where workspace_id = %(workspace_id)s and host = any(%(hosts)s::text[])",
            {"workspace_id": actor.workspace_id, "hosts": hosts},
        )
        if usage is not None and usage["blocked"]:
            return "host_access_blocked"
        if usage is not None and int(usage["requests"]) >= source.rate_budget().daily_request_budget:
            return "daily_budget_exhausted"
    return None


async def advance_schedule(
    conn: Conn, actor: ActorContext, schedule_id: UUID, slot: datetime | None = None
) -> ScheduleAdvance:
    """Spec 9 scheduling steps 1-4 in the caller's short transaction (see module docstring).

    Returns ``enqueued`` (job created), ``already_scheduled`` (a racing scheduler won the slot),
    ``not_due`` or ``skipped`` (with the reason; the slot is consumed and recorded as a gap).
    """
    _require_system_or_admin(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _schedule_select("id = %(id)s", lock=" for update"),
            {"workspace_id": actor.workspace_id, "id": schedule_id},
        )
        if row is None:
            raise NotFound("Schedule not found")
        schedule = ScheduleRecord.model_validate(row)
        now_row = await fetch_one(conn, "select now() as now")
        assert now_row is not None
        now = ensure_utc(now_row["now"])
        current = await jobs.current_slot(conn, schedule.interval) if slot is None else ensure_utc(slot)
        if schedule.next_due_at > now:
            return ScheduleAdvance(
                schedule_id=schedule.id, outcome="not_due", slot=current, next_due_at=schedule.next_due_at
            )
        if schedule.last_slot is not None and current <= schedule.last_slot:
            # Due by time but this slot was already handled (e.g. the interval changed): move the
            # due time to the next slot so the schedule does not stay due forever.
            due = schedule.last_slot + schedule.interval
            await conn.execute(
                "update ops.source_schedules set next_due_at = %(due)s, row_version = row_version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s",
                {"workspace_id": actor.workspace_id, "id": schedule.id, "due": due},
            )
            return ScheduleAdvance(schedule_id=schedule.id, outcome="not_due", slot=current, next_due_at=due)
        next_due = current + schedule.interval
        gaps: list[str] = []
        if schedule.last_slot is not None and current > schedule.last_slot + schedule.interval:
            gaps.append(
                f"scheduler_missed_slots: {schedule.last_slot.isoformat()} .. {current.isoformat()}"
                " not scheduled"
            )
        reason = await _skip_reason(conn, actor, schedule, now)
        job_id: UUID | None = None
        outcome: ScheduleOutcome = "skipped"
        if reason is not None:
            gaps.append(f"slot_skipped: {reason}")
        else:
            spec = jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key=f"discovery:{schedule.id}:{current.strftime('%Y%m%dT%H%M%SZ')}",
                payload={
                    "schedule_id": str(schedule.id),
                    "source_id": str(schedule.source_id),
                    "profile_id": str(schedule.profile_id),
                    "partition_key": schedule.partition_key,
                    "coverage_mode": schedule.coverage_mode.value,
                    "slot": current.isoformat(),
                    "cursor": schedule.cursor,
                    "complete_watermark": None
                    if schedule.complete_watermark is None
                    else schedule.complete_watermark.isoformat(),
                },
                max_attempts=3,
                source_id=schedule.source_id,
                profile_id=schedule.profile_id,
                partition_key=schedule.partition_key,
            )
            job_id, created = await jobs.enqueue_slot(conn, actor, spec, current)
            outcome = "enqueued" if created else "already_scheduled"
        await conn.execute(
            "update ops.source_schedules set next_due_at = %(next_due)s, last_slot = %(slot)s,"
            " gap_reasons = %(gaps)s, row_version = row_version + 1"
            " where workspace_id = %(workspace_id)s and id = %(id)s",
            {
                "workspace_id": actor.workspace_id,
                "id": schedule.id,
                "next_due": next_due,
                "slot": current,
                "gaps": Jsonb(_merge_gaps(schedule.gap_reasons, gaps)),
            },
        )
    return ScheduleAdvance(
        schedule_id=schedule.id,
        outcome=outcome,
        slot=current,
        job_id=job_id,
        skip_reason=reason,
        next_due_at=next_due,
    )


# --------------------------------------------------------------------------------------------
# Crawl runs, watermarks and coverage
# --------------------------------------------------------------------------------------------


def _run_select(where: str, *, lock: str = "") -> sql.Composable:
    return _where("ops.crawl_runs", _RUN_COLUMNS, where, lock)


async def start_crawl_run(
    conn: Conn,
    actor: ActorContext,
    *,
    source_id: UUID,
    profile_id: UUID | None,
    coverage_mode: CoverageMode,
    adapter_version: str,
    partition_key: str = "default",
    job_id: UUID | None = None,
    parser_version: str | None = None,
    crawler_version: str | None = None,
    build_id: str | None = None,
    watermark_from: datetime | None = None,
) -> CrawlRunRecord:
    """Open a traversal record; the schedule (if any) remembers the in-progress run id."""
    _require_system_or_admin(actor)
    mode = CoverageMode(coverage_mode)
    if mode == CoverageMode.ROLLING_PAGES and watermark_from is not None:
        raise ValidationFailed("rolling_pages coverage never uses a timestamp watermark")
    params = {
        "workspace_id": actor.workspace_id,
        "source_id": source_id,
        "profile_id": profile_id,
        "partition_key": partition_key,
        "job_id": job_id,
        "build_id": build_id,
        "adapter_version": adapter_version,
        "parser_version": parser_version,
        "crawler_version": crawler_version,
        "coverage_mode": mode.value,
        "watermark_from": None if watermark_from is None else ensure_utc(watermark_from),
    }
    async with mapped_errors():
        if profile_id is not None:
            existing = await fetch_one(
                conn,
                "select coverage_mode from ops.source_schedules where workspace_id = %(workspace_id)s"
                " and source_id = %(source_id)s and profile_id = %(profile_id)s"
                " and partition_key = %(partition_key)s",
                params,
            )
            if existing is not None and existing["coverage_mode"] != mode.value:
                # The schedule's coverage contract decides how the finished run is applied; a run
                # in the other mode could never be recorded (and would stay running forever).
                raise ValidationFailed("the crawl run's coverage mode differs from its schedule")
        row = await fetch_one(
            conn,
            sql.SQL(
                "insert into ops.crawl_runs (workspace_id, source_id, profile_id, partition_key, job_id,"
                " build_id, adapter_version, parser_version, crawler_version, coverage_mode, watermark_from)"
                " values (%(workspace_id)s, %(source_id)s, %(profile_id)s, %(partition_key)s, %(job_id)s,"
                " %(build_id)s, %(adapter_version)s, %(parser_version)s, %(crawler_version)s,"
                " %(coverage_mode)s, %(watermark_from)s) returning {columns}"
            ).format(columns=_cols(_RUN_COLUMNS)),
            params,
        )
        assert row is not None
        run = CrawlRunRecord.model_validate(row)
        if profile_id is not None:
            await conn.execute(
                "update ops.source_schedules set run_id = %(run_id)s, row_version = row_version + 1"
                " where workspace_id = %(workspace_id)s and source_id = %(source_id)s"
                " and profile_id = %(profile_id)s and partition_key = %(partition_key)s",
                {**params, "run_id": run.id},
            )
    return run


async def get_crawl_run(conn: Conn, actor: ActorContext, run_id: UUID) -> CrawlRunRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn, _run_select("id = %(id)s"), {"workspace_id": actor.workspace_id, "id": run_id}
        )
    if row is None:
        raise NotFound("Crawl run not found")
    return CrawlRunRecord.model_validate(row)


def _backoff(schedule: ScheduleRecord, failures: int) -> timedelta:
    delay: timedelta = schedule.interval * (2 ** min(failures, 6))
    return min(delay, MAX_BACKOFF)


async def finish_crawl_run(conn: Conn, actor: ActorContext, run_id: UUID, outcome: RunOutcome) -> RunFinish:
    """Close a traversal and apply its coverage to the schedule (watermark rules of spec 9).

    Idempotent for the same final outcome; a different outcome for a finished run is a
    `VersionConflict`.
    """
    _require_system_or_admin(actor)
    final = str(getattr(outcome.completeness, "value", outcome.completeness))
    code = _safe_code(outcome.error_code)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _run_select("id = %(id)s", lock=" for update"),
            {"workspace_id": actor.workspace_id, "id": run_id},
        )
        if row is None:
            raise NotFound("Crawl run not found")
        run = CrawlRunRecord.model_validate(row)
        if not run.running:
            if run.outcome != final:
                raise VersionConflict("The crawl run already finished with another outcome")
            return RunFinish(run=run, schedule=None, watermark_advanced=False)
        if run.coverage_mode == CoverageMode.ROLLING_PAGES and outcome.watermark_to is not None:
            raise ValidationFailed("rolling_pages coverage never records a timestamp watermark")
        watermark_to = outcome.watermark_to if run.coverage_mode == CoverageMode.WATERMARK else None
        if watermark_to is not None and run.watermark_from is not None and watermark_to < run.watermark_from:
            raise ValidationFailed("watermark_to precedes watermark_from")
        reasons = [_gap(r) for r in outcome.gap_reasons]
        updated_row = await fetch_one(
            conn,
            sql.SQL(
                "update ops.crawl_runs set outcome = %(outcome)s,"
                " finished_at = greatest(clock_timestamp(), started_at),"
                " watermark_to = %(watermark_to)s, gap_reasons = %(gaps)s,"
                " access_state = coalesce(%(access_state)s, access_state), error_code = %(error_code)s"
                " where workspace_id = %(workspace_id)s and id = %(id)s returning {columns}"
            ).format(columns=_cols(_RUN_COLUMNS)),
            {
                "workspace_id": actor.workspace_id,
                "id": run.id,
                "outcome": final,
                "watermark_to": watermark_to,
                "gaps": Jsonb(_merge_gaps(run.gap_reasons, reasons)),
                "access_state": None if outcome.access_state is None else outcome.access_state.value,
                "error_code": code,
            },
        )
        assert updated_row is not None
        finished = CrawlRunRecord.model_validate(updated_row)
        schedule, advanced = await _apply_run_to_schedule(conn, actor, finished, outcome, reasons)
    return RunFinish(run=finished, schedule=schedule, watermark_advanced=advanced)


async def _apply_run_to_schedule(
    conn: Conn,
    actor: ActorContext,
    run: CrawlRunRecord,
    outcome: RunOutcome,
    reasons: list[str],
) -> tuple[ScheduleRecord | None, bool]:
    if run.profile_id is None:
        return None, False
    row = await fetch_one(
        conn,
        _schedule_select(
            "source_id = %(source_id)s and profile_id = %(profile_id)s and partition_key = %(partition_key)s",
            lock=" for update",
        ),
        {
            "workspace_id": actor.workspace_id,
            "source_id": run.source_id,
            "profile_id": run.profile_id,
            "partition_key": run.partition_key,
        },
    )
    if row is None:
        return None, False
    schedule = ScheduleRecord.model_validate(row)
    final = run.outcome
    assert run.finished_at is not None
    values: dict[str, Any] = {
        "complete_watermark": schedule.complete_watermark,
        "page_depth": schedule.page_depth,
        "last_complete_traversal_at": schedule.last_complete_traversal_at,
        "incomplete_since": schedule.incomplete_since,
        "cursor": schedule.cursor,
        "backoff_until": schedule.backoff_until,
        "consecutive_failures": schedule.consecutive_failures,
        "gap_reasons": list(schedule.gap_reasons),
    }
    advanced = False
    if final == Completeness.COMPLETE.value:
        if run.coverage_mode == CoverageMode.WATERMARK:
            if run.watermark_to is not None and (
                schedule.complete_watermark is None or run.watermark_to > schedule.complete_watermark
            ):
                values["complete_watermark"] = run.watermark_to
                advanced = True
        else:
            values["page_depth"] = None if run.page_depth is None else min(run.page_depth, 1000)
            # A late finish of an OLDER run never moves the last complete traversal backwards.
            previous = schedule.last_complete_traversal_at
            values["last_complete_traversal_at"] = (
                run.started_at if previous is None else max(previous, run.started_at)
            )
        values.update(
            {
                "incomplete_since": None,
                "cursor": None,
                "backoff_until": None,
                "consecutive_failures": 0,
                "gap_reasons": [],
            }
        )
    else:
        detail = f"{final}: run {run.id} at {run.started_at.isoformat()}"
        gap_list = [detail, *reasons] if final != "cancelled" else [f"cancelled: run {run.id}", *reasons]
        values["gap_reasons"] = _merge_gaps(schedule.gap_reasons, gap_list)
        values["incomplete_since"] = schedule.incomplete_since or run.started_at
        if outcome.cursor is not None:
            values["cursor"] = outcome.cursor
        if final in (Completeness.FAILED.value, Completeness.BLOCKED.value):
            failures = schedule.consecutive_failures + 1
            values["consecutive_failures"] = failures
            values["backoff_until"] = run.finished_at + _backoff(schedule, failures)
    updated = await fetch_one(
        conn,
        sql.SQL(
            "update ops.source_schedules set complete_watermark = %(complete_watermark)s,"
            " page_depth = %(page_depth)s, last_complete_traversal_at = %(last_complete_traversal_at)s,"
            " incomplete_since = %(incomplete_since)s, cursor = %(cursor)s,"
            " backoff_until = %(backoff_until)s,"
            " consecutive_failures = %(consecutive_failures)s, gap_reasons = %(gap_reasons)s,"
            " run_id = %(run_id)s, row_version = row_version + 1"
            " where workspace_id = %(workspace_id)s and id = %(id)s returning {columns}"
        ).format(columns=_cols(_SCHEDULE_COLUMNS)),
        {
            **values,
            "cursor": None if values["cursor"] is None else Jsonb(values["cursor"]),
            "gap_reasons": Jsonb(values["gap_reasons"]),
            "run_id": run.id,
            "workspace_id": actor.workspace_id,
            "id": schedule.id,
        },
    )
    assert updated is not None
    return ScheduleRecord.model_validate(updated), advanced


__all__ = [
    "ACCESS_GATE_PREFIX",
    "AccessBlockResult",
    "CrawlRunRecord",
    "RobotsRevisionResult",
    "RunFinish",
    "RunOutcome",
    "ScheduleAdvance",
    "ScheduleRecord",
    "SourceRecord",
    "SourceRoute",
    "SourceSyncReport",
    "access_gate_capability",
    "advance_schedule",
    "alert_pause_reason",
    "due_schedules",
    "ensure_schedule",
    "finish_crawl_run",
    "get_crawl_run",
    "get_schedule",
    "get_source",
    "get_source_by_key",
    "get_source_record",
    "list_sources",
    "pause_source",
    "record_access_block",
    "record_fetch_attempt",
    "record_robots_revision",
    "resume_source",
    "run_state",
    "set_technical_status",
    "start_crawl_run",
    "sync_sources_from_yaml",
    "url_hash",
]
