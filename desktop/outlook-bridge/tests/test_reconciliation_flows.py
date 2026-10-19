"""NewMailEx prompts plus reconciliation, moves, duplicates, the binding race and local privacy.

Spec 37.6 "Event detection plus reconciliation", 37.7 local matching privacy and the 37.10 delta
tests: NewMailEx startup gaps recovered; moved messages deduplicated (fingerprint excludes
locators); duplicate message events; reply-before-binding-sync race; unrelated personal mail never
leaves the local mailbox; replies map by IDs/headers rather than subject alone.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from bridge_support import LISTING_REF, OWNER, SELLER, Harness
from outlook_bridge.credentials import CredentialManager
from outlook_bridge.local_queue import LocalStore, Outcome, ts
from outlook_bridge.matching import LocalMatcher
from outlook_bridge.testing import AttachmentSpec, inquiry_message_id

SPEC_V1_KEYS = {
    "schema_version",
    "inquiry_id",
    "binding_version",
    "mailbox_binding_id",
    "source_message",
    "headers",
    "subject",
    "sanitized_body_text",
    "detected_language",
    "attachments",
    "observed_at",
}


def _interval(h: Harness) -> timedelta:
    return h.config.reconcile_interval + timedelta(seconds=1)


# ------------------------------------------------------------------------------------------ uploads


def test_correlated_reply_is_uploaded_in_the_exact_v1_shape(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    item = harness.deliver_reply(inquiry, fire_event=False, message_id="<reply-1@dealer.example.invalid>")
    harness.worker().start()
    posts = harness.reply_posts()
    assert len(posts) == 1
    payload = posts[0]
    assert set(payload) == SPEC_V1_KEYS
    assert payload["schema_version"] == "1.0"
    assert payload["inquiry_id"] == str(inquiry)
    assert payload["binding_version"] == 1
    assert set(payload["source_message"]) == {
        "internet_message_id",
        "provider_message_id",
        "outlook_entry_id",
        "outlook_store_id",
        "received_at",
    }
    assert payload["source_message"]["internet_message_id"] == "<reply-1@dealer.example.invalid>"
    assert payload["source_message"]["outlook_entry_id"] == item.EntryID
    assert payload["headers"] == {
        "from": SELLER,
        "in_reply_to": inquiry_message_id(inquiry),
        "references": [inquiry_message_id(inquiry)],
    }
    assert payload["detected_language"] == "de"
    assert payload["attachments"] == []
    headers = harness.backend.reply_headers[0]
    assert headers["idempotency-key"].startswith("mwr1-")
    assert len(json.dumps(payload).encode()) <= 128 * 1024
    assert harness.outlook.send_calls == []  # never an automatic answer or follow-up


def test_startup_gap_without_new_mail_events_is_recovered_by_reconciliation(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    # Delivered while the worker was not running: no NewMailEx sink existed, no event is seen.
    harness.deliver_reply(inquiry, fire_event=True)
    report = harness.worker().start()
    assert sum(report.events.values()) == 0
    assert report.scan["uploaded"] == 1
    assert len(harness.reply_posts()) == 1


def test_mail_synchronised_without_an_event_is_found_by_the_periodic_scan(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.advance(timedelta(seconds=30))
    harness.deliver_reply(inquiry, fire_event=False)  # e.g. Outlook startup synchronisation
    assert harness.reply_posts() == [] and sum(worker.tick().scan.values()) == 0  # not due yet
    harness.advance(_interval(harness))
    report = worker.tick()
    assert report.scan["uploaded"] == 1
    assert len(harness.reply_posts()) == 1


def test_late_synchronised_old_mail_is_caught_by_the_deep_scan(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.advance(timedelta(hours=1))
    worker.tick()
    # A server message received three hours ago appears only now (outside the overlap window).
    harness.deliver_reply(inquiry, fire_event=False, received_at=harness.clock.now() - timedelta(hours=3))
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []
    harness.advance(timedelta(hours=24))
    worker.tick()
    assert len(harness.reply_posts()) == 1


def test_new_mail_event_prompts_an_immediate_look(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.advance(timedelta(seconds=5))
    harness.deliver_reply(inquiry, fire_event=True)
    report = worker.tick()
    assert report.events["uploaded"] == 1
    assert len(harness.reply_posts()) == 1


def test_duplicate_message_events_upload_once(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    item = harness.deliver_reply(inquiry, fire_event=True)
    harness.outlook.fire_new_mail((item.EntryID, item.EntryID))
    first = worker.tick()
    assert first.events["uploaded"] == 1
    harness.outlook.fire_new_mail((item.EntryID,))
    second = worker.tick()
    assert second.events["duplicate"] == 1
    harness.advance(_interval(harness))
    third = worker.tick()
    assert third.scan["duplicate"] == 1
    assert len(harness.reply_posts()) == 1
    assert len(harness.store.backlog_rows()) == 1


def test_event_ids_outside_the_configured_folders_are_ignored_unread(harness: Harness) -> None:
    worker = harness.worker()
    worker.start()
    other_inbox = harness.outlook.folder(harness.other_account, "inbox")
    foreign = harness.outlook.deliver(
        other_inbox,
        subject="Private",
        body="private",
        sender="x@private.example.invalid",
        message_id="<x@private.example.invalid>",
        received_at=harness.clock.now(),
        fire_event=False,
    )
    worker.on_new_mail((foreign.EntryID, "UNKNOWN-ENTRY-ID"))
    report = worker.tick()
    assert report.events["out_of_scope"] == 2
    assert foreign.EntryID not in harness.outlook.root.item_reads


def test_new_mail_event_overflow_falls_back_to_reconciliation(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.deliver_reply(inquiry, fire_event=False)
    for index in range(1001):
        worker.on_new_mail((f"bogus-{index}",))
    report = worker.tick()
    assert report.scan["uploaded"] == 1  # overflow forced a reconciliation pass
    assert any(g.kind == "new_mail_event_overflow" for g in harness.store.gaps())


# -------------------------------------------------------------------------------------------- moves


def test_moved_message_is_deduplicated_and_its_locator_recorded(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    item = harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    old_entry = item.EntryID
    harness.outlook.move(item, harness.cars)  # a mailbox rule moved it: new EntryID
    assert item.EntryID != old_entry
    harness.advance(_interval(harness))
    report = worker.tick()
    assert report.scan["moved"] == 1
    assert len(harness.reply_posts()) == 1
    key = harness.store.backlog_rows()[0].dedup_key_hash
    assert harness.store.locator_history(key)[-1][0] == item.EntryID


def test_moved_message_without_internet_message_id_is_deduplicated(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    item = harness.outlook.deliver(
        harness.inbox,
        subject=f"AW: Anfrage {LISTING_REF}",
        body="Das Fahrzeug ist verfügbar.",
        sender=SELLER,
        message_id=None,
        received_at=harness.clock.now() - timedelta(minutes=1),
        in_reply_to=inquiry_message_id(inquiry),
        references=(inquiry_message_id(inquiry),),
        fire_event=True,
    )
    worker.tick()
    assert len(harness.reply_posts()) == 1
    harness.outlook.move(item, harness.cars)
    harness.advance(_interval(harness))
    assert worker.tick().scan["moved"] == 1
    assert len(harness.reply_posts()) == 1


def test_fingerprint_and_keys_exclude_mutable_locators(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.worker().start()  # sync bindings
    item = harness.deliver_reply(inquiry, fire_event=False)
    harness.session.connect(OWNER)
    folders = harness.session.resolve_folders(harness.config.folders)
    matcher = LocalMatcher(harness.config.mailbox_binding_id, retry_window=timedelta(hours=24))
    bindings = harness.store.bindings_for_matching()

    def prepared() -> tuple[str, str, str, str]:
        ref = harness.session.find_by_internet_message_id(
            item.PropertyAccessor._props["http://schemas.microsoft.com/mapi/proptag/0x1035001F"]
        )
        assert ref is not None
        snapshot = harness.session.read_item(ref)
        assert snapshot is not None
        folder = next(f for f in folders if f.entry_id == snapshot.ref.folder_entry_id)
        decision = matcher.evaluate(
            snapshot,
            bindings,
            in_junk_folder=folder.role == "junk",
            first_seen_at=harness.clock.now(),
            now=harness.clock.now(),
        )
        upload = matcher.prepare_upload(
            snapshot, decision, {}, in_junk_folder=False, observed_at=harness.clock.now()
        )
        return (
            upload.request.fingerprint(),
            upload.idempotency_key,
            matcher.local_key(snapshot).key_hash,
            upload.payload["source_message"]["outlook_entry_id"],
        )

    before = prepared()
    harness.outlook.move(item, harness.cars)
    harness.advance(timedelta(minutes=5))  # a later scan: observed_at differs too
    after = prepared()
    assert before[:3] == after[:3]  # fingerprint, idempotency key and local key are stable
    assert before[3] != after[3]  # only the locator changed


def test_server_side_replay_after_local_state_loss_returns_the_existing_reply(make_harness) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    inquiry = uuid4()
    h.bind(inquiry)
    h.deliver_reply(inquiry, fire_event=False)
    h.worker().start()
    first_reply_id = next(iter(h.backend.stored_replies))
    # The local store is lost (new machine profile); the message has also moved to another folder.
    item = h.outlook.items_in(h.inbox)[0]
    h.outlook.move(item, h.cars)
    h.store.close()
    h.store = LocalStore.in_memory(h.config.mailbox_binding_id)
    h.credentials = CredentialManager(h.cred_store, h.store)
    h.worker().start()
    assert len(h.reply_posts()) == 2  # uploaded again from the fresh store ...
    assert list(h.backend.stored_replies) == [first_reply_id]  # ... but stored exactly once
    assert h.store.backlog_rows()[0].reply_id == first_reply_id
    assert h.backend.locator_history and h.backend.locator_history[0][1] == item.EntryID


# --------------------------------------------------------------------------------------------- race


def test_reply_before_binding_sync_keeps_only_a_locator_then_matches(harness: Harness) -> None:
    inquiry = uuid4()
    worker = harness.worker()
    worker.start()  # no binding for this inquiry yet
    harness.deliver_reply(inquiry, fire_event=True)
    report = worker.tick()
    assert report.events["pending"] == 1
    assert harness.reply_posts() == []
    assert harness.store.pending_count() == 1
    assert harness.store.backlog_rows() == []
    # The binding arrives with the next sync: the locator is re-read and the reply uploaded.
    harness.bind(inquiry)
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.reply_posts()) == 1
    assert harness.store.pending_count() == 0


def test_reply_before_binding_race_window_is_bounded_and_surfaces_a_gap(harness: Harness) -> None:
    inquiry = uuid4()
    worker = harness.worker()
    worker.start()
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    assert harness.store.pending_count() == 1
    harness.advance(timedelta(hours=25))
    worker.tick()
    assert harness.store.pending_count() == 0
    assert harness.store.outcome_counts().get(Outcome.MATCHING_GAP.value) == 1
    assert any(g.kind == "unresolved_reply_matching" for g in harness.store.gaps())
    assert worker.health().backlog.unresolved_matching == 1
    harness.bind(inquiry)  # too late: the gap is surfaced, nothing is uploaded silently
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []


def test_pending_locator_is_refound_after_a_move(harness: Harness) -> None:
    inquiry = uuid4()
    worker = harness.worker()
    worker.start()
    item = harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    harness.outlook.move(item, harness.cars)
    harness.bind(inquiry)
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.reply_posts()) == 1
    assert harness.reply_posts()[0]["source_message"]["outlook_entry_id"] == item.EntryID


# ------------------------------------------------------------------------------------------ privacy


def test_unrelated_personal_mail_never_leaves_the_machine(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    secrets = []

    def personal(subject: str, body: str, **kwargs: object) -> None:
        secrets.extend([subject, body])
        harness.deliver_personal(subject=subject, body=body, **kwargs)  # type: ignore[arg-type]

    personal("Dinner on Friday?", "Private family plans for the weekend at grandma's.")
    personal("Newsletter October", "Our dealership news: winter tyres discount.", sender=SELLER)
    personal(
        f"Question about {LISTING_REF}",
        "Someone else mentions the listing reference only.",
        sender="stranger@private.example.invalid",
    )
    personal(
        "Re: holiday photos",
        "Here are the photos from the lake.",
        in_reply_to="<thread-1@private.example.invalid>",
    )
    meeting = harness.outlook.deliver(
        harness.inbox,
        subject=f"Viewing {LISTING_REF}",
        body="Meeting invitation body",
        sender=SELLER,
        message_id="<meeting@dealer.example.invalid>",
        received_at=harness.clock.now(),
        in_reply_to=inquiry_message_id(inquiry),
        message_class="IPM.Schedule.Meeting.Request",
        fire_event=True,
    )
    secrets.append("Meeting invitation body")
    own = harness.outlook.deliver(
        harness.inbox,
        subject="Re: my own manual answer",
        body="Owner's private manual reply text.",
        sender=OWNER,
        message_id="<own@example.invalid>",
        received_at=harness.clock.now(),
        in_reply_to=inquiry_message_id(inquiry),
        references=(inquiry_message_id(inquiry),),
        fire_event=True,
    )
    secrets.extend(["Re: my own manual answer", "Owner's private manual reply text."])
    harness.deliver_reply(inquiry, fire_event=True, body="Ja, verfügbar. Preis auf Anfrage.")
    worker.tick()
    harness.advance(timedelta(hours=26))  # also expire the foreign-thread locator
    worker.tick()

    posts = harness.reply_posts()
    assert len(posts) == 1
    assert posts[0]["inquiry_id"] == str(inquiry)
    assert posts[0]["headers"]["from"] == SELLER
    everything_sent = json.dumps(
        [posts, harness.backend.heartbeats, harness.backend.account_reports, harness.backend.reports]
    )
    for secret in secrets:
        assert secret not in everything_sent
    local_dump = "\n".join(harness.store._db.iterdump())
    for secret in secrets:
        assert secret not in local_dump  # only hashed keys and outcome codes are kept locally
    assert meeting.EntryID not in harness.outlook.root.item_reads  # non-mail content never read
    counts = harness.store.outcome_counts()
    assert counts[Outcome.UNRELATED.value] >= 5
    assert counts[Outcome.NON_MAIL.value] == 1
    assert own.EntryID in harness.outlook.root.item_reads  # read locally, never uploaded
    assert harness.outlook.send_calls == []


def test_subject_match_alone_never_correlates(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.outlook.deliver(
        harness.inbox,
        subject=f"AW: Anfrage zu Synthetic SUV \u2013 {LISTING_REF}",
        body="Hallo",
        sender="stranger@private.example.invalid",
        message_id="<s@private.example.invalid>",
        received_at=harness.clock.now(),
        fire_event=False,
    )
    harness.worker().start()
    assert harness.reply_posts() == []


@pytest.mark.parametrize(
    ("kwargs", "message_type", "reason"),
    [
        ({"sender": "new-address@dealer.example.invalid"}, "seller_reply", "CHANGED_ADDRESS"),
        ({"subject": f"WG: Anfrage {LISTING_REF}"}, "seller_reply", "FORWARDED"),
        ({"folder_role": "junk"}, "spam", "SPAM_MESSAGE"),
    ],
)
def test_possible_matches_are_uploaded_only_as_quarantined(
    harness: Harness, kwargs: dict[str, str], message_type: str, reason: str
) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    folder = harness.junk if kwargs.pop("folder_role", None) == "junk" else harness.inbox
    harness.deliver_reply(inquiry, fire_event=False, folder=folder, **kwargs)  # type: ignore[arg-type]
    harness.worker().start()
    posts = harness.reply_posts()
    assert len(posts) == 1
    assert posts[0]["correlation_status"] == "quarantined"
    assert reason in posts[0]["correlation_reasons"]
    assert posts[0].get("message_type", "seller_reply") == message_type
    assert harness.store.backlog_rows()[0].acked_at is not None


def test_auto_reply_is_matched_with_its_message_type(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(
        inquiry,
        fire_event=False,
        subject="Abwesenheitsnotiz: Anfrage",
        body="Ich bin bis Montag nicht im Büro.",
        extra_headers={"Auto-Submitted": "auto-replied"},
    )
    harness.worker().start()
    posts = harness.reply_posts()
    assert len(posts) == 1 and posts[0]["message_type"] == "auto_reply"
    assert "correlation_reasons" not in posts[0]


def test_multi_inquiry_ambiguity_stays_local_as_a_matching_gap(harness: Harness) -> None:
    a, b = uuid4(), uuid4()
    harness.bind(a, listing_references=["REF-AAAA"], listing_urls=[])
    harness.bind(b, listing_references=["REF-BBBB"], listing_urls=[])
    harness.outlook.deliver(
        harness.inbox,
        subject="AW: two cars",
        body="Both cars are available.",
        sender=SELLER,
        message_id="<both@dealer.example.invalid>",
        received_at=harness.clock.now(),
        in_reply_to=inquiry_message_id(a),
        references=(inquiry_message_id(a), inquiry_message_id(b)),
        fire_event=False,
    )
    worker = harness.worker()
    worker.start()
    assert harness.reply_posts() == []
    assert any(g.kind == "ambiguous_multi_inquiry" for g in harness.store.gaps())


def test_attachments_upload_metadata_only_and_withhold_identity_documents(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(
        inquiry,
        fire_event=False,
        attachments=(
            AttachmentSpec("CoC.pdf", b"%PDF-1.7 synthetic coc document", mime_type="application/pdf"),
            AttachmentSpec("Reisepass.jpg", b"\xff\xd8synthetic", mime_type="image/jpeg"),
            AttachmentSpec("invoice.exe", b"MZ synthetic", mime_type="application/octet-stream"),
            AttachmentSpec("signature.png", b"png", mime_type="image/png", hidden=True),
        ),
    )
    harness.worker().start()
    posts = harness.reply_posts()
    assert len(posts) == 1
    attachments = posts[0]["attachments"]
    assert [a["filename"] for a in attachments] == ["CoC.pdf"]
    assert attachments[0]["byte_size"] == len(b"%PDF-1.7 synthetic coc document")
    assert len(attachments[0]["sha256"]) == 64 and attachments[0]["sha256"] != "0" * 64
    assert set(attachments[0]) == {"filename", "mime_type", "byte_size", "sha256", "local_ref"}
    assert posts[0]["withheld_sensitive_attachments"] == 1
    dumped = json.dumps(posts)
    assert "Reisepass" not in dumped and "invoice.exe" not in dumped and "synthetic coc" not in dumped


def test_oversized_bodies_and_many_attachments_respect_the_ingest_limits(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    long_body = "Das Fahrzeug ist verfügbar. " + ("Sehr lange Beschreibung des Zustands. " * 6000)
    many = tuple(
        AttachmentSpec(f"CoC-{index}.pdf", f"%PDF synthetic {index}".encode(), mime_type="application/pdf")
        for index in range(25)
    )
    harness.deliver_reply(inquiry, fire_event=False, body=long_body, attachments=many)
    harness.worker().start()
    posts = harness.reply_posts()
    assert len(posts) == 1
    body = json.dumps(posts[0], separators=(",", ":")).encode()
    assert len(body) <= 128 * 1024
    assert len(posts[0]["sanitized_body_text"].encode()) <= 64 * 1024
    assert len(posts[0]["subject"]) <= 512
    assert len(posts[0]["attachments"]) <= 20


def test_non_mail_items_are_skipped_without_reading_content(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    sharing = harness.outlook.deliver(
        harness.inbox,
        subject="Sharing invitation",
        body="calendar sharing",
        sender=SELLER,
        message_id="<share@dealer.example.invalid>",
        received_at=harness.clock.now(),
        in_reply_to=inquiry_message_id(inquiry),
        message_class="IPM.Sharing",
        fire_event=False,
    )
    report = harness.worker().start()
    assert report.scan["non_mail"] == 1
    assert sharing.EntryID not in harness.outlook.root.item_reads
    assert harness.reply_posts() == []


def test_an_item_given_up_after_repeated_failures_is_surfaced_as_a_gap(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After three failed reads an item is no longer retried - but never silently: a gap is kept."""
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.deliver_reply(inquiry, fire_event=False)

    def always_fails(self: LocalMatcher, *args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated unreadable item")

    monkeypatch.setattr(LocalMatcher, "prepare_upload", always_fails)
    outcomes = []
    for _ in range(3):
        harness.advance(_interval(harness))
        report = worker.tick()
        outcomes.append({k: v for k, v in report.scan.items() if k in ("read_failed", "unreadable")})
    assert outcomes == [{"read_failed": 1}, {"read_failed": 1}, {"unreadable": 1}]
    gaps = [g for g in harness.store.gaps() if g.kind == "item_unreadable"]
    assert len(gaps) == 1 and gaps[0].ended_at is not None
    assert harness.reply_posts() == []
    reads = len(harness.outlook.root.item_reads)
    for _ in range(3):  # later overlapping scans (and the daily deep scan) skip it unread
        harness.advance(_interval(harness))
        report = worker.tick()
        assert report.scan.get("unreadable", 0) == 0 and report.scan.get("read_failed", 0) == 0
    harness.advance(timedelta(hours=24))
    worker.tick()
    assert len(harness.outlook.root.item_reads) == reads
    assert len([g for g in harness.store.gaps() if g.kind == "item_unreadable"]) == 1  # no gap flood


def test_catch_up_gap_is_recorded_once_per_streak_not_per_scan(harness: Harness) -> None:
    """A checkpoint held behind the catch-up window (long open detection gap) keeps the reason on the
    checkpoint; the gap itself is recorded once, not as a new row every two minutes."""
    worker = harness.worker()
    worker.start()
    old = harness.clock.now() - timedelta(days=40)
    harness.store._db.execute("update folders set scan_watermark = ?", (ts(old),))
    harness.store.open_gap("outlook_disconnected", old)  # still open: holds every watermark
    for _ in range(4):
        harness.advance(_interval(harness))
        worker.tick()
    catch_up = [g for g in harness.store.gaps() if g.kind == "catchup_window_exceeded"]
    assert len(catch_up) == len(harness.store.folder_checkpoints())  # one per folder, once
    assert all("catchup_window_exceeded" in cp.gap_reasons for cp in harness.store.folder_checkpoints())
