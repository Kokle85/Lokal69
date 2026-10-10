"""``db target`` (the single local-target guard) and the password handoff to ``migrate.sh``.

No database is contacted: ``db target`` never connects, and the ``migrate.sh`` engine is replaced
by a fake script that records what it received. All credentials are FAKE.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.cli.conftest import Cli

from suv_deals.cli_commands import db as db_module

BYPASSES = [
    "postgresql://suv:FAKE-pw-71@127.0.0.1:5432/suv_dev?host=db.example.invalid",
    "postgresql://suv:FAKE-pw-71@127.0.0.1:5432/suv_dev?hostaddr=10.20.30.40",
    "postgresql://suv:FAKE-pw-71@127.0.0.1:5432,db.example.invalid:5432/suv_dev",
    "postgresql://suv:FAKE-pw-71@localhost/suv_dev?service=production",
]


@pytest.mark.parametrize("url", BYPASSES)
def test_db_target_local_only_refuses_every_libpq_escape(run_cli: Cli, url: str) -> None:
    result = run_cli(
        "db", "target", "--url-env", "LOCAL_ADMIN_URL", "--local-only", env={"LOCAL_ADMIN_URL": url}
    )
    assert result.exit_code == 3, result.output
    assert "LOCAL_ADMIN_URL is not a loopback address or a local socket" in result.output
    assert "local    : no" in result.output
    assert "FAKE-pw-71" not in result.output


def test_db_target_accepts_a_loopback_target_and_never_prints_the_password(run_cli: Cli) -> None:
    url = "postgresql://suv:FAKE-pw-72@127.0.0.1:5433/suv_dev"
    result = run_cli(
        "db", "target", "--url-env", "LOCAL_DATABASE_URL", "--local-only", env={"LOCAL_DATABASE_URL": url}
    )
    assert result.exit_code == 0, result.output
    assert "host     : 127.0.0.1" in result.output and "port     : 5433" in result.output
    assert "password : set (not shown)" in result.output
    assert "local    : yes" in result.output
    assert "FAKE-pw-72" not in result.output


def test_db_target_honours_libpq_environment_defaults(run_cli: Cli) -> None:
    env = {"LOCAL_DATABASE_URL": "dbname=suv_dev user=suv", "PGHOSTADDR": "10.20.30.40"}
    result = run_cli("db", "target", "--url-env", "LOCAL_DATABASE_URL", "--local-only", env=env)
    assert result.exit_code == 3
    assert "hostaddr : 10.20.30.40" in result.output


def test_db_target_without_local_only_only_reports(run_cli: Cli) -> None:
    result = run_cli("db", "target", env={"DATABASE_URL": BYPASSES[0]})
    assert result.exit_code == 0
    assert "local    : no" in result.output
    assert "FAKE-pw-71" not in result.output


def test_db_target_usage_errors(run_cli: Cli) -> None:
    assert run_cli("db", "target", "--url-env", "lower_case").exit_code == 2
    missing = run_cli("db", "target", "--url-env", "LOCAL_ADMIN_URL")
    assert missing.exit_code == 2
    assert "LOCAL_ADMIN_URL is not set" in missing.output
    garbage = run_cli(
        "db", "target", "--url-env", "LOCAL_ADMIN_URL", env={"LOCAL_ADMIN_URL": "FAKE-not-a-dsn-73"}
    )
    assert garbage.exit_code == 2
    assert "FAKE-not-a-dsn-73" not in garbage.output


def test_migrate_script_gets_the_password_only_through_the_environment(
    run_cli: Cli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """psql (inside migrate.sh) receives $DATABASE_URL as an argument: it must be password-free."""
    record = tmp_path / "received.txt"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "migrate.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'{{ echo "url=$DATABASE_URL"; echo "pgpassword=${{PGPASSWORD:-}}"; echo "args=$*"; }} > "{record}"\n'
        'echo "Target database : fake"\n',
        encoding="utf-8",
    )
    (tmp_path / "supabase" / "migrations").mkdir(parents=True)
    monkeypatch.setattr(db_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(db_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    url = "postgresql://migrator:FAKE%40pass-74@127.0.0.1:5433/suv_dev?sslmode=disable"
    result = run_cli("db", "migrate", "--engine", "psql", "--yes", env={"DATABASE_URL": url})
    assert result.exit_code == 0, result.output
    received = dict(
        line.split("=", 1) for line in record.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert "FAKE" not in received["url"]
    assert "127.0.0.1" in received["url"] and "migrator" in received["url"]
    assert "sslmode" in received["url"]
    assert received["pgpassword"] == "FAKE@pass-74"
    assert received["args"] == "--yes"
    assert "FAKE" not in result.output
