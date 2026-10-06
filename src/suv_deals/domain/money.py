"""Exact money and FX arithmetic.

Rules (spec sections 7, 17, 18):
- Never use binary floating point for money. All amounts are `Decimal`.
- Persist amounts as integer minor units (EUR 2,750.00 -> 275000) plus ISO currency.
- FX rates store their direction explicitly: one unit of `base` buys `rate` units of `quote`.
  Converting quote -> base divides; base -> quote multiplies. No CHF/EUR parity assumption.
- Conversions return unrounded Decimals. Round only for display, after comparisons.
"""

from __future__ import annotations

import decimal
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.domain.enums import FxPurpose
from suv_deals.errors import ValidationFailed

# ISO 4217 minor-unit exponents for currencies the system may encounter.
CURRENCY_EXPONENTS: dict[str, int] = {
    "EUR": 2,
    "CHF": 2,
    "MKD": 2,
    "USD": 2,
    "GBP": 2,
    "PLN": 2,
    "CZK": 2,
    "HUF": 2,
    "SEK": 2,
    "DKK": 2,
    "NOK": 2,
    "RON": 2,
    "BGN": 2,
    "RSD": 2,
    "BAM": 2,
    "ALL": 2,
    "TRY": 2,
    "JPY": 0,
}

CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

# Wide context for intermediate arithmetic; quantisation happens explicitly.
_CTX = decimal.Context(prec=40, rounding=decimal.ROUND_HALF_EVEN)


def exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError as exc:
        raise ValidationFailed(f"Unsupported currency {currency!r}") from exc


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert to Decimal without ever passing through float."""
    if isinstance(value, float):  # pragma: no cover - defensive, typing forbids it
        raise TypeError("float is not allowed for money")
    if isinstance(value, Decimal):
        return value
    try:
        result = Decimal(value)
    except decimal.InvalidOperation as exc:
        raise ValidationFailed(f"Not a decimal amount: {value!r}") from exc
    if not result.is_finite():
        raise ValidationFailed("Amount must be finite")
    return result


class Money(BaseModel):
    """An exact amount in one currency. Serialises the amount as a string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: CurrencyCode

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("float is not allowed for money; pass a string or Decimal")
        return value

    @field_validator("amount")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, value: str) -> str:
        exponent(value)
        return value

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str) -> Money:
        return cls(amount=to_decimal(amount), currency=currency)

    @classmethod
    def from_minor(cls, minor: int, currency: str) -> Money:
        exp = exponent(currency)
        return cls(amount=Decimal(minor).scaleb(-exp), currency=currency)

    def to_minor(self) -> int:
        """Exact conversion to minor units; raises if sub-minor precision would be lost."""
        exp = exponent(self.currency)
        scaled = self.amount.scaleb(exp)
        if scaled != scaled.to_integral_value():
            raise ValidationFailed(f"{self.amount} {self.currency} has more precision than minor units allow")
        return int(scaled)

    def to_minor_rounded(self) -> int:
        """Round half-up to minor units. Use only for display/storage after comparisons."""
        exp = exponent(self.currency)
        return int(self.amount.scaleb(exp).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def quantized(self) -> Decimal:
        exp = exponent(self.currency)
        return self.amount.quantize(Decimal(1).scaleb(-exp), rounding=ROUND_HALF_UP)

    def display(self) -> str:
        return f"{self.quantized():,.{exponent(self.currency)}f} {self.currency}"

    def _same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValidationFailed(f"Currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(amount=_CTX.add(self.amount, other.amount), currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(amount=_CTX.subtract(self.amount, other.amount), currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount=-self.amount, currency=self.currency)

    def times(self, factor: Decimal | int | str) -> Money:
        return Money(amount=_CTX.multiply(self.amount, to_decimal(factor)), currency=self.currency)

    def compare(self, other: Money) -> int:
        self._same(other)
        return (self.amount > other.amount) - (self.amount < other.amount)

    def __lt__(self, other: Money) -> bool:
        return self.compare(other) < 0

    def __le__(self, other: Money) -> bool:
        return self.compare(other) <= 0

    def __gt__(self, other: Money) -> bool:
        return self.compare(other) > 0

    def __ge__(self, other: Money) -> bool:
        return self.compare(other) >= 0

    @staticmethod
    def zero(currency: str) -> Money:
        return Money(amount=Decimal(0), currency=currency)


class FxRate(BaseModel):
    """One unit of `base` equals `rate` units of `quote` (e.g. ECB: 1 EUR = 0.93 CHF)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: CurrencyCode
    quote: CurrencyCode
    rate: Decimal
    rate_date: date
    retrieved_at: datetime
    provider: str = Field(min_length=1, max_length=100)
    purpose: FxPurpose = FxPurpose.REFERENCE

    @field_validator("rate", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("float is not allowed for FX rates")
        return value

    @field_validator("rate")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("rate must be a positive finite decimal")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _distinct(self) -> FxRate:
        if self.base == self.quote:
            raise ValueError("base and quote must differ")
        return self

    def convert(self, money: Money, to_currency: str) -> Money:
        """Unrounded conversion honouring the stored direction."""
        if money.currency == to_currency:
            return money
        if money.currency == self.base and to_currency == self.quote:
            return Money(amount=_CTX.multiply(money.amount, self.rate), currency=to_currency)
        if money.currency == self.quote and to_currency == self.base:
            return Money(amount=_CTX.divide(money.amount, self.rate), currency=to_currency)
        raise ValidationFailed(
            f"Rate {self.base}/{self.quote} cannot convert {money.currency} to {to_currency}"
        )

    def age_days(self, as_of: date) -> int:
        return (as_of - self.rate_date).days

    def is_stale(self, as_of: date, max_age_days: int) -> bool:
        return self.age_days(as_of) > max_age_days


def convert_to_eur(money: Money, rates: list[FxRate]) -> Money | None:
    """Return the unrounded EUR equivalent, or None if no usable rate is supplied."""
    if money.currency == "EUR":
        return money
    for rate in rates:
        if {rate.base, rate.quote} == {"EUR", money.currency}:
            return rate.convert(money, "EUR")
    return None
