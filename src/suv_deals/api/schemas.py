"""Dashboard BFF request/response contracts and the route table (docs/api_contract.md).

- Authentication: ``Authorization: Bearer <Supabase user access token>`` only (no cookies, so no
  CSRF surface). The server verifies the JWT, resolves an *active* membership, derives the role's
  scopes (``domain.actor.ROLE_SCOPES``) and checks the route's scope.
- Every response body is a ``ResponseEnvelope`` (except ``/healthz`` and ``/readyz``); errors are
  ``ApiErrorResponse`` with the spec 21 codes mapped through ``errors.HTTP_STATUS``.
- Mutations reuse the MCP tool input models: each body converts to the tool input with the id
  from the path (``to_tool_input``), so validation, idempotency keys and expected versions are
  identical on both surfaces.
- Query models are the lax (string-parsing) counterparts of the MCP list inputs and convert to
  them before use, so the same constraints apply.

Spec v1.1 section 37 contracts (served by ``api.mail_worker_routes`` and ``api.inquiry_routes``,
which ``api.app.create_app`` includes by default; kept apart from ``ROUTES``, the v1.0 table):

- ``MAIL_WORKER_ROUTES``: the mailbox-worker API under ``/v1/mail-workers`` used by the Windows
  desktop worker (``desktop/outlook-bridge``). Authentication is the worker's revocable
  ``mail:ingest`` bearer credential only; the server derives workspace and mailbox from it (a
  request can never select either). Bodies and responses are the desktop wire models EXACTLY
  (``outlook_bridge.wire`` / ``outlook_bridge.api_client``; parity is contract-tested field by
  field) and are top-level JSON objects with ``schema_version: "1.0"`` (no ``ResponseEnvelope``),
  except the account-report body, which has no ``schema_version`` (as on the wire).
  Errors are ``ApiErrorResponse`` bodies (``error.code``, ``request_id``, ``retry_after_seconds``).
  Where ``wire.py`` and spec 37.8 differ, the desktop wire wins (documented in
  docs/api_contract.md): replies may be acknowledged ``ingest_status: "quarantined"``; the reply
  body carries the optional worker extensions ``message_type``, ``correlation_status``,
  ``correlation_reasons`` and ``withheld_sensitive_attachments``; ``detected_language`` is one of
  de/it/fr/en or null; binding items carry ``state`` (active/suppressed/uncertain/tombstoned) and
  a binding page carries ``has_more``.
- ``V11_DASHBOARD_ROUTES``: inquiry/reply read models and the inquiry control (pause via the same
  rules as the ``seller_inquiries_pause`` MCP tool; resume is owner-only, dashboard-only), the
  mail-worker health and coverage-gap views, the lifecycle/lag views and the 15-day evaluation.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.canary import (
    CanaryClaimDecision,
    CanaryClaimRequest,
    CanaryIntent,
    CanaryIntentBatch,
    CanaryReplyReport,
    CanaryReport,
)
from suv_deals.domain.enums import (
    EmailProviderKind,
    InquiryState,
    ProfileKey,
    ReplyMessageType,
    ReviewOutcome,
    Scope,
)
from suv_deals.domain.evaluation import EVALUATION_WINDOW_DAYS, EvaluationReport
from suv_deals.domain.replies import (
    MAX_REQUEST_BYTES,
    InquiryBinding,
    InquiryBindingState,
    ReplyIngestRequest,
    normalize_message_id,
)
from suv_deals.errors import HTTP_STATUS, AppError, ErrorCode, ValidationFailed
from suv_deals.integrations.email_providers.outlook_local import (
    OutlookAccountReport,
    OutlookHeartbeat,
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
)
from suv_deals.mcp.schemas import (
    AwareDatetime,
    CaseVersion,
    ClaimToken,
    Cursor,
    DealsAddNoteInput,
    DealsGetCandidateInput,
    DealsGetComparablesInput,
    DealsListCandidatesInput,
    DealsRequestRecheckInput,
    EvidenceIds,
    Id,
    IdempotencyKey,
    ListingRevisionNumber,
    MissingInformation,
    ModelRunId,
    NoteText,
    Reason,
    ReasonCodes,
    ReviewsClaimInput,
    ReviewsListPendingInput,
    ReviewsReleaseInput,
    ReviewsSubmitInput,
    SellerInquiriesPauseInput,
    SourcesPauseInput,
    SourceVersion,
    SubmitRuleError,
    SummaryText,
    ToolInput,
    ValuationIdRef,
    validation_error_fields,
)
from suv_deals.views.candidates import CandidateDetail, CandidateListView
from suv_deals.views.common import (
    SCHEMA_VERSION,
    ErrorPayload,
    ResponseEnvelope,
    UtcDatetime,
    ViewModel,
    envelope_model_for,
    is_valid_request_id,
)
from suv_deals.views.comparables import ComparableSetView
from suv_deals.views.inquiries import (
    CanaryEvidenceView,
    InquiryControlView,
    InquiryListView,
    InquiryPauseResult,
    InquiryResumeResult,
    InquiryView,
    ReplyListView,
    ReplyView,
)
from suv_deals.views.jsonschema import model_schema
from suv_deals.views.lifecycle import CoverageLagsView, ListingLifecycleView
from suv_deals.views.mail_workers import MailCoverageGapListView, MailWorkerHealthView
from suv_deals.views.notes import NoteView, RecheckRequestResult
from suv_deals.views.operations import (
    LivenessView,
    MeView,
    OutboxPage,
    OverviewView,
    ReadinessView,
    SettingsView,
    SourceListView,
    SourcePauseResult,
)
from suv_deals.views.reviews import (
    ClaimResult,
    ReleaseResultView,
    ReviewCaseView,
    ReviewDecisionView,
    ReviewQueuePage,
)
from suv_deals.views.valuations import ValuationView

#: Path parameter type for every ``{..._id}`` segment: canonical UUID string.
PathId = Id

_LaxLimit = Annotated[int, Field(ge=1, le=100)]


def _to_input[InputT: ToolInput](model: type[InputT], data: Mapping[str, Any], name: str) -> InputT:
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        fields = validation_error_fields(exc, root="request", model=model)
        raise ValidationFailed(f"Invalid {name} request", details={"fields": fields}) from None


# --------------------------------------------------------------------------- query models


class ApiQuery(BaseModel):
    """Query-string parameters (strings parsed into types); unknown parameters are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CandidateListQuery(ApiQuery):
    """``GET /api/candidates``. ``include_screening_rejected`` (dashboard only, default ``false``)
    also lists the screening-rejected observations kept for audit (spec 11): it is not part of the
    spec 21 ``deals_list_candidates`` input (the MCP tool never lists them) and is bound into the
    cursor's filter hash, so a cursor of one setting is refused for the other."""

    cursor: Cursor = None
    limit: _LaxLimit = 25
    profile: ProfileKey | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    status: Literal["pending", "needs_information", "watch", "shortlisted", "rejected"] | None = None
    changed_since: AwareDatetime | None = None  # RFC 3339 with an offset, exactly as for MCP
    include_screening_rejected: bool = False

    def to_tool_input(self) -> DealsListCandidatesInput:
        data = self.model_dump(exclude_none=True, exclude={"include_screening_rejected"})
        return _to_input(DealsListCandidatesInput, data, "candidate list")


class CandidateDetailQuery(ApiQuery):
    revision: Annotated[int, Field(ge=1)] | None = None

    def to_tool_input(self, listing_id: UUID) -> DealsGetCandidateInput:
        data = {"listing_id": listing_id, **self.model_dump(exclude_none=True)}
        return _to_input(DealsGetCandidateInput, data, "candidate")


class ComparablesQuery(ApiQuery):
    include_excluded: bool = False
    cursor: Cursor = None
    limit: _LaxLimit = 25

    def to_tool_input(self, comparable_set_id: UUID) -> DealsGetComparablesInput:
        data = {"comparable_set_id": comparable_set_id, **self.model_dump()}
        return _to_input(DealsGetComparablesInput, data, "comparables")


class ReviewQueueQuery(ApiQuery):
    cursor: Cursor = None
    limit: _LaxLimit = 25
    include_needs_information: bool = True

    def to_tool_input(self) -> ReviewsListPendingInput:
        return _to_input(ReviewsListPendingInput, self.model_dump(), "review queue")


OutboxAttentionState = Literal["uncertain", "blocked", "dead_letter", "retry_wait"]


class OutboxQuery(ApiQuery):
    """Failed or uncertain deliveries only; delivered/pending rows are not listed here."""

    cursor: Cursor = None
    limit: _LaxLimit = 25
    state: OutboxAttentionState | None = None

    def filters(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"cursor", "limit"}, exclude_none=True)


# --------------------------------------------------------------------------- mutation bodies


class ClaimRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/claim`` (= ``reviews_claim`` minus the path id)."""

    expected_version: CaseVersion
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsClaimInput:
        return _to_input(ReviewsClaimInput, {"case_id": case_id, **self.model_dump()}, "claim")


class ReleaseRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/release`` (= ``reviews_release`` minus the path id)."""

    claim_token: ClaimToken = Field(repr=False)
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsReleaseInput:
        return _to_input(ReviewsReleaseInput, {"case_id": case_id, **self.model_dump()}, "release")


class SubmitReviewRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/submit`` (= ``reviews_submit`` minus the path id).

    The domain submission rules (reason-code format, unique ids, summary hygiene) run in
    ``to_tool_input``, exactly as for the MCP tool.
    """

    claim_token: ClaimToken = Field(repr=False)
    expected_version: CaseVersion
    listing_revision: ListingRevisionNumber
    valuation_id: ValuationIdRef = None
    outcome: ReviewOutcome
    reason_codes: ReasonCodes
    summary: SummaryText
    evidence_ids: EvidenceIds
    missing_information: MissingInformation = ()
    model_run_id: ModelRunId = None
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsSubmitInput:
        return _to_input(ReviewsSubmitInput, {"case_id": case_id, **self.model_dump()}, "submit")


class AddNoteRequest(ToolInput):
    """Body of ``POST /api/listings/{listing_id}/notes`` (= ``deals_add_note`` minus the path id)."""

    note: NoteText
    idempotency_key: IdempotencyKey

    def to_tool_input(self, listing_id: UUID) -> DealsAddNoteInput:
        return _to_input(DealsAddNoteInput, {"listing_id": listing_id, **self.model_dump()}, "note")


class RecheckRequest(ToolInput):
    """Body of ``POST /api/listings/{listing_id}/recheck`` (= ``deals_request_recheck`` minus the path id)."""

    reason: Reason
    idempotency_key: IdempotencyKey

    def to_tool_input(self, listing_id: UUID) -> DealsRequestRecheckInput:
        return _to_input(DealsRequestRecheckInput, {"listing_id": listing_id, **self.model_dump()}, "recheck")


class PauseSourceRequest(ToolInput):
    """Body of ``POST /api/sources/{source_id}/pause`` (= ``sources_pause`` minus the path id)."""

    expected_version: SourceVersion
    reason: Reason
    idempotency_key: IdempotencyKey

    def to_tool_input(self, source_id: UUID) -> SourcesPauseInput:
        return _to_input(SourcesPauseInput, {"source_id": source_id, **self.model_dump()}, "pause")


# --------------------------------------------------------------------------- spec 37 dashboard


class InquiryListQuery(ApiQuery):
    """``GET /api/inquiries``: a frozen snapshot page, optionally one state, only uncertain sends
    or only inquiries needing attention (uncertain, held for facts, suppressed, failed, stuck
    sending)."""

    cursor: Cursor = None
    limit: _LaxLimit = 25
    state: InquiryState | None = None
    uncertain_only: bool = False
    attention_only: bool = False

    def filters(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"cursor", "limit"}, exclude_none=True)


class ReplyListQuery(ApiQuery):
    """``GET /api/replies``: keyset page, optionally of one inquiry or only quarantined replies."""

    cursor: Cursor = None
    limit: _LaxLimit = 25
    inquiry_id: Id | None = None
    quarantined_only: bool = False

    def filters(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"cursor", "limit"}, exclude_none=True)


class InquiryPauseRequest(ToolInput):
    """Body of ``POST /api/inquiry-control/pause`` (= ``seller_inquiries_pause``)."""

    expected_version: Annotated[int, Field(ge=1, strict=True)]
    reason: Reason
    idempotency_key: IdempotencyKey

    def to_tool_input(self) -> SellerInquiriesPauseInput:
        return _to_input(SellerInquiriesPauseInput, self.model_dump(), "inquiry pause")


#: Upper bound of ``InquiryResumeRequest.expected_removable_suppressions`` (the repository lists
#: at most 5,000 active suppressions).
MAX_REMOVABLE_SUPPRESSIONS: Final = 5000
_RemovableCount = Annotated[int, Field(ge=0, le=MAX_REMOVABLE_SUPPRESSIONS, strict=True)]


class InquiryResumeRequest(ToolInput):
    """Body of ``POST /api/inquiry-control/resume`` (owner only; never an MCP tool).

    ``remove_suppressions``: also remove (audited, one audit event per suppression) the active
    ``kill_switch`` suppressions and, while the current standing authorization is effective and
    unrevoked, the ``authorization_revoked`` ones. Other suppressions (opt-out, bounce, complaint,
    sender revoked, ...) are never removed here. ``expected_removable_suppressions`` closes the
    time-of-check/time-of-use gap between the control view the owner read and the resume: the
    count is compared under the controls lock (kill-switch and authorization-revoked suppressions
    are added only under that lock) and a mismatch refuses the whole resume. It is REQUIRED
    whenever ``remove_suppressions`` is true (``422 VALIDATION_ERROR`` naming the field
    otherwise), so a removal only ever removes the set the owner saw; the CLI
    (``--expected-suppressions``) and the dashboard (the count the owner ticked) send it. Without
    a removal it names nothing and is not checked.
    """

    model_config = ConfigDict(
        **ToolInput.model_config,
        json_schema_extra={
            "if": {
                "properties": {"remove_suppressions": {"const": True}},
                "required": ["remove_suppressions"],
            },
            "then": {
                "properties": {"expected_removable_suppressions": {"type": "integer"}},
                "required": ["expected_removable_suppressions"],
            },
        },
    )

    expected_version: Annotated[int, Field(ge=1, strict=True)]
    reason: Reason
    idempotency_key: IdempotencyKey
    remove_suppressions: bool = False
    expected_removable_suppressions: _RemovableCount | None = Field(
        default=None,
        description=(
            "Required with remove_suppressions: the removable_suppressions count the owner saw in"
            " GET /api/inquiry-control. When the current count differs, the resume is refused"
            " (409 VERSION_CONFLICT, details.reason = suppressions_changed) and nothing changes,"
            " so only the suppressions the owner saw are ever removed."
        ),
    )

    @model_validator(mode="after")
    def _count_with_removal(self) -> InquiryResumeRequest:
        if self.remove_suppressions and self.expected_removable_suppressions is None:
            raise SubmitRuleError(("expected_removable_suppressions",))
        return self


class MailWorkerHealthQuery(ApiQuery):
    """``GET /api/mail-workers/health`` and ``/coverage-gaps``: revoked workers only on request."""

    include_revoked: bool = False


class EvaluationQuery(ApiQuery):
    """``GET /api/evaluation``: the 15-day quality evaluation (the window length is fixed; ``days``
    may be given as ``15`` and anything else is refused)."""

    days: Annotated[int, Field(ge=EVALUATION_WINDOW_DAYS, le=EVALUATION_WINDOW_DAYS)] = EVALUATION_WINDOW_DAYS


# --------------------------------------------------------------------------- spec 37.8 mail workers

MAIL_WORKER_PREFIX: Final = "/v1/mail-workers"
MAIL_WORKER_SCHEMA_VERSION: Final = "1.0"
#: Whole-request ceiling of every mail-worker POST (spec 37.8: 128 KiB); wire it with
#: ``ApiOptions(prefix_body_limits={MAIL_WORKER_PREFIX: MAIL_WORKER_BODY_LIMIT})``.
MAIL_WORKER_BODY_LIMIT: Final = MAX_REQUEST_BYTES
MAIL_WORKER_MAX_BINDINGS_PAGE: Final = 100
MAIL_WORKER_MAX_INTENTS_PAGE: Final = 50
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
REQUEST_ID_HEADER: Final = "X-Request-Id"
_WORKER_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]+$"
_CURSOR_PATTERN: Final = r"^[\x21-\x7e]{1,1024}$"
_HEX64_PATTERN: Final = r"^[0-9a-f]{64}$"
_WIRE: Final = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


class MailWorkerBindingsQuery(ApiQuery):
    """``GET /v1/mail-workers/inquiry-bindings?cursor=<opaque>&limit=<1..100>``."""

    cursor: Annotated[str, Field(pattern=_CURSOR_PATTERN)] | None = None
    limit: Annotated[int, Field(ge=1, le=MAIL_WORKER_MAX_BINDINGS_PAGE)] = MAIL_WORKER_MAX_BINDINGS_PAGE


class MailWorkerBindingItem(BaseModel):
    """One binding change (== ``api_client.BindingSyncItem``). A tombstone carries identity,
    version and state only; any other item satisfies the shared ``InquiryBinding`` rules."""

    model_config = _WIRE

    inquiry_id: UUID
    binding_version: int = Field(ge=1)
    mailbox_binding_id: UUID
    state: InquiryBindingState
    provider: EmailProviderKind | None = None
    outbound_message_ids: tuple[str, ...] = ()
    send_intent_message_ids: tuple[str, ...] = ()
    provider_message_ids: tuple[str, ...] = ()
    provider_thread_ids: tuple[str, ...] = ()
    verified_seller_aliases: tuple[str, ...] = ()
    listing_references: tuple[str, ...] = ()
    listing_urls: tuple[str, ...] = ()
    listing_id: UUID | None = None
    vehicle_cluster_id: UUID | None = None
    is_canary: bool = False

    @model_validator(mode="after")
    def _shape(self) -> MailWorkerBindingItem:
        if self.state == InquiryBindingState.TOMBSTONED:
            payload = (
                self.outbound_message_ids,
                self.send_intent_message_ids,
                self.provider_message_ids,
                self.provider_thread_ids,
                self.verified_seller_aliases,
                self.listing_references,
                self.listing_urls,
            )
            if any(payload) or self.listing_id or self.vehicle_cluster_id or self.provider is not None:
                raise ValueError("a tombstone carries no binding payload")
        elif self.provider is None:
            raise ValueError("an active binding names its sending provider")
        else:
            InquiryBinding.model_validate(self.model_dump())
        return self


class MailWorkerBindingPage(BaseModel):
    """Response of ``GET /v1/mail-workers/inquiry-bindings`` (== ``api_client.BindingPage``).

    ``next_cursor`` is the opaque position after this complete page (the worker persists page and
    cursor atomically); ``has_more`` says whether another page is ready now. Items are at most
    ``limit`` (<= 100) although the client accepts up to 1000.
    """

    model_config = _WIRE

    schema_version: Literal["1.0"]
    items: tuple[MailWorkerBindingItem, ...] = Field(default=(), max_length=1000)
    next_cursor: str | None = None
    has_more: bool = False

    @field_validator("next_cursor")
    @classmethod
    def _cursor(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(_CURSOR_PATTERN, value):
            raise ValueError("cursor must be an opaque printable token")
        return value


MAX_RETURNED_MESSAGE_IDS: Final = 20
DELIVERY_REPORT_TYPES: Final = frozenset({ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE})


class MailWorkerReplyRequest(ReplyIngestRequest):
    """Body of ``POST /v1/mail-workers/replies`` (spec 37.8 v1.0 + the worker's optional
    extensions; == ``outlook_bridge.wire.ReplyUpload``, a ``domain.replies.ReplyIngestRequest``
    plus ``returned_message_ids``).

    Headers: ``Idempotency-Key`` (8-128 printable ASCII) is REQUIRED; the server checks it AND the
    stable source identity (``ReplyIngestRequest.dedup_key``), never one alone. Limits: request
    <= 128 KiB, ``sanitized_body_text`` <= 64 KiB, ``subject`` <= 512 characters, <= 20 attachment
    metadata entries (safe filename, MIME type, byte count, SHA-256, opaque local ref; no URLs,
    paths or bytes).

    ``returned_message_ids`` (bounce/delivery notice only, at most 20): the returned original's
    Message-IDs, which the worker reads before its sanitiser removes the quoted original. The
    server uses them only when its own classification of the uploaded fields is a delivery report
    too; they are not part of the immutable source fingerprint.
    """

    returned_message_ids: tuple[str, ...] = Field(default=(), max_length=MAX_RETURNED_MESSAGE_IDS)

    @field_validator("returned_message_ids")
    @classmethod
    def _returned(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            normalized = normalize_message_id(item)
            if normalized is None:
                raise ValueError("returned_message_ids must be valid RFC Message-IDs")
            if normalized not in result:
                result.append(normalized)
        return tuple(result)

    @model_validator(mode="after")
    def _delivery_reports_only(self) -> MailWorkerReplyRequest:
        if self.returned_message_ids and self.message_type not in DELIVERY_REPORT_TYPES:
            raise ValueError("returned_message_ids belong to a bounce or delivery notice only")
        return self


class MailWorkerReplyAck(BaseModel):
    """Successful ``POST /v1/mail-workers/replies`` answer (== ``api_client.IngestAck``).

    The same key/message with the same immutable content returns the existing ``reply_id`` with
    ``duplicate: true`` (also after a folder move); only then may the worker advance its
    acknowledged checkpoint.
    """

    model_config = _WIRE

    schema_version: Literal["1.0"]
    reply_id: UUID
    inquiry_id: UUID
    ingest_status: Literal["stored", "quarantined"]
    duplicate: bool
    request_id: str = Field(min_length=1, max_length=200)
    ingested_at: datetime

    @field_validator("ingested_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class MailWorkerSendIntentsQuery(ApiQuery):
    """``GET /v1/mail-workers/send-intents?limit=<1..50>``."""

    limit: Annotated[int, Field(ge=1, le=MAIL_WORKER_MAX_INTENTS_PAGE)] = 10


with warnings.catch_warnings():
    # The wire field ``expired`` deliberately replaces ``OutlookSendIntent.expired(now)`` on this
    # transport model (the server never calls the method on it).
    warnings.filterwarnings("ignore", message='Field name "expired"', category=UserWarning)

    class MailWorkerSendIntent(OutlookSendIntent):
        """One ``outlook_local`` send intent (== ``wire.WorkerSendIntent``; the length limits and the
        ``inquiry_ref == "inquiry-<inquiry_id>"`` check live on ``OutlookSendIntent`` itself).

        ``expired``: the intent's validity ended before any worker claimed it (the backend reaped
        the attempt as uncertain and no report exists). The worker refuses it as
        ``intent_expired`` (never sends it), which lets the backend prove non-submission and
        reconcile the inquiry.
        """

        expired: bool = False  # type: ignore[assignment]


class MailWorkerSendIntentBatch(BaseModel):
    """Response of ``GET /v1/mail-workers/send-intents`` (== ``wire.SendIntentBatch``)."""

    model_config = _WIRE

    schema_version: Literal["1.0"] = MAIL_WORKER_SCHEMA_VERSION
    intents: tuple[MailWorkerSendIntent, ...] = Field(default=(), max_length=MAIL_WORKER_MAX_INTENTS_PAGE)
    kill_switch_active: bool


class MailWorkerClaimRequest(BaseModel):
    """Body of ``POST /v1/mail-workers/send-intents/{intent_id}/claim`` (``api_client``).

    Every claim has its own ``claim_attempt_id`` (and ``Idempotency-Key: claim-<intent>-<attempt>``)
    so an idempotency layer can never replay an earlier ``proceed: true``: a claim is always
    evaluated fresh (kill switch, suppression, cancellation, binding version)."""

    model_config = _WIRE

    schema_version: Literal["1.0"]
    intent_id: UUID
    claim_attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID_PATTERN)


class MailWorkerClaimDecision(BaseModel):
    """Response of the claim (== ``wire.ClaimDecision``)."""

    model_config = _WIRE

    schema_version: Literal["1.0"] = MAIL_WORKER_SCHEMA_VERSION
    intent_id: UUID
    proceed: bool
    refusal_reason: OutlookRefusalReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> MailWorkerClaimDecision:
        if self.proceed and self.refusal_reason is not None:
            raise ValueError("a proceeding claim carries no refusal reason")
        return self


class MailWorkerSendReport(OutlookSendReport):
    """Body of ``POST /v1/mail-workers/send-intents/{intent_id}/report`` (== ``wire.WorkerSendReport``;
    ``Idempotency-Key: report-<intent>-<state>``)."""


class MailWorkerCanaryIntent(CanaryIntent):
    """One published ``outlook_local`` activation canary (== ``wire.WorkerCanaryIntent``; F3, wave
    D2): the fixed canary rendering, the sender identity and the owner-controlled target as its
    SHA-256 only (the worker sends to the address configured on the owner's machine)."""


class MailWorkerCanaryIntentBatch(CanaryIntentBatch):
    """Response of ``GET /v1/mail-workers/canary-intents`` (== ``wire.CanaryIntentBatch``)."""

    intents: tuple[MailWorkerCanaryIntent, ...] = Field(default=(), max_length=10)


class MailWorkerCanaryClaimRequest(CanaryClaimRequest):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/claim`` (never replayed)."""


class MailWorkerCanaryClaimDecision(CanaryClaimDecision):
    """Response of the canary claim (== ``wire.CanaryClaimDecision``)."""


class MailWorkerCanaryReport(CanaryReport):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/report`` (no address)."""


class MailWorkerCanaryReply(CanaryReplyReport):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/reply`` (headers only; the
    reply's sender as its target hash)."""


class MailWorkerAccepted(BaseModel):
    """Response of the report and account-report routes."""

    model_config = _WIRE

    schema_version: Literal["1.0"] = MAIL_WORKER_SCHEMA_VERSION
    accepted: Literal[True] = True


class MailWorkerCheckpointReport(BaseModel):
    """One mailbox/store/folder checkpoint (hashed identities; == ``wire.CheckpointReport``)."""

    model_config = _WIRE

    store_id_hash: str = Field(pattern=_HEX64_PATTERN)
    folder_id_hash: str = Field(pattern=_HEX64_PATTERN)
    folder_role: Literal["inbox", "sent_items", "outbox", "junk", "rule_target", "other"]
    overlap_watermark: datetime | None = None
    acknowledged_watermark: datetime | None = None
    last_complete_scan_at: datetime | None = None
    last_scan_started_at: datetime | None = None
    backlog_count: int = Field(default=0, ge=0)
    backlog_oldest_at: datetime | None = None
    gap_reasons: tuple[str, ...] = Field(default=(), max_length=30)


class MailWorkerGapReport(BaseModel):
    """A monitored coverage gap (== ``wire.GapReport``); never claimed coverage. A gap that ends
    before it starts is refused (``422``): it could never be shown, so it would be hidden."""

    model_config = _WIRE

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    started_at: datetime
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> MailWorkerGapReport:
        if self.ended_at is not None and ensure_utc(self.ended_at) < ensure_utc(self.started_at):
            raise ValueError("a coverage gap cannot end before it starts")
        return self


class MailWorkerHeartbeatRequest(BaseModel):
    """Body of ``POST /v1/mail-workers/heartbeat`` (== ``wire.HeartbeatEnvelope``)."""

    model_config = _WIRE

    schema_version: Literal["1.0"] = MAIL_WORKER_SCHEMA_VERSION
    heartbeat: OutlookHeartbeat
    last_successful_reconciliation_at: datetime | None = None
    mailbox_last_sync_at: datetime | None = None
    backlog_count: int = Field(default=0, ge=0)
    backlog_oldest_age_seconds: int | None = Field(default=None, ge=0)
    unresolved_matching_gaps: int = Field(default=0, ge=0)
    checkpoints: tuple[MailWorkerCheckpointReport, ...] = Field(default=(), max_length=20)
    gaps: tuple[MailWorkerGapReport, ...] = Field(default=(), max_length=50)


class MailWorkerHeartbeatAck(BaseModel):
    """Response of the heartbeat (== ``wire.HeartbeatAck``): ``downstream`` carries Slack/MCP
    health the worker cannot observe (short codes only, at most 10 entries)."""

    model_config = _WIRE

    schema_version: Literal["1.0"] = MAIL_WORKER_SCHEMA_VERSION
    received_at: datetime | None = None
    downstream: dict[str, str] = Field(default_factory=dict, max_length=10)

    @field_validator("downstream")
    @classmethod
    def _codes(cls, value: dict[str, str]) -> dict[str, str]:
        code = r"^[A-Za-z0-9_.:-]{1,32}$"
        for key, item in value.items():
            if not (re.fullmatch(code, key) and re.fullmatch(code, item)):
                raise ValueError("downstream health is short codes only")
        return value


class MailWorkerAccountReport(OutlookAccountReport):
    """Body of ``POST /v1/mail-workers/account-report`` (== ``wire.WorkerAccountReport``; no
    credentials; ``security_settings_unchanged`` is ``Literal[True]`` on ``OutlookAccountReport``:
    the worker never weakens Outlook security)."""


# --------------------------------------------------------------------------- errors


class ApiErrorResponse(ViewModel):
    """Error body for every failed ``/api`` request (same codes as MCP tool errors)."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(pattern=r"^[\x21-\x7e]{1,200}$")
    as_of: UtcDatetime
    error: ErrorPayload


def api_error(error: AppError, *, request_id: str, as_of: datetime) -> tuple[int, ApiErrorResponse]:
    """HTTP status (``errors.HTTP_STATUS``) and safe body for an ``AppError``.

    Building the error body never fails: a malformed ``request_id`` (it may come from a client
    header) is replaced by a fresh server-generated id.
    """
    if not is_valid_request_id(request_id):
        request_id = f"req-{uuid4().hex}"
    body = ApiErrorResponse(
        request_id=request_id,
        as_of=as_of,
        error=ErrorPayload.from_app_error(error, correlation_id=request_id),
    )
    return HTTP_STATUS[error.code], body


# --------------------------------------------------------------------------- routes

Auth = Literal["none", "user_jwt", "mail_worker"]
RequestLocation = Literal["query", "body"]
#: ``Idempotency-Key`` request header: ``required`` (missing -> 400), ``optional`` (when sent it
#: must equal the body's ``idempotency_key``), ``None`` (not used by the route).
IdempotencyHeader = Literal["required", "optional"]

COMMON_ERRORS: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.RATE_LIMITED,
    ErrorCode.DEPENDENCY_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR,
)


@dataclass(frozen=True, slots=True)
class ApiRoute:
    method: Literal["GET", "POST"]
    path: str
    auth: Auth
    scope: Scope | None
    response_model: type[BaseModel]
    data_model: type[BaseModel] | None
    request_model: type[BaseModel] | None = None
    request_location: RequestLocation | None = None
    success_status: int = 200
    errors: tuple[ErrorCode, ...] = ()
    paginated: bool = False
    mcp_tool: str | None = None
    summary: str = ""
    idempotency_header: IdempotencyHeader | None = None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def path_params(self) -> tuple[str, ...]:
        return tuple(
            part[1:-1] for part in self.path.split("/") if part.startswith("{") and part.endswith("}")
        )


def _r(
    method: Literal["GET", "POST"],
    path: str,
    data: type[BaseModel],
    *,
    scope: Scope | None,
    summary: str,
    request: type[BaseModel] | None = None,
    location: RequestLocation | None = None,
    status: int = 200,
    errors: tuple[ErrorCode, ...] = (),
    paginated: bool = False,
    tool: str | None = None,
    idempotency_header: IdempotencyHeader | None = None,
) -> ApiRoute:
    return ApiRoute(
        method=method,
        path=path,
        auth="user_jwt",
        scope=scope,
        response_model=envelope_model_for(data),
        data_model=data,
        request_model=request,
        request_location=location,
        success_status=status,
        errors=(*COMMON_ERRORS, *errors),
        paginated=paginated,
        mcp_tool=tool,
        summary=summary,
        idempotency_header=idempotency_header,
    )


_NF = (ErrorCode.NOT_FOUND,)
_WRITE = (ErrorCode.NOT_FOUND, ErrorCode.IDEMPOTENCY_CONFLICT)

ROUTES: Final[tuple[ApiRoute, ...]] = (
    ApiRoute(
        method="GET",
        path="/healthz",
        auth="none",
        scope=None,
        response_model=LivenessView,
        data_model=None,
        errors=(),
        summary="Process liveness only (no dependencies checked).",
    ),
    ApiRoute(
        method="GET",
        path="/readyz",
        auth="none",
        scope=None,
        response_model=ReadinessView,
        data_model=None,
        errors=(),
        summary="Readiness: database, schema compatibility and critical configuration; 503 when not ready.",
    ),
    _r(
        "GET",
        "/api/me",
        MeView,
        scope=None,
        summary="Authenticated principal, workspace, role, scopes, memberships.",
    ),
    _r("GET", "/api/overview", OverviewView, scope=Scope.DEALS_READ, summary="Overview screen."),
    _r(
        "GET",
        "/api/candidates",
        CandidateListView,
        scope=Scope.DEALS_READ,
        summary="Candidate queue with filters and keyset pagination.",
        request=CandidateListQuery,
        location="query",
        paginated=True,
        tool="deals_list_candidates",
    ),
    _r(
        "GET",
        "/api/candidates/{listing_id}",
        CandidateDetail,
        scope=Scope.DEALS_READ,
        summary="Candidate detail at the current or requested revision.",
        request=CandidateDetailQuery,
        location="query",
        errors=_NF,
        tool="deals_get_candidate",
    ),
    _r(
        "GET",
        "/api/comparables/{set_id}",
        ComparableSetView,
        scope=Scope.DEALS_READ,
        summary="Comparable set with paginated members.",
        request=ComparablesQuery,
        location="query",
        errors=_NF,
        paginated=True,
        tool="deals_get_comparables",
    ),
    _r(
        "GET",
        "/api/valuations/{valuation_id}",
        ValuationView,
        scope=Scope.DEALS_READ,
        summary="Versioned valuation breakdown.",
        errors=_NF,
        tool="deals_get_valuation",
    ),
    _r(
        "GET",
        "/api/reviews",
        ReviewQueuePage,
        scope=Scope.REVIEWS_READ,
        summary="Pending review queue (frozen snapshot pagination).",
        request=ReviewQueueQuery,
        location="query",
        paginated=True,
        tool="reviews_list_pending",
    ),
    _r(
        "GET",
        "/api/reviews/{case_id}",
        ReviewCaseView,
        scope=Scope.REVIEWS_READ,
        summary="One review case with candidate, valuation reference and decision history.",
        errors=_NF,
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/claim",
        ClaimResult,
        scope=Scope.REVIEWS_WRITE,
        summary="Claim the current case version.",
        request=ClaimRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT, ErrorCode.ALREADY_CLAIMED),
        tool="reviews_claim",
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/release",
        ReleaseResultView,
        scope=Scope.REVIEWS_WRITE,
        summary="Release the caller's own claim (idempotent).",
        request=ReleaseRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.CLAIM_EXPIRED),
        tool="reviews_release",
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/submit",
        ReviewDecisionView,
        scope=Scope.REVIEWS_WRITE,
        summary="Submit a decision against exact versions.",
        request=SubmitReviewRequest,
        location="body",
        status=201,
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT, ErrorCode.ALREADY_CLAIMED, ErrorCode.CLAIM_EXPIRED),
        tool="reviews_submit",
    ),
    _r(
        "POST",
        "/api/listings/{listing_id}/notes",
        NoteView,
        scope=Scope.NOTES_WRITE,
        summary="Append a private labelled note.",
        request=AddNoteRequest,
        location="body",
        status=201,
        errors=_WRITE,
        tool="deals_add_note",
    ),
    _r(
        "POST",
        "/api/listings/{listing_id}/recheck",
        RecheckRequestResult,
        scope=Scope.RECHECKS_REQUEST,
        summary="Queue a bounded recheck; returns the job id.",
        request=RecheckRequest,
        location="body",
        status=202,
        errors=(*_WRITE, ErrorCode.SOURCE_PAUSED, ErrorCode.ACCESS_BLOCKED),
        tool="deals_request_recheck",
    ),
    _r("GET", "/api/sources", SourceListView, scope=Scope.DEALS_READ, summary="Source status screen."),
    _r(
        "POST",
        "/api/sources/{source_id}/pause",
        SourcePauseResult,
        scope=Scope.SOURCES_PAUSE,
        summary="Pause one source with a reason (never resumes).",
        request=PauseSourceRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT),
        tool="sources_pause",
    ),
    _r("GET", "/api/settings", SettingsView, scope=Scope.DEALS_READ, summary="Settings screen (read-only)."),
    _r(
        "GET",
        "/api/outbox",
        OutboxPage,
        scope=Scope.REVIEWS_READ,
        summary="Failed, blocked and uncertain deliveries (no payloads).",
        request=OutboxQuery,
        location="query",
        paginated=True,
    ),
)

ROUTE_INDEX: Final[Mapping[str, ApiRoute]] = MappingProxyType({route.key: route for route in ROUTES})

# --------------------------------------------------------------------------- spec 37 route tables

_MW_COMMON: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.RATE_LIMITED,
    ErrorCode.DEPENDENCY_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR,
)


def _mw(
    method: Literal["GET", "POST"],
    path: str,
    response: type[BaseModel],
    *,
    summary: str,
    request: type[BaseModel] | None = None,
    location: RequestLocation | None = None,
    errors: tuple[ErrorCode, ...] = (),
    paginated: bool = False,
    idempotency_header: IdempotencyHeader | None = None,
) -> ApiRoute:
    return ApiRoute(
        method=method,
        path=MAIL_WORKER_PREFIX + path,
        auth="mail_worker",
        scope=Scope.MAIL_INGEST,
        response_model=response,
        data_model=None,
        request_model=request,
        request_location=location,
        errors=(*_MW_COMMON, *errors),
        paginated=paginated,
        summary=summary,
        idempotency_header=idempotency_header,
    )


#: The mailbox-worker API (spec 37.8; implemented by the API package). Auth: the worker's
#: ``mail:ingest`` bearer credential; workspace and mailbox come from the credential only.
MAIL_WORKER_ROUTES: Final[tuple[ApiRoute, ...]] = (
    _mw(
        "GET",
        "/inquiry-bindings",
        MailWorkerBindingPage,
        summary="Binding changes for the worker's own mailbox, tombstones included (opaque cursor).",
        request=MailWorkerBindingsQuery,
        location="query",
        paginated=True,
    ),
    _mw(
        "POST",
        "/replies",
        MailWorkerReplyAck,
        summary="Store one inquiry-correlated reply (Idempotency-Key + stable source identity).",
        request=MailWorkerReplyRequest,
        location="body",
        errors=(ErrorCode.VERSION_CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "GET",
        "/send-intents",
        MailWorkerSendIntentBatch,
        summary="Pending outlook_local send intents of the worker's mailbox plus the kill switch state.",
        request=MailWorkerSendIntentsQuery,
        location="query",
    ),
    _mw(
        "POST",
        "/send-intents/{intent_id}/claim",
        MailWorkerClaimDecision,
        summary="Fresh server revalidation immediately before .Send (never replayed).",
        request=MailWorkerClaimRequest,
        location="body",
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VERSION_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "POST",
        "/send-intents/{intent_id}/report",
        MailWorkerAccepted,
        summary="Submission/evidence report of one intent (uncertain outcomes stay uncertain).",
        request=MailWorkerSendReport,
        location="body",
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VERSION_CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "GET",
        "/canary-intents",
        MailWorkerCanaryIntentBatch,
        summary="Published outlook_local activation canaries of the worker's mailbox (F3).",
    ),
    _mw(
        "POST",
        "/canary-intents/{canary_id}/claim",
        MailWorkerCanaryClaimDecision,
        summary="Fresh server revalidation immediately before the canary's .Send (never replayed).",
        request=MailWorkerCanaryClaimRequest,
        location="body",
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VERSION_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "POST",
        "/canary-intents/{canary_id}/report",
        MailWorkerAccepted,
        summary="Submission/Sent Items evidence of one activation canary.",
        request=MailWorkerCanaryReport,
        location="body",
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VERSION_CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "POST",
        "/canary-intents/{canary_id}/reply",
        MailWorkerAccepted,
        summary="The owner's correlated test reply to an activation canary (headers only).",
        request=MailWorkerCanaryReply,
        location="body",
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VERSION_CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT),
        idempotency_header="required",
    ),
    _mw(
        "POST",
        "/heartbeat",
        MailWorkerHeartbeatAck,
        summary="Worker, Outlook and mailbox health, checkpoints and coverage gaps.",
        request=MailWorkerHeartbeatRequest,
        location="body",
    ),
    _mw(
        "POST",
        "/account-report",
        MailWorkerAccepted,
        summary="Classic-Outlook account verification report (no credentials).",
        request=MailWorkerAccountReport,
        location="body",
        errors=(ErrorCode.VERSION_CONFLICT,),
    ),
)

#: Dashboard inquiry routes (spec 37.8, 23; implemented by the API package).
V11_DASHBOARD_ROUTES: Final[tuple[ApiRoute, ...]] = (
    _r(
        "GET",
        "/api/inquiries",
        InquiryListView,
        scope=Scope.INQUIRIES_READ,
        summary="Seller inquiries with keyset pagination (no message text, no addresses).",
        request=InquiryListQuery,
        location="query",
        paginated=True,
    ),
    _r(
        "GET",
        "/api/inquiries/{inquiry_id}",
        InquiryView,
        scope=Scope.INQUIRIES_READ,
        summary="One seller inquiry (recipient address only for the owner).",
        errors=_NF,
        tool="seller_inquiries_get",
    ),
    _r(
        "GET",
        "/api/replies",
        ReplyListView,
        scope=Scope.INQUIRIES_READ,
        summary="Seller replies with keyset pagination (no bodies).",
        request=ReplyListQuery,
        location="query",
        paginated=True,
    ),
    _r(
        "GET",
        "/api/replies/{reply_id}",
        ReplyView,
        scope=Scope.INQUIRIES_READ,
        summary="One seller reply: original text, MK summary, claims, attachment metadata.",
        errors=_NF,
        tool="seller_replies_get",
    ),
    _r(
        "GET",
        "/api/inquiry-control",
        InquiryControlView,
        scope=Scope.INQUIRIES_READ,
        summary=(
            "Kill switch, mode, owner-reducible caps and current usage (NOT_FOUND until the"
            " workspace controls exist)."
        ),
        errors=_NF,
    ),
    _r(
        "POST",
        "/api/inquiry-control/pause",
        InquiryPauseResult,
        scope=Scope.INQUIRIES_PAUSE,
        summary="Activate the inquiry kill switch against the expected control version.",
        request=InquiryPauseRequest,
        location="body",
        errors=(ErrorCode.IDEMPOTENCY_CONFLICT, ErrorCode.VERSION_CONFLICT),
        tool="seller_inquiries_pause",
        idempotency_header="optional",
    ),
    _r(
        "POST",
        "/api/inquiry-control/resume",
        InquiryResumeResult,
        scope=Scope.CONFIG_ADMIN,
        summary=(
            "Owner-only: clear the kill switch against the expected control version; optionally"
            " remove kill-switch/authorization-revoked suppressions (each audited)."
        ),
        request=InquiryResumeRequest,
        location="body",
        errors=(ErrorCode.IDEMPOTENCY_CONFLICT, ErrorCode.VERSION_CONFLICT),
        idempotency_header="optional",
    ),
    _r(
        "GET",
        "/api/activation/canary-evidence",
        CanaryEvidenceView,
        scope=Scope.CONFIG_ADMIN,
        summary=(
            "Owner-only, read-only: activation-canary evidence (rows 4-6) of the configured sender"
            " and the newest canaries (ids, states, times; never the target or its hash)."
        ),
    ),
    _r(
        "GET",
        "/api/mail-workers/health",
        MailWorkerHealthView,
        scope=Scope.INQUIRIES_READ,
        summary="Mailbox-worker health: heartbeat, Outlook, reconciliation, backlog, account, gaps.",
        request=MailWorkerHealthQuery,
        location="query",
    ),
    _r(
        "GET",
        "/api/mail-workers/coverage-gaps",
        MailCoverageGapListView,
        scope=Scope.INQUIRIES_READ,
        summary="Mailbox coverage gaps (open first); a gap is never hidden.",
        request=MailWorkerHealthQuery,
        location="query",
    ),
    _r(
        "GET",
        "/api/lifecycle/lags",
        CoverageLagsView,
        scope=Scope.DEALS_READ,
        summary="Separate scan, notification and mail-reply lags; unknown is never shown as zero.",
    ),
    _r(
        "GET",
        "/api/listings/{listing_id}/lifecycle",
        ListingLifecycleView,
        scope=Scope.DEALS_READ,
        summary="One source listing's first/last seen, detail freshness and detection delay.",
        errors=_NF,
    ),
    _r(
        "GET",
        "/api/evaluation",
        EvaluationReport,
        scope=Scope.INQUIRIES_READ,
        summary="The 15-day evaluation report from stored evidence (zero is reported as zero).",
        request=EvaluationQuery,
        location="query",
    ),
)
V11_ROUTE_INDEX: Final[Mapping[str, ApiRoute]] = MappingProxyType(
    {route.key: route for route in (*MAIL_WORKER_ROUTES, *V11_DASHBOARD_ROUTES)}
)


def route_slug(route: ApiRoute) -> str:
    """Stable file name part of a route: ``mail-workers.send-intents.intent_id.claim.post``."""
    parts = [p.strip("{}") for p in route.path.split("/") if p and p not in ("v1", "api")]
    return ".".join([*parts, route.method.lower()])


def route_schema_document(route: ApiRoute) -> dict[str, Any]:
    """``schemas/api/<slug>.json``: method, path, auth, scope, request/response schemas, errors and
    the ``Idempotency-Key`` header rule."""
    request = (
        None
        if route.request_model is None
        else model_schema(route.request_model, mode="validation", title=route.request_model.__name__)
    )
    return {
        "method": route.method,
        "path": route.path,
        "auth": route.auth,
        "requiredScope": None if route.scope is None else route.scope.value,
        "requestLocation": route.request_location,
        "request": request,
        "response": model_schema(
            route.response_model, mode="serialization", title=f"{route_slug(route)}_response"
        ),
        "successStatus": route.success_status,
        "errors": [code.value for code in route.errors],
        "paginated": route.paginated,
        "mcpTool": route.mcp_tool,
        "summary": route.summary,
        "idempotencyKeyHeader": route.idempotency_header,
    }


def exported_api_schema_documents() -> dict[str, dict[str, Any]]:
    """Spec 37 API contracts as committed schema files (``schemas/api/``), keyed by path."""
    return {
        f"api/{route_slug(route)}.json": route_schema_document(route)
        for route in (*MAIL_WORKER_ROUTES, *V11_DASHBOARD_ROUTES)
    }


__all__ = [
    "COMMON_ERRORS",
    "IDEMPOTENCY_HEADER",
    "MAIL_WORKER_BODY_LIMIT",
    "MAIL_WORKER_PREFIX",
    "MAIL_WORKER_ROUTES",
    "MAIL_WORKER_SCHEMA_VERSION",
    "MAX_REMOVABLE_SUPPRESSIONS",
    "MAX_RETURNED_MESSAGE_IDS",
    "REQUEST_ID_HEADER",
    "ROUTES",
    "ROUTE_INDEX",
    "V11_DASHBOARD_ROUTES",
    "V11_ROUTE_INDEX",
    "AddNoteRequest",
    "ApiErrorResponse",
    "ApiQuery",
    "ApiRoute",
    "CandidateDetailQuery",
    "CandidateListQuery",
    "ClaimRequest",
    "ComparablesQuery",
    "EvaluationQuery",
    "IdempotencyHeader",
    "InquiryListQuery",
    "InquiryPauseRequest",
    "InquiryResumeRequest",
    "MailWorkerAccepted",
    "MailWorkerAccountReport",
    "MailWorkerBindingItem",
    "MailWorkerBindingPage",
    "MailWorkerBindingsQuery",
    "MailWorkerCanaryClaimDecision",
    "MailWorkerCanaryClaimRequest",
    "MailWorkerCanaryIntent",
    "MailWorkerCanaryIntentBatch",
    "MailWorkerCanaryReply",
    "MailWorkerCanaryReport",
    "MailWorkerCheckpointReport",
    "MailWorkerClaimDecision",
    "MailWorkerClaimRequest",
    "MailWorkerGapReport",
    "MailWorkerHealthQuery",
    "MailWorkerHeartbeatAck",
    "MailWorkerHeartbeatRequest",
    "MailWorkerReplyAck",
    "MailWorkerReplyRequest",
    "MailWorkerSendIntent",
    "MailWorkerSendIntentBatch",
    "MailWorkerSendIntentsQuery",
    "MailWorkerSendReport",
    "OutboxQuery",
    "PathId",
    "PauseSourceRequest",
    "RecheckRequest",
    "ReleaseRequest",
    "ReplyListQuery",
    "ResponseEnvelope",
    "ReviewQueueQuery",
    "SubmitReviewRequest",
    "api_error",
    "exported_api_schema_documents",
    "route_schema_document",
    "route_slug",
]
