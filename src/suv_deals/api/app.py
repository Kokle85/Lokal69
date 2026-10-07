"""The backend application: dashboard BFF (``/api``), health/readiness and the MCP mount.

`create_app` builds one FastAPI app from ``Settings``:

- **Lifespan**: opens the ``Database`` (``SET ROLE settings.database_set_role``, ADR 0001) without
  blocking start-up on the database (readiness reports it), runs the mounted MCP app's own
  lifespan (the SDK's session manager must run in the parent lifespan) and closes both.
- **Middleware** (outermost first): request id + security headers + no-store + metrics (+ a
  backstop ``INTERNAL_ERROR``); strict ``Host`` allow-list (``APP_BASE_URL``/``MCP_PUBLIC_URL``
  hosts, loopback, plus ``API_ALLOWED_HOSTS``); CORS for ``/api`` only (origin
  allow-list from ``API_ALLOWED_ORIGINS`` or the origin of ``APP_BASE_URL``; no credentials);
  ``INTERNAL_ERROR`` rendering inside the CORS layer; request body limits. See
  ``api.middleware``.
- **Errors**: ``AppError`` -> ``ApiErrorResponse`` with ``errors.HTTP_STATUS``; request
  validation -> ``VALIDATION_ERROR`` with field names only; anything else -> ``INTERNAL_ERROR``
  without traces (``api.errors``).
- **Routes**: every route of ``docs/api_contract.md`` (``api.routes``); ``/healthz`` (liveness),
  ``/readyz`` (database, schema markers, critical configuration; 503 when not ready);
  ``/metrics`` only with ``ApiOptions.expose_metrics`` (prefer the private bind below).
  OpenAPI/Swagger pages are not served.
- **Private metrics**: with ``METRICS_ENABLED=true`` the lifespan serves Prometheus metrics on
  ``METRICS_BIND`` (default ``127.0.0.1:9464``) in a separate daemon-thread server
  (`start_private_metrics_server`), never on the public API port. A bind failure is logged and
  the API keeps serving (metrics are optional).
- **Database**: ``Database.from_settings`` (pool sizes, ``DATABASE_SET_ROLE`` and
  ``DATABASE_POOL_TIMEOUT_S``, so a database outage answers 503 quickly).
- **Extension points**: ``extra_routers`` are included after the core routes and before the MCP
  mount (the spec 37.8 mail-worker and inquiry routers plug in here, with their own
  authentication dependency and, through ``ApiOptions.prefix_body_limits``, their own body limit).
- **MCP**: the ASGI app built by ``suv_deals.mcp.server.build_mcp`` is mounted at ``/`` LAST, so
  every ``/api`` route takes precedence (`build_app` imports it lazily and tolerates its absence).

`build_app` is the production factory (``uvicorn suv_deals.api.app:build_app --factory``).
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIServer

import yaml
from fastapi import APIRouter, FastAPI, Request
from prometheus_client import start_http_server
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp

import suv_deals
from suv_deals.api import routes
from suv_deals.api.auth import SigningKeyResolver, StaticJwks, SupabaseJwtVerifier
from suv_deals.api.deps import STATE_ATTRIBUTE, ApiOptions, ApiState
from suv_deals.api.errors import install_exception_handlers
from suv_deals.api.middleware import (
    ApiCorsMiddleware,
    BodySizeLimitMiddleware,
    InternalErrorMiddleware,
    PrincipalRateLimiter,
    RequestContextMiddleware,
)
from suv_deals.api.schemas import ROUTES
from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.errors import AppError
from suv_deals.observability.logging import configure_logging
from suv_deals.observability.metrics import AppMetrics, get_metrics, render_metrics
from suv_deals.persistence.database import Database
from suv_deals.persistence.queries import cursor_secret
from suv_deals.settings import Settings, get_settings

logger = logging.getLogger("suv_deals.api")

LOCAL_HOSTS: Final = ("127.0.0.1", "localhost")
_HOST_NAME_RE: Final = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
DEFAULT_PORTS: Final = {"http": 80, "https": 443}
MCP_SERVER_MODULE: Final = "suv_deals.mcp.server"


# --------------------------------------------------------------------------------------------
# Host and origin allow-lists
# --------------------------------------------------------------------------------------------


def _hostname(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    return parts.hostname if parts.scheme in ("http", "https") and parts.hostname else None


def allowed_hosts(settings: Settings, override: Sequence[str] | None = None) -> list[str]:
    """``Host`` allow-list: the hosts of ``APP_BASE_URL`` and ``MCP_PUBLIC_URL``, loopback names
    (container health probes) and ``API_ALLOWED_HOSTS`` (a reverse proxy's internal name, a
    probe's service DNS name), unless ``override`` is given. Never a wildcard; an extra entry that
    is not a plain host name or IPv4 address is dropped with a warning."""
    if override is not None:
        hosts = {h.strip().lower() for h in override if h.strip()}
    else:
        hosts = {h for h in (_hostname(settings.app_base_url), _hostname(settings.mcp_public_url)) if h}
        hosts.update(LOCAL_HOSTS)
        for extra in settings.extra_allowed_hosts():
            if "*" not in extra and _HOST_NAME_RE.fullmatch(extra):
                hosts.add(extra)
            elif "*" not in extra:
                logger.warning("ignoring an invalid API_ALLOWED_HOSTS entry")
    if "*" in hosts or any("*" in h for h in hosts):
        raise ValueError("wildcard hosts are not allowed")
    return sorted(hosts)


def _origin(value: str, *, production: bool) -> str | None:
    """The serialized origin a browser sends (``scheme://host[:port]``), or ``None`` if invalid.

    The default port is omitted (``https://x:443`` is the origin ``https://x``), an IPv6 host is
    bracketed, and an invalid port or a wildcard host is invalid rather than a start-up crash.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    hostname = parts.hostname
    if (
        parts.scheme not in ("http", "https")
        or not hostname
        or "*" in hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return None
    if parts.scheme == "http" and production and hostname not in LOCAL_HOSTS:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    suffix = f":{port}" if port is not None and port != DEFAULT_PORTS[parts.scheme] else ""
    return f"{parts.scheme}://{host}{suffix}"


def allowed_origins(settings: Settings) -> list[str]:
    """CORS origins: ``API_ALLOWED_ORIGINS`` (comma-separated), else the origin of ``APP_BASE_URL``.

    Wildcards, paths, credentials and (in production) non-loopback ``http`` origins are dropped.
    """
    configured = [o.strip() for o in settings.api_allowed_origins.split(",") if o.strip()]
    candidates = configured or [settings.app_base_url]
    origins: list[str] = []
    production = settings.app_env == "production"
    for candidate in candidates:
        origin = _origin(candidate, production=production) if candidate != "*" else None
        if origin is None:
            logger.warning("ignoring an invalid CORS origin from the configuration")
        elif origin not in origins:
            origins.append(origin)
    return origins


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _fallback_config(settings: Settings) -> BusinessConfig | None:
    """The YAML business defaults shown by ``/api/settings`` until a revision is recorded."""
    try:
        return load_business_config(settings.config_dir)
    except (AppError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        logger.warning("the business configuration defaults could not be loaded")
        return None


def _database(settings: Settings) -> Database:
    if settings.database_url is None or not settings.database_url.get_secret_value():
        raise ValueError("DATABASE_URL is required for the API")
    return Database.from_settings(settings, application_name="suv-deals-api")


def _metrics_route(metrics: AppMetrics) -> Any:
    async def metrics_endpoint(_: Request) -> Response:
        body, content_type = render_metrics(metrics)
        return Response(content=body, media_type=content_type, headers={"Cache-Control": "no-store"})

    return metrics_endpoint


def create_metrics_app(metrics: AppMetrics | None = None) -> Starlette:
    """A ``/metrics``-only ASGI app for a private bind (for example ``127.0.0.1:9100``)."""
    target = metrics or get_metrics()
    endpoint = _metrics_route(target)
    return Starlette(routes=[Route("/metrics", endpoint, methods=["GET"])])


@dataclass(slots=True)
class PrivateMetricsServer:
    """A running private Prometheus endpoint (daemon thread; see `start_private_metrics_server`)."""

    host: str
    port: int
    server: WSGIServer
    thread: threading.Thread

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_private_metrics_server(
    settings: Settings, metrics: AppMetrics | None = None
) -> PrivateMetricsServer | None:
    """Serve ``metrics`` on ``METRICS_BIND`` when ``METRICS_ENABLED`` (else ``None``).

    The server runs in a daemon thread (``prometheus_client``'s WSGI server), so it never touches
    the event loop or uvicorn's signal handling. It exposes only the metrics registry: no request
    data, ids, URLs or secrets (label cardinality is bounded, ``observability.metrics``). The bind
    is private by default (``127.0.0.1:9464``); a container may bind ``0.0.0.0`` without
    publishing the port. Port 0 picks a free port (tests); the result reports the actual port.
    """
    if not settings.metrics_enabled:
        return None
    host, port = settings.metrics_address
    target = metrics or get_metrics()
    server, thread = start_http_server(port, addr=host, registry=target.registry)
    return PrivateMetricsServer(host=host, port=int(server.server_port), server=server, thread=thread)


# --------------------------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------------------------


def create_app(
    settings: Settings,
    *,
    db: Database | None = None,
    jwks: SigningKeyResolver | Mapping[str, Any] | None = None,
    mcp_asgi: ASGIApp | None = None,
    manage_db: bool | None = None,
    clock: Clock | None = None,
    metrics: AppMetrics | None = None,
    limiter: PrincipalRateLimiter | None = None,
    options: ApiOptions | None = None,
    extra_routers: Sequence[APIRouter] = (),
    legacy_hs256_secret: SecretStr | None = None,
) -> FastAPI:
    """Build the backend app (see module docstring).

    ``db``: a caller-managed ``Database`` (already open) unless ``manage_db=True``; without it the
    app creates and manages one from ``DATABASE_URL``. ``jwks``: the signing-key resolver for
    Supabase access tokens, or a JWK-set document (``{"keys": [...]}``, wrapped in `StaticJwks`;
    tests and pinned keys); default: the project's JWKS endpoint through ``PyJWKClient``.
    ``legacy_hs256_secret``: only for a project still on the legacy shared secret.
    ``extra_routers``: extension routers (see module docstring).
    """
    opts = options or ApiOptions()
    resolver: SigningKeyResolver | None = StaticJwks(jwks) if isinstance(jwks, Mapping) else jwks
    app_clock = clock or SystemClock()
    app_metrics = metrics or get_metrics()
    owns_db = (db is None) if manage_db is None else manage_db
    database = db if db is not None else _database(settings)
    verifier = SupabaseJwtVerifier.from_settings(
        settings, resolver=resolver, leeway=opts.jwt_leeway, legacy_hs256_secret=legacy_hs256_secret
    )
    if verifier is None:
        logger.warning("SUPABASE_URL is not configured: every /api request will be refused")
    state = ApiState(
        settings=settings,
        db=database,
        verifier=verifier,
        limiter=limiter or PrincipalRateLimiter(),
        metrics=app_metrics,
        clock=app_clock,
        options=opts,
        fallback_config=_fallback_config(settings),
        cursor_secret=lambda: cursor_secret(settings.mcp_cursor_signing_secret),
    )
    app_metrics.set_build_info(settings.build_id, settings.app_env)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        metrics_server: PrivateMetricsServer | None = None
        try:
            metrics_server = start_private_metrics_server(settings, app_metrics)
        except OSError:
            logger.error("the private metrics endpoint could not bind METRICS_BIND; serving without it")
        try:
            if owns_db:
                await database.open(wait=False)
            try:
                async with contextlib.AsyncExitStack() as stack:
                    if isinstance(mcp_asgi, Starlette):
                        await stack.enter_async_context(mcp_asgi.router.lifespan_context(mcp_asgi))
                    yield
            finally:
                if owns_db:
                    await database.close()
        finally:
            # Also when start-up failed: never leave the listener thread and its port behind.
            if metrics_server is not None:
                metrics_server.close()

    middleware = [
        Middleware(
            RequestContextMiddleware,
            metrics=app_metrics,
            clock=app_clock,
            hsts=settings.app_base_url.startswith("https://"),
        ),
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_hosts(settings, opts.allowed_hosts),
            www_redirect=False,
        ),
        Middleware(ApiCorsMiddleware, allow_origins=allowed_origins(settings)),
        Middleware(InternalErrorMiddleware, metrics=app_metrics, clock=app_clock),
        Middleware(
            BodySizeLimitMiddleware,
            api_limit=opts.api_body_limit,
            default_limit=opts.other_body_limit,
            prefix_limits=dict(opts.prefix_body_limits),
            clock=app_clock,
            metrics=app_metrics,
        ),
    ]
    app = FastAPI(
        title="SUV deals backend",
        version=suv_deals.__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        middleware=middleware,
    )
    setattr(app.state, STATE_ATTRIBUTE, state)
    install_exception_handlers(app, clock=app_clock, metrics=app_metrics)
    app.include_router(routes.router)
    if opts.expose_metrics:
        app.add_api_route("/metrics", _metrics_route(app_metrics), methods=["GET"], include_in_schema=False)
    for extra in extra_routers:
        app.include_router(extra)
    routes.install_api_fallback(app)  # unknown /api paths never reach the MCP mount
    if mcp_asgi is not None:
        app.mount("/", mcp_asgi)  # LAST: /api, /healthz and /readyz take precedence
    return app


def load_mcp_app(settings: Settings, db: Database) -> ASGIApp | None:
    """The MCP ASGI app from ``suv_deals.mcp.server.build_mcp(settings, db=db)``, or ``None`` when
    that package is not installed yet. Any other import or build error propagates."""
    try:
        module = importlib.import_module(MCP_SERVER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != MCP_SERVER_MODULE:
            raise
        logger.info("the MCP server package is not available; serving the dashboard API only")
        return None
    build_mcp = module.build_mcp
    app: ASGIApp = build_mcp(settings, db=db)
    return app


def build_app(
    settings: Settings | None = None,
    *,
    extra_routers: Sequence[APIRouter] = (),
    options: ApiOptions | None = None,
) -> FastAPI:
    """Production factory: logging, database, the optional MCP mount and the API.

    ``extra_routers``/``options`` are the extension hook for later packages (for example the
    spec 37.8 mail-worker router with ``ApiOptions(prefix_body_limits={"/v1/mail-workers":
    128 * 1024})``); a wrapper factory passes them and serves the result with ``--factory``.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)
    database = _database(resolved)
    mcp_app = load_mcp_app(resolved, database)
    return create_app(
        resolved,
        db=database,
        mcp_asgi=mcp_app,
        manage_db=True,
        extra_routers=extra_routers,
        options=options,
    )


#: Route keys this app serves (``api.schemas.ROUTES``); used by the contract tests.
SERVED_ROUTE_KEYS: Final = frozenset(route.key for route in ROUTES)

__all__ = [
    "SERVED_ROUTE_KEYS",
    "PrivateMetricsServer",
    "allowed_hosts",
    "allowed_origins",
    "build_app",
    "create_app",
    "create_metrics_app",
    "load_mcp_app",
    "start_private_metrics_server",
]
