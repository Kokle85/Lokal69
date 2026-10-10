"""Backend side of the classic-Outlook send route (spec 37.5, 37.6; ADR 0002 addendum).

The backend never sends through Outlook itself. For ``SELLER_EMAIL_PROVIDER=outlook_local`` it
stores an ``OutlookSendIntent`` that the mailbox-bound desktop worker (classic Outlook for
Windows, signed-in interactive user, STA thread; a separate package) pulls and submits with
``MailItem.Send``. The worker reports an ``OutlookSendReport``; ``map_outlook_report`` turns it
into the shared ``SendOutcome`` vocabulary:

=========================  ===========================================================
worker state               backend outcome
=========================  ===========================================================
refused_before_send        ``definite_failure`` (pre-submission, proof: the worker never
                           called ``.Send``); ``duplicate_intent`` is ``uncertain`` because
                           the earlier attempt's result is unknown here
send_call_failed           ``uncertain`` (``.Send`` raised; Outlook may still have queued it)
submitted_to_outbox        ``uncertain`` with ``outbox_pending=yes``: a *local submission* is
                           not provider acceptance; Outlook may send it whenever it is online
sent_items_confirmed       ``accepted`` (evidence the message reached Sent Items, i.e. the
                           configured account's transport took it); no provider message id
                           or SMTP receipt exists and none is fabricated
transport_rejected         ``definite_failure`` after the hand-over (never retried)
=========================  ===========================================================

A report naming another sending account than the intent is ``uncertain`` with
``local_account_mismatch_reported`` (investigate; never resend). Until the worker reports,
``OutlookLocalProvider.send`` returns ``uncertain`` (``local_worker_handoff``) - the honest
backend view - and the inquiry keeps its reservation and quota debit. Inquiries wait in the
backend queue while the laptop/Outlook is offline; that time is a reported coverage gap.

Leaving ``uncertain`` again: ``reconcile_from_reports`` returns ``found_sent`` on Sent Items
evidence and ``proven_not_submitted`` only when *every* stored intent of the inquiry (covering
every searched Message-ID) has a definitive ``refused_before_send`` report from its own
mailbox-bound worker and no report suggesting ``.Send`` was called (an intent that expired while
the laptop was off is the common case). Retryable refusals (expired intent, Outlook not classic,
mailbox unavailable, kill switch) then go through the domain's guarded retry with a *new* intent
from the same account; the refused intent itself is never sent by the worker.

A refusal is proof only while the server never GRANTED a claim for that intent: the worker calls
``.Send`` only after a granted claim, so after one a ``refused_before_send`` report (e.g. from a
stolen worker credential, after the real worker had sent) proves nothing. ``map_outlook_report``
maps it to ``uncertain`` (``claim_granted=True``; provider error
``REFUSED_AFTER_GRANTED_CLAIM``) and ``reconcile_from_reports`` never counts a claimed intent
(``claimed_intent_ids``, from ``OutlookWorkerGateway.claimed_intents``) as proven unsent: the
inquiry is held ``uncertain`` for the owner's reconciliation, never retried automatically. A
refusal at claim time (claim not granted) or before any claim (e.g. a pre-claim expiry) stays
proof.

``IntentNotStored`` is the only gateway error that proves the intent was not stored (so the
worker can never pick it up); every other publish error is treated as an uncertain hand-over.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.enums import EmailProviderKind, Tristate
from suv_deals.domain.inquiries import SenderBinding
from suv_deals.domain.replies import InquiryBinding, normalize_message_id
from suv_deals.errors import ValidationFailed
from suv_deals.integrations.email_providers.base import (
    MAX_RECONCILE_MESSAGE_IDS,
    USABLE_ALIAS_STATUSES,
    AliasStatus,
    ProviderCapabilities,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderReceipt,
    ReconcileFoundSent,
    ReconcileNotFoundYet,
    ReconcileOutcome,
    ReconcileProvenNotSubmitted,
    ReconcileProviderUnavailable,
    ReconcileWindow,
    ReplyFetchResult,
    SendAccepted,
    SendDefiniteFailure,
    SenderVerification,
    SendFailureReason,
    SendUncertain,
    SentEvidence,
    UncertainReason,
    canonical_or_none,
    local_refusal,
    normalize_search_ids,
    safe_token,
    same_account_address,
    send_precondition_problems,
)
from suv_deals.integrations.mime_builder import (
    MAX_BODY_CHARS,
    MAX_SUBJECT_CHARS,
    BuiltMessage,
    parse_inquiry_message_id,
)

OUTLOOK_INTENT_SCHEMA_VERSION: Final = "1.0"
DEFAULT_INTENT_TTL: Final = timedelta(hours=6)
MAX_INTENT_TTL: Final = timedelta(hours=48)
MAX_ACCOUNT_REPORT_AGE: Final = timedelta(minutes=15)
HEARTBEAT_STALE_AFTER: Final = timedelta(minutes=5)
#: Desktop wire limits (``outlook_bridge.wire.WorkerSendIntent``), enforced on the backend model.
MAX_WIRE_ADDRESS_CHARS: Final = 254
MAX_WIRE_MESSAGE_ID_CHARS: Final = 998
MAX_WIRE_INQUIRY_REF_CHARS: Final = 64
#: ``provider_error`` of a pre-send refusal reported for an intent whose claim was GRANTED: no
#: proof of non-submission (the worker calls ``.Send`` only after a granted claim).
REFUSED_AFTER_GRANTED_CLAIM: Final = "refused_after_granted_claim"
_FROZEN = ConfigDict(frozen=True, extra="forbid")

CAPABILITIES: Final = ProviderCapabilities(
    kind=EmailProviderKind.OUTLOOK_LOCAL,
    delivery_mode="local_worker",
    returns_provider_message_id=False,
    returns_thread_id=False,
    preserves_client_message_id=Tristate.UNKNOWN,
    documented_send_idempotency=False,
    reconcile_by_rfc_message_id=Tristate.UNKNOWN,
    alias_verification="worker_report",
    reply_retrieval="local_worker_push",
    unverified_offline=(
        "classic_outlook_installed",
        "preserves_client_message_id",
        "sent_items_saved_for_account_type",
        "outbox_sync_timing",
    ),
)


class IntentNotStored(Exception):  # not an AppError: internal gateway contract
    """Raised by a gateway only when it is certain the intent was NOT stored."""


class OutlookAccountReport(BaseModel):
    """What the desktop worker reports about the configured Outlook account (no credentials)."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    reported_at: datetime
    outlook_flavour: Literal["classic", "new", "unknown"]
    outlook_version: str | None = Field(default=None, max_length=64)
    stable_account_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:=-]+$")
    account_smtp_address: str = Field(max_length=254)
    account_display_name: str | None = Field(default=None, max_length=256)
    account_type: Literal["exchange", "imap", "pop3", "http", "other", "unknown"] = "unknown"
    #: The worker never disables Trust Center / Object Model Guard / antivirus checks; a report
    #: claiming otherwise is not representable (validation refuses it).
    security_settings_unchanged: Literal[True] = True

    @field_validator("reported_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class OutlookHeartbeat(BaseModel):
    model_config = _FROZEN

    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    at: datetime
    outlook_running: bool
    mailbox_connected: bool
    sync_lag_seconds: int | None = Field(default=None, ge=0)
    pending_intents: int = Field(default=0, ge=0)

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class OutlookSendIntent(BaseModel):
    """Everything the worker may put into ONE ``MailItem``; nothing else is representable.

    Worker obligations (desktop package): refuse when ``not_after`` has passed, when the
    profile's account for ``from_address`` is not the bound account, when the kill switch or
    binding version changed, or when this ``intent_id`` was already attempted; set
    ``SendUsingAccount`` to that account only and never switch accounts after a failure; add
    exactly one ``To`` recipient; no CC/BCC, attachments, signature or tracking; plain-text
    body exactly as given; record ``rfc_message_id``/``inquiry_ref`` where Outlook allows.

    The model carries the desktop wire limits itself (addresses <= 254, Message-ID <= 998,
    ``inquiry_ref`` <= 64 characters and exactly ``inquiry-<inquiry_id>``), so an intent the
    backend can build is always one the worker accepts (``outlook_bridge.wire.WorkerSendIntent``).
    """

    model_config = _FROZEN

    schema_version: Literal["1.0"] = OUTLOOK_INTENT_SCHEMA_VERSION
    intent_id: UUID  # == the durable send-attempt id
    inquiry_id: UUID
    attempt_number: int = Field(ge=1, le=99)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    mailbox_binding_id: UUID
    binding_id: UUID
    binding_version: int = Field(ge=1)
    account_id: str = Field(min_length=1, max_length=320)
    from_address: str = Field(max_length=MAX_WIRE_ADDRESS_CHARS)
    from_display_name: str = Field(min_length=1, max_length=64)
    to_address: str = Field(max_length=MAX_WIRE_ADDRESS_CHARS)
    reply_to_address: str | None = Field(default=None, max_length=MAX_WIRE_ADDRESS_CHARS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body_text: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    rfc_message_id: str = Field(max_length=MAX_WIRE_MESSAGE_ID_CHARS)
    inquiry_ref: str = Field(max_length=MAX_WIRE_INQUIRY_REF_CHARS)
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    not_after: datetime

    @field_validator("created_at", "not_after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> OutlookSendIntent:
        ref = parse_inquiry_message_id(self.rfc_message_id)
        if ref is None or ref.inquiry_id != self.inquiry_id or ref.attempt_number != self.attempt_number:
            raise ValueError("intent Message-ID does not belong to this inquiry attempt")
        if self.inquiry_ref != f"inquiry-{self.inquiry_id}":
            raise ValueError("inquiry reference does not belong to this inquiry")
        if not self.created_at < self.not_after <= self.created_at + MAX_INTENT_TTL:
            raise ValueError("intent validity window is invalid")
        for address in (self.from_address, self.to_address, self.reply_to_address):
            if address is not None and canonical_or_none(address) != address:
                raise ValueError("intent addresses must be canonical")
        return self

    def expired(self, now: datetime) -> bool:
        return ensure_utc(now) >= self.not_after


def build_send_intent(
    message: BuiltMessage,
    *,
    attempt_id: UUID,
    idempotency_key: str,
    mailbox_binding_id: UUID,
    binding: SenderBinding,
    created_at: datetime,
    ttl: timedelta = DEFAULT_INTENT_TTL,
) -> OutlookSendIntent:
    """The intent for exactly the verified built message (same subject/body/addresses/ids)."""
    if binding.provider != EmailProviderKind.OUTLOOK_LOCAL:
        raise ValidationFailed("binding is not an outlook_local binding")
    created = ensure_utc(created_at)
    return OutlookSendIntent(
        intent_id=attempt_id,
        inquiry_id=message.inquiry_id,
        attempt_number=message.attempt_number,
        idempotency_key=idempotency_key,
        mailbox_binding_id=mailbox_binding_id,
        binding_id=binding.binding_id,
        binding_version=binding.binding_version,
        account_id=binding.account_id,
        from_address=message.from_address,
        from_display_name=message.from_display_name,
        to_address=message.to_address,
        reply_to_address=message.reply_to_address,
        subject=message.subject,
        body_text=message.body,
        rfc_message_id=message.rfc_message_id,
        inquiry_ref=message.inquiry_ref,
        body_hash=message.body_hash,
        mime_sha256=message.raw_sha256,
        created_at=created,
        not_after=created + ttl,
    )


class OutlookSubmissionState(StrEnum):
    REFUSED_BEFORE_SEND = "refused_before_send"
    SEND_CALL_FAILED = "send_call_failed"
    SUBMITTED_TO_OUTBOX = "submitted_to_outbox"
    SENT_ITEMS_CONFIRMED = "sent_items_confirmed"
    TRANSPORT_REJECTED = "transport_rejected"


class OutlookRefusalReason(StrEnum):
    INTENT_EXPIRED = "intent_expired"
    ACCOUNT_MISMATCH = "account_mismatch"
    KILL_SWITCH = "kill_switch"
    BINDING_MISMATCH = "binding_mismatch"
    INTENT_INVALID = "intent_invalid"
    OUTLOOK_NOT_CLASSIC = "outlook_not_classic"
    MAILBOX_UNAVAILABLE = "mailbox_unavailable"
    DUPLICATE_INTENT = "duplicate_intent"  # already attempted earlier: that result stands
    #: A claim refusal that lifts on its own (rolling caps, seller cooldown, source pause): the
    #: worker keeps the intent waiting and claims again later; nothing was sent.
    NOT_NOW = "not_now"


#: Refusals that say nothing against a later attempt of the same message from the same account
#: (the domain retry policy and dispatch preflight decide whether and when). A kill-switch refusal
#: stops this transmission only; it must not permanently fail the inquiry.
_RETRYABLE_REFUSALS: Final = frozenset(
    {
        OutlookRefusalReason.INTENT_EXPIRED,
        OutlookRefusalReason.OUTLOOK_NOT_CLASSIC,
        OutlookRefusalReason.MAILBOX_UNAVAILABLE,
        OutlookRefusalReason.KILL_SWITCH,
        OutlookRefusalReason.NOT_NOW,
    }
)


class OutlookSendReport(BaseModel):
    """The desktop worker's report for one intent (via the mailbox-bound worker API)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = OUTLOOK_INTENT_SCHEMA_VERSION
    intent_id: UUID
    inquiry_id: UUID
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    state: OutlookSubmissionState
    refusal_reason: OutlookRefusalReason | None = None
    account_smtp_address_used: str | None = Field(default=None, max_length=254)
    observed_internet_message_id: str | None = Field(default=None, max_length=998)
    outbox_pending: Tristate = Tristate.UNKNOWN
    sent_items_present: bool = False
    error_code: str | None = Field(default=None, max_length=64)
    reported_at: datetime
    sent_at: datetime | None = None

    @field_validator("reported_at", "sent_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("error_code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return None if value is None else safe_token(value)

    @model_validator(mode="after")
    def _consistent(self) -> OutlookSendReport:
        if (self.state == OutlookSubmissionState.REFUSED_BEFORE_SEND) != (self.refusal_reason is not None):
            raise ValueError("a refusal reason is given exactly for refused_before_send")
        if self.state == OutlookSubmissionState.SENT_ITEMS_CONFIRMED and not self.sent_items_present:
            raise ValueError("sent_items_confirmed requires Sent Items evidence")
        if self.state != OutlookSubmissionState.SENT_ITEMS_CONFIRMED and self.sent_items_present:
            raise ValueError("Sent Items evidence must be reported as sent_items_confirmed")
        return self


def _report_problems(intent: OutlookSendIntent, report: OutlookSendReport) -> list[str]:
    problems: list[str] = []
    if report.intent_id != intent.intent_id:
        problems.append("INTENT_MISMATCH")
    if report.inquiry_id != intent.inquiry_id:
        problems.append("INQUIRY_MISMATCH")
    if report.mailbox_binding_id != intent.mailbox_binding_id:
        problems.append("MAILBOX_MISMATCH")
    return problems


def map_outlook_report(
    intent: OutlookSendIntent,
    report: OutlookSendReport,
    *,
    observed_at: datetime,
    claim_granted: bool = False,
) -> SendAccepted | SendDefiniteFailure | SendUncertain:
    """Map a worker report onto the shared send outcome (see the module table).

    ``claim_granted``: the server granted a claim for this intent (the worker may have called
    ``.Send``), so a ``refused_before_send`` report is ``uncertain``
    (``REFUSED_AFTER_GRANTED_CLAIM``), never a proven pre-submission failure. The persistence layer
    passes it from the audited claims; it re-checks the same rule itself (defence in depth).
    """
    problems = _report_problems(intent, report)
    if problems:
        raise ValidationFailed("worker report does not belong to this intent", details={"problems": problems})
    common = {
        "provider": EmailProviderKind.OUTLOOK_LOCAL,
        "inquiry_id": intent.inquiry_id,
        "attempt_id": intent.intent_id,
        "rfc_message_id": intent.rfc_message_id,
        "raw_sha256": intent.mime_sha256,
    }
    used = report.account_smtp_address_used
    if (
        report.state != OutlookSubmissionState.REFUSED_BEFORE_SEND
        and used is not None
        and not same_account_address(used, intent.from_address)
    ):
        return SendUncertain.model_validate(
            {
                **common,
                "reason": UncertainReason.LOCAL_ACCOUNT_MISMATCH_REPORTED,
                "provider_error": report.error_code,
                "outbox_pending": report.outbox_pending,
            }
        )
    if report.state == OutlookSubmissionState.REFUSED_BEFORE_SEND:
        reason = report.refusal_reason
        if claim_granted:
            return SendUncertain.model_validate(
                {
                    **common,
                    "reason": UncertainReason.LOCAL_WORKER_NO_RESULT,
                    "provider_error": REFUSED_AFTER_GRANTED_CLAIM,
                    "outbox_pending": report.outbox_pending,
                }
            )
        if reason == OutlookRefusalReason.DUPLICATE_INTENT:
            return SendUncertain.model_validate(
                {
                    **common,
                    "reason": UncertainReason.LOCAL_WORKER_NO_RESULT,
                    "provider_error": "duplicate_intent",
                    "awaiting_local_worker": True,
                }
            )
        failure_reason = {
            OutlookRefusalReason.KILL_SWITCH: SendFailureReason.KILL_SWITCH_ACTIVE,
            OutlookRefusalReason.ACCOUNT_MISMATCH: SendFailureReason.ACCOUNT_MISMATCH,
        }.get(reason or OutlookRefusalReason.INTENT_INVALID, SendFailureReason.LOCAL_WORKER_REFUSED)
        return SendDefiniteFailure.model_validate(
            {
                **common,
                "pre_submission": True,
                "reason": failure_reason,
                "retryable": reason in _RETRYABLE_REFUSALS,
                "proof": "local_validation_failed_before_submit",
                "provider_error": reason.value if reason else None,
            }
        )
    if report.state == OutlookSubmissionState.SEND_CALL_FAILED:
        return SendUncertain.model_validate(
            {
                **common,
                "reason": UncertainReason.LOCAL_SEND_CALL_FAILED,
                "provider_error": report.error_code,
                "outbox_pending": report.outbox_pending,
            }
        )
    if report.state == OutlookSubmissionState.SUBMITTED_TO_OUTBOX:
        return SendUncertain.model_validate(
            {
                **common,
                "reason": UncertainReason.LOCAL_SUBMISSION_PENDING,
                "awaiting_local_worker": True,
                "outbox_pending": Tristate.YES
                if report.outbox_pending == Tristate.UNKNOWN
                else report.outbox_pending,
            }
        )
    if report.state == OutlookSubmissionState.SENT_ITEMS_CONFIRMED:
        return SendAccepted.model_validate(
            {
                **common,
                "observed_rfc_message_id": normalize_message_id(report.observed_internet_message_id),
                "receipt": ProviderReceipt(kind="outlook_sent_items", observed_at=observed_at),
                "accepted_at": report.sent_at or report.reported_at,
            }
        )
    return SendDefiniteFailure.model_validate(
        {
            **common,
            "pre_submission": False,
            "reason": SendFailureReason.TRANSPORT_REJECTED,
            "retryable": False,
            "provider_error": report.error_code,
        }
    )


def _definitive_refusal(
    intent: OutlookSendIntent, reports: Sequence[OutlookSendReport], *, claimed: bool = False
) -> str | None:
    """The refusal reason when the intent's own worker definitively refused it before ``.Send``.

    ``None`` unless at least one report from the intent's mailbox binding is a non-duplicate
    ``refused_before_send`` and no report for the intent (from any mailbox) indicates that
    ``.Send`` may have been called or that a copy may sit in the Outbox. A ``duplicate_intent``
    refusal only says that an *earlier* attempt of this intent exists, whose result is unknown.
    A ``claimed`` intent (the server granted a claim, so ``.Send`` may have been called) is never
    definitively refused.
    """
    if claimed:
        return None
    own = [r for r in reports if r.intent_id == intent.intent_id and r.inquiry_id == intent.inquiry_id]
    if any(
        r.state != OutlookSubmissionState.REFUSED_BEFORE_SEND
        or r.refusal_reason == OutlookRefusalReason.DUPLICATE_INTENT
        or r.outbox_pending == Tristate.YES
        for r in own
    ):
        return None
    refusals = [r for r in own if r.mailbox_binding_id == intent.mailbox_binding_id]
    if not refusals or refusals[-1].refusal_reason is None:
        return None
    return refusals[-1].refusal_reason.value


def reconcile_from_reports(
    inquiry_id: UUID,
    rfc_message_ids: Sequence[str],
    reports: Sequence[OutlookSendReport],
    *,
    worker_online: bool,
    intents: Sequence[OutlookSendIntent] = (),
    claimed_intent_ids: Collection[UUID] = (),
) -> ReconcileOutcome:
    """Sent Items evidence or proven refusal from worker reports; absence never proves anything.

    ``intents`` are every send intent stored for the inquiry (all attempts). Non-submission is
    proven only when each of them was definitively refused by its own worker and every searched
    Message-ID belongs to one of them (an attempt whose intent may or may not have been stored
    is never assumed unsent). ``claimed_intent_ids`` are the intents a claim was GRANTED for (the
    worker may have called ``.Send``): a refusal reported for one of them proves nothing.
    """
    claimed = frozenset(claimed_intent_ids)
    ids = normalize_search_ids(rfc_message_ids)
    relevant = [r for r in reports if r.inquiry_id == inquiry_id]
    for report in relevant:
        if report.state == OutlookSubmissionState.SENT_ITEMS_CONFIRMED:
            observed = normalize_message_id(report.observed_internet_message_id)
            return ReconcileFoundSent(
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                inquiry_id=inquiry_id,
                evidence=SentEvidence(
                    matched_by="local_sent_items",
                    location="outlook:sent_items",
                    rfc_message_id=observed,
                    sent_at=report.sent_at,
                ),
            )
    own_intents = list({i.intent_id: i for i in intents if i.inquiry_id == inquiry_id}.values())
    covered = {normalize_message_id(i.rfc_message_id) for i in own_intents}
    if own_intents and len(own_intents) <= MAX_RECONCILE_MESSAGE_IDS and set(ids) <= covered:
        reasons = [
            _definitive_refusal(intent, relevant, claimed=intent.intent_id in claimed)
            for intent in own_intents
        ]
        if all(reason is not None for reason in reasons):
            return ReconcileProvenNotSubmitted(
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                inquiry_id=inquiry_id,
                proof="local_validation_failed_before_submit",
                refused_intent_ids=tuple(dict.fromkeys(i.intent_id for i in own_intents)),
                refusal_reasons=tuple(r for r in reasons if r is not None),
            )
    if not worker_online:
        return ReconcileProviderUnavailable(
            provider=EmailProviderKind.OUTLOOK_LOCAL, inquiry_id=inquiry_id, reason="worker_offline"
        )
    pending = any(
        r.state == OutlookSubmissionState.SUBMITTED_TO_OUTBOX or r.outbox_pending == Tristate.YES
        for r in relevant
    )
    no_outbox = bool(relevant) and all(r.outbox_pending == Tristate.NO for r in relevant)
    return ReconcileNotFoundYet(
        provider=EmailProviderKind.OUTLOOK_LOCAL,
        inquiry_id=inquiry_id,
        searched_message_ids=ids,
        pending_in_drafts_or_outbox=Tristate.YES
        if pending
        else Tristate.NO
        if no_outbox
        else Tristate.UNKNOWN,
    )


class OutlookWorkerGateway(Protocol):
    """Persistence-backed bridge to the desktop worker (implemented by another package)."""

    async def publish_intent(self, intent: OutlookSendIntent) -> None:
        """Idempotent on ``intent_id``. Raise ``IntentNotStored`` only when nothing was stored."""
        ...

    async def latest_account_report(self, mailbox_binding_id: UUID) -> OutlookAccountReport | None: ...

    async def latest_heartbeat(self, mailbox_binding_id: UUID) -> OutlookHeartbeat | None: ...

    async def reports_for(self, inquiry_id: UUID) -> Sequence[OutlookSendReport]: ...

    async def intents_for(self, inquiry_id: UUID) -> Sequence[OutlookSendIntent]:
        """Every intent ever stored for the inquiry (all attempts, any state)."""
        ...

    async def claimed_intents(self, inquiry_id: UUID) -> frozenset[UUID]:
        """The intents of the inquiry a claim was GRANTED for (``.Send`` may have been called)."""
        ...


class OutlookLocalProvider:
    """``SenderProvider`` facade for the local worker route (intent hand-over, report mapping)."""

    def __init__(
        self,
        *,
        binding: SenderBinding,
        mailbox_binding_id: UUID,
        gateway: OutlookWorkerGateway,
        clock: Clock | None = None,
        intent_ttl: timedelta = DEFAULT_INTENT_TTL,
    ) -> None:
        if binding.provider != EmailProviderKind.OUTLOOK_LOCAL:
            raise ValidationFailed("binding is not an outlook_local binding")
        if not timedelta(minutes=5) <= intent_ttl <= MAX_INTENT_TTL:
            raise ValidationFailed("intent TTL out of range")
        self._binding = binding
        self._mailbox_binding_id = mailbox_binding_id
        self._gateway = gateway
        self._clock = clock or SystemClock()
        self._ttl = intent_ttl

    @property
    def kind(self) -> EmailProviderKind:
        return EmailProviderKind.OUTLOOK_LOCAL

    @property
    def binding(self) -> SenderBinding:
        return self._binding

    @property
    def capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    def __repr__(self) -> str:
        return f"OutlookLocalProvider(binding_id={self._binding.binding_id})"

    async def _worker_online(self) -> tuple[bool, OutlookHeartbeat | None]:
        heartbeat = await self._gateway.latest_heartbeat(self._mailbox_binding_id)
        if heartbeat is None or heartbeat.mailbox_binding_id != self._mailbox_binding_id:
            return False, None  # another mailbox's worker never vouches for this one
        fresh = self._clock.now() - heartbeat.at <= HEARTBEAT_STALE_AFTER
        return fresh and heartbeat.outlook_running and heartbeat.mailbox_connected, heartbeat

    async def verify_account(self) -> SenderVerification:
        now = self._clock.now()
        base = {
            "provider": EmailProviderKind.OUTLOOK_LOCAL,
            "checked_at": now,
            "configured_account_id": self._binding.account_id,
            "configured_from": self._binding.from_address,
            "configured_reply_to": self._binding.reply_to_address,
            "configured_display_name": self._binding.display_name,
            "reply_to_status": AliasStatus.NOT_CHECKED if self._binding.reply_to_address else None,
            "capabilities": CAPABILITIES,
        }
        report = await self._gateway.latest_account_report(self._mailbox_binding_id)
        if report is None:
            return SenderVerification.model_validate(
                {**base, "health": ProviderHealthStatus.UNAVAILABLE, "problems": ("WORKER_REPORT_MISSING",)}
            )
        problems: list[str] = []
        warnings: list[str] = []
        if report.mailbox_binding_id != self._mailbox_binding_id:
            problems.append("MAILBOX_BINDING_MISMATCH")
        if now - report.reported_at > MAX_ACCOUNT_REPORT_AGE:
            problems.append("WORKER_REPORT_STALE")
        if report.outlook_flavour != "classic":
            problems.append(
                "NEW_OUTLOOK_UNSUPPORTED" if report.outlook_flavour == "new" else "OUTLOOK_UNKNOWN"
            )
        if not report.security_settings_unchanged:
            problems.append("SECURITY_SETTINGS_WEAKENED")
        account_ok = self._binding.account_id == report.stable_account_key or same_account_address(
            self._binding.account_id, report.account_smtp_address
        )
        if not account_ok:
            problems.append("ACCOUNT_MISMATCH")
        elif self._binding.account_id != report.stable_account_key:
            warnings.append("ACCOUNT_ID_IS_ADDRESS")

        def status(address: str) -> AliasStatus:
            if same_account_address(address, report.account_smtp_address):
                return AliasStatus.PRIMARY
            return AliasStatus.NOT_VERIFIABLE  # Outlook offers no send-as alias verification here

        from_status = status(self._binding.from_address)
        reply_status = status(self._binding.reply_to_address) if self._binding.reply_to_address else None
        if from_status not in USABLE_ALIAS_STATUSES:
            problems.append("FROM_NOT_VERIFIED")
        if reply_status is not None and reply_status not in USABLE_ALIAS_STATUSES:
            problems.append("REPLY_TO_NOT_VERIFIED")
        if report.account_display_name and report.account_display_name != self._binding.display_name:
            warnings.append("PROVIDER_DISPLAY_NAME_DIFFERS")
        online, _heartbeat = await self._worker_online()
        return SenderVerification.model_validate(
            {
                **base,
                "stable_account_id": report.stable_account_key,
                "account_email": canonical_or_none(report.account_smtp_address),
                "provider_display_name": report.account_display_name,
                "from_status": from_status,
                "reply_to_status": reply_status,
                "health": ProviderHealthStatus.OK if online else ProviderHealthStatus.DEGRADED,
                "problems": tuple(problems) if online else (*problems, "WORKER_OFFLINE"),
                "warnings": tuple(warnings),
            }
        )

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted | SendDefiniteFailure | SendUncertain:
        """Hand the intent to the worker queue. The result is uncertain until the worker reports."""
        problems = send_precondition_problems(
            message,
            self._binding,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            inquiry_id=inquiry_id,
            idempotency_key=idempotency_key,
        )
        if problems:
            return local_refusal(
                message,
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                problems=problems,
            )
        try:
            intent = build_send_intent(
                message,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                mailbox_binding_id=self._mailbox_binding_id,
                binding=self._binding,
                created_at=self._clock.now(),
                ttl=self._ttl,
            )
        except (ValueError, ValidationFailed):  # refused locally: nothing was handed over
            return local_refusal(
                message,
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                problems=("INTENT_INVALID",),
            )
        common = {
            "provider": EmailProviderKind.OUTLOOK_LOCAL,
            "inquiry_id": inquiry_id,
            "attempt_id": attempt_id,
            "rfc_message_id": message.rfc_message_id,
            "raw_sha256": message.raw_sha256,
        }
        try:
            await self._gateway.publish_intent(intent)
        except IntentNotStored:
            return SendDefiniteFailure.model_validate(
                {
                    **common,
                    "pre_submission": True,
                    "reason": SendFailureReason.LOCAL_HANDOFF_FAILED,
                    "retryable": True,
                    "proof": "local_validation_failed_before_submit",
                }
            )
        except Exception:
            # The intent may have been stored (commit succeeded, acknowledgement lost): the worker
            # could still send it, so this is never a pre-submission failure.
            return SendUncertain.model_validate(
                {**common, "reason": UncertainReason.LOCAL_WORKER_HANDOFF, "awaiting_local_worker": True}
            )
        return SendUncertain.model_validate(
            {**common, "reason": UncertainReason.LOCAL_WORKER_HANDOFF, "awaiting_local_worker": True}
        )

    async def reconcile(
        self,
        *,
        inquiry_id: UUID,
        rfc_message_ids: Sequence[str],
        window: ReconcileWindow,
        provider_message_ids: Sequence[str] = (),
    ) -> ReconcileOutcome:
        try:
            normalize_search_ids(rfc_message_ids)
        except ValueError as exc:
            raise ValidationFailed("invalid reconciliation Message-IDs") from exc
        del window, provider_message_ids
        reports = await self._gateway.reports_for(inquiry_id)
        intents = await self._gateway.intents_for(inquiry_id)
        claimed = await self._gateway.claimed_intents(inquiry_id)
        online, _heartbeat = await self._worker_online()
        return reconcile_from_reports(
            inquiry_id,
            rfc_message_ids,
            reports,
            worker_online=online,
            intents=intents,
            claimed_intent_ids=claimed,
        )

    async def fetch_correlated_replies(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        since: datetime,
        cursor: str | None = None,
        max_messages: int | None = None,
    ) -> ReplyFetchResult:
        """Replies arrive through ``POST /v1/mail-workers/replies`` from the desktop worker."""
        del mailbox_binding_id, bindings, since, max_messages
        return ReplyFetchResult(
            provider=EmailProviderKind.OUTLOOK_LOCAL, retrieval_mode="local_worker_push", next_cursor=cursor
        )

    async def health(self) -> ProviderHealth:
        now = self._clock.now()
        online, heartbeat = await self._worker_online()
        if heartbeat is None:
            return ProviderHealth(
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                status=ProviderHealthStatus.UNAVAILABLE,
                checked_at=now,
                problems=("WORKER_HEARTBEAT_MISSING",),
            )
        problems: list[str] = []
        if now - heartbeat.at > HEARTBEAT_STALE_AFTER:
            problems.append("WORKER_HEARTBEAT_STALE")
        if not heartbeat.outlook_running:
            problems.append("OUTLOOK_NOT_RUNNING")
        if not heartbeat.mailbox_connected:
            problems.append("MAILBOX_DISCONNECTED")
        return ProviderHealth(
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            status=ProviderHealthStatus.OK if online else ProviderHealthStatus.UNAVAILABLE,
            checked_at=now,
            problems=tuple(problems),
            last_worker_heartbeat_age_seconds=max(0, int((now - heartbeat.at).total_seconds())),
        )


__all__ = [
    "CAPABILITIES",
    "DEFAULT_INTENT_TTL",
    "HEARTBEAT_STALE_AFTER",
    "MAX_ACCOUNT_REPORT_AGE",
    "MAX_WIRE_ADDRESS_CHARS",
    "MAX_WIRE_INQUIRY_REF_CHARS",
    "MAX_WIRE_MESSAGE_ID_CHARS",
    "OUTLOOK_INTENT_SCHEMA_VERSION",
    "REFUSED_AFTER_GRANTED_CLAIM",
    "IntentNotStored",
    "OutlookAccountReport",
    "OutlookHeartbeat",
    "OutlookLocalProvider",
    "OutlookRefusalReason",
    "OutlookSendIntent",
    "OutlookSendReport",
    "OutlookSubmissionState",
    "OutlookWorkerGateway",
    "build_send_intent",
    "map_outlook_report",
    "reconcile_from_reports",
]
