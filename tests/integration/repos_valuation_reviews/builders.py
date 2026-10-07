"""SYNTHETIC builders for the market/valuation/review repository tests.

Arrangement uses the superuser ``Seed`` connection; every operation under test runs through
``Database(db_url, set_role="suv_backend")``. No real listing, tax rate, quote or person appears
here: every name, amount and rule is visibly synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.persistence_core.support import member, system

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.comparables import ComparableTarget, MarketObservation, select_comparables
from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    CostAssumption,
    CostLine,
    CostProfile,
    ProceedsEstimate,
    PurchaseInput,
    compute_scenarios,
    tax_cost_lines,
)
from suv_deals.domain.enums import (
    Co2Cycle,
    CostCategory,
    CostLineStatus,
    Drive,
    EligibilityState,
    EvidenceKind,
    Fuel,
    FxPurpose,
    Gearbox,
    ProfileKey,
    ReviewOutcome,
    Role,
    TaxRuleStatus,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import BusinessConfig, ContributionThreshold, SearchProfile
from suv_deals.domain.reviews import SubmitRequest
from suv_deals.domain.tax_engine import (
    IMPORT_CATEGORIES,
    Classification,
    OriginProof,
    OriginProofStatus,
    ReviewRecord,
    RuleSet,
    RuleSource,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    load_rule_set_file,
)
from suv_deals.domain.valuation import ScreeningInput, Valuation, assemble_valuation
from suv_deals.persistence import market_repo, reviews_repo, valuation_repo
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.views.reviews import ClaimResult

REPO = Path(__file__).resolve().parents[3]
TAX_FIXTURE = REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json"
DASHBOARD = "https://dashboard.synthetic.example"
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)

__all__ = ["member", "system"]


def owner(ws: UUID, principal_id: UUID | None = None) -> ActorContext:
    return member(ws, Role.OWNER, principal_id=principal_id)


def reviewer(ws: UUID, principal_id: UUID | None = None, *, kind: str = "mcp_client") -> ActorContext:
    return member(ws, Role.REVIEWER, principal_id=principal_id, kind=kind)  # type: ignore[arg-type]


def viewer(ws: UUID) -> ActorContext:
    return member(ws, Role.VIEWER)


async def run[T](db: Database, actor: ActorContext, fn: Callable[[Conn], Awaitable[T]]) -> T:
    async with unit_of_work(db, actor) as conn:
        return await fn(conn)


@dataclass(frozen=True)
class RealWorld:
    """A non-fixture workspace: public_html source, primary profile, listing, revision 1."""

    workspace_id: UUID
    config_revision_id: UUID
    source_id: UUID
    source_key: str
    listing_id: UUID
    revision_id: UUID


def add_revision(seed: Seed, ws: UUID, listing_id: UUID, number: int, **cols: Any) -> UUID:
    """Insert revision ``number`` (with its allocated detail observation) and promote it."""
    _, gen, obs = seed.detail_observation(ws, listing_id, promoted=True)
    rev = seed.revision(ws, listing_id, number, detail_generation=gen, observation_id=obs, **cols)
    seed.promote(ws, listing_id, rev, gen, obs)
    return rev


def make_listing(seed: Seed, ws: UUID, source_id: UUID, *, eligible: bool = True) -> tuple[UUID, UUID]:
    listing = seed.listing(ws, source_id)
    rev = add_revision(
        seed,
        ws,
        listing,
        1,
        make="Example",
        model="Trail",
        seller_country="DE",
        normalized={"title": "SYNTHETIC Example Trail 2.0 TDI", "synthetic": True},
    )
    if eligible:
        make_eligible(seed, ws, listing)
    return listing, rev


def make_eligible(seed: Seed, ws: UUID, listing_id: UUID, *, eur: str = "2750.00") -> None:
    seed.conn.execute(
        "update app.listings set eligibility_state = 'eligible_primary', eligibility_profile = 'primary',"
        " screening = %s, screening_version = 'screening@test', screened_at = now(),"
        " availability = 'available', last_detail_success_at = now(), last_seen_at = now(),"
        " row_version = row_version + 1"
        " where workspace_id = %s and id = %s",
        (Jsonb({"eur_amount": eur, "synthetic": True}), ws, listing_id),
    )


def set_eligibility(seed: Seed, ws: UUID, listing_id: UUID, state: str, profile: str | None = None) -> None:
    """The listing's committed screening (what ingest records when it screens a revision)."""
    seed.conn.execute(
        "update app.listings set eligibility_state = %s, eligibility_profile = %s,"
        " screening = coalesce(screening, %s), screening_version = 'screening@test',"
        " screened_at = now(), row_version = row_version + 1 where workspace_id = %s and id = %s",
        (state, profile, Jsonb({"synthetic": True}), ws, listing_id),
    )


def real_world(seed: Seed, name: str) -> RealWorld:
    ws = seed.workspace(name)
    cfg = seed.config_revision(ws)
    key = unique("synthetic_src").lower()
    src = seed.source(ws, source_key=key, mode="public_html", adapter="synthetic_adapter")
    seed.profile(ws, "primary", cfg)
    listing, rev = make_listing(seed, ws, src)
    return RealWorld(ws, cfg, src, key, listing, rev)


def primary_profile(**overrides: Any) -> SearchProfile:
    values: dict[str, Any] = {
        "key": ProfileKey.PRIMARY,
        "label": "Synthetic primary profile",
        "queue_label": "Primary queue",
        "enabled": True,
        "min_price_eur": Decimal("2500.00"),
        "max_price_eur": Decimal("3000.00"),
    }
    values.update(overrides)
    return SearchProfile(**values)


def screening(state: EligibilityState = EligibilityState.ELIGIBLE_PRIMARY, **kw: Any) -> ScreeningResult:
    values: dict[str, Any] = {
        "state": state,
        "profile": ProfileKey.PRIMARY if state == EligibilityState.ELIGIBLE_PRIMARY else None,
        "queue_label": "Primary queue" if state == EligibilityState.ELIGIBLE_PRIMARY else None,
        "eur_amount": Decimal("2750.00"),
        "payable_amount": Money.of("2750.00", "EUR"),
        "fx_rate_used": None,
        "reasons": (),
        "missing_facts": (),
    }
    values.update(kw)
    return ScreeningResult(**values)


def business_config() -> BusinessConfig:
    return BusinessConfig(profiles={ProfileKey.PRIMARY: primary_profile()})


# --------------------------------------------------------------------------------------------
# Market evidence
# --------------------------------------------------------------------------------------------


def observation(source_key: str, amount: str | None = "9000.00", **kw: Any) -> MarketObservation:
    vehicle = VehicleSpec(
        make="Example",
        model="Trail",
        fuel=Fuel.DIESEL,
        gearbox=Gearbox.AUTOMATIC,
        drive=Drive.AWD,
        first_registration=PartialDate(value="2012", precision="year"),  # type: ignore[arg-type]
        mileage_km=Decimal("180000"),
    )
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source_key": source_key,
        "url": f"https://mk-classifieds.example/ad/{uuid.uuid4().hex[:10]}",
        "observed_at": T0 - timedelta(days=3),
        "evidence_kind": EvidenceKind.ASKING_PRICE,
        "amount": None if amount is None else Money.of(amount, "EUR"),
        "vehicle": vehicle,
        "local_registration_status": "locally_registered",
    }
    values.update(kw)
    return MarketObservation(**values)


def target(listing_id: UUID, *, is_fixture: bool = False) -> ComparableTarget:
    return ComparableTarget(
        listing_id=listing_id,
        make="Example",
        model="Trail",
        fuel=Fuel.DIESEL,
        gearbox=Gearbox.AUTOMATIC,
        drive=Drive.AWD,
        year=2012,
        year_source="first_registration",
        mileage_km=Decimal("185000"),
        is_fixture=is_fixture,
    )


# --------------------------------------------------------------------------------------------
# Tax, FX, costs and a complete valuation
# --------------------------------------------------------------------------------------------


def synthetic_rule_set(*, rule_set_id: str | None = None, version: str = "synthetic-1") -> RuleSet:
    """The synthetic engine fixture as a NON-fixture draft (still clearly synthetic content)."""
    fixture = load_rule_set_file(TAX_FIXTURE)
    source = RuleSource(
        url="https://tax-authority.example/SYNTHETIC.pdf",
        title="SYNTHETIC source",
        retrieved_at=T0,
        sha256="0" * 64,
    )
    return RuleSet.model_validate(
        {
            **fixture.model_dump(),
            "rule_set_id": rule_set_id or unique("SYNTHETIC-rules"),
            "version": version,
            "status": "draft",
            "is_fixture": False,
            "sha256": None,
            "sources": [source],
        }
    )


def review_record(rule_set: RuleSet) -> ReviewRecord:
    return ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=T0,
        content_sha256=compute_rule_set_sha256(rule_set),
        scope="SYNTHETIC golden cases",
    )


async def active_rule_set(db: Database, actor: ActorContext) -> valuation_repo.StoredRuleSet:
    """Store a draft and drive it through review, approval and activation (owner actions)."""
    draft = synthetic_rule_set()

    async def go(conn: Conn) -> valuation_repo.StoredRuleSet:
        await valuation_repo.store_rule_set(conn, actor, draft)
        rs, version = draft.rule_set_id, draft.version
        await valuation_repo.transition_tax_rule_set(conn, actor, rs, version, TaxRuleStatus.UNDER_REVIEW)
        await valuation_repo.transition_tax_rule_set(
            conn,
            actor,
            rs,
            version,
            TaxRuleStatus.APPROVED,
            approved_by="SYNTHETIC owner",
            review_record=review_record(draft),
        )
        return await valuation_repo.transition_tax_rule_set(conn, actor, rs, version, TaxRuleStatus.ACTIVE)

    return await run(db, actor, go)


def tax_inputs() -> TaxInputs:
    return TaxInputs(
        declaration_date=date(2026, 11, 2),
        classification=Classification(
            tariff_code="8703 23",
            evidence_ids=("e",),
            approval_status="approved",
            approved_by="SYNTHETIC owner",
        ),
        origin_proof=OriginProof(proof_type="none", acceptance_status=OriginProofStatus.NOT_AVAILABLE),
        customs_value=Money.of("100000.00", "MKD"),
        customs_value_basis="SYNTHETIC",
        co2_g_km=Decimal("120"),
        co2_cycle=Co2Cycle.WLTP,
        vehicle_age_years=Decimal("12"),
        engine_displacement_cm3=Decimal("1995"),
    )


def mkd_rate(at: datetime | None = None) -> FxRate:
    """SYNTHETIC EUR->MKD reference rate (dated ``at`` when given)."""
    return FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.5"),
        rate_date=date(2026, 10, 5) if at is None else at.date(),
        retrieved_at=datetime(2026, 10, 5, 16, tzinfo=UTC) if at is None else at,
        provider="SYNTHETIC owner-approved MKD source",
        purpose=FxPurpose.REFERENCE,
    )


def cost_profile(key: str | None = None) -> CostProfile:
    return CostProfile(
        profile_key=key or unique("synthetic_profile").lower(),
        version=1,
        basis="SYNTHETIC test assumptions",
        assumptions=(
            CostAssumption(
                category=CostCategory.RISK_RESERVE,
                label="SYNTHETIC reserve",
                status=CostLineStatus.ESTIMATED,
                base=Decimal("600.00"),
                note="SYNTHETIC assumption",
            ),
        ),
    )


def _eur(value: str) -> Money:
    return Money.of(value, "EUR")


def other_lines(transport: CostLine | None = None) -> list[CostLine]:
    """SYNTHETIC cost lines; ``transport`` replaces the estimated transport line (e.g. a quote)."""
    estimated = (
        (CostCategory.TRANSPORT, "700.00"),
        (CostCategory.CUSTOMS_BROKER, "250.00"),
        (CostCategory.REPAIRS, "800.00"),
    )
    lines = [
        CostLine(
            category=c,
            label=f"SYNTHETIC {c.value}",
            status=CostLineStatus.ESTIMATED,
            currency="EUR",
            base=_eur(v),
        )
        for c, v in estimated
        if transport is None or c != CostCategory.TRANSPORT
    ]
    if transport is not None:
        lines.append(transport)
    lines += [
        CostLine(
            category=c,
            label=f"SYNTHETIC {c.value}",
            status=CostLineStatus.ESTIMATED,
            currency="EUR",
            base=_eur(v),
            assumption_approved=True,
        )
        for c, v in ((CostCategory.RISK_RESERVE, "600.00"), (CostCategory.SELLING_COSTS, "150.00"))
    ]
    covered = {ln.category for ln in lines} | IMPORT_CATEGORIES
    lines += [
        CostLine(
            category=c,
            label="SYNTHETIC n/a",
            status=CostLineStatus.NOT_APPLICABLE,
            currency="EUR",
            reason="SYNTHETIC test",
        )
        for c in sorted(REQUIRED_CATEGORIES - covered)
    ]
    return lines


@dataclass(frozen=True)
class ValuationBundle:
    valuation: Valuation
    refs: valuation_repo.ValuationRefs
    inputs: valuation_repo.ValuationInputs
    rule_set: valuation_repo.StoredRuleSet
    fx: valuation_repo.StoredFxRate
    profile: valuation_repo.StoredCostProfile
    comparable: market_repo.StoredComparableSet


async def complete_valuation(
    db: Database,
    world: RealWorld,
    *,
    revision_id: UUID | None = None,
    transport: valuation_repo.StoredCostEvidence | None = None,
    as_of: datetime = T0,
) -> ValuationBundle:
    """A complete, non-fixture ESTIMATED valuation whose every dependency is stored.

    ``transport``: stored cost evidence used as the transport line (cited by the valuation)."""
    ws = world.workspace_id
    own = owner(ws)
    sys_actor = system(ws)
    rule = await active_rule_set(db, own)
    revision = revision_id or world.revision_id

    async def deps(conn: Conn) -> tuple[valuation_repo.StoredFxRate, valuation_repo.StoredCostProfile, Any]:
        fx, _ = await valuation_repo.upsert_fx_rate(conn, sys_actor, mkd_rate(None if as_of == T0 else as_of))
        profile = await valuation_repo.store_cost_profile(conn, sys_actor, cost_profile())
        obs = [observation(world.source_key, a) for a in ("8800.00", "9000.00", "9200.00", "9400.00")]
        for item in obs:
            await market_repo.insert_market_observation(
                conn, sys_actor, item, confidence="medium", source_id=world.source_id
            )
        result = select_comparables(target(world.listing_id), obs, business_config(), as_of=T0)
        stored = await market_repo.persist_comparable_set(
            conn, sys_actor, result, listing_id=world.listing_id, target_revision_id=revision
        )
        return fx, profile, stored

    fx, profile, comparable = await run(db, sys_actor, deps)
    calc = calculate(rule.rule_set, tax_inputs(), T0)
    assert calc.production_ready
    purchase = PurchaseInput(status=CostLineStatus.ESTIMATED, amount=Money.of("2800.00", "EUR"))
    proceeds = ProceedsEstimate(
        status=CostLineStatus.ESTIMATED,
        currency="EUR",
        base=Money.of("8000.00", "EUR"),
        basis="owner_estimate",
    )
    transport_line = None if transport is None else valuation_repo.cost_line_from_evidence(transport)
    lines = other_lines(transport_line) + list(tax_cost_lines(calc))
    scenarios = compute_scenarios(purchase, lines, proceeds, [fx.rate], ContributionThreshold(), as_of=as_of)
    valuation = assemble_valuation(
        listing_revision_id=str(revision),
        screening=ScreeningInput(
            eligibility=EligibilityState.ELIGIBLE_PRIMARY,
            profile_key=ProfileKey.PRIMARY,
            eur_payable=Money.of("2800.00", "EUR"),
        ),
        comparable=comparable.reference(),
        tax=calc,
        scenarios=scenarios,
        fx_rates=[fx.rate],
        cost_profile=profile.profile.reference(),
        config_revision_id=str(world.config_revision_id),
        as_of=as_of,
    )
    refs = valuation_repo.ValuationRefs(
        listing_id=world.listing_id,
        config_revision_id=world.config_revision_id,
        comparable_set_id=comparable.id,
        tax_rule_set_row_id=rule.row_for("passenger_car"),
        cost_profile_id=profile.id,
        fx_rate_ids=(fx.id,),
        cost_evidence_ids=() if transport is None else (transport.id,),
    )
    inputs = valuation_repo.ValuationInputs(
        cost_lines=tuple(lines),
        purchase=purchase,
        proceeds=proceeds,
        import_line_sources=scenarios.import_line_sources,
    )
    return ValuationBundle(valuation, refs, inputs, rule, fx, profile, comparable)


def incomplete_valuation(
    world: RealWorld, *, revision_id: UUID | None = None, config: UUID | None = None
) -> Valuation:
    """A non-fixture valuation without tax rules or scenarios (state not_started)."""
    return assemble_valuation(
        listing_revision_id=str(revision_id or world.revision_id),
        screening=ScreeningInput(
            eligibility=EligibilityState.ELIGIBLE_PRIMARY, profile_key=ProfileKey.PRIMARY
        ),
        comparable=None,
        tax=None,
        scenarios=None,
        fx_rates=[],
        cost_profile=None,
        config_revision_id=str(config or world.config_revision_id),
        as_of=T0,
    )


async def store_simple_valuation(
    db: Database, world: RealWorld, *, revision_id: UUID | None = None, config: UUID | None = None
) -> valuation_repo.StoredValuation:
    valuation = incomplete_valuation(world, revision_id=revision_id, config=config)
    refs = valuation_repo.ValuationRefs(
        listing_id=world.listing_id, config_revision_id=config or world.config_revision_id
    )
    return await run(
        db,
        system(world.workspace_id),
        lambda c: valuation_repo.persist_valuation(
            c, system(world.workspace_id), valuation, refs, valuation_repo.ValuationInputs()
        ),
    )


# --------------------------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------------------------

CURSOR_SECRET = b"SYNTHETIC-cursor-secret-0123456789abcdef"


def idem_key(prefix: str = "key") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


async def open_case(
    db: Database,
    world: RealWorld,
    *,
    listing_id: UUID | None = None,
    revision_id: UUID | None = None,
    valuation_id: UUID | None = None,
    screening_result: ScreeningResult | None = None,
    profile: SearchProfile | None = None,
) -> reviews_repo.CaseUpsertResult:
    """``upsert_review_case`` as the system worker for a screened revision."""
    actor = system(world.workspace_id)
    return await run(
        db,
        actor,
        lambda c: reviews_repo.upsert_review_case(
            c,
            actor,
            listing_id or world.listing_id,
            revision_id or world.revision_id,
            screening_result or screening(),
            valuation_id,
            profile or primary_profile(),
            dashboard_base_url=DASHBOARD,
        ),
    )


async def claim_case(db: Database, actor: ActorContext, case_id: UUID, version: int) -> ClaimResult:
    return await run(db, actor, lambda c: reviews_repo.claim(c, actor, case_id, version, idem_key("claim")))


def submit_request(
    claim: ClaimResult,
    *,
    outcome: ReviewOutcome = ReviewOutcome.WATCH,
    idempotency_key: str | None = None,
    **overrides: Any,
) -> SubmitRequest:
    assert claim.claim_token is not None
    values: dict[str, Any] = {
        "case_id": claim.case_id,
        "claim_token": claim.claim_token,
        "expected_version": claim.case_version,
        "listing_revision": claim.listing_revision,
        "valuation_id": claim.valuation_id,
        "outcome": outcome,
        "reason_codes": ("SYNTHETIC_REASON",),
        "summary": "SYNTHETIC rationale: asking price inside the band; costs still estimated.",
        "missing_information": ("SYNTHETIC service history",)
        if outcome == ReviewOutcome.NEEDS_INFORMATION
        else (),
        "idempotency_key": idempotency_key or idem_key("submit"),
    }
    values.update(overrides)
    return SubmitRequest(**values)


def decision_count(seed: Seed, case_id: UUID) -> int:
    return int(seed.scalar("select count(*) from app.review_decisions where case_id = %s", (case_id,)))


def outbox_rows(seed: Seed, ws: UUID, event_type: str = "review.pending") -> list[dict[str, Any]]:
    cur = seed.conn.execute(
        "select event_id, dedup_key, state, is_fixture, aggregate_version, payload, destination_binding_id,"
        " event_version from ops.outbox where workspace_id = %s and event_type = %s"
        " order by event_created_at, id",
        (ws, event_type),
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def case_row(seed: Seed, case_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute("select * from app.review_cases where id = %s", (case_id,))
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def expire_claim(seed: Seed, case_id: UUID) -> None:
    """Simulate time passing: the claim expired in database time."""
    seed.conn.execute(
        "update app.review_cases set claimed_at = clock_timestamp() - interval '10 minutes',"
        " claim_expires_at = clock_timestamp() - interval '1 second' where id = %s",
        (case_id,),
    )


_ = sha
