"""Detail ingestion: revisions, out-of-order generations, identity conflicts, quarantine, screening.

Spec sections 10, 14 and 25.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import expire_job_lease, member
from tests.integration.repos_sources_listings.support import (
    PARSER,
    Env,
    claim_detail,
    discover,
    later,
    refresh_and_claim,
    run_detail,
    vehicle,
)

from suv_deals.adapters.base import ParsedListing
from suv_deals.domain.enums import (
    AccessState,
    Availability,
    EligibilityState,
    Fuel,
    JobType,
    Role,
    TechnicalStatus,
)
from suv_deals.domain.filters import SCREENING_VERSION, ScreeningResult
from suv_deals.domain.identity import PromotionOutcome
from suv_deals.errors import Forbidden, ValidationFailed
from suv_deals.persistence import jobs, listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.listings_repo import DetailSnapshotRef
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def _listing(seed: Seed, listing_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select current_generation, current_observation_id, current_revision_id, availability,"
        " detail_generation,"
        " last_detail_success_at, eligibility_state, eligibility_profile, screening, screening_version,"
        " screened_at, identity_conflict, incarnation, row_version from app.listings where id = %s",
        (listing_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _revisions(seed: Seed, listing_id: UUID) -> list[tuple[int, str, int | None, bool]]:
    rows = seed.conn.execute(
        "select revision_number, semantic_hash, asking_minor, quarantined from app.listing_revisions"
        " where listing_id = %s order by revision_number",
        (listing_id,),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _current_price(seed: Seed, listing_id: UUID) -> int:
    return int(
        seed.scalar(
            "select r.asking_minor from app.listings l join app.listing_revisions r"
            " on r.id = l.current_revision_id where l.id = %s",
            (listing_id,),
        )
    )


async def test_first_detail_promotes_revision_with_evidence(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    job = await claim_detail(db, env)
    result = await run_detail(db, env, job, listing_id, vehicle(slid))
    assert result.outcome == PromotionOutcome.PROMOTE_NEW_REVISION and result.revision_number == 1
    assert result.promoted and not result.quarantined
    row = _listing(seed, listing_id)
    assert (
        row["current_generation"] == job.generation == 1 and row["current_revision_id"] == result.revision_id
    )
    assert row["availability"] == "available" and row["last_detail_success_at"] is not None
    assert seed.scalar("select state from ops.jobs where id = %s", (job.id,)) == "succeeded"
    evidence = seed.conn.execute(
        "select field_path, method, confidence, claim_status from app.field_evidence"
        " where revision_id = %s order by field_path",
        (result.revision_id,),
    ).fetchall()
    assert evidence == [
        ("price.amount_minor", "css", "high", None),
        ("vehicle.mileage_km", "json_ld", "high", None),
    ]
    typed = seed.conn.execute(
        "select asking_minor, currency, mileage_km, make, model, registration_year, registration_month, fuel"
        " from app.listing_revisions where id = %s",
        (result.revision_id,),
    ).fetchone()
    assert typed is not None and typed[:2] == (275000, "EUR") and str(typed[2]) == "187500.000000"
    assert typed[3:] == ("Volkswagen", "Tiguan", 2012, 5, "diesel")


async def test_unchanged_content_creates_no_new_revision(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    # Cosmetic differences (raw text, observation time) are not semantic changes.
    again = vehicle(slid, observed_at=later(30))
    job = await refresh_and_claim(db, env, listing_id)
    result = await run_detail(db, env, job, listing_id, again)
    assert result.outcome == PromotionOutcome.CONFIRM_UNCHANGED and result.revision_id is None
    assert len(_revisions(seed, listing_id)) == 1
    row = _listing(seed, listing_id)
    assert row["current_generation"] == 2 and row["last_detail_success_at"] == later(30)
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 2
    )


async def test_price_reversion_creates_chronological_revisions(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid, price_minor=275000))
    await run_detail(
        db, env, await refresh_and_claim(db, env, listing_id), listing_id, vehicle(slid, price_minor=260000)
    )
    await run_detail(
        db, env, await refresh_and_claim(db, env, listing_id), listing_id, vehicle(slid, price_minor=275000)
    )
    revisions = _revisions(seed, listing_id)
    assert [(n, price) for n, _, price, _ in revisions] == [(1, 275000), (2, 260000), (3, 275000)]
    assert revisions[0][1] == revisions[2][1] != revisions[1][1]  # A -> B -> A
    assert _current_price(seed, listing_id) == 275000
    assert _listing(seed, listing_id)["current_generation"] == 3


async def test_late_older_generation_is_historical_only(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    old_job = await claim_detail(db, env)  # generation 1, fetched first but finishes last
    new_job = await refresh_and_claim(db, env, listing_id)  # generation 2
    assert (old_job.generation, new_job.generation) == (1, 2)
    newer = await run_detail(
        db, env, new_job, listing_id, vehicle(slid, price_minor=255000, availability=Availability.RESERVED)
    )
    assert newer.outcome == PromotionOutcome.PROMOTE_NEW_REVISION
    late = await run_detail(db, env, old_job, listing_id, vehicle(slid, price_minor=299000))
    assert late.outcome == PromotionOutcome.HISTORICAL_ONLY and not late.promoted and late.revision_id is None
    row = _listing(seed, listing_id)
    assert row["current_generation"] == 2 and row["availability"] == "reserved"
    assert _current_price(seed, listing_id) == 255000
    stored = seed.conn.execute(
        "select generation, promoted, not_promoted_reason from app.detail_observations"
        " where listing_id = %s order by generation",
        (listing_id,),
    ).fetchall()
    assert stored == [(1, False, "historical_only"), (2, True, None)]


async def test_concurrent_detail_completions_serialize(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    first = await claim_detail(db, env, "worker-a")
    second = await refresh_and_claim(db, env, listing_id)
    results = await asyncio.gather(
        run_detail(db, env, second, listing_id, vehicle(slid, price_minor=240000)),
        run_detail(db, env, first, listing_id, vehicle(slid, price_minor=270000)),
    )
    row = _listing(seed, listing_id)
    assert row["current_generation"] == 2  # whatever order the locks were granted in
    assert _current_price(seed, listing_id) == 240000
    assert {r.outcome for r in results} <= {
        PromotionOutcome.PROMOTE_NEW_REVISION,
        PromotionOutcome.HISTORICAL_ONLY,
    }
    numbers = [n for n, *_ in _revisions(seed, listing_id)]
    assert numbers == list(range(1, len(numbers) + 1))
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 2
    )
    assert (
        seed.scalar(
            "select count(*) from ops.jobs where id = any(%s) and state = 'succeeded'",
            ([first.id, second.id],),
        )
        == 2
    )


async def test_identity_conflict_creates_new_incarnation(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    job = await refresh_and_claim(db, env, listing_id)
    other_car = vehicle(slid, make="Toyota", model="RAV4", first_registration="2009-03", fuel=Fuel.PETROL)
    result = await run_detail(db, env, job, listing_id, other_car)
    assert result.identity_conflict and result.outcome == "identity_conflict"
    assert result.job_listing_id == listing_id and result.listing_id != listing_id
    assert result.new_incarnation == 2 and "MAKE_CHANGED" in result.conflict_codes
    old, new = _listing(seed, listing_id), _listing(seed, result.listing_id)
    assert old["identity_conflict"] and old["availability"] == "unknown" and old["current_generation"] == 1
    assert new["identity_conflict"] and new["incarnation"] == 2 and new["current_generation"] == 1
    # Nothing is inherited: the new incarnation starts at revision 1 with its own evidence.
    assert [n for n, *_ in _revisions(seed, result.listing_id)] == [1]
    assert len(_revisions(seed, listing_id)) == 1
    assert (
        seed.scalar(
            "select not_promoted_reason from app.detail_observations"
            " where listing_id = %s and generation = 2",
            (listing_id,),
        )
        == "identity_conflict"
    )
    # Later search cards resolve to the newest incarnation.
    async with unit_of_work(db, env.system) as conn:
        found = await listings_repo.find_listing(conn, env.system, env.source_id, slid)
    assert found is not None and found.id == result.listing_id


async def test_removed_page_and_not_found(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    removed = ParsedListing(page_type="removed", access_state=AccessState.REMOVED)
    result = await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, removed)
    assert result.kind == "removed" and result.availability_after == Availability.REMOVED
    row = _listing(seed, listing_id)
    assert row["availability"] == "removed" and row["eligibility_state"] == EligibilityState.REJECTED.value
    assert len(_revisions(seed, listing_id)) == 1  # facts are kept; only availability changed
    event = seed.conn.execute(
        "select metadata from ops.audit_events where target_id = %s and action = 'listing.availability'"
        " order by occurred_at desc limit 1",
        (listing_id,),
    ).fetchone()
    assert event is not None and event[0]["evidence_kind"] == "source_removed_page"
    gone = ParsedListing(page_type="unknown", access_state=AccessState.NOT_FOUND)
    result = await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, gone)
    assert result.kind == "not_found" and _listing(seed, listing_id)["availability"] == "unknown"


async def test_challenge_page_is_not_ingested(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    job = await claim_detail(db, env)
    challenge = ParsedListing(page_type="challenge", access_state=AccessState.ACCESS_BLOCKED)
    with pytest.raises(ValidationFailed):
        await run_detail(db, env, job, listing_id, challenge)
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 0
    )
    assert seed.scalar("select state from ops.jobs where id = %s", (job.id,)) == "running"


async def test_parser_unhealthy_quarantines_new_revisions(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    job = await refresh_and_claim(db, env, listing_id)
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.set_technical_status(
            conn,
            env.system,
            env.source_id,
            TechnicalStatus.PARSER_UNHEALTHY,
            reason="synthetic drift tripwire",
        )
    before = _listing(seed, listing_id)
    result = await run_detail(db, env, job, listing_id, vehicle(slid, price_minor=100))
    assert result.quarantined and not result.promoted
    assert _listing(seed, listing_id)["current_revision_id"] == before["current_revision_id"]
    assert _current_price(seed, listing_id) == 275000  # reliable facts never overwritten
    revisions = _revisions(seed, listing_id)
    assert [(n, q) for n, _, _, q in revisions] == [(1, False), (2, True)]
    assert seed.scalar(
        "select quarantined from app.detail_observations where listing_id = %s and generation = 2",
        (listing_id,),
    )


async def test_screening_persisted_with_version_and_valuation_job(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    result = await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    assert result.eligibility_state == EligibilityState.ELIGIBLE_PRIMARY
    row = _listing(seed, listing_id)
    assert row["eligibility_state"] == "eligible_primary" and row["eligibility_profile"] == "primary"
    assert row["screening_version"] == SCREENING_VERSION and row["screened_at"] is not None
    screening = ScreeningResult.model_validate(row["screening"])
    assert screening.state == EligibilityState.ELIGIBLE_PRIMARY and screening.queue_label == "Primary queue"
    valuation = seed.conn.execute(
        "select job_type, dedup_key, payload from ops.jobs where id = %s", (result.valuation_job_id,)
    ).fetchone()
    assert valuation is not None and valuation[0] == "valuation"
    assert valuation[1] == f"valuation:{listing_id}:{result.revision_id}:eligible_primary"
    assert valuation[2]["config_revision_id"] == str(env.config_revision_id)
    # 200,000 km is never eligible: a rejected revision gets no valuation work.
    rejected = await run_detail(
        db, env, await refresh_and_claim(db, env, listing_id), listing_id, vehicle(slid, mileage="200000")
    )
    assert rejected.eligibility_state == EligibilityState.REJECTED and rejected.valuation_job_id is None


async def test_lease_lost_rolls_back_everything(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    job = await claim_detail(db, env)
    expire_job_lease(seed, job.id)
    with pytest.raises(LeaseLost):
        await run_detail(db, env, job, listing_id, vehicle(slid))
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 0
    )
    assert seed.scalar("select count(*) from app.listing_revisions where listing_id = %s", (listing_id,)) == 0
    assert _listing(seed, listing_id)["current_generation"] is None


async def test_transaction_retry_with_same_snapshot_ref_is_idempotent(
    db: Database, env: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    job = await claim_detail(db, env)
    ref = DetailSnapshotRef(parser_version=PARSER)
    document = ParsedListing(page_type="detail", access_state=AccessState.OK, listing=vehicle(slid))
    async with unit_of_work(db, env.system) as conn:
        first = await listings_repo.ingest_detail(
            conn, env.system, job, listing_id, document, ref, complete_job=False
        )
        replay = await listings_repo.ingest_detail(conn, env.system, job, listing_id, document, ref)
    assert first.outcome == PromotionOutcome.PROMOTE_NEW_REVISION
    assert replay.outcome == PromotionOutcome.DUPLICATE_REPLAY and replay.revision_id is None
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 1
    )
    assert len(_revisions(seed, listing_id)) == 1


async def test_job_must_belong_to_the_listing(db: Database, env: Env) -> None:
    slid, other = unique("SYN"), unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    other_id, _ = await discover(db, env, other)
    job = await claim_detail(db, env)
    target = other_id if job.listing_id == listing_id else listing_id
    with pytest.raises(ValidationFailed):
        await run_detail(db, env, job, target, vehicle(slid))


async def test_reappearing_listing_with_same_facts_gets_no_duplicate_revision(
    db: Database, env: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    removed = ParsedListing(page_type="removed", access_state=AccessState.REMOVED)
    await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, removed)
    back = await run_detail(db, env, await refresh_and_claim(db, env, listing_id), listing_id, vehicle(slid))
    assert back.promoted and back.revision_id is None and back.availability_after == Availability.AVAILABLE
    assert len(_revisions(seed, listing_id)) == 1
    row = _listing(seed, listing_id)
    assert row["availability"] == "available" and row["current_generation"] == 3
    assert row["eligibility_state"] == "eligible_primary"
    assert back.valuation_job_id is not None  # eligibility changed from rejected back to eligible


async def test_late_job_of_superseded_incarnation_is_evidence_only(
    db: Database, env: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    conflicting = await refresh_and_claim(db, env, listing_id)  # generation 2
    late = await refresh_and_claim(db, env, listing_id)  # generation 3, still for the old incarnation
    other_car = vehicle(slid, make="Toyota", model="RAV4", first_registration="2009-03", fuel=Fuel.PETROL)
    first = await run_detail(db, env, conflicting, listing_id, other_car)
    assert first.identity_conflict and first.new_incarnation == 2
    result = await run_detail(db, env, late, listing_id, other_car)
    assert result.outcome == PromotionOutcome.HISTORICAL_ONLY and not result.promoted
    assert seed.scalar("select max(incarnation) from app.listings where source_listing_id = %s", (slid,)) == 2
    assert (
        seed.scalar(
            "select not_promoted_reason from app.detail_observations"
            " where listing_id = %s and generation = 3",
            (listing_id,),
        )
        == "superseded_incarnation"
    )
    assert seed.scalar("select state from ops.jobs where id = %s", (late.id,)) == "succeeded"


async def test_late_completion_after_lease_recovery_and_newer_unavailable_observation(
    db: Database, env: Env, seed: Seed
) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    stale = await claim_detail(db, env, "worker-a")  # generation 1
    expire_job_lease(seed, stale.id)
    reaped = await jobs.reap_expired(db, env.workspace_id)
    assert stale.id in reaped.requeued
    seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (stale.id,))
    recovered = await claim_detail(db, env, "worker-b")
    assert (
        recovered.id == stale.id and recovered.generation == stale.generation == 1
    )  # retries keep generation
    newer = await refresh_and_claim(db, env, listing_id)  # generation 2: the ad was removed meanwhile
    removed = ParsedListing(page_type="removed", access_state=AccessState.REMOVED)
    await run_detail(db, env, newer, listing_id, removed)
    late = await run_detail(db, env, recovered, listing_id, vehicle(slid))
    assert late.outcome == PromotionOutcome.HISTORICAL_ONLY and not late.promoted
    row = _listing(seed, listing_id)
    assert row["availability"] == "removed" and row["current_generation"] == 2  # never regressed
    with pytest.raises(LeaseLost):  # the original holder lost the lease; nothing it writes commits
        await run_detail(db, env, stale, listing_id, vehicle(slid, price_minor=1000))
    assert (
        seed.scalar("select count(*) from app.detail_observations where listing_id = %s", (listing_id,)) == 2
    )


async def test_refresh_scopes_dedup_and_generation_allocation(db: Database, env: Env, seed: Seed) -> None:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)  # generation 1 waiting
    reviewer = member(env.workspace_id, Role.REVIEWER)
    viewer = member(env.workspace_id, Role.VIEWER)
    async with unit_of_work(db, reviewer) as conn:
        assert (
            await listings_repo.request_detail_refresh(conn, reviewer, listing_id, reason="recheck") is None
        )
        with pytest.raises(Forbidden):
            await listings_repo.request_detail_refresh(
                conn, reviewer, listing_id, reason="detail is system work", job_type=JobType.DETAIL
            )
    async with unit_of_work(db, viewer) as conn:
        with pytest.raises(Forbidden):
            await listings_repo.request_detail_refresh(conn, viewer, listing_id, reason="viewer recheck")
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now() where listing_id = %s", (listing_id,)
    )
    async with unit_of_work(db, reviewer) as conn:
        ref = await listings_repo.request_detail_refresh(conn, reviewer, listing_id, reason="price recheck")
    assert ref is not None and ref.generation == 2
    assert seed.scalar("select job_type from ops.jobs where id = %s", (ref.job_id,)) == "recheck"
    async with unit_of_work(db, env.system) as conn:
        allocated = [
            await listings_repo.allocate_detail_generation(conn, env.system, listing_id) for _ in range(3)
        ]
    assert allocated == [3, 4, 5]
    async with unit_of_work(db, viewer) as conn:
        with pytest.raises(Forbidden):
            await listings_repo.allocate_detail_generation(conn, viewer, listing_id)
