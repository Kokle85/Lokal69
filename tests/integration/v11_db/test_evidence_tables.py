"""Seller identity/contact evidence, authorization records, sender secrets and availability
events (spec 37.1, 37.3, 37.8, 37.9).

- Recipient evidence: one verified recipient per listing, accepted evidence kinds only, immutable
  evidence, a changed contact never becomes current again.
- Aliases: one active entity per alias; a false-positive link is unlinked, never deleted.
- The standing authorization is an append-only, versioned record with the exact bounded scope.
- Sender credentials are never stored in clear.
- Availability events use the canonical ``listings.availability`` values only; an absence is
  ``unknown`` (never sold) and needs a finished complete scan; a seller statement needs a
  verified seller reply about this vehicle; history is append-only.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import T0, Seed, backend, sha
from tests.integration.v11_db.support import (
    SV_APPEND_ONLY,
    SV_FROZEN,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    accept,
    authorization,
    confirmed_cluster,
    contact,
    dispatch,
    expect_sqlstate,
    inquiry_world,
    insert_inquiry,
    insert_reply,
    insert_row,
    mailbox,
    publish_binding,
    queue,
    reply_values,
    reserve,
    seller_entity,
    sender_binding,
    sent_inquiry,
    vehicle,
)

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------------------------
# Seller contacts
# ---------------------------------------------------------------------------------------------


def test_one_verified_recipient_per_listing(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    with expect_sqlstate("23505", "seller_contacts_verified_uidx"):
        contact(iw.seed, iw.workspace_id, iw.vehicle, iw.seller_entity_id)
    # Further (unverified) evidence for the same listing is fine: it is not a second recipient.
    contact(iw.seed, iw.workspace_id, iw.vehicle, iw.seller_entity_id, status="unverified")


@pytest.mark.parametrize("evidence_kind", ["guessed_address", "generic_search_result", "unrelated_harvested"])
def test_rejected_evidence_kinds_can_never_be_verified(
    db_conn: psycopg.Connection, seed: Seed, evidence_kind: str
) -> None:
    iw = inquiry_world(seed, "V11 rejected evidence")
    other = vehicle(seed, iw.workspace_id)
    with expect_sqlstate("23514"):
        contact(seed, iw.workspace_id, other, iw.seller_entity_id, evidence_kind=evidence_kind)
    contact(
        seed,
        iw.workspace_id,
        other,
        iw.seller_entity_id,
        evidence_kind=evidence_kind,
        status="unverified",
        status_reasons=[evidence_kind.upper()],
    )


def test_contact_kind_must_match_its_evidence(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    seller = seller_entity(iw.seed, iw.workspace_id)
    with expect_sqlstate("23514", "seller_contacts_kind_mapping_ck"):
        contact(
            iw.seed,
            iw.workspace_id,
            iw.vehicle,
            seller,
            status="unverified",
            contact_kind="marketplace_relay",
        )
    with expect_sqlstate("23514", "seller_contacts_relay_ck"):
        contact(
            iw.seed,
            iw.workspace_id,
            iw.vehicle,
            seller,
            status="unverified",
            evidence_kind="marketplace_relay_for_listing",
            relay_listing_reference=None,
        )
    with expect_sqlstate("23514", "seller_contacts_address_ck"):
        contact(
            iw.seed, iw.workspace_id, iw.vehicle, seller, status="unverified", address="Info@Dealer.EXAMPLE"
        )
    with expect_sqlstate("23514", "seller_contacts_listing_url_ck"):
        contact(
            iw.seed,
            iw.workspace_id,
            iw.vehicle,
            seller,
            status="unverified",
            listing_url="javascript:alert(1)",
        )


def test_contact_evidence_is_immutable_and_changed_is_terminal(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    for column, value in (
        ("address", "other@synthetic-dealer.example"),
        ("extraction_excerpt", "rewritten evidence"),
        ("language_code", "it"),
        ("seller_entity_id", seller_entity(iw.seed, iw.workspace_id)),
    ):
        with expect_sqlstate(SV_FROZEN, "evidence is immutable"):
            db_conn.execute(
                f"update app.seller_contacts set {column} = %s where id = %s", (value, iw.contact_id)
            )
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_contacts set last_rechecked_at = now(), status = 'changed',"
            " changed_at = now(),"
            " status_reasons = %s where id = %s",
            (["SELLER_CONTACT_CHANGED"], iw.contact_id),
        )
    with expect_sqlstate(SV_TRANSITION, "cannot become current again"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_contacts set status = 'verified', changed_at = null where id = %s",
            (iw.contact_id,),
        )
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("delete from app.seller_contacts where id = %s", (iw.contact_id,))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from app.seller_contacts where id = %s", (iw.contact_id,))


def test_unverified_contact_can_be_verified_once(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    other = vehicle(seed, iw.workspace_id)
    contact_id, _ = contact(seed, iw.workspace_id, other, iw.seller_entity_id, status="unverified")
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_contacts set status = 'verified', verified_at = now() where id = %s",
            (contact_id,),
        )
    with expect_sqlstate(SV_FROZEN, "set once"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_contacts set verified_at = now() + interval '1 hour' where id = %s",
            (contact_id,),
        )


# ---------------------------------------------------------------------------------------------
# Seller aliases
# ---------------------------------------------------------------------------------------------


def _alias(iw: InquiryWorld, entity: uuid.UUID, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "seller_entity_id": entity,
        "source_id": None,
        "alias_kind": "vat_id",
        "reference": "DE999999999",
        "alias_key_hash": sha("vat_id:*:DE999999999"),
        "evidence_kind": "same_vat_id",
        "observed_at": T0,
    }
    values.update(cols)
    return values


def test_an_alias_identifies_one_entity_at_a_time(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    other = seller_entity(iw.seed, iw.workspace_id)
    with backend(db_conn, iw.workspace_id):
        first = insert_row(db_conn, "app.seller_entity_aliases", _alias(iw, iw.seller_entity_id))
    with expect_sqlstate("23505", "seller_entity_aliases_active_uidx"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.seller_entity_aliases", _alias(iw, other))
    # False-positive link: unlink (kept with who/why), then the alias may point elsewhere.
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_entity_aliases set unlinked_at = now(), unlinked_by = %s,"
            " unlink_reason = 'different company, same group VAT fixture' where id = %s",
            (uuid.uuid4(), first),
        )
        insert_row(db_conn, "app.seller_entity_aliases", _alias(iw, other))
    with expect_sqlstate(SV_FROZEN):
        db_conn.execute("update app.seller_entity_aliases set reference = 'DE111' where id = %s", (first,))
    with expect_sqlstate(SV_APPEND_ONLY):
        db_conn.execute("delete from app.seller_entity_aliases where id = %s", (first,))


@pytest.mark.parametrize(
    "overrides",
    [
        {"alias_kind": "marketplace_seller_id", "reference": "SYN-1", "source_id": None},
        {"alias_kind": "vat_id", "reference": "de 999.999"},
        {"alias_kind": "dealer_website_domain", "reference": "https://dealer.example/"},
        {"evidence_kind": "same_name"},
        {"unlinked_at": T0, "unlinked_by": None, "unlink_reason": None},
    ],
)
def test_alias_constraints(db_conn: psycopg.Connection, iw: InquiryWorld, overrides: dict[str, Any]) -> None:
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.seller_entity_aliases", _alias(iw, iw.seller_entity_id, **overrides))


# ---------------------------------------------------------------------------------------------
# Standing authorization record
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"approval_mode": "per_message_approval"},
        {"questions": ["availability", "vehicle_documents"]},
        {"questions": ["availability", "vehicle_documents", "lowest_final_price", "can_you_deliver"]},
        {"purpose": "follow_up"},
        {"max_inquiries_per_vehicle_seller_pair": 2},
        {"follow_ups_allowed": True},
        {"attachments_allowed": True},
        {"cc_bcc_allowed": True},
        {"additional_recipients_allowed": True},
        {"english_requires_positive_evidence": False},
        {"languages": ["de", "mk"]},
        {"allowed_outgoing_data_categories": ["verified_sender_display_name", "telephone"]},
        {"excluded_data_categories": ["home_address"]},
        {"not_authorized": ["purchase"]},
        {"profiles_in_scope": []},
        {"profiles_in_scope": ["everything"]},
        {"revoked_at": T0, "revoked_by": None, "revoke_reason": None},
        {"record_hash": "not-a-hash"},
    ],
)
def test_authorization_record_is_exactly_the_bounded_scope(
    db_conn: psycopg.Connection, seed: Seed, overrides: dict[str, Any]
) -> None:
    ws = seed.workspace("V11 authorization")
    with expect_sqlstate("23514"):
        authorization(seed, ws, **overrides)


def test_authorization_versions_are_append_only(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    with expect_sqlstate("23505"):
        authorization(iw.seed, iw.workspace_id, version=1)
    revoked = authorization(
        iw.seed,
        iw.workspace_id,
        version=2,
        revoked_at=T0,
        revoked_by="Synthetic owner",
        revoke_reason="owner revoked the standing authorization (synthetic)",
    )
    for statement in (
        "update app.seller_inquiry_authorizations set revoked_at = null, revoked_by ="
        " null, revoke_reason = null"
        " where id = %s",
        "delete from app.seller_inquiry_authorizations where id = %s",
    ):
        with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
            db_conn.execute(statement, (revoked,))
        with expect_sqlstate(SV_APPEND_ONLY):
            db_conn.execute(statement, (revoked,))


# ---------------------------------------------------------------------------------------------
# Sender secrets
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_reference": "ya29.a0AfH6SMBsyntheticRawAccessToken"},
        {"secret_reference": "eyJhbGciOiJIUzI1NiJ9.synthetic.jwt"},
        {"secret_reference": "wincred:suv/sender with space"},
        {"secret_reference": "vault:user:password@host"},
        {"secret_envelope": b"short"},
        {"secret_envelope": b"x" * 32, "secret_reference": "wincred:suv-deals/seller-sender"},
        {"display_name": "Sender\r\nBcc: victim@example.invalid"},
        {"from_address": "Inquiries@Synthetic-Mail.EXAMPLE"},
        {"provider": "smtp_password"},
        {"health": "great"},
        {"alias_verified": True, "alias_verified_at": None},
    ],
)
def test_sender_binding_never_stores_raw_credentials(
    db_conn: psycopg.Connection, seed: Seed, overrides: dict[str, Any]
) -> None:
    ws = seed.workspace("V11 sender secrets")
    with expect_sqlstate("23514"):
        sender_binding(seed, ws, **overrides)


def test_sender_binding_accepts_sealed_or_referenced_secrets(db_conn: psycopg.Connection, seed: Seed) -> None:
    ws = seed.workspace("V11 sender secrets ok")
    sender_binding(seed, ws, secret_reference="wincred:suv-deals/seller-sender")
    sender_binding(seed, ws, from_address="second@synthetic-mail.example", secret_envelope=b"\x01" * 64)
    with expect_sqlstate("23505", "email_sender_bindings_active_from_uidx"):
        sender_binding(seed, ws, from_address="SECOND@synthetic-mail.example")


# ---------------------------------------------------------------------------------------------
# Availability events
# ---------------------------------------------------------------------------------------------


def _event(iw: InquiryWorld, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "source_id": iw.vehicle.source_id,
        "listing_id": iw.listing_id,
        "old_availability": "available",
        "new_availability": "unknown",
        "evidence_kind": "source_observation",
        "reason": "detail_not_found",
        "source_reference": "detail fetch 404 (synthetic)",
        "effective_at": T0,
        "observed_at": T0,
        "confidence": "low",
    }
    values.update(cols)
    return values


def _run(seed: Seed, iw: InquiryWorld, outcome: str) -> uuid.UUID:
    finished = None if outcome == "running" else T0 + timedelta(minutes=5)
    return seed.insert_id(
        "ops.crawl_runs",
        workspace_id=iw.workspace_id,
        source_id=iw.vehicle.source_id,
        adapter_version="fixture@1.0.0",
        coverage_mode="rolling_pages",
        outcome=outcome,
        started_at=T0,
        finished_at=finished,
    )


@pytest.mark.parametrize("value", ["sold", "not_seen_in_complete_scan", "availability_unknown", "gone"])
def test_only_canonical_availability_values(
    db_conn: psycopg.Connection, iw: InquiryWorld, value: str
) -> None:
    manual = {"evidence_kind": "manual", "reason": "manual", "manual_principal_id": uuid.uuid4()}
    with expect_sqlstate("23514", "availability_events_values_ck"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, new_availability=value, **manual))
    with expect_sqlstate("23514", "availability_events_values_ck"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, old_availability=value, **manual))


def test_absence_is_unknown_never_sold_and_needs_a_complete_scan(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    complete = _run(seed, iw, "complete")
    partial = _run(seed, iw, "partial")
    running = _run(seed, iw, "running")
    absence = {
        "evidence_kind": "complete_scan_absence",
        "reason": "not_seen_in_complete_scan",
        "source_reference": None,
        "confidence": "medium",
    }
    for new in ("sold_claimed", "removed"):
        with expect_sqlstate("23514", "availability_events_mapping_ck"), backend(db_conn, iw.workspace_id):
            insert_row(
                db_conn,
                "app.availability_events",
                _event(iw, new_availability=new, crawl_run_id=complete, **absence),
            )
    with expect_sqlstate("23514", "availability_events_reference_ck"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, **absence))
    for run in (partial, running):
        with expect_sqlstate(SV_REFERENCE, "complete scan"), backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", _event(iw, crawl_run_id=run, **absence))
    with backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, crawl_run_id=complete, **absence))
    # The run must belong to the listing's own source.
    other = vehicle(seed, iw.workspace_id)
    with expect_sqlstate("23503"), backend(db_conn, iw.workspace_id):
        insert_row(
            db_conn,
            "app.availability_events",
            _event(iw, source_id=other.source_id, crawl_run_id=complete, **absence),
        )


@pytest.mark.parametrize(
    ("evidence_kind", "new", "ok"),
    [
        ("source_sold_badge", "sold_claimed", True),
        ("source_sold_badge", "removed", False),
        ("source_removed_page", "removed", True),
        ("source_removed_page", "sold_claimed", False),
        ("source_observation", "available", True),
        ("source_observation", "sold_claimed", False),
        ("manual", "reserved", True),
    ],
)
def test_evidence_kind_determines_the_canonical_value(
    db_conn: psycopg.Connection, iw: InquiryWorld, evidence_kind: str, new: str, ok: bool
) -> None:
    extra: dict[str, Any] = {"manual_principal_id": uuid.uuid4()} if evidence_kind == "manual" else {}
    values = _event(iw, evidence_kind=evidence_kind, new_availability=new, reason=evidence_kind, **extra)
    if ok:
        with backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", values)
    else:
        with expect_sqlstate("23514", "availability_events_mapping_ck"), backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", values)


def test_seller_statement_needs_a_verified_reply_about_this_vehicle(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        quarantined = insert_reply(
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
    sold = {
        "evidence_kind": "seller_reported_sold",
        "new_availability": "sold_claimed",
        "reason": "seller_reported_sold",
        "source_reference": None,
        "confidence": "medium",
    }
    with expect_sqlstate("23514", "availability_events_reference_ck"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, **sold))
    with expect_sqlstate(SV_REFERENCE, "non-quarantined"), backend(db_conn, iw.workspace_id):
        insert_row(db_conn, "app.availability_events", _event(iw, reply_id=quarantined, **sold))
    other = vehicle(seed, iw.workspace_id)
    with expect_sqlstate(SV_REFERENCE, "another vehicle"), backend(db_conn, iw.workspace_id):
        insert_row(
            db_conn,
            "app.availability_events",
            {
                **_event(iw, reply_id=reply, **sold),
                "source_id": other.source_id,
                "listing_id": other.listing_id,
            },
        )
    with backend(db_conn, iw.workspace_id):
        event = insert_row(db_conn, "app.availability_events", _event(iw, reply_id=reply, **sold))
    row = db_conn.execute(
        "select new_availability, evidence_kind from app.availability_events where id = %s", (event,)
    ).fetchone()
    assert row == ("sold_claimed", "seller_reported_sold")


def test_seller_statement_may_describe_another_listing_of_the_confirmed_cluster(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    twin = vehicle(seed, iw.workspace_id)
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    inquiry = insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    accept(db_conn, iw, inquiry, attempt)
    box = mailbox(seed, iw)
    publish_binding(db_conn, iw, box, inquiry)
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        insert_row(
            db_conn,
            "app.availability_events",
            {
                **_event(
                    iw,
                    reply_id=reply,
                    evidence_kind="seller_reported_reserved",
                    new_availability="reserved",
                    reason="seller_reported_reserved",
                    source_reference=None,
                ),
                "source_id": twin.source_id,
                "listing_id": twin.listing_id,
                "vehicle_cluster_id": cluster,
            },
        )


def test_conflicting_or_historical_events_are_never_promoted(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    for flags in ({"conflicts_with_current": True}, {"historical_only": True}):
        with expect_sqlstate("23514", "availability_events_promotion_ck"), backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", _event(iw, **flags))
        with backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", _event(iw, promote_current=False, **flags))


def test_availability_events_are_append_only(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    with backend(db_conn, iw.workspace_id):
        event = insert_row(db_conn, "app.availability_events", _event(iw))
    for statement in (
        "update app.availability_events set new_availability = 'sold_claimed' where id = %s",
        "delete from app.availability_events where id = %s",
    ):
        with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
            db_conn.execute(statement, (event,))
        with expect_sqlstate(SV_APPEND_ONLY):
            db_conn.execute(statement, (event,))


def test_reason_is_a_label_not_free_text(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for reason in ("Not seen", "sold!", "", "x" * 81):
        with expect_sqlstate("23514", "availability_events_reason_ck"), backend(db_conn, iw.workspace_id):
            insert_row(db_conn, "app.availability_events", _event(iw, reason=reason))
