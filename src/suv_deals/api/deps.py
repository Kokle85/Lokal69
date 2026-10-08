"""Request dependencies of the dashboard API: app state, authentication and input parsing.

- `ApiState` holds everything a route needs (settings, database, verifier, limiter, metrics,
  clock, options); ``app.create_app`` stores it on ``app.state.suv_api``.
- `authenticate` (a FastAPI dependency) reads ONLY ``Authorization: Bearer`` (never cookies,
  query strings or bodies), verifies the Supabase access token, resolves the active membership
  for ``X-Workspace-Id`` and builds the request's `ActorContext`. A client whose failed
  authentications exhausted its `PreAuthLimiter` budget is refused before verification; the
  per-principal rate limit (mutations and reads have separate buckets) is applied right after
  token verification, before the membership query. Denials are counted with a bounded reason
  label.
- Input parsing never trusts FastAPI's implicit coercion: path ids are canonical UUID strings,
  query strings (at most 4 KiB) go through the closed ``api.schemas`` query models (unknown or
  repeated parameters are refused), and bodies must be ``application/json`` and validate against the
  closed mutation models in JSON mode. Every failure is ``VALIDATION_ERROR`` naming fields only.
- `in_transaction` runs one short workspace-scoped unit of work and re-runs it only after a
  failure that proves a rollback (`transactions.retry_transient`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Final, cast
from uuid import UUID

import anyio
from fastapi import Depends, Request
from pydantic import BaseModel, TypeAdapter, ValidationError

from suv_deals.api.auth import (
    WORKSPACE_HEADER,
    AuthFailure,
    MembershipDenied,
    Principal,
    SupabaseJwtVerifier,
    bearer_token,
    resolve_principal,
)
from suv_deals.api.errors import request_id_of, safe_field_names, unsupported_media_type
from suv_deals.api.middleware import PreAuthLimiter, PrincipalRateLimiter, RateLimit, client_address
from suv_deals.clock import Clock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.errors import DependencyUnavailable, Forbidden, ValidationFailed
from suv_deals.mcp.schemas import Id, validation_error_fields
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.views.operations import ReadinessView

STATE_ATTRIBUTE: Final = "suv_api"
#: Per mailbox-worker credential (``/v1/mail-workers``): a backlog drain may post bursts of
#: replies/reports, polling reads are periodic.
MAIL_WORKER_MUTATION_LIMIT: Final = RateLimit(capacity=120, per_seconds=0.5)  # 120 burst, 120/minute
MAIL_WORKER_READ_LIMIT: Final = RateLimit(capacity=60, per_seconds=1.0)  # 60 burst, 60/minute
TRANSACTION_ATTEMPTS: Final = 3
MAX_QUERY_STRING_BYTES: Final = 4096
_ID_ADAPTER: Final = TypeAdapter(Id)


@dataclass(frozen=True, slots=True)
class ApiOptions:
    """Tunables of one API instance (defaults are the production defaults)."""

    api_body_limit: int = 64 * 1024
    other_body_limit: int = 4 * 1024 * 1024
    #: Extra per-prefix body limits; they override the built-in ``/v1/mail-workers`` 128 KiB one.
    prefix_body_limits: Mapping[str, int] = field(default_factory=dict)
    #: Overrides the Host allow-list derived from APP_BASE_URL / MCP_PUBLIC_URL.
    allowed_hosts: tuple[str, ...] | None = None
    #: Serve ``/metrics`` on this app (only for a private bind; see ``app.create_metrics_app``).
    expose_metrics: bool = False
    jwt_leeway: timedelta = timedelta(seconds=30)
    readiness_cache_seconds: float = 1.0


@dataclass(slots=True)
class ApiState:
    settings: Settings
    db: Database
    verifier: SupabaseJwtVerifier | None
    limiter: PrincipalRateLimiter
    metrics: AppMetrics
    clock: Clock
    options: ApiOptions
    fallback_config: BusinessConfig | None
    cursor_secret: Callable[[], bytes]
    #: Failed-authentication budget per client, checked before any token verification.
    preauth: PreAuthLimiter = field(default_factory=PreAuthLimiter)
    #: Per mailbox-worker credential rate limits (keyed before the database lookup).
    mail_worker_limiter: PrincipalRateLimiter = field(
        default_factory=lambda: PrincipalRateLimiter(
            mutations=MAIL_WORKER_MUTATION_LIMIT, reads=MAIL_WORKER_READ_LIMIT
        )
    )
    #: ``(loop time, view)`` of the last readiness probe (see ``routes.readiness_view``).
    readiness_cache: tuple[float, ReadinessView] | None = None
    #: Serializes readiness refreshes: concurrent probes share one database check.
    readiness_lock: anyio.Lock = field(default_factory=anyio.Lock)


def api_state(request: Request) -> ApiState:
    return cast(ApiState, getattr(request.app.state, STATE_ATTRIBUTE))


# --------------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated principal of one request."""

    principal: Principal
    request_id: str

    @property
    def actor(self) -> ActorContext:
        return self.principal.actor


def _workspace_header(request: Request) -> str | None:
    values = request.headers.getlist(WORKSPACE_HEADER)
    if not values:
        return None
    if len(values) > 1:
        raise ValidationFailed("Send X-Workspace-Id at most once", details={"fields": [WORKSPACE_HEADER]})
    return values[0]


async def _authenticate(request: Request, *, allow_default_workspace: bool) -> AuthContext:
    state = api_state(request)
    request_id = request_id_of(request)
    client = client_address(request.scope)
    # A client that keeps presenting missing/invalid tokens is refused here, before any signature
    # check, JWKS fetch or database query.
    state.preauth.check(client)
    try:
        if state.verifier is None:
            raise DependencyUnavailable("Dashboard authentication is not configured")
        token = bearer_token(request.headers.getlist("authorization"))
        user = await state.verifier.verify(token)
        # Rate limit by the verified subject BEFORE the membership query, so a flood from one
        # signed-in user (member or not) never turns into database load.
        state.limiter.check(user.user_id, mutation=request.method not in ("GET", "HEAD"))
        principal = await resolve_principal(
            state.db,
            user,
            requested_workspace=_workspace_header(request),
            request_id=request_id,
            allow_default_workspace=allow_default_workspace,
        )
    except AuthFailure as exc:
        state.preauth.failed(client)
        state.metrics.record_auth_denial("api", exc.reason)
        raise
    except MembershipDenied as exc:
        state.metrics.record_auth_denial("api", exc.reason)
        raise
    return AuthContext(principal=principal, request_id=request_id)


async def authenticate(request: Request) -> AuthContext:
    """Dependency for every ``/api`` route that needs a selected workspace."""
    return await _authenticate(request, allow_default_workspace=False)


async def authenticate_bootstrap(request: Request) -> AuthContext:
    """Dependency for ``GET /api/me``: with several memberships and no header, the first one."""
    return await _authenticate(request, allow_default_workspace=True)


Authenticated = Annotated[AuthContext, Depends(authenticate)]
AuthenticatedBootstrap = Annotated[AuthContext, Depends(authenticate_bootstrap)]


def require_scope(request: Request, auth: AuthContext, scope: Scope | None) -> None:
    """Route-level scope check (repositories check again); a denial is counted and ``403``."""
    if scope is None:
        return
    try:
        auth.actor.require(scope)
    except Forbidden:
        api_state(request).metrics.record_auth_denial("api", "insufficient_scope")
        raise


# --------------------------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------------------------


def path_id(value: str, name: str) -> UUID:
    """A canonical 8-4-4-4-12 UUID path segment (``422`` otherwise)."""
    try:
        parsed = _ID_ADAPTER.validate_python(value)
    except ValidationError:
        raise ValidationFailed(f"{name} must be a UUID", details={"fields": [name]}) from None
    return parsed if isinstance(parsed, UUID) else UUID(str(parsed))


def _check_query_size(request: Request) -> None:
    """Refuse oversized query strings before parsing them (a cursor is at most 2,048 characters,
    so 4 KiB is ample; nothing else bounds the URL when uvicorn runs on httptools)."""
    raw = request.scope.get("query_string", b"")
    if isinstance(raw, bytes | bytearray) and len(raw) > MAX_QUERY_STRING_BYTES:
        raise ValidationFailed(
            "The query string is too long",
            details={"fields": ["query"], "limit_bytes": MAX_QUERY_STRING_BYTES},
        )


def query_model[M: BaseModel](request: Request, model: type[M]) -> M:
    """Parse the query string into a closed query model; repeated or unknown keys are refused.

    Repeated keys are counted in one linear pass (a per-key ``getlist`` is quadratic in the
    number of parameters, which an authenticated caller could use to stall the event loop).
    """
    _check_query_size(request)
    params = request.query_params
    counts = Counter(key for key, _ in params.multi_items())
    repeated = sorted(key for key, count in counts.items() if count > 1)
    if repeated:
        raise ValidationFailed(
            "Query parameters may appear only once",
            details={"fields": safe_field_names([[k] for k in repeated])},
        )
    try:
        return model.model_validate(dict(params))
    except ValidationError as exc:
        fields = validation_error_fields(exc, root="query", model=model)
        raise ValidationFailed("Invalid query parameters", details={"fields": fields}) from None


def no_query(request: Request) -> None:
    """Routes without query parameters refuse any (tokens are never accepted in query strings)."""
    _check_query_size(request)
    keys = sorted(set(request.query_params))
    if keys:
        raise ValidationFailed(
            "This route takes no query parameters", details={"fields": safe_field_names([[k] for k in keys])}
        )


def _is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


async def body_model[M: BaseModel](request: Request, model: type[M]) -> M:
    """Parse an ``application/json`` body into a closed mutation model (JSON-mode validation)."""
    if not _is_json(request.headers.get("content-type")):
        raise unsupported_media_type()
    raw = await request.body()
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        fields = validation_error_fields(exc, root="body", model=model)
        raise ValidationFailed("Invalid request body", details={"fields": fields}) from None


def check_idempotency_header(request: Request, key: str) -> None:
    """An optional ``Idempotency-Key`` header must equal the body's ``idempotency_key``."""
    values = request.headers.getlist("idempotency-key")
    if values and (len(values) > 1 or values[0].strip() != key):
        raise ValidationFailed(
            "Idempotency-Key header does not match the body's idempotency_key",
            details={"fields": ["Idempotency-Key"]},
        )


# --------------------------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------------------------


async def in_transaction[T](state: ApiState, actor: ActorContext, work: Callable[[Conn], Awaitable[T]]) -> T:
    """One short workspace-scoped transaction (no network I/O inside); transient conflicts that
    prove a rollback are re-run, everything else propagates as an `AppError`."""

    async def attempt() -> T:
        async with unit_of_work(state.db, actor) as conn:
            return await work(conn)

    return await retry_transient(attempt, attempts=TRANSACTION_ATTEMPTS)


__all__ = [
    "MAIL_WORKER_MUTATION_LIMIT",
    "MAIL_WORKER_READ_LIMIT",
    "MAX_QUERY_STRING_BYTES",
    "STATE_ATTRIBUTE",
    "ApiOptions",
    "ApiState",
    "AuthContext",
    "Authenticated",
    "AuthenticatedBootstrap",
    "api_state",
    "authenticate",
    "authenticate_bootstrap",
    "body_model",
    "check_idempotency_header",
    "in_transaction",
    "no_query",
    "path_id",
    "query_model",
    "require_scope",
]
