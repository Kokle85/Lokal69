"""MCP contract of the import-cost calculator (``mcp.schemas.CALC_TOOLS``): ``deals_estimate_import_costs``.

A separate table, so the spec 21 catalogue stays exactly twelve tools and the spec 37.8 table three.
Read-only under ``deals:read`` in the expensive rate-limit bucket; the closed input mirrors the
calculator service request (``persistence.queries.ImportCostRequest`` and the domain
``VehicleImportFacts``) with decimals as bounded strings; the output is the resolved
``ResponseEnvelope[ImportCostEstimateView]`` without JSON numbers. The description and the server
instructions tell dot what it computes, that an ``indicative_preview`` comes from rules that are not
ACTIVE (draft, under review or approved but not yet activated, as ``rule.status`` and the banner
say), that unknown inputs change the result and that no tool approves tax rules. No rate,
coefficient or threshold appears in the MCP layer. SYNTHETIC values only.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest

from suv_deals.api.middleware import RateLimit
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.costs import CostScope, load_cost_profile
from suv_deals.domain.enums import Co2Cycle, Fuel, Precision, Role, Scope
from suv_deals.domain.import_estimate import (
    Co2Document,
    ImporterStatus,
    OriginProofKind,
    PriceBasis,
    VehicleImportFacts,
    single_transport_line,
)
from suv_deals.domain.listings import PartialDate
from suv_deals.errors import RateLimited, ValidationFailed
from suv_deals.mcp.schemas import (
    ALL_TOOL_NAMES,
    CALC_TOOL_NAMES,
    CALC_TOOLS,
    FORBIDDEN_TOOL_NAMES,
    TOOLS,
    V11_TOOLS,
    DealsEstimateImportCostsInput,
    exported_schema_documents,
    tool_document,
    tool_input_schema,
    tool_output_schema,
    tool_spec,
    tools_for_scopes,
    validate_tool_input,
)
from suv_deals.mcp.server import INSTRUCTIONS
from suv_deals.mcp.tools import (
    CHEAP_TOOLS,
    ToolRateLimiter,
    ToolRegistry,
    handler_for,
    import_cost_request,
)
from suv_deals.persistence.queries import ImportCostRequest
from suv_deals.views.import_costs import ImportCostEstimateView
from suv_deals.views.jsonschema import find_refs, open_objects

REPO = Path(__file__).resolve().parents[2]
NAME = "deals_estimate_import_costs"
UID = "12345678-1234-4234-8234-123456789abc"
SECRET = "Zz" * 20  # a value that must never be echoed in an error
#: The words no tool name may contain (tests/contracts/test_mcp_tool_schemas.py).
BANNED_WORDS = (
    "buy",
    "purchase",
    "bid",
    "pay",
    "message",
    "contact",
    "sql",
    "crawl",
    "fetch",
    "url",
    "approve",
    "tax",
    "shell",
    "exec",
    "delete",
    "enable",
    "resume",
    "admin",
    "credential",
    "secret",
)
#: SYNTHETIC D1 facts (fact sheet golden case G01): diesel, NEDC 189 g/km on the COC, EUR 3,000.
VEHICLE: dict[str, Any] = {
    "price": {"amount": "3000.00", "currency": "EUR"},
    "price_basis": "invoice",
    "declaration_date": "2026-10-13",
    "fuel": "diesel",
    "co2": [{"cycle": "nedc", "g_km": "189"}],
    "co2_document": "coc",
    "first_registration": "2012-05",
    "emissions_class": "Euro 5",
    "origin_proof": {"kind": "eu", "country": "DE", "evidence_ref": "SYNTHETIC EUR.1 0001"},
    "hs_code": "8703.32",
    "hs_evidence_ref": "SYNTHETIC broker note",
    "transport_after_entry_eur": "150.00",
    "transport_after_entry_separately_invoiced": True,
    "catalogue_value_eur": "6000",
    "importer_status": "private",
    "mileage_km": "182000",
    "seller_country": "DE",
    "engine_displacement_cm3": "1968",
    "power_kw": "110.5",
}


def actor() -> ActorContext:
    return ActorContext(
        workspace_id=uuid4(),
        principal_id=uuid4(),
        principal_kind="mcp_client",
        role=Role.VIEWER,
        scopes=ROLE_SCOPES[Role.VIEWER],
        request_id="req-calc-1",
    )


def fields_of(arguments: dict[str, Any]) -> list[str]:
    """The failing field names on both validation paths (``validate_tool_input`` and the MCP
    dispatcher's ``RegisteredTool.validate``), which must agree; values are never echoed."""
    with pytest.raises(ValidationFailed) as schema_path:
        validate_tool_input(NAME, arguments)
    tool = ToolRegistry.default().get(NAME)
    assert tool is not None
    with pytest.raises(ValidationFailed) as served_path:
        tool.validate(arguments)
    assert schema_path.value.details == served_path.value.details, arguments
    assert SECRET not in json.dumps(served_path.value.details) + served_path.value.message
    fields: list[str] = served_path.value.details["fields"]
    return fields


# =========================================================================== registry


def test_one_read_only_calculator_tool_in_its_own_table() -> None:
    assert CALC_TOOL_NAMES == (NAME,)
    spec = CALC_TOOLS[NAME]
    assert spec.scope == Scope.DEALS_READ
    assert spec.input_model is DealsEstimateImportCostsInput
    assert spec.output_model is ImportCostEstimateView
    assert not set(CALC_TOOLS) & (set(TOOLS) | set(V11_TOOLS) | FORBIDDEN_TOOL_NAMES)
    assert not any(word in NAME for word in BANNED_WORDS)
    hints = spec.annotations
    assert hints.read_only and hints.idempotent and not hints.destructive and not hints.open_world
    assert spec.idempotency_operation is None and not spec.paginated
    assert 40 <= len(spec.description) <= 600
    # Expensive bucket: every call reads files, rule sets, rates and a listing.
    assert NAME not in CHEAP_TOOLS


def test_served_after_the_inquiry_tools_and_discoverable_with_deals_read() -> None:
    registry = ToolRegistry.default()
    assert registry.names == ALL_TOOL_NAMES and registry.names[-1] == NAME
    tool = registry.get(NAME)
    assert tool is not None
    assert tool.input_schema == tool_input_schema(NAME)
    assert tool.definition.output_schema == tool_output_schema(NAME)
    assert tool_spec(NAME) is CALC_TOOLS[NAME]
    assert handler_for(NAME) is tool.handler
    for role in (Role.VIEWER, Role.REVIEWER, Role.OWNER):
        assert NAME in [s.name for s in tools_for_scopes(ROLE_SCOPES[role])], role
        assert NAME in {t.name for t in registry.visible(ROLE_SCOPES[role])}, role
    assert NAME not in {t.name for t in registry.visible([Scope.REVIEWS_READ, Scope.INQUIRIES_READ])}


def test_the_expensive_bucket_limits_the_calculator() -> None:
    limiter = ToolRateLimiter(
        expensive=RateLimit(capacity=1, per_seconds=60.0), cheap=RateLimit(capacity=50, per_seconds=1.0)
    )
    caller = actor()
    limiter.check(caller, NAME)
    with pytest.raises(RateLimited):
        limiter.check(caller, NAME)
    limiter.check(caller, "deals_get_candidate")  # cheap reads keep their own bucket


def test_description_and_instructions_tell_dot_how_to_relay_the_result() -> None:
    description = CALC_TOOLS[NAME].description
    for phrase in (
        "customs duty",
        "motor-vehicle tax",
        "import VAT",
        "landed cost",
        "EUR 700",
        "indicative_preview",
        "rules that are not ACTIVE",
        "approved but not yet activated",
        "rule.status",
        "banner unchanged",
        "CO2 document",
        "proof of origin",
        "never 0",
        "Read-only",
        "no tool approves tax rules",
    ):
        assert phrase in description, phrase
    for phrase in (
        NAME,
        "customs duty",
        "motor-vehicle tax",
        "import VAT",
        "EUR 700",
        "indicative_preview",
        "rules that are not ACTIVE",
        "approved but not yet activated",
        "rule.status",
        "banner unchanged",
        "CO2 document",
        "proof of origin",
        "missing_facts",
        "never present an unknown charge as zero",
        "approves tax rules",
    ):
        assert phrase in INSTRUCTIONS, phrase
    # Regression: both texts called every preview "UNAPPROVED draft rules", contradicting the
    # banner of an approved-but-not-active (or under-review) rule set.
    assert "UNAPPROVED draft" not in description and "UNAPPROVED draft" not in INSTRUCTIONS
    # No rate, coefficient or threshold in the MCP layer: they live only in the rule-set JSON.
    for text in (description, INSTRUCTIONS, json.dumps(tool_input_schema(NAME))):
        assert "%" not in text and "percent" not in text.lower()


def test_the_transport_named_to_dot_is_the_configured_one() -> None:
    """The owner's fixed transport lives in the cost profile; the wording shown to dot names it, so
    a change of the profile must update the description and the server instructions too."""
    profile = load_cost_profile(REPO / "config" / "cost_profiles" / "default_unapproved.yaml")
    transport = single_transport_line(profile.lines(target_scope=CostScope()))
    assert transport.base is not None and transport.base.currency == "EUR"
    assert transport.low == transport.base == transport.high  # fixed, not a range
    named = f"EUR {transport.base.amount.normalize():f}"
    assert named in CALC_TOOLS[NAME].description and named in INSTRUCTIONS


# =========================================================================== input schema


def test_input_schema_is_closed_resolved_and_number_free() -> None:
    schema = tool_input_schema(NAME)
    text = json.dumps(schema)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert not find_refs(schema) and not open_objects(schema) and "$defs" not in text
    assert '"number"' not in text, "decimals are bounded strings"
    assert "required" not in schema  # listing_id and/or vehicle: a cross-field rule
    props = schema["properties"]
    # Mirrors the calculator service request field by field.
    assert set(props) == set(ImportCostRequest.model_fields)
    assert props["listing_id"] == {**props["listing_id"], "type": "string", "format": "uuid"}
    assert props["accept_previous_month_fx"]["type"] == "boolean"
    assert props["accept_previous_month_fx"]["default"] is False
    rate = props["customs_eur_mkd_rate"]
    assert rate["type"] == "string" and rate["maxLength"] <= 20 and rate["pattern"].startswith("^")


def test_vehicle_facts_mirror_the_domain_facts_with_closed_enums() -> None:
    vehicle = tool_input_schema(NAME)["properties"]["vehicle"]
    assert vehicle["type"] == "object" and vehicle["additionalProperties"] is False
    assert "required" not in vehicle  # every fact is optional; omitted keeps the listing's
    props = vehicle["properties"]
    assert set(props) == set(VehicleImportFacts.model_fields)
    assert props["fuel"]["enum"] == [f.value for f in Fuel]
    assert props["co2"]["maxItems"] == 3
    reading = props["co2"]["items"]
    assert reading["additionalProperties"] is False and reading["required"] == ["cycle", "g_km"]
    assert reading["properties"]["cycle"]["enum"] == [c.value for c in Co2Cycle]
    assert props["co2_document"]["enum"] == list(get_args(Co2Document))
    assert props["price_basis"]["enum"] == list(get_args(PriceBasis))
    assert props["importer_status"]["enum"] == list(get_args(ImporterStatus))
    origin = props["origin_proof"]
    assert origin["additionalProperties"] is False and origin["required"] == ["kind"]
    assert origin["properties"]["kind"]["enum"] == list(get_args(OriginProofKind))
    price = props["price"]
    assert price["required"] == ["amount", "currency"] and price["properties"]["currency"]["enum"] == [
        "EUR",
        "MKD",
        "CHF",
    ]
    assert props["declaration_date"] == {**props["declaration_date"], "type": "string", "format": "date"}
    for name in ("price", "transport_after_entry_eur", "catalogue_value_eur"):
        node = price["properties"]["amount"] if name == "price" else props[name]
        assert node["type"] == "string" and node["maxLength"] <= 20 and "pattern" in node, name
    for name in ("mileage_km", "engine_displacement_cm3", "power_kw"):
        assert props[name]["type"] == "string" and "pattern" in props[name], name
    for name in ("emissions_class", "hs_evidence_ref"):
        assert props[name]["maxLength"] <= 200, name


# =========================================================================== validation


def test_valid_arguments_become_the_service_request_with_only_the_set_facts() -> None:
    arguments = DealsEstimateImportCostsInput.model_validate(
        {"listing_id": UID, "vehicle": VEHICLE, "customs_eur_mkd_rate": "61.5337"}
    )
    request = import_cost_request(arguments)
    assert request.listing_id == UUID(UID)
    assert request.customs_eur_mkd_rate == Decimal("61.5337") and not request.accept_previous_month_fx
    facts = request.vehicle
    assert facts is not None and facts.model_fields_set == set(VEHICLE)
    assert facts.price is not None and facts.price.amount == Decimal("3000.00")
    assert facts.price.currency == "EUR" and facts.price_basis == "invoice"
    assert facts.declaration_date == date(2026, 10, 13) and facts.fuel == Fuel.DIESEL
    assert [(r.cycle, r.g_km) for r in facts.co2] == [(Co2Cycle.NEDC, Decimal("189"))]
    assert facts.first_registration == PartialDate(value="2012-05", precision=Precision.MONTH)
    assert facts.origin_proof.kind == "eu" and facts.origin_proof.country == "DE"
    assert facts.hs_code == "870332"  # separators stripped, HS-6; never inferred
    assert facts.transport_after_entry_eur == Decimal("150.00")
    assert facts.power_kw == Decimal("110.5") and facts.mileage_km == Decimal(182000)
    # Unset facts stay unset: they keep the listing's (merge_facts) instead of a default.
    partial = import_cost_request(
        DealsEstimateImportCostsInput.model_validate({"listing_id": UID, "vehicle": {"co2_document": "coc"}})
    )
    assert partial.vehicle is not None and partial.vehicle.model_fields_set == {"co2_document"}
    listing_only = import_cost_request(DealsEstimateImportCostsInput.model_validate({"listing_id": UID}))
    assert listing_only.vehicle is None and listing_only.customs_eur_mkd_rate is None
    assert import_cost_request(DealsEstimateImportCostsInput.model_validate({"vehicle": {}})).vehicle == (
        VehicleImportFacts()
    )


@pytest.mark.parametrize(
    ("arguments", "fields"),
    [
        ({}, ["listing_id", "vehicle"]),
        ({"listing_id": "not-a-uuid"}, ["listing_id"]),
        ({"listing_id": None}, ["listing_id"]),
        ({"vehicle": None}, ["vehicle"]),
        ({"listing_id": UID, "workspace_id": UID}, ["workspace_id"]),
        ({"listing_id": UID, "customs_eur_mkd_rate": 61.5337}, ["customs_eur_mkd_rate"]),
        ({"listing_id": UID, "customs_eur_mkd_rate": "0"}, ["customs_eur_mkd_rate"]),
        ({"listing_id": UID, "customs_eur_mkd_rate": "6.15e1"}, ["customs_eur_mkd_rate"]),
        ({"listing_id": UID, "accept_previous_month_fx": "yes"}, ["accept_previous_month_fx"]),
        ({"vehicle": {"price": {"amount": "3,000", "currency": "EUR"}}}, ["vehicle.price.amount"]),
        ({"vehicle": {"price": {"amount": 3000, "currency": "EUR"}}}, ["vehicle.price.amount"]),
        ({"vehicle": {"price": {"amount": "-1", "currency": "EUR"}}}, ["vehicle.price.amount"]),
        ({"vehicle": {"price": {"amount": "3000", "currency": "USD"}}}, ["vehicle.price.currency"]),
        ({"vehicle": {"price": {"amount": "3000"}}}, ["vehicle.price.currency"]),
        ({"vehicle": {"fuel": "steam"}}, ["vehicle.fuel"]),
        ({"vehicle": {"co2": [{"cycle": "nedc", "g_km": "1e3"}]}}, ["vehicle.co2.0.g_km"]),
        ({"vehicle": {"co2": [{"cycle": "epa", "g_km": "189"}]}}, ["vehicle.co2.0.cycle"]),
        (
            {"vehicle": {"co2": [{"cycle": "nedc", "g_km": "189"}, {"cycle": "nedc", "g_km": "190"}]}},
            ["vehicle.co2"],
        ),
        ({"vehicle": {"co2": [{"cycle": "wltp", "g_km": "2001"}]}}, ["vehicle.co2.0.g_km"]),
        ({"vehicle": {"co2_document": "seller_email"}}, ["vehicle.co2_document"]),
        ({"vehicle": {"co2_round_up_confirmed": "true"}}, ["vehicle.co2_round_up_confirmed"]),
        ({"vehicle": {"first_registration": "2026-02-30"}}, ["vehicle.first_registration"]),
        ({"vehicle": {"first_registration": "2012-13"}}, ["vehicle.first_registration"]),
        ({"vehicle": {"declaration_date": "1759744800"}}, ["vehicle.declaration_date"]),
        ({"vehicle": {"declaration_date": None}}, ["vehicle.declaration_date"]),
        ({"vehicle": {"origin_proof": {"kind": "efta"}}}, ["vehicle.origin_proof"]),
        ({"vehicle": {"origin_proof": {"kind": "none", "country": "DE"}}}, ["vehicle.origin_proof"]),
        ({"vehicle": {"origin_proof": {"kind": "eu", "country": "de"}}}, ["vehicle.origin_proof.country"]),
        ({"vehicle": {"hs_code": "8704 21 10"}}, ["vehicle.hs_code"]),
        ({"vehicle": {"hs_evidence_ref": "SYNTHETIC broker"}}, ["vehicle.hs_evidence_ref"]),
        (
            {"vehicle": {"transport_after_entry_eur": "150"}},
            ["vehicle.transport_after_entry_eur", "vehicle.transport_after_entry_separately_invoiced"],
        ),
        (
            {"vehicle": {"transport_after_entry_separately_invoiced": True}},
            ["vehicle.transport_after_entry_eur", "vehicle.transport_after_entry_separately_invoiced"],
        ),
        ({"vehicle": {"emissions_class": "Euro 5" + chr(0x202E)}}, ["vehicle.emissions_class"]),
        ({"vehicle": {"mileage_km": "200000000"}}, ["vehicle.mileage_km"]),
        ({"vehicle": {"power_kw": "0"}}, ["vehicle.power_kw"]),
        ({"vehicle": {"recipient": SECRET}}, ["vehicle.recipient"]),
        ({"vehicle": {"<script>": SECRET}}, ["<unrecognised field>"]),
        ({"vehicle": {"price": {"amount": SECRET, "currency": "EUR"}}}, ["vehicle.price.amount"]),
    ],
)
def test_invalid_arguments_name_their_fields_never_their_values(
    arguments: dict[str, Any], fields: list[str]
) -> None:
    assert fields_of(arguments) == fields


# =========================================================================== output and snapshot


def test_output_schema_is_a_closed_number_free_envelope_with_the_labels() -> None:
    schema = tool_output_schema(NAME)
    assert set(schema["required"]) == {
        "schema_version",
        "request_id",
        "as_of",
        "data",
        "warnings",
        "next_cursor",
    }
    text = json.dumps(schema)
    assert not find_refs(schema) and not open_objects(schema)
    assert '"number"' not in text, "money and decimals are strings"
    data = schema["properties"]["data"]
    assert data["additionalProperties"] is False
    props = data["properties"]
    assert props["mode"]["enum"] == ["active", "indicative_preview", "unavailable"]
    for field in (
        "production_ready",
        "banner",
        "rule",
        "usage_note",
        "selection_reason",
        "facts",
        "fact_sources",
        "scenarios",
        "assumptions",
        "warnings",
        "uncertain_items",
        "unverified_inputs",
        "gates",
        "missing_facts",
        "customs_fx_source",
    ):
        assert field in props, field
    scenario = props["scenarios"]["items"]["properties"]
    for field in (
        "total",
        "indicative_total",
        "known_subtotal",
        "landed_cost_eur",
        "indicative_landed_cost_eur",
    ):
        assert field in scenario, field
    assert scenario["components"]["items"]["properties"]["intermediate"]["type"] == "boolean"
    assert set(props["banner"]["properties"]) == {"mk", "en"}


def test_snapshot_document_is_exported() -> None:
    document = exported_schema_documents()[f"tools/{NAME}.json"]
    assert document == tool_document(NAME)
    assert document["requiredScope"] == "deals:read" and document["idempotencyOperation"] is None
    assert document["paginated"] is False and document["annotations"]["readOnlyHint"] is True
