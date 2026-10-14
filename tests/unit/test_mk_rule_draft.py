"""The North Macedonian import-charge DRAFT (config/tax_rules/drafts) through the real engine.

The draft is NOT APPROVED. It is pinned here to the golden cases of the design (G01-G19,
G17b, G17c, U1-U10) and the motor-vehicle-tax (DMV) regression set of the fact sheet (section
10.3), every amount in whole MKD and computed by ``tax_engine.calculate`` on the shipped file.
Owner decisions of 2026-10-13 (draft.3): DMV is part of the import VAT base (ZDDV art.
21(2)(1)), and every MKD amount, intermediate helpers included, and the total are rounded to
whole denars half-up before they feed the next charge. The expected amounts were produced
independently (hand arithmetic and an evaluator written from the fact sheet formulas, with
exact fractions) and must never be edited to make a test pass: a disagreement is a defect in
the engine or the draft.

The band and table tests transcribe the fact sheet's tables (sections 5.4 and 5.5) here,
independently of the JSON, so a transcription error in the draft fails a test.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from suv_deals.domain.costs import tax_cost_lines
from suv_deals.domain.enums import Co2Cycle, CostCategory, CostLineStatus, Fuel, FxPurpose, TaxRuleStatus
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    Classification,
    ComponentResult,
    ComponentStatus,
    IncludedCost,
    OriginProof,
    OriginProofStatus,
    PercentageComponent,
    RuleSet,
    TaxCalculation,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    load_rule_set_file,
    select_preview_rule_set,
    select_rule_set,
    transition_rule_set,
)
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "config" / "tax_rules" / "drafts"
DRAFT = DRAFTS / "mk_passenger_car_import_2026.10-draft.3.json"
#: Content hash of the shipped draft. A changed draft is a new version, never an edit.
DRAFT_CONTENT_SHA256 = "4fe7f064f46ca8b91a90c5d799aa7c3c905973f6ea9a5dadb697490d91c83752"
CATEGORY = "passenger_car"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
DECL = date(2026, 10, 13)
#: Customs monthly EUR/MKD list from 01.10.2026 (fact sheet section 3.5).
FX = "61.5337"
DENAR = Decimal("1")

DUTY_MEMBERS = ("duty_preferential_eu", "duty_preferential_other_fta", "duty_us_origin", "duty_standard")


@functools.cache
def draft() -> RuleSet:
    return load_rule_set_file(DRAFT)


def customs_rate(rate: str = FX, rate_date: date = date(2026, 10, 1)) -> FxRate:
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal(rate),
        rate_date=rate_date,
        retrieved_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        provider="customs monthly list (test input)",
        purpose=FxPurpose.CUSTOMS,
    )


def origin(country: str | None) -> OriginProof | None:
    """``None``: no proof object (unknown); ``"none"``: positively no proof; else an accepted proof."""
    if country is None:
        return None
    if country == "none":
        return OriginProof(proof_type="none_claimed", acceptance_status=OriginProofStatus.NOT_AVAILABLE)
    return OriginProof(
        proof_type="caller_attested_proof",
        origin_country=country,
        preferential=True,
        acceptance_status=OriginProofStatus.ACCEPTED,
        evidence_ids=("caller_attestation:test",),
    )


def inputs(
    *,
    cv: str | None = "3700",
    cv_currency: str = "EUR",
    fuel: str = "diesel",
    co2: str | None = "189",
    cycle: str | None = "nedc",
    doc: str | None = "coc",
    origin_country: str | None = "none",
    hs: str | None = None,
    vat_incidental_eur: tuple[str, ...] = (),
    fx: str = FX,
    rate_date: date = date(2026, 10, 1),
    fx_valid: tuple[date, date] | None = (date(2026, 10, 1), date(2026, 10, 31)),
    declaration: date = DECL,
) -> TaxInputs:
    classification = None
    if hs is not None:
        classification = Classification(
            tariff_code=hs,
            vehicle_category=CATEGORY,
            evidence_ids=("caller_attestation:hs",),
            approval_status="approved",
            approved_by="owner (test)",
        )
    return TaxInputs(
        declaration_date=declaration,
        jurisdiction="MK",
        customs_value=None if cv is None else Money.of(cv, cv_currency),
        customs_value_basis="Царински закон чл.28 + чл.35(1)(д): invoice + transport to MK entry (test)",
        customs_fx_rate=customs_rate(fx, rate_date),
        customs_fx_valid_from=None if fx_valid is None else fx_valid[0],
        customs_fx_valid_to=None if fx_valid is None else fx_valid[1],
        origin_proof=origin(origin_country),
        fuel=Fuel(fuel),
        co2_g_km=None if co2 is None else Decimal(co2),
        co2_cycle=Co2Cycle(cycle or "unknown"),
        co2_source_document=doc,
        classification=classification,
        vat_incidental_costs=tuple(
            IncludedCost(
                label="inland transport after entry",
                amount=Money.of(amount, "EUR"),
                legal_basis="ЗДДВ чл. 21(2)(2)",
            )
            for amount in vat_incidental_eur
        ),
    )


def run(**kw: Any) -> TaxCalculation:
    return calculate(draft(), inputs(**kw), AS_OF)


def component(calc: TaxCalculation, component_id: str) -> ComponentResult:
    return next(c for c in calc.components if c.component_id == component_id)


def amounts(calc: TaxCalculation) -> dict[str, str]:
    return {c.component_id: str(c.amount.amount) for c in calc.components if c.amount is not None}


# --------------------------------------------------------------------------- the shipped draft


def test_drafts_directory_ships_only_unapproved_sourced_drafts() -> None:
    files = sorted(DRAFTS.glob("*.json"))
    assert files == [DRAFT]
    for path in files:
        rule_set = load_rule_set_file(path)  # parses, validates and verifies
        assert rule_set.status == TaxRuleStatus.DRAFT, path.name
        assert not rule_set.is_fixture, path.name
        assert rule_set.approved_by is None and rule_set.approved_at is None, path.name
        assert rule_set.review_record is None, path.name
        assert rule_set.sha256 is None, path.name  # sealed only on store or transition
        assert rule_set.sources and all(s.url.startswith("https://") for s in rule_set.sources), path.name
        assert rule_set.components, path.name
        assert "DRAFT, NOT APPROVED" in (rule_set.description or ""), path.name


def test_draft_identity_and_content_hash_are_pinned() -> None:
    rule_set = draft()
    assert rule_set.label() == "mk-passenger-car-import@2026.10-draft.3"
    assert (rule_set.jurisdiction, rule_set.currency, rule_set.vehicle_categories) == (
        "MK",
        "MKD",
        (CATEGORY,),
    )
    assert rule_set.valid_from == date(2025, 7, 1) and rule_set.valid_to is None
    assert compute_rule_set_sha256(rule_set) == DRAFT_CONTENT_SHA256
    assert len(rule_set.sources) == 8 and len(rule_set.components) == 21


@pytest.mark.parametrize("on", [date(2025, 7, 1), DECL, date(2030, 1, 1)])
def test_production_selection_never_selects_the_draft(on: date) -> None:
    selection = select_rule_set([draft()], "MK", CATEGORY, on)
    assert selection.rule_set is None and selection.mode == "active"
    assert any("not active for production (draft" in c for c in selection.considered)


def test_indicative_preview_selects_the_draft_only_inside_its_period() -> None:
    preview = select_preview_rule_set([draft()], "MK", CATEGORY, DECL)
    assert preview.rule_set == draft() and preview.mode == "indicative_preview"
    assert "INDICATIVE PREVIEW" in preview.reason and "NOT production-ready" in preview.reason
    before = select_preview_rule_set([draft()], "MK", CATEGORY, date(2025, 6, 30))
    assert before.rule_set is None
    assert select_preview_rule_set([draft()], "MK", "motorcycle", DECL).rule_set is None
    assert select_preview_rule_set([draft()], "XK", CATEGORY, DECL).rule_set is None


def test_a_revoked_copy_of_the_draft_is_never_previewed() -> None:
    revoked = transition_rule_set(draft(), TaxRuleStatus.REVOKED, at=AS_OF)
    selection = select_preview_rule_set([draft(), revoked], "MK", CATEGORY, DECL)
    assert selection.rule_set is None
    assert any("revoked" in c for c in selection.considered)


def test_the_draft_cannot_be_approved_without_a_review_record() -> None:
    under_review = transition_rule_set(draft(), TaxRuleStatus.UNDER_REVIEW, at=AS_OF)
    with pytest.raises(ValidationFailed, match="approved_by and a review_record"):
        transition_rule_set(under_review, TaxRuleStatus.APPROVED, at=AS_OF)


def test_every_uncertain_marker_is_kept() -> None:
    rule_set = draft()
    marked = {
        c.id
        for c in rule_set.components
        if "UNCERTAIN - confirm" in c.label or "UNCERTAIN - confirm" in (c.legal_reference or "")
    }
    assert marked == {
        "duty_preferential_other_fta",
        "duty_us_origin",
        "dmv_value_part",
        "dmv_co2_petrol",
        "dmv_co2_diesel",
        "dmv_co2_phev_petrol",
        "dmv_co2_phev_diesel",
        "dmv_co2_deemed_phev_petrol",
        "dmv_co2_deemed_phev_diesel",
    }
    description = rule_set.description or ""
    open_list = next(s for s in description.split(". ") if s.startswith("UNCERTAIN - confirm:"))
    still_open = (
        "СКД",
        "non-integer CO2",
        "hybrid/PHEV fuel column",
        "non-EU preferential list",
        "US-origin evidence",
    )
    for topic in still_open:
        assert topic in open_list
    # Resolved by the owner on 2026-10-13: no longer open.
    for resolved in ("rounding", "whole denars", "VAT base"):
        assert resolved not in open_list
    assert description.count("UNCERTAIN") == 2
    raw = DRAFT.read_text(encoding="utf-8")
    assert raw.count("UNCERTAIN") == 17


def test_owner_decisions_of_2026_10_13_are_encoded() -> None:
    rule_set = draft()
    description = rule_set.description or ""
    assert "Owner decisions of 2026-10-13: ДМВ is part of the import VAT base (ЗДДВ чл. 21(2)(1)" in (
        description
    )
    assert "rounded to whole denars, half-up" in description
    # D1: the VAT base sums every DMV alternative, with no UNCERTAIN marker, citing the owner.
    vat_base = next(c for c in rule_set.components if c.id == "vat_base_mkd")
    assert isinstance(vat_base, PercentageComponent) and vat_base.rate == 1
    assert {"dmv_full", "dmv_plugin_hybrid_half", "dmv_bev_not_subject"} <= set(vat_base.base)
    assert "UNCERTAIN" not in vat_base.label and "UNCERTAIN" not in (vat_base.legal_reference or "")
    assert "owner confirmation 2026-10-13" in vat_base.label
    assert "чл. 21(2)(1)" in (vat_base.legal_reference or "")
    assert "owner confirmation 2026-10-13" in (vat_base.legal_reference or "")
    # D3: whole denars, half-up, before dependents, for every component and the total.
    rules = rule_set.rounding_rules
    assert rules is not None and rules.component_default is not None and rules.total is not None
    for spec in (rules.component_default, rules.total):
        assert (spec.quantum, spec.mode, spec.stage) == (DENAR, "half_up", "before_dependents")
    assert all(c.rounding is None for c in rule_set.components)  # no component overrides the default


def test_draft_calculation_is_never_production_ready_and_never_rule_supported() -> None:
    calc = run()
    assert calc.complete and not calc.production_ready
    assert calc.rule_status == TaxRuleStatus.DRAFT and calc.rule_sha256 is None
    assert "RULE_SET_NOT_ACTIVE:draft: result is not production-ready" in calc.warnings
    lines = tax_cost_lines(calc)
    assert not any(line.rule_supported or line.assumption_approved for line in lines)
    by_category = {line.category: line for line in lines}
    assert by_category[CostCategory.OTHER_IMPORT_CHARGES].status == CostLineStatus.NOT_APPLICABLE
    assert by_category[CostCategory.IMPORT_DUTY].base == Money.of("11384", "MKD")
    assert by_category[CostCategory.MOTOR_VEHICLE_TAX].base == Money.of("96390", "MKD")
    assert by_category[CostCategory.IMPORT_VAT].base == Money.of("60381", "MKD")


# --------------------------------------------------------------------------- golden cases


@dataclass(frozen=True)
class Golden:
    case: str
    kwargs: dict[str, Any]
    #: Every RESOLVED component and its amount; every other component is not applicable.
    expected: dict[str, str]
    total: str
    bracket: str | None = None  # the value-part band chosen (law-style bounds)
    notes: tuple[str, ...] = field(default=())


def _std(
    cv_mkd: str, duty: str, vmv: str, value: str, co2_id: str, co2: str, dmv: str, vat_base: str, vat: str
) -> dict[str, str]:
    return {
        "customs_value_mkd": cv_mkd,
        "duty_standard": duty,
        "vmv_mkd": vmv,
        "dmv_value_part": value,
        co2_id: co2,
        "dmv_full": dmv,
        "vat_base_mkd": vat_base,
        "vat_import": vat,
    }


def _with_duty(member: str, rows: dict[str, str]) -> dict[str, str]:
    result = dict(rows)
    result[member] = result.pop("duty_standard")
    return result


GOLDEN: tuple[Golden, ...] = (
    Golden(
        "G01 D1 diesel NEDC 189, 3000 + 700",
        {},
        _std("227675", "11384", "239059", "0", "dmv_co2_diesel", "96390", "96390", "335449", "60381"),
        "168155",
        "(open, 10000]",
    ),
    Golden(
        "G02 D2 EU proof (DE) accepted",
        {"origin_country": "DE"},
        _with_duty(
            "duty_preferential_eu",
            _std("227675", "0", "227675", "0", "dmv_co2_diesel", "96390", "96390", "324065", "58332"),
        ),
        "154722",
    ),
    Golden(
        "G03 D3 petrol NEDC 245, 2500 + 700",
        {"cv": "3200", "fuel": "petrol", "co2": "245"},
        _std("196908", "9845", "206753", "0", "dmv_co2_petrol", "144550", "144550", "351303", "63235"),
        "217630",
    ),
    Golden(
        "G04 D4 diesel, CO2 not established (none_available)",
        {"cv": "3500", "co2": None, "cycle": None, "doc": "none_available"},
        _std(
            "215368", "10768", "226136", "0", "dmv_co2_deemed_diesel", "352000", "352000", "578136", "104064"
        ),
        "466832",
    ),
    Golden(
        "G05 D5 diesel NEDC 170, 550 in CV + 150 inland (VAT base only)",
        {"cv": "3550", "co2": "170", "vat_incidental_eur": ("150",)},
        _std("218445", "10922", "229367", "0", "dmv_co2_diesel", "63750", "63750", "302347", "54422"),
        "129094",
    ),
    Golden(
        "G06 D6 risk: catalogue 6000 + 700",
        {"cv": "6700"},
        _std("412276", "20614", "432890", "0", "dmv_co2_diesel", "96390", "96390", "529280", "95270"),
        "212274",
    ),
    Golden(
        "G07 petrol, COC NEDC 130 + WLTP 155: WLTP is used",
        {"fuel": "petrol", "co2": "155", "cycle": "wltp"},
        _std("227675", "11384", "239059", "0", "dmv_co2_petrol", "16275", "16275", "255334", "45960"),
        "73619",
    ),
    Golden(
        "G08 origin EU, CV exactly 10000: the upper bound is inclusive",
        {"cv": "10000", "co2": "150", "origin_country": "EU"},
        _with_duty(
            "duty_preferential_eu",
            _std("615337", "0", "615337", "0", "dmv_co2_diesel", "40500", "40500", "655837", "118051"),
        ),
        "158551",
        "(open, 10000]",
    ),
    Golden(
        "G09 origin IT, CV 10001",
        {"cv": "10001", "co2": "150", "origin_country": "IT"},
        _with_duty(
            "duty_preferential_eu",
            _std("615399", "0", "615399", "6154", "dmv_co2_diesel", "40500", "46654", "662053", "119170"),
        ),
        "165824",
        "(10000, 12000]",
    ),
    Golden(
        "G10 standard duty, CV 9600 (VMV 10080)",
        {"cv": "9600", "co2": "150"},
        _std("590724", "29536", "620260", "6203", "dmv_co2_diesel", "40500", "46703", "666963", "120053"),
        "196292",
        "(10000, 12000]",
    ),
    Golden(
        "G11 fact sheet 10.3 #18: FX 61.5, origin AT, CV 12500",
        {"fx": "61.5", "cv": "12500", "co2": "150", "origin_country": "AT"},
        _with_duty(
            "duty_preferential_eu",
            _std("768750", "0", "768750", "15375", "dmv_co2_diesel", "40500", "55875", "824625", "148433"),
        ),
        "204308",
        "(12000, 14000]",
    ),
    Golden(
        "G12 plug-in hybrid HS 870360, NEDC 50: 50 % of the tax",
        {"fuel": "plugin_hybrid", "hs": "870360", "co2": "50"},
        {
            "customs_value_mkd": "227675",
            "duty_standard": "11384",
            "vmv_mkd": "239059",
            "dmv_value_part": "0",
            "dmv_co2_phev_petrol": "400",
            "dmv_plugin_hybrid_half": "200",
            "vat_base_mkd": "239259",
            "vat_import": "43067",
        },
        "54651",
    ),
    Golden(
        "G13 battery-electric: no CO2 document needed, not subject",
        {"fuel": "electric", "co2": None, "cycle": None, "doc": None},
        {
            "customs_value_mkd": "227675",
            "duty_standard": "11384",
            "vmv_mkd": "239059",
            "dmv_value_part": "0",
            "dmv_co2_bev_none": "0",
            "dmv_bev_not_subject": "0",
            "vat_base_mkd": "239059",
            "vat_import": "43031",
        },
        "54415",
    ),
    Golden(
        "G14 full hybrid (petrol) NEDC 118",
        {"fuel": "hybrid_petrol", "co2": "118"},
        _std("227675", "11384", "239059", "0", "dmv_co2_petrol", "8850", "8850", "247909", "44624"),
        "64858",
    ),
    Golden(
        "G15 diesel NEDC 191: the printed lower figure is inclusive",
        {"co2": "191"},
        _std("227675", "11384", "239059", "0", "dmv_co2_diesel", "115555", "115555", "354614", "63831"),
        "190770",
    ),
    Golden(
        "G16 diesel, WLTP-only COC 160",
        {"co2": "160", "cycle": "wltp"},
        _std("227675", "11384", "239059", "0", "dmv_co2_diesel", "31200", "31200", "270259", "48647"),
        "91231",
    ),
    # The value-part edge in whole denars: CV and duty are assessed in whole MKD before the
    # VMV key is converted, so the smallest VMV over EUR 10,000 at 61.5337 is 615,338 MKD.
    Golden(
        "G17 value-part edge, standard duty, CV 9523.82 (VMV 615338 MKD, key just above 10000)",
        {"cv": "9523.82"},
        _std("586036", "29302", "615338", "6153", "dmv_co2_diesel", "96390", "102543", "717881", "129219"),
        "261064",
        "(10000, 12000]",
    ),
    Golden(
        "G17b same, CV 9523.79 (VMV 615336 MKD, key just below 10000)",
        {"cv": "9523.79"},
        _std("586034", "29302", "615336", "0", "dmv_co2_diesel", "96390", "96390", "711726", "128111"),
        "253803",
        "(open, 10000]",
    ),
    Golden(
        "G17c same, CV 9523.81: whole denars put the key on exactly 10000 (0 %; 1 % with 0.01 MKD)",
        {"cv": "9523.81"},
        _std("586035", "29302", "615337", "0", "dmv_co2_diesel", "96390", "96390", "711727", "128111"),
        "253803",
        "(open, 10000]",
    ),
    Golden(
        "G18 US-origin evidence accepted",
        {"origin_country": "US"},
        _with_duty(
            "duty_us_origin",
            _std("227675", "0", "227675", "0", "dmv_co2_diesel", "96390", "96390", "324065", "58332"),
        ),
        "154722",
    ),
    Golden(
        "G19 accepted proof of a non-FTA origin (JP): standard duty",
        {"origin_country": "JP"},
        _std("227675", "11384", "239059", "0", "dmv_co2_diesel", "96390", "96390", "335449", "60381"),
        "168155",
    ),
)


@pytest.mark.parametrize("golden", GOLDEN, ids=[g.case.split(" ")[0] for g in GOLDEN])
def test_golden_case(golden: Golden) -> None:
    calc = run(**golden.kwargs)
    assert calc.complete, (calc.unknown_components, calc.missing_inputs, calc.warnings)
    resolved = {c.component_id for c in calc.components if c.status == ComponentStatus.RESOLVED}
    assert resolved == set(golden.expected)
    for comp in calc.components:
        if comp.component_id in golden.expected:
            assert comp.amount is not None
            assert comp.amount.currency == "MKD"
            assert comp.amount.amount == Decimal(golden.expected[comp.component_id]), comp.component_id
            assert comp.amount.amount == comp.amount.amount.to_integral_value(), comp.component_id  # D3
        else:
            assert comp.status == ComponentStatus.NOT_APPLICABLE, comp.component_id
    assert calc.total_import_cost == Money.of(golden.total, "MKD")
    assert calc.total_import_cost.amount.as_tuple().exponent == 0  # whole denars, no decimals
    assert calc.known_subtotal is None and calc.unknown_components == () and calc.missing_inputs == ()

    # The total is duty + DMV + VAT; helpers (customs value, VMV, value part, CO2 part, VAT base)
    # are reported but never counted.
    final = [c.amount.amount for c in calc.components if c.amount is not None and not c.intermediate]
    assert sum(final, Decimal(0)) == Decimal(golden.total)
    totals = calc.category_totals()
    duty = next(golden.expected[m] for m in DUTY_MEMBERS if m in golden.expected)
    dmv = next(
        golden.expected[m]
        for m in ("dmv_full", "dmv_plugin_hybrid_half", "dmv_bev_not_subject")
        if m in golden.expected
    )
    assert totals[CostCategory.IMPORT_DUTY].amount == Money.of(duty, "MKD")
    assert totals[CostCategory.MOTOR_VEHICLE_TAX].amount == Money.of(dmv, "MKD")
    assert totals[CostCategory.IMPORT_VAT].amount == Money.of(golden.expected["vat_import"], "MKD")
    if golden.bracket is not None:
        assert component(calc, "dmv_value_part").inputs_used["bracket"] == golden.bracket
    assert not calc.production_ready


def test_value_part_key_is_vmv_converted_to_eur_with_the_customs_rate() -> None:
    at_threshold = component(run(cv="10000", co2="150", origin_country="EU"), "dmv_value_part")
    assert at_threshold.inputs_used["key"].startswith("615337 MKD -> 10000 EUR (customs EUR/MKD=61.5337")
    above = component(run(cv="9523.82"), "dmv_value_part")
    assert above.inputs_used["key"].startswith("615338 MKD -> 10000.01625")
    below = component(run(cv="9523.79"), "dmv_value_part")
    assert below.inputs_used["key"].startswith("615336 MKD -> 9999.98374")
    # The key is the ASSESSED (whole-denar) customs value + duty: 586,035.27 -> 586,035 and
    # 29,301.75 -> 29,302 give exactly 615,337 MKD = EUR 10,000 (0 %), where the unrounded
    # 615,337.03 MKD was just over (1 %).
    on_edge = component(run(cv="9523.81"), "dmv_value_part")
    assert on_edge.inputs_used["key"].startswith("615337 MKD -> 10000 EUR")
    assert on_edge.inputs_used["bracket"] == "(open, 10000]"


def test_inland_leg_enters_the_vat_base_converted_unrounded() -> None:
    calc = run(cv="3550", co2="170", vat_incidental_eur=("150",))
    used = component(calc, "vat_base_mkd").inputs_used["vat_incidental_costs_total"]
    assert used.startswith("9230.055")
    assert component(calc, "vat_base_mkd").amount == Money.of("302347", "MKD")  # 302,346.055 half-up
    no_inland = run(cv="3550", co2="170")
    assert component(no_inland, "vat_base_mkd").amount == Money.of("293117", "MKD")


# --------------------------------------------------------------------------- DMV regression set


@pytest.mark.parametrize(
    ("fuel", "cycle", "co2", "helper", "dmv"),
    [
        # fact sheet 10.3, VMV <= EUR 10,000 (value part 0)
        ("diesel", "nedc", "189", "dmv_co2_diesel", "96390"),  # 1
        ("diesel", "nedc", "231", "dmv_co2_diesel", "173250"),  # 2
        ("diesel", "nedc", "263", "dmv_co2_diesel", "231440"),  # 3
        ("diesel", "nedc", "145", "dmv_co2_diesel", "39150"),  # 4
        ("diesel", "nedc", "118", "dmv_co2_diesel", "17700"),  # 5
        ("diesel", "nedc", "170", "dmv_co2_diesel", "63750"),  # 6
        ("diesel", "nedc", "190", "dmv_co2_diesel", "96900"),  # 7
        ("diesel", "nedc", "191", "dmv_co2_diesel", "115555"),  # 8
        ("diesel", "nedc", "260", "dmv_co2_diesel", "228800"),  # 9
        ("petrol", "nedc", "245", "dmv_co2_petrol", "144550"),  # 10
        ("petrol", "nedc", "130", "dmv_co2_petrol", "9750"),  # 11
        ("hybrid_petrol", "nedc", "118", "dmv_co2_petrol", "8850"),  # 12
        ("diesel", "wltp", "160", "dmv_co2_diesel", "31200"),  # 16
        ("petrol", "wltp", "155", "dmv_co2_petrol", "16275"),  # 17
        ("lpg", "nedc", "130", "dmv_co2_petrol", "9750"),
        ("cng", "wltp", "155", "dmv_co2_petrol", "16275"),
        ("hybrid_diesel", "nedc", "145", "dmv_co2_diesel", "39150"),
    ],
)
def test_dmv_regression(fuel: str, cycle: str, co2: str, helper: str, dmv: str) -> None:
    calc = run(fuel=fuel, cycle=cycle, co2=co2)
    assert calc.complete
    assert component(calc, helper).amount == Money.of(dmv, "MKD")
    assert component(calc, "dmv_full").amount == Money.of(dmv, "MKD")
    assert component(calc, "dmv_value_part").amount == Money.of("0", "MKD")


def test_dmv_regression_special_drive_types() -> None:
    phev = run(fuel="plugin_hybrid", hs="870360", co2="50")  # 13
    assert component(phev, "dmv_co2_phev_petrol").amount == Money.of("400", "MKD")
    assert component(phev, "dmv_plugin_hybrid_half").amount == Money.of("200", "MKD")
    phev_diesel = run(fuel="plugin_hybrid", hs="870370", co2="50")
    assert component(phev_diesel, "dmv_co2_phev_diesel").amount == Money.of("750", "MKD")
    assert component(phev_diesel, "dmv_plugin_hybrid_half").amount == Money.of("375", "MKD")
    bev = run(fuel="electric", co2="0", doc="coc")  # 19
    assert component(bev, "dmv_bev_not_subject").amount == Money.of("0", "MKD")
    assert component(bev, "dmv_co2_bev_none").amount == Money.of("0", "MKD")
    deemed_diesel = run(co2=None, cycle=None, doc="none_available")  # 14
    assert component(deemed_diesel, "dmv_full").amount == Money.of("352000", "MKD")
    deemed_petrol = run(fuel="petrol", co2=None, cycle=None, doc="none_available")  # 15
    assert component(deemed_petrol, "dmv_full").amount == Money.of("278000", "MKD")
    # A CO2 figure next to "no document" never replaces the deemed value.
    assert component(run(doc="none_available"), "dmv_full").amount == Money.of("352000", "MKD")
    with_value_part = run(fx="61.5", cv="12500", co2="150", origin_country="AT")  # 18
    assert component(with_value_part, "dmv_full").amount == Money.of("55875", "MKD")


# Fact sheet section 5.5 (Table 2 per Sl. vesnik 220/2023), transcribed independently of the
# JSON: (WLTP lower bound, NEDC lower bound, petrol/LPG/CNG coefficient, diesel coefficient).
TABLE_2 = (
    (0, 0, 0, 0),
    (1, 1, 8, 15),
    (64, 51, 15, 30),
    (95, 76, 30, 60),
    (114, 91, 45, 90),
    (124, 101, 60, 120),
    (136, 111, 75, 150),
    (154, 131, 105, 195),
    (161, 141, 180, 270),
    (173, 151, 265, 375),
    (189, 171, 390, 510),
    (211, 191, 495, 605),
    (250, 226, 590, 750),
    (283, 256, 695, 880),
)


def _table_2_cases() -> list[tuple[str, str, int, int]]:
    cases: list[tuple[str, str, int, int]] = []
    for index, (wltp, nedc, petrol, diesel) in enumerate(TABLE_2):
        for cycle, lower in (("wltp", wltp), ("nedc", nedc)):
            for fuel, coefficient, column in (("petrol", petrol, 2), ("diesel", diesel, 3)):
                cases.append((fuel, cycle, lower, coefficient))
                if index:  # the integer just below the printed lower figure is in the band below
                    cases.append((fuel, cycle, lower - 1, TABLE_2[index - 1][column]))
    for fuel, column in (("petrol", 2), ("diesel", 3)):
        cases.extend((fuel, cycle, 1000, TABLE_2[-1][column]) for cycle in ("wltp", "nedc"))
    return cases


@pytest.mark.parametrize(("fuel", "cycle", "co2", "coefficient"), _table_2_cases())
def test_table_2_band_edges(fuel: str, cycle: str, co2: int, coefficient: int) -> None:
    calc = run(fuel=fuel, cycle=cycle, co2=str(co2))
    expected = Decimal(co2) * coefficient
    assert component(calc, f"dmv_co2_{fuel}").amount == Money(amount=expected, currency="MKD")


# Fact sheet section 5.4 (Table 1, Uredba 277/2019): "over X up to Y" in EUR on the whole VMV.
TABLE_1 = (
    (10000, "0"),
    (12000, "0.01"),
    (14000, "0.02"),
    (16000, "0.04"),
    (18000, "0.06"),
    (20000, "0.08"),
    (23000, "0.10"),
    (26000, "0.12"),
    (29000, "0.14"),
    (32000, "0.16"),
    (35000, "0.17"),
    (40000, "0.18"),
    (50000, "0.19"),
)
TOP_RATE = "0.20"


def _table_1_cases() -> list[tuple[int, str]]:
    cases: list[tuple[int, str]] = []
    rates = [rate for _, rate in TABLE_1[1:]] + [TOP_RATE]
    for (upper, rate), above in zip(TABLE_1, rates, strict=True):
        cases.append((upper, rate))  # exactly "up to" belongs to the band
        cases.append((upper + 1, above))  # one euro over belongs to the next band
    cases.append((90000, TOP_RATE))
    return cases


@pytest.mark.parametrize(("vmv_eur", "rate"), _table_1_cases())
def test_table_1_value_part_bands(vmv_eur: int, rate: str) -> None:
    # Duty 0 (accepted EU proof) and FX 61.5: the customs value is rounded to whole denars
    # (exact for an even EUR amount, +0.5 MKD for an odd one), so the EUR key is vmv_eur or
    # vmv_eur + 0.5/61.5 and stays in the band under test.
    calc = run(fx="61.5", cv=str(vmv_eur), co2="150", origin_country="DE")
    vmv_mkd = (Decimal(vmv_eur) * Decimal("61.5")).quantize(DENAR, rounding=ROUND_HALF_UP)
    expected = (vmv_mkd * Decimal(rate)).quantize(DENAR, rounding=ROUND_HALF_UP)
    assert component(calc, "vmv_mkd").amount == Money(amount=vmv_mkd, currency="MKD")
    assert component(calc, "dmv_value_part").amount == Money(amount=expected, currency="MKD")
    assert component(calc, "dmv_full").amount == Money(amount=expected + 40500, currency="MKD")


# --------------------------------------------------------------------------- unknown and incomplete


def _incomplete(calc: TaxCalculation, unknown: list[str]) -> None:
    assert not calc.complete
    assert calc.total_import_cost is None
    assert list(calc.unknown_components) == unknown
    assert "INCOMPLETE: only known_subtotal is available; it is not a total import cost" in calc.warnings
    assert not calc.production_ready


def test_u1_co2_document_unknown() -> None:
    calc = run(doc=None)
    _incomplete(calc, ["dmv_co2_diesel", "dmv_co2_deemed_diesel", "dmv_full", "vat_base_mkd", "vat_import"])
    assert component(calc, "duty_standard").amount == Money.of("11384", "MKD")
    assert calc.known_subtotal == Money.of("11384", "MKD")  # duty only; helpers never count
    assert "co2_source_document" in component(calc, "dmv_co2_diesel").missing_inputs
    assert calc.category_totals()[CostCategory.MOTOR_VEHICLE_TAX].status == ComponentStatus.UNKNOWN


def test_u2_no_origin_proof_object() -> None:
    calc = run(origin_country=None)
    _incomplete(
        calc,
        [
            *DUTY_MEMBERS,
            "vmv_mkd",
            "dmv_value_part",
            "dmv_full",
            "vat_base_mkd",
            "vat_import",
        ],
    )
    assert calc.missing_inputs == ("preferential_origin_country",)
    assert calc.known_subtotal is None
    assert any(w.startswith("ORIGIN_PROOF_MISSING") for w in component(calc, "duty_standard").warnings)
    # The CO2 helper still resolves; nothing else is guessed.
    assert component(calc, "dmv_co2_diesel").amount == Money.of("96390", "MKD")


def test_u3_non_integer_co2_is_unknown_not_the_lower_band() -> None:
    calc = run(co2="190.5")
    _incomplete(calc, ["dmv_co2_diesel", "dmv_full", "vat_base_mkd", "vat_import"])
    assert component(calc, "dmv_co2_diesel").warnings == ("VALUE_NOT_INTEGER_NO_BAND_RULE:co2_g_km=190.5",)
    assert calc.known_subtotal == Money.of("11384", "MKD")


def test_u4_plugin_hybrid_without_a_classification() -> None:
    calc = run(fuel="plugin_hybrid", co2="50")
    _incomplete(
        calc,
        [
            "dmv_co2_phev_petrol",
            "dmv_co2_phev_diesel",
            "dmv_plugin_hybrid_half",
            "vat_base_mkd",
            "vat_import",
        ],
    )
    assert "classification" in component(calc, "dmv_co2_phev_petrol").missing_inputs


def test_u4b_plugin_hybrid_with_another_8703_subheading_is_a_gap() -> None:
    calc = run(fuel="plugin_hybrid", co2="50", hs="870340")
    assert not calc.complete
    assert any(
        w.startswith("ALTERNATIVE_GROUP_GAP:dmv_co2") for w in component(calc, "dmv_co2_phev_petrol").warnings
    )


def test_u5_fuel_other_leaves_both_dmv_groups_with_a_gap() -> None:
    calc = run(fuel="other")
    _incomplete(
        calc,
        [
            "dmv_co2_petrol",
            "dmv_co2_diesel",
            "dmv_co2_phev_petrol",
            "dmv_co2_phev_diesel",
            "dmv_co2_deemed_petrol",
            "dmv_co2_deemed_diesel",
            "dmv_co2_deemed_phev_petrol",
            "dmv_co2_deemed_phev_diesel",
            "dmv_co2_bev_none",
            "dmv_full",
            "dmv_plugin_hybrid_half",
            "dmv_bev_not_subject",
            "vat_base_mkd",
            "vat_import",
        ],
    )
    warnings = component(calc, "dmv_full").warnings
    assert any(w.startswith("ALTERNATIVE_GROUP_GAP:dmv:") for w in warnings)


def test_u5b_unknown_fuel_is_a_missing_required_input() -> None:
    calc = run(fuel="unknown")
    assert not calc.complete and "fuel" in calc.missing_inputs


def test_u6_unknown_co2_cycle() -> None:
    calc = run(cycle=None)
    _incomplete(calc, ["dmv_co2_diesel", "dmv_full", "vat_base_mkd", "vat_import"])
    helper = component(calc, "dmv_co2_diesel")
    assert "co2_cycle" in helper.missing_inputs
    assert "CO2_CYCLE_UNKNOWN: no WLTP/NEDC conversion is performed" in helper.warnings


def test_u7_correlated_nedc_is_never_converted() -> None:
    calc = run(cycle="nedc_correlated")
    _incomplete(calc, ["dmv_co2_diesel", "dmv_full", "vat_base_mkd", "vat_import"])
    assert any(
        w.startswith("CO2_CYCLE_NOT_SUPPORTED:nedc_correlated")
        for w in component(calc, "dmv_co2_diesel").warnings
    )


def test_u8_customs_value_missing() -> None:
    calc = run(cv=None)
    _incomplete(
        calc,
        [
            "customs_value_mkd",
            "duty_standard",
            "vmv_mkd",
            "dmv_value_part",
            "dmv_full",
            "vat_base_mkd",
            "vat_import",
        ],
    )
    assert calc.missing_inputs == ("customs_value",)
    assert component(calc, "dmv_co2_diesel").amount == Money.of("96390", "MKD")
    assert any(w.startswith("CUSTOMS_VALUE_MISSING") for w in component(calc, "customs_value_mkd").warnings)


def test_u9_co2_document_outside_the_closed_code_set() -> None:
    calc = run(doc="seller_email")
    _incomplete(
        calc,
        [
            "dmv_co2_petrol",
            "dmv_co2_diesel",
            "dmv_co2_phev_petrol",
            "dmv_co2_phev_diesel",
            "dmv_co2_deemed_petrol",
            "dmv_co2_deemed_diesel",
            "dmv_co2_deemed_phev_petrol",
            "dmv_co2_deemed_phev_diesel",
            "dmv_co2_bev_none",
            "dmv_full",
            "vat_base_mkd",
            "vat_import",
        ],
    )


CV_CHAIN = [
    "customs_value_mkd",
    "duty_standard",
    "vmv_mkd",
    "dmv_value_part",
    "dmv_full",
    "vat_base_mkd",
    "vat_import",
]


def test_u10_customs_rate_dated_after_the_declaration() -> None:
    calc = run(rate_date=date(2026, 10, 14), fx_valid=(date(2026, 10, 14), date(2026, 10, 31)))
    _incomplete(calc, CV_CHAIN)
    assert component(calc, "customs_value_mkd").warnings == (
        "CUSTOMS_FX_RATE_AFTER_DECLARATION:2026-10-14>2026-10-13",
    )
    assert calc.customs_fx_rate is None


def test_u10_customs_rate_outside_its_monthly_window() -> None:
    calc = run(rate_date=date(2026, 9, 1), fx_valid=(date(2026, 9, 1), date(2026, 9, 30)))
    _incomplete(calc, CV_CHAIN)
    assert component(calc, "customs_value_mkd").warnings == ("CUSTOMS_FX_RATE_NOT_EFFECTIVE",)


def test_u10_customs_value_in_mkd_still_needs_the_rate_for_the_value_part_key() -> None:
    calc = run(cv="227674.69", cv_currency="MKD", rate_date=date(2026, 10, 14), fx_valid=None)
    _incomplete(calc, ["dmv_value_part", "dmv_full", "vat_base_mkd", "vat_import"])
    customs_value = component(calc, "customs_value_mkd")
    assert customs_value.amount == Money.of("227675", "MKD")  # whole denars, half-up
    assert customs_value.unrounded_amount == Decimal("227674.69")
    part = component(calc, "dmv_value_part")
    assert "customs_fx_rate" in part.missing_inputs
    assert any(w.startswith("CUSTOMS_FX_RATE_AFTER_DECLARATION") for w in part.warnings)


def test_a_reference_rate_never_replaces_the_customs_rate() -> None:
    reference = customs_rate().model_copy(update={"purpose": FxPurpose.REFERENCE})
    calc = calculate(draft(), inputs().model_copy(update={"customs_fx_rate": reference}), AS_OF)
    assert not calc.complete and "customs_fx_rate" in calc.missing_inputs
    assert any(
        w.startswith("CUSTOMS_FX_RATE_WRONG_PURPOSE") for w in component(calc, "customs_value_mkd").warnings
    )
