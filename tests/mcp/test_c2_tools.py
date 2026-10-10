"""Work package C2 on the MCP surface: ``details.reason`` of a transient conflict reaches the tool
error payload (``busy`` for a lost race / lock timeout, ``in_progress`` for an idempotent request
whose key is still running), the quarantine rule comes from the query layer only, the reply view
carries its signal status, and there is still no resume tool. SYNTHETIC data only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role
from suv_deals.mcp import tools as mcp_tools
from suv_deals.mcp.schemas import SellerInquiriesPauseInput
from suv_deals.mcp.tools import PAUSE_OPERATION, ToolRegistry, error_result
from suv_deals.persistence import idempotency
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.transactions import unit_of_work
from tests.api.v11_support import controls_version, outlook_world
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


@dataclass
class C2Mcp:
    client: McpClient
    tokens: TokenFactory
    users: Users
    db: Database
    world: World


@pytest.fixture
async def c2_mcp(db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory) -> AsyncIterator[C2Mcp]:
    world = await outlook_world(db, seed, "MCP C2")
    users = add_members(seed, world.workspace_id)
    async with mcp_client(make_settings(), db, keys) as client:
        yield C2Mcp(client=client, tokens=tokens, users=users, db=db, world=world)


def _owner_actor(workspace_id: UUID, user: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=user,
        principal_kind="user",
        role=Role.OWNER,
        scopes=ROLE_SCOPES[Role.OWNER],
        request_id="req-c2-mcp-owner",
        display_name="Synthetic owner",
    )


@pytest.mark.db
async def test_pause_still_in_progress_is_a_retryable_in_progress_conflict(c2_mcp: C2Mcp) -> None:
    owner_token = c2_mcp.tokens.for_role(c2_mcp.users.owner, Role.OWNER)
    arguments = SellerInquiriesPauseInput(
        expected_version=await controls_version(c2_mcp.db, c2_mcp.world.workspace_id),
        reason="dot pauses while another call runs",
        idempotency_key="mcp-c2-inflight-0001",
    )
    actor = _owner_actor(c2_mcp.world.workspace_id, c2_mcp.users.owner)
    async with unit_of_work(c2_mcp.db, actor) as conn:
        started = await idempotency.begin(
            conn,
            actor,
            PAUSE_OPERATION,
            arguments.idempotency_key,
            idempotency.request_hash_for(PAUSE_OPERATION, arguments),
        )
    assert isinstance(started, idempotency.NewRequest)
    payload = await c2_mcp.client.fails(
        "seller_inquiries_pause", arguments.model_dump(), token=owner_token, code="VERSION_CONFLICT"
    )
    assert payload["retryable"] is True
    assert payload["details"] == {"reason": "in_progress"}


def test_transient_reasons_reach_the_tool_error_payload() -> None:
    busy = error_result(TransientConflict(), "req-c2-busy").structured_content
    assert busy is not None and busy["code"] == "VERSION_CONFLICT" and busy["retryable"] is True
    assert busy["details"] == {"reason": "busy"} and busy["retry_after_seconds"] == 1
    running = error_result(TransientConflict.in_progress(), "req-c2-run").structured_content
    assert running is not None and running["details"] == {"reason": "in_progress"}


def test_no_resume_tool_and_no_route_level_reply_filter() -> None:
    names = {name.lower() for name in ToolRegistry.default().names}
    assert not any("resume" in name for name in names)
    # The quarantine rule is applied by the read query (``queries.get_reply``) for every surface;
    # the MCP layer no longer carries its own copy of it.
    assert not hasattr(mcp_tools, "visible_reply")
