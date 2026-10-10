"""Builders for the bounded-inquiry persistence tests (spec 37.1-37.5, 37.8, 37.10; work package B1a).

Everything is SYNTHETIC: reserved example domains (``*.example`` / ``*.invalid``), synthetic
sources (real lineage unless a test asks for fixture lineage), invented listing references, seller
ids and Message-IDs. Nothing is ever sent or
fetched: the tests drive the persistence layer through ``Database(set_role="suv_backend")`` (RLS
and least-privilege grants apply) and arrange rows through the superuser ``Seed`` connection.

The domain evidence (screening, comparables, cost scenarios, recipient verification, language
resolution) is computed by the REAL domain code for a synthetic VW Tiguan, exactly as the
valuation pipeline would, and handed to ``inquiries_repo.read_readiness_inputs``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.v11_db.support import SYNTHETIC_HOST, confirmed_cluster, enabled_source

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.comparables import (
    ComparableTarget,
    MarketObservation,
    proceeds_from_comparables,
    select_comparables,
)
from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    CostLine,
    ProceedsEstimate,
    PurchaseInput,
    compute_scenarios,
)
from suv_deals.domain.enums import (
    Availability,
    BodyType,
    Confidence,
    CostLineStatus,
    Drive,
    EmailProviderKind,
    EvidenceKind,
    ExtractionMethod,
    Fuel,
    Gearbox,
    OdometerClaim,
    PriceBasis,
    PriceType,
    Role,
    Scope,
    SellerType,
    Tristate,
)
from suv_deals.domain.filters import screen
from suv_deals.domain.inquiries import (
    ComparableEvidence,
    CostEvidence,
    InquiryReadinessDecision,
    VehicleIdentification,
    evaluate_inquiry_readiness,
    load_seller_inquiry_authorization,
)
from suv_deals.domain.language import AdTextFragment, LanguageDecision, resolve_inquiry_language
from suv_deals.domain.listings import LocationInfo, NormalizedListing, PartialDate, PriceInfo, VehicleSpec
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import ContributionThreshold, load_business_config
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.seller_contacts import (
    ExtractionLocation,
    RecipientEvidence,
    RecipientEvidenceKind,
    SellerAlias,
    SellerIdentity,
    verify_recipient,
)
from suv_deals.domain.taxonomy import default_taxonomy
from suv_deals.integrations.email_providers.base import (
    ProviderReceipt,
    SendAccepted,
    SendDefiniteFailure,
    SendFailureReason,
    SendUncertain,
    UncertainReason,
)
from suv_deals.persistence import inquiries_repo, sellers_repo, sender_bindings_repo
from suv_deals.persistence.database import Database, fetch_one
from suv_deals.persistence.inquiries_repo import (
    AttemptLease,
    DispatchResult,
    InquiryRecord,
    OutcomeResult,
    ReadinessEvaluation,
    ReadinessSnapshot,
)
from suv_deals.persistence.sellers_repo import LinkedSeller, SellerContactRecord
from suv_deals.persistence.transactions import unit_of_work

REPO = Path(__file__).resolve().parents[3]
CONFIG = load_business_config(REPO / "config")
TAXONOMY = default_taxonomy()
AUTHORIZATION = load_seller_inquiry_authorization()

SENDER_ADDRESS = "inquiries@synthetic-mail.example"
SENDER_NAME = "Synthetic Sender"
SENDER_ACCOUNT = "synthetic-outlook-account"
DE_TEXT = (
    "Verkaufe unseren gepflegten Geländewagen. Fahrzeug ist unfallfrei, TÜV neu, Scheckheft gepflegt. "
    "Nichtraucherfahrzeug mit Anhängerkupplung und Sitzheizung."
)
#: Too short / not seller-written enough to resolve a language: English is never a fallback.
UNRESOLVABLE_TEXT = "SUV 4x4"


# =============================================================================================
# Actors
# =============================================================================================


def system(workspace_id: UUID) -> ActorContext:
    return ActorContext.system(workspace_id, "req-b1a-system")


def owner(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset(Scope),
        request_id="req-b1a-owner",
        display_name="Synthetic owner",
    )


# =============================================================================================
# Domain evidence for a synthetic Tiguan (real domain code)
# =============================================================================================


def now_utc() -> datetime:
    return datetime.now(UTC)


def normalized(source_key: str, reference: str, url: str, *, observed_at: datetime) -> NormalizedListing:
    return NormalizedListing(
        source_key=source_key,
        source_listing_id=reference,
        canonical_url=url,
        observed_at=observed_at,
        location=LocationInfo(country="DE"),
        availability=Availability.AVAILABLE,
        seller_type=SellerType.DEALER,
        vehicle=VehicleSpec(
            make="Volkswagen",
            model="Tiguan",
            body_type=BodyType.SUV,
            mileage_km=Decimal("187500"),
            mileage_claim=OdometerClaim.SELLER_REPORTED,
            first_registration=PartialDate(value="2011-05", precision="month"),
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            power_kw=103,
            engine_displacement_cm3=1968,
        ),
        price=PriceInfo(
            amount_minor=275000,
            currency="EUR",
            basis=PriceBasis.GROSS,
            type=PriceType.FULL_VEHICLE_ASKING,
            required_seller_fees_known=Tristate.NO,
        ),
        parser_version="fixture@1.0.0",
    )


def _observation(n: int, amount: str, at: datetime) -> MarketObservation:
    return MarketObservation(
        id=UUID(int=900 + n),
        source_key="fixture_mk",
        observed_at=at - timedelta(days=3),
        evidence_kind=EvidenceKind.ASKING_PRICE,
        amount=Money.of(amount, "EUR"),
        vehicle=VehicleSpec(
            make="Volkswagen",
            model="Tiguan",
            generation="5N",
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            power_kw=103,
            engine_displacement_cm3=1968,
            first_registration=PartialDate(value="2011", precision="year"),
            mileage_km=Decimal("180000"),
        ),
    )


def evaluation(listing: NormalizedListing, *, at: datetime | None = None) -> ReadinessEvaluation:
    """Screening, identification, MK comparables and cost scenarios as the pipeline computes them.

    The documentation is left UNKNOWN on purpose: a missing CoC is a question the inquiry asks,
    never a reason to hold it (spec 37.2).
    """
    now = at or now_utc()
    screening = screen(listing, CONFIG, [], now.date(), TAXONOMY)
    target = ComparableTarget.from_listing(listing).model_copy(update={"generation": "5N"})
    comparables = select_comparables(
        target, [_observation(i, a, now) for i, a in enumerate(("8500", "9000", "9500"))], CONFIG, now
    )
    proceeds = ProceedsEstimate(**proceeds_from_comparables(comparables, None).as_proceeds_estimate_kwargs())
    lines = [
        CostLine(
            category=category,
            label=f"SYNTHETIC {category.value}",
            status=CostLineStatus.UNKNOWN,
            currency="EUR",
        )
        for category in sorted(REQUIRED_CATEGORIES)
    ]
    scenarios = compute_scenarios(
        PurchaseInput(status=CostLineStatus.ESTIMATED, amount=Money.of("2750", "EUR")),
        lines,
        proceeds,
        [],
        ContributionThreshold(),
        as_of=now,
    )
    return ReadinessEvaluation(
        screening=screening,
        vehicle=VehicleIdentification.from_screening(screening, listing),
        comparables=ComparableEvidence.from_comparable_set(comparables),
        costs=CostEvidence.from_scenario_set(scenarios),
        documentation=listing.documentation,
        co2=listing.co2,
    )


def language_from(text: str) -> LanguageDecision:
    fragment = AdTextFragment(
        field="description",
        text=text,
        seller_written=True,
        provenance=FieldProvenance(
            method=ExtractionMethod.CSS, confidence=Confidence.HIGH, observed_at=now_utc()
        ),
    )
    return resolve_inquiry_language(None, [fragment], "de", "DE")


LANGUAGE_DE = language_from(DE_TEXT)


# =============================================================================================
# World: one workspace with controls, authorization and a verified Outlook sender binding
# =============================================================================================


@dataclass(frozen=True)
class Vehicle:
    """One listing (own synthetic source) with its linked seller and current contact evidence."""

    source_id: UUID
    source_key: str
    listing_id: UUID
    revision_id: UUID
    reference: str
    url: str
    seller_entity_id: UUID
    contact_id: UUID | None
    address: str | None
    listing: NormalizedListing = field(repr=False)


@dataclass(frozen=True)
class World:
    workspace_id: UUID
    seed: Seed = field(repr=False)
    sender_binding_id: UUID
    vehicle: Vehicle

    @property
    def listing_id(self) -> UUID:
        return self.vehicle.listing_id

    @property
    def seller_entity_id(self) -> UUID:
        return self.vehicle.seller_entity_id

    def with_vehicle(self, vehicle: Vehicle) -> World:
        return replace(self, vehicle=vehicle)


def seed_listing(
    seed: Seed, workspace_id: UUID, *, price_minor: int = 275000, fixture: bool = False
) -> tuple[UUID, str, UUID, UUID, str, str]:
    """A promoted listing on its own enabled synthetic source, freshly observed (no network).

    Real lineage by default (``app.listings.is_fixture = false``); ``fixture=True`` for the explicit
    fixture-lineage refusal tests (plan, dispatch and the worker claim refuse such a listing)."""
    source_id, key = enabled_source(seed, workspace_id, fixture=fixture)
    reference = unique("SYN").upper().replace("_", "-")
    url = f"https://{SYNTHETIC_HOST}/vehicles/{reference}"
    listing = seed.listing(
        workspace_id,
        source_id,
        source_listing_id=reference,
        canonical_url=url,
        availability="available",
        eligibility_state="eligible_primary",
        eligibility_profile="primary",
        screening={"synthetic": True},
        screening_version="screening@synthetic",
        screened_at=now_utc(),
    )
    _, gen, obs = seed.detail_observation(workspace_id, listing, promoted=True)
    revision = seed.revision(
        workspace_id,
        listing,
        1,
        detail_generation=gen,
        observation_id=obs,
        semantic_hash=sha(unique("semantic")),
        asking_minor=price_minor,
    )
    seed.promote(workspace_id, listing, revision, gen, obs)
    seed.conn.execute(
        "update app.listings set last_detail_success_at = now() where workspace_id = %s and id = %s",
        (workspace_id, listing),
    )
    return source_id, key, listing, revision, reference, url


def alias(
    kind: str, reference: str, source_key: str | None, evidence: str = "listing_seller_block"
) -> SellerAlias:
    return SellerAlias.model_validate(
        {
            "alias_kind": kind,
            "source_key": source_key,
            "reference": reference,
            "evidence_kind": evidence,
            "observed_at": now_utc() - timedelta(minutes=5),
        }
    )


async def link(db: Database, workspace_id: UUID, aliases: Sequence[SellerAlias]) -> LinkedSeller:
    identity = SellerIdentity(seller_type=SellerType.DEALER, aliases=tuple(aliases))
    async with unit_of_work(db, system(workspace_id)) as conn:
        return await sellers_repo.link_seller(conn, system(workspace_id), identity)


def recipient_evidence(
    vehicle: Vehicle,
    seller_entity_id: UUID,
    *,
    address: str | None,
    aliases: Sequence[SellerAlias],
    kind: RecipientEvidenceKind = RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
    verified_at: datetime | None = None,
) -> RecipientEvidence:
    at = verified_at or now_utc() - timedelta(minutes=1)
    return RecipientEvidence(
        kind=kind,
        address=address,
        listing_id=vehicle.listing_id,
        listing_incarnation_id=vehicle.listing_id,
        listing_revision_id=vehicle.revision_id,
        listing_revision_number=1,
        source_key=vehicle.source_key,
        listing_reference=vehicle.reference,
        listing_url=vehicle.url,
        evidence_url=vehicle.url,
        extraction_location=ExtractionLocation.LISTING_CONTACT_BLOCK,
        extraction_excerpt=f"E-Mail: {address}" if address else "Kontakt nur per Formular",
        distinct_addresses_on_page=1 if address else 0,
        seller=SellerIdentity(
            seller_entity_id=seller_entity_id, seller_type=SellerType.DEALER, aliases=tuple(aliases)
        ),
        observed_at=at - timedelta(minutes=1),
        verified_at=at,
    )


async def record_contact(
    db: Database,
    workspace_id: UUID,
    vehicle: Vehicle,
    seller_entity_id: UUID,
    *,
    address: str | None,
    aliases: Sequence[SellerAlias],
    language: LanguageDecision | None = LANGUAGE_DE,
    kind: RecipientEvidenceKind = RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
) -> SellerContactRecord:
    evidence = recipient_evidence(vehicle, seller_entity_id, address=address, aliases=aliases, kind=kind)
    decision = verify_recipient(evidence, now=now_utc())
    async with unit_of_work(db, system(workspace_id)) as conn:
        return await sellers_repo.record_contact(
            conn,
            system(workspace_id),
            evidence=evidence,
            decision=decision,
            language=language,
            seller_entity_id=seller_entity_id,
        )


def seller_address() -> str:
    return f"{unique('verkauf').replace('_', '-')}@{SYNTHETIC_HOST}"


async def add_vehicle(
    db: Database,
    seed: Seed,
    workspace_id: UUID,
    *,
    aliases: Sequence[SellerAlias] | None = None,
    address: str | None = "",
    language: LanguageDecision | None = LANGUAGE_DE,
    kind: RecipientEvidenceKind = RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
    price_minor: int = 275000,
    fixture: bool = False,
) -> Vehicle:
    """A new listing with a linked seller (own marketplace alias unless given) and its contact."""
    source_id, key, listing_id, revision_id, reference, url = seed_listing(
        seed, workspace_id, price_minor=price_minor, fixture=fixture
    )
    own = (
        list(aliases) if aliases is not None else [alias("marketplace_seller_id", f"dealer-{reference}", key)]
    )
    linked = await link(db, workspace_id, own)
    resolved = seller_address() if address == "" else address
    vehicle = Vehicle(
        source_id=source_id,
        source_key=key,
        listing_id=listing_id,
        revision_id=revision_id,
        reference=reference,
        url=url,
        seller_entity_id=linked.entity_id,
        contact_id=None,
        address=resolved,
        listing=normalized(key, reference, url, observed_at=now_utc() - timedelta(minutes=30)),
    )
    contact = await record_contact(
        db,
        workspace_id,
        vehicle,
        linked.entity_id,
        address=resolved,
        aliases=own,
        language=language,
        kind=kind,
    )
    return replace(vehicle, contact_id=contact.id)


async def build_world(
    db: Database,
    seed: Seed,
    name: str,
    *,
    provider: EmailProviderKind = EmailProviderKind.OUTLOOK_LOCAL,
    automatic: bool = True,
) -> World:
    ws = seed.workspace(name)
    seed.profile(ws, "primary")
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        controls = await inquiries_repo.ensure_controls(conn, actor)
        await inquiries_repo.record_authorization(
            conn, actor, AUTHORIZATION, reason="standing authorization v1"
        )
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=provider,
            account_id=SENDER_ACCOUNT,
            from_address=SENDER_ADDRESS,
            display_name=SENDER_NAME,
            reason="owner-authorized sending identity (synthetic)",
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
    vehicle = await add_vehicle(db, seed, ws)
    return World(workspace_id=ws, seed=seed, sender_binding_id=binding.id, vehicle=vehicle)


# =============================================================================================
# The inquiry flow through the repository
# =============================================================================================


async def readiness(
    db: Database, world: World, vehicle: Vehicle | None = None
) -> tuple[ReadinessSnapshot, InquiryReadinessDecision]:
    veh = vehicle or world.vehicle
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        snapshot = await inquiries_repo.read_readiness_inputs(
            conn,
            actor,
            listing_id=veh.listing_id,
            seller_entity_id=veh.seller_entity_id,
            evaluation=evaluation(veh.listing),
            sender_binding_id=world.sender_binding_id,
        )
    return snapshot, evaluate_inquiry_readiness(snapshot.inputs)


async def qualify(
    db: Database, world: World, vehicle: Vehicle | None = None
) -> tuple[InquiryRecord, InquiryReadinessDecision, ReadinessSnapshot]:
    """Read inputs, decide readiness, open the ONE inquiry record and record the decision."""
    snapshot, decision = await readiness(db, world, vehicle)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        record = await inquiries_repo.open_inquiry(
            conn, actor, snapshot.identity, qualification_listing_id=(vehicle or world.vehicle).listing_id
        )
        record = await inquiries_repo.record_readiness(conn, actor, record.id, decision)
    return record, decision, snapshot


async def reserve_existing(
    db: Database,
    world: World,
    record: InquiryRecord,
    decision: InquiryReadinessDecision,
    snapshot: ReadinessSnapshot,
) -> InquiryRecord:
    actor = system(world.workspace_id)
    prepared = inquiries_repo.prepare_binding(snapshot, decision, inquiry_id=record.id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.reserve(conn, actor, record.id, decision=decision, prepared=prepared)


async def reserve(db: Database, world: World, vehicle: Vehicle | None = None) -> InquiryRecord:
    record, decision, snapshot = await qualify(db, world, vehicle)
    return await reserve_existing(db, world, record, decision, snapshot)


async def queue(db: Database, world: World, inquiry_id: UUID) -> InquiryRecord:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.queue(conn, actor, inquiry_id)


async def reserve_and_queue(db: Database, world: World, vehicle: Vehicle | None = None) -> InquiryRecord:
    record = await reserve(db, world, vehicle)
    return await queue(db, world, record.id)


def lease(minutes: int = 5, owner_name: str = "b1a-dispatcher") -> AttemptLease:
    return AttemptLease(
        owner=owner_name, token=uuid.uuid4(), expires_at=now_utc() + timedelta(minutes=minutes)
    )


async def dispatch(
    db: Database,
    world: World,
    inquiry_id: UUID,
    *,
    attempt_lease: AttemptLease | None = None,
    approval_required: bool = False,
) -> DispatchResult:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.dispatch(
            conn,
            actor,
            inquiry_id,
            lease=attempt_lease or lease(),
            message_approval_required=approval_required,
        )


def accepted(result: DispatchResult, *, sent_items: bool = False) -> SendAccepted:
    assert result.attempt is not None and result.message is not None
    at = now_utc()
    return SendAccepted(
        provider=result.attempt.provider,
        inquiry_id=result.inquiry_id,
        attempt_id=result.attempt.attempt_id,
        rfc_message_id=result.message.rfc_message_id,
        raw_sha256=result.message.raw_sha256,
        receipt=ProviderReceipt(
            kind="outlook_sent_items" if sent_items else "gmail_message_resource", observed_at=at
        ),
        accepted_at=at,
    )


def uncertain(
    result: DispatchResult, reason: UncertainReason = UncertainReason.READ_TIMEOUT
) -> SendUncertain:
    assert result.attempt is not None and result.message is not None
    return SendUncertain(
        provider=result.attempt.provider,
        inquiry_id=result.inquiry_id,
        attempt_id=result.attempt.attempt_id,
        rfc_message_id=result.message.rfc_message_id,
        raw_sha256=result.message.raw_sha256,
        reason=reason,
    )


def refused_before_submit(result: DispatchResult) -> SendDefiniteFailure:
    assert result.attempt is not None and result.message is not None
    return SendDefiniteFailure(
        provider=result.attempt.provider,
        inquiry_id=result.inquiry_id,
        attempt_id=result.attempt.attempt_id,
        rfc_message_id=result.message.rfc_message_id,
        raw_sha256=result.message.raw_sha256,
        pre_submission=True,
        reason=SendFailureReason.CONNECTION_FAILED,
        retryable=True,
        proof="connection_refused_before_submit",
    )


async def report(
    db: Database,
    world: World,
    result: DispatchResult,
    outcome: SendAccepted | SendDefiniteFailure | SendUncertain,
    *,
    token: UUID | None = None,
) -> OutcomeResult:
    assert result.attempt is not None
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.record_outcome(
            conn,
            actor,
            attempt_id=result.attempt.attempt_id,
            lease_token=token or result.attempt.lease_token,
            outcome=outcome,
        )


async def send(
    db: Database, world: World, vehicle: Vehicle | None = None
) -> tuple[InquiryRecord, DispatchResult]:
    """Reserve, queue, dispatch and record the provider's acceptance (one complete send)."""
    record = await reserve_and_queue(db, world, vehicle)
    result = await dispatch(db, world, record.id)
    assert result.outcome == "proceed", result.decision
    await report(db, world, result, accepted(result))
    return record, result


async def inquiry(db: Database, world: World, inquiry_id: UUID) -> InquiryRecord:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.get_inquiry(conn, actor, inquiry_id)


async def scalar(db: Database, world: World, query: str, params: dict[str, Any]) -> Any:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        row = await fetch_one(conn, query, {"ws": world.workspace_id, **params})
    assert row is not None
    return next(iter(row.values()))


# =============================================================================================
# Arranged history (TEST ARRANGEMENT ONLY: superuser, triggers bypassed for one statement)
# =============================================================================================


def backdate(
    conn: psycopg.Connection,
    inquiry_id: UUID,
    *,
    to: datetime,
) -> None:
    """Move an inquiry's reservation, debit and send timestamps to ``to`` (elapsed time)."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update app.seller_inquiries set reserved_at = %(at)s,"
            " queued_at = case when queued_at is null then null else %(at)s end,"
            " send_attempted_at = case when send_attempted_at is null then null else %(at)s end,"
            " accepted_at = case when accepted_at is null then null else %(at)s end,"
            " created_at = least(created_at, %(at)s), state_changed_at = %(at)s where id = %(id)s",
            {"id": inquiry_id, "at": to},
        )
        conn.execute(
            "update ops.inquiry_quota_ledger set debited_at = %(at)s where inquiry_id = %(id)s",
            {"id": inquiry_id, "at": to},
        )
        conn.execute(
            "update ops.email_delivery_attempts set send_intent_committed_at = %(at)s,"
            " finished_at = case when finished_at is null then null else %(at)s end"
            " where inquiry_id = %(id)s",
            {"id": inquiry_id, "at": to},
        )


def confirm_cluster(seed: Seed, workspace_id: UUID, listings: list[UUID]) -> UUID:
    return confirmed_cluster(seed, workspace_id, listings)


def today() -> date:
    return now_utc().date()
