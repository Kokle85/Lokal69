"""Shared builders for the spec v1.1 API tests (mail-worker API, dashboard inquiry routes, MCP
inquiry tools, the CLI and the mail-worker end-to-end test).

Everything is SYNTHETIC: reserved example domains (``*.example`` / ``*.invalid``), fixture
sources, invented listing references and Message-IDs. Nothing is ever sent: an ``outlook_local``
send intent is only a committed database attempt that a (fake) desktop worker could pick up.

The inquiry world is built through the real repositories (``tests.integration.v11_inquiries``
builders: controls, standing authorization, a verified ``outlook_local`` sender binding, a
qualified vehicle with verified recipient evidence), and the mailbox worker is issued through
``mail_workers_repo.issue_mail_worker`` (a real ``suvmail_`` credential whose token exists only in
the test's memory).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import psycopg
from tests.api.conftest import make_settings, running_client
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import World, build_world, reserve_and_queue, system

from suv_deals.api.app import create_app
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.integrations.email_providers.outlook_local import OutlookSendIntent
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import inquiries_repo, mail_workers_repo, send_intents_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.mail_workers_repo import IssuedMailWorker
from suv_deals.persistence.transactions import unit_of_work

MAIL = "/v1/mail-workers"
WORKER_ID = "desktop-synthetic-1"


@dataclass(frozen=True)
class MailWorker:
    """An issued mailbox worker (the token is synthetic and lives only in memory)."""

    issued: IssuedMailWorker
    token: str

    @property
    def mailbox_id(self) -> UUID:
        return self.issued.mailbox_binding_id

    @property
    def credential_id(self) -> UUID:
        return self.issued.credential.credential_id

    def headers(self, key: str | None = None, **extra: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        headers.update(extra)
        return headers


@asynccontextmanager
async def mail_worker_api(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    """The real backend app (only its ``/v1/mail-workers`` routes are used: no dashboard token is
    ever verified, so no JWKS is fetched) behind an in-process ASGI transport. The process gate
    (``SELLER_INQUIRY_MODE``) is open so the synthetic claims proceed."""
    app = create_app(
        make_settings(seller_inquiry_mode="automatic"), db=db, metrics=AppMetrics(process_metrics=False)
    )
    async with running_client(app) as client:
        yield client


async def outlook_world(db: Database, seed: Seed, name: str) -> World:
    """A workspace in ``automatic`` mode with a verified ``outlook_local`` sender and a vehicle."""
    return await build_world(db, seed, name)


async def issue_worker(db: Database, world: World, *, label: str = "Synthetic desktop worker") -> MailWorker:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        issued = await mail_workers_repo.issue_mail_worker(
            conn, actor, sender_binding_id=world.sender_binding_id, label=label
        )
    return MailWorker(issued=issued, token=issued.credential.token.get_secret_value())


async def dispatch_intent(
    db: Database, world: World, mailbox_id: UUID, *, ttl: timedelta = timedelta(hours=6)
) -> tuple[UUID, OutlookSendIntent]:
    """Reserve, queue and dispatch the world's vehicle on the ``outlook_local`` route."""
    record = await reserve_and_queue(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        dispatched = await send_intents_repo.dispatch_outlook(
            conn,
            actor,
            record.id,
            mailbox_binding_id=mailbox_id,
            message_approval_required=False,
            ttl=ttl,
        )
    assert dispatched.result.outcome == "proceed" and dispatched.intent is not None
    return record.id, dispatched.intent


async def controls_version(db: Database, workspace_id: UUID) -> int:
    actor = system(workspace_id)
    async with unit_of_work(db, actor) as conn:
        controls = await inquiries_repo.get_controls(conn, actor)
    assert controls is not None
    return controls.version


def owner_actor(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset(Scope) - {Scope.MAIL_INGEST},
        request_id="req-v11-owner",
        display_name="Synthetic owner",
    )


def now_utc() -> datetime:
    return datetime.now(UTC)


def claim_body(intent: OutlookSendIntent, mailbox_id: UUID, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": str(intent.intent_id),
        "claim_attempt_id": uuid.uuid4().hex,
        "mailbox_binding_id": str(mailbox_id),
        "worker_id": WORKER_ID,
    }
    body.update(overrides)
    return body


def report_body(
    intent: OutlookSendIntent,
    state: str,
    *,
    refusal: str | None = None,
    observed: str | None = None,
    sent_at: datetime | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    at = now_utc()
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": str(intent.intent_id),
        "inquiry_id": str(intent.inquiry_id),
        "mailbox_binding_id": str(intent.mailbox_binding_id),
        "worker_id": WORKER_ID,
        "state": state,
        "refusal_reason": refusal,
        "account_smtp_address_used": intent.from_address if state != "refused_before_send" else None,
        "observed_internet_message_id": observed
        if observed is not None
        else (intent.rfc_message_id if state == "sent_items_confirmed" else None),
        "outbox_pending": "no" if state in ("sent_items_confirmed", "refused_before_send") else "unknown",
        "sent_items_present": state == "sent_items_confirmed",
        "error_code": None,
        "reported_at": at.isoformat(),
        "sent_at": (sent_at or at).isoformat() if state == "sent_items_confirmed" else None,
    }
    body.update(overrides)
    return body


def reply_body(
    *,
    inquiry_id: UUID,
    mailbox_id: UUID,
    binding_version: int,
    from_address: str,
    in_reply_to: str | None,
    message_id: str | None = None,
    body: str = "Guten Tag, das Fahrzeug ist noch verfuegbar. Synthetic fixture reply only.",
    subject: str = "AW: Anfrage zu Ihrem Fahrzeug (synthetic)",
    references: tuple[str, ...] = (),
    received_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A ``POST /v1/mail-workers/replies`` body in the spec 37.8 v1.0 shape."""
    when = received_at or now_utc() - timedelta(minutes=2)
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "inquiry_id": str(inquiry_id),
        "binding_version": binding_version,
        "mailbox_binding_id": str(mailbox_id),
        "source_message": {
            "internet_message_id": message_id or f"<{uuid.uuid4().hex}@synthetic-dealer.example>",
            "provider_message_id": None,
            "outlook_entry_id": "synthetic-local-locator",
            "outlook_store_id": "synthetic-store-locator",
            "received_at": when.isoformat(),
        },
        "headers": {"from": from_address, "in_reply_to": in_reply_to, "references": list(references)},
        "subject": subject,
        "sanitized_body_text": body,
        "detected_language": "de",
        "attachments": [],
        "observed_at": (when + timedelta(seconds=2)).isoformat(),
    }
    document.update(extra)
    return document


def heartbeat_body(mailbox_id: UUID, **overrides: Any) -> dict[str, Any]:
    at = now_utc()
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "heartbeat": {
            "mailbox_binding_id": str(mailbox_id),
            "worker_id": WORKER_ID,
            "at": at.isoformat(),
            "outlook_running": True,
            "mailbox_connected": True,
            "sync_lag_seconds": 3,
            "pending_intents": 0,
        },
        "last_successful_reconciliation_at": (at - timedelta(seconds=30)).isoformat(),
        "mailbox_last_sync_at": (at - timedelta(seconds=10)).isoformat(),
        "backlog_count": 0,
        "backlog_oldest_age_seconds": None,
        "unresolved_matching_gaps": 0,
        "checkpoints": [],
        "gaps": [],
    }
    body.update(overrides)
    return body


def account_report_body(mailbox_id: UUID, address: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mailbox_binding_id": str(mailbox_id),
        "worker_id": WORKER_ID,
        "reported_at": now_utc().isoformat(),
        "outlook_flavour": "classic",
        "outlook_version": "16.0.17928.20114",
        "stable_account_key": "synthetic-account-key-0001",
        "account_smtp_address": address,
        "account_display_name": "Synthetic Sender",
        "account_type": "imap",
        "security_settings_unchanged": True,
    }
    body.update(overrides)
    return body


def expire_attempt(conn: psycopg.Connection, attempt_id: UUID) -> None:
    """TEST ARRANGEMENT ONLY (superuser, triggers bypassed for one statement): let an intent's
    validity end so the reaper finalises it ``uncertain`` (``LEASE_EXPIRED``)."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts"
            " set send_intent_committed_at = clock_timestamp() - interval '2 hours',"
            " lease_expires_at = clock_timestamp() - interval '1 minute'"
            " where attempt_id = %s",
            (attempt_id,),
        )


async def accepted_send(
    client: httpx.AsyncClient, db: Database, world: World, worker: MailWorker
) -> tuple[UUID, OutlookSendIntent]:
    """Dispatch the world's vehicle, then claim it and report Sent Items evidence through the
    mail-worker API (the inquiry becomes ``accepted`` and its binding is re-published)."""
    inquiry_id, intent = await dispatch_intent(db, world, worker.mailbox_id)
    claim = await client.post(
        f"{MAIL}/send-intents/{intent.intent_id}/claim",
        headers=worker.headers(f"claim-{intent.intent_id}-{uuid.uuid4().hex}"),
        json=claim_body(intent, worker.mailbox_id),
    )
    assert claim.status_code == 200 and claim.json()["proceed"] is True, claim.text
    report = await client.post(
        f"{MAIL}/send-intents/{intent.intent_id}/report",
        headers=worker.headers(f"report-{intent.intent_id}-sent_items_confirmed"),
        json=report_body(intent, "sent_items_confirmed"),
    )
    assert report.status_code == 200, report.text
    return inquiry_id, intent


async def latest_binding_version(client: httpx.AsyncClient, worker: MailWorker, inquiry_id: UUID) -> int:
    response = await client.get(f"{MAIL}/inquiry-bindings", headers=worker.headers())
    assert response.status_code == 200, response.text
    versions = [i["binding_version"] for i in response.json()["items"] if i["inquiry_id"] == str(inquiry_id)]
    return int(max(versions))


async def upload_reply(
    client: httpx.AsyncClient,
    world: World,
    worker: MailWorker,
    inquiry_id: UUID,
    intent: OutlookSendIntent,
    *,
    body: str = "Guten Tag, das Fahrzeug ist noch verfuegbar. Der letzte Preis ist 26.500 EUR. Synthetic.",
    key: str | None = None,
) -> dict[str, Any]:
    """A correlated seller reply uploaded through ``POST /v1/mail-workers/replies``."""
    document = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=worker.mailbox_id,
        binding_version=await latest_binding_version(client, worker, inquiry_id),
        from_address=str(world.vehicle.address),
        in_reply_to=intent.rfc_message_id,
        body=body,
    )
    response = await client.post(
        f"{MAIL}/replies", headers=worker.headers(key or f"mwr1-{uuid.uuid4().hex}"), json=document
    )
    assert response.status_code == 200, response.text
    ack: dict[str, Any] = response.json()
    return ack


def rows(conn: psycopg.Connection, query: str, *params: Any) -> list[dict[str, Any]]:
    cur = conn.execute(query, params)
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def one(conn: psycopg.Connection, query: str, *params: Any) -> dict[str, Any]:
    found = rows(conn, query, *params)
    assert len(found) == 1, found
    return found[0]


def strip_ids(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in ("request_id", "as_of")}


__all__ = [
    "MAIL",
    "WORKER_ID",
    "MailWorker",
    "accepted_send",
    "account_report_body",
    "claim_body",
    "controls_version",
    "dispatch_intent",
    "expire_attempt",
    "heartbeat_body",
    "issue_worker",
    "latest_binding_version",
    "mail_worker_api",
    "now_utc",
    "one",
    "outlook_world",
    "owner_actor",
    "reply_body",
    "report_body",
    "rows",
    "strip_ids",
    "upload_reply",
]
