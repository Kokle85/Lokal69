"""``deals_estimate_import_costs`` over Streamable HTTP (marker ``db``): dot's import-cost calculator.

North Macedonian customs duty, motor-vehicle tax and import VAT for one vehicle through the shared
calculator read (``queries.get_import_cost_estimate``). Covered: the indicative preview on the
shipped DRAFT rule set from vehicle facts alone, from a listing and with request overrides (the
fact sheet's golden totals G01/G02/G03), unknown inputs that change the result (never zero), the
ACTIVE rule set taking precedence, ``unavailable`` without any rule set, scope, validation, the
same ``NOT_FOUND`` for missing and foreign listings, the expensive rate-limit bucket, the official
SDK client's output-schema validation, and that a call persists nothing. Every workspace, listing
and rate is SYNTHETIC; the customs rate 61.5337 is the golden-case test input.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from suv_deals.api.middleware import RateLimit
from suv_deals.domain.enums import Role, Scope, TaxRuleStatus
from suv_deals.mcp.schemas import tool_output_schema
from suv_deals.mcp.server import McpOptions
from suv_deals.persistence.database import Database
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
from tests.mcp.conftest import (
    BASE_URL,
    MCP_URL,
    McpClient,
    SigningKeys,
    TokenFactory,
    Users,
    add_members,
    make_settings,
    mcp_client,
)

pytestmark = pytest.mark.db

NAME = "deals_estimate_import_costs"
G01 = "168154.45"  # D1: diesel, NEDC 189 on the COC, EUR 3,000 + EUR 700 transport, no proof of origin
G02 = "154721.64"  # D2: the same car with an accepted EU proof of origin
G03 = "217629.97"  # D3: petrol, NEDC 245 on the COC, EUR 2,500 + EUR 700
SECRET = "Zz" * 20  # a value that must never be echoed
D1: dict[str, Any] = {
    "price": {"amount": "3000.00", "currency": "EUR"},
    "price_basis": "invoice",
    "declaration_date": "2026-10-13",
    "fuel": "diesel",
    "co2": [{"cycle": "nedc", "g_km": "189"}],
    "co2_document": "coc",
}


@dataclass
class CalcHarness:
    client: McpClient
    tokens: TokenFactory
    users: Users
    db: Database
    seed: Seed
    workspace_id: UUID
    listing_id: UUID

    @property
    def viewer(self) -> str:
        return self.tokens.for_role(self.users.viewer, Role.VIEWER)

    @property
    def reviewer(self) -> str:
        return self.tokens.for_role(self.users.reviewer, Role.REVIEWER)

    async def ok(self, arguments: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
        """A successful call, validated against the ``outputSchema`` the server publishes."""
        structured = await self.client.ok(NAME, arguments, token=token or self.viewer)
        tools = {t["name"]: t for t in await self.client.tools(token or self.viewer)}
        assert tools[NAME]["outputSchema"] == tool_output_schema(NAME)
        errors = list(Draft202012Validator(tools[NAME]["outputSchema"]).iter_errors(structured))
        assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
        return structured


@pytest.fixture
async def calc_mcp(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[CalcHarness]:
    ws, source, key = workspace(seed, "MCP import costs")
    listing = add_listing(seed, ws, source, key)  # SYNTHETIC D1 diesel, listed NEDC 189, EUR 3,000
    users = add_members(seed, ws)
    async with mcp_client(make_settings(), db, keys) as client:
        yield CalcHarness(
            client=client, tokens=tokens, users=users, db=db, seed=seed, workspace_id=ws, listing_id=listing
        )


def legal_base(data: dict[str, Any]) -> dict[str, Any]:
    scenario: dict[str, Any] = data["scenarios"][0]
    assert scenario["name"] == "legal_base"
    return scenario


def codes(envelope: dict[str, Any]) -> set[str]:
    return {w["code"] for w in envelope["warnings"]}


# --------------------------------------------------------------------------------------------
# Indicative preview (no ACTIVE rule set: the shipped DRAFT)
# --------------------------------------------------------------------------------------------


async def test_preview_from_vehicle_facts_alone_is_labelled_and_persists_nothing(
    calc_mcp: CalcHarness,
) -> None:
    before = counts(calc_mcp.seed, calc_mcp.workspace_id)
    envelope = await calc_mcp.ok({"vehicle": D1, "customs_eur_mkd_rate": CUSTOMS_RATE})
    assert counts(calc_mcp.seed, calc_mcp.workspace_id) == before  # no valuation, rate, audit or job row
    data = envelope["data"]
    assert data["mode"] == "indicative_preview" and data["production_ready"] is False
    assert data["banner"]["en"].startswith("INDICATIVE PREVIEW") and data["banner"]["mk"]
    assert "unapproved draft rules" in data["banner"]["en"]
    assert data["rule"]["rule_set_id"] == "mk-passenger-car-import" and data["rule"]["status"] == "draft"
    assert data["rule"]["sha256_recorded"] is False  # an unsealed draft shows its content hash
    legal = legal_base(data)
    assert legal["complete"] is True and legal["production_ready"] is False
    assert legal["indicative_total"]["amount"] == G01 and legal["indicative_total"]["currency"] == "MKD"
    assert legal["total"] is None and legal["landed_cost_eur"] is None
    assert legal["indicative_landed_cost_eur"]["status"] == "known"
    assert data["customs_fx_source"] == "caller_supplied" and data["listing"] is None
    assert data["customs_fx_rate"]["rate"] == CUSTOMS_RATE
    assert any(a.startswith("CALLER_SUPPLIED_CUSTOMS_RATE") for a in data["assumptions"])
    assert any(a.startswith("TRANSPORT_IN_CUSTOMS_VALUE:700.00 EUR") for a in data["assumptions"])
    assert {s["fact"]: s["source"] for s in data["fact_sources"]}["price"] == "request"
    assert "TAX_RULES_UNAPPROVED" in codes(envelope)
    assert "display only" in data["usage_note"].lower()
    assert envelope["request_id"].startswith("req-") and envelope["next_cursor"] is None


async def test_preview_from_a_listing_uses_its_facts_and_the_recorded_customs_rate(
    calc_mcp: CalcHarness,
) -> None:
    await record_customs_rate(calc_mcp.db, calc_mcp.workspace_id)
    envelope = await calc_mcp.ok(
        {
            "listing_id": str(calc_mcp.listing_id),
            "vehicle": {"co2_document": "coc", "declaration_date": "2026-10-13"},
        },
        token=calc_mcp.reviewer,
    )
    data = envelope["data"]
    assert data["mode"] == "indicative_preview"
    assert legal_base(data)["indicative_total"]["amount"] == G01
    assert (
        data["listing"]["listing_id"] == str(calc_mcp.listing_id) and data["listing"]["is_fixture"] is False
    )
    assert data["customs_fx_source"] == "recorded"
    sources = {s["fact"]: s["source"] for s in data["fact_sources"]}
    assert sources["price"] == "listing" and sources["co2"] == "listing"
    assert sources["co2_document"] == "request" and sources["origin_proof"] == "default"
    # The listing's price is an advertised claim, labelled as such.
    assert "SELLER_CLAIMS_UNVERIFIED" in codes(envelope)
    assert any(item.startswith("price:") for item in data["unverified_inputs"])


async def test_vehicle_facts_override_the_listing(calc_mcp: CalcHarness) -> None:
    await record_customs_rate(calc_mcp.db, calc_mcp.workspace_id)
    envelope = await calc_mcp.ok(
        {
            "listing_id": str(calc_mcp.listing_id),
            "vehicle": {
                "price": {"amount": "2500.00", "currency": "EUR"},
                "price_basis": "invoice",
                "fuel": "petrol",
                "co2": [{"cycle": "nedc", "g_km": "245"}],
                "co2_document": "coc",
                "declaration_date": "2026-10-13",
            },
        }
    )
    data = envelope["data"]
    assert legal_base(data)["indicative_total"]["amount"] == G03
    sources = {s["fact"]: s["source"] for s in data["fact_sources"]}
    assert {sources[f] for f in ("price", "fuel", "co2", "co2_document")} == {"request"}
    assert sources["seller_country"] == "listing" and sources["first_registration"] == "listing"
    assert data["facts"]["fuel"] == "petrol" and data["facts"]["price"]["amount"] == "2500.00"


async def test_unknown_inputs_change_the_result_and_are_never_zero(calc_mcp: CalcHarness) -> None:
    unknown = {k: v for k, v in D1.items() if k != "co2_document"} | {"origin_proof": {"kind": "unknown"}}
    envelope = await calc_mcp.ok({"vehicle": unknown, "customs_eur_mkd_rate": CUSTOMS_RATE})
    data = envelope["data"]
    legal = legal_base(data)
    assert legal["complete"] is False
    assert legal["indicative_total"] == {**legal["indicative_total"], "status": "unknown", "amount": None}
    assert legal["known_subtotal"] is not None and legal["unknown_components"]
    assert {"co2_document", "origin_proof"} <= set(data["missing_facts"])
    assert "UNKNOWN_COSTS" in codes(envelope)
    # A held EU proof of origin changes the duty; the standard-duty alternative is shown next to it.
    proof = await calc_mcp.ok(
        {
            "vehicle": D1 | {"origin_proof": {"kind": "eu", "country": "DE"}},
            "customs_eur_mkd_rate": CUSTOMS_RATE,
        }
    )
    scenarios = {s["name"]: s for s in proof["data"]["scenarios"]}
    assert scenarios["legal_base"]["indicative_total"]["amount"] == G02
    assert scenarios["standard_duty_if_proof_rejected"]["indicative_total"]["amount"] == G01


@pytest.mark.parametrize("first_registration", ["0000", "0000-05", "1800-01-01"])
async def test_an_implausible_first_registration_is_a_labelled_unknown_not_an_internal_error(
    calc_mcp: CalcHarness, first_registration: str
) -> None:
    """Regression: the shared builder raised, so dot got INTERNAL_ERROR with retryable true (an
    invitation to retry a call that always fails)."""
    envelope = await calc_mcp.ok(
        {"vehicle": D1 | {"first_registration": first_registration}, "customs_eur_mkd_rate": CUSTOMS_RATE}
    )
    warnings = envelope["data"]["warnings"]
    assert any(w.startswith(f"FIRST_REGISTRATION_IMPLAUSIBLE:{first_registration}") for w in warnings)
    assert legal_base(envelope["data"])["indicative_total"]["amount"] == G01  # the age never changes it


# --------------------------------------------------------------------------------------------
# Active and unavailable
# --------------------------------------------------------------------------------------------


async def test_an_active_rule_set_takes_precedence_over_the_preview(calc_mcp: CalcHarness) -> None:
    # Activated through the real owner transitions in this throwaway SYNTHETIC workspace only.
    await activate(calc_mcp.db, owner(calc_mcp.workspace_id), mk_draft())
    await record_customs_rate(calc_mcp.db, calc_mcp.workspace_id)
    envelope = await calc_mcp.ok({"vehicle": D1})
    data = envelope["data"]
    assert data["mode"] == "active" and data["banner"] is None
    assert data["rule"]["status"] == "active" and data["rule"]["sha256_recorded"] is True
    legal = legal_base(data)
    assert legal["total"]["amount"] == G01 and legal["indicative_total"] is None
    assert "TAX_RULES_UNAPPROVED" not in codes(envelope)


async def test_an_approved_but_inactive_rule_set_is_relayed_as_such(calc_mcp: CalcHarness) -> None:
    """Regression: dot was told every preview is "UNAPPROVED draft rules"; between Approve and
    Activate the data says approved, and the instructions now tell dot to relay that banner."""
    await activate(calc_mcp.db, owner(calc_mcp.workspace_id), mk_draft(), until=TaxRuleStatus.APPROVED)
    envelope = await calc_mcp.ok({"vehicle": D1, "customs_eur_mkd_rate": CUSTOMS_RATE})
    data = envelope["data"]
    assert data["mode"] == "indicative_preview" and data["rule"]["status"] == "approved"
    assert "approved rules that are not yet active" in data["banner"]["en"]
    assert "unapproved" not in data["banner"]["en"]
    tools = {t["name"]: t for t in await calc_mcp.client.tools(calc_mcp.viewer)}
    assert "UNAPPROVED draft" not in tools[NAME]["description"]
    assert "approved but not yet activated" in tools[NAME]["description"]


async def test_without_any_rule_set_every_charge_is_unknown(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, tmp_path: Path
) -> None:
    config = tmp_path / "config"
    (config / "cost_profiles").mkdir(parents=True)
    shutil.copy(CONFIG / "cost_profiles" / "default_unapproved.yaml", config / "cost_profiles")
    ws, _, _ = workspace(seed, "MCP import costs unavailable")
    users = add_members(seed, ws)
    async with mcp_client(make_settings(config_dir=config), db, keys) as client:
        envelope = await client.ok(
            NAME,
            {"vehicle": D1, "customs_eur_mkd_rate": CUSTOMS_RATE},
            token=tokens.for_role(users.viewer, Role.VIEWER),
        )
    data = envelope["data"]
    assert data["mode"] == "unavailable" and data["production_ready"] is False
    assert data["rule"] is None and data["scenarios"] == []
    assert {"TAX_RULES_UNAPPROVED", "UNKNOWN_COSTS"} <= codes(envelope)


# --------------------------------------------------------------------------------------------
# Scope, validation, isolation, limits
# --------------------------------------------------------------------------------------------


async def test_the_tool_needs_deals_read(calc_mcp: CalcHarness) -> None:
    reviews_only = calc_mcp.tokens.mint(calc_mcp.users.reviewer, scopes=[Scope.REVIEWS_READ])
    assert NAME not in [t["name"] for t in await calc_mcp.client.tools(reviews_only)]
    denied = await calc_mcp.client.fails(NAME, {"garbage": SECRET}, token=reviews_only, code="FORBIDDEN")
    assert "deals:read" in denied["message"] and "fields" not in (denied.get("details") or {})
    assert SECRET not in str(denied)
    users = calc_mcp.users
    members = ((users.viewer, Role.VIEWER), (users.reviewer, Role.REVIEWER), (users.owner, Role.OWNER))
    for user, role in members:
        token = calc_mcp.tokens.for_role(user, role)
        assert NAME in [t["name"] for t in await calc_mcp.client.tools(token)], role


async def test_invalid_arguments_are_validation_errors_naming_fields_only(calc_mcp: CalcHarness) -> None:
    listing = str(calc_mcp.listing_id)
    bad: list[tuple[dict[str, Any], list[str]]] = [
        ({}, ["listing_id", "vehicle"]),
        ({"listing_id": "not-a-uuid"}, ["listing_id"]),
        ({"listing_id": listing, "workspace_id": listing}, ["workspace_id"]),
        ({"listing_id": listing, "customs_eur_mkd_rate": 61.5337}, ["customs_eur_mkd_rate"]),
        ({"vehicle": {"price": {"amount": SECRET, "currency": "EUR"}}}, ["vehicle.price.amount"]),
        ({"vehicle": {"co2": [{"cycle": "nedc", "g_km": "1e3"}]}}, ["vehicle.co2.0.g_km"]),
        ({"vehicle": {"co2_document": "seller_email"}}, ["vehicle.co2_document"]),
        ({"vehicle": {"hs_code": "8704 21 10"}}, ["vehicle.hs_code"]),
        (
            {"vehicle": {"transport_after_entry_eur": "150"}},
            ["vehicle.transport_after_entry_eur", "vehicle.transport_after_entry_separately_invoiced"],
        ),
        ({"vehicle": {"recipient": SECRET}}, ["vehicle.recipient"]),
    ]
    for arguments, fields in bad:
        payload = await calc_mcp.client.fails(NAME, arguments, token=calc_mcp.viewer, code="VALIDATION_ERROR")
        assert payload["details"]["fields"] == fields, (arguments, payload)
        assert SECRET not in str(payload)


async def test_missing_and_foreign_listings_are_the_same_not_found(calc_mcp: CalcHarness) -> None:
    other_ws, other_source, other_key = workspace(calc_mcp.seed, "MCP import costs foreign")
    foreign = add_listing(calc_mcp.seed, other_ws, other_source, other_key)
    viewer = calc_mcp.viewer
    missing = await calc_mcp.client.fails(
        NAME, {"listing_id": str(uuid.uuid4())}, token=viewer, code="NOT_FOUND"
    )
    other = await calc_mcp.client.fails(NAME, {"listing_id": str(foreign)}, token=viewer, code="NOT_FOUND")
    assert {k: v for k, v in missing.items() if k != "correlation_id"} == {
        k: v for k, v in other.items() if k != "correlation_id"
    }
    assert str(foreign) not in str(other)


async def test_the_calculator_uses_the_expensive_bucket(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    ws, _, _ = workspace(seed, "MCP import costs rate limit")
    users = add_members(seed, ws)
    options = McpOptions(
        expensive_limit=RateLimit(capacity=1, per_seconds=60.0),
        cheap_limit=RateLimit(capacity=50, per_seconds=1.0),
    )
    async with mcp_client(make_settings(), db, keys, options=options) as client:
        viewer = tokens.for_role(users.viewer, Role.VIEWER)
        arguments = {"vehicle": D1, "customs_eur_mkd_rate": CUSTOMS_RATE}
        await client.ok(NAME, arguments, token=viewer)
        limited = await client.fails(NAME, arguments, token=viewer, code="RATE_LIMITED")
        assert limited["retryable"] is True and 1 <= limited["retry_after_seconds"] <= 60
        # Cheap single-object reads keep their own bucket.
        await client.fails(
            "deals_get_candidate", {"listing_id": str(uuid.uuid4())}, token=viewer, code="NOT_FOUND"
        )


async def test_official_sdk_client_validates_the_result(calc_mcp: CalcHarness) -> None:
    headers = {"Authorization": f"Bearer {calc_mcp.viewer}"}
    transport = httpx2.ASGITransport(app=calc_mcp.client.app)
    async with (
        httpx2.AsyncClient(transport=transport, base_url=BASE_URL, headers=headers) as http,
        Client(streamable_http_client(MCP_URL, http_client=http)) as sdk,
    ):
        listed = await sdk.list_tools()
        assert NAME in [t.name for t in listed.tools]
        # The SDK client validates structuredContent against the tool's outputSchema.
        result = await sdk.call_tool(NAME, {"vehicle": D1, "customs_eur_mkd_rate": CUSTOMS_RATE})
        assert result.is_error is False and result.structured_content is not None
        assert result.structured_content["data"]["mode"] == "indicative_preview"
        refused = await sdk.call_tool(NAME, {})
        assert refused.is_error is True
