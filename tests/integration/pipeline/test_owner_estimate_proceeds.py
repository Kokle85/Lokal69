"""The owner's MK estimates value a listing when no spec-15 MK asking price or sale matches it.

Owner decision 2026-10-16: the owner's own MK resale estimates (``owner_estimate``) value a
listing when no spec-15 MK asking price or sale matches it and no CLOSE MK asking-price
orientation exists (ADR 0005: a distant orientation yields to a strictly matched estimate),
labelled as the owner's assumption (never approved, research still needed); any matching MK
asking price wins. The listings are the SYNTHETIC fixture dealer pages, the estimates SYNTHETIC
rows of the fixture lineage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    PipelineEnv,
    build_env,
    by_slid,
    comparable,
    run,
    seed_comparables,
    seed_fx,
)

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.comparables import OWNER_ESTIMATE_LABEL, MarketObservation
from suv_deals.domain.enums import Confidence, CostLineStatus, EvidenceKind, JobState
from suv_deals.persistence import market_repo, valuation_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db


def _estimate(amount: str) -> MarketObservation:
    """A SYNTHETIC owner estimate for the fixture "Example Trail" (no ad URL: it is no ad)."""
    return comparable(amount).model_copy(
        update={
            "id": uuid.uuid4(),
            "evidence_kind": EvidenceKind.OWNER_ESTIMATE,
            "url": None,
            "source_key": "owner_estimate",
            "observed_at": datetime.now(UTC) - timedelta(days=1),
        }
    )


async def _record_estimate(env: PipelineEnv, amount: str) -> MarketObservation:
    estimate = _estimate(amount)

    async def insert(conn: Conn) -> None:
        await market_repo.insert_market_observation(conn, env.owner, estimate, confidence=Confidence.LOW)

    await run(env.ctx, env.owner, insert)
    return estimate


async def _value(env: PipelineEnv) -> list[valuation_repo.StoredValuation]:
    await seed_fx(env)
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    assert any(w.workspace_id == env.workspace_id and w.enqueued for w in tick.workspaces)
    reports = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-oe").run_until_idle()
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


async def test_owner_estimate_values_a_listing_without_mk_market_evidence(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="Owner estimate fallback")
    try:
        estimate = await _record_estimate(env, "9300")
        valued = await _value(env)
        from_estimate = [
            v for v in valued if v.inputs.proceeds and v.inputs.proceeds.basis == "owner_estimate"
        ]
        assert from_estimate, [v.inputs.proceeds.basis for v in valued if v.inputs.proceeds]
        for stored in from_estimate:
            proceeds = stored.inputs.proceeds
            assert proceeds is not None
            assert proceeds.evidence_kind == EvidenceKind.OWNER_ESTIMATE
            assert proceeds.evidence_ids == (str(estimate.id),)
            assert proceeds.base is not None and proceeds.base.amount == 9300
            assert proceeds.negotiation_discount_pct is None and not proceeds.assumption_approved
            assert proceeds.label() == OWNER_ESTIMATE_LABEL
            valuation = stored.valuation
            assert any(w.startswith("PROCEEDS_FROM_OWNER_ESTIMATE") for w in valuation.warnings)
            # The market set itself stays insufficient: the estimate is no MK comparable.
            assert any(w.startswith("INSUFFICIENT_COMPARABLES") for w in valuation.warnings)
            assert not valuation.alert_eligible
            view = stored.view()
            assert view.proceeds is not None and "owner's own estimate" in view.proceeds.notice
    finally:
        await env.close()


async def test_mk_asking_prices_win_over_the_owner_estimate(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="Owner estimate yields")
    try:
        await _record_estimate(env, "12000")
        await seed_comparables(env)
        valued = await _value(env)
        # Strict spec-15 sets only (a relaxed orientation for an unmatched listing has no set id).
        asking = [
            v
            for v in valued
            if v.inputs.proceeds is not None
            and v.inputs.proceeds.basis == "mk_asking_prices"
            and v.inputs.proceeds.status == CostLineStatus.ESTIMATED
            and v.inputs.proceeds.comparable_set_id is not None
        ]
        assert asking, [v.inputs.proceeds for v in valued]
        # Wherever an MK asking price matched, the (higher) estimate never replaced it.
        for stored in asking:
            proceeds = stored.inputs.proceeds
            assert proceeds is not None and proceeds.base is not None
            assert proceeds.base.amount < 12000
            assert proceeds.evidence_kind == EvidenceKind.ASKING_PRICE
            warnings = stored.valuation.warnings
            assert not any(w.startswith("PROCEEDS_FROM_OWNER_ESTIMATE") for w in warnings)
    finally:
        await env.close()


async def test_owner_estimate_freshness_bounds_the_valuation(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="Owner estimate freshness")
    try:
        estimate = _estimate("9300").model_copy(
            update={"observed_at": datetime.now(UTC) - timedelta(days=85)}
        )

        async def insert(conn: Conn) -> None:
            await market_repo.insert_market_observation(conn, env.owner, estimate, confidence=Confidence.LOW)

        await run(env.ctx, env.owner, insert)
        valued = await _value(env)
        from_estimate = [
            v for v in valued if v.inputs.proceeds is not None and v.inputs.proceeds.basis == "owner_estimate"
        ]
        assert from_estimate, [v.inputs.proceeds for v in valued]
        deadline = estimate.observed_at + timedelta(days=90)
        for stored in from_estimate:
            expires_at = stored.valuation.expires_at
            assert expires_at is not None and abs(expires_at - deadline) < timedelta(seconds=1)
            assert stored.valuation.research_candidate
    finally:
        await env.close()
