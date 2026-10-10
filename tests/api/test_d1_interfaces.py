"""Work package D1 interface items (items 6 and 7).

- ``GET /api/activation/canary-evidence`` (owner only, read-only): the activation-canary evidence
  of the CONFIGURED sender (the same state ``suv-deals canary status`` and ``doctor`` report) and
  the canaries (ids, states, times; never the target address or its hash). Nothing is prepared or
  sent through the API.
- ``GET /api/inquiry-control`` reports the PROCESS-level gate (``SELLER_INQUIRY_MODE``, the
  process kill switch, the owner's message-approval setting) next to the database controls, and
  ``automatic_inquiries_possible`` only when every gate is open, so the dashboard never claims
  automatic inquiries are possible while the process gate is closed.

Real ASGI app, real PostgreSQL, locally signed Supabase-shaped JWTs, SYNTHETIC data only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tests.api.conftest import (
    ApiHarness,
    SigningKeys,
    TokenFactory,
    Users,
    add_members,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.api.v11_support import controls_version, issue_worker, outlook_world, owner_actor
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    SENDER_ACCOUNT,
    SENDER_ADDRESS,
    World,
    complete_activation_canary,
)

from suv_deals.api.inquiry_routes import process_gate
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import canaries_repo, inquiries_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings
from suv_deals.views.inquiries import InquiryControlView

pytestmark = pytest.mark.db

TARGET = "activation-canary@owner-test.example.invalid"
EVIDENCE = "/api/activation/canary-evidence"


def _configured(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "seller_inquiry_mode": "automatic",
        "seller_email_provider": "outlook_local",
        "seller_email_account_id": SENDER_ACCOUNT,
        "seller_email_from": SENDER_ADDRESS,
    }
    values.update(overrides)
    return make_settings(**values)


def _data(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["data"]


class _Env:
    def __init__(
        self, db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, world: World
    ) -> None:
        self.db, self.seed, self.keys, self.tokens, self.world = db, seed, keys, tokens, world
        self.users: Users = add_members(seed, world.workspace_id)

    async def call(self, settings: Settings, method: str, path: str, who: Any) -> httpx.Response:
        async with running_client(build_test_app(settings, self.keys, self.db)) as client:
            api = ApiHarness(
                client=client,
                tokens=self.tokens,
                users=self.users,
                workspace_id=self.world.workspace_id,
                metrics=AppMetrics(process_metrics=False),
            )
            if method == "POST":
                return await api.post(path, who, {})
            return await api.get(path, who)

    async def get(self, settings: Settings, path: str, who: Any) -> httpx.Response:
        return await self.call(settings, "GET", path, who)


@pytest.fixture
async def env(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, request: pytest.FixtureRequest
) -> AsyncIterator[_Env]:
    world = await outlook_world(db, seed, "D1 interfaces", canary=False)
    await issue_worker(db, world)
    if request.node.get_closest_marker("no_arranged_canary") is None:
        await complete_activation_canary(db, world.workspace_id, world.sender_binding_id)
    yield _Env(db, seed, keys, tokens, world)


# ---------------------------------------------------------------------------------- item 7


async def test_control_view_reports_the_process_gate_next_to_the_database_controls(env: _Env) -> None:
    owner = env.users.owner
    open_view = _data(await env.get(_configured(), "/api/inquiry-control", owner))
    assert open_view["mode"] == "automatic" and open_view["kill_switch"] is False
    assert open_view["process_mode"] == "automatic" and open_view["process_kill_switch"] is False
    assert open_view["process_message_approval_required"] is False
    assert open_view["process_blockers"] == [] and open_view["automatic_inquiries_possible"] is True
    # The database says automatic, but the PROCESS gate is closed: never claimed possible.
    closed = _data(
        await env.get(
            _configured(seller_inquiry_mode="disabled_until_sender_ready"), "/api/inquiry-control", owner
        )
    )
    assert closed["mode"] == "automatic" and closed["process_mode"] == "disabled_until_sender_ready"
    assert closed["process_blockers"] == ["SELLER_INQUIRY_MODE_NOT_AUTOMATIC"]
    assert closed["automatic_inquiries_possible"] is False
    killed = _data(await env.get(_configured(seller_inquiry_kill_switch=True), "/api/inquiry-control", owner))
    assert killed["process_kill_switch"] is True and killed["automatic_inquiries_possible"] is False
    assert killed["process_blockers"] == ["SELLER_INQUIRY_KILL_SWITCH_ON"]
    approval = _data(
        await env.get(
            _configured(seller_inquiry_require_message_approval=True), "/api/inquiry-control", owner
        )
    )
    assert approval["process_message_approval_required"] is True
    assert approval["process_blockers"] == ["MESSAGE_APPROVAL_SETTING_ON"]
    assert approval["automatic_inquiries_possible"] is False


async def test_automatic_inquiries_are_not_possible_while_the_database_gate_is_closed(env: _Env) -> None:
    admin = owner_actor(env.world.workspace_id)
    async with unit_of_work(env.db, admin) as conn:
        await inquiries_repo.pause(
            conn,
            admin,
            expected_version=await controls_version(env.db, env.world.workspace_id),
            reason="Owner pause (synthetic)",
        )
    paused = _data(await env.get(_configured(), "/api/inquiry-control", env.users.reviewer))
    assert paused["process_blockers"] == [] and paused["kill_switch"] is True
    assert paused["automatic_inquiries_possible"] is False
    # Without the configured sender identity nothing is possible either.
    unconfigured = _data(
        await env.get(make_settings(seller_inquiry_mode="automatic"), "/api/inquiry-control", env.users.owner)
    )
    assert (
        unconfigured["sender_readiness"] == "missing"
        and unconfigured["automatic_inquiries_possible"] is False
    )


async def test_automatic_inquiries_are_not_possible_while_the_owner_caps_hold_them(env: _Env) -> None:
    """D1 review: docs/seller_email_activation.md section 8 holds real seller inquiries with
    ``set-limits --max-per-24h 0 --max-per-15d 0`` WHILE the process gate is open for the canary
    step. Every gate is then "open", but no inquiry can be sent: the view must never claim that
    automatic inquiries are possible now."""
    admin = owner_actor(env.world.workspace_id)
    async with unit_of_work(env.db, admin) as conn:
        await inquiries_repo.set_limits(
            conn,
            admin,
            expected_version=await controls_version(env.db, env.world.workspace_id),
            max_per_24h=0,
            max_per_15d=0,
            seller_cooldown=timedelta(days=7),
            reason="Owner holds real inquiries during the canary step (synthetic)",
        )
    held = _data(await env.get(_configured(), "/api/inquiry-control", env.users.owner))
    assert held["process_blockers"] == [] and held["mode"] == "automatic" and held["kill_switch"] is False
    assert held["authorization_status"] == "active" and held["sender_readiness"] == "ready"
    assert held["max_per_24h"] == 0 and held["max_per_15d"] == 0
    assert held["automatic_inquiries_possible"] is False


def _view(**overrides: Any) -> InquiryControlView:
    values: dict[str, Any] = {
        "version": 3,
        "mode": "automatic",
        "kill_switch": False,
        "kill_switch_reason": None,
        "kill_switch_set_at": None,
        "max_per_24h": 2,
        "max_per_15d": 5,
        "seller_cooldown_seconds": 7 * 86_400,
        "used_24h": 0,
        "used_15d": 0,
        "updated_at": datetime(2026, 10, 10, 8, 0, tzinfo=UTC),
        "authorization_status": "active",
        "authorization_version": 1,
        "sender_readiness": "ready",
        "activation_canary_complete": True,  # F3/OPS-04 (wave D2)
    }
    values.update(overrides)
    return InquiryControlView.model_validate(values)


@pytest.mark.parametrize(
    ("overrides", "possible"),
    [
        ({}, True),
        ({"used_24h": 1}, True),
        ({"max_per_24h": 0}, False),
        ({"max_per_15d": 0}, False),
        ({"used_24h": 2}, False),  # the rolling 24-hour cap is used up: nothing can go out now
        ({"used_15d": 5, "used_24h": 0}, False),
    ],
)
def test_the_process_gate_needs_room_under_the_owner_caps(overrides: dict[str, Any], possible: bool) -> None:
    gate = process_gate(_configured(), _view(**overrides))
    assert gate["process_blockers"] == ()
    assert gate["automatic_inquiries_possible"] is possible


# ---------------------------------------------------------------------------------- item 6


@pytest.mark.no_arranged_canary
async def test_canary_evidence_is_owner_only_and_read_only(env: _Env) -> None:
    settings = _configured()
    for who in (env.users.reviewer, env.users.viewer):
        refused = await env.get(settings, EVIDENCE, who)
        assert refused.status_code == 403, refused.text
    none = _data(await env.get(settings, EVIDENCE, env.users.owner))
    assert none["evidence"] == "none" and none["canaries"] == []
    assert none["sender_provider"] == "outlook_local" and none["sender_binding_version"] >= 1
    assert (await env.get(settings, EVIDENCE + "?prepare=1", env.users.owner)).status_code == 422
    posted = await env.call(settings, "POST", EVIDENCE, env.users.owner)
    assert posted.status_code in (404, 405)  # read-only: no canary is prepared or sent here


@pytest.mark.no_arranged_canary
async def test_canary_evidence_follows_the_canary_and_never_shows_the_target(env: _Env) -> None:
    settings = _configured()
    admin = owner_actor(env.world.workspace_id)
    async with unit_of_work(env.db, admin) as conn:
        canary = await canaries_repo.create_canary(
            conn,
            admin,
            sender_binding_id=env.world.sender_binding_id,
            target_address=TARGET,
            purpose="activation evidence rows 4-6 (synthetic)",
        )
    response = await env.get(settings, EVIDENCE, env.users.owner)
    prepared = _data(response)
    assert prepared["evidence"] == "prepared"
    [item] = prepared["canaries"]
    assert (
        item["id"] == str(canary.id)
        and item["state"] == "prepared"
        and item["current_sender_version"] is True
    )
    assert set(item) == {
        "id",
        "provider",
        "sender_binding_version",
        "current_sender_version",
        "state",
        "created_at",
        "outcome_recorded_at",
        "accepted_at",
        "reply_recorded_at",
    }
    assert TARGET not in response.text and canary.target_address_hash not in response.text
    assert "owner-test.example.invalid" not in response.text
    async with unit_of_work(env.db, admin) as conn:
        await canaries_repo.record_canary_outcome(conn, admin, canary.id, outcome="accepted")
        await canaries_repo.record_canary_reply(
            conn,
            admin,
            canary.id,
            reply_message_id="<owner-reply-1@owner-test.example.invalid>",
            in_reply_to=canary.rfc_message_id,
            received_at=canary.created_at,
        )
    complete = _data(await env.get(settings, EVIDENCE, env.users.owner))
    assert complete["evidence"] == "complete" and complete["canaries"][0]["state"] == "reply_correlated"
    # Without the configured sender identity the evidence is never claimed for it.
    unconfigured = _data(await env.get(make_settings(), EVIDENCE, env.users.owner))
    assert unconfigured["evidence"] == "no_sender" and unconfigured["sender_provider"] is None


async def test_canary_evidence_error_shape(env: _Env) -> None:
    refused = await env.get(_configured(), EVIDENCE, env.users.reviewer)
    assert error_of(refused)["code"] == "FORBIDDEN"
