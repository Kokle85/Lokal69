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
- ``offline``: published as an Outlook send intent that no worker claims (the owner's PC is off):
  ``sending`` with a running desktop intent -> waiting ``WORKER_OFFLINE``;
- ``replied``: dispatched as an Outlook send intent, claimed, Sent Items reported (``accepted``,
  moved 25 hours into the past so the 24-hour cap has room for ``uncertain``),
  then a correlated seller reply quoting a final price, stating document availability and asking
  for a deposit and a reservation (escalations), plus a QUARANTINED possible match from another
  address quoting the inquiry's Message-ID and asking for a deposit (never "the seller asks");
- ``cooldown``: another vehicle of the ``replied`` seller, qualified right after that seller was
  contacted: its plan job waits ``INQUIRY_WAIT_SELLER_COOLDOWN`` (``SELLER_COOLDOWN``);
- ``uncertain``: dispatched, claimed, handed to the Outbox without proof of submission;
- ``held``: no resolvable advertisement language (held for facts; English is never a fallback);
- ``waiting``: qualified while the rolling 24-hour cap (2) is used up: its plan job waits
  ``INQUIRY_WAIT_RATE_CAP_REACHED`` (``RATE_CAP_REACHED``);
- ``killswitch``: qualified, then stopped by a ``kill_switch`` suppression of its vehicle: the
  suppression a resume can remove (``removable_suppressions`` = 1);
- the plan jobs of ``waiting`` and ``cooldown`` are the REAL ones (`enqueue_plan_job`), claimed and
  released with the plan handler's own wait code (`_hold_code` of the readiness decision) until
  the window frees, exactly like ``workers.inquiry_handlers._hold_plan`` (no worker runs here);
- mail workers: a retired worker (issued, then revoked: counted as a revoked mailbox) and the
  active one, whose credential expires within the 14-day notice (``expiring``); the active
  worker's heartbeat is moved 6 hours into the past afterwards: the owner's PC looks powered off,
  so the health view must show a coverage gap, never "monitoring".

All listings are on one dedicated SYNTHETIC source of the main workspace with REAL lineage
(``tests.integration.v11_db.support.enabled_source``: a non-fixture source mode at ingest, so
``app.listings.is_fixture = false`` and the inquiries reserve and dispatch like production ones;
fixture lineage is refused by the reservation, the dispatch and the worker claim). Nothing is ever
fetched from the synthetic host, and the E2E backend keeps ``SELLER_INQUIRY_MODE`` at its default,
so its inquiry control reports the closed process gate (nothing can be sent).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from suv_deals.domain.enums import EmailProviderKind, JobType, SuppressionReason
from suv_deals.domain.inquiries import InquiryReadinessDecision
from suv_deals.domain.language import LanguageDecision
from suv_deals.persistence import inquiries_repo, jobs, mail_workers_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.inquiries_repo import InquiryRecord
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.inquiry_handlers import _codes, _hold_code, enqueue_plan_job
from tests.api.v11_support import (
    MAIL,
    MailWorker,
    accepted_send,
    claim_body,
    dispatch_intent,
    heartbeat_body,
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
    backdate,
    complete_activation_canary,
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
RETIRED_WORKER_LABEL = "SYNTHETIC E2E retired desktop worker"
#: Inside the 14-day expiry notice: the health view reports the active credential ``expiring``.
ACTIVE_CREDENTIAL_LIFETIME = timedelta(days=10)

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
    seller_of: Vehicle | None = None,
) -> Vehicle:
    """A vehicle with its own dealer, or (``seller_of``) another vehicle of that vehicle's dealer
    (the same marketplace seller id and contact address, so it links to the same seller entity)."""
    source_id, key = source
    listing_id, revision_id, reference, url = _listing(seed, ws, source_id, key, tag)
    dealer = reference if seller_of is None else seller_of.reference
    aliases = [alias("marketplace_seller_id", f"dealer-{dealer}", key)]
    linked = await link(db, ws, aliases)
    if seller_of is not None:
        assert linked.entity_id == seller_of.seller_entity_id, "the second vehicle links to the same dealer"
    address = seller_address(tag) if seller_of is None or seller_of.address is None else seller_of.address
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


async def _issue_worker(
    db: Database, ws: UUID, sender_binding_id: UUID, *, label: str, lifetime: timedelta
) -> MailWorker:
    """A mailbox worker of the sender's mailbox (the token exists only in this process's memory)."""
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        issued = await mail_workers_repo.issue_mail_worker(
            conn, actor, sender_binding_id=sender_binding_id, label=label, lifetime=lifetime
        )
    return MailWorker(issued=issued, token=issued.credential.token.get_secret_value())


async def _retire_worker(db: Database, ws: UUID, sender_binding_id: UUID) -> UUID:
    """An earlier desktop worker the owner revoked (e.g. an old PC): counted, never usable. The
    owner's activation canary ran on it before it was retired (F3/OPS-04, wave D2: nothing is
    reserved without a completed canary of the current sender binding version)."""
    retired = await _issue_worker(
        db, ws, sender_binding_id, label=RETIRED_WORKER_LABEL, lifetime=timedelta(days=90)
    )
    await complete_activation_canary(db, ws, sender_binding_id)
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        revoked = await mail_workers_repo.revoke_mail_worker(
            conn, actor, retired.mailbox_id, reason="SYNTHETIC E2E: the old PC was retired"
        )
    assert revoked
    return retired.mailbox_id


async def _hold_plan_job(
    db: Database, ws: UUID, vehicle: Vehicle, record: InquiryRecord, decision: InquiryReadinessDecision
) -> str:
    """The plan step of a qualified inquiry that must wait, as ``inquiry_handlers._hold_plan`` does
    it: the REAL plan job (`enqueue_plan_job`) is claimed and released (`jobs.release`, audited, no
    attempt consumed) until the rolling window or the seller cooldown frees, with the handler's own
    wait code for the decision's hold reasons. Returns that code."""
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        job_id = await enqueue_plan_job(
            conn, actor, listing_id=vehicle.listing_id, revision_id=vehicle.revision_id, reason="e2e_seed"
        )
    assert job_id is not None
    claimed = await jobs.claim(db, ws, "e2e-seed-planner", [JobType.SELLER_INQUIRY_PLAN], lease_seconds=60)
    assert claimed is not None and claimed.id == job_id
    code = _hold_code(_codes(decision), "INQUIRY_WAIT_WINDOW")
    async with unit_of_work(db, actor) as conn:
        window = await inquiries_repo.next_window_at(
            conn, actor, seller_entity_id=record.seller_entity_id, exclude_inquiry_id=record.id
        )
        assert window.next_at is not None, "the wait has a known end (rolling window or cooldown)"
        await jobs.release(
            conn,
            claimed,
            available_at=window.next_at,
            code=code,
            detail="waiting for the rolling window or the seller cooldown",
            actor=actor,
        )
    return code


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
        for tag in ("suppressed", "offline", "replied", "uncertain", "waiting", "killswitch")
    }
    vehicles["held"] = await _vehicle(db, seed, ws, source, "held", language=language_from(UNRESOLVABLE_TEXT))
    vehicles["cooldown"] = await _vehicle(db, seed, ws, source, "cooldown", seller_of=vehicles["replied"])
    world = World(
        workspace_id=ws, seed=seed, sender_binding_id=sender_binding_id, vehicle=vehicles["replied"]
    )
    actor = system(ws)
    # One active mailbox per sender: the retired worker (which carried the owner's completed
    # activation canary) is issued and revoked first.
    retired_mailbox_id = await _retire_worker(db, ws, sender_binding_id)
    inquiries: dict[str, UUID] = {}
    replies: dict[str, UUID] = {}
    wait_codes: dict[str, str] = {}

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
        worker = await _issue_worker(
            db, ws, sender_binding_id, label=WORKER_LABEL, lifetime=ACTIVE_CREDENTIAL_LIFETIME
        )
        beat = await client.post(
            f"{MAIL}/heartbeat", headers=worker.headers(), json=heartbeat_body(worker.mailbox_id)
        )
        assert beat.status_code == 200, beat.text

        # Published to the desktop worker, which never claims it (the PC is off): ``sending`` with a
        # running intent. It counts against the rolling caps NOW (it may still be handed over).
        offline, _offline_intent = await dispatch_intent(
            db, world.with_vehicle(vehicles["offline"]), worker.mailbox_id
        )
        inquiries["offline"] = offline

        replied, intent = await accepted_send(client, db, world, worker)
        # TEST ARRANGEMENT (elapsed time): accepted by the provider 25 hours ago, so the 24-hour cap
        # leaves room for the uncertain send below; the dealer is still in its 7-day cooldown.
        backdate(seed.conn, replied, to=now_utc() - timedelta(hours=25))
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

        # The same dealer's other vehicle, right after the dealer was contacted: seller cooldown.
        cooldown, cooldown_decision, _ = await qualify(db, world, vehicles["cooldown"])
        assert "SELLER_COOLDOWN" in _codes(cooldown_decision), _codes(cooldown_decision)
        wait_codes["cooldown"] = await _hold_plan_job(
            db, ws, vehicles["cooldown"], cooldown, cooldown_decision
        )
        inquiries["cooldown"] = cooldown.id

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
    waiting, waiting_decision, _ = await qualify(db, world, vehicles["waiting"])
    assert "RATE_CAP_REACHED" in _codes(waiting_decision), _codes(waiting_decision)
    wait_codes["waiting"] = await _hold_plan_job(db, ws, vehicles["waiting"], waiting, waiting_decision)
    inquiries["waiting"] = waiting.id

    # Stopped by the kill switch before transmission: the suppression a resume can remove.
    stopped, _, _ = await qualify(db, world, vehicles["killswitch"])
    async with unit_of_work(db, actor) as conn:
        added = await inquiries_repo.add_suppression(
            conn,
            actor,
            scope="vehicle",
            key=f"listing_incarnation:{vehicles['killswitch'].listing_id}",
            reason=SuppressionReason.KILL_SWITCH,
            evidence={
                "synthetic": True,
                "note": "SYNTHETIC: stopped by the kill switch during an earlier pause",
            },
        )
    assert added.suppressed_inquiry_ids == (stopped.id,), added
    inquiries["killswitch"] = stopped.id

    _power_off(seed.conn, ws, worker.mailbox_id, hours=6)
    return {
        "inquiries": {key: str(value) for key, value in inquiries.items()},
        "replies": {key: str(value) for key, value in replies.items()},
        "references": {key: vehicle.reference for key, vehicle in vehicles.items()},
        "listings": {key: str(vehicle.listing_id) for key, vehicle in vehicles.items()},
        "wait_codes": wait_codes,
        "worker_label": WORKER_LABEL,
        "retired_worker_label": RETIRED_WORKER_LABEL,
        "mailbox_id": str(worker.mailbox_id),
        "retired_mailbox_id": str(retired_mailbox_id),
        "seeded_at": now.isoformat(),
    }


__all__ = [
    "ACTIVE_CREDENTIAL_LIFETIME",
    "RETIRED_WORKER_LABEL",
    "SENDER_ACCOUNT",
    "SENDER_ADDRESS",
    "WORKER_LABEL",
    "seed_v11",
]
