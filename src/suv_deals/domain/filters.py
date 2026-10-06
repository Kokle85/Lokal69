"""Deterministic eligibility screening per search profile (spec sections 3, 14, 17, 18 FX rules).

Pure functions, no I/O. ``screen()`` turns one ``NormalizedListing`` into one ``ScreeningResult``.

Hard rules (spec 3):

- Primary band is inclusive: EUR 2,500.00 <= full-vehicle payable asking amount <= EUR 3,000.00,
  compared on the *unrounded* Decimal EUR equivalent. Payable = full-vehicle asking amount plus
  required seller fees known for this purchase; transport/import costs are not included.
- A listing above EUR 3,000 never qualifies for the primary profile.
- Mileage must be strictly below 200,000 km (200,000 fails; 199,999.999 passes).
- Net-only prices with unknown gross, instalments/leasing/deposits, price on request, missing
  prices, missing mileage, mileage ranges, conflicting odometer statements and missing FX are
  ``needs_facts``; parts/damaged and auction prices are ``rejected``.
- Non-EUR prices use the recorded *reference* rate with its explicit direction
  (1 EUR = x CHF -> CHF / x). A stale rate makes the result ``needs_facts`` only when the EUR value
  lies within ``fx_boundary_margin_pct`` of a band boundary; otherwise it is a warning.
- Profiles are evaluated in the order primary -> manual_4000 -> below_target_watch. Only enabled
  profiles can make a listing eligible; a disabled profile that would (or might) match adds an
  ``info`` reason so the option is never silently discarded.

Precedence of ``needs_facts`` versus ``rejected`` (per profile): every rule returns PASS, FAIL or
UNKNOWN. A rule only returns FAIL when its decision does not depend on a missing fact -- e.g. a
net-only price whose *net* amount is already above the band maximum fails (gross >= net), and
mileage >= 200,000 fails whatever the price. So a profile is ``rejected`` if any rule FAILs,
otherwise ``needs_facts`` if any rule is UNKNOWN, otherwise eligible. Across profiles: eligible
primary wins, then the first eligible enabled manual profile, then ``needs_facts`` if any enabled
profile needs facts, else ``rejected``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from suv_deals.domain.enums import (
    Availability,
    BodyType,
    EligibilityState,
    FxPurpose,
    OdometerClaim,
    PriceBasis,
    PriceType,
    ProfileKey,
    SellerType,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.parsing import MILES_TO_KM
from suv_deals.domain.profiles import BusinessConfig, SearchProfile
from suv_deals.domain.taxonomy import CLASS_BODY_TYPES, TaxonomyMatch, VehicleTaxonomy

SCREENING_VERSION = "screening@1.0.0"

PROFILE_ORDER: tuple[ProfileKey, ...] = (
    ProfileKey.PRIMARY,
    ProfileKey.MANUAL_4000,
    ProfileKey.BELOW_TARGET_WATCH,
)
# An estimated odometer reading ("ca. 150.000 km") passes only below 180,000 km (= ceiling minus
# this margin); at or above ceiling plus the margin it fails; in between it needs facts.
ESTIMATE_UNCERTAINTY_KM = Decimal("20000")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ReasonSeverity(StrEnum):
    REJECT = "reject"
    NEEDS_FACTS = "needs_facts"
    WARNING = "warning"
    INFO = "info"


class ReasonCode(StrEnum):
    AVAILABILITY_REMOVED = "AVAILABILITY_REMOVED"
    AVAILABILITY_SOLD_CLAIMED = "AVAILABILITY_SOLD_CLAIMED"
    AVAILABILITY_RESERVED = "AVAILABILITY_RESERVED"
    AVAILABILITY_UNKNOWN = "AVAILABILITY_UNKNOWN"
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_CURRENCY_MISSING = "PRICE_CURRENCY_MISSING"
    PRICE_ON_REQUEST = "PRICE_ON_REQUEST"
    PRICE_TYPE_INSTALMENT = "PRICE_TYPE_INSTALMENT"
    PRICE_TYPE_LEASING = "PRICE_TYPE_LEASING"
    PRICE_TYPE_DEPOSIT = "PRICE_TYPE_DEPOSIT"
    PRICE_TYPE_AUCTION = "PRICE_TYPE_AUCTION"
    PRICE_TYPE_EXPORT_NET = "PRICE_TYPE_EXPORT_NET"
    PRICE_TYPE_PARTS_OR_DAMAGED = "PRICE_TYPE_PARTS_OR_DAMAGED"
    PRICE_TYPE_UNKNOWN = "PRICE_TYPE_UNKNOWN"
    PRICE_EXPORT_NET_GROSS_USED = "PRICE_EXPORT_NET_GROSS_USED"
    PRICE_BASIS_NET_ONLY = "PRICE_BASIS_NET_ONLY"
    PRICE_BASIS_UNKNOWN = "PRICE_BASIS_UNKNOWN"
    PRICE_BASIS_ASSUMED_NO_VAT = "PRICE_BASIS_ASSUMED_NO_VAT"
    PRICE_BASIS_GROSS_FROM_VAT_WORDING = "PRICE_BASIS_GROSS_FROM_VAT_WORDING"
    PRICE_GROSS_STATED_USED = "PRICE_GROSS_STATED_USED"
    PRICE_NEGOTIABLE = "PRICE_NEGOTIABLE"
    REFUNDABLE_DEPOSIT_STATED = "REFUNDABLE_DEPOSIT_STATED"
    SELLER_FEES_UNKNOWN = "SELLER_FEES_UNKNOWN"
    SELLER_FEE_AMOUNT_UNKNOWN = "SELLER_FEE_AMOUNT_UNKNOWN"
    SELLER_FEES_INCLUDED = "SELLER_FEES_INCLUDED"
    FX_MISSING = "FX_MISSING"
    FX_STALE = "FX_STALE"
    FX_STALE_NEAR_BOUNDARY = "FX_STALE_NEAR_BOUNDARY"
    FX_RATE_FUTURE_DATED = "FX_RATE_FUTURE_DATED"
    FX_CONVERTED = "FX_CONVERTED"
    PRICE_IN_BAND = "PRICE_IN_BAND"
    PRICE_BELOW_BAND = "PRICE_BELOW_BAND"
    PRICE_ABOVE_BAND = "PRICE_ABOVE_BAND"
    MILEAGE_MISSING = "MILEAGE_MISSING"
    MILEAGE_TOO_HIGH = "MILEAGE_TOO_HIGH"
    MILEAGE_RANGE_ONLY = "MILEAGE_RANGE_ONLY"
    MILEAGE_CONFLICT = "MILEAGE_CONFLICT"
    MILEAGE_ESTIMATE = "MILEAGE_ESTIMATE"
    MILEAGE_ESTIMATE_NEAR_LIMIT = "MILEAGE_ESTIMATE_NEAR_LIMIT"
    MILEAGE_CLAIM_UNKNOWN = "MILEAGE_CLAIM_UNKNOWN"
    NOT_SUV = "NOT_SUV"
    SUV_IDENTITY_UNKNOWN = "SUV_IDENTITY_UNKNOWN"
    TAXONOMY_UNMATCHED = "TAXONOMY_UNMATCHED"
    TAXONOMY_NOT_SUPPLIED = "TAXONOMY_NOT_SUPPLIED"
    BODY_TYPE_CONFLICTS_TAXONOMY = "BODY_TYPE_CONFLICTS_TAXONOMY"
    COUNTRY_MISSING = "COUNTRY_MISSING"
    COUNTRY_NOT_IN_PROFILE = "COUNTRY_NOT_IN_PROFILE"
    DISABLED_PROFILE_WOULD_MATCH = "DISABLED_PROFILE_WOULD_MATCH"
    DISABLED_PROFILE_MIGHT_MATCH = "DISABLED_PROFILE_MIGHT_MATCH"


class ScreeningReason(BaseModel):
    model_config = _FROZEN

    code: ReasonCode
    message: str = Field(max_length=300)
    field: str | None = None
    severity: ReasonSeverity
    profile: ProfileKey | None = None  # None: rule independent of the profile


ProfileOutcome = Literal["eligible", "needs_facts", "rejected"]


class ProfileEvaluation(BaseModel):
    model_config = _FROZEN

    profile: ProfileKey
    enabled: bool
    outcome: ProfileOutcome
    reasons: tuple[ScreeningReason, ...]


class ScreeningResult(BaseModel):
    """Deterministic eligibility decision for one listing revision."""

    model_config = _FROZEN

    state: EligibilityState
    profile: ProfileKey | None
    queue_label: str | None
    eur_amount: Decimal | None  # unrounded EUR equivalent of the payable amount
    payable_amount: Money | None  # payable amount in the advertised currency
    fx_rate_used: FxRate | None
    reasons: tuple[ScreeningReason, ...]
    missing_facts: tuple[str, ...]
    screening_version: str = SCREENING_VERSION
    profile_evaluations: tuple[ProfileEvaluation, ...] = ()
    taxonomy_match: TaxonomyMatch | None = None

    def codes(self) -> set[ReasonCode]:
        return {r.code for r in self.reasons}


# ---------------------------------------------------------------------------------------------
# Rule plumbing
# ---------------------------------------------------------------------------------------------

RuleOutcome = Literal["pass", "fail", "unknown"]


@dataclass
class _Rule:
    outcome: RuleOutcome = "pass"
    reasons: list[ScreeningReason] = field(default_factory=list)

    def add(
        self,
        code: ReasonCode,
        severity: ReasonSeverity,
        message: str,
        field_name: str | None = None,
        profile: ProfileKey | None = None,
    ) -> None:
        self.reasons.append(
            ScreeningReason(code=code, message=message, field=field_name, severity=severity, profile=profile)
        )
        if severity == ReasonSeverity.REJECT:
            self.outcome = "fail"
        elif severity == ReasonSeverity.NEEDS_FACTS and self.outcome != "fail":
            self.outcome = "unknown"


def select_reference_rate(
    fx_rates: Sequence[FxRate], currency: str, as_of: date
) -> tuple[FxRate | None, bool]:
    """Most recent REFERENCE rate pairing EUR with ``currency`` dated on/before ``as_of``.

    Returns ``(rate, ignored_future_rate)``. Payment/customs rates are never used for screening.
    """
    candidates = [
        r for r in fx_rates if {r.base, r.quote} == {"EUR", currency} and r.purpose == FxPurpose.REFERENCE
    ]
    usable = [r for r in candidates if r.rate_date <= as_of]
    ignored_future = len(usable) != len(candidates)
    if not usable:
        return None, ignored_future
    return max(usable, key=lambda r: (r.rate_date, r.retrieved_at)), ignored_future


# ---------------------------------------------------------------------------------------------
# Price facts (profile independent)
# ---------------------------------------------------------------------------------------------


@dataclass
class _PriceFacts:
    """Exact payable amount, or a lower bound on it, plus the reasons it is not exact."""

    payable: Money | None = None
    lower_bound: Money | None = None
    eur_exact: Decimal | None = None
    eur_lower: Decimal | None = None
    rate: FxRate | None = None
    common: _Rule = field(default_factory=_Rule)  # fails/warnings/info, independent of the band
    undetermined: list[ScreeningReason] = field(default_factory=list)  # needs_facts for the band

    def unknown(self, code: ReasonCode, message: str, field_name: str) -> None:
        self.undetermined.append(
            ScreeningReason(code=code, message=message, field=field_name, severity=ReasonSeverity.NEEDS_FACTS)
        )


_NON_ASKING_UNKNOWN: dict[PriceType, tuple[ReasonCode, str]] = {
    PriceType.INSTALMENT: (
        ReasonCode.PRICE_TYPE_INSTALMENT,
        "advertised amount is an instalment, not the price",
    ),
    PriceType.LEASING: (ReasonCode.PRICE_TYPE_LEASING, "advertised amount is a leasing rate, not the price"),
    PriceType.DEPOSIT: (ReasonCode.PRICE_TYPE_DEPOSIT, "advertised amount is a deposit, not the price"),
    PriceType.PRICE_ON_REQUEST: (ReasonCode.PRICE_ON_REQUEST, "price only on request"),
}


def _price_facts(listing: NormalizedListing, fx_rates: Sequence[FxRate], as_of: date) -> _PriceFacts:
    price = listing.price
    facts = _PriceFacts()
    currency = price.currency
    base_minor: int | None = None
    lower_minor: int | None = None

    if price.type == PriceType.PARTS_OR_DAMAGED:
        facts.common.add(
            ReasonCode.PRICE_TYPE_PARTS_OR_DAMAGED,
            ReasonSeverity.REJECT,
            "parts/damaged-vehicle price is not an ordinary vehicle price",
            "price.type",
        )
        return facts
    if price.type in (PriceType.AUCTION_START, PriceType.AUCTION_CURRENT_BID):
        facts.common.add(
            ReasonCode.PRICE_TYPE_AUCTION,
            ReasonSeverity.REJECT,
            "auction bid amounts are not a payable asking price (bidding is out of scope)",
            "price.type",
        )
        return facts
    if price.type in _NON_ASKING_UNKNOWN:
        code, message = _NON_ASKING_UNKNOWN[price.type]
        facts.unknown(code, message, "price.type")
        return facts
    if price.type == PriceType.UNKNOWN:
        if price.amount_minor is None and price.gross_amount_minor is None:
            facts.unknown(ReasonCode.PRICE_MISSING, "no advertised price", "price.amount_minor")
        else:
            facts.unknown(ReasonCode.PRICE_TYPE_UNKNOWN, "price type not established", "price.type")
        return facts

    if price.type == PriceType.EXPORT_NET:
        if price.gross_amount_minor is not None:
            base_minor = price.gross_amount_minor
            facts.common.add(
                ReasonCode.PRICE_EXPORT_NET_GROSS_USED,
                ReasonSeverity.WARNING,
                "export/net price ignored; stated gross amount used as payable",
                "price.gross_amount_minor",
            )
        else:
            lower_minor = (
                price.export_net_price_minor
                if price.export_net_price_minor is not None
                else price.amount_minor
            )
            facts.unknown(
                ReasonCode.PRICE_TYPE_EXPORT_NET,
                "export/net price is not a confirmed payable amount for this buyer",
                "price.gross_amount_minor",
            )
    else:  # FULL_VEHICLE_ASKING
        amount = price.amount_minor
        if amount is None and price.gross_amount_minor is None:
            facts.unknown(ReasonCode.PRICE_MISSING, "no advertised price", "price.amount_minor")
        elif price.basis == PriceBasis.GROSS:
            base_minor = amount if amount is not None else price.gross_amount_minor
        elif price.gross_amount_minor is not None:
            base_minor = price.gross_amount_minor
            facts.common.add(
                ReasonCode.PRICE_GROSS_STATED_USED,
                ReasonSeverity.INFO,
                "stated gross amount used as payable",
                "price.gross_amount_minor",
            )
        elif price.basis == PriceBasis.NET:
            lower_minor = amount
            facts.unknown(
                ReasonCode.PRICE_BASIS_NET_ONLY,
                "net-only price; payable gross amount unknown",
                "price.gross_amount_minor",
            )
        elif price.vat_treatment in (VatTreatment.MARGIN_SCHEME, VatTreatment.PRIVATE_SALE) or (
            listing.seller_type == SellerType.PRIVATE
        ):
            base_minor = amount
            facts.common.add(
                ReasonCode.PRICE_BASIS_ASSUMED_NO_VAT,
                ReasonSeverity.WARNING,
                "price basis not stated; margin-scheme/private sale treated as payable as advertised",
                "price.basis",
            )
        elif price.vat_treatment == VatTreatment.VAT_SHOWN:
            base_minor = amount
            facts.common.add(
                ReasonCode.PRICE_BASIS_GROSS_FROM_VAT_WORDING,
                ReasonSeverity.WARNING,
                "VAT-shown wording implies a gross price; basis not stated explicitly",
                "price.basis",
            )
        else:
            lower_minor = amount
            facts.unknown(
                ReasonCode.PRICE_BASIS_UNKNOWN, "gross/net basis of the price unknown", "price.basis"
            )

    if (base_minor is not None or lower_minor is not None) and currency is None:
        facts.unknown(ReasonCode.PRICE_CURRENCY_MISSING, "price currency unknown", "price.currency")
        return facts

    # Required seller fees (spec 3): known fees are added; unknown fees are a warning only.
    fees_known = price.required_seller_fees_known
    fees = price.required_seller_fees_minor
    if base_minor is not None or lower_minor is not None:
        if fees_known == Tristate.YES and fees is None:
            facts.unknown(
                ReasonCode.SELLER_FEE_AMOUNT_UNKNOWN,
                "a required seller fee exists but its amount is unknown",
                "price.required_seller_fees_minor",
            )
            if base_minor is not None:
                lower_minor, base_minor = base_minor, None
        elif fees is not None and fees_known != Tristate.NO:
            if base_minor is not None:
                base_minor += fees
            if lower_minor is not None:
                lower_minor += fees
            facts.common.add(
                ReasonCode.SELLER_FEES_INCLUDED,
                ReasonSeverity.INFO,
                "required seller fees added to the payable amount",
                "price.required_seller_fees_minor",
            )
        elif fees_known == Tristate.UNKNOWN:
            facts.common.add(
                ReasonCode.SELLER_FEES_UNKNOWN,
                ReasonSeverity.WARNING,
                "required seller fees not established; payable may be higher",
                "price.required_seller_fees_known",
            )
    if price.negotiable == Tristate.YES:
        facts.common.add(
            ReasonCode.PRICE_NEGOTIABLE,
            ReasonSeverity.INFO,
            "price negotiable; screened on the asking amount (no discount assumed)",
            "price.negotiable",
        )
    if price.refundable_deposit_minor is not None:
        facts.common.add(
            ReasonCode.REFUNDABLE_DEPOSIT_STATED,
            ReasonSeverity.INFO,
            "refundable deposit stated; affects cash exposure, not the screened payable amount",
            "price.refundable_deposit_minor",
        )

    assert currency is not None or (base_minor is None and lower_minor is None)
    if currency is None:
        return facts
    facts.payable = Money.from_minor(base_minor, currency) if base_minor is not None else None
    facts.lower_bound = Money.from_minor(lower_minor, currency) if lower_minor is not None else None
    if currency == "EUR":
        facts.eur_exact = facts.payable.amount if facts.payable else None
        facts.eur_lower = facts.lower_bound.amount if facts.lower_bound else None
        return facts

    rate, ignored_future = select_reference_rate(fx_rates, currency, as_of)
    if ignored_future:
        facts.common.add(
            ReasonCode.FX_RATE_FUTURE_DATED,
            ReasonSeverity.WARNING,
            "an FX rate dated after the screening date was ignored",
            f"fx:{currency}/EUR",
        )
    if rate is None:
        facts.unknown(ReasonCode.FX_MISSING, f"no EUR reference rate for {currency}", f"fx:{currency}/EUR")
        return facts
    facts.rate = rate
    facts.eur_exact = rate.convert(facts.payable, "EUR").amount if facts.payable else None
    facts.eur_lower = rate.convert(facts.lower_bound, "EUR").amount if facts.lower_bound else None
    facts.common.add(
        ReasonCode.FX_CONVERTED,
        ReasonSeverity.INFO,
        f"converted {currency} to EUR with {rate.provider} reference rate of {rate.rate_date.isoformat()}",
        f"fx:{currency}/EUR",
    )
    return facts


# ---------------------------------------------------------------------------------------------
# Per-profile rules
# ---------------------------------------------------------------------------------------------


def _above_max(value: Decimal, profile: SearchProfile) -> bool:
    return value > profile.max_price_eur if profile.max_price_inclusive else value >= profile.max_price_eur


def _near_boundary(value: Decimal, profile: SearchProfile) -> bool:
    boundaries = [profile.max_price_eur] + (
        [profile.min_price_eur] if profile.min_price_eur is not None else []
    )
    margin = profile.fx_boundary_margin_pct
    return any(abs(value - b) * 100 <= b * margin for b in boundaries)


def _band_rule(facts: _PriceFacts, profile: SearchProfile, as_of: date) -> _Rule:
    rule = _Rule()
    key = profile.key
    stale = facts.rate is not None and facts.rate.is_stale(as_of, profile.fx_max_age_days)
    if facts.eur_exact is not None:
        value = facts.eur_exact
        if stale and _near_boundary(value, profile):
            assert facts.rate is not None
            rule.add(
                ReasonCode.FX_STALE_NEAR_BOUNDARY,
                ReasonSeverity.NEEDS_FACTS,
                f"FX rate is {facts.rate.age_days(as_of)} days old and the EUR value is near a band boundary",
                f"fx:{facts.rate.base}/{facts.rate.quote}",
                key,
            )
            return rule
        if stale:
            assert facts.rate is not None
            rule.add(
                ReasonCode.FX_STALE,
                ReasonSeverity.WARNING,
                f"FX rate is {facts.rate.age_days(as_of)} days old (far from band boundaries)",
                f"fx:{facts.rate.base}/{facts.rate.quote}",
                key,
            )
        if profile.min_price_eur is not None and value < profile.min_price_eur:
            rule.add(
                ReasonCode.PRICE_BELOW_BAND,
                ReasonSeverity.REJECT,
                f"payable EUR {value} is below EUR {profile.min_price_eur}",
                "price.amount_minor",
                key,
            )
        elif _above_max(value, profile):
            bound = "<=" if profile.max_price_inclusive else "<"
            rule.add(
                ReasonCode.PRICE_ABOVE_BAND,
                ReasonSeverity.REJECT,
                f"payable EUR {value} is not {bound} EUR {profile.max_price_eur}",
                "price.amount_minor",
                key,
            )
        else:
            rule.add(
                ReasonCode.PRICE_IN_BAND, ReasonSeverity.INFO, f"payable EUR {value} within band", None, key
            )
        return rule
    if (
        facts.eur_lower is not None
        and _above_max(facts.eur_lower, profile)
        and not (stale and _near_boundary(facts.eur_lower, profile))
    ):
        rule.add(
            ReasonCode.PRICE_ABOVE_BAND,
            ReasonSeverity.REJECT,
            f"payable is at least EUR {facts.eur_lower}, above EUR {profile.max_price_eur}",
            "price.amount_minor",
            key,
        )
        return rule
    reasons = facts.undetermined or [
        ScreeningReason(
            code=ReasonCode.PRICE_MISSING,
            message="payable amount not established",
            field="price.amount_minor",
            severity=ReasonSeverity.NEEDS_FACTS,
        )
    ]
    if facts.common.outcome == "fail" and not facts.undetermined:
        return rule  # the price type already rejects; nothing to add for the band
    for reason in reasons:
        rule.add(reason.code, reason.severity, reason.message, reason.field)
    return rule


def _decimal_or_none(value: str) -> Decimal | None:
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None
    return number if number.is_finite() else None


def _mileage_rule(listing: NormalizedListing, profile: SearchProfile) -> _Rule:
    rule = _Rule()
    key = profile.key
    vehicle = listing.vehicle
    ceiling = profile.max_mileage_km_exclusive
    km = vehicle.mileage_km
    conflicts = [c for c in listing.conflicts if c.field.split(".")[-1].startswith("mileage")]
    if conflicts or vehicle.mileage_claim == OdometerClaim.CONFLICTING:
        values: list[Decimal | None] = [_decimal_or_none(v) for c in conflicts for v in c.values]
        if km is not None:
            values.append(km)
        if values and all(v is not None and v >= ceiling for v in values):
            rule.add(
                ReasonCode.MILEAGE_TOO_HIGH,
                ReasonSeverity.REJECT,
                f"every conflicting odometer statement is >= {ceiling} km",
                "vehicle.mileage_km",
                key,
            )
        else:
            rule.add(
                ReasonCode.MILEAGE_CONFLICT,
                ReasonSeverity.NEEDS_FACTS,
                "conflicting odometer statements (e.g. title vs specification)",
                "vehicle.mileage_km",
            )
        return rule
    if vehicle.mileage_claim == OdometerClaim.RANGE_ONLY:
        low = vehicle.mileage_original.range_low
        if low is not None and vehicle.mileage_original.unit == "mi":
            low = low * MILES_TO_KM
        if low is not None and vehicle.mileage_original.unit != "unknown" and low >= ceiling:
            rule.add(
                ReasonCode.MILEAGE_TOO_HIGH,
                ReasonSeverity.REJECT,
                f"stated mileage range starts at or above {ceiling} km",
                "vehicle.mileage_km",
                key,
            )
        else:
            rule.add(
                ReasonCode.MILEAGE_RANGE_ONLY,
                ReasonSeverity.NEEDS_FACTS,
                "mileage given only as an uncertain range",
                "vehicle.mileage_km",
            )
        return rule
    if km is None:
        rule.add(
            ReasonCode.MILEAGE_MISSING, ReasonSeverity.NEEDS_FACTS, "mileage unknown", "vehicle.mileage_km"
        )
        return rule
    if vehicle.mileage_claim == OdometerClaim.ESTIMATED or vehicle.mileage_original.is_estimate:
        if km < ceiling - ESTIMATE_UNCERTAINTY_KM:
            rule.add(
                ReasonCode.MILEAGE_ESTIMATE,
                ReasonSeverity.WARNING,
                f"mileage is a seller estimate ({km} km)",
                "vehicle.mileage_km",
            )
        elif km >= ceiling + ESTIMATE_UNCERTAINTY_KM:
            rule.add(
                ReasonCode.MILEAGE_TOO_HIGH,
                ReasonSeverity.REJECT,
                f"estimated mileage {km} km is far above the limit",
                "vehicle.mileage_km",
                key,
            )
        else:
            rule.add(
                ReasonCode.MILEAGE_ESTIMATE_NEAR_LIMIT,
                ReasonSeverity.NEEDS_FACTS,
                f"estimated mileage {km} km is too close to the {ceiling} km limit",
                "vehicle.mileage_km",
                key,
            )
        return rule
    if km >= ceiling:
        rule.add(
            ReasonCode.MILEAGE_TOO_HIGH,
            ReasonSeverity.REJECT,
            f"mileage {km} km is not below {ceiling} km",
            "vehicle.mileage_km",
            key,
        )
        return rule
    if vehicle.mileage_claim == OdometerClaim.UNKNOWN:
        rule.add(
            ReasonCode.MILEAGE_CLAIM_UNKNOWN,
            ReasonSeverity.WARNING,
            "odometer claim status not recorded; treated as seller-reported",
            "vehicle.mileage_claim",
        )
    return rule


_SUV_BODY_TYPES = frozenset({BodyType.SUV, BodyType.OFFROAD, BodyType.CROSSOVER})


def _suv_rule(listing: NormalizedListing, match: TaxonomyMatch | None, profile: SearchProfile) -> _Rule:
    rule = _Rule()
    key = profile.key
    body = listing.vehicle.body_type
    if match is not None and match.is_suv is False:
        rule.add(
            ReasonCode.NOT_SUV,
            ReasonSeverity.REJECT,
            f"{match.make} {match.model} is listed as {match.vehicle_class}: {match.exclusion_reason}",
            "vehicle.model",
        )
        if body in profile.body_types:
            rule.add(
                ReasonCode.BODY_TYPE_CONFLICTS_TAXONOMY,
                ReasonSeverity.WARNING,
                f"body type {body.value} contradicts the taxonomy exclusion",
                "vehicle.body_type",
            )
        return rule
    if match is not None and match.is_suv is True and match.vehicle_class is not None:
        taxonomy_body = CLASS_BODY_TYPES[match.vehicle_class]
        if taxonomy_body not in profile.body_types:
            rule.add(
                ReasonCode.NOT_SUV,
                ReasonSeverity.REJECT,
                f"vehicle class {match.vehicle_class} is not in this profile",
                "vehicle.model",
                key,
            )
        elif body not in (BodyType.UNKNOWN, *_SUV_BODY_TYPES):
            rule.add(
                ReasonCode.BODY_TYPE_CONFLICTS_TAXONOMY,
                ReasonSeverity.WARNING,
                f"body type {body.value} contradicts taxonomy class {match.vehicle_class}",
                "vehicle.body_type",
            )
        return rule
    if body in profile.body_types:
        if profile.require_taxonomy_match and match is not None:
            rule.add(
                ReasonCode.TAXONOMY_UNMATCHED,
                ReasonSeverity.WARNING,
                "SUV identity from body type only; model not in the reference taxonomy",
                "vehicle.model",
            )
        return rule
    if body == BodyType.UNKNOWN:
        rule.add(
            ReasonCode.SUV_IDENTITY_UNKNOWN,
            ReasonSeverity.NEEDS_FACTS,
            "SUV identity not established (unknown model and body type)",
            "vehicle.body_type",
        )
        return rule
    rule.add(
        ReasonCode.NOT_SUV,
        ReasonSeverity.REJECT,
        f"body type {body.value} is not an SUV body type for this profile",
        "vehicle.body_type",
        key,
    )
    return rule


def _country_rule(listing: NormalizedListing, profile: SearchProfile) -> _Rule:
    rule = _Rule()
    country = listing.location.country
    if country is None:
        rule.add(
            ReasonCode.COUNTRY_MISSING,
            ReasonSeverity.NEEDS_FACTS,
            "seller country unknown",
            "location.country",
        )
    elif country not in profile.source_countries:
        rule.add(
            ReasonCode.COUNTRY_NOT_IN_PROFILE,
            ReasonSeverity.REJECT,
            f"seller country {country} is not in {', '.join(profile.source_countries)}",
            "location.country",
            profile.key,
        )
    return rule


def _availability_rule(listing: NormalizedListing) -> _Rule:
    rule = _Rule()
    availability = listing.availability
    if availability == Availability.REMOVED:
        rule.add(ReasonCode.AVAILABILITY_REMOVED, ReasonSeverity.REJECT, "listing removed", "availability")
    elif availability == Availability.SOLD_CLAIMED:
        rule.add(
            ReasonCode.AVAILABILITY_SOLD_CLAIMED, ReasonSeverity.REJECT, "listing marked sold", "availability"
        )
    elif availability == Availability.RESERVED:
        rule.add(
            ReasonCode.AVAILABILITY_RESERVED, ReasonSeverity.NEEDS_FACTS, "listing reserved", "availability"
        )
    elif availability == Availability.UNKNOWN:
        rule.add(
            ReasonCode.AVAILABILITY_UNKNOWN,
            ReasonSeverity.WARNING,
            "availability not established",
            "availability",
        )
    return rule


def _combine(rules: Sequence[_Rule]) -> tuple[ProfileOutcome, list[ScreeningReason]]:
    reasons: list[ScreeningReason] = []
    for rule in rules:
        reasons.extend(rule.reasons)
    if any(r.outcome == "fail" for r in rules):
        return "rejected", reasons
    if any(r.outcome == "unknown" for r in rules):
        return "needs_facts", reasons
    return "eligible", reasons


def _dedupe(reasons: Sequence[ScreeningReason]) -> tuple[ScreeningReason, ...]:
    seen: set[tuple[object, ...]] = set()
    result: list[ScreeningReason] = []
    for reason in reasons:
        key = (reason.code, reason.field, reason.severity, reason.profile, reason.message)
        if key not in seen:
            seen.add(key)
            result.append(reason)
    return tuple(result)


# ---------------------------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------------------------


def screen(
    listing: NormalizedListing,
    config: BusinessConfig,
    fx_rates: Sequence[FxRate],
    as_of: date,
    taxonomy: VehicleTaxonomy | None,
) -> ScreeningResult:
    """Screen one normalized listing against every configured profile (see module docstring)."""
    common = _availability_rule(listing)
    facts = _price_facts(listing, fx_rates, as_of)
    match: TaxonomyMatch | None = None
    taxonomy_rule = _Rule()
    if taxonomy is not None:
        vehicle = listing.vehicle
        match = taxonomy.match_vehicle(
            vehicle.make, vehicle.model, listing.title, vehicle.first_registration.year
        )
    else:
        taxonomy_rule.add(
            ReasonCode.TAXONOMY_NOT_SUPPLIED,
            ReasonSeverity.WARNING,
            "no vehicle taxonomy supplied; SUV identity from body type only",
        )

    evaluations: list[ProfileEvaluation] = []
    for key in PROFILE_ORDER:
        profile = config.profiles.get(key)
        if profile is None:
            continue
        outcome, profile_reasons = _combine(
            [
                common,
                facts.common,
                taxonomy_rule,
                _band_rule(facts, profile, as_of),
                _mileage_rule(listing, profile),
                _suv_rule(listing, match, profile),
                _country_rule(listing, profile),
            ]
        )
        evaluations.append(
            ProfileEvaluation(
                profile=key, enabled=profile.enabled, outcome=outcome, reasons=_dedupe(profile_reasons)
            )
        )

    by_key = {e.profile: e for e in evaluations}
    enabled = [e for e in evaluations if e.enabled]
    decisive: ProfileEvaluation | None = None
    state = EligibilityState.REJECTED
    primary = by_key[ProfileKey.PRIMARY]
    if primary.outcome == "eligible":
        decisive, state = primary, EligibilityState.ELIGIBLE_PRIMARY
    else:
        for evaluation in enabled:
            if evaluation.profile != ProfileKey.PRIMARY and evaluation.outcome == "eligible":
                decisive, state = evaluation, EligibilityState.ELIGIBLE_MANUAL_PROFILE
                break
        else:
            needing = [e for e in enabled if e.outcome == "needs_facts"]
            if needing:
                decisive, state = needing[0], EligibilityState.NEEDS_FACTS

    # Report the decisive profile in full (primary when everything is rejected), plus the
    # reject/needs-facts reasons of every other enabled profile.
    reported = decisive if decisive is not None else primary
    reasons: list[ScreeningReason] = list(reported.reasons)
    for evaluation in enabled:
        if evaluation is not reported:
            reasons.extend(
                r
                for r in evaluation.reasons
                if r.severity in (ReasonSeverity.REJECT, ReasonSeverity.NEEDS_FACTS)
            )
    for evaluation in evaluations:
        if evaluation.enabled or evaluation.outcome == "rejected":
            continue
        profile = config.profiles[evaluation.profile]
        if evaluation.outcome == "eligible":
            code, verb = ReasonCode.DISABLED_PROFILE_WOULD_MATCH, "would match"
        else:
            code, verb = ReasonCode.DISABLED_PROFILE_MIGHT_MATCH, "might match (facts missing)"
        reasons.append(
            ScreeningReason(
                code=code,
                message=f"{verb} disabled profile {evaluation.profile.value} ({profile.queue_label})",
                severity=ReasonSeverity.INFO,
                profile=evaluation.profile,
            )
        )
    final_reasons = _dedupe(reasons)
    missing_source = (
        decisive.reasons if decisive is not None and state == EligibilityState.NEEDS_FACTS else ()
    )
    missing = tuple(
        dict.fromkeys(
            r.field
            for r in missing_source
            if r.severity == ReasonSeverity.NEEDS_FACTS and r.field is not None
        )
    )
    return ScreeningResult(
        state=state,
        profile=decisive.profile if decisive is not None else None,
        queue_label=config.profiles[decisive.profile].queue_label if decisive is not None else None,
        eur_amount=facts.eur_exact,
        payable_amount=facts.payable,
        fx_rate_used=facts.rate,
        reasons=final_reasons,
        missing_facts=missing,
        profile_evaluations=tuple(evaluations),
        taxonomy_match=match,
    )
