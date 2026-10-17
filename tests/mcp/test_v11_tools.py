"""The three spec 37.8 MCP tools: ``seller_inquiries_get``, ``seller_replies_get``
(``inquiries:read``) and ``seller_inquiries_pause`` (``inquiries:pause``).

Exact ``V11_TOOLS`` schemas, workspace from the token only, scope filtering of ``tools/list``,
reply output per spec 37.8 (no secrets, no unrelated thread message, the sender address only for
scopes ``recipient_address_visible`` allows: never on MCP, which never carries ``config:admin``),
idempotent and versioned pause, and no send/reply/resume tool. SYNTHETIC data only; nothing is
ever sent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.enums import Role
from suv_deals.mcp.schemas import V11_TOOL_NAMES, V11_TOOLS, tool_input_schema
from suv_deals.persistence.database import Database
from tests.api.v11_support import (
    MAIL,
    MailWorker,
    accepted_send,
    controls_version,
    issue_worker,
    latest_binding_version,
    mail_worker_api,
    outlook_world,
    reply_body,
    upload_reply,
)
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import World
from tests.mcp.conftest import (
    McpClient,
    SigningKeys,
    TokenFactory,
    Users,
    add_members,
    make_settings,
    mcp_client,
)

pytestmark = pytest.mark.db

SPEC_37_8 = {
    "seller_inquiries_get": {
        "type": "object",
        "additionalProperties": False,
        "required": ["inquiry_id"],
        "properties": {"inquiry_id": {"type": "string", "format": "uuid"}},
    },
    "seller_replies_get": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply_id"],
        "properties": {"reply_id": {"type": "string", "format": "uuid"}},
    },
    "seller_inquiries_pause": {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected_version", "reason", "idempotency_key"],
        "properties": {
            "expected_version": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 3, "maxLength": 2000},
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
        },
    },
}


@dataclass
class V11Harness:
    client: McpClient
    tokens: TokenFactory
    users: Users
    db: Database
    seed: Seed
    world: World
    worker: MailWorker
    inquiry_id: UUID
    reply_id: str

    def token(self, user: UUID, role: Role) -> str:
        return self.tokens.for_role(user, role)


@pytest.fixture
async def v11_mcp(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[V11Harness]:
    world = await outlook_world(db, seed, "MCP inquiries A")
    users = add_members(seed, world.workspace_id)
    worker = await issue_worker(db, world)
    async with mail_worker_api(db) as api:
        inquiry_id, intent = await accepted_send(api, db, world, worker)
        ack = await upload_reply(api, world, worker, inquiry_id, intent)
    async with mcp_client(make_settings(), db, keys) as client:
        yield V11Harness(
            client=client,
            tokens=tokens,
            users=users,
            db=db,
            seed=seed,
            world=world,
            worker=worker,
            inquiry_id=inquiry_id,
            reply_id=ack["reply_id"],
        )


def _subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(actual[k], v) for k, v in expected.items()
        )
    return bool(actual == expected)


def test_v11_input_schemas_are_exactly_spec_37_8() -> None:
    for name, expected in SPEC_37_8.items():
        schema = tool_input_schema(name)
        assert _subset(schema, expected), name
        assert set(schema["properties"]) == set(expected["properties"])
        assert schema["additionalProperties"] is False
    assert V11_TOOLS["seller_inquiries_pause"].scope.value == "inquiries:pause"
    assert {V11_TOOLS[n].scope.value for n in ("seller_inquiries_get", "seller_replies_get")} == {
        "inquiries:read"
    }
    lowered = {n.lower() for n in V11_TOOL_NAMES}
    assert not any(word in n for n in lowered for word in ("send", "reply_to", "resume", "approve"))


async def test_tools_list_shows_inquiry_tools_by_scope(v11_mcp: V11Harness) -> None:
    owner = [t["name"] for t in await v11_mcp.client.tools(v11_mcp.token(v11_mcp.users.owner, Role.OWNER))]
    reviewer = [
        t["name"] for t in await v11_mcp.client.tools(v11_mcp.token(v11_mcp.users.reviewer, Role.REVIEWER))
    ]
    viewer = [t["name"] for t in await v11_mcp.client.tools(v11_mcp.token(v11_mcp.users.viewer, Role.VIEWER))]
    assert set(V11_TOOL_NAMES) <= set(owner)
    assert {"seller_inquiries_get", "seller_replies_get"} <= set(reviewer)
    assert "seller_inquiries_pause" not in reviewer
    assert not set(V11_TOOL_NAMES) & set(viewer)
    assert not any("send" in n for n in owner)


async def test_seller_inquiries_get_returns_the_workspace_inquiry_without_the_address(
    v11_mcp: V11Harness,
) -> None:
    token = v11_mcp.token(v11_mcp.users.owner, Role.OWNER)
    result = await v11_mcp.client.ok(
        "seller_inquiries_get", {"inquiry_id": str(v11_mcp.inquiry_id)}, token=token
    )
    data = result["data"]
    assert data["inquiry_id"] == str(v11_mcp.inquiry_id)
    assert data["approval_required"] is False
    assert data["recipient"]["address"] is None and data["recipient"]["address_redacted"] is True
    text = str(result)
    assert str(v11_mcp.world.vehicle.address) not in text and v11_mcp.worker.token not in text
    missing = await v11_mcp.client.fails(
        "seller_inquiries_get", {"inquiry_id": str(uuid.uuid4())}, token=token, code="NOT_FOUND"
    )
    assert "inquiry" not in str(missing.get("details") or {})
    await v11_mcp.client.fails(
        "seller_inquiries_get", {"inquiry_id": "not-a-uuid"}, token=token, code="VALIDATION_ERROR"
    )
    await v11_mcp.client.fails(
        "seller_inquiries_get",
        {"inquiry_id": str(v11_mcp.inquiry_id), "workspace_id": str(uuid.uuid4())},
        token=token,
        code="VALIDATION_ERROR",
    )
    await v11_mcp.client.fails(
        "seller_inquiries_get",
        {"inquiry_id": str(v11_mcp.inquiry_id)},
        token=v11_mcp.token(v11_mcp.users.viewer, Role.VIEWER),
        code="FORBIDDEN",
    )


async def test_foreign_workspace_records_are_not_found(v11_mcp: V11Harness, db: Database, seed: Seed) -> None:
    other = await outlook_world(db, seed, "MCP inquiries B")
    other_users = add_members(seed, other.workspace_id)
    del other_users
    token = v11_mcp.token(v11_mcp.users.owner, Role.OWNER)
    other_worker = await issue_worker(db, other)
    async with mail_worker_api(db) as api:
        foreign_inquiry, intent = await accepted_send(api, db, other, other_worker)
        foreign_reply = (await upload_reply(api, other, other_worker, foreign_inquiry, intent))["reply_id"]
    await v11_mcp.client.fails(
        "seller_inquiries_get", {"inquiry_id": str(foreign_inquiry)}, token=token, code="NOT_FOUND"
    )
    await v11_mcp.client.fails(
        "seller_replies_get", {"reply_id": foreign_reply}, token=token, code="NOT_FOUND"
    )


async def test_seller_replies_get_returns_the_reply_per_spec_37_8(v11_mcp: V11Harness) -> None:
    token = v11_mcp.token(v11_mcp.users.reviewer, Role.REVIEWER)
    result = await v11_mcp.client.ok("seller_replies_get", {"reply_id": v11_mcp.reply_id}, token=token)
    data = result["data"]
    assert data["reply_id"] == v11_mcp.reply_id and data["inquiry_id"] == str(v11_mcp.inquiry_id)
    assert data["vehicle"]["listing_id"] == str(v11_mcp.world.listing_id)
    assert "verfuegbar" in data["sanitized_body"]
    assert data["mk_summary"]
    assert data["sender"]["address"] is None and data["sender"]["address_domain"]
    assert data["received_at"] and data["ingested_at"]
    assert "valuation" in data
    for attachment in data["attachments"]:
        assert "local_ref" not in attachment
    text = str(result)
    assert str(v11_mcp.world.vehicle.address) not in text
    assert v11_mcp.worker.token not in text and "suvmail_" not in text
    assert "synthetic-local-locator" not in text  # Outlook locators are never returned
    await v11_mcp.client.fails(
        "seller_replies_get", {"reply_id": str(uuid.uuid4())}, token=token, code="NOT_FOUND"
    )


async def test_seller_inquiries_pause_is_scoped_idempotent_and_versioned(v11_mcp: V11Harness) -> None:
    owner = v11_mcp.token(v11_mcp.users.owner, Role.OWNER)
    reviewer = v11_mcp.token(v11_mcp.users.reviewer, Role.REVIEWER)
    version = await controls_version(v11_mcp.db, v11_mcp.world.workspace_id)
    arguments = {
        "expected_version": version,
        "reason": "dot pauses inquiries",
        "idempotency_key": "mcp-pause-0001",
    }
    await v11_mcp.client.fails("seller_inquiries_pause", arguments, token=reviewer, code="FORBIDDEN")
    first = await v11_mcp.client.ok("seller_inquiries_pause", arguments, token=owner)
    assert first["data"]["version"] == version + 1 and first["data"]["already_paused"] is False
    replay = await v11_mcp.client.ok("seller_inquiries_pause", arguments, token=owner)
    assert replay["data"] == first["data"]
    await v11_mcp.client.fails(
        "seller_inquiries_pause",
        {**arguments, "reason": "a different reason"},
        token=owner,
        code="IDEMPOTENCY_CONFLICT",
    )
    stale = await v11_mcp.client.fails(
        "seller_inquiries_pause",
        {"expected_version": version, "reason": "stale pause", "idempotency_key": "mcp-pause-0002"},
        token=owner,
        code="VERSION_CONFLICT",
    )
    assert stale["details"]["current_version"] == version + 1
    again = await v11_mcp.client.ok(
        "seller_inquiries_pause",
        {"expected_version": version + 1, "reason": "already paused", "idempotency_key": "mcp-pause-0003"},
        token=owner,
    )
    assert again["data"]["already_paused"] is True and again["data"]["version"] == version + 1
    for bad in (
        {"expected_version": 0, "reason": "zero", "idempotency_key": "mcp-pause-0004"},
        {"expected_version": version, "reason": "no", "idempotency_key": "mcp-pause-0005"},
        {"expected_version": version, "reason": "short key", "idempotency_key": "short"},
        {"expected_version": version, "reason": "extra", "idempotency_key": "mcp-pause-0006", "resume": True},
    ):
        await v11_mcp.client.fails("seller_inquiries_pause", bad, token=owner, code="VALIDATION_ERROR")
    controls = v11_mcp.seed.conn.execute(
        "select kill_switch from app.seller_inquiry_controls where workspace_id = %s",
        (v11_mcp.world.workspace_id,),
    ).fetchone()
    assert controls is not None and controls[0] is True


async def test_quarantined_reply_content_is_withheld_on_mcp(v11_mcp: V11Harness) -> None:
    """A quarantined possible match (here: the inquiry's Message-ID quoted from an address that is
    not the verified seller's) is not verified as related to the inquiry; it may be unrelated
    personal mail. Its text never reaches dot (and through it a model provider) over MCP: only the
    metadata and the quarantine reason do, while the owner verifies it on the dashboard."""
    row = v11_mcp.seed.conn.execute(
        "select rfc_message_id from app.seller_inquiries where id = %s", (v11_mcp.inquiry_id,)
    ).fetchone()
    assert row is not None
    async with mail_worker_api(v11_mcp.db) as api:
        version = await latest_binding_version(api, v11_mcp.worker, v11_mcp.inquiry_id)
        document = reply_body(
            inquiry_id=v11_mcp.inquiry_id,
            mailbox_id=v11_mcp.worker.mailbox_id,
            binding_version=version,
            from_address="family.member@private.example.invalid",
            in_reply_to=row[0],
            subject="Re: Familienessen am Freitag (synthetic)",
            body="Liebe Gruesse, das Familienessen ist am Freitag um 19 Uhr. Synthetic private note.",
        )
        response = await api.post(
            f"{MAIL}/replies", headers=v11_mcp.worker.headers("mwr1-quarantine-0001"), json=document
        )
    assert response.status_code == 200, response.text
    reply_id = response.json()["reply_id"]
    stored = v11_mcp.seed.conn.execute(
        "select quarantined, sanitized_body from app.seller_replies where id = %s", (reply_id,)
    ).fetchone()
    assert stored is not None and stored[0] is True and "Familienessen" in stored[1]

    for role, user in ((Role.OWNER, v11_mcp.users.owner), (Role.REVIEWER, v11_mcp.users.reviewer)):
        result = await v11_mcp.client.ok(
            "seller_replies_get", {"reply_id": reply_id}, token=v11_mcp.token(user, role)
        )
        text = json.dumps(result)
        assert "Familienessen" not in text and "Freitag" not in text
        assert "family.member" not in text
        data = result["data"]
        assert data["reply_id"] == reply_id and data["quarantined"] is True
        assert data["quarantine_reason"] and data["content_withheld"] is True
        assert data["sanitized_body"] == "" and data["subject"] == ""
        assert data["mk_summary"] is None and data["claims"] is None and data["attachments"] == []
    # A matched, verified reply keeps its text on MCP.
    matched = await v11_mcp.client.ok(
        "seller_replies_get",
        {"reply_id": v11_mcp.reply_id},
        token=v11_mcp.token(v11_mcp.users.owner, Role.OWNER),
    )
    assert matched["data"]["content_withheld"] is False and matched["data"]["sanitized_body"]
