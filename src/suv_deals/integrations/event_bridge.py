"""MCP Events provider logic for `review.pending.v1` (spec sections 13, 20, 22).

This module holds the protocol-level rules; the MCP server (`mcp/*`) exposes
`events/list`, `events/subscribe` and `events/unsubscribe` on the authenticated
endpoint and persists subscriptions, and the outbox dispatcher calls
`decide_dispatch` / `deliver`. Everything here is pure or takes injected HTTP,
clock and randomness. Wire details follow docs/research/mcp_events_and_webhooks.md:

- ChatGPT supports only webhook delivery plus callback verification. Polling,
  streaming, `gap` and `terminated` are not supported and are not exposed.
- Subscription identity is derived from (authenticated principal, callback URL,
  event name, canonical arguments); the id is a routing handle, never accepted
  as input and never a capability.
- `ttlMs` omitted -> server default; a number -> grant <= n, clamped *up* to a
  server minimum; `null` (no expiry requested) -> this MVP still grants a finite
  lifetime, so `refreshBefore` is never null. `maxAgeMs` is accepted and ignored.
- No replay in the MVP: `cursor` is always null and `truncated` is always
  false (research doc sections 4 and 13: an event type without replay returns
  `cursor: null, truncated: false`); the durable pending-review tool is the
  catch-up path. A client-supplied cursor is accepted and ignored.
- Before any application data a signed, single-use, 60-second challenge must be
  echoed by a 2xx response (constant-time compare).
- Exactly one occurrence per request; `eventId` = outbox event UUID = `webhook-id`
  and stays the same on every retry while timestamp and signature are fresh.
- 2xx = receipt only (never review completion); 410/413 are terminal for that
  delivery only; 3xx is refused; 408/425/429/5xx, timeouts before sending and
  connection failures are retried with exponential backoff + jitter honouring
  Retry-After; a timeout after the request was sent is UNCERTAIN (spec 13).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import secrets as pysecrets
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import IntEnum, StrEnum
from typing import Any, Final, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import OutboxState, ProfileKey, ReviewState, Scope
from suv_deals.errors import AppError, ErrorCode, ValidationFailed
from suv_deals.integrations.safe_http import SafeHttp, SafeHttpError, SafeHttpFailure, SafeResponse
from suv_deals.integrations.secret_box import SecretBoxConfigError, parse_keyring
from suv_deals.integrations.webhook_signing import (
    InvalidWebhookSecret,
    WebhookPayloadTooLarge,
    WebhookSecret,
    build_signed_request,
    canonical_json,
    parse_whsec,
    validate_webhook_id,
)
from suv_deals.netguard import UnsafeDestination, parse_safe_url
from suv_deals.settings import Settings

EVENT_NAME: Final = "review.pending.v1"
DELIVERY_MODE: Final = "webhook"
PERMITTED_PROFILES: Final[tuple[str, ...]] = tuple(p.value for p in ProfileKey)
CALLBACK_SCHEMES: Final = ("https",)
CALLBACK_PORTS: Final = (443,)
MAX_CALLBACK_URL_LENGTH: Final = 2048
MAX_CURSOR_LENGTH: Final = 1024
_QUEUE_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_READINESS_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_QUEUE_RE = re.compile(_QUEUE_PATTERN)
_SECRET_QUERY_NAME = re.compile(
    r"^(?:[\w-]*[_-])?(token|key|secret|signature|sig|code|password|passwd|pwd|auth|apikey|jwt)$",
    re.IGNORECASE,
)
_OCCURRENCE_KEYS: Final = frozenset({"eventId", "name", "timestamp", "data", "cursor"})
_PAYLOAD_KEYS: Final = frozenset(
    {"case_id", "case_version", "listing_id", "listing_revision", "readiness", "dashboard_url"}
)
_REQUIRED_SCOPES: Final = (Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE)


# --------------------------------------------------------------------------- helpers


def rfc3339(value: datetime) -> str:
    """UTC RFC 3339 with a `Z` suffix (milliseconds only when non-zero)."""
    utc = ensure_utc(value)
    spec = "milliseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=spec).replace("+00:00", "Z")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- JSON-RPC errors


class JsonRpcCode(IntEnum):
    INVALID_PARAMS = -32602
    NOT_FOUND = -32011
    FORBIDDEN = -32012
    RESOURCE_EXHAUSTED = -32013
    UNSUPPORTED = -32014
    CALLBACK_ENDPOINT_ERROR = -32015


class CallbackErrorReason(StrEnum):
    """Draft `data.reason` / `deliveryStatus.lastError` categories (never raw responses)."""

    CHALLENGE_FAILED = "challenge_failed"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    TLS_ERROR = "tls_error"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"


_RPC_MESSAGES: Final = {
    JsonRpcCode.INVALID_PARAMS: "Invalid params",
    JsonRpcCode.NOT_FOUND: "NotFound",
    JsonRpcCode.FORBIDDEN: "Forbidden",
    JsonRpcCode.RESOURCE_EXHAUSTED: "ResourceExhausted",
    JsonRpcCode.UNSUPPORTED: "Unsupported",
    JsonRpcCode.CALLBACK_ENDPOINT_ERROR: "CallbackEndpointError",
}
_RPC_APP_CODES: Final = {
    JsonRpcCode.INVALID_PARAMS: ErrorCode.VALIDATION_ERROR,
    JsonRpcCode.NOT_FOUND: ErrorCode.NOT_FOUND,
    JsonRpcCode.FORBIDDEN: ErrorCode.FORBIDDEN,
    JsonRpcCode.RESOURCE_EXHAUSTED: ErrorCode.RATE_LIMITED,
    JsonRpcCode.UNSUPPORTED: ErrorCode.VALIDATION_ERROR,
    JsonRpcCode.CALLBACK_ENDPOINT_ERROR: ErrorCode.DEPENDENCY_UNAVAILABLE,
}


class EventsProtocolError(AppError):
    """A JSON-RPC error for the events/* methods. `rpc_data` never contains secrets or raw responses."""

    def __init__(self, rpc_code: JsonRpcCode, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(_RPC_APP_CODES[rpc_code], _RPC_MESSAGES[rpc_code], retryable=False)
        self.rpc_code = rpc_code
        self.rpc_data: dict[str, Any] | None = dict(data) if data else None

    def to_jsonrpc_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": int(self.rpc_code), "message": self.message}
        if self.rpc_data:
            error["data"] = dict(self.rpc_data)
        return error

    def to_mcp_error(self) -> Exception:
        """Convert for the MCP SDK (`raise err.to_mcp_error()` inside a method handler)."""
        from mcp import MCPError  # noqa: PLC0415 - optional import, only the MCP server needs it

        return MCPError(code=int(self.rpc_code), message=self.message, data=self.rpc_data)


def invalid_params(detail: str, *, field_name: str | None = None) -> EventsProtocolError:
    data: dict[str, Any] = {"detail": detail[:200]}
    if field_name:
        data["field"] = field_name
    return EventsProtocolError(JsonRpcCode.INVALID_PARAMS, data)


def not_found(kind: Literal["event", "subscription"]) -> EventsProtocolError:
    return EventsProtocolError(JsonRpcCode.NOT_FOUND, {"kind": kind})


def forbidden(detail: str = "principal is not allowed to use this event") -> EventsProtocolError:
    return EventsProtocolError(JsonRpcCode.FORBIDDEN, {"detail": detail[:200]})


def resource_exhausted(limit: str, maximum: int | None = None) -> EventsProtocolError:
    data: dict[str, Any] = {"limit": limit}
    if maximum is not None:
        data["max"] = maximum
    return EventsProtocolError(JsonRpcCode.RESOURCE_EXHAUSTED, data)


def unsupported(feature: str, value: object) -> EventsProtocolError:
    shown = value if isinstance(value, str | int | bool) or value is None else type(value).__name__
    if isinstance(shown, str):
        shown = shown[:32]
    return EventsProtocolError(JsonRpcCode.UNSUPPORTED, {"feature": feature, "value": shown})


def callback_endpoint_error(reason: CallbackErrorReason) -> EventsProtocolError:
    return EventsProtocolError(JsonRpcCode.CALLBACK_ENDPOINT_ERROR, {"reason": reason.value})


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True, slots=True)
class SubscriptionPolicy:
    """Engineering defaults (PROPOSED; ChatGPT's own timeouts/TTLs are UNVERIFIED)."""

    default_ttl: timedelta = timedelta(hours=12)
    min_ttl: timedelta = timedelta(minutes=5)
    max_ttl: timedelta = timedelta(hours=24)
    verification_cache_ttl: timedelta = timedelta(hours=24)
    challenge_ttl: timedelta = timedelta(seconds=60)
    verification_timeout_s: float = 10.0
    max_subscriptions_per_principal: int = 10
    secret_rotation_overlap: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.min_ttl <= self.default_ttl <= self.max_ttl:
            raise ValueError("require 0 < min_ttl <= default_ttl <= max_ttl")


DEFAULT_POLICY: Final = SubscriptionPolicy()


# --------------------------------------------------------------------------- events/list


def input_schema(permitted_profiles: Sequence[str] = PERMITTED_PROFILES) -> dict[str, Any]:
    """JSON Schema (2020-12) of the subscribe `arguments`: server-enforced filters only."""
    profiles = [p for p in PERMITTED_PROFILES if p in set(permitted_profiles)]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["profile"],
        "properties": {
            "profile": {
                "type": "string",
                "enum": profiles,
                "description": "Search profile whose review queue is monitored.",
            },
            "queue": {
                "type": "string",
                "pattern": _QUEUE_PATTERN,
                "maxLength": 64,
                "description": "Optional queue label inside the profile.",
            },
        },
    }


def payload_schema() -> dict[str, Any]:
    """JSON Schema of the occurrence `data`: a minimal reference, no seller text or money."""
    uuid_schema = {"type": "string", "format": "uuid", "maxLength": 36}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_PAYLOAD_KEYS),
        "properties": {
            "case_id": uuid_schema,
            "case_version": {"type": "integer", "minimum": 1},
            "listing_id": uuid_schema,
            "listing_revision": {"type": "integer", "minimum": 1},
            "readiness": {"type": "string", "pattern": _READINESS_PATTERN, "maxLength": 64},
            "dashboard_url": {"type": "string", "format": "uri", "maxLength": 2048},
        },
    }


def event_descriptor(permitted_profiles: Sequence[str] = PERMITTED_PROFILES) -> dict[str, Any]:
    return {
        "name": EVENT_NAME,
        "description": (
            "A review case in the selected profile queue became pending. The payload is a minimal "
            "reference; read details with the authenticated review and deal tools."
        ),
        "delivery": [DELIVERY_MODE],
        "inputSchema": input_schema(permitted_profiles),
        "payloadSchema": payload_schema(),
    }


def can_subscribe(principal: ActorContext) -> bool:
    return principal.principal_kind != "system" and all(principal.has(s) for s in _REQUIRED_SCOPES)


def list_events(
    principal: ActorContext,
    params: Mapping[str, Any] | None = None,
    *,
    permitted_profiles: Sequence[str] = PERMITTED_PROFILES,
) -> dict[str, Any]:
    """`events/list` result. Only events this principal may subscribe to are returned."""
    if params is not None:
        cursor = params.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH):
            raise invalid_params("cursor must be a string", field_name="cursor")
    if not can_subscribe(principal) or not permitted_profiles:
        return {"events": []}
    return {"events": [event_descriptor(permitted_profiles)]}


# --------------------------------------------------------------------------- identity


def validate_arguments(
    arguments: object, permitted_profiles: Sequence[str] = PERMITTED_PROFILES
) -> dict[str, str]:
    """Validate subscribe `arguments` against the inputSchema (additionalProperties: false)."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise invalid_params("arguments must be an object", field_name="arguments")
    unknown = sorted(str(k) for k in arguments if k not in {"profile", "queue"})
    if unknown:
        raise invalid_params("unknown argument(s)", field_name="arguments")
    profile = arguments.get("profile")
    if not isinstance(profile, str) or profile not in PERMITTED_PROFILES:
        raise invalid_params("profile must be one of the permitted profiles", field_name="arguments.profile")
    if profile not in set(permitted_profiles):
        raise invalid_params("profile is not enabled for this workspace", field_name="arguments.profile")
    normalized = {"profile": profile}
    if "queue" in arguments:
        queue = arguments["queue"]
        if not isinstance(queue, str) or not _QUEUE_RE.fullmatch(queue):
            raise invalid_params("queue must be a short lowercase label", field_name="arguments.queue")
        normalized["queue"] = queue
    return normalized


def subscription_identity(
    principal_id: UUID | str,
    callback_url: str,
    event_name: str,
    arguments: Mapping[str, Any],
    *,
    workspace_id: UUID | str | None = None,
) -> str:
    """Deterministic `sub_<32 hex>` from the four immutable identity parts (+ workspace)."""
    key: dict[str, Any] = {
        "principal": str(principal_id),
        "url": callback_url,
        "name": event_name,
        "arguments": dict(arguments),
    }
    if workspace_id is not None:
        key["workspace"] = str(workspace_id)
    return "sub_" + _sha256_hex(canonical_json(key))[:32]


def verification_cache_key(
    principal_id: UUID | str, callback_url: str, *, workspace_id: UUID | str | None = None
) -> str:
    """Verification is cached per (authenticated principal, callback URL) across arguments."""
    key: dict[str, Any] = {"principal": str(principal_id), "url": callback_url}
    if workspace_id is not None:
        key["workspace"] = str(workspace_id)
    return "vrf_" + _sha256_hex(canonical_json(key))[:32]


def validate_callback_url(url: object) -> str:
    """https only, port 443, public DNS name or public IP literal, no credentials/fragment."""
    if not isinstance(url, str) or not url or len(url) > MAX_CALLBACK_URL_LENGTH:
        raise invalid_params("callback url must be a string of at most 2048 characters", field_name="url")
    if "#" in url:
        raise invalid_params("callback url must not contain a fragment", field_name="url")
    try:
        parse_safe_url(url, allowed_schemes=CALLBACK_SCHEMES, allowed_ports=CALLBACK_PORTS)
    except UnsafeDestination:
        raise invalid_params("callback url is not an allowed https destination", field_name="url") from None
    return url


# --------------------------------------------------------------------------- events/subscribe

TtlKind = Literal["default", "value", "no_expiry"]
_MISSING: Final = object()


def _require_subscriber(principal: ActorContext) -> None:
    if principal.principal_kind == "system":
        raise forbidden("webhook subscriptions require an authenticated user or client principal")
    if not all(principal.has(s) for s in _REQUIRED_SCOPES):
        raise forbidden("requires reviews:read and events:subscribe")


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise invalid_params(f"{field_name} must be a non-negative integer", field_name=field_name)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise invalid_params(f"{field_name} must be a non-negative integer", field_name=field_name)
    return value


def grant_ttl(raw_ttl: object, policy: SubscriptionPolicy = DEFAULT_POLICY) -> tuple[TtlKind, timedelta]:
    """Apply the draft TTL rules. `raw_ttl` is `_MISSING` (omitted), None (no expiry) or a number."""
    if raw_ttl is _MISSING:
        return "default", policy.default_ttl
    if raw_ttl is None:
        # No-expiry is requested, but the MVP grants only finite lifetimes (refreshBefore never null).
        return "no_expiry", policy.max_ttl
    requested_ms = _non_negative_int(raw_ttl, "ttlMs")
    max_ms = policy.max_ttl // timedelta(milliseconds=1)
    granted = timedelta(milliseconds=min(requested_ms, max_ms))  # cap before timedelta (overflow)
    return "value", max(granted, policy.min_ttl)


@dataclass(frozen=True, slots=True)
class SubscriptionRequest:
    """A validated, normalized events/subscribe request (create or refresh)."""

    subscription_id: str
    workspace_id: UUID
    principal_id: UUID
    event_name: str
    arguments: Mapping[str, str]
    canonical_arguments: str
    callback_url: str
    callback_host: str
    secret: WebhookSecret
    ttl_kind: TtlKind
    requested_ttl_ms: int | None
    granted_ttl: timedelta
    expires_at: datetime
    cursor_supplied: bool
    verification_cache_key: str

    @property
    def refresh_before(self) -> datetime:
        return self.expires_at

    def result(self) -> dict[str, Any]:
        return subscribe_result(self)


def validate_subscribe_params(
    params: object,
    principal: ActorContext,
    *,
    clock: Clock,
    policy: SubscriptionPolicy = DEFAULT_POLICY,
    permitted_profiles: Sequence[str] = PERMITTED_PROFILES,
) -> SubscriptionRequest:
    """Validate events/subscribe params for an authenticated principal.

    `params` must be the raw params mapping so an omitted `ttlMs` can be told
    apart from `ttlMs: null`. Unknown top-level envelope keys (e.g. `_meta`) are
    tolerated; `arguments` are validated strictly against the inputSchema.
    Does NOT verify the callback; call `run_verification_challenge` next unless
    `needs_verification` says a cached verification still applies.
    """
    _require_subscriber(principal)
    if not isinstance(params, Mapping):
        raise invalid_params("params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name or len(name) > 128:
        raise invalid_params("name must be a non-empty string", field_name="name")
    if name != EVENT_NAME:
        raise not_found("event")
    arguments = validate_arguments(params.get("arguments"), permitted_profiles)
    delivery = params.get("delivery")
    if not isinstance(delivery, Mapping):
        raise invalid_params("delivery must be an object", field_name="delivery")
    mode = delivery.get("mode")
    if mode != DELIVERY_MODE:
        if not isinstance(mode, str):
            raise invalid_params("delivery.mode must be a string", field_name="delivery.mode")
        raise unsupported("deliveryMode", mode)
    url = validate_callback_url(delivery.get("url"))
    try:
        secret = parse_whsec(delivery.get("secret"))
    except InvalidWebhookSecret:
        raise invalid_params(
            "secret must be whsec_ followed by base64 of 24..64 bytes", field_name="delivery.secret"
        ) from None
    cursor = params.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH):
        raise invalid_params("cursor must be a string or null", field_name="cursor")
    if "maxAgeMs" in params and params["maxAgeMs"] is not None:
        _non_negative_int(params["maxAgeMs"], "maxAgeMs")  # accepted and ignored (no replay)
    raw_ttl = params.get("ttlMs", _MISSING)
    ttl_kind, granted = grant_ttl(raw_ttl, policy)
    now = ensure_utc(clock.now())
    host = urlsplit(url).hostname or ""
    return SubscriptionRequest(
        subscription_id=subscription_identity(
            principal.principal_id, url, name, arguments, workspace_id=principal.workspace_id
        ),
        workspace_id=principal.workspace_id,
        principal_id=principal.principal_id,
        event_name=name,
        arguments=dict(arguments),
        canonical_arguments=canonical_json(arguments),
        callback_url=url,
        callback_host=host.lower(),
        secret=secret,
        ttl_kind=ttl_kind,
        requested_ttl_ms=None if ttl_kind != "value" else _non_negative_int(raw_ttl, "ttlMs"),
        granted_ttl=granted,
        expires_at=now + granted,
        cursor_supplied=cursor is not None,
        verification_cache_key=verification_cache_key(
            principal.principal_id, url, workspace_id=principal.workspace_id
        ),
    )


def subscribe_result(
    request: SubscriptionRequest, *, delivery_status: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": request.subscription_id,
        "refreshBefore": rfc3339(request.expires_at),
        # review.pending.v1 does not support replay: the verified rule for such event types
        # is `cursor: null, truncated: false`, also when the client sent a cursor (we never
        # issue one). `truncated: true` is reserved for a future replay implementation.
        "cursor": None,
        "truncated": False,
    }
    if delivery_status is not None:
        result["deliveryStatus"] = dict(delivery_status)
    return result


def check_subscription_quota(
    active_for_principal: int, *, is_refresh: bool, policy: SubscriptionPolicy = DEFAULT_POLICY
) -> None:
    if not is_refresh and active_for_principal >= policy.max_subscriptions_per_principal:
        raise resource_exhausted("subscriptions", policy.max_subscriptions_per_principal)


def delivery_status_payload(
    *,
    active: bool,
    last_delivery_at: datetime | None,
    last_error: CallbackErrorReason | None,
    failed_since: datetime | None = None,
) -> dict[str, Any]:
    """Optional draft `deliveryStatus` (whether ChatGPT reads it is UNVERIFIED)."""
    status: dict[str, Any] = {
        "active": active,
        "lastDeliveryAt": rfc3339(last_delivery_at) if last_delivery_at else None,
        "lastError": last_error.value if last_error else None,
    }
    if failed_since is not None:
        status["failedSince"] = rfc3339(failed_since)
    return status


# --------------------------------------------------------------------------- events/unsubscribe


@dataclass(frozen=True, slots=True)
class UnsubscribeRequest:
    subscription_id: str
    workspace_id: UUID
    principal_id: UUID


def validate_unsubscribe_params(params: object, principal: ActorContext) -> UnsubscribeRequest:
    """Resolve the identity from (principal from auth, url, name, canonical args); no secret.

    The URL is not SSRF-checked because nothing is contacted; any shape error
    simply yields an identity that matches nothing, and the result stays `{}`.
    """
    if principal.principal_kind == "system" or not principal.has(Scope.EVENTS_SUBSCRIBE):
        raise forbidden("requires events:subscribe")
    if not isinstance(params, Mapping):
        raise invalid_params("params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name or len(name) > 128:
        raise invalid_params("name must be a non-empty string", field_name="name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise invalid_params("arguments must be an object", field_name="arguments")
    delivery = params.get("delivery")
    if not isinstance(delivery, Mapping):
        raise invalid_params("delivery must be an object", field_name="delivery")
    url = delivery.get("url")
    if not isinstance(url, str) or not url or len(url) > MAX_CALLBACK_URL_LENGTH:
        raise invalid_params("delivery.url must be a string", field_name="delivery.url")
    try:
        canonical_json(dict(arguments))
    except (TypeError, ValueError):
        raise invalid_params("arguments must be JSON", field_name="arguments") from None
    return UnsubscribeRequest(
        subscription_id=subscription_identity(
            principal.principal_id, url, name, dict(arguments), workspace_id=principal.workspace_id
        ),
        workspace_id=principal.workspace_id,
        principal_id=principal.principal_id,
    )


def unsubscribe_result() -> dict[str, Any]:
    """Always `{}`: idempotent even when nothing matched (OpenAI wording; draft -32011 UNVERIFIED)."""
    return {}


# --------------------------------------------------------------------------- callback verification


class ChallengeCheck(StrEnum):
    OK = "ok"
    MISMATCH = "mismatch"
    EXPIRED = "expired"
    UNKNOWN_OR_REUSED = "unknown_or_reused"


@dataclass
class ChallengeLedger:
    """Issued challenges: single use (removed on first check) and short-lived."""

    ttl: timedelta = timedelta(seconds=60)
    max_outstanding: int = 1000
    _issued: dict[str, datetime] = field(default_factory=dict)

    def issue(self, now: datetime, token_factory: Callable[[], str]) -> str:
        now = ensure_utc(now)
        for token, issued_at in list(self._issued.items()):
            if now - issued_at > self.ttl:
                del self._issued[token]
        if len(self._issued) >= self.max_outstanding:
            raise resource_exhausted("verification_challenges", self.max_outstanding)
        token = token_factory()
        if not isinstance(token, str) or len(token) < 32 or token in self._issued:
            raise ValueError("challenge token must be a fresh string of at least 32 characters")
        self._issued[token] = now
        return token

    def consume(self, expected: str, echoed: object, now: datetime) -> ChallengeCheck:
        issued_at = self._issued.pop(expected, None)
        if issued_at is None:
            return ChallengeCheck.UNKNOWN_OR_REUSED
        if not isinstance(echoed, str) or len(echoed) > 512:
            return ChallengeCheck.MISMATCH
        if not hmac.compare_digest(expected.encode("utf-8"), echoed.encode("utf-8")):
            return ChallengeCheck.MISMATCH
        elapsed = ensure_utc(now) - issued_at
        if elapsed > self.ttl or elapsed < timedelta(0):
            return ChallengeCheck.EXPIRED
        return ChallengeCheck.OK

    def __len__(self) -> int:
        return len(self._issued)


@dataclass
class VerificationRateLimiter:
    """Per destination host sliding window for verification POSTs (process-local)."""

    max_attempts: int = 5
    window: timedelta = timedelta(minutes=1)
    max_hosts: int = 10_000
    _attempts: dict[str, deque[datetime]] = field(default_factory=dict)

    def allow(self, host: str, now: datetime) -> bool:
        now = ensure_utc(now)
        host = host.lower()
        bucket = self._attempts.get(host)
        if bucket is None:
            if len(self._attempts) >= self.max_hosts:
                self._evict(now)
            bucket = deque()
            self._attempts[host] = bucket
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if len(bucket) >= self.max_attempts:
            return False
        bucket.append(now)
        return True

    def _evict(self, now: datetime) -> None:
        for host, bucket in list(self._attempts.items()):
            if not bucket or now - bucket[-1] >= self.window:
                del self._attempts[host]
        while len(self._attempts) >= self.max_hosts:
            self._attempts.pop(next(iter(self._attempts)))


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    reason: CallbackErrorReason | None
    detail: str | None  # internal classification only (never a raw response)
    status_code: int | None
    webhook_id: str
    attempted_at: datetime
    verified_at: datetime | None
    secret_fingerprint: str

    def raise_for_failure(self) -> None:
        if not self.ok:
            raise callback_endpoint_error(self.reason or CallbackErrorReason.CHALLENGE_FAILED)


_VERIFY_FAILURE_REASON: Final = {
    SafeHttpFailure.DESTINATION_REJECTED: CallbackErrorReason.CONNECTION_REFUSED,
    SafeHttpFailure.CONNECT_FAILED: CallbackErrorReason.CONNECTION_REFUSED,
    SafeHttpFailure.TLS_FAILED: CallbackErrorReason.TLS_ERROR,
    SafeHttpFailure.TIMEOUT_BEFORE_SEND: CallbackErrorReason.TIMEOUT,
    SafeHttpFailure.TIMEOUT_AFTER_SEND: CallbackErrorReason.TIMEOUT,
    SafeHttpFailure.CONNECTION_LOST: CallbackErrorReason.CONNECTION_REFUSED,
    SafeHttpFailure.REDIRECT_REFUSED: CallbackErrorReason.CHALLENGE_FAILED,
    SafeHttpFailure.PROTOCOL_ERROR: CallbackErrorReason.CHALLENGE_FAILED,
    SafeHttpFailure.REQUEST_TOO_LARGE: CallbackErrorReason.CHALLENGE_FAILED,
}


def _status_reason(status: int) -> CallbackErrorReason:
    if 400 <= status < 500:
        return CallbackErrorReason.HTTP_4XX
    if 500 <= status < 600:
        return CallbackErrorReason.HTTP_5XX
    return CallbackErrorReason.CHALLENGE_FAILED


def _default_challenge_token() -> str:
    return pysecrets.token_urlsafe(32)


def _default_message_suffix() -> str:
    return pysecrets.token_urlsafe(16)


def _echoed_challenge(response: SafeResponse) -> object:
    if response.truncated or not response.body:
        return None
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get("challenge")


async def run_verification_challenge(
    url: str,
    secret: WebhookSecret,
    subscription_id: str,
    http: SafeHttp,
    *,
    clock: Clock,
    ledger: ChallengeLedger | None = None,
    rate_limiter: VerificationRateLimiter | None = None,
    policy: SubscriptionPolicy = DEFAULT_POLICY,
    token_factory: Callable[[], str] = _default_challenge_token,
    id_factory: Callable[[], str] = _default_message_suffix,
) -> VerificationResult:
    """POST a signed `{"type":"verification","challenge":...}` and require a 2xx exact echo.

    The challenge is fresh (32 random bytes), single-use and accepted only within
    `policy.challenge_ttl` of issue. Failures map to the draft reasons.
    """
    validate_callback_url(url)
    ledger = ledger if ledger is not None else ChallengeLedger(ttl=policy.challenge_ttl)
    attempted_at = ensure_utc(clock.now())
    host = (urlsplit(url).hostname or "").lower()
    if rate_limiter is not None and not rate_limiter.allow(host, attempted_at):
        raise resource_exhausted("verification_rate", rate_limiter.max_attempts)
    challenge = ledger.issue(attempted_at, token_factory)
    webhook_id = validate_webhook_id(f"msg_verification_{id_factory()}")
    signed = build_signed_request(
        secret,
        webhook_id,
        subscription_id,
        {"type": "verification", "challenge": challenge},
        now=attempted_at,
    )

    def result(
        reason: CallbackErrorReason | None, detail: str | None, status: int | None
    ) -> VerificationResult:
        ok = reason is None
        return VerificationResult(
            ok=ok,
            reason=reason,
            detail=detail,
            status_code=status,
            webhook_id=webhook_id,
            attempted_at=attempted_at,
            verified_at=ensure_utc(clock.now()) if ok else None,
            secret_fingerprint=secret.fingerprint,
        )

    try:
        response = await http.post(
            url,
            content=signed.body,
            headers=signed.headers,
            timeout_s=policy.verification_timeout_s,
            max_response_bytes=4096,
        )
    except SafeHttpError as exc:
        ledger.consume(challenge, None, clock.now())  # burn the challenge
        return result(_VERIFY_FAILURE_REASON[exc.failure], exc.failure.value, exc.status_code)
    if not response.is_success:
        ledger.consume(challenge, None, clock.now())
        return result(_status_reason(response.status_code), "non_2xx", response.status_code)
    check = ledger.consume(challenge, _echoed_challenge(response), clock.now())
    if check is not ChallengeCheck.OK:
        return result(CallbackErrorReason.CHALLENGE_FAILED, check.value, response.status_code)
    return result(None, None, response.status_code)


@dataclass(frozen=True, slots=True)
class VerificationCacheEntry:
    cache_key: str
    verified_at: datetime
    secret_fingerprint: str


def needs_verification(
    entry: VerificationCacheEntry | None,
    *,
    principal_id: UUID | str,
    callback_url: str,
    secret: WebhookSecret,
    now: datetime,
    workspace_id: UUID | str | None = None,
    policy: SubscriptionPolicy = DEFAULT_POLICY,
) -> bool:
    """True unless a bounded, still-valid verification exists for this principal+URL+secret."""
    if entry is None:
        return True
    expected_key = verification_cache_key(principal_id, callback_url, workspace_id=workspace_id)
    if not hmac.compare_digest(entry.cache_key, expected_key):
        return True
    if not hmac.compare_digest(entry.secret_fingerprint, secret.fingerprint):
        return True  # secret rotation invalidates the verification
    now = ensure_utc(now)
    verified_at = ensure_utc(entry.verified_at)
    if verified_at > now + timedelta(minutes=1):
        return True  # clock skew or tampering: never trust a future verification
    return now - verified_at >= policy.verification_cache_ttl


# --------------------------------------------------------------------------- subscription lifecycle


class SubscriptionStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    REVOKED = "revoked"
    UNSUBSCRIBED = "unsubscribed"


class BlockReason(StrEnum):
    UNSUBSCRIBED = "subscription_unsubscribed"
    REVOKED = "subscription_revoked"
    UNVERIFIED = "subscription_unverified"
    EXPIRED = "subscription_expired"
    NO_SECRET = "subscription_no_secret"  # noqa: S105 - a reason label, not a secret
    WRONG_EVENT = "subscription_wrong_event"


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """What the dispatcher loads from ops.event_subscriptions (secrets already decrypted)."""

    subscription_id: str
    workspace_id: UUID
    principal_id: UUID
    event_name: str
    arguments: Mapping[str, str]
    callback_url: str
    secrets: tuple[WebhookSecret, ...]  # newest first; see signing_secrets()
    status: SubscriptionStatus
    expires_at: datetime
    verified_at: datetime | None

    def __repr__(self) -> str:
        return (
            f"DeliveryTarget(subscription_id={self.subscription_id!r}, status={self.status.value}, "
            f"expires_at={self.expires_at.isoformat()})"
        )


AccessCheck = Callable[[DeliveryTarget], Awaitable[bool]]


def delivery_block_reason(target: DeliveryTarget, now: datetime) -> BlockReason | None:
    """Expired, revoked, unsubscribed or unverified subscriptions receive nothing."""
    if target.status is SubscriptionStatus.UNSUBSCRIBED:
        return BlockReason.UNSUBSCRIBED
    if target.status is SubscriptionStatus.REVOKED:
        return BlockReason.REVOKED
    if target.status is SubscriptionStatus.PENDING_VERIFICATION or target.verified_at is None:
        return BlockReason.UNVERIFIED
    if ensure_utc(now) >= ensure_utc(target.expires_at):
        return BlockReason.EXPIRED
    if not target.secrets:
        return BlockReason.NO_SECRET
    if target.event_name != EVENT_NAME:
        return BlockReason.WRONG_EVENT
    return None


def signing_secrets(
    current: WebhookSecret,
    previous: WebhookSecret | None = None,
    previous_valid_until: datetime | None = None,
    *,
    now: datetime,
) -> tuple[WebhookSecret, ...]:
    """During a short rotation window sign with both keys (newest first)."""
    if previous is None or previous_valid_until is None or previous.matches(current):
        return (current,)
    if ensure_utc(now) < ensure_utc(previous_valid_until):
        return (current, previous)
    return (current,)


# --------------------------------------------------------------------------- occurrence


_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})


def dashboard_url_problem(value: str, *, allow_local_http: bool) -> str | None:
    """Why a dashboard link may not leave the system, or None when it is acceptable.

    Links must require normal dashboard authentication (spec 22): absolute https
    (plain http only for local development when allowed), no embedded
    credentials, no fragment and no token-like query parameters.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        return "dashboard_url must be a string of at most 2048 characters"
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        return "dashboard_url must not contain whitespace or control characters"
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return "dashboard_url is malformed"
    if parts.scheme not in {"https", "http"} or not hostname:
        return "dashboard_url must be an absolute http(s) URL"
    if parts.scheme == "http" and not (allow_local_http and hostname in _LOCAL_HOSTS):
        return "dashboard_url must use https outside local development"
    if parts.username or parts.password or "@" in parts.netloc:
        return "dashboard_url must not embed credentials"
    if parts.fragment:
        return "dashboard_url must not contain a fragment"
    for name, _ in parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_QUERY_NAME.fullmatch(name):
            return "dashboard_url must not embed access tokens"
    return None


class ReviewPendingSignal(BaseModel):
    """Read view of the internal `review.pending` outbox payload (spec 22).

    Unknown internal fields (summary, priority, ...) are ignored on purpose:
    only the six minimal reference fields ever leave the system.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: UUID
    type: Literal["review.pending"] = "review.pending"
    occurred_at: datetime
    case_id: UUID
    case_version: StrictInt = Field(ge=1)
    listing_id: UUID
    listing_revision: StrictInt = Field(ge=1)
    readiness: str = Field(default="unknown", pattern=_READINESS_PATTERN)
    dashboard_url: str = Field(max_length=2048)
    profile: ProfileKey | None = None
    queue: str | None = Field(default=None, pattern=_QUEUE_PATTERN)

    @field_validator("occurred_at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("dashboard_url")
    @classmethod
    def _safe_dashboard_url(cls, value: str) -> str:
        problem = dashboard_url_problem(value, allow_local_http=True)
        if problem is not None:
            raise ValueError(problem)
        return value


def parse_signal(outbox_event: ReviewPendingSignal | Mapping[str, Any]) -> ReviewPendingSignal:
    if isinstance(outbox_event, ReviewPendingSignal):
        return outbox_event
    try:
        return ReviewPendingSignal.model_validate(dict(outbox_event))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise ValidationFailed("Invalid review.pending outbox payload", details={"fields": fields}) from None


def build_occurrence(outbox_event: ReviewPendingSignal | Mapping[str, Any]) -> dict[str, Any]:
    """Map one committed outbox event to exactly one MCP EventOccurrence."""
    signal = parse_signal(outbox_event)
    return {
        "eventId": str(signal.event_id),
        "name": EVENT_NAME,
        "timestamp": rfc3339(signal.occurred_at),
        "data": {
            "case_id": str(signal.case_id),
            "case_version": signal.case_version,
            "listing_id": str(signal.listing_id),
            "listing_revision": signal.listing_revision,
            "readiness": signal.readiness,
            "dashboard_url": signal.dashboard_url,
        },
        "cursor": None,
    }


def matches_filters(arguments: Mapping[str, str], signal: ReviewPendingSignal) -> bool:
    """Server-side filter: profile must match; queue must match when the subscription names one."""
    if signal.profile is None or arguments.get("profile") != signal.profile.value:
        return False
    if "queue" in arguments:
        return signal.queue is not None and signal.queue == arguments["queue"]
    return True


def _occurrence_problem(occurrence: Mapping[str, Any], target: DeliveryTarget) -> str | None:
    if set(occurrence) != _OCCURRENCE_KEYS:
        return "keys"
    if occurrence.get("name") != EVENT_NAME or target.event_name != EVENT_NAME:
        return "name"
    try:
        validate_webhook_id(occurrence.get("eventId"))
    except ValueError:
        return "event_id"
    data = occurrence.get("data")
    if not isinstance(data, Mapping) or set(data) != _PAYLOAD_KEYS:
        return "data"
    if occurrence.get("cursor") is not None:
        return "cursor"
    if not isinstance(occurrence.get("timestamp"), str):
        return "timestamp"
    return None


# --------------------------------------------------------------------------- dispatch decisions


class DispatchDecision(StrEnum):
    SEND = "send"
    SKIP_INACTIVE = "skip_inactive"
    SKIP_DUPLICATE = "skip_duplicate"  # (subscription_id, event_id) already delivered
    SKIP_IN_FLIGHT = "skip_in_flight"
    HOLD_UNCERTAIN = "hold_uncertain"  # reconcile first (spec 13); never blind resend
    SKIP_FINAL = "skip_final"  # dead-lettered/cancelled/blocked delivery stays visible
    SKIP_FILTER_MISMATCH = "skip_filter_mismatch"
    SUPPRESS_STALE = "suppress_stale"  # newer case version or no longer pending


def decide_dispatch(
    signal: ReviewPendingSignal,
    target: DeliveryTarget,
    *,
    existing_state: OutboxState | None,
    current_case_version: int,
    current_case_state: ReviewState,
    now: datetime,
) -> DispatchDecision:
    """Decide whether a committed event may be (re)sent to one subscription.

    Handles duplicates (unique delivery record per (subscription_id, event_id)),
    out-of-order events (an older case version is suppressed) and stale
    decisions (the case is no longer pending at dispatch time).
    """
    if delivery_block_reason(target, now) is not None:
        return DispatchDecision.SKIP_INACTIVE
    if existing_state is OutboxState.DELIVERED:
        return DispatchDecision.SKIP_DUPLICATE
    if existing_state is OutboxState.SENDING:
        return DispatchDecision.SKIP_IN_FLIGHT
    if existing_state is OutboxState.UNCERTAIN:
        return DispatchDecision.HOLD_UNCERTAIN
    if existing_state in {OutboxState.DEAD_LETTER, OutboxState.CANCELLED, OutboxState.BLOCKED}:
        return DispatchDecision.SKIP_FINAL
    if not matches_filters(target.arguments, signal):
        return DispatchDecision.SKIP_FILTER_MISMATCH
    if current_case_version != signal.case_version or current_case_state is not ReviewState.PENDING:
        return DispatchDecision.SUPPRESS_STALE
    return DispatchDecision.SEND


# --------------------------------------------------------------------------- delivery


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Draft guidance: 3-5 attempts over at most 10-15 minutes."""

    max_attempts: int = 5
    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(minutes=5)
    max_total: timedelta = timedelta(minutes=15)
    max_retry_after: timedelta = timedelta(minutes=15)
    request_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay <= timedelta(0) or self.max_delay < self.base_delay:
            raise ValueError("invalid retry policy")


DEFAULT_RETRY_POLICY: Final = RetryPolicy()


def parse_retry_after(value: str | None, now: datetime) -> timedelta | None:
    """Parse `Retry-After` (delta-seconds or HTTP-date). Garbage or negative -> None."""
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    if text.isdigit():
        seconds = int(text)
        return timedelta(seconds=seconds) if seconds <= 7 * 24 * 3600 else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    delta = when - ensure_utc(now)
    return delta if delta > timedelta(0) else timedelta(0)


def backoff_delay(attempt: int, policy: RetryPolicy, rng: random.Random) -> timedelta:
    """Exponential backoff with equal jitter: half fixed, half random (attempt is 1-based)."""
    exponent = min(max(attempt - 1, 0), 30)
    ceiling = min(policy.max_delay, policy.base_delay * (1 << exponent))
    half = ceiling / 2
    return half + timedelta(seconds=rng.uniform(0, half.total_seconds()))


class DeliveryOutcomeKind(StrEnum):
    DELIVERED = "delivered"  # 2xx receipt; NOT review completion
    RETRY = "retry"
    FAILED = "failed"  # terminal for this delivery only; the subscription stays
    UNCERTAIN = "uncertain"  # receiver may have accepted it: reconcile, do not blindly resend
    DEAD_LETTER = "dead_letter"  # retryable failure but attempts/time window exhausted
    SKIPPED = "skipped"  # nothing was sent


class DeliveryFailureReason(StrEnum):
    HTTP_REDIRECT_REFUSED = "http_redirect_refused"
    HTTP_4XX = "http_4xx"
    HTTP_410_GONE = "http_410_gone"
    HTTP_413_TOO_LARGE = "http_413_too_large"
    HTTP_RETRYABLE_4XX = "http_retryable_4xx"  # 408, 425, 429
    HTTP_5XX = "http_5xx"
    HTTP_UNEXPECTED_STATUS = "http_unexpected_status"
    TIMEOUT = "timeout"
    TIMEOUT_AFTER_SEND = "timeout_after_send"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_LOST = "connection_lost"
    PROTOCOL_ERROR = "protocol_error"
    TLS_ERROR = "tls_error"
    DESTINATION_REJECTED = "destination_rejected"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INVALID_OCCURRENCE = "invalid_occurrence"
    ACCESS_REVOKED = "access_revoked"
    ACCESS_CHECK_UNAVAILABLE = "access_check_unavailable"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    kind: DeliveryOutcomeKind
    subscription_id: str
    webhook_id: str | None
    attempt: int
    status_code: int | None = None
    reason: DeliveryFailureReason | None = None
    block_reason: BlockReason | None = None
    wire_error: CallbackErrorReason | None = None  # for draft deliveryStatus.lastError
    retry_after: timedelta | None = None
    next_attempt_at: datetime | None = None
    send_attempted_at: datetime | None = None
    provider_accepted_at: datetime | None = None  # receipt time, never "dot reviewed it"
    elapsed: timedelta | None = None
    revoke_subscription: bool = False  # membership/scope recheck failed: stop the subscription

    @property
    def retryable(self) -> bool:
        return self.kind is DeliveryOutcomeKind.RETRY


_RETRYABLE_4XX: Final = frozenset({408, 425, 429})
_RETRY_AFTER_STATUSES: Final = frozenset({429, 502, 503, 504})


async def deliver(
    occurrence: Mapping[str, Any],
    target: DeliveryTarget,
    http: SafeHttp,
    *,
    attempt: int,
    clock: Clock,
    access_check: AccessCheck,
    rng: random.Random | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    first_attempt_at: datetime | None = None,
) -> DeliveryOutcome:
    """Send one occurrence to one subscription and classify the result.

    Signs fresh on every call (new `webhook-timestamp` and signature) while the
    `webhook-id` stays the occurrence `eventId`. The caller records the outcome
    on the unique (subscription_id, event_id) delivery row.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    rng = rng if rng is not None else random.Random()  # noqa: S311 - jitter, not cryptography
    now = ensure_utc(clock.now())
    raw_id = occurrence.get("eventId")
    webhook_id = raw_id if isinstance(raw_id, str) else None
    base: dict[str, Any] = {
        "subscription_id": target.subscription_id,
        "webhook_id": webhook_id,
        "attempt": attempt,
    }

    block = delivery_block_reason(target, now)
    if block is not None:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.SKIPPED,
            reason=DeliveryFailureReason.SUBSCRIPTION_INACTIVE,
            block_reason=block,
            **base,
        )
    problem = _occurrence_problem(occurrence, target)
    if problem is not None or webhook_id is None:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.FAILED, reason=DeliveryFailureReason.INVALID_OCCURRENCE, **base
        )
    try:
        allowed = await access_check(target)
    except Exception:  # fail closed: no delivery without a positive membership/scope check
        return _retry_or_dead_letter(
            base,
            reason=DeliveryFailureReason.ACCESS_CHECK_UNAVAILABLE,
            wire=None,
            status=None,
            retry_after=None,
            now=now,
            policy=retry_policy,
            rng=rng,
            first_attempt_at=first_attempt_at,
            send_attempted_at=None,
            elapsed=None,
        )
    if not allowed:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.SKIPPED,
            reason=DeliveryFailureReason.ACCESS_REVOKED,
            revoke_subscription=True,
            **base,
        )
    try:
        signed = build_signed_request(target.secrets, webhook_id, target.subscription_id, occurrence, now=now)
    except WebhookPayloadTooLarge:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.FAILED, reason=DeliveryFailureReason.PAYLOAD_TOO_LARGE, **base
        )
    try:
        response = await http.post(
            target.callback_url,
            content=signed.body,
            headers=signed.headers,
            timeout_s=retry_policy.request_timeout_s,
            max_response_bytes=4096,
        )
    except SafeHttpError as exc:
        return _classify_transport_error(
            exc, base, now=now, policy=retry_policy, rng=rng, first_attempt_at=first_attempt_at, clock=clock
        )
    finished = ensure_utc(clock.now())
    return _classify_status(
        response,
        base,
        now=now,
        finished=finished,
        policy=retry_policy,
        rng=rng,
        first_attempt_at=first_attempt_at,
    )


def _retry_or_dead_letter(
    base: Mapping[str, Any],
    *,
    reason: DeliveryFailureReason,
    wire: CallbackErrorReason | None,
    status: int | None,
    retry_after: timedelta | None,
    now: datetime,
    policy: RetryPolicy,
    rng: random.Random,
    first_attempt_at: datetime | None,
    send_attempted_at: datetime | None,
    elapsed: timedelta | None,
) -> DeliveryOutcome:
    common: dict[str, Any] = {
        **base,
        "status_code": status,
        "reason": reason,
        "wire_error": wire,
        "retry_after": retry_after,
        "send_attempted_at": send_attempted_at,
        "elapsed": elapsed,
    }
    attempt = int(base["attempt"])
    if attempt >= policy.max_attempts:
        return DeliveryOutcome(kind=DeliveryOutcomeKind.DEAD_LETTER, **common)
    if retry_after is not None and retry_after > policy.max_retry_after:
        return DeliveryOutcome(kind=DeliveryOutcomeKind.DEAD_LETTER, **common)
    delay = backoff_delay(attempt, policy, rng)
    if retry_after is not None:
        delay = max(delay, retry_after)
    next_at = now + delay
    started = ensure_utc(first_attempt_at) if first_attempt_at is not None else now
    if next_at > started + policy.max_total:
        return DeliveryOutcome(kind=DeliveryOutcomeKind.DEAD_LETTER, **common)
    return DeliveryOutcome(kind=DeliveryOutcomeKind.RETRY, next_attempt_at=next_at, **common)


_TRANSPORT_CLASSIFICATION: Final = {
    SafeHttpFailure.CONNECT_FAILED: (
        DeliveryFailureReason.CONNECTION_REFUSED,
        CallbackErrorReason.CONNECTION_REFUSED,
    ),
    SafeHttpFailure.TLS_FAILED: (DeliveryFailureReason.TLS_ERROR, CallbackErrorReason.TLS_ERROR),
    SafeHttpFailure.TIMEOUT_BEFORE_SEND: (DeliveryFailureReason.TIMEOUT, CallbackErrorReason.TIMEOUT),
    SafeHttpFailure.TIMEOUT_AFTER_SEND: (
        DeliveryFailureReason.TIMEOUT_AFTER_SEND,
        CallbackErrorReason.TIMEOUT,
    ),
    SafeHttpFailure.CONNECTION_LOST: (
        DeliveryFailureReason.CONNECTION_LOST,
        CallbackErrorReason.CONNECTION_REFUSED,
    ),
    SafeHttpFailure.PROTOCOL_ERROR: (
        DeliveryFailureReason.PROTOCOL_ERROR,
        CallbackErrorReason.CONNECTION_REFUSED,
    ),
}


def _classify_transport_error(
    exc: SafeHttpError,
    base: Mapping[str, Any],
    *,
    now: datetime,
    policy: RetryPolicy,
    rng: random.Random,
    first_attempt_at: datetime | None,
    clock: Clock,
) -> DeliveryOutcome:
    elapsed = ensure_utc(clock.now()) - now
    if exc.failure is SafeHttpFailure.REDIRECT_REFUSED:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.FAILED,
            status_code=exc.status_code,
            reason=DeliveryFailureReason.HTTP_REDIRECT_REFUSED,
            wire_error=CallbackErrorReason.HTTP_4XX,
            send_attempted_at=now,
            elapsed=elapsed,
            **base,
        )
    if exc.failure in {SafeHttpFailure.DESTINATION_REJECTED, SafeHttpFailure.REQUEST_TOO_LARGE}:
        reason = (
            DeliveryFailureReason.DESTINATION_REJECTED
            if exc.failure is SafeHttpFailure.DESTINATION_REJECTED
            else DeliveryFailureReason.PAYLOAD_TOO_LARGE
        )
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.FAILED,
            reason=reason,
            wire_error=(
                CallbackErrorReason.CONNECTION_REFUSED
                if reason is DeliveryFailureReason.DESTINATION_REJECTED
                else None
            ),
            elapsed=elapsed,
            **base,
        )
    if exc.status_code is not None:
        # The receiver's status line arrived before the body/deadline failed, so its answer
        # is known: a 2xx is a receipt, 410/413/other 4xx stay terminal (never re-sent via
        # the uncertain path), 408/425/429/5xx are retried.
        return _outcome_for_status(
            exc.status_code,
            None,
            base,
            now=now,
            finished=ensure_utc(clock.now()),
            elapsed=elapsed,
            policy=policy,
            rng=rng,
            first_attempt_at=first_attempt_at,
        )
    reason, wire = _TRANSPORT_CLASSIFICATION[exc.failure]
    if exc.possibly_delivered:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.UNCERTAIN,
            status_code=exc.status_code,
            reason=reason,
            wire_error=wire,
            send_attempted_at=now,
            elapsed=elapsed,
            **base,
        )
    return _retry_or_dead_letter(
        base,
        reason=reason,
        wire=wire,
        status=exc.status_code,
        retry_after=None,
        now=now,
        policy=policy,
        rng=rng,
        first_attempt_at=first_attempt_at,
        send_attempted_at=now,
        elapsed=elapsed,
    )


def _classify_status(
    response: SafeResponse,
    base: Mapping[str, Any],
    *,
    now: datetime,
    finished: datetime,
    policy: RetryPolicy,
    rng: random.Random,
    first_attempt_at: datetime | None,
) -> DeliveryOutcome:
    return _outcome_for_status(
        response.status_code,
        response.headers.get("retry-after"),
        base,
        now=now,
        finished=finished,
        elapsed=response.elapsed,
        policy=policy,
        rng=rng,
        first_attempt_at=first_attempt_at,
    )


def _outcome_for_status(
    status: int,
    retry_after_header: str | None,
    base: Mapping[str, Any],
    *,
    now: datetime,
    finished: datetime,
    elapsed: timedelta | None,
    policy: RetryPolicy,
    rng: random.Random,
    first_attempt_at: datetime | None,
) -> DeliveryOutcome:
    if 200 <= status < 300:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.DELIVERED,
            status_code=status,
            send_attempted_at=now,
            provider_accepted_at=finished,
            elapsed=elapsed,
            **base,
        )
    terminal: DeliveryFailureReason | None = None
    if status == 410:
        terminal = DeliveryFailureReason.HTTP_410_GONE
    elif status == 413:
        terminal = DeliveryFailureReason.HTTP_413_TOO_LARGE
    elif 300 <= status < 400:
        terminal = DeliveryFailureReason.HTTP_REDIRECT_REFUSED
    elif 400 <= status < 500 and status not in _RETRYABLE_4XX:
        terminal = DeliveryFailureReason.HTTP_4XX
    elif not 400 <= status < 600:
        terminal = DeliveryFailureReason.HTTP_UNEXPECTED_STATUS
    if terminal is not None:
        return DeliveryOutcome(
            kind=DeliveryOutcomeKind.FAILED,
            status_code=status,
            reason=terminal,
            wire_error=CallbackErrorReason.HTTP_5XX if 500 <= status < 600 else CallbackErrorReason.HTTP_4XX,
            send_attempted_at=now,
            elapsed=elapsed,
            **base,
        )
    retry_after = (
        parse_retry_after(retry_after_header, finished) if status in _RETRY_AFTER_STATUSES else None
    )
    reason = DeliveryFailureReason.HTTP_5XX if status >= 500 else DeliveryFailureReason.HTTP_RETRYABLE_4XX
    return _retry_or_dead_letter(
        base,
        reason=reason,
        wire=_status_reason(status),
        status=status,
        retry_after=retry_after,
        now=finished,
        policy=policy,
        rng=rng,
        first_attempt_at=first_attempt_at if first_attempt_at is not None else now,
        send_attempted_at=now,
        elapsed=elapsed,
    )


# --------------------------------------------------------------------------- uncertain deliveries


@dataclass(frozen=True, slots=True)
class UncertainPolicy:
    """Documented conservative rule for MCP Events (no provider lookup exists).

    The receiver deduplicates on `webhook-id` (Standard Webhooks idempotency key),
    so after a hold period the same eventId may be re-sent at most `max_resends`
    times; afterwards the delivery stays visibly `uncertain`.
    """

    hold_for: timedelta = timedelta(minutes=2)
    max_resends: int = 1


class UncertainDecision(StrEnum):
    HOLD = "hold"
    RESEND_SAME_EVENT_ID = "resend_same_event_id"
    KEEP_UNCERTAIN = "keep_uncertain"


def decide_uncertain_followup(
    *,
    uncertain_since: datetime,
    resends_done: int,
    now: datetime,
    policy: UncertainPolicy = UncertainPolicy(),  # noqa: B008 - frozen dataclass default
) -> UncertainDecision:
    if resends_done >= policy.max_resends:
        return UncertainDecision.KEEP_UNCERTAIN
    if ensure_utc(now) - ensure_utc(uncertain_since) < policy.hold_for:
        return UncertainDecision.HOLD
    return UncertainDecision.RESEND_SAME_EVENT_ID


# --------------------------------------------------------------------------- activation route


class ActivationRoute(StrEnum):
    NONE = "none"
    MCP_EVENTS = "mcp_events"
    SLACK = "slack"


class BridgeStatus(StrEnum):
    UNAVAILABLE = "unavailable"  # default: no supported wake route active
    CONFIGURED = "configured"  # route selected, end-to-end canary not recorded
    VERIFIED = "verified"  # EVENT_BRIDGE_VERIFIED_AT recorded after a canary


class ActivationRouteConflict(ValidationFailed):
    def __init__(self, message: str) -> None:
        super().__init__(message, details={"setting": "event_bridge"})


@dataclass(frozen=True, slots=True)
class ActivationSelection:
    route: ActivationRoute
    bridge_status: BridgeStatus
    blockers: tuple[str, ...]


def _parse_verified_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _route_blockers(settings: Settings, route: ActivationRoute) -> list[str]:
    """Configuration the selected route cannot work without (names only, never values)."""
    blockers: list[str] = []
    if route is ActivationRoute.MCP_EVENTS:
        key = settings.mcp_event_subscription_secret_encryption_key
        if key is None or not key.get_secret_value().strip():
            blockers.append("mcp_event_subscription_secret_encryption_key is not configured")
        else:
            try:
                parse_keyring(key.get_secret_value())
            except SecretBoxConfigError:
                blockers.append("mcp_event_subscription_secret_encryption_key is invalid")
    elif route is ActivationRoute.SLACK:
        if settings.notification_provider != "slack":
            blockers.append("notification_provider is not slack")
        for name, secret in (
            ("slack_bot_token", settings.slack_bot_token),
            ("slack_signing_secret", settings.slack_signing_secret),
        ):
            if secret is None or not secret.get_secret_value():
                blockers.append(f"{name} is not configured")
        if not settings.slack_channel_id:
            blockers.append("slack_channel_id is not configured")
    return blockers


def select_activation_route(settings: Settings, *, clock: Clock | None = None) -> ActivationSelection:
    """Pick the single activation route. Native MCP Events and Slack must never both be on.

    Raises `ActivationRouteConflict` for contradictory configuration instead of
    silently preferring one route (two routes would start duplicate review runs).
    A route whose required configuration is missing is reported as `none` /
    `unavailable` with the blockers. `verified` requires a recorded canary time
    (EVENT_BRIDGE_VERIFIED_AT) that parses as an aware timestamp not in the future.
    """
    native = (
        settings.mcp_events_enabled
        or settings.event_bridge_provider == "mcp_events"
        or settings.notification_provider == "mcp_events"
    )
    slack = settings.event_bridge_provider == "slack" or settings.notification_provider == "slack"
    if native and slack:
        raise ActivationRouteConflict("Native MCP Events and Slack cannot both be enabled")
    if settings.event_bridge_enabled and settings.event_bridge_provider == "disabled":
        raise ActivationRouteConflict("EVENT_BRIDGE_ENABLED requires EVENT_BRIDGE_PROVIDER")
    if settings.event_bridge_provider == "mcp_events" and not settings.mcp_events_enabled:
        raise ActivationRouteConflict("EVENT_BRIDGE_PROVIDER=mcp_events requires MCP_EVENTS_ENABLED")
    blockers: list[str] = []
    if not settings.allow_external_notifications:
        blockers.append("allow_external_notifications is false")
    if not settings.event_bridge_enabled:
        blockers.append("event_bridge_enabled is false")
    if blockers:
        return ActivationSelection(ActivationRoute.NONE, BridgeStatus.UNAVAILABLE, tuple(blockers))
    route = ActivationRoute(settings.event_bridge_provider)
    blockers = _route_blockers(settings, route)
    if blockers:
        return ActivationSelection(ActivationRoute.NONE, BridgeStatus.UNAVAILABLE, tuple(blockers))
    raw_verified = settings.event_bridge_verified_at
    verified_at = _parse_verified_at(raw_verified)
    now = ensure_utc((clock or SystemClock()).now())
    if verified_at is None:
        if raw_verified:
            blockers.append("event_bridge_verified_at is not an aware ISO 8601 timestamp")
        blockers.append("no recorded end-to-end canary (event_bridge_verified_at)")
        return ActivationSelection(route, BridgeStatus.CONFIGURED, tuple(blockers))
    if verified_at > now + timedelta(minutes=5):
        blockers.append("event_bridge_verified_at is in the future")
        return ActivationSelection(route, BridgeStatus.CONFIGURED, tuple(blockers))
    return ActivationSelection(route, BridgeStatus.VERIFIED, ())
