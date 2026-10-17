"""Operational read models: health, readiness, sources, overview, settings, identity and outbox.

- Health and readiness never contain secrets, connection strings or URLs with credentials
  (spec 20, 30). Free text written by people (gate notes, readiness details, gap reasons) is
  redacted when it looks like a credential; ``HealthView``/``ReadinessView`` still refuse any
  other string that does, as a backstop.
- Terms status/decision stay separate from technical status (spec 5): a recorded terms decision
  is an audit of the owner's choice, not legal permission.
- Activation gates use the honest completion states of spec 32.
- Settings label the disabled EUR 4,000 manual profile and the PROPOSED contribution threshold
  explicitly (spec 3, 18, 23 screen 7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from suv_deals.domain.enums import (
    AccessState,
    CoverageMode,
    GateStatus,
    OutboxState,
    ProfileKey,
    Role,
    Scope,
    SourceMode,
    TechnicalStatus,
    TermsDecision,
    TermsStatus,
)
from suv_deals.domain.profiles import (
    MANUAL_PROFILE_MAX_EUR,
    BusinessConfig,
    SearchProfile,
)
from suv_deals.domain.sources import RateBudget
from suv_deals.views.common import DecimalStr, Sha256Hex, UtcDatetime, ViewModel, decimal_str

TERMS_MEANING: Final = (
    "A recorded terms decision audits the owner's choice; it is not legal permission and does not "
    "remove rights held by the provider. Technical access is reported separately."
)
COVERAGE_NOTE: Final = (
    "Coverage is limited to configured sources, profiles and page depths; search results are not all "
    "European inventory."
)
ADMIN_NOTE: Final = "Changes to profiles, thresholds, bindings and gates are owner-only (config:admin)."

#: Credential-bearing URLs, connection strings, bearer tokens, webhook/API secrets and
#: ``name=value`` secrets. Plain https links without credentials are allowed.
_FORBIDDEN_TEXT: Final = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/\s@]*@"
    r"|\b(?:postgres|postgresql|mysql|redis|amqp|mongodb)(?:\+[a-z0-9]+)?://"
    r"|\bbearer\s+\S"
    r"|\bwhsec_"
    r"|\bsk-[A-Za-z0-9_-]{8}"
    r"|[?&][a-z_]*(?:token|key|secret|sig|signature|password|jwt|code)="
    r"|\b(?:password|passwd|secret|token|api_?key)\s*[=:]",
    re.IGNORECASE,
)


REDACTED_TEXT: Final = "[redacted: text resembled a credential]"


def redact_secrets(value: str) -> str:
    """The text, or a fixed marker when it looks like a credential (the text is never echoed).

    Free-text fields written by people (gate notes, readiness details, gap reasons) are redacted
    rather than refused, so one unlucky phrase cannot take ``deals_health`` or ``/readyz`` down.
    """
    return REDACTED_TEXT if _FORBIDDEN_TEXT.search(value) else value


def _redact_optional(value: str | None) -> str | None:
    return None if value is None else redact_secrets(value)


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk_strings(item)


class GateView(ViewModel):
    """One activation gate (``ops.activation_gates``, spec 32)."""

    capability: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,79}$")
    dependency: str = Field(min_length=1, max_length=500)
    required_evidence: str = Field(min_length=1, max_length=2000)
    status: GateStatus
    owner: str | None = Field(max_length=200)
    next_action: str | None = Field(max_length=2000)
    checked_at: UtcDatetime | None

    @field_validator("dependency", "required_evidence", "owner", "next_action")
    @classmethod
    def _redact(cls, value: str | None) -> str | None:
        return _redact_optional(value)

    @property
    def is_blocker(self) -> bool:
        return self.status != GateStatus.ACTIVE


# --------------------------------------------------------------------------- health


ComponentState = Literal["ok", "degraded", "unavailable", "not_configured", "unknown"]


class ReadinessCheck(ViewModel):
    name: Literal["database", "schema", "config"]
    status: ComponentState
    detail: str | None = Field(max_length=300)

    @field_validator("detail")
    @classmethod
    def _redact(cls, value: str | None) -> str | None:
        return _redact_optional(value)


class BuildInfo(ViewModel):
    build_id: str = Field(pattern=r"^[A-Za-z0-9._:+-]{1,120}$")
    version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,60}$")
    app_env: Literal["development", "test", "staging", "production"]


class SourceRunState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    DISABLED = "disabled"
    BLOCKED = "blocked"
    PARSER_UNHEALTHY = "parser_unhealthy"
    NOT_SCANNED = "not_scanned"


class SourceCoverageView(ViewModel):
    """Per-source coverage and status: never scanned, blocked and unhealthy stay distinct."""

    source_id: UUID
    source_key: str = Field(max_length=80)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    role: Literal["acquisition", "mk_comparable"]
    state: SourceRunState
    enabled: bool
    paused: bool
    technical_status: TechnicalStatus
    terms_status: TermsStatus
    terms_decision: TermsDecision
    coverage_mode: CoverageMode | None
    last_successful_scan_at: UtcDatetime | None
    last_complete_traversal_at: UtcDatetime | None
    incomplete_since: UtcDatetime | None
    gap_reasons: tuple[str, ...] = Field(max_length=50)

    @field_validator("gap_reasons")
    @classmethod
    def _redact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(redact_secrets(reason) for reason in value)

    @model_validator(mode="after")
    def _state(self) -> SourceCoverageView:
        if self.paused and self.state != SourceRunState.PAUSED:
            raise ValueError("a paused source reads as paused")
        if not self.enabled and self.state == SourceRunState.RUNNING:
            raise ValueError("a disabled source is never running")
        return self


class HealthView(ViewModel):
    """``deals_health``: build, readiness, per-source coverage and activation blockers; no secrets."""

    build: BuildInfo
    ready: bool
    readiness: tuple[ReadinessCheck, ...] = Field(min_length=1, max_length=3)
    source_network_enabled: bool
    bridge_status: Literal["unavailable", "configured", "verified"]
    notification_route: Literal["none", "mcp_events", "slack"]
    sources: tuple[SourceCoverageView, ...] = Field(max_length=200)
    activation_blockers: tuple[GateView, ...] = Field(max_length=100)
    coverage_note: str = COVERAGE_NOTE

    @model_validator(mode="after")
    def _safe(self) -> HealthView:
        if self.ready != all(check.status == "ok" for check in self.readiness):
            raise ValueError("ready is true exactly when every readiness check is ok")
        if any(gate.status == GateStatus.ACTIVE for gate in self.activation_blockers):
            raise ValueError("active gates are not blockers")
        for text in _walk_strings(self.model_dump(mode="json", exclude={"coverage_note"})):
            if _FORBIDDEN_TEXT.search(text):
                raise ValueError("health output must not contain credentials, connection strings or secrets")
        return self


class LivenessView(ViewModel):
    """``/healthz``: process liveness only. Readiness is separate."""

    status: Literal["alive"] = "alive"
    build_id: str = Field(pattern=r"^[A-Za-z0-9._:+-]{1,120}$")


class ReadinessView(ViewModel):
    """``/readyz``: database, schema compatibility and critical configuration (no details leak)."""

    ready: bool
    checks: tuple[ReadinessCheck, ...] = Field(min_length=1, max_length=3)
    build_id: str = Field(pattern=r"^[A-Za-z0-9._:+-]{1,120}$")

    @model_validator(mode="after")
    def _ready(self) -> ReadinessView:
        if self.ready != all(check.status == "ok" for check in self.checks):
            raise ValueError("ready is true exactly when every check is ok")
        for check in self.checks:
            if check.detail is not None and _FORBIDDEN_TEXT.search(check.detail):
                raise ValueError("readiness details must not contain credentials or secrets")
        return self


# --------------------------------------------------------------------------- sources


class TermsView(ViewModel):
    status: TermsStatus
    decision: TermsDecision
    decision_actor: str | None = Field(max_length=200)
    decision_note: str | None = Field(max_length=2000)
    reviewed_at: UtcDatetime | None
    terms_url: str | None = Field(max_length=2048)
    meaning: str = TERMS_MEANING


class ParserHealthView(ViewModel):
    status: Literal["healthy", "degraded", "unhealthy", "insufficient_sample", "unknown"]
    sample_size: int = Field(ge=0)
    reasons: tuple[str, ...] = Field(max_length=50)
    recommended_actions: tuple[str, ...] = Field(max_length=10)
    checked_at: UtcDatetime | None


class TechnicalView(ViewModel):
    status: TechnicalStatus
    mode: SourceMode
    adapter: str = Field(max_length=80)
    adapter_version: str = Field(max_length=40)
    detail_mode: Literal["fetch", "card_only"]
    last_live_smoke_at: UtcDatetime | None
    parser_health: ParserHealthView


class RobotsView(ViewModel):
    policy: Literal["obey"] = "obey"
    last_checked_at: UtcDatetime | None
    revision_hash: Sha256Hex | None
    fetch_status: int | None = Field(ge=100, le=599)
    summary: str | None = Field(max_length=2000)


class RateBudgetView(ViewModel):
    """Configured engineering budget plus today's usage. Never silently increased to catch up."""

    budget: RateBudget
    budget_label: Literal["engineering_default", "owner_approved"] = "engineering_default"
    requests_today: int | None = Field(ge=0)
    bytes_today: int | None = Field(ge=0)
    circuit_state: Literal["closed", "open", "half_open", "unknown"]
    next_request_not_before: UtcDatetime | None
    retry_after_until: UtcDatetime | None


CrawlOutcome = Literal["running", "complete", "budget_limited", "partial", "failed", "blocked", "cancelled"]


class CrawlRunView(ViewModel):
    run_id: UUID
    profile: ProfileKey | None
    partition_key: str = Field(max_length=80)
    coverage_mode: CoverageMode
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    outcome: CrawlOutcome
    pages_fetched: int = Field(ge=0)
    cards_seen: int = Field(ge=0)
    new_listings: int = Field(ge=0)
    changed_listings: int = Field(ge=0)
    detail_jobs_enqueued: int = Field(ge=0)
    detail_jobs_deduplicated: int = Field(ge=0)
    access_state: AccessState | None
    error_code: str | None = Field(max_length=80)
    gap_reasons: tuple[str, ...] = Field(max_length=50)
    adapter_version: str = Field(max_length=40)
    parser_version: str | None = Field(max_length=120)

    @model_validator(mode="after")
    def _finished(self) -> CrawlRunView:
        if (self.outcome == "running") != (self.finished_at is None):
            raise ValueError("a run is running exactly when it has no finish time")
        return self


class SourceStatusView(ViewModel):
    """One registered source (spec 23 screen 6). ``version`` is the ``sources_pause`` expected version."""

    source_id: UUID
    source_key: str = Field(max_length=80)
    display_name: str = Field(max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    role: Literal["acquisition", "mk_comparable"]
    state: SourceRunState
    enabled: bool
    paused: bool
    pause_reason: str | None = Field(max_length=2000)
    paused_at: UtcDatetime | None
    version: int = Field(ge=1)
    terms: TermsView
    technical: TechnicalView
    robots: RobotsView
    rate_budget: RateBudgetView
    last_runs: tuple[CrawlRunView, ...] = Field(max_length=20)
    activation_problems: tuple[str, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def _pause(self) -> SourceStatusView:
        if self.paused and (self.pause_reason is None or self.paused_at is None):
            raise ValueError("a paused source records its reason and time")
        if self.paused and self.state != SourceRunState.PAUSED:
            raise ValueError("a paused source reads as paused")
        if self.enabled and self.activation_problems:
            raise ValueError("an enabled source has no activation problems")
        return self


class SourceListView(ViewModel):
    items: tuple[SourceStatusView, ...] = Field(max_length=200)


class SourcePauseResult(ViewModel):
    """``sources_pause`` result. Pausing never implies a later resume or enable."""

    source_id: UUID
    source_key: str = Field(max_length=80)
    paused: Literal[True] = True
    already_paused: bool
    version: int = Field(ge=1)
    paused_at: UtcDatetime
    reason: str = Field(min_length=3, max_length=2000)
    notice: str = "Paused. Resuming or enabling requires a separate owner action."


# --------------------------------------------------------------------------- overview


class OverviewSourceItem(ViewModel):
    source_id: UUID
    source_key: str = Field(max_length=80)
    display_name: str = Field(max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    state: SourceRunState
    last_successful_scan_at: UtcDatetime | None
    pause_reason: str | None = Field(max_length=2000)


class CoverageGapView(ViewModel):
    source_key: str = Field(max_length=80)
    profile: ProfileKey | None
    partition_key: str | None = Field(max_length=80)
    kind: Literal["not_scanned", "incomplete_scan", "budget_limited", "blocked", "parser_unhealthy", "paused"]
    since: UtcDatetime | None
    reasons: tuple[str, ...] = Field(max_length=50)


class QueueCount(ViewModel):
    profile: ProfileKey
    queue_label: str = Field(max_length=120)
    pending: int = Field(ge=0)
    claimed: int = Field(ge=0)
    needs_information: int = Field(ge=0)


class ReviewCounts(ViewModel):
    pending: int = Field(ge=0)
    claimed: int = Field(ge=0)
    needs_information: int = Field(ge=0)
    watch: int = Field(ge=0)
    shortlisted: int = Field(ge=0)
    by_queue: tuple[QueueCount, ...] = Field(max_length=10)


class DeliveryCounts(ViewModel):
    """Outbox rows needing attention. A failed alert stays visible; it is never discarded."""

    uncertain: int = Field(ge=0)
    blocked: int = Field(ge=0)
    dead_letter: int = Field(ge=0)
    retry_wait: int = Field(ge=0)


class OverviewView(ViewModel):
    """Spec 23 screen 1: sources, scans, coverage gaps, pending reviews, failed deliveries, blockers."""

    sources: tuple[OverviewSourceItem, ...] = Field(max_length=200)
    running_sources: int = Field(ge=0)
    paused_sources: int = Field(ge=0)
    last_successful_scan_at: UtcDatetime | None
    coverage_gaps: tuple[CoverageGapView, ...] = Field(max_length=500)
    pending_reviews: ReviewCounts
    failed_deliveries: DeliveryCounts
    activation_blockers: tuple[GateView, ...] = Field(max_length=100)
    bridge_status: Literal["unavailable", "configured", "verified"]
    coverage_note: str = COVERAGE_NOTE

    @model_validator(mode="after")
    def _counts(self) -> OverviewView:
        if self.running_sources != sum(1 for s in self.sources if s.state == SourceRunState.RUNNING):
            raise ValueError("running_sources must match the listed sources")
        if self.paused_sources != sum(1 for s in self.sources if s.state == SourceRunState.PAUSED):
            raise ValueError("paused_sources must match the listed sources")
        return self


# --------------------------------------------------------------------------- settings


class ProfileView(ViewModel):
    profile_key: ProfileKey
    label: str = Field(max_length=120)
    queue_label: str = Field(max_length=120)
    enabled: bool
    optional: bool
    status_label: str = Field(max_length=200)
    min_price_eur: DecimalStr | None
    max_price_eur: DecimalStr
    max_price_inclusive: bool
    max_mileage_km_exclusive: DecimalStr
    source_countries: tuple[str, ...] = Field(max_length=50)
    config_revision_id: UUID | None
    row_version: int | None = Field(ge=1)

    @model_validator(mode="after")
    def _label(self) -> ProfileView:
        expected = "ENABLED" if self.enabled else "DISABLED"
        if not self.status_label.startswith(expected):
            raise ValueError(f"status_label must start with {expected}")
        return self


def profile_status_label(profile: SearchProfile) -> str:
    """Explicit label; the optional EUR 4,000 profile is never confused with the primary band."""
    state = "ENABLED" if profile.enabled else "DISABLED"
    if profile.key == ProfileKey.PRIMARY:
        return f"{state} - primary acquisition band EUR 2,500.00-3,000.00 (inclusive), < 200,000 km"
    if profile.key == ProfileKey.MANUAL_4000:
        ceiling = f"{MANUAL_PROFILE_MAX_EUR:,.2f}"
        return f"{state} - optional manual-review profile up to EUR {ceiling}; separate queue, not the target"
    return f"{state} - optional below-target watch (< EUR 2,500.00); separate queue"


class ThresholdSettingView(ViewModel):
    amount_eur: DecimalStr
    approval_status: Literal["unapproved", "approved"]
    label: Literal["PROPOSED", "APPROVED"]
    approved_by: str | None = Field(max_length=200)
    approved_at: str | None = Field(max_length=40)
    note: str = Field(max_length=300)

    @model_validator(mode="after")
    def _label(self) -> ThresholdSettingView:
        if (self.label == "PROPOSED") != (self.approval_status == "unapproved"):
            raise ValueError("an unapproved threshold is labelled PROPOSED")
        return self


class RealertPolicyView(ViewModel):
    abs_eur: DecimalStr
    pct: DecimalStr
    approved: bool
    label: Literal["PROPOSED", "APPROVED"]


class MkBandSettingView(ViewModel):
    min_eur: DecimalStr
    max_eur: DecimalStr
    meaning: str = Field(max_length=300)


class DestinationBindingView(ViewModel):
    """An approved notification destination. External ids only: never secrets or webhook URLs."""

    binding_id: UUID
    provider: Literal["slack", "mcp_events"]
    label: str = Field(max_length=120)
    enabled: bool
    approval_recorded: bool
    approved_at: UtcDatetime | None
    verified_at: UtcDatetime | None
    external_workspace_id: str | None = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    external_channel_id: str | None = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    row_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _approval(self) -> DestinationBindingView:
        if self.enabled and not self.approval_recorded:
            raise ValueError("an enabled binding has a recorded approval")
        return self


class ConfigRevisionRef(ViewModel):
    config_revision_id: UUID
    revision: int = Field(ge=1)
    created_at: UtcDatetime
    reason: str | None = Field(max_length=2000)


class SettingsView(ViewModel):
    """Spec 23 screen 7. Read-only here; changes are separate owner-only operations."""

    config_revision: ConfigRevisionRef | None
    profiles: tuple[ProfileView, ...] = Field(min_length=1, max_length=10)
    mk_resale_band: MkBandSettingView
    contribution_threshold: ThresholdSettingView
    price_realert_policy: RealertPolicyView
    destination_bindings: tuple[DestinationBindingView, ...] = Field(max_length=50)
    gates: tuple[GateView, ...] = Field(max_length=100)
    can_administer: bool
    administration_note: str = ADMIN_NOTE

    @model_validator(mode="after")
    def _primary(self) -> SettingsView:
        keys = [p.profile_key for p in self.profiles]
        if ProfileKey.PRIMARY not in keys or len(keys) != len(set(keys)):
            raise ValueError("settings list the primary profile once and each profile at most once")
        return self

    @classmethod
    def from_config(
        cls,
        config: BusinessConfig,
        *,
        config_revision: ConfigRevisionRef | None,
        destination_bindings: Iterable[DestinationBindingView] = (),
        gates: Iterable[GateView] = (),
        can_administer: bool,
        profile_rows: dict[ProfileKey, tuple[UUID, int]] | None = None,
    ) -> SettingsView:
        """Settings from the validated business configuration (labels derived, never free text)."""
        rows = profile_rows or {}
        order = (ProfileKey.PRIMARY, ProfileKey.MANUAL_4000, ProfileKey.BELOW_TARGET_WATCH)
        profiles = []
        for key in order:
            profile = config.profiles.get(key)
            if profile is None:
                continue
            row = rows.get(key)
            profiles.append(
                ProfileView(
                    profile_key=key,
                    label=profile.label,
                    queue_label=profile.queue_label,
                    enabled=profile.enabled,
                    optional=key != ProfileKey.PRIMARY,
                    status_label=profile_status_label(profile),
                    min_price_eur=None
                    if profile.min_price_eur is None
                    else decimal_str(profile.min_price_eur),
                    max_price_eur=decimal_str(profile.max_price_eur),
                    max_price_inclusive=profile.max_price_inclusive,
                    max_mileage_km_exclusive=decimal_str(profile.max_mileage_km_exclusive),
                    source_countries=profile.source_countries,
                    config_revision_id=None if row is None else row[0],
                    row_version=None if row is None else row[1],
                )
            )
        threshold = config.contribution_threshold
        approved = threshold.approval_status == "approved"
        return cls(
            config_revision=config_revision,
            profiles=tuple(profiles),
            mk_resale_band=MkBandSettingView(
                min_eur=decimal_str(config.mk_resale_band.min_eur),
                max_eur=decimal_str(config.mk_resale_band.max_eur),
                meaning=config.mk_resale_band.meaning,
            ),
            contribution_threshold=ThresholdSettingView(
                amount_eur=decimal_str(threshold.amount_eur),
                approval_status="approved" if approved else "unapproved",
                label="APPROVED" if approved else "PROPOSED",
                approved_by=threshold.approved_by,
                approved_at=threshold.approved_at,
                note=(
                    "Owner-approved minimum estimated contribution."
                    if approved
                    else "PROPOSED minimum estimated contribution; not confirmed by the owner. "
                    "Threshold-driven alerts stay off until approved."
                ),
            ),
            price_realert_policy=RealertPolicyView(
                abs_eur=decimal_str(config.price_realert_abs_eur),
                pct=decimal_str(config.price_realert_pct),
                approved=config.price_realert_policy_approved,
                label="APPROVED" if config.price_realert_policy_approved else "PROPOSED",
            ),
            destination_bindings=tuple(destination_bindings),
            gates=tuple(gates),
            can_administer=can_administer,
        )


# --------------------------------------------------------------------------- identity


class WorkspaceView(ViewModel):
    workspace_id: UUID
    name: str = Field(max_length=200)
    display_timezone: str = Field(max_length=64)


class MembershipView(ViewModel):
    workspace_id: UUID
    workspace_name: str = Field(max_length=200)
    role: Role
    active: bool


class MeView(ViewModel):
    """``GET /api/me``: the authenticated principal, selected workspace, role, scopes and memberships."""

    principal_id: UUID
    principal_kind: Literal["user", "mcp_client"]
    display_name: str | None = Field(max_length=200)
    role: Role
    scopes: tuple[Scope, ...] = Field(max_length=len(Scope))
    workspace: WorkspaceView
    memberships: tuple[MembershipView, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def _member(self) -> MeView:
        current = [m for m in self.memberships if m.workspace_id == self.workspace.workspace_id]
        if not current or not current[0].active or current[0].role != self.role:
            raise ValueError("the selected workspace must be an active membership with the same role")
        return self


# --------------------------------------------------------------------------- outbox


class OutboxItemView(ViewModel):
    """A delivery needing attention. Payloads are never returned here."""

    outbox_id: UUID
    event_id: UUID
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,79}$")
    aggregate_type: str = Field(max_length=60)
    aggregate_id: UUID
    aggregate_version: int | None = Field(ge=1)
    state: OutboxState
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    destination_binding_id: UUID | None
    last_error_code: str | None = Field(max_length=80)
    blocker_code: str | None = Field(max_length=80)
    event_created_at: UtcDatetime
    send_attempted_at: UtcDatetime | None
    provider_accepted_at: UtcDatetime | None
    owner_seen_at: UtcDatetime | None
    available_at: UtcDatetime
    is_fixture: bool
    uncertain_notice: str | None = Field(max_length=300)

    @model_validator(mode="after")
    def _states(self) -> OutboxItemView:
        if self.state == OutboxState.BLOCKED and self.blocker_code is None:
            raise ValueError("a blocked delivery names its blocker")
        if self.state == OutboxState.UNCERTAIN and not self.uncertain_notice:
            raise ValueError("an uncertain delivery explains that it may or may not have been accepted")
        if self.is_fixture and self.state not in (OutboxState.BLOCKED, OutboxState.CANCELLED):
            raise ValueError("fixture events are only ever blocked or cancelled")
        return self


class OutboxPage(ViewModel):
    items: tuple[OutboxItemView, ...] = Field(max_length=100)
