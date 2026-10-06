"""RLS, grants and append-only history (spec 11, 12, 31 RLS row; ADR 0001).

Positive and negative checks for the BFF-only model:
- anon/authenticated (and service_role, PUBLIC) have no access to app/ops;
- suv_backend sees and writes only the workspace selected by the GUC, and
  nothing at all without it;
- membership, workspace and credential bootstrap policies;
- ops.active_workspace_ids() is a hardened SECURITY DEFINER function;
- immutable history rejects UPDATE/DELETE for suv_backend (privileges) and for
  every role including superusers (triggers), except documented maintenance.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg import errors, sql
from tests.integration.db.helpers import (
    SV_APPEND_ONLY,
    T0,
    Seed,
    World,
    as_role,
    backend,
    sha,
    table_ident,
    unique,
)

pytestmark = pytest.mark.db

CLIENT_ROLES = ("anon", "authenticated", "service_role")
APPEND_ONLY = (
    "app.config_revisions",
    "app.listing_revisions",
    "app.detail_observations",
    "app.listing_observations",
    "app.listing_aliases",
    "app.field_evidence",
    "app.market_observations",
    "app.comparable_sets",
    "app.comparable_set_members",
    "app.fx_rates",
    "app.cost_evidence",
    "app.review_decisions",
    "ops.audit_events",
    "ops.delivery_attempts",
)


def _all_tables(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "select n.nspname || '.' || c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname in ('app', 'ops') and c.relkind = 'r' order by 1"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------------------
# Client roles: no access at all
# ---------------------------------------------------------------------------------------


def test_client_roles_have_no_privileges_on_private_schemas(db_conn: psycopg.Connection) -> None:
    tables = _all_tables(db_conn)
    for role in CLIENT_ROLES:
        for schema in ("app", "ops"):
            assert db_conn.execute(
                "select has_schema_privilege(%s, %s, 'USAGE') or has_schema_privilege(%s, %s, 'CREATE')",
                (role, schema, role, schema),
            ).fetchone() == (False,), (role, schema)
        for table in tables:
            row = db_conn.execute(
                "select has_table_privilege(%(r)s, %(t)s,"
                " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                " or has_any_column_privilege(%(r)s, %(t)s, 'SELECT,INSERT,UPDATE,REFERENCES')",
                {"r": role, "t": table},
            ).fetchone()
            assert row == (False,), (role, table)
    # PUBLIC: no ACL entries on any app/ops table, schema or routine.
    public_acl = db_conn.execute(
        "select count(*) from ("
        "  select c.relacl as acl from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        "   where n.nspname in ('app', 'ops')"
        "  union all select n.nspacl from pg_namespace n where n.nspname in ('app', 'ops')"
        "  union all select p.proacl from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        "   where n.nspname in ('app', 'ops')"
        ") s, lateral aclexplode(s.acl) a where s.acl is not null and a.grantee = 0"
    ).fetchone()
    assert public_acl == (0,)
    # Functions default to PUBLIC EXECUTE when their ACL is NULL: every routine must have an explicit ACL.
    null_acl = db_conn.execute(
        "select array_agg(p.oid::regprocedure::text) from pg_proc p"
        " join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('app', 'ops') and p.proacl is null"
    ).fetchone()
    assert null_acl == (None,)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_roles_get_permission_denied_on_every_table(
    db_conn: psycopg.Connection, world_a: World, role: str
) -> None:
    claims = f'{{"sub": "{uuid.uuid4()}", "role": "{role}"}}'
    for table in _all_tables(db_conn):
        with (
            pytest.raises(errors.InsufficientPrivilege),
            as_role(db_conn, role, workspace_id=world_a.workspace_id),
        ):
            db_conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            db_conn.execute(sql.SQL("select 1 from {} limit 1").format(table_ident(table)))
    with pytest.raises(errors.InsufficientPrivilege), as_role(db_conn, "authenticated"):
        db_conn.execute("insert into app.workspaces (name) values ('Synthetic intruder workspace')")


@pytest.mark.parametrize("role", ["anon", "authenticated", "service_role"])
def test_client_roles_cannot_call_private_functions(db_conn: psycopg.Connection, role: str) -> None:
    for call in (
        "select * from ops.active_workspace_ids()",
        "select app.current_workspace_id()",
        "call ops.apply_security_baseline()",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_role(db_conn, role):
            db_conn.execute(call)


def test_backend_cannot_run_owner_maintenance(db_conn: psycopg.Connection) -> None:
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn):
        db_conn.execute("call ops.apply_security_baseline()")
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn):
        db_conn.execute("create table app.sneaky (id int)")


def test_default_privileges_keep_future_objects_private(db_conn: psycopg.Connection) -> None:
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("create table app.future_table (id uuid primary key)")
        db_conn.execute("create function app.future_fn() returns int language sql as 'select 1'")
        for role in CLIENT_ROLES:
            assert db_conn.execute(
                "select has_table_privilege(%s, 'app.future_table', 'SELECT')", (role,)
            ).fetchone() == (False,)
        assert db_conn.execute(
            "select has_function_privilege('public', 'app.future_fn()', 'EXECUTE')"
        ).fetchone() == (False,)
        assert db_conn.execute(
            "select has_table_privilege('suv_backend', 'app.future_table', 'SELECT')"
        ).fetchone() == (False,)


def test_backend_role_cannot_bypass_rls_or_escalate(db_conn: psycopg.Connection) -> None:
    """Roles are cluster-global: migrations verify a pre-existing suv_backend fails closed."""
    assert db_conn.execute("select ops.backend_role_problems()").fetchone() == ([],)
    unsafe = f"suv_test_unsafe_{uuid.uuid4().hex[:12]}"
    db_conn.execute(sql.SQL("create role {} login bypassrls createdb").format(sql.Identifier(unsafe)))
    try:
        db_conn.execute(sql.SQL("grant pg_monitor to {}").format(sql.Identifier(unsafe)))
        row = db_conn.execute("select ops.backend_role_problems(%s)", (unsafe,)).fetchone()
        assert row is not None
        assert set(row[0]) == {"BYPASSRLS", "CREATEDB", "LOGIN", "member of another role"}
    finally:
        db_conn.execute(sql.SQL("drop role {}").format(sql.Identifier(unsafe)))
    missing = db_conn.execute("select ops.backend_role_problems('suv_test_no_such_role')").fetchone()
    assert missing == (["role suv_test_no_such_role does not exist"],)
    for role in ("suv_backend", *CLIENT_ROLES):
        with pytest.raises(errors.InsufficientPrivilege), as_role(db_conn, role):
            db_conn.execute("select ops.backend_role_problems()")


def test_security_definer_function_is_hardened(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute(
        "select p.prosecdef, p.proconfig, p.provolatile,"
        " has_function_privilege('suv_backend', p.oid, 'EXECUTE')"
        " from pg_proc p where p.oid = 'ops.active_workspace_ids()'::regprocedure"
    ).fetchone()
    assert row is not None
    secdef, config, volatility, backend_exec = row
    assert secdef is True
    assert config == ['search_path=""']
    assert volatility == "s"
    assert backend_exec is True
    # It is the only SECURITY DEFINER routine in the private schemas.
    others = db_conn.execute(
        "select array_agg(p.oid::regprocedure::text) from pg_proc p"
        " join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('app', 'ops') and p.prosecdef"
    ).fetchone()
    assert others == (["ops.active_workspace_ids()"],)
    # Every app/ops routine pins its search_path.
    unpinned = db_conn.execute(
        "select array_agg(p.oid::regprocedure::text) from pg_proc p"
        " join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('app', 'ops')"
        " and not coalesce(p.proconfig, '{}') @> array['search_path=\"\"']"
    ).fetchone()
    assert unpinned == (None,)


# ---------------------------------------------------------------------------------------
# suv_backend privileges
# ---------------------------------------------------------------------------------------


def test_backend_privilege_matrix(db_conn: psycopg.Connection) -> None:
    tables = _all_tables(db_conn)
    rows = {
        table: db_conn.execute(
            "select has_table_privilege('suv_backend', %(t)s, 'SELECT'),"
            " has_table_privilege('suv_backend', %(t)s, 'INSERT'),"
            " has_any_column_privilege('suv_backend', %(t)s, 'UPDATE'),"
            " has_table_privilege('suv_backend', %(t)s, 'DELETE'),"
            " has_table_privilege('suv_backend', %(t)s, 'TRUNCATE'),"
            " has_table_privilege('suv_backend', %(t)s, 'REFERENCES,TRIGGER')",
            {"t": table},
        ).fetchone()
        for table in tables
    }
    for table, privs in rows.items():
        assert privs is not None
        select, _insert, update, delete, truncate, ddlish = privs
        assert select, table
        assert not truncate, table
        assert not ddlish, table
        if table in APPEND_ONLY:
            assert not update, f"{table} is append-only"
            assert not delete, f"{table} is append-only"
    deletable = {t for t, p in rows.items() if p and p[3]}
    assert deletable == {"ops.query_snapshots", "ops.idempotency_records"}
    insertable = {t for t, p in rows.items() if p and p[1]}
    assert set(tables) - insertable == {"app.workspaces"}


@pytest.mark.parametrize(
    ("table", "frozen_column"),
    [
        ("app.valuations", "scenarios"),
        ("app.valuations", "base_contribution_minor"),
        ("app.cost_profiles", "assumptions"),
        ("ops.source_snapshots", "content_hash"),
        ("ops.api_credentials", "scopes"),
        ("app.workspaces", "active"),
        ("app.memberships", "user_id"),
        ("app.owner_notes", "label"),
    ],
)
def test_backend_column_grants_freeze_content(
    db_conn: psycopg.Connection, table: str, frozen_column: str
) -> None:
    assert db_conn.execute(
        "select has_column_privilege('suv_backend', %s, %s, 'UPDATE')", (table, frozen_column)
    ).fetchone() == (False,)


# ---------------------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------------------

ISOLATION_TABLES = (
    "app.listings",
    "app.listing_revisions",
    "app.sources",
    "app.config_revisions",
    "app.search_profiles",
)


def test_backend_without_workspace_guc_sees_nothing(db_conn: psycopg.Connection, world_a: World) -> None:
    with backend(db_conn):
        for table in (
            *ISOLATION_TABLES,
            "app.workspaces",
            "app.memberships",
            "ops.jobs",
            "ops.api_credentials",
        ):
            count = db_conn.execute(sql.SQL("select count(*) from {}").format(table_ident(table))).fetchone()
            assert count == (0,), table
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn):
        db_conn.execute(
            "insert into ops.jobs (workspace_id, job_type, dedup_key) values (%s, 'valuation', %s)",
            (world_a.workspace_id, unique("job")),
        )


def test_empty_guc_fails_closed_and_malformed_guc_errors(db_conn: psycopg.Connection, world_a: World) -> None:
    with backend(db_conn, ""):
        assert db_conn.execute("select count(*) from app.listings").fetchone() == (0,)
    with pytest.raises(errors.InvalidTextRepresentation), backend(db_conn, "not-a-uuid"):
        db_conn.execute("select count(*) from app.listings")


def test_backend_sees_only_the_selected_workspace(
    db_conn: psycopg.Connection, world_a: World, world_b: World
) -> None:
    for world, other in ((world_a, world_b), (world_b, world_a)):
        with backend(db_conn, world.workspace_id):
            for table in ISOLATION_TABLES:
                workspaces = db_conn.execute(
                    sql.SQL("select distinct workspace_id from {}").format(table_ident(table))
                ).fetchall()
                assert workspaces == [(world.workspace_id,)], table
            assert db_conn.execute("select id from app.workspaces").fetchall() == [(world.workspace_id,)]
            assert db_conn.execute(
                "select count(*) from app.listings where id = %s", (other.listing_id,)
            ).fetchone() == (0,)
            updated = db_conn.execute(
                "update app.listings set row_version = row_version + 1 where id = %s", (other.listing_id,)
            )
            assert updated.rowcount == 0
            assert db_conn.execute(
                "select count(*) from app.listings where id = %s", (world.listing_id,)
            ).fetchone() == (1,)


def test_backend_cannot_insert_or_move_rows_into_another_workspace(
    db_conn: psycopg.Connection, world_a: World, world_b: World
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, world_a.workspace_id):
        db_conn.execute(
            "insert into ops.jobs (workspace_id, job_type, dedup_key) values (%s, 'valuation', %s)",
            (world_b.workspace_id, unique("job")),
        )
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, world_a.workspace_id):
        db_conn.execute(
            "update app.sources set workspace_id = %s where id = %s",
            (world_b.workspace_id, world_a.source_id),
        )
    # Even a correctly scoped insert cannot reference another workspace's parent.
    with pytest.raises(errors.ForeignKeyViolation), backend(db_conn, world_a.workspace_id):
        db_conn.execute(
            "insert into app.listings (workspace_id, source_id, source_listing_id, canonical_url,"
            " identity_method, identity_material, identity_hash, identity_confidence, first_seen_at,"
            " last_seen_at) values (%s, %s, 'TEST-X', 'https://synthetic-dealer.example/v/TEST-X',"
            " 'provider_id', 'TEST-X', %s, 'high', now(), now())",
            (world_a.workspace_id, world_b.source_id, sha("TEST-X")),
        )


def test_backend_can_work_inside_its_workspace(db_conn: psycopg.Connection, world_a: World) -> None:
    with backend(db_conn, world_a.workspace_id):
        job = db_conn.execute(
            "insert into ops.jobs (workspace_id, job_type, dedup_key)"
            " values (%s, 'valuation', %s) returning id",
            (world_a.workspace_id, unique("job")),
        ).fetchone()
        assert job is not None
        db_conn.execute(
            "insert into ops.audit_events (workspace_id, actor_principal_id, actor_kind, action, target_type,"
            " target_id) values (%s, %s, 'system', 'job.enqueue', 'job', %s)",
            (world_a.workspace_id, uuid.uuid4(), job[0]),
        )
        db_conn.execute(
            "update app.listings set last_seen_at = greatest(last_seen_at, now()),"
            " row_version = row_version + 1 where id = %s",
            (world_a.listing_id,),
        )
        snapshot = db_conn.execute(
            "insert into ops.query_snapshots (workspace_id, principal_id, query_name, filter_hash,"
            " result_ids,"
            " expires_at) values (%s, %s, 'reviews_list_pending', %s, %s, now() + interval '10 minutes')"
            " returning id",
            (world_a.workspace_id, uuid.uuid4(), sha("filter"), [uuid.uuid4()]),
        ).fetchone()
        assert snapshot is not None
        assert db_conn.execute("delete from ops.query_snapshots where id = %s", (snapshot[0],)).rowcount == 1


# ---------------------------------------------------------------------------------------
# Bootstrap policies
# ---------------------------------------------------------------------------------------


def test_membership_and_workspace_bootstrap_by_user(
    db_conn: psycopg.Connection, seed: Seed, world_a: World, world_b: World
) -> None:
    user = seed.user()
    stranger = seed.user()
    inactive_ws = seed.workspace("Inactive membership")
    seed.membership(world_a.workspace_id, user, "owner")
    seed.membership(world_b.workspace_id, user, "viewer")
    seed.membership(inactive_ws, user, "reviewer", active=False)
    seed.membership(world_a.workspace_id, stranger, "reviewer")
    with backend(db_conn, user_id=user):
        memberships = db_conn.execute(
            "select workspace_id, role, active from app.memberships order by role"
        ).fetchall()
        assert sorted(memberships) == sorted(
            [
                (world_a.workspace_id, "owner", True),
                (world_b.workspace_id, "viewer", True),
                (inactive_ws, "reviewer", False),
            ]
        )
        visible_ws = {r[0] for r in db_conn.execute("select id from app.workspaces").fetchall()}
        assert visible_ws == {world_a.workspace_id, world_b.workspace_id}
        # Bootstrap grants no access to workspace data.
        assert db_conn.execute("select count(*) from app.listings").fetchone() == (0,)
    # An authenticated stranger with no membership sees nothing.
    other = seed.user()
    with backend(db_conn, user_id=other):
        assert db_conn.execute("select count(*) from app.memberships").fetchone() == (0,)
        assert db_conn.execute("select count(*) from app.workspaces").fetchone() == (0,)
    # Membership writes are workspace-scoped: cannot add yourself to another workspace.
    with (
        pytest.raises(errors.InsufficientPrivilege),
        backend(db_conn, world_a.workspace_id, user_id=user),
    ):
        db_conn.execute(
            "insert into app.memberships (workspace_id, user_id, role) values (%s, %s, 'owner')",
            (inactive_ws, other),
        )


def test_backend_manages_members_of_its_own_workspace_only(
    db_conn: psycopg.Connection, seed: Seed, world_a: World, world_b: World
) -> None:
    """Owner-guarded membership writes run as suv_backend: FK to auth.users, column-limited update."""
    ws = world_a.workspace_id
    member = seed.user()
    with backend(db_conn, ws):
        db_conn.execute(
            "insert into app.memberships (workspace_id, user_id, role) values (%s, %s, 'viewer')",
            (ws, member),
        )
        assert (
            db_conn.execute(
                "update app.memberships set role = 'reviewer' where workspace_id = %s and user_id = %s",
                (ws, member),
            ).rowcount
            == 1
        )
    with pytest.raises(errors.ForeignKeyViolation), backend(db_conn, ws):
        db_conn.execute(
            "insert into app.memberships (workspace_id, user_id, role) values (%s, %s, 'viewer')",
            (ws, uuid.uuid4()),
        )
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, ws):
        db_conn.execute("update app.memberships set user_id = %s where user_id = %s", (seed.user(), member))
    # The member's bootstrap view shows the new role; workspace B's backend cannot see or change it.
    with backend(db_conn, user_id=member):
        assert db_conn.execute("select workspace_id, role from app.memberships").fetchall() == [
            (ws, "reviewer")
        ]
    with backend(db_conn, world_b.workspace_id):
        assert (
            db_conn.execute(
                "update app.memberships set active = false where user_id = %s", (member,)
            ).rowcount
            == 0
        )


def test_active_workspace_ids_for_backend(db_conn: psycopg.Connection, seed: Seed, world_a: World) -> None:
    inactive = seed.workspace("Inactive", active=False)
    with backend(db_conn):
        ids = {r[0] for r in db_conn.execute("select * from ops.active_workspace_ids()").fetchall()}
    assert world_a.workspace_id in ids
    assert inactive not in ids


def test_credential_bootstrap_by_token_hash(db_conn: psycopg.Connection, seed: Seed, world_a: World) -> None:
    token_hash = sha(f"synthetic-token-{uuid.uuid4()}")
    credential = seed.insert(
        "ops.api_credentials",
        workspace_id=world_a.workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="mcp_client",
        role="reviewer",
        credential_kind="dev_local",
        token_hash=token_hash,
        scopes=["deals:read", "reviews:read"],
        label="Synthetic local dev credential",
        created_at=T0,
        expires_at=T0 + timedelta(days=3650),
    )
    with backend(db_conn, credential_hash=token_hash):
        rows = db_conn.execute("select id, workspace_id, scopes from ops.api_credentials").fetchall()
        assert rows == [(credential, world_a.workspace_id, ["deals:read", "reviews:read"])]
        assert (
            db_conn.execute(
                "update ops.api_credentials set last_used_at = now() where id = %s", (credential,)
            ).rowcount
            == 0
        )
    with backend(db_conn, credential_hash=sha("wrong")):
        assert db_conn.execute("select count(*) from ops.api_credentials").fetchone() == (0,)
    with backend(db_conn, world_a.workspace_id):
        assert (
            db_conn.execute(
                "update ops.api_credentials set last_used_at = now() where id = %s", (credential,)
            ).rowcount
            == 1
        )
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, world_a.workspace_id):
        db_conn.execute(
            "update ops.api_credentials set scopes = '{config:admin}' where id = %s", (credential,)
        )
    with pytest.raises(errors.CheckViolation):
        seed.insert(
            "ops.api_credentials",
            workspace_id=world_a.workspace_id,
            principal_id=uuid.uuid4(),
            principal_kind="mcp_client",
            role="reviewer",
            credential_kind="static_bearer",
            token_hash=sha("another"),
            scopes=["deals:read", "execute_sql"],
            label="Synthetic invalid scope",
            created_at=T0,
            expires_at=T0 + timedelta(days=30),
        )


# ---------------------------------------------------------------------------------------
# Append-only history
# ---------------------------------------------------------------------------------------


def _history_rows(seed: Seed, world: World) -> dict[str, UUID]:
    ws = world.workspace_id
    rows: dict[str, UUID] = {}
    rows["app.config_revisions"] = world.config_revision_id
    rows["app.listing_revisions"] = world.revision_id
    rows["app.detail_observations"] = seed.scalar(
        "select id from app.detail_observations where listing_id = %s limit 1", (world.listing_id,)
    )
    run = seed.crawl_run(ws, world.source_id)
    rows["app.listing_observations"] = seed.insert(
        "app.listing_observations",
        workspace_id=ws,
        source_id=world.source_id,
        listing_id=world.listing_id,
        crawl_run_id=run,
        position=0,
        source_listing_id="TEST-204",
        card_hash=sha("card"),
        card_material={"synthetic": True},
        observed_at=T0,
        ingestion_key=sha(unique("ingest")),
    )
    rows["app.listing_aliases"] = seed.insert(
        "app.listing_aliases",
        workspace_id=ws,
        source_id=world.source_id,
        listing_id=world.listing_id,
        alias_url="https://synthetic-dealer.example/old/TEST-204",
        alias_hash=sha(unique("alias")),
        reason="synthetic url change",
    )
    rows["app.field_evidence"] = seed.insert(
        "app.field_evidence",
        workspace_id=ws,
        listing_id=world.listing_id,
        revision_id=world.revision_id,
        field_path="price.amount_minor",
        raw_excerpt="2.750 EUR (synthetic)",
        method="css",
        confidence="high",
        claim_status="seller_claimed",
        observed_at=T0,
    )
    obs = seed.market_observation(ws)
    rows["app.market_observations"] = obs
    comp = seed.comparable_set(ws, world.listing_id, world.revision_id)
    rows["app.comparable_sets"] = comp
    rows["app.comparable_set_members"] = seed.insert(
        "app.comparable_set_members",
        workspace_id=ws,
        comparable_set_id=comp,
        market_observation_id=obs,
        disposition="excluded",
        reasons=["wrong_generation"],
    )
    rows["app.fx_rates"] = seed.fx_rate(ws)
    rows["app.cost_evidence"] = seed.cost_evidence(ws)
    case = seed.review_case(ws, world.listing_id, world.revision_id)
    rows["app.review_decisions"] = seed.decision(ws, case, world.listing_id, world.revision_id)
    rows["ops.audit_events"] = seed.insert(
        "ops.audit_events",
        workspace_id=ws,
        actor_principal_id=uuid.uuid4(),
        actor_kind="system",
        action="test.synthetic",
        target_type="listing",
        target_id=world.listing_id,
    )
    outbox = seed.outbox(ws, state="blocked", blocker_code="test_only")
    rows["ops.delivery_attempts"] = seed.insert(
        "ops.delivery_attempts",
        workspace_id=ws,
        outbox_id=outbox,
        attempt_id=uuid.uuid4(),
        attempt_number=1,
        provider="mcp_events",
        sent_at=T0,
        uncertain=True,
    )
    assert set(rows) == set(APPEND_ONLY)
    return rows


def test_append_only_history_rejects_update_and_delete(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    rows = _history_rows(seed, world_a)
    for table, row_id in rows.items():
        update = sql.SQL("update {} set created_at = created_at where id = {}").format(
            table_ident(table), sql.Literal(row_id)
        )
        delete = sql.SQL("delete from {} where id = {}").format(table_ident(table), sql.Literal(row_id))
        # suv_backend: no privilege at all.
        for statement in (update, delete):
            with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, world_a.workspace_id):
                db_conn.execute(statement)
        # Superuser/owner: the trigger still refuses (defence in depth).
        for statement in (update, delete):
            with pytest.raises(psycopg.Error) as exc:
                db_conn.execute(statement)
            assert exc.value.sqlstate == SV_APPEND_ONLY, table
        assert db_conn.execute(
            sql.SQL("select count(*) from {} where id = {}").format(table_ident(table), sql.Literal(row_id))
        ).fetchone() == (1,), table


def test_documented_maintenance_bypass_is_owner_only(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    audit = seed.insert(
        "ops.audit_events",
        workspace_id=world_a.workspace_id,
        actor_principal_id=uuid.uuid4(),
        actor_kind="system",
        action="test.maintenance",
        target_type="listing",
    )
    statement = "delete from ops.audit_events where id = %s"
    # The GUC alone does not help suv_backend (no DELETE privilege, not the owner).
    with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, world_a.workspace_id):
        db_conn.execute("select set_config('app.history_maintenance', 'on', true)")
        db_conn.execute(statement, (audit,))
    # The owner with the explicit transaction-local GUC may perform documented maintenance.
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("select set_config('app.history_maintenance', 'on', true)")
        assert db_conn.execute(statement, (audit,)).rowcount == 1
    assert db_conn.execute("select count(*) from ops.audit_events where id = %s", (audit,)).fetchone() == (1,)


def test_backend_review_flow_respects_grants(db_conn: psycopg.Connection, seed: Seed, world_a: World) -> None:
    """A full claim/submit as suv_backend works with exactly the granted privileges."""
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    case = seed.review_case(ws, listing, rev)
    holder = uuid.uuid4()
    with backend(db_conn, ws):
        claimed = db_conn.execute(
            "update app.review_cases set state = 'claimed', claim_holder = %s, claim_token_hash = %s,"
            " claimed_at = now(), claim_expires_at = now() + interval '5 minutes'"
            " where id = %s and row_version = 1 and state = 'pending' returning row_version",
            (holder, sha("synthetic-claim-token"), case),
        ).fetchone()
        assert claimed == (1,)
    with backend(db_conn, ws):
        decision = db_conn.execute(
            "insert into app.review_decisions (workspace_id, case_id, case_version, listing_id,"
            " listing_revision_id, actor_principal_id, actor_kind, outcome, reason_codes, summary,"
            " is_fixture)"
            " values (%s, %s, 1, %s, %s, %s, 'mcp_client', 'watch', %s, %s, true) returning id",
            (ws, case, listing, rev, holder, ["SYNTHETIC"], "Synthetic decision for a synthetic listing."),
        ).fetchone()
        assert decision is not None
        submitted = db_conn.execute(
            "update app.review_cases set state = 'watch', latest_decision_id = %s,"
            " row_version = row_version + 1,"
            " claim_holder = null, claim_token_hash = null, claimed_at = null, claim_expires_at = null"
            " where id = %s and state = 'claimed' and claim_holder = %s and claim_token_hash = %s"
            " and claim_expires_at > clock_timestamp() and row_version = 1",
            (decision[0], case, holder, sha("synthetic-claim-token")),
        )
        assert submitted.rowcount == 1
        db_conn.execute(
            "insert into ops.outbox (workspace_id, event_type, aggregate_type, aggregate_id,"
            " aggregate_version,"
            " payload, payload_hash, dedup_key, state, blocker_code, is_fixture)"
            " values (%s, 'review.decided', 'review_case', %s, 2, '{}', %s, %s, 'blocked', 'fixture', true)",
            (ws, case, sha("{}"), f"review.decided:{case}:2"),
        )
