"""The owner-controlled activation canary on the ``outlook_local`` route (spec 37.10; F3, wave D2).

Pure and shared by the backend (``persistence.canaries_repo``, the mail-worker API) and the Windows
desktop worker (``outlook_bridge``): the fixed canary message, its Message-ID, the target-address
hash and the wire models of the four canary routes.

The canary is a one-time wiring test of the sender route: ONE fixed, neutral message from the
configured sender to an OWNER-CONTROLLED address, its Sent Items evidence and a correlated test
reply. It is never a seller inquiry:

- the message is a fixed template (`canary_subject` / `canary_body`): no vehicle, no seller, no
  question beyond "reply to confirm"; the desktop worker refuses anything else;
- its Message-ID is ``<canary-<uuid>@<sender domain>>``, which can never be parsed as an inquiry
  Message-ID, so a reply to it never correlates to a seller inquiry;
- the target address never reaches the backend: the backend keeps its SHA-256 only
  (`canary_target_hash`), and the desktop worker sends to the address configured ON THE OWNER'S
  MACHINE (``canary_target_address`` in its configuration) after checking that it hashes to the
  canary's target hash. A test reply is reported with the sender's hash only.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.replies import normalize_message_id
from suv_deals.domain.seller_contacts import canonicalize_address
from suv_deals.domain.seller_templates import message_body_hash

CANARY_SCHEMA_VERSION: Final = "1.0"
CANARY_MESSAGE_ID_PREFIX: Final = "canary-"
CANARY_TEMPLATE_VERSION: Final = "activation_canary/1"
#: How long a published canary stays sendable by the desktop worker (PROPOSED engineering default).
CANARY_INTENT_TTL: Final = timedelta(hours=24)
MAX_CANARY_INTENTS_PAGE: Final = 10

_HEX64: Final = r"^[0-9a-f]{64}$"
_WORKER_ID: Final = r"^[A-Za-z0-9._:-]{1,128}$"
_DOMAIN_RE: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_CANARY_ID_RE: Final = re.compile(
    r"^<canary-(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@(?P<domain>[a-z0-9.-]{1,253})>$"
)
_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: Mirrors ``OutlookSubmissionState`` (the desktop worker's submission evidence).
CanarySubmissionState = Literal[
    "refused_before_send",
    "send_call_failed",
    "submitted_to_outbox",
    "sent_items_confirmed",
    "transport_rejected",
]
#: Mirrors ``OutlookRefusalReason`` (``not_now`` is never used for a canary).
CanaryRefusalReason = Literal[
    "intent_expired",
    "account_mismatch",
    "kill_switch",
    "binding_mismatch",
    "intent_invalid",
    "outlook_not_classic",
    "mailbox_unavailable",
    "duplicate_intent",
]


# =============================================================================================
# The fixed message
# =============================================================================================


def canary_reference(canary_id: UUID) -> str:
    """``canary-<uuid>``: the canary's reference (subject, body and Outlook item property)."""
    return f"{CANARY_MESSAGE_ID_PREFIX}{canary_id}"


def canary_subject(canary_id: UUID) -> str:
    return f"Mailbox activation test {canary_reference(canary_id)}"


def canary_body(canary_id: UUID) -> str:
    """The fixed, neutral canary text (English, plain, no personal data, no vehicle)."""
    return (
        "This is a one-time technical test of a mailbox connection.\n"
        "It was sent to an address controlled by the mailbox owner and asks for nothing else.\n"
        "\n"
        'To complete the test, please reply to this message with a short text (for example "ok").\n'
        "\n"
        f"Reference: {canary_reference(canary_id)}\n"
    )


def canary_body_hash(canary_id: UUID) -> str:
    return message_body_hash(canary_subject(canary_id), canary_body(canary_id))


def canary_target_hash(address: str) -> str:
    """SHA-256 (hex) of the canonical, lower-cased address (raises ``AddressError``)."""
    canonical = canonicalize_address(address).canonical
    return hashlib.sha256(canonical.lower().encode("utf-8")).hexdigest()


def canary_message_id(canary_id: UUID, sender_from_address: str) -> str:
    """``<canary-<uuid>@<sender domain>>``; ``ValueError`` when the sender has no usable domain."""
    domain = sender_from_address.rsplit("@", 1)[-1].strip().lower()
    if "@" not in sender_from_address or not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("the sender address has no usable domain")
    value = f"<{canary_reference(canary_id)}@{domain}>"
    if normalize_message_id(value) != value or parse_canary_message_id(value) != canary_id:
        raise ValueError("the canary Message-ID is invalid")
    return value


def parse_canary_message_id(value: str | None) -> UUID | None:
    """The canary id of a Message-ID made by `canary_message_id`; anything else is ``None``."""
    if not isinstance(value, str):
        return None
    match = _CANARY_ID_RE.fullmatch(value.strip())
    if match is None or not _DOMAIN_RE.fullmatch(match.group("domain")):
        return None
    return UUID(match.group("uuid"))


# =============================================================================================
# Wire models (``/v1/mail-workers/canary-intents``; mirrored by ``outlook_bridge.wire``)
# =============================================================================================


def _aware(value: datetime) -> datetime:
    return ensure_utc(value)


class CanaryIntent(BaseModel):
    """One published canary for the worker's mailbox (``GET /canary-intents``).

    ``target_address_hash`` names the recipient; the worker sends only to its locally configured
    owner-controlled address with that hash. The subject, body, body hash and Message-ID are
    exactly the fixed canary rendering of ``canary_id`` (checked here, on both sides).
    """

    model_config = _FROZEN

    schema_version: Literal["1.0"] = CANARY_SCHEMA_VERSION
    canary_id: UUID
    mailbox_binding_id: UUID
    binding_id: UUID
    binding_version: int = Field(ge=1)
    account_id: str = Field(min_length=1, max_length=320)
    from_address: str = Field(min_length=3, max_length=254)
    from_display_name: str = Field(min_length=1, max_length=64)
    reply_to_address: str | None = Field(default=None, max_length=254)
    target_address_hash: str = Field(pattern=_HEX64)
    subject: str = Field(min_length=1, max_length=200)
    body_text: str = Field(min_length=1, max_length=2000)
    rfc_message_id: str = Field(max_length=998)
    body_hash: str = Field(pattern=_HEX64)
    created_at: datetime
    not_after: datetime
    #: The canary's validity ended before any worker claimed it: refuse it ``intent_expired``.
    expired: bool = False

    @field_validator("created_at", "not_after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _fixed_rendering(self) -> CanaryIntent:
        if parse_canary_message_id(self.rfc_message_id) != self.canary_id:
            raise ValueError("the Message-ID does not belong to this canary")
        if self.subject != canary_subject(self.canary_id) or self.body_text != canary_body(self.canary_id):
            raise ValueError("the canary text is not the fixed canary rendering")
        if self.body_hash != canary_body_hash(self.canary_id):
            raise ValueError("the canary body hash does not match")
        if not self.created_at < self.not_after <= self.created_at + CANARY_INTENT_TTL:
            raise ValueError("the canary validity window is invalid")
        return self

    def is_expired(self, now: datetime) -> bool:
        return self.expired or _aware(now) >= self.not_after

    def same_canary(self, other: CanaryIntent) -> bool:
        return self.model_dump(exclude={"expired"}) == other.model_dump(exclude={"expired"})


class CanaryIntentBatch(BaseModel):
    """Response of ``GET /v1/mail-workers/canary-intents``."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = CANARY_SCHEMA_VERSION
    intents: tuple[CanaryIntent, ...] = Field(default=(), max_length=MAX_CANARY_INTENTS_PAGE)
    kill_switch_active: bool


class CanaryClaimRequest(BaseModel):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/claim`` (always evaluated fresh)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"]
    canary_id: UUID
    claim_attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)


class CanaryClaimDecision(BaseModel):
    """Response of the canary claim."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = CANARY_SCHEMA_VERSION
    canary_id: UUID
    proceed: bool
    refusal_reason: CanaryRefusalReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CanaryClaimDecision:
        if self.proceed and self.refusal_reason is not None:
            raise ValueError("a proceeding claim carries no refusal reason")
        return self


class CanaryReport(BaseModel):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/report`` (no address)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = CANARY_SCHEMA_VERSION
    canary_id: UUID
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)
    state: CanarySubmissionState
    refusal_reason: CanaryRefusalReason | None = None
    observed_internet_message_id: str | None = Field(default=None, max_length=998)
    sent_items_present: bool = False
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    reported_at: datetime
    sent_at: datetime | None = None

    @field_validator("reported_at", "sent_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> CanaryReport:
        if (self.state == "refused_before_send") != (self.refusal_reason is not None):
            raise ValueError("a refusal reason belongs to refused_before_send only")
        if self.sent_items_present != (self.state == "sent_items_confirmed"):
            raise ValueError("sent_items_present must match sent_items_confirmed")
        return self


class CanaryReplyReport(BaseModel):
    """Body of ``POST /v1/mail-workers/canary-intents/{canary_id}/reply``: the correlated test
    reply's headers only (no body, no address: the sender is given as its target hash)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = CANARY_SCHEMA_VERSION
    canary_id: UUID
    mailbox_binding_id: UUID
    worker_id: str = Field(min_length=1, max_length=128, pattern=_WORKER_ID)
    internet_message_id: str = Field(min_length=3, max_length=998)
    in_reply_to: tuple[str, ...] = Field(default=(), max_length=20)
    references: tuple[str, ...] = Field(default=(), max_length=50)
    from_address_hash: str = Field(pattern=_HEX64)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("in_reply_to", "references")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(v) > 998 for v in value):
            raise ValueError("a Message-ID is at most 998 characters")
        return value

    @model_validator(mode="after")
    def _correlated(self) -> CanaryReplyReport:
        linked = {parse_canary_message_id(v) for v in (*self.in_reply_to, *self.references)}
        if self.canary_id not in linked:
            raise ValueError("the reply does not reference the canary Message-ID")
        return self


__all__ = [
    "CANARY_INTENT_TTL",
    "CANARY_MESSAGE_ID_PREFIX",
    "CANARY_SCHEMA_VERSION",
    "CANARY_TEMPLATE_VERSION",
    "MAX_CANARY_INTENTS_PAGE",
    "CanaryClaimDecision",
    "CanaryClaimRequest",
    "CanaryIntent",
    "CanaryIntentBatch",
    "CanaryRefusalReason",
    "CanaryReplyReport",
    "CanaryReport",
    "CanarySubmissionState",
    "canary_body",
    "canary_body_hash",
    "canary_message_id",
    "canary_reference",
    "canary_subject",
    "canary_target_hash",
    "parse_canary_message_id",
]
