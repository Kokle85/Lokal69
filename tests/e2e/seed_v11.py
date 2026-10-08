"""SYNTHETIC spec v1.1 data for the dashboard browser E2E tests (seller inquiries, replies, controls,
mail-worker health). Nothing is ever sent: the E2E backend runs no worker, and every address is an
``example.invalid`` address that cannot receive mail.

Everything is created through the REAL repositories and the REAL mail-worker API (in-process ASGI
transport, the same path the desktop reply worker uses), exactly like the v1.1 API tests
(``tests/api/v11_support.py``, ``tests/integration/v11_inquiries/support.py``):

- inquiry controls in ``automatic`` mode, the standing authorization and a verified
  ``outlook_local`` sender binding of the main E2E workspace, plus one issued mail worker (its
  token exists only in this process's memory and is discarded);
- ``suppressed``: reserved, then the seller opted out (suppression; its quota debit is released);
- ``replied``: dispatched as an Outlook send intent, claimed, Sent Items reported (``accepted``),
  then a correlated seller reply quoting a final price, stating document availability and asking
  for a deposit and a reservation (escalations), plus a QUARANTINED possible match from another
  address quoting the inquiry's Message-ID and asking for a deposit (never "the seller asks");
- ``uncertain``: dispatched, claimed, handed to the Outbox without proof of submission;
- ``held``: no resolvable advertisement language (held for facts; English is never a fallback);
- ``waiting``: qualified while the rolling 24-hour cap (2) is used up, so it waits for the window;
- the mail worker's heartbeat is moved 6 hours into the past afterwards: the owner's PC looks
  powered off, so the health view must show a coverage gap, never "monitoring".

All listings are on one dedicated SYNTHETIC fixture source of the main workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from suv_deals.domain.enums import EmailProviderKind, SuppressionReason
from suv_deals.domain.language import LanguageDecision
from suv_deals.persistence import inquiries_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from tests.api.v11_support import (
    MAIL,
    accepted_send,
    claim_body,
    dispatch_intent,
    heartbeat_body,
    issue_worker,
    latest_binding_version,
    mail_worker_api,
    reply_body,
    report_body,
)
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.v11_db.support import SYNTHETIC_HOST, enabled_source
from tests.integration.v11_inquiries.support import (
    AUTHORIZATION,
    LANGUAGE_DE,
    UNRESOLVABLE_TEXT,
    Vehicle,
    World,
    alias,
    language_from,
    link,
    normalized,
    now_utc,
    qualify,
    record_contact,
    reserve_and_queue,
    system,
)

#: SYNTHETIC sender identity (``.invalid``: cannot receive mail; never the owner's mailbox).
SENDER_ADDRESS = "synthetic-sender@example.invalid"
SENDER_NAME = "Synthetic Sender"
SENDER_ACCOUNT = "synthetic-e2e-outlook-account"
WORKER_LABEL = "SYNTHETIC E2E desktop reply worker"

SELLER_REPLY = (
    "Guten Tag, das Fahrzeug ist noch verfügbar. Unser letzter Preis ist 26.500 EUR, Festpreis. "
    "Die Zulassungsbescheinigung liegt vor. Bitte überweisen Sie eine Anzahlung von 500 EUR, "
    "dann reservieren wir das Fahrzeug für Sie. SYNTHETIC E2E fixture reply only."
)
#: A possible match from ANOTHER address quoting the inquiry's Message-ID (quarantined). It asks for a
#: deposit: the dashboard must never present that as "the seller asks" (payment-fraud safety).
FORWARDED_REPLY = (
    "Weitergeleitet: bitte bei Interesse melden. Bitte überweisen Sie vorab eine Anzahlung von 300 EUR. "
    "SYNTHETIC E2E possible match from another address."
)


def seller_address(tag: str) -> str:
    return f"seller-{tag}-{uuid.uuid4().hex[:8]}@example.invalid"


def _listing(seed: Seed, ws: UUID, source_id: UUID, key: str, tag: str) -> tuple[UUID, UUID, str, str]:
    """A promoted, freshly observed listing on the dedicated SYNTHETIC v1.1 source."""
    reference = f"SYN-E2E-{tag.upper()}-{uuid.uuid4().hex[:6].upper()}"
    url = f"https://{SYNTHETIC_HOST}/vehicles/{reference}"
    now = now_utc()
    listing = seed.listing(
        ws,
        source_id,
        source_listing_id=reference,
        canonical_url=url,
        availability="available",
        eligibility_state="eligible_primary",
        eligibility_profile="primary",
        screening={"synthetic": True},
        screening_version="screening@synthetic",
        screened_at=now,
    )
    _, generation, observation = seed.detail_observation(ws, listing, promoted=True)
    revision = seed.revision(
        ws,
        listing,
        1,
        detail_generation=generation,
        observation_id=observation,
        semantic_hash=sha(unique("semantic")),
        asking_minor=275000,
    )
    seed.promote(ws, listing, revision, generation, observation)
    seed.conn.execute(
        "update app.listings set last_detail_success_at = now() where workspace_id = %s and id = %s",
        (ws, listing),
    )
    return listing, revision, reference, url


async def _vehicle(
    db: Database,
    seed: Seed,
    ws: UUID,
    source: tuple[UUID, str],
    tag: str,
    *,
    language: LanguageDecision | None = LANGUAGE_DE,
) -> Vehicle:
    source_id, key = source
    listing_id, revision_id, reference, url = _listing(seed, ws, source_id, key, tag)
    aliases = [alias("marketplace_seller_id", f"dealer-{reference}", key)]
    linked = await link(db, ws, aliases)
    address = seller_address(tag)
    vehicle = Vehicle(
        source_id=source_id,
        source_key=key,
        listing_id=listing_id,
        revision_id=revision_id,
        reference=reference,
        url=url,
        seller_entity_id=linked.entity_id,
        contact_id=None,
        address=address,
        listing=normalized(key, reference, url, observed_at=now_utc() - timedelta(minutes=30)),
    )
    contact = await record_contact(
        db, ws, vehicle, linked.entity_id, address=address, aliases=aliases, language=language
    )
    return replace(vehicle, contact_id=contact.id)


async def _controls(db: Database, ws: UUID) -> UUID:
    """Controls (automatic), the standing authorization and a verified ``outlook_local`` sender."""
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        controls = await inquiries_repo.ensure_controls(conn, actor)
        await inquiries_repo.record_authorization(
            conn, actor, AUTHORIZATION, reason="SYNTHETIC E2E standing authorization"
        )
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id=SENDER_ACCOUNT,
            from_address=SENDER_ADDRESS,
            display_name=SENDER_NAME,
            reason="SYNTHETIC E2E owner-authorized sending identity",
        )
        binding = await sender_bindings_repo.record_verification(
            conn,
            actor,
            binding.id,
            expected_version=binding.version,
            verified=True,
            alias_verified=True,
            health="healthy",
            reason="SYNTHETIC E2E technical verification",
        )
        await inquiries_repo.set_mode(
            conn,
            actor,
            expected_version=controls.version,
            mode="automatic",
            reason="SYNTHETIC sender verified",
        )
    return binding.id


def _power_off(conn: psycopg.Connection, ws: UUID, mailbox_id: UUID, hours: int) -> None:
    """TEST ARRANGEMENT ONLY (superuser, triggers bypassed for one statement): the worker's last
    heartbeat, reconciliation and sync are ``hours`` old, as after the owner's PC was shut down."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.mail_worker_checkpoints set heartbeat_at = heartbeat_at - make_interval(hours => %s),"
            " last_complete_scan_at = last_complete_scan_at - make_interval(hours => %s),"
            " mailbox_last_sync_at = mailbox_last_sync_at - make_interval(hours => %s)"
            " where workspace_id = %s and mailbox_binding_id = %s",
            (hours, hours, hours, ws, mailbox_id),
        )


async def seed_v11(db: Database, seed: Seed, ws: UUID, now: datetime) -> dict[str, Any]:
    """Seed the SYNTHETIC v1.1 world into the main E2E workspace; returns the manifest part."""
    sender_binding_id = await _controls(db, ws)
    source = enabled_source(seed, ws)
    vehicles = {
        tag: await _vehicle(db, seed, ws, source, tag)
        for tag in ("suppressed", "replied", "uncertain", "waiting")
    }
    vehicles["held"] = await _vehicle(db, seed, ws, source, "held", language=language_from(UNRESOLVABLE_TEXT))
    world = World(
        workspace_id=ws, seed=seed, sender_binding_id=sender_binding_id, vehicle=vehicles["replied"]
    )
    actor = system(ws)
    inquiries: dict[str, UUID] = {}
    replies: dict[str, UUID] = {}

    # Suppressed first: its reservation debit is released by the suppression, so it does not
    # count against the 24-hour cap the later sends use up.
    suppressed = await reserve_and_queue(db, world.with_vehicle(vehicles["suppressed"]))
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(vehicles["suppressed"].seller_entity_id),
            reason=SuppressionReason.SELLER_OPT_OUT,
            evidence={"synthetic": True, "note": "SYNTHETIC seller asked not to be contacted"},
        )
    inquiries["suppressed"] = suppressed.id

    async with mail_worker_api(db) as client:
        worker = await issue_worker(db, world, label=WORKER_LABEL)
        beat = await client.post(
            f"{MAIL}/heartbeat", headers=worker.headers(), json=heartbeat_body(worker.mailbox_id)
        )
        assert beat.status_code == 200, beat.text

        replied, intent = await accepted_send(client, db, world, worker)
        inquiries["replied"] = replied
        binding_version = await latest_binding_version(client, worker, replied)
        seller = await client.post(
            f"{MAIL}/replies",
            headers=worker.headers(f"e2e-reply-{uuid.uuid4().hex}"),
            json=reply_body(
                inquiry_id=replied,
                mailbox_id=worker.mailbox_id,
                binding_version=binding_version,
                from_address=str(vehicles["replied"].address),
                in_reply_to=intent.rfc_message_id,
                references=(intent.rfc_message_id,),
                subject="AW: Anfrage (SYNTHETIC E2E)",
                body=SELLER_REPLY,
                received_at=now_utc() - timedelta(minutes=20),
            ),
        )
        assert seller.status_code == 200, seller.text
        replies["seller"] = UUID(seller.json()["reply_id"])
        forwarded = await client.post(
            f"{MAIL}/replies",
            headers=worker.headers(f"e2e-reply-{uuid.uuid4().hex}"),
            json=reply_body(
                inquiry_id=replied,
                mailbox_id=worker.mailbox_id,
                binding_version=await latest_binding_version(client, worker, replied),
                from_address=seller_address("forwarder"),
                in_reply_to=intent.rfc_message_id,
                references=(intent.rfc_message_id,),
                subject="WG: Anfrage (SYNTHETIC E2E)",
                body=FORWARDED_REPLY,
                received_at=now_utc() - timedelta(minutes=10),
            ),
        )
        assert forwarded.status_code == 200, forwarded.text
        assert forwarded.json()["ingest_status"] == "quarantined", forwarded.text
        replies["quarantined"] = UUID(forwarded.json()["reply_id"])

        uncertain, pending = await dispatch_intent(
            db, world.with_vehicle(vehicles["uncertain"]), worker.mailbox_id
        )
        claim = await client.post(
            f"{MAIL}/send-intents/{pending.intent_id}/claim",
            headers=worker.headers(f"claim-{pending.intent_id}-{uuid.uuid4().hex}"),
            json=claim_body(pending, worker.mailbox_id),
        )
        assert claim.status_code == 200 and claim.json()["proceed"] is True, claim.text
        handed = await client.post(
            f"{MAIL}/send-intents/{pending.intent_id}/report",
            headers=worker.headers(f"report-{pending.intent_id}-submitted_to_outbox"),
            json=report_body(pending, "submitted_to_outbox"),
        )
        assert handed.status_code == 200, handed.text
        inquiries["uncertain"] = uncertain

    held, _, _ = await qualify(db, world, vehicles["held"])
    inquiries["held"] = held.id
    waiting, _, _ = await qualify(db, world, vehicles["waiting"])
    inquiries["waiting"] = waiting.id

    _power_off(seed.conn, ws, worker.mailbox_id, hours=6)
    return {
        "inquiries": {key: str(value) for key, value in inquiries.items()},
        "replies": {key: str(value) for key, value in replies.items()},
        "references": {key: vehicle.reference for key, vehicle in vehicles.items()},
        "listings": {key: str(vehicle.listing_id) for key, vehicle in vehicles.items()},
        "worker_label": WORKER_LABEL,
        "mailbox_id": str(worker.mailbox_id),
        "seeded_at": now.isoformat(),
    }


__all__ = ["SENDER_ADDRESS", "WORKER_LABEL", "seed_v11"]
