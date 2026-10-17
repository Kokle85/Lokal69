"""The owner's recorded import values of a listing (owner decision 2026-10-13: two separate values).

Each vehicle keeps two values apart (ADR 0003 decision 13, fact sheet 3.4):

- the REAL purchase price (реална набавна цена; category ``purchase``): what is, or will be,
  actually paid. Valuations use it for the profit and the landed cost (``PurchaseInput``, status
  ``actual`` / ``quoted`` / ``estimated`` by the recorded ``kind``) instead of the advertised price;
- the ESTIMATED customs value (проценета царинска вредност; category ``customs_value``, always kind
  ``estimate``): the value of the vehicle for customs in EUR WITHOUT transport (e.g. the
  SchwackeNet catalogue value Customs uses for used vehicles). The import charges are computed on
  it (plus the transport to the MK place of entry); the transaction value stays the alternative.

Storage: ``app.cost_evidence`` (append-only; migration ``20261013000100_import_values``), one row
per recorded value: ``base_minor`` + ``currency`` EUR, ``provider`` = the source label (e.g.
"seller invoice", "SchwackeNet catalogue"), ``scope`` = the listing, ``document_ref`` (redacted),
``recorded_by`` = the owner, ``is_fixture`` = the listing's frozen lineage. A new value SUPERSEDES
the listing's current row of the same category (``supersedes_id``), so each listing has one
linear chain per category (partial unique indexes of that migration are the backstop) and the
current value is the chain's newest row. Neither category is a ``CostCategory`` for this
module's purpose: the cost profile, its term mapping and ``valuation_repo.get_cost_evidence``
never see these rows.

- `record_import_values`: OWNER only (a signed-in user with ``config:admin``; system workers and
  MCP clients never record them). Locks the listing row (``FOR NO KEY UPDATE``; the valuation
  commit takes the same lock, so recording and persisting a valuation serialise, and under it the
  commit re-reads the current values and refuses to persist a calculation that read others:
  `valuation_pipeline._check_import_values`), checks it
  belongs to the workspace (otherwise ``NotFound``), inserts the superseding row(s), audits
  (``import_values.record``) and invalidates the listing's valuations
  (``valuation_repo.invalidate_listing``: open valuations stale now, one deduplicated
  recomputation queued even when none was open). Idempotency is the caller's (the API route).
- `current_import_values`: ``deals:read`` (or a system worker): the current purchase price and
  customs value plus a short history, newest first.

Lock order: listing -> cost evidence -> audit -> valuations -> jobs (``valuation_repo``).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.costs import SOURCE_LABEL_MAX_LENGTH, CostScope, SourceLabel
from suv_deals.domain.enums import CostLineStatus, Scope
from suv_deals.domain.import_estimate import (
    PriceFact,
    PurchasePriceBasis,
    VehicleImportFacts,
    plain_amount_text,
)
from suv_deals.domain.money import Money
from suv_deals.domain.tax_engine import MAX_INPUT_AMOUNT
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound, ValidationFailed
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit, valuation_repo
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.market_repo import require_reader

ImportValueCategory = Literal["purchase", "customs_value"]
#: How firm a recorded purchase price is: paid (``actual``), offered/agreed but not paid
#: (``quote``) or the owner's estimate (``estimate``). An estimated customs value is always an
#: ``estimate``.
RecordedKind = Literal["actual", "quote", "estimate"]

#: The categories this module owns, in a fixed order.
IMPORT_VALUE_CATEGORIES: Final[tuple[ImportValueCategory, ...]] = ("purchase", "customs_value")
#: Rows ``current_import_values`` returns as history by default (newest first).
DEFAULT_HISTORY_LIMIT: Final = 10
MAX_HISTORY_LIMIT: Final = 50
MAX_DOCUMENT_REF: Final = 500
#: The price basis of the calculator for each recorded purchase kind.
PRICE_BASIS: Final[Mapping[str, PurchasePriceBasis]] = {
    "actual": "recorded_actual",
    "quote": "recorded_quote",
    "estimate": "recorded_estimate",
}
#: The valuation purchase status for each recorded purchase kind.
PURCHASE_STATUS: Final[Mapping[str, CostLineStatus]] = {
    "actual": CostLineStatus.ACTUAL,
    "quote": CostLineStatus.QUOTED,
    "estimate": CostLineStatus.ESTIMATED,
}
AUDIT_ACTION: Final = "import_values.record"
_FROZEN = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
#: Control, format (bidirectional overrides, zero-width), surrogate and unassigned characters.
_HIDDEN: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _no_float(data: Any, *names: str) -> Any:
    if isinstance(data, dict):
        for name in names:
            if isinstance(data.get(name), float):
                raise ValueError(f"{name}: binary float is not allowed; use a decimal string")
    return data


def _document_ref(value: str) -> str:
    cleaned = redact(value).strip()
    if not cleaned:
        raise ValueError("a document reference has content")
    if any(unicodedata.category(ch) in _HIDDEN for ch in cleaned):
        raise ValueError("a document reference is one plain line (no control or format characters)")
    return cleaned[:MAX_DOCUMENT_REF]


DocumentRef = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_DOCUMENT_REF), AfterValidator(_document_ref)
]
#: A recorded amount in EUR: positive (zero is never a recorded value), at most two decimals,
#: within the tax engine's input bound.
RecordedAmount = Annotated[Decimal, Field(gt=0, le=MAX_INPUT_AMOUNT, decimal_places=2, allow_inf_nan=False)]


class RecordedPurchase(BaseModel):
    """The REAL purchase price to record (EUR only; a price in another currency is converted by
    the owner at the rate actually paid, never here)."""

    model_config = _FROZEN

    amount: RecordedAmount
    currency: Literal["EUR"] = "EUR"
    kind: RecordedKind
    source: SourceLabel
    document_ref: DocumentRef | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        return _no_float(data, "amount")


class RecordedCustomsValue(BaseModel):
    """The ESTIMATED customs value of the vehicle to record, in EUR, WITHOUT transport."""

    model_config = _FROZEN

    amount_eur: RecordedAmount
    source: SourceLabel
    document_ref: DocumentRef | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        return _no_float(data, "amount_eur")


class ImportValueRecord(BaseModel):
    """One recorded import value (an ``app.cost_evidence`` row of category purchase/customs_value)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    listing_id: UUID
    category: ImportValueCategory
    kind: RecordedKind
    amount: Money
    source: str
    document_ref: str | None
    supersedes_id: UUID | None
    #: False once a newer value of the same category superseded it.
    current: bool
    recorded_by: UUID | None
    recorded_at: datetime
    is_fixture: bool

    @property
    def price_basis(self) -> PurchasePriceBasis:
        """The calculator's price basis of a recorded purchase price."""
        return PRICE_BASIS[self.kind]

    @property
    def purchase_status(self) -> CostLineStatus:
        """The valuation purchase status of a recorded purchase price."""
        return PURCHASE_STATUS[self.kind]

    def describe(self) -> str:
        """``recorded actual purchase price EUR 2800 (seller invoice, 2026-10-13)``; a real fraction
        stays exact (``EUR 2800.50``)."""
        what = (
            f"recorded {self.kind} purchase price"
            if self.category == "purchase"
            else "recorded estimated customs value"
        )
        amount = plain_amount_text(self.amount)
        return f"{what} EUR {amount} ({self.source}, {self.recorded_at.date().isoformat()})"


class ImportValues(BaseModel):
    """The current recorded values of one listing and a short history (newest first)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: UUID
    purchase: ImportValueRecord | None
    customs_value: ImportValueRecord | None
    history: tuple[ImportValueRecord, ...]

    @property
    def current_ids(self) -> tuple[UUID, ...]:
        return tuple(r.id for r in (self.purchase, self.customs_value) if r is not None)


class ImportValuesRecorded(BaseModel):
    """`record_import_values` result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: ImportValues
    recorded_ids: tuple[UUID, ...]
    superseded_ids: tuple[UUID, ...]
    stale_valuation_ids: tuple[UUID, ...]
    recompute_job_id: UUID


#: Calculator facts a recorded import value supplies, per category.
RECORDED_FACTS: Final[Mapping[str, tuple[str, ...]]] = {
    "purchase": ("price", "price_basis"),
    "customs_value": ("estimated_customs_value_eur", "estimated_customs_value_source"),
}


def apply_recorded_values(
    facts: VehicleImportFacts, values: ImportValues
) -> tuple[VehicleImportFacts, frozenset[str]]:
    """``facts`` with the listing's CURRENT recorded real purchase price (instead of the advertised
    price; basis ``recorded_<kind>``) and estimated customs value, and the names of the facts they
    supplied. The calculator's listing mode and the valuation pipeline both use it."""
    updates: dict[str, object] = {}
    if values.purchase is not None:
        updates["price"] = PriceFact(amount=values.purchase.amount.amount, currency="EUR")
        updates["price_basis"] = values.purchase.price_basis
    if values.customs_value is not None:
        updates["estimated_customs_value_eur"] = values.customs_value.amount.amount
        updates["estimated_customs_value_source"] = values.customs_value.source
    if not updates:
        return facts, frozenset()
    try:
        merged = VehicleImportFacts.model_validate({**facts.model_dump(), **updates})
    except ValidationError:  # pragma: no cover - recorded values are validated when recorded
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "The recorded import values could not be read", retryable=False
        ) from None
    names = frozenset(
        name
        for category, record in (("purchase", values.purchase), ("customs_value", values.customs_value))
        if record is not None
        for name in RECORDED_FACTS[category]
    )
    return merged, names


_COLUMNS: Final = (
    "c.id, c.listing_id, c.category, c.kind, c.base_minor, c.currency, c.provider, c.document_ref,"
    " c.supersedes_id, c.recorded_by, c.created_at, c.is_fixture,"
    " not exists (select 1 from app.cost_evidence n where n.workspace_id = c.workspace_id"
    " and n.supersedes_id = c.id) as current"
)
_CURRENT_SQL: Final = (
    f"select distinct on (c.category) {_COLUMNS} from app.cost_evidence c"  # noqa: S608 - fixed column list
    " where c.workspace_id = %(ws)s and c.listing_id = %(listing)s"
    " and c.category = any(%(categories)s::text[])"
    " and not exists (select 1 from app.cost_evidence n where n.workspace_id = c.workspace_id"
    " and n.supersedes_id = c.id)"
    " order by c.category, c.created_at desc, c.id desc"
)
_HISTORY_SQL: Final = (
    f"select {_COLUMNS} from app.cost_evidence c"  # noqa: S608 - fixed column list
    " where c.workspace_id = %(ws)s and c.listing_id = %(listing)s"
    " and c.category = any(%(categories)s::text[])"
    " order by c.created_at desc, c.id desc limit %(limit)s"
)
_INSERT_SQL: Final = (
    "insert into app.cost_evidence (workspace_id, kind, category, provider, base_minor, currency,"
    " obtained_at, scope, evidence, document_ref, listing_id, supersedes_id, recorded_by, is_fixture)"
    " values (%(ws)s, %(kind)s, %(category)s, %(source)s, %(base)s, 'EUR', clock_timestamp(), %(scope)s,"
    " %(evidence)s, %(document_ref)s, %(listing)s, %(supersedes)s, %(recorded_by)s, %(fixture)s)"
    " returning id"
)


def _record(row: Mapping[str, Any]) -> ImportValueRecord:
    if row["currency"] != "EUR" or row["base_minor"] is None:  # pragma: no cover - written EUR only
        raise ValidationFailed("a recorded import value is a base amount in EUR")
    return ImportValueRecord(
        id=row["id"],
        listing_id=row["listing_id"],
        category=row["category"],
        kind=row["kind"],
        amount=Money.from_minor(row["base_minor"], "EUR"),
        source=row["provider"] or "unnamed source",
        document_ref=row["document_ref"],
        supersedes_id=row["supersedes_id"],
        current=bool(row["current"]),
        recorded_by=row["recorded_by"],
        recorded_at=ensure_utc(row["created_at"]),
        is_fixture=row["is_fixture"],
    )


def _require_owner(actor: ActorContext) -> None:
    if actor.principal_kind != "user":
        raise Forbidden(
            "Recording import values needs the signed-in owner; workers and MCP clients never do it"
        )
    actor.require(Scope.CONFIG_ADMIN)


async def _read(conn: Conn, actor: ActorContext, listing_id: UUID, history_limit: int) -> ImportValues:
    params = {
        "ws": actor.workspace_id,
        "listing": listing_id,
        "categories": list(IMPORT_VALUE_CATEGORIES),
        "limit": history_limit,
    }
    async with mapped_errors():
        current = [_record(r) for r in await fetch_all(conn, _CURRENT_SQL, params)]
        history = [_record(r) for r in await fetch_all(conn, _HISTORY_SQL, params)] if history_limit else []
    by_category = {r.category: r for r in current}
    return ImportValues(
        listing_id=listing_id,
        purchase=by_category.get("purchase"),
        customs_value=by_category.get("customs_value"),
        history=tuple(history),
    )


async def current_import_values(
    conn: Conn, actor: ActorContext, listing_id: UUID, *, history_limit: int = DEFAULT_HISTORY_LIMIT
) -> ImportValues:
    """The listing's current recorded purchase price and estimated customs value with a short
    history (newest first). A missing or foreign-workspace listing is ``NotFound``."""
    require_reader(actor)
    if not 0 <= history_limit <= MAX_HISTORY_LIMIT:
        raise ValidationFailed(f"history_limit must be between 0 and {MAX_HISTORY_LIMIT}")
    async with mapped_errors():
        exists = await fetch_one(
            conn,
            "select 1 as found from app.listings where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": listing_id},
        )
    if exists is None:
        raise NotFound("Listing not found")
    return await _read(conn, actor, listing_id, history_limit)


async def record_import_values(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    *,
    purchase: RecordedPurchase | None = None,
    customs_value: RecordedCustomsValue | None = None,
) -> ImportValuesRecorded:
    """Record the listing's real purchase price and/or estimated customs value (module docstring).

    Each given value supersedes the current row of its category; the listing's valuations are
    invalidated and its recomputation queued, all in the caller's transaction."""
    _require_owner(actor)
    if purchase is None and customs_value is None:
        raise ValidationFailed(
            "record a purchase price, an estimated customs value or both",
            details={"fields": ["purchase_price", "estimated_customs_value"]},
        )
    ws = actor.workspace_id
    async with mapped_errors():
        listing = await fetch_one(
            conn,
            "select id, is_fixture from app.listings where workspace_id = %(ws)s and id = %(id)s"
            " for no key update",
            {"ws": ws, "id": listing_id},
        )
        if listing is None:
            raise NotFound("Listing not found")
        before = await _read(conn, actor, listing_id, 0)
        scope = Jsonb(CostScope(listing_id=str(listing_id)).model_dump(mode="json"))
        planned: list[
            tuple[ImportValueCategory, str, Decimal, str, str | None, ImportValueRecord | None]
        ] = []
        if purchase is not None:
            planned.append(
                (
                    "purchase",
                    purchase.kind,
                    purchase.amount,
                    purchase.source,
                    purchase.document_ref,
                    before.purchase,
                )
            )
        if customs_value is not None:
            planned.append(
                (
                    "customs_value",
                    "estimate",
                    customs_value.amount_eur,
                    customs_value.source,
                    customs_value.document_ref,
                    before.customs_value,
                )
            )
        recorded: list[UUID] = []
        superseded: list[UUID] = []
        for category, kind, amount, source, document_ref, previous in planned:
            row = await fetch_one(
                conn,
                _INSERT_SQL,
                {
                    "ws": ws,
                    "kind": kind,
                    "category": category,
                    "source": redact(source)[:SOURCE_LABEL_MAX_LENGTH].strip(),
                    "base": Money(amount=amount, currency="EUR").to_minor(),
                    "scope": scope,
                    "evidence": Jsonb({"document_sha256": None, "details": {"import_value": category}}),
                    "document_ref": document_ref,
                    "listing": listing_id,
                    "supersedes": None if previous is None else previous.id,
                    "recorded_by": actor.principal_id,
                    "fixture": bool(listing["is_fixture"]),
                },
            )
            assert row is not None
            recorded.append(row["id"])
            if previous is not None:
                superseded.append(previous.id)
        await audit.record(
            conn,
            actor,
            AUDIT_ACTION,
            "listing",
            listing_id,
            metadata={
                "recorded": [
                    {"category": category, "kind": kind, "amount_eur": str(amount), "source": source}
                    for category, kind, amount, source, _ref, _prev in planned
                ],
                "cost_evidence_ids": [str(i) for i in recorded],
                "superseded_ids": [str(i) for i in superseded],
            },
        )
    invalidation = await valuation_repo.invalidate_listing(
        conn,
        actor,
        listing_id,
        InvalidationReason.COST_QUOTE,
        detail="recorded import values changed ("
        + ", ".join("purchase price" if c == "purchase" else "estimated customs value" for c, *_ in planned)
        + ")",
    )
    values = await _read(conn, actor, listing_id, DEFAULT_HISTORY_LIMIT)
    return ImportValuesRecorded(
        values=values,
        recorded_ids=tuple(recorded),
        superseded_ids=tuple(superseded),
        stale_valuation_ids=invalidation.stale_valuation_ids,
        recompute_job_id=invalidation.recompute_job_id,
    )


__all__ = [
    "AUDIT_ACTION",
    "DEFAULT_HISTORY_LIMIT",
    "IMPORT_VALUE_CATEGORIES",
    "MAX_HISTORY_LIMIT",
    "PRICE_BASIS",
    "PURCHASE_STATUS",
    "RECORDED_FACTS",
    "ImportValueCategory",
    "ImportValueRecord",
    "ImportValues",
    "ImportValuesRecorded",
    "RecordedCustomsValue",
    "RecordedKind",
    "RecordedPurchase",
    "apply_recorded_values",
    "current_import_values",
    "record_import_values",
]
