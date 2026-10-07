"""Database-backed operator commands on the migrated test database (marker ``db``).

Covers ``sources sync`` (dry run rolls back, ``--yes`` required), ``credentials create-mcp|list|
revoke`` (``--yes`` required, token printed once, only the hash stored), ``bootstrap owner``
(existing Auth users only), ``evidence verify``, ``outbox inspect``, ``reviews list``,
``reconcile --dry-run`` and ``db migrate`` (target printed without the password, ``--yes``
required, both engines). Everything is SYNTHETIC; nothing reaches a network.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from tests.cli.conftest import Cli
from tests.db_harness import (
    SUPABASE_STUB,
    _url_for,
    admin_url,
    apply_sql_file,
    create_migrated_database,
    drop_database,
    migration_files,
)
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import add_revision

from suv_deals.domain.enums import Drive, Fuel, Gearbox, Precision
from suv_deals.domain.listings import NormalizedListing, PartialDate, PriceInfo, VehicleSpec

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------------------------
# sources sync
# --------------------------------------------------------------------------------------------


def _source_count(seed: Seed, workspace: UUID) -> int:
    return int(seed.scalar("select count(*) from app.sources where workspace_id = %s", (workspace,)))


def test_sources_sync_dry_run_rolls_back(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    result = run_cli("sources", "sync", "--workspace", str(workspace), "--dry-run", env=db_env)
    assert result.exit_code == 0, result.output
    assert "Dry run (rolled back; nothing changed)" in result.output
    assert "autoscout24_de" in result.output
    assert _source_count(seed, workspace) == 0


def test_sources_sync_requires_yes_and_never_enables_gated_sources(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    refused = run_cli("sources", "sync", "--workspace", str(workspace), env=db_env)
    assert refused.exit_code == 3
    assert _source_count(seed, workspace) == 0
    done = run_cli("sources", "sync", "--workspace", str(workspace), "--yes", env=db_env)
    assert done.exit_code == 0, done.output
    assert _source_count(seed, workspace) >= 14
    assert (
        seed.scalar("select count(*) from app.sources where workspace_id = %s and enabled", (workspace,)) == 0
    )
    again = run_cli("sources", "sync", "--workspace", str(workspace), "--yes", env=db_env)
    assert "created                       : -" in again.output
    audits = seed.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'source.sync_create'",
        (workspace,),
    )
    assert audits == _source_count(seed, workspace)
    listed = run_cli("sources", "list", "--from-db", "--workspace", str(workspace), env=db_env)
    assert listed.exit_code == 0
    assert "autoscout24_de" in listed.output
    inspected = run_cli(
        "sources", "inspect", "autoscout24_de", "--from-db", "--workspace", str(workspace), env=db_env
    )
    assert inspected.exit_code == 0
    assert '"source_key": "autoscout24_de"' in inspected.output


def test_fixture_sources_are_refused_in_production(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    result = run_cli(
        "sources",
        "sync",
        "--workspace",
        str(workspace),
        "--fixture-sources",
        "--dry-run",
        env={**db_env, "APP_ENV": "production"},
    )
    assert result.exit_code == 3


def test_unknown_or_inactive_workspace_is_refused(run_cli: Cli, db_env: dict[str, str], seed: Seed) -> None:
    inactive = seed.workspace("inactive", active=False)
    result = run_cli("sources", "sync", "--workspace", str(inactive), "--dry-run", env=db_env)
    assert result.exit_code == 1
    assert "unknown or inactive" in result.output


# --------------------------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"suvmcp_[0-9a-f]{64}")


def test_credentials_create_requires_yes_and_prints_the_token_once(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    args = (
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "dot read-only (synthetic)",
    )
    args += ("--scopes", "deals:read,reviews:read", "--role", "viewer", "--expires", "30d")
    refused = run_cli(*args, env=db_env)
    assert refused.exit_code == 3
    assert "--yes" in refused.output
    assert seed.scalar("select count(*) from ops.api_credentials where workspace_id = %s", (workspace,)) == 0

    created = run_cli(*args, "--yes", env=db_env)
    assert created.exit_code == 0, created.output
    tokens = _TOKEN_RE.findall(created.output)
    assert len(tokens) == 1  # printed exactly once
    token = tokens[0]
    row = seed.conn.execute(
        "select id, token_hash, token_prefix, scopes, role, credential_kind from ops.api_credentials"
        " where workspace_id = %s",
        (workspace,),
    ).fetchone()
    assert row is not None
    credential_id, token_hash, prefix, scopes, role, kind = row
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token.startswith(prefix)
    assert scopes == ["deals:read", "reviews:read"] and role == "viewer" and kind == "static_bearer"
    # The token itself is stored nowhere (only its hash).
    dump = seed.scalar(
        "select row_to_json(c)::text from ops.api_credentials c where id = %s", (credential_id,)
    )
    assert token not in dump
    audit = seed.scalar(
        "select metadata::text from ops.audit_events"
        " where workspace_id = %s and action = 'credential.create'",
        (workspace,),
    )
    assert token not in audit

    listed = run_cli("credentials", "list", "--workspace", str(workspace), env=db_env)
    assert listed.exit_code == 0
    assert prefix in listed.output and token not in listed.output and token_hash not in listed.output

    revoked = run_cli(
        "credentials",
        "revoke",
        str(credential_id),
        "--workspace",
        str(workspace),
        "--reason",
        "synthetic",
        env=db_env,
    )
    assert revoked.exit_code == 3  # needs --yes
    revoked = run_cli(
        "credentials",
        "revoke",
        str(credential_id),
        "--workspace",
        str(workspace),
        "--reason",
        "synthetic test revoke",
        "--yes",
        env=db_env,
    )
    assert revoked.exit_code == 0, revoked.output
    assert seed.scalar(
        "select revoked_at is not null from ops.api_credentials where id = %s", (credential_id,)
    )
    assert (
        "No credentials." in run_cli("credentials", "list", "--workspace", str(workspace), env=db_env).output
    )


def test_credentials_never_grant_admin_or_mail_scopes(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    for scope in ("config:admin", "mail:ingest"):
        result = run_cli(
            "credentials",
            "create-mcp",
            "--workspace",
            str(workspace),
            "--label",
            "x",
            "--scopes",
            scope,
            "--role",
            "owner",
            "--yes",
            env=db_env,
        )
        assert result.exit_code == 3, scope
    widened = run_cli(
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "x",
        "--scopes",
        "reviews:write",
        "--role",
        "viewer",
        "--yes",
        env=db_env,
    )
    assert widened.exit_code == 1
    assert "VALIDATION_ERROR" in widened.output


# --------------------------------------------------------------------------------------------
# bootstrap owner
# --------------------------------------------------------------------------------------------


def test_bootstrap_owner_requires_an_existing_auth_user(run_cli: Cli, db_url: str, seed: Seed) -> None:
    name = f"CLI bootstrap {uuid.uuid4().hex[:8]}"
    env = {"MAINTENANCE_DATABASE_URL": db_url}
    missing = run_cli(
        "bootstrap", "owner", "--user-id", str(uuid.uuid4()), "--workspace-name", name, "--yes", env=env
    )
    assert missing.exit_code == 1
    assert "never creates users" in missing.output
    assert seed.scalar("select count(*) from app.workspaces where name = %s", (name,)) == 0

    user = seed.user()
    refused = run_cli("bootstrap", "owner", "--user-id", str(user), "--workspace-name", name, env=env)
    assert refused.exit_code == 3
    assert seed.scalar("select count(*) from app.workspaces where name = %s", (name,)) == 0

    created = run_cli(
        "bootstrap", "owner", "--user-id", str(user), "--workspace-name", name, "--yes", env=env
    )
    assert created.exit_code == 0, created.output
    workspace = seed.scalar("select id from app.workspaces where name = %s", (name,))
    try:
        role = seed.scalar(
            "select role from app.memberships where workspace_id = %s and user_id = %s and active",
            (workspace, user),
        )
        assert role == "owner"
        other = seed.user()
        email = seed.scalar("select email from auth.users where id = %s", (other,))
        joined = run_cli(
            "bootstrap", "owner", "--email", email.upper(), "--workspace", str(workspace), "--yes", env=env
        )
        assert joined.exit_code == 0, joined.output
        assert (
            seed.scalar(
                "select count(*) from app.memberships where workspace_id = %s and role = 'owner' and active",
                (workspace,),
            )
            == 2
        )
        audits = seed.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s"
            " and action in ('workspace.create', 'membership.bootstrap_owner')",
            (workspace,),
        )
        assert audits == 2
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (workspace,))


def test_bootstrap_owner_does_not_create_a_second_workspace_on_rerun(
    run_cli: Cli, db_url: str, seed: Seed
) -> None:
    name = f"CLI rerun {uuid.uuid4().hex[:8]}"
    env = {"MAINTENANCE_DATABASE_URL": db_url}
    user = seed.user()
    args = ("bootstrap", "owner", "--user-id", str(user), "--workspace-name", name, "--yes")
    first = run_cli(*args, env=env)
    assert first.exit_code == 0, first.output
    workspace = seed.scalar("select id from app.workspaces where name = %s", (name,))
    try:
        again = run_cli(*args[:-2], f"  {name} ", "--yes", env=env)  # same name after trimming
        assert again.exit_code == 1
        assert f"already owns the active workspace {workspace}" in again.output
        assert seed.scalar("select count(*) from app.workspaces where name = %s", (name,)) == 1
        # Re-confirming ownership of the existing workspace stays possible.
        confirmed = run_cli(
            "bootstrap", "owner", "--user-id", str(user), "--workspace", str(workspace), "--yes", env=env
        )
        assert confirmed.exit_code == 0, confirmed.output
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (workspace,))


def test_bootstrap_owner_never_takes_the_url_from_the_command_line(run_cli: Cli) -> None:
    result = run_cli("bootstrap", "owner", "--user-id", str(uuid.uuid4()), "--workspace-name", "x", "--yes")
    assert result.exit_code == 2
    assert "MAINTENANCE_DATABASE_URL is not set" in result.output


# --------------------------------------------------------------------------------------------
# evidence verify, outbox inspect, reviews list, reconcile
# --------------------------------------------------------------------------------------------


def _normalized(listing_id: str) -> NormalizedListing:
    return NormalizedListing(
        source_key="fixture_dealer_de",
        source_listing_id=listing_id,
        canonical_url=f"https://dealer.example/fahrzeug/{listing_id}",
        observed_at=datetime(2026, 10, 6, 10, 0, tzinfo=UTC),
        vehicle=VehicleSpec(
            make="Example",
            model="Trail",
            first_registration=PartialDate(value="2011-05", precision=Precision.MONTH),
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            power_kw=103,
            mileage_km=Decimal("187500"),
        ),
        price=PriceInfo(amount_minor=275000, currency="EUR"),
        parser_version="fixture@1.0.0",
    )


def test_evidence_verify_recomputes_revision_hashes(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    source = seed.source(workspace)
    good = _normalized("SYN-GOOD-1")
    listing = seed.listing(workspace, source)
    add_revision(
        seed,
        workspace,
        listing,
        1,
        normalized=good.model_dump(mode="json"),
        semantic_hash=good.semantic_hash(),
    )
    ok = run_cli("evidence", "verify", "--workspace", str(workspace), env=db_env)
    assert ok.exit_code == 0, ok.output
    assert "revisions: 1 ok, 0 hash mismatch, 0 unreadable" in ok.output

    other = seed.listing(workspace, source)
    bad = _normalized("SYN-BAD-2")
    add_revision(seed, workspace, other, 1, normalized=bad.model_dump(mode="json"), semantic_hash="0" * 64)
    failed = run_cli("evidence", "verify", "--workspace", str(workspace), env=db_env)
    assert failed.exit_code == 1
    assert "1 hash mismatch" in failed.output

    # --skip-objects needs no storage configuration at all (e.g. the isolated restore check).
    unconfigured = {**db_env, "SNAPSHOT_STORAGE": "supabase"}
    refused = run_cli("evidence", "verify", "--workspace", str(workspace), env=unconfigured)
    assert refused.exit_code == 1
    assert "SUPABASE_URL" in refused.output
    skipped = run_cli("evidence", "verify", "--workspace", str(workspace), "--skip-objects", env=unconfigured)
    assert "1 hash mismatch" in skipped.output
    assert "SUPABASE_URL" not in skipped.output


def test_outbox_reviews_and_reconcile_are_read_only(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    ws = str(workspace)
    outbox = run_cli("outbox", "inspect", "--workspace", ws, env=db_env)
    assert outbox.exit_code == 0, outbox.output
    assert "no open events" in outbox.output
    reviews = run_cli("reviews", "list", "--status", "pending,claimed", "--workspace", ws, env=db_env)
    assert reviews.exit_code == 0
    assert "No review cases" in reviews.output
    assert run_cli("reviews", "list", "--status", "approved", "--workspace", ws, env=db_env).exit_code == 2
    before = seed.scalar("select count(*) from ops.audit_events where workspace_id = %s", (workspace,))
    dry = run_cli("reconcile", "--dry-run", "--json", env={**db_env, "LOG_LEVEL": "WARNING"})
    assert dry.exit_code == 0, dry.output
    assert ws in dry.output
    assert (
        seed.scalar("select count(*) from ops.audit_events where workspace_id = %s", (workspace,)) == before
    )


# --------------------------------------------------------------------------------------------
# db migrate
# --------------------------------------------------------------------------------------------


@pytest.fixture
def emulated_database(db_url: str) -> Iterator[str]:
    """A scratch database with only the Supabase emulation (no migrations)."""
    del db_url  # ensures PostgreSQL is reachable (skips otherwise)
    name = f"suv_test_cli_migrate_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = _url_for(name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            apply_sql_file(conn, SUPABASE_STUB)
        yield url
    finally:
        drop_database(name)


def _ledger(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return [
            r[0] for r in conn.execute("select version from supabase_migrations.schema_migrations order by 1")
        ]


def test_db_migrate_prints_the_target_without_password_and_refuses_without_yes(run_cli: Cli) -> None:
    url = "postgresql://migrator:FAKE-MigratePass-99@db-host.example.invalid:6543/proddb"
    result = run_cli("db", "migrate", env={"DATABASE_URL": url})
    assert result.exit_code == 3
    assert "db-host.example.invalid" in result.output
    assert "proddb" in result.output
    assert "password : set (not shown)" in result.output
    assert "FAKE-MigratePass-99" not in result.output
    local_only = run_cli("db", "migrate", "--local-only", "--yes", env={"DATABASE_URL": url})
    assert local_only.exit_code == 3
    assert "not a loopback address" in local_only.output


@pytest.mark.parametrize("engine", ["psycopg", "psql"])
def test_db_migrate_applies_pending_migrations_once(
    run_cli: Cli, emulated_database: str, engine: str
) -> None:
    if engine == "psql" and shutil.which("psql") is None:
        pytest.skip("psql is not installed")
    env = {"MIGRATE_URL": emulated_database}
    base = ("db", "migrate", "--url-env", "MIGRATE_URL", "--engine", engine, "--local-only")
    dry = run_cli(*base, "--dry-run", env=env)
    assert dry.exit_code == 0, dry.output
    assert "Dry run: nothing applied." in dry.output
    with psycopg.connect(emulated_database) as conn:
        assert conn.execute("select to_regclass('app.sources')").fetchone() == (None,)
    applied = run_cli(*base, "--yes", env=env)
    assert applied.exit_code == 0, applied.output
    assert _ledger(emulated_database) == [p.name[:14] for p in migration_files()]
    again = run_cli(*base, "--yes", env=env)
    assert again.exit_code == 0
    assert "No pending migrations." in again.output
    password = str(psycopg.conninfo.conninfo_to_dict(emulated_database).get("password") or "")
    if len(password) >= 6:
        assert password not in applied.output


@pytest.fixture
def ledger_database_and_login() -> Iterator[tuple[str, str]]:
    """A migrated scratch database WITH a migration ledger, plus a dedicated LOGIN member of
    suv_backend that has no privilege on the ledger schema (as on a hosted project)."""
    name, url = create_migrated_database("suv_test_cli_doctor")
    login = f"suv_cli_probe_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "create schema supabase_migrations; create table supabase_migrations.schema_migrations"
                " (version text primary key, statements text[], name text)"
            )
            conn.execute(
                sql.SQL("create role {} login password {} in role suv_backend").format(
                    sql.Identifier(login), sql.Literal(password)
                )
            )
            conn.execute(
                sql.SQL("grant connect on database {} to {}").format(
                    sql.Identifier(name), sql.Identifier(login)
                )
            )
        login_url = psycopg.conninfo.make_conninfo(url, user=login, password=password)
        yield url, login_url
    finally:
        drop_database(name)
        with psycopg.connect(admin_url(), autocommit=True) as admin:
            admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(login)))


def test_doctor_reports_the_ledger_as_skipped_without_privilege(
    run_cli: Cli, ledger_database_and_login: tuple[str, str]
) -> None:
    _owner_url, login_url = ledger_database_and_login
    env = {"DATABASE_URL": login_url, "DATABASE_SET_ROLE": "suv_backend"}
    result = run_cli("doctor", "--process", "scheduler", env=env)
    out = result.output
    assert "SKIP  database/migration_ledger" in out and "no privilege to read the ledger" in out
    # The remaining checks still ran instead of one "database/check" error.
    assert "database/check" not in out
    assert "OK    database/set_role" in out
    assert "OK    database/schema" in out


def test_db_migrate_refuses_schemas_without_a_ledger(run_cli: Cli, db_url: str) -> None:
    # The harness database has app/ops but no ledger (migrations applied by the test harness).
    result = run_cli("db", "migrate", "--engine", "psycopg", "--yes", env={"DATABASE_URL": db_url})
    assert result.exit_code == 3
    assert "no migration ledger" in result.output


# --------------------------------------------------------------------------------------------
# Owner recovery actions
# --------------------------------------------------------------------------------------------


def test_owner_recovery_actions_never_enable_a_source(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    ws = str(workspace)
    assert (
        run_cli("sources", "sync", "--workspace", ws, "--fixture-sources", "--yes", env=db_env).exit_code == 0
    )
    seed.conn.execute(
        "update app.sources set technical_status = 'access_blocked', version = version + 1"
        " where workspace_id = %s and source_key = 'fixture_dealer_de'",
        (workspace,),
    )
    args = ("sources", "set-technical-status", "fixture_dealer_de", "fixture_tested", "--workspace", ws)
    refused = run_cli(*args, "--reason", "synthetic recovery", env=db_env)
    assert refused.exit_code == 3
    done = run_cli(*args, "--reason", "synthetic recovery after permitted access", "--yes", env=db_env)
    assert done.exit_code == 0, done.output
    assert "access_blocked -> fixture_tested" in done.output
    row = seed.conn.execute(
        "select technical_status, enabled from app.sources where workspace_id = %s"
        " and source_key = 'fixture_dealer_de'",
        (workspace,),
    ).fetchone()
    assert row == ("fixture_tested", False)
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events"
            " where workspace_id = %s and action = 'source.technical_status'",
            (workspace,),
        )
        == 1
    )

    seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'synthetic pause', paused_at = now(),"
        " version = version + 1 where workspace_id = %s and source_key = 'fixture_dealer_de'",
        (workspace,),
    )
    resumed = run_cli(
        "sources",
        "resume",
        "fixture_dealer_de",
        "--workspace",
        ws,
        "--reason",
        "synthetic resume",
        "--yes",
        env=db_env,
    )
    assert resumed.exit_code == 0, resumed.output
    assert "paused=false, enabled=false" in resumed.output
