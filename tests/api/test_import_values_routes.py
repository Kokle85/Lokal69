"""A listing's recorded import values (``api.import_values_routes``; docs/api_contract.md 10.2):
``GET`` / ``POST /api/listings/{listing_id}/import-values``.

Owner decision 2026-10-13: the REAL purchase price (profit, landed cost) and the ESTIMATED customs
value (import charges) are two separate values per vehicle. Covered: the route table (reading
``deals:read``, recording ``config:admin`` with a body idempotency key, no MCP tool) and the
published envelope schemas; recording and reading back (whole-euro ``amount`` plus the exact
``amount_exact``); owner-only recording (reviewers and viewers get ``403`` and change nothing);
workspace isolation (``404``); idempotent replays, key reuse and the header rule; validation that
names fields only; the calculator's listing mode using the recorded values.

Real ASGI app, real PostgreSQL, locally signed Supabase-shaped JWTs; every workspace, listing and
value is SYNTHETIC (the owner's own example: bought for EUR 2,800, SchwackeNet EUR 4,200).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import cache
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
from tests.integration.import_costs.support import CONFIG, add_listing, workspace
from tests.integration.read_queries.schema_check import walk_numbers

from suv_deals.api import import_values_routes
from suv_deals.api.schemas import V11_ROUTE_INDEX, route_slug
from suv_deals.domain.enums import Scope
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.views.jsonschema import model_schema

pytestmark = pytest.mark.db

GET_KEY = "GET /api/listings/{listing_id}/import-values"
POST_KEY = "POST /api/listings/{listing_id}/import-values"
PAID = {
    "amount": "2800.50",
    "currency": "EUR",
    "kind": "actual",
    "source": "seller invoice",
    "document_ref": "INV-1",
}
SCHWACKE = {"amount_eur": "4200", "source": "SchwackeNet catalogue"}


@cache
def _validator(route_key: str) -> Draft202012Validator:
    schema = model_schema(V11_ROUTE_INDEX[route_key].response_model, mode="serialization")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(response: httpx.Response, route_key: str) -> dict[str, Any]:
    assert response.status_code == 200, (response.status_code, response.text[:800])
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    errors = sorted(_validator(route_key).iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    assert walk_numbers(body["data"]) == []  # amounts are strings, never JSON numbers
    data: dict[str, Any] = body["data"]
    return data


def key(name: str) -> str:
    return f"{name}:{uuid.uuid4().hex}"


def path(listing: UUID | str) -> str:
    return f"/api/listings/{listing}/import-values"


@dataclass
class ValuesHarness(ApiHarness):
    seed: Seed = None  # type: ignore[assignment]
    listing_id: UUID = None  # type: ignore[assignment]
    source_id: UUID = None  # type: ignore[assignment]
    source_key: str = ""

    def rows(self) -> int:
        return int(
            self.seed.scalar(
                "select count(*) from app.cost_evidence where workspace_id = %s", (self.workspace_id,)
            )
        )

    async def record(
        self, body: dict[str, Any], *, user: UUID | None = None, **kwargs: Any
    ) -> httpx.Response:
        payload = {"idempotency_key": key("record"), **body}
        return await self.post(path(self.listing_id), user or self.users.owner, payload, **kwargs)


@pytest.fixture
async def values_api(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[ValuesHarness]:
    ws, source, source_key = workspace(seed, "API import values")
    users = add_members(seed, ws)
    listing = add_listing(seed, ws, source, source_key)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(make_settings(config_dir=CONFIG), keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield ValuesHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=ws,
            metrics=metrics,
            seed=seed,
            listing_id=listing,
            source_id=source,
            source_key=source_key,
        )


# ------------------------------------------------------------------------------- route table


def test_route_table_reads_with_deals_read_and_records_owner_only() -> None:
    read, write = V11_ROUTE_INDEX[GET_KEY], V11_ROUTE_INDEX[POST_KEY]
    assert read.scope == Scope.DEALS_READ and read.request_model is None and read.idempotency_header is None
    assert write.scope == Scope.CONFIG_ADMIN and write.request_location == "body"
    assert write.idempotency_header == "optional"
    assert read.mcp_tool is None and write.mcp_tool is None  # never an MCP tool
    assert route_slug(write) == "listings.listing_id.import-values.post"
    assert {import_values_routes.IMPORT_VALUES.key, import_values_routes.RECORD.key} == {GET_KEY, POST_KEY}


# ------------------------------------------------------------------------------- record and read


async def test_owner_records_and_everyone_in_the_workspace_reads(values_api: ValuesHarness) -> None:
    first_key = key("record")
    recorded = valid(
        await values_api.record(
            {"purchase_price": PAID, "estimated_customs_value": SCHWACKE, "idempotency_key": first_key},
            headers={"Idempotency-Key": first_key},
        ),
        POST_KEY,
    )
    assert len(recorded["recorded_ids"]) == 2 and recorded["superseded_ids"] == []
    assert recorded["stale_valuation_ids"] == [] and recorded["recompute_job_id"]
    values = recorded["values"]
    price, estimate = values["purchase_price"], values["estimated_customs_value"]
    # Whole euros for display (owner decision "без децимали"), the exact amount as recorded.
    assert price["amount"] == {"status": "known", "amount": "2801", "currency": "EUR", "reason": None}
    assert price["amount_exact"] == "2800.50" and price["kind"] == "actual"
    assert (price["source"], price["document_ref"], price["current"]) == ("seller invoice", "INV-1", True)
    assert estimate["amount"]["amount"] == "4200" and estimate["amount_exact"] == "4200.00"
    assert (estimate["category"], estimate["kind"], estimate["source"]) == (
        "customs_value",
        "estimate",
        "SchwackeNet catalogue",
    )
    assert str(values_api.users.owner) in {price["recorded_by_principal"], estimate["recorded_by_principal"]}
    for user in (values_api.users.viewer, values_api.users.reviewer, values_api.users.owner):
        read = valid(await values_api.get(path(values_api.listing_id), user), GET_KEY)
        assert read == values
    # A new value supersedes the current one; the history keeps both.
    quote = {"amount": "2750", "currency": "EUR", "kind": "quote", "source": "agreed with the seller"}
    second = valid(await values_api.record({"purchase_price": quote}), POST_KEY)
    assert second["superseded_ids"] == [price["id"]]
    history = [r for r in second["values"]["history"] if r["category"] == "purchase"]
    assert [(r["amount_exact"], r["current"]) for r in history] == [("2750.00", True), ("2800.50", False)]
    assert values_api.rows() == 3


async def test_reviewers_and_viewers_cannot_record(values_api: ValuesHarness) -> None:
    for user in (values_api.users.reviewer, values_api.users.viewer):
        refused = await values_api.record({"purchase_price": PAID}, user=user)
        assert refused.status_code == 403 and error_of(refused)["code"] == "FORBIDDEN"
    stranger = await values_api.get(path(values_api.listing_id), values_api.users.stranger)
    assert stranger.status_code == 403
    assert values_api.rows() == 0


async def test_a_foreign_or_missing_listing_is_not_found(values_api: ValuesHarness, seed: Seed) -> None:
    other_ws, other_source, other_key = workspace(seed, "API import values other")
    foreign = add_listing(seed, other_ws, other_source, other_key)
    for listing in (foreign, uuid.uuid4()):
        missing = await values_api.get(path(listing), values_api.users.owner)
        assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
        refused = await values_api.post(
            path(listing), values_api.users.owner, {"purchase_price": PAID, "idempotency_key": key("x")}
        )
        assert refused.status_code == 404 and error_of(refused)["code"] == "NOT_FOUND"
    malformed = await values_api.get(path("not-a-uuid"), values_api.users.owner)
    assert malformed.status_code == 422 and error_of(malformed)["details"]["fields"] == ["listing_id"]
    assert values_api.rows() == 0
    assert (
        int(seed.scalar("select count(*) from app.cost_evidence where workspace_id = %s", (other_ws,))) == 0
    )


async def test_idempotent_replay_key_reuse_and_header_rule(values_api: ValuesHarness) -> None:
    body = {"estimated_customs_value": SCHWACKE, "idempotency_key": key("replay")}
    first = valid(await values_api.record(body), POST_KEY)
    replay = valid(await values_api.record(body), POST_KEY)
    assert replay == first and values_api.rows() == 1  # the original result, nothing recorded again
    changed = await values_api.record({**body, "estimated_customs_value": {**SCHWACKE, "amount_eur": "4300"}})
    assert changed.status_code == 409 and error_of(changed)["code"] == "IDEMPOTENCY_CONFLICT"
    # The key is bound to the listing: the same body for another listing is another request.
    other = add_listing(values_api.seed, values_api.workspace_id, values_api.source_id, values_api.source_key)
    elsewhere = await values_api.post(path(other), values_api.users.owner, body)
    assert elsewhere.status_code == 409 and error_of(elsewhere)["code"] == "IDEMPOTENCY_CONFLICT"
    header = await values_api.record(
        {"purchase_price": PAID, "idempotency_key": key("header")},
        headers={"Idempotency-Key": "other-key-0001"},
    )
    assert header.status_code == 422 and error_of(header)["details"]["fields"] == ["Idempotency-Key"]
    assert values_api.rows() == 1


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({}, ["estimated_customs_value", "purchase_price"]),
        ({"purchase_price": {**PAID, "amount": "0"}}, ["purchase_price.amount"]),
        ({"purchase_price": {**PAID, "amount": "2800.505"}}, ["purchase_price.amount"]),
        ({"purchase_price": {**PAID, "amount": 2800}}, ["purchase_price.amount"]),
        ({"purchase_price": {**PAID, "currency": "MKD"}}, ["purchase_price.currency"]),
        ({"purchase_price": {**PAID, "kind": "paid"}}, ["purchase_price.kind"]),
        ({"purchase_price": {**PAID, "source": "ab"}}, ["purchase_price.source"]),
        (
            {"estimated_customs_value": {**SCHWACKE, "amount_eur": "0.00"}},
            ["estimated_customs_value.amount_eur"],
        ),
        ({"estimated_customs_value": {"amount_eur": "4200"}}, ["estimated_customs_value.source"]),
        (
            {"estimated_customs_value": {**SCHWACKE, "transport": "700"}},
            ["estimated_customs_value.transport"],
        ),
        ({"purchase_price": PAID, "listing_id": str(uuid.uuid4())}, ["listing_id"]),
    ],
)
async def test_invalid_requests_name_the_fields(
    values_api: ValuesHarness, body: dict[str, Any], fields: list[str]
) -> None:
    refused = await values_api.record(body)
    assert refused.status_code == 422, refused.text
    error = error_of(refused)
    assert error["code"] == "VALIDATION_ERROR" and error["details"]["fields"] == fields
    echoed = f"{error['message']} {error['details']}"
    assert "2800" not in echoed and "4200" not in echoed  # values are never echoed
    assert values_api.rows() == 0


# ------------------------------------------------------------------------------- calculator


async def test_the_calculator_uses_the_recorded_values(values_api: ValuesHarness) -> None:
    valid(await values_api.record({"purchase_price": PAID, "estimated_customs_value": SCHWACKE}), POST_KEY)
    response = await values_api.get(
        "/api/import-costs/estimate",
        values_api.users.viewer,
        params={"listing_id": str(values_api.listing_id), "co2_document": "coc"},
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()["data"]
    sources = {s["fact"]: s["source"] for s in data["fact_sources"]}
    assert sources["price"] == "recorded" and sources["estimated_customs_value_eur"] == "recorded"
    assert data["purchase_price"]["amount"]["amount"] == "2801"  # EUR 2,800.50, whole euros
    assert data["purchase_price"]["basis"] == "recorded_actual"
    assert data["customs_value_for_duties"]["basis"] == "estimated"
    assert data["customs_value_for_duties"]["amount"]["amount"] == "4900"
    assert data["listing"]["recorded_purchase_price_id"] is not None
    # A request price still wins over the recorded one.
    override = await values_api.get(
        "/api/import-costs/estimate",
        values_api.users.viewer,
        params={
            "listing_id": str(values_api.listing_id),
            "price_amount": "3000",
            "price_currency": "EUR",
            "price_basis": "invoice",
        },
    )
    assert override.status_code == 200
    overridden = override.json()["data"]
    assert {s["fact"]: s["source"] for s in overridden["fact_sources"]}["price"] == "request"
    assert overridden["purchase_price"]["amount"]["amount"] == "3000"


async def test_a_lone_price_basis_cannot_relabel_the_recorded_purchase_price(
    values_api: ValuesHarness,
) -> None:
    """Regression: ``?listing_id=..&price_basis=invoice`` kept the owner's recorded estimate of the
    purchase price but labelled it an invoice price (and dropped its unverified flag). Refused on
    ``price_basis``; the recorded value keeps its recorded kind."""
    guess = {"amount": "2500", "currency": "EUR", "kind": "estimate", "source": "owner guess"}
    valid(await values_api.record({"purchase_price": guess}), POST_KEY)
    params = {"listing_id": str(values_api.listing_id), "co2_document": "coc"}
    refused = await values_api.get(
        "/api/import-costs/estimate", values_api.users.viewer, params={**params, "price_basis": "invoice"}
    )
    assert refused.status_code == 422, refused.text[:500]
    error = error_of(refused)
    assert error["code"] == "VALIDATION_ERROR" and error["details"] == {"fields": ["price_basis"]}
    kept = await values_api.get("/api/import-costs/estimate", values_api.users.viewer, params=params)
    assert kept.status_code == 200
    data = kept.json()["data"]
    assert data["purchase_price"]["basis"] == "recorded_estimate"
    assert "price: recorded estimate, not an invoice or contract price" in data["unverified_inputs"]
    assert "SELLER_CLAIMS_UNVERIFIED" in {w["code"] for w in kept.json()["warnings"]}
