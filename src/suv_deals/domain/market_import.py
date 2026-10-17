"""Owner import of MK market evidence (spec 15; F2, wave D2): parse and validate, nothing else.

Until an MK comparable adapter is implemented and its terms are reviewed (both MK sources are
placeholders), the owner can record MK evidence by hand: ASKING prices he saw on MK classifieds
(``asking_price``, each with the ad URL) and his own estimates (``owner_estimate``). Without
any such evidence every valuation is ``insufficient_comparables`` and no listing can become
``inquiry_ready`` (spec 37.2 check 2).

File format (JSON, UTF-8, at most `MAX_IMPORT_BYTES` and `MAX_IMPORT_ROWS` rows)::

    {"format": "suv_deals.market_import/1",
     "evidence_kind": "asking_price",
     "observations": [
       {"url": "https://...", "observed_at": "2026-10-08T09:30:00+02:00",
        "price": "8900", "currency": "EUR", "price_basis": "unknown",
        "provenance": "seen on the MK classifieds site; screenshot kept by the owner",
        "source_key": "pazar3_mk", "seller_type": "private",
        "local_registration_status": "locally_registered",
        "vehicle": {"make": "...", "model": "...", "first_registration": "2011", "fuel": "diesel",
                    "gearbox": "manual", "drive": "awd", "mileage_km": "180000",
                    "engine_displacement_cm3": 1995, "power_kw": 103}}]}

Rules:

- **Evidence kinds stay distinct**: the file names ONE kind and the command must name the same
  kind (a double confirmation); an asking price is never a sale.
- **No seller contact data**: every object refuses unknown keys (no ``phone``, ``email``,
  ``seller_name`` ...), and URL/provenance text that the log redactor would change (an e-mail
  address or a phone number) is refused. URLs carry no user-info.
- **Exact values**: prices are decimal strings (never floats), timestamps carry a time zone and
  are not in the future, mileage is a decimal.
- **Idempotent**: each observation's id is derived from its content and kind
  (`observation_id`), so re-importing the same file records nothing new.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.comparables import LocalRegistrationStatus, MarketObservation
from suv_deals.domain.enums import (
    Drive,
    EvidenceKind,
    Fuel,
    Gearbox,
    Precision,
    PriceBasis,
    SellerType,
    Tristate,
)
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import Money
from suv_deals.errors import ValidationFailed
from suv_deals.observability.logging import redact

IMPORT_FORMAT: Final = "suv_deals.market_import/1"
MAX_IMPORT_BYTES: Final = 1_000_000
MAX_IMPORT_ROWS: Final = 500
#: Kinds this import accepts. Sales need transaction evidence and stay out of a hand-written file.
IMPORTABLE_KINDS: Final = frozenset({EvidenceKind.ASKING_PRICE, EvidenceKind.OWNER_ESTIMATE})
OWNER_ESTIMATE_SOURCE_KEY: Final = "owner_estimate"
MANUAL_IMPORT_SOURCE_KEY: Final = "manual_import"
_NAMESPACE: Final = uuid.UUID("6d1f3c0e-7a51-4c8b-9f0e-3b0e5d1c2a77")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _no_contact_data(value: str, name: str) -> str:
    if redact(value) != value:
        raise ValueError(f"{name} must not contain contact data (e-mail address, phone number) or secrets")
    return value


class ImportVehicle(BaseModel):
    model_config = _FROZEN

    make: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    generation: str | None = Field(default=None, max_length=80)
    facelift: Tristate = Tristate.UNKNOWN
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    first_registration: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2})?$")
    fuel: Fuel = Fuel.UNKNOWN
    gearbox: Gearbox = Gearbox.UNKNOWN
    drive: Drive = Drive.UNKNOWN
    engine_code: str | None = Field(default=None, max_length=40)
    engine_displacement_cm3: int | None = Field(default=None, ge=50, le=10000)
    power_kw: int | None = Field(default=None, ge=1, le=2000)
    mileage_km: Decimal | None = Field(default=None, ge=0, le=Decimal(2_000_000))

    @field_validator("mileage_km", mode="before")
    @classmethod
    def _decimal_text(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("mileage_km must be a decimal string or an integer, never a float")
        return value

    def spec(self) -> VehicleSpec:
        registration = (
            PartialDate()
            if self.first_registration is None
            else PartialDate(
                value=self.first_registration,
                precision=Precision.YEAR if len(self.first_registration) == 4 else Precision.MONTH,
            )
        )
        return VehicleSpec(
            make=self.make.strip(),
            model=self.model.strip(),
            generation=self.generation,
            facelift=self.facelift,
            model_year=self.model_year,
            first_registration=registration,
            fuel=self.fuel,
            gearbox=self.gearbox,
            drive=self.drive,
            engine_code=self.engine_code,
            engine_displacement_cm3=self.engine_displacement_cm3,
            power_kw=self.power_kw,
            mileage_km=self.mileage_km,
        )


class ImportRow(BaseModel):
    """One hand-recorded observation (no seller contact data: unknown keys are refused)."""

    model_config = _FROZEN

    url: str | None = Field(default=None, min_length=8, max_length=2048)
    observed_at: datetime
    price: str = Field(min_length=1, max_length=20)
    currency: Literal["EUR", "MKD"]
    price_basis: PriceBasis = PriceBasis.UNKNOWN
    provenance: str = Field(min_length=3, max_length=500)
    source_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,79}$")
    seller_type: SellerType = SellerType.UNKNOWN
    local_registration_status: LocalRegistrationStatus = "unknown"
    vehicle: ImportVehicle

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at needs a time zone (e.g. +02:00 or Z)")
        return ensure_utc(value)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("url must be an http(s) URL")
        if parts.username or parts.password or "@" in parts.netloc:
            raise ValueError("url must not carry user information")
        return _no_contact_data(value, "url")

    @field_validator("provenance")
    @classmethod
    def _provenance(cls, value: str) -> str:
        return _no_contact_data(value.strip(), "provenance")

    @field_validator("price")
    @classmethod
    def _price(cls, value: str) -> str:
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError("price must be a decimal string such as 8900 or 8900.00") from None
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:  # type: ignore[operator]
            raise ValueError("price must be a positive amount with at most 2 decimals")
        return value


class MarketImport(BaseModel):
    model_config = _FROZEN

    format: Literal["suv_deals.market_import/1"]
    evidence_kind: EvidenceKind
    observations: tuple[ImportRow, ...] = Field(min_length=1, max_length=MAX_IMPORT_ROWS)

    @model_validator(mode="after")
    def _kind_rules(self) -> MarketImport:
        if self.evidence_kind not in IMPORTABLE_KINDS:
            raise ValueError("only asking_price and owner_estimate evidence can be imported")
        if self.evidence_kind == EvidenceKind.ASKING_PRICE:
            missing = [i for i, row in enumerate(self.observations) if row.url is None]
            if missing:
                raise ValueError(f"every asking price needs the ad url (rows {missing[:10]})")
        return self


def parse_import(data: bytes, *, expected_kind: EvidenceKind) -> MarketImport:
    """Parse and validate one import file; ``ValidationFailed`` names rows/fields, never values."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ValidationFailed(f"the import file exceeds {MAX_IMPORT_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"), parse_float=_refuse_float)
    except UnicodeDecodeError:
        raise ValidationFailed("the import file must be UTF-8 JSON") from None
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"the import file is not valid JSON ({str(exc)[:120]})") from None
    try:
        parsed = MarketImport.model_validate(document)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"[:200] for err in exc.errors()[:20]
        ]
        raise ValidationFailed("the import file is invalid", details={"problems": problems}) from None
    if parsed.evidence_kind != expected_kind:
        raise ValidationFailed(
            f"the file holds {parsed.evidence_kind.value} evidence"
            f" but --evidence-kind is {expected_kind.value}"
        )
    return parsed


def _refuse_float(text: str) -> Any:
    raise ValueError(f"numbers with a fraction must be strings (got {text[:20]})")


def observation_id(row: ImportRow, kind: EvidenceKind) -> uuid.UUID:
    """Stable id of one imported observation (content + kind): re-imports record nothing new."""
    material = json.dumps(
        {"kind": kind.value, **row.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")
    )
    return uuid.uuid5(_NAMESPACE, hashlib.sha256(material.encode("utf-8")).hexdigest())


def to_observation(row: ImportRow, kind: EvidenceKind) -> MarketObservation:
    """The domain observation of one row (market MK, real lineage)."""
    if kind == EvidenceKind.OWNER_ESTIMATE:
        source_key = OWNER_ESTIMATE_SOURCE_KEY
    else:
        source_key = row.source_key or MANUAL_IMPORT_SOURCE_KEY
    return MarketObservation(
        id=observation_id(row, kind),
        source_key=source_key,
        url=row.url,
        observed_at=row.observed_at,
        evidence_kind=kind,
        amount=Money(amount=Decimal(row.price), currency=row.currency),
        price_basis=row.price_basis,
        vehicle=row.vehicle.spec(),
        local_registration_status=row.local_registration_status,
        seller_type=row.seller_type,
        market="MK",
        is_fixture=False,
    )


def import_evidence(row: ImportRow, *, file_sha256: str) -> dict[str, str]:
    """The provenance stored with each observation (no contact data; bounded)."""
    return {"provenance": row.provenance, "import_file_sha256": file_sha256, "method": "owner_import"}


def future_rows(parsed: MarketImport, now: datetime) -> list[int]:
    """Rows observed in the future (refused by the command)."""
    return [i for i, row in enumerate(parsed.observations) if row.observed_at > ensure_utc(now)]


__all__ = [
    "IMPORTABLE_KINDS",
    "IMPORT_FORMAT",
    "MANUAL_IMPORT_SOURCE_KEY",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ROWS",
    "OWNER_ESTIMATE_SOURCE_KEY",
    "ImportRow",
    "ImportVehicle",
    "MarketImport",
    "future_rows",
    "import_evidence",
    "observation_id",
    "parse_import",
    "to_observation",
]
