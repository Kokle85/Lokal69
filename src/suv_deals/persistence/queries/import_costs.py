"""Import-cost calculator read: customs duty, motor-vehicle tax (ДМВ) and import VAT for one
vehicle, from a listing's facts and/or the caller's (fit design 5-6; dot via MCP, dashboard API,
CLI). Pure computation in ``domain.import_estimate``; this module only gathers its inputs.

- Scope ``deals:read``. Read-only: nothing is persisted (no valuation, cost line, alert or audit
  row) and the result carries no ``TaxCalculation`` that could become cost lines.
- Facts: with ``listing_id``, the listing's CURRENT, non-quarantined revision
  (`import_estimate.facts_from_listing`: its screened payable amount as an advertised price, its
  CO2 figure as ``listing_claim_unverified``, no proof of origin). Missing and foreign-workspace
  ids raise the same ``NotFound``; fixture-lineage listings are allowed and labelled. Every field
  the request's ``vehicle`` sets overrides the listing's; fields it leaves unset keep the
  listing's. Caller CO2 readings without a stated document never inherit the listing's claim
  label, and a caller price without a stated basis counts as advertised.
- Two values (owner decision 2026-10-13): the vehicle's ``price`` is the REAL purchase price (the
  landed cost uses it); an ``estimated_customs_value_eur`` with its source, when given, carries
  the import charges of the ``primary`` scenario, and the ``transaction_value`` scenario shows the
  charges on the price paid.
- Recorded values (``import_values_repo``): in listing mode the listing's CURRENT recorded real
  purchase price replaces the advertised price (``price_basis`` ``recorded_actual`` /
  ``recorded_quote`` / ``recorded_estimate``) and its current recorded estimated customs value is
  the estimate, each with fact source ``recorded``, unless the request's ``vehicle`` sets that
  fact (a request ``price`` replaces the recorded purchase price with its own basis; a request
  estimate replaces the recorded one). A request ``price_basis`` without a ``price`` cannot relabel
  a recorded purchase price (refused on ``price_basis``: it keeps its recorded kind).
  ``purchase_price.source`` / ``customs_value_for_duties.source`` name the recorded value (kind,
  amount, source, date), the notes say which recorded values were used or overridden, and
  ``listing`` carries their ids.
- Rule set: the production selection first (``select_rule_set`` over the STORED rule sets: an
  ACTIVE, owner-approved, non-fixture version, exactly as valuations select it) -> ``active``;
  otherwise the indicative preview (``select_preview_rule_set``) over the stored approved,
  under-review and draft versions plus the DRAFT files in ``config/tax_rules/drafts`` ->
  ``indicative_preview``; otherwise ``unavailable``. Files never reach the production
  selection, and a file that is not a draft is ignored. A file and a stored row of the same
  version with different content, or an ambiguous preview, leave the result ``unavailable``
  with the reason.
- Customs FX: the latest RECORDED EUR/<rule currency> rate with purpose ``customs`` dated on or
  before the declaration date. When none is recorded for the declaration month, the caller's
  ``customs_eur_mkd_rate`` is used for this estimate only (assumption
  ``CALLER_SUPPLIED_CUSTOMS_RATE``; never stored); otherwise the customs value stays unknown
  and the warning names the ``suv-deals fx record --purpose customs`` command. A recorded rate
  of an earlier month is never presented as used (``customs_fx_source``
  ``recorded_not_effective``, ``customs_fx_rate`` null, ``customs_fx_rate`` in ``missing_facts``)
  unless last month's rate is accepted for a preview.
- Transport: the configured cost profile's transport line
  (``config/cost_profiles/default_unapproved.yaml``, the file the worker runtime loads). Its
  approval is the workspace's STORED version of it (the owner approves in Settings), read-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as ModelValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.costs import CostLine, CostProfile, CostScope, load_cost_profile
from suv_deals.domain.enums import (
    CostCategory,
    CostLineStatus,
    Fuel,
    FxPurpose,
    Scope,
    SourceMode,
    TaxRuleStatus,
)
from suv_deals.domain.import_estimate import (
    DEFAULT_JURISDICTION,
    DEFAULT_VEHICLE_CATEGORY,
    ImportCostEstimate,
    VehicleImportFacts,
    declaration_date_for,
    estimate_import_costs,
    facts_from_listing,
    single_transport_line,
)
from suv_deals.domain.listings import PartialDate
from suv_deals.domain.money import FxRate
from suv_deals.domain.tax_engine import (
    RuleSelection,
    RuleSet,
    load_rule_set_file,
    select_preview_rule_set,
    select_rule_set,
)
from suv_deals.errors import AppError, ErrorCode, NotFound, ValidationFailed
from suv_deals.persistence import import_values_repo, listings_repo, sources_repo, valuation_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.import_values_repo import ImportValues, apply_recorded_values
from suv_deals.persistence.queries._common import QueryResult, db_now, rendering
from suv_deals.persistence.valuation_repo import StoredFxRate, StoredRuleSet
from suv_deals.views.common import ResponseWarning, WarningCode, warning
from suv_deals.views.import_costs import (
    FIXTURE_LABEL,
    CustomsFxSource,
    FactSource,
    ImportCostEstimateView,
    ImportListingView,
)

IMPORT_COSTS_QUERY: Final = "deals_estimate_import_costs"
#: Relative to ``Settings.config_dir``; the same files the worker runtime and ``config validate`` use.
DRAFT_RULES_DIR: Final = Path("tax_rules") / "drafts"
COST_PROFILE_FILE: Final = Path("cost_profiles") / "default_unapproved.yaml"
#: The pair of the caller-supplied customs rate (``customs_eur_mkd_rate``).
CALLER_RATE_PAIR: Final = ("EUR", "MKD")
CALLER_RATE_PROVIDER: Final = "caller-supplied (not recorded)"
#: Listing facts ``facts_from_listing`` can establish (provenance ``listing`` when known).
_LISTING_FACTS: Final = frozenset(
    {
        "price",
        "price_basis",
        "fuel",
        "co2",
        "co2_document",
        "first_registration",
        "emissions_class",
        "mileage_km",
        "seller_country",
        "engine_displacement_cm3",
        "power_kw",
    }
)


class ImportCostRequest(BaseModel):
    """What the caller asks for. ``vehicle`` fields that are SET override the listing's facts
    (unset ones keep them); without ``listing_id`` the vehicle facts stand alone."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    listing_id: UUID | None = None
    vehicle: VehicleImportFacts | None = None
    #: 1 EUR = rate MKD, the Customs monthly list rate the caller knows; used only when no customs
    #: rate is recorded for the declaration month, labelled, never stored.
    customs_eur_mkd_rate: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=10)
    #: Preview only: accept last month's recorded customs rate (labelled assumption).
    accept_previous_month_fx: bool = False

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("customs_eur_mkd_rate"), float):
            raise ValueError("customs_eur_mkd_rate: binary float is not allowed; use a decimal string")
        return data

    @model_validator(mode="after")
    def _something_to_estimate(self) -> ImportCostRequest:
        if self.listing_id is None and self.vehicle is None:
            raise ValueError("give a listing_id, vehicle facts or both")
        return self


@dataclass(frozen=True, slots=True)
class ImportCostResult:
    """The pure estimate plus where its facts and customs rate came from."""

    estimate: ImportCostEstimate
    facts: VehicleImportFacts
    fact_sources: Mapping[str, FactSource]
    customs_fx_source: CustomsFxSource
    listing: ImportListingView | None
    as_of: datetime

    def view(self) -> ImportCostEstimateView:
        return ImportCostEstimateView.of(
            self.estimate,
            facts=self.facts,
            fact_sources=self.fact_sources,
            customs_fx_source=self.customs_fx_source,
            listing=self.listing,
        )


# --------------------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class _ListingFacts:
    view: ImportListingView
    facts: VehicleImportFacts
    notes: tuple[str, ...]
    recorded: ImportValues
    #: Facts the recorded import values supplied (before request overrides).
    recorded_facts: frozenset[str]


async def _listing_facts(conn: Conn, actor: ActorContext, listing_id: UUID) -> _ListingFacts:
    listing = await listings_repo.get_listing(conn, actor, listing_id)
    revision = await listings_repo.current_revision(conn, actor, listing_id)
    if revision is None or revision.quarantined or listing.quarantined:
        raise NotFound("The listing has no current, non-quarantined revision")
    try:
        normalized = revision.listing()
    except ValidationFailed:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "The stored listing revision could not be read", retryable=False
        ) from None
    source = await sources_repo.get_source_record(conn, actor, listing.source_id)
    # Lineage frozen at ingest; a source that is now fixture also taints the result (fail-safe).
    is_fixture = listing.is_fixture or source.mode == SourceMode.FIXTURE
    screening = listing.screening_result()
    notes: list[str] = []
    if screening is None:
        notes.append("LISTING_NOT_SCREENED: screening sets the payable amount; the listing price is unknown")
    elif screening.payable_amount is None:
        notes.append("LISTING_PAYABLE_UNKNOWN: screening found no payable amount; the price is unknown")
    facts = facts_from_listing(normalized, payable=None if screening is None else screening.payable_amount)
    recorded = await import_values_repo.current_import_values(conn, actor, listing_id, history_limit=0)
    facts, recorded_facts = apply_recorded_values(facts, recorded)
    if normalized.co2.g_per_km is not None and not facts.co2:
        notes.append(
            f"LISTING_CO2_NOT_A_FACT:{str(normalized.co2.g_per_km)[:40]}: the listing's CO2 figure is not "
            f"plausible for a {normalized.vehicle.fuel.value} car (under 1 g/km or a parse artefact); CO2 "
            "unknown until a document's figure is given"
        )
    view = ImportListingView(
        listing_id=listing.id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        source_key=source.source_key,
        is_fixture=is_fixture,
        fixture_label=FIXTURE_LABEL if is_fixture else None,
        screened=screening is not None,
        recorded_purchase_price_id=None if recorded.purchase is None else recorded.purchase.id,
        recorded_customs_value_id=None if recorded.customs_value is None else recorded.customs_value.id,
    )
    return _ListingFacts(
        view=view, facts=facts, notes=tuple(notes), recorded=recorded, recorded_facts=recorded_facts
    )


def _known(value: object) -> bool:
    if value is None or value == () or (isinstance(value, Fuel) and value == Fuel.UNKNOWN):
        return False
    return not (isinstance(value, PartialDate) and value.value is None)


def merge_facts(
    listing: VehicleImportFacts | None,
    override: VehicleImportFacts | None,
    *,
    recorded: frozenset[str] = frozenset(),
) -> tuple[VehicleImportFacts, dict[str, FactSource]]:
    """Listing facts with the request's explicitly SET fields on top, and each fact's source.

    ``recorded`` names the listing facts a recorded import value supplied (source ``recorded``
    unless the request sets them). Caller CO2 readings without a stated document get no document
    (never the listing's claim label); a caller price without a stated basis counts as advertised.
    A request ``price_basis`` without a ``price`` is refused when the price is a RECORDED one: the
    owner's recorded purchase price keeps its recorded kind (an actual, a quote or an estimate),
    exactly as the valuation uses it; only a price the request gives can carry another basis.
    """
    data = (listing or VehicleImportFacts()).model_dump()
    chosen = override.model_fields_set if override is not None else set()
    if listing is not None and "price" in recorded and "price_basis" in chosen and "price" not in chosen:
        raise ValidationFailed(
            "the recorded purchase price keeps its recorded kind; give a price to state another basis "
            "(or record a new value for the listing)",
            details={"fields": ["price_basis"]},
        )
    if override is not None:
        for name in chosen:
            data[name] = getattr(override, name)
        if "co2" in chosen and "co2_document" not in chosen:
            data["co2_document"] = None
        if "price" in chosen and "price_basis" not in chosen:
            data["price_basis"] = override.price_basis
    try:
        facts = VehicleImportFacts.model_validate(data)
    except ModelValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        raise ValidationFailed(
            "the listing facts and the request's vehicle facts are inconsistent",
            details={"fields": fields},
        ) from None
    sources: dict[str, FactSource] = {}
    for name in VehicleImportFacts.model_fields:
        if name in chosen:
            sources[name] = "request"
        elif listing is not None and name in recorded:
            sources[name] = "recorded"
        elif (
            listing is not None
            and name in _LISTING_FACTS
            and _known(getattr(facts, "price" if name == "price_basis" else name))
        ):
            sources[name] = "listing"
        else:
            sources[name] = "default"
    # Derived from the caller's own fact, not the listing's: no document / the advertised default.
    for name, coupled in (("co2_document", "co2"), ("price_basis", "price")):
        if coupled in chosen and name not in chosen:
            sources[name] = "default"
    return facts, sources


# --------------------------------------------------------------------------- rule sets


def load_draft_rule_sets(directory: Path) -> tuple[list[RuleSet], list[str]]:
    """The DRAFT rule-set files of ``config/tax_rules/drafts`` (sorted by name). An unreadable or
    invalid file, or one whose status is not ``draft``, is skipped with a note: approval and
    activation happen only through the stored owner transitions, never through a file. Only
    regular files directly in the directory count (a symlink leaving it is skipped), the same
    rule as the owner's store route (``api.owner_config_routes``)."""
    rule_sets: list[RuleSet] = []
    notes: list[str] = []
    if not directory.is_dir():
        return rule_sets, notes
    root = directory.resolve()
    for path in sorted(directory.glob("*.json")):
        resolved = path.resolve()
        if resolved.parent != root or not resolved.is_file():
            notes.append(
                f"DRAFT_RULE_FILE_IGNORED:{path.name[:160]}: not a regular file in the drafts directory"
            )
            continue
        try:
            rule_set = load_rule_set_file(path)
        except ValidationFailed as exc:
            notes.append(f"DRAFT_RULE_FILE_UNREADABLE:{path.name}: {exc.message}"[:500])
            continue
        if rule_set.status != TaxRuleStatus.DRAFT or rule_set.is_fixture:
            notes.append(
                f"DRAFT_RULE_FILE_IGNORED:{path.name}: status {rule_set.status.value}"
                f"{', fixture' if rule_set.is_fixture else ''}; files under tax_rules/drafts are drafts only"
            )
            continue
        rule_sets.append(rule_set)
    return rule_sets, notes


def select_calculator_rule_set(
    stored: Sequence[StoredRuleSet],
    files: Sequence[RuleSet],
    jurisdiction: str,
    category: str,
    on_date: date,
) -> RuleSelection:
    """Production selection over the STORED rule sets first; only without an ACTIVE one, the
    indicative preview over the stored versions plus the draft files. A conflict between a file
    and a stored row of one version, or an ambiguous preview, selects nothing (with the reason)."""
    stored_sets = [s.rule_set for s in stored]
    production = select_rule_set(stored_sets, jurisdiction, category, on_date)
    if production.rule_set is not None:
        return production
    created = {s.rule_set.label(): s.created_at for s in stored}
    try:
        preview = select_preview_rule_set(
            [*stored_sets, *files], jurisdiction, category, on_date, created_at=created
        )
    except ValidationFailed as exc:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(exc.details.items()))
        conflict = "different content" in exc.message
        code = "CONFLICTING_RULE_SET_COPIES" if conflict else "AMBIGUOUS_PREVIEW"
        hint = (
            "the draft file and the stored version differ; remove or re-version one"
            if conflict
            else "remove or re-date one of the candidates"
        )
        return RuleSelection(
            rule_set=None,
            reason=f"{production.reason}; {code}: {exc.message} ({detail}); {hint}; no indicative preview, "
            "import costs unknown",
            allow_unapproved=True,
            considered=production.considered,
            mode="indicative_preview",
        )
    considered = tuple(dict.fromkeys((*production.considered, *preview.considered)))
    if preview.rule_set is not None:
        return preview.model_copy(update={"considered": considered})
    return preview.model_copy(
        update={"reason": f"{production.reason}; {preview.reason}", "considered": considered}
    )


# --------------------------------------------------------------------------- customs FX


@dataclass(frozen=True, slots=True)
class _CustomsRate:
    rate: FxRate | None
    source: CustomsFxSource
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]


def _record_hint(quote: str, declaration: date) -> str:
    month = declaration.replace(day=1).isoformat()
    return (
        f"record the Customs monthly EUR/{quote} list rate for {declaration.strftime('%Y-%m')} with "
        f"`suv-deals fx record --base EUR --quote {quote} --rate <rate> --date {month} --purpose customs "
        "--provider <publisher> --source-ref <document> --reason <why> --yes`"
    )


async def _latest_customs(
    conn: Conn, actor: ActorContext, quote: str, on_or_before: date, *, fixtures: bool
) -> StoredFxRate | None:
    """The newest recorded customs observation of the EUR/<quote> pair, either stored direction."""
    found: list[StoredFxRate] = []
    for base, other in (("EUR", quote), (quote, "EUR")):
        found += await valuation_repo.latest_fx_rates(
            conn,
            actor,
            base=base,
            quote=other,
            purpose=FxPurpose.CUSTOMS,
            on_or_before=on_or_before,
            include_fixtures=fixtures,
            limit=1,
        )
    if not found:
        return None
    return max(found, key=lambda r: (r.rate.rate_date, r.rate.retrieved_at, str(r.id)))


async def _customs_rate(
    conn: Conn,
    actor: ActorContext,
    *,
    quote: str,
    declaration: date,
    caller_rate: Decimal | None,
    fixtures: bool,
    now: datetime,
) -> _CustomsRate:
    if quote == "EUR":
        ignored = ("CALLER_CUSTOMS_RATE_IGNORED: the rule currency is EUR; no customs rate is needed",)
        return _CustomsRate(None, "not_needed", (), ignored if caller_rate is not None else ())
    stored = await _latest_customs(conn, actor, quote, declaration, fixtures=fixtures)
    in_month = stored is not None and (stored.rate.rate_date.year, stored.rate.rate_date.month) == (
        declaration.year,
        declaration.month,
    )
    warnings: list[str] = []
    month = declaration.strftime("%Y-%m")
    if stored is not None and in_month:
        if caller_rate is not None:
            warnings.append(
                f"CALLER_CUSTOMS_RATE_IGNORED: a Customs rate is recorded for {month} "
                f"({stored.rate.rate_date.isoformat()}, {stored.rate.provider}); it is used instead"
            )
        return _CustomsRate(stored.rate, "recorded", (), tuple(warnings))
    usable_caller = caller_rate is not None and ("EUR", quote) == CALLER_RATE_PAIR
    if caller_rate is not None and not usable_caller:
        warnings.append(f"CALLER_CUSTOMS_RATE_IGNORED: the rule currency is {quote}, not MKD")
    if caller_rate is not None and usable_caller:
        month_start = declaration.replace(day=1)
        rate = FxRate(
            base="EUR",
            quote=quote,
            rate=caller_rate,
            rate_date=month_start,
            retrieved_at=now,
            provider=CALLER_RATE_PROVIDER,
            purpose=FxPurpose.CUSTOMS,
        )
        assumption = (
            f"CALLER_SUPPLIED_CUSTOMS_RATE:1 EUR = {caller_rate} {quote} for {month}: "
            "stated by the caller as the Customs monthly list rate; not a recorded rate, never stored"
        )
        if stored is not None:
            warnings.append(
                f"CUSTOMS_FX_RATE_NOT_FOR_DECLARATION_MONTH: the newest recorded Customs rate is of "
                f"{stored.rate.rate_date.isoformat()}; {_record_hint(quote, declaration)}"
            )
        return _CustomsRate(rate, "caller_supplied", (assumption,), tuple(warnings))
    if stored is not None:
        warnings.append(
            f"CUSTOMS_FX_RATE_NOT_FOR_DECLARATION_MONTH: the newest recorded Customs rate is of "
            f"{stored.rate.rate_date.isoformat()}; the customs value stays unknown unless last month's rate "
            f"is accepted for a preview; {_record_hint(quote, declaration)}, or pass customs_eur_mkd_rate"
        )
        return _CustomsRate(stored.rate, "recorded", (), tuple(warnings))
    warnings.append(
        f"CUSTOMS_FX_RATE_NOT_RECORDED: no Customs EUR/{quote} rate is recorded on or before "
        f"{declaration.isoformat()}; the customs value is unknown (never converted with another rate); "
        f"{_record_hint(quote, declaration)}, or pass customs_eur_mkd_rate for this estimate"
    )
    return _CustomsRate(None, "none", (), tuple(warnings))


# --------------------------------------------------------------------------- transport


def _transport(config_dir: Path, scope: CostScope) -> tuple[CostLine, CostProfile | None, tuple[str, ...]]:
    path = config_dir / COST_PROFILE_FILE
    if not path.is_file():
        reason = f"no cost profile at {COST_PROFILE_FILE.as_posix()}; the transport amount is unknown"
        line = CostLine(
            category=CostCategory.TRANSPORT,
            label="Transport (cost profile)",
            status=CostLineStatus.UNKNOWN,
            currency="EUR",
            reason=reason,
        )
        return line, None, (f"COST_PROFILE_MISSING: {reason}",)
    try:
        profile = load_cost_profile(path)
    except ValidationFailed:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "The configured cost profile could not be read", retryable=False
        ) from None
    return single_transport_line(profile.lines(target_scope=scope)), profile, ()


async def _profile_note(conn: Conn, actor: ActorContext, profile: CostProfile) -> str:
    """The configured profile's approval as the workspace records it: the owner approves the
    STORED version (Settings), the same row valuations use; the file always says unapproved."""
    tag = f"COST_PROFILE:{profile.profile_key}@v{profile.version}"
    try:
        stored = await valuation_repo.find_cost_profile(conn, actor, profile.profile_key, profile.version)
    except NotFound:
        return f"{tag} ({profile.approval_status}; not yet stored in this workspace)"
    if stored.profile.content_sha256() != profile.content_sha256():
        return (
            f"{tag} (the configured file differs from the stored version, {stored.profile.approval_status}; "
            "bump its version)"
        )
    return f"{tag} ({stored.profile.approval_status})"


# --------------------------------------------------------------------------- service


async def estimate_import_costs_for(
    conn: Conn,
    actor: ActorContext,
    request: ImportCostRequest,
    *,
    config_dir: Path,
    as_of: datetime | None = None,
    jurisdiction: str = DEFAULT_JURISDICTION,
    vehicle_category: str = DEFAULT_VEHICLE_CATEGORY,
) -> ImportCostResult:
    """Estimate the import charges for one vehicle (module docstring). Read-only."""
    actor.require(Scope.DEALS_READ)
    now = ensure_utc(as_of) if as_of is not None else await db_now(conn)
    listing = None if request.listing_id is None else await _listing_facts(conn, actor, request.listing_id)
    facts, sources = merge_facts(
        None if listing is None else listing.facts,
        request.vehicle,
        recorded=frozenset() if listing is None else listing.recorded_facts,
    )
    declaration = declaration_date_for(facts, now)

    stored = await valuation_repo.list_rule_sets(conn, actor, jurisdiction=jurisdiction)
    files, file_notes = load_draft_rule_sets(config_dir / DRAFT_RULES_DIR)
    selection = select_calculator_rule_set(stored, files, jurisdiction, vehicle_category, declaration)
    chosen = selection.rule_set
    quote = (chosen.currency if chosen is not None else None) or CALLER_RATE_PAIR[1]
    customs = await _customs_rate(
        conn,
        actor,
        quote=quote,
        declaration=declaration,
        caller_rate=request.customs_eur_mkd_rate,
        fixtures=listing is not None and listing.view.is_fixture,
        now=now,
    )
    scope = CostScope(
        origin_country=facts.seller_country,
        listing_id=None if request.listing_id is None else str(request.listing_id),
    )
    transport, profile, profile_notes = _transport(config_dir, scope)
    if profile is not None:
        profile_notes = (*profile_notes, await _profile_note(conn, actor, profile))
    estimate = estimate_import_costs(
        selection,
        facts,
        transport=transport,
        customs_rate=customs.rate,
        as_of=now,
        jurisdiction=jurisdiction,
        vehicle_category=vehicle_category,
        accept_previous_month_fx=request.accept_previous_month_fx,
        attested_by=f"{actor.principal_kind}:{actor.principal_id}",
    )
    warnings = [
        *(() if listing is None else listing.notes),
        *customs.warnings,
        *file_notes,
        *(n for n in profile_notes if n.startswith("COST_PROFILE_MISSING")),
    ]
    notes = [
        *(() if listing is None else _recorded_notes(listing.recorded, sources)),
        *(n for n in profile_notes if not n.startswith("COST_PROFILE_MISSING")),
    ]
    update: dict[str, object] = {
        "assumptions": tuple(dict.fromkeys([*estimate.assumptions, *customs.assumptions])),
        "warnings": tuple(dict.fromkeys([*estimate.warnings, *(w[:500] for w in warnings)])),
        "notes": tuple(dict.fromkeys([*estimate.notes, *(n[:500] for n in notes)])),
    }
    if listing is not None:
        update.update(_recorded_labels(estimate, listing.recorded, sources))
    estimate = estimate.model_copy(update=update)
    fx_source: CustomsFxSource = customs.source
    if customs.rate is not None and estimate.customs_fx_rate is None and customs.source == "recorded":
        # The newest recorded rate is not valid on the declaration date and was not used.
        fx_source = "recorded_not_effective"
    return ImportCostResult(
        estimate=estimate,
        facts=facts,
        fact_sources=sources,
        customs_fx_source=fx_source,
        listing=None if listing is None else listing.view,
        as_of=now,
    )


def _purchase_recorded(sources: Mapping[str, FactSource]) -> bool:
    """The estimate's purchase price is the recorded one: its amount AND its basis (the recorded
    kind) both come from the recorded value (``merge_facts`` never splits them)."""
    return sources.get("price") == "recorded" and sources.get("price_basis") == "recorded"


def _customs_value_recorded(sources: Mapping[str, FactSource]) -> bool:
    """The estimated customs value and its source both come from the recorded value."""
    return (
        sources.get("estimated_customs_value_eur") == "recorded"
        and sources.get("estimated_customs_value_source") == "recorded"
    )


def _recorded_notes(recorded: ImportValues, sources: Mapping[str, FactSource]) -> list[str]:
    """Which of the listing's recorded import values the estimate used, and which the request
    replaced."""
    notes: list[str] = []
    for code, record, used in (
        ("RECORDED_PURCHASE_PRICE", recorded.purchase, _purchase_recorded(sources)),
        ("RECORDED_CUSTOMS_VALUE", recorded.customs_value, _customs_value_recorded(sources)),
    ):
        if record is None:
            continue
        if used:
            notes.append(f"{code}_USED:{record.id}: {record.describe()}")
        else:
            notes.append(
                f"{code}_OVERRIDDEN:{record.id}: the request's value replaces the {record.describe()}"
            )
    return notes


def _recorded_labels(
    estimate: ImportCostEstimate, recorded: ImportValues, sources: Mapping[str, FactSource]
) -> dict[str, object]:
    """``purchase_price`` / ``customs_value_for_duties`` sources that name the recorded value used."""
    update: dict[str, object] = {}
    purchase = recorded.purchase if _purchase_recorded(sources) else None
    if purchase is not None:
        update["purchase_price"] = estimate.purchase_price.model_copy(
            update={"source": purchase.describe()[:200]}
        )
    duties = estimate.customs_value_for_duties
    customs = recorded.customs_value if _customs_value_recorded(sources) else None
    if duties.basis == "estimated" and customs is not None:
        update["customs_value_for_duties"] = duties.model_copy(update={"source": customs.describe()[:200]})
    elif duties.basis == "transaction_value" and purchase is not None:
        update["customs_value_for_duties"] = duties.model_copy(update={"source": purchase.describe()[:200]})
    return update


def import_cost_warnings(view: ImportCostEstimateView) -> list[ResponseWarning]:
    """Typed envelope warnings (existing codes only): unapproved/absent rules, unknown charges,
    fixture lineage and unverified claims."""
    warnings: list[ResponseWarning] = []
    if view.mode == "indicative_preview" and view.banner is not None:
        warnings.append(warning(WarningCode.TAX_RULES_UNAPPROVED, view.banner.en))
    elif view.mode == "unavailable":
        warnings.append(warning(WarningCode.TAX_RULES_UNAPPROVED))
    primary = view.scenarios[0] if view.scenarios else None
    if primary is None or not primary.complete:
        missing = ", ".join(view.missing_facts) or "see the unknown components"
        warnings.append(
            warning(
                WarningCode.UNKNOWN_COSTS,
                f"Some import charges are unknown (never zero); needed: {missing}"[:500],
            )
        )
    if view.listing is not None and view.listing.is_fixture:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    if view.unverified_inputs:
        claims = "; ".join(view.unverified_inputs)
        warnings.append(
            warning(
                WarningCode.SELLER_CLAIMS_UNVERIFIED, f"The figures rest on unverified claims: {claims}"[:500]
            )
        )
    return warnings


async def get_import_cost_estimate(
    conn: Conn,
    actor: ActorContext,
    request: ImportCostRequest,
    *,
    config_dir: Path,
    as_of: datetime | None = None,
    jurisdiction: str = DEFAULT_JURISDICTION,
    vehicle_category: str = DEFAULT_VEHICLE_CATEGORY,
) -> QueryResult[ImportCostEstimateView]:
    """``deals_estimate_import_costs``: the estimate as the shared wire view with typed warnings."""
    result = await estimate_import_costs_for(
        conn,
        actor,
        request,
        config_dir=config_dir,
        as_of=as_of,
        jurisdiction=jurisdiction,
        vehicle_category=vehicle_category,
    )
    with rendering("import-cost estimate"):
        view = result.view()
    return QueryResult(data=view, as_of=result.as_of, warnings=tuple(import_cost_warnings(view)))


__all__ = [
    "CALLER_RATE_PROVIDER",
    "COST_PROFILE_FILE",
    "DRAFT_RULES_DIR",
    "IMPORT_COSTS_QUERY",
    "ImportCostRequest",
    "ImportCostResult",
    "estimate_import_costs_for",
    "get_import_cost_estimate",
    "import_cost_warnings",
    "load_draft_rule_sets",
    "merge_facts",
    "select_calculator_rule_set",
]
