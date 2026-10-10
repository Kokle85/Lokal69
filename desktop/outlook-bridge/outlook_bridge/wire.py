"""Wire models exchanged with the mailbox-worker API (client side).

``WorkerSendIntent``/``WorkerSendReport``/``WorkerAccountReport``/``WorkerHeartbeat`` mirror the
backend models in ``suv_deals.integrations.email_providers.outlook_local`` field for field
(``OutlookSendIntent``, ``OutlookSendReport``, ``OutlookAccountReport``, ``OutlookHeartbeat``);
contract tests round-trip them through the backend models. They are duplicated rather than
imported so the desktop worker does not load the backend settings/provider stack.

A send intent is everything the worker may put into ONE ``MailItem``: exactly one ``To``
recipient, the verified sender account, plain-text subject/body exactly as given, no CC/BCC, no
attachments. ``validate_intent_integrity`` re-checks the shape before anything is sent.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.canary import (
    CanaryClaimDecision,
    CanaryIntent,
    CanaryIntentBatch,
    CanaryReplyReport,
    CanaryReport,
)
from suv_deals.domain.enums import ReplyMessageType
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.replies import ReplyIngestRequest, canonical_address, normalize_message_id
from suv_deals.domain.seller_templates import (
    SCOPE_HASH,
    TEMPLATES,
    InquiryPlaceholders,
    RenderedMessage,
    SellerTemplate,
    rendering_problems,
)

WIRE_SCHEMA_VERSION: Final = "1.0"
#: Returned original Message-IDs a bounce/delivery notice upload may carry.
MAX_RETURNED_MESSAGE_IDS: Final = 20
DELIVERY_REPORT_TYPES: Final = frozenset({ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE})
MAX_INTENT_SUBJECT_CHARS: Final = 200  # mime_builder.MAX_SUBJECT_CHARS
MAX_INTENT_BODY_CHARS: Final = 4_000  # mime_builder.MAX_BODY_CHARS
MAX_INTENT_TTL: Final = timedelta(hours=48)
_UUID_TEXT: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_INQUIRY_MSGID_RE: Final = re.compile(
    rf"^<inquiry-(?P<uuid>{_UUID_TEXT})\.(?P<attempt>[1-9][0-9]?)@(?P<domain>[a-z0-9.-]{{1,253}})>$"
)
_INQUIRY_REF_RE: Final = re.compile(rf"^inquiry-(?P<uuid>{_UUID_TEXT})$")
_WORKER_ID: Final = r"^[A-Za-z0-9._:-]+$"
_HEX64: Final = r"^[0-9a-f]{64}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _aware(value: datetime) -> datetime:
    return ensure_utc(value)


def parse_inquiry_message_id(value: str | None) -> tuple[UUID, int] | None:
    """``(inquiry_id, attempt)`` for a Message-ID in this system's format, else ``None``."""
    if not isinstance(value, str):
        return None
    match = _INQUIRY_MSGID_RE.fullmatch(value.strip())
    if match is None:
        return None
    return UUID(match.group("uuid")), int(match.group("attempt"))


def is_own_message_id_format(value: str) -> bool:
    """Whether a Message-ID has this system's ``<inquiry-<uuid>.<n>@domain>`` shape."""
    return parse_inquiry_message_id(value) is not None


def canonical_message(subject: str, body: str) -> dict[str, str]:
    """Same canonical form as ``seller_templates.canonical_message`` (contract-tested)."""
    return {
        "subject": unicodedata.normalize("NFC", subject),
        "body": unicodedata.normalize("NFC", body.replace("\r\n", "\n").replace("\r", "\n")),
    }


def message_body_hash(subject: str, body: str) -> str:
    return sha256_json(canonical_message(subject, body))


def _plain_address(value: str | None) -> bool:
    return value is not None and canonical_address(value) == value and value.isascii()


# =============================================================================================
# Send intents (outlook_local route)
# =============================================================================================


class WorkerSendIntent(BaseModel):
    """Mirror of the backend ``OutlookSendIntent`` (spec 37.5/37.6; ADR 0002 addendum)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    intent_id: UUID
    inquiry_id: UUID
    attempt_number: int = Field(ge=1, le=99)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    mailbox_binding_id: UUID
    binding_id: UUID
    binding_version: int = Field(ge=1)
    account_id: str = Field(min_length=1, max_length=320)
    from_address: str = Field(max_length=254)
    from_display_name: str = Field(min_length=1, max_length=64)
    to_address: str = Field(max_length=254)
    reply_to_address: str | None = Field(default=None, max_length=254)
    subject: str = Field(min_length=1, max_length=MAX_INTENT_SUBJECT_CHARS)
    body_text: str = Field(min_length=1, max_length=MAX_INTENT_BODY_CHARS)
    rfc_message_id: str = Field(max_length=998)
    inquiry_ref: str = Field(max_length=64)
    body_hash: str = Field(pattern=_HEX64)
    mime_sha256: str = Field(pattern=_HEX64)
    created_at: datetime
    not_after: datetime
    #: Set by the server for an intent whose validity ended before any worker claimed it (the
    #: backend reaped it as uncertain): the worker must refuse it as ``intent_expired`` whatever
    #: its own clock says, so the inquiry can be reconciled instead of staying uncertain.
    expired: bool = False

    @field_validator("created_at", "not_after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> WorkerSendIntent:
        parsed = parse_inquiry_message_id(self.rfc_message_id)
        if parsed is None or parsed != (self.inquiry_id, self.attempt_number):
            raise ValueError("intent Message-ID does not belong to this inquiry attempt")
        ref = _INQUIRY_REF_RE.fullmatch(self.inquiry_ref)
        if ref is None or UUID(ref.group("uuid")) != self.inquiry_id:
            raise ValueError("inquiry reference does not belong to this inquiry")
        if not self.created_at < self.not_after <= self.created_at + MAX_INTENT_TTL:
            raise ValueError("intent validity window is invalid")
        return self

    def is_expired(self, now: datetime) -> bool:
        """Past its validity (by the worker's clock) or flagged expired by the server."""
        return self.expired or _aware(now) >= self.not_after

    def same_intent(self, other: WorkerSendIntent) -> bool:
        """The same message content (the server's ``expired`` flag is listing state, not content)."""
        return self.model_dump(exclude={"expired"}) == other.model_dump(exclude={"expired"})


_CONTROL_RE: Final = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)
_SUBJECT_FORBIDDEN_RE: Final = re.compile(
    r"[\x00-\x1f\x7f\x85\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)
_MARKUP_RE: Final = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


_TEMPLATE_GROUPS: Final[dict[str, str]] = {
    "vehicle_label": r"(?P<vehicle_label>[^\n]+?)",
    "listing_reference": r"(?P<listing_reference>[^\n]+?)",
    "listing_url": r"(?P<listing_url>\S+)",
    "verified_sender_display_name": r"(?P<verified_sender_display_name>[^\n]+?)",
}
_TEMPLATE_PLACEHOLDER_RE: Final = re.compile(r"\\\{\\\{([a-z_]+)\\\}\\\}")


def _template_pattern(text: str) -> re.Pattern[str]:
    """``text`` with every ``{{placeholder}}`` turned into a named capture group."""
    return re.compile(_TEMPLATE_PLACEHOLDER_RE.sub(lambda m: _TEMPLATE_GROUPS[m.group(1)], re.escape(text)))


_SELLER_TEMPLATE_PATTERNS: Final[tuple[tuple[SellerTemplate, re.Pattern[str], re.Pattern[str]], ...]] = tuple(
    (template, _template_pattern(template.subject), _template_pattern(template.body))
    for template in TEMPLATES.values()
    if template.kind == "seller_inquiry"
)


def template_scope_problems(subject: str, body: str, *, from_display_name: str) -> tuple[str, ...]:
    """Prove locally that ``subject``/``body`` are an exact registered seller-inquiry rendering.

    The worker never sends free text: the message must be byte-for-byte one of the versioned
    spec 37.4 templates (DE/IT/FR/EN) with placeholder values that pass the shared domain
    validation and the bounded three-question scope validator (``seller_templates``), and its
    signature must be the verified sender display name of the intent. An intent that adds an
    offer, a commitment, a follow-up, extra questions or personal data is refused before
    ``.Send`` even if the backend (or anything between it and this machine) produced it.
    """
    canonical = canonical_message(subject, body)
    for template, subject_re, body_re in _SELLER_TEMPLATE_PATTERNS:
        subject_match = subject_re.fullmatch(canonical["subject"])
        body_match = body_re.fullmatch(canonical["body"])
        if subject_match is None or body_match is None:
            continue
        try:
            placeholders = InquiryPlaceholders(
                vehicle_label=subject_match.group("vehicle_label"),
                listing_reference=subject_match.group("listing_reference"),
                listing_url=body_match.group("listing_url"),
                sender_display_name=body_match.group("verified_sender_display_name"),
            )
            rendered = RenderedMessage(
                template_id=template.template_id,
                template_version=template.version,
                template_hash=template.template_hash(),
                kind=template.kind,
                language=template.language,
                subject=canonical["subject"],
                body=canonical["body"],
                placeholders=placeholders,
                body_hash=message_body_hash(subject, body),
                scope_hash=SCOPE_HASH,
            )
        except ValueError:  # pydantic ValidationError is a ValueError
            continue
        if rendering_problems(rendered):
            continue
        if placeholders.sender_display_name != from_display_name:
            return ("SENDER_DISPLAY_NAME_MISMATCH",)
        return ()
    return ("NOT_TEMPLATE_RENDERING",)


def intent_integrity_problems(intent: WorkerSendIntent) -> tuple[str, ...]:
    """Local defence-in-depth checks before ``MailItem.Send`` (codes only).

    The backend already validated the bounded inquiry scope; the worker re-checks that the
    intent is still exactly one plain-text message to one canonical recipient whose body hash
    matches and whose subject/body are an exact registered seller-template rendering inside the
    three-question scope (``template_scope_problems``), so a corrupted, altered or out-of-scope
    intent is refused before submission.
    """
    problems: list[str] = []
    if message_body_hash(intent.subject, intent.body_text) != intent.body_hash:
        problems.append("BODY_HASH_MISMATCH")
    problems.extend(
        template_scope_problems(intent.subject, intent.body_text, from_display_name=intent.from_display_name)
    )
    if _SUBJECT_FORBIDDEN_RE.search(intent.subject) or _SUBJECT_FORBIDDEN_RE.search(intent.from_display_name):
        problems.append("HEADER_INJECTION")
    if "\r" in intent.body_text or _CONTROL_RE.search(intent.body_text):
        problems.append("BODY_CONTROL_CHARACTER")
    if _MARKUP_RE.search(intent.body_text) or _MARKUP_RE.search(intent.subject):
        problems.append("RAW_MARKUP")
    for address in (intent.from_address, intent.to_address, intent.reply_to_address):
        if address is not None and not _plain_address(address):
            problems.append("ADDRESS_NOT_CANONICAL")
    if any(c in intent.to_address for c in ",;<> "):
        problems.append("EXTRA_RECIPIENT")
    if normalize_message_id(intent.rfc_message_id) is None:
        problems.append("MESSAGE_ID_INVALID")
    return tuple(dict.fromkeys(problems))


class SubmissionState(StrEnum):
    """Mirror of ``OutlookSubmissionState``."""

    REFUSED_BEFORE_SEND = "refused_before_send"
    SEND_CALL_FAILED = "send_call_failed"
    SUBMITTED_TO_OUTBOX = "submitted_to_outbox"
    SENT_ITEMS_CONFIRMED = "sent_items_confirmed"
    TRANSPORT_REJECTED = "transport_rejected"


class RefusalReason(StrEnum):
    """Mirror of ``OutlookRefusalReason``."""

    INTENT_EXPIRED = "intent_expired"
    ACCOUNT_MISMATCH = "account_mismatch"
    KILL_SWITCH = "kill_switch"
    BINDING_MISMATCH = "binding_mismatch"
    INTENT_INVALID = "intent_invalid"
    OUTLOOK_NOT_CLASSIC = "outlook_not_classic"
    MAILBOX_UNAVAILABLE = "mailbox_unavailable"
    DUPLICATE_INTENT = "duplicate_intent"
    #: Claim answer only: a waiting condition (rolling caps, seller cooldown, source pause). The
    #: worker keeps the intent waiting and claims it again later; it is never a local refusal.
    NOT_NOW = "not_now"


class Tristate(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


_SAFE_CODE_RE: Final = re.compile(r"[^A-Za-z0-9_.:-]")


def safe_code(value: str | None, limit: int = 64) -> str | None:
    if value is None:
        return None
    return _SAFE_CODE_RE.sub("_", unicodedata.normalize("NFKC", value))[:limit] or "unknown"


class WorkerSendReport(BaseModel):
    """Mirror of the backend ``OutlookSendReport``."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    intent_id: UUID
    inquiry_id: UUID
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)
    state: SubmissionState
    refusal_reason: RefusalReason | None = None
    account_smtp_address_used: str | None = Field(default=None, max_length=254)
    observed_internet_message_id: str | None = Field(default=None, max_length=998)
    outbox_pending: Tristate = Tristate.UNKNOWN
    sent_items_present: bool = False
    error_code: str | None = Field(default=None, max_length=64)
    reported_at: datetime
    sent_at: datetime | None = None

    @field_validator("reported_at", "sent_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @field_validator("error_code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return safe_code(value)

    @model_validator(mode="after")
    def _consistent(self) -> WorkerSendReport:
        if (self.state == SubmissionState.REFUSED_BEFORE_SEND) != (self.refusal_reason is not None):
            raise ValueError("a refusal reason is given exactly for refused_before_send")
        if self.state == SubmissionState.SENT_ITEMS_CONFIRMED and not self.sent_items_present:
            raise ValueError("sent_items_confirmed requires Sent Items evidence")
        if self.state != SubmissionState.SENT_ITEMS_CONFIRMED and self.sent_items_present:
            raise ValueError("Sent Items evidence must be reported as sent_items_confirmed")
        return self


class ClaimDecision(BaseModel):
    """Server answer to the pre-transmission claim/revalidation of one intent."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    intent_id: UUID
    proceed: bool
    refusal_reason: RefusalReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ClaimDecision:
        if self.proceed and self.refusal_reason is not None:
            raise ValueError("a proceeding claim carries no refusal reason")
        return self


class SendIntentBatch(BaseModel):
    """``GET /v1/mail-workers/send-intents`` response."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    intents: tuple[WorkerSendIntent, ...] = Field(default=(), max_length=50)
    kill_switch_active: bool


# =============================================================================================
# Reply upload
# =============================================================================================


class ReplyUpload(ReplyIngestRequest):
    """``POST /v1/mail-workers/replies`` body: the spec 37.8 v1.0 request plus one optional
    extension (mirror of the backend ``api.schemas.MailWorkerReplyRequest``).

    ``returned_message_ids``: the Message-IDs of the returned original that a bounce or delivery
    notice carries (DSN ``Original-Message-ID``/``Message-ID`` lines of the attached original).
    The sanitiser removes the quoted original from the body, so without them the backend could not
    verify the link and would store the notice quarantined. Allowed for ``bounce`` and
    ``delivery_notice`` uploads only; at most 20; not part of the immutable source fingerprint (they
    are derived from the message itself).
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
    def _delivery_reports_only(self) -> ReplyUpload:
        if self.returned_message_ids and self.message_type not in DELIVERY_REPORT_TYPES:
            raise ValueError("returned_message_ids belong to a bounce or delivery notice only")
        return self


# =============================================================================================
# Account report and heartbeat
# =============================================================================================

AccountType = Literal["exchange", "imap", "pop3", "http", "other", "unknown"]


class WorkerAccountReport(BaseModel):
    """Mirror of the backend ``OutlookAccountReport`` (no credentials)."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)
    reported_at: datetime
    outlook_flavour: Literal["classic", "new", "unknown"]
    outlook_version: str | None = Field(default=None, max_length=64)
    stable_account_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:=-]+$")
    account_smtp_address: str = Field(max_length=254)
    account_display_name: str | None = Field(default=None, max_length=256)
    account_type: AccountType = "unknown"
    security_settings_unchanged: Literal[True] = True  # the worker never weakens Outlook security

    @field_validator("reported_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)


class WorkerHeartbeat(BaseModel):
    """Mirror of the backend ``OutlookHeartbeat``."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)
    at: datetime
    outlook_running: bool
    mailbox_connected: bool
    sync_lag_seconds: int | None = Field(default=None, ge=0)
    pending_intents: int = Field(default=0, ge=0)

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)


FolderRoleWire = Literal["inbox", "sent_items", "outbox", "junk", "rule_target", "other"]


class CheckpointReport(BaseModel):
    """One ``ops.mail_worker_checkpoints`` row as reported by the worker (hashed identities)."""

    model_config = _FROZEN

    store_id_hash: str = Field(pattern=_HEX64)
    folder_id_hash: str = Field(pattern=_HEX64)
    folder_role: FolderRoleWire
    overlap_watermark: datetime | None = None
    acknowledged_watermark: datetime | None = None
    last_complete_scan_at: datetime | None = None
    last_scan_started_at: datetime | None = None
    backlog_count: int = Field(default=0, ge=0)
    backlog_oldest_at: datetime | None = None
    gap_reasons: tuple[str, ...] = Field(default=(), max_length=30)


class GapReport(BaseModel):
    """A monitored coverage gap; the worker never claims coverage it did not have.

    A gap never ends before it starts (the server refuses such a report with ``422``);
    `GapReport.of` clamps a locally recorded end that precedes its start (clock steps).
    """

    model_config = _FROZEN

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    started_at: datetime
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> GapReport:
        if self.ended_at is not None and _aware(self.ended_at) < _aware(self.started_at):
            raise ValueError("a coverage gap cannot end before it starts")
        return self

    @classmethod
    def of(cls, kind: str, started_at: datetime, ended_at: datetime | None) -> GapReport:
        end = None if ended_at is None else max(_aware(ended_at), _aware(started_at))
        return cls(kind=kind, started_at=started_at, ended_at=end)


class HeartbeatEnvelope(BaseModel):
    """``POST /v1/mail-workers/heartbeat`` body."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    heartbeat: WorkerHeartbeat
    last_successful_reconciliation_at: datetime | None = None
    mailbox_last_sync_at: datetime | None = None
    backlog_count: int = Field(default=0, ge=0)
    backlog_oldest_age_seconds: int | None = Field(default=None, ge=0)
    unresolved_matching_gaps: int = Field(default=0, ge=0)
    checkpoints: tuple[CheckpointReport, ...] = Field(default=(), max_length=20)
    gaps: tuple[GapReport, ...] = Field(default=(), max_length=50)


class HeartbeatAck(BaseModel):
    """Server answer; ``downstream`` carries Slack/MCP health the worker cannot observe itself."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"] = WIRE_SCHEMA_VERSION
    received_at: datetime | None = None
    downstream: dict[str, str] = Field(default_factory=dict, max_length=10)

    @field_validator("downstream")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            safe_code(k, 32) or "unknown": safe_code(v, 32) or "unknown" for k, v in list(value.items())[:10]
        }


# =============================================================================================
# Activation canaries (outlook_local transport; F3, wave D2)
# =============================================================================================
#
# The canary wire models are the shared pure domain models themselves (``suv_deals.domain.canary``:
# the fixed canary rendering, Message-ID and target hash are defined once for both sides).

WorkerCanaryIntent = CanaryIntent
CanaryBatch = CanaryIntentBatch
CanaryDecision = CanaryClaimDecision
WorkerCanaryReport = CanaryReport
CanaryReplyUpload = CanaryReplyReport


def canary_intent_problems(intent: CanaryIntent) -> tuple[str, ...]:
    """Local checks of a canary before ``MailItem.Send`` (codes only; the fixed text, its hash
    and the Message-ID are already proven by the model): plain canonical sender/Reply-To
    addresses and a display name without control characters or markup."""
    problems: list[str] = []
    if _SUBJECT_FORBIDDEN_RE.search(intent.from_display_name) or _MARKUP_RE.search(intent.from_display_name):
        problems.append("HEADER_INJECTION")
    for address in (intent.from_address, intent.reply_to_address):
        if address is not None and not _plain_address(address):
            problems.append("ADDRESS_NOT_CANONICAL")
    if normalize_message_id(intent.rfc_message_id) != intent.rfc_message_id:
        problems.append("MESSAGE_ID_INVALID")
    return tuple(dict.fromkeys(problems))


__all__ = [
    "DELIVERY_REPORT_TYPES",
    "MAX_INTENT_BODY_CHARS",
    "MAX_INTENT_SUBJECT_CHARS",
    "MAX_RETURNED_MESSAGE_IDS",
    "WIRE_SCHEMA_VERSION",
    "AccountType",
    "CanaryBatch",
    "CanaryDecision",
    "CanaryReplyUpload",
    "CheckpointReport",
    "ClaimDecision",
    "FolderRoleWire",
    "GapReport",
    "HeartbeatAck",
    "HeartbeatEnvelope",
    "RefusalReason",
    "ReplyUpload",
    "SendIntentBatch",
    "SubmissionState",
    "Tristate",
    "WorkerAccountReport",
    "WorkerCanaryIntent",
    "WorkerCanaryReport",
    "WorkerHeartbeat",
    "WorkerSendIntent",
    "WorkerSendReport",
    "canary_intent_problems",
    "canonical_message",
    "intent_integrity_problems",
    "is_own_message_id_format",
    "message_body_hash",
    "parse_inquiry_message_id",
    "safe_code",
    "template_scope_problems",
]
