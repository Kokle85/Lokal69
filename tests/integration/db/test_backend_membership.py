"""The migration owner (non-superuser `postgres` on hosted Supabase) can switch into
suv_backend, and RLS then applies even though the owner itself has BYPASSRLS."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import sql
from tests.db_harness import migrator_role

pytestmark = pytest.mark.db


def _owner_url(db_url: str, role: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(db_url)
    info["user"] = role
    info["password"] = role
    return psycopg.conninfo.make_conninfo("", **{k: v for k, v in info.items() if v is not None})


def test_backend_role_has_no_unsafe_memberships(db_conn: psycopg.Connection) -> None:
    assert db_conn.execute("select ops.backend_role_problems()").fetchone() == ([],)


def test_owner_can_set_role_and_rls_applies(db_url: str, db_conn: psycopg.Connection) -> None:
    role = migrator_role()
    if role is None:
        pytest.skip("only meaningful with TEST_DATABASE_MIGRATOR_ROLE (Supabase-like owner)")
    ws_a = db_conn.execute(
        "insert into app.workspaces (name) values ('Synthetic membership WS A') returning id"
    ).fetchone()
    ws_b = db_conn.execute(
        "insert into app.workspaces (name) values ('Synthetic membership WS B') returning id"
    ).fetchone()
    assert ws_a is not None and ws_b is not None
    with psycopg.connect(_owner_url(db_url, role), autocommit=True) as owner:
        attrs = owner.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        assert attrs == (False, True)
        assert owner.execute("select pg_has_role(current_user, 'suv_backend', 'USAGE')").fetchone() == (
            False,
        )
        with owner.transaction():
            owner.execute(sql.SQL("set local role {}").format(sql.Identifier("suv_backend")))
            owner.execute("select set_config('app.workspace_id', %s, true)", (str(ws_a[0]),))
            ids = {r[0] for r in owner.execute("select id from app.workspaces").fetchall()}
            assert ids == {ws_a[0]}
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                owner.execute(
                    "insert into app.sources (workspace_id, source_key, country, display_name, role,"
                    " mode, adapter, adapter_version)"
                    " values (%s, %s, 'DE', 'x', 'acquisition', 'fixture', 'x', 'x')",
                    (ws_b[0], f"synthetic_{uuid.uuid4().hex[:8]}"),
                )
