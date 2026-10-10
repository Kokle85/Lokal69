"""The MCP app mounted at ``/`` (last) inside the dashboard backend (``api.app.create_app``).

The parent FastAPI lifespan runs the SDK session manager (mounted lifespans never run), ``/api``,
``/healthz`` and unknown ``/api`` paths keep precedence, the protected-resource metadata is served
at the root well-known path, and a tool call through the mount uses the backend's request id.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from suv_deals.api.app import create_app, load_mcp_app
from suv_deals.domain.enums import Role
from suv_deals.mcp.server import McpApp, build_mcp
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from tests.integration.db.helpers import Seed
from tests.mcp.conftest import (
    BASE_URL,
    METADATA_URL,
    SigningKeys,
    TokenFactory,
    add_members,
    envelope,
    make_settings,
    offline_db,
    rpc_headers,
)

SUPABASE_URL = "https://synthetic-project.supabase.example"


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """The backend with its lifespan entered and exited in one owner task."""
    started, stop = asyncio.Event(), asyncio.Event()
    failures: list[BaseException] = []

    async def owner() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failures.append(exc)
            started.set()
            raise

    task = asyncio.create_task(owner())
    await started.wait()
    if failures:
        await asyncio.gather(task, return_exceptions=True)
        raise failures[0]
    try:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as http:
            yield http
    finally:
        stop.set()
        await task


def backend(db: Database, keys: SigningKeys, **overrides: Any) -> tuple[FastAPI, McpApp]:
    settings = make_settings(supabase_url=SUPABASE_URL, **overrides)
    metrics = AppMetrics(process_metrics=False)
    mcp = build_mcp(settings, db, jwks=keys.jwks, metrics=metrics)
    app = create_app(settings, db=db, jwks=keys.jwks, mcp_asgi=mcp.asgi_app, metrics=metrics)
    return app, mcp


async def test_mount_keeps_api_precedence_and_serves_mcp(keys: SigningKeys) -> None:
    app, mcp = backend(offline_db(), keys)
    assert mcp.configured and mcp.asgi_app is mcp
    async with serving(app) as http:
        health = await http.get("/healthz")
        assert health.status_code == 200
        unknown = await http.get("/api/nope")
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "NOT_FOUND"
        metadata = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200 and metadata.json()["resource"].endswith("/mcp")
        unauthenticated = await http.post(
            "/mcp", json=envelope("tools/list"), headers=rpc_headers("tools/list")
        )
        assert unauthenticated.status_code == 401
        assert f'resource_metadata="{METADATA_URL}"' in unauthenticated.headers["www-authenticate"]
        assert (
            unauthenticated.headers.get_list("x-request-id")
            and len(unauthenticated.headers.get_list("x-request-id")) == 1
        )
        assert (
            unauthenticated.headers["x-content-type-options"] == "nosniff"
        )  # backend security headers apply
        stream = await asyncio.wait_for(http.get("/mcp", headers={"Accept": "text/event-stream"}), timeout=10)
        assert stream.status_code == 405 and stream.headers["allow"] == "POST"


async def test_load_mcp_app_returns_a_mountable_app_even_when_disabled(keys: SigningKeys) -> None:
    settings = make_settings(mcp_oauth_issuer=None)
    loaded = load_mcp_app(settings, offline_db())
    assert isinstance(loaded, McpApp) and not loaded.configured
    app = create_app(
        settings,
        db=offline_db(),
        jwks=keys.jwks,
        mcp_asgi=loaded,
        metrics=AppMetrics(process_metrics=False),
    )
    async with serving(app) as http:
        response = await http.post("/mcp", json=envelope("tools/list"), headers=rpc_headers("tools/list"))
        assert response.status_code == 503
        assert (await http.get("/healthz")).status_code == 200


@pytest.mark.db
async def test_tool_call_through_the_mount_uses_the_backend_request_id(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("MCP mount")
    users = add_members(seed, workspace)
    app, _ = backend(db, keys)
    token = tokens.for_role(users.viewer, Role.VIEWER)
    async with serving(app) as http:
        headers = {
            **rpc_headers("tools/call", "deals_health", token),
            "X-Request-Id": "req-synthetic-mount-1",
        }
        response = await http.post(
            "/mcp", json=envelope("tools/call", {"name": "deals_health", "arguments": {}}), headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.headers["x-request-id"] == "req-synthetic-mount-1"
        result = response.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["request_id"] == "req-synthetic-mount-1"
        dashboard = await http.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert dashboard.status_code == 401  # an MCP access token is not a dashboard session token
