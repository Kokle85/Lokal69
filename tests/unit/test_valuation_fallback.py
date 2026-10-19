"""Unit tests for the ADR 0005 proceeds fallback order (``valuation_pipeline.fallback_proceeds``).

When the spec-15 comparable set gives no proceeds: (a) a close asking-price orientation, (b) the
strictly matched owner estimates, (c) any asking-price orientation, (d) the relaxed orientation
over the owner's estimates. Every vehicle, price and ad URL is SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.unit.test_comparables import AS_OF, obs, target

from suv_deals.crawling.valuation_pipeline import ProceedsFallback, fallback_proceeds
from suv_deals.domain.comparables import ComparableTarget, MarketObservation, select_comparables
from suv_deals.domain.enums import Drive, EvidenceKind, Fuel, Gearbox
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import BusinessConfig, load_business_config

CONFIG: BusinessConfig = load_business_config(Path(__file__).resolve().parents[2] / "config")


def _estimate(**kw: Any) -> MarketObservation:
    return obs(kind=EvidenceKind.OWNER_ESTIMATE, url=None, **kw)


def _fallback(cands: list[MarketObservation], tgt: ComparableTarget | None = None) -> ProceedsFallback | None:
    tgt = tgt or target()
    estimates = select_comparables(tgt, cands, CONFIG, AS_OF, owner_estimates=True)
    return fallback_proceeds(
        tgt, cands, config=CONFIG, as_of=AS_OF, rates=(), taxonomy=None, estimates=estimates
    )


def test_a_close_asking_orientation_beats_a_strictly_matched_owner_estimate() -> None:
    estimate = _estimate(amount="12000")
    ads = [obs(year=2014, km="187500", amount="9600"), obs(year=2014, km="187500", amount="9800")]
    fallback = _fallback([estimate, *ads])
    assert fallback is not None and fallback.orientation is not None
    assert fallback.orientation.close and fallback.orientation.evidence_kind == EvidenceKind.ASKING_PRICE
    assert fallback.proceeds.basis == "mk_asking_prices"
    assert set(fallback.proceeds.evidence_ids) == {str(a.id) for a in ads}
    assert fallback.proceeds.base == Money.of("9700.00", "EUR")
    assert fallback.fresh_until == min(a.observed_at for a in ads) + timedelta(days=90)


def test_a_strict_owner_estimate_beats_a_distant_asking_orientation() -> None:
    estimate = _estimate(amount="9300", observed_at=AS_OF - timedelta(days=5))
    petrol = obs(fuel=Fuel.PETROL, amount="7900")
    fallback = _fallback([estimate, petrol])
    assert fallback is not None and fallback.orientation is None
    assert fallback.proceeds.basis == "owner_estimate"
    assert fallback.proceeds.evidence_ids == (str(estimate.id),)
    assert fallback.observations == (estimate,)
    assert fallback.fresh_until == estimate.observed_at + timedelta(days=90)


def test_an_ad_of_unknown_fuel_gearbox_and_drive_never_beats_a_matched_estimate() -> None:
    # Distance exactly ORIENTATION_CLOSE, but it may be a petrol: not verified close.
    blind = obs(fuel=Fuel.UNKNOWN, gearbox=Gearbox.UNKNOWN, drive=Drive.UNKNOWN, km=None, amount="6500")
    estimate = _estimate(amount="12000")
    fallback = _fallback([blind, estimate])
    assert fallback is not None and fallback.proceeds.basis == "owner_estimate"
    assert fallback.proceeds.base == Money.of("12000.00", "EUR")


def test_a_distant_asking_orientation_is_used_without_any_estimate() -> None:
    petrol = obs(fuel=Fuel.PETROL, amount="7900")
    fallback = _fallback([petrol])
    assert fallback is not None and fallback.orientation is not None
    assert not fallback.orientation.close
    assert fallback.proceeds.basis == "mk_asking_prices"
    assert fallback.proceeds.evidence_ids == (str(petrol.id),)
    assert any(w.startswith("ORIENTATION_DISTANT") for w in fallback.orientation.warnings)


def test_the_relaxed_owner_estimate_orientation_is_the_last_fallback() -> None:
    estimate = _estimate(year=2016, amount="9300")  # outside every strict year window
    fallback = _fallback([estimate])
    assert fallback is not None and fallback.orientation is not None
    assert fallback.orientation.evidence_kind == EvidenceKind.OWNER_ESTIMATE
    assert fallback.proceeds.basis == "owner_estimate"
    assert fallback.proceeds.negotiation_discount_pct is None
    assert fallback.fresh_until == estimate.observed_at + timedelta(days=90)


def test_no_same_model_point_gives_no_fallback() -> None:
    assert _fallback([obs(model="Other"), _estimate(model="Other")]) is None
    assert _fallback([]) is None


def test_target_side_unknowns_do_not_move_the_fallback() -> None:
    # A listing with unknown fuel, gearbox and drive: no orientation is verified close, but the
    # strict estimates cannot match either, so step (c) uses the same asking-price orientation.
    blind_target = target(fuel=Fuel.UNKNOWN, gearbox=Gearbox.UNKNOWN, drive=Drive.UNKNOWN)
    ad = obs(year=2013, km="187500", amount="9600")
    fallback = _fallback([ad, _estimate(amount="12000")], blind_target)
    assert fallback is not None and fallback.orientation is not None
    assert not fallback.orientation.close
    assert fallback.proceeds.basis == "mk_asking_prices"
    assert fallback.proceeds.evidence_ids == (str(ad.id),)


def test_the_configured_discount_is_passed_to_asking_orientations_only() -> None:
    config = CONFIG.model_copy(update={"negotiation_discount_pct": Decimal("10")})
    ad = obs(year=2013, km="187500")
    asking = fallback_proceeds(
        target(),
        [ad],
        config=config,
        as_of=AS_OF,
        rates=(),
        taxonomy=None,
        estimates=select_comparables(target(), [ad], config, AS_OF, owner_estimates=True),
    )
    assert asking is not None and asking.proceeds.negotiation_discount_pct == Decimal("10")
    estimate = _estimate(year=2016)
    loose = fallback_proceeds(
        target(),
        [estimate],
        config=config,
        as_of=AS_OF,
        rates=(),
        taxonomy=None,
        estimates=select_comparables(target(), [estimate], config, AS_OF, owner_estimates=True),
    )
    assert loose is not None and loose.proceeds.negotiation_discount_pct is None
