"""v1.1 runtime wiring: API_ALLOWED_HOSTS in the Host check, the private metrics server and the
database pool timeout (``Database.from_settings``). Only loopback sockets are used; no real
service is contacted."""

from __future__ import annotations

import logging
import time
import urllib.request

import pytest
from pydantic import SecretStr

from suv_deals.api import app as app_module
from suv_deals.api.app import PrivateMetricsServer, allowed_hosts, create_app, start_private_metrics_server
from suv_deals.errors import DependencyUnavailable
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings


def _settings(**values: object) -> Settings:
    return Settings.model_construct(**{**Settings().model_dump(), **values})


# --------------------------------------------------------------------------- Host allow-list


def test_api_allowed_hosts_extend_the_host_check() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_base_url="https://deals.example.invalid",
        api_allowed_hosts="API-internal.example.invalid, 10.0.0.7 ,probe",
    )
    hosts = allowed_hosts(settings)
    assert {"deals.example.invalid", "127.0.0.1", "localhost"} <= set(hosts)
    assert {"api-internal.example.invalid", "10.0.0.7", "probe"} <= set(hosts)
    # An explicit override (tests, special deployments) replaces the whole list.
    assert allowed_hosts(settings, ["only.example.invalid"]) == ["only.example.invalid"]


def test_invalid_extra_hosts_are_dropped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(
        app_base_url="https://deals.example.invalid",
        api_allowed_hosts="good.example.invalid,bad host,under_score.example.invalid,-lead.example.invalid",
    )
    with caplog.at_level(logging.WARNING, logger="suv_deals.api"):
        hosts = allowed_hosts(settings)
    assert "good.example.invalid" in hosts
    assert not any(" " in h or "_" in h or h.startswith("-") for h in hosts)
    assert "invalid API_ALLOWED_HOSTS entry" in caplog.text
    assert "bad host" not in caplog.text  # the rejected value is never logged


def test_wildcards_never_reach_the_allow_list() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        allowed_hosts(_settings(), ["*"])
    # A wildcard smuggled past validation (model_construct) is not added either.
    hosts = allowed_hosts(_settings(api_allowed_hosts="*.example.invalid"))
    assert not any("*" in h for h in hosts)


# --------------------------------------------------------------------------- private metrics


def test_metrics_server_is_off_by_default() -> None:
    assert start_private_metrics_server(_settings()) is None


def test_private_metrics_server_serves_only_the_registry_on_its_bind() -> None:
    metrics = AppMetrics(process_metrics=False)
    metrics.build_info.labels(version="test", app_env="test").set(1)
    server = start_private_metrics_server(
        _settings(metrics_enabled=True, metrics_bind="127.0.0.1:0"), metrics
    )
    assert server is not None
    try:
        assert server.host == "127.0.0.1" and server.port > 0
        assert server.thread.daemon
        url = f"http://127.0.0.1:{server.port}/metrics"  # loopback only
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8")
        assert "build_info" in body
    finally:
        server.close()
    assert not server.thread.is_alive()


def test_metrics_bind_conflict_is_an_os_error() -> None:
    first = start_private_metrics_server(_settings(metrics_enabled=True, metrics_bind="127.0.0.1:0"))
    assert first is not None
    try:
        with pytest.raises(OSError):
            start_private_metrics_server(
                _settings(metrics_enabled=True, metrics_bind=f"127.0.0.1:{first.port}"),
                AppMetrics(process_metrics=False),
            )
    finally:
        first.close()


class _FailingDatabase:
    """A caller-supplied database whose start-up fails (the app manages its lifecycle)."""

    def __init__(self) -> None:
        self.closed = False

    async def open(self, wait: bool = True, open_timeout_s: float = 10.0) -> None:
        raise DependencyUnavailable("database unavailable")

    async def close(self) -> None:
        self.closed = True


async def test_failed_start_up_never_leaves_the_metrics_listener_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[PrivateMetricsServer] = []
    real_start = app_module.start_private_metrics_server

    def recording_start(settings: Settings, metrics: AppMetrics | None = None) -> PrivateMetricsServer | None:
        server = real_start(settings, metrics)
        if server is not None:
            started.append(server)
        return server

    monkeypatch.setattr(app_module, "start_private_metrics_server", recording_start)
    app = create_app(
        _settings(metrics_enabled=True, metrics_bind="127.0.0.1:0", supabase_url=None),
        db=_FailingDatabase(),  # type: ignore[arg-type]
        manage_db=True,
        metrics=AppMetrics(process_metrics=False),
    )
    with pytest.raises(DependencyUnavailable):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover - start-up fails before serving
    assert len(started) == 1
    assert not started[0].thread.is_alive()
    # The port is free again: a new listener can bind it.
    again = start_private_metrics_server(
        _settings(metrics_enabled=True, metrics_bind=f"127.0.0.1:{started[0].port}"),
        AppMetrics(process_metrics=False),
    )
    assert again is not None
    again.close()


# --------------------------------------------------------------------------- database pool timeout


@pytest.mark.parametrize("value", [0, -1, 121])
def test_database_refuses_an_unbounded_pool_timeout(value: float) -> None:
    with pytest.raises(ValueError, match="pool_timeout_s"):
        Database("postgresql://synthetic@127.0.0.1:1/none", pool_timeout_s=value)


def test_from_settings_needs_a_url_and_passes_the_timeout() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Database.from_settings(_settings(database_url=None), application_name="test")
    db = Database.from_settings(
        _settings(
            database_url=SecretStr("postgresql://synthetic@127.0.0.1:1/none"),
            database_pool_timeout_s=0.5,
            database_set_role="suv_backend",
        ),
        application_name="test",
    )
    assert db.pool_timeout_s == 0.5


async def test_unreachable_database_fails_fast_with_dependency_unavailable() -> None:
    # Port 1 on loopback refuses connections: the pool never gets a connection.
    db = Database("postgresql://synthetic@127.0.0.1:1/none", pool_timeout_s=0.5, min_size=1, max_size=1)
    await db.open(wait=False)
    started = time.monotonic()
    try:
        with pytest.raises(DependencyUnavailable):
            async with db.transaction():
                pass
    finally:
        await db.close()
    assert time.monotonic() - started < 5
