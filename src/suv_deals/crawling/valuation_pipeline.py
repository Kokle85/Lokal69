"""Valuation job handler: comparables, tax, costs, scenarios, ranking and the review case (spec 14-18, 22).

Serves the valuation jobs `listings_repo.ingest_detail` enqueues for eligible / needs-facts screenings
and the deduplicated recomputation jobs of reverse invalidation (`valuation_repo.mark_stale`,
`invalidate_dependents`). Every job values the listing's CURRENT revision; nothing here performs
network I/O.

1. **Read** (short transaction): the current revision and its committed screening, the source,
   the fixture lineage (the listing's frozen ``is_fixture`` from ingest; a source that is CURRENTLY
   ``mode: fixture`` also makes the result a fixture, fail-safe), the business configuration, MK comparable
   candidates of the same fixture lineage (`market_repo.list_candidate_comparables`, year window
   including the one permitted widening step), ONE latest reference FX observation per currency that
   is needed (comparable currencies, the purchase currency, the tax rule currency), and the ACTIVE tax
   rule sets of the jurisdiction.
2. **Compute** (pure domain code):

   - comparables: `select_comparables` (tight first, labelled widening, explicit exclusions; no
     adequate set -> ``insufficient_comparables`` and ``research_needed``; nothing is fabricated);
   - tax: only an ACTIVE, approved rule set is ever selected (`select_rule_set`); without one, every
     import category stays ``unknown`` (never invented, never zero) and the reason is recorded;
     inputs the listing does not establish (classification, origin proof, customs value, ...) stay
     missing, so a calculation is incomplete rather than guessed;
   - costs: `CostProfile.lines(target_scope=...)` scoped to the purchase country (a CH purchase gets
     its ``ch_purchase`` lines, others never do); the profile's import categories are replaced by the
     tax engine's lines; `compute_scenarios` with the asking price as an ESTIMATED purchase and the
     comparable-based proceeds;
   - `assemble_valuation`: state, unknowns, dependency fingerprint and expiry; fixtures stay fixtures.
3. **Commit** (one short transaction, the job row locked first, then the listing): when a newer
   revision was promoted while the calculation ran (`valuation_repo.lock_current_revision`), the job
   completes as a successful no-op (``skipped: revision_superseded``; that revision's own valuation
   job values it) and nothing is stored -- no extra open valuation row. Otherwise the comparable
   set, the valuation (`persist_valuation` with ``require_current_revision`` checks every
   reference against the fingerprint), the transparent ranking, the
   review case of every enabled profile (`reviews_repo.upsert_review_case`: a qualifying revision
   creates/updates the pending case and writes the ``review.pending`` outbox event in the SAME
   transaction -- blocked fixture events for fixture data; a revision that no longer qualifies
   supersedes the open case) and the guarded job completion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.comparables import (
    YEAR_WIDEN_STEP,
    ComparableSetResult,
    ComparableTarget,
    MarketObservation,
    proceeds_from_comparables,
    select_comparables,
)
from suv_deals.domain.costs import (
    CostProfile,
    CostScope,
    ProceedsEstimate,
    PurchaseInput,
    compute_scenarios,
    tax_cost_lines,
)
from suv_deals.domain.enums import (
    CostLineStatus,
    EligibilityState,
    FxPurpose,
    JobState,
    SourceMode,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.domain.ranking import (
    RankingFeatures,
    RankResult,
    completeness_from_listing,
    rank_candidate,
    risk_flags_from_listing,
)
from suv_deals.domain.tax_engine import (
    IMPORT_CATEGORIES,
    RuleSelection,
    TaxCalculation,
    TaxInputs,
    select_rule_set,
)
from suv_deals.domain.tax_engine import calculate as calculate_tax
from suv_deals.domain.valuation import FxDependency, ScreeningInput, Valuation, assemble_valuation
from suv_deals.errors import NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import log_context
from suv_deals.persistence import (
    config_repo,
    listings_repo,
    market_repo,
    reviews_repo,
    sources_repo,
    valuation_repo,
)
from suv_deals.persistence.config_repo import ConfigRevisionRecord
from suv_deals.persistence.database import Conn, db_now
from suv_deals.persistence.listings_repo import ListingRecord, RevisionRecord
from suv_deals.persistence.reviews_repo import CaseUpsertResult
from suv_deals.persistence.sources_repo import SourceRecord
from suv_deals.persistence.transactions import job_unit_of_work, retry_transient, unit_of_work
from suv_deals.persistence.valuation_repo import StoredCostProfile, StoredFxRate, StoredRuleSet
from suv_deals.workers.runtime import Disposition, JobExecution, JobOutcome, RuntimeContext, apply_disposition

logger = logging.getLogger(__name__)

PURCHASE_EVIDENCE_PREFIX: Final = "listing_revision:"
#: Mileage at which a vehicle is unambiguously used for the tax engine's new/used input.
USED_VEHICLE_MIN_KM: Final = Decimal(1000)
_FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})
_COMPARABLE_METRIC: Final = {
    "adequate": "sufficient",
    "small_sample": "sparse",
    "insufficient_comparables": "none",
}


class ValuationPayload(BaseModel):
    """Valuation and recomputation job payloads (unknown keys are ignored)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    listing_id: UUID | None = None
    revision_id: UUID | None = None
    reason: str | None = None


@dataclass(slots=True)
class ValuationReads:
    """Everything the read phase loaded for one listing."""

    listing: ListingRecord
    revision: RevisionRecord
    normalized: NormalizedListing
    screening: ScreeningResult
    source: SourceRecord
    is_fixture: bool
    config_record: ConfigRevisionRecord
    config: BusinessConfig
    as_of: datetime
    candidates: list[MarketObservation]
    reference_rates: dict[str, StoredFxRate]
    customs_rate: StoredFxRate | None
    rule_sets: list[StoredRuleSet]


@dataclass(frozen=True, slots=True)
class ValuationRun:
    """The committed result of one valuation job."""

    valuation_id: UUID | None
    state: ValuationState | None
    cases: tuple[CaseUpsertResult, ...]
    skipped: str | None = None


# --------------------------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------------------------


async def handle_valuation(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    job = execution.job
    actor = execution.actor
    payload = ValuationPayload.model_validate(job.payload)
    listing_id = job.listing_id or payload.listing_id
    if listing_id is None:
        raise ValidationFailed("a valuation job needs its listing")
    profile = ctx.cost_profile
    if profile is None:
        raise ValidationFailed("no cost profile is configured (config/cost_profiles)")
    reads, skipped = await _read(ctx, actor, listing_id)
    if reads is None:
        run = await _complete(ctx, execution, {"skipped": skipped or "nothing to value"})
        return JobOutcome(state=JobState.SUCCEEDED, details={"skipped": run.skipped})
    stored_profile = await ensure_cost_profile(ctx, actor, profile)
    with log_context(source_key=reads.source.source_key):
        run = await _value_and_commit(ctx, execution, reads, stored_profile)
    if run.skipped is not None:
        return JobOutcome(state=JobState.SUCCEEDED, details={"skipped": run.skipped})
    case_ids = [str(c.case_id) for c in run.cases if c.case_id is not None]
    logger.info(
        "valuation recorded",
        extra={"state": None if run.state is None else run.state.value, "cases": len(case_ids)},
    )
    return JobOutcome(
        state=JobState.SUCCEEDED,
        details={
            "valuation_id": None if run.valuation_id is None else str(run.valuation_id),
            "state": None if run.state is None else run.state.value,
            "case_ids": case_ids,
        },
    )


async def _complete(ctx: RuntimeContext, execution: JobExecution, result: dict[str, str]) -> ValuationRun:
    async def commit() -> None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            await apply_disposition(conn, execution.job, Disposition.complete(dict(result)))

    await retry_transient(commit)
    return ValuationRun(valuation_id=None, state=None, cases=(), skipped=result.get("skipped"))


# --------------------------------------------------------------------------------------------
# Read phase
# --------------------------------------------------------------------------------------------


async def _read(
    ctx: RuntimeContext, actor: ActorContext, listing_id: UUID
) -> tuple[ValuationReads | None, str | None]:
    options = ctx.options
    async with unit_of_work(ctx.db, actor) as conn:
        listing = await listings_repo.get_listing(conn, actor, listing_id)
        revision = await listings_repo.current_revision(conn, actor, listing_id)
        if revision is None:
            return None, "no_current_revision"
        screening = listing.screening_result()
        if screening is None:
            return None, "not_screened"
        if revision.quarantined or listing.quarantined:
            return None, "quarantined"
        normalized = revision.listing()
        source = await sources_repo.get_source_record(conn, actor, listing.source_id)
        # Lineage frozen at ingest; a source that is now fixture also taints new results (fail-safe).
        is_fixture = listing.is_fixture or source.mode == SourceMode.FIXTURE
        config_record, config = await config_repo.current_config(conn, actor)
        as_of = ensure_utc(await db_now(conn))
        target = ComparableTarget.from_listing(normalized, listing_id=listing.id, is_fixture=is_fixture)
        candidates = [
            c
            for c in await market_repo.list_candidate_comparables(
                conn,
                actor,
                target,
                as_of=as_of,
                max_age_days=config.comparable_max_age_days,
                year_window=config.comparable_year_window + YEAR_WIDEN_STEP,
            )
            if c.is_fixture == is_fixture  # never mix fixture and real evidence (spec 18)
        ]
        rule_sets = await valuation_repo.list_rule_sets(
            conn, actor, jurisdiction=options.tax_jurisdiction, statuses=[TaxRuleStatus.ACTIVE]
        )
        currencies = {c.amount.currency for c in candidates if c.amount is not None}
        if screening.payable_amount is not None:
            currencies.add(screening.payable_amount.currency)
        currencies.update(r.rule_set.currency for r in rule_sets if r.rule_set.currency)
        reference = {
            currency: rate
            for currency in sorted(currencies - {"EUR"})
            if (
                rate := await _latest_rate(
                    conn, actor, currency, FxPurpose.REFERENCE, as_of=as_of, fixtures=is_fixture
                )
            )
            is not None
        }
        tax_currency = next((r.rule_set.currency for r in rule_sets if r.rule_set.currency), None)
        customs = (
            await _latest_rate(conn, actor, tax_currency, FxPurpose.CUSTOMS, as_of=as_of, fixtures=is_fixture)
            if tax_currency and tax_currency != "EUR"
            else None
        )
    return (
        ValuationReads(
            listing=listing,
            revision=revision,
            normalized=normalized,
            screening=screening,
            source=source,
            is_fixture=is_fixture,
            config_record=config_record,
            config=config,
            as_of=as_of,
            candidates=candidates,
            reference_rates=reference,
            customs_rate=customs,
            rule_sets=rule_sets,
        ),
        None,
    )


async def _latest_rate(
    conn: Conn, actor: ActorContext, currency: str, purpose: FxPurpose, *, as_of: datetime, fixtures: bool
) -> StoredFxRate | None:
    """The latest observation of the EUR/<currency> pair in either stored direction."""
    found: list[StoredFxRate] = []
    for base, quote in (("EUR", currency), (currency, "EUR")):
        found += await valuation_repo.latest_fx_rates(
            conn,
            actor,
            base=base,
            quote=quote,
            purpose=purpose,
            on_or_before=as_of.date(),
            include_fixtures=fixtures,
            limit=1,
        )
    if not found:
        return None
    return max(found, key=lambda r: (r.rate.rate_date, r.rate.retrieved_at, str(r.id)))


async def ensure_cost_profile(
    ctx: RuntimeContext, actor: ActorContext, profile: CostProfile
) -> StoredCostProfile:
    """The stored row of the configured profile version (stored unapproved on first use)."""

    async def once() -> StoredCostProfile:
        async with unit_of_work(ctx.db, actor) as conn:
            try:
                return await valuation_repo.find_cost_profile(
                    conn, actor, profile.profile_key, profile.version
                )
            except NotFound:
                pass
        try:
            async with unit_of_work(ctx.db, actor) as conn:
                return await valuation_repo.store_cost_profile(conn, actor, profile)
        except VersionConflict:  # a concurrent worker stored the same version first
            async with unit_of_work(ctx.db, actor) as conn:
                return await valuation_repo.find_cost_profile(
                    conn, actor, profile.profile_key, profile.version
                )

    stored = await retry_transient(once)
    if _content_sha(stored.profile) != _content_sha(profile):
        raise ValidationFailed(
            "the stored cost profile version differs from the configured one; bump its version"
        )
    return stored


def _content_sha(profile: CostProfile) -> str:
    """Content hash without the approval record (approval is a separate owner act)."""
    return profile.model_copy(
        update={"approval_status": "unapproved", "approved_by": None, "approved_at": None}
    ).sha256()


# --------------------------------------------------------------------------------------------
# Compute + commit
# --------------------------------------------------------------------------------------------


def _superseded_result(superseded: valuation_repo.SupersededRevision) -> dict[str, str | None]:
    return {
        "skipped": superseded.reason,
        "cited_revision_id": str(superseded.cited_revision_id),
        "current_revision_id": None
        if superseded.current_revision_id is None
        else str(superseded.current_revision_id),
    }


def _purchase(screening: ScreeningResult, revision_id: UUID) -> PurchaseInput:
    if screening.payable_amount is None:
        return PurchaseInput(status=CostLineStatus.UNKNOWN)
    return PurchaseInput(
        status=CostLineStatus.ESTIMATED,
        amount=screening.payable_amount,
        basis="advertised payable asking amount; not a confirmed purchase price",
        evidence_ids=(f"{PURCHASE_EVIDENCE_PREFIX}{revision_id}",),
    )


def _tax_inputs(
    listing: NormalizedListing,
    screening: ScreeningResult,
    as_of: datetime,
    customs: FxRate | None,
    jurisdiction: str,
) -> TaxInputs:
    """Only facts the listing establishes; everything else stays missing (calculation incomplete)."""
    vehicle = listing.vehicle
    used = vehicle.mileage_km is not None and vehicle.mileage_km >= USED_VEHICLE_MIN_KM
    return TaxInputs(
        declaration_date=as_of.date(),
        jurisdiction=jurisdiction,
        vehicle_condition="used" if used else None,
        seller_country=listing.location.country,
        invoice_price=screening.payable_amount,
        co2_g_km=listing.co2.g_per_km,
        co2_cycle=listing.co2.cycle,
        emissions_class=listing.documentation.emissions_class,
        fuel=vehicle.fuel,
        engine_displacement_cm3=None
        if vehicle.engine_displacement_cm3 is None
        else Decimal(vehicle.engine_displacement_cm3),
        power_kw=None if vehicle.power_kw is None else Decimal(vehicle.power_kw),
        customs_fx_rate=customs,
    )


def _selection(ctx: RuntimeContext, reads: ValuationReads) -> tuple[RuleSelection, StoredRuleSet | None]:
    options = ctx.options
    selection = select_rule_set(
        [r.rule_set for r in reads.rule_sets],
        options.tax_jurisdiction,
        options.tax_vehicle_category,
        reads.as_of.date(),
    )
    stored = None
    if selection.rule_set is not None:
        chosen = selection.rule_set
        stored = next(
            r
            for r in reads.rule_sets
            if (r.rule_set.rule_set_id, r.rule_set.version) == (chosen.rule_set_id, chosen.version)
        )
    return selection, stored


def _rank(reads: ValuationReads, result: ComparableSetResult, valuation: Valuation) -> RankResult:
    known, total = completeness_from_listing(reads.normalized)
    mechanical, document = risk_flags_from_listing(reads.normalized)
    complete = valuation.state in _FIGURE_STATES
    conservative = valuation.conservative_contribution
    return rank_candidate(
        RankingFeatures(
            listing_id=reads.listing.id,
            as_of=reads.as_of,
            acquisition_price_eur=reads.screening.eur_amount,
            comparable_status=result.status,
            mk_band_fit=result.mk_band_fit,
            valuation_complete=complete,
            conservative_contribution_eur=conservative.amount
            if complete and conservative is not None
            else None,
            known_required_facts=known,
            total_required_facts=total,
            last_checked_at=reads.listing.last_detail_success_at,
            mechanical_risks=mechanical[:50],
            document_risks=document[:50],
        )
    )


def _fx_ids(valuation: Valuation, stored: list[StoredFxRate]) -> tuple[UUID, ...]:
    by_key = {FxDependency.from_rate(s.rate).key(): s.id for s in stored}
    ids: list[UUID] = []
    for dependency in valuation.dependencies.fx:
        row = by_key.get(dependency.key())
        if row is None:
            raise ValidationFailed("a valuation FX dependency is not a stored observation")
        ids.append(row)
    return tuple(ids)


async def _value_and_commit(
    ctx: RuntimeContext, execution: JobExecution, reads: ValuationReads, stored_profile: StoredCostProfile
) -> ValuationRun:
    job = execution.job
    actor = execution.actor
    screening = reads.screening
    revision = reads.revision
    rejected = screening.state == EligibilityState.REJECTED
    target = ComparableTarget.from_listing(
        reads.normalized, listing_id=reads.listing.id, is_fixture=reads.is_fixture
    )
    stored_rates = list(reads.reference_rates.values())
    result = select_comparables(
        target, reads.candidates, reads.config, reads.as_of, [r.rate for r in stored_rates]
    )
    selection, stored_rule = _selection(ctx, reads)
    calc: TaxCalculation | None = None
    if selection.rule_set is not None and not rejected:
        customs = None if reads.customs_rate is None else reads.customs_rate.rate
        calc = calculate_tax(
            selection.rule_set,
            _tax_inputs(reads.normalized, screening, reads.as_of, customs, ctx.options.tax_jurisdiction),
            reads.as_of,
        )
    country = reads.normalized.location.country or reads.source.country
    scope = CostScope(origin_country=country, listing_id=str(reads.listing.id))
    profile = stored_profile.profile
    lines = [line for line in profile.lines(target_scope=scope) if line.category not in IMPORT_CATEGORIES]
    lines += list(tax_cost_lines(calc, missing_reason=None if calc is not None else selection.reason))
    purchase = _purchase(screening, revision.id)
    comparable_rates = [
        reads.reference_rates[c]
        for c in sorted({s.observation.amount.currency for s in result.selected if s.observation.amount})
        if c in reads.reference_rates
    ]
    all_rates = stored_rates + ([reads.customs_rate] if reads.customs_rate is not None else [])
    screening_input = ScreeningInput(
        eligibility=screening.state,
        profile_key=screening.profile,
        eur_payable=None if screening.eur_amount is None else Money.of(screening.eur_amount, "EUR"),
        reasons=tuple(r.code.value for r in screening.reasons)[:50],
        is_fixture=reads.is_fixture,
    )

    async def commit() -> ValuationRun:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            # Lock order: job -> listing. A revision promoted while we computed makes this a no-op.
            superseded = await valuation_repo.lock_current_revision(
                conn, actor, reads.listing.id, revision.id
            )
            if superseded is not None:
                await apply_disposition(conn, job, Disposition.complete(_superseded_result(superseded)))
                return ValuationRun(valuation_id=None, state=None, cases=(), skipped=superseded.reason)
            stored_set = (
                None
                if rejected
                else await market_repo.persist_comparable_set(
                    conn, actor, result, listing_id=reads.listing.id, target_revision_id=revision.id
                )
            )
            proceeds: ProceedsEstimate | None = None
            scenarios = None
            if stored_set is not None:
                proceeds = ProceedsEstimate(
                    **proceeds_from_comparables(
                        result, reads.config.negotiation_discount_pct, comparable_set_id=stored_set.id
                    ).as_proceeds_estimate_kwargs()
                )
                scenarios = compute_scenarios(
                    purchase,
                    lines,
                    proceeds,
                    [r.rate for r in stored_rates],
                    reads.config.contribution_threshold,
                    "EUR",
                    as_of=reads.as_of,
                    target_scope=scope,
                )
            valuation = assemble_valuation(
                listing_revision_id=str(revision.id),
                screening=screening_input,
                comparable=None if stored_set is None else stored_set.reference(),
                tax=calc,
                scenarios=scenarios,
                fx_rates=[r.rate for r in comparable_rates] if stored_set is not None else [],
                cost_profile=None if stored_set is None else profile.reference(),
                config_revision_id=str(reads.config_record.id),
                as_of=reads.as_of,
                fixture_inputs=reads.is_fixture,
                tax_unavailable_reason=None if calc is not None else selection.reason,
            )
            refs = valuation_repo.ValuationRefs(
                listing_id=reads.listing.id,
                config_revision_id=reads.config_record.id,
                comparable_set_id=None if stored_set is None else stored_set.id,
                tax_rule_set_row_id=(
                    stored_rule.row_for(ctx.options.tax_vehicle_category)
                    if calc is not None and stored_rule is not None
                    else None
                ),
                cost_profile_id=None if stored_set is None else stored_profile.id,
                fx_rate_ids=_fx_ids(valuation, all_rates),
            )
            inputs = (
                valuation_repo.ValuationInputs()
                if scenarios is None
                else valuation_repo.ValuationInputs(
                    cost_lines=tuple(lines),
                    purchase=purchase,
                    proceeds=proceeds,
                    import_line_sources=scenarios.import_line_sources,
                )
            )
            persisted = await valuation_repo.persist_valuation(
                conn, actor, valuation, refs, inputs, require_current_revision=True
            )
            if isinstance(persisted, valuation_repo.SupersededRevision):  # pragma: no cover - locked above
                raise VersionConflict("The listing has a newer revision; recompute")
            stored = persisted
            rank = None if rejected else _rank(reads, result, valuation)
            cases = []
            for key, search_profile in sorted(reads.config.profiles.items(), key=lambda kv: kv[0].value):
                if not search_profile.enabled:
                    continue
                cases.append(
                    await reviews_repo.upsert_review_case(
                        conn,
                        actor,
                        reads.listing.id,
                        revision.id,
                        screening,
                        stored.id,
                        search_profile,
                        dashboard_base_url=ctx.dashboard_base_url,
                        rank=rank if key == screening.profile else None,
                    )
                )
            await apply_disposition(
                conn,
                job,
                Disposition.complete(
                    {
                        "valuation_id": str(stored.id),
                        "state": valuation.state.value,
                        "comparable_set_id": None if stored_set is None else str(stored_set.id),
                    }
                ),
            )
        return ValuationRun(valuation_id=stored.id, state=valuation.state, cases=tuple(cases))

    run = await retry_transient(commit)
    if run.skipped is not None:
        logger.info("valuation skipped", extra={"reason": run.skipped})
        return run
    if screening.profile is not None and not rejected:
        ctx.metrics.record_comparables(
            screening.profile, sample_size=len(result.selected), quality=_COMPARABLE_METRIC[result.status]
        )
    return run


__all__ = ["PURCHASE_EVIDENCE_PREFIX", "ValuationPayload", "ensure_cost_profile", "handle_valuation"]
