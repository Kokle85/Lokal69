"""ASGI middleware and the per-principal rate limiter of the dashboard API (spec 24, 30;
docs/api_contract.md section 1).

Order (outermost first; ``app.create_app`` builds it):

1. `RequestContextMiddleware`: request id (a valid client ``X-Request-Id`` -- printable ASCII,
   at most 200 characters -- or a fresh ``req-<hex>``), echoed as ``X-Request-Id``; security
   headers on every response; ``Cache-Control: no-store`` on ``/api``, ``/healthz`` and ``/readyz``
   (other paths keep their own cache header); request metrics with route *templates* only; one
   structured log line per request without query strings or headers; and a backstop that turns
   an exception escaping the outer layers into ``INTERNAL_ERROR`` without details.
2. Starlette ``TrustedHostMiddleware`` (strict ``Host`` allow-list).
3. `ApiCorsMiddleware`: Starlette ``CORSMiddleware`` for ``/api`` paths only, with the configured
   origin allow-list (no wildcard), ``GET``/``POST``/``OPTIONS``, the documented request headers
   (``Authorization``, ``Content-Type``, ``Idempotency-Key``, ``X-Request-Id``,
   ``X-Workspace-Id``), no credentials (bearer tokens only, no cookies), ``X-Request-Id`` and
   ``Retry-After`` exposed to the dashboard, and a 600 second preflight cache. The mounted MCP
   endpoint handles its own origin checks. Every layer below renders its errors INSIDE this one,
   so an allowed dashboard origin can read 413 and 500 bodies (and their correlation ids) too.
4. `InternalErrorMiddleware`: renders an unexpected exception as ``INTERNAL_ERROR`` without
   details and logs it server-side (through the redacting JSON logger).
5. `BodySizeLimitMiddleware`: ``Content-Length`` and streamed bytes are capped per path prefix
   (``413``); a repeated, non-numeric (ASCII digits only) or negative ``Content-Length`` is
   refused.

`PrincipalRateLimiter` is an in-memory token bucket per authenticated principal, with separate
buckets for mutations and reads; `PreAuthLimiter` is a per-client bucket of failed
authentications that refuses a flooding client before any token verification. Both are per
process: N replicas allow N times the budget, and a restart resets them. That is adequate for the
single-owner MVP; a shared limiter (database or proxy) is the upgrade path when the API is
replicated.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from suv_deals.api.errors import error_response, internal_error, new_request_id, payload_too_large
from suv_deals.clock import Clock
from suv_deals.errors import RateLimited
from suv_deals.observability.logging import bind_log_context, reset_log_context
from suv_deals.observability.metrics import AppMetrics, route_label
from suv_deals.views.common import is_valid_request_id

logger = logging.getLogger("suv_deals.api")

REQUEST_ID_HEADER: Final = "X-Request-Id"
API_PREFIX: Final = "/api"
#: The mailbox-worker API (``api.mail_worker_routes``); no CORS, its own body limit.
MAIL_WORKER_PATH_PREFIX: Final = "/v1/mail-workers"
OPERATIONAL_PATHS: Final = frozenset({"/healthz", "/readyz", "/metrics"})
CORS_ALLOWED_METHODS: Final = ("GET", "POST", "OPTIONS")
CORS_ALLOWED_HEADERS: Final = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-Request-Id",
    "X-Workspace-Id",
)
CORS_EXPOSED_HEADERS: Final = ("X-Request-Id", "Retry-After")
CORS_MAX_AGE_SECONDS: Final = 600
DEFAULT_API_BODY_LIMIT: Final = 64 * 1024
DEFAULT_OTHER_BODY_LIMIT: Final = 4 * 1024 * 1024

BASE_SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)
HSTS_VALUE: Final = "max-age=31536000; includeSubDomains"


def _header(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", ()) if key.lower() == name]


def is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def is_mail_worker_path(path: str) -> bool:
    return path == MAIL_WORKER_PATH_PREFIX or path.startswith(MAIL_WORKER_PATH_PREFIX + "/")


def _no_store_path(path: str) -> bool:
    return is_api_path(path) or is_mail_worker_path(path) or path in OPERATIONAL_PATHS


# --------------------------------------------------------------------------------------------
# Request context, security headers, metrics and the last-resort error handler
# --------------------------------------------------------------------------------------------


class RequestContextMiddleware:
    """See the module docstring (outermost middleware)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: AppMetrics,
        clock: Clock,
        hsts: bool = False,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.clock = clock
        self.monotonic = monotonic
        security = list(BASE_SECURITY_HEADERS)
        if hsts:
            security.append(("Strict-Transport-Security", HSTS_VALUE))
        self._security = tuple((k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = _header(scope, b"x-request-id")
        candidate = supplied[0].decode("latin-1") if len(supplied) == 1 else None
        request_id = (
            candidate if isinstance(candidate, str) and is_valid_request_id(candidate) else new_request_id()
        )
        scope.setdefault("state", {})["request_id"] = request_id
        path: str = scope.get("path", "")
        started = self.monotonic()
        status_holder = [500]
        response_started = [False]
        security = self._security
        no_store = _no_store_path(path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_started[0] = True
                status_holder[0] = int(message["status"])
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not (no_store and k.lower() in (b"cache-control", b"pragma"))
                    and k.lower() != b"x-request-id"
                ]
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in security if k not in present)
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                if no_store:
                    headers.append((b"cache-control", b"no-store"))
                    headers.append((b"pragma", b"no-cache"))
                elif b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        tokens = bind_log_context(request_id=request_id)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Backstop: normally `InternalErrorMiddleware` (inside the CORS layer) renders these.
            logger.exception("unhandled error while serving the request")
            if response_started[0]:
                raise
            response = error_response(
                internal_error(), request_id=request_id, clock=self.clock, metrics=self.metrics
            )
            await response(scope, receive, send_wrapper)
        finally:
            elapsed = self.monotonic() - started
            self._observe(scope, path, status_holder[0], elapsed)
            reset_log_context(tokens)

    def _observe(self, scope: Scope, path: str, status: int, elapsed: float) -> None:
        if not (is_api_path(path) or is_mail_worker_path(path) or path in OPERATIONAL_PATHS):
            return  # the mounted MCP endpoint records its own surface metrics
        route = scope.get("route")
        template = getattr(route, "path", None)
        label = template if isinstance(template, str) else route_label(path)
        self.metrics.observe_request("api", label, status, elapsed)
        logger.info(
            "request served",
            extra={
                "method": scope.get("method"),
                "route": label,
                "status": status,
                "ms": round(elapsed * 1000),
            },
        )


# --------------------------------------------------------------------------------------------
# Unexpected errors (inside the CORS layer)
# --------------------------------------------------------------------------------------------


class _BodyTooLarge(Exception):
    """Raised from `BodySizeLimitMiddleware`'s receive wrapper once the streamed body is too big."""


class InternalErrorMiddleware:
    """Renders an unexpected exception as ``INTERNAL_ERROR`` (no details, no traces) and logs it.

    It sits inside `ApiCorsMiddleware`, so the dashboard can read the error body. An exception
    raised after the response started cannot be rendered and propagates (the server closes the
    connection); ``_BodyTooLarge`` belongs to `BodySizeLimitMiddleware` and is re-raised.
    """

    def __init__(self, app: ASGIApp, *, metrics: AppMetrics, clock: Clock) -> None:
        self.app = app
        self.metrics = metrics
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = [False]

        async def tracking_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                started[0] = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except _BodyTooLarge:
            raise
        except Exception:
            logger.exception("unhandled error while serving the request")
            if started[0]:
                raise
            request_id = scope.get("state", {}).get("request_id")
            response = error_response(
                internal_error(), request_id=request_id, clock=self.clock, metrics=self.metrics
            )
            await response(scope, receive, send)


# --------------------------------------------------------------------------------------------
# Body size limits
# --------------------------------------------------------------------------------------------


class BodySizeLimitMiddleware:
    """Caps request bodies per path prefix (``Content-Length`` and streamed bytes)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_limit: int = DEFAULT_API_BODY_LIMIT,
        default_limit: int = DEFAULT_OTHER_BODY_LIMIT,
        prefix_limits: Mapping[str, int] | None = None,
        clock: Clock | None = None,
        metrics: AppMetrics | None = None,
    ) -> None:
        if api_limit < 1 or default_limit < 1:
            raise ValueError("body limits must be positive")
        self.app = app
        self.api_limit = api_limit
        self.default_limit = default_limit
        # Longest prefix first, so a narrower extension route can carry its own limit.
        self.prefix_limits = sorted((prefix_limits or {}).items(), key=lambda kv: len(kv[0]), reverse=True)
        self.clock = clock
        self.metrics = metrics

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.prefix_limits:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return limit
        return self.api_limit if is_api_path(path) else self.default_limit

    async def _reject(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        request_id = scope.get("state", {}).get("request_id")
        response = error_response(
            payload_too_large(limit), request_id=request_id, clock=self.clock, metrics=self.metrics
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        lengths = _header(scope, b"content-length")
        if lengths:
            text = lengths[0].decode("latin-1").strip()
            # ASCII digits only: str.isdigit() also accepts e.g. "²", which int() refuses.
            if len(lengths) > 1 or not (text.isascii() and text.isdigit()) or int(text) > limit:
                await self._reject(scope, receive, send, limit)
                return
        received = 0
        started = [False]

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                started[0] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if started[0]:
                raise
            await self._reject(scope, receive, send, limit)


# --------------------------------------------------------------------------------------------
# CORS for the dashboard API only
# --------------------------------------------------------------------------------------------


class ApiCorsMiddleware:
    """Starlette CORS for ``/api`` paths; other paths (the MCP mount) pass through untouched."""

    def __init__(self, app: ASGIApp, *, allow_origins: Sequence[str]) -> None:
        if any(origin == "*" for origin in allow_origins):
            raise ValueError("wildcard CORS origins are not allowed")
        self.app = app
        self.cors = CORSMiddleware(
            app,
            allow_origins=list(allow_origins),
            allow_methods=list(CORS_ALLOWED_METHODS),
            allow_headers=list(CORS_ALLOWED_HEADERS),
            allow_credentials=False,
            expose_headers=list(CORS_EXPOSED_HEADERS),
            max_age=CORS_MAX_AGE_SECONDS,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_api_path(scope.get("path", "")):
            await self.cors(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# --------------------------------------------------------------------------------------------
# Per-principal rate limiting
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Token bucket: ``capacity`` requests in a burst, refilled at ``per_seconds`` per request."""

    capacity: int
    per_seconds: float

    def __post_init__(self) -> None:
        if self.capacity < 1 or not (self.per_seconds > 0 and math.isfinite(self.per_seconds)):
            raise ValueError("a rate limit needs a positive capacity and refill interval")


DEFAULT_MUTATION_LIMIT: Final = RateLimit(capacity=30, per_seconds=2.0)  # 30 burst, 30/minute
DEFAULT_READ_LIMIT: Final = RateLimit(capacity=120, per_seconds=0.25)  # 120 burst, 240/minute


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class PrincipalRateLimiter:
    """In-memory token buckets per (principal, mutation|read); bounded number of tracked keys."""

    def __init__(
        self,
        *,
        mutations: RateLimit = DEFAULT_MUTATION_LIMIT,
        reads: RateLimit = DEFAULT_READ_LIMIT,
        max_principals: int = 10_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_principals < 1:
            raise ValueError("max_principals must be positive")
        self._limits = {True: mutations, False: reads}
        self._buckets: OrderedDict[tuple[UUID, bool], _Bucket] = OrderedDict()
        self._max = max_principals * 2
        self._monotonic = monotonic
        self._lock = threading.Lock()

    def check(self, principal_id: UUID, *, mutation: bool) -> None:
        """Take one token or raise ``RATE_LIMITED`` with the whole seconds until the next one."""
        limit = self._limits[mutation]
        key = (principal_id, mutation)
        now = self._monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit.capacity), updated=now)
                self._buckets[key] = bucket
                while len(self._buckets) > self._max:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
                elapsed = max(now - bucket.updated, 0.0)
                bucket.tokens = min(float(limit.capacity), bucket.tokens + elapsed / limit.per_seconds)
                bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return
            wait = (1.0 - bucket.tokens) * limit.per_seconds
        raise RateLimited("Too many requests; slow down", retry_after_seconds=max(1, math.ceil(wait)))


# --------------------------------------------------------------------------------------------
# Per-client limiter for unauthenticated / invalid-credential requests
# --------------------------------------------------------------------------------------------

#: Failed authentications a client may cause before it is refused BEFORE verification: 20 in a
#: burst, then one more every 3 seconds (20/minute).
DEFAULT_PREAUTH_LIMIT: Final = RateLimit(capacity=20, per_seconds=3.0)
UNKNOWN_CLIENT: Final = "unknown"


#: An IPv6 client is limited per /64: one subscriber or host typically controls a whole /64
#: (SLAAC privacy addresses rotate inside it), so per-address buckets would be no limit at all.
IPV6_CLIENT_PREFIX: Final = 64


def client_address(scope: Scope) -> str:
    """The ASGI client host (the proxy's address unless the server trusts proxy headers)."""
    client = scope.get("client")
    host = client[0] if isinstance(client, list | tuple) and client else None
    return host[:64] if isinstance(host, str) and host else UNKNOWN_CLIENT


def limiter_key(client: str) -> str:
    """The bucket a client shares in `PreAuthLimiter`: an IPv4 address as it is, an IPv4-mapped
    IPv6 address as its IPv4 address, any other IPv6 address as its ``/64`` network (zone id and
    spelling ignored); anything that is not an IP address (a test client label) unchanged."""
    try:
        address = ipaddress.ip_address(client.split("%", 1)[0].strip())
    except ValueError:
        return client
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return str(ipaddress.IPv6Network((address, IPV6_CLIENT_PREFIX), strict=False))
    return str(address)


class PreAuthLimiter:
    """Cheap per-client limiter in front of token verification (``/api``, ``/mcp``,
    ``/v1/mail-workers``).

    Each missing, malformed, wrongly signed, unknown, revoked or expired credential takes one
    token from the client's bucket (`failed`); while a bucket is empty, `check` refuses the
    client's requests with ``RATE_LIMITED`` BEFORE any signature check, key fetch or database
    lookup, so a credential-guessing flood costs a dictionary lookup per request. A successful
    authentication takes nothing. Like `PrincipalRateLimiter` it is per process, bounded in the
    number of tracked clients, and keyed by the ASGI client address (behind a reverse proxy run
    uvicorn with ``--proxy-headers`` and a trusted ``--forwarded-allow-ips``, or every client
    shares the proxy's bucket); IPv6 clients share one bucket per ``/64`` (`limiter_key`), so
    rotating addresses inside one's own prefix buys no new budget.
    """

    def __init__(
        self,
        *,
        limit: RateLimit = DEFAULT_PREAUTH_LIMIT,
        max_clients: int = 10_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        self._limit = limit
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._max = max_clients
        self._monotonic = monotonic
        self._lock = threading.Lock()

    def _refill(self, client: str, now: float) -> _Bucket | None:
        bucket = self._buckets.get(client)
        if bucket is not None:
            elapsed = max(now - bucket.updated, 0.0)
            bucket.tokens = min(
                float(self._limit.capacity), bucket.tokens + elapsed / self._limit.per_seconds
            )
            bucket.updated = now
            if bucket.tokens >= float(self._limit.capacity):
                del self._buckets[client]  # fully refilled: forget the client
                return None
        return bucket

    def check(self, client: str) -> None:
        """Refuse (``RATE_LIMITED``) a client that has no failed-authentication budget left."""
        client = limiter_key(client)
        now = self._monotonic()
        with self._lock:
            bucket = self._refill(client, now)
            if bucket is None or bucket.tokens >= 1.0:
                return
            wait = (1.0 - bucket.tokens) * self._limit.per_seconds
        raise RateLimited(
            "Too many failed authentication attempts; slow down", retry_after_seconds=max(1, math.ceil(wait))
        )

    def failed(self, client: str) -> None:
        """Record one failed authentication of ``client`` (never raises)."""
        client = limiter_key(client)
        now = self._monotonic()
        with self._lock:
            bucket = self._refill(client, now)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._limit.capacity), updated=now)
                self._buckets[client] = bucket
                while len(self._buckets) > self._max:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(client)
            bucket.tokens = max(bucket.tokens - 1.0, 0.0)

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._buckets)


__all__ = [
    "CORS_ALLOWED_HEADERS",
    "CORS_ALLOWED_METHODS",
    "CORS_MAX_AGE_SECONDS",
    "DEFAULT_API_BODY_LIMIT",
    "DEFAULT_MUTATION_LIMIT",
    "DEFAULT_OTHER_BODY_LIMIT",
    "DEFAULT_PREAUTH_LIMIT",
    "DEFAULT_READ_LIMIT",
    "IPV6_CLIENT_PREFIX",
    "MAIL_WORKER_PATH_PREFIX",
    "REQUEST_ID_HEADER",
    "ApiCorsMiddleware",
    "BodySizeLimitMiddleware",
    "InternalErrorMiddleware",
    "PreAuthLimiter",
    "PrincipalRateLimiter",
    "RateLimit",
    "RequestContextMiddleware",
    "client_address",
    "is_api_path",
    "is_mail_worker_path",
    "limiter_key",
]
