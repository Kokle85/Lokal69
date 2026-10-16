"""Owner-run sample capture for parser work (``suv-deals capture-samples``; spec sections 5, 24, 31).

Parsers are written from real page structure, never from guessed structure. This module fetches a
handful of pages of ONE registered source from the owner's own network so that the structure can
be read locally. It is deliberately not a crawler:

- one search page (a reviewed path, no query string) and at most ``detail_limit`` detail pages, and
  only links that the source's reviewed detail pattern accepts; no detail pages at all for
  ``detail_mode: card_only`` sources;
- robots.txt is fetched first and every URL is checked against it for our product token; a URL that
  robots.txt disallows is never requested, and nothing is requested when robots.txt is unreachable;
- every request passes ``SourceUrlPolicy`` (host, path, public DNS). A redirect is followed only when
  its target is on the same host, passes the same policy and robots.txt, and within two hops; any
  other redirect stops the run;
- one request at a time, spaced by the source's ``min_delay_seconds`` or a robots crawl-delay;
- the first block (401/403/429), challenge page, network error or other non-2xx answer stops the
  run. There are no retries and no workaround of any kind: no CAPTCHA solving, proxies, stealth
  headers or IP changes.

Saved output, under ``<out_root>/<source_key>/<UTC timestamp>/``:

- ``search.html`` and ``detail-N.html``: the 2xx pages with e-mail addresses, international phone
  numbers and ``tel:`` links masked. Parsers are developed from these files on this machine only.
- ``structure.json``: per page, tag, class and attribute NAMES with counts, the ``h1`` count, JSON-LD
  ``@type`` and key NAMES, link shapes (host kind, first path segment, depth) and script tags (type,
  id, size). No text, no slugs and no attribute values.
- ``manifest.json``: statuses, sizes, SHA-256 of the saved bytes, the robots verdict and the stop
  reason. A run that stops early still writes its manifest: that is the evidence of the block.

This module never reads or writes the database and never changes a source flag or gate.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx

from suv_deals.adapters.base import FetchPurpose
from suv_deals.adapters.crawl4ai_client import DEFAULT_USER_AGENT, Crawl4AIClient
from suv_deals.clock import Clock, ensure_utc
from suv_deals.crawling.rate_limits import effective_delay_seconds
from suv_deals.crawling.robots import (
    RobotsAvailability,
    RobotsFetchResponse,
    RobotsRevision,
    crawl_delay,
    evaluate,
    fetch_robots,
)
from suv_deals.crawling.url_policy import PolicyDenied, SourceUrlPolicy
from suv_deals.domain.sources import SourceConfig
from suv_deals.netguard import Resolver, system_resolver

CAPTURE_USER_AGENT: Final = DEFAULT_USER_AGENT
DEFAULT_TIMEOUT_S: Final = 45.0
DEFAULT_MAX_BYTES: Final = 8_000_000
MAX_DETAIL_PAGES: Final = 10
MAX_REDIRECT_HOPS: Final = 2  # same-host hops only; each hop is checked like a request of its own
PageFetcher = Callable[[str], Awaitable[RobotsFetchResponse]]
Sleep = Callable[[float], Awaitable[None]]
TRUNCATED: Final = "truncated"

_HEAD_BYTES: Final = 64 * 1024
_MAX_CLASSES: Final = 300
_MAX_ATTRIBUTES: Final = 200
_MAX_JSONLD: Final = 20
_MAX_LINKS: Final = 5000
_MAX_SCRIPTS: Final = 40
_MAX_JSONLD_SHAPE: Final = 300
_MAX_QUERY_KEYS: Final = 40
_QUERY_STRING: Final = re.compile(
    r"^[A-Za-z0-9_.~-]{1,40}=[A-Za-z0-9_.~,%+-]{0,80}(?:&[A-Za-z0-9_.~-]{1,40}=[A-Za-z0-9_.~,%+-]{0,80}){0,11}$"
)
_QUERY_KEY: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")
_DETAIL_PREFIX: Final = re.compile(r"^\^/([A-Za-z0-9_-]+)/")
_VEHICLE_TYPES: Final = frozenset({"Car", "Vehicle", "Product"})
_MAX_LINK_SHAPES: Final = 40
_BLOCK_STATUSES: Final = frozenset({401, 403, 429})
_TITLE: Final = re.compile(rb"<title[^>]*>(.*?)</title>", re.DOTALL)
# Title phrases of block and challenge pages (matched on the lower-cased title).
_CHALLENGE_TITLES: Final = (
    "just a moment",
    "attention required",
    "access denied",
    "you have been blocked",
    "are you a robot",
    "verify you are human",
    "robot check",
    "unusual traffic",
    "captcha",
)
# Markers that only challenge pages carry (lower-cased bytes, whole body).
_CHALLENGE_MARKERS: Final = (
    b"cf_chl_opt",
    b"cf-chl-",
    b"captcha-delivery.com",
    b"_incapsula_resource",
    b"px-captcha",
)
# A JSON/JS escape sequence ("\u003e" in "\u003eprivacy@x.it" inside __NEXT_DATA__, "\n" in a
# script string) is matched as ONE token and written back unchanged, so an address match never
# starts inside an escape: masking must never eat the escape's letters and leave a dangling
# backslash (the saved page would no longer parse). One escape per match keeps the scan linear
# (a long run of escapes, such as escaped CJK text, is consumed token by token, never rescanned).
_ESCAPE: Final = rb"\\u[0-9A-Fa-f]{4}|\\x[0-9A-Fa-f]{2}|\\[\\/\"'bfnrt]"
_EMAIL: Final = re.compile(
    rb"(?P<escape>" + _ESCAPE + rb")"
    rb"|(?P<address>[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5})"
)
_PHONE_NUMBER: Final = rb"\+\d{1,3}[\s().\-/]{0,3}(?:\d[\s().\-/]{0,3}){6,13}\d"
# A number right after an escape ("\u003cbr\u003e+39 ..." or "\n+39 ..." in embedded JSON) follows
# the escape's last letter, which the word-boundary lookbehind would take for part of a word: the
# escape is matched first, kept, and a number directly after it is masked too.
_PHONE: Final = re.compile(
    rb"(?P<escape>" + _ESCAPE + rb")(?P<after>" + _PHONE_NUMBER + rb")?"
    rb"|(?<![\w+])(?P<phone>" + _PHONE_NUMBER + rb")"
)
# The value stops at a backslash: inside JSON (href=\"tel:...\") the closing quote is escaped, and
# swallowing its backslash would end the JSON string early. \uXXXX escapes inside the value are
# masked with it.
_TEL: Final = re.compile(rb"tel:(?:\\u[0-9A-Fa-f]{4}|[^\\\"'\s<>]){1,40}", re.IGNORECASE)
_ABSOLUTE_URL: Final = re.compile(r"https?://([a-z0-9.-]{3,253})(/[^\s\"'<>]*)?", re.IGNORECASE)
_RELATIVE_PATH: Final = re.compile(r"(?<=[\s\"'(=>,])(/[^\s\"'<>]*)")
_JSON_TYPE: Final = re.compile(r'"@type"\s*:\s*"([A-Za-z]{2,40})"')
_ID_TAIL: Final = re.compile(r"(?:^|-)\d{5,}$")


class CaptureRefused(Exception):
    """Refused before or during a run (configuration or reviewed-path gate). Names the reason only."""


@dataclass
class SampleStep:
    """One request of the run. ``size``, ``sha256`` and ``file`` describe the SAVED bytes."""

    label: str
    url: str  # without query string
    status: int | None
    size: int = 0
    truncated: bool = False
    location: str | None = None  # redirect target without query string (3xx only)
    challenge: str | None = None
    error: str | None = None
    sha256: str | None = None
    file: str | None = None

    def stop_kind(self) -> str | None:
        """Why this answer ends the run, or None when the run may continue."""
        if self.error is not None:
            return "policy_denied" if self.error.startswith("policy:") else "network_error"
        if self.status is None:
            return "network_error"
        if self.challenge is not None or self.status in _BLOCK_STATUSES:
            return "access_blocked"
        if 300 <= self.status <= 399:
            return "redirect_not_followed"
        if not 200 <= self.status <= 299:
            return "http_error"
        return None


@dataclass
class CaptureOutcome:
    source_key: str
    host: str
    robots_status: int | None = None
    robots_availability: str = "unknown"
    robots_sha256: str | None = None
    steps: list[SampleStep] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    links_total: int | None = None  # None until the search page was captured
    search_link_shapes: dict[str, int] = field(default_factory=dict)
    search_scripts: list[dict[str, Any]] = field(default_factory=list)
    search_path_shapes: dict[str, int] = field(default_factory=dict)
    search_json_types: dict[str, int] = field(default_factory=dict)
    search_jsonld_shape: dict[str, int] = field(default_factory=dict)
    search_query: str | None = None
    jsonld_links_total: int = 0
    search_offer_stats: dict[str, Any] = field(default_factory=dict)
    search_query_keys: dict[str, int] = field(default_factory=dict)
    search_rel_next: int = 0
    detail_json_types: dict[str, dict[str, int]] = field(default_factory=dict)
    detail_misses: dict[str, int] = field(default_factory=dict)
    detail_links_matching: int = 0
    requests: int = 0
    stop_kind: str | None = None
    stop_reason: str | None = None
    directory: Path | None = None


class _Pacer:
    """Spaces requests: the first one starts at once, every later one waits ``delay`` seconds."""

    def __init__(self, sleep: Sleep, delay: Decimal) -> None:
        self._sleep = sleep
        self.delay = delay
        self.requests = 0

    async def before_request(self) -> None:
        if self.requests:
            await self._sleep(float(self.delay))
        self.requests += 1


class _OutlineParser(HTMLParser):
    """Collects structure (names and counts only) and absolute links; keeps no text."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self.tag_counts: Counter[str] = Counter()
        self.class_counts: Counter[str] = Counter()
        self.attribute_counts: Counter[str] = Counter()
        self.h1 = 0
        self.jsonld: list[dict[str, Any]] = []
        self.links: list[str] = []
        self.link_shapes: Counter[str] = Counter()
        self.scripts: list[dict[str, Any]] = []
        self.jsonld_documents: list[Any] = []
        self.rel_next = 0
        self._base_host = urlsplit(base_url).hostname or ""
        self._script_type: str | None = None
        self._script_attrs: dict[str, Any] = {}
        self._script_chars = 0
        self._script_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_counts[tag] += 1
        if tag == "h1":
            self.h1 += 1
        values = {name: value for name, value in attrs}
        for name, value in attrs:
            self.attribute_counts[name] += 1
            if name == "class" and value:
                self.class_counts.update(value.split())
        if tag in ("a", "link") and "next" in (values.get("rel") or "").lower().split():
            self.rel_next += 1
        if tag == "a" and values.get("href"):
            absolute = _absolute(self._base, values["href"] or "")
            if absolute is not None:
                self.link_shapes[_link_shape(absolute, self._base_host)] += 1
                if len(self.links) < _MAX_LINKS:
                    self.links.append(absolute)
        if tag == "script":
            self._script_type = (values.get("type") or "").strip().lower()
            self._script_attrs = {
                "type": self._script_type or None,
                "id": values.get("id"),
                "external": values.get("src") is not None,
            }
            self._script_chars = 0
            self._script_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_type is not None:
            if self._script_type == "application/ld+json":
                self._add_jsonld("".join(self._script_text))
            if len(self.scripts) < _MAX_SCRIPTS:
                self.scripts.append({**self._script_attrs, "chars": self._script_chars})
            self._script_type = None

    def handle_data(self, data: str) -> None:
        if self._script_type is None:
            return
        self._script_chars += len(data)
        if self._script_type == "application/ld+json":
            self._script_text.append(data)

    def _add_jsonld(self, text: str) -> None:
        try:
            data: Any = json.loads(text)
        except (ValueError, RecursionError):
            return
        self.jsonld_documents.append(data)
        graph = data.get("@graph") if isinstance(data, dict) else None
        items: list[Any] = graph if isinstance(graph, list) else data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and len(self.jsonld) < _MAX_JSONLD:
                self.jsonld.append(
                    {"type": _type_name(item.get("@type")), "keys": sorted(map(str, item))[:60]}
                )


def _type_name(value: object) -> str:
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, list):
        return ",".join(str(v)[:80] for v in value[:5] if isinstance(v, str))
    return ""


def _link_shape(link: str, base_host: str) -> str:
    """Where a link points, as a shape only: host kind, first path segment and depth (no slug, no query)."""
    parts = urlsplit(link)
    where = "same-host" if parts.hostname == base_host else "other-host"
    segments = [segment for segment in parts.path.split("/") if segment]
    head = f"/{segments[0]}/" if segments else "/"
    query = " +query" if parts.query else ""
    return f"{where} {head} {len(segments)} segment(s){query}"


def _absolute(base: str, href: str) -> str | None:
    try:
        parts = urlsplit(urljoin(base, href.strip()))
    except ValueError:
        return None
    if parts.scheme not in ("https", "http"):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _path_shape(path: str, kind: str) -> str:
    """A path as a shape: where it points, first segment, depth and whether the last segment ends in a
    numeric id. No slug and no query string."""
    segments = [segment for segment in path.split("?", 1)[0].split("#", 1)[0].split("/") if segment]
    head = f"/{segments[0]}/" if segments else "/"
    tail = " +id" if segments and _ID_TAIL.search(segments[-1]) else ""
    return f"{kind} {head} {len(segments)} segment(s){tail}"


def _value_kind(value: Any, base_host: str) -> str:
    """The kind of a JSON value, never the value itself; URL-like strings are reduced to their shape."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parts = urlsplit(value)
            kind = "same-host" if (parts.hostname or "") == base_host else "other-host"
            return f"url: {_path_shape(parts.path or '/', kind)}"
        if value.startswith("/"):
            return f"url: {_path_shape(value, 'relative')}"
        return "string"
    if isinstance(value, dict):
        return "object"
    return "list"


def jsonld_shape(documents: list[Any], *, base_host: str) -> Counter[str]:
    """Key names of every JSON-LD object with the kind of each value, as ``<type>.<key>: <kind>``.

    The type is the object's own ``@type`` (or the enclosing one when it has none). Keys that start
    with ``@`` are not counted, except that ``@graph`` items are walked. Values are never stored.
    """
    shapes: Counter[str] = Counter()

    def walk(value: Any, owner: str, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            name = _type_name(value.get("@type")) or owner
            for key, item in value.items():
                if key == "@graph":
                    walk(item, name, depth + 1)
                    continue
                if key.startswith("@"):
                    continue
                shapes[f"{name}.{key}: {_value_kind(item, base_host)}"] += 1
                walk(item, name, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, owner, depth + 1)

    for document in documents:
        walk(document, "root", 0)
    return shapes


def _query_keys(path: str) -> list[str]:
    query = path.split("#", 1)[0].partition("?")[2]
    return [key for key, _ in parse_qsl(query, keep_blank_values=True) if _QUERY_KEY.fullmatch(key)]


def path_forms(text: str, *, base_host: str) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """Path shapes of every URL-like path in the page (scripts, JSON and attributes included), the
    JSON ``@type`` values and the query parameter NAMES used in those URLs. Entities and escaped
    slashes are normalised first. Parameter values are never kept."""
    normalized = html.unescape(text).replace("\\/", "/")
    shapes: Counter[str] = Counter()
    query_keys: Counter[str] = Counter()
    for match in _ABSOLUTE_URL.finditer(normalized):
        host = match.group(1).lower()
        kind = "same-host" if host == base_host else "other-host"
        shapes[_path_shape(match.group(2) or "/", kind)] += 1
        if kind == "same-host":
            query_keys.update(_query_keys(match.group(2) or ""))
    for match in _RELATIVE_PATH.finditer(normalized):
        shapes[_path_shape(match.group(1), "relative")] += 1
        query_keys.update(_query_keys(match.group(1)))
    return shapes, Counter(_JSON_TYPE.findall(normalized)), query_keys


def jsonld_urls(documents: list[Any], *, base_url: str) -> list[str]:
    """Absolute http(s) URLs found as string values in JSON-LD documents, in document order."""
    found: list[str] = []

    def walk(value: Any, depth: int) -> None:
        if depth > 10 or len(found) >= _MAX_LINKS:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key != "@context":
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, str) and value.startswith(("/", "http://", "https://")):
            absolute = _absolute(base_url, value)
            if absolute is not None:
                found.append(absolute)

    for document in documents:
        walk(document, 0)
    return found


def offer_stats(documents: list[Any]) -> dict[str, Any]:
    """Aggregates over the JSON-LD offers of a page: counts, minimum and maximum, and how often each
    model and brand name occurs.

    Used to check that a search filter took effect (all prices inside the asked band, only SUV
    models after a body filter) without keeping any single listing's values.
    """
    prices: list[float] = []
    currencies: Counter[str] = Counter()
    mileages: list[float] = []
    models: Counter[str] = Counter()
    brands: Counter[str] = Counter()

    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    def walk(value: Any, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(value, dict):
            if "Offer" in _type_name(value.get("@type")).split(","):
                price = number(value.get("price"))
                if price is not None:
                    prices.append(price)
                    currency = value.get("priceCurrency")
                    if isinstance(currency, str) and currency:
                        currencies[currency.upper()[:3]] += 1
            if set(_type_name(value.get("@type")).split(",")) & _VEHICLE_TYPES:
                model = value.get("model")
                if isinstance(model, str) and model.strip():
                    models[model.strip()[:40]] += 1
                brand = value.get("brand")
                brand_name = brand.get("name") if isinstance(brand, dict) else brand
                if isinstance(brand_name, str) and brand_name.strip():
                    brands[brand_name.strip()[:40]] += 1
            odometer = value.get("mileageFromOdometer")
            if isinstance(odometer, dict) and str(odometer.get("unitCode", "")).upper() in {"KMT", "KM"}:
                km = number(odometer.get("value"))
                if km is not None:
                    mileages.append(km)
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    for document in documents:
        walk(document, 0)
    return {
        "offers_with_price": len(prices),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "currency": currencies.most_common(1)[0][0] if currencies else None,
        "mileages_km": len(mileages),
        "mileage_km_min": min(mileages) if mileages else None,
        "mileage_km_max": max(mileages) if mileages else None,
        "models": dict(models.most_common(15)),
        "brands": dict(brands.most_common(10)),
    }


def outline_html(body: bytes, *, base_url: str) -> tuple[dict[str, Any], list[str]]:
    """Structure of one page (names and counts) and its absolute links: anchors in document order,
    then the URLs found in JSON-LD (``links_total`` counts anchors only)."""
    text = body.decode("utf-8", errors="replace")
    parser = _OutlineParser(base_url)
    parser.feed(text)
    parser.close()
    shapes, json_types, query_keys = path_forms(text, base_host=urlsplit(base_url).hostname or "")
    jsonld_links = jsonld_urls(parser.jsonld_documents, base_url=base_url)
    outline: dict[str, Any] = {
        "tags": dict(parser.tag_counts.most_common()),
        "classes": dict(parser.class_counts.most_common(_MAX_CLASSES)),
        "attributes": dict(parser.attribute_counts.most_common(_MAX_ATTRIBUTES)),
        "h1": parser.h1,
        "jsonld": parser.jsonld,
        "links_total": len(parser.links),
        "link_shapes": dict(parser.link_shapes.most_common(_MAX_LINK_SHAPES)),
        "path_shapes": dict(shapes.most_common(_MAX_LINK_SHAPES)),
        "json_types": dict(json_types.most_common(_MAX_LINK_SHAPES)),
        "jsonld_shape": dict(
            jsonld_shape(parser.jsonld_documents, base_host=urlsplit(base_url).hostname or "").most_common(
                _MAX_JSONLD_SHAPE
            )
        ),
        "scripts": parser.scripts,
        "jsonld_links_total": len(jsonld_links),
        "offer_stats": offer_stats(parser.jsonld_documents),
        "query_keys": dict(query_keys.most_common(_MAX_QUERY_KEYS)),
        "rel_next": parser.rel_next,
    }
    return outline, parser.links + jsonld_links


def redact_html(body: bytes) -> bytes:
    """Mask e-mail addresses, international phone numbers and ``tel:`` links before saving.

    An escape sequence right before an address or a phone number (``\\u003e`` in embedded JSON) is
    kept intact (the number after it is still masked), and a ``tel:`` value never takes the
    backslash of an escaped closing quote, so masking never turns valid JSON or script text into an
    invalid escape.
    """
    masked = _EMAIL.sub(lambda m: m.group("escape") or b"[redacted-email]", body)
    masked = _PHONE.sub(_mask_phone, masked)
    return _TEL.sub(b"tel:[redacted]", masked)


def _mask_phone(match: re.Match[bytes]) -> bytes:
    escape = match.group("escape")
    if escape is None:
        return b"[redacted-phone]"
    return escape + (b"[redacted-phone]" if match.group("after") is not None else b"")


def challenge_reason(body: bytes) -> str | None:
    """A reason when a 2xx body is a block or challenge page, else None."""
    match = _TITLE.search(body[:_HEAD_BYTES].lower())
    title = match.group(1).decode("utf-8", errors="replace") if match else ""
    for marker in _CHALLENGE_TITLES:
        if marker in title:
            return f"title:{marker}"
    lowered = body.lower()
    for raw_marker in _CHALLENGE_MARKERS:
        if raw_marker in lowered:
            return f"marker:{raw_marker.decode('ascii')}"
    return None


def _public_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _single_host(cfg: SourceConfig) -> str:
    if len(cfg.allowed_hosts) != 1 or cfg.allowed_hosts[0].startswith("*."):
        raise CaptureRefused(f"{cfg.source_key} needs exactly one exact allowed host in config")
    return cfg.allowed_hosts[0].lower()


def _path_refusal(cfg: SourceConfig, search_path: str, reason: str) -> str:
    if not cfg.allowed_search_paths:
        return (
            f"{cfg.source_key} has no reviewed search path yet (allowed_search_paths is empty); "
            "a path must be reviewed in config before it can be captured"
        )
    return (
        f"{search_path} is not in the reviewed allowed_search_paths of {cfg.source_key} ({reason}); "
        "nothing was requested"
    )


def _char_classes(segment: str) -> str:
    classes: set[str] = set()
    for char in segment:
        if "a" <= char <= "z":
            classes.add("lower")
        elif "A" <= char <= "Z":
            classes.add("upper")
        elif "0" <= char <= "9":
            classes.add("digit")
        else:
            classes.add({"-": "hyphen", ".": "dot", "_": "underscore", "%": "percent"}.get(char, "other"))
    return ",".join(sorted(classes))


def detail_prefixes(cfg: SourceConfig) -> frozenset[str]:
    """The literal first path segment of each reviewed detail pattern (``^/annunci/...`` -> annunci)."""
    return frozenset(
        match.group(1) for pattern in cfg.allowed_detail_paths if (match := _DETAIL_PREFIX.match(pattern))
    )


def _detail_candidates(
    links: Iterable[str], policy: SourceUrlPolicy, prefixes: frozenset[str] = frozenset()
) -> tuple[list[str], Counter[str]]:
    """Links the reviewed detail pattern accepts, and the shapes of same-host links under a detail
    prefix that it refuses (reason and character classes of the last segment; never the slug)."""
    seen: set[str] = set()
    accepted: list[str] = []
    misses: Counter[str] = Counter()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        try:
            policy.check(link, "detail")
        except PolicyDenied as exc:
            parts = urlsplit(link)
            segments = [segment for segment in parts.path.split("/") if segment]
            if segments and segments[0] in prefixes and policy.host_allowed(parts.hostname or ""):
                trailing = " trailing-slash" if parts.path.endswith("/") else ""
                misses[
                    f"/{segments[0]}/ {len(segments)} segment(s) {exc.reason.value} "
                    f"[{_char_classes(segments[-1])}]{trailing}"
                ] += 1
            continue
        accepted.append(link)
    return accepted, misses


async def _fetch(
    label: str,
    url: str,
    purpose: FetchPurpose,
    *,
    policy: SourceUrlPolicy,
    pacer: _Pacer,
    fetcher: PageFetcher,
    resolver: Resolver,
) -> tuple[SampleStep, bytes | None, str | None]:
    """One policy-checked request.

    Returns the step, the body (only for a 2xx page that is not a challenge) and, for a 3xx answer
    with a Location, the absolute redirect target. The caller decides whether to follow it.
    """
    public = _public_url(url)
    try:
        await policy.resolve_check(url, purpose, resolver)
    except PolicyDenied as exc:
        return (
            SampleStep(label=label, url=public, status=None, error=f"policy:{exc.reason.value}"),
            None,
            None,
        )
    await pacer.before_request()
    response = await fetcher(url)
    body = response.body if response.body is not None else b""
    step = SampleStep(
        label=label,
        url=public,
        status=response.status,
        size=len(body),
        truncated=response.error_kind == TRUNCATED,
    )
    if response.status is None or response.error_kind not in (None, TRUNCATED):
        step.error = response.error_kind or "no_response"
        return step, None, None
    if 300 <= response.status <= 399:
        target = _absolute(url, response.location) if response.location else None
        step.location = _public_url(target) if target is not None else None
        return step, None, target
    if not 200 <= response.status <= 299:
        return step, None, None
    step.challenge = challenge_reason(body)
    return step, (None if step.challenge is not None else body), None


class _Run:
    """One capture run: its requests, the pages it keeps, and the decision to stop."""

    def __init__(
        self,
        cfg: SourceConfig,
        *,
        policy: SourceUrlPolicy,
        fetcher: PageFetcher,
        resolver: Resolver,
        sleep: Sleep,
        clock: Clock,
        user_agent: str,
    ) -> None:
        self.cfg = cfg
        self.host = _single_host(cfg)
        self.policy = policy
        self.fetcher = fetcher
        self.resolver = resolver
        self.clock = clock
        self.user_agent = user_agent
        self.pacer = _Pacer(sleep, Decimal(cfg.rate_budget.min_delay_seconds))
        self.outcome = CaptureOutcome(source_key=cfg.source_key, host=self.host)
        self.pages: list[tuple[str, bytes, SampleStep]] = []
        self.outlines: dict[str, dict[str, Any]] = {}
        self.revision: RobotsRevision | None = None

    def _stop(self, kind: str, reason: str) -> None:
        self.outcome.stop_kind = kind
        self.outcome.stop_reason = reason

    async def _robots_fetch(self, url: str) -> RobotsFetchResponse:
        await self.pacer.before_request()
        return await self.fetcher(url)

    def _redirect_refusal(self, current: str, target: str, purpose: FetchPurpose) -> str | None:
        """Why a redirect target may not be followed, or None when it may (same host, reviewed, allowed)."""
        try:
            self.policy.validate_redirect(current, target, purpose)
        except PolicyDenied as exc:
            return f"the URL policy refuses it ({exc.reason.value})"
        if (
            self.revision is not None
            and not evaluate(self.revision, target, self.user_agent, now=self.clock.now()).allowed
        ):
            return "robots.txt disallows it"
        return None

    async def _get(self, label: str, url: str, purpose: FetchPurpose) -> bytes | None:
        current = url
        for hop in range(MAX_REDIRECT_HOPS + 1):
            hop_label = label if hop == 0 else f"{label}-redirect-{hop}"
            step, body, target = await _fetch(
                hop_label,
                current,
                purpose,
                policy=self.policy,
                pacer=self.pacer,
                fetcher=self.fetcher,
                resolver=self.resolver,
            )
            self.outcome.steps.append(step)
            if target is not None:
                if hop == MAX_REDIRECT_HOPS:
                    self._stop("redirect_refused", f"{label} redirected more than {MAX_REDIRECT_HOPS} times")
                    return None
                refusal = self._redirect_refusal(current, target, purpose)
                if refusal is not None:
                    self._stop("redirect_refused", f"{label} redirect not followed: {refusal}")
                    return None
                current = target
                continue
            kind = step.stop_kind()
            if kind is not None:
                self._stop(kind, _describe_stop(step, kind))
                return None
            if body is not None:
                self.pages.append((label, body, step))
            return body
        return None

    async def run(self, search_path: str, detail_limit: int, query: str | None = None) -> None:
        revision = await fetch_robots(
            self.policy, self.host, self._robots_fetch, resolver=self.resolver, clock=self.clock
        )
        self.outcome.robots_status = revision.http_status
        self.outcome.robots_availability = revision.availability.value
        self.outcome.robots_sha256 = revision.content_hash
        self.revision = revision
        if revision.availability == RobotsAvailability.UNREACHABLE:
            self._stop("robots_unreachable", "robots.txt could not be read, so nothing else was requested")
            return
        target = f"{search_path}?{query}" if query else search_path
        search_url = f"https://{self.host}{target}"
        if not evaluate(revision, search_url, self.user_agent, now=self.clock.now()).allowed:
            self._stop("robots_disallowed", f"robots.txt disallows {target}; nothing else was requested")
            return
        self.pacer.delay = effective_delay_seconds(
            self.cfg.rate_budget, crawl_delay(revision, self.user_agent)
        )

        body = await self._get("search", search_url, "search")
        if body is None:
            return
        outline, links = outline_html(body, base_url=search_url)
        self.outcome.links_total = int(outline["links_total"])
        self.outcome.jsonld_links_total = int(outline["jsonld_links_total"])
        self.outcome.search_offer_stats = dict(outline["offer_stats"])
        self.outcome.search_query_keys = dict(outline["query_keys"])
        self.outcome.search_rel_next = int(outline["rel_next"])
        self.outcome.search_link_shapes = dict(outline["link_shapes"])
        self.outcome.search_scripts = list(outline["scripts"])
        self.outcome.search_path_shapes = dict(outline["path_shapes"])
        self.outcome.search_json_types = dict(outline["json_types"])
        self.outcome.search_jsonld_shape = dict(outline["jsonld_shape"])
        candidates, misses = _detail_candidates(links, self.policy, detail_prefixes(self.cfg))
        outline["detail_links_matching"] = len(candidates)
        outline["detail_misses"] = dict(misses.most_common(10))
        self.outcome.detail_misses = dict(misses.most_common(10))
        self.outlines["search"] = outline
        self.outcome.detail_links_matching = len(candidates)

        if self.cfg.detail_mode == "card_only":
            self.outcome.skipped.append("detail pages not requested: detail_mode is card_only")
            return
        if not self.cfg.allowed_detail_paths:
            self.outcome.skipped.append("detail pages not requested: no reviewed detail path")
            return
        budget = min(detail_limit, self.cfg.rate_budget.max_detail_jobs_per_run, MAX_DETAIL_PAGES)
        if budget == 0:
            self.outcome.skipped.append("detail pages not requested: --detail-limit is 0")
            return
        fetched = 0
        for url in candidates:
            if fetched >= budget:
                break
            if not evaluate(revision, url, self.user_agent, now=self.clock.now()).allowed:
                self.outcome.skipped.append(f"detail link skipped: robots.txt disallows {_public_url(url)}")
                continue
            fetched += 1
            label = f"detail-{fetched}"
            detail_body = await self._get(label, url, "detail")
            if detail_body is None:
                return
            detail_outline = outline_html(detail_body, base_url=url)[0]
            self.outlines[label] = detail_outline
            self.outcome.detail_json_types[label] = dict(detail_outline["json_types"])


def _describe_stop(step: SampleStep, kind: str) -> str:
    if step.challenge is not None:
        detail = f"a challenge page ({step.challenge})"
    elif step.status is not None:
        detail = f"HTTP {step.status}"
    else:
        detail = step.error or "no response"
    return f"{step.label} answered {detail} ({kind}); the run stopped"


def _write(
    run: _Run, *, out_root: Path, search_path: str, detail_limit: int, query: str | None = None
) -> None:
    outcome = run.outcome
    stamp = ensure_utc(run.clock.now())
    directory = out_root / run.cfg.source_key / stamp.strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=False)
    for label, body, step in run.pages:
        saved = redact_html(body)
        (directory / f"{label}.html").write_bytes(saved)
        step.file = f"{label}.html"
        step.size = len(saved)
        step.sha256 = hashlib.sha256(saved).hexdigest()
    if run.revision is not None and run.revision.body is not None:
        (directory / "robots.txt").write_text(run.revision.body, encoding="utf-8")
    (directory / "structure.json").write_text(
        json.dumps(run.outlines, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "source_key": run.cfg.source_key,
        "host": run.host,
        "captured_at": stamp.isoformat(),
        "user_agent": run.user_agent,
        "search_path": search_path,
        "search_query": query,
        "detail_limit": detail_limit,
        "robots": {
            "file": "robots.txt" if run.revision is not None and run.revision.body is not None else None,
            "http_status": outcome.robots_status,
            "availability": outcome.robots_availability,
            "content_sha256": outcome.robots_sha256,
        },
        "requests": outcome.requests,
        "steps": [asdict(step) for step in outcome.steps],
        "skipped": outcome.skipped,
        "stop": {"kind": outcome.stop_kind, "reason": outcome.stop_reason} if outcome.stop_kind else None,
        "saved_pages_are_masked": True,
        "note": (
            "Local parser-development material. Do not publish or commit the pages; see "
            "docs/research/source_terms_review_2026-10-14.md."
        ),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    outcome.directory = directory


async def capture_samples(
    cfg: SourceConfig,
    *,
    search_path: str,
    detail_limit: int,
    out_root: Path,
    fetcher: PageFetcher,
    clock: Clock,
    sleep: Sleep,
    resolver: Resolver = system_resolver,
    user_agent: str = CAPTURE_USER_AGENT,
    query: str | None = None,
) -> CaptureOutcome:
    """Capture the reviewed pages of one source (every guard is described in the module docstring).

    Raises `CaptureRefused` before any request when the request itself is outside the reviewed
    configuration. Every later problem (robots, blocks, network) ends the run and is reported in
    the outcome and in the manifest.
    """
    host = _single_host(cfg)
    if not 0 <= detail_limit <= MAX_DETAIL_PAGES:
        raise CaptureRefused(f"--detail-limit must be between 0 and {MAX_DETAIL_PAGES}")
    if not search_path.startswith("/") or "?" in search_path or "#" in search_path:
        raise CaptureRefused("the search path must start with / and carry no query string or fragment")
    if query is not None and (len(query) > 400 or not _QUERY_STRING.fullmatch(query)):
        raise CaptureRefused(
            "the query must be key=value pairs joined by & (letters, digits and . _ ~ - , % + only)"
        )
    policy = SourceUrlPolicy.from_source(cfg)
    try:
        policy.check(f"https://{host}{search_path}", "search")
    except PolicyDenied as exc:
        raise CaptureRefused(_path_refusal(cfg, search_path, exc.reason.value)) from None
    run = _Run(
        cfg,
        policy=policy,
        fetcher=fetcher,
        resolver=resolver,
        sleep=sleep,
        clock=clock,
        user_agent=user_agent,
    )
    run.outcome.search_query = query
    await run.run(search_path, detail_limit, query)
    run.outcome.requests = run.pacer.requests
    _write(run, out_root=out_root, search_path=search_path, detail_limit=detail_limit, query=query)
    return run.outcome


@asynccontextmanager
async def httpx_page_fetcher(
    *,
    user_agent: str = CAPTURE_USER_AGENT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> AsyncIterator[PageFetcher]:
    """The real fetcher: HTTPS, no redirects followed, bodies capped at ``max_bytes``."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "Accept-Language": "it,de,en;q=0.5",
    }
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout_s, headers=headers) as client:

        async def fetch(url: str) -> RobotsFetchResponse:
            try:
                async with client.stream("GET", url) as response:
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if size + len(chunk) > max_bytes:
                            chunks.append(chunk[: max_bytes - size])
                            truncated = True
                            break
                        size += len(chunk)
                        chunks.append(chunk)
                    return RobotsFetchResponse(
                        response.status_code,
                        b"".join(chunks),
                        TRUNCATED if truncated else None,
                        response.headers.get("location"),
                    )
            except (httpx.HTTPError, ValueError):
                return RobotsFetchResponse(None, None, "network")

        yield fetch


@asynccontextmanager
async def crawl4ai_page_fetcher(
    client: Crawl4AIClient,
    *,
    source_key: str,
    robots_fetcher: PageFetcher,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> AsyncIterator[PageFetcher]:
    """Pages through the self-hosted Crawl4AI browser (headless, honest User-Agent, no stealth).

    robots.txt is still fetched directly (``robots_fetcher``): the crawler renders pages, not plain
    files. The browser follows redirects itself, so a final URL on another host is reported as a
    redirect (never kept); a crawler-side failure is a network error, never a source block.
    """

    async def fetch(url: str) -> RobotsFetchResponse:
        if urlsplit(url).path == "/robots.txt":
            return await robots_fetcher(url)
        result = await client.fetch_detailed(url, purpose="search", source_key=source_key)
        document = result.document
        outcome = document.fetch
        if outcome.infrastructure_failure or outcome.http_status is None:
            return RobotsFetchResponse(None, None, f"crawler:{outcome.error_code or 'no_response'}")
        final = document.final_url
        if final and (urlsplit(final).hostname or "").lower() != (urlsplit(url).hostname or "").lower():
            return RobotsFetchResponse(307, None, None, final)
        body = (document.html or "").encode("utf-8")
        truncated = len(body) > max_bytes
        return RobotsFetchResponse(
            outcome.http_status, body[:max_bytes], TRUNCATED if truncated else None, None
        )

    yield fetch


__all__ = [
    "CAPTURE_USER_AGENT",
    "CaptureOutcome",
    "CaptureRefused",
    "PageFetcher",
    "SampleStep",
    "capture_samples",
    "challenge_reason",
    "crawl4ai_page_fetcher",
    "httpx_page_fetcher",
    "outline_html",
    "redact_html",
]
