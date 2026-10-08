"""Bounded automatic seller inquiries: transactional persistence (spec 37.1-37.5, 37.8, 37.10).

The domain decides (``domain.inquiries``: readiness, binding, duplicate rule, rate caps, seller
cooldown, ``dispatch_preflight``, transition graph, retry and reconciliation policy); this module
reads the records the domain needs, records its decisions and lets the database guards of
migration ``20261006001000_seller_inquiries`` enforce them atomically. There is no approval
state anywhere: the bounded standing authorization (spec 37.1) is the only authority.

Lock order of the inquiry path (docs/schema.md section 10.7 and 11)::

    ops.jobs row (dispatch only) -> app.seller_inquiry_controls -> app.seller_entities
      -> app.seller_inquiries -> ops.inquiry_quota_ledger -> ops.email_delivery_attempts -> ops.outbox

Every reservation, queueing, dispatch, authorization change, pause/resume and suppression takes the
workspace control row ``FOR UPDATE`` first, so all of them are serialised per workspace: two
workers (or a worker and a merge, or a reservation through another alias or another listing of a
plausibly same vehicle) can never both pass the one-inquiry rule, the rolling caps or the seller
cooldown. The database repeats every check inside the state transition (``SV002``) and at commit
(``SV003``); its refusals surface as typed ``AppError``s through ``persistence.errors_map``.

Main entry points:

- standing authorization: ``record_authorization`` / ``record_authorization_file`` (versioned,
  append-only; a revoked version suppresses every untransmitted inquiry with
  ``authorization_revoked``), ``current_authorization``;
- controls: ``ensure_controls``, ``get_controls``, ``control_view``, ``pause`` (kill switch,
  ``expected_version``), ``resume`` (owner; re-qualifies kill-switch / revoked-authorization
  suppressions with one audit event each), ``set_mode``, ``set_limits``;
- quota: ``quota_usage``, ``next_window_at`` (when a cap or the seller cooldown frees);
- qualification: ``read_readiness_inputs`` (one read of every record the readiness decision
  needs), ``open_inquiry``, ``record_readiness``, ``prepare_binding``, ``reserve``, ``queue``;
- dispatch: ``dispatch`` (re-reads everything under the controls/seller/inquiry locks, builds
  ``domain.inquiries.DispatchFacts``, runs ``dispatch_preflight``; ``proceed`` commits
  ``queued -> sending`` plus the send intent (the running ``ops.email_delivery_attempts`` row)
  BEFORE any external I/O; ``cancel_stale``/``hold`` record or return the domain decision);
- outcomes: ``record_outcome`` (fenced by the attempt lease token; a late report can only add
  positive acceptance evidence, never overwrite), ``reconcile`` (``ReconciliationEvidence``; an
  empty Sent Items/provider search keeps the inquiry ``uncertain`` with its reservation and quota
  debit), ``retry`` (guarded ``failed_definite -> queued``), ``reap_expired_attempts`` (a crashed
  or expired attempt becomes ``uncertain``, never re-queued);
- mailbox binding sync: every send-path transition (``sending``, the finalised outcome, a
  reconciliation, the reaper's ``uncertain``) re-publishes the inquiry's binding for the sender's
  active mailbox worker in the same transaction (``mail_workers_repo.publish_inquiry_binding``);
- staleness and suppression: ``cancel_stale_inquiries``, ``cancel_inquiry``, ``requalify``,
  ``add_suppression`` (optionally suppressing matching untransmitted inquiries at once),
  ``remove_suppression`` (audited, never automatic), ``active_suppressions``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    Availability,
    EmailProviderKind,
    InquiryReadiness,
    InquiryState,
    MessageLanguage,
    Scope,
    SuppressionReason,
    Tristate,
    ValuationState,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.inquiries import (
    MAX_READINESS_AGE,
    POSSIBLY_TRANSMITTED_STATES,
    PRE_RESERVATION_STATES,
    WINDOW_15D,
    WINDOW_24H,
    ComparableEvidence,
    CooldownDecision,
    CostEvidence,
    DispatchFacts,
    DisqualifierFacts,
    DuplicateDecision,
    ExistingInquiry,
    InquiryBinding,
    InquiryIdentity,
    InquiryReadinessDecision,
    InquiryReadinessInputs,
    ListingFactsSnapshot,
    PreflightDecision,
    PreflightOutcome,
    QuotaDebit,
    RateCapDecision,
    RateCapPolicy,
    ReconcileDecision,
    ReconciliationEvidence,
    RelatedListingLink,
    SellerInquiryAuthorization,
    SendAttemptEvidence,
    SendAttemptOutcome,
    SenderBinding,
    SenderMode,
    SourceObservationFacts,
    SuppressionRecord,
    TransitionContext,
    VehicleIdentification,
    VehicleIdentityRef,
    bind_inquiry,
    canonical_vehicle_identity,
    dispatch_preflight,
    evaluate_duplicate_contact,
    evaluate_rate_caps,
    evaluate_seller_cooldown,
    load_seller_inquiry_authorization,
    reconcile_uncertain,
    require_transition,
    seller_contact_times,
    should_retry,
)
from suv_deals.domain.language import LanguageDecision
from suv_deals.domain.listings import Co2Info, Documentation, NormalizedListing
from suv_deals.domain.replies import ReplyClaims, normalize_message_id
from suv_deals.domain.seller_contacts import (
    AddressError,
    ContactChange,
    ContactRecheck,
    RecipientDecision,
    canonicalize_address,
    detect_contact_change,
)
from suv_deals.domain.seller_templates import (
    TEMPLATES,
    InquiryPlaceholders,
    RenderedMessage,
    TemplateRenderError,
    build_vehicle_label,
    render,
    render_preview_mk,
    rendering_problems,
    template_for_language,
)
from suv_deals.errors import (
    AppError,
    EmailDeliveryUncertain,
    Forbidden,
    IdempotencyConflict,
    InsufficientData,
    NotFound,
    RateLimited,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.integrations.email_providers.base import (
    SendAccepted,
    SendDefiniteFailure,
    SendUncertain,
    attempt_outcome,
)
from suv_deals.integrations.mime_builder import BuiltMessage, MimeBuildError, build_inquiry_message, mailbox
from suv_deals.persistence import audit, mail_workers_repo, valuation_repo
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors
from suv_deals.persistence.sellers_repo import (
    CANCELLABLE_STATES,
    SellerContactRecord,
    cancel_untransmitted,
    current_contact,
    get_contact,
    language_decision,
    lock_controls,
    recipient_binding,
    recipient_decision,
    require_inquiry_reader,
    require_inquiry_writer,
)
from suv_deals.persistence.sender_bindings_repo import (
    SenderBindingRecord,
    active_binding,
    get_binding,
    sender_status,
)
from suv_deals.views.inquiries import InquiryControlView, InquiryPauseResult, InquiryResumeResult

SuppressionScope = Literal["workspace", "seller", "address", "vehicle", "source", "sender"]
DispatchOutcome = Literal["proceed", "cancelled", "suppressed", "hold"]

_FROZEN = ConfigDict(frozen=True, extra="ignore")
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_PENDING_STATES: Final = tuple(sorted(s.value for s in CANCELLABLE_STATES))
_UUID_RE: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SELLER_KEY_RE: Final = re.compile(rf"^seller_entity:(?P<id>{_UUID_RE})$")
_VEHICLE_KEY_RE: Final = re.compile(rf"^(vehicle_cluster|listing_incarnation):{_UUID_RE}$")
_SOURCE_KEY_RE: Final = re.compile(r"^[a-z0-9_]{3,60}$")
_PLACEHOLDER_RE: Final = re.compile(r"\{\{([a-z_]+)\}\}")
REQUALIFIED_AFTER_RESUME: Final = "REQUALIFIED_AFTER_RESUME"
LEASE_EXPIRED: Final = "LEASE_EXPIRED"


# =============================================================================================
# Records
# =============================================================================================


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


class AuthorizationRecord(BaseModel):
    model_config = _FROZEN

    id: UUID
    version: int
    record_hash: str
    revoked_at: datetime | None = None
    created_at: datetime
    authorization: SellerInquiryAuthorization

    @field_validator("revoked_at", "created_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class InquiryControls(BaseModel):
    model_config = _FROZEN

    id: UUID
    mode: SenderMode
    kill_switch: bool
    kill_switch_reason: str | None = None
    kill_switch_set_at: datetime | None = None
    max_per_24h: int
    max_per_15d: int
    seller_cooldown: timedelta
    version: int
    updated_at: datetime

    @field_validator("kill_switch_set_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def paused(self) -> bool:
        return self.kill_switch or self.mode == "paused"

    def policy(self) -> RateCapPolicy:
        return RateCapPolicy(
            max_per_24h=self.max_per_24h, max_per_15d=self.max_per_15d, seller_cooldown=self.seller_cooldown
        )


class InquiryRecord(BaseModel):
    """One ``app.seller_inquiries`` row (system/owner only: holds the message and addresses)."""

    model_config = _FROZEN

    id: UUID
    identity_key: str
    vehicle_kind: Literal["vehicle_cluster", "listing_incarnation"]
    vehicle_cluster_id: UUID | None = None
    vehicle_listing_id: UUID | None = None
    vehicle_key: str
    seller_entity_id: UUID
    qualification_listing_id: UUID
    qualification_revision_id: UUID | None = None
    qualification_revision_number: int | None = None
    qualified_semantic_hash: str | None = None
    qualified_price_minor: int | None = None
    qualified_currency: str | None = None
    qualified_availability: Availability | None = None
    readiness: InquiryReadiness
    readiness_reasons: tuple[str, ...] = ()
    readiness_rationale_hash: str | None = None
    readiness_rules_version: str | None = None
    readiness_evaluated_at: datetime | None = None
    authorization_id: UUID | None = None
    authorization_version: int | None = None
    authorization_fingerprint: str | None = None
    template_id: str | None = None
    template_version: int | None = None
    template_hash: str | None = None
    template_set_version: str | None = None
    language: MessageLanguage | None = None
    scope_hash: str | None = None
    body_hash: str | None = None
    binding_hash: str | None = None
    original_subject: str | None = None
    original_body: str | None = None
    mk_preview_subject: str | None = None
    mk_preview_body: str | None = None
    mk_preview_hash: str | None = None
    sender_binding_id: UUID | None = None
    sender_binding_version: int | None = None
    sender_provider: EmailProviderKind | None = None
    sender_account_id: str | None = None
    sender_from_address: str | None = None
    sender_display_name: str | None = None
    sender_reply_to_address: str | None = None
    recipient_contact_id: UUID | None = None
    recipient_address: str | None = None
    recipient_binding_hash: str | None = None
    state: InquiryState
    state_reasons: tuple[str, ...] = ()
    suppression_reason: SuppressionReason | None = None
    rfc_message_id: str | None = None
    provider_message_id: str | None = None
    provider_thread_id: str | None = None
    reserved_at: datetime | None = None
    queued_at: datetime | None = None
    send_attempted_at: datetime | None = None
    accepted_at: datetime | None = None
    replied_at: datetime | None = None
    state_changed_at: datetime
    row_version: int
    created_at: datetime

    @field_validator(
        "readiness_evaluated_at",
        "reserved_at",
        "queued_at",
        "send_attempted_at",
        "accepted_at",
        "replied_at",
        "state_changed_at",
        "created_at",
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def seller_key(self) -> str:
        return f"seller_entity:{self.seller_entity_id}"

    @property
    def vehicle(self) -> VehicleIdentityRef:
        vehicle_id = self.vehicle_cluster_id or self.vehicle_listing_id
        assert vehicle_id is not None
        return VehicleIdentityRef(kind=self.vehicle_kind, id=vehicle_id)

    @property
    def possibly_transmitted(self) -> bool:
        return self.send_attempted_at is not None or self.state in POSSIBLY_TRANSMITTED_STATES


class AttemptRecord(BaseModel):
    """One ``ops.email_delivery_attempts`` row (the committed send intent and its outcome)."""

    model_config = _FROZEN

    id: UUID
    attempt_id: UUID
    inquiry_id: UUID
    attempt_number: int
    outbox_id: UUID | None = None
    job_id: UUID | None = None
    sender_binding_id: UUID
    sender_binding_version: int
    provider: EmailProviderKind
    rfc_message_id: str | None = None
    fencing_token: int
    lease_owner: str
    lease_token: UUID
    lease_expires_at: datetime
    send_intent_committed_at: datetime
    outcome: SendAttemptOutcome
    finished_at: datetime | None = None
    pre_submission_proof: str | None = None
    provider_idempotency_key: str | None = None
    provider_idempotency_documented: bool = False
    provider_message_id: str | None = None
    provider_thread_id: str | None = None
    provider_response: dict[str, Any] | None = None
    error_code: str | None = None
    reconciled_outcome: Literal["accepted", "proven_not_submitted"] | None = None
    reconciled_at: datetime | None = None
    reconciliation_evidence: dict[str, Any] | None = None
    submission_uncertain: bool

    @field_validator("lease_expires_at", "send_intent_committed_at", "finished_at", "reconciled_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    def evidence(self) -> SendAttemptEvidence:
        """The attempt as ``domain.inquiries.SendAttemptEvidence`` (reconciliation applied)."""
        outcome = self.outcome
        proof = self.pre_submission_proof
        recon = self.reconciliation_evidence or {}
        worker_alive = Tristate.UNKNOWN if outcome == SendAttemptOutcome.RUNNING else Tristate.NO
        if self.reconciled_outcome == "proven_not_submitted":
            outcome = SendAttemptOutcome.PRE_SUBMISSION_FAILURE
            proof = recon.get("proven_not_submitted")
        elif self.reconciled_outcome == "accepted":
            outcome = SendAttemptOutcome.ACCEPTED
        sent_items = recon.get("sent_items", "not_searched")
        provider_search = recon.get("provider_search", "not_searched")
        return SendAttemptEvidence.model_validate(
            {
                "attempt_id": self.attempt_id,
                "inquiry_id": self.inquiry_id,
                "sender_binding_id": self.sender_binding_id,
                "provider": self.provider,
                "fencing_token": self.fencing_token,
                "started_at": self.send_intent_committed_at,
                "finished_at": self.finished_at,
                "outcome": outcome,
                "pre_submission_proof": proof
                if outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE
                else None,
                "worker_alive": worker_alive,
                "lease_expires_at": self.lease_expires_at,
                "provider_idempotency_key": self.provider_idempotency_key,
                "provider_idempotency_documented": self.provider_idempotency_documented,
                "stable_message_id": self.rfc_message_id,
                "sent_items_search": sent_items
                if sent_items in ("not_searched", "found", "not_found")
                else "not_searched",
                "provider_search": provider_search
                if provider_search in ("not_searched", "found", "not_found", "unsupported")
                else "not_searched",
            }
        )


class AttemptLease(BaseModel):
    """The fence of a send attempt: the dispatch job's lease, or the local worker's intent TTL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str = Field(min_length=1, max_length=200)
    token: UUID
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class QuotaUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    count_24h: int
    count_15d: int


class WindowDecision(BaseModel):
    """When the next inquiry may use the quota / contact the seller (``next_at=None`` = now or never)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    rate_caps: RateCapDecision
    cooldown: CooldownDecision | None = None
    blocked: bool
    next_at: datetime | None = None

    def delay(self) -> timedelta | None:
        """Delay until ``next_at`` (``None`` when nothing blocks or no end is known)."""
        if self.next_at is None:
            return None
        return max(timedelta(0), self.next_at - self.now)


class ReadinessEvaluation(BaseModel):
    """Evidence computed by the valuation pipeline (overrides what the records hold)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    screening: ScreeningResult | None = None
    vehicle: VehicleIdentification | None = None
    comparables: ComparableEvidence | None = None
    costs: CostEvidence | None = None
    documentation: Documentation | None = None
    co2: Co2Info | None = None
    fraud_warnings: tuple[str, ...] = ()


class ReadinessSnapshot(BaseModel):
    """Everything ``evaluate_inquiry_readiness`` needs, read in one pass, plus what it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: InquiryReadinessInputs
    identity: InquiryIdentity
    contact: SellerContactRecord | None = None
    sender_binding: SenderBindingRecord | None = None
    authorization_id: UUID
    vehicle_label: tuple[str | None, str | None, str | None] = (None, None, None)


class DispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DispatchOutcome
    inquiry_id: UUID
    decision: PreflightDecision
    attempt: AttemptRecord | None = None
    message: BuiltMessage | None = None
    sender: SenderBinding | None = None
    next_attempt_at: datetime | None = None


class OutcomeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool
    inquiry_state: InquiryState
    attempt_outcome: SendAttemptOutcome
    reconciled: bool = False
    note: str | None = None


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ReconcileDecision
    inquiry_state: InquiryState
    attempt_id: UUID


class SuppressionRow(BaseModel):
    model_config = _FROZEN

    id: UUID
    scope: SuppressionScope
    scope_key: str
    match_key: str
    reason: SuppressionReason
    effective_at: datetime
    inquiry_id: UUID | None = None
    reply_id: UUID | None = None
    created_by_kind: Literal["user", "mcp_client", "system"]
    removed_at: datetime | None = None
    removal_reason: str | None = None
    removal_audit_id: UUID | None = None

    @field_validator("effective_at", "removed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    def record(self) -> SuppressionRecord:
        return SuppressionRecord(
            suppression_id=self.id,
            scope=self.scope,
            key=self.scope_key,
            reason=self.reason,
            effective_at=self.effective_at,
            removed_at=self.removed_at,
            removal_audit_id=self.removal_audit_id,
        )


class SuppressionAdded(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suppression: SuppressionRow
    created: bool
    suppressed_inquiry_ids: tuple[UUID, ...] = ()


# =============================================================================================
# Column lists and small helpers
# =============================================================================================

_INQUIRY_COLUMNS: Final = (
    "id, identity_key, vehicle_kind, vehicle_cluster_id, vehicle_listing_id, vehicle_key, seller_entity_id,"
    " qualification_listing_id, qualification_revision_id, qualification_revision_number,"
    " qualified_semantic_hash, qualified_price_minor, qualified_currency, qualified_availability, readiness,"
    " readiness_reasons, readiness_rationale_hash, readiness_rules_version, readiness_evaluated_at,"
    " authorization_id, authorization_version, authorization_fingerprint, template_id, template_version,"
    " template_hash, template_set_version, language, scope_hash, body_hash, binding_hash, original_subject,"
    " original_body, mk_preview_subject, mk_preview_body, mk_preview_hash, sender_binding_id,"
    " sender_binding_version, sender_provider, sender_account_id, sender_from_address, sender_display_name,"
    " sender_reply_to_address, recipient_contact_id, recipient_address, recipient_binding_hash, state,"
    " state_reasons, suppression_reason, rfc_message_id, provider_message_id, provider_thread_id,"
    " reserved_at, queued_at, send_attempted_at, accepted_at, replied_at, state_changed_at, row_version,"
    " created_at"
)
_SELECT_INQUIRY: Final = f"select {_INQUIRY_COLUMNS} from app.seller_inquiries"  # noqa: S608
_ATTEMPT_COLUMNS: Final = (
    "id, attempt_id, inquiry_id, attempt_number, outbox_id, job_id, sender_binding_id,"
    " sender_binding_version,"
    " provider, rfc_message_id, fencing_token, lease_owner, lease_token, lease_expires_at,"
    " send_intent_committed_at, outcome, finished_at, pre_submission_proof, provider_idempotency_key,"
    " provider_idempotency_documented, provider_message_id, provider_thread_id, provider_response,"
    " error_code, reconciled_outcome, reconciled_at, reconciliation_evidence, submission_uncertain"
)
_SELECT_ATTEMPT: Final = f"select {_ATTEMPT_COLUMNS} from ops.email_delivery_attempts"  # noqa: S608
_SUPPRESSION_COLUMNS: Final = (
    "id, scope, scope_key, match_key, reason, effective_at, inquiry_id, reply_id, created_by_kind,"
    " removed_at, removal_reason, removal_audit_id"
)
_SELECT_SUPPRESSION: Final = f"select {_SUPPRESSION_COLUMNS} from ops.email_suppressions"  # noqa: S608
_CONTROL_COLUMNS: Final = (
    "id, mode, kill_switch, kill_switch_reason, kill_switch_set_at, max_per_24h, max_per_15d,"
    " seller_cooldown, version, updated_at"
)


def _inquiry(row: Mapping[str, Any]) -> InquiryRecord:
    data = dict(row)
    data["readiness_reasons"] = tuple(data.get("readiness_reasons") or ())
    data["state_reasons"] = tuple(data.get("state_reasons") or ())
    return InquiryRecord.model_validate(data)


def _reason_text(reason: str, minimum: int = 3, maximum: int = 2000) -> str:
    text = " ".join(str(reason).split())[:maximum]
    if len(text) < minimum:
        raise ValidationFailed(f"a reason of at least {minimum} characters is required")
    return text


def _codes(values: Iterable[str], limit: int = 30) -> list[str]:
    return [c for c in dict.fromkeys(values) if _CODE_RE.fullmatch(c)][:limit]


async def _now(conn: Conn) -> datetime:
    row = await fetch_one(conn, "select now() as tx, clock_timestamp() as wall")
    assert row is not None
    return ensure_utc(row["wall"])


async def _tx_now(conn: Conn) -> datetime:
    row = await fetch_one(conn, "select now() as tx")
    assert row is not None
    return ensure_utc(row["tx"])


async def _publish_binding(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> None:
    """Re-publish the inquiry's mailbox binding after a send-path state change (spec 37.8).

    ``mail_workers_repo.publish_inquiry_binding`` appends a new binding version for the sender's
    active mailbox worker only when the state or payload changed (no mailbox: nothing happens), so
    the desktop worker can link replies, bounces and Sent Items evidence to every send intent.
    """
    await mail_workers_repo.publish_inquiry_binding(conn, actor, inquiry_id)


async def get_inquiry(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> InquiryRecord:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT_INQUIRY + " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": inquiry_id},
        )
    if row is None:
        raise NotFound("Seller inquiry not found")
    return _inquiry(row)


async def _lock_inquiry(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> InquiryRecord:
    """Seller entity row, then the inquiry row, both ``FOR UPDATE`` (after the controls row)."""
    ws = actor.workspace_id
    async with mapped_errors():
        head = await fetch_one(
            conn,
            "select seller_entity_id from app.seller_inquiries where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": inquiry_id},
        )
        if head is None:
            raise NotFound("Seller inquiry not found")
        await conn.execute(
            "select 1 from app.seller_entities where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": ws, "id": head["seller_entity_id"]},
        )
        row = await fetch_one(
            conn,
            _SELECT_INQUIRY + " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": ws, "id": inquiry_id},
        )
    if row is None:  # pragma: no cover - inquiries are never deleted
        raise NotFound("Seller inquiry not found")
    return _inquiry(row)


async def list_attempts(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> list[AttemptRecord]:
    require_inquiry_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SELECT_ATTEMPT + " where workspace_id = %(ws)s and inquiry_id = %(id)s order by attempt_number",
            {"ws": actor.workspace_id, "id": inquiry_id},
        )
    return [AttemptRecord.model_validate(r) for r in rows]


async def get_attempt(conn: Conn, actor: ActorContext, attempt_id: UUID) -> AttemptRecord:
    """An attempt by its durable ``attempt_id`` (== the Outlook send-intent id)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT_ATTEMPT + " where workspace_id = %(ws)s and attempt_id = %(id)s",
            {"ws": actor.workspace_id, "id": attempt_id},
        )
    if row is None:
        raise NotFound("Send attempt not found")
    return AttemptRecord.model_validate(row)


# =============================================================================================
# Standing authorization
# =============================================================================================

_AUTH_SELECT: Final = (
    "select id, version, record_hash, revoked_at, created_at, record from app.seller_inquiry_authorizations"
)


def _authorization(row: Mapping[str, Any]) -> AuthorizationRecord:
    try:
        authorization = SellerInquiryAuthorization.model_validate(row["record"])
    except ValidationError as exc:
        raise ValidationFailed("the stored standing authorization record is invalid") from exc
    return AuthorizationRecord.model_validate({**dict(row), "authorization": authorization})


async def current_authorization(conn: Conn, actor: ActorContext) -> AuthorizationRecord | None:
    """The workspace's latest authorization version (the only one reservations may bind)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _AUTH_SELECT + " where workspace_id = %(ws)s order by version desc limit 1",
            {"ws": actor.workspace_id},
        )
    return None if row is None else _authorization(row)


async def record_authorization(
    conn: Conn, actor: ActorContext, authorization: SellerInquiryAuthorization, *, reason: str
) -> AuthorizationRecord:
    """Append the next authorization version (idempotent for an identical latest version).

    A revoked version suppresses every untransmitted inquiry (``authorization_revoked``) in the
    same transaction; (possibly) transmitted ones keep their state and evidence.
    """
    require_inquiry_writer(actor)
    text = _reason_text(reason, maximum=1000)
    ws = actor.workspace_id
    await lock_controls(conn, ws)
    fingerprint = authorization.fingerprint()
    async with mapped_errors():
        latest = await fetch_one(
            conn,
            _AUTH_SELECT + " where workspace_id = %(ws)s order by version desc limit 1",
            {"ws": ws},
        )
    if latest is not None and int(latest["version"]) == authorization.version:
        if latest["record_hash"] != fingerprint:
            raise IdempotencyConflict("This authorization version was already recorded with other content")
        return _authorization(latest)
    expected = 1 if latest is None else int(latest["version"]) + 1
    if authorization.version != expected:
        raise VersionConflict(
            "Authorization versions are consecutive", expected_version=expected, current_version=expected - 1
        )
    revocation = authorization.revocation
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.seller_inquiry_authorizations (workspace_id, version, owner_label,"
            " effective_date,"
            " recorded_at, source, approval_mode, purpose, questions, recipient_class,"
            " max_inquiries_per_vehicle_seller_pair, scope_version, languages,"
            " english_requires_positive_evidence,"
            " allowed_outgoing_data_categories, excluded_data_categories, attachments_allowed,"
            " cc_bcc_allowed,"
            " additional_recipients_allowed, follow_ups_allowed, not_authorized, profiles_in_scope,"
            " revoked_at,"
            " revoked_by, revoke_reason, record, record_hash, created_by_principal_id)"
            " values (%(ws)s, %(version)s, %(owner)s, %(effective)s, %(recorded)s, %(source)s, %(mode)s,"
            " %(purpose)s, %(questions)s, %(recipients)s, %(pair)s, %(scope)s, %(languages)s, true,"
            " %(allowed)s,"
            " %(excluded)s, false, false, false, false, %(not_authorized)s, %(profiles)s, %(revoked_at)s,"
            " %(revoked_by)s, %(revoke_reason)s, %(record)s, %(hash)s, %(by)s)"
            " returning id",
            {
                "ws": ws,
                "version": authorization.version,
                "owner": authorization.owner,
                "effective": authorization.effective_date,
                "recorded": authorization.recorded_at,
                "source": authorization.source,
                "mode": authorization.approval_mode,
                "purpose": authorization.purpose,
                "questions": [q.value for q in authorization.questions],
                "recipients": authorization.recipient_class,
                "pair": authorization.max_inquiries_per_vehicle_seller_pair,
                "scope": authorization.scope_version,
                "languages": [lang.value for lang in authorization.languages],
                "allowed": list(authorization.allowed_outgoing_data_categories),
                "excluded": list(authorization.excluded_data_categories),
                "not_authorized": list(authorization.not_authorized),
                "profiles": [p.value for p in authorization.profiles_in_scope],
                "revoked_at": revocation.revoked_at if revocation.revoked else None,
                "revoked_by": revocation.revoked_by if revocation.revoked else None,
                "revoke_reason": revocation.reason if revocation.revoked else None,
                "record": Jsonb(authorization.model_dump(mode="json")),
                "hash": fingerprint,
                "by": actor.principal_id,
            },
        )
    assert row is not None
    authorization_id: UUID = row["id"]
    await audit.record(
        conn,
        actor,
        "seller_inquiry_authorization.record",
        "seller_inquiry_authorization",
        authorization_id,
        new_version=authorization.version,
        reason=text,
        metadata={"revoked": revocation.revoked, "fingerprint": fingerprint},
    )
    if revocation.revoked:
        async with mapped_errors():
            pending = await fetch_all(
                conn,
                "select id from app.seller_inquiries where workspace_id = %(ws)s and state = any(%(states)s)"
                " order by id",
                {"ws": ws, "states": list(_PENDING_STATES)},
            )
        await cancel_untransmitted(
            conn,
            actor,
            [r["id"] for r in pending],
            reasons=["AUTHORIZATION_REVOKED"],
            target="suppressed",
            suppression_reason=SuppressionReason.AUTHORIZATION_REVOKED.value,
        )
    async with mapped_errors():
        stored = await fetch_one(
            conn,
            _AUTH_SELECT + " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": authorization_id},
        )
    assert stored is not None
    return _authorization(stored)


async def record_authorization_file(
    conn: Conn, actor: ActorContext, path: Path | None = None, *, reason: str
) -> AuthorizationRecord:
    """Record ``config/seller_inquiry_authorization.yaml`` (or ``path``) as the next version."""
    return await record_authorization(conn, actor, load_seller_inquiry_authorization(path), reason=reason)


# =============================================================================================
# Controls: kill switch, pause/resume, mode and caps
# =============================================================================================


def _controls(row: Mapping[str, Any]) -> InquiryControls:
    return InquiryControls.model_validate(dict(row))


async def get_controls(conn: Conn, actor: ActorContext) -> InquiryControls | None:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_CONTROL_COLUMNS} from app.seller_inquiry_controls where workspace_id = %(ws)s",  # noqa: S608
            {"ws": actor.workspace_id},
        )
    return None if row is None else _controls(row)


async def ensure_controls(conn: Conn, actor: ActorContext) -> InquiryControls:
    """Create the workspace control row (mode ``disabled_until_sender_ready``) if missing."""
    require_inquiry_writer(actor)
    async with mapped_errors():
        await conn.execute(
            "insert into app.seller_inquiry_controls (workspace_id) values (%(ws)s)"
            " on conflict (workspace_id) do nothing",
            {"ws": actor.workspace_id},
        )
    controls = await get_controls(conn, actor)
    assert controls is not None
    return controls


async def _locked_controls(conn: Conn, actor: ActorContext) -> InquiryControls:
    row = await lock_controls(conn, actor.workspace_id)
    if row is None:
        raise VersionConflict(
            "Seller inquiries are not configured for this workspace", reason="inquiry_controls_missing"
        )
    return _controls(row)


async def quota_usage(
    conn: Conn, actor: ActorContext, *, exclude_inquiry_id: UUID | None = None
) -> QuotaUsage:
    """Unreleased debits in the rolling windows, counted exactly as the repository enforces them
    (``_debits``: at the later of reservation and the latest possible hand-over)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        now = await _now(conn)
        debits = await _debits(conn, actor.workspace_id)
    times = [d.counted_at for d in debits if d.inquiry_id != exclude_inquiry_id]
    return QuotaUsage(
        count_24h=sum(1 for t in times if t > now - WINDOW_24H),
        count_15d=sum(1 for t in times if t > now - WINDOW_15D),
    )


async def rate_cap_decision(
    conn: Conn,
    actor: ActorContext,
    *,
    policy: RateCapPolicy,
    exclude_inquiry_id: UUID | None = None,
) -> RateCapDecision:
    """``evaluate_rate_caps`` over ``_debits`` at the database's wall clock (the claim re-check)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        now = await _now(conn)
        debits = await _debits(conn, actor.workspace_id)
    return evaluate_rate_caps(debits, now=now, policy=policy, exclude_inquiry_id=exclude_inquiry_id)


async def control_view(conn: Conn, actor: ActorContext) -> InquiryControlView:
    """``GET /api/inquiry-control`` data (``InquiryControlView``)."""
    controls = await get_controls(conn, actor)
    if controls is None:
        raise NotFound("Seller inquiry controls not found")
    usage = await quota_usage(conn, actor)
    return InquiryControlView(
        version=controls.version,
        mode=controls.mode,
        kill_switch=controls.kill_switch,
        kill_switch_reason=controls.kill_switch_reason,
        kill_switch_set_at=controls.kill_switch_set_at,
        max_per_24h=controls.max_per_24h,
        max_per_15d=controls.max_per_15d,
        seller_cooldown_seconds=InquiryControlView.cooldown_seconds(controls.seller_cooldown),
        used_24h=usage.count_24h,
        used_15d=usage.count_15d,
        updated_at=controls.updated_at,
    )


async def pause(conn: Conn, actor: ActorContext, *, expected_version: int, reason: str) -> InquiryPauseResult:
    """``seller_inquiries_pause``: activate the kill switch against ``expected_version``.

    Untransmitted work stops at the next guard (reservation, dispatch, worker claim). Pausing an
    already paused control answers ``already_paused`` without a change. Never resumes anything.
    """
    actor.require(Scope.INQUIRIES_PAUSE)
    text = _reason_text(reason)
    controls = await _locked_controls(conn, actor)
    if controls.version != expected_version:
        raise VersionConflict(
            "The inquiry controls changed; reload and retry", current_version=controls.version
        )
    if controls.kill_switch:
        assert controls.kill_switch_set_at is not None
        return InquiryPauseResult(
            version=controls.version,
            already_paused=True,
            kill_switch_set_at=controls.kill_switch_set_at,
            mode=controls.mode,
        )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update app.seller_inquiry_controls set kill_switch = true, kill_switch_reason = %(reason)s,"
            " kill_switch_set_at = now(), kill_switch_set_by = %(by)s, updated_by = %(by)s,"
            " update_reason = %(reason)s, version = version + 1"
            " where workspace_id = %(ws)s and version = %(expected)s"
            " returning version, kill_switch_set_at, mode",
            {
                "ws": actor.workspace_id,
                "reason": text,
                "by": actor.principal_id,
                "expected": expected_version,
            },
        )
    if row is None:  # pragma: no cover - the row is locked above
        raise VersionConflict("The inquiry controls changed; reload and retry")
    await audit.record(
        conn,
        actor,
        "seller_inquiry_controls.pause",
        "seller_inquiry_controls",
        controls.id,
        prior_version=controls.version,
        new_version=int(row["version"]),
        reason=text,
    )
    return InquiryPauseResult(
        version=int(row["version"]),
        already_paused=False,
        kill_switch_set_at=ensure_utc(row["kill_switch_set_at"]),
        mode=row["mode"],
    )


async def resume(
    conn: Conn, actor: ActorContext, *, expected_version: int, reason: str
) -> InquiryResumeResult:
    """Owner-only resume (``config:admin``, dashboard): kill switch off, with audit.

    Inquiries the kill switch suppressed (and, while the current authorization is effective and
    unrevoked, those suppressed for a revoked authorization) were never transmitted; each is
    re-qualified with its own audit event (``suppressed -> qualifying``). They are reserved again
    only after a fresh readiness decision; nothing is sent by the resume itself.
    """
    actor.require(Scope.CONFIG_ADMIN)
    text = _reason_text(reason)
    controls = await _locked_controls(conn, actor)
    if controls.version != expected_version:
        raise VersionConflict(
            "The inquiry controls changed; reload and retry", current_version=controls.version
        )
    version = controls.version
    resumed_at = controls.updated_at
    if controls.kill_switch:
        async with mapped_errors():
            row = await fetch_one(
                conn,
                "update app.seller_inquiry_controls set kill_switch = false, kill_switch_reason = null,"
                " kill_switch_set_at = null, kill_switch_set_by = null, updated_by = %(by)s,"
                " update_reason = %(reason)s, version = version + 1"
                " where workspace_id = %(ws)s and version = %(expected)s returning version, updated_at",
                {
                    "ws": actor.workspace_id,
                    "reason": text,
                    "by": actor.principal_id,
                    "expected": expected_version,
                },
            )
        assert row is not None
        version = int(row["version"])
        resumed_at = ensure_utc(row["updated_at"])
        await audit.record(
            conn,
            actor,
            "seller_inquiry_controls.resume",
            "seller_inquiry_controls",
            controls.id,
            prior_version=controls.version,
            new_version=version,
            reason=text,
        )
    reasons = [SuppressionReason.KILL_SWITCH.value]
    authorization = await current_authorization(conn, actor)
    if authorization is not None and not authorization.authorization.problems_at(resumed_at):
        reasons.append(SuppressionReason.AUTHORIZATION_REVOKED.value)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select id from app.seller_inquiries where workspace_id = %(ws)s and state = 'suppressed'"
            " and suppression_reason = any(%(reasons)s) and send_attempted_at is null order by id",
            {"ws": actor.workspace_id, "reasons": reasons},
        )
    for row_item in rows:
        await _requalify(conn, actor, row_item["id"], reason=f"resume: {text}", code=REQUALIFIED_AFTER_RESUME)
    return InquiryResumeResult(version=version, mode=controls.mode, resumed_at=resumed_at)


async def set_mode(
    conn: Conn, actor: ActorContext, *, expected_version: int, mode: SenderMode, reason: str
) -> InquiryControls:
    """Owner/system mode change (``automatic`` only after the sender binding is verified)."""
    require_inquiry_writer(actor)
    text = _reason_text(reason)
    controls = await _locked_controls(conn, actor)
    if controls.version != expected_version:
        raise VersionConflict(
            "The inquiry controls changed; reload and retry", current_version=controls.version
        )
    if controls.mode == mode:
        return controls
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiry_controls set mode = %(mode)s, updated_by = %(by)s,"
            " update_reason = %(reason)s, version = version + 1 where workspace_id = %(ws)s",
            {"ws": actor.workspace_id, "mode": mode, "by": actor.principal_id, "reason": text},
        )
    await audit.record(
        conn,
        actor,
        "seller_inquiry_controls.mode",
        "seller_inquiry_controls",
        controls.id,
        prior_version=controls.version,
        new_version=controls.version + 1,
        reason=text,
        metadata={"mode": mode},
    )
    updated = await get_controls(conn, actor)
    assert updated is not None
    return updated


async def set_limits(
    conn: Conn,
    actor: ActorContext,
    *,
    expected_version: int,
    max_per_24h: int,
    max_per_15d: int,
    seller_cooldown: timedelta,
    reason: str,
) -> InquiryControls:
    """Owner-controlled ceilings (0..2 per 24 h, 0..5 per 15 days; never above the v1.1 ceilings)."""
    actor.require(Scope.CONFIG_ADMIN)
    text = _reason_text(reason)
    policy = RateCapPolicy(max_per_24h=max_per_24h, max_per_15d=max_per_15d, seller_cooldown=seller_cooldown)
    controls = await _locked_controls(conn, actor)
    if controls.version != expected_version:
        raise VersionConflict(
            "The inquiry controls changed; reload and retry", current_version=controls.version
        )
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiry_controls set max_per_24h = %(d)s, max_per_15d = %(f)s,"
            " seller_cooldown = %(cooldown)s, updated_by = %(by)s, update_reason = %(reason)s,"
            " version = version + 1 where workspace_id = %(ws)s",
            {
                "ws": actor.workspace_id,
                "d": policy.max_per_24h,
                "f": policy.max_per_15d,
                "cooldown": policy.seller_cooldown,
                "by": actor.principal_id,
                "reason": text,
            },
        )
    await audit.record(
        conn,
        actor,
        "seller_inquiry_controls.limits",
        "seller_inquiry_controls",
        controls.id,
        prior_version=controls.version,
        new_version=controls.version + 1,
        reason=text,
        metadata={"max_per_24h": policy.max_per_24h, "max_per_15d": policy.max_per_15d},
    )
    updated = await get_controls(conn, actor)
    assert updated is not None
    return updated


# =============================================================================================
# Quota windows and seller cooldown
# =============================================================================================


#: The latest moment an inquiry's message may have been (or may still be) handed over: its latest
#: send attempt, the end of every finished attempt, and NOW while an attempt is still running. On
#: the ``outlook_local`` route the hand-over happens when the desktop worker claims the intent,
#: up to the intent TTL after the attempt was committed; counting the debit only at
#: ``send_attempted_at`` (the database's ``ops.inquiry_quota_usage``) would let intents committed
#: while the worker was offline leave together with later ones, above 2 per rolling 24 hours.
_TRANSMISSION_BOUND_SQL: Final = (
    "greatest(i.send_attempted_at, (select max(case when a.outcome = 'running' then clock_timestamp()"
    " else a.finished_at end) from ops.email_delivery_attempts a"
    " where a.workspace_id = i.workspace_id and a.inquiry_id = i.id))"
)


async def _debits(conn: Conn, workspace_id: UUID) -> list[QuotaDebit]:
    """Unreleased quota debits, each counted at the later of its reservation and the latest
    moment its message may have been handed over (``_TRANSMISSION_BOUND_SQL``).

    Stricter than the database guard (which counts at ``send_attempted_at``): the repository
    decides under the same controls lock, the database stays the backstop.
    """
    rows = await fetch_all(
        conn,
        "select u.inquiry_id, u.debited_at, u.transmission_bound from ("  # noqa: S608 - fixed fragment
        f"  select q.inquiry_id, q.debited_at, {_TRANSMISSION_BOUND_SQL} as transmission_bound"
        "    from ops.inquiry_quota_ledger q"
        "    join app.seller_inquiries i on i.workspace_id = q.workspace_id and i.id = q.inquiry_id"
        "   where q.workspace_id = %(ws)s and q.released_at is null) u"
        " where greatest(u.debited_at, coalesce(u.transmission_bound, u.debited_at))"
        " > now() - interval '16 days'",
        {"ws": workspace_id},
    )
    return [
        QuotaDebit(inquiry_id=r["inquiry_id"], at=r["debited_at"], send_attempted_at=r["transmission_bound"])
        for r in rows
    ]


_RELATED_SQL: Final = """
with family as (
  select e.id from app.seller_entities e
   where e.workspace_id = %(ws)s and (e.id = %(seller)s or e.merged_into_id = %(seller)s)
), vehicle as (
  select %(listing)s::uuid as listing_id
  union
  select m2.listing_id
    from app.vehicle_cluster_members m1
    join app.vehicle_clusters c on c.workspace_id = m1.workspace_id and c.id = m1.cluster_id
     and c.review_status <> 'rejected'
    join app.vehicle_cluster_members m2 on m2.workspace_id = m1.workspace_id and m2.cluster_id = m1.cluster_id
     and m2.unlinked_at is null
   where m1.workspace_id = %(ws)s and m1.listing_id = %(listing)s and m1.unlinked_at is null
)
select i.id, i.identity_key, i.vehicle_kind,
       coalesce(i.vehicle_cluster_id, i.vehicle_listing_id) as vehicle_id,
       i.qualification_listing_id, coalesce(e.merged_into_id, e.id) as seller_root, i.state, i.reserved_at,
       i.send_attempted_at, i.seller_entity_id in (select id from family) as same_seller,
       (select count(*) from ops.email_delivery_attempts a
         where a.workspace_id = i.workspace_id and a.inquiry_id = i.id) as attempts,
       (i.qualification_listing_id in (select listing_id from vehicle)
        or i.vehicle_listing_id in (select listing_id from vehicle)
        or exists (select 1 from app.vehicle_cluster_members om
                    where om.workspace_id = i.workspace_id and om.cluster_id = i.vehicle_cluster_id
                      and om.unlinked_at is null and om.listing_id in (select listing_id from vehicle)))
         as same_vehicle
  from app.seller_inquiries i
  join app.seller_entities e on e.workspace_id = i.workspace_id and e.id = i.seller_entity_id
 where i.workspace_id = %(ws)s
   and (i.seller_entity_id in (select id from family)
        or i.qualification_listing_id in (select listing_id from vehicle)
        or i.vehicle_listing_id in (select listing_id from vehicle)
        or exists (select 1 from app.vehicle_cluster_members om
                    where om.workspace_id = i.workspace_id and om.cluster_id = i.vehicle_cluster_id
                      and om.unlinked_at is null and om.listing_id in (select listing_id from vehicle)))
 order by i.created_at, i.id
"""

_LINKS_SQL: Final = """
select m2.listing_id, bool_or(c.review_status = 'confirmed') as confirmed
  from app.vehicle_cluster_members m1
  join app.vehicle_clusters c on c.workspace_id = m1.workspace_id and c.id = m1.cluster_id
   and c.review_status <> 'rejected'
  join app.vehicle_cluster_members m2 on m2.workspace_id = m1.workspace_id and m2.cluster_id = m1.cluster_id
   and m2.unlinked_at is null and m2.listing_id <> m1.listing_id
 where m1.workspace_id = %(ws)s and m1.listing_id = %(listing)s and m1.unlinked_at is null
 group by m2.listing_id
"""


class _Neighbourhood(BaseModel):
    """Other inquiries of the seller family or of a (plausibly) same vehicle, as the domain sees them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    existing: tuple[ExistingInquiry, ...]
    related: tuple[RelatedListingLink, ...]


async def _neighbourhood(
    conn: Conn,
    workspace_id: UUID,
    *,
    seller_root_id: UUID,
    listing_id: UUID | None,
    exclude_identity: str | None,
    exclude_inquiry: UUID | None,
) -> _Neighbourhood:
    rows = await fetch_all(
        conn, _RELATED_SQL, {"ws": workspace_id, "seller": seller_root_id, "listing": listing_id}
    )
    links = (
        await fetch_all(conn, _LINKS_SQL, {"ws": workspace_id, "listing": listing_id})
        if listing_id is not None
        else []
    )
    confirmed = {r["listing_id"]: bool(r["confirmed"]) for r in links}
    existing: list[ExistingInquiry] = []
    related: list[RelatedListingLink] = []
    for row in rows:
        if exclude_inquiry is not None and row["id"] == exclude_inquiry:
            continue
        sent = row["send_attempted_at"]
        vehicle = VehicleIdentityRef(kind=row["vehicle_kind"], id=row["vehicle_id"])
        item = ExistingInquiry(
            inquiry_id=row["id"],
            identity_key=row["identity_key"],
            vehicle=vehicle,
            seller_key=f"seller_entity:{row['seller_root']}",
            state=InquiryState(row["state"]),
            transmission_attempts=int(row["attempts"]),
            reserved_at=row["reserved_at"],
            last_contact_at=sent or row["reserved_at"],
        )
        existing.append(item)
        qualifying = row["qualification_listing_id"]
        if exclude_identity is not None and row["identity_key"] == exclude_identity:
            continue
        # The same listing under another identity (e.g. qualified before its cluster was
        # confirmed) is certainly the same vehicle; other cluster members are as linked.
        same_listing = listing_id is not None and qualifying == listing_id
        if same_listing or qualifying in confirmed:
            related.append(
                RelatedListingLink(
                    related_listing_id=qualifying,
                    relation="confirmed_same_vehicle"
                    if same_listing or confirmed[qualifying]
                    else "possible_same_unresolved",
                    related_inquiry_state=item.state,
                    related_vehicle=vehicle,
                    related_inquiry_id=row["id"],
                    related_reserved_at=row["reserved_at"],
                )
            )
    return _Neighbourhood(existing=tuple(existing), related=tuple(related))


async def next_window_at(
    conn: Conn,
    actor: ActorContext,
    *,
    seller_entity_id: UUID | None = None,
    exclude_inquiry_id: UUID | None = None,
) -> WindowDecision:
    """When a rolling cap and the seller cooldown free again (for delayed re-queueing).

    ``blocked`` with ``next_at`` = the earliest moment both allow one more inquiry; ``next_at`` is
    ``None`` when nothing blocks, or when no end is known (a cap set to 0, a paused workspace):
    hand-offs then wait for an owner action instead of retrying into a dead letter.
    """
    require_inquiry_reader(actor)
    ws = actor.workspace_id
    controls = await get_controls(conn, actor)
    async with mapped_errors():
        now = await _now(conn)
        debits = await _debits(conn, ws)
    policy = controls.policy() if controls is not None else RateCapPolicy(max_per_24h=0, max_per_15d=0)
    caps = evaluate_rate_caps(debits, now=now, policy=policy, exclude_inquiry_id=exclude_inquiry_id)
    cooldown: CooldownDecision | None = None
    if seller_entity_id is not None:
        async with mapped_errors():
            root_row = await fetch_one(
                conn,
                "select coalesce(merged_into_id, id) as root from app.seller_entities"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": seller_entity_id},
            )
            if root_row is None:
                raise NotFound("Seller not found")
            hood = await _neighbourhood(
                conn,
                ws,
                seller_root_id=root_row["root"],
                listing_id=None,
                exclude_identity=None,
                exclude_inquiry=exclude_inquiry_id,
            )
        key = f"seller_entity:{root_row['root']}"
        cooldown = evaluate_seller_cooldown(
            seller_contact_times(hood.existing, key), now=now, cooldown=policy.seller_cooldown
        )
    blocked = not caps.allowed or (cooldown is not None and cooldown.active)
    candidates: list[datetime] = []
    unknown = False
    if not caps.allowed:
        if caps.next_allowed_at is None:
            unknown = True
        else:
            candidates.append(caps.next_allowed_at)
    if cooldown is not None and cooldown.active and cooldown.until is not None:
        candidates.append(cooldown.until)
    if controls is None or controls.paused:
        blocked, unknown = True, True
    next_at = None if (unknown or not candidates) else max(candidates)
    return WindowDecision(now=now, rate_caps=caps, cooldown=cooldown, blocked=blocked, next_at=next_at)


# =============================================================================================
# Suppressions
# =============================================================================================


def seller_suppression_key(entity_id: UUID) -> str:
    return f"seller_entity:{entity_id}"


async def _normalize_scope_key(conn: Conn, actor: ActorContext, scope: SuppressionScope, key: str) -> str:
    ws = actor.workspace_id
    if scope == "workspace":
        if key not in ("*", str(ws)):
            raise ValidationFailed("a workspace suppression key is '*' or the workspace id")
        return key
    if scope == "seller":
        match = _SELLER_KEY_RE.fullmatch(key)
        if match is None:
            raise ValidationFailed(
                "seller suppressions name a persisted seller entity (seller_entity:<id>); link aliases first"
            )
        async with mapped_errors():
            row = await fetch_one(
                conn,
                "select coalesce(merged_into_id, id) as root from app.seller_entities"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": UUID(match.group("id"))},
            )
        if row is None:
            raise NotFound("Seller not found")
        return seller_suppression_key(row["root"])
    if scope == "address":
        try:
            return canonicalize_address(key).canonical
        except AddressError as exc:
            raise ValidationFailed("the suppressed address is not a canonical e-mail address") from exc
    if scope == "vehicle":
        if not _VEHICLE_KEY_RE.fullmatch(key):
            raise ValidationFailed(
                "vehicle suppressions name vehicle_cluster:<id> or listing_incarnation:<id>"
            )
        return key
    if scope == "source":
        if not _SOURCE_KEY_RE.fullmatch(key):
            raise ValidationFailed("source suppressions name a source key")
        return key
    try:
        return str(UUID(key))
    except ValueError as exc:
        raise ValidationFailed("sender suppressions name a sender binding id") from exc


async def add_suppression(
    conn: Conn,
    actor: ActorContext,
    *,
    scope: SuppressionScope,
    key: str,
    reason: SuppressionReason,
    evidence: Mapping[str, Any] | None = None,
    inquiry_id: UUID | None = None,
    reply_id: UUID | None = None,
    suppress_pending: bool = True,
) -> SuppressionAdded:
    """Record a suppression (idempotent per active scope/key/reason).

    With ``suppress_pending`` every matching untransmitted inquiry moves to ``suppressed`` (its
    quota debit released) in the same transaction; the dispatch guard re-checks suppressions
    anyway. Seller suppressions match persisted entity keys only (aliases must be linked first).
    """
    if actor.principal_kind != "system":
        actor.require(Scope.CONFIG_ADMIN)
    ws = actor.workspace_id
    await lock_controls(conn, ws)
    scope_key = await _normalize_scope_key(conn, actor, scope, key)
    params = {
        "ws": ws,
        "scope": scope,
        "key": scope_key,
        "reason": SuppressionReason(reason).value,
        "evidence": Jsonb(dict(evidence or {})),
        "inquiry": inquiry_id,
        "reply": reply_id,
        "by": actor.principal_id if actor.principal_kind != "system" else None,
        "kind": actor.principal_kind,
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.email_suppressions (workspace_id, scope, scope_key, reason, evidence,"  # noqa: S608 - fixed column list
            " inquiry_id,"
            " reply_id, created_by_principal_id, created_by_kind)"
            " values (%(ws)s, %(scope)s, %(key)s, %(reason)s, %(evidence)s, %(inquiry)s, %(reply)s, %(by)s,"
            " %(kind)s)"
            " on conflict (workspace_id, scope, match_key, reason) where removed_at is null do nothing"
            f" returning {_SUPPRESSION_COLUMNS}",
            params,
        )
        created = row is not None
        if row is None:
            row = await fetch_one(
                conn,
                _SELECT_SUPPRESSION
                + " where workspace_id = %(ws)s and scope = %(scope)s and reason = %(reason)s"
                " and match_key = case when %(scope)s = 'address' then lower(%(key)s) else %(key)s end"
                " and removed_at is null",
                params,
            )
    assert row is not None
    suppression = SuppressionRow.model_validate(dict(row))
    if created:
        await audit.record(
            conn,
            actor,
            "email_suppression.add",
            "email_suppression",
            suppression.id,
            reason=f"{scope} suppression: {suppression.reason.value}",
            metadata={"scope": scope, "reason": suppression.reason.value},
        )
    suppressed: tuple[UUID, ...] = ()
    if suppress_pending:
        hit = f"{scope}:{suppression.reason.value}"
        async with mapped_errors():
            pending = await fetch_all(
                conn,
                "select i.id from app.seller_inquiries i"
                " join app.listings l on l.workspace_id = i.workspace_id and l.id ="
                " i.qualification_listing_id"
                " join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id"
                " where i.workspace_id = %(ws)s and i.state = any(%(states)s)"
                " and %(hit)s = any(ops.seller_inquiry_active_suppressions(i.workspace_id,"
                " i.seller_entity_id,"
                "   coalesce(i.recipient_address, (select c.address from app.seller_contacts c"
                "      where c.workspace_id = i.workspace_id and c.listing_id = i.qualification_listing_id"
                "        and c.status = 'verified')),"
                "   i.vehicle_key, i.qualification_listing_id, i.vehicle_cluster_id, s.source_key,"
                "   i.sender_binding_id))"
                " order by i.id",
                {"ws": ws, "states": list(_PENDING_STATES), "hit": hit},
            )
        suppressed = await cancel_untransmitted(
            conn,
            actor,
            [r["id"] for r in pending],
            reasons=[f"SUPPRESSED_{suppression.reason.value.upper()}"],
            target="suppressed",
            suppression_reason=suppression.reason.value,
        )
    return SuppressionAdded(suppression=suppression, created=created, suppressed_inquiry_ids=suppressed)


async def remove_suppression(
    conn: Conn, actor: ActorContext, suppression_id: UUID, *, reason: str
) -> SuppressionRow:
    """Explicit, audited removal by an owner (never automatic; never a system principal)."""
    if actor.principal_kind == "system":
        raise Forbidden("Suppressions are never removed automatically")
    actor.require(Scope.CONFIG_ADMIN)
    text = _reason_text(reason)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT_SUPPRESSION + " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": actor.workspace_id, "id": suppression_id},
        )
    if row is None:
        raise NotFound("Suppression not found")
    current = SuppressionRow.model_validate(dict(row))
    if current.removed_at is not None:
        return current
    audit_id = await audit.record(
        conn,
        actor,
        "email_suppression.remove",
        "email_suppression",
        suppression_id,
        reason=text,
        metadata={"scope": current.scope, "reason": current.reason.value},
    )
    async with mapped_errors():
        updated = await fetch_one(
            conn,
            "update ops.email_suppressions set removed_at = greatest(now(), effective_at),"  # noqa: S608 - fixed column list
            " removed_by_principal_id = %(by)s, removed_by_kind = %(kind)s, removal_reason = %(reason)s,"
            " removal_audit_id = %(audit)s where workspace_id = %(ws)s and id = %(id)s"
            f" returning {_SUPPRESSION_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "id": suppression_id,
                "by": actor.principal_id,
                "kind": actor.principal_kind,
                "reason": text,
                "audit": audit_id,
            },
        )
    assert updated is not None
    return SuppressionRow.model_validate(dict(updated))


async def active_suppressions(
    conn: Conn, actor: ActorContext, *, scope: SuppressionScope | None = None, limit: int = 500
) -> list[SuppressionRow]:
    require_inquiry_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SELECT_SUPPRESSION + " where workspace_id = %(ws)s and removed_at is null"
            " and (%(scope)s::text is null or scope = %(scope)s) order by effective_at desc, id limit"
            " %(limit)s",
            {"ws": actor.workspace_id, "scope": scope, "limit": max(1, min(limit, 5000))},
        )
    return [SuppressionRow.model_validate(dict(r)) for r in rows]


_MATCHED_SUPPRESSIONS_SQL: Final = f"""
select {_SUPPRESSION_COLUMNS} from ops.email_suppressions s
 where s.workspace_id = %(ws)s and s.removed_at is null
   and (
     (s.scope = 'workspace' and s.match_key in ('*', %(ws)s::text))
     or (s.scope = 'seller' and s.match_key in (
           select 'seller_entity:' || e.id::text from app.seller_entities e
            where e.workspace_id = %(ws)s and (e.id = %(seller)s or e.merged_into_id = %(seller)s)))
     or (s.scope = 'address' and s.match_key = lower(%(address)s::text))
     or (s.scope = 'vehicle' and (
           s.match_key = %(vehicle_key)s
           or s.match_key = 'listing_incarnation:' || %(listing)s::text
           or s.match_key in (select 'listing_incarnation:' || m.listing_id::text
                                from app.vehicle_cluster_members m
                               where m.workspace_id = %(ws)s and m.cluster_id = %(cluster)s
                                 and m.unlinked_at is null)
           or s.match_key in (select 'vehicle_cluster:' || m.cluster_id::text
                                from app.vehicle_cluster_members m
                               where m.workspace_id = %(ws)s and m.listing_id = %(listing)s
                                 and m.unlinked_at is null)))
     or (s.scope = 'source' and s.match_key = %(source_key)s)
     or (s.scope = 'sender' and s.match_key = %(sender)s::text)
   )
 order by s.effective_at, s.id
"""  # noqa: S608 - fixed column list


async def _matched_suppressions(
    conn: Conn,
    workspace_id: UUID,
    *,
    seller_root_id: UUID,
    address: str | None,
    vehicle_key: str,
    listing_id: UUID,
    cluster_id: UUID | None,
    source_key: str | None,
    sender_binding_id: UUID | None,
) -> list[SuppressionRow]:
    """The database's matching rule (``ops.seller_inquiry_active_suppressions``), as rows."""
    rows = await fetch_all(
        conn,
        _MATCHED_SUPPRESSIONS_SQL,
        {
            "ws": workspace_id,
            "seller": seller_root_id,
            "address": address,
            "vehicle_key": vehicle_key,
            "listing": listing_id,
            "cluster": cluster_id,
            "source_key": source_key,
            "sender": str(sender_binding_id) if sender_binding_id else None,
        },
    )
    return [SuppressionRow.model_validate(dict(r)) for r in rows]


def _as_target_records(
    rows: Sequence[SuppressionRow],
    *,
    workspace_id: UUID,
    seller_key: str,
    address: str | None,
    vehicle_key: str,
    source_key: str | None,
    sender_binding_id: UUID | None,
) -> tuple[SuppressionRecord, ...]:
    """Re-key database-matched suppressions onto the domain targets (merged sellers, cluster
    members and relistings match through the database rule, then the domain sees them)."""
    keys: dict[str, str | None] = {
        "workspace": str(workspace_id),
        "seller": seller_key,
        "address": address,
        "vehicle": vehicle_key,
        "source": source_key,
        "sender": str(sender_binding_id) if sender_binding_id else None,
    }
    records: list[SuppressionRecord] = []
    for row in rows:
        key = keys.get(row.scope)
        if key is None:
            continue
        records.append(row.record().model_copy(update={"key": key}))
    return tuple(records)


# =============================================================================================
# Qualification: readiness inputs, open/continue, readiness, binding and reservation
# =============================================================================================

_LISTING_SQL: Final = """
select l.id, l.source_id, l.availability, l.current_revision_id, l.quarantined, l.identity_conflict,
       l.eligibility_state, l.eligibility_profile, l.screening, l.last_detail_success_at,
       r.revision_number, r.semantic_hash, r.asking_minor, r.currency, r.normalized,
       r.quarantined as rev_quarantined,
       s.source_key, s.enabled, s.paused, s.terms_decision, s.technical_status,
       array(select c.id from app.vehicle_cluster_members m
          join app.vehicle_clusters c on c.workspace_id = m.workspace_id and c.id = m.cluster_id
           and c.review_status = 'confirmed'
         where m.workspace_id = l.workspace_id and m.listing_id = l.id and m.unlinked_at is null
         order by c.created_at, c.id) as confirmed_cluster_ids,
       (select e.conflicts_with_current from app.availability_events e
         where e.workspace_id = l.workspace_id and e.listing_id = l.id
         order by e.observed_at desc, e.created_at desc limit 1) as availability_conflict
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join app.listing_revisions r on r.workspace_id = l.workspace_id and r.listing_id = l.id
   and r.id = l.current_revision_id
 where l.workspace_id = %(ws)s and l.id = %(listing)s
"""


async def _listing_row(conn: Conn, workspace_id: UUID, listing_id: UUID) -> dict[str, Any]:
    row = await fetch_one(conn, _LISTING_SQL, {"ws": workspace_id, "listing": listing_id})
    if row is None:
        raise NotFound("Listing not found")
    data = dict(row)
    clusters = list(data.get("confirmed_cluster_ids") or ())
    data["confirmed_cluster_id"] = clusters[0] if clusters else None
    return data


def _listing_facts(row: Mapping[str, Any]) -> ListingFactsSnapshot:
    if row["current_revision_id"] is None or row["revision_number"] is None:
        raise InsufficientData("The listing has no current revision to qualify on")
    return ListingFactsSnapshot(
        listing_id=row["id"],
        listing_incarnation_id=row["id"],
        revision_id=row["current_revision_id"],
        revision_number=int(row["revision_number"]),
        semantic_hash=row["semantic_hash"],
        price_amount_minor=row["asking_minor"],
        price_currency=row["currency"],
        availability=Availability(row["availability"]),
    )


def _source_facts(row: Mapping[str, Any]) -> SourceObservationFacts:
    return SourceObservationFacts(
        source_key=row["source_key"],
        source_enabled=bool(row["enabled"]),
        source_paused=bool(row["paused"]),
        terms_blocked=row["terms_decision"] == "do_not_use",
        access_blocked=row["technical_status"] == "access_blocked",
        last_detail_success_at=row["last_detail_success_at"],
        availability=Availability(row["availability"]),
    )


async def _comparables(conn: Conn, workspace_id: UUID, revision_id: UUID) -> ComparableEvidence | None:
    row = await fetch_one(
        conn,
        "select cs.id, cs.sample_quality, cs.rationale, cs.criteria_version,"
        " count(*) filter (where m.disposition = 'selected' and o.evidence_kind = 'asking_price') as asking,"
        " count(*) filter (where m.disposition = 'selected' and o.evidence_kind = 'verified_sale') as sales"
        " from app.comparable_sets cs"
        " left join app.comparable_set_members m on m.workspace_id = cs.workspace_id"
        "  and m.comparable_set_id = cs.id"
        " left join app.market_observations o on o.workspace_id = m.workspace_id"
        "  and o.id = m.market_observation_id"
        " where cs.workspace_id = %(ws)s and cs.target_revision_id = %(revision)s"
        " group by cs.id order by cs.computed_at desc, cs.id desc limit 1",
        {"ws": workspace_id, "revision": revision_id},
    )
    if row is None:
        return None
    status = {"adequate": "adequate", "small": "small_sample"}.get(
        row["sample_quality"], "insufficient_comparables"
    )
    return ComparableEvidence.model_validate(
        {
            "status": status,
            "asking_count": int(row["asking"]),
            "verified_sale_count": int(row["sales"]),
            "matching_rationale": str(row["rationale"])[:1000] if row["rationale"] else None,
            "comparable_set_id": row["id"],
            "criteria_version": row["criteria_version"],
        }
    )


async def _costs(conn: Conn, actor: ActorContext, listing_id: UUID) -> CostEvidence | None:
    try:
        stored = await valuation_repo.current_valuation(conn, actor, listing_id)
    except AppError:
        return None
    if stored is None or stored.valuation.scenarios is None:
        return None
    approved = stored.valuation.state in (ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED)
    return CostEvidence.from_scenario_set(stored.valuation.scenarios, tax_rule_approved=approved)


async def read_readiness_inputs(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID,
    seller_entity_id: UUID,
    evaluation: ReadinessEvaluation | None = None,
    sender_binding_id: UUID | None = None,
) -> ReadinessSnapshot:
    """One read of every record ``evaluate_inquiry_readiness`` needs (spec 37.2).

    Listing facts (current revision), source facts, the latest authorization, the canonical
    identity (confirmed cluster, else the listing incarnation; surviving seller entity), duplicate
    and cross-site links, recipient and language decisions rebuilt from the stored contact
    evidence, the sender status (binding + controls), rolling caps and seller cooldown. Screening,
    comparables, costs, documentation and CO2 come from ``evaluation`` when given, else from the
    stored screening, revision, comparable set and current valuation. The reservation re-checks
    all of it under the locks, so a reservation never binds facts that changed since this read.
    """
    require_inquiry_reader(actor)
    ws = actor.workspace_id
    ev = evaluation or ReadinessEvaluation()
    async with mapped_errors():
        now = await _now(conn)
        listing = await _listing_row(conn, ws, listing_id)
        root_row = await fetch_one(
            conn,
            "select coalesce(merged_into_id, id) as root from app.seller_entities"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": seller_entity_id},
        )
    if root_row is None:
        raise NotFound("Seller not found")
    root: UUID = root_row["root"]
    facts = _listing_facts(listing)
    authorization = await current_authorization(conn, actor)
    if authorization is None:
        raise InsufficientData("No standing seller inquiry authorization is recorded for this workspace")
    identity = InquiryIdentity(
        workspace_id=ws,
        vehicle=canonical_vehicle_identity(
            vehicle_cluster_id=listing["confirmed_cluster_id"], listing_incarnation_id=listing_id
        ),
        seller_key=f"seller_entity:{root}",
    )
    normalized: NormalizedListing | None = None
    if listing["normalized"] is not None:
        try:
            normalized = NormalizedListing.model_validate(listing["normalized"])
        except ValidationError:
            normalized = None
    screening = ev.screening
    if screening is None and listing["screening"] is not None:
        try:
            screening = ScreeningResult.model_validate(listing["screening"])
        except ValidationError:
            screening = None
    if screening is None:
        raise InsufficientData("The listing has not been screened yet")
    vehicle = ev.vehicle
    if vehicle is None and normalized is not None:
        vehicle = VehicleIdentification.from_screening(screening, normalized)
    if vehicle is None:
        vehicle = VehicleIdentification(
            make=None, model=None, generation=None, is_suv=None, confidence=None, matched_via="none"
        )
    async with mapped_errors():
        comparables = ev.comparables
        if comparables is None:
            comparables = await _comparables(conn, ws, facts.revision_id) if facts.revision_id else None
    costs = ev.costs if ev.costs is not None else await _costs(conn, actor, listing_id)
    contact = await current_contact(conn, actor, listing_id)
    recipient: RecipientDecision | None = recipient_decision(contact)
    language: LanguageDecision | None = language_decision(contact)
    controls = await get_controls(conn, actor)
    binding: SenderBindingRecord | None
    if sender_binding_id is not None:
        binding = await get_binding(conn, actor, sender_binding_id)
    else:
        binding = await active_binding(conn, actor)
    sender = sender_status(
        binding,
        mode=controls.mode if controls is not None else "disabled_until_sender_ready",
        kill_switch=controls.kill_switch if controls is not None else False,
    )
    async with mapped_errors():
        hood = await _neighbourhood(
            conn,
            ws,
            seller_root_id=root,
            listing_id=listing_id,
            exclude_identity=identity.key(),
            exclude_inquiry=None,
        )
        debits = await _debits(conn, ws)
        matched = await _matched_suppressions(
            conn,
            ws,
            seller_root_id=root,
            address=contact.address if contact is not None else None,
            vehicle_key=identity.vehicle.key(),
            listing_id=listing_id,
            cluster_id=listing["confirmed_cluster_id"],
            source_key=listing["source_key"],
            sender_binding_id=binding.id if binding is not None else None,
        )
    policy = controls.policy() if controls is not None else RateCapPolicy()
    records = _as_target_records(
        matched,
        workspace_id=ws,
        seller_key=identity.seller_key,
        address=contact.address if contact is not None else None,
        vehicle_key=identity.vehicle.key(),
        source_key=listing["source_key"],
        sender_binding_id=binding.id if binding is not None else None,
    )
    duplicate: DuplicateDecision = evaluate_duplicate_contact(identity, hood.existing, hood.related)
    documentation = ev.documentation
    if documentation is None and normalized is not None:
        documentation = normalized.documentation
    co2 = ev.co2
    if co2 is None and normalized is not None:
        co2 = normalized.co2
    inputs = InquiryReadinessInputs(
        as_of=now,
        listing_id=listing_id,
        listing_facts=facts,
        authorization=authorization.authorization,
        identity=identity,
        screening=screening,
        vehicle=vehicle,
        source=_source_facts(listing),
        comparables=comparables,
        costs=costs,
        documentation=documentation,
        co2=co2,
        disqualifiers=DisqualifierFacts(
            fraud_warnings=ev.fraud_warnings,
            identity_conflict_open=bool(listing["identity_conflict"]) or bool(listing["quarantined"]),
            availability_conflict=bool(listing["availability_conflict"]),
            seller_opted_out=any(r.reason == SuppressionReason.SELLER_OPT_OUT for r in records),
            active_suppressions=records,
        ),
        duplicate=duplicate,
        recipient=recipient,
        language=language,
        sender=sender,
        rate_caps=evaluate_rate_caps(debits, now=now, policy=policy),
        seller_cooldown=evaluate_seller_cooldown(
            seller_contact_times(hood.existing, identity.seller_key), now=now, cooldown=policy.seller_cooldown
        ),
    )
    return ReadinessSnapshot(
        inputs=inputs,
        identity=identity,
        contact=contact,
        sender_binding=binding,
        authorization_id=authorization.id,
        vehicle_label=(vehicle.make, vehicle.model, vehicle.generation),
    )


async def open_inquiry(
    conn: Conn, actor: ActorContext, identity: InquiryIdentity, *, qualification_listing_id: UUID
) -> InquiryRecord:
    """Create the ONE record of ``identity`` (``candidate``) or return the existing one.

    A price change, relisting, profile switch, sender change or retry never creates a second
    record for an identity (``seller_inquiries_identity_uk``). Another live identity of the same
    (listing, seller) - e.g. the listing identity before a cluster was confirmed - must be
    reconciled first: ``VersionConflict`` with reason ``inquiry_identity_superseded``.
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    if identity.workspace_id != ws:
        raise Forbidden("The inquiry identity belongs to another workspace")
    match = _SELLER_KEY_RE.fullmatch(identity.seller_key)
    if match is None:
        raise ValidationFailed("an inquiry needs a persisted seller entity (seller_entity:<id>)")
    seller = UUID(match.group("id"))
    vehicle = identity.vehicle
    if vehicle.kind == "listing_incarnation" and vehicle.id != qualification_listing_id:
        raise ValidationFailed("a listing-incarnation identity names exactly the qualifying listing")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.seller_inquiries (workspace_id, identity_key, vehicle_kind, vehicle_cluster_id,"
            " vehicle_listing_id, seller_entity_id, qualification_listing_id, state)"
            " values (%(ws)s, %(key)s, %(kind)s, %(cluster)s, %(listing)s, %(seller)s, %(qualifying)s,"
            " 'candidate') on conflict do nothing returning id",
            {
                "ws": ws,
                "key": identity.key(),
                "kind": vehicle.kind,
                "cluster": vehicle.id if vehicle.kind == "vehicle_cluster" else None,
                "listing": vehicle.id if vehicle.kind == "listing_incarnation" else None,
                "seller": seller,
                "qualifying": qualification_listing_id,
            },
        )
        existing = await fetch_one(
            conn,
            _SELECT_INQUIRY + " where workspace_id = %(ws)s and identity_key = %(key)s",
            {"ws": ws, "key": identity.key()},
        )
    if existing is None:
        raise VersionConflict(
            "Another inquiry identity of this listing and seller is still live; reconcile it first",
            reason="inquiry_identity_superseded",
        )
    record = _inquiry(existing)
    if row is not None:
        await audit.record(
            conn,
            actor,
            "seller_inquiry.open",
            "seller_inquiry",
            record.id,
            new_version=record.row_version,
            reason="inquiry identity recorded",
            metadata={"vehicle_kind": vehicle.kind},
        )
    return record


async def record_readiness(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, decision: InquiryReadinessDecision
) -> InquiryRecord:
    """Store the readiness decision on a not-yet-reserved record and move it accordingly.

    ``inquiry_ready`` -> ``qualifying`` (a never-transmitted cancelled record is re-qualified),
    ``needs_facts``/``needs_technical_review`` -> ``held_facts``, ``not_eligible`` -> ``cancelled``.
    No state waits for an approval: ``qualifying`` is reserved automatically once the guards pass.
    """
    require_inquiry_writer(actor)
    record = await get_inquiry(conn, actor, inquiry_id)
    if decision.evidence.get("identity_key") != record.identity_key:
        raise ValidationFailed("the readiness decision was evaluated for another inquiry identity")
    if record.state not in PRE_RESERVATION_STATES and record.state != InquiryState.CANCELLED:
        raise VersionConflict(
            "The inquiry is already reserved or beyond; its binding is immutable",
            current_state=record.state.value,
        )
    if decision.readiness == InquiryReadiness.INQUIRY_READY:
        target = InquiryState.QUALIFYING
    elif decision.readiness == InquiryReadiness.NOT_ELIGIBLE:
        target = InquiryState.CANCELLED
    else:
        target = InquiryState.HELD_FACTS
    state = record.state
    if state == InquiryState.CANCELLED and target == InquiryState.CANCELLED:
        # Still not eligible: the cancellation stands, and a cancelled record's readiness columns
        # are frozen (database binding guard), so a periodic re-evaluation is a no-op.
        return record
    codes = _codes((r.code.value for r in decision.reasons), 60)
    async with mapped_errors():
        if state == InquiryState.CANCELLED and target != InquiryState.CANCELLED:
            require_transition(state, InquiryState.QUALIFYING, TransitionContext(transmission_attempts=0))
            await conn.execute(
                "update app.seller_inquiries set state = 'qualifying', row_version = row_version + 1"
                " where workspace_id = %(ws)s and id = %(id)s and state = 'cancelled'",
                {"ws": actor.workspace_id, "id": inquiry_id},
            )
            state = InquiryState.QUALIFYING
        await conn.execute(
            "update app.seller_inquiries set readiness = %(readiness)s, readiness_reasons = %(reasons)s,"
            " readiness_rationale_hash = %(hash)s, readiness_rules_version = %(version)s,"
            " readiness_evaluated_at = %(at)s, row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s",
            {
                "ws": actor.workspace_id,
                "id": inquiry_id,
                "readiness": decision.readiness.value,
                "reasons": codes,
                "hash": decision.rationale_hash,
                "version": decision.rationale_version,
                "at": decision.as_of,
            },
        )
    if state != target:
        if target == InquiryState.CANCELLED:
            await cancel_untransmitted(conn, actor, [inquiry_id], reasons=["NOT_ELIGIBLE", *codes[:20]])
        else:
            require_transition(state, target)
            async with mapped_errors():
                await conn.execute(
                    "update app.seller_inquiries set state = %(state)s, state_reasons = %(reasons)s,"
                    " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                    {
                        "ws": actor.workspace_id,
                        "id": inquiry_id,
                        "state": target.value,
                        "reasons": codes[:30],
                    },
                )
    return await get_inquiry(conn, actor, inquiry_id)


class PreparedBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding: InquiryBinding
    message: RenderedMessage
    preview: RenderedMessage


def prepare_binding(
    snapshot: ReadinessSnapshot, decision: InquiryReadinessDecision, *, inquiry_id: UUID
) -> PreparedBinding:
    """Render the registered template in the resolved language and bind it (``bind_inquiry``).

    Uses exactly the decisions of ``snapshot`` (recipient/language/sender rebuilt from the stored
    evidence), so ``reserve`` can verify every cross-reference against the records.
    """
    inputs = snapshot.inputs
    language = inputs.language
    recipient = inputs.recipient
    if language is None or language.language is None or recipient is None or recipient.binding is None:
        raise ValidationFailed(
            "inquiry binding refused", details={"problems": ["LANGUAGE_OR_RECIPIENT_NOT_VERIFIED"]}
        )
    if inputs.sender.display_name is None:
        raise ValidationFailed("inquiry binding refused", details={"problems": ["SENDER_NOT_USABLE"]})
    make, model, generation = snapshot.vehicle_label
    try:
        label = build_vehicle_label(make, model, generation)
        message = render(
            template_for_language(language.language).template_id,
            label,
            recipient.binding.listing_reference,
            recipient.binding.listing_url,
            inputs.sender.display_name,
            verified_listing_url=recipient.binding.listing_url,
        )
        preview = render_preview_mk(message)
    except TemplateRenderError as exc:
        raise ValidationFailed("inquiry binding refused", details=exc.details) from exc
    binding = bind_inquiry(
        at=decision.as_of,
        inquiry_id=inquiry_id,
        identity=snapshot.identity,
        authorization=inputs.authorization,
        readiness=decision,
        language=language,
        message=message,
        sender=inputs.sender,
        recipient=recipient,
        listing=inputs.listing_facts,
    )
    return PreparedBinding(binding=binding, message=message, preview=preview)


def _refuse(problem: str, message: str = "The reservation is refused") -> ValidationFailed:
    return ValidationFailed(message, details={"problems": [problem]})


async def _hold_errors(
    conn: Conn,
    actor: ActorContext,
    record: InquiryRecord,
    controls: InquiryControls,
    *,
    identity: InquiryIdentity,
    now: datetime,
) -> None:
    """Domain re-check under the locks: one-inquiry rule, rolling caps, seller cooldown."""
    ws = actor.workspace_id
    if controls.kill_switch:
        raise VersionConflict("The seller inquiry kill switch is active", reason="inquiry_kill_switch")
    if controls.mode != "automatic":
        raise VersionConflict(
            "Automatic seller inquiries are not enabled",
            reason="inquiry_mode_not_automatic",
            mode=controls.mode,
        )
    async with mapped_errors():
        hood = await _neighbourhood(
            conn,
            ws,
            seller_root_id=record.seller_entity_id,
            listing_id=record.qualification_listing_id,
            exclude_identity=record.identity_key,
            exclude_inquiry=None,
        )
        debits = await _debits(conn, ws)
    duplicate = evaluate_duplicate_contact(identity, hood.existing, hood.related)
    if duplicate.blocks:
        raise VersionConflict(
            "This seller already has an inquiry about this vehicle (or a plausibly same one)",
            reason="inquiry_vehicle_seller_conflict",
            outcome=duplicate.outcome,
            reasons=list(duplicate.reasons),
        )
    caps = evaluate_rate_caps(debits, now=now, policy=controls.policy())
    if not caps.allowed:
        retry = (
            None
            if caps.next_allowed_at is None
            else max(1, int((caps.next_allowed_at - now).total_seconds()))
        )
        raise RateLimited(
            "The seller inquiry rate cap is reached; the inquiry waits for the rolling window",
            retry,
            details={
                "reason": "inquiry_cap_reached",
                "phase": "reserve",
                "next_allowed_at": caps.next_allowed_at.isoformat() if caps.next_allowed_at else None,
            },
        )
    cooldown = evaluate_seller_cooldown(
        seller_contact_times(hood.existing, record.seller_key), now=now, cooldown=controls.seller_cooldown
    )
    if cooldown.active:
        retry = None if cooldown.until is None else max(1, int((cooldown.until - now).total_seconds()))
        raise RateLimited(
            "The seller was contacted recently; the inquiry waits for the seller cooldown",
            retry,
            details={
                "reason": "seller_cooldown",
                "phase": "reserve",
                "until": cooldown.until.isoformat() if cooldown.until else None,
            },
        )


async def reserve(
    conn: Conn,
    actor: ActorContext,
    inquiry_id: UUID,
    *,
    decision: InquiryReadinessDecision,
    prepared: PreparedBinding,
) -> InquiryRecord:
    """Bind and reserve in one transaction: quota debit + ``qualifying -> reserved``.

    Lock order: controls -> seller entity -> inquiry -> ledger. The readiness decision must be
    ``inquiry_ready`` without hold, at most ``MAX_READINESS_AGE`` old (database time) and for
    exactly this identity and listing facts; the binding must name the listing's current verified
    contact, the exact sender binding version and the current authorization. The domain re-checks
    the one-inquiry rule, caps and cooldown under the locks; the database repeats everything
    (``seller_inquiry_preflight('reserve')``) and refuses with typed errors.
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    binding, message, preview = prepared.binding, prepared.message, prepared.preview
    controls = await _locked_controls(conn, actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    async with mapped_errors():
        now = await _now(conn)
    if record.state == InquiryState.RESERVED or record.state not in PRE_RESERVATION_STATES:
        raise VersionConflict(
            "The inquiry is already reserved or beyond",
            reason="inquiry_in_progress",
            current_state=record.state.value,
        )
    if decision.readiness != InquiryReadiness.INQUIRY_READY or not decision.can_reserve_now:
        raise _refuse("NOT_INQUIRY_READY", "The readiness decision does not allow reserving now")
    if decision.as_of > now + timedelta(seconds=5) or now - decision.as_of > MAX_READINESS_AGE:
        raise _refuse("READINESS_STALE", "The readiness decision is stale; evaluate it again")
    if (
        binding.inquiry_id != record.id
        or binding.identity_key != record.identity_key
        or binding.vehicle_key != record.vehicle_key
        or decision.evidence.get("identity_key") != record.identity_key
        or binding.readiness_rationale_hash != decision.rationale_hash
    ):
        raise _refuse("BINDING_FOR_OTHER_INQUIRY")
    if binding.qualified_listing.listing_id != record.qualification_listing_id:
        raise _refuse("BINDING_FOR_OTHER_LISTING")
    if (
        message.body_hash != binding.body_hash
        or message.template_id != binding.template_id
        or message.template_hash != binding.template_hash
        or message.language != binding.language.value
        or rendering_problems(message)
        or preview.kind != "owner_preview"
        or preview.source_body_hash != message.body_hash
        or rendering_problems(preview)
    ):
        raise _refuse("MESSAGE_NOT_BOUND_RENDERING")
    contact = await current_contact(conn, actor, record.qualification_listing_id)
    if (
        contact is None
        or contact.status != "verified"
        or contact.seller_entity_id != record.seller_entity_id
        or recipient_binding(contact).fingerprint() != binding.recipient.fingerprint()
    ):
        raise _refuse("RECIPIENT_NOT_CURRENT_VERIFIED_CONTACT")
    sender = await get_binding(conn, actor, binding.sender.binding_id)
    if sender.sender_binding() != binding.sender or not sender.usable:
        raise _refuse("SENDER_BINDING_CHANGED")
    authorization = await current_authorization(conn, actor)
    if (
        authorization is None
        or authorization.version != binding.authorization_version
        or authorization.record_hash != binding.authorization_fingerprint
    ):
        raise _refuse("AUTHORIZATION_CHANGED")
    identity = InquiryIdentity(workspace_id=ws, vehicle=record.vehicle, seller_key=record.seller_key)
    await _hold_errors(conn, actor, record, controls, identity=identity, now=now)
    facts = binding.qualified_listing
    async with mapped_errors():
        if record.state != InquiryState.QUALIFYING:
            require_transition(record.state, InquiryState.QUALIFYING)
            await conn.execute(
                "update app.seller_inquiries set state = 'qualifying', row_version = row_version + 1"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": inquiry_id},
            )
        require_transition(InquiryState.QUALIFYING, InquiryState.RESERVED)
        await conn.execute(
            "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id) values (%(ws)s, %(id)s)",
            {"ws": ws, "id": inquiry_id},
        )
        row = await fetch_one(
            conn,
            "update app.seller_inquiries set state = 'reserved', state_reasons = %(state_reasons)s,"
            " qualification_revision_id = %(revision)s, qualification_revision_number = %(revision_number)s,"
            " qualified_semantic_hash = %(semantic)s, qualified_price_minor = %(price)s,"
            " qualified_currency = %(currency)s, qualified_availability = %(availability)s,"
            " readiness = %(readiness)s, readiness_reasons = %(readiness_reasons)s,"
            " readiness_rationale_hash = %(rationale)s, readiness_rules_version = %(rules)s,"
            " readiness_evaluated_at = %(evaluated)s, authorization_id = %(auth_id)s,"
            " authorization_version = %(auth_version)s, authorization_fingerprint = %(auth_fp)s,"
            " template_id = %(template_id)s, template_version = %(template_version)s,"
            " template_hash = %(template_hash)s, template_set_version = %(template_set)s,"
            " language = %(language)s, scope_hash = %(scope)s, body_hash = %(body_hash)s,"
            " binding_hash = %(binding_hash)s, original_subject = %(subject)s, original_body = %(body)s,"
            " mk_preview_subject = %(preview_subject)s, mk_preview_body = %(preview_body)s,"
            " mk_preview_hash = %(preview_hash)s, sender_binding_id = %(sender_id)s,"
            " sender_binding_version = %(sender_version)s, sender_provider = %(provider)s,"
            " sender_account_id = %(account)s, sender_from_address = %(from)s,"
            " sender_display_name = %(display)s, sender_reply_to_address = %(reply_to)s,"
            " recipient_contact_id = %(contact)s, recipient_address = %(address)s,"
            " recipient_binding_hash = %(recipient_hash)s, row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s returning row_version",
            {
                "ws": ws,
                "id": inquiry_id,
                "state_reasons": ["RESERVED_UNDER_STANDING_AUTHORIZATION"],
                "revision": facts.revision_id,
                "revision_number": facts.revision_number,
                "semantic": facts.semantic_hash,
                "price": facts.price_amount_minor,
                "currency": facts.price_currency,
                "availability": facts.availability.value,
                "readiness": decision.readiness.value,
                "readiness_reasons": _codes((r.code.value for r in decision.reasons), 60),
                "rationale": decision.rationale_hash,
                "rules": decision.rationale_version,
                "evaluated": decision.as_of,
                "auth_id": authorization.id,
                "auth_version": binding.authorization_version,
                "auth_fp": binding.authorization_fingerprint,
                "template_id": binding.template_id,
                "template_version": binding.template_version,
                "template_hash": binding.template_hash,
                "template_set": message.template_set_version,
                "language": binding.language.value,
                "scope": binding.scope_hash,
                "body_hash": binding.body_hash,
                "binding_hash": binding.binding_hash(),
                "subject": message.subject,
                "body": message.body,
                "preview_subject": preview.subject,
                "preview_body": preview.body,
                "preview_hash": preview.body_hash,
                "sender_id": binding.sender.binding_id,
                "sender_version": binding.sender.binding_version,
                "provider": binding.sender.provider.value,
                "account": binding.sender.account_id,
                "from": binding.sender.from_address,
                "display": binding.sender.display_name,
                "reply_to": binding.sender.reply_to_address,
                "contact": contact.id,
                "address": binding.recipient.canonical_address,
                "recipient_hash": binding.recipient.fingerprint(),
            },
        )
    assert row is not None
    await audit.record(
        conn,
        actor,
        "seller_inquiry.reserve",
        "seller_inquiry",
        inquiry_id,
        prior_version=record.row_version,
        new_version=int(row["row_version"]),
        reason="reserved under the bounded standing authorization (no message approval)",
        metadata={
            "language": binding.language.value,
            "template_id": binding.template_id,
            "authorization_version": binding.authorization_version,
            "sender_binding_version": binding.sender.binding_version,
        },
    )
    return await get_inquiry(conn, actor, inquiry_id)


async def queue(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> InquiryRecord:
    """``reserved -> queued`` (kill switch off, mode automatic; database ``queue`` preflight)."""
    require_inquiry_writer(actor)
    controls = await _locked_controls(conn, actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    if record.state == InquiryState.QUEUED:
        return record
    require_transition(record.state, InquiryState.QUEUED)
    if record.state != InquiryState.RESERVED:
        raise VersionConflict("Only a reserved inquiry is queued here", current_state=record.state.value)
    if controls.kill_switch:
        raise VersionConflict("The seller inquiry kill switch is active", reason="inquiry_kill_switch")
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiries set state = 'queued', row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": inquiry_id},
        )
    return await get_inquiry(conn, actor, inquiry_id)


# =============================================================================================
# Dispatch (immediately before transmission)
# =============================================================================================


def rebuild_message(record: InquiryRecord, contact: SellerContactRecord) -> RenderedMessage | None:
    """The bound registered-template rendering, rebuilt from the stored subject/body.

    The vehicle label is recovered from the subject with the template's own literal text around
    it; every other placeholder comes from the bound records. ``None`` when the stored text is not
    that rendering (the dispatch then cancels the inquiry as stale).
    """
    if (
        record.template_id is None
        or record.original_subject is None
        or record.original_body is None
        or record.sender_display_name is None
        or record.body_hash is None
        or record.scope_hash is None
    ):
        return None
    template = TEMPLATES.get(record.template_id)
    if template is None or template.kind != "seller_inquiry":
        return None
    parts = _PLACEHOLDER_RE.split(template.subject)
    pattern = ""
    for index, part in enumerate(parts):
        if index % 2 == 0:
            pattern += re.escape(part)
        elif part == "vehicle_label":
            pattern += r"(?P<vehicle_label>[^\n]+?)"
        elif part == "listing_reference":
            pattern += re.escape(contact.listing_reference)
        else:  # pragma: no cover - subjects carry only these two placeholders
            return None
    match = re.fullmatch(pattern, record.original_subject)
    if match is None:
        return None
    try:
        return RenderedMessage(
            template_id=template.template_id,
            template_version=template.version,
            template_hash=template.template_hash(),
            template_set_version=record.template_set_version or "",
            kind="seller_inquiry",
            language=template.language,
            subject=record.original_subject,
            body=record.original_body,
            placeholders=InquiryPlaceholders(
                vehicle_label=match.group("vehicle_label"),
                listing_reference=contact.listing_reference,
                listing_url=contact.listing_url,
                sender_display_name=record.sender_display_name,
            ),
            body_hash=record.body_hash,
            scope_hash=record.scope_hash,
        )
    except ValidationError:
        return None


def rebuild_binding(record: InquiryRecord, contact: SellerContactRecord) -> InquiryBinding | None:
    """The immutable ``InquiryBinding`` of a reserved record (``None`` if it cannot be rebuilt)."""
    if (
        record.authorization_version is None
        or record.authorization_fingerprint is None
        or record.template_id is None
        or record.template_version is None
        or record.template_hash is None
        or record.language is None
        or record.scope_hash is None
        or record.body_hash is None
        or record.sender_binding_id is None
        or record.sender_binding_version is None
        or record.sender_provider is None
        or record.sender_account_id is None
        or record.sender_from_address is None
        or record.sender_display_name is None
        or record.qualification_revision_number is None
        or record.qualified_semantic_hash is None
        or record.qualified_availability is None
        or record.readiness_rationale_hash is None
        or contact.status not in ("verified", "changed")
        or contact.verified_at is None
    ):
        return None
    try:
        return InquiryBinding(
            inquiry_id=record.id,
            identity_key=record.identity_key,
            vehicle_key=record.vehicle_key,
            authorization_version=record.authorization_version,
            authorization_fingerprint=record.authorization_fingerprint,
            template_id=record.template_id,
            template_version=record.template_version,
            template_hash=record.template_hash,
            language=record.language,
            scope_hash=record.scope_hash,
            body_hash=record.body_hash,
            sender=SenderBinding(
                binding_id=record.sender_binding_id,
                binding_version=record.sender_binding_version,
                provider=record.sender_provider,
                account_id=record.sender_account_id,
                from_address=record.sender_from_address,
                display_name=record.sender_display_name,
                reply_to_address=record.sender_reply_to_address,
            ),
            recipient=recipient_binding(contact),
            qualified_listing=ListingFactsSnapshot(
                listing_id=record.qualification_listing_id,
                listing_incarnation_id=record.qualification_listing_id,
                revision_id=record.qualification_revision_id,
                revision_number=record.qualification_revision_number,
                semantic_hash=record.qualified_semantic_hash,
                price_amount_minor=record.qualified_price_minor,
                price_currency=record.qualified_currency,
                availability=record.qualified_availability,
            ),
            readiness_rationale_hash=record.readiness_rationale_hash,
        )
    except (ValidationError, ValidationFailed):
        return None


def _stale(reasons: Sequence[str]) -> PreflightDecision:
    return PreflightDecision(
        outcome=PreflightOutcome.CANCEL_STALE, target_state=InquiryState.CANCELLED, reasons=tuple(reasons)
    )


#: Suppressions that lift on their own (owner resume, source resume). A re-queued inquiry whose
#: earlier attempt provably never left can never be re-qualified (the database forbids it once an
#: attempt exists), so for it these stop only this dispatch (``hold``) instead of closing it.
_TRANSIENT_SUPPRESSIONS: Final = frozenset({SuppressionReason.KILL_SWITCH, SuppressionReason.SOURCE_PAUSED})


def _held(record: InquiryRecord, decision: PreflightDecision, *extra: str) -> DispatchResult:
    held = PreflightDecision(
        outcome=PreflightOutcome.HOLD,
        reasons=tuple(dict.fromkeys((*decision.reasons, *extra))),
        next_attempt_at=None,
    )
    return DispatchResult(outcome="hold", inquiry_id=record.id, decision=held)


async def _apply_preflight(
    conn: Conn, actor: ActorContext, record: InquiryRecord, decision: PreflightDecision
) -> DispatchResult:
    """Record a non-proceed preflight decision; the result always reports the state truthfully."""
    if decision.outcome == PreflightOutcome.HOLD:
        return DispatchResult(
            outcome="hold", inquiry_id=record.id, decision=decision, next_attempt_at=decision.next_attempt_at
        )
    attempted = record.send_attempted_at is not None
    if decision.target_state == InquiryState.SUPPRESSED:
        reason = decision.suppression_reason or SuppressionReason.MANUAL
        if attempted and reason in _TRANSIENT_SUPPRESSIONS:
            return _held(record, decision, "TRANSIENT_SUPPRESSION_AFTER_RETRY")
        changed = await cancel_untransmitted(
            conn,
            actor,
            [record.id],
            reasons=decision.reasons,
            target="suppressed",
            suppression_reason=reason.value,
        )
        if not changed:  # pragma: no cover - a queued inquiry never has a possibly transmitted attempt
            return _held(record, decision, "NOT_CANCELLABLE")
        return DispatchResult(outcome="suppressed", inquiry_id=record.id, decision=decision)
    changed = await cancel_untransmitted(conn, actor, [record.id], reasons=decision.reasons)
    if not changed:  # pragma: no cover - see above
        return _held(record, decision, "NOT_CANCELLABLE")
    return DispatchResult(outcome="cancelled", inquiry_id=record.id, decision=decision)


async def dispatch(
    conn: Conn,
    actor: ActorContext,
    inquiry_id: UUID,
    *,
    lease: AttemptLease,
    message_approval_required: bool,
    job_id: UUID | None = None,
    outbox_id: UUID | None = None,
    attempt_id: UUID | None = None,
) -> DispatchResult:
    """Revalidate immediately before transmission and commit the send intent (spec 37.5).

    Must run in the dispatch job's unit of work (the job row is locked first). Takes the controls
    row, then the seller entity and the inquiry, re-reads every fact under those locks into
    ``DispatchFacts`` and runs ``domain.inquiries.dispatch_preflight``:

    - ``proceed``: ``queued -> sending`` (the database repeats caps counted at the send attempt,
      cooldown, suppressions, kill switch, staleness, sender version) and the running attempt row
      with ``lease`` as its fence; the built message (exact registered rendering, one recipient)
      is returned for the provider. No external I/O happens before this commits.
    - ``cancel_stale``: ``cancelled`` (price/availability/revision/recipient/sender/identity changed)
      or ``suppressed`` (kill switch, revoked authorization or sender, source pause, suppression);
      nothing was transmitted, so the quota debit is released, unless an earlier attempt (proven
      never submitted) exists: then the debit stays, and a kill switch or source pause only holds.
    - ``hold``: nothing changes; ``next_attempt_at`` says when caps/cooldown free (``None`` = unknown).

    ``message_approval_required`` must be ``requires_message_approval(settings)`` (``False`` under
    the standing authorization); it is required so an owner setting is never dropped by omission.
    """
    require_inquiry_writer(actor)
    approval_required = require_message_approval_flag(message_approval_required)
    ws = actor.workspace_id
    controls = await _locked_controls(conn, actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    async with mapped_errors():
        now = await _now(conn)
        tx_now = await _tx_now(conn)
    if record.state != InquiryState.QUEUED:
        return DispatchResult(
            outcome="hold",
            inquiry_id=record.id,
            decision=PreflightDecision(outcome=PreflightOutcome.HOLD, reasons=("NOT_QUEUED",)),
        )
    if lease.expires_at <= now:
        raise LeaseLost("The dispatch lease expired before the send intent was committed")
    if record.recipient_contact_id is None or record.sender_binding_id is None:
        return await _apply_preflight(conn, actor, record, _stale(["BINDING_INCOMPLETE"]))
    bound_contact = await get_contact(conn, actor, record.recipient_contact_id)
    binding = rebuild_binding(record, bound_contact)
    message = rebuild_message(record, bound_contact)
    if (
        binding is None
        or message is None
        or binding.binding_hash() != record.binding_hash
        or binding.recipient.fingerprint() != record.recipient_binding_hash
    ):
        return await _apply_preflight(conn, actor, record, _stale(["MESSAGE_BINDING_MISMATCH"]))
    async with mapped_errors():
        listing = await _listing_row(conn, ws, record.qualification_listing_id)
        root_row = await fetch_one(
            conn,
            "select coalesce(merged_into_id, id) as root from app.seller_entities"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": record.seller_entity_id},
        )
        attempts_rows = await fetch_all(
            conn,
            _SELECT_ATTEMPT + " where workspace_id = %(ws)s and inquiry_id = %(id)s order by attempt_number",
            {"ws": ws, "id": inquiry_id},
        )
        debit = await fetch_one(
            conn,
            "select 1 as present from ops.inquiry_quota_ledger"
            " where workspace_id = %(ws)s and inquiry_id = %(id)s and released_at is null",
            {"ws": ws, "id": inquiry_id},
        )
    assert root_row is not None
    root: UUID = root_row["root"]
    attempts = [AttemptRecord.model_validate(r) for r in attempts_rows]
    sender_record = await get_binding(conn, actor, record.sender_binding_id)
    authorization = await current_authorization(conn, actor)
    if authorization is None:
        return await _apply_preflight(conn, actor, record, _stale(["AUTHORIZATION_CHANGED"]))
    confirmed_now: list[UUID] = list(listing["confirmed_cluster_ids"] or ())
    if record.vehicle_kind == "vehicle_cluster" and record.vehicle_cluster_id in confirmed_now:
        cluster_now: UUID | None = record.vehicle_cluster_id
    else:
        cluster_now = confirmed_now[0] if confirmed_now else None
    identity = InquiryIdentity(
        workspace_id=ws,
        vehicle=canonical_vehicle_identity(
            vehicle_cluster_id=cluster_now, listing_incarnation_id=record.qualification_listing_id
        ),
        seller_key=f"seller_entity:{root}",
    )
    if listing["current_revision_id"] is None or listing["revision_number"] is None:
        return await _apply_preflight(conn, actor, record, _stale(["LISTING_REVISION_CHANGED"]))
    current_listing = _listing_facts(listing)
    current = await current_contact(conn, actor, record.qualification_listing_id)
    recheck = detect_contact_change(binding.recipient, recipient_decision(current), now=now)
    if bound_contact.status != "verified":
        recheck = ContactRecheck(
            material_change=True,
            recheck_required=True,
            changes=tuple(dict.fromkeys((*recheck.changes, ContactChange.RECIPIENT_NO_LONGER_VERIFIED))),
        )
    attempt_number = len(attempts) + 1
    sender_binding = binding.sender
    try:
        built = build_inquiry_message(
            message,
            inquiry_id=record.id,
            attempt_number=attempt_number,
            sender=mailbox(sender_binding.from_address, sender_binding.display_name),
            recipient_address=binding.recipient.canonical_address,
            reply_to_address=sender_binding.reply_to_address,
            date=tx_now,
        )
    except (MimeBuildError, ValidationError, ValueError):
        return await _apply_preflight(conn, actor, record, _stale(["MESSAGE_NOT_TEMPLATE_RENDERING"]))
    async with mapped_errors():
        hood = await _neighbourhood(
            conn,
            ws,
            seller_root_id=root,
            listing_id=record.qualification_listing_id,
            exclude_identity=None,
            exclude_inquiry=record.id,
        )
        debits = await _debits(conn, ws)
        matched = await _matched_suppressions(
            conn,
            ws,
            seller_root_id=root,
            address=binding.recipient.canonical_address,
            vehicle_key=record.vehicle_key,
            listing_id=record.qualification_listing_id,
            cluster_id=record.vehicle_cluster_id or listing["confirmed_cluster_id"],
            source_key=listing["source_key"],
            sender_binding_id=record.sender_binding_id,
        )
    records = _as_target_records(
        matched,
        workspace_id=ws,
        seller_key=binding.recipient.seller_identity_key,
        address=binding.recipient.canonical_address,
        vehicle_key=binding.vehicle_key,
        source_key=binding.recipient.source_key,
        sender_binding_id=record.sender_binding_id,
    )
    assert record.reserved_at is not None
    facts = DispatchFacts(
        now=now,
        state=record.state,
        binding=binding,
        identity=identity,
        reserved_at=record.reserved_at,
        authorization=authorization.authorization,
        workspace_id=ws,
        current_listing=current_listing,
        source=_source_facts(listing),
        disqualifiers=DisqualifierFacts(
            identity_conflict_open=bool(listing["identity_conflict"]) or bool(listing["quarantined"]),
            availability_conflict=bool(listing["availability_conflict"]),
            seller_opted_out=any(r.reason == SuppressionReason.SELLER_OPT_OUT for r in records),
        ),
        sender=sender_status(sender_record, mode=controls.mode, kill_switch=controls.kill_switch),
        recipient_recheck=recheck,
        current_language=language_decision(current),
        suppressions=records,
        other_inquiries=hood.existing,
        related_links=hood.related,
        rate_caps=evaluate_rate_caps(debits, now=now, policy=controls.policy(), exclude_inquiry_id=record.id),
        quota_debit_present=debit is not None,
        seller_cooldown=controls.seller_cooldown,
        attempts=tuple(a.evidence() for a in attempts),
        message=message,
        envelope=built.envelope(),
        message_approval_required=approval_required,
    )
    decision = dispatch_preflight(facts)
    if decision.outcome != PreflightOutcome.PROCEED:
        return await _apply_preflight(conn, actor, record, decision)
    require_transition(InquiryState.QUEUED, InquiryState.SENDING, TransitionContext(preflight=decision))
    fencing = max((a.fencing_token for a in attempts), default=0) + 1
    new_attempt_id = attempt_id or uuid4()
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiries set state = 'sending', state_reasons = %(reasons)s,"
            " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": inquiry_id, "reasons": ["DISPATCH_PREFLIGHT_PROCEED"]},
        )
        row = await fetch_one(
            conn,
            "insert into ops.email_delivery_attempts (workspace_id, inquiry_id, attempt_id, attempt_number,"  # noqa: S608 - fixed column list
            " outbox_id, job_id, sender_binding_id, sender_binding_version, provider, rfc_message_id,"
            " fencing_token, lease_owner, lease_token, lease_expires_at)"
            " values (%(ws)s, %(inquiry)s, %(attempt)s, %(number)s, %(outbox)s, %(job)s, %(sender)s,"
            " %(sender_version)s, %(provider)s, %(message_id)s, %(fencing)s, %(owner)s, %(token)s,"
            " %(expires)s)"
            f" returning {_ATTEMPT_COLUMNS}",
            {
                "ws": ws,
                "inquiry": inquiry_id,
                "attempt": new_attempt_id,
                "number": attempt_number,
                "outbox": outbox_id,
                "job": job_id,
                "sender": sender_binding.binding_id,
                "sender_version": sender_binding.binding_version,
                "provider": sender_binding.provider.value,
                "message_id": built.rfc_message_id,
                "fencing": fencing,
                "owner": lease.owner,
                "token": lease.token,
                "expires": lease.expires_at,
            },
        )
    assert row is not None
    attempt = AttemptRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "seller_inquiry.dispatch",
        "seller_inquiry",
        inquiry_id,
        reason="send intent committed before external I/O",
        metadata={
            "attempt_id": str(attempt.attempt_id),
            "attempt_number": attempt_number,
            "provider": sender_binding.provider.value,
        },
    )
    await _publish_binding(conn, actor, inquiry_id)
    return DispatchResult(
        outcome="proceed",
        inquiry_id=inquiry_id,
        decision=decision,
        attempt=attempt,
        message=built,
        sender=sender_binding,
    )


# =============================================================================================
# Outcomes, reconciliation, retry and the attempt reaper
# =============================================================================================


def _response_summary(outcome: SendAccepted | SendDefiniteFailure | SendUncertain) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": outcome.status}
    if isinstance(outcome, SendDefiniteFailure | SendUncertain):
        summary["reason"] = outcome.reason.value
        summary["http_status"] = outcome.http_status
        summary["provider_error"] = outcome.provider_error
    if isinstance(outcome, SendUncertain):
        summary["outbox_pending"] = outcome.outbox_pending.value
        summary["awaiting_local_worker"] = outcome.awaiting_local_worker
    if isinstance(outcome, SendAccepted):
        summary["observed_rfc_message_id"] = outcome.observed_rfc_message_id
    return summary


async def _lock_attempt(
    conn: Conn, actor: ActorContext, attempt_id: UUID
) -> tuple[InquiryRecord, AttemptRecord]:
    """Inquiry row, then the attempt row, both ``FOR UPDATE``."""
    ws = actor.workspace_id
    async with mapped_errors():
        head = await fetch_one(
            conn,
            "select inquiry_id from ops.email_delivery_attempts where workspace_id = %(ws)s and"
            " attempt_id = %(id)s",
            {"ws": ws, "id": attempt_id},
        )
        if head is None:
            raise NotFound("Send attempt not found")
        inquiry_row = await fetch_one(
            conn,
            _SELECT_INQUIRY + " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": ws, "id": head["inquiry_id"]},
        )
        attempt_row = await fetch_one(
            conn,
            _SELECT_ATTEMPT + " where workspace_id = %(ws)s and attempt_id = %(id)s for update",
            {"ws": ws, "id": attempt_id},
        )
    assert inquiry_row is not None and attempt_row is not None
    return _inquiry(inquiry_row), AttemptRecord.model_validate(attempt_row)


async def record_outcome(
    conn: Conn,
    actor: ActorContext,
    *,
    attempt_id: UUID,
    lease_token: UUID,
    outcome: SendAccepted | SendDefiniteFailure | SendUncertain,
    report_summary: Mapping[str, Any] | None = None,
) -> OutcomeResult:
    """Record a provider/worker outcome for one attempt, fenced by its lease token.

    ``report_summary`` (sanitized, no addresses) is stored with the provider response, e.g. the
    local worker's report for ``reconcile_from_reports``.

    A running attempt is finalised once (``accepted`` / ``pre_submission_failure`` /
    ``definite_rejection`` / ``uncertain``, mapped by ``attempt_outcome``) and the inquiry moves
    along ``domain.inquiries.require_transition``. After the lease expired a pre-submission
    failure is recorded as ``uncertain`` (the worker may still have submitted). A report for an
    already finalised attempt never overwrites it: only positive acceptance evidence is added as
    the one-time reconciliation of an ``uncertain`` attempt; anything else is a no-op. A wrong
    lease token raises ``LeaseLost``. Never triggers a second transmission.
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    record, attempt = await _lock_attempt(conn, actor, attempt_id)
    if attempt.lease_token != lease_token:
        raise LeaseLost("The send attempt belongs to another lease; the report was not recorded")
    if outcome.attempt_id != attempt.attempt_id or outcome.inquiry_id != record.id:
        raise ValidationFailed("the outcome does not belong to this send attempt")
    if outcome.provider != attempt.provider:
        raise ValidationFailed("the outcome names another provider than the attempt")
    if attempt.rfc_message_id is not None and normalize_message_id(
        outcome.rfc_message_id
    ) != normalize_message_id(attempt.rfc_message_id):
        # An outcome is evidence about exactly the message this attempt committed.
        raise ValidationFailed("the outcome names another Message-ID than the send attempt")
    async with mapped_errors():
        now = await _now(conn)
    if attempt.outcome != SendAttemptOutcome.RUNNING:
        if (
            isinstance(outcome, SendAccepted)
            and attempt.outcome == SendAttemptOutcome.UNCERTAIN
            and attempt.reconciled_outcome is None
        ):
            found = "outlook_sent_items" if outcome.receipt.kind == "outlook_sent_items" else "provider"
            evidence = ReconciliationEvidence(
                sent_items="found" if found == "outlook_sent_items" else "not_searched",
                provider_search="found" if found == "provider" else "not_searched",
            )
            result = await _apply_reconciliation(
                conn, actor, record, attempt, evidence, late=True, extra=report_summary
            )
            return OutcomeResult(
                applied=True,
                inquiry_state=result.inquiry_state,
                attempt_outcome=attempt.outcome,
                reconciled=True,
                note="late acceptance recorded as reconciliation evidence",
            )
        return OutcomeResult(
            applied=False,
            inquiry_state=record.state,
            attempt_outcome=attempt.outcome,
            note="attempt already finalised; the late report changes nothing",
        )
    mapping = attempt_outcome(outcome)
    target_outcome = mapping.outcome
    proof = mapping.pre_submission_proof
    error_code: str | None = None
    if target_outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE and now >= attempt.lease_expires_at:
        target_outcome, proof, error_code = SendAttemptOutcome.UNCERTAIN, None, "LATE_PRE_SUBMISSION_REPORT"
    if isinstance(outcome, SendDefiniteFailure | SendUncertain) and error_code is None:
        error_code = outcome.reason.value[:80]
    target_state = {
        SendAttemptOutcome.ACCEPTED: InquiryState.ACCEPTED,
        SendAttemptOutcome.PRE_SUBMISSION_FAILURE: InquiryState.FAILED_DEFINITE,
        SendAttemptOutcome.DEFINITE_REJECTION: InquiryState.FAILED_DEFINITE,
        SendAttemptOutcome.UNCERTAIN: InquiryState.UNCERTAIN,
    }[target_outcome]
    evidence_model = attempt.evidence().model_copy(
        update={"outcome": target_outcome, "pre_submission_proof": proof, "finished_at": now}
    )
    if record.state != InquiryState.SENDING:  # pragma: no cover - evidence trigger keeps them in step
        raise VersionConflict("The inquiry is not sending", current_state=record.state.value)
    require_transition(InquiryState.SENDING, target_state, TransitionContext(attempt=evidence_model))
    accepted = outcome if isinstance(outcome, SendAccepted) else None
    async with mapped_errors():
        await conn.execute(
            "update ops.email_delivery_attempts set outcome = %(outcome)s, finished_at = greatest("
            " clock_timestamp(), send_intent_committed_at), pre_submission_proof = %(proof)s,"
            " provider_message_id = %(pmid)s, provider_thread_id = %(ptid)s, provider_response ="
            " %(response)s,"
            " receipt = %(receipt)s, error_code = %(error)s"
            " where workspace_id = %(ws)s and attempt_id = %(id)s and outcome = 'running'",
            {
                "ws": ws,
                "id": attempt_id,
                "outcome": target_outcome.value,
                "proof": proof,
                "pmid": accepted.provider_message_id if accepted else None,
                "ptid": accepted.provider_thread_id if accepted else None,
                "response": Jsonb(
                    {
                        **_response_summary(outcome),
                        **({"worker_report": dict(report_summary)} if report_summary else {}),
                    }
                ),
                "receipt": Jsonb(accepted.receipt.model_dump(mode="json")) if accepted else None,
                "error": error_code,
            },
        )
        params: dict[str, Any] = {
            "ws": ws,
            "id": record.id,
            "state": target_state.value,
            "reasons": _codes(
                [f"ATTEMPT_{target_outcome.value.upper()}", *([error_code] if error_code else [])]
            ),
        }
        if accepted is not None:
            await conn.execute(
                "update app.seller_inquiries set state = 'accepted', state_reasons = %(reasons)s,"
                # A reported acceptance time lies between the send attempt and now (never
                # backdated before the attempt, never in the future).
                " accepted_at = greatest(send_attempted_at, least(%(accepted_at)s::timestamptz, now())),"
                " rfc_message_id = coalesce(rfc_message_id, %(message_id)s),"
                " provider_message_id = coalesce(provider_message_id, %(pmid)s),"
                " provider_thread_id = coalesce(provider_thread_id, %(ptid)s),"
                " provider_receipt = coalesce(provider_receipt, %(receipt)s),"
                " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                {
                    **params,
                    "accepted_at": accepted.accepted_at,
                    "message_id": attempt.rfc_message_id,
                    "pmid": accepted.provider_message_id,
                    "ptid": accepted.provider_thread_id,
                    "receipt": Jsonb(accepted.receipt.model_dump(mode="json")),
                },
            )
        else:
            await conn.execute(
                "update app.seller_inquiries set state = %(state)s, state_reasons = %(reasons)s,"
                " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                params,
            )
    await audit.record(
        conn,
        actor,
        "seller_inquiry.send_outcome",
        "seller_inquiry",
        record.id,
        reason=f"send attempt {target_outcome.value}",
        metadata={"attempt_id": str(attempt_id), "outcome": target_outcome.value, "error_code": error_code},
    )
    await _publish_binding(conn, actor, record.id)
    return OutcomeResult(applied=True, inquiry_state=target_state, attempt_outcome=target_outcome)


async def _apply_reconciliation(
    conn: Conn,
    actor: ActorContext,
    record: InquiryRecord,
    attempt: AttemptRecord,
    evidence: ReconciliationEvidence,
    *,
    late: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> ReconcileResult:
    decision = reconcile_uncertain(evidence)
    if decision.next_state is None:
        await audit.record(
            conn,
            actor,
            "seller_inquiry.reconcile_check",
            "seller_inquiry",
            record.id,
            reason="still uncertain: " + ", ".join(decision.reasons),
            metadata={"attempt_id": str(attempt.attempt_id), "reasons": list(decision.reasons)},
        )
        return ReconcileResult(decision=decision, inquiry_state=record.state, attempt_id=attempt.attempt_id)
    reconciled = "accepted" if decision.next_state == InquiryState.ACCEPTED else "proven_not_submitted"
    if record.state == InquiryState.UNCERTAIN:
        require_transition(
            InquiryState.UNCERTAIN, decision.next_state, TransitionContext(reconciliation=decision)
        )
    async with mapped_errors():
        await conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = %(outcome)s, reconciled_at = now(),"
            " reconciliation_evidence = %(evidence)s"
            " where workspace_id = %(ws)s and attempt_id = %(id)s and reconciled_outcome is null",
            {
                "ws": actor.workspace_id,
                "id": attempt.attempt_id,
                "outcome": reconciled,
                "evidence": Jsonb(
                    {
                        **evidence.model_dump(mode="json"),
                        "late_report": late,
                        **({"worker_report": dict(extra)} if extra else {}),
                    }
                ),
            },
        )
        if record.state == InquiryState.UNCERTAIN:
            if decision.next_state == InquiryState.ACCEPTED:
                await conn.execute(
                    "update app.seller_inquiries set state = 'accepted', state_reasons = %(reasons)s,"
                    " rfc_message_id = coalesce(rfc_message_id, %(message_id)s), row_version = row_version +"
                    " 1"
                    " where workspace_id = %(ws)s and id = %(id)s",
                    {
                        "ws": actor.workspace_id,
                        "id": record.id,
                        "reasons": _codes(decision.reasons),
                        "message_id": attempt.rfc_message_id,
                    },
                )
            else:
                await conn.execute(
                    "update app.seller_inquiries set state = 'failed_definite', state_reasons = %(reasons)s,"
                    " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                    {"ws": actor.workspace_id, "id": record.id, "reasons": _codes(decision.reasons)},
                )
    await audit.record(
        conn,
        actor,
        "seller_inquiry.reconcile",
        "seller_inquiry",
        record.id,
        reason=f"reconciled: {reconciled}",
        metadata={"attempt_id": str(attempt.attempt_id), "outcome": reconciled},
    )
    await _publish_binding(conn, actor, record.id)
    state = decision.next_state if record.state == InquiryState.UNCERTAIN else record.state
    return ReconcileResult(decision=decision, inquiry_state=state, attempt_id=attempt.attempt_id)


async def reconcile(
    conn: Conn,
    actor: ActorContext,
    inquiry_id: UUID,
    *,
    evidence: ReconciliationEvidence,
    extra: Mapping[str, Any] | None = None,
) -> ReconcileResult:
    """Resolve the unresolved uncertain attempt of an ``uncertain`` inquiry on positive evidence.

    ``reconcile_uncertain`` decides: a Sent Items/provider hit or a Message-ID-linked inbound
    message -> ``accepted``; a documented pre-submission proof with no live worker and nothing in
    the Outbox -> ``failed_definite`` (eligible for the guarded retry). An empty search keeps the
    inquiry ``uncertain`` with its reservation and quota debit (only an audit row is written).
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select attempt_id from ops.email_delivery_attempts where workspace_id = %(ws)s"
            " and inquiry_id = %(id)s and outcome = 'uncertain' and reconciled_outcome is null"
            " order by attempt_number desc limit 1",
            {"ws": ws, "id": inquiry_id},
        )
    if row is None:
        raise VersionConflict(
            "The inquiry has no unresolved uncertain send attempt", reason="nothing_to_reconcile"
        )
    record, attempt = await _lock_attempt(conn, actor, row["attempt_id"])
    if record.state != InquiryState.UNCERTAIN or attempt.reconciled_outcome is not None:
        raise VersionConflict("The inquiry is no longer uncertain", current_state=record.state.value)
    return await _apply_reconciliation(conn, actor, record, attempt, evidence, extra=extra)


async def retry(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> InquiryRecord:
    """Guarded ``failed_definite -> queued`` (``domain.inquiries.should_retry``; same account only).

    Allowed only after a proven pre-submission failure (or a reconciled proof of non-submission)
    with no running, accepted or unresolved attempt and attempts remaining; the database repeats
    the rule. The next dispatch then runs the full preflight again.
    """
    require_inquiry_writer(actor)
    await _locked_controls(conn, actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    if record.state != InquiryState.FAILED_DEFINITE:
        raise VersionConflict(
            "Only a definitely failed inquiry can be retried", current_state=record.state.value
        )
    attempts = await list_attempts(conn, actor, inquiry_id)
    if not attempts:  # pragma: no cover - the evidence trigger forbids it
        raise VersionConflict("The inquiry has no send attempt")
    async with mapped_errors():
        now = await _now(conn)
    evidence = [a.evidence() for a in attempts]
    decision = should_retry(
        evidence[-1], now=now, other_attempts=evidence[:-1], retry_sender_binding_id=record.sender_binding_id
    )
    if not decision.retry:
        raise EmailDeliveryUncertain(
            "The retry policy refuses a new transmission",
            details={"reason": "retry_refused", "reasons": list(decision.reasons)},
        )
    require_transition(InquiryState.FAILED_DEFINITE, InquiryState.QUEUED, TransitionContext(retry=decision))
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiries set state = 'queued', state_reasons = %(reasons)s,"
            " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": inquiry_id, "reasons": _codes(decision.reasons)},
        )
    await audit.record(
        conn,
        actor,
        "seller_inquiry.retry",
        "seller_inquiry",
        inquiry_id,
        reason="guarded retry after proven non-submission",
        metadata={"reasons": list(decision.reasons)},
    )
    return await get_inquiry(conn, actor, inquiry_id)


async def reap_expired_attempts(db: Database, workspace_id: UUID, *, limit: int = 100) -> tuple[UUID, ...]:
    """Crashed/expired send attempts become ``uncertain`` together with their inquiry (spec 37.5).

    The reservation and quota debit stay; nothing is ever re-queued for another send. Inquiries
    locked by a live worker are skipped until the next pass. Returns the attempt ids reaped.
    """
    if not 1 <= limit <= 1000:
        raise ValidationFailed("invalid reaper limit")
    actor = ActorContext.system(workspace_id, request_id="attempt-reaper")
    reaped: list[UUID] = []
    async with mapped_errors(), db.transaction(actor) as conn, mapped_errors():
        rows = await fetch_all(
            conn,
            "select a.attempt_id, a.inquiry_id from ops.email_delivery_attempts a"
            " where a.workspace_id = %(ws)s and a.outcome = 'running' and a.lease_expires_at <="
            " clock_timestamp()"
            " order by a.lease_expires_at, a.id limit %(limit)s",
            {"ws": workspace_id, "limit": limit},
        )
        for row in rows:
            locked = await fetch_one(
                conn,
                "select id, state from app.seller_inquiries where workspace_id = %(ws)s and id = %(id)s"
                " for update skip locked",
                {"ws": workspace_id, "id": row["inquiry_id"]},
            )
            if locked is None:
                continue
            changed = await fetch_one(
                conn,
                "update ops.email_delivery_attempts set outcome = 'uncertain',"
                " finished_at = greatest(clock_timestamp(), send_intent_committed_at), error_code = %(code)s"
                " where workspace_id = %(ws)s and attempt_id = %(id)s and outcome = 'running'"
                " and lease_expires_at <= clock_timestamp() returning attempt_id",
                {"ws": workspace_id, "id": row["attempt_id"], "code": LEASE_EXPIRED},
            )
            if changed is None:
                continue
            if locked["state"] == InquiryState.SENDING.value:
                require_transition(InquiryState.SENDING, InquiryState.UNCERTAIN)
                await conn.execute(
                    "update app.seller_inquiries set state = 'uncertain', state_reasons = %(reasons)s,"
                    " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                    {"ws": workspace_id, "id": row["inquiry_id"], "reasons": ["SEND_ATTEMPT_LEASE_EXPIRED"]},
                )
            await audit.record(
                conn,
                actor,
                "seller_inquiry.attempt_reaped",
                "seller_inquiry",
                row["inquiry_id"],
                reason="send attempt lease expired without a recorded outcome; held for reconciliation",
                metadata={"attempt_id": str(row["attempt_id"])},
            )
            await _publish_binding(conn, actor, row["inquiry_id"])
            reaped.append(row["attempt_id"])
    return tuple(reaped)


# =============================================================================================
# Staleness, cancellation and re-qualification
# =============================================================================================


async def cancel_inquiry(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, *, reasons: Sequence[str]
) -> InquiryRecord:
    """Cancel never-transmitted work (quota debit released); refuses anything (possibly) sent."""
    require_inquiry_writer(actor)
    await _locked_controls(conn, actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    if record.state == InquiryState.CANCELLED:
        return record
    changed = (
        await cancel_untransmitted(conn, actor, [inquiry_id], reasons=_codes(reasons) or ["CANCELLED"])
        if record.state in CANCELLABLE_STATES
        else ()
    )
    if not changed:
        raise VersionConflict(
            "A (possibly) transmitted or finished inquiry is never cancelled",
            current_state=record.state.value,
        )
    return await get_inquiry(conn, actor, inquiry_id)


async def cancel_stale_inquiries(conn: Conn, actor: ActorContext, *, listing_id: UUID) -> tuple[UUID, ...]:
    """Cancel reserved/queued inquiries of a listing whose bound facts changed (spec 37.5).

    Compares the bound qualification snapshot (revision, semantic hash, price, availability) with
    the listing's current facts; a sold/removed/reserved listing also cancels. Call after a
    listing revision or availability change; the dispatch preflight repeats the check.
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    await _locked_controls(conn, actor)
    async with mapped_errors():
        listing = await _listing_row(conn, ws, listing_id)
        rows = await fetch_all(
            conn,
            _SELECT_INQUIRY + " where workspace_id = %(ws)s and qualification_listing_id = %(id)s"
            " and state in ('reserved', 'queued') order by id",
            {"ws": ws, "id": listing_id},
        )
    stale: list[UUID] = []
    reasons: dict[UUID, list[str]] = {}
    for row in rows:
        record = _inquiry(row)
        codes: list[str] = []
        if listing["current_revision_id"] != record.qualification_revision_id:
            codes.append("LISTING_REVISION_CHANGED")
        if (listing["asking_minor"], listing["currency"]) != (
            record.qualified_price_minor,
            record.qualified_currency,
        ):
            codes.append("PRICE_CHANGED")
        if (
            record.qualified_availability is None
            or listing["availability"] != record.qualified_availability.value
        ):
            codes.append("AVAILABILITY_CHANGED")
        if listing["availability"] in ("sold_claimed", "removed", "reserved"):
            codes.append("VEHICLE_UNAVAILABLE")
        if codes:
            stale.append(record.id)
            reasons[record.id] = codes
    cancelled: list[UUID] = []
    for inquiry_id in stale:
        await _lock_inquiry(conn, actor, inquiry_id)
        cancelled.extend(await cancel_untransmitted(conn, actor, [inquiry_id], reasons=reasons[inquiry_id]))
    return tuple(cancelled)


async def _requalify(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, *, reason: str, code: str
) -> InquiryRecord:
    record = await get_inquiry(conn, actor, inquiry_id)
    if record.state not in (InquiryState.CANCELLED, InquiryState.SUPPRESSED):
        raise VersionConflict(
            "Only a cancelled or suppressed inquiry is re-qualified", current_state=record.state.value
        )
    attempts = await list_attempts(conn, actor, inquiry_id)
    audit_id = await audit.record(
        conn,
        actor,
        "seller_inquiry.requalify",
        "seller_inquiry",
        inquiry_id,
        prior_version=record.row_version,
        reason=reason,
        metadata={"from_state": record.state.value, "suppression_reason": record.suppression_reason},
    )
    require_transition(
        record.state,
        InquiryState.QUALIFYING,
        TransitionContext(transmission_attempts=len(attempts), suppression_removal_audit_id=audit_id),
    )
    async with mapped_errors():
        await conn.execute(
            "update app.seller_inquiries set state = 'qualifying', suppression_reason = null,"
            " requalification_audit_id = case when state = 'suppressed' then %(audit)s::uuid"
            "   else requalification_audit_id end,"
            " state_reasons = %(reasons)s, row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": inquiry_id, "audit": audit_id, "reasons": [code]},
        )
    return await get_inquiry(conn, actor, inquiry_id)


async def requalify(conn: Conn, actor: ActorContext, inquiry_id: UUID, *, reason: str) -> InquiryRecord:
    """Re-open a never-transmitted cancelled/suppressed inquiry (``-> qualifying``, audited).

    Leaving ``suppressed`` is an explicit owner/system decision recorded as an audit event about
    the inquiry (``requalification_audit_id``); the matching suppression row, if any, must be
    removed separately (``remove_suppression``) or the next reservation is refused again.
    """
    require_inquiry_writer(actor)
    text = _reason_text(reason)
    await _locked_controls(conn, actor)
    await _lock_inquiry(conn, actor, inquiry_id)
    return await _requalify(conn, actor, inquiry_id, reason=text, code="REQUALIFIED")


#: The reply-driven steps of the state machine (``domain.replies`` / ``ALLOWED_TRANSITIONS``).
_STORED_REPLY_STEPS: Final[dict[InquiryState, frozenset[InquiryState]]] = {
    InquiryState.ACCEPTED: frozenset(
        {InquiryState.REPLIED, InquiryState.BOUNCED, InquiryState.SELLER_OPTED_OUT}
    ),
    InquiryState.NO_REPLY_YET: frozenset(
        {InquiryState.REPLIED, InquiryState.BOUNCED, InquiryState.SELLER_OPTED_OUT}
    ),
    InquiryState.REPLIED: frozenset({InquiryState.SELLER_OPTED_OUT}),
}
_STORED_REPLIES_SQL: Final = (
    "select id, message_type, claims from app.seller_replies where workspace_id = %(ws)s"
    " and inquiry_id = %(id)s and not quarantined and message_type in ('seller_reply', 'bounce')"
    " order by ingested_at, id"
)


def _stored_reply_target(message_type: str, claims: Mapping[str, Any] | None) -> InquiryState:
    """The state a matched reply moves an accepted inquiry to (``decide_reply_processing``): a
    bounce -> ``bounced``; a seller reply that opts out or complains -> ``seller_opted_out``;
    any other seller reply -> ``replied``."""
    if message_type == "bounce":
        return InquiryState.BOUNCED
    if claims:
        try:
            parsed = ReplyClaims.model_validate(dict(claims))
        except ValidationError:
            parsed = None
        if parsed is not None and (parsed.opted_out or parsed.complaint):
            return InquiryState.SELLER_OPTED_OUT
    return InquiryState.REPLIED


async def mark_replied(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, *, reply_id: UUID | None = None
) -> InquiryRecord | None:
    """Apply the state steps of replies stored before the send was accepted (B2a addition).

    A reply that arrived while the send was still ``sending``/``uncertain`` is stored without a
    state step (``domain.replies`` has no reply transition from those states). Once the send is
    accepted (a worker/provider report, or the reconciliation citing that very reply), the
    reconcile job, the seller-reply processing job or the reconciliation pass walks every stored,
    unquarantined seller reply and bounce of this inquiry in ingest order, exactly as the ingest
    would have: ``accepted``/``no_reply_yet`` -> ``replied`` | ``bounced`` | ``seller_opted_out``
    (an opt-out or complaint), and ``replied`` -> ``seller_opted_out``; a step the state machine
    does not allow (a late bounce after a reply) is no step. The suppressions were recorded at
    ingest already. ``reply_id`` names the triggering reply for the audit only. Lock order: the
    inquiry row, then the binding publication (as ``record_outcome``). Returns the updated
    record, or ``None`` when no step applies.
    """
    require_inquiry_writer(actor)
    record = await _lock_inquiry(conn, actor, inquiry_id)
    if record.state not in (InquiryState.ACCEPTED, InquiryState.NO_REPLY_YET):
        return None
    async with mapped_errors():
        rows = await fetch_all(conn, _STORED_REPLIES_SQL, {"ws": actor.workspace_id, "id": inquiry_id})
    state: InquiryState = record.state
    path: list[tuple[InquiryState, UUID]] = []
    for row in rows:
        target = _stored_reply_target(row["message_type"], row["claims"])
        if target != state and target in _STORED_REPLY_STEPS.get(state, frozenset()):
            path.append((target, row["id"]))
            state = target
    if not path:
        return None
    previous: InquiryState = record.state
    for target, _source in path:
        require_transition(previous, target)
        async with mapped_errors():
            await conn.execute(
                "update app.seller_inquiries set state = %(to)s, state_reasons = %(reasons)s,"
                " row_version = row_version + 1"
                " where workspace_id = %(ws)s and id = %(id)s and state = %(from)s",
                {
                    "ws": actor.workspace_id,
                    "id": inquiry_id,
                    "from": previous.value,
                    "to": target.value,
                    "reasons": ["SELLER_REPLY_STORED_BEFORE_ACCEPTANCE"],
                },
            )
        previous = target
    await audit.record(
        conn,
        actor,
        "seller_inquiry.replied",
        "seller_inquiry",
        inquiry_id,
        prior_version=record.row_version,
        reason="seller reply stored before the send was reconciled",
        metadata={
            "reply_id": str(reply_id or path[-1][1]),
            "from_state": record.state.value,
            "to_state": state.value,
        },
    )
    await _publish_binding(conn, actor, inquiry_id)
    return await get_inquiry(conn, actor, inquiry_id)


def require_message_approval_flag(value: object) -> bool:
    """``dispatch`` needs an explicit bool (``requires_message_approval(settings)``)."""
    if not isinstance(value, bool):
        raise ValidationFailed("message_approval_required must be passed explicitly")
    return value


__all__ = [
    "LEASE_EXPIRED",
    "REQUALIFIED_AFTER_RESUME",
    "AttemptLease",
    "AttemptRecord",
    "AuthorizationRecord",
    "DispatchResult",
    "InquiryControls",
    "InquiryRecord",
    "OutcomeResult",
    "PreparedBinding",
    "QuotaUsage",
    "ReadinessEvaluation",
    "ReadinessSnapshot",
    "ReconcileResult",
    "SuppressionAdded",
    "SuppressionRow",
    "WindowDecision",
    "active_suppressions",
    "add_suppression",
    "cancel_inquiry",
    "cancel_stale_inquiries",
    "control_view",
    "current_authorization",
    "dispatch",
    "ensure_controls",
    "get_attempt",
    "get_controls",
    "get_inquiry",
    "list_attempts",
    "mark_replied",
    "next_window_at",
    "open_inquiry",
    "pause",
    "prepare_binding",
    "queue",
    "quota_usage",
    "rate_cap_decision",
    "read_readiness_inputs",
    "reap_expired_attempts",
    "rebuild_binding",
    "rebuild_message",
    "reconcile",
    "record_authorization",
    "record_authorization_file",
    "record_outcome",
    "record_readiness",
    "remove_suppression",
    "requalify",
    "reserve",
    "resume",
    "retry",
    "seller_suppression_key",
    "set_limits",
    "set_mode",
]
