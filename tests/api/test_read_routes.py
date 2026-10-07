"""Read routes of the dashboard API over the SYNTHETIC read-query dataset (marker ``db``).

Every response is validated against the route's published ``ResponseEnvelope[<view>]`` schema;
foreign-workspace ids are indistinguishable from missing ones; cursors are signed and bound to
query, filters, workspace and principal.
"""

from __future__ import annotations

import uuid
from functools import cache
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from tests.api.conftest import UNREACHABLE_DB, DataHarness, comparable_error, error_of, make_settings
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import seed_foreign_workspace
from tests.integration.read_queries.schema_check import walk_numbers

from suv_deals.api import routes
from suv_deals.api.app import create_app
from suv_deals.api.schemas import ROUTE_INDEX, ROUTES
from suv_deals.persistence.database import Database
from suv_deals.views.jsonschema import model_schema

pytestmark = pytest.mark.db


@cache
def _validator(route_key: str) -> Draft202012Validator:
    schema = model_schema(ROUTE_INDEX[route_key].response_model, mode="serialization")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(response: httpx.Response, route_key: str) -> dict[str, Any]:
    route = ROUTE_INDEX[route_key]
    assert response.status_code == route.success_status, (response.status_code, response.text[:500])
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    errors = sorted(_validator(route_key).iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    if "request_id" in body:
        assert body["request_id"] == response.headers["x-request-id"]
        assert body["as_of"].endswith("Z")
    return body


def test_the_app_serves_exactly_the_contract_routes() -> None:
    assert create_app(make_settings(), db=Database(UNREACHABLE_DB)) is not None
    served = {
        f"{method} {route.path}"
        for route in routes.router.routes
        for method in getattr(route, "methods", ()) or ()
        if method != "HEAD"
    }
    assert served == {route.key for route in ROUTES}


async def test_every_read_route_returns_its_published_schema(data_api: DataHarness) -> None:
    d = data_api.data
    viewer = data_api.users.viewer
    checks = {
        "GET /api/me": "/api/me",
        "GET /api/overview": "/api/overview",
        "GET /api/candidates": "/api/candidates",
        "GET /api/candidates/{listing_id}": f"/api/candidates/{d.listings['priced']}",
        "GET /api/comparables/{set_id}": f"/api/comparables/{d.comparable_set_id}?include_excluded=true",
        "GET /api/valuations/{valuation_id}": f"/api/valuations/{d.valuations['estimated']}",
        "GET /api/reviews": "/api/reviews",
        "GET /api/reviews/{case_id}": f"/api/reviews/{d.cases['priced']}",
        "GET /api/sources": "/api/sources",
        "GET /api/settings": "/api/settings",
        "GET /api/outbox": "/api/outbox",
    }
    for key, path in checks.items():
        body = valid(await data_api.get(path, viewer), key)
        assert walk_numbers(body["data"]) == [], key  # money and decimals are strings, never JSON numbers
    candidates = (await data_api.get("/api/candidates", viewer)).json()["data"]["items"]
    assert {item["listing_id"] for item in candidates} == {str(i) for i in d.candidate_ids}
    older = valid(
        await data_api.get(f"/api/candidates/{d.listings['priced']}?revision=1", viewer),
        "GET /api/candidates/{listing_id}",
    )
    assert "REVISION_NOT_CURRENT" in {w["code"] for w in older["warnings"]}
    outbox = (await data_api.get("/api/outbox?state=uncertain", viewer)).json()["data"]["items"]
    assert [item["state"] for item in outbox] == ["uncertain"]
    assert all("payload" not in item for item in outbox)
    queue = (await data_api.get("/api/reviews", viewer)).json()
    assert "FROZEN_QUEUE_PROJECTION" in {w["code"] for w in queue["warnings"]}
    assert "claim_token" not in str(queue)


async def test_settings_show_owner_only_administration(data_api: DataHarness) -> None:
    owner = (await data_api.get("/api/settings", data_api.users.owner)).json()["data"]
    viewer = (await data_api.get("/api/settings", data_api.users.viewer)).json()["data"]
    assert owner["can_administer"] is True
    assert viewer["can_administer"] is False
    assert viewer["contribution_threshold"]["label"] == "PROPOSED"
    manual = next(p for p in viewer["profiles"] if p["profile_key"] == "manual_4000")
    assert manual["status_label"].startswith("DISABLED")


async def test_foreign_workspace_ids_are_identical_to_missing_ones(
    data_api: DataHarness, db: Database, seed: Seed
) -> None:
    foreign = await seed_foreign_workspace(db, seed, "API-B")
    reviewer = data_api.users.reviewer
    pairs = {
        "/api/candidates/{}": (foreign.listings["priced"], uuid.uuid4()),
        "/api/valuations/{}": (foreign.valuations["not_started"], uuid.uuid4()),
        "/api/comparables/{}": (foreign.comparable_set_id, uuid.uuid4()),
        "/api/reviews/{}": (foreign.cases["priced"], uuid.uuid4()),
    }
    for template, (foreign_id, missing_id) in pairs.items():
        a = await data_api.get(template.format(foreign_id), reviewer)
        b = await data_api.get(template.format(missing_id), reviewer)
        assert a.status_code == b.status_code == 404, template
        assert comparable_error(a) == comparable_error(b), template
        assert str(foreign_id) not in a.text
    writes = {
        "/api/reviews/{}/claim": (
            foreign.cases["priced"],
            {"expected_version": 1, "idempotency_key": "foreign-claim-01"},
        ),
        "/api/reviews/{}/release": (
            foreign.cases["priced"],
            {"claim_token": "A" * 43, "idempotency_key": "foreign-release1"},
        ),
        "/api/listings/{}/notes": (
            foreign.listings["priced"],
            {"note": "probe", "idempotency_key": "foreign-note-001"},
        ),
        "/api/listings/{}/recheck": (
            foreign.listings["priced"],
            {"reason": "probe recheck", "idempotency_key": "foreign-recheck1"},
        ),
    }
    for template, (foreign_id, body) in writes.items():
        a = await data_api.post(template.format(foreign_id), reviewer, body)
        missing_body = {**body, "idempotency_key": body["idempotency_key"] + "-m"}
        b = await data_api.post(template.format(uuid.uuid4()), reviewer, missing_body)
        assert a.status_code == b.status_code == 404, (template, a.text)
        assert comparable_error(a) == comparable_error(b), template
        assert str(foreign_id) not in a.text
    pause_body = {"expected_version": 1, "reason": "probe pause", "idempotency_key": "foreign-pause-01"}
    pause = await data_api.post(
        f"/api/sources/{foreign.sources['running']}/pause", data_api.users.owner, pause_body
    )
    missing_pause = await data_api.post(
        f"/api/sources/{uuid.uuid4()}/pause",
        data_api.users.owner,
        {**pause_body, "idempotency_key": "foreign-pause-02"},
    )
    assert pause.status_code == missing_pause.status_code == 404
    assert comparable_error(pause) == comparable_error(missing_pause)
    # The foreign workspace is untouched.
    assert (
        seed.scalar("select count(*) from app.owner_notes where workspace_id = %s", (foreign.workspace_id,))
        == 0
    )
    assert seed.scalar("select paused from app.sources where id = %s", (foreign.sources["running"],)) is False


async def test_invalid_inputs_name_fields_and_never_echo_values(data_api: DataHarness) -> None:
    viewer = data_api.users.viewer
    cases = {
        "/api/candidates/not-a-uuid": ["listing_id"],
        "/api/candidates/" + str(uuid.uuid4()).replace("-", ""): ["listing_id"],
        "/api/candidates?limit=0": ["limit"],
        "/api/candidates?limit=abc": ["limit"],
        "/api/candidates?country=de": ["country"],
        "/api/candidates?changed_since=1759744800": ["changed_since"],
        "/api/candidates?status=bought": ["status"],
        "/api/candidates?access_token=SYNTHETIC-SECRET-VALUE": ["access_token"],
        "/api/candidates?limit=1&limit=2": ["limit"],
        "/api/overview?<script>=1": ["<unrecognised field>"],
        "/api/outbox?state=delivered": ["state"],
    }
    for path, fields in cases.items():
        response = await data_api.get(path, viewer)
        assert response.status_code == 422, path
        error = error_of(response)
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["fields"] == fields, path
        assert "SYNTHETIC-SECRET-VALUE" not in response.text
        assert "<script>" not in response.text


async def test_cursors_are_signed_and_bound_to_filters_and_principal(data_api: DataHarness) -> None:
    viewer, reviewer = data_api.users.viewer, data_api.users.reviewer
    first = (await data_api.get("/api/candidates?limit=1", viewer)).json()
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    second = await data_api.get("/api/candidates", viewer, params={"limit": "1", "cursor": cursor})
    assert second.status_code == 200
    assert second.json()["data"]["items"][0]["listing_id"] != first["data"]["items"][0]["listing_id"]

    body, mac = cursor.split(".")
    flipped = ("B" if body[5] != "B" else "C").join([body[:5], body[6:]])
    problems = {
        "tampered": f"{flipped}.{mac}",
        "malformed": "definitely-not-a-cursor",
    }
    for problem, value in problems.items():
        response = await data_api.get("/api/candidates", viewer, params={"limit": "1", "cursor": value})
        assert response.status_code == 422, problem
        details = error_of(response)["details"]
        assert details["cursor"] in {problem, "malformed", "tampered"}, details
        assert value not in response.text
    filtered = await data_api.get(
        "/api/candidates", viewer, params={"limit": "1", "cursor": cursor, "country": "DE"}
    )
    assert error_of(filtered)["details"]["cursor"] == "mismatch"
    other_principal = await data_api.get("/api/candidates", reviewer, params={"limit": "1", "cursor": cursor})
    assert error_of(other_principal)["details"]["cursor"] == "mismatch"
    other_query = await data_api.get("/api/outbox", viewer, params={"limit": "1", "cursor": cursor})
    assert error_of(other_query)["details"]["cursor"] == "mismatch"

    outbox = (await data_api.get("/api/outbox?limit=2", viewer)).json()
    assert outbox["next_cursor"] is not None
    rest = await data_api.get("/api/outbox", viewer, params={"limit": "2", "cursor": outbox["next_cursor"]})
    assert rest.status_code == 200
    queue = (await data_api.get("/api/reviews?limit=1", viewer)).json()
    snapshot_cursor = queue["next_cursor"]
    assert isinstance(snapshot_cursor, str)
    page = await data_api.get("/api/reviews", viewer, params={"limit": "1", "cursor": snapshot_cursor})
    assert page.status_code == 200
    assert page.json()["data"]["items"][0]["case_id"] != queue["data"]["items"][0]["case_id"]
    for value in (snapshot_cursor + "x", "A" + snapshot_cursor[1:]):
        tampered = await data_api.get("/api/reviews", viewer, params={"limit": "1", "cursor": value})
        assert tampered.status_code == 422
        assert error_of(tampered)["details"]["cursor"] in {"malformed", "tampered"}
    foreign_principal = await data_api.get(
        "/api/reviews", reviewer, params={"limit": "1", "cursor": snapshot_cursor}
    )
    assert foreign_principal.status_code == 422
    assert error_of(foreign_principal)["details"]["cursor"] == "mismatch"
