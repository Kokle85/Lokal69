"""Tax engine 1.1 language extensions and the indicative-preview selector.

E1 intermediate components, E2 brackets keyed on money, E3 law-style ``over``/``up_to``
bounds, E4 whole-value ``amount_per_unit``, E5 the integer guard, E6 the
``vat_incidental_costs_total`` input, hash stability and ``select_preview_rule_set``.

Every rule set here is SYNTHETIC: jurisdiction ``XX`` is a user-assigned code, and every
rate, threshold, coefficient and FX rate is invented to exercise the engine. None is a
real tax rate.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.costs import tax_cost_lines
from suv_deals.domain.enums import Co2Cycle, CostCategory, CostLineStatus, Fuel, FxPurpose, TaxRuleStatus
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    ENGINE_VERSION,
    INPUT_REGISTRY,
    Bracket,
    ComponentStatus,
    IncludedCost,
    InputKind,
    ReviewRecord,
    RuleSelection,
    RuleSet,
    RuleSource,
    TaxCalculation,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    load_rule_set_file,
    parse_rule_set_json,
    rule_set_problems,
    seal_rule_set,
    select_preview_rule_set,
    select_rule_set,
    transition_rule_set,
    validate_rule_set,
)
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json"
#: The fixture's stored hash, computed before the 1.1 language fields existed.
FIXTURE_SHA256 = "be2412a15136f280fecf537efdd1699b11f899f6ff326936135786f57e8e9659"
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
DECL = date(2026, 6, 1)
CATEGORY = "passenger_car"
SYNTHETIC_SOURCE = RuleSource(
    url="https://example.invalid/SYNTHETIC-legal-source",
    title="SYNTHETIC source document (test only)",
    retrieved_at=datetime(2026, 5, 1, tzinfo=UTC),
    sha256="0" * 64,
)
VAT_BASE_TERMS = ["value_helper", "duty", "value_band_fee", "co2_charge", "vat_incidental_costs_total"]


# --------------------------------------------------------------------------- helpers


def ext_doc() -> dict[str, Any]:
    """SYNTHETIC rule set using every 1.1 extension.

    value_helper (helper) = customs value in MKD; duty = 10 % of it; value_band_fee keyed
    on the helper in EUR with law-style bands; co2_charge = whole CO2 value x per-unit
    amount (integers only); vat_base (helper) = helper + charges + VAT-only costs;
    vat = 20 % of vat_base.
    """
    return {
        "rule_set_id": "SYNTHETIC-TEST-ONLY-xx-extensions",
        "jurisdiction": "XX",
        "version": "synthetic-ext-1",
        "status": "unapproved",
        "valid_from": "2026-01-01",
        "valid_to": "2027-01-01",
        "currency": "MKD",
        "vehicle_categories": [CATEGORY],
        "description": "SYNTHETIC TEST RULE SET. Every rate and threshold is invented.",
        "sources": [],
        "approved_by": None,
        "approved_at": None,
        "required_inputs": [
            "declaration_date",
            "customs_value",
            "customs_fx_rate",
            "co2_g_km",
            "co2_cycle",
            "vat_incidental_costs_total",
        ],
        "optional_inputs": [],
        "input_units": {"co2_g_km": "g/km"},
        "components": [
            {
                "id": "value_helper",
                "kind": "percentage",
                "label": "SYNTHETIC customs value in MKD (helper)",
                "category": "import_duty",
                "currency": "MKD",
                "intermediate": True,
                "rate": "1",
                "base": ["customs_value"],
            },
            {
                "id": "duty",
                "kind": "percentage",
                "label": "SYNTHETIC duty",
                "category": "import_duty",
                "currency": "MKD",
                "depends_on": ["value_helper"],
                "rate": "0.10",
                "base": ["value_helper"],
            },
            {
                "id": "value_band_fee",
                "kind": "bracket",
                "label": "SYNTHETIC value band fee keyed on the EUR value",
                "category": "other_import_charges",
                "currency": "MKD",
                "depends_on": ["value_helper"],
                "key_terms": ["value_helper"],
                "key_currency": "EUR",
                "base": ["value_helper"],
                "brackets": [
                    {"over": None, "up_to": "1000", "amount": "0"},
                    {"over": "1000", "up_to": "2000", "rate_of_base": "0.01"},
                    {"over": "2000", "up_to": None, "rate_of_base": "0.02"},
                ],
            },
            {
                "id": "co2_charge",
                "kind": "bracket",
                "label": "SYNTHETIC CO2 charge (whole value x amount)",
                "category": "motor_vehicle_tax",
                "currency": "MKD",
                "input": "co2_g_km",
                "integer_values_only": True,
                "tables": {
                    "wltp": [
                        {"lower_inclusive": "0", "upper_exclusive": "100", "amount_per_unit": "2"},
                        {"lower_inclusive": "100", "upper_exclusive": None, "amount_per_unit": "7"},
                    ]
                },
            },
            {
                "id": "vat_base",
                "kind": "percentage",
                "label": "SYNTHETIC VAT base (helper)",
                "category": "import_vat",
                "currency": "MKD",
                "intermediate": True,
                "depends_on": ["value_helper", "duty", "value_band_fee", "co2_charge"],
                "rate": "1",
                "base": VAT_BASE_TERMS,
            },
            {
                "id": "vat",
                "kind": "percentage",
                "label": "SYNTHETIC import VAT",
                "category": "import_vat",
                "currency": "MKD",
                "depends_on": ["vat_base"],
                "rate": "0.20",
                "base": ["vat_base"],
            },
        ],
        "rounding_rules": {"component_default": {"quantum": "0.01", "mode": "half_up"}, "total": None},
        "missing_input_behavior": "return_incomplete",
        "sha256": None,
        "is_fixture": True,
    }


def component(doc: dict[str, Any], comp_id: str) -> dict[str, Any]:
    return next(c for c in doc["components"] if c["id"] == comp_id)


def build(mutate: Callable[[dict[str, Any]], None] | None = None) -> RuleSet:
    doc = ext_doc()
    if mutate is not None:
        mutate(doc)
    return RuleSet.model_validate(doc)


def valid(mutate: Callable[[dict[str, Any]], None] | None = None) -> RuleSet:
    rule_set = build(mutate)
    validate_rule_set(rule_set)
    return rule_set


def problems_of(mutate: Callable[[dict[str, Any]], None]) -> list[str]:
    return rule_set_problems(build(mutate))


def customs_rate(**kw: Any) -> FxRate:
    values: dict[str, Any] = {
        "base": "EUR",
        "quote": "MKD",
        "rate": Decimal("50"),
        "rate_date": date(2026, 5, 29),
        "retrieved_at": datetime(2026, 5, 29, 12, tzinfo=UTC),
        "provider": "SYNTHETIC customs authority",
        "purpose": FxPurpose.CUSTOMS,
    }
    values.update(kw)
    return FxRate(**values)


def inputs(**overrides: Any) -> TaxInputs:
    values: dict[str, Any] = {
        "declaration_date": DECL,
        "jurisdiction": "XX",
        "customs_value": Money.of("1500.00", "EUR"),
        "customs_value_basis": "SYNTHETIC transaction value",
        "customs_fx_rate": customs_rate(),
        "co2_g_km": Decimal("120"),
        "co2_cycle": Co2Cycle.WLTP,
        "vat_incidental_costs": (),
    }
    values.update(overrides)
    return TaxInputs(**values)


def amounts(calc: TaxCalculation) -> dict[str, Decimal | None]:
    return {c.component_id: (None if c.amount is None else c.amount.amount) for c in calc.components}


def result(calc: TaxCalculation, comp_id: str) -> Any:
    return next(c for c in calc.components if c.component_id == comp_id)


def amount_of(rule: RuleSet, comp_id: str, **overrides: Any) -> Decimal | None:
    return amounts(calculate(rule, inputs(**overrides), T0))[comp_id]


# --------------------------------------------------------------------------- E1 intermediate components


def test_helpers_feed_consumers_and_are_never_counted() -> None:
    calc = calculate(valid(), inputs(), T0)
    assert amounts(calc) == {
        "value_helper": Decimal("75000.00"),  # 1500 EUR x 50
        "duty": Decimal("7500.00"),
        "value_band_fee": Decimal("750.00"),  # key 1500 EUR in (1000, 2000] -> 1 %
        "co2_charge": Decimal("840.00"),  # 120 x 7, the whole value
        "vat_base": Decimal("84090.00"),
        "vat": Decimal("16818.00"),
    }
    assert {c.component_id for c in calc.components if c.intermediate} == {"value_helper", "vat_base"}
    assert calc.complete
    assert calc.total_import_cost == Money.of("25908.00", "MKD")  # helpers excluded
    totals = calc.category_totals()
    assert totals[CostCategory.IMPORT_DUTY].amount == Money.of("7500.00", "MKD")
    assert totals[CostCategory.IMPORT_DUTY].component_ids == ("value_helper", "duty")
    assert totals[CostCategory.IMPORT_VAT].amount == Money.of("16818.00", "MKD")
    assert result(calc, "vat").inputs_used["vat_base"] == "84090.00 MKD"


def test_tax_cost_lines_exclude_helpers() -> None:
    lines = {line.category: line for line in tax_cost_lines(calculate(valid(), inputs(), T0))}
    assert len(lines) == 4
    assert {c: (line.status, line.base) for c, line in lines.items()} == {
        CostCategory.IMPORT_DUTY: (CostLineStatus.ESTIMATED, Money.of("7500.00", "MKD")),
        CostCategory.IMPORT_VAT: (CostLineStatus.ESTIMATED, Money.of("16818.00", "MKD")),
        CostCategory.MOTOR_VEHICLE_TAX: (CostLineStatus.ESTIMATED, Money.of("840.00", "MKD")),
        CostCategory.OTHER_IMPORT_CHARGES: (CostLineStatus.ESTIMATED, Money.of("750.00", "MKD")),
    }
    assert not any(line.rule_supported for line in lines.values())  # fixture, never production


def test_unknown_helper_makes_consumers_and_category_unknown() -> None:
    calc = calculate(valid(), inputs(customs_value=None), T0)
    assert result(calc, "value_helper").status == ComponentStatus.UNKNOWN
    assert set(calc.unknown_components) == {"value_helper", "duty", "value_band_fee", "vat_base", "vat"}
    assert not calc.complete and calc.total_import_cost is None
    assert calc.known_subtotal == Money.of("840.00", "MKD")  # only the CO2 charge is payable and known
    assert calc.category_totals()[CostCategory.IMPORT_DUTY].status == ComponentStatus.UNKNOWN


def _mini(fee_applies: str) -> dict[str, Any]:
    """SYNTHETIC: a helper sharing its category with one payable fee."""
    return {
        **{k: v for k, v in ext_doc().items() if k != "components"},
        "required_inputs": ["declaration_date", "co2_g_km", "co2_cycle", "fuel"],
        "components": [
            {
                "id": "fee",
                "kind": "fixed",
                "label": "SYNTHETIC diesel-only fee",
                "category": "other_import_charges",
                "currency": "MKD",
                "applies_when": [{"input": "fuel", "op": "eq", "value": fee_applies}],
                "amount": "100",
            },
            {
                "id": "co2_helper",
                "kind": "bracket",
                "label": "SYNTHETIC CO2 helper",
                "category": "other_import_charges",
                "currency": "MKD",
                "intermediate": True,
                "input": "co2_g_km",
                "tables": {
                    "wltp": [{"lower_inclusive": "0", "upper_exclusive": None, "amount_per_unit": "3"}]
                },
            },
            {
                "id": "co2_tax",
                "kind": "percentage",
                "label": "SYNTHETIC CO2 tax, half of the helper",
                "category": "motor_vehicle_tax",
                "currency": "MKD",
                "depends_on": ["co2_helper"],
                "rate": "0.5",
                "base": ["co2_helper"],
            },
        ],
    }


def test_category_with_unknown_helper_is_never_not_applicable() -> None:
    rule = RuleSet.model_validate(_mini("diesel"))
    validate_rule_set(rule)
    petrol = {"fuel": Fuel.PETROL, "co2_cycle": Co2Cycle.WLTP}
    unknown = calculate(rule, TaxInputs(declaration_date=DECL, co2_g_km=None, **petrol), T0)
    assert result(unknown, "fee").status == ComponentStatus.NOT_APPLICABLE
    assert unknown.category_totals()[CostCategory.OTHER_IMPORT_CHARGES].status == ComponentStatus.UNKNOWN
    known = calculate(rule, TaxInputs(declaration_date=DECL, co2_g_km=Decimal("10"), **petrol), T0)
    other = known.category_totals()[CostCategory.OTHER_IMPORT_CHARGES]
    assert other.status == ComponentStatus.NOT_APPLICABLE and other.amount is None  # helper never counted
    assert known.total_import_cost == Money.of("15.00", "MKD")  # 10 x 3 x 0.5
    lines = {line.category: line.status for line in tax_cost_lines(known)}
    assert lines[CostCategory.OTHER_IMPORT_CHARGES] == CostLineStatus.NOT_APPLICABLE


def _orphan_helper(d: dict[str, Any]) -> None:
    d["components"].append(
        {
            "id": "orphan",
            "kind": "fixed",
            "label": "SYNTHETIC helper nobody uses",
            "category": "other_import_charges",
            "currency": "MKD",
            "intermediate": True,
            "amount": "1",
        }
    )


def _helper_only_in_depends_on(d: dict[str, Any]) -> None:
    _orphan_helper(d)
    d["components"].append(
        {
            "id": "later_fee",
            "kind": "fixed",
            "label": "SYNTHETIC fee listing the helper only in depends_on",
            "category": "other_import_charges",
            "currency": "MKD",
            "depends_on": ["orphan"],
            "amount": "1",
        }
    )


def _helper_group(*, referenced: tuple[str, ...], second_intermediate: bool = True) -> Callable[..., None]:
    def mutate(d: dict[str, Any]) -> None:
        for comp_id, fuel_op, intermediate in (("h_a", "eq", True), ("h_b", "ne", second_intermediate)):
            d["components"].append(
                {
                    "id": comp_id,
                    "kind": "fixed",
                    "label": f"SYNTHETIC grouped helper {comp_id}",
                    "category": "other_import_charges",
                    "currency": "MKD",
                    "intermediate": intermediate,
                    "alternative_group": "helpers",
                    "applies_when": [{"input": "fuel", "op": fuel_op, "value": "diesel"}],
                    "amount": "5",
                }
            )
        d["optional_inputs"].append("fuel")
        d["components"].append(
            {
                "id": "group_user",
                "kind": "percentage",
                "label": "SYNTHETIC consumer of the group",
                "category": "other_import_charges",
                "currency": "MKD",
                "depends_on": list(referenced),
                "rate": "1",
                "base": list(referenced) or ["customs_value"],
            }
        )

    return mutate


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_orphan_helper, "component 'orphan': an intermediate component must be a base or key term"),
        (
            _helper_only_in_depends_on,
            "component 'orphan': an intermediate component must be a base or key term",
        ),
        (_helper_group(referenced=(), second_intermediate=True), "component 'h_a': an intermediate"),
        (_helper_group(referenced=("h_a",), second_intermediate=False), "mixes intermediate and normal"),
    ],
)
def test_intermediate_validation(mutate: Callable[[dict[str, Any]], None], needle: str) -> None:
    problems = problems_of(mutate)
    assert any(needle in p for p in problems), problems
    with pytest.raises(ValidationFailed):
        validate_rule_set(build(mutate))


def test_unreferenced_group_member_is_accepted_for_coverage() -> None:
    # Only h_a feeds the consumer; h_b exists to give the group full coverage.
    rule = valid(_helper_group(referenced=("h_a",)))
    diesel = calculate(rule, inputs(fuel=Fuel.DIESEL), T0)
    assert amounts(diesel)["group_user"] == Decimal("5.00")
    petrol = calculate(rule, inputs(fuel=Fuel.PETROL), T0)
    assert result(petrol, "h_b").status == ComponentStatus.RESOLVED and result(petrol, "h_b").intermediate
    assert amounts(petrol)["group_user"] == Decimal("0")  # h_a not applicable contributes nothing
    assert petrol.total_import_cost == calculate(valid(), inputs(), T0).total_import_cost


def test_approved_rule_set_needs_a_non_intermediate_component() -> None:
    def all_helpers(d: dict[str, Any]) -> None:
        d.update(status="approved", is_fixture=False)
        for comp in d["components"]:
            comp["intermediate"] = True

    problems = problems_of(all_helpers)
    assert any("requires at least one non-intermediate component" in p for p in problems), problems
    for status in ("draft", "approved"):

        def normal(d: dict[str, Any], status: str = status) -> None:
            d.update(status=status, is_fixture=False)

        assert not any("non-intermediate" in p for p in problems_of(normal))


# --------------------------------------------------------------------------- E2 brackets keyed on money


@pytest.mark.parametrize(
    ("value_eur", "fee", "band"),
    [
        ("0.00", "0.00", "(open, 1000]"),
        ("1000.00", "0.00", "(open, 1000]"),  # upper bound inclusive
        ("1000.02", "500.01", "(1000, 2000]"),  # key 1000.02 EUR -> 1 % of 50001.00
        ("2000.00", "1000.00", "(1000, 2000]"),
        ("2000.02", "2000.02", "(2000, open)"),  # 2 % of 100001.00
    ],
)
def test_money_key_in_rule_currency_against_eur_bands(value_eur: str, fee: str, band: str) -> None:
    calc = calculate(valid(), inputs(customs_value=Money.of(value_eur, "EUR")), T0)
    fee_result = result(calc, "value_band_fee")
    assert fee_result.amount == Money.of(fee, "MKD")
    assert fee_result.inputs_used["bracket"] == band
    key = fee_result.inputs_used["key"]
    assert key.endswith("EUR (customs EUR/MKD=50 2026-05-29 SYNTHETIC customs authority)"), key
    assert "E+" not in key
    assert calc.customs_fx_rate == customs_rate()


def test_money_key_in_rule_currency_needs_no_rate() -> None:
    def mkd_key(d: dict[str, Any]) -> None:
        comp = component(d, "value_band_fee")
        del comp["key_currency"]
        comp["brackets"] = [
            {"over": None, "up_to": "50000", "amount": "0"},
            {"over": "50000", "up_to": None, "amount": "9"},
        ]

    calc = calculate(valid(mkd_key), inputs(customs_value=Money.of("50001.00", "MKD")), T0)
    fee = result(calc, "value_band_fee")
    assert fee.amount == Money.of("9.00", "MKD")
    assert fee.inputs_used["key"] == "50001.00 MKD"
    assert calc.customs_fx_rate is None  # nothing was converted


@pytest.mark.parametrize(
    ("overrides", "warning"),
    [
        ({"customs_fx_rate": None}, "CUSTOMS_FX_RATE_MISSING:value_band_fee:key"),
        ({"customs_fx_rate": customs_rate(purpose=FxPurpose.REFERENCE)}, "CUSTOMS_FX_RATE_WRONG_PURPOSE"),
        ({"customs_fx_rate": customs_rate(base="CHF")}, "CUSTOMS_FX_RATE_PAIR_MISMATCH"),
        ({"customs_fx_rate": customs_rate(rate_date=date(2026, 6, 2))}, "CUSTOMS_FX_RATE_AFTER_DECLARATION"),
        ({"customs_fx_valid_from": date(2026, 6, 2)}, "CUSTOMS_FX_RATE_NOT_EFFECTIVE"),
        ({"customs_fx_valid_to": date(2026, 5, 31)}, "CUSTOMS_FX_RATE_NOT_EFFECTIVE"),
    ],
)
def test_money_key_conversion_uses_only_a_valid_customs_rate(overrides: dict[str, Any], warning: str) -> None:
    # Customs value in MKD: the helper resolves, so only the key conversion needs the rate.
    calc = calculate(valid(), inputs(customs_value=Money.of("75000.00", "MKD"), **overrides), T0)
    assert result(calc, "value_helper").status == ComponentStatus.RESOLVED
    fee = result(calc, "value_band_fee")
    assert fee.status == ComponentStatus.UNKNOWN
    assert any(warning in w for w in fee.warnings), fee.warnings
    assert "customs_fx_rate" in fee.missing_inputs
    assert {"value_band_fee", "vat_base", "vat"} <= set(calc.unknown_components)
    assert not calc.complete and calc.customs_fx_rate is None


def _both_input_and_key(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["input"] = "co2_g_km"


def _neither_input_nor_key(d: dict[str, Any]) -> None:
    del component(d, "value_band_fee")["key_terms"]


def _empty_key_terms(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["key_terms"] = []


@pytest.mark.parametrize("mutate", [_both_input_and_key, _neither_input_nor_key, _empty_key_terms])
def test_key_shape_is_structural(mutate: Callable[[dict[str, Any]], None]) -> None:
    with pytest.raises(ValidationError):
        build(mutate)


def _key_with_tables(d: dict[str, Any]) -> None:
    comp = component(d, "value_band_fee")
    comp["tables"] = {"wltp": comp.pop("brackets")}


def _key_integer_guard(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["integer_values_only"] = True


def _key_per_unit(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["brackets"][0] = {"over": None, "up_to": "1000", "amount_per_unit": "1"}


def _key_per_unit_above_lower(d: dict[str, Any]) -> None:
    row = {"over": None, "up_to": "1000", "amount_per_unit_above_lower": "1"}
    component(d, "value_band_fee")["brackets"][0] = row


def _key_currency_without_key(d: dict[str, Any]) -> None:
    component(d, "co2_charge")["key_currency"] = "EUR"


def _key_currency_without_customs_rate(d: dict[str, Any]) -> None:
    d["required_inputs"].remove("customs_fx_rate")


def _unsupported_key_currency(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["key_currency"] = "XAU"


def _non_money_key(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["key_terms"] = ["co2_g_km"]


def _key_not_in_depends_on(d: dict[str, Any]) -> None:
    component(d, "value_band_fee")["key_terms"] = ["duty"]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_key_with_tables, "cannot use per-cycle tables"),
        (_key_integer_guard, "integer_values_only is only valid for a bracket on a numeric input"),
        (_key_per_unit, "allows only amount or rate_of_base"),
        (_key_per_unit_above_lower, "allows only amount or rate_of_base"),
        (_key_currency_without_key, "key_currency is only valid with key_terms"),
        (_key_currency_without_customs_rate, "customs_fx_rate must be declared"),
        (_unsupported_key_currency, "not a supported currency"),
        (_non_money_key, "key term 'co2_g_km' is not a money input"),
        (_key_not_in_depends_on, "key term 'duty' must also be listed in depends_on"),
    ],
)
def test_key_validation_problems(mutate: Callable[[dict[str, Any]], None], needle: str) -> None:
    problems = problems_of(mutate)
    assert any(needle in p for p in problems), problems


# --------------------------------------------------------------------------- E3 law-style bounds


def test_law_bounds_exclusive_lower_inclusive_upper() -> None:
    first = Bracket(over=None, up_to=Decimal("10000"), amount=Decimal("0"))
    second = Bracket(over=Decimal("10000"), up_to=Decimal("12000"), amount=Decimal("0"))
    last = Bracket(over=Decimal("12000"), up_to=None, amount=Decimal("0"))
    assert first.contains(Decimal("0")) and first.contains(Decimal("10000"))
    assert not second.contains(Decimal("10000"))
    assert second.contains(Decimal("10000.000001")) and second.contains(Decimal("12000"))
    assert not second.contains(Decimal("12000.000001")) and last.contains(Decimal("12000.000001"))
    assert (first.display(), second.display(), last.display()) == (
        "(open, 10000]",
        "(10000, 12000]",
        "(12000, open)",
    )
    classic = Bracket(lower_inclusive=Decimal("0"), upper_exclusive=Decimal("5"), amount=Decimal("1"))
    assert classic.display() == "[0, 5)" and not classic.law_form and first.law_form


@pytest.mark.parametrize(
    "row",
    [
        {"lower_inclusive": "0", "over": "1", "amount": "0"},
        {"lower_inclusive": "0", "up_to": "1", "amount": "0"},
        {"upper_exclusive": "5", "amount": "0"},
        {"over": "5", "up_to": "5", "amount": "0"},
        {"over": "6", "up_to": "5", "amount": "0"},
        {"over": "-1", "up_to": "5", "amount": "0"},
        {"over": "0", "up_to": "5", "amount": "1", "amount_per_unit": "1"},
        {"over": "0", "up_to": "5", "amount_per_unit": "-1"},
    ],
)
def test_invalid_law_rows_rejected(row: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Bracket.model_validate(row)


def _rows(*rows: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        component(d, "value_band_fee")["brackets"] = [dict(r, amount="0") for r in rows]

    return mutate


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_rows({"over": None, "up_to": "10"}, {"lower_inclusive": "10"}), "rows mix"),
        (
            _rows({"over": None, "up_to": "10"}, {"over": None, "up_to": "20"}),
            "only the first bracket may have no",
        ),
        (
            _rows({"over": None, "up_to": None}, {"over": "10", "up_to": None}),
            "only the last bracket may be open",
        ),
        (
            _rows(
                {"over": None, "up_to": "10"}, {"over": "20", "up_to": "30"}, {"over": "10", "up_to": "20"}
            ),
            "sorted",
        ),
        (_rows({"over": None, "up_to": "10"}, {"over": "5", "up_to": None}), "overlap"),
        (_rows({"over": None, "up_to": "10"}, {"over": "11", "up_to": None}), "not contiguous"),
    ],
)
def test_law_table_validation(mutate: Callable[[dict[str, Any]], None], needle: str) -> None:
    problems = problems_of(mutate)
    assert any(needle in p for p in problems), problems


def test_law_table_gap_allowed_when_declared_non_contiguous() -> None:
    def gap(d: dict[str, Any]) -> None:
        _rows({"over": None, "up_to": "1000"}, {"over": "2000", "up_to": None})(d)
        component(d, "value_band_fee")["contiguous"] = False

    calc = calculate(valid(gap), inputs(), T0)  # key 1500 EUR lies in the gap
    fee = result(calc, "value_band_fee")
    assert fee.status == ComponentStatus.UNKNOWN
    assert any(w.startswith("VALUE_OUTSIDE_BRACKETS:key=1500") for w in fee.warnings), fee.warnings


def test_law_rows_on_a_numeric_input_with_marginal_outcome() -> None:
    def age_fee(d: dict[str, Any]) -> None:
        d["optional_inputs"].append("vehicle_age_years")
        d["input_units"]["vehicle_age_years"] = "years"
        d["components"].append(
            {
                "id": "age_fee",
                "kind": "bracket",
                "label": "SYNTHETIC age fee, marginal above the band floor",
                "category": "other_import_charges",
                "currency": "MKD",
                "input": "vehicle_age_years",
                "brackets": [
                    {"over": None, "up_to": "5", "amount_per_unit_above_lower": "10"},
                    {"over": "5", "up_to": None, "amount_per_unit_above_lower": "100"},
                ],
            }
        )

    rule = valid(age_fee)
    assert amount_of(rule, "age_fee", vehicle_age_years=Decimal("5")) == Decimal("50.00")
    assert amount_of(rule, "age_fee", vehicle_age_years=Decimal("7")) == Decimal("200.00")


# --------------------------------------------------------------------------- E4 whole-value per-unit amount


def test_amount_per_unit_multiplies_the_whole_value() -> None:
    whole = calculate(valid(), inputs(co2_g_km=Decimal("120")), T0)
    assert amounts(whole)["co2_charge"] == Decimal("840.00")  # 120 x 7
    assert result(whole, "co2_charge").inputs_used["bracket"] == "[100, open)"

    def marginal(d: dict[str, Any]) -> None:
        row = component(d, "co2_charge")["tables"]["wltp"][1]
        row["amount_per_unit_above_lower"] = row.pop("amount_per_unit")

    # The marginal outcome gives (120 - 100) x 7 instead: a different amount.
    assert amount_of(valid(marginal), "co2_charge", co2_g_km=Decimal("120")) == Decimal("140.00")
    assert amount_of(valid(), "co2_charge", co2_g_km=Decimal("99")) == Decimal("198.00")  # 99 x 2


# --------------------------------------------------------------------------- E5 integer guard


def test_non_integer_value_is_unknown_under_the_integer_guard() -> None:
    calc = calculate(valid(), inputs(co2_g_km=Decimal("120.5")), T0)
    co2 = result(calc, "co2_charge")
    assert co2.status == ComponentStatus.UNKNOWN and co2.amount is None
    assert co2.warnings == ("VALUE_NOT_INTEGER_NO_BAND_RULE:co2_g_km=120.5",)
    assert {"co2_charge", "vat_base", "vat"} <= set(calc.unknown_components)
    assert not calc.complete and calc.total_import_cost is None
    assert amount_of(valid(), "co2_charge", co2_g_km=Decimal("120.0")) == Decimal("840.00")

    def no_guard(d: dict[str, Any]) -> None:
        component(d, "co2_charge")["integer_values_only"] = False

    assert amount_of(valid(no_guard), "co2_charge", co2_g_km=Decimal("120.5")) == Decimal("843.50")


# --------------------------------------------------------------------------- E6 VAT-only incidental costs


def test_vat_incidental_costs_input() -> None:
    spec = INPUT_REGISTRY["vat_incidental_costs_total"]
    assert spec.kind == InputKind.MONEY
    unknown = calculate(valid(), inputs(vat_incidental_costs=None), T0)
    assert result(unknown, "vat_base").status == ComponentStatus.UNKNOWN
    assert unknown.missing_inputs == ("vat_incidental_costs_total",)
    assert not unknown.complete
    none = calculate(valid(), inputs(vat_incidental_costs=()), T0)
    assert none.complete and result(none, "vat_base").inputs_used["vat_incidental_costs_total"] == "0 MKD ()"
    cost = IncludedCost(
        label="SYNTHETIC inland leg", amount=Money.of("10.00", "EUR"), legal_basis="SYNTHETIC VAT article"
    )
    converted = calculate(valid(), inputs(vat_incidental_costs=(cost,)), T0)
    assert amounts(converted)["vat_base"] == Decimal("84590.00")  # + 10 EUR x 50
    assert amounts(converted)["vat"] == Decimal("16918.00")
    assert amounts(converted)["duty"] == Decimal("7500.00")  # never in the customs value
    used = result(converted, "vat_base").inputs_used["vat_incidental_costs_total"]
    assert used.startswith("500.00 MKD (10.00 EUR -> 500.00 MKD (customs EUR/MKD=50"), used
    chf = cost.model_copy(update={"amount": Money.of("10.00", "CHF")})
    mismatch = calculate(valid(), inputs(vat_incidental_costs=(chf,)), T0)
    assert result(mismatch, "vat_base").status == ComponentStatus.UNKNOWN
    assert any("CUSTOMS_FX_RATE_PAIR_MISMATCH" in w for w in result(mismatch, "vat_base").warnings)


def test_vat_incidental_costs_bounds_and_money_predicates() -> None:
    cost = IncludedCost(label="x", amount=Money.of("1", "MKD"), legal_basis="SYNTHETIC basis")
    assert len(inputs(vat_incidental_costs=(cost,) * 20).vat_incidental_costs or ()) == 20
    with pytest.raises(ValidationError):
        inputs(vat_incidental_costs=(cost,) * 21)

    def predicate(d: dict[str, Any]) -> None:
        component(d, "duty")["applies_when"] = [
            {"input": "vat_incidental_costs_total", "op": "gt", "value": "1"}
        ]

    assert any("cannot test money" in p for p in problems_of(predicate))


# --------------------------------------------------------------------------- hashing and version


def test_existing_fixture_hash_is_unchanged() -> None:
    rule = load_rule_set_file(FIXTURE)
    assert rule.sha256 == FIXTURE_SHA256 == compute_rule_set_sha256(rule)


def test_explicit_extension_defaults_do_not_change_the_hash() -> None:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for comp in doc["components"]:
        comp["intermediate"] = False
        if comp["kind"] == "bracket":
            comp.update(key_terms=None, key_currency=None, integer_values_only=False)
            tables = comp.get("tables") or {"default": comp.get("brackets") or []}
            for rows in tables.values():
                for row in rows:
                    row.update(over=None, up_to=None, amount_per_unit=None)
    rule = parse_rule_set_json(json.dumps(doc))  # verifies the stored sha256
    assert rule.sha256 == FIXTURE_SHA256
    flagged = copy.deepcopy(doc)
    flagged["sha256"] = None
    flagged["components"][2]["integer_values_only"] = True
    assert compute_rule_set_sha256(RuleSet.model_validate(flagged)) != FIXTURE_SHA256


def test_new_fields_are_part_of_the_hash() -> None:
    base = compute_rule_set_sha256(valid())

    def flip(d: dict[str, Any]) -> None:
        component(d, "vat_base")["intermediate"] = False

    assert compute_rule_set_sha256(build(flip)) != base
    sealed = seal_rule_set(valid())
    assert parse_rule_set_json(sealed.model_dump_json()).sha256 == sealed.sha256


def test_engine_version_is_bumped() -> None:
    assert ENGINE_VERSION == "tax-engine/1.1.0"
    assert calculate(valid(), inputs(), T0).engine_version == ENGINE_VERSION


# --------------------------------------------------------------------------- indicative preview selection


def preview_set(
    version: str,
    status: TaxRuleStatus = TaxRuleStatus.DRAFT,
    *,
    valid_from: str | None = "2026-01-01",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> RuleSet:
    """A SYNTHETIC non-fixture rule set moved through the real lifecycle to ``status``."""
    doc = ext_doc()
    doc.update(
        version=version,
        status="draft",
        is_fixture=False,
        valid_from=valid_from,
        valid_to=None,
        sources=[SYNTHETIC_SOURCE.model_dump(mode="json")],
    )
    if mutate is not None:
        mutate(doc)
    rule = RuleSet.model_validate(doc)
    path = {
        TaxRuleStatus.DRAFT: (),
        TaxRuleStatus.UNDER_REVIEW: (TaxRuleStatus.UNDER_REVIEW,),
        TaxRuleStatus.APPROVED: (TaxRuleStatus.UNDER_REVIEW, TaxRuleStatus.APPROVED),
        TaxRuleStatus.ACTIVE: (TaxRuleStatus.UNDER_REVIEW, TaxRuleStatus.APPROVED, TaxRuleStatus.ACTIVE),
        TaxRuleStatus.REVOKED: (TaxRuleStatus.REVOKED,),
    }[status]
    for target in path:
        approval: dict[str, Any] = {}
        if target == TaxRuleStatus.APPROVED:
            review = ReviewRecord(
                reviewer="SYNTHETIC reviewer",
                reviewed_at=T0,
                content_sha256=compute_rule_set_sha256(rule),
                scope="SYNTHETIC review",
            )
            approval = {"approved_by": "SYNTHETIC owner", "review_record": review}
        rule = transition_rule_set(rule, target, at=T0, **approval)
    return rule


def preview(rule_sets: list[RuleSet], **kw: Any) -> RuleSelection:
    return select_preview_rule_set(rule_sets, "XX", CATEGORY, DECL, **kw)


def test_preview_prefers_approved_then_under_review_then_draft() -> None:
    draft = preview_set("d1")
    review = preview_set("r1", TaxRuleStatus.UNDER_REVIEW)
    approved = preview_set("a1", TaxRuleStatus.APPROVED)
    for pool, expected in (
        ([draft, review, approved], approved),
        ([draft, review], review),
        ([draft], draft),
    ):
        selection = preview(pool)
        assert selection.rule_set is expected
        assert selection.mode == "indicative_preview" and selection.allow_unapproved
        assert selection.reason.startswith(f"INDICATIVE PREVIEW: selected {expected.label()}")


def test_preview_never_uses_fixtures_examples_active_or_closed_sets() -> None:
    fixture = load_rule_set_file(FIXTURE)  # unapproved fixture
    draft_fixture = RuleSet.model_validate({**ext_doc(), "status": "draft"})
    active = preview_set("act", TaxRuleStatus.ACTIVE)
    revoked = preview_set("rev", TaxRuleStatus.REVOKED)
    empty = preview_set("empty", mutate=lambda d: d.update(components=[], required_inputs=[]))
    selection = preview([fixture, draft_fixture, active, revoked, empty])
    assert selection.rule_set is None
    assert "no indicative preview" in selection.reason
    assert any("fixture" in c for c in selection.considered)
    assert any("revoked" in c for c in selection.considered)
    assert any("active is never previewed" in c for c in selection.considered)
    assert any("no components" in c for c in selection.considered)


def test_preview_skips_invalid_and_ineffective_sets() -> None:
    tampered = seal_rule_set(preview_set("tampered")).model_copy(update={"description": "edited"})
    later = preview_set("later", valid_from="2026-07-01")
    selection = preview([tampered, later])
    assert selection.rule_set is None
    assert any("invalid" in c and "sha256" in c for c in selection.considered), selection.considered
    assert any("not effective" in c for c in selection.considered)
    open_start = preview_set("open", valid_from=None)
    assert preview([open_start]).rule_set is open_start


def test_preview_orders_by_valid_from_then_created_at() -> None:
    old = preview_set("old", valid_from="2026-01-01")
    new = preview_set("new", valid_from="2026-03-01")
    undated = preview_set("undated", valid_from=None)
    assert preview([old, new, undated]).rule_set is new
    twin = preview_set("twin", valid_from="2026-03-01")
    with pytest.raises(ValidationFailed, match="ambiguous") as exc:
        preview([new, twin])
    assert sorted(exc.value.details["candidates"]) == [new.label(), twin.label()]
    stamps = {new.label(): T0, twin.label(): datetime(2026, 6, 2, tzinfo=UTC)}
    assert preview([new, twin], created_at=stamps).rule_set is twin
    with pytest.raises(ValidationFailed, match="ambiguous"):
        preview([new, twin], created_at={new.label(): T0})  # a copy without created_at
    with pytest.raises(ValidationFailed, match="ambiguous"):
        preview([new, twin], created_at={new.label(): T0, twin.label(): T0})


def test_preview_copies_of_one_version() -> None:
    file_copy = preview_set("same")
    db_copy = preview_set("same", TaxRuleStatus.UNDER_REVIEW)
    chosen = preview([file_copy, db_copy]).rule_set
    assert chosen is not None and chosen.status == TaxRuleStatus.UNDER_REVIEW  # most advanced copy
    revoked_copy = preview_set("same", TaxRuleStatus.REVOKED)
    closed = preview([file_copy, revoked_copy])
    assert closed.rule_set is None and any("revoked" in c for c in closed.considered)
    edited = preview_set("same", mutate=lambda d: d.update(description="SYNTHETIC other content"))
    with pytest.raises(ValidationFailed, match="different content"):
        preview([file_copy, edited])


def test_preview_result_is_never_production_ready() -> None:
    chosen = preview([preview_set("d1")]).rule_set
    assert chosen is not None
    calc = calculate(chosen, inputs(), T0)
    assert calc.complete and not calc.production_ready
    assert "RULE_SET_NOT_ACTIVE:draft: result is not production-ready" in calc.warnings
    assert not any(line.rule_supported or line.assumption_approved for line in tax_cost_lines(calc))


def test_production_selection_is_unchanged_and_labelled() -> None:
    drafts = [preview_set("d1"), preview_set("a1", TaxRuleStatus.APPROVED)]
    production = select_rule_set(drafts, "XX", CATEGORY, DECL)
    assert production.rule_set is None and production.mode == "active" and not production.allow_unapproved
    active = preview_set("act", TaxRuleStatus.ACTIVE)
    assert select_rule_set([*drafts, active], "XX", CATEGORY, DECL).rule_set is active
    fixtures = select_rule_set([load_rule_set_file(FIXTURE)], "XX", CATEGORY, DECL, allow_unapproved=True)
    assert fixtures.mode == "unapproved_allowed"


def test_rule_selection_mode_is_consistent() -> None:
    with pytest.raises(ValidationError, match="active"):
        RuleSelection(rule_set=None, reason="x", allow_unapproved=False, mode="indicative_preview")
    with pytest.raises(ValidationError, match="active"):
        RuleSelection(rule_set=None, reason="x", allow_unapproved=True)
    with pytest.raises(ValidationError, match="indicative preview"):
        RuleSelection(
            rule_set=preview_set("act", TaxRuleStatus.ACTIVE),
            reason="x",
            allow_unapproved=True,
            mode="indicative_preview",
        )


def test_production_paths_never_reference_the_preview_selector() -> None:
    for path in ("src/suv_deals/crawling/valuation_pipeline.py", "src/suv_deals/domain/costs.py"):
        assert "select_preview_rule_set" not in (REPO / path).read_text(encoding="utf-8"), path
