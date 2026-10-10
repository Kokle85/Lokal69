"""ECB euro reference rates: parsing, gated fetching and rate selection (spec section 18 "FX rules").

Rules:
- The ECB publishes ``1 EUR = x CCY``. Each parsed observation is stored as
  ``FxRate(base="EUR", quote=CCY, rate=x)`` with ``purpose=reference`` and
  ``provider="ECB"``; ``rate_date`` is the ``Cube@time`` date. Converting CHF to EUR
  therefore divides by the EUR/CHF rate (``FxRate.convert`` honours the direction).
  No CHF/EUR parity is ever assumed.
- Reference rates support estimation only. A payable bank rate (``payment``) and a
  customs-prescribed rate (``customs``) are separate observations.
- The ECB does not publish MKD. Nothing here returns an MKD rate and no peg is
  assumed: an MKD rate needs an owner-approved source (for example the National Bank
  of the Republic of North Macedonia) recorded with its own provider and purpose.
- XML is parsed defensively: size-bounded, UTF-8 only, DTD/entity declarations
  refused (no XXE, no entity expansion), no network access, no DTD loading.
- Fetching is disabled unless ``settings.fx_fetch_enabled`` is true; the URL must be
  HTTPS on an allow-listed ECB host. Stale or missing rates produce warnings; callers
  decide whether that blocks an exact-price decision near a threshold.
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

from lxml import etree
from pydantic import BaseModel, ConfigDict

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.enums import FxPurpose
from suv_deals.domain.listings import sha256_bytes
from suv_deals.domain.money import FxRate, Money
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode, ValidationFailed
from suv_deals.netguard import UnsafeDestination, parse_safe_url
from suv_deals.settings import Settings

ECB_PROVIDER: Final = "ECB"
ECB_ALLOWED_HOSTS: Final = frozenset({"www.ecb.europa.eu"})
MAX_ECB_XML_BYTES: Final = 2_000_000
FETCH_TIMEOUT_S: Final = 20.0
#: Currencies the ECB does not publish; an ECB-labelled row for them is never trusted.
ECB_UNPUBLISHED: Final = frozenset({"MKD"})
MKD_SOURCE_NOTE: Final = (
    "The ECB publishes no MKD reference rate. An MKD rate needs an owner-approved source "
    "(for example the National Bank of the Republic of North Macedonia); no peg is assumed."
)

_GESMES_NS: Final = "http://www.gesmes.org/xml/2002-08-01"
_ECB_NS: Final = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_CUBE: Final = f"{{{_ECB_NS}}}Cube"
_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")
_RATE_RE: Final = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,12})?$")
_FORBIDDEN_MARKUP: Final = (b"<!doctype", b"<!entity", b"<!element", b"<!attlist", b"<!notation")
_CTX: Final = decimal.Context(prec=50)


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse_ecb_daily_xml(data: bytes, *, retrieved_at: datetime) -> list[FxRate]:
    """Parse an ECB ``eurofxref`` XML document (daily or multi-day) into reference rates.

    Raises ``ValidationFailed`` for oversize, non-UTF-8, DTD/entity-bearing, malformed or
    structurally unexpected documents, duplicate (date, currency) rows and invalid rates.
    MKD rows are dropped (the ECB does not publish MKD).
    """
    retrieved_at = ensure_utc(retrieved_at)
    if len(data) > MAX_ECB_XML_BYTES:
        raise ValidationFailed("ECB XML document exceeds the size limit")
    if not data.strip():
        raise ValidationFailed("ECB XML document is empty")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("ECB XML document must be UTF-8") from exc
    lowered = data.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKUP):
        raise ValidationFailed("DTD and entity declarations are not allowed in rate documents")
    try:
        root = etree.fromstring(data, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise ValidationFailed("ECB XML document is not well-formed") from exc
    docinfo = root.getroottree().docinfo
    if docinfo.doctype or docinfo.internalDTD is not None:
        raise ValidationFailed("DTD declarations are not allowed in rate documents")
    if root.tag != f"{{{_GESMES_NS}}}Envelope":
        raise ValidationFailed("unexpected root element; not an ECB eurofxref document")
    outer = root.find(_CUBE)
    if outer is None:
        raise ValidationFailed("ECB document has no Cube element")

    rates: list[FxRate] = []
    seen: set[tuple[date, str]] = set()
    for day_cube in outer.findall(_CUBE):
        raw_time = day_cube.get("time")
        if raw_time is None:
            raise ValidationFailed("ECB Cube without a time attribute")
        try:
            rate_date = date.fromisoformat(raw_time.strip())
        except ValueError as exc:
            raise ValidationFailed("ECB Cube time is not an ISO date") from exc
        for row in day_cube.findall(_CUBE):
            currency = (row.get("currency") or "").strip()
            raw_rate = (row.get("rate") or "").strip()
            if not _CURRENCY_RE.fullmatch(currency) or currency == "EUR":
                raise ValidationFailed("ECB row has an invalid currency code")
            if not _RATE_RE.fullmatch(raw_rate):
                raise ValidationFailed(f"ECB row for {currency} has an invalid rate")
            try:
                value = Decimal(raw_rate)
            except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
                raise ValidationFailed(f"ECB row for {currency} has an invalid rate") from exc
            if value <= 0:
                raise ValidationFailed(f"ECB row for {currency} has a non-positive rate")
            key = (rate_date, currency)
            if key in seen:
                raise ValidationFailed(f"duplicate ECB row for {currency} on {rate_date.isoformat()}")
            seen.add(key)
            if currency in ECB_UNPUBLISHED:
                continue
            rates.append(
                FxRate(
                    base="EUR",
                    quote=currency,
                    rate=value,
                    rate_date=rate_date,
                    retrieved_at=retrieved_at,
                    provider=ECB_PROVIDER,
                    purpose=FxPurpose.REFERENCE,
                )
            )
    if not rates:
        raise ValidationFailed("ECB document contains no usable rates")
    rates.sort(key=lambda r: (r.rate_date, r.quote))
    return rates


class FxHttpResponse(Protocol):
    """Read-only view of a bounded HTTP response (``SafeResponse`` satisfies it)."""

    @property
    def status_code(self) -> int: ...

    @property
    def body(self) -> bytes: ...

    @property
    def truncated(self) -> bool: ...


class FxHttpClient(Protocol):
    """Outbound GET (``integrations.safe_http.SafeHttpClient`` satisfies it)."""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> FxHttpResponse: ...


class EcbFetchResult(BaseModel):
    """Parsed rates plus retrieval metadata for provenance (spec 18: provider, date, time)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rates: tuple[FxRate, ...]
    provider: str = ECB_PROVIDER
    source_url: str
    retrieved_at: datetime
    sha256: str
    byte_count: int
    rate_dates: tuple[date, ...]
    notes: tuple[str, ...] = (MKD_SOURCE_NOTE,)


def _validated_ecb_url(url: str) -> str:
    try:
        target = parse_safe_url(url, allowed_schemes=("https",), allowed_ports=(443,))
    except UnsafeDestination as exc:
        raise ValidationFailed("FX_ECB_DAILY_URL is not an allowed HTTPS URL") from exc
    if target.hostname not in ECB_ALLOWED_HOSTS:
        raise ValidationFailed("FX_ECB_DAILY_URL host is not an allow-listed ECB host")
    return target.url


async def fetch_ecb_daily(
    settings: Settings, client: FxHttpClient, *, clock: Clock | None = None
) -> EcbFetchResult:
    """Fetch and parse the ECB daily reference rates. Refuses unless FX fetching is enabled.

    Transport errors raised by ``client`` propagate unchanged. Upstream HTTP errors and
    rejected documents raise ``DEPENDENCY_UNAVAILABLE``; messages never include the URL.
    """
    if not settings.fx_fetch_enabled:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "FX fetching is disabled (FX_FETCH_ENABLED=false)",
            retryable=False,
        )
    url = _validated_ecb_url(settings.fx_ecb_daily_url)
    response = await client.get(
        url,
        headers={"Accept": "application/xml, text/xml"},
        timeout_s=FETCH_TIMEOUT_S,
        max_response_bytes=MAX_ECB_XML_BYTES,
    )
    if response.truncated:
        raise DependencyUnavailable("ECB reference rate response exceeded the size limit")
    if not 200 <= response.status_code < 300:
        raise DependencyUnavailable(f"ECB reference rates unavailable (HTTP {response.status_code})")
    retrieved_at = (clock or SystemClock()).now()
    body = bytes(response.body)
    try:
        rates = parse_ecb_daily_xml(body, retrieved_at=retrieved_at)
    except ValidationFailed as exc:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "ECB reference rate document was rejected",
            retryable=True,
            details={"reason": exc.message},
        ) from exc
    return EcbFetchResult(
        rates=tuple(rates),
        source_url=url,
        retrieved_at=ensure_utc(retrieved_at),
        sha256=sha256_bytes(body),
        byte_count=len(body),
        rate_dates=tuple(sorted({r.rate_date for r in rates})),
    )


def _same_rate(a: FxRate, b: FxRate) -> bool:
    """True when two observations state the same rate: numerically equal in the same
    direction (``0.94`` == ``0.9400``) or exact inverses when stored the other way round."""
    if (a.base, a.quote) == (b.base, b.quote):
        return a.rate == b.rate
    return (a.base, a.quote) == (b.quote, b.base) and _CTX.multiply(a.rate, b.rate) == 1


def select_rate(  # noqa: PLR0917 - positional order is the documented package contract
    rates: Iterable[FxRate],
    base: str,
    quote: str,
    on_date: date,
    max_age_days: int,
    purpose: FxPurpose = FxPurpose.REFERENCE,
    *,
    provider: str | None = None,
) -> tuple[FxRate | None, tuple[str, ...]]:
    """Latest rate for the currency pair (either stored direction) and purpose on or before
    ``on_date``. Pure.

    Returns ``(None, warnings)`` when no rate exists or same-day providers disagree;
    a rate older than ``max_age_days`` is returned with an ``FX_RATE_STALE`` warning so
    the caller can block exact-price decisions near a threshold. Future-dated rates are
    ignored. Customs, payment and reference purposes are never mixed.
    """
    if base == quote:
        raise ValidationFailed("base and quote currency must differ")
    if max_age_days < 0:
        raise ValidationFailed("max_age_days must not be negative")
    pair = {base, quote}
    warnings: list[str] = []
    matching = [
        r
        for r in rates
        if {r.base, r.quote} == pair and r.purpose == purpose and (provider is None or r.provider == provider)
    ]
    if any(r.rate_date > on_date for r in matching):
        warnings.append(f"FX_RATE_FUTURE_IGNORED:{base}/{quote}")
    candidates = [r for r in matching if r.rate_date <= on_date]
    if not candidates:
        message = f"FX_RATE_MISSING:{base}/{quote}:{purpose.value}"
        if "MKD" in pair:
            message += f" ({MKD_SOURCE_NOTE})"
        warnings.append(message)
        return None, tuple(warnings)
    latest = max(r.rate_date for r in candidates)
    # Prefer an observation stored in the requested direction, then by provider name.
    same_day = sorted(
        (r for r in candidates if r.rate_date == latest),
        key=lambda r: (r.base != base, r.provider, str(r.rate)),
    )
    chosen = same_day[0]
    if any(not _same_rate(chosen, other) for other in same_day[1:]):
        distinct = sorted({f"{r.provider}:{r.base}/{r.quote}={r.rate}" for r in same_day})
        warnings.append(f"FX_RATE_AMBIGUOUS:{base}/{quote} {latest.isoformat()}: " + ", ".join(distinct))
        return None, tuple(warnings)
    age = chosen.age_days(on_date)
    if age > max_age_days:
        warnings.append(
            f"FX_RATE_STALE:{chosen.base}/{chosen.quote} {chosen.rate_date.isoformat()} is {age} days old "
            f"(max {max_age_days})"
        )
    return chosen, tuple(warnings)


def convert_money(
    money: Money,
    to_currency: str,
    rates: Iterable[FxRate],
    on_date: date,
    max_age_days: int,
    *,
    purpose: FxPurpose = FxPurpose.REFERENCE,
) -> tuple[Money | None, tuple[str, ...]]:
    """Unrounded conversion via ``select_rate``; ``None`` (never a guess) when no rate exists."""
    if money.currency == to_currency:
        return money, ()
    rate, warnings = select_rate(rates, money.currency, to_currency, on_date, max_age_days, purpose)
    if rate is None:
        return None, warnings
    return rate.convert(money, to_currency), warnings
