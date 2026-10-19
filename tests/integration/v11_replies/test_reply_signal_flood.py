"""Seller-reply signal flood control (C1 item 5).

At most ONE undelivered ``seller.reply.received`` signal per inquiry (a second matched reply while
one is still pending is ``coalesced``: dot reads every reply of the inquiry anyway), at most
``MAX_SIGNALS_PER_INQUIRY_24H`` signal-emitting replies per inquiry per rolling 24 hours (then
``rate_limited``), and at most ``MAX_NEW_REPLIES_PER_MAILBOX_PER_HOUR`` NEW stored replies per
mailbox worker credential per rolling hour (then 429, the upload stays queued on the worker).
Every reply is still stored and visible; ``signal_status`` records what happened. A fixture
listing's signal is stored blocked (never delivered).

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import uuid

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld, with_vehicle
from tests.integration.v11_replies.support import (
    CURSOR_SECRET,
    MailWorld,
    another,
    ingest,
    issue,
    owner,
    publish,
    request,
    reviewer,
    rows,
    sent,
)

from suv_deals.api.schemas import ReplyListQuery
from suv_deals.errors import RateLimited
from suv_deals.persistence import queries, replies_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

AVAILABLE = "Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply."


def _signals(seed: Seed, iw: InquiryWorld, inquiry: uuid.UUID) -> list[dict[str, object]]:
    return rows(
        seed,
        "select event_id, state, is_fixture from ops.outbox where workspace_id = %s"
        " and event_type = 'seller.reply.received' and payload ->> 'inquiry_id' = %s order by created_at",
        iw.workspace_id,
        str(inquiry),
    )


def _deliver_all(seed: Seed, iw: InquiryWorld) -> None:
    """TEST ARRANGEMENT ONLY: the dispatcher delivered every pending signal (Slack accepted it)."""
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.outbox set state = 'delivered', send_attempted_at = now(),"
            " provider_accepted_at = now() where workspace_id = %s"
            " and event_type = 'seller.reply.received' and state = 'pending'",
            (iw.workspace_id,),
        )


async def _reply(db: Database, mw: MailWorld, inquiry: uuid.UUID, n: int) -> replies_repo.ReplyIngestOutcome:
    return await ingest(db, mw, request(mw, inquiry, body=f"{AVAILABLE} Nachricht {n}."))


async def test_a_pending_signal_coalesces_further_replies(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    first = await _reply(db, mw, inquiry, 1)
    second = await _reply(db, mw, inquiry, 2)
    third = await _reply(db, mw, inquiry, 3)
    assert first.ingest_status == second.ingest_status == third.ingest_status == "stored"
    assert (first.signal_status, second.signal_status, third.signal_status) == (
        "emitted",
        "coalesced",
        "coalesced",
    )
    assert len(_signals(seed, iw, inquiry)) == 1  # ONE undelivered signal for the inquiry
    # Every reply is stored and visible, with what happened to its signal.
    for role in (owner, reviewer):
        actor = role(iw.workspace_id)
        async with unit_of_work(db, actor) as conn:
            page = await queries.list_replies(
                conn, actor, ReplyListQuery(inquiry_id=inquiry), secret=CURSOR_SECRET
            )
        assert {i.reply_id: i.signal_status for i in page.data.items} == {
            first.reply_id: "emitted",
            second.reply_id: "coalesced",
            third.reply_id: "coalesced",
        }
    audited = rows(
        seed,
        "select metadata ->> 'signal_status' as status from ops.audit_events where workspace_id = %s"
        " and target_id = %s order by occurred_at",
        iw.workspace_id,
        second.reply_id,
    )
    assert [a["status"] for a in audited] == ["coalesced"]

    # Once the pending signal is delivered, the next reply signals again.
    _deliver_all(seed, iw)
    fourth = await _reply(db, mw, inquiry, 4)
    assert fourth.signal_status == "emitted"
    assert [s["state"] for s in _signals(seed, iw, inquiry)] == ["delivered", "pending"]


async def test_signals_per_inquiry_are_capped_per_rolling_day(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    for n in range(replies_repo.MAX_SIGNALS_PER_INQUIRY_24H):
        outcome = await _reply(db, mw, inquiry, n)
        assert outcome.signal_status == "emitted", n
        _deliver_all(seed, iw)
    capped = await _reply(db, mw, inquiry, 99)
    assert capped.ingest_status == "stored" and capped.signal_status == "rate_limited"
    assert len(_signals(seed, iw, inquiry)) == replies_repo.MAX_SIGNALS_PER_INQUIRY_24H
    stored = rows(
        seed,
        "select signal_status from app.seller_replies where id = %s",
        capped.reply_id,
    )
    assert stored == [{"signal_status": "rate_limited"}]
    # Another inquiry of the same workspace is not affected by this inquiry's cap.
    other_world = another(iw)  # another seller (no seller cooldown)
    other = sent(seed.conn, other_world)
    await publish(db, iw.workspace_id, other)  # what the send path does after a state change
    from_other = await ingest(db, mw, request(mw, other, from_address=other_world.contact_address))
    assert from_other.signal_status == "emitted"


async def test_a_fixture_listing_signal_is_stored_blocked(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    fixture_world = with_vehicle(iw, fixture=True)
    inquiry = sent(seed.conn, fixture_world)
    mw = await issue(db, fixture_world)
    outcome = await ingest(db, mw, request(mw, inquiry, from_address=fixture_world.contact_address))
    assert outcome.ingest_status == "stored" and outcome.signal_status == "emitted"
    signals = _signals(seed, iw, inquiry)
    assert len(signals) == 1 and signals[0]["is_fixture"] is True and signals[0]["state"] == "blocked"


async def test_a_quarantined_reply_never_signals(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    changed = await ingest(db, mw, request(mw, inquiry, from_address="someone-else@unrelated.example"))
    assert changed.ingest_status == "quarantined" and changed.signal_status == "not_applicable"
    assert _signals(seed, iw, inquiry) == []


async def test_new_replies_per_mailbox_worker_are_rate_limited(
    db: Database, seed: Seed, iw: InquiryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replies_repo, "MAX_NEW_REPLIES_PER_MAILBOX_PER_HOUR", 2)
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    first = request(mw, inquiry, body=f"{AVAILABLE} 1")
    await ingest(db, mw, first, "idem-volume-1")
    await ingest(db, mw, request(mw, inquiry, body=f"{AVAILABLE} 2"))
    with pytest.raises(RateLimited) as exc:
        await ingest(db, mw, request(mw, inquiry, body=f"{AVAILABLE} 3"))
    assert exc.value.details["reason"] == "mail_worker_ingest_volume"
    assert exc.value.retry_after_seconds is not None and exc.value.retry_after_seconds >= 1
    # A replay of an already stored upload stores nothing and is never limited.
    replay = await ingest(db, mw, first, "idem-volume-1")
    assert replay.duplicate
    stored = rows(seed, "select id from app.seller_replies where inquiry_id = %s", inquiry)
    assert len(stored) == 2
