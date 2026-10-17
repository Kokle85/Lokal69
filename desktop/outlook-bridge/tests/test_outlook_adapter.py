"""Outlook Object Model adapter over the fake OOM: scope, identities, headers, attachments, send."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bridge_support import OWNER, SELLER, Harness
from outlook_bridge.config import FolderSpec
from outlook_bridge.errors import FolderScopeError, OutlookUnavailable
from outlook_bridge.outlook_adapter import (
    PR_INTERNET_MESSAGE_ID,
    NewMailEventHandler,
    OutgoingMail,
    SyncEventHandler,
    com_time_as_utc,
    com_time_local,
    identity_hash,
    parse_header_block,
)
from outlook_bridge.testing import AttachmentSpec, inquiry_message_id, make_intent


def _connect(h: Harness) -> None:
    h.session.connect(OWNER)
    h.session.resolve_folders(h.config.folders)


def test_connect_binds_only_the_configured_account(harness: Harness) -> None:
    info = harness.session.connect(OWNER.upper())
    assert info.smtp_address == OWNER
    assert info.account_type == "imap"
    assert info.outlook_version == "16.0.17928.20114"
    assert info.stable_account_key.startswith("store:")
    assert info.store_id_hash == identity_hash("store", harness.account._store.StoreID)
    with pytest.raises(OutlookUnavailable):
        harness.session.connect("absent@example.invalid")


def test_outlook_not_running_is_reported_as_unavailable(harness: Harness) -> None:
    harness.outlook.set_running(False)
    with pytest.raises(OutlookUnavailable):
        harness.session.connect(OWNER)
    state = harness.session.connection_state()
    assert state.outlook_running is False and state.connected is False


def test_folders_resolve_inside_the_configured_store_only(harness: Harness) -> None:
    harness.session.connect(OWNER)
    refs = harness.session.resolve_folders(harness.config.folders)
    assert [r.role for r in refs] == ["inbox", "junk", "rule_target"]
    assert {r.store_id for r in refs} == {harness.account._store.StoreID}
    assert refs[2].entry_id == harness.cars.EntryID
    with pytest.raises(FolderScopeError):
        harness.session.resolve_folders((FolderSpec(role="rule_target", path="Inbox/Missing"),))
    # Two folders with case-variant names make the path ambiguous: refused, never guessed.
    harness.outlook.create_folder(harness.inbox, "cars")
    harness.outlook.create_folder(harness.inbox, "CARS")
    with pytest.raises(FolderScopeError):
        harness.session.resolve_folders((FolderSpec(role="rule_target", path="Inbox/Cars2"),))
    exact = harness.session.resolve_folders(
        (FolderSpec(role="inbox"), FolderSpec(role="rule_target", path="Inbox/Cars"))
    )
    assert exact[1].entry_id == harness.cars.EntryID
    with pytest.raises(FolderScopeError):  # two specs resolving to the same folder
        harness.session.resolve_folders(
            (FolderSpec(role="inbox"), FolderSpec(role="rule_target", path="Inbox"))
        )


def test_items_of_other_accounts_are_ignored_before_any_content_is_read(harness: Harness) -> None:
    _connect(harness)
    other_inbox = harness.outlook.folder(harness.other_account, "inbox")
    foreign = harness.outlook.deliver(
        other_inbox,
        subject="Private",
        body="private body",
        sender="x@private.example.invalid",
        message_id="<p@private.example.invalid>",
        received_at=harness.clock.now(),
        fire_event=False,
    )
    assert harness.session.locate_item(foreign.EntryID, None) is None
    assert harness.session.locate_item(foreign.EntryID, harness.other_account._store.StoreID) is None
    sent_copy = harness.outlook.deliver(
        harness.sent,
        subject="Sent",
        body="sent body",
        sender=OWNER,
        message_id="<s@example.invalid>",
        received_at=harness.clock.now(),
        fire_event=False,
    )
    assert harness.session.locate_item(sent_copy.EntryID, None) is None  # Sent Items is not a reply folder
    assert foreign.EntryID not in harness.outlook.root.item_reads
    assert sent_copy.EntryID not in harness.outlook.root.item_reads


def test_listing_is_bounded_and_ordered(harness: Harness) -> None:
    _connect(harness)
    inbox_ref = harness.session.resolve_folders(harness.config.folders)[0]
    base = harness.clock.now()
    for minutes in (50, 30, 10):
        harness.outlook.deliver(
            harness.inbox,
            subject=f"m{minutes}",
            body="b",
            sender=SELLER,
            message_id=f"<m{minutes}@x.example.invalid>",
            received_at=base - timedelta(minutes=minutes),
            fire_event=False,
        )
    listing = harness.session.list_items(inbox_ref, since=base - timedelta(minutes=40), max_items=10)
    assert [r.received_at for r in listing.refs] == [
        base - timedelta(minutes=30),
        base - timedelta(minutes=10),
    ]
    assert not listing.truncated
    capped = harness.session.list_items(inbox_ref, since=base - timedelta(hours=1), max_items=2)
    assert capped.truncated and len(capped.refs) == 2


def test_snapshot_reads_internet_message_id_and_reply_headers(harness: Harness) -> None:
    _connect(harness)
    inquiry = uuid4()
    item = harness.deliver_reply(inquiry, fire_event=False, message_id="<reply-1@dealer.example.invalid>")
    ref = harness.session.locate_item(item.EntryID, None)
    assert ref is not None and ref.message_class == "IPM.Note"
    snapshot = harness.session.read_item(ref)
    assert snapshot is not None
    assert snapshot.internet_message_id == "<reply-1@dealer.example.invalid>"
    assert snapshot.headers["in-reply-to"] == (inquiry_message_id(inquiry),)
    assert snapshot.headers["from"] == (SELLER,)
    assert snapshot.sender_smtp == SELLER
    assert snapshot.ref.received_at is not None and snapshot.ref.received_at.tzinfo is not None


def test_mapi_reply_properties_are_the_fallback_without_transport_headers(harness: Harness) -> None:
    _connect(harness)
    inquiry = uuid4()
    item = harness.deliver_reply(inquiry, fire_event=False, transport_headers=False)
    ref = harness.session.locate_item(item.EntryID, None)
    assert ref is not None
    snapshot = harness.session.read_item(ref)
    assert snapshot is not None
    assert snapshot.headers["in-reply-to"] == (inquiry_message_id(inquiry),)
    assert snapshot.headers["references"] == (inquiry_message_id(inquiry),)
    assert snapshot.headers["message-id"] == (item.PropertyAccessor._props[PR_INTERNET_MESSAGE_ID],)


def test_moved_items_are_refound_by_internet_message_id_inside_the_scope(harness: Harness) -> None:
    _connect(harness)
    item = harness.deliver_reply(uuid4(), fire_event=False, message_id="<moved@dealer.example.invalid>")
    old_entry = item.EntryID
    harness.outlook.move(item, harness.cars)
    assert harness.session.locate_item(old_entry, None) is None  # EntryID changed with the move
    found = harness.session.find_by_internet_message_id("<moved@dealer.example.invalid>")
    assert found is not None and found.folder_entry_id == harness.cars.EntryID
    harness.outlook.move(item, harness.outlook.folder(harness.other_account, "inbox"))
    assert harness.session.find_by_internet_message_id("<moved@dealer.example.invalid>") is None


def test_attachments_metadata_hidden_parts_and_digests(harness: Harness) -> None:
    _connect(harness)
    item = harness.deliver_reply(
        uuid4(),
        fire_event=False,
        attachments=(
            AttachmentSpec("CoC.pdf", b"%PDF-1.7 synthetic coc", mime_type="application/pdf"),
            AttachmentSpec("logo.png", b"png", mime_type="image/png", hidden=True),
            AttachmentSpec("../../evil.exe", b"MZ"),
        ),
    )
    ref = harness.session.locate_item(item.EntryID, None)
    assert ref is not None
    snapshot = harness.session.read_item(ref)
    assert snapshot is not None
    assert [(a.filename, a.kind) for a in snapshot.attachments] == [
        ("CoC.pdf", "file"),
        ("../../evil.exe", "file"),
    ]
    assert snapshot.attachments[0].mime_type == "application/pdf"
    assert snapshot.attachments[1].mime_type.startswith("application/")  # guessed; never an allowed type
    digests = harness.session.attachment_digests(ref, [0])
    assert digests[0].byte_size == len(b"%PDF-1.7 synthetic coc")
    assert len(digests[0].sha256) == 64
    assert os.listdir(harness.tmp_path / "tmp") == []  # the temporary private copy is removed


def test_new_mail_event_handler_copies_plain_ids_only() -> None:
    received: list[tuple[str, ...]] = []
    handler = NewMailEventHandler()
    handler.OnNewMailEx("A, B ,,C")  # no sink yet: ignored
    handler._bridge_sink = received.append
    handler.OnNewMailEx("A, B ,,C")
    handler.OnNewMailEx(",".join(f"E{i}" for i in range(1000)))
    assert received[0] == ("A", "B", "C")
    assert len(received[1]) == 500


def test_resubscribing_new_mail_never_duplicates_sinks(harness: Harness) -> None:
    _connect(harness)
    calls: list[tuple[str, ...]] = []
    harness.session.subscribe_new_mail(calls.append)
    harness.session.subscribe_new_mail(calls.append)
    harness.outlook.fire_new_mail(("E1",))
    assert calls == [("E1",)]


def test_header_block_parser_is_bounded_and_decodes() -> None:
    headers = parse_header_block(
        "From: =?utf-8?q?M=C3=BCller?= <seller@dealer.example.invalid>\r\n"
        "Subject: Re: test\r\nReferences: <a@x> <b@x>\r\n\r\n"
    )
    assert headers["from"] == ("Müller <seller@dealer.example.invalid>",)
    assert headers["references"] == ("<a@x> <b@x>",)
    assert parse_header_block(None) == {}
    many = "".join(f"X-H{i}: v\r\n" for i in range(500))
    assert len(parse_header_block(many)) == 200


def test_com_time_conversions() -> None:
    naive_utc = datetime(2026, 10, 6, 17, 59)  # PT_SYSTIME values arrive naive (UTC fields)
    assert com_time_as_utc(naive_utc) == datetime(2026, 10, 6, 17, 59, tzinfo=UTC)
    assert com_time_as_utc(datetime(4501, 1, 1)) is None  # Outlook's "no date"
    assert com_time_as_utc("2026-10-06") is None
    aware = datetime(2026, 10, 6, 19, 59, tzinfo=UTC)
    assert com_time_local(aware) == aware


# ------------------------------------------------------------------------------------------ sending


def _mail(h: Harness, **overrides: object) -> OutgoingMail:
    intent = make_intent(
        inquiry_id=uuid4(),
        mailbox_binding_id=h.config.mailbox_binding_id,
        from_address=OWNER,
        created_at=h.clock.now(),
    )
    mail = OutgoingMail.from_intent(intent)
    return OutgoingMail(**{**{f: getattr(mail, f) for f in mail.__slots__}, **overrides})


def test_submit_creates_one_plain_text_mail_through_the_bound_account(harness: Harness) -> None:
    _connect(harness)
    mail = _mail(harness)
    result = harness.session.submit(mail)
    assert result.outcome == "submitted"
    assert result.account_used == OWNER
    assert result.message_id_property_set is True
    queued = harness.outlook.items_in(harness.outbox)
    assert len(queued) == 1
    item = queued[0]
    assert item.Recipients.Count == 1 and item.Recipients.Item(1).Address == mail.to_address
    assert item.CC == "" and item.BCC == "" and item.Attachments.Count == 0
    assert item.BodyFormat == 1
    assert item.SendUsingAccount is harness.account
    assert item.PropertyAccessor._props[PR_INTERNET_MESSAGE_ID] == mail.rfc_message_id
    assert "\r\n" in item._body and "\n\n" not in item._body.replace("\r\n", "")
    lookup = harness.session.lookup_sent(mail, since=harness.clock.now() - timedelta(minutes=1))
    assert lookup.location == "outbox" and lookup.sent_at is None
    harness.outlook.deliver_outbox(harness.account)
    lookup = harness.session.lookup_sent(mail, since=harness.clock.now() - timedelta(minutes=1))
    assert lookup.location == "sent_items" and lookup.internet_message_id == mail.rfc_message_id


def test_submit_refuses_another_sending_account_and_never_switches(harness: Harness) -> None:
    _connect(harness)
    result = harness.session.submit(_mail(harness, from_address="private@other.example.invalid"))
    assert result.outcome == "refused" and result.refusal == "account_mismatch"
    assert harness.outlook.send_calls == []


def test_unresolvable_recipient_is_refused_before_send(harness: Harness) -> None:
    _connect(harness)
    result = harness.session.submit(_mail(harness, to_address="not an address"))
    assert result.outcome == "refused" and result.refusal == "intent_invalid"
    assert result.error_code == "RECIPIENT_UNRESOLVED"
    assert harness.outlook.send_calls == []


def test_send_failure_is_reported_never_retried_here(harness: Harness) -> None:
    _connect(harness)
    harness.outlook.root.send_mode = "raise"
    result = harness.session.submit(_mail(harness))
    assert result.outcome == "send_call_failed"
    assert len(harness.outlook.send_calls) == 1


def test_message_id_not_settable_is_observed_not_fabricated(harness: Harness) -> None:
    _connect(harness)
    harness.outlook.root.message_id_settable = False
    mail = _mail(harness)
    result = harness.session.submit(mail)
    assert result.outcome == "submitted" and result.message_id_property_set is False
    harness.outlook.deliver_outbox(harness.account)
    lookup = harness.session.lookup_sent(mail, since=harness.clock.now() - timedelta(minutes=1))
    assert lookup.location == "sent_items"  # found by inquiry ref + subject + recipient
    assert lookup.internet_message_id is not None and lookup.internet_message_id != mail.rfc_message_id


def test_absent_sent_items_evidence_is_not_found_never_failure(harness: Harness) -> None:
    _connect(harness)
    lookup = harness.session.lookup_sent(_mail(harness), since=harness.clock.now() - timedelta(hours=1))
    assert lookup.location == "not_found" and lookup.matches == 0


# ----------------------------------------------------------------- envelope hardening (review fixes)


def test_recipient_resolved_to_another_address_is_refused_before_send(harness: Harness) -> None:
    """Outlook resolves the string through the address book; a different SMTP address never sends."""
    _connect(harness)
    mail = _mail(harness)
    harness.outlook.root.resolve_overrides[mail.to_address] = "someone-else@other.example.invalid"
    result = harness.session.submit(mail)
    assert result.outcome == "refused" and result.refusal == "intent_invalid"
    assert result.error_code == "RECIPIENT_MISMATCH"
    assert harness.outlook.send_calls == [] and harness.outlook.items_in(harness.outbox) == []


def test_reply_to_resolved_to_another_address_is_refused_before_send(harness: Harness) -> None:
    _connect(harness)
    mail = _mail(harness, reply_to_address=OWNER)
    harness.outlook.root.resolve_overrides[OWNER] = "redirect@other.example.invalid"
    result = harness.session.submit(mail)
    assert result.outcome == "refused" and result.error_code == "REPLY_TO_MISMATCH"
    assert harness.outlook.send_calls == []


def test_matching_reply_to_is_kept_and_sent(harness: Harness) -> None:
    _connect(harness)
    result = harness.session.submit(_mail(harness, reply_to_address=OWNER))
    assert result.outcome == "submitted"
    item = harness.outlook.items_in(harness.outbox)[0]
    assert item.ReplyRecipients.Count == 1 and item.ReplyRecipients.Item(1).Address == OWNER


def test_no_read_or_delivery_receipt_is_requested(harness: Harness) -> None:
    _connect(harness)
    assert harness.session.submit(_mail(harness)).outcome == "submitted"
    item = harness.outlook.items_in(harness.outbox)[0]
    assert item.ReadReceiptRequested is False and item.OriginatorDeliveryReportRequested is False


@pytest.mark.parametrize(
    ("attribute", "value", "code"),
    [
        ("ReadReceiptRequested", True, "TRACKING_REQUESTED"),
        ("OriginatorDeliveryReportRequested", True, "TRACKING_REQUESTED"),
        ("SentOnBehalfOfName", "Somebody Else", "SEND_ON_BEHALF"),
    ],
)
def test_tracking_or_send_on_behalf_that_cannot_be_cleared_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, code: str
) -> None:
    """Item defaults that survive (e.g. a policy forcing receipts) block the send; nothing in
    Outlook's own options is changed."""
    _connect(harness)
    original = type(harness.outlook.app).CreateItem

    def create_item(app: object, kind: int) -> object:
        item = original(app, kind)  # type: ignore[arg-type]
        setattr(item, attribute, value)
        return item

    monkeypatch.setattr(type(harness.outlook.app), "CreateItem", create_item)
    monkeypatch.setattr(type(harness.session), "_clear_tracking", staticmethod(lambda item: None))
    result = harness.session.submit(_mail(harness))
    assert result.outcome == "refused" and result.refusal == "intent_invalid"
    assert result.error_code == code
    assert harness.outlook.send_calls == []


def test_reconnecting_never_piles_up_sync_event_sinks(harness: Harness) -> None:
    for _ in range(5):
        harness.session.connect(OWNER)
    sinks = [h for h in harness.session._state.events if isinstance(h, SyncEventHandler)]
    assert len(sinks) == 1  # one SyncObject in the fake profile: one live sink, not five
