"""Server assembly details: Host/Origin allow-lists, the standalone factory and guard behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from suv_deals.mcp.server import McpApp, create_standalone_app, mcp_allowed_hosts, mcp_allowed_origins
from suv_deals.observability.metrics import AppMetrics
from tests.mcp.conftest import (
    SigningKeys,
    envelope,
    make_settings,
    mcp_client,
    offline_db,
    rpc_headers,
)

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
