"""Deterministic extraction primitives shared by source adapters.

Everything here is pure (no I/O) and conservative: when a value is ambiguous
the helpers return `None` instead of guessing, so unknown stays unknown.

Scope note: `domain/parsing.py` (locale number/price/mileage/date parsing) is
owned by another work package. The small private parsers below exist so the
adapters do not depend on a module that is still being written; they should be
consolidated into `domain/parsing.py` once it lands (tracked as an open issue).

Rules implemented here (spec sections 3, 7, 24, 31):
- Decimal only. JSON-LD is decoded with `parse_float=Decimal` so no binary float
  ever touches a price, mileage or power value.
- DE/IT/CH separators: `.` `,` apostrophes (`'` `’`), NBSP/narrow NBSP/thin space
  as grouping, trailing `.–`/`,-` "no cents" notation. Plain ASCII spaces are never
  treated as grouping inside free text (they would merge adjacent numbers).
- Miles -> km uses the exact factor 1.609344 and keeps the unrounded result.
- Seller text is untrusted: markup is removed, length is bounded and
  prompt-injection-like wording is flagged, never interpreted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxml import etree
from lxml import html as lxml_html

from suv_deals.domain.enums import Precision, Tristate
from suv_deals.domain.listings import PartialDate, SourceTimestamp

# --------------------------------------------------------------------------- units

MILES_TO_KM = Decimal("1.609344")  # exact international mile
PS_TO_KW = Decimal("0.73549875")  # metric horsepower (PS, CV, ch): exact definition
HP_TO_KW = Decimal("0.74569987158227022")  # mechanical/imperial horsepower (UN/CEFACT BHP)

NumberKind = Literal["money", "count"]

_GROUPING_CHARS = ("'", "’", "ʼ", " ", " ", " ", " ", " ")
_DASH_CENTS = re.compile(r"[.,]\s?[-–—]{1,2}$")
_TRAILING_DASH = re.compile(r"[-–—]{1,2}$")
_DIGITS_AND_SEPARATORS = re.compile(r"\d[\d.,]*")

# Number token used inside free text: grouping only with . , apostrophes or typographic spaces.
_NUM_TOKEN = r"\d{1,3}(?:[.,'’   ]\d{3})+|\d+"


def _grouped_digits(value: str, sep: str) -> str | None:
    parts = value.split(sep)
    if not 1 <= len(parts[0]) <= 3 or any(len(part) != 3 for part in parts[1:]):
        return None
    joined = "".join(parts)
    return joined if joined.isdigit() else None


def parse_locale_number(raw: str, *, kind: NumberKind) -> Decimal | None:
    """Parse one number token written with DE/IT/CH/EN conventions.

    Returns None for anything ambiguous or malformed (e.g. ``1234.567`` or ``0.500``).
    A single separator followed by exactly three digits is a thousands separator
    (``187.500`` -> 187500, ``2,750`` -> 2750); one or two digits are decimals.
    """
    text = raw.strip()
    if not text or len(text) > 40:
        return None
    text = _DASH_CENTS.sub("", text)
    text = _TRAILING_DASH.sub("", text)
    for ch in _GROUPING_CHARS:
        text = text.replace(ch, "")
    if not text or not _DIGITS_AND_SEPARATORS.fullmatch(text) or text[-1] in ".,":
        return None
    try:
        if "." in text and "," in text:
            dec_pos = max(text.rfind("."), text.rfind(","))
            dec = text[dec_pos]
            grp = "," if dec == "." else "."
            int_part, frac = text[:dec_pos], text[dec_pos + 1 :]
            if dec in int_part or not frac.isdigit():
                return None
            digits = _grouped_digits(int_part, grp) if grp in int_part else int_part
            if digits is None or not digits.isdigit():
                return None
            if kind == "money" and len(frac) > 2:
                return None
            return Decimal(f"{digits}.{frac}")
        sep = "." if "." in text else "," if "," in text else None
        if sep is None:
            return Decimal(text)
        parts = text.split(sep)
        if len(parts) > 2:
            grouped = _grouped_digits(text, sep)
            return Decimal(grouped) if grouped is not None else None
        head, frac = parts
        if not head:
            return None
        if len(frac) == 3:
            if 1 <= len(head) <= 3 and head != "0":
                return Decimal(head + frac)
            return None  # "1234.567" / "0.500": ambiguous
        if 1 <= len(frac) <= 2:
            return Decimal(f"{head}.{frac}")
        return None
    except InvalidOperation:
        return None


def decimal_from_json(value: Any, *, kind: NumberKind) -> Decimal | None:
    """Decimal from a JSON-LD scalar (int, Decimal from parse_float, or a locale string)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        stripped = value.strip()
        # Machine format first (schema.org recommends '.' as the decimal mark, no grouping).
        if re.fullmatch(r"\d+(\.\d{1,2})?", stripped):
            return Decimal(stripped)
        return parse_locale_number(stripped, kind=kind)
    return None


def json_number_is_ambiguous(value: Any) -> bool:
    """A JSON numeric literal with exactly three fraction digits (e.g. ``187.500``) is
    almost certainly a locale-formatted integer typed into JSON. Treat as ambiguous."""
    if isinstance(value, Decimal) and value.is_finite():
        exp = value.as_tuple().exponent
        return isinstance(exp, int) and exp == -3
    return False


def round_half_up_int(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def miles_to_km(miles: Decimal) -> Decimal:
    """Exact conversion; never rounded here (rounding could cross the 200,000 km rule)."""
    return miles * MILES_TO_KM


# --------------------------------------------------------------------------- whitespace


_WS = re.compile(r"[ \t\r\f\v      ​]+")
_MULTI_NL = re.compile(r"\n\s*\n+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def collapse_ws(text: str) -> str:
    """Single-line normalisation (all whitespace incl. NBSP -> one space)."""
    return re.sub(r"\s+", " ", _WS.sub(" ", text)).strip()


def bounded(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- money in text

_CURRENCY_ALIASES: dict[str, str] = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "chf": "CHF",
    "sfr.": "CHF",
    "mkd": "MKD",
    "ден": "MKD",
    "ден.": "MKD",
}
_CUR = r"€|Euro|EUR|CHF|SFr\.|MKD|ден\.?"
_MONEY_NUM = (
    r"\d{1,3}(?:[.,'’   ]\d{3})+(?:[.,](?:\d{1,2}|\s?[-–—]{1,2}))?"
    r"|\d+(?:[.,](?:\d{1,2}|\s?[-–—]{1,2}))?"
)
_PRICE_RE = re.compile(
    rf"(?<![A-Za-z])(?P<cur1>{_CUR})\s?(?P<num1>{_MONEY_NUM})(?![\d])"
    rf"|(?<![\w.,'’])(?P<num2>{_MONEY_NUM})\s?(?P<cur2>{_CUR})(?![A-Za-z])",
    re.IGNORECASE,
)
_MONTHLY = re.compile(
    r"^\W{0,3}(mtl|monatl|/\s*monat|pro\s+monat|im\s+monat|al\s+mese|/\s*mese|mensil|/\s*mo\b|p\.\s?m\.|"
    r"per\s+month|/\s*month|rate\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MoneyMatch:
    amount: Decimal
    currency: str
    raw: str
    start: int
    monthly: bool


def currency_code(token: str) -> str | None:
    token = token.strip()
    if re.fullmatch(r"[A-Z]{3}", token):
        return token
    return _CURRENCY_ALIASES.get(token.lower())


def find_money(text: str) -> list[MoneyMatch]:
    """All currency amounts in free text, in order. Instalment-like amounts are flagged `monthly`."""
    found: list[MoneyMatch] = []
    for match in _PRICE_RE.finditer(text):
        cur_raw = match.group("cur1") or match.group("cur2") or ""
        num_raw = match.group("num1") or match.group("num2") or ""
        currency = currency_code(cur_raw)
        amount = parse_locale_number(num_raw, kind="money")
        if currency is None or amount is None:
            continue
        tail = text[match.end() : match.end() + 25]
        found.append(
            MoneyMatch(
                amount=amount,
                currency=currency,
                raw=match.group(0),
                start=match.start(),
                monthly=bool(_MONTHLY.match(tail)),
            )
        )
    return found


# --------------------------------------------------------------------------- mileage in text

_KM_UNITS = r"km|kilometern?|kilometres?|kilometers|chilometri"
_MI_UNITS = r"miles|meilen|miglia|mi"
_MILEAGE_AFTER = re.compile(
    rf"(?<![\w.,'’])(?P<num>{_NUM_TOKEN})\s?(?P<unit>{_KM_UNITS}|{_MI_UNITS})\b(?!\s*/\s*h)",
    re.IGNORECASE,
)
_MILEAGE_BEFORE = re.compile(
    rf"(?<![\w/])(?P<unit>km)\.?\s?:?\s?(?P<num>{_NUM_TOKEN})(?![\d])(?![.,'’]\d)",
    re.IGNORECASE,
)
_ESTIMATE = re.compile(
    r"(ca\.?|circa|approx\.?|approximately|etwa|ungefähr|rund|about|~|≈)\s*$", re.IGNORECASE
)
_ODOMETER_LABEL = re.compile(
    r"(kilometerstand|km-stand|kilometer-stand|laufleistung|tachostand|chilometraggio|percorrenza|"
    r"km\s+percorsi|mileage|odometer)\s*[:=]?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MileageMatch:
    km: Decimal  # canonical, unrounded
    amount: Decimal  # as written
    unit: Literal["km", "mi"]
    raw: str
    is_estimate: bool
    start: int


def _mileage_from(num_raw: str, unit_raw: str, raw: str, start: int, prefix: str) -> MileageMatch | None:
    amount = parse_locale_number(num_raw, kind="count")
    if amount is None:
        return None
    unit: Literal["km", "mi"] = "mi" if unit_raw.lower() in {"mi", "miles", "meilen", "miglia"} else "km"
    km = miles_to_km(amount) if unit == "mi" else amount
    return MileageMatch(
        km=km,
        amount=amount,
        unit=unit,
        raw=raw,
        is_estimate=bool(_ESTIMATE.search(prefix)),
        start=start,
    )


def find_mileages(text: str) -> list[MileageMatch]:
    """All `<number> km|mi` and Italian-style `km <number>` mentions, ordered by position."""
    seen: set[int] = set()
    results: list[MileageMatch] = []
    for regex in (_MILEAGE_AFTER, _MILEAGE_BEFORE):
        for match in regex.finditer(text):
            if any(match.start() <= s < match.end() for s in seen):
                continue
            item = _mileage_from(
                match.group("num"),
                match.group("unit"),
                match.group(0),
                match.start(),
                text[max(0, match.start() - 14) : match.start()],
            )
            if item is not None:
                seen.add(match.start())
                results.append(item)
    results.sort(key=lambda m: m.start)
    return results


def find_labelled_odometer(text: str) -> list[MileageMatch]:
    """Odometer statements introduced by an explicit label ("Kilometerstand: 87.500 km").

    Used for conflict detection in seller descriptions, where unlabelled numbers such
    as "Zahnriemen bei 150.000 km gewechselt" are not odometer claims.
    """
    results: list[MileageMatch] = []
    for label in _ODOMETER_LABEL.finditer(text):
        window = text[label.end() : label.end() + 40]
        est = re.match(r"(ca\.?|circa|etwa|approx\.?)\s*", window, re.IGNORECASE)
        offset = est.end() if est else 0
        num = re.match(rf"(?P<num>{_NUM_TOKEN})(?![\d])\s?(?P<unit>{_KM_UNITS}|{_MI_UNITS})?\b", window[offset:])
        if not num:
            continue
        item = _mileage_from(
            num.group("num"),
            num.group("unit") or "km",
            text[label.start() : label.end() + offset + num.end()],
            label.start(),
            "ca." if est else "",
        )
        if item is not None:
            results.append(item)
    return results


# --------------------------------------------------------------------------- power / displacement


@dataclass(frozen=True, slots=True)
class PowerValue:
    kw: int
    raw: str
    converted_from: str | None  # None when stated in kW


def power_from_value(value: Decimal, unit: str, raw: str) -> PowerValue | None:
    unit_l = unit.strip().lower()
    if value <= 0:
        return None
    if unit_l in {"kwt", "kw"}:
        return PowerValue(kw=round_half_up_int(value), raw=raw, converted_from=None)
    if unit_l in {"ps", "cv", "ch", "hk"}:
        return PowerValue(kw=round_half_up_int(value * PS_TO_KW), raw=raw, converted_from="metric_hp")
    if unit_l in {"bhp", "hp"}:
        return PowerValue(kw=round_half_up_int(value * HP_TO_KW), raw=raw, converted_from="mechanical_hp")
    return None


_POWER_RE = re.compile(r"(?<![\d.,])(?P<num>\d{2,4})\s?(?P<unit>kW|PS|CV|bhp|hp)\b", re.IGNORECASE)


def find_power(text: str) -> PowerValue | None:
    """Prefer an explicit kW statement; fall back to PS/CV/hp."""
    candidates = [
        (m.group("unit").lower(), Decimal(m.group("num")), m.group(0)) for m in _POWER_RE.finditer(text)
    ]
    for unit, value, raw in candidates:
        if unit == "kw":
            return power_from_value(value, unit, raw)
    for unit, value, raw in candidates:
        result = power_from_value(value, unit, raw)
        if result is not None:
            return result
    return None


# --------------------------------------------------------------------------- dates


def parse_partial_date(raw: str) -> PartialDate | None:
    """ISO and common European forms. A month is never invented into a day."""
    text = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", text)
    if m:
        try:
            parsed = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
        return PartialDate(value=parsed.isoformat(), precision=Precision.DAY)
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if m:
        try:
            parsed = date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
        return PartialDate(value=parsed.isoformat(), precision=Precision.DAY)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text) or None
    if m:
        year, month = int(m[1]), int(m[2])
    else:
        m = re.fullmatch(r"(\d{1,2})\s?[./-]\s?(\d{4})", text)
        if m:
            year, month = int(m[2]), int(m[1])
        else:
            m = re.fullmatch(r"(\d{4})", text)
            if m:
                return PartialDate(value=m[1], precision=Precision.YEAR)
            return None
    if not 1 <= month <= 12:
        return None
    return PartialDate(value=f"{year:04d}-{month:02d}", precision=Precision.MONTH)


def year_from(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        year = int(value)
    elif isinstance(value, str):
        m = re.match(r"\s*(\d{4})\b", value)
        if not m:
            pd = parse_partial_date(value)
            return pd.year if pd else None
        year = int(m[1])
    else:
        return None
    return year if 1950 <= year <= 2100 else None


def parse_source_timestamp(raw: str, source_zone: str) -> SourceTimestamp | None:
    """Zone-aware parse. Zone-less values get the documented source zone and a flag."""
    text = raw.strip()
    if not text or len(text) > 100:
        return None
    try:
        zone = ZoneInfo(source_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return None
        local = datetime.combine(day, time(0, 0), tzinfo=zone)
        return SourceTimestamp(
            value=local.astimezone(UTC),
            raw=text,
            zone_assumed=True,
            assumed_zone=source_zone,
            precision=Precision.DAY,
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return SourceTimestamp(
            value=parsed.replace(tzinfo=zone).astimezone(UTC),
            raw=text,
            zone_assumed=True,
            assumed_zone=source_zone,
            precision=Precision.DAY,
        )
    return SourceTimestamp(value=parsed.astimezone(UTC), raw=text, precision=Precision.DAY)


# --------------------------------------------------------------------------- VIN

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def normalize_vin(raw: Any) -> tuple[str | None, Tristate]:
    """(vin, format_valid). Only a provided, format-valid VIN is kept (ISO 3779 characters)."""
    if not isinstance(raw, str):
        return None, Tristate.UNKNOWN
    candidate = re.sub(r"[\s-]", "", raw).upper()
    if not candidate:
        return None, Tristate.UNKNOWN
    if _VIN_RE.fullmatch(candidate):
        return candidate, Tristate.YES
    return None, Tristate.NO


# --------------------------------------------------------------------------- HTML -> text

_DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "head")
_ACTIVE_MARKUP = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|svg|style|link|meta|base|form)\b|javascript\s*:|"
    r"\bon(error|load|click|mouseover|focus|submit)\s*=",
    re.IGNORECASE,
)
_SCRIPT_BLOCK = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_LIKE = re.compile(r"<\s*/?\s*[A-Za-z!][^<>]{0,500}>")
_MAX_MARKUP_INPUT = 200_000


@dataclass(frozen=True, slots=True)
class CleanText:
    text: str
    markup_removed: bool
    active_markup: bool


def html_to_text(markup: str) -> CleanText:
    """Plain text from an untrusted HTML fragment; never returns tags."""
    source = markup[:_MAX_MARKUP_INPUT]
    active = bool(_ACTIVE_MARKUP.search(source))
    removed = False
    text = source
    if "<" in source or "&" in source:
        try:
            fragment = lxml_html.fragment_fromstring(source, create_parent="div")
            etree.strip_elements(fragment, *_DROP_TAGS, with_tail=False)
            for br in fragment.iter("br", "p", "li", "div", "tr"):
                br.tail = "\n" + (br.tail or "")
            text = str(fragment.text_content())
            removed = "<" in source
        except (etree.ParserError, ValueError):
            text = _TAG_LIKE.sub(" ", _SCRIPT_BLOCK.sub(" ", source))
            removed = True
    # Entity-encoded markup decodes to literal tags; remove those too (defence in depth).
    if _TAG_LIKE.search(text):
        active = active or bool(_ACTIVE_MARKUP.search(text))
        text = _TAG_LIKE.sub(" ", _SCRIPT_BLOCK.sub(" ", text))
        removed = True
    text = _CONTROL.sub("", text)
    lines = [collapse_ws(line) for line in text.split("\n")]
    text = _MULTI_NL.sub("\n\n", "\n".join(lines)).strip()
    return CleanText(text=text, markup_removed=removed, active_markup=active)


# --------------------------------------------------------------------------- prompt injection

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "ignore_instructions",
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your)\b"
            r"[^.\n]{0,30}\b(instructions?|prompts?|rules|directives)\b",
        ),
        ("ignore_instructions_de", r"\b(ignorier\w*|vergiss|missachte\w*)\b[^.\n]{0,40}\b(anweisung\w*|instruktion\w*)"),
        ("ignore_instructions_it", r"\b(ignora\w*|dimentica\w*)\b[^.\n]{0,40}\b(istruzion\w*)"),
        ("system_prompt", r"\b(system\s?prompt|developer message|jailbreak)\b"),
        ("role_override", r"\byou are (now )?(an?|the) (ai|assistant|language model|llm|agent|bot)\b"),
        (
            "secret_request",
            r"\b(reveal|print|send|share|leak|show|output)\b[^.\n]{0,30}"
            r"\b(api[ _-]?keys?|secrets?|passwords?|tokens?|credentials?)\b",
        ),
        (
            "approval_request",
            r"\b(approve|shortlist|whitelist|auto-?approve|mark)\b[^.\n]{0,20}\b(this|the)\b[^.\n]{0,15}"
            r"\b(car|vehicle|listing|offer|deal)\b",
        ),
        (
            "tool_call",
            r"\b(call|fetch|invoke|post to|send (?:it|this|the data|your \w+) to)\b[^.\n]{0,25}"
            r"(https?://|\burl\b|\bendpoint\b|\bwebhook\b)",
        ),
    )
)


def injection_signals(text: str) -> tuple[str, ...]:
    """Names of prompt-injection-like patterns found in untrusted text (flag only, never obey)."""
    return tuple(name for name, regex in _INJECTION_PATTERNS if regex.search(text))


@dataclass(frozen=True, slots=True)
class SellerText:
    excerpt: str | None
    warnings: tuple[str, ...]
    signals: tuple[str, ...]


def clean_seller_text(raw: str | None, *, limit: int = 4000) -> SellerText:
    """Bounded plain-text excerpt of untrusted seller text plus safety flags."""
    if raw is None or not raw.strip():
        return SellerText(excerpt=None, warnings=(), signals=())
    cleaned = html_to_text(raw)
    warnings: list[str] = []
    signals = injection_signals(cleaned.text)
    if cleaned.active_markup:
        warnings.append("SELLER_TEXT_MARKUP_REMOVED")
        signals = (*signals, "active_markup")
    if signals and "SELLER_TEXT_SUSPICIOUS" not in warnings:
        warnings.append("SELLER_TEXT_SUSPICIOUS")
    excerpt = bounded(cleaned.text, limit)
    if excerpt is not None and len(cleaned.text) > limit:
        warnings.append("SELLER_TEXT_TRUNCATED")
    return SellerText(excerpt=excerpt, warnings=tuple(warnings), signals=signals)


# --------------------------------------------------------------------------- JSON-LD

_MAX_JSON_LD_BLOCK = 1_000_000
_MAX_DEPTH = 8


@dataclass(frozen=True, slots=True)
class JsonLd:
    nodes: tuple[dict[str, Any], ...]
    blocks: int
    malformed: int


def _strip_wrappers(text: str) -> str:
    text = text.strip()
    for prefix in ("<!--", "//<![CDATA[", "<![CDATA["):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    for suffix in ("-->", "//]]>", "]]>"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def _flatten(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item, depth + 1)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            yield from _flatten(graph, depth + 1)
        if "@type" in value:
            yield value
        main = value.get("mainEntity")
        if isinstance(main, dict | list):
            yield from _flatten(main, depth + 1)


def parse_json_ld_blocks(blocks: Iterable[str]) -> JsonLd:
    nodes: list[dict[str, Any]] = []
    count = 0
    malformed = 0
    for block in blocks:
        count += 1
        if len(block) > _MAX_JSON_LD_BLOCK:
            malformed += 1
            continue
        try:
            data = json.loads(_strip_wrappers(block), parse_float=Decimal)
        except (json.JSONDecodeError, ValueError, RecursionError):
            malformed += 1
            continue
        nodes.extend(_flatten(data))
    return JsonLd(nodes=tuple(nodes), blocks=count, malformed=malformed)


def local_type_names(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    names: set[str] = set()
    for value in values:
        if isinstance(value, str) and value:
            names.add(re.split(r"[/:#]", value)[-1])
    return names


def scalar(value: Any) -> Any:
    """Unwrap single-element lists; return the first non-empty element of a list."""
    if isinstance(value, list):
        for item in value:
            if item not in (None, "", [], {}):
                return item
        return None
    return value


def text_of(value: Any) -> str | None:
    """Readable text of a JSON-LD value (string, number, or a node with name/value)."""
    value = scalar(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return collapse_ws(value) or None
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "@value", "value", "@id"):
            if key in value:
                return text_of(value[key])
    return None


def enum_name(value: Any) -> str | None:
    """`https://schema.org/InStock` / `schema:InStock` / `InStock` -> `InStock`."""
    text = text_of(value)
    if text is None:
        return None
    return re.split(r"[/:#]", text.strip())[-1] or None


# --------------------------------------------------------------------------- page view


@dataclass(frozen=True, slots=True)
class Anchor:
    href: str
    text: str
    title_attr: str | None
    container_text: str


@dataclass(frozen=True, slots=True)
class PageView:
    """Parsed, read-only view of one HTML document."""

    json_ld: JsonLd
    title: str | None
    h1: str | None
    lang: str | None
    visible_text: str  # whitespace-joined text nodes; typographic spaces preserved
    text_lower: str  # collapsed + lower-cased, for marker matching
    raw_lower: str  # bounded lower-cased raw HTML, for attribute markers
    has_password_input: bool
    next_links: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    microdata_description: str | None


_MAX_HTML = 6_000_000
_CONTAINER_TAGS = frozenset({"article", "li", "tr"})


def _element_text(element: Any) -> str:
    parts = [str(t) for t in element.itertext()]
    return " ".join(p for p in parts if p.strip())


def _container_text(anchor: Any) -> str:
    node = anchor
    for _ in range(6):
        parent = node.getparent()
        if parent is None:
            break
        node = parent
        if isinstance(node.tag, str) and node.tag.lower() in _CONTAINER_TAGS:
            return _element_text(node)[:2000]
    return _element_text(anchor)[:2000]


def parse_page(html: str | None) -> PageView | None:
    """Parse HTML once. Returns None for empty/unparseable documents."""
    if html is None or not html.strip():
        return None
    source = html[:_MAX_HTML]
    try:
        tree = lxml_html.document_fromstring(source)
    except (etree.ParserError, ValueError):
        return None
    blocks = [
        str(script.text or "")
        for script in tree.iter("script")
        if str(script.get("type") or "").strip().lower() == "application/ld+json"
    ]
    json_ld = parse_json_ld_blocks(blocks)
    title_el = tree.find(".//title")
    title = collapse_ws(_element_text(title_el)) if title_el is not None else None
    lang_raw = tree.get("lang")
    lang = str(lang_raw).strip()[:10] if lang_raw else None
    has_password = any(
        str(inp.get("type") or "").strip().lower() == "password" for inp in tree.iter("input")
    )
    next_links: list[str] = []
    for el in tree.iter("link", "a"):
        rel = str(el.get("rel") or "").lower().split()
        href = el.get("href")
        if "next" in rel and href:
            next_links.append(str(href).strip())
    md_desc = None
    for el in tree.iter():
        if isinstance(el.tag, str) and str(el.get("itemprop") or "").strip().lower() == "description":
            # Serialised before script stripping so active markup in seller text is detected.
            md_desc = str(lxml_html.tostring(el, encoding="unicode", with_tail=False))
            break
    etree.strip_elements(tree, "script", "style", "noscript", "template", with_tail=False)
    h1_el = tree.find(".//h1")
    h1 = collapse_ws(_element_text(h1_el)) if h1_el is not None else None
    anchors: list[Anchor] = []
    for a in tree.iter("a"):
        href = a.get("href")
        if not href:
            continue
        title_attr = a.get("title")
        anchors.append(
            Anchor(
                href=str(href).strip(),
                text=collapse_ws(_element_text(a)),
                title_attr=collapse_ws(str(title_attr)) if title_attr else None,
                container_text=_container_text(a),
            )
        )
    body = tree.find(".//body")
    visible = _element_text(body if body is not None else tree)
    visible = re.sub(r"[ \t\r\n]+", " ", visible).strip()
    return PageView(
        json_ld=json_ld,
        title=title or None,
        h1=h1 or None,
        lang=lang,
        visible_text=visible,
        text_lower=collapse_ws(visible).lower(),
        raw_lower=source[:1_000_000].lower(),
        has_password_input=has_password,
        next_links=tuple(next_links),
        anchors=tuple(anchors),
        microdata_description=md_desc,
    )
