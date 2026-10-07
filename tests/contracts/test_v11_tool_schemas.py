"""Spec 37.8 MCP tool contracts (``mcp.schemas.V11_TOOLS``) and validation-error field names.

The three inquiry tools are prepared but NOT served until the inquiry package registers handlers
(``build_mcp(extra_tools=...)``). Their input schemas are compared with the JSON block of spec
37.8 itself; outputs are closed, resolved envelopes without money as JSON numbers and without
secrets, raw recipient addresses (unless the owner reads), local locators or signed links.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from suv_deals.domain.actor import ROLE_SCOPES
from suv_deals.domain.enums import Role, Scope
from suv_deals.domain.replies import ReplyIngestRequest
from suv_deals.errors import ValidationFailed
from suv_deals.mcp.schemas import (
    FORBIDDEN_TOOL_NAMES,
    TOOLS,
    V11_TOOL_NAMES,
    V11_TOOLS,
    DealsGetCandidateInput,
    DealsListCandidatesInput,
    SellerInquiriesPauseInput,
    exported_schema_documents,
    mcp_tool_definition,
    tool_document,
    tool_input_schema,
    tool_output_schema,
    tool_spec,
    tools_for_scopes,
    validate_tool_input,
    validation_error_fields,
)
from suv_deals.mcp.tools import ToolRegistry
from suv_deals.views.inquiries import (
    InquiryPauseResult,
    InquiryView,
    RecipientView,
    ReplySenderView,
    ReplySummaryView,
    ReplyView,
    SendAttemptSummary,
    address_domain,
    recipient_address_visible,
)
from suv_deals.views.jsonschema import find_refs, open_objects

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "spec" / "suv-deal-system-build-spec.md"
UID = "12345678-1234-4234-8234-123456789abc"
KEY = "pause-0001-abcd"


def spec_37_8_inputs() -> dict[str, Any]:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("```json", text.index("### 37 8 Data API and operational extensions")) + len("```json")
    end = text.index("```", start)
    loaded: dict[str, Any] = json.loads(text[start:end])
    return loaded


def test_the_three_spec_tools_with_their_scopes() -> None:
    assert V11_TOOL_NAMES == ("seller_inquiries_get", "seller_replies_get", "seller_inquiries_pause")
    assert {n: s.scope for n, s in V11_TOOLS.items()} == {
        "seller_inquiries_get": Scope.INQUIRIES_READ,
        "seller_replies_get": Scope.INQUIRIES_READ,
        "seller_inquiries_pause": Scope.INQUIRIES_PAUSE,
    }
    assert {n: s.output_model for n, s in V11_TOOLS.items()} == {
        "seller_inquiries_get": InquiryView,
        "seller_replies_get": ReplyView,
        "seller_inquiries_pause": InquiryPauseResult,
    }
    # Kept apart from the served registry; never a send/resume/admin tool.
    assert not set(V11_TOOLS) & set(TOOLS)
    assert not set(V11_TOOLS) & FORBIDDEN_TOOL_NAMES
    for name in V11_TOOLS:
        assert not any(word in name for word in ("send", "resume", "message", "admin", "enable"))


def test_v11_tools_are_not_served_or_discoverable_yet() -> None:
    assert not set(ToolRegistry.default().names) & set(V11_TOOLS)
    owner = {spec.name for spec in tools_for_scopes(ROLE_SCOPES[Role.OWNER])}
    assert not owner & set(V11_TOOLS)
    with pytest.raises(ValidationFailed):
        tool_spec("seller_inquiries_get")  # the served lookup is unchanged
    with pytest.raises(ValidationFailed):
        validate_tool_input("seller_inquiries_pause", {})


def test_v11_tools_register_as_extensions() -> None:
    async def handler(call: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError

    registry = ToolRegistry.default().with_tools((spec, handler) for spec in V11_TOOLS.values())
    assert registry.names[-3:] == V11_TOOL_NAMES
    for name in V11_TOOL_NAMES:
        tool = registry.get(name)
        assert tool is not None
        assert tool.input_schema == tool_input_schema(name)
        assert tool.definition.output_schema == tool_output_schema(name)
    reviewer = {t.name for t in registry.visible(ROLE_SCOPES[Role.REVIEWER])}
    assert {"seller_inquiries_get", "seller_replies_get"} <= reviewer
    assert "seller_inquiries_pause" not in reviewer  # owner-only scope (narrowly grantable)
    viewer = {t.name for t in registry.visible(ROLE_SCOPES[Role.VIEWER])}
    assert not viewer & set(V11_TOOLS)


def test_annotations_describe_real_behaviour() -> None:
    for name in ("seller_inquiries_get", "seller_replies_get"):
        hints = V11_TOOLS[name].annotations
        assert hints.read_only and hints.idempotent and not hints.destructive and not hints.open_world
        assert V11_TOOLS[name].idempotency_operation is None
    pause = V11_TOOLS["seller_inquiries_pause"]
    assert not pause.annotations.read_only and pause.annotations.idempotent
    assert not pause.annotations.destructive and not pause.annotations.open_world
    assert pause.idempotency_operation == "seller_inquiries_pause"
    for spec in V11_TOOLS.values():
        assert 40 <= len(spec.description) <= 600
        assert not spec.paginated


def test_input_schemas_are_exactly_the_spec_37_8_block() -> None:
    expected_map = spec_37_8_inputs()
    assert list(expected_map) == list(V11_TOOL_NAMES)
    for name, expected in expected_map.items():
        ours = tool_input_schema(name)
        assert ours["additionalProperties"] is False and expected["additionalProperties"] is False
        assert ours["type"] == expected["type"] == "object"
        assert set(ours["properties"]) == set(expected["properties"]), name
        assert list(ours["required"]) == list(expected["required"]), name
        for prop, constraints in expected["properties"].items():
            actual = ours["properties"][prop]
            for keyword, value in constraints.items():
                assert actual.get(keyword) == value, f"{name}.{prop}.{keyword}"
            # Only documented narrowing beyond the spec: the shared idempotency key pattern.
            extra = set(actual) - set(constraints) - {"description", "title"}
            assert extra <= {"pattern"}, f"{name}.{prop}: {extra}"
            if "pattern" in extra:
                assert prop == "idempotency_key"
        assert not find_refs(ours) and not open_objects(ours)


@pytest.mark.parametrize("name", V11_TOOL_NAMES)
def test_output_schemas_are_closed_envelopes_without_numbers(name: str) -> None:
    schema = tool_output_schema(name)
    assert set(schema["required"]) == {
        "schema_version",
        "request_id",
        "as_of",
        "data",
        "warnings",
        "next_cursor",
    }
    assert not find_refs(schema) and not open_objects(schema)
    text = json.dumps(schema)
    assert '"number"' not in text
    for forbidden in ("local_ref", "outlook_entry_id", "outlook_store_id", "secret", "token", "account_id"):
        assert forbidden not in text, (name, forbidden)
    data = schema["properties"]["data"]
    assert data["additionalProperties"] is False


def test_inquiry_output_has_the_required_parts() -> None:
    props = tool_output_schema("seller_inquiries_get")["properties"]["data"]["properties"]
    for field in (
        "inquiry_id",
        "vehicle",
        "state",
        "qualification",
        "authorization",
        "template",
        "language",
        "recipient",
        "send_attempts",
        "delivery_uncertain",
        "suppression_reason",
        "timestamps",
        "approval_required",
    ):
        assert field in props, field
    assert props["approval_required"]["const"] is False
    recipient = props["recipient"]["properties"]
    assert {"verification_status", "address", "address_domain", "address_redacted"} <= set(recipient)
    assert "from_address" not in props["sender"]["properties"]


def test_reply_output_has_the_required_parts() -> None:
    props = tool_output_schema("seller_replies_get")["properties"]["data"]["properties"]
    for field in (
        "reply_id",
        "inquiry_id",
        "vehicle",
        "original_language",
        "sanitized_body",
        "mk_summary",
        "sender",
        "received_at",
        "ingested_at",
        "claims",
        "attachments",
        "valuation",
    ):
        assert field in props, field
    quote = props["claims"]["properties"]["price_quotes"]["items"]["properties"]
    assert quote["accepted"]["const"] is False and quote["status"]["const"] == "unaccepted_seller_quote"
    assert set(props["attachments"]["items"]["properties"]) == {
        "filename",
        "mime_type",
        "byte_size",
        "sha256",
        "action",
        "document_kind",
    }


def test_pause_output_reports_the_new_version_and_kill_switch() -> None:
    props = tool_output_schema("seller_inquiries_pause")["properties"]["data"]["properties"]
    assert props["kill_switch"]["const"] is True
    assert props["version"]["minimum"] == 1
    assert {"already_paused", "kill_switch_set_at", "mode"} <= set(props)


def test_inputs_validate_strictly() -> None:
    model = SellerInquiriesPauseInput
    ok = model.model_validate({"expected_version": 3, "reason": "Owner pause", "idempotency_key": KEY})
    assert ok.expected_version == 3
    for bad in (
        {"expected_version": 0, "reason": "Owner pause", "idempotency_key": KEY},
        {"expected_version": "3", "reason": "Owner pause", "idempotency_key": KEY},
        {"expected_version": 3, "reason": "x", "idempotency_key": KEY},
        {"expected_version": 3, "reason": "Owner pause", "idempotency_key": "short"},
        {
            "expected_version": 3,
            "reason": "Owner pause",
            "idempotency_key": KEY,
            "recipient": "x@example.invalid",
        },
    ):
        with pytest.raises(ValidationError):
            model.model_validate(bad)
    get = V11_TOOLS["seller_inquiries_get"].input_model
    assert get.model_validate({"inquiry_id": UID})
    for bad_id in ({"inquiry_id": "not-a-uuid"}, {"inquiry_id": UID, "workspace_id": UID}, {}):
        with pytest.raises(ValidationError):
            get.model_validate(bad_id)


def test_snapshots_include_the_v11_tools() -> None:
    documents = exported_schema_documents()
    for name in V11_TOOL_NAMES:
        doc = documents[f"tools/{name}.json"]
        assert doc == tool_document(name)
        assert doc["requiredScope"] == V11_TOOLS[name].scope.value
        assert mcp_tool_definition(name)["name"] == name


# ---------------------------------------------------------------------------------------------
# validation_error_fields: plain field names, never pydantic union-member tags
# ---------------------------------------------------------------------------------------------


def _fields(model: type[BaseModel], data: dict[str, Any], *, with_model: bool) -> list[str]:
    with pytest.raises(ValidationError) as info:
        model.model_validate(data)
    return validation_error_fields(info.value, model=model if with_model else None)


@pytest.mark.parametrize("with_model", [True, False])
def test_union_member_tags_are_stripped(with_model: bool) -> None:
    fields = _fields(
        DealsListCandidatesInput,
        {"country": "xx", "status": "nope", "changed_since": "yesterday", "profile": "other"},
        with_model=with_model,
    )
    assert fields == ["changed_since", "country", "profile", "status"]
    assert _fields(DealsGetCandidateInput, {"listing_id": "x", "revision": "a"}, with_model=with_model) == [
        "listing_id",
        "revision",
    ]
    # An unknown key that is not a plain name is still reported as unrecognised (never echoed).
    assert _fields(DealsListCandidatesInput, {"<script>": 1}, with_model=with_model) == [
        "<unrecognised field>"
    ]


def test_nested_paths_keep_real_field_names() -> None:
    data = {
        "schema_version": "1.0",
        "inquiry_id": UID,
        "binding_version": 1,
        "mailbox_binding_id": UID,
        "source_message": {"received_at": "not-a-time"},
        "headers": {"from": "not an address"},
        "subject": "s",
        "sanitized_body_text": "b",
        "attachments": [{"filename": "a.pdf", "mime_type": "application/pdf", "byte_size": 1, "sha256": "x"}],
        "observed_at": "2026-10-06T18:00:02Z",
        "detected_language": "xx",
    }
    for with_model in (True, False):
        assert _fields(ReplyIngestRequest, data, with_model=with_model) == [
            "attachments.0.sha256",
            "detected_language",
            "headers.from",
            "source_message.received_at",
        ]


def test_tool_validation_errors_name_plain_fields() -> None:
    with pytest.raises(ValidationFailed) as info:
        validate_tool_input("deals_list_candidates", {"country": "xx"})
    assert info.value.details == {"fields": ["country"]}


# ---------------------------------------------------------------------------------------------
# Privacy helpers of the read models
# ---------------------------------------------------------------------------------------------


def test_recipient_address_is_owner_only() -> None:
    assert recipient_address_visible(ROLE_SCOPES[Role.OWNER])
    assert not recipient_address_visible(ROLE_SCOPES[Role.REVIEWER])
    assert address_domain("Seller@Dealer.Example.Invalid") == "dealer.example.invalid"
    common: dict[str, Any] = {
        "contact_id": UUID(UID),
        "verification_status": "verified",
        "contact_kind": "ad_email",
        "language": "de",
        "language_status": "resolved",
        "verified_at": None,
    }
    hidden = RecipientView.build(address="seller@dealer.example.invalid", show_address=False, **common)
    assert (
        hidden.address is None
        and hidden.address_redacted
        and hidden.address_domain == "dealer.example.invalid"
    )
    shown = RecipientView.build(address="seller@dealer.example.invalid", show_address=True, **common)
    assert shown.address == "seller@dealer.example.invalid" and not shown.address_redacted
    with pytest.raises(ValidationError):
        RecipientView.model_validate({**hidden.model_dump(), "address": "seller@dealer.example.invalid"})


# ---------------------------------------------------------------------------------------------
# Review regressions: read models accept every value the database accepts; tag stripping never
# eats a real nested field name
# ---------------------------------------------------------------------------------------------

_NOW = "2026-10-06T18:00:00Z"
_VEHICLE: dict[str, Any] = {
    "vehicle_kind": "listing_incarnation",
    "vehicle_cluster_id": None,
    "listing_id": UID,
    "source_key": None,
    "listing_reference": None,
    "listing_url": None,
}


@pytest.mark.parametrize("code", ["LEASE_EXPIRED", "_lease", ".x", "-x", ":x", "a" * 80])
def test_attempt_error_codes_follow_the_database_check(code: str) -> None:
    """``ops.email_delivery_attempts_error_ck`` is ``^[A-Za-z0-9_.:-]{1,80}$``; a stricter view
    pattern turned a valid stored attempt into a read failure (500)."""
    attempt = SendAttemptSummary.model_validate(
        {
            "attempt_number": 1,
            "provider": "outlook_local",
            "outcome": "uncertain",
            "send_intent_committed_at": _NOW,
            "finished_at": _NOW,
            "reconciled_outcome": None,
            "reconciled_at": None,
            "submission_uncertain": True,
            "error_code": code,
        }
    )
    assert attempt.error_code == code
    for bad in ("", "a" * 81, "has space", "x/y"):
        with pytest.raises(ValidationError):
            SendAttemptSummary.model_validate({**attempt.model_dump(mode="json"), "error_code": bad})


def test_reply_language_is_any_stored_iso_code() -> None:
    """``seller_replies.detected_language`` and ``seller_contacts.language_code`` are
    ``^[a-z]{2}$``: a Dutch reply or an unsupported contact language must stay readable."""
    summary = ReplySummaryView.model_validate(
        {
            "reply_id": UID,
            "inquiry_id": UID,
            "vehicle": _VEHICLE,
            "message_type": "seller_reply",
            "original_language": "nl",
            "availability": None,
            "quarantined": False,
            "processing_state": "stored",
            "received_at": _NOW,
            "ingested_at": _NOW,
        }
    )
    assert summary.original_language == "nl"
    held = RecipientView.build(
        address=None,
        show_address=False,
        contact_id=None,
        verification_status="verified",
        contact_kind="ad_email",
        language="nl",
        language_status="unsupported_language",
        verified_at=None,
    )
    assert held.language == "nl"
    for bad in ("NL", "nld", "n", ""):
        with pytest.raises(ValidationError):
            ReplySummaryView.model_validate({**summary.model_dump(mode="json"), "original_language": bad})
    schema = tool_output_schema("seller_replies_get")["properties"]["data"]["properties"]
    assert schema["original_language"]["pattern"] == "^[a-z]{2}$"
    # The inquiry itself is only ever sent in one of the four template languages.
    inquiry = tool_output_schema("seller_inquiries_get")["properties"]["data"]["properties"]
    assert inquiry["language"]["enum"] == ["de", "it", "fr", "en", None]
    assert inquiry["recipient"]["properties"]["language"]["pattern"] == "^[a-z]{2}$"


class _Inner(BaseModel):
    model: int
    url: int
    date: int
    none: int


class _Outer(BaseModel):
    vehicle: _Inner
    choice: int | str
    nested: _Inner | int


@pytest.mark.parametrize("with_model", [True, False])
def test_real_nested_field_names_are_never_taken_for_union_tags(with_model: bool) -> None:
    data = {
        "vehicle": {"model": "x", "url": "y", "date": "z", "none": "w"},
        "choice": [],
        "nested": {"model": "x", "url": 1, "date": 1, "none": 1},
    }
    assert _fields(_Outer, data, with_model=with_model) == [
        "choice",
        "nested",
        "nested.model",
        "vehicle.date",
        "vehicle.model",
        "vehicle.none",
        "vehicle.url",
    ]


def test_reply_sender_address_is_owner_only() -> None:
    common: dict[str, Any] = {
        "matches_verified_recipient": True,
        "correlation_status": "matched",
        "correlation_reasons": ("IN_REPLY_TO_MATCH",),
        "header_linked": True,
        "thread_linked": False,
    }
    hidden = ReplySenderView.build(
        address="seller@dealer.example.invalid",
        show_address=recipient_address_visible(ROLE_SCOPES[Role.REVIEWER]),
        **common,
    )
    assert hidden.address is None and hidden.address_redacted
    assert hidden.address_domain == "dealer.example.invalid"
    assert "seller@" not in hidden.model_dump_json()
    shown = ReplySenderView.build(
        address="seller@dealer.example.invalid",
        show_address=recipient_address_visible(ROLE_SCOPES[Role.OWNER]),
        **common,
    )
    assert shown.address == "seller@dealer.example.invalid" and not shown.address_redacted
