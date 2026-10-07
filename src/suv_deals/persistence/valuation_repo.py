"""FX observations, tax rule sets, cost profiles/evidence and reproducible valuations (spec 16-18).

Tables: ``app.fx_rates``, ``app.tax_rule_sets``, ``app.cost_profiles``, ``app.cost_evidence``,
``app.valuations`` (+ ``ops.jobs`` for recomputation, ``ops.audit_events``).

FX (``app.fx_rates``, append-only)
    ``1 base = rate quote`` with an explicit direction, provider, purpose
    (reference/customs/payment) and fixture flag. `upsert_fx_rate` is insert-or-return: the
    same (base, quote, date, provider, purpose) with the same rate returns the existing row; a
    different rate is a conflict, never an overwrite. Rates are stored canonically (trailing
    zeros stripped, at most 10 fractional digits) and returned as such: compute valuations
    from the returned/loaded ``FxRate``.

Tax rule sets (``app.tax_rule_sets``) - mapping (``RuleSet`` -> row)
    - one row per entry of ``vehicle_categories`` (``unspecified`` when empty), column
      ``vehicle_category``; ``rule_set_id`` column = ``<rule_set_id>::<category>`` (unique per
      version, so every category row of one version is distinct);
    - content (everything except status/approval/review/sha256) -> ``rules`` (jsonb document);
    - ``sources``, ``valid_from``, ``valid_to``, ``currency``, ``jurisdiction``, ``version``,
      ``is_fixture`` -> typed columns (copies, verified on load);
    - ``sha256`` (content hash) -> ``sha256``, sealed when the draft is submitted for review;
    - ``approved_by`` (the approver's NAME) and ``review_record`` -> ``approval_reference`` =
      canonical JSON ``{"approver": ..., "review_record": ...}`` (at most 500 characters);
    - the authenticated approving principal -> ``approved_by`` (uuid), ``approved_at``.

    New versions are stored only as ``draft`` (or ``unapproved`` examples/fixtures). Every
    status change goes through ``domain.tax_engine.transition_rule_set`` (and the database
    trigger); approval needs the owner (``config:admin``), a named approver and a review record
    bound to the content hash. Nothing is ever approved or activated automatically. Moving to
    superseded/expired/revoked marks dependent valuations stale and queues recomputation.

Cost profiles (``app.cost_profiles``)
    ``assumptions`` holds ``{"notes", "assumptions", "content_sha256"}``; only the approval
    columns change after insert. Profiles are stored unapproved; `approve_cost_profile` records
    the authenticated owner. Because the row stores a principal id rather than a name, a loaded
    approved profile reports ``approved_by = "principal:<uuid>"`` (deterministic, so its
    ``CostProfileRef.sha256`` is stable across loads).

Cost evidence (``app.cost_evidence``, append-only)
    quote/estimate/actual with low/base/high minor units, expiry and scope; the evidence
    document's SHA-256 is kept in ``evidence.document_sha256``. A superseding row invalidates
    valuations that cited the superseded evidence.

Valuations (``app.valuations``)
    `persist_valuation` stores the domain ``Valuation`` plus the inputs ``ValuationView`` needs
    (cost lines, purchase, proceeds, import line sources) in ``scenarios`` (a versioned
    document), the typed dependency references (FK columns and id arrays, each verified
    against the recorded ``ValuationDependencies``) and the dependency fingerprint. The only
    later change is `mark_stale`. `invalidate_dependents` is the reverse invalidation of spec
    18: valuations that depend on a changed listing revision, comparable set/observation, FX
    rate, cost quote/profile, tax rule set or business configuration are marked stale
    immediately and ONE deduplicated recomputation job is queued per affected listing
    (``valuation.recompute:<listing_id>``; while that job is already RUNNING, one follow-up
    ``valuation.recompute:<listing_id>:after:<job id>``, because the running job may have read
    the old inputs). `persist_valuation` holds FOR SHARE locks on the tax rule row and cost
    profile it cites (a concurrent revoke/approval waits and then finds the new valuation), and
    refuses cost evidence that is already superseded or cited but not tracked.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.costs import CostLine, CostProfile, CostScope, ProceedsEstimate, PurchaseInput
from suv_deals.domain.enums import (
    CostCategory,
    CostLineStatus,
    FxPurpose,
    JobState,
    JobType,
    Scope,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    ReviewRecord,
    RuleSet,
    compute_rule_set_sha256,
    transition_rule_set,
    validate_rule_set,
)
from suv_deals.domain.valuation import InvalidationReason, Valuation
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import audit, jobs
from suv_deals.persistence.database import Conn, db_now, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.market_repo import require_reader, require_writer
from suv_deals.views.valuations import ValuationView

_FROZEN = ConfigDict(frozen=True, extra="forbid")
VALUATION_DOCUMENT_FORMAT: Final = "suv_deals.valuation/1"
RULE_SET_DOCUMENT_FORMAT: Final = "suv_deals.tax_rule_set/1"
COST_PROFILE_DOCUMENT_FORMAT: Final = "suv_deals.cost_profile/1"
RULE_KEY_SEPARATOR: Final = "::"
UNSPECIFIED_CATEGORY: Final = "unspecified"
MAX_APPROVAL_REFERENCE: Final = 500
OPEN_VALUATION_STATES: Final = (
    ValuationState.NOT_STARTED,
    ValuationState.INCOMPLETE,
    ValuationState.ESTIMATED,
    ValuationState.QUOTE_SUPPORTED,
)
_LIFECYCLE_FIELDS: Final = frozenset({"status", "approved_by", "approved_at", "review_record", "sha256"})
_CLOSED_TAX_STATUSES: Final = frozenset(
    {TaxRuleStatus.SUPERSEDED, TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED}
)
_STORABLE_TAX_STATUSES: Final = frozenset({TaxRuleStatus.DRAFT, TaxRuleStatus.UNAPPROVED})
_MAX_FX_FRACTION_DIGITS: Final = 10
_MAX_FX_INTEGER_DIGITS: Final = 10
_PRINCIPAL_PREFIX: Final = "principal:"


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed("timestamps must be timezone-aware") from exc


def _require_owner(actor: ActorContext, what: str) -> None:
    if actor.principal_kind == "system":
        raise Forbidden(f"{what} needs the owner; system workers never do it")
    actor.require(Scope.CONFIG_ADMIN)


# =============================================================================================
# FX rates
# =============================================================================================


class StoredFxRate(BaseModel):
    model_config = _FROZEN

    id: UUID
    rate: FxRate
    is_fixture: bool
    source_ref: str | None
    created_at: datetime


def canonical_rate(value: Decimal) -> Decimal:
    """Exact rate without trailing zeros (``61.5000000000`` -> ``61.5``); refuses values that
    ``numeric(20,10)`` cannot hold exactly."""
    try:
        canonical = Decimal(format(value.normalize(), "f"))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed("FX rate is not a finite decimal") from exc
    exponent = canonical.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_MAX_FX_FRACTION_DIGITS:
        raise ValidationFailed("FX rate has more than 10 fractional digits")
    if canonical >= Decimal(10) ** _MAX_FX_INTEGER_DIGITS or canonical <= 0:
        raise ValidationFailed("FX rate is out of range")
    return canonical


def _fx_from_row(row: Mapping[str, Any]) -> StoredFxRate:
    return StoredFxRate(
        id=row["id"],
        rate=FxRate(
            base=row["base"],
            quote=row["quote"],
            rate=canonical_rate(row["rate"]),
            rate_date=row["rate_date"],
            retrieved_at=ensure_utc(row["retrieved_at"]),
            provider=row["provider"],
            purpose=FxPurpose(row["purpose"]),
        ),
        is_fixture=row["is_fixture"],
        source_ref=row["source_ref"],
        created_at=ensure_utc(row["created_at"]),
    )


_FX_COLUMNS: Final = (
    "id, base, quote, rate, rate_date, retrieved_at, provider, purpose, source_ref, is_fixture, created_at"
)


async def upsert_fx_rate(
    conn: Conn,
    actor: ActorContext,
    rate: FxRate,
    *,
    is_fixture: bool = False,
    source_ref: str | None = None,
) -> tuple[StoredFxRate, bool]:
    """Record one FX observation (direction explicit) or return the identical existing one."""
    require_writer(actor)
    value = canonical_rate(rate.rate)
    if source_ref is not None and not 1 <= len(source_ref) <= 500:
        raise ValidationFailed("source_ref must be 1-500 characters")
    params = {
        "ws": actor.workspace_id,
        "base": rate.base,
        "quote": rate.quote,
        "rate": value,
        "rate_date": rate.rate_date,
        "retrieved_at": _aware(rate.retrieved_at),
        "provider": rate.provider,
        "purpose": rate.purpose.value,
        "source_ref": source_ref,
        "is_fixture": is_fixture,
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.fx_rates (workspace_id, base, quote, rate, rate_date, retrieved_at, provider,"  # noqa: S608 - fixed column list
            " purpose, source_ref, is_fixture) values (%(ws)s, %(base)s, %(quote)s, %(rate)s, %(rate_date)s,"
            " %(retrieved_at)s, %(provider)s, %(purpose)s, %(source_ref)s, %(is_fixture)s)"
            " on conflict (workspace_id, base, quote, rate_date, provider, purpose) do nothing"
            f" returning {_FX_COLUMNS}",
            params,
        )
        if row is not None:
            return _fx_from_row(row), True
        existing = await fetch_one(
            conn,
            f"select {_FX_COLUMNS} from app.fx_rates where workspace_id = %(ws)s and base = %(base)s"  # noqa: S608
            " and quote = %(quote)s and rate_date = %(rate_date)s and provider = %(provider)s"
            " and purpose = %(purpose)s",
            params,
        )
    if existing is None:  # pragma: no cover - unique key is workspace-scoped
        raise AppError(ErrorCode.VERSION_CONFLICT, "FX observation changed concurrently", retryable=True)
    stored = _fx_from_row(existing)
    if stored.rate.rate != value or stored.is_fixture != is_fixture:
        raise VersionConflict("A different FX observation is already recorded for this date and provider")
    return stored, False


async def get_fx_rates(conn: Conn, actor: ActorContext, ids: Sequence[UUID]) -> list[StoredFxRate]:
    """FX rows by id in the requested order; a missing or foreign id is NotFound."""
    require_reader(actor)
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_FX_COLUMNS} from app.fx_rates"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": wanted},
        )
    by_id = {r["id"]: _fx_from_row(r) for r in rows}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise NotFound("FX rate not found")
    return [by_id[i] for i in wanted]


async def latest_fx_rates(
    conn: Conn,
    actor: ActorContext,
    *,
    base: str,
    quote: str,
    purpose: FxPurpose,
    on_or_before: date,
    include_fixtures: bool = False,
    limit: int = 10,
) -> list[StoredFxRate]:
    """Most recent observations of one directed pair and purpose (newest first)."""
    require_reader(actor)
    if not 1 <= limit <= 100:
        raise ValidationFailed("limit must be between 1 and 100")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_FX_COLUMNS} from app.fx_rates where workspace_id = %(ws)s"  # noqa: S608
            " and base = %(base)s and quote = %(quote)s and purpose = %(purpose)s"
            " and rate_date <= %(day)s and (%(fixtures)s or not is_fixture)"
            " order by rate_date desc, retrieved_at desc, id limit %(limit)s",
            {
                "ws": actor.workspace_id,
                "base": base,
                "quote": quote,
                "purpose": purpose.value,
                "day": on_or_before,
                "fixtures": include_fixtures,
                "limit": limit,
            },
        )
    return [_fx_from_row(r) for r in rows]


# =============================================================================================
# Tax rule sets
# =============================================================================================


class TaxRuleRow(BaseModel):
    model_config = _FROZEN

    id: UUID
    vehicle_category: str


class StoredRuleSet(BaseModel):
    """A rule set version as stored (one row per vehicle category)."""

    model_config = _FROZEN

    rule_set: RuleSet
    rows: tuple[TaxRuleRow, ...]
    approved_by_principal: UUID | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    def row_for(self, category: str) -> UUID:
        for row in self.rows:
            if row.vehicle_category == category:
                return row.id
        raise NotFound("The rule set does not cover this vehicle category")


def rule_row_key(rule_set_id: str, category: str) -> str:
    key = f"{rule_set_id}{RULE_KEY_SEPARATOR}{category}"
    if len(key) > 200:
        raise ValidationFailed("rule_set_id plus vehicle category exceeds 200 characters")
    return key


def _content_document(rule_set: RuleSet) -> dict[str, Any]:
    return {
        "format": RULE_SET_DOCUMENT_FORMAT,
        "rule_set": rule_set.model_dump(mode="json", exclude=set(_LIFECYCLE_FIELDS)),
    }


def approval_reference(approver: str, review_record: ReviewRecord) -> str:
    """Canonical JSON ``{"approver", "review_record"}`` stored in ``approval_reference``."""
    text = json.dumps(
        {"approver": approver, "review_record": review_record.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(text) > MAX_APPROVAL_REFERENCE:
        raise ValidationFailed(
            "The approval record exceeds the 500-character approval_reference column; shorten the "
            "review scope/notes (a dedicated review_record column is requested)",
            details={"limit": MAX_APPROVAL_REFERENCE},
        )
    return text


_TAX_COLUMNS: Final = (
    "id, rule_set_id, jurisdiction, vehicle_category, version, status, valid_from, valid_to, currency,"
    " rules, sources, sha256, approved_by, approved_at, approval_reference, is_fixture, created_by,"
    " created_at, updated_at"
)


def _rule_set_from_rows(rows: Sequence[Mapping[str, Any]]) -> StoredRuleSet:
    if not rows:
        raise NotFound("Tax rule set not found")
    first = rows[0]
    shared = ("status", "rules", "sha256", "approved_by", "approved_at", "approval_reference", "is_fixture")
    if any(any(r[name] != first[name] for name in shared) for r in rows[1:]):
        raise ValidationFailed("tax rule set rows disagree; the stored version is inconsistent")
    doc = first["rules"]
    if not isinstance(doc, Mapping) or doc.get("format") != RULE_SET_DOCUMENT_FORMAT:
        raise ValidationFailed("tax rule set document is not in the repository format")
    approver: str | None = None
    review: Any = None
    if first["approval_reference"] is not None:
        try:
            approval = json.loads(first["approval_reference"])
            approver = str(approval["approver"])
            review = approval["review_record"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationFailed("tax rule approval record is malformed") from exc
    try:
        rule_set = RuleSet.model_validate(
            {
                **doc["rule_set"],
                "status": first["status"],
                "sha256": first["sha256"],
                "approved_by": approver,
                "approved_at": first["approved_at"],
                "review_record": review,
            }
        )
    except ValidationError as exc:
        raise ValidationFailed("stored tax rule set is invalid") from exc
    expected = {
        (rule_row_key(rule_set.rule_set_id, c))
        for c in (rule_set.vehicle_categories or (UNSPECIFIED_CATEGORY,))
    }
    if {r["rule_set_id"] for r in rows} != expected or any(
        (r["jurisdiction"], r["version"], r["is_fixture"])
        != (rule_set.jurisdiction, rule_set.version, rule_set.is_fixture)
        for r in rows
    ):
        raise ValidationFailed("tax rule set columns disagree with the stored document")
    if rule_set.sha256 is not None and rule_set.sha256 != compute_rule_set_sha256(rule_set):
        raise ValidationFailed("stored tax rule set does not match its content hash")
    if rule_set.status in (TaxRuleStatus.APPROVED, TaxRuleStatus.ACTIVE):
        validate_rule_set(rule_set)  # fail closed: an unverifiable rule set is never used
    return StoredRuleSet(
        rule_set=rule_set,
        rows=tuple(TaxRuleRow(id=r["id"], vehicle_category=r["vehicle_category"]) for r in rows),
        approved_by_principal=first["approved_by"],
        created_by=first["created_by"],
        created_at=ensure_utc(first["created_at"]),
        updated_at=max(ensure_utc(r["updated_at"]) for r in rows),
    )


async def store_rule_set(conn: Conn, actor: ActorContext, rule_set: RuleSet) -> StoredRuleSet:
    """Store a NEW rule-set version as ``draft`` (or an ``unapproved`` example/fixture).

    Approved/active versions never enter through here: approval and activation are explicit
    owner transitions (`transition_tax_rule_set`).
    """
    _require_owner(actor, "Storing a tax rule set")
    if rule_set.status not in _STORABLE_TAX_STATUSES:
        raise ValidationFailed("new tax rule set versions are stored as draft (or unapproved examples)")
    if rule_set.approved_by is not None or rule_set.review_record is not None:
        raise ValidationFailed("a new rule set version carries no approval record")
    if rule_set.sha256 is not None and rule_set.sha256 != compute_rule_set_sha256(rule_set):
        raise ValidationFailed("sha256 does not match the rule set content")
    categories = rule_set.vehicle_categories or (UNSPECIFIED_CATEGORY,)
    document = Jsonb(_content_document(rule_set))
    sources = Jsonb([s.model_dump(mode="json") for s in rule_set.sources])
    async with mapped_errors():
        for category in categories:
            await conn.execute(
                "insert into app.tax_rule_sets (workspace_id, rule_set_id, jurisdiction, vehicle_category,"
                " version, status, valid_from, valid_to, currency, rules, sources, sha256, is_fixture,"
                " created_by) values (%(ws)s, %(key)s, %(jurisdiction)s, %(category)s, %(version)s,"
                " %(status)s, %(valid_from)s, %(valid_to)s, %(currency)s, %(rules)s, %(sources)s,"
                " %(sha256)s, %(is_fixture)s, %(created_by)s)",
                {
                    "ws": actor.workspace_id,
                    "key": rule_row_key(rule_set.rule_set_id, category),
                    "jurisdiction": rule_set.jurisdiction,
                    "category": category,
                    "version": rule_set.version,
                    "status": rule_set.status.value,
                    "valid_from": rule_set.valid_from,
                    "valid_to": rule_set.valid_to,
                    "currency": rule_set.currency,
                    "rules": document,
                    "sources": sources,
                    "sha256": rule_set.sha256,
                    "is_fixture": rule_set.is_fixture,
                    "created_by": actor.principal_id,
                },
            )
        await audit.record(
            conn,
            actor,
            "tax_rules.store",
            "tax_rule_set",
            None,
            metadata={"rule_set": rule_set.label(), "status": rule_set.status.value},
        )
    return await load_rule_set(conn, actor, rule_set.rule_set_id, rule_set.version)


async def _rule_rows(
    conn: Conn, actor: ActorContext, rule_set_id: str, version: str, *, lock: bool
) -> list[Any]:
    query = (
        f"select {_TAX_COLUMNS} from app.tax_rule_sets where workspace_id = %(ws)s"  # noqa: S608
        " and rules -> 'rule_set' ->> 'rule_set_id' = %(rule_set_id)s and version = %(version)s"
        " order by id" + (" for update" if lock else "")
    )
    async with mapped_errors():
        return await fetch_all(
            conn, query, {"ws": actor.workspace_id, "rule_set_id": rule_set_id, "version": version}
        )


async def load_rule_set(conn: Conn, actor: ActorContext, rule_set_id: str, version: str) -> StoredRuleSet:
    require_reader(actor)
    return _rule_set_from_rows(await _rule_rows(conn, actor, rule_set_id, version, lock=False))


async def list_rule_sets(
    conn: Conn,
    actor: ActorContext,
    *,
    jurisdiction: str | None = None,
    statuses: Sequence[TaxRuleStatus] | None = None,
) -> list[StoredRuleSet]:
    """Every stored version (grouped), optionally filtered; e.g. ACTIVE ones for selection."""
    require_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_TAX_COLUMNS} from app.tax_rule_sets where workspace_id = %(ws)s"  # noqa: S608
            " and (%(jurisdiction)s::text is null or jurisdiction = %(jurisdiction)s)"
            " and (%(statuses)s::text[] is null or status = any(%(statuses)s::text[]))"
            " order by rules -> 'rule_set' ->> 'rule_set_id', version, id",
            {
                "ws": actor.workspace_id,
                "jurisdiction": jurisdiction,
                "statuses": None if statuses is None else [s.value for s in statuses],
            },
        )
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        key = (str(row["rules"]["rule_set"]["rule_set_id"]), row["version"])
        groups.setdefault(key, []).append(row)
    return [_rule_set_from_rows(g) for g in groups.values()]


async def transition_tax_rule_set(
    conn: Conn,
    actor: ActorContext,
    rule_set_id: str,
    version: str,
    target: TaxRuleStatus,
    *,
    approved_by: str | None = None,
    review_record: ReviewRecord | None = None,
) -> StoredRuleSet:
    """Move a rule set version through the lifecycle (domain rules + database trigger).

    Owner only. ``approved_by`` is the approver's NAME (kept in ``approval_reference``); the
    authenticated principal is recorded in ``approved_by``/``approved_at``. Never automatic.
    """
    _require_owner(actor, "Changing a tax rule set status")
    rows = await _rule_rows(conn, actor, rule_set_id, version, lock=True)
    stored = _rule_set_from_rows(rows)
    current = stored.rule_set
    now = ensure_utc(await db_now(conn))
    existing = [
        s.rule_set
        for s in await list_rule_sets(
            conn, actor, jurisdiction=current.jurisdiction, statuses=(TaxRuleStatus.ACTIVE,)
        )
    ]
    candidate = transition_rule_set(
        current, target, at=now, approved_by=approved_by, review_record=review_record, existing=existing
    )
    seal = current.status == TaxRuleStatus.DRAFT
    if seal and candidate.sha256 is None:
        candidate = candidate.model_copy(update={"sha256": compute_rule_set_sha256(candidate)})
    reference: str | None = None
    approving = target == TaxRuleStatus.APPROVED
    if approving:
        assert candidate.approved_by is not None and candidate.review_record is not None
        reference = approval_reference(candidate.approved_by, candidate.review_record)
    assignments = [sql.SQL("status = %(status)s")]
    if approving:
        assignments += [
            sql.SQL("approved_by = %(principal)s"),
            sql.SQL("approved_at = %(approved_at)s"),
            sql.SQL("approval_reference = %(reference)s"),
        ]
    if seal:
        assignments.append(sql.SQL("sha256 = %(sha256)s"))
    query = sql.SQL(
        "update app.tax_rule_sets set {assignments} where workspace_id = %(ws)s and id = any(%(ids)s::uuid[])"
        " and status = %(prior)s"
    ).format(assignments=sql.SQL(", ").join(assignments))
    async with mapped_errors():
        cur = await conn.execute(
            query,
            {
                "ws": actor.workspace_id,
                "ids": [r.id for r in stored.rows],
                "status": target.value,
                "prior": current.status.value,
                "principal": actor.principal_id,
                "approved_at": candidate.approved_at,
                "reference": reference,
                "sha256": candidate.sha256,
            },
        )
        if cur.rowcount != len(stored.rows):
            raise VersionConflict("The tax rule set changed concurrently; reload and retry")
        await audit.record(
            conn,
            actor,
            "tax_rules.approve" if approving else "tax_rules.transition",
            "tax_rule_set",
            stored.rows[0].id,
            metadata={
                "rule_set": current.label(),
                "from": current.status.value,
                "to": target.value,
                "rows": len(stored.rows),
            },
        )
    if target in _CLOSED_TAX_STATUSES:
        await invalidate_dependents(
            conn,
            actor,
            DependencyChange(
                reason=InvalidationReason.TAX_RULE,
                tax_rule_set_ids=tuple(r.id for r in stored.rows),
                detail=f"tax rule {current.label()} is {target.value}",
            ),
        )
    return await load_rule_set(conn, actor, rule_set_id, version)


# =============================================================================================
# Cost profiles and cost evidence
# =============================================================================================


class StoredCostProfile(BaseModel):
    model_config = _FROZEN

    id: UUID
    profile: CostProfile
    approved_by_principal: UUID | None
    config_revision_id: UUID | None
    created_by: UUID | None
    created_at: datetime


def principal_label(principal_id: UUID) -> str:
    """How a principal-id approval is shown where the domain expects an approver label."""
    return f"{_PRINCIPAL_PREFIX}{principal_id}"


def _unapproved(profile: CostProfile) -> CostProfile:
    return profile.model_copy(
        update={"approval_status": "unapproved", "approved_by": None, "approved_at": None}
    )


_PROFILE_COLUMNS: Final = (
    "id, profile_key, version, basis, assumptions, currency, approval_status, approved_by, approved_at,"
    " config_revision_id, is_fixture, created_by, created_at"
)


def _profile_from_row(row: Mapping[str, Any]) -> StoredCostProfile:
    doc = row["assumptions"]
    if not isinstance(doc, Mapping) or doc.get("format") != COST_PROFILE_DOCUMENT_FORMAT:
        raise ValidationFailed("cost profile document is not in the repository format")
    approved = row["approval_status"] == "approved"
    try:
        profile = CostProfile.model_validate(
            {
                "profile_key": row["profile_key"],
                "version": row["version"],
                "basis": row["basis"],
                "currency": row["currency"],
                "approval_status": row["approval_status"],
                "approved_by": principal_label(row["approved_by"]) if approved else None,
                "approved_at": row["approved_at"] if approved else None,
                "is_fixture": row["is_fixture"],
                "notes": doc.get("notes", []),
                "assumptions": doc.get("assumptions", []),
            }
        )
    except ValidationError as exc:
        raise ValidationFailed("stored cost profile is invalid") from exc
    if _unapproved(profile).sha256() != doc.get("content_sha256"):
        raise ValidationFailed("stored cost profile does not match its content hash")
    return StoredCostProfile(
        id=row["id"],
        profile=profile,
        approved_by_principal=row["approved_by"],
        config_revision_id=row["config_revision_id"],
        created_by=row["created_by"],
        created_at=ensure_utc(row["created_at"]),
    )


async def store_cost_profile(
    conn: Conn, actor: ActorContext, profile: CostProfile, *, config_revision_id: UUID | None = None
) -> StoredCostProfile:
    """Store a new cost-profile version, always unapproved (approval is a separate owner act)."""
    require_writer(actor)
    if profile.approval_status != "unapproved" or profile.approved_by or profile.approved_at:
        raise ValidationFailed("cost profiles are stored unapproved; approve them explicitly")
    document = {
        "format": COST_PROFILE_DOCUMENT_FORMAT,
        "content_sha256": profile.sha256(),
        "notes": list(profile.notes),
        "assumptions": [a.model_dump(mode="json") for a in profile.assumptions],
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.cost_profiles (workspace_id, profile_key, version, basis, assumptions, currency,"  # noqa: S608 - fixed column list
            " config_revision_id, is_fixture, created_by) values (%(ws)s, %(key)s, %(version)s, %(basis)s,"
            f" %(doc)s, %(currency)s, %(config)s, %(fixture)s, %(created_by)s) returning {_PROFILE_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "key": profile.profile_key,
                "version": profile.version,
                "basis": profile.basis,
                "doc": Jsonb(document),
                "currency": profile.currency,
                "config": config_revision_id,
                "fixture": profile.is_fixture,
                "created_by": None if actor.principal_kind == "system" else actor.principal_id,
            },
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "cost_profile.store",
            "cost_profile",
            row["id"],
            metadata={"profile": f"{profile.profile_key}@v{profile.version}"},
        )
    return _profile_from_row(row)


async def load_cost_profile(conn: Conn, actor: ActorContext, profile_id: UUID) -> StoredCostProfile:
    require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_PROFILE_COLUMNS} from app.cost_profiles"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": profile_id},
        )
    if row is None:
        raise NotFound("Cost profile not found")
    return _profile_from_row(row)


async def find_cost_profile(
    conn: Conn, actor: ActorContext, profile_key: str, version: int
) -> StoredCostProfile:
    require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_PROFILE_COLUMNS} from app.cost_profiles"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and profile_key = %(key)s and version = %(version)s",
            {"ws": actor.workspace_id, "key": profile_key, "version": version},
        )
    if row is None:
        raise NotFound("Cost profile not found")
    return _profile_from_row(row)


async def approve_cost_profile(conn: Conn, actor: ActorContext, profile_id: UUID) -> StoredCostProfile:
    """Owner approval of a cost-profile version (records the authenticated principal).

    Approval changes the profile's ``CostProfileRef``; valuations that used the unapproved
    version are invalidated so recomputation can use the approved assumptions.
    """
    _require_owner(actor, "Approving a cost profile")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update app.cost_profiles set approval_status = 'approved', approved_by = %(principal)s,"  # noqa: S608 - fixed column list
            " approved_at = clock_timestamp()"
            " where workspace_id = %(ws)s and id = %(id)s and approval_status = 'unapproved'"
            f" and not is_fixture returning {_PROFILE_COLUMNS}",
            {"ws": actor.workspace_id, "id": profile_id, "principal": actor.principal_id},
        )
        if row is None:
            existing = await fetch_one(
                conn,
                "select approval_status, is_fixture from app.cost_profiles"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": actor.workspace_id, "id": profile_id},
            )
            if existing is None:
                raise NotFound("Cost profile not found")
            if existing["is_fixture"]:
                raise ValidationFailed("a fixture cost profile can never be approved")
            raise VersionConflict("The cost profile is already approved")
        await audit.record(conn, actor, "cost_profile.approve", "cost_profile", profile_id)
    await invalidate_dependents(
        conn,
        actor,
        DependencyChange(
            reason=InvalidationReason.COST_PROFILE,
            cost_profile_ids=(profile_id,),
            detail="cost profile approved",
        ),
    )
    return _profile_from_row(row)


CostEvidenceKind = Literal["quote", "estimate", "actual"]


class CostEvidenceInput(BaseModel):
    """One quote/estimate/actual. ``document_sha256`` is the hash of the stored evidence file."""

    model_config = _FROZEN

    kind: CostEvidenceKind
    category: CostCategory
    provider: str | None = Field(default=None, min_length=1, max_length=200)
    low: Money | None = None
    base: Money | None = None
    high: Money | None = None
    obtained_at: datetime
    expires_at: datetime | None = None
    scope: CostScope
    details: dict[str, Any] = Field(default_factory=dict)
    document_ref: str | None = Field(default=None, max_length=500)
    document_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    listing_id: UUID | None = None
    supersedes_id: UUID | None = None
    is_fixture: bool = False

    @field_validator("obtained_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _rules(self) -> CostEvidenceInput:
        amounts = [m for m in (self.low, self.base, self.high) if m is not None]
        if not amounts:
            raise ValueError("cost evidence needs at least one amount")
        if len({m.currency for m in amounts}) != 1:
            raise ValueError("cost evidence amounts share one currency")
        ordered = [m.amount for m in (self.low, self.base, self.high) if m is not None]
        if ordered != sorted(ordered) or any(a < 0 for a in ordered):
            raise ValueError("amounts must satisfy 0 <= low <= base <= high")
        if self.kind == "quote" and not self.provider:
            raise ValueError("a quote needs a provider")
        if self.expires_at is not None and self.expires_at <= self.obtained_at:
            raise ValueError("expiry must follow the time the evidence was obtained")
        if self.category == CostCategory.PURCHASE:
            raise ValueError("the purchase is listing price evidence, not cost evidence")
        return self

    @property
    def currency(self) -> str:
        return next(m.currency for m in (self.low, self.base, self.high) if m is not None)


class StoredCostEvidence(CostEvidenceInput):
    id: UUID
    recorded_by: UUID | None
    created_at: datetime


_EVIDENCE_COLUMNS: Final = (
    "id, kind, category, provider, low_minor, base_minor, high_minor, currency, obtained_at, expires_at,"
    " scope, evidence, document_ref, listing_id, supersedes_id, recorded_by, is_fixture, created_at"
)


def _minor(value: Money | None) -> int | None:
    return None if value is None else value.to_minor()


def _evidence_from_row(row: Mapping[str, Any]) -> StoredCostEvidence:
    evidence = row["evidence"] or {}
    currency = row["currency"]

    def money(minor: int | None) -> Money | None:
        return None if minor is None else Money.from_minor(minor, currency)

    return StoredCostEvidence(
        id=row["id"],
        kind=row["kind"],
        category=CostCategory(row["category"]),
        provider=row["provider"],
        low=money(row["low_minor"]),
        base=money(row["base_minor"]),
        high=money(row["high_minor"]),
        obtained_at=ensure_utc(row["obtained_at"]),
        expires_at=None if row["expires_at"] is None else ensure_utc(row["expires_at"]),
        scope=CostScope.model_validate(row["scope"]),
        details=dict(evidence.get("details") or {}),
        document_ref=row["document_ref"],
        document_sha256=evidence.get("document_sha256"),
        listing_id=row["listing_id"],
        supersedes_id=row["supersedes_id"],
        is_fixture=row["is_fixture"],
        recorded_by=row["recorded_by"],
        created_at=ensure_utc(row["created_at"]),
    )


async def insert_cost_evidence(
    conn: Conn, actor: ActorContext, item: CostEvidenceInput
) -> StoredCostEvidence:
    """Append cost evidence; a superseding row invalidates valuations that cited the old one."""
    require_writer(actor)
    params = {
        "ws": actor.workspace_id,
        "kind": item.kind,
        "category": item.category.value,
        "provider": item.provider,
        "low": _minor(item.low),
        "base": _minor(item.base),
        "high": _minor(item.high),
        "currency": item.currency,
        "obtained_at": item.obtained_at,
        "expires_at": item.expires_at,
        "scope": Jsonb(item.scope.model_dump(mode="json")),
        "evidence": Jsonb({"document_sha256": item.document_sha256, "details": item.details}),
        "document_ref": item.document_ref,
        "listing_id": item.listing_id,
        "supersedes_id": item.supersedes_id,
        "recorded_by": None if actor.principal_kind == "system" else actor.principal_id,
        "fixture": item.is_fixture,
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.cost_evidence (workspace_id, kind, category, provider, low_minor, base_minor,"  # noqa: S608 - fixed column list
            " high_minor, currency, obtained_at, expires_at, scope, evidence, document_ref, listing_id,"
            " supersedes_id, recorded_by, is_fixture) values (%(ws)s, %(kind)s, %(category)s, %(provider)s,"
            " %(low)s, %(base)s, %(high)s, %(currency)s, %(obtained_at)s, %(expires_at)s, %(scope)s,"
            " %(evidence)s, %(document_ref)s, %(listing_id)s, %(supersedes_id)s, %(recorded_by)s,"
            f" %(fixture)s) returning {_EVIDENCE_COLUMNS}",
            params,
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "cost_evidence.record",
            "cost_evidence",
            row["id"],
            metadata={"kind": item.kind, "category": item.category.value},
        )
    if item.supersedes_id is not None:
        await invalidate_dependents(
            conn,
            actor,
            DependencyChange(
                reason=InvalidationReason.COST_QUOTE,
                cost_evidence_ids=(item.supersedes_id,),
                detail="cost evidence superseded",
            ),
        )
    return _evidence_from_row(row)


async def get_cost_evidence(conn: Conn, actor: ActorContext, ids: Sequence[UUID]) -> list[StoredCostEvidence]:
    require_reader(actor)
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_EVIDENCE_COLUMNS} from app.cost_evidence"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": wanted},
        )
    by_id = {r["id"]: _evidence_from_row(r) for r in rows}
    if len(by_id) != len(wanted):
        raise NotFound("Cost evidence not found")
    return [by_id[i] for i in wanted]


_EVIDENCE_STATUS: Final[dict[str, CostLineStatus]] = {
    "quote": CostLineStatus.QUOTED,
    "estimate": CostLineStatus.ESTIMATED,
    "actual": CostLineStatus.ACTUAL,
}


def cost_line_from_evidence(evidence: StoredCostEvidence, *, label: str | None = None) -> CostLine:
    """The scenario line for one evidence row (evidence id = the row id)."""
    base = evidence.base or evidence.low or evidence.high
    return CostLine(
        category=evidence.category,
        label=label or f"{evidence.kind} {evidence.category.value}",
        status=_EVIDENCE_STATUS[evidence.kind],
        currency=evidence.currency,
        low=evidence.low,
        base=base,
        high=evidence.high,
        evidence_ids=(str(evidence.id),),
        provider=evidence.provider,
        expires_at=evidence.expires_at,
        scope=evidence.scope,
        refundable=evidence.category == CostCategory.REFUNDABLE_DEPOSIT,
    )


# =============================================================================================
# Valuations
# =============================================================================================


class ValuationRefs(BaseModel):
    """The rows a valuation depends on (each is checked against ``Valuation.dependencies``)."""

    model_config = _FROZEN

    listing_id: UUID
    config_revision_id: UUID
    comparable_set_id: UUID | None = None
    tax_rule_set_row_id: UUID | None = None
    cost_profile_id: UUID | None = None
    fx_rate_ids: tuple[UUID, ...] = ()
    cost_evidence_ids: tuple[UUID, ...] = ()


class ValuationInputs(BaseModel):
    """Inputs ``ValuationView`` shows next to the scenario results."""

    model_config = _FROZEN

    cost_lines: tuple[CostLine, ...] = Field(default=(), max_length=200)
    purchase: PurchaseInput | None = None
    proceeds: ProceedsEstimate | None = None
    import_line_sources: tuple[str, ...] = ()


class StoredValuation(BaseModel):
    model_config = _FROZEN

    id: UUID
    listing_id: UUID
    listing_revision_id: UUID
    listing_revision: int
    valuation: Valuation
    inputs: ValuationInputs
    refs: ValuationRefs
    created_at: datetime

    def view(self) -> ValuationView:
        return ValuationView.of(
            self.valuation,
            valuation_id=self.id,
            listing_id=self.listing_id,
            listing_revision=self.listing_revision,
            cost_lines=self.inputs.cost_lines,
            purchase=self.inputs.purchase,
            proceeds=self.inputs.proceeds,
        )


def _uuid_text(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationFailed(f"{what} must be the id of the stored row") from exc


def _valuation_document(valuation: Valuation, inputs: ValuationInputs) -> dict[str, Any]:
    return {
        "format": VALUATION_DOCUMENT_FORMAT,
        "valuation": valuation.model_dump(mode="json"),
        "inputs": inputs.model_dump(mode="json"),
    }


async def _check_refs(conn: Conn, actor: ActorContext, valuation: Valuation, refs: ValuationRefs) -> UUID:
    """Every typed reference must be exactly what the fingerprinted dependencies record."""
    deps = valuation.dependencies
    revision_id = _uuid_text(valuation.listing_revision_id, "listing_revision_id")
    if deps.config_revision_id != str(refs.config_revision_id):
        raise ValidationFailed("config_revision_id differs from the recorded dependency")
    if (deps.comparable_set_id is None) != (refs.comparable_set_id is None) or (
        refs.comparable_set_id is not None and deps.comparable_set_id != str(refs.comparable_set_id)
    ):
        raise ValidationFailed("comparable_set_id differs from the recorded dependency")
    ws = actor.workspace_id
    async with mapped_errors():
        if refs.comparable_set_id is not None:
            row = await fetch_one(
                conn,
                "select listing_id, criteria ->> 'content_sha256' as content_sha256 from app.comparable_sets"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": refs.comparable_set_id},
            )
            if row is None:
                raise NotFound("Comparable set not found")
            if row["content_sha256"] != deps.comparable_hash or row["listing_id"] != refs.listing_id:
                raise ValidationFailed("the comparable set differs from the one the valuation used")
        if (deps.tax_rule is None) != (refs.tax_rule_set_row_id is None):
            raise ValidationFailed("tax_rule_set_row_id differs from the recorded dependency")
        if refs.tax_rule_set_row_id is not None and deps.tax_rule is not None:
            # FOR SHARE: a concurrent status change (revoke/expire/supersede) waits for this
            # transaction, so its reverse invalidation sees the new valuation (no lost update).
            row = await fetch_one(
                conn,
                "select rules -> 'rule_set' ->> 'rule_set_id' as rule_set_id, version, sha256, status,"
                " is_fixture from app.tax_rule_sets where workspace_id = %(ws)s and id = %(id)s for share",
                {"ws": ws, "id": refs.tax_rule_set_row_id},
            )
            if row is None:
                raise NotFound("Tax rule set not found")
            dep = deps.tax_rule
            if (row["rule_set_id"], row["version"], row["sha256"], row["is_fixture"]) != (
                dep.rule_set_id,
                dep.version,
                dep.sha256,
                dep.is_fixture,
            ):
                raise ValidationFailed("the tax rule set row differs from the recorded dependency")
            if row["status"] != dep.status.value:
                raise VersionConflict("The tax rule set status changed since the calculation; recompute")
        if (deps.cost_profile is None) != (refs.cost_profile_id is None):
            raise ValidationFailed("cost_profile_id differs from the recorded dependency")
        if refs.cost_profile_id is not None:
            # Same reason as the tax rule lock: a concurrent approval waits for this insert.
            await conn.execute(
                "select id from app.cost_profiles where workspace_id = %(ws)s and id = %(id)s for share",
                {"ws": ws, "id": refs.cost_profile_id},
            )
    if refs.cost_profile_id is not None:
        profile = await load_cost_profile(conn, actor, refs.cost_profile_id)
        if profile.profile.reference() != deps.cost_profile:
            raise VersionConflict("The cost profile changed since the calculation; recompute")
    rates = await get_fx_rates(conn, actor, refs.fx_rate_ids)
    stored_keys = sorted(
        (r.rate.base, r.rate.quote, r.rate.rate_date, r.rate.provider, r.rate.purpose.value, r.rate.rate)
        for r in rates
    )
    dep_keys = sorted(
        (f.base, f.quote, f.rate_date, f.provider, f.purpose.value, Decimal(f.rate)) for f in deps.fx
    )
    if stored_keys != dep_keys:
        raise ValidationFailed("fx_rate_ids differ from the recorded FX dependencies")
    cited = set(deps.cost_evidence_ids)
    if any(str(i) not in cited for i in refs.cost_evidence_ids):
        raise ValidationFailed("cost_evidence_ids must be evidence the scenarios cite")
    await _check_cost_evidence(conn, actor, cited, refs.cost_evidence_ids)
    return revision_id


async def _check_cost_evidence(
    conn: Conn, actor: ActorContext, cited: set[str], tracked: Sequence[UUID]
) -> None:
    """Every cited ``app.cost_evidence`` row is tracked in ``cost_evidence_ids`` (otherwise a
    superseding quote could never find the valuation), and none of them is already superseded
    (the calculation read outdated evidence: recompute)."""
    candidates: list[UUID] = []
    for text in cited:
        try:
            candidates.append(UUID(text))
        except ValueError:
            continue  # not a cost-evidence row id (e.g. another kind of evidence reference)
    wanted = sorted(set(candidates) | set(tracked), key=str)
    if not wanted:
        return
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select c.id, exists (select 1 from app.cost_evidence n where n.workspace_id = c.workspace_id"
            " and n.supersedes_id = c.id) as superseded from app.cost_evidence c"
            " where c.workspace_id = %(ws)s and c.id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": wanted},
        )
    if any(r["id"] not in set(tracked) for r in rows):
        raise ValidationFailed("cost_evidence_ids must list every cost evidence row the scenarios cite")
    if any(r["superseded"] for r in rows):
        raise VersionConflict("Cited cost evidence was superseded since the calculation; recompute")


_VALUATION_COLUMNS: Final = (
    "v.id, v.listing_id, v.listing_revision_id, v.comparable_set_id, v.tax_rule_set_id, v.cost_profile_id,"
    " v.config_revision_id, v.fx_rate_ids, v.cost_evidence_ids, v.dependency_fingerprint, v.state,"
    " v.scenarios, v.stale_at, v.stale_reason, v.is_fixture, v.created_at, r.revision_number"
)


async def persist_valuation(
    conn: Conn,
    actor: ActorContext,
    valuation: Valuation,
    refs: ValuationRefs,
    inputs: ValuationInputs,
) -> StoredValuation:
    """Insert one reproducible valuation (a recalculation is always a new row)."""
    require_writer(actor)
    if valuation.scenarios is not None:
        if tuple(valuation.scenarios.import_line_sources) != inputs.import_line_sources:
            raise ValidationFailed("import_line_sources differ from the scenarios")
        if inputs.purchase is None or inputs.proceeds is None:
            raise ValidationFailed("valued scenarios need their purchase and proceeds inputs")
    revision_id = await _check_refs(conn, actor, valuation, refs)
    currency = valuation.currency

    def minor(value: Money | None) -> int | None:
        if value is None:
            return None
        if value.currency != currency:
            raise ValidationFailed("contribution currency differs from the valuation currency")
        return value.to_minor_rounded()

    params = {
        "ws": actor.workspace_id,
        "listing_id": refs.listing_id,
        "revision_id": revision_id,
        "comparable_set_id": refs.comparable_set_id,
        "tax_id": refs.tax_rule_set_row_id,
        "profile_id": refs.cost_profile_id,
        "config_id": refs.config_revision_id,
        "fx_ids": list(refs.fx_rate_ids),
        "ce_ids": list(refs.cost_evidence_ids),
        "fingerprint": valuation.dependency_fingerprint,
        "state": valuation.state.value,
        "doc": Jsonb(_valuation_document(valuation, inputs)),
        "unknowns": Jsonb(list(valuation.unknowns)),
        "warnings": Jsonb(list(valuation.warnings)),
        "calc_version": valuation.calculation_version,
        "currency": currency,
        "base": minor(valuation.base_contribution),
        "conservative": minor(valuation.conservative_contribution),
        "upside": minor(valuation.upside_contribution),
        "expires_at": valuation.expires_at,
        "stale_at": valuation.stale_at,
        "stale_reason": valuation.stale_reason,
        "fixture": valuation.is_fixture,
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.valuations (workspace_id, listing_id, listing_revision_id, comparable_set_id,"
            " tax_rule_set_id, cost_profile_id, config_revision_id, fx_rate_ids, cost_evidence_ids,"
            " dependency_fingerprint, state, scenarios, unknowns, warnings, calculation_version, currency,"
            " base_contribution_minor, conservative_contribution_minor, upside_contribution_minor,"
            " expires_at, stale_at, stale_reason, is_fixture) values (%(ws)s, %(listing_id)s,"
            " %(revision_id)s, %(comparable_set_id)s, %(tax_id)s, %(profile_id)s, %(config_id)s,"
            " %(fx_ids)s::uuid[], %(ce_ids)s::uuid[], %(fingerprint)s, %(state)s, %(doc)s, %(unknowns)s,"
            " %(warnings)s, %(calc_version)s, %(currency)s, %(base)s, %(conservative)s, %(upside)s,"
            " %(expires_at)s, %(stale_at)s, %(stale_reason)s, %(fixture)s) returning id",
            params,
        )
        assert row is not None
        valuation_id: UUID = row["id"]
        await audit.record(
            conn,
            actor,
            "valuation.record",
            "valuation",
            valuation_id,
            metadata={
                "state": valuation.state.value,
                "dependency_fingerprint": valuation.dependency_fingerprint,
                "is_fixture": valuation.is_fixture,
            },
        )
    return await load_valuation(conn, actor, valuation_id)


def _stored_valuation(row: Mapping[str, Any]) -> StoredValuation:
    doc = row["scenarios"]
    if not isinstance(doc, Mapping) or doc.get("format") != VALUATION_DOCUMENT_FORMAT:
        raise ValidationFailed("valuation document is not in the repository format")
    state = ValuationState(row["state"])
    body = dict(doc["valuation"])
    body.update(
        state=state.value,
        stale_at=row["stale_at"],
        stale_reason=row["stale_reason"],
        alert_eligible=bool(body.get("alert_eligible")) and state != ValuationState.STALE,
    )
    try:
        valuation = Valuation.model_validate(body)
        inputs = ValuationInputs.model_validate(doc.get("inputs") or {})
    except ValidationError as exc:
        raise ValidationFailed("stored valuation is invalid") from exc
    if valuation.dependency_fingerprint != row["dependency_fingerprint"]:
        raise ValidationFailed("stored valuation does not match its dependency fingerprint")
    return StoredValuation(
        id=row["id"],
        listing_id=row["listing_id"],
        listing_revision_id=row["listing_revision_id"],
        listing_revision=row["revision_number"],
        valuation=valuation,
        inputs=inputs,
        refs=ValuationRefs(
            listing_id=row["listing_id"],
            config_revision_id=row["config_revision_id"],
            comparable_set_id=row["comparable_set_id"],
            tax_rule_set_row_id=row["tax_rule_set_id"],
            cost_profile_id=row["cost_profile_id"],
            fx_rate_ids=tuple(row["fx_rate_ids"] or ()),
            cost_evidence_ids=tuple(row["cost_evidence_ids"] or ()),
        ),
        created_at=ensure_utc(row["created_at"]),
    )


_SELECT_VALUATION: Final = (
    f"select {_VALUATION_COLUMNS} from app.valuations v"  # noqa: S608 - fixed column list
    " join app.listing_revisions r on r.workspace_id = v.workspace_id and r.id = v.listing_revision_id"
    " where v.workspace_id = %(ws)s"
)


async def load_valuation(conn: Conn, actor: ActorContext, valuation_id: UUID) -> StoredValuation:
    """Reconstruct a stored valuation (foreign or missing -> NotFound)."""
    require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn, _SELECT_VALUATION + " and v.id = %(id)s", {"ws": actor.workspace_id, "id": valuation_id}
        )
    if row is None:
        raise NotFound("Valuation not found")
    return _stored_valuation(row)


async def get_valuation_view(conn: Conn, actor: ActorContext, valuation_id: UUID) -> ValuationView:
    """``deals_get_valuation`` data."""
    actor.require(Scope.DEALS_READ)
    return (await load_valuation(conn, actor, valuation_id)).view()


async def current_valuation(conn: Conn, actor: ActorContext, listing_id: UUID) -> StoredValuation | None:
    """Newest valuation of the listing's CURRENT revision (any state; stale reads as stale)."""
    require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT_VALUATION + " and v.listing_id = %(listing_id)s and v.listing_revision_id = ("  # noqa: S608 - fixed column list
            "   select l.current_revision_id from app.listings l"
            "    where l.workspace_id = %(ws)s and l.id = %(listing_id)s)"
            " order by v.created_at desc, v.id desc limit 1",
            {"ws": actor.workspace_id, "listing_id": listing_id},
        )
    return None if row is None else _stored_valuation(row)


class ValuationStateChange(BaseModel):
    model_config = _FROZEN

    valuation_id: UUID
    listing_id: UUID
    previous_state: ValuationState
    state: ValuationState
    stale_at: datetime | None
    stale_reason: str | None
    changed: bool
    recompute_job_id: UUID | None = None  # the (deduplicated) recomputation queued on a change


def stale_reason_text(reasons: Sequence[InvalidationReason], detail: str | None = None) -> str:
    """Same wording as ``domain.valuation.mark_stale`` (``fx, tax_rule``), plus an optional detail."""
    if not reasons:
        raise ValidationFailed("marking a valuation stale needs at least one reason")
    try:
        text = ", ".join(InvalidationReason(r).value for r in reasons)
    except ValueError as exc:
        raise ValidationFailed("unknown invalidation reason") from exc
    if detail:
        text = f"{text}: {detail}"
    return text[:500]


async def mark_stale(
    conn: Conn,
    actor: ActorContext,
    valuation_id: UUID,
    reason: InvalidationReason | Sequence[InvalidationReason],
    *,
    detail: str | None = None,
) -> ValuationStateChange:
    """The only valuation state change: open -> ``stale`` (idempotent; ``invalid`` is terminal).

    A real change also queues the listing's deduplicated recomputation (``recompute_job_id``)."""
    require_writer(actor)
    reasons = [reason] if isinstance(reason, str) else list(reason)  # a StrEnum is a str
    text = stale_reason_text(reasons, detail)
    params = {"ws": actor.workspace_id, "id": valuation_id, "reason": text}
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id, listing_id, state, stale_at, stale_reason from app.valuations"
            " where workspace_id = %(ws)s and id = %(id)s for update",
            params,
        )
        if row is None:
            raise NotFound("Valuation not found")
        prior = ValuationState(row["state"])
        if prior == ValuationState.STALE:
            return ValuationStateChange(
                valuation_id=valuation_id,
                listing_id=row["listing_id"],
                previous_state=prior,
                state=prior,
                stale_at=ensure_utc(row["stale_at"]),
                stale_reason=row["stale_reason"],
                changed=False,
            )
        if prior == ValuationState.INVALID:
            raise ValidationFailed("an invalid valuation is terminal and cannot become stale")
        updated = await fetch_one(
            conn,
            "update app.valuations set state = 'stale', stale_at = clock_timestamp(),"
            " stale_reason = %(reason)s"
            " where workspace_id = %(ws)s and id = %(id)s returning stale_at",
            params,
        )
        assert updated is not None
        await audit.record(
            conn,
            actor,
            "valuation.mark_stale",
            "valuation",
            valuation_id,
            reason=text,
            metadata={"from": prior.value},
        )
    # Spec 18: mark stale immediately, THEN queue the deduplicated recomputation (same
    # transaction), so a stale valuation is never orphaned.
    job_id, _ = await _enqueue_recompute(conn, actor, row["listing_id"], [valuation_id], reasons[0])
    return ValuationStateChange(
        valuation_id=valuation_id,
        listing_id=row["listing_id"],
        previous_state=prior,
        state=ValuationState.STALE,
        stale_at=ensure_utc(updated["stale_at"]),
        stale_reason=text,
        changed=True,
        recompute_job_id=job_id,
    )


class DependencyChange(BaseModel):
    """What changed (spec 18 "Invalidation and material changes"). Set the fields that apply.

    - ``listing_id`` + ``current_revision_id``: valuations of an older revision of the listing;
    - ``comparable_set_ids`` / ``market_observation_ids``: sets (or sets containing observations);
    - ``fx_rate_ids``; or ``fx_pair`` + ``fx_newer_than``: valuations that used an OLDER rate of
      the same directed pair and purpose than a newly recorded observation;
    - ``cost_evidence_ids``, ``cost_profile_ids``, ``tax_rule_set_ids`` (rows);
    - ``current_config_revision_id``: valuations computed under any other configuration.
    """

    model_config = _FROZEN

    reason: InvalidationReason
    listing_id: UUID | None = None
    current_revision_id: UUID | None = None
    comparable_set_ids: tuple[UUID, ...] = ()
    market_observation_ids: tuple[UUID, ...] = ()
    fx_rate_ids: tuple[UUID, ...] = ()
    fx_pair: tuple[str, str, FxPurpose] | None = None
    fx_newer_than: date | None = None
    cost_evidence_ids: tuple[UUID, ...] = ()
    cost_profile_ids: tuple[UUID, ...] = ()
    tax_rule_set_ids: tuple[UUID, ...] = ()
    current_config_revision_id: UUID | None = None
    detail: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _shape(self) -> DependencyChange:
        if (self.listing_id is None) != (self.current_revision_id is None):
            raise ValueError("listing_id and current_revision_id go together")
        if (self.fx_pair is None) != (self.fx_newer_than is None):
            raise ValueError("fx_pair and fx_newer_than go together")
        if not any(
            (
                self.listing_id,
                self.comparable_set_ids,
                self.market_observation_ids,
                self.fx_rate_ids,
                self.fx_pair,
                self.cost_evidence_ids,
                self.cost_profile_ids,
                self.tax_rule_set_ids,
                self.current_config_revision_id,
            )
        ):
            raise ValueError("a dependency change names at least one changed dependency")
        return self

    @classmethod
    def new_fx_rate(cls, stored: StoredFxRate) -> DependencyChange:
        rate = stored.rate
        return cls(
            reason=InvalidationReason.FX,
            fx_pair=(rate.base, rate.quote, rate.purpose),
            fx_newer_than=rate.rate_date,
            detail=f"newer {rate.base}/{rate.quote} {rate.purpose.value} rate {rate.rate_date.isoformat()}",
        )


class InvalidationResult(BaseModel):
    model_config = _FROZEN

    stale_valuation_ids: tuple[UUID, ...]
    recompute_jobs: dict[UUID, UUID]  # listing id -> job id
    jobs_created: int
    more: bool


def _dependent_predicate(change: DependencyChange) -> tuple[sql.Composable, dict[str, Any]]:
    parts: list[sql.Composable] = []
    params: dict[str, Any] = {}
    if change.listing_id is not None:
        parts.append(
            sql.SQL("(v.listing_id = %(dep_listing)s and v.listing_revision_id <> %(dep_revision)s)")
        )
        params.update(dep_listing=change.listing_id, dep_revision=change.current_revision_id)
    if change.comparable_set_ids:
        parts.append(sql.SQL("v.comparable_set_id = any(%(dep_sets)s::uuid[])"))
        params["dep_sets"] = list(change.comparable_set_ids)
    if change.market_observation_ids:
        parts.append(
            sql.SQL(
                "v.comparable_set_id in (select m.comparable_set_id from app.comparable_set_members m"
                " where m.workspace_id = %(ws)s and m.market_observation_id = any(%(dep_obs)s::uuid[]))"
            )
        )
        params["dep_obs"] = list(change.market_observation_ids)
    if change.fx_rate_ids:
        parts.append(sql.SQL("v.fx_rate_ids && %(dep_fx)s::uuid[]"))
        params["dep_fx"] = list(change.fx_rate_ids)
    if change.fx_pair is not None:
        parts.append(
            sql.SQL(
                "exists (select 1 from app.fx_rates f where f.workspace_id = %(ws)s"
                " and f.id = any(v.fx_rate_ids)"
                " and f.base = %(dep_base)s and f.quote = %(dep_quote)s and f.purpose = %(dep_purpose)s"
                " and f.rate_date < %(dep_date)s)"
            )
        )
        base, quote, purpose = change.fx_pair
        params.update(
            dep_base=base, dep_quote=quote, dep_purpose=purpose.value, dep_date=change.fx_newer_than
        )
    if change.cost_evidence_ids:
        parts.append(sql.SQL("v.cost_evidence_ids && %(dep_ce)s::uuid[]"))
        params["dep_ce"] = list(change.cost_evidence_ids)
    if change.cost_profile_ids:
        parts.append(sql.SQL("v.cost_profile_id = any(%(dep_cp)s::uuid[])"))
        params["dep_cp"] = list(change.cost_profile_ids)
    if change.tax_rule_set_ids:
        parts.append(sql.SQL("v.tax_rule_set_id = any(%(dep_tax)s::uuid[])"))
        params["dep_tax"] = list(change.tax_rule_set_ids)
    if change.current_config_revision_id is not None:
        parts.append(sql.SQL("v.config_revision_id <> %(dep_config)s"))
        params["dep_config"] = change.current_config_revision_id
    return sql.SQL("(") + sql.SQL(" or ").join(parts) + sql.SQL(")"), params


async def find_dependents(
    conn: Conn, actor: ActorContext, change: DependencyChange, *, limit: int = 1000
) -> list[UUID]:
    """Ids of OPEN (not yet stale/invalid) valuations that depend on the change."""
    require_reader(actor)
    predicate, params = _dependent_predicate(change)
    query = sql.SQL(
        "select v.id from app.valuations v where v.workspace_id = %(ws)s and v.state = any(%(open)s::text[])"
        " and {predicate} order by v.id limit %(limit)s"
    ).format(predicate=predicate)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            query,
            {
                **params,
                "ws": actor.workspace_id,
                "open": [s.value for s in OPEN_VALUATION_STATES],
                "limit": limit,
            },
        )
    return [r["id"] for r in rows]


async def _enqueue_recompute(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    valuation_ids: Sequence[UUID],
    reason: InvalidationReason,
) -> tuple[UUID, bool]:
    """Queue the listing's recomputation (deduplicated). A recomputation that is already
    RUNNING may have read its inputs before this change, so it cannot absorb it: one follow-up
    job (``valuation.recompute:<listing_id>:after:<running job id>``) runs after it."""
    base_key = f"valuation.recompute:{listing_id}"
    payload = {
        "listing_id": str(listing_id),
        "stale_valuation_ids": [str(v) for v in valuation_ids[:100]],
        "reason": InvalidationReason(reason).value,
    }

    def spec(dedup_key: str) -> jobs.JobSpec:
        return jobs.JobSpec(
            job_type=JobType.VALUATION, dedup_key=dedup_key, payload=payload, listing_id=listing_id
        )

    job_id, created = await jobs.enqueue(conn, actor, spec(base_key))
    if created:
        return job_id, True
    async with mapped_errors():
        existing = await fetch_one(
            conn,
            "select state from ops.jobs where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": job_id},
        )
    if existing is not None and existing["state"] == JobState.RUNNING.value:
        return await jobs.enqueue(conn, actor, spec(f"{base_key}:after:{job_id}"))
    return job_id, False


async def invalidate_dependents(
    conn: Conn, actor: ActorContext, change: DependencyChange, *, limit: int = 1000
) -> InvalidationResult:
    """Reverse invalidation (spec 18): mark dependents stale NOW, then queue one deduplicated
    recomputation per affected listing, all in the caller's transaction."""
    require_writer(actor)
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid invalidation batch size")
    predicate, params = _dependent_predicate(change)
    text = stale_reason_text([change.reason], change.detail)
    query = sql.SQL(
        "with picked as ("
        " select v.id from app.valuations v where v.workspace_id = %(ws)s"
        " and v.state = any(%(open)s::text[]) and {predicate}"
        " order by v.id for update of v limit %(limit)s)"
        " update app.valuations u set state = 'stale', stale_at = clock_timestamp(),"
        " stale_reason = %(reason)s"
        " from picked where u.workspace_id = %(ws)s and u.id = picked.id"
        " returning u.id, u.listing_id"
    ).format(predicate=predicate)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            query,
            {
                **params,
                "ws": actor.workspace_id,
                "open": [s.value for s in OPEN_VALUATION_STATES],
                "limit": limit,
                "reason": text,
            },
        )
    by_listing: dict[UUID, list[UUID]] = {}
    for row in sorted(rows, key=lambda r: (str(r["listing_id"]), str(r["id"]))):
        by_listing.setdefault(row["listing_id"], []).append(row["id"])
    job_ids: dict[UUID, UUID] = {}
    created = 0
    for listing_id, valuation_ids in by_listing.items():
        job_id, was_created = await _enqueue_recompute(conn, actor, listing_id, valuation_ids, change.reason)
        job_ids[listing_id] = job_id
        created += int(was_created)
    if rows:
        async with mapped_errors():
            await audit.record(
                conn,
                actor,
                "valuation.invalidate",
                "valuation",
                None,
                reason=text,
                metadata={
                    "count": len(rows),
                    "valuation_ids": [str(r["id"]) for r in rows[:50]],
                    "jobs_created": created,
                },
            )
    stale_ids = tuple(r["id"] for r in rows)
    return InvalidationResult(
        stale_valuation_ids=stale_ids,
        recompute_jobs=job_ids,
        jobs_created=created,
        more=len(rows) >= limit,
    )


__all__ = [
    "OPEN_VALUATION_STATES",
    "CostEvidenceInput",
    "DependencyChange",
    "InvalidationResult",
    "StoredCostEvidence",
    "StoredCostProfile",
    "StoredFxRate",
    "StoredRuleSet",
    "StoredValuation",
    "TaxRuleRow",
    "ValuationInputs",
    "ValuationRefs",
    "ValuationStateChange",
    "approval_reference",
    "approve_cost_profile",
    "canonical_rate",
    "cost_line_from_evidence",
    "current_valuation",
    "find_cost_profile",
    "find_dependents",
    "get_cost_evidence",
    "get_fx_rates",
    "get_valuation_view",
    "insert_cost_evidence",
    "invalidate_dependents",
    "latest_fx_rates",
    "list_rule_sets",
    "load_cost_profile",
    "load_rule_set",
    "load_valuation",
    "mark_stale",
    "persist_valuation",
    "principal_label",
    "rule_row_key",
    "stale_reason_text",
    "store_cost_profile",
    "store_rule_set",
    "transition_tax_rule_set",
    "upsert_fx_rate",
]
