"""Property tests for the import-cost estimate on the shipped North Macedonian DRAFT.

The draft is unapproved; every estimate here is an indicative preview. Properties:
- the transport line is counted exactly once: customs value + VAT-only inland leg equals the
  price plus the whole transport amount, whatever the split;
- helpers are never counted: the indicative total equals the sum of the category amounts;
- a preview never carries a production total and is never production-ready;
- the estimate is deterministic.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from suv_deals.domain.costs import CostLine, load_cost_profile
from suv_deals.domain.enums import CostCategory, FxPurpose
from suv_deals.domain.import_estimate import (
    VehicleImportFacts,
    build_tax_inputs,
    estimate_from_rule_sets,
)
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import RuleSet, load_rule_set_file

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "config" / "tax_rules" / "drafts" / "mk_passenger_car_import_2026.10-draft.1.json"
PROFILE = REPO / "config" / "cost_profiles" / "default_unapproved.yaml"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
RATE = FxRate(
    base="EUR",
    quote="MKD",
    rate=Decimal("61.5337"),
    rate_date=date(2026, 10, 1),
    retrieved_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
    provider="customs monthly list (test input)",
    purpose=FxPurpose.CUSTOMS,
)

cents = st.integers(min_value=0, max_value=6_000_000).map(lambda c: Decimal(c).scaleb(-2))


@functools.cache
def draft() -> RuleSet:
    return load_rule_set_file(DRAFT)


@functools.cache
def transport() -> CostLine:
    return next(
        line for line in load_cost_profile(PROFILE).lines() if line.category == CostCategory.TRANSPORT
    )


def facts(price: Decimal, **kw: object) -> VehicleImportFacts:
    data: dict[str, object] = {
        "price": {"amount": price, "currency": "EUR"},
        "price_basis": "invoice",
        "declaration_date": date(2026, 10, 13),
        "fuel": "diesel",
        "co2": [{"cycle": "nedc", "g_km": "189"}],
        "co2_document": "coc",
    }
    data.update(kw)
    return VehicleImportFacts.model_validate(data)


@settings(max_examples=60, deadline=None)
@given(price=cents, inland_cents=st.integers(min_value=0, max_value=70_000))
def test_transport_is_counted_exactly_once(price: Decimal, inland_cents: int) -> None:
    inland = Decimal(inland_cents).scaleb(-2)
    split = {"transport_after_entry_eur": inland, "transport_after_entry_separately_invoiced": True}
    built = build_tax_inputs(facts(price, **split), transport=transport(), customs_rate=RATE, as_of=AS_OF)
    line = transport().base
    assert line is not None and built.inputs.customs_value is not None
    vat_only = sum((c.amount.amount for c in built.inputs.vat_incidental_costs or ()), Decimal(0))
    assert built.inputs.customs_value.amount + vat_only == price + line.amount
    assert vat_only == inland


@settings(max_examples=40, deadline=None)
@given(price=cents, co2=st.integers(min_value=0, max_value=400), fuel=st.sampled_from(["diesel", "petrol"]))
def test_indicative_total_is_the_sum_of_the_categories(price: Decimal, co2: int, fuel: str) -> None:
    est = estimate_from_rule_sets(
        [draft()],
        facts(price, fuel=fuel, co2=[{"cycle": "wltp", "g_km": str(co2)}]),
        transport=transport(),
        customs_rate=RATE,
        as_of=AS_OF,
    )
    assert est.mode == "indicative_preview" and not est.production_ready
    base = est.scenarios[0]
    assert base.complete and base.total is None and base.total_eur is None and base.landed_cost_eur is None
    assert base.indicative_total is not None
    amounts = [c.amount for c in base.categories if c.amount is not None]
    total = Money.zero("MKD")
    for amount in amounts:
        total = total + amount
    assert total == base.indicative_total
    assert est == estimate_from_rule_sets(
        [draft()],
        facts(price, fuel=fuel, co2=[{"cycle": "wltp", "g_km": str(co2)}]),
        transport=transport(),
        customs_rate=RATE,
        as_of=AS_OF,
    )
