"""Locale-aware parsing of listing text: numbers, prices, mileage, power, displacement, dates.

Pure functions, no I/O (architecture: domain modules are pure). Every parser returns a small
frozen result model carrying the parsed value *or* ``None`` plus machine-readable warnings.

Binding rules (spec sections 2, 3, 7, 17, 31):

- Never use binary floating point. All amounts are ``Decimal``.
- Unknown stays unknown: an unparseable, missing or ambiguous value is ``None`` with a warning,
  never ``0`` and never a guess.
- Locale conventions (``de``/``it``/``mk``: ``.`` groups, ``,`` decimal; ``ch``: apostrophes
  ``'``/U+2019 and spaces group, ``.`` decimal; ``en``: ``,`` groups, ``.`` decimal). Spaces,
  NBSP (U+00A0), narrow NBSP (U+202F), thin space (U+2009) and figure space (U+2007) are group
  separators in every locale; so are apostrophes.
- A lone decimal separator followed by exactly three digits (``2,750`` in ``de``, ``2.750`` in
  ``ch``/``en``, either under an unknown locale) is ambiguous between a thousands group and a
  three-digit fraction and yields ``None`` + ``AMBIGUOUS_SEPARATOR``.
- Input that violates the declared locale (``1.5`` in ``de``; ``2,750.50`` in ``de``) is never
  reinterpreted under another locale: ``None`` + warning.
- Miles convert to km with the exact factor ``1.609344`` and the result is kept unrounded
  (spec 7) so that rounding can never cross the 200,000 km threshold.
- A first-registration month is never turned into an invented day (spec 7).
- A zone-less source timestamp gets the documented source zone and ``zone_assumed=True``; it is
  never silently treated as UTC (spec 7).
- Technical-inspection wording (HU/TÜV, Swiss MFK/expertise, Italian revisione) is a seller claim,
  never a verified inspection; negated, expired or conditional wording is never positive.
- Swiss price wording (``CHF``/``Fr.`` with apostrophe grouping and ``.-``/dash endings, ``MWST``,
  ``TVA``, ``Exportpreis``, ``Händlerpreis``) is parsed like DE/IT wording; the Swiss VAT rate is
  only ever *recorded as stated*, never assumed.
"""

from __future__ import annotations

import decimal
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    ClaimStatus,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.listings import MileageOriginal, PartialDate, SourceTimestamp
from suv_deals.domain.money import Money, exponent
from suv_deals.errors import ValidationFailed

Locale = Literal["de", "it", "ch", "mk", "en"]
SUPPORTED_LOCALES: tuple[Locale, ...] = ("de", "it", "ch", "mk", "en")

# Country -> number-format locale. Only countries whose listing conventions were reviewed are
# mapped; anything else returns None (unknown locale -> conservative unknown-locale parsing).
COUNTRY_LOCALES: dict[str, Locale] = {
    "DE": "de",
    "AT": "de",
    "IT": "it",
    "CH": "ch",
    "MK": "mk",
}

MILES_TO_KM = Decimal("1.609344")  # exact international mile (spec 7)
PS_TO_KW = Decimal("0.73549875")  # metric horsepower (PS/CV) -> kW, exact by definition
HP_TO_KW = Decimal("0.745699872")  # mechanical horsepower (hp/bhp) -> kW
POWER_CONFLICT_TOLERANCE_KW = Decimal("2")
MAX_INPUT_LENGTH = 500
MAX_DIGITS = 15  # far above any vehicle price/mileage; keeps Decimal arithmetic exact
FUTURE_TOLERANCE = timedelta(days=1)
MIN_PLAUSIBLE_YEAR = 1950
MAX_PLAUSIBLE_MILEAGE_KM = Decimal("2000000")

_CTX = decimal.Context(prec=40, rounding=decimal.ROUND_HALF_EVEN)
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ParseWarning(StrEnum):
    """Machine-readable parse warnings. Values are persisted in listing ``warnings``."""

    EMPTY_INPUT = "EMPTY_INPUT"
    INPUT_TOO_LONG = "INPUT_TOO_LONG"
    NO_NUMBER = "NO_NUMBER"
    MULTIPLE_NUMBERS = "MULTIPLE_NUMBERS"
    NEGATIVE_VALUE = "NEGATIVE_VALUE"
    AMBIGUOUS_SEPARATOR = "AMBIGUOUS_SEPARATOR"
    INVALID_GROUPING = "INVALID_GROUPING"
    LOCALE_MISMATCH = "LOCALE_MISMATCH"
    UNKNOWN_LOCALE = "UNKNOWN_LOCALE"
    # prices
    CURRENCY_MISSING = "CURRENCY_MISSING"
    CURRENCY_DEFAULTED = "CURRENCY_DEFAULTED"
    CONFLICTING_CURRENCIES = "CONFLICTING_CURRENCIES"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    MULTIPLE_AMOUNTS = "MULTIPLE_AMOUNTS"
    MULTIPLE_PRICE_TYPES = "MULTIPLE_PRICE_TYPES"
    CONFLICTING_VAT_WORDING = "CONFLICTING_VAT_WORDING"
    MULTIPLE_VAT_RATES = "MULTIPLE_VAT_RATES"
    CONFLICTING_PRICE_BASIS = "CONFLICTING_PRICE_BASIS"
    CONFLICTING_NEGOTIABILITY = "CONFLICTING_NEGOTIABILITY"
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_ZERO_PLACEHOLDER = "PRICE_ZERO_PLACEHOLDER"
    SUB_MINOR_PRECISION = "SUB_MINOR_PRECISION"
    # mileage
    MILEAGE_UNKNOWN = "MILEAGE_UNKNOWN"
    MILEAGE_UNIT_MISSING = "MILEAGE_UNIT_MISSING"
    MILEAGE_UNIT_DEFAULTED = "MILEAGE_UNIT_DEFAULTED"
    CONFLICTING_UNITS = "CONFLICTING_UNITS"
    MILEAGE_ESTIMATE = "MILEAGE_ESTIMATE"
    MILEAGE_RANGE_ONLY = "MILEAGE_RANGE_ONLY"
    MILEAGE_BOUND_ONLY = "MILEAGE_BOUND_ONLY"
    MILES_CONVERTED = "MILES_CONVERTED"
    MILEAGE_ZERO_TREATED_UNKNOWN = "MILEAGE_ZERO_TREATED_UNKNOWN"
    MILEAGE_IMPLAUSIBLE = "MILEAGE_IMPLAUSIBLE"
    MILEAGE_NEGATIVE = "MILEAGE_NEGATIVE"
    # power / displacement
    POWER_MISSING = "POWER_MISSING"
    POWER_CONFLICT = "POWER_CONFLICT"
    POWER_DERIVED = "POWER_DERIVED"
    POWER_ROUNDED = "POWER_ROUNDED"
    MULTIPLE_POWER_VALUES = "MULTIPLE_POWER_VALUES"
    POWER_IMPLAUSIBLE = "POWER_IMPLAUSIBLE"
    DISPLACEMENT_UNIT_MISSING = "DISPLACEMENT_UNIT_MISSING"
    DISPLACEMENT_FROM_LITRES = "DISPLACEMENT_FROM_LITRES"
    DISPLACEMENT_IMPLAUSIBLE = "DISPLACEMENT_IMPLAUSIBLE"
    MULTIPLE_DISPLACEMENTS = "MULTIPLE_DISPLACEMENTS"
    # dates
    DATE_UNPARSEABLE = "DATE_UNPARSEABLE"
    INVALID_DATE = "INVALID_DATE"
    TWO_DIGIT_YEAR = "TWO_DIGIT_YEAR"
    MULTIPLE_DATES = "MULTIPLE_DATES"
    IMPLAUSIBLE_YEAR = "IMPLAUSIBLE_YEAR"
    FUTURE_DATE = "FUTURE_DATE"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    ZONE_ASSUMED = "ZONE_ASSUMED"
    DATE_ONLY_START_OF_DAY_ASSUMED = "DATE_ONLY_START_OF_DAY_ASSUMED"
    DST_GAP_NONEXISTENT_TIME = "DST_GAP_NONEXISTENT_TIME"
    DST_FOLD_AMBIGUOUS_TIME = "DST_FOLD_AMBIGUOUS_TIME"
    TWO_DIGIT_YEAR_EXPANDED = "TWO_DIGIT_YEAR_EXPANDED"
    # technical inspection / roadworthiness wording
    INSPECTION_EXPIRED = "INSPECTION_EXPIRED"
    INSPECTION_NOT_VALID = "INSPECTION_NOT_VALID"
    INSPECTION_NOT_FRESH = "INSPECTION_NOT_FRESH"
    INSPECTION_CONFLICTING = "INSPECTION_CONFLICTING"
    INSPECTION_CONDITIONAL = "INSPECTION_CONDITIONAL"
    INSPECTION_DATE_AMBIGUOUS = "INSPECTION_DATE_AMBIGUOUS"
    INSPECTION_DATE_IMPLAUSIBLE = "INSPECTION_DATE_IMPLAUSIBLE"
    INSPECTION_EXPIRY_IMPRECISE = "INSPECTION_EXPIRY_IMPRECISE"


def locale_for_country(country: str | None) -> Locale | None:
    """Return the reviewed number-format locale for an ISO country code, or None if unknown."""
    if not country:
        return None
    return COUNTRY_LOCALES.get(country.strip().upper())


# ---------------------------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------------------------

_SPACE_SEPARATORS = frozenset(" \u00a0\u202f\u2009\u2007")
_APOSTROPHES = frozenset("'\u2019\u02bc")
_SEP_CLASS = r"[.,'\u2019\u02bc \u00a0\u202f\u2009\u2007]"
_DASHES = "-\u2013\u2014"
# A number token: digit groups joined by single separator characters, optionally followed by a
# "no cents" dash marker such as ``2.750,-`` (DE) or ``12'500.\u2013`` (CH).
_NUMBER_TOKEN = re.compile(rf"[0-9]+(?:{_SEP_CLASS}[0-9]+)*(?:[.,][{_DASHES}](?![0-9]))?")
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


@dataclass(frozen=True, slots=True)
class _LocaleFormat:
    group_marks: frozenset[str]  # '.' and/or ',' acting as group separators
    decimal_mark: str | None  # None = unknown locale; decided structurally


_LOCALE_FORMATS: dict[Locale, _LocaleFormat] = {
    "de": _LocaleFormat(frozenset({"."}), ","),
    "it": _LocaleFormat(frozenset({"."}), ","),
    "mk": _LocaleFormat(frozenset({"."}), ","),
    "ch": _LocaleFormat(frozenset(), "."),
    "en": _LocaleFormat(frozenset({","}), "."),
}


class NumberParse(BaseModel):
    """Result of parsing one number. ``value`` is None whenever the text is not unambiguous."""

    model_config = _FROZEN

    value: Decimal | None = None
    ambiguous: bool = False
    warnings: tuple[str, ...] = ()
    raw: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    start: int
    end: int


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return _ZERO_WIDTH.sub("", text)


def _tokens(text: str) -> list[_Token]:
    return [_Token(m.group(0), m.start(), m.end()) for m in _NUMBER_TOKEN.finditer(text)]


def _sep_kind(char: str) -> str:
    if char in _SPACE_SEPARATORS:
        return "space"
    if char in _APOSTROPHES:
        return "apostrophe"
    return char


def _interpret_token(token: str, locale: Locale | None) -> NumberParse:
    """Interpret one number token under a locale. Pure structural rules; see module docstring."""
    body = token
    dash_decimal: str | None = None
    if len(body) >= 2 and body[-1] in _DASHES and body[-2] in ".,":
        dash_decimal = body[-2]
        body = body[:-2]

    if sum(c.isdigit() for c in body) > MAX_DIGITS:
        return NumberParse(warnings=(ParseWarning.INPUT_TOO_LONG,), raw=token)
    parts = re.split(_SEP_CLASS, body)
    seps = [c for c in body if not c.isdigit()]
    if not seps:
        return NumberParse(value=Decimal(body), raw=token)

    fmt = _LOCALE_FORMATS[locale] if locale is not None else None
    if fmt is not None and dash_decimal is not None and dash_decimal != fmt.decimal_mark:
        return NumberParse(ambiguous=True, warnings=(ParseWarning.LOCALE_MISMATCH,), raw=token)

    # Decide the role of '.' and ','.
    punct = [c for c in seps if c in ".,"]
    decimal_mark: str | None
    if fmt is not None:
        foreign = [c for c in punct if c not in fmt.group_marks and c != fmt.decimal_mark]
        if foreign:
            return NumberParse(ambiguous=True, warnings=(ParseWarning.LOCALE_MISMATCH,), raw=token)
        decimal_mark = fmt.decimal_mark
    else:
        kinds = set(punct)
        if dash_decimal is not None:
            decimal_mark = dash_decimal
        elif len(kinds) == 2:
            decimal_mark = punct[-1]
        elif len(kinds) == 1:
            mark = punct[0]
            if punct.count(mark) > 1:
                decimal_mark = None  # repeated mark can only be grouping
            else:
                tail = parts[-1] if seps[-1] == mark else None
                if tail is None:
                    decimal_mark = None
                elif len(tail) == 3 and parts[0] != "0" and len(seps) == 1:
                    return NumberParse(
                        ambiguous=True,
                        warnings=(ParseWarning.AMBIGUOUS_SEPARATOR, ParseWarning.UNKNOWN_LOCALE),
                        raw=token,
                    )
                else:
                    decimal_mark = mark
        else:
            decimal_mark = None

    # Split integer/fraction at the decimal mark (must be the last separator, at most once).
    decimal_positions = [i for i, c in enumerate(seps) if c == decimal_mark]
    if len(decimal_positions) > 1 or (decimal_positions and decimal_positions[0] != len(seps) - 1):
        return NumberParse(ambiguous=True, warnings=(ParseWarning.INVALID_GROUPING,), raw=token)
    if dash_decimal is not None and decimal_positions:
        return NumberParse(ambiguous=True, warnings=(ParseWarning.INVALID_GROUPING,), raw=token)
    if decimal_positions:
        int_parts, fraction = parts[:-1], parts[-1]
        group_seps = seps[:-1]
    else:
        int_parts, fraction = parts, ""
        group_seps = seps

    if group_seps:
        if len({_sep_kind(c) for c in group_seps}) > 1:
            return NumberParse(ambiguous=True, warnings=(ParseWarning.INVALID_GROUPING,), raw=token)
        if int_parts[0].startswith("0"):
            # '0.750' (de) / "0'750" (ch): a leading zero never starts a thousands grouping; it is
            # far more likely a foreign-locale fraction (0.75), so it is never read as 750.
            return NumberParse(ambiguous=True, warnings=(ParseWarning.INVALID_GROUPING,), raw=token)
        if not 1 <= len(int_parts[0]) <= 3 or any(len(p) != 3 for p in int_parts[1:]):
            # e.g. '1.5' in de: the group mark is not followed by a three-digit group.
            return NumberParse(ambiguous=True, warnings=(ParseWarning.AMBIGUOUS_SEPARATOR,), raw=token)
    elif decimal_positions and len(fraction) == 3 and int_parts[0] != "0":
        # e.g. '2,750' in de or '2.750' in ch/en: thousands group or 3-digit fraction?
        return NumberParse(ambiguous=True, warnings=(ParseWarning.AMBIGUOUS_SEPARATOR,), raw=token)

    digits = "".join(int_parts) + ("." + fraction if fraction else "")
    return NumberParse(value=Decimal(digits), raw=token)


def _too_long_or_empty(text: str | None) -> ParseWarning | None:
    if text is None or not text.strip():
        return ParseWarning.EMPTY_INPUT
    if len(text) > MAX_INPUT_LENGTH:
        return ParseWarning.INPUT_TOO_LONG
    return None


def _bounded_raw(text: str | None, limit: int = MAX_INPUT_LENGTH) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_negative(text: str, token: _Token) -> bool:
    before = text[: token.start].rstrip(" \u00a0")
    return before.endswith(("-", "\u2212")) and not re.search(r"[0-9]\s*[-\u2212]$", before)


def parse_number(text: str | None, locale: Locale | None) -> NumberParse:
    """Parse exactly one non-negative number from ``text`` under ``locale``.

    ``locale=None`` means unknown: only structurally unambiguous input is accepted
    (``2,750`` -> None + ``AMBIGUOUS_SEPARATOR``). Surrounding words/units are ignored
    (``'199.999 km'`` in ``de`` -> 199999). More than one number -> None + ``MULTIPLE_NUMBERS``.
    """
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return NumberParse(warnings=(problem or ParseWarning.EMPTY_INPUT,), raw=_bounded_raw(text))
    cleaned = _clean(text)
    toks = _tokens(cleaned)
    if not toks:
        return NumberParse(warnings=(ParseWarning.NO_NUMBER,), raw=text)
    if len(toks) > 1:
        return NumberParse(ambiguous=True, warnings=(ParseWarning.MULTIPLE_NUMBERS,), raw=text)
    if _is_negative(cleaned, toks[0]):
        return NumberParse(warnings=(ParseWarning.NEGATIVE_VALUE,), raw=text)
    result = _interpret_token(toks[0].text.strip(), locale)
    return result.model_copy(update={"raw": text})


# ---------------------------------------------------------------------------------------------
# Prices (spec 17)
# ---------------------------------------------------------------------------------------------

_I = re.IGNORECASE


def _rx(pattern: str, flags: int = _I) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_NL = r"(?<![^\W\d_])"  # not preceded by a letter (digits may touch: "2750EUR")
_NR = r"(?![^\W\d_])"  # not followed by a letter

_CURRENCY_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_rx("\u20ac", 0), "EUR"),
    (_rx(rf"{_NL}EUR{_NR}"), "EUR"),
    (_rx(rf"{_NL}euro{_NR}(?!\s*[1-6]\b)"), "EUR"),  # not the "Euro 5" emissions class
    (_rx(rf"{_NL}CHF{_NR}"), "CHF"),
    (_rx(rf"{_NL}[sS]?Fr\.", 0), "CHF"),
    (_rx(rf"{_NL}SFr{_NR}", 0), "CHF"),  # bare "Fr" without a dot is not matched ("Fr" = Friday)
    (_rx(rf"{_NL}MKD{_NR}"), "MKD"),
    (_rx(rf"{_NL}ден(?:ари|ар)?{_NR}\.?"), "MKD"),  # noqa: RUF001 (Macedonian Cyrillic wording)
    (_rx("\u00a3", 0), "GBP"),
    (_rx(rf"{_NL}GBP{_NR}"), "GBP"),
    (_rx(rf"{_NL}USD{_NR}|{_NL}US\$", 0), "USD"),
)

_NOT_POSSIBLE = (
    r"(?!\s+(?:möglich|moeglich|possibile|possible|disponibile|available|auf\s+anfrage|su\s+richiesta))"
)
# Negated damage wording ("keine Defekte", "ohne Motorschaden", "non incidentata") states the
# opposite and must never classify the vehicle as parts/damaged.
_NOT_NEGATED = r"(?<!kein )(?<!keine )(?<!keinen )(?<!ohne )(?<!nicht )(?<!non )(?<!senza )(?<!no )(?<!not )"

# (pattern, PriceType) in no particular order; precedence is applied separately.
_PRICE_TYPE_MARKERS: tuple[tuple[re.Pattern[str], PriceType], ...] = (
    (_rx(r"/\s*monat\b"), PriceType.INSTALMENT),
    (_rx(r"\bmtl\b\.?"), PriceType.INSTALMENT),
    (_rx(r"\bpro\s+monat\b"), PriceType.INSTALMENT),
    (_rx(r"\bmonatlich\w*"), PriceType.INSTALMENT),
    (_rx(r"\bmonatsrate\b"), PriceType.INSTALMENT),
    (_rx(r"\bal\s+mese\b"), PriceType.INSTALMENT),
    (_rx(r"/\s*mese\b"), PriceType.INSTALMENT),
    (_rx(r"\brata\b|\brate\s+(?:da|ab|mensili)\b|\bmonatliche\s+rate\b"), PriceType.INSTALMENT),
    (_rx(r"\bfinanzierung\s+ab\b"), PriceType.INSTALMENT),
    (_rx(r"\bfinanziamento\s+da\b"), PriceType.INSTALMENT),
    (_rx(r"\bper\s+month\b|/\s*month\b|\bmonthly\b"), PriceType.INSTALMENT),
    (_rx(r"\bleasing(?:rate)?\b" + _NOT_POSSIBLE), PriceType.LEASING),
    (_rx(r"(?<!ohne )(?<!senza )\banzahlung\b"), PriceType.DEPOSIT),
    (_rx(r"(?<!senza )\banticipo\b"), PriceType.DEPOSIT),
    (_rx(r"\bcaparra\b"), PriceType.DEPOSIT),
    (_rx(r"(?<!no )\bdeposit\b"), PriceType.DEPOSIT),
    (_rx(r"\bstartpreis\b"), PriceType.AUCTION_START),
    (_rx(r"\bmindestgebot\b"), PriceType.AUCTION_START),
    (_rx(r"\bbase\s+d['\u2019]\s*asta\b"), PriceType.AUCTION_START),
    (_rx(r"\basta\b"), PriceType.AUCTION_START),
    (_rx(r"\b(?:auktion|versteigerung|auction)\b"), PriceType.AUCTION_START),
    (_rx(r"\bstarting\s+bid\b"), PriceType.AUCTION_START),
    (_rx(r"\b(?:aktuelles\s+)?gebot\b"), PriceType.AUCTION_CURRENT_BID),
    (_rx(r"\bhöchstgebot\b"), PriceType.AUCTION_CURRENT_BID),
    (_rx(r"\b(?:current\s+bid|offerta\s+attuale)\b"), PriceType.AUCTION_CURRENT_BID),
    (_rx(r"\bexport(?:preis)?\b" + _NOT_POSSIBLE), PriceType.EXPORT_NET),
    (_rx(r"\bh(?:ä|ae)ndlerpreis\b"), PriceType.EXPORT_NET),
    (_rx(r"\b(?:export\s+price|prezzo\s+export)\b"), PriceType.EXPORT_NET),
    (_rx(r"\bprix\s+(?:[àa]\s+l['\u2019]\s*)?export\b"), PriceType.EXPORT_NET),  # Swiss romandie
    (_rx(_NOT_NEGATED + r"\bbastler\w*"), PriceType.PARTS_OR_DAMAGED),
    (_rx(_NOT_NEGATED + r"\bdefekt\w*"), PriceType.PARTS_OR_DAMAGED),
    (
        _rx(_NOT_NEGATED + r"\b(?:motorschaden|getriebeschaden|unfallwagen|unfallfahrzeug)\b"),
        PriceType.PARTS_OR_DAMAGED,
    ),
    (_rx(r"\bper\s+ricambi\b"), PriceType.PARTS_OR_DAMAGED),
    (_rx(_NOT_NEGATED + r"\bincidentat[ao]\b"), PriceType.PARTS_OR_DAMAGED),
    (_rx(r"\bnon\s+marciante\b"), PriceType.PARTS_OR_DAMAGED),
    (_rx(r"\bfor\s+parts\b|\bspares\s+or\s+repair\b"), PriceType.PARTS_OR_DAMAGED),
    (_rx(r"\bpreis\s+auf\s+anfrage\b"), PriceType.PRICE_ON_REQUEST),
    (_rx(r"\bprezzo\s+su\s+richiesta\b"), PriceType.PRICE_ON_REQUEST),
    (_rx(r"\bprice\s+on\s+request\b"), PriceType.PRICE_ON_REQUEST),
    (_rx(r"\btrattativa\s+riservata\b"), PriceType.PRICE_ON_REQUEST),
)
# Generic "on request" wording only means price-on-request when no amount is present
# (e.g. "Leasing auf Anfrage" next to a real price must not hide the price).
_GENERIC_ON_REQUEST = _rx(r"(?<!leasing )(?<!finanzierung )\b(?:auf\s+anfrage|su\s+richiesta|on\s+request)\b")

# Precedence: parts/damaged describes the *vehicle* and disqualifies it whatever the number means,
# so it wins; after that the most specific statement about *what the number is* wins.
_PRICE_TYPE_PRECEDENCE: tuple[PriceType, ...] = (
    PriceType.PARTS_OR_DAMAGED,
    PriceType.INSTALMENT,
    PriceType.LEASING,
    PriceType.DEPOSIT,
    PriceType.AUCTION_CURRENT_BID,
    PriceType.AUCTION_START,
    PriceType.PRICE_ON_REQUEST,
    PriceType.EXPORT_NET,
)

# "MWST" (Swiss spelling) is matched case-insensitively as "mwst"; "TVA" is the Swiss-French form.
_VAT_WORD = r"(?:mwst|ust|mehrwertsteuer|iva|vat|tva)"
_RATE_NUMBER = r"\d{1,2}(?:[.,]\d{1,2})?"
_RATE = rf"(?:{_RATE_NUMBER}\s*%\s*)?"
# "zzgl. gesetzl. MwSt." / "inkl. gesetzlicher MwSt." / "inkl. ges. MwSt." are the usual DE forms.
_LEGAL = r"(?:(?:gesetzl(?:\.|iche[rn]?)?|ges\.)\s*)?"
_NET_MARKERS: tuple[re.Pattern[str], ...] = (
    _rx(r"\bnetto\b"),
    _rx(r"\bnet\b"),
    _rx(rf"\bzzgl\.?\s*{_RATE}{_LEGAL}{_RATE}{_VAT_WORD}\b\.?"),
    _rx(rf"\bexkl\.?\s*{_RATE}{_LEGAL}{_RATE}{_VAT_WORD}\b\.?"),
    _rx(rf"\bexcl\.?\s*{_RATE}{_VAT_WORD}\b\.?"),
    _rx(rf"\bohne\s+{_VAT_WORD}\b\.?"),
    _rx(r"\+\s*iva\b"),
    _rx(r"\+\s*vat\b"),  # no leading \b: '+' follows a space, which is not a word boundary
    _rx(r"\biva\s+esclusa\b|\besclusa\s+iva\b|\boltre\s+iva\b"),
    _rx(r"\bplus\s+vat\b"),
    _rx(r"\bhors\s+tva\b|\btva\s+en\s+sus\b|\+\s*tva\b"),
)
_VAT_SHOWN_MARKERS: tuple[re.Pattern[str], ...] = (
    _rx(r"\b(?:mwst|ust|mehrwertsteuer)\.?\s*ausweisbar\b"),
    _rx(rf"\biva\s+{_RATE}(?:esposta|deducibile|detraibile)\b"),
    _rx(r"\bvat\s+(?:deductible|qualifying|reclaimable)\b"),
)
_GROSS_MARKERS: tuple[re.Pattern[str], ...] = (
    _rx(rf"\binkl\.?\s*{_RATE}{_LEGAL}{_RATE}{_VAT_WORD}\b\.?"),
    _rx(rf"\bincl\.?\s*{_RATE}{_VAT_WORD}\b\.?"),
    _rx(rf"\biva\s+{_RATE}(?:inclusa|compresa)\b"),
    _rx(rf"\btva\s+{_RATE}(?:incluse|comprise)\b|\bttc\b"),
    _rx(r"\bbrutto\b"),
    _rx(r"\bgross\b"),
)
_MARGIN_MARKERS: tuple[re.Pattern[str], ...] = (
    _rx(r"\bdifferenzbesteuer\w*"),
    _rx(r"\bdifferenzbest\."),
    _rx(r"§\s*25\s*a\b"),
    _rx(r"\bregime\s+del\s+margine\b"),
    _rx(r"\bmargin\s+scheme\b"),
)
_VAT_NOT_SHOWN = _rx(r"\b(?:mwst|ust|mehrwertsteuer)\.?\s*nicht\s+ausweisbar\b|\biva\s+non\s+esposta\b")
_PRIVATE_MARKERS: tuple[re.Pattern[str], ...] = (
    _rx(r"\bprivatverkauf\b"),
    _rx(r"\bvon\s+privat\b"),
    _rx(r"\bda\s+privato\b"),
    _rx(r"\bprivate\s+sale\b"),
)
_NEGOTIABLE_NO: tuple[re.Pattern[str], ...] = (
    _rx(r"\bfestpreis\b"),
    _rx(r"\bnicht\s+verhandelbar\b"),
    _rx(r"\bnon\s+trattabil[ei]\b"),
    _rx(r"\bprezzo\s+fisso\b"),
    _rx(r"\bfixed\s+price\b"),
    _rx(r"\bnon[-\s]negotiable\b|\bnot\s+negotiable\b"),
    _rx(r"\bprix\s+ferme\b|\bnon\s+n[ée]gociable\b"),
)
_NEGOTIABLE_YES: tuple[re.Pattern[str], ...] = (
    _rx(r"\bVB\b|\bVHB\b|\bONO\b", 0),  # case-sensitive abbreviations
    _rx(r"\bverhandlungsbasis\b"),
    _rx(r"\bverhandelbar\b"),
    _rx(r"\btrattabil[ei]\b"),
    _rx(r"\bnegotiable\b"),
    _rx(r"\bn[ée]gociable\b|(?<![^\W\d_])[àa]\s+(?:discuter|d[ée]battre)\b"),
)
# A percentage is a *VAT* rate only when it is attached to VAT wording: "19% MwSt.", "22 % IVA",
# "MwSt. 19%", "IVA 22% inclusa", "MwSt. ausweisbar 19%". A "0% Finanzierung" or "3% Zinsen" next
# to an unrelated "inkl. MwSt." is never read as the VAT rate.
_VAT_RATE_BEFORE_WORD = _rx(rf"(?<![0-9.,])({_RATE_NUMBER})\s*%\s*{_LEGAL}{_VAT_WORD}\b")
_VAT_RATE_AFTER_WORD = _rx(
    rf"\b{_VAT_WORD}\b\.?\s*(?:ausweisbar\s*|inclusa\s*|esposta\s*)?[(:]?\s*({_RATE_NUMBER})\s*%"
)


class PriceParse(BaseModel):
    """Parsed advertised price. Classification and VAT wording are seller statements only."""

    model_config = _FROZEN

    amount: Decimal | None = None
    currency: str | None = None
    price_type: PriceType = PriceType.UNKNOWN
    basis: PriceBasis = PriceBasis.UNKNOWN
    vat_treatment: VatTreatment = VatTreatment.UNKNOWN
    vat_rate_stated: Decimal | None = None
    # Seller wording such as "MwSt. ausweisbar" -> YES. Never a confirmed entitlement (spec 17).
    vat_reclaimable: Tristate = Tristate.UNKNOWN
    negotiable: Tristate = Tristate.UNKNOWN
    raw_text: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)
    warnings: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()

    @property
    def amount_minor(self) -> int | None:
        money = self.money()
        return None if money is None else money.to_minor()

    def money(self) -> Money | None:
        if self.amount is None or self.currency is None:
            return None
        return Money(amount=self.amount, currency=self.currency)


def _mask(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[str, bool]:
    found = False
    for pattern in patterns:
        if pattern.search(text):
            found = True
            text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text, found


def _any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(text) for p in patterns)


_GLUED_WORD_AFTER = re.compile(r"[^\W\d_]+")
_GLUED_WORD_BEFORE = re.compile(r"[^\W\d_]+$")
_EMISSION_CLASS_BEFORE = re.compile(r"(?<![^\W\d_])euro$", re.IGNORECASE)
_CURRENCY_WORDS = frozenset({"EUR", "EURO", "CHF", "FR", "SFR", "MKD", "GBP", "USD", "US"})


def _is_currency_word(word: str) -> bool:
    return word.upper() in _CURRENCY_WORDS or word.lower().startswith("ден")


def _amount_tokens(text: str) -> list[_Token]:
    """Number tokens that can be the price: excludes percentages and statute references (§25a)."""
    result: list[_Token] = []
    for token in _tokens(text):
        after = text[token.end :].lstrip(" \u00a0\u202f")
        before = text[: token.start].rstrip(" \u00a0\u202f")
        if after.startswith("%") or before.endswith("§"):
            continue
        if _EMISSION_CLASS_BEFORE.search(before) and len(token.text) == 1:
            continue  # "Euro 5" emissions class, not an amount
        glued_after = _GLUED_WORD_AFTER.match(text, token.end)
        glued_before = _GLUED_WORD_BEFORE.search(text, 0, token.start)
        if (glued_after and not _is_currency_word(glued_after.group(0))) or (
            glued_before and not _is_currency_word(glued_before.group(0))
        ):
            # e.g. "25a", "4x4", "X5": glued to letters, so not an amount.
            continue
        result.append(token)
    return result


def parse_price(text: str | None, locale: Locale | None, default_currency: str | None = None) -> PriceParse:
    """Parse an advertised price string into amount, currency, type, basis and VAT wording.

    Rules (spec 17): instalment/leasing/deposit/auction/export-net/parts and on-request wording
    classify the number so it is never mistaken for a full-vehicle payable price. Gross/net/VAT
    wording is recorded exactly as stated; "MwSt. ausweisbar" records a *claimed* reclaimable VAT
    (``vat_reclaimable=YES``), never an entitlement. Several different amounts or currencies yield
    ``None`` with a warning rather than a guess; ``'0 €'`` is a placeholder (``None`` +
    ``PRICE_ZERO_PLACEHOLDER``), never a price of zero. Negated damage wording ("keine Defekte",
    "ohne Motorschaden") is not a parts/damaged classification, and parts/damaged wording wins over
    every other price type because it disqualifies the vehicle itself. A VAT rate is recorded only
    when the percentage is attached to VAT wording. ``default_currency`` is used only when the text
    carries no currency marker, and that is flagged ``CURRENCY_DEFAULTED``.
    """
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return PriceParse(raw_text=_bounded_raw(text), warnings=(problem or ParseWarning.EMPTY_INPUT,))
    cleaned = _clean(text)
    warnings: list[str] = []
    markers: list[str] = []

    # Currency.
    currencies = {code for pattern, code in _CURRENCY_MARKERS if pattern.search(cleaned)}
    currency: str | None
    if len(currencies) > 1:
        currency = None
        warnings.append(ParseWarning.CONFLICTING_CURRENCIES)
    elif currencies:
        currency = next(iter(currencies))
    elif default_currency is not None:
        exponent(default_currency)
        currency = default_currency
        warnings.append(ParseWarning.CURRENCY_DEFAULTED)
    else:
        currency = None

    # Amount (exclude VAT percentages and §25a before tokenising).
    amount: Decimal | None = None
    candidates = _amount_tokens(cleaned)
    values: list[Decimal] = []
    for token in candidates:
        parsed = _interpret_token(token.text, locale)
        if parsed.value is None:
            warnings.extend(parsed.warnings)
        else:
            values.append(parsed.value)
    zero_placeholder = False
    if candidates and len(values) == len(candidates):
        if len(set(values)) == 1:
            amount = values[0]
        else:
            warnings.append(ParseWarning.MULTIPLE_AMOUNTS)
    if amount is not None and amount == 0:
        # "0 €" is a site placeholder for a missing/on-request price; unknown is never 0 (spec 2).
        warnings.append(ParseWarning.PRICE_ZERO_PLACEHOLDER)
        amount = None
        zero_placeholder = True
    if (
        amount is not None
        and currency is not None
        and amount != amount.quantize(Decimal(1).scaleb(-exponent(currency)), context=_CTX)
    ):
        warnings.append(ParseWarning.SUB_MINOR_PRECISION)
        amount = None
    if amount is not None and currency is None and ParseWarning.CONFLICTING_CURRENCIES not in warnings:
        warnings.append(ParseWarning.CURRENCY_MISSING)

    # Price type.
    found_types = {ptype for pattern, ptype in _PRICE_TYPE_MARKERS if pattern.search(cleaned)}
    if amount is None and (not candidates or zero_placeholder) and _GENERIC_ON_REQUEST.search(cleaned):
        found_types.add(PriceType.PRICE_ON_REQUEST)
    ordered = [t for t in _PRICE_TYPE_PRECEDENCE if t in found_types]
    markers.extend(t.value for t in ordered)
    if len(ordered) > 1:
        warnings.append(ParseWarning.MULTIPLE_PRICE_TYPES)
    if ordered:
        price_type = ordered[0]
    elif amount is not None:
        price_type = PriceType.FULL_VEHICLE_ASKING
    else:
        price_type = PriceType.UNKNOWN
    if amount is None and price_type != PriceType.PRICE_ON_REQUEST and not candidates:
        warnings.append(ParseWarning.PRICE_MISSING)

    # VAT wording. "nicht ausweisbar" is masked before looking for "ausweisbar".
    masked, vat_not_shown = _mask(cleaned, (_VAT_NOT_SHOWN,))
    vat_shown = _any(masked, _VAT_SHOWN_MARKERS)
    margin = _any(masked, _MARGIN_MARKERS)
    private = _any(masked, _PRIVATE_MARKERS)
    treatments = [
        t
        for t, present in (
            (VatTreatment.VAT_SHOWN, vat_shown),
            (VatTreatment.MARGIN_SCHEME, margin),
            (VatTreatment.PRIVATE_SALE, private),
        )
        if present
    ]
    markers.extend(t.value for t in treatments)
    if len(treatments) > 1:
        vat_treatment = VatTreatment.UNKNOWN
        warnings.append(ParseWarning.CONFLICTING_VAT_WORDING)
    elif treatments:
        vat_treatment = treatments[0]
    else:
        vat_treatment = VatTreatment.NOT_STATED

    if vat_treatment == VatTreatment.VAT_SHOWN and not vat_not_shown:
        vat_reclaimable = Tristate.YES
    elif (vat_treatment == VatTreatment.MARGIN_SCHEME or vat_not_shown) and not vat_shown:
        vat_reclaimable = Tristate.NO
    else:
        vat_reclaimable = Tristate.UNKNOWN

    # Basis.
    net = _any(masked, _NET_MARKERS) or price_type == PriceType.EXPORT_NET
    gross = _any(masked, _GROSS_MARKERS) or vat_shown
    if net and gross:
        basis = PriceBasis.UNKNOWN
        warnings.append(ParseWarning.CONFLICTING_PRICE_BASIS)
    elif net:
        basis = PriceBasis.NET
    elif gross:
        basis = PriceBasis.GROSS
    else:
        basis = PriceBasis.UNKNOWN
    if basis != PriceBasis.UNKNOWN:
        markers.append(f"basis:{basis.value}")

    # Stated VAT rate: only a percentage attached to VAT wording (see _VAT_RATE_* patterns).
    vat_rate: Decimal | None = None
    rates = {
        Decimal(m.group(1).replace(",", "."))
        for pattern in (_VAT_RATE_BEFORE_WORD, _VAT_RATE_AFTER_WORD)
        for m in pattern.finditer(cleaned)
    }
    if len(rates) == 1:
        vat_rate = next(iter(rates))
    elif len(rates) > 1:
        warnings.append(ParseWarning.MULTIPLE_VAT_RATES)

    # Negotiability: explicit "not negotiable" wording is masked before looking for "VB".
    neg_masked, negotiable_no = _mask(cleaned, _NEGOTIABLE_NO)
    negotiable_yes = _any(neg_masked, _NEGOTIABLE_YES)
    if negotiable_no and negotiable_yes:
        negotiable = Tristate.UNKNOWN
        warnings.append(ParseWarning.CONFLICTING_NEGOTIABILITY)
    elif negotiable_no:
        negotiable = Tristate.NO
    elif negotiable_yes:
        negotiable = Tristate.YES
    else:
        negotiable = Tristate.UNKNOWN

    return PriceParse(
        amount=amount,
        currency=currency,
        price_type=price_type,
        basis=basis,
        vat_treatment=vat_treatment,
        vat_rate_stated=vat_rate,
        vat_reclaimable=vat_reclaimable,
        negotiable=negotiable,
        raw_text=text,
        warnings=tuple(dict.fromkeys(warnings)),
        markers=tuple(dict.fromkeys(markers)),
    )


# ---------------------------------------------------------------------------------------------
# Mileage (spec 3, 7)
# ---------------------------------------------------------------------------------------------

_MILEAGE_UNKNOWN_WORDS = _rx(
    r"\b(?:unbekannt|nicht\s+bekannt|k\.\s*a\.|n\.\s*d\.|n/a|sconosciut\w*|non\s+disponibile|unknown)"
    r"|\bнепознат\w*"  # noqa: RUF001 (Macedonian Cyrillic wording)
)
_ESTIMATE_WORDS = _rx(
    r"\bca\b\.?|\bcirca\b|\bcca\b\.?|\bapprox\w*|\bungef(?:ä|ae)hr\b|\betwa\b|\bzirka\b"
    r"|\bapprossimativ\w*|\babout\b|~|\bоколу\b|\bприближно\b"  # noqa: RUF001 (Macedonian Cyrillic wording)
)
_LOWER_BOUND_WORDS = _rx(
    r"(?:\büber\b|\bueber\b|\bmehr\s+als\b|\boltre\b|\bpiù\s+di\b|\bover\b|\bmore\s+than\b|>)"
)
_UPPER_BOUND_WORDS = _rx(
    r"(?:\bunter\b|\bweniger\s+als\b|\bmeno\s+di\b|\bunder\b|\bless\s+than\b|<|\bmax\b\.?|\bfino\s+a\b|\bbis\s+zu\b)"
)
_RANGE_JOINER = _rx(r"^\s*(?:[-\u2013\u2014]|bis|to|a|до)\s*$")
_THOUSAND_KM = _rx(
    rf"(?:{_NL}tkm{_NR}|{_NL}t\s*km{_NR}|{_NL}tsd\.?\s*km{_NR}|{_NL}tausend\s*km{_NR}|{_NL}mila\s*km{_NR})"
)
_KM_UNIT = _rx(rf"{_NL}(?:km|kms|kilometer[n]?|kilometri|chilometri|км){_NR}(?!\s*/\s*h\b)\.?")
_MILES_UNIT = _rx(rf"[0-9]\s*mi{_NR}\.?|{_NL}(?:miles?|meilen|miglia|mls){_NR}")


class MileageParse(BaseModel):
    """Parsed odometer statement. ``km`` is unrounded (miles * 1.609344 exactly) or None."""

    model_config = _FROZEN

    km: Decimal | None = None
    original: MileageOriginal = MileageOriginal()
    claim: OdometerClaim = OdometerClaim.UNKNOWN
    ambiguous: bool = False
    warnings: tuple[str, ...] = ()


def parse_mileage(
    text: str | None,
    locale: Locale | None,
    *,
    default_unit: Literal["km", "mi"] | None = None,
) -> MileageParse:
    """Parse a mileage statement.

    - ``'199.999 km'`` (de) -> 199999; ``'124.274 mi'`` -> 124274 * 1.609344 km, unrounded.
    - ``'ca. 150.000 km'`` -> km kept, ``is_estimate=True``, claim ``ESTIMATED``.
    - ``'150.000 - 160.000 km'`` / ``'150-160 Tkm'`` / ``'unter 150.000 km'`` -> ``km=None``,
      claim ``RANGE_ONLY`` with the bounds in ``original`` (spec 3: an uncertain range never passes).
    - ``'unbekannt'``/``'n.d.'`` -> ``km=None``, claim ``UNKNOWN``.
    - ``'0 km'`` on a used vehicle is treated as a placeholder: ``None`` + warning (unknown never 0).
    - A value above 2,000,000 km (e.g. the typo ``'150.000 Tkm'``) is implausible: ``km=None`` +
      ``MILEAGE_IMPLAUSIBLE``; the stated figure stays visible in ``original.amount``.
    - A negative figure (``'-150.000 km'``) -> ``None`` + ``MILEAGE_NEGATIVE``.
    - ``km/h`` is a speed, not a mileage unit.
    - No unit and no ``default_unit`` -> ``None`` + ``MILEAGE_UNIT_MISSING``.
    """
    problem = _too_long_or_empty(text)
    raw = _bounded_raw(text, 200)
    if problem is not None or text is None:
        return MileageParse(
            original=MileageOriginal(text=raw), warnings=(problem or ParseWarning.EMPTY_INPUT,)
        )
    cleaned = _clean(text)
    toks = _tokens(cleaned)
    warnings: list[str] = []

    if not toks:
        if _MILEAGE_UNKNOWN_WORDS.search(cleaned):
            warnings.append(ParseWarning.MILEAGE_UNKNOWN)
        else:
            warnings.append(ParseWarning.NO_NUMBER)
        return MileageParse(original=MileageOriginal(text=raw), warnings=tuple(warnings))

    thousand = bool(_THOUSAND_KM.search(cleaned))
    has_km = thousand or bool(_KM_UNIT.search(cleaned))
    has_mi = bool(_MILES_UNIT.search(cleaned))
    unit: Literal["km", "mi"] | None
    if has_km and has_mi:
        return MileageParse(
            original=MileageOriginal(text=raw), ambiguous=True, warnings=(ParseWarning.CONFLICTING_UNITS,)
        )
    if has_km:
        unit = "km"
    elif has_mi:
        unit = "mi"
    elif default_unit is not None:
        unit = default_unit
        warnings.append(ParseWarning.MILEAGE_UNIT_DEFAULTED)
    else:
        unit = None
    multiplier = Decimal(1000) if thousand else Decimal(1)
    estimate = bool(_ESTIMATE_WORDS.search(cleaned))

    def to_km(amount: Decimal) -> Decimal:
        return _CTX.multiply(amount, MILES_TO_KM) if unit == "mi" else amount

    if _is_negative(cleaned, toks[0]):
        return MileageParse(
            original=MileageOriginal(text=raw, unit=unit or "unknown", is_estimate=estimate),
            warnings=tuple([*warnings, ParseWarning.MILEAGE_NEGATIVE]),
        )
    values: list[Decimal] = []
    for token in toks:
        parsed = _interpret_token(token.text, locale)
        if parsed.value is None:
            return MileageParse(
                original=MileageOriginal(text=raw, unit=unit or "unknown", is_estimate=estimate),
                ambiguous=parsed.ambiguous,
                warnings=tuple([*warnings, *parsed.warnings]),
            )
        values.append(_CTX.multiply(parsed.value, multiplier))

    if len(toks) == 2 and _RANGE_JOINER.match(cleaned[toks[0].end : toks[1].start]):
        low, high = sorted(values)
        warnings.append(ParseWarning.MILEAGE_RANGE_ONLY)
        if unit is None:
            warnings.append(ParseWarning.MILEAGE_UNIT_MISSING)
        return MileageParse(
            original=MileageOriginal(
                text=raw, unit=unit or "unknown", is_estimate=estimate, range_low=low, range_high=high
            ),
            claim=OdometerClaim.RANGE_ONLY,
            warnings=tuple(warnings),
        )
    if len(toks) > 1:
        return MileageParse(
            original=MileageOriginal(text=raw, unit=unit or "unknown", is_estimate=estimate),
            ambiguous=True,
            warnings=tuple([*warnings, ParseWarning.MULTIPLE_NUMBERS]),
        )

    amount = values[0]
    before = cleaned[: toks[0].start]
    lower = bool(_LOWER_BOUND_WORDS.search(before))
    upper = bool(_UPPER_BOUND_WORDS.search(before))
    if lower or upper:
        warnings.append(ParseWarning.MILEAGE_BOUND_ONLY)
        if unit is None:
            warnings.append(ParseWarning.MILEAGE_UNIT_MISSING)
        return MileageParse(
            original=MileageOriginal(
                text=raw,
                unit=unit or "unknown",
                is_estimate=estimate,
                range_low=amount if lower and not upper else None,
                range_high=amount if upper and not lower else None,
            ),
            claim=OdometerClaim.RANGE_ONLY,
            warnings=tuple(warnings),
        )
    if unit is None:
        warnings.append(ParseWarning.MILEAGE_UNIT_MISSING)
        return MileageParse(original=MileageOriginal(text=raw, amount=amount), warnings=tuple(warnings))
    if amount == 0:
        warnings.append(ParseWarning.MILEAGE_ZERO_TREATED_UNKNOWN)
        return MileageParse(
            original=MileageOriginal(text=raw, amount=amount, unit=unit), warnings=tuple(warnings)
        )

    km = to_km(amount)
    if unit == "mi":
        warnings.append(ParseWarning.MILES_CONVERTED)
    if km > MAX_PLAUSIBLE_MILEAGE_KM:
        # Almost certainly a typo or unit slip ("150.000 Tkm"); acting on it would be a guess.
        warnings.append(ParseWarning.MILEAGE_IMPLAUSIBLE)
        return MileageParse(
            original=MileageOriginal(amount=amount, unit=unit, text=raw, is_estimate=estimate),
            warnings=tuple(warnings),
        )
    if estimate:
        warnings.append(ParseWarning.MILEAGE_ESTIMATE)
    return MileageParse(
        km=km,
        original=MileageOriginal(amount=amount, unit=unit, text=raw, is_estimate=estimate),
        claim=OdometerClaim.ESTIMATED if estimate else OdometerClaim.SELLER_REPORTED,
        warnings=tuple(warnings),
    )


def miles_to_km(miles: Decimal) -> Decimal:
    """Exact miles -> km (1.609344), unrounded."""
    return _CTX.multiply(miles, MILES_TO_KM)


# ---------------------------------------------------------------------------------------------
# Power and displacement (spec 7: canonical kW and cm3)
# ---------------------------------------------------------------------------------------------

_POWER = _rx(r"(?<![0-9.,])([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(kw|ps|cv|hp|bhp|к\.?\s?с\.?)(?![^\W\d_])")  # noqa: RUF001 (Macedonian Cyrillic wording)


class PowerParse(BaseModel):
    """Power in kW. ``kw`` is the stated kW (rounded half-up only if stated with decimals) or,
    when no kW is stated, derived from PS/CV (x 0.73549875) or hp (x 0.745699872), half-up."""

    model_config = _FROZEN

    kw: int | None = None
    kw_stated: Decimal | None = None
    ps_stated: Decimal | None = None
    hp_stated: Decimal | None = None
    kw_derived: bool = False
    conflict: bool = False
    warnings: tuple[str, ...] = ()


def _half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_power(text: str | None) -> PowerParse:
    """Parse ``'103 kW (140 PS)'``, ``'140 PS'``, ``'140 CV'``, ``'140 hp'``, ``'103kW'``.

    If kW and PS/CV are both stated and disagree by more than 2 kW, ``conflict=True`` and the
    stated kW is kept (it is never silently replaced by the derived value).
    """
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return PowerParse(warnings=(problem or ParseWarning.EMPTY_INPUT,))
    found: dict[str, set[Decimal]] = {"kw": set(), "ps": set(), "hp": set()}
    for match in _POWER.finditer(_clean(text)):
        value = Decimal(match.group(1).replace(",", "."))
        unit = match.group(2).lower()
        key = "kw" if unit == "kw" else "hp" if unit in ("hp", "bhp") else "ps"
        found[key].add(value)
    warnings: list[str] = []
    if any(len(v) > 1 for v in found.values()):
        return PowerParse(warnings=(ParseWarning.MULTIPLE_POWER_VALUES,))
    kw_stated = next(iter(found["kw"]), None)
    ps = next(iter(found["ps"]), None)
    hp = next(iter(found["hp"]), None)
    if kw_stated is None and ps is None and hp is None:
        return PowerParse(warnings=(ParseWarning.POWER_MISSING,))

    derived_exact: Decimal | None = None
    if ps is not None:
        derived_exact = _CTX.multiply(ps, PS_TO_KW)
    elif hp is not None:
        derived_exact = _CTX.multiply(hp, HP_TO_KW)

    conflict = False
    kw_derived = False
    if kw_stated is not None:
        kw = _half_up(kw_stated)
        if kw_stated != kw:
            warnings.append(ParseWarning.POWER_ROUNDED)
        if derived_exact is not None and abs(kw_stated - derived_exact) > POWER_CONFLICT_TOLERANCE_KW:
            conflict = True
            warnings.append(ParseWarning.POWER_CONFLICT)
    else:
        assert derived_exact is not None
        kw = _half_up(derived_exact)
        kw_derived = True
        warnings.append(ParseWarning.POWER_DERIVED)
    if not 1 <= kw <= 2000:
        return PowerParse(
            kw_stated=kw_stated, ps_stated=ps, hp_stated=hp, warnings=(ParseWarning.POWER_IMPLAUSIBLE,)
        )
    return PowerParse(
        kw=kw,
        kw_stated=kw_stated,
        ps_stated=ps,
        hp_stated=hp,
        kw_derived=kw_derived,
        conflict=conflict,
        warnings=tuple(warnings),
    )


_CM3 = _rx(
    r"(?<![0-9.,'\u2019])([0-9]{1,2}[.,'\u2019 \u00a0\u202f][0-9]{3}|[0-9]{2,5})\s*"
    rf"(?:cm\s*(?:³|3|\^3)|ccm|cc|cmc|см\s*(?:³|3)|куб\.?\s*см){_NR}"
)
_LITRES = _rx(
    r"(?<![0-9.,])([0-9](?:[.,][0-9]{1,2})?)\s*(?:l|lt|liter|litre|litri|litro|литри?)"
    rf"{_NR}\.?(?!\s*/\s*100)"
)
_BARE_DECIMAL = _rx(r"(?<![0-9])[0-9][.,][0-9](?![0-9])")


class DisplacementParse(BaseModel):
    """Engine displacement in cm3. Litre statements are nominal and flagged as such."""

    model_config = _FROZEN

    cm3: int | None = None
    litres_stated: Decimal | None = None
    from_litres: bool = False
    warnings: tuple[str, ...] = ()


def parse_displacement(text: str | None) -> DisplacementParse:
    """Parse ``'1.995 cm³'``, ``'1995 ccm'``, ``'1995 cc'`` or ``'2.0 l'`` -- only with a unit.

    A model designation such as ``'2.0 TDI'`` is never read as a displacement (``None`` +
    ``DISPLACEMENT_UNIT_MISSING``). For cm3 a three-digit group after ``.``/``,``/``'`` is always a
    thousands group (sub-cm3 precision is never advertised).
    """
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return DisplacementParse(warnings=(problem or ParseWarning.EMPTY_INPUT,))
    cleaned = _clean(text)
    cm3_values = {int(re.sub(r"[^0-9]", "", m.group(1))) for m in _CM3.finditer(cleaned)}
    if len(cm3_values) > 1:
        return DisplacementParse(warnings=(ParseWarning.MULTIPLE_DISPLACEMENTS,))
    if cm3_values:
        cm3 = next(iter(cm3_values))
        if not 50 <= cm3 <= 10000:
            return DisplacementParse(warnings=(ParseWarning.DISPLACEMENT_IMPLAUSIBLE,))
        return DisplacementParse(cm3=cm3)
    litre_values = {Decimal(m.group(1).replace(",", ".")) for m in _LITRES.finditer(cleaned)}
    if len(litre_values) > 1:
        return DisplacementParse(warnings=(ParseWarning.MULTIPLE_DISPLACEMENTS,))
    if litre_values:
        litres = next(iter(litre_values))
        cm3 = _half_up(_CTX.multiply(litres, Decimal(1000)))
        if not 50 <= cm3 <= 10000:
            return DisplacementParse(litres_stated=litres, warnings=(ParseWarning.DISPLACEMENT_IMPLAUSIBLE,))
        return DisplacementParse(
            cm3=cm3, litres_stated=litres, from_litres=True, warnings=(ParseWarning.DISPLACEMENT_FROM_LITRES,)
        )
    if _BARE_DECIMAL.search(cleaned) or _tokens(cleaned):
        return DisplacementParse(warnings=(ParseWarning.DISPLACEMENT_UNIT_MISSING,))
    return DisplacementParse(warnings=(ParseWarning.NO_NUMBER,))


# ---------------------------------------------------------------------------------------------
# First registration (spec 7: never invent a day)
# ---------------------------------------------------------------------------------------------

_MONTH_NAMES: dict[str, int] = {}
for _month, _names in enumerate(
    (
        ("januar", "jänner", "jaenner", "january", "gennaio", "jan", "jän", "gen"),
        ("februar", "february", "febbraio", "feb", "febr"),
        ("märz", "maerz", "march", "marzo", "mär", "mrz", "mar"),
        ("april", "aprile", "apr"),
        ("mai", "may", "maggio", "mag"),
        ("juni", "june", "giugno", "jun", "giu"),
        ("juli", "july", "luglio", "jul", "lug"),
        ("august", "agosto", "aug", "ago"),
        ("september", "settembre", "sep", "sept", "set"),
        ("oktober", "october", "ottobre", "okt", "oct", "ott"),
        ("november", "novembre", "nov"),
        ("dezember", "december", "dicembre", "dez", "dec", "dic"),
    ),
    start=1,
):
    for _name in _names:
        _MONTH_NAMES[_name] = _month

_DATE_PATTERN = re.compile(
    r"(?<![0-9])(?:"
    r"(?P<d1>[0-9]{1,2})[./-](?P<m1>[0-9]{1,2})[./-](?P<y1>[0-9]{4})"
    r"|(?P<y2>[0-9]{4})-(?P<m2>[0-9]{1,2})-(?P<d2>[0-9]{1,2})"
    r"|(?P<y3>[0-9]{4})[-/](?P<m3>[0-9]{1,2})"
    r"|(?P<m4>[0-9]{1,2})\s*[./-]\s*(?P<y4>[0-9]{4})"
    r"|(?P<m5>[0-9]{1,2})[./](?P<y5>[0-9]{2})"
    r"|(?P<y6>[0-9]{4})"
    r")(?![0-9])"
)
_MONTH_NAME_PATTERN = re.compile(
    r"(?<![^\W\d_])(?P<name>[^\W\d_]{3,9})\.?\s+(?P<year>[0-9]{4})(?![0-9])", re.IGNORECASE
)


class FirstRegistrationParse(BaseModel):
    model_config = _FROZEN

    value: PartialDate = PartialDate()
    warnings: tuple[str, ...] = ()
    raw: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)


def parse_first_registration(text: str | None, as_of: date | None = None) -> FirstRegistrationParse:
    """Parse a first-registration statement into a ``PartialDate`` at its stated precision.

    ``'05/2011'``, ``'5/2011'``, ``'2011-05'``, ``'EZ 05/2011'``, ``'Erstzulassung 05/2011'``,
    ``'Immatricolazione 05/2011'``, ``'05.2011'``, ``'Mai 2011'`` -> ``2011-05`` (month precision);
    ``'2011'`` -> year precision; a full ``'14.05.2011'`` keeps its day. A two-digit year, an
    impossible month, a year before 1950, several different dates, or a date later than ``as_of``
    yields ``None`` with a warning.
    """
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return FirstRegistrationParse(warnings=(problem or ParseWarning.EMPTY_INPUT,), raw=_bounded_raw(text))
    cleaned = _clean(text)
    found: list[tuple[int, int | None, int | None]] = []
    warnings: list[str] = []
    for match in _DATE_PATTERN.finditer(cleaned):
        g = match.groupdict()
        if g["y5"] is not None:
            warnings.append(ParseWarning.TWO_DIGIT_YEAR)
            continue
        year = int(g["y1"] or g["y2"] or g["y3"] or g["y4"] or g["y6"])
        month_s = g["m1"] or g["m2"] or g["m3"] or g["m4"]
        day_s = g["d1"] or g["d2"]
        found.append((year, int(month_s) if month_s else None, int(day_s) if day_s else None))
    for match in _MONTH_NAME_PATTERN.finditer(cleaned):
        month = _MONTH_NAMES.get(match.group("name").lower())
        if month is not None:
            year = int(match.group("year"))
            # The bare year is also matched by _DATE_PATTERN; replace that coarser reading.
            found = [f for f in found if f != (year, None, None)]
            found.append((year, month, None))
    distinct = sorted(set(found), key=lambda f: (f[0], f[1] or 0, f[2] or 0))
    if not distinct:
        return FirstRegistrationParse(
            warnings=tuple(dict.fromkeys(warnings or [ParseWarning.DATE_UNPARSEABLE])), raw=text
        )
    if len(distinct) > 1:
        return FirstRegistrationParse(warnings=(ParseWarning.MULTIPLE_DATES,), raw=text)
    year, month, day = distinct[0]
    if month is not None and not 1 <= month <= 12:
        return FirstRegistrationParse(warnings=(ParseWarning.INVALID_DATE,), raw=text)
    if day is not None:
        assert month is not None
        try:
            date(year, month, day)
        except ValueError:
            return FirstRegistrationParse(warnings=(ParseWarning.INVALID_DATE,), raw=text)
    if year < MIN_PLAUSIBLE_YEAR:
        return FirstRegistrationParse(warnings=(ParseWarning.IMPLAUSIBLE_YEAR,), raw=text)
    if as_of is not None and (year, month or 1, day or 1) > (as_of.year, as_of.month, as_of.day):
        return FirstRegistrationParse(warnings=(ParseWarning.FUTURE_DATE,), raw=text)
    if day is not None and month is not None:
        partial = PartialDate(value=f"{year:04d}-{month:02d}-{day:02d}", precision=Precision.DAY)
    elif month is not None:
        partial = PartialDate(value=f"{year:04d}-{month:02d}", precision=Precision.MONTH)
    else:
        partial = PartialDate(value=f"{year:04d}", precision=Precision.YEAR)
    return FirstRegistrationParse(value=partial, raw=text)


# ---------------------------------------------------------------------------------------------
# Source timestamps (spec 7: zone-less source dates get the documented source zone + marker)
# ---------------------------------------------------------------------------------------------

_ISO_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_PART = re.compile(r"[Tt ]\d")
_EU_DATETIME = re.compile(
    r"^(?P<d>\d{1,2})[./](?P<m>\d{1,2})[./](?P<y>\d{4})"
    r"(?:\s*,?\s*(?:um\s+|ore\s+|alle\s+)?(?P<H>\d{1,2})[:.](?P<M>\d{2})(?::(?P<S>\d{2}))?(?:\s*uhr)?)?$",
    re.IGNORECASE,
)

TimePrecision = Literal["second", "minute", "day"]


class SourceDateTimeParse(BaseModel):
    """Parsed source timestamp plus warnings.

    ``timestamp.precision`` is ``DAY`` for every parsed value (the shared ``Precision`` enum has no
    sub-day member); ``time_precision`` states the finer precision actually given by the source.
    """

    model_config = _FROZEN

    timestamp: SourceTimestamp = SourceTimestamp()
    time_precision: TimePrecision | None = None
    warnings: tuple[str, ...] = ()


def _zone(source_tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(source_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed(f"unknown source timezone {source_tz!r}") from exc


def _localize(naive: datetime, tz: ZoneInfo) -> tuple[datetime | None, list[str]]:
    """Attach ``tz`` to a wall-clock time, refusing DST gaps and flagging DST folds."""
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, []
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        # Non-existent local time (spring-forward gap): any instant would be a guess.
        return None, [ParseWarning.DST_GAP_NONEXISTENT_TIME]
    # Repeated local time (fall-back fold): keep the earlier instant and flag it.
    return first, [ParseWarning.DST_FOLD_AMBIGUOUS_TIME]


def parse_source_datetime(text: str | None, source_tz: str, as_of: datetime) -> SourceDateTimeParse:
    """Parse a source published/modified timestamp.

    - ISO 8601 with ``Z``/offset -> exact instant, ``zone_assumed=False``.
    - Zone-less (``'2026-10-06 10:00'``, ``'06.10.2026, 10:00 Uhr'``, ``'06/10/2026 10:00'``,
      day-first as used by DE/IT/CH sources) -> the documented ``source_tz`` is attached with
      ``zone_assumed=True`` and ``assumed_zone`` (never silently UTC).
    - Date-only -> start of that day in ``source_tz``, ``time_precision='day'`` and
      ``DATE_ONLY_START_OF_DAY_ASSUMED``.
    - A local time inside a DST gap -> ``value=None`` + ``DST_GAP_NONEXISTENT_TIME``; inside a DST
      fold -> earlier instant + ``DST_FOLD_AMBIGUOUS_TIME``.
    - More than one day after ``as_of`` -> ``value=None`` + ``FUTURE_TIMESTAMP``.
    """
    tz = _zone(source_tz)
    try:
        now = ensure_utc(as_of)
    except ValueError as exc:
        raise ValidationFailed("as_of must be timezone-aware") from exc
    raw = _bounded_raw(text, 100)
    problem = _too_long_or_empty(text)
    if problem is not None or text is None:
        return SourceDateTimeParse(
            timestamp=SourceTimestamp(raw=raw), warnings=(problem or ParseWarning.EMPTY_INPUT,)
        )
    stripped = _clean(text).strip()
    warnings: list[str] = []
    naive: datetime | None = None
    aware: datetime | None = None
    time_precision: TimePrecision
    try:
        if _ISO_DATE_ONLY.match(stripped):
            naive = datetime.combine(date.fromisoformat(stripped), time(0, 0))
            time_precision = "day"
        elif (eu := _EU_DATETIME.match(stripped)) is not None:
            day = date(int(eu["y"]), int(eu["m"]), int(eu["d"]))
            if eu["H"] is None:
                naive = datetime.combine(day, time(0, 0))
                time_precision = "day"
            else:
                naive = datetime.combine(day, time(int(eu["H"]), int(eu["M"]), int(eu["S"] or 0)))
                time_precision = "second" if eu["S"] else "minute"
        else:
            parsed = datetime.fromisoformat(stripped.replace(" ", "T", 1) if " " in stripped else stripped)
            if not _ISO_TIME_PART.search(stripped):
                # Other ISO date-only forms ('20261006', week dates): no time was stated.
                time_precision = "day"
            elif parsed.second or parsed.microsecond or stripped.count(":") >= 2:
                time_precision = "second"
            else:
                time_precision = "minute"
            if parsed.tzinfo is None:
                naive = parsed
            else:
                aware = parsed
    except ValueError:
        return SourceDateTimeParse(
            timestamp=SourceTimestamp(raw=raw), warnings=(ParseWarning.DATE_UNPARSEABLE,)
        )

    zone_assumed = False
    try:
        if aware is None:
            assert naive is not None
            aware, dst_warnings = _localize(naive, tz)
            warnings.extend(dst_warnings)
            zone_assumed = True
            warnings.append(ParseWarning.ZONE_ASSUMED)
            if time_precision == "day":
                warnings.append(ParseWarning.DATE_ONLY_START_OF_DAY_ASSUMED)
        value = None if aware is None else aware.astimezone(UTC)
    except OverflowError:
        return SourceDateTimeParse(timestamp=SourceTimestamp(raw=raw), warnings=(ParseWarning.INVALID_DATE,))
    if value is not None and value.year < MIN_PLAUSIBLE_YEAR:
        return SourceDateTimeParse(
            timestamp=SourceTimestamp(raw=raw), warnings=(ParseWarning.IMPLAUSIBLE_YEAR,)
        )
    if value is not None and value > now + FUTURE_TOLERANCE:
        warnings.append(ParseWarning.FUTURE_TIMESTAMP)
        value = None
    return SourceDateTimeParse(
        timestamp=SourceTimestamp(
            value=value,
            raw=raw,
            zone_assumed=zone_assumed,
            assumed_zone=source_tz if zone_assumed else None,
            precision=Precision.DAY if value is not None else Precision.UNKNOWN,
        ),
        time_precision=time_precision if value is not None else None,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------------------------
# Technical inspection and roadworthiness wording (spec 7 documentation, spec 19 due diligence)
# ---------------------------------------------------------------------------------------------

InspectionKind = Literal["hu_tuv", "mfk", "revisione", "unknown"]
_RuleKind = Literal["hu_tuv", "mfk", "revisione", "collaudo"]
_Role = Literal["expired", "not_valid", "not_fresh", "expiry", "last", "bare", "fresh"]

# ENGINEERING DEFAULTS (sanity bounds, not business rules).
MAX_INSPECTION_TEXT_LENGTH = 200_000
INSPECTION_YEARS_AHEAD = 5  # no periodic inspection is valid further ahead than this
INSPECTION_YEARS_BACK = 30
_MAX_INSPECTION_EVIDENCE = 8
_EVIDENCE_LIMIT = 120
_CONDITION_WINDOW = 40
_MASK = "\x00"  # masked spans never match again and never join neighbouring words

_FR_MONTHS: dict[str, int] = {
    "janvier": 1,
    "janv": 1,
    "février": 2,
    "fevrier": 2,
    "févr": 2,
    "fevr": 2,
    "mars": 3,
    "avril": 4,
    "avr": 4,
    "juin": 6,
    "juillet": 7,
    "juil": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
    "déc": 12,
}
_INSPECTION_MONTHS: dict[str, int] = {**_MONTH_NAMES, **_FR_MONTHS}
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _INSPECTION_MONTHS), key=len, reverse=True))
# A date right after inspection wording. Only digits and real month names: never an arbitrary word.
_IDATE = (
    r"(?P<date>"
    r"[0-9]{1,2}\.[0-9]{1,2}\.(?:[0-9]{4}|[0-9]{2})"
    r"|[0-9]{1,2}/[0-9]{1,2}/(?:[0-9]{4}|[0-9]{2})"
    r"|[0-9]{4}-[0-9]{1,2}(?:-[0-9]{1,2})?"
    r"|[0-9]{1,2} ?[./-] ?(?:[0-9]{4}|[0-9]{2})"
    rf"|(?:{_MONTH_ALT})\.? [0-9]{{4}}"
    r"|(?:19|20)[0-9]{2}"
    r")(?![0-9])"
)
_ID_DMY = re.compile(r"([0-9]{1,2})[./]([0-9]{1,2})[./]([0-9]{4}|[0-9]{2})")
_ID_ISO = re.compile(r"([0-9]{4})-([0-9]{1,2})(?:-([0-9]{1,2}))?")
_ID_MY = re.compile(r"([0-9]{1,2}) ?[./-] ?([0-9]{4}|[0-9]{2})")
_ID_NAME = re.compile(r"([^\W\d_]+)\.? ([0-9]{4})")
_ID_YEAR = re.compile(r"[0-9]{4}")

_HU = (
    r"(?:hu(?: ?(?:/|\+|&|und|u\.) ?au)?|t(?:ü|ue|u)v(?: ?(?:/|\+|&|und|u\.) ?au)?"
    r"|hauptuntersuchung)"
)
_MFK = r"(?:mfk|motorfahrzeugkontrolle)"
_EXPD = r"expertis(?:é|ée|és|ées|ee|ees)\b"  # participle only; the noun "expertise" is not a claim
_NOT_DATE_NEXT = r"(?! ?(?:im |am |ab |: ?)?[0-9])"
# Words that may sit between a negation and the inspection wording ("nicht mehr ab MFK", "non è
# ancora revisionata", "pas encore expertisée"). A closed list: "non fumatori, revisionata" is no
# negation of the inspection.
_DE_FILL = r"(?:(?:mehr|frisch|neu|direkt|ganz) ){0,2}"
_IT_FILL = r"(?:(?:ancora|è|e|la|il|stata|stato|mai|più|piu) ){0,3}"
_FR_FILL = r"(?:(?:encore|été|ete|du tout|jamais) ){0,2}"


@dataclass(frozen=True, slots=True)
class _InspectionRule:
    role: _Role
    kind: _RuleKind
    pattern: re.Pattern[str]


def _irule(role: _Role, kind: _RuleKind, pattern: str) -> _InspectionRule:
    return _InspectionRule(role, kind, re.compile(pattern, re.IGNORECASE))


# Applied in this order; every match is masked before the next rule runs, so a negated or dated
# statement ("nicht ab MFK", "ab MFK 03.2025") can never be re-read as a bare positive one.
_INSPECTION_RULES: tuple[_InspectionRule, ...] = (
    # Explicitly expired.
    _irule("expired", "hu_tuv", rf"\b{_HU} ?(?:ist )?(?:abgelaufen|[üu]berf[äa]llig|[üu]berzogen)\b"),
    _irule("expired", "hu_tuv", rf"\b{_HU} ?(?:ist )?f[äa]llig\b{_NOT_DATE_NEXT}"),
    _irule("expired", "mfk", rf"\b{_MFK} ?(?:ist )?(?:abgelaufen|[üu]berf[äa]llig)\b"),
    _irule("expired", "revisione", r"\brevisione (?:è |e )?scadut[ao]\b|\bscadut[ao] (?:la )?revisione\b"),
    _irule("expired", "collaudo", r"\bcollaudo (?:è |e )?scaduto\b"),
    _irule("expired", "mfk", r"\bexpertise (?:est )?(?:[ée]chue|expir[ée]e|p[ée]rim[ée]e|d[ée]pass[ée]e)\b"),
    # No valid inspection (DE/IT: the inspection is a hard validity date).
    _irule(
        "not_valid",
        "hu_tuv",
        rf"\b(?:ohne|kein(?:e[nr]?)?) (?:g[üu]ltige[nr]? )?{_HU}\b(?![-\u2013][^\W\d_])",
    ),
    _irule("not_valid", "revisione", rf"\b(?:senza|non) {_IT_FILL}revision(?:e|at[ao])\b"),
    _irule(
        "not_valid",
        "revisione",
        r"\bda revisionare\b|\brevisione (?:da fare|non (?:fatta|effettuata|valida))\b",
    ),
    # Not freshly inspected for the sale (CH/FR: says nothing positive about current validity).
    _irule("not_fresh", "mfk", rf"\bvor (?:der )?{_MFK}\b"),
    _irule("not_fresh", "mfk", rf"\bnicht {_DE_FILL}ab (?:der )?{_MFK}\b"),
    _irule("not_fresh", "hu_tuv", rf"\b(?:ohne|kein(?:e[nr]?)?|nicht) (?:neue[rn]?|frische[rn]?) {_HU}\b"),
    _irule("not_fresh", "mfk", rf"\b(?:ohne|keine) (?:frische )?{_MFK}\b"),
    _irule("not_fresh", "mfk", rf"\bmuss (?:noch )?(?:zur|an die|in die) {_MFK}\b"),
    _irule("not_fresh", "mfk", rf"\bnicht {_DE_FILL}vorgef[üu]hrt\b"),
    _irule("not_fresh", "mfk", rf"\b{_MFK} ?(?:ist )?f[äa]llig\b{_NOT_DATE_NEXT}"),
    _irule("not_fresh", "collaudo", rf"\b(?:senza|non) {_IT_FILL}collaud(?:o|at[ao])\b|\bda collaudare\b"),
    _irule("not_fresh", "mfk", rf"\bsans (?:l['\u2019] ?)?expertise\b|\b(?:non|pas) {_FR_FILL}{_EXPD}"),
    _irule("not_fresh", "mfk", r"\b[àa] expertiser\b|\bavant (?:l['\u2019] ?)?expertise\b"),
    # Stated expiry / next due date.
    _irule(
        "expiry",
        "hu_tuv",
        rf"\b{_HU} ?(?P<fresh>(?:neu|frisch),? )?"
        rf"(?:(?:g[üu]ltig )?bis (?:ende |zum )?|f[äa]llig (?:im |am |ab )?|: ?)?{_IDATE}",
    ),
    _irule("expiry", "mfk", rf"\b{_MFK} (?:g[üu]ltig )?bis (?:ende |zum )?{_IDATE}"),
    _irule("expiry", "mfk", rf"\bn[äa]chste {_MFK} ?(?:im |am |: ?)?{_IDATE}"),
    _irule("expiry", "mfk", rf"\b{_MFK} f[äa]llig (?:im |am |ab |: ?)?{_IDATE}"),
    _irule(
        "expiry",
        "revisione",
        r"\brevision(?:e|at[ao]) (?:valida )?"
        r"(?:fino (?:all['\u2019]|al|a) ?|(?:in )?scadenza (?:il |a |nel )?|scade (?:il |a |nel )?)"
        rf"(?:: ?)?{_IDATE}",
    ),
    _irule("expiry", "revisione", rf"\bscadenza (?:della )?revisione ?(?:: ?|il |a |nel )?{_IDATE}"),
    _irule("expiry", "revisione", rf"\bprossima revisione ?(?:: ?|il |a |nel |entro (?:il )?)?{_IDATE}"),
    _irule("expiry", "mfk", rf"\bprochaine expertise ?(?:: ?|en |le |au |pour )?{_IDATE}"),
    _irule("expiry", "mfk", rf"\bexpertise valable jusqu['\u2019] ?(?:au |à |a |en |fin )?{_IDATE}"),
    # Date of the last inspection (not an expiry).
    _irule("last", "hu_tuv", rf"\b(?:letzte|zuletzt) {_HU} ?(?:am |im |vom |: ?)?{_IDATE}"),
    _irule("last", "mfk", rf"\b(?:letzte|zuletzt) {_MFK} ?(?:im |am |vom |: ?)?{_IDATE}"),
    _irule("last", "mfk", rf"\b(?:frisch )?ab (?:der )?{_MFK} (?:vom |am |im |: ?)?{_IDATE}"),
    _irule("last", "mfk", rf"\b{_MFK} (?:vom|am|gemacht|erledigt|bestanden) (?:am |im )?{_IDATE}"),
    _irule("last", "mfk", rf"\b(?:frisch )?vorgef[üu]hrt (?:am|im) {_IDATE}"),
    _irule("last", "revisione", rf"\bultima revisione ?(?:: ?|il |a |nel |in |del )?{_IDATE}"),
    _irule("last", "revisione", rf"\brevisione (?:fatta|effettuata|eseguita) (?:il |a |nel |in )?{_IDATE}"),
    _irule("last", "revisione", rf"\b(?:appena )?revisionat[ao] (?:il |a |nel |in )?{_IDATE}"),
    _irule("last", "collaudo", rf"\bultimo collaudo ?(?:: ?|il |nel |del )?{_IDATE}"),
    _irule("last", "collaudo", rf"\b(?:appena )?collaudat[ao] (?:il |a |nel |in )?{_IDATE}"),
    _irule("last", "mfk", rf"\bderni[èe]re expertise ?(?:: ?|en |le |du |au |de )?{_IDATE}"),
    _irule("last", "mfk", rf"\bexpertise (?:du|de|le|en) {_IDATE}"),
    _irule("last", "mfk", rf"\b(?:fra[îi]chement )?{_EXPD} (?:le |en |du |au |depuis )?{_IDATE}"),
    # A date next to bare MFK/revisione/expertise/collaudo wording: last inspection or next due?
    _irule("bare", "mfk", rf"\b{_MFK} ?:? ?{_IDATE}"),
    _irule("bare", "revisione", rf"\brevisione ?:? ?{_IDATE}"),
    _irule("bare", "collaudo", rf"\bcollaudo ?:? ?{_IDATE}"),
    _irule("bare", "mfk", rf"\bexpertise ?:? ?{_IDATE}"),
    # Freshly inspected (seller wording; positive only when not conditional).
    _irule("fresh", "hu_tuv", rf"\b{_HU} ?[:\-\u2013]? ?(?:neu|frisch)\b"),
    _irule("fresh", "hu_tuv", rf"\b(?:neue[rn]?|frische[rn]?) {_HU}\b"),
    _irule("fresh", "mfk", rf"\bab (?:der )?{_MFK}\b"),
    _irule("fresh", "mfk", rf"\b{_MFK} ?[:\-\u2013]? ?(?:neu|frisch)\b|\b(?:neue|frische) {_MFK}\b"),
    _irule("fresh", "mfk", r"\bfrisch vorgef[üu]hrt\b"),
    _irule("fresh", "revisione", r"\b(?:appena )?revisionat[ao]\b"),
    _irule("fresh", "revisione", r"\brevisione (?:appena )?(?:fatta|effettuata|nuova)\b"),
    _irule("fresh", "collaudo", r"\b(?:appena )?collaudat[ao]\b|\bcollaudo (?:nuovo|fatto|appena fatto)\b"),
    _irule("fresh", "mfk", rf"\b(?:fra[îi]chement )?{_EXPD}"),
    _irule("fresh", "mfk", r"\bexpertise (?:du jour|r[ée]cente|neuve|fra[îi]che)\b"),
)

# Wording that makes a fresh-inspection statement an offer instead of a fact
# ("ab MFK auf Wunsch", "TÜV neu gegen Aufpreis", "MFK + CHF 500", "revisione su richiesta").
_INSPECTION_CONDITIONAL = re.compile(
    r"\bauf wunsch\b|\baufpreis\b|\bzuschlag\b|\bnach absprache\b|\bwahlweise\b|\bfalls gew[üu]nscht\b"
    r"|\b(?:su|a) richiesta\b|\bsupplemento\b|\bsur demande\b|\ben option\b|\bsuppl[ée]ment\b"
    r"|\bon request\b|\+ ?(?:chf|sfr|fr\.|eur\b|\u20ac)",
    re.IGNORECASE,
)
# "possible"-type wording only counts directly after the statement ("ab MFK möglich"), so an
# unrelated "Probefahrt möglich" in the next sentence does not hide a real claim.
_INSPECTION_POSSIBLE = re.compile(
    r"^\W{0,3}(?:(?:ist|sind|è|e|est) )?(?:m(?:ö|oe?)glich|optional|possibile|possible)\b", re.IGNORECASE
)
_POSSIBLE_WINDOW = 20
_CLAUSE_BREAK = re.compile(r"[!?;|\u2022\u00b7]")


class InspectionParse(BaseModel):
    """Technical-inspection wording on a listing page. Seller statements only, never verified.

    ``roadworthy_claim`` answers "does the seller's wording claim a valid/fresh inspection?":
    ``seller_claimed`` (fresh-inspection wording or a stated expiry not before ``as_of``),
    ``seller_denied`` (explicitly expired, "no valid inspection", or a stated expiry before
    ``as_of``), ``conflicting`` (both) or ``unknown``. It is never ``verified``: only an
    owner-held report can verify an inspection (spec 19).
    """

    model_config = _FROZEN

    roadworthy_claim: ClaimStatus = ClaimStatus.UNKNOWN
    inspection_expiry: PartialDate = PartialDate()
    last_inspection: PartialDate = PartialDate()
    inspection_kind: InspectionKind = "unknown"
    fresh_inspection: bool = False
    evidence: tuple[str, ...] = Field(default=(), max_length=_MAX_INSPECTION_EVIDENCE)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _never_verified(self) -> InspectionParse:
        if self.roadworthy_claim == ClaimStatus.VERIFIED:
            raise ValueError("seller inspection wording is never a verified inspection result")
        if self.fresh_inspection and self.roadworthy_claim != ClaimStatus.SELLER_CLAIMED:
            raise ValueError("fresh_inspection requires a positive seller claim")
        return self


def _expand_year(digits: str, as_of: date, warnings: list[str]) -> int:
    """'27' -> 2027 relative to ``as_of``: the century that keeps the year at most
    ``INSPECTION_YEARS_AHEAD`` years ahead (the plausibility window is checked by the caller)."""
    if len(digits) == 4:
        return int(digits)
    warnings.append(ParseWarning.TWO_DIGIT_YEAR_EXPANDED)
    year = as_of.year // 100 * 100 + int(digits)
    if year > as_of.year + INSPECTION_YEARS_AHEAD:
        year -= 100
    return year


def _inspection_date(raw: str, as_of: date) -> tuple[PartialDate | None, list[str]]:
    """Parse the date captured by ``_IDATE`` at its stated precision (never inventing a day)."""
    warnings: list[str] = []
    text = raw.strip()
    month: int | None = None
    day: int | None = None
    if (m := _ID_DMY.fullmatch(text)) is not None:
        day, month, year = int(m[1]), int(m[2]), _expand_year(m[3], as_of, warnings)
    elif (m := _ID_ISO.fullmatch(text)) is not None:
        year, month, day = int(m[1]), int(m[2]), int(m[3]) if m[3] else None
    elif (m := _ID_MY.fullmatch(text)) is not None:
        month, year = int(m[1]), _expand_year(m[2], as_of, warnings)
    elif (m := _ID_NAME.fullmatch(text)) is not None:
        month, year = _INSPECTION_MONTHS.get(m[1].lower()), int(m[2])
    elif _ID_YEAR.fullmatch(text) is not None:
        year = int(text)
    else:
        return None, [*warnings, ParseWarning.DATE_UNPARSEABLE]
    if month is not None and not 1 <= month <= 12:
        return None, [*warnings, ParseWarning.INVALID_DATE]
    if day is not None:
        try:
            date(year, month or 0, day)
        except ValueError:
            return None, [*warnings, ParseWarning.INVALID_DATE]
    if not as_of.year - INSPECTION_YEARS_BACK <= year <= as_of.year + INSPECTION_YEARS_AHEAD:
        return None, [*warnings, ParseWarning.INSPECTION_DATE_IMPLAUSIBLE]
    if day is not None and month is not None:
        return PartialDate(value=f"{year:04d}-{month:02d}-{day:02d}", precision=Precision.DAY), warnings
    if month is not None:
        return PartialDate(value=f"{year:04d}-{month:02d}", precision=Precision.MONTH), warnings
    return PartialDate(value=f"{year:04d}", precision=Precision.YEAR), warnings


def _compare_to(value: PartialDate, as_of: date) -> int | None:
    """-1 if ``value`` lies wholly before ``as_of``, 1 if wholly after, 0 if it contains it;
    ``None`` if unknown. Month precision means the whole month (an inspection due "05/2027" is
    valid through May 2027)."""
    if value.value is None or value.year is None:
        return None
    if value.precision == Precision.DAY:
        stated = date.fromisoformat(value.value)
        return (stated > as_of) - (stated < as_of)
    if value.precision == Precision.MONTH:
        key, ref = (value.year, value.month or 0), (as_of.year, as_of.month)
    else:
        key, ref = (value.year, 0), (as_of.year, 0)
    return (key > ref) - (key < ref)


def _is_conditional(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - _CONDITION_WINDOW) : start]
    after = text[end : end + _CONDITION_WINDOW]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    if breaks:
        before = before[breaks[-1].end() :]
    first = _CLAUSE_BREAK.search(after)
    if first is not None:
        after = after[: first.start()]
    if _INSPECTION_CONDITIONAL.search(f"{before} {after}") is not None:
        return True
    return _INSPECTION_POSSIBLE.search(text[end : end + _POSSIBLE_WINDOW]) is not None


def _as_of_date(as_of: date) -> date:
    if isinstance(as_of, datetime):
        try:
            return ensure_utc(as_of).date()
        except ValueError as exc:
            raise ValidationFailed("as_of must be timezone-aware") from exc
    return as_of


def parse_inspection(text: str | None, locale: Locale | None, as_of: date) -> InspectionParse:
    """Read technical-inspection (roadworthiness) wording from listing text.

    Recognised (case-insensitive, whitespace-normalised; all SELLER wording, never verified):

    - DE HU/TÜV (``hu_tuv``): ``'HU/AU neu'``, ``'TÜV neu'``, ``'neuer TÜV'`` (fresh);
      ``'HU bis 05/2027'``, ``'TÜV 05/2027'``, ``'HU 05/27'``, ``'TÜV: 05.2027'`` (expiry: in DE a
      date next to HU/TÜV is the due date); ``'TÜV abgelaufen'``, ``'TÜV fällig'`` (expired);
      ``'ohne TÜV'``, ``'kein TÜV'`` (no valid inspection -> ``seller_denied``).
    - CH MFK (``mfk``): ``'ab MFK'``, ``'frisch ab MFK'``, ``'MFK neu'``, ``'frisch vorgeführt'``
      (fresh); ``'MFK bis 06/2026'``, ``'nächste MFK 06/2026'`` (expiry); ``'letzte MFK 2025'``,
      ``'ab MFK 03.2025'`` (last inspection, not an expiry); ``'vor MFK'``, ``'nicht ab MFK'``,
      ``'ohne MFK'``, ``'muss zur MFK'`` (not freshly inspected: never positive; validity unknown,
      ``INSPECTION_NOT_FRESH``); ``'MFK abgelaufen'`` (expired). A bare ``'MFK 05.2024'`` is
      ambiguous (Swiss ads use it for the last inspection) and yields no date
      (``INSPECTION_DATE_AMBIGUOUS``).
    - IT revisione (``revisione``): ``'revisionata'``, ``'revisione fatta'`` (fresh);
      ``'revisione fino a 05/2027'``, ``'scadenza revisione 05/2027'`` (expiry); ``'ultima
      revisione 2025'`` (last); ``'revisione scaduta'`` (expired); ``'senza revisione'``,
      ``'da revisionare'`` (no valid inspection). Ticino ``'collaudata'``/``'senza collaudo'`` are
      MFK wording when ``locale='ch'`` and revisione wording otherwise.
    - FR (Swiss romandie, ``mfk``): ``'expertisé(e)'``, ``'expertise du jour'`` (fresh);
      ``'prochaine expertise 06/2027'`` (expiry); ``'dernière expertise 2025'``, ``'expertisée le
      03.2025'`` (last); ``'sans expertise'``, ``'non expertisé'``, ``'à expertiser'`` (not fresh).

    Rules: negated, expired and dated statements are matched first and masked, so they can never
    be re-read as a bare positive. A stated expiry before ``as_of`` (month precision: before the
    ``as_of`` month) is ``INSPECTION_EXPIRED`` and never positive; a year-only expiry in the
    ``as_of`` year is ``INSPECTION_EXPIRY_IMPRECISE`` and not positive. Fresh wording next to
    "auf Wunsch"/"gegen Aufpreis"/"+ CHF"/"su richiesta"/"sur demande" is an offer, not a fact
    (``INSPECTION_CONDITIONAL``, not positive). Two-digit years map to the century that keeps the
    year at most ``INSPECTION_YEARS_AHEAD`` years after ``as_of`` (``TWO_DIGIT_YEAR_EXPANDED``);
    years outside ``as_of - 30 .. as_of + 5`` are ``INSPECTION_DATE_IMPLAUSIBLE``. Different
    stated expiries are ``MULTIPLE_DATES`` (no expiry). Positive and negative wording together is
    ``conflicting``. ``locale`` only decides the kind of Ticino ``collaudo`` wording.
    """
    as_day = _as_of_date(as_of)
    if text is None or not text.strip():
        return InspectionParse(warnings=(ParseWarning.EMPTY_INPUT,))
    if len(text) > MAX_INSPECTION_TEXT_LENGTH:
        return InspectionParse(warnings=(ParseWarning.INPUT_TOO_LONG,))
    cleaned = re.sub(r"\s+", " ", _clean(text))
    work = cleaned
    warnings: list[str] = []
    evidence: list[str] = []
    kinds: set[InspectionKind] = set()
    expiries: list[PartialDate] = []
    lasts: list[PartialDate] = []
    expired_wording = not_valid = not_fresh = ambiguous = conditional = False
    fresh = False

    for rule in _INSPECTION_RULES:
        matches = list(rule.pattern.finditer(work))
        if not matches:
            continue
        kind: InspectionKind = (
            rule.kind if rule.kind != "collaudo" else "mfk" if locale == "ch" else "revisione"
        )
        for match in matches:
            kinds.add(kind)
            if rule.role == "fresh":
                if _is_conditional(cleaned, match.start(), match.end()):
                    conditional = True
                else:
                    fresh = True
            elif rule.role == "expired":
                expired_wording = True
            elif rule.role == "not_valid":
                not_valid = True
            elif rule.role == "not_fresh":
                not_fresh = True
            elif rule.role == "bare":
                ambiguous = True
            else:  # expiry / last: a dated statement
                parsed, date_warnings = _inspection_date(match.group("date"), as_day)
                warnings.extend(date_warnings)
                if parsed is not None:
                    (expiries if rule.role == "expiry" else lasts).append(parsed)
                if rule.role == "expiry" and match.groupdict().get("fresh"):
                    fresh = True
            snippet = match.group(0).strip()
            if snippet and len(evidence) < _MAX_INSPECTION_EVIDENCE and snippet not in evidence:
                evidence.append(snippet[:_EVIDENCE_LIMIT])
        for match in reversed(matches):
            work = work[: match.start()] + _MASK * (match.end() - match.start()) + work[match.end() :]

    expiry = PartialDate()
    distinct_expiries = list(dict.fromkeys(expiries))
    # Any stated expiry in the past blocks a positive claim, even when another (different) expiry
    # is stated too; several different expiries leave the expiry itself unknown.
    expiry_past = any((_compare_to(e, as_day) or 0) < 0 for e in distinct_expiries)
    expiry_valid = False
    if len(distinct_expiries) > 1:
        warnings.append(ParseWarning.MULTIPLE_DATES)
    elif distinct_expiries:
        expiry = distinct_expiries[0]
        expiry_cmp = _compare_to(expiry, as_day)
        expiry_valid = expiry_cmp is not None and (
            expiry_cmp > 0 or (expiry_cmp == 0 and expiry.precision != Precision.YEAR)
        )
        if expiry_cmp == 0 and expiry.precision == Precision.YEAR:
            warnings.append(ParseWarning.INSPECTION_EXPIRY_IMPRECISE)

    last = PartialDate()
    distinct_lasts = list(dict.fromkeys(lasts))
    if len(distinct_lasts) > 1:
        warnings.append(ParseWarning.MULTIPLE_DATES)
    elif distinct_lasts:
        last_cmp = _compare_to(distinct_lasts[0], as_day)
        if last_cmp is not None and last_cmp > 0:
            warnings.append(ParseWarning.FUTURE_DATE)
        else:
            last = distinct_lasts[0]

    if expired_wording or expiry_past:
        warnings.append(ParseWarning.INSPECTION_EXPIRED)
    if not_valid:
        warnings.append(ParseWarning.INSPECTION_NOT_VALID)
    if not_fresh:
        warnings.append(ParseWarning.INSPECTION_NOT_FRESH)
    if ambiguous:
        warnings.append(ParseWarning.INSPECTION_DATE_AMBIGUOUS)
    if conditional:
        warnings.append(ParseWarning.INSPECTION_CONDITIONAL)

    positive = fresh or expiry_valid
    negative = expired_wording or not_valid or expiry_past
    claim: ClaimStatus
    if positive and (negative or not_fresh):
        claim = ClaimStatus.CONFLICTING
        warnings.append(ParseWarning.INSPECTION_CONFLICTING)
    elif negative:
        claim = ClaimStatus.SELLER_DENIED
    elif positive:
        claim = ClaimStatus.SELLER_CLAIMED
    else:
        claim = ClaimStatus.UNKNOWN
    return InspectionParse(
        roadworthy_claim=claim,
        inspection_expiry=expiry,
        last_inspection=last,
        inspection_kind=next(iter(kinds)) if len(kinds) == 1 else "unknown",
        fresh_inspection=fresh and claim == ClaimStatus.SELLER_CLAIMED,
        evidence=tuple(evidence),
        warnings=tuple(dict.fromkeys(warnings)),
    )
