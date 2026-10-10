"""Builders for the spec v1.1 runtime-worker tests (work package B2a; spec 37.1-37.7, 37.10).

Everything is SYNTHETIC and nothing leaves the process:

- the "live" dealer source is a copy of the hand-written ``fixture_dealer_de`` pages registered
  under a NON-fixture source key and mode (``public_html``), so its listings carry REAL lineage
  (``app.listings.is_fixture = false``, frozen at ingest). Its pages are served by the in-memory
  `FixtureCrawlClient` injected as the runtime's network client, behind a fake resolver; the
  runtime's ``SOURCE_NETWORK_ENABLED`` is switched on ONLY inside that runtime so the in-memory
  client is used -- no crawler client, socket or DNS lookup exists;
- the MK comparables and the EUR/MKD rate are invented, labelled real-lineage observations (a
  real-lineage listing is only ever valued against real-lineage evidence);
- sender, seller and Message-ID addresses use reserved example domains; the owner's sending
  identity is ``owner-inquiries@example.invalid``;
- the desktop mailbox worker is simulated through ``send_intents_repo`` (claim + report) and
  ``replies_repo.ingest_reply``; Gmail and Slack are ``httpx.MockTransport`` doubles.

Arrangement the application cannot do itself (time travel, worker-health ageing) uses the
superuser ``seed`` connection; everything under test runs as ``suv_backend`` through the runtime.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import yaml
from pydantic import SecretStr
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    DASHBOARD,
    DEALER_DE,
    REPO,
    SLACK_CHANNEL,
    PipelineEnv,
    fixture_source_config,
    pipeline_settings,
    run,
    synthetic_taxonomy,
    system,
    user_actor,
)
from tests.integration.v11_inquiries.support import complete_activation_canary

from suv_deals.clock import SystemClock
from suv_deals.domain.comparables import MarketObservation
from suv_deals.domain.enums import (
    Confidence,
    Drive,
    EmailProviderKind,
    EvidenceKind,
    Fuel,
    FxPurpose,
    Gearbox,
    JobType,
    SellerType,
    SourceMode,
)
from suv_deals.domain.inquiries import load_seller_inquiry_authorization
from suv_deals.domain.language import AdTextFragment, LanguageDecision, resolve_inquiry_language
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.replies import ReplyIngestRequest
from suv_deals.domain.seller_contacts import (
    ExtractionLocation,
    RecipientEvidence,
    RecipientEvidenceKind,
    SellerAlias,
    SellerIdentity,
    verify_recipient,
)
from suv_deals.integrations.email_providers.outlook_local import (
    OutlookSendIntent,
    OutlookSendReport,
    OutlookSubmissionState,
)
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.persistence import (
    bindings_repo,
    config_repo,
    inquiries_repo,
    mail_workers_repo,
    market_repo,
    replies_repo,
    sellers_repo,
    send_intents_repo,
    sender_bindings_repo,
    sources_repo,
    valuation_repo,
)
from suv_deals.persistence.database import Conn
from suv_deals.persistence.mail_workers_repo import WorkerIdentity
from suv_deals.persistence.replies_repo import ReplyIngestOptions, ReplyIngestOutcome
from suv_deals.persistence.sender_bindings_repo import OAuthRefreshGrant
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.runner import JobRunReport, Worker
from suv_deals.workers.runtime import RuntimeOptions, build_runtime

LIVE_KEY = "synthetic_dealer_live"
ELIGIBLE_REF = "TEST-204"  # the one eligible_primary car of the dealer pages (EUR 2,750 gross)
SENDER_ADDRESS = "owner-inquiries@example.invalid"
SENDER_ACCOUNT = "synthetic-owner-account"
GMAIL_ACCOUNT = "owner-inquiries@example.invalid"
SECRET_KEY = base64.b64encode(b"R" * 32).decode("ascii")  # SYNTHETIC secret-box key
PUBLIC_IP = "93.184.215.14"  # documentation-style answer of the fake resolver
AUTHORIZATION = load_seller_inquiry_authorization()
DE_TEXT = (
    "Verkaufe unseren gepflegten Geländewagen. Fahrzeug ist unfallfrei, TÜV neu, Scheckheft gepflegt. "
    "Nichtraucherfahrzeug mit Anhängerkupplung und Sitzheizung."
)
WORKER_REQ = "req-desktop-worker"


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return [PUBLIC_IP]


def now_utc() -> datetime:
    return datetime.now(UTC)


# =============================================================================================
# Settings and the runtime environment
# =============================================================================================


def runtime_settings(db_url: str, **overrides: Any) -> Settings:
    """Automatic mode on the default ``outlook_local`` route with the configured sending identity
    (``SELLER_EMAIL_ACCOUNT_ID`` / ``SELLER_EMAIL_FROM``, required in automatic mode); external
    notifications OFF."""
    values: dict[str, Any] = {
        "seller_inquiry_mode": "automatic",
        "seller_email_provider": "outlook_local",
        "seller_email_account_id": SENDER_ACCOUNT,
        "seller_email_from": SENDER_ADDRESS,
    }
    values.update(overrides)
    return pipeline_settings(db_url, **values)


def slack_signal_settings(db_url: str, **overrides: Any) -> Settings:
    """External notifications allowed with a (SYNTHETIC) Slack app; MCP Events stays the
    candidate route; seller replies go to the Slack route of their own category."""
    values: dict[str, Any] = {
        "allow_external_notifications": True,
        "seller_reply_signal_provider": "slack",
        "slack_bot_token": "xoxb-SYNTHETIC-test-token-not-real",
        "slack_signing_secret": "synthetic-signing-secret",
        "slack_channel_id": SLACK_CHANNEL,
    }
    values.update(overrides)
    return runtime_settings(db_url, **values)


def live_fixture_dir(tmp_path: Path, page_overrides: Mapping[str, str] | None = None) -> Path:
    """A copy of the synthetic dealer pages registered under the live (non-fixture) source key.

    ``page_overrides`` replaces copied page files (file name -> SYNTHETIC html) in the copy only.
    """
    target = tmp_path / LIVE_KEY
    shutil.copytree(DEALER_DE, target)
    for name, html in (page_overrides or {}).items():
        (target / name).write_text(html, encoding="utf-8")
    manifest = yaml.safe_load((target / "MANIFEST.yaml").read_text(encoding="utf-8"))
    manifest["source_key"] = LIVE_KEY
    (target / "MANIFEST.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return target


#: The one address the SYNTHETIC seller-email page shows (reserved example domain).
SELLER_PAGE_ADDRESS = "verkauf@dealer.example"


def seller_email_page() -> str:
    """The eligible car's page (TEST-204, same facts and price) as the dealer shows it WITH its
    one e-mail address and a German seller text (``detail_seller_email.html``, re-labelled): the
    detail pipeline itself records a verified contact and the language (F1, wave D2)."""
    html = (DEALER_DE / "detail_seller_email.html").read_text(encoding="utf-8")
    return html.replace("TEST-224", ELIGIBLE_REF).replace("XXXSYNTH000000224", "XXXSYNTH000000204")


def show_seller_email(tmp_path: Path) -> None:
    """From the next fetch on, the live TEST-204 page shows the seller's e-mail address."""
    (tmp_path / LIVE_KEY / "detail_normal.html").write_text(seller_email_page(), encoding="utf-8")


def live_source_config() -> Any:
    return fixture_source_config(
        source_key=LIVE_KEY,
        mode=SourceMode.PUBLIC_HTML,
        display_name="SYNTHETIC live-mode dealer (in-memory pages; not a real source)",
    )


def with_settings(env: PipelineEnv, settings: Settings) -> PipelineEnv:
    """The same workspace and database with other settings (the runtime is shared, not closed)."""
    live = settings.model_copy(update={"source_network_enabled": True})
    ctx = dataclasses.replace(env.ctx, settings=live, owns_db=False, inquiry_runtime=None)
    return dataclasses.replace(env, ctx=ctx)


async def build_live_env(
    db_url: str,
    seed: Seed,
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    name: str = "Runtime",
    market: bool = True,
    page_overrides: Mapping[str, str] | None = None,
) -> PipelineEnv:
    """A workspace with an owner, the business config, the LIVE-mode synthetic source and a runtime
    whose network client is the in-memory page store (see the module docstring). ``market=False``
    leaves the MK comparables and the EUR/MKD rate out (a test records its own)."""
    ws = seed.workspace(f"{name} {uuid.uuid4().hex[:6]}")
    owner_id = seed.user()
    seed.membership(ws, owner_id, "owner")
    owner = user_actor(ws, owner_id)
    configured = settings or runtime_settings(db_url)
    ctx = await build_runtime(
        configured,
        application_name="suv-deals-runtime-test",
        clock=SystemClock(),
        options=RuntimeOptions(job_lease_seconds=120, heartbeat_seconds=40),
        fixture_dirs=[live_fixture_dir(tmp_path, page_overrides)],
        taxonomy=synthetic_taxonomy(),
    )
    # The in-memory pages ARE the "network" of this runtime: no crawler client, no DNS.
    ctx.settings = configured.model_copy(update={"source_network_enabled": True})
    if ctx.inquiry_runtime is not None:
        ctx.inquiry_runtime.settings = ctx.settings
    ctx.network_client = ctx.fixture_client
    ctx.resolver = public_resolver
    env = PipelineEnv(
        ctx=ctx,
        seed=seed,
        workspace_id=ws,
        owner=owner,
        system=system(ws),
        source_id=uuid.UUID(int=0),
        profiles={},
    )

    async def sleep(seconds: float) -> None:
        del seconds
        env.refill_budgets()

    ctx.sleep = sleep
    result = await run(
        ctx,
        owner,
        lambda c: config_repo.record_config_revision(
            c, owner, load_business_config(REPO / "config"), "synthetic runtime configuration", None
        ),
    )
    env.profiles = {p.profile_key.value: p.id for p in result.profiles}
    actor = env.system

    async def sync(conn: Conn) -> UUID:
        await sources_repo.sync_sources_from_yaml(conn, actor, [live_source_config()])
        return (await sources_repo.get_source_by_key(conn, actor, LIVE_KEY)).id

    env.source_id = await run(ctx, actor, sync)
    if market:
        await seed_live_market(env)
    return env


# =============================================================================================
# Real-lineage market evidence
# =============================================================================================


def live_comparable(amount: str, *, mileage: str) -> MarketObservation:
    """A SYNTHETIC real-lineage MK asking price for the dealer's "Example Trail" (invented)."""
    return MarketObservation(
        id=uuid.uuid4(),
        source_key="synthetic_mk_market",
        url=f"https://mk-classifieds.example/ad/{uuid.uuid4().hex[:10]}",
        observed_at=now_utc() - timedelta(days=2),
        evidence_kind=EvidenceKind.ASKING_PRICE,
        amount=Money.of(amount, "EUR"),
        vehicle=VehicleSpec(
            make="Example",
            model="Trail",
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            first_registration=PartialDate(value="2011", precision="year"),
            mileage_km=Decimal(mileage),
            engine_displacement_cm3=1995,
            power_kw=103,
        ),
        local_registration_status="locally_registered",
        is_fixture=False,
    )


async def seed_live_market(env: PipelineEnv) -> None:
    actor = env.system

    async def insert(conn: Conn) -> None:
        for index, amount in enumerate(("8600", "8900", "9100", "9400")):
            await market_repo.insert_market_observation(
                conn,
                actor,
                live_comparable(amount, mileage=str(170000 + 10000 * index)),
                confidence=Confidence.MEDIUM,
            )
        rate = FxRate(
            base="EUR",
            quote="MKD",
            rate=Decimal("61.5"),
            rate_date=date.today(),
            retrieved_at=now_utc() - timedelta(hours=1),
            provider="SYNTHETIC reference rate (test)",
            purpose=FxPurpose.REFERENCE,
        )
        await valuation_repo.upsert_fx_rate(conn, actor, rate, is_fixture=False)

    await run(env.ctx, actor, insert)


# =============================================================================================
# Inquiry prerequisites: controls, standing authorization, sender, desktop worker
# =============================================================================================


@dataclass
class Sender:
    binding_id: UUID
    provider: EmailProviderKind
    worker: WorkerIdentity | None = None
    box: SecretBox | None = None


async def prepare_sender(
    env: PipelineEnv,
    *,
    provider: EmailProviderKind = EmailProviderKind.OUTLOOK_LOCAL,
    automatic: bool = True,
    desktop_worker: bool = True,
    box: SecretBox | None = None,
    canary: bool = True,
) -> Sender:
    """Controls row, the standing authorization, a VERIFIED sender binding (and, for
    ``outlook_local``, an issued desktop worker with a fresh heartbeat) and, unless
    ``canary=False``, the owner's completed activation canary for that binding version (F3/OPS-04:
    nothing is reserved without it)."""
    actor = env.system
    account = GMAIL_ACCOUNT if provider == EmailProviderKind.GMAIL_API else SENDER_ACCOUNT

    async def go(conn: Conn) -> UUID:
        controls = await inquiries_repo.ensure_controls(conn, actor)
        await inquiries_repo.record_authorization(
            conn, actor, AUTHORIZATION, reason="standing authorization v1"
        )
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=provider,
            account_id=account,
            from_address=SENDER_ADDRESS,
            display_name="Synthetic Owner",
            reason="owner-authorized sending identity (synthetic)",
        )
        if provider != EmailProviderKind.OUTLOOK_LOCAL:
            assert box is not None
            binding = await sender_bindings_repo.store_secret(
                conn,
                actor,
                binding.id,
                grant=OAuthRefreshGrant(
                    client_id="synthetic-client.apps.example.invalid",
                    client_secret=SecretStr("synthetic-client-secret"),
                    refresh_token=SecretStr("synthetic-refresh-token"),
                ),
                box=box,
                expected_version=binding.version,
                reason="sealed OAuth grant (synthetic)",
            )
        binding = await sender_bindings_repo.record_verification(
            conn,
            actor,
            binding.id,
            expected_version=binding.version,
            verified=True,
            alias_verified=True,
            health="healthy",
            reason="technical verification passed (synthetic)",
        )
        if automatic:
            await inquiries_repo.set_mode(
                conn, actor, expected_version=controls.version, mode="automatic", reason="sender verified"
            )
        return binding.id

    binding_id = await run(env.ctx, actor, go)
    sender = Sender(binding_id=binding_id, provider=provider, box=box)
    if provider == EmailProviderKind.OUTLOOK_LOCAL and desktop_worker:
        async with unit_of_work(env.ctx.db, actor) as conn:
            issued = await mail_workers_repo.issue_mail_worker(
                conn, actor, sender_binding_id=binding_id, label="Synthetic desktop worker"
            )
        async with env.ctx.db.transaction() as conn:
            sender.worker = await mail_workers_repo.resolve_worker(
                conn, issued.credential.token.get_secret_value()
            )
        await heartbeat(env, sender.worker)
    if canary:
        await complete_activation_canary(env.ctx.db, env.workspace_id, binding_id)
    return sender


async def heartbeat(env: PipelineEnv, worker: WorkerIdentity) -> None:
    from suv_deals.api.schemas import MailWorkerHeartbeatRequest  # noqa: PLC0415 - wire schema

    now = now_utc()
    body = MailWorkerHeartbeatRequest.model_validate(
        {
            "schema_version": "1.0",
            "heartbeat": {
                "mailbox_binding_id": str(worker.mailbox_binding_id),
                "worker_id": "synthetic-desktop-1",
                "at": now.isoformat(),
                "outlook_running": True,
                "mailbox_connected": True,
            },
            "last_successful_reconciliation_at": now.isoformat(),
            "mailbox_last_sync_at": now.isoformat(),
        }
    )
    async with unit_of_work(env.ctx.db, worker.actor("req-beat")) as conn:
        await mail_workers_repo.record_heartbeat(conn, worker, body, request_id="req-beat")


def age_heartbeat(env: PipelineEnv, worker: WorkerIdentity, *, minutes: int) -> None:
    """TEST ARRANGEMENT: the desktop worker went silent ``minutes`` ago."""
    env.seed.conn.execute(
        "update ops.mail_worker_checkpoints set heartbeat_at = now() - make_interval(mins => %s)"
        " where workspace_id = %s and mailbox_binding_id = %s",
        (minutes, env.workspace_id, worker.mailbox_binding_id),
    )


async def pull_kill_switch(env: PipelineEnv) -> None:
    """``seller_inquiries_pause``: the workspace kill switch (stops every untransmitted send)."""
    actor = env.system

    async def go(conn: Conn) -> None:
        controls = await inquiries_repo.get_controls(conn, actor)
        assert controls is not None
        await inquiries_repo.pause(
            conn, actor, expected_version=controls.version, reason="synthetic kill switch"
        )

    await run(env.ctx, actor, go)


# =============================================================================================
# Pipeline: ingest, seller evidence, worker
# =============================================================================================


async def ingest(env: PipelineEnv) -> dict[str, dict[str, Any]]:
    """Scheduler tick + discovery/detail jobs only (valuation and inquiry jobs wait)."""
    from suv_deals.crawling.scheduler import run_scheduler_tick  # noqa: PLC0415

    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])
    await work(env, JobType.DISCOVERY, JobType.DETAIL)
    rows = env.rows(
        "select id, source_listing_id, canonical_url, eligibility_state, is_fixture, current_revision_id,"
        " availability from app.listings where workspace_id = %s",
        env.workspace_id,
    )
    return {r["source_listing_id"]: r for r in rows}


async def work(env: PipelineEnv, *job_types: JobType) -> list[JobRunReport]:
    """Run the worker of this runtime until nothing (of ``job_types``) is due."""
    worker = Worker(
        env.ctx,
        workspace_ids=[env.workspace_id],
        worker_id=f"worker-{uuid.uuid4().hex[:8]}",
        job_types=job_types or None,
    )
    return await worker.run_until_idle()


def language_de() -> LanguageDecision:
    fragment = AdTextFragment(
        field="description",
        text=DE_TEXT,
        seller_written=True,
        provenance=FieldProvenance(method=_css_method(), confidence=Confidence.HIGH, observed_at=now_utc()),
    )
    return resolve_inquiry_language(None, [fragment], "de", "DE")


def _css_method() -> Any:
    from suv_deals.domain.enums import ExtractionMethod  # noqa: PLC0415

    return ExtractionMethod.CSS


@dataclass(frozen=True)
class SellerLink:
    seller_entity_id: UUID
    contact_id: UUID
    address: str


async def link_seller(
    env: PipelineEnv, listing: Mapping[str, Any], *, source_key: str = LIVE_KEY
) -> SellerLink:
    """TEST ARRANGEMENT of seller evidence the page itself does not show (the live TEST-204 page
    has a contact form only; the detail pipeline records it as ``unavailable``, wave D2): the
    dealer's marketplace alias and an e-mail address "printed on the advertisement". A verified
    contact outranks the unavailable one until the page is fetched again (a re-fetch records the
    page's own evidence, which supersedes it); `show_seller_email` makes the page show one."""
    actor = env.system
    reference = str(listing["source_listing_id"])
    url = str(listing["canonical_url"])
    revision_number = env.scalar(
        "select revision_number from app.listing_revisions where id = %s", listing["current_revision_id"]
    )
    address = f"verkauf-{uuid.uuid4().hex[:8]}@dealer.example"
    alias = SellerAlias.model_validate(
        {
            "alias_kind": "marketplace_seller_id",
            "source_key": source_key,
            "reference": f"dealer-{reference}-{uuid.uuid4().hex[:6]}",
            "evidence_kind": "listing_seller_block",
            "observed_at": now_utc() - timedelta(minutes=5),
        }
    )
    async with unit_of_work(env.ctx.db, actor) as conn:
        linked = await sellers_repo.link_seller(
            conn, actor, SellerIdentity(seller_type=SellerType.DEALER, aliases=(alias,))
        )
    at = now_utc() - timedelta(minutes=1)
    evidence = RecipientEvidence(
        kind=RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
        address=address,
        listing_id=listing["id"],
        listing_incarnation_id=listing["id"],
        listing_revision_id=listing["current_revision_id"],
        listing_revision_number=int(revision_number),
        source_key=source_key,
        listing_reference=reference,
        listing_url=url,
        evidence_url=url,
        extraction_location=ExtractionLocation.LISTING_CONTACT_BLOCK,
        extraction_excerpt=f"E-Mail: {address}",
        distinct_addresses_on_page=1,
        seller=SellerIdentity(
            seller_entity_id=linked.entity_id, seller_type=SellerType.DEALER, aliases=(alias,)
        ),
        observed_at=at - timedelta(minutes=1),
        verified_at=at,
    )
    decision = verify_recipient(evidence, now=now_utc())
    async with unit_of_work(env.ctx.db, actor) as conn:
        contact = await sellers_repo.record_contact(
            conn,
            actor,
            evidence=evidence,
            decision=decision,
            language=language_de(),
            seller_entity_id=linked.entity_id,
        )
    return SellerLink(seller_entity_id=linked.entity_id, contact_id=contact.id, address=address)


async def eligible_live_listing(env: PipelineEnv) -> dict[str, Any]:
    """Ingest the live pages and return the eligible REAL-lineage listing."""
    listings = await ingest(env)
    listing = listings[ELIGIBLE_REF]
    assert listing["eligibility_state"] == "eligible_primary", listing
    assert listing["is_fixture"] is False  # real lineage (non-fixture source mode at ingest)
    return listing


# =============================================================================================
# Inspection
# =============================================================================================


def jobs_of(env: PipelineEnv, job_type: JobType) -> list[dict[str, Any]]:
    return env.rows(
        "select id, state, attempts, dedup_key, last_error_code, blocker_code, available_at,"
        " result_reference, listing_id, payload from ops.jobs where workspace_id = %s and job_type = %s"
        " order by created_at, id",
        env.workspace_id,
        job_type.value,
    )


def inquiries_of(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select id, state, readiness, readiness_reasons, qualification_listing_id, sender_binding_id"
        " from app.seller_inquiries where workspace_id = %s order by created_at, id",
        env.workspace_id,
    )


def attempts_of(env: PipelineEnv, inquiry_id: UUID) -> list[dict[str, Any]]:
    return env.rows(
        "select id, attempt_id, attempt_number, provider, outcome, rfc_message_id, error_code,"
        " reconciled_outcome from ops.email_delivery_attempts where workspace_id = %s and inquiry_id = %s"
        " order by attempt_number",
        env.workspace_id,
        inquiry_id,
    )


def debits_of(env: PipelineEnv) -> int:
    return int(
        env.scalar(
            "select count(*) from ops.inquiry_quota_ledger where workspace_id = %s and released_at is null",
            env.workspace_id,
        )
    )


def outbox_of(env: PipelineEnv, event_type: str) -> list[dict[str, Any]]:
    return env.rows(
        "select event_id, event_type, state, blocker_code, last_error_code, is_fixture, payload,"
        " attempts, dedup_key from ops.outbox where workspace_id = %s and event_type = %s"
        " order by event_created_at, id",
        env.workspace_id,
        event_type,
    )


def no_approval_state(env: PipelineEnv) -> None:
    """Spec 37.1: no inquiry and no job ever waits for a per-message approval."""
    states = {r["state"] for r in inquiries_of(env)}
    assert not {s for s in states if "approv" in s}, states
    codes = env.rows(
        "select coalesce(last_error_code, '') as code, coalesce(blocker_code, '') as blocker,"
        " coalesce(result_reference::text, '') as result from ops.jobs where workspace_id = %s",
        env.workspace_id,
    )
    for row in codes:
        text = " ".join(str(v) for v in row.values()).lower()
        assert "approval_required" not in text and "awaiting_approval" not in text, row


# =============================================================================================
# The simulated desktop worker and the seller's reply
# =============================================================================================


async def pending_intents(env: PipelineEnv, worker: WorkerIdentity) -> send_intents_repo.SendIntentBatch:
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        return await send_intents_repo.list_pending(conn, worker, request_id=WORKER_REQ)


async def worker_sends(env: PipelineEnv, worker: WorkerIdentity, intent: OutlookSendIntent) -> None:
    """The desktop worker claims the intent (fresh revalidation) and reports Sent Items."""
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        claim = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="claim-1",
            worker_id="synthetic-desktop-1",
            request_id=WORKER_REQ,
        )
    assert claim.proceed, claim
    report = OutlookSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=intent.mailbox_binding_id,
        worker_id="synthetic-desktop-1",
        state=OutlookSubmissionState.SENT_ITEMS_CONFIRMED,
        account_smtp_address_used=SENDER_ADDRESS,
        observed_internet_message_id=intent.rfc_message_id,
        sent_items_present=True,
        reported_at=now_utc(),
        sent_at=now_utc(),
    )
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        await send_intents_repo.report(conn, worker, report=report, request_id=WORKER_REQ)


def binding_version(env: PipelineEnv, inquiry_id: UUID) -> int:
    value = env.scalar(
        "select max(binding_version) from ops.mail_binding_sync where workspace_id = %s and inquiry_id = %s",
        env.workspace_id,
        inquiry_id,
    )
    return int(value or 1)


async def seller_replies(
    env: PipelineEnv,
    worker: WorkerIdentity,
    inquiry_id: UUID,
    *,
    in_reply_to: str,
    from_address: str,
    body: str,
    options: ReplyIngestOptions | None = None,
) -> ReplyIngestOutcome:
    """The desktop worker uploads one correlated seller reply (``replies_repo.ingest_reply``)."""
    when = now_utc() - timedelta(minutes=1)
    upload = ReplyIngestRequest.model_validate(
        {
            "schema_version": "1.0",
            "inquiry_id": str(inquiry_id),
            "binding_version": binding_version(env, inquiry_id),
            "mailbox_binding_id": str(worker.mailbox_binding_id),
            "source_message": {
                "internet_message_id": f"<{uuid.uuid4().hex}@dealer.example>",
                "provider_message_id": None,
                "outlook_entry_id": None,
                "outlook_store_id": None,
                "received_at": when.isoformat(),
            },
            "headers": {"from": from_address, "in_reply_to": in_reply_to, "references": []},
            "subject": "AW: Anfrage zu Ihrem Fahrzeug (synthetic)",
            "sanitized_body_text": body,
            "detected_language": "de",
            "observed_at": (when + timedelta(seconds=3)).isoformat(),
        }
    )
    async with unit_of_work(env.ctx.db, worker.actor("req-ingest")) as conn:
        return await replies_repo.ingest_reply(
            conn,
            worker,
            upload,
            f"idem-{uuid.uuid4().hex}",
            now_utc(),
            request_id="req-ingest",
            options=options or ReplyIngestOptions(dashboard_base_url=DASHBOARD, enqueue_processing_job=True),
        )


# =============================================================================================
# Notification routes and the Slack double
# =============================================================================================


async def approve_slack_category(env: PipelineEnv, category: bindings_repo.EventCategory) -> UUID:
    """Owner approves, verifies and enables a private Slack destination for ONE category."""
    owner = env.owner

    async def go(conn: Conn) -> UUID:
        binding = await bindings_repo.create_binding(
            conn,
            owner,
            provider="slack",
            label=f"SYNTHETIC private {category} channel",
            external_workspace_id="T0SYNTHETIC1",
            external_channel_id=SLACK_CHANNEL,
        )
        binding = await bindings_repo.approve_binding(
            conn, owner, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        )
        binding = await bindings_repo.mark_binding_verified(
            conn, owner, binding.id, expected_version=binding.row_version
        )
        preference = await bindings_repo.upsert_preferences(
            conn, owner, binding.id, event_categories=[category]
        )
        preference = await bindings_repo.approve_preferences(
            conn, owner, preference.id, approval_reference="SYNTHETIC approval", expected_version=1
        )
        await bindings_repo.set_preferences_enabled(
            conn, owner, preference.id, True, expected_version=preference.row_version
        )
        await bindings_repo.set_binding_enabled(
            conn, owner, binding.id, True, expected_version=binding.row_version
        )
        return binding.id

    return await run(env.ctx, owner, go)


class SlackApi:
    """In-process stand-in for chat.postMessage / conversations.history (SYNTHETIC answers)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.posts: list[dict[str, Any]] = []
        self.other: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # SafeHttpClient connects to the pinned (fake-resolved) address; the Host header names Slack.
        if request.headers.get("host") == "slack.com" and request.url.path.endswith("/chat.postMessage"):
            assert request.headers["authorization"].startswith("Bearer xoxb-")
            self.posts.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100", "channel": SLACK_CHANNEL})
        self.other.append(request)
        return httpx.Response(404, json={"ok": False, "error": "unexpected_synthetic_route"})

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


# =============================================================================================
# Gmail double (httpx.MockTransport): token endpoint, profile, send, search
# =============================================================================================

GMAIL_ROOT = f"https://gmail.googleapis.com/gmail/v1/users/{GMAIL_ACCOUNT}"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


class GmailApi:
    """SYNTHETIC Gmail: ``send_mode`` = ``ok`` | ``timeout``; ``search_finds`` decides the
    reconciliation search by Message-ID; the first ``token_failures`` token requests answer 503
    (nothing reaches Gmail: a proven pre-submission failure)."""

    def __init__(self, *, send_mode: str = "ok", search_finds: bool = False, token_failures: int = 0) -> None:
        self.send_mode = send_mode
        self.search_finds = search_finds
        self.token_failures = token_failures
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if url.startswith(GOOGLE_TOKEN) and self.token_failures > 0:
            self.token_failures -= 1
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        if url.startswith(GOOGLE_TOKEN):
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
                    "token_type": "Bearer",
                },
            )
        assert request.headers["authorization"] == "Bearer synthetic-access-token"
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": GMAIL_ACCOUNT, "messagesTotal": 1})
        if path.endswith("/messages/send"):
            self.sent.append(json.loads(request.content))
            if self.send_mode == "timeout":
                raise httpx.ReadTimeout("synthetic timeout after the request was written", request=request)
            return httpx.Response(
                200, json={"id": "synthetic-msg-1", "threadId": "synthetic-thread-1", "labelIds": ["SENT"]}
            )
        if path.endswith("/messages") and request.method == "GET":
            if self.search_finds:
                return httpx.Response(
                    200, json={"messages": [{"id": "synthetic-msg-1", "threadId": "synthetic-thread-1"}]}
                )
            return httpx.Response(200, json={"resultSizeEstimate": 0})
        if "/messages/synthetic-msg-1" in path:
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-msg-1",
                    "threadId": "synthetic-thread-1",
                    "labelIds": ["SENT"],
                    "payload": {"headers": [{"name": "Message-ID", "value": self.message_id()}]},
                },
            )
        return httpx.Response(404, json={"error": {"code": 404, "message": "synthetic"}})

    def message_id(self) -> str:
        """The Message-ID header of the raw message the provider received (base64url)."""
        if not self.sent:
            return ""
        raw = base64.urlsafe_b64decode(self.sent[-1]["raw"] + "==").decode("utf-8", "replace")
        for line in raw.splitlines():
            if line.lower().startswith("message-id:"):
                return line.split(":", 1)[1].strip()
        return ""


def gmail_settings(db_url: str, binding_id: UUID, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "seller_email_provider": "gmail_api",
        "seller_email_account_id": GMAIL_ACCOUNT,
        "seller_email_from": SENDER_ADDRESS,
        "seller_email_oauth_secret_reference": sender_bindings_repo.secret_reference_for(binding_id),
        "mcp_event_subscription_secret_encryption_key": SECRET_KEY,
    }
    values.update(overrides)
    return runtime_settings(db_url, **values)


def attach_gmail(env: PipelineEnv, api: GmailApi, box: SecretBox) -> PipelineEnv:
    """Inject the Gmail double into this runtime's inquiry runtime (no real client is built)."""
    from suv_deals.workers.inquiry_handlers import inquiry_runtime  # noqa: PLC0415

    rt = inquiry_runtime(env.ctx)
    rt.http_client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    rt.secret_box = box
    env.ctx.add_closer(rt.http_client.aclose)
    return env


def box() -> SecretBox:
    return SecretBox({1: base64.b64decode(SECRET_KEY)}, 1)


def age_uncertainty(env: PipelineEnv, inquiry_id: UUID, *, minutes: int) -> None:
    """TEST ARRANGEMENT (superuser, append-only guards bypassed for one statement): the inquiry's
    uncertain attempt ended ``minutes`` ago."""
    with env.seed.conn.transaction():
        env.seed.conn.execute("set local session_replication_role = replica")
        env.seed.conn.execute(
            "update ops.email_delivery_attempts"
            " set send_intent_committed_at = now() - make_interval(mins => %s),"
            " finished_at = now() - make_interval(mins => %s)"
            " where workspace_id = %s and inquiry_id = %s and outcome = 'uncertain'",
            (minutes + 1, minutes, env.workspace_id, inquiry_id),
        )


def expire_job_wait(env: PipelineEnv, job_id: UUID) -> None:
    """TEST ARRANGEMENT: the job's release delay passed."""
    env.seed.conn.execute(
        "update ops.jobs set available_at = now() - interval '1 second' where workspace_id = %s and id = %s",
        (env.workspace_id, job_id),
    )


def results(reports: Sequence[JobRunReport], job_type: JobType) -> list[JobRunReport]:
    return [r for r in reports if r.job_type == job_type]


Predicate = Callable[[dict[str, Any]], bool]

__all__ = [
    "DASHBOARD",
    "ELIGIBLE_REF",
    "LIVE_KEY",
    "SELLER_PAGE_ADDRESS",
    "SENDER_ADDRESS",
    "GmailApi",
    "SellerLink",
    "Sender",
    "SlackApi",
    "age_heartbeat",
    "age_uncertainty",
    "approve_slack_category",
    "attach_gmail",
    "attempts_of",
    "box",
    "build_live_env",
    "debits_of",
    "eligible_live_listing",
    "expire_job_wait",
    "gmail_settings",
    "heartbeat",
    "ingest",
    "inquiries_of",
    "jobs_of",
    "link_seller",
    "no_approval_state",
    "outbox_of",
    "pending_intents",
    "prepare_sender",
    "pull_kill_switch",
    "results",
    "runtime_settings",
    "seller_email_page",
    "seller_replies",
    "show_seller_email",
    "slack_signal_settings",
    "with_settings",
    "work",
    "worker_sends",
]
