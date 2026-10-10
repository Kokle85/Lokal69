"""Migration application and schema catalogue checks (spec sections 11, 12, 31).

Covers: the Supabase emulation is idempotent and refuses a foreign auth
schema, migrations rebuild cleanly in a cluster where the roles already exist,
every table of the spec section 11 catalogue exists with workspace ownership,
composite uniqueness, RLS and UTC timestamps, and required indexes exist.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from tests.db_harness import (
    MIGRATIONS_DIR,
    REPO_ROOT,
    SUPABASE_STUB,
    _url_for,
    admin_url,
    apply_sql_file,
    create_migrated_database,
    drop_database,
    migration_files,
)

pytestmark = pytest.mark.db

# Spec section 11 "Required tables" (all 37 rows).
SPEC_TABLES = {
    "app.workspaces",
    "app.memberships",
    "app.config_revisions",
    "app.sources",
    "app.search_profiles",
    "ops.source_schedules",
    "ops.crawl_runs",
    "ops.fetch_attempts",
    "app.listings",
    "app.listing_aliases",
    "app.listing_revisions",
    "app.listing_observations",
    "ops.source_snapshots",
    "app.vehicle_clusters",
    "app.vehicle_cluster_members",
    "app.field_evidence",
    "app.comparable_sets",
    "app.market_observations",
    "app.fx_rates",
    "app.tax_rule_sets",
    "app.cost_profiles",
    "app.cost_evidence",
    "app.valuations",
    "app.review_cases",
    "app.review_decisions",
    "app.watchlists",
    "app.owner_notes",
    "app.notification_preferences",
    "ops.jobs",
    "ops.outbox",
    "ops.delivery_attempts",
    "ops.event_subscriptions",
    "ops.event_deliveries",
    "ops.query_snapshots",
    "ops.idempotency_records",
    "ops.audit_events",
    "ops.activation_gates",
}
# Additional tables this system needs beyond the spec list.
EXTRA_TABLES = {
    "app.detail_observations",
    "app.comparable_set_members",
    "app.destination_bindings",
    "ops.host_budgets",
    "ops.robots_revisions",
    "ops.api_credentials",
    # Spec v1.1 section 37.8 (migration 20261006001000_seller_inquiries).
    "app.seller_entities",
    "app.seller_entity_aliases",
    "app.seller_contacts",
    "app.seller_inquiry_authorizations",
    "app.seller_inquiry_controls",
    "app.seller_inquiries",
    "app.seller_replies",
    "app.seller_reply_locators",
    "app.availability_events",
    "ops.email_sender_bindings",
    "ops.email_delivery_attempts",
    "ops.email_suppressions",
    "ops.inquiry_quota_ledger",
    "ops.mail_worker_bindings",
    "ops.mail_worker_checkpoints",
    "ops.mail_ingest_dedup",
    "ops.mail_binding_sync",
    # Owner-controlled activation canaries (migration 20261008000200_inquiry_hardening).
    "ops.inquiry_activation_canaries",
}
ALL_TABLES = SPEC_TABLES | EXTRA_TABLES


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "select n.nspname || '.' || c.relname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname in ('app', 'ops') and c.relkind in ('r', 'p')"
    ).fetchall()
    return {r[0] for r in rows}


def test_spec_table_catalogue_exists(db_conn: psycopg.Connection) -> None:
    assert len(SPEC_TABLES) == 37
    tables = _tables(db_conn)
    assert tables >= SPEC_TABLES, f"missing: {sorted(SPEC_TABLES - tables)}"
    assert tables == ALL_TABLES, f"undocumented tables: {sorted(tables - ALL_TABLES)}"


def test_every_table_has_rls_enabled(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select n.nspname || '.' || c.relname, c.relrowsecurity from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname in ('app', 'ops') and c.relkind in ('r', 'p')"
    ).fetchall()
    assert rows
    assert [name for name, rls in rows if not rls] == []


def test_workspace_owned_tables_have_tenant_policy_and_shape(db_conn: psycopg.Connection) -> None:
    owned = db_conn.execute(
        "select c.oid, n.nspname || '.' || c.relname, a.attnotnull"
        " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " join pg_attribute a on a.attrelid = c.oid and a.attname = 'workspace_id' and not a.attisdropped"
        " where n.nspname in ('app', 'ops') and c.relkind = 'r'"
    ).fetchall()
    names = {name for _, name, _ in owned}
    assert names == ALL_TABLES - {"app.workspaces"}
    for oid, name, not_null in owned:
        assert not_null, f"{name}.workspace_id must be NOT NULL"
        policy = db_conn.execute(
            "select pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid),"
            " array(select rolname from pg_roles where oid = any(p.polroles)), p.polcmd"
            " from pg_policy p where p.polrelid = %s and p.polname = 'tenant_isolation'",
            (oid,),
        ).fetchone()
        assert policy is not None, f"{name} has no tenant_isolation policy"
        using, check, roles, cmd = policy
        assert "workspace_id = ( SELECT app.current_workspace_id()" in using, name
        assert "workspace_id = ( SELECT app.current_workspace_id()" in check, name
        assert roles == ["suv_backend"], name
        assert cmd == "*", name
        created = db_conn.execute(
            "select format_type(atttypid, atttypmod), attnotnull from pg_attribute"
            " where attrelid = %s and attname = 'created_at' and not attisdropped",
            (oid,),
        ).fetchone()
        assert created == ("timestamp with time zone", True), f"{name}.created_at"


def test_workspace_owned_tables_have_composite_identity(db_conn: psycopg.Connection) -> None:
    """Every child table exposes (workspace_id, id) for composite foreign keys."""
    rows = db_conn.execute(
        "select n.nspname || '.' || c.relname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname in ('app', 'ops') and c.relkind = 'r'"
        " and exists (select 1 from pg_attribute a where a.attrelid = c.oid and a.attname = 'workspace_id')"
        " and exists (select 1 from pg_attribute a where a.attrelid = c.oid and a.attname = 'id')"
        " and not exists ("
        "   select 1 from pg_constraint k where k.conrelid = c.oid and k.contype in ('u', 'p')"
        "   and (select array_agg(attname::text order by attname) from pg_attribute"
        "        where attrelid = c.oid and attnum = any(k.conkey)) = array['id', 'workspace_id'])"
    ).fetchall()
    assert rows == []


def test_all_timestamps_are_timestamptz_and_no_float_columns(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select table_schema || '.' || table_name || '.' || column_name, data_type"
        " from information_schema.columns where table_schema in ('app', 'ops')"
        " and data_type in ('timestamp without time zone', 'real', 'double precision', 'money')"
    ).fetchall()
    assert rows == []


def test_money_columns_are_paired_with_currency(db_conn: psycopg.Connection) -> None:
    """Every *_minor money column is bigint (never float) and carries its currency.

    Architecture convention: ``<name>_minor bigint`` plus ``currency char(3)``, both present or
    both absent. Either the table's currency column is NOT NULL (e.g. valuations), or a CHECK
    constraint ties the amount column to a currency column.
    """
    rows = db_conn.execute(
        "select c.table_schema || '.' || c.table_name, c.column_name, c.data_type"
        " from information_schema.columns c where c.table_schema in ('app', 'ops')"
        " and c.column_name like '%%\\_minor'"
    ).fetchall()
    assert rows
    assert {dtype for _, _, dtype in rows} == {"bigint"}
    for table, column, _ in rows:
        currency_cols = db_conn.execute(
            "select attname, attnotnull, format_type(atttypid, atttypmod) from pg_attribute"
            " where attrelid = %s::regclass and attnum > 0 and not attisdropped"
            " and attname like '%%currency'",
            (table,),
        ).fetchall()
        assert currency_cols, f"{table}.{column} has no currency column"
        assert {fmt for _, _, fmt in currency_cols} == {"character(3)"}, table
        if any(not_null for _, not_null, _ in currency_cols):
            continue
        paired = db_conn.execute(
            "select count(*) from pg_constraint where conrelid = %s::regclass and contype = 'c'"
            " and pg_get_constraintdef(oid) like %s and pg_get_constraintdef(oid) ~ 'currency IS NULL'",
            (table, f"%({column} IS NULL)%"),
        ).fetchone()
        assert paired is not None and paired[0] >= 1, f"{table}.{column} is not paired with its currency"


def test_listing_revisions_allow_semantic_reversion(db_conn: psycopg.Connection) -> None:
    """No unique index may contain semantic_hash (A -> B -> A must be insertable)."""
    rows = db_conn.execute(
        "select i.indexrelid::regclass::text from pg_index i"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indrelid = 'app.listing_revisions'::regclass and i.indisunique"
        " and a.attname = 'semantic_hash'"
    ).fetchall()
    assert rows == []


@pytest.mark.parametrize(
    ("index", "fragments"),
    [
        ("ops.jobs_due_idx", ["(workspace_id, available_at, priority, id)", "'queued'", "'retry_wait'"]),
        ("ops.jobs_lease_expiry_idx", ["(lease_expires_at)", "'running'"]),
        ("ops.jobs_dedup_open_uidx", ["UNIQUE", "(workspace_id, dedup_key)"]),
        (
            "ops.jobs_scheduler_slot_uidx",
            ["UNIQUE", "(workspace_id, source_id, profile_id, partition_key, scheduled_slot)"],
        ),
        ("ops.outbox_due_idx", ["(workspace_id, available_at, id)", "'pending'", "'retry_wait'"]),
        ("app.review_queue_idx", ["(workspace_id, state, priority DESC, created_at, id)"]),
        ("app.review_cases_open_uidx", ["UNIQUE", "(workspace_id, listing_id, profile_key)", "superseded"]),
        ("app.listing_recent_idx", ["(workspace_id, last_seen_at DESC, id)"]),
        # Migration 20261007000100 (EXPLAIN-verified read paths).
        ("app.listings_created_idx", ["(workspace_id, created_at DESC, id DESC)"]),
        (
            "ops.outbox_attention_created_idx",
            [
                "(workspace_id, event_created_at, id)",
                "'uncertain'",
                "'blocked'",
                "'dead_letter'",
                "'retry_wait'",
            ],
        ),
        ("app.memberships_user_idx", ["(user_id, workspace_id)", "WHERE active"]),
        (
            "app.market_observations_comparable_idx",
            [
                "market, make, model, vehicle_generation, registration_year,",
                "fuel, gearbox, drive, observed_at DESC",
            ],
        ),
    ],
)
def test_required_indexes(db_conn: psycopg.Connection, index: str, fragments: list[str]) -> None:
    row = db_conn.execute("select pg_get_indexdef(%s::regclass)", (index,)).fetchone()
    assert row is not None
    for fragment in fragments:
        assert fragment in row[0], f"{index}: {row[0]}"


def test_current_revision_fk_is_composite_and_deferrable(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute(
        "select condeferrable, condeferred, pg_get_constraintdef(oid) from pg_constraint"
        " where conname = 'current_revision_belongs_to_listing'"
    ).fetchone()
    assert row is not None
    deferrable, deferred, definition = row
    assert deferrable and deferred
    assert "(workspace_id, id, current_revision_id)" in definition
    assert "app.listing_revisions(workspace_id, listing_id, id)" in definition


def test_migration_files_are_ordered_plain_sql(db_conn: psycopg.Connection) -> None:
    files = migration_files()
    assert len(files) >= 8
    versions = []
    for path in files:
        assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", path.name), path.name
        versions.append(path.name[:14])
        text = path.read_text(encoding="utf-8")
        # Applied as one multi-statement string by psycopg and by psql --single-transaction:
        # no psql meta-commands and no transaction control inside a migration.
        assert not re.search(r"^\s*\\", text, re.MULTILINE), path.name
        assert not re.search(r"^\s*(begin|commit|rollback)\s*;", text, re.MULTILINE | re.IGNORECASE), (
            path.name
        )
    assert versions == sorted(set(versions))
    assert MIGRATIONS_DIR == REPO_ROOT / "supabase" / "migrations"


def test_emulation_is_idempotent_on_migrated_database(db_conn: psycopg.Connection) -> None:
    before = db_conn.execute("select count(*) from auth.users").fetchone()
    apply_sql_file(db_conn, SUPABASE_STUB)
    apply_sql_file(db_conn, SUPABASE_STUB)
    after = db_conn.execute("select count(*) from auth.users").fetchone()
    assert before == after
    roles = db_conn.execute(
        "select rolname, rolcanlogin, rolbypassrls from pg_roles"
        " where rolname in ('anon', 'authenticated', 'service_role', 'suv_backend') order by rolname"
    ).fetchall()
    assert roles == [
        ("anon", False, False),
        ("authenticated", False, False),
        ("service_role", False, True),
        ("suv_backend", False, False),
    ]


def test_emulation_auth_helpers_read_jwt_claims(db_conn: psycopg.Connection) -> None:
    user = uuid.uuid4()
    with db_conn.transaction():
        db_conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (f'{{"sub": "{user}", "role": "authenticated", "email": "synthetic@example.invalid"}}',),
        )
        row = db_conn.execute("select auth.uid(), auth.role(), auth.email(), auth.jwt() ->> 'sub'").fetchone()
    assert row == (user, "authenticated", "synthetic@example.invalid", str(user))
    # Outside the transaction the GUC reads as '' and the helpers return NULL.
    assert db_conn.execute("select auth.uid()").fetchone() == (None,)


def _scratch_database(prefix: str) -> tuple[str, str]:
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    return name, _url_for(name)


def test_emulation_refuses_a_foreign_auth_schema(db_url: str) -> None:
    name, url = _scratch_database("suv_test_foreign_auth")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("create schema auth")
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                apply_sql_file(conn, SUPABASE_STUB)
            # Nothing was created inside the foreign auth schema.
            assert conn.execute("select to_regclass('auth.users')").fetchone() == (None,)
    finally:
        drop_database(name)


def test_migrations_refuse_plain_postgres_without_supabase_prerequisites(db_url: str) -> None:
    name, url = _scratch_database("suv_test_noprereq")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                apply_sql_file(conn, migration_files()[0])
            assert conn.execute("select to_regnamespace('app')").fetchone() == (None,)
    finally:
        drop_database(name)


def test_a_second_database_migrates_while_global_roles_exist(db_url: str) -> None:
    """Roles are cluster-global: re-running emulation + migrations elsewhere must work."""
    name, url = create_migrated_database("suv_test_second")
    try:
        with psycopg.connect(url) as conn:
            assert len(_tables(conn)) == len(ALL_TABLES)
    finally:
        drop_database(name)


def test_seed_is_synthetic_and_idempotent(db_conn: psycopg.Connection) -> None:
    seed_sql = REPO_ROOT / "supabase" / "seed.sql"
    text = seed_sql.read_text(encoding="utf-8")
    assert "synthetic" in text.lower()
    with db_conn.transaction(force_rollback=True):
        apply_sql_file(db_conn, seed_sql)
        apply_sql_file(db_conn, seed_sql)
        rows = db_conn.execute(
            "select name from app.workspaces where name = 'Local development (synthetic)'"
        ).fetchall()
        assert rows == [("Local development (synthetic)",)]
        # No users, credentials or listings are seeded.
        for table in ("app.memberships", "ops.api_credentials", "app.listings"):
            count = db_conn.execute(
                sql.SQL("select count(*) from {} where workspace_id = {}").format(
                    sql.Identifier(*table.split(".")),
                    sql.Literal("00000000-0000-4000-8000-00000000d001"),
                )
            ).fetchone()
            assert count == (0,)


def test_supabase_config_does_not_expose_private_schemas() -> None:
    text = (REPO_ROOT / "supabase" / "config.toml").read_text(encoding="utf-8")
    match = re.search(r"^schemas\s*=\s*\[(?P<items>[^\]]*)\]", text, re.MULTILINE)
    assert match is not None
    exposed = {item.strip().strip('"') for item in match.group("items").split(",") if item.strip()}
    assert "app" not in exposed
    assert "ops" not in exposed


def test_scripts_exist_and_are_guarded() -> None:
    migrate = (REPO_ROOT / "scripts" / "migrate.sh").read_text(encoding="utf-8")
    assert "ON_ERROR_STOP=1" in migrate
    assert "--yes" in migrate
    rollback = (REPO_ROOT / "scripts" / "rollback.sh").read_text(encoding="utf-8")
    assert "forward" in rollback.lower()
    for script in ("migrate.sh", "rollback.sh"):
        assert Path(REPO_ROOT / "scripts" / script).stat().st_mode & 0o111, f"{script} not executable"
