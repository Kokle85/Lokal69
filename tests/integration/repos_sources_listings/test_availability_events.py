"""Availability transitions reach ``app.availability_events`` (spec 37.8/37.9) with their evidence.

The table comes with migration 20261006001000 (seller inquiries, another work package); the
audit-event part of the default sink is checked regardless, the table rows only where it exists.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.repos_sources_listings.support import (
    Env,
    card,
    claim_detail,
    discover,
    ingest,
    page,
    refresh_and_claim,
    run_detail,
    start_run,
    vehicle,
)

from suv_deals.adapters.base import ParsedListing
from suv_deals.domain.enums import AccessState, Availability, Fuel
from suv_deals.persistence import listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import RunOutcome
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def _events(seed: Seed, listing_id: UUID) -> list[dict[str, Any]]:
    if not seed.scalar("select to_regclass('app.availability_events') is not null"):
        pytest.skip("app.availability_events is not part of this schema yet (migration 20261006001000)")
    cur = seed.conn.execute(
        "select old_availability, new_availability, evidence_kind, reason, crawl_run_id,"
        " detail_observation_id, source_reference, confidence, promote_current"
        " from app.availability_events where listing_id = %s order by created_at, id",
        (listing_id,),
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _audit_kinds(seed: Seed, listing_id: UUID) -> list[str]:
    rows = seed.conn.execute(
        "select metadata->>'evidence_kind' from ops.audit_events"
        " where target_id = %s and action = 'listing.availability' order by occurred_at, id",
        (listing_id,),
    ).fetchall()
    return [r[0] for r in rows]


async def test_detail_transitions_cite_their_detail_observation(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    removed = ParsedListing(page_type="removed", access_state=AccessState.REMOVED)
    await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, removed)
    assert _audit_kinds(seed, listing_id) == ["source_observation", "source_removed_page"]
    events = _events(seed, listing_id)
    assert [(e["old_availability"], e["new_availability"], e["evidence_kind"]) for e in events] == [
        ("unknown", "available", "source_observation"),
        ("available", "removed", "source_removed_page"),
    ]
    observation_ids = {
        r[0]
        for r in seed.conn.execute(
            "select id from app.detail_observations where listing_id = %s", (listing_id,)
        ).fetchall()
    }
    assert all(e["detail_observation_id"] in observation_ids for e in events)
    assert all(e["confidence"] == "high" and e["promote_current"] for e in events)


async def test_a_detail_page_that_reports_removed_is_removal_evidence(
    db: Database, env: Env, seed: Seed
) -> None:
    """A parsed detail page whose own availability is ``removed`` is an explicit removal, not a
    plain observation (the reviewed code labelled it ``source_observation``)."""
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    gone = vehicle(slid, availability=Availability.REMOVED)
    result = await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, gone)
    assert result.availability_after == Availability.REMOVED
    assert _audit_kinds(seed, listing_id)[-1] == "source_removed_page"
    assert _events(seed, listing_id)[-1]["evidence_kind"] == "source_removed_page"


async def test_absence_event_cites_the_complete_run(db: Database, env: Env, seed: Seed) -> None:
    gone, stays = unique("SYN"), unique("SYN")
    first = await start_run(db, env)
    report = await ingest(db, env, first, page(env.source_key, [card(gone), card(stays, position=1)]))
    ids = [j.listing_id for j in report.detail_jobs]
    seed.conn.execute("update app.listings set availability = 'available' where id = any(%s)", (ids,))
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, first.id, RunOutcome(completeness="complete"))
    second = await start_run(db, env)
    await ingest(db, env, second, page(env.source_key, [card(stays)]))
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, second.id, RunOutcome(completeness="complete"))
        absent = await listings_repo.mark_complete_scan_absences(conn, env.system, second.id)
    gone_id = absent.marked_unknown[0]
    (event,) = _events(seed, gone_id)
    assert (event["new_availability"], event["evidence_kind"], event["reason"]) == (
        "unknown",
        "complete_scan_absence",
        "not_seen_in_complete_scan",
    )
    assert event["crawl_run_id"] == second.id and event["confidence"] == "low"


async def test_identity_conflict_event_cites_the_conflicting_observation(
    db: Database, env: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    other = vehicle(slid, make="Toyota", model="RAV4", first_registration="2009-03", fuel=Fuel.PETROL)
    result = await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, other)
    assert result.identity_conflict
    last = _events(seed, listing_id)[-1]
    assert (last["old_availability"], last["new_availability"], last["reason"]) == (
        "available",
        "unknown",
        "identity_conflict_relisted",
    )
    conflict_row = seed.scalar(
        "select id from app.detail_observations where listing_id = %s and not_promoted_reason = %s",
        (listing_id, "identity_conflict"),
    )
    assert last["detail_observation_id"] == conflict_row and last["confidence"] == "medium"
