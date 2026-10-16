"""AutoScout24 Italy and Germany public listings (spec sections 5, 7, 8, 9, 10, 24, 25).

One adapter class, parameterised per country (each country is registered and gated separately).
Every structural fact below was read from owner-captured pages (saved 2026-10-15/16, structure
only; no page content is kept in the repository):

- Search pages ``https://www.autoscout24.<tld>/lst/<make-slug>/<model-slug>`` are the Next.js app
  ``as24-search-funnel``. ``<script id="__NEXT_DATA__" type="application/json">`` holds
  ``props.pageProps`` with ``numberOfResults``, ``numberOfPages``, ``pageQuery`` (the server's
  normalised query) and ``listings`` (20 per page). Each listing has ``id`` (a UUID that also ends
  its ``url``: ``/annunci/<slug>-<uuid>`` in Italy, ``/angebote/<slug>-<uuid>`` in Germany; slugs
  may contain ``+``), ``title``, ``price.priceRaw`` (integer EUR), ``vehicle`` (make, model,
  modelGroup, localised transmission and fuel, ``mileageInKm`` text, ``engineDisplacementInCCM``
  text, ``offerType``), ``tracking`` (``firstRegistration`` MM-YYYY, ``fuelType`` code, ``mileage``
  and ``price`` digits), ``vehicleDetails`` pills (``speedometer`` "81 kW (110 CV)") and a
  ``seller`` object. Only ``searchResultType == "Organic"`` and ``searchResultSection == "Main"``
  cards are results. No drive, body type or listing date is published on a card.
- Italian detail pages ``/annunci/<slug>-<uuid>`` publish ``props.pageProps.listingDetails`` with
  language-neutral raw values (``vehicle.rawPowerInKw``, ``rawDisplacementInCCM``,
  ``firstRegistrationDateRaw``, ``fuelCategory.raw``, ``rawData.engine.transmissionType.raw``,
  ``driveTrain``) and a JSON-LD ``Product`` whose ``offers.itemOffered`` is the ``Car`` (read with
  the shared schema.org reader as a fallback and a cross-check).

Personal data is never read into any model: of the ``seller`` object only ``type`` / ``isDealer``
is used and of ``location`` only ``countryCode``. Names, phones, links, streets and coordinates
are skipped at parse time. Titles and version texts are untrusted seller text (bounded, markup
removed, injection-like wording flagged, never interpreted).

Discovery walks the configured models with an opaque cursor (``model:<n>``, ``:partial`` once
any earlier model page was incomplete, ``:nf<k>`` after k models answered HTTP 404): one search
page per model per run, newest first, with the profile's price band and mileage ceiling as site
filters. A genuine 404 skips an unknown model slug; a 404 of the slug verified on the captures
(``renault/captur``) or of every configured model is route drift, and so is a result page whose
listings carry no Organic/Main card at all. The source's per-run search-page
budget must cover every model; the shared rate limiter keeps its per-host spacing between them.
Detail pages are fetched for Italy only: the German site's robots.txt disallows ``/angebote/``,
so that source is ``card_only`` and its detail URLs are identity only, never requested.

Search config keys (``SourceConfig.search``):
- ``models`` (required for discovery): ``<make-slug>/<model-slug>`` entries separated by spaces
  or commas, searched in that order.
- ``sort`` (default ``age``) and ``desc`` (default ``1``): the site's sort key and direction.
- ``ustate`` (optional): the site's offer-state filter (``U`` = used), sent only when configured.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, ClassVar, Final
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from suv_deals.adapters._access import AccessClassification, classify_document, classify_status_only
from suv_deals.adapters._extract import (
    PageView,
    bounded,
    collapse_ws,
    decimal_from_json,
    find_power,
    html_to_text,
    injection_signals,
    local_type_names,
    parse_locale_number,
    parse_page,
    parse_partial_date,
    parse_source_timestamp,
    plain,
    power_from_value,
    scalar,
    text_of,
)
from suv_deals.adapters._health import assess_samples
from suv_deals.adapters._policy import UrlPolicy, build_identity
from suv_deals.adapters._vocabulary import drive_word, fuel_word, gearbox_word
from suv_deals.adapters.base import (
    CanonicalIdentity,
    CrawlClient,
    DiscoveryPage,
    FetchOutcome,
    PageType,
    ParsedListing,
    ParseOutcome,
    ParserHealth,
    RawDocument,
    SearchObservation,
    SearchRequest,
    SourceCapabilities,
)
from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.enums import (
    AccessState,
    Availability,
    BodyType,
    ClaimStatus,
    Co2Cycle,
    Completeness,
    Confidence,
    CoverageMode,
    Drive,
    ExtractionMethod,
    Fuel,
    Gearbox,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    SellerType,
    SourceMode,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.identity import card_hash
from suv_deals.domain.listings import (
    Co2Info,
    ConditionClaims,
    Documentation,
    LocationInfo,
    MileageOriginal,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    SourceTimestamp,
    VehicleSpec,
)
from suv_deals.domain.profiles import SearchProfile
from suv_deals.domain.provenance import FieldConflict, FieldProvenance
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import ValidationFailed

ADAPTER_VERSION: Final = "autoscout24@1.0.0"

_SEARCH_KEYS: Final = frozenset({"models", "sort", "desc", "ustate"})
#: Sort keys of the sites' own vocabulary (``taxonomy.sortingKeys`` on the captured pages).
_SORT_KEYS: Final = frozenset({"standard", "age", "price", "year", "mileage", "power"})
_SLUG: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_USTATE: Final = re.compile(r"[A-Z](?:,[A-Z]){0,5}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_URL_UUID_TAIL: Final = re.compile(r"-([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})/?$")
_CURSOR: Final = re.compile(r"model:(\d{1,3})(:partial)?(?::nf(\d{1,3}))?")
#: Model slugs whose search route was verified on owner captures (IT and DE, 2026-10-15/16): a 404
#: there is route drift, not an unknown model.
VERIFIED_MODEL_SLUGS: Final = frozenset({"renault/captur"})
_NEXT_DATA: Final = re.compile(
    r"<script\b[^>]{0,300}\bid\s*=\s*[\"']__NEXT_DATA__[\"'][^>]{0,300}>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MAX_HTML: Final = 8_000_000
_MAX_NEXT_DATA: Final = 5_000_000
_KW: Final = re.compile(r"(?<![\d.,])(\d{1,4})\s?kW\b", re.IGNORECASE)
_CCM: Final = re.compile(r"([\d.,'\u00a0\u202f]{1,9})\s?(?:cm³|cm3|ccm)", re.IGNORECASE)
_DIGITS: Final = re.compile(r"\d{1,9}")
_NUMBER_IN_TEXT: Final = re.compile(r"\d{1,3}(?:[.,'\u00a0\u202f]\d{3})+|\d+")

#: ``tracking.fuelType`` (cards) and ``fuelCategory.raw`` (detail) codes, from the sites' own
#: ``taxonomy.fuelType`` vocabulary: 2 = Elettrica/Benzina, 3 = Elettrica/Diesel, B Benzina,
#: C Metano, D Diesel, E Elettrica, H Idrogeno, L GPL; M and O are other fuels.
FUEL_CODES: Final = {
    "b": Fuel.PETROL,
    "d": Fuel.DIESEL,
    "l": Fuel.LPG,
    "c": Fuel.CNG,
    "e": Fuel.ELECTRIC,
    "2": Fuel.HYBRID_PETROL,
    "3": Fuel.HYBRID_DIESEL,
    "h": Fuel.OTHER,
    "m": Fuel.OTHER,
    "o": Fuel.OTHER,
}
_HYBRID_CODES: Final = frozenset({"2", "3"})
#: ``rawData.bodyType.raw`` on detail pages. AutoScout24 lumps SUV, off-road and pick-up into one
#: category ("SUV/Fuoristrada/Pick-up"), read as SUV at low confidence.
_BODY_RAW: Final = {
    "offroad": BodyType.SUV,
    "compact": BodyType.HATCHBACK,
    "sedan": BodyType.SEDAN,
    "stationwagon": BodyType.ESTATE,
    "van": BodyType.VAN,
    "transporter": BodyType.VAN,
    "convertible": BodyType.OTHER,
    "coupe": BodyType.OTHER,
}
_BODY_TEXT: Final = re.compile(r"\bsuv\b", re.IGNORECASE)
#: Sanity bounds (ENGINEERING DEFAULTS) for card and detail prices in EUR, not business rules.
MIN_PLAUSIBLE_PRICE_EUR: Final = 100
MAX_PLAUSIBLE_PRICE_EUR: Final = 1_000_000
#: Typed-in placeholder amounts that are never a real asking price.
_PLACEHOLDER_PRICES: Final = frozenset({1234, 12345, 123456, 1234567, 11111, 111111, 1111111})
#: Seller wording that states the advertised price excludes VAT (Italian and German). Only the flag
#: is kept; the description itself is never stored (it carries the dealer's contact details).
_NET_WORDING: Final = re.compile(
    r"al\s+netto\s+(?:dell['\u2019\s]?\s*)?iva|iva\s+esclusa|esclusa\s+iva|oltre\s+(?:l['\u2019]\s*)?iva|"
    r"pi[uù]\s+iva|\+\s*iva\b|zzgl\.?\s*(?:\d{1,2}\s*%\s*)?(?:mwst|ust)|netto\s*preis|nettopreis",
    re.IGNORECASE,
)
#: Commercial (N1) registration markers in the version text or description ("AUTOCARRO", "N1",
#: "LKW-Zulassung"): flagged for the owner, the vehicle is still parsed.
_COMMERCIAL: Final = re.compile(r"\bautocarro\b|\bN1\b|\blkw[- ]zulassung\b", re.IGNORECASE)
_SELLER_TYPES: Final = {
    "dealer": SellerType.DEALER,
    "private": SellerType.PRIVATE,
    "privat": SellerType.PRIVATE,
    "privato": SellerType.PRIVATE,
}
_COUNTRY: Final = re.compile(r"[A-Z]{2}")
_SEARCH_PROV: Final = "next_data:pageProps.listings[]"
_DETAIL_PROV: Final = "next_data:pageProps.listingDetails"


# --------------------------------------------------------------------------- small helpers


def _get(node: Any, *path: str) -> Any:
    """``node[path[0]][path[1]]...`` for dicts, else None."""
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    if isinstance(value, str):
        text = collapse_ws(value)
        return text or None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        return int(value.strip())
    return None


def name_key(text: str) -> str:
    """Comparison key of a make/model label or slug: accents removed, letters and digits only."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in decomposed if not unicodedata.combining(c)))


def _clean_text(raw: str | None, limit: int) -> tuple[str | None, bool]:
    """Bounded plain text of untrusted seller text and whether it looked suspicious."""
    if not raw:
        return None, False
    cleaned = html_to_text(raw)
    text = bounded(collapse_ws(cleaned.text), limit)
    return text, cleaned.active_markup or bool(injection_signals(cleaned.text))


def _cursor_index(cursor: str | None) -> tuple[int, bool, int]:
    """``(model index, an earlier model page was incomplete, earlier models that answered 404)``."""
    if cursor is None:
        return 0, False, 0
    match = _CURSOR.fullmatch(cursor)
    if match is None:
        raise ValidationFailed("AutoScout24 discovery cursor is not a model cursor")
    return int(match.group(1)), match.group(2) is not None, int(match.group(3) or 0)


def price_problem(amount: int) -> str | None:
    """Why an integer EUR asking price is not usable (placeholder or absurd), else None."""
    if amount <= 0:
        return "PRICE_MISSING_OR_ZERO"
    if amount < MIN_PLAUSIBLE_PRICE_EUR or amount in _PLACEHOLDER_PRICES:
        return "PRICE_PLACEHOLDER"
    if amount > MAX_PLAUSIBLE_PRICE_EUR:
        return "PRICE_IMPLAUSIBLE"
    return None


@dataclass(frozen=True, slots=True)
class ModelSearch:
    """One configured make/model search (``/lst/<make>/<model>``)."""

    make: str
    model: str

    @property
    def path(self) -> str:
        return f"/lst/{self.make}/{self.model}"

    @property
    def label(self) -> str:
        return f"{self.make}/{self.model}"

    def matches(self, make: str, model: str, model_group: str | None) -> bool:
        """The card's make and model (or model group) name this search's slugs.

        Hyphens, spaces and accents are ignored, so ``mokka-x`` matches "Mokka X" and a
        slug the site normalised by redirect (``rav-4`` -> ``rav4``) still matches "RAV 4".
        """
        if name_key(make) != name_key(self.make):
            return False
        wanted = name_key(self.model)
        return name_key(model) == wanted or (model_group is not None and name_key(model_group) == wanted)


def parse_models(raw: str | None) -> tuple[ModelSearch, ...]:
    """``"renault/captur nissan/qashqai"`` (spaces or commas) -> model searches, in order."""
    if not raw:
        return ()
    models: list[ModelSearch] = []
    for entry in re.split(r"[\s,]+", raw.strip()):
        if not entry:
            continue
        make, sep, model = entry.partition("/")
        if not sep or not _SLUG.fullmatch(make) or not _SLUG.fullmatch(model):
            raise ValidationFailed(f"AutoScout24 model entry {entry[:60]!r} is not <make-slug>/<model-slug>")
        item = ModelSearch(make, model)
        if item in models:
            raise ValidationFailed(f"AutoScout24 model {item.label} is configured twice")
        models.append(item)
    return tuple(models)


@dataclass(frozen=True, slots=True)
class NextData:
    """``props.pageProps`` of a page's ``__NEXT_DATA__`` script, or the drift reason.

    ``present`` says the script exists at all: the page is the site's own application (even when its
    JSON is broken), not a challenge or block page, so its widget words ("captcha" in a contact
    form script) do not turn a drifted page into an access block.
    """

    page_props: dict[str, Any] | None
    error: str | None
    route: str | None = None
    present: bool = False


def read_next_data(html: str | None) -> NextData:
    """Parse ``<script id="__NEXT_DATA__">`` strictly (no repair: invalid JSON is drift)."""
    if not html:
        return NextData(None, "NEXT_DATA_MISSING")
    match = _NEXT_DATA.search(html[:_MAX_HTML])
    if match is None:
        return NextData(None, "NEXT_DATA_MISSING")
    body = match.group(1)
    if len(body) > _MAX_NEXT_DATA:
        return NextData(None, "NEXT_DATA_TOO_LARGE", present=True)
    try:
        data = json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return NextData(None, "NEXT_DATA_UNPARSABLE", present=True)
    page_props = _get(data, "props", "pageProps")
    if not isinstance(page_props, dict):
        return NextData(None, "NEXT_DATA_WITHOUT_PAGE_PROPS", present=True)
    route = data.get("page") if isinstance(data, dict) and isinstance(data.get("page"), str) else None
    return NextData(page_props, None, route, present=True)


# --------------------------------------------------------------------------- fact collection


@dataclass(slots=True)
class _Facts:
    """Provenance, warnings and conflicts of one listing being assembled."""

    source_url: str
    snapshot_id: UUID | None
    observed_at: datetime
    provenance: dict[str, FieldProvenance] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[FieldConflict] = field(default_factory=list)

    def prov(
        self,
        name: str,
        selector: str,
        raw: object,
        *,
        confidence: Confidence = Confidence.HIGH,
        method: ExtractionMethod = ExtractionMethod.API_FIELD,
        transformation: str | None = None,
    ) -> None:
        self.provenance[name] = FieldProvenance(
            method=method,
            selector=bounded(selector, 300),
            raw_text=None if raw is None else str(raw),
            source_url=self.source_url[:2048],
            snapshot_id=self.snapshot_id,
            transformation=bounded(transformation, 200),
            confidence=confidence,
            observed_at=self.observed_at,
        )

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def conflict(self, name: str, values: list[str], locations: list[str], note: str | None = None) -> None:
        unique = list(dict.fromkeys(bounded(v, 200) or "" for v in values))
        if len(unique) < 2:
            return
        self.conflicts.append(
            FieldConflict(
                field=name,
                values=unique[:10],
                locations=[bounded(loc, 200) or "" for loc in locations][:10],
                note=bounded(note, 500),
            )
        )
        self.warn(f"{name.replace('.', '_').upper()}_SOURCES_DISAGREE")


def _not_future(date: PartialDate, observed_at: datetime) -> bool:
    stated = date.value or ""
    return stated <= observed_at.date().isoformat()[: len(stated)]


def _month_of(raw: str) -> PartialDate | None:
    """``YYYY-MM-DD`` whose day is a filler (the site shows MM/YYYY) -> month precision."""
    parsed = parse_partial_date(raw)
    if parsed is None or parsed.value is None:
        return None
    if parsed.precision == Precision.DAY:
        return PartialDate(value=parsed.value[:7], precision=Precision.MONTH)
    return parsed


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class CardResult:
    """One organic search card: its observation and the facts the card itself shows."""

    observation: SearchObservation
    listing: NormalizedListing


@dataclass(frozen=True, slots=True)
class SearchParse:
    """One parsed search page (exposed for tests, diagnostics and parser health)."""

    page: DiscoveryPage
    cards: tuple[CardResult, ...] = ()
    dropped: dict[str, int] = field(default_factory=dict)
    model: ModelSearch | None = None


# --------------------------------------------------------------------------- adapter


class AutoScout24Adapter:
    """Country-parameterised AutoScout24 adapter. Use `AutoScout24ItAdapter` / `AutoScout24DeAdapter`."""

    ADAPTER_KEY: ClassVar[str] = ""
    ADAPTER_VERSION: ClassVar[str] = ADAPTER_VERSION
    SUPPORTED_MODES: ClassVar[frozenset[SourceMode]] = frozenset({SourceMode.PUBLIC_HTML, SourceMode.FIXTURE})
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 public listings"
    COUNTRY: ClassVar[str] = ""
    #: Path prefix of the country's detail pages (identity for both, fetched for Italy only).
    DETAIL_PREFIX: ClassVar[str] = ""
    #: False: robots.txt disallows the detail pages, the source must be ``detail_mode: card_only``.
    DETAIL_PAGES_ALLOWED: ClassVar[bool] = False
    #: Language of the site's labels and of the seller texts it shows.
    LANGUAGE: ClassVar[str] = ""

    def __init__(self, config: SourceConfig, *, clock: Clock | None = None) -> None:
        if config.adapter != self.ADAPTER_KEY:
            raise ValidationFailed(f"source {config.source_key} is not configured for {self.ADAPTER_KEY}")
        if config.country != self.COUNTRY:
            raise ValidationFailed(f"{self.ADAPTER_KEY} serves {self.COUNTRY}, not {config.country}")
        if config.role != "acquisition":
            raise ValidationFailed(f"{self.ADAPTER_KEY} serves role acquisition, not {config.role}")
        if not self.DETAIL_PAGES_ALLOWED and config.detail_mode != "card_only":
            raise ValidationFailed(
                f"source {config.source_key}: {self.DISPLAY_NAME} detail pages are robots-disallowed;"
                " detail_mode must be card_only"
            )
        unknown = set(config.search) - _SEARCH_KEYS
        if unknown:
            raise ValidationFailed(
                f"source {config.source_key}: unsupported search keys {sorted(unknown)}",
                details={"allowed": sorted(_SEARCH_KEYS)},
            )
        if len(config.allowed_hosts) != 1:
            raise ValidationFailed(f"source {config.source_key}: exactly one allowed host is required")
        self.config = config
        self.source_key: str = config.source_key
        self.adapter_version: str = ADAPTER_VERSION
        self.policy = UrlPolicy(config)
        self._host = next(iter(self.policy.hosts))
        self._clock: Clock = clock or SystemClock()
        self.models: tuple[ModelSearch, ...] = parse_models(config.search.get("models"))
        budget = config.rate_budget.max_search_pages_per_run
        if len(self.models) > budget:
            raise ValidationFailed(
                f"source {config.source_key}: {len(self.models)} models need one search page each,"
                f" but max_search_pages_per_run is {budget}"
            )
        self._sort = config.search.get("sort", "age")
        self._desc = config.search.get("desc", "1")
        self._ustate = config.search.get("ustate") or None
        if self._sort not in _SORT_KEYS or self._desc not in {"0", "1"}:
            raise ValidationFailed(f"source {config.source_key}: unsupported sort/desc search setting")
        if self._ustate is not None and not _USTATE.fullmatch(self._ustate):
            raise ValidationFailed(f"source {config.source_key}: unsupported ustate search setting")

    # ------------------------------------------------------------------ contract

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            coverage_mode=CoverageMode.ROLLING_PAGES,
            provides_listing_ids=True,
            supports_modified_since=False,
            supports_stable_sort=False,
            has_cursor_pagination=False,
            exposes_source_modified_at=False,
            detail_required_for_price=False,
            max_page_size=20,
            countries=(self.config.country,),
            # Structure verified from owner captures only; False until a live smoke run confirms it.
            verified=False,
            seller_contact_evidence=False,
        )

    def query(self, profile: SearchProfile) -> list[tuple[str, str]]:
        """Site filters from the profile bounds (screening still applies every exact rule).

        The price band is widened to whole euros (floor/ceil) and ``kmto`` carries the mileage
        ceiling itself (the strict ``< 200,000 km`` rule is enforced by eligibility screening).
        """
        params: list[tuple[str, str]] = []
        if profile.min_price_eur is not None:
            params.append(("pricefrom", str(int(profile.min_price_eur.to_integral_value(ROUND_FLOOR)))))
        params.append(("priceto", str(int(profile.max_price_eur.to_integral_value(ROUND_CEILING)))))
        params.append(("kmto", str(int(profile.max_mileage_km_exclusive.to_integral_value(ROUND_CEILING)))))
        params.append(("sort", self._sort))
        params.append(("desc", self._desc))
        if self._ustate is not None:
            params.append(("ustate", self._ustate))
        return params

    def build_search(self, profile: SearchProfile, cursor: str | None) -> SearchRequest:
        """The search page of the cursor's model (the first model without a cursor)."""
        if not self.models:
            raise ValidationFailed(f"source {self.source_key} has no AutoScout24 models configured")
        if self.config.country not in profile.source_countries:
            raise ValidationFailed(f"profile {profile.key.value} does not search {self.config.country}")
        index, _partial, _not_found = _cursor_index(cursor)
        if index >= len(self.models):
            raise ValidationFailed("AutoScout24 discovery cursor points past the configured models")
        params = self.query(profile)
        url = self.policy.canonical(f"https://{self._host}{self.models[index].path}?{urlencode(params)}")
        if not self.policy.is_search_url(url):
            raise ValidationFailed(f"source {self.source_key}: search URL is outside the search path policy")
        return SearchRequest(
            source_key=self.source_key,
            profile_key=profile.key.value,
            url=url,
            page_number=index + 1,
            cursor=cursor,
            params=dict(params),
        )

    async def discover(self, request: SearchRequest, client: CrawlClient) -> DiscoveryPage:
        if request.source_key != self.source_key:
            raise ValidationFailed("search request belongs to another source")
        if not self.policy.is_search_url(request.url):
            document = self._denied_document(request.url, "search URL outside the source search policy")
            return self._failed_page(
                request,
                document,
                AccessClassification(AccessState.POLICY_DENIED, "unknown", "search URL outside policy"),
            )
        document = await client.fetch(request.url, purpose="search", source_key=self.source_key)
        return self.parse_search(request, document).page

    def canonicalize(self, url: str) -> CanonicalIdentity:
        canonical = self.policy.canonical(url)
        if not self.policy.host_allowed(canonical):
            raise ValidationFailed(f"URL host is not allowed for source {self.source_key}")
        return build_identity(self.source_key, canonical, self._listing_id(canonical))

    async def fetch_detail(self, identity: CanonicalIdentity, client: CrawlClient) -> RawDocument:
        if identity.source_key != self.source_key:
            raise ValidationFailed("identity belongs to another source")
        if not self.DETAIL_PAGES_ALLOWED or self.config.detail_mode == "card_only":
            return self._denied_document(
                identity.canonical_url, "card_only source: detail pages are never fetched"
            )
        if not self.policy.is_detail_url(identity.canonical_url):
            return self._denied_document(
                identity.canonical_url, "detail URL outside the source detail policy"
            )
        return await client.fetch(identity.canonical_url, purpose="detail", source_key=self.source_key)

    def detect_access_state(self, document: RawDocument) -> AccessState:
        page = parse_page(document.html)
        state = read_next_data(document.html)
        present = state.present or self._ld_vehicle(page) is not None
        return classify_document(
            document, page, allowed_hosts=self.policy.hosts, content_present=present
        ).access_state

    def assess_parser_health(self, samples: list[ParseOutcome]) -> ParserHealth:
        return assess_samples(samples)

    # ------------------------------------------------------------------ search pages

    def parse_search(self, request: SearchRequest, document: RawDocument) -> SearchParse:
        """Classify and parse one fetched model search page (pure; used by `discover`)."""
        index, partial_before, _not_found = _cursor_index(request.cursor)
        model = self.models[index] if index < len(self.models) else None
        has_more = model is not None and index + 1 < len(self.models)
        label = model.label if model is not None else urlsplit(request.url).path

        status_only = classify_status_only(document)
        if status_only is not None and status_only.access_state == AccessState.NOT_FOUND:
            # Only a genuine 404 of our own request (never a policy refusal that happened to answer
            # 404) is an unknown model slug: skip it and keep walking. A 404 of a verified slug, or
            # every configured model answering 404, is route drift and stops the walk.
            if model is not None and model.label in VERIFIED_MODEL_SLUGS:
                return self._drift(
                    request, document, f"drift: verified model search {label} answered HTTP 404"
                )
            if not has_more and _not_found + 1 >= len(self.models):
                return self._drift(
                    request, document, f"drift: all {len(self.models)} model searches answered HTTP 404"
                )
            return self._model_page(
                request,
                document,
                index,
                partial=True,
                has_more=has_more,
                notes=[f"model search {label} answered HTTP 404 (model slug not found); skipped"],
                model=model,
                not_found=True,
            )

        page = parse_page(document.html)
        state = read_next_data(document.html)
        props = state.page_props or {}
        listings = props.get("listings")
        # The application's own state script (even when its JSON is broken) is the expected content:
        # weak widget words then never turn a drifted result page into an access block.
        cls = classify_document(
            document, page, allowed_hosts=self.policy.hosts, content_present=state.present
        )
        if cls.access_state != AccessState.OK:
            return SearchParse(page=self._failed_page(request, document, cls), model=model)
        notes: list[str] = []
        final = document.final_url or document.url
        if final != request.url:
            if not self.policy.is_search_url(final):
                return self._drift(request, document, "search URL redirected outside the search path policy")
            if urlsplit(final).path != urlsplit(request.url).path:
                notes.append(f"redirected to {urlsplit(final).path[:120]}")
        if state.error is not None:
            return self._drift(request, document, f"drift: {state.error}")
        if props.get("pageid") not in (None, "list"):
            return self._drift(request, document, "drift: __NEXT_DATA__ is not a result list page")
        if not isinstance(listings, list):
            return self._drift(request, document, "drift: pageProps.listings is missing")
        reported = _int(props.get("numberOfResults"))
        pages = _int(props.get("numberOfPages"))
        if not listings and reported != 0:
            shown = "no result count" if reported is None else f"{reported} results"
            return self._drift(request, document, f"drift: no listings while the page reports {shown}")

        cards: list[CardResult] = []
        dropped: Counter[str] = Counter()
        organic = 0
        for position, item in enumerate(listings):
            if not isinstance(item, dict):
                dropped["malformed_card"] += 1
                continue
            if item.get("searchResultType") != "Organic" or item.get("searchResultSection") != "Main":
                dropped["not_organic"] += 1
                continue
            organic += 1
            result = self._card(item, position, model, document)
            if isinstance(result, str):
                dropped[result] += 1
            else:
                cards.append(result)
        if listings and organic == 0 and (reported or 0) > 0:
            # Results are reported but no card carries the Organic/Main markers: the site renamed
            # or changed them. Dropping every card silently would look like an empty, healthy page.
            return self._drift(
                request, document, f"drift: no Organic/Main card among {len(listings)} listings"
            )
        unreadable = {k: v for k, v in dropped.items() if k not in ("not_organic", "model_mismatch")}
        if organic and not cards and unreadable:
            reasons = ", ".join(f"{k}={v}" for k, v in sorted(unreadable.items()))
            return self._drift(request, document, f"drift: no organic card could be read ({reasons})")
        if dropped:
            notes.append("dropped cards: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
        incomplete = bool(unreadable)
        if organic and dropped["model_mismatch"] == organic:
            incomplete = True
            notes.append(f"no card of the requested model {label}")
        if pages is not None and pages > 1:
            incomplete = True
            notes.append(f"{pages} result pages; only page 1 is read per model and run")
        elif reported is not None and reported > len(listings):
            incomplete = True
            notes.append(f"{reported} results reported, {len(listings)} cards on the page")
        if reported == 0 and not listings:
            notes.append("explicit empty result")
        return self._model_page(
            request,
            document,
            index,
            partial=partial_before or incomplete,
            has_more=has_more,
            notes=notes,
            model=model,
            cards=tuple(cards),
            dropped=dict(dropped),
            reported=reported,
        )

    def _model_page(
        self,
        request: SearchRequest,
        document: RawDocument,
        index: int,
        *,
        partial: bool,
        has_more: bool,
        notes: list[str],
        model: ModelSearch | None,
        cards: tuple[CardResult, ...] = (),
        dropped: dict[str, int] | None = None,
        reported: int | None = None,
        not_found: bool = False,
    ) -> SearchParse:
        _, partial_before, not_found_before = _cursor_index(request.cursor)
        partial = partial or partial_before
        missing = not_found_before + (1 if not_found else 0)
        next_cursor = (
            f"model:{index + 1}{':partial' if partial else ''}{f':nf{missing}' if missing else ''}"
            if has_more
            else None
        )
        if has_more:
            completeness = Completeness.PARTIAL  # the traversal continues with the next model
        else:
            completeness = Completeness.PARTIAL if partial else Completeness.COMPLETE
        warnings = tuple(bounded(n, 300) or "" for n in notes[:10])
        page = DiscoveryPage(
            request=request,
            observations=tuple(c.observation for c in cards),
            next_cursor=next_cursor,
            has_more=has_more,
            completeness=completeness,
            access_state=AccessState.OK,
            access_evidence=bounded("; ".join(notes), 500) if notes else None,
            result_count_reported=reported if reported is not None and reported >= 0 else None,
            fetched_at=document.fetched_at,
            fetch=document.fetch,
            page_type="search",
            warnings=warnings,
        )
        return SearchParse(page=page, cards=cards, dropped=dropped or {}, model=model)

    def _drift(self, request: SearchRequest, document: RawDocument, reason: str) -> SearchParse:
        """Unexpected structure: the traversal stops here and parser health records it."""
        cls = AccessClassification(AccessState.UNEXPECTED_CONTENT, "unknown", reason)
        return SearchParse(page=self._failed_page(request, document, cls))

    # ------------------------------------------------------------------ one card

    def _card(
        self, item: dict[str, Any], position: int, model: ModelSearch | None, document: RawDocument
    ) -> CardResult | str:
        """A card's observation and facts, or the reason it is dropped."""
        listing_id = item.get("id")
        if not isinstance(listing_id, str) or not _UUID.fullmatch(listing_id):
            return "invalid_listing_id"
        href = item.get("url")
        if not isinstance(href, str) or not href.startswith(self.DETAIL_PREFIX):
            return "invalid_url"
        try:
            canonical = self.policy.canonical(href, base=f"https://{self._host}/")
        except ValidationFailed:
            return "invalid_url"
        parts = urlsplit(canonical)
        if (
            not self.policy.host_allowed(canonical)
            or parts.query
            or not parts.path.startswith(self.DETAIL_PREFIX)
        ):
            return "invalid_url"
        tail = _URL_UUID_TAIL.search(parts.path)
        if tail is None or tail.group(1) != listing_id:
            return "url_id_mismatch"
        if self.config.detail_mode == "fetch" and not self.policy.is_detail_url(canonical):
            return "url_outside_detail_policy"
        vehicle = item.get("vehicle")
        if not isinstance(vehicle, dict):
            return "missing_vehicle"
        make, model_name = _str(vehicle.get("make")), _str(vehicle.get("model"))
        if make is None or model_name is None:
            return "missing_make_model"
        if model is not None and not model.matches(make, model_name, _str(vehicle.get("modelGroup"))):
            return "model_mismatch"

        identity = build_identity(self.source_key, canonical, listing_id)
        facts = _Facts(source_url=canonical, snapshot_id=None, observed_at=document.fetched_at)
        facts.prov(
            "source_listing_id", f"{_SEARCH_PROV}.id", listing_id, transformation="equals the URL's UUID"
        )
        tracking = _dict(item.get("tracking"))
        pills = self._pills(item.get("vehicleDetails"))

        title_raw = " ".join(
            t for t in (_str(item.get("title")), _str(vehicle.get("modelVersionInput"))) if t
        )
        title, suspicious = _clean_text(title_raw, 300)
        trim, trim_suspicious = _clean_text(_str(vehicle.get("modelVersionInput")), 200)
        if suspicious or trim_suspicious:
            facts.warn("SELLER_TEXT_SUSPICIOUS")
        if _COMMERCIAL.search(title_raw):
            facts.warn("COMMERCIAL_REGISTRATION_SUSPECTED")
        facts.prov("vehicle.make", f"{_SEARCH_PROV}.vehicle.make", make)
        facts.prov("vehicle.model", f"{_SEARCH_PROV}.vehicle.model", model_name)
        if trim:
            facts.prov(
                "vehicle.trim",
                f"{_SEARCH_PROV}.vehicle.modelVersionInput",
                trim,
                confidence=Confidence.MEDIUM,
            )

        price, price_raw = self._card_price(item, tracking, facts)
        mileage_km, mileage_raw = self._card_mileage(vehicle, tracking, pills, facts)
        first_reg = self._card_first_registration(tracking, pills, facts)
        fuel = self._card_fuel(vehicle, tracking, pills, facts)
        gearbox = self._gearbox(
            _str(vehicle.get("transmission")) or pills.get("gearbox"),
            f"{_SEARCH_PROV}.vehicle.transmission",
            facts,
        )
        power_kw = self._power_text(
            pills.get("speedometer"), f"{_SEARCH_PROV}.vehicleDetails[speedometer]", facts
        )
        displacement = self._displacement_text(
            _str(vehicle.get("engineDisplacementInCCM")),
            f"{_SEARCH_PROV}.vehicle.engineDisplacementInCCM",
            facts,
        )
        seller_type = self._seller_type(
            _get(item, "seller", "type"), None, f"{_SEARCH_PROV}.seller.type", facts
        )
        country = self._country(
            _get(item, "location", "countryCode"), f"{_SEARCH_PROV}.location.countryCode", facts
        )
        offer_type = _str(vehicle.get("offerType"))
        if offer_type is not None and offer_type != "U":
            facts.warn(f"OFFER_TYPE_{offer_type[:3].upper()}")
        damaged = ClaimStatus.UNKNOWN
        if vehicle.get("isCurrentlyDamaged") is True:
            damaged = ClaimStatus.SELLER_CLAIMED
            facts.prov("condition.damaged_vehicle", f"{_SEARCH_PROV}.vehicle.isCurrentlyDamaged", True)
            if price.type == PriceType.FULL_VEHICLE_ASKING:
                price = price.model_copy(update={"type": PriceType.PARTS_OR_DAMAGED})
        facts.warn("FACTS_FROM_SEARCH_CARD")

        listing = NormalizedListing(
            source_key=self.source_key,
            source_listing_id=identity.source_listing_id,
            canonical_url=canonical,
            observed_at=document.fetched_at,
            language=self.LANGUAGE or None,
            title=title,
            seller_type=seller_type,
            location=LocationInfo(country=country),
            vehicle=VehicleSpec(
                make=bounded(make, 80),
                model=bounded(model_name, 120),
                trim=trim,
                first_registration=first_reg,
                fuel=fuel,
                engine_displacement_cm3=displacement,
                power_kw=power_kw,
                gearbox=gearbox,
                mileage_km=mileage_km,
                mileage_original=MileageOriginal(
                    amount=mileage_km, unit="km" if mileage_km is not None else "unknown", text=mileage_raw
                ),
                mileage_claim=OdometerClaim.SELLER_REPORTED
                if mileage_km is not None
                else OdometerClaim.UNKNOWN,
            ),
            price=price,
            condition=ConditionClaims(damaged_vehicle=damaged),
            provenance=facts.provenance,
            conflicts=tuple(facts.conflicts),
            warnings=tuple(facts.warnings),
            parser_version=ADAPTER_VERSION,
        )
        # A conditional price (only with the dealer's financing) is no card price for screening.
        card_minor = price.amount_minor if price.type != PriceType.UNKNOWN else None
        card_currency = price.currency if card_minor is not None else None
        material: dict[str, str | None] = {
            "source_listing_id": identity.source_listing_id,
            "canonical_url": canonical,
            "title": title,
            "price_minor": None if card_minor is None else str(card_minor),
            "currency": card_currency,
            "mileage_km": None if mileage_km is None else plain(mileage_km),
            "source_modified_at": None,
        }
        digest, normalized = card_hash(material)
        observation = SearchObservation(
            source_listing_id=identity.source_listing_id,
            canonical_url=canonical,
            title=title,
            card_price_raw=bounded(price_raw, 200),
            card_price_minor=card_minor,
            card_currency=card_currency,
            card_mileage_raw=bounded(mileage_raw, 200),
            card_mileage_km=mileage_km,
            position=position,
            card_hash=digest,
            card_hash_material=normalized,
            # card_only (Germany): the card is the only source of vehicle facts, so they travel
            # with the observation and become a card-sourced revision (no detail page is fetched).
            card_listing=listing if self.config.detail_mode == "card_only" else None,
        )
        return CardResult(observation=observation, listing=listing)

    @staticmethod
    def _pills(details: Any) -> dict[str, str]:
        """``vehicleDetails`` pills by icon name (``speedometer`` -> "81 kW (110 CV)")."""
        pills: dict[str, str] = {}
        if isinstance(details, list):
            for entry in details:
                icon, data = _get(entry, "iconName"), _str(_get(entry, "data"))
                if isinstance(icon, str) and data and icon not in pills:
                    pills[icon] = data
        return pills

    def _card_price(
        self, item: dict[str, Any], tracking: dict[str, Any], facts: _Facts
    ) -> tuple[PriceInfo, str | None]:
        price = _dict(item.get("price"))
        formatted = _str(price.get("priceFormatted"))
        raw = _int(price.get("priceRaw"))
        if raw is None:
            facts.warn("PRICE_MISSING")
            return PriceInfo(raw_text=bounded(formatted, 300)), formatted
        tracked = _int(tracking.get("price"))
        if tracked is not None and tracked != raw:
            facts.conflict(
                "price.amount_minor",
                [str(raw * 100), str(tracked * 100)],
                [f"{_SEARCH_PROV}.price.priceRaw", f"{_SEARCH_PROV}.tracking.price"],
            )
        if formatted is not None and "€" not in formatted:
            facts.warn("PRICE_CURRENCY_UNEXPECTED")
            return PriceInfo(raw_text=bounded(formatted, 300)), formatted
        problem = price_problem(raw)
        if problem is not None:
            facts.warn(problem)
            return PriceInfo(raw_text=bounded(formatted or str(raw), 300)), formatted or str(raw)
        price_type = PriceType.FULL_VEHICLE_ASKING
        if price.get("isConditionalPrice") is True:
            price_type = PriceType.UNKNOWN
            facts.warn("PRICE_CONDITIONAL")
        basis, vat = PriceBasis.UNKNOWN, VatTreatment.UNKNOWN
        label = (_str(price.get("vatLabel")) or "").casefold()
        if label in {"inkl. mwst.", "iva inclusa"}:
            basis, vat = PriceBasis.GROSS, VatTreatment.VAT_SHOWN
            facts.prov(
                "price.basis",
                f"{_SEARCH_PROV}.price.vatLabel",
                label,
                confidence=Confidence.MEDIUM,
                transformation="VAT-included label -> gross",
            )
        facts.prov(
            "price.amount_minor",
            f"{_SEARCH_PROV}.price.priceRaw",
            raw,
            transformation="integer EUR x 100; currency from the euro sign",
        )
        return (
            PriceInfo(
                raw_text=bounded(formatted or str(raw), 300),
                amount_minor=raw * 100,
                currency="EUR",
                basis=basis,
                type=price_type,
                vat_treatment=vat,
            ),
            formatted or str(raw),
        )

    def _card_mileage(
        self, vehicle: dict[str, Any], tracking: dict[str, Any], pills: dict[str, str], facts: _Facts
    ) -> tuple[Decimal | None, str | None]:
        text = _str(vehicle.get("mileageInKm")) or pills.get("mileage_odometer")
        digits = _int(tracking.get("mileage"))
        from_text = self._km_from_text(text)
        if digits is None and from_text is None:
            facts.warn("MILEAGE_MISSING")
            return None, text
        if digits is not None and from_text is not None and Decimal(digits) != from_text:
            facts.conflict(
                "vehicle.mileage_km",
                [str(digits), plain(from_text)],
                [f"{_SEARCH_PROV}.tracking.mileage", f"{_SEARCH_PROV}.vehicle.mileageInKm"],
            )
        km = Decimal(digits) if digits is not None else from_text
        assert km is not None
        facts.prov(
            "vehicle.mileage_km",
            f"{_SEARCH_PROV}.tracking.mileage"
            if digits is not None
            else f"{_SEARCH_PROV}.vehicle.mileageInKm",
            digits if digits is not None else text,
        )
        return km, text

    @staticmethod
    def _km_from_text(text: str | None) -> Decimal | None:
        if not text or "km" not in text.casefold():
            return None
        match = _NUMBER_IN_TEXT.search(text)
        return parse_locale_number(match.group(0), kind="count") if match else None

    def _card_first_registration(
        self, tracking: dict[str, Any], pills: dict[str, str], facts: _Facts
    ) -> PartialDate:
        for raw, selector in (
            (_str(tracking.get("firstRegistration")), f"{_SEARCH_PROV}.tracking.firstRegistration"),
            (pills.get("calendar"), f"{_SEARCH_PROV}.vehicleDetails[calendar]"),
        ):
            if not raw:
                continue
            parsed = parse_partial_date(raw)
            if parsed is None or parsed.value is None:
                facts.warn("FIRST_REGISTRATION_UNPARSEABLE")
                continue
            if not _not_future(parsed, facts.observed_at):
                facts.warn("FIRST_REGISTRATION_IN_FUTURE")
                return PartialDate()
            facts.prov(
                "vehicle.first_registration",
                selector,
                raw,
                transformation=f"precision={parsed.precision.value}",
            )
            return parsed
        return PartialDate()

    def _card_fuel(
        self, vehicle: dict[str, Any], tracking: dict[str, Any], pills: dict[str, str], facts: _Facts
    ) -> Fuel:
        code = (_str(tracking.get("fuelType")) or "").casefold()
        text = _str(vehicle.get("fuel")) or pills.get("gas_pump")
        by_code = FUEL_CODES.get(code)
        by_text = fuel_word(text)
        if by_code is not None and by_text is not None and by_code != by_text:
            facts.conflict(
                "vehicle.fuel",
                [by_code.value, by_text.value],
                [f"{_SEARCH_PROV}.tracking.fuelType", f"{_SEARCH_PROV}.vehicle.fuel"],
            )
            return Fuel.UNKNOWN
        fuel = by_code or by_text
        if fuel is None:
            if code or text:
                facts.warn("FUEL_UNMAPPED")
            return Fuel.UNKNOWN
        hybrid = code in _HYBRID_CODES or fuel in (Fuel.HYBRID_PETROL, Fuel.HYBRID_DIESEL)
        facts.prov(
            "vehicle.fuel",
            f"{_SEARCH_PROV}.tracking.fuelType" if by_code is not None else f"{_SEARCH_PROV}.vehicle.fuel",
            code or text,
            confidence=Confidence.MEDIUM if hybrid or by_code is None else Confidence.HIGH,
            transformation=(
                "fuel code cross-checked with the localised label"
                + ("; full or plug-in hybrid not distinguishable on the card" if hybrid else "")
            ),
        )
        return fuel

    # ------------------------------------------------------------------ shared field readers

    @staticmethod
    def _gearbox(raw: str | None, selector: str, facts: _Facts) -> Gearbox:
        if not raw:
            return Gearbox.UNKNOWN
        mapped = gearbox_word(raw)
        if mapped is None:
            facts.warn("GEARBOX_UNMAPPED")
            return Gearbox.UNKNOWN
        facts.prov("vehicle.gearbox", selector, raw, transformation=f"localised label -> {mapped.value}")
        return mapped

    @staticmethod
    def _power_text(raw: str | None, selector: str, facts: _Facts) -> int | None:
        if not raw:
            return None
        match = _KW.search(raw)
        power = power_from_value(Decimal(match.group(1)), "kw", match.group(0)) if match else find_power(raw)
        if power is None or not 1 <= power.kw <= 2000:
            facts.warn("POWER_UNPARSEABLE")
            return None
        facts.prov(
            "vehicle.power_kw",
            selector,
            raw,
            confidence=Confidence.HIGH if power.converted_from is None else Confidence.MEDIUM,
            transformation=None if power.converted_from is None else f"{power.converted_from} -> kW, half-up",
        )
        return power.kw

    @staticmethod
    def _displacement_text(raw: str | None, selector: str, facts: _Facts) -> int | None:
        if not raw:
            return None
        match = _CCM.search(raw)
        value = parse_locale_number(match.group(1), kind="count") if match else None
        if value is None or value != value.to_integral_value() or not 50 <= value <= 10000:
            facts.warn("DISPLACEMENT_UNPARSEABLE")
            return None
        facts.prov(
            "vehicle.engine_displacement_cm3", selector, raw, transformation="'.' is the thousands separator"
        )
        return int(value)

    @staticmethod
    def _seller_type(raw_type: Any, is_dealer: Any, selector: str, facts: _Facts) -> SellerType:
        """Seller type from ``seller.type`` / ``seller.isDealer`` ONLY (no other seller field)."""
        if isinstance(is_dealer, bool):
            facts.prov("seller_type", selector.replace(".type", ".isDealer"), is_dealer)
            return SellerType.DEALER if is_dealer else SellerType.PRIVATE
        text = _str(raw_type)
        mapped = _SELLER_TYPES.get(text.casefold()) if text else None
        if mapped is None:
            return SellerType.UNKNOWN
        facts.prov("seller_type", selector, text)
        return mapped

    @staticmethod
    def _country(raw: Any, selector: str, facts: _Facts) -> str | None:
        text = _str(raw)
        if text is None or not _COUNTRY.fullmatch(text.upper()):
            return None
        facts.prov("location.country", selector, text)
        return text.upper()

    # ------------------------------------------------------------------ detail pages (Italy)

    def parse_detail(self, document: RawDocument) -> ParsedListing:
        if not self.DETAIL_PAGES_ALLOWED or self.config.detail_mode == "card_only":
            return ParsedListing(
                page_type="unknown",
                access_state=AccessState.POLICY_DENIED,
                errors=("DETAIL_PAGES_NOT_FETCHED: card_only source (robots.txt disallows detail pages)",),
            )
        page = parse_page(document.html)
        state = read_next_data(document.html)
        props = state.page_props or {}
        details = props.get("listingDetails") if props.get("pageid") in (None, "detail") else None
        nd_vehicle = _get(details, "vehicle") if isinstance(_get(details, "vehicle"), dict) else None
        ld_vehicle = self._ld_vehicle(page)
        cls = classify_document(
            document,
            page,
            allowed_hosts=self.policy.hosts,
            content_present=state.present or ld_vehicle is not None,
        )
        if cls.access_state != AccessState.OK:
            return ParsedListing(
                page_type=cls.page_type,
                access_state=cls.access_state,
                errors=(bounded(cls.evidence, 300),) if cls.evidence else (),
                warnings=("LISTING_REMOVED_EXPLICIT",) if cls.access_state == AccessState.REMOVED else (),
            )
        source_url = document.final_url or document.url
        if not self.policy.is_detail_url(source_url):
            return ParsedListing(
                page_type="search" if self.policy.is_search_url(source_url) else "unknown",
                access_state=AccessState.UNEXPECTED_CONTENT,
                errors=("FINAL_URL_NOT_A_DETAIL_PAGE",),
            )
        canonical = self.policy.canonical(source_url)
        listing_id = self._listing_id(canonical)
        if listing_id is None:
            return ParsedListing(
                page_type="detail", access_state=AccessState.OK, errors=("LISTING_ID_MISSING_IN_URL",)
            )
        if nd_vehicle is None and ld_vehicle is None:
            errors = [f"NO_VEHICLE_STRUCTURED_DATA: {state.error or 'listingDetails.vehicle missing'}"]
            return ParsedListing(page_type="detail", access_state=AccessState.OK, errors=tuple(errors))
        if isinstance(details, dict) and details.get("id") not in (None, listing_id):
            return ParsedListing(
                page_type="detail",
                access_state=AccessState.OK,
                errors=("LISTING_ID_MISMATCH: listingDetails.id is not the URL's listing",),
            )
        ld_id = text_of(ld_vehicle.get("identifier")) if ld_vehicle is not None else None
        if ld_id is not None and _UUID.fullmatch(ld_id) and ld_id != listing_id:
            return ParsedListing(
                page_type="detail",
                access_state=AccessState.OK,
                errors=("LISTING_ID_MISMATCH: JSON-LD Car.identifier is not the URL's listing",),
            )
        listing = self._detail_listing(
            document=document,
            canonical=canonical,
            listing_id=listing_id,
            details=details,
            v=nd_vehicle,
            ld=ld_vehicle,
            state=state,
        )
        return ParsedListing(
            page_type="detail", access_state=AccessState.OK, listing=listing, warnings=listing.warnings
        )

    @staticmethod
    def _ld_vehicle(page: PageView | None) -> dict[str, Any] | None:
        """The JSON-LD vehicle node (the derived ``Product.offers.itemOffered`` Car) when unique."""
        if page is None:
            return None
        vehicles = [
            n for n in page.json_ld.nodes if local_type_names(n) & {"Car", "Vehicle", "MotorizedVehicle"}
        ]
        return vehicles[0] if len(vehicles) == 1 else None

    def _detail_listing(
        self,
        *,
        document: RawDocument,
        canonical: str,
        listing_id: str,
        details: Any,
        v: dict[str, Any] | None,
        ld: dict[str, Any] | None,
        state: NextData,
    ) -> NormalizedListing:
        facts = _Facts(
            source_url=canonical, snapshot_id=document.snapshot_id, observed_at=document.fetched_at
        )
        facts.prov("source_listing_id", "url:uuid-tail", listing_id, method=ExtractionMethod.DERIVED)
        nd = v or {}
        raw = _dict(nd.get("rawData"))
        engine = next(iter(x for x in _as_list(_get(ld, "vehicleEngine")) if isinstance(x, dict)), None)
        if v is None:
            facts.warn("NEXT_DATA_MISSING_JSON_LD_FALLBACK")
            if state.error:
                facts.warn(state.error)
        if ld is None:
            facts.warn("JSON_LD_VEHICLE_MISSING")

        make = _str(nd.get("make")) or text_of(scalar(_get(ld, "manufacturer"))) or text_of(_get(ld, "brand"))
        model = _str(nd.get("model")) or text_of(_get(ld, "model"))
        self._cross(facts, "vehicle.make", make, text_of(scalar(_get(ld, "manufacturer"))), key=name_key)
        self._cross(facts, "vehicle.model", model, text_of(_get(ld, "model")), key=name_key)
        if make:
            facts.prov(
                "vehicle.make",
                f"{_DETAIL_PROV}.vehicle.make" if nd.get("make") else "json_ld:Car.manufacturer",
                make,
            )
        if model:
            facts.prov(
                "vehicle.model",
                f"{_DETAIL_PROV}.vehicle.model" if nd.get("model") else "json_ld:Car.model",
                model,
            )
        version = _str(nd.get("modelVersionInput"))
        trim, suspicious = _clean_text(version, 200)
        title_raw = " ".join(
            t for t in (_str(_get(details, "title")) or text_of(_get(ld, "name")), version) if t
        )
        title, title_suspicious = _clean_text(title_raw, 300)
        if suspicious or title_suspicious:
            facts.warn("SELLER_TEXT_SUSPICIOUS")
        description = _str(_get(details, "description"))
        description_text = html_to_text(description).text if description else ""
        if _COMMERCIAL.search(f"{title_raw} {description_text}"):
            facts.warn("COMMERCIAL_REGISTRATION_SUSPECTED")

        # First registration: language-neutral raw date (day is a filler), JSON-LD productionDate
        # (which AutoScout24 Italy fills with the FIRST-REGISTRATION date) as fallback/cross-check.
        first_reg = PartialDate()
        reg_raw = _str(nd.get("firstRegistrationDateRaw")) or _str(
            _get(raw, "condition", "firstRegistrationDate", "raw")
        )
        ld_reg_raw = text_of(_get(ld, "dateVehicleFirstRegistered")) or text_of(_get(ld, "productionDate"))
        nd_reg = _month_of(reg_raw) if reg_raw else None
        ld_reg = _month_of(ld_reg_raw) if ld_reg_raw else None
        chosen = nd_reg or ld_reg
        if chosen is not None and chosen.value is not None:
            if not _not_future(chosen, facts.observed_at):
                facts.warn("FIRST_REGISTRATION_IN_FUTURE")
            else:
                first_reg = chosen
                facts.prov(
                    "vehicle.first_registration",
                    f"{_DETAIL_PROV}.vehicle.firstRegistrationDateRaw"
                    if nd_reg
                    else "json_ld:Car.productionDate",
                    reg_raw if nd_reg else ld_reg_raw,
                    confidence=Confidence.HIGH if nd_reg else Confidence.LOW,
                    transformation="day is a filler: month precision"
                    + ("" if nd_reg else "; productionDate read as first registration"),
                )
        if nd_reg is not None and ld_reg is not None and nd_reg.value != ld_reg.value:
            facts.conflict(
                "vehicle.first_registration",
                [nd_reg.value or "", ld_reg.value or ""],
                [f"{_DETAIL_PROV}.vehicle.firstRegistrationDateRaw", "json_ld:Car.productionDate"],
            )
        production_year = None
        production_raw = _str(_get(raw, "productionDate", "raw")) or _str(nd.get("productionYear"))
        if production_raw:
            year = _int(production_raw[:4])
            if year is not None and 1950 <= year <= 2100:
                production_year = year
                facts.prov(
                    "vehicle.production_year",
                    f"{_DETAIL_PROV}.vehicle.rawData.productionDate",
                    production_raw,
                )

        mileage_nd = _int(nd.get("mileageInKmRaw"))
        if mileage_nd is None:
            mileage_nd = _int(_get(raw, "condition", "mileageInKm", "raw"))
        mileage_ld = self._ld_km(ld)
        self._cross(facts, "vehicle.mileage_km", mileage_nd, mileage_ld)
        km: Decimal | None = Decimal(mileage_nd) if mileage_nd is not None else mileage_ld
        if km is not None:
            facts.prov(
                "vehicle.mileage_km",
                f"{_DETAIL_PROV}.vehicle.mileageInKmRaw"
                if mileage_nd is not None
                else "json_ld:Car.mileageFromOdometer",
                plain(km),
            )
        else:
            facts.warn("MILEAGE_MISSING")

        fuel = self._detail_fuel(nd, raw, ld, engine, facts)
        gearbox_raw = _str(_get(raw, "engine", "transmissionType", "raw")) or _str(nd.get("transmissionType"))
        gearbox = self._gearbox(gearbox_raw, f"{_DETAIL_PROV}.vehicle.rawData.engine.transmissionType", facts)
        ld_gearbox = gearbox_word(text_of(_get(ld, "vehicleTransmission")))
        if gearbox == Gearbox.UNKNOWN and ld_gearbox is not None:
            gearbox = self._gearbox(
                text_of(_get(ld, "vehicleTransmission")), "json_ld:Car.vehicleTransmission", facts
            )
        self._cross(
            facts,
            "vehicle.gearbox",
            gearbox if gearbox != Gearbox.UNKNOWN else None,
            ld_gearbox,
            key=lambda g: g.value,
        )

        drive = self._detail_drive(nd, raw, ld, facts)
        power_nd = _int(nd.get("rawPowerInKw"))
        power_ld = self._ld_power(engine)
        self._cross(facts, "vehicle.power_kw", power_nd, power_ld)
        power_kw = power_nd if power_nd is not None and 1 <= power_nd <= 2000 else power_ld
        if power_kw is not None:
            facts.prov(
                "vehicle.power_kw",
                f"{_DETAIL_PROV}.vehicle.rawPowerInKw"
                if power_nd is not None
                else "json_ld:enginePower[KWT]",
                power_kw,
            )
        disp_nd = _int(nd.get("rawDisplacementInCCM"))
        disp_ld = self._ld_displacement(engine)
        self._cross(facts, "vehicle.engine_displacement_cm3", disp_nd, disp_ld)
        displacement = disp_nd if disp_nd is not None and 50 <= disp_nd <= 10000 else disp_ld
        if displacement is not None:
            facts.prov(
                "vehicle.engine_displacement_cm3",
                f"{_DETAIL_PROV}.vehicle.rawDisplacementInCCM"
                if disp_nd is not None
                else "json_ld:engineDisplacement",
                displacement,
            )
        body = self._detail_body(raw, nd, ld, facts)
        seats = _int(nd.get("numberOfSeats"))
        seats = seats if seats is not None and 1 <= seats <= 12 else None

        price = self._detail_price(details, ld, description_text, facts)
        seller_type = self._seller_type(
            _get(details, "seller", "type"),
            _get(details, "seller", "isDealer"),
            f"{_DETAIL_PROV}.seller.type",
            facts,
        )
        country = self._country(
            _get(details, "location", "countryCode"), f"{_DETAIL_PROV}.location.countryCode", facts
        )
        availability = Availability.UNKNOWN
        status = _str(_get(details, "status"))
        if status == "Active":
            availability = Availability.AVAILABLE
            facts.prov("availability", f"{_DETAIL_PROV}.status", status, confidence=Confidence.MEDIUM)
        elif status is not None:
            facts.warn(f"LISTING_STATUS_{name_key(status)[:20].upper()}")
        published = SourceTimestamp()
        created = _str(_get(details, "createdTimestampWithOffset"))
        if created:
            published = parse_source_timestamp(created, self.config.source_timezone) or SourceTimestamp()

        damage = _get(raw, "condition", "damage")
        accident = _get(damage, "accidentFree")
        condition = ConditionClaims(
            accident_free=ClaimStatus.SELLER_CLAIMED
            if accident is True
            else ClaimStatus.SELLER_DENIED
            if accident is False
            else ClaimStatus.UNKNOWN,
            damaged_vehicle=ClaimStatus.SELLER_CLAIMED
            if _get(damage, "isCurrentlyDamaged") is True
            else ClaimStatus.UNKNOWN,
        )
        if (
            condition.damaged_vehicle == ClaimStatus.SELLER_CLAIMED
            and price.type == PriceType.FULL_VEHICLE_ASKING
        ):
            price = price.model_copy(update={"type": PriceType.PARTS_OR_DAMAGED})
        owners = _int(_get(raw, "condition", "numberOfPreviousOwnersExtended", "raw"))
        inspection = parse_partial_date(_str(nd.get("nextVehicleSafetyInspection")) or "") or PartialDate()
        documentation = Documentation(
            previous_owners=owners if owners is not None and 0 <= owners <= 50 else None,
            inspection_expiry=inspection,
        )
        co2 = self._detail_co2(raw, fuel, facts)

        return NormalizedListing(
            source_key=self.source_key,
            source_listing_id=listing_id,
            canonical_url=canonical,
            observed_at=document.fetched_at,
            language=self.LANGUAGE or None,
            title=title,
            seller_type=seller_type,
            location=LocationInfo(country=country),
            vehicle=VehicleSpec(
                make=bounded(make, 80),
                model=bounded(model, 120),
                trim=trim,
                first_registration=first_reg,
                production_year=production_year,
                body_type=body,
                seats=seats,
                fuel=fuel,
                engine_displacement_cm3=displacement,
                power_kw=power_kw,
                gearbox=gearbox,
                drive=drive,
                mileage_km=km,
                mileage_original=MileageOriginal(amount=km, unit="km" if km is not None else "unknown"),
                mileage_claim=OdometerClaim.SELLER_REPORTED if km is not None else OdometerClaim.UNKNOWN,
            ),
            price=price,
            availability=availability,
            condition=condition,
            documentation=documentation,
            co2=co2,
            source_published_at=published,
            provenance=facts.provenance,
            conflicts=tuple(facts.conflicts),
            warnings=tuple(facts.warnings),
            parser_version=ADAPTER_VERSION,
        )

    @staticmethod
    def _cross(facts: _Facts, name: str, primary: Any, secondary: Any, *, key: Any = None) -> None:
        """Record a conflict when ``__NEXT_DATA__`` and JSON-LD both state a value and disagree."""
        if primary is None or secondary is None:
            return
        convert = key or (lambda x: plain(Decimal(x)) if isinstance(x, int | Decimal) else str(x))
        a, b = convert(primary), convert(secondary)
        if a != b:
            facts.conflict(name, [str(a), str(b)], [_DETAIL_PROV, "json_ld:Car"])

    @staticmethod
    def _ld_km(ld: dict[str, Any] | None) -> Decimal | None:
        quantity = scalar(_get(ld, "mileageFromOdometer"))
        if not isinstance(quantity, dict):
            return None
        unit = (text_of(quantity.get("unitCode")) or text_of(quantity.get("unitText")) or "").casefold()
        value = decimal_from_json(scalar(quantity.get("value")), kind="count")
        return value if value is not None and unit in {"kmt", "km"} else None

    @staticmethod
    def _ld_power(engine: dict[str, Any] | None) -> int | None:
        for entry in _as_list(_get(engine, "enginePower")):
            if isinstance(entry, dict) and (text_of(entry.get("unitCode")) or "").upper() == "KWT":
                value = decimal_from_json(scalar(entry.get("value")), kind="count")
                if value is not None and value == value.to_integral_value() and 1 <= value <= 2000:
                    return int(value)
        return None

    @staticmethod
    def _ld_displacement(engine: dict[str, Any] | None) -> int | None:
        quantity = scalar(_get(engine, "engineDisplacement"))
        if not isinstance(quantity, dict) or (text_of(quantity.get("unitCode")) or "").upper() != "CMQ":
            return None
        value = decimal_from_json(scalar(quantity.get("value")), kind="count")
        if value is None or value != value.to_integral_value() or not 50 <= value <= 10000:
            return None
        return int(value)

    def _detail_fuel(
        self,
        nd: dict[str, Any],
        raw: dict[str, Any],
        ld: dict[str, Any] | None,
        engine: dict[str, Any] | None,
        facts: _Facts,
    ) -> Fuel:
        code = (
            _str(_get(nd, "fuelCategory", "raw")) or _str(_get(raw, "fuels", "fuelCategory", "raw")) or ""
        ).casefold()
        by_code = FUEL_CODES.get(code)
        plugin = _get(raw, "fuels", "isPluginHybrid", "raw")
        if by_code in (Fuel.HYBRID_PETROL, Fuel.HYBRID_DIESEL) and plugin is True:
            by_code = Fuel.PLUGIN_HYBRID
        ld_text = text_of(_get(engine, "fuelType")) or text_of(_get(ld, "fuelType"))
        by_text = fuel_word(ld_text)
        if by_code is not None:
            if (
                by_text is not None
                and by_text != by_code
                and not (
                    by_code == Fuel.PLUGIN_HYBRID and by_text in (Fuel.HYBRID_PETROL, Fuel.HYBRID_DIESEL)
                )
            ):
                facts.conflict(
                    "vehicle.fuel",
                    [by_code.value, by_text.value],
                    [f"{_DETAIL_PROV}.vehicle.fuelCategory", "json_ld:vehicleEngine.fuelType"],
                )
            facts.prov("vehicle.fuel", f"{_DETAIL_PROV}.vehicle.fuelCategory.raw", code)
            return by_code
        if by_text is not None:
            facts.prov(
                "vehicle.fuel", "json_ld:vehicleEngine.fuelType", ld_text, confidence=Confidence.MEDIUM
            )
            return by_text
        if code or ld_text:
            facts.warn("FUEL_UNMAPPED")
        return Fuel.UNKNOWN

    @staticmethod
    def _detail_drive(
        nd: dict[str, Any], raw: dict[str, Any], ld: dict[str, Any] | None, facts: _Facts
    ) -> Drive:
        candidates = (
            (_str(nd.get("driveTrain")), f"{_DETAIL_PROV}.vehicle.driveTrain"),
            (
                _str(_get(raw, "engine", "driveTrain", "formatted")),
                f"{_DETAIL_PROV}.vehicle.rawData.engine.driveTrain",
            ),
            (text_of(_get(ld, "driveWheelConfiguration")), "json_ld:Car.driveWheelConfiguration"),
        )
        seen = False
        for text, selector in candidates:
            if not text:
                continue
            seen = True
            mapped = drive_word(text)
            if mapped is not None:
                facts.prov(
                    "vehicle.drive",
                    selector,
                    text,
                    confidence=Confidence.MEDIUM,
                    transformation=f"localised label -> {mapped.value}; permanent/selectable unverified",
                )
                return mapped
        if seen:
            facts.warn("DRIVE_UNMAPPED")
        return Drive.UNKNOWN

    @staticmethod
    def _detail_body(
        raw: dict[str, Any], nd: dict[str, Any], ld: dict[str, Any] | None, facts: _Facts
    ) -> BodyType:
        code = _str(_get(raw, "bodyType", "raw"))
        mapped = _BODY_RAW.get(name_key(code)) if code else None
        if mapped is not None:
            facts.prov(
                "vehicle.body_type",
                f"{_DETAIL_PROV}.vehicle.rawData.bodyType.raw",
                code,
                confidence=Confidence.LOW,
                transformation="site category; SUV/off-road/pick-up are one category",
            )
            return mapped
        text = _str(nd.get("bodyType")) or text_of(_get(ld, "bodyType"))
        if text and _BODY_TEXT.search(text):
            facts.prov(
                "vehicle.body_type",
                "json_ld:Car.bodyType",
                text,
                confidence=Confidence.LOW,
                transformation="site category; SUV/off-road/pick-up are one category",
            )
            return BodyType.SUV
        if code or text:
            facts.warn("BODY_TYPE_UNMAPPED")
        return BodyType.UNKNOWN

    def _detail_price(
        self, details: Any, ld: dict[str, Any] | None, description_text: str, facts: _Facts
    ) -> PriceInfo:
        public = _get(details, "prices", "public")
        offer = next(iter(x for x in _as_list(_get(ld, "offers")) if isinstance(x, dict)), None)
        nd_amount = _int(_get(public, "priceRaw"))
        ld_amount = _int(scalar(_get(offer, "price")))
        ld_currency = (text_of(_get(offer, "priceCurrency")) or "").upper() or None
        self._cross(
            facts, "price.amount_minor", nd_amount, ld_amount if ld_currency in (None, "EUR") else None
        )
        if _get(public, "onRequestOnly") is True:
            facts.warn("PRICE_ON_REQUEST")
            return PriceInfo(type=PriceType.PRICE_ON_REQUEST)
        amount = nd_amount if nd_amount is not None else (ld_amount if ld_currency == "EUR" else None)
        if amount is None:
            facts.warn("PRICE_MISSING")
            return PriceInfo()
        problem = price_problem(amount)
        if problem is not None:
            facts.warn(problem)
            return PriceInfo(raw_text=str(amount))
        facts.prov(
            "price.amount_minor",
            f"{_DETAIL_PROV}.prices.public.priceRaw" if nd_amount is not None else "json_ld:Offer.price",
            amount,
            transformation="integer EUR x 100",
        )
        # The same flags as on the card: a conditional price ("Prezzo con condizioni", only with
        # the dealer's financing or trade-in) is no full asking price, so the detail page must not
        # undo the card's protection. ``prices.isFinalPrice`` false means extra costs may apply.
        price_type = PriceType.FULL_VEHICLE_ASKING
        if _get(details, "price", "isConditionalPrice") is True:
            price_type = PriceType.UNKNOWN
            facts.warn("PRICE_CONDITIONAL")
        if _get(details, "prices", "isFinalPrice") is False:
            facts.warn("PRICE_NOT_FINAL")
        negotiable = _get(public, "negotiable")
        basis = PriceBasis.UNKNOWN
        vat = VatTreatment.UNKNOWN
        net_minor: int | None = None
        net_raw = _int(_get(public, "netPriceRaw"))
        if net_raw is not None and net_raw > 0:
            net_minor = net_raw * 100
            facts.prov("price.net_amount_minor", f"{_DETAIL_PROV}.prices.public.netPriceRaw", net_raw)
        if _get(public, "taxDeductible") is True:
            vat = VatTreatment.VAT_SHOWN
            facts.prov(
                "price.vat_treatment",
                f"{_DETAIL_PROV}.prices.public.taxDeductible",
                True,
                confidence=Confidence.MEDIUM,
                transformation="site flag 'VAT deductible'; gross or net is not stated",
            )
            facts.warn("VAT_DEDUCTIBLE_FLAG")
        if description_text and _NET_WORDING.search(description_text):
            # The seller says the advertised price excludes VAT: a landed-cost risk worth flagging.
            basis = PriceBasis.NET
            facts.prov(
                "price.basis",
                f"{_DETAIL_PROV}.description",
                "net-price wording",
                method=ExtractionMethod.REGEX,
                confidence=Confidence.LOW,
                transformation="seller wording; the description itself is not stored",
            )
            facts.warn("SELLER_TEXT_NET_PRICE_WORDING")
        return PriceInfo(
            raw_text=str(amount),
            amount_minor=amount * 100,
            currency="EUR",
            basis=basis,
            type=price_type,
            negotiable=Tristate.YES
            if negotiable is True
            else Tristate.NO
            if negotiable is False
            else Tristate.UNKNOWN,
            vat_treatment=vat,
            net_amount_minor=net_minor,
        )

    @staticmethod
    def _detail_co2(raw: dict[str, Any], fuel: Fuel, facts: _Facts) -> Co2Info:
        co2 = _get(raw, "fuels", "primary", "co2emissionInGramPerKmWithFallback")
        value = _get(co2, "raw")
        if (
            not isinstance(value, int | Decimal)
            or isinstance(value, bool)
            or _get(co2, "isFallback") is not False
        ):
            return Co2Info()
        grams = Decimal(value)
        if grams <= 0:
            if fuel not in (Fuel.ELECTRIC, Fuel.UNKNOWN):
                facts.warn("CO2_ZERO_FOR_COMBUSTION")
            return Co2Info()
        if grams > 1000:
            return Co2Info()
        facts.prov("co2.g_per_km", f"{_DETAIL_PROV}.vehicle.rawData.fuels.primary.co2emission", plain(grams))
        return Co2Info(g_per_km=grams, cycle=Co2Cycle.UNKNOWN)

    # ------------------------------------------------------------------ helpers

    def _listing_id(self, canonical: str) -> str | None:
        path = urlsplit(canonical).path
        if not path.startswith(self.DETAIL_PREFIX):
            return None
        match = _URL_UUID_TAIL.search(path)
        return match.group(1) if match else None

    def _denied_document(self, url: str, reason: str) -> RawDocument:
        now = self._clock.now()
        return RawDocument(
            url=url[:2048],
            final_url=None,
            fetched_at=now,
            fetch=FetchOutcome(
                requested_url=url[:2048],
                success=False,
                access_state=AccessState.POLICY_DENIED,
                error_code="POLICY_DENIED",
                error_message=reason,
                fetched_at=now,
            ),
        )

    @staticmethod
    def _failed_page(
        request: SearchRequest, document: RawDocument, cls: AccessClassification
    ) -> DiscoveryPage:
        state = cls.access_state
        completeness = (
            Completeness.BLOCKED
            if state in (AccessState.ACCESS_BLOCKED, AccessState.POLICY_DENIED)
            else Completeness.FAILED
        )
        page_type: PageType = cls.page_type
        return DiscoveryPage(
            request=request,
            observations=(),
            has_more=False,
            completeness=completeness,
            access_state=state,
            access_evidence=bounded(cls.evidence, 500),
            fetched_at=document.fetched_at,
            fetch=document.fetch,
            page_type=page_type,
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class AutoScout24ItAdapter(AutoScout24Adapter):
    """AutoScout24 Italy: search cards and ``/annunci/`` detail pages (robots.txt allows both)."""

    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_it"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Italy public listings"
    COUNTRY: ClassVar[str] = "IT"
    DETAIL_PREFIX: ClassVar[str] = "/annunci/"
    DETAIL_PAGES_ALLOWED: ClassVar[bool] = True
    LANGUAGE: ClassVar[str] = "it"


class AutoScout24DeAdapter(AutoScout24Adapter):
    """AutoScout24 Germany: search cards only (robots.txt disallows ``/angebote/`` detail pages)."""

    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_de"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Germany public listings"
    COUNTRY: ClassVar[str] = "DE"
    DETAIL_PREFIX: ClassVar[str] = "/angebote/"
    DETAIL_PAGES_ALLOWED: ClassVar[bool] = False
    LANGUAGE: ClassVar[str] = "de"


AUTOSCOUT24_ADAPTERS: tuple[type[AutoScout24Adapter], ...] = (AutoScout24ItAdapter, AutoScout24DeAdapter)

__all__ = [
    "ADAPTER_VERSION",
    "AUTOSCOUT24_ADAPTERS",
    "FUEL_CODES",
    "AutoScout24Adapter",
    "AutoScout24DeAdapter",
    "AutoScout24ItAdapter",
    "CardResult",
    "ModelSearch",
    "NextData",
    "SearchParse",
    "name_key",
    "parse_models",
    "price_problem",
    "read_next_data",
]
