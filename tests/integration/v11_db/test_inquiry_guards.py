"""Reservation and dispatch guards (spec 37.2 check 6, 37.3, 37.5; 37.10 delta tests).

The database re-checks, immediately before reservation and again before transmission, the kill
switch and mode, the current standing-authorization version, the exact sender binding, the
verified recipient and its positively resolved language (English only with evidence), every
active suppression, the canonical vehicle identity, the listing facts (a changed price or
availability cancels stale queued work), the seller cooldown and the quota. None of these is a
message-approval gate: the inquiry proceeds automatically once they pass.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import T0, Seed, backend, sha
from tests.integration.v11_db.support import (
    SV_FROZEN,
    SV_MONOTONIC,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    audit_event,
    authorization,
    binding_values,
    confirmed_cluster,
    contact,
    debit,
    dispatch,
    expect_sqlstate,
    inquiry_world,
    insert_inquiry,
    queue,
    reserve,
    seller_entity,
    state_of,
    suppression,
    update_inquiry,
    vehicle,
    with_vehicle,
)

pytestmark = pytest.mark.db


def _queued(db_conn: psycopg.Connection, iw: InquiryWorld, **overrides: Any) -> uuid.UUID:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry, **overrides)
    queue(db_conn, iw, inquiry)
    return inquiry


def _try_reserve(db_conn: psycopg.Connection, iw: InquiryWorld, inquiry: uuid.UUID, **overrides: Any) -> None:
    reserve(db_conn, iw, inquiry, **overrides)


# ---------------------------------------------------------------------------------------------
# No approval gate: a qualifying inquiry proceeds automatically
# ---------------------------------------------------------------------------------------------


def test_no_human_approval_is_needed_anywhere(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Reservation, queueing and dispatch need only the automatic checks: no approval column,
    approval record or approval state exists in the schema."""
    inquiry = _queued(db_conn, iw)
    dispatch(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "sending"
    columns = db_conn.execute(
        "select table_schema || '.' || table_name || '.' || column_name from information_schema.columns"
        " where table_name in ('seller_inquiries', 'email_delivery_attempts', 'seller_inquiry_controls')"
        " and column_name like '%%approv%%'"
    ).fetchall()
    assert columns == []
    with expect_sqlstate("23514"):
        authorization(iw.seed, iw.workspace_id, version=2, approval_mode="message_approval_required")


# ---------------------------------------------------------------------------------------------
# Kill switch, mode and the pause tool's optimistic version
# ---------------------------------------------------------------------------------------------


def _pause(db_conn: psycopg.Connection, iw: InquiryWorld, expected_version: int) -> int:
    with backend(db_conn, iw.workspace_id):
        return db_conn.execute(
            "update app.seller_inquiry_controls set kill_switch = true, kill_switch_reason = %s,"
            " kill_switch_set_at = now(), kill_switch_set_by = %s, version = version + 1"
            " where workspace_id = %s and version = %s",
            ("owner pause via seller_inquiries_pause", uuid.uuid4(), iw.workspace_id, expected_version),
        ).rowcount


def test_kill_switch_stops_untransmitted_work(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    reserved = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, reserved)
    second = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    candidate = insert_inquiry(db_conn, second)
    assert _pause(db_conn, iw, expected_version=1) == 1
    with expect_sqlstate(SV_TRANSITION, "kill switch"):
        queue(db_conn, iw, reserved)
    with expect_sqlstate(SV_TRANSITION, "kill switch"):
        reserve(db_conn, second, candidate)
    # Leaving the pipeline is always possible (safe direction).
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, reserved, state="suppressed", suppression_reason="kill_switch")
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'KILL_SWITCH'"
            " where inquiry_id = %s and released_at is null",
            (reserved,),
        )
    assert state_of(db_conn, reserved) == "suppressed"


def test_kill_switch_blocks_dispatch_of_queued_work(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = _queued(db_conn, iw)
    assert _pause(db_conn, iw, expected_version=1) == 1
    with expect_sqlstate(SV_TRANSITION, "kill switch"):
        dispatch(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "queued"


def test_pause_uses_an_optimistic_version(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    assert _pause(db_conn, iw, expected_version=7) == 0  # stale expected_version: nothing changes
    with expect_sqlstate(SV_MONOTONIC, "advancing the version"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_inquiry_controls set mode = 'paused' where workspace_id = %s",
            (iw.workspace_id,),
        )
    with expect_sqlstate(SV_MONOTONIC), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_inquiry_controls set mode = 'paused', version = version + 2"
            " where workspace_id = %s",
            (iw.workspace_id,),
        )
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update app.seller_inquiry_controls set kill_switch = true, version = version +"
            " 1 where workspace_id = %s",
            (iw.workspace_id,),
        )
    assert _pause(db_conn, iw, expected_version=1) == 1
    row = db_conn.execute(
        "select kill_switch, version from app.seller_inquiry_controls where workspace_id = %s",
        (iw.workspace_id,),
    ).fetchone()
    assert row == (True, 2)


@pytest.mark.parametrize("mode", ["disabled_until_sender_ready", "paused"])
def test_mode_must_be_automatic(db_conn: psycopg.Connection, iw: InquiryWorld, mode: str) -> None:
    db_conn.execute(
        "update app.seller_inquiry_controls set mode = %s, version = version + 1 where workspace_id = %s",
        (mode, iw.workspace_id),
    )
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "not enabled"):
        reserve(db_conn, iw, inquiry)


def test_controls_are_required_and_caps_are_ceilings(db_conn: psycopg.Connection, seed: Seed) -> None:
    ws = seed.workspace("V11 no controls")
    with expect_sqlstate("23514"):
        seed.insert("app.seller_inquiry_controls", workspace_id=ws, max_per_24h=3)
    with expect_sqlstate("23514"):
        seed.insert("app.seller_inquiry_controls", workspace_id=ws, max_per_15d=6)
    with expect_sqlstate("23514"):
        seed.insert("app.seller_inquiry_controls", workspace_id=ws, seller_cooldown="1 hour")
    seed.insert("app.seller_inquiry_controls", workspace_id=ws, max_per_24h=0, max_per_15d=0)
    with expect_sqlstate("23505"):
        seed.insert("app.seller_inquiry_controls", workspace_id=ws)


def test_missing_controls_fail_closed(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    db_conn.execute("delete from app.seller_inquiry_controls where workspace_id = %s", (iw.workspace_id,))
    with expect_sqlstate(SV_TRANSITION, "not initialised"):
        reserve(db_conn, iw, inquiry)


# ---------------------------------------------------------------------------------------------
# Authorization, sender, recipient and language
# ---------------------------------------------------------------------------------------------


def test_reservation_needs_the_current_unrevoked_authorization(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    authorization(
        iw.seed,
        iw.workspace_id,
        version=2,
        revoked_at=T0,
        revoked_by="Synthetic owner",
        revoke_reason="synthetic revocation for a test",
    )
    with expect_sqlstate(SV_TRANSITION, "current seller inquiry authorization"):
        reserve(db_conn, iw, inquiry)
    third = authorization(
        iw.seed,
        iw.workspace_id,
        version=3,
        revoked_at=T0,
        revoked_by="Synthetic owner",
        revoke_reason="still revoked",
    )
    with expect_sqlstate(SV_TRANSITION, "revoked"):
        reserve(db_conn, iw, inquiry, authorization_id=third, authorization_version=3)


def test_queued_inquiry_is_refused_after_a_new_authorization_version(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = _queued(db_conn, iw)
    authorization(iw.seed, iw.workspace_id, version=2)
    with expect_sqlstate(SV_TRANSITION, "current seller inquiry authorization"):
        dispatch(db_conn, iw, inquiry)


def test_authorization_profile_scope_is_not_silently_broadened(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    manual = vehicle(iw.seed, iw.workspace_id, profile="manual_4000")
    contact_id, address = contact(iw.seed, iw.workspace_id, manual, iw.seller_entity_id)
    world = InquiryWorld(
        iw.workspace_id,
        iw.seed,
        manual,
        iw.seller_entity_id,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )
    inquiry = insert_inquiry(db_conn, world)
    with expect_sqlstate(SV_TRANSITION, "profile covered by the authorization"):
        reserve(db_conn, world, inquiry)


def test_language_must_be_covered_by_the_authorization(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    second = authorization(iw.seed, iw.workspace_id, version=2, languages=["it"])
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "language is not covered"):
        reserve(db_conn, iw, inquiry, authorization_id=second, authorization_version=2)


def test_sender_change_cancels_instead_of_switching_accounts(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = _queued(db_conn, iw)
    # Re-verification with a new display name is a new binding version.
    with expect_sqlstate(SV_MONOTONIC, "advance its version"):
        db_conn.execute(
            "update ops.email_sender_bindings set display_name = 'Other Name' where id = %s",
            (iw.sender_binding_id,),
        )
    db_conn.execute(
        "update ops.email_sender_bindings set display_name = 'Other Name', version = 2 where id = %s",
        (iw.sender_binding_id,),
    )
    with expect_sqlstate(SV_TRANSITION, "sender binding changed"):
        dispatch(db_conn, iw, inquiry)


@pytest.mark.parametrize(
    ("column", "value", "fragment"),
    [
        ("health", "degraded", "verified and healthy"),
        ("alias_verified", False, "verified and healthy"),
    ],
)
def test_unusable_sender_blocks_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld, column: str, value: Any, fragment: str
) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        f"update ops.email_sender_bindings set {column} = %s, version = version + 1 where id = %s",
        (value, iw.sender_binding_id),
    )
    with expect_sqlstate(SV_TRANSITION, fragment):
        dispatch(db_conn, iw, inquiry)


def test_revoked_sender_blocks_dispatch_and_is_frozen(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        "update ops.email_sender_bindings set revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic token revoked' where id = %s",
        (uuid.uuid4(), iw.sender_binding_id),
    )
    with expect_sqlstate(SV_TRANSITION, "revoked"):
        dispatch(db_conn, iw, inquiry)
    with expect_sqlstate(SV_FROZEN, "revoked sender binding"):
        db_conn.execute(
            "update ops.email_sender_bindings set revoked_at = null, revoked_by = null, revoke_reason = null,"
            " version = version + 1 where id = %s",
            (iw.sender_binding_id,),
        )


def test_sender_identity_is_never_switched_in_place(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for column, value in (
        ("from_address", "other@synthetic-mail.example"),
        ("account_id", "other-account"),
        ("provider", "gmail_api"),
    ):
        with expect_sqlstate(SV_FROZEN, "identity"):
            db_conn.execute(
                f"update ops.email_sender_bindings set {column} = %s, version = version + 1 where id = %s",
                (value, iw.sender_binding_id),
            )


def test_recipient_change_blocks_dispatch(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        "update app.seller_contacts set status = 'changed', changed_at = now(),"
        " status_reasons = %s where id = %s",
        (["SELLER_CONTACT_CHANGED"], iw.contact_id),
    )
    with expect_sqlstate(SV_TRANSITION, "no longer verified"):
        dispatch(db_conn, iw, inquiry)


def test_recipient_must_be_the_exact_listing_sellers_verified_contact(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    # A verified contact of ANOTHER listing (even of the same seller) is rejected by composite FKs.
    other = with_vehicle(iw)
    with expect_sqlstate("23503"), backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, recipient_contact_id=other.contact_id, recipient_address=other.contact_address
        )
    # A contact of another seller for this listing is rejected as well.
    stranger = seller_entity(iw.seed, iw.workspace_id)
    stranger_contact, stranger_address = contact(
        iw.seed, iw.workspace_id, iw.vehicle, stranger, status="unverified"
    )
    with expect_sqlstate("23503"), backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, recipient_contact_id=stranger_contact, recipient_address=stranger_address
        )
    # An address that differs from the verified contact's.
    with expect_sqlstate(SV_TRANSITION, "differs from the verified contact"):
        reserve(db_conn, iw, inquiry, recipient_address="guessed-info@synthetic-dealer.example")


def test_unverified_or_unavailable_recipient_cannot_be_reserved(
    db_conn: psycopg.Connection, seed: Seed
) -> None:
    iw = inquiry_world(seed, "V11 unverified recipient")
    db_conn.execute(
        "update app.seller_contacts set status = 'unverified', status_reasons = %s where id = %s",
        (["EVIDENCE_STALE"], iw.contact_id),
    )
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "no longer verified"):
        reserve(db_conn, iw, inquiry)
    # seller_email_unavailable: no address at all, so nothing can be bound.
    with expect_sqlstate("23514"):
        contact(
            seed,
            iw.workspace_id,
            iw.vehicle,
            iw.seller_entity_id,
            status="unavailable",
            evidence_kind="contact_form_only",
        )
    contact(
        seed,
        iw.workspace_id,
        iw.vehicle,
        iw.seller_entity_id,
        address=None,
        status="unavailable",
        evidence_kind="contact_form_only",
    )


@pytest.mark.parametrize(
    ("contact_language", "status", "basis", "inquiry_language"),
    [
        ("de", "resolved", "seller_ad_text", "en"),  # English is never the fallback
        ("it", "resolved", "seller_ad_text", "de"),  # Swiss listing: the evidence decides, not the country
        ("en", "language_unresolved", "none", "en"),
        (
            "nl",
            "unsupported_language",
            "seller_ad_text",
            "en",
        ),  # unsupported is held, not replaced by English
    ],
)
def test_language_must_be_positively_resolved(
    db_conn: psycopg.Connection,
    seed: Seed,
    contact_language: str,
    status: str,
    basis: str,
    inquiry_language: str,
) -> None:
    iw = inquiry_world(seed, "V11 language")
    db_conn.execute(
        "update app.seller_contacts set status = 'changed', changed_at = now() where id = %s",
        (iw.contact_id,),
    )
    contact_id, address = contact(
        seed,
        iw.workspace_id,
        iw.vehicle,
        iw.seller_entity_id,
        language=contact_language,
        language_status=status,
        language_basis=basis,
    )
    world = InquiryWorld(
        iw.workspace_id,
        seed,
        iw.vehicle,
        iw.seller_entity_id,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )
    inquiry = insert_inquiry(db_conn, world)
    with expect_sqlstate(SV_TRANSITION, "positively resolved"):
        reserve(db_conn, world, inquiry, language=inquiry_language)


def test_verified_english_evidence_allows_the_english_template(
    db_conn: psycopg.Connection, seed: Seed
) -> None:
    iw = inquiry_world(seed, "V11 English")
    db_conn.execute(
        "update app.seller_contacts set status = 'changed', changed_at = now() where id = %s",
        (iw.contact_id,),
    )
    contact_id, address = contact(
        seed,
        iw.workspace_id,
        iw.vehicle,
        iw.seller_entity_id,
        language="en",
        language_basis="verified_seller_preference",
    )
    world = InquiryWorld(
        iw.workspace_id,
        seed,
        iw.vehicle,
        iw.seller_entity_id,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )
    inquiry = insert_inquiry(db_conn, world)
    reserve(db_conn, world, inquiry, language="en")
    assert state_of(db_conn, inquiry) == "reserved"


def test_contact_language_evidence_constraints(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    seller = seller_entity(iw.seed, iw.workspace_id)
    # A resolved language needs an evidence basis (country/navigation alone is not evidence).
    with expect_sqlstate("23514"):
        contact(iw.seed, iw.workspace_id, iw.vehicle, seller, status="unverified", language_basis="none")
    # Only supported template languages resolve.
    with expect_sqlstate("23514"):
        contact(iw.seed, iw.workspace_id, iw.vehicle, seller, status="unverified", language="nl")
    with expect_sqlstate("23514"):
        contact(iw.seed, iw.workspace_id, iw.vehicle, seller, status="unverified", language=None)


def test_template_language_and_body_hash_are_consistent(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw, template_id="seller_initial_it_v1"))
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw, body_hash=sha("not the body")))
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        values = binding_values(iw)
        update_inquiry(
            db_conn, inquiry, **{**values, "original_subject": values["original_subject"] + "\r\nBcc: x"}
        )
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw, sender_display_name="Evil <x@y.example>"))
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        update_inquiry(
            db_conn, inquiry, **binding_values(iw, recipient_address="Inquiries@SYNTHETIC-MAIL.example")
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"readiness": "needs_facts"},
        {"readiness": "needs_technical_review"},
        {"original_subject": None, "original_body": None},
        {"mk_preview_subject": None, "mk_preview_body": None, "mk_preview_hash": None},
        {"recipient_binding_hash": None},
        {"qualification_revision_id": None},
        {"qualified_currency": None},
    ],
)
def test_reservation_needs_inquiry_readiness_and_a_complete_binding(
    db_conn: psycopg.Connection, iw: InquiryWorld, overrides: dict[str, Any]
) -> None:
    """Readiness is the automatic decision (never a human click); a reserved row is fully bound."""
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate("23514"):
        reserve(db_conn, iw, inquiry, **overrides)


@pytest.mark.parametrize("language", ["de", "it", "fr", "en"])
def test_every_template_language_binds_with_matching_hashes(
    db_conn: psycopg.Connection, iw: InquiryWorld, language: str
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw, language=language))


# ---------------------------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------------------------


def _scope_key(iw: InquiryWorld, scope: str) -> str:
    return {
        "workspace": "*",
        "seller": f"seller_entity:{iw.seller_entity_id}",
        "address": iw.contact_address.upper().replace(
            "@SYNTHETIC-DEALER.EXAMPLE", "@synthetic-dealer.example"
        ),
        "vehicle": f"listing_incarnation:{iw.listing_id}",
        "source": iw.vehicle.source_key,
        "sender": str(iw.sender_binding_id),
    }[scope]


@pytest.mark.parametrize("scope", ["workspace", "seller", "address", "vehicle", "source", "sender"])
def test_active_suppression_blocks_reservation_and_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld, scope: str
) -> None:
    queued = _queued(db_conn, iw)
    suppression(iw.seed, iw.workspace_id, scope, _scope_key(iw, scope), reason="hard_bounce")
    with expect_sqlstate(SV_TRANSITION, "active suppression"):
        dispatch(db_conn, iw, queued)
    other = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    if scope in ("workspace", "sender"):
        fresh = insert_inquiry(db_conn, other)
        with expect_sqlstate(SV_TRANSITION, "active suppression"):
            reserve(db_conn, other, fresh)


def test_suppression_of_a_merged_seller_applies_to_the_survivor(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    absorbed = seller_entity(iw.seed, iw.workspace_id)
    suppression(iw.seed, iw.workspace_id, "seller", f"seller_entity:{absorbed}", reason="seller_opt_out")
    db_conn.execute(
        "update app.seller_entities set merged_into_id = %s, merged_at = now(), merge_reason = 'same VAT id'"
        " where id = %s",
        (iw.seller_entity_id, absorbed),
    )
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "seller:seller_opt_out"):
        reserve(db_conn, iw, inquiry)


def test_vehicle_suppression_survives_identity_merges(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """A suppression on one listing incarnation applies to the cluster identity of the same car,
    and a suppression on the cluster applies to a listing-identity inquiry of any member."""
    twin = vehicle(iw.seed, iw.workspace_id)
    cluster = confirmed_cluster(iw.seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    suppression(
        iw.seed, iw.workspace_id, "vehicle", f"listing_incarnation:{twin.listing_id}", reason="seller_opt_out"
    )
    inquiry = insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    with expect_sqlstate(SV_TRANSITION, "vehicle:seller_opt_out"):
        reserve(db_conn, iw, inquiry)

    other = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    unconfirmed = iw.seed.insert_id(
        "app.vehicle_clusters",
        workspace_id=iw.workspace_id,
        confidence="medium",
        match_basis={"synthetic": True},
    )
    iw.seed.insert(
        "app.vehicle_cluster_members",
        workspace_id=iw.workspace_id,
        cluster_id=unconfirmed,
        listing_id=other.listing_id,
        confidence="medium",
    )
    suppression(iw.seed, iw.workspace_id, "vehicle", f"vehicle_cluster:{unconfirmed}", reason="complaint")
    listing_inquiry = insert_inquiry(db_conn, other)
    with expect_sqlstate(SV_TRANSITION, "vehicle:complaint"):
        reserve(db_conn, other, listing_inquiry)


def test_suppression_removal_is_explicit_audited_and_never_automatic(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    sid = suppression(seed, iw.workspace_id, "address", iw.contact_address, reason="hard_bounce")
    remove = (
        "update ops.email_suppressions set removed_at = now(), removed_by_principal_id = %s,"
        " removed_by_kind = %s, removal_reason = %s, removal_audit_id = %s where id = %s"
    )
    # A removal without its audit event is refused.
    with expect_sqlstate(SV_REFERENCE, "audit event"), backend(db_conn, iw.workspace_id):
        db_conn.execute("update ops.email_suppressions set removed_at = now() where id = %s", (sid,))
    # Removal fields are all-or-nothing (CHECK), also when written directly.
    with expect_sqlstate("23514"):
        suppression(seed, iw.workspace_id, "seller", f"seller_entity:{uuid.uuid4()}", removed_at=T0)
    # A system (automatic) removal is rejected even with a matching audit event.
    audit = audit_event(seed, iw.workspace_id, "email_suppression", sid, "email_suppression.removed")
    with expect_sqlstate("23514"), backend(db_conn, iw.workspace_id):
        db_conn.execute(remove, (uuid.uuid4(), "system", "ad reappeared", audit, sid))
    # An audit event about something else is not a removal audit.
    unrelated = audit_event(
        seed, iw.workspace_id, "email_suppression", uuid.uuid4(), "email_suppression.removed"
    )
    with (
        expect_sqlstate(SV_REFERENCE, "audit event about this suppression"),
        backend(db_conn, iw.workspace_id),
    ):
        db_conn.execute(remove, (uuid.uuid4(), "user", "owner confirmed the address works", unrelated, sid))
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(remove, (uuid.uuid4(), "user", "owner confirmed the address works", audit, sid))
    dispatch(db_conn, iw, queued)
    assert state_of(db_conn, queued) == "sending"
    # A removed suppression is frozen; suppressing again is a new row.
    with expect_sqlstate(SV_FROZEN), backend(db_conn, iw.workspace_id):
        db_conn.execute("update ops.email_suppressions set removed_at = null where id = %s", (sid,))
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("delete from ops.email_suppressions where id = %s", (sid,))


def test_suppression_content_is_immutable_and_unique_while_active(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    sid = suppression(iw.seed, iw.workspace_id, "seller", f"seller_entity:{iw.seller_entity_id}")
    with expect_sqlstate(SV_FROZEN):
        db_conn.execute("update ops.email_suppressions set reason = 'complaint' where id = %s", (sid,))
    with expect_sqlstate("23505"):
        suppression(iw.seed, iw.workspace_id, "seller", f"seller_entity:{iw.seller_entity_id}")
    # Address suppressions match case-insensitively, so a re-cased duplicate is the same suppression.
    suppression(iw.seed, iw.workspace_id, "address", "Seller.X@synthetic-dealer.example")
    with expect_sqlstate("23505"):
        suppression(iw.seed, iw.workspace_id, "address", "seller.x@synthetic-dealer.example")
    for scope, key in (
        ("vehicle", "listing:abc"),
        ("seller", "acme gmbh"),
        ("address", "not-an-address"),
        ("workspace", str(uuid.uuid4())),
        ("sender", "outlook"),
    ):
        with expect_sqlstate("23514"):
            suppression(iw.seed, iw.workspace_id, scope, key)


# ---------------------------------------------------------------------------------------------
# Listing facts at dispatch (changed price/availability cancels stale queued messages)
# ---------------------------------------------------------------------------------------------


def test_changed_listing_revision_blocks_stale_dispatch(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = _queued(db_conn, iw)
    _, gen, obs = seed.detail_observation(iw.workspace_id, iw.listing_id, promoted=True)
    revision = seed.revision(
        iw.workspace_id, iw.listing_id, 2, detail_generation=gen, observation_id=obs, asking_minor=240000
    )
    seed.promote(iw.workspace_id, iw.listing_id, revision, gen, obs)
    with expect_sqlstate(SV_TRANSITION, "listing changed since qualification"):
        dispatch(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="cancelled", state_reasons=["PRICE_CHANGED"])
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'PRICE_CHANGED'"
            " where inquiry_id = %s and released_at is null",
            (inquiry,),
        )
    assert state_of(db_conn, inquiry) == "cancelled"


@pytest.mark.parametrize(
    ("availability", "fragment"),
    [
        ("reserved", "availability changed"),
        ("sold_claimed", "sold or removed"),
        ("unknown", "availability changed"),
    ],
)
def test_changed_availability_blocks_stale_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld, availability: str, fragment: str
) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        "update app.listings set availability = %s, row_version = row_version + 1 where id = %s",
        (availability, iw.listing_id),
    )
    with expect_sqlstate(SV_TRANSITION, fragment):
        dispatch(db_conn, iw, inquiry)


@pytest.mark.parametrize(
    ("statement", "fragment"),
    [
        (
            "update app.listings set quarantined = true, quarantine_reason = 'synthetic' where id = %s",
            "quarantined",
        ),
        (
            "update app.listings set eligibility_state = 'rejected' where id = %s",
            "not eligible",
        ),
    ],
)
def test_listing_must_stay_eligible(
    db_conn: psycopg.Connection, iw: InquiryWorld, statement: str, fragment: str
) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(statement, (iw.listing_id,))
    with expect_sqlstate(SV_TRANSITION, fragment):
        dispatch(db_conn, iw, inquiry)


def test_paused_source_blocks_dispatch(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        "update app.sources set paused = true, pause_reason = 'synthetic pause', paused_at = now(),"
        " version = version + 1 where id = %s",
        (iw.vehicle.source_id,),
    )
    with expect_sqlstate(SV_TRANSITION, "disabled or paused"):
        dispatch(db_conn, iw, inquiry)


# ---------------------------------------------------------------------------------------------
# Identity: canonical vehicle, merged sellers, seller cooldown
# ---------------------------------------------------------------------------------------------


def test_merged_seller_cannot_be_reserved_or_dispatched(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    survivor = seller_entity(iw.seed, iw.workspace_id)
    inquiry = _queued(db_conn, iw)
    db_conn.execute(
        "update app.seller_entities set merged_into_id = %s, merged_at = now(),"
        " merge_reason = 'same legal entity'"
        " where id = %s",
        (survivor, iw.seller_entity_id),
    )
    with expect_sqlstate(SV_REFERENCE, "merged"):
        dispatch(db_conn, iw, inquiry)
    other = with_vehicle(iw, seller=iw.seller_entity_id)
    with expect_sqlstate(SV_REFERENCE, "merged"):
        insert_inquiry(db_conn, other)


def test_seller_merges_are_one_level_and_permanent(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    root = seller_entity(iw.seed, iw.workspace_id)
    child = seller_entity(iw.seed, iw.workspace_id)
    grandchild = seller_entity(iw.seed, iw.workspace_id)
    merge = (
        "update app.seller_entities set merged_into_id = %s, merged_at = now(), merge_reason = 'synthetic'"
        " where id = %s"
    )
    db_conn.execute(merge, (child, grandchild))
    with expect_sqlstate(SV_REFERENCE, "absorbed other entities"):
        db_conn.execute(merge, (root, child))
    with expect_sqlstate(SV_REFERENCE, "unmerged root"):
        db_conn.execute(merge, (grandchild, root))
    with expect_sqlstate(SV_FROZEN, "permanent"):
        db_conn.execute("update app.seller_entities set merged_into_id = null where id = %s", (grandchild,))
    with expect_sqlstate("23514"):
        db_conn.execute(merge, (root, root))


def test_seller_cooldown_prevents_a_burst_to_one_dealer(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    first = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, first)
    second_car = with_vehicle(iw)  # same seller, different vehicle and listing
    second = insert_inquiry(db_conn, second_car)
    with expect_sqlstate(SV_TRANSITION, "seller cooldown"):
        reserve(db_conn, second_car, second)
    # Cancelling the never-sent first reservation frees the seller again.
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, first, state="cancelled")
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'CANCELLED'"
            " where inquiry_id = %s",
            (first,),
        )
    reserve(db_conn, second_car, second)
    assert state_of(db_conn, second) == "reserved"


def test_seller_cooldown_counts_entities_merged_into_the_seller(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    absorbed_world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    first = insert_inquiry(db_conn, absorbed_world)
    reserve(db_conn, absorbed_world, first)
    db_conn.execute(
        "update app.seller_entities set merged_into_id = %s, merged_at = now(), merge_reason = 'same VAT id'"
        " where id = %s",
        (iw.seller_entity_id, absorbed_world.seller_entity_id),
    )
    second = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "seller cooldown"):
        reserve(db_conn, iw, second)


def test_cluster_identity_requires_a_confirmed_cluster(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    cluster = confirmed_cluster(iw.seed, iw.workspace_id, [iw.listing_id])
    db_conn.execute(
        "update app.vehicle_clusters set review_status = 'unreviewed', reviewed_by ="
        " null, reviewed_at = null,"
        " row_version = row_version + 1 where id = %s",
        (cluster,),
    )
    with expect_sqlstate(SV_REFERENCE, "confirmed vehicle cluster"):
        insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    # An unconfirmed cluster does not force the cluster identity: the listing identity is used.
    insert_inquiry(db_conn, iw)


def test_unlinked_member_cannot_qualify_for_the_cluster(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    other = vehicle(iw.seed, iw.workspace_id)
    cluster = confirmed_cluster(iw.seed, iw.workspace_id, [iw.listing_id, other.listing_id])
    db_conn.execute(
        "update app.vehicle_cluster_members set unlinked_at = now(), unlinked_by = %s,"
        " unlink_reason = 'false positive' where cluster_id = %s and listing_id = %s",
        (uuid.uuid4(), cluster, iw.listing_id),
    )
    with expect_sqlstate(SV_REFERENCE, "containing the qualifying listing"):
        insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)


def test_quota_debit_needs_a_reserving_inquiry(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw, state="candidate")
    with (
        expect_sqlstate(SV_TRANSITION, "only when the inquiry is reserved"),
        backend(db_conn, iw.workspace_id),
    ):
        debit(db_conn, iw, inquiry)


# ---------------------------------------------------------------------------------------------
# The bound snapshot is the listing's current revision; recipient kinds fit the seller
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"qualified_price_minor": 199000},  # a cheaper price than the revision shows
        {"qualified_currency": "CHF"},
        {"qualified_semantic_hash": sha("other facts")},
        {"qualification_revision_number": 7},
    ],
)
def test_reservation_snapshot_must_be_the_bound_revision(
    db_conn: psycopg.Connection, iw: InquiryWorld, overrides: dict[str, Any]
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_REFERENCE, "qualification snapshot"):
        reserve(db_conn, iw, inquiry, **overrides)


def test_reservation_of_a_stale_revision_is_refused(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    _, gen, obs = seed.detail_observation(iw.workspace_id, iw.listing_id, promoted=True)
    newer = seed.revision(iw.workspace_id, iw.listing_id, 2, detail_generation=gen, observation_id=obs)
    seed.promote(iw.workspace_id, iw.listing_id, newer, gen, obs)
    with expect_sqlstate(SV_TRANSITION, "listing changed since qualification"):
        reserve(db_conn, iw, inquiry)


def test_listing_reserved_for_someone_else_is_not_reserved(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    db_conn.execute(
        "update app.listings set availability = 'reserved', row_version = row_version + 1 where id = %s",
        (iw.listing_id,),
    )
    inquiry = insert_inquiry(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "availability changed"):
        reserve(db_conn, iw, inquiry, qualified_availability="reserved")


def test_official_dealer_contact_needs_a_dealer_seller(db_conn: psycopg.Connection, seed: Seed) -> None:
    iw = inquiry_world(seed, "V11 dealer contact")
    private = seller_entity(seed, iw.workspace_id, seller_type="private")
    other = vehicle(seed, iw.workspace_id)
    contact_id, address = contact(
        seed, iw.workspace_id, other, private, evidence_kind="official_dealer_contact_via_listing"
    )
    world = InquiryWorld(
        iw.workspace_id,
        seed,
        other,
        private,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )
    inquiry = insert_inquiry(db_conn, world)
    with expect_sqlstate(SV_TRANSITION, "only for a dealer seller"):
        reserve(db_conn, world, inquiry)


def test_recipient_is_never_our_own_reply_to_address(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    with (
        expect_sqlstate("23514", "seller_inquiries_recipient_not_sender_ck"),
        backend(db_conn, iw.workspace_id),
    ):
        update_inquiry(db_conn, inquiry, **binding_values(iw, sender_reply_to_address=iw.contact_address))
