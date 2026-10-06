"""Property tests for domain.comparables (spec 15; spec 31 "Comparables").

All generated vehicles, prices and clusters are SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from suv_deals.domain.comparables import (
    ADEQUACY_KINDS,
    QUARTILE_MIN_SAMPLE,
    ComparableTarget,
    ExclusionReason,
    MarketObservation,
    quantile,
    select_comparables,
)
from suv_deals.domain.enums import Availability, Drive, EvidenceKind, Fuel, Gearbox, PriceBasis, Tristate
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import Money

AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
TARGET = ComparableTarget(
    make="Example",
    model="Trail",
    generation="G2",
    facelift=Tristate.NO,
    engine_displacement_cm3=1995,
    power_kw=103,
    fuel=Fuel.DIESEL,
    gearbox=Gearbox.MANUAL,
    drive=Drive.AWD,
    year=2011,
    year_source="first_registration",
    mileage_km=Decimal("187500"),
)


class Windows:
    def __init__(self, min_sample: int) -> None:
        self.comparable_year_window = 1
        self.comparable_mileage_window_km = Decimal("30000")
        self.comparable_max_age_days = 90
        self.comparable_min_sample = min_sample


_2WD = {Drive.FWD, Drive.RWD}


@st.composite
def observation(draw: Any, index: int) -> MarketObservation:
    year = draw(st.one_of(st.none(), st.integers(2007, 2015)))
    km = draw(st.one_of(st.none(), st.integers(100_000, 280_000)))
    amount = draw(st.one_of(st.none(), st.integers(0, 2_000_000).map(lambda c: Money.from_minor(c, "EUR"))))
    return MarketObservation(
        id=UUID(int=index + 1),
        source_key="fixture_mk_synthetic",
        url="https://mk-market.example/synthetic",
        observed_at=AS_OF - timedelta(hours=draw(st.integers(-48, 24 * 120))),
        evidence_kind=draw(st.sampled_from(list(EvidenceKind))),
        amount=amount,
        price_basis=draw(st.sampled_from(list(PriceBasis))),
        vehicle=VehicleSpec(
            make="Example",
            model=draw(st.sampled_from(["Trail", "Trail", "Trail", "Other"])),
            generation=draw(st.sampled_from(["G2", "G2", "G1", None])),
            facelift=draw(st.sampled_from(list(Tristate))),
            engine_displacement_cm3=draw(st.one_of(st.none(), st.integers(1500, 2600))),
            power_kw=draw(st.one_of(st.none(), st.integers(80, 140))),
            fuel=draw(st.sampled_from([Fuel.DIESEL, Fuel.DIESEL, Fuel.PETROL, Fuel.UNKNOWN])),
            gearbox=draw(
                st.sampled_from([Gearbox.MANUAL, Gearbox.MANUAL, Gearbox.AUTOMATIC, Gearbox.UNKNOWN])
            ),
            drive=draw(st.sampled_from([Drive.AWD, Drive.FOUR_WD, Drive.FWD, Drive.UNKNOWN])),
            first_registration=PartialDate(value=str(year), precision="year") if year else PartialDate(),
            mileage_km=None if km is None else Decimal(km),
        ),
        cluster_id=draw(st.one_of(st.none(), st.integers(1, 4).map(lambda n: UUID(int=10_000 + n)))),
        availability=draw(st.sampled_from(list(Availability))),
    )


@st.composite
def candidate_sets(draw: Any) -> list[MarketObservation]:
    size = draw(st.integers(0, 14))
    return [draw(observation(i)) for i in range(size)]


@settings(max_examples=150, deadline=None)
@given(cands=candidate_sets(), min_sample=st.integers(1, 6))
def test_partition_and_invariants(cands: list[MarketObservation], min_sample: int) -> None:
    result = select_comparables(TARGET, cands, Windows(min_sample), AS_OF)
    selected = {s.observation_id for s in result.selected}
    excluded = {e.observation_id for e in result.excluded}
    # every candidate exactly once
    assert selected.isdisjoint(excluded)
    assert selected | excluded == {c.id for c in cands}
    assert len(result.selected) + len(result.excluded) == len(cands)
    for item in result.excluded:
        assert item.reasons
        assert (item.duplicate_of is not None) == (ExclusionReason.DUPLICATE_OF_CLUSTER in item.reasons)
    for sel in result.selected:
        obs = sel.observation
        # hard boundaries are never crossed
        assert obs.evidence_kind != EvidenceKind.OWNER_ESTIMATE
        assert obs.vehicle.fuel == TARGET.fuel
        assert obs.vehicle.gearbox == TARGET.gearbox
        assert obs.vehicle.drive in {Drive.AWD, Drive.FOUR_WD}
        assert obs.vehicle.model == "Trail"
        assert obs.vehicle.generation in ("G2", None)
        assert obs.amount is not None and obs.amount.amount > 0
        assert AS_OF - timedelta(days=90) <= obs.observed_at <= AS_OF
        assert sel.weight > 0
        if sel.widened_dimensions:
            assert sel.match_level == "widened"
    # one kept observation per (cluster, kind)
    kept = [(s.observation.cluster_id, s.evidence_kind) for s in result.selected if s.observation.cluster_id]
    assert len(kept) == len(set(kept))


@settings(max_examples=150, deadline=None)
@given(cands=candidate_sets(), min_sample=st.integers(1, 6))
def test_stats_are_per_kind_and_ordered(cands: list[MarketObservation], min_sample: int) -> None:
    result = select_comparables(TARGET, cands, Windows(min_sample), AS_OF)
    for stats in result.stats:
        members = [s for s in result.selected if s.evidence_kind == stats.evidence_kind]
        assert stats.n == len(members) == len(stats.points)
        assert stats.min <= stats.median <= stats.max
        if stats.n >= QUARTILE_MIN_SAMPLE:
            assert stats.q1 is not None and stats.q3 is not None
            assert stats.min <= stats.q1 <= stats.median <= stats.q3 <= stats.max
        else:
            assert stats.q1 is None and stats.q3 is None
        assert stats.date_from <= stats.date_to
        assert sum(stats.match_quality.values()) == stats.n
    adequacy = max(sum(1 for s in result.selected if s.evidence_kind == k) for k in ADEQUACY_KINDS)
    if adequacy >= min_sample:
        assert result.status == "adequate" and not result.research_needed
    elif adequacy:
        assert result.status == "small_sample" and result.research_needed
    else:
        assert result.status == "insufficient_comparables" and result.research_needed
    if result.stats_for(EvidenceKind.ASKING_PRICE) is None:
        assert result.mk_band_fit == "unknown"
    # widening steps are recorded one dimension at a time, in the fixed order
    dims = [s.dimension for s in result.widening_steps]
    assert dims == ["mileage", "year", "facelift"][: len(dims)]
    assert [s.step for s in result.widening_steps] == list(range(1, len(dims) + 1))


@settings(max_examples=60, deadline=None)
@given(cands=candidate_sets(), seed=st.randoms(use_true_random=False))
def test_order_independent(cands: list[MarketObservation], seed: Any) -> None:
    shuffled = list(cands)
    seed.shuffle(shuffled)
    assert select_comparables(TARGET, cands, Windows(3), AS_OF) == select_comparables(
        TARGET, shuffled, Windows(3), AS_OF
    )


@settings(max_examples=200, deadline=None)
@given(
    values=st.lists(st.integers(1, 10_000_000), min_size=1, max_size=40),
    p1=st.integers(0, 100),
    p2=st.integers(0, 100),
)
def test_quantile_monotone_and_bounded(values: list[int], p1: int, p2: int) -> None:
    ordered = sorted(Decimal(v) for v in values)
    lo, hi = sorted((Decimal(p1) / 100, Decimal(p2) / 100))
    q_lo, q_hi = quantile(ordered, lo), quantile(ordered, hi)
    assert ordered[0] <= q_lo <= q_hi <= ordered[-1]
