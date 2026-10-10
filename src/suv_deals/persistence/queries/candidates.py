"""Candidate queue and candidate detail reads (spec 21 ``deals_list_candidates``/``deals_get_candidate``,
spec 23 screens 2-3).

A *candidate* is a listing with a promoted current revision that screening did not reject
(``eligible_primary``, ``eligible_manual_profile`` or ``needs_facts``), or any listing that has a
non-superseded review case. Rejected observations stay stored (spec 11) but are not candidates.

Queue (`list_candidates`)
    Keyset pagination over the immutable sort ``(listings.created_at desc, listings.id desc)``
    (unique id tie-breaker) with an as-of boundary (``created_at <= as_of``, database time of the
    first page). Inclusion semantics, precisely: membership and filters are evaluated when each
    page is read, and the cursor only moves forward over the immutable key, so no listing is ever
    listed twice and every listing that is a matching candidate both when the listing started and
    when its page is read is listed exactly once. A listing that stops matching before its page
    (rejected, status changed) is not listed; one that starts matching after the first page shows
    only if it sorts after the cursor; listings created after the as-of boundary wait for a
    re-query. This is NOT a frozen snapshot (the review queue uses ``ops.query_snapshots`` for
    that). Filters: ``profile`` (screened profile), ``country`` (seller country of the current
    revision), ``status`` (review status of the candidate's case; a claim is a lock, not a status:
    a claimed case matches its restore state) and ``changed_since`` (the listing row, its review
    case or the newest valuation of its current revision changed at or after the time).

Detail (`get_candidate`)
    The exact (current or requested) revision: normalized fields, field provenance (extraction
    confidence, never truth) joined with the latest field-evidence row per path, conflicts, price
    and availability history, screening reasons, the latest valuation and comparable references of
    that revision, the review case, the spec 19 due-diligence checklist, notes, ranking and the
    source link. Only promoted revisions are served: quarantined revisions (stored while the source
    parser was unhealthy, spec 30) and revisions newer than the current one never appear in the
    history, and requesting one is ``NOT_FOUND``. The reads are separate statements under READ
    COMMITTED, so every history read is bounded by the current revision number of the first read.
    Availability history comes from the revisions plus ``listing.availability`` audit events
    today; ``app.availability_events`` (spec 37.8) replaces the audit source once that table is
    read here (see `AVAILABILITY_HISTORY_SOURCE`).

Review state shown for a case: ``claimed`` only while the claim is active; an expired claim shows
the restore state (see ``_common.CASE_STATE_SQL``).

Scope ``deals:read``. Every statement carries an explicit ``workspace_id`` predicate (RLS is
defence in depth). Missing and foreign-workspace ids raise the same `NotFound`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import sql
from pydantic import ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.due_diligence import build_checklist
from suv_deals.domain.enums import (
    Availability,
    AvailabilityEvidenceKind,
    ClaimStatus,
    EligibilityState,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    ProfileKey,
    ReviewState,
    Scope,
    Tristate,
    ValuationState,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.listings import NormalizedListing, PartialDate, PriceInfo
from suv_deals.domain.pagination import filter_hash, validate_limit
from suv_deals.domain.ranking import RankResult
from suv_deals.errors import AppError, ErrorCode, NotFound, ValidationFailed
from suv_deals.mcp.schemas import DealsListCandidatesInput
from suv_deals.persistence import notes_repo
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.queries._common import (
    CASE_STATE_SQL,
    CASE_STATUS_SQL,
    FIGURE_STATES,
    CursorSecret,
    QueryResult,
    db_now,
    decimal_or_none,
    decode_keyset,
    encode_keyset,
    enum_or,
    enum_or_none,
    json_field,
    parse_datetime,
    parse_uuid,
    plain_decimal,
    rendering,
    require_secret,
    utc_or_none,
)
from suv_deals.views.candidates import (
    AvailabilityPoint,
    CandidateDetail,
    CandidateListView,
    CandidateSummary,
    ComparableSetRef,
    FieldProvenanceView,
    FreshnessFlag,
    FreshnessView,
    NormalizedFieldsView,
    PriceSummary,
    RankSummary,
    RankView,
    ReviewCaseRef,
    RevisionView,
    ScreeningView,
    SellerTextView,
    SourceLink,
    ValuationRef,
    eur_amount_view,
    price_history,
    safe_http_url,
)
from suv_deals.views.common import AmountView, FxRateView, ResponseWarning, WarningCode, warning

CANDIDATES_QUERY: Final = "deals_list_candidates"
CANDIDATE_STATES: Final = ("eligible_primary", "eligible_manual_profile", "needs_facts")
MAX_HISTORY: Final = 500
MAX_PROVENANCE: Final = 500
MAX_CONFLICTS: Final = 100
MAX_NOTES: Final = 200
#: Where availability history comes from in this schema version (spec 37.8 hook).
AVAILABILITY_HISTORY_SOURCE: Final = "audit_events"
_ELIGIBLE: Final = frozenset({EligibilityState.ELIGIBLE_PRIMARY, EligibilityState.ELIGIBLE_MANUAL_PROFILE})
_DB_QUALITY_TO_STATUS: Final[dict[str, str]] = {
    "adequate": "adequate",
    "small": "small_sample",
    "insufficient": "insufficient_comparables",
}
_EVIDENCE_VIA: Final[
    dict[AvailabilityEvidenceKind, Literal["detail", "search_card", "recheck", "reconciliation"]]
] = {
    AvailabilityEvidenceKind.SOURCE_OBSERVATION: "detail",
    AvailabilityEvidenceKind.SOURCE_SOLD_BADGE: "detail",
    AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE: "detail",
    AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE: "detail",
    AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND: "detail",
    AvailabilityEvidenceKind.COMPLETE_SCAN_ABSENCE: "reconciliation",
    AvailabilityEvidenceKind.SELLER_REPORTED_SOLD: "reconciliation",
    AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE: "reconciliation",
    AvailabilityEvidenceKind.SELLER_REPORTED_RESERVED: "reconciliation",
    AvailabilityEvidenceKind.MANUAL: "reconciliation",
}

# One row per candidate: listing, source, CURRENT revision, the profile's queue label, the
# candidate's review case (the case of its screened profile first, else the most recently
# updated open case; ``case_state`` is claim-aware, ``case_status`` ignores the claim) and the
# newest valuation of the current revision. ``%(now)s`` is the database time of the read.
_CANDIDATE_SELECT: Final = """
select l.id as listing_id, l.source_id, s.source_key, s.country as source_country,
       s.paused as source_paused, l.is_fixture as listing_fixture, l.created_at, l.updated_at,
       l.first_seen_at, l.last_seen_at, l.last_detail_success_at, l.last_availability_check_at,
       l.availability, l.eligibility_state, l.eligibility_profile, l.screening, l.screened_at,
       l.quarantined, l.canonical_url,
       r.id as revision_id, r.revision_number, r.seller_country, r.normalized ->> 'title' as title,
       r.normalized -> 'price' ->> 'negotiable' as negotiable,
       r.normalized -> 'vehicle' ->> 'mileage_claim' as mileage_claim,
       r.make, r.model, r.vehicle_generation, r.asking_minor, r.currency, r.price_basis,
       r.price_type, r.mileage_km, r.registration_year, r.registration_month,
       p.queue_label as profile_queue_label,
       c.id as case_id, c.row_version as case_version, c.state as case_state,
       c.status as case_status, c.profile_key as case_profile, c.queue_label as case_queue_label,
       c.ranking as case_ranking, c.is_fixture as case_fixture, c.updated_at as case_updated_at,
       v.id as valuation_id, v.state as valuation_state, v.is_fixture as valuation_fixture
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  join app.listing_revisions r
    on r.workspace_id = l.workspace_id and r.listing_id = l.id and r.id = l.current_revision_id
  left join app.search_profiles p
    on p.workspace_id = l.workspace_id and p.profile_key = l.eligibility_profile
  left join lateral (
    select c.id, c.row_version, c.profile_key, c.queue_label, c.ranking, c.is_fixture, c.updated_at,
           {case_state} as state, {case_status} as status
      from app.review_cases c
     where c.workspace_id = l.workspace_id and c.listing_id = l.id and c.state <> 'superseded'
     order by coalesce(c.profile_key = l.eligibility_profile, false) desc, c.updated_at desc, c.id
     limit 1) c on true
  left join lateral (
    select v.id, v.state, v.is_fixture, v.created_at
      from app.valuations v
     where v.workspace_id = l.workspace_id and v.listing_id = l.id
       and v.listing_revision_id = l.current_revision_id
     order by v.created_at desc, v.id desc
     limit 1) v on true
 where l.workspace_id = %(ws)s
"""

_LIST_FILTERS: Final = """
   and l.created_at <= %(as_of)s
   and (l.eligibility_state = any(%(candidate_states)s::text[]) or c.id is not null
        or (%(include_rejected)s::boolean and l.eligibility_state = 'rejected'))
   and (%(profile)s::text is null or l.eligibility_profile = %(profile)s::text)
   and (%(country)s::text is null or r.seller_country = %(country)s::text)
   and (%(status)s::text is null or c.status = %(status)s::text)
   and (%(changed_since)s::timestamptz is null
        or greatest(l.updated_at, c.updated_at, v.created_at) >= %(changed_since)s::timestamptz)
   and (%(after_created)s::timestamptz is null
        or (l.created_at, l.id) < (%(after_created)s::timestamptz, %(after_id)s::uuid))
 order by l.created_at desc, l.id desc
 limit %(limit)s
"""

_CASE_EXPRESSIONS: Final = {"case_state": sql.SQL(CASE_STATE_SQL), "case_status": sql.SQL(CASE_STATUS_SQL)}
_LIST_SQL: Final = sql.SQL(_CANDIDATE_SELECT + _LIST_FILTERS).format(**_CASE_EXPRESSIONS)
_ONE_SQL: Final = sql.SQL(_CANDIDATE_SELECT + " and l.id = %(listing_id)s").format(**_CASE_EXPRESSIONS)


# --------------------------------------------------------------------------------------------
# Summary building
# --------------------------------------------------------------------------------------------


def _internal(message: str) -> AppError:
    return AppError(ErrorCode.INTERNAL_ERROR, message, retryable=False)


def _screening(row: Mapping[str, Any]) -> ScreeningResult | None:
    stored = row["screening"]
    if not isinstance(stored, Mapping):
        return None
    try:
        return ScreeningResult.model_validate(stored)
    except ValidationError:
        return None


def _eur_amount(row: Mapping[str, Any], screening: ScreeningResult | None) -> Decimal | None:
    if screening is not None:
        return screening.eur_amount
    return decimal_or_none(json_field(row["screening"], "eur_amount"))


def _partial_date(year: int | None, month: int | None) -> PartialDate:
    if year is None:
        return PartialDate()
    if month is None:
        return PartialDate(value=f"{year:04d}", precision=Precision.YEAR)
    return PartialDate(value=f"{year:04d}-{month:02d}", precision=Precision.MONTH)


def _rank_result(ranking: object) -> RankResult | None:
    if not isinstance(ranking, Mapping) or not ranking:
        return None
    try:
        return RankResult.model_validate(ranking)
    except ValidationError:
        return None


def _rank_summary(ranking: object) -> RankSummary | None:
    result = _rank_result(ranking)
    if result is not None:
        return RankView.of(result).summary()
    total = decimal_or_none(json_field(ranking, "total"))
    version = json_field(ranking, "scoring_version")
    if total is None or not isinstance(version, str) or not version:
        return None
    try:
        return RankSummary(score=format(total, "f"), scoring_version=version[:80])
    except ValidationError:
        return None


def _is_fixture(row: Mapping[str, Any]) -> bool:
    # Fixture lineage is frozen on the listing at ingest (migration 20261007000200): a listing
    # stays synthetic even if its source later switches mode, and a live source's listing never
    # reads as a fixture because of the source's current mode.
    return bool(row["listing_fixture"] or row["case_fixture"] or row["valuation_fixture"])


def _summary(row: Mapping[str, Any], *, now: datetime) -> CandidateSummary:
    screening = _screening(row)
    eligibility = enum_or_none(EligibilityState, row["eligibility_state"])
    eligibility_profile = enum_or_none(ProfileKey, row["eligibility_profile"])
    valuation_state = (
        ValuationState(row["valuation_state"])
        if row["valuation_id"] is not None
        else ValuationState.NOT_STARTED
    )
    queue_label = row["case_queue_label"] or row["profile_queue_label"]
    if queue_label is None and screening is not None:
        queue_label = screening.queue_label
    fx = (
        None if screening is None or screening.fx_rate_used is None else FxRateView.of(screening.fx_rate_used)
    )
    title = row["title"]
    return CandidateSummary(
        listing_id=row["listing_id"],
        revision_id=row["revision_id"],
        revision_number=row["revision_number"],
        source_id=row["source_id"],
        source_key=row["source_key"],
        source_country=row["source_country"],
        seller_country=row["seller_country"],
        title=None if title is None else str(title)[:300],
        make=row["make"],
        model=row["model"],
        generation=row["vehicle_generation"],
        price=PriceSummary(
            payable=AmountView.from_minor(
                row["asking_minor"], row["currency"], unknown_reason="asking price not stated"
            ),
            original_currency=row["currency"],
            eur_equivalent=eur_amount_view(
                _eur_amount(row, screening), reason="EUR equivalent not established"
            ),
            fx_rate=fx,
            basis=enum_or(PriceBasis, row["price_basis"], PriceBasis.UNKNOWN),
            price_type=enum_or(PriceType, row["price_type"], PriceType.UNKNOWN),
            negotiable=enum_or(Tristate, row["negotiable"], Tristate.UNKNOWN),
        ),
        mileage_km=plain_decimal(row["mileage_km"]),
        mileage_claim=enum_or(OdometerClaim, row["mileage_claim"], OdometerClaim.UNKNOWN),
        first_registration=_partial_date(row["registration_year"], row["registration_month"]),
        availability=enum_or(Availability, row["availability"], Availability.UNKNOWN),
        eligibility=eligibility,
        eligibility_profile=eligibility_profile,
        queue_label=None if queue_label is None else str(queue_label)[:120],
        valuation_id=row["valuation_id"],
        valuation_state=valuation_state,
        case_id=row["case_id"],
        review_state=None if row["case_id"] is None else ReviewState(row["case_state"]),
        freshness=FreshnessView.compute(
            now=now,
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            last_detail_success_at=row["last_detail_success_at"],
            last_availability_check_at=row["last_availability_check_at"],
            source_paused=bool(row["source_paused"]),
        ),
        rank=_rank_summary(row["case_ranking"]),
        research_candidate=valuation_state not in FIGURE_STATES,
        quarantined=bool(row["quarantined"]),
        is_fixture=_is_fixture(row),
    )


def _summary_warnings(summary: CandidateSummary) -> list[ResponseWarning]:
    warnings: list[ResponseWarning] = []
    if summary.freshness.stale:
        warnings.append(warning(WarningCode.STALE_DATA))
    if FreshnessFlag.SOURCE_PAUSED in summary.freshness.flags:
        warnings.append(warning(WarningCode.SOURCE_PAUSED))
    if summary.is_fixture:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    if summary.research_candidate and summary.eligibility in _ELIGIBLE:
        warnings.append(warning(WarningCode.RESEARCH_CANDIDATE))
    if summary.rank is not None:
        warnings.append(warning(WarningCode.SCORE_NOT_PROBABILITY))
    return warnings


# --------------------------------------------------------------------------------------------
# deals_list_candidates
# --------------------------------------------------------------------------------------------


async def list_candidates(
    conn: Conn,
    actor: ActorContext,
    query: DealsListCandidatesInput,
    *,
    secret: CursorSecret,
    include_screening_rejected: bool = False,
) -> QueryResult[CandidateListView]:
    """One keyset page of candidate summaries plus the signed cursor for the next page.

    ``include_screening_rejected`` (the dashboard's audit filter; never the MCP tool) also lists
    listings that screening rejected (``eligibility_state = 'rejected'``), which are kept for audit
    but are not candidates (spec 11). The flag is bound into the cursor's filter hash, so a cursor
    issued for one setting is refused (``mismatch``) for the other; with the default ``False`` the
    hash is exactly the spec 21 filter hash (existing cursors stay valid).
    """
    actor.require(Scope.DEALS_READ)
    if not isinstance(include_screening_rejected, bool):
        raise ValidationFailed(
            "include_screening_rejected must be a boolean", details={"fields": ["include_screening_rejected"]}
        )
    keys = require_secret(secret)
    limit = validate_limit(query.limit)
    filters = query.filters()
    if include_screening_rejected:
        filters = {**filters, "include_screening_rejected": True}
    filters_hash = filter_hash(filters)
    now = await db_now(conn)
    after_created: datetime | None = None
    after_id: UUID | None = None
    as_of = now
    if query.cursor is not None:
        position = decode_keyset(
            query.cursor,
            actor,
            query=CANDIDATES_QUERY,
            filters_hash=filters_hash,
            now=now,
            secret=keys,
            parsers=(parse_datetime, parse_uuid),
        )
        after_created, after_id = position.sort
        as_of = position.as_of
    params = {
        "ws": actor.workspace_id,
        "now": now,
        "as_of": as_of,
        "candidate_states": list(CANDIDATE_STATES),
        "include_rejected": include_screening_rejected,
        "profile": None if query.profile is None else query.profile.value,
        "country": query.country,
        "status": query.status,
        "changed_since": query.changed_since,
        "after_created": after_created,
        "after_id": after_id,
        "limit": limit + 1,
    }
    async with mapped_errors():
        rows = await fetch_all(conn, _LIST_SQL, params)
    page = rows[:limit]
    with rendering("candidate"):
        items = tuple(_summary(row, now=now) for row in page)
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = encode_keyset(
            actor,
            query=CANDIDATES_QUERY,
            filters_hash=filters_hash,
            last_sort_key=(ensure_utc(last["created_at"]), last["listing_id"]),
            as_of=as_of,
            now=now,
            secret=keys,
        )
    warnings: list[ResponseWarning] = []
    for item in items:
        warnings.extend(_summary_warnings(item))
    return QueryResult(
        data=CandidateListView(items=items), as_of=now, warnings=tuple(warnings), next_cursor=next_cursor
    )


# --------------------------------------------------------------------------------------------
# deals_get_candidate
# --------------------------------------------------------------------------------------------

# Promoted history only: quarantined revisions (parser unhealthy) are evidence, not facts, and a
# revision newer than the current one (unpromoted, or promoted after the first read) is excluded.
_REVISIONS_SQL: Final = """
select r.id, r.revision_number, r.observed_at, r.availability, r.normalized -> 'price' as price
  from app.listing_revisions r
 where r.workspace_id = %(ws)s and r.listing_id = %(listing_id)s
   and not r.quarantined and r.revision_number <= %(current)s
 order by r.revision_number desc
 limit %(limit)s
"""
_REVISION_SQL: Final = """
select r.id, r.revision_number, r.observed_at, r.semantic_hash, r.parser_version, r.normalized
  from app.listing_revisions r
 where r.workspace_id = %(ws)s and r.listing_id = %(listing_id)s and r.revision_number = %(number)s
   and not r.quarantined and r.revision_number <= %(current)s
"""
_EVIDENCE_SQL: Final = """
select distinct on (e.field_path) e.field_path, e.id, e.claim_status
  from app.field_evidence e
 where e.workspace_id = %(ws)s and e.listing_id = %(listing_id)s and e.revision_id = %(revision_id)s
   and not exists (select 1 from app.field_evidence n
                    where n.workspace_id = e.workspace_id and n.supersedes_id = e.id)
 order by e.field_path, e.created_at desc, e.id desc
"""
_AVAILABILITY_EVENTS_SQL: Final = """
select a.occurred_at, a.metadata
  from ops.audit_events a
 where a.workspace_id = %(ws)s and a.target_type = 'listing' and a.target_id = %(listing_id)s
   and a.action = 'listing.availability'
 order by a.occurred_at desc, a.id desc
 limit %(limit)s
"""
_VALUATION_REF_SQL: Final = """
select v.id, v.state, v.is_fixture, v.created_at, v.expires_at, v.dependency_fingerprint, v.currency,
       v.base_contribution_minor, v.conservative_contribution_minor
  from app.valuations v
 where v.workspace_id = %(ws)s and v.listing_id = %(listing_id)s and v.listing_revision_id = %(revision_id)s
 order by v.created_at desc, v.id desc
 limit 1
"""
_COMPARABLE_REF_SQL: Final = """
select cs.id, cs.sample_quality, cs.sample_size, cs.criteria_version, cs.computed_at, cs.created_at,
       cs.criteria -> 'result' ->> 'mk_band_fit' as mk_band_fit,
       cs.criteria -> 'result' ->> 'research_needed' as research_needed
  from app.comparable_sets cs
 where cs.workspace_id = %(ws)s and cs.listing_id = %(listing_id)s and cs.target_revision_id = %(revision_id)s
 order by cs.created_at desc, cs.id desc
 limit 1
"""


def _normalized(document: object) -> NormalizedListing:
    try:
        return NormalizedListing.model_validate(document)
    except ValidationError:
        raise _internal("The stored listing revision could not be read") from None


def _price_info(document: object) -> PriceInfo:
    try:
        return PriceInfo.model_validate(document if isinstance(document, Mapping) else {})
    except ValidationError:
        return PriceInfo()


def _provenance(
    listing: NormalizedListing, evidence: Mapping[str, Mapping[str, Any]]
) -> tuple[FieldProvenanceView, ...]:
    views = []
    for path in sorted(listing.provenance)[:MAX_PROVENANCE]:
        record = evidence.get(path)
        views.append(
            FieldProvenanceView.of(
                path[:200],
                listing.provenance[path],
                evidence_id=None if record is None else record["id"],
                claim_status=None if record is None else enum_or_none(ClaimStatus, record["claim_status"]),
            )
        )
    return tuple(views)


def _event_time(observed: object, recorded: datetime) -> datetime:
    """The evidence time an availability event records (``metadata.observed_at``), else the time
    the event was written. Several events written in one transaction share ``occurred_at``."""
    if isinstance(observed, str):
        try:
            return parse_datetime(observed)
        except ValueError:
            pass
    return ensure_utc(recorded)


def _availability_history(
    revisions: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> tuple[AvailabilityPoint, ...]:
    points: list[AvailabilityPoint] = [
        AvailabilityPoint(
            observed_at=ensure_utc(r["observed_at"]),
            availability=enum_or(Availability, r["availability"], Availability.UNKNOWN),
            observed_via="detail",
            revision_number=r["revision_number"],
        )
        for r in revisions
    ]
    for event in events:
        metadata = event["metadata"] if isinstance(event["metadata"], Mapping) else {}
        new = enum_or_none(Availability, metadata.get("new"))
        if new is None:
            continue
        kind = enum_or(
            AvailabilityEvidenceKind, metadata.get("evidence_kind"), AvailabilityEvidenceKind.MANUAL
        )
        points.append(
            AvailabilityPoint(
                observed_at=_event_time(metadata.get("observed_at"), event["occurred_at"]),
                availability=new,
                observed_via=_EVIDENCE_VIA[kind],
                revision_number=None,
            )
        )
    points.sort(key=lambda p: (p.observed_at, p.revision_number or 0))
    return tuple(points[-MAX_HISTORY:])


def _valuation_ref(row: Mapping[str, Any] | None) -> ValuationRef | None:
    if row is None:
        return None
    state = ValuationState(row["state"])
    reason = f"valuation is {state.value}; unknown figures are never shown as zero"
    figures = state in FIGURE_STATES or state == ValuationState.STALE
    currency = row["currency"]
    return ValuationRef(
        valuation_id=row["id"],
        state=state,
        research_candidate=state not in FIGURE_STATES,
        is_fixture=row["is_fixture"],
        created_at=ensure_utc(row["created_at"]),
        expires_at=utc_or_none(row["expires_at"]),
        dependency_fingerprint=row["dependency_fingerprint"],
        conservative_contribution=AmountView.from_minor(
            row["conservative_contribution_minor"] if figures else None, currency, unknown_reason=reason
        ),
        base_contribution=AmountView.from_minor(
            row["base_contribution_minor"] if figures else None, currency, unknown_reason=reason
        ),
    )


def _comparable_ref(row: Mapping[str, Any] | None) -> ComparableSetRef | None:
    if row is None:
        return None
    quality = row["sample_quality"]
    fit = row["mk_band_fit"] if row["mk_band_fit"] in ("below", "within", "above", "unknown") else "unknown"
    research = row["research_needed"]
    return ComparableSetRef.model_validate(
        {
            "comparable_set_id": row["id"],
            "sample_quality": quality,
            "sample_size": row["sample_size"],
            "mk_band_fit": fit,
            "criteria_version": str(row["criteria_version"])[:80],
            "as_of": ensure_utc(row["computed_at"] or row["created_at"]),
            "research_needed": research == "true" if research in ("true", "false") else quality != "adequate",
        }
    )


def _case_ref(row: Mapping[str, Any]) -> ReviewCaseRef | None:
    if row["case_id"] is None:
        return None
    return ReviewCaseRef(
        case_id=row["case_id"],
        case_version=row["case_version"],
        state=ReviewState(row["case_state"]),
        profile=ProfileKey(row["case_profile"]),
        queue_label=str(row["case_queue_label"])[:120],
    )


def _detail_warnings(detail: CandidateDetail, *, requested_is_current: bool) -> list[ResponseWarning]:
    warnings = _summary_warnings(detail.summary)
    if not requested_is_current:
        warnings.append(warning(WarningCode.REVISION_NOT_CURRENT))
    warnings.append(warning(WarningCode.SELLER_CLAIMS_UNVERIFIED))
    valuation = detail.latest_valuation
    if valuation is not None:
        if valuation.state == ValuationState.STALE:
            warnings.append(warning(WarningCode.VALUATION_STALE))
        elif valuation.state not in FIGURE_STATES:
            warnings.append(warning(WarningCode.VALUATION_INCOMPLETE))
        if valuation.is_fixture:
            warnings.append(warning(WarningCode.FIXTURE_DATA))
    comparable = detail.comparable_set
    if comparable is not None:
        if comparable.sample_quality == "insufficient":
            warnings.append(warning(WarningCode.INSUFFICIENT_COMPARABLES))
        elif comparable.sample_quality == "small":
            warnings.append(warning(WarningCode.SMALL_COMPARABLE_SAMPLE))
        warnings.append(warning(WarningCode.ASKING_NOT_SALE))
    return warnings


async def get_candidate(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    revision: int | None = None,
) -> QueryResult[CandidateDetail]:
    """One candidate at its exact current (or requested) revision number (`NotFound` for missing
    and foreign-workspace listings alike)."""
    actor.require(Scope.DEALS_READ)
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise ValidationFailed("revision must be a positive integer", details={"fields": ["revision"]})
    ws = actor.workspace_id
    now = await db_now(conn)
    async with mapped_errors():
        row = await fetch_one(conn, _ONE_SQL, {"ws": ws, "listing_id": listing_id, "now": now})
        if row is None:
            raise NotFound("Candidate not found")
        current = row["revision_number"]
        number = revision if revision is not None else current
        stored = await fetch_one(
            conn, _REVISION_SQL, {"ws": ws, "listing_id": listing_id, "number": number, "current": current}
        )
        if stored is None:
            raise NotFound("Listing revision not found")
        ids = {"ws": ws, "listing_id": listing_id, "revision_id": stored["id"]}
        history = await fetch_all(conn, _REVISIONS_SQL, {**ids, "current": current, "limit": MAX_HISTORY})
        evidence_rows = await fetch_all(conn, _EVIDENCE_SQL, ids)
        events = await fetch_all(conn, _AVAILABILITY_EVENTS_SQL, {**ids, "limit": MAX_HISTORY})
        valuation_row = await fetch_one(conn, _VALUATION_REF_SQL, ids)
        comparable_row = await fetch_one(conn, _COMPARABLE_REF_SQL, ids)
    notes = await notes_repo.list_notes(conn, actor, listing_id, limit=MAX_NOTES)
    listing = _normalized(stored["normalized"])
    source_url = safe_http_url(row["canonical_url"])
    if source_url is None:
        raise _internal("The stored listing URL is not a safe link")
    screening = _screening(row)
    valuation_ref = _valuation_ref(valuation_row)
    comparable_ref = _comparable_ref(comparable_row)
    rank = _rank_result(row["case_ranking"])
    history_sorted = sorted(history, key=lambda r: r["revision_number"])
    with rendering("candidate"):
        detail = CandidateDetail(
            summary=_summary(row, now=now),
            revision=RevisionView(
                revision_id=stored["id"],
                revision_number=stored["revision_number"],
                current_revision_number=current,
                is_current=stored["revision_number"] == current,
                observed_at=ensure_utc(stored["observed_at"]),
                semantic_hash=stored["semantic_hash"],
                parser_version=str(stored["parser_version"])[:120],
            ),
            normalized=NormalizedFieldsView.of(listing),
            seller_text=SellerTextView(title=listing.title, description_excerpt=listing.description_excerpt),
            field_provenance=_provenance(listing, {r["field_path"]: r for r in evidence_rows}),
            conflicts=listing.conflicts[:MAX_CONFLICTS],
            availability_history=_availability_history(history_sorted, events),
            price_history=price_history(
                [
                    (r["revision_number"], ensure_utc(r["observed_at"]), _price_info(r["price"]))
                    for r in history_sorted
                ]
            ),
            screening=None
            if screening is None
            else ScreeningView.of(screening, screened_at=utc_or_none(row["screened_at"])),
            latest_valuation=valuation_ref,
            comparable_set=comparable_ref,
            review_case=_case_ref(row),
            due_diligence=build_checklist(
                listing,
                None if valuation_ref is None else valuation_ref.state,
                None if comparable_ref is None else _DB_QUALITY_TO_STATUS.get(comparable_ref.sample_quality),
            ),
            notes=tuple(notes),
            rank=None if rank is None else RankView.of(rank),
            source_link=SourceLink(url=source_url, source_key=row["source_key"]),
        )
    warnings = _detail_warnings(detail, requested_is_current=detail.revision.is_current)
    return QueryResult(data=detail, as_of=now, warnings=tuple(warnings))


__all__ = [
    "AVAILABILITY_HISTORY_SOURCE",
    "CANDIDATES_QUERY",
    "CANDIDATE_STATES",
    "get_candidate",
    "list_candidates",
]
