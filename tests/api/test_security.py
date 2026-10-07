"""Transport security and operations of the backend app: CORS, headers, request ids, safe errors,
body/media/rate limits, Host checks, health/readiness/metrics and the MCP mount.

Tests that need PostgreSQL carry the ``db`` marker; the rest run without a database (an unopened
``Database`` pointing at a closed port stands in, so any accidental use fails as "unavailable").
"""

from __future__ import annotations

import asyncio
import contextlib
import types
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.api.conftest import (
    BASE_URL,
    DASHBOARD_ORIGIN,
    UNREACHABLE_DB,
    ApiHarness,
    SigningKeys,
    TokenFactory,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.integration.db.helpers import Seed

from suv_deals.api import app as app_module
from suv_deals.api import routes
from suv_deals.api.app import allowed_hosts, allowed_origins, create_metrics_app, load_mcp_app
from suv_deals.api.deps import ApiOptions
from suv_deals.api.middleware import PrincipalRateLimiter, RateLimit
from suv_deals.errors import DependencyUnavailable, RateLimited
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings

LEAKS = ("Traceback", "postgresql://", "select ", "SELECT ", "password", "psycopg", 'File "')


def no_leaks(response: httpx.Response) -> None:
    for marker in LEAKS:
        assert marker not in response.text, marker


@contextlib.asynccontextmanager
async def offline_client(
    keys: SigningKeys, settings: Settings | None = None, **kwargs: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, AppMetrics]]:
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(
        settings or make_settings(), keys, Database(UNREACHABLE_DB), metrics=metrics, **kwargs
    )
    async with running_client(app) as client:
        yield client, metrics


# --------------------------------------------------------------------------------------------
# CORS, security headers, request ids
# --------------------------------------------------------------------------------------------


async def test_cors_preflight_allows_only_the_dashboard_origin(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        allowed = await client.options(
            "/api/reviews/x/claim",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization, content-type, idempotency-key, x-workspace-id, x-request-id"
                ),
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
        assert "access-control-allow-credentials" not in allowed.headers
        assert allowed.headers["access-control-max-age"] == "600"
        assert "cookie" not in allowed.headers["access-control-allow-headers"].lower()
        assert set(allowed.headers["access-control-allow-methods"].replace(" ", "").split(",")) == {
            "GET",
            "POST",
            "OPTIONS",
        }
        for origin in (
            "https://evil.example",
            "null",
            DASHBOARD_ORIGIN + ".evil.example",
            "http://dashboard.synthetic.example",
        ):
            denied = await client.options(
                "/api/me", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
            )
            assert denied.status_code == 400, origin
            assert "access-control-allow-origin" not in denied.headers
        bad_header = await client.options(
            "/api/me",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-evil",
            },
        )
        assert bad_header.status_code == 400
        bad_method = await client.options(
            "/api/me", headers={"Origin": DASHBOARD_ORIGIN, "Access-Control-Request-Method": "DELETE"}
        )
        assert bad_method.status_code == 400
        simple = await client.get("/healthz", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in simple.headers
        api_simple = await client.get("/api/me", headers={"Origin": "https://evil.example"})
        assert api_simple.status_code == 401
        assert "access-control-allow-origin" not in api_simple.headers
        api_ok_origin = await client.get("/api/me", headers={"Origin": DASHBOARD_ORIGIN})
        assert api_ok_origin.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
        exposed = api_ok_origin.headers["access-control-expose-headers"].lower()
        assert "x-request-id" in exposed and "retry-after" in exposed


def test_origin_and_host_allow_lists() -> None:
    settings = make_settings(
        api_allowed_origins="https://dash.example, *, https://a.example/path, https://u:p@b.example, http://plain.example"
    )
    assert allowed_origins(settings) == ["https://dash.example", "http://plain.example"]
    production = make_settings(
        app_env="production", api_allowed_origins="http://plain.example,http://127.0.0.1:5173"
    )
    assert allowed_origins(production) == ["http://127.0.0.1:5173"]
    assert allowed_origins(make_settings()) == [DASHBOARD_ORIGIN]
    hosts = allowed_hosts(make_settings(mcp_public_url="https://mcp.synthetic.example/mcp"))
    assert hosts == ["127.0.0.1", "dashboard.synthetic.example", "localhost", "mcp.synthetic.example"]
    assert allowed_hosts(make_settings(), ["API.example"]) == ["api.example"]
    with pytest.raises(ValueError):
        allowed_hosts(make_settings(), ["*"])


def test_origins_are_serialized_like_browsers_and_bad_entries_never_crash() -> None:
    """A browser omits the default port from ``Origin``: ``https://x:443`` must match it. An
    invalid port or a wildcard host is dropped (logged) instead of failing app start-up."""
    settings = make_settings(
        api_allowed_origins=(
            "https://Dash.Example:443, http://127.0.0.1:80, https://dash.example:8443,"
            " https://bad.example:99999, https://worse.example:abc, https://*.example, http://[::1]:5173"
        )
    )
    assert allowed_origins(settings) == [
        "https://dash.example",
        "http://127.0.0.1",
        "https://dash.example:8443",
        "http://[::1]:5173",
    ]
    assert allowed_origins(make_settings(app_base_url="https://dash.example:443/")) == [
        "https://dash.example"
    ]


async def test_default_port_origin_configuration_matches_the_browser_origin(keys: SigningKeys) -> None:
    settings = make_settings(api_allowed_origins="https://dashboard.synthetic.example:443")
    async with offline_client(keys, settings=settings) as (client, _):
        preflight = await client.options(
            "/api/me", headers={"Origin": DASHBOARD_ORIGIN, "Access-Control-Request-Method": "GET"}
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN


async def test_security_headers_no_store_and_request_ids(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        for path in ("/healthz", "/api/me", "/api/unknown"):
            response = await client.get(path)
            assert response.headers["cache-control"] == "no-store", path
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["strict-transport-security"].startswith("max-age=")
            assert response.headers["content-type"] == "application/json"
        echoed = await client.get("/api/me", headers={"X-Request-Id": "req-dashboard-42"})
        assert echoed.headers["x-request-id"] == "req-dashboard-42"
        assert error_of(echoed)["correlation_id"] == "req-dashboard-42"
        for bad in ("has space", "x" * 201, "line\tbreak"):
            replaced = await client.get("/api/me", headers={"X-Request-Id": bad})
            assert replaced.headers["x-request-id"].startswith("req-")
            assert replaced.headers["x-request-id"] != bad


async def test_unknown_api_paths_and_methods_are_typed_errors(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        missing = await client.get("/api/does-not-exist")
        assert missing.status_code == 404
        assert error_of(missing)["code"] == "NOT_FOUND"
        wrong_method = await client.delete("/api/me")
        assert wrong_method.status_code == 405
        assert wrong_method.headers["allow"] == "GET"
        assert error_of(wrong_method)["code"] == "VALIDATION_ERROR"
        get_on_post = await client.get(f"/api/reviews/{uuid.uuid4()}/claim")
        assert get_on_post.status_code == 405
        assert get_on_post.headers["allow"] == "POST"
        head_me = await client.head("/api/me")
        assert head_me.status_code == 405
        assert head_me.headers["allow"] == "GET"
        assert (await client.head("/api/not-a-route")).status_code == 404
        put_me = await client.put("/api/me", json={})
        assert put_me.status_code == 405
        assert put_me.headers["allow"] == "GET"


async def test_strict_host_check(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        assert (await client.get("/healthz")).status_code == 200
        evil = await client.get("/healthz", headers={"Host": "evil.example"})
        assert evil.status_code == 400
        local = await client.get("/healthz", headers={"Host": "127.0.0.1:8000"})
        assert local.status_code == 200


# --------------------------------------------------------------------------------------------
# Safe errors
# --------------------------------------------------------------------------------------------


async def test_unexpected_errors_are_internal_error_without_details(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    boom = APIRouter()

    @boom.get("/api/v11/boom")
    async def explode() -> Response:
        raise RuntimeError("SELECT password FROM secrets WHERE dsn = 'postgresql://u:p@db/x'")

    async with offline_client(keys, extra_routers=[boom]) as (client, metrics):
        response = await client.get("/api/v11/boom", headers={"Origin": DASHBOARD_ORIGIN})
        assert response.status_code == 500
        # Rendered inside the CORS layer: the dashboard can read the body and correlation id.
        assert response.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
        assert response.headers["cache-control"] == "no-store"
        error = error_of(response)
        assert error == {
            "code": "INTERNAL_ERROR",
            "message": "Internal error; the incident was logged",
            "retryable": True,
            "retry_after_seconds": None,
            "correlation_id": response.headers["x-request-id"],
            "details": None,
        }
        no_leaks(response)
        assert metrics.errors_total.labels(surface="api", code="INTERNAL_ERROR")._value.get() == 1


async def test_database_outage_is_503_without_connection_details(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with offline_client(keys) as (client, _):
        token = tokens.mint(uuid.uuid4())
        response = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503
        error = error_of(response)
        assert error["code"] == "DEPENDENCY_UNAVAILABLE"
        assert error["retryable"] is True
        no_leaks(response)
        assert "127.0.0.1:1" not in response.text


async def test_missing_auth_configuration_refuses_requests(keys: SigningKeys, tokens: TokenFactory) -> None:
    async with offline_client(keys, settings=make_settings(supabase_url=None)) as (client, _):
        response = await client.get(
            "/api/me", headers={"Authorization": f"Bearer {tokens.mint(uuid.uuid4())}"}
        )
        assert response.status_code == 503
        assert error_of(response)["code"] == "DEPENDENCY_UNAVAILABLE"


# --------------------------------------------------------------------------------------------
# Body, media type and rate limits
# --------------------------------------------------------------------------------------------


async def test_oversized_bodies_are_413_and_wrong_media_types_415(keys: SigningKeys) -> None:
    options = ApiOptions(api_body_limit=1024)
    path = f"/api/reviews/{uuid.uuid4()}/claim"
    async with offline_client(keys, options=options) as (client, _):
        declared = await client.post(
            path,
            content=b"x" * 4096,
            headers={"Content-Type": "application/json", "Origin": DASHBOARD_ORIGIN},
        )
        assert declared.status_code == 413
        assert declared.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
        error = error_of(declared)
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"] == {"fields": ["body"], "limit_bytes": 1024}

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(8):
                yield b"y" * 512

        streamed = await client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
        assert streamed.status_code in (401, 413)  # auth runs first unless the body is read
        bad_length = await client.post(path, content=b"{}", headers={"Content-Length": "-1"})
        assert bad_length.status_code == 413


@pytest.mark.db
async def test_streamed_oversized_body_is_413_for_an_authenticated_caller(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("API body limit")
    user = seed.user()
    seed.membership(workspace, user, "reviewer")
    app = build_test_app(make_settings(), keys, db, options=ApiOptions(api_body_limit=1024))

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b"y" * 512

    async with running_client(app) as client:
        headers = {"Authorization": f"Bearer {tokens.mint(user)}", "Content-Type": "application/json"}
        response = await client.post(f"/api/reviews/{uuid.uuid4()}/claim", content=chunks(), headers=headers)
        assert response.status_code == 413
        text_plain = await client.post(
            f"/api/reviews/{uuid.uuid4()}/claim",
            content=b'{"expected_version": 1, "idempotency_key": "media-type-001"}',
            headers={**headers, "Content-Type": "text/plain"},
        )
        assert text_plain.status_code == 415
        assert error_of(text_plain)["details"] == {"fields": ["Content-Type"]}


@pytest.mark.db
async def test_mutations_are_rate_limited_per_principal(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("API rate limit")
    first, second = seed.user(), seed.user()
    seed.membership(workspace, first, "reviewer")
    seed.membership(workspace, second, "reviewer")
    limiter = PrincipalRateLimiter(
        mutations=RateLimit(capacity=2, per_seconds=60.0), reads=RateLimit(capacity=100, per_seconds=1.0)
    )
    app = build_test_app(make_settings(), keys, db, limiter=limiter)
    async with running_client(app) as client:
        harness = ApiHarness(client, tokens, None, workspace, AppMetrics(process_metrics=False))  # type: ignore[arg-type]
        body = {"note": "SYNTHETIC", "idempotency_key": "rate-limit-note1"}
        path = f"/api/listings/{uuid.uuid4()}/notes"
        statuses = [(await harness.post(path, first, body)).status_code for _ in range(3)]
        assert statuses == [404, 404, 429]
        limited = await harness.post(path, first, body)
        assert limited.status_code == 429
        error = error_of(limited)
        assert error["code"] == "RATE_LIMITED"
        assert error["retryable"] is True
        assert 1 <= error["retry_after_seconds"] <= 60
        assert limited.headers["retry-after"] == str(error["retry_after_seconds"])
        assert (await harness.get("/api/me", first)).status_code == 200  # reads have their own bucket
        assert (await harness.post(path, second, body)).status_code == 404  # other principals unaffected


def test_rate_limiter_refills_over_time() -> None:
    now = [0.0]
    limiter = PrincipalRateLimiter(
        mutations=RateLimit(capacity=1, per_seconds=10.0),
        reads=RateLimit(capacity=1, per_seconds=1.0),
        monotonic=lambda: now[0],
    )
    principal = uuid.uuid4()
    limiter.check(principal, mutation=True)
    with pytest.raises(RateLimited) as caught:
        limiter.check(principal, mutation=True)
    assert caught.value.retry_after_seconds == 10
    now[0] = 10.0
    limiter.check(principal, mutation=True)
    with pytest.raises(ValueError):
        RateLimit(capacity=0, per_seconds=1.0)


async def _drive_asgi(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one HTTP request through the ASGI app directly and return the sent messages."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_non_ascii_content_length_is_413_not_an_internal_error(keys: SigningKeys) -> None:
    """``"²".isdigit()`` is true but ``int("²")`` fails: such a header is a 413 like any other
    malformed length (the ASGI app is driven directly; HTTP clients refuse to send it)."""
    app = build_test_app(
        make_settings(), keys, Database(UNREACHABLE_DB), metrics=AppMetrics(process_metrics=False)
    )
    for raw in ("²".encode("latin-1"), b"\xb9\xb2", b"1 2", b"0x10"):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": f"/api/reviews/{uuid.uuid4()}/claim",
            "raw_path": b"/api/reviews/x/claim",
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"dashboard.synthetic.example"),
                (b"content-type", b"application/json"),
                (b"content-length", raw),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("dashboard.synthetic.example", 443),
        }
        sent = await _drive_asgi(app, scope)
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 413, raw
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert b'"VALIDATION_ERROR"' in body and b"INTERNAL_ERROR" not in body


# --------------------------------------------------------------------------------------------
# Health, readiness, metrics
# --------------------------------------------------------------------------------------------


async def test_liveness_and_readiness_without_a_database(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        alive = await client.get("/healthz")
        assert alive.status_code == 200
        assert alive.json() == {"status": "alive", "build_id": "synthetic-api.1"}
        assert (await client.head("/healthz")).status_code == 200
        assert (await client.head("/readyz")).status_code == 503
        posted = await client.post("/healthz")
        assert posted.status_code == 405
        assert posted.headers["allow"] == "GET, HEAD"
        ready = await client.get("/readyz")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert {c["name"]: c["status"] for c in body["checks"]} == {
            "database": "unavailable",
            "schema": "unknown",
            "config": "ok",
        }
        no_leaks(ready)
        assert "supabase" not in ready.text


async def test_concurrent_readiness_probes_share_one_database_check(
    keys: SigningKeys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/readyz`` is unauthenticated: a burst of probes must not take one pooled connection each."""
    calls = 0
    original = routes._database_checks

    async def slow_checks(state: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await original(state)

    monkeypatch.setattr(routes, "_database_checks", slow_checks)
    async with offline_client(keys) as (client, _):
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(12)))
        assert {r.status_code for r in responses} == {503}
        assert calls == 1
        assert len({r.text for r in responses}) == 1


@pytest.mark.db
async def test_app_managed_database_uses_the_backend_role_and_closes(
    db_url: str, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    """Without an injected ``db`` the lifespan opens ``Database(DATABASE_URL, set_role=...)``
    itself (ADR 0001: RLS applies) and closes it on shutdown; a JWK-set document is accepted."""
    workspace = seed.workspace("API managed database")
    user = seed.user()
    seed.membership(workspace, user, "viewer")
    settings = make_settings(database_url=SecretStr(db_url), database_set_role="suv_backend")
    app = app_module.create_app(settings, jwks=keys.jwks, metrics=AppMetrics(process_metrics=False))
    state = app.state.suv_api
    async with running_client(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        me = await client.get("/api/me", headers={"Authorization": f"Bearer {tokens.mint(user)}"})
        assert me.status_code == 200, me.text
        assert me.json()["data"]["role"] == "viewer"
        async with state.db.transaction() as conn:
            role = await (await conn.execute("select current_user as role")).fetchone()
        assert role is not None and role["role"] == "suv_backend"
    with pytest.raises(DependencyUnavailable):
        async with state.db.transaction():
            pass  # the pool was closed by the lifespan


@pytest.mark.db
async def test_readiness_with_database_schema_and_configuration(db: Database, keys: SigningKeys) -> None:
    app = build_test_app(make_settings(), keys, db)
    async with running_client(app) as client:
        ready = await client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True
        assert all(check["status"] == "ok" for check in ready.json()["checks"])
    unconfigured = build_test_app(make_settings(mcp_cursor_signing_secret=None), keys, db)
    async with running_client(unconfigured) as client:
        response = await client.get("/readyz")
        assert response.status_code == 503
        config = next(c for c in response.json()["checks"] if c["name"] == "config")
        assert config["status"] == "not_configured"


async def test_metrics_are_private_by_default(keys: SigningKeys) -> None:
    async with offline_client(keys) as (client, _):
        assert (await client.get("/metrics")).status_code == 404
    async with offline_client(keys, options=ApiOptions(expose_metrics=True)) as (client, _):
        await client.get(f"/api/candidates/{uuid.uuid4()}")
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "suv_deals_request_duration_seconds" in response.text
        assert 'route="/api/candidates/{listing_id}"' in response.text  # templates, never raw ids
        assert "Bearer" not in response.text
    private = create_metrics_app(AppMetrics(process_metrics=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=private), base_url="http://127.0.0.1:9100"
    ) as c:
        assert (await c.get("/metrics")).status_code == 200


# --------------------------------------------------------------------------------------------
# MCP mount
# --------------------------------------------------------------------------------------------


def fake_mcp_app(events: list[str]) -> Starlette:
    async def mcp_endpoint(request: Request) -> Response:
        return JSONResponse({"mcp": True, "path": request.url.path})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        events.append("started")
        yield
        events.append("stopped")

    return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])], lifespan=lifespan)


async def test_mcp_app_is_mounted_last_and_its_lifespan_runs(keys: SigningKeys) -> None:
    events: list[str] = []
    mcp = fake_mcp_app(events)
    async with offline_client(keys, mcp_asgi=mcp) as (client, _):
        assert events == ["started"]
        mcp_response = await client.post("/mcp", json={})
        assert mcp_response.json() == {"mcp": True, "path": "/mcp"}
        assert (await client.get("/healthz")).json()["status"] == "alive"
        api_missing = await client.get("/api/not-a-route")
        assert error_of(api_missing)["code"] == "NOT_FOUND"  # never falls through to the MCP app
        assert (await client.get("/api/me")).status_code == 401
    assert events == ["started", "stopped"]


def test_load_mcp_app_tolerates_only_the_absent_package(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    db = Database(UNREACHABLE_DB)

    def absent(name: str) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(app_module.importlib, "import_module", absent)
    assert load_mcp_app(settings, db) is None

    def broken_dependency(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")

    monkeypatch.setattr(app_module.importlib, "import_module", broken_dependency)
    with pytest.raises(ModuleNotFoundError):
        load_mcp_app(settings, db)

    built: list[Any] = []
    sentinel = Starlette()

    def build_mcp(given_settings: Settings, *, db: Database) -> Starlette:
        built.append((given_settings, db))
        return sentinel

    module = types.SimpleNamespace(build_mcp=build_mcp)
    monkeypatch.setattr(app_module.importlib, "import_module", lambda name: module)
    assert load_mcp_app(settings, db) is sentinel
    assert built == [(settings, db)]


async def test_the_real_mcp_package_mounts_behind_the_api_routes(keys: SigningKeys) -> None:
    """``load_mcp_app`` builds the in-tree MCP server (``build_mcp(settings, db=db)``); mounted at
    ``/`` it never shadows ``/api``, ``/healthz`` or the API's typed 404, and its lifespan runs."""
    settings = make_settings()
    db = Database(UNREACHABLE_DB)
    mcp = load_mcp_app(settings, db)
    assert isinstance(mcp, Starlette)
    async with offline_client(keys, settings=settings, mcp_asgi=mcp) as (client, _):
        assert (await client.get("/healthz")).status_code == 200
        unauthenticated = await client.get("/api/me")
        assert unauthenticated.status_code == 401
        assert error_of(unauthenticated)["code"] == "UNAUTHENTICATED"
        assert error_of(await client.get("/api/not-a-route"))["code"] == "NOT_FOUND"
        mcp_response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert mcp_response.status_code != 404
        assert "schema_version" not in mcp_response.text  # answered by the MCP app, not /api
        no_leaks(mcp_response)


def test_create_app_requires_a_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        app_module.create_app(make_settings(database_url=None))
    assert BASE_URL.startswith("https://")
