"""Recorded import values without a database (owner decision 2026-10-13: two separate values).

The calculator facts a recorded real purchase price and estimated customs value supply
(``import_values_repo.apply_recorded_values``), the price basis and valuation status per recorded
kind, the import-value categories staying out of the cost categories, the API request's
"at least one value" rule and the listing-level dependency change. Every value is SYNTHETIC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from suv_deals.api.schemas import ImportValuesRecordRequest
from suv_deals.domain.enums import CostCategory, CostLineStatus
from suv_deals.domain.import_estimate import PriceFact, VehicleImportFacts
from suv_deals.domain.money import Money
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import ValidationFailed
from suv_deals.persistence import valuation_repo
from suv_deals.persistence.import_values_repo import (
    IMPORT_VALUE_CATEGORIES,
    PRICE_BASIS,
    PURCHASE_STATUS,
    ImportValueRecord,
    ImportValues,
    apply_recorded_values,
)
from suv_deals.persistence.queries.import_costs import merge_facts

LISTING = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
AT = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def record(category: str, kind: str, amount: str, source: str) -> ImportValueRecord:
    return ImportValueRecord(
        id=uuid.uuid4(),
        listing_id=LISTING,
        category=category,
        kind=kind,
        amount=Money.of(amount, "EUR"),
        source=source,
        document_ref=None,
        supersedes_id=None,
        current=True,
        recorded_by=uuid.uuid4(),
        recorded_at=AT,
        is_fixture=False,
    )


def values(
    purchase: ImportValueRecord | None = None, customs: ImportValueRecord | None = None
) -> ImportValues:
    return ImportValues(listing_id=LISTING, purchase=purchase, customs_value=customs, history=())


ADVERTISED = VehicleImportFacts(
    price=PriceFact(amount=Decimal("3000"), currency="EUR"), price_basis="advertised"
)


def test_recorded_values_replace_the_advertised_price_and_add_the_estimate() -> None:
    paid = record("purchase", "actual", "2800.00", "seller invoice")
    schwacke = record("customs_value", "estimate", "4200.00", "SchwackeNet catalogue")
    facts, supplied = apply_recorded_values(ADVERTISED, values(paid, schwacke))
    assert facts.price == PriceFact(amount=Decimal("2800.00"), currency="EUR")
    assert facts.price_basis == "recorded_actual"
    assert facts.estimated_customs_value_eur == Decimal("4200.00")
    assert facts.estimated_customs_value_source == "SchwackeNet catalogue"
    assert supplied == {
        "price",
        "price_basis",
        "estimated_customs_value_eur",
        "estimated_customs_value_source",
    }
    only_estimate, supplied = apply_recorded_values(ADVERTISED, values(customs=schwacke))
    assert only_estimate.price == ADVERTISED.price and only_estimate.price_basis == "advertised"
    assert supplied == {"estimated_customs_value_eur", "estimated_customs_value_source"}
    unchanged, supplied = apply_recorded_values(ADVERTISED, values())
    assert unchanged is ADVERTISED and supplied == frozenset()


@pytest.mark.parametrize(
    ("kind", "basis", "status"),
    [
        ("actual", "recorded_actual", CostLineStatus.ACTUAL),
        ("quote", "recorded_quote", CostLineStatus.QUOTED),
        ("estimate", "recorded_estimate", CostLineStatus.ESTIMATED),
    ],
)
def test_each_recorded_kind_has_its_price_basis_and_valuation_status(
    kind: str, basis: str, status: CostLineStatus
) -> None:
    price = record("purchase", kind, "2800", "seller")
    assert price.price_basis == basis == PRICE_BASIS[kind]
    assert price.purchase_status == status == PURCHASE_STATUS[kind]
    facts, _ = apply_recorded_values(ADVERTISED, values(price))
    assert facts.price_basis == basis


@pytest.mark.parametrize("basis", ["invoice", "advertised"])
def test_a_lone_request_price_basis_never_relabels_the_recorded_purchase_price(basis: str) -> None:
    """Regression: ``merge_facts`` copied a request ``price_basis`` set WITHOUT a ``price`` over
    the recorded purchase price, so an owner's unpaid estimate read as an invoice price (sources
    price=recorded, price_basis=request). It is refused on ``price_basis``."""
    guess = record("purchase", "estimate", "2500", "owner guess")
    facts, supplied = apply_recorded_values(ADVERTISED, values(guess))
    with pytest.raises(ValidationFailed) as refused:
        merge_facts(facts, VehicleImportFacts.model_validate({"price_basis": basis}), recorded=supplied)
    assert refused.value.details == {"fields": ["price_basis"]}
    # With the request's own price the basis applies to that price (the recorded one is replaced).
    priced = VehicleImportFacts.model_validate(
        {"price": {"amount": "2500", "currency": "EUR"}, "price_basis": basis}
    )
    merged, sources = merge_facts(facts, priced, recorded=supplied)
    assert merged.price_basis == basis and sources["price"] == sources["price_basis"] == "request"
    # Unrelated request facts keep the recorded price AND its recorded kind together.
    merged, sources = merge_facts(
        facts, VehicleImportFacts.model_validate({"co2_document": "coc"}), recorded=supplied
    )
    assert merged.price_basis == "recorded_estimate"
    assert sources["price"] == sources["price_basis"] == "recorded"
    # A recorded customs value alone does not lock the listing's advertised price basis.
    estimate_only, supplied = apply_recorded_values(
        ADVERTISED, values(customs=record("customs_value", "estimate", "4200", "SchwackeNet catalogue"))
    )
    merged, sources = merge_facts(
        estimate_only, VehicleImportFacts.model_validate({"price_basis": basis}), recorded=supplied
    )
    assert merged.price_basis == basis and sources["price_basis"] == "request"


def test_describe_names_kind_amount_source_and_date() -> None:
    assert record("purchase", "quote", "2750", "agreed with the seller").describe() == (
        "recorded quote purchase price EUR 2750 (agreed with the seller, 2026-10-13)"
    )
    assert record("customs_value", "estimate", "4200", "SchwackeNet catalogue").describe() == (
        "recorded estimated customs value EUR 4200 (SchwackeNet catalogue, 2026-10-13)"
    )
    # No needless decimals (owner decision 2026-10-13), but a real fraction is quoted exactly:
    # the text names the figure the calculation uses, never a rounded one.
    assert record("purchase", "actual", "2800.00", "seller invoice").describe() == (
        "recorded actual purchase price EUR 2800 (seller invoice, 2026-10-13)"
    )
    assert record("purchase", "actual", "2800.50", "seller invoice").describe() == (
        "recorded actual purchase price EUR 2800.50 (seller invoice, 2026-10-13)"
    )


def test_import_values_are_not_cost_categories() -> None:
    """The cost profile and its term mapping never see a customs value; the purchase category
    exists but ``insert_cost_evidence`` refuses it (only ``import_values_repo`` writes it)."""
    assert IMPORT_VALUE_CATEGORIES == valuation_repo.IMPORT_VALUE_CATEGORIES == ("purchase", "customs_value")
    assert "customs_value" not in {c.value for c in CostCategory}
    assert CostCategory.PURCHASE.value == "purchase"


def test_the_request_records_at_least_one_value() -> None:
    with pytest.raises(ValidationError):
        ImportValuesRecordRequest.model_validate({"idempotency_key": "record-values-0001"})
    request = ImportValuesRecordRequest.model_validate(
        {
            "estimated_customs_value": {"amount_eur": "4200", "source": "SchwackeNet catalogue"},
            "idempotency_key": "record-values-0001",
        }
    )
    assert request.purchase_price is None and request.estimated_customs_value is not None
    with pytest.raises(ValidationError):  # EUR only; a float amount is refused
        ImportValuesRecordRequest.model_validate(
            {
                "purchase_price": {
                    "amount": "2800",
                    "currency": "MKD",
                    "kind": "actual",
                    "source": "invoice",
                },
                "idempotency_key": "record-values-0001",
            }
        )


def test_a_listing_level_dependency_change_is_a_valid_change() -> None:
    change = valuation_repo.DependencyChange(
        reason=InvalidationReason.COST_QUOTE, listing_ids=(LISTING,), detail="recorded import values changed"
    )
    assert change.listing_ids == (LISTING,)
    with pytest.raises(ValidationError):
        valuation_repo.DependencyChange(reason=InvalidationReason.COST_QUOTE)
