"""D1 item 4: a dead or cancelled seller-reply signal never mutes the replies coalesced into it.

C1 flood control coalesces a newer reply into the inquiry's still-undelivered signal (dot reads
every reply of the inquiry once that signal posts). When that signal then ends ``dead_letter``
(Slack refused it for good, attempts exhausted) or ``cancelled`` (stale), it never posts: the
coalesced replies would never activate dot. The dispatcher's sweep
(`replies_repo.muted_signal_inquiries` + `replies_repo.reemit_muted_signal`) re-emits ONE new
``seller.reply.received`` signal for the inquiry (for its newest coalesced reply; idempotent by
that reply's deduplication key), bounded by ``MAX_SIGNALS_PER_INQUIRY_24H`` signal events per
rolling 24 hours, which the ingest gate now counts too. Synthetic; nothing is sent.
"""

from __future__ import annotations

import uuid

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld
from tests.integration.v11_replies.support import MailWorld, issue, rows, sent
from tests.integration.v11_replies.test_c1_review_signals import _first_signal, _reply, _set_signal

from suv_deals.domain.replies import ReplySignalStatus
from suv_deals.persistence import replies_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.runtime import system_actor

pytestmark = pytest.mark.db

DASHBOARD = "https://dashboard.synthetic.example"


def _signals(seed: Seed, iw: InquiryWorld, inquiry: uuid.UUID) -> list[dict[str, object]]:
    return rows(
        seed,
        "select event_id, state, dedup_key, payload from ops.outbox where workspace_id = %s"
        " and event_type = 'seller.reply.received' and payload ->> 'inquiry_id' = %s"
        " order by created_at, id",
        iw.workspace_id,
        str(inquiry),
    )


async def _sweep(db: Database, iw: InquiryWorld) -> list[uuid.UUID]:
    """What the dispatcher does each cycle: list the muted inquiries, re-emit one signal each."""
    actor = system_actor(iw.workspace_id, "dispatcher")
    async with unit_of_work(db, actor) as conn:
        muted = await replies_repo.muted_signal_inquiries(conn, actor, limit=20)
    emitted: list[uuid.UUID] = []
    for inquiry_id in muted:
        async with unit_of_work(db, actor) as conn:
            event_id = await replies_repo.reemit_muted_signal(
                conn, actor, inquiry_id, dashboard_base_url=DASHBOARD
            )
        if event_id is not None:
            emitted.append(event_id)
    return emitted


async def _muted_world(
    db: Database, seed: Seed, iw: InquiryWorld, end_state: str
) -> tuple[uuid.UUID, MailWorld, uuid.UUID]:
    """Reply 1 emits the signal; replies 2 and 3 are coalesced into it; the signal then ends
    ``end_state`` without ever posting. Returns ``(inquiry, mail world, newest coalesced reply)``."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert (await _reply(db, mw, inquiry, 1)).signal_status == "emitted"
    assert (await _reply(db, mw, inquiry, 2)).signal_status == "coalesced"
    newest = await _reply(db, mw, inquiry, 3)
    assert newest.signal_status == "coalesced"
    _set_signal(
        seed,
        iw,
        _first_signal(seed, iw, inquiry),
        f"state = '{end_state}', completed_at = now(), last_error_code = 'SLACK_FAILED',"
        " lease_owner = null, lease_token = null, lease_expires_at = null",
    )
    return inquiry, mw, newest.reply_id


@pytest.mark.parametrize("end_state", ["dead_letter", "cancelled"])
async def test_a_dead_signal_with_coalesced_replies_is_re_emitted_once(
    db: Database, seed: Seed, iw: InquiryWorld, end_state: str
) -> None:
    inquiry, _mw, newest = await _muted_world(db, seed, iw, end_state)
    [event_id] = await _sweep(db, iw)
    dead, fresh = _signals(seed, iw, inquiry)
    assert dead["state"] == end_state
    assert fresh["event_id"] == event_id and fresh["state"] == "pending"
    payload = fresh["payload"]
    assert isinstance(payload, dict)
    assert payload["reply_id"] == str(newest) and payload["inquiry_id"] == str(inquiry)
    assert payload["status"] == ReplySignalStatus.RECEIVED.value
    assert fresh["dedup_key"] == f"seller.reply.received:{newest}"
    assert payload["dashboard_url"].startswith(DASHBOARD)
    audited = rows(
        seed,
        "select metadata from ops.audit_events where workspace_id = %s"
        " and action = 'seller_reply.signal_reemit'",
        iw.workspace_id,
    )
    assert len(audited) == 1 and audited[0]["metadata"]["coalesced_replies"] == 2
    # ONE re-emit: later sweeps find nothing to do while the new signal is pending or delivered.
    assert await _sweep(db, iw) == []
    _set_signal(
        seed,
        iw,
        event_id,
        "state = 'delivered', send_attempted_at = now(), provider_accepted_at = now(), completed_at = now()",
    )
    assert await _sweep(db, iw) == []
    assert len(_signals(seed, iw, inquiry)) == 2


async def test_the_re_emitted_signal_dying_too_is_not_re_emitted_again(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """Idempotent by the newest coalesced reply's deduplication key: no loop of re-emits."""
    inquiry, _mw, _newest = await _muted_world(db, seed, iw, "dead_letter")
    [event_id] = await _sweep(db, iw)
    _set_signal(seed, iw, event_id, "state = 'dead_letter', completed_at = now()")
    assert await _sweep(db, iw) == []
    assert len(_signals(seed, iw, inquiry)) == 2


async def test_a_reply_coalesced_into_the_re_emitted_signal_is_carried_by_it(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, mw, _newest = await _muted_world(db, seed, iw, "dead_letter")
    [event_id] = await _sweep(db, iw)
    # A further reply while the re-emitted signal is still pending is coalesced into it.
    assert (await _reply(db, mw, inquiry, 4)).signal_status == "coalesced"
    assert await _sweep(db, iw) == []
    # If THAT signal dies too, its newer coalesced reply gets one more signal (still capped).
    _set_signal(seed, iw, event_id, "state = 'dead_letter', completed_at = now()")
    assert len(await _sweep(db, iw)) == 1
    assert len(_signals(seed, iw, inquiry)) == 3


async def test_nothing_is_re_emitted_without_a_muted_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    # A dead signal without coalesced replies: its own reply is visible in the dashboard; nothing
    # new to activate dot for.
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert (await _reply(db, mw, inquiry, 1)).signal_status == "emitted"
    _set_signal(seed, iw, _first_signal(seed, iw, inquiry), "state = 'dead_letter', completed_at = now()")
    assert await _sweep(db, iw) == []


async def test_coalesced_replies_of_a_delivered_signal_are_covered(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert (await _reply(db, mw, inquiry, 1)).signal_status == "emitted"
    assert (await _reply(db, mw, inquiry, 2)).signal_status == "coalesced"
    _set_signal(
        seed,
        iw,
        _first_signal(seed, iw, inquiry),
        "state = 'delivered', send_attempted_at = now(), provider_accepted_at = now(), completed_at = now()",
    )
    assert await _sweep(db, iw) == []
    assert len(_signals(seed, iw, inquiry)) == 1


async def test_the_re_emit_and_the_ingest_gate_respect_the_per_inquiry_cap(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """Signal EVENTS of the inquiry in the rolling 24 hours count against
    ``MAX_SIGNALS_PER_INQUIRY_24H`` for the re-emit and for a newly ingested reply alike."""
    inquiry, mw, _newest = await _muted_world(db, seed, iw, "dead_letter")
    first = _first_signal(seed, iw, inquiry)
    # TEST ARRANGEMENT ONLY: earlier signals of the inquiry in the window (delivered long before the
    # coalesced replies arrived, so they cover none of them).
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        for n in range(replies_repo.MAX_SIGNALS_PER_INQUIRY_24H - 1):
            seed.conn.execute(
                "insert into ops.outbox (workspace_id, event_type, event_version, aggregate_type,"
                " aggregate_id, payload, payload_hash, dedup_key, state, send_attempted_at,"
                " provider_accepted_at, completed_at, created_at, event_created_at)"
                " select workspace_id, event_type, event_version, aggregate_type, aggregate_id, payload,"
                " payload_hash, dedup_key || ':synthetic-earlier-' || %s, 'delivered',"
                " now() - interval '3 hours', now() - interval '3 hours', now() - interval '3 hours',"
                " now() - interval '4 hours', now() - interval '4 hours'"
                " from ops.outbox where workspace_id = %s and event_id = %s",
                (n, iw.workspace_id, first),
            )
    assert await _sweep(db, iw) == []  # capped: still muted, never over the cap
    fourth = await _reply(db, mw, inquiry, 4)
    assert fourth.signal_status == "rate_limited"  # the gate counts the signal events too
