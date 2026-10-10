"""C1 review (item 5): a newer reply is coalesced only into a signal that cannot have activated dot
yet. A signal whose Slack post may already have happened (``uncertain``, or ``sending`` after its
send attempt began) may have let dot read the inquiry's replies BEFORE the newer one existed; the
dispatcher reconciles such a post as delivered without re-posting it, so coalescing into it would
lose the newer reply's activation for good. The newer reply emits its own signal (still bounded
by ``MAX_SIGNALS_PER_INQUIRY_24H``). Synthetic; nothing is sent.
"""

from __future__ import annotations

import uuid

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld
from tests.integration.v11_replies.support import MailWorld, ingest, issue, request, rows, sent

from suv_deals.persistence import replies_repo
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

AVAILABLE = "Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply."


async def _reply(db: Database, mw: MailWorld, inquiry: uuid.UUID, n: int) -> replies_repo.ReplyIngestOutcome:
    return await ingest(db, mw, request(mw, inquiry, body=f"{AVAILABLE} Nachricht {n}."))


def _set_signal(seed: Seed, iw: InquiryWorld, event_id: object, sql_set: str) -> None:
    """TEST ARRANGEMENT ONLY: the dispatcher's progress on the first signal."""
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            f"update ops.outbox set {sql_set} where workspace_id = %s and event_id = %s",
            (iw.workspace_id, event_id),
        )


def _first_signal(seed: Seed, iw: InquiryWorld, inquiry: uuid.UUID) -> object:
    found = rows(
        seed,
        "select event_id from ops.outbox where workspace_id = %s and event_type = 'seller.reply.received'"
        " and payload ->> 'inquiry_id' = %s order by created_at limit 1",
        iw.workspace_id,
        str(inquiry),
    )
    return found[0]["event_id"]


async def test_an_uncertain_signal_post_never_swallows_a_newer_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    first = await _reply(db, mw, inquiry, 1)
    assert first.signal_status == "emitted"
    # The post's outcome is unknown: Slack may have accepted it and dot may have read the replies.
    _set_signal(
        seed,
        iw,
        _first_signal(seed, iw, inquiry),
        "state = 'uncertain', send_attempted_at = now(), last_error_code = 'SLACK_TIMEOUT'",
    )
    second = await _reply(db, mw, inquiry, 2)
    assert second.signal_status == "emitted"


async def test_a_post_in_flight_never_swallows_a_newer_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert (await _reply(db, mw, inquiry, 1)).signal_status == "emitted"
    _set_signal(
        seed,
        iw,
        _first_signal(seed, iw, inquiry),
        "state = 'sending', lease_owner = 'synthetic-dispatcher', lease_token = gen_random_uuid(),"
        " lease_expires_at = now() + interval '1 minute', last_heartbeat_at = now(),"
        " send_attempted_at = now(), attempts = attempts + 1",
    )
    assert (await _reply(db, mw, inquiry, 2)).signal_status == "emitted"


async def test_a_signal_not_yet_posted_still_coalesces(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert (await _reply(db, mw, inquiry, 1)).signal_status == "emitted"
    event_id = _first_signal(seed, iw, inquiry)
    # Leased by the dispatcher but its send attempt has not begun: the post follows the newer reply.
    _set_signal(
        seed,
        iw,
        event_id,
        "state = 'sending', lease_owner = 'synthetic-dispatcher', lease_token = gen_random_uuid(),"
        " lease_expires_at = now() + interval '1 minute', last_heartbeat_at = now(), attempts = attempts + 1",
    )
    assert (await _reply(db, mw, inquiry, 2)).signal_status == "coalesced"
    # A definitely failed attempt waits for its retry (re-posted later): still coalesced.
    _set_signal(
        seed,
        iw,
        event_id,
        "state = 'retry_wait', lease_owner = null, lease_token = null, lease_expires_at = null,"
        " send_attempted_at = now(), last_error_code = 'SLACK_HTTP_500'",
    )
    assert (await _reply(db, mw, inquiry, 3)).signal_status == "coalesced"
