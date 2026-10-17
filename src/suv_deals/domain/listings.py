"""Canonical normalized listing model (spec section 7).

A listing is an observed offer for a vehicle, not necessarily a unique
physical vehicle. Unknown values are `None` or an explicit `unknown` enum;
`False`/`no` means positively known false.

`NormalizedListing.semantic_payload()` defines exactly which fields are
business-meaningful. Only a change in that payload creates a new revision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    BodyType,
    ClaimStatus,
    Co2Cycle,
    Drive,
    Fuel,
    Gearbox,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    SellerType,
    SteeringSide,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.money import CurrencyCode, exponent
from suv_deals.domain.provenance import FieldConflict, FieldProvenance

SCHEMA_VERSION: Literal["1.0"] = "1.0"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PartialDate(BaseModel):
    """A date known only to some precision. `2011-05` is never invented as 2011-05-01."""

    model_config = _FROZEN

    value: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")
    precision: Precision = Precision.UNKNOWN

    @model_validator(mode="after")
    def _consistent(self) -> PartialDate:
        expected = {
            None: Precision.UNKNOWN,
            4: Precision.YEAR,
            7: Precision.MONTH,
            10: Precision.DAY,
        }[None if self.value is None else len(self.value)]
        if self.precision != expected:
            raise ValueError(f"precision {self.precision} does not match value {self.value!r}")
        if self.value is not None and len(self.value) >= 7:
            month = int(self.value[5:7])
            if not 1 <= month <= 12:
                raise ValueError("invalid month")
        return self

    @property
    def year(self) -> int | None:
        return int(self.value[:4]) if self.value else None

    @property
    def month(self) -> int | None:
        return int(self.value[5:7]) if self.value and len(self.value) >= 7 else None


class SourceTimestamp(BaseModel):
    """A source-provided timestamp. Zone-less source dates carry an explicit assumption flag."""

    model_config = _FROZEN

    value: datetime | None = None
    raw: str | None = Field(default=None, max_length=100)
    zone_assumed: bool = False
    assumed_zone: str | None = Field(default=None, max_length=64)
    precision: Precision = Precision.UNKNOWN

    @field_validator("value")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class MileageOriginal(BaseModel):
    model_config = _FROZEN

    amount: Decimal | None = None
    unit: Literal["km", "mi", "unknown"] = "unknown"
    text: str | None = Field(default=None, max_length=200)
    is_estimate: bool = False
    range_low: Decimal | None = None
    range_high: Decimal | None = None


class VehicleSpec(BaseModel):
    model_config = _FROZEN

    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    generation: str | None = Field(default=None, max_length=80)
    facelift: Tristate = Tristate.UNKNOWN
    trim: str | None = Field(default=None, max_length=200)
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    first_registration: PartialDate = PartialDate()
    production_year: int | None = Field(default=None, ge=1950, le=2100)
    body_type: BodyType = BodyType.UNKNOWN
    steering_side: SteeringSide = SteeringSide.UNKNOWN
    seats: int | None = Field(default=None, ge=1, le=12)
    fuel: Fuel = Fuel.UNKNOWN
    engine_code: str | None = Field(default=None, max_length=40)
    engine_displacement_cm3: int | None = Field(default=None, ge=50, le=10000)
    power_kw: int | None = Field(default=None, ge=1, le=2000)
    gearbox: Gearbox = Gearbox.UNKNOWN
    gearbox_subtype: str | None = Field(default=None, max_length=80)
    drive: Drive = Drive.UNKNOWN
    # Canonical km, unrounded (miles * 1.609344 kept exact). None = unknown.
    mileage_km: Decimal | None = Field(default=None, ge=0)
    mileage_original: MileageOriginal = MileageOriginal()
    mileage_claim: OdometerClaim = OdometerClaim.UNKNOWN


class PriceInfo(BaseModel):
    """Acquisition price evidence. Keep gross/net/VAT/export/deposit values separate (spec 17)."""

    model_config = _FROZEN

    raw_text: str | None = Field(default=None, max_length=300)
    amount_minor: int | None = Field(default=None, ge=0)
    currency: CurrencyCode | None = None
    basis: PriceBasis = PriceBasis.UNKNOWN
    type: PriceType = PriceType.UNKNOWN
    negotiable: Tristate = Tristate.UNKNOWN
    vat_treatment: VatTreatment = VatTreatment.UNKNOWN
    vat_rate_stated: Decimal | None = Field(default=None, ge=0, le=100)
    vat_amount_minor: int | None = Field(default=None, ge=0)
    vat_reclaimable: Tristate = Tristate.UNKNOWN  # seller wording only, never an entitlement
    gross_amount_minor: int | None = Field(default=None, ge=0)
    net_amount_minor: int | None = Field(default=None, ge=0)
    export_net_price_minor: int | None = Field(default=None, ge=0)
    refundable_deposit_minor: int | None = Field(default=None, ge=0)
    # Unavoidable seller fees known to be required for this purchase (e.g. mandatory prep fee).
    required_seller_fees_minor: int | None = Field(default=None, ge=0)
    required_seller_fees_known: Tristate = Tristate.UNKNOWN

    @model_validator(mode="after")
    def _currency_pairing(self) -> PriceInfo:
        amounts = [
            self.amount_minor,
            self.vat_amount_minor,
            self.gross_amount_minor,
            self.net_amount_minor,
            self.export_net_price_minor,
            self.refundable_deposit_minor,
            self.required_seller_fees_minor,
        ]
        if any(a is not None for a in amounts) and self.currency is None:
            raise ValueError("price amounts require a currency")
        if self.currency is not None:
            exponent(self.currency)
        return self


class LocationInfo(BaseModel):
    model_config = _FROZEN

    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    approx_lat: Decimal | None = Field(default=None, ge=-90, le=90)
    approx_lon: Decimal | None = Field(default=None, ge=-180, le=180)
    coordinates_source: str | None = Field(default=None, max_length=120)


class ConditionClaims(BaseModel):
    """Each field names a positive proposition; the ClaimStatus says how it is supported."""

    model_config = _FROZEN

    accident_free: ClaimStatus = ClaimStatus.UNKNOWN
    roadworthy: ClaimStatus = ClaimStatus.UNKNOWN
    running: ClaimStatus = ClaimStatus.UNKNOWN
    warning_lights_off: ClaimStatus = ClaimStatus.UNKNOWN
    corrosion_free: ClaimStatus = ClaimStatus.UNKNOWN
    full_service_history: ClaimStatus = ClaimStatus.UNKNOWN
    damaged_vehicle: ClaimStatus = ClaimStatus.UNKNOWN
    mechanical_faults: tuple[str, ...] = Field(default=(), max_length=30)


class Documentation(BaseModel):
    model_config = _FROZEN

    vin: str | None = Field(default=None, pattern=r"^[A-HJ-NPR-Z0-9]{17}$")
    vin_format_valid: Tristate = Tristate.UNKNOWN
    registration_documents: ClaimStatus = ClaimStatus.UNKNOWN
    coc_available: ClaimStatus = ClaimStatus.UNKNOWN
    emissions_class: str | None = Field(default=None, max_length=40)
    origin_evidence: str | None = Field(default=None, max_length=300)
    inspection_expiry: PartialDate = PartialDate()
    previous_owners: int | None = Field(default=None, ge=0, le=50)


class Co2Info(BaseModel):
    model_config = _FROZEN

    g_per_km: Decimal | None = Field(default=None, ge=0, le=1000)
    cycle: Co2Cycle = Co2Cycle.UNKNOWN
    evidence_id: UUID | None = None


class NormalizedListing(BaseModel):
    """One normalized observation of a listing, produced by an adapter's parser."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    source_key: str = Field(min_length=1, max_length=80)
    source_listing_id: str = Field(min_length=1, max_length=200)
    canonical_url: str = Field(min_length=8, max_length=2048)
    observed_at: datetime
    language: str | None = Field(default=None, max_length=10)
    title: str | None = Field(default=None, max_length=300)
    seller_type: SellerType = SellerType.UNKNOWN
    location: LocationInfo = LocationInfo()
    vehicle: VehicleSpec = VehicleSpec()
    price: PriceInfo = PriceInfo()
    availability: Availability = Availability.UNKNOWN
    condition: ConditionClaims = ConditionClaims()
    documentation: Documentation = Documentation()
    co2: Co2Info = Co2Info()
    source_published_at: SourceTimestamp = SourceTimestamp()
    source_modified_at: SourceTimestamp = SourceTimestamp()
    # Untrusted seller text, bounded; stored as data and never executed or obeyed.
    description_excerpt: str | None = Field(default=None, max_length=4000)
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    conflicts: tuple[FieldConflict, ...] = ()
    warnings: tuple[str, ...] = ()
    parser_version: str = Field(min_length=1, max_length=120)

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def semantic_payload(self) -> dict[str, Any]:
        """Business-meaningful fields only. Excludes timing, provenance, parser and wording noise."""
        return {
            "schema_version": self.schema_version,
            "source_key": self.source_key,
            "source_listing_id": self.source_listing_id,
            "seller_type": self.seller_type.value,
            "location": self.location.model_dump(mode="json", include={"country", "region", "city"}),
            "vehicle": self.vehicle.model_dump(mode="json"),
            "price": self.price.model_dump(mode="json", exclude={"raw_text"}),
            "availability": self.availability.value,
            "condition": self.condition.model_dump(mode="json"),
            "documentation": self.documentation.model_dump(mode="json"),
            "co2": self.co2.model_dump(mode="json", exclude={"evidence_id"}),
            "conflicts": sorted(c.field for c in self.conflicts),
        }

    def semantic_hash(self) -> str:
        return sha256_json(self.semantic_payload())


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for every hash in the system."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
