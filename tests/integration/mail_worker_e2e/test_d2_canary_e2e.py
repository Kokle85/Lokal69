"""The ``outlook_local`` activation canary end to end (spec 37.10 rows 4-6; F3, wave D2).

The real desktop worker (FakeOutlook at the COM level, the real ``OutlookComSession``) against the
real backend app and PostgreSQL:

1. the owner's ``canary send`` claims the prepared canary and publishes it to the desktop worker in
   ONE transaction (``claim_for_send`` + ``publish_for_desktop``);
2. the worker pulls it, claims it again, sends the fixed canary text ONCE to the owner-controlled
   address configured on ITS machine (the backend only knows the hash), reports the Outbox
   hand-over (``uncertain``) and then Sent Items evidence (``accepted``);
3. the owner replies from that mailbox; the worker uploads the reply's headers and the sender's
   hash only, and the canary is ``reply_correlated``: the activation evidence is ``complete``.

Refusals: a worker without (or with another) configured target refuses the canary before any
``.Send`` and the canary fails honestly; a second worker id cannot claim a claimed canary; the
canary routes never serve another mailbox's canary. Synthetic ``example.invalid`` data only;
nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.api.conftest import make_settings, running_client
from tests.api.v11_support import MailWorker, issue_worker, outlook_world, owner_actor
from tests.integration.db.helpers import Seed
from tests.integration.mail_worker_e2e.harness import API_BASE, Desktop
from tests.integration.v11_inquiries.support import SENDER_ADDRESS, World

from suv_deals.api.app import create_app
from suv_deals.domain.canary import canary_body, canary_subject
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import canaries_repo, sender_bindings_repo
from suv_deals.persistence.canaries_repo import CanaryRecord
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

TARGET = "owner-test-mailbox@example.invalid"  # SYNTHETIC owner-controlled test address


async def _prepare_and_publish(db: Database, world: World) -> CanaryRecord:
    """``canary prepare`` then the owner's ``canary send`` (claim + publish, one transaction)."""
    actor = owner_actor(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        prepared = await canaries_repo.create_canary(
            conn,
            actor,
            sender_binding_id=world.sender_binding_id,
            target_address=TARGET,
            purpose="SYNTHETIC activation canary (test)",
        )
    token = uuid.uuid4().hex
    async with unit_of_work(db, actor) as conn:
        await canaries_repo.claim_for_send(
            conn, actor, prepared.id, expected_version=prepared.version, send_token=token
        )
        return await canaries_repo.publish_for_desktop(conn, actor, prepared.id, send_token=token)


async def _canary(db: Database, world: World, canary_id: uuid.UUID) -> CanaryRecord:
    actor = owner_actor(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await canaries_repo.get_canary(conn, actor, canary_id)


async def _evidence(db: Database, world: World) -> str:
    actor = owner_actor(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        records = await canaries_repo.list_canaries(conn, actor)
        sender = await sender_bindings_repo.get_binding(conn, actor, world.sender_binding_id)
    return canaries_repo.evidence_state(records, sender)[0]


class Harness:
    def __init__(self, db: Database, world: World, worker: MailWorker, desktop: Desktop) -> None:
        self.db = db
        self.world = world
        self.worker = worker
        self.desktop = desktop


async def _harness(
    db: Database, seed: Seed, tmp_path: Path, name: str, **desktop_options: Any
) -> AsyncIterator[Harness]:
    world = await outlook_world(db, seed, name, canary=False)  # these tests make their own canaries
    worker = await issue_worker(db, world)
    app = create_app(
        make_settings(seller_inquiry_mode="automatic"), db=db, metrics=AppMetrics(process_metrics=False)
    )
    async with running_client(app):
        desktop = Desktop(
            app=app,
            loop=asyncio.get_running_loop(),
            mailbox_binding_id=worker.mailbox_id,
            token=worker.token,
            account_address=SENDER_ADDRESS,
            tmp_path=tmp_path,
            **desktop_options,
        )
        await desktop.build()
        try:
            yield Harness(db, world, worker, desktop)
        finally:
            await desktop.close()


@pytest.fixture
async def canary_e2e(db: Database, seed: Seed, tmp_path: Path) -> AsyncIterator[Harness]:
    async for harness in _harness(db, seed, tmp_path, "E2E canary", canary_target_address=TARGET):
        yield harness


@pytest.fixture
async def no_target(db: Database, seed: Seed, tmp_path: Path) -> AsyncIterator[Harness]:
    async for harness in _harness(db, seed, tmp_path, "E2E canary without target"):
        yield harness


async def test_outlook_local_canary_is_sent_confirmed_and_correlated(canary_e2e: Harness) -> None:
    h = canary_e2e
    desktop = h.desktop
    canary = await _prepare_and_publish(h.db, h.world)
    assert canary.state == "uncertain" and canary.outcome_evidence["phase"] == "published"
    assert await _evidence(h.db, h.world) == "uncertain"

    started = await desktop.start()
    assert started.sends["canary_submitted"] == 1, started.sends
    assert desktop.outlook.send_calls == [canary_subject(canary.id)]  # ONE .Send, the fixed text
    sent = desktop.outlook.folder(desktop.account, "outbox")._items[0]
    assert sent.Body.replace("\r\n", "\n") == canary_body(canary.id)
    assert [r.Address for r in sent.Recipients._items] == [TARGET]
    record = await _canary(h.db, h.world, canary.id)
    assert record.state == "uncertain" and record.outcome_evidence["phase"] == "submitted"
    claims = [c for c in desktop.transport.calls if c[1].endswith("/claim")]
    assert len(claims) == 1

    # Outlook moves it to Sent Items: the next poll reports the evidence -> accepted.
    assert desktop.outlook.deliver_outbox(desktop.account) == 1
    confirmed = await desktop.tick(timedelta(seconds=31))
    assert confirmed.sends["canary_confirmed"] == 1, confirmed.sends
    record = await _canary(h.db, h.world, canary.id)
    assert record.state == "accepted" and record.outcome_evidence["message_id_matches"] is True
    again = await desktop.tick(timedelta(seconds=31))
    assert again.sends.get("canary_submitted", 0) == 0  # never a second .Send
    assert len(desktop.outlook.send_calls) == 1

    # The owner answers from the target mailbox; unrelated personal mail arrives too.
    desktop.deliver_personal()
    desktop.deliver_reply(sender=TARGET, in_reply_to=canary.rfc_message_id, subject="Re: activation test")
    await desktop.tick(timedelta(seconds=150))
    await desktop.tick(timedelta(seconds=31))
    record = await _canary(h.db, h.world, canary.id)
    assert record.state == "reply_correlated", record.state
    assert record.reply_evidence["from_matches_target"] is True
    assert await _evidence(h.db, h.world) == "complete"
    # Neither the canary reply nor the personal mail was uploaded as a seller reply.
    assert not [c for c in desktop.transport.calls if c[1].endswith("/replies")]


async def test_worker_without_the_target_refuses_before_any_send(no_target: Harness) -> None:
    h = no_target
    canary = await _prepare_and_publish(h.db, h.world)
    report = await h.desktop.start()
    assert report.sends["canary_refused"] == 1, report.sends
    assert h.desktop.outlook.send_calls == []
    assert not [c for c in h.desktop.transport.calls if c[1].endswith("/claim")]
    record = await _canary(h.db, h.world, canary.id)
    assert record.state == "failed"
    assert record.outcome_evidence["refusal_reason"] == "intent_invalid"
    assert record.outcome_evidence["error_code"] == "CANARY_TARGET_NOT_CONFIGURED"


async def test_a_second_worker_id_cannot_claim_and_foreign_canaries_are_hidden(
    canary_e2e: Harness, db: Database, seed: Seed
) -> None:
    h = canary_e2e
    canary = await _prepare_and_publish(h.db, h.world)
    desktop = h.desktop
    api = desktop.api
    assert api is not None
    first = await desktop.run(lambda: api.claim_canary(canary.id))
    assert first.proceed is True
    retry = await desktop.run(lambda: api.claim_canary(canary.id))
    assert retry.proceed is True  # the same worker id (a lost answer) may claim again

    # Another store/installation of the same credential is another worker id: refused.
    from outlook_bridge.api_client import BridgeApiClient, ClientIdentity  # noqa: PLC0415

    other = BridgeApiClient(
        API_BASE,
        token_provider=lambda: h.worker.token,
        identity=ClientIdentity(h.worker.mailbox_id, "desktop-e2e-1", "s00000000000000ff"),
        transport=desktop.transport,
    )
    try:
        second = await desktop.run(lambda: other.claim_canary(canary.id))
    finally:
        await desktop.run(other.close)
    assert second.proceed is False and second.refusal_reason == "intent_invalid"

    # Another workspace's mailbox never sees (or claims) this canary.
    foreign = await outlook_world(db, seed, "E2E canary (other mailbox)")
    foreign_worker = await issue_worker(db, foreign)
    stranger = BridgeApiClient(
        API_BASE,
        token_provider=lambda: foreign_worker.token,
        identity=ClientIdentity(foreign_worker.mailbox_id, "desktop-e2e-2", "s00000000000000fe"),
        transport=desktop.transport,
    )
    try:
        batch = await desktop.run(stranger.fetch_canary_intents)
        assert batch.intents == ()
        from outlook_bridge.api_client import ApiErrorKind, BridgeApiError  # noqa: PLC0415

        def forbidden() -> BridgeApiError:
            try:
                stranger.claim_canary(canary.id)
            except BridgeApiError as exc:
                return exc
            raise AssertionError("a foreign canary claim must be refused")

        error = await desktop.run(forbidden)
        assert error.kind == ApiErrorKind.FORBIDDEN
    finally:
        await desktop.run(stranger.close)
