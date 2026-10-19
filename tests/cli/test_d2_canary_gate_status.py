"""OPS-04 / F3 (wave D2): ``inquiries status`` and ``doctor`` name the activation-canary gate.

No real seller inquiry is reserved before a ``reply_correlated`` activation canary exists for the
configured sender binding's CURRENT version (``inquiries_repo.reserve`` refuses with
``activation_canary_incomplete``). ``inquiries status`` must therefore report the canary state and
never ``sending_possible`` without it; ``doctor`` names the refusal code.

SYNTHETIC data only; nothing is contacted or sent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import pytest
from tests.api.v11_support import issue_worker, outlook_world
from tests.cli.conftest import Cli
from tests.cli.test_c2_units import _binding, _canary, _settings
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    SENDER_ACCOUNT,
    SENDER_ADDRESS,
    World,
    complete_activation_canary,
)

from suv_deals.cli_commands.doctor import canary_finding
from suv_deals.persistence.database import Database

T = TypeVar("T")


def _run[T](db_url: str, work: Callable[[Database], Awaitable[T]]) -> T:
    async def main() -> T:
        db = Database(db_url, set_role="suv_backend", min_size=1, max_size=4)
        await db.open()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(main())


@pytest.fixture
def world(db_url: str, seed: Seed) -> Iterator[World]:
    async def build(db: Database) -> World:
        built = await outlook_world(db, seed, "D2 canary gate status", canary=False)
        await issue_worker(db, built)
        return built

    built = _run(db_url, build)
    try:
        yield built
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (built.workspace_id,))


def _status(run_cli: Cli, env: dict[str, str], world: World) -> dict[str, Any]:
    result = run_cli("inquiries", "status", "--workspace", str(world.workspace_id), "--json", env=env)
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.output[result.output.find("{") :])
    return data


@pytest.mark.db
def test_inquiries_status_reports_the_activation_canary_gate(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World
) -> None:
    env = {
        **db_env,
        "SELLER_INQUIRY_MODE": "automatic",
        "SELLER_EMAIL_ACCOUNT_ID": SENDER_ACCOUNT,
        "SELLER_EMAIL_FROM": SENDER_ADDRESS,
    }
    before = _status(run_cli, env, world)
    assert before["sender_binding"] == "usable" and before["authorization"] == "active"
    assert before["activation_canary"] == "activation_canary_incomplete"
    assert before["sending_possible"] is False
    _run(db_url, lambda db: complete_activation_canary(db, world.workspace_id, world.sender_binding_id))
    after = _status(run_cli, env, world)
    assert after["activation_canary"] == "complete" and after["sending_possible"] is True
    # The human output names it too.
    text = run_cli("inquiries", "status", "--workspace", str(world.workspace_id), env=env).output
    assert "activation_canary" in text and "complete" in text
    # Without the configured identity the canary state is "no_sender".
    unconfigured = _status(run_cli, {**db_env, "SELLER_INQUIRY_MODE": "automatic"}, world)
    assert unconfigured["activation_canary"] == "no_sender" and unconfigured["sending_possible"] is False


def test_doctor_names_the_reservation_refusal_while_the_canary_is_incomplete() -> None:
    binding = _binding()
    prepared = _canary(binding)
    for mode in ("disabled_until_sender_ready", "automatic"):
        finding = canary_finding("", _settings(seller_inquiry_mode=mode), [prepared], binding)
        assert "activation_canary_incomplete" in finding.detail, finding
        assert "reserved" in finding.detail
    done = canary_finding("", _settings(), [_canary(binding, state="reply_correlated")], binding)
    assert done.status == "ok" and "activation_canary_incomplete" not in done.detail
    no_sender = canary_finding("", _settings(seller_inquiry_mode="automatic"), [], None)
    assert no_sender.status == "warn" and "activation_canary_incomplete" in no_sender.detail
