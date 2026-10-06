"""Search/eligibility profiles (spec section 3).

Hard business rules:
- primary: EUR 2,500.00 <= full-vehicle payable asking amount <= EUR 3,000.00 (inclusive), enabled.
- mileage strictly below 200,000 km for every profile.
- manual_4000: optional manual-review ceiling EUR 4,000, disabled by default, separate queue.
- below_target_watch: < EUR 2,500 watch option, disabled until the owner chooses it.
A configuration whose primary max is not EUR 3,000 fails baseline validation.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from suv_deals.domain.enums import BodyType, ProfileKey
from suv_deals.errors import ValidationFailed

PRIMARY_MIN_EUR = Decimal("2500.00")
PRIMARY_MAX_EUR = Decimal("3000.00")
MAX_MILEAGE_KM_EXCLUSIVE = Decimal("200000")
MK_ASKING_BAND_MIN_EUR = Decimal("8000.00")
MK_ASKING_BAND_MAX_EUR = Decimal("10000.00")
MANUAL_PROFILE_MAX_EUR = Decimal("4000.00")
PROPOSED_MIN_CONTRIBUTION_EUR = Decimal("1500.00")


class SearchProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ProfileKey
    label: str = Field(min_length=3, max_length=120)
    queue_label: str = Field(min_length=3, max_length=120)
    enabled: bool
    # Inclusive bounds on the EUR-equivalent full-vehicle payable asking amount.
    min_price_eur: Decimal | None
    max_price_eur: Decimal
    # below_target_watch uses an exclusive upper bound (< EUR 2,500.00) so no FX value falls in a gap.
    max_price_inclusive: bool = True
    max_mileage_km_exclusive: Decimal = MAX_MILEAGE_KM_EXCLUSIVE
    source_countries: tuple[str, ...] = ("DE", "IT", "CH")
    body_types: tuple[BodyType, ...] = (BodyType.SUV, BodyType.OFFROAD, BodyType.CROSSOVER)
    require_taxonomy_match: bool = True
    # FX staleness tolerance used when a non-EUR price is near a band boundary.
    fx_max_age_days: int = Field(default=7, ge=0, le=60)
    fx_boundary_margin_pct: Decimal = Decimal("3")
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _sane(self) -> SearchProfile:
        if self.min_price_eur is not None and self.min_price_eur > self.max_price_eur:
            raise ValueError("min_price_eur must not exceed max_price_eur")
        if self.max_mileage_km_exclusive > MAX_MILEAGE_KM_EXCLUSIVE:
            raise ValueError("mileage ceiling may never exceed the strict 200,000 km rule")
        return self


class MkResaleBand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_eur: Decimal = MK_ASKING_BAND_MIN_EUR
    max_eur: Decimal = MK_ASKING_BAND_MAX_EUR
    meaning: str = "asking-price research band; not a proven realized sale price"


class ContributionThreshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_eur: Decimal = PROPOSED_MIN_CONTRIBUTION_EUR
    approval_status: str = Field(default="unapproved", pattern=r"^(unapproved|approved)$")
    approved_by: str | None = None
    approved_at: str | None = None

    @model_validator(mode="after")
    def _approval_evidence(self) -> ContributionThreshold:
        if self.approval_status == "approved" and not (self.approved_by and self.approved_at):
            raise ValueError("an approved threshold needs approved_by and approved_at")
        return self


class BusinessConfig(BaseModel):
    """Validated business configuration; every change is a config_revisions row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination_market: str = "MK"
    mk_resale_band: MkResaleBand = MkResaleBand()
    contribution_threshold: ContributionThreshold = ContributionThreshold()
    profiles: dict[ProfileKey, SearchProfile]
    price_realert_abs_eur: Decimal = Decimal("100")
    price_realert_pct: Decimal = Decimal("5")
    price_realert_policy_approved: bool = False
    comparable_year_window: int = Field(default=1, ge=0, le=5)
    comparable_mileage_window_km: Decimal = Decimal("30000")
    comparable_max_age_days: int = Field(default=90, ge=1, le=730)
    comparable_min_sample: int = Field(default=3, ge=1, le=50)
    negotiation_discount_pct: Decimal | None = None  # unknown until the owner supplies one
    claim_duration_seconds: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def _baseline(self) -> BusinessConfig:
        validate_baseline(self)
        return self


def validate_baseline(config: BusinessConfig) -> None:
    """Acceptance baseline: the confirmed rules cannot be silently changed."""
    try:
        primary = config.profiles[ProfileKey.PRIMARY]
    except KeyError as exc:
        raise ValueError("primary profile is required") from exc
    if primary.min_price_eur != PRIMARY_MIN_EUR or primary.max_price_eur != PRIMARY_MAX_EUR:
        raise ValueError("primary profile must be exactly EUR 2,500.00-3,000.00 inclusive")
    if not primary.max_price_inclusive:
        raise ValueError("primary profile upper bound EUR 3,000.00 is inclusive")
    if primary.max_mileage_km_exclusive != MAX_MILEAGE_KM_EXCLUSIVE:
        raise ValueError("primary profile mileage must be strictly below 200,000 km")
    if not primary.enabled:
        raise ValueError("primary profile must be enabled")
    manual = config.profiles.get(ProfileKey.MANUAL_4000)
    if manual is not None:
        if manual.max_price_eur != MANUAL_PROFILE_MAX_EUR:
            raise ValueError("manual_4000 profile ceiling must be EUR 4,000.00")
        if manual.queue_label == primary.queue_label:
            raise ValueError("manual_4000 must use a visibly different queue")
    below = config.profiles.get(ProfileKey.BELOW_TARGET_WATCH)
    if below is not None:
        if below.max_price_eur != PRIMARY_MIN_EUR or below.max_price_inclusive:
            raise ValueError("below_target_watch must be exactly < EUR 2,500.00 (exclusive bound)")
        if below.queue_label == primary.queue_label:
            raise ValueError("below_target_watch must use a visibly different queue")
    if config.mk_resale_band.min_eur != MK_ASKING_BAND_MIN_EUR or (
        config.mk_resale_band.max_eur != MK_ASKING_BAND_MAX_EUR
    ):
        raise ValueError("MK asking-price research band must be EUR 8,000-10,000")


def load_business_config(config_dir: Path, overrides: dict[str, Any] | None = None) -> BusinessConfig:
    """Load config/defaults.yaml plus config/profiles/*.yaml into a validated BusinessConfig."""
    defaults_path = config_dir / "defaults.yaml"
    try:
        raw: dict[str, Any] = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ValidationFailed(f"missing {defaults_path}") from exc
    profiles: dict[str, Any] = {}
    for path in sorted((config_dir / "profiles").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        profiles[data["key"]] = data
    raw["profiles"] = profiles
    if overrides:
        raw = _deep_merge(raw, overrides)
    try:
        return BusinessConfig.model_validate(raw)
    except ValueError as exc:
        raise ValidationFailed(f"invalid business configuration: {exc}") from exc


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
