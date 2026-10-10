"""Reply correlation links and uncertain-send reconciliation (spec 37.5, 37.7; 37.10 delta tests).

- Replies map by Message-ID/thread identity, never by subject alone: an automatic (unquarantined)
  match must reference a Message-ID this system sent or published for the inquiry, or be in the
  inquiry's own provider thread. The links are computed by the database, never supplied.
- Only a Message-ID link (In-Reply-To/References or a bounce's returned original) proves that an
  uncertain send was submitted; a thread-only or quarantined possible match never does.
- Reconciliation of an uncertain attempt needs positive evidence in the shape of
  ``domain.inquiries.ReconciliationEvidence``: an empty Sent Items search is never proof of
  non-submission, so it can never enable a resend.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    PROVEN_NOT_SUBMITTED,
    SV_FROZEN,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    attempt_values,
    dispatch,
    expect_sqlstate,
    finish_attempt,
    insert_attempt,
    insert_inquiry,
    insert_reply,
    mailbox,
    outbound_message_id,
    publish_binding,
    queue,
    reply_values,
    reserve,
    seller_entity,
    sent_inquiry,
    state_of,
    update_inquiry,
    with_vehicle,
)

from suv_deals.domain.enums import InquiryState, Tristate
from suv_deals.domain.inquiries import ReconciliationEvidence, reconcile_uncertain

pytestmark = pytest.mark.db

LINK_CK = "seller_replies_link_ck"
PROOF_CK = "email_delivery_attempts_reconciliation_proof_ck"
NO_ACCEPTANCE = "provider acceptance needs"


def _uncertain(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An inquiry whose send timed out after the hand-over, plus its published mailbox binding."""
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain", error_code="PROVIDER_TIMEOUT")
        update_inquiry(db_conn, inquiry, state="uncertain")
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry, state="uncertain")
    return inquiry, attempt, box


def _links(db_conn: psycopg.Connection, reply: uuid.UUID) -> tuple[bool, bool]:
    row = db_conn.execute(
        "select header_linked, thread_linked from app.seller_replies where id = %s", (reply,)
    ).fetchone()
    assert row is not None
    return bool(row[0]), bool(row[1])


UNLINKED: dict[str, Any] = {"in_reply_to": None, "reference_ids": [], "provider_thread_id": None}


# ---------------------------------------------------------------------------------------------
# Never a subject-only match
# ---------------------------------------------------------------------------------------------


def test_subject_only_match_is_never_an_automatic_reply(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    subject = db_conn.execute(
        "select original_subject from app.seller_inquiries where id = %s", (inquiry,)
    ).fetchone()
    assert subject is not None
    same_subject = reply_values(iw, inquiry, box, subject=f"AW: {subject[0]}", **UNLINKED)
    with expect_sqlstate("23514", LINK_CK), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, same_subject)
    # As a quarantined possible match it may be stored for verification, never applied.
    with backend(db_conn, iw.workspace_id):
        possible = insert_reply(
            db_conn,
            {
                **same_subject,
                "correlation_status": "quarantined",
                "quarantined": True,
                "quarantine_reason": "subject_only_possible_match",
            },
        )
    assert _links(db_conn, possible) == (False, False)
    with expect_sqlstate(SV_REFERENCE, "seller reply"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="replied")


def test_reply_referencing_another_inquiry_is_not_linked(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    other_world = with_vehicle(iw, seller=seller_entity(seed, iw.workspace_id))
    other, _ = sent_inquiry(db_conn, other_world)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    foreign = outbound_message_id(other)
    with expect_sqlstate("23514", LINK_CK), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, in_reply_to=foreign, reference_ids=[foreign]))


def test_links_are_computed_by_the_database_and_frozen(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    # A caller claiming a link it does not have is overruled (and therefore refused).
    with expect_sqlstate("23514", LINK_CK), backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn, reply_values(iw, inquiry, box, header_linked=True, thread_linked=True, **UNLINKED)
        )
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box, header_linked=False))
    assert _links(db_conn, reply) == (True, False)
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("update app.seller_replies set header_linked = false where id = %s", (reply,))
    with expect_sqlstate(SV_FROZEN, "immutable"):
        db_conn.execute("update app.seller_replies set thread_linked = true where id = %s", (reply,))


def test_a_verified_match_is_only_reached_by_a_recorded_release(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    with expect_sqlstate("23514", "seller_replies_verified_ck"), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, correlation_status="verified_match", **UNLINKED))


def test_published_binding_ids_link_replies_of_that_version(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """The Message-ID Outlook actually used (observed in Sent Items) is published to the worker;
    a reply to it is linked under that binding version, not under an older one."""
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry, 1)
    observed = "<observed-in-sent-items@synthetic-mail.example>"
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id, inquiry_id,"
            " binding_version,"
            " binding_state, payload) values (%s, %s, %s, 2, 'active', %s)",
            (iw.workspace_id, box, inquiry, Jsonb({"outbound_message_ids": [observed]})),
        )
    with expect_sqlstate("23514", LINK_CK), backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, in_reply_to=observed, reference_ids=[]))
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(
            db_conn, reply_values(iw, inquiry, box, binding_version=2, in_reply_to=observed, reference_ids=[])
        )
    assert _links(db_conn, reply) == (True, False)


# ---------------------------------------------------------------------------------------------
# What resolves an uncertain send
# ---------------------------------------------------------------------------------------------


def test_header_linked_reply_resolves_an_uncertain_send(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _attempt, box = _uncertain(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, in_reply_to=None))  # References only
        update_inquiry(db_conn, inquiry, state="accepted")
    assert state_of(db_conn, inquiry) == "accepted"


def test_thread_only_reply_never_resolves_an_uncertain_send(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _attempt, box = _uncertain(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, provider_thread_id="synthetic-thread-1")
        reply = insert_reply(
            db_conn,
            reply_values(iw, inquiry, box, **{**UNLINKED, "provider_thread_id": "synthetic-thread-1"}),
        )
    assert _links(db_conn, reply) == (False, True)
    with expect_sqlstate(SV_REFERENCE, NO_ACCEPTANCE), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="accepted")
    assert state_of(db_conn, inquiry) == "uncertain"


def test_quarantined_possible_match_never_resolves_an_uncertain_send(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _attempt, box = _uncertain(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        insert_reply(
            db_conn,
            reply_values(
                iw,
                inquiry,
                box,
                correlation_status="quarantined",
                quarantined=True,
                quarantine_reason="changed_sender_address",
            ),
        )
    with expect_sqlstate(SV_REFERENCE, NO_ACCEPTANCE), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="accepted")


def test_bounce_linked_by_its_returned_original_resolves_and_records_the_bounce(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _attempt, box = _uncertain(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        bounce = insert_reply(
            db_conn,
            reply_values(
                iw,
                inquiry,
                box,
                message_type="bounce",
                from_address="mailer-daemon@synthetic-mail.example",
                returned_message_ids=[outbound_message_id(inquiry)],
                **UNLINKED,
            ),
        )
        update_inquiry(db_conn, inquiry, state="accepted")
        update_inquiry(db_conn, inquiry, state="bounced")
    assert _links(db_conn, bounce) == (True, False)
    assert state_of(db_conn, inquiry) == "bounced"


# ---------------------------------------------------------------------------------------------
# Reconciliation evidence
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "evidence"),
    [
        ("proven_not_submitted", None),
        ("proven_not_submitted", {"sent_items": "not_found"}),  # an empty search is not proof
        ("proven_not_submitted", {"sent_items": "not_found", "provider_search": "not_found"}),
        ("proven_not_submitted", {**PROVEN_NOT_SUBMITTED, "worker_alive": "unknown"}),
        ("proven_not_submitted", {**PROVEN_NOT_SUBMITTED, "outbox_pending": "unknown"}),
        ("proven_not_submitted", {**PROVEN_NOT_SUBMITTED, "sent_items": "found"}),
        ("proven_not_submitted", {**PROVEN_NOT_SUBMITTED, "correlated_inbound": True}),
        ("proven_not_submitted", {**PROVEN_NOT_SUBMITTED, "proven_not_submitted": "nothing_in_sent_items"}),
        ("accepted", None),
        ("accepted", {"sent_items": "not_found", "provider_search": "not_found"}),
        ("accepted", {"sent_items": "not_searched", "correlated_inbound": False}),
    ],
)
def test_reconciliation_needs_positive_evidence(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, outcome: str, evidence: dict[str, Any] | None
) -> None:
    inquiry, attempt, _box = _uncertain(db_conn, seed, iw)
    with expect_sqlstate("23514", PROOF_CK), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = %s, reconciled_at = now(),"
            " reconciliation_evidence = %s where id = %s",
            (outcome, None if evidence is None else Jsonb(evidence), attempt),
        )
    assert state_of(db_conn, inquiry) == "uncertain"


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (ReconciliationEvidence(sent_items="found"), InquiryState.ACCEPTED),
        (ReconciliationEvidence(provider_search="found"), InquiryState.ACCEPTED),
        (
            ReconciliationEvidence(
                sent_items="not_found",
                outbox_pending=Tristate.NO,
                worker_alive=Tristate.NO,
                proven_not_submitted="provider_documented_not_sent",
            ),
            InquiryState.FAILED_DEFINITE,
        ),
        (ReconciliationEvidence(sent_items="not_found", provider_search="not_found"), None),
        (
            ReconciliationEvidence(
                sent_items="not_found", proven_not_submitted="connection_refused_before_submit"
            ),
            None,  # the worker may still be alive / the Outbox may still submit
        ),
    ],
)
def test_database_accepts_exactly_the_domain_reconciliation_decisions(
    db_conn: psycopg.Connection,
    seed: Seed,
    iw: InquiryWorld,
    evidence: ReconciliationEvidence,
    expected: InquiryState | None,
) -> None:
    decision = reconcile_uncertain(evidence)
    assert decision.next_state == expected
    inquiry, attempt, _box = _uncertain(db_conn, seed, iw)
    payload = Jsonb(evidence.model_dump(mode="json"))
    statement = (
        "update ops.email_delivery_attempts set reconciled_outcome = %s, reconciled_at = now(),"
        " reconciliation_evidence = %s where id = %s"
    )
    outcomes = {"accepted": InquiryState.ACCEPTED, "proven_not_submitted": InquiryState.FAILED_DEFINITE}
    # Every outcome the domain would not choose is refused (tried first: a reconciliation is
    # recorded only once)...
    for outcome, target in outcomes.items():
        if target != expected:
            with expect_sqlstate("23514", PROOF_CK), backend(db_conn, iw.workspace_id):
                db_conn.execute(statement, (outcome, payload, attempt))
    assert state_of(db_conn, inquiry) == "uncertain"
    # ...and the domain's decision is accepted together with the inquiry transition.
    for outcome, target in outcomes.items():
        if target == expected:
            with backend(db_conn, iw.workspace_id):
                db_conn.execute(statement, (outcome, payload, attempt))
                update_inquiry(db_conn, inquiry, state=target.value)
            assert state_of(db_conn, inquiry) == target.value


def test_reconciliation_citing_an_inbound_message_needs_that_message(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, attempt, box = _uncertain(db_conn, seed, iw)
    evidence = Jsonb(ReconciliationEvidence(correlated_inbound=True).model_dump(mode="json"))
    with expect_sqlstate(SV_REFERENCE, NO_ACCEPTANCE), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = 'accepted', reconciled_at = now(),"
            " reconciliation_evidence = %s where id = %s",
            (evidence, attempt),
        )
        update_inquiry(db_conn, inquiry, state="accepted")
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = 'accepted', reconciled_at = now(),"
            " reconciliation_evidence = %s where id = %s",
            (evidence, attempt),
        )
        update_inquiry(db_conn, inquiry, state="accepted")
    assert state_of(db_conn, inquiry) == "accepted"


# ---------------------------------------------------------------------------------------------
# Leases: an expired attempt may still be running
# ---------------------------------------------------------------------------------------------


def test_send_intent_needs_a_live_lease(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with expect_sqlstate(SV_TRANSITION, "live lease"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(db_conn, attempt_values(iw, inquiry, lease_expires_at=expired))
    assert state_of(db_conn, inquiry) == "queued"


def test_expired_attempt_is_never_finalised_as_a_pre_submission_failure(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    """The reaper cannot turn a crashed attempt into a retryable one: only uncertainty (or
    positive provider evidence) closes an attempt whose worker may still be submitting."""
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with db_conn.transaction():  # TEST ARRANGEMENT: the lease ran out
        db_conn.execute("set local session_replication_role = replica")
        db_conn.execute(
            "update ops.email_delivery_attempts set lease_expires_at = now() - interval '1 minute'"
            " where id = %s",
            (attempt,),
        )
    with expect_sqlstate(SV_TRANSITION, "lease expired"), backend(db_conn, iw.workspace_id):
        finish_attempt(
            db_conn,
            attempt,
            "pre_submission_failure",
            pre_submission_proof="connection_refused_before_submit",
        )
        update_inquiry(db_conn, inquiry, state="failed_definite")
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain", error_code="LEASE_EXPIRED")
        update_inquiry(db_conn, inquiry, state="uncertain")
    assert state_of(db_conn, inquiry) == "uncertain"
