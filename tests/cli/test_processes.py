"""Process commands: safe defaults and extension points (no server is started, no network)."""

from __future__ import annotations

from typing import Any

import pytest
import uvicorn
from tests.cli.conftest import Cli

from suv_deals.cli_commands.processes import DEFAULT_APP_FACTORY, DEFAULT_REGISTRY, load_factory

DEV = {"APP_ENV": "development", "DATABASE_URL": "postgresql://fake:FAKE-pw@127.0.0.1:1/none"}


@pytest.fixture
def captured_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return captured


def test_api_serve_defaults_to_loopback_and_trusted_proxy_only(
    run_cli: Cli, captured_uvicorn: dict[str, Any]
) -> None:
    result = run_cli("api", "serve", env=DEV)
    assert result.exit_code == 0, result.output
    assert captured_uvicorn["host"] == "127.0.0.1"
    assert captured_uvicorn["port"] == 8000
    assert captured_uvicorn["proxy_headers"] is True
    assert captured_uvicorn["forwarded_allow_ips"] == "127.0.0.1"
    assert captured_uvicorn["log_config"] is None  # the application's redacting JSON logging stays
    assert captured_uvicorn["server_header"] is False
    assert captured_uvicorn["app"] is not None


def test_api_serve_refuses_a_public_bind_without_opt_in(
    run_cli: Cli, captured_uvicorn: dict[str, Any]
) -> None:
    result = run_cli("api", "serve", "--host", "0.0.0.0", env=DEV)
    assert result.exit_code == 3
    assert "--allow-non-loopback" in result.output
    assert captured_uvicorn == {}
    allowed = run_cli("api", "serve", "--host", "0.0.0.0", "--allow-non-loopback", env=DEV)
    assert allowed.exit_code == 0, allowed.output
    assert captured_uvicorn["host"] == "0.0.0.0"


def test_api_serve_warns_about_trusting_every_proxy(run_cli: Cli, captured_uvicorn: dict[str, Any]) -> None:
    result = run_cli("api", "serve", "--forwarded-allow-ips", "*", env=DEV)
    assert result.exit_code == 0
    assert "trusts forwarded headers from any client" in result.output


@pytest.mark.parametrize(
    "factory", ["os:system", "json:loads", "suv_deals.api.app", "suv_deals.nope:build", "x"]
)
def test_factories_outside_the_package_are_refused(run_cli: Cli, factory: str) -> None:
    result = run_cli("api", "serve", "--app-factory", factory, env=DEV)
    assert result.exit_code == 2
    assert "--app-factory" in result.output
    assert run_cli("worker", "--registry", factory).exit_code == 2


def test_default_factories_are_importable() -> None:
    assert callable(load_factory(DEFAULT_APP_FACTORY, option="--app-factory"))
    registry = load_factory(DEFAULT_REGISTRY, option="--registry")()
    assert {t.value for t in registry.job_types()} >= {"discovery", "detail", "recheck", "valuation"}


def test_worker_queue_validation(run_cli: Cli) -> None:
    unknown = run_cli("worker", "--queues", "discovery,mail_send")
    assert unknown.exit_code == 2
    assert "unknown queue" in unknown.output
    unregistered = run_cli("worker", "--queues", "stale_sweep")
    assert unregistered.exit_code == 2
    assert "no handler is registered" in unregistered.output


def test_api_serve_needs_a_database_url(run_cli: Cli, captured_uvicorn: dict[str, Any]) -> None:
    result = run_cli("api", "serve")
    assert result.exit_code == 2
    assert "DATABASE_URL is not configured" in result.output
    assert captured_uvicorn == {}


def test_worker_needs_a_database_url(run_cli: Cli) -> None:
    result = run_cli("worker", "--drain")
    assert result.exit_code != 0
    assert "DATABASE_URL" in result.output


def test_reconcile_flags_are_exclusive(run_cli: Cli) -> None:
    assert run_cli("reconcile", "--dry-run", "--loop").exit_code == 2


def test_dispatcher_warns_when_external_notifications_are_off(run_cli: Cli) -> None:
    result = run_cli("dispatcher", "--once")
    assert "ALLOW_EXTERNAL_NOTIFICATIONS=false" in result.output
    assert result.exit_code != 0  # no DATABASE_URL in this test
