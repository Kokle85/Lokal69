"""Seller-email provider contract (spec 37.3 sender binding, 37.5 send semantics, 37.8 adapters).

Every provider adapter implements ``SenderProvider``: account verification, send, receipt
reconciliation, correlated-reply retrieval and health checks. The contract is deliberately
pessimistic:

``send`` returns exactly one of

* ``SendAccepted`` - the configured provider accepted the submission. This is not delivery,
  reading or seller agreement. Provider message/thread ids and the receipt are recorded only
  when the provider actually returned them; nothing is ever fabricated (Microsoft Graph's
  ``sendMail`` returns ``202`` without an id, so its ids stay ``None``).
* ``SendDefiniteFailure`` - nothing was sent. ``pre_submission=True`` means it is proven that
  the message never entered the provider's submission pipeline (connection refused before any
  request byte, local validation/kill-switch refusal, credentials unavailable, or a documented
  provider rejection such as 400/401/403); ``proof`` names the evidence in the domain's
  ``PreSubmissionProof`` vocabulary. ``pre_submission=False`` is a definitive provider/transport
  rejection reported *after* the hand-over (for example an Outlook transport rejection): never
  retried automatically.
* ``SendUncertain`` - the request may have reached the provider and the outcome is unknown:
  read timeouts, connection resets after the request may have been transmitted, write
  interruptions, total-deadline expiry, 429/5xx (the request reached the provider, which does
  not prove that nothing was processed), unexpected statuses or redirects, and - for the
  local Outlook route - every state before Sent Items evidence. The caller keeps the
  reservation and quota debit, marks the inquiry ``uncertain`` and reconciles; it never
  blindly resends and never sends from another account.

``reconcile`` returns ``found_sent`` (positive evidence), ``not_found_yet`` (searched, nothing
found - explicitly *not* proof of non-submission: search indexes and Sent Items lag, the
message may sit in Drafts/Outbox, an old worker may still complete its request),
``provider_unavailable`` or - only on the local worker route - ``proven_not_submitted``: every
send intent of the inquiry was definitively refused by the mailbox-bound worker before
``.Send`` (for example because it expired while the laptop was off), so no handed-over copy can
ever be transmitted. ``reconciliation_evidence`` converts it into the domain's
``ReconciliationEvidence`` so ``inquiries.reconcile_uncertain`` makes the decision.

Before any I/O every ``send`` re-checks the final bytes (``built_message_problems``) *and* the
bounded scope (``mime_builder.inquiry_scope_problems``): only the exact rendering of a registered
seller template, addressed to exactly one recipient without Cc/Bcc or attachments, is ever handed
to a provider.

``fetch_correlated_replies`` is the provider-API alternative to the local Outlook reply
worker: it reads candidate *metadata* first, fetches a body only for messages that reference
this system's outbound Message-IDs/threads or come from a verified seller alias, correlates
locally with ``replies.correlate_reply`` and returns only matched/quarantined messages.
Unrelated personal mail is never returned.

There is no approval hook anywhere in this contract: the bounded standing authorization
(spec 37.1) means a qualifying send never waits for a human.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable
from uuid import UUID

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import EmailProviderKind, Tristate
from suv_deals.domain.inquiries import (
    PreSubmissionProof,
    ReconciliationEvidence,
    SendAttemptOutcome,
    SenderBinding,
)
from suv_deals.domain.replies import (
    CorrelationResult,
    InboundMessage,
    InquiryBinding,
    normalize_message_id,
    safe_filename,
)
from suv_deals.domain.seller_contacts import AddressError, canonicalize_address
from suv_deals.integrations.mime_builder import (
    BuiltMessage,
    built_message_problems,
    inquiry_scope_problems,
)

PROVIDER_CONTRACT_VERSION: Final = "sender-provider/1"
MAX_RECONCILE_WINDOW: Final = timedelta(days=60)
MAX_RECONCILE_MESSAGE_IDS: Final = 20
MAX_PROVIDER_ERROR_CHARS: Final = 64
MAX_RETRY_AFTER_SECONDS: Final = 86_400
IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SAFE_TOKEN_RE: Final = re.compile(r"[^A-Za-z0-9_.:-]")
_OPAQUE_ID_RE: Final = re.compile(r"^[A-Za-z0-9_=+/.:-]{1,512}$")

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _utc(value: datetime) -> datetime:
    return ensure_utc(value)


# =============================================================================================
# Capabilities, verification and health
# =============================================================================================


class AliasStatus(StrEnum):
    PRIMARY = "primary"  # the account's own primary address
    VERIFIED_ALIAS = "verified_alias"  # provider-verified send-as alias of the same account
    PENDING = "pending"  # alias exists but the provider has not verified it
    NOT_FOUND = "not_found"  # not an address of this account
    NOT_VERIFIABLE = "not_verifiable"  # this provider/route offers no verification API
    NOT_CHECKED = "not_checked"  # the check could not run (provider unavailable, scope missing)


USABLE_ALIAS_STATUSES: Final = frozenset({AliasStatus.PRIMARY, AliasStatus.VERIFIED_ALIAS})


class ProviderHealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    CREDENTIALS_REVOKED = "credentials_revoked"
    NOT_CONFIGURED = "not_configured"


class ProviderCapabilities(BaseModel):
    """What the provider offers; ``unverified_offline`` lists claims not verified by a live test."""

    model_config = _FROZEN

    kind: EmailProviderKind
    delivery_mode: Literal["direct_api", "local_worker"]
    returns_provider_message_id: bool
    returns_thread_id: bool
    preserves_client_message_id: Tristate
    documented_send_idempotency: bool = False  # no supported provider documents one
    reconcile_by_rfc_message_id: Tristate
    alias_verification: Literal["api", "primary_only", "worker_report"]
    reply_retrieval: Literal["provider_pull", "local_worker_push"]
    required_scopes: tuple[str, ...] = ()
    unverified_offline: tuple[str, ...] = ()


class SenderVerification(BaseModel):
    """Result of a technical account/alias verification (one-time setup and periodic health).

    This is a technical prerequisite, never a message approval: it binds the stable account id,
    actual email, display name and verified alias status (spec 37.3).
    """

    model_config = _FROZEN

    provider: EmailProviderKind
    checked_at: datetime
    configured_account_id: str
    configured_from: str
    configured_reply_to: str | None
    configured_display_name: str
    stable_account_id: str | None = None
    account_email: str | None = None
    provider_display_name: str | None = Field(default=None, max_length=256)
    from_status: AliasStatus = AliasStatus.NOT_CHECKED
    reply_to_status: AliasStatus | None = None
    granted_scopes: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()
    excess_scopes: tuple[str, ...] = ()
    capabilities: ProviderCapabilities
    health: ProviderHealthStatus
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _reply_to(self) -> SenderVerification:
        if (self.configured_reply_to is None) != (self.reply_to_status is None):
            raise ValueError("reply_to_status is set exactly when a Reply-To is configured")
        return self

    @property
    def alias_verified(self) -> bool:
        return self.from_status in USABLE_ALIAS_STATUSES and (
            self.reply_to_status is None or self.reply_to_status in USABLE_ALIAS_STATUSES
        )

    @property
    def verified(self) -> bool:
        return not self.problems and self.health == ProviderHealthStatus.OK and self.alias_verified

    @property
    def credentials_revoked(self) -> bool:
        return self.health == ProviderHealthStatus.CREDENTIALS_REVOKED


class ProviderHealth(BaseModel):
    model_config = _FROZEN

    provider: EmailProviderKind
    status: ProviderHealthStatus
    checked_at: datetime
    problems: tuple[str, ...] = ()
    last_worker_heartbeat_age_seconds: int | None = Field(default=None, ge=0)

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


# =============================================================================================
# Send outcomes
# =============================================================================================


class SendFailureReason(StrEnum):
    LOCAL_VALIDATION_FAILED = "local_validation_failed"
    SENDER_MISMATCH = "sender_mismatch"  # message From/Reply-To/display name is not the binding
    ACCOUNT_MISMATCH = "account_mismatch"  # the credential belongs to another account
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    CREDENTIALS_REVOKED = "credentials_revoked"
    CREDENTIALS_REJECTED = "credentials_rejected"  # 401 from the provider
    INSUFFICIENT_SCOPE = "insufficient_scope"
    ACCOUNT_NOT_PERMITTED = "account_not_permitted"  # domain policy, delegation denied, licence
    PROVIDER_REJECTED_INVALID = "provider_rejected_invalid"  # 400
    PROVIDER_NOT_FOUND = "provider_not_found"  # 404 (mailbox/user)
    MESSAGE_TOO_LARGE = "message_too_large"  # 413
    CONNECTION_FAILED = "connection_failed"  # nothing reached the provider
    PRECHECK_FAILED = "precheck_failed"  # identity pre-check failed; the send was never issued
    LOCAL_HANDOFF_FAILED = "local_handoff_failed"  # Outlook intent could not be stored
    LOCAL_WORKER_REFUSED = "local_worker_refused"  # the desktop worker refused before .Send
    TRANSPORT_REJECTED = "transport_rejected"  # definitive rejection after the hand-over


class UncertainReason(StrEnum):
    READ_TIMEOUT = "read_timeout"
    WRITE_INTERRUPTED = "write_interrupted"
    CONNECTION_LOST = "connection_lost"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROVIDER_THROTTLED = "provider_throttled"  # 429 / rate-limit 403 after the request arrived
    PROVIDER_SERVER_ERROR = "provider_server_error"  # 5xx
    UNEXPECTED_STATUS = "unexpected_status"
    UNEXPECTED_ERROR = "unexpected_error"
    LOCAL_WORKER_HANDOFF = "local_worker_handoff"  # intent stored for the desktop worker
    LOCAL_SUBMISSION_PENDING = "local_submission_pending"  # Outlook Outbox, no Sent Items evidence
    LOCAL_SEND_CALL_FAILED = "local_send_call_failed"
    LOCAL_WORKER_NO_RESULT = "local_worker_no_result"
    LOCAL_ACCOUNT_MISMATCH_REPORTED = "local_account_mismatch_reported"


class ProviderReceipt(BaseModel):
    """What the provider actually returned for an accepted submission (never invented)."""

    model_config = _FROZEN

    kind: Literal["gmail_message_resource", "graph_202_accepted", "outlook_sent_items"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = Field(default=None, max_length=128)
    label_ids: tuple[str, ...] = Field(default=(), max_length=50)
    response_complete: bool = True
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class _OutcomeBase(BaseModel):
    model_config = _FROZEN

    provider: EmailProviderKind
    inquiry_id: UUID
    attempt_id: UUID
    rfc_message_id: str  # the client Message-ID that was (or may have been) transmitted
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SendAccepted(_OutcomeBase):
    status: Literal["accepted"] = "accepted"
    observed_rfc_message_id: str | None = None  # read back from the provider when available
    provider_message_id: str | None = Field(default=None, max_length=512)
    provider_thread_id: str | None = Field(default=None, max_length=512)
    receipt: ProviderReceipt
    accepted_at: datetime

    @field_validator("accepted_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _never_fabricated(self) -> SendAccepted:
        if self.receipt.kind == "graph_202_accepted" and (
            self.provider_message_id is not None or self.provider_thread_id is not None
        ):
            raise ValueError("Graph sendMail returns no message id; none may be recorded")
        if not self.receipt.response_complete and (
            self.provider_message_id is not None or self.provider_thread_id is not None
        ):
            raise ValueError("ids can only come from a complete provider response")
        return self


class SendDefiniteFailure(_OutcomeBase):
    status: Literal["definite_failure"] = "definite_failure"
    pre_submission: bool
    reason: SendFailureReason
    retryable: bool
    proof: PreSubmissionProof | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_error: str | None = Field(default=None, max_length=MAX_PROVIDER_ERROR_CHARS)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=MAX_RETRY_AFTER_SECONDS)
    problems: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> SendDefiniteFailure:
        if self.pre_submission and self.proof is None:
            raise ValueError("a pre-submission failure must name its proof")
        if not self.pre_submission and (self.retryable or self.proof is not None):
            raise ValueError(
                "a failure after the hand-over is never retryable and has no pre-submission proof"
            )
        return self


class SendUncertain(_OutcomeBase):
    status: Literal["uncertain"] = "uncertain"
    reason: UncertainReason
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_error: str | None = Field(default=None, max_length=MAX_PROVIDER_ERROR_CHARS)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=MAX_RETRY_AFTER_SECONDS)
    awaiting_local_worker: bool = False
    outbox_pending: Tristate = Tristate.UNKNOWN


SendOutcome = Annotated[SendAccepted | SendDefiniteFailure | SendUncertain, Field(discriminator="status")]


class AttemptOutcomeMapping(BaseModel):
    """How a ``SendOutcome`` is recorded as a domain ``SendAttemptEvidence`` outcome."""

    model_config = _FROZEN

    outcome: SendAttemptOutcome
    pre_submission_proof: PreSubmissionProof | None = None
    requires_reconciliation: bool = False


def attempt_outcome(outcome: SendAccepted | SendDefiniteFailure | SendUncertain) -> AttemptOutcomeMapping:
    """Map a provider outcome onto the domain attempt vocabulary (``inquiries.should_retry``).

    Only a retryable, proven pre-submission failure becomes ``PRE_SUBMISSION_FAILURE`` (eligible
    for the domain retry policy); every other definite failure is a ``DEFINITE_REJECTION`` (no
    automatic retry) and every uncertain outcome is ``UNCERTAIN`` (hold for reconciliation).
    """
    if isinstance(outcome, SendAccepted):
        return AttemptOutcomeMapping(outcome=SendAttemptOutcome.ACCEPTED)
    if isinstance(outcome, SendUncertain):
        return AttemptOutcomeMapping(outcome=SendAttemptOutcome.UNCERTAIN, requires_reconciliation=True)
    if outcome.pre_submission and outcome.retryable:
        return AttemptOutcomeMapping(
            outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE, pre_submission_proof=outcome.proof
        )
    return AttemptOutcomeMapping(outcome=SendAttemptOutcome.DEFINITE_REJECTION)


# =============================================================================================
# Reconciliation
# =============================================================================================


class ReconcileWindow(BaseModel):
    model_config = _FROZEN

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _order(self) -> ReconcileWindow:
        if self.end <= self.start:
            raise ValueError("reconcile window end must be after start")
        if self.end - self.start > MAX_RECONCILE_WINDOW:
            raise ValueError("reconcile window is too long")
        return self


class SentEvidence(BaseModel):
    model_config = _FROZEN

    matched_by: Literal["rfc_message_id", "provider_message_id", "local_sent_items"]
    location: str = Field(max_length=64)  # e.g. "gmail:SENT", "graph:sent", "outlook:sent_items"
    rfc_message_id: str | None = None
    provider_message_id: str | None = Field(default=None, max_length=512)
    provider_thread_id: str | None = Field(default=None, max_length=512)
    sent_at: datetime | None = None

    @field_validator("sent_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class ReconcileFoundSent(BaseModel):
    model_config = _FROZEN

    status: Literal["found_sent"] = "found_sent"
    provider: EmailProviderKind
    inquiry_id: UUID
    evidence: SentEvidence


class ReconcileNotFoundYet(BaseModel):
    """Searched and found nothing. NOT proof of non-submission (spec 37.5)."""

    model_config = _FROZEN

    status: Literal["not_found_yet"] = "not_found_yet"
    provider: EmailProviderKind
    inquiry_id: UUID
    searched_message_ids: tuple[str, ...] = ()
    searched_provider_ids: tuple[str, ...] = ()
    pending_in_drafts_or_outbox: Tristate = Tristate.UNKNOWN
    proves_non_submission: Literal[False] = False
    note: str = "absence from a provider search or Sent Items is not proof of non-submission"


class ReconcileProviderUnavailable(BaseModel):
    model_config = _FROZEN

    status: Literal["provider_unavailable"] = "provider_unavailable"
    provider: EmailProviderKind
    inquiry_id: UUID
    reason: str = Field(max_length=MAX_PROVIDER_ERROR_CHARS)
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=MAX_RETRY_AFTER_SECONDS)


class ReconcileProvenNotSubmitted(BaseModel):
    """Positive proof that no handed-over copy of the inquiry can ever be transmitted.

    Only the local worker route produces it: every send intent stored for the inquiry (and every
    searched Message-ID belongs to one of them) carries a definitive ``refused_before_send``
    report from its mailbox-bound worker, which never calls ``.Send`` for a refused or already
    attempted intent. A direct-API provider can never prove non-submission by searching.
    """

    model_config = _FROZEN

    status: Literal["proven_not_submitted"] = "proven_not_submitted"
    provider: EmailProviderKind
    inquiry_id: UUID
    proof: PreSubmissionProof
    refused_intent_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_RECONCILE_MESSAGE_IDS)
    refusal_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _local_only(self) -> ReconcileProvenNotSubmitted:
        if self.provider != EmailProviderKind.OUTLOOK_LOCAL:
            raise ValueError("only the local worker route can prove that nothing was submitted")
        return self


ReconcileOutcome = (
    ReconcileFoundSent | ReconcileNotFoundYet | ReconcileProviderUnavailable | ReconcileProvenNotSubmitted
)
ReconcileResult = Annotated[ReconcileOutcome, Field(discriminator="status")]


def reconciliation_evidence(result: ReconcileOutcome) -> ReconciliationEvidence:
    """Domain evidence for ``inquiries.reconcile_uncertain``.

    A search that found nothing never claims non-submission; only ``proven_not_submitted`` (the
    local worker's definitive refusal of every intent) does.
    """
    local = result.provider == EmailProviderKind.OUTLOOK_LOCAL
    if isinstance(result, ReconcileProvenNotSubmitted):
        return ReconciliationEvidence(
            outbox_pending=Tristate.NO,
            proven_not_submitted=result.proof,
            worker_alive=Tristate.NO,  # the worker has finished with every refused intent
        )
    if isinstance(result, ReconcileFoundSent):
        if local:
            return ReconciliationEvidence(sent_items="found", outbox_pending=Tristate.NO)
        return ReconciliationEvidence(provider_search="found")
    if isinstance(result, ReconcileNotFoundYet):
        if local:
            return ReconciliationEvidence(
                sent_items="not_found", outbox_pending=result.pending_in_drafts_or_outbox
            )
        return ReconciliationEvidence(
            provider_search="not_found", outbox_pending=result.pending_in_drafts_or_outbox
        )
    return ReconciliationEvidence()


def normalize_search_ids(values: Sequence[str]) -> tuple[str, ...]:
    """Validated, de-duplicated RFC Message-IDs (bounded) for a reconciliation search."""
    found: list[str] = []
    for value in values:
        normalized = normalize_message_id(value)
        if normalized is None:
            raise ValueError("reconciliation needs valid RFC Message-IDs")
        if normalized not in found:
            found.append(normalized)
    if not found or len(found) > MAX_RECONCILE_MESSAGE_IDS:
        raise ValueError("between 1 and 20 Message-IDs are required")
    return tuple(found)


# =============================================================================================
# Correlated replies
# =============================================================================================


class ProviderAttachmentRef(BaseModel):
    """Attachment metadata seen through a provider API. Bytes are never fetched here."""

    model_config = _FROZEN

    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(max_length=191)
    byte_size: int | None = Field(default=None, ge=0, le=2**40)
    provider_attachment_id: str | None = Field(default=None, max_length=1024)

    @field_validator("filename", mode="before")
    @classmethod
    def _filename(cls, value: Any) -> str:
        return safe_filename(value if isinstance(value, str) else None)

    @field_validator("mime_type", mode="before")
    @classmethod
    def _mime(cls, value: Any) -> str:
        text = value.strip().lower().split(";", 1)[0] if isinstance(value, str) else ""
        return (
            text
            if re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}", text)
            else ("application/octet-stream")
        )


class CorrelatedReply(BaseModel):
    """A message that may leave the mailbox: matched (full) or quarantined (one candidate)."""

    model_config = _FROZEN

    message: InboundMessage
    correlation: CorrelationResult
    attachment_refs: tuple[ProviderAttachmentRef, ...] = Field(default=(), max_length=50)
    body_truncated: bool = False
    html_only_body: bool = False

    @model_validator(mode="after")
    def _only_correlated(self) -> CorrelatedReply:
        if self.correlation.upload_scope == "none":
            raise ValueError("unrelated mail never leaves the mailbox")
        return self


class ReplyFetchResult(BaseModel):
    model_config = _FROZEN

    provider: EmailProviderKind
    retrieval_mode: Literal["provider_pull", "local_worker_push"]
    replies: tuple[CorrelatedReply, ...] = ()
    #: Provider message ids to re-inspect later (``recheck_locators``): unmatched messages that
    #: reference one of this system's Message-ID shapes not yet in the synced bindings
    #: (reply-before-binding race), and messages a window pull listed but could not inspect in
    #: this pull (budget or transient failure). Locators only, never content.
    pending_retry_locators: tuple[str, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=512)
    complete: bool = True
    gap_detected: bool = False
    scanned: int = Field(default=0, ge=0)
    skipped_unrelated: int = Field(default=0, ge=0)
    problems: tuple[str, ...] = ()


# =============================================================================================
# Credentials
# =============================================================================================


class AccessToken(BaseModel):
    """A short-lived OAuth access token resolved server-side from a secret reference."""

    model_config = _FROZEN

    token: SecretStr
    scopes: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("token")
    @classmethod
    def _token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or any(ord(c) < 33 or ord(c) > 126 for c in raw) or len(raw) > 4096:
            raise ValueError("access token must be non-empty printable ASCII without whitespace")
        return value

    def bearer(self) -> str:
        return f"Bearer {self.token.get_secret_value()}"


class TokenUnavailable(Exception):  # not an AppError: never surfaced to API callers
    """No usable token. ``revoked`` means the grant was revoked/expired (re-consent needed)."""

    def __init__(self, *, revoked: bool, code: str = "token_unavailable") -> None:
        super().__init__(f"access token unavailable: {safe_token(code)}")
        self.revoked = revoked
        self.code = safe_token(code)


class TokenProvider(Protocol):
    """Resolves the binding's secret reference server-side; tokens are never logged or returned."""

    async def access_token(self, *, force_refresh: bool = False) -> AccessToken: ...


# =============================================================================================
# Provider protocol
# =============================================================================================


@runtime_checkable
class SenderProvider(Protocol):
    @property
    def kind(self) -> EmailProviderKind: ...

    @property
    def binding(self) -> SenderBinding: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def verify_account(self) -> SenderVerification: ...

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted | SendDefiniteFailure | SendUncertain: ...

    async def reconcile(
        self,
        *,
        inquiry_id: UUID,
        rfc_message_ids: Sequence[str],
        window: ReconcileWindow,
        provider_message_ids: Sequence[str] = (),
    ) -> ReconcileOutcome: ...

    async def fetch_correlated_replies(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        since: datetime,
        cursor: str | None = None,
        max_messages: int | None = None,
    ) -> ReplyFetchResult: ...

    async def health(self) -> ProviderHealth: ...


# =============================================================================================
# Shared helpers
# =============================================================================================


def safe_token(value: object, *, limit: int = MAX_PROVIDER_ERROR_CHARS) -> str:
    """A short log/DB-safe token (provider error reasons/codes); never free text."""
    text = value if isinstance(value, str) else str(value)
    return _SAFE_TOKEN_RE.sub("_", unicodedata.normalize("NFKC", text))[:limit] or "unknown"


def safe_opaque_id(value: object) -> str | None:
    """A provider id as returned (opaque token) or ``None`` when it is missing/unsafe."""
    if (
        isinstance(value, str)
        and _OPAQUE_ID_RE.fullmatch(value)
        and ".." not in value
        and not value.startswith(("/", "."))
    ):
        return value
    return None


def canonical_or_none(address: str | None) -> str | None:
    if address is None:
        return None
    try:
        return canonicalize_address(address).canonical
    except AddressError:
        return None


def same_account_address(first: str | None, second: str | None) -> bool:
    """Provider-reported own-account address equality (domain canonical, case-insensitive).

    Used only to compare the configured account with the address the provider itself reports
    for the authenticated account (Gmail/Microsoft treat their own account addresses
    case-insensitively); never to equate two seller addresses.
    """
    a, b = canonical_or_none(first), canonical_or_none(second)
    return a is not None and b is not None and a.casefold() == b.casefold()


def send_precondition_problems(
    message: BuiltMessage,
    binding: SenderBinding,
    *,
    provider: EmailProviderKind,
    inquiry_id: UUID,
    idempotency_key: str,
) -> list[str]:
    """Local checks before any I/O: a failure here is a proven pre-submission refusal.

    Covers the final bytes (exact allowed header set, one recipient, no Cc/Bcc/attachment), the
    bounded inquiry scope (exact registered-template rendering, ``validate_scope`` with the real
    envelope) and the exact sender binding. There is no approval check: none exists.
    """
    problems = [f"MIME:{p}" for p in built_message_problems(message)]
    problems.extend(f"SCOPE:{p}" for p in inquiry_scope_problems(message))
    if binding.provider != provider:
        problems.append("PROVIDER_MISMATCH")
    if message.inquiry_id != inquiry_id:
        problems.append("INQUIRY_MISMATCH")
    if message.from_address != canonical_or_none(binding.from_address):
        problems.append("SENDER_MISMATCH")
    if message.from_display_name != binding.display_name:
        problems.append("SENDER_DISPLAY_NAME_MISMATCH")
    if message.reply_to_address != canonical_or_none(binding.reply_to_address):
        problems.append("REPLY_TO_MISMATCH")
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        problems.append("IDEMPOTENCY_KEY_INVALID")
    return problems


def local_refusal(
    message: BuiltMessage,
    *,
    provider: EmailProviderKind,
    inquiry_id: UUID,
    attempt_id: UUID,
    problems: Sequence[str],
) -> SendDefiniteFailure:
    sender_codes = {
        "SENDER_MISMATCH",
        "SENDER_DISPLAY_NAME_MISMATCH",
        "REPLY_TO_MISMATCH",
        "PROVIDER_MISMATCH",
    }
    reason = (
        SendFailureReason.SENDER_MISMATCH
        if sender_codes & set(problems)
        else SendFailureReason.LOCAL_VALIDATION_FAILED
    )
    return SendDefiniteFailure(
        provider=provider,
        inquiry_id=inquiry_id,
        attempt_id=attempt_id,
        rfc_message_id=message.rfc_message_id,
        raw_sha256=message.raw_sha256,
        pre_submission=True,
        reason=reason,
        retryable=False,
        proof="local_validation_failed_before_submit",
        problems=tuple(sorted(set(problems)))[:20],
    )


def parse_retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after")
    if value is None or not value.strip().isdigit():
        return None
    return min(int(value.strip()), MAX_RETRY_AFTER_SECONDS)


# ---------------------------------------------------------------------------------------------
# HTTP execution with send-safety classification
# ---------------------------------------------------------------------------------------------


class TransportFailure(StrEnum):
    NOT_SENT = "not_sent"  # provably no request byte reached the provider
    MAYBE_SENT = "maybe_sent"  # the request may have been (partly or fully) transmitted


@dataclass
class _Tracer:
    """httpcore trace hook: did the request start before a deadline cancelled it?"""

    events: int = 0
    request_started: bool = False

    async def __call__(self, name: str, info: Mapping[str, Any]) -> None:
        self.events += 1
        if name.endswith("send_request_headers.started"):
            self.request_started = True


@dataclass(frozen=True)
class HttpResult:
    status_code: int | None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    body_complete: bool = True
    failure: TransportFailure | None = None
    failure_detail: str | None = None  # internal classification only (never a URL or body)

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    def json(self) -> dict[str, Any] | None:
        if not self.body_complete or not self.body:
            return None
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None


_KEPT_RESPONSE_HEADERS: Final = frozenset(
    {"content-type", "retry-after", "request-id", "client-request-id", "x-ms-ags-diagnostic"}
)


def _classify_exception(exc: BaseException) -> tuple[TransportFailure, str]:
    if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
        return TransportFailure.NOT_SENT, "connect_timeout"
    if isinstance(exc, httpx.ProxyError):
        return TransportFailure.NOT_SENT, "proxy_refused"
    if isinstance(exc, httpx.ConnectError):
        return TransportFailure.NOT_SENT, "connect_error"
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return TransportFailure.NOT_SENT, "invalid_url"
    if isinstance(exc, httpx.ReadTimeout):
        return TransportFailure.MAYBE_SENT, "read_timeout"
    if isinstance(exc, httpx.WriteTimeout | httpx.WriteError):
        # A partially written request is very likely incomplete for the server, but that is a
        # protocol argument, not observed proof: conservatively uncertain (spec 37.5).
        return TransportFailure.MAYBE_SENT, "write_interrupted"
    if isinstance(exc, httpx.ReadError | httpx.RemoteProtocolError):
        return TransportFailure.MAYBE_SENT, "connection_lost"
    return TransportFailure.MAYBE_SENT, "unexpected_error"


async def http_call(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, str | int | Sequence[str]] | None = None,
    content: bytes | None = None,
    timeout_s: float,
    max_response_bytes: int,
) -> HttpResult:
    """Run one request and classify failures for send safety. Never raises for I/O errors.

    Cancellation (``asyncio.CancelledError``/anyio cancellation) is *not* caught: a cancelled
    send must be treated as an interrupted attempt (uncertain) by the caller.
    """
    tracer = _Tracer()
    status: int | None = None
    kept: dict[str, str] = {}
    buffer = bytearray()
    complete = True
    try:
        with anyio.fail_after(timeout_s):
            async with client.stream(
                method,
                url,
                headers=dict(headers),
                params=params,
                content=content,
                extensions={"trace": tracer},
            ) as response:
                status = response.status_code
                kept = {
                    k.lower(): v[:256]
                    for k, v in response.headers.items()
                    if k.lower() in _KEPT_RESPONSE_HEADERS
                }
                try:
                    async for chunk in response.aiter_bytes():
                        remaining = max_response_bytes - len(buffer)
                        if len(chunk) > remaining:
                            buffer.extend(chunk[:remaining])
                            complete = False
                            break
                        buffer.extend(chunk)
                except httpx.HTTPError:
                    complete = False  # the status is known; only the body is incomplete
    except TimeoutError:
        if status is not None:
            return HttpResult(status_code=status, headers=kept, body=bytes(buffer), body_complete=False)
        not_started = tracer.events > 0 and not tracer.request_started
        return HttpResult(
            status_code=None,
            failure=TransportFailure.NOT_SENT if not_started else TransportFailure.MAYBE_SENT,
            failure_detail="deadline_before_request" if not_started else "deadline",
        )
    except httpx.HTTPError as exc:
        if status is not None:
            return HttpResult(status_code=status, headers=kept, body=bytes(buffer), body_complete=False)
        failure, detail = _classify_exception(exc)
        return HttpResult(status_code=None, failure=failure, failure_detail=detail)
    except (OSError, ValueError, RuntimeError) as exc:
        if status is not None:
            return HttpResult(status_code=status, headers=kept, body=bytes(buffer), body_complete=False)
        failure, detail = _classify_exception(exc)
        return HttpResult(status_code=None, failure=failure, failure_detail=detail)
    return HttpResult(status_code=status, headers=kept, body=bytes(buffer), body_complete=complete)


def uncertain_from_transport(
    message: BuiltMessage,
    *,
    provider: EmailProviderKind,
    inquiry_id: UUID,
    attempt_id: UUID,
    result: HttpResult,
) -> SendDefiniteFailure | SendUncertain:
    """Outcome of a send request that produced no HTTP status."""
    if result.failure == TransportFailure.NOT_SENT:
        return SendDefiniteFailure(
            provider=provider,
            inquiry_id=inquiry_id,
            attempt_id=attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            pre_submission=True,
            reason=SendFailureReason.CONNECTION_FAILED,
            retryable=True,
            proof="connection_refused_before_submit",
            provider_error=safe_token(result.failure_detail or "not_sent"),
        )
    reason = {
        "read_timeout": UncertainReason.READ_TIMEOUT,
        "write_interrupted": UncertainReason.WRITE_INTERRUPTED,
        "connection_lost": UncertainReason.CONNECTION_LOST,
        "deadline": UncertainReason.DEADLINE_EXCEEDED,
    }.get(result.failure_detail or "", UncertainReason.UNEXPECTED_ERROR)
    return SendUncertain(
        provider=provider,
        inquiry_id=inquiry_id,
        attempt_id=attempt_id,
        rfc_message_id=message.rfc_message_id,
        raw_sha256=message.raw_sha256,
        reason=reason,
        provider_error=safe_token(result.failure_detail or "unknown"),
    )


__all__ = [
    "IDEMPOTENCY_KEY_RE",
    "MAX_RECONCILE_MESSAGE_IDS",
    "MAX_RECONCILE_WINDOW",
    "PROVIDER_CONTRACT_VERSION",
    "USABLE_ALIAS_STATUSES",
    "AccessToken",
    "AliasStatus",
    "AttemptOutcomeMapping",
    "CorrelatedReply",
    "HttpResult",
    "ProviderAttachmentRef",
    "ProviderCapabilities",
    "ProviderHealth",
    "ProviderHealthStatus",
    "ProviderReceipt",
    "ReconcileFoundSent",
    "ReconcileNotFoundYet",
    "ReconcileOutcome",
    "ReconcileProvenNotSubmitted",
    "ReconcileProviderUnavailable",
    "ReconcileResult",
    "ReconcileWindow",
    "ReplyFetchResult",
    "SendAccepted",
    "SendDefiniteFailure",
    "SendFailureReason",
    "SendOutcome",
    "SendUncertain",
    "SenderProvider",
    "SenderVerification",
    "SentEvidence",
    "TokenProvider",
    "TokenUnavailable",
    "TransportFailure",
    "UncertainReason",
    "attempt_outcome",
    "canonical_or_none",
    "http_call",
    "local_refusal",
    "normalize_search_ids",
    "parse_retry_after",
    "reconciliation_evidence",
    "safe_opaque_id",
    "safe_token",
    "same_account_address",
    "send_precondition_problems",
    "uncertain_from_transport",
]
