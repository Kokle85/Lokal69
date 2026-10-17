"""Process commands: safe defaults and extension points (no server is started, no network)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
import yaml
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

from suv_deals.cli_commands import processes
from suv_deals.cli_commands.processes import DEFAULT_APP_FACTORY, DEFAULT_REGISTRY, load_factory
from suv_deals.domain.enums import JobType
from suv_deals.errors import SourcePaused, ValidationFailed
from suv_deals.persistence.transactions import job_unit_of_work
from suv_deals.workers.handlers import HandlerRegistry
from suv_deals.workers.runtime import Disposition, JobExecution, JobOutcome, RuntimeContext, apply_disposition

REPO = Path(__file__).resolve().parents[2]
FIXTURE_SOURCE = REPO / "tests" / "adapters" / "fixtures" / "fixture_dealer_de" / "source.yaml"
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


def test_worker_source_requires_drain(run_cli: Cli) -> None:
    result = run_cli("worker", "--source", "autoscout24_it", env=DEV)
    assert result.exit_code == 2
    assert "--source requires --drain" in result.output


@pytest.mark.parametrize("key", ["not_a_configured_source", "AutoScout24-IT", "https://example.com/x"])
def test_worker_source_must_be_in_the_yaml_registry(run_cli: Cli, key: str) -> None:
    # The fake DATABASE_URL is never reached: the key is checked against config/sources first.
    result = run_cli("worker", "--drain", "--queues", "detail", "--source", key, env=DEV)
    assert result.exit_code == 2
    assert "not a source of the YAML registry" in result.output
    assert "autoscout24_it" in result.output  # the configured keys are listed
    assert "database" not in result.output.lower()


def test_worker_source_of_the_registry_passes_the_check(run_cli: Cli) -> None:
    # A configured (even disabled) source passes; the drain then needs its database.
    result = run_cli("worker", "--drain", "--queues", "detail", "--source", "autoscout24_de")
    assert result.exit_code != 0
    assert "not a source of the YAML registry" not in result.output
    assert "DATABASE_URL" in result.output


def test_worker_help_lists_the_source_option(run_cli: Cli) -> None:
    result = run_cli("worker", "--help")
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "--source KEY" in text
    assert (
        "With --drain: claim only jobs of this source (an owner smoke); jobs of other sources stay queued."
        in text
    )


@pytest.mark.db
def test_worker_drain_with_source_claims_only_that_sources_jobs(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's smoke drain through the real command (runbook 9.1, ``worker --drain --source``).

    The other source stands for ``autoscout24_it`` after its smokes: switched off with older detail
    jobs still queued, which its handler would block ``source_paused``. Only the named source's jobs
    are claimed (in claim order, within ``--max-jobs``); the other source's job and a source-less
    job stay queued, untouched; an empty drain names the source. The handlers are SYNTHETIC (no
    network): ``--registry`` accepts only ``suv_deals.*`` factories, so the factory is replaced.
    """
    smoke_key = f"synthetic_smoke_{uuid4().hex[:8]}"  # unique: the session database is shared
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    source_yaml = yaml.safe_load(FIXTURE_SOURCE.read_text(encoding="utf-8"))
    source_yaml["source_key"] = smoke_key
    (config / "sources" / f"{smoke_key}.yaml").write_text(yaml.safe_dump(source_yaml), encoding="utf-8")
    env = {**db_env, "CONFIG_DIR": str(config), "LOG_LEVEL": "WARNING"}

    smoke = seed.source(workspace, source_key=smoke_key)
    left = seed.source(workspace)  # disabled again, detail jobs still queued

    def detail_job(source_id: UUID, **cols: Any) -> UUID:
        listing = seed.listing(workspace, source_id)
        return seed.job(
            workspace, job_type="detail", source_id=source_id, listing_id=listing, generation=1, **cols
        )

    day_ago = seed.scalar("select now() - interval '1 day'")
    leftover = detail_job(left, priority=100, available_at=day_ago)  # older AND higher priority
    sourceless = seed.job(workspace, job_type="valuation", priority=1000)  # no source_id
    smoke_first = detail_job(smoke, priority=50)
    smoke_second = detail_job(smoke)

    handled: list[UUID] = []

    async def handler(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        handled.append(execution.job.id)
        if execution.job.source_id not in (None, smoke):
            raise SourcePaused("synthetic: the left-over source is disabled")
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            state = await apply_disposition(conn, execution.job, Disposition.complete({"synthetic": True}))
        return JobOutcome(state=state)

    registry = HandlerRegistry()
    for job_type in (JobType.DETAIL, JobType.VALUATION):
        registry.register(job_type, handler, description="synthetic test handler")
    real_load_factory = processes.load_factory

    def load(spec: str, *, option: str) -> Any:
        return (lambda: registry) if option == "--registry" else real_load_factory(spec, option=option)

    monkeypatch.setattr(processes, "load_factory", load)

    def state(job_id: UUID) -> str:
        return str(seed.scalar("select state from ops.jobs where id = %s", (job_id,)))

    def assert_untouched() -> None:
        for job_id in (leftover, sourceless):
            row = seed.conn.execute(
                "select state, attempts, blocker_code, lease_token from ops.jobs where id = %s", (job_id,)
            ).fetchone()
            assert row == ("queued", 0, None, None), (job_id, row)

    drain = ("worker", "--drain", "--queues", "detail,valuation", "--source", smoke_key)
    first = run_cli(*drain, "--max-jobs", "1", env=env)
    assert first.exit_code == 0, first.output
    assert str(smoke_first) in first.stdout
    assert all(str(j) not in first.stdout for j in (smoke_second, leftover, sourceless))
    assert (state(smoke_first), state(smoke_second)) == ("succeeded", "queued")
    assert_untouched()

    rest = run_cli(*drain, env=env)
    assert rest.exit_code == 0, rest.output
    assert str(smoke_second) in rest.stdout and str(leftover) not in rest.stdout
    assert state(smoke_second) == "succeeded"
    assert_untouched()

    idle = run_cli(*drain, env=env)
    assert idle.exit_code == 0, idle.output
    assert idle.stdout.strip() == f"No due jobs of source {smoke_key}."
    assert handled == [smoke_first, smoke_second]
    assert_untouched()


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


def test_api_serve_reports_app_factory_errors_without_a_traceback(
    run_cli: Cli, monkeypatch: pytest.MonkeyPatch, captured_uvicorn: dict[str, Any]
) -> None:
    def refusing_factory(settings: Any) -> Any:
        raise ValidationFailed("unsafe production configuration", details={"problems": ["CORS origin"]})

    monkeypatch.setattr(processes, "load_factory", lambda spec, *, option: refusing_factory)
    result = run_cli("api", "serve", env=DEV)
    assert result.exit_code == 1
    assert "VALIDATION_ERROR: unsafe production configuration" in result.output
    assert "Traceback" not in result.output
    assert captured_uvicorn == {}
