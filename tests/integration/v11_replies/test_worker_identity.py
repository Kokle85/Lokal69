"""Mailbox-worker identity (spec 37.8): one narrow, revocable credential per mailbox binding.

- the token is shown once; only its SHA-256 is stored; the credential is ``mail_worker`` with
  exactly ``mail:ingest``; the mailbox is the sender binding's own account (never chosen by a
  request);
- revoked/expired credentials are ``UNAUTHENTICATED`` (``mail_worker_credential_revoked``), a
  rotated token stops working at once, a revoked mailbox binding is ``FORBIDDEN``;
- an MCP token (``suvmcp_``/``suvdev_``) never authenticates as a worker, and migration
  ``20261007000400`` refuses to bind a mailbox to anything but a ``mail_worker`` credential.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    SENDER_ADDRESS,
    InquiryWorld,
    expect_sqlstate,
    mail_credential,
    mailbox,
)
from tests.integration.v11_replies.support import (
    ingest,
    issue,
    one,
    owner,
    publish,
    request,
    resolve,
    reviewer,
    rows,
    sent,
    system,
)

from suv_deals.api.schemas import MailWorkerHeartbeatRequest
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, Unauthenticated, ValidationFailed
from suv_deals.persistence import credentials_repo, mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import guard_rule_for
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def test_issue_shows_the_token_once_and_stores_only_its_hash(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    token = mw.token
    assert token.startswith("suvmail_") and len(token) == len("suvmail_") + 64
    credential = one(
        seed, "select * from ops.api_credentials where id = %s", mw.issued.credential.credential_id
    )
    assert credential["credential_kind"] == "mail_worker"
    assert list(credential["scopes"]) == ["mail:ingest"]
    assert credential["role"] == "owner" and credential["principal_kind"] == "mcp_client"
    assert credential["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(credential)
    binding = one(seed, "select * from ops.mail_worker_bindings where id = %s", mw.mailbox_id)
    assert binding["credential_id"] == credential["id"]
    assert binding["account_address"] == SENDER_ADDRESS  # the sender binding's own mailbox
    assert binding["sender_binding_id"] == iw.sender_binding_id and binding["state"] == "active"
    audits = rows(
        seed,
        "select action, metadata::text as metadata from ops.audit_events where workspace_id = %s"
        " and action in ('mail_worker.create', 'credential.create')",
        iw.workspace_id,
    )
    assert {a["action"] for a in audits} == {"mail_worker.create", "credential.create"}
    assert all(token not in a["metadata"] and token[8:] not in a["metadata"] for a in audits)
    # The issued object's repr never shows the secret either.
    assert token not in repr(mw.issued)


async def test_the_identity_comes_from_the_token_alone(db: Database, iw: InquiryWorld) -> None:
    mw = await issue(db, iw)
    worker = await resolve(db, mw.token)
    assert worker.workspace_id == iw.workspace_id
    assert worker.mailbox_binding_id == mw.mailbox_id
    assert worker.sender_binding_id == iw.sender_binding_id
    assert worker.credential_id == mw.issued.credential.credential_id
    assert SENDER_ADDRESS not in repr(worker)  # the account address is never in a repr/log
    actor = worker.actor("req-1")
    assert actor.scopes == frozenset({Scope.MAIL_INGEST}) and actor.role == Role.OWNER


async def test_unknown_malformed_and_mcp_tokens_are_refused(db: Database, iw: InquiryWorld) -> None:
    await issue(db, iw)
    for token in ("suvmail_" + "0" * 64, "not-a-token", "", "suvmail_" + "g" * 64):
        with pytest.raises(Unauthenticated):
            await resolve(db, token)
    # A real MCP credential of the same workspace never speaks for a mailbox.
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        mcp = await credentials_repo.issue_credential(
            conn,
            owner(iw.workspace_id),
            principal_id=uuid.uuid4(),
            principal_kind="mcp_client",
            role=Role.REVIEWER,
            scopes=(Scope.DEALS_READ,),
            label="Synthetic MCP client",
            kind="static_bearer",
        )
    with pytest.raises(Unauthenticated) as refused:
        await resolve(db, mcp.token.get_secret_value())
    assert refused.value.details.get("reason") != "mail_worker_credential_revoked"


async def test_a_revoked_credential_is_unauthenticated_and_keeps_the_backlog_reason(
    db: Database, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        assert await credentials_repo.revoke_credential(
            conn, owner(iw.workspace_id), mw.issued.credential.credential_id, reason="synthetic revocation"
        )
    with pytest.raises(Unauthenticated) as refused:
        await resolve(db, mw.token)
    assert refused.value.details == {"reason": "mail_worker_credential_revoked"}


async def test_an_expired_credential_is_unauthenticated(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    async with unit_of_work(db, system(iw.workspace_id)) as conn:
        issued = await mail_workers_repo.issue_mail_worker(
            conn,
            system(iw.workspace_id),
            sender_binding_id=iw.sender_binding_id,
            label="Short-lived synthetic worker",
            lifetime=timedelta(days=1),
        )
    # TEST ARRANGEMENT ONLY: expiry is database-owned and frozen; age the row as the superuser.
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.api_credentials set created_at = now() - interval '2 days',"
            " expires_at = now() - interval '1 minute' where id = %s",
            (issued.credential.credential_id,),
        )
    with pytest.raises(Unauthenticated) as refused:
        await resolve(db, issued.credential.token.get_secret_value())
    assert refused.value.details == {"reason": "mail_worker_credential_revoked"}


async def test_rotation_replaces_the_token_and_keeps_the_mailbox_identity(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    inquiry = sent(seed.conn, iw)
    await publish(db, iw.workspace_id, inquiry)
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        rotated = await mail_workers_repo.rotate_mail_worker_credential(
            conn, owner(iw.workspace_id), mw.mailbox_id, reason="synthetic scheduled rotation"
        )
    assert rotated.mailbox_binding_id == mw.mailbox_id
    assert rotated.mailbox_version == mw.issued.mailbox_version + 1
    with pytest.raises(Unauthenticated):
        await resolve(db, mw.token)
    fresh = await resolve(db, rotated.credential.token.get_secret_value())
    assert fresh.mailbox_binding_id == mw.mailbox_id
    # A request authenticated just before the rotation cannot write after it.
    with pytest.raises(Unauthenticated) as refused:
        await ingest(db, mw, request(mw, inquiry))
    assert refused.value.details == {"reason": "mail_worker_credential_revoked"}
    ok = await ingest(db, mw, request(mw, inquiry), worker=fresh)
    assert ok.ingest_status == "stored"


async def test_a_revoked_mailbox_binding_is_refused(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    mw = await issue(db, iw)
    inquiry = sent(seed.conn, iw)
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, owner(iw.workspace_id), mw.mailbox_id, reason="synthetic decommission"
        )
        assert not await mail_workers_repo.revoke_mail_worker(
            conn, owner(iw.workspace_id), mw.mailbox_id, reason="synthetic decommission"
        )
    with pytest.raises(Unauthenticated):
        await resolve(db, mw.token)  # the credential went with it
    with pytest.raises(Forbidden) as refused:
        await ingest(db, mw, request(mw, inquiry))
    assert refused.value.details == {"reason": "mailbox_binding_revoked"}
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        with pytest.raises(Forbidden):
            await mail_workers_repo.rotate_mail_worker_credential(
                conn, owner(iw.workspace_id), mw.mailbox_id, reason="too late to rotate"
            )


async def test_only_the_owner_or_the_operator_administers_workers(db: Database, iw: InquiryWorld) -> None:
    mcp_owner = ActorContext(
        workspace_id=iw.workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="mcp_client",
        role=Role.OWNER,
        scopes=frozenset(Scope),
        request_id="req-mcp",
    )
    for actor in (mcp_owner, reviewer(iw.workspace_id)):
        async with unit_of_work(db, actor) as conn:
            with pytest.raises(Forbidden):
                await mail_workers_repo.issue_mail_worker(
                    conn, actor, sender_binding_id=iw.sender_binding_id, label="Not allowed"
                )
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        with pytest.raises(NotFound):
            await mail_workers_repo.issue_mail_worker(
                conn, owner(iw.workspace_id), sender_binding_id=uuid.uuid4(), label="Unknown sender"
            )
        with pytest.raises(ValidationFailed):
            await mail_workers_repo.issue_mail_worker(
                conn, owner(iw.workspace_id), sender_binding_id=iw.sender_binding_id, label="bad\nlabel"
            )


async def test_a_foreign_sender_binding_cannot_be_bound(
    db: Database, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        with pytest.raises(NotFound):
            await mail_workers_repo.issue_mail_worker(
                conn,
                owner(iw.workspace_id),
                sender_binding_id=iw_b.sender_binding_id,
                label="Cross workspace",
            )


def test_migration_0400_binds_mailboxes_to_mail_worker_credentials_only(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """A ``static_bearer`` credential with exactly ``mail:ingest`` used to pass the guard."""
    static = mail_credential(seed, iw.workspace_id, credential_kind="static_bearer")
    message = "a mailbox worker binding needs a live credential carrying only mail:ingest"
    with expect_sqlstate("SV003", message), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "insert into ops.mail_worker_bindings (workspace_id, sender_binding_id, credential_id, provider,"
            " account_address, worker_label) values (%s, %s, %s, 'outlook_local', %s, 'static')",
            (iw.workspace_id, iw.sender_binding_id, static, SENDER_ADDRESS),
        )
    rule = guard_rule_for("SV003", message)
    assert rule is not None and rule.reason == "mail_worker_credential_invalid"
    # Rotation onto a static_bearer credential is refused as well; a mail_worker one is fine.
    box = mailbox(seed, iw)
    with expect_sqlstate("SV003", message), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set credential_id = %s, version = version + 1 where id = %s",
            (static, box),
        )
    narrow = mail_credential(seed, iw.workspace_id)
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set credential_id = %s, version = version + 1 where id = %s",
            (narrow, box),
        )
    assert seed.scalar("select credential_id from ops.mail_worker_bindings where id = %s", (box,)) == narrow


async def test_a_credential_revoked_after_authentication_stops_the_request(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: the per-request re-check (``require_active_mailbox``) only compared the
    mailbox's credential id, so a request authenticated just before the owner revoked (or the
    expiry of) the worker credential could still record heartbeats and checkpoints and read the
    binding sync. Every worker operation now refuses it like a revoked token."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)  # the identity is resolved (the request authenticated) here
    # TEST ARRANGEMENT ONLY: the credential expired while its binding stays active (since C1 a
    # revocation through the repository also revokes the binding: see test_reply_hardening.py).
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.api_credentials set created_at = now() - interval '1 hour',"
            " expires_at = now() - interval '1 second' where id = %s",
            (mw.issued.credential.credential_id,),
        )
    beat = MailWorkerHeartbeatRequest.model_validate(
        {
            "schema_version": "1.0",
            "heartbeat": {
                "mailbox_binding_id": str(mw.mailbox_id),
                "worker_id": "synthetic-worker-1",
                "at": datetime.now(UTC).isoformat(),
                "outlook_running": True,
                "mailbox_connected": True,
            },
        }
    )
    actor = mw.worker.actor("req-late")
    async with unit_of_work(db, actor) as conn:
        with pytest.raises(Unauthenticated) as refused:
            await mail_workers_repo.record_heartbeat(conn, mw.worker, beat, request_id="req-late")
    assert refused.value.details == {"reason": "mail_worker_credential_revoked"}
    async with unit_of_work(db, actor) as conn:
        with pytest.raises(Unauthenticated):
            await mail_workers_repo.list_binding_changes(
                conn, mw.worker, cursor=None, secret=b"synthetic-binding-sync-key-0001-abcdefghijklmnop"
            )
    with pytest.raises(Unauthenticated):
        await ingest(db, mw, request(mw, inquiry))
    assert (
        rows(seed, "select id from ops.mail_worker_checkpoints where mailbox_binding_id = %s", mw.mailbox_id)
        == []
    )
    assert rows(seed, "select id from app.seller_replies where inquiry_id = %s", inquiry) == []
