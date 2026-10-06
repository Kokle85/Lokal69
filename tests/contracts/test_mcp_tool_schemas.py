"""MCP tool contracts (spec 20, 21): exactly twelve tools, closed resolved schemas, strict inputs.

The input schemas are compared with the "Complete input schema map" JSON block of the
specification itself, so the exported schemas cannot drift from the acceptance contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.domain.reviews import SubmitRequest
from suv_deals.errors import ErrorCode, Forbidden, IdempotencyConflict, RateLimited, ValidationFailed
from suv_deals.mcp.schemas import (
    FORBIDDEN_TOOL_NAMES,
    TOOL_NAMES,
    TOOLS,
    DealsGetComparablesInput,
    DealsListCandidatesInput,
    ReviewsListPendingInput,
    ReviewsSubmitInput,
    ToolError,
    mcp_tool_definition,
    require_tool_scope,
    tool_error,
    tool_error_schema,
    tool_input_schema,
    tool_output_schema,
    tools_for_scopes,
    validate_tool_input,
)
from suv_deals.views.jsonschema import JSON_SCHEMA_DIALECT, find_refs, open_objects

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "spec" / "suv-deal-system-build-spec.md"

SPEC_TOOLS = (
    "deals_health",
    "deals_list_candidates",
    "deals_get_candidate",
    "deals_get_comparables",
    "deals_get_valuation",
    "reviews_list_pending",
    "reviews_claim",
    "reviews_release",
    "reviews_submit",
    "deals_request_recheck",
    "deals_add_note",
    "sources_pause",
)
SPEC_SCOPES = {
    "deals_health": Scope.DEALS_READ,
    "deals_list_candidates": Scope.DEALS_READ,
    "deals_get_candidate": Scope.DEALS_READ,
    "deals_get_comparables": Scope.DEALS_READ,
    "deals_get_valuation": Scope.DEALS_READ,
    "reviews_list_pending": Scope.REVIEWS_READ,
    "reviews_claim": Scope.REVIEWS_WRITE,
    "reviews_release": Scope.REVIEWS_WRITE,
    "reviews_submit": Scope.REVIEWS_WRITE,
    "deals_request_recheck": Scope.RECHECKS_REQUEST,
    "deals_add_note": Scope.NOTES_WRITE,
    "sources_pause": Scope.SOURCES_PAUSE,
}
UID = "12345678-1234-4234-8234-123456789abc"
KEY = "key-0001-abcd"
TOKEN = "t" * 43

#: Minimal valid arguments per tool.
VALID: dict[str, dict[str, Any]] = {
    "deals_health": {},
    "deals_list_candidates": {},
    "deals_get_candidate": {"listing_id": UID},
    "deals_get_comparables": {"comparable_set_id": UID},
    "deals_get_valuation": {"valuation_id": UID},
    "reviews_list_pending": {},
    "reviews_claim": {"case_id": UID, "expected_version": 1, "idempotency_key": KEY},
    "reviews_release": {"case_id": UID, "claim_token": TOKEN, "idempotency_key": KEY},
    "reviews_submit": {
        "case_id": UID,
        "claim_token": TOKEN,
        "expected_version": 2,
        "listing_revision": 3,
        "outcome": "watch",
        "reason_codes": ["PRICE_IN_BAND"],
        "summary": "Watch: price in band, documents pending.",
        "evidence_ids": [],
        "idempotency_key": KEY,
    },
    "deals_request_recheck": {"listing_id": UID, "reason": "Price may have changed", "idempotency_key": KEY},
    "deals_add_note": {"listing_id": UID, "note": "Ask for the service book.", "idempotency_key": KEY},
    "sources_pause": {
        "source_id": UID,
        "expected_version": 4,
        "reason": "CAPTCHA observed",
        "idempotency_key": KEY,
    },
}
ID_FIELDS = {
    "deals_get_candidate": "listing_id",
    "deals_get_comparables": "comparable_set_id",
    "deals_get_valuation": "valuation_id",
    "reviews_claim": "case_id",
    "reviews_release": "case_id",
    "reviews_submit": "case_id",
    "deals_request_recheck": "listing_id",
    "deals_add_note": "listing_id",
    "sources_pause": "source_id",
}
PAGINATED = ("deals_list_candidates", "deals_get_comparables", "reviews_list_pending")
WITH_KEY = tuple(name for name, args in VALID.items() if "idempotency_key" in args)


def spec_schema_map() -> dict[str, Any]:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("```json", text.index("### Complete input schema map")) + len("```json")
    end = text.index("```", start)
    loaded: dict[str, Any] = json.loads(text[start:end])
    return loaded


def resolve_spec(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].removeprefix("#/$defs/")]
            return {**resolve_spec(target, defs), **{k: v for k, v in node.items() if k != "$ref"}}
        return {k: resolve_spec(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_spec(v, defs) for v in node]
    return node


def invalid(name: str, **changes: Any) -> None:
    args = {**VALID[name], **changes}
    with pytest.raises(ValidationFailed) as info:
        validate_tool_input(name, args)
    assert info.value.code == ErrorCode.VALIDATION_ERROR


# =========================================================================== registry


def test_exactly_the_twelve_spec_tools_in_order() -> None:
    assert TOOL_NAMES == SPEC_TOOLS
    assert set(TOOLS) == set(SPEC_TOOLS)


def test_no_forbidden_tools() -> None:
    assert {
        "buy_vehicle",
        "send_seller_message",
        "create_payment",
        "approve_tax_rules",
        "execute_sql",
        "crawl_url",
    } <= (FORBIDDEN_TOOL_NAMES)
    assert not FORBIDDEN_TOOL_NAMES & set(TOOLS)
    banned_words = (
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
    for name in TOOLS:
        assert not any(word in name for word in banned_words), name


def test_scopes_match_spec_table() -> None:
    assert {name: spec.scope for name, spec in TOOLS.items()} == SPEC_SCOPES
    assert all(spec.scope != Scope.CONFIG_ADMIN for spec in TOOLS.values())


def test_annotations_describe_real_behaviour() -> None:
    for name, spec in TOOLS.items():
        hints = spec.annotations
        is_read = spec.scope in (Scope.DEALS_READ, Scope.REVIEWS_READ)
        assert hints.read_only is is_read, name
        assert hints.destructive is False, name
        assert hints.idempotent is True, name  # reads, or writes with idempotency keys
        assert hints.open_world is (name == "deals_request_recheck"), name
        assert (spec.idempotency_operation is not None) is (not is_read), name
        assert spec.paginated is (name in PAGINATED), name
        assert 40 <= len(spec.description) <= 600, name


def test_tool_discovery_hides_unauthorized_tools() -> None:
    viewer = {s.name for s in tools_for_scopes(ROLE_SCOPES[Role.VIEWER])}
    assert viewer == {n for n, s in SPEC_SCOPES.items() if s in (Scope.DEALS_READ, Scope.REVIEWS_READ)}
    reviewer = {s.name for s in tools_for_scopes(ROLE_SCOPES[Role.REVIEWER])}
    assert reviewer == set(SPEC_TOOLS) - {"sources_pause"}
    assert {s.name for s in tools_for_scopes(ROLE_SCOPES[Role.OWNER])} == set(SPEC_TOOLS)
    assert tools_for_scopes([]) == ()


def test_require_tool_scope() -> None:
    viewer = ActorContext(
        workspace_id=uuid4(),
        principal_id=uuid4(),
        principal_kind="mcp_client",
        role=Role.VIEWER,
        scopes=ROLE_SCOPES[Role.VIEWER],
        request_id="r1",
    )
    assert require_tool_scope("deals_health", viewer).name == "deals_health"
    with pytest.raises(Forbidden):
        require_tool_scope("reviews_claim", viewer)
    with pytest.raises(ValidationFailed):
        require_tool_scope("execute_sql", viewer)


# =========================================================================== schemas


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_input_schema_resolved_and_closed(name: str) -> None:
    schema = tool_input_schema(name)
    text = json.dumps(schema)
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "$ref" not in text and "$defs" not in text
    assert not find_refs(schema) and not open_objects(schema)
    # A fresh copy every call: callers cannot corrupt the cached schema.
    schema["properties"]["injected"] = {}
    assert "injected" not in tool_input_schema(name)["properties"]


def test_input_schemas_match_the_spec_schema_map() -> None:
    bundle = spec_schema_map()
    assert bundle["$schema"] == JSON_SCHEMA_DIALECT
    spec_tools = resolve_spec(bundle["tools"], bundle["$defs"])
    assert list(spec_tools) == list(SPEC_TOOLS)
    for name, expected in spec_tools.items():
        ours = tool_input_schema(name)
        assert ours["additionalProperties"] is False and expected["additionalProperties"] is False
        assert set(ours["properties"]) == set(expected["properties"]), name
        assert list(ours.get("required", [])) == list(expected.get("required", [])), name
        for prop, constraints in expected["properties"].items():
            actual = ours["properties"][prop]
            for keyword, value in constraints.items():
                assert keyword in actual, f"{name}.{prop}: missing {keyword}"
                if keyword == "items":
                    for item_key, item_value in value.items():
                        assert actual["items"][item_key] == item_value, f"{name}.{prop}.items.{item_key}"
                else:
                    assert actual[keyword] == value, f"{name}.{prop}.{keyword}"
            # Narrowing only: our schema never accepts a type the spec forbids.
            if "type" in actual and "type" in constraints:
                assert actual["type"] == constraints["type"], f"{name}.{prop}"


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_output_schema_is_the_envelope(name: str) -> None:
    schema = tool_output_schema(name)
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert set(schema["required"]) == {
        "schema_version",
        "request_id",
        "as_of",
        "data",
        "warnings",
        "next_cursor",
    }
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert not find_refs(schema)
    assert not open_objects(schema)
    assert '"number"' not in json.dumps(schema), "financial values must be strings, never JSON numbers"
    data = schema["properties"]["data"]
    assert data["type"] == "object" and data["additionalProperties"] is False


def test_tool_error_schema_and_payload() -> None:
    schema = tool_error_schema()
    assert schema["properties"]["code"]["enum"] == [code.value for code in ErrorCode]
    assert set(schema["required"]) >= {
        "code",
        "message",
        "retryable",
        "retry_after_seconds",
        "correlation_id",
    }
    assert schema["additionalProperties"] is False
    payload = tool_error(RateLimited(retry_after_seconds=30), correlation_id="req-9")
    assert isinstance(payload, ToolError)
    assert payload.model_dump(mode="json") == {
        "code": "RATE_LIMITED",
        "message": "Rate limited",
        "retryable": True,
        "retry_after_seconds": 30,
        "correlation_id": "req-9",
        "details": None,
    }
    assert tool_error(IdempotencyConflict()).retryable is False


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_mcp_tool_definition_shape(name: str) -> None:
    tool = mcp_tool_definition(name)
    assert set(tool) == {"name", "title", "description", "inputSchema", "outputSchema", "annotations"}
    assert tool["name"] == name
    assert set(tool["annotations"]) == {
        "title",
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    }
    assert 1 <= len(name) <= 128 and name.replace("_", "").isalnum()


# =========================================================================== validation


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_minimal_valid_arguments_pass(name: str) -> None:
    model = validate_tool_input(name, VALID[name])
    assert type(model) is TOOLS[name].input_model
    if name == "deals_health":
        assert validate_tool_input(name, None) == model


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_unknown_fields_are_rejected(name: str) -> None:
    invalid(name, unexpected_field=1)
    invalid(name, workspace_id=UID)  # client-supplied tenancy is never accepted here
    invalid(name, actor="owner")  # caller text cannot impersonate an actor


def test_non_object_arguments_rejected() -> None:
    with pytest.raises(ValidationFailed):
        validate_tool_input("deals_health", ["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        validate_tool_input("buy_vehicle", {})


@pytest.mark.parametrize("name", list(ID_FIELDS))
@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        "12345678123456781234567812345678",
        "{12345678-1234-4234-8234-123456789abc}",
        "urn:uuid:12345678-1234-4234-8234-123456789abc",
        "12345678-1234-4234-8234-123456789abcd",
        "",
        12345,
        None,
    ],
)
def test_bad_uuids_rejected(name: str, bad: Any) -> None:
    invalid(name, **{ID_FIELDS[name]: bad})


def test_evidence_and_valuation_ids_must_be_uuids() -> None:
    invalid("reviews_submit", evidence_ids=["nope"])
    invalid("reviews_submit", valuation_id="nope")
    assert validate_tool_input("reviews_submit", {**VALID["reviews_submit"], "valuation_id": None})
    assert validate_tool_input("reviews_submit", {**VALID["reviews_submit"], "valuation_id": UID})


@pytest.mark.parametrize("name", PAGINATED)
@pytest.mark.parametrize("bad", [0, -1, 101, 1000, True, "25", 25.0, None])
def test_out_of_range_limits_rejected(name: str, bad: Any) -> None:
    invalid(name, limit=bad)


@pytest.mark.parametrize("name", PAGINATED)
def test_limit_bounds_and_default(name: str) -> None:
    assert validate_tool_input(name, VALID[name]).limit == 25  # type: ignore[attr-defined]
    for good in (1, 100):
        assert validate_tool_input(name, {**VALID[name], "limit": good}).limit == good  # type: ignore[attr-defined]
    invalid(name, cursor="c" * 2049)
    assert validate_tool_input(name, {**VALID[name], "cursor": None}).cursor is None  # type: ignore[attr-defined]
    assert validate_tool_input(name, {**VALID[name], "cursor": "c" * 2048})


@pytest.mark.parametrize(
    ("name", "field", "bad"),
    [
        ("reviews_claim", "expected_version", 0),
        ("reviews_claim", "expected_version", -1),
        ("reviews_claim", "expected_version", True),
        ("reviews_claim", "expected_version", "1"),
        ("reviews_claim", "expected_version", 1.0),
        ("sources_pause", "expected_version", 0),
        ("reviews_submit", "listing_revision", 0),
        ("deals_get_candidate", "revision", 0),
        ("deals_get_candidate", "revision", None),
    ],
)
def test_versions_must_be_positive_integers(name: str, field: str, bad: Any) -> None:
    invalid(name, **{field: bad})


@pytest.mark.parametrize("name", WITH_KEY)
@pytest.mark.parametrize("bad", ["k" * 7, "k" * 129, "has spaces 123", "ключ-12345678", 12345678, None])
def test_idempotency_keys_bounded(name: str, bad: Any) -> None:
    invalid(name, idempotency_key=bad)


@pytest.mark.parametrize(
    ("name", "field", "bad"),
    [
        ("reviews_release", "claim_token", "t" * 19),
        ("reviews_release", "claim_token", "t" * 257),
        ("reviews_release", "claim_token", "t" * 30 + "!"),
        ("reviews_submit", "claim_token", "t" * 257),
        ("reviews_submit", "summary", "too short"),
        ("reviews_submit", "summary", "s" * 4001),
        ("reviews_submit", "summary", "         x         "),
        ("reviews_submit", "summary", "<thinking>secret plan</thinking> ok"),
        ("reviews_submit", "summary", "Line with a \u202e bidi override"),
        ("reviews_submit", "model_run_id", "m" * 201),
        ("reviews_submit", "missing_information", ["m" * 301]),
        ("reviews_submit", "missing_information", ["ok"] * 31),
        ("reviews_submit", "reason_codes", []),
        ("reviews_submit", "reason_codes", [f"R{i}" for i in range(21)]),
        ("reviews_submit", "reason_codes", ["R" * 81]),
        ("reviews_submit", "reason_codes", ["bad code"]),
        ("reviews_submit", "reason_codes", ["SAME", "SAME"]),
        ("reviews_submit", "evidence_ids", [UID] * 2),
        ("reviews_submit", "evidence_ids", [str(uuid4()) for _ in range(101)]),
        ("reviews_submit", "outcome", "buy"),
        ("reviews_submit", "outcome", "pending"),
        ("deals_request_recheck", "reason", "ab"),
        ("deals_request_recheck", "reason", "r" * 2001),
        ("deals_request_recheck", "reason", "   "),
        ("deals_request_recheck", "reason", "null byte \x00 here"),
        ("deals_add_note", "note", ""),
        ("deals_add_note", "note", "n" * 4001),
        ("deals_add_note", "note", "   "),
        ("sources_pause", "reason", "r" * 2001),
        ("deals_list_candidates", "country", "de"),
        ("deals_list_candidates", "country", "DEU"),
        ("deals_list_candidates", "country", None),
        ("deals_list_candidates", "profile", "eur_4000"),
        ("deals_list_candidates", "profile", None),
        ("deals_list_candidates", "status", "claimed"),
        ("deals_list_candidates", "status", "superseded"),
        ("deals_list_candidates", "changed_since", "2026-10-06T10:00:00"),
        ("deals_list_candidates", "changed_since", 1_759_744_800),
        ("deals_list_candidates", "changed_since", "yesterday"),
        ("deals_get_comparables", "include_excluded", "true"),
        ("deals_get_comparables", "include_excluded", 1),
        ("reviews_list_pending", "include_needs_information", None),
    ],
)
def test_bounded_strings_lists_and_enums(name: str, field: str, bad: Any) -> None:
    invalid(name, **{field: bad})


def test_upper_bounds_are_inclusive() -> None:
    submit = {
        **VALID["reviews_submit"],
        "claim_token": "t" * 256,
        "summary": "s" * 4000,
        "reason_codes": [f"R{i}" for i in range(20)],
        "evidence_ids": [str(uuid4()) for _ in range(100)],
        "missing_information": ["m" * 300] * 30,
        "model_run_id": "m" * 200,
        "idempotency_key": "k" * 128,
    }
    assert validate_tool_input("reviews_submit", submit)
    assert validate_tool_input("deals_add_note", {**VALID["deals_add_note"], "note": "n" * 4000})
    assert validate_tool_input(
        "deals_request_recheck", {**VALID["deals_request_recheck"], "reason": "r" * 2000}
    )


def test_filters_and_defaults() -> None:
    model = validate_tool_input(
        "deals_list_candidates",
        {
            "profile": "manual_4000",
            "country": "IT",
            "status": "watch",
            "changed_since": "2026-10-06T12:00:00+02:00",
        },
    )
    assert isinstance(model, DealsListCandidatesInput)
    assert model.filters() == {
        "profile": "manual_4000",
        "country": "IT",
        "status": "watch",
        "changed_since": "2026-10-06T10:00:00Z",
    }
    comparables = validate_tool_input("deals_get_comparables", VALID["deals_get_comparables"])
    assert isinstance(comparables, DealsGetComparablesInput) and comparables.include_excluded is False
    queue = validate_tool_input("reviews_list_pending", {})
    assert isinstance(queue, ReviewsListPendingInput) and queue.include_needs_information is True


def test_validation_errors_never_echo_values() -> None:
    secret = "S3cretTokenValue_" + "x" * 20 + "!"
    with pytest.raises(ValidationFailed) as info:
        validate_tool_input(
            "reviews_release", {"case_id": UID, "claim_token": secret, "idempotency_key": KEY}
        )
    rendered = json.dumps(info.value.to_payload())
    assert secret not in rendered and "claim_token" in rendered
    with pytest.raises(ValidationFailed) as info:
        validate_tool_input("deals_health", {"<script>alert(1)</script>": 1})
    assert "<script>" not in json.dumps(info.value.to_payload())


def test_submit_input_maps_to_the_domain_request() -> None:
    model = validate_tool_input(
        "reviews_submit", {**VALID["reviews_submit"], "summary": "  Watch: price in band, docs pending.  "}
    )
    assert isinstance(model, ReviewsSubmitInput)
    request = model.to_submit_request()
    assert isinstance(request, SubmitRequest)
    assert request.summary == "Watch: price in band, docs pending."
    assert request.claim_token == TOKEN
    assert TOKEN not in repr(model)  # the claim token is excluded from reprs and logs
    # reviews_submit requires evidence_ids even though the list may be empty (spec 21 map).
    without = {k: v for k, v in VALID["reviews_submit"].items() if k != "evidence_ids"}
    with pytest.raises(ValidationFailed):
        validate_tool_input("reviews_submit", without)


@pytest.mark.parametrize("name", SPEC_TOOLS)
def test_required_fields_are_required(name: str) -> None:
    for field in tool_input_schema(name).get("required", []):
        args = {k: v for k, v in VALID[name].items() if k != field}
        with pytest.raises(ValidationFailed):
            validate_tool_input(name, args)


def test_pydantic_errors_hide_submitted_values() -> None:
    secret = "S3cretTokenValue_" + "x" * 20 + "!"
    with pytest.raises(ValueError) as info:
        TOOLS["reviews_release"].input_model.model_validate(
            {"case_id": UID, "claim_token": secret, "idempotency_key": KEY}
        )
    assert secret not in str(info.value)


# =========================================================================== review regressions


@pytest.mark.parametrize(
    "value",
    [
        "1759744800",  # epoch seconds in a string: pydantic alone reads this as 2025-10-06
        "1759744800.5",
        "2026-10-06 10:00:00Z",  # space separator
        "2026-10-06T10:00Z",  # no seconds
        "2026-10-06T10:00:00+0200",  # offset without a colon
        "2026-10-06",
        " 2026-10-06T10:00:00Z",
    ],
)
def test_changed_since_is_strict_rfc3339(value: str) -> None:
    invalid("deals_list_candidates", changed_since=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-06T10:00:00Z", "2026-10-06T10:00:00Z"),
        ("2026-10-06t12:00:00+02:00", "2026-10-06T10:00:00Z"),
        ("2026-10-06T10:00:00.250-00:00", "2026-10-06T10:00:00.250000Z"),
    ],
)
def test_changed_since_accepts_rfc3339(value: str, expected: str) -> None:
    model = validate_tool_input("deals_list_candidates", {"changed_since": value})
    assert isinstance(model, DealsListCandidatesInput)
    assert model.filters()["changed_since"] == expected


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"reason_codes": ["SAME", "SAME"]}, "reason_codes"),
        ({"evidence_ids": [UID, UID]}, "evidence_ids"),
        ({"summary": "<thinking>hidden</thinking> watch"}, "summary"),
        ({"model_run_id": "run\x07id"}, "model_run_id"),
    ],
)
def test_domain_rule_failures_name_the_field(changes: dict[str, Any], field: str) -> None:
    # Regression: domain-rule failures used to report only "arguments", hiding the bad field.
    args = {**VALID["reviews_submit"], **changes}
    with pytest.raises(ValidationFailed) as info:
        validate_tool_input("reviews_submit", args)
    assert info.value.details["fields"] == [field]
    rendered = json.dumps(info.value.to_payload())
    assert "hidden" not in rendered and TOKEN not in rendered  # names only, never values


# =========================================================================== schema vs validator

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def _rfc3339_date_time(value: object) -> bool:
    return not isinstance(value, str) or _RFC3339.fullmatch(value) is not None


#: Arguments the published schema must reject (expressible in JSON Schema) - and so must we.
STRUCTURALLY_INVALID: list[tuple[str, dict[str, Any]]] = [
    *[(name, {**args, "unexpected_field": 1}) for name, args in VALID.items()],
    *[(name, {**VALID[name], "limit": bad}) for name in PAGINATED for bad in (0, 101, "25", True, None)],
    *[(name, {**VALID[name], "cursor": "c" * 2049}) for name in PAGINATED],
    *[(name, {**VALID[name], field: "not-a-uuid"}) for name, field in ID_FIELDS.items()],
    *[(name, {**VALID[name], "idempotency_key": "short"}) for name in WITH_KEY],
    ("reviews_submit", {**VALID["reviews_submit"], "reason_codes": []}),
    ("reviews_submit", {**VALID["reviews_submit"], "summary": "too short"}),
    ("reviews_submit", {**VALID["reviews_submit"], "outcome": "buy"}),
    ("reviews_submit", {**VALID["reviews_submit"], "evidence_ids": [UID, UID]}),
    ("reviews_release", {**VALID["reviews_release"], "claim_token": "t" * 19}),
    ("deals_list_candidates", {"country": "de"}),
    ("deals_list_candidates", {"status": "claimed"}),
    ("deals_list_candidates", {"changed_since": "1759744800"}),
    ("deals_get_candidate", {**VALID["deals_get_candidate"], "revision": 0}),
    ("sources_pause", {**VALID["sources_pause"], "reason": "ab"}),
]

#: Arguments both must accept.
VALID_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    *VALID.items(),
    ("deals_list_candidates", {"profile": "manual_4000", "country": "IT", "status": "watch", "limit": 100}),
    ("deals_list_candidates", {"changed_since": "2026-10-06T10:00:00+02:00", "cursor": None}),
    ("deals_get_candidate", {**VALID["deals_get_candidate"], "revision": 3}),
    ("deals_get_comparables", {**VALID["deals_get_comparables"], "include_excluded": True, "limit": 1}),
    ("reviews_list_pending", {"include_needs_information": False, "cursor": "opaque"}),
    ("reviews_submit", {**VALID["reviews_submit"], "valuation_id": None, "model_run_id": None}),
    ("reviews_submit", {**VALID["reviews_submit"], "valuation_id": UID, "missing_information": ["VIN"]}),
]


def _schema_errors(name: str, args: dict[str, Any]) -> list[str]:
    jsonschema = pytest.importorskip("jsonschema")
    checker = jsonschema.FormatChecker()
    checker.checks("date-time")(_rfc3339_date_time)
    validator = jsonschema.Draft202012Validator(tool_input_schema(name), format_checker=checker)
    return [error.message for error in validator.iter_errors(args)]


@pytest.mark.parametrize(("name", "args"), VALID_VARIANTS)
def test_schema_and_validator_accept_the_same_valid_arguments(name: str, args: dict[str, Any]) -> None:
    assert not _schema_errors(name, args), _schema_errors(name, args)
    validate_tool_input(name, args)


@pytest.mark.parametrize(("name", "args"), STRUCTURALLY_INVALID)
def test_schema_and_validator_reject_the_same_structural_errors(name: str, args: dict[str, Any]) -> None:
    assert _schema_errors(name, args), f"published schema accepts invalid {name} arguments"
    with pytest.raises(ValidationFailed):
        validate_tool_input(name, args)
