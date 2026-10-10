"""Server assembly details: Host/Origin allow-lists, the standalone factory and guard behaviour."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from suv_deals.mcp.server import (
    McpApp,
    McpOptions,
    build_mcp,
    create_standalone_app,
    mcp_allowed_hosts,
    mcp_allowed_origins,
)
from suv_deals.observability.metrics import AppMetrics
from tests.mcp.conftest import (
    SigningKeys,
    envelope,
    make_settings,
    mcp_client,
    offline_db,
    rpc_headers,
    running,
)
from tests.mcp.test_auth import StubVerifier

DENIALS = "suv_deals_authorization_denials_total"


def test_allowed_hosts_come_from_the_public_urls_without_wildcards() -> None:
    settings = make_settings(
        mcp_public_url="https://api.synthetic.example:443/mcp",
        app_base_url="https://dash.synthetic.example:8443",
    )
    assert mcp_allowed_hosts(settings) == [
        "api.synthetic.example",
        "api.synthetic.example:443",
        "dash.synthetic.example:8443",
    ]
    local = make_settings(mcp_public_url="http://127.0.0.1:8000/mcp", app_base_url="http://localhost:3000")
    assert mcp_allowed_hosts(local) == ["127.0.0.1:8000", "127.0.0.1:*", "localhost:3000", "localhost:*"]
    ipv6 = make_settings(mcp_public_url="http://[::1]:8000/mcp", app_base_url="")
    assert mcp_allowed_hosts(ipv6) == ["[::1]:8000", "[::1]:*"]
    assert mcp_allowed_hosts(settings, ["API.Synthetic.example "]) == ["api.synthetic.example"]
    with pytest.raises(ValueError, match="wildcard"):
        mcp_allowed_hosts(settings, ["*"])


def test_allowed_origins_drop_wildcards_paths_and_plain_http_in_production() -> None:
    settings = make_settings(
        app_env="production",
        mcp_allowed_origins="https://dash.synthetic.example, *, https://x.synthetic.example/path,"
        " http://insecure.synthetic.example, http://127.0.0.1:5173, https://dash.synthetic.example",
    )
    assert mcp_allowed_origins(settings) == ["https://dash.synthetic.example", "http://127.0.0.1:5173"]
    assert mcp_allowed_origins(make_settings(mcp_allowed_origins="")) == []


def test_standalone_factory_requires_a_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_standalone_app(make_settings(database_url=None))


async def test_standalone_app_owns_its_database_pool(keys: SigningKeys) -> None:
    settings = make_settings(
        mcp_auth_mode="static_bearer",
        database_url=SecretStr("postgresql://suv:suv@127.0.0.1:1/synthetic_unreachable"),
        log_level="WARNING",
    )
    app = create_standalone_app(settings)
    assert isinstance(app, McpApp) and app.configured and app.auth_mode == "static_bearer"
    async with app.lifespan():  # opens the pool without blocking, closes it on exit
        pass


async def test_non_bearer_authorization_is_counted_and_refused(keys: SigningKeys) -> None:
    metrics = AppMetrics(process_metrics=False)
    async with mcp_client(make_settings(), offline_db(), keys, metrics=metrics) as client:
        headers = {**rpc_headers("tools/list"), "Authorization": "Basic c3ludGhldGljOnNlY3JldA=="}
        response = await client.http.post("/mcp", json=envelope("tools/list"), headers=headers)
        assert response.status_code == 401
        assert metrics.registry.get_sample_value(DENIALS, {"surface": "mcp", "reason": "invalid_token"}) == 1


def test_allowed_origins_are_serialized_like_browsers_send_them() -> None:
    settings = make_settings(
        mcp_allowed_origins="https://dash.synthetic.example:443, http://127.0.0.1:80,"
        " https://dash.synthetic.example:8443, http://[::1]:5173, https://*.synthetic.example,"
        " https://bad-port.synthetic.example:99999, https://[::1"
    )
    assert mcp_allowed_origins(settings) == [
        "https://dash.synthetic.example",
        "http://127.0.0.1",
        "https://dash.synthetic.example:8443",
        "http://[::1]:5173",
    ]


async def test_default_port_origin_is_accepted_on_the_wire(keys: SigningKeys) -> None:
    settings = make_settings(mcp_allowed_origins="https://dash.synthetic.example:443")
    async with mcp_client(settings, offline_db(), keys, token_verifier=StubVerifier()) as client:
        headers = {
            **rpc_headers("tools/list", token="synthetic-good-token"),
            "Origin": "https://dash.synthetic.example",
        }
        response = await client.http.post("/mcp", json=envelope("tools/list"), headers=headers)
        assert response.status_code == 200


def test_allowed_hosts_ignore_malformed_urls() -> None:
    settings = make_settings(
        mcp_public_url="https://api.synthetic.example:99999/mcp", app_base_url="https://[::1"
    )
    assert mcp_allowed_hosts(settings) == []


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        (
            {"mcp_auth_mode": "static_bearer", "mcp_public_url": None, "app_base_url": ""},
            "MCP_PUBLIC_URL (or APP_BASE_URL) must be a plain http(s) URL",
        ),
        (
            {"mcp_auth_mode": "static_bearer", "mcp_public_url": None, "app_base_url": "not a url"},
            "MCP_PUBLIC_URL (or APP_BASE_URL) must be a plain http(s) URL",
        ),
        (
            {"mcp_public_url": "https://api.synthetic.example:99999/mcp"},
            "MCP_PUBLIC_URL must be a plain http(s) URL",
        ),
        ({"mcp_public_url": "https://[::1/mcp"}, "MCP_PUBLIC_URL must be a plain http(s) URL"),
        (
            {"mcp_oauth_issuer": "https://issuer.synthetic.example:99999"},
            "MCP_OAUTH_ISSUER must be a plain http(s) URL",
        ),
        (
            {"mcp_auth_mode": "dev_local", "mcp_public_url": "http://[::1/mcp"},
            "MCP_AUTH_MODE=dev_local requires a loopback MCP_PUBLIC_URL/APP_BASE_URL",
        ),
        (
            {"mcp_auth_mode": "dev_local", "mcp_public_url": None, "app_base_url": "http://[::1"},
            "MCP_AUTH_MODE=dev_local requires a loopback MCP_PUBLIC_URL/APP_BASE_URL",
        ),
        ({"mcp_allowed_origins": "https://o.synthetic.example:99999"}, None),
        ({"app_base_url": "https://dash.synthetic.example:99999"}, None),
        (
            {
                "mcp_events_enabled": True,
                "allow_external_notifications": True,
                "mcp_event_subscription_secret_encryption_key": SecretStr(
                    "U1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1M="
                ),
                "callback_egress_proxy_url": "not a url",
            },
            "CALLBACK_EGRESS_PROXY_URL is invalid",
        ),
    ],
)
async def test_malformed_settings_never_crash_the_backend(
    overrides: dict[str, object], problem: str | None, keys: SigningKeys
) -> None:
    """A typo in an MCP setting disables (or narrows) the MCP endpoint; it never raises out of
    ``build_mcp``, which the dashboard backend calls at start-up."""
    app = build_mcp(
        make_settings(**overrides), offline_db(), jwks=keys.jwks, metrics=AppMetrics(process_metrics=False)
    )
    if problem is None:
        assert app.configured  # an unusable allow-list entry is dropped, the rest keeps working
    else:
        assert not app.configured and app.problems == (problem,)
        async with running(app) as http:
            response = await http.post("/mcp", json=envelope("tools/list"), headers=rpc_headers("tools/list"))
            assert response.status_code == 503


async def test_get_never_opens_a_standalone_sse_stream(keys: SigningKeys) -> None:
    """The stateless server sends nothing server-initiated: ``GET /mcp`` is ``405`` in every era
    (the SDK would otherwise hold a handshake-era GET stream open indefinitely)."""
    async with mcp_client(make_settings(), offline_db(), keys, token_verifier=StubVerifier()) as client:
        sse = {"Accept": "text/event-stream"}
        for headers in (
            {**sse, "Authorization": "Bearer synthetic-good-token"},  # handshake era (no version header)
            {**sse, "Authorization": "Bearer synthetic-good-token", "MCP-Protocol-Version": "2025-06-18"},
            {**sse, "Authorization": "Bearer synthetic-good-token", "MCP-Protocol-Version": "2026-07-28"},
            sse,  # unauthenticated: refused without consulting any verifier
        ):
            response = await asyncio.wait_for(client.http.get("/mcp", headers=headers), timeout=10)
            assert response.status_code == 405, response.text
            assert response.headers["allow"] == "POST"
            assert response.json()["error"]["code"] == -32600
            assert response.headers["cache-control"] == "no-store"


async def test_handshake_era_posts_stay_stateless(keys: SigningKeys) -> None:
    async with mcp_client(make_settings(), offline_db(), keys, token_verifier=StubVerifier()) as client:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": "Bearer synthetic-good-token",
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-legacy-client", "version": "0"},
            },
        }
        response = await client.http.post("/mcp", json=initialize, headers=headers)
        assert response.status_code == 200 and "mcp-session-id" not in response.headers
        assert response.json()["result"]["serverInfo"]["name"] == "suv-deals"
        listed = await client.http.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=headers
        )
        assert [t["name"] for t in listed.json()["result"]["tools"]] == [
            "deals_health",
            "deals_list_candidates",
            "deals_get_candidate",
            "deals_get_comparables",
            "deals_get_valuation",
        ]
        unauthenticated = {k: v for k, v in headers.items() if k != "Authorization"}
        assert (await client.http.post("/mcp", json=initialize, headers=unauthenticated)).status_code == 401


async def test_oversized_bodies_are_refused(keys: SigningKeys) -> None:
    options = McpOptions(max_request_body_size=4096)
    async with mcp_client(
        make_settings(), offline_db(), keys, token_verifier=StubVerifier(), options=options
    ) as client:
        body = envelope("tools/call", {"name": "deals_health", "arguments": {"padding": "x" * 8192}})
        headers = rpc_headers("tools/call", "deals_health", "synthetic-good-token")
        response = await client.http.post("/mcp", json=body, headers=headers)
        assert response.status_code == 413
