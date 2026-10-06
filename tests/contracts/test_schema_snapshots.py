"""Committed schema snapshots, the export script, the dashboard route table and its documentation.

If a snapshot test fails after a model change, regenerate and commit the snapshots:

    uv run python scripts/export_schemas.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from suv_deals.api.schemas import (
    COMMON_ERRORS,
    ROUTE_INDEX,
    ROUTES,
    AddNoteRequest,
    ApiErrorResponse,
    CandidateDetailQuery,
    CandidateListQuery,
    ClaimRequest,
    ComparablesQuery,
    OutboxQuery,
    PauseSourceRequest,
    RecheckRequest,
    ReleaseRequest,
    ReviewQueueQuery,
    SubmitReviewRequest,
    api_error,
)
from suv_deals.domain.enums import Scope
from suv_deals.errors import HTTP_STATUS, ErrorCode, NotFound, ValidationFailed, VersionConflict
from suv_deals.mcp.schemas import (
    TOOL_NAMES,
    TOOLS,
    DealsListCandidatesInput,
    ReviewsSubmitInput,
    exported_schema_documents,
    render_schema_document,
)
from suv_deals.views.jsonschema import find_refs, model_schema, open_objects

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
SCRIPT = REPO / "scripts" / "export_schemas.py"
CONTRACT_DOC = REPO / "docs" / "api_contract.md"
REGENERATE = "Schema snapshot is stale: run `uv run python scripts/export_schemas.py` and commit schemas/."
UID = UUID("12345678-1234-4234-8234-123456789abc")
NOW = datetime(2026, 10, 6, 10, 5, tzinfo=UTC)

EXPECTED_FILES = {
    "listing.schema.json",
    "review.schema.json",
    "valuation.schema.json",
    "event.schema.json",
    *(f"tools/{name}.json" for name in TOOL_NAMES),
}


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    return {path: render_schema_document(doc) for path, doc in exported_schema_documents().items()}


# =========================================================================== snapshots


def test_expected_snapshot_set(generated: dict[str, str]) -> None:
    assert set(generated) == EXPECTED_FILES


@pytest.mark.parametrize("path", sorted(EXPECTED_FILES))
def test_snapshot_matches_generated(path: str, generated: dict[str, str]) -> None:
    target = SCHEMAS / path
    assert target.is_file(), f"missing schemas/{path}. {REGENERATE}"
    assert target.read_text(encoding="utf-8") == generated[path], f"schemas/{path} differs. {REGENERATE}"


def test_no_stale_tool_snapshots() -> None:
    committed = {f"tools/{p.name}" for p in (SCHEMAS / "tools").glob("*.json")}
    stale = committed - EXPECTED_FILES
    assert not stale, f"stale tool schemas {sorted(stale)}. {REGENERATE}"


def test_rendering_is_deterministic(generated: dict[str, str]) -> None:
    again = {path: render_schema_document(doc) for path, doc in exported_schema_documents().items()}
    assert again == generated
    for path, text in generated.items():
        assert text.endswith("}\n"), path
        parsed = json.loads(text)
        assert json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False) + "\n" == text, path


@pytest.mark.parametrize(
    "path", ["listing.schema.json", "review.schema.json", "valuation.schema.json", "event.schema.json"]
)
def test_document_schemas_are_self_contained(path: str, generated: dict[str, str]) -> None:
    doc = json.loads(generated[path])
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"].startswith("urn:suv-deals:schema:1.0:")
    assert not find_refs(doc) and "$defs" not in generated[path]
    assert '"number"' not in generated[path], "financial values must be strings"
    open_maps = open_objects(doc)
    if path == "listing.schema.json":
        # Field provenance is a map keyed by field path; everything else is closed.
        assert len(open_maps) == 1 and "additionalProperties" in open_maps[0]
        assert open_maps[0]["additionalProperties"]["properties"]["confidence"]["enum"] == [
            "high",
            "medium",
            "low",
        ]
    else:
        assert not open_maps


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_tool_snapshot_content(name: str, generated: dict[str, str]) -> None:
    doc = json.loads(generated[f"tools/{name}.json"])
    assert doc["name"] == name
    assert doc["requiredScope"] == TOOLS[name].scope.value
    assert doc["inputSchema"]["additionalProperties"] is False
    assert not open_objects(doc["inputSchema"]) and not find_refs(doc["inputSchema"])
    assert not open_objects(doc["outputSchema"]) and not find_refs(doc["outputSchema"])
    assert doc["errorSchema"]["title"] == "ToolError"


def test_export_script_check_mode(tmp_path: Path) -> None:
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, cwd=REPO, check=False
    )
    assert ok.returncode == 0, ok.stderr + REGENERATE
    # Writing into an empty directory produces exactly the snapshot set, and --check then passes.
    written = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert written.returncode == 0, written.stderr
    produced = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.json")}
    assert produced == EXPECTED_FILES
    # A stale tool file and a modified snapshot are both reported.
    (tmp_path / "tools" / "crawl_url.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "event.schema.json").write_text("{}\n", encoding="utf-8")
    stale = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--output", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 1
    assert "stale: tools/crawl_url.json" in stale.stderr and "out of date: event.schema.json" in stale.stderr
    assert "scripts/export_schemas.py" in stale.stderr


# =========================================================================== route table


def test_route_table_covers_the_dashboard_contract() -> None:
    required = {
        "GET /healthz",
        "GET /readyz",
        "GET /api/me",
        "GET /api/overview",
        "GET /api/candidates",
        "GET /api/candidates/{listing_id}",
        "GET /api/comparables/{set_id}",
        "GET /api/valuations/{valuation_id}",
        "GET /api/reviews",
        "POST /api/reviews/{case_id}/claim",
        "POST /api/reviews/{case_id}/release",
        "POST /api/reviews/{case_id}/submit",
        "POST /api/listings/{listing_id}/notes",
        "POST /api/listings/{listing_id}/recheck",
        "GET /api/sources",
        "POST /api/sources/{source_id}/pause",
        "GET /api/settings",
        "GET /api/outbox",
    }
    assert required <= set(ROUTE_INDEX)
    assert len(ROUTE_INDEX) == len(ROUTES)


def test_route_auth_and_scopes() -> None:
    for route in ROUTES:
        if route.path in ("/healthz", "/readyz"):
            assert route.auth == "none" and route.scope is None and route.data_model is None
            continue
        assert route.path.startswith("/api/") and route.auth == "user_jwt", route.key
        assert set(COMMON_ERRORS) <= set(route.errors), route.key
        if route.method == "POST":
            assert route.request_location == "body" and route.request_model is not None, route.key
            assert route.scope not in (None, Scope.DEALS_READ, Scope.REVIEWS_READ), route.key
            assert ErrorCode.IDEMPOTENCY_CONFLICT in route.errors, route.key
        if route.paginated:
            assert route.request_location == "query", route.key
        if "{" in route.path:
            assert ErrorCode.NOT_FOUND in route.errors, route.key
        if route.mcp_tool is not None:
            spec = TOOLS[route.mcp_tool]
            assert route.scope == spec.scope, route.key
            assert route.data_model is spec.output_model, route.key
    assert ROUTE_INDEX["GET /api/me"].scope is None


@pytest.mark.parametrize("route", [r for r in ROUTES if r.method == "POST"], ids=lambda r: r.key)
def test_mutation_bodies_reuse_tool_inputs(route: Any) -> None:
    assert route.mcp_tool is not None
    tool_schema = model_schema(TOOLS[route.mcp_tool].input_model, mode="validation", keep_object_titles=False)
    body_schema = model_schema(route.request_model, mode="validation", keep_object_titles=False)
    (path_param,) = route.path_params
    path_field = "case_id" if path_param == "case_id" else path_param
    expected_props = {k: v for k, v in tool_schema["properties"].items() if k != path_field}
    assert body_schema["properties"] == expected_props, route.key
    assert body_schema.get("required", []) == [f for f in tool_schema.get("required", []) if f != path_field]
    assert body_schema["additionalProperties"] is False


def test_bodies_convert_to_tool_inputs() -> None:
    key = "key-0001-abcd"
    token = "t" * 43
    assert ClaimRequest(expected_version=1, idempotency_key=key).to_tool_input(UID).case_id == UID
    assert ReleaseRequest(claim_token=token, idempotency_key=key).to_tool_input(UID).claim_token == token
    submit = SubmitReviewRequest.model_validate(
        {
            "claim_token": token,
            "expected_version": 2,
            "listing_revision": 3,
            "outcome": "watch",
            "reason_codes": ["PRICE_IN_BAND"],
            "summary": "Watch: price in band, documents pending.",
            "evidence_ids": [],
            "idempotency_key": key,
        }
    )
    converted = submit.to_tool_input(UID)
    assert isinstance(converted, ReviewsSubmitInput) and converted.case_id == UID
    bad = submit.model_copy(update={"reason_codes": ("SAME", "SAME")})
    with pytest.raises(ValidationFailed):  # domain rules apply on conversion, as for MCP
        bad.to_tool_input(UID)
    assert AddNoteRequest(note="note", idempotency_key=key).to_tool_input(UID).listing_id == UID
    assert RecheckRequest(reason="price changed", idempotency_key=key).to_tool_input(UID).listing_id == UID
    pause = PauseSourceRequest(expected_version=3, reason="CAPTCHA seen", idempotency_key=key)
    assert pause.to_tool_input(UID).source_id == UID
    with pytest.raises(ValueError, match="extra"):
        ClaimRequest.model_validate({"expected_version": 1, "idempotency_key": key, "case_id": str(UID)})


def test_query_models_parse_strings_then_apply_tool_rules() -> None:
    query = CandidateListQuery.model_validate(
        {"limit": "50", "country": "DE", "changed_since": "2026-10-06T10:00:00Z"}
    )
    tool_input = query.to_tool_input()
    assert isinstance(tool_input, DealsListCandidatesInput)
    assert tool_input.limit == 50 and tool_input.country == "DE"
    with pytest.raises(ValidationFailed):  # naive timestamps are refused by the tool rules
        CandidateListQuery.model_validate({"changed_since": "2026-10-06T10:00:00"}).to_tool_input()
    with pytest.raises(ValueError, match="limit"):
        CandidateListQuery.model_validate({"limit": "101"})
    with pytest.raises(ValueError, match="extra"):
        CandidateListQuery.model_validate({"workspace_id": str(UID)})
    assert CandidateDetailQuery.model_validate({"revision": "2"}).to_tool_input(UID).revision == 2
    assert CandidateDetailQuery().to_tool_input(UID).revision is None
    comparables = ComparablesQuery.model_validate({"include_excluded": "true"}).to_tool_input(UID)
    assert comparables.include_excluded is True and comparables.comparable_set_id == UID
    assert ReviewQueueQuery.model_validate({"include_needs_information": "false"}).to_tool_input().limit == 25
    assert OutboxQuery.model_validate({"state": "uncertain"}).filters() == {"state": "uncertain"}
    with pytest.raises(ValueError, match="state"):
        OutboxQuery.model_validate({"state": "delivered"})


def test_api_errors_use_http_status_map() -> None:
    status, body = api_error(VersionConflict(current_version=4), request_id="req-1", as_of=NOW)
    assert status == 409 == HTTP_STATUS[ErrorCode.VERSION_CONFLICT]
    assert isinstance(body, ApiErrorResponse)
    dumped = body.model_dump(mode="json")
    assert dumped["error"]["code"] == "VERSION_CONFLICT" and dumped["error"]["correlation_id"] == "req-1"
    assert dumped["as_of"] == "2026-10-06T10:05:00Z"
    assert api_error(NotFound(), request_id="req-2", as_of=NOW)[0] == 404
    for code in ErrorCode:
        assert code in HTTP_STATUS


# =========================================================================== documentation


def test_contract_doc_lists_every_route_scope_model_and_error() -> None:
    doc = CONTRACT_DOC.read_text(encoding="utf-8")
    for route in ROUTES:
        assert f"`{route.key}`" in doc, f"{route.key} is not documented in docs/api_contract.md"
        if route.scope is not None:
            assert f"`{route.scope.value}`" in doc
        if route.request_model is not None:
            assert f"`{route.request_model.__name__}`" in doc, route.request_model.__name__
        model = route.data_model or route.response_model
        assert f"`{model.__name__}`" in doc, model.__name__
        assert str(route.success_status) in doc
    for code, status in HTTP_STATUS.items():
        assert f"| `{code.value}` | {status} |" in doc, (
            f"{code.value} -> {status} missing from the error table"
        )
    for name, spec in TOOLS.items():
        assert f"| `{name}` | `{spec.scope.value}` |" in doc, name
        assert f"`{spec.output_model.__name__}`" in doc
    for phrase in (
        "Bearer",
        "CSRF",
        "CORS",
        "no cookies",
        "idempotency_key",
        "expected_version",
        "next_cursor",
    ):
        assert phrase in doc, phrase
