"""Review cases, claims, decisions and the pending queue (spec sections 14, 18, 21, 22).

Tables: ``app.review_cases``, ``app.review_decisions`` (append-only), ``ops.outbox``,
``ops.idempotency_records``, ``ops.query_snapshots``, ``ops.audit_events``. Every function runs
inside the caller's transaction (``transactions.unit_of_work``); nothing here performs network
I/O. Lock order (docs/schema.md section 4): idempotency record -> ``app.listings`` ->
``app.valuations`` -> ``app.review_cases`` -> ``ops.outbox`` -> audit (insert-only, last).

- **Cases** (`upsert_review_case`): a qualifying revision creates the pending case of its
  (listing, profile) or updates the open one. A NEWER revision is new material information:
  ``domain.reviews.apply_new_revision`` creates a new case version, clears the stale valuation
  (or attaches the new revision's valuation) and returns decided cases to pending; an active
  claim stays but every submission against the old version/revision now fails with
  ``VERSION_CONFLICT``. A revision that no longer qualifies supersedes the open case
  (``mark_superseded``). Whenever the resulting case version is ``pending`` because of new
  information, the ``review.pending`` outbox event of exactly that version is written in the
  SAME transaction (``build_review_pending_event``; dedup key
  ``review.pending:<case_id>:<case_version>``; integer ``event_version`` 1 while the payload
  keeps ``schema_version`` "1.0"; fixture cases produce blocked fixture events).
- **Claims** (`claim`, `release`, `expire_claims`): ``domain.reviews`` decides; expiry uses
  database time (``clock_timestamp()``); the random token is returned once and only its SHA-256
  is stored. Release only affects the caller's current claim and is idempotent.
- **Submit** (`submit`): ONE transaction: idempotency record (hash of the validated request),
  listing lock, case lock, ``evaluate_submit`` (claim ownership, expiry by database time,
  expected version, listing revision, valuation applicability, shortlist guard), the immutable
  decision row (actor from the authenticated context only), the case update (state,
  ``row_version``, ``latest_decision_id``, claim cleared), an authorized owner-alert event when
  the materiality policy allows it, the audit row and the idempotency completion. A timeout
  retry with the same key replays the stored decision; a second decision for the same case
  version is impossible (unique ``(case_id, case_version)`` and the cleared claim).
- **Queue** (`list_pending_queue`): pages come from a frozen ``ops.query_snapshots``
  projection bound to principal, workspace and filter hash; claim/submit always revalidate.

Failures are not recorded in ``ops.idempotency_records``: a failing request rolls back
entirely (including its in-progress record), so a retry re-evaluates the current state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    Availability,
    EligibilityState,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    ProfileKey,
    ReviewOutcome,
    ReviewState,
    Scope,
    Tristate,
    ValuationState,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.listings import PartialDate
from suv_deals.domain.money import Money
from suv_deals.domain.notifications import (
    AlertState,
    MaterialityPolicy,
    Priority,
    build_review_pending_event,
    dashboard_case_url,
    derive_readiness,
    evaluate_materiality,
)
from suv_deals.domain.pagination import DEFAULT_CURSOR_TTL, filter_hash
from suv_deals.domain.profiles import SearchProfile
from suv_deals.domain.ranking import RankResult
from suv_deals.domain.reviews import (
    DEFAULT_CLAIM_DURATION,
    CaseUpdate,
    ReviewCaseSnapshot,
    SubmitDecision,
    SubmitGuard,
    SubmitRequest,
    apply_new_revision,
    evaluate_claim,
    evaluate_release,
    evaluate_submit,
    expire_claim,
    mark_superseded,
    outcome_state,
)
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import audit, bindings_repo, idempotency, outbox, query_snapshots
from suv_deals.persistence.database import Conn, db_now, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.views.candidates import (
    DEFAULT_DETAIL_MAX_AGE,
    CandidateSummary,
    FreshnessView,
    PriceSummary,
    RankSummary,
    ValuationRef,
)
from suv_deals.views.common import AmountView
from suv_deals.views.reviews import (
    ClaimResult,
    ClaimStateView,
    DecisionActorView,
    ReleaseResultView,
    ReviewCaseView,
    ReviewDecisionView,
    ReviewQueueItem,
    ReviewQueuePage,
)

QUEUE_QUERY_NAME: Final = "reviews_list_pending"
PENDING_EVENT_VERSION: Final = 1
SHORTLIST_EVENT_TYPE: Final = "review.shortlisted"
SHORTLIST_EVENT_VERSION: Final = 1
SHORTLIST_SUMMARY: Final = (
    "Shortlisted research candidate after review; open the dashboard for evidence, unknown costs and risks."
)
MAX_QUEUE_SIZE: Final = 10_000
_FROZEN = ConfigDict(frozen=True, extra="forbid")
_ELIGIBLE: Final = frozenset({EligibilityState.ELIGIBLE_PRIMARY, EligibilityState.ELIGIBLE_MANUAL_PROFILE})
_UNUSABLE_VALUATION: Final = frozenset({ValuationState.STALE, ValuationState.INVALID})
_FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})
_COMPARABLE_STATUS: Final[dict[str, str]] = {
    "adequate": "adequate",
    "small": "small_sample",
    "insufficient_comparables": "insufficient_comparables",
}

UpsertAction = Literal["created", "updated", "unchanged", "superseded", "not_qualifying", "stale_revision"]


# =============================================================================================
# Shared case loading
# =============================================================================================


@dataclass(frozen=True, slots=True)
class _CaseRow:
    snapshot: ReviewCaseSnapshot
    queue_label: str
    readiness: str
    priority: int
    ranking: dict[str, Any]
    ranking_version: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime

    def values(self) -> dict[str, Any]:
        """Every mutable column of the case as currently stored."""
        s = self.snapshot
        return {
            "state": s.state.value,
            "row_version": s.row_version,
            "revision_id": s.revision_id,
            "valuation_id": s.valuation_id,
            "readiness": self.readiness,
            "priority": self.priority,
            "ranking": self.ranking,
            "ranking_version": self.ranking_version,
            "claim_holder": s.claim_holder,
            "claim_token_hash": s.claim_token_hash,
            "claimed_at": s.claimed_at,
            "claim_expires_at": s.claim_expires_at,
            "latest_decision_id": s.latest_decision_id,
            "reason": self.reason,
            "superseded_by_id": s.superseded_by_id,
        }


_CASE_SELECT: Final = """
select c.id as case_id, c.workspace_id, c.listing_id, c.profile_key, c.state, c.row_version,
       c.revision_id, r.revision_number as listing_revision, c.valuation_id, v.state as valuation_state,
       c.claim_holder, c.claim_token_hash, c.claimed_at, c.claim_expires_at, c.latest_decision_id,
       d.outcome as latest_decision_outcome, dr.revision_number as latest_decision_listing_revision,
       c.superseded_by_id, c.is_fixture, c.queue_label, c.readiness, c.priority, c.ranking,
       c.ranking_version, c.reason, c.created_at, c.updated_at
  from app.review_cases c
  join app.listing_revisions r on r.workspace_id = c.workspace_id and r.id = c.revision_id
  left join app.valuations v on v.workspace_id = c.workspace_id and v.id = c.valuation_id
  left join app.review_decisions d on d.workspace_id = c.workspace_id and d.id = c.latest_decision_id
  left join app.listing_revisions dr on dr.workspace_id = c.workspace_id and dr.id = d.listing_revision_id
 where c.workspace_id = %(ws)s
"""
_SNAPSHOT_FIELDS: Final = (
    "case_id",
    "workspace_id",
    "listing_id",
    "profile_key",
    "state",
    "row_version",
    "revision_id",
    "listing_revision",
    "valuation_id",
    "valuation_state",
    "claim_holder",
    "claim_token_hash",
    "claimed_at",
    "claim_expires_at",
    "latest_decision_id",
    "latest_decision_outcome",
    "latest_decision_listing_revision",
    "superseded_by_id",
    "is_fixture",
)


def _case_row(row: Mapping[str, Any]) -> _CaseRow:
    try:
        snapshot = ReviewCaseSnapshot.model_validate({k: row[k] for k in _SNAPSHOT_FIELDS})
    except ValidationError as exc:
        raise ValidationFailed("stored review case is inconsistent") from exc
    return _CaseRow(
        snapshot=snapshot,
        queue_label=row["queue_label"],
        readiness=row["readiness"],
        priority=row["priority"],
        ranking=dict(row["ranking"] or {}),
        ranking_version=row["ranking_version"],
        reason=row["reason"],
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
    )


async def _load_case(conn: Conn, actor: ActorContext, case_id: UUID, *, lock: bool) -> _CaseRow:
    query = _CASE_SELECT + " and c.id = %(id)s" + (" for update of c" if lock else "")
    async with mapped_errors():
        row = await fetch_one(conn, query, {"ws": actor.workspace_id, "id": case_id})
    if row is None:
        raise NotFound("Review case not found")  # missing and foreign are indistinguishable
    return _case_row(row)


_UPDATE_CASE_SQL: Final = """
update app.review_cases set
  state = %(state)s, row_version = %(row_version)s, revision_id = %(revision_id)s,
  valuation_id = %(valuation_id)s, readiness = %(readiness)s, priority = %(priority)s,
  ranking = %(ranking)s, ranking_version = %(ranking_version)s, claim_holder = %(claim_holder)s,
  claim_token_hash = %(claim_token_hash)s, claimed_at = %(claimed_at)s,
  claim_expires_at = %(claim_expires_at)s, latest_decision_id = %(latest_decision_id)s,
  reason = %(reason)s, superseded_by_id = %(superseded_by_id)s
where workspace_id = %(ws)s and id = %(id)s and row_version = %(expected)s
returning updated_at
"""

_NO_CLAIM: Final[dict[str, Any]] = {
    "claim_holder": None,
    "claim_token_hash": None,
    "claimed_at": None,
    "claim_expires_at": None,
}


async def _write_case(conn: Conn, actor: ActorContext, row: _CaseRow, changes: Mapping[str, Any]) -> None:
    values = {**row.values(), **changes}
    params = {
        **values,
        "ranking": Jsonb(values["ranking"]),
        "ws": actor.workspace_id,
        "id": row.snapshot.case_id,
        "expected": row.snapshot.row_version,
    }
    async with mapped_errors():
        updated = await fetch_one(conn, _UPDATE_CASE_SQL, params)
    if updated is None:
        raise VersionConflict("The review case changed concurrently; reload and retry")


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed", retryable=False)


async def begin_idempotent(
    conn: Conn, actor: ActorContext, operation: str, key: str, request_hash: str
) -> dict[str, Any] | None:
    started = await idempotency.begin(conn, actor, operation, key, request_hash)
    if isinstance(started, idempotency.Replay):
        return started.result
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict("The same request is still in progress; retry shortly")
    return None


# =============================================================================================
# Case creation / update (system workers)
# =============================================================================================


class CaseUpsertResult(BaseModel):
    model_config = _FROZEN

    action: UpsertAction
    case_id: UUID | None
    case_version: int | None
    state: ReviewState | None
    readiness: str | None
    event_id: UUID | None = None
    event_created: bool = False


def _require_system_or_owner(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Review cases are created by system workers (or the owner)")


def _readiness(valuation_state: ValuationState | None, document: Any) -> str:
    unknown: list[str] = []
    comparable_status: str | None = None
    body = document.get("valuation") if isinstance(document, Mapping) else None
    if isinstance(body, Mapping):
        scenarios = body.get("scenarios")
        if isinstance(scenarios, Mapping):
            unknown = [
                str(u.get("item")) for u in scenarios.get("unknown_lines") or () if isinstance(u, Mapping)
            ]
        comparable = body.get("comparable")
        if isinstance(comparable, Mapping):
            comparable_status = _COMPARABLE_STATUS.get(str(comparable.get("quality")))
    return derive_readiness(
        valuation_state, unknown_cost_categories=unknown, comparable_status=comparable_status
    )


def _priority(rank: RankResult | None) -> tuple[int, dict[str, Any], str | None]:
    if rank is None:
        return 0, {}, None
    return max(-100_000, min(100_000, rank.priority)), rank.model_dump(mode="json"), rank.scoring_version


async def _emit_pending(
    conn: Conn,
    actor: ActorContext,
    snapshot: ReviewCaseSnapshot,
    *,
    readiness: str,
    dashboard_base_url: str,
    priority: Priority,
) -> tuple[UUID, bool]:
    now = ensure_utc(await db_now(conn))
    draft = build_review_pending_event(
        snapshot,
        dashboard_base_url=dashboard_base_url,
        event_id=uuid4(),
        occurred_at=now,
        readiness=readiness,
        priority=priority,
        queue=snapshot.profile_key.value,
    )
    route = (
        None
        if draft.is_fixture
        else await bindings_repo.route_for(conn, actor.workspace_id, "candidate_discovery")
    )
    return await outbox.enqueue_event(
        conn,
        actor,
        event_type=draft.event_type,
        event_version=PENDING_EVENT_VERSION,
        aggregate_type=draft.aggregate_type,
        aggregate_id=draft.aggregate_id,
        aggregate_version=draft.aggregate_version,
        payload=draft.payload,
        dedup_key=draft.dedup_key,
        destination_binding_id=None if route is None else route.binding_id,
        is_fixture=draft.is_fixture,
        event_id=draft.event_id,
    )


_LISTING_FOR_CASE_SQL: Final = """
select l.id, l.current_revision_id, cr.revision_number as current_revision_number, s.mode as source_mode
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join app.listing_revisions cr on cr.workspace_id = l.workspace_id and cr.id = l.current_revision_id
 where l.workspace_id = %(ws)s and l.id = %(listing_id)s
 for no key update of l
"""


async def upsert_review_case(  # noqa: PLR0917 - positional contract of the work package
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    revision_id: UUID,
    screening: ScreeningResult,
    valuation_id: UUID | None,
    profile: SearchProfile,
    *,
    dashboard_base_url: str,
    rank: RankResult | None = None,
    event_priority: Priority = "normal",
) -> CaseUpsertResult:
    """Create or update the pending case of (listing, profile) for a screened revision."""
    _require_system_or_owner(actor)
    ws = actor.workspace_id
    async with mapped_errors():
        listing = await fetch_one(conn, _LISTING_FOR_CASE_SQL, {"ws": ws, "listing_id": listing_id})
        if listing is None:
            raise NotFound("Listing not found")
        revision = await fetch_one(
            conn,
            "select id, revision_number from app.listing_revisions"
            " where workspace_id = %(ws)s and listing_id = %(listing_id)s and id = %(id)s",
            {"ws": ws, "listing_id": listing_id, "id": revision_id},
        )
        if revision is None:
            raise NotFound("Listing revision not found")
        number = int(revision["revision_number"])
        current_number = listing["current_revision_number"]
        if current_number is not None and number < int(current_number):
            return CaseUpsertResult(
                action="stale_revision", case_id=None, case_version=None, state=None, readiness=None
            )
        valuation_state: ValuationState | None = None
        valuation_fixture = False
        document: Any = None
        if valuation_id is not None:
            valuation = await fetch_one(
                conn,
                "select id, listing_id, listing_revision_id, state, is_fixture, scenarios from app.valuations"
                " where workspace_id = %(ws)s and id = %(id)s for share",
                {"ws": ws, "id": valuation_id},
            )
            if valuation is None or valuation["listing_id"] != listing_id:
                raise NotFound("Valuation not found")
            if valuation["listing_revision_id"] != revision_id:
                raise ValidationFailed("the valuation belongs to another revision of the listing")
            valuation_state = ValuationState(valuation["state"])
            valuation_fixture = bool(valuation["is_fixture"])
            document = valuation["scenarios"]
        is_fixture = listing["source_mode"] == "fixture" or valuation_fixture
        readiness = _readiness(valuation_state, document)
        priority, ranking, ranking_version = _priority(rank)
        qualifies = profile.enabled and screening.state in _ELIGIBLE and screening.profile == profile.key
        open_row = await fetch_one(
            conn,
            _CASE_SELECT + " and c.listing_id = %(listing_id)s and c.profile_key = %(profile)s"
            " and c.state <> 'superseded' for update of c",
            {"ws": ws, "listing_id": listing_id, "profile": profile.key.value},
        )
    if open_row is None:
        if not qualifies:
            return CaseUpsertResult(
                action="not_qualifying", case_id=None, case_version=None, state=None, readiness=None
            )
        return await _create_case(
            conn,
            actor,
            listing_id=listing_id,
            revision_id=revision_id,
            revision_number=number,
            valuation_id=valuation_id,
            valuation_state=valuation_state,
            profile=profile,
            readiness=readiness,
            priority=priority,
            ranking=ranking,
            ranking_version=ranking_version,
            is_fixture=is_fixture,
            dashboard_base_url=dashboard_base_url,
            event_priority=event_priority,
        )
    row = _case_row(open_row)
    case = row.snapshot
    if case.is_fixture != is_fixture:
        raise ValidationFailed("the fixture status of a review case cannot change; supersede it instead")
    if not qualifies:
        update = mark_superseded(
            case,
            superseded_by_id=None,
            reason=f"revision {number} no longer qualifies for {profile.key.value} ({screening.state.value})",
        )
        await _apply_update(conn, actor, row, update, readiness=row.readiness)
        await _audit_upsert(conn, actor, case, case.row_version, update.row_version, action="superseded")
        return CaseUpsertResult(
            action="superseded",
            case_id=case.case_id,
            case_version=update.row_version,
            state=ReviewState.SUPERSEDED,
            readiness=row.readiness,
        )
    if number < case.listing_revision:
        return CaseUpsertResult(
            action="stale_revision",
            case_id=case.case_id,
            case_version=case.row_version,
            state=case.state,
            readiness=row.readiness,
        )
    if number > case.listing_revision:
        update = apply_new_revision(
            case, revision_id=revision_id, listing_revision=number, reason=f"new material revision {number}"
        )
        new_snapshot = await _apply_update(
            conn,
            actor,
            row,
            update,
            readiness=readiness,
            valuation_id=valuation_id,
            valuation_state=valuation_state,
            priority=priority,
            ranking=ranking,
            ranking_version=ranking_version,
        )
    elif valuation_id != case.valuation_id or readiness != row.readiness:
        # Same revision, new valuation (e.g. recomputed after invalidation): a new case version.
        await _write_case(
            conn,
            actor,
            row,
            {
                "row_version": case.row_version + 1,
                "valuation_id": valuation_id,
                "readiness": readiness,
                "priority": priority,
                "ranking": ranking,
                "ranking_version": ranking_version,
                "reason": "valuation updated",
            },
        )
        new_snapshot = case.model_copy(
            update={
                "row_version": case.row_version + 1,
                "valuation_id": valuation_id,
                "valuation_state": valuation_state,
            }
        )
    else:
        if (priority, ranking_version) != (row.priority, row.ranking_version) or ranking != row.ranking:
            # Reprioritisation only: not material, no new case version, no new event.
            async with mapped_errors():
                await conn.execute(
                    "update app.review_cases set priority = %(priority)s, ranking = %(ranking)s,"
                    " ranking_version = %(ranking_version)s where workspace_id = %(ws)s and id = %(id)s",
                    {
                        "ws": actor.workspace_id,
                        "id": case.case_id,
                        "priority": priority,
                        "ranking": Jsonb(ranking),
                        "ranking_version": ranking_version,
                    },
                )
        return CaseUpsertResult(
            action="unchanged",
            case_id=case.case_id,
            case_version=case.row_version,
            state=case.state,
            readiness=row.readiness,
        )
    event_id: UUID | None = None
    created = False
    if new_snapshot.state == ReviewState.PENDING:
        event_id, created = await _emit_pending(
            conn,
            actor,
            new_snapshot,
            readiness=readiness,
            dashboard_base_url=dashboard_base_url,
            priority=event_priority,
        )
    await _audit_upsert(conn, actor, case, case.row_version, new_snapshot.row_version, action="updated")
    return CaseUpsertResult(
        action="updated",
        case_id=case.case_id,
        case_version=new_snapshot.row_version,
        state=new_snapshot.state,
        readiness=readiness,
        event_id=event_id,
        event_created=created,
    )


async def _apply_update(
    conn: Conn,
    actor: ActorContext,
    row: _CaseRow,
    update: CaseUpdate,
    *,
    readiness: str,
    valuation_id: UUID | None = None,
    valuation_state: ValuationState | None = None,
    priority: int | None = None,
    ranking: dict[str, Any] | None = None,
    ranking_version: str | None = None,
) -> ReviewCaseSnapshot:
    case = row.snapshot
    superseding = update.state == ReviewState.SUPERSEDED
    new_valuation = update.valuation_id if superseding else valuation_id
    new_valuation_state = update.valuation_state if superseding else valuation_state
    changes: dict[str, Any] = {
        "state": update.state.value,
        "row_version": update.row_version,
        "revision_id": update.revision_id,
        "valuation_id": new_valuation,
        "readiness": readiness,
        "reason": update.reason,
        "superseded_by_id": update.superseded_by_id,
    }
    if priority is not None:
        changes.update(priority=priority, ranking=ranking or {}, ranking_version=ranking_version)
    if update.clear_claim:
        changes.update(_NO_CLAIM)
    await _write_case(conn, actor, row, changes)
    keep_claim = not update.clear_claim and update.state == ReviewState.CLAIMED
    return case.model_copy(
        update={
            "state": update.state,
            "row_version": update.row_version,
            "revision_id": update.revision_id,
            "listing_revision": update.listing_revision,
            "valuation_id": new_valuation,
            "valuation_state": new_valuation_state,
            "superseded_by_id": update.superseded_by_id,
            **({} if keep_claim else {k: None for k in _NO_CLAIM}),
        }
    )


async def _create_case(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID,
    revision_id: UUID,
    revision_number: int,
    valuation_id: UUID | None,
    valuation_state: ValuationState | None,
    profile: SearchProfile,
    readiness: str,
    priority: int,
    ranking: dict[str, Any],
    ranking_version: str | None,
    is_fixture: bool,
    dashboard_base_url: str,
    event_priority: Priority,
) -> CaseUpsertResult:
    async with mapped_errors(
        unique={"review_cases_open_uidx": lambda: TransientConflict("A case was created concurrently; retry")}
    ):
        created = await fetch_one(
            conn,
            "insert into app.review_cases (workspace_id, listing_id, revision_id, valuation_id, profile_key,"
            " queue_label, state, readiness, priority, ranking, ranking_version, row_version, reason,"
            " is_fixture) values (%(ws)s, %(listing_id)s, %(revision_id)s, %(valuation_id)s, %(profile)s,"
            " %(queue_label)s, 'pending', %(readiness)s, %(priority)s, %(ranking)s, %(ranking_version)s, 1,"
            " %(reason)s, %(is_fixture)s) returning id",
            {
                "ws": actor.workspace_id,
                "listing_id": listing_id,
                "revision_id": revision_id,
                "valuation_id": valuation_id,
                "profile": profile.key.value,
                "queue_label": profile.queue_label,
                "readiness": readiness,
                "priority": priority,
                "ranking": Jsonb(ranking),
                "ranking_version": ranking_version,
                "reason": f"qualifying revision {revision_number}",
                "is_fixture": is_fixture,
            },
        )
    assert created is not None
    snapshot = ReviewCaseSnapshot(
        case_id=created["id"],
        workspace_id=actor.workspace_id,
        listing_id=listing_id,
        profile_key=profile.key,
        state=ReviewState.PENDING,
        row_version=1,
        revision_id=revision_id,
        listing_revision=revision_number,
        valuation_id=valuation_id,
        valuation_state=valuation_state,
        is_fixture=is_fixture,
    )
    event_id, event_created = await _emit_pending(
        conn,
        actor,
        snapshot,
        readiness=readiness,
        dashboard_base_url=dashboard_base_url,
        priority=event_priority,
    )
    await _audit_upsert(conn, actor, snapshot, None, 1, action="created")
    return CaseUpsertResult(
        action="created",
        case_id=snapshot.case_id,
        case_version=1,
        state=ReviewState.PENDING,
        readiness=readiness,
        event_id=event_id,
        event_created=event_created,
    )


async def _audit_upsert(
    conn: Conn,
    actor: ActorContext,
    case: ReviewCaseSnapshot,
    prior: int | None,
    new_version: int,
    *,
    action: str,
) -> None:
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "review.case_upsert",
            "review_case",
            case.case_id,
            prior,
            new_version,
            metadata={"action": action, "is_fixture": case.is_fixture},
        )


# =============================================================================================
# Claims
# =============================================================================================


async def claim(
    conn: Conn,
    actor: ActorContext,
    case_id: UUID,
    expected_version: int,
    idempotency_key: str,
    *,
    duration: timedelta = DEFAULT_CLAIM_DURATION,
) -> ClaimResult:
    """``reviews_claim``: a fresh opaque token (returned once), expiry by database time."""
    actor.require(Scope.REVIEWS_WRITE)
    request = {"case_id": str(case_id), "expected_version": expected_version}
    replay = await begin_idempotent(
        conn, actor, "reviews_claim", idempotency_key, idempotency.request_hash_for("reviews_claim", request)
    )
    if replay is not None:
        return ClaimResult.from_stored(replay)
    row = await _load_case(conn, actor, case_id, lock=True)
    now = ensure_utc(await db_now(conn))
    grant = evaluate_claim(row.snapshot, actor, expected_version=expected_version, now=now, duration=duration)
    await _write_case(
        conn,
        actor,
        row,
        {
            "state": ReviewState.CLAIMED.value,
            "row_version": grant.row_version,
            "claim_holder": grant.holder,
            "claim_token_hash": grant.claim_token_hash,
            "claimed_at": grant.claimed_at,
            "claim_expires_at": grant.expires_at,
        },
    )
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "review.claim",
            "review_case",
            case_id,
            grant.expected_row_version,
            grant.row_version,
            metadata={"rotated": grant.rotated, "took_over_expired": grant.took_over_expired},
        )
    stored = {
        **grant.redacted_result(),
        "rotated": grant.rotated,
        "took_over_expired": grant.took_over_expired,
    }
    await idempotency.complete(conn, actor, "reviews_claim", idempotency_key, stored)
    return ClaimResult.of(grant)


async def release(
    conn: Conn, actor: ActorContext, case_id: UUID, claim_token: str, idempotency_key: str
) -> ReleaseResultView:
    """``reviews_release``: only the caller's current claim; nothing to release is a no-op."""
    actor.require(Scope.REVIEWS_WRITE)
    request = {"case_id": str(case_id), "claim_token": claim_token}
    replay = await begin_idempotent(
        conn,
        actor,
        "reviews_release",
        idempotency_key,
        idempotency.request_hash_for("reviews_release", request),
    )
    if replay is not None:
        return ReleaseResultView.model_validate(replay)
    row = await _load_case(conn, actor, case_id, lock=True)
    now = ensure_utc(await db_now(conn))
    result = evaluate_release(row.snapshot, actor, claim_token, now)
    if result.changed:
        await _write_case(
            conn,
            actor,
            row,
            {"state": result.new_state.value, "row_version": result.row_version, **_NO_CLAIM},
        )
        async with mapped_errors():
            await audit.record(
                conn,
                actor,
                "review.release",
                "review_case",
                case_id,
                result.expected_row_version,
                result.row_version,
                metadata={"new_state": result.new_state.value},
            )
    view = ReleaseResultView.of(result)
    await idempotency.complete(conn, actor, "reviews_release", idempotency_key, view.model_dump(mode="json"))
    return view


async def expire_claims(conn: Conn, actor: ActorContext, *, limit: int = 100) -> list[ReleaseResultView]:
    """Reaper (system): expired claims return to their restore state (database time)."""
    if actor.principal_kind != "system":
        raise Forbidden("Only the system reaper expires claims")
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    async with mapped_errors():
        due = await fetch_all(
            conn,
            "select id from app.review_cases where workspace_id = %(ws)s and state = 'claimed'"
            " and claim_expires_at <= clock_timestamp() order by claim_expires_at, id"
            " for update skip locked limit %(limit)s",
            {"ws": actor.workspace_id, "limit": limit},
        )
    results: list[ReleaseResultView] = []
    for item in due:
        row = await _load_case(conn, actor, item["id"], lock=True)
        now = ensure_utc(await db_now(conn))
        expired = expire_claim(row.snapshot, now)
        if expired is None:
            continue
        await _write_case(
            conn,
            actor,
            row,
            {"state": expired.new_state.value, "row_version": expired.row_version, **_NO_CLAIM},
        )
        async with mapped_errors():
            await audit.record(
                conn,
                actor,
                "review.claim_expire",
                "review_case",
                row.snapshot.case_id,
                expired.expected_row_version,
                expired.row_version,
            )
        results.append(ReleaseResultView.of(expired))
    return results


# =============================================================================================
# Submit
# =============================================================================================


_LISTING_FACTS_SQL: Final = """
select l.id, l.current_revision_id, l.eligibility_state, l.availability, l.last_detail_success_at,
       l.screening
  from app.listings l
 where l.workspace_id = %(ws)s and l.id = %(listing_id)s
 for share
"""


def _decision_view(decision: SubmitDecision, decision_id: UUID) -> ReviewDecisionView:
    bounded = decision.model_copy(update={"tool_request_id": decision.tool_request_id[:200] or "unknown"})
    return ReviewDecisionView.of(bounded, decision_id=decision_id)


async def submit(
    conn: Conn,
    actor: ActorContext,
    request: SubmitRequest,
    *,
    dashboard_base_url: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    prompt_template_version: str | None = None,
    materiality_policy: MaterialityPolicy | None = None,
    freshness_max_age: timedelta = DEFAULT_DETAIL_MAX_AGE,
) -> ReviewDecisionView:
    """``reviews_submit`` in ONE transaction (see module docstring)."""
    actor.require(Scope.REVIEWS_WRITE)
    key = request.idempotency_key
    replay = await begin_idempotent(
        conn, actor, "reviews_submit", key, idempotency.request_hash_for("reviews_submit", request)
    )
    if replay is not None:
        return ReviewDecisionView.model_validate(replay)
    ws = actor.workspace_id
    preview = await _load_case(conn, actor, request.case_id, lock=False)
    async with mapped_errors():
        listing = await fetch_one(
            conn, _LISTING_FACTS_SQL, {"ws": ws, "listing_id": preview.snapshot.listing_id}
        )
    if listing is None:  # pragma: no cover - composite FK guarantees the listing
        raise NotFound("Review case not found")
    row = await _load_case(conn, actor, request.case_id, lock=True)
    case = row.snapshot
    now = ensure_utc(await db_now(conn))
    valuation = None
    if case.valuation_id is not None:
        async with mapped_errors():
            valuation = await fetch_one(
                conn,
                "select id, listing_revision_id, state, expires_at, scenarios, currency,"
                " base_contribution_minor, conservative_contribution_minor from app.valuations"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": case.valuation_id},
            )
    guard = SubmitGuard(
        eligibility=None
        if listing["eligibility_state"] is None
        else EligibilityState(listing["eligibility_state"]),
        availability=Availability(listing["availability"]),
        freshness_ok=listing["last_detail_success_at"] is not None
        and now - ensure_utc(listing["last_detail_success_at"]) <= freshness_max_age,
        valuation_fingerprint_current=_valuation_current(valuation, listing["current_revision_id"], now),
    )
    decision = evaluate_submit(
        case,
        actor,
        request,
        now=now,
        guard=guard,
        model_name=model_name,
        model_version=model_version,
        prompt_template_version=prompt_template_version,
    )
    if listing["current_revision_id"] != case.revision_id:
        raise VersionConflict(
            "The listing has a newer revision; reload before deciding",
            current_listing_revision=None,
        )
    decision_id = await _insert_decision(conn, actor, decision)
    await _write_case(
        conn,
        actor,
        row,
        {
            "state": decision.new_state.value,
            "row_version": decision.row_version,
            "latest_decision_id": decision_id,
            **_NO_CLAIM,
        },
    )
    notification: dict[str, Any] = {"notification": "not_candidate"}
    if decision.notification_candidate:
        notification = await _shortlist_notification(
            conn,
            actor,
            decision,
            decision_id,
            listing=listing,
            valuation=valuation,
            dashboard_base_url=dashboard_base_url,
            policy=materiality_policy or MaterialityPolicy(),
        )
    view = _decision_view(decision, decision_id)
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "review.submit",
            "review_case",
            case.case_id,
            decision.case_version,
            decision.row_version,
            metadata={
                "decision_id": str(decision_id),
                "outcome": decision.outcome.value,
                "listing_revision": decision.listing_revision,
                "input_hash": decision.input_hash,
                **notification,
            },
        )
    await idempotency.complete(conn, actor, "reviews_submit", key, view.model_dump(mode="json"))
    return view


def _valuation_current(
    valuation: Mapping[str, Any] | None, current_revision_id: UUID | None, now: datetime
) -> bool:
    """The cited valuation still matches the committed facts: not stale/invalid, not expired and
    computed for the listing's current revision (its dependency fingerprint is still valid)."""
    if valuation is None:
        return False
    if ValuationState(valuation["state"]) in _UNUSABLE_VALUATION:
        return False
    if valuation["expires_at"] is not None and ensure_utc(valuation["expires_at"]) <= now:
        return False
    return bool(valuation["listing_revision_id"] == current_revision_id)


_INSERT_DECISION_SQL: Final = """
insert into app.review_decisions (
  workspace_id, case_id, case_version, listing_id, listing_revision_id, valuation_id,
  actor_principal_id, actor_kind, actor_role, outcome, reason_codes, summary, evidence_ids,
  missing_information, model_name, model_version, model_run_id, prompt_template_version,
  tool_request_id, input_hash, supersedes_id, is_fixture, created_at)
values (
  %(ws)s, %(case_id)s, %(case_version)s, %(listing_id)s, %(revision_id)s, %(valuation_id)s,
  %(principal)s, %(kind)s, %(role)s, %(outcome)s, %(reason_codes)s::text[], %(summary)s,
  %(evidence_ids)s::uuid[], %(missing)s::text[], %(model_name)s, %(model_version)s, %(model_run_id)s,
  %(prompt_version)s, %(tool_request_id)s, %(input_hash)s, %(supersedes_id)s, %(is_fixture)s,
  %(decided_at)s)
returning id
"""


async def _insert_decision(conn: Conn, actor: ActorContext, decision: SubmitDecision) -> UUID:
    params = {
        "ws": actor.workspace_id,
        "case_id": decision.case_id,
        "case_version": decision.case_version,
        "listing_id": decision.listing_id,
        "revision_id": decision.listing_revision_id,
        "valuation_id": decision.valuation_id,
        # The actor comes from the authenticated context only (request bodies carry no actor).
        "principal": decision.actor_principal_id,
        "kind": decision.actor_kind,
        "role": decision.actor_role.value,
        "outcome": decision.outcome.value,
        "reason_codes": list(decision.reason_codes),
        "summary": decision.summary,
        "evidence_ids": list(decision.evidence_ids),
        "missing": list(decision.missing_information),
        "model_name": decision.model_name,
        "model_version": decision.model_version,
        "model_run_id": decision.model_run_id,
        "prompt_version": decision.prompt_template_version,
        "tool_request_id": decision.tool_request_id[:200] or "unknown",
        "input_hash": decision.input_hash,
        "supersedes_id": decision.supersedes_decision_id,
        "is_fixture": decision.is_fixture,
        "decided_at": decision.decided_at,
    }
    async with mapped_errors(
        unique={
            "review_decisions_case_version_uk": lambda: VersionConflict(
                "A decision for this case version already exists; reload"
            )
        }
    ):
        row = await fetch_one(conn, _INSERT_DECISION_SQL, params)
    assert row is not None
    decision_id: UUID = row["id"]
    return decision_id


def _eur(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _alert_state(listing: Mapping[str, Any], valuation: Mapping[str, Any] | None) -> AlertState:
    """Facts an owner alert would rest on, read from committed rows (unknown stays unknown)."""
    body: Mapping[str, Any] = {}
    if valuation is not None and isinstance(valuation["scenarios"], Mapping):
        found = valuation["scenarios"].get("valuation")
        body = found if isinstance(found, Mapping) else {}
    screening = body.get("screening") if isinstance(body.get("screening"), Mapping) else {}
    payable = screening.get("eur_payable") if isinstance(screening, Mapping) else None
    price = _eur(payable.get("amount")) if isinstance(payable, Mapping) else None
    if price is None and isinstance(listing["screening"], Mapping):
        price = _eur(listing["screening"].get("eur_amount"))
    tax = body.get("tax")
    scenarios = body.get("scenarios") if isinstance(body.get("scenarios"), Mapping) else None
    threshold = scenarios.get("threshold") if isinstance(scenarios, Mapping) else None
    conservative = base = None
    if valuation is not None and valuation["currency"] == "EUR":
        if valuation["conservative_contribution_minor"] is not None:
            conservative = Money.from_minor(valuation["conservative_contribution_minor"], "EUR").amount
        if valuation["base_contribution_minor"] is not None:
            base = Money.from_minor(valuation["base_contribution_minor"], "EUR").amount
    production_ready: bool | None = None
    if isinstance(tax, Mapping):
        production_ready = (
            tax.get("rule_status") == "active" and not tax.get("is_fixture") and bool(tax.get("complete"))
        )
    return AlertState(
        price_eur=price if price is not None and price > 0 else None,
        eligibility=None
        if listing["eligibility_state"] is None
        else EligibilityState(listing["eligibility_state"]),
        availability=Availability(listing["availability"]),
        tax_rules_valid=production_ready,
        conservative_contribution_eur=conservative,
        base_contribution_eur=base,
        contribution_meets_threshold=threshold.get("would_meet") if isinstance(threshold, Mapping) else None,
    )


async def _previous_alert(conn: Conn, actor: ActorContext, listing_id: UUID) -> AlertState | None:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select payload from ops.outbox where workspace_id = %(ws)s and event_type = %(type)s"
            " and aggregate_type = 'review_case' and payload ->> 'listing_id' = %(listing)s"
            " order by event_created_at desc, id desc limit 1",
            {"ws": actor.workspace_id, "type": SHORTLIST_EVENT_TYPE, "listing": str(listing_id)},
        )
    if row is None:
        return None
    try:
        return AlertState.model_validate(row["payload"].get("alert_state") or {})
    except (ValidationError, AttributeError):
        return None


async def _shortlist_notification(
    conn: Conn,
    actor: ActorContext,
    decision: SubmitDecision,
    decision_id: UUID,
    *,
    listing: Mapping[str, Any],
    valuation: Mapping[str, Any] | None,
    dashboard_base_url: str | None,
    policy: MaterialityPolicy,
) -> dict[str, Any]:
    """Create the owner alert event when it is AUTHORIZED (an approved, enabled ``owner_alert``
    route) and MATERIAL (``evaluate_materiality``); otherwise record why not (audit)."""
    route = await bindings_repo.route_for(conn, actor.workspace_id, "owner_alert")
    if route is None:
        return {"notification": "no_active_route"}
    if dashboard_base_url is None:
        return {"notification": "no_dashboard_url"}
    current = _alert_state(listing, valuation)
    previous = await _previous_alert(conn, actor, decision.listing_id)
    materiality = evaluate_materiality(previous, current, policy)
    facts = {
        "materiality_reasons": [r.value for r in materiality.reasons],
        "materiality_blockers": list(materiality.blockers),
        "policy_version": materiality.policy_version,
    }
    if not materiality.realert_allowed:
        return {"notification": "not_material", **facts}
    now = ensure_utc(await db_now(conn))
    event_id = uuid4()
    dedup = f"{SHORTLIST_EVENT_TYPE}:{decision_id}"
    payload = {
        "schema_version": "1.0",
        "event_id": str(event_id),
        "type": SHORTLIST_EVENT_TYPE,
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
        "case_id": str(decision.case_id),
        "case_version": decision.row_version,
        "decision_id": str(decision_id),
        "listing_id": str(decision.listing_id),
        "listing_revision": decision.listing_revision,
        "valuation_id": None if decision.valuation_id is None else str(decision.valuation_id),
        "dashboard_url": dashboard_case_url(dashboard_base_url, decision.case_id),
        "summary": SHORTLIST_SUMMARY,
        "deduplication_key": dedup,
        "materiality": {
            "policy_version": materiality.policy_version,
            "policy_approved": materiality.policy_approved,
            "reasons": [r.value for r in materiality.reasons],
        },
        "alert_state": current.model_dump(mode="json"),
    }
    created_id, created = await outbox.enqueue_event(
        conn,
        actor,
        event_type=SHORTLIST_EVENT_TYPE,
        event_version=SHORTLIST_EVENT_VERSION,
        aggregate_type="review_case",
        aggregate_id=decision.case_id,
        aggregate_version=decision.row_version,
        payload=payload,
        dedup_key=dedup,
        destination_binding_id=route.binding_id,
        is_fixture=decision.is_fixture,
        event_id=event_id,
    )
    return {
        "notification": "event_created" if created else "event_exists",
        "event_id": str(created_id),
        **facts,
    }


# =============================================================================================
# Queue and case reads
# =============================================================================================


class ReviewQueueFilters(BaseModel):
    model_config = _FROZEN

    include_needs_information: bool = True
    profile: ProfileKey | None = None

    def as_filters(self) -> dict[str, Any]:
        data: dict[str, Any] = {"include_needs_information": self.include_needs_information}
        if self.profile is not None:
            data["profile"] = self.profile.value
        return data

    def states(self) -> list[str]:
        states = [ReviewState.PENDING.value, ReviewState.CLAIMED.value]
        if self.include_needs_information:
            states.append(ReviewState.NEEDS_INFORMATION.value)
        return states


class ReviewQueueResult(BaseModel):
    model_config = _FROZEN

    page: ReviewQueuePage
    next_cursor: str | None


_QUEUE_SQL: Final = """
select c.id, c.row_version, c.listing_id, c.revision_id, r.revision_number, c.valuation_id,
       v.state as valuation_state, c.profile_key, c.queue_label, c.state, l.eligibility_state,
       c.readiness, c.priority, c.ranking, c.claim_holder, c.claim_expires_at,
       r.normalized ->> 'title' as title, r.make, r.model, r.seller_country, r.asking_minor,
       r.currency, r.mileage_km, l.screening ->> 'eur_amount' as eur_amount, c.is_fixture,
       c.created_at, c.updated_at
  from app.review_cases c
  join app.listings l on l.workspace_id = c.workspace_id and l.id = c.listing_id
  join app.listing_revisions r on r.workspace_id = c.workspace_id and r.id = c.revision_id
  left join app.valuations v on v.workspace_id = c.workspace_id and v.id = c.valuation_id
 where c.workspace_id = %(ws)s and c.state = any(%(states)s::text[])
   and (%(profile)s::text is null or c.profile_key = %(profile)s)
 order by c.priority desc, c.created_at, c.id
 limit %(limit)s
"""


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _rank_summary(ranking: Mapping[str, Any] | None) -> RankSummary | None:
    if not ranking or ranking.get("total") is None or not ranking.get("scoring_version"):
        return None
    try:
        return RankSummary(score=str(ranking["total"]), scoring_version=str(ranking["scoring_version"])[:80])
    except ValidationError:
        return None


def _claim_state(row: Mapping[str, Any], caller: UUID, now: datetime) -> ClaimStateView:
    expires = row["claim_expires_at"]
    if row["state"] != ReviewState.CLAIMED.value or expires is None or ensure_utc(expires) <= now:
        return ClaimStateView(claimed=False, held_by_caller=False, expires_at=None)
    return ClaimStateView(
        claimed=True, held_by_caller=row["claim_holder"] == caller, expires_at=ensure_utc(expires)
    )


def _eur_amount(value: Any) -> AmountView:
    amount = _eur(value)
    if amount is None:
        return AmountView.unknown("EUR payable amount unknown", currency="EUR")
    return AmountView.of(Money(amount=amount, currency="EUR"))


def _queue_item(row: Mapping[str, Any], caller: UUID, now: datetime) -> ReviewQueueItem:
    valuation_state = (
        ValuationState(row["valuation_state"]) if row["valuation_state"] else ValuationState.NOT_STARTED
    )
    title = row["title"]
    return ReviewQueueItem(
        case_id=row["id"],
        case_version=row["row_version"],
        listing_id=row["listing_id"],
        revision_id=row["revision_id"],
        listing_revision=row["revision_number"],
        valuation_id=row["valuation_id"],
        profile=ProfileKey(row["profile_key"]),
        queue_label=row["queue_label"],
        state=ReviewState(row["state"]),
        eligibility=None if row["eligibility_state"] is None else EligibilityState(row["eligibility_state"]),
        readiness=row["readiness"],
        valuation_state=valuation_state,
        priority=row["priority"],
        rank=_rank_summary(row["ranking"]),
        claim=_claim_state(row, caller, now),
        title=None if title is None else str(title)[:300],
        make=row["make"],
        model=row["model"],
        seller_country=row["seller_country"],
        payable=AmountView.from_minor(
            row["asking_minor"], row["currency"], unknown_reason="asking price unknown"
        ),
        payable_eur=_eur_amount(row["eur_amount"]),
        mileage_km=_plain(row["mileage_km"]),
        research_candidate=valuation_state not in _FIGURE_STATES,
        is_fixture=row["is_fixture"],
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
    )


async def list_pending_queue(
    conn: Conn,
    actor: ActorContext,
    filters: ReviewQueueFilters,
    cursor: str | None,
    *,
    secret: bytes | Sequence[bytes],
    limit: int | None = None,
    snapshot_ttl: timedelta = query_snapshots.DEFAULT_SNAPSHOT_TTL,
    cursor_ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> ReviewQueueResult:
    """``reviews_list_pending``: the first call freezes membership + projections; a cursor
    pages through that frozen snapshot (bound to principal, workspace and filter hash)."""
    actor.require(Scope.REVIEWS_READ)
    fh = filter_hash(filters.as_filters())
    if cursor is None:
        async with mapped_errors():
            rows = await fetch_all(
                conn,
                _QUEUE_SQL,
                {
                    "ws": actor.workspace_id,
                    "states": filters.states(),
                    "profile": None if filters.profile is None else filters.profile.value,
                    "limit": MAX_QUEUE_SIZE,
                },
            )
        now = ensure_utc(await db_now(conn))
        projections = [_queue_item(r, actor.principal_id, now).model_dump(mode="json") for r in rows]
        result = await query_snapshots.start_listing(
            conn,
            actor,
            query_name=QUEUE_QUERY_NAME,
            filter_hash=fh,
            ordered_ids=[r["id"] for r in rows],
            projections=projections,
            limit=limit,
            secret=secret,
            ttl=snapshot_ttl,
            cursor_ttl=cursor_ttl,
        )
    else:
        result = await query_snapshots.next_page(
            conn,
            actor,
            cursor=cursor,
            query_name=QUEUE_QUERY_NAME,
            filter_hash=fh,
            limit=limit,
            secret=secret,
            cursor_ttl=cursor_ttl,
        )
    async with mapped_errors():
        snap = await fetch_one(
            conn,
            "select created_at from ops.query_snapshots"
            " where workspace_id = %(ws)s and id = %(id)s and principal_id = %(principal)s",
            {"ws": actor.workspace_id, "id": result.page.snapshot_id, "principal": actor.principal_id},
        )
    assert snap is not None
    page = ReviewQueuePage(
        items=tuple(ReviewQueueItem.model_validate(p) for p in result.page.projections),
        total=result.page.total,
        snapshot_created_at=ensure_utc(snap["created_at"]),
        snapshot_expires_at=result.page.expires_at,
        include_needs_information=filters.include_needs_information,
    )
    return ReviewQueueResult(page=page, next_cursor=result.next_cursor)


_CANDIDATE_SQL: Final = """
select l.id as listing_id, l.source_id, s.source_key, s.country as source_country, s.paused as source_paused,
       l.first_seen_at, l.last_seen_at, l.last_detail_success_at, l.last_availability_check_at,
       l.availability, l.eligibility_state, l.eligibility_profile, l.quarantined,
       l.screening ->> 'eur_amount' as eur_amount, r.id as revision_id, r.revision_number,
       r.seller_country, r.normalized, r.make, r.model, r.vehicle_generation, r.asking_minor, r.currency,
       r.price_basis, r.price_type, r.mileage_km, r.registration_year, r.registration_month
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  join app.listing_revisions r on r.workspace_id = l.workspace_id and r.id = %(revision_id)s
 where l.workspace_id = %(ws)s and l.id = %(listing_id)s
"""


def _partial_date(year: int | None, month: int | None) -> PartialDate:
    if year is None:
        return PartialDate()
    if month is None:
        return PartialDate(value=f"{year:04d}", precision=Precision.YEAR)
    return PartialDate(value=f"{year:04d}-{month:02d}", precision=Precision.MONTH)


def _normalized_field(normalized: Any, *path: str) -> Any:
    value = normalized
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _enum_or[E: StrEnum](enum: type[E], value: object, default: E) -> E:
    if not isinstance(value, str):
        return default
    try:
        return enum(value)
    except ValueError:
        return default


async def get_case(conn: Conn, actor: ActorContext, case_id: UUID) -> ReviewCaseView:
    """``GET /api/reviews/{case_id}``: the case, its candidate, valuation and decision history."""
    actor.require(Scope.REVIEWS_READ)
    row = await _load_case(conn, actor, case_id, lock=False)
    case = row.snapshot
    now = ensure_utc(await db_now(conn))
    params = {
        "ws": actor.workspace_id,
        "listing_id": case.listing_id,
        "revision_id": case.revision_id,
        "id": case_id,
    }
    async with mapped_errors():
        listing = await fetch_one(conn, _CANDIDATE_SQL, params)
        valuation = None
        if case.valuation_id is not None:
            valuation = await fetch_one(
                conn,
                "select id, state, is_fixture, created_at, expires_at, dependency_fingerprint, currency,"
                " base_contribution_minor, conservative_contribution_minor from app.valuations"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": actor.workspace_id, "id": case.valuation_id},
            )
        decisions = await fetch_all(
            conn,
            "select d.*, r.revision_number from app.review_decisions d"
            " join app.listing_revisions r"
            "   on r.workspace_id = d.workspace_id and r.id = d.listing_revision_id"
            " where d.workspace_id = %(ws)s and d.case_id = %(id)s order by d.case_version limit 200",
            params,
        )
    assert listing is not None
    valuation_state = (
        ValuationState(valuation["state"]) if valuation is not None else ValuationState.NOT_STARTED
    )
    normalized = listing["normalized"]
    payable_eur = _eur_amount(listing["eur_amount"])
    summary = CandidateSummary(
        listing_id=case.listing_id,
        revision_id=listing["revision_id"],
        revision_number=listing["revision_number"],
        source_id=listing["source_id"],
        source_key=listing["source_key"],
        source_country=listing["source_country"],
        seller_country=listing["seller_country"],
        title=None
        if _normalized_field(normalized, "title") is None
        else str(_normalized_field(normalized, "title"))[:300],
        make=listing["make"],
        model=listing["model"],
        generation=listing["vehicle_generation"],
        price=PriceSummary(
            payable=AmountView.from_minor(
                listing["asking_minor"], listing["currency"], unknown_reason="asking price unknown"
            ),
            original_currency=listing["currency"],
            eur_equivalent=payable_eur,
            fx_rate=None,
            basis=_enum_or(PriceBasis, listing["price_basis"], PriceBasis.UNKNOWN),
            price_type=_enum_or(PriceType, listing["price_type"], PriceType.UNKNOWN),
            negotiable=_enum_or(
                Tristate, _normalized_field(normalized, "price", "negotiable"), Tristate.UNKNOWN
            ),
        ),
        mileage_km=_plain(listing["mileage_km"]),
        mileage_claim=_enum_or(
            OdometerClaim, _normalized_field(normalized, "vehicle", "mileage_claim"), OdometerClaim.UNKNOWN
        ),
        first_registration=_partial_date(listing["registration_year"], listing["registration_month"]),
        availability=Availability(listing["availability"]),
        eligibility=None
        if listing["eligibility_state"] is None
        else EligibilityState(listing["eligibility_state"]),
        eligibility_profile=None
        if listing["eligibility_profile"] is None
        else ProfileKey(listing["eligibility_profile"]),
        queue_label=row.queue_label,
        valuation_id=case.valuation_id,
        valuation_state=valuation_state,
        case_id=case.case_id,
        review_state=case.state,
        freshness=FreshnessView.compute(
            now=now,
            first_seen_at=listing["first_seen_at"],
            last_seen_at=listing["last_seen_at"],
            last_detail_success_at=listing["last_detail_success_at"],
            last_availability_check_at=listing["last_availability_check_at"],
            source_paused=bool(listing["source_paused"]),
        ),
        rank=_rank_summary(row.ranking),
        research_candidate=valuation_state not in _FIGURE_STATES,
        quarantined=bool(listing["quarantined"]),
        is_fixture=case.is_fixture,
    )
    valuation_ref = None
    if valuation is not None:
        currency = valuation["currency"]
        reason = f"valuation is {valuation_state.value}"
        valuation_ref = ValuationRef(
            valuation_id=valuation["id"],
            state=valuation_state,
            research_candidate=valuation_state == ValuationState.INCOMPLETE,
            is_fixture=valuation["is_fixture"],
            created_at=ensure_utc(valuation["created_at"]),
            expires_at=None if valuation["expires_at"] is None else ensure_utc(valuation["expires_at"]),
            dependency_fingerprint=valuation["dependency_fingerprint"],
            conservative_contribution=AmountView.from_minor(
                valuation["conservative_contribution_minor"], currency, unknown_reason=reason
            ),
            base_contribution=AmountView.from_minor(
                valuation["base_contribution_minor"], currency, unknown_reason=reason
            ),
        )
    claim_row = {
        "state": case.state.value,
        "claim_expires_at": case.claim_expires_at,
        "claim_holder": case.claim_holder,
    }
    return ReviewCaseView(
        case_id=case.case_id,
        case_version=case.row_version,
        state=case.state,
        listing_id=case.listing_id,
        revision_id=case.revision_id,
        listing_revision=case.listing_revision,
        profile=case.profile_key,
        queue_label=row.queue_label,
        readiness=row.readiness,
        priority=row.priority,
        claim=_claim_state(claim_row, actor.principal_id, now),
        candidate=summary,
        valuation=valuation_ref,
        latest_decision_id=case.latest_decision_id,
        decisions=tuple(_decision_from_row(d) for d in decisions),
        superseded_by_id=case.superseded_by_id,
        reason=row.reason,
        is_fixture=case.is_fixture,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _decision_from_row(row: Mapping[str, Any]) -> ReviewDecisionView:
    outcome = ReviewOutcome(row["outcome"])
    return ReviewDecisionView(
        decision_id=row["id"],
        case_id=row["case_id"],
        case_version=row["case_version"],
        case_state=outcome_state(outcome),
        new_case_version=row["case_version"] + 1,
        listing_id=row["listing_id"],
        listing_revision_id=row["listing_revision_id"],
        listing_revision=row["revision_number"],
        valuation_id=row["valuation_id"],
        outcome=outcome,
        reason_codes=tuple(row["reason_codes"]),
        summary=row["summary"],
        evidence_ids=tuple(row["evidence_ids"] or ()),
        missing_information=tuple(row["missing_information"] or ()),
        actor=DecisionActorView(
            principal_id=row["actor_principal_id"],
            principal_kind=row["actor_kind"],
            role=row["actor_role"],
        ),
        model_name=row["model_name"],
        model_version=row["model_version"],
        model_run_id=row["model_run_id"],
        prompt_template_version=row["prompt_template_version"],
        tool_request_id=row["tool_request_id"] or "unknown",
        input_hash=row["input_hash"],
        decided_at=ensure_utc(row["created_at"]),
        supersedes_decision_id=row["supersedes_id"],
        is_fixture=row["is_fixture"],
    )


__all__ = [
    "PENDING_EVENT_VERSION",
    "QUEUE_QUERY_NAME",
    "SHORTLIST_EVENT_TYPE",
    "CaseUpsertResult",
    "ReviewQueueFilters",
    "ReviewQueueResult",
    "begin_idempotent",
    "claim",
    "expire_claims",
    "get_case",
    "list_pending_queue",
    "release",
    "submit",
    "upsert_review_case",
]
