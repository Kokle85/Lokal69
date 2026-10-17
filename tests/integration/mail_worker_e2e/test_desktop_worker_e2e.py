"""The real desktop worker against the real backend app and PostgreSQL (spec 37.6-37.8, 37.10).

Covered: binding sync; claim + report of a send intent (Outbox, then Sent Items evidence
reconciles the uncertain hand-over to ``accepted``); a correlated reply uploaded, stored with its
outbox signal; the same upload again is ``duplicate: true``; cross-mailbox injection is ``403``;
the reply-before-binding-sync race; a revoked credential is ``401`` and the worker keeps its local
backlog; a backend outage keeps the local queue and replays it; a reaped, never-claimed intent is
refused ``intent_expired`` and reconciles the inquiry; a ``not_now`` claim keeps the intent
waiting. Unrelated personal mail never leaves the (fake) mailbox. Nothing is ever sent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from outlook_bridge.api_client import ApiErrorKind, BridgeApiError
from outlook_bridge.credentials import CredentialState
from outlook_bridge.local_queue import BacklogState
from tests.api.conftest import make_settings, running_client
from tests.api.v11_support import (
    MailWorker,
    accepted_send,
    dispatch_intent,
    expire_attempt,
    issue_worker,
    outlook_world,
    owner_actor,
    rows,
)
from tests.integration.db.helpers import Seed
from tests.integration.mail_worker_e2e.harness import Desktop
from tests.integration.v11_inquiries.support import SENDER_ADDRESS, World

from suv_deals.api.app import create_app
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import inquiries_repo, mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


class E2E:
    def __init__(
        self, *, db: Database, seed: Seed, world: World, worker: MailWorker, desktop: Desktop, api: Any
    ):
        self.db = db
        self.seed = seed
        self.world = world
        self.worker = worker
        self.desktop = desktop
        self.api = api  # an async client on the same app (server-side arrangement only)

    def state(self, inquiry_id: UUID) -> str:
        row = self.seed.conn.execute(
            "select state from app.seller_inquiries where id = %s", (inquiry_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])

    def replies(self, inquiry_id: UUID) -> list[dict[str, Any]]:
        return rows(
            self.seed.conn,
            "select id, quarantined, message_type from app.seller_replies where inquiry_id = %s"
            " and conflict_of_reply_id is null",
            inquiry_id,
        )

    def signals(self) -> list[dict[str, Any]]:
        return rows(
            self.seed.conn,
            "select payload from ops.outbox where workspace_id = %s and event_type = 'seller.reply.received'",
            self.world.workspace_id,
        )


async def _e2e(
    db: Database, seed: Seed, tmp_path: Path, name: str, **desktop_options: Any
) -> AsyncIterator[E2E]:
    world = await outlook_world(db, seed, name)
    worker = await issue_worker(db, world)
    # The process gate (SELLER_INQUIRY_MODE) is open, as on a backend that may send.
    settings = make_settings(seller_inquiry_mode="automatic")
    app = create_app(settings, db=db, metrics=AppMetrics(process_metrics=False))
    async with running_client(app) as client:
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
            yield E2E(db=db, seed=seed, world=world, worker=worker, desktop=desktop, api=client)
        finally:
            await desktop.close()


@pytest.fixture
async def e2e(db: Database, seed: Seed, tmp_path: Path) -> AsyncIterator[E2E]:
    async for harness in _e2e(db, seed, tmp_path, "E2E mail worker"):
        yield harness


@pytest.fixture
async def e2e_no_send(db: Database, seed: Seed, tmp_path: Path) -> AsyncIterator[E2E]:
    async for harness in _e2e(
        db, seed, tmp_path, "E2E mail worker (replies only)", send_intents_enabled=False
    ):
        yield harness


# ---------------------------------------------------------------------------------- full route


async def test_binding_sync_send_intent_and_correlated_reply_end_to_end(
    e2e: E2E, db: Database, seed: Seed
) -> None:
    desktop = e2e.desktop
    inquiry_id, intent = await dispatch_intent(e2e.db, e2e.world, e2e.worker.mailbox_id)
    started = await desktop.start()
    assert started.binding_pages >= 1  # the binding of the committed intent reached the worker
    assert started.sends["submitted"] == 1
    assert [c for c in desktop.outlook.send_calls]  # one MailItem.Send on the FAKE Outlook only
    assert e2e.state(inquiry_id) == "uncertain"  # handed to the Outbox: no proof of submission yet
    claims = rows(
        e2e.seed.conn,
        "select metadata from ops.audit_events where target_id = %s and action = 'send_intent.claim'",
        inquiry_id,
    )
    assert [c["metadata"]["proceed"] for c in claims] == [True]
    # Outlook moves the message to Sent Items: the next poll reports the evidence.
    assert desktop.outlook.deliver_outbox(desktop.account) == 1
    confirmed = await desktop.tick(timedelta(seconds=31))
    assert confirmed.sends["confirmed"] == 1
    assert e2e.state(inquiry_id) == "accepted"
    attempt = rows(
        e2e.seed.conn,
        "select outcome, reconciled_outcome from ops.email_delivery_attempts where attempt_id = %s",
        intent.intent_id,
    )[0]
    assert attempt == {"outcome": "uncertain", "reconciled_outcome": "accepted"}

    # The seller answers; unrelated personal mail arrives too.
    desktop.deliver_personal()
    desktop.deliver_reply(sender=str(e2e.world.vehicle.address), in_reply_to=intent.rfc_message_id)
    report = await desktop.tick(timedelta(seconds=150))  # reconciliation due: bindings re-synced
    assert report.uploads["acked"] == 1, report
    stored = e2e.replies(inquiry_id)
    assert len(stored) == 1 and stored[0]["quarantined"] is False
    assert stored[0]["message_type"] == "seller_reply"
    assert e2e.state(inquiry_id) == "replied"
    signals = e2e.signals()
    assert len(signals) == 1
    assert "private.example.invalid" not in json.dumps(signals[0]["payload"])
    uploaded = [c for c in desktop.transport.calls if c[1].endswith("/replies")]
    assert len(uploaded) == 1  # the personal message never left the machine
    assert not rows(e2e.seed.conn, "select id from app.seller_replies where subject like 'Dinner%%'")

    # The same upload again (e.g. a lost acknowledgement): the existing reply, duplicate=true.
    backlog = [r for r in await desktop.backlog() if r.state == BacklogState.ACKED]
    assert len(backlog) == 1
    row = backlog[0]
    ack = await desktop.run(
        lambda: desktop.api.post_reply(  # type: ignore[union-attr]
            row.request_json.encode("utf-8"),
            idempotency_key=row.idempotency_key,
            expected_inquiry_id=inquiry_id,
        )
    )
    assert ack.duplicate is True and ack.reply_id == stored[0]["id"]
    assert len(e2e.replies(inquiry_id)) == 1

    # Cross-mailbox injection: another mailbox's inquiry through this worker's credential.
    other = await outlook_world(db, seed, "E2E mail worker (other mailbox)")
    other_worker = await issue_worker(db, other)
    foreign, foreign_intent = await accepted_send(e2e.api, db, other, other_worker)
    forged = json.loads(row.request_json)
    forged["inquiry_id"] = str(foreign)
    forged["headers"]["in_reply_to"] = foreign_intent.rfc_message_id
    forged["source_message"]["internet_message_id"] = f"<forged-{uuid.uuid4().hex}@synthetic-dealer.example>"
    body = json.dumps(forged, separators=(",", ":")).encode()

    def inject() -> BridgeApiError:
        try:
            desktop.api.post_reply(
                body, idempotency_key="mwr1-forged-cross-mailbox", expected_inquiry_id=foreign
            )  # type: ignore[union-attr]
        except BridgeApiError as exc:
            return exc
        raise AssertionError("a cross-mailbox upload must be refused")

    error = await desktop.run(inject)
    assert error.kind == ApiErrorKind.FORBIDDEN and error.status == 403
    assert e2e.replies(foreign) == []


# ---------------------------------------------------------------------------------- race


async def test_reply_before_binding_sync_is_matched_after_the_next_sync(e2e_no_send: E2E) -> None:
    e2e = e2e_no_send
    desktop = e2e.desktop
    await desktop.start()  # bindings synced: nothing to bind yet
    inquiry_id, intent = await dispatch_intent(e2e.db, e2e.world, e2e.worker.mailbox_id)
    desktop.deliver_reply(sender=str(e2e.world.vehicle.address), in_reply_to=intent.rfc_message_id)
    early = await desktop.tick(timedelta(seconds=5))  # NewMailEx prompt; the binding is unknown here
    assert early.uploads.get("acked", 0) == 0
    assert e2e.replies(inquiry_id) == []  # an unmatched body is never uploaded
    later = await desktop.tick(timedelta(seconds=150))  # the next binding sync + reconciliation
    assert later.binding_pages >= 1
    assert later.uploads["acked"] == 1, later
    stored = e2e.replies(inquiry_id)
    assert len(stored) == 1


# ---------------------------------------------------------------------------------- revocation


async def test_revoked_credential_stops_transmission_and_keeps_the_backlog(e2e_no_send: E2E) -> None:
    e2e = e2e_no_send
    desktop = e2e.desktop
    inquiry_id, intent = await accepted_send(e2e.api, e2e.db, e2e.world, e2e.worker)
    await desktop.start()
    actor = owner_actor(e2e.world.workspace_id)
    async with unit_of_work(e2e.db, actor) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, actor, e2e.worker.mailbox_id, reason="laptop replaced (synthetic)"
        )
    desktop.deliver_reply(sender=str(e2e.world.vehicle.address), in_reply_to=intent.rfc_message_id)
    report = await desktop.tick(timedelta(seconds=150))
    assert report.uploads.get("acked", 0) == 0
    assert any(status == 401 for _m, _p, status in desktop.transport.calls)
    assert e2e.replies(inquiry_id) == []
    backlog = await desktop.backlog()
    assert [r.state for r in backlog] == [BacklogState.PENDING]  # kept for a new credential
    state = await desktop.run(lambda: desktop.credentials.state(desktop.clock.now()))  # type: ignore[union-attr]
    assert state == CredentialState.REJECTED
    calls = len(desktop.transport.calls)
    await desktop.tick(timedelta(seconds=150))
    assert len(desktop.transport.calls) == calls  # transmission stopped: no retry storm


# ---------------------------------------------------------------------------------- outage


async def test_backend_outage_keeps_the_local_queue_and_replays_it(e2e_no_send: E2E) -> None:
    e2e = e2e_no_send
    desktop = e2e.desktop
    inquiry_id, intent = await accepted_send(e2e.api, e2e.db, e2e.world, e2e.worker)
    await desktop.start()
    desktop.transport.down = True
    desktop.deliver_reply(sender=str(e2e.world.vehicle.address), in_reply_to=intent.rfc_message_id)
    report = await desktop.tick(timedelta(seconds=5))
    assert report.uploads["deferred_transient"] == 1, report
    assert e2e.replies(inquiry_id) == []
    assert [r.state for r in await desktop.backlog()] == [BacklogState.PENDING]
    desktop.transport.down = False
    replayed = await desktop.tick(timedelta(minutes=5))
    assert replayed.uploads["acked"] == 1, replayed
    assert len(e2e.replies(inquiry_id)) == 1
    assert len(e2e.signals()) == 1
    assert [r.state for r in await desktop.backlog()] == [BacklogState.ACKED]


# ---------------------------------------------------------------------------------- wire evolution


async def test_reaped_unclaimed_intent_is_refused_expired_and_reconciles(
    e2e: E2E, db_conn: psycopg.Connection
) -> None:
    desktop = e2e.desktop
    inquiry_id, intent = await dispatch_intent(e2e.db, e2e.world, e2e.worker.mailbox_id)
    expire_attempt(db_conn, intent.intent_id)
    await inquiries_repo.reap_expired_attempts(e2e.db, e2e.world.workspace_id)
    assert e2e.state(inquiry_id) == "uncertain"
    report = await desktop.start()
    assert report.sends["refused"] == 1, report
    assert desktop.outlook.send_calls == []  # never sent
    claims = [c for c in desktop.transport.calls if c[1].endswith("/claim")]
    assert claims == []  # refused locally, without a claim
    assert e2e.state(inquiry_id) == "failed_definite"


async def test_not_now_claim_keeps_the_intent_waiting_without_a_report(e2e: E2E) -> None:
    desktop = e2e.desktop
    _inquiry_id, intent = await dispatch_intent(e2e.db, e2e.world, e2e.worker.mailbox_id)
    admin = owner_actor(e2e.world.workspace_id)
    async with unit_of_work(e2e.db, admin) as conn:
        controls = await inquiries_repo.get_controls(conn, admin)
        assert controls is not None
        await inquiries_repo.set_limits(
            conn,
            admin,
            expected_version=controls.version,
            max_per_24h=0,
            max_per_15d=controls.max_per_15d,
            seller_cooldown=controls.seller_cooldown,
            reason="owner lowered the cap (synthetic)",
        )
    report = await desktop.start()
    assert report.sends["deferred_not_now"] == 1, report
    assert desktop.outlook.send_calls == []
    assert [c for c in desktop.transport.calls if c[1].endswith("/report")] == []
    again = await desktop.tick(timedelta(seconds=31))
    assert again.sends.get("deferred_not_now", 0) == 1  # waits locally; no new claim yet
    assert len([c for c in desktop.transport.calls if c[1].endswith("/claim")]) == 1
    del intent
