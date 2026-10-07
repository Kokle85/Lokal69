"""Correlated seller-reply ingest (spec 37.7, 37.8 ``POST /v1/mail-workers/replies``; 37.10 deltas).

Each test drives ``replies_repo`` through ``Database(set_role="suv_backend")`` and checks the
stored rows through the superuser seed connection. Synthetic addresses and Message-IDs only.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    InquiryWorld,
    accept,
    dispatch,
    insert_inquiry,
    outbound_message_id,
    queue,
    reserve,
    sender_binding,
)
from tests.integration.v11_replies.support import (
    OPTIONS,
    another,
    ingest,
    issue,
    one,
    owner,
    publish,
    request,
    rows,
    sent,
    system,
    uncertain,
)

from suv_deals.domain.enums import ReplyMessageType
from suv_deals.domain.lifecycle import AvailabilitySignal, AvailabilitySignalKind
from suv_deals.errors import Forbidden, IdempotencyConflict, ValidationFailed
from suv_deals.persistence import availability_repo, replies_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.replies_repo import ReplyIngestOptions
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

SOLD = "Guten Tag, leider schon verkauft. Synthetic fixture reply."
PRICE = "Guten Tag, das Fahrzeug ist noch verfügbar. Letzter Preis 2.450 Euro, Festpreis."


def replies_of(seed: Seed, inquiry: uuid.UUID) -> list[dict[str, Any]]:
    return rows(
        seed,
        "select id, quarantined, quarantine_reason, conflict_of_reply_id, sanitized_body, message_type,"
        " correlation_status, correlation_reasons, header_linked from app.seller_replies"
        " where inquiry_id = %s order by ingested_at, id",
        inquiry,
    )


def state(seed: Seed, inquiry: uuid.UUID) -> str:
    return str(seed.scalar("select state from app.seller_inquiries where id = %s", (inquiry,)))


# ---------------------------------------------------------------------------------------------
# Idempotency and deduplication
# ---------------------------------------------------------------------------------------------


async def test_replay_after_a_backend_outage_returns_the_original_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """The worker retries the same request (same key) after a lost response: one reply."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    upload = request(mw, inquiry, entry_id="synthetic-entry-1")
    first = await ingest(db, mw, upload, "idem-outage-0001")
    second = await ingest(db, mw, upload, "idem-outage-0001")
    assert first.ingest_status == "stored" and not first.duplicate
    assert second.duplicate and second.reply_id == first.reply_id
    assert second.ingested_at == first.ingested_at and second.inquiry_id == inquiry
    assert len(replies_of(seed, inquiry)) == 1
    dedup = one(
        seed,
        "select duplicate_count, conflict_count from ops.mail_ingest_dedup where reply_id = %s",
        first.reply_id,
    )
    assert dedup == {"duplicate_count": 1, "conflict_count": 0}
    # The route entry point returns the wire acknowledgement.
    ack = await replies_repo.ingest(
        db, mw.worker, upload, "idem-outage-0001", request_id="req-3", options=OPTIONS
    )
    assert ack.duplicate and ack.reply_id == first.reply_id and ack.schema_version == "1.0"
    assert ack.request_id == "req-3" and ack.ingest_status == "stored"
    assert state(seed, inquiry) == "replied"


async def test_duplicate_message_events_with_different_keys_are_one_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """A folder move or a rescan re-uploads the same message with a new key and locator."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    original = request(mw, inquiry, entry_id="synthetic-entry-inbox")
    first = await ingest(db, mw, original, "idem-event-0001")
    moved = original.model_copy(
        update={
            "source_message": original.source_message.model_copy(
                update={"outlook_entry_id": "synthetic-entry-archive"}
            )
        }
    )
    second = await ingest(db, mw, moved, "idem-event-0002")
    assert second.duplicate and second.reply_id == first.reply_id
    assert len(replies_of(seed, inquiry)) == 1
    locators = rows(
        seed,
        "select outlook_entry_id from app.seller_reply_locators where reply_id = %s order by seen_at",
        first.reply_id,
    )
    assert {r["outlook_entry_id"] for r in locators} == {"synthetic-entry-inbox", "synthetic-entry-archive"}
    # Without an Internet Message-ID the provider id is the stable identity.
    no_mid = request(mw, inquiry, message_id=None, provider_message_id="synthetic-provider-42")
    third = await ingest(db, mw, no_mid, "idem-event-0003")
    fourth = await ingest(db, mw, no_mid, "idem-event-0004")
    assert not third.duplicate and fourth.duplicate and fourth.reply_id == third.reply_id


async def test_same_key_with_an_altered_body_conflicts_and_is_quarantined(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    upload = request(mw, inquiry)
    first = await ingest(db, mw, upload, "idem-conflict-01")
    altered = upload.model_copy(update={"sanitized_body_text": "Altered synthetic body: price 1 EUR."})
    with pytest.raises(IdempotencyConflict):
        await replies_repo.ingest(
            db, mw.worker, altered, "idem-conflict-01", request_id="req-c", options=OPTIONS
        )
    stored = replies_of(seed, inquiry)
    assert len(stored) == 2
    original_row, conflict_row = stored
    assert (
        original_row["id"] == first.reply_id and original_row["sanitized_body"] == upload.sanitized_body_text
    )
    assert not original_row["quarantined"]  # never overwritten
    assert conflict_row["conflict_of_reply_id"] == first.reply_id
    assert conflict_row["quarantined"] and conflict_row["quarantine_reason"] == "idempotency_conflict"
    dedup = one(
        seed,
        "select conflict_count, last_conflict_reply_id from ops.mail_ingest_dedup where reply_id = %s",
        first.reply_id,
    )
    assert dedup == {"conflict_count": 1, "last_conflict_reply_id": conflict_row["id"]}
    # The same Message-ID with different content under a NEW key conflicts as well (no new row).
    again = await ingest(db, mw, altered, "idem-conflict-02")
    assert again.conflict and again.reply_id == conflict_row["id"] and again.ingest_status == "quarantined"
    assert len(replies_of(seed, inquiry)) == 2
    # The same key reused for a DIFFERENT message is a conflict too.
    other_message = request(mw, inquiry, body="A different synthetic message.")
    reused = await ingest(db, mw, other_message, "idem-conflict-01")
    assert reused.conflict
    audit = rows(
        seed,
        "select metadata->>'audit_outcome' as outcome from ops.audit_events"
        " where action = 'seller_reply.idempotency_conflict' and workspace_id = %s",
        iw.workspace_id,
    )
    assert audit and all(a["outcome"] == "denied" for a in audit)


async def test_concurrent_uploads_of_one_message_store_it_once(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    upload = request(mw, inquiry)
    acks = await asyncio.gather(
        *(
            replies_repo.ingest(
                db, mw.worker, upload, f"idem-race-{n:04d}", request_id=f"req-{n}", options=OPTIONS
            )
            for n in range(4)
        )
    )
    assert len({a.reply_id for a in acks}) == 1
    assert sorted(a.duplicate for a in acks) == [False, True, True, True]
    assert len(replies_of(seed, inquiry)) == 1


async def test_an_invalid_idempotency_key_is_refused(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    for key in ("short", "x" * 129, "has space-in-key", "café-key-1234"):
        with pytest.raises(ValidationFailed):
            await ingest(db, mw, request(mw, inquiry), key)


# ---------------------------------------------------------------------------------------------
# Scope: the worker's own mailbox only
# ---------------------------------------------------------------------------------------------


async def test_cross_mailbox_injection_is_refused_without_an_existence_leak(
    db: Database, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    mine = sent(seed.conn, iw)
    mw = await issue(db, iw)
    theirs = sent(seed.conn, iw_b)
    other = await issue(db, iw_b)
    # Same workspace, another sender account (another mailbox).
    second_sender = sender_binding(
        seed,
        iw.workspace_id,
        account_id="synthetic-outlook-account-2",
        from_address="second@synthetic-mail.example",
    )
    neighbour_world = replace(another(iw), sender_binding_id=second_sender)
    neighbour = sent_with_sender(seed.conn, neighbour_world)
    refusals = []
    for inquiry in (theirs, uuid.uuid4(), neighbour):
        with pytest.raises(Forbidden) as refused:
            await ingest(db, mw, request(mw, inquiry, in_reply_to=outbound_message_id(inquiry)))
        refusals.append((refused.value.message, refused.value.details))
    assert len(set(json.dumps(r, sort_keys=True) for r in refusals)) == 1  # indistinguishable
    assert refusals[0][1] == {"reason": "mailbox_binding_mismatch"}
    # A body naming another mailbox is refused the same way (never silently reassigned).
    with pytest.raises(Forbidden) as refused:
        await ingest(db, mw, request(mw, mine, mailbox_binding_id=other.mailbox_id))
    assert refused.value.details == {"reason": "mailbox_binding_mismatch"}
    for inquiry in (theirs, neighbour):
        assert replies_of(seed, inquiry) == []
    assert rows(seed, "select id from app.seller_replies where workspace_id = %s", iw_b.workspace_id) == []


def sent_with_sender(conn: psycopg.Connection, world: InquiryWorld) -> uuid.UUID:
    """A sent inquiry of ``world`` whose binding names ``world.sender_binding_id``."""
    inquiry = insert_inquiry(conn, world)
    reserve(
        conn,
        world,
        inquiry,
        sender_account_id="synthetic-outlook-account-2",
        sender_from_address="second@synthetic-mail.example",
    )
    queue(conn, world, inquiry)
    attempt = dispatch(conn, world, inquiry)
    accept(conn, world, inquiry, attempt)
    return inquiry


# ---------------------------------------------------------------------------------------------
# Correlation is recomputed on the server
# ---------------------------------------------------------------------------------------------


async def test_forged_correlation_fields_are_ignored(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    forged = request(
        mw,
        inquiry,
        in_reply_to=None,
        from_address="stranger@unrelated.example",
        subject="Re: your enquiry",
        body="Synthetic unrelated message that claims to be a reply.",
        correlation_status="matched",
        correlation_reasons=["HEADER_REFERENCE_MATCH", "SENDER_VERIFIED", "RESOLVES_UNCERTAIN_SEND"],
    )
    with pytest.raises(ValidationFailed) as refused:
        await ingest(db, mw, forged)
    assert refused.value.details == {"reason": "reply_not_correlated"}
    assert replies_of(seed, inquiry) == [] and state(seed, inquiry) == "accepted"
    # Linked by headers but from a changed address: stored for verification, never applied,
    # whatever the worker claimed.
    changed = request(
        mw,
        inquiry,
        from_address="someone-else@unrelated.example",
        body=SOLD,
        correlation_status="matched",
        correlation_reasons=["SENDER_VERIFIED"],
    )
    outcome = await ingest(db, mw, changed)
    assert outcome.ingest_status == "quarantined" and outcome.quarantine_reason == "changed_address"
    assert state(seed, inquiry) == "accepted"
    assert rows(seed, "select id from app.availability_events where listing_id = %s", iw.listing_id) == []
    assert (
        rows(
            seed,
            "select id from ops.outbox where workspace_id = %s and event_type = 'seller.reply.received'",
            iw.workspace_id,
        )
        == []
    )
    (stored,) = replies_of(seed, inquiry)
    assert "SENDER_VERIFIED" not in stored["correlation_reasons"]
    assert "CHANGED_ADDRESS" in stored["correlation_reasons"]


async def test_a_worker_cannot_upgrade_a_bounce_to_a_seller_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    disguised = request(
        mw,
        inquiry,
        from_address="mailer-daemon@synthetic-mail.example",
        subject="Undeliverable: Anfrage zu Ihrem Fahrzeug",
        body=f"Final-Recipient: rfc822; {iw.contact_address}\nAction: failed\nStatus: 5.1.1\n",
        references=(outbound_message_id(inquiry),),
        message_type="seller_reply",
    )
    outcome = await ingest(db, mw, disguised)
    assert outcome.message_type == ReplyMessageType.BOUNCE
    assert state(seed, inquiry) == "bounced"


# ---------------------------------------------------------------------------------------------
# Effects of a matched reply
# ---------------------------------------------------------------------------------------------


async def test_a_dsn_suppresses_the_address(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    dsn = request(
        mw,
        inquiry,
        from_address="mailer-daemon@synthetic-mail.example",
        subject="Undeliverable: Anfrage zu Ihrem Fahrzeug",
        body=(
            "Reporting-MTA: dns; mx.synthetic-mail.example\n\n"
            f"Final-Recipient: rfc822; {iw.contact_address}\nAction: failed\nStatus: 5.1.1\n"
        ),
        message_type="bounce",
    )
    outcome = await ingest(db, mw, dsn)
    assert outcome.ingest_status == "stored" and outcome.message_type == ReplyMessageType.BOUNCE
    assert state(seed, inquiry) == "bounced"
    suppression = one(
        seed,
        "select scope, match_key, reason, inquiry_id, reply_id, removed_at from ops.email_suppressions"
        " where workspace_id = %s",
        iw.workspace_id,
    )
    assert suppression == {
        "scope": "address",
        "match_key": iw.contact_address.lower(),
        "reason": "hard_bounce",
        "inquiry_id": inquiry,
        "reply_id": outcome.reply_id,
        "removed_at": None,
    }
    # A bounce never signals the owner; it stays in the dashboard and the audit trail.
    assert (
        rows(
            seed,
            "select id from ops.outbox where workspace_id = %s and event_type = 'seller.reply.received'",
            iw.workspace_id,
        )
        == []
    )


async def test_a_dsn_returning_the_original_resolves_an_uncertain_send(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    inquiry, attempt = uncertain(seed.conn, iw)
    await publish(db, iw.workspace_id, inquiry)
    dsn = request(
        mw,
        inquiry,
        in_reply_to=None,
        from_address="postmaster@synthetic-mail.example",
        subject="Delivery Status Notification (Failure)",
        body=(
            f"Final-Recipient: rfc822; {iw.contact_address}\nAction: failed\nStatus: 5.1.1\n\n"
            f"Message-ID: {outbound_message_id(inquiry)}\n"
        ),
        message_type="bounce",
    )
    outcome = await ingest(db, mw, dsn)
    assert outcome.transitions == ("accepted", "bounced")
    assert state(seed, inquiry) == "bounced"
    reconciled = one(
        seed,
        "select reconciled_outcome, reconciliation_evidence from ops.email_delivery_attempts where id = %s",
        attempt,
    )
    assert reconciled["reconciled_outcome"] == "accepted"
    assert reconciled["reconciliation_evidence"]["correlated_inbound"] is True
    assert len(outcome.suppression_ids) == 1


async def test_an_unlinked_dsn_is_kept_for_verification_without_effects(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    dsn = request(
        mw,
        inquiry,
        in_reply_to=None,
        from_address="mailer-daemon@synthetic-mail.example",
        subject="Undeliverable",
        body=f"Final-Recipient: rfc822; {iw.contact_address}\nAction: failed\nStatus: 5.1.1\n",
        message_type="bounce",
    )
    outcome = await ingest(db, mw, dsn)
    assert outcome.ingest_status == "quarantined" and outcome.quarantine_reason == "dsn_link_unverified"
    assert state(seed, inquiry) == "accepted"
    assert rows(seed, "select id from ops.email_suppressions where workspace_id = %s", iw.workspace_id) == []


async def test_a_sold_claim_is_sold_claimed_evidence_with_its_effects(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    valuation = seed.valuation(iw.workspace_id, iw.listing_id, iw.vehicle.revision_id, state="incomplete")
    outcome = await ingest(db, mw, request(mw, inquiry, body=SOLD))
    assert outcome.ingest_status == "stored" and state(seed, inquiry) == "replied"
    event = one(
        seed,
        "select old_availability, new_availability, evidence_kind, reason, reply_id, promote_current,"
        " conflicts_with_current from app.availability_events where listing_id = %s",
        iw.listing_id,
    )
    assert event == {
        "old_availability": "available",
        "new_availability": "sold_claimed",
        "evidence_kind": "seller_reported_sold",
        "reason": "seller_reported_sold",
        "reply_id": outcome.reply_id,
        "promote_current": True,
        "conflicts_with_current": False,
    }
    assert (
        seed.scalar("select availability from app.listings where id = %s", (iw.listing_id,)) == "sold_claimed"
    )
    assert outcome.availability_event_id is not None
    # Valuation invalidated and recomputation queued (deduplicated per listing).
    assert seed.scalar("select state from app.valuations where id = %s", (valuation,)) == "stale"
    assert outcome.stale_valuation_ids == (valuation,)
    job = one(
        seed, "select job_type, state, dedup_key from ops.jobs where id = %s", outcome.recompute_job_ids[0]
    )
    assert job["job_type"] == "valuation" and job["dedup_key"] == f"valuation.recompute:{iw.listing_id}"
    # The outbox signal is minimal: ids, the safe dashboard URL and a fixed status only.
    signal = one(
        seed,
        "select event_type, payload, state, is_fixture from ops.outbox where workspace_id = %s"
        " and event_type = 'seller.reply.received'",
        iw.workspace_id,
    )
    payload = signal["payload"]
    assert replies_repo.signal_payload_is_minimal(payload)
    text = json.dumps(payload)
    assert "verkauft" not in text and iw.contact_address not in text and "@" not in text
    assert payload["reply_id"] == str(outcome.reply_id) and payload["inquiry_id"] == str(inquiry)
    assert "sold" in str(payload["status"])
    assert str(payload["dashboard_url"]).startswith("https://dashboard.example.invalid/")
    # Fixture listings never notify.
    assert signal["is_fixture"] is True and signal["state"] == "blocked"


async def test_one_missing_result_is_never_a_sale_and_a_quote_stays_unaccepted(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    asking = seed.scalar(
        "select asking_minor from app.listing_revisions where id = %s", (iw.vehicle.revision_id,)
    )
    outcome = await ingest(db, mw, request(mw, inquiry, body=PRICE))
    claims = seed.scalar("select claims from app.seller_replies where id = %s", (outcome.reply_id,))
    (price,) = claims["prices"]
    assert price["accepted"] is False and price["status"] == "unaccepted_seller_quote"
    assert price["amount"] == "2450"
    # The advertised price and the listing revision are untouched.
    assert (
        seed.scalar("select asking_minor from app.listing_revisions where id = %s", (iw.vehicle.revision_id,))
        == asking
    )
    assert (
        seed.scalar("select current_revision_id from app.listings where id = %s", (iw.listing_id,))
        == iw.vehicle.revision_id
    )
    # "Still available" is seller evidence for available (no sale is ever inferred).
    kinds = [
        r["evidence_kind"]
        for r in rows(
            seed, "select evidence_kind from app.availability_events where listing_id = %s", iw.listing_id
        )
    ]
    assert kinds == ["seller_reported_available"]
    assert seed.scalar("select availability from app.listings where id = %s", (iw.listing_id,)) == "available"


async def test_an_opt_out_suppresses_seller_and_address(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    outcome = await ingest(db, mw, request(mw, inquiry, body="Bitte keine weiteren Anfragen."))
    assert state(seed, inquiry) == "seller_opted_out"
    scopes = {
        (r["scope"], r["reason"])
        for r in rows(
            seed, "select scope, reason from ops.email_suppressions where workspace_id = %s", iw.workspace_id
        )
    }
    assert scopes == {("seller", "seller_opt_out"), ("address", "seller_opt_out")}
    assert len(outcome.suppression_ids) == 2


async def test_the_processing_job_is_optional(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    options = ReplyIngestOptions(dashboard_base_url=OPTIONS.dashboard_base_url, enqueue_processing_job=True)
    outcome = await ingest(db, mw, request(mw, inquiry), options=options)
    assert outcome.processing_job_id is not None
    job = one(seed, "select job_type, dedup_key from ops.jobs where id = %s", outcome.processing_job_id)
    assert job == {
        "job_type": "seller_reply_process",
        "dedup_key": f"seller_reply.process:{outcome.reply_id}",
    }


async def test_rls_keeps_replies_inside_their_workspace(
    db: Database, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    outcome = await ingest(db, mw, request(mw, inquiry, body=SOLD))
    for table in (
        "app.seller_replies",
        "ops.mail_ingest_dedup",
        "app.availability_events",
        "ops.mail_binding_sync",
    ):
        with backend(seed.conn, iw_b.workspace_id):
            assert seed.conn.execute(f"select count(*) from {table}").fetchone() == (0,)
        with backend(seed.conn, iw.workspace_id):
            count = seed.conn.execute(f"select count(*) from {table}").fetchone()
            assert count is not None and count[0] >= 1
    assert outcome.reply_id is not None


async def test_a_contradicting_seller_statement_is_kept_and_stops_outreach(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """The site shows a sold badge; the seller then says "still available": both are kept, the
    listing is not overridden, and outreach for the vehicle stops (spec 37.9)."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    badge_at = datetime.now(UTC) - timedelta(minutes=30)
    seed.insert_id(
        "app.availability_events",
        workspace_id=iw.workspace_id,
        source_id=iw.vehicle.source_id,
        listing_id=iw.listing_id,
        old_availability="available",
        new_availability="sold_claimed",
        evidence_kind="source_sold_badge",
        reason="source_sold_badge",
        source_reference="synthetic sold badge",
        effective_at=badge_at,
        observed_at=badge_at,
        confidence="high",
    )
    seed.conn.execute("update app.listings set availability = 'sold_claimed' where id = %s", (iw.listing_id,))
    outcome = await ingest(db, mw, request(mw, inquiry, body=PRICE))
    assert outcome.availability_conflict
    event = one(
        seed,
        "select new_availability, evidence_kind, promote_current, conflicts_with_current"
        " from app.availability_events where reply_id = %s",
        outcome.reply_id,
    )
    assert event == {
        "new_availability": "available",
        "evidence_kind": "seller_reported_available",
        "promote_current": False,
        "conflicts_with_current": True,
    }
    assert (
        seed.scalar("select availability from app.listings where id = %s", (iw.listing_id,)) == "sold_claimed"
    )
    reasons = {
        r["reason"]
        for r in rows(
            seed,
            "select reason from ops.email_suppressions where workspace_id = %s and scope = 'vehicle'",
            iw.workspace_id,
        )
    }
    assert reasons == {"contradictory_availability"}


async def test_availability_history_reads_need_a_read_scope(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    outcome = await ingest(db, mw, request(mw, inquiry, body=SOLD))
    actor = owner(iw.workspace_id)
    async with unit_of_work(db, actor) as conn:
        (event,) = await availability_repo.list_reply_events(conn, actor, outcome.reply_id)
        history = await availability_repo.list_listing_events(conn, actor, iw.listing_id)
        state_now = await availability_repo.listing_lifecycle_state(conn, actor, iw.listing_id)
    assert event.evidence_kind == "seller_reported_sold" and event.new_availability == "sold_claimed"
    assert [e.id for e in history] == [event.id]
    assert state_now.availability == "sold_claimed"
    assert state_now.availability_evidence_kind == "seller_reported_sold"
    worker_actor = mw.worker.actor("req")
    async with unit_of_work(db, worker_actor) as conn:
        with pytest.raises(Forbidden):
            await availability_repo.list_reply_events(conn, worker_actor, outcome.reply_id)


# ---------------------------------------------------------------------------------------------
# Review regressions (independent review of B1b)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["bounce", "delivery_notice"])
async def test_a_worker_label_alone_never_stores_unlinked_mail(
    db: Database, seed: Seed, iw: InquiryWorld, label: str
) -> None:
    """REGRESSION: an unlinked upload the worker merely LABELLED a delivery report was stored
    (quarantined ``dsn_link_unverified``), so unrelated personal mail could enter the database.
    Only an upload whose own From/subject/body the server reads as a delivery report may be kept
    unlinked; anything else is refused like every uncorrelated message (spec 37.7)."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    personal = request(
        mw,
        inquiry,
        in_reply_to=None,
        from_address="friend@unrelated.example",
        subject="Dinner on Friday",
        body="Hi, see you on Friday at eight. Synthetic personal message.",
        message_type=label,
    )
    with pytest.raises(ValidationFailed) as refused:
        await ingest(db, mw, personal)
    assert refused.value.details == {"reason": "reply_not_correlated"}
    assert replies_of(seed, inquiry) == []
    assert rows(seed, "select id from ops.mail_ingest_dedup where workspace_id = %s", iw.workspace_id) == []
    # A genuine (sanitised) delivery report without a checkable link is still kept for review.
    dsn = request(
        mw,
        inquiry,
        in_reply_to=None,
        from_address="postmaster@synthetic-mail.example",
        subject="Undeliverable: Anfrage zu Ihrem Fahrzeug",
        body="Delivery has failed to these recipients or groups:\n[email removed]\nStatus: 5.1.1\n",
        message_type="bounce",
    )
    kept = await ingest(db, mw, dsn)
    assert kept.ingest_status == "quarantined" and kept.quarantine_reason == "dsn_link_unverified"
    assert state(seed, inquiry) == "accepted"


async def test_a_future_dated_seller_statement_never_pins_the_listing(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: the worker's ``received_at`` was the statement's effective time unbounded, so a
    reply dated in the future (worker clock skew or a forged upload) outranked every later source
    observation: a removed page seen afterwards was stored as history and never promoted."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    outcome = await ingest(db, mw, request(mw, inquiry, body=PRICE, received_at=future))
    event = one(
        seed,
        "select effective_at, observed_at, evidence_kind from app.availability_events where reply_id = %s",
        outcome.reply_id,
    )
    now = datetime.now(UTC)
    assert event["evidence_kind"] == "seller_reported_available"
    assert event["effective_at"] <= now and event["observed_at"] <= now
    claims = seed.scalar("select claims from app.seller_replies where id = %s", (outcome.reply_id,))
    (price,) = claims["prices"]
    assert datetime.fromisoformat(price["quoted_at"]) <= now
    # The source then shows the ad removed: current evidence, promoted onto the listing.
    actor = system(iw.workspace_id)
    async with unit_of_work(db, actor) as conn:
        later = await availability_repo.apply_signal(
            conn,
            actor,
            iw.listing_id,
            AvailabilitySignal(
                kind=AvailabilitySignalKind.REMOVED_PAGE,
                observed_at=datetime.now(UTC),
                source_reference="synthetic removal page",
            ),
        )
    assert later.event is not None and later.event.promote_current and not later.event.historical_only
    assert seed.scalar("select availability from app.listings where id = %s", (iw.listing_id,)) == "removed"


async def test_a_running_recomputation_gets_a_follow_up_after_a_reply(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: a listing without an open valuation was re-queued with a plain enqueue, so a
    recomputation already RUNNING (it read its inputs before the reply) silently absorbed the new
    seller evidence. Like ``valuation_repo.mark_stale``, a follow-up job now runs after it."""
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    running = seed.job(
        iw.workspace_id,
        job_type="valuation",
        dedup_key=f"valuation.recompute:{iw.listing_id}",
        listing_id=iw.listing_id,
        payload={"listing_id": str(iw.listing_id), "stale_valuation_ids": [], "reason": "evidence"},
    )
    seed.conn.execute(
        "update ops.jobs set state = 'running', lease_owner = 'SYNTHETIC-worker',"
        " lease_token = gen_random_uuid(), lease_expires_at = clock_timestamp() + interval '5 minutes',"
        " last_heartbeat_at = clock_timestamp(), attempts = 1 where id = %s",
        (running,),
    )
    outcome = await ingest(db, mw, request(mw, inquiry, body=PRICE))
    assert outcome.stale_valuation_ids == ()
    (follow_up,) = outcome.recompute_job_ids
    assert follow_up != running
    job = one(seed, "select state, dedup_key from ops.jobs where id = %s", follow_up)
    assert job == {"state": "queued", "dedup_key": f"valuation.recompute:{iw.listing_id}:after:{running}"}
