"""OPS-04 / F3 (wave D2): the activation-canary reservation gate is VISIBLE where the owner looks.

``inquiries_repo.reserve`` refuses every real seller inquiry with ``activation_canary_incomplete``
until a ``reply_correlated`` canary exists for the configured sender binding's CURRENT version
(tests/integration/v11_inquiries/test_d2_activation_canary_gate.py). The readiness surfaces must
say so instead of claiming that automatic inquiries are possible:

- ``GET /api/inquiry-control``: ``activation_canary_complete`` and ``automatic_inquiries_possible``
  only with it (``process_gate``);
- ``suv-deals inquiries status`` and ``doctor`` (``canary_finding`` names
  ``activation_canary_incomplete``): tests/cli/test_d2_canary_gate_status.py.

Real ASGI app, real PostgreSQL, SYNTHETIC data only; nothing is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from tests.api.conftest import (
    ApiHarness,
    SigningKeys,
    TokenFactory,
    add_members,
    build_test_app,
    make_settings,
    running_client,
)
from tests.api.v11_support import issue_worker, outlook_world
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    SENDER_ACCOUNT,
    SENDER_ADDRESS,
    complete_activation_canary,
)

from suv_deals.api.inquiry_routes import process_gate
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings
from suv_deals.views.inquiries import InquiryControlView

CONTROL = "/api/inquiry-control"


def _configured(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "seller_inquiry_mode": "automatic",
        "seller_email_provider": "outlook_local",
        "seller_email_account_id": SENDER_ACCOUNT,
        "seller_email_from": SENDER_ADDRESS,
    }
    values.update(overrides)
    return make_settings(**values)


async def _control(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, workspace_id: Any, settings: Settings
) -> dict[str, Any]:
    users = add_members(seed, workspace_id)
    async with running_client(build_test_app(settings, keys, db)) as client:
        api = ApiHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=workspace_id,
            metrics=AppMetrics(process_metrics=False),
        )
        response: httpx.Response = await api.get(CONTROL, users.owner)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


@pytest.mark.db
async def test_control_view_withholds_automatic_inquiries_until_the_canary_is_complete(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    world = await outlook_world(db, seed, "D2 canary visibility", canary=False)
    await issue_worker(db, world)
    settings = _configured()
    before = await _control(db, seed, keys, tokens, world.workspace_id, settings)
    # Every other gate is open: process, database mode, kill switch, authorization, sender, caps.
    assert before["process_blockers"] == [] and before["mode"] == "automatic"
    assert before["authorization_status"] == "active" and before["sender_readiness"] == "ready"
    assert before["activation_canary_complete"] is False
    assert before["automatic_inquiries_possible"] is False
    await complete_activation_canary(db, world.workspace_id, world.sender_binding_id)
    after = await _control(db, seed, keys, tokens, world.workspace_id, settings)
    assert after["activation_canary_complete"] is True
    assert after["automatic_inquiries_possible"] is True
    # Without the configured identity the canary of another binding never counts.
    unconfigured = await _control(
        db, seed, keys, tokens, world.workspace_id, make_settings(seller_inquiry_mode="automatic")
    )
    assert unconfigured["sender_readiness"] == "missing"
    assert unconfigured["activation_canary_complete"] is False
    assert unconfigured["automatic_inquiries_possible"] is False


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
        "activation_canary_complete": True,
    }
    values.update(overrides)
    return InquiryControlView.model_validate(values)


def test_the_process_gate_needs_the_completed_activation_canary() -> None:
    assert process_gate(_configured(), _view())["automatic_inquiries_possible"] is True
    gate = process_gate(_configured(), _view(activation_canary_complete=False))
    assert gate["process_blockers"] == () and gate["automatic_inquiries_possible"] is False
    # The field defaults to "not complete": an old producer never opens the gate by omission.
    assert InquiryControlView.model_fields["activation_canary_complete"].default is False
