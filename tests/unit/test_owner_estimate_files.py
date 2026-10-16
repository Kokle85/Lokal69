"""The owner's MK estimate files in ``data/market`` stay importable and match the taxonomy.

The files hold the owner's OWN resale estimates of 2026-10-16 (``owner_estimate``; not ads, not
sales). They must parse with the import validator, use the taxonomy's canonical make/model names
(the candidate lookup is by exact name) and be SUV/crossover models of the search.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from suv_deals.domain.comparables import (
    ComparableTarget,
    MarketObservation,
    canonical_names,
    proceeds_from_owner_estimates,
    select_comparables,
)
from suv_deals.domain.enums import Drive, EvidenceKind, Fuel, Gearbox
from suv_deals.domain.market_import import parse_import, to_observation
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.taxonomy import default_taxonomy

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "market"
FILES = ("owner_estimates_mk_2026-10-16.json", "owner_estimates_mk_2026-10-16_to_verify.json")


@pytest.mark.parametrize("name", FILES)
def test_owner_estimate_file_parses_with_canonical_suv_names(name: str) -> None:
    parsed = parse_import((DATA / name).read_bytes(), expected_kind=EvidenceKind.OWNER_ESTIMATE)
    taxonomy = default_taxonomy()
    ids = set()
    for row in parsed.observations:
        vehicle = row.vehicle
        assert canonical_names(taxonomy, vehicle.make, vehicle.model) == (vehicle.make, vehicle.model)
        year = int(vehicle.first_registration or "0")
        match = taxonomy.match_vehicle(vehicle.make, vehicle.model, None, year)
        assert match.is_suv, (vehicle.make, vehicle.model)
        if vehicle.generation is not None:
            assert vehicle.generation in match.generation_candidates, (vehicle.model, vehicle.generation)
        # Every matching dimension the comparables require is known (unknown would exclude the row).
        assert vehicle.fuel == Fuel.DIESEL
        assert vehicle.gearbox != Gearbox.UNKNOWN and vehicle.drive != Drive.UNKNOWN
        assert vehicle.mileage_km is not None and vehicle.engine_displacement_cm3 is not None
        assert row.currency == "EUR" and row.url is None
        assert "estimate" in row.provenance.casefold()
        observation = to_observation(row, EvidenceKind.OWNER_ESTIMATE)
        assert observation.market == "MK" and not observation.is_fixture
        ids.add(observation.id)
    assert len(ids) == len(parsed.observations)  # no duplicate rows


def test_confirmed_file_covers_the_nine_confirmed_models() -> None:
    parsed = parse_import((DATA / FILES[0]).read_bytes(), expected_kind=EvidenceKind.OWNER_ESTIMATE)
    models = {(r.vehicle.make, r.vehicle.model) for r in parsed.observations}
    assert models == {
        ("Renault", "Captur"),
        ("Nissan", "Qashqai"),
        ("Nissan", "Juke"),
        ("Kia", "Sportage"),
        ("Opel", "Mokka"),  # Mokka 2015 and Mokka X 2017 (the facelift)
        ("Dacia", "Duster"),
        ("Peugeot", "2008"),
    }
    prices = sorted(int(r.price) for r in parsed.observations)
    assert prices[0] == 7500 and prices[-1] == 9800


def _estimates() -> list[MarketObservation]:
    parsed = parse_import((DATA / FILES[0]).read_bytes(), expected_kind=EvidenceKind.OWNER_ESTIMATE)
    return [to_observation(row, EvidenceKind.OWNER_ESTIMATE) for row in parsed.observations]


def _foreign_listing(make: str, model: str, year: int, km: str, **extra: object) -> ComparableTarget:
    """A target as a foreign listing would give it (raw names, no generation, unknown facelift)."""
    data: dict[str, object] = {
        "make": make,
        "model": model,
        "fuel": Fuel.DIESEL,
        "gearbox": Gearbox.MANUAL,
        "drive": Drive.FWD,
        "year": year,
        "year_source": "first_registration",
        "mileage_km": Decimal(km),
        **extra,
    }
    return ComparableTarget.model_validate(data)


AS_OF = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CONFIG = load_business_config(REPO / "config")


def test_a_foreign_captur_is_valued_from_the_owner_estimates() -> None:
    target = _foreign_listing(
        "Renault", "Captur 1.5 dCi Energy", 2014, "175000", engine_displacement_cm3=1461
    )
    result = select_comparables(
        target, _estimates(), CONFIG, AS_OF, owner_estimates=True, taxonomy=default_taxonomy()
    )
    proceeds = proceeds_from_owner_estimates(result)
    assert proceeds.sample_size == 2
    assert (proceeds.low, proceeds.base, proceeds.high) == (
        Money.of("7500", "EUR"),
        Money.of("8250", "EUR"),
        Money.of("9000", "EUR"),
    )


def test_a_foreign_mokka_x_uses_the_mokka_x_estimate_first() -> None:
    target = _foreign_listing("OPEL", "Mokka X", 2017, "176000", engine_displacement_cm3=1598)
    result = select_comparables(
        target, _estimates(), CONFIG, AS_OF, owner_estimates=True, taxonomy=default_taxonomy()
    )
    proceeds = proceeds_from_owner_estimates(result)
    assert proceeds.sample_size == 1 and proceeds.base == Money.of("9800", "EUR")
    # The 2015 Mokka is two years older: no widening once the closest estimate is admitted.
    assert result.widening_steps == ()
