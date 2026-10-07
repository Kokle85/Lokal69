"""Seller inquiry state machine, binding immutability and evidence (spec 37.5; 37.10 delta tests).

- The transition graph is exactly ``domain.inquiries.ALLOWED_TRANSITIONS`` (exhaustive 15 x 15).
- The legitimate flow candidate -> ... -> replied works as ``suv_backend`` with the grants.
- An uncertain send can never be re-queued, cancelled or retried without proof; an empty search
  result cannot release the reservation; a crashed ``sending`` attempt blocks any resend.
- The binding (template, body, scope, sender, recipient, qualification) is immutable once
  reserved; identity is always immutable; provider references are set once.
- Every state carries its evidence at commit (quota debit, send intent, acceptance/failure
  evidence, correlated reply).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import T0, Seed, backend, sha
from tests.integration.v11_db.support import (
    ALL_STATES,
    SV_APPEND_ONLY,
    SV_FROZEN,
    SV_MONOTONIC,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    accept,
    attempt_values,
    audit_event,
    binding_values,
    contact,
    debit,
    dispatch,
    expect_sqlstate,
    finish_attempt,
    fresh_seller,
    insert_attempt,
    insert_inquiry,
    insert_reply,
    mailbox,
    publish_binding,
    queue,
    reply_values,
    reserve,
    seller_entity,
    sender_binding,
    sent_inquiry,
    state_of,
    update_inquiry,
)

from suv_deals.domain.enums import InquiryState
from suv_deals.domain.inquiries import ALLOWED_TRANSITIONS

pytestmark = pytest.mark.db

GRAPH_REFUSAL = "is not permitted"


# ---------------------------------------------------------------------------------------------
# The full legitimate flow
# ---------------------------------------------------------------------------------------------


def test_full_flow_as_backend_reaches_replied(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw, state="candidate")
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="qualifying")
    reserve(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "reserved"
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    row = db_conn.execute(
        "select state, reserved_at is not null, queued_at is not null, send_attempted_at is not null"
        " from app.seller_inquiries where id = %s",
        (inquiry,),
    ).fetchone()
    assert row == ("sending", True, True, True)
    accept(db_conn, iw, inquiry, attempt)
    assert state_of(db_conn, inquiry) == "accepted"

    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))
        update_inquiry(db_conn, inquiry, state="replied")
    row = db_conn.execute(
        "select state, replied_at is not null, accepted_at is not null from"
        " app.seller_inquiries where id = %s",
        (inquiry,),
    ).fetchone()
    assert row == ("replied", True, True)
    # A seller who later asks not to be contacted again is still recorded.
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="seller_opted_out")
    assert state_of(db_conn, inquiry) == "seller_opted_out"


def test_inquiries_are_created_before_reservation_only(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for state in ("reserved", "queued", "sending", "accepted", "uncertain"):
        with expect_sqlstate(SV_TRANSITION, "created as candidate"):
            insert_inquiry(db_conn, iw, state=state, **binding_values(iw))
    for state in ("candidate", "qualifying", "held_facts"):
        inquiry = insert_inquiry(db_conn, fresh_seller(iw), state=state)
        assert state_of(db_conn, inquiry) == state


# ---------------------------------------------------------------------------------------------
# Exhaustive transition matrix (mirrors domain.inquiries.ALLOWED_TRANSITIONS)
# ---------------------------------------------------------------------------------------------

_POST_RESERVED = {
    "reserved",
    "queued",
    "sending",
    "accepted",
    "uncertain",
    "failed_definite",
    "replied",
    "bounced",
    "seller_opted_out",
    "no_reply_yet",
}


def _row_in_state(seed: Seed, conn: psycopg.Connection, world: InquiryWorld, state: str) -> uuid.UUID:
    """Arrange (only) an inquiry directly in ``state``; triggers are bypassed for setup."""
    seller = seller_entity(seed, world.workspace_id)
    contact_id, address = contact(
        seed, world.workspace_id, world.vehicle, seller, status="unverified", verified_at=None
    )
    local = InquiryWorld(
        world.workspace_id,
        seed,
        world.vehicle,
        seller,
        contact_id,
        address,
        world.authorization_id,
        world.sender_binding_id,
        world.controls_id,
    )
    now = datetime.now(UTC)
    cols: dict[str, Any] = {}
    if state in _POST_RESERVED or state in ("cancelled", "suppressed"):
        cols.update(binding_values(local))
        cols["reserved_at"] = now
    if state in _POST_RESERVED - {"reserved"}:
        cols["queued_at"] = now
    if state in _POST_RESERVED - {"reserved", "queued"}:
        cols["send_attempted_at"] = now
    if state in ("accepted", "replied", "bounced", "seller_opted_out", "no_reply_yet"):
        cols["accepted_at"] = now
    if state == "replied":
        cols["replied_at"] = now
    if state == "suppressed":
        cols["suppression_reason"] = "manual"
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        return insert_inquiry(conn, local, state=state, as_backend=False, **cols)


def test_transition_matrix_matches_the_domain_graph(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    domain = {s.value: {t.value for t in targets} for s, targets in ALLOWED_TRANSITIONS.items()}
    assert set(domain) == set(ALL_STATES) == {s.value for s in InquiryState}
    rows = {state: _row_in_state(seed, db_conn, iw, state) for state in ALL_STATES}
    for source, inquiry in rows.items():
        for target in ALL_STATES:
            if target == source:
                continue
            extra: dict[str, Any] = {"suppression_reason": "manual"} if target == "suppressed" else {}
            if source == "suppressed":
                extra["suppression_reason"] = None
            with db_conn.transaction(force_rollback=True):
                try:
                    with db_conn.transaction():
                        update_inquiry(db_conn, inquiry, state=target, **extra)
                    refused_by_graph = False
                except psycopg.Error as exc:
                    refused_by_graph = exc.sqlstate == SV_TRANSITION and GRAPH_REFUSAL in str(exc)
            assert refused_by_graph == (target not in domain[source]), (source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("accepted", "queued"),
        ("uncertain", "queued"),
        ("uncertain", "cancelled"),
        ("uncertain", "sending"),
        ("sending", "queued"),
        ("sending", "cancelled"),
        ("replied", "accepted"),
        ("bounced", "queued"),
        ("seller_opted_out", "qualifying"),
        ("candidate", "reserved"),
        ("cancelled", "reserved"),
        ("failed_definite", "sending"),
    ],
)
def test_named_illegal_transitions_are_refused(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, source: str, target: str
) -> None:
    inquiry = _row_in_state(seed, db_conn, iw, source)
    with expect_sqlstate(SV_TRANSITION, GRAPH_REFUSAL), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state=target)
    assert state_of(db_conn, inquiry) == source


# ---------------------------------------------------------------------------------------------
# Uncertain sends: never blindly retried
# ---------------------------------------------------------------------------------------------


def _uncertain(db_conn: psycopg.Connection, iw: InquiryWorld) -> tuple[uuid.UUID, uuid.UUID]:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    # Provider timeout after possible acceptance (or a crash): the reaper records uncertainty.
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain", error_code="PROVIDER_TIMEOUT")
        update_inquiry(db_conn, inquiry, state="uncertain")
    return inquiry, attempt


def test_uncertain_send_is_never_requeued_cancelled_or_resent(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry, _attempt = _uncertain(db_conn, iw)
    for target in ("queued", "cancelled", "sending", "suppressed", "qualifying"):
        with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, inquiry, state=target)
    # No new send attempt can be recorded either (another account, or the same one).
    with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
        insert_attempt(db_conn, attempt_values(iw, inquiry, 2))
    other_sender = sender_binding(
        iw.seed, iw.workspace_id, from_address="second@synthetic-mail.example", account_id="other"
    )
    with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
        insert_attempt(db_conn, attempt_values(iw, inquiry, 2, sender_binding_id=other_sender))
    # The quota debit is retained.
    with expect_sqlstate(SV_TRANSITION, "never-transmitted"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'TRY_RELEASE'"
            " where inquiry_id = %s",
            (inquiry,),
        )
    assert db_conn.execute(
        "select count(*) from ops.inquiry_quota_ledger where inquiry_id = %s and released_at is null",
        (inquiry,),
    ).fetchone() == (1,)
    assert state_of(db_conn, inquiry) == "uncertain"


def test_empty_sent_items_search_cannot_release_the_reservation(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry, attempt = _uncertain(db_conn, iw)
    # Reconciliation found nothing in Sent Items: that is not proof, so failed_definite is refused.
    with expect_sqlstate(SV_REFERENCE, "proof of non-submission"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="failed_definite")
    # Nor can the empty search be recorded as an outcome change of the finalised attempt.
    with expect_sqlstate(SV_FROZEN), backend(db_conn, iw.workspace_id):
        finish_attempt(
            db_conn, attempt, "pre_submission_failure", pre_submission_proof="provider_documented_not_sent"
        )
    assert state_of(db_conn, inquiry) == "uncertain"


def test_uncertain_resolves_only_on_positive_evidence(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry, attempt = _uncertain(db_conn, iw)
    # Found in Sent Items/provider: reconciled acceptance, then accepted.
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = 'accepted', reconciled_at = now(),"
            " reconciliation_evidence = %s where id = %s",
            (Jsonb({"sent_items": "found"}), attempt),
        )
        update_inquiry(db_conn, inquiry, state="accepted")
    assert state_of(db_conn, inquiry) == "accepted"
    # The reconciliation is recorded once.
    with expect_sqlstate(SV_FROZEN), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome ="
            " 'proven_not_submitted' where id = %s",
            (attempt,),
        )


def test_uncertain_accepted_by_a_correlated_reply(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _attempt = _uncertain(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry, state="uncertain")
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box))
        update_inquiry(db_conn, inquiry, state="accepted")
        update_inquiry(db_conn, inquiry, state="replied")
    assert state_of(db_conn, inquiry) == "replied"


def test_proven_non_submission_allows_one_guarded_retry_on_the_same_account(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry, attempt = _uncertain(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set reconciled_outcome = 'proven_not_submitted',"
            " reconciled_at = now() where id = %s",
            (attempt,),
        )
        update_inquiry(db_conn, inquiry, state="failed_definite")
    queue(db_conn, iw, inquiry)
    # Attempt numbers are consecutive and the fencing token grows.
    with expect_sqlstate(SV_TRANSITION, "consecutive"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(db_conn, attempt_values(iw, inquiry, 3, fencing_token=9))
    with expect_sqlstate(SV_MONOTONIC, "fencing"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(db_conn, attempt_values(iw, inquiry, 2, fencing_token=1))
    second = dispatch(db_conn, iw, inquiry, number=2)
    accept(db_conn, iw, inquiry, second)
    assert state_of(db_conn, inquiry) == "accepted"


def test_definite_rejection_is_never_retried(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "definite_rejection", error_code="RECIPIENT_REJECTED")
        update_inquiry(db_conn, inquiry, state="failed_definite")
    with expect_sqlstate(SV_TRANSITION, "never reached the provider"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="queued")


def test_pre_submission_failure_needs_proof(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with expect_sqlstate(SV_REFERENCE, "proof of non-submission"), backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "pre_submission_failure")
        update_inquiry(db_conn, inquiry, state="failed_definite")
    with backend(db_conn, iw.workspace_id):
        finish_attempt(
            db_conn,
            attempt,
            "pre_submission_failure",
            pre_submission_proof="connection_refused_before_submit",
        )
        update_inquiry(db_conn, inquiry, state="failed_definite")
    queue(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "queued"


def test_attempts_are_exhausted_after_three(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    for number in (1, 2, 3):
        attempt = dispatch(db_conn, iw, inquiry, number=number)
        with backend(db_conn, iw.workspace_id):
            finish_attempt(
                db_conn,
                attempt,
                "pre_submission_failure",
                pre_submission_proof="credentials_rejected_before_submit",
            )
            update_inquiry(db_conn, inquiry, state="failed_definite")
        if number < 3:
            queue(db_conn, iw, inquiry)
    with expect_sqlstate(SV_TRANSITION, "exhausted"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="queued")


def test_crashed_sending_attempt_blocks_any_second_send(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    dispatch(db_conn, iw, inquiry)
    # The worker crashed after the send intent: nothing may start a second transmission.
    with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="queued")
    with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
        insert_attempt(db_conn, attempt_values(iw, inquiry, 2))
    # The reaper may only record uncertainty, finalising the running attempt at the same time.
    with expect_sqlstate(SV_REFERENCE, "running send attempt"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="uncertain")
    assert state_of(db_conn, inquiry) == "sending"


# ---------------------------------------------------------------------------------------------
# Evidence at commit
# ---------------------------------------------------------------------------------------------


def test_reservation_needs_a_quota_debit(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_REFERENCE, "quota debit"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="reserved", **binding_values(iw))
    assert state_of(db_conn, inquiry) == "qualifying"


def test_sending_needs_the_committed_send_intent(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    with expect_sqlstate(SV_REFERENCE, "send intent"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
    # A send intent cannot be recorded for an inquiry that is not sending.
    with expect_sqlstate(SV_TRANSITION, "moved to sending"), backend(db_conn, iw.workspace_id):
        insert_attempt(db_conn, attempt_values(iw, inquiry))
    # Nor as anything but a running intent.
    with expect_sqlstate(SV_TRANSITION, "running send intent"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(
            db_conn,
            attempt_values(iw, inquiry, outcome="accepted", finished_at=datetime.now(UTC) + timedelta(1)),
        )


def test_acceptance_needs_provider_evidence(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with expect_sqlstate(SV_REFERENCE), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="accepted")
    # An attempt left running while the inquiry moves on is refused at commit too.
    with expect_sqlstate(SV_REFERENCE), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="uncertain")
    # Finalising the attempt without moving the inquiry leaves an inconsistent pair: refused.
    with expect_sqlstate(SV_REFERENCE, "send intent"), backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "accepted")
    accept(db_conn, iw, inquiry, attempt)


def test_replied_needs_a_correlated_seller_reply(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    with expect_sqlstate(SV_REFERENCE, "seller reply"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="replied")
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="no_reply_yet")
    assert state_of(db_conn, inquiry) == "no_reply_yet"


def test_cancelling_a_reservation_releases_its_debit(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    with expect_sqlstate(SV_REFERENCE, "release its quota debit"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="cancelled", state_reasons=["PRICE_CHANGED"])
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="cancelled", state_reasons=["PRICE_CHANGED"])
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'CANCELLED_UNSENT'"
            " where inquiry_id = %s and released_at is null",
            (inquiry,),
        )
    # A never-transmitted cancellation is re-qualified, never duplicated.
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="qualifying")
    reserve(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "reserved"


def test_suppressed_inquiry_requalifies_only_with_an_audit_event(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="suppressed", suppression_reason="manual")
    with expect_sqlstate(SV_TRANSITION, "audit event"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="qualifying", suppression_reason=None)
    unrelated = audit_event(
        seed, iw.workspace_id, "seller_inquiry", uuid.uuid4(), "seller_inquiry.requalified"
    )
    with expect_sqlstate(SV_TRANSITION, "audit event"), backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, state="qualifying", suppression_reason=None, requalification_audit_id=unrelated
        )
    audit = audit_event(seed, iw.workspace_id, "seller_inquiry", inquiry, "seller_inquiry.requalified")
    with backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, state="qualifying", suppression_reason=None, requalification_audit_id=audit
        )
    assert state_of(db_conn, inquiry) == "qualifying"


def test_transmitted_inquiries_never_requalify(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    # Arrange a suppressed record that had a send attempt (possible after a pre-submission failure).
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        finish_attempt(
            db_conn,
            attempt,
            "pre_submission_failure",
            pre_submission_proof="connection_refused_before_submit",
        )
        update_inquiry(db_conn, inquiry, state="failed_definite")
    queue(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="suppressed", suppression_reason="kill_switch")
    audit = audit_event(seed, iw.workspace_id, "seller_inquiry", inquiry, "seller_inquiry.requalified")
    with expect_sqlstate(SV_TRANSITION, "never be re-qualified"), backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, state="qualifying", suppression_reason=None, requalification_audit_id=audit
        )
    # Its debit is retained (an attempt exists).
    with expect_sqlstate(SV_TRANSITION), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'X_RELEASE'"
            " where inquiry_id = %s",
            (inquiry,),
        )


# ---------------------------------------------------------------------------------------------
# Binding immutability and identity
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("template_id", "seller_initial_it_v1"),
        ("original_body", "Changed body"),
        ("body_hash", sha("other")),
        ("scope_hash", sha("other-scope")),
        ("sender_binding_version", 2),
        ("sender_from_address", "other@synthetic-mail.example"),
        ("recipient_address", "other@synthetic-dealer.example"),
        ("qualification_revision_number", 2),
        ("qualified_price_minor", 199000),
        ("readiness_rationale_hash", sha("other-rationale")),
        ("binding_hash", sha("other-binding")),
    ],
)
def test_binding_is_immutable_after_reservation(
    db_conn: psycopg.Connection, iw: InquiryWorld, column: str, value: Any
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    with expect_sqlstate(SV_FROZEN, "binding is immutable"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **{column: value})


def test_binding_may_change_before_reservation(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw, language="de"))
        update_inquiry(db_conn, inquiry, state="held_facts", state_reasons=["MILEAGE_AMBIGUOUS"])
        update_inquiry(db_conn, inquiry, state="qualifying", binding_hash=sha("rebound"))
    reserve(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "reserved"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("identity_key", sha("other")),
        ("seller_entity_id", None),
        ("vehicle_listing_id", None),
        ("purpose", "follow_up"),
    ],
)
def test_identity_is_always_immutable(
    db_conn: psycopg.Connection, iw: InquiryWorld, column: str, value: Any
) -> None:
    inquiry = insert_inquiry(db_conn, iw, state="candidate")
    if value is None:
        value = uuid.uuid4()
    with pytest.raises(psycopg.Error) as exc, backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **{column: value})
    # suv_backend has no UPDATE privilege on identity columns at all (42501); the trigger also refuses.
    assert exc.value.sqlstate == "42501"
    with expect_sqlstate(SV_FROZEN, "identity is immutable"):
        update_inquiry(db_conn, inquiry, **{column: value})


def test_provider_references_are_set_once(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn,
            inquiry,
            rfc_message_id="<synthetic-inquiry-1@synthetic-mail.example>",
            provider_thread_id="synthetic-thread-1",
        )
    with expect_sqlstate(SV_FROZEN, "set once"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, rfc_message_id="<other@synthetic-mail.example>")
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, provider_message_id="has whitespace")


def test_row_version_never_decreases(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw, row_version=3)
    with expect_sqlstate(SV_MONOTONIC), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, row_version=2)


def test_inquiries_and_attempts_are_never_deleted(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry, attempt = sent_inquiry(db_conn, iw)
    for table, row_id in (("app.seller_inquiries", inquiry), ("ops.email_delivery_attempts", attempt)):
        schema, name = table.split(".")
        with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
            db_conn.execute(f'delete from "{schema}"."{name}" where id = %s', (row_id,))
        with expect_sqlstate(SV_APPEND_ONLY):
            db_conn.execute(f'delete from "{schema}"."{name}" where id = %s', (row_id,))


# ---------------------------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------------------------


def test_attempt_must_use_the_bound_sender_account(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    other = sender_binding(
        iw.seed, iw.workspace_id, from_address="second@synthetic-mail.example", account_id="b"
    )
    for overrides in (
        {"sender_binding_id": other},
        {"sender_binding_version": 2},
        {"provider": "gmail_api"},
    ):
        with expect_sqlstate(SV_REFERENCE, "bound sender account"), backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, inquiry, state="sending")
            insert_attempt(db_conn, attempt_values(iw, inquiry, **overrides))


def test_attempt_must_reuse_the_stable_message_id(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, rfc_message_id="<stable-1@synthetic-mail.example>")
    queue(db_conn, iw, inquiry)
    with expect_sqlstate(SV_REFERENCE, "stable Message-ID"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(db_conn, attempt_values(iw, inquiry, rfc_message_id="<other@synthetic-mail.example>"))
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(
            db_conn, attempt_values(iw, inquiry, rfc_message_id="<stable-1@synthetic-mail.example>")
        )


def test_attempt_is_append_only_except_its_outcome(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    for column, value in (
        ("attempt_id", uuid.uuid4()),
        ("lease_token", uuid.uuid4()),
        ("sender_binding_version", 2),
        ("send_intent_committed_at", T0),
    ):
        with expect_sqlstate(SV_FROZEN):
            db_conn.execute(
                f"update ops.email_delivery_attempts set {column} = %s where id = %s",
                (value, attempt),
            )
    # A receipt only exists for an accepted submission (never fabricated for a timeout).
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain", receipt=Jsonb({"smtp": "fabricated"}))
    accept(db_conn, iw, inquiry, attempt)
    with expect_sqlstate(SV_FROZEN, "finalised"), backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain")
    row = db_conn.execute(
        "select outcome, submission_uncertain from ops.email_delivery_attempts where id = %s", (attempt,)
    ).fetchone()
    assert row == ("accepted", False)


def test_attempt_ledger_and_debit_checks(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    # One active debit per inquiry.
    with expect_sqlstate("23505"), backend(db_conn, iw.workspace_id):
        debit(db_conn, iw, inquiry)
    # A debit is immutable except its release.
    with expect_sqlstate(SV_FROZEN):
        db_conn.execute(
            "update ops.inquiry_quota_ledger set debited_at = debited_at - interval '30 days'"
            " where inquiry_id = %s",
            (inquiry,),
        )
    # Only a cancelled/suppressed never-transmitted inquiry releases.
    with expect_sqlstate(SV_TRANSITION, "never-transmitted"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'EARLY_RELEASE'"
            " where inquiry_id = %s",
            (inquiry,),
        )
