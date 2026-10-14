"""The owner's recorded import values of a listing (``/api/listings/{listing_id}/import-values``).

Owner decision 2026-10-13 ("two separate values"): every vehicle keeps its REAL purchase price
(реална набавна цена: the profit and the landed cost use it) apart from its ESTIMATED customs value
(проценета царинска вредност: the import charges are computed on it plus the transport to the MK
place of entry; Customs Law art. 28 keeps the price actually paid as the primary method, so the
calculator always shows the ``transaction_value`` alternative).

- ``purchase_price`` / ``estimated_customs_value``: the CURRENT recorded value of each (``null``
  when none is recorded); ``history`` lists recent rows of both, newest first, superseded ones
  with ``current: false``.
- ``amount`` is shown in whole euros (half-up, owner decision 2026-10-13 "без децимали");
  ``amount_exact`` is the amount exactly as recorded (at most two decimals). Calculations always
  use the exact amount.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from suv_deals.views.common import AmountView, DecimalStr, UtcDatetime, ViewModel

IMPORT_VALUES_NOTE: Final = (
    "Two separate values per vehicle (owner decision 2026-10-13): the real purchase price is used for the "
    "profit and the landed cost; the estimated customs value (without transport) for the import charges. "
    "Customs first applies the price actually paid (Customs Law art. 28); the calculator shows that "
    "alternative too."
)
RECORD_EFFECT: Final = (
    "Recorded: the listing's valuations are marked stale and recomputed with the recorded values; the "
    "calculator's listing mode uses them unless a request overrides them."
)


class ImportValueRecordView(ViewModel):
    """One recorded value: a real purchase price (``purchase``) or an estimated customs value
    (``customs_value``, always an ``estimate``, in EUR without transport)."""

    id: UUID
    category: Literal["purchase", "customs_value"]
    kind: Literal["actual", "quote", "estimate"]
    amount: AmountView
    amount_exact: DecimalStr
    source: str = Field(max_length=120)
    document_ref: str | None = Field(max_length=500)
    supersedes_id: UUID | None
    current: bool
    recorded_by_principal: UUID | None
    recorded_at: UtcDatetime
    is_fixture: bool


class ImportValuesView(ViewModel):
    """``GET /api/listings/{listing_id}/import-values`` data."""

    listing_id: UUID
    purchase_price: ImportValueRecordView | None
    estimated_customs_value: ImportValueRecordView | None
    history: tuple[ImportValueRecordView, ...] = Field(max_length=50)
    note: str = IMPORT_VALUES_NOTE


class ImportValuesRecordResult(ViewModel):
    """``POST /api/listings/{listing_id}/import-values`` data: the listing's values after the
    recording, the new and superseded row ids, the valuations marked stale and the queued
    recomputation."""

    values: ImportValuesView
    recorded_ids: tuple[UUID, ...] = Field(max_length=2)
    superseded_ids: tuple[UUID, ...] = Field(max_length=2)
    stale_valuation_ids: tuple[UUID, ...] = Field(max_length=1000)
    recompute_job_id: UUID
    production_effect: str = Field(default=RECORD_EFFECT, max_length=500)


__all__ = [
    "IMPORT_VALUES_NOTE",
    "RECORD_EFFECT",
    "ImportValueRecordView",
    "ImportValuesRecordResult",
    "ImportValuesView",
]
