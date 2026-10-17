"""Property tests for the tax engine 1.1 language extensions.

Every rule set and amount is SYNTHETIC (jurisdiction ``XX``; invented rates, thresholds
and FX rate). Properties:
- a contiguous law-style table (``over``/``up_to``) partitions the values: every value
  falls in exactly one row, the row with ``over < value <= up_to``;
- intermediate components are never counted: the total equals the sum of the payable
  components and of the category amounts, whatever the inputs;
- a bracket keyed on money picks the band of the converted key;
- the integer guard resolves exactly the integral values;
- explicit default values of the new fields never change a rule set's content hash.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from suv_deals.domain.enums import Co2Cycle, FxPurpose
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    Bracket,
    ComponentStatus,
    RuleSet,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    rule_set_problems,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tax" / "synthetic_rule_set.json"
AS_OF = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
#: SYNTHETIC customs rate: 1 EUR = 40 MKD.
RATE = FxRate(
    base="EUR",
    quote="MKD",
    rate=Decimal("40"),
    rate_date=date(2026, 5, 29),
    retrieved_at=datetime(2026, 5, 29, 12, tzinfo=UTC),
    provider="SYNTHETIC customs authority",
    purpose=FxPurpose.CUSTOMS,
)
#: SYNTHETIC EUR band edges and rates of the value band fee.
BANDS = (("1000", "0.01"), ("3000", "0.02"))

cents = st.integers(min_value=0, max_value=10_000_000).map(lambda c: Decimal(c).scaleb(-2))


def rule_doc() -> dict[str, Any]:
    def comp(comp_id: str, kind: str, category: str, **fields: Any) -> dict[str, Any]:
        label = f"SYNTHETIC {comp_id}"
        return {
            "id": comp_id,
            "kind": kind,
            "label": label,
            "category": category,
            "currency": "MKD",
            **fields,
        }

    (edge_1, rate_1), (edge_2, rate_2) = BANDS
    return {
        "rule_set_id": "SYNTHETIC-TEST-ONLY-xx-properties",
        "jurisdiction": "XX",
        "version": "synthetic-prop-1",
        "status": "unapproved",
        "valid_from": "2026-01-01",
        "currency": "MKD",
        "vehicle_categories": ["passenger_car"],
        "required_inputs": ["declaration_date", "customs_value", "customs_fx_rate", "co2_g_km", "co2_cycle"],
        "input_units": {"co2_g_km": "g/km"},
        "components": [
            comp(
                "value_mkd", "percentage", "import_duty", intermediate=True, rate="1", base=["customs_value"]
            ),
            comp(
                "duty", "percentage", "import_duty", depends_on=["value_mkd"], rate="0.07", base=["value_mkd"]
            ),
            comp(
                "band_fee",
                "bracket",
                "other_import_charges",
                depends_on=["value_mkd"],
                key_terms=["value_mkd"],
                key_currency="EUR",
                base=["value_mkd"],
                brackets=[
                    {"over": None, "up_to": edge_1, "amount": "0"},
                    {"over": edge_1, "up_to": edge_2, "rate_of_base": rate_1},
                    {"over": edge_2, "up_to": None, "rate_of_base": rate_2},
                ],
            ),
            comp(
                "co2_charge",
                "bracket",
                "motor_vehicle_tax",
                input="co2_g_km",
                integer_values_only=True,
                tables={"nedc": [{"lower_inclusive": "0", "upper_exclusive": None, "amount_per_unit": "3"}]},
            ),
            comp(
                "vat_base",
                "percentage",
                "import_vat",
                intermediate=True,
                depends_on=["value_mkd", "duty", "band_fee", "co2_charge"],
                rate="1",
                base=["value_mkd", "duty", "band_fee", "co2_charge"],
            ),
            comp("vat", "percentage", "import_vat", depends_on=["vat_base"], rate="0.15", base=["vat_base"]),
        ],
        "rounding_rules": {"component_default": {"quantum": "0.01", "mode": "half_up"}},
        "is_fixture": True,
    }


RULE = RuleSet.model_validate(rule_doc())


def inputs(value_eur: Decimal, co2: Decimal) -> TaxInputs:
    return TaxInputs(
        declaration_date=date(2026, 6, 1),
        customs_value=Money(amount=value_eur, currency="EUR"),
        customs_value_basis="SYNTHETIC transaction value",
        customs_fx_rate=RATE,
        co2_g_km=co2,
        co2_cycle=Co2Cycle.NEDC,
    )


def test_synthetic_rule_is_valid() -> None:
    assert rule_set_problems(RULE) == []


@settings(max_examples=120, deadline=None)
@given(
    edges=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=6, unique=True).map(
        sorted
    ),
    value=st.integers(min_value=0, max_value=1_200_000).map(lambda v: Decimal(v).scaleb(-2)),
)
def test_contiguous_law_table_partitions_values(edges: list[int], value: Decimal) -> None:
    bounds = [None, *(Decimal(e) for e in edges), None]
    rows = [Bracket(over=lower, up_to=upper, amount=Decimal(0)) for lower, upper in pairwise(bounds)]
    hits = [row for row in rows if row.contains(value)]
    assert len(hits) == 1
    row = hits[0]
    assert (row.over is None or value > row.over) and (row.up_to is None or value <= row.up_to)


@settings(max_examples=80, deadline=None)
@given(value=cents, co2=st.integers(min_value=0, max_value=600).map(Decimal))
def test_helpers_are_never_counted(value: Decimal, co2: Decimal) -> None:
    calc = calculate(RULE, inputs(value, co2), AS_OF)
    assert calc.complete and calc.total_import_cost is not None
    payable = sum((c.amount.amount for c in calc.components if c.amount and not c.intermediate), Decimal(0))
    assert calc.total_import_cost.amount == payable
    categories = sum(
        (t.amount.amount for t in calc.category_totals().values() if t.amount is not None), Decimal(0)
    )
    assert categories == payable
    helpers = {c.component_id: c.amount for c in calc.components if c.intermediate}
    assert set(helpers) == {"value_mkd", "vat_base"}
    assert all(h is not None for h in helpers.values())


@settings(max_examples=80, deadline=None)
@given(value=cents)
def test_money_key_selects_the_band_of_the_converted_key(value: Decimal) -> None:
    calc = calculate(RULE, inputs(value, Decimal(100)), AS_OF)
    fee = next(c for c in calc.components if c.component_id == "band_fee")
    helper = next(c for c in calc.components if c.component_id == "value_mkd")
    assert fee.amount is not None and helper.amount is not None
    key = helper.amount.amount / RATE.rate  # the helper is rounded before it feeds the key
    (edge_1, rate_1), (edge_2, rate_2) = ((Decimal(e), Decimal(r)) for e, r in BANDS)
    expected_rate = Decimal(0) if key <= edge_1 else rate_1 if key <= edge_2 else rate_2
    assert fee.unrounded_amount == helper.amount.amount * expected_rate


@settings(max_examples=80, deadline=None)
@given(co2=st.integers(min_value=0, max_value=60_000).map(lambda v: Decimal(v).scaleb(-2)))
def test_integer_guard_resolves_exactly_the_integral_values(co2: Decimal) -> None:
    calc = calculate(RULE, inputs(Decimal(1000), co2), AS_OF)
    charge = next(c for c in calc.components if c.component_id == "co2_charge")
    integral = co2 == co2.to_integral_value()
    assert (charge.status == ComponentStatus.RESOLVED) is integral
    assert calc.complete is integral
    if not integral:
        assert charge.warnings == (f"VALUE_NOT_INTEGER_NO_BAND_RULE:co2_g_km={co2}",)


@settings(max_examples=40, deadline=None)
@given(data=st.data())
def test_explicit_defaults_never_change_the_hash(data: st.DataObject) -> None:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stored = doc["sha256"]
    for comp in doc["components"]:
        if data.draw(st.booleans()):
            comp["intermediate"] = False
        if comp["kind"] != "bracket":
            continue
        for key, default in (("key_terms", None), ("key_currency", None), ("integer_values_only", False)):
            if data.draw(st.booleans()):
                comp[key] = default
        tables = comp.get("tables") or {"default": comp.get("brackets") or []}
        for rows in tables.values():
            for row in rows:
                for key in ("over", "up_to", "amount_per_unit"):
                    if data.draw(st.booleans()):
                        row[key] = None
    assert compute_rule_set_sha256(RuleSet.model_validate(doc)) == stored
