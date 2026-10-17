"""Workspace isolation for the WP7b1 repositories (ADR 0001, spec 12).

Repository calls carry explicit workspace predicates (foreign objects are `NotFound`, never
revealed); underneath, ``suv_backend`` RLS rejects any cross-workspace access even for raw SQL.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.repos_sources_listings.support import (
    HOST,
    Env,
    card,
    claim_detail,
    discover,
    page,
    parsed,
    run_detail,
    start_run,
    vehicle,
)

from suv_deals.domain.enums import JobType, TechnicalStatus
from suv_deals.errors import NotFound
from suv_deals.persistence import listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.listings_repo import DetailSnapshotRef
from suv_deals.persistence.sources_repo import RunOutcome, SourceRoute
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def test_raw_sql_as_backend_cannot_cross_workspaces(
    db: Database, env: Env, env_b: Env, seed: Seed
) -> None:
    listing_id, _ = await discover(db, env, unique("SYN"))
    async with db.transaction(workspace_id=env_b.workspace_id) as conn:
        for table in ("app.listings", "app.sources", "app.listing_observations", "ops.source_schedules"):
            cur = await conn.execute(
                f"select count(*) as n from {table} where workspace_id = %s", (env.workspace_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row["n"] == 0, table
        cur = await conn.execute(
            "update app.listings set availability = 'removed' where id = %s", (listing_id,)
        )
        assert cur.rowcount == 0
        cur = await conn.execute(
            "update app.sources set paused = true, pause_reason = 'x x x', paused_at = now() where id = %s",
            (env.source_id,),
        )
        assert cur.rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction(workspace_id=env_b.workspace_id) as conn:
            await conn.execute(
                "insert into ops.robots_revisions (workspace_id, host, fetched_at, parse_ok)"
                " values (%s, %s, now(), true)",
                (env.workspace_id, HOST),
            )
    assert seed.scalar("select availability from app.listings where id = %s", (listing_id,)) == "unknown"


async def test_repositories_treat_foreign_objects_as_missing(
    db: Database, env: Env, env_b: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, run = await discover(db, env, slid)
    job = await claim_detail(db, env)
    foreign = env_b.system
    calls: list[Any] = [
        lambda c: sources_repo.get_source(c, foreign, env.source_id),
        lambda c: sources_repo.record_access_block(c, foreign, env.source_id, SourceRoute(host=HOST), "403"),
        lambda c: sources_repo.set_technical_status(
            c, foreign, env.source_id, TechnicalStatus.DEGRADED, reason="foreign"
        ),
        lambda c: sources_repo.advance_schedule(c, foreign, env.schedule_id),
        lambda c: sources_repo.finish_crawl_run(c, foreign, run.id, RunOutcome(completeness="complete")),
        lambda c: sources_repo.get_crawl_run(c, foreign, run.id),
        lambda c: listings_repo.get_listing(c, foreign, listing_id),
        lambda c: listings_repo.allocate_detail_generation(c, foreign, listing_id),
        lambda c: listings_repo.request_detail_refresh(
            c, foreign, listing_id, reason="x", job_type=JobType.DETAIL
        ),
        lambda c: listings_repo.ingest_search_page(
            c, foreign, run.id, page(env.source_key, [card(unique("SYN"))])
        ),
        lambda c: listings_repo.ingest_detail(
            c, foreign, job, listing_id, listings_repo_parsed(slid), DetailSnapshotRef(parser_version="p@1")
        ),
    ]
    for call in calls:
        with pytest.raises(NotFound):
            async with unit_of_work(db, foreign) as conn:
                await call(conn)
    # Nothing of workspace A changed, and A's own pipeline still works.
    assert seed.scalar("select detail_generation from app.listings where id = %s", (listing_id,)) == 1
    assert (
        seed.scalar("select technical_status from app.sources where id = %s", (env.source_id,))
        == "fixture_tested"
    )
    result = await run_detail(db, env, job, listing_id, vehicle(slid))
    assert result.promoted
    async with unit_of_work(db, env_b.owner) as conn:
        assert await listings_repo.find_listing(conn, env_b.owner, env.source_id, slid) is None
        assert (await sources_repo.list_sources(conn, env_b.owner)).items[0].source_id == env_b.source_id


async def test_a_run_of_another_source_cannot_be_fed(db: Database, env: Env, env_b: Env) -> None:
    run_b = await start_run(db, env_b)
    with pytest.raises(NotFound):
        async with unit_of_work(db, env.system) as conn:
            await listings_repo.ingest_search_page(
                conn, env.system, run_b, page(env.source_key, [card(unique("SYN"))])
            )


def listings_repo_parsed(slid: str) -> Any:
    return parsed(vehicle(slid))
