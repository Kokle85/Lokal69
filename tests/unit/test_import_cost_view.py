"""Import-cost calculator: the wire view (``views.import_costs``), the pure parts of the DB-backed
service (``persistence.queries.import_costs``: fact merging, draft files, rule selection) and the
builder additions the valuation pipeline relies on. SYNTHETIC inputs; the MK amounts are the
golden cases of the shipped DRAFT (never approved here except as labelled XX copies)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tests.unit.test_import_estimate import (
    AS_OF,
    BANNER,
    DRAFT,
    draft,
    estimate,
    facts,
    rate,
    synthetic_active,
    transport_line,
)

from suv_deals.domain.costs import CostLine
from suv_deals.domain.enums import (
    Co2Cycle,
    CostCategory,
    CostLineStatus,
    EligibilityState,
    Fuel,
    Precision,
    TaxRuleStatus,
)
from suv_deals.domain.import_estimate import (
    VehicleImportFacts,
    build_tax_inputs,
    facts_from_listing,
    single_transport_line,
)
from suv_deals.domain.listings import (
    Co2Info,
    Documentation,
    LocationInfo,
    NormalizedListing,
    PartialDate,
    VehicleSpec,
)
from suv_deals.domain.money import Money
from suv_deals.domain.tax_engine import RuleSet, transition_rule_set
from suv_deals.domain.valuation import ScreeningInput, assemble_valuation
from suv_deals.persistence.queries.import_costs import (
    COST_PROFILE_FILE,
    DRAFT_RULES_DIR,
    ImportCostRequest,
    ImportCostResult,
    import_cost_warnings,
    load_draft_rule_sets,
    merge_facts,
    select_calculator_rule_set,
)
from suv_deals.persistence.valuation_repo import StoredRuleSet
from suv_deals.views.common import envelope_model_for
from suv_deals.views.import_costs import FIXTURE_LABEL, ImportCostEstimateView, ImportListingView
from suv_deals.workers.runtime import DEFAULT_COST_PROFILE

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"
DECL = date(2026, 10, 13)


def view_of(est: Any, f: VehicleImportFacts | None = None, **kw: Any) -> ImportCostEstimateView:
    options: dict[str, Any] = {
        "facts": f or facts(),
        "fact_sources": {"price": "request"},
        "customs_fx_source": "recorded",
    }
    options.update(kw)
    return ImportCostEstimateView.of(est, **options)


def stored(rule_set: RuleSet, *, created: datetime = AS_OF) -> StoredRuleSet:
    return StoredRuleSet(
        rule_set=rule_set,
        rows=(),
        approved_by_principal=None,
        created_by=None,
        created_at=created,
        updated_at=created,
    )


# --------------------------------------------------------------------------- the wire view


def test_preview_view_names_its_totals_indicative_and_flags_helpers() -> None:
    view = view_of(estimate())
    assert view.mode == "indicative_preview" and not view.production_ready
    assert view.banner is not None and f"{view.banner.mk} / {view.banner.en}" == BANNER
    assert view.rule is not None and view.rule.status == TaxRuleStatus.DRAFT and not view.rule.sha256_recorded
    legal = view.scenarios[0]
    assert legal.name == "legal_base" and legal.complete
    assert legal.total is None and legal.total_eur is None and legal.landed_cost_eur is None
    assert legal.indicative_total is not None and legal.indicative_total.amount == "168154.45"
    assert legal.indicative_total.currency == "MKD" and legal.known_subtotal is None
    assert legal.indicative_total_eur is not None and legal.indicative_total_eur.amount == "2732.72"
    flags = {c.component_id: c.intermediate for c in legal.components}
    assert flags["customs_value_mkd"] and flags["vmv_mkd"] and flags["vat_base_mkd"]
    assert not flags["duty_standard"] and not flags["dmv_full"] and not flags["vat_import"]
    categories = {c.category: c for c in legal.categories}
    assert categories[CostCategory.IMPORT_DUTY].amount.amount == "11383.73"
    assert categories[CostCategory.OTHER_IMPORT_CHARGES].status == "not_applicable"
    assert categories[CostCategory.OTHER_IMPORT_CHARGES].amount.amount is None
    # Decimals are strings on the wire; the view round-trips through its own JSON.
    data = view.model_dump(mode="json")
    assert ImportCostEstimateView.model_validate(data) == view
    assert data["scenarios"][0]["customs_value"] == {
        "status": "known",
        "amount": "3700.00",
        "currency": "EUR",
        "reason": None,
    }


def test_active_view_carries_final_totals_and_production_readiness() -> None:
    est = estimate(rule_sets=[synthetic_active()], jurisdiction="XX")
    view = view_of(est)
    legal = view.scenarios[0]
    assert view.mode == "active" and view.production_ready and view.banner is None
    assert legal.total is not None and legal.total.amount == "168154.45" and legal.indicative_total is None
    assert legal.production_ready


def test_incomplete_and_unavailable_views_show_unknown_never_zero() -> None:
    unknown_co2 = estimate(facts(co2_document=None))
    legal = view_of(unknown_co2).scenarios[0]
    assert not legal.complete and legal.indicative_total is not None
    assert legal.indicative_total.status == "unknown" and legal.indicative_total.amount is None
    assert legal.indicative_total.reason is not None and "incomplete" in legal.indicative_total.reason
    assert legal.known_subtotal is not None and legal.known_subtotal.amount == "11383.73"
    none = view_of(estimate(rule_sets=[]))
    assert none.mode == "unavailable" and none.scenarios == () and none.rule is None
    assert none.banner is not None and "unknown" in none.banner.en
    assert none.missing_facts[0] == "tax_rule_set"
    codes = [w.code.value for w in import_cost_warnings(none)]
    assert codes == ["TAX_RULES_UNAPPROVED", "UNKNOWN_COSTS"]


def test_view_invariants_refuse_mislabelled_results() -> None:
    data = view_of(estimate()).model_dump(mode="json")
    scenario = data["scenarios"][0]
    with pytest.raises(ValidationError, match="indicative_total"):
        final = {
            **scenario,
            "total": scenario["indicative_total"],
            "total_eur": scenario["indicative_total_eur"],
        }
        final["landed_cost_eur"] = scenario["indicative_landed_cost_eur"]
        final.update(indicative_total=None, indicative_total_eur=None, indicative_landed_cost_eur=None)
        ImportCostEstimateView.model_validate({**data, "scenarios": [final]})
    with pytest.raises(ValidationError, match="never production-ready"):
        ImportCostEstimateView.model_validate({**data, "production_ready": True})
    with pytest.raises(ValidationError, match="banner"):
        ImportCostEstimateView.model_validate({**data, "banner": None})
    with pytest.raises(ValidationError, match="either the final totals or the indicative"):
        ImportCostEstimateView.model_validate(
            {**data, "scenarios": [{**scenario, "total": scenario["indicative_total"]}]}
        )
    with pytest.raises(ValidationError, match="known exactly when"):
        broken = {**scenario, "complete": False, "known_subtotal": scenario["indicative_total"]}
        ImportCostEstimateView.model_validate({**data, "scenarios": [broken]})
    with pytest.raises(ValidationError, match="an unavailable estimate has no rule"):
        ImportCostEstimateView.model_validate({**data, "mode": "unavailable"})
    with pytest.raises(ValidationError, match="ACTIVE rule set"):
        ImportCostEstimateView.model_validate({**data, "mode": "active"})
    with pytest.raises(ValidationError, match="fixture-lineage"):
        listing = {
            "listing_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_number": 1,
            "source_key": "synthetic_src",
            "is_fixture": True,
            "fixture_label": None,
            "screened": True,
        }
        ImportCostEstimateView.model_validate({**data, "listing": listing})


def test_published_schema_is_closed_and_has_no_json_numbers() -> None:
    schema = envelope_model_for(ImportCostEstimateView).model_json_schema(mode="serialization")
    text = json.dumps(schema)
    assert '"number"' not in text
    data = schema["$defs"]["ImportCostEstimateView"]
    assert data["additionalProperties"] is False and set(data["required"]) == set(data["properties"])


def test_typed_warnings_label_preview_claims_and_fixtures() -> None:
    est = estimate(facts(price_basis="advertised"))
    listing = ImportListingView(
        listing_id=uuid4(),
        revision_id=uuid4(),
        revision_number=2,
        source_key="fixture_dealer_de",
        is_fixture=True,
        fixture_label=FIXTURE_LABEL,
        screened=True,
    )
    view = view_of(est, listing=listing)
    warnings = {w.code.value: w.message for w in import_cost_warnings(view)}
    assert warnings["TAX_RULES_UNAPPROVED"].startswith("INDICATIVE PREVIEW")
    assert "FIXTURE_DATA" in warnings and "UNKNOWN_COSTS" not in warnings
    assert "advertised price" in warnings["SELLER_CLAIMS_UNVERIFIED"]


# --------------------------------------------------------------------------- the service's pure parts


def _listing(**kw: Any) -> NormalizedListing:
    values: dict[str, Any] = {
        "source_key": "synthetic_src",
        "source_listing_id": "SYN-1",
        "canonical_url": "https://synthetic-dealer.example/vehicles/SYN-1",
        "observed_at": AS_OF,
        "location": LocationInfo(country="DE"),
        "vehicle": VehicleSpec(
            fuel=Fuel.DIESEL,
            first_registration=PartialDate(value="2012-05", precision=Precision.MONTH),
            mileage_km=Decimal("180000"),
            engine_displacement_cm3=1995,
            power_kw=130,
        ),
        "co2": Co2Info(g_per_km=Decimal("189"), cycle=Co2Cycle.NEDC),
        "documentation": Documentation(emissions_class="Euro 5"),
        "parser_version": "synthetic_parser@1.0.0",
    }
    values.update(kw)
    return NormalizedListing(**values)


def test_merge_keeps_listing_facts_and_applies_only_set_request_fields() -> None:
    listed = facts_from_listing(_listing(), payable=Money.of("3000", "EUR"))
    override = VehicleImportFacts.model_validate({"co2_document": "coc", "declaration_date": "2026-10-13"})
    merged, sources = merge_facts(listed, override)
    assert merged.co2 == listed.co2 and merged.co2_document == "coc" and merged.fuel == Fuel.DIESEL
    assert merged.price == listed.price and merged.price_basis == "advertised"
    assert merged.engine_displacement_cm3 == Decimal(1995) and merged.power_kw == Decimal(130)
    assert sources["co2"] == "listing" and sources["co2_document"] == "request"
    assert sources["price"] == sources["price_basis"] == "listing"
    assert sources["declaration_date"] == "request" and sources["origin_proof"] == "default"
    # Caller readings never inherit the listing's claim label; a caller price is advertised
    # unless the caller states the basis.
    readings = VehicleImportFacts.model_validate(
        {"co2": [{"cycle": "wltp", "g_km": "160"}], "price": {"amount": "2800", "currency": "EUR"}}
    )
    merged, sources = merge_facts(listed, readings)
    assert merged.co2_document is None and sources["co2_document"] == "default"
    assert merged.price_basis == "advertised" and sources["price_basis"] == "default"
    invoice = VehicleImportFacts.model_validate(
        {"price": {"amount": "2800", "currency": "EUR"}, "price_basis": "invoice"}
    )
    assert merge_facts(listed, invoice)[0].price_basis == "invoice"
    alone, sources = merge_facts(None, facts())
    assert alone == facts() and "listing" not in sources.values()
    unpriced = facts_from_listing(_listing(), payable=None)
    assert merge_facts(unpriced, None)[1]["price_basis"] == "default"


def test_request_needs_something_to_estimate_and_refuses_floats() -> None:
    with pytest.raises(ValidationError, match="listing_id, vehicle facts or both"):
        ImportCostRequest()
    with pytest.raises(ValidationError, match="binary float"):
        ImportCostRequest.model_validate({"listing_id": str(uuid4()), "customs_eur_mkd_rate": 61.5})
    with pytest.raises(ValidationError):
        ImportCostRequest.model_validate({"listing_id": str(uuid4()), "customs_eur_mkd_rate": "0"})
    with pytest.raises(ValidationError):
        ImportCostRequest.model_validate(
            {"listing_id": str(uuid4()), "customs_eur_mkd_rate": "61.12345678901"}
        )
    with pytest.raises(ValidationError):
        ImportCostRequest.model_validate({"listing_id": str(uuid4()), "unknown_field": 1})
    ok = ImportCostRequest.model_validate({"vehicle": {"fuel": "diesel"}, "customs_eur_mkd_rate": "61.5337"})
    assert ok.customs_eur_mkd_rate == Decimal("61.5337")


def test_draft_files_are_drafts_only(tmp_path: Path) -> None:
    shipped, notes = load_draft_rule_sets(CONFIG / DRAFT_RULES_DIR)
    assert [rs.label() for rs in shipped] == ["mk-passenger-car-import@2026.10-draft.1"] and notes == []
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    shutil.copy(DRAFT, drafts / "a_draft.json")
    shutil.copy(CONFIG / "tax_rules" / "example_unapproved.json", drafts / "b_unapproved.json")
    (drafts / "c_broken.json").write_text("{not json", encoding="utf-8")
    # A symlink leaving the drafts directory is never read (the owner's store route refuses it too).
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(DRAFT, outside / "escape.json")
    (drafts / "d_escape.json").symlink_to(outside / "escape.json")
    loaded, notes = load_draft_rule_sets(drafts)
    assert [rs.status for rs in loaded] == [TaxRuleStatus.DRAFT]
    assert notes[0].startswith("DRAFT_RULE_FILE_IGNORED:b_unapproved.json: status unapproved")
    assert notes[1].startswith("DRAFT_RULE_FILE_UNREADABLE:c_broken.json")
    assert notes[2] == "DRAFT_RULE_FILE_IGNORED:d_escape.json: not a regular file in the drafts directory"
    assert load_draft_rule_sets(tmp_path / "missing") == ([], [])


def test_selection_prefers_stored_active_and_never_promotes_a_file() -> None:
    active = synthetic_active()
    on = DECL
    chosen = select_calculator_rule_set([stored(active)], [], "XX", "passenger_car", on)
    assert chosen.mode == "active" and chosen.rule_set == active
    # An ACTIVE-looking set passed as a file is never a production candidate.
    as_file = select_calculator_rule_set([], [active], "XX", "passenger_car", on)
    assert as_file.rule_set is None and as_file.mode == "indicative_preview"
    preview = select_calculator_rule_set([], [draft()], "MK", "passenger_car", on)
    assert preview.mode == "indicative_preview" and preview.rule_set == draft()
    # The same version stored identically merges with the file (stored created_at breaks ties).
    same = select_calculator_rule_set([stored(draft())], [draft()], "MK", "passenger_car", on)
    assert same.rule_set == draft()


def test_conflicting_or_revoked_copies_leave_the_calculator_unavailable() -> None:
    changed = RuleSet.model_validate({**draft().model_dump(), "description": "SYNTHETIC changed copy"})
    conflict = select_calculator_rule_set([stored(changed)], [draft()], "MK", "passenger_car", DECL)
    assert conflict.rule_set is None
    assert "CONFLICTING_RULE_SET_COPIES" in conflict.reason and "remove or re-version" in conflict.reason
    revoked = transition_rule_set(draft(), TaxRuleStatus.REVOKED, at=AS_OF)
    hidden = select_calculator_rule_set([stored(revoked)], [draft()], "MK", "passenger_car", DECL)
    assert hidden.rule_set is None and any("revoked" in c for c in hidden.considered)
    other = RuleSet.model_validate({**draft().model_dump(), "version": "2026.10-draft.2"})
    ambiguous = select_calculator_rule_set([], [draft(), other], "MK", "passenger_car", DECL)
    assert ambiguous.rule_set is None and "AMBIGUOUS_PREVIEW" in ambiguous.reason


def test_service_paths_match_the_worker_runtime() -> None:
    assert COST_PROFILE_FILE == DEFAULT_COST_PROFILE
    assert (CONFIG / COST_PROFILE_FILE).is_file() and (CONFIG / DRAFT_RULES_DIR).is_dir()


def test_result_view_carries_listing_and_fact_sources() -> None:
    result = ImportCostResult(
        estimate=estimate(),
        facts=facts(),
        fact_sources={"price": "request", "co2": "listing"},
        customs_fx_source="caller_supplied",
        listing=None,
        as_of=AS_OF,
    )
    view = result.view()
    assert view.customs_fx_source == "caller_supplied"
    assert [(s.fact, s.source) for s in view.fact_sources] == [("price", "request"), ("co2", "listing")]
    assert view.facts.price.amount == "3000.00" and view.facts.co2[0].g_km == "189"


# --------------------------------------------------------------------------- builder additions


def test_builder_marks_claims_and_passes_the_price_and_engine_facts_through() -> None:
    listed = facts_from_listing(_listing(), payable=Money.of("3000", "EUR"), declaration_date=DECL)
    built = build_tax_inputs(listed, transport=transport_line(), customs_rate=rate(), as_of=AS_OF)
    assert built.unverified_inputs == (
        "price: advertised price, not an invoice or contract price",
        "co2: listing claim, not a ЗДМВ чл. 12(1) document",
    )
    assert built.inputs.invoice_price == Money.of("3000", "EUR")
    assert built.inputs.engine_displacement_cm3 == Decimal(1995) and built.inputs.power_kw == Decimal(130)
    verified = build_tax_inputs(facts(), transport=transport_line(), customs_rate=rate(), as_of=AS_OF)
    assert verified.unverified_inputs == ()
    claimed = facts(origin_proof={"kind": "eu", "country": "DE"}, hs_code="8703 32", hs_evidence_ref="broker")
    built = build_tax_inputs(claimed, transport=transport_line(), customs_rate=rate(), as_of=AS_OF)
    assert [u.split(":")[0] for u in built.unverified_inputs] == ["origin_proof", "hs_code"]
    assert estimate(listed).unverified_inputs == built_claims(listed)


def built_claims(f: VehicleImportFacts) -> tuple[str, ...]:
    return build_tax_inputs(f, transport=transport_line(), customs_rate=rate(), as_of=AS_OF).unverified_inputs


def test_listing_values_outside_fact_bounds_stay_unknown_instead_of_raising() -> None:
    huge = _listing(vehicle=VehicleSpec(fuel=Fuel.DIESEL, mileage_km=Decimal("1000000000")))
    listed = facts_from_listing(huge, payable=Money.of("10000000000000", "EUR"))
    assert listed.mileage_km is None and listed.price is None


def test_single_transport_line_picks_exactly_one_or_reports_unknown() -> None:
    line = transport_line()
    assert single_transport_line([line]) == line
    for lines in ([], [line, line.model_copy(update={"label": "second"})]):
        unknown = single_transport_line(lines)
        assert unknown.status == CostLineStatus.UNKNOWN and unknown.base is None
        assert unknown.reason is not None and "transport amount is unknown" in unknown.reason
    other: CostLine = line.model_copy(update={"category": CostCategory.CUSTOMS_BROKER})
    assert single_transport_line([other]).status == CostLineStatus.UNKNOWN


def test_valuation_records_input_warnings_without_changing_state_or_fingerprint() -> None:
    screening = ScreeningInput(
        eligibility=EligibilityState.ELIGIBLE_PRIMARY,
        profile_key=None,
        eur_payable=Money.of("2750", "EUR"),
        reasons=(),
        is_fixture=False,
    )
    common: dict[str, Any] = {
        "listing_revision_id": str(uuid4()),
        "screening": screening,
        "comparable": None,
        "tax": None,
        "scenarios": None,
        "fx_rates": [],
        "cost_profile": None,
        "config_revision_id": str(uuid4()),
        "as_of": datetime(2026, 10, 13, tzinfo=UTC),
    }
    plain = assemble_valuation(**common)
    noted = assemble_valuation(**common, input_warnings=("IMPORT_GATE:unknown: EURO_CLASS_UNKNOWN",))
    assert noted.warnings == (*plain.warnings, "IMPORT_GATE:unknown: EURO_CLASS_UNKNOWN")
    assert noted.state == plain.state and noted.dependency_fingerprint == plain.dependency_fingerprint
