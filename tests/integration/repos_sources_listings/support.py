"""Synthetic builders for the WP7b1 repository tests. No real vehicle, dealer or person appears."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import member

from suv_deals.adapters.base import (
    DiscoveryPage,
    FetchOutcome,
    ParsedListing,
    SearchObservation,
    SearchRequest,
)
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    AccessState,
    Availability,
    BodyType,
    Completeness,
    Confidence,
    CoverageMode,
    ExtractionMethod,
    Fuel,
    JobType,
    OdometerClaim,
    PriceBasis,
    PriceType,
    Role,
    SourceMode,
    TechnicalStatus,
    TermsDecision,
    TermsStatus,
    Tristate,
)
from suv_deals.domain.identity import card_hash
from suv_deals.domain.listings import (
    Documentation,
    LocationInfo,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.sources import SourceConfig
from suv_deals.persistence import config_repo, jobs, listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.jobs import ClaimedJob
from suv_deals.persistence.listings_repo import DetailSnapshotRef, IngestDetailResult, IngestReport
from suv_deals.persistence.sources_repo import CrawlRunRecord
from suv_deals.persistence.transactions import unit_of_work

REPO_ROOT = Path(__file__).resolve().parents[3]
HOST = "dealer-a.synthetic.example"
T0 = datetime(2026, 10, 6, 8, 0, 0, tzinfo=UTC)
PARSER = "fixture_dealer@1.0.0"


def system(workspace_id: UUID) -> ActorContext:
    return ActorContext.system(workspace_id, request_id=f"test-{uuid.uuid4().hex[:12]}")


def business_config() -> BusinessConfig:
    return load_business_config(REPO_ROOT / "config")


def source_config(source_key: str, **overrides: Any) -> SourceConfig:
    """An enabled, fixture-tested synthetic acquisition source (passes the activation gate)."""
    values: dict[str, Any] = {
        "source_key": source_key,
        "display_name": "Synthetic fixture dealer",
        "country": "DE",
        "role": "acquisition",
        "mode": SourceMode.FIXTURE,
        "adapter": "fixture_adapter",
        "adapter_version": "fixture@1.0.0",
        "enabled": True,
        "technical_status": TechnicalStatus.FIXTURE_TESTED,
        "terms_status": TermsStatus.NO_RESTRICTION_FOUND,
        "terms_url": "https://dealer-a.synthetic.example/terms",
        "terms_reviewed_at": T0,
        "terms_decision": TermsDecision.PROCEED_ACKNOWLEDGED,
        "terms_decision_actor": "synthetic owner",
        "allowed_hosts": (HOST,),
        "allowed_search_paths": ("^/search",),
        "allowed_detail_paths": ("^/vehicles/",),
    }
    values.update(overrides)
    return SourceConfig(**values)


@dataclass(frozen=True)
class Env:
    workspace_id: UUID
    owner: ActorContext
    owner_user_id: UUID
    system: ActorContext
    source_id: UUID
    source_key: str
    profiles: dict[str, UUID]
    config_revision_id: UUID
    schedule_id: UUID


async def build_env(db: Database, seed: Seed, name: str) -> Env:
    ws = seed.workspace(name)
    user = seed.user()
    seed.membership(ws, user, "owner")
    owner = member(ws, Role.OWNER, principal_id=user)
    actor = system(ws)
    async with unit_of_work(db, owner) as conn:
        result = await config_repo.record_config_revision(
            conn, owner, business_config(), "initial synthetic configuration", None
        )
    key = unique("src").lower()
    async with unit_of_work(db, actor) as conn:
        await sources_repo.sync_sources_from_yaml(conn, actor, [source_config(key)])
        source = await sources_repo.get_source_by_key(conn, actor, key)
        profiles = {p.profile_key.value: p.id for p in result.profiles}
        schedule = await sources_repo.ensure_schedule(
            conn, actor, source.id, profiles["primary"], coverage_mode=CoverageMode.ROLLING_PAGES
        )
    return Env(
        workspace_id=ws,
        owner=owner,
        owner_user_id=user,
        system=actor,
        source_id=source.id,
        source_key=key,
        profiles=profiles,
        config_revision_id=result.revision.id,
        schedule_id=schedule.id,
    )


def card(
    slid: str,
    *,
    price_minor: int = 275000,
    mileage: str = "187500",
    position: int = 0,
    url: str | None = None,
    title: str = "Volkswagen Tiguan 2.0 TDI (synthetic)",
) -> SearchObservation:
    link = url or f"https://{HOST}/vehicles/{slid}"
    material = {
        "source_listing_id": slid,
        "canonical_url": link,
        "title": title,
        "price_minor": str(price_minor),
        "currency": "EUR",
        "mileage_km": mileage,
        "source_modified_at": None,
    }
    digest, normalized = card_hash(material)
    return SearchObservation(
        source_listing_id=slid,
        canonical_url=link,
        title=title,
        card_price_minor=price_minor,
        card_currency="EUR",
        card_mileage_km=Decimal(mileage),
        position=position,
        card_hash=digest,
        card_hash_material=normalized,
    )


def page(
    source_key: str,
    cards: list[SearchObservation],
    *,
    page_number: int = 1,
    fetched_at: datetime = T0,
    completeness: Completeness = Completeness.COMPLETE,
) -> DiscoveryPage:
    url = f"https://{HOST}/search?page={page_number}"
    return DiscoveryPage(
        request=SearchRequest(source_key=source_key, profile_key="primary", url=url, page_number=page_number),
        observations=tuple(cards),
        has_more=False,
        completeness=completeness,
        access_state=AccessState.OK,
        fetched_at=fetched_at,
        fetch=FetchOutcome(
            requested_url=url,
            success=True,
            access_state=AccessState.OK,
            http_status=200,
            bytes=1000,
            fetched_at=fetched_at,
        ),
    )


def vehicle(
    slid: str,
    *,
    price_minor: int = 275000,
    mileage: str = "187500",
    make: str = "Volkswagen",
    model: str = "Tiguan",
    first_registration: str = "2012-05",
    fuel: Fuel = Fuel.DIESEL,
    availability: Availability = Availability.AVAILABLE,
    observed_at: datetime = T0,
    vin: str | None = None,
) -> NormalizedListing:
    """An otherwise eligible synthetic VW Tiguan (EUR 2,750 gross, 187,500 km, DE)."""
    return NormalizedListing(
        source_key="fixture_dealer",
        source_listing_id=slid,
        canonical_url=f"https://{HOST}/vehicles/{slid}",
        observed_at=observed_at,
        title=f"{make} {model} (synthetic)",
        location=LocationInfo(country="DE"),
        availability=availability,
        vehicle=VehicleSpec(
            make=make,
            model=model,
            body_type=BodyType.SUV,
            fuel=fuel,
            first_registration=PartialDate(value=first_registration, precision="month"),
            mileage_km=Decimal(mileage),
            mileage_claim=OdometerClaim.SELLER_REPORTED,
        ),
        price=PriceInfo(
            amount_minor=price_minor,
            currency="EUR",
            basis=PriceBasis.GROSS,
            type=PriceType.FULL_VEHICLE_ASKING,
            required_seller_fees_known=Tristate.NO,
            raw_text=f"{price_minor // 100} EUR",
        ),
        documentation=Documentation(vin=vin),
        provenance={
            "price.amount_minor": FieldProvenance(
                method=ExtractionMethod.CSS,
                selector=".vehicle-price",
                raw_text=f"{price_minor // 100} EUR",
                confidence=Confidence.HIGH,
                observed_at=observed_at,
            ),
            "vehicle.mileage_km": FieldProvenance(
                method=ExtractionMethod.JSON_LD,
                raw_text=f"{mileage} km",
                confidence=Confidence.HIGH,
                observed_at=observed_at,
            ),
        },
        parser_version=PARSER,
    )


def parsed(listing: NormalizedListing) -> ParsedListing:
    return ParsedListing(page_type="detail", access_state=AccessState.OK, listing=listing)


async def start_run(db: Database, env: Env, *, partition_key: str = "default") -> CrawlRunRecord:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.start_crawl_run(
            conn,
            env.system,
            source_id=env.source_id,
            profile_id=env.profiles["primary"],
            partition_key=partition_key,
            coverage_mode=CoverageMode.ROLLING_PAGES,
            adapter_version="fixture@1.0.0",
            parser_version=PARSER,
        )


async def ingest(db: Database, env: Env, run: CrawlRunRecord, discovery: DiscoveryPage) -> IngestReport:
    async with unit_of_work(db, env.system) as conn:
        return await listings_repo.ingest_search_page(conn, env.system, run, discovery)


async def discover(db: Database, env: Env, slid: str, **card_kwargs: Any) -> tuple[UUID, CrawlRunRecord]:
    """Run one search page with one card; returns the new listing id (detail job generation 1)."""
    run = await start_run(db, env)
    report = await ingest(db, env, run, page(env.source_key, [card(slid, **card_kwargs)]))
    assert report.new_listings == 1 and len(report.detail_jobs) == 1
    return report.detail_jobs[0].listing_id, run


async def claim_detail(db: Database, env: Env, worker: str = "worker-test") -> ClaimedJob:
    job = await jobs.claim(db, env.workspace_id, worker, [JobType.DETAIL, JobType.RECHECK], lease_seconds=120)
    assert job is not None
    return job


async def refresh_and_claim(db: Database, env: Env, listing_id: UUID) -> ClaimedJob:
    async with unit_of_work(db, env.system) as conn:
        ref = await listings_repo.request_detail_refresh(
            conn, env.system, listing_id, reason="synthetic refresh", job_type=JobType.DETAIL
        )
    assert ref is not None
    return await claim_detail(db, env)


async def run_detail(
    db: Database,
    env: Env,
    job: ClaimedJob,
    listing_id: UUID,
    listing: NormalizedListing | ParsedListing,
    **kwargs: Any,
) -> IngestDetailResult:
    document = listing if isinstance(listing, ParsedListing) else parsed(listing)
    async with unit_of_work(db, env.system) as conn:
        return await listings_repo.ingest_detail(
            conn, env.system, job, listing_id, document, DetailSnapshotRef(parser_version=PARSER), **kwargs
        )


def later(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)
