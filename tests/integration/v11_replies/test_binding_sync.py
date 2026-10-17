"""Inquiry-binding sync for the mailbox worker (spec 37.8 ``GET /v1/mail-workers/inquiry-bindings``).

- items follow the desktop wire contract; an uncertain send carries its send-intent Message-ID;
- the opaque cursor is signed, bound to the worker's mailbox, advances only to what was
  returned (``has_more`` pages), is echoed when nothing changed, and survives key rotation;
- publication is idempotent; versions only grow; a tombstone is final and carries no payload;
- another mailbox's changes are never visible; a reply named against an unpublished binding
  version is refused until the binding appears (reply-before-binding-sync).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import InquiryWorld, outbound_message_id, sender_binding, with_vehicle
from tests.integration.v11_replies.support import (
    SYNC_SECRET,
    SYNC_SECRET_NEXT,
    MailWorld,
    another,
    ingest,
    issue,
    publish,
    request,
    rows,
    sent,
    system,
    uncertain,
)

from suv_deals.api.schemas import MailWorkerBindingPage
from suv_deals.domain.replies import InquiryBindingState
from suv_deals.errors import AppError, Forbidden, ValidationFailed, VersionConflict
from suv_deals.persistence import mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def changes(
    db: Database,
    mw: MailWorld,
    cursor: str | None = None,
    *,
    limit: int = 100,
    secret: bytes | list[bytes] = SYNC_SECRET,
) -> MailWorkerBindingPage:
    async with unit_of_work(db, mw.worker.actor("req-sync")) as conn:
        return await mail_workers_repo.list_binding_changes(
            conn, mw.worker, cursor=cursor, limit=limit, secret=secret
        )


async def test_issue_publishes_transmitted_inquiries_in_the_wire_shape(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    assert mw.issued.published_bindings == 1
    page = await changes(db, mw)
    (item,) = page.items
    assert page.schema_version == "1.0" and not page.has_more and page.next_cursor is not None
    assert item.inquiry_id == inquiry and item.binding_version == 1
    assert item.mailbox_binding_id == mw.mailbox_id and item.state == InquiryBindingState.ACTIVE
    assert item.outbound_message_ids == (outbound_message_id(inquiry),)
    assert item.send_intent_message_ids == ()
    assert item.verified_seller_aliases == (iw.contact_address.lower(),)
    assert iw.vehicle.reference in item.listing_references
    assert iw.vehicle.url in item.listing_urls
    assert item.listing_id == iw.listing_id and item.is_canary is False
    # Nothing new: the cursor is echoed (the worker keeps its position).
    again = await changes(db, mw, page.next_cursor)
    assert again.items == () and again.next_cursor == page.next_cursor and not again.has_more


async def test_an_uncertain_send_publishes_its_send_intent_message_id(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    inquiry, _attempt = uncertain(seed.conn, iw)
    published = await publish(db, iw.workspace_id, inquiry)
    assert published is not None and published.created and published.state == InquiryBindingState.UNCERTAIN
    (item,) = (await changes(db, mw)).items
    assert item.state == InquiryBindingState.UNCERTAIN
    assert item.outbound_message_ids == ()
    assert item.send_intent_message_ids == (outbound_message_id(inquiry),)


async def test_publication_is_idempotent_and_versions_only_grow(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    again = await publish(db, iw.workspace_id, inquiry)
    assert again is not None and not again.created and again.binding_version == 1
    # A seller contact verified later changes the payload -> a new version.
    other = with_vehicle(iw)
    assert other.contact_address != iw.contact_address
    newer = await publish(db, iw.workspace_id, inquiry)
    assert newer is not None and newer.created and newer.binding_version == 2
    assert newer.sequence > again.sequence
    items = (await changes(db, mw)).items
    assert [i.binding_version for i in items] == [1, 2]
    assert other.contact_address.lower() in items[1].verified_seller_aliases


async def test_cursor_advances_only_to_what_was_returned(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    mw = await issue(db, iw)
    # Two sends per rolling 24 hours is the database ceiling (spec 37.5).
    first_inquiry = sent(seed.conn, iw)
    second_inquiry = sent(seed.conn, another(iw))
    for inquiry in (first_inquiry, second_inquiry):
        await publish(db, iw.workspace_id, inquiry)
    first = await changes(db, mw, limit=1)
    assert [i.inquiry_id for i in first.items] == [first_inquiry] and first.has_more
    second = await changes(db, mw, first.next_cursor, limit=1)
    assert [i.inquiry_id for i in second.items] == [second_inquiry] and not second.has_more
    # A change published after the last page appears exactly once after the stored cursor
    # (a newly verified address of the first seller is a new binding version).
    with_vehicle(iw)
    await publish(db, iw.workspace_id, first_inquiry)
    third = await changes(db, mw, second.next_cursor)
    assert [(i.inquiry_id, i.binding_version) for i in third.items] == [(first_inquiry, 2)]
    assert (await changes(db, mw, third.next_cursor)).items == ()
    # Re-reading from an older cursor replays (the worker applies versions idempotently).
    replay = await changes(db, mw, first.next_cursor)
    assert [(i.inquiry_id, i.binding_version) for i in replay.items] == [
        (second_inquiry, 1),
        (first_inquiry, 2),
    ]
    for bad in (0, 101, True):
        with pytest.raises(ValidationFailed):
            await changes(db, mw, limit=bad)


async def test_the_cursor_is_signed_bound_to_the_mailbox_and_survives_key_rotation(
    db: Database, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    sent(seed.conn, iw)
    mw = await issue(db, iw)
    cursor = (await changes(db, mw)).next_cursor
    assert cursor is not None and cursor.startswith("mbs1.")
    prefix, sequence, mac = cursor.split(".")
    tampered = f"{prefix}.{int(sequence) - 1}.{mac}"
    for bad in (tampered, "mbs1.x.y", "garbage", cursor + "0"):
        with pytest.raises(ValidationFailed) as refused:
            await changes(db, mw, bad)
        assert refused.value.details.get("cursor") in ("tampered", "malformed")
    # Another worker (another workspace) cannot use this mailbox's cursor.
    other = await issue(db, iw_b)
    with pytest.raises(ValidationFailed):
        await changes(db, other, cursor)
    # Key rotation: the old key still verifies; new cursors are signed with the first key.
    rotated = await changes(db, mw, cursor, secret=[SYNC_SECRET_NEXT, SYNC_SECRET])
    assert rotated.next_cursor == cursor
    with pytest.raises(AppError):
        await changes(db, mw, cursor, secret=b"short")


async def test_a_tombstone_is_final_and_carries_identity_only(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    async with unit_of_work(db, system(iw.workspace_id)) as conn:
        tomb = await mail_workers_repo.tombstone_inquiry_binding(
            conn, system(iw.workspace_id), inquiry, reason="synthetic owner withdrawal"
        )
    assert tomb is not None and tomb.state == InquiryBindingState.TOMBSTONED and tomb.binding_version == 2
    again = await publish(db, iw.workspace_id, inquiry)
    assert again is not None and not again.created and again.state == InquiryBindingState.TOMBSTONED
    items = (await changes(db, mw)).items
    final = items[-1]
    assert final.state == InquiryBindingState.TOMBSTONED and final.binding_version == 2
    assert final.outbound_message_ids == () and final.verified_seller_aliases == ()
    stored = rows(
        seed,
        "select payload from ops.mail_binding_sync where inquiry_id = %s order by binding_version",
        inquiry,
    )
    assert stored[-1]["payload"] == {}
    # A reply naming the tombstoned inquiry is refused (even against the old version).
    with pytest.raises(Forbidden) as refused:
        await ingest(db, mw, request(mw, inquiry, binding_version=1))
    assert refused.value.details == {"reason": "inquiry_binding_tombstoned"}


async def test_another_mailbox_never_sees_these_bindings(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    # A second sender account of the same workspace with its own worker.
    second_sender = sender_binding(
        seed,
        iw.workspace_id,
        account_id="synthetic-outlook-account-2",
        from_address="second@synthetic-mail.example",
    )
    other = await issue(db, replace(iw, sender_binding_id=second_sender))
    assert (await changes(db, other)).items == ()
    assert [i.inquiry_id for i in (await changes(db, mw)).items] == [inquiry]


async def test_reply_before_binding_sync_waits_for_the_binding(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """The worker saw the reply before the backend published the binding version it names:
    the upload is refused (``409 binding_version_unpublished``), nothing is stored, and the
    same upload succeeds once the version appears in the worker's sync."""
    mw = await issue(db, iw)
    inquiry, _attempt = uncertain(seed.conn, iw)
    upload = request(mw, inquiry, binding_version=1)
    with pytest.raises(VersionConflict) as early:
        await ingest(db, mw, upload, "idem-before-sync-1")
    assert early.value.details.get("reason") == "binding_version_unpublished"
    assert rows(seed, "select id from app.seller_replies where inquiry_id = %s", inquiry) == []
    page = await changes(db, mw)
    assert page.items == () and page.next_cursor is None
    await publish(db, iw.workspace_id, inquiry)
    page = await changes(db, mw)
    assert [(i.inquiry_id, i.binding_version) for i in page.items] == [(inquiry, 1)]
    outcome = await ingest(db, mw, upload, "idem-before-sync-1")
    assert outcome.ingest_status == "stored" and not outcome.duplicate
    # The matched reply resolved the uncertain send; the binding was re-published (version 2).
    after = await changes(db, mw, page.next_cursor)
    assert [(i.binding_version, i.state) for i in after.items] == [(2, InquiryBindingState.ACTIVE)]
    assert after.items[0].outbound_message_ids == (outbound_message_id(inquiry),)


async def test_publication_needs_a_system_or_owner_actor(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    async with unit_of_work(db, mw.worker.actor("req")) as conn:
        with pytest.raises(Forbidden):
            await mail_workers_repo.publish_inquiry_binding(conn, mw.worker.actor("req"), inquiry)


async def test_a_mailbox_batch_publication_locks_its_inquiries_before_the_mailbox(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION (lock order): ``publish_mailbox_bindings`` published inquiry by inquiry, so it
    held the mailbox row (sync sequence) while waiting for the next inquiry's lock. A reply
    ingest or a send transition holds one inquiry and then publishes (inquiry -> mailbox): the
    two deadlocked. The batch now locks all its inquiries (id order) before the first publication.
    """
    pair = [sent(seed.conn, iw), sent(seed.conn, another(iw))]
    mw = await issue(db, iw)  # publishes both (version 1)
    # The batch's first inquiry (in any order it might use) gets a changed binding, so publishing
    # it takes the mailbox row; the other one is held by a concurrent transaction.
    order = [
        r["id"]
        for r in rows(
            seed,
            "select id from app.seller_inquiries where id = any(%s) order by created_at, id",
            pair,
        )
    ]
    first, held = order
    seller = seed.scalar("select seller_entity_id from app.seller_inquiries where id = %s", (first,))
    with_vehicle(replace(iw, seller_entity_id=seller))  # a newly verified address: version 2
    actor = system(iw.workspace_id)

    async def batch() -> list[mail_workers_repo.PublishedBinding]:
        async with unit_of_work(db, actor) as conn:
            return await mail_workers_repo.publish_mailbox_bindings(conn, actor, mw.mailbox_id)

    # Another transaction holds one inquiry (like a reply ingest) ...
    async with unit_of_work(db, actor) as conn:
        await conn.execute("select id from app.seller_inquiries where id = %s for update", (held,))
        running = asyncio.create_task(batch())
        await asyncio.sleep(0.5)  # the batch now waits for the held inquiry's lock
        # ... and publishes its binding: it needs the mailbox row, which the batch must not hold.
        tomb = await mail_workers_repo.tombstone_inquiry_binding(
            conn, actor, held, reason="synthetic withdrawal during a batch"
        )
        assert tomb is not None and tomb.created
    published = await running
    assert [(p.inquiry_id, p.binding_version) for p in published] == [(first, 2)]
    versions = rows(
        seed,
        "select inquiry_id, binding_version, binding_state from ops.mail_binding_sync"
        " where mailbox_binding_id = %s order by sequence",
        mw.mailbox_id,
    )
    assert [(v["inquiry_id"], v["binding_version"], v["binding_state"]) for v in versions[-2:]] == [
        (held, 2, "tombstoned"),
        (first, 2, "active"),
    ]
