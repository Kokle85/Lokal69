"""Unit tests for domain.comparables (spec 15; spec 31 "Comparables" row).

Every vehicle, price, URL, cluster and FX rate here is SYNTHETIC test data ("Example Trail" is
not a real model), not real MK market evidence or a real exchange rate.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain import comparables as comparables_module
from suv_deals.domain.comparables import (
    CRITERIA_VERSION,
    DISCOUNT_UNKNOWN_LABEL,
    ORIENTATION_CLOSE,
    ORIENTATION_LABEL,
    ORIENTATION_LIMIT,
    ORIENTATION_VERSION,
    ComparableTarget,
    ConditionSummary,
    DifferenceSeverity,
    ExclusionReason,
    MarketObservation,
    MatchingCriteria,
    orient_comparables,
    orientation_warning,
    proceeds_from_comparables,
    proceeds_from_orientation,
    quantile,
    select_comparables,
)
from suv_deals.domain.costs import ProceedsEstimate, proceeds_supported
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    CostLineStatus,
    Drive,
    EvidenceKind,
    Fuel,
    FxPurpose,
    Gearbox,
    PriceBasis,
    SellerType,
    Tristate,
)
from suv_deals.domain.listings import NormalizedListing, PartialDate, VehicleSpec
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.domain.taxonomy import default_taxonomy
from suv_deals.errors import ValidationFailed

AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


class Windows:
    """Minimal BusinessConfig-like window object (synthetic)."""

    def __init__(self, year: int = 1, km: str = "30000", age: int = 90, min_sample: int = 3) -> None:
        self.comparable_year_window = year
        self.comparable_mileage_window_km = Decimal(km)
        self.comparable_max_age_days = age
        self.comparable_min_sample = min_sample


DEFAULT = Windows()
_counter = iter(range(1, 10_000_000))


def uid(n: int | None = None) -> UUID:
    return UUID(int=n if n is not None else next(_counter))


def target(**overrides: Any) -> ComparableTarget:
    data: dict[str, Any] = {
        "make": "Example",
        "model": "Trail",
        "generation": "G2",
        "facelift": Tristate.NO,
        "engine_code": None,
        "engine_displacement_cm3": 1995,
        "power_kw": 103,
        "fuel": Fuel.DIESEL,
        "gearbox": Gearbox.MANUAL,
        "drive": Drive.AWD,
        "year": 2011,
        "year_source": "first_registration",
        "mileage_km": Decimal("187500"),
    }
    data.update(overrides)
    return ComparableTarget(**data)


def obs(
    *,
    amount: str | None = "9000",
    currency: str = "EUR",
    kind: EvidenceKind = EvidenceKind.ASKING_PRICE,
    observed_at: datetime | None = None,
    cluster: UUID | None = None,
    oid: UUID | None = None,
    year: int | None = 2011,
    km: str | None = "180000",
    **vehicle: Any,
) -> MarketObservation:
    spec: dict[str, Any] = {
        "make": "Example",
        "model": "Trail",
        "generation": "G2",
        "facelift": Tristate.NO,
        "engine_displacement_cm3": 1995,
        "power_kw": 103,
        "fuel": Fuel.DIESEL,
        "gearbox": Gearbox.MANUAL,
        "drive": Drive.AWD,
        "first_registration": PartialDate(value=str(year), precision="year") if year else PartialDate(),
        "mileage_km": None if km is None else Decimal(km),
    }
    extra = {k: vehicle.pop(k) for k in list(vehicle) if k not in VehicleSpec.model_fields}
    spec.update(vehicle)
    oid = oid or uid()
    data: dict[str, Any] = {
        "id": oid,
        "source_key": "fixture_mk_synthetic",
        # One ad per observation (URL duplicates are tested with an explicit ``url=``).
        "url": f"https://mk-market.example/synthetic/listing?ad={oid.int}",
        "observed_at": observed_at or AS_OF - timedelta(days=3),
        "evidence_kind": kind,
        "amount": None if amount is None else Money.of(amount, currency),
        "price_basis": PriceBasis.GROSS,
        "vehicle": VehicleSpec(**spec),
        "local_registration_status": "locally_registered",
        "seller_type": SellerType.PRIVATE,
        "cluster_id": cluster,
        "availability": Availability.AVAILABLE,
        "condition": ConditionSummary(accident_free=ClaimStatus.SELLER_CLAIMED),
    }
    data.update(extra)
    return MarketObservation(**data)


def reasons_of(result: Any, oid: UUID) -> tuple[ExclusionReason, ...]:
    for item in result.excluded:
        if item.observation_id == oid:
            return tuple(item.reasons)
    raise AssertionError(f"{oid} not excluded")


def selected_ids(result: Any) -> set[UUID]:
    return {s.observation_id for s in result.selected}


# --------------------------------------------------------------------------------------------- basics


def test_exact_match_selected_with_full_weight() -> None:
    o = obs()
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    assert result.criteria_version == CRITERIA_VERSION
    assert selected_ids(result) == {o.id}
    sel = result.selected[0]
    assert sel.match_level == "exact"
    assert sel.weight == Decimal("1.000000")
    assert sel.amount_eur == Decimal("9000.000000")
    assert sel.widened_dimensions == ()
    assert result.status == "adequate"
    assert result.research_needed is False
    assert result.sample_quality == "adequate"


def test_business_config_satisfies_window_protocol() -> None:
    config: BusinessConfig = load_business_config(Path(__file__).resolve().parents[2] / "config")
    result = select_comparables(target(), [obs(), obs(), obs()], config, AS_OF)
    assert result.criteria.year_window == 1
    assert result.criteria.mileage_window_km == Decimal("30000")
    assert result.status == "adequate"


def test_every_candidate_is_selected_or_excluded_exactly_once() -> None:
    cands = [obs(), obs(fuel=Fuel.PETROL), obs(amount=None), obs(model="Other")]
    result = select_comparables(target(), cands, DEFAULT, AS_OF)
    seen = [s.observation_id for s in result.selected] + [e.observation_id for e in result.excluded]
    assert sorted(seen, key=str) == sorted((c.id for c in cands), key=str)


def test_duplicate_candidate_ids_rejected() -> None:
    o = obs()
    with pytest.raises(ValidationFailed):
        select_comparables(target(), [o, o], DEFAULT, AS_OF)


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValidationFailed):
        select_comparables(target(), [], DEFAULT, datetime(2026, 10, 6, 10, 0))


def test_too_many_candidates_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comparables_module, "MAX_CANDIDATES", 2)
    with pytest.raises(ValidationFailed):
        select_comparables(target(), [obs(), obs(), obs()], DEFAULT, AS_OF)


def test_result_is_independent_of_candidate_order() -> None:
    cands = [obs(amount=str(8000 + i * 250), km=str(170000 + i * 9000)) for i in range(8)]
    cands += [obs(fuel=Fuel.PETROL), obs(amount=None), obs(cluster=uid(77)), obs(cluster=uid(77))]
    first = select_comparables(target(), cands, DEFAULT, AS_OF)
    shuffled = list(cands)
    random.Random(4).shuffle(shuffled)
    second = select_comparables(target(), shuffled, DEFAULT, AS_OF)
    assert first == second


# --------------------------------------------------------------------------------------------- identity


def test_wrong_make_or_model_excluded() -> None:
    wrong_model = obs(model="Trail Sport")
    wrong_make = obs(make="Sample")
    result = select_comparables(target(), [wrong_model, wrong_make], DEFAULT, AS_OF)
    assert reasons_of(result, wrong_model.id) == (ExclusionReason.WRONG_MODEL,)
    assert reasons_of(result, wrong_make.id) == (ExclusionReason.WRONG_MODEL,)
    assert result.status == "insufficient_comparables"
    details = next(e.details for e in result.excluded if e.observation_id == wrong_model.id)
    assert any("Trail Sport" in d for d in details)


def test_model_match_is_case_accent_and_punctuation_insensitive() -> None:
    o = obs(make="EXAMPLÉ", model="trail")
    t = target(make="Example", model="Trail")
    result = select_comparables(t, [o], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {o.id}
    o2 = obs(model="X-Trail")
    result2 = select_comparables(target(model="X Trail"), [o2], Windows(min_sample=1), AS_OF)
    assert selected_ids(result2) == {o2.id}


def test_missing_model_on_either_side_excluded() -> None:
    no_model = obs(model=None)
    result = select_comparables(target(), [no_model], DEFAULT, AS_OF)
    assert reasons_of(result, no_model.id) == (ExclusionReason.MODEL_UNKNOWN,)
    result2 = select_comparables(target(model=None), [obs()], DEFAULT, AS_OF)
    assert result2.excluded[0].reasons == (ExclusionReason.MODEL_UNKNOWN,)
    assert any(w.startswith("TARGET_MODEL_UNKNOWN") for w in result2.warnings)


def test_wrong_generation_excluded_when_both_known() -> None:
    g1 = obs(generation="G1")
    result = select_comparables(target(), [g1], DEFAULT, AS_OF)
    assert reasons_of(result, g1.id) == (ExclusionReason.WRONG_GENERATION,)


def test_unknown_target_generation_labels_generation_unverified() -> None:
    cands = [obs(), obs(generation="G1"), obs(generation=None)]
    result = select_comparables(target(generation=None), cands, DEFAULT, AS_OF)
    assert len(result.selected) == 3  # generation cannot be compared, so it is not an exclusion
    for sel in result.selected:
        codes = {d.code for d in sel.differences}
        assert "generation_unverified" in codes
        assert sel.match_level == "close"
        assert sel.weight == Decimal("0.750000")
    assert any(w.startswith("TARGET_GENERATION_UNKNOWN") for w in result.warnings)


def test_unknown_candidate_generation_labelled() -> None:
    o = obs(generation=None)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    assert "generation_unverified" in {d.code for d in result.selected[0].differences}


# --------------------------------------------------------------------------------------------- engine


@pytest.mark.parametrize(
    ("displacement", "selected"),
    [(1995, True), (2194, True), (1796, True), (2195, False), (1795, False), (2993, False)],
)
def test_displacement_within_ten_percent(displacement: int, selected: bool) -> None:
    # 10 % of 1995 cm3 = 199.5 cm3 -> [1795.5, 2194.5]
    o = obs(engine_displacement_cm3=displacement)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    if selected:
        assert selected_ids(result) == {o.id}
    else:
        assert reasons_of(result, o.id) == (ExclusionReason.ENGINE_MISMATCH,)


@pytest.mark.parametrize(("power", "selected"), [(118, True), (88, True), (119, False), (87, False)])
def test_power_within_fifteen_percent(power: int, selected: bool) -> None:
    # 15 % of 103 kW = 15.45 kW -> [87.55, 118.45]
    o = obs(power_kw=power)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    assert (o.id in selected_ids(result)) is selected
    if not selected:
        assert reasons_of(result, o.id) == (ExclusionReason.ENGINE_MISMATCH,)


def test_engine_within_tolerance_but_different_is_labelled_close() -> None:
    o = obs(engine_displacement_cm3=1998, power_kw=110)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    sel = result.selected[0]
    codes = {d.code for d in sel.differences}
    assert {"displacement_differs_within_tolerance", "power_differs_within_tolerance"} <= codes
    assert sel.match_level == "close"


def test_unknown_engine_values_labelled_not_excluded() -> None:
    o = obs(engine_displacement_cm3=None, power_kw=None)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    codes = {d.code for d in result.selected[0].differences}
    assert {"displacement_unverified", "power_unverified", "engine_code_unverified"} <= codes


def test_engine_code_mismatch_excluded_and_prefix_variant_allowed() -> None:
    t = target(engine_code="N47D20")
    other = obs(engine_code="M57D30")
    variant = obs(engine_code="N47")
    same = obs(engine_code="n47-d20")
    result = select_comparables(t, [other, variant, same], Windows(min_sample=1), AS_OF)
    assert reasons_of(result, other.id) == (ExclusionReason.ENGINE_MISMATCH,)
    assert {variant.id, same.id} == selected_ids(result)
    variant_sel = next(s for s in result.selected if s.observation_id == variant.id)
    assert "engine_code_variant" in {d.code for d in variant_sel.differences}
    same_sel = next(s for s in result.selected if s.observation_id == same.id)
    assert "engine_code_variant" not in {d.code for d in same_sel.differences}


# --------------------------------------------------------------------------------------------- powertrain


def test_fuel_gearbox_drive_mismatches_excluded() -> None:
    petrol = obs(fuel=Fuel.PETROL)
    auto = obs(gearbox=Gearbox.AUTOMATIC)
    semi = obs(gearbox=Gearbox.SEMI_AUTOMATIC)
    fwd = obs(drive=Drive.FWD)
    rwd = obs(drive=Drive.RWD)
    result = select_comparables(target(), [petrol, auto, semi, fwd, rwd], DEFAULT, AS_OF)
    assert reasons_of(result, petrol.id) == (ExclusionReason.FUEL_MISMATCH,)
    assert reasons_of(result, auto.id) == (ExclusionReason.GEARBOX_MISMATCH,)
    assert reasons_of(result, semi.id) == (ExclusionReason.GEARBOX_MISMATCH,)
    assert reasons_of(result, fwd.id) == (ExclusionReason.DRIVE_MISMATCH,)
    assert reasons_of(result, rwd.id) == (ExclusionReason.DRIVE_MISMATCH,)
    assert result.selected == ()


def test_diesel_automatic_awd_never_valued_from_petrol_manual_2wd() -> None:
    t = target(fuel=Fuel.DIESEL, gearbox=Gearbox.AUTOMATIC, drive=Drive.AWD)
    wrong = [obs(fuel=Fuel.PETROL, gearbox=Gearbox.MANUAL, drive=Drive.FWD) for _ in range(10)]
    result = select_comparables(t, wrong, DEFAULT, AS_OF)
    assert result.selected == ()
    assert result.status == "insufficient_comparables"
    assert result.research_needed is True
    for item in result.excluded:
        assert set(item.reasons) == {
            ExclusionReason.FUEL_MISMATCH,
            ExclusionReason.GEARBOX_MISMATCH,
            ExclusionReason.DRIVE_MISMATCH,
        }
        # reasons are reported in spec 15 matching order
        assert list(item.reasons) == sorted(item.reasons, key=list(ExclusionReason).index)


def test_awd_and_4wd_are_one_class_with_labelled_difference() -> None:
    o = obs(drive=Drive.FOUR_WD)
    result = select_comparables(target(drive=Drive.AWD), [o], Windows(min_sample=1), AS_OF)
    sel = result.selected[0]
    assert "drive_variant_differs" in {d.code for d in sel.differences}
    assert sel.match_level == "close"


def test_unknown_core_specs_are_excluded_as_unverified() -> None:
    no_fuel = obs(fuel=Fuel.UNKNOWN)
    no_box = obs(gearbox=Gearbox.UNKNOWN)
    no_drive = obs(drive=Drive.UNKNOWN)
    result = select_comparables(target(), [no_fuel, no_box, no_drive], DEFAULT, AS_OF)
    assert reasons_of(result, no_fuel.id) == (ExclusionReason.FUEL_UNVERIFIED,)
    assert reasons_of(result, no_box.id) == (ExclusionReason.GEARBOX_UNVERIFIED,)
    assert reasons_of(result, no_drive.id) == (ExclusionReason.DRIVE_UNVERIFIED,)


def test_unknown_target_drive_excludes_all_and_warns() -> None:
    result = select_comparables(target(drive=Drive.UNKNOWN), [obs(), obs()], DEFAULT, AS_OF)
    assert result.selected == ()
    assert all(e.reasons == (ExclusionReason.DRIVE_UNVERIFIED,) for e in result.excluded)
    assert any(w.startswith("TARGET_DRIVE_UNKNOWN") for w in result.warnings)


def test_gearbox_subtype_difference_labelled() -> None:
    o = obs(gearbox_subtype="6-speed")
    result = select_comparables(target(gearbox_subtype="5-speed"), [o], Windows(min_sample=1), AS_OF)
    assert "gearbox_subtype_differs" in {d.code for d in result.selected[0].differences}


# --------------------------------------------------------------------------------------------- windows


def test_tight_window_boundaries_inclusive() -> None:
    edge_km_low = obs(km="157500")  # exactly -30,000 km
    edge_km_high = obs(km="217500")  # exactly +30,000 km
    edge_year = obs(year=2012)  # exactly +1 year
    result = select_comparables(target(), [edge_km_low, edge_km_high, edge_year], DEFAULT, AS_OF)
    assert selected_ids(result) == {edge_km_low.id, edge_km_high.id, edge_year.id}
    assert result.widening_steps == ()
    for sel in result.selected:
        assert sel.match_level == "exact"  # inside the tight window is an exact match (info deltas only)
        assert all(
            d.severity in (DifferenceSeverity.INFO, DifferenceSeverity.CONTEXT) for d in sel.differences
        )


def test_no_widening_when_tight_sample_is_adequate() -> None:
    tight = [obs(), obs(), obs()]
    far_km = obs(km="235000")  # needs the widened mileage window
    result = select_comparables(target(), [*tight, far_km], DEFAULT, AS_OF)
    assert result.widening_steps == ()
    assert reasons_of(result, far_km.id) == (ExclusionReason.MILEAGE_OUT_OF_WINDOW,)
    assert result.status == "adequate"


def test_widening_one_dimension_at_a_time_with_labels() -> None:
    tight = obs(amount="8800")
    km_only = obs(km="232000", amount="9100")  # +44,500 km -> mileage step
    year_only = obs(year=2013, amount="9400")  # +2 years -> year step
    result = select_comparables(target(), [tight, km_only, year_only], DEFAULT, AS_OF)
    assert [s.dimension for s in result.widening_steps] == ["mileage", "year"]
    assert result.widening_steps[0].step == 1
    assert result.widening_steps[0].adequacy_count_after == 2
    assert result.widening_steps[1].adequacy_count_after == 3
    assert "30000" in result.widening_steps[0].label and "60000" in result.widening_steps[0].label
    by_id = {s.observation_id: s for s in result.selected}
    assert by_id[tight.id].widened_dimensions == ()
    assert by_id[km_only.id].widened_dimensions == ("mileage",)
    assert by_id[km_only.id].match_level == "widened"
    assert by_id[km_only.id].weight == Decimal("0.500000")
    assert by_id[year_only.id].widened_dimensions == ("year",)
    assert result.status == "adequate"


def test_widening_stops_once_minimum_reached() -> None:
    tight = [obs(), obs()]
    km_only = obs(km="232000")
    year_only = obs(year=2013)
    result = select_comparables(target(), [*tight, km_only, year_only], DEFAULT, AS_OF)
    assert [s.dimension for s in result.widening_steps] == ["mileage"]
    assert km_only.id in selected_ids(result)
    assert reasons_of(result, year_only.id) == (ExclusionReason.YEAR_OUT_OF_WINDOW,)


def test_facelift_is_last_widening_step() -> None:
    other_facelift = obs(facelift=Tristate.YES)
    result = select_comparables(target(facelift=Tristate.NO), [other_facelift], DEFAULT, AS_OF)
    assert [s.dimension for s in result.widening_steps] == ["mileage", "year", "facelift"]
    sel = result.selected[0]
    assert sel.widened_dimensions == ("facelift",)
    assert result.status == "small_sample"


def test_facelift_unknown_is_labelled_not_widened() -> None:
    o = obs(facelift=Tristate.UNKNOWN)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    assert "facelift_unverified" in {d.code for d in result.selected[0].differences}
    assert result.widening_steps == ()


def test_beyond_widest_window_always_excluded() -> None:
    far_km = obs(km="250000")  # +62,500 km > 60,000
    far_year = obs(year=2014)  # +3 years > 2
    result = select_comparables(target(), [far_km, far_year], DEFAULT, AS_OF)
    assert reasons_of(result, far_km.id) == (ExclusionReason.MILEAGE_OUT_OF_WINDOW,)
    assert reasons_of(result, far_year.id) == (ExclusionReason.YEAR_OUT_OF_WINDOW,)
    assert result.widening_steps == ()  # nothing could be admitted; no empty widening recorded


def test_candidate_needing_two_widened_dimensions() -> None:
    both = obs(km="232000", year=2009)
    result = select_comparables(target(), [both], DEFAULT, AS_OF)
    assert result.selected[0].widened_dimensions == ("mileage", "year")


def test_unknown_candidate_year_and_mileage_excluded() -> None:
    no_year = obs(year=None)
    no_km = obs(km=None)
    result = select_comparables(target(), [no_year, no_km], DEFAULT, AS_OF)
    assert reasons_of(result, no_year.id) == (ExclusionReason.YEAR_UNKNOWN,)
    assert reasons_of(result, no_km.id) == (ExclusionReason.MILEAGE_UNKNOWN,)


def test_unknown_target_year_and_mileage_labelled() -> None:
    o = obs()
    result = select_comparables(target(year=None, mileage_km=None), [o], Windows(min_sample=1), AS_OF)
    codes = {d.code for d in result.selected[0].differences}
    assert {"target_year_unknown", "target_mileage_unknown"} <= codes
    assert any(w.startswith("TARGET_YEAR_UNKNOWN") for w in result.warnings)
    assert any(w.startswith("TARGET_MILEAGE_UNKNOWN") for w in result.warnings)


def test_model_year_fallback_is_labelled() -> None:
    o = obs(year=None, model_year=2011)
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    assert "year_basis_differs" in {d.code for d in result.selected[0].differences}


# --------------------------------------------------------------------------------------------- staleness


def test_stale_and_future_evidence_excluded() -> None:
    stale = obs(observed_at=AS_OF - timedelta(days=90, seconds=1))
    boundary = obs(observed_at=AS_OF - timedelta(days=90))
    future = obs(observed_at=AS_OF + timedelta(seconds=1))
    result = select_comparables(target(), [stale, boundary, future], DEFAULT, AS_OF)
    assert reasons_of(result, stale.id) == (ExclusionReason.STALE,)
    assert reasons_of(result, future.id) == (ExclusionReason.OBSERVED_AFTER_AS_OF,)
    assert selected_ids(result) == {boundary.id}
    stale_details = next(e.details for e in result.excluded if e.observation_id == stale.id)
    assert any("retained historically" in d for d in stale_details)


def test_only_stale_sample_is_insufficient() -> None:
    old = [obs(observed_at=AS_OF - timedelta(days=200)) for _ in range(5)]
    result = select_comparables(target(), old, DEFAULT, AS_OF)
    assert result.status == "insufficient_comparables"
    assert result.research_needed is True
    assert result.mk_band_fit == "unknown"


# --------------------------------------------------------------------------------------------- duplicates


def test_cluster_duplicates_keep_newest() -> None:
    cluster = uid(500)
    older = obs(cluster=cluster, observed_at=AS_OF - timedelta(days=10), amount="9500")
    newer = obs(cluster=cluster, observed_at=AS_OF - timedelta(days=1), amount="9200")
    other = obs()
    result = select_comparables(target(), [older, newer, other], DEFAULT, AS_OF)
    assert selected_ids(result) == {newer.id, other.id}
    dup = next(e for e in result.excluded if e.observation_id == older.id)
    assert dup.reasons == (ExclusionReason.DUPLICATE_OF_CLUSTER,)
    assert dup.duplicate_of == newer.id
    assert result.stats_for(EvidenceKind.ASKING_PRICE).n == 2  # type: ignore[union-attr]


def test_cluster_duplicates_tie_break_by_id() -> None:
    cluster = uid(501)
    a = obs(cluster=cluster, oid=uid(9001))
    b = obs(cluster=cluster, oid=uid(9002))
    result = select_comparables(target(), [b, a], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {a.id}
    assert reasons_of(result, b.id) == (ExclusionReason.DUPLICATE_OF_CLUSTER,)


def test_cluster_dedupe_is_per_evidence_kind() -> None:
    cluster = uid(502)
    asking = obs(cluster=cluster)
    sale = obs(cluster=cluster, kind=EvidenceKind.VERIFIED_SALE, amount="8600")
    result = select_comparables(target(), [asking, sale], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {asking.id, sale.id}


def test_invalid_newest_does_not_hide_valid_cluster_member() -> None:
    cluster = uid(503)
    valid_old = obs(cluster=cluster, observed_at=AS_OF - timedelta(days=5))
    newest_no_price = obs(cluster=cluster, observed_at=AS_OF - timedelta(days=1), amount=None)
    result = select_comparables(target(), [valid_old, newest_no_price], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {valid_old.id}
    assert reasons_of(result, newest_no_price.id) == (ExclusionReason.PRICE_MISSING,)


AD = "https://www.reklama5.example/AdDetails?ad=4711"  # SYNTHETIC ad URL shape (query = ad id)


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.reklama5.example/AdDetails?ad=4711", "reklama5.example/AdDetails?ad=4711"),
        ("http://reklama5.example/AdDetails?ad=4711#photos", "reklama5.example/AdDetails?ad=4711"),
        ("https://WWW.Reklama5.Example:443/AdDetails/?ad=4711", "reklama5.example/AdDetails?ad=4711"),
        ("https://user:secret@pazar3.example/ad/77/", "pazar3.example/ad/77"),
        ("https://pazar3.example/", "pazar3.example"),
        ("https://pazar3.example/ad/77?", "pazar3.example/ad/77"),
        (None, None),
        ("https:///no-host", None),
        ("https://[::1", None),  # malformed IPv6 host: no key, never an error
    ],
)
def test_ad_url_key_normalises_only_what_names_the_same_ad(url: str | None, key: str | None) -> None:
    assert comparables_module.ad_url_key(url) == key


def test_ad_url_key_keeps_the_query_and_the_path_case() -> None:
    key = comparables_module.ad_url_key
    assert key("https://reklama5.example/AdDetails?ad=1") != key("https://reklama5.example/AdDetails?ad=2")
    assert key("https://pazar3.example/Ad/1") != key("https://pazar3.example/ad/1")


def test_same_ad_url_recorded_daily_counts_once_and_keeps_the_newest() -> None:
    days = [obs(url=AD, observed_at=AS_OF - timedelta(days=d), amount=str(9000 + d * 10)) for d in (5, 3, 1)]
    other = obs()
    result = select_comparables(target(), [*days, other], DEFAULT, AS_OF)
    newest = days[-1]
    assert selected_ids(result) == {newest.id, other.id}
    for older in days[:-1]:
        dup = next(e for e in result.excluded if e.observation_id == older.id)
        assert dup.reasons == (ExclusionReason.DUPLICATE_OF_URL,)
        assert dup.duplicate_of == newest.id
        assert any(d.startswith("DUPLICATE_OF_URL: same ad url re-recorded") for d in dup.details)
        assert not any(AD in d for d in dup.details)  # the URL is never echoed
    assert result.stats_for(EvidenceKind.ASKING_PRICE).n == 2  # type: ignore[union-attr]


def test_url_variants_of_one_ad_are_duplicates_but_other_ads_are_not() -> None:
    a = obs(url="https://reklama5.example/AdDetails?ad=4711", observed_at=AS_OF - timedelta(days=2))
    b = obs(url="http://www.reklama5.example/AdDetails/?ad=4711#top", observed_at=AS_OF - timedelta(days=1))
    c = obs(url="https://reklama5.example/AdDetails?ad=4712", observed_at=AS_OF - timedelta(days=2))
    result = select_comparables(target(), [a, b, c], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {b.id, c.id}
    assert reasons_of(result, a.id) == (ExclusionReason.DUPLICATE_OF_URL,)


def test_url_duplicates_tie_break_by_id() -> None:
    a = obs(url=AD, oid=uid(9101))
    b = obs(url=AD, oid=uid(9102))
    result = select_comparables(target(), [b, a], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {a.id}
    assert reasons_of(result, b.id) == (ExclusionReason.DUPLICATE_OF_URL,)


def test_url_dedupe_is_per_evidence_kind() -> None:
    asking = obs(url=AD)
    sale = obs(url=AD, kind=EvidenceKind.VERIFIED_SALE, amount="8600")
    result = select_comparables(target(), [asking, sale], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {asking.id, sale.id}


def test_newer_url_record_without_price_does_not_hide_the_valid_one() -> None:
    valid_old = obs(url=AD, observed_at=AS_OF - timedelta(days=5))
    newest_no_price = obs(url=AD, observed_at=AS_OF - timedelta(days=1), amount=None)
    result = select_comparables(target(), [valid_old, newest_no_price], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {valid_old.id}
    assert reasons_of(result, newest_no_price.id) == (ExclusionReason.PRICE_MISSING,)


def test_newer_re_record_failing_a_hard_check_never_hides_the_complete_one() -> None:
    # ADR 0005 decision 6: the newest row that passes the hard checks is kept, not just the newest.
    old_full = obs(url=AD, observed_at=AS_OF - timedelta(days=2))
    new_gap = obs(url=AD, observed_at=AS_OF - timedelta(days=1), km=None)  # lost its mileage
    new_far = obs(url=AD, observed_at=AS_OF - timedelta(hours=1), year=2016)  # out of every window
    result = select_comparables(target(), [old_full, new_gap, new_far], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {old_full.id}
    assert set(reasons_of(result, new_gap.id)) == {
        ExclusionReason.MILEAGE_UNKNOWN,
        ExclusionReason.DUPLICATE_OF_URL,
    }
    assert set(reasons_of(result, new_far.id)) == {
        ExclusionReason.YEAR_OUT_OF_WINDOW,
        ExclusionReason.DUPLICATE_OF_URL,
    }
    assert {e.duplicate_of for e in result.excluded} == {old_full.id}
    assert any(
        d.startswith(f"DUPLICATE_OF_URL: same ad url re-recorded; kept {old_full.id}")
        for e in result.excluded
        for d in e.details
    )


def test_without_an_eligible_re_record_the_newest_is_kept() -> None:
    older = obs(url=AD, observed_at=AS_OF - timedelta(days=2), km=None)
    newer = obs(url=AD, observed_at=AS_OF - timedelta(days=1), km=None)
    result = select_comparables(target(), [older, newer], Windows(min_sample=1), AS_OF)
    assert result.selected == ()
    assert reasons_of(result, newer.id) == (ExclusionReason.MILEAGE_UNKNOWN,)
    assert set(reasons_of(result, older.id)) == {
        ExclusionReason.MILEAGE_UNKNOWN,
        ExclusionReason.DUPLICATE_OF_URL,
    }


def test_owner_estimates_without_url_or_cluster_are_never_deduplicated() -> None:
    # ADR 0005 decision 5 (open point): a re-import with revised values is counted together with
    # the old rows until they turn stale.
    old = obs(
        kind=EvidenceKind.OWNER_ESTIMATE, url=None, amount="9000", observed_at=AS_OF - timedelta(days=80)
    )
    new = obs(
        kind=EvidenceKind.OWNER_ESTIMATE, url=None, amount="8000", observed_at=AS_OF - timedelta(days=1)
    )
    strict = select_comparables(target(), [old, new], DEFAULT, AS_OF, owner_estimates=True)
    assert selected_ids(strict) == {old.id, new.id}
    oriented = orient_comparables(target(), [old, new], DEFAULT, AS_OF, kind=EvidenceKind.OWNER_ESTIMATE)
    assert {p.observation_id for p in oriented.points} == {old.id, new.id}
    assert proceeds_from_orientation(oriented).base == Money.of("8500.00", "EUR")


def test_observations_without_url_are_never_url_duplicates() -> None:
    a, b = obs(url=None), obs(url=None)
    result = select_comparables(target(), [a, b], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {a.id, b.id}


def test_cluster_and_url_links_are_transitive() -> None:
    cluster = uid(504)
    a = obs(url=AD, cluster=cluster, observed_at=AS_OF - timedelta(days=4))
    b = obs(url=AD, observed_at=AS_OF - timedelta(days=3))
    c = obs(cluster=cluster, observed_at=AS_OF - timedelta(days=1))
    result = select_comparables(target(), [a, b, c], Windows(min_sample=1), AS_OF)
    assert selected_ids(result) == {c.id}
    assert reasons_of(result, a.id) == (
        ExclusionReason.DUPLICATE_OF_CLUSTER,
        ExclusionReason.DUPLICATE_OF_URL,
    )
    assert reasons_of(result, b.id) == (ExclusionReason.DUPLICATE_OF_URL,)
    assert {e.duplicate_of for e in result.excluded} == {c.id}
    shuffled = select_comparables(target(), [c, b, a], Windows(min_sample=1), AS_OF)
    assert shuffled == result


# --------------------------------------------------------------------------------------------- evidence kinds


def test_asking_and_sale_evidence_never_mixed() -> None:
    asking = [obs(amount=a) for a in ("8000", "9000", "10000")]
    sold_badge = obs(amount="9900", availability=Availability.SOLD_CLAIMED)
    removed = obs(amount="9700", availability=Availability.REMOVED)
    seller_sale = obs(kind=EvidenceKind.SELLER_REPORTED_SALE, amount="7500")
    verified = obs(kind=EvidenceKind.VERIFIED_SALE, amount="7800")
    estimate = obs(kind=EvidenceKind.OWNER_ESTIMATE, amount="9500")
    cands = [*asking, sold_badge, removed, seller_sale, verified, estimate]
    result = select_comparables(target(), cands, DEFAULT, AS_OF)

    ask_stats = result.stats_for(EvidenceKind.ASKING_PRICE)
    assert ask_stats is not None
    assert ask_stats.n == 5  # removed and sold-claimed listings stay asking-price evidence
    assert ask_stats.evidence_note.startswith("advertised asking prices")
    sold_sel = next(s for s in result.selected if s.observation_id == sold_badge.id)
    assert sold_sel.evidence_kind == EvidenceKind.ASKING_PRICE
    assert "sold_claimed_asking_only" in {d.code for d in sold_sel.differences}
    removed_sel = next(s for s in result.selected if s.observation_id == removed.id)
    assert "listing_removed_asking_only" in {d.code for d in removed_sel.differences}

    seller_stats = result.stats_for(EvidenceKind.SELLER_REPORTED_SALE)
    verified_stats = result.stats_for(EvidenceKind.VERIFIED_SALE)
    assert seller_stats is not None and seller_stats.n == 1
    assert verified_stats is not None and verified_stats.n == 1
    assert seller_stats.median == Decimal("7500.00")
    assert verified_stats.median == Decimal("7800.00")
    assert ask_stats.min == Decimal("8000.00")  # no sale amount leaked into asking stats
    assert reasons_of(result, estimate.id) == (ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND,)
    assert result.stats_for(EvidenceKind.OWNER_ESTIMATE) is None
    assert any(w.startswith("SELLER_REPORTED_SALES_UNVERIFIED") for w in result.warnings)


def test_seller_reported_sales_never_make_a_set_adequate() -> None:
    claims = [obs(kind=EvidenceKind.SELLER_REPORTED_SALE, amount="8000") for _ in range(6)]
    result = select_comparables(target(), claims, DEFAULT, AS_OF)
    assert len(result.selected) == 6
    assert result.status == "insufficient_comparables"
    assert result.adequacy_count == 0
    assert result.research_needed is True


def test_seller_reported_sale_without_amount_excluded() -> None:
    claim = obs(kind=EvidenceKind.SELLER_REPORTED_SALE, amount=None)
    result = select_comparables(target(), [claim], DEFAULT, AS_OF)
    assert reasons_of(result, claim.id) == (ExclusionReason.PRICE_MISSING,)


def test_zero_price_is_unknown_not_zero() -> None:
    zero = obs(amount="0")
    result = select_comparables(target(), [zero], DEFAULT, AS_OF)
    assert reasons_of(result, zero.id) == (ExclusionReason.PRICE_MISSING,)


def test_negative_amount_rejected_by_model() -> None:
    with pytest.raises(ValueError):
        obs(amount="-1")


# --------------------------------------------------------------------------------------------- FX


def _mkd_rate(rate_date: date = date(2026, 10, 5)) -> FxRate:
    # SYNTHETIC rate for tests: 1 EUR = 61.5 MKD.
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.5"),
        rate_date=rate_date,
        retrieved_at=AS_OF,
        provider="synthetic-test",
    )


def test_mkd_converted_with_explicit_direction() -> None:
    o = obs(amount="553500", currency="MKD")
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF, [_mkd_rate()])
    assert result.selected[0].amount_eur == Decimal("9000.000000")
    assert result.selected[0].observation.amount == Money.of("553500", "MKD")  # original kept


def test_unconvertible_currency_excluded() -> None:
    o = obs(amount="553500", currency="MKD")
    result = select_comparables(target(), [o], DEFAULT, AS_OF)
    assert reasons_of(result, o.id) == (ExclusionReason.CURRENCY_UNCONVERTIBLE,)


# --------------------------------------------------------------------------------------------- other filters


def test_wrong_market_and_fixture_mismatch_excluded() -> None:
    de = obs(market="DE")
    fixture = obs(is_fixture=True)
    result = select_comparables(target(), [de, fixture], DEFAULT, AS_OF)
    assert reasons_of(result, de.id) == (ExclusionReason.WRONG_MARKET,)
    assert reasons_of(result, fixture.id) == (ExclusionReason.FIXTURE_MISMATCH,)
    fixture_target = target(is_fixture=True)
    result2 = select_comparables(fixture_target, [obs(), obs(is_fixture=True)], Windows(min_sample=1), AS_OF)
    assert len(result2.selected) == 1 and result2.selected[0].observation.is_fixture


def test_damaged_or_non_running_comparable_excluded() -> None:
    damaged = obs(condition=ConditionSummary(damaged_vehicle=ClaimStatus.SELLER_CLAIMED))
    non_running = obs(condition=ConditionSummary(running=ClaimStatus.SELLER_DENIED))
    result = select_comparables(target(), [damaged, non_running], DEFAULT, AS_OF)
    assert reasons_of(result, damaged.id) == (ExclusionReason.CONDITION_MISMATCH,)
    assert reasons_of(result, non_running.id) == (ExclusionReason.CONDITION_MISMATCH,)


def test_context_differences_listed_without_lowering_match_level() -> None:
    o = obs(
        local_registration_status="imported_unregistered",
        seller_type=SellerType.DEALER,
        warranty=Tristate.YES,
        price_basis=PriceBasis.UNKNOWN,
    )
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF)
    sel = result.selected[0]
    codes = {d.code for d in sel.differences}
    assert {
        "registration_imported_unregistered",
        "dealer_seller",
        "warranty_included",
        "price_basis_unknown",
    } <= codes
    assert sel.match_level == "exact"


def test_registered_mk_alias_normalised() -> None:
    o = obs(local_registration_status="registered_mk")
    assert o.local_registration_status == "locally_registered"


def test_observation_url_must_be_http() -> None:
    with pytest.raises(ValueError):
        MarketObservation(
            id=uid(),
            source_key="fixture_mk_synthetic",
            url="javascript:alert(1)",
            observed_at=AS_OF,
            evidence_kind=EvidenceKind.ASKING_PRICE,
            amount=Money.of("9000", "EUR"),
        )


# --------------------------------------------------------------------------------------------- statistics


def test_sparse_samples_labelled() -> None:
    empty = select_comparables(target(), [], DEFAULT, AS_OF)
    assert empty.status == "insufficient_comparables"
    assert empty.research_needed is True
    assert empty.stats == ()
    assert empty.mk_band_fit == "unknown"
    assert empty.sample_quality == "insufficient"
    assert empty.date_span is None

    single = select_comparables(target(), [obs()], DEFAULT, AS_OF)
    assert single.status == "small_sample"
    assert single.sample_quality == "small"
    assert single.research_needed is True
    stats = single.stats_for(EvidenceKind.ASKING_PRICE)
    assert stats is not None and stats.sample_label == "single_observation"
    assert stats.n == 1 and stats.min == stats.max == stats.median == Decimal("9000.00")

    two = select_comparables(target(), [obs(amount="8000"), obs(amount="9001")], DEFAULT, AS_OF)
    stats2 = two.stats_for(EvidenceKind.ASKING_PRICE)
    assert stats2 is not None and stats2.sample_label == "small_sample"
    assert stats2.median == Decimal("8500.50")
    assert stats2.q1 is None and stats2.q3 is None


def test_quartiles_only_from_five() -> None:
    four = [obs(amount=a) for a in ("8000", "8500", "9000", "9500")]
    s4 = select_comparables(target(), four, DEFAULT, AS_OF).stats_for(EvidenceKind.ASKING_PRICE)
    assert s4 is not None and s4.q1 is None and s4.q3 is None
    assert s4.median == Decimal("8750.00")
    five = [*four, obs(amount="10000")]
    s5 = select_comparables(target(), five, DEFAULT, AS_OF).stats_for(EvidenceKind.ASKING_PRICE)
    assert s5 is not None
    assert (s5.min, s5.q1, s5.median, s5.q3, s5.max) == (
        Decimal("8000.00"),
        Decimal("8500.00"),
        Decimal("9000.00"),
        Decimal("9500.00"),
        Decimal("10000.00"),
    )
    assert s5.sample_label == "adequate"


def test_poor_comparable_stays_visible_in_aggregate() -> None:
    good = [obs(amount="9000"), obs(amount="9100")]
    poor = obs(amount="15000", km="232000")  # admitted only by widening
    result = select_comparables(target(), [*good, poor], DEFAULT, AS_OF)
    stats = result.stats_for(EvidenceKind.ASKING_PRICE)
    assert stats is not None
    assert stats.match_quality == {"exact": 2, "close": 0, "widened": 1}
    poor_point = next(p for p in stats.points if p.observation_id == poor.id)
    assert poor_point.match_level == "widened"
    assert any(code.startswith("mileage_window_widened") for code in poor_point.difference_codes)
    assert stats.max == Decimal("15000.00")


def test_date_span_reported() -> None:
    a = obs(observed_at=AS_OF - timedelta(days=30))
    b = obs(observed_at=AS_OF - timedelta(days=2))
    result = select_comparables(target(), [a, b], DEFAULT, AS_OF)
    stats = result.stats_for(EvidenceKind.ASKING_PRICE)
    assert stats is not None
    assert (stats.date_from, stats.date_to) == (a.observed_at, b.observed_at)
    assert result.date_span == (a.observed_at, b.observed_at)


def test_quantile_helper() -> None:
    values = [Decimal(v) for v in ("1", "2", "3", "4")]
    assert quantile(values, Decimal("0")) == Decimal("1")
    assert quantile(values, Decimal("1")) == Decimal("4")
    assert quantile(values, Decimal("0.5")) == Decimal("2.5")
    assert quantile(values, Decimal("0.25")) == Decimal("1.75")
    with pytest.raises(ValidationFailed):
        quantile([], Decimal("0.5"))
    with pytest.raises(ValidationFailed):
        quantile(values, Decimal("1.1"))


# --------------------------------------------------------------------------------------------- band fit


@pytest.mark.parametrize(
    ("amounts", "fit"),
    [
        (("7999.99", "7999.99", "7999.99"), "below"),
        (("8000.00", "8000.00", "8000.00"), "within"),
        (("10000.00", "10000.00", "10000.00"), "within"),
        (("10000.01", "10000.01", "10000.01"), "above"),
        (("6000", "9000", "12000"), "within"),
    ],
)
def test_mk_band_fit(amounts: tuple[str, ...], fit: str) -> None:
    result = select_comparables(target(), [obs(amount=a) for a in amounts], DEFAULT, AS_OF)
    assert result.mk_band_fit == fit
    assert "asking prices are not realized sales" in result.mk_band_fit_basis


def test_band_fit_ignores_sale_evidence() -> None:
    sales = [obs(kind=EvidenceKind.VERIFIED_SALE, amount="9000") for _ in range(3)]
    result = select_comparables(target(), sales, DEFAULT, AS_OF)
    assert result.status == "adequate"
    assert result.mk_band_fit == "unknown"


# --------------------------------------------------------------------------------------------- target


def test_target_from_listing() -> None:
    listing = NormalizedListing(
        source_key="fixture_dealer_de",
        source_listing_id="TEST-204",
        canonical_url="https://dealer.example/vehicles/TEST-204",
        observed_at=AS_OF,
        parser_version="fixture@1.0.0",
        vehicle=VehicleSpec(
            make="Example",
            model="Trail",
            generation="G2",
            first_registration=PartialDate(value="2011-05", precision="month"),
            model_year=2010,
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            engine_displacement_cm3=1995,
            power_kw=103,
            mileage_km=Decimal("187500"),
        ),
    )
    t = ComparableTarget.from_listing(listing, listing_id=uid(42))
    assert t.year == 2011 and t.year_source == "first_registration"
    assert t.listing_id == uid(42)
    assert t.mileage_km == Decimal("187500")
    no_reg = listing.model_copy(
        update={"vehicle": listing.vehicle.model_copy(update={"first_registration": PartialDate()})}
    )
    t2 = ComparableTarget.from_listing(no_reg)
    assert t2.year == 2010 and t2.year_source == "model_year"


def test_criteria_recorded() -> None:
    result = select_comparables(target(), [], Windows(year=2, km="20000", age=30, min_sample=4), AS_OF)
    assert result.criteria == MatchingCriteria(
        year_window=2,
        mileage_window_km=Decimal("20000"),
        max_year_window=3,
        max_mileage_window_km=Decimal("40000"),
        max_age_days=30,
        min_sample=4,
    )


def test_invalid_windows_rejected() -> None:
    with pytest.raises(ValidationFailed):
        select_comparables(target(), [], Windows(min_sample=0), AS_OF)


# --------------------------------------------------------------------------------------------- proceeds


def _adequate_result(amounts: tuple[str, ...]) -> Any:
    return select_comparables(target(), [obs(amount=a) for a in amounts], DEFAULT, AS_OF)


def test_proceeds_discount_unknown_is_labelled_and_undiscounted() -> None:
    result = _adequate_result(("8000", "9000", "10000"))
    proceeds = proceeds_from_comparables(result, None)
    assert proceeds.status == CostLineStatus.ESTIMATED
    assert proceeds.basis == "mk_asking_prices"
    assert proceeds.labels[0] == DISCOUNT_UNKNOWN_LABEL == "asking-based; discount unknown"
    assert proceeds.low == Money.of("8000.00", "EUR")
    assert proceeds.base == Money.of("9000.00", "EUR")
    assert proceeds.high == Money.of("10000.00", "EUR")
    assert proceeds.negotiation_discount_pct is None
    assert proceeds.sample_size == 3
    assert len(proceeds.evidence_ids) == 3
    assert proceeds.research_needed is False


def test_proceeds_with_discount_keeps_amounts_undiscounted() -> None:
    result = _adequate_result(("8000", "8500", "9000", "9500", "10000"))
    proceeds = proceeds_from_comparables(result, Decimal("7.5"), comparable_set_id=uid(800))
    assert proceeds.negotiation_discount_pct == Decimal("7.5")
    assert proceeds.base == Money.of("9000.00", "EUR")  # discount is applied once, downstream
    assert proceeds.low == Money.of("8500.00", "EUR") and proceeds.high == Money.of("9500.00", "EUR")
    assert any("unapproved assumption" in label for label in proceeds.labels)
    assert any("quartiles" in label for label in proceeds.labels)
    assert proceeds.comparable_set_id == str(uid(800))


def test_proceeds_sensitivity_is_hypothetical() -> None:
    result = _adequate_result(("8000", "9000", "10000"))
    proceeds = proceeds_from_comparables(
        result, None, sensitivity_discounts_pct=[Decimal("5"), Decimal("10")]
    )
    assert [p.base_after_discount for p in proceeds.sensitivity] == [
        Money.of("8550.00", "EUR"),
        Money.of("8100.00", "EUR"),
    ]
    assert all("hypothetical" in p.label for p in proceeds.sensitivity)


def test_proceeds_insufficient_returns_unknown() -> None:
    result = select_comparables(target(), [], DEFAULT, AS_OF)
    proceeds = proceeds_from_comparables(result, None)
    assert proceeds.status == CostLineStatus.UNKNOWN
    assert proceeds.low is None and proceeds.base is None and proceeds.high is None
    assert "insufficient_comparables" in proceeds.labels
    assert proceeds.research_needed is True


def test_proceeds_small_sample_labelled() -> None:
    result = _adequate_result(("8000", "9000"))
    proceeds = proceeds_from_comparables(result, None)
    assert any(label.startswith("small sample (n=2)") for label in proceeds.labels)
    assert proceeds.research_needed is True


def test_proceeds_prefer_adequate_verified_sales_without_discount() -> None:
    sales = [obs(kind=EvidenceKind.VERIFIED_SALE, amount=a) for a in ("7600", "7800", "8000")]
    asking = [obs(amount=a) for a in ("9000", "9500", "9900")]
    result = select_comparables(target(), [*sales, *asking], DEFAULT, AS_OF)
    proceeds = proceeds_from_comparables(result, Decimal("10"))
    assert proceeds.basis == "verified_sales"
    assert proceeds.base == Money.of("7800.00", "EUR")
    assert proceeds.negotiation_discount_pct is None
    assert "negotiation discount not applied to verified sales" in proceeds.labels


def test_proceeds_small_verified_sales_used_when_no_asking() -> None:
    sales = [obs(kind=EvidenceKind.VERIFIED_SALE, amount="7800")]
    result = select_comparables(target(), sales, DEFAULT, AS_OF)
    proceeds = proceeds_from_comparables(result, None)
    assert proceeds.basis == "verified_sales"
    assert proceeds.research_needed is True


def test_seller_sale_claims_never_become_proceeds() -> None:
    claims = [obs(kind=EvidenceKind.SELLER_REPORTED_SALE, amount="8000") for _ in range(5)]
    proceeds = proceeds_from_comparables(select_comparables(target(), claims, DEFAULT, AS_OF), None)
    assert proceeds.status == CostLineStatus.UNKNOWN


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("100"), Decimal("150")])
def test_proceeds_invalid_discount(bad: Decimal) -> None:
    result = _adequate_result(("8000", "9000", "10000"))
    with pytest.raises(ValidationFailed):
        proceeds_from_comparables(result, bad)
    with pytest.raises(ValidationFailed):
        proceeds_from_comparables(result, None, sensitivity_discounts_pct=[bad])


def test_approved_discount_needs_value() -> None:
    result = _adequate_result(("8000", "9000", "10000"))
    with pytest.raises(ValidationFailed):
        proceeds_from_comparables(result, None, discount_approved=True)


def test_proceeds_kwargs_build_costs_proceeds_estimate() -> None:
    costs = pytest.importorskip("suv_deals.domain.costs")
    result = _adequate_result(("8000", "9000", "10000"))
    for discount in (None, Decimal("5")):
        estimate = costs.ProceedsEstimate(
            **proceeds_from_comparables(result, discount).as_proceeds_estimate_kwargs()
        )
        assert estimate.base == Money.of("9000.00", "EUR")
    unknown = proceeds_from_comparables(select_comparables(target(), [], DEFAULT, AS_OF), None)
    estimate = costs.ProceedsEstimate(**unknown.as_proceeds_estimate_kwargs())
    assert estimate.status == CostLineStatus.UNKNOWN
    for amounts in (("7600", "7800", "8000"), ("7800",)):  # adequate and small verified-sale samples
        sales = [obs(kind=EvidenceKind.VERIFIED_SALE, amount=a) for a in amounts]
        verified = proceeds_from_comparables(
            select_comparables(target(), sales, DEFAULT, AS_OF), Decimal("10")
        )
        estimate = costs.ProceedsEstimate(**verified.as_proceeds_estimate_kwargs())
        assert estimate.basis == "verified_sales" and estimate.negotiation_discount_pct is None
        assert estimate.base == Money.of("7800.00", "EUR")


# ----------------------------------------------------------------------------------------- review regressions


def test_equal_short_engine_codes_match() -> None:
    # Regression: two identical 2-character codes were treated as incompatible (ENGINE_MISMATCH).
    same = obs(engine_code="K9")
    other = obs(engine_code="M9")
    result = select_comparables(target(engine_code="k9"), [same, other], Windows(min_sample=1), AS_OF)
    assert same.id in selected_ids(result)
    assert "engine_code_variant" not in {d.code for d in result.selected[0].differences}
    assert reasons_of(result, other.id) == (ExclusionReason.ENGINE_MISMATCH,)
    # the prefix rule still needs three characters
    short_prefix = obs(engine_code="N4")
    result2 = select_comparables(target(engine_code="N47D20"), [short_prefix], Windows(min_sample=1), AS_OF)
    assert reasons_of(result2, short_prefix.id) == (ExclusionReason.ENGINE_MISMATCH,)


@pytest.mark.parametrize(
    ("amounts", "fit", "display_median"),
    [
        # true median 7,999.995 displays as 8,000.00 but is below the band (spec 3 compare unrounded)
        (("7999.99", "8000.00"), "below", Decimal("8000.00")),
        # true median 10,000.005 displays as 10,000.00 but is above the band
        (("10000.00", "10000.01"), "above", Decimal("10000.00")),
    ],
)
def test_band_fit_compares_unrounded_median(
    amounts: tuple[str, ...], fit: str, display_median: Decimal
) -> None:
    result = select_comparables(target(), [obs(amount=a) for a in amounts], DEFAULT, AS_OF)
    stats = result.stats_for(EvidenceKind.ASKING_PRICE)
    assert stats is not None and stats.median == display_median
    assert result.mk_band_fit == fit


def test_damaged_target_is_labelled_against_running_comparables() -> None:
    running = obs()
    damaged_comp = obs(condition=ConditionSummary(damaged_vehicle=ClaimStatus.SELLER_CLAIMED))
    for target_condition in (
        {"damaged_vehicle": ClaimStatus.SELLER_CLAIMED},
        {"running": ClaimStatus.SELLER_DENIED},
    ):
        result = select_comparables(
            target(**target_condition), [running, damaged_comp], Windows(min_sample=1), AS_OF
        )
        by_id = {s.observation_id: s for s in result.selected}
        assert set(by_id) == {running.id, damaged_comp.id}
        assert "target_damaged_comparable_running" in {d.code for d in by_id[running.id].differences}
        assert by_id[running.id].match_level == "close"
        assert "target_damaged_comparable_running" not in {d.code for d in by_id[damaged_comp.id].differences}
        assert any(w.startswith("TARGET_DAMAGED_OR_NON_RUNNING") for w in result.warnings)
    healthy = select_comparables(target(), [running], Windows(min_sample=1), AS_OF)
    assert not any(w.startswith("TARGET_DAMAGED") for w in healthy.warnings)


def test_seller_reported_sale_stats_are_never_labelled_adequate() -> None:
    claims = [obs(kind=EvidenceKind.SELLER_REPORTED_SALE, amount=str(8000 + i)) for i in range(6)]
    result = select_comparables(target(), claims, Windows(min_sample=1), AS_OF)
    stats = result.stats_for(EvidenceKind.SELLER_REPORTED_SALE)
    assert stats is not None and stats.n == 6
    assert stats.sample_label == "unverified_claims"
    assert result.status == "insufficient_comparables"
    single = select_comparables(target(), claims[:1], Windows(min_sample=1), AS_OF)
    single_stats = single.stats_for(EvidenceKind.SELLER_REPORTED_SALE)
    assert single_stats is not None and single_stats.sample_label == "unverified_claims"


def _rate(rate: str, rate_date: date, purpose: FxPurpose = FxPurpose.REFERENCE) -> FxRate:
    # SYNTHETIC rates for tests only.
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal(rate),
        rate_date=rate_date,
        retrieved_at=AS_OF,
        provider="synthetic-test",
        purpose=purpose,
    )


def test_fx_rate_choice_is_order_independent_and_latest_reference_rate_wins() -> None:
    o = obs(amount="615000", currency="MKD")
    old, current = _rate("50", date(2020, 1, 1)), _rate("61.5", date(2026, 10, 5))
    a = select_comparables(target(), [o], Windows(min_sample=1), AS_OF, [old, current])
    b = select_comparables(target(), [o], Windows(min_sample=1), AS_OF, [current, old])
    assert a == b
    assert a.selected[0].amount_eur == Decimal("10000.000000")
    assert any(w.startswith("FX_RATE_USED: 1 EUR = 61.5 MKD") and "2026-10-05" in w for w in a.warnings)
    assert not any(w.startswith("FX_RATE_OLD") for w in a.warnings)


def test_fx_customs_payment_and_future_rates_never_used_for_estimation() -> None:
    o = obs(amount="615000", currency="MKD")
    unusable = [
        _rate("70", date(2026, 10, 5), FxPurpose.CUSTOMS),
        _rate("60", date(2026, 10, 5), FxPurpose.PAYMENT),
        _rate("40", date(2026, 12, 1)),  # dated after as_of: the set would not be reproducible
    ]
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF, unusable)
    assert reasons_of(result, o.id) == (ExclusionReason.CURRENCY_UNCONVERTIBLE,)
    assert any(w.startswith("FX_RATES_IGNORED: 3") for w in result.warnings)


def test_old_fx_rate_is_flagged_not_hidden() -> None:
    o = obs(amount="615000", currency="MKD")
    result = select_comparables(
        target(), [o], Windows(min_sample=1), AS_OF, [_rate("61.5", date(2026, 9, 1))]
    )
    assert result.selected[0].amount_eur == Decimal("10000.000000")
    assert any(w.startswith("FX_RATE_OLD: EUR/MKD rate is 35 days old") for w in result.warnings)


def test_inverse_direction_rate_converts_by_stored_direction() -> None:
    # SYNTHETIC: 1 MKD = 0.016 EUR expressed with MKD as base -> multiply MKD by 0.016
    inverse = FxRate(
        base="MKD",
        quote="EUR",
        rate=Decimal("0.016"),
        rate_date=date(2026, 10, 5),
        retrieved_at=AS_OF,
        provider="synthetic-test",
    )
    o = obs(amount="625000", currency="MKD")
    result = select_comparables(target(), [o], Windows(min_sample=1), AS_OF, [inverse])
    assert result.selected[0].amount_eur == Decimal("10000.000000")


# ---------------------------------------------------------------------------------------------
# Owner estimates as the fallback orientation (owner decision 2026-10-16) and canonical names
# ---------------------------------------------------------------------------------------------


def _estimate(amount: str = "9000", **kw: Any) -> MarketObservation:
    return obs(kind=EvidenceKind.OWNER_ESTIMATE, amount=amount, url=None, **kw)


def test_owner_estimate_selection_holds_only_estimates_and_is_never_adequate() -> None:
    asking = obs(amount="9100")
    estimates = [_estimate("8800"), _estimate("9000"), _estimate("9300", km="190000")]
    result = select_comparables(target(), [asking, *estimates], DEFAULT, AS_OF, owner_estimates=True)
    assert {s.observation_id for s in result.selected} == {e.id for e in estimates}
    assert reasons_of(result, asking.id) == (ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND,)
    # Three estimates meet min_sample 3, yet an assumption is never an adequate market sample.
    assert result.status == "small_sample"
    assert result.research_needed
    stats = result.stats_for(EvidenceKind.OWNER_ESTIMATE)
    assert stats is not None and stats.sample_label == "small_sample" and stats.median == Decimal("9000.00")
    assert result.stats_for(EvidenceKind.ASKING_PRICE) is None
    assert result.adequacy_count == 0
    assert result.mk_band_fit == "unknown"
    assert any(w.startswith("OWNER_ESTIMATES_ONLY") for w in result.warnings)


def test_market_selection_still_never_holds_an_owner_estimate() -> None:
    estimate = _estimate()
    result = select_comparables(target(), [estimate], DEFAULT, AS_OF)
    assert result.selected == ()
    assert reasons_of(result, estimate.id) == (ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND,)
    unknown = comparables_module.proceeds_from_owner_estimates(result)
    assert unknown.status == CostLineStatus.UNKNOWN and unknown.base is None


def test_owner_estimate_widening_stops_at_the_first_admitted_estimate() -> None:
    close = _estimate("9000", year=2011)
    far = _estimate("7600", year=2013)  # needs the widened year window
    result = select_comparables(target(), [close, far], DEFAULT, AS_OF, owner_estimates=True)
    assert [s.observation_id for s in result.selected] == [close.id]
    assert result.widening_steps == ()
    assert ExclusionReason.YEAR_OUT_OF_WINDOW in reasons_of(result, far.id)
    # Only the far estimate: widened (labelled) to reach one reference point.
    alone = select_comparables(target(), [far], DEFAULT, AS_OF, owner_estimates=True)
    assert [s.observation_id for s in alone.selected] == [far.id]
    assert [step.dimension for step in alone.widening_steps] == ["mileage", "year"]
    assert alone.selected[0].match_level == "widened"
    assert alone.stats_for(EvidenceKind.OWNER_ESTIMATE).sample_label == "single_observation"  # type: ignore[union-attr]


def test_owner_estimates_keep_the_strict_matching_rules() -> None:
    petrol = _estimate(fuel=Fuel.PETROL)
    automatic = _estimate(gearbox=Gearbox.AUTOMATIC)
    two_wd = _estimate(drive=Drive.FWD)
    result = select_comparables(target(), [petrol, automatic, two_wd], DEFAULT, AS_OF, owner_estimates=True)
    assert result.selected == ()
    assert result.status == "insufficient_comparables"
    assert ExclusionReason.FUEL_MISMATCH in reasons_of(result, petrol.id)
    assert ExclusionReason.GEARBOX_MISMATCH in reasons_of(result, automatic.id)
    assert ExclusionReason.DRIVE_MISMATCH in reasons_of(result, two_wd.id)


def test_proceeds_from_owner_estimates_are_labelled_and_fit_the_cost_model() -> None:
    estimates = [_estimate("8600", km="170000"), _estimate("9300", km="195000")]
    result = select_comparables(target(), estimates, DEFAULT, AS_OF, owner_estimates=True)
    proceeds = comparables_module.proceeds_from_owner_estimates(result)
    assert proceeds.status == CostLineStatus.ESTIMATED
    assert proceeds.basis == "owner_estimate" and proceeds.evidence_kind == EvidenceKind.OWNER_ESTIMATE
    assert (proceeds.low, proceeds.base, proceeds.high) == (
        Money.of("8600", "EUR"),
        Money.of("8950", "EUR"),
        Money.of("9300", "EUR"),
    )
    assert proceeds.negotiation_discount_pct is None and not proceeds.discount_approved
    assert proceeds.comparable_set_id is None and proceeds.research_needed
    assert set(proceeds.evidence_ids) == {str(e.id) for e in estimates}
    assert proceeds.labels[0] == comparables_module.OWNER_ESTIMATE_LABEL
    estimate = ProceedsEstimate(**proceeds.as_proceeds_estimate_kwargs())
    assert estimate.label() == comparables_module.OWNER_ESTIMATE_LABEL
    assert not proceeds_supported(estimate)  # never an approved basis for an alert


def test_owner_estimate_proceeds_without_a_match_are_unknown() -> None:
    result = select_comparables(target(), [], DEFAULT, AS_OF, owner_estimates=True)
    proceeds = comparables_module.proceeds_from_owner_estimates(result)
    assert proceeds.status == CostLineStatus.UNKNOWN
    assert proceeds.low is proceeds.base is proceeds.high is None
    assert proceeds.research_needed


def test_taxonomy_compares_canonical_make_and_model_names() -> None:
    mokka = target(make="Opel", model="Mokka X", generation=None)
    row = obs(make="Opel", model="Mokka", generation=None)
    plain = select_comparables(mokka, [row], Windows(min_sample=1), AS_OF)
    assert reasons_of(plain, row.id) == (ExclusionReason.WRONG_MODEL,)
    canonical = select_comparables(mokka, [row], Windows(min_sample=1), AS_OF, taxonomy=default_taxonomy())
    assert [s.observation_id for s in canonical.selected] == [row.id]
    assert (canonical.target.make, canonical.target.model) == ("Opel", "Mokka")


def test_canonical_names_keep_unknown_or_missing_names() -> None:
    taxonomy = default_taxonomy()
    assert comparables_module.canonical_names(taxonomy, "Skoda", "Yeti 1.6 TDI") == ("Škoda", "Yeti")
    assert comparables_module.canonical_names(taxonomy, "Example", "Trail") == ("Example", "Trail")
    assert comparables_module.canonical_names(taxonomy, "Opel", None) == ("Opel", None)
    assert comparables_module.canonical_names(None, "Opel", "Mokka X") == ("Opel", "Mokka X")
    unchanged = target()
    assert unchanged.with_canonical_names(taxonomy) is unchanged


# ---------------------------------------------------------------------------------------------
# MK price orientation: a framework, not strict rules (owner decision 2026-10-16, ADR 0005)
# ---------------------------------------------------------------------------------------------


def orient(cands: list[MarketObservation], kind: EvidenceKind = EvidenceKind.ASKING_PRICE, **kw: Any) -> Any:
    t = kw.pop("tgt", None) or target()
    return orient_comparables(t, cands, DEFAULT, AS_OF, kind=kind, **kw)


def codes(point: Any) -> set[str]:
    return {d.code for d in point.differences}


def test_orientation_labels_every_relaxed_difference_instead_of_excluding() -> None:
    petrol = obs(fuel=Fuel.PETROL, amount="8000")
    g1 = obs(generation="G1", amount="7000")
    old = obs(year=2008, km="250000", amount="6000")
    automatic_fwd = obs(gearbox=Gearbox.AUTOMATIC, drive=Drive.FWD, amount="8500")
    unknowns = obs(fuel=Fuel.UNKNOWN, year=None, km=None, amount="9500")
    other_engine = obs(engine_displacement_cm3=1598, power_kw=85, amount="8700")
    facelift = obs(facelift=Tristate.YES, amount="9100")
    cands = [petrol, g1, old, automatic_fwd, unknowns, other_engine, facelift]
    strict = select_comparables(target(), cands, DEFAULT, AS_OF)
    # Only the facelift difference is a strict (labelled) widening step; the rest is excluded.
    assert selected_ids(strict) == {facelift.id}
    result = orient(cands, limit=10)
    assert result.excluded == () and result.eligible_count == 7
    by_id = {p.observation_id: p for p in result.points}
    assert "orientation_fuel_mismatch" in codes(by_id[petrol.id])
    assert by_id[petrol.id].distance == Decimal("5.25")  # fuel 5 + 7,500 km / 30,000 km
    assert "orientation_wrong_generation" in codes(by_id[g1.id])
    assert {"orientation_year_differs", "orientation_mileage_differs"} <= codes(by_id[old.id])
    assert by_id[old.id].distance == Decimal("5.083333")  # 3 years + 62,500 km / 30,000 km
    assert {"orientation_gearbox_mismatch", "orientation_drive_mismatch"} <= codes(by_id[automatic_fwd.id])
    assert {
        "orientation_fuel_unverified",
        "orientation_year_unknown",
        "orientation_mileage_unknown",
    } <= codes(by_id[unknowns.id])
    assert by_id[unknowns.id].distance == Decimal(3)
    assert "orientation_engine_mismatch" in codes(by_id[other_engine.id])
    assert "orientation_facelift_differs" in codes(by_id[facelift.id])
    fuel = next(d for d in by_id[petrol.id].differences if d.code == "orientation_fuel_mismatch")
    assert (fuel.dimension, fuel.severity, fuel.target, fuel.comparable) == (
        "fuel",
        DifferenceSeverity.DIFFERS,
        "diesel",
        "petrol",
    )
    unverified = next(d for d in by_id[unknowns.id].differences if d.code == "orientation_year_unknown")
    assert unverified.severity == DifferenceSeverity.UNVERIFIED
    # The strict labels stay next to the orientation labels.
    assert "engine_code_unverified" in codes(by_id[petrol.id])


def test_orientation_keeps_identity_condition_market_and_freshness_hard() -> None:
    other_model = obs(model="Other")
    no_model = obs(model=None)
    damaged = obs(condition=ConditionSummary(damaged_vehicle=ClaimStatus.SELLER_CLAIMED))
    stale = obs(observed_at=AS_OF - timedelta(days=200))
    future = obs(observed_at=AS_OF + timedelta(days=1))
    no_price = obs(amount=None)
    mkd = obs(amount="500000", currency="MKD")  # no recorded EUR/MKD rate
    foreign = obs(market="DE")
    fixture = obs(is_fixture=True)
    kept = obs()
    result = orient([other_model, no_model, damaged, stale, future, no_price, mkd, foreign, fixture, kept])
    assert [p.observation_id for p in result.points] == [kept.id]
    reasons = {e.observation_id: e.reasons for e in result.excluded}
    assert reasons[other_model.id] == (ExclusionReason.WRONG_MODEL,)
    assert reasons[no_model.id] == (ExclusionReason.MODEL_UNKNOWN,)
    assert reasons[damaged.id] == (ExclusionReason.CONDITION_MISMATCH,)
    assert reasons[stale.id] == (ExclusionReason.STALE,)
    assert reasons[future.id] == (ExclusionReason.OBSERVED_AFTER_AS_OF,)
    assert reasons[no_price.id] == (ExclusionReason.PRICE_MISSING,)
    assert reasons[mkd.id] == (ExclusionReason.CURRENCY_UNCONVERTIBLE,)
    assert reasons[foreign.id] == (ExclusionReason.WRONG_MARKET,)
    assert reasons[fixture.id] == (ExclusionReason.FIXTURE_MISMATCH,)


def test_orientation_holds_exactly_one_evidence_kind() -> None:
    asking = obs()
    verified = obs(kind=EvidenceKind.VERIFIED_SALE)
    claimed = obs(kind=EvidenceKind.SELLER_REPORTED_SALE)
    estimate = obs(kind=EvidenceKind.OWNER_ESTIMATE, url=None)
    cands = [asking, verified, claimed, estimate]
    over_asking = orient(cands)
    assert [p.observation_id for p in over_asking.points] == [asking.id]
    assert {e.observation_id for e in over_asking.excluded} == {verified.id, claimed.id, estimate.id}
    assert all(e.reasons == (ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND,) for e in over_asking.excluded)
    over_estimates = orient(cands, kind=EvidenceKind.OWNER_ESTIMATE)
    assert [p.observation_id for p in over_estimates.points] == [estimate.id]
    assert all(p.evidence_kind == EvidenceKind.OWNER_ESTIMATE for p in over_estimates.points)
    for bad in (EvidenceKind.VERIFIED_SALE, EvidenceKind.SELLER_REPORTED_SALE):
        with pytest.raises(ValidationFailed):
            orient(cands, kind=bad)


def test_orientation_ranks_nearest_first_and_keeps_the_limit() -> None:
    exact = obs(km="187500", amount="9000")
    km_off = obs(km="217500", amount="9200")  # +1.0
    year_off = obs(year=2013, km="187500", amount="9400")  # +2.0
    petrol = obs(fuel=Fuel.PETROL, km="187500", amount="7000")  # +5.0
    far = [obs(year=2005, km="187500", amount=str(5000 + i)) for i in range(3)]  # +6.0
    result = orient([far[0], petrol, year_off, exact, far[1], km_off, far[2]])
    assert result.limit == ORIENTATION_LIMIT == 5
    assert result.eligible_count == 7 and len(result.points) == 5
    assert [p.observation_id for p in result.points[:4]] == [exact.id, km_off.id, year_off.id, petrol.id]
    assert [p.rank for p in result.points] == [1, 2, 3, 4, 5]
    assert [p.distance for p in result.points[:4]] == [Decimal(0), Decimal(1), Decimal(2), Decimal(5)]
    assert result.points[0].distance == result.nearest_distance
    assert result.points[4].observation_id in {f.id for f in far}
    assert len(orient([exact, km_off], limit=1).points) == 1
    for bad in (0, 51):
        with pytest.raises(ValidationFailed):
            orient([exact], limit=bad)


def test_orientation_tie_break_is_newer_first_then_smallest_id() -> None:
    older = obs(oid=uid(9201), observed_at=AS_OF - timedelta(days=9))
    newer_b = obs(oid=uid(9203), observed_at=AS_OF - timedelta(days=2))
    newer_a = obs(oid=uid(9202), observed_at=AS_OF - timedelta(days=2))
    result = orient([older, newer_b, newer_a])
    assert [p.observation_id for p in result.points] == [newer_a.id, newer_b.id, older.id]


def test_orientation_is_independent_of_candidate_order() -> None:
    cands = [obs(year=2008 + i % 6, km=str(150000 + 9000 * i), amount=str(7000 + 150 * i)) for i in range(12)]
    cands += [obs(fuel=Fuel.PETROL), obs(model="Other"), obs(url=AD), obs(url=AD)]
    first = orient(cands)
    shuffled = list(cands)
    random.Random(7).shuffle(shuffled)
    assert orient(shuffled) == first


def test_orientation_counts_a_daily_re_recorded_ad_once() -> None:
    days = [obs(url=AD, observed_at=AS_OF - timedelta(days=d), amount="9000") for d in (3, 2, 1)]
    result = orient(days)
    assert [p.observation_id for p in result.points] == [days[-1].id]
    assert {e.reasons for e in result.excluded} == {(ExclusionReason.DUPLICATE_OF_URL,)}
    assert {e.duplicate_of for e in result.excluded} == {days[-1].id}


def test_orientation_uses_canonical_names_and_reports_fx() -> None:
    mokka = target(make="Opel", model="Mokka X", generation=None)
    row = obs(make="Opel", model="Mokka", generation=None, amount="553500", currency="MKD")
    rate = FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.5"),
        rate_date=date(2026, 10, 5),
        retrieved_at=datetime(2026, 10, 5, 16, tzinfo=UTC),
        provider="SYNTHETIC rate",
        purpose=FxPurpose.REFERENCE,
    )
    plain = orient_comparables(mokka, [row], DEFAULT, AS_OF, [rate], kind=EvidenceKind.ASKING_PRICE)
    assert plain.points == () and plain.excluded[0].reasons == (ExclusionReason.WRONG_MODEL,)
    result = orient_comparables(
        mokka, [row], DEFAULT, AS_OF, [rate], kind=EvidenceKind.ASKING_PRICE, taxonomy=default_taxonomy()
    )
    assert result.points[0].amount_eur == Decimal("9000.000000")
    assert any(w.startswith("FX_RATE_USED") for w in result.warnings)


def test_orientation_warnings_and_freshness() -> None:
    newest, oldest = obs(observed_at=AS_OF - timedelta(days=1)), obs(observed_at=AS_OF - timedelta(days=20))
    close = orient([newest, oldest])
    assert close.version == ORIENTATION_VERSION
    assert close.warnings[0].startswith("ORIENTATION_ONLY")
    assert not any(w.startswith("ORIENTATION_DISTANT") for w in close.warnings)
    assert close.fresh_until == oldest.observed_at + timedelta(days=DEFAULT.comparable_max_age_days)
    distant = orient([obs(fuel=Fuel.PETROL)])
    assert distant.nearest_distance is not None and distant.nearest_distance > ORIENTATION_CLOSE
    assert any(w.startswith("ORIENTATION_DISTANT") for w in distant.warnings)
    empty = orient([])
    assert empty.points == () and empty.fresh_until is None and empty.nearest_distance is None
    assert any(w.startswith("ORIENTATION_EMPTY") for w in empty.warnings)
    with pytest.raises(ValidationFailed):
        orient_comparables(target(), [], DEFAULT, datetime(2026, 10, 6), kind=EvidenceKind.ASKING_PRICE)
    o = obs()
    with pytest.raises(ValidationFailed):
        orient([o, o])


def test_orientation_warning_summarises_the_points() -> None:
    result = orient([obs(fuel=Fuel.PETROL, km="187500"), obs(year=2014, km="187500"), obs(km="187500")])
    text = orientation_warning(result)
    assert text.startswith("MK_ORIENTATION:asking_price: 3 nearest same-model MK point(s), distance 0-5")
    assert "differs: fuel_mismatch x1, year_differs x1" in text
    assert ORIENTATION_VERSION in text and len(text) <= 500
    assert orientation_warning(orient([])).startswith("MK_ORIENTATION:asking_price: no same-model MK point")


def test_proceeds_from_asking_orientation_are_labelled_and_never_supported() -> None:
    result = orient([obs(fuel=Fuel.PETROL, amount=a) for a in ("7000", "8000", "9000")])
    proceeds = proceeds_from_orientation(result)
    assert proceeds.status == CostLineStatus.ESTIMATED
    assert proceeds.basis == "mk_asking_prices" and proceeds.evidence_kind == EvidenceKind.ASKING_PRICE
    assert (proceeds.low, proceeds.base, proceeds.high) == (
        Money.of("7000.00", "EUR"),
        Money.of("8000.00", "EUR"),
        Money.of("9000.00", "EUR"),
    )
    assert proceeds.labels[:2] == (ORIENTATION_LABEL, DISCOUNT_UNKNOWN_LABEL)
    assert any(label.startswith("3 nearest same-model point(s), distance") for label in proceeds.labels)
    assert proceeds.comparable_set_id is None and proceeds.research_needed
    assert set(proceeds.evidence_ids) == {str(p.observation_id) for p in result.points}
    estimate = ProceedsEstimate(**proceeds.as_proceeds_estimate_kwargs())
    assert estimate.comparable_set_id is None
    assert not proceeds_supported(estimate)
    discounted = proceeds_from_orientation(
        result, Decimal("7.5"), discount_approved=True, sensitivity_discounts_pct=[Decimal("10")]
    )
    assert discounted.negotiation_discount_pct == Decimal("7.5") and discounted.discount_approved
    assert discounted.base == Money.of("8000.00", "EUR")  # applied once, by the scenario engine
    assert any("7.5% negotiation discount (approved)" in label for label in discounted.labels)
    assert discounted.sensitivity[0].base_after_discount == Money.of("7200.00", "EUR")
    assert not proceeds_supported(ProceedsEstimate(**discounted.as_proceeds_estimate_kwargs()))
    with pytest.raises(ValidationFailed):
        proceeds_from_orientation(result, None, discount_approved=True)
    with pytest.raises(ValidationFailed):
        proceeds_from_orientation(result, Decimal("100"))


def test_proceeds_from_orientation_use_quartiles_from_five_points() -> None:
    result = orient([obs(amount=a) for a in ("8000", "8500", "9000", "9500", "10000", "12000")], limit=5)
    proceeds = proceeds_from_orientation(result)
    assert proceeds.sample_size == 5
    assert "low/high are the first/third quartiles" in proceeds.labels


def test_proceeds_from_owner_estimate_orientation_and_empty_orientation() -> None:
    estimates = [
        obs(kind=EvidenceKind.OWNER_ESTIMATE, url=None, gearbox=Gearbox.AUTOMATIC, amount=a)
        for a in ("8600", "9300")
    ]
    result = orient(estimates, kind=EvidenceKind.OWNER_ESTIMATE)
    proceeds = proceeds_from_orientation(result, Decimal("10"))  # no discount for an estimate
    assert proceeds.basis == "owner_estimate" and proceeds.evidence_kind == EvidenceKind.OWNER_ESTIMATE
    assert proceeds.negotiation_discount_pct is None and not proceeds.discount_approved
    assert proceeds.base == Money.of("8950.00", "EUR")
    assert comparables_module.OWNER_ESTIMATE_LABEL in proceeds.labels
    estimate = ProceedsEstimate(**proceeds.as_proceeds_estimate_kwargs())
    assert not proceeds_supported(estimate)
    for kind, basis in (
        (EvidenceKind.ASKING_PRICE, "mk_asking_prices"),
        (EvidenceKind.OWNER_ESTIMATE, "owner_estimate"),
    ):
        unknown = proceeds_from_orientation(orient([], kind=kind))
        assert unknown.status == CostLineStatus.UNKNOWN and unknown.basis == basis
        assert unknown.low is unknown.base is unknown.high is None and unknown.research_needed
        assert ProceedsEstimate(**unknown.as_proceeds_estimate_kwargs()).status == CostLineStatus.UNKNOWN


def test_orientation_re_record_with_a_model_typo_never_hides_the_valid_one() -> None:
    old = obs(url=AD, observed_at=AS_OF - timedelta(days=2))
    typo = obs(url=AD, observed_at=AS_OF - timedelta(days=1), model="Trial")
    result = orient([old, typo])
    assert [p.observation_id for p in result.points] == [old.id]
    assert set(next(e for e in result.excluded if e.observation_id == typo.id).reasons) == {
        ExclusionReason.WRONG_MODEL,
        ExclusionReason.DUPLICATE_OF_URL,
    }


def test_orientation_keeps_the_row_of_an_ad_the_strict_set_keeps() -> None:
    old_full = obs(url=AD, observed_at=AS_OF - timedelta(days=2))
    new_gap = obs(url=AD, observed_at=AS_OF - timedelta(days=1), km=None)  # orientation-eligible
    strict = select_comparables(target(), [old_full, new_gap], Windows(min_sample=1), AS_OF)
    oriented = orient([old_full, new_gap])
    assert selected_ids(strict) == {p.observation_id for p in oriented.points} == {old_full.id}
    alone = orient([obs(url=AD, observed_at=AS_OF - timedelta(days=2), km=None), new_gap])
    assert [p.observation_id for p in alone.points] == [new_gap.id]  # neither is strict: newest


def test_orientation_closeness_counts_unknowns_as_the_mismatch_they_may_hide() -> None:
    near = obs(year=2013, km="187500")  # 2 years: distance 2
    close = orient([near])
    assert close.nearest_distance == close.nearest_verified_distance == Decimal(2)
    assert close.close and not any(w.startswith("ORIENTATION_DISTANT") for w in close.warnings)
    # Only make, model and year known: distance exactly ORIENTATION_CLOSE, but it may be a petrol.
    unknown = obs(fuel=Fuel.UNKNOWN, gearbox=Gearbox.UNKNOWN, drive=Drive.UNKNOWN, km=None)
    blind = orient([unknown])
    assert blind.nearest_distance == ORIENTATION_CLOSE
    assert blind.nearest_verified_distance == Decimal(10)  # fuel 5 + gearbox 2 + drive 2 + km 1
    assert not blind.close
    assert any(w.startswith("ORIENTATION_DISTANT") for w in blind.warnings)
    # A known near-variant is not close either (gearbox 2 + 2 years + 7,500 km = 4.25).
    variant = orient([obs(gearbox=Gearbox.AUTOMATIC, year=2013)])
    assert variant.nearest_distance == Decimal("4.25") and not variant.close
    # A verified-close point anywhere among the eligible ones makes the orientation close, even
    # when an unknown ranks first.
    fuel_unknown = obs(fuel=Fuel.UNKNOWN, km="187500")  # distance 1, verified 5
    third_year = obs(year=2014, km="187500")  # distance 3, verified 3
    mixed = orient([fuel_unknown, third_year])
    assert [p.observation_id for p in mixed.points] == [fuel_unknown.id, third_year.id]
    assert mixed.nearest_verified_distance == Decimal(3) and mixed.close


def test_orientation_target_side_unknowns_are_never_close() -> None:
    # Target-side unknowns add the same amount to every point; the closeness test counts them as
    # mismatches. The fallback order is unaffected: the strict owner estimates need the same
    # fields known (ADR 0004 ``*_UNVERIFIED``).
    blind_target = target(fuel=Fuel.UNKNOWN, gearbox=Gearbox.UNKNOWN, drive=Drive.UNKNOWN)
    ad = obs(year=2013, km="187500")
    result = orient([ad], tgt=blind_target)
    assert result.nearest_distance == Decimal(5) and not result.close
    estimate = obs(kind=EvidenceKind.OWNER_ESTIMATE, url=None)
    strict = select_comparables(blind_target, [estimate], DEFAULT, AS_OF, owner_estimates=True)
    assert strict.selected == ()
    assert ExclusionReason.FUEL_UNVERIFIED in reasons_of(strict, estimate.id)
