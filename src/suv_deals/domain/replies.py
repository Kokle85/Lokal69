# ruff: noqa: RUF001
# (RUF001: the Macedonian summary vocabulary is Cyrillic by design; its letters look like Latin ones.)
"""Seller reply classification, correlation, deduplication, claims and processing.

Spec v1.1 sections 37.6 (backend-side semantics of event detection plus reconciliation), 37.7
(reply correlation, privacy and processing) and 37.8 (reply ingest idempotency). Pure domain
code: no I/O, no clock reads and no network. The local mailbox worker and the backend call the
same functions, so the decision whether a message may leave the local mailbox is identical on
both sides.

Message types (``classify_message``)
    ``spam`` (``X-Spam-Flag``/``X-Spam-Status``, Exchange SCL >= 5, junk folder) wins over every
    other type. Then ``bounce`` (DSN ``multipart/report; report-type=delivery-status`` with
    ``Action: failed``, mailer-daemon/postmaster senders, ``X-Failed-Recipients``, Outlook
    ``REPORT.IPM.Note.NDR`` and DE/IT/FR/EN subjects such as "Undelivered Mail",
    "Unzustellbar", "Non recapitabile", "Non remis"), ``delivery_notice`` (delayed/delivered DSN
    actions, read receipts/MDNs, "Zugestellt:", "Consegnato:", "Remis :"; subject wording counts
    only when the subject does not start with a human "Re:"/"AW:"/"R:"/"Fwd:" prefix), ``auto_reply``
    (``Auto-Submitted`` other than ``no``, ``X-Autoreply``/``X-Autorespond``, ``Precedence:
    auto_reply``, Outlook OOF templates and subjects such as "Out of office", "Abwesenheitsnotiz",
    "Fuori sede", "Réponse automatique"; ordinary words such as "Abwesend", "Assente" or
    "Absence" only as the subject's leading prefix, never after "Re:"/"AW:"), ``ambiguous``
    (no/several From addresses, unknown report type, non-mail Outlook items, an out-of-office
    phrase only in the body) and otherwise ``seller_reply``.

Correlation (``correlate_reply``)
    Only bindings of the message's own mailbox count; the newest binding version per inquiry
    wins and a tombstoned/revoked binding never grants access. A message is linked to an inquiry
    by verified ``In-Reply-To``/``References`` against the stored outbound (or send-intent)
    Message-IDs, by the provider thread id of the same provider, or - for bounces/delivery
    notices - by the Message-ID of the returned original. The link is then corroborated: the
    sender must be a verified seller alias (exact local part, normalised domain; no Gmail
    dot/plus folding) and the text must not reference a *different* inquiry's listing. A
    thread-id-only link (Outlook/Gmail threading can follow subjects) additionally needs a
    verified sender *and* a listing reference. A subject match alone never correlates; without a
    thread link a verified sender is a possible match only when the text also references that
    inquiry's listing (a dealer newsletter stays local). Non-mail Outlook items and the mailbox
    owner's own messages (``own_addresses``) are never reply-processed and stay local.
    Forwarded, changed-address, spam, ambiguous and multi-inquiry messages are quarantined: never
    applied to a vehicle before verification. ``upload_scope`` says what may leave the mailbox:
    ``full`` (matched), ``quarantine`` (exactly one candidate inquiry, uploaded flagged and not
    applied) or ``none`` (unrelated personal mail and multi-candidate ambiguity stay local).
    A matched message that references (In-Reply-To/References, or the returned original of a
    DSN) a Message-ID of an ``uncertain`` send - normally its send-intent Message-ID - resolves
    that send as submitted; a thread-only link or a quarantined possible match never does.

Source fingerprint and ingest dedup (``source_content_fingerprint``, ``decide_ingest``)
    The fingerprint covers the schema-selected immutable content: Internet Message-ID (or, only
    when that is absent, the provider message id), From, In-Reply-To, References, subject,
    sanitised body and attachment content metadata (name, MIME type, size, SHA-256;
    order-insensitive). Outlook EntryID/StoreID, a provider message id next to an Internet
    Message-ID (Graph ids change on a move; copies differ), attachment local references,
    ``received_at``, ``observed_at``, ``detected_language`` and binding/sync metadata are
    excluded. Same dedup key + same fingerprint -> duplicate (the
    existing reply id; a changed locator is recorded in locator history). Same key + different
    fingerprint -> ``IDEMPOTENCY_CONFLICT`` and quarantine, never an overwrite. The request
    idempotency key and the source identity are both checked; neither is trusted alone.

Claims (``extract_reply_claims``)
    Conservative DE/IT/FR/EN patterns over the reply text with the quoted previous message
    removed. Questions, conditional clauses ("falls", "se", "si", "if") and negations are not
    statements. Availability (available/sold/reserved/not_available), seller quotes (single,
    range or minimum; currency only when stated; ``accepted`` is always ``False``), other amounts
    (deposits, transport/fees, previous prices) kept separately, documents
    (available/attached/refused/not_available/mentioned) and requests (payment, reservation,
    identity document, appointment, commitment, price acceptance, opt-out, complaint). Nothing
    is invented: an ambiguous number is kept as an unparsed mention, never guessed. A number
    without a currency is a quote only next to a price word and when plausible (>= 100); numbers
    labelled as mileage/year/power/owners/doors/keys are never money, and another money-like
    number beside a stated price is kept as a currency-less mention, not a quote.

Processing (``decide_reply_processing``)
    Never an automatic response or follow-up. Payment/reservation/identity/appointment/
    commitment/price-acceptance requests and withheld sensitive attachments escalate to the
    owner. "Sold" yields ``sold_claimed`` with ``seller_reported_sold`` evidence only - no
    purchase, buyer or price. A hard bounce suppresses the address; an opt-out or complaint
    suppresses without acknowledgement. Only a matched seller reply emits the minimal
    ``seller.reply.received.v1`` Slack-route signal (ids, safe dashboard URL, brief status).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import getaddresses
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    AvailabilityEvidenceKind,
    EmailProviderKind,
    InquiryState,
    MessageLanguage,
    OutboxState,
    PriceBasis,
    ReplyMessageType,
    SuppressionReason,
)
from suv_deals.domain.listings import canonical_json, sha256_json
from suv_deals.domain.notifications import (
    FIXTURE_BLOCKER,
    guard_payload,
    sanitize_seller_text,
    text_problems,
)
from suv_deals.domain.parsing import Locale, parse_number
from suv_deals.errors import Forbidden, IdempotencyConflict, ValidationFailed

REPLY_SCHEMA_VERSION: Final = "1.0"
FINGERPRINT_VERSION: Final = "reply-source-fingerprint/2"
SANITIZER_VERSION: Final = "reply-sanitizer/1"
CLAIMS_VERSION: Final = "reply-claims/1"
MK_SUMMARY_VERSION: Final = "reply-mk-summary/1"
CORRELATION_VERSION: Final = "reply-correlation/1"

MAX_REQUEST_BYTES: Final = 128 * 1024
MAX_BODY_BYTES: Final = 64 * 1024
MAX_SUBJECT_CHARS: Final = 512
MAX_ATTACHMENTS: Final = 20
MAX_REFERENCES: Final = 200
MAX_HEADER_VALUE_CHARS: Final = 16 * 1024
MAX_HEADER_NAMES: Final = 200
MAX_VALUES_PER_HEADER: Final = 50
MAX_RAW_BODY_CHARS: Final = 1024 * 1024  # analysis bound for untrusted input before sanitising
MAX_MESSAGE_ID_CHARS: Final = 998
MAX_EXCERPT_CHARS: Final = 240
UNMATCHED_RETRY_WINDOW: Final = timedelta(hours=24)

SELLER_REPLY_EVENT_TYPE: Final = "seller.reply.received"
SELLER_REPLY_EVENT_NAME: Final = "seller.reply.received.v1"
SELLER_REPLY_ROUTE: Final = "slack_seller_reply"
CANARY_MARKER: Final = "canary"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


# =============================================================================================
# Text helpers
# =============================================================================================

_ZERO_WIDTH_RE: Final = re.compile(
    r"[\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"
    r"\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}]"
)
_CONTROL_EXCEPT_NL_TAB_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HEADER_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_FOLDING_RE: Final = re.compile(r"\r?\n[ \t]+")


def _bounded(text: str | None, limit: int = MAX_RAW_BODY_CHARS) -> str:
    if not text:
        return ""
    return text[:limit]


def _normalize_text(text: str | None) -> str:
    """NFKC, LF newlines, no zero-width or control characters except newline and tab."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", _bounded(text))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH_RE.sub("", value)
    value = _CONTROL_EXCEPT_NL_TAB_RE.sub(" ", value)
    return value.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").replace(
        "\N{LEFT SINGLE QUOTATION MARK}", "'"
    )


def _lower_same_length(text: str) -> str:
    """Lower-case per character, keeping every index aligned with the input."""
    return "".join(low if len(low := ch.lower()) == 1 else ch for ch in text)


def _fold(text: str | None) -> str:
    """Comparison form for short header text: NFKC, lower-case, single spaces."""
    return re.sub(r"\s+", " ", _lower_same_length(_normalize_text(text))).strip()


def _excerpt(text: str, start: int, end: int) -> str:
    raw = text[max(0, start) : max(start, end)]
    cleaned = sanitize_seller_text(raw, max_length=MAX_EXCERPT_CHARS)
    return cleaned or ""


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _rfc3339(value: datetime) -> str:
    utc = ensure_utc(value)
    spec = "milliseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=spec).replace("+00:00", "Z")


# =============================================================================================
# Headers, addresses and Message-IDs
# =============================================================================================

_HEADER_NAME_RE: Final = re.compile(r"^[!-9;-~]{1,76}$")


def _clean_header_value(value: str) -> str:
    unfolded = _FOLDING_RE.sub(" ", value)
    return _HEADER_CONTROL_RE.sub(" ", unfolded).strip()[:MAX_HEADER_VALUE_CHARS]


class MessageHeaders(BaseModel):
    """Case-insensitive, bounded view of selected untrusted message headers.

    ``from_raw`` unfolds folded lines, replaces control characters (no CR/LF survives), caps the
    number and length of values and silently skips invalid header names or non-text values: a
    hostile header can never break classification.
    """

    model_config = _FROZEN

    values: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _clean(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        """Same bounds and cleaning as ``from_raw`` whichever way the model is built."""
        cleaned: dict[str, tuple[str, ...]] = {}
        for name, items in value.items():
            if not _HEADER_NAME_RE.fullmatch(name.strip()) or len(cleaned) >= MAX_HEADER_NAMES:
                continue
            kept = tuple(_clean_header_value(item) for item in items[:MAX_VALUES_PER_HEADER])
            if kept:
                key = name.strip().lower()
                cleaned[key] = (*cleaned.get(key, ()), *kept)[:MAX_VALUES_PER_HEADER]
        return cleaned

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | MessageHeaders | None) -> MessageHeaders:
        if raw is None:
            return cls()
        if isinstance(raw, MessageHeaders):
            return raw
        if not isinstance(raw, Mapping):
            raise ValidationFailed("headers must be a mapping of header name to value(s)")
        collected: dict[str, list[str]] = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name.strip()):
                continue
            key = name.strip().lower()
            if key not in collected and len(collected) >= MAX_HEADER_NAMES:
                continue
            items: list[Any]
            if isinstance(value, str):
                items = [value]
            elif isinstance(value, Sequence):
                items = list(value)
            else:
                continue
            bucket = collected.setdefault(key, [])
            for item in items:
                if isinstance(item, str) and len(bucket) < MAX_VALUES_PER_HEADER:
                    bucket.append(_clean_header_value(item))
        return cls(values={k: tuple(v) for k, v in collected.items() if v})

    def get(self, name: str) -> str | None:
        found = self.values.get(name.lower())
        return found[0] if found else None

    def get_all(self, name: str) -> tuple[str, ...]:
        return self.values.get(name.lower(), ())


_LOCAL_PART_RE: Final = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_DOMAIN_LABEL_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _canonical_domain(domain: str) -> str | None:
    name = unicodedata.normalize("NFC", domain.strip().rstrip(".")).lower()
    if not name or len(name) > 253:
        return None
    try:
        host = name.encode("idna").decode("ascii").lower()
        if host.encode("ascii").decode("idna").lower() != name:
            return None
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or not all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None
    tld = labels[-1]
    if not (tld.isalpha() or tld.startswith("xn--")):
        return None
    return host


def canonical_address(value: str | None) -> str | None:
    """Conservative canonical e-mail address, or ``None`` when it is not a plain valid address.

    The local part is kept exactly (no case folding, no Gmail dot/plus rules: two addresses are
    not assumed equivalent without evidence); the domain is lower-cased and IDNA-encoded.
    Quoted or non-ASCII local parts are not accepted and therefore never verify a sender.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if len(text) > 320 or text.count("@") != 1:
        return None
    local, domain = text.split("@", 1)
    if not _LOCAL_PART_RE.fullmatch(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    host = _canonical_domain(domain)
    return f"{local}@{host}" if host else None


def parse_address_list(value: str | None) -> tuple[str | None, ...]:
    """Every address in an address-list header; invalid entries are ``None`` (never dropped)."""
    if not value:
        return ()
    pairs = getaddresses([value[:MAX_HEADER_VALUE_CHARS]])
    result: list[str | None] = []
    for _name, address in pairs:
        if not address and not _name:
            result.append(None)  # strict parser failure: "('', '')"
            continue
        result.append(canonical_address(address) if address else None)
    return tuple(result[:MAX_VALUES_PER_HEADER])


_MSGID_TOKEN_RE: Final = re.compile(r"<([^<>\s]{3,996})>")


def normalize_message_id(value: str | None) -> str | None:
    """``<left@right>`` with the domain part lower-cased, or ``None`` when it is not a Message-ID.

    The left part stays case-sensitive (RFC 5322 identity); whitespace, control characters,
    nested angle brackets and a missing ``@`` are rejected.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_MESSAGE_ID_CHARS:
        return None
    inner = text[1:-1] if text.startswith("<") and text.endswith(">") else text
    if not inner or any(ord(c) < 33 or ord(c) > 126 or c in "<>" for c in inner):
        return None
    if inner.count("@") != 1:
        return None
    left, right = inner.split("@", 1)
    if not left or not right:
        return None
    return f"<{left}@{right.lower()}>"


def parse_message_id_list(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalised, de-duplicated Message-IDs from an ``In-Reply-To``/``References`` value.

    At most ``MAX_REFERENCES`` are kept: the oldest and newest halves of an over-long chain,
    because the inquiry's own Message-ID is normally near the start.
    """
    if value is None:
        return ()
    texts = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)]
    found: list[str] = []
    for text in texts:
        tokens = _MSGID_TOKEN_RE.findall(text[:MAX_HEADER_VALUE_CHARS])
        candidates = [f"<{t}>" for t in tokens] if tokens else [text]
        for candidate in candidates:
            normalized = normalize_message_id(candidate)
            if normalized is not None and normalized not in found:
                found.append(normalized)
    if len(found) > MAX_REFERENCES:
        half = MAX_REFERENCES // 2
        found = found[:half] + found[-half:]
    return tuple(found)


_ITEM_CLASS_RE: Final = re.compile(r"^[a-z0-9._-]{1,255}$")
_NON_MAIL_PREFIXES: Final = (
    "ipm.schedule.meeting",
    "ipm.appointment",
    "ipm.sharing",
    "ipm.contact",
    "ipm.distlist",
    "ipm.task",
    "ipm.stickynote",
    "ipm.activity",
    "ipm.post",
    "ipm.recall",
    "ipm.outlook.recall",
    "ipm.note.rules.recall",
    "report.ipm.schedule",
)


def is_processable_item(message_class: str | None) -> bool:
    """Whether an Outlook item class is mail that reply processing may inspect (spec 37.6).

    Mail (``IPM.Note`` and its subclasses) and mail reports (``REPORT.IPM.Note.*``: NDRs,
    delivery and read receipts) are processable; meetings, appointments, sharing invitations,
    contacts, tasks, posts and recall items are not. An unknown class is not processable.
    """
    if not message_class:
        return False
    value = message_class.strip().lower()
    if not _ITEM_CLASS_RE.fullmatch(value):
        return False
    if value.startswith(_NON_MAIL_PREFIXES):
        return False
    return value == "ipm.note" or value.startswith(("ipm.note.", "report.ipm.note."))


# =============================================================================================
# Attachments (metadata only)
# =============================================================================================

_SAFE_REF_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=-]{0,255}$")
_MIME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_FILENAME_FORBIDDEN_RE: Final = re.compile(r"[\x00-\x1f\x7f/\\]")


def safe_filename(raw: str | None, *, default: str = "attachment") -> str:
    """A display-safe attachment name: no path, control characters or traversal, <= 255 chars."""
    text = unicodedata.normalize("NFC", raw or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _FILENAME_FORBIDDEN_RE.sub("_", text).strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    if not text or text in {".", ".."}:
        text = default
    if len(text) > 255:
        stem, dot, ext = text.rpartition(".")
        text = (stem[: 255 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else text[:255]
    return text


class AttachmentMeta(BaseModel):
    """Attachment *metadata* (spec 37.8): safe name, MIME type, size, SHA-256 and a local ref.

    Never bytes, URLs or paths: names containing path separators, control characters or
    traversal and local references that look like URLs/paths are rejected.
    """

    model_config = _FROZEN

    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=3, max_length=191)
    byte_size: int = Field(ge=0, le=2**40)
    sha256: str
    local_ref: str | None = Field(default=None, max_length=256)

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        if _FILENAME_FORBIDDEN_RE.search(value) or value.strip() in {"", ".", ".."} or value.startswith(".."):
            raise ValueError("attachment filename must be a plain name without paths or control characters")
        return unicodedata.normalize("NFC", value)

    @field_validator("mime_type")
    @classmethod
    def _mime(cls, value: str) -> str:
        lowered = value.strip().lower().split(";", 1)[0].strip()
        if not _MIME_RE.fullmatch(lowered):
            raise ValueError("invalid MIME type")
        return lowered

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        if not _HEX64_RE.fullmatch(value):
            raise ValueError("sha256 must be a lowercase hex digest")
        return value

    @field_validator("local_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SAFE_REF_RE.fullmatch(value) or ".." in value or "://" in value:
            raise ValueError("local_ref must be an opaque local token, not a URL or path")
        return value

    def content_material(self) -> dict[str, Any]:
        """Immutable content metadata used by the source fingerprint (no local reference)."""
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


# =============================================================================================
# Inbound message
# =============================================================================================


class SourceMessageIdentity(BaseModel):
    """Where a message lives and how the provider identifies it.

    ``internet_message_id``, ``provider_message_id`` and ``provider_thread_id`` are identities;
    ``outlook_entry_id``/``outlook_store_id`` are *locators* that can change after a move and are
    never part of the dedup key or fingerprint.
    """

    model_config = _FROZEN

    mailbox_binding_id: UUID
    provider: EmailProviderKind
    internet_message_id: str | None = None
    provider_message_id: str | None = Field(default=None, max_length=512)
    provider_thread_id: str | None = Field(default=None, max_length=512)
    outlook_entry_id: str | None = Field(default=None, max_length=1024)
    outlook_store_id: str | None = Field(default=None, max_length=1024)
    received_at: datetime | None = None

    @field_validator("internet_message_id")
    @classmethod
    def _msgid(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = normalize_message_id(value)
        if normalized is None:
            raise ValueError("internet_message_id is not a valid RFC Message-ID")
        return normalized

    @field_validator("provider_message_id", "provider_thread_id", "outlook_entry_id", "outlook_store_id")
    @classmethod
    def _opaque(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError("provider identifiers must not contain whitespace or control characters")
        return value

    @field_validator("received_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class InboundMessage(BaseModel):
    """One mailbox item as the local worker copied it off the Outlook/provider thread."""

    model_config = _FROZEN

    identity: SourceMessageIdentity
    headers: MessageHeaders = Field(default_factory=MessageHeaders)
    body_text: str = Field(default="", max_length=MAX_RAW_BODY_CHARS)
    attachments: tuple[AttachmentMeta, ...] = Field(default=(), max_length=200)
    in_junk_folder: bool = False
    message_class: str | None = Field(default=None, max_length=255)

    @field_validator("headers", mode="before")
    @classmethod
    def _headers(cls, value: Any) -> Any:
        if value is None or isinstance(value, MessageHeaders):
            return value or MessageHeaders()
        if isinstance(value, Mapping):
            # A serialised ``MessageHeaders`` is ``{"values": {name: [..]}}``; anything else
            # (including a hostile raw header literally named "values") is a raw header mapping.
            if set(value.keys()) == {"values"} and isinstance(value.get("values"), Mapping):
                try:
                    return MessageHeaders.model_validate(value)
                except ValueError:
                    pass
            return MessageHeaders.from_raw(value)
        return value

    @property
    def subject(self) -> str:
        return (self.headers.get("subject") or "")[:MAX_SUBJECT_CHARS]

    @property
    def from_addresses(self) -> tuple[str | None, ...]:
        addresses: list[str | None] = []
        for value in self.headers.get_all("from"):
            addresses.extend(parse_address_list(value))
        return tuple(addresses)

    @property
    def in_reply_to(self) -> tuple[str, ...]:
        return parse_message_id_list(self.headers.get_all("in-reply-to"))

    @property
    def references(self) -> tuple[str, ...]:
        return parse_message_id_list(self.headers.get_all("references"))

    def reference_ids(self) -> tuple[str, ...]:
        """In-Reply-To first, then References, de-duplicated."""
        seen: list[str] = []
        for item in (*self.in_reply_to, *self.references):
            if item not in seen:
                seen.append(item)
        return tuple(seen)


# =============================================================================================
# Classification
# =============================================================================================


class ClassificationSignal(StrEnum):
    NON_MAIL_ITEM = "NON_MAIL_ITEM"
    SPAM_HEADER = "SPAM_HEADER"
    JUNK_FOLDER = "JUNK_FOLDER"
    OUTLOOK_NDR_CLASS = "OUTLOOK_NDR_CLASS"
    OUTLOOK_RECEIPT_CLASS = "OUTLOOK_RECEIPT_CLASS"
    OUTLOOK_AUTO_REPLY_CLASS = "OUTLOOK_AUTO_REPLY_CLASS"
    DSN_REPORT = "DSN_REPORT"
    DSN_ACTION_FAILED = "DSN_ACTION_FAILED"
    DSN_ACTION_NOT_FAILED = "DSN_ACTION_NOT_FAILED"
    MDN_REPORT = "MDN_REPORT"
    UNKNOWN_REPORT_TYPE = "UNKNOWN_REPORT_TYPE"
    MAILER_DAEMON_SENDER = "MAILER_DAEMON_SENDER"
    FAILED_RECIPIENTS_HEADER = "FAILED_RECIPIENTS_HEADER"
    BOUNCE_SUBJECT = "BOUNCE_SUBJECT"
    DELAY_SUBJECT = "DELAY_SUBJECT"
    DELIVERY_SUBJECT = "DELIVERY_SUBJECT"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
    AUTOREPLY_HEADER = "AUTOREPLY_HEADER"
    PRECEDENCE_AUTO_REPLY = "PRECEDENCE_AUTO_REPLY"
    AUTO_REPLY_SUBJECT = "AUTO_REPLY_SUBJECT"
    AUTO_REPLY_BODY_PHRASE = "AUTO_REPLY_BODY_PHRASE"
    MISSING_SENDER = "MISSING_SENDER"
    MULTIPLE_SENDERS = "MULTIPLE_SENDERS"
    INVALID_SENDER = "INVALID_SENDER"


class MessageClassification(BaseModel):
    model_config = _FROZEN

    message_type: ReplyMessageType
    signals: tuple[ClassificationSignal, ...]


def _rx_any(parts: Iterable[str], flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), flags)


_BOUNCE_SUBJECT_RE: Final = _rx_any(
    (
        r"undeliver(?:ed|able)",
        r"delivery status notification \(failure\)",
        r"mail delivery (?:failed|failure|system)",
        r"delivery (?:has )?failed",
        r"delivery failure",
        r"returned mail",
        r"failure notice",
        r"message not delivered",
        r"returned to sender",
        r"unzustellbar",
        r"nicht zustellbar",
        r"zustellung fehlgeschlagen",
        r"fehlgeschlagene zustellung",
        r"non recapitabile",
        r"impossibile recapitare",
        r"mancato recapito",
        r"messaggio non (?:consegnato|recapitato)",
        r"consegna non riuscita",
        r"non remis",
        r"[ée]chec de (?:la )?(?:remise|livraison|distribution)",
        r"message non (?:distribu[ée]|remis)",
        r"non distribuable",
        r"impossible de remettre",
    )
)
_DELAY_SUBJECT_RE: Final = _rx_any(
    (
        r"delivery status notification \(delay\)",
        r"delayed mail",
        r"delivery delayed",
        r"message delayed",
        r"verz[öo]gert",
        r"ritardat[oa]",
        r"remise diff[ée]r[ée]e",
        r"distribution retard[ée]e",
    )
)
_DELIVERY_SUBJECT_RE: Final = _rx_any(
    (
        r"^\s*(?:delivered|zugestellt|consegnato|recapitato|remis|read|gelesen|letto|lu|not read"
        r"|nicht gelesen|ungelesen|non letto|non lu)\s*:",
        r"delivery status notification \(success\)",
        r"read receipt",
        r"delivery receipt",
        r"lesebest[äa]tigung",
        r"zustellbest[äa]tigung",
        r"übermittlungsbest[äa]tigung",
        r"conferma di (?:lettura|consegna|recapito)",
        r"accus[ée] de (?:lecture|r[ée]ception)",
    )
)
# Unambiguous auto-reply phrases count anywhere in the subject.
_AUTO_REPLY_SUBJECT_RE: Final = _rx_any(
    (
        r"out[ -]of[ -](?:the[ -])?office",
        r"\bautomatic reply\b",
        r"\bauto[- ]?reply\b",
        r"\bauto[- ]?response\b",
        r"\bautoresponder\b",
        r"\baway from (?:the )?office\b",
        r"\babwesenheits\w*",
        r"\bautomatische (?:antwort|r[üu]ckantwort)",
        r"\bnicht im b[üu]ro\b",
        r"\bfuori (?:sede|ufficio)\b",
        r"\brisposta automatica\b",
        r"\br[ée]ponse automatique\b",
    )
)
# Ordinary words ("Abwesenheit", "assente", "absence", "en congé") only count as an auto-reply
# prefix of the subject, never after a human reply prefix ("Re: ... absence of rust?").
_AUTO_REPLY_SUBJECT_PREFIX_RE: Final = re.compile(
    r"^\s*(?:abwesenheit|abwesend|assen(?:te|za)|in ferie|absence|absente?|en cong[ée]s?|vacation"
    r"|on holiday|on leave|urlaub|ferien|vacances)\b",
    re.IGNORECASE,
)
_REPLY_PREFIX_RE: Final = re.compile(r"^\s*(?:re|aw|r|rif|sv|antw|ri|wg|fwd?|tr|i)\s*:", re.IGNORECASE)
_AUTO_REPLY_BODY_RE: Final = _rx_any(
    (
        r"\bi am (?:currently )?(?:out of (?:the )?office|away from (?:the|my) (?:office|desk)|on (?:vacation"
        r"|holiday|leave))",
        r"\bi will be (?:out of (?:the )?office|away)\b",
        r"\bich bin (?:derzeit |zurzeit |momentan )?(?:bis [^\n]{0,40}?)?(?:nicht im b[üu]ro|abwesend"
        r"|im urlaub)",
        r"\bsono (?:attualmente )?(?:fuori (?:sede|ufficio)|assente|in ferie)",
        r"\bje suis (?:actuellement )?(?:absente?|en cong[ée]s?)",
        r"\bthis is an automatic (?:reply|response|message)",
        r"\bdies ist eine automatische",
        r"\bquesta [èe] una risposta automatica",
        r"\bceci est une r[ée]ponse automatique",
    )
)
_MAILER_DAEMON_LOCALS: Final = frozenset({"mailer-daemon", "postmaster", "mail-daemon", "mailerdaemon"})
_MAILER_DAEMON_NAME_RE: Final = re.compile(r"mail delivery (?:system|subsystem|service)", re.IGNORECASE)
_SCL_RE: Final = re.compile(r"\bSCL\s*:\s*([5-9])\b", re.IGNORECASE)
_REPORT_TYPE_RE: Final = re.compile(r"report-type\s*=\s*\"?([a-z0-9-]+)\"?", re.IGNORECASE)


DsnAction = Literal["failed", "delayed", "delivered", "relayed", "expanded"]
_DSN_ACTIONS: Final[tuple[DsnAction, ...]] = ("failed", "delayed", "delivered", "relayed", "expanded")


class BounceDetails(BaseModel):
    """Parsed delivery-status information (RFC 3464). Addresses stay local; never surfaced."""

    model_config = _FROZEN

    action: DsnAction | None = None
    status_code: str | None = None
    permanent: bool | None = None
    final_recipients: tuple[str, ...] = ()
    original_message_ids: tuple[str, ...] = ()


_DSN_ACTION_RE: Final = re.compile(
    r"^[ \t]*action[ \t]*:[ \t]*(failed|delayed|delivered|relayed|expanded)\b", re.IGNORECASE | re.MULTILINE
)
_DSN_STATUS_RE: Final = re.compile(
    r"^[ \t]*status[ \t]*:[ \t]*([245])\.(\d{1,3})\.(\d{1,3})\b", re.IGNORECASE | re.MULTILINE
)
_ENHANCED_STATUS_RE: Final = re.compile(r"(?<![\d.])([45])\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_DSN_RECIPIENT_RE: Final = re.compile(
    r"^[ \t]*(?:final|original)-recipient[ \t]*:[ \t]*(?:rfc822[ \t]*;)?[ \t]*<?([^\s<>;]{3,320})>?",
    re.IGNORECASE | re.MULTILINE,
)
_ORIGINAL_MSGID_RE: Final = re.compile(
    r"^[ \t]*(?:message-id|original-message-id|x-original-message-id)[ \t]*:[ \t]*(<[^<>\s]{3,996}>)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_delivery_report(body: str | None) -> BounceDetails:
    """Action, enhanced status, final recipients and original Message-IDs from a DSN/NDR body.

    Several ``Action`` lines: any ``failed`` wins. Without a structured ``Status`` line the
    first enhanced status code in the text (e.g. Exchange "550 5.1.1") is used. ``permanent``
    is ``True`` for class 5, ``False`` for class 4 and ``None`` when unknown or successful.
    """
    text = _bounded(body)
    actions = {m.group(1).lower() for m in _DSN_ACTION_RE.finditer(text)}
    action = next((candidate for candidate in _DSN_ACTIONS if candidate in actions), None)
    status = _DSN_STATUS_RE.search(text) or _ENHANCED_STATUS_RE.search(text)
    code = ".".join(status.groups()) if status else None
    permanent = None if code is None or code.startswith("2") else code.startswith("5")
    recipients: list[str] = []
    for match in _DSN_RECIPIENT_RE.finditer(text):
        address = canonical_address(match.group(1))
        if address and address not in recipients:
            recipients.append(address)
    msgids: list[str] = []
    for match in _ORIGINAL_MSGID_RE.finditer(text):
        normalized = normalize_message_id(match.group(1))
        if normalized and normalized not in msgids:
            msgids.append(normalized)
    return BounceDetails(
        action=action,
        status_code=code,
        permanent=permanent,
        final_recipients=tuple(recipients[:MAX_VALUES_PER_HEADER]),
        original_message_ids=tuple(msgids[:MAX_REFERENCES]),
    )


def _is_spam(headers: MessageHeaders) -> bool:
    for name in ("x-spam-flag", "x-spam-status"):
        if any(v.strip().lower().startswith("yes") for v in headers.get_all(name)):
            return True
    for value in headers.get_all("x-ms-exchange-organization-scl"):
        try:
            if int(value.strip()) >= 5:
                return True
        except ValueError:
            continue
    return any(_SCL_RE.search(v) for v in headers.get_all("x-forefront-antispam-report"))


def _is_mailer_daemon(headers: MessageHeaders) -> bool:
    for raw in headers.get_all("from"):
        if _MAILER_DAEMON_NAME_RE.search(raw):
            return True
        for _name, address in getaddresses([raw]):
            local = address.split("@", 1)[0].strip().lower() if address else ""
            if local in _MAILER_DAEMON_LOCALS or local.startswith("microsoftexchange"):
                return True
    return False


def _auto_reply_header_signals(headers: MessageHeaders) -> list[ClassificationSignal]:
    signals: list[ClassificationSignal] = []
    for value in headers.get_all("auto-submitted"):
        keyword = value.split(";", 1)[0].strip().lower()
        if keyword and keyword != "no":
            signals.append(ClassificationSignal.AUTO_SUBMITTED)
            break
    for name in ("x-autoreply", "x-autorespond", "x-autoresponder", "x-auto-reply"):
        values = headers.get_all(name)
        if any(v.strip().lower() not in {"no", "false", "0"} for v in values):
            signals.append(ClassificationSignal.AUTOREPLY_HEADER)
            break
    if any(v.strip().lower() == "auto_reply" for v in headers.get_all("precedence")):
        signals.append(ClassificationSignal.PRECEDENCE_AUTO_REPLY)
    return signals


def _report_kind(headers: MessageHeaders, attachments: Sequence[AttachmentMeta]) -> str | None:
    content_type = (headers.get("content-type") or "").lower()
    mimes = {a.mime_type for a in attachments}
    if mimes & {"message/delivery-status", "message/global-delivery-status"}:
        return "delivery-status"
    if mimes & {"message/disposition-notification", "message/global-disposition-notification"}:
        return "disposition-notification"
    if content_type.startswith("multipart/report"):
        match = _REPORT_TYPE_RE.search(content_type)
        kind = match.group(1).lower() if match else "unknown"
        if kind in {"delivery-status", "global-delivery-status"}:
            return "delivery-status"
        if kind in {"disposition-notification", "global-disposition-notification"}:
            return "disposition-notification"
        return "unknown"
    return None


def explain_classification(
    headers: Mapping[str, Any] | MessageHeaders | None,
    body: str | None,
    *,
    attachments: Sequence[AttachmentMeta] = (),
    in_junk_folder: bool = False,
    message_class: str | None = None,
) -> MessageClassification:
    """``classify_message`` plus the signals that decided it (see module docstring for order)."""
    hdrs = MessageHeaders.from_raw(headers)
    text = _normalize_text(body)
    subject = _fold(hdrs.get("subject"))
    signals: list[ClassificationSignal] = []

    def done(kind: ReplyMessageType) -> MessageClassification:
        return MessageClassification(message_type=kind, signals=tuple(dict.fromkeys(signals)))

    item_class = (message_class or "").strip().lower()
    if item_class and not is_processable_item(item_class):
        signals.append(ClassificationSignal.NON_MAIL_ITEM)
        return done(ReplyMessageType.AMBIGUOUS)
    if _is_spam(hdrs):
        signals.append(ClassificationSignal.SPAM_HEADER)
    if in_junk_folder:
        signals.append(ClassificationSignal.JUNK_FOLDER)
    if signals:
        return done(ReplyMessageType.SPAM)

    if item_class.startswith("report.ipm.note.ndr"):
        signals.append(ClassificationSignal.OUTLOOK_NDR_CLASS)
        return done(ReplyMessageType.BOUNCE)
    if item_class.startswith("report.ipm.note."):
        signals.append(ClassificationSignal.OUTLOOK_RECEIPT_CLASS)
        return done(ReplyMessageType.DELIVERY_NOTICE)
    if item_class.startswith(("ipm.note.rules.ooftemplate", "ipm.note.rules.replytemplate")):
        signals.append(ClassificationSignal.OUTLOOK_AUTO_REPLY_CLASS)
        return done(ReplyMessageType.AUTO_REPLY)

    report = _report_kind(hdrs, attachments)
    # Report subjects never start with a human reply/forward prefix ("AW: ... nicht zustellbar?",
    # "R: ... consegna ritardata"): behind one, delivery wording is the sender's own text and only
    # structural evidence (DSN/MDN parts, mailer-daemon sender, X-Failed-Recipients) counts.
    human_subject = bool(_REPLY_PREFIX_RE.match(subject))
    delay_subject = not human_subject and bool(_DELAY_SUBJECT_RE.search(subject))
    if delay_subject:
        signals.append(ClassificationSignal.DELAY_SUBJECT)
    if report == "delivery-status":
        signals.append(ClassificationSignal.DSN_REPORT)
        details = parse_delivery_report(text)
        if details.action == "failed":
            signals.append(ClassificationSignal.DSN_ACTION_FAILED)
            return done(ReplyMessageType.BOUNCE)
        if details.action is not None:
            signals.append(ClassificationSignal.DSN_ACTION_NOT_FAILED)
            return done(ReplyMessageType.DELIVERY_NOTICE)
        return done(ReplyMessageType.DELIVERY_NOTICE if delay_subject else ReplyMessageType.BOUNCE)
    if report == "disposition-notification":
        signals.append(ClassificationSignal.MDN_REPORT)
        return done(ReplyMessageType.DELIVERY_NOTICE)
    if report == "unknown":
        signals.append(ClassificationSignal.UNKNOWN_REPORT_TYPE)
        return done(ReplyMessageType.AMBIGUOUS)

    daemon = _is_mailer_daemon(hdrs)
    failed_rcpt = bool(hdrs.get_all("x-failed-recipients"))
    bounce_subject = not human_subject and bool(_BOUNCE_SUBJECT_RE.search(subject))
    if daemon:
        signals.append(ClassificationSignal.MAILER_DAEMON_SENDER)
    if failed_rcpt:
        signals.append(ClassificationSignal.FAILED_RECIPIENTS_HEADER)
    if bounce_subject:
        signals.append(ClassificationSignal.BOUNCE_SUBJECT)
    if daemon or failed_rcpt or bounce_subject:
        details = parse_delivery_report(text)
        if details.action == "failed" or failed_rcpt or bounce_subject:
            return done(ReplyMessageType.BOUNCE)
        if delay_subject or details.action is not None:
            return done(ReplyMessageType.DELIVERY_NOTICE)
        return done(ReplyMessageType.BOUNCE)
    if delay_subject:
        return done(ReplyMessageType.DELIVERY_NOTICE)
    if not human_subject and _DELIVERY_SUBJECT_RE.search(subject):
        signals.append(ClassificationSignal.DELIVERY_SUBJECT)
        return done(ReplyMessageType.DELIVERY_NOTICE)

    signals.extend(_auto_reply_header_signals(hdrs))
    if _AUTO_REPLY_SUBJECT_RE.search(subject) or (
        not _REPLY_PREFIX_RE.match(subject) and _AUTO_REPLY_SUBJECT_PREFIX_RE.match(subject)
    ):
        signals.append(ClassificationSignal.AUTO_REPLY_SUBJECT)
    if signals:
        return done(ReplyMessageType.AUTO_REPLY)

    senders: list[str | None] = []
    for value in hdrs.get_all("from"):
        senders.extend(parse_address_list(value))
    if not senders:
        signals.append(ClassificationSignal.MISSING_SENDER)
    elif len(senders) > 1:
        signals.append(ClassificationSignal.MULTIPLE_SENDERS)
    elif senders[0] is None:
        signals.append(ClassificationSignal.INVALID_SENDER)
    opening = _lower_same_length(strip_quoted_text(text)[0][:600])
    if _AUTO_REPLY_BODY_RE.search(opening):
        signals.append(ClassificationSignal.AUTO_REPLY_BODY_PHRASE)
    if signals:
        return done(ReplyMessageType.AMBIGUOUS)
    return done(ReplyMessageType.SELLER_REPLY)


def classify_message(
    headers: Mapping[str, Any] | MessageHeaders | None,
    body: str | None,
    *,
    attachments: Sequence[AttachmentMeta] = (),
    in_junk_folder: bool = False,
    message_class: str | None = None,
) -> ReplyMessageType:
    """Message type of one inbound item: seller reply, auto-reply, bounce, delivery notice, spam
    or ambiguous. Never raises on hostile header/body content."""
    return explain_classification(
        headers, body, attachments=attachments, in_junk_folder=in_junk_folder, message_class=message_class
    ).message_type


# =============================================================================================
# Inquiry bindings and correlation
# =============================================================================================


class InquiryBindingState(StrEnum):
    """Binding state as synced to the mailbox worker (spec 37.8 inquiry-bindings endpoint)."""

    ACTIVE = "active"
    SUPPRESSED = "suppressed"  # no further sends; replies still belong to the inquiry
    UNCERTAIN = "uncertain"  # send outcome unknown; send-intent references may resolve it
    TOMBSTONED = "tombstoned"  # revoked: never grants access to mailbox content


_REF_TEXT_RE: Final = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


class InquiryBinding(BaseModel):
    """What the worker may match a reply against (one inquiry, one binding version)."""

    model_config = _FROZEN

    inquiry_id: UUID
    binding_version: int = Field(ge=1)
    mailbox_binding_id: UUID
    provider: EmailProviderKind
    state: InquiryBindingState = InquiryBindingState.ACTIVE
    outbound_message_ids: tuple[str, ...] = Field(default=(), max_length=20)
    send_intent_message_ids: tuple[str, ...] = Field(default=(), max_length=20)
    provider_message_ids: tuple[str, ...] = Field(default=(), max_length=20)
    provider_thread_ids: tuple[str, ...] = Field(default=(), max_length=20)
    verified_seller_aliases: tuple[str, ...] = Field(default=(), max_length=20)
    listing_references: tuple[str, ...] = Field(default=(), max_length=20)
    listing_urls: tuple[str, ...] = Field(default=(), max_length=20)
    listing_id: UUID | None = None
    vehicle_cluster_id: UUID | None = None
    is_canary: bool = False

    @field_validator("outbound_message_ids", "send_intent_message_ids")
    @classmethod
    def _msgids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            normalized = normalize_message_id(item)
            if normalized is None:
                raise ValueError("binding Message-IDs must be valid RFC Message-IDs")
            if normalized not in result:
                result.append(normalized)
        return tuple(result)

    @field_validator("verified_seller_aliases")
    @classmethod
    def _aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            address = canonical_address(item)
            if address is None:
                raise ValueError("verified seller aliases must be plain valid e-mail addresses")
            if address not in result:
                result.append(address)
        return tuple(result)

    @field_validator("provider_message_ids", "provider_thread_ids", "listing_references", "listing_urls")
    @classmethod
    def _texts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not _REF_TEXT_RE.fullmatch(item) or not item.strip():
                raise ValueError("binding references must be short single-line text")
        return tuple(dict.fromkeys(item.strip() for item in value))

    def all_message_ids(self) -> frozenset[str]:
        return frozenset((*self.outbound_message_ids, *self.send_intent_message_ids))


class CorrelationOutcome(StrEnum):
    MATCHED = "matched"
    QUARANTINED = "quarantined"
    UNMATCHED = "unmatched"


class CorrelationReason(StrEnum):
    HEADER_REFERENCE_MATCH = "HEADER_REFERENCE_MATCH"
    THREAD_MATCH = "THREAD_MATCH"
    RETURNED_ORIGINAL_MATCH = "RETURNED_ORIGINAL_MATCH"
    SEND_INTENT_MATCH = "SEND_INTENT_MATCH"
    SENDER_VERIFIED = "SENDER_VERIFIED"
    REFERENCE_CORROBORATED = "REFERENCE_CORROBORATED"
    REFERENCE_NOT_FOUND = "REFERENCE_NOT_FOUND"
    DSN_RECIPIENT_VERIFIED = "DSN_RECIPIENT_VERIFIED"
    RESOLVES_UNCERTAIN_SEND = "RESOLVES_UNCERTAIN_SEND"
    # quarantine
    FORWARDED = "FORWARDED"
    CHANGED_ADDRESS = "CHANGED_ADDRESS"
    AMBIGUOUS_SENDER = "AMBIGUOUS_SENDER"
    MULTIPLE_INQUIRIES = "MULTIPLE_INQUIRIES"
    CONFLICTING_REFERENCE = "CONFLICTING_REFERENCE"
    THREAD_ONLY_UNCORROBORATED = "THREAD_ONLY_UNCORROBORATED"
    SENDER_ONLY_NO_THREAD = "SENDER_ONLY_NO_THREAD"
    DSN_RECIPIENT_MISMATCH = "DSN_RECIPIENT_MISMATCH"
    SPAM_MESSAGE = "SPAM_MESSAGE"
    AMBIGUOUS_MESSAGE = "AMBIGUOUS_MESSAGE"
    # unmatched
    NO_BINDING_MATCH = "NO_BINDING_MATCH"
    SUBJECT_ONLY = "SUBJECT_ONLY"
    BINDING_REVOKED = "BINDING_REVOKED"
    OTHER_MAILBOX_BINDING = "OTHER_MAILBOX_BINDING"
    NON_MAIL_ITEM = "NON_MAIL_ITEM"  # meetings, sharing invitations, tasks: never reply processing
    OWN_MESSAGE = "OWN_MESSAGE"  # sent by the mailbox owner (e.g. a manual reply in the thread)


UploadScope = Literal["full", "quarantine", "none"]


class CorrelationResult(BaseModel):
    """Whether an inbound message belongs to one of this system's inquiries."""

    model_config = _FROZEN

    version: str = CORRELATION_VERSION
    outcome: CorrelationOutcome
    message_type: ReplyMessageType
    inquiry_id: UUID | None = None
    binding_version: int | None = None
    candidate_inquiry_ids: tuple[UUID, ...] = ()
    reasons: tuple[CorrelationReason, ...] = ()
    sender_verified: bool = False
    reference_corroborated: bool | None = None
    resolves_uncertain_send: bool = False
    retry_after_binding_sync: bool = False
    is_canary: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> CorrelationResult:
        if self.outcome == CorrelationOutcome.MATCHED and (
            self.inquiry_id is None or self.binding_version is None
        ):
            raise ValueError("a matched correlation names exactly one inquiry and binding version")
        if self.outcome == CorrelationOutcome.MATCHED and self.message_type in (
            ReplyMessageType.SPAM,
            ReplyMessageType.AMBIGUOUS,
        ):
            raise ValueError("spam and ambiguous messages are never matched; they are quarantined")
        if self.outcome == CorrelationOutcome.UNMATCHED and self.inquiry_id is not None:
            raise ValueError("an unmatched message has no inquiry")
        if self.resolves_uncertain_send and self.outcome != CorrelationOutcome.MATCHED:
            raise ValueError("only a matched message can resolve an uncertain send")
        return self

    @property
    def upload_scope(self) -> UploadScope:
        """What may leave the local mailbox: unrelated mail never does (spec 37.7)."""
        if self.outcome == CorrelationOutcome.MATCHED:
            return "full"
        if self.outcome == CorrelationOutcome.QUARANTINED and self.inquiry_id is not None:
            return "quarantine"
        return "none"


_FORWARD_SUBJECT_RE: Final = re.compile(
    r"^\s*(?:(?:re|aw|r|rif|sv|antw|ri)\s*:\s*)*(?:fwd?|wg|tr|i|inoltro|rv|enc|weiterleitung)\s*:",
    re.IGNORECASE,
)
_FORWARD_BODY_RE: Final = _rx_any(
    (
        r"^-{2,}\s*forwarded message\s*-{2,}",
        r"^begin forwarded message\s*:",
        r"^-{2,}\s*weitergeleitete nachricht\s*-{2,}",
        r"^anfang der weitergeleiteten nachricht\s*:",
        r"^-{2,}\s*messaggio inoltrato\s*-{2,}",
        r"^-{2,}\s*message transf[ée]r[ée]\s*-{2,}",
        r"^d[ée]but du message r[ée]exp[ée]di[ée]\s*:",
    ),
    re.IGNORECASE | re.MULTILINE,
)


def is_forwarded(subject: str | None, body: str | None) -> bool:
    """Forward prefix in the subject (Fwd/FW/WG/I/TR/Inoltro) or a forwarded-message marker."""
    if _FORWARD_SUBJECT_RE.search(_normalize_text(subject)):
        return True
    return bool(_FORWARD_BODY_RE.search(_normalize_text(body)))


def _reference_pattern(ref: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(_lower_same_length(ref))}(?![A-Za-z0-9])")


def _mentions_binding(text_lower: str, binding: InquiryBinding) -> bool:
    for ref in binding.listing_references:
        if len(ref) >= 4 and _reference_pattern(ref).search(text_lower):
            return True
    for url in binding.listing_urls:
        lowered = _lower_same_length(url)
        bare = re.sub(r"^https?://", "", lowered)
        if len(bare) >= 8 and (lowered in text_lower or bare in text_lower):
            return True
    return False


def _latest_bindings(
    bindings: Sequence[InquiryBinding], mailbox_binding_id: UUID
) -> tuple[list[InquiryBinding], set[UUID], bool]:
    """Newest binding per inquiry for the mailbox: (usable, revoked inquiry ids, other-mailbox seen)."""
    newest: dict[UUID, InquiryBinding] = {}
    other_mailbox = False
    for binding in bindings:
        if binding.mailbox_binding_id != mailbox_binding_id:
            other_mailbox = True
            continue
        current = newest.get(binding.inquiry_id)
        if current is None or binding.binding_version > current.binding_version:
            newest[binding.inquiry_id] = binding
        elif binding.binding_version == current.binding_version and binding != current:
            # Two different payloads under one version: treat as revoked until resynced.
            newest[binding.inquiry_id] = binding.model_copy(update={"state": InquiryBindingState.TOMBSTONED})
    usable = [b for b in newest.values() if b.state != InquiryBindingState.TOMBSTONED]
    revoked = {b.inquiry_id for b in newest.values() if b.state == InquiryBindingState.TOMBSTONED}
    return usable, revoked, other_mailbox


def correlate_reply(
    message: InboundMessage,
    bindings: Sequence[InquiryBinding],
    *,
    message_type: ReplyMessageType | None = None,
    all_bindings_for_reference_check: Sequence[InquiryBinding] | None = None,
    own_addresses: Sequence[str] = (),
) -> CorrelationResult:
    """Match one inbound message to at most one inquiry (see module docstring).

    ``bindings`` are the worker's synced bindings (any mailbox; only the message's own mailbox
    is used). ``message_type`` defaults to ``classify_message``. ``own_addresses`` are the
    mailbox owner's own sender addresses: a message from one of them (a manual reply by the
    owner in the seller thread, a Sent Items copy) is never a seller reply and stays local.
    Non-mail Outlook items (meetings, sharing invitations, tasks) are never reply-processed
    (spec 37.6) and stay local too. The decision is made locally, before any body or attachment
    leaves the mailbox.
    """
    mtype = message_type or classify_message(
        message.headers,
        message.body_text,
        attachments=message.attachments,
        in_junk_folder=message.in_junk_folder,
        message_class=message.message_class,
    )
    identity = message.identity
    usable, revoked, other_mailbox = _latest_bindings(bindings, identity.mailbox_binding_id)
    if message.message_class and not is_processable_item(message.message_class):
        return CorrelationResult(
            outcome=CorrelationOutcome.UNMATCHED,
            message_type=mtype,
            reasons=(CorrelationReason.NON_MAIL_ITEM,),
        )
    own = {address for address in (canonical_address(a) for a in own_addresses) if address}
    from_addresses = message.from_addresses
    if (
        own
        and len(from_addresses) == 1
        and from_addresses[0] in own
        # An owner-controlled test seller address (activation canary) stays matchable.
        and not any(from_addresses[0] in b.verified_seller_aliases for b in usable)
    ):
        return CorrelationResult(
            outcome=CorrelationOutcome.UNMATCHED,
            message_type=mtype,
            reasons=(CorrelationReason.OWN_MESSAGE,),
        )
    reference_pool = list(all_bindings_for_reference_check or usable)
    refs = set(message.reference_ids())
    dsn = (
        parse_delivery_report(message.body_text)
        if mtype in (ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE)
        else None
    )
    returned_ids = set(dsn.original_message_ids) if dsn else set()

    header_hits: dict[UUID, InquiryBinding] = {}
    returned_hits: dict[UUID, InquiryBinding] = {}
    thread_hits: dict[UUID, InquiryBinding] = {}
    send_intent_inquiries: set[UUID] = set()
    for binding in usable:
        ids = binding.all_message_ids()
        hit = refs & ids
        if hit:
            header_hits[binding.inquiry_id] = binding
        if returned_ids & ids:
            returned_hits[binding.inquiry_id] = binding
        intent_only = set(binding.send_intent_message_ids) - set(binding.outbound_message_ids)
        if (hit | (returned_ids & ids)) & intent_only:
            send_intent_inquiries.add(binding.inquiry_id)
        if (
            identity.provider_thread_id is not None
            and identity.provider == binding.provider
            and identity.provider_thread_id in binding.provider_thread_ids
        ):
            thread_hits[binding.inquiry_id] = binding
    revoked_hit = any(
        refs & b.all_message_ids()
        for b in bindings
        if b.inquiry_id in revoked and b.mailbox_binding_id == identity.mailbox_binding_id
    )
    other_mailbox_hit = other_mailbox and any(
        refs & b.all_message_ids() for b in bindings if b.mailbox_binding_id != identity.mailbox_binding_id
    )

    strong = {**header_hits, **returned_hits}
    reasons: list[CorrelationReason] = []
    if header_hits:
        reasons.append(CorrelationReason.HEADER_REFERENCE_MATCH)
    if returned_hits:
        reasons.append(CorrelationReason.RETURNED_ORIGINAL_MATCH)
    if thread_hits:
        reasons.append(CorrelationReason.THREAD_MATCH)

    linked = set(strong) | set(thread_hits)
    if len(linked) > 1:
        return CorrelationResult(
            outcome=CorrelationOutcome.QUARANTINED,
            message_type=mtype,
            candidate_inquiry_ids=tuple(sorted(linked, key=str)),
            reasons=(*reasons, CorrelationReason.MULTIPLE_INQUIRIES),
        )
    text_lower = _lower_same_length(_normalize_text(f"{message.subject}\n{message.body_text}"))
    senders = message.from_addresses
    sender = senders[0] if len(senders) == 1 else None

    if not linked:
        return _correlate_without_thread(
            mtype=mtype,
            usable=usable,
            text_lower=text_lower,
            sender=sender,
            revoked_hit=revoked_hit,
            other_mailbox_hit=other_mailbox_hit,
            refs=refs,
        )

    inquiry_id = next(iter(linked))
    binding = strong.get(inquiry_id) or thread_hits[inquiry_id]
    thread_only = inquiry_id not in strong
    mentions_this = _mentions_binding(text_lower, binding)
    mentions_other = any(
        _mentions_binding(text_lower, other) for other in reference_pool if other.inquiry_id != inquiry_id
    )
    reference_corroborated: bool | None = mentions_this
    if mentions_this:
        reasons.append(CorrelationReason.REFERENCE_CORROBORATED)
    else:
        reasons.append(CorrelationReason.REFERENCE_NOT_FOUND)

    quarantine: list[CorrelationReason] = []
    sender_verified = False
    if mtype in (ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE):
        recipients = set(dsn.final_recipients) if dsn else set()
        if recipients and not recipients & set(binding.verified_seller_aliases):
            quarantine.append(CorrelationReason.DSN_RECIPIENT_MISMATCH)
        elif recipients:
            reasons.append(CorrelationReason.DSN_RECIPIENT_VERIFIED)
        if thread_only:
            quarantine.append(CorrelationReason.THREAD_ONLY_UNCORROBORATED)
    else:
        if len(senders) != 1 or sender is None:
            quarantine.append(CorrelationReason.AMBIGUOUS_SENDER)
        elif sender in binding.verified_seller_aliases:
            sender_verified = True
            reasons.append(CorrelationReason.SENDER_VERIFIED)
        else:
            quarantine.append(CorrelationReason.CHANGED_ADDRESS)
        if is_forwarded(message.subject, message.body_text):
            quarantine.append(CorrelationReason.FORWARDED)
        if thread_only and not (sender_verified and mentions_this):
            quarantine.append(CorrelationReason.THREAD_ONLY_UNCORROBORATED)
    if mentions_other and not mentions_this:
        quarantine.append(CorrelationReason.CONFLICTING_REFERENCE)
    if mtype == ReplyMessageType.SPAM:
        quarantine.append(CorrelationReason.SPAM_MESSAGE)
    if mtype == ReplyMessageType.AMBIGUOUS:
        quarantine.append(CorrelationReason.AMBIGUOUS_MESSAGE)

    if quarantine:
        return CorrelationResult(
            outcome=CorrelationOutcome.QUARANTINED,
            message_type=mtype,
            inquiry_id=inquiry_id,
            binding_version=binding.binding_version,
            candidate_inquiry_ids=(inquiry_id,),
            reasons=(*reasons, *quarantine),
            sender_verified=sender_verified,
            reference_corroborated=reference_corroborated,
            is_canary=binding.is_canary,
        )
    # Any strong link (In-Reply-To/References or the returned original of a DSN) to a Message-ID
    # of an uncertain send proves that the message was submitted; a thread-only link does not.
    resolves = binding.state == InquiryBindingState.UNCERTAIN and not thread_only
    if inquiry_id in send_intent_inquiries:
        reasons.append(CorrelationReason.SEND_INTENT_MATCH)
    if resolves:
        reasons.append(CorrelationReason.RESOLVES_UNCERTAIN_SEND)
    return CorrelationResult(
        outcome=CorrelationOutcome.MATCHED,
        message_type=mtype,
        inquiry_id=inquiry_id,
        binding_version=binding.binding_version,
        candidate_inquiry_ids=(inquiry_id,),
        reasons=tuple(reasons),
        sender_verified=sender_verified,
        reference_corroborated=reference_corroborated,
        resolves_uncertain_send=resolves,
        is_canary=binding.is_canary,
    )


def _correlate_without_thread(
    *,
    mtype: ReplyMessageType,
    usable: Sequence[InquiryBinding],
    text_lower: str,
    sender: str | None,
    revoked_hit: bool,
    other_mailbox_hit: bool,
    refs: set[str],
) -> CorrelationResult:
    """No header/thread link: a verified seller address *plus* a reference to that inquiry's
    listing can only produce a quarantined *possible* match; a subject/reference match alone, or
    a verified sender alone (a dealer newsletter, unrelated mail), never correlates."""
    # A reply to an outbound message whose binding has not synced yet: keep only a bounded local
    # locator and retry after the next binding sync (spec 37.8 reply-before-binding race).
    retry = bool(refs) and not revoked_hit and not other_mailbox_hit
    by_sender = [b for b in usable if sender is not None and sender in b.verified_seller_aliases]
    if by_sender and mtype not in (ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE):
        mentioned = [b for b in by_sender if _mentions_binding(text_lower, b)]
        if len(mentioned) == 1:
            only = mentioned[0]
            return CorrelationResult(
                outcome=CorrelationOutcome.QUARANTINED,
                message_type=mtype,
                inquiry_id=only.inquiry_id,
                binding_version=only.binding_version,
                candidate_inquiry_ids=(only.inquiry_id,),
                reasons=(
                    CorrelationReason.SENDER_VERIFIED,
                    CorrelationReason.SENDER_ONLY_NO_THREAD,
                    CorrelationReason.REFERENCE_CORROBORATED,
                ),
                sender_verified=True,
                reference_corroborated=True,
                is_canary=only.is_canary,
            )
        if mentioned:
            return CorrelationResult(
                outcome=CorrelationOutcome.QUARANTINED,
                message_type=mtype,
                candidate_inquiry_ids=tuple(sorted({b.inquiry_id for b in mentioned}, key=str)),
                reasons=(
                    CorrelationReason.SENDER_VERIFIED,
                    CorrelationReason.SENDER_ONLY_NO_THREAD,
                    CorrelationReason.MULTIPLE_INQUIRIES,
                ),
                sender_verified=True,
            )
        # Verified sender, no thread link and no reference to any of its listings: not even a
        # possible match. The content stays in the local mailbox.
        return CorrelationResult(
            outcome=CorrelationOutcome.UNMATCHED,
            message_type=mtype,
            reasons=(
                CorrelationReason.SENDER_VERIFIED,
                CorrelationReason.SENDER_ONLY_NO_THREAD,
                CorrelationReason.REFERENCE_NOT_FOUND,
                CorrelationReason.NO_BINDING_MATCH,
            ),
            sender_verified=True,
            retry_after_binding_sync=retry,
        )
    reasons: list[CorrelationReason] = []
    if revoked_hit:
        reasons.append(CorrelationReason.BINDING_REVOKED)
    if other_mailbox_hit:
        reasons.append(CorrelationReason.OTHER_MAILBOX_BINDING)
    if any(_mentions_binding(text_lower, b) for b in usable):
        reasons.append(CorrelationReason.SUBJECT_ONLY)
    reasons.append(CorrelationReason.NO_BINDING_MATCH)
    return CorrelationResult(
        outcome=CorrelationOutcome.UNMATCHED,
        message_type=mtype,
        reasons=tuple(reasons),
        retry_after_binding_sync=retry,
    )


class UnmatchedRetention(BaseModel):
    """What the worker keeps for an unmatched message (metadata locator only, never content)."""

    model_config = _FROZEN

    retain_locator: bool
    retry_until: datetime | None
    surface_matching_gap: bool
    reason: str


def unmatched_retention(
    correlation: CorrelationResult,
    *,
    first_seen_locally_at: datetime,
    now: datetime,
    window: timedelta = UNMATCHED_RETRY_WINDOW,
) -> UnmatchedRetention:
    """Bounded retry for the reply-before-binding-sync race (spec 37.8).

    Only an unmatched message that carries reply references is retained, and only as a local
    locator, until ``first_seen + window``. After that the locator is dropped and an unresolved
    matching gap is surfaced (as a count, never content). Matched/quarantined messages need no
    retention entry.
    """
    if window <= timedelta(0):
        raise ValidationFailed("retry window must be positive")
    first = _aware(first_seen_locally_at)
    current = _aware(now)
    if correlation.outcome != CorrelationOutcome.UNMATCHED or not correlation.retry_after_binding_sync:
        return UnmatchedRetention(
            retain_locator=False,
            retry_until=None,
            surface_matching_gap=False,
            reason="not awaiting a binding",
        )
    until = first + window
    if current < until:
        return UnmatchedRetention(
            retain_locator=True,
            retry_until=until,
            surface_matching_gap=False,
            reason="reply references unknown outbound ids; retry after binding sync",
        )
    return UnmatchedRetention(
        retain_locator=False,
        retry_until=until,
        surface_matching_gap=True,
        reason="retry window elapsed without a binding; unresolved matching gap",
    )


# =============================================================================================
# Source fingerprint, dedup keys and ingest idempotency
# =============================================================================================


def _normalize_body_for_fingerprint(text: str) -> str:
    value = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).strip("\n")


def _normalize_subject(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


class ReplySourceContent(BaseModel):
    """The schema-selected immutable source content of one reply (spec 37.8)."""

    model_config = _FROZEN

    internet_message_id: str | None = None
    provider_message_id: str | None = None
    from_address: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    subject: str = ""
    body_text: str = ""
    attachments: tuple[AttachmentMeta, ...] = ()

    def material(self) -> dict[str, Any]:
        return {
            "version": FINGERPRINT_VERSION,
            "internet_message_id": self.internet_message_id,
            # Where an Internet Message-ID exists, a provider message id is a per-copy locator
            # (Microsoft Graph ids change when a message is moved; Gmail/Outlook copies in two
            # folders differ): including it would turn a moved or copied message into a false
            # IDEMPOTENCY_CONFLICT. It is identity only as the fallback key.
            "provider_message_id": None if self.internet_message_id else self.provider_message_id,
            "from": self.from_address,
            "in_reply_to": self.in_reply_to,
            "references": list(self.references),
            "subject": _normalize_subject(self.subject),
            "body": _normalize_body_for_fingerprint(self.body_text),
            "attachments": sorted(
                (a.content_material() for a in self.attachments),
                key=lambda m: (m["sha256"], m["filename"], m["mime_type"], m["byte_size"]),
            ),
        }


def source_content_fingerprint(content: ReplySourceContent) -> str:
    """SHA-256 over the normalised immutable source content (locators/sync metadata excluded)."""
    return sha256_json(content.material())


class ReplyDedupKey(BaseModel):
    """Stable per-mailbox message identity (spec 37.6): Internet Message-ID, else the provider
    message id, else a content hash scoped to the mailbox."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    kind: Literal["internet_message_id", "provider_message_id", "content_hash"]
    value: str = Field(min_length=1, max_length=1024)

    def as_string(self) -> str:
        return f"{self.mailbox_binding_id}:{self.kind}:{self.value}"


def reply_dedup_key(mailbox_binding_id: UUID, content: ReplySourceContent) -> ReplyDedupKey:
    if content.internet_message_id:
        return ReplyDedupKey(
            mailbox_binding_id=mailbox_binding_id,
            kind="internet_message_id",
            value=content.internet_message_id,
        )
    if content.provider_message_id:
        return ReplyDedupKey(
            mailbox_binding_id=mailbox_binding_id,
            kind="provider_message_id",
            value=content.provider_message_id,
        )
    return ReplyDedupKey(
        mailbox_binding_id=mailbox_binding_id, kind="content_hash", value=source_content_fingerprint(content)
    )


class MessageLocator(BaseModel):
    """A mutable retrieval locator (Outlook EntryID/StoreID). Recorded in locator history only."""

    model_config = _FROZEN

    outlook_entry_id: str | None = Field(default=None, max_length=1024)
    outlook_store_id: str | None = Field(default=None, max_length=1024)


class StoredReplyIngest(BaseModel):
    """What ``ops.mail_ingest_dedup`` holds for an already ingested message."""

    model_config = _FROZEN

    reply_id: UUID
    dedup_key: str = Field(min_length=1, max_length=1200)
    idempotency_key: str = Field(min_length=8, max_length=128)
    fingerprint: str
    fingerprint_version: str = FINGERPRINT_VERSION
    locators: tuple[MessageLocator, ...] = ()

    @field_validator("fingerprint")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not _HEX64_RE.fullmatch(value):
            raise ValueError("fingerprint must be a sha256 hex digest")
        return value


class IngestDecisionKind(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    FINGERPRINT_VERSION_MISMATCH = "fingerprint_version_mismatch"


class IngestDecision(BaseModel):
    model_config = _FROZEN

    kind: IngestDecisionKind
    reply_id: UUID | None = None
    duplicate: bool = False
    locator_changed: bool = False
    record_locator: MessageLocator | None = None
    quarantine: bool = False
    error_code: Literal["IDEMPOTENCY_CONFLICT"] | None = None
    reason: str


_IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[\x21-\x7e]{8,128}$")


def decide_ingest(
    *,
    dedup_key: ReplyDedupKey | str,
    idempotency_key: str,
    fingerprint: str,
    locator: MessageLocator | None = None,
    existing_by_dedup_key: StoredReplyIngest | None,
    existing_by_idempotency_key: StoredReplyIngest | None,
    fingerprint_version: str = FINGERPRINT_VERSION,
) -> IngestDecision:
    """Idempotent ingest decision checking both the request idempotency key and the source key.

    - Neither known: ``NEW``.
    - The idempotency key was used for a *different* message: ``CONFLICT``.
    - Same source key, same fingerprint: ``DUPLICATE`` returning the existing reply id (a new
      locator, e.g. after a folder move, is returned for locator history).
    - Same source key, different fingerprint (same version): ``CONFLICT`` + quarantine
      (``IDEMPOTENCY_CONFLICT``); never an overwrite.
    - Stored fingerprint of another algorithm version: ``FINGERPRINT_VERSION_MISMATCH`` - the
      caller recomputes the stored row's fingerprint from its stored content and decides again.
    """
    key = dedup_key.as_string() if isinstance(dedup_key, ReplyDedupKey) else dedup_key
    if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise ValidationFailed("idempotency key must be 8-128 printable ASCII characters")
    if not _HEX64_RE.fullmatch(fingerprint):
        raise ValidationFailed("fingerprint must be a sha256 hex digest")
    if existing_by_idempotency_key is not None and existing_by_idempotency_key.dedup_key != key:
        return IngestDecision(
            kind=IngestDecisionKind.CONFLICT,
            quarantine=True,
            error_code="IDEMPOTENCY_CONFLICT",
            reason="idempotency key already used for a different source message",
        )
    existing = existing_by_dedup_key or existing_by_idempotency_key
    if existing is None:
        return IngestDecision(
            kind=IngestDecisionKind.NEW, record_locator=locator, reason="new source message"
        )
    if existing.fingerprint_version != fingerprint_version:
        return IngestDecision(
            kind=IngestDecisionKind.FINGERPRINT_VERSION_MISMATCH,
            reply_id=existing.reply_id,
            reason="stored fingerprint uses another algorithm version; recompute before deciding",
        )
    if existing.fingerprint != fingerprint:
        return IngestDecision(
            kind=IngestDecisionKind.CONFLICT,
            reply_id=existing.reply_id,
            quarantine=True,
            error_code="IDEMPOTENCY_CONFLICT",
            reason="same source identity with different immutable content; quarantined for investigation",
        )
    changed = locator is not None and locator not in existing.locators
    return IngestDecision(
        kind=IngestDecisionKind.DUPLICATE,
        reply_id=existing.reply_id,
        duplicate=True,
        locator_changed=changed,
        record_locator=locator if changed else None,
        reason="already ingested; existing reply returned",
    )


def raise_for_ingest_conflict(decision: IngestDecision) -> None:
    """Raise the typed ``IDEMPOTENCY_CONFLICT`` error for a conflicting decision."""
    if decision.kind == IngestDecisionKind.CONFLICT:
        raise IdempotencyConflict("The source message conflicts with an already ingested message")


# =============================================================================================
# Ingest request (spec 37.8 POST /v1/mail-workers/replies)
# =============================================================================================


class IngestSourceMessage(BaseModel):
    model_config = _FROZEN

    internet_message_id: str | None = None
    provider_message_id: str | None = Field(default=None, max_length=512)
    outlook_entry_id: str | None = Field(default=None, max_length=1024)
    outlook_store_id: str | None = Field(default=None, max_length=1024)
    received_at: datetime

    @field_validator("internet_message_id")
    @classmethod
    def _msgid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_message_id(value)
        if normalized is None:
            raise ValueError("internet_message_id is not a valid RFC Message-ID")
        return normalized

    @field_validator("provider_message_id", "outlook_entry_id", "outlook_store_id")
    @classmethod
    def _opaque(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(ord(c) < 33 or ord(c) == 127 for c in value)):
            raise ValueError("identifiers must not contain whitespace or control characters")
        return value

    @field_validator("received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class IngestHeaders(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True)

    from_address: str = Field(alias="from", max_length=320)
    in_reply_to: str | None = None
    references: tuple[str, ...] = Field(default=(), max_length=MAX_REFERENCES)

    @field_validator("from_address")
    @classmethod
    def _from(cls, value: str) -> str:
        address = canonical_address(value)
        if address is None:
            raise ValueError("from must be a single plain e-mail address")
        return address

    @field_validator("in_reply_to")
    @classmethod
    def _irt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_message_id(value)
        if normalized is None:
            raise ValueError("in_reply_to is not a valid RFC Message-ID")
        return normalized

    @field_validator("references")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for item in value:
            normalized = normalize_message_id(item)
            if normalized is None:
                raise ValueError("references must be valid RFC Message-IDs")
            result.append(normalized)
        return tuple(result)


class ReplyIngestRequest(BaseModel):
    """Domain view of the versioned reply ingest request (spec 37.8), with its size limits.

    The four trailing fields are optional domain extensions (defaults keep the spec shape):
    ``message_type`` carries the worker's local classification (the backend never sees the
    full headers, so it cannot tell an auto-reply or DSN from a seller reply itself),
    ``correlation_status``/``correlation_reasons`` mark a quarantined possible match that must
    not update a vehicle, and ``withheld_sensitive_attachments`` reports the presence of
    withheld sensitive attachments without repeating them. None of them is part of the
    immutable source fingerprint.
    """

    model_config = _FROZEN

    schema_version: Literal["1.0"]
    inquiry_id: UUID
    binding_version: int = Field(ge=1)
    mailbox_binding_id: UUID
    source_message: IngestSourceMessage
    headers: IngestHeaders
    subject: str = Field(max_length=MAX_SUBJECT_CHARS)
    sanitized_body_text: str
    detected_language: MessageLanguage | None = None
    attachments: tuple[AttachmentMeta, ...] = Field(default=(), max_length=MAX_ATTACHMENTS)
    observed_at: datetime
    message_type: ReplyMessageType = ReplyMessageType.SELLER_REPLY
    correlation_status: Literal["matched", "quarantined"] = "matched"
    correlation_reasons: tuple[CorrelationReason, ...] = Field(default=(), max_length=30)
    withheld_sensitive_attachments: int = Field(default=0, ge=0, le=200)

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        if _HEADER_CONTROL_RE.search(value):
            raise ValueError("subject must not contain control characters or line breaks")
        return value

    @field_validator("sanitized_body_text")
    @classmethod
    def _body(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("sanitized body exceeds 64 KiB")
        if _CONTROL_EXCEPT_NL_TAB_RE.search(value):
            raise ValueError("sanitized body contains control characters")
        return value

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _size(self) -> ReplyIngestRequest:
        if self.message_type in (ReplyMessageType.SPAM, ReplyMessageType.AMBIGUOUS) and (
            self.correlation_status != "quarantined"
        ):
            raise ValueError("spam and ambiguous messages are only ever uploaded as quarantined")
        encoded = canonical_json(self.model_dump(mode="json", by_alias=True)).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("ingest request exceeds 128 KiB")
        return self

    def source_content(self) -> ReplySourceContent:
        return ReplySourceContent(
            internet_message_id=self.source_message.internet_message_id,
            provider_message_id=self.source_message.provider_message_id,
            from_address=self.headers.from_address,
            in_reply_to=self.headers.in_reply_to,
            references=self.headers.references,
            subject=self.subject,
            body_text=self.sanitized_body_text,
            attachments=self.attachments,
        )

    def fingerprint(self) -> str:
        return source_content_fingerprint(self.source_content())

    def dedup_key(self) -> ReplyDedupKey:
        return reply_dedup_key(self.mailbox_binding_id, self.source_content())

    def locator(self) -> MessageLocator | None:
        sm = self.source_message
        if sm.outlook_entry_id is None and sm.outlook_store_id is None:
            return None
        return MessageLocator(outlook_entry_id=sm.outlook_entry_id, outlook_store_id=sm.outlook_store_id)


# =============================================================================================
# Body sanitising
# =============================================================================================


class RemovedCounts(BaseModel):
    model_config = _FROZEN

    emails: int = 0
    phones: int = 0
    addresses: int = 0
    bank_details: int = 0
    links: int = 0
    outbound_echo_lines: int = 0
    device_footer_lines: int = 0


class SanitizedReplyBody(BaseModel):
    """Minimised reply text safe to store privately and surface (spec 37.7 step 2)."""

    model_config = _FROZEN

    version: str = SANITIZER_VERSION
    text: str
    quoted_text_removed: bool
    signature_block_detected: bool
    truncated: bool
    removed: RemovedCounts
    original_chars: int


_QUOTE_HEADER_RE: Final = _rx_any(
    (
        r"^\s*-{2,}\s*(?:original message|ursprüngliche nachricht|original-nachricht|originalnachricht"
        r"|messaggio originale|message d'origine|message original|forwarded message"
        r"|weitergeleitete nachricht|messaggio inoltrato|message transf[ée]r[ée])\s*-{2,}\s*$",
        r"^\s*_{10,}\s*$",
        r"^\s*begin forwarded message\s*:\s*$",
    ),
    re.IGNORECASE,
)
# "On ... X <x@y> wrote:", "Il ... X ha scritto:", "Le ..., X a écrit :" end with the verb; German
# attributions put it before the name: "Am ... schrieb X <x@y>:".
_ATTRIBUTION_END_RE: Final = re.compile(
    r"(?:(?:wrote|ha scritto|a [ée]crit|scrisse)\s*:|\bschrieb\b[^\n]{0,200}:)\s*$", re.IGNORECASE
)
_ATTRIBUTION_START_RE: Final = re.compile(r"^\s*(?:on|am|il|le|el)\b", re.IGNORECASE)
_HEADER_BLOCK_FROM_RE: Final = re.compile(r"^\s*\**(?:from|von|da|de)\s*\**\s*:", re.IGNORECASE)
_HEADER_BLOCK_NEXT_RE: Final = re.compile(
    r"^\s*\**(?:sent|gesendet|inviato|envoy[ée]|date|datum|data|to|an|a|à|subject|betreff|oggetto|objet)"
    r"\s*\**\s*:",
    re.IGNORECASE,
)
_SIGNATURE_DELIMITER_RE: Final = re.compile(r"^-- ?$")
_CLOSING_RE: Final = re.compile(
    r"^\s*(?:mit (?:freundlichen|besten|herzlichen) gr(?:ü|ue|u)(?:ß|ss)en|freundliche gr(?:ü|ue)(?:ß|ss)e"
    r"|beste gr(?:ü|ue)(?:ß|ss)e|viele gr(?:ü|ue)(?:ß|ss)e|liebe gr(?:ü|ue)(?:ß|ss)e|mfg|lg|gru(?:ß|ss)"
    r"|cordiali saluti|distinti saluti|saluti|cordialmente|un saluto|cordialement|bien cordialement"
    r"|bien [àa] vous|salutations(?: distingu[ée]es)?|kind regards|best regards|warm regards|regards"
    r"|best wishes|sincerely|many thanks|thanks|cheers)\b[\s,.!]*$",
    re.IGNORECASE,
)
_DEVICE_FOOTER_RE: Final = re.compile(
    r"^\s*(?:sent from my \w+|von meinem \w+ gesendet|inviato da(?:l mio)? \w+|envoy[ée] de mon \w+"
    r"|get outlook for \w+|outlook f[üu]r \w+ beziehen|scarica outlook per \w+)\b.*$",
    re.IGNORECASE,
)
_EMAIL_RE: Final = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b"
)
_IBAN_RE: Final = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_URL_RE: Final = re.compile(r"(?:\bhttps?://|\bwww\.)[^\s<>\"'\])]{1,2048}", re.IGNORECASE)
# Phone numbers: digit groups joined by short separator runs; separators and digits are disjoint
# sets and the repetition is bounded, so matching stays linear. The digit count is checked after.
_PHONE_RE: Final = re.compile(
    r"(?<![\w.,:/=+-])(?:\+|00)?\(?\d{1,5}\)?(?:[ \t\xa0./-]{1,2}\(?\d{1,6}\)?){1,6}(?![\w])"
)
_LABELLED_PHONE_RE: Final = re.compile(
    r"(\b(?:tel|telefon|telefono|t[ée]l[ée]phone|t[ée]l|phone|mobil|mobile|handy|cell|cellulare|fax"
    r"|portable|whatsapp)\b\.?[ \t]*:?[ \t]*)([+(]?\d[\d \t\xa0()./-]{4,28}\d)",
    re.IGNORECASE,
)
_DATE_LIKE_RE: Final = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})$")
_LOOSE_EMAIL_RE: Final = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
)
_LOOSE_PHONE_RE: Final = re.compile(r"[+(]?\d[\d \t\xa0()./-]{5,40}\d")
_STREET_RE: Final = _rx_any(
    (
        r"\b[A-ZÄÖÜ][\wäöüß.-]{1,40}(?:stra(?:ß|ss)e|str\.|weg|allee|platz|gasse|ring|damm|ufer)"
        r"\s+\d{1,4}\s?[a-zA-Z]?\b",
        r"\b(?i:via|viale|piazza|piazzale|corso|largo|vicolo|strada|contrada|localit[àa]|loc\.)\s+"
        r"[A-ZÀ-Ýa-zà-ÿ'][\wÀ-ÿ' .-]{1,60}?,?\s+\d{1,4}[a-zA-Z]?\b",
        r"\b\d{1,4}(?:\s?(?:bis|ter))?,?\s+(?i:rue|avenue|av\.|boulevard|bd|chemin|route|place|impasse"
        r"|all[ée]e|quai)\s+[\wÀ-ÿ' -]{2,40}",
        r"\b\d{1,5}\s+[A-Z][\w .-]{1,40}\s(?i:street|st\.|road|rd\.|avenue|ave\.|lane|drive|way)\b",
    ),
    0,
)
_POSTCODE_CITY_RE: Final = re.compile(
    r"\b(?:[A-Z]{1,2}-)?\d{4,5}\s+[A-ZÄÖÜÀ-Ý][a-zäöüßà-ÿ]+(?:[ -][A-ZÄÖÜÀ-Ý]?[a-zäöüßà-ÿ]+){0,3}\b"
)
# Postcode and city directly after a removed street ("[address removed], 75002 Paris") belong to it.
_ADDRESS_TAIL_RE: Final = re.compile(
    r"\[address removed\](?:,?[ \t]*(?:[A-Z]{1,2}-)?\d{4,5}[ \t]+[A-ZÄÖÜÀ-Ý][a-zäöüßà-ÿ]+"
    r"(?:[ -][A-ZÄÖÜÀ-Ý]?[a-zäöüßà-ÿ]+){0,3}\b)+"
)
_CONTACT_LINE_PLACEHOLDER: Final = "[line removed: contact data]"
_SCRUB_INPUT_CHARS: Final = 2 * MAX_BODY_BYTES


def strip_quoted_text(text: str) -> tuple[str, bool]:
    """Remove a clearly delimited quoted previous message; return (text, removed?).

    Everything from the first delimiter on is dropped: "-----Original Message-----" style
    separators (DE/IT/FR/EN), Outlook underscore separators, "On ... wrote:" / "Am ... schrieb:"
    / "Il ... ha scritto:" / "Le ... a écrit :" attributions (also wrapped over two lines) and
    Outlook header blocks (From/Von/Da/De followed by Sent/Date/To/Subject lines). Remaining
    ``>``-quoted lines are removed individually (inline replies keep the seller's own text).
    An attribution followed by a ``>``-quoted block is bottom-posting: the attribution and the
    ``>`` lines go, and the seller's own text after the block is kept.
    """
    lines = text.split("\n")
    kept: list[str] = []
    removed = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if _QUOTE_HEADER_RE.search(line):
            removed = True
            break
        attribution = False
        if _ATTRIBUTION_END_RE.search(line):
            if _ATTRIBUTION_START_RE.search(line):
                attribution = True
            elif (
                index > 0
                and kept
                and kept[-1] == lines[index - 1]
                and _ATTRIBUTION_START_RE.search(lines[index - 1])
            ):
                kept.pop()  # an attribution wrapped over two lines
                attribution = True
        if attribution:
            removed = True
            following = index + 1
            while following < len(lines) and not lines[following].strip():
                following += 1
            if following < len(lines) and lines[following].lstrip().startswith(">"):
                index = following  # ">" lines are dropped below; own text after them is kept
                continue
            break  # an un-prefixed quote: everything after the attribution is the old message
        if _HEADER_BLOCK_FROM_RE.search(line):
            following_lines = lines[index + 1 : index + 6]
            if sum(1 for item in following_lines if _HEADER_BLOCK_NEXT_RE.search(item)) >= 2:
                removed = True
                break
        if line.lstrip().startswith(">"):
            removed = True
        else:
            kept.append(line)
        index += 1
    return "\n".join(kept), removed


def _phone_sub(counts: dict[str, int]) -> Any:
    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        digits = sum(ch.isdigit() for ch in value)
        if _DATE_LIKE_RE.fullmatch(value.strip()):
            return value
        if 7 <= digits <= 15 and (value.startswith(("+", "00", "0", "(")) or digits >= 9):
            counts["phones"] += 1
            return "[phone removed]"
        return value

    return replace


def _labelled_phone_sub(counts: dict[str, int]) -> Any:
    def replace(match: re.Match[str]) -> str:
        if sum(ch.isdigit() for ch in match.group(2)) >= 6:
            counts["phones"] += 1
            return f"{match.group(1)}[phone removed]"
        return match.group(0)

    return replace


def _link_placeholder(match: re.Match[str]) -> str:
    raw = match.group(0)
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        host = (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        host = ""
    host = re.sub(r"[^a-z0-9.-]", "", host.removeprefix("www."))[:100]
    if not re.search(r"[a-z]", host):
        return "[link removed]"  # numeric hosts could look like phone numbers
    return f"[link removed: {host}]"


def _guard_flags(line: str) -> set[str]:
    """Contact-data problem codes of the shared notification guard, checked in bounded chunks."""
    flags: set[str] = set()
    for start in range(0, max(len(line), 1), 1900):
        chunk = line[max(0, start - 100) : start + 1900]
        flags.update(p for p in text_problems(chunk) if p in {"EMAIL_ADDRESS", "PHONE_NUMBER"})
    return flags


def _scrub_contacts(body: str, counts: dict[str, int]) -> str:
    def count_sub(pattern: re.Pattern[str], key: str, replacement: Any, value: str) -> str:
        result, n = pattern.subn(replacement, value)
        counts[key] += n
        return result

    body = count_sub(_EMAIL_RE, "emails", "[email removed]", body)
    body = count_sub(_URL_RE, "links", _link_placeholder, body)
    body = count_sub(_IBAN_RE, "bank_details", "[bank details removed]", body)
    body = count_sub(_STREET_RE, "addresses", "[address removed]", body)
    body = _ADDRESS_TAIL_RE.sub("[address removed]", body)
    body = _LABELLED_PHONE_RE.sub(_labelled_phone_sub(counts), body)
    body = _PHONE_RE.sub(_phone_sub(counts), body)

    # Belt and braces: the shared notification guard must not find contact data in any line.
    def loose_phone(match: re.Match[str]) -> str:
        if "PHONE_NUMBER" in text_problems(match.group(0)):
            counts["phones"] += 1
            return "[phone removed]"
        return match.group(0)

    final_lines: list[str] = []
    for raw_line in body.split("\n"):
        line = raw_line
        flags = _guard_flags(line)
        if "EMAIL_ADDRESS" in flags:
            line = count_sub(_LOOSE_EMAIL_RE, "emails", "[email removed]", line)
        if "PHONE_NUMBER" in flags:
            line = _LOOSE_PHONE_RE.sub(loose_phone, line)
        if _guard_flags(line):
            counts["phones"] += 1
            line = _CONTACT_LINE_PLACEHOLDER
        final_lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(final_lines)).strip()


def _scrub_to_fixpoint(body: str, counts: dict[str, int]) -> str:
    """Removing one value can expose another (a placeholder next to a postcode, a re-joined
    number): scrub until nothing changes so sanitising sanitised text is a no-op."""
    for _ in range(4):
        scrubbed = _scrub_contacts(body, counts)
        if scrubbed == body:
            return body
        body = scrubbed
    return body


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    space = max(cut.rfind(" "), cut.rfind("\n"))
    return (cut[:space] if space > limit // 2 else cut).rstrip()


def sanitize_reply_body(text: str | None, *, known_outbound_text: str | None = None) -> SanitizedReplyBody:
    """Minimise an untrusted reply body before it is stored or surfaced (spec 37.7).

    Steps: normalise (NFKC, LF, no control/zero-width characters); strip the clearly delimited
    quoted previous message; drop lines that verbatim echo the outbound inquiry
    (``known_outbound_text``) and device footers; remove e-mail addresses, phone numbers
    (including labelled "Tel./Mobil/Cell" numbers; dates are kept), IBANs and street addresses
    everywhere and postcode/city lines inside the signature block (after a closing formula or
    ``-- ``); reduce links to their host; collapse blank lines; bound to 64 KiB UTF-8 at a word
    boundary. Seller names and ordinary text are kept. Removed values are only counted, never
    kept; a line that still trips the shared contact-data guard is replaced entirely.
    """
    original = _normalize_text(text)
    stripped, quoted_removed = strip_quoted_text(original)
    counts: dict[str, int] = dict.fromkeys(RemovedCounts.model_fields, 0)
    echo: set[str] = set()
    if known_outbound_text:
        for line in _normalize_text(known_outbound_text).split("\n"):
            folded = _fold(line)
            if len(folded) >= 12:
                echo.add(folded)
    kept: list[str] = []
    in_signature = False
    signature_detected = False
    for raw_line in stripped.split("\n"):
        line = raw_line
        if echo and _fold(line) in echo:
            counts["outbound_echo_lines"] += 1
            continue
        if _DEVICE_FOOTER_RE.match(line):
            counts["device_footer_lines"] += 1
            continue
        delimiter = bool(_SIGNATURE_DELIMITER_RE.match(line))
        if delimiter or _CLOSING_RE.match(line):
            in_signature = True
            signature_detected = True
            if delimiter:
                continue
        if in_signature and _POSTCODE_CITY_RE.search(line):
            counts["addresses"] += 1
            line = "[address removed]"
        kept.append(line)
    body = "\n".join(kept)
    # The output is capped at 64 KiB anyway: scrub only a bounded prefix (cut at a line break,
    # so no value is split) to keep hostile megabyte bodies cheap.
    pre_cut = len(body) > _SCRUB_INPUT_CHARS
    if pre_cut:
        head = body[:_SCRUB_INPUT_CHARS]
        newline = head.rfind("\n")
        body = head[:newline] if newline > _SCRUB_INPUT_CHARS // 2 else head.rsplit(" ", 1)[0]
    body = _scrub_to_fixpoint(body, counts)
    truncated = False
    marker = "\n[truncated]"
    if pre_cut or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        truncated = True
        limit = MAX_BODY_BYTES - len(marker.encode("utf-8"))
        body = _scrub_to_fixpoint(_truncate_utf8(body, limit), counts)
        body = _truncate_utf8(body, limit) + marker
    return SanitizedReplyBody(
        text=body,
        quoted_text_removed=quoted_removed,
        signature_block_detected=signature_detected,
        truncated=truncated,
        removed=RemovedCounts(**counts),
        original_chars=len(original),
    )


# =============================================================================================
# Claim extraction
# =============================================================================================


class AvailabilityClaimStatus(StrEnum):
    AVAILABLE = "available"
    SOLD = "sold"
    RESERVED = "reserved"
    NOT_AVAILABLE = "not_available"  # "no longer available" without saying why; never "sold"
    UNKNOWN = "unknown"


class DocumentKind(StrEnum):
    REGISTRATION = "registration"
    COC = "coc"
    SERVICE_HISTORY = "service_history"
    INSPECTION = "inspection"
    GENERAL = "documents_general"


class DocumentClaimStatus(StrEnum):
    AVAILABLE = "available"
    ATTACHED = "attached"
    REFUSED = "refused"
    NOT_AVAILABLE = "not_available"
    MENTIONED = "mentioned"  # named without a clear status


class RequestKind(StrEnum):
    PAYMENT = "payment"
    RESERVATION = "reservation"
    IDENTITY_DOCUMENT = "identity_document"
    APPOINTMENT = "appointment"
    COMMITMENT = "commitment"
    PRICE_ACCEPTANCE = "price_acceptance"
    OPT_OUT = "opt_out"
    COMPLAINT = "complaint"


ESCALATION_KINDS: Final = frozenset(
    {
        RequestKind.PAYMENT,
        RequestKind.RESERVATION,
        RequestKind.IDENTITY_DOCUMENT,
        RequestKind.APPOINTMENT,
        RequestKind.COMMITMENT,
        RequestKind.PRICE_ACCEPTANCE,
    }
)


class PriceCondition(StrEnum):
    NEGOTIABLE = "negotiable"
    FINAL_OR_LOWEST = "final_or_lowest"
    FIXED = "fixed"
    NO_DISCOUNT = "no_discount"
    CASH_PAYMENT = "cash_payment"
    EXPORT_ONLY = "export_only"
    CONDITIONAL = "conditional"
    TIME_LIMITED = "time_limited"


class AmountContext(StrEnum):
    PRICE = "price"
    UNLABELLED = "unlabelled"  # an amount with currency and no other context
    DEPOSIT_OR_PAYMENT = "deposit_or_payment"
    OTHER_COST = "other_cost"
    PREVIOUS_PRICE = "previous_price"


class EvidenceSpan(BaseModel):
    """Where a claim came from in the analysed (unquoted) text; the excerpt is sanitised."""

    model_config = _FROZEN

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)
    rule: str = Field(max_length=80)


class AvailabilityClaim(BaseModel):
    model_config = _FROZEN

    status: AvailabilityClaimStatus
    language: MessageLanguage | None
    evidence: EvidenceSpan
    confidence: Literal["high", "medium", "low"]
    implied: bool = False  # "not yet sold": availability implied, consistent with "reserved"


class PriceQuoteClaim(BaseModel):
    """A seller's stated price. Always an *unaccepted* quote; never a purchase price."""

    model_config = _FROZEN

    kind: Literal["single", "range", "minimum"]
    amount: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    basis: PriceBasis = PriceBasis.UNKNOWN
    conditions: tuple[PriceCondition, ...] = ()
    status: Literal["unaccepted_seller_quote"] = "unaccepted_seller_quote"
    accepted: Literal[False] = False
    quoted_at: datetime | None = None
    language: MessageLanguage | None = None
    evidence: EvidenceSpan
    confidence: Literal["high", "medium", "low"] = "medium"
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> PriceQuoteClaim:
        if self.kind in ("single", "minimum"):
            if self.amount is None or self.amount <= 0 or self.low is not None or self.high is not None:
                raise ValueError("a single/minimum quote has one positive amount")
        elif self.low is None or self.high is None or not 0 < self.low < self.high or self.amount is not None:
            raise ValueError("a range quote has 0 < low < high and no single amount")
        return self

    @field_validator("quoted_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class AmountMention(BaseModel):
    """Any other stated amount, preserved for the owner (deposit, fees, previous price, unparsed)."""

    model_config = _FROZEN

    context: AmountContext
    amount: Decimal | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    raw: str = Field(max_length=60)
    evidence: EvidenceSpan
    warnings: tuple[str, ...] = ()


class DocumentClaim(BaseModel):
    model_config = _FROZEN

    kind: DocumentKind
    status: DocumentClaimStatus
    language: MessageLanguage | None
    evidence: EvidenceSpan
    warnings: tuple[str, ...] = ()


class RequestFlag(BaseModel):
    model_config = _FROZEN

    kind: RequestKind
    language: MessageLanguage | None
    evidence: EvidenceSpan

    @property
    def escalates(self) -> bool:
        return self.kind in ESCALATION_KINDS


class InquiryQuestion(StrEnum):
    AVAILABILITY = "availability"
    DOCUMENTS = "documents"
    LOWEST_PRICE = "lowest_price"


AvailabilitySummary = Literal["available", "sold", "reserved", "not_available", "conflicting", "not_stated"]
_SUMMARY_FOR: Final[dict[AvailabilityClaimStatus, AvailabilitySummary]] = {
    AvailabilityClaimStatus.AVAILABLE: "available",
    AvailabilityClaimStatus.SOLD: "sold",
    AvailabilityClaimStatus.RESERVED: "reserved",
    AvailabilityClaimStatus.NOT_AVAILABLE: "not_available",
}


def summarize_availability(claims: Sequence[AvailabilityClaim]) -> AvailabilitySummary:
    """Single availability status of a set of claims (see ``ReplyClaims.availability_summary``)."""
    statuses = {c.status for c in claims if c.status != AvailabilityClaimStatus.UNKNOWN}
    if statuses & {AvailabilityClaimStatus.SOLD, AvailabilityClaimStatus.RESERVED}:
        statuses.discard(AvailabilityClaimStatus.NOT_AVAILABLE)
    explicit_available = any(c.status == AvailabilityClaimStatus.AVAILABLE and not c.implied for c in claims)
    if AvailabilityClaimStatus.RESERVED in statuses and not explicit_available:
        statuses.discard(AvailabilityClaimStatus.AVAILABLE)
    if not statuses:
        return "not_stated"
    if len(statuses) > 1:
        return "conflicting"
    return _SUMMARY_FOR[next(iter(statuses))]


class ReplyClaims(BaseModel):
    """Separate claim records extracted from one reply (spec 37.7 step 3)."""

    model_config = _FROZEN

    version: str = CLAIMS_VERSION
    language: MessageLanguage | None
    availability: tuple[AvailabilityClaim, ...] = ()
    prices: tuple[PriceQuoteClaim, ...] = ()
    other_amounts: tuple[AmountMention, ...] = ()
    documents: tuple[DocumentClaim, ...] = ()
    requests: tuple[RequestFlag, ...] = ()
    warnings: tuple[str, ...] = ()
    analyzed_chars: int = 0

    @property
    def availability_summary(self) -> AvailabilitySummary:
        """One status for the reply, or ``conflicting``.

        Consistent combinations collapse: "sold"/"reserved" subsume "no longer available", and
        "not yet sold, but reserved" is reserved. Anything else with several statuses (for example
        "available" and "sold") is a contradiction the owner must see.
        """
        return summarize_availability(self.availability)

    @property
    def escalation_kinds(self) -> tuple[RequestKind, ...]:
        return tuple(dict.fromkeys(r.kind for r in self.requests if r.kind in ESCALATION_KINDS))

    @property
    def opted_out(self) -> bool:
        return any(r.kind == RequestKind.OPT_OUT for r in self.requests)

    @property
    def complaint(self) -> bool:
        return any(r.kind == RequestKind.COMPLAINT for r in self.requests)

    def answered_questions(self, *, vehicle_document_attachments: int = 0) -> frozenset[InquiryQuestion]:
        answered: set[InquiryQuestion] = set()
        if self.availability:
            answered.add(InquiryQuestion.AVAILABILITY)
        if (
            any(d.status != DocumentClaimStatus.MENTIONED for d in self.documents)
            or vehicle_document_attachments > 0
        ):
            answered.add(InquiryQuestion.DOCUMENTS)
        if self.prices:
            answered.add(InquiryQuestion.LOWEST_PRICE)
        return frozenset(answered)

    def unanswered_questions(self, *, vehicle_document_attachments: int = 0) -> tuple[InquiryQuestion, ...]:
        answered = self.answered_questions(vehicle_document_attachments=vehicle_document_attachments)
        return tuple(q for q in InquiryQuestion if q not in answered)


# --- sentence and clause segmentation ---------------------------------------------------------

_ABBREVIATIONS: Final = frozenset(
    {
        "ca", "inkl", "zzgl", "exkl", "excl", "incl", "nr", "bzw", "evtl", "ggf", "usw", "etc", "z", "b",
        "d", "h", "u", "mfg", "str", "tel", "approx", "pag", "sig",
        "dott", "ing", "rag", "mr", "mrs", "ms", "dr", "st", "av", "bd", "env", "tsd",
        "abs", "art", "geb", "mod", "ff", "vs", "co", "pp",
    }
)  # fmt: skip
_BOUNDARY_RE: Final = re.compile(r"\n|[!?;]+|\.+(?=\s|$)")
_CLAUSE_SEPARATOR_RE: Final = re.compile(
    r",\s+|\s+[-\N{EN DASH}\N{EM DASH}]\s+|\s+(?:aber|jedoch|doch|ma|per[òo]|mais|but|however)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    question: bool


def _sentences(text: str) -> list[_Span]:
    spans: list[_Span] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        token = match.group(0)
        if token.startswith("."):
            before = text[start : match.start()]
            word = re.search(r"([^\W\d_]+)\s*$", before)
            prev = text[match.start() - 1] if match.start() > 0 else ""
            following = text[match.end() :].lstrip()
            if word and before.endswith(word.group(1)) and word.group(1).lower() in _ABBREVIATIONS:
                continue
            if prev.isdigit() and not (following[:1].isupper() or not following):
                continue
        end = match.end()
        segment = text[start:end]
        if segment.strip():
            lead = len(segment) - len(segment.lstrip())
            spans.append(_Span(start + lead, end, "?" in token))
        start = end
    if text[start:].strip():
        segment = text[start:]
        lead = len(segment) - len(segment.lstrip())
        spans.append(_Span(start + lead, len(text), segment.rstrip().endswith("?")))
    return spans


def _clauses(text: str, span: _Span) -> list[tuple[int, int]]:
    parts: list[tuple[int, int]] = []
    cursor = span.start
    for match in _CLAUSE_SEPARATOR_RE.finditer(text, span.start, span.end):
        if match.start() > cursor:
            parts.append((cursor, match.start()))
        cursor = match.end()
    if cursor < span.end:
        parts.append((cursor, span.end))
    return parts


# --- vocabulary --------------------------------------------------------------------------------

_L = MessageLanguage
_NEGATIONS: Final[dict[MessageLanguage, frozenset[str]]] = {
    _L.DE: frozenset({"nicht", "kein", "keine", "keinen", "keiner", "nie", "niemals", "ohne"}),
    _L.IT: frozenset({"non", "mai", "né", "senza", "nessun", "nessuna", "nessuno"}),
    _L.FR: frozenset({"pas", "ne", "n'", "jamais", "aucun", "aucune", "sans"}),
    _L.EN: frozenset({"not", "no", "never", "without", "neither", "nor"}),
}
_CONDITIONALS: Final[dict[MessageLanguage, re.Pattern[str]]] = {
    _L.DE: re.compile(r"\b(?:falls|wenn|ob|sofern|vielleicht|eventuell|m[öo]glicherweise|wahrscheinlich)\b"),
    _L.IT: re.compile(r"\b(?:se|qualora|forse|eventualmente|nel caso|probabilmente)\b"),
    _L.FR: re.compile(
        r"(?:\bsi\b|\bs'il\b|\bpeut-[êe]tre\b|\b[ée]ventuellement\b|\bau cas\b|\bprobablement\b)"
    ),
    _L.EN: re.compile(r"\b(?:if|whether|maybe|perhaps|possibly|in case|probably)\b"),
}
_PASSIVE_SALE_BEFORE: Final[dict[MessageLanguage, frozenset[str]]] = {
    _L.DE: frozenset({"wird", "werden", "würde", "wuerde", "soll", "zu"}),
    _L.IT: frozenset({"viene", "verrà", "sarà", "va", "da"}),
    _L.FR: frozenset({"sera", "serait", "être", "etre", "à"}),
    _L.EN: frozenset({"will", "be", "to", "being", "would"}),
}


@dataclass(frozen=True, slots=True)
class _Rule:
    code: str
    pattern: re.Pattern[str]


def _rules(prefix: str, patterns: Sequence[str]) -> tuple[_Rule, ...]:
    return tuple(_Rule(f"{prefix}_{i}", re.compile(p)) for i, p in enumerate(patterns))


_AV_NOT_AVAILABLE: Final[dict[MessageLanguage, tuple[_Rule, ...]]] = {
    _L.DE: _rules(
        "de_not_available",
        (
            r"\bnicht mehr (?:verf[üu]gbar|erh[äa]ltlich|vorhanden|zu haben|da|im angebot|zu verkaufen)\b",
            r"\bnicht (?:verf[üu]gbar|erh[äa]ltlich)\b",
        ),
    ),
    _L.IT: _rules(
        "it_not_available",
        (
            r"\bnon (?:[èe]'? |e' )?pi[ùu] disponibile\b",
            r"\bnon (?:[èe]'? )?disponibile\b",
            r"\bnon pi[ùu] in vendita\b",
        ),
    ),
    _L.FR: _rules(
        "fr_not_available",
        (
            r"\bn'est plus disponible\b",
            r"\bplus disponible\b",
            r"\bn'est pas disponible\b",
            r"\bindisponible\b",
        ),
    ),
    _L.EN: _rules(
        "en_not_available",
        (
            r"\bno longer (?:available|for sale)\b",
            r"\bnot available\b",
            r"\bisn't available\b",
            r"\bunavailable\b",
        ),
    ),
}
_AV_NOT_YET_SOLD: Final[dict[MessageLanguage, tuple[_Rule, ...]]] = {
    _L.DE: _rules("de_not_yet_sold", (r"\bnoch nicht verkauft\b",)),
    _L.IT: _rules("it_not_yet_sold", (r"\bnon (?:[èe] )?ancora vendut[aoie]\b",)),
    _L.FR: _rules("fr_not_yet_sold", (r"\bpas encore vendue?s?\b",)),
    _L.EN: _rules(
        "en_not_yet_sold",
        (
            r"\bnot (?:yet|been) sold(?: yet)?\b",
            r"\bnot sold yet\b",
            r"\b(?:hasn't|has not|isn't|is not) (?:been )?sold yet\b",
        ),
    ),
}
_AV_SOLD: Final[dict[MessageLanguage, tuple[_Rule, ...]]] = {
    _L.DE: _rules(
        "de_sold",
        (r"\bverkauft\b(?!\s+(?:als|mit|ohne|wie)\b)", r"\bist (?:leider )?(?:schon |bereits )?weg\b"),
    ),
    _L.IT: _rules("it_sold", (r"\bvendut[aoie]\b(?!\s+(?:come|con|senza)\b)",)),
    _L.FR: _rules("fr_sold", (r"\bvendue?s?\b(?!\s+(?:en l'[ée]tat|tel(?:le)? quel(?:le)?|avec|sans)\b)",)),
    _L.EN: _rules(
        "en_sold",
        (r"\bsold\b(?!\s+(?:as\s+(?:seen|is)|with|without|together|separately)\b)", r"\bit'?s gone\b"),
    ),
}
_AV_RESERVED: Final[dict[MessageLanguage, tuple[_Rule, ...]]] = {
    _L.DE: _rules("de_reserved", (r"\breserviert\b", r"\bangezahlt\b")),
    _L.IT: _rules("it_reserved", (r"\b(?:riservat|prenotat|opzionat)[aoie]\b",)),
    _L.FR: _rules("fr_reserved", (r"\br[ée]serv[ée]e?s?\b",)),
    _L.EN: _rules("en_reserved", (r"\breserved\b", r"\bon hold\b", r"\bunder offer\b")),
}
_AV_AVAILABLE: Final[dict[MessageLanguage, tuple[_Rule, ...]]] = {
    _L.DE: _rules(
        "de_available",
        (
            r"\bnoch (?:verf[üu]gbar|erh[äa]ltlich|vorhanden|da|zu haben|zu verkaufen|im angebot)\b",
            r"\b(?:ist|sind|bleibt) (?:weiterhin |aktuell |derzeit )?verf[üu]gbar\b",
            r"\bsteht noch zum verkauf\b",
        ),
    ),
    _L.IT: _rules(
        "it_available",
        (
            r"\bancora disponibile\b",
            r"\b(?:[èe]|e') disponibile\b",
            r"\bancora in vendita\b",
            r"\bc'[èe] ancora\b",
        ),
    ),
    _L.FR: _rules(
        "fr_available",
        (
            r"\b(?:toujours|encore) disponible\b",
            r"\best disponible\b",
            r"\btoujours (?:[àa] vendre|en vente)\b",
        ),
    ),
    _L.EN: _rules(
        "en_available",
        (r"\bstill (?:available|for sale|on sale)\b", r"\b(?:is|it's) available\b", r"\bstill have it\b"),
    ),
}

_PRICE_KEYWORDS: Final[dict[MessageLanguage, re.Pattern[str]]] = {
    _L.DE: re.compile(
        r"\b(?:preis\w*|vb|vhb|verhandlungsbasis|festpreis|letzt\w*|endpreis|kost\w*|gehe auf)\b"
    ),
    _L.IT: re.compile(r"\b(?:prezz\w*|costo|costa|trattabil\w*|ultimo|finale|minimo)\b"),
    _L.FR: re.compile(r"\b(?:prix|co[ûu]t\w*|dernier|n[ée]gociable|minimum)\b"),
    _L.EN: re.compile(r"\b(?:price\w*|cost\w*|lowest|final|asking|ono)\b"),
}
_DEPOSIT_WORDS: Final = re.compile(
    # "anzahlen"/"Anzahlung" (deposit) but not the noun "Anzahl" (number of owners, keys, ...)
    r"\b(?:anzahl(?:ung\w*|en|t)|angeld|kaution|reservierungsgeb[üu]hr|vorkasse|vorauszahl\w*|[üu]berweis\w*"
    r"|restbetrag|saldo|solde|caparra|acconto|anticipo|bonifico|versament\w*"
    r"|acompte|arrhes|virement|caution|deposit|down payment|advance payment|upfront|bank transfer"
    r"|wire transfer|paypal)\b"
)
_OTHER_COST_WORDS: Final = re.compile(
    r"\b(?:transport\w*|[üu]berf[üu]hrung|lieferung|versand|geb[üu]hr\w*|ummeldung|kennzeichen|zoll"
    r"|versicherung"
    r"|trasporto|consegna|spedizione|passaggio|voltura|targhe|assicurazione|livraison|frais|assurance"
    r"|shipping|delivery|fee|fees|plates|insurance|customs)\b"
)
_PREVIOUS_WORDS: Final = re.compile(
    r"\b(?:statt|anstatt|vorher|urspr[üu]nglich|bisher|invece di|anzich[ée]|prima|au lieu de|auparavant|avant"
    r"|initialement|instead of|previously|originally|was)\s*(?:€|eur\w*|chf)?\s*$"
)
_MINIMUM_BEFORE: Final = re.compile(
    r"\b(?:(?:nicht|non|pas|not)\s+(?:unter|weniger als|sotto|meno di|en[ -]dessous de|moins de|below|under"
    r"|less than)|mindestens|almeno|au moins|at least|minimum)\s*(?:€|eur\w*|chf)?\s*$"
)
_CONDITION_RULES: Final[tuple[tuple[PriceCondition, re.Pattern[str]], ...]] = (
    (
        PriceCondition.FIXED,
        re.compile(
            r"\b(?:festpreis|nicht verhandelbar|prezzo fisso|non trattabil[ei]|prix ferme|non n[ée]gociable"
            r"|fixed price|non-negotiable|not negotiable|firm price)\b"
        ),
    ),
    (
        PriceCondition.FINAL_OR_LOWEST,
        re.compile(
            r"\b(?:letzte[rn]? preis|letztpreis|endpreis|schmerzgrenze|niedrigste[rn]? preis|untergrenze"
            r"|ultimo prezzo|prezzo finale|prezzo minimo|dernier prix|prix final|prix le plus bas"
            r"|prix minimum"
            r"|final price|last price|lowest price|best price|rock bottom)\b"
        ),
    ),
    (
        PriceCondition.NO_DISCOUNT,
        re.compile(
            r"\b(?:keine?n? (?:rabatt|nachlass)|nessuno sconto|niente sconto|pas de remise|aucune remise"
            r"|no discount)\b"
        ),
    ),
    (
        PriceCondition.CASH_PAYMENT,
        re.compile(
            r"\b(?:barzahlung|bar bezahlen|in bar|nur bar|in contanti|contanti|en esp[èe]ces|comptant|cash)\b"
            r"|(?<!al )\bbar\b"
        ),
    ),
    (
        PriceCondition.EXPORT_ONLY,
        re.compile(r"\b(?:nur (?:f[üu]r )?export|solo (?:per )?export|export uniquement|export only)\b"),
    ),
    (
        PriceCondition.CONDITIONAL,
        re.compile(
            r"\b(?:bei (?:sofortiger|schneller|zeitnaher) |wenn sie (?:heute|sofort)|se (?:acquista"
            r"|compra) subito"
            r"|si vous (?:achetez|le prenez)|if you (?:buy|take)|for a quick sale)"
        ),
    ),
    (
        PriceCondition.TIME_LIMITED,
        re.compile(
            r"\b(?:nur (?:noch )?heute|bis morgen|nur diese woche|solo oggi|entro domani"
            r"|(?:seulement|uniquement) aujourd'hui|aujourd'hui seulement|today only|until tomorrow"
            r"|this week only)\b"
        ),
    ),
)
_NEGOTIABLE_LOWER: Final = re.compile(
    r"\b(?:verhandlungsbasis|verhandelbar|trattabil[ei]|negoziabile|[àa] d[ée]battre|n[ée]gociable|negotiable"
    r"|o\.?n\.?o)\b"
)
_NEGOTIABLE_CASE: Final = re.compile(r"\b(?:VB|VHB|ONO)\b")
_NET_WORDS: Final = re.compile(
    r"(?:\bnetto\b|\bnet\b|\bzzgl\.?\s*(?:\d{1,2}\s*%\s*)?mwst|\bplus mwst|\bohne mwst|\bexkl\.?\s*mwst"
    r"|\+\s*iva\b"
    r"|\bpi[ùu] iva\b|\biva esclusa\b|\boltre iva\b|\bhors tax\w*|\bhors tva\b|\bexcl\.?\s*vat\b|\bplus vat\b"
    r"|\bex vat\b|\+\s*vat\b)"
)
_GROSS_WORDS: Final = re.compile(
    r"(?:\bbrutto\b|\binkl\.?\s*(?:\d{1,2}\s*%\s*)?mwst|\bincl\.?\s*vat\b|\bincluding vat\b|\biva inclusa\b"
    r"|\biva compresa\b|\bttc\b|\btva (?:incluse|comprise)\b|\bgross\b|\bmwst\.?\s*ausweisbar\b)"
)

_AMOUNT_TOKEN_RE: Final = re.compile(
    r"(?<![\w.,'])(\d{1,3}(?:[.,'\xa0\N{NARROW NO-BREAK SPACE} ]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)(?:[.,][-\N{EN DASH}])?"
)
_CURRENCY_BEFORE_RE: Final = re.compile(
    r"(€|\$|£|\b(?:eur|euros?|chf|sfr|usd|gbp)\b\.?|\bfr\.)\s*$", re.IGNORECASE
)
_CURRENCY_AFTER_RE: Final = re.compile(
    r"^\s*(?:[.,][-\N{EN DASH}])?\s*(€|\$|£|(?:eur|euros?|chf|sfr|franken|francs?|usd|gbp)\b|fr\.)",
    re.IGNORECASE,
)
_UNIT_AFTER_RE: Final = re.compile(
    r"^\s*(?:km\b|kms\b|kilomet\w*|chilometri\b|kw\b|ps\b|cv\b|hp\b|ccm\b|cm3\b|cm³|%|g/km|l/100|liter\w*"
    r"|litri\b"
    r"|jahre?\b|anni\b|ans\b|years?\b|monate?\b|mesi\b|mois\b|months?\b|tage?\b|giorni\b|jours?\b|days?\b"
    r"|stunden\b|ore\b|heures?\b|hours?\b|uhr\b|h\b|min\b|tkm\b|tsd\b|mila\b|zoll\b"
    # counted things: owners, doors, seats, keys, gears, cylinders
    r"|(?:vor)?besitzer\b|halter(?:n|in)?\b|t[üu]ren\b|sitze\b|sitzpl[äa]tze\b|schl[üu]ssel\b|g[äa]nge\b"
    r"|zylinder\b|proprietari\b|porte\b|posti\b|chiavi\b|marce\b|cilindri\b|propri[ée]taires\b"
    r"|portes\b|places\b|cl[ée]s\b|vitesses\b|cylindres\b|owners?\b|doors\b|seats\b|keys\b|gears\b"
    r"|cylinders\b|x\b)",
    re.IGNORECASE,
)
# A bare number (no currency) right after a non-price label is a mileage, year, power, count or
# reference - never money, even when the sentence also states a price.
_NON_PRICE_LABEL_BEFORE_RE: Final = re.compile(
    r"\b(?:kilometerstand|km-stand|kilometer|laufleistung|tachostand|tacho|baujahr|bj|ez|erstzulassung"
    r"|modelljahr|hubraum|leistung|vorbesitzer|halter|besitzer|plz|nummer|chilometri|chilometraggio"
    r"|percorrenza|anno|immatricolazione|cilindrata|potenza|proprietari|cap|numero|kilom[ée]trage"
    r"|ann[ée]e|mise en circulation|cylindr[ée]e|puissance|propri[ée]taires|num[ée]ro|mileage|odometer"
    r"|miles|year|registered|registration|engine|power|owners|km|kms|vin|fin|telai?o)\b"
    r"[ \t:=.-]{0,4}(?:(?:of|di|de|du|von|ca|circa|about|approx|ungef[äa]hr|etwa|environ|ist|is|è|est|ha|hat"
    r"|has)\.?[ \t:=-]{1,3}){0,2}$"
)
_RANGE_JOINER_RE: Final = re.compile(
    r"^\s*(?:€|eur\w*|chf)?\s*(?P<joiner>-|\N{EN DASH}|\N{EM DASH}|bis|to|a|à|und|e|et|and)\s*$",
    re.IGNORECASE,
)
_RANGE_INTRO_RE: Final = re.compile(
    r"\b(?:zwischen|tra|fra|entre|between|von|da|de|from)\s*(?:€|eur\w*|chf)?\s*$"
)
_CURRENCY_CODES: Final[dict[str, str]] = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "chf": "CHF",
    "sfr": "CHF",
    "fr.": "CHF",
    "franken": "CHF",
    "franc": "CHF",
    "francs": "CHF",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
}
_NUMBER_LOCALE: Final[dict[MessageLanguage, Locale]] = {_L.DE: "de", _L.IT: "it", _L.FR: "de", _L.EN: "en"}

_DOC_KIND_RULES: Final[tuple[tuple[DocumentKind, re.Pattern[str]], ...]] = (
    (
        DocumentKind.SERVICE_HISTORY,
        re.compile(
            r"\b(?:scheckheft|serviceheft|wartungsheft|servicenachweis\w*|libretto (?:dei )?tagliandi"
            r"|tagliandi"
            r"|carnet d'entretien|historique d'entretien|service ?book|service history|service records)\b"
        ),
    ),
    (
        DocumentKind.COC,
        re.compile(
            r"(?:\bc\.?o\.?c\b|\bcertificate of conformity\b|\bkonformit[äa]tsbescheinigung\b"
            r"|\b[üu]bereinstimmungsbescheinigung\b|\bcertificato di conformit[àa]\b"
            r"|\bcertificat de conformit[ée]\b)"
        ),
    ),
    (
        DocumentKind.REGISTRATION,
        re.compile(
            r"\b(?:fahrzeugschein|fahrzeugbrief|zulassungsbescheinigung\w*|zulassungsunterlagen"
            r"|zulassungspapiere"
            r"|kfz-brief|kfz-schein|libretto di circolazione|carta di circolazione|libretto"
            r"|certificato di propriet[àa]"
            r"|carte grise|certificat d'immatriculation|registration (?:document|papers?|certificate)s?"
            r"|logbook|v5c)\b"
        ),
    ),
    (
        DocumentKind.INSPECTION,
        re.compile(r"\b(?:t[üu]v|hu|hauptuntersuchung|revisione|contr[ôo]le technique|mot)\b"),
    ),
    (
        DocumentKind.GENERAL,
        re.compile(r"\b(?:unterlagen|papiere|dokumente|documenti|documents?|papiers|papers|paperwork)\b"),
    ),
)
_DOC_STATUS_RULES: Final[tuple[tuple[DocumentClaimStatus, re.Pattern[str]], ...]] = (
    (
        DocumentClaimStatus.REFUSED,
        re.compile(
            r"(?:\b(?:schicke|sende|versende|verschicke|gebe) (?:ich )?keine\b|\bnur (?:vor ort"
            r"|bei (?:der )?besichtigung)"
            r"|\bnicht per (?:e-?)?mail\b|\bnon (?:li )?(?:invio|mando|spedisco)\b|\bsolo (?:in sede"
            r"|di persona"
            r"|alla visione|in visione)\b|\bnon via (?:e-?)?mail\b|\bje n'envoie pas\b"
            r"|\bpas par (?:e-?)?mail\b"
            r"|\b(?:uniquement|seulement) sur place\b|\bsur place uniquement\b|\bwon't send\b"
            r"|\bwill not send\b"
            r"|\bdo not send\b|\bdon't send\b|\bonly (?:on site|in person|at (?:the )?viewing)\b"
            r"|\bnot by e-?mail\b)"
        ),
    ),
    (
        DocumentClaimStatus.NOT_AVAILABLE,
        re.compile(
            r"(?:\bkeine?n?\b|\bnicht vorhanden\b|\bfehl(?:t|en)\b|\bnon c'[èe]\b|\bnon disponibil[ei]\b"
            r"|\bmanca(?:no)?\b"
            r"|\bsenza\b|\bpas de\b|\bpas disponibles?\b|\bmanque\b|\bsans\b|\bno\b|\bnot available\b"
            r"|\bmissing\b"
            r"|\bwithout\b|\bdon't have\b|\bdo not have\b|\bnicht\b|\bnon\b|\bpas\b|\bnot\b)"
        ),
    ),
    (
        DocumentClaimStatus.ATTACHED,
        re.compile(
            r"(?:\banbei\b|\bim anhang\b|\bangeh[äa]ngt\b|\bbeigef[üu]gt\b|\bals anhang\b|\bin allegato\b"
            r"|\ballego\b"
            r"|\ballegat[oaie]\b|\bci-joints?\b|\ben pi[èe]ce jointe\b|\ben pj\b|\bje vous joins\b"
            r"|\battached\b"
            r"|\benclosed\b|\battaching\b|\bplease find\b)"
        ),
    ),
    (
        DocumentClaimStatus.AVAILABLE,
        re.compile(
            r"(?:\bvorhanden\b|\bliegt vor\b|\bliegen vor\b|\bist dabei\b|\bsind dabei\b|\bhaben wir\b"
            r"|\bhabe ich\b|\bhab ich\b|\bist da\b|\bsind da\b|\bliegt bei\b|\bliegen bei\b"
            r"|\bce l'ho\b|\bce l'abbiamo\b|\bje l'ai\b|\bi've got\b|\bwe've got\b"
            r"|\bverf[üu]gbar\b|\bkann ich (?:ihnen )?(?:zu)?(?:schicken|senden)\b|\bschicke ich\b"
            r"|\bsende ich\b"
            r"|\bdisponibil[ei]\b|\bc'[èe]\b|\bci sono\b|\bpresent[ei]\b|\babbiamo\b|\bposso inviar\w*"
            r"|\binvier[òo]\b"
            r"|\bdisponibles?\b|\bj'ai\b|\bnous avons\b|\bfournie?s?\b|\bje (?:peux|vous) (?:les )?envoy\w*"
            r"|\bj'enverrai\b|\bavailable\b|\bi have\b|\bwe have\b|\bincluded\b|\bcomes with\b|\bcan send\b"
            r"|\bwill send\b|\bi'll send\b)"
        ),
    ),
)
_REQUEST_RULES: Final[dict[RequestKind, re.Pattern[str]]] = {
    RequestKind.PAYMENT: re.compile(
        r"\b(?:anzahl(?:ung|en|t)|angeld|[üu]berweisung|[üu]berweisen|vorkasse|vorauszahlung|kaution|iban"
        r"|paypal|western union|bezahlen|caparra|acconto|bonifico|versamento|pagamento|pagare|ricarica"
        r"|acompte|arrhes|virement|paiement|payer|mandat cash|deposit|down payment|advance payment"
        r"|pay upfront|pay in advance|bank transfer|wire transfer|payment|escrow)\b"
    ),
    RequestKind.RESERVATION: re.compile(
        r"\b(?:reservieren|reservierung|zur[üu]cklegen|zur[üu]ckhalten|prenotare|prenotarl[ao]|prenotazione"
        r"|riservare|riservarl[ao]|tenerl[ao] da parte|r[ée]server|r[ée]servation|mettre de c[ôo]t[ée]"
        r"|reserve"
        r"|reservation|hold it for you|put it aside|keep it for you)\b"
    ),
    RequestKind.IDENTITY_DOCUMENT: re.compile(
        r"\b(?:personalausweis\w*|ausweis(?!bar)\w*|reisepass\w*|passkopie|f[üu]hrerschein\w*"
        r"|identit[äa]tsnachweis"
        r"|carta d'identit[àa]|documento (?:d'|di )identit[àa]|passaporto|patente|codice fiscale"
        r"|carte d'identit[ée]|pi[èe]ce d'identit[ée]|passeport|permis de conduire|passport|id card"
        r"|identity (?:card|document)|driver'?s licen[cs]e|driving licen[cs]e|proof of identity)\b"
    ),
    RequestKind.APPOINTMENT: re.compile(
        r"\b(?:besichtigung|besichtigen|termin\w*|probefahrt|vorbeikommen|vorbeischauen|anschauen"
        r"|appuntamento"
        r"|visionare|vederla|venire a vedere|prova su strada|giro di prova|rendez-vous|rdv"
        r"|visite(?! technique)|venir (?:la |le )?voir"
        r"|essai|essayer|viewing|view it|appointment|test drive|come and see|come by)\b"
    ),
    RequestKind.COMMITMENT: re.compile(
        r"\b(?:kaufvertrag|verbindlich\w*|kaufzusage|zusage|kaufentscheidung|wollen sie (?:es|ihn"
        r"|das auto) kaufen"
        r"|contratto|impegno|vincolant[ei]|confermare l'acquisto|conferma l'acquisto|contrat|engagement"
        r"|bon de commande|confirmer l'achat|contract|binding|commitment|confirm the purchase"
        r"|do you want to buy)\b"
    ),
    RequestKind.PRICE_ACCEPTANCE: re.compile(
        r"(?:\beinverstanden\b|\bakzeptieren sie\b|\bpasst (?:ihnen )?das\b|\bdeal\s*\?|\baccetta\b"
        r"|\ble va bene\b"
        r"|\bacceptez\b|\b[çc]a vous va\b|\bdo you accept\b|\bis that ok\b|\bagreed\s*\?)"
    ),
    RequestKind.OPT_OUT: re.compile(
        r"(?:\bkeine (?:weiteren )?(?:anfragen|nachrichten|e-?mails|mails)\b|\bnicht mehr (?:kontaktieren"
        r"|anschreiben"
        r"|schreiben)\b|\bkontaktieren sie mich nicht\b|\babmelden\b|\bnon (?:mi )?(?:contatt"
        r"|scriv)\w*(?: pi[ùu])?\b"
        r"|\bnon contattatemi\b|\bcancellatemi\b|\bne (?:me )?contactez plus\b|\bne plus me contacter\b"
        r"|\bne m'[ée]crivez plus\b|\bd[ée]sinscri\w*|\bdo not contact\b|\bdon't contact\b"
        r"|\bstop (?:contacting"
        r"|emailing|writing)\b|\bunsubscribe\b|\bremove me\b|\bno more (?:emails|messages)\b)"
    ),
    RequestKind.COMPLAINT: re.compile(r"\b(?:spam|bel[äa]stig\w*|molest\w*|harc[èe]l\w*|harass\w*|abuse)\b"),
}
_REQUEST_NEGATION_EXEMPT: Final = frozenset(
    {RequestKind.OPT_OUT, RequestKind.COMPLAINT, RequestKind.PRICE_ACCEPTANCE}
)


def _languages(language: MessageLanguage | None) -> tuple[MessageLanguage, ...]:
    return (language,) if language is not None else tuple(MessageLanguage)


# --- analysis region ---------------------------------------------------------------------------

# Sign-off lines after which a signature, company footer or legal disclaimer follows. Stricter
# than the sanitiser's list: "Thanks!" or "Cheers" can open a message, these do not.
_SIGN_OFF_RE: Final = re.compile(
    r"^\s*(?:mit (?:freundlichen|besten|herzlichen|lieben) gr(?:ü|ue|u)(?:ß|ss)en"
    r"|(?:freundliche|beste|viele|liebe|herzliche) gr(?:ü|ue)(?:ß|ss)e|mfg|lg|gru(?:ß|ss)"
    r"|cordiali saluti|distinti saluti|saluti|cordialmente|un (?:caro )?saluto|cordialement"
    r"|bien cordialement|bien [àa] vous|salutations(?: distingu[ée]es)?|kind regards|best regards"
    r"|warm regards|regards|best wishes|yours sincerely|sincerely)\b[\s,.!]*$",
    re.IGNORECASE,
)
_POSTSCRIPT_RE: Final = re.compile(r"^\s*(?:p\.?\s?s\.?|n\.?\s?b\.?)\s*[:.)-]?\s", re.IGNORECASE)
# Boilerplate that contains availability words without being an availability statement:
# copyright lines, confidentiality disclaimers ("informazioni riservate", "réservé à l'usage"),
# "subject to prior sale" wording and French "sous réserve".
_BOILERPLATE_RE: Final = re.compile(
    r"\ball rights reserved\b|\balle rechte vorbehalten\b|\btutti i diritti (?:sono )?riservati\b"
    r"|\btous droits r[ée]serv[ée]s\b"
    r"|\b(?:informazion\w*|contenut\w*|dati|messaggio|comunicazion\w*|documento|carattere|natura|uso)"
    r"(?:\s+[\w']+){0,4}?\s+(?:riservat\w*|confidenzial\w*)(?:\s+e\s+(?:riservat\w*|confidenzial\w*))?"
    r"|\b(?:riservat\w*|confidenzial\w*)\s+e\s+(?:riservat\w*|confidenzial\w*)"
    r"|\br[ée]serv[ée]e?s?\s+[àa]\s+l'usage\b|\busage\s+(?:\w+\s+)?r[ée]serv[ée]e?s?\b"
    r"|\bsous r[ée]serve\b|\br[ée]serve de propri[ée]t[ée]\b"
    r"|\bsalvo (?:il )?vendut[oa]\b|\bsauf vente\b|\bunless (?:already |previously )?sold\b"
    r"|\bsubject to (?:prior )?sale\b|\bzwischenverkauf vorbehalten\b"
    r"|\b(?:sofern|falls|wenn) (?:nicht )?(?:zwischenzeitlich |bereits |schon )?verkauft\b"
)


def _blank_ranges(text: str, ranges: Iterable[tuple[int, int]]) -> str:
    """``text`` with every ``[start, end)`` range replaced by spaces; newlines and therefore all
    indices stay aligned with the input."""
    chars = list(text)
    for start, end in ranges:
        for position in range(max(0, start), min(len(chars), end)):
            if chars[position] != "\n":
                chars[position] = " "
    return "".join(chars)


def _claims_region(text: str) -> str:
    """Same-length copy of ``text`` with the signature/footer/disclaimer region blanked.

    Everything after the first sign-off line ("Mit freundlichen Grüßen", "Cordiali saluti",
    "Kind regards", a ``-- `` delimiter) that follows some message text is the signature,
    company footer or legal disclaimer - bank details, "All rights reserved", "informazioni
    riservate" - and never a claim; postscript paragraphs ("PS: ...") after it are kept.
    Known boilerplate phrases are blanked wherever they occur.
    """
    ranges: list[tuple[int, int]] = []
    position = 0
    seen_text = False
    signature = False
    in_postscript = False
    for line in text.split("\n"):
        start, end = position, position + len(line)
        position = end + 1
        if not signature:
            if seen_text and (_SIGNATURE_DELIMITER_RE.match(line) or _SIGN_OFF_RE.match(line)):
                signature = True
            elif line.strip():
                seen_text = True
        if signature:
            if _POSTSCRIPT_RE.match(line):
                in_postscript = True
            elif not line.strip():
                in_postscript = False
            if not in_postscript:
                ranges.append((start, end))
    ranges.extend(m.span() for m in _BOILERPLATE_RE.finditer(_lower_same_length(text)))
    return _blank_ranges(text, ranges) if ranges else text


def _tokens_before(lowered: str, clause_start: int, position: int, count: int = 3) -> list[str]:
    window = lowered[clause_start:position]
    return re.findall(r"[\w']+", window)[-count:]


def _negated(
    lowered: str, clause_start: int, position: int, langs: Sequence[MessageLanguage], count: int = 3
) -> bool:
    tokens = _tokens_before(lowered, clause_start, position, count)
    negations = set().union(*(_NEGATIONS[lang] for lang in langs))
    return any(t in negations or t.endswith("n't") or t.startswith("n'") for t in tokens)


def _availability_negated(lowered: str, clause_start: int, position: int, lang: MessageLanguage) -> bool:
    """Negation before an availability word. A clause-initial English "no" followed by more words
    is an answer interjection ("No it's sold"), not a negation of the word."""
    tokens = re.findall(r"[\w']+", lowered[clause_start:position])
    if len(tokens) > 1 and tokens[0] == "no":
        tokens = tokens[1:]
    negations = _NEGATIONS[lang]
    return any(t in negations or t.endswith("n't") or t.startswith("n'") for t in tokens[-3:])


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _conditional(clause_text: str, langs: Sequence[MessageLanguage]) -> bool:
    return any(_CONDITIONALS[lang].search(clause_text) for lang in langs)


def _availability_claims(
    text: str, lowered: str, spans: Sequence[_Span], langs: Sequence[MessageLanguage], declared: bool
) -> list[AvailabilityClaim]:
    claims: list[AvailabilityClaim] = []
    confidence: Literal["high", "medium", "low"] = "high" if declared else "medium"
    work = lowered
    for span in spans:
        if span.question:
            continue
        for c_start, c_end in _clauses(lowered, span):
            if _conditional(lowered[c_start:c_end], langs):
                continue
            for lang in langs:
                ordered: tuple[
                    tuple[dict[MessageLanguage, tuple[_Rule, ...]], AvailabilityClaimStatus, bool], ...
                ] = (
                    (_AV_NOT_AVAILABLE, AvailabilityClaimStatus.NOT_AVAILABLE, False),
                    (_AV_NOT_YET_SOLD, AvailabilityClaimStatus.AVAILABLE, False),
                    (_AV_SOLD, AvailabilityClaimStatus.SOLD, True),
                    (_AV_RESERVED, AvailabilityClaimStatus.RESERVED, True),
                    (_AV_AVAILABLE, AvailabilityClaimStatus.AVAILABLE, True),
                )
                for table, status, check_negation in ordered:
                    for rule in table[lang]:
                        for match in rule.pattern.finditer(work, c_start, c_end):
                            start, end = match.start(), match.end()
                            work = _mask(work, start, end)
                            if check_negation and _availability_negated(lowered, c_start, start, lang):
                                continue
                            if status == AvailabilityClaimStatus.SOLD and any(
                                t in _PASSIVE_SALE_BEFORE[lang]
                                for t in _tokens_before(lowered, c_start, start, 4)
                            ):
                                continue  # "wird verkauft" / "will be sold": for sale, not sold
                            claims.append(
                                AvailabilityClaim(
                                    status=status,
                                    language=lang,
                                    evidence=EvidenceSpan(
                                        start=start,
                                        end=end,
                                        excerpt=_excerpt(text, span.start, span.end),
                                        rule=rule.code,
                                    ),
                                    confidence=confidence,
                                    implied=table is _AV_NOT_YET_SOLD,
                                )
                            )
    return claims


@dataclass(frozen=True, slots=True)
class _Amount:
    start: int
    end: int
    raw: str
    value: Decimal | None
    currency: str | None
    warnings: tuple[str, ...]
    standalone: bool  # has a currency, or a price keyword in the sentence (and is not a year)
    year_like: bool = False


def _currency_code(marker: str) -> str | None:
    key = marker.strip().lower()
    return _CURRENCY_CODES.get(key) or _CURRENCY_CODES.get(key.rstrip("."))


_MIN_BARE_AMOUNT: Final = Decimal(100)  # a currency-less number below this is never a vehicle price


def _amounts_in(text: str, lowered: str, span: _Span, language: MessageLanguage | None) -> list[_Amount]:
    """Number tokens of one sentence that can be money (units, years, codes and dates excluded)."""
    found: list[_Amount] = []
    has_keyword = any(
        _PRICE_KEYWORDS[lang].search(lowered, span.start, span.end) for lang in _languages(language)
    )
    for match in _AMOUNT_TOKEN_RE.finditer(text, span.start, span.end):
        start, end = match.start(), match.end()
        raw = match.group(0)
        after = text[end : min(span.end, end + 12)]
        before = text[max(span.start, start - 8) : start]
        cur_after = _CURRENCY_AFTER_RE.match(after)
        cur_before = _CURRENCY_BEFORE_RE.search(before)
        currency = _currency_code(cur_after.group(1)) if cur_after else None
        if currency is None and cur_before:
            currency = _currency_code(cur_before.group(1))
        if re.match(r"[^\W\d_]", after) and not cur_after:
            continue  # glued to letters: "4x4", "6d", "X5"
        if re.search(r"[^\W\d_]$", before) and not cur_before:
            continue
        if _UNIT_AFTER_RE.match(after) and not cur_after:
            continue
        if before.rstrip().endswith(("§", "#", "+", "/")) or after.startswith(("/", ":")):
            continue  # statute numbers, dates, times, phone fragments
        if re.search(r"(?i)\beuro\s*$", before) and len(raw) == 1:
            continue  # "Euro 5" emissions class, not an amount
        if currency is None and _NON_PRICE_LABEL_BEFORE_RE.search(
            lowered, max(span.start, start - 40), start
        ):
            continue  # "Kilometerstand 150000", "Baujahr: 2012", "mileage of 145000" are not money
        year_like = currency is None and raw.isdigit() and len(raw) == 4 and 1950 <= int(raw) <= 2100
        locale: Locale | None = _NUMBER_LOCALE.get(language) if language else None
        if currency == "CHF":
            locale = "ch"
        parsed = parse_number(raw, locale)
        warnings = tuple(str(w) for w in parsed.warnings)
        value = parsed.value if parsed.value is not None and parsed.value > 0 else None
        if parsed.value is not None and parsed.value <= 0:
            warnings = (*warnings, "ZERO_AMOUNT")
        if currency is None:
            warnings = (*warnings, "CURRENCY_NOT_STATED")
        # Without a currency, only a plausible vehicle amount next to a price word is a quote.
        plausible = value is None or value >= _MIN_BARE_AMOUNT
        standalone = currency is not None or (has_keyword and not year_like and plausible)
        found.append(_Amount(start, end, raw, value, currency, warnings, standalone, year_like))
    return found


_INCLUDED_AFTER_RE: Final = re.compile(
    r"\b(?:inkl|incl|inclus\w*|compres\w*|including|included|mit|con|avec|with)\b"
)


def _context(lowered: str, span: _Span, amount: _Amount, prev_end: int, next_start: int) -> AmountContext:
    """Nearest context of one amount: words before it (back to the previous amount) and words
    after it up to the next amount or clause punctuation, unless they say "included"."""
    before = lowered[max(span.start, prev_end, amount.start - 40) : amount.start]
    after = lowered[amount.end : min(span.end, next_start, amount.end + 25)]
    after = re.split(r"[,;:]", after, maxsplit=1)[0]
    if _INCLUDED_AFTER_RE.search(after):
        after = ""
    if _DEPOSIT_WORDS.search(before) or _DEPOSIT_WORDS.search(after):
        return AmountContext.DEPOSIT_OR_PAYMENT
    if _OTHER_COST_WORDS.search(before) or _OTHER_COST_WORDS.search(after):
        return AmountContext.OTHER_COST
    if _PREVIOUS_WORDS.search(lowered[max(span.start, prev_end, amount.start - 25) : amount.start]):
        return AmountContext.PREVIOUS_PRICE
    return AmountContext.PRICE


def _sentence_conditions(
    text: str, lowered: str, span: _Span
) -> tuple[tuple[PriceCondition, ...], PriceBasis, list[str]]:
    work = lowered[span.start : span.end]
    conditions: list[PriceCondition] = []
    for condition, pattern in _CONDITION_RULES:
        if pattern.search(work):
            conditions.append(condition)
            work = pattern.sub(lambda m: " " * len(m.group(0)), work)
    if _NEGOTIABLE_LOWER.search(work) or _NEGOTIABLE_CASE.search(text[span.start : span.end]):
        conditions.append(PriceCondition.NEGOTIABLE)
    warnings: list[str] = []
    if PriceCondition.NEGOTIABLE in conditions and (
        PriceCondition.FIXED in conditions or PriceCondition.FINAL_OR_LOWEST in conditions
    ):
        warnings.append("CONFLICTING_PRICE_CONDITIONS")
    segment = lowered[span.start : span.end]
    net, gross = bool(_NET_WORDS.search(segment)), bool(_GROSS_WORDS.search(segment))
    if net and gross:
        basis = PriceBasis.UNKNOWN
        warnings.append("CONFLICTING_PRICE_BASIS")
    elif net:
        basis = PriceBasis.NET
    elif gross:
        basis = PriceBasis.GROSS
    else:
        basis = PriceBasis.UNKNOWN
    return tuple(conditions), basis, warnings


def _range_with_next(lowered: str, span: _Span, amount: _Amount, nxt: _Amount | None) -> bool:
    """``2.500 - 2.700 €``, ``2.500 bis 2.700``, ``zwischen 2.500 und 2.700 €``, ``tra 2.500 e
    2.700``, ``entre 2 500 et 2 700``, ``between 2,500 and 2,700``."""
    if nxt is None or not (nxt.standalone or amount.standalone):
        return False
    joiner = _RANGE_JOINER_RE.match(lowered[amount.end : nxt.start])
    if joiner is None:
        return False
    if joiner.group("joiner").lower() in {"und", "e", "et", "and", "a", "à"}:
        return bool(_RANGE_INTRO_RE.search(lowered[max(span.start, amount.start - 15) : amount.start]))
    return True


def _price_claims(
    text: str,
    lowered: str,
    spans: Sequence[_Span],
    language: MessageLanguage | None,
    quoted_at: datetime | None,
) -> tuple[list[PriceQuoteClaim], list[AmountMention], list[str]]:
    prices: list[PriceQuoteClaim] = []
    mentions: list[AmountMention] = []
    warnings: list[str] = []
    for span in spans:
        if span.question:
            continue
        amounts = _amounts_in(text, lowered, span, language)
        if not any(a.standalone for a in amounts):
            continue
        conditions, basis, cond_warnings = _sentence_conditions(text, lowered, span)
        if PriceCondition.CONDITIONAL not in conditions and _conditional(
            lowered[span.start : span.end], _languages(language)
        ):
            # "2.500 €, wenn Sie es diese Woche abholen" / "if you pick it up": the quote keeps
            # its qualification instead of reading as an unconditional price.
            conditions = (*conditions, PriceCondition.CONDITIONAL)
        has_keyword = any(
            _PRICE_KEYWORDS[lang].search(lowered, span.start, span.end) for lang in _languages(language)
        )
        excerpt = _excerpt(text, span.start, span.end)
        used: set[int] = set()
        for index, amount in enumerate(amounts):
            if index in used:
                continue
            nxt = amounts[index + 1] if index + 1 < len(amounts) else None
            prev_end = amounts[index - 1].end if index > 0 else span.start
            next_start = nxt.start if nxt is not None else span.end
            if _range_with_next(lowered, span, amount, nxt) and nxt is not None:
                used.add(index + 1)
                context = _context(lowered, span, amount, prev_end, nxt.start)
                currency = amount.currency or nxt.currency
                consistent = amount.currency in (None, nxt.currency) or nxt.currency is None
                if (
                    context in (AmountContext.PRICE, AmountContext.UNLABELLED)
                    and amount.value is not None
                    and nxt.value is not None
                    and amount.value < nxt.value
                    and consistent
                ):
                    prices.append(
                        PriceQuoteClaim(
                            kind="range",
                            low=amount.value,
                            high=nxt.value,
                            currency=currency,
                            basis=basis,
                            conditions=conditions,
                            quoted_at=quoted_at,
                            language=language,
                            evidence=EvidenceSpan(
                                start=amount.start, end=nxt.end, excerpt=excerpt, rule="amount_range"
                            ),
                            confidence="medium" if currency else "low",
                            warnings=tuple(
                                dict.fromkeys(
                                    w
                                    for w in (*cond_warnings, *amount.warnings, *nxt.warnings)
                                    if w != "CURRENCY_NOT_STATED" or currency is None
                                )
                            ),
                        )
                    )
                    continue
                for item in (amount, nxt):
                    mentions.append(
                        AmountMention(
                            context=context if context != AmountContext.PRICE else AmountContext.UNLABELLED,
                            amount=item.value,
                            currency=item.currency,
                            raw=item.raw[:60],
                            evidence=EvidenceSpan(
                                start=item.start, end=item.end, excerpt=excerpt, rule="amount_range_unparsed"
                            ),
                            warnings=(*item.warnings, "RANGE_NOT_UNDERSTOOD"),
                        )
                    )
                continue
            if not amount.standalone:
                # A money-like number next to a stated price ("2.800 EUR, nicht unter 2.600") is
                # preserved for the owner as a mention - never a quote, never given a currency.
                if amount.value is not None and amount.value >= _MIN_BARE_AMOUNT and not amount.year_like:
                    bare_context = _context(lowered, span, amount, prev_end, next_start)
                    mentions.append(
                        AmountMention(
                            context=AmountContext.UNLABELLED
                            if bare_context == AmountContext.PRICE
                            else bare_context,
                            amount=amount.value,
                            currency=None,
                            raw=amount.raw[:60],
                            evidence=EvidenceSpan(
                                start=amount.start, end=amount.end, excerpt=excerpt, rule="amount_bare"
                            ),
                            warnings=(*amount.warnings, "AMOUNT_WITHOUT_PRICE_CONTEXT"),
                        )
                    )
                continue
            context = _context(lowered, span, amount, prev_end, next_start)
            evidence = EvidenceSpan(start=amount.start, end=amount.end, excerpt=excerpt, rule="amount")
            if context != AmountContext.PRICE or amount.value is None:
                mentions.append(
                    AmountMention(
                        context=AmountContext.UNLABELLED if context == AmountContext.PRICE else context,
                        amount=amount.value,
                        currency=amount.currency,
                        raw=amount.raw[:60],
                        evidence=evidence,
                        warnings=amount.warnings
                        if amount.value is not None
                        else (*amount.warnings, "AMOUNT_NOT_PARSED"),
                    )
                )
                continue
            minimum = bool(
                _MINIMUM_BEFORE.search(lowered[max(span.start, prev_end, amount.start - 30) : amount.start])
            )
            claim_warnings = list(dict.fromkeys((*cond_warnings, *amount.warnings)))
            if not has_keyword:
                claim_warnings.append("PRICE_CONTEXT_UNLABELLED")
            prices.append(
                PriceQuoteClaim(
                    kind="minimum" if minimum else "single",
                    amount=amount.value,
                    currency=amount.currency,
                    basis=basis,
                    conditions=conditions,
                    quoted_at=quoted_at,
                    language=language,
                    evidence=EvidenceSpan(
                        start=amount.start,
                        end=amount.end,
                        excerpt=excerpt,
                        rule="amount_minimum" if minimum else "amount",
                    ),
                    confidence="medium" if has_keyword and amount.currency else "low",
                    warnings=tuple(claim_warnings),
                )
            )
    if len(prices) > 1:
        warnings.append("MULTIPLE_PRICE_STATEMENTS")
    return prices, mentions, warnings


_ENUMERATION_WORDS: Final = frozenset(
    {
        # DE
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "und",
        "oder", "sowie", "auch", "als", "kopie", "original",
        # IT
        "il", "lo", "la", "i", "gli", "le", "l", "un", "una", "uno", "e", "ed", "o", "oppure", "anche",
        "copia", "originale",
        # FR
        "les", "d", "de", "du", "des", "une", "et", "ou", "aussi", "copie",
        # EN
        "the", "a", "an", "and", "or", "also", "too", "plus", "copy",
    }
)  # fmt: skip


def _document_claims(
    text: str,
    lowered: str,
    spans: Sequence[_Span],
    language: MessageLanguage | None,
    attachment_count: int,
) -> tuple[list[DocumentClaim], list[str]]:
    claims: list[DocumentClaim] = []
    warnings: list[str] = []
    for span in spans:
        if span.question:
            continue
        clauses = _clauses(lowered, span)
        rows: list[tuple[list[tuple[DocumentKind, int, int]], DocumentClaimStatus | None, bool]] = []
        for c_start, c_end in clauses:
            clause = lowered[c_start:c_end]
            if _conditional(clause, _languages(language)):
                rows.append(([], None, False))
                continue
            work = lowered
            kinds: list[tuple[DocumentKind, int, int]] = []
            for kind, pattern in _DOC_KIND_RULES:
                for match in pattern.finditer(work, c_start, c_end):
                    kinds.append((kind, match.start(), match.end()))
                    work = _mask(work, match.start(), match.end())
            status: DocumentClaimStatus | None = None
            for candidate, pattern in _DOC_STATUS_RULES:
                if pattern.search(work, c_start, c_end):
                    status = candidate
                    break
            # Only a bare list item ("den Fahrzeugschein, den CoC und ... habe ich") shares the
            # status of its neighbours; "Fahrzeugbrief ja, CoC nicht" must not inherit "nicht".
            rest = re.findall(r"[^\W\d_]+", work[c_start:c_end])
            enumeration_only = all(token in _ENUMERATION_WORDS for token in rest)
            rows.append((kinds, status, enumeration_only))
        for index, (kinds, status, enumeration_only) in enumerate(rows):
            if not kinds:
                continue
            effective = status
            if effective is None and enumeration_only:
                later = [s for _, s, _e in rows[index + 1 :] if s is not None]
                earlier = [s for _, s, _e in rows[:index] if s is not None]
                effective = later[0] if later else (earlier[-1] if earlier else None)
            if effective is None:
                effective = DocumentClaimStatus.MENTIONED
            seen: set[DocumentKind] = set()
            for kind, start, end in kinds:
                if kind in seen:
                    continue
                seen.add(kind)
                claim_warnings: tuple[str, ...] = ()
                if effective == DocumentClaimStatus.ATTACHED and attachment_count == 0:
                    claim_warnings = ("ATTACHMENT_CLAIMED_NOT_PRESENT",)
                claims.append(
                    DocumentClaim(
                        kind=kind,
                        status=effective,
                        language=language,
                        evidence=EvidenceSpan(
                            start=start,
                            end=end,
                            excerpt=_excerpt(text, span.start, span.end),
                            rule=f"doc_{kind.value}",
                        ),
                        warnings=claim_warnings,
                    )
                )
    if any(c.warnings for c in claims):
        warnings.append("ATTACHMENT_CLAIMED_NOT_PRESENT")
    return claims, warnings


def _request_flags(
    text: str, lowered: str, spans: Sequence[_Span], language: MessageLanguage | None
) -> list[RequestFlag]:
    flags: list[RequestFlag] = []
    langs = _languages(language)
    for span in spans:
        for kind, pattern in _REQUEST_RULES.items():
            for match in pattern.finditer(lowered, span.start, span.end):
                if kind not in _REQUEST_NEGATION_EXEMPT and _negated(
                    lowered, span.start, match.start(), langs, count=2
                ):
                    continue
                flags.append(
                    RequestFlag(
                        kind=kind,
                        language=language,
                        evidence=EvidenceSpan(
                            start=match.start(),
                            end=match.end(),
                            excerpt=_excerpt(text, span.start, span.end),
                            rule=f"request_{kind.value}",
                        ),
                    )
                )
                break  # one flag per kind and sentence
    return flags


def extract_reply_claims(
    text: str | None,
    language: MessageLanguage | str | None,
    *,
    quoted_at: datetime | None = None,
    attachment_count: int = 0,
) -> ReplyClaims:
    """Extract separate, conservative claim records from a seller reply (spec 37.7 step 3).

    ``text`` should be the sanitised body; any remaining clearly delimited quoted message is
    stripped again so that the system's own questions are never read as seller statements.
    ``language`` selects the DE/IT/FR/EN vocabulary; ``None`` (or an unsupported code) applies
    all four with lower confidence. ``quoted_at`` (the reply's received time) dates every quote.
    Price quotes are always ``unaccepted_seller_quote`` and never overwrite the advertised price.
    """
    lang: MessageLanguage | None
    if isinstance(language, MessageLanguage) or language is None:
        lang = language
    else:
        try:
            lang = MessageLanguage(str(language).strip().lower()[:2])
        except ValueError:
            lang = None
    normalized = _normalize_text(text)
    unquoted, _removed = strip_quoted_text(normalized)
    unquoted = _claims_region(unquoted[:MAX_BODY_BYTES])
    lowered = _lower_same_length(unquoted)
    spans = _sentences(lowered)
    quoted = None if quoted_at is None else _aware(quoted_at)
    langs = _languages(lang)
    availability = _availability_claims(unquoted, lowered, spans, langs, declared=lang is not None)
    prices, mentions, price_warnings = _price_claims(unquoted, lowered, spans, lang, quoted)
    documents, doc_warnings = _document_claims(unquoted, lowered, spans, lang, attachment_count)
    requests = _request_flags(unquoted, lowered, spans, lang)
    warnings = [*price_warnings, *doc_warnings]
    if lang is None:
        warnings.append("LANGUAGE_UNKNOWN_ALL_VOCABULARIES_APPLIED")
    if summarize_availability(availability) == "conflicting":
        warnings.append("CONFLICTING_AVAILABILITY_STATEMENTS")
    return ReplyClaims(
        language=lang,
        availability=tuple(availability),
        prices=tuple(prices),
        other_amounts=tuple(mentions),
        documents=tuple(documents),
        requests=tuple(requests),
        warnings=tuple(dict.fromkeys(warnings)),
        analyzed_chars=len(unquoted),
    )


# =============================================================================================
# Attachment policy
# =============================================================================================


class AttachmentAction(StrEnum):
    ALLOW_VEHICLE_DOCUMENT = "allow_vehicle_document"
    QUARANTINE_SENSITIVE = "quarantine_sensitive"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class _MimePolicy:
    extensions: frozenset[str]
    max_bytes: int


ATTACHMENT_POLICY: Final[dict[str, _MimePolicy]] = {
    "application/pdf": _MimePolicy(frozenset({".pdf"}), 20 * 1024 * 1024),
    "image/jpeg": _MimePolicy(frozenset({".jpg", ".jpeg", ".jpe"}), 15 * 1024 * 1024),
    "image/png": _MimePolicy(frozenset({".png"}), 15 * 1024 * 1024),
    "image/heic": _MimePolicy(frozenset({".heic", ".heif"}), 15 * 1024 * 1024),
    "image/heif": _MimePolicy(frozenset({".heic", ".heif"}), 15 * 1024 * 1024),
}
MAX_TOTAL_ATTACHMENT_BYTES: Final = 50 * 1024 * 1024
_DANGEROUS_EXTENSIONS: Final = frozenset(
    {
        ".exe", ".com", ".bat", ".cmd", ".scr", ".pif", ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".ps1",
        ".msi", ".msp", ".jar", ".hta", ".lnk", ".dll", ".sh", ".app", ".iso", ".img", ".zip", ".rar", ".7z",
        ".gz", ".tar", ".docm", ".xlsm", ".pptm", ".html", ".htm", ".svg", ".xml", ".url", ".reg",
    }
)  # fmt: skip
_IDENTITY_FILENAME_RE: Final = re.compile(
    r"(?:^|[^a-z])(?:personalausweis|ausweis|perso|reisepass|pass|passport|passaporto|passeport|identita"
    r"|identity|id|carta identita|carta d identita|carte identite|carte d identite|cni|fuhrerschein"
    r"|fuehrerschein|patente|permis|driving licen[cs]e|drivers? licen[cs]e|selfie|codice fiscale"
    r"|tessera sanitaria|steuer id)(?:[^a-z]|$)"
)
_FINANCIAL_FILENAME_RE: Final = re.compile(
    r"(?:^|[^a-z])(?:iban|kontoauszug|bank|estratto conto|releve|rib|bank statement|kreditkarte"
    r"|carta di credito|carte bancaire|credit card)(?:[^a-z]|$)"
)
_VEHICLE_DOC_FILENAME: Final[tuple[tuple[DocumentKind, re.Pattern[str]], ...]] = (
    (DocumentKind.COC, re.compile(r"(?:^|[^a-z])(?:coc|konformitat\w*|conformit\w*)(?:[^a-z]|$)")),
    (
        DocumentKind.REGISTRATION,
        re.compile(
            r"(?:^|[^a-z])(?:fahrzeugschein|fahrzeugbrief|zulassung\w*|zb ?[12]|libretto"
            r"|carta di circolazione"
            r"|carte grise|immatriculation|registration|v5c|logbook)(?:[^a-z]|$)"
        ),
    ),
    (
        DocumentKind.SERVICE_HISTORY,
        re.compile(r"(?:^|[^a-z])(?:scheckheft|serviceheft|service\w*|tagliand\w*|entretien)(?:[^a-z]|$)"),
    ),
    (
        DocumentKind.INSPECTION,
        re.compile(r"(?:^|[^a-z])(?:tuv|hu|revisione|controle technique|mot)(?:[^a-z]|$)"),
    ),
)


class AttachmentDecision(BaseModel):
    """Policy decision for one attachment. Attachments are never forwarded to Slack, a model
    provider or any other external service (``forward_externally`` is always ``False``)."""

    model_config = _FROZEN

    index: int = Field(ge=0)
    action: AttachmentAction
    reasons: tuple[str, ...]
    document_kind: DocumentKind | None = None
    forward_externally: Literal[False] = False
    malware_scan_required: bool = True
    redaction_check_required: bool = False
    safe_metadata: dict[str, Any] | None = None


def _filename_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only).strip()


def evaluate_attachments(
    attachments: Sequence[AttachmentMeta], *, body_text: str | None = None
) -> tuple[AttachmentDecision, ...]:
    """Attachment policy (spec 37.7 step 4): permitted vehicle documents only.

    Allowed: PDF (<= 20 MiB) and JPEG/PNG/HEIC/HEIF images (<= 15 MiB) whose extension matches
    the MIME type, at most 20 attachments and 50 MiB in total. Executables, archives, macro
    documents, HTML/SVG, double extensions, empty files and anything else are rejected.
    Identity documents (passport/ID card/driving licence keywords in DE/IT/FR/EN file names, or a
    body mentioning an identity document next to an attachment that is not recognisably a
    vehicle document) and financial documents are quarantined, never uploaded or forwarded; their
    metadata reports presence only ("withheld"). Registration documents carry personal data and
    need a redaction check. Every allowed file still needs malware-aware inspection.
    """
    body_lower = _lower_same_length(_normalize_text(body_text))
    identity_in_body = bool(_REQUEST_RULES[RequestKind.IDENTITY_DOCUMENT].search(body_lower))
    decisions: list[AttachmentDecision] = []
    total = 0
    for index, meta in enumerate(attachments):
        name = meta.filename
        key = _filename_key(name)
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        inner_exts = {"." + part.lower() for part in name.split(".")[1:-1]}
        reasons: list[str] = []
        policy = ATTACHMENT_POLICY.get(meta.mime_type)
        kind = next((k for k, p in _VEHICLE_DOC_FILENAME if p.search(key)), None)
        if index >= MAX_ATTACHMENTS:
            reasons.append("TOO_MANY_ATTACHMENTS")
        if policy is None:
            reasons.append("MIME_NOT_ALLOWED")
        elif ext not in policy.extensions:
            reasons.append("EXTENSION_MIME_MISMATCH")
        if ext in _DANGEROUS_EXTENSIONS or inner_exts & _DANGEROUS_EXTENSIONS:
            reasons.append("DANGEROUS_EXTENSION")
        if meta.byte_size == 0:
            reasons.append("EMPTY_FILE")
        if policy is not None and meta.byte_size > policy.max_bytes:
            reasons.append("FILE_TOO_LARGE")
        if not reasons and total + meta.byte_size > MAX_TOTAL_ATTACHMENT_BYTES:
            reasons.append("TOTAL_SIZE_EXCEEDED")
        sensitive: list[str] = []
        if _IDENTITY_FILENAME_RE.search(key):
            sensitive.append("IDENTITY_DOCUMENT_SUSPECTED")
        if _FINANCIAL_FILENAME_RE.search(key):
            sensitive.append("FINANCIAL_DOCUMENT_SUSPECTED")
        if identity_in_body and kind is None:
            sensitive.append("IDENTITY_DOCUMENT_MENTIONED")
        if sensitive:
            decisions.append(
                AttachmentDecision(
                    index=index,
                    action=AttachmentAction.QUARANTINE_SENSITIVE,
                    reasons=tuple(sensitive + reasons),
                    safe_metadata={
                        "filename": "withheld-sensitive-attachment",
                        "mime_type": meta.mime_type,
                        "byte_size": meta.byte_size,
                        "sensitive_withheld": True,
                    },
                )
            )
            continue
        if reasons:
            decisions.append(
                AttachmentDecision(index=index, action=AttachmentAction.REJECT, reasons=tuple(reasons))
            )
            continue
        total += meta.byte_size
        decisions.append(
            AttachmentDecision(
                index=index,
                action=AttachmentAction.ALLOW_VEHICLE_DOCUMENT,
                reasons=("ALLOWED_VEHICLE_DOCUMENT_TYPE",),
                document_kind=kind,
                redaction_check_required=kind in (DocumentKind.REGISTRATION, None),
                safe_metadata={
                    "filename": safe_filename(name),
                    "mime_type": meta.mime_type,
                    "byte_size": meta.byte_size,
                    "sha256": meta.sha256,
                    "local_ref": meta.local_ref,
                    "document_kind": kind.value if kind else None,
                },
            )
        )
    return tuple(decisions)


# =============================================================================================
# Macedonian structured summary
# =============================================================================================

_MK_LANGUAGE: Final[dict[MessageLanguage | None, str]] = {
    _L.DE: "германски",
    _L.IT: "италијански",
    _L.FR: "француски",
    _L.EN: "англиски",
    None: "непознат",
}
_MK_AVAILABILITY: Final[dict[str, str]] = {
    "available": "продавачот наведува дека возилото е достапно",
    "sold": (
        "продавачот наведува дека возилото е продадено (само изјава; не утврдува купувач, "
        "продажна цена ниту дека Васко го купил)"
    ),
    "reserved": "продавачот наведува дека возилото е резервирано",
    "not_available": "продавачот наведува дека возилото повеќе не е достапно (причината не е наведена)",
    "conflicting": "противречни изјави за достапноста - потребна е проверка",
    "not_stated": "не е одговорено",
}
_MK_CONDITION: Final[dict[PriceCondition, str]] = {
    PriceCondition.NEGOTIABLE: "договорлива",
    PriceCondition.FINAL_OR_LOWEST: "последна/најниска цена според продавачот",
    PriceCondition.FIXED: "фиксна цена",
    PriceCondition.NO_DISCOUNT: "без попуст",
    PriceCondition.CASH_PAYMENT: "плаќање во готово",
    PriceCondition.EXPORT_ONLY: "само за извоз",
    PriceCondition.CONDITIONAL: "условена понуда",
    PriceCondition.TIME_LIMITED: "временски ограничена",
}
_MK_BASIS: Final[dict[PriceBasis, str]] = {
    PriceBasis.GROSS: "бруто (со ДДВ според продавачот)",
    PriceBasis.NET: "нето (без ДДВ според продавачот)",
    PriceBasis.UNKNOWN: "основата (бруто/нето) не е наведена",
}
_MK_DOCUMENT: Final[dict[DocumentKind, str]] = {
    DocumentKind.REGISTRATION: "Сообраќајна документација",
    DocumentKind.COC: "CoC (сертификат за сообразност)",
    DocumentKind.SERVICE_HISTORY: "Сервисна книшка/историја",
    DocumentKind.INSPECTION: "Технички преглед",
    DocumentKind.GENERAL: "Документи (општо)",
}
_MK_DOC_STATUS: Final[dict[DocumentClaimStatus, str]] = {
    DocumentClaimStatus.AVAILABLE: "достапни според продавачот",
    DocumentClaimStatus.ATTACHED: "приложени според продавачот",
    DocumentClaimStatus.REFUSED: "продавачот одбива да ги испрати",
    DocumentClaimStatus.NOT_AVAILABLE: "не се достапни според продавачот",
    DocumentClaimStatus.MENTIONED: "споменати без јасен статус",
}
_MK_REQUEST: Final[dict[RequestKind, str]] = {
    RequestKind.PAYMENT: "бара уплата/капара или зборува за плаќање",
    RequestKind.RESERVATION: "предлага резервација",
    RequestKind.IDENTITY_DOCUMENT: "бара или спомнува документ за идентитет",
    RequestKind.APPOINTMENT: "предлага термин/разгледување/пробно возење",
    RequestKind.COMMITMENT: "бара обврска или одлука за купување",
    RequestKind.PRICE_ACCEPTANCE: "прашува дали ја прифаќате цената",
    RequestKind.OPT_OUT: "бара да не биде повеќе контактиран (без автоматски одговор)",
    RequestKind.COMPLAINT: "поплака за контактот",
}
_MK_CONTEXT: Final[dict[AmountContext, str]] = {
    AmountContext.DEPOSIT_OR_PAYMENT: "капара/плаќање",
    AmountContext.OTHER_COST: "други трошоци (транспорт/такси)",
    AmountContext.PREVIOUS_PRICE: "претходна цена",
    AmountContext.UNLABELLED: "износ без јасен контекст",
    AmountContext.PRICE: "цена",
}
_MK_QUESTION: Final[dict[InquiryQuestion, str]] = {
    InquiryQuestion.AVAILABILITY: "достапност",
    InquiryQuestion.DOCUMENTS: "документи",
    InquiryQuestion.LOWEST_PRICE: "најниска/последна цена",
}


def format_amount_mk(amount: Decimal) -> str:
    """Exact amount in Macedonian notation (``.`` thousands, ``,`` decimals); no rounding."""
    sign = "-" if amount < 0 else ""
    text = format(abs(amount), "f")
    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{sign}{grouped},{fraction}" if fraction else f"{sign}{grouped}"


def _money_mk(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "износот не е јасен"
    return f"{format_amount_mk(amount)} {currency or '(валута не е наведена)'}"


class MkReplySummary(BaseModel):
    """Deterministic Macedonian structured summary; the original text stays alongside it."""

    model_config = _FROZEN

    version: str = MK_SUMMARY_VERSION
    original_language: MessageLanguage | None
    text: str
    unanswered: tuple[InquiryQuestion, ...]
    amounts_preserved: tuple[str, ...]
    translation_status: Literal["structured_summary_only", "structured_summary_with_translation"] = (
        "structured_summary_only"
    )
    full_translation: str | None = None
    translation_provider: str | None = None


@runtime_checkable
class TranslationProvider(Protocol):
    """Full free-text translation into Macedonian. Requires an approved LLM provider
    (``LLM_EXTRACTION_ENABLED`` is false), so no implementation is wired yet."""

    provider_id: str

    def translate(
        self, text: str, *, source_language: MessageLanguage | None, target_language: str
    ) -> str: ...


def build_mk_summary(
    claims: ReplyClaims,
    original_language: MessageLanguage | None,
    *,
    attachments: Sequence[AttachmentDecision] = (),
) -> MkReplySummary:
    """Macedonian structured summary preserving original amounts, currency, qualifications and
    unanswered questions (spec 37.7 step 5). Seller excerpts are sanitised and quoted."""
    vehicle_docs = sum(1 for a in attachments if a.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT)
    withheld = sum(1 for a in attachments if a.action == AttachmentAction.QUARANTINE_SENSITIVE)
    rejected = sum(1 for a in attachments if a.action == AttachmentAction.REJECT)
    lines = [
        "Резиме на одговорот од продавачот (автоматско структурирано резиме, не целосен превод)",
        f"Јазик на оригиналот: {_MK_LANGUAGE.get(original_language, 'непознат')}",
        f"Достапност: {_MK_AVAILABILITY[claims.availability_summary]}",
    ]
    amounts: list[str] = []
    if claims.prices:
        lines.append("Цена наведена од продавачот (непотврдена понуда; НЕ е прифатена):")
        for price in claims.prices:
            if price.kind == "range" and price.low is not None and price.high is not None:
                value = (
                    f"{format_amount_mk(price.low)}-{format_amount_mk(price.high)} "
                    f"{price.currency or '(валута не е наведена)'}"
                )
            elif price.kind == "minimum":
                value = f"не помалку од {_money_mk(price.amount, price.currency)}"
            else:
                value = _money_mk(price.amount, price.currency)
            amounts.append(value)
            details = [_MK_BASIS[price.basis], *(_MK_CONDITION[c] for c in price.conditions)]
            if "CONFLICTING_PRICE_CONDITIONS" in price.warnings:
                details.append("противречни услови - потребна е проверка")
            date_text = f"; дата: {price.quoted_at:%Y-%m-%d}" if price.quoted_at else ""
            lines.append(f"- {value} ({'; '.join(details)}{date_text}); оригинал: „{price.evidence.excerpt}“")
    else:
        lines.append("Цена: продавачот не наведе цена")
    for mention in claims.other_amounts:
        value = (
            _money_mk(mention.amount, mention.currency) if mention.amount is not None else f"„{mention.raw}“"
        )
        amounts.append(value)
        lines.append(
            f"- Друг износ ({_MK_CONTEXT[mention.context]}): {value}; оригинал: „{mention.evidence.excerpt}“"
        )
    if claims.documents:
        lines.append("Документи:")
        seen: set[tuple[DocumentKind, DocumentClaimStatus]] = set()
        for doc in claims.documents:
            if (doc.kind, doc.status) in seen:
                continue
            seen.add((doc.kind, doc.status))
            lines.append(f"- {_MK_DOCUMENT[doc.kind]}: {_MK_DOC_STATUS[doc.status]}")
    else:
        lines.append("Документи: не се споменати")
    if vehicle_docs or withheld or rejected:
        lines.append(
            f"Прилози: {vehicle_docs} дозволени документи/слики за возилото (за проверка); "
            f"{withheld} чувствителни прилози задржани и не се препраќаат; {rejected} одбиени"
        )
    escalations = list(dict.fromkeys(flag.kind for flag in claims.requests))
    if escalations:
        lines.append("Барања од продавачот (системот не одговара автоматски; одлуката е на Васко):")
        lines.extend(f"- {_MK_REQUEST[kind]}" for kind in escalations)
    unanswered = claims.unanswered_questions(vehicle_document_attachments=vehicle_docs)
    lines.append(
        "Неодговорени прашања: " + ("; ".join(_MK_QUESTION[q] for q in unanswered) if unanswered else "нема")
    )
    lines.append("Оригиналниот текст е зачуван и достапен покрај ова резиме.")
    return MkReplySummary(
        original_language=original_language,
        text="\n".join(lines),
        unanswered=unanswered,
        amounts_preserved=tuple(amounts),
    )


_DIGIT_RUN_RE: Final = re.compile(r"\d+")


def attach_full_translation(
    summary: MkReplySummary, *, original_text: str, translated_text: str, provider_id: str
) -> MkReplySummary:
    """Add a provider translation only if every digit run of the original survives in it.

    A translation that drops or alters a number (amount, year, mileage, reference) is refused
    and the structured summary stays the only Macedonian view.
    """
    if not provider_id or len(provider_id) > 100:
        raise ValidationFailed("translation provider id is required")
    original_digits = {d.lstrip("0") or "0" for d in _DIGIT_RUN_RE.findall(_normalize_text(original_text))}
    translated_digits = {
        d.lstrip("0") or "0" for d in _DIGIT_RUN_RE.findall(_normalize_text(translated_text))
    }
    if not original_digits <= translated_digits:
        raise ValidationFailed("translation does not preserve all numbers of the original")
    text = translated_text.strip()
    if not text or len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValidationFailed("translation is empty or too long")
    return summary.model_copy(
        update={
            "translation_status": "structured_summary_with_translation",
            "full_translation": text,
            "translation_provider": provider_id,
        }
    )


# =============================================================================================
# seller.reply.received.v1 signal
# =============================================================================================


class ReplySignalStatus(StrEnum):
    RECEIVED = "seller reply received"
    DECISION_NEEDED = "seller reply received; owner decision needed"
    SELLER_REPORTED_SOLD = "seller reply received; seller reports sold"
    DOCUMENTS_ATTACHED = "seller reply received; documents attached"


class SellerReplySignalDraft(BaseModel):
    """One ``ops.outbox`` row for the selected Slack seller-reply route (spec 37.6/37.7)."""

    model_config = _FROZEN

    event_id: UUID
    event_type: Literal["seller.reply.received"] = SELLER_REPLY_EVENT_TYPE
    event_name: Literal["seller.reply.received.v1"] = SELLER_REPLY_EVENT_NAME
    event_version: str = REPLY_SCHEMA_VERSION
    aggregate_type: Literal["seller_reply"] = "seller_reply"
    aggregate_id: UUID
    route: Literal["slack_seller_reply"] = SELLER_REPLY_ROUTE
    dedup_key: str
    payload: dict[str, Any]
    payload_hash: str
    payload_bytes: int
    is_fixture: bool
    is_canary: bool
    initial_state: OutboxState
    blocker_code: str | None


def reply_signal_route(provider_setting: str) -> Literal["slack"] | None:
    """Route for seller-reply activation: Slack only (never also native MCP Events)."""
    value = provider_setting.strip().lower()
    if value == "slack":
        return "slack"
    if value in {"disabled", ""}:
        return None
    raise ValidationFailed(
        "seller replies use the Slack signal route only; native MCP Events is for candidates"
    )


def dashboard_reply_url(dashboard_base_url: str, inquiry_id: UUID, reply_id: UUID) -> str:
    """``<base>/inquiries/<inquiry_id>/replies/<reply_id>``; https (http only for localhost),
    no credentials, query or fragment - the link never embeds an access token."""
    parts = urlsplit(dashboard_base_url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValidationFailed("dashboard base URL must be an absolute http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValidationFailed("dashboard base URL must use https outside local development")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValidationFailed("dashboard base URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ValidationFailed("dashboard base URL must not carry a query or fragment")
    url = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/inquiries/{inquiry_id}/replies/{reply_id}"
    if len(url) > 2048:
        raise ValidationFailed("dashboard URL too long")
    return url


def build_seller_reply_signal(
    *,
    event_id: UUID,
    inquiry_id: UUID,
    reply_id: UUID,
    listing_id: UUID | None,
    dashboard_base_url: str,
    occurred_at: datetime,
    status: ReplySignalStatus = ReplySignalStatus.RECEIVED,
    vehicle_cluster_id: UUID | None = None,
    is_fixture: bool = False,
    is_canary: bool = False,
) -> SellerReplySignalDraft:
    """Minimal internal ``seller.reply.received.v1`` signal: ids, safe dashboard URL and a brief
    fixed-vocabulary status. No body, attachment, address, name or price. Guarded by the shared
    notification payload guard. Fixture events start ``blocked`` (``FIXTURE_EVENT``) and never
    leave the system; a canary is routable (it validates the wiring) but carries ``canary: true``
    and never counts as a real reply."""
    if not isinstance(status, ReplySignalStatus):
        raise ValidationFailed("status must be a ReplySignalStatus")
    dedup = f"{SELLER_REPLY_EVENT_TYPE}:{reply_id}"
    payload: dict[str, Any] = {
        "schema_version": REPLY_SCHEMA_VERSION,
        "event_id": str(event_id),
        "type": SELLER_REPLY_EVENT_TYPE,
        "event_name": SELLER_REPLY_EVENT_NAME,
        "occurred_at": _rfc3339(_aware(occurred_at)),
        "inquiry_id": str(inquiry_id),
        "reply_id": str(reply_id),
        "dashboard_url": dashboard_reply_url(dashboard_base_url, inquiry_id, reply_id),
        "status": status.value,
        "deduplication_key": dedup,
        "route": SELLER_REPLY_ROUTE,
    }
    if listing_id is not None:
        payload["listing_id"] = str(listing_id)
    if vehicle_cluster_id is not None:
        payload["vehicle_cluster_id"] = str(vehicle_cluster_id)
    if is_canary:
        payload[CANARY_MARKER] = True
    if is_fixture:
        payload["fixture"] = True
    size = guard_payload(payload)
    return SellerReplySignalDraft(
        event_id=event_id,
        aggregate_id=reply_id,
        dedup_key=dedup,
        payload=payload,
        payload_hash=sha256_json(payload),
        payload_bytes=size,
        is_fixture=is_fixture,
        is_canary=is_canary,
        initial_state=OutboxState.BLOCKED if is_fixture else OutboxState.PENDING,
        blocker_code=FIXTURE_BLOCKER if is_fixture else None,
    )


# =============================================================================================
# Processing decision
# =============================================================================================


class AvailabilityEvidenceDraft(BaseModel):
    """Seller-statement availability evidence for ``app.availability_events``.

    It never establishes a purchase, a buyer or a transaction price.
    """

    model_config = _FROZEN

    availability: Availability
    evidence_kind: AvailabilityEvidenceKind
    reason: str = Field(max_length=80)
    establishes_purchase: Literal[False] = False
    realized_price: None = None


class EscalationReason(StrEnum):
    PAYMENT_REQUEST = "payment_request"
    RESERVATION_REQUEST = "reservation_request"
    IDENTITY_DOCUMENT_REQUEST = "identity_document_request"
    APPOINTMENT_REQUEST = "appointment_request"
    COMMITMENT_REQUEST = "commitment_request"
    PRICE_ACCEPTANCE_REQUEST = "price_acceptance_request"
    SENSITIVE_ATTACHMENT_WITHHELD = "sensitive_attachment_withheld"
    CONTRADICTORY_REPLY = "contradictory_reply"


_ESCALATION_FOR: Final[dict[RequestKind, EscalationReason]] = {
    RequestKind.PAYMENT: EscalationReason.PAYMENT_REQUEST,
    RequestKind.RESERVATION: EscalationReason.RESERVATION_REQUEST,
    RequestKind.IDENTITY_DOCUMENT: EscalationReason.IDENTITY_DOCUMENT_REQUEST,
    RequestKind.APPOINTMENT: EscalationReason.APPOINTMENT_REQUEST,
    RequestKind.COMMITMENT: EscalationReason.COMMITMENT_REQUEST,
    RequestKind.PRICE_ACCEPTANCE: EscalationReason.PRICE_ACCEPTANCE_REQUEST,
}


class ReplyProcessingDecision(BaseModel):
    """What the backend does with one ingested message. No outgoing mail is ever generated."""

    model_config = _FROZEN

    message_type: ReplyMessageType
    correlation_outcome: CorrelationOutcome
    apply_to_vehicle: bool
    needs_verification: bool
    inquiry_transition: InquiryState | None = None
    suppressions: tuple[SuppressionReason, ...] = ()
    escalate_to_owner: bool = False
    escalation_reasons: tuple[EscalationReason, ...] = ()
    availability_evidence: AvailabilityEvidenceDraft | None = None
    invalidate_valuation: bool = False
    queue_recalculation: bool = False
    emit_signal: bool = False
    signal_status: ReplySignalStatus | None = None
    resolves_uncertain_send: bool = False
    counts_as_real_reply: bool = False
    auto_response: Literal[False] = False
    follow_up: Literal[False] = False
    notes: tuple[str, ...] = ()


def decide_reply_processing(
    correlation: CorrelationResult,
    claims: ReplyClaims | None = None,
    *,
    attachments: Sequence[AttachmentDecision] = (),
    bounce: BounceDetails | None = None,
) -> ReplyProcessingDecision:
    """Deterministic processing decision (spec 37.5 states, 37.7 steps 3-7)."""
    mtype = correlation.message_type
    if correlation.outcome != CorrelationOutcome.MATCHED:
        note = (
            "possible match quarantined; verify before updating any vehicle"
            if correlation.outcome == CorrelationOutcome.QUARANTINED
            else "not related to an inquiry; stays in the local mailbox"
        )
        return ReplyProcessingDecision(
            message_type=mtype,
            correlation_outcome=correlation.outcome,
            apply_to_vehicle=False,
            needs_verification=correlation.outcome == CorrelationOutcome.QUARANTINED,
            notes=(note,),
        )
    base: dict[str, Any] = {
        "message_type": mtype,
        "correlation_outcome": correlation.outcome,
        "resolves_uncertain_send": correlation.resolves_uncertain_send,
    }
    if mtype == ReplyMessageType.BOUNCE:
        hard = bounce is None or bounce.permanent is not False
        return ReplyProcessingDecision(
            **base,
            apply_to_vehicle=False,
            needs_verification=False,
            inquiry_transition=InquiryState.BOUNCED,
            suppressions=(SuppressionReason.HARD_BOUNCE,) if hard else (),
            notes=("hard bounce: address suppressed" if hard else "transient bounce status; no suppression",),
        )
    if mtype in (ReplyMessageType.DELIVERY_NOTICE, ReplyMessageType.AUTO_REPLY):
        return ReplyProcessingDecision(
            **base,
            apply_to_vehicle=False,
            needs_verification=False,
            notes=(f"{mtype.value}: no inquiry state change and no response",),
        )
    claims = claims or ReplyClaims(language=None)
    suppressions: list[SuppressionReason] = []
    transition = InquiryState.REPLIED
    if claims.opted_out:
        suppressions.append(SuppressionReason.SELLER_OPT_OUT)
        transition = InquiryState.SELLER_OPTED_OUT
    if claims.complaint:
        suppressions.append(SuppressionReason.COMPLAINT)
        transition = InquiryState.SELLER_OPTED_OUT
    escalations = [_ESCALATION_FOR[k] for k in claims.escalation_kinds]
    if any(a.action == AttachmentAction.QUARANTINE_SENSITIVE for a in attachments):
        escalations.append(EscalationReason.SENSITIVE_ATTACHMENT_WITHHELD)
    summary = claims.availability_summary
    evidence: AvailabilityEvidenceDraft | None = None
    notes: list[str] = []
    if summary == "sold":
        evidence = AvailabilityEvidenceDraft(
            availability=Availability.SOLD_CLAIMED,
            evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            reason="seller_reported_sold",
        )
    elif summary == "available":
        evidence = AvailabilityEvidenceDraft(
            availability=Availability.AVAILABLE,
            evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE,
            reason="seller_reported_available",
        )
    elif summary == "reserved":
        evidence = AvailabilityEvidenceDraft(
            availability=Availability.RESERVED,
            evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_RESERVED,
            reason="seller_reported_reserved",
        )
    elif summary == "not_available":
        notes.append(
            "seller says no longer available without a reason; shown to the owner, not recorded as sold"
        )
    elif summary == "conflicting":
        escalations.append(EscalationReason.CONTRADICTORY_REPLY)
        notes.append("contradictory availability statements in one reply; no availability event")
    material = bool(
        claims.prices
        or evidence is not None
        or summary == "not_available"
        or any(d.status != DocumentClaimStatus.MENTIONED for d in claims.documents)
        or any(a.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT for a in attachments)
    )
    escalations = list(dict.fromkeys(escalations))
    if escalations:
        status = ReplySignalStatus.DECISION_NEEDED
    elif summary == "sold":
        status = ReplySignalStatus.SELLER_REPORTED_SOLD
    elif any(a.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT for a in attachments):
        status = ReplySignalStatus.DOCUMENTS_ATTACHED
    else:
        status = ReplySignalStatus.RECEIVED
    if claims.prices:
        notes.append("price statements stored as unaccepted seller quotes; advertised price unchanged")
    return ReplyProcessingDecision(
        **base,
        apply_to_vehicle=True,
        needs_verification=False,
        inquiry_transition=transition,
        suppressions=tuple(dict.fromkeys(suppressions)),
        escalate_to_owner=bool(escalations),
        escalation_reasons=tuple(escalations),
        availability_evidence=evidence,
        invalidate_valuation=material,
        queue_recalculation=material,
        emit_signal=True,
        signal_status=status,
        counts_as_real_reply=not correlation.is_canary,
        notes=tuple(notes),
    )


def should_emit_reply_signal(correlation: CorrelationResult) -> bool:
    """Only a matched seller reply activates dot; auto-replies, bounces, notices and quarantined
    possible matches stay in the dashboard/audit trail."""
    return (
        correlation.outcome == CorrelationOutcome.MATCHED
        and correlation.message_type == ReplyMessageType.SELLER_REPLY
    )


# =============================================================================================
# Building the upload (the only path by which content leaves the local mailbox)
# =============================================================================================


def build_ingest_request(
    message: InboundMessage,
    correlation: CorrelationResult,
    *,
    sanitized: SanitizedReplyBody,
    attachment_decisions: Sequence[AttachmentDecision],
    detected_language: MessageLanguage | None,
    observed_at: datetime,
) -> ReplyIngestRequest:
    """Assemble the 37.8 ingest request for a correlated message.

    Raises ``Forbidden`` for anything whose ``upload_scope`` is ``none``: unrelated personal
    mail and unresolved multi-inquiry ambiguity never leave the local mailbox. Only allowed
    vehicle-document attachment metadata is included; withheld sensitive attachments are
    counted, never described; rejected ones are omitted.
    """
    scope = correlation.upload_scope
    if scope == "none" or correlation.inquiry_id is None or correlation.binding_version is None:
        raise Forbidden("Message is not correlated to exactly one inquiry; it stays in the local mailbox")
    senders = message.from_addresses
    if len(senders) != 1 or senders[0] is None:
        raise ValidationFailed("a correlated upload needs exactly one valid sender address")
    allowed: list[AttachmentMeta] = []
    for decision in attachment_decisions:
        if decision.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT and decision.index < len(
            message.attachments
        ):
            meta = message.attachments[decision.index]
            allowed.append(meta.model_copy(update={"filename": safe_filename(meta.filename)}))
    withheld = sum(1 for d in attachment_decisions if d.action == AttachmentAction.QUARANTINE_SENSITIVE)
    in_reply_to = message.in_reply_to
    identity = message.identity
    if identity.received_at is None:
        raise ValidationFailed("the mailbox received time is required for an upload")
    received = identity.received_at
    subject = _HEADER_CONTROL_RE.sub(" ", message.subject)[:MAX_SUBJECT_CHARS]
    return ReplyIngestRequest(
        schema_version="1.0",
        inquiry_id=correlation.inquiry_id,
        binding_version=correlation.binding_version,
        mailbox_binding_id=identity.mailbox_binding_id,
        source_message=IngestSourceMessage(
            internet_message_id=identity.internet_message_id,
            provider_message_id=identity.provider_message_id,
            outlook_entry_id=identity.outlook_entry_id,
            outlook_store_id=identity.outlook_store_id,
            received_at=received,
        ),
        headers=IngestHeaders.model_validate(
            {
                "from": senders[0],
                "in_reply_to": in_reply_to[0] if in_reply_to else None,
                "references": message.references[:MAX_REFERENCES],
            }
        ),
        subject=subject,
        sanitized_body_text=sanitized.text,
        detected_language=detected_language,
        attachments=tuple(allowed[:MAX_ATTACHMENTS]),
        observed_at=_aware(observed_at),
        message_type=correlation.message_type,
        correlation_status="matched" if scope == "full" else "quarantined",
        correlation_reasons=correlation.reasons[:30],
        withheld_sensitive_attachments=withheld,
    )


__all__ = [
    "ATTACHMENT_POLICY",
    "CLAIMS_VERSION",
    "ESCALATION_KINDS",
    "FINGERPRINT_VERSION",
    "MAX_ATTACHMENTS",
    "MAX_BODY_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_SUBJECT_CHARS",
    "MAX_TOTAL_ATTACHMENT_BYTES",
    "MK_SUMMARY_VERSION",
    "SANITIZER_VERSION",
    "SELLER_REPLY_EVENT_NAME",
    "SELLER_REPLY_EVENT_TYPE",
    "SELLER_REPLY_ROUTE",
    "UNMATCHED_RETRY_WINDOW",
    "AmountContext",
    "AmountMention",
    "AttachmentAction",
    "AttachmentDecision",
    "AttachmentMeta",
    "AvailabilityClaim",
    "AvailabilityClaimStatus",
    "AvailabilityEvidenceDraft",
    "BounceDetails",
    "ClassificationSignal",
    "CorrelationOutcome",
    "CorrelationReason",
    "CorrelationResult",
    "DocumentClaim",
    "DocumentClaimStatus",
    "DocumentKind",
    "EscalationReason",
    "EvidenceSpan",
    "InboundMessage",
    "IngestDecision",
    "IngestDecisionKind",
    "IngestHeaders",
    "IngestSourceMessage",
    "InquiryBinding",
    "InquiryBindingState",
    "InquiryQuestion",
    "MessageClassification",
    "MessageHeaders",
    "MessageLocator",
    "MkReplySummary",
    "PriceCondition",
    "PriceQuoteClaim",
    "RemovedCounts",
    "ReplyClaims",
    "ReplyDedupKey",
    "ReplyIngestRequest",
    "ReplyProcessingDecision",
    "ReplySignalStatus",
    "ReplySourceContent",
    "RequestFlag",
    "RequestKind",
    "SanitizedReplyBody",
    "SellerReplySignalDraft",
    "SourceMessageIdentity",
    "StoredReplyIngest",
    "TranslationProvider",
    "UnmatchedRetention",
    "attach_full_translation",
    "build_ingest_request",
    "build_mk_summary",
    "build_seller_reply_signal",
    "canonical_address",
    "classify_message",
    "correlate_reply",
    "dashboard_reply_url",
    "decide_ingest",
    "decide_reply_processing",
    "evaluate_attachments",
    "explain_classification",
    "extract_reply_claims",
    "format_amount_mk",
    "is_forwarded",
    "is_processable_item",
    "normalize_message_id",
    "parse_address_list",
    "parse_delivery_report",
    "parse_message_id_list",
    "raise_for_ingest_conflict",
    "reply_dedup_key",
    "reply_signal_route",
    "safe_filename",
    "sanitize_reply_body",
    "should_emit_reply_signal",
    "source_content_fingerprint",
    "strip_quoted_text",
    "summarize_availability",
    "unmatched_retention",
]
