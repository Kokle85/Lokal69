"""Search-page ingestion: ingestion-key dedup, seen windows, aliases, collisions, detail jobs (spec 9, 10)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.repos_sources_listings.support import (
    HOST,
    Env,
    card,
    discover,
    ingest,
    later,
    page,
    source_config,
    start_run,
)

from suv_deals.domain.enums import CoverageMode
from suv_deals.errors import ValidationFailed
from suv_deals.persistence import listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import RunOutcome
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def _listing_row(seed: Seed, listing_id: Any) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select first_seen_at, last_seen_at, canonical_url, detail_generation, quarantined, quarantine_reason,"
        " availability, row_version from app.listings where id = %s",
        (listing_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_duplicate_observation_is_a_no_op(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    run = await start_run(db, env)
    discovery = page(env.source_key, [card(slid)])
    first = await ingest(db, env, run, discovery)
    assert (first.stored, first.new_listings, len(first.detail_jobs)) == (1, 1, 1)
    listing_id = first.detail_jobs[0].listing_id
    before = _listing_row(seed, listing_id)
    again = await ingest(db, env, run, discovery)
    assert (again.stored, again.duplicates, again.new_listings, len(again.detail_jobs)) == (0, 1, 0, 0)
    assert seed.scalar("select count(*) from app.listing_observations where listing_id = %s", (listing_id,)) == 1
    assert seed.scalar("select count(*) from ops.jobs where listing_id = %s", (listing_id,)) == 1
    assert _listing_row(seed, listing_id) == before
    counters = seed.conn.execute(
        "select pages_fetched, cards_seen, new_listings, detail_jobs_enqueued, page_depth from ops.crawl_runs"
        " where id = %s",
        (run.id,),
    ).fetchone()
    assert counters == (1, 1, 1, 1, 1)


async def test_first_and_last_seen_semantics(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    middle = await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid)], fetched_at=later(60)))
    listing_id = middle.detail_jobs[0].listing_id
    # A page fetched earlier but ingested later: first_seen moves back, last_seen never decreases.
    await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid)], fetched_at=later(0)))
    row = _listing_row(seed, listing_id)
    assert (row["first_seen_at"], row["last_seen_at"]) == (later(0), later(60))
    await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid)], fetched_at=later(120)))
    row = _listing_row(seed, listing_id)
    assert (row["first_seen_at"], row["last_seen_at"]) == (later(0), later(120))


async def test_card_change_enqueues_detail_bound_to_generation(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now() where listing_id = %s", (listing_id,)
    )
    unchanged = await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid)], fetched_at=later(5)))
    assert (unchanged.unchanged_listings, len(unchanged.detail_jobs)) == (1, 0)
    changed = await ingest(
        db, env, await start_run(db, env), page(env.source_key, [card(slid, price_minor=265000)], fetched_at=later(10))
    )
    assert changed.changed_listings == 1 and len(changed.detail_jobs) == 1
    ref = changed.detail_jobs[0]
    assert ref.generation == 2
    key = seed.scalar("select dedup_key from ops.jobs where id = %s", (ref.job_id,))
    assert key == f"detail:{listing_id}:i1:g2"  # identity + incarnation + generation, never a semantic hash
    # A further change while that job still waits is deduplicated (no new generation).
    again = await ingest(
        db, env, await start_run(db, env), page(env.source_key, [card(slid, price_minor=255000)], fetched_at=later(15))
    )
    assert again.changed_listings == 1 and again.detail_jobs == () and again.detail_jobs_deduplicated == 1
    assert _listing_row(seed, listing_id)["detail_generation"] == 2


async def test_alias_recorded_for_changed_url(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    original = _listing_row(seed, listing_id)["canonical_url"]
    moved = f"https://{HOST}/vehicles/moved/{slid}"
    report = await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid, url=moved)], fetched_at=later(5)))
    assert report.aliases_recorded == 1 and report.new_listings == 0
    alias = seed.conn.execute(
        "select alias_url, alias_hash, reason, evidence from app.listing_aliases where listing_id = %s", (listing_id,)
    ).fetchone()
    assert alias is not None
    assert alias[0] == moved and alias[1] == sha(moved) and alias[2] == "canonical_url_changed"
    assert alias[3]["previous_url_hash"] == sha(original)
    assert _listing_row(seed, listing_id)["canonical_url"] == original
    replay = await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid, url=moved)], fetched_at=later(9)))
    assert replay.aliases_recorded == 0
    assert seed.scalar("select count(*) from app.listing_aliases where listing_id = %s", (listing_id,)) == 1


async def test_identity_hash_collision_is_quarantined_not_merged(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    tampered = seed.listing(
        env.workspace_id,
        env.source_id,
        source_listing_id=slid,
        identity_material="tampered material",
        identity_hash=sha(f"{env.source_key}:{slid}"),
    )
    report = await ingest(db, env, await start_run(db, env), page(env.source_key, [card(slid)]))
    assert report.identity_collisions == (slid,) and report.stored == 0 and report.detail_jobs == ()
    row = _listing_row(seed, tampered)
    assert row["quarantined"] and row["quarantine_reason"] == "identity_hash_collision"
    assert seed.scalar("select count(*) from app.listings where source_listing_id = %s", (slid,)) == 1
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'listing.identity_hash_collision'",
            (tampered,),
        )
        == 1
    )


async def test_card_only_source_gets_no_detail_jobs(db: Database, env: Env, seed: Seed) -> None:
    key = unique("cardonly").lower()
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(
            conn,
            env.system,
            [source_config(env.source_key), source_config(key, detail_mode="card_only", allowed_detail_paths=())],
        )
        source = await sources_repo.get_source_by_key(conn, env.system, key)
        run = await sources_repo.start_crawl_run(
            conn,
            env.system,
            source_id=source.id,
            profile_id=env.profiles["primary"],
            coverage_mode=CoverageMode.ROLLING_PAGES,
            adapter_version="fixture@1.0.0",
        )
    report = await ingest(db, env, run, page(key, [card(unique("SYN"))]))
    assert report.new_listings == 1 and report.detail_jobs == ()


async def test_page_validation(db: Database, env: Env) -> None:
    run = await start_run(db, env)
    with pytest.raises(ValidationFailed):
        await ingest(db, env, run, page("another_source", [card(unique("SYN"))]))
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, run.id, RunOutcome(completeness="cancelled"))
    with pytest.raises(ValidationFailed):
        await ingest(db, env, run, page(env.source_key, [card(unique("SYN"))]))


async def test_invalid_card_url_is_skipped_with_warning(db: Database, env: Env) -> None:
    good = card(unique("SYN"), position=1)
    bad = card(unique("SYN"), position=2, url="https://user:pw@dealer-a.synthetic.example/vehicles/x")
    report = await ingest(db, env, await start_run(db, env), page(env.source_key, [good, bad]))
    assert report.stored == 1 and any("position 2" in w for w in report.warnings)


async def test_complete_scan_absence_sets_unknown_never_removed(db: Database, env: Env, seed: Seed) -> None:
    gone, stays = unique("SYN"), unique("SYN")
    first = await start_run(db, env)
    report = await ingest(db, env, first, page(env.source_key, [card(gone), card(stays, position=1)]))
    ids = {j.listing_id for j in report.detail_jobs}
    seed.conn.execute("update app.listings set availability = 'available' where id = any(%s)", (list(ids),))
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, first.id, RunOutcome(completeness="complete"))
    second = await start_run(db, env)
    await ingest(db, env, second, page(env.source_key, [card(stays)], fetched_at=later(30)))
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, second.id, RunOutcome(completeness="complete"))
    async with unit_of_work(db, env.system) as conn:
        absent = await listings_repo.mark_complete_scan_absences(conn, env.system, second.id)
    gone_id = seed.scalar("select id from app.listings where source_listing_id = %s", (gone,))
    stays_id = seed.scalar("select id from app.listings where source_listing_id = %s", (stays,))
    assert absent.marked_unknown == (gone_id,)
    assert _listing_row(seed, gone_id)["availability"] == "unknown"  # never 'removed' or sold
    assert _listing_row(seed, stays_id)["availability"] == "available"
    event = seed.conn.execute(
        "select metadata from ops.audit_events where target_id = %s and action = 'listing.availability'", (gone_id,)
    ).fetchone()
    assert event is not None
    assert event[0]["reason_code"] == "not_seen_in_complete_scan"
    assert event[0]["evidence_kind"] == "complete_scan_absence"
    # A budget-limited (incomplete) traversal never produces absences.
    third = await start_run(db, env)
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, third.id, RunOutcome(completeness="budget_limited"))
        assert (await listings_repo.mark_complete_scan_absences(conn, env.system, third.id)).marked_unknown == ()
