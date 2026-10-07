"""Seller inquiry, seller reply and inquiry-control read models (spec 37.1-37.8, 23).

Shared by the MCP tools ``seller_inquiries_get`` / ``seller_replies_get`` /
``seller_inquiries_pause`` (``mcp.schemas.V11_TOOLS``) and the dashboard inquiry routes
(``api.schemas.V11_DASHBOARD_ROUTES``).

Privacy rules (spec 37.1, 37.7, 37.8):

- A seller's e-mail address (the verified recipient of an inquiry, the From of a reply) is shown
  only to principals holding ``config:admin`` (the owner); everyone else sees the domain plus
  ``address_redacted: true`` (`recipient_address_visible`). The owner's own mailbox address and
  the sender account id never appear in a read model; the sender is shown by provider, binding
  version and verified display name only.
- Message text is untrusted seller data (replies) or the exact registered template rendering
  (inquiries); both are returned as data, never as instructions. The Macedonian preview of an
  inquiry is an informational audit view, never an approval draft (``approval_required`` is
  always ``false``: no per-message or first-template approval exists).
- Reply claims are seller statements: a price is always an *unaccepted* seller quote, a "sold"
  statement is ``sold_claimed`` evidence, and nothing here records a purchase or an agreement.
- Attachments are metadata only (name, MIME type, size, SHA-256, policy decision): no bytes, no
  local paths or locators, no URLs, no signed access links.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.enums import (
    Availability,
    EmailProviderKind,
    InquiryReadiness,
    InquiryState,
    MessageLanguage,
    ReplyMessageType,
    Scope,
    SuppressionReason,
    ValuationState,
)
from suv_deals.domain.replies import (
    AvailabilitySummary,
    DocumentClaimStatus,
    DocumentKind,
    InquiryQuestion,
    PriceCondition,
    ReplyClaims,
    RequestKind,
)
from suv_deals.views.common import (
    AmountView,
    DecimalStr,
    Sha256Hex,
    UtcDatetime,
    ViewModel,
    decimal_str,
)

INQUIRY_PURPOSE: Final = "initial_availability_documents_price"
#: Spec 37.5 ceilings; the owner may reduce (or set 0), never raise them.
MAX_PER_24H_CEILING: Final = 2
MAX_PER_15D_CEILING: Final = 5
MAX_ATTEMPTS: Final = 3
MAX_REPLY_ATTACHMENTS: Final = 20
MAX_LIST_ITEMS: Final = 100

_ADDRESS_DOMAIN_PATTERN: Final = r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"
_REASON_CODE_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
_TEMPLATE_ID_PATTERN: Final = r"^seller_initial_(de|it|fr|en)_v[1-9][0-9]*$"

VehicleKind = Literal["vehicle_cluster", "listing_incarnation"]
ContactStatus = Literal["verified", "unverified", "unavailable", "changed", "unknown"]
ContactKind = Literal["ad_email", "marketplace_relay", "official_dealer_contact"]
LanguageStatus = Literal["resolved", "language_unresolved", "unsupported_language", "unknown"]
AttemptOutcome = Literal["running", "accepted", "pre_submission_failure", "definite_rejection", "uncertain"]
ReconciledOutcome = Literal["accepted", "proven_not_submitted"]
InquiryMode = Literal["disabled_until_sender_ready", "automatic", "paused"]
CorrelationStatus = Literal["matched", "quarantined", "verified_match"]
ProcessingState = Literal["stored", "processed", "failed"]
AttachmentAction = Literal["allow_vehicle_document", "quarantine_sensitive", "reject"]
QuoteKind = Literal["single", "range", "minimum"]
ReasonCode = str


def recipient_address_visible(scopes: Iterable[Scope]) -> bool:
    """Whether a principal with ``scopes`` may see a seller's full e-mail address (owner only)."""
    return Scope.CONFIG_ADMIN in set(scopes)


def address_domain(address: str | None) -> str | None:
    """The lower-case domain of an address (``None`` when absent or malformed)."""
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower()
    return domain or None


# --------------------------------------------------------------------------- shared parts


class InquiryVehicleRef(ViewModel):
    """The canonical vehicle of an inquiry: a confirmed cluster or one listing incarnation."""

    vehicle_kind: VehicleKind
    vehicle_cluster_id: UUID | None
    listing_id: UUID = Field(description="The qualifying listing (source advertisement).")
    source_key: str | None = Field(max_length=80)
    listing_reference: str | None = Field(max_length=200)
    listing_url: str | None = Field(max_length=2048)

    @model_validator(mode="after")
    def _kind(self) -> InquiryVehicleRef:
        if (self.vehicle_kind == "vehicle_cluster") != (self.vehicle_cluster_id is not None):
            raise ValueError("a cluster identity names its cluster; a listing identity has none")
        return self


class RecipientView(ViewModel):
    """The verified recipient (spec 37.3). ``address`` is ``null`` unless the caller is the owner."""

    contact_id: UUID | None
    verification_status: ContactStatus
    contact_kind: ContactKind | None
    address: str | None = Field(max_length=320)
    address_domain: str | None = Field(max_length=253, pattern=_ADDRESS_DOMAIN_PATTERN)
    address_redacted: bool
    language: MessageLanguage | None
    language_status: LanguageStatus
    verified_at: UtcDatetime | None

    @model_validator(mode="after")
    def _redaction(self) -> RecipientView:
        if self.address_redacted and self.address is not None:
            raise ValueError("a redacted recipient carries no address")
        return self

    @classmethod
    def build(
        cls,
        *,
        address: str | None,
        show_address: bool,
        contact_id: UUID | None,
        verification_status: ContactStatus,
        contact_kind: ContactKind | None,
        language: MessageLanguage | None,
        language_status: LanguageStatus,
        verified_at: UtcDatetime | None,
    ) -> RecipientView:
        """Apply the owner-only address rule (`recipient_address_visible`)."""
        return cls(
            contact_id=contact_id,
            verification_status=verification_status,
            contact_kind=contact_kind,
            address=address if show_address else None,
            address_domain=address_domain(address),
            address_redacted=address is not None and not show_address,
            language=language,
            language_status=language_status,
            verified_at=verified_at,
        )


class SenderRef(ViewModel):
    """The bound sending identity, without addresses, account ids or credentials."""

    sender_binding_id: UUID | None
    binding_version: int | None = Field(ge=1)
    provider: EmailProviderKind | None
    display_name: str | None = Field(max_length=64)


class InquiryQualificationView(ViewModel):
    """The qualification snapshot the inquiry is bound to (stale facts cancel queued work)."""

    listing_revision_id: UUID | None
    revision_number: int | None = Field(ge=0)
    semantic_hash: Sha256Hex | None
    asking_price: AmountView
    availability: Availability | None
    readiness: InquiryReadiness
    readiness_reasons: tuple[ReasonCode, ...] = Field(max_length=60)
    rules_version: str | None = Field(max_length=80)
    evaluated_at: UtcDatetime | None


class InquiryAuthorizationRef(ViewModel):
    """The standing-authorization version the inquiry relies on (spec 37.1)."""

    authorization_id: UUID | None
    version: int | None = Field(ge=1)
    fingerprint: Sha256Hex | None


class InquiryTemplateView(ViewModel):
    template_id: str | None = Field(pattern=_TEMPLATE_ID_PATTERN)
    template_version: int | None = Field(ge=1)
    template_set_version: str | None = Field(max_length=80)
    scope_hash: Sha256Hex | None
    body_hash: Sha256Hex | None


class InquiryMessageView(ViewModel):
    """The exact message (registered template rendering) and its informational MK preview."""

    original_subject: str = Field(min_length=1, max_length=200)
    original_body: str = Field(min_length=1, max_length=2000)
    mk_preview_subject: str | None = Field(max_length=200)
    mk_preview_body: str | None = Field(max_length=2000)
    preview_is_informational: Literal[True] = True


class SendAttemptSummary(ViewModel):
    attempt_number: int = Field(ge=1, le=MAX_ATTEMPTS)
    provider: EmailProviderKind
    outcome: AttemptOutcome
    send_intent_committed_at: UtcDatetime
    finished_at: UtcDatetime | None
    reconciled_outcome: ReconciledOutcome | None
    reconciled_at: UtcDatetime | None
    submission_uncertain: bool
    error_code: str | None = Field(max_length=80, pattern=_REASON_CODE_PATTERN)


class SendAttemptsView(ViewModel):
    """Every transmission attempt (at most 3); an uncertain one is never resent blindly."""

    count: int = Field(ge=0, le=MAX_ATTEMPTS)
    last_outcome: AttemptOutcome | None
    uncertain: bool
    attempts: tuple[SendAttemptSummary, ...] = Field(max_length=MAX_ATTEMPTS)


class InquiryTimestamps(ViewModel):
    created_at: UtcDatetime
    state_changed_at: UtcDatetime
    reserved_at: UtcDatetime | None
    queued_at: UtcDatetime | None
    send_attempted_at: UtcDatetime | None
    accepted_at: UtcDatetime | None = Field(description="Provider acceptance, not delivery or reading.")
    replied_at: UtcDatetime | None
    updated_at: UtcDatetime


# --------------------------------------------------------------------------- inquiries


class InquiryView(ViewModel):
    """``seller_inquiries_get`` / ``GET /api/inquiries/{inquiry_id}``."""

    inquiry_id: UUID
    identity_key: Sha256Hex
    purpose: Literal["initial_availability_documents_price"] = INQUIRY_PURPOSE
    seller_entity_id: UUID
    vehicle: InquiryVehicleRef
    state: InquiryState
    state_reasons: tuple[ReasonCode, ...] = Field(max_length=30)
    qualification: InquiryQualificationView
    authorization: InquiryAuthorizationRef
    template: InquiryTemplateView
    language: MessageLanguage | None
    recipient: RecipientView
    sender: SenderRef
    rfc_message_id: str | None = Field(max_length=998)
    send_attempts: SendAttemptsView
    delivery_uncertain: bool = Field(
        description="A send attempt may have reached the provider; held for reconciliation."
    )
    suppression_reason: SuppressionReason | None
    message: InquiryMessageView | None
    reply_count: int = Field(ge=0)
    latest_reply_id: UUID | None
    timestamps: InquiryTimestamps
    row_version: int = Field(ge=1)
    approval_required: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self) -> InquiryView:
        if (self.state == InquiryState.SUPPRESSED) != (self.suppression_reason is not None):
            raise ValueError("a suppressed inquiry names its suppression reason (and only then)")
        return self


class InquirySummaryView(ViewModel):
    """One row of the inquiry list (no message text, no addresses)."""

    inquiry_id: UUID
    seller_entity_id: UUID
    vehicle: InquiryVehicleRef
    state: InquiryState
    language: MessageLanguage | None
    recipient_status: ContactStatus
    delivery_uncertain: bool
    suppression_reason: SuppressionReason | None
    reply_count: int = Field(ge=0)
    state_changed_at: UtcDatetime
    reserved_at: UtcDatetime | None
    send_attempted_at: UtcDatetime | None
    accepted_at: UtcDatetime | None
    row_version: int = Field(ge=1)


class InquiryListView(ViewModel):
    items: tuple[InquirySummaryView, ...] = Field(max_length=MAX_LIST_ITEMS)


# --------------------------------------------------------------------------- replies


class ReplySenderView(ViewModel):
    """Who sent the reply and how it was correlated (never by subject alone, spec 37.7)."""

    address: str | None = Field(max_length=320)
    address_domain: str | None = Field(max_length=253, pattern=_ADDRESS_DOMAIN_PATTERN)
    address_redacted: bool
    matches_verified_recipient: bool
    correlation_status: CorrelationStatus
    correlation_reasons: tuple[ReasonCode, ...] = Field(max_length=30)
    header_linked: bool
    thread_linked: bool

    @model_validator(mode="after")
    def _redaction(self) -> ReplySenderView:
        if self.address_redacted and self.address is not None:
            raise ValueError("a redacted sender carries no address")
        return self


class ReplyAttachmentView(ViewModel):
    """Attachment metadata only: no bytes, local locator, path or URL."""

    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=3, max_length=191)
    byte_size: int = Field(ge=0)
    sha256: Sha256Hex
    action: AttachmentAction | None
    document_kind: str | None = Field(max_length=40, pattern=r"^[a-z][a-z0-9_]{0,39}$")


class PriceQuoteView(ViewModel):
    """A seller's stated price: always an unaccepted quote, never a purchase price."""

    kind: QuoteKind
    amount: DecimalStr | None
    low: DecimalStr | None
    high: DecimalStr | None
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    conditions: tuple[PriceCondition, ...] = Field(max_length=10)
    status: Literal["unaccepted_seller_quote"] = "unaccepted_seller_quote"
    accepted: Literal[False] = False
    excerpt: str = Field(max_length=240)


class DocumentClaimView(ViewModel):
    kind: DocumentKind
    status: DocumentClaimStatus
    excerpt: str = Field(max_length=240)


class ReplyClaimsView(ViewModel):
    """Separate claim records extracted from the reply (spec 37.7 step 3)."""

    claims_version: str | None = Field(max_length=80)
    availability: AvailabilitySummary
    price_quotes: tuple[PriceQuoteView, ...] = Field(max_length=20)
    documents: tuple[DocumentClaimView, ...] = Field(max_length=20)
    requests: tuple[RequestKind, ...] = Field(max_length=10)
    escalations: tuple[RequestKind, ...] = Field(max_length=10)
    unanswered_questions: tuple[InquiryQuestion, ...] = Field(max_length=3)

    @classmethod
    def from_claims(cls, claims: ReplyClaims, *, vehicle_document_attachments: int = 0) -> ReplyClaimsView:
        return cls(
            claims_version=claims.version,
            availability=claims.availability_summary,
            price_quotes=tuple(
                PriceQuoteView(
                    kind=p.kind,
                    amount=None if p.amount is None else decimal_str(p.amount),
                    low=None if p.low is None else decimal_str(p.low),
                    high=None if p.high is None else decimal_str(p.high),
                    currency=p.currency,
                    conditions=p.conditions[:10],
                    excerpt=p.evidence.excerpt,
                )
                for p in claims.prices[:20]
            ),
            documents=tuple(
                DocumentClaimView(kind=d.kind, status=d.status, excerpt=d.evidence.excerpt)
                for d in claims.documents[:20]
            ),
            requests=tuple(dict.fromkeys(r.kind for r in claims.requests))[:10],
            escalations=claims.escalation_kinds[:10],
            unanswered_questions=claims.unanswered_questions(
                vehicle_document_attachments=vehicle_document_attachments
            ),
        )


class ValuationStatusView(ViewModel):
    """The vehicle's current valuation status after the reply (recalculation is queued)."""

    valuation_id: UUID | None
    state: ValuationState | None
    stale_reason: str | None = Field(max_length=200)
    recalculation_pending: bool


class ReplyView(ViewModel):
    """``seller_replies_get`` / ``GET /api/replies/{reply_id}``."""

    reply_id: UUID
    inquiry_id: UUID
    seller_entity_id: UUID
    vehicle: InquiryVehicleRef
    message_type: ReplyMessageType
    original_language: MessageLanguage | None
    subject: str = Field(max_length=512)
    sanitized_body: str = Field(max_length=65536, description="Sanitized original text; untrusted data.")
    mk_summary: str | None = Field(max_length=32768)
    mk_summary_version: str | None = Field(max_length=80)
    mk_summary_generated_at: UtcDatetime | None
    sender: ReplySenderView
    received_at: UtcDatetime
    observed_at: UtcDatetime
    ingested_at: UtcDatetime
    processing_state: ProcessingState
    processed_at: UtcDatetime | None
    claims: ReplyClaimsView | None
    quarantined: bool
    quarantine_reason: str | None = Field(max_length=80)
    attachments: tuple[ReplyAttachmentView, ...] = Field(max_length=MAX_REPLY_ATTACHMENTS)
    withheld_sensitive_attachments: int = Field(ge=0, le=200)
    valuation: ValuationStatusView


class ReplySummaryView(ViewModel):
    """One row of the reply list (no body)."""

    reply_id: UUID
    inquiry_id: UUID
    vehicle: InquiryVehicleRef
    message_type: ReplyMessageType
    original_language: MessageLanguage | None
    availability: AvailabilitySummary | None
    quarantined: bool
    processing_state: ProcessingState
    received_at: UtcDatetime
    ingested_at: UtcDatetime


class ReplyListView(ViewModel):
    items: tuple[ReplySummaryView, ...] = Field(max_length=MAX_LIST_ITEMS)


# --------------------------------------------------------------------------- controls


class InquiryControlView(ViewModel):
    """Workspace inquiry controls (``GET /api/inquiries/control``): kill switch, mode, caps."""

    version: int = Field(ge=1, description="Send as expected_version to pause or resume.")
    mode: InquiryMode
    kill_switch: bool
    kill_switch_reason: str | None = Field(max_length=2000)
    kill_switch_set_at: UtcDatetime | None
    max_per_24h: int = Field(ge=0, le=MAX_PER_24H_CEILING)
    max_per_15d: int = Field(ge=0, le=MAX_PER_15D_CEILING)
    ceiling_per_24h: Literal[2] = MAX_PER_24H_CEILING
    ceiling_per_15d: Literal[5] = MAX_PER_15D_CEILING
    seller_cooldown_seconds: int = Field(ge=86_400, le=365 * 86_400)
    used_24h: int = Field(ge=0)
    used_15d: int = Field(ge=0)
    updated_at: UtcDatetime
    approval_required: Literal[False] = False

    @staticmethod
    def cooldown_seconds(cooldown: timedelta) -> int:
        return int(cooldown.total_seconds())


class InquiryPauseResult(ViewModel):
    """``seller_inquiries_pause`` / ``POST /api/inquiries/control/pause`` result.

    Pausing activates the kill switch: untransmitted work stops at the next guard. It never
    implies a resume, which is a separate owner action on the dashboard only.
    """

    version: int = Field(ge=1, description="The new control version.")
    kill_switch: Literal[True] = True
    already_paused: bool
    kill_switch_set_at: UtcDatetime
    mode: InquiryMode
    notice: str = "Seller inquiries paused. Resuming requires a separate owner action."


class InquiryResumeResult(ViewModel):
    """``POST /api/inquiries/control/resume`` (owner, dashboard only) result."""

    version: int = Field(ge=1, description="The new control version.")
    kill_switch: Literal[False] = False
    mode: InquiryMode
    resumed_at: UtcDatetime


__all__ = [
    "INQUIRY_PURPOSE",
    "MAX_PER_15D_CEILING",
    "MAX_PER_24H_CEILING",
    "DocumentClaimView",
    "InquiryAuthorizationRef",
    "InquiryControlView",
    "InquiryListView",
    "InquiryMessageView",
    "InquiryPauseResult",
    "InquiryQualificationView",
    "InquiryResumeResult",
    "InquirySummaryView",
    "InquiryTemplateView",
    "InquiryTimestamps",
    "InquiryVehicleRef",
    "InquiryView",
    "PriceQuoteView",
    "RecipientView",
    "ReplyAttachmentView",
    "ReplyClaimsView",
    "ReplyListView",
    "ReplySenderView",
    "ReplySummaryView",
    "ReplyView",
    "SendAttemptSummary",
    "SendAttemptsView",
    "SenderRef",
    "ValuationStatusView",
    "address_domain",
    "recipient_address_visible",
]
