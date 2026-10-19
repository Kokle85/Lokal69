"""Migration ``20261007000200_fixture_lineage_and_guards`` on a database that already has listings.

Every other test applies the migration to an EMPTY database, so its backfill never runs there.
Here the earlier migrations are applied, real and fixture listings are stored, and only then the
new migration runs: ``app.listings.is_fixture`` is backfilled from each listing's source mode
without touching ``updated_at``/``row_version`` (the touch trigger is disabled for the backfill),
becomes NOT NULL, and is frozen from then on. SYNTHETIC data only; a scratch database per test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from tests.db_harness import (
    SUPABASE_STUB,
    _ensure_migrator,
    _url_for,
    admin_url,
    apply_sql_file,
    drop_database,
    migration_files,
    migrator_role,
)
from tests.integration.db.helpers import Seed

pytestmark = pytest.mark.db

LINEAGE_MIGRATION = "20261007000200_fixture_lineage_and_guards.sql"


@pytest.fixture
def pre_lineage_database(db_url: str) -> Iterator[str]:
    """A scratch database migrated up to (excluding) the fixture-lineage migration, by the same
    role the session database uses (the superuser, or ``TEST_DATABASE_MIGRATOR_ROLE`` like the
    hosted Supabase ``postgres`` role). Yields the URL the remaining migrations must run with."""
    del db_url  # ensures PostgreSQL is reachable (skips otherwise)
    name = f"suv_test_lineage_{uuid.uuid4().hex[:10]}"
    role = migrator_role()
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        if role is None:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        else:
            _ensure_migrator(admin, role)
            admin.execute(
                sql.SQL("create database {} owner {}").format(sql.Identifier(name), sql.Identifier(role))
            )
    url = _url_for(name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            apply_sql_file(conn, SUPABASE_STUB)
            if role is not None:
                conn.execute(
                    sql.SQL(
                        "grant usage on schema auth to {}; grant select, references on auth.users to {}"
                    ).format(sql.Identifier(role), sql.Identifier(role))
                )
        migrate_url = url if role is None else _url_for(name, user=role, password=role)
        with psycopg.connect(migrate_url, autocommit=True) as conn:
            for path in migration_files():
                if path.name >= LINEAGE_MIGRATION:
                    break
                apply_sql_file(conn, path)
        yield migrate_url
    finally:
        drop_database(name)


def _lineage_migration() -> list[object]:
    found = [p for p in migration_files() if p.name == LINEAGE_MIGRATION]
    assert found, "the fixture-lineage migration is missing"
    later = [p for p in migration_files() if p.name > LINEAGE_MIGRATION]
    return [found[0], *later]


def test_backfill_labels_existing_listings_from_their_source_mode(pre_lineage_database: str) -> None:
    with psycopg.connect(pre_lineage_database, autocommit=True) as conn:
        seed = Seed(conn)
        assert (
            seed.scalar(
                "select count(*) from information_schema.columns where table_schema = 'app'"
                " and table_name = 'listings' and column_name = 'is_fixture'"
            )
            == 0
        )
        ws = seed.workspace("Lineage backfill")
        fixture_source = seed.source(ws)  # mode 'fixture'
        real_source = seed.source(ws, mode="public_html", adapter="synthetic_adapter")
        fixture_listing = seed.listing(ws, fixture_source)
        real_listing = seed.listing(ws, real_source)
        before = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "select id, updated_at, row_version from app.listings where workspace_id = %s", (ws,)
            ).fetchall()
        }

        for path in _lineage_migration():
            apply_sql_file(conn, path)  # type: ignore[arg-type]

        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "select id, is_fixture, updated_at, row_version from app.listings where workspace_id = %s",
                (ws,),
            ).fetchall()
        }
        assert rows[fixture_listing][0] is True and rows[real_listing][0] is False
        # Only the new column changed: no touch of updated_at, no version bump.
        assert {k: (v[1], v[2]) for k, v in rows.items()} == before
        assert (
            seed.scalar(
                "select is_nullable from information_schema.columns where table_schema = 'app'"
                " and table_name = 'listings' and column_name = 'is_fixture'"
            )
            == "NO"
        )
        # The touch trigger is enabled again and the lineage is frozen from now on.
        assert (
            seed.scalar(
                "select tgenabled from pg_trigger where tgname = 'listings_touch'"
                " and tgrelid = 'app.listings'::regclass"
            )
            == "O"
        )
        with pytest.raises(psycopg.Error, match="frozen at ingest"), conn.transaction():
            conn.execute("update app.listings set is_fixture = false where id = %s", (fixture_listing,))
        # Switching the source to a real mode afterwards never relabels the earlier listing.
        conn.execute("update app.sources set mode = 'public_html' where id = %s", (fixture_source,))
        assert seed.scalar("select is_fixture from app.listings where id = %s", (fixture_listing,)) is True
        newer = seed.listing(ws, fixture_source)
        assert seed.scalar("select is_fixture from app.listings where id = %s", (newer,)) is False
