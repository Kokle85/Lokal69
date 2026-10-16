"""MK asking prices outside the strict spec-15 rules still orient the proceeds (ADR 0005).

Owner decision 2026-10-16: the MK resale rules are a framework for orientation, not strict rules.
Same-model MK asking prices of other model years or another fuel give a labelled relaxed
orientation (basis ``mk_asking_prices`` without a comparable set id), while the spec-15 set stays
``insufficient_comparables``. The same ad recorded on two days counts once. The listings are the
SYNTHETIC fixture dealer pages; every MK row is SYNTHETIC data of the fixture lineage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import PipelineEnv, build_env, by_slid, comparable, run, seed_fx
from tests.integration.pipeline.test_owner_estimate_proceeds import _estimate

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.comparables import MarketObservation
from suv_deals.domain.costs import proceeds_supported
from suv_deals.domain.enums import Confidence, CostLineStatus, EvidenceKind, Fuel, JobState
from suv_deals.domain.listings import PartialDate
from suv_deals.domain.money import Money
from suv_deals.persistence import inquiries_repo, market_repo, reviews_repo, valuation_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.queries import candidates
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db

#: One SYNTHETIC ad, recorded on two days (the daily re-recording of the same ad).
AD_URL = "https://www.mk-classifieds.example/AdDetails?ad=4711"


def _row(
    amount: str, *, year: int, days_ago: int, url: str | None = None, **vehicle: object
) -> MarketObservation:
    base = comparable(amount, year=year)
    update: dict[str, object] = {
        "id": uuid.uuid4(),
        "observed_at": datetime.now(UTC) - timedelta(days=days_ago),
    }
    if url is not None:
        update["url"] = url
    if vehicle:
        update["vehicle"] = base.vehicle.model_copy(update=vehicle)
    return base.model_copy(update=update)


async def _record(env: PipelineEnv, rows: list[MarketObservation]) -> None:
    async def insert(conn: Conn) -> None:
        for row in rows:
            await market_repo.insert_market_observation(conn, env.owner, row, confidence=Confidence.LOW)

    await run(env.ctx, env.owner, insert)


async def _value(env: PipelineEnv) -> list[valuation_repo.StoredValuation]:
    await seed_fx(env)
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    assert any(w.workspace_id == env.workspace_id and w.enqueued for w in tick.workspaces)
    reports = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-mo").run_until_idle()
    assert all(r.state == JobState.SUCCEEDED for r in reports), [(r.job_type, r.code) for r in reports]
    listing_ids = [row["id"] for row in by_slid(env).values()]

    async def load(conn: Conn) -> list[valuation_repo.StoredValuation]:
        found = []
        for listing_id in listing_ids:
            stored = await valuation_repo.current_valuation(conn, env.system, listing_id)
            if stored is not None and stored.inputs.proceeds is not None:
                found.append(stored)
        return found

    valued = await run(env.ctx, env.system, load)
    assert valued, "no fixture listing reached the scenario stage"
    return valued


async def test_same_model_mk_asking_prices_outside_the_strict_rules_orient_the_proceeds(
    db_url: str, seed: Seed
) -> None:
    env = await build_env(db_url, seed, name="MK orientation")
    try:
        ad_older = _row("9900", year=2014, days_ago=3, url=AD_URL)
        ad_newer = _row("9600", year=2014, days_ago=1, url=AD_URL.replace("https://www.", "http://"))
        newer_years = [_row("10400", year=2016, days_ago=2), _row("10800", year=2016, days_ago=2)]
        petrol = _row("7900", year=2011, days_ago=2, fuel=Fuel.PETROL)
        rows = [ad_older, ad_newer, *newer_years, petrol]
        await _record(env, rows)
        seeded = {str(r.id) for r in rows}

        valued = await _value(env)
        oriented = [
            v
            for v in valued
            if v.inputs.proceeds is not None
            and v.inputs.proceeds.basis == "mk_asking_prices"
            and v.inputs.proceeds.status == CostLineStatus.ESTIMATED
        ]
        assert oriented, [v.inputs.proceeds for v in valued]
        for stored in oriented:
            proceeds = stored.inputs.proceeds
            assert proceeds is not None
            assert proceeds.evidence_kind == EvidenceKind.ASKING_PRICE
            assert proceeds.comparable_set_id is None  # no spec-15 comparable set behind it
            assert set(proceeds.evidence_ids) <= seeded
            # The ad recorded on two days counts once: the newer record is used, never the older.
            assert str(ad_newer.id) in proceeds.evidence_ids
            assert str(ad_older.id) not in proceeds.evidence_ids
            # 4 eligible rows (fewer than ORIENTATION_LIMIT): another fuel and other model years
            # are labelled differences, never exclusions.
            assert set(proceeds.evidence_ids) == {
                str(ad_newer.id),
                *(str(r.id) for r in newer_years),
                str(petrol.id),
            }
            assert not proceeds_supported(proceeds)
            valuation = stored.valuation
            warnings = valuation.warnings
            assert any(w.startswith("PROCEEDS_FROM_MK_ORIENTATION") for w in warnings)
            assert any(w.startswith("MK_ORIENTATION:asking_price") for w in warnings)
            assert any("fuel_mismatch x" in w for w in warnings if w.startswith("MK_ORIENTATION"))
            assert any(w.startswith("ORIENTATION_ONLY") for w in warnings)
            assert valuation.research_candidate
            assert any(w.startswith("INSUFFICIENT_COMPARABLES") for w in warnings)  # strict set
            assert "proceeds: insufficient_comparables for an asking-price basis" not in valuation.unknowns
            assert not valuation.alert_eligible
            assert valuation.comparable is not None
            assert valuation.comparable.quality == "insufficient_comparables"
            # The oldest used point bounds the valuation's freshness (comparable_max_age_days 90).
            oldest = min(r.observed_at for r in rows if str(r.id) in proceeds.evidence_ids)
            assert valuation.expires_at is not None
            assert valuation.expires_at <= oldest + timedelta(days=90)
        await _assert_research_everywhere(env, [v.listing_id for v in oriented])
    finally:
        await env.close()


async def _assert_research_everywhere(env: PipelineEnv, listing_ids: list[uuid.UUID]) -> None:
    """Every owner-facing read shows an orientation valuation as a research candidate."""

    async def read(conn: Conn) -> None:
        for listing_id in listing_ids:
            detail = (await candidates.get_candidate(conn, env.owner, listing_id)).data
            assert detail.summary.research_candidate
            assert detail.latest_valuation is not None and detail.latest_valuation.research_candidate
        queue = await reviews_repo.list_pending_queue(
            conn,
            env.owner,
            reviews_repo.ReviewQueueFilters(),
            None,
            secret=b"SYNTHETIC-cursor-secret-mk-orientation-0001",
        )
        for item in queue.page.items:
            if item.listing_id in listing_ids:
                assert item.research_candidate

    await run(env.ctx, env.owner, read)


def _asking_bases(valued: list[valuation_repo.StoredValuation]) -> list[valuation_repo.StoredValuation]:
    return [
        v
        for v in valued
        if v.inputs.proceeds is not None
        and v.inputs.proceeds.basis == "mk_asking_prices"
        and v.inputs.proceeds.base is not None
    ]


async def test_a_close_asking_orientation_beats_a_strict_owner_estimate(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation close")
    try:
        # Year 2014 is outside the strict widened year window, so only the orientation sees it.
        close = [_row("9600", year=2014, days_ago=1), _row("9800", year=2014, days_ago=2)]
        await _record(env, [_estimate("12000"), *close])
        valued = await _value(env)
        bases = {v.inputs.proceeds.basis for v in valued if v.inputs.proceeds is not None}
        assert "owner_estimate" not in bases
        oriented = _asking_bases(valued)
        assert oriented, bases
        for stored in oriented:
            assert stored.inputs.proceeds is not None
            assert set(stored.inputs.proceeds.evidence_ids) == {str(r.id) for r in close}
        # Where the listing's fuel, gearbox and drive are known, the orientation is verified close
        # (a listing with unknowns falls to step c, which uses the same orientation).
        assert any(
            not any(w.startswith("ORIENTATION_DISTANT") for w in v.valuation.warnings) for v in oriented
        )
    finally:
        await env.close()


async def test_a_strict_owner_estimate_beats_a_distant_asking_orientation(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation distant")
    try:
        estimate = _estimate("9300")
        await _record(env, [estimate, _row("7900", year=2011, days_ago=2, fuel=Fuel.PETROL)])
        valued = await _value(env)
        from_estimate = [
            v for v in valued if v.inputs.proceeds is not None and v.inputs.proceeds.basis == "owner_estimate"
        ]
        assert from_estimate, [v.inputs.proceeds for v in valued]
        for stored in from_estimate:
            assert stored.inputs.proceeds is not None
            assert stored.inputs.proceeds.evidence_ids == (str(estimate.id),)
            warnings = stored.valuation.warnings
            assert not any(w.startswith("PROCEEDS_FROM_MK_ORIENTATION") for w in warnings)
            assert stored.valuation.research_candidate
    finally:
        await env.close()


async def test_a_distant_asking_orientation_is_labelled_distant(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation only distant")
    try:
        petrol = _row("7900", year=2011, days_ago=2, fuel=Fuel.PETROL)
        await _record(env, [petrol])
        oriented = _asking_bases(await _value(env))
        assert oriented
        for stored in oriented:
            assert stored.inputs.proceeds is not None
            assert stored.inputs.proceeds.evidence_ids == (str(petrol.id),)
            assert any(w.startswith("ORIENTATION_DISTANT") for w in stored.valuation.warnings)
    finally:
        await env.close()


async def test_relaxed_owner_estimate_orientation_is_the_last_fallback(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation estimates")
    try:
        base = _estimate("9300")
        estimate = base.model_copy(
            update={
                "vehicle": base.vehicle.model_copy(
                    update={"first_registration": PartialDate(value="2016", precision="year")}
                )
            }
        )
        await _record(env, [estimate])
        valued = await _value(env)
        loose = [
            v
            for v in valued
            if v.inputs.proceeds is not None
            and v.inputs.proceeds.basis == "owner_estimate"
            and v.inputs.proceeds.base is not None
        ]
        assert loose, [v.inputs.proceeds for v in valued]
        for stored in loose:
            assert stored.inputs.proceeds is not None
            assert stored.inputs.proceeds.evidence_ids == (str(estimate.id),)
            assert any(w.startswith("MK_ORIENTATION:owner_estimate") for w in stored.valuation.warnings)
            assert stored.valuation.expires_at is not None
            assert stored.valuation.research_candidate
    finally:
        await env.close()


async def test_stored_orientation_proceeds_never_reach_inquiry_cost_evidence(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation inquiries")
    try:
        await _record(env, [_row("9600", year=2014, days_ago=1), _row("9800", year=2014, days_ago=2)])
        oriented = _asking_bases(await _value(env))
        assert oriented
        for stored in oriented:
            costs = await run(
                env.ctx,
                env.system,
                lambda c, lid=stored.listing_id: inquiries_repo._costs(c, env.system, lid),
            )
            assert costs is not None and costs.best_case_proceeds is None
            assert not costs.disproves_opportunity()
    finally:
        await env.close()


async def test_orientation_fx_rate_is_a_dependency(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="MK orientation FX")
    try:
        # Year 2014: the strict set never selects it, only the orientation prices it in MKD.
        row = _row("9600", year=2014, days_ago=1).model_copy(update={"amount": Money.of("590400", "MKD")})
        await _record(env, [row])
        oriented = _asking_bases(await _value(env))  # seeds the EUR/MKD reference rate
        assert oriented
        for stored in oriented:
            assert stored.inputs.proceeds is not None
            assert stored.inputs.proceeds.evidence_ids == (str(row.id),)
            assert any(d.quote == "MKD" for d in stored.valuation.dependencies.fx)
    finally:
        await env.close()
