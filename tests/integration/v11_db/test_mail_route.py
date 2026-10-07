"""Local mailbox reply route (spec 37.6-37.8; 37.10 delta tests).

- A mailbox worker binding uses a live credential carrying ONLY ``mail:ingest``; one active
  consumer per mailbox; the mailbox identity is never reassigned; revocation is permanent.
- Binding sync: per-mailbox sequences allocated in commit order, growing binding versions,
  final tombstones, never published to another mailbox.
- Replies: only inquiry-correlated, only for a (possibly) sent inquiry, only through the active
  binding of the inquiry's own sender mailbox under a published, non-tombstoned binding (no
  cross-mailbox injection); dedup by Internet Message-ID / provider id / content fingerprint;
  a conflicting replay is quarantined next to (never over) the original; immutable source.
- Locator history is append-only; ingest dedup and checkpoints are monotonic.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import T0, Seed, backend, sha, unique
from tests.integration.v11_db.support import (
    SENDER_ADDRESS,
    SV_APPEND_ONLY,
    SV_FROZEN,
    SV_MONOTONIC,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    expect_sqlstate,
    insert_inquiry,
    insert_reply,
    insert_row,
    mail_credential,
    mailbox,
    publish_binding,
    reply_values,
    reserve,
    sender_binding,
    sent_inquiry,
    update_inquiry,
)

pytestmark = pytest.mark.db


def _sent_with_mailbox(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> tuple[uuid.UUID, uuid.UUID]:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    return inquiry, box


# ---------------------------------------------------------------------------------------------
# Worker bindings and credentials
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"scopes": ["deals:read"]},
        {"revoked_at": "now", "revoked_by": "uuid", "revoke_reason": "synthetic revocation"},
    ],
)
def test_worker_binding_needs_a_live_mail_ingest_only_credential(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, overrides: dict[str, Any]
) -> None:
    values = dict(overrides)
    if values.get("revoked_at") == "now":
        values["created_at"] = T0
        values["revoked_at"] = T0 + timedelta(hours=1)
        values["revoked_by"] = uuid.uuid4()
    credential = mail_credential(seed, iw.workspace_id, **values)
    with expect_sqlstate(SV_REFERENCE, "only mail:ingest"):
        mailbox(seed, iw, credential_id=credential)


def test_expired_credential_cannot_bind_a_mailbox(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    credential = mail_credential(
        seed,
        iw.workspace_id,
        created_at=datetime.now(UTC) - timedelta(days=10),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    with expect_sqlstate(SV_REFERENCE, "only mail:ingest"):
        mailbox(seed, iw, credential_id=credential)


def test_one_active_consumer_per_mailbox_and_per_credential(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    box = mailbox(seed, iw)
    with expect_sqlstate("23505", "mail_worker_bindings_active_mailbox_uidx"):
        mailbox(seed, iw)
    credential = db_conn.execute(
        "select credential_id from ops.mail_worker_bindings where id = %s", (box,)
    ).fetchone()
    assert credential is not None
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    with expect_sqlstate("23505", "mail_worker_bindings_active_credential_uidx"):
        mailbox(seed, iw, sender_binding_id=other_sender, credential_id=credential[0])


def test_worker_binding_identity_is_never_reassigned(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    box = mailbox(seed, iw)
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    for column, value in (
        ("sender_binding_id", other_sender),
        ("account_address", "moved@synthetic-mail.example"),
        ("store_id_hash", sha("other-store")),
        ("provider", "gmail_api"),
    ):
        with expect_sqlstate(SV_FROZEN, "never reassigned"):
            db_conn.execute(
                f"update ops.mail_worker_bindings set {column} = %s, version = version + 1 where id = %s",
                (value, box),
            )


def test_credential_rotation_and_revocation(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    box = mailbox(seed, iw)
    rotated = mail_credential(seed, iw.workspace_id)
    with expect_sqlstate(SV_MONOTONIC, "advance its version"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set credential_id = %s where id = %s", (rotated, box)
        )
    bad = mail_credential(seed, iw.workspace_id, scopes=["inquiries:read"])
    with expect_sqlstate(SV_REFERENCE, "only mail:ingest"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set credential_id = %s, version = version + 1 where id = %s",
            (bad, box),
        )
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set credential_id = %s, version = version + 1 where id = %s",
            (rotated, box),
        )
        db_conn.execute(
            "update ops.mail_worker_bindings set state = 'revoked', revoked_at = now(), revoked_by = %s,"
            " revoke_reason = 'laptop replaced' where id = %s",
            (uuid.uuid4(), box),
        )
    with expect_sqlstate(SV_FROZEN, "revoked mailbox"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_bindings set state = 'active', revoked_at = null, revoked_by = null,"
            " revoke_reason = null where id = %s",
            (box,),
        )
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("delete from ops.mail_worker_bindings where id = %s", (box,))


# ---------------------------------------------------------------------------------------------
# Binding sync (GET /v1/mail-workers/inquiry-bindings)
# ---------------------------------------------------------------------------------------------


def test_binding_sync_sequences_grow_per_mailbox(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    sequences = [publish_binding(db_conn, iw, box, inquiry, version) for version in (1, 2, 3)]
    assert sequences == [1, 2, 3]
    # Versions only grow; a replayed or older version is refused.
    with expect_sqlstate(SV_MONOTONIC, "binding versions grow"):
        publish_binding(db_conn, iw, box, inquiry, 2)
    # A client-supplied sequence is ignored: the database allocates it.
    with backend(db_conn, iw.workspace_id):
        row = db_conn.execute(
            "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id, sequence, inquiry_id,"
            " binding_version, binding_state, payload) values (%s, %s, 999, %s, 4, 'suppressed', %s)"
            " returning sequence",
            (iw.workspace_id, box, inquiry, Jsonb({"synthetic": True})),
        ).fetchone()
    assert row == (4,)
    assert db_conn.execute(
        "select sync_sequence from ops.mail_worker_bindings where id = %s", (box,)
    ).fetchone() == (4,)


def test_tombstone_is_final_and_has_no_payload(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry, 1)
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id,"
            " inquiry_id, binding_version,"
            " binding_state, payload) values (%s, %s, %s, 2, 'tombstoned', %s)",
            (iw.workspace_id, box, inquiry, Jsonb({"aliases": ["stale"]})),
        )
    publish_binding(db_conn, iw, box, inquiry, 2, state="tombstoned")
    with expect_sqlstate(SV_TRANSITION, "never re-published"):
        publish_binding(db_conn, iw, box, inquiry, 3)
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_binding_sync set binding_state = 'active' where inquiry_id = %s", (inquiry,)
        )
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from ops.mail_binding_sync where inquiry_id = %s", (inquiry,))


def test_bindings_are_published_only_to_the_inquirys_own_mailbox(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    foreign_box = mailbox(
        seed, iw, sender_binding_id=other_sender, account_address="second@synthetic-mail.example"
    )
    with expect_sqlstate(SV_REFERENCE, "mailbox it was sent from"):
        publish_binding(db_conn, iw, foreign_box, inquiry)
    own_box = mailbox(seed, iw)
    db_conn.execute(
        "update ops.mail_worker_bindings set state = 'revoked', revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic revocation' where id = %s",
        (uuid.uuid4(), own_box),
    )
    with expect_sqlstate(SV_TRANSITION, "active mailbox"):
        publish_binding(db_conn, iw, own_box, inquiry)


def test_binding_sync_commit_order_equals_sequence_order(
    db_url: str, db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """A writer that allocated sequence n holds the mailbox row until commit, so a reader that
    saw sequence n+1 can never miss n (no cursor gap)."""
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    allocated = threading.Event()
    release = threading.Event()
    seen: dict[str, Any] = {}

    def slow_writer() -> None:
        with psycopg.connect(db_url, autocommit=True) as conn, backend(conn, iw.workspace_id):
            row = conn.execute(
                "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id,"
                " inquiry_id, binding_version,"
                " binding_state, payload) values (%s, %s, %s, 1, 'active', '{}') returning sequence",
                (iw.workspace_id, box, inquiry),
            ).fetchone()
            seen["slow"] = row
            allocated.set()
            release.wait(timeout=30)

    def fast_writer() -> None:
        allocated.wait(timeout=30)
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute("set lock_timeout = '20s'")
            started = time.monotonic()
            with backend(conn, iw.workspace_id):
                row = conn.execute(
                    "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id, inquiry_id,"
                    " binding_version, binding_state, payload) values (%s, %s, %s, 2, 'active', '{}')"
                    " returning sequence",
                    (iw.workspace_id, box, inquiry),
                ).fetchone()
            seen["fast"] = row
            seen["waited"] = time.monotonic() - started

    slow = threading.Thread(target=slow_writer)
    fast = threading.Thread(target=fast_writer)
    slow.start()
    fast.start()
    allocated.wait(timeout=30)
    time.sleep(0.5)
    assert "fast" not in seen  # blocked behind the uncommitted allocation
    release.set()
    slow.join(timeout=30)
    fast.join(timeout=30)
    assert seen["slow"] == (1,)
    assert seen["fast"] == (2,)
    assert seen["waited"] >= 0.4


# ---------------------------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------------------------


def test_correlated_reply_is_stored_with_its_processing_outputs(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    attachments = [
        {
            "filename": "CoC_synthetic.pdf",
            "mime_type": "application/pdf",
            "byte_size": 123456,
            "sha256": sha("coc"),
            "local_ref": "att-1",
            "action": "allow_vehicle_document",
            "document_kind": "coc",
            "reasons": ["VEHICLE_DOCUMENT"],
        }
    ]
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box, attachments=attachments))
        db_conn.execute(
            "update app.seller_replies set mk_summary = %s, mk_summary_version = 'reply-mk-summary/1',"
            " mk_summary_generated_at = now(), claims = %s, claims_version = 'reply-claims/1',"
            " processing_state = 'processed', processed_at = now() where id = %s",
            ("Синтетички: возилото е достапно.", Jsonb({"availability": "available"}), reply),  # noqa: RUF001
        )
        update_inquiry(db_conn, inquiry, state="replied")
    row = db_conn.execute(
        "select processing_state, quarantined, jsonb_array_length(attachments) from"
        " app.seller_replies where id = %s",
        (reply,),
    ).fetchone()
    assert row == ("processed", False, 1)


def test_reply_for_an_unsent_inquiry_is_refused(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    with expect_sqlstate(SV_REFERENCE, "transmitted"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))


def test_cross_mailbox_injection_is_refused(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _own_box = _sent_with_mailbox(db_conn, seed, iw)
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    foreign_box = mailbox(
        seed, iw, sender_binding_id=other_sender, account_address="second@synthetic-mail.example"
    )
    with (
        expect_sqlstate(SV_REFERENCE, "not the mailbox the inquiry was sent from"),
        backend(db_conn, iw.workspace_id),
    ):
        insert_reply(db_conn, reply_values(iw, inquiry, foreign_box))


def test_reply_needs_a_published_live_binding_version(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with expect_sqlstate(SV_REFERENCE, "never published"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, binding_version=7))
    publish_binding(db_conn, iw, box, inquiry, 2, state="tombstoned")
    # A reply under the still-published version 1 is refused once the binding is revoked.
    with expect_sqlstate(SV_TRANSITION, "tombstoned"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))
    with expect_sqlstate(SV_REFERENCE, "tombstone"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, binding_version=2))


def test_revoked_mailbox_cannot_ingest(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    db_conn.execute(
        "update ops.mail_worker_bindings set state = 'revoked', revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic revocation' where id = %s",
        (uuid.uuid4(), box),
    )
    with expect_sqlstate(SV_TRANSITION, "revoked"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))


def test_reply_dedup_unique_keys(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    imid = "<dedup-1@synthetic-dealer.example>"
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, internet_message_id=imid))
    # The same Internet Message-ID in the same mailbox (e.g. after a folder move): one record.
    with expect_sqlstate("23505", "seller_replies_message_uidx"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, internet_message_id=imid))
    # Without a Message-ID, the provider message id is the key...
    with backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, internet_message_id=None, provider_message_id="prov-1")
        )
    with expect_sqlstate("23505", "seller_replies_provider_message_uidx"), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, internet_message_id=None, provider_message_id="prov-1")
        )
    # ...and without both, the immutable content fingerprint.
    fingerprint = sha("same immutable content")
    with backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, internet_message_id=None, source_fingerprint=fingerprint)
        )
    with expect_sqlstate("23505", "seller_replies_fingerprint_uidx"), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, internet_message_id=None, source_fingerprint=fingerprint)
        )


def test_conflicting_replay_is_quarantined_beside_the_original(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    imid = "<conflict-1@synthetic-dealer.example>"
    with backend(db_conn, iw.workspace_id):
        original = insert_reply(db_conn, reply_values(iw, inquiry, box, internet_message_id=imid))
    conflicting = reply_values(
        iw,
        inquiry,
        box,
        internet_message_id=imid,
        sanitized_body="Synthetic: a DIFFERENT body under the same id.",
        conflict_of_reply_id=original,
    )
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, conflicting)  # must be quarantined
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, {**conflicting, "quarantined": True, "quarantine_reason": "possible_match"})
    with backend(db_conn, iw.workspace_id):
        conflict = insert_reply(
            db_conn, {**conflicting, "quarantined": True, "quarantine_reason": "idempotency_conflict"}
        )
    # The same conflicting content is recorded once; the original is untouched.
    with expect_sqlstate("23505", "seller_replies_conflict_uidx"), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, {**conflicting, "quarantined": True, "quarantine_reason": "idempotency_conflict"}
        )
    with expect_sqlstate(SV_TRANSITION, "stay quarantined"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_replies set quarantined = false, quarantine_reason = null,"
            " correlation_status = 'verified_match', quarantine_released_at = now(),"
            " quarantine_released_by = %s, quarantine_release_reason = 'looked fine' where id = %s",
            (uuid.uuid4(), conflict),
        )
    body = db_conn.execute(
        "select sanitized_body from app.seller_replies where id = %s", (original,)
    ).fetchone()
    assert body == ("Synthetic fixture only: the vehicle is available.",)


def test_reply_source_content_is_immutable(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
    for column, value in (
        ("sanitized_body", "overwritten"),
        ("internet_message_id", "<other@synthetic-dealer.example>"),
        ("source_fingerprint", sha("other")),
        ("inquiry_id", uuid.uuid4()),
    ):
        with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
            db_conn.execute(f"update app.seller_replies set {column} = %s where id = %s", (value, reply))
        with expect_sqlstate(SV_FROZEN, "immutable"):
            db_conn.execute(f"update app.seller_replies set {column} = %s where id = %s", (value, reply))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from app.seller_replies where id = %s", (reply,))


def test_ambiguous_reply_is_quarantined_until_a_recorded_verification(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    ambiguous = reply_values(iw, inquiry, box, message_type="ambiguous", correlation_status="quarantined")
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, ambiguous)
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, message_type="spam"))
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(
            db_conn, {**ambiguous, "quarantined": True, "quarantine_reason": "forwarded_reply"}
        )
    # A quarantined possible match is not evidence: the inquiry cannot become replied from it.
    with expect_sqlstate(SV_REFERENCE, "seller reply"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="replied")
    release = (
        "update app.seller_replies set quarantined = false, quarantine_reason = null,"
        " correlation_status = %s,"
        " message_type = %s, quarantine_released_at = %s, quarantine_released_by = %s,"
        " quarantine_release_reason = %s where id = %s"
    )
    with expect_sqlstate(SV_TRANSITION, "recorded verification"), backend(db_conn, iw.workspace_id):
        db_conn.execute(release, ("verified_match", "seller_reply", None, None, None, reply))
    with expect_sqlstate(SV_FROZEN, "message type"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            release,
            ("verified_match", "spam", datetime.now(UTC), uuid.uuid4(), "owner verified sender", reply),
        )
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            release,
            (
                "verified_match",
                "seller_reply",
                datetime.now(UTC),
                uuid.uuid4(),
                "owner verified the sender",
                reply,
            ),
        )
        update_inquiry(db_conn, inquiry, state="replied")
    with expect_sqlstate(SV_FROZEN, "recorded once"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_replies set quarantined = true, quarantine_reason = 'again' where id = %s",
            (reply,),
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"filename": "../../etc/passwd", "mime_type": "application/pdf", "byte_size": 1, "sha256": "0" * 64},
        {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "byte_size": 1,
            "sha256": "0" * 64,
            "local_ref": "https://attacker.example/x",
        },
        {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "byte_size": 1,
            "sha256": "0" * 64,
            "url": "https://attacker.example/x",
        },
        {"filename": "doc.pdf", "mime_type": "not a mime", "byte_size": 1, "sha256": "0" * 64},
        {"filename": "doc.pdf", "mime_type": "application/pdf", "byte_size": -1, "sha256": "0" * 64},
        {"filename": "doc.pdf", "mime_type": "application/pdf", "byte_size": 1, "sha256": "XYZ"},
        {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "byte_size": 1,
            "sha256": "0" * 64,
            "bytes_base64": "JVBERi0=",
        },
        {"filename": "a\\b.pdf", "mime_type": "application/pdf", "byte_size": 1, "sha256": "0" * 64},
        {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "byte_size": 1,
            "sha256": "0" * 64,
            "action": "forward_to_slack",
        },
    ],
)
def test_attachment_metadata_rejects_paths_urls_and_bytes(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, bad: dict[str, Any]
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with expect_sqlstate("23514", "seller_replies_attachments_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, attachments=[bad]))


def test_reply_size_limits(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    entry = {"filename": "p.jpg", "mime_type": "image/jpeg", "byte_size": 10, "sha256": "a" * 64}
    with expect_sqlstate("23514", "seller_replies_attachments_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, attachments=[entry] * 21))
    with backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, attachments=[entry] * 20, sanitized_body="x" * 65536)
        )
    with expect_sqlstate("23514", "seller_replies_body_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, sanitized_body="ä" * 32769))
    with expect_sqlstate("23514", "seller_replies_subject_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, subject="Re: x\r\nBcc: victim@example.invalid"))
    with expect_sqlstate("23514", "seller_replies_subject_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, subject="s" * 513))
    with expect_sqlstate("23514", "seller_replies_message_id_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, internet_message_id="no-brackets@example.invalid")
        )
    with expect_sqlstate("23514", "seller_replies_references_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, reference_ids=["<ok@example.invalid>", "bad ref"])
        )


# ---------------------------------------------------------------------------------------------
# Locators, ingest dedup and checkpoints
# ---------------------------------------------------------------------------------------------


def _locator(iw: InquiryWorld, reply: uuid.UUID, box: uuid.UUID, entry: str, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "reply_id": reply,
        "mailbox_binding_id": box,
        "outlook_entry_id": entry,
        "outlook_store_id": "SYNTHETICSTORE01",
        "folder_id": "SYNTHETICFOLDERINBOX",
        "seen_at": T0 + timedelta(days=1),
    }
    values.update(cols)
    return values


def test_locator_history_is_append_only(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        first = insert_row(db_conn, "app.seller_reply_locators", _locator(iw, reply, box, "ENTRY0001"))
        # A move to another folder is a new locator row, the reply id stays the same.
        insert_row(
            db_conn,
            "app.seller_reply_locators",
            _locator(iw, reply, box, "ENTRY0002", folder_id="SYNTHETICFOLDERCARS"),
        )
    with expect_sqlstate("23505", "seller_reply_locators_uidx"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.seller_reply_locators", _locator(iw, reply, box, "ENTRY0001"))
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("update app.seller_reply_locators set folder_id = 'X' where id = %s", (first,))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("update app.seller_reply_locators set folder_id = 'X' where id = %s", (first,))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from app.seller_reply_locators where id = %s", (first,))
    # The locator must be recorded against the reply's own mailbox.
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    foreign = mailbox(
        seed, iw, sender_binding_id=other_sender, account_address="second@synthetic-mail.example"
    )
    with expect_sqlstate("23503"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.seller_reply_locators", _locator(iw, reply, foreign, "ENTRY0003"))
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.seller_reply_locators", _locator(iw, reply, box, "ENTRY WITH SPACE"))


def _dedup(
    iw: InquiryWorld, box: uuid.UUID, credential: uuid.UUID, inquiry: uuid.UUID, reply: uuid.UUID, **cols: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "mailbox_binding_id": box,
        "credential_id": credential,
        "dedup_kind": "internet_message_id",
        "dedup_key": f"{box}:internet_message_id:<dedup@synthetic-dealer.example>",
        "idempotency_key": unique("idem-key"),
        "fingerprint": sha("fp"),
        "fingerprint_version": "reply-source-fingerprint/1",
        "inquiry_id": inquiry,
        "reply_id": reply,
        "ingest_result": "stored",
    }
    values.update(cols)
    return values


def test_ingest_dedup_identity_and_replay_counters(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    credential = db_conn.execute(
        "select credential_id from ops.mail_worker_bindings where id = %s", (box,)
    ).fetchone()
    assert credential is not None
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        second_reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        dedup = insert_row(
            db_conn,
            "ops.mail_ingest_dedup",
            _dedup(iw, box, credential[0], inquiry, reply, idempotency_key="idem-key-0001"),
        )
    # Same source identity again, or the same idempotency key for another message: unique.
    with expect_sqlstate("23505", "mail_ingest_dedup_key_uk"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "ops.mail_ingest_dedup", _dedup(iw, box, credential[0], inquiry, second_reply))
    with expect_sqlstate("23505", "mail_ingest_dedup_idempotency_uk"), backend(db_conn, iw.workspace_id):
        insert_row(
            db_conn,
            "ops.mail_ingest_dedup",
            _dedup(
                iw,
                box,
                credential[0],
                inquiry,
                second_reply,
                idempotency_key="idem-key-0001",
                dedup_key=f"{box}:internet_message_id:<other@synthetic-dealer.example>",
            ),
        )
    # The dedup key is scoped to its own mailbox and kind.
    with expect_sqlstate("23514", "mail_ingest_dedup_key_ck"), backend(db_conn, iw.workspace_id):
        insert_row(
            db_conn,
            "ops.mail_ingest_dedup",
            _dedup(
                iw,
                box,
                credential[0],
                inquiry,
                second_reply,
                dedup_key=f"{uuid.uuid4()}:internet_message_id:<x@y.z>",
            ),
        )
    # A replay increments counters; a conflict records its fingerprint; identity stays frozen.
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_ingest_dedup set duplicate_count = duplicate_count + 1, last_seen_at = now(),"
            " conflict_count = 1, last_conflict_at = now(), last_conflict_fingerprint = %s where id = %s",
            (sha("conflicting"), dedup),
        )
    with expect_sqlstate(SV_MONOTONIC), backend(db_conn, iw.workspace_id):
        db_conn.execute("update ops.mail_ingest_dedup set duplicate_count = 0 where id = %s", (dedup,))
    with expect_sqlstate(SV_FROZEN):
        db_conn.execute("update ops.mail_ingest_dedup set fingerprint = %s where id = %s", (sha("x"), dedup))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from ops.mail_ingest_dedup where id = %s", (dedup,))


def test_ingest_is_recorded_only_with_the_mailboxs_current_credential(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
    stale = mail_credential(seed, iw.workspace_id)
    with expect_sqlstate(SV_REFERENCE, "own credential"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "ops.mail_ingest_dedup", _dedup(iw, box, stale, inquiry, reply))


def _checkpoint(iw: InquiryWorld, box: uuid.UUID, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "mailbox_binding_id": box,
        "store_id_hash": sha("synthetic-store"),
        "folder_id_hash": sha("synthetic-inbox"),
        "folder_role": "inbox",
        "last_complete_scan_at": T0,
        "heartbeat_at": T0,
        "backlog_count": 0,
    }
    values.update(cols)
    return values


def test_checkpoints_never_move_backwards(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    box = mailbox(seed, iw)
    with backend(db_conn, iw.workspace_id):
        checkpoint = insert_row(db_conn, "ops.mail_worker_checkpoints", _checkpoint(iw, box))
        db_conn.execute(
            "update ops.mail_worker_checkpoints set last_complete_scan_at = %s, overlap_watermark = %s,"
            " heartbeat_at = now(), backlog_count = 3, backlog_oldest_at = %s, row_version = row_version + 1"
            " where id = %s",
            (T0 + timedelta(minutes=2), T0, T0, checkpoint),
        )
    with expect_sqlstate(SV_MONOTONIC, "never moves backwards"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_checkpoints set last_complete_scan_at = %s where id = %s",
            (T0, checkpoint),
        )
    with expect_sqlstate("23505"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "ops.mail_worker_checkpoints", _checkpoint(iw, box))
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "ops.mail_worker_checkpoints", _checkpoint(iw, box, folder_id_hash="Inbox"))
    db_conn.execute(
        "update ops.mail_worker_bindings set state = 'revoked', revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic revocation' where id = %s",
        (uuid.uuid4(), box),
    )
    with expect_sqlstate(SV_TRANSITION, "revoked"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.mail_worker_checkpoints set heartbeat_at = now() where id = %s", (checkpoint,)
        )


def test_mail_ingest_credential_cannot_carry_other_scopes(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    for scopes, role in (
        (["mail:ingest", "deals:read"], "owner"),
        (["mail:ingest", "inquiries:read"], "owner"),
        (["mail:ingest"], "reviewer"),
        (["mail:ingest"], "viewer"),
    ):
        with expect_sqlstate("23514"):
            mail_credential(seed, iw.workspace_id, scopes=scopes, role=role)
    mail_credential(seed, iw.workspace_id, scopes=["mail:ingest"], role="owner")
    assert SENDER_ADDRESS.endswith(".example")
