"""MK market evidence and reproducible comparable sets (spec sections 11, 15).

Tables: ``app.market_observations``, ``app.comparable_sets``, ``app.comparable_set_members``
(all append-only; a correction is a new row).

- **Evidence kinds stay distinct.** ``asking_price``, ``seller_reported_sale``,
  ``verified_sale`` and ``owner_estimate`` are stored exactly as the domain names them; an
  advertised price is never re-labelled as a sale. ``owner_estimate`` is recorded only by a
  human owner (``recorded_by`` is the authenticated principal, never a request field) and a
  ``verified_sale`` needs non-empty evidence.
- **Lossless storage.** ``normalized`` holds the full ``MarketObservation`` document (without
  its id, which is the row id); the typed columns are indexed copies for candidate lookup.
- **Comparable sets.** A ``ComparableSetResult`` is persisted as one ``app.comparable_sets``
  row plus one member row per selected or excluded candidate. Mapping:

  - criteria, target, as_of, status, widening steps, band fit, research flag, warnings and
    the content hash -> ``criteria`` (jsonb document);
  - ``ComparableSetResult.sample_quality`` (``adequate``/``small``/``insufficient``) ->
    ``sample_quality``; per-kind ``EvidenceStats`` -> ``statistics`` (``{"stats": [...]}``,
    currency ``EUR``);
  - selected member: ordinal, match level, differences, EUR amount, weight -> ``differences``
    (jsonb object) and ``weight``; excluded member: ordinal, duplicate_of, details ->
    ``differences``, reasons -> ``reasons[]``; widened dimensions -> ``widened_dimensions[]``.

  ``content_sha256`` = SHA-256 of the canonical JSON of the whole result; a loaded set is
  re-hashed and must match, so the ``ComparableReference.content_hash`` a valuation records
  is verifiable. Members keep their stable ordinal (selected first, then excluded).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.comparables import (
    MAX_CANDIDATES,
    ComparableSetResult,
    ComparableTarget,
    ExcludedComparable,
    MarketObservation,
    SelectedComparable,
)
from suv_deals.domain.enums import Confidence, EvidenceKind, Scope
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.valuation import ComparableReference
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.views.comparables import ComparableSetView, comparable_members

COMPARABLE_DOCUMENT_FORMAT: Final = "suv_deals.comparable_set/1"
OBSERVATION_DOCUMENT_FORMAT: Final = "suv_deals.market_observation/1"
MAX_MEMBERS_PAGE: Final = 100
_FROZEN = ConfigDict(frozen=True, extra="forbid")

ObservationConfidence = Literal["high", "medium", "low"]


def require_writer(actor: ActorContext) -> None:
    """Market/valuation records are written by system workers or the owner (``config:admin``)."""
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system workers or an owner may record market and valuation data")


def require_reader(actor: ActorContext) -> None:
    if actor.principal_kind != "system":
        actor.require(Scope.DEALS_READ)


# --------------------------------------------------------------------------------------------
# Market observations
# --------------------------------------------------------------------------------------------


class StoredMarketObservation(BaseModel):
    """One ``app.market_observations`` row with its lossless domain observation."""

    model_config = _FROZEN

    id: UUID
    observation: MarketObservation
    source_id: UUID | None
    listing_id: UUID | None
    source_listing_id: str | None
    confidence: ObservationConfidence
    evidence: dict[str, Any]
    recorded_by: UUID | None
    archived_snapshot_id: UUID | None
    is_fixture: bool
    created_at: datetime


_INSERT_OBSERVATION_SQL: Final = """
insert into app.market_observations (
  id, workspace_id, source_id, listing_id, source_listing_id, evidence_kind, market, amount_minor,
  currency, price_basis, normalized, make, model, vehicle_generation, facelift, engine_code,
  engine_displacement_cm3, power_kw, registration_year, fuel, gearbox, drive, mileage_km,
  local_registration_status, seller_type, cluster_id, observed_at, source_published_at, url,
  archived_snapshot_id, confidence, evidence, recorded_by, is_fixture)
values (
  %(id)s, %(workspace_id)s, %(source_id)s, %(listing_id)s, %(source_listing_id)s, %(evidence_kind)s,
  %(market)s, %(amount_minor)s, %(currency)s, %(price_basis)s, %(normalized)s, %(make)s, %(model)s,
  %(generation)s, %(facelift)s, %(engine_code)s, %(displacement)s, %(power_kw)s, %(year)s, %(fuel)s,
  %(gearbox)s, %(drive)s, %(mileage_km)s, %(registration)s, %(seller_type)s, %(cluster_id)s,
  %(observed_at)s, %(published_at)s, %(url)s, %(snapshot_id)s, %(confidence)s, %(evidence)s,
  %(recorded_by)s, %(is_fixture)s)
on conflict (id) do nothing
returning id
"""

_OBSERVATION_COLUMNS: Final = (
    "id, source_id, listing_id, source_listing_id, evidence_kind, market, amount_minor, currency,"
    " normalized, cluster_id, observed_at, url, confidence, evidence, recorded_by,"
    " archived_snapshot_id, is_fixture, created_at"
)


def _vehicle_year(observation: MarketObservation) -> int | None:
    vehicle = observation.vehicle
    if vehicle.first_registration.year is not None:
        return vehicle.first_registration.year
    return vehicle.model_year


def _observation_document(observation: MarketObservation) -> dict[str, Any]:
    body = observation.model_dump(mode="json", exclude={"id"})
    return {"format": OBSERVATION_DOCUMENT_FORMAT, "observation": body}


async def insert_market_observation(
    conn: Conn,
    actor: ActorContext,
    observation: MarketObservation,
    *,
    confidence: Confidence | ObservationConfidence,
    source_id: UUID | None = None,
    listing_id: UUID | None = None,
    source_listing_id: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    archived_snapshot_id: UUID | None = None,
    source_published_at: datetime | None = None,
) -> tuple[UUID, bool]:
    """Append one MK observation (id = ``observation.id``). Returns ``(id, created)``.

    A replay of the same observation id returns ``created=False`` when the stored document is
    identical and ``VERSION_CONFLICT`` otherwise (history is never rewritten).
    """
    require_writer(actor)
    kind = observation.evidence_kind
    evidence_doc = dict(evidence or {})
    recorded_by: UUID | None = None
    if kind == EvidenceKind.OWNER_ESTIMATE:
        if actor.principal_kind == "system":
            raise Forbidden("An owner estimate is recorded by the owner, never by a system worker")
        actor.require(Scope.CONFIG_ADMIN)
    if actor.principal_kind != "system":
        recorded_by = actor.principal_id  # from authentication only, never from the request
    if kind == EvidenceKind.VERIFIED_SALE and not evidence_doc:
        raise ValidationFailed("a verified sale needs its transaction evidence")
    if kind in (EvidenceKind.ASKING_PRICE, EvidenceKind.SELLER_REPORTED_SALE) and (
        source_id is None and observation.url is None
    ):
        raise ValidationFailed("public market evidence needs a source or a URL")
    if kind != EvidenceKind.SELLER_REPORTED_SALE and observation.amount is None:
        raise ValidationFailed("only a seller-reported sale may lack an amount")
    amount_minor = None if observation.amount is None else observation.amount.to_minor()
    vehicle = observation.vehicle
    document = _observation_document(observation)
    params = {
        "id": observation.id,
        "workspace_id": actor.workspace_id,
        "source_id": source_id,
        "listing_id": listing_id,
        "source_listing_id": source_listing_id,
        "evidence_kind": kind.value,
        "market": observation.market,
        "amount_minor": amount_minor,
        "currency": None if observation.amount is None else observation.amount.currency,
        "price_basis": observation.price_basis.value,
        "normalized": Jsonb(document),
        "make": vehicle.make,
        "model": vehicle.model,
        "generation": vehicle.generation,
        "facelift": vehicle.facelift.value,
        "engine_code": vehicle.engine_code,
        "displacement": vehicle.engine_displacement_cm3,
        "power_kw": vehicle.power_kw,
        "year": _vehicle_year(observation),
        "fuel": vehicle.fuel.value,
        "gearbox": vehicle.gearbox.value,
        "drive": vehicle.drive.value,
        "mileage_km": vehicle.mileage_km,
        "registration": observation.local_registration_status,
        "seller_type": observation.seller_type.value,
        "cluster_id": observation.cluster_id,
        "observed_at": observation.observed_at,
        "published_at": None if source_published_at is None else _aware(source_published_at),
        "url": observation.url,
        "snapshot_id": archived_snapshot_id,
        "confidence": str(getattr(confidence, "value", confidence)),
        "evidence": Jsonb(evidence_doc),
        "recorded_by": recorded_by,
        "is_fixture": observation.is_fixture,
    }
    async with mapped_errors():
        if source_id is not None:
            source = await fetch_one(
                conn,
                "select source_key from app.sources where workspace_id = %(ws)s and id = %(id)s",
                {"ws": actor.workspace_id, "id": source_id},
            )
            if source is None:
                raise NotFound("Source not found")
            if source["source_key"] != observation.source_key:
                raise ValidationFailed("observation source_key does not match the source")
        row = await fetch_one(conn, _INSERT_OBSERVATION_SQL, params)
        if row is not None:
            await audit.record(
                conn,
                actor,
                "market.observation_record",
                "market_observation",
                observation.id,
                metadata={"evidence_kind": kind.value, "is_fixture": observation.is_fixture},
            )
            return observation.id, True
        existing = await fetch_one(
            conn,
            "select normalized from app.market_observations where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": observation.id},
        )
    if existing is None:
        # The id exists in another workspace: never reveal it, never write.
        raise ValidationFailed("observation id is not available; use a fresh id")
    if existing["normalized"] != document:
        raise AppError(
            ErrorCode.VERSION_CONFLICT,
            "A different observation with this id is already recorded",
            retryable=False,
        )
    return observation.id, False


def _observation_from_row(row: Mapping[str, Any]) -> StoredMarketObservation:
    doc = row["normalized"]
    body = doc.get("observation") if isinstance(doc, Mapping) else None
    if not isinstance(body, Mapping) or doc.get("format") != OBSERVATION_DOCUMENT_FORMAT:
        raise ValidationFailed("market observation document is not in the repository format")
    try:
        observation = MarketObservation.model_validate({**body, "id": row["id"]})
    except ValidationError as exc:
        raise ValidationFailed("stored market observation is invalid") from exc
    if observation.evidence_kind.value != row["evidence_kind"] or observation.is_fixture != row["is_fixture"]:
        raise ValidationFailed("stored market observation columns disagree with its document")
    return StoredMarketObservation(
        id=row["id"],
        observation=observation,
        source_id=row["source_id"],
        listing_id=row["listing_id"],
        source_listing_id=row["source_listing_id"],
        confidence=row["confidence"],
        evidence=dict(row["evidence"] or {}),
        recorded_by=row["recorded_by"],
        archived_snapshot_id=row["archived_snapshot_id"],
        is_fixture=row["is_fixture"],
        created_at=ensure_utc(row["created_at"]),
    )


async def get_market_observations(
    conn: Conn, actor: ActorContext, ids: Sequence[UUID]
) -> dict[UUID, StoredMarketObservation]:
    """Observations by id (foreign/missing ids are simply absent)."""
    require_reader(actor)
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    if len(wanted) > MAX_CANDIDATES:
        raise ValidationFailed("too many observation ids")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_OBSERVATION_COLUMNS} from app.market_observations"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s and id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": wanted},
        )
    return {r["id"]: _observation_from_row(r) for r in rows}


async def list_candidate_comparables(
    conn: Conn,
    actor: ActorContext,
    target: ComparableTarget,
    *,
    as_of: datetime,
    max_age_days: int,
    year_window: int,
    market: str = "MK",
    limit: int = MAX_CANDIDATES,
) -> list[MarketObservation]:
    """Candidate MK evidence for ``target`` by the indexed dimensions (spec 11/15).

    Narrows by workspace, market, exact make and model, registration year within
    ``year_window`` (unknown years are kept so their exclusion is recorded), and observation
    time in ``(as_of - max_age_days, as_of]``. Fuel/gearbox/drive/engine/condition are left to
    ``domain.comparables.select_comparables`` so every mismatch is recorded as an exclusion.
    """
    require_reader(actor)
    if target.make is None or target.model is None:
        return []
    if not 1 <= max_age_days <= 3650 or not 0 <= year_window <= 10 or not 1 <= limit <= MAX_CANDIDATES:
        raise ValidationFailed("invalid comparable candidate query")
    when = _aware(as_of)
    params = {
        "ws": actor.workspace_id,
        "market": market,
        "make": target.make,
        "model": target.model,
        "has_year": target.year is not None,
        "year_lo": (target.year or 0) - year_window,
        "year_hi": (target.year or 0) + year_window,
        "as_of": when,
        "oldest": when - timedelta(days=max_age_days),
        "limit": limit,
    }
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_OBSERVATION_COLUMNS} from app.market_observations"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s and market = %(market)s and make = %(make)s"
            " and model = %(model)s"
            " and (not %(has_year)s or registration_year is null"
            "      or registration_year between %(year_lo)s and %(year_hi)s)"
            " and observed_at <= %(as_of)s and observed_at > %(oldest)s"
            " order by observed_at desc, id limit %(limit)s",
            params,
        )
    return [_observation_from_row(r).observation for r in rows]


# --------------------------------------------------------------------------------------------
# Comparable sets
# --------------------------------------------------------------------------------------------


class StoredComparableSet(BaseModel):
    """A loaded comparable set: the exact domain result plus its row identity."""

    model_config = _FROZEN

    id: UUID
    listing_id: UUID
    target_revision_id: UUID
    result: ComparableSetResult
    content_sha256: str
    is_fixture: bool
    sample_quality: str
    excluded_observations: dict[UUID, MarketObservation]
    computed_at: datetime
    created_at: datetime

    def reference(self) -> ComparableReference:
        """The ``ComparableReference`` a valuation records for this set (fresh until the oldest
        selected member exceeds the maximum evidence age)."""
        return comparable_reference(self.id, self.result, self.content_sha256)


def comparable_content_sha256(result: ComparableSetResult) -> str:
    return sha256_json(result.model_dump(mode="json"))


def comparable_reference(
    set_id: UUID, result: ComparableSetResult, content_sha256: str
) -> ComparableReference:
    fresh_until: datetime | None = None
    if result.selected:
        oldest = min(s.observation.observed_at for s in result.selected)
        fresh_until = oldest + timedelta(days=result.criteria.max_age_days)
    return ComparableReference(
        comparable_set_id=str(set_id),
        content_hash=content_sha256,
        sample_size=len(result.selected),
        quality=ComparableReference.quality_from_status(result.status),
        fresh_until=fresh_until,
        is_fixture=result.target.is_fixture,
    )


def _set_document(result: ComparableSetResult, content_sha256: str) -> dict[str, Any]:
    dump = result.model_dump(mode="json", exclude={"selected", "excluded", "stats"})
    return {"format": COMPARABLE_DOCUMENT_FORMAT, "content_sha256": content_sha256, "result": dump}


_INSERT_SET_SQL: Final = """
insert into app.comparable_sets (
  workspace_id, listing_id, target_revision_id, criteria_version, criteria, sample_size,
  selected_count, excluded_count, sample_quality, currency, statistics, date_span_from,
  date_span_to, rationale, is_fixture, computed_at)
values (
  %(ws)s, %(listing_id)s, %(revision_id)s, %(criteria_version)s, %(criteria)s, %(sample_size)s,
  %(selected)s, %(excluded)s, %(quality)s, %(currency)s, %(statistics)s, %(span_from)s, %(span_to)s,
  %(rationale)s, %(is_fixture)s, %(computed_at)s)
returning id, created_at
"""

_INSERT_MEMBER_SQL: Final = """
insert into app.comparable_set_members (
  workspace_id, comparable_set_id, market_observation_id, disposition, reasons, differences,
  widened_dimensions, weight)
values (%(ws)s, %(set_id)s, %(observation_id)s, %(disposition)s, %(reasons)s, %(differences)s,
        %(widened)s, %(weight)s)
"""


def _selected_member(item: SelectedComparable, ordinal: int) -> dict[str, Any]:
    return {
        "observation_id": item.observation_id,
        "disposition": "selected",
        "reasons": [],
        "differences": {
            "ordinal": ordinal,
            "evidence_kind": item.evidence_kind.value,
            "match_level": item.match_level,
            "weight": format(item.weight, "f"),
            "amount_eur": format(item.amount_eur, "f"),
            "differences": [d.model_dump(mode="json") for d in item.differences],
        },
        "widened": list(item.widened_dimensions),
        "weight": item.weight,
    }


def _excluded_member(item: ExcludedComparable, ordinal: int) -> dict[str, Any]:
    return {
        "observation_id": item.observation_id,
        "disposition": "excluded",
        "reasons": [r.value for r in item.reasons],
        "differences": {
            "ordinal": ordinal,
            "evidence_kind": item.evidence_kind.value,
            "duplicate_of": None if item.duplicate_of is None else str(item.duplicate_of),
            "details": list(item.details),
        },
        "widened": [],
        "weight": None,
    }


async def persist_comparable_set(
    conn: Conn,
    actor: ActorContext,
    result: ComparableSetResult,
    *,
    listing_id: UUID,
    target_revision_id: UUID,
    rationale: str | None = None,
) -> StoredComparableSet:
    """Persist one reproducible comparable selection for a target revision (append-only)."""
    require_writer(actor)
    if result.target.listing_id is not None and result.target.listing_id != listing_id:
        raise ValidationFailed("the comparable target belongs to another listing")
    if len(result.selected) + len(result.excluded) > MAX_CANDIDATES:
        raise ValidationFailed("too many comparable members")
    content_sha256 = comparable_content_sha256(result)
    span = result.date_span
    text = rationale or (
        f"{result.status}: {len(result.selected)} selected, {len(result.excluded)} excluded "
        f"({result.criteria_version})"
    )
    params = {
        "ws": actor.workspace_id,
        "listing_id": listing_id,
        "revision_id": target_revision_id,
        "criteria_version": result.criteria_version,
        "criteria": Jsonb(_set_document(result, content_sha256)),
        "sample_size": len(result.selected),
        "selected": len(result.selected),
        "excluded": len(result.excluded),
        "quality": result.sample_quality,
        "currency": "EUR" if result.stats else None,
        "statistics": Jsonb({"stats": [s.model_dump(mode="json") for s in result.stats]})
        if result.stats
        else None,
        "span_from": None if span is None else span[0],
        "span_to": None if span is None else span[1],
        "rationale": text[:4000],
        "is_fixture": result.target.is_fixture,
        "computed_at": result.as_of,
    }
    members = [_selected_member(s, i) for i, s in enumerate(result.selected)]
    offset = len(members)
    members += [_excluded_member(e, offset + i) for i, e in enumerate(result.excluded)]
    async with mapped_errors():
        row = await fetch_one(conn, _INSERT_SET_SQL, params)
        assert row is not None
        set_id: UUID = row["id"]
        if members:
            async with conn.cursor() as cur:
                await cur.executemany(
                    _INSERT_MEMBER_SQL,
                    [
                        {
                            **m,
                            "ws": actor.workspace_id,
                            "set_id": set_id,
                            "differences": Jsonb(m["differences"]),
                        }
                        for m in members
                    ],
                )
        await audit.record(
            conn,
            actor,
            "comparables.set_record",
            "comparable_set",
            set_id,
            metadata={
                "status": result.status,
                "selected": len(result.selected),
                "excluded": len(result.excluded),
                "content_sha256": content_sha256,
            },
        )
    excluded_ids = [e.observation_id for e in result.excluded]
    excluded_obs = await _observations(conn, actor, excluded_ids)
    return StoredComparableSet(
        id=set_id,
        listing_id=listing_id,
        target_revision_id=target_revision_id,
        result=result,
        content_sha256=content_sha256,
        is_fixture=result.target.is_fixture,
        sample_quality=result.sample_quality,
        excluded_observations=excluded_obs,
        computed_at=result.as_of,
        created_at=ensure_utc(row["created_at"]),
    )


async def _observations(
    conn: Conn, actor: ActorContext, ids: Sequence[UUID]
) -> dict[UUID, MarketObservation]:
    if not ids:
        return {}
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_OBSERVATION_COLUMNS} from app.market_observations"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s and id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": list(ids)},
        )
    return {r["id"]: _observation_from_row(r).observation for r in rows}


async def load_comparable_set(conn: Conn, actor: ActorContext, set_id: UUID) -> StoredComparableSet:
    """Rebuild the exact ``ComparableSetResult`` (content hash verified). Foreign -> NotFound."""
    require_reader(actor)
    params = {"ws": actor.workspace_id, "id": set_id}
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id, listing_id, target_revision_id, criteria, statistics, sample_quality,"
            " is_fixture, computed_at, created_at from app.comparable_sets"
            " where workspace_id = %(ws)s and id = %(id)s",
            params,
        )
        if row is None:
            raise NotFound("Comparable set not found")
        members = await fetch_all(
            conn,
            "select market_observation_id, disposition, reasons, differences, widened_dimensions, weight"
            " from app.comparable_set_members where workspace_id = %(ws)s and comparable_set_id = %(id)s"
            " order by (differences ->> 'ordinal')::int, id",
            params,
        )
    doc = row["criteria"]
    if not isinstance(doc, Mapping) or doc.get("format") != COMPARABLE_DOCUMENT_FORMAT:
        raise ValidationFailed("comparable set document is not in the repository format")
    observations = await _observations(conn, actor, [m["market_observation_id"] for m in members])
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for member in members:
        diff = member["differences"]
        obs_id = member["market_observation_id"]
        if member["disposition"] == "selected":
            observation = observations.get(obs_id)
            if observation is None:
                raise ValidationFailed("a selected comparable observation is missing")
            selected.append(
                {
                    "observation_id": obs_id,
                    "evidence_kind": diff["evidence_kind"],
                    "weight": diff["weight"],
                    "match_level": diff["match_level"],
                    "differences": diff["differences"],
                    "widened_dimensions": list(member["widened_dimensions"]),
                    "amount_eur": diff["amount_eur"],
                    "observation": observation,
                }
            )
        else:
            excluded.append(
                {
                    "observation_id": obs_id,
                    "evidence_kind": diff["evidence_kind"],
                    "reasons": list(member["reasons"]),
                    "duplicate_of": diff.get("duplicate_of"),
                    "details": diff.get("details", []),
                }
            )
    stats = (row["statistics"] or {}).get("stats", [])
    try:
        result = ComparableSetResult.model_validate(
            {**doc["result"], "selected": selected, "excluded": excluded, "stats": stats}
        )
    except ValidationError as exc:
        raise ValidationFailed("stored comparable set is invalid") from exc
    content = comparable_content_sha256(result)
    if content != doc.get("content_sha256"):
        raise ValidationFailed("stored comparable set does not match its content hash")
    excluded_obs = {k: v for k, v in observations.items() if k in {e.observation_id for e in result.excluded}}
    return StoredComparableSet(
        id=row["id"],
        listing_id=row["listing_id"],
        target_revision_id=row["target_revision_id"],
        result=result,
        content_sha256=content,
        is_fixture=row["is_fixture"],
        sample_quality=row["sample_quality"],
        excluded_observations=excluded_obs,
        computed_at=ensure_utc(row["computed_at"]),
        created_at=ensure_utc(row["created_at"]),
    )


async def get_comparable_set_view(
    conn: Conn,
    actor: ActorContext,
    set_id: UUID,
    *,
    include_excluded: bool = False,
    ordinal_start: int = 0,
    limit: int = 25,
) -> tuple[ComparableSetView, int | None]:
    """``deals_get_comparables`` data: one page of members by stable ordinal plus the next
    ordinal (``None`` on the last page). The API layer wraps the ordinal in a signed cursor."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= MAX_MEMBERS_PAGE or ordinal_start < 0:
        raise ValidationFailed("invalid comparable member page")
    stored = await load_comparable_set(conn, actor, set_id)
    members = comparable_members(
        stored.result, include_excluded=include_excluded, excluded_observations=stored.excluded_observations
    )
    page = members[ordinal_start : ordinal_start + limit]
    next_ordinal = ordinal_start + len(page) if ordinal_start + len(page) < len(members) else None
    view = ComparableSetView.of(
        stored.result,
        comparable_set_id=stored.id,
        listing_id=stored.listing_id,
        target_revision_id=stored.target_revision_id,
        include_excluded=include_excluded,
        members=page,
    )
    return view, next_ordinal


async def comparable_sets_with_observation(
    conn: Conn, actor: ActorContext, observation_ids: Sequence[UUID]
) -> list[UUID]:
    """Comparable sets that contain any of the observations (reverse invalidation input)."""
    require_reader(actor)
    if not observation_ids:
        return []
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select distinct comparable_set_id from app.comparable_set_members"
            " where workspace_id = %(ws)s and market_observation_id = any(%(ids)s::uuid[])",
            {"ws": actor.workspace_id, "ids": list(observation_ids)},
        )
    return [r["comparable_set_id"] for r in rows]


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed("timestamps must be timezone-aware") from exc


__all__ = [
    "COMPARABLE_DOCUMENT_FORMAT",
    "OBSERVATION_DOCUMENT_FORMAT",
    "StoredComparableSet",
    "StoredMarketObservation",
    "comparable_content_sha256",
    "comparable_reference",
    "comparable_sets_with_observation",
    "get_comparable_set_view",
    "get_market_observations",
    "insert_market_observation",
    "list_candidate_comparables",
    "load_comparable_set",
    "persist_comparable_set",
    "require_reader",
    "require_writer",
]
