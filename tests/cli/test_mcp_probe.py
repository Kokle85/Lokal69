"""``suv-deals mcp probe`` against the in-process MCP app (SYNTHETIC data and credentials only).

The app (`mcp.server.build_mcp`, ``MCP_AUTH_MODE=static_bearer``) is served by uvicorn on a free
loopback port in a background thread, so the probe's official SDK client talks real Streamable
HTTP exactly as it would to ``suv-deals api serve``. Tests without a database use an unreachable
pool (the unauthenticated check never touches it) or a verifier that refuses every token; the
``db`` tests mint a viewer credential with ``credentials create-mcp --authorization-file`` and
probe with that file. Nothing reaches a non-loopback address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import mcp.client.streamable_http  # noqa: F401 - its module logger exists before the guard, as in run_probe
import pytest
import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

from suv_deals.cli_commands.mcp_probe import (
    guarded_library_logs,
    health_summary,
    parse_challenge,
    tools_sha256,
)
from suv_deals.domain.enums import Scope
from suv_deals.mcp.auth import McpAccessToken
from suv_deals.mcp.server import build_mcp
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings

#: What a deals:read + reviews:read viewer credential discovers (sorted, as the probe prints them).
VIEWER_TOOLS = sorted(
    [
        "deals_health",
        "deals_list_candidates",
        "deals_get_candidate",
        "deals_get_comparables",
        "deals_get_valuation",
        "reviews_list_pending",
        "deals_estimate_import_costs",
    ]
)
UNREACHABLE_DB = "postgresql://suv:suv@127.0.0.1:1/synthetic_unreachable"
CURSOR_SECRET = "SYNTHETIC-probe-cursor-signing-secret-0123456789abcdef"
UNKNOWN_TOKEN = "suvmcp_" + "0f" * 32  # well-formed, never issued


# --------------------------------------------------------------------------------------------
# In-process server
# --------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def probe_settings(port: int) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "app_base_url": "",
        "build_id": "synthetic-probe.1",
        "mcp_public_url": f"http://127.0.0.1:{port}/mcp",
        "mcp_auth_mode": "static_bearer",
        "mcp_cursor_signing_secret": SecretStr(CURSOR_SECRET),
        "source_network_enabled": False,
        "allow_external_notifications": False,
        "event_bridge_enabled": False,
        "mcp_events_enabled": False,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@contextmanager
def serving(app: Any, port: int) -> Iterator[str]:
    """``app`` under uvicorn on 127.0.0.1:``port`` (lifespan on) in a background thread."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="probe-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


class RefuseAll:
    """A verifier that refuses every token (no database needed)."""

    async def verify_token(self, token: str) -> McpAccessToken | None:
        return None


@contextmanager
def offline_server(**kwargs: Any) -> Iterator[str]:
    port = free_port()
    app = build_mcp(
        probe_settings(port),
        Database(UNREACHABLE_DB, min_size=1, max_size=1),
        metrics=AppMetrics(process_metrics=False),
        **kwargs,
    )
    with serving(app, port) as url:
        yield url


def raw_tools_list(url: str, token: str) -> list[dict[str, Any]]:
    """``tools/list`` as raw JSON-RPC on the 2026-07-28 path (no SDK), for the hash comparison."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    headers = {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    response = httpx2.post(url, json=body, headers=headers, trust_env=False, timeout=10)
    assert response.status_code == 200, response.text
    tools: list[dict[str, Any]] = response.json()["result"]["tools"]
    return tools


def secret_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def write_authorization(
    directory: Path, value: str, *, mode: int = 0o600, name: str = "dot.authorization"
) -> Path:
    path = directory / name
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.write(fd, value.encode("ascii"))
    finally:
        os.close(fd)
    path.chmod(mode)
    return path


def probe(run_cli: Cli, *args: str) -> Any:
    return run_cli("mcp", "probe", *args)


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def test_tools_hash_is_canonical_and_order_independent() -> None:
    tools = [
        {"name": "b_tool", "inputSchema": {"type": "object", "properties": {}}, "description": "Ä"},
        {"name": "a_tool", "description": "x", "inputSchema": {"type": "object"}},
    ]
    canonical = (
        '{"tools":[{"description":"x","inputSchema":{"type":"object"},"name":"a_tool"},'
        '{"description":"Ä","inputSchema":{"properties":{},"type":"object"},"name":"b_tool"}]}'
    )
    assert tools_sha256(tools) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert tools_sha256(list(reversed(tools))) == tools_sha256(tools)
    assert tools_sha256(tools[:1]) != tools_sha256(tools)


def test_challenge_parameters_are_parsed() -> None:
    header = (
        'Bearer error="invalid_token", error_description="Authentication \\"required\\"", scope="a b",x=1'
    )
    assert parse_challenge(header) == {
        "error": "invalid_token",
        "error_description": 'Authentication "required"',
        "scope": "a b",
        "x": "1",
    }
    assert parse_challenge("Bearer") == {}


def test_health_summary_keeps_status_fields_only() -> None:
    structured = {
        "request_id": "req-1",
        "warnings": [{"code": "FIXTURE_DATA", "message": "m"}],
        "data": {
            "build": {"build_id": "b", "version": "1", "app_env": "test"},
            "ready": True,
            "readiness": [{"name": "database", "status": "ok", "detail": "free text"}],
            "source_network_enabled": False,
            "bridge_status": "unavailable",
            "notification_route": "none",
            "sources": [{"state": "disabled", "source_key": "x"}, {"state": "running", "source_key": "y"}],
            "activation_blockers": [{"capability": "c", "next_action": "free text"}],
            "coverage_note": "free text",
        },
    }
    summary = health_summary(structured)
    assert summary == {
        "ready": True,
        "readiness": {"database": "ok"},
        "source_network_enabled": False,
        "bridge_status": "unavailable",
        "notification_route": "none",
        "sources_by_state": {"disabled": 1, "running": 1},
        "activation_blockers": 1,
        "warnings": ["FIXTURE_DATA"],
        "request_id": "req-1",
    }
    assert "free text" not in json.dumps(summary)


def test_library_logs_are_held_down_and_redacted_during_the_probe(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    token = "suvmcp_" + "ef" * 32
    child = logging.getLogger("mcp.client.streamable_http")
    httpx_logger = logging.getLogger("httpx2")
    httpx_logger.setLevel(logging.DEBUG)
    try:
        with guarded_library_logs():
            httpx_logger.debug("request headers: Authorization: Bearer %s", token)
            child.info("Sending client message with %s", token)
            child.warning("Session termination failed: Authorization: Bearer %s", token)
            logging.getLogger("mcp").error("raw %s", token)
        assert httpx_logger.level == logging.DEBUG and not child.filters  # restored
        child.debug("after the probe")
    finally:
        httpx_logger.setLevel(logging.NOTSET)
    messages = [r.getMessage() for r in caplog.records]
    assert token not in caplog.text and all(token not in m for m in messages)
    assert not any("request headers" in m or "Sending client message" in m for m in messages)
    assert any(m.startswith("Session termination failed") and "[REDACTED]" in m for m in messages)
    assert "after the probe" in caplog.text


# --------------------------------------------------------------------------------------------
# Usage errors (exit 2)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/mcp",
        "http://user:pw@127.0.0.1:8000/mcp",
        "http://127.0.0.1:8000/mcp?token=x",
        "http:///mcp",
        "http://127.0.0.1:99999/mcp",
        "not a url",
    ],
)
def test_bad_urls_are_usage_errors(run_cli: Cli, url: str) -> None:
    result = probe(run_cli, "--url", url)
    assert result.exit_code == 2, result.output
    assert "--url" in result.output
    assert "pw@" not in result.output


def test_bad_file_permissions_and_symlinks_are_refused(run_cli: Cli, tmp_path: Path) -> None:
    directory = secret_dir(tmp_path)
    token = "suvmcp_" + "ab" * 32
    readable = write_authorization(directory, f"Bearer {token}", mode=0o644, name="readable")
    refused = probe(run_cli, "--authorization-file", str(readable))
    assert refused.exit_code == 2, refused.output
    assert "accessible to group or others (mode 0644)" in refused.output and "chmod 600" in refused.output
    group_only = write_authorization(directory, f"Bearer {token}", mode=0o640, name="group")
    assert probe(run_cli, "--authorization-file", str(group_only)).exit_code == 2
    good = write_authorization(directory, f"Bearer {token}", name="good")
    link = directory / "link"
    link.symlink_to(good)
    linked = probe(run_cli, "--authorization-file", str(link))
    assert linked.exit_code == 2 and "symlink" in linked.output
    missing = probe(run_cli, "--authorization-file", str(directory / "missing"))
    assert missing.exit_code == 2 and "does not exist" in missing.output
    for result in (refused, linked, missing):
        assert token not in result.output


@pytest.mark.parametrize(
    "content",
    [
        "Bearer suvmcp_" + "ab" * 32 + "\n",  # trailing newline
        "bearer suvmcp_" + "ab" * 32,
        "suvmcp_" + "ab" * 32,
        "Bearer suvmail_" + "ab" * 32,  # a mailbox-worker credential is never an MCP credential
        "Bearer suvdev_" + "ab" * 32,
        "Bearer suvmcp_" + "AB" * 32,
        "Bearer not-a-token",
        "Bearer a.b",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_malformed_files_are_refused_without_echoing_them(run_cli: Cli, tmp_path: Path, content: str) -> None:
    path = write_authorization(secret_dir(tmp_path), content)
    result = probe(run_cli, "--authorization-file", str(path))
    assert result.exit_code == 2, result.output
    assert "must contain exactly the word Bearer, one space and a suvmcp_ token" in result.output
    assert "content not shown" in result.output
    secret_part = content.rsplit(" ", 1)[-1].strip()
    assert secret_part not in result.output


def test_a_file_is_never_sent_over_plain_http_to_another_host(run_cli: Cli, tmp_path: Path) -> None:
    path = write_authorization(secret_dir(tmp_path), "Bearer suvmcp_" + "ab" * 32)
    result = probe(run_cli, "--url", "http://mcp.synthetic.example/mcp", "--authorization-file", str(path))
    assert result.exit_code == 2
    assert "only over https or to a loopback address" in result.output


def test_timeout_is_bounded(run_cli: Cli) -> None:
    assert probe(run_cli, "--timeout", "0").exit_code == 2
    assert probe(run_cli, "--timeout", "1000").exit_code == 2


# --------------------------------------------------------------------------------------------
# Against the in-process app without a database
# --------------------------------------------------------------------------------------------


def test_unauthenticated_only_run_without_the_file(run_cli: Cli) -> None:
    with offline_server() as url:
        result = probe(run_cli, "--url", url)
        assert result.exit_code == 0, result.output
        assert "PASS  unauthenticated initialize: HTTP 401 with WWW-Authenticate: Bearer" in result.output
        assert 'error="invalid_token"' in result.output and 'scope="deals:read' in result.output
        assert (
            "SKIP  initialize, tools/list, tools/call deals_health: no --authorization-file" in result.output
        )
        assert "Result: all 1 check(s) passed (authenticated checks skipped)" in result.output
        assert "Request ids: req-" in result.output

        report = json.loads(probe(run_cli, "--url", url, "--json").stdout)
        assert report["passed"] is True and report["failed_checks"] == []
        assert report["authenticated_checks"] == "skipped (no --authorization-file)"
        assert report["challenge"]["error"] == "invalid_token"
        assert "resource_metadata" not in report["challenge"]  # static_bearer advertises no OAuth
        assert report["tools"] == [] and report["tools_sha256"] is None
        assert len(report["request_ids"]) == 1 and report["request_ids"][0].startswith("req-")


def test_wrong_token_fails_initialize_with_a_clear_message(
    run_cli: Cli, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx2").setLevel(logging.DEBUG)  # the probe must hold it down itself
    path = write_authorization(secret_dir(tmp_path), f"Bearer {UNKNOWN_TOKEN}")
    with offline_server(token_verifier=RefuseAll()) as url:
        result = probe(run_cli, "--url", url, "--authorization-file", str(path))
    assert result.exit_code == 1, result.output
    assert "PASS  unauthenticated initialize" in result.output
    assert (
        "FAIL  initialize: HTTP 401: the server refused the credential (unknown, revoked or expired token"
        in result.output
    )
    assert "Result: FAILED (1 of 2 checks): initialize" in result.output
    assert UNKNOWN_TOKEN not in result.output and UNKNOWN_TOKEN not in caplog.text
    assert logging.getLogger("httpx2").level == logging.DEBUG  # restored afterwards
    logging.getLogger("httpx2").setLevel(logging.NOTSET)


def test_an_unreachable_server_fails_the_first_check(run_cli: Cli) -> None:
    url = f"http://127.0.0.1:{free_port()}/mcp"  # nothing listens there
    result = probe(run_cli, "--url", url, "--timeout", "2")
    assert result.exit_code == 1
    assert "FAIL  unauthenticated initialize: no answer from" in result.output
    assert "cannot connect" in result.output


def test_an_open_endpoint_fails_the_401_check(run_cli: Cli) -> None:
    async def open_endpoint(_: Request) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})

    port = free_port()
    app = Starlette(routes=[Route("/mcp", open_endpoint, methods=["POST"])])
    with serving(app, port) as url:
        result = probe(run_cli, "--url", url)
    assert result.exit_code == 1
    assert (
        "FAIL  unauthenticated initialize: HTTP 200 WITHOUT authentication: the endpoint is open"
        in result.output
    )


def test_an_unconfigured_endpoint_reports_503(run_cli: Cli) -> None:
    port = free_port()
    settings = probe_settings(port).model_copy(update={"mcp_auth_mode": "oauth", "mcp_oauth_issuer": None})
    app = build_mcp(settings, Database(UNREACHABLE_DB), metrics=AppMetrics(process_metrics=False))
    with serving(app, port) as url:
        result = probe(run_cli, "--url", url)
    assert result.exit_code == 1
    assert "HTTP 503, expected 401 (unavailable" in result.output
    assert "server says: The MCP endpoint is not configured" in result.output


# --------------------------------------------------------------------------------------------
# End to end with a real viewer credential (marker db)
# --------------------------------------------------------------------------------------------


def _deals_health_calls(metrics: AppMetrics) -> float:
    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if (
                sample.name == "suv_deals_request_duration_seconds_count"
                and sample.labels.get("surface") == "mcp"
                and sample.labels.get("route") == "deals_health"
            ):
                total += sample.value
    return total


@pytest.mark.db
def test_probe_with_a_viewer_credential_end_to_end(
    run_cli: Cli,
    db_env: dict[str, str],
    db_url: str,
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    path = secret_dir(tmp_path) / "dot.authorization"
    created = run_cli(
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "probe viewer (synthetic)",
        "--scopes",
        "deals:read,reviews:read",
        "--role",
        "viewer",
        "--expires",
        "30d",
        "--authorization-file",
        str(path),
        "--yes",
        env=db_env,
    )
    assert created.exit_code == 0, created.output
    token = path.read_text(encoding="ascii").removeprefix("Bearer ")
    assert token.startswith("suvmcp_")

    port = free_port()
    metrics = AppMetrics(process_metrics=False)
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=4)
    app = build_mcp(probe_settings(port), database, metrics=metrics, manage_db=True)
    with serving(app, port) as url:
        first = probe(run_cli, "--url", url, "--authorization-file", str(path), "--json")
        second = probe(run_cli, "--url", url, "--authorization-file", str(path), "--json")
        text = probe(run_cli, "--url", url, "--authorization-file", str(path))
        calls = _deals_health_calls(metrics)
        wire_tools = raw_tools_list(url, token)

    for result in (created, first, second, text):
        assert token not in result.output
        assert token not in result.stdout and token not in result.stderr
    assert token not in caplog.text
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0 and text.exit_code == 0, text.output

    report = json.loads(first.stdout)
    again = json.loads(second.stdout)
    assert report["passed"] is True and report["failed_checks"] == []
    assert [c["name"] for c in report["checks"]] == [
        "unauthenticated initialize",
        "initialize",
        "tools/list",
        "tools/call deals_health",
    ]
    assert report["challenge"]["error"] == "invalid_token"
    assert report["protocol_version"] == "2025-11-25"  # the initialize handshake
    assert report["server"]["name"] == "suv-deals"
    assert report["tools"] == VIEWER_TOOLS  # exactly the 7 read tools
    assert len(report["tools_sha256"]) == 64
    assert again["tools_sha256"] == report["tools_sha256"]  # stable across runs
    # ... and it is the hash of the server's own tools/list wire result for this credential.
    assert tools_sha256(wire_tools) == report["tools_sha256"]
    health = report["deals_health"]
    assert health["ready"] is True and health["readiness"] == {
        "config": "ok",
        "database": "ok",
        "schema": "ok",
    }
    assert health["source_network_enabled"] is False
    assert health["request_id"] in report["request_ids"]
    assert len(report["request_ids"]) >= 4 and len(set(report["request_ids"])) == len(report["request_ids"])
    assert calls == 3  # deals_health really ran once per probe

    assert "PASS  initialize: protocol 2025-11-25; server suv-deals" in text.output
    assert "PASS  tools/list: 7 tool(s)" in text.output
    assert f"sha256 : {report['tools_sha256']} (canonical tools/list result)" in text.output
    assert "PASS  tools/call deals_health: ready=true (config=ok, database=ok, schema=ok)" in text.output
    assert "Result: all 4 check(s) passed" in text.output
    last_used = seed.scalar(
        "select last_used_at is not null from ops.api_credentials where workspace_id = %s", (workspace,)
    )
    assert last_used is True


@pytest.mark.db
def test_unknown_and_revoked_tokens_fail_with_exit_1(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID, tmp_path: Path
) -> None:
    directory = secret_dir(tmp_path)
    unknown = write_authorization(directory, f"Bearer {UNKNOWN_TOKEN}", name="unknown")
    revoked_path = directory / "revoked"
    created = run_cli(
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "probe revoked (synthetic)",
        "--scopes",
        "deals:read",
        "--role",
        "viewer",
        "--authorization-file",
        str(revoked_path),
        "--yes",
        env=db_env,
    )
    assert created.exit_code == 0, created.output
    credential_id = created.output.split("Created credential ", 1)[1].split(" ", 1)[0]
    revoked = run_cli(
        "credentials",
        "revoke",
        credential_id,
        "--workspace",
        str(workspace),
        "--reason",
        "synthetic probe test",
        "--yes",
        env=db_env,
    )
    assert revoked.exit_code == 0, revoked.output
    revoked_token = revoked_path.read_text(encoding="ascii").removeprefix("Bearer ")

    port = free_port()
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=2)
    app = build_mcp(probe_settings(port), database, metrics=AppMetrics(process_metrics=False), manage_db=True)
    with serving(app, port) as url:
        for path, token in ((unknown, UNKNOWN_TOKEN), (revoked_path, revoked_token)):
            result = probe(run_cli, "--url", url, "--authorization-file", str(path))
            assert result.exit_code == 1, result.output
            assert "FAIL  initialize: HTTP 401: the server refused the credential" in result.output
            assert token not in result.output


@pytest.mark.db
def test_a_credential_without_deals_read_fails_tools_list(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID, tmp_path: Path
) -> None:
    path = secret_dir(tmp_path) / "reviews-only"
    created = run_cli(
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "probe reviews only (synthetic)",
        "--scopes",
        Scope.REVIEWS_READ.value,
        "--role",
        "viewer",
        "--authorization-file",
        str(path),
        "--yes",
        env=db_env,
    )
    assert created.exit_code == 0, created.output
    port = free_port()
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=2)
    app = build_mcp(probe_settings(port), database, metrics=AppMetrics(process_metrics=False), manage_db=True)
    with serving(app, port) as url:
        result = probe(run_cli, "--url", url, "--authorization-file", str(path))
    assert result.exit_code == 1, result.output
    assert (
        "FAIL  tools/list: 1 tool(s) but no deals_health (the credential lacks deals:read)" in result.output
    )
    assert "tools  : reviews_list_pending" in result.output
