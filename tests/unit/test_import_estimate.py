# ruff: noqa: RUF001
# (RUF001: the Macedonian banner and legal references are Cyrillic by design.)
"""Import-cost estimate: vehicle facts -> TaxInputs builder and the estimate service.

The estimates run on the shipped North Macedonian DRAFT rule set (never approved) in
indicative-preview mode, and on a SYNTHETIC jurisdiction-XX copy of it moved to ACTIVE inside
the test only (never a real approval) for the active mode. The transport amount comes from
the shipped cost profile's transport line. Expected amounts are the fact sheet's golden
examples (section 10.4, D1-D6) and the design's golden cases.
"""

from __future__ import annotations

import ast
import functools
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.crawling.valuation_pipeline import USED_VEHICLE_MIN_KM as PIPELINE_USED_VEHICLE_MIN_KM
from suv_deals.domain.costs import CostLine, load_cost_profile, tax_cost_lines
from suv_deals.domain.enums import (
    Co2Cycle,
    CostCategory,
    CostLineStatus,
    Fuel,
    FxPurpose,
    Precision,
    TaxRuleStatus,
)
from suv_deals.domain.import_estimate import (
    USED_VEHICLE_MIN_KM,
    BuiltTaxInputs,
    Co2Reading,
    EstimatePolicy,
    ImportCostEstimate,
    OriginProofClaim,
    PriceFact,
    VehicleImportFacts,
    build_tax_inputs,
    declaration_date_for,
    estimate_from_rule_sets,
    estimate_import_costs,
    facts_from_listing,
    select_estimate_rule_set,
    vehicle_age_range,
)
from suv_deals.domain.listings import (
    Co2Info,
    Documentation,
    LocationInfo,
    NormalizedListing,
    PartialDate,
    VehicleSpec,
)
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    ComponentStatus,
    OriginProofStatus,
    ReviewRecord,
    RuleSelection,
    RuleSet,
    TaxInputs,
    compute_rule_set_sha256,
    convert_with_customs_rate,
    load_rule_set_file,
    transition_rule_set,
)
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "config" / "tax_rules" / "drafts" / "mk_passenger_car_import_2026.10-draft.2.json"
PROFILE = REPO / "config" / "cost_profiles" / "default_unapproved.yaml"
MODULE = REPO / "src" / "suv_deals" / "domain" / "import_estimate.py"
DRAFT_SHA256 = "a856d5e401eb87e728079be0bfe3a723311480d6cdf1f38123cf6c052603d4b7"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
DECL = date(2026, 10, 13)
BANNER = (
    "ИНДИКАТИВНА ПРОЦЕНКА – неодобрени нацрт-правила (mk-passenger-car-import@2026.10-draft.2). Не е "
    "царинска пресметка; не се користи за вреднувања ни известувања. / INDICATIVE PREVIEW – unapproved "
    "draft rules; not a customs assessment; never used for valuations or alerts."
)


@functools.cache
def draft() -> RuleSet:
    return load_rule_set_file(DRAFT)


@functools.cache
def transport_line() -> CostLine:
    """The shipped cost profile's transport line (owner decision: fixed per vehicle)."""
    profile = load_cost_profile(PROFILE)
    return next(line for line in profile.lines() if line.category == CostCategory.TRANSPORT)


def quoted_transport(amount: str) -> CostLine:
    return CostLine(
        category=CostCategory.TRANSPORT,
        label="SYNTHETIC transporter quote",
        status=CostLineStatus.QUOTED,
        currency="EUR",
        base=Money.of(amount, "EUR"),
        evidence_ids=("quote:synthetic",),
        provider="SYNTHETIC transporter",
    )


def rate(
    value: str = "61.5337", on: date = date(2026, 10, 1), purpose: FxPurpose = FxPurpose.CUSTOMS
) -> FxRate:
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal(value),
        rate_date=on,
        retrieved_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        provider="customs monthly list (test input)",
        purpose=purpose,
    )


def facts(**kw: Any) -> VehicleImportFacts:
    """D1 of the fact sheet: diesel, NEDC 189 on the COC, EUR 3,000 invoice, no proof of origin."""
    data: dict[str, Any] = {
        "price": {"amount": "3000", "currency": "EUR"},
        "price_basis": "invoice",
        "declaration_date": DECL,
        "fuel": "diesel",
        "co2": [{"cycle": "nedc", "g_km": "189"}],
        "co2_document": "coc",
        "emissions_class": "Euro 5",
        "first_registration": {"value": "2012-05", "precision": "month"},
        "mileage_km": "180000",
        "seller_country": "DE",
    }
    data.update(kw)
    return VehicleImportFacts.model_validate(data)


def build(f: VehicleImportFacts | None = None, **kw: Any) -> BuiltTaxInputs:
    options: dict[str, Any] = {"transport": transport_line(), "customs_rate": rate(), "as_of": AS_OF}
    options.update(kw)
    return build_tax_inputs(f or facts(), **options)


def estimate(f: VehicleImportFacts | None = None, **kw: Any) -> ImportCostEstimate:
    options: dict[str, Any] = {
        "rule_sets": [draft()],
        "transport": transport_line(),
        "customs_rate": rate(),
        "as_of": AS_OF,
    }
    options.update(kw)
    rule_sets = options.pop("rule_sets")
    return estimate_from_rule_sets(rule_sets, f or facts(), **options)


def synthetic_active(status: TaxRuleStatus = TaxRuleStatus.ACTIVE) -> RuleSet:
    """A SYNTHETIC jurisdiction-XX copy of the draft's structure, approved inside this test only."""
    doc = json.loads(DRAFT.read_text(encoding="utf-8"), parse_float=Decimal)
    doc.update(
        rule_set_id="SYNTHETIC-TEST-ONLY-xx-active-copy",
        jurisdiction="XX",
        version="synthetic-1",
        description="SYNTHETIC TEST COPY of the draft structure under code XX; never a real approval.",
    )
    rule_set = RuleSet.model_validate(doc)
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    rule_set = transition_rule_set(rule_set, TaxRuleStatus.UNDER_REVIEW, at=at)
    if status == TaxRuleStatus.UNDER_REVIEW:
        return rule_set
    review = ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=at,
        content_sha256=compute_rule_set_sha256(rule_set),
        scope="SYNTHETIC test review of a copy; not a review of any real rule",
    )
    rule_set = transition_rule_set(
        rule_set, TaxRuleStatus.APPROVED, at=at, approved_by="SYNTHETIC owner", review_record=review
    )
    if status == TaxRuleStatus.APPROVED:
        return rule_set
    return transition_rule_set(rule_set, TaxRuleStatus.ACTIVE, at=at)


def money(amount: str, currency: str = "MKD") -> Money:
    return Money.of(amount, currency)


def component_amount(
    est: ImportCostEstimate, component_id: str, scenario: str = "legal_base"
) -> Money | None:
    chosen = est.scenario(scenario)  # type: ignore[arg-type]
    assert chosen is not None
    return next(c.amount for c in chosen.components if c.component_id == component_id)


# --------------------------------------------------------------------------- golden examples


@pytest.mark.parametrize(
    ("case", "kw", "total", "total_eur", "landed"),
    [
        ("D1", {}, "168154.45", "2732.72", "6432.72"),
        (
            "D3",
            {
                "price": {"amount": "2500", "currency": "EUR"},
                "fuel": "petrol",
                "co2": [{"cycle": "nedc", "g_km": "245"}],
            },
            "217629.97",
            "3536.76",
            "6736.76",
        ),
        (
            "D4",
            {"price": {"amount": "2800", "currency": "EUR"}, "co2": [], "co2_document": "none_available"},
            "466832.94",
            "7586.62",
            "11086.62",
        ),
        (
            "D5",
            {
                "co2": [{"cycle": "nedc", "g_km": "170"}],
                "transport_after_entry_eur": "150",
                "transport_after_entry_separately_invoiced": True,
            },
            "129094.68",
            "2097.95",
            "5797.95",
        ),
    ],
)
def test_fact_sheet_examples_as_indicative_preview(
    case: str, kw: dict[str, Any], total: str, total_eur: str, landed: str
) -> None:
    est = estimate(facts(**kw))
    assert est.mode == "indicative_preview" and not est.production_ready, case
    base = est.scenario("legal_base")
    assert base is not None and base.complete, (case, base and base.unknown_components)
    assert base.indicative_total == money(total), case
    assert base.indicative_total_eur == money(total_eur, "EUR"), case
    assert base.indicative_landed_cost_eur == money(landed, "EUR"), case
    assert base.total is None and base.total_eur is None and base.landed_cost_eur is None
    assert not base.production_ready


def test_d2_eu_proof_and_the_standard_duty_alternative() -> None:
    est = estimate(facts(origin_proof={"kind": "eu", "country": "DE", "evidence_ref": "eur1-scan-1"}))
    base, rejected = est.scenarios[0], est.scenario("standard_duty_if_proof_rejected")
    assert base.name == "legal_base" and rejected is not None
    assert base.indicative_total == money("154721.64")
    assert base.indicative_total_eur == money("2514.42", "EUR")
    assert base.indicative_landed_cost_eur == money("6214.42", "EUR")
    assert rejected.indicative_total == money("168154.45")  # D1: the 5 % alternative
    assert component_amount(est, "duty_preferential_eu") == money("0.00")
    assert component_amount(est, "duty_standard", "standard_duty_if_proof_rejected") == money("11383.73")
    assert any(a.startswith("ORIGIN_PROOF_ASSUMED_ACCEPTED_BY_CUSTOMS:eu:DE") for a in est.assumptions)
    assert rejected.assumptions[0].startswith("NO_ORIGIN_PROOF_STANDARD_DUTY")


def test_d6_customs_revaluation_risk_scenario() -> None:
    est = estimate(facts(catalogue_value_eur="6000"))
    risk = est.scenario("customs_revaluation_risk")
    assert risk is not None and risk.complete
    assert risk.customs_value == money("6700.00", "EUR")
    assert (risk.customs_value_basis or "").startswith(
        "RISK SCENARIO - customs revaluation (SchwackeNet), not the legal value: catalogue value 6000.00 EUR"
    )
    assert risk.indicative_total == money("212274.11")
    assert risk.indicative_total_eur == money("3449.72", "EUR")
    # The buyer still pays the EUR 3,000 price; only the charges follow the catalogue value.
    assert risk.indicative_landed_cost_eur == money("7149.72", "EUR")
    assert any("UNCERTAIN - confirm whether Customs adds transport" in a for a in risk.assumptions)
    assert est.scenarios[0].indicative_total == money("168154.45")  # the legal base is unchanged
    assert est.scenario("standard_duty_if_proof_rejected") is None  # no proof claimed


def test_cycle_choice_wltp_over_nedc_from_the_coc() -> None:
    est = estimate(
        facts(fuel="petrol", co2=[{"cycle": "nedc", "g_km": "130"}, {"cycle": "wltp", "g_km": "155"}])
    )
    assert est.scenarios[0].indicative_total == money("73618.75")  # G07
    assert component_amount(est, "dmv_co2_petrol") == money("16275.00")
    assert any(w.startswith("CO2_CYCLE_SELECTED:wltp") for w in est.warnings)


def test_plugin_hybrid_with_an_attested_hs_code() -> None:
    est = estimate(facts(fuel="plugin_hybrid", hs_code="8703 60", co2=[{"cycle": "nedc", "g_km": "50"}]))
    assert est.scenarios[0].indicative_total == money("54650.25")  # G12
    assert component_amount(est, "dmv_plugin_hybrid_half") == money("200.00")
    assert any(a.startswith("HS_CODE_FROM_CALLER:870360") for a in est.assumptions)


def test_plugin_hybrid_without_hs_code_is_unknown_not_zero() -> None:
    est = estimate(facts(fuel="plugin_hybrid", co2=[{"cycle": "nedc", "g_km": "50"}]))
    base = est.scenarios[0]
    assert not base.complete and base.indicative_total is None
    assert base.known_subtotal == money("11383.73")  # the duty only
    assert "hs_code" in est.missing_facts and "classification" in est.missing_inputs
    assert any(w.startswith("PHEV_HS_CODE_REQUIRED") for w in est.warnings)
    categories = {c.category: c for c in base.categories}
    assert categories[CostCategory.MOTOR_VEHICLE_TAX].status == ComponentStatus.UNKNOWN
    assert categories[CostCategory.MOTOR_VEHICLE_TAX].amount is None
    assert categories[CostCategory.OTHER_IMPORT_CHARGES].status == ComponentStatus.UNKNOWN


def test_battery_electric_pays_no_motor_vehicle_tax() -> None:
    est = estimate(facts(fuel="electric", co2=[], co2_document=None))
    base = est.scenarios[0]
    assert base.indicative_total == money("54414.25")  # G13
    categories = {c.category: c for c in base.categories}
    assert categories[CostCategory.MOTOR_VEHICLE_TAX].amount == money("0")
    assert categories[CostCategory.MOTOR_VEHICLE_TAX].status == ComponentStatus.RESOLVED
    assert "co2" not in est.missing_facts and "co2_document" not in est.missing_facts


def test_price_in_mkd_uses_the_customs_rate_for_the_transport() -> None:
    est = estimate(facts(price={"amount": "184601.10", "currency": "MKD"}))  # EUR 3,000 at 61.5337
    base = est.scenarios[0]
    assert base.customs_value == money("227674.69")  # 184,601.10 + 700 x 61.5337
    assert base.indicative_total == money("168154.45")
    assert base.indicative_landed_cost_eur == money("6432.72", "EUR")


def test_chf_price_leaves_the_customs_value_unknown() -> None:
    est = estimate(facts(price={"amount": "3000", "currency": "CHF"}))
    base = est.scenarios[0]
    assert base.customs_value is None and not base.complete
    assert base.indicative_total is None and base.indicative_landed_cost_eur is None
    assert base.known_subtotal is None
    # The price is given; what is missing is a price the customs value can use (regression:
    # it was reported as a missing "price").
    assert est.missing_facts == ("price_in_eur_or_mkd",)
    assert any(w.startswith("CUSTOMS_VALUE_CHF_UNSUPPORTED") for w in est.warnings)
    assert any(w.startswith("CUSTOMS_VALUE_CHF_UNSUPPORTED") for w in est.warnings)
    assert component_amount(est, "dmv_co2_diesel") == money("96390.00")  # nothing else is guessed


def test_missing_customs_rate_keeps_every_conversion_unknown() -> None:
    est = estimate(customs_rate=None)
    base = est.scenarios[0]
    assert not base.complete and base.indicative_total is None
    assert "customs_fx_rate" in est.missing_facts and "customs_fx_rate" in est.missing_inputs
    assert est.eur_basis.startswith("no customs rate")
    assert all(c.amount_eur is None for c in base.categories)
    assert any(w.startswith("CUSTOMS_FX_RATE_MISSING") for w in est.warnings)


def test_a_reference_rate_is_never_used_as_the_customs_rate() -> None:
    est = estimate(customs_rate=rate(purpose=FxPurpose.REFERENCE))
    base = est.scenarios[0]
    assert not base.complete and base.indicative_total is None
    assert "customs_fx_rate" in est.missing_facts


def test_euro_3_is_not_importable_but_the_charges_are_still_estimated() -> None:
    est = estimate(facts(emissions_class="Euro 3"))
    assert est.gates.importable == "no" and est.gates.euro_class == 3 and est.gates.min_euro == 4
    assert "UNCERTAIN - confirm" in est.gates.legal_basis
    assert any(w.startswith("EURO_CLASS_BELOW_MINIMUM") for w in est.warnings)
    assert est.scenarios[0].indicative_total == money("168154.45")


def test_euro_gate_minimum_is_a_policy_setting() -> None:
    assert (
        estimate(facts(emissions_class="Euro 5"), policy=EstimatePolicy(min_euro=6)).gates.importable == "no"
    )
    assert estimate(facts(emissions_class=None)).gates.importable == "unknown"


@pytest.mark.parametrize(
    ("text", "euro"),
    [
        ("Euro 4", 4),
        ("EURO 6d-TEMP", 6),
        ("Euro VI", 6),
        ("euro iv", 4),
        ("Euro-5", 5),
        ("5", 5),
        ("EU6", 6),
        ("Euro 5b", 5),
        ("Diesel", None),
        ("Euro 0", None),
        ("Euro 12", None),
    ],
)
def test_euro_class_parsing(text: str, euro: int | None) -> None:
    gates = build(facts(emissions_class=text)).gates
    assert gates.euro_class == euro
    assert gates.importable == ("unknown" if euro is None else ("yes" if euro >= 4 else "no"))


# --------------------------------------------------------------------------- preview labelling


def test_preview_is_labelled_on_every_field() -> None:
    est = estimate()
    assert est.banner is not None and est.banner.text() == BANNER
    assert est.rule is not None
    assert (est.rule.rule_set_id, est.rule.version, est.rule.status) == (
        "mk-passenger-car-import",
        "2026.10-draft.2",
        TaxRuleStatus.DRAFT,
    )
    assert est.rule.content_sha256 == DRAFT_SHA256 and not est.rule.sha256_recorded
    assert est.selection_reason.startswith("INDICATIVE PREVIEW: selected mk-passenger-car-import")
    assert any("not active for production (draft" in c for c in est.considered)
    base = est.scenarios[0]
    assert "RULE_SET_NOT_ACTIVE:draft: result is not production-ready" in base.warnings
    assert base.engine_version == "tax-engine/1.1.0" and len(base.calculation_sha256) == 64
    assert est.eur_basis.startswith("EUR equivalents at the customs rate EUR/MKD 61.5337 (2026-10-01")
    assert "excludes customs broker" in est.landed_cost_note
    helpers = {c.component_id for c in base.components if c.intermediate}
    assert helpers >= {"customs_value_mkd", "vmv_mkd", "dmv_value_part", "dmv_co2_diesel", "vat_base_mkd"}


def test_preview_banner_names_the_rule_status() -> None:
    under_review = transition_rule_set(draft(), TaxRuleStatus.UNDER_REVIEW, at=AS_OF)
    est = estimate(rule_sets=[under_review])
    assert est.banner is not None
    assert "неодобрени правила во преглед" in est.banner.mk and "under review" in est.banner.en
    approved = synthetic_active(TaxRuleStatus.APPROVED)
    est = estimate(rule_sets=[approved], jurisdiction="XX")
    assert est.mode == "indicative_preview" and est.banner is not None
    assert "not yet active" in est.banner.en and est.rule is not None and est.rule.sha256_recorded


def test_uncertain_items_list_every_marked_component_applicable_first() -> None:
    est = estimate()
    components = [i for i in est.uncertain_items if i.source == "component"]
    assert {i.component_id for i in components} == {
        "duty_preferential_other_fta",
        "duty_us_origin",
        "dmv_value_part",
        "dmv_co2_petrol",
        "dmv_co2_diesel",
        "dmv_co2_phev_petrol",
        "dmv_co2_phev_diesel",
        "dmv_co2_deemed_phev_petrol",
        "dmv_co2_deemed_phev_diesel",
        "vat_base_mkd",
    }
    applying = [i.component_id for i in components if i.applies]
    assert applying == ["dmv_value_part", "dmv_co2_diesel", "vat_base_mkd"]
    assert [i.applies for i in components] == sorted((i.applies for i in components), reverse=True)
    assert all("UNCERTAIN" in i.text for i in est.uncertain_items)
    rule_level = [i.text for i in est.uncertain_items if i.source == "rule_set"]
    assert est.uncertain_items[0].source == "rule_set"
    assert any(t.startswith("UNCERTAIN - confirm: rounding") for t in rule_level)
    assert any("customs processing fees (none found, UNCERTAIN)" in t for t in rule_level)
    proof = estimate(facts(origin_proof={"kind": "us"}))
    assert "duty_us_origin" in {i.component_id for i in proof.uncertain_items if i.applies}


def test_preview_result_can_never_be_production_ready_or_carry_final_totals() -> None:
    est = estimate()
    data = est.model_dump()
    with pytest.raises(ValidationError, match="never production-ready"):
        ImportCostEstimate.model_validate({**data, "production_ready": True})
    with pytest.raises(ValidationError, match="indicative_total"):
        scenario = {**data["scenarios"][0], "total": {"amount": "1", "currency": "MKD"}}
        ImportCostEstimate.model_validate({**data, "scenarios": [scenario]})
    with pytest.raises(ValidationError, match="banner"):
        ImportCostEstimate.model_validate({**data, "banner": None})


def test_preview_result_can_never_become_cost_lines() -> None:
    est = estimate()
    with pytest.raises(ValidationFailed, match="never does"):
        tax_cost_lines(est)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="never does"):
        tax_cost_lines(est.scenarios[0])  # type: ignore[arg-type]
    # The result carries no TaxCalculation anywhere that could be turned into lines.
    models = set(ImportCostEstimate.model_json_schema()["$defs"])
    assert "ComponentResult" in models and "TaxCalculation" not in models


def test_production_code_never_reaches_the_estimate() -> None:
    # The valuation pipeline shares the pure input BUILDER (same duty/ДМВ/VAT inputs as the
    # calculator), but never the estimate service, the preview selection or its result type.
    for path in (
        "src/suv_deals/crawling/valuation_pipeline.py",
        "src/suv_deals/domain/costs.py",
        "src/suv_deals/domain/valuation.py",
        "src/suv_deals/persistence/valuation_repo.py",
    ):
        text = (REPO / path).read_text(encoding="utf-8")
        if not path.endswith("valuation_pipeline.py"):
            assert "suv_deals.domain.import_estimate" not in text, path
        for name in (
            "estimate_import_costs",
            "estimate_from_rule_sets",
            "select_estimate_rule_set",
            "select_preview_rule_set",
            "ImportCostEstimate",
            "accept_previous_month_fx",
        ):
            assert name not in text, (path, name)
    pipeline = ast.parse((REPO / "src/suv_deals/crawling/valuation_pipeline.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(pipeline)
        if isinstance(node, ast.ImportFrom) and node.module == "suv_deals.domain.import_estimate"
        for alias in node.names
    }
    assert imported == {
        "USED_VEHICLE_MIN_KM",
        "BuiltTaxInputs",
        "build_tax_inputs",
        "facts_from_listing",
        "single_transport_line",
    }
    source = MODULE.read_text(encoding="utf-8")
    assert "tax_cost_lines(" not in source


def test_no_tax_rate_coefficient_or_threshold_literal_in_the_module() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    numbers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    }
    assert not any(isinstance(n, float) for n in numbers)
    decimal_strings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Decimal"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert decimal_strings == []
    rule_numbers: set[Decimal] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"rate", "amount", "rate_of_base", "amount_per_unit", "over", "up_to"} and item:
                    rule_numbers.add(Decimal(item))
                if key in {"lower_inclusive", "upper_exclusive"} and item is not None:
                    rule_numbers.add(Decimal(item))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(DRAFT.read_text(encoding="utf-8")))
    assert len(rule_numbers) > 40
    assert {Decimal(n) for n in numbers} & rule_numbers <= {Decimal(0), Decimal(1)}


# --------------------------------------------------------------------------- modes and selection


def test_active_rule_set_is_used_and_preview_is_only_the_fallback() -> None:
    active = synthetic_active()
    draft_copy = RuleSet.model_validate(
        {**draft().model_dump(), "jurisdiction": "XX", "rule_set_id": "SYNTHETIC-TEST-ONLY-xx-draft"}
    )
    est = estimate(rule_sets=[draft_copy, active], jurisdiction="XX")
    assert est.mode == "active" and est.banner is None and est.production_ready
    assert est.rule is not None and est.rule.status == TaxRuleStatus.ACTIVE and est.rule.sha256_recorded
    base = est.scenarios[0]
    assert base.production_ready and base.complete
    assert base.total == money("168154.45") and base.total_eur == money("2732.72", "EUR")
    assert base.landed_cost_eur == money("6432.72", "EUR")
    assert base.indicative_total is None and base.indicative_landed_cost_eur is None
    assert "RULE_SET_NOT_ACTIVE" not in " ".join(base.warnings)
    data = est.model_dump()
    with pytest.raises(ValidationError, match="indicative"):
        scenario = {**data["scenarios"][0], "indicative_total": {"amount": "1", "currency": "MKD"}}
        ImportCostEstimate.model_validate({**data, "scenarios": [scenario]})


def test_no_rule_set_means_unavailable_with_unknown_charges() -> None:
    for rule_sets, f in (
        ([], facts()),
        ([draft()], facts(declaration_date=date(2025, 6, 30))),  # before the draft's period
    ):
        est = estimate(f, rule_sets=rule_sets)
        assert est.mode == "unavailable" and not est.production_ready
        assert est.rule is None and est.scenarios == () and est.uncertain_items == ()
        assert est.banner is not None and "unknown (not zero)" in est.banner.en
        assert est.missing_facts[0] == "tax_rule_set" and est.missing_inputs == ("tax_rule_set",)
        assert any(w.startswith("IMPORT_COSTS_UNKNOWN") for w in est.warnings)
        assert est.gates.importable == "yes"  # facts are still assessed
    other_category = estimate(vehicle_category="motorcycle")
    assert other_category.mode == "unavailable"


def test_selection_refuses_fixture_mode_and_mislabelled_active() -> None:
    fixture_mode = RuleSelection(
        rule_set=draft(), reason="x", allow_unapproved=True, mode="unapproved_allowed"
    )
    with pytest.raises(ValidationFailed, match="fixture"):
        estimate_import_costs(
            fixture_mode, facts(), transport=transport_line(), customs_rate=rate(), as_of=AS_OF
        )
    mislabelled = RuleSelection(rule_set=draft(), reason="x", allow_unapproved=False, mode="active")
    with pytest.raises(ValidationFailed, match="ACTIVE"):
        estimate_import_costs(
            mislabelled, facts(), transport=transport_line(), customs_rate=rate(), as_of=AS_OF
        )
    other = select_estimate_rule_set([draft()], "MK", "passenger_car", DECL)
    with pytest.raises(ValidationFailed, match="another jurisdiction"):
        estimate_import_costs(
            other, facts(), transport=transport_line(), customs_rate=rate(), as_of=AS_OF, jurisdiction="XX"
        )


def test_select_estimate_rule_set_merges_what_was_considered() -> None:
    selection = select_estimate_rule_set([draft()], "MK", "passenger_car", DECL)
    assert selection.mode == "indicative_preview" and selection.rule_set == draft()
    empty = select_estimate_rule_set([], "MK", "passenger_car", DECL)
    assert empty.rule_set is None
    assert "no applicable ACTIVE rule set" in empty.reason and "no indicative preview" in empty.reason


# --------------------------------------------------------------------------- builder table


def test_builder_legal_base_inputs() -> None:
    built = build()
    inputs = built.inputs
    assert inputs.jurisdiction == "MK" and inputs.declaration_date == DECL
    assert inputs.customs_value == money("3700.00", "EUR")
    assert inputs.customs_value_basis == (
        "Царински закон чл. 28: invoice 3000.00 EUR + чл. 35(1)(д) transport to MK place of entry 700.00 EUR "
        "(transport line 700.00 EUR; separately invoiced inland leg 0.00 EUR)"
    )
    assert inputs.included_costs is not None and len(inputs.included_costs) == 1
    entry = inputs.included_costs[0]
    assert entry.amount == money("700", "EUR") and entry.legal_basis == "Царински закон чл. 35(1)(д)"
    assert inputs.vat_incidental_costs == ()
    assert inputs.customs_fx_rate == rate()
    assert (inputs.customs_fx_valid_from, inputs.customs_fx_valid_to) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert inputs.origin_proof is not None
    assert inputs.origin_proof.acceptance_status == OriginProofStatus.NOT_AVAILABLE
    assert inputs.origin_country is None and inputs.dispatch_country is None
    assert inputs.seller_country == "DE"
    assert inputs.fuel == Fuel.DIESEL
    assert (inputs.co2_g_km, inputs.co2_cycle, inputs.co2_source_document) == (
        Decimal(189),
        Co2Cycle.NEDC,
        "coc",
    )
    assert inputs.classification is None
    assert inputs.vehicle_age_years == Decimal(14)
    assert inputs.vehicle_condition == "used"
    assert inputs.emissions_class == "Euro 5"
    assert inputs.exemptions == ()
    assert [s.name for s in built.scenarios] == ["legal_base"]
    assert built.transport == money("700.00", "EUR") and built.price_eur == money("3000", "EUR")
    assert any(a.startswith("NO_ORIGIN_PROOF_STANDARD_DUTY") for a in built.assumptions)
    assert any(a.startswith("TRANSPORT_IN_CUSTOMS_VALUE:700.00 EUR") for a in built.assumptions)
    assert any(
        a.startswith("CUSTOMS_FX_MONTHLY_LIST_VALIDITY:2026-10-01..2026-10-31") for a in built.assumptions
    )
    assert not any(a.startswith("ADVERTISED_PRICE") for a in built.assumptions)
    assert built.warnings == () and built.missing_facts == ()


def test_shipped_transport_line_is_the_owner_fixed_amount() -> None:
    line = transport_line()
    assert line.status == CostLineStatus.ESTIMATED
    assert line.base == line.low == line.high == money("700.00", "EUR")


def test_transport_amount_comes_from_the_line_not_from_code() -> None:
    built = build(transport=quoted_transport("650"))
    assert built.inputs.customs_value == money("3650", "EUR")
    assert built.transport == money("650", "EUR")


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (
            CostLine(
                category=CostCategory.TRANSPORT, label="t", status=CostLineStatus.UNKNOWN, currency="EUR"
            ),
            "TRANSPORT_LINE_NOT_USABLE:unknown",
        ),
        (
            CostLine(
                category=CostCategory.TRANSPORT,
                label="t",
                status=CostLineStatus.ESTIMATED,
                currency="CHF",
                base=Money.of("650", "CHF"),
            ),
            "TRANSPORT_LINE_CURRENCY_UNSUPPORTED:CHF",
        ),
    ],
)
def test_unusable_transport_line_leaves_the_customs_value_unknown(line: CostLine, code: str) -> None:
    built = build(transport=line)
    assert built.inputs.customs_value is None and built.inputs.included_costs is None
    assert built.transport is None and "transport" in built.missing_facts
    assert any(w.startswith(code) for w in built.warnings)


def test_transport_line_must_be_a_transport_line() -> None:
    wrong = CostLine(
        category=CostCategory.CUSTOMS_BROKER, label="b", status=CostLineStatus.UNKNOWN, currency="EUR"
    )
    with pytest.raises(ValidationFailed, match="category transport"):
        build(transport=wrong)


def test_separately_invoiced_inland_leg_goes_to_the_vat_base_only() -> None:
    built = build(facts(transport_after_entry_eur="150", transport_after_entry_separately_invoiced=True))
    assert built.inputs.customs_value == money("3550", "EUR")
    assert built.inputs.vat_incidental_costs is not None and len(built.inputs.vat_incidental_costs) == 1
    leg = built.inputs.vat_incidental_costs[0]
    assert leg.amount == money("150", "EUR") and leg.legal_basis == "ЗДДВ чл. 21(2)(2)"
    assert any(a.startswith("TRANSPORT_SPLIT:550.00 EUR") for a in built.assumptions)
    with pytest.raises(ValidationFailed, match="exceeds the transport line"):
        build(facts(transport_after_entry_eur="700.01", transport_after_entry_separately_invoiced=True))


def test_inland_leg_needs_the_separately_invoiced_statement() -> None:
    with pytest.raises(ValidationError, match="separately invoiced"):
        facts(transport_after_entry_eur="150")
    with pytest.raises(ValidationError, match="separately invoiced"):
        facts(transport_after_entry_separately_invoiced=True)


def test_declaration_date_defaults_to_as_of_with_a_warning() -> None:
    f = facts(declaration_date=None)
    built = build(f)
    assert built.inputs.declaration_date == DECL == declaration_date_for(f, AS_OF)
    assert "DECLARATION_DATE_ASSUMED_TODAY:2026-10-13" in built.warnings


def test_advertised_price_is_labelled() -> None:
    built = build(facts(price_basis="advertised"))
    assert any(a.startswith("ADVERTISED_PRICE_AS_TRANSACTION_VALUE") for a in built.assumptions)
    assert "advertised price 3000.00 EUR" in (built.inputs.customs_value_basis or "")


def test_missing_price() -> None:
    built = build(facts(price=None))
    assert built.inputs.customs_value is None and built.inputs.customs_value_basis is None
    assert built.missing_facts == ("price",) and built.price_eur is None


def test_previous_month_customs_rate_only_on_request() -> None:
    september = rate("61.4950", date(2026, 9, 1))
    strict = estimate(customs_rate=september)
    assert not strict.scenarios[0].complete
    assert (
        "CUSTOMS_FX_RATE_NOT_EFFECTIVE"
        in next(c for c in strict.scenarios[0].components if c.component_id == "customs_value_mkd").warnings
    )
    relaxed = estimate(customs_rate=september, accept_previous_month_fx=True)
    assert relaxed.scenarios[0].complete
    assert any(a.startswith("CUSTOMS_FX_PREVIOUS_MONTH_ACCEPTED:2026-09-01") for a in relaxed.assumptions)
    two_months = build(customs_rate=rate("61.4988", date(2026, 8, 1)), accept_previous_month_fx=True)
    assert two_months.inputs.customs_fx_valid_to == date(2026, 8, 31)
    active = estimate(
        rule_sets=[synthetic_active()],
        jurisdiction="XX",
        customs_rate=september,
        accept_previous_month_fx=True,
    )
    assert active.mode == "active" and not active.scenarios[0].complete and not active.production_ready
    assert any(w.startswith("PREVIOUS_MONTH_FX_PREVIEW_ONLY") for w in active.warnings)


@pytest.mark.parametrize(
    ("claim", "country", "duty_component"),
    [
        ({"kind": "eu"}, "EU", "duty_preferential_eu"),
        ({"kind": "eu", "country": "IT"}, "IT", "duty_preferential_eu"),
        ({"kind": "efta", "country": "CH"}, "CH", "duty_preferential_other_fta"),
        ({"kind": "tr"}, "TR", "duty_preferential_other_fta"),
        ({"kind": "uk"}, "GB", "duty_preferential_other_fta"),
        ({"kind": "cefta", "country": "RS"}, "RS", "duty_preferential_other_fta"),
        ({"kind": "ua"}, "UA", "duty_preferential_other_fta"),
        ({"kind": "us"}, "US", "duty_us_origin"),
        ({"kind": "eu", "country": "JP"}, "JP", "duty_standard"),  # the rule set decides, not the kind
    ],
)
def test_claimed_origin_proofs(claim: dict[str, str], country: str, duty_component: str) -> None:
    est = estimate(facts(origin_proof=claim))
    built = build(facts(origin_proof=claim))
    proof = built.inputs.origin_proof
    assert proof is not None and proof.acceptance_status == OriginProofStatus.ACCEPTED
    assert proof.origin_country == country and proof.preferential is True
    assert proof.evidence_ids == ("caller_attestation:origin_proof",)
    assert [s.name for s in built.scenarios] == ["legal_base", "standard_duty_if_proof_rejected"]
    rejected = built.scenario("standard_duty_if_proof_rejected")
    assert rejected is not None and rejected.inputs.origin_proof is not None
    assert rejected.inputs.origin_proof.acceptance_status == OriginProofStatus.NOT_AVAILABLE
    resolved = {
        c.component_id
        for c in est.scenarios[0].components
        if c.status == ComponentStatus.RESOLVED and c.component_id.startswith("duty_")
    }
    assert resolved == {duty_component}


def test_unknown_origin_proof_leaves_the_duty_unknown() -> None:
    est = estimate(facts(origin_proof={"kind": "unknown"}))
    assert est.scenarios[0].indicative_total is None
    assert "origin_proof" in est.missing_facts
    assert any(w.startswith("ORIGIN_PROOF_UNKNOWN") for w in est.warnings)
    assert [s.name for s in est.scenarios] == ["legal_base"]


@pytest.mark.parametrize(
    "claim",
    [
        {"kind": "efta"},
        {"kind": "cefta"},
        {"kind": "none", "country": "DE"},
        {"kind": "unknown", "evidence_ref": "x"},
    ],
)
def test_origin_proof_claims_are_consistent(claim: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        OriginProofClaim.model_validate(claim)


def test_co2_cycle_and_document_rules() -> None:
    nedc_only = build(facts(co2=[{"cycle": "nedc", "g_km": "189"}]))
    assert nedc_only.inputs.co2_cycle == Co2Cycle.NEDC
    assert not any(w.startswith("CO2_CYCLE_SELECTED") for w in nedc_only.warnings)
    correlated = build(facts(co2=[{"cycle": "nedc_correlated", "g_km": "189"}]))
    assert correlated.inputs.co2_cycle == Co2Cycle.NEDC_CORRELATED  # passed through, never converted
    both = build(facts(co2=[{"cycle": "nedc_correlated", "g_km": "150"}, {"cycle": "nedc", "g_km": "189"}]))
    assert (both.inputs.co2_cycle, both.inputs.co2_g_km) == (Co2Cycle.NEDC, Decimal(189))
    listing = build(facts(co2_document="listing_claim_unverified"))
    assert any(w.startswith("CO2_NOT_FROM_ART12_DOCUMENT") for w in listing.warnings)
    undocumented = build(facts(co2_document=None))
    assert "co2_document" in undocumented.missing_facts
    no_figure = build(facts(co2=[]))
    assert "co2" in no_figure.missing_facts and no_figure.inputs.co2_g_km is None
    assert any(w.startswith("CO2_MISSING") for w in no_figure.warnings)
    deemed = build(facts(co2_document="none_available"))
    assert any(w.startswith("CO2_FIGURE_IGNORED") for w in deemed.warnings)
    assert "co2" not in deemed.missing_facts
    with pytest.raises(ValidationError, match="one CO2 reading per measurement cycle"):
        facts(co2=[{"cycle": "nedc", "g_km": "1"}, {"cycle": "nedc", "g_km": "2"}])
    with pytest.raises(ValidationError):
        facts(co2_document="seller_email")  # closed code set


def test_non_integer_co2_is_unknown_unless_rounding_up_is_confirmed() -> None:
    fraction = estimate(facts(co2=[{"cycle": "nedc", "g_km": "190.5"}]))
    assert not fraction.scenarios[0].complete
    assert any(w.startswith("CO2_NOT_INTEGER:190.5") for w in fraction.warnings)
    confirmed = estimate(facts(co2=[{"cycle": "nedc", "g_km": "190.5"}], co2_round_up_confirmed=True))
    assert component_amount(confirmed, "dmv_co2_diesel") == money("115555.00")  # 191 x 605
    assert "CO2_ROUNDED_UP_BY_CALLER:190.5->191 g/km" in confirmed.assumptions


def test_hs_code_is_canonical_hs6_and_only_8703() -> None:
    assert facts(hs_code="8703.60.10").hs_code == "870360"
    assert facts(hs_code="870370").hs_code == "870370"
    built = build(facts(hs_code="8703 23", hs_evidence_ref="broker-ruling-7"))
    classification = built.inputs.classification
    assert classification is not None and classification.approval_status == "approved"
    assert classification.tariff_code == "870323" and classification.vehicle_category == "passenger_car"
    assert classification.evidence_ids == ("caller_attestation:broker-ruling-7",)
    assert classification.approved_by == "caller"
    with pytest.raises(ValidationError, match="only HS 8703"):
        facts(hs_code="8704 21")
    with pytest.raises(ValidationError, match="6-10 digits"):
        facts(hs_code="8703")
    with pytest.raises(ValidationError, match="needs hs_code"):
        facts(hs_evidence_ref="x")


@pytest.mark.parametrize(
    ("value", "precision", "expected"),
    [
        ("2016-10-13", Precision.DAY, (10, 10)),
        ("2016-10-14", Precision.DAY, (9, 9)),
        ("2016-05", Precision.MONTH, (10, 10)),
        ("2016-10", Precision.MONTH, (9, 10)),  # 1-31 October: before or after the anniversary
        ("2016", Precision.YEAR, (9, 10)),
        (None, Precision.UNKNOWN, None),
    ],
)
def test_vehicle_age_range(value: str | None, precision: Precision, expected: tuple[int, int] | None) -> None:
    assert vehicle_age_range(PartialDate(value=value, precision=precision), DECL) == expected


def test_vehicle_age_only_when_unambiguous() -> None:
    exact = build(facts(first_registration={"value": "2016-10-13", "precision": "day"}))
    assert exact.inputs.vehicle_age_years == Decimal(10)
    ambiguous = build(facts(first_registration={"value": "2016", "precision": "year"}))
    assert ambiguous.inputs.vehicle_age_years is None and "AGE_AMBIGUOUS:9-10" in " ".join(ambiguous.warnings)
    unknown = build(facts(first_registration={"value": None, "precision": "unknown"}))
    assert unknown.inputs.vehicle_age_years is None
    future = build(facts(first_registration={"value": "2027-01", "precision": "month"}))
    assert any(w.startswith("FIRST_REGISTRATION_AFTER_DECLARATION") for w in future.warnings)


def test_possible_oldtimer_is_warned_never_applied() -> None:
    old = estimate(facts(first_registration={"value": "1990", "precision": "year"}))
    assert any(w.startswith("POSSIBLE_OLDTIMER_EXEMPTION_NOT_APPLIED") for w in old.warnings)
    assert old.scenarios[0].indicative_total == money("168154.45")  # no exemption is applied
    young = build(facts(first_registration={"value": "1992-01-01", "precision": "day"}))
    assert not any(w.startswith("POSSIBLE_OLDTIMER") for w in young.warnings)
    stricter = build(facts(), policy=EstimatePolicy(oldtimer_min_age_years=14))
    assert any(w.startswith("POSSIBLE_OLDTIMER") for w in stricter.warnings)


def test_used_condition_follows_the_pipeline_mileage_convention() -> None:
    assert USED_VEHICLE_MIN_KM == PIPELINE_USED_VEHICLE_MIN_KM
    assert build(facts(mileage_km="999")).inputs.vehicle_condition is None
    assert build(facts(mileage_km="1000")).inputs.vehicle_condition == "used"
    assert build(facts(mileage_km=None)).inputs.vehicle_condition is None


def test_importer_status_is_presentation_only() -> None:
    dealer = estimate(facts(importer_status="vat_dealer"))
    assert dealer.notes[0].startswith("IMPORT_VAT_DEDUCTIBLE_ONLY_FOR_TRADING")
    assert dealer.scenarios[0].indicative_total == money("168154.45")
    private = build(facts(importer_status="private"))
    assert private.notes[0].startswith("IMPORT_VAT_IS_A_FINAL_COST")
    assert private.inputs.importer_status == "private"


def test_unknown_fuel() -> None:
    est = estimate(facts(fuel="unknown"))
    assert not est.scenarios[0].complete and "fuel" in est.missing_facts


def test_facts_refuse_floats_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="binary float"):
        PriceFact.model_validate({"amount": 3000.5, "currency": "EUR"})
    with pytest.raises(ValidationError, match="binary float"):
        Co2Reading.model_validate({"cycle": "nedc", "g_km": 189.0})
    with pytest.raises(ValidationError):
        facts(colour="red")
    with pytest.raises(ValidationError):
        facts(price={"amount": "3000", "currency": "USD"})


# --------------------------------------------------------------------------- listing facts


def _listing(**vehicle: Any) -> NormalizedListing:
    return NormalizedListing(
        source_key="synthetic",
        source_listing_id="SYNTHETIC-1",
        canonical_url="https://example.invalid/listing/1",
        observed_at=AS_OF,
        location=LocationInfo(country="DE"),
        vehicle=VehicleSpec(
            fuel=Fuel.DIESEL,
            first_registration=PartialDate(value="2012-05", precision=Precision.MONTH),
            mileage_km=Decimal(180000),
            **vehicle,
        ),
        documentation=Documentation(emissions_class="Euro 5"),
        co2=Co2Info(g_per_km=Decimal(189), cycle=Co2Cycle.NEDC),
        parser_version="synthetic/1",
    )


def test_facts_from_listing() -> None:
    listed = facts_from_listing(_listing(), payable=Money.of("3000", "EUR"), declaration_date=DECL)
    assert listed.price == PriceFact(amount=Decimal(3000), currency="EUR")
    assert listed.price_basis == "advertised" and listed.co2_document == "listing_claim_unverified"
    assert listed.co2 == (Co2Reading(cycle=Co2Cycle.NEDC, g_km=Decimal(189)),)
    assert listed.origin_proof.kind == "none" and listed.seller_country == "DE"
    est = estimate(listed)
    assert est.scenarios[0].indicative_total == money("168154.45")
    assert any(a.startswith("ADVERTISED_PRICE_AS_TRANSACTION_VALUE") for a in est.assumptions)
    assert any(w.startswith("CO2_NOT_FROM_ART12_DOCUMENT") for w in est.warnings)
    no_co2 = _listing().model_copy(update={"co2": Co2Info()})
    bare = facts_from_listing(no_co2, payable=Money.of("100", "USD"))
    assert bare.co2 == () and bare.co2_document is None and bare.price is None


# --------------------------------------------------------------------------- engine conversion helper


def test_convert_with_customs_rate_applies_the_engine_checks() -> None:
    inputs = TaxInputs(
        declaration_date=DECL,
        customs_fx_rate=rate(),
        customs_fx_valid_from=date(2026, 10, 1),
        customs_fx_valid_to=date(2026, 10, 31),
    )
    converted, codes = convert_with_customs_rate(Money.of("700", "EUR"), "MKD", inputs)
    assert converted == Money.of("43073.5900", "MKD") and codes == ()
    back, _ = convert_with_customs_rate(Money.of("43073.59", "MKD"), "EUR", inputs)
    assert back == Money.of("700", "EUR")
    late = inputs.model_copy(update={"customs_fx_rate": rate(on=date(2026, 10, 14))})
    assert convert_with_customs_rate(Money.of("1", "EUR"), "MKD", late)[0] is None
    reference = inputs.model_copy(update={"customs_fx_rate": rate(purpose=FxPurpose.REFERENCE)})
    missing, codes = convert_with_customs_rate(Money.of("1", "EUR"), "MKD", reference)
    assert missing is None and codes[0].startswith("CUSTOMS_FX_RATE_WRONG_PURPOSE")
    assert convert_with_customs_rate(Money.of("1", "CHF"), "MKD", inputs)[0] is None


# --------------------------------------------------------------------------- review regressions


def _mvt(est: ImportCostEstimate) -> Any:
    return next(c for c in est.scenarios[0].categories if c.category == CostCategory.MOTOR_VEHICLE_TAX)


@pytest.mark.parametrize("fuel", ["diesel", "petrol", "hybrid_petrol", "lpg", "plugin_hybrid"])
@pytest.mark.parametrize("cycle", ["nedc", "wltp"])
@pytest.mark.parametrize("document", ["coc", "listing_claim_unverified"])
@pytest.mark.parametrize("figure", ["0", "0.5"])
def test_co2_under_one_gram_for_a_combustion_car_is_not_established(
    fuel: str, cycle: str, document: str, figure: str
) -> None:
    """Regression: CO2 0 g/km for a combustion car hit the Table 2 [0,1) row (coefficient 0), so
    the ДМВ CO2 part came out 0 and the result was complete (54,414.25 instead of 168,154.45)."""
    hs = {"hs_code": "870360"} if fuel == "plugin_hybrid" else {}
    est = estimate(facts(fuel=fuel, co2=[{"cycle": cycle, "g_km": figure}], co2_document=document, **hs))
    base = est.scenarios[0]
    assert not base.complete and base.indicative_total is None
    assert "co2" in est.missing_facts
    assert any(w.startswith(f"CO2_ZERO_FOR_COMBUSTION_VEHICLE:{figure}") for w in est.warnings)
    assert _mvt(est).status == ComponentStatus.UNKNOWN and _mvt(est).amount is None
    built = build(facts(fuel=fuel, co2=[{"cycle": cycle, "g_km": figure}], co2_document=document, **hs))
    assert built.inputs.co2_g_km is None and built.inputs.co2_cycle == Co2Cycle.UNKNOWN
    assert not any(u.startswith("co2:") for u in built.unverified_inputs)


def test_co2_zero_stays_a_fact_for_an_electric_car_and_one_gram_for_any_car() -> None:
    electric = estimate(facts(fuel="electric", co2=[{"cycle": "wltp", "g_km": "0"}]))
    assert electric.scenarios[0].complete and _mvt(electric).amount == money("0")
    assert not any(w.startswith("CO2_ZERO_FOR_COMBUSTION_VEHICLE") for w in electric.warnings)
    one = estimate(facts(co2=[{"cycle": "wltp", "g_km": "1"}]))
    assert one.scenarios[0].complete and "co2" not in one.missing_facts
    deemed = estimate(facts(co2=[{"cycle": "nedc", "g_km": "0"}], co2_document="none_available"))
    assert deemed.scenarios[0].indicative_total == money("469774.25")  # the deemed 400 g/km applies


def test_listing_co2_under_one_gram_for_a_combustion_car_is_no_fact() -> None:
    """Regression: a dealer page's ``emissionsCO2: 0`` reached valuations and the calculator as
    a listing claim and gave a complete, understated ДМВ."""
    zero = _listing().model_copy(update={"co2": Co2Info(g_per_km=Decimal(0), cycle=Co2Cycle.WLTP)})
    listed = facts_from_listing(zero, payable=Money.of("3000", "EUR"), declaration_date=DECL)
    assert listed.co2 == () and listed.co2_document is None
    est = estimate(listed)
    assert not est.scenarios[0].complete and {"co2", "co2_document"} <= set(est.missing_facts)
    electric = zero.model_copy(update={"vehicle": zero.vehicle.model_copy(update={"fuel": Fuel.ELECTRIC})})
    kept = facts_from_listing(electric, payable=Money.of("3000", "EUR"))
    assert kept.co2 == (Co2Reading(cycle=Co2Cycle.WLTP, g_km=Decimal(0)),)


def test_listing_co2_with_an_extreme_exponent_is_no_fact() -> None:
    """Regression: a crawled ``1e-100000000`` (in range) reached the engine and expanded to a
    100 MB string per estimate; it is a parse artefact, never a fact."""
    tiny = Co2Info(g_per_km=Decimal("1E-100000000"), cycle=Co2Cycle.WLTP)
    electric = _listing().model_copy(update={"co2": tiny})
    electric = electric.model_copy(
        update={"vehicle": electric.vehicle.model_copy(update={"fuel": Fuel.ELECTRIC})}
    )
    listed = facts_from_listing(electric, payable=Money.of("3000", "EUR"))
    assert listed.co2 == () and listed.co2_document is None
    petrol = _listing().model_copy(update={"co2": tiny})
    est = estimate(facts_from_listing(petrol, payable=Money.of("3000", "EUR"), declaration_date=DECL))
    assert len(est.model_dump_json()) < 200_000


@pytest.mark.parametrize(
    ("hs", "deemed", "amount", "dmv", "total"),
    [
        ("870360", "dmv_co2_deemed_phev_petrol", "278000.00", "139000.00", "218434.25"),
        ("870370", "dmv_co2_deemed_phev_diesel", "352000.00", "176000.00", "262094.25"),
    ],
)
def test_plugin_hybrid_without_a_co2_document_gets_the_deemed_400_g_km_halved(
    hs: str, deemed: str, amount: str, dmv: str, total: str
) -> None:
    """Regression: no deemed CO2 component covered a plug-in hybrid, so with none_available the
    ДМВ stayed unknown with nothing listed as missing. ЗДМВ чл. 12(2) deems 400 g/km and чл. 9(3)
    halves the tax: 0.5 x (value part 0 + 400 x 695 or 880)."""
    est = estimate(facts(fuel="plugin_hybrid", hs_code=hs, co2=[], co2_document="none_available"))
    base = est.scenarios[0]
    assert base.complete, base.unknown_components
    assert component_amount(est, deemed) == money(amount)
    assert component_amount(est, "dmv_value_part") == money("0.00")
    assert component_amount(est, "dmv_plugin_hybrid_half") == money(dmv)
    # VAT 18 % of customs value 227,674.69 + duty 11,383.73 + ДМВ; total = duty + ДМВ + VAT.
    assert base.indicative_total == money(total)
    assert est.missing_facts == ()
    assert any("applied only where the rule set models it" in w for w in est.warnings)
    without_hs = estimate(facts(fuel="plugin_hybrid", co2=[], co2_document="none_available"))
    assert not without_hs.scenarios[0].complete and "hs_code" in without_hs.missing_facts


def test_a_rule_set_that_covers_no_alternative_names_the_gap() -> None:
    """Defence in depth: an alternative group with no applicable member is named, so an
    incomplete result never carries nothing to act on."""
    est = estimate(facts(fuel="other"))
    assert not est.scenarios[0].complete
    gaps = [w for w in est.warnings if w.startswith("RULE_SET_DOES_NOT_COVER_VEHICLE:")]
    assert any(w.startswith("RULE_SET_DOES_NOT_COVER_VEHICLE:dmv:") for w in gaps), est.warnings
    assert not any(w.startswith("RULE_SET_DOES_NOT_COVER_VEHICLE") for w in estimate().warnings)


@pytest.mark.parametrize(
    ("fuel", "hs"),
    [
        ("hybrid_petrol", "870360"),  # listing says hybrid, the attested code says plug-in
        ("electric", "870360"),  # was: ДМВ 0 although a plug-in hybrid pays 50 %
        ("diesel", "870380"),  # was: full ДМВ although a BEV is not subject
        ("petrol", "870332"),
        ("plugin_hybrid", "870380"),
    ],
)
def test_an_attested_hs_code_that_contradicts_the_fuel_leaves_the_tax_unknown(fuel: str, hs: str) -> None:
    """Regression: the ДМВ treatment followed the fuel alone, so an attested HS code that
    disagreed gave a complete result with the wrong alternative and no warning."""
    est = estimate(facts(fuel=fuel, hs_code=hs, co2=[{"cycle": "wltp", "g_km": "40"}]))
    base = est.scenarios[0]
    assert not base.complete and base.indicative_total is None
    assert {"fuel", "hs_code"} <= set(est.missing_facts)
    assert any(w.startswith(f"HS_CODE_FUEL_MISMATCH:{hs}/{fuel}") for w in est.warnings)
    assert _mvt(est).status == ComponentStatus.UNKNOWN
    assert build(facts(fuel=fuel, hs_code=hs)).inputs.fuel == Fuel.UNKNOWN


@pytest.mark.parametrize(
    ("fuel", "hs"),
    [
        ("plugin_hybrid", "870360"),
        ("plugin_hybrid", "870370"),
        ("petrol", "870323"),
        ("lpg", "870322"),
        ("diesel", "870332"),
        ("hybrid_petrol", "870340"),
        ("hybrid_diesel", "870350"),
        ("electric", "870380"),
        ("diesel", "870390"),  # "other" subheading: not constrained
    ],
)
def test_a_consistent_hs_code_keeps_the_fuel(fuel: str, hs: str) -> None:
    est = estimate(facts(fuel=fuel, hs_code=hs, co2=[{"cycle": "wltp", "g_km": "40"}]))
    assert not any(w.startswith("HS_CODE_FUEL_MISMATCH") for w in est.warnings)
    assert "fuel" not in est.missing_facts
    assert build(facts(fuel=fuel, hs_code=hs)).inputs.fuel == Fuel(fuel)


@pytest.mark.parametrize(
    ("value", "precision", "declaration"),
    [
        ("0000", "year", DECL),
        ("0000-05", "month", DECL),
        ("1800-01-01", "day", DECL),
        ("1800", "year", DECL),
        ("2020", "year", date(9999, 12, 31)),
    ],
)
def test_an_implausible_first_registration_leaves_the_age_unknown_never_an_error(
    value: str, precision: str, declaration: date
) -> None:
    """Regression: year 0000 raised ValueError and an age over 200 raised a ValidationError, so
    the API and MCP answered 500 INTERNAL_ERROR (retryable)."""
    f = facts(first_registration={"value": value, "precision": precision}, declaration_date=declaration)
    built = build(f)
    assert built.inputs.vehicle_age_years is None
    assert any(w.startswith(f"FIRST_REGISTRATION_IMPLAUSIBLE:{value}") for w in built.warnings), (
        built.warnings
    )
    assert estimate(f).scenarios  # the estimate itself never raises
    if value.startswith("0000"):
        assert (
            vehicle_age_range(PartialDate(value=value, precision=Precision(precision)), declaration) is None
        )


def test_a_previous_month_customs_rate_is_named_missing_and_never_shown_as_used() -> None:
    """Regression: a stale recorded rate left every charge unknown, yet missing_facts was empty
    and the estimate presented the stale rate as the rate used for EUR equivalents."""
    september = rate("61.6", date(2026, 9, 1))
    stale = estimate(customs_rate=september)
    assert not stale.scenarios[0].complete
    assert "customs_fx_rate" in stale.missing_facts
    assert stale.customs_fx_rate is None
    assert "61.6" in stale.eur_basis and "not used" in stale.eur_basis
    assert stale.eur_basis.startswith("no customs rate effective for 2026-10")
    assert any(
        w.startswith("CUSTOMS_FX_RATE_NOT_EFFECTIVE_FOR_DECLARATION:2026-09-01") for w in stale.warnings
    )
    accepted = estimate(customs_rate=september, accept_previous_month_fx=True)
    assert accepted.scenarios[0].complete and "customs_fx_rate" not in accepted.missing_facts
    assert accepted.customs_fx_rate == september and accepted.eur_basis.startswith("EUR equivalents at")
    active = estimate(
        rule_sets=[synthetic_active()],
        jurisdiction="XX",
        customs_rate=september,
        accept_previous_month_fx=True,
    )
    assert "customs_fx_rate" in active.missing_facts and active.customs_fx_rate is None
    current = estimate()
    assert current.customs_fx_rate == rate() and "customs_fx_rate" not in current.missing_facts
