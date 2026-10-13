"""``suv-deals import-costs estimate`` and ``tax-rules store`` without a database.

Request building and validation (every bad fact is a usage error before any database work), the
CLI's closed vocabularies against the domain's, and the human-readable rendering of the shared
view in each mode. The MK amounts are the fact sheet's golden case G01 on the shipped DRAFT rule
set; the ACTIVE case is a SYNTHETIC jurisdiction-XX copy approved inside the test only. No
network, no database, no real rate (61.5337 is the golden-case input).
"""

# ruff: noqa: RUF001 - the MK banner under test is Cyrillic by design

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
from tests.cli.conftest import Cli

from suv_deals.cli_commands import import_costs
from suv_deals.domain.costs import CostScope, load_cost_profile
from suv_deals.domain.enums import FxPurpose, TaxRuleStatus
from suv_deals.domain.import_estimate import (
    Co2Document,
    ImporterStatus,
    OriginProofKind,
    PriceBasis,
    PriceFact,
    VehicleImportFacts,
    estimate_from_rule_sets,
    single_transport_line,
)
from suv_deals.domain.money import FxRate
from suv_deals.domain.tax_engine import (
    ReviewRecord,
    RuleSet,
    compute_rule_set_sha256,
    load_rule_set_file,
    transition_rule_set,
)
from suv_deals.views.import_costs import FactSource, ImportCostEstimateView

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "config" / "tax_rules" / "drafts" / "mk_passenger_car_import_2026.10-draft.1.json"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
G01 = "168154.45"  # D1: diesel, NEDC 189 (COC), EUR 3,000 + EUR 700 transport, no proof of origin
G01_FACTS = (
    "--price", "3000", "--fuel", "diesel", "--co2", "nedc:189", "--co2-document", "coc",
    "--declaration-date", "2026-10-13",
)  # fmt: skip


# --------------------------------------------------------------------------- request


def test_the_cli_vocabularies_are_the_domains() -> None:
    assert get_args(Co2Document) == import_costs.CO2_DOCUMENTS
    assert get_args(OriginProofKind) == import_costs.ORIGIN_PROOF_KINDS
    assert get_args(ImporterStatus) == import_costs.IMPORTER_STATUSES
    assert get_args(PriceBasis) == import_costs.PRICE_BASES
    assert get_args(PriceFact.model_fields["currency"].annotation) == import_costs.PRICE_CURRENCIES


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "give a listing_id, vehicle facts or both"),
        (("--price", "3000,50"), "is not a plain decimal"),
        (("--price", "-3000"), "is not a plain decimal"),
        (("--co2", "189"), "is not CYCLE:G_KM"),
        (("--co2", "wltp:abc"), "is not CYCLE:G_KM"),
        (("--co2", "nedc:189", "--co2", "nedc:190"), "one CO2 reading per measurement cycle"),
        (("--price", "3000", "--transport-after-entry-eur", "100"), "separately invoiced"),
        (("--price", "3000", "--separately-invoiced"), "separately invoiced"),
        (("--hs-code", "8704 21"), "only HS 8703"),
        (("--origin-proof", "efta"), "must state its origin country"),
        (("--origin-proof", "none", "--origin-country", "DE"), "has no country or evidence"),
        (("--origin-country", "DE"), "need --origin-proof"),
        (("--price-currency", "MKD"), "--price-currency needs --price"),
        (("--first-registration", "2014/05"), "YYYY, YYYY-MM or YYYY-MM-DD"),
        (("--first-registration", "2014-13"), "invalid month"),
        (("--seller-country", "Germany"), "two-letter country code"),
        (("--customs-eur-mkd-rate", "0", "--price", "1"), "customs_eur_mkd_rate"),
        (("--declaration-date", "13.10.2026"), "--declaration-date"),
        (("--fuel", "kerosene"), "--fuel"),
    ],
)
def test_bad_facts_are_usage_errors_before_any_database_work(
    run_cli: Cli, args: tuple[str, ...], message: str
) -> None:
    result = run_cli("import-costs", "estimate", *args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert "DATABASE_URL" not in result.output


def test_valid_facts_then_need_the_database(run_cli: Cli) -> None:
    result = run_cli("import-costs", "estimate", *G01_FACTS)
    assert result.exit_code == 2
    assert "DATABASE_URL is not configured" in result.output


def _options(**given: Any) -> dict[str, Any]:
    names = [
        p.name for p in import_costs.estimate.params if p.name not in ("workspace", "as_json", "verbose")
    ]
    options: dict[str, Any] = dict.fromkeys(names)
    options.update(co2=(), co2_round_up_confirmed=False, separately_invoiced=False)
    options.update(accept_previous_month_fx=False)
    options.update(given)
    return options


def test_only_the_given_facts_are_sent_so_the_listing_keeps_the_rest() -> None:
    assert import_costs.vehicle_facts(_options()) == {}
    vehicle = import_costs.vehicle_facts(
        _options(
            price=Decimal("2500"),
            co2=(("wltp", Decimal("160")),),
            first_registration="2014-05",
            origin_proof="eu",
            origin_country="DE",
            transport_after_entry_eur=Decimal("120"),
            separately_invoiced=True,
            declaration_date=datetime(2026, 10, 13),
        )
    )
    assert vehicle == {
        "price": {"amount": Decimal("2500"), "currency": "EUR"},
        "co2": [{"cycle": "wltp", "g_km": Decimal("160")}],
        "first_registration": {"value": "2014-05", "precision": "month"},
        "origin_proof": {"kind": "eu", "country": "DE", "evidence_ref": None},
        "transport_after_entry_eur": Decimal("120"),
        "transport_after_entry_separately_invoiced": True,
        "declaration_date": date(2026, 10, 13),
    }
    request = import_costs.build_request(_options(listing_id=None, **{"co2_document": "coc"}, fuel="diesel"))
    assert request.vehicle is not None and request.vehicle.model_fields_set == {"co2_document", "fuel"}
    assert request.customs_eur_mkd_rate is None and not request.accept_previous_month_fx


# --------------------------------------------------------------------------- rendering


def _rate() -> FxRate:
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.5337"),
        rate_date=date(2026, 10, 1),
        retrieved_at=datetime(2026, 10, 1, 8, tzinfo=UTC),
        provider="SYNTHETIC customs monthly list",
        purpose=FxPurpose.CUSTOMS,
    )


def _view(rule_sets: list[RuleSet], jurisdiction: str = "MK", **facts: Any) -> ImportCostEstimateView:
    profile = load_cost_profile(REPO / "config" / "cost_profiles" / "default_unapproved.yaml")
    transport = single_transport_line(profile.lines(target_scope=CostScope(origin_country="DE")))
    data = {
        "price": {"amount": "3000", "currency": "EUR"},
        "fuel": "diesel",
        "co2": [{"cycle": "nedc", "g_km": "189"}],
        "co2_document": "coc",
        "declaration_date": "2026-10-13",
        "emissions_class": "Euro 5",
        **facts,
    }
    vehicle = VehicleImportFacts.model_validate(data)
    estimate = estimate_from_rule_sets(
        rule_sets, vehicle, transport=transport, customs_rate=_rate(), as_of=AS_OF, jurisdiction=jurisdiction
    )
    sources: dict[str, FactSource] = {
        name: "request" if name in data else "default" for name in VehicleImportFacts.model_fields
    }
    return ImportCostEstimateView.of(
        estimate, facts=vehicle, fact_sources=sources, customs_fx_source="recorded"
    )


def _text(view: ImportCostEstimateView, *, verbose: bool = False) -> str:
    """The rendering with each label's padding collapsed (``label : value``)."""
    return re.sub(r"(?<=\S) {2,}: ", " : ", "\n".join(import_costs.render_estimate(view, verbose=verbose)))


def test_a_preview_is_labelled_in_both_languages_and_names_its_totals_indicative() -> None:
    text = _text(_view([load_rule_set_file(DRAFT)]))
    assert "mode: indicative_preview  production-ready: no" in text
    assert "!! ИНДИКАТИВНА ПРОЦЕНКА" in text and "!! INDICATIVE PREVIEW" in text
    assert "Display only" in text
    assert (
        "mk-passenger-car-import@2026.10-draft.1 (draft)" in text
        and "an unsealed draft has no recorded hash" in text
    )
    assert f"indicative_total : {G01} MKD  (2732.72 EUR)" in text
    assert "indicative_landed_cost_eur : 6432.72 EUR" in text
    assert "\n  total " not in text and "known_subtotal" not in text
    assert "customs value : 3700.00 EUR" in text
    assert "customs duty : 11383.73 MKD" in text  # 5% standard duty: no proof of origin
    assert "motor-vehicle tax (ДМВ) : 96390.00 MKD  (1566.46 EUR)" in text
    assert "import VAT : " in text and "other import charges : not applicable" in text
    assert "1 EUR = 61.5337 MKD (2026-10-01, SYNTHETIC customs monthly list); source recorded" in text
    assert "origin proof : none  [request]" not in text  # origin was not given here
    assert "origin proof : none  [default]" in text
    assert "import gate : importable yes" in text
    assert "Uncertain items (UNCERTAIN - confirm before approval):" in text
    assert "-v lists them" in text and "[n/a]" not in text
    assert "NO_ORIGIN_PROOF_STANDARD_DUTY" in text


def test_verbose_lists_every_component_with_helpers_flagged() -> None:
    text = _text(_view([load_rule_set_file(DRAFT)]), verbose=True)
    assert "customs_value_mkd            resolved        227674.69 MKD  [helper, not counted]" in text
    assert "dmv_full                     resolved        96390.00 MKD\n" in text
    assert "[n/a]" in text and "-v lists them" not in text


def test_a_claimed_proof_shows_the_standard_duty_beside_it() -> None:
    text = _text(_view([load_rule_set_file(DRAFT)], origin_proof={"kind": "eu", "country": "DE"}))
    assert "Scenario legal_base" in text and "Scenario standard_duty_if_proof_rejected" in text
    assert "indicative_total : 154721.64 MKD" in text  # G02
    assert "origin proof : eu (DE)  [request]" in text


def test_an_incomplete_estimate_shows_unknown_and_a_labelled_subtotal_never_zero() -> None:
    view = _view([load_rule_set_file(DRAFT)], co2_document=None)
    text = _text(view)
    assert "indicative_total : unknown (the calculation is incomplete" in text
    assert "(NOT a total)" in text and "complete : no" in text
    assert "Missing facts: " in text and "co2_document" in text


def test_an_active_estimate_names_final_totals_and_shows_no_banner() -> None:
    doc = json.loads(DRAFT.read_text(encoding="utf-8"), parse_float=Decimal)
    doc.update(
        rule_set_id="SYNTHETIC-TEST-ONLY-xx-cli-copy",
        jurisdiction="XX",
        version="synthetic-1",
        description="SYNTHETIC TEST COPY of the draft structure under code XX; never a real approval.",
    )
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    rule_set = transition_rule_set(RuleSet.model_validate(doc), TaxRuleStatus.UNDER_REVIEW, at=at)
    review = ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=at,
        content_sha256=compute_rule_set_sha256(rule_set),
        scope="SYNTHETIC test review of a copy; not a review of any real rule",
    )
    rule_set = transition_rule_set(
        rule_set, TaxRuleStatus.APPROVED, at=at, approved_by="SYNTHETIC owner", review_record=review
    )
    active = transition_rule_set(rule_set, TaxRuleStatus.ACTIVE, at=at)
    text = _text(_view([active], jurisdiction="XX", price_basis="invoice"))
    assert "mode: active  production-ready: yes" in text and "!!" not in text
    assert f"\n  total : {G01} MKD  (2732.72 EUR)" in text
    assert "\n  landed_cost_eur : 6432.72 EUR" in text
    assert "indicative_" not in text and "(recorded)" in text


def test_without_rules_the_result_is_unavailable_and_says_so() -> None:
    text = _text(_view([]))
    assert "mode: unavailable" in text
    assert "!! НЕМА ПРИМЕНЛИВИ ПРАВИЛА" in text and "!! NO APPLICABLE RULES" in text
    assert "Scenario" not in text and "Missing facts: tax_rule_set" in text


# --------------------------------------------------------------------------- tax-rules store (offline)


def test_store_refuses_anything_but_a_valid_non_fixture_draft(run_cli: Cli, tmp_path: Path) -> None:
    owner = ("--owner-user-id", "00000000-0000-4000-8000-000000000001", "--yes")
    example = REPO / "config" / "tax_rules" / "example_unapproved.json"
    unapproved = run_cli("tax-rules", "store", str(example), *owner)
    assert unapproved.exit_code == 3 and "only drafts are stored here" in unapproved.output
    doc = json.loads(DRAFT.read_text(encoding="utf-8"))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({**doc, "is_fixture": True}), encoding="utf-8")
    refused = run_cli("tax-rules", "store", str(fixture), *owner)
    assert refused.exit_code == 3 and "is a SYNTHETIC fixture" in refused.output
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({**doc, "currency": "XYZ1"}), encoding="utf-8")
    invalid = run_cli("tax-rules", "store", str(broken), *owner)
    assert invalid.exit_code == 1 and "INVALID  broken.json" in invalid.output
    assert "nothing was stored" in invalid.output


def test_store_shows_the_target_and_needs_yes(run_cli: Cli) -> None:
    result = run_cli(
        "tax-rules", "store", str(DRAFT), "--owner-user-id", "00000000-0000-4000-8000-000000000001"
    )
    assert result.exit_code == 3
    assert "mk-passenger-car-import@2026.10-draft.1" in result.output
    assert "content sha256   : " + compute_rule_set_sha256(load_rule_set_file(DRAFT)) in result.output
    assert "re-run with --yes" in result.output
    missing_owner = run_cli("tax-rules", "store", str(DRAFT), "--yes")
    assert missing_owner.exit_code == 2 and "--owner-user-id" in missing_owner.output
