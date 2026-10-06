"""Standard Webhooks signing and verification for MCP Events delivery.

Wire facts (docs/research/mcp_events_and_webhooks.md sections 6, 9-11, verified
against `standardwebhooks` 1.1.0):

- Secrets are `whsec_` + base64 of 24..64 random bytes. The library itself does
  not enforce the length and decodes leniently, so `parse_whsec` validates
  strictly before any key reaches the library.
- `Webhook.sign()` *relabels* the timestamp as UTC instead of converting it, so
  every timestamp is converted with `ensure_utc` first. The body passed to
  `sign()` must be `str`; bytes would silently sign their `repr`.
- The library emits a single `v1` signature. Rotation is done here by joining
  one signature per secret, newest first, separated by single spaces.
- `verify()` raises `WebhookVerificationError` *or* a bare `ValueError` (entry
  without a comma, bad base64 padding, non UTF-8 body); both are caught and
  mapped to a typed `WebhookVerificationFailed`.
- The library does not deduplicate on `webhook-id`; `WebhookReplayGuard` does.
- The complete request body must not exceed 256 KiB (262,144 bytes).

Secrets never appear in messages, reprs or logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from standardwebhooks import Webhook, WebhookVerificationError

from suv_deals.clock import ensure_utc
from suv_deals.errors import AppError, ErrorCode, ValidationFailed

WHSEC_PREFIX: Final = "whsec_"
MIN_SECRET_BYTES: Final = 24
MAX_SECRET_BYTES: Final = 64
WEBHOOK_BODY_LIMIT_BYTES: Final = 262_144  # 256 KiB, the complete request body
SIGNATURE_TOLERANCE: Final = timedelta(minutes=5)  # hard-coded in standardwebhooks 1.1.0
MAX_SIGNATURES: Final = 4  # current + overlap secrets; more is a misconfiguration
MAX_WEBHOOK_ID_LENGTH: Final = 255
MAX_SUBSCRIPTION_ID_LENGTH: Final = 128

HEADER_ID: Final = "webhook-id"
HEADER_TIMESTAMP: Final = "webhook-timestamp"
HEADER_SIGNATURE: Final = "webhook-signature"
HEADER_SUBSCRIPTION: Final = "X-MCP-Subscription-Id"

# Our own ids are UUIDs, `sub_<hex>` and `msg_verification_<urlsafe>`; none contain a dot.
_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_SUBSCRIPTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{1,12}$")
_SIG_ENTRY_RE = re.compile(r"^v[0-9]+[a-z]?,[A-Za-z0-9+/]+={0,2}$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class InvalidWebhookSecret(ValidationFailed):
    """The supplied secret is not `whsec_` + strict base64 of 24..64 bytes."""

    def __init__(self, message: str) -> None:
        super().__init__(message, details={"field": "secret"})


class WebhookPayloadTooLarge(ValidationFailed):
    def __init__(self, size: int) -> None:
        super().__init__(
            "Webhook body exceeds the 256 KiB limit",
            details={"size_bytes": size, "limit_bytes": WEBHOOK_BODY_LIMIT_BYTES},
        )


class VerificationFailureReason(StrEnum):
    MISSING_HEADERS = "missing_headers"
    INVALID_ID = "invalid_id"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    MALFORMED_SIGNATURE = "malformed_signature"
    NO_MATCHING_SIGNATURE = "no_matching_signature"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_BODY = "invalid_body"
    REPLAYED = "replayed"


class WebhookVerificationFailed(AppError):
    """Inbound webhook rejected. The message is generic; `reason` is for metrics/logs."""

    def __init__(self, reason: VerificationFailureReason) -> None:
        super().__init__(ErrorCode.UNAUTHENTICATED, "Webhook signature verification failed", retryable=False)
        self.reason = reason


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WebhookSecret:
    """Decoded signing key. Never printable; compare with `matches()`."""

    key: bytes

    def __post_init__(self) -> None:
        if not MIN_SECRET_BYTES <= len(self.key) <= MAX_SECRET_BYTES:
            raise InvalidWebhookSecret("Secret must decode to 24..64 bytes")

    def __repr__(self) -> str:
        return "WebhookSecret(<redacted>)"

    __str__ = __repr__

    @property
    def fingerprint(self) -> str:
        """Short non-reversible identifier for change detection (never the key)."""
        return hashlib.sha256(b"suv_deals/whsec-fingerprint\x00" + self.key).hexdigest()[:16]

    def matches(self, other: WebhookSecret) -> bool:
        return hmac.compare_digest(self.key, other.key)

    def to_whsec(self) -> str:
        """Serialized form for encrypted storage only (see secret_box)."""
        return WHSEC_PREFIX + base64.b64encode(self.key).decode("ascii")


def parse_whsec(value: object) -> WebhookSecret:
    """Strictly parse a client-supplied `whsec_` secret (JSON-RPC -32602 on failure)."""
    if not isinstance(value, str) or not value.startswith(WHSEC_PREFIX):
        raise InvalidWebhookSecret("Secret must be a string starting with whsec_")
    b64 = value[len(WHSEC_PREFIX) :]
    if not b64 or len(b64) > 128 or not _BASE64_RE.fullmatch(b64):
        raise InvalidWebhookSecret("Secret is not valid base64")
    padded = b64 + "=" * (-len(b64) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidWebhookSecret("Secret is not valid base64") from exc
    if not MIN_SECRET_BYTES <= len(raw) <= MAX_SECRET_BYTES:
        raise InvalidWebhookSecret("Secret must decode to 24..64 bytes")
    return WebhookSecret(raw)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8, no NaN.

    This is not full RFC 8785 for floats; payloads and subscription arguments
    here contain only strings, integers, booleans, null, lists and objects.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def validate_webhook_id(webhook_id: object) -> str:
    """Standard Webhooks ids must not contain '.', and must not be user-controlled."""
    if not isinstance(webhook_id, str) or not _WEBHOOK_ID_RE.fullmatch(webhook_id):
        raise ValueError("webhook id must be 1..255 characters of [A-Za-z0-9_-] (no '.')")
    return webhook_id


def validate_subscription_id(subscription_id: object) -> str:
    if not isinstance(subscription_id, str) or not _SUBSCRIPTION_ID_RE.fullmatch(subscription_id):
        raise ValueError("subscription id must be 1..128 characters of [A-Za-z0-9_-]")
    return subscription_id


def _secret_list(secrets: WebhookSecret | Sequence[WebhookSecret]) -> list[WebhookSecret]:
    items = [secrets] if isinstance(secrets, WebhookSecret) else list(secrets)
    if not items:
        raise ValueError("at least one signing secret is required")
    if not all(isinstance(item, WebhookSecret) for item in items):
        raise TypeError("signing secrets must be WebhookSecret instances")
    unique: list[WebhookSecret] = []
    for item in items:
        if not any(item.matches(seen) for seen in unique):
            unique.append(item)
    if len(unique) > MAX_SIGNATURES:
        raise ValueError("too many concurrent signing secrets")
    return unique


def unix_seconds(timestamp: datetime) -> int:
    return math.floor(ensure_utc(timestamp).timestamp())


def sign(
    secrets: WebhookSecret | Sequence[WebhookSecret],
    webhook_id: str,
    timestamp: datetime,
    body: str,
) -> str:
    """Return the `webhook-signature` header value.

    `secrets` is ordered newest first; during rotation one `v1,<sig>` entry is
    produced per secret, separated by single spaces.
    """
    if not isinstance(body, str):
        raise TypeError("body must be str; bytes would sign their repr")
    validate_webhook_id(webhook_id)
    utc_ts = ensure_utc(timestamp)
    keys = _secret_list(secrets)
    signatures = [Webhook(key.key).sign(webhook_id, utc_ts, body) for key in keys]
    return " ".join(signatures)


@dataclass(frozen=True, slots=True)
class SignedRequest:
    body: bytes
    headers: dict[str, str]
    webhook_id: str
    signed_at: datetime


def build_signed_request(
    secrets: WebhookSecret | Sequence[WebhookSecret],
    webhook_id: str,
    subscription_id: str,
    payload: Any,
    *,
    now: datetime | None = None,
) -> SignedRequest:
    """Serialize `payload` exactly once and sign those bytes.

    A fresh `now` must be supplied for every attempt so the timestamp and
    signature are regenerated while `webhook_id` stays the same.
    """
    validate_webhook_id(webhook_id)
    validate_subscription_id(subscription_id)
    body_text = canonical_json(payload)
    raw = body_text.encode("utf-8")
    if len(raw) > WEBHOOK_BODY_LIMIT_BYTES:
        raise WebhookPayloadTooLarge(len(raw))
    signed_at = ensure_utc(now if now is not None else datetime.now(UTC))
    headers = {
        "Content-Type": "application/json",
        HEADER_ID: webhook_id,
        HEADER_TIMESTAMP: str(unix_seconds(signed_at)),
        HEADER_SIGNATURE: sign(secrets, webhook_id, signed_at, body_text),
        HEADER_SUBSCRIPTION: subscription_id,
    }
    return SignedRequest(body=raw, headers=headers, webhook_id=webhook_id, signed_at=signed_at)


@dataclass(frozen=True, slots=True)
class VerifiedWebhook:
    webhook_id: str
    timestamp: datetime
    payload: Any
    subscription_id: str | None


_LIBRARY_REASONS: tuple[tuple[str, VerificationFailureReason], ...] = (
    ("Missing required headers", VerificationFailureReason.MISSING_HEADERS),
    ("Invalid Signature Headers", VerificationFailureReason.MALFORMED_TIMESTAMP),
    ("Message timestamp too old", VerificationFailureReason.STALE_TIMESTAMP),
    ("Message timestamp too new", VerificationFailureReason.FUTURE_TIMESTAMP),
    ("No matching signature found", VerificationFailureReason.NO_MATCHING_SIGNATURE),
)


def _reason_from_library(exc: Exception) -> VerificationFailureReason:
    if isinstance(exc, WebhookVerificationError):
        text = str(exc)
        for prefix, reason in _LIBRARY_REASONS:
            if text.startswith(prefix):
                return reason
    return VerificationFailureReason.MALFORMED_SIGNATURE


def _strict_json(raw: bytes) -> Any:
    def _reject_constant(value: str) -> Any:
        raise ValueError("non-finite JSON number")

    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)


def verify_inbound(
    secrets: WebhookSecret | Sequence[WebhookSecret],
    raw: bytes,
    headers: Mapping[str, str],
    *,
    replay_guard: WebhookReplayGuard | None = None,
) -> VerifiedWebhook:
    """Verify a Standard Webhooks request against the raw body bytes, then parse it.

    Freshness uses the library's fixed 5-minute tolerance against the system clock.
    The body is only parsed after the signature has been verified.
    """
    if not isinstance(raw, bytes | bytearray):
        raise TypeError("raw body must be bytes as received on the wire")
    raw_bytes = bytes(raw)
    if len(raw_bytes) > WEBHOOK_BODY_LIMIT_BYTES:
        raise WebhookVerificationFailed(VerificationFailureReason.BODY_TOO_LARGE)
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    msg_id = lowered.get(HEADER_ID, "")
    msg_ts = lowered.get(HEADER_TIMESTAMP, "")
    msg_sig = lowered.get(HEADER_SIGNATURE, "")
    if not (msg_id and msg_ts and msg_sig):
        raise WebhookVerificationFailed(VerificationFailureReason.MISSING_HEADERS)
    try:
        validate_webhook_id(msg_id)
    except ValueError:
        raise WebhookVerificationFailed(VerificationFailureReason.INVALID_ID) from None
    if not _TIMESTAMP_RE.fullmatch(msg_ts):
        raise WebhookVerificationFailed(VerificationFailureReason.MALFORMED_TIMESTAMP)
    entries = msg_sig.split(" ")
    if len(entries) > 16 or not all(_SIG_ENTRY_RE.fullmatch(entry) for entry in entries):
        raise WebhookVerificationFailed(VerificationFailureReason.MALFORMED_SIGNATURE)
    keys = _secret_list(secrets)
    lib_headers = {HEADER_ID: msg_id, HEADER_TIMESTAMP: msg_ts, HEADER_SIGNATURE: msg_sig}
    last_reason = VerificationFailureReason.NO_MATCHING_SIGNATURE
    verified = False
    for key in keys:
        try:
            Webhook(key.key).verify(raw_bytes, lib_headers, json_parse=False)
        except (WebhookVerificationError, ValueError) as exc:
            last_reason = _reason_from_library(exc)
            if last_reason is not VerificationFailureReason.NO_MATCHING_SIGNATURE:
                break  # header-level failure: identical for every key
            continue
        verified = True
        break
    if not verified:
        raise WebhookVerificationFailed(last_reason)
    signed_at = datetime.fromtimestamp(int(msg_ts), tz=UTC)
    if replay_guard is not None and not replay_guard.check_and_record(msg_id, now=datetime.now(UTC)):
        raise WebhookVerificationFailed(VerificationFailureReason.REPLAYED)
    try:
        payload = _strict_json(raw_bytes)
    except ValueError:
        raise WebhookVerificationFailed(VerificationFailureReason.INVALID_BODY) from None
    subscription = lowered.get(HEADER_SUBSCRIPTION.lower())
    return VerifiedWebhook(webhook_id=msg_id, timestamp=signed_at, payload=payload, subscription_id=subscription)


@dataclass
class WebhookReplayGuard:
    """Process-local, bounded `webhook-id` dedup cache (Standard Webhooks idempotency key).

    The signature tolerance is 5 minutes, so remembering ids for longer than that
    rejects every replay of a captured request. Durable dedup for business
    effects belongs in the database; this guard only blocks fast replays.
    """

    ttl: timedelta = timedelta(minutes=10)
    max_entries: int = 10_000
    _seen: OrderedDict[str, datetime] = field(default_factory=OrderedDict)

    def check_and_record(self, webhook_id: str, *, now: datetime) -> bool:
        """Return True the first time an id is seen inside the TTL, False for a replay."""
        now = ensure_utc(now)
        self._prune(now)
        if webhook_id in self._seen:
            return False
        self._seen[webhook_id] = now
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.ttl
        while self._seen:
            oldest_id, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            del self._seen[oldest_id]

    def __len__(self) -> int:
        return len(self._seen)

