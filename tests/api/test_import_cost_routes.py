"""The dashboard import-cost calculator route ``GET /api/import-costs/estimate``
(``api.import_cost_routes``; docs/api_contract.md 10.2).

North Macedonian customs duty, motor-vehicle tax and import VAT for one vehicle through the shared
calculator read (``queries.get_import_cost_estimate``, also behind dot's MCP tool and the CLI).
Covered: the route table entry and its tool equivalence, the flat query parameters converting to
the ``deals_estimate_import_costs`` tool input (same rules, failures naming the flat parameters,
values never echoed), the indicative preview on the shipped DRAFT rule set (labelled, totals named
``indicative_total``, the fact sheet's golden totals G01/G02/G03), unknown inputs that stay unknown
(never zero), the ACTIVE rule set taking precedence, ``unavailable`` without any rule set, the same
``NOT_FOUND`` for missing and foreign listings, authentication and membership, and that a call
persists nothing. Real ASGI app, real PostgreSQL, locally signed Supabase-shaped JWTs; every
workspace, listing and rate is SYNTHETIC (the customs rate 61.5337 is the golden-case test input).
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator
from tests.api.conftest import (
    ApiHarness,
    SigningKeys,
    TokenFactory,
    add_members,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.integration.db.helpers import Seed
from tests.integration.import_costs.support import (
    CONFIG,
    CUSTOMS_RATE,
    activate,
    add_listing,
    counts,
    mk_draft,
    owner,
    record_customs_rate,
    workspace,
)
from tests.integration.read_queries.schema_check import walk_numbers

from suv_deals.api import import_cost_routes
from suv_deals.api.schemas import V11_ROUTE_INDEX, ImportCostEstimateQuery, route_slug
from suv_deals.domain.enums import Scope
from suv_deals.errors import ErrorCode, ValidationFailed
from suv_deals.mcp.schemas import CALC_TOOLS, DealsEstimateImportCostsInput
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.views.import_costs import ImportCostEstimateView
from suv_deals.views.jsonschema import model_schema

PATH = "/api/import-costs/estimate"
KEY = f"GET {PATH}"
G01 = "168154.45"  # D1: diesel, NEDC 189 on the COC, EUR 3,000 + EUR 700 transport, no proof of origin
G02 = "154721.64"  # D2: the same car with an accepted EU proof of origin
G03 = "217629.97"  # D3: petrol, NEDC 245 on the COC, EUR 2,500 + EUR 700
SECRET = "Zz" * 20  # a value that must never be echoed
#: Fact sheet D1 as flat query parameters (stands alone: no listing needed).
D1: dict[str, str] = {
    "price_amount": "3000.00",
    "price_currency": "EUR",
    "price_basis": "invoice",
    "declaration_date": "2026-10-13",
    "fuel": "diesel",
    "co2_nedc_g_km": "189",
    "co2_document": "coc",
}


@cache
def _validator() -> Draft202012Validator:
    schema = model_schema(V11_ROUTE_INDEX[KEY].response_model, mode="serialization")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(response: httpx.Response) -> dict[str, Any]:
    """A successful response validated against the route's published envelope schema."""
    assert response.status_code == 200, (response.status_code, response.text[:500])
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    errors = sorted(_validator().iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    assert body["request_id"] == response.headers["x-request-id"]
    assert walk_numbers(body["data"]) == []  # money and decimals are strings, never JSON numbers
    assert ImportCostEstimateView.model_validate(body["data"]) is not None
    return body


def legal_base(data: dict[str, Any]) -> dict[str, Any]:
    scenario: dict[str, Any] = data["scenarios"][0]
    assert scenario["name"] == "legal_base"
    return scenario


def warning_codes(body: dict[str, Any]) -> dict[str, str]:
    return {w["code"]: w["message"] for w in body["warnings"]}


def config_without_drafts(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    (target / "cost_profiles").mkdir(parents=True)
    shutil.copy(CONFIG / "cost_profiles" / "default_unapproved.yaml", target / "cost_profiles")
    return target


@dataclass
class CalcHarness(ApiHarness):
    db: Database = None  # type: ignore[assignment]
    seed: Seed = None  # type: ignore[assignment]
    listing_id: UUID = None  # type: ignore[assignment]

    async def estimate(self, params: dict[str, str], user: UUID | None = None) -> httpx.Response:
        return await self.get(PATH, user or self.users.viewer, params=params)


async def _harness(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, name: str, **settings: Any
) -> AsyncIterator[CalcHarness]:
    ws, source, key = workspace(seed, name)
    listing = add_listing(seed, ws, source, key)  # SYNTHETIC D1 diesel, listed NEDC 189, EUR 3,000
    users = add_members(seed, ws)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(make_settings(**settings), keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield CalcHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=ws,
            metrics=metrics,
            db=db,
            seed=seed,
            listing_id=listing,
        )


@pytest.fixture
async def calc(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[CalcHarness]:
    async for harness in _harness(db, seed, keys, tokens, "API import costs"):
        yield harness


# =========================================================================== contract (no database)


def test_the_route_is_the_read_only_twin_of_the_mcp_tool() -> None:
    route = V11_ROUTE_INDEX[KEY]
    spec = CALC_TOOLS["deals_estimate_import_costs"]
    assert route.mcp_tool == spec.name and route.scope == spec.scope == Scope.DEALS_READ
    assert route.data_model is spec.output_model is ImportCostEstimateView
    assert route.request_model is ImportCostEstimateQuery and route.request_location == "query"
    assert route.idempotency_header is None and route.success_status == 200
    assert ErrorCode.NOT_FOUND in route.errors and not route.paginated
    assert route_slug(route) == "import-costs.estimate.get"
    assert import_cost_routes.ESTIMATE is route
    served = {
        f"{method} {getattr(r, 'path', '')}"
        for r in import_cost_routes.router.routes
        for method in getattr(r, "methods", ()) or ()
        if method != "HEAD"
    }
    assert served == {KEY}


def test_flat_query_parameters_convert_to_the_tool_input() -> None:
    listing = "12345678-1234-4234-8234-123456789abc"
    query = ImportCostEstimateQuery.model_validate(
        {
            "listing_id": listing,
            **D1,
            "co2_wltp_g_km": "200.5",
            "co2_round_up_confirmed": "true",
            "origin_proof_kind": "efta",
            "origin_proof_country": "CH",
            "origin_proof_evidence_ref": "SYNTHETIC EUR.1 ref",
            "hs_code": "8703.32.19",
            "hs_evidence_ref": "SYNTHETIC broker note",
            "transport_after_entry_eur": "150.00",
            "transport_after_entry_separately_invoiced": "true",
            "catalogue_value_eur": "6000",
            "importer_status": "private",
            "first_registration": "2012-05",
            "emissions_class": "Euro 5",
            "mileage_km": "180000",
            "seller_country": "DE",
            "engine_displacement_cm3": "1995",
            "power_kw": "130",
            "customs_eur_mkd_rate": CUSTOMS_RATE,
            "accept_previous_month_fx": "false",
        }
    )
    nested = DealsEstimateImportCostsInput.model_validate(
        {
            "listing_id": listing,
            "vehicle": {
                "price": {"amount": "3000.00", "currency": "EUR"},
                "price_basis": "invoice",
                "declaration_date": "2026-10-13",
                "fuel": "diesel",
                "co2": [{"cycle": "wltp", "g_km": "200.5"}, {"cycle": "nedc", "g_km": "189"}],
                "co2_document": "coc",
                "co2_round_up_confirmed": True,
                "origin_proof": {"kind": "efta", "country": "CH", "evidence_ref": "SYNTHETIC EUR.1 ref"},
                "hs_code": "8703.32.19",
                "hs_evidence_ref": "SYNTHETIC broker note",
                "transport_after_entry_eur": "150.00",
                "transport_after_entry_separately_invoiced": True,
                "catalogue_value_eur": "6000",
                "importer_status": "private",
                "first_registration": "2012-05",
                "emissions_class": "Euro 5",
                "mileage_km": "180000",
                "seller_country": "DE",
                "engine_displacement_cm3": "1995",
                "power_kw": "130",
            },
            "customs_eur_mkd_rate": CUSTOMS_RATE,
            "accept_previous_month_fx": False,
        }
    )
    converted = query.to_tool_input()
    assert converted == nested
    assert converted.vehicle is not None and converted.vehicle_facts() == nested.vehicle_facts()
    # Only the GIVEN facts are set (they replace the listing's); everything else keeps the listing's.
    listing_only = ImportCostEstimateQuery.model_validate({"listing_id": listing}).to_tool_input()
    assert listing_only.vehicle is None and listing_only.vehicle_facts() is None
    partial = ImportCostEstimateQuery.model_validate({"listing_id": listing, "co2_document": "coc"})
    facts = partial.to_tool_input().vehicle_facts()
    assert facts is not None and facts.model_fields_set == {"co2_document"}
    cleared = ImportCostEstimateQuery.model_validate({"listing_id": listing, "co2_clear": "true"})
    cleared_facts = cleared.to_tool_input().vehicle_facts()
    assert cleared_facts is not None and cleared_facts.co2 == () and "co2" in cleared_facts.model_fields_set


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({}, ["listing_id"]),
        ({"customs_eur_mkd_rate": CUSTOMS_RATE}, ["listing_id"]),
        ({"price_amount": "3000"}, ["price_currency"]),
        ({"price_currency": "EUR"}, ["price_amount"]),
        ({"origin_proof_country": "DE"}, ["origin_proof_kind"]),
        (
            {"origin_proof_kind": "efta"},
            ["origin_proof_country", "origin_proof_evidence_ref", "origin_proof_kind"],
        ),
        ({"co2_nedc_g_km": "189", "co2_clear": "true"}, ["co2_clear", "co2_nedc_g_km"]),
        (
            {"transport_after_entry_eur": "150"},
            ["transport_after_entry_eur", "transport_after_entry_separately_invoiced"],
        ),
        ({"hs_evidence_ref": "SYNTHETIC"}, ["hs_evidence_ref"]),
        ({"hs_code": "870421", "fuel": "diesel"}, ["hs_code"]),
        (
            {
                "co2_wltp_g_km": "1",
                "co2_nedc_g_km": "2",
                "co2_nedc_correlated_g_km": "3",
                "co2_unknown_cycle_g_km": "4",
            },
            ["co2_nedc_correlated_g_km", "co2_nedc_g_km", "co2_unknown_cycle_g_km", "co2_wltp_g_km"],
        ),
    ],
)
def test_conversion_failures_name_the_flat_parameters(params: dict[str, str], fields: list[str]) -> None:
    with pytest.raises(ValidationFailed) as info:
        ImportCostEstimateQuery.model_validate(params).to_tool_input()
    assert info.value.details == {"fields": fields}
    assert "SYNTHETIC" not in str(info.value.details)


# =========================================================================== the route (database)


@pytest.mark.db
async def test_preview_from_vehicle_facts_alone(calc: CalcHarness) -> None:
    before = counts(calc.seed, calc.workspace_id)
    body = valid(await calc.estimate({**D1, "customs_eur_mkd_rate": CUSTOMS_RATE}))
    assert counts(calc.seed, calc.workspace_id) == before  # a read: no valuation, rate, audit row or job
    data = body["data"]
    assert data["mode"] == "indicative_preview" and data["production_ready"] is False
    assert data["banner"]["mk"].startswith("ИНДИКАТИВНА ПРОЦЕНКА")
    assert data["banner"]["en"].startswith("INDICATIVE PREVIEW")
    assert data["rule"]["rule_set_id"] == "mk-passenger-car-import" and data["rule"]["status"] == "draft"
    assert data["rule"]["sha256_recorded"] is False and data["listing"] is None
    legal = legal_base(data)
    assert legal["indicative_total"] == {"status": "known", "amount": G01, "currency": "MKD", "reason": None}
    assert legal["total"] is None and legal["total_eur"] is None and legal["landed_cost_eur"] is None
    assert legal["indicative_landed_cost_eur"]["status"] == "known" and legal["production_ready"] is False
    assert data["customs_fx_source"] == "caller_supplied" and data["customs_fx_rate"]["rate"] == CUSTOMS_RATE
    assert any(a.startswith("CALLER_SUPPLIED_CUSTOMS_RATE") for a in data["assumptions"])
    assert any(a.startswith("TRANSPORT_IN_CUSTOMS_VALUE:700.00 EUR") for a in data["assumptions"])
    assert any(a.startswith("NO_ORIGIN_PROOF_STANDARD_DUTY") for a in data["assumptions"])
    helpers = {c["component_id"] for c in legal["components"] if c["intermediate"]}
    assert {"customs_value_mkd", "vat_base_mkd"} <= helpers
    warnings = warning_codes(body)
    assert warnings["TAX_RULES_UNAPPROVED"].startswith("INDICATIVE PREVIEW")
    assert "UNKNOWN_COSTS" not in warnings
    assert {s["fact"]: s["source"] for s in data["fact_sources"]}["price"] == "request"


@pytest.mark.db
async def test_listing_facts_with_query_overrides(calc: CalcHarness) -> None:
    await record_customs_rate(calc.db, calc.workspace_id)
    listing = str(calc.listing_id)
    base = {"listing_id": listing, "declaration_date": "2026-10-13", "co2_document": "coc"}
    g01 = valid(await calc.estimate(base))["data"]
    assert legal_base(g01)["indicative_total"]["amount"] == G01
    assert g01["listing"]["listing_id"] == listing and g01["customs_fx_source"] == "recorded"
    sources = {s["fact"]: s["source"] for s in g01["fact_sources"]}
    assert (
        sources["price"] == "listing" and sources["co2"] == "listing" and sources["co2_document"] == "request"
    )
    # An accepted EU proof of origin: 0% duty, with the standard-duty alternative shown next to it.
    eu = valid(
        await calc.estimate({**base, "origin_proof_kind": "eu", "origin_proof_evidence_ref": "SYNTHETIC"})
    )
    names = [s["name"] for s in eu["data"]["scenarios"]]
    assert names[:2] == ["legal_base", "standard_duty_if_proof_rejected"]
    assert legal_base(eu["data"])["indicative_total"]["amount"] == G02
    assert eu["data"]["scenarios"][1]["indicative_total"]["amount"] == G01
    assert "SELLER_CLAIMS_UNVERIFIED" in warning_codes(eu)
    # Every given parameter replaces the listing's fact (D3: petrol, NEDC 245, EUR 2,500 invoice).
    petrol = {
        **base,
        "fuel": "petrol",
        "co2_nedc_g_km": "245",
        "price_amount": "2500.00",
        "price_currency": "EUR",
        "price_basis": "invoice",
    }
    g03 = valid(await calc.estimate(petrol))["data"]
    assert legal_base(g03)["indicative_total"]["amount"] == G03
    assert g03["facts"]["fuel"] == "petrol" and g03["facts"]["price_basis"] == "invoice"


@pytest.mark.db
async def test_unknown_inputs_stay_unknown_never_zero(calc: CalcHarness) -> None:
    # No CO2 figure (the listing's claim dropped) and no customs rate recorded or given.
    body = valid(
        await calc.estimate(
            {"listing_id": str(calc.listing_id), "declaration_date": "2026-10-13", "co2_clear": "true"}
        )
    )
    data = body["data"]
    legal = legal_base(data)
    assert legal["complete"] is False
    assert legal["indicative_total"]["status"] == "unknown" and legal["indicative_total"]["amount"] is None
    assert legal["indicative_landed_cost_eur"]["status"] == "unknown"
    assert legal["known_subtotal"] is not None and legal["known_subtotal_label"] == "known_subtotal"
    assert legal["customs_value"]["status"] == "known"  # the EUR customs value; its MKD conversion is unknown
    assert {"co2", "customs_fx_rate"} <= set(data["missing_facts"])
    assert data["customs_fx_source"] == "none" and data["customs_fx_rate"] is None
    assert any("suv-deals fx record" in w for w in data["warnings"])
    unknown = [c for c in legal["components"] if c["status"] == "unknown"]
    assert unknown and all(c["amount"]["amount"] is None for c in unknown)
    assert "Some import charges are unknown (never zero)" in warning_codes(body)["UNKNOWN_COSTS"]


@pytest.mark.db
@pytest.mark.parametrize(
    "dates",
    [
        {"first_registration": "0000"},
        {"first_registration": "0000-05"},
        {"first_registration": "1800-01-01"},
        {"declaration_date": "9999-12-31", "first_registration": "2020"},
        {"declaration_date": "2026-12-31", "first_registration": "1800"},
    ],
)
async def test_an_implausible_first_registration_is_never_a_server_error(
    calc: CalcHarness, dates: dict[str, str]
) -> None:
    """Regression: year 0000 raised ValueError and an age over 200 a ValidationError in the shared
    builder, so the route answered 500 INTERNAL_ERROR (retryable) instead of a labelled unknown."""
    body = valid(await calc.estimate({**D1, "customs_eur_mkd_rate": CUSTOMS_RATE, **dates}))
    data = body["data"]
    assert data["facts"]["first_registration"] == dates["first_registration"]
    assert any(w.startswith("FIRST_REGISTRATION_IMPLAUSIBLE:") for w in data["warnings"]), data["warnings"]
    listing = valid(await calc.estimate({"listing_id": str(calc.listing_id), **dates}))
    assert any(w.startswith("FIRST_REGISTRATION_IMPLAUSIBLE:") for w in listing["data"]["warnings"])


@pytest.mark.db
async def test_an_active_rule_set_takes_precedence(calc: CalcHarness) -> None:
    await record_customs_rate(calc.db, calc.workspace_id)
    await activate(calc.db, owner(calc.workspace_id), mk_draft())  # throwaway SYNTHETIC workspace only
    body = valid(await calc.estimate(D1))
    data = body["data"]
    assert data["mode"] == "active" and data["banner"] is None and data["rule"]["status"] == "active"
    legal = legal_base(data)
    assert legal["total"]["amount"] == G01 and legal["indicative_total"] is None
    assert legal["total_eur"]["status"] == "known" and legal["landed_cost_eur"]["status"] == "known"
    assert data["production_ready"] is True and "TAX_RULES_UNAPPROVED" not in warning_codes(body)


@pytest.mark.db
async def test_unavailable_without_any_rule_set(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, tmp_path: Path
) -> None:
    config = config_without_drafts(tmp_path)
    async for harness in _harness(db, seed, keys, tokens, "API import costs none", config_dir=config):
        body = valid(await harness.estimate({**D1, "customs_eur_mkd_rate": CUSTOMS_RATE}))
        data = body["data"]
        assert data["mode"] == "unavailable" and data["scenarios"] == [] and data["rule"] is None
        assert data["production_ready"] is False and data["banner"] is not None
        assert data["missing_facts"][0] == "tax_rule_set"
        assert {"TAX_RULES_UNAPPROVED", "UNKNOWN_COSTS"} <= set(warning_codes(body))


@pytest.mark.db
async def test_missing_and_foreign_listings_are_the_same_not_found(calc: CalcHarness) -> None:
    other_ws, other_source, other_key = workspace(calc.seed, "API import costs foreign")
    foreign = add_listing(calc.seed, other_ws, other_source, other_key)
    responses = [
        await calc.estimate({"listing_id": str(listing_id)}) for listing_id in (foreign, uuid.uuid4())
    ]
    assert [r.status_code for r in responses] == [404, 404]
    first, second = (error_of(r) for r in responses)
    assert first["code"] == second["code"] == "NOT_FOUND" and first["message"] == second["message"]
    assert str(foreign) not in responses[0].text


@pytest.mark.db
async def test_authentication_and_membership_are_required(calc: CalcHarness) -> None:
    anonymous = await calc.client.get(PATH, params=D1)
    assert anonymous.status_code == 401 and error_of(anonymous)["code"] == "UNAUTHENTICATED"
    stranger = await calc.estimate(D1, user=calc.users.stranger)
    assert stranger.status_code == 403 and error_of(stranger)["code"] == "FORBIDDEN"
    for user in (calc.users.viewer, calc.users.reviewer, calc.users.owner):  # every role holds deals:read
        assert (
            await calc.estimate({**D1, "customs_eur_mkd_rate": CUSTOMS_RATE}, user=user)
        ).status_code == 200


@pytest.mark.db
async def test_invalid_queries_name_fields_and_never_echo_values(calc: CalcHarness) -> None:
    cases: list[tuple[dict[str, str], list[str]]] = [
        ({**D1, "price_amount": "1e3"}, ["price_amount"]),
        ({**D1, "price_amount": "-5"}, ["price_amount"]),
        ({**D1, "co2_nedc_g_km": SECRET}, ["co2_nedc_g_km"]),
        ({**D1, "fuel": "steam"}, ["fuel"]),
        ({**D1, "declaration_date": "1759744800"}, ["declaration_date"]),
        ({**D1, "first_registration": "2015-02-30"}, ["first_registration"]),
        ({**D1, "origin_proof_country": "DE"}, ["origin_proof_kind"]),
        ({**D1, "hs_code": "8704 21"}, ["hs_code"]),
        ({**D1, "customs_eur_mkd_rate": "0"}, ["customs_eur_mkd_rate"]),
        ({**D1, "listing_id": "not-a-uuid"}, ["listing_id"]),
        ({**D1, "access_token": SECRET}, ["access_token"]),
        ({**D1, "vehicle": SECRET}, ["vehicle"]),
        ({}, ["listing_id"]),
    ]
    for params, fields in cases:
        response = await calc.estimate(params)
        assert response.status_code == 422, params
        error = error_of(response)
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["fields"] == fields, (params, error["details"])
        assert SECRET not in response.text
    repeated = await calc.client.get(
        f"{PATH}?fuel=diesel&fuel=petrol&price_amount=1&price_currency=EUR",
        headers=calc.auth(calc.users.viewer),
    )
    assert repeated.status_code == 422 and error_of(repeated)["details"]["fields"] == ["fuel"]
