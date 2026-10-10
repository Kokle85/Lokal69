"""Seller-reply signal coalescing never swallows a reply into a signal that can never post
(independent security review r2 of work package C1).

A ``blocked`` outbox event is terminal: nothing moves it back to ``pending`` (the dispatcher
blocks a signal while external notifications are off, which is the safe default before
activation, or while the Slack route is missing/unverified). Coalescing a newer reply into such a
signal therefore muted dot for that inquiry for good, even after the owner enabled the route. A
newer reply now coalesces only into a signal that will still post (``pending`` / ``retry_wait`` /
``sending`` before its attempt); otherwise it emits its own signal, still bounded by the
per-inquiry 24-hour cap.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld
from tests.integration.v11_replies.support import issue, sent
from tests.integration.v11_replies.test_reply_signal_flood import _deliver_all, _reply, _signals

from suv_deals.persistence import replies_repo
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db


def _block_all(seed: Seed, iw: InquiryWorld, code: str = "EXTERNAL_DISABLED") -> None:
    """TEST ARRANGEMENT ONLY: the dispatcher blocked every pending signal (external delivery off)."""
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.outbox set state = 'blocked', blocker_code = %s, last_error_code = %s"
            " where workspace_id = %s and event_type = 'seller.reply.received' and state = 'pending'",
            (code, code, iw.workspace_id),
        )


async def test_a_blocked_signal_never_swallows_a_newer_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    first = await _reply(db, mw, inquiry, 1)
    assert first.signal_status == "emitted"
    _block_all(seed, iw)  # e.g. ALLOW_EXTERNAL_NOTIFICATIONS was still off (the safe default)
    second = await _reply(db, mw, inquiry, 2)
    assert second.signal_status == "emitted"  # its own signal: the blocked one never posts
    assert [s["state"] for s in _signals(seed, iw, inquiry)] == ["blocked", "pending"]
    # A pending signal still coalesces (one post makes dot read every reply of the inquiry).
    third = await _reply(db, mw, inquiry, 3)
    assert third.signal_status == "coalesced"
    assert len(_signals(seed, iw, inquiry)) == 2


async def test_blocked_signals_still_count_toward_the_daily_cap(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """Not coalescing into blocked signals can never flood: the 24-hour cap still bounds them."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    for n in range(replies_repo.MAX_SIGNALS_PER_INQUIRY_24H):
        assert (await _reply(db, mw, inquiry, n)).signal_status == "emitted", n
        if n % 2:
            _block_all(seed, iw)
        else:
            _deliver_all(seed, iw)
    capped = await _reply(db, mw, inquiry, 99)
    assert capped.ingest_status == "stored" and capped.signal_status == "rate_limited"
    assert len(_signals(seed, iw, inquiry)) == replies_repo.MAX_SIGNALS_PER_INQUIRY_24H
