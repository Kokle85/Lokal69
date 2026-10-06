"""Real-PostgreSQL test harness.

Each test session creates an isolated, uniquely named database, installs the
Supabase emulation layer (only when the server is plain PostgreSQL rather than
a Supabase stack), applies every migration in order and drops the database at
the end. Safe for concurrent test runs.

Environment:
  TEST_DATABASE_ADMIN_URL      superuser URL used only to create/drop test databases
                               (default postgresql://suv:suv@127.0.0.1:5432/postgres)
  TEST_DATABASE_MIGRATOR_ROLE  optional; when set (e.g. ``suv_migrator``), migrations run as
                               that NON-superuser role with CREATEROLE + BYPASSRLS, which is
                               how the ``postgres`` role behaves on hosted Supabase. The role
                               is created on demand and owns the test database.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
SUPABASE_STUB = REPO_ROOT / "supabase" / "tests" / "supabase_emulation.sql"
DEFAULT_ADMIN_URL = "postgresql://suv:suv@127.0.0.1:5432/postgres"


def admin_url() -> str:
    return os.environ.get("TEST_DATABASE_ADMIN_URL", DEFAULT_ADMIN_URL)


def db_available() -> bool:
    try:
        with psycopg.connect(admin_url(), connect_timeout=3) as conn:
            conn.execute("select 1")
        return True
    except psycopg.Error:
        return False


def migrator_role() -> str | None:
    role = os.environ.get("TEST_DATABASE_MIGRATOR_ROLE") or None
    if role is not None and not role.replace("_", "").isalnum():
        raise ValueError("invalid TEST_DATABASE_MIGRATOR_ROLE")
    return role


def _url_for(dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    info = psycopg.conninfo.conninfo_to_dict(admin_url())
    info["dbname"] = dbname
    if user is not None:
        info["user"] = user
        info["password"] = password or ""
    return psycopg.conninfo.make_conninfo("", **{k: v for k, v in info.items() if v is not None})


def _ensure_migrator(admin: psycopg.Connection, role: str) -> None:
    """Create a Supabase-`postgres`-like role: LOGIN, NOSUPERUSER, CREATEROLE, BYPASSRLS."""
    exists = admin.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
    if exists is None:
        with contextlib.suppress(psycopg.errors.DuplicateObject):
            admin.execute(
                sql.SQL("create role {} login nosuperuser createrole bypassrls createdb password {}").format(
                    sql.Identifier(role), sql.Literal(role)
                )
            )


def migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())


def apply_sql_file(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


def create_migrated_database(prefix: str = "suv_test") -> tuple[str, str]:
    """Create a fresh database with emulation + migrations. Returns (dbname, url)."""
    dbname = f"{prefix}_{secrets.token_hex(6)}"
    role = migrator_role()
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        if role is not None:
            _ensure_migrator(admin, role)
            admin.execute(
                sql.SQL("create database {} owner {}").format(sql.Identifier(dbname), sql.Identifier(role))
            )
        else:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    url = _url_for(dbname)
    with psycopg.connect(url, autocommit=True) as conn:
        is_supabase = conn.execute(
            "select exists(select 1 from pg_namespace where nspname = 'auth')"
        ).fetchone()
        if not (is_supabase and is_supabase[0]):
            apply_sql_file(conn, SUPABASE_STUB)
        if role is not None:
            # Hosted Supabase lets `postgres` use and reference auth.users.
            conn.execute(
                sql.SQL(
                    "grant usage on schema auth to {}; grant select, references on auth.users to {}"
                ).format(sql.Identifier(role), sql.Identifier(role))
            )
    migrate_url = url if role is None else _url_for(dbname, user=role, password=role)
    with psycopg.connect(migrate_url, autocommit=True) as conn:
        for path in migration_files():
            apply_sql_file(conn, path)
    return dbname, url


def drop_database(dbname: str) -> None:
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity"
            " where datname = %s and pid <> pg_backend_pid()",
            (dbname,),
        )
        admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(dbname)))


@contextmanager
def migrated_database(prefix: str = "suv_test") -> Iterator[str]:
    dbname, url = create_migrated_database(prefix)
    try:
        yield url
    finally:
        drop_database(dbname)
