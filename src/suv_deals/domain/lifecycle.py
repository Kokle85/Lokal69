"""Listing lifecycle, availability events, freshness lags and coverage evidence across sites.

Spec v1.1 section 37.9 (lifecycle and coverage evidence across sites), with the disappearance
rules of section 9, the out-of-order rules of section 10 and the backend-side mail-worker coverage
semantics of section 37.6. Pure domain code: no I/O and no clock reads (``now`` is a parameter).

Per source listing
    First seen (minimum trustworthy observation), last seen on search (``greatest``), last
    successful detail check, source-created/modified times only when the adapter actually exposes
    them (with an explicit trust flag), the last complete source scan and the source health.
    Nothing here is merged across sources: each source keeps its own evidence.

Vehicle cluster
    ``derive_cluster_lifecycle`` reports the earliest observed appearance and the latest source
    presence across the cluster's listings while keeping every member's evidence. "New today"
    (``is_new_today``) needs a trustworthy, day-precise source publication time with a known
    zone; first-seen by this system never makes an older ad "new".

Availability events (``derive_availability_event``)
    Canonical values only (``listings.availability``). One missing search result, a reordered
    search, an inaccessible page or a paused/disabled source is never a sale or removal:

    - absence from a *complete, healthy* scan of the same search filter, started after the last
      sighting -> ``unknown`` with reason ``not_seen_in_complete_scan`` (never removed/sold);
      failed, partial or budget-limited scans, changed filters and parser incidents produce no
      event at all;
    - an explicit site sold badge -> ``sold_claimed`` + ``source_sold_badge``;
    - a seller statement -> ``sold_claimed`` + ``seller_reported_sold`` (or available/reserved);
    - an explicit removed-listing page -> ``removed`` + ``source_removed_page``;
    - an explicit reserved badge -> ``reserved`` + ``source_reserved_badge``;
    - a 404 detail page from a healthy source -> ``unknown`` + ``source_detail_not_found``
      (reason ``detail_not_found``), never removed.

    None of them establishes a purchase, buyer or transaction price. Older evidence is kept as
    history but never regresses the current status. A source showing the ad active or reserved
    after the seller said "sold", or a later seller "available"/"reserved", is recorded as a
    conflict without overriding the seller's statement. A complete-scan absence counts only
    when the scan started after the last sighting in search *or* on a successful detail page.

Contradictions (``detect_availability_conflicts``)
    An active duplicate elsewhere does not cancel a seller's "sold" statement (and a seller's
    "available" does not cancel a site's sold badge/removal that already existed): the conflict is
    preserved and stops outreach (``SuppressionReason.CONTRADICTORY_AVAILABILITY``) until
    resolved. A badge or removal that appears only after the seller's "available", and any
    evidence older than the current status, is an ordinary progression, not a contradiction.

Lags (``source_scan_lag``, ``detection_delay``, ``detail_freshness``, ``mail_reply_detection_lag``,
``notification_processing_lag``)
    Each is explicitly ``unknown`` when an input is missing and ``inconsistent`` when timestamps
    contradict each other. A configured interval (15-minute scheduler, 2-minute mail
    reconciliation) is reported for context only and is never presented as an observed latency.

Coverage (``healthy_coverage``, ``mail_worker_coverage``, ``reconciliation_window``)
    Healthy source coverage intervals come only from complete, healthy, parser-incident-free
    scans; everything between them is an explicit gap with a reason. Mail-worker gaps (sleep,
    power-off, Outlook closed) are shown, and monitoring is never claimed while the worker is
    not demonstrably running and reconciling.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    AvailabilityEvidenceKind,
    Completeness,
    Precision,
    SuppressionReason,
)
from suv_deals.domain.listings import SourceTimestamp
from suv_deals.errors import ValidationFailed

LIFECYCLE_VERSION: Final = "lifecycle/1.0.0"
OWNER_TIMEZONE: Final = "Europe/Skopje"
CLOCK_SKEW_TOLERANCE: Final = timedelta(minutes=5)
INTERVAL_NOTE: Final = "configured interval for context only; not an observed latency guarantee"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _max(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _min(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


# =============================================================================================
# Source health, scans and trusted source times
# =============================================================================================


class SourceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    PAUSED = "paused"  # owner/source pause or disabled source: never evidence of anything
    BLOCKED = "blocked"  # access blocked / rate limited
    PARSER_INCIDENT = "parser_incident"
    UNKNOWN = "unknown"


class ScanRecord(BaseModel):
    """One discovery traversal of a source search partition (an ``ops.crawl_runs`` view)."""

    model_config = _FROZEN

    scan_id: UUID
    source_key: str = Field(min_length=1, max_length=80)
    partition_key: str = Field(default="default", min_length=1, max_length=200)
    started_at: datetime
    finished_at: datetime | None = None
    completeness: Completeness
    filter_fingerprint: str = Field(min_length=1, max_length=128)
    parser_incident: bool = False
    health: SourceHealth = SourceHealth.HEALTHY
    is_fixture: bool = False

    @field_validator("started_at")
    @classmethod
    def _start(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("finished_at")
    @classmethod
    def _finish(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _order(self) -> ScanRecord:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("a scan cannot finish before it started")
        return self

    @property
    def healthy_complete(self) -> bool:
        """Usable as coverage and as absence evidence: finished, complete, healthy, no incident."""
        return (
            self.finished_at is not None
            and self.completeness == Completeness.COMPLETE
            and not self.parser_incident
            and self.health == SourceHealth.HEALTHY
        )


class TrustedSourceTime(BaseModel):
    """A source-provided created/modified time plus whether the adapter validated its meaning."""

    model_config = _FROZEN

    value: datetime | None = None
    trustworthy: bool = False
    precision: Precision = Precision.UNKNOWN
    zone_assumed: bool = False

    @field_validator("value")
    @classmethod
    def _v(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> TrustedSourceTime:
        if self.value is None and self.trustworthy:
            raise ValueError("a missing source time cannot be trustworthy")
        return self

    @classmethod
    def from_source_timestamp(cls, stamp: SourceTimestamp, *, trustworthy: bool) -> TrustedSourceTime:
        """``trustworthy`` is the adapter's validated statement about the field's semantics."""
        return cls(
            value=stamp.value,
            trustworthy=trustworthy and stamp.value is not None,
            precision=stamp.precision,
            zone_assumed=stamp.zone_assumed,
        )

    @property
    def usable(self) -> bool:
        return self.trustworthy and self.value is not None


# =============================================================================================
# Per-source listing lifecycle
# =============================================================================================


class SourceListingLifecycle(BaseModel):
    """Lifecycle evidence of one source listing. Never merged with another source's evidence."""

    model_config = _FROZEN

    listing_id: UUID
    source_key: str = Field(min_length=1, max_length=80)
    vehicle_cluster_id: UUID | None = None
    first_seen_at: datetime
    last_seen_on_search_at: datetime | None = None
    last_seen_filter_fingerprint: str | None = Field(default=None, max_length=128)
    last_detail_success_at: datetime | None = None
    source_published: TrustedSourceTime = TrustedSourceTime()
    source_modified: TrustedSourceTime = TrustedSourceTime()
    last_complete_scan_at: datetime | None = None
    source_health: SourceHealth = SourceHealth.UNKNOWN
    source_health_at: datetime | None = None  # time of the scan that set ``source_health``
    availability: Availability = Availability.UNKNOWN
    availability_evidence_kind: AvailabilityEvidenceKind | None = None
    availability_reason: str | None = Field(default=None, max_length=80)
    availability_effective_at: datetime | None = None
    is_fixture: bool = False

    @field_validator(
        "first_seen_at",
        "last_seen_on_search_at",
        "last_detail_success_at",
        "last_complete_scan_at",
        "source_health_at",
        "availability_effective_at",
    )
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _order(self) -> SourceListingLifecycle:
        if self.last_seen_on_search_at is not None and self.last_seen_on_search_at < self.first_seen_at:
            raise ValueError("last seen on search cannot precede first seen")
        if self.last_detail_success_at is not None and self.last_detail_success_at < self.first_seen_at:
            raise ValueError("a detail check cannot precede first seen")
        return self

    @property
    def latest_presence_at(self) -> datetime | None:
        """Latest moment the source showed the listing (search or successful detail)."""
        return _max(self.last_seen_on_search_at, self.last_detail_success_at)


class LifecycleObservation(BaseModel):
    """A trustworthy observation to merge into a listing's lifecycle."""

    model_config = _FROZEN

    kind: Literal["search_seen", "detail_success"]
    observed_at: datetime
    filter_fingerprint: str | None = Field(default=None, max_length=128)
    source_published: TrustedSourceTime | None = None
    source_modified: TrustedSourceTime | None = None

    @field_validator("observed_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _filter(self) -> LifecycleObservation:
        if self.kind == "search_seen" and not self.filter_fingerprint:
            raise ValueError("a search observation records the search filter fingerprint")
        return self


def _better_time(current: TrustedSourceTime, incoming: TrustedSourceTime | None) -> TrustedSourceTime:
    """Prefer trustworthy over untrustworthy; never replace a trusted value with an untrusted one."""
    if incoming is None or incoming.value is None:
        return current
    if incoming.trustworthy or not current.trustworthy:
        return incoming
    return current


def apply_observation(
    state: SourceListingLifecycle, observation: LifecycleObservation
) -> SourceListingLifecycle:
    """Merge one observation: first seen = minimum, last seen = greatest; out-of-order safe."""
    at = observation.observed_at
    update: dict[str, object] = {"first_seen_at": min(state.first_seen_at, at)}
    if observation.kind == "search_seen":
        if state.last_seen_on_search_at is None or at >= state.last_seen_on_search_at:
            update["last_seen_on_search_at"] = at
            update["last_seen_filter_fingerprint"] = observation.filter_fingerprint
    else:
        update["last_detail_success_at"] = _max(state.last_detail_success_at, at)
    update["source_published"] = _better_time(state.source_published, observation.source_published)
    update["source_modified"] = _better_time(state.source_modified, observation.source_modified)
    return state.model_validate({**state.model_dump(), **update})


def apply_scan_result(state: SourceListingLifecycle, scan: ScanRecord) -> SourceListingLifecycle:
    """Record the source-level last complete scan time and health for this listing's view.

    Out-of-order safe: the health comes from the newest scan only (an older blocked or failed
    scan that is processed late never replaces a newer healthy state), and the last complete
    scan time only moves forward.
    """
    if scan.source_key != state.source_key:
        raise ValidationFailed("scan belongs to another source")
    update: dict[str, object] = {}
    scan_at = scan.finished_at or scan.started_at
    if state.source_health_at is None or scan_at >= state.source_health_at:
        update["source_health"] = scan.health if not scan.parser_incident else SourceHealth.PARSER_INCIDENT
        update["source_health_at"] = scan_at
    if scan.healthy_complete:
        update["last_complete_scan_at"] = _max(state.last_complete_scan_at, scan.finished_at)
    return state.model_copy(update=update)


# =============================================================================================
# Availability events
# =============================================================================================


class AvailabilitySignalKind(StrEnum):
    SEEN_IN_SEARCH = "seen_in_search"
    DETAIL_ACTIVE = "detail_active"
    NOT_SEEN_IN_SCAN = "not_seen_in_scan"
    SOLD_BADGE = "sold_badge"
    RESERVED_BADGE = "reserved_badge"
    REMOVED_PAGE = "removed_page"
    DETAIL_NOT_FOUND = "detail_not_found"
    DETAIL_INACCESSIBLE = "detail_inaccessible"
    SELLER_STATEMENT = "seller_statement"
    MANUAL = "manual"


_SOURCE_SIGNALS: Final = frozenset(
    {
        AvailabilitySignalKind.SEEN_IN_SEARCH,
        AvailabilitySignalKind.DETAIL_ACTIVE,
        AvailabilitySignalKind.NOT_SEEN_IN_SCAN,
        AvailabilitySignalKind.SOLD_BADGE,
        AvailabilitySignalKind.RESERVED_BADGE,
        AvailabilitySignalKind.REMOVED_PAGE,
        AvailabilitySignalKind.DETAIL_NOT_FOUND,
        AvailabilitySignalKind.DETAIL_INACCESSIBLE,
    }
)


class AvailabilitySignal(BaseModel):
    """One piece of availability evidence for a single source listing."""

    model_config = _FROZEN

    kind: AvailabilitySignalKind
    observed_at: datetime
    effective_at: datetime | None = None
    scan: ScanRecord | None = None
    source_health: SourceHealth = SourceHealth.HEALTHY
    parser_incident: bool = False
    seller_status: Availability | None = None
    reply_id: UUID | None = None
    manual_status: Availability | None = None
    source_reference: str | None = Field(default=None, max_length=200)

    @field_validator("observed_at")
    @classmethod
    def _o(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("effective_at")
    @classmethod
    def _e(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _shape(self) -> AvailabilitySignal:
        if self.kind in (AvailabilitySignalKind.NOT_SEEN_IN_SCAN, AvailabilitySignalKind.SEEN_IN_SEARCH) and (
            self.scan is None
        ):
            raise ValueError("search presence/absence signals reference their scan")
        seller_ok = self.reply_id is not None and self.seller_status in (
            Availability.SOLD_CLAIMED,
            Availability.AVAILABLE,
            Availability.RESERVED,
        )
        if self.kind == AvailabilitySignalKind.SELLER_STATEMENT and not seller_ok:
            raise ValueError("a seller statement names its reply and an available/reserved/sold status")
        if self.kind == AvailabilitySignalKind.MANUAL and self.manual_status is None:
            raise ValueError("a manual signal states the availability")
        return self

    @property
    def when(self) -> datetime:
        return self.effective_at or self.observed_at


class AvailabilityEventDraft(BaseModel):
    """One ``app.availability_events`` row. Never establishes a purchase, buyer or price."""

    model_config = _FROZEN

    listing_id: UUID
    old_status: Availability
    new_status: Availability
    evidence_kind: AvailabilityEvidenceKind
    reason: str = Field(min_length=1, max_length=80)
    effective_at: datetime
    observed_at: datetime
    confidence: Literal["high", "medium", "low"]
    scan_id: UUID | None = None
    reply_id: UUID | None = None
    source_reference: str | None = None
    promote_current: bool = True
    historical_only: bool = False
    conflicts_with_current: bool = False
    establishes_purchase: Literal[False] = False
    transaction_price: None = None


class AvailabilityDecision(BaseModel):
    model_config = _FROZEN

    event: AvailabilityEventDraft | None
    no_event_reason: str | None = None

    @model_validator(mode="after")
    def _one(self) -> AvailabilityDecision:
        if (self.event is None) == (self.no_event_reason is None):
            raise ValueError("either an event or a reason for no event")
        return self


def _no_event(reason: str) -> AvailabilityDecision:
    return AvailabilityDecision(event=None, no_event_reason=reason)


_STRONG_UNAVAILABLE: Final = frozenset({Availability.SOLD_CLAIMED, Availability.REMOVED})


def derive_availability_event(
    state: SourceListingLifecycle, signal: AvailabilitySignal
) -> AvailabilityDecision:
    """Availability event for one signal under the spec 37.9 rules (see module docstring)."""
    kind = signal.kind
    if kind in _SOURCE_SIGNALS:
        problem = _source_signal_problem(state, signal)
        if problem is not None:
            return _no_event(problem)
    target: tuple[Availability, AvailabilityEvidenceKind, str, Literal["high", "medium", "low"]]
    if kind == AvailabilitySignalKind.NOT_SEEN_IN_SCAN:
        if state.availability not in (Availability.AVAILABLE, Availability.RESERVED):
            return _no_event("absence does not change an unknown, sold-claimed or removed status")
        target = (
            Availability.UNKNOWN,
            AvailabilityEvidenceKind.COMPLETE_SCAN_ABSENCE,
            "not_seen_in_complete_scan",
            "medium",
        )
    elif kind in (AvailabilitySignalKind.SEEN_IN_SEARCH, AvailabilitySignalKind.DETAIL_ACTIVE):
        target = (
            Availability.AVAILABLE,
            AvailabilityEvidenceKind.SOURCE_OBSERVATION,
            "seen_in_search" if kind == AvailabilitySignalKind.SEEN_IN_SEARCH else "detail_active",
            "high",
        )
    elif kind == AvailabilitySignalKind.SOLD_BADGE:
        target = (
            Availability.SOLD_CLAIMED,
            AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
            "source_sold_badge",
            "high",
        )
    elif kind == AvailabilitySignalKind.RESERVED_BADGE:
        target = (
            Availability.RESERVED,
            AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE,
            "source_reserved_badge",
            "high",
        )
    elif kind == AvailabilitySignalKind.REMOVED_PAGE:
        target = (
            Availability.REMOVED,
            AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE,
            "source_removed_page",
            "high",
        )
    elif kind == AvailabilitySignalKind.DETAIL_NOT_FOUND:
        if state.availability not in (Availability.AVAILABLE, Availability.RESERVED):
            return _no_event(
                "a missing detail page does not change an unknown, sold-claimed or removed status"
            )
        target = (
            Availability.UNKNOWN,
            AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND,
            "detail_not_found",
            "low",
        )
    elif kind == AvailabilitySignalKind.DETAIL_INACCESSIBLE:
        return _no_event("an inaccessible page is not availability evidence")
    elif kind == AvailabilitySignalKind.SELLER_STATEMENT:
        status = signal.seller_status or Availability.UNKNOWN
        evidence = {
            Availability.SOLD_CLAIMED: AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            Availability.AVAILABLE: AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE,
            Availability.RESERVED: AvailabilityEvidenceKind.SELLER_REPORTED_RESERVED,
        }[status]
        target = (status, evidence, evidence.value, "medium")
    else:  # MANUAL
        status = signal.manual_status or Availability.UNKNOWN
        target = (status, AvailabilityEvidenceKind.MANUAL, "manual", "high")

    new_status, evidence_kind, reason, confidence = target
    if new_status == state.availability and evidence_kind == state.availability_evidence_kind:
        return _no_event("no change")
    historical = state.availability_effective_at is not None and signal.when < state.availability_effective_at
    seller_sold = (
        state.availability == Availability.SOLD_CLAIMED
        and state.availability_evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_SOLD
    )
    # The ad still showing active or "reserved", or the seller later saying available or
    # reserved, does not silently cancel the seller's "sold": the contradiction is kept.
    contradicts_seller_sold = (
        seller_sold
        and new_status in (Availability.AVAILABLE, Availability.RESERVED)
        and evidence_kind
        in (
            AvailabilityEvidenceKind.SOURCE_OBSERVATION,
            AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE,
            AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE,
            AvailabilityEvidenceKind.SELLER_REPORTED_RESERVED,
        )
    )
    # A seller's "available" after a site's sold badge or removal page is a contradiction too.
    contradicts_source_unavailable = (
        new_status == Availability.AVAILABLE
        and evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE
        and state.availability in _STRONG_UNAVAILABLE
        and state.availability_evidence_kind
        in (AvailabilityEvidenceKind.SOURCE_SOLD_BADGE, AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE)
    )
    # Older evidence (the ad seen active, or the seller's "available", *before* the current status
    # took effect) is history - an ordinary progression, never a contradiction.
    conflict = not historical and (contradicts_seller_sold or contradicts_source_unavailable)
    return AvailabilityDecision(
        event=AvailabilityEventDraft(
            listing_id=state.listing_id,
            old_status=state.availability,
            new_status=new_status,
            evidence_kind=evidence_kind,
            reason=reason,
            effective_at=signal.when,
            observed_at=signal.observed_at,
            confidence=confidence,
            scan_id=signal.scan.scan_id if signal.scan else None,
            reply_id=signal.reply_id,
            source_reference=signal.source_reference,
            promote_current=not historical and not conflict,
            historical_only=historical,
            conflicts_with_current=conflict,
        )
    )


def _source_signal_problem(state: SourceListingLifecycle, signal: AvailabilitySignal) -> str | None:
    """Why a source observation is not evidence (``None`` when it is usable)."""
    scan = signal.scan
    if scan is not None:
        if scan.source_key != state.source_key:
            raise ValidationFailed("scan belongs to another source")
        if scan.is_fixture != state.is_fixture:
            raise ValidationFailed("fixture scans and real listings are never mixed")
    health = scan.health if scan is not None else signal.source_health
    incident = signal.parser_incident or (scan is not None and scan.parser_incident)
    if health == SourceHealth.PAUSED:
        return "source paused or disabled; not evidence"
    if incident or health == SourceHealth.PARSER_INCIDENT:
        return "parser incident; observations are not evidence"
    if signal.kind == AvailabilitySignalKind.NOT_SEEN_IN_SCAN:
        if scan is None or scan.finished_at is None:
            return "scan not finished"
        if scan.completeness != Completeness.COMPLETE:
            return f"scan {scan.completeness.value}; absence is not evidence"
        if health != SourceHealth.HEALTHY:
            return f"source {health.value}; absence is not evidence"
        if state.last_seen_filter_fingerprint is None:
            return "listing was never seen under a known search filter"
        if scan.filter_fingerprint != state.last_seen_filter_fingerprint:
            return "search filter changed; absence is not evidence"
        # A successful detail check counts as a sighting too: an ad whose page was live after
        # the scan started is not "absent" because the search did not list it.
        if state.latest_presence_at is not None and scan.started_at <= state.latest_presence_at:
            return "scan started before the last sighting"
        return None
    if signal.kind == AvailabilitySignalKind.DETAIL_NOT_FOUND and health != SourceHealth.HEALTHY:
        return f"source {health.value}; a missing page is not evidence"
    if health in (SourceHealth.BLOCKED, SourceHealth.UNKNOWN) and signal.kind not in (
        AvailabilitySignalKind.DETAIL_INACCESSIBLE,
    ):
        return f"source {health.value}; observation not trusted"
    return None


# =============================================================================================
# Vehicle clusters and contradictions
# =============================================================================================


class SellerAvailabilityStatement(BaseModel):
    """A seller's availability statement from a correlated reply."""

    model_config = _FROZEN

    reply_id: UUID
    status: Availability
    stated_at: datetime

    @field_validator("stated_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("status")
    @classmethod
    def _s(cls, value: Availability) -> Availability:
        if value not in (Availability.SOLD_CLAIMED, Availability.AVAILABLE, Availability.RESERVED):
            raise ValueError("seller statements are available, reserved or sold_claimed")
        return value


ConflictKind = Literal[
    "active_listing_vs_seller_sold",
    "seller_available_vs_source_unavailable",
    "sources_disagree",
    "seller_statements_disagree",
]


class AvailabilityConflict(BaseModel):
    """A preserved contradiction. It stops outreach until resolved; nothing is overwritten."""

    model_config = _FROZEN

    kind: ConflictKind
    listing_ids: tuple[UUID, ...]
    reply_ids: tuple[UUID, ...] = ()
    detail: str = Field(max_length=300)
    stop_outreach: Literal[True] = True
    suppression_reason: Literal[SuppressionReason.CONTRADICTORY_AVAILABILITY] = (
        SuppressionReason.CONTRADICTORY_AVAILABILITY
    )


def detect_availability_conflicts(
    members: Sequence[SourceListingLifecycle],
    seller_statements: Sequence[SellerAvailabilityStatement] = (),
) -> tuple[AvailabilityConflict, ...]:
    """Contradictions between a cluster's source listings and the seller's statements."""
    conflicts: list[AvailabilityConflict] = []
    latest_statement = max(seller_statements, key=lambda s: s.stated_at, default=None)
    source_unavailable = [
        m
        for m in members
        if m.availability in _STRONG_UNAVAILABLE
        and m.availability_evidence_kind
        in (AvailabilityEvidenceKind.SOURCE_SOLD_BADGE, AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE)
    ]
    seller_sold = [s for s in seller_statements if s.status == Availability.SOLD_CLAIMED]
    seller_sold_listings = [
        m
        for m in members
        if m.availability == Availability.SOLD_CLAIMED
        and m.availability_evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_SOLD
    ]
    sold_times = [s.stated_at for s in seller_sold] + [
        m.availability_effective_at for m in seller_sold_listings if m.availability_effective_at is not None
    ]
    sold_at = min(sold_times) if sold_times else None
    active = [m for m in members if m.availability == Availability.AVAILABLE]
    if sold_at is not None:
        # The seller-sold listing itself still showing up in search after the statement is also
        # an active advertisement contradicting it.
        active += [
            m
            for m in seller_sold_listings
            if m.latest_presence_at is not None and m.latest_presence_at > sold_at
        ]
    if (seller_sold or seller_sold_listings) and active:
        later = [
            m for m in active if sold_at is not None and (m.latest_presence_at or m.first_seen_at) >= sold_at
        ]
        involved = {m.listing_id: None for m in [*active, *seller_sold_listings]}
        conflicts.append(
            AvailabilityConflict(
                kind="active_listing_vs_seller_sold",
                listing_ids=tuple(involved),
                reply_ids=tuple(s.reply_id for s in seller_sold),
                detail=(
                    f"seller reports sold while {len(active)} listing(s) remain active"
                    + (f" ({len(later)} seen after the statement)" if later else "")
                ),
            )
        )
    active = [m for m in members if m.availability == Availability.AVAILABLE]
    if latest_statement is not None and latest_statement.status == Availability.AVAILABLE:
        # A sold badge or removal that appeared only *after* the seller said "available" is an
        # ordinary progression (the car sold later); one that already existed (or whose time is
        # unknown) contradicts the statement.
        contradicted = [
            m
            for m in source_unavailable
            if m.availability_effective_at is None
            or m.availability_effective_at <= latest_statement.stated_at
        ]
        if contradicted:
            conflicts.append(
                AvailabilityConflict(
                    kind="seller_available_vs_source_unavailable",
                    listing_ids=tuple(m.listing_id for m in contradicted),
                    reply_ids=(latest_statement.reply_id,),
                    detail="seller says available while a source shows a sold badge or removal",
                )
            )
    if active and source_unavailable:
        conflicts.append(
            AvailabilityConflict(
                kind="sources_disagree",
                listing_ids=tuple(m.listing_id for m in active + source_unavailable),
                detail="one source shows the vehicle active while another shows it sold or removed",
            )
        )
    disagreeing = _disagreeing_statements(seller_statements)
    if disagreeing:
        conflicts.append(
            AvailabilityConflict(
                kind="seller_statements_disagree",
                listing_ids=tuple(m.listing_id for m in members),
                reply_ids=tuple(dict.fromkeys(s.reply_id for s in disagreeing)),
                detail=(
                    "the seller made contradictory availability statements "
                    "(different statuses at the same time, or available/reserved after sold)"
                ),
            )
        )
    return tuple(conflicts)


def _disagreeing_statements(
    statements: Sequence[SellerAvailabilityStatement],
) -> list[SellerAvailabilityStatement]:
    """Statements that contradict each other rather than describe a progression.

    available -> reserved -> sold (and reserved -> available after a cancelled reservation) is an
    ordinary sequence over time. Different statuses with the same statement time, or "available"/
    "reserved" after the seller said "sold", are contradictions the owner must resolve.
    """
    ordered = sorted(statements, key=lambda s: (s.stated_at, str(s.reply_id)))
    involved: list[SellerAvailabilityStatement] = []
    by_time: dict[datetime, set[Availability]] = {}
    for statement in ordered:
        by_time.setdefault(statement.stated_at, set()).add(statement.status)
    for statement in ordered:
        if len(by_time[statement.stated_at]) > 1:
            involved.append(statement)
    first_sold = next((s for s in ordered if s.status == Availability.SOLD_CLAIMED), None)
    if first_sold is not None:
        reversals = [
            s for s in ordered if s.stated_at > first_sold.stated_at and s.status != Availability.SOLD_CLAIMED
        ]
        if reversals:
            involved.extend([first_sold, *reversals])
    return list(dict.fromkeys(involved))


class ClusterLifecycle(BaseModel):
    """Cluster-level lifecycle derived from (not replacing) each source's own evidence."""

    model_config = _FROZEN

    vehicle_cluster_id: UUID
    earliest_observed_appearance: datetime
    earliest_observed_source: str
    earliest_trusted_publication: datetime | None
    earliest_trusted_publication_source: str | None
    latest_source_presence: datetime | None
    latest_presence_source: str | None
    members: tuple[SourceListingLifecycle, ...]
    conflicts: tuple[AvailabilityConflict, ...] = ()

    @property
    def outreach_blocked(self) -> bool:
        return bool(self.conflicts)


def derive_cluster_lifecycle(
    vehicle_cluster_id: UUID,
    members: Sequence[SourceListingLifecycle],
    seller_statements: Sequence[SellerAvailabilityStatement] = (),
) -> ClusterLifecycle:
    """Earliest observed appearance, earliest trustworthy publication and latest source presence
    across a cluster, keeping every member's evidence and any availability contradiction."""
    if not members:
        raise ValidationFailed("a cluster lifecycle needs at least one member listing")
    if len({m.listing_id for m in members}) != len(members):
        raise ValidationFailed("duplicate member listing")
    if any(m.vehicle_cluster_id not in (None, vehicle_cluster_id) for m in members):
        raise ValidationFailed("member listing belongs to another cluster")
    if len({m.is_fixture for m in members}) > 1:
        raise ValidationFailed("fixture and real listings are never clustered together")
    earliest = min(members, key=lambda m: (m.first_seen_at, m.source_key, str(m.listing_id)))
    published = [m for m in members if m.source_published.usable]
    first_pub = min(
        published,
        key=lambda m: (m.source_published.value or m.first_seen_at, m.source_key),
        default=None,
    )
    present = [m for m in members if m.latest_presence_at is not None]
    latest = max(present, key=lambda m: (m.latest_presence_at or m.first_seen_at, m.source_key), default=None)
    return ClusterLifecycle(
        vehicle_cluster_id=vehicle_cluster_id,
        earliest_observed_appearance=earliest.first_seen_at,
        earliest_observed_source=earliest.source_key,
        earliest_trusted_publication=first_pub.source_published.value if first_pub else None,
        earliest_trusted_publication_source=first_pub.source_key if first_pub else None,
        latest_source_presence=latest.latest_presence_at if latest else None,
        latest_presence_source=latest.source_key if latest else None,
        members=tuple(sorted(members, key=lambda m: (m.source_key, str(m.listing_id)))),
        conflicts=detect_availability_conflicts(members, seller_statements),
    )


# =============================================================================================
# "New today"
# =============================================================================================


class NewTodayDecision(BaseModel):
    model_config = _FROZEN

    status: Literal["yes", "no", "unknown"]
    reason: str
    local_date: str | None = None


_DAY_OR_FINER: Final = frozenset({Precision.DAY})


def is_new_today(
    published: TrustedSourceTime, *, now: datetime, timezone: str = OWNER_TIMEZONE
) -> NewTodayDecision:
    """Whether the *source* published the ad today (owner time zone). Unknown without a
    trustworthy, day-precise publication time with a known zone; first-seen never counts."""
    current = _aware(now)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed("unknown time zone") from exc
    if not published.usable or published.value is None:
        return NewTodayDecision(status="unknown", reason="publication_time_unknown")
    if published.precision not in _DAY_OR_FINER:
        return NewTodayDecision(status="unknown", reason="publication_time_too_coarse")
    if published.zone_assumed:
        return NewTodayDecision(status="unknown", reason="publication_zone_assumed")
    if published.value > current + CLOCK_SKEW_TOLERANCE:
        return NewTodayDecision(status="unknown", reason="publication_time_in_future")
    local = published.value.astimezone(zone).date()
    today = current.astimezone(zone).date()
    return NewTodayDecision(
        status="yes" if local == today else "no",
        reason="published_today" if local == today else "published_earlier",
        local_date=local.isoformat(),
    )


# =============================================================================================
# Lags
# =============================================================================================

LagName = Literal[
    "source_scan_lag",
    "detection_delay",
    "detail_freshness",
    "mail_reply_detection_lag",
    "notification_processing_lag",
    "mailbox_sync_lag",
    "backlog_age",
]


class LagStatus(StrEnum):
    MEASURED = "measured"
    UNKNOWN = "unknown"
    INCONSISTENT = "inconsistent"


class LagMeasurement(BaseModel):
    """An observed lag. ``unknown`` and ``inconsistent`` carry no value - never zero."""

    model_config = _FROZEN

    name: LagName
    status: LagStatus
    value_seconds: int | None = None
    reason: str | None = None
    configured_interval_seconds: int | None = None
    note: str = INTERVAL_NOTE

    @model_validator(mode="after")
    def _shape(self) -> LagMeasurement:
        if (self.status == LagStatus.MEASURED) != (self.value_seconds is not None):
            raise ValueError("only a measured lag has a value")
        return self

    @property
    def value(self) -> timedelta | None:
        return None if self.value_seconds is None else timedelta(seconds=self.value_seconds)


def _lag(
    name: LagName,
    start: datetime | None,
    end: datetime | None,
    *,
    missing: str,
    configured: timedelta | None = None,
) -> LagMeasurement:
    interval = None if configured is None else int(configured.total_seconds())
    if start is None or end is None:
        return LagMeasurement(
            name=name, status=LagStatus.UNKNOWN, reason=missing, configured_interval_seconds=interval
        )
    delta = _aware(end) - _aware(start)
    if delta < -CLOCK_SKEW_TOLERANCE:
        return LagMeasurement(
            name=name,
            status=LagStatus.INCONSISTENT,
            reason="timestamps out of order",
            configured_interval_seconds=interval,
        )
    seconds = max(0, int(Decimal(delta.total_seconds()).to_integral_value(rounding=ROUND_HALF_UP)))
    return LagMeasurement(
        name=name, status=LagStatus.MEASURED, value_seconds=seconds, configured_interval_seconds=interval
    )


def source_scan_lag(
    last_complete_scan_finished_at: datetime | None,
    *,
    now: datetime,
    configured_interval: timedelta | None = None,
) -> LagMeasurement:
    """Time since the last complete, healthy scan of a source."""
    return _lag(
        "source_scan_lag",
        last_complete_scan_finished_at,
        now,
        missing="no complete healthy scan recorded",
        configured=configured_interval,
    )


def detection_delay(first_seen_at: datetime | None, source_published: TrustedSourceTime) -> LagMeasurement:
    """First seen by this system minus a *trustworthy* source publication time; unknown without."""
    if not source_published.usable:
        return LagMeasurement(
            name="detection_delay", status=LagStatus.UNKNOWN, reason="no trustworthy source publication time"
        )
    if source_published.precision not in _DAY_OR_FINER or source_published.zone_assumed:
        return LagMeasurement(
            name="detection_delay",
            status=LagStatus.UNKNOWN,
            reason="source publication time too coarse or zone assumed",
        )
    return _lag("detection_delay", source_published.value, first_seen_at, missing="first seen unknown")


def detail_freshness(last_detail_success_at: datetime | None, *, now: datetime) -> LagMeasurement:
    """Age of the last successful detail check."""
    return _lag("detail_freshness", last_detail_success_at, now, missing="no successful detail check")


def mail_reply_detection_lag(
    received_at: datetime | None,
    detected_at: datetime | None,
    *,
    configured_interval: timedelta | None = None,
) -> LagMeasurement:
    """Mailbox received time to local worker detection (or backend ingest)."""
    return _lag(
        "mail_reply_detection_lag",
        received_at,
        detected_at,
        missing="received or detection time missing",
        configured=configured_interval,
    )


def notification_processing_lag(
    event_created_at: datetime | None, provider_accepted_at: datetime | None
) -> LagMeasurement:
    """Outbox event creation to provider acceptance. Says nothing about dot's processing."""
    return _lag(
        "notification_processing_lag",
        event_created_at,
        provider_accepted_at,
        missing="event not yet accepted by the provider",
    )


# =============================================================================================
# Coverage intervals and gaps
# =============================================================================================


class CoverageInterval(BaseModel):
    model_config = _FROZEN

    source_key: str
    start: datetime
    end: datetime
    scans: int = Field(ge=1)


class CoverageGap(BaseModel):
    model_config = _FROZEN

    subject: str  # a source key or a mailbox id
    start: datetime
    end: datetime
    reason: str = Field(max_length=120)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class SourceCoverage(BaseModel):
    """Healthy coverage of one source inside a window, with every gap explained."""

    model_config = _FROZEN

    source_key: str
    window_start: datetime
    window_end: datetime
    intervals: tuple[CoverageInterval, ...]
    gaps: tuple[CoverageGap, ...]
    healthy_seconds: int
    coverage_ratio: Decimal | None  # None for an empty window

    @property
    def has_coverage(self) -> bool:
        return bool(self.intervals)


def _gap_reason(scans: Sequence[ScanRecord]) -> str:
    if not scans:
        return "no_scans"
    if any(s.health == SourceHealth.PAUSED for s in scans):
        return "source_paused"
    if any(s.parser_incident or s.health == SourceHealth.PARSER_INCIDENT for s in scans):
        return "parser_incident"
    if any(s.health == SourceHealth.BLOCKED or s.completeness == Completeness.BLOCKED for s in scans):
        return "access_blocked"
    if any(s.health == SourceHealth.DEGRADED for s in scans):
        return "source_degraded"
    return "incomplete_or_failed_scans"


def healthy_coverage(
    scans: Sequence[ScanRecord],
    *,
    window_start: datetime,
    window_end: datetime,
    max_gap: timedelta,
    include_fixture: bool = False,
) -> tuple[SourceCoverage, ...]:
    """Per-source healthy coverage intervals inside ``[window_start, window_end)``.

    An interval is a maximal chain of healthy complete scans whose successive finish times are
    at most ``max_gap`` apart, spanning from the first scan's start to the last scan's finish.
    Everything else in the window is a gap with a reason derived from the scans inside it.
    Fixture scans are ignored unless ``include_fixture``.
    """
    start, end = _aware(window_start), _aware(window_end)
    if end < start:
        raise ValidationFailed("window end precedes its start")
    if max_gap <= timedelta(0):
        raise ValidationFailed("max_gap must be positive")
    by_source: dict[str, list[ScanRecord]] = {}
    for scan in scans:
        if scan.is_fixture and not include_fixture:
            continue
        by_source.setdefault(scan.source_key, []).append(scan)
    reports: list[SourceCoverage] = []
    for source_key in sorted(by_source):
        source_scans = sorted(by_source[source_key], key=lambda s: (s.started_at, str(s.scan_id)))
        healthy = [s for s in source_scans if s.healthy_complete]
        raw_intervals: list[tuple[datetime, datetime, int]] = []
        for scan in sorted(healthy, key=lambda s: (s.finished_at or s.started_at, str(s.scan_id))):
            finished = scan.finished_at or scan.started_at
            if raw_intervals and finished - raw_intervals[-1][1] <= max_gap:
                first, _last, count = raw_intervals[-1]
                raw_intervals[-1] = (min(first, scan.started_at), finished, count + 1)
            else:
                raw_intervals.append((scan.started_at, finished, 1))
        # A long scan can start before an earlier chain ended: overlapping chains are one
        # interval, so healthy time is never counted twice (the ratio stays <= 1).
        merged: list[tuple[datetime, datetime, int]] = []
        for first, last, count in sorted(raw_intervals):
            if merged and first <= merged[-1][1]:
                m_first, m_last, m_count = merged[-1]
                merged[-1] = (m_first, max(m_last, last), m_count + count)
            else:
                merged.append((first, last, count))
        intervals: list[CoverageInterval] = []
        for first, last, count in merged:
            clipped_start, clipped_end = max(first, start), min(last, end)
            if clipped_end > clipped_start or (clipped_end == clipped_start and start <= first < end):
                intervals.append(
                    CoverageInterval(source_key=source_key, start=clipped_start, end=clipped_end, scans=count)
                )
        gaps: list[CoverageGap] = []
        cursor = start
        for interval in [*intervals, None]:
            gap_end = interval.start if interval is not None else end
            if gap_end > cursor:
                inside = [
                    s
                    for s in source_scans
                    if not s.healthy_complete
                    and s.started_at < gap_end
                    and (s.finished_at or s.started_at) >= cursor
                ]
                gaps.append(
                    CoverageGap(subject=source_key, start=cursor, end=gap_end, reason=_gap_reason(inside))
                )
            if interval is not None:
                cursor = max(cursor, interval.end)
        healthy_seconds = int(sum((i.end - i.start).total_seconds() for i in intervals))
        window_seconds = int((end - start).total_seconds())
        ratio = (
            (Decimal(healthy_seconds) / Decimal(window_seconds)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if window_seconds > 0
            else None
        )
        reports.append(
            SourceCoverage(
                source_key=source_key,
                window_start=start,
                window_end=end,
                intervals=tuple(intervals),
                gaps=tuple(gaps),
                healthy_seconds=healthy_seconds,
                coverage_ratio=ratio,
            )
        )
    return tuple(reports)


# =============================================================================================
# Mail worker coverage and reconciliation (spec 37.6, backend-side)
# =============================================================================================


class MailWorkerHeartbeat(BaseModel):
    """One heartbeat/health report from the local mailbox worker."""

    model_config = _FROZEN

    observed_at: datetime
    outlook_connected: bool | None = None
    mailbox_sync_ok: bool | None = None
    mailbox_last_sync_at: datetime | None = None
    last_reconciliation_completed_at: datetime | None = None
    backlog_count: int | None = Field(default=None, ge=0)
    backlog_oldest_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def _o(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("mailbox_last_sync_at", "last_reconciliation_completed_at", "backlog_oldest_at")
    @classmethod
    def _t(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


ComponentStatus = Literal["healthy", "stale", "down", "unknown"]


class MailCoverageReport(BaseModel):
    """Separate health dimensions of the reply path; never a blanket "monitoring active"."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    generated_at: datetime
    last_heartbeat_at: datetime | None
    heartbeat_status: ComponentStatus
    outlook_status: ComponentStatus
    mailbox_sync_lag: LagMeasurement
    last_successful_reconciliation_at: datetime | None
    reconciliation_status: ComponentStatus
    backlog_count: int | None
    backlog_age: LagMeasurement
    slack_signal_status: ComponentStatus
    mcp_read_status: ComponentStatus
    gaps: tuple[CoverageGap, ...]
    monitoring_active: bool
    reasons: tuple[str, ...]


def _freshness_status(last: datetime | None, now: datetime, expected: timedelta) -> ComponentStatus:
    if last is None:
        return "unknown"
    age = now - last
    if age <= expected * 2:
        return "healthy"
    if age <= expected * 6:
        return "stale"
    return "down"


def _backlog_age(last: MailWorkerHeartbeat | None, now: datetime) -> LagMeasurement:
    if last is None or last.backlog_count is None:
        return LagMeasurement(name="backlog_age", status=LagStatus.UNKNOWN, reason="backlog not reported")
    if last.backlog_count == 0:
        return LagMeasurement(name="backlog_age", status=LagStatus.MEASURED, value_seconds=0)
    return _lag("backlog_age", last.backlog_oldest_at, now, missing="backlog age not reported")


def mail_worker_coverage(
    mailbox_binding_id: UUID,
    heartbeats: Sequence[MailWorkerHeartbeat],
    *,
    now: datetime,
    window_start: datetime,
    heartbeat_interval: timedelta,
    reconcile_interval: timedelta,
    slack_last_accepted_at: datetime | None = None,
    slack_expected_interval: timedelta | None = None,
    mcp_last_read_at: datetime | None = None,
    mcp_expected_interval: timedelta | None = None,
) -> MailCoverageReport:
    """Heartbeat gaps (sleep/offline), Outlook connection, sync lag, last reconciliation,
    backlog age and Slack/MCP health reported separately (spec 37.6).

    A gap is any stretch longer than twice the heartbeat interval without a heartbeat, including
    from ``window_start`` to the first heartbeat and from the last heartbeat to ``now``.
    ``monitoring_active`` is true only when the heartbeat and reconciliation are fresh and Outlook
    reports a connection; Slack/MCP status never implies dot processed anything.
    """
    current, start = _aware(now), _aware(window_start)
    if heartbeat_interval <= timedelta(0) or reconcile_interval <= timedelta(0):
        raise ValidationFailed("intervals must be positive")
    ordered = sorted(
        (h for h in heartbeats if h.observed_at <= current + CLOCK_SKEW_TOLERANCE),
        key=lambda h: h.observed_at,
    )
    subject = str(mailbox_binding_id)
    allowed = heartbeat_interval * 2
    gaps: list[CoverageGap] = []
    cursor = start
    for beat in ordered:
        if beat.observed_at < start:
            cursor = max(cursor, beat.observed_at)
            continue
        if beat.observed_at - cursor > allowed:
            gaps.append(
                CoverageGap(subject=subject, start=cursor, end=beat.observed_at, reason="worker_offline")
            )
        cursor = max(cursor, beat.observed_at)
    if current - cursor > allowed:
        gaps.append(CoverageGap(subject=subject, start=cursor, end=current, reason="worker_offline"))
    last = ordered[-1] if ordered else None
    heartbeat_status = _freshness_status(last.observed_at if last else None, current, heartbeat_interval)
    if last is None or last.outlook_connected is None or heartbeat_status != "healthy":
        outlook_status: ComponentStatus = (
            "unknown" if last is None or last.outlook_connected is None else "stale"
        )
    else:
        outlook_status = "healthy" if last.outlook_connected else "down"
    reconciliation = _max(*(h.last_reconciliation_completed_at for h in ordered))
    reconciliation_status = _freshness_status(reconciliation, current, reconcile_interval)
    sync_lag = _lag(
        "mailbox_sync_lag",
        last.mailbox_last_sync_at if last else None,
        last.observed_at if last else None,
        missing="no mailbox sync time reported",
    )
    backlog_age = _backlog_age(last, current)
    slack_status: ComponentStatus = (
        _freshness_status(_utc(slack_last_accepted_at), current, slack_expected_interval)
        if slack_expected_interval is not None
        else ("unknown" if slack_last_accepted_at is None else "healthy")
    )
    mcp_status: ComponentStatus = (
        _freshness_status(_utc(mcp_last_read_at), current, mcp_expected_interval)
        if mcp_expected_interval is not None
        else ("unknown" if mcp_last_read_at is None else "healthy")
    )
    reasons: list[str] = []
    if heartbeat_status != "healthy":
        reasons.append(f"worker heartbeat {heartbeat_status}")
    if outlook_status != "healthy":
        reasons.append(f"Outlook connection {outlook_status}")
    if reconciliation_status != "healthy":
        reasons.append(f"reconciliation {reconciliation_status}")
    if gaps:
        reasons.append(f"{len(gaps)} coverage gap(s) in the window")
    monitoring = (
        heartbeat_status == "healthy" and outlook_status == "healthy" and reconciliation_status == "healthy"
    )
    return MailCoverageReport(
        mailbox_binding_id=mailbox_binding_id,
        generated_at=current,
        last_heartbeat_at=last.observed_at if last else None,
        heartbeat_status=heartbeat_status,
        outlook_status=outlook_status,
        mailbox_sync_lag=sync_lag,
        last_successful_reconciliation_at=reconciliation,
        reconciliation_status=reconciliation_status,
        backlog_count=last.backlog_count if last else None,
        backlog_age=backlog_age,
        slack_signal_status=slack_status,
        mcp_read_status=mcp_status,
        gaps=tuple(gaps),
        monitoring_active=monitoring,
        reasons=tuple(reasons),
    )


class ReconciliationWindow(BaseModel):
    """Received-time window for the next mailbox reconciliation pass."""

    model_config = _FROZEN

    since: datetime
    until: datetime
    full_rescan_required: bool
    gap: CoverageGap | None = None


def reconciliation_window(
    *,
    mailbox_binding_id: UUID,
    last_complete_scan_at: datetime | None,
    now: datetime,
    overlap: timedelta,
    retention_limit: timedelta,
) -> ReconciliationWindow:
    """Overlapping catch-up window (spec 37.6): from the last complete scan minus ``overlap``.

    Without a checkpoint, or when the checkpoint is older than what the mailbox/local queue
    retains, the window starts at the retention limit and the unrecoverable stretch is returned
    as an explicit gap instead of being silently skipped.
    """
    current = _aware(now)
    if overlap < timedelta(0) or retention_limit <= timedelta(0):
        raise ValidationFailed("overlap must be >= 0 and retention positive")
    floor = current - retention_limit
    subject = str(mailbox_binding_id)
    if last_complete_scan_at is None:
        return ReconciliationWindow(
            since=floor,
            until=current,
            full_rescan_required=True,
            gap=CoverageGap(
                subject=subject, start=floor, end=floor, reason="no_checkpoint_history_before_retention"
            ),
        )
    checkpoint = _aware(last_complete_scan_at)
    if checkpoint > current + CLOCK_SKEW_TOLERANCE:
        raise ValidationFailed("checkpoint lies in the future")
    since = checkpoint - overlap
    if since < floor:
        return ReconciliationWindow(
            since=floor,
            until=current,
            full_rescan_required=True,
            gap=CoverageGap(
                subject=subject, start=since, end=floor, reason="checkpoint_older_than_retention"
            ),
        )
    return ReconciliationWindow(since=since, until=current, full_rescan_required=False)


def advance_checkpoint(
    previous: datetime | None,
    *,
    scanned_until: datetime,
    all_candidates_committed: bool,
) -> datetime | None:
    """A checkpoint advances only after every candidate reply record and upload-queue entry of
    the pass is durably committed; it never moves backwards."""
    target = _aware(scanned_until)
    if not all_candidates_committed:
        return previous
    if previous is None:
        return target
    return max(_aware(previous), target)


__all__ = [
    "CLOCK_SKEW_TOLERANCE",
    "INTERVAL_NOTE",
    "LIFECYCLE_VERSION",
    "OWNER_TIMEZONE",
    "AvailabilityConflict",
    "AvailabilityDecision",
    "AvailabilityEventDraft",
    "AvailabilitySignal",
    "AvailabilitySignalKind",
    "ClusterLifecycle",
    "CoverageGap",
    "CoverageInterval",
    "LagMeasurement",
    "LagStatus",
    "LifecycleObservation",
    "MailCoverageReport",
    "MailWorkerHeartbeat",
    "NewTodayDecision",
    "ReconciliationWindow",
    "ScanRecord",
    "SellerAvailabilityStatement",
    "SourceCoverage",
    "SourceHealth",
    "SourceListingLifecycle",
    "TrustedSourceTime",
    "advance_checkpoint",
    "apply_observation",
    "apply_scan_result",
    "derive_availability_event",
    "derive_cluster_lifecycle",
    "detail_freshness",
    "detect_availability_conflicts",
    "detection_delay",
    "healthy_coverage",
    "is_new_today",
    "mail_reply_detection_lag",
    "mail_worker_coverage",
    "notification_processing_lag",
    "reconciliation_window",
    "source_scan_lag",
]
