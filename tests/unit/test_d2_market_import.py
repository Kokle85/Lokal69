"""F2 (wave D2): the owner's MK market-evidence import file is validated before anything is stored.

Pure parsing tests (no database): evidence kinds stay distinct, no seller contact data passes,
exact decimals only, time zones required, the ad URL is required for an asking price, the ids are
content-derived (re-import is idempotent). SYNTHETIC data only.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from suv_deals.domain import market_import as mi
from suv_deals.domain.enums import Drive, EvidenceKind, Fuel
from suv_deals.errors import ValidationFailed

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "market" / "asking_prices_synthetic.json"


def _doc() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _problems(doc: dict[str, Any], kind: EvidenceKind = EvidenceKind.ASKING_PRICE) -> str:
    with pytest.raises(ValidationFailed) as caught:
        mi.parse_import(_bytes(doc), expected_kind=kind)
    details = caught.value.details or {}
    return caught.value.message + " " + " ".join(details.get("problems", []))


def test_the_synthetic_fixture_parses_into_mk_asking_prices() -> None:
    parsed = mi.parse_import(FIXTURE.read_bytes(), expected_kind=EvidenceKind.ASKING_PRICE)
    assert len(parsed.observations) == 4
    observation = mi.to_observation(parsed.observations[0], parsed.evidence_kind)
    assert observation.evidence_kind == EvidenceKind.ASKING_PRICE
    assert observation.market == "MK" and observation.is_fixture is False
    assert observation.amount is not None and observation.amount.amount == Decimal("8600")
    assert observation.observed_at == datetime(2026, 10, 6, 7, 30, tzinfo=UTC)
    vehicle = observation.vehicle
    assert (vehicle.make, vehicle.model) == ("Example", "Trail")
    assert (vehicle.fuel, vehicle.drive) == (Fuel.DIESEL, Drive.AWD)
    assert vehicle.first_registration.year == 2011 and vehicle.mileage_km == Decimal("170000")
    assert observation.source_key == "synthetic_mk_market"


def test_ids_are_content_derived_so_a_reimport_is_idempotent() -> None:
    first = mi.parse_import(FIXTURE.read_bytes(), expected_kind=EvidenceKind.ASKING_PRICE)
    again = mi.parse_import(FIXTURE.read_bytes(), expected_kind=EvidenceKind.ASKING_PRICE)
    ids = [mi.observation_id(r, first.evidence_kind) for r in first.observations]
    assert ids == [mi.observation_id(r, again.evidence_kind) for r in again.observations]
    assert len(set(ids)) == 4
    changed = _doc()
    changed["observations"][0]["price"] = "8601"
    other = mi.parse_import(_bytes(changed), expected_kind=EvidenceKind.ASKING_PRICE)
    assert mi.observation_id(other.observations[0], other.evidence_kind) != ids[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [("seller_email", "seller@dealer.example"), ("phone", "+389 70 123 456"), ("seller_name", "Somebody")],
)
def test_seller_contact_fields_are_refused(field: str, value: str) -> None:
    doc = _doc()
    doc["observations"][1][field] = value
    assert "observations.1" in _problems(doc)


@pytest.mark.parametrize(
    "provenance", ["call seller@dealer.example for details", "phone +389 70 123 456 after 18:00"]
)
def test_contact_data_in_free_text_is_refused(provenance: str) -> None:
    doc = _doc()
    doc["observations"][0]["provenance"] = provenance
    assert "contact data" in _problems(doc)


def test_url_with_user_information_is_refused() -> None:
    doc = _doc()
    doc["observations"][0]["url"] = "https://user:secret@mk-classifieds.example/ad/1"
    assert "user information" in _problems(doc)


def test_asking_price_needs_the_ad_url_and_owner_estimates_do_not() -> None:
    doc = _doc()
    del doc["observations"][2]["url"]
    assert "needs the ad url" in _problems(doc)
    estimate = copy.deepcopy(doc)
    estimate["evidence_kind"] = "owner_estimate"
    parsed = mi.parse_import(_bytes(estimate), expected_kind=EvidenceKind.OWNER_ESTIMATE)
    observation = mi.to_observation(parsed.observations[2], parsed.evidence_kind)
    assert observation.url is None and observation.source_key == mi.OWNER_ESTIMATE_SOURCE_KEY


def test_the_command_kind_must_equal_the_file_kind() -> None:
    assert "--evidence-kind is owner_estimate" in _problems(_doc(), EvidenceKind.OWNER_ESTIMATE)


def test_sales_cannot_be_imported() -> None:
    doc = _doc()
    doc["evidence_kind"] = "verified_sale"
    assert "only asking_price and owner_estimate" in _problems(doc, EvidenceKind.VERIFIED_SALE)


def test_exact_values_and_time_zones() -> None:
    floats = FIXTURE.read_text(encoding="utf-8").replace('"mileage_km": "170000"', '"mileage_km": 170000.5')
    with pytest.raises(ValidationFailed):
        mi.parse_import(floats.encode("utf-8"), expected_kind=EvidenceKind.ASKING_PRICE)
    naive = _doc()
    naive["observations"][0]["observed_at"] = "2026-10-06T09:30:00"
    assert "time zone" in _problems(naive)
    cents = _doc()
    cents["observations"][0]["price"] = "8600.001"
    assert "at most 2 decimals" in _problems(cents)
    zero = _doc()
    zero["observations"][0]["price"] = "0"
    assert "positive" in _problems(zero)


def test_size_and_row_bounds() -> None:
    with pytest.raises(ValidationFailed, match="exceeds"):
        mi.parse_import(b" " * (mi.MAX_IMPORT_BYTES + 1), expected_kind=EvidenceKind.ASKING_PRICE)
    many = _doc()
    many["observations"] = many["observations"] * (mi.MAX_IMPORT_ROWS // 4 + 1)
    assert "observations" in _problems(many)
    empty = _doc()
    empty["observations"] = []
    assert "observations" in _problems(empty)


def test_future_rows_are_reported() -> None:
    parsed = mi.parse_import(FIXTURE.read_bytes(), expected_kind=EvidenceKind.ASKING_PRICE)
    assert mi.future_rows(parsed, datetime(2026, 10, 6, 7, 45, tzinfo=UTC)) == [1, 2, 3]
    assert mi.future_rows(parsed, datetime(2026, 10, 10, tzinfo=UTC)) == []
