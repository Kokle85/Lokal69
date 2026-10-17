"""scripts/migrate.sh and scripts/rollback.sh behaviour (spec 29; docs/schema.md section 1).

Runs the real scripts with psql against scratch databases created by the test harness:
- the target is printed as reported by the server, never the connection string/password;
- without a terminal and without --yes nothing is applied;
- --yes applies every migration once and records the ledger; a rerun has nothing pending;
- a database with app/ops but no ledger, or with an unknown ledger version, is refused;
- rollback.sh prints the forward-fix policy, --status is read-only, down requests are refused.
Skipped (and reported) when psql is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from tests.db_harness import (
    REPO_ROOT,
    SUPABASE_STUB,
    _url_for,
    admin_url,
    apply_sql_file,
    drop_database,
    migration_files,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(shutil.which("psql") is None, reason="psql is not installed"),
]

MIGRATE = REPO_ROOT / "scripts" / "migrate.sh"
ROLLBACK = REPO_ROOT / "scripts" / "rollback.sh"


def run(script: Path, *args: str, url: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.pop("DATABASE_URL", None)
    if url is not None:
        env["DATABASE_URL"] = url
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def emulated_database(db_url: str) -> Iterator[tuple[str, str]]:
    """A scratch database with only the Supabase emulation applied (no migrations)."""
    name = f"suv_test_scripts_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = _url_for(name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            apply_sql_file(conn, SUPABASE_STUB)
        yield name, url
    finally:
        drop_database(name)


def test_migrate_applies_once_with_ledger_and_never_prints_the_url(
    emulated_database: tuple[str, str],
) -> None:
    name, url = emulated_database
    password = psycopg.conninfo.conninfo_to_dict(url).get("password")
    versions = [p.name[:14] for p in migration_files()]

    dry = run(MIGRATE, "--dry-run", url=url)
    assert dry.returncode == 0, dry.stderr
    assert f"Target database : {name}" in dry.stdout
    assert "Dry run: nothing applied." in dry.stdout
    assert all(p.name in dry.stdout for p in migration_files())

    refused = run(MIGRATE, url=url)  # stdin is not a terminal and --yes is absent
    assert refused.returncode == 3
    assert "pass --yes" in refused.stderr

    applied = run(MIGRATE, "--yes", url=url)
    assert applied.returncode == 0, applied.stderr
    assert f"Applied {len(versions)} migration(s) to {name}." in applied.stdout
    for output in (dry.stdout + dry.stderr, applied.stdout + applied.stderr):
        assert url not in output
        assert "password" not in output.lower()
        if password:
            assert f"password={password}" not in output
    with psycopg.connect(url) as conn:
        ledger = conn.execute(
            "select version from supabase_migrations.schema_migrations order by version"
        ).fetchall()
        assert [v for (v,) in ledger] == versions
        assert conn.execute("select count(*) from app.workspaces").fetchone() == (0,)

    again = run(MIGRATE, "--yes", url=url)
    assert again.returncode == 0, again.stderr
    assert "No pending migrations." in again.stdout

    status = run(ROLLBACK, "--status", url=url)
    assert status.returncode == 0, status.stderr
    assert all(v in status.stdout for v in versions)

    # A ledger entry this checkout does not know (drifted database / wrong checkout) is refused.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into supabase_migrations.schema_migrations (version, name)"
            " values ('29991231235959', 'from_another_branch')"
        )
    drifted = run(MIGRATE, "--dry-run", url=url)
    assert drifted.returncode == 3
    assert "does not contain" in drifted.stderr


def test_migrate_refuses_schemas_without_a_ledger(db_url: str) -> None:
    """The harness database has app/ops but no ledger: never re-apply blindly."""
    result = run(MIGRATE, "--dry-run", url=db_url)
    assert result.returncode == 3
    assert "no migration ledger" in result.stderr


def test_migrate_requires_database_url() -> None:
    result = run(MIGRATE, "--dry-run", url=None)
    assert result.returncode == 2
    assert "DATABASE_URL is not set" in result.stderr


@pytest.mark.parametrize("args", [["20261006000800"], ["--down"], ["--force"], ["down", "1"]])
def test_rollback_refuses_down_migrations(args: list[str]) -> None:
    result = run(ROLLBACK, *args, url=None)
    assert result.returncode == 2
    assert "forward" in (result.stdout + result.stderr).lower()


def test_rollback_policy_is_forward_fix_only() -> None:
    result = run(ROLLBACK, url=None)
    assert result.returncode == 0
    assert "forward fixes only" in result.stdout.lower()
    assert "never" in result.stdout.lower()
