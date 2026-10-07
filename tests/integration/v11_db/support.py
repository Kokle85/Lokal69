"""Synthetic builders for the spec v1.1 section 37 database tests (migration 20261006001000).

Everything here is SYNTHETIC: reserved example domains, fixture sources, invented listing
references. No real seller, address, vehicle, mailbox or credential appears, and nothing is
ever sent anywhere: these tests only exercise PostgreSQL constraints, triggers, RLS and grants.

Arrangement uses the superuser test connection (`Seed`, RLS bypassed, triggers active). The
behaviour under test runs as ``suv_backend`` with the transaction-local workspace GUC, exactly
as the repository layer does (`backend()` from the M2 helpers).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import T0, Seed, backend, sha, unique

from suv_deals.domain.inquiries import InquiryIdentity, VehicleIdentityRef
from suv_deals.domain.seller_templates import (
    RenderedMessage,
    VehicleLabel,
    render,
    render_preview_mk,
)

SV_APPEND_ONLY = "SV001"
SV_TRANSITION = "SV002"
SV_REFERENCE = "SV003"
SV_FROZEN = "SV004"
SV_MONOTONIC = "SV005"

SYNTHETIC_HOST = "synthetic-dealer.example"
SENDER_ADDRESS = "inquiries@synthetic-mail.example"
SENDER_NAME = "Synthetic Sender"
PURPOSE = "initial_availability_documents_price"

ALL_STATES = (
    "candidate",
    "qualifying",
    "reserved",
    "queued",
    "sending",
    "accepted",
    "held_facts",
    "uncertain",
    "suppressed",
    "failed_definite",
    "cancelled",
    "replied",
    "bounced",
    "seller_opted_out",
    "no_reply_yet",
)

#: Every table added by migration 20261006001000.
V11_TABLES = (
    "app.seller_entities",
    "app.seller_entity_aliases",
    "app.seller_contacts",
    "app.seller_inquiry_authorizations",
    "app.seller_inquiry_controls",
    "app.seller_inquiries",
    "app.seller_replies",
    "app.seller_reply_locators",
    "app.availability_events",
    "ops.email_sender_bindings",
    "ops.email_delivery_attempts",
    "ops.email_suppressions",
    "ops.inquiry_quota_ledger",
    "ops.mail_worker_bindings",
    "ops.mail_worker_checkpoints",
    "ops.mail_ingest_dedup",
    "ops.mail_binding_sync",
)
APPEND_ONLY_V11 = (
    "app.seller_inquiry_authorizations",
    "app.seller_reply_locators",
    "app.availability_events",
    "ops.mail_binding_sync",
)

_ALLOWED_DATA = [
    "verified_sender_display_name",
    "verified_sender_email",
    "vehicle_make_model",
    "listing_reference",
    "listing_url",
    "three_permitted_questions",
]
_EXCLUDED_DATA = [
    "home_address",
    "telephone",
    "identity_documents",
    "bank_details",
    "finances",
    "acquisition_budget",
    "target_resale_price",
    "profit_calculation",
    "unrelated_business_information",
]
_NOT_AUTHORIZED = [
    "follow_up",
    "outgoing_reply",
    "offer",
    "price_acceptance",
    "negotiation_beyond_lowest_price",
    "reservation",
    "viewing_appointment",
    "deposit",
    "purchase",
    "resale_promise",
    "payment",
]


@contextmanager
def expect_sqlstate(code: str, fragment: str | None = None) -> Iterator[None]:
    """Expect a database error with this SQLSTATE (and message fragment) inside the block."""
    with pytest.raises(psycopg.Error) as exc:
        yield
    assert exc.value.sqlstate == code, (exc.value.sqlstate, str(exc.value))
    if fragment is not None:
        assert fragment in str(exc.value), str(exc.value)


def identity_key(workspace_id: UUID, vehicle_kind: str, vehicle_id: UUID, seller_entity_id: UUID) -> str:
    """The inquiry identity hash, computed by the DOMAIN (the DB check must agree)."""
    return InquiryIdentity(
        workspace_id=workspace_id,
        vehicle=VehicleIdentityRef.model_validate({"kind": vehicle_kind, "id": vehicle_id}),
        seller_key=f"seller_entity:{seller_entity_id}",
    ).key()


def rendered(language: str, reference: str, url: str) -> tuple[RenderedMessage, RenderedMessage]:
    """A real domain rendering of the spec 37.4 template plus its informational MK preview."""
    message = render(
        f"seller_initial_{language}_v1",
        VehicleLabel(text="Example Trail", make="Example", model="Trail"),
        reference,
        url,
        SENDER_NAME,
        verified_listing_url=url,
    )
    return message, render_preview_mk(message)


@dataclass(frozen=True)
class Vehicle:
    """One listing (on its own enabled fixture source) with a promoted revision."""

    source_id: UUID
    source_key: str
    listing_id: UUID
    revision_id: UUID
    semantic_hash: str
    reference: str
    url: str


@dataclass(frozen=True)
class InquiryWorld:
    workspace_id: UUID
    seed: Seed = field(repr=False)
    vehicle: Vehicle
    seller_entity_id: UUID
    contact_id: UUID
    contact_address: str
    authorization_id: UUID
    sender_binding_id: UUID
    controls_id: UUID

    @property
    def listing_id(self) -> UUID:
        return self.vehicle.listing_id


def enabled_source(seed: Seed, workspace_id: UUID) -> tuple[UUID, str]:
    key = unique("v11src").lower()
    source_id = seed.source(
        workspace_id,
        source_key=key,
        enabled=True,
        adapter_version="fixture@1.0.0",
        terms_status="permitted",
        terms_decision="proceed_permitted",
        terms_decision_actor="synthetic owner decision",
        technical_status="fixture_tested",
        allowed_hosts=[SYNTHETIC_HOST],
        allowed_search_paths=["/search"],
        allowed_detail_paths=["/vehicles/"],
    )
    return source_id, key


def vehicle(seed: Seed, workspace_id: UUID, *, profile: str = "primary") -> Vehicle:
    source_id, key = enabled_source(seed, workspace_id)
    reference = unique("SYN").upper().replace("_", "-")
    url = f"https://{SYNTHETIC_HOST}/vehicles/{reference}"
    listing = seed.listing(
        workspace_id,
        source_id,
        source_listing_id=reference,
        canonical_url=url,
        availability="available",
        eligibility_state="eligible_primary" if profile == "primary" else "eligible_manual_profile",
        eligibility_profile=profile,
        screening={"synthetic": True},
        screening_version="screening@synthetic",
        screened_at=T0,
    )
    _, gen, obs = seed.detail_observation(workspace_id, listing, promoted=True)
    semantic = sha(unique("semantic"))
    revision = seed.revision(
        workspace_id, listing, 1, detail_generation=gen, observation_id=obs, semantic_hash=semantic
    )
    seed.promote(workspace_id, listing, revision, gen, obs)
    return Vehicle(source_id, key, listing, revision, semantic, reference, url)


def seller_entity(seed: Seed, workspace_id: UUID, **cols: Any) -> UUID:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "seller_type": "dealer",
        "display_name": "Synthetic Autohaus (fixture)",
    }
    values.update(cols)
    return seed.insert_id("app.seller_entities", **values)


def contact(
    seed: Seed,
    workspace_id: UUID,
    veh: Vehicle,
    seller: UUID,
    *,
    address: str | None = "",
    status: str = "verified",
    language: str | None = "de",
    language_status: str = "resolved",
    language_basis: str = "seller_ad_text",
    evidence_kind: str = "email_on_advertisement",
    **cols: Any,
) -> tuple[UUID, str]:
    # "" (default) generates a fresh synthetic address; None records "no e-mail available".
    addr = f"{unique('seller')}@{SYNTHETIC_HOST}" if address == "" else address
    kinds = {
        "email_on_advertisement": "ad_email",
        "marketplace_relay_for_listing": "marketplace_relay",
        "official_dealer_contact_via_listing": "official_dealer_contact",
    }
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "source_id": veh.source_id,
        "listing_id": veh.listing_id,
        "listing_revision_id": veh.revision_id,
        "listing_revision_number": 1,
        "seller_entity_id": seller,
        "address": addr,
        "contact_kind": kinds.get(evidence_kind),
        "evidence_kind": evidence_kind,
        "listing_reference": veh.reference,
        "listing_url": veh.url,
        "extraction_location": {
            "marketplace_relay_for_listing": "listing_relay_contact",
            "official_dealer_contact_via_listing": "dealer_page_linked_from_listing",
        }.get(evidence_kind, "listing_contact_block"),
        "extraction_excerpt": "E-Mail: (synthetic fixture excerpt)",
        "language_code": language,
        "language_status": language_status,
        "language_basis": language_basis,
        "language_confidence": "0.95",
        "status": status,
        "rules_version": "seller_contacts@1.0.0",
        "observed_at": T0,
        "verified_at": T0 if status == "verified" else None,
    }
    if evidence_kind == "marketplace_relay_for_listing":
        values["relay_listing_reference"] = veh.reference
    values.update(cols)
    # The address is "" when the evidence records that no e-mail is available.
    return seed.insert_id("app.seller_contacts", **values), addr or ""


def authorization(seed: Seed, workspace_id: UUID, version: int = 1, **cols: Any) -> UUID:
    effective = min(T0.date(), datetime.now(UTC).date() - timedelta(days=1))
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "version": version,
        "owner_label": "Synthetic owner",
        "effective_date": effective,
        "recorded_at": effective,
        "source": "synthetic test authorization record (spec 37.1 shape)",
        "languages": ["de", "it", "fr", "en"],
        "allowed_outgoing_data_categories": _ALLOWED_DATA,
        "excluded_data_categories": _EXCLUDED_DATA,
        "not_authorized": _NOT_AUTHORIZED,
        "profiles_in_scope": ["primary"],
        "record": {"synthetic": True, "version": version},
        "record_hash": sha(f"authorization-{workspace_id}-{version}"),
    }
    values.update(cols)
    return seed.insert_id("app.seller_inquiry_authorizations", **values)


def controls(seed: Seed, workspace_id: UUID, **cols: Any) -> UUID:
    values: dict[str, Any] = {"workspace_id": workspace_id, "mode": "automatic"}
    values.update(cols)
    return seed.insert_id("app.seller_inquiry_controls", **values)


def sender_binding(seed: Seed, workspace_id: UUID, **cols: Any) -> UUID:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "provider": "outlook_local",
        "account_id": "synthetic-outlook-account",
        "from_address": SENDER_ADDRESS,
        "display_name": SENDER_NAME,
        "alias_verified": True,
        "alias_verified_at": T0,
        "health": "healthy",
        "health_checked_at": T0,
        "verified_at": T0,
        "verified_by": uuid.uuid4(),
    }
    values.update(cols)
    return seed.insert_id("ops.email_sender_bindings", **values)


def inquiry_world(seed: Seed, name: str = "V11 inquiries") -> InquiryWorld:
    ws = seed.workspace(name)
    seed.profile(ws, "primary")
    veh = vehicle(seed, ws)
    seller = seller_entity(seed, ws)
    contact_id, address = contact(seed, ws, veh, seller)
    auth = authorization(seed, ws)
    sender = sender_binding(seed, ws)
    ctl = controls(seed, ws)
    return InquiryWorld(ws, seed, veh, seller, contact_id, address, auth, sender, ctl)


def with_vehicle(world: InquiryWorld, *, seller: UUID | None = None, language: str = "de") -> InquiryWorld:
    """Another listing (new source) of the same workspace with its own verified contact."""
    veh = vehicle(world.seed, world.workspace_id)
    seller_id = seller or world.seller_entity_id
    contact_id, address = contact(world.seed, world.workspace_id, veh, seller_id, language=language)
    return replace(
        world, vehicle=veh, seller_entity_id=seller_id, contact_id=contact_id, contact_address=address
    )


def fresh_seller(world: InquiryWorld) -> InquiryWorld:
    """The same listing with another (unverified-contact) seller entity: a distinct identity."""
    seller = seller_entity(world.seed, world.workspace_id)
    return replace(world, seller_entity_id=seller)


def insert_inquiry(
    conn: psycopg.Connection,
    world: InquiryWorld,
    *,
    state: str = "qualifying",
    vehicle_kind: str = "listing_incarnation",
    cluster_id: UUID | None = None,
    as_backend: bool = True,
    **cols: Any,
) -> UUID:
    vehicle_id = cluster_id if vehicle_kind == "vehicle_cluster" else world.listing_id
    assert vehicle_id is not None
    values: dict[str, Any] = {
        "workspace_id": world.workspace_id,
        "identity_key": identity_key(world.workspace_id, vehicle_kind, vehicle_id, world.seller_entity_id),
        "vehicle_kind": vehicle_kind,
        "vehicle_cluster_id": cluster_id if vehicle_kind == "vehicle_cluster" else None,
        "vehicle_listing_id": world.listing_id if vehicle_kind == "listing_incarnation" else None,
        "seller_entity_id": world.seller_entity_id,
        "qualification_listing_id": world.listing_id,
        "state": state,
    }
    values.update(cols)
    query = sql.SQL("insert into app.seller_inquiries ({}) values ({}) returning id").format(
        sql.SQL(", ").join(sql.Identifier(k) for k in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    params = [_adapt(v) for v in values.values()]
    if as_backend:
        with backend(conn, world.workspace_id):
            row = conn.execute(query, params).fetchone()
    else:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    inquiry_id = row[0]
    assert isinstance(inquiry_id, UUID)
    return inquiry_id


def binding_values(world: InquiryWorld, *, language: str = "de", **overrides: Any) -> dict[str, Any]:
    message, preview = rendered(language, world.vehicle.reference, world.vehicle.url)
    values: dict[str, Any] = {
        "qualification_revision_id": world.vehicle.revision_id,
        "qualification_revision_number": 1,
        "qualified_semantic_hash": world.vehicle.semantic_hash,
        "qualified_price_minor": 275000,
        "qualified_currency": "EUR",
        "qualified_availability": "available",
        "readiness": "inquiry_ready",
        "readiness_reasons": [],
        "readiness_rationale_hash": sha(unique("rationale")),
        "readiness_rules_version": "inquiry_readiness@1.0.0",
        "readiness_evaluated_at": T0,
        "authorization_id": world.authorization_id,
        "authorization_version": 1,
        "authorization_fingerprint": sha("authorization-fingerprint"),
        "template_id": message.template_id,
        "template_version": message.template_version,
        "template_hash": message.template_hash,
        "template_set_version": message.template_set_version,
        "language": language,
        "scope_hash": message.scope_hash,
        "body_hash": message.body_hash,
        "binding_hash": sha(unique("binding")),
        "original_subject": message.subject,
        "original_body": message.body,
        "mk_preview_subject": preview.subject,
        "mk_preview_body": preview.body,
        "mk_preview_hash": preview.body_hash,
        "sender_binding_id": world.sender_binding_id,
        "sender_binding_version": 1,
        "sender_provider": "outlook_local",
        "sender_account_id": "synthetic-outlook-account",
        "sender_from_address": SENDER_ADDRESS,
        "sender_display_name": SENDER_NAME,
        "sender_reply_to_address": None,
        "recipient_contact_id": world.contact_id,
        "recipient_address": world.contact_address,
        "recipient_binding_hash": sha(unique("recipient")),
    }
    values.update(overrides)
    return values


def _adapt(value: Any) -> Any:
    """dicts (and lists of dicts) are JSON documents; other lists are PostgreSQL arrays."""
    if isinstance(value, Mapping) or (isinstance(value, list) and any(isinstance(v, Mapping) for v in value)):
        return Jsonb(value)
    return value


def update_inquiry(conn: psycopg.Connection, inquiry_id: UUID, **cols: Any) -> int:
    query = sql.SQL("update app.seller_inquiries set {} where id = {}").format(
        sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder()) for k in cols),
        sql.Placeholder(),
    )
    return conn.execute(query, [*(_adapt(v) for v in cols.values()), inquiry_id]).rowcount


def debit(conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID) -> UUID:
    row = conn.execute(
        "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id) values (%s, %s) returning id",
        (world.workspace_id, inquiry_id),
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def reserve(conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID, **overrides: Any) -> None:
    """Bind and reserve in one backend transaction: quota debit + qualifying -> reserved."""
    with backend(conn, world.workspace_id):
        debit(conn, world, inquiry_id)
        assert update_inquiry(conn, inquiry_id, state="reserved", **binding_values(world, **overrides)) == 1


def queue(conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID) -> None:
    with backend(conn, world.workspace_id):
        assert update_inquiry(conn, inquiry_id, state="queued") == 1


#: ``domain.inquiries.ReconciliationEvidence`` that proves non-submission (a documented
#: pre-submission proof, no live worker, nothing pending in the Outbox, no positive hit).
PROVEN_NOT_SUBMITTED: dict[str, Any] = {
    "sent_items": "not_found",
    "provider_search": "unsupported",
    "outbox_pending": "no",
    "proven_not_submitted": "credentials_rejected_before_submit",
    "worker_alive": "no",
    "correlated_inbound": False,
}


def outbound_message_id(inquiry_id: UUID) -> str:
    """The synthetic stable Message-ID of an inquiry's send intent (replies reference it)."""
    return f"<inquiry-{inquiry_id}@synthetic-mail.example>"


def attempt_values(world: InquiryWorld, inquiry_id: UUID, number: int = 1, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": world.workspace_id,
        "inquiry_id": inquiry_id,
        "attempt_id": uuid.uuid4(),
        "attempt_number": number,
        "rfc_message_id": outbound_message_id(inquiry_id),
        "sender_binding_id": world.sender_binding_id,
        "sender_binding_version": 1,
        "provider": "outlook_local",
        "fencing_token": number,
        "lease_owner": "synthetic-dispatcher-1",
        "lease_token": uuid.uuid4(),
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    values.update(cols)
    return values


def insert_attempt(conn: psycopg.Connection, values: Mapping[str, Any]) -> UUID:
    query = sql.SQL("insert into ops.email_delivery_attempts ({}) values ({}) returning id").format(
        sql.SQL(", ").join(sql.Identifier(k) for k in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    row = conn.execute(query, [_adapt(v) for v in values.values()]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def dispatch(conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID, number: int = 1) -> UUID:
    """queued -> sending plus the committed send intent (running attempt), one transaction."""
    with backend(conn, world.workspace_id):
        assert update_inquiry(conn, inquiry_id, state="sending") == 1
        return insert_attempt(conn, attempt_values(world, inquiry_id, number))


def finish_attempt(conn: psycopg.Connection, attempt_row_id: UUID, outcome: str, **cols: Any) -> None:
    values: dict[str, Any] = {"outcome": outcome, "finished_at": datetime.now(UTC)}
    values.update(cols)
    query = sql.SQL("update ops.email_delivery_attempts set {} where id = {}").format(
        sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder()) for k in values),
        sql.Placeholder(),
    )
    assert conn.execute(query, [*(_adapt(v) for v in values.values()), attempt_row_id]).rowcount == 1


def accept(conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID, attempt_row_id: UUID) -> None:
    with backend(conn, world.workspace_id):
        finish_attempt(conn, attempt_row_id, "accepted", provider_message_id="synthetic-provider-msg-1")
        assert update_inquiry(conn, inquiry_id, state="accepted") == 1


def sent_inquiry(conn: psycopg.Connection, world: InquiryWorld) -> tuple[UUID, UUID]:
    """A complete, legitimate flow up to provider acceptance. Returns (inquiry, attempt row)."""
    inquiry_id = insert_inquiry(conn, world)
    reserve(conn, world, inquiry_id)
    queue(conn, world, inquiry_id)
    attempt = dispatch(conn, world, inquiry_id)
    accept(conn, world, inquiry_id, attempt)
    return inquiry_id, attempt


def state_of(conn: psycopg.Connection, inquiry_id: UUID) -> str:
    row = conn.execute("select state from app.seller_inquiries where id = %s", (inquiry_id,)).fetchone()
    assert row is not None
    return str(row[0])


def audit_event(seed: Seed, workspace_id: UUID, target_type: str, target_id: UUID, action: str) -> UUID:
    return seed.insert_id(
        "ops.audit_events",
        workspace_id=workspace_id,
        actor_principal_id=uuid.uuid4(),
        actor_kind="user",
        actor_role="owner",
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason="synthetic owner decision for a test",
    )


def confirmed_cluster(seed: Seed, workspace_id: UUID, listings: list[UUID]) -> UUID:
    cluster = seed.insert_id(
        "app.vehicle_clusters",
        workspace_id=workspace_id,
        confidence="high",
        review_status="confirmed",
        reviewed_by=uuid.uuid4(),
        reviewed_at=T0,
        match_basis={"synthetic": "same VIN fixture"},
    )
    for listing in listings:
        seed.insert(
            "app.vehicle_cluster_members",
            workspace_id=workspace_id,
            cluster_id=cluster,
            listing_id=listing,
            confidence="high",
        )
    return cluster


def suppression(
    seed: Seed, workspace_id: UUID, scope: str, key: str, reason: str = "manual", **cols: Any
) -> UUID:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "scope": scope,
        "scope_key": key,
        "reason": reason,
        "created_by_kind": "system",
        "evidence": {"synthetic": True},
    }
    values.update(cols)
    return seed.insert_id("ops.email_suppressions", **values)


# --- mailbox route -------------------------------------------------------------------------


def mail_credential(seed: Seed, workspace_id: UUID, scopes: list[str] | None = None, **cols: Any) -> UUID:
    """A synthetic mailbox-worker credential row (only its hash; no token exists).

    Migration ``20261007000400`` binds mailboxes only to ``credential_kind = 'mail_worker'``
    credentials (``suvmail_`` prefix, exactly ``mail:ingest``, owner role). That is the default
    here; a test that probes another shape (extra scopes, another role) gets a ``static_bearer``
    row unless it names ``credential_kind`` itself, because ``api_credentials_mail_worker_ck``
    refuses any other shape for the mail_worker kind before the binding guard could run.
    """
    wanted = scopes or ["mail:ingest"]
    narrow = wanted == ["mail:ingest"] and cols.get("role", "owner") == "owner"
    kind = cols.pop("credential_kind", "mail_worker" if narrow else "static_bearer")
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "principal_id": uuid.uuid4(),
        "principal_kind": "mcp_client",
        "role": "owner",
        "credential_kind": kind,
        "token_hash": sha(unique("mail-worker-token")),
        "scopes": wanted,
        "label": "Synthetic mailbox worker",
        "expires_at": datetime.now(UTC) + timedelta(days=30),
    }
    if kind == "mail_worker":
        values["token_prefix"] = f"suvmail_{uuid.uuid4().hex[:6]}"
    values.update(cols)
    return seed.insert_id("ops.api_credentials", **values)


def mailbox(seed: Seed, world: InquiryWorld, *, sender_binding_id: UUID | None = None, **cols: Any) -> UUID:
    values: dict[str, Any] = {
        "workspace_id": world.workspace_id,
        "sender_binding_id": sender_binding_id or world.sender_binding_id,
        "credential_id": mail_credential(seed, world.workspace_id),
        "provider": "outlook_local",
        "account_address": SENDER_ADDRESS,
        "store_id_hash": sha("synthetic-store"),
        "worker_label": "Synthetic desktop worker",
    }
    values.update(cols)
    return seed.insert_id("ops.mail_worker_bindings", **values)


def publish_binding(
    conn: psycopg.Connection,
    world: InquiryWorld,
    mailbox_id: UUID,
    inquiry_id: UUID,
    version: int = 1,
    state: str = "active",
) -> int:
    payload: dict[str, Any] = {} if state == "tombstoned" else {"synthetic": True, "aliases": ["redacted"]}
    with backend(conn, world.workspace_id):
        row = conn.execute(
            "insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id,"
            " inquiry_id, binding_version,"
            " binding_state, payload) values (%s, %s, %s, %s, %s, %s) returning sequence",
            (world.workspace_id, mailbox_id, inquiry_id, version, state, Jsonb(payload)),
        ).fetchone()
    assert row is not None
    return int(row[0])


def reply_values(world: InquiryWorld, inquiry_id: UUID, mailbox_id: UUID, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": world.workspace_id,
        "inquiry_id": inquiry_id,
        "mailbox_binding_id": mailbox_id,
        "binding_version": 1,
        "internet_message_id": f"<{unique('reply')}@synthetic-dealer.example>",
        "from_address": world.contact_address,
        # Linked by Message-ID to the inquiry's send intent (never by subject).
        "in_reply_to": outbound_message_id(inquiry_id),
        "reference_ids": [outbound_message_id(inquiry_id)],
        "subject": "AW: Anfrage (synthetic fixture)",
        "sanitized_body": "Synthetic fixture only: the vehicle is available.",
        "body_sanitizer_version": "reply-sanitizer/1",
        "source_fingerprint": sha(unique("fingerprint")),
        "fingerprint_version": "reply-source-fingerprint/1",
        "received_at": T0 + timedelta(days=1),
        "observed_at": T0 + timedelta(days=1, seconds=2),
        "detected_language": "de",
    }
    values.update(cols)
    return values


def insert_reply(conn: psycopg.Connection, values: Mapping[str, Any]) -> UUID:
    if isinstance(values.get("attachments"), list):
        values = {**values, "attachments": Jsonb(values["attachments"])}
    query = sql.SQL("insert into app.seller_replies ({}) values ({}) returning id").format(
        sql.SQL(", ").join(sql.Identifier(k) for k in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    row = conn.execute(query, [_adapt(v) for v in values.values()]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def insert_row(conn: psycopg.Connection, table: str, values: Mapping[str, Any]) -> UUID:
    schema, name = table.split(".", 1)
    query = sql.SQL("insert into {} ({}) values ({}) returning id").format(
        sql.Identifier(schema, name),
        sql.SQL(", ").join(sql.Identifier(k) for k in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    row = conn.execute(query, [_adapt(v) for v in values.values()]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def today_utc() -> date:
    return datetime.now(UTC).date()


# --- arranged history and concurrency ------------------------------------------------------


def arrange_inquiry_history(conn: psycopg.Connection, inquiry_id: UUID, **cols: Any) -> None:
    """Rewrite lifecycle timestamps to simulate elapsed time (TEST ARRANGEMENT ONLY).

    Triggers are bypassed for this one statement as the superuser test role; neither
    ``suv_backend`` nor any repository can do this (the columns are database-owned)."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        assert update_inquiry(conn, inquiry_id, **cols) == 1


def reserve_with_debit_at(
    conn: psycopg.Connection, world: InquiryWorld, inquiry_id: UUID, debited_at: datetime
) -> None:
    """Reserve with a historical quota debit (the documented owner maintenance path)."""
    with conn.transaction():
        conn.execute("select set_config('app.history_maintenance', 'on', true)")
        conn.execute(
            "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id, debited_at) values (%s, %s, %s)",
            (world.workspace_id, inquiry_id, debited_at),
        )
        assert update_inquiry(conn, inquiry_id, state="reserved", **binding_values(world)) == 1


def race(db_url: str, *jobs: Callable[[psycopg.Connection], object]) -> list[BaseException | None]:
    """Run each job on its own connection, released together; return the error of each (or None)."""
    barrier = threading.Barrier(len(jobs))
    results: list[BaseException | None] = [None] * len(jobs)

    def run(index: int, job: Callable[[psycopg.Connection], object]) -> None:
        try:
            with psycopg.connect(db_url, autocommit=True) as conn:
                conn.execute("set lock_timeout = '20s'")
                barrier.wait(timeout=30)
                job(conn)
        except BaseException as exc:
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return results


def single_success(results: list[BaseException | None], sqlstate: str) -> None:
    """Exactly one job succeeded; every other one failed with ``sqlstate``."""
    assert sum(r is None for r in results) == 1, results
    errors = [r for r in results if r is not None]
    assert len(errors) == len(results) - 1
    for error in errors:
        assert isinstance(error, psycopg.Error), error
        assert error.sqlstate == sqlstate, (error.sqlstate, str(error))
