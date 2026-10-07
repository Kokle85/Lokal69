"""MCP Events for ``review.pending.v1``: ``events/list``, ``events/subscribe``, ``events/unsubscribe``.

Wire facts follow docs/research/mcp_events_and_webhooks.md (verified) and the protocol rules in
``integrations.event_bridge``; this module wires them to the authenticated MCP endpoint and to
``persistence.subscriptions_repo``. Only registered when ``MCP_EVENTS_ENABLED=true``.

SDK integration (``mcp`` 2.3.0 has no events support):

- `EventsExtension` (an SDK ``Extension``) serves the three methods as ``MethodBinding``s
  (custom methods skip the spec result sieve; the runner adds ``resultType`` and ``_meta``).
  `install_extension` applies it to the low-level ``Server`` exactly as ``MCPServer`` does
  (handler registration plus ``capabilities.extensions[<id>]``).
- `DiscoverEventsCapability` is a ``ServerMiddleware`` that adds ``capabilities.events = {}`` to the
  already-serialized ``server/discover`` (and legacy ``initialize``) result, because the SDK's
  ``ServerCapabilities`` silently drops unknown keys.

Methods (the principal always comes from the verified token, never from params):

``events/list``
    The ``review.pending.v1`` descriptor (``delivery: ["webhook"]``, closed ``inputSchema`` with
    the permitted profiles, minimal ``payloadSchema``), only for principals holding
    ``reviews:read`` + ``events:subscribe`` whose access the dispatcher can recheck before each
    delivery (`can_hold_subscriptions`); otherwise ``{"events": []}``.
``events/subscribe``
    ``event_bridge.validate_subscribe_params`` (authorization, name, closed arguments,
    ``delivery.mode == "webhook"``, an https/443 public callback, a client-supplied ``whsec_``
    secret of 24-64 bytes, TTL rules) -> store or refresh the subscription with the secret
    sealed by ``SecretBox`` (``MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY``) in one short
    transaction -> unless a bounded verification for this principal + callback + secret is
    still valid, run the signed single-use callback challenge through ``integrations.safe_http``
    (outside any transaction) and record the outcome -> ``{id, refreshBefore, cursor: null,
    truncated: false}`` with a finite ``refreshBefore``. A failed challenge is ``-32015`` with
    ``data.reason``. Outbound challenges need ``ALLOW_EXTERNAL_NOTIFICATIONS=true``; otherwise
    the server answers ``-32014`` (webhook delivery unsupported here) without contacting anyone.
``events/unsubscribe``
    Idempotent; always ``{}`` (also when nothing matched).

Errors are JSON-RPC errors (``mcp.MCPError``): ``-32602`` invalid params, ``-32011`` unknown
event, ``-32012`` forbidden, ``-32013`` quota/verification rate, ``-32014`` unsupported,
``-32015`` callback endpoint error; anything else is ``-32603`` with a correlation id only.
Secrets and callback URLs are never logged or returned.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, TypeVar

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError

from suv_deals.clock import Clock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import ProfileKey
from suv_deals.errors import AppError, ErrorCode
from suv_deals.integrations.event_bridge import (
    DEFAULT_POLICY,
    PERMITTED_PROFILES,
    ChallengeLedger,
    EventsProtocolError,
    SubscriptionPolicy,
    SubscriptionRequest,
    SubscriptionStatus,
    VerificationCacheEntry,
    VerificationRateLimiter,
    VerificationResult,
    can_subscribe,
    forbidden,
    invalid_params,
    list_events,
    needs_verification,
    not_found,
    resource_exhausted,
    run_verification_challenge,
    unsubscribe_result,
    unsupported,
    validate_subscribe_params,
    validate_unsubscribe_params,
)
from suv_deals.integrations.safe_http import SafeHttp
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.mcp.auth import McpPrincipal, current_principal
from suv_deals.mcp.tools import ToolRateLimiter, request_id_for
from suv_deals.observability.logging import log_context
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import subscriptions_repo
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings

logger = logging.getLogger("suv_deals.mcp.events")

EXTENSION_ID: Final = "dev.suv-deals/review-events"
EVENTS_CAPABILITY: Final = "events"
LIST_METHOD: Final = "events/list"
SUBSCRIBE_METHOD: Final = "events/subscribe"
UNSUBSCRIBE_METHOD: Final = "events/unsubscribe"
TRANSACTION_ATTEMPTS: Final = 3
MAX_CACHED_VERIFICATIONS: Final = 10_000
_PATCHED_METHODS: Final = frozenset({"server/discover", "initialize"})

T = TypeVar("T")


class EventsListParams(types.RequestParams):
    """``events/list`` params; the raw mapping (``ctx.params``) is validated by the event bridge."""


class EventsSubscribeParams(types.RequestParams):
    """``events/subscribe`` params; validated from the raw mapping so an omitted ``ttlMs`` differs
    from ``ttlMs: null``."""


class EventsUnsubscribeParams(types.RequestParams):
    """``events/unsubscribe`` params (validated by the event bridge)."""


def permitted_profiles(settings: Settings) -> tuple[str, ...]:
    """Profiles a subscription may monitor: ``primary`` plus the optional profiles enabled here."""
    enabled = {ProfileKey.PRIMARY.value}
    if settings.manual_4000_profile_enabled:
        enabled.add(ProfileKey.MANUAL_4000.value)
    if settings.below_target_watch_enabled:
        enabled.add(ProfileKey.BELOW_TARGET_WATCH.value)
    return tuple(p for p in PERMITTED_PROFILES if p in enabled)


@dataclass
class VerificationCache:
    """Successful callback verifications per (principal, workspace, callback URL), process-local
    and bounded; `event_bridge.needs_verification` decides whether an entry still applies."""

    max_entries: int = MAX_CACHED_VERIFICATIONS
    _entries: OrderedDict[str, VerificationCacheEntry] = field(default_factory=OrderedDict)

    def get(self, key: str) -> VerificationCacheEntry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, entry: VerificationCacheEntry) -> None:
        self._entries[entry.cache_key] = entry
        self._entries.move_to_end(entry.cache_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


def _strip_meta(params: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in dict(params or {}).items() if k != "_meta"}


def can_hold_subscriptions(principal: McpPrincipal) -> bool:
    """Whether the dispatcher can recheck this principal's access before every delivery
    (``subscriptions_repo.check_subscriber_access``): a workspace member, or a principal
    behind a stored API credential. An OAuth machine client mapped only in configuration has
    neither, so its subscription would be revoked at the first dispatch without ever
    delivering; it is not offered the event and its subscribe is ``-32012``."""
    return principal.principal_kind == "user" or principal.credential_id is not None


def to_mcp_error(error: AppError, correlation_id: str | None = None) -> MCPError:
    """JSON-RPC error for an ``events/*`` failure (draft codes; never secrets or raw responses)."""
    if not isinstance(error, EventsProtocolError):
        error = _protocol_error(error)
    if isinstance(error, EventsProtocolError):
        return MCPError(code=int(error.rpc_code), message=error.message, data=error.rpc_data)
    data: dict[str, Any] = {"retryable": error.retryable}
    if correlation_id:
        data["correlation_id"] = correlation_id
    return MCPError(code=types.INTERNAL_ERROR, message="Internal error", data=data)


def _protocol_error(error: AppError) -> AppError:
    match error.code:
        case ErrorCode.FORBIDDEN | ErrorCode.UNAUTHENTICATED:
            return forbidden()
        case ErrorCode.VALIDATION_ERROR:
            return invalid_params("invalid subscription request")
        case ErrorCode.NOT_FOUND:
            return not_found("subscription")
        case ErrorCode.RATE_LIMITED:
            return resource_exhausted("requests")
        case _:
            return error


@dataclass(slots=True)
class EventServices:
    """What the events methods need; one instance per MCP server."""

    settings: Settings
    db: Database
    box: SecretBox
    http: SafeHttp
    clock: Clock
    metrics: AppMetrics
    limiter: ToolRateLimiter
    policy: SubscriptionPolicy = DEFAULT_POLICY
    ledger: ChallengeLedger = field(default_factory=ChallengeLedger)
    verification_limiter: VerificationRateLimiter = field(default_factory=VerificationRateLimiter)
    verifications: VerificationCache = field(default_factory=VerificationCache)
    profiles: Sequence[str] | None = None

    def permitted_profiles(self) -> tuple[str, ...]:
        return tuple(self.profiles) if self.profiles is not None else permitted_profiles(self.settings)


class EventsService:
    """Handlers of the three ``events/*`` methods (module docstring)."""

    def __init__(self, services: EventServices) -> None:
        self.services = services

    # ---------------------------------------------------------------- helpers

    def _actor(self, ctx: ServerRequestContext[Any, Any]) -> tuple[McpPrincipal, ActorContext]:
        principal = current_principal()
        if principal is None:
            raise forbidden("webhook subscriptions require an authenticated principal")
        return principal, principal.actor(request_id_for(ctx))

    async def _in_transaction(self, actor: ActorContext, work: Callable[[Conn], Awaitable[T]]) -> T:
        async def attempt() -> T:
            async with unit_of_work(self.services.db, actor) as conn:
                return await work(conn)

        return await retry_transient(attempt, attempts=TRANSACTION_ATTEMPTS)

    async def _guarded(
        self, ctx: ServerRequestContext[Any, Any], method: str, run: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        request_id = request_id_for(ctx)
        with log_context(request_id=request_id):
            try:
                return await run()
            except AppError as exc:
                if not isinstance(exc, EventsProtocolError):
                    logger.info(
                        "mcp events request refused", extra={"method": method, "code": exc.code.value}
                    )
                raise to_mcp_error(exc, request_id) from None
            except MCPError:
                raise
            except Exception:
                logger.exception("mcp events request failed", extra={"method": method})
                raise MCPError(
                    code=types.INTERNAL_ERROR,
                    message="Internal error",
                    data={"retryable": True, "correlation_id": request_id},
                ) from None

    # ---------------------------------------------------------------- events/list

    async def handle_list(
        self, ctx: ServerRequestContext[Any, Any], params: EventsListParams
    ) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            principal, actor = self._actor(ctx)
            listed = list_events(
                actor, _strip_meta(ctx.params), permitted_profiles=self.services.permitted_profiles()
            )
            return listed if can_hold_subscriptions(principal) else {"events": []}

        return await self._guarded(ctx, LIST_METHOD, run)

    # ---------------------------------------------------------------- events/subscribe

    async def handle_subscribe(
        self, ctx: ServerRequestContext[Any, Any], params: EventsSubscribeParams
    ) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            principal, actor = self._actor(ctx)
            services = self.services
            if not can_subscribe(actor):
                # Authorization first (draft -32012), before any other answer about this server.
                raise forbidden("requires reviews:read and events:subscribe")
            if not can_hold_subscriptions(principal):
                raise forbidden("this principal's access cannot be rechecked before delivery")
            if not services.settings.allow_external_notifications:
                # The callback challenge is outbound traffic: refused before validation contacts anyone.
                raise unsupported("deliveryMode", "webhook")
            request = validate_subscribe_params(
                _strip_meta(ctx.params),
                actor,
                clock=services.clock,
                policy=services.policy,
                permitted_profiles=services.permitted_profiles(),
            )
            services.limiter.check(actor, "events.subscribe")
            outcome = await self._in_transaction(
                actor,
                lambda conn: subscriptions_repo.create_or_refresh_subscription(
                    conn,
                    actor,
                    request,
                    services.box,
                    policy=services.policy,
                    credential_id=principal.credential_id,
                ),
            )
            record = outcome.record
            if self._must_verify(request, record.status, record.verified_at):
                result = await self._verify(request)
                await self._in_transaction(
                    actor,
                    lambda conn: subscriptions_repo.record_verification(
                        conn, actor, record.id, result, services.box
                    ),
                )
                result.raise_for_failure()
            return request.result()

        return await self._guarded(ctx, SUBSCRIBE_METHOD, run)

    def _must_verify(
        self, request: SubscriptionRequest, status: SubscriptionStatus, verified_at: datetime | None
    ) -> bool:
        if status is not SubscriptionStatus.ACTIVE or verified_at is None:
            return True
        entry = VerificationCacheEntry(
            cache_key=request.verification_cache_key,
            verified_at=verified_at,
            secret_fingerprint=request.secret.fingerprint,
        )
        return self._stale(request, entry)

    def _stale(self, request: SubscriptionRequest, entry: VerificationCacheEntry | None) -> bool:
        return needs_verification(
            entry,
            principal_id=request.principal_id,
            callback_url=request.callback_url,
            secret=request.secret,
            now=self.services.clock.now(),
            workspace_id=request.workspace_id,
            policy=self.services.policy,
        )

    async def _verify(self, request: SubscriptionRequest) -> VerificationResult:
        services = self.services
        cached = services.verifications.get(request.verification_cache_key)
        if cached is not None and not self._stale(request, cached):
            now = ensure_utc(services.clock.now())
            return VerificationResult(
                ok=True,
                reason=None,
                detail="cached",
                status_code=None,
                webhook_id="msg_verification_cached",
                attempted_at=now,
                verified_at=ensure_utc(cached.verified_at),
                secret_fingerprint=request.secret.fingerprint,
            )
        result = await run_verification_challenge(
            request.callback_url,
            request.secret,
            request.subscription_id,
            services.http,
            clock=services.clock,
            ledger=services.ledger,
            rate_limiter=services.verification_limiter,
            policy=services.policy,
        )
        services.metrics.record_verification("ok" if result.ok else (result.reason or "challenge_failed"))
        if result.ok and result.verified_at is not None:
            services.verifications.put(
                VerificationCacheEntry(
                    cache_key=request.verification_cache_key,
                    verified_at=result.verified_at,
                    secret_fingerprint=result.secret_fingerprint,
                )
            )
        else:
            logger.info(
                "mcp events callback verification failed",
                extra={"reason": None if result.reason is None else result.reason.value},
            )
        return result

    # ---------------------------------------------------------------- events/unsubscribe

    async def handle_unsubscribe(
        self, ctx: ServerRequestContext[Any, Any], params: EventsUnsubscribeParams
    ) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            _, actor = self._actor(ctx)
            request = validate_unsubscribe_params(_strip_meta(ctx.params), actor)
            self.services.limiter.check(actor, "events.unsubscribe")  # a locking write per call
            await self._in_transaction(
                actor, lambda conn: subscriptions_repo.unsubscribe(conn, actor, request)
            )
            return unsubscribe_result()

        return await self._guarded(ctx, UNSUBSCRIBE_METHOD, run)


class EventsExtension(Extension):
    """The ``events/*`` methods as an SDK extension (applied with `install_extension`)."""

    identifier = EXTENSION_ID

    def __init__(self, service: EventsService) -> None:
        self.service = service

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding(LIST_METHOD, EventsListParams, self.service.handle_list),
            MethodBinding(SUBSCRIBE_METHOD, EventsSubscribeParams, self.service.handle_subscribe),
            MethodBinding(UNSUBSCRIBE_METHOD, EventsUnsubscribeParams, self.service.handle_unsubscribe),
        )


def install_extension(server: Server[Any], extension: Extension) -> None:
    """Apply an extension's methods to a low-level ``Server`` (what ``MCPServer`` does for its
    own extensions): register each binding and advertise ``capabilities.extensions[<id>]``."""
    for binding in extension.methods():
        if server.get_request_handler(binding.method) is not None:
            raise ValueError(f"method {binding.method!r} is already registered")
        if binding.protocol_versions is not None:
            raise ValueError("version-gated extension methods are not supported here")
        server.add_request_handler(binding.method, binding.params_type, binding.handler)
    server.extensions[extension.identifier] = extension.settings()


class DiscoverEventsCapability:
    """``ServerMiddleware``: add ``capabilities.events = {}`` to the serialized discover result."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method in _PATCHED_METHODS and isinstance(result, dict):
            capabilities = result.get("capabilities")
            if isinstance(capabilities, dict):
                result["capabilities"] = {**capabilities, EVENTS_CAPABILITY: {}}
        return result


def install_events(server: Server[Any], services: EventServices) -> EventsService:
    """Register the events methods and the discover capability patch on ``server``."""
    service = EventsService(services)
    install_extension(server, EventsExtension(service))
    server.middleware.append(DiscoverEventsCapability())
    return service


__all__ = [
    "EVENTS_CAPABILITY",
    "EXTENSION_ID",
    "LIST_METHOD",
    "SUBSCRIBE_METHOD",
    "UNSUBSCRIBE_METHOD",
    "DiscoverEventsCapability",
    "EventServices",
    "EventsExtension",
    "EventsListParams",
    "EventsService",
    "EventsSubscribeParams",
    "EventsUnsubscribeParams",
    "VerificationCache",
    "can_hold_subscriptions",
    "install_events",
    "install_extension",
    "permitted_profiles",
    "to_mcp_error",
]
