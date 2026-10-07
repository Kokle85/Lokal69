"""Listing ingestion pipeline persistence (spec sections 7, 9, 10, 11, 25, 37.9).

Search pages (`ingest_search_page`)
    Every card becomes one ``app.listing_observations`` row keyed by the spec 10 ingestion key
    ``(source, run, page, source_listing_id, card_hash)``: replaying the same observation is a
    no-op (nothing else changes either). The listing identity comes from
    `domain.identity.identity_from` (provider id, else source-scoped canonical URL). An identity
    hash whose stored material differs is a collision: the stored listing is quarantined and the
    card is skipped, never merged. ``first_seen_at = least(existing, observed)`` and
    ``last_seen_at = greatest(existing, observed)`` (observation time is our fetch time). A known
    provider-id listing seen under a new canonical URL gets an alias row with evidence. Detail work
    is enqueued (spec 9 "Detail fetch rules") for a newly seen listing or a changed card hash
    (the card hash covers price, mileage, title, URL and the source modification marker) unless a
    detail job of the listing is already waiting; the job's dedup key binds identity, incarnation
    and a freshly allocated detail generation -- never a semantic hash. ``card_only`` sources get no
    detail jobs.

Detail pages (`ingest_detail`, one transaction after the network fetch)
    Locks ``ops.jobs`` (lease revalidated), then ``app.sources`` (share), then ``app.listings``,
    exactly the global lock order, and ends with the fenced `jobs.complete` (`LeaseLost` rolls
    everything back). Every accepted parse is stored in ``app.detail_observations``, including
    late completions of older generations. `domain.identity.decide_promotion` decides the effect:

    - PROMOTE_NEW_REVISION: a new immutable ``app.listing_revisions`` row (next chronological
      number; A -> B -> A is allowed), field evidence from provenance, current pointers,
      availability and ``last_detail_success_at``;
    - CONFIRM_UNCHANGED: only refreshes pointers and freshness;
    - HISTORICAL_ONLY: stored as evidence, never regresses current facts;
    - same-generation replays/conflicts: deterministic lower-observation-id tie-break, incidents
      audited.

    A listing that comes back after a removed/not-found page with exactly the facts of its current
    revision gets no duplicate revision (only availability and freshness change).

    An identity-critical change (`domain.identity.detect_identity_conflict`) creates a NEW listing
    incarnation flagged ``identity_conflict`` that inherits no revisions, evidence, screening or
    reviews; the old incarnation is flagged and its availability becomes ``unknown``. A late job of
    a superseded incarnation is stored as evidence only (``superseded_incarnation``) and never
    opens yet another incarnation.
    When the source's parser is unhealthy (spec 25) the observation and any new revision are stored
    as quarantined evidence and nothing is promoted or screened.
    Promoted facts are screened (`domain.filters.screen` with the current business config, recent
    reference FX rates and the vehicle taxonomy); state, profile, screening JSON and version are
    persisted on the listing, and a valuation job is enqueued for eligible / needs-facts results.

Availability (spec 9, 37.9)
    Removal is never inferred from absence. An explicit removed page sets ``removed``; a sold
    badge from the parser sets ``sold_claimed``; a complete scan that no longer shows a listing
    sets ``unknown`` with reason ``not_seen_in_complete_scan`` (`mark_complete_scan_absences`).
    Every transition is handed to an `AvailabilityEventSink`. The default sink records a
    ``listing.availability`` audit event and, once migration ``20261006001000`` has created
    ``app.availability_events`` (spec 37.8), the evidence row there (`TableAvailabilitySink`).

Scopes: ingestion is system work (system principals or ``config:admin``); detail refreshes need
``rechecks:request`` for rechecks; reads need ``deals:read``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from suv_deals.adapters.base import CanonicalIdentity, DiscoveryPage, ParsedListing, SearchObservation
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    AccessState,
    Availability,
    AvailabilityEvidenceKind,
    EligibilityState,
    FxPurpose,
    JobState,
    JobType,
    Scope,
    SourceMode,
    TechnicalStatus,
)
from suv_deals.domain.filters import ScreeningResult, screen
from suv_deals.domain.identity import (
    DetailObservation,
    IdentityConflictReason,
    ListingCurrentState,
    PromotionDecision,
    PromotionOutcome,
    card_hash,
    compare_identity,
    decide_promotion,
    detect_identity_conflict,
    identity_from,
    ingestion_key,
)
from suv_deals.domain.listings import NormalizedListing, sha256_json
from suv_deals.domain.money import FxRate
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.domain.taxonomy import VehicleTaxonomy, default_taxonomy
from suv_deals.errors import (
    AccessBlocked,
    AppError,
    ErrorCode,
    Forbidden,
    NotFound,
    SourcePaused,
    ValidationFailed,
)
from suv_deals.persistence import audit, config_repo, evidence_repo, idempotency, jobs, sources_repo
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.persistence.jobs import ClaimedJob
from suv_deals.persistence.sources_repo import CrawlRunRecord, SourceRecord
from suv_deals.persistence.transactions import lock_job, lock_source
from suv_deals.views.notes import RecheckRequestResult

MAX_MILEAGE_COLUMN: Final = Decimal("99999999.999999")
RECHECK_OPERATION: Final = "deals_request_recheck"
FX_LOOKBACK_DAYS: Final = 60
VALUATION_STATES: Final = frozenset(
    {
        EligibilityState.ELIGIBLE_PRIMARY,
        EligibilityState.ELIGIBLE_MANUAL_PROFILE,
        EligibilityState.NEEDS_FACTS,
    }
)
_WAITING_DETAIL_SQL: Final = (
    "select id from ops.jobs where workspace_id = %(workspace_id)s and listing_id = %(listing_id)s"
    " and job_type in ('detail', 'recheck') and state in ('queued', 'retry_wait') limit 1"
)

_LISTING_COLUMNS: Final = (
    "id",
    "workspace_id",
    "source_id",
    "source_listing_id",
    "incarnation",
    "canonical_url",
    "identity_method",
    "identity_material",
    "identity_hash",
    "identity_confidence",
    "identity_conflict",
    "first_seen_at",
    "last_seen_at",
    "last_detail_success_at",
    "last_availability_check_at",
    "availability",
    "current_revision_id",
    "detail_generation",
    "current_generation",
    "current_observation_id",
    "eligibility_state",
    "eligibility_profile",
    "screening",
    "screening_version",
    "screened_at",
    "quarantined",
    "quarantine_reason",
    "is_fixture",
    "row_version",
    "created_at",
    "updated_at",
)
_REVISION_COLUMNS: Final = (
    "id",
    "workspace_id",
    "listing_id",
    "revision_number",
    "observed_at",
    "semantic_hash",
    "asking_minor",
    "currency",
    "price_basis",
    "price_type",
    "mileage_km",
    "availability",
    "normalized",
    "provenance",
    "parser_version",
    "detail_generation",
    "observation_id",
    "quarantined",
    "created_at",
)


def _cols(names: Sequence[str]) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(c) for c in names)


_LISTING_SQL: Final = sql.SQL(
    "select {columns} from app.listings where workspace_id = %(workspace_id)s and id = %(id)s"
).format(columns=_cols(_LISTING_COLUMNS))
_LOCK_LISTING_SQL: Final = sql.SQL(
    "select {columns} from app.listings where workspace_id = %(workspace_id)s and id = %(id)s for update"
).format(columns=_cols(_LISTING_COLUMNS))
_LOCK_IDENTITY_SQL: Final = sql.SQL(
    "select {columns} from app.listings where workspace_id = %(workspace_id)s"
    " and source_id = %(source_id)s and source_listing_id = %(source_listing_id)s"
    " order by incarnation desc limit 1 for update"
).format(columns=_cols(_LISTING_COLUMNS))


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


class ListingRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    source_id: UUID
    source_listing_id: str
    incarnation: int
    canonical_url: str
    identity_method: Literal["provider_id", "canonical_url"]
    identity_material: str
    identity_hash: str
    identity_confidence: Literal["high", "medium", "low"]
    identity_conflict: bool
    first_seen_at: datetime
    last_seen_at: datetime
    last_detail_success_at: datetime | None = None
    last_availability_check_at: datetime | None = None
    availability: Availability
    current_revision_id: UUID | None = None
    detail_generation: int
    current_generation: int | None = None
    current_observation_id: UUID | None = None
    eligibility_state: EligibilityState | None = None
    eligibility_profile: str | None = None
    screening: dict[str, Any] | None = None
    screening_version: str | None = None
    screened_at: datetime | None = None
    quarantined: bool
    quarantine_reason: str | None = None
    #: Fixture lineage frozen at ingest (the source's mode when the listing was first stored);
    #: never derived from the source's CURRENT mode.
    is_fixture: bool
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "first_seen_at",
        "last_seen_at",
        "last_detail_success_at",
        "last_availability_check_at",
        "screened_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    def screening_result(self) -> ScreeningResult | None:
        if self.screening is None:
            return None
        try:
            return ScreeningResult.model_validate(self.screening)
        except ValidationError:
            return None


class RevisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    listing_id: UUID
    revision_number: int
    observed_at: datetime
    semantic_hash: str
    asking_minor: int | None = None
    currency: str | None = None
    price_basis: str
    price_type: str
    mileage_km: Decimal | None = None
    availability: Availability
    normalized: dict[str, Any]
    provenance: dict[str, Any]
    parser_version: str
    detail_generation: int | None = None
    observation_id: UUID | None = None
    quarantined: bool
    created_at: datetime

    @field_validator("observed_at", "created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def listing(self) -> NormalizedListing:
        try:
            return NormalizedListing.model_validate(self.normalized)
        except ValidationError as exc:
            raise ValidationFailed("the stored revision is not a valid normalized listing") from exc


class DetailJobRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: UUID
    generation: int
    job_id: UUID
    reason: str
    created: bool


class IngestReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    page_number: int
    cards_seen: int
    stored: int
    duplicates: int
    new_listings: int
    changed_listings: int
    unchanged_listings: int
    aliases_recorded: int
    detail_jobs: tuple[DetailJobRef, ...]
    detail_jobs_deduplicated: int
    identity_collisions: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def needs_detail(self) -> tuple[UUID, ...]:
        return tuple(j.listing_id for j in self.detail_jobs)


class AvailabilityTransition(BaseModel):
    """One availability change with its evidence (``app.availability_events`` shape, spec 37.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: UUID
    source_id: UUID
    previous: Availability
    new: Availability
    reason: str = Field(min_length=3, max_length=80)
    evidence_kind: AvailabilityEvidenceKind
    observed_at: datetime
    generation: int | None = None
    observation_id: UUID | None = None
    run_id: UUID | None = None
    # ``app.detail_observations.id`` of the evidence row (detail transitions only).
    detail_observation_row_id: UUID | None = None


class AvailabilityEventSink(Protocol):
    """Hook that persists availability transitions inside the caller's transaction."""

    async def record(self, conn: Conn, actor: ActorContext, transition: AvailabilityTransition) -> None: ...


class AuditAvailabilitySink:
    """Default sink: an append-only ``listing.availability`` audit event per transition."""

    async def record(self, conn: Conn, actor: ActorContext, transition: AvailabilityTransition) -> None:
        await audit.record(
            conn,
            actor,
            "listing.availability",
            "listing",
            transition.listing_id,
            reason=transition.reason,
            metadata={
                "previous": transition.previous.value,
                "new": transition.new.value,
                "reason_code": transition.reason,
                "evidence_kind": transition.evidence_kind.value,
                "observed_at": transition.observed_at.isoformat(),
                "generation": transition.generation,
                "observation_id": None
                if transition.observation_id is None
                else str(transition.observation_id),
                "run_id": None if transition.run_id is None else str(transition.run_id),
            },
        )


# Extraction/observation confidence of each availability reason (absence is the weakest evidence).
_AVAILABILITY_CONFIDENCE: Final[dict[str, Literal["high", "medium", "low"]]] = {
    "not_seen_in_complete_scan": "low",
    "detail_not_found": "medium",
    "identity_conflict_relisted": "medium",
}


class TableAvailabilitySink:
    """One append-only ``app.availability_events`` row per transition (spec 37.8/37.9).

    The table comes with migration ``20261006001000``. The row cites its evidence: the detail
    observation row, or the complete crawl run for an absence (the database refuses an absence
    event whose run is not a finished complete scan).
    """

    async def record(self, conn: Conn, actor: ActorContext, transition: AvailabilityTransition) -> None:
        reference = None
        if transition.detail_observation_row_id is None and transition.run_id is None:
            reference = f"listing:{transition.listing_id}:g{transition.generation or 0}"
        await conn.execute(
            "insert into app.availability_events (workspace_id, source_id, listing_id, old_availability,"
            " new_availability, evidence_kind, reason, crawl_run_id, detail_observation_id, source_reference,"
            " effective_at, observed_at, confidence)"
            " values (%(workspace_id)s, %(source_id)s, %(listing_id)s, %(old)s, %(new)s, %(kind)s,"
            " %(reason)s, %(run_id)s, %(detail_id)s, %(reference)s, %(observed)s, %(observed)s,"
            " %(confidence)s)",
            {
                "workspace_id": actor.workspace_id,
                "source_id": transition.source_id,
                "listing_id": transition.listing_id,
                "old": transition.previous.value,
                "new": transition.new.value,
                "kind": transition.evidence_kind.value,
                "reason": transition.reason,
                "run_id": transition.run_id,
                "detail_id": transition.detail_observation_row_id,
                "reference": reference,
                "observed": transition.observed_at,
                "confidence": _AVAILABILITY_CONFIDENCE.get(transition.reason, "high"),
            },
        )


async def availability_events_available(conn: Conn) -> bool:
    """Whether ``app.availability_events`` exists in this database (a catalog lookup)."""
    row = await fetch_one(conn, "select to_regclass('app.availability_events') is not null as present")
    return bool(row and row["present"])


class DefaultAvailabilitySink:
    """The audit event always, plus the ``app.availability_events`` row once that table exists."""

    def __init__(self) -> None:
        self._audit = AuditAvailabilitySink()
        self._table = TableAvailabilitySink()

    async def record(self, conn: Conn, actor: ActorContext, transition: AvailabilityTransition) -> None:
        await self._audit.record(conn, actor, transition)
        if await availability_events_available(conn):
            await self._table.record(conn, actor, transition)


DEFAULT_AVAILABILITY_SINK: Final = DefaultAvailabilitySink()


class DetailSnapshotRef(BaseModel):
    """Where a detail parse came from. Create it once per fetch: a re-run of the same unit of work
    (e.g. after a transient conflict) reuses ``observation_id`` and is therefore idempotent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parser_version: str = Field(min_length=1, max_length=120)
    observation_id: UUID = Field(default_factory=uuid4)
    snapshot_id: UUID | None = None
    fetch_attempt_id: UUID | None = None
    raw_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    crawler_version: str | None = Field(default=None, max_length=80)
    observed_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


@dataclass(frozen=True)
class ScreeningContext:
    """Inputs for `domain.filters.screen` (loaded once per worker batch or per transaction)."""

    config: BusinessConfig
    config_revision_id: UUID | None
    fx_rates: tuple[FxRate, ...] = ()
    taxonomy: VehicleTaxonomy | None = None
    as_of: date | None = None
    extra: dict[str, str] = field(default_factory=dict)


DetailKind = Literal["listing", "removed", "not_found"]


class IngestDetailResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_listing_id: UUID
    listing_id: UUID
    kind: DetailKind
    outcome: PromotionOutcome | Literal["identity_conflict"]
    observation_row_id: UUID | None
    revision_id: UUID | None
    revision_number: int | None
    promoted: bool
    quarantined: bool
    identity_conflict: bool
    conflict_codes: tuple[str, ...] = ()
    new_incarnation: int | None = None
    availability_before: Availability
    availability_after: Availability
    eligibility_state: EligibilityState | None = None
    screening_version: str | None = None
    valuation_job_id: UUID | None = None
    incident_code: str | None = None


class AbsenceReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    marked_unknown: tuple[UUID, ...]
    # Why the run was not used as absence evidence at all (None = it was evaluated).
    skipped_reason: Literal["run_not_complete", "parser_unhealthy", "empty_traversal"] | None = None


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _require_system(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only the ingestion pipeline may write listing observations")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _db_now(conn: Conn) -> datetime:
    row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return ensure_utc(row["now"])


async def _allocate(conn: Conn, workspace_id: UUID, listing_id: UUID) -> int:
    row = await fetch_one(
        conn,
        "update app.listings set detail_generation = detail_generation + 1"
        " where workspace_id = %(workspace_id)s and id = %(id)s returning detail_generation",
        {"workspace_id": workspace_id, "id": listing_id},
    )
    if row is None:
        raise NotFound("Listing not found")
    return int(row["detail_generation"])


async def allocate_detail_generation(conn: Conn, actor: ActorContext, listing_id: UUID) -> int:
    """Hand out the next detail-fetch generation of a listing (monotonic, spec 10)."""
    if actor.principal_kind != "system" and not (
        actor.has(Scope.CONFIG_ADMIN) or actor.has(Scope.RECHECKS_REQUEST)
    ):
        raise Forbidden("Only workers or recheck requests allocate detail generations")
    async with mapped_errors():
        return await _allocate(conn, actor.workspace_id, listing_id)


async def _enqueue_detail(
    conn: Conn,
    actor: ActorContext,
    listing: ListingRecord,
    *,
    reason: str,
    url: str,
    job_type: JobType = JobType.DETAIL,
    run_id: UUID | None = None,
    priority: int = 0,
) -> DetailJobRef | None:
    """Detail/recheck job bound to identity + incarnation + a fresh generation (None = deduplicated)."""
    waiting = await fetch_one(
        conn, _WAITING_DETAIL_SQL, {"workspace_id": actor.workspace_id, "listing_id": listing.id}
    )
    if waiting is not None:
        return None
    generation = await _allocate(conn, actor.workspace_id, listing.id)
    spec = jobs.JobSpec(
        job_type=job_type,
        dedup_key=f"{job_type.value}:{listing.id}:i{listing.incarnation}:g{generation}",
        payload={
            "listing_id": str(listing.id),
            "source_listing_id": listing.source_listing_id,
            "incarnation": listing.incarnation,
            "url": url,
            "reason": reason,
            "run_id": None if run_id is None else str(run_id),
        },
        priority=priority,
        source_id=listing.source_id,
        listing_id=listing.id,
        generation=generation,
    )
    job_id, created = await jobs.enqueue(conn, actor, spec)
    return DetailJobRef(
        listing_id=listing.id, generation=generation, job_id=job_id, reason=reason, created=created
    )


async def request_detail_refresh(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    *,
    reason: str,
    job_type: JobType = JobType.RECHECK,
    priority: int = 0,
) -> DetailJobRef | None:
    """Queue a bounded detail refresh of a known listing (recheck / stale sweep / retry).

    Rechecks need ``rechecks:request``; other types are system work. Returns None when a detail
    job for the listing is already waiting (deduplicated). Never fetches an arbitrary URL: the job
    targets the stored canonical URL of the listing.
    """
    if job_type not in (JobType.DETAIL, JobType.RECHECK):
        raise ValidationFailed("only detail or recheck jobs refresh a listing")
    if job_type == JobType.RECHECK:
        actor.require(Scope.RECHECKS_REQUEST)
    else:
        _require_system(actor)
    async with mapped_errors():
        row = await fetch_one(conn, _LOCK_LISTING_SQL, {"workspace_id": actor.workspace_id, "id": listing_id})
        if row is None:
            raise NotFound("Listing not found")
        listing = ListingRecord.model_validate(row)
        return await _enqueue_detail(
            conn,
            actor,
            listing,
            reason=reason[:200],
            url=listing.canonical_url,
            job_type=job_type,
            priority=priority,
        )


class WaitingDetailJob(BaseModel):
    """The detail/recheck job already waiting for a listing (what a new request deduplicates to)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    job_type: JobType
    state: JobState
    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


_WAITING_JOB_SQL: Final = (
    "select id, job_type, state, available_at from ops.jobs"
    " where workspace_id = %(workspace_id)s and listing_id = %(listing_id)s"
    " and job_type in ('detail', 'recheck') and state in ('queued', 'retry_wait')"
    " order by created_at, id limit 1"
)


async def find_waiting_detail_job(
    conn: Conn, actor: ActorContext, listing_id: UUID
) -> WaitingDetailJob | None:
    """The oldest queued/retry-waiting detail or recheck job of the listing (``None``: none)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn, _WAITING_JOB_SQL, {"workspace_id": actor.workspace_id, "listing_id": listing_id}
        )
    if row is None:
        return None
    return WaitingDetailJob(
        job_id=row["id"],
        job_type=JobType(row["job_type"]),
        state=JobState(row["state"]),
        available_at=row["available_at"],
    )


class RecheckRequest(Protocol):
    """What `request_recheck` needs (``mcp.schemas.DealsRequestRecheckInput`` satisfies it)."""

    @property
    def listing_id(self) -> UUID: ...

    @property
    def reason(self) -> str: ...

    @property
    def idempotency_key(self) -> str: ...


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed", retryable=False)


async def request_recheck(conn: Conn, actor: ActorContext, request: RecheckRequest) -> RecheckRequestResult:
    """``deals_request_recheck`` (dashboard ``POST /api/listings/{id}/recheck`` and the MCP tool) in
    the caller's transaction.

    A budget-controlled ``recheck`` job for a REGISTERED listing only (its stored canonical URL; the
    request carries no URL). A paused, disabled or parser-unhealthy source is ``SOURCE_PAUSED``; an
    access-blocked source is ``ACCESS_BLOCKED``; a detail/recheck job already waiting for the
    listing is returned as ``deduplicated: true``. Idempotent per principal through the request's
    ``idempotency_key`` (same key and request replays the stored result; another request is
    ``IDEMPOTENCY_CONFLICT``); audited as ``recheck.request``.

    Lock order: idempotency record -> ``app.sources`` (share) -> ``app.listings`` -> new job row ->
    audit (insert-only, last).
    """
    actor.require(Scope.RECHECKS_REQUEST)
    reason = request.reason
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 2000:
        raise ValidationFailed("reason must be 3-2000 characters", details={"fields": ["reason"]})
    request_hash = idempotency.request_hash_for(
        RECHECK_OPERATION, {"listing_id": str(request.listing_id), "reason": reason}
    )
    started = await idempotency.begin(conn, actor, RECHECK_OPERATION, request.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return RecheckRequestResult.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict("The same request is still in progress; retry shortly")
    listing = await get_listing(conn, actor, request.listing_id)
    source = await lock_source(conn, actor.workspace_id, listing.source_id, for_network=False)
    if source.technical_status == TechnicalStatus.ACCESS_BLOCKED:
        raise AccessBlocked()
    if not source.enabled or source.paused or source.technical_status == TechnicalStatus.PARSER_UNHEALTHY:
        raise SourcePaused()
    ref = await request_detail_refresh(
        conn, actor, listing.id, reason=f"recheck: {reason}", job_type=JobType.RECHECK
    )
    if ref is not None:
        job = await jobs.get_job(conn, actor, ref.job_id)
        result = RecheckRequestResult(
            job_id=job.id,
            listing_id=listing.id,
            state=job.state,
            deduplicated=not ref.created,
            available_at=job.available_at,
        )
    else:
        waiting = await find_waiting_detail_job(conn, actor, listing.id)
        if waiting is None:  # pragma: no cover - request_detail_refresh only dedupes a waiting job
            raise AppError(ErrorCode.INTERNAL_ERROR, "The waiting recheck could not be read", retryable=True)
        result = RecheckRequestResult(
            job_id=waiting.job_id,
            listing_id=listing.id,
            state=waiting.state,
            deduplicated=True,
            available_at=waiting.available_at,
        )
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "recheck.request",
            "listing",
            listing.id,
            reason=reason,
            metadata={"job_id": str(result.job_id), "deduplicated": result.deduplicated},
        )
    await idempotency.complete(
        conn, actor, RECHECK_OPERATION, request.idempotency_key, result.model_dump(mode="json")
    )
    return result


# --------------------------------------------------------------------------------------------
# Search pages
# --------------------------------------------------------------------------------------------


@dataclass
class _Card:
    observation: SearchObservation
    identity: CanonicalIdentity
    card_hash: str
    material: dict[str, str | None]
    key: str


async def _lock_or_create(
    conn: Conn, workspace_id: UUID, source: SourceRecord, identity: CanonicalIdentity, observed_at: datetime
) -> tuple[ListingRecord | None, bool]:
    """``(listing, created)``; ``(None, False)`` on an identity-hash collision.

    A new listing freezes its fixture lineage from the source's mode NOW (the source row is
    share-locked by the caller, and the database trigger re-checks it)."""
    source_id = source.id
    params = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "source_listing_id": identity.source_listing_id,
    }
    row = await fetch_one(conn, _LOCK_IDENTITY_SQL, params)
    if row is None:
        inserted = await fetch_one(
            conn,
            "insert into app.listings (workspace_id, source_id, source_listing_id, incarnation,"
            " canonical_url, identity_method, identity_material, identity_hash, identity_confidence,"
            " first_seen_at, last_seen_at, is_fixture)"
            " values (%(workspace_id)s, %(source_id)s, %(source_listing_id)s, 1, %(url)s, %(method)s,"
            " %(material)s, %(hash)s, %(confidence)s, %(observed)s, %(observed)s, %(is_fixture)s)"
            " on conflict do nothing returning id",
            {
                **params,
                "is_fixture": source.mode == SourceMode.FIXTURE,
                "url": identity.canonical_url,
                "method": identity.identity_method,
                "material": identity.identity_material,
                "hash": identity.identity_hash,
                "confidence": "high" if identity.identity_method == "provider_id" else "medium",
                "observed": observed_at,
            },
        )
        row = await fetch_one(conn, _LOCK_IDENTITY_SQL, params)
        if row is None:
            return None, False  # the identity hash belongs to another source listing id
        return ListingRecord.model_validate(row), inserted is not None
    listing = ListingRecord.model_validate(row)
    if compare_identity(listing.identity_material, listing.identity_hash, identity) != "same":
        return None, False
    return listing, False


async def _quarantine_collision(
    conn: Conn, actor: ActorContext, source_id: UUID, identity: CanonicalIdentity
) -> None:
    rows = await fetch_all(
        conn,
        "select id, quarantined, row_version from app.listings where workspace_id = %(workspace_id)s"
        " and source_id = %(source_id)s and (identity_hash = %(hash)s or source_listing_id = %(slid)s)"
        ' order by source_listing_id collate "C", incarnation for update',
        {
            "workspace_id": actor.workspace_id,
            "source_id": source_id,
            "hash": identity.identity_hash,
            "slid": identity.source_listing_id,
        },
    )
    for row in rows:
        if not row["quarantined"]:
            await conn.execute(
                "update app.listings set quarantined = true, quarantine_reason = 'identity_hash_collision',"
                " row_version = row_version + 1 where workspace_id = %(workspace_id)s and id = %(id)s",
                {"workspace_id": actor.workspace_id, "id": row["id"]},
            )
        await audit.record(
            conn,
            actor,
            "listing.identity_hash_collision",
            "listing",
            row["id"],
            prior_version=int(row["row_version"]),
            new_version=int(row["row_version"]) + (0 if row["quarantined"] else 1),
            reason="identity hash matches different canonical material; quarantined, not merged",
            metadata={"identity_hash": identity.identity_hash, "incoming_method": identity.identity_method},
        )


async def ingest_search_page(
    conn: Conn,
    actor: ActorContext,
    run: CrawlRunRecord | UUID,
    page: DiscoveryPage,
    *,
    job_id: UUID | None = None,
) -> IngestReport:
    """Store one discovery page of a running crawl run (see module docstring)."""
    _require_system(actor)
    run_id = run.id if isinstance(run, CrawlRunRecord) else run
    ws = actor.workspace_id
    warnings: list[str] = []
    async with mapped_errors():
        run_row = await fetch_one(
            conn,
            "select id, source_id, outcome from ops.crawl_runs where workspace_id = %(workspace_id)s"
            " and id = %(id)s",
            {"workspace_id": ws, "id": run_id},
        )
        if run_row is None:
            raise NotFound("Crawl run not found")
        await lock_source(conn, ws, run_row["source_id"], for_network=False)
        locked_run = await fetch_one(
            conn,
            "select outcome from ops.crawl_runs where workspace_id = %(workspace_id)s and id = %(id)s"
            " for update",
            {"workspace_id": ws, "id": run_id},
        )
        assert locked_run is not None
        if locked_run["outcome"] != "running":
            raise ValidationFailed("the crawl run is already finished")
        source: SourceRecord = await sources_repo.get_source_record(conn, actor, run_row["source_id"])
        if page.request.source_key != source.source_key:
            raise ValidationFailed("the discovery page belongs to another source")
        page_number = page.request.page_number
        observed_at = page.fetched_at
        tracking = source.tracking_params()
        cards: dict[str, _Card] = {}
        for obs in page.observations:
            try:
                identity = identity_from(
                    source.source_key, obs.source_listing_id, obs.canonical_url, tracking
                )
                digest, material = card_hash(obs.card_hash_material)
            except ValidationFailed as exc:
                warnings.append(f"card at position {obs.position} skipped: {exc.message}"[:300])
                continue
            if digest != obs.card_hash:
                warnings.append(f"card hash recomputed for {identity.source_listing_id[:80]}")
            key = ingestion_key(source.source_key, run_id, page_number, identity.source_listing_id, digest)
            cards.setdefault(key, _Card(obs, identity, digest, material, key))
        counts = {
            "stored": 0,
            "duplicates": 0,
            "new": 0,
            "changed": 0,
            "unchanged": 0,
            "aliases": 0,
            "dedup": 0,
        }
        detail_jobs: list[DetailJobRef] = []
        collisions: list[str] = []
        # Deterministic lock order across concurrent ingestions of overlapping pages.
        for card in sorted(cards.values(), key=lambda c: (c.identity.source_listing_id, c.key)):
            listing, created = await _lock_or_create(conn, ws, source, card.identity, observed_at)
            if listing is None:
                collisions.append(card.identity.source_listing_id)
                await _quarantine_collision(conn, actor, source.id, card.identity)
                continue
            stored = await _insert_card(
                conn, ws, source.id, listing.id, run_id, job_id, page_number, card, observed_at
            )
            if stored is None:
                counts["duplicates"] += 1
                continue
            counts["stored"] += 1
            reason: str | None = None
            if created:
                counts["new"] += 1
                reason = "new_listing"
            else:
                previous = await fetch_one(
                    conn,
                    "select card_hash from app.listing_observations where workspace_id = %(workspace_id)s"
                    " and listing_id = %(listing_id)s and id <> %(id)s"
                    " order by observed_at desc, created_at desc, id desc limit 1",
                    {"workspace_id": ws, "listing_id": listing.id, "id": stored},
                )
                if previous is None or previous["card_hash"] != card.card_hash:
                    counts["changed"] += 1
                    reason = "card_changed"
                else:
                    counts["unchanged"] += 1
                await conn.execute(
                    "update app.listings set first_seen_at = least(first_seen_at, %(observed)s),"
                    " last_seen_at = greatest(last_seen_at, %(observed)s)"
                    " where workspace_id = %(workspace_id)s and id = %(id)s",
                    {"workspace_id": ws, "id": listing.id, "observed": observed_at},
                )
                if (
                    listing.identity_method == "provider_id"
                    and listing.canonical_url != card.identity.canonical_url
                ):
                    counts["aliases"] += await _record_alias(
                        conn, ws, source.id, listing, card, run_id, observed_at
                    )
            if reason is not None and source.detail_mode == "fetch":
                ref = await _enqueue_detail(
                    conn, actor, listing, reason=reason, url=card.identity.canonical_url, run_id=run_id
                )
                if ref is None:
                    counts["dedup"] += 1
                else:
                    detail_jobs.append(ref)
        pages = 1 if counts["stored"] or not page.observations else 0
        await conn.execute(
            "update ops.crawl_runs set pages_fetched = pages_fetched + %(pages)s,"
            " cards_seen = cards_seen + %(cards)s, new_listings = new_listings + %(new)s,"
            " changed_listings = changed_listings + %(changed)s,"
            " detail_jobs_enqueued = detail_jobs_enqueued + %(enqueued)s,"
            " detail_jobs_deduplicated = detail_jobs_deduplicated + %(dedup)s,"
            " result_count_reported = coalesce(%(reported)s, result_count_reported),"
            " page_depth = greatest(coalesce(page_depth, 0), %(page)s),"
            " access_state = %(access_state)s"
            " where workspace_id = %(workspace_id)s and id = %(id)s",
            {
                "workspace_id": ws,
                "id": run_id,
                "pages": pages,
                "cards": counts["stored"],
                "new": counts["new"],
                "changed": counts["changed"],
                "enqueued": len(detail_jobs),
                "dedup": counts["dedup"],
                "reported": page.result_count_reported,
                "page": page_number,
                "access_state": page.access_state.value,
            },
        )
    return IngestReport(
        run_id=run_id,
        page_number=page_number,
        cards_seen=len(page.observations),
        stored=counts["stored"],
        duplicates=counts["duplicates"],
        new_listings=counts["new"],
        changed_listings=counts["changed"],
        unchanged_listings=counts["unchanged"],
        aliases_recorded=counts["aliases"],
        detail_jobs=tuple(detail_jobs),
        detail_jobs_deduplicated=counts["dedup"],
        identity_collisions=tuple(collisions),
        warnings=tuple(warnings[:50]),
    )


async def _insert_card(  # noqa: PLR0917 - private helper
    conn: Conn,
    workspace_id: UUID,
    source_id: UUID,
    listing_id: UUID,
    run_id: UUID,
    job_id: UUID | None,
    page_number: int,
    card: _Card,
    observed_at: datetime,
) -> UUID | None:
    obs = card.observation
    price = obs.card_price_minor if obs.card_currency is not None else None
    currency = obs.card_currency if price is not None else None
    mileage = obs.card_mileage_km
    if mileage is not None and mileage > MAX_MILEAGE_COLUMN:
        mileage = None
    row = await fetch_one(
        conn,
        "insert into app.listing_observations (workspace_id, source_id, listing_id, crawl_run_id,"
        " job_id, page_number, position, source_listing_id, card_hash, card_material, card_price_minor,"
        " card_currency, card_mileage_km, observed_at, source_modified_at, ingestion_key)"
        " values (%(workspace_id)s, %(source_id)s, %(listing_id)s, %(run_id)s, %(job_id)s, %(page)s,"
        " %(position)s, %(slid)s, %(card_hash)s, %(material)s, %(price)s, %(currency)s, %(mileage)s,"
        " %(observed)s, %(modified)s, %(key)s)"
        " on conflict (workspace_id, ingestion_key) do nothing returning id",
        {
            "workspace_id": workspace_id,
            "source_id": source_id,
            "listing_id": listing_id,
            "run_id": run_id,
            "job_id": job_id,
            "page": page_number,
            "position": obs.position,
            "slid": card.identity.source_listing_id,
            "card_hash": card.card_hash,
            "material": Jsonb(card.material),
            "price": price,
            "currency": currency,
            "mileage": None
            if mileage is None
            else mileage.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            "observed": observed_at,
            "modified": None if obs.source_modified_at is None else ensure_utc(obs.source_modified_at),
            "key": card.key,
        },
    )
    return None if row is None else row["id"]


async def _record_alias(  # noqa: PLR0917 - private helper
    conn: Conn,
    workspace_id: UUID,
    source_id: UUID,
    listing: ListingRecord,
    card: _Card,
    run_id: UUID,
    observed_at: datetime,
) -> int:
    url = card.identity.canonical_url
    row = await fetch_one(
        conn,
        "insert into app.listing_aliases (workspace_id, source_id, listing_id, alias_url, alias_hash, reason,"
        " evidence) values (%(workspace_id)s, %(source_id)s, %(listing_id)s, %(url)s, %(hash)s,"
        " 'canonical_url_changed', %(evidence)s)"
        " on conflict (workspace_id, source_id, alias_hash) do nothing returning id",
        {
            "workspace_id": workspace_id,
            "source_id": source_id,
            "listing_id": listing.id,
            "url": url,
            "hash": _sha(url),
            "evidence": Jsonb(
                {
                    "run_id": str(run_id),
                    "observed_at": observed_at.isoformat(),
                    "previous_url_hash": _sha(listing.canonical_url),
                    "identity_method": listing.identity_method,
                    "source_listing_id": listing.source_listing_id,
                    "card_hash": card.card_hash,
                }
            ),
        },
    )
    return 0 if row is None else 1


# --------------------------------------------------------------------------------------------
# Detail pages
# --------------------------------------------------------------------------------------------


def _classify(parsed: ParsedListing) -> DetailKind:
    if parsed.page_type == "removed" or parsed.access_state == AccessState.REMOVED:
        return "removed"
    if parsed.access_state == AccessState.NOT_FOUND:
        return "not_found"
    if parsed.access_state == AccessState.OK and parsed.page_type == "detail" and parsed.listing is not None:
        return "listing"
    raise ValidationFailed(
        "not an ingestible detail page; record the fetch outcome and fail or block the job instead"
    )


def _revision_values(listing: NormalizedListing) -> dict[str, Any]:
    price = listing.price
    vehicle = listing.vehicle
    amount = price.amount_minor if price.currency is not None else None
    mileage = vehicle.mileage_km
    if mileage is not None:
        mileage = (
            None
            if mileage > MAX_MILEAGE_COLUMN
            else mileage.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        )
    year = vehicle.first_registration.year
    if year is not None and not 1950 <= year <= 2100:
        year = None
    month = vehicle.first_registration.month if year is not None else None
    return {
        "asking_minor": amount,
        "currency": price.currency if amount is not None else None,
        "price_basis": price.basis.value,
        "price_type": price.type.value,
        "mileage_km": mileage,
        "availability": listing.availability.value,
        "seller_country": listing.location.country,
        "make": vehicle.make,
        "model": vehicle.model,
        "vehicle_generation": vehicle.generation,
        "registration_year": year,
        "registration_month": month,
        "fuel": vehicle.fuel.value,
        "gearbox": vehicle.gearbox.value,
        "drive": vehicle.drive.value,
        "body_type": vehicle.body_type.value,
    }


async def _insert_revision(  # noqa: PLR0917 - private helper
    conn: Conn,
    workspace_id: UUID,
    listing_id: UUID,
    normalized: NormalizedListing,
    semantic_hash: str,
    generation: int,
    observation_id: UUID,
    *,
    quarantined: bool,
) -> RevisionRecord:
    values = _revision_values(normalized)
    row = await fetch_one(
        conn,
        sql.SQL(
            "insert into app.listing_revisions (workspace_id, listing_id, revision_number, observed_at,"
            " semantic_hash, asking_minor, currency, price_basis, price_type, mileage_km, availability,"
            " seller_country, make, model, vehicle_generation, registration_year, registration_month, fuel,"
            " gearbox, drive, body_type, normalized, provenance, parser_version, detail_generation,"
            " observation_id, quarantined)"
            " values (%(workspace_id)s, %(listing_id)s,"
            " (select coalesce(max(revision_number), 0) + 1 from app.listing_revisions"
            "   where workspace_id = %(workspace_id)s and listing_id = %(listing_id)s),"
            " %(observed_at)s, %(semantic_hash)s, %(asking_minor)s, %(currency)s, %(price_basis)s,"
            " %(price_type)s, %(mileage_km)s, %(availability)s, %(seller_country)s, %(make)s, %(model)s,"
            " %(vehicle_generation)s, %(registration_year)s, %(registration_month)s, %(fuel)s, %(gearbox)s,"
            " %(drive)s, %(body_type)s, %(normalized)s, %(provenance)s, %(parser_version)s, %(generation)s,"
            " %(observation_id)s, %(quarantined)s) returning {columns}"
        ).format(columns=_cols(_REVISION_COLUMNS)),
        {
            **values,
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "observed_at": normalized.observed_at,
            "semantic_hash": semantic_hash,
            "normalized": Jsonb(normalized.model_dump(mode="json")),
            "provenance": Jsonb({k: v.model_dump(mode="json") for k, v in normalized.provenance.items()}),
            "parser_version": normalized.parser_version,
            "generation": generation,
            "observation_id": observation_id,
            "quarantined": quarantined,
        },
    )
    assert row is not None
    return RevisionRecord.model_validate(row)


async def _insert_observation(  # noqa: PLR0917 - private helper
    conn: Conn,
    workspace_id: UUID,
    listing_id: UUID,
    job_id: UUID,
    generation: int,
    ref: DetailSnapshotRef,
    parsed: ParsedListing,
    kind: DetailKind,
    semantic_hash: str,
    availability: Availability,
    observed_at: datetime,
    *,
    promoted: bool,
    not_promoted_reason: str | None,
    quarantined: bool,
) -> UUID | None:
    normalized = parsed.listing
    page_type = parsed.page_type if kind != "not_found" else "unknown"
    row = await fetch_one(
        conn,
        "insert into app.detail_observations (workspace_id, listing_id, generation, observation_id, job_id,"
        " fetch_attempt_id, snapshot_id, semantic_hash, raw_content_hash, normalized, provenance,"
        " parser_version, crawler_version, page_type, availability, observed_at, promoted,"
        " not_promoted_reason, quarantined)"
        " values (%(workspace_id)s, %(listing_id)s, %(generation)s, %(observation_id)s, %(job_id)s,"
        " %(fetch_attempt_id)s, %(snapshot_id)s, %(semantic_hash)s, %(raw_hash)s, %(normalized)s,"
        " %(provenance)s, %(parser_version)s, %(crawler_version)s, %(page_type)s, %(availability)s,"
        " %(observed_at)s, %(promoted)s, %(reason)s, %(quarantined)s)"
        " on conflict (workspace_id, listing_id, generation, observation_id) do nothing returning id",
        {
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "generation": generation,
            "observation_id": ref.observation_id,
            "job_id": job_id,
            "fetch_attempt_id": ref.fetch_attempt_id,
            "snapshot_id": ref.snapshot_id,
            "semantic_hash": semantic_hash,
            "raw_hash": ref.raw_content_hash,
            "normalized": Jsonb(
                normalized.model_dump(mode="json") if normalized is not None else {"page_type": page_type}
            ),
            "provenance": Jsonb(
                {}
                if normalized is None
                else {k: v.model_dump(mode="json") for k, v in normalized.provenance.items()}
            ),
            "parser_version": normalized.parser_version if normalized is not None else ref.parser_version,
            "crawler_version": ref.crawler_version,
            "page_type": page_type,
            "availability": availability.value,
            "observed_at": observed_at,
            "promoted": promoted,
            "reason": None if promoted else (not_promoted_reason or "not_promoted")[:200],
            "quarantined": quarantined,
        },
    )
    return None if row is None else row["id"]


async def _current_state(
    conn: Conn, listing: ListingRecord
) -> tuple[ListingCurrentState, RevisionRecord | None]:
    revision: RevisionRecord | None = None
    if listing.current_revision_id is not None:
        row = await fetch_one(
            conn,
            sql.SQL(
                "select {columns} from app.listing_revisions where workspace_id = %(workspace_id)s"
                " and listing_id = %(listing_id)s and id = %(id)s"
            ).format(columns=_cols(_REVISION_COLUMNS)),
            {
                "workspace_id": listing.workspace_id,
                "listing_id": listing.id,
                "id": listing.current_revision_id,
            },
        )
        revision = None if row is None else RevisionRecord.model_validate(row)
    if listing.current_generation is None or listing.current_observation_id is None:
        return ListingCurrentState(availability=listing.availability), revision
    obs = await fetch_one(
        conn,
        "select semantic_hash from app.detail_observations where workspace_id = %(workspace_id)s"
        " and listing_id = %(listing_id)s and generation = %(generation)s and observation_id = %(obs)s",
        {
            "workspace_id": listing.workspace_id,
            "listing_id": listing.id,
            "generation": listing.current_generation,
            "obs": listing.current_observation_id,
        },
    )
    assert obs is not None  # deferred composite FK
    number = revision.revision_number if revision is not None else 1
    return (
        ListingCurrentState(
            current_generation=listing.current_generation,
            accepted_observation_id=listing.current_observation_id,
            current_semantic_hash=obs["semantic_hash"],
            revision_number=max(number, 1),
            availability=listing.availability,
        ),
        revision,
    )


async def load_screening_context(
    conn: Conn, actor: ActorContext, *, as_of: date | None = None, taxonomy: VehicleTaxonomy | None = None
) -> ScreeningContext | None:
    """Current business config, recent non-fixture reference FX rates and the taxonomy.

    None when no configuration revision exists yet (screening is then skipped, never guessed).
    """
    actor.require(Scope.DEALS_READ)
    try:
        record, config = await config_repo.current_config(conn, actor)
    except NotFound:
        return None
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select base, quote, rate, rate_date, retrieved_at, provider, purpose from app.fx_rates"
            " where workspace_id = %(workspace_id)s and purpose = %(purpose)s and not is_fixture"
            " and rate_date >= (now() at time zone 'UTC')::date - %(days)s::integer"
            " order by rate_date desc, retrieved_at desc limit 500",
            {
                "workspace_id": actor.workspace_id,
                "purpose": FxPurpose.REFERENCE.value,
                "days": FX_LOOKBACK_DAYS,
            },
        )
    rates = tuple(FxRate.model_validate(dict(r)) for r in rows)
    return ScreeningContext(
        config=config,
        config_revision_id=record.id,
        fx_rates=rates,
        taxonomy=taxonomy if taxonomy is not None else default_taxonomy(),
        as_of=as_of,
    )


def _evidence_kind(kind: DetailKind, availability: Availability) -> tuple[str, AvailabilityEvidenceKind]:
    # A detail page whose own parse says "removed" is the same explicit evidence as a removed page
    # (spec 37.9 maps ``removed`` only from an explicit removal, never from a plain observation).
    if kind == "removed" or availability == Availability.REMOVED:
        return "source_removed_page", AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE
    if kind == "not_found":
        return "detail_not_found", AvailabilityEvidenceKind.SOURCE_OBSERVATION
    if availability == Availability.SOLD_CLAIMED:
        return "source_sold_badge", AvailabilityEvidenceKind.SOURCE_SOLD_BADGE
    return "detail_observation", AvailabilityEvidenceKind.SOURCE_OBSERVATION


@dataclass
class _Promotion:
    listing: ListingRecord
    decision: PromotionDecision
    revision: RevisionRecord | None
    current_revision: RevisionRecord | None
    observation_row_id: UUID | None
    kind: DetailKind
    observed_at: datetime
    snapshot_id: UUID | None


async def ingest_detail(  # noqa: PLR0917 - public contract (job, listing, parse, snapshot)
    conn: Conn,
    actor: ActorContext,
    job: ClaimedJob,
    listing_id: UUID,
    parsed: ParsedListing,
    snapshot_ref: DetailSnapshotRef,
    *,
    screening_context: ScreeningContext | None = None,
    availability_sink: AvailabilityEventSink | None = None,
    complete_job: bool = True,
) -> IngestDetailResult:
    """Persist one detail parse for a leased detail/recheck job (see module docstring)."""
    _require_system(actor)
    sink = availability_sink or DEFAULT_AVAILABILITY_SINK
    if job.workspace_id != actor.workspace_id:
        raise NotFound("Job not found")
    if (
        job.job_type not in (JobType.DETAIL, JobType.RECHECK)
        or job.listing_id != listing_id
        or job.generation is None
    ):
        raise ValidationFailed("the job is not a detail job of this listing")
    kind = _classify(parsed)
    ws = actor.workspace_id
    async with mapped_errors():
        await lock_job(conn, ws, job.id, job.lease_token, job.lease_owner)
        head = await fetch_one(
            conn,
            "select source_id from app.listings where workspace_id = %(workspace_id)s and id = %(id)s",
            {"workspace_id": ws, "id": listing_id},
        )
        if head is None:
            raise NotFound("Listing not found")
        source_lock = await lock_source(conn, ws, head["source_id"], for_network=False)
        row = await fetch_one(conn, _LOCK_LISTING_SQL, {"workspace_id": ws, "id": listing_id})
        assert row is not None
        listing = ListingRecord.model_validate(row)
        normalized = parsed.listing
        if (
            kind == "listing"
            and normalized is not None
            and listing.identity_method == "provider_id"
            and normalized.source_listing_id != listing.source_listing_id
        ):
            raise ValidationFailed("the detail page belongs to a different source listing")
        quarantined = source_lock.technical_status == TechnicalStatus.PARSER_UNHEALTHY
        observed_at = snapshot_ref.observed_at or (
            normalized.observed_at if normalized is not None else await _db_now(conn)
        )
        if kind == "listing":
            assert normalized is not None
            semantic = normalized.semantic_hash()
            availability = normalized.availability
        elif kind == "removed":
            semantic = sha256_json({"page_type": "removed", "availability": Availability.REMOVED.value})
            availability = Availability.REMOVED
        else:
            semantic = sha256_json({"page_type": "not_found", "availability": Availability.UNKNOWN.value})
            availability = Availability.UNKNOWN
        state, current_revision = await _current_state(conn, listing)
        incoming = DetailObservation(
            generation=job.generation,
            observation_id=snapshot_ref.observation_id,
            semantic_hash=semantic,
            availability=availability,
            observed_at=observed_at,
        )
        successor = await fetch_one(
            conn,
            "select id from app.listings where workspace_id = %(workspace_id)s and source_id = %(source_id)s"
            " and source_listing_id = %(slid)s and incarnation > %(incarnation)s"
            " order by incarnation desc limit 1",
            {
                "workspace_id": ws,
                "source_id": listing.source_id,
                "slid": listing.source_listing_id,
                "incarnation": listing.incarnation,
            },
        )
        if successor is not None:
            # A newer incarnation replaced this one (identity conflict): a late job of the old
            # incarnation is kept as evidence only and never opens yet another incarnation.
            result = await _superseded_incarnation(
                conn,
                job,
                listing,
                parsed,
                snapshot_ref,
                kind,
                semantic,
                availability,
                observed_at,
                quarantined,
            )
            if complete_job:
                await jobs.complete(conn, job, _job_result(result))
            return result
        newer = state.current_generation is None or job.generation > state.current_generation
        context = screening_context
        if kind == "listing" and not quarantined and newer and current_revision is not None:
            assert normalized is not None
            if context is None:
                context = await load_screening_context(conn, actor)
            reasons = _identity_conflicts(current_revision, normalized, context)
            if reasons:
                result = await _identity_conflict(
                    conn,
                    actor,
                    job,
                    listing,
                    parsed,
                    snapshot_ref,
                    semantic,
                    observed_at,
                    reasons,
                    context,
                    sink,
                )
                if complete_job:
                    await jobs.complete(conn, job, _job_result(result))
                return result
        decision = decide_promotion(state, incoming)
        accepted_incoming = (
            decision.update_listing_state
            and decision.accepted_observation_id == incoming.observation_id
            and decision.current_generation == incoming.generation
        )
        promoted = accepted_incoming and not quarantined
        observation_row_id: UUID | None = None
        if decision.store_historical_evidence or accepted_incoming:
            observation_row_id = await _insert_observation(
                conn,
                ws,
                listing.id,
                job.id,
                job.generation,
                snapshot_ref,
                parsed,
                kind,
                semantic,
                availability,
                observed_at,
                promoted=promoted,
                not_promoted_reason="parser_unhealthy" if quarantined else decision.outcome.value,
                quarantined=quarantined,
            )
        revision: RevisionRecord | None = None
        create_revision = decision.create_revision and kind == "listing" and observation_row_id is not None
        if (
            create_revision
            and current_revision is not None
            and not current_revision.quarantined
            and current_revision.semantic_hash == semantic
        ):
            # Same facts as the current revision after a removed/not-found interlude: the listing
            # is back, but nothing business-meaningful changed, so no duplicate revision.
            create_revision = False
        if create_revision:
            assert normalized is not None
            revision = await _insert_revision(
                conn,
                ws,
                listing.id,
                normalized,
                semantic,
                job.generation,
                incoming.observation_id,
                quarantined=quarantined,
            )
        if decision.incident:
            await audit.record(
                conn,
                actor,
                "listing.observation_incident",
                "listing",
                listing.id,
                reason=decision.explanation,
                metadata={
                    "incident_code": decision.incident_code,
                    "generation": job.generation,
                    "observation_id": str(incoming.observation_id),
                    "superseded_observation_id": None
                    if decision.superseded_observation_id is None
                    else str(decision.superseded_observation_id),
                },
            )
        promotion = _Promotion(
            listing,
            decision,
            revision,
            current_revision,
            observation_row_id,
            kind,
            observed_at,
            snapshot_ref.snapshot_id,
        )
        if quarantined or not promoted:
            result = IngestDetailResult(
                job_listing_id=listing.id,
                listing_id=listing.id,
                kind=kind,
                outcome=decision.outcome,
                observation_row_id=observation_row_id,
                revision_id=None if revision is None else revision.id,
                revision_number=None if revision is None else revision.revision_number,
                promoted=False,
                quarantined=quarantined,
                identity_conflict=False,
                availability_before=listing.availability,
                availability_after=listing.availability,
                eligibility_state=listing.eligibility_state,
                screening_version=listing.screening_version,
                incident_code=decision.incident_code,
            )
        else:
            if context is None and decision.promote_current:
                # Removed / not-found pages re-screen the current facts with the new availability.
                context = await load_screening_context(conn, actor)
            result = await _apply_promotion(conn, actor, job, promotion, normalized, context, sink)
        if complete_job:
            await jobs.complete(conn, job, _job_result(result))
    return result


async def _superseded_incarnation(  # noqa: PLR0917 - private helper
    conn: Conn,
    job: ClaimedJob,
    listing: ListingRecord,
    parsed: ParsedListing,
    ref: DetailSnapshotRef,
    kind: DetailKind,
    semantic: str,
    availability: Availability,
    observed_at: datetime,
    quarantined: bool,
) -> IngestDetailResult:
    assert job.generation is not None
    row_id = await _insert_observation(
        conn,
        listing.workspace_id,
        listing.id,
        job.id,
        job.generation,
        ref,
        parsed,
        kind,
        semantic,
        availability,
        observed_at,
        promoted=False,
        not_promoted_reason="superseded_incarnation",
        quarantined=quarantined,
    )
    return IngestDetailResult(
        job_listing_id=listing.id,
        listing_id=listing.id,
        kind=kind,
        outcome=PromotionOutcome.HISTORICAL_ONLY,
        observation_row_id=row_id,
        revision_id=None,
        revision_number=None,
        promoted=False,
        quarantined=quarantined,
        identity_conflict=listing.identity_conflict,
        availability_before=listing.availability,
        availability_after=listing.availability,
        eligibility_state=listing.eligibility_state,
        screening_version=listing.screening_version,
    )


def _identity_conflicts(
    current_revision: RevisionRecord, incoming: NormalizedListing, context: ScreeningContext | None
) -> list[IdentityConflictReason]:
    try:
        previous = current_revision.listing()
    except ValidationFailed:
        return []
    taxonomy = context.taxonomy if context is not None else None
    return detect_identity_conflict(previous, incoming, taxonomy=taxonomy)


def _job_result(result: IngestDetailResult) -> dict[str, Any]:
    return {
        "listing_id": str(result.listing_id),
        "job_listing_id": str(result.job_listing_id),
        "outcome": str(getattr(result.outcome, "value", result.outcome)),
        "revision_id": None if result.revision_id is None else str(result.revision_id),
        "observation_row_id": None if result.observation_row_id is None else str(result.observation_row_id),
        "promoted": result.promoted,
        "quarantined": result.quarantined,
        "identity_conflict": result.identity_conflict,
    }


def _screen(
    normalized: NormalizedListing | None, context: ScreeningContext | None, observed_at: datetime
) -> ScreeningResult | None:
    if normalized is None or context is None:
        return None
    as_of = context.as_of or observed_at.date()
    return screen(normalized, context.config, context.fx_rates, as_of, context.taxonomy)


async def _apply_promotion(  # noqa: PLR0917 - private helper
    conn: Conn,
    actor: ActorContext,
    job: ClaimedJob,
    promotion: _Promotion,
    normalized: NormalizedListing | None,
    context: ScreeningContext | None,
    sink: AvailabilityEventSink,
) -> IngestDetailResult:
    listing, decision = promotion.listing, promotion.decision
    new_availability = decision.availability if decision.promote_current else listing.availability
    revision_id = promotion.revision.id if promotion.revision is not None else listing.current_revision_id
    # Facts to screen: the new revision, or the current one with the newly observed availability.
    facts: NormalizedListing | None = None
    if promotion.kind == "listing":
        facts = normalized
    elif promotion.current_revision is not None and decision.promote_current:
        try:
            facts = promotion.current_revision.listing().model_copy(update={"availability": new_availability})
        except ValidationFailed:
            facts = None
    screening = _screen(facts, context, promotion.observed_at) if decision.promote_current else None
    refresh = decision.refresh_last_detail_success and promotion.kind == "listing"
    row = await fetch_one(
        conn,
        sql.SQL(
            "update app.listings set current_generation = %(generation)s,"
            " current_observation_id = %(observation_id)s,"
            " current_revision_id = %(revision_id)s, availability = %(availability)s,"
            " last_detail_success_at = case when %(refresh)s"
            "   then greatest(coalesce(last_detail_success_at, %(observed)s), %(observed)s)"
            "   else last_detail_success_at end,"
            " last_availability_check_at = case when %(checked)s"
            "   then greatest(coalesce(last_availability_check_at, %(observed)s), %(observed)s)"
            "   else last_availability_check_at end,"
            " eligibility_state = coalesce(%(eligibility_state)s, eligibility_state),"
            " eligibility_profile = case when %(screened)s then %(eligibility_profile)s"
            "   else eligibility_profile end,"
            " screening = coalesce(%(screening)s, screening),"
            " screening_version = coalesce(%(screening_version)s, screening_version),"
            " screened_at = case when %(screened)s then clock_timestamp() else screened_at end,"
            " row_version = row_version + 1"
            " where workspace_id = %(workspace_id)s and id = %(id)s returning {columns}"
        ).format(columns=_cols(_LISTING_COLUMNS)),
        {
            "workspace_id": actor.workspace_id,
            "id": listing.id,
            "generation": decision.current_generation,
            "observation_id": decision.accepted_observation_id,
            "revision_id": revision_id,
            "availability": new_availability.value,
            "refresh": refresh,
            "checked": decision.promote_current,
            "observed": promotion.observed_at,
            "screened": screening is not None,
            "eligibility_state": None if screening is None else screening.state.value,
            "eligibility_profile": None
            if screening is None or screening.profile is None
            else screening.profile.value,
            "screening": None if screening is None else Jsonb(screening.model_dump(mode="json")),
            "screening_version": None if screening is None else screening.screening_version,
        },
    )
    assert row is not None
    updated = ListingRecord.model_validate(row)
    if promotion.revision is not None:
        assert normalized is not None
        await evidence_repo.insert_provenance_evidence(
            conn,
            actor,
            listing_id=listing.id,
            revision_id=promotion.revision.id,
            provenance=normalized.provenance,
            snapshot_id=promotion.snapshot_id,
        )
    if new_availability != listing.availability:
        reason, evidence_kind = _evidence_kind(promotion.kind, new_availability)
        await sink.record(
            conn,
            actor,
            AvailabilityTransition(
                listing_id=listing.id,
                source_id=listing.source_id,
                previous=listing.availability,
                new=new_availability,
                reason=reason,
                evidence_kind=evidence_kind,
                observed_at=promotion.observed_at,
                generation=job.generation,
                observation_id=decision.accepted_observation_id,
                detail_observation_row_id=promotion.observation_row_id,
            ),
        )
    valuation_job_id = await _maybe_enqueue_valuation(
        conn, actor, updated, listing, promotion.revision, screening, context
    )
    return IngestDetailResult(
        job_listing_id=listing.id,
        listing_id=listing.id,
        kind=promotion.kind,
        outcome=decision.outcome,
        observation_row_id=promotion.observation_row_id,
        revision_id=None if promotion.revision is None else promotion.revision.id,
        revision_number=None if promotion.revision is None else promotion.revision.revision_number,
        promoted=True,
        quarantined=False,
        identity_conflict=False,
        availability_before=listing.availability,
        availability_after=updated.availability,
        eligibility_state=updated.eligibility_state,
        screening_version=updated.screening_version,
        valuation_job_id=valuation_job_id,
        incident_code=decision.incident_code,
    )


async def _maybe_enqueue_valuation(  # noqa: PLR0917 - private helper
    conn: Conn,
    actor: ActorContext,
    updated: ListingRecord,
    before: ListingRecord,
    revision: RevisionRecord | None,
    screening: ScreeningResult | None,
    context: ScreeningContext | None,
) -> UUID | None:
    """Valuation work for eligible / needs-facts results of a new revision or a changed state."""
    if screening is None or screening.state not in VALUATION_STATES or updated.current_revision_id is None:
        return None
    if revision is None and before.eligibility_state == screening.state:
        return None
    spec = jobs.JobSpec(
        job_type=JobType.VALUATION,
        dedup_key=f"valuation:{updated.id}:{updated.current_revision_id}:{screening.state.value}",
        payload={
            "listing_id": str(updated.id),
            "revision_id": str(updated.current_revision_id),
            "eligibility_state": screening.state.value,
            "profile": None if screening.profile is None else screening.profile.value,
            "screening_version": screening.screening_version,
            "config_revision_id": None
            if context is None or context.config_revision_id is None
            else str(context.config_revision_id),
        },
        listing_id=updated.id,
    )
    job_id, _ = await jobs.enqueue(conn, actor, spec)
    return job_id


async def _identity_conflict(  # noqa: PLR0917 - private helper
    conn: Conn,
    actor: ActorContext,
    job: ClaimedJob,
    old: ListingRecord,
    parsed: ParsedListing,
    ref: DetailSnapshotRef,
    semantic: str,
    observed_at: datetime,
    reasons: list[IdentityConflictReason],
    context: ScreeningContext | None,
    sink: AvailabilityEventSink,
) -> IngestDetailResult:
    """Open a new incarnation for an implausible identity change (spec 10); inherit nothing."""
    ws = actor.workspace_id
    normalized = parsed.listing
    assert normalized is not None and job.generation is not None
    old_observation_row_id = await _insert_observation(
        conn,
        ws,
        old.id,
        job.id,
        job.generation,
        ref,
        parsed,
        "listing",
        semantic,
        normalized.availability,
        observed_at,
        promoted=False,
        not_promoted_reason="identity_conflict",
        quarantined=False,
    )
    await conn.execute(
        "update app.listings set identity_conflict = true, availability = 'unknown',"
        " row_version = row_version + 1 where workspace_id = %(workspace_id)s and id = %(id)s",
        {"workspace_id": ws, "id": old.id},
    )
    new_row = await fetch_one(
        conn,
        sql.SQL(
            "insert into app.listings (workspace_id, source_id, source_listing_id, incarnation,"
            " canonical_url, identity_method, identity_material, identity_hash, identity_confidence,"
            " identity_conflict, first_seen_at, last_seen_at, detail_generation, is_fixture)"
            " values (%(workspace_id)s, %(source_id)s, %(slid)s,"
            " (select max(incarnation) + 1 from app.listings where workspace_id = %(workspace_id)s"
            "   and source_id = %(source_id)s and source_listing_id = %(slid)s),"
            " %(url)s, %(method)s, %(material)s, %(hash)s, %(confidence)s, true, %(observed)s,"
            " %(observed)s, 1,"
            # A new incarnation is ingested now: its lineage is the source's mode of this moment
            # (the source row is share-locked by `ingest_detail`).
            " (select s.mode = 'fixture' from app.sources s"
            "   where s.workspace_id = %(workspace_id)s and s.id = %(source_id)s))"
            " returning {columns}"
        ).format(columns=_cols(_LISTING_COLUMNS)),
        {
            "workspace_id": ws,
            "source_id": old.source_id,
            "slid": old.source_listing_id,
            "url": old.canonical_url,
            "method": old.identity_method,
            "material": old.identity_material,
            "hash": old.identity_hash,
            "confidence": old.identity_confidence,
            "observed": observed_at,
        },
    )
    assert new_row is not None
    fresh = ListingRecord.model_validate(new_row)
    new_ref = ref.model_copy(update={"observation_id": uuid4()})
    observation_row_id = await _insert_observation(
        conn,
        ws,
        fresh.id,
        job.id,
        1,
        new_ref,
        parsed,
        "listing",
        semantic,
        normalized.availability,
        observed_at,
        promoted=True,
        not_promoted_reason=None,
        quarantined=False,
    )
    revision = await _insert_revision(
        conn, ws, fresh.id, normalized, semantic, 1, new_ref.observation_id, quarantined=False
    )
    decision = decide_promotion(
        ListingCurrentState(availability=fresh.availability),
        DetailObservation(
            generation=1,
            observation_id=new_ref.observation_id,
            semantic_hash=semantic,
            availability=normalized.availability,
            observed_at=observed_at,
        ),
    )
    if old.availability != Availability.UNKNOWN:
        await sink.record(
            conn,
            actor,
            AvailabilityTransition(
                listing_id=old.id,
                source_id=old.source_id,
                previous=old.availability,
                new=Availability.UNKNOWN,
                reason="identity_conflict_relisted",
                evidence_kind=AvailabilityEvidenceKind.SOURCE_OBSERVATION,
                observed_at=observed_at,
                generation=job.generation,
                observation_id=ref.observation_id,
                detail_observation_row_id=old_observation_row_id,
            ),
        )
    await audit.record(
        conn,
        actor,
        "listing.identity_conflict",
        "listing",
        old.id,
        prior_version=old.row_version,
        new_version=old.row_version + 1,
        reason="identity-critical fields changed; a new listing incarnation was created",
        metadata={
            "codes": [r.code.value for r in reasons],
            "fields": [r.field for r in reasons],
            "new_listing_id": str(fresh.id),
            "new_incarnation": fresh.incarnation,
        },
    )
    promotion = _Promotion(
        fresh, decision, revision, None, observation_row_id, "listing", observed_at, ref.snapshot_id
    )
    applied = await _apply_promotion(conn, actor, job, promotion, normalized, context, sink)
    return applied.model_copy(
        update={
            "job_listing_id": old.id,
            "outcome": "identity_conflict",
            "identity_conflict": True,
            "conflict_codes": tuple(r.code.value for r in reasons),
            "new_incarnation": fresh.incarnation,
        }
    )


# --------------------------------------------------------------------------------------------
# Complete-scan absence (never removal)
# --------------------------------------------------------------------------------------------


async def mark_complete_scan_absences(
    conn: Conn,
    actor: ActorContext,
    run_id: UUID,
    *,
    availability_sink: AvailabilityEventSink | None = None,
    limit: int = 1000,
) -> AbsenceReport:
    """After a COMPLETE traversal: listings of the same (source, profile, partition) that an earlier
    complete traversal showed, that this one did not show and that nothing has seen since it
    started become ``availability = unknown`` (reason ``not_seen_in_complete_scan``). Absence never
    means removed or sold. A detail check made after the traversal started is fresher, direct
    evidence and is never overridden by the absence. A run of a parser-unhealthy source, or one that
    stored no card at all, is no evidence about the inventory (``skipped_reason``; spec 9 and 25: a
    parser incident never sweeps listings). Run this in its own short transaction after
    `finish_crawl_run`.

    Listings are locked in ``(source_listing_id, incarnation)`` byte order, the order search-page
    ingestion of the same source uses, so the two cannot deadlock on overlapping listings.
    """
    _require_system(actor)
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("limit must be between 1 and 10000")
    sink = availability_sink or DEFAULT_AVAILABILITY_SINK
    ws = actor.workspace_id
    async with mapped_errors():
        run = await fetch_one(
            conn,
            "select r.id, r.source_id, r.profile_id, r.partition_key, r.outcome, r.started_at,"
            " r.cards_seen, s.technical_status from ops.crawl_runs r"
            " join app.sources s on s.workspace_id = r.workspace_id and s.id = r.source_id"
            " where r.workspace_id = %(workspace_id)s and r.id = %(id)s",
            {"workspace_id": ws, "id": run_id},
        )
        if run is None:
            raise NotFound("Crawl run not found")
        if run["outcome"] != "complete":
            return AbsenceReport(run_id=run_id, marked_unknown=(), skipped_reason="run_not_complete")
        # Spec 9/25: a parser incident (or a "complete" traversal that found nothing, its typical
        # signature) is never evidence about the inventory; nothing is marked.
        if run["technical_status"] == TechnicalStatus.PARSER_UNHEALTHY.value:
            return AbsenceReport(run_id=run_id, marked_unknown=(), skipped_reason="parser_unhealthy")
        if int(run["cards_seen"]) == 0:
            return AbsenceReport(run_id=run_id, marked_unknown=(), skipped_reason="empty_traversal")
        rows = await fetch_all(
            conn,
            "select l.id, l.availability, l.source_id from app.listings l"
            " where l.workspace_id = %(workspace_id)s and l.source_id = %(source_id)s"
            " and l.availability in ('available', 'reserved') and l.last_seen_at < %(started)s"
            " and (l.last_availability_check_at is null or l.last_availability_check_at < %(started)s)"
            " and exists (select 1 from app.listing_observations o"
            "   join ops.crawl_runs r on r.workspace_id = o.workspace_id and r.id = o.crawl_run_id"
            "  where o.workspace_id = l.workspace_id and o.listing_id = l.id and r.id <> %(run_id)s"
            "    and r.outcome = 'complete' and r.profile_id is not distinct from %(profile_id)s"
            "    and r.partition_key = %(partition)s)"
            " and not exists (select 1 from app.listing_observations o"
            "  where o.workspace_id = l.workspace_id and o.listing_id = l.id and o.crawl_run_id = %(run_id)s)"
            ' order by l.source_listing_id collate "C", l.incarnation limit %(limit)s for update of l',
            {
                "workspace_id": ws,
                "source_id": run["source_id"],
                "started": run["started_at"],
                "run_id": run_id,
                "profile_id": run["profile_id"],
                "partition": run["partition_key"],
                "limit": limit,
            },
        )
        marked: list[UUID] = []
        observed = ensure_utc(run["started_at"])
        for row in rows:
            await conn.execute(
                "update app.listings set availability = 'unknown', row_version = row_version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s",
                {"workspace_id": ws, "id": row["id"]},
            )
            await sink.record(
                conn,
                actor,
                AvailabilityTransition(
                    listing_id=row["id"],
                    source_id=row["source_id"],
                    previous=Availability(row["availability"]),
                    new=Availability.UNKNOWN,
                    reason="not_seen_in_complete_scan",
                    evidence_kind=AvailabilityEvidenceKind.COMPLETE_SCAN_ABSENCE,
                    observed_at=observed,
                    run_id=run_id,
                ),
            )
            marked.append(row["id"])
    return AbsenceReport(run_id=run_id, marked_unknown=tuple(marked))


# --------------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------------


async def get_listing(conn: Conn, actor: ActorContext, listing_id: UUID) -> ListingRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(conn, _LISTING_SQL, {"workspace_id": actor.workspace_id, "id": listing_id})
    if row is None:
        raise NotFound("Listing not found")
    return ListingRecord.model_validate(row)


async def find_listing(
    conn: Conn, actor: ActorContext, source_id: UUID, source_listing_id: str
) -> ListingRecord | None:
    """The newest incarnation of a source listing id (None when unknown)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "select {columns} from app.listings where workspace_id = %(workspace_id)s"
                " and source_id = %(source_id)s and source_listing_id = %(slid)s"
                " order by incarnation desc limit 1"
            ).format(columns=_cols(_LISTING_COLUMNS)),
            {"workspace_id": actor.workspace_id, "source_id": source_id, "slid": source_listing_id},
        )
    return None if row is None else ListingRecord.model_validate(row)


async def list_revisions(
    conn: Conn, actor: ActorContext, listing_id: UUID, *, limit: int = 100
) -> list[RevisionRecord]:
    """Chronological revisions (oldest first) of one listing incarnation."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            sql.SQL(
                "select {columns} from app.listing_revisions where workspace_id = %(workspace_id)s"
                " and listing_id = %(listing_id)s order by revision_number limit %(limit)s"
            ).format(columns=_cols(_REVISION_COLUMNS)),
            {"workspace_id": actor.workspace_id, "listing_id": listing_id, "limit": limit},
        )
    return [RevisionRecord.model_validate(r) for r in rows]


async def current_revision(conn: Conn, actor: ActorContext, listing_id: UUID) -> RevisionRecord | None:
    listing = await get_listing(conn, actor, listing_id)
    if listing.current_revision_id is None:
        return None
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "select {columns} from app.listing_revisions where workspace_id = %(workspace_id)s"
                " and listing_id = %(listing_id)s and id = %(id)s"
            ).format(columns=_cols(_REVISION_COLUMNS)),
            {"workspace_id": actor.workspace_id, "listing_id": listing_id, "id": listing.current_revision_id},
        )
    return None if row is None else RevisionRecord.model_validate(row)


__all__ = [
    "DEFAULT_AVAILABILITY_SINK",
    "AbsenceReport",
    "AuditAvailabilitySink",
    "AvailabilityEventSink",
    "AvailabilityTransition",
    "DefaultAvailabilitySink",
    "DetailJobRef",
    "DetailSnapshotRef",
    "IngestDetailResult",
    "IngestReport",
    "ListingRecord",
    "RevisionRecord",
    "ScreeningContext",
    "TableAvailabilitySink",
    "allocate_detail_generation",
    "availability_events_available",
    "current_revision",
    "find_listing",
    "get_listing",
    "ingest_detail",
    "ingest_search_page",
    "list_revisions",
    "load_screening_context",
    "mark_complete_scan_absences",
    "request_detail_refresh",
]
