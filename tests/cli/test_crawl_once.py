"""``crawl once``: only through every source and runtime gate; no URL; bounded pages (spec 9, 27).

The only source that may run in these tests is the SYNTHETIC ``fixture_dealer_de`` (reserved
host ``dealer.example``, saved files, ``mode: fixture``): nothing is fetched from a network.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

REPO = Path(__file__).resolve().parents[2]
FIXTURE_SOURCE = REPO / "tests" / "adapters" / "fixtures" / "fixture_dealer_de" / "source.yaml"
SYNTHETIC_TERMS = {
    "terms_status": "permitted",
    "terms_decision": "proceed_permitted",
    "terms_decision_actor": "synthetic-fixture",
    "terms_reviewed_at": "2026-10-06T00:00:00Z",
    "terms_decision_note": "SYNTHETIC fixture source on a reserved example host; no real provider",
}


def _config_with(tmp_path: Path, **source: object) -> Path:
    """A copy of config/ plus one source YAML (default: the enabled SYNTHETIC fixture dealer)."""
    target = tmp_path / "config"
    shutil.copytree(REPO / "config", target)
    data = yaml.safe_load(FIXTURE_SOURCE.read_text(encoding="utf-8"))
    data.update({"enabled": True, **SYNTHETIC_TERMS})
    data.update(source)
    (target / "sources" / f"{data['source_key']}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def test_crawl_once_refuses_a_disabled_gated_source(run_cli: Cli) -> None:
    result = run_cli(
        "crawl", "once", "--source", "autoscout24_de", "--profile", "primary", "--max-pages", "1"
    )
    assert result.exit_code == 3
    assert "refused: source autoscout24_de is not active" in result.output
    assert "terms decision is pending" in result.output


def test_crawl_once_refuses_a_real_source_while_the_network_is_disabled(run_cli: Cli, tmp_path: Path) -> None:
    config = _config_with(
        tmp_path,
        source_key="synthetic_real_dealer",
        mode="public_html",
        allowed_hosts=["dealer.example"],
    )
    result = run_cli("crawl", "once", "--source", "synthetic_real_dealer", env={"CONFIG_DIR": str(config)})
    assert result.exit_code == 3
    assert "SOURCE_NETWORK_ENABLED=false blocks every real fetch" in result.output


def test_crawl_once_unknown_source_needs_the_database(run_cli: Cli) -> None:
    # Not in the YAML registry: the runtime registry decides, which needs DATABASE_URL.
    result = run_cli("crawl", "once", "--source", "not_registered_anywhere")
    assert result.exit_code != 0
    assert "DATABASE_URL" in result.output or "error" in result.output


# --------------------------------------------------------------------------------------------
# Database (marker db)
# --------------------------------------------------------------------------------------------


@pytest.mark.db
def test_crawl_once_refuses_a_gated_runtime_source(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    ws = str(workspace)
    synced = run_cli("sources", "sync", "--workspace", ws, "--fixture-sources", "--yes", env=db_env)
    assert synced.exit_code == 0, synced.output
    result = run_cli("crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, env=db_env)
    assert result.exit_code == 3
    assert "source is not enabled in the runtime registry" in result.output
    assert "terms decision is pending" in result.output


@pytest.mark.db
def test_crawl_once_runs_one_bounded_fixture_discovery(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    env = {**db_env, "CONFIG_DIR": str(_config_with(tmp_path)), "LOG_LEVEL": "WARNING"}
    ws = str(workspace)
    no_config = run_cli("sources", "sync", "--workspace", ws, "--yes", env=env)
    assert no_config.exit_code == 0, no_config.output
    # Without a recorded business configuration the run is refused before anything is enqueued.
    refused = run_cli("crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, env=env)
    assert refused.exit_code == 1
    assert "no business configuration is recorded" in refused.output
    applied = run_cli(
        "config", "apply", "--workspace", ws, "--reason", "synthetic test config", "--yes", env=env
    )
    assert applied.exit_code == 0, applied.output

    result = run_cli(
        "crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, "--max-pages", "1", env=env
    )
    assert result.exit_code == 0, result.output
    assert "Job state: succeeded" in result.output
    assert "completeness: budget_limited" in result.output  # two fixture pages exist; the cap is one
    assert "pages: 1" in result.output
    runs = seed.scalar(
        "select count(*) from ops.crawl_runs r join app.sources s on s.workspace_id = r.workspace_id"
        " and s.id = r.source_id where r.workspace_id = %s and s.source_key = 'fixture_dealer_de'",
        (workspace,),
    )
    assert runs == 1
    job = seed.conn.execute(
        "select state, priority, max_attempts from ops.jobs"
        " where workspace_id = %s and job_type = 'discovery'",
        (workspace,),
    ).fetchall()
    assert job == [("succeeded", 1000, 1)]


@pytest.mark.db
def test_crawl_once_never_exceeds_the_source_page_budget(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, tmp_path: Path
) -> None:
    config = _config_with(tmp_path, rate_budget={"max_search_pages_per_run": 1})
    env = {**db_env, "CONFIG_DIR": str(config), "LOG_LEVEL": "WARNING"}
    ws = str(workspace)
    assert run_cli("sources", "sync", "--workspace", ws, "--yes", env=env).exit_code == 0
    assert (
        run_cli("config", "apply", "--workspace", ws, "--reason", "synthetic", "--yes", env=env).exit_code
        == 0
    )
    result = run_cli(
        "crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, "--max-pages", "20", env=env
    )
    assert result.exit_code == 0, result.output
    assert "--max-pages 20 exceeds the source budget; capped to 1." in result.output
    assert "pages: 1" in result.output


@pytest.mark.db
def test_crawl_once_refuses_an_access_blocked_source(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    env = {**db_env, "CONFIG_DIR": str(_config_with(tmp_path))}
    ws = str(workspace)
    assert run_cli("sources", "sync", "--workspace", ws, "--yes", env=env).exit_code == 0
    assert (
        run_cli("config", "apply", "--workspace", ws, "--reason", "synthetic", "--yes", env=env).exit_code
        == 0
    )
    seed.conn.execute(
        "update app.sources set technical_status = 'access_blocked', enabled = false, version = version + 1"
        " where workspace_id = %s and source_key = 'fixture_dealer_de'",
        (workspace,),
    )
    result = run_cli("crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, env=env)
    assert result.exit_code == 3
    assert "access_blocked" in result.output
    assert seed.scalar("select count(*) from ops.jobs where workspace_id = %s", (workspace,)) == 0


@pytest.mark.db
def test_crawl_once_cancels_its_job_when_another_job_is_claimed_first(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    """The page cap exists only in-process: a job this command did not run must not stay queued
    for a background worker, which would run it with the source's full per-run page budget."""
    env = {**db_env, "CONFIG_DIR": str(_config_with(tmp_path)), "LOG_LEVEL": "WARNING"}
    ws = str(workspace)
    assert run_cli("sources", "sync", "--workspace", ws, "--yes", env=env).exit_code == 0
    applied = run_cli("config", "apply", "--workspace", ws, "--reason", "synthetic", "--yes", env=env)
    assert applied.exit_code == 0, applied.output
    source_id = seed.scalar(
        "select id from app.sources where workspace_id = %s and source_key = 'fixture_dealer_de'",
        (workspace,),
    )
    profile_id = seed.scalar(
        "select id from app.search_profiles where workspace_id = %s and profile_key = 'primary'", (workspace,)
    )
    # An older due discovery job with the same top priority (e.g. left by an earlier run).
    earlier = seed.job(
        workspace,
        job_type="discovery",
        priority=1000,
        max_attempts=1,
        available_at=datetime.now(UTC) - timedelta(minutes=5),
        payload={"source_id": str(source_id), "profile_id": str(profile_id), "partition_key": "default"},
        source_id=source_id,
        profile_id=profile_id,
        partition_key="default",
    )
    result = run_cli(
        "crawl", "once", "--source", "fixture_dealer_de", "--workspace", ws, "--max-pages", "1", env=env
    )
    assert result.exit_code == 1, result.output
    assert f"another due discovery job ({earlier}) was claimed first" in result.output
    assert "was cancelled so that no other worker runs it without the page cap" in result.output
    states = dict(
        seed.conn.execute(
            "select case when id = %s then 'earlier' else 'cli' end, state from ops.jobs"
            " where workspace_id = %s and job_type = 'discovery'",
            (earlier, workspace),
        ).fetchall()
    )
    assert states == {"earlier": "succeeded", "cli": "cancelled"}
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s and action = 'job.cancel'",
            (workspace,),
        )
        == 1
    )
