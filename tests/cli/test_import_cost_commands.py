"""``suv-deals import-costs estimate | values | record-values`` and ``tax-rules store`` without a
database.

Request building and validation (every bad fact is a usage error before any database work), the
CLI's closed vocabularies against the domain's, the human-readable rendering of the shared view in
each mode in whole units (owner decision 2026-10-13), and the recorded real purchase price /
estimated customs value commands (validation, confirmation, rendering). The MK amounts are the
fact sheet's golden case G01 on the shipped DRAFT rule set; the ACTIVE case is a SYNTHETIC
jurisdiction-XX copy approved inside the test only. No network, no database, no real rate
(61.5337 is the golden-case input).
"""

# ruff: noqa: RUF001 - the MK banner under test is Cyrillic by design

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from tests.cli.conftest import Cli

from suv_deals.api.import_values_routes import record_result, values_view
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
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    MAX_INPUT_AMOUNT,
    ReviewRecord,
    RuleSet,
    compute_rule_set_sha256,
    load_rule_set_file,
    transition_rule_set,
)
from suv_deals.persistence import import_values_repo
from suv_deals.persistence.import_values_repo import (
    ImportValueCategory,
    ImportValueRecord,
    ImportValues,
    ImportValuesRecorded,
    RecordedKind,
)
from suv_deals.views.import_costs import FactSource, ImportCostEstimateView
from suv_deals.views.import_values import ImportValuesRecordResult

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "config" / "tax_rules" / "drafts" / "mk_passenger_car_import_2026.10-draft.3.json"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
#: Whole denars without a fraction (owner decision of 2026-10-13: amounts without decimals).
G01 = "168155"  # D1: diesel, NEDC 189 (COC), EUR 3,000 + EUR 700 transport, no proof of origin
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
        (("--price", "2800", "--estimated-customs-value-eur", "4200"), "go together"),
        (("--price", "2800", "--estimated-customs-value-source", "SchwackeNet"), "go together"),
        (
            ("--estimated-customs-value-eur", "0", "--estimated-customs-value-source", "SchwackeNet"),
            "greater than 0",
        ),
        (
            ("--estimated-customs-value-eur", "4200", "--estimated-customs-value-source", "ab"),
            "at least 3",
        ),
        (("--catalogue-value-eur", "6000"), "No such option"),
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
            estimated_customs_value_eur=Decimal("4200"),
            estimated_customs_value_source="SchwackeNet catalogue",
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
        "estimated_customs_value_eur": Decimal("4200"),
        "estimated_customs_value_source": "SchwackeNet catalogue",
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
        "mk-passenger-car-import@2026.10-draft.3 (draft)" in text
        and "an unsealed draft has no recorded hash" in text
    )
    # Whole units (owner decision 2026-10-13): EUR 2,732.73 and 6,432.73 shown as 2733 and 6433.
    assert f"indicative_total : {G01} MKD  (2733 EUR)" in text
    assert "indicative_landed_cost_eur : 6433 EUR" in text
    assert "\n  total " not in text and "known_subtotal" not in text
    assert "customs value : 3700 EUR [transaction_value]" in text
    assert "customs duty : 11384 MKD" in text  # 5% standard duty: no proof of origin
    assert "motor-vehicle tax (ДМВ) : 96390 MKD  (1566 EUR)" in text
    assert "real purchase price : 3000 EUR  (advertised: advertised price (a claim, not an invoice))" in text
    assert "customs value for duties : 3700 EUR  (transaction_value:" in text
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
    assert "customs_value_mkd            resolved        227675 MKD  [helper, not counted]" in text
    assert "dmv_full                     resolved        96390 MKD\n" in text
    assert "[n/a]" in text and "-v lists them" not in text


def test_a_claimed_proof_shows_the_standard_duty_beside_it() -> None:
    text = _text(_view([load_rule_set_file(DRAFT)], origin_proof={"kind": "eu", "country": "DE"}))
    assert "Scenario primary" in text and "Scenario standard_duty_if_proof_rejected" in text
    assert "indicative_total : 154722 MKD" in text  # G02
    assert "origin proof : eu (DE)  [request]" in text


def test_an_estimated_customs_value_is_shown_apart_from_the_real_purchase_price() -> None:
    """Owner decision 2026-10-13: bought for EUR 2,800, the catalogue says EUR 4,200."""
    text = _text(
        _view(
            [load_rule_set_file(DRAFT)],
            price={"amount": "2800", "currency": "EUR"},
            price_basis="invoice",
            estimated_customs_value_eur="4200",
            estimated_customs_value_source="SchwackeNet catalogue",
        )
    )
    assert "Scenario primary: Primary: estimated customs value (SchwackeNet catalogue)" in text
    assert "Scenario transaction_value: Alternative: Customs accepts the invoice" in text
    assert "customs value : 4900 EUR [estimated]" in text
    assert "customs value : 3500 EUR [transaction_value]" in text
    assert "indicative_total : 185803 MKD  (3020 EUR)" in text
    assert "indicative_landed_cost_eur : 6520 EUR" in text
    assert "indicative_total : 165213 MKD  (2685 EUR)" in text
    assert "indicative_landed_cost_eur : 6185 EUR" in text
    assert "real purchase price : 2800 EUR  (invoice: invoice or contract price)" in text
    assert "customs value for duties : 4900 EUR  (estimated: SchwackeNet catalogue)" in text
    assert "estimated customs value : 4200 EUR, without transport (SchwackeNet catalogue)  [request]" in text
    assert "TRANSACTION_VALUE_ALTERNATIVE" in text


def test_amounts_are_whole_and_a_typed_fraction_keeps_its_exact_value() -> None:
    """Owner decision 2026-10-13 (round amounts without decimals): whole units half-up; a fact
    typed with a fraction is shown whole with the exact value the calculation used."""
    assert import_costs.whole("3000.00", "EUR") == "3000 EUR"
    assert import_costs.whole("2800.50", "EUR") == "2801 EUR (exact 2800.50)"
    assert import_costs.whole("2800.49") == "2800 (exact 2800.49)"
    text = _text(
        _view(
            [load_rule_set_file(DRAFT)],
            price={"amount": "2800.50", "currency": "EUR"},
            price_basis="invoice",
            estimated_customs_value_eur="4200.40",
            estimated_customs_value_source="SchwackeNet catalogue",
        )
    )
    assert "real purchase price : 2801 EUR (exact 2800.50) (invoice)  [request]" in text
    assert (
        "estimated customs value : 4200 EUR (exact 4200.40), without transport (SchwackeNet catalogue)"
        "  [request]"
    ) in text
    assert "real purchase price : 2801 EUR  (invoice: invoice or contract price)" in text
    assert "customs value for duties : 4900 EUR  (estimated: SchwackeNet catalogue)" in text
    # Every computed amount is whole; only the customs value's basis explanation quotes the
    # exact inputs it was computed from (e.g. 4200.40 EUR + 700 EUR transport).
    assert "customs value : 4900 EUR [estimated]  (" in text
    computed = [
        line
        for line in text.splitlines()
        if re.match(r"\s+(indicative_total|indicative_landed_cost_eur|customs duty|import VAT|motor)", line)
    ]
    assert len(computed) >= 8 and not [line for line in computed if re.search(r"\d\.\d", line)], computed


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
    assert f"\n  total : {G01} MKD  (2733 EUR)" in text
    assert "\n  landed_cost_eur : 6433 EUR" in text
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
    assert "mk-passenger-car-import@2026.10-draft.3" in result.output
    assert "content sha256   : " + compute_rule_set_sha256(load_rule_set_file(DRAFT)) in result.output
    assert "re-run with --yes" in result.output
    missing_owner = run_cli("tax-rules", "store", str(DRAFT), "--yes")
    assert missing_owner.exit_code == 2 and "--owner-user-id" in missing_owner.output


# --------------------------------------------------------------------------- values / record-values

LISTING = "00000000-0000-4000-8000-0000000000aa"
OWNER = "00000000-0000-4000-8000-000000000001"
RECORD = ("import-costs", "record-values", "--listing-id", LISTING, "--owner-user-id", OWNER)
PAID = ("--purchase-price", "2800.50", "--purchase-kind", "actual", "--purchase-source", "seller invoice")
SCHWACKE = (
    "--estimated-customs-value-eur",
    "4200",
    "--estimated-customs-value-source",
    "SchwackeNet catalogue",
)
SECRET = "Zz" * 70  # a value that must never be echoed in an error


def test_the_record_vocabularies_are_the_repositorys() -> None:
    assert get_args(RecordedKind) == import_costs.PURCHASE_KINDS
    assert import_costs.DEFAULT_HISTORY == import_values_repo.DEFAULT_HISTORY_LIMIT
    assert import_costs.MAX_HISTORY == import_values_repo.MAX_HISTORY_LIMIT


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "record --purchase-price (with --purchase-kind and --purchase-source)"),
        (
            ("--purchase-price", "2800"),
            "a real purchase price needs --purchase-price, --purchase-kind, --purchase-source together"
            " (missing: --purchase-kind, --purchase-source)",
        ),
        (("--purchase-kind", "actual"), "(missing: --purchase-price, --purchase-source)"),
        (
            ("--estimated-customs-value-eur", "4200"),
            "an estimated customs value needs --estimated-customs-value-eur, --estimated-customs-value-source"
            " together (missing: --estimated-customs-value-source)",
        ),
        (
            ("--estimated-customs-value-source", "SchwackeNet catalogue"),
            "(missing: --estimated-customs-value-eur)",
        ),
        ((*PAID[:1], "0", *PAID[2:]), "--purchase-price: Input should be greater than 0"),
        ((*PAID[:1], "2800.505", *PAID[2:]), "--purchase-price: Decimal input should have no more than 2"),
        ((*PAID[:1], "2800,50", *PAID[2:]), "is not a plain decimal"),
        ((*PAID[:3], "paid", *PAID[4:]), "Invalid value for '--purchase-kind'"),
        ((*PAID[:5], "ab"), "--purchase-source: String should have at least 3 characters"),
        ((*PAID[:5], SECRET), "--purchase-source: String should have at most 120 characters"),
        (
            (*SCHWACKE[:3], "Schwacke\u202eNet"),
            "--estimated-customs-value-source: Value error, a source label",
        ),
        (
            (*SCHWACKE[:1], str(MAX_INPUT_AMOUNT * 10), *SCHWACKE[2:]),
            "--estimated-customs-value-eur: Input should be less than or equal to",
        ),
        ((*SCHWACKE, "--document-ref", "INV\n1"), "--document-ref: Value error, a document reference"),
    ],
)
def test_record_values_refuses_bad_input_before_any_database_work(
    run_cli: Cli, args: tuple[str, ...], message: str
) -> None:
    result = run_cli(*RECORD, *args, "--yes")
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert "DATABASE_URL" not in result.output and SECRET not in result.output


def test_record_values_needs_the_listing_and_the_owner(run_cli: Cli) -> None:
    no_listing = run_cli("import-costs", "record-values", "--owner-user-id", OWNER, *PAID, "--yes")
    assert no_listing.exit_code == 2 and "Missing option '--listing-id'" in no_listing.output
    no_owner = run_cli("import-costs", "record-values", "--listing-id", LISTING, *PAID, "--yes")
    assert no_owner.exit_code == 2 and "Missing option '--owner-user-id'" in no_owner.output


def test_record_values_shows_both_values_in_whole_euros_and_needs_yes(run_cli: Cli) -> None:
    shown = run_cli(*RECORD, *PAID, *SCHWACKE, "--document-ref", "INV-SYNTHETIC-1")
    assert shown.exit_code == 3, shown.output
    assert f"Listing                  : {LISTING}" in shown.output
    assert "real purchase price    : 2801 EUR (exact 2800.50) (actual; seller invoice)" in shown.output
    assert "estimated customs value: 4200 EUR, without transport (SchwackeNet catalogue)" in shown.output
    assert "document reference     : INV-SYNTHETIC-1" in shown.output
    assert "valuations are marked stale and recomputed" in shown.output
    assert "re-run with --yes" in shown.output and "DATABASE_URL" not in shown.output
    only_estimate = run_cli(*RECORD, *SCHWACKE)
    assert only_estimate.exit_code == 3 and "real purchase price" not in only_estimate.output
    quiet = run_cli(*RECORD, *PAID, "--json")
    assert quiet.exit_code == 3 and "real purchase price" not in quiet.output  # JSON mode prints no summary
    confirmed = run_cli(*RECORD, *PAID, "--yes")
    assert confirmed.exit_code == 2 and "DATABASE_URL is not configured" in confirmed.output


def test_values_needs_a_listing_and_the_database(run_cli: Cli) -> None:
    missing = run_cli("import-costs", "values")
    assert missing.exit_code == 2 and "Missing option '--listing-id'" in missing.output
    too_long = run_cli("import-costs", "values", "--listing-id", LISTING, "--history", "51")
    assert too_long.exit_code == 2
    result = run_cli("import-costs", "values", "--listing-id", LISTING)
    assert result.exit_code == 2 and "DATABASE_URL is not configured" in result.output


def _record(
    category: ImportValueCategory,
    kind: RecordedKind,
    amount: str,
    source: str,
    *,
    current: bool = True,
    supersedes: UUID | None = None,
) -> ImportValueRecord:
    return ImportValueRecord(
        id=uuid4(),
        listing_id=UUID(LISTING),
        category=category,
        kind=kind,
        amount=Money.of(amount, "EUR"),
        source=source,
        document_ref="INV-SYNTHETIC-1" if category == "purchase" else None,
        supersedes_id=supersedes,
        current=current,
        recorded_by=UUID(OWNER),
        recorded_at=AS_OF,
        is_fixture=False,
    )


def test_values_render_whole_euros_both_values_and_the_history() -> None:
    old = _record("purchase", "quote", "3000", "seller offer", current=False)
    paid = _record("purchase", "actual", "2800.50", "seller invoice", supersedes=old.id)
    schwacke = _record("customs_value", "estimate", "4200", "SchwackeNet catalogue")
    values = ImportValues(
        listing_id=UUID(LISTING), purchase=paid, customs_value=schwacke, history=(paid, schwacke, old)
    )
    text = re.sub(r"(?<=\S) {2,}: ", " : ", "\n".join(import_costs.render_values(values_view(values))))
    assert text.startswith(f"IMPORT VALUES  listing {LISTING}")
    assert (
        "real purchase price : 2801 EUR (exact 2800.50)  (actual; source: seller invoice; document "
        f"INV-SYNTHETIC-1; recorded 2026-10-13 09:00 UTC by {OWNER})  [{paid.id}]"
    ) in text
    assert (
        "estimated customs value : 4200 EUR  (estimate, without transport; source: SchwackeNet catalogue;"
    ) in text
    assert "History (newest first):" in text
    assert re.search(
        rf"real purchase price\s+quote\s+3000 EUR\s+superseded\s+seller offer  \[{old.id}\]", text
    )
    assert re.search(r"estimated customs value\s+estimate\s+4200 EUR\s+current\s+SchwackeNet catalogue", text)
    assert "Note: Two separate values per vehicle" in text
    empty = ImportValues(listing_id=UUID(LISTING), purchase=None, customs_value=None, history=())
    blank = "\n".join(import_costs.render_values(values_view(empty)))
    assert "not recorded (the calculator and valuations use the advertised price)" in blank
    assert "not recorded (the charges use the transaction value: price + transport)" in blank
    assert "History" not in blank


def test_a_recording_names_what_it_superseded_and_the_recomputation() -> None:
    old = _record("purchase", "quote", "3000", "seller offer", current=False)
    paid = _record("purchase", "actual", "2800.50", "seller invoice", supersedes=old.id)
    schwacke = _record("customs_value", "estimate", "4200", "SchwackeNet catalogue")
    job, stale = uuid4(), uuid4()
    recorded = ImportValuesRecorded(
        values=ImportValues(
            listing_id=UUID(LISTING), purchase=paid, customs_value=schwacke, history=(paid, schwacke, old)
        ),
        recorded_ids=(paid.id, schwacke.id),
        superseded_ids=(old.id,),
        stale_valuation_ids=(stale,),
        recompute_job_id=job,
    )
    result: ImportValuesRecordResult = record_result(recorded)
    text = re.sub(r"(?<=\S) {2,}: ", " : ", "\n".join(import_costs.render_recorded(result, UUID(OWNER))))
    assert f"Recorded for listing {LISTING} (owner {OWNER}; audited import_values.record):" in text
    assert f"real purchase price : 2801 EUR (exact 2800.50)  (seller invoice, supersedes {old.id})" in text
    assert "estimated customs value : 4200 EUR  (SchwackeNet catalogue, the first value of its kind)" in text
    assert "valuations marked stale : 1" in text and f"recomputation queued : job {job}" in text
    assert "IMPORT VALUES  listing" in text  # followed by the current values
