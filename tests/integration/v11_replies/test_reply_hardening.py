"""Mailbox-worker and reply-read hardening of work package C1 (items 6, 7 and 8).

- Item 6: revoking a ``mail_worker`` credential (``credentials_repo.revoke_credential``) revokes
  the bound ``ops.mail_worker_bindings`` row in the same transaction and tombstones every
  inquiry binding published to it, so a stranded mailbox is visible and unusable.
- Item 7: a quarantined reply's content is withheld inside ``queries.get_reply`` and its
  claim-derived ``availability`` inside ``queries.list_replies`` for everyone but the owner.
- Item 8: the worker-reported health dimensions (sync, backlog, matching gaps) are UNKNOWN while
  the heartbeat is not healthy - never shown as if still current.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld
from tests.integration.v11_replies.support import (
    CURSOR_SECRET,
    MailWorld,
    ingest,
    issue,
    owner,
    request,
    resolve,
    reviewer,
    rows,
    sent,
    system,
)

from suv_deals.api.schemas import MailWorkerHeartbeatRequest, ReplyListQuery
from suv_deals.domain.lifecycle import LagStatus
from suv_deals.errors import Forbidden, Unauthenticated
from suv_deals.persistence import credentials_repo, mail_workers_repo, queries
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

SOLD = "Leider ist das Fahrzeug schon verkauft. Synthetic fixture reply."


# ---------------------------------------------------------------------------------------------
# Item 6: revoking the worker credential revokes the mailbox binding (with tombstones)
# ---------------------------------------------------------------------------------------------


async def test_revoking_the_worker_credential_revokes_the_mailbox(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert mw.issued.published_bindings == 1
    boss = owner(iw.workspace_id)
    async with unit_of_work(db, boss) as conn:
        revoked = await credentials_repo.revoke_credential(
            conn, boss, mw.issued.credential.credential_id, reason="laptop lost (synthetic)"
        )
    assert revoked is True
    box = rows(
        seed,
        "select state, revoke_reason from ops.mail_worker_bindings where id = %s",
        mw.mailbox_id,
    )
    assert box == [{"state": "revoked", "revoke_reason": "laptop lost (synthetic)"}]
    credential = rows(
        seed,
        "select revoked_at is not null as revoked from ops.api_credentials where id = %s",
        mw.issued.credential.credential_id,
    )
    assert credential == [{"revoked": True}]
    latest = rows(
        seed,
        "select binding_state from ops.mail_binding_sync where mailbox_binding_id = %s and inquiry_id = %s"
        " order by sequence desc limit 1",
        mw.mailbox_id,
        inquiry,
    )
    assert latest == [{"binding_state": "tombstoned"}]
    audit = rows(
        seed,
        "select metadata ->> 'tombstoned_bindings' as tombstoned from ops.audit_events"
        " where workspace_id = %s and action = 'mail_worker.revoke' and target_id = %s",
        iw.workspace_id,
        mw.mailbox_id,
    )
    assert audit == [{"tombstoned": "1"}]
    # The token is dead and the stranded mailbox is visible as revoked.
    with pytest.raises((Unauthenticated, Forbidden)):
        await resolve(db, mw.token)
    async with unit_of_work(db, boss) as conn:
        view = await queries.mail_worker_health_view(conn, boss, include_revoked=True)
    assert [(m.mailbox_binding_id, m.binding_state) for m in view.data.mailboxes] == [
        (mw.mailbox_id, "revoked")
    ]
    # Revoking again is a no-op.
    async with unit_of_work(db, boss) as conn:
        again = await credentials_repo.revoke_credential(
            conn, boss, mw.issued.credential.credential_id, reason="again (synthetic)"
        )
    assert again is False


async def test_a_new_worker_can_be_issued_after_revocation(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    sent(seed.conn, iw)
    first = await issue(db, iw)
    boss = owner(iw.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await credentials_repo.revoke_credential(
            conn, boss, first.issued.credential.credential_id, reason="rotate (synthetic)"
        )
    second = await issue(db, iw, label="Replacement desktop worker")
    assert second.mailbox_id != first.mailbox_id and second.issued.published_bindings == 1


# ---------------------------------------------------------------------------------------------
# Item 7: quarantined replies are withheld inside the queries
# ---------------------------------------------------------------------------------------------


async def test_quarantined_reply_content_is_withheld_for_non_owners(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    changed = await ingest(
        db, mw, request(mw, inquiry, from_address="someone-else@unrelated.example", body=SOLD)
    )
    assert changed.ingest_status == "quarantined"
    boss, peer = owner(iw.workspace_id), reviewer(iw.workspace_id)
    async with unit_of_work(db, boss) as conn:
        as_owner = (await queries.get_reply(conn, boss, changed.reply_id)).data
        owner_page = await queries.list_replies(
            conn, boss, ReplyListQuery(inquiry_id=inquiry), secret=CURSOR_SECRET
        )
    async with unit_of_work(db, peer) as conn:
        as_reviewer = (await queries.get_reply(conn, peer, changed.reply_id)).data
        reviewer_page = await queries.list_replies(
            conn, peer, ReplyListQuery(inquiry_id=inquiry), secret=CURSOR_SECRET
        )
    assert as_owner.quarantined and not as_owner.content_withheld and "verkauft" in as_owner.sanitized_body
    assert as_reviewer.quarantined and as_reviewer.content_withheld
    assert as_reviewer.sanitized_body == "" and as_reviewer.subject == "" and as_reviewer.claims is None
    assert "verkauft" not in as_reviewer.model_dump_json()
    [owner_row] = owner_page.data.items
    [reviewer_row] = reviewer_page.data.items
    assert owner_row.availability is not None and not owner_row.content_withheld
    assert reviewer_row.availability is None and reviewer_row.content_withheld


async def test_matched_reply_content_stays_visible_to_reviewers(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    matched = await ingest(db, mw, request(mw, inquiry, body=SOLD))
    peer = reviewer(iw.workspace_id)
    async with unit_of_work(db, peer) as conn:
        view = (await queries.get_reply(conn, peer, matched.reply_id)).data
        page = await queries.list_replies(
            conn, peer, ReplyListQuery(inquiry_id=inquiry), secret=CURSOR_SECRET
        )
    assert not view.content_withheld and "verkauft" in view.sanitized_body
    assert page.data.items[0].availability is not None and not page.data.items[0].content_withheld


# ---------------------------------------------------------------------------------------------
# Item 8: worker-reported health is unknown while the heartbeat is not healthy
# ---------------------------------------------------------------------------------------------


def _beat(mw: MailWorld) -> MailWorkerHeartbeatRequest:
    now = datetime.now(UTC)
    return MailWorkerHeartbeatRequest.model_validate(
        {
            "schema_version": "1.0",
            "heartbeat": {
                "mailbox_binding_id": str(mw.mailbox_id),
                "worker_id": "synthetic-worker-1",
                "at": now.isoformat(),
                "outlook_running": True,
                "mailbox_connected": True,
            },
            "last_successful_reconciliation_at": now.isoformat(),
            "mailbox_last_sync_at": now.isoformat(),
            "backlog_count": 3,
            "backlog_oldest_age_seconds": 120,
            "unresolved_matching_gaps": 2,
        }
    )


async def _health(db: Database, mw: MailWorld) -> mail_workers_repo.MailboxHealth:
    async with unit_of_work(db, mw.worker.actor("req-health")) as conn:
        return await mail_workers_repo.mailbox_health(conn, mw.worker)


async def test_worker_reported_health_is_unknown_without_a_healthy_heartbeat(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    async with unit_of_work(db, mw.worker.actor("req-beat")) as conn:
        await mail_workers_repo.record_heartbeat(conn, mw.worker, _beat(mw), request_id="req-beat")
    fresh = await _health(db, mw)
    assert fresh.heartbeat_status == "healthy"
    assert fresh.mailbox_sync_ok is True and fresh.backlog_count == 3 and fresh.unresolved_matching_gaps == 2
    # TEST ARRANGEMENT ONLY: the worker went silent an hour ago.
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.mail_worker_checkpoints set heartbeat_at = %s where mailbox_binding_id = %s"
            " and store_id_hash = %s and folder_id_hash = %s",
            (
                datetime.now(UTC) - timedelta(hours=1),
                mw.mailbox_id,
                mail_workers_repo.HEALTH_ROW_HASH,
                mail_workers_repo.HEALTH_ROW_HASH,
            ),
        )
    stale = await _health(db, mw)
    assert stale.heartbeat_status != "healthy" and not stale.monitoring_active
    assert stale.mailbox_sync_ok is None and stale.backlog_count is None
    assert stale.unresolved_matching_gaps is None
    for lag in (stale.mailbox_sync_lag, stale.backlog_age):
        assert lag.status == LagStatus.UNKNOWN and lag.reason == mail_workers_repo.HEARTBEAT_NOT_HEALTHY
    # The dashboard view carries the same unknowns.
    actor = system(iw.workspace_id)
    async with unit_of_work(db, actor) as conn:
        view = await queries.mail_worker_health_view(conn, owner(iw.workspace_id))
    [mailbox] = view.data.mailboxes
    assert mailbox.backlog_count is None and mailbox.mailbox_sync_ok is None
