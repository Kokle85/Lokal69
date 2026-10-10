"""Builders for the mailbox-worker and seller-reply persistence tests (spec 37.6-37.10).

Everything is SYNTHETIC: reserved example domains (``*.example`` / ``*.invalid``), fixture
sources, invented listing references and Message-IDs. Nothing is ever sent or fetched: the
tests drive the persistence layer (``Database(set_role="suv_backend")``, RLS and grants apply)
and arrange history through the superuser ``Seed`` connection of the M2 helpers.

Inquiries are arranged with the v11 database builders (``tests.integration.v11_db.support``):
an accepted (or uncertain) send whose stable outbound Message-ID is
``<inquiry-{id}@synthetic-mail.example>``. The mailbox worker is issued through
``mail_workers_repo.issue_mail_worker`` (a real ``suvmail_`` credential; the token exists only in
the test's memory).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    InquiryWorld,
    dispatch,
    finish_attempt,
    insert_inquiry,
    outbound_message_id,
    queue,
    reserve,
    seller_entity,
    sent_inquiry,
    update_inquiry,
    with_vehicle,
)

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.domain.replies import ReplyIngestRequest
from suv_deals.persistence import mail_workers_repo, replies_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.mail_workers_repo import IssuedMailWorker, PublishedBinding, WorkerIdentity
from suv_deals.persistence.replies_repo import ReplyIngestOptions, ReplyIngestOutcome
from suv_deals.persistence.transactions import unit_of_work

#: Binding-sync cursor signing key (synthetic, >= 32 bytes) and a rotated successor.
SYNC_SECRET = b"synthetic-binding-sync-key-0001-abcdefghijklmnop"
SYNC_SECRET_NEXT = b"synthetic-binding-sync-key-0002-abcdefghijklmnop"
#: Query-snapshot cursor key for the dashboard list reads.
CURSOR_SECRET = b"synthetic-query-cursor-key-0001-abcdefghijklmnop"
DASHBOARD = "https://dashboard.example.invalid"
OPTIONS = ReplyIngestOptions(dashboard_base_url=DASHBOARD)


def owner(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset(Scope),
        request_id="req-owner",
        display_name="Synthetic owner",
    )


def reviewer(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.REVIEWER,
        scopes=ROLE_SCOPES[Role.REVIEWER],
        request_id="req-reviewer",
        display_name="Synthetic reviewer",
    )


def system(workspace_id: UUID) -> ActorContext:
    return ActorContext.system(workspace_id, "req-system")


@dataclass(frozen=True)
class MailWorld:
    """An inquiry world with an issued mailbox worker (identity resolved from its token)."""

    world: InquiryWorld
    issued: IssuedMailWorker
    token: str
    worker: WorkerIdentity

    @property
    def workspace_id(self) -> UUID:
        return self.world.workspace_id

    @property
    def mailbox_id(self) -> UUID:
        return self.issued.mailbox_binding_id


async def issue(db: Database, world: InquiryWorld, *, label: str = "Synthetic desktop worker") -> MailWorld:
    async with unit_of_work(db, system(world.workspace_id)) as conn:
        issued = await mail_workers_repo.issue_mail_worker(
            conn, system(world.workspace_id), sender_binding_id=world.sender_binding_id, label=label
        )
    token = issued.credential.token.get_secret_value()
    return MailWorld(world, issued, token, await resolve(db, token))


async def resolve(db: Database, token: str) -> WorkerIdentity:
    async with db.transaction() as conn:
        return await mail_workers_repo.resolve_worker(conn, token)


async def publish(db: Database, workspace_id: UUID, inquiry_id: UUID) -> PublishedBinding | None:
    """What the send path does after a state change (B1a hook): re-publish the binding."""
    async with unit_of_work(db, system(workspace_id)) as conn:
        return await mail_workers_repo.publish_inquiry_binding(conn, system(workspace_id), inquiry_id)


def another(world: InquiryWorld) -> InquiryWorld:
    """Another vehicle of the same workspace offered by ANOTHER seller (no seller cooldown)."""
    return with_vehicle(world, seller=seller_entity(world.seed, world.workspace_id))


def sent(conn: psycopg.Connection, world: InquiryWorld) -> UUID:
    inquiry, _attempt = sent_inquiry(conn, world)
    return inquiry


def uncertain(conn: psycopg.Connection, world: InquiryWorld) -> tuple[UUID, UUID]:
    """An inquiry whose send timed out after the hand-over (attempt outcome ``uncertain``)."""
    inquiry = insert_inquiry(conn, world)
    reserve(conn, world, inquiry)
    queue(conn, world, inquiry)
    attempt = dispatch(conn, world, inquiry)
    with backend(conn, world.workspace_id):
        finish_attempt(conn, attempt, "uncertain", error_code="PROVIDER_TIMEOUT")
        update_inquiry(conn, inquiry, state="uncertain")
    return inquiry, attempt


def received(minutes_ago: int = 5) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def request(
    mw: MailWorld,
    inquiry_id: UUID,
    *,
    body: str = "Guten Tag, das Fahrzeug ist noch verf\u00fcgbar. Synthetic fixture reply.",
    subject: str = "AW: Anfrage zu Ihrem Fahrzeug (synthetic)",
    binding_version: int = 1,
    from_address: str | None = None,
    in_reply_to: str | bool | None = True,
    references: tuple[str, ...] = (),
    message_id: str | bool | None = True,
    provider_message_id: str | None = None,
    entry_id: str | None = None,
    received_at: datetime | None = None,
    language: str | None = "de",
    mailbox_binding_id: UUID | None = None,
    **extra: Any,
) -> ReplyIngestRequest:
    """A worker upload for ``inquiry_id``; by default a reply to the inquiry's outbound Message-ID."""
    irt = outbound_message_id(inquiry_id) if in_reply_to is True else in_reply_to or None
    mid = f"<{uuid.uuid4().hex}@synthetic-dealer.example>" if message_id is True else message_id or None
    when = received_at or received()
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "inquiry_id": str(inquiry_id),
        "binding_version": binding_version,
        "mailbox_binding_id": str(mailbox_binding_id or mw.mailbox_id),
        "source_message": {
            "internet_message_id": mid,
            "provider_message_id": provider_message_id,
            "outlook_entry_id": entry_id,
            "outlook_store_id": None if entry_id is None else "synthetic-store-1",
            "received_at": when.isoformat(),
        },
        "headers": {
            "from": from_address or mw.world.contact_address,
            "in_reply_to": irt,
            "references": list(references),
        },
        "subject": subject,
        "sanitized_body_text": body,
        "detected_language": language,
        "observed_at": (when + timedelta(seconds=3)).isoformat(),
    }
    document.update(extra)
    return ReplyIngestRequest.model_validate(document)


async def ingest(
    db: Database,
    mw: MailWorld,
    upload: ReplyIngestRequest,
    key: str | None = None,
    *,
    worker: WorkerIdentity | None = None,
    options: ReplyIngestOptions = OPTIONS,
) -> ReplyIngestOutcome:
    """One ingest in its own unit of work (the API route's transaction)."""
    who = worker or mw.worker
    async with unit_of_work(db, who.actor("req-ingest")) as conn:
        return await replies_repo.ingest_reply(
            conn,
            who,
            upload,
            key or f"idem-{uuid.uuid4().hex}",
            datetime.now(UTC),
            request_id="req-ingest",
            options=options,
        )


def rows(seed: Seed, query: str, *params: Any) -> list[dict[str, Any]]:
    cur = seed.conn.execute(query, params)
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def one(seed: Seed, query: str, *params: Any) -> dict[str, Any]:
    found = rows(seed, query, *params)
    assert len(found) == 1, found
    return found[0]


__all__ = [
    "CURSOR_SECRET",
    "DASHBOARD",
    "OPTIONS",
    "SYNC_SECRET",
    "SYNC_SECRET_NEXT",
    "MailWorld",
    "another",
    "ingest",
    "issue",
    "one",
    "owner",
    "publish",
    "received",
    "request",
    "resolve",
    "reviewer",
    "rows",
    "sent",
    "system",
    "uncertain",
]
