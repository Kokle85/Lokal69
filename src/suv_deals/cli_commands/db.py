"""``suv-deals db migrate`` (spec 27, 29; docs/schema.md section 1).

Prints the target (host, port, database, user; never the password) BEFORE anything else, and
applies nothing without ``--yes``. ``--local-only`` (used by ``make db-migrate-local``) refuses any
target that is not a loopback address or a local socket.

Engines:

- ``psql`` (default when ``psql`` is installed): delegates to ``scripts/migrate.sh`` with the
  connection string in the child's environment (the script reports the server-side target, lists
  pending files, refuses drifted ledgers and applies each file with its ledger row atomically);
- ``psycopg`` (default otherwise, e.g. in the slim runtime image): the same rules implemented with
  psycopg: ledger ``supabase_migrations.schema_migrations``, refusal when ``app``/``ops`` exist
  without a ledger or the ledger names versions this checkout lacks, one transaction per file
  together with its ledger row, ``lock_timeout=10s``.

The connection string comes from ``DATABASE_URL`` (or the variable named by ``--url-env``), never
from a command-line argument. Migrations need the schema-owner role, not ``suv_backend``.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    CliContext,
    database_target,
    echo,
    exit_with,
    fail,
    load_settings,
    pass_cli,
    refuse,
    safe,
)

_MIGRATION_RE = re.compile(r"^([0-9]{14})_([a-z0-9_]+)\.sql$")
_LEDGER_DDL = (
    "create schema if not exists supabase_migrations;"
    " create table if not exists supabase_migrations.schema_migrations"
    " (version text not null primary key, statements text[], name text)"
)


def repo_root() -> Path:
    from suv_deals.settings import REPO_ROOT

    return REPO_ROOT


def migration_plan(directory: Path) -> list[tuple[str, str, Path]]:
    """``(version, label, path)`` per file, validated like ``scripts/migrate.sh``."""
    plan: list[tuple[str, str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None:
            refuse(f"unexpected migration file name: {path.name}")
        plan.append((match.group(1), match.group(2), path))
    return plan


def apply_with_psycopg(url: str, directory: Path, *, dry_run: bool) -> int:
    """Apply pending migrations with psycopg (same safety rules as ``scripts/migrate.sh``)."""
    import psycopg

    plan = migration_plan(directory)
    with psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=10,
        options=(
            "-c lock_timeout=10s -c idle_in_transaction_session_timeout=120s -c client_min_messages=warning"
        ),
        application_name="suv-deals-migrate",
    ) as conn:
        row = conn.execute(
            "select current_database(), coalesce(host(inet_server_addr()), 'local socket'),"
            " current_user, current_setting('server_version')"
        ).fetchone()
        assert row is not None
        echo(f"Server reports   : database {row[0]}, address {row[1]}, role {row[2]}, version {row[3]}")
        ledger = conn.execute(
            "select to_regclass('supabase_migrations.schema_migrations') is not null"
        ).fetchone()
        schemas = conn.execute(
            "select exists (select 1 from pg_namespace where nspname in ('app', 'ops'))"
        ).fetchone()
        applied: set[str] = set()
        if ledger and ledger[0]:
            applied = {
                str(r[0]) for r in conn.execute("select version from supabase_migrations.schema_migrations")
            }
        elif schemas and schemas[0]:
            refuse("schemas app/ops exist but no migration ledger was found; reconcile the ledger first")
        known = {version for version, _, _ in plan}
        foreign = sorted(applied - known)
        if foreign:
            refuse(f"the database has migration(s) this checkout does not contain: {', '.join(foreign)}")
        pending = [(v, label, p) for v, label, p in plan if v not in applied]
        if not pending:
            echo("No pending migrations.")
            return 0
        echo(f"Pending migrations ({len(pending)}):")
        for _, _, path in pending:
            echo(f"  - {path.name}")
        if dry_run:
            echo("Dry run: nothing applied.")
            return 0
        conn.execute(_LEDGER_DDL)
        for version, label, path in pending:
            echo(f"Applying {path.name} ...")
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "insert into supabase_migrations.schema_migrations (version, name) values (%s, %s)",
                    (version, label),
                )
        echo(f"Applied {len(pending)} migration(s).")
    return 0


def apply_with_script(url: str, *, dry_run: bool) -> int:
    script = repo_root() / "scripts" / "migrate.sh"
    if not script.is_file():
        fail("scripts/migrate.sh is missing from this checkout", EXIT_USAGE)
    env = {**os.environ, "DATABASE_URL": url}
    args = ["bash", str(script), "--dry-run" if dry_run else "--yes"]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell; the URL travels in the environment
        args, env=env, check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if completed.stdout:
        click.echo(completed.stdout.rstrip("\n"))
    if completed.stderr:
        click.echo(safe(completed.stderr.rstrip("\n")), err=True)
    return completed.returncode


@click.group("db")
def db_group() -> None:
    """Database maintenance (migrations are forward-only; see docs/schema.md)."""


@db_group.command("migrate")
@click.option("--yes", is_flag=True, help="Apply the pending migrations to the printed target.")
@click.option("--dry-run", is_flag=True, help="Connect, list pending migrations, change nothing.")
@click.option(
    "--local-only", is_flag=True, help="Refuse unless the target is a loopback host or local socket."
)
@click.option(
    "--url-env",
    default="DATABASE_URL",
    show_default=True,
    help="Environment variable holding the migration connection string (never pass it as an argument).",
)
@click.option(
    "--engine",
    type=click.Choice(["auto", "psql", "psycopg"]),
    default="auto",
    show_default=True,
    help="auto: scripts/migrate.sh when psql is installed, otherwise psycopg.",
)
@pass_cli
def migrate(
    cli: CliContext, *, yes: bool, dry_run: bool, local_only: bool, url_env: str, engine: str
) -> None:
    """Print the target, then apply pending supabase/migrations/*.sql (requires --yes)."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", url_env):
        fail("--url-env must name an environment variable (upper case)", EXIT_USAGE)
    settings = load_settings(cli)
    if url_env == "DATABASE_URL":
        url = settings.database_url.get_secret_value() if settings.database_url is not None else ""
    else:
        url = os.environ.get(url_env, "")
    if not url:
        fail(f"{url_env} is not set (server-side secret; set it in the environment)", EXIT_USAGE)
    target = database_target(url)
    directory = repo_root() / "supabase" / "migrations"
    echo("Migration target (from the connection string; password never shown):")
    for line in target.lines():
        echo(line)
    echo(f"  APP_ENV  : {settings.app_env}")
    echo(f"  files    : {len(migration_plan(directory))} in supabase/migrations")
    if local_only and not target.is_local:
        refuse("--local-only: the target is not a loopback address or a local socket")
    if not dry_run and not yes:
        echo("Nothing applied.")
        refuse("db migrate applies migrations; re-run with --yes (or --dry-run to inspect)")
    use_psql = engine == "psql" or (engine == "auto" and shutil.which("psql") is not None)
    if engine == "psql" and shutil.which("psql") is None:
        fail("psql is not installed; use --engine psycopg", EXIT_USAGE)
    code = (
        apply_with_script(url, dry_run=dry_run)
        if use_psql
        else apply_with_psycopg(url, directory, dry_run=dry_run)
    )
    if code:
        exit_with(EXIT_PROBLEMS if code != EXIT_REFUSED else EXIT_REFUSED)
