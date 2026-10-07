"""MCP tools end to end over Streamable HTTP (spec 21, 31 "MCP tools"; marker ``db`` for calls).

Covers scope-filtered discovery with the exact published schemas, every tool's positive path
(validated against the ``outputSchema`` the server itself publishes), validation / scope /
not-found / conflict negatives, the claim -> submit flow with idempotent replay and
``IDEMPOTENCY_CONFLICT``, frozen review-queue pagination under reprioritisation, rate limiting,
the official SDK client round trip and the extension hook. SYNTHETIC data only.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from jsonschema import Draft202012Validator
from mcp.client.streamable_http import streamable_http_client

from mcp import Client
from suv_deals.api.middleware import RateLimit
from suv_deals.domain.enums import Role, Scope
from suv_deals.mcp.schemas import (
    FORBIDDEN_TOOL_NAMES,
    TOOL_NAMES,
    TOOLS,
    DealsHealthInput,
    ToolSpec,
    tool_input_schema,
    tool_output_schema,
)
from suv_deals.mcp.server import McpOptions, build_mcp
from suv_deals.mcp.tools import ToolCall, ToolRegistry
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.views.common import ResponseEnvelope
from suv_deals.views.operations import HealthView
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import seed_foreign_workspace
from tests.mcp.conftest import (
    BASE_URL,
    MCP_URL,
    DataHarness,
    SigningKeys,
    TokenFactory,
    add_members,
    build_app,
    make_settings,
    mcp_client,
    offline_db,
    role_scopes,
    running,
)

pytestmark = pytest.mark.db

READ_TOOLS = [
    "deals_health",
    "deals_list_candidates",
    "deals_get_candidate",
    "deals_get_comparables",
    "deals_get_valuation",
]
SECRET_TOKEN = "Zz" * 20  # a well-formed claim token value that must never be echoed


def validator_for(tool_definition: dict[str, Any]) -> Draft202012Validator:
    schema = tool_definition["outputSchema"]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class SchemaCheck:
    """Validates structured results against the outputSchema published by ``tools/list``."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self.validators = {t["name"]: validator_for(t) for t in tools}

    def __call__(self, tool: str, structured: dict[str, Any]) -> dict[str, Any]:
        errors = sorted(self.validators[tool].iter_errors(structured), key=lambda e: list(e.absolute_path))
        assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
        return structured


async def schema_check(h: DataHarness) -> SchemaCheck:
    return SchemaCheck(await h.client.tools(h.owner))


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------


async def test_tools_list_is_scope_filtered_deterministic_and_exact(data_mcp: DataHarness) -> None:
    client = data_mcp.client
    owner_tools = await client.tools(data_mcp.owner)
    assert [t["name"] for t in owner_tools] == list(TOOL_NAMES)
    assert [t["name"] for t in await client.tools(data_mcp.owner)] == list(TOOL_NAMES)  # stable order
    for tool in owner_tools:
        name = tool["name"]
        spec = TOOLS[name]
        assert tool["inputSchema"] == tool_input_schema(name)
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"] == tool_output_schema(name)
        assert tool["title"] == spec.annotations.title and tool["description"] == spec.description
        assert tool["annotations"] == {
            "title": spec.annotations.title,
            "readOnlyHint": spec.annotations.read_only,
            "destructiveHint": spec.annotations.destructive,
            "idempotentHint": spec.annotations.idempotent,
            "openWorldHint": spec.annotations.open_world,
        }
    assert not FORBIDDEN_TOOL_NAMES & {t["name"] for t in owner_tools}
    reviewer = [t["name"] for t in await client.tools(data_mcp.reviewer)]
    assert reviewer == [n for n in TOOL_NAMES if n != "sources_pause"]
    viewer = [t["name"] for t in await client.tools(data_mcp.viewer)]
    assert viewer == [*READ_TOOLS, "reviews_list_pending"]
    listing = await client.result("tools/list", token=data_mcp.viewer)
    assert listing["ttlMs"] == 0 and listing["cacheScope"] == "private"


async def test_unknown_or_forbidden_tool_is_a_protocol_error(data_mcp: DataHarness) -> None:
    client = data_mcp.client
    for name in [*sorted(FORBIDDEN_TOOL_NAMES), "nope"]:
        response = await client.rpc(
            "tools/call", {"name": name, "arguments": {}}, token=data_mcp.owner, name=name
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == -32602 and error["message"] == f"Unknown tool: {name}"
    odd = "x" * 129
    response = await client.rpc("tools/call", {"name": odd, "arguments": {}}, token=data_mcp.owner, name=odd)
    assert response.json()["error"]["message"] == "Unknown tool"


async def test_official_sdk_client_round_trip_validates_output_schemas(data_mcp: DataHarness) -> None:
    headers = {"Authorization": f"Bearer {data_mcp.viewer}"}
    transport = httpx2.ASGITransport(app=data_mcp.client.app)
    async with (
        httpx2.AsyncClient(transport=transport, base_url=BASE_URL, headers=headers) as http,
        Client(streamable_http_client(MCP_URL, http_client=http)) as sdk,
    ):
        listed = await sdk.list_tools()
        assert [t.name for t in listed.tools] == [*READ_TOOLS, "reviews_list_pending"]
        # The SDK client validates structuredContent against each tool's outputSchema.
        health = await sdk.call_tool("deals_health", {})
        assert health.is_error is False and health.structured_content is not None
        candidate = await sdk.call_tool(
            "deals_get_candidate", {"listing_id": str(data_mcp.data.listings["priced"])}
        )
        assert candidate.is_error is False
        queue = await sdk.call_tool("reviews_list_pending", {"limit": 1})
        assert queue.is_error is False
        denied = await sdk.call_tool("reviews_claim", {})
        assert denied.is_error is True


# --------------------------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------------------------


async def test_read_tools_positive_paths_match_their_output_schemas(data_mcp: DataHarness) -> None:
    client, data, viewer = data_mcp.client, data_mcp.data, data_mcp.viewer
    check = await schema_check(data_mcp)

    health = check("deals_health", await client.ok("deals_health", token=viewer))
    assert health["data"]["build"]["build_id"] == "synthetic-mcp.1"
    assert health["data"]["source_network_enabled"] is False
    assert {s["source_key"] for s in health["data"]["sources"]} >= set(data.source_keys.values()) - {
        data.source_keys["mk"]
    }

    first = check(
        "deals_list_candidates", await client.ok("deals_list_candidates", {"limit": 2}, token=viewer)
    )
    assert len(first["data"]["items"]) == 2 and first["next_cursor"]
    rest = check(
        "deals_list_candidates",
        await client.ok(
            "deals_list_candidates", {"limit": 100, "cursor": first["next_cursor"]}, token=viewer
        ),
    )
    seen = [i["listing_id"] for i in first["data"]["items"] + rest["data"]["items"]]
    assert len(seen) == len(set(seen)) and set(seen) == {str(x) for x in data.candidate_ids}
    filtered = await client.ok("deals_list_candidates", {"country": "IT"}, token=viewer)
    assert {i["listing_id"] for i in filtered["data"]["items"]} == {
        str(data.listings["needs_facts"]),
        str(data.listings["paused"]),
    }

    detail = check(
        "deals_get_candidate",
        await client.ok("deals_get_candidate", {"listing_id": str(data.listings["priced"])}, token=viewer),
    )
    assert detail["data"]["summary"]["listing_id"] == str(data.listings["priced"])
    older = await client.ok(
        "deals_get_candidate", {"listing_id": str(data.listings["priced"]), "revision": 1}, token=viewer
    )
    assert "REVISION_NOT_CURRENT" in {w["code"] for w in older["warnings"]}

    comparables = check(
        "deals_get_comparables",
        await client.ok(
            "deals_get_comparables",
            {"comparable_set_id": str(data.comparable_set_id), "include_excluded": True},
            token=viewer,
        ),
    )
    assert comparables["data"]["comparable_set_id"] == str(data.comparable_set_id)

    valuation = check(
        "deals_get_valuation",
        await client.ok(
            "deals_get_valuation", {"valuation_id": str(data.valuations["estimated"])}, token=viewer
        ),
    )
    assert valuation["data"]["valuation_id"] == str(data.valuations["estimated"])

    queue = check("reviews_list_pending", await client.ok("reviews_list_pending", token=viewer))
    assert {i["case_id"] for i in queue["data"]["items"]} == {
        str(data.cases["priced"]),
        str(data.cases["incomplete"]),
    }
    for envelope in (health, first, detail, comparables, valuation, queue):
        assert set(envelope) == {"schema_version", "request_id", "as_of", "data", "warnings", "next_cursor"}
        assert envelope["request_id"].startswith("req-")


async def test_missing_and_foreign_objects_are_the_same_not_found(
    data_mcp: DataHarness, db: Database, seed: Seed
) -> None:
    foreign = await seed_foreign_workspace(db, seed, "MCP")
    client, viewer = data_mcp.client, data_mcp.viewer
    cases = {
        "deals_get_candidate": ("listing_id", foreign.listings["priced"]),
        "deals_get_comparables": ("comparable_set_id", foreign.comparable_set_id),
        "deals_get_valuation": ("valuation_id", foreign.valuations["not_started"]),
    }
    for tool, (field, foreign_id) in cases.items():
        missing = await client.fails(tool, {field: str(uuid.uuid4())}, token=viewer, code="NOT_FOUND")
        other = await client.fails(tool, {field: str(foreign_id)}, token=viewer, code="NOT_FOUND")
        assert {k: v for k, v in missing.items() if k != "correlation_id"} == {
            k: v for k, v in other.items() if k != "correlation_id"
        }
        assert str(foreign_id) not in str(other)


async def test_cursor_tampering_and_filter_mismatch_are_validation_errors(data_mcp: DataHarness) -> None:
    client, viewer = data_mcp.client, data_mcp.viewer
    first = await client.ok("deals_list_candidates", {"limit": 1}, token=viewer)
    cursor = first["next_cursor"]
    mismatch = await client.fails(
        "deals_list_candidates",
        {"limit": 1, "cursor": cursor, "country": "DE"},
        token=viewer,
        code="VALIDATION_ERROR",
    )
    assert mismatch["details"]["cursor"] == "mismatch"
    tampered = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]
    altered = await client.fails(
        "deals_list_candidates", {"limit": 1, "cursor": tampered}, token=viewer, code="VALIDATION_ERROR"
    )
    assert altered["details"]["cursor"] in ("tampered", "malformed")
    # A cursor is bound to its principal: another member cannot replay it.
    other = await client.fails(
        "deals_list_candidates",
        {"limit": 1, "cursor": cursor},
        token=data_mcp.reviewer,
        code="VALIDATION_ERROR",
    )
    assert other["details"]["cursor"] == "mismatch"


# --------------------------------------------------------------------------------------------
# Validation and scope negatives
# --------------------------------------------------------------------------------------------


async def test_validation_errors_name_fields_and_never_echo_values(data_mcp: DataHarness) -> None:
    client, reviewer = data_mcp.client, data_mcp.reviewer
    case_id = str(data_mcp.data.cases["priced"])
    bad: list[tuple[str, dict[str, Any], list[str]]] = [
        ("deals_health", {"unexpected": True}, ["unexpected"]),
        ("deals_list_candidates", {"limit": 0}, ["limit"]),
        ("deals_list_candidates", {"limit": "5"}, ["limit"]),
        ("deals_list_candidates", {"country": None}, ["country"]),
        ("deals_list_candidates", {"changed_since": "1759744800"}, ["changed_since"]),
        ("deals_get_candidate", {}, ["listing_id"]),
        ("deals_get_candidate", {"listing_id": "not-a-uuid"}, ["listing_id"]),
        ("reviews_claim", {"case_id": case_id, "expected_version": 1}, ["idempotency_key"]),
        (
            "reviews_release",
            {"case_id": case_id, "claim_token": SECRET_TOKEN, "idempotency_key": "x", "workspace_id": "w"},
            ["idempotency_key", "workspace_id"],
        ),
        (
            "deals_request_recheck",
            {
                "listing_id": str(data_mcp.data.listings["priced"]),
                "reason": "SYNTHETIC",
                "idempotency_key": "recheck-url-0001",
                "url": "http://169.254.169.254/",
            },
            ["url"],
        ),
        (
            "deals_add_note",
            {
                "listing_id": str(data_mcp.data.listings["priced"]),
                "note": "bidi " + chr(0x202E),
                "idempotency_key": "note-bidi-0001",
            },
            ["note"],
        ),
    ]
    for tool, arguments, fields in bad:
        payload = await client.fails(
            tool,
            arguments,
            token=data_mcp.owner if tool == "sources_pause" else reviewer,
            code="VALIDATION_ERROR",
        )
        assert payload["details"]["fields"] == fields, (tool, payload)
        assert SECRET_TOKEN not in str(payload)
    not_object = await client.call("deals_health", None, token=reviewer)
    assert not_object["isError"] is False


async def test_scope_is_checked_before_arguments_and_per_tool(data_mcp: DataHarness) -> None:
    client = data_mcp.client
    denied = await client.fails("reviews_claim", {"garbage": 1}, token=data_mcp.viewer, code="FORBIDDEN")
    assert "fields" not in (denied.get("details") or {})
    await client.fails(
        "sources_pause",
        {
            "source_id": str(data_mcp.data.sources["running"]),
            "expected_version": 1,
            "reason": "SYNTHETIC",
            "idempotency_key": "pause-denied-01",
        },
        token=data_mcp.reviewer,
        code="FORBIDDEN",
    )
    notes_only = data_mcp.tokens.mint(data_mcp.users.reviewer, scopes=[Scope.NOTES_WRITE])
    await client.fails("deals_health", token=notes_only, code="FORBIDDEN")
    denials = client.metrics.registry.get_sample_value(
        "suv_deals_authorization_denials_total", {"surface": "mcp", "reason": "insufficient_scope"}
    )
    assert denials == 3


# --------------------------------------------------------------------------------------------
# Review flow
# --------------------------------------------------------------------------------------------


def submit_args(data_mcp: DataHarness, token: str, key: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "case_id": str(data_mcp.data.cases["priced"]),
        "claim_token": token,
        "expected_version": 2,
        "listing_revision": 2,
        "valuation_id": str(data_mcp.data.valuations["estimated"]),
        "outcome": "watch",
        "reason_codes": ["SYNTHETIC_PRICE_WATCH"],
        "summary": "SYNTHETIC: watch the price; comparables support the band but costs are unknown.",
        "evidence_ids": [],
        "idempotency_key": key,
    }
    args.update(overrides)
    return args


async def test_claim_submit_flow_with_idempotent_replay_and_conflicts(data_mcp: DataHarness) -> None:
    client, data = data_mcp.client, data_mcp.data
    reviewer, other = data_mcp.reviewer, data_mcp.second_reviewer
    check = await schema_check(data_mcp)
    case_id = str(data.cases["priced"])

    queue = await client.ok("reviews_list_pending", token=reviewer)
    item = next(i for i in queue["data"]["items"] if i["case_id"] == case_id)
    assert item["case_version"] == 1

    stale = await client.fails(
        "reviews_claim",
        {"case_id": case_id, "expected_version": 5, "idempotency_key": "claim-stale-001"},
        token=reviewer,
        code="VERSION_CONFLICT",
    )
    assert stale["retryable"] is False

    claim_args = {"case_id": case_id, "expected_version": 1, "idempotency_key": "claim-flow-0001"}
    granted = check("reviews_claim", await client.ok("reviews_claim", claim_args, token=reviewer))
    token = granted["data"]["claim_token"]
    assert isinstance(token, str) and granted["data"]["claim_token_redacted"] is False
    assert granted["data"]["case_version"] == 2

    replay = check("reviews_claim", await client.ok("reviews_claim", claim_args, token=reviewer))
    assert replay["data"]["claim_token"] is None and replay["data"]["claim_token_redacted"] is True
    await client.fails(
        "reviews_claim", {**claim_args, "expected_version": 2}, token=reviewer, code="IDEMPOTENCY_CONFLICT"
    )
    taken = await client.fails(
        "reviews_claim",
        {"case_id": case_id, "expected_version": 2, "idempotency_key": "claim-other-001"},
        token=other,
        code="ALREADY_CLAIMED",
    )
    assert str(data_mcp.users.reviewer) not in str(taken)

    await client.fails(
        "reviews_submit", submit_args(data_mcp, token, "submit-other-01"), token=other, code="ALREADY_CLAIMED"
    )
    conflict = await client.fails(
        "reviews_submit",
        submit_args(data_mcp, token, "submit-stale-01", expected_version=1),
        token=reviewer,
        code="VERSION_CONFLICT",
    )
    assert conflict["details"]["current_version"] == 2
    await client.fails(
        "reviews_submit",
        submit_args(data_mcp, token, "submit-stale-02", listing_revision=1),
        token=reviewer,
        code="VERSION_CONFLICT",
    )
    expired = await client.fails(
        "reviews_submit",
        submit_args(data_mcp, SECRET_TOKEN, "submit-badtok-1"),
        token=reviewer,
        code="CLAIM_EXPIRED",
    )
    assert SECRET_TOKEN not in str(expired)

    body = submit_args(data_mcp, token, "submit-flow-001")
    decided = check("reviews_submit", await client.ok("reviews_submit", body, token=reviewer))
    decision = decided["data"]
    assert decision["outcome"] == "watch" and decision["case_state"] == "watch"
    assert decision["actor"] == {
        "principal_id": str(data_mcp.users.reviewer),
        "principal_kind": "user",
        "role": "reviewer",
    }
    assert decision["tool_request_id"] == decided["request_id"]
    assert token not in str(decided)

    again = await client.ok("reviews_submit", body, token=reviewer)
    assert again["data"] == decision  # the original decision, not a second one
    await client.fails(
        "reviews_submit",
        {**body, "summary": "SYNTHETIC: a different rationale entirely."},
        token=reviewer,
        code="IDEMPOTENCY_CONFLICT",
    )
    count = data_mcp.seed.scalar(
        "select count(*) from app.review_decisions where workspace_id = %s and case_id = %s",
        (data.workspace_id, data.cases["priced"]),
    )
    assert count == 1


async def test_release_is_idempotent_and_only_affects_the_callers_claim(data_mcp: DataHarness) -> None:
    client, case_id = data_mcp.client, str(data_mcp.data.cases["priced"])
    check = await schema_check(data_mcp)
    claim = await client.ok(
        "reviews_claim",
        {"case_id": case_id, "expected_version": 1, "idempotency_key": "claim-rel-0001"},
        token=data_mcp.reviewer,
    )
    token = claim["data"]["claim_token"]
    not_held = check(
        "reviews_release",
        await client.ok(
            "reviews_release",
            {"case_id": case_id, "claim_token": token, "idempotency_key": "release-other-1"},
            token=data_mcp.second_reviewer,
        ),
    )
    assert not_held["data"]["released"] is False and not_held["data"]["reason"] == "not_held"
    args = {"case_id": case_id, "claim_token": token, "idempotency_key": "release-own-001"}
    released = await client.ok("reviews_release", args, token=data_mcp.reviewer)
    assert released["data"]["released"] is True and released["data"]["state"] == "pending"
    assert (await client.ok("reviews_release", args, token=data_mcp.reviewer))["data"] == released["data"]
    await client.fails(
        "reviews_release",
        {**args, "claim_token": SECRET_TOKEN},
        token=data_mcp.reviewer,
        code="IDEMPOTENCY_CONFLICT",
    )


async def test_review_queue_pages_are_frozen_under_reprioritisation(data_mcp: DataHarness) -> None:
    client, data, seed = data_mcp.client, data_mcp.data, data_mcp.seed
    first = await client.ok("reviews_list_pending", {"limit": 1}, token=data_mcp.reviewer)
    assert first["data"]["total"] == 2 and first["next_cursor"]
    first_case = first["data"]["items"][0]["case_id"]
    other_case = next(
        str(c) for k, c in data.cases.items() if k in ("priced", "incomplete") and str(c) != first_case
    )
    # Between pages: reverse the priorities, add a brand-new high-priority pending case.
    seed.conn.execute(
        "update app.review_cases set priority = case when id = %s then -500 else 900 end"
        " where workspace_id = %s and id in (%s, %s)",
        (first_case, data.workspace_id, data.cases["priced"], data.cases["incomplete"]),
    )
    newcomer = seed.review_case(
        data.workspace_id,
        data.listings["needs_facts"],
        data.revisions["needs_facts"][-1],
        is_fixture=False,
        priority=5000,
    )
    second = await client.ok(
        "reviews_list_pending", {"limit": 1, "cursor": first["next_cursor"]}, token=data_mcp.reviewer
    )
    assert [i["case_id"] for i in second["data"]["items"]] == [other_case]
    assert second["next_cursor"] is None
    assert "FROZEN_QUEUE_PROJECTION" in {w["code"] for w in second["warnings"]}
    # A fresh query sees the new membership and order.
    fresh = await client.ok("reviews_list_pending", {"limit": 10}, token=data_mcp.reviewer)
    assert fresh["data"]["total"] == 3
    assert fresh["data"]["items"][0]["case_id"] == str(newcomer)
    # A cursor of another filter is refused.
    mismatch = await client.fails(
        "reviews_list_pending",
        {"limit": 1, "cursor": first["next_cursor"], "include_needs_information": False},
        token=data_mcp.reviewer,
        code="VALIDATION_ERROR",
    )
    assert mismatch["details"]["cursor"] == "mismatch"


# --------------------------------------------------------------------------------------------
# Other mutations
# --------------------------------------------------------------------------------------------


async def test_recheck_queues_a_bounded_job_for_registered_listings_only(data_mcp: DataHarness) -> None:
    client, data, seed = data_mcp.client, data_mcp.data, data_mcp.seed
    check = await schema_check(data_mcp)
    args = {
        "listing_id": str(data.listings["priced"]),
        "reason": "SYNTHETIC: price changed on the source",
        "idempotency_key": "recheck-mcp-0001",
    }
    queued = check(
        "deals_request_recheck", await client.ok("deals_request_recheck", args, token=data_mcp.reviewer)
    )
    result = queued["data"]
    assert result["job_type"] == "recheck" and result["state"] == "queued" and result["deduplicated"] is False
    job = seed.conn.execute(
        "select job_type, listing_id from ops.jobs where id = %s", (result["job_id"],)
    ).fetchone()
    assert job is not None and job[0] == "recheck" and job[1] == data.listings["priced"]
    assert (await client.ok("deals_request_recheck", args, token=data_mcp.reviewer))["data"] == result
    await client.fails(
        "deals_request_recheck",
        {**args, "reason": "SYNTHETIC: another reason"},
        token=data_mcp.reviewer,
        code="IDEMPOTENCY_CONFLICT",
    )
    dedup = await client.ok(
        "deals_request_recheck", {**args, "idempotency_key": "recheck-mcp-0002"}, token=data_mcp.reviewer
    )
    assert dedup["data"]["deduplicated"] is True and dedup["data"]["job_id"] == result["job_id"]
    await client.fails(
        "deals_request_recheck",
        {**args, "listing_id": str(data.listings["paused"]), "idempotency_key": "recheck-mcp-0003"},
        token=data_mcp.reviewer,
        code="SOURCE_PAUSED",
    )
    await client.fails(
        "deals_request_recheck",
        {**args, "listing_id": str(uuid.uuid4()), "idempotency_key": "recheck-mcp-0004"},
        token=data_mcp.reviewer,
        code="NOT_FOUND",
    )
    await client.fails("deals_request_recheck", args, token=data_mcp.viewer, code="FORBIDDEN")


async def test_notes_are_labelled_idempotent_and_private(data_mcp: DataHarness) -> None:
    client, data = data_mcp.client, data_mcp.data
    check = await schema_check(data_mcp)
    args = {
        "listing_id": str(data.listings["priced"]),
        "note": "SYNTHETIC: ask for the timing-belt invoice.",
        "idempotency_key": "note-mcp-00001",
    }
    created = check("deals_add_note", await client.ok("deals_add_note", args, token=data_mcp.reviewer))
    note = created["data"]
    assert note["label"] == "reviewer" and note["author_principal_id"] == str(data_mcp.users.reviewer)
    assert (await client.ok("deals_add_note", args, token=data_mcp.reviewer))["data"] == note
    await client.fails(
        "deals_add_note",
        {**args, "note": "SYNTHETIC: different"},
        token=data_mcp.reviewer,
        code="IDEMPOTENCY_CONFLICT",
    )
    await client.fails(
        "deals_add_note",
        {**args, "listing_id": str(uuid.uuid4()), "idempotency_key": "note-mcp-00002"},
        token=data_mcp.reviewer,
        code="NOT_FOUND",
    )
    detail = await client.ok("deals_get_candidate", {"listing_id": args["listing_id"]}, token=data_mcp.viewer)
    assert note["note_id"] in {n["note_id"] for n in detail["data"]["notes"]}


async def test_owner_pauses_a_source_with_optimistic_concurrency(data_mcp: DataHarness) -> None:
    client, data, seed = data_mcp.client, data_mcp.data, data_mcp.seed
    check = await schema_check(data_mcp)
    source = data.sources["running"]
    version = seed.scalar("select version from app.sources where id = %s", (source,))
    base = {"source_id": str(source), "reason": "SYNTHETIC: parser drift suspected"}
    await client.fails(
        "sources_pause",
        {**base, "expected_version": version + 1, "idempotency_key": "pause-stale-01"},
        token=data_mcp.owner,
        code="VERSION_CONFLICT",
    )
    args = {**base, "expected_version": version, "idempotency_key": "pause-mcp-0001"}
    paused = check("sources_pause", await client.ok("sources_pause", args, token=data_mcp.owner))
    assert paused["data"]["paused"] is True and paused["data"]["already_paused"] is False
    assert paused["data"]["version"] == version + 1
    assert "SOURCE_PAUSED" in {w["code"] for w in paused["warnings"]}
    assert (await client.ok("sources_pause", args, token=data_mcp.owner))["data"] == paused["data"]
    await client.fails(
        "sources_pause",
        {**args, "reason": "SYNTHETIC: another reason"},
        token=data_mcp.owner,
        code="IDEMPOTENCY_CONFLICT",
    )
    await client.fails(
        "sources_pause",
        {**args, "source_id": str(uuid.uuid4()), "idempotency_key": "pause-mcp-0002"},
        token=data_mcp.owner,
        code="NOT_FOUND",
    )


# --------------------------------------------------------------------------------------------
# Rate limiting, metrics, internal errors and the extension hook
# --------------------------------------------------------------------------------------------


async def test_expensive_tools_are_rate_limited_per_principal(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("MCP rate limit")
    users = add_members(seed, workspace)
    options = McpOptions(
        expensive_limit=RateLimit(capacity=2, per_seconds=60.0),
        cheap_limit=RateLimit(capacity=50, per_seconds=1.0),
    )
    async with mcp_client(make_settings(), db, keys, options=options) as client:
        viewer = tokens.for_role(users.viewer, Role.VIEWER)
        await client.ok("deals_list_candidates", token=viewer)
        await client.ok("deals_health", token=viewer)
        limited = await client.fails("deals_list_candidates", token=viewer, code="RATE_LIMITED")
        assert limited["retryable"] is True and 1 <= limited["retry_after_seconds"] <= 60
        # Cheap single-object reads and other principals keep their own buckets.
        await client.fails(
            "deals_get_valuation", {"valuation_id": str(uuid.uuid4())}, token=viewer, code="NOT_FOUND"
        )
        await client.ok("deals_list_candidates", token=tokens.for_role(users.owner, Role.OWNER))
        histogram = client.metrics.registry.get_sample_value(
            "suv_deals_request_duration_seconds_count",
            {"surface": "mcp", "route": "deals_list_candidates", "status_class": "4xx"},
        )
        assert histogram == 1


async def test_unexpected_handler_errors_never_leak_details(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async def explode(call: ToolCall[Any]) -> ResponseEnvelope[Any]:
        raise RuntimeError("SELECT secret FROM ops.api_credentials -- password=hunter2")

    probe = ToolSpec(
        name="synthetic_probe",
        input_model=DealsHealthInput,
        output_model=HealthView,
        scope=Scope.DEALS_READ,
        annotations=TOOLS["deals_health"].annotations,
        description="SYNTHETIC extension probe.",
    )
    workspace = seed.workspace("MCP internal error")
    users = add_members(seed, workspace)
    async with mcp_client(make_settings(), db, keys, extra_tools=[(probe, explode)]) as client:
        viewer = tokens.for_role(users.viewer, Role.VIEWER)
        names = [t["name"] for t in await client.tools(viewer)]
        assert names[-1] == "synthetic_probe" and names[:-1] == [*READ_TOOLS, "reviews_list_pending"]
        result = await client.call("synthetic_probe", {}, token=viewer)
        assert result["isError"] is True
        payload = result["structuredContent"]
        assert payload["code"] == "INTERNAL_ERROR" and payload["message"] == "Internal error"
        assert "SELECT" not in str(result) and "hunter2" not in str(result)
        await client.fails("synthetic_probe", {"x": 1}, token=viewer, code="VALIDATION_ERROR")


def test_registry_refuses_forbidden_and_duplicate_tools() -> None:
    forbidden = ToolSpec(
        name="execute_sql",
        input_model=DealsHealthInput,
        output_model=HealthView,
        scope=Scope.CONFIG_ADMIN,
        annotations=TOOLS["deals_health"].annotations,
        description="must never exist",
    )
    with pytest.raises(ValueError, match="must never exist"):
        ToolRegistry.default().with_tools([(forbidden, lambda call: None)])  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="registered twice"):
        ToolRegistry.default().with_tools([(TOOLS["deals_health"], lambda call: None)])  # type: ignore[arg-type,return-value]
    assert ToolRegistry.default().names == TOOL_NAMES


async def test_unauthenticated_principal_cannot_reach_any_tool_handler(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys)
    async with running(app) as http:
        response = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        assert response.status_code == 401
    assert role_scopes(Role.VIEWER) == [Scope.DEALS_READ, Scope.REVIEWS_READ]
    assert build_mcp is not None
    assert isinstance(AppMetrics(process_metrics=False), AppMetrics)


async def test_every_tool_requires_its_own_scope(data_mcp: DataHarness) -> None:
    client = data_mcp.client
    for name in TOOL_NAMES:
        spec = TOOLS[name]
        others = [s for s in role_scopes(Role.OWNER) if s != spec.scope]
        token = data_mcp.tokens.mint(data_mcp.users.owner, scopes=others)
        assert name not in [t["name"] for t in await client.tools(token)]
        payload = await client.fails(name, {}, token=token, code="FORBIDDEN")
        assert spec.scope.value in payload["message"]


async def test_review_mutations_on_unknown_cases_are_not_found(data_mcp: DataHarness) -> None:
    client, reviewer = data_mcp.client, data_mcp.reviewer
    unknown = str(uuid.uuid4())
    await client.fails(
        "reviews_claim",
        {"case_id": unknown, "expected_version": 1, "idempotency_key": "claim-unknown-1"},
        token=reviewer,
        code="NOT_FOUND",
    )
    await client.fails(
        "reviews_release",
        {"case_id": unknown, "claim_token": SECRET_TOKEN, "idempotency_key": "release-unknown"},
        token=reviewer,
        code="NOT_FOUND",
    )
    await client.fails(
        "reviews_submit",
        {**submit_args(data_mcp, SECRET_TOKEN, "submit-unknown-1"), "case_id": unknown},
        token=reviewer,
        code="NOT_FOUND",
    )
