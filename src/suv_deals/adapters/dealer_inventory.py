"""Generic schema.org dealer-inventory adapter (spec sections 5, 7, 8, 9, 10, 17, 24, 25).

`SchemaOrgDealerAdapter` reads dealer inventory websites that publish schema.org
structured data. It relies only on the published schema.org vocabulary
(`Car`/`Vehicle`/`Product` with `offers`, `ItemList` on result pages,
`QuantitativeValue` unit codes KMT/SMI/CMQ/KWT/BHP, `ItemAvailability`,
`PriceSpecification.valueAddedTaxIncluded`, `DriveWheelConfigurationValue`) plus
generic page wording for VAT/negotiation/removal/challenge signals. It contains no
site-specific selectors, search URLs or ID patterns: every host, path pattern, search
URL and (optional) ID regex comes from the per-dealer `SourceConfig`, which stays
disabled until the activation checklist in docs/source_access_register.md is done.

Search config keys (`SourceConfig.search`):
- `search_url` (required for discovery): absolute URL; must satisfy the search path policy.
- `page_param` (optional): query parameter carrying the page number (for page numbering only;
  pagination itself follows explicit `rel=next` links).
- `detail_id_regex` (optional): regex with a named group `id`, verified by the operator for this
  dealer, applied to the canonical detail URL path to derive a provider listing ID.
- `provides_listing_ids` (optional, "true"/"false"): whether the dealer publishes stable IDs.

Seller text (descriptions, titles) is untrusted data: it is bounded, stripped of markup,
flagged when it looks like prompt injection and never interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from suv_deals.adapters._access import (
    REMOVED_MARKERS,
    AccessClassification,
    classify_document,
    has_empty_result_marker,
)
from suv_deals.adapters._extract import (
    Anchor,
    MileageMatch,
    PageView,
    bounded,
    clean_seller_text,
    collapse_ws,
    decimal_from_json,
    enum_name,
    find_labelled_odometer,
    find_mileages,
    find_money,
    find_power,
    html_to_text,
    injection_signals,
    json_number_is_ambiguous,
    local_type_names,
    miles_to_km,
    normalize_vin,
    parse_page,
    parse_partial_date,
    parse_source_timestamp,
    plain,
    power_from_value,
    round_half_up_int,
    scalar,
    text_of,
    year_from,
)
from suv_deals.adapters._health import assess_samples
from suv_deals.adapters._policy import UrlPolicy, build_identity, clean_provider_id
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
    PriceBasis,
    PriceType,
    SellerType,
    SourceMode,
    Tristate,
    VatTreatment,
)
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
    sha256_json,
)
from suv_deals.domain.money import CURRENCY_EXPONENTS, Money
from suv_deals.domain.parsing import (
    MAX_INSPECTION_TEXT_LENGTH,
    InspectionParse,
    locale_for_country,
    parse_inspection,
)
from suv_deals.domain.profiles import SearchProfile
from suv_deals.domain.provenance import FieldConflict, FieldProvenance
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import ValidationFailed

ADAPTER_KEY = "schemaorg_dealer"
# 1.1.0: technical-inspection wording (condition.roadworthy, documentation.inspection_expiry),
# Swiss "ohne MWST"/"Export ohne MWST" and Swiss-French TVA/export price wording.
ADAPTER_VERSION = "schemaorg_dealer@1.1.0"

# Sanity bounds (ENGINEERING DEFAULTS), not business rules: values outside them are data errors.
# 100 million major units keeps every supported currency (incl. MKD) far inside a bigint.
MAX_PLAUSIBLE_PRICE = Decimal(100_000_000)
MAX_PLAUSIBLE_MILEAGE_KM = Decimal(3_000_000)

_SEARCH_KEYS = frozenset({"search_url", "page_param", "detail_id_regex", "provides_listing_ids"})
_VEHICLE_TYPES = frozenset({"Car", "Vehicle", "MotorizedVehicle"})
_PRODUCT_TYPES = frozenset({"Product", "IndividualProduct", "SomeProducts"})
_DEALER_TYPES = frozenset(
    {
        "AutoDealer",
        "AutomotiveBusiness",
        "AutoRental",
        "AutoRepair",
        "Organization",
        "LocalBusiness",
        "Corporation",
        "Store",
        "OnlineBusiness",
    }
)
_KM_UNIT_CODES = frozenset({"kmt", "km", "kilometer", "kilometers", "kilometre", "kilometres"})
_MI_UNIT_CODES = frozenset({"smi", "mi", "mile", "miles"})

_COUNTRY_NAMES: dict[str, str] = {
    "deutschland": "DE",
    "germany": "DE",
    "germania": "DE",
    "italia": "IT",
    "italy": "IT",
    "italien": "IT",
    "schweiz": "CH",
    "suisse": "CH",
    "svizzera": "CH",
    "switzerland": "CH",
    "österreich": "AT",
    "austria": "AT",
    "france": "FR",
    "frankreich": "FR",
    "nederland": "NL",
    "belgique": "BE",
    "belgië": "BE",
    "slovenija": "SI",
    "hrvatska": "HR",
    "polska": "PL",
    "česko": "CZ",
    "españa": "ES",
    "north macedonia": "MK",
    "северна македонија": "MK",
}

# --------------------------------------------------------------------------- wording rules


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_GROSS_WORDING = _rx(
    r"inkl\.?\s*(\d{1,2}(?:[.,]\d)?\s*%\s*)?(mwst|mwst\.|ust|mehrwertsteuer)",
    r"inklusive\s+(\d{1,2}(?:[.,]\d)?\s*%\s*)?(mwst|mehrwertsteuer)",
    r"incl\.?\s*(\d{1,2}(?:[.,]\d)?\s*%\s*)?(vat|tva)",
    r"including\s+vat",
    r"\btva\s+(\d{1,2}(?:[.,]\d)?\s*%\s*)?(incluse|comprise)",
    r"iva\s+(inclusa|compresa)",
    r"\bprezzo\s+ivato\b",
    r"bruttopreis",
    r"\bttc\b",
)
_NET_WORDING = _rx(
    r"zzgl\.?\s*(\d{1,2}(?:[.,]\d)?\s*%\s*)?(mwst|ust|mehrwertsteuer)",
    r"exkl\.?\s*(\d{1,2}(?:[.,]\d)?\s*%\s*)?(mwst|ust)",
    r"nettopreis",
    r"netto\s*preis",
    r"preis\s+netto",
    r"\+\s*(\d{1,2}(?:[.,]\d)?\s*%\s*)?(mwst|iva|vat)\b",
    r"plus\s+vat",
    r"excl\.?\s*(vat|tva)",
    # Swiss "Export ohne MWST" / "Preis ohne MWST"; "ohne MWST-Ausweis" (VAT not shown) is not net.
    r"\bohne\s+(mwst|mehrwertsteuer|ust)\b(?!\s?-?\s?ausweis)",
    r"\bhors\s+tva\b",
    r"\btva\s+en\s+sus\b",
    r"iva\s+esclusa",
    r"oltre\s+iva",
    r"\bhors\s+taxes?\b",
)
_VAT_SHOWN_WORDING = _rx(
    r"(mwst|ust)\.?\s*ausweisbar",
    r"ausweisbare\s+(mwst|mehrwertsteuer)",
    r"iva\s+esposta",
    r"iva\s+deducibile",
    r"vat\s+(qualifying|deductible|reclaimable)",
    r"tva\s+r[ée]cup[ée]rable",
)
_MARGIN_WORDING = _rx(
    r"differenzbesteuert",
    r"differenzbesteuerung",
    r"§\s*25\s*a",
    r"regime\s+del\s+margine",
    r"iva\s+non\s+esposta",
    r"margin\s+scheme",
    r"r[ée]gime\s+de\s+la\s+marge",
    r"tva\s+sur\s+marge",
)
_PRIVATE_SALE_WORDING = _rx(r"privatverkauf", r"vendita\s+privata", r"private\s+sale", r"vente\s+priv[ée]e")
_VAT_RATE = re.compile(
    r"(?P<a>\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:mwst|ust|iva|vat|tva|mehrwertsteuer)"
    r"|(?:mwst|ust|iva|vat|tva)\.?\s*(?P<b>\d{1,2}(?:[.,]\d{1,2})?)\s*%",
    re.IGNORECASE,
)
_NEGOTIABLE = _rx(
    r"\bvb\b",
    r"verhandlungsbasis",
    r"verhandelbar",
    r"(?<!non )\btrattabil[ei]\b",
    r"(?<!non )\bnegoziabil[ei]\b",
    r"(?<!not )\bnegotiable\b",
    r"[àa]\s+d[ée]battre",
)
_FIXED_PRICE = _rx(
    r"festpreis",
    r"nicht\s+verhandelbar",
    r"non\s+trattabil[ei]",
    r"prezzo\s+fisso",
    r"fixed\s+price",
    r"not\s+negotiable",
    r"prix\s+ferme",
)
_ON_REQUEST = _rx(
    r"preis\s*:?\s*auf\s+anfrage",
    r"prezzo\s+su\s+richiesta",
    r"trattativa\s+riservata",
    r"prix\s+sur\s+demande",
    r"price\s+(up)?on\s+request",
)
_EXPORT_PRICE = _rx(
    r"exportpreis",
    r"export-preis",
    r"export\s+netto",
    r"h[äa]ndler\s*-?\s*/?\s*exportpreis",
    r"prezzo\s+(per\s+(l'?)?)?export",
    r"export\s+price",
    r"\bexport\s+ohne\s+(mwst|mehrwertsteuer|ust)\b",
    r"\bprix\s+([àa]\s+l['\u2019]\s*)?export",
)
_SELLER_FEES = _rx(
    r"zzgl\.?\s+(überführung|ueberfuehrung|zulassung|bereitstellung)",
    r"überführungskosten",
    r"bereitstellungsgebühr",
    r"spese\s+di\s+passaggio",
    r"messa\s+su\s+strada",
    r"frais\s+de\s+mise\s+[àa]\s+disposition",
)
# Negations matter: "nicht unfallfrei" is a damage statement, "kein Unfallwagen" an accident-free one.
_ACCIDENT_FREE = _rx(
    r"(?<!nicht )(?<!not )\bunfallfrei\b",
    r"\bkein(?:e|en)?\s+(?:unfallwagen|unfallfahrzeug|unfallsch[äa]den|unfallschaden)\b",
    r"\bnon\s+incidentat[ao]\b",
    r"(?<!not )(?<!non )\baccident[- ]free\b",
    r"\bkeine\s+unf[äa]lle\b",
    r"\bnessun\s+incidente\b",
    r"\bno\s+accident\s+damage\b",
)
_ACCIDENT_DAMAGE = _rx(
    r"\bnicht\s+unfallfrei\b",
    r"\bnot\s+accident[- ]free\b",
    r"(?<!kein )(?<!keine )(?<!keinen )\bunfallwagen\b",
    r"(?<!kein )(?<!keine )(?<!keinen )\bunfallsch[äa]den\b",
    r"(?<!kein )(?<!keine )(?<!keinen )\bunfallfahrzeug\b",
    r"(?<!non )\bincidentat[ao]\b",
    r"(?<!no )\baccident\s+damage\b",
)
_CO2_WLTP = _rx(r"\bwltp\b")
_CO2_NEDC = _rx(r"\bnedc\b", r"\bnefz\b")

_BODY_RULES: tuple[tuple[BodyType, re.Pattern[str]], ...] = (
    (BodyType.SUV, _rx(r"\bsuv\b")),
    (BodyType.PICKUP, _rx(r"\bpick[- ]?up\b")),
    (
        BodyType.OFFROAD,
        _rx(r"gel[äa]ndewagen", r"\boff[- ]?road", r"fuoristrada", r"\b4x4\b", r"tout[- ]terrain"),
    ),
    (BodyType.CROSSOVER, _rx(r"crossover")),
    (
        BodyType.ESTATE,
        _rx(r"\bkombi\b", r"\bestate\b", r"station\s?wagon", r"\bwagon\b", r"familiare", r"\bbreak\b"),
    ),
    (BodyType.SEDAN, _rx(r"\blimousine\b", r"\bsedan\b", r"\bsaloon\b", r"\bberlina\b")),
    (BodyType.HATCHBACK, _rx(r"kleinwagen", r"hatchback", r"kompaktklasse", r"utilitaria", r"schr[äa]gheck")),
    (BodyType.VAN, _rx(r"\bvan\b", r"\bminivan\b", r"monovolume", r"\bbus\b", r"kleinbus", r"transporter")),
    (BodyType.OTHER, _rx(r"cabrio", r"convertible", r"coup[ée]", r"roadster", r"\bspider\b", r"\bspyder\b")),
)
_FUEL_RULES: tuple[tuple[Fuel, re.Pattern[str]], ...] = (
    (Fuel.PLUGIN_HYBRID, _rx(r"plug[- ]?in", r"\bphev\b")),
    (
        Fuel.HYBRID_DIESEL,
        _rx(
            r"hybrid.{0,20}diesel",
            r"diesel.{0,20}(hybrid|elektro|electric|elettric)",
            r"(elektro|electric|elettric).{0,20}(diesel|gasolio)",
            r"ibrid[oa].{0,20}(diesel|gasolio)",
        ),
    ),
    (
        Fuel.HYBRID_PETROL,
        _rx(
            r"hybrid.{0,20}(benzin|petrol|gasoline|benzina)",
            r"(benzin|petrol|gasoline|benzina).{0,20}(hybrid|elektro|electric|elettric)",
            r"(elektro|electric|elettric).{0,20}(benzin|petrol|gasoline|benzina)",
            r"ibrid[oa].{0,20}benzina",
        ),
    ),
    (Fuel.LPG, _rx(r"\blpg\b", r"\bgpl\b", r"autogas", r"fl[üu]ssiggas")),
    (Fuel.CNG, _rx(r"\bcng\b", r"erdgas", r"metano", r"\bgnv\b")),
    (Fuel.DIESEL, _rx(r"diesel", r"gasolio", r"gazole")),
    (Fuel.PETROL, _rx(r"benzin", r"petrol", r"gasoline", r"benzina", r"essence")),
    (Fuel.ELECTRIC, _rx(r"elektr", r"electric", r"elettric", r"\bbev\b")),
)
_GEARBOX_RULES: tuple[tuple[Gearbox, re.Pattern[str]], ...] = (
    (
        Gearbox.SEMI_AUTOMATIC,
        _rx(r"halbautomat", r"semi[- ]?auto", r"sequen[tz]", r"robotizzat", r"automatisiertes\s+schalt"),
    ),
    (
        Gearbox.AUTOMATIC,
        _rx(
            r"automat",
            r"\bdsg\b",
            r"\bdct\b",
            r"\bcvt\b",
            r"tiptronic",
            r"steptronic",
            r"doppelkupplung",
            r"s[- ]?tronic",
            r"dual[- ]clutch",
            r"\bpdk\b",
        ),
    ),
    (Gearbox.MANUAL, _rx(r"schalt", r"manual", r"manuell", r"manuale", r"meccanic", r"mechanisch")),
)
_GEARBOX_SUBTYPE = re.compile(
    r"\b(DSG|DCT|CVT|PDK|Tiptronic|Steptronic|S[- ]?tronic|\d-Gang|\d-speed|\d\s+marce)\b", re.IGNORECASE
)
_DRIVE_ENUM: dict[str, Drive] = {
    "allwheeldriveconfiguration": Drive.AWD,
    "fourwheeldriveconfiguration": Drive.FOUR_WD,
    "frontwheeldriveconfiguration": Drive.FWD,
    "rearwheeldriveconfiguration": Drive.RWD,
}
_DRIVE_RULES: tuple[tuple[Drive, re.Pattern[str]], ...] = (
    (Drive.FOUR_WD, _rx(r"zuschaltbar", r"selectable", r"part[- ]time", r"inseribile")),
    (
        Drive.AWD,
        _rx(
            r"allrad",
            r"all[- ]wheel",
            r"\bawd\b",
            r"integrale",
            r"quattro",
            r"4motion",
            r"xdrive",
            r"4matic",
            r"\b4x4\b",
            r"four[- ]wheel",
            r"\b4wd\b",
        ),
    ),
    (Drive.FWD, _rx(r"front", r"vorderrad", r"anteriore", r"\bfwd\b")),
    (Drive.RWD, _rx(r"\brear\b", r"hinterrad", r"posteriore", r"\brwd\b", r"heckantrieb")),
)
_AVAILABILITY: dict[str, Availability] = {
    "instock": Availability.AVAILABLE,
    "instoreonly": Availability.AVAILABLE,
    "onlineonly": Availability.AVAILABLE,
    "limitedavailability": Availability.AVAILABLE,
    "reserved": Availability.RESERVED,
    "soldout": Availability.SOLD_CLAIMED,
    "discontinued": Availability.REMOVED,
}
_DROPPED_NOTE = "result link(s) outside host/path policy"
_AVAILABILITY_UNCLEAR = frozenset({"outofstock", "preorder", "presale", "backorder", "madetoorder"})


def _first_rule[T](rules: tuple[tuple[T, re.Pattern[str]], ...], text: str) -> T | None:
    for value, regex in rules:
        if regex.search(text):
            return value
    return None


# --------------------------------------------------------------------------- JSON-LD helpers


def _get(node: dict[str, Any] | None, *keys: str) -> Any:
    if not node:
        return None
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _bool(value: Any) -> bool | None:
    value = scalar(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _quantity(value: Any) -> tuple[Any, str | None]:
    """(raw value, unit code/text) of a QuantitativeValue or bare scalar."""
    value = scalar(value)
    if isinstance(value, dict):
        unit = text_of(value.get("unitCode")) or text_of(value.get("unitText"))
        return scalar(value.get("value")), unit
    return value, None


def _label(value: Any) -> str | None:
    """Human label (make/model/trim). A bare IRI such as a node's `@id` is not a label."""
    text = text_of(value)
    if text is None or re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE):
        return None
    return html_to_text(text).text or None


def _raw_repr(value: Any, unit: str | None = None) -> str:
    text = value if isinstance(value, str) else str(value)
    return f"{text} {unit}" if unit else text


def _provider_id(node: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(id, json-ld key) from identifier/sku/productID/vehicleIdentification-free keys."""
    for key in ("identifier", "sku", "productID", "mpn"):
        raw = _get(node, key)
        if raw is None:
            continue
        value = raw
        if isinstance(scalar(raw), dict):
            value = text_of(scalar(raw).get("value")) or text_of(scalar(raw).get("@value"))
        cleaned = clean_provider_id(scalar(value))
        if cleaned:
            return cleaned, key
    return None, None


def _price_plausible(amount: Decimal) -> bool:
    """Sanity bound only (keeps minor units inside a PostgreSQL bigint); not a business filter."""
    return Decimal(0) < amount <= MAX_PLAUSIBLE_PRICE


def _mileage_plausible(km: Decimal) -> bool:
    return Decimal(0) <= km <= MAX_PLAUSIBLE_MILEAGE_KM


def _money_minor(amount: Decimal, currency: str) -> int | None:
    if currency not in CURRENCY_EXPONENTS or not _price_plausible(amount):
        return None
    try:
        return Money.of(amount, currency).to_minor()
    except ValidationFailed:
        return None


def _inspection_note(result: InspectionParse, text: str) -> str:
    kind = "" if result.inspection_kind == "unknown" else f"{result.inspection_kind}: "
    return f"{kind}{text}"


# --------------------------------------------------------------------------- results


@dataclass(slots=True)
class _Collector:
    source_url: str
    snapshot_id: UUID | None
    observed_at: datetime
    provenance: dict[str, FieldProvenance] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[FieldConflict] = field(default_factory=list)

    def prov(
        self,
        name: str,
        method: ExtractionMethod,
        confidence: Confidence,
        *,
        selector: str | None = None,
        raw: object = None,
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


_CONVERSION_TOLERANCE = Decimal("0.005")  # km vs miles statements of the same reading
_ESTIMATE_TOLERANCE = Decimal("0.05")  # "ca. 150.000 km" / "150 Tkm" against an exact reading


def _mileage_value(mention: MileageMatch) -> str:
    """Plain km decimal of a text statement ("187500"); a range is "low-high"."""
    low = mention.range_low_km
    return plain(mention.km) if low is None else f"{plain(low)}-{plain(mention.km)}"


@dataclass(frozen=True, slots=True)
class _MileageStatement:
    """The structured odometer statement: an exact reading or a range (km, unrounded)."""

    exact_km: Decimal | None
    low_km: Decimal | None
    high_km: Decimal
    unit: str = "km"
    exact_amount: Decimal | None = None  # as written, in `unit`

    @property
    def value(self) -> str:
        if self.exact_km is not None:
            return plain(self.exact_km)
        assert self.low_km is not None
        return f"{plain(self.low_km)}-{plain(self.high_km)}"

    def consistent_with(self, mention: MileageMatch) -> bool:
        if self.exact_km is not None and mention.range_low is None and not mention.is_estimate:
            if mention.unit == self.unit and self.exact_amount is not None:
                return mention.amount == self.exact_amount
            return abs(self.exact_km - mention.km) <= self.exact_km * _CONVERSION_TOLERANCE
        tolerance = _ESTIMATE_TOLERANCE if mention.is_estimate else _CONVERSION_TOLERANCE
        own_low = self.exact_km if self.exact_km is not None else self.low_km
        assert own_low is not None
        their_low = mention.range_low_km if mention.range_low_km is not None else mention.km
        # Intervals overlap (an exact reading is a zero-width interval).
        return own_low <= mention.km * (1 + tolerance) and their_low * (1 - tolerance) <= self.high_km


@dataclass(frozen=True, slots=True)
class _Card:
    canonical_url: str
    item: dict[str, Any] | None
    anchor: Anchor | None


@dataclass(frozen=True, slots=True)
class SearchExtraction:
    """Diagnostics of one search page (exposed for parser health and tests)."""

    observations: tuple[SearchObservation, ...]
    item_list_found: bool
    item_list_entries: int
    reported_count: int | None
    dropped_links: int
    empty_marker: str | None


# --------------------------------------------------------------------------- adapter


class SchemaOrgDealerAdapter:
    """Standards-based adapter for one dealer website configured by a SourceConfig."""

    ADAPTER_KEY: ClassVar[str] = ADAPTER_KEY
    ADAPTER_VERSION: ClassVar[str] = ADAPTER_VERSION
    SUPPORTED_MODES: ClassVar[frozenset[SourceMode]] = frozenset({SourceMode.PUBLIC_HTML, SourceMode.FIXTURE})

    def __init__(self, config: SourceConfig, *, clock: Clock | None = None) -> None:
        if config.adapter != ADAPTER_KEY:
            raise ValidationFailed(f"source {config.source_key} is not configured for {ADAPTER_KEY}")
        unknown = set(config.search) - _SEARCH_KEYS
        if unknown:
            raise ValidationFailed(
                f"source {config.source_key}: unsupported search keys {sorted(unknown)}",
                details={"allowed": sorted(_SEARCH_KEYS)},
            )
        try:
            ZoneInfo(config.source_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationFailed(f"source {config.source_key}: unknown source_timezone") from None
        self.config = config
        self.source_key: str = config.source_key
        self.adapter_version: str = ADAPTER_VERSION
        self.policy = UrlPolicy(config)
        self._clock: Clock = clock or SystemClock()
        self._search_url = config.search.get("search_url") or None
        self._page_param = config.search.get("page_param") or None
        self._provides_ids = config.search.get("provides_listing_ids", "false").strip().lower() == "true"
        id_regex = config.search.get("detail_id_regex")
        self._id_regex: re.Pattern[str] | None = None
        if id_regex:
            try:
                self._id_regex = re.compile(id_regex)
            except re.error:
                raise ValidationFailed(f"source {config.source_key}: invalid detail_id_regex") from None
            if "id" not in self._id_regex.groupindex:
                raise ValidationFailed(
                    f"source {config.source_key}: detail_id_regex needs a named group 'id'"
                )

    # ------------------------------------------------------------------ contract

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            coverage_mode=CoverageMode.ROLLING_PAGES,
            provides_listing_ids=self._provides_ids,
            supports_modified_since=False,
            supports_stable_sort=False,
            has_cursor_pagination=False,
            exposes_source_modified_at=False,
            detail_required_for_price=False,
            countries=(self.config.country,),
        )

    def build_search(self, profile: SearchProfile, cursor: str | None) -> SearchRequest:
        """Search request for the configured result page, or for a previously returned next URL.

        The profile's price/mileage bounds are NOT translated into site query parameters:
        no generic, verified parameter names exist. Eligibility screening filters afterwards.
        """
        url = cursor or self._search_url
        if not url:
            raise ValidationFailed(f"source {self.source_key} has no search_url configured")
        canonical = self.policy.canonical(url)
        if not self.policy.is_search_url(canonical):
            raise ValidationFailed(f"source {self.source_key}: search URL is outside the search path policy")
        return SearchRequest(
            source_key=self.source_key,
            profile_key=profile.key.value,
            url=canonical,
            page_number=self._page_number(canonical),
            cursor=cursor,
            params={k: v for k, v in parse_qsl(urlsplit(canonical).query, keep_blank_values=True)},
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
        return self.parse_search(request, document)

    def canonicalize(self, url: str) -> CanonicalIdentity:
        canonical = self.policy.canonical(url)
        if not self.policy.host_allowed(canonical):
            raise ValidationFailed(f"URL host is not allowed for source {self.source_key}")
        return build_identity(self.source_key, canonical, self._id_from_url(canonical))

    def identity_for(self, url: str, provider_id: str | None) -> CanonicalIdentity:
        """Identity using a provider ID observed on a card/detail page when available."""
        canonical = self.policy.canonical(url)
        if not self.policy.host_allowed(canonical):
            raise ValidationFailed(f"URL host is not allowed for source {self.source_key}")
        cleaned = clean_provider_id(provider_id) or self._id_from_url(canonical)
        return build_identity(self.source_key, canonical, cleaned)

    async def fetch_detail(self, identity: CanonicalIdentity, client: CrawlClient) -> RawDocument:
        if identity.source_key != self.source_key:
            raise ValidationFailed("identity belongs to another source")
        if not self.policy.is_detail_url(identity.canonical_url):
            return self._denied_document(
                identity.canonical_url, "detail URL outside the source detail policy"
            )
        return await client.fetch(identity.canonical_url, purpose="detail", source_key=self.source_key)

    def detect_access_state(self, document: RawDocument) -> AccessState:
        return self.classify(document).access_state

    def classify(self, document: RawDocument) -> AccessClassification:
        """Access classification with page type and a short evidence string."""
        page = parse_page(document.html)
        return classify_document(
            document,
            page,
            allowed_hosts=self.policy.hosts,
            content_present=self._content_present(document, page),
        )

    def parse_detail(self, document: RawDocument) -> ParsedListing:
        page = parse_page(document.html)
        nodes = self._vehicle_nodes(page) if page is not None else []
        cls = classify_document(document, page, allowed_hosts=self.policy.hosts, content_present=bool(nodes))
        if cls.access_state != AccessState.OK:
            return ParsedListing(
                page_type=cls.page_type,
                access_state=cls.access_state,
                errors=(bounded(cls.evidence, 300),) if cls.evidence else (),
                warnings=("LISTING_REMOVED_EXPLICIT",) if cls.access_state == AccessState.REMOVED else (),
            )
        assert page is not None  # classify_document returns non-OK for page None
        source_url = document.final_url or document.url
        if not self.policy.is_detail_url(source_url):
            # A detail URL that redirected to a result list, the home page or any other non-detail
            # path must never yield a listing: the vehicles on that page are different offers.
            # Absence of the original listing is not evidence of removal either.
            is_list = bool(self._item_lists(page)) or len(nodes) > 1 or self.policy.is_search_url(source_url)
            return ParsedListing(
                page_type="search" if is_list else "unknown",
                access_state=AccessState.UNEXPECTED_CONTENT,
                errors=("FINAL_URL_NOT_A_DETAIL_PAGE",),
            )
        vehicle = self._select_vehicle(nodes, source_url) if nodes else None
        if nodes and vehicle is None:
            return ParsedListing(
                page_type="detail",
                access_state=AccessState.OK,
                errors=(
                    f"AMBIGUOUS_VEHICLE_NODES: {len(nodes)} vehicle nodes and none identifies this page",
                ),
            )
        if vehicle is None:
            if self._item_lists(page):
                return ParsedListing(
                    page_type="search",
                    access_state=AccessState.UNEXPECTED_CONTENT,
                    errors=("EXPECTED_DETAIL_GOT_SEARCH",),
                )
            errors = ["NO_VEHICLE_STRUCTURED_DATA"]
            if page.json_ld.malformed:
                errors.insert(
                    0,
                    f"STRUCTURED_DATA_MALFORMED: {page.json_ld.malformed} of "
                    f"{page.json_ld.blocks} JSON-LD block(s)",
                )
            return ParsedListing(page_type="detail", access_state=AccessState.OK, errors=tuple(errors))
        try:
            listing = self._build_listing(document, page, vehicle)
        except (ValidationError, ValidationFailed) as exc:
            message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return ParsedListing(
                page_type="detail",
                access_state=AccessState.OK,
                errors=(
                    bounded(f"LISTING_VALIDATION_FAILED: {message}", 300) or "LISTING_VALIDATION_FAILED",
                ),
            )
        extra = (f"JSON_LD_MALFORMED_BLOCKS:{page.json_ld.malformed}",) if page.json_ld.malformed else ()
        return ParsedListing(
            page_type="detail",
            access_state=AccessState.OK,
            listing=listing,
            warnings=tuple(listing.warnings) + extra,
        )

    def assess_parser_health(self, samples: list[ParseOutcome]) -> ParserHealth:
        return assess_samples(samples)

    # ------------------------------------------------------------------ parse outcomes for health

    def detail_outcome(self, parsed: ParsedListing, observed_at: datetime) -> ParseOutcome:
        listing = parsed.listing
        return ParseOutcome(
            page_type=parsed.page_type,
            access_state=parsed.access_state,
            ok=listing is not None,
            listing_count=1 if listing is not None else 0,
            has_price=bool(listing and listing.price.amount_minor is not None),
            has_mileage=bool(listing and listing.vehicle.mileage_km is not None),
            has_make_model=bool(listing and listing.vehicle.make and listing.vehicle.model),
            currency=listing.price.currency if listing else None,
            price_minor=listing.price.amount_minor if listing else None,
            mileage_km=listing.vehicle.mileage_km if listing else None,
            observed_at=observed_at,
        )

    def discovery_outcome(
        self, page: DiscoveryPage, extraction: SearchExtraction | None = None
    ) -> ParseOutcome:
        return ParseOutcome(
            page_type=page.page_type,
            access_state=page.access_state,
            ok=page.access_state == AccessState.OK,
            listing_count=len(page.observations),
            unexpected_host=bool(extraction.dropped_links)
            if extraction is not None
            else _DROPPED_NOTE in (page.access_evidence or "")
            or (
                page.access_state == AccessState.UNEXPECTED_CONTENT
                and "policy" in (page.access_evidence or "")
            ),
            observed_at=page.fetched_at,
        )

    # ------------------------------------------------------------------ search parsing

    def parse_search(self, request: SearchRequest, document: RawDocument) -> DiscoveryPage:
        """Classify and parse one fetched search page (pure; used by `discover`)."""
        page = parse_page(document.html)
        base = document.final_url or document.url
        extraction = self.extract_search(page, base) if page is not None else None
        # Explicit empty-result wording counts as expected content: a footer newsletter reCAPTCHA
        # on a genuine "0 Treffer" page must not be read as a challenge that pauses the source.
        content_present = bool(
            extraction
            and (
                extraction.item_list_found
                or extraction.observations
                or extraction.dropped_links
                or extraction.empty_marker is not None
            )
        )
        cls = classify_document(
            document, page, allowed_hosts=self.policy.hosts, content_present=content_present
        )
        if cls.access_state != AccessState.OK:
            return self._failed_page(request, document, cls)
        assert page is not None and extraction is not None
        if base != request.url and not self.policy.is_search_url(base):
            # Redirected away from the search policy (detail page, home page with "featured"
            # cars, expired-search landing page): never a result page, never complete coverage.
            to_detail = self.policy.is_detail_url(base)
            return self._failed_page(
                request,
                document,
                AccessClassification(
                    AccessState.UNEXPECTED_CONTENT,
                    "detail" if to_detail else "unknown",
                    "search URL redirected to a detail page"
                    if to_detail
                    else "search URL redirected outside the search path policy",
                ),
            )
        observations = extraction.observations
        notes: list[str] = []
        if extraction.dropped_links:
            notes.append(f"dropped {extraction.dropped_links} {_DROPPED_NOTE}")
        if not observations:
            is_empty = (extraction.item_list_found and extraction.item_list_entries == 0) or (
                extraction.empty_marker is not None and not extraction.dropped_links
            )
            if not is_empty:
                reason = (
                    f"all {extraction.dropped_links} {_DROPPED_NOTE}"
                    if extraction.dropped_links
                    else "no result list, result links or empty-result marker found"
                )
                return self._failed_page(
                    request, document, AccessClassification(AccessState.UNEXPECTED_CONTENT, "unknown", reason)
                )
            notes.append("explicit empty result")
        try:
            current = self.policy.canonical(request.url)
        except ValidationFailed:
            current = request.url
        next_url, refused = self._next_url(page, base, current=current)
        if refused:
            notes.append(refused)
        has_more = next_url is not None
        if has_more:
            budget_pages = self.config.rate_budget.max_search_pages_per_run
            completeness = (
                Completeness.BUDGET_LIMITED if request.page_number >= budget_pages else Completeness.PARTIAL
            )
        elif refused:
            completeness = Completeness.PARTIAL
        elif (
            extraction.reported_count is not None
            and request.page_number == 1
            and extraction.reported_count > len(observations)
        ):
            completeness = Completeness.PARTIAL
            notes.append("reported result count exceeds observed cards and no next link exists")
        else:
            completeness = Completeness.COMPLETE
        return DiscoveryPage(
            request=request,
            observations=observations,
            next_url=next_url,
            has_more=has_more,
            completeness=completeness,
            access_state=AccessState.OK,
            access_evidence=bounded("; ".join(notes), 500) if notes else None,
            result_count_reported=extraction.reported_count,
            fetched_at=document.fetched_at,
            fetch=document.fetch,
            page_type="search",
        )

    def extract_search(self, page: PageView, base: str) -> SearchExtraction:
        anchors: dict[str, Anchor] = {}
        for anchor in page.anchors:
            url = self._detail_url(anchor.href, base)
            if url is not None and url not in anchors:
                anchors[url] = anchor
        item_lists = self._item_lists(page)
        cards: list[_Card] = []
        seen: set[str] = set()
        dropped = 0
        entries = 0
        reported: int | None = None
        for item_list in item_lists:
            count = scalar(item_list.get("numberOfItems"))
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                reported = count if reported is None else reported + count
            elements = item_list.get("itemListElement")
            element_list = elements if isinstance(elements, list) else [] if elements is None else [elements]
            for element in element_list:
                entries += 1
                href, item = self._element_url(element)
                url = self._detail_url(href, base) if href else None
                if url is None:
                    dropped += 1
                    continue
                if url in seen:
                    continue
                seen.add(url)
                cards.append(_Card(canonical_url=url, item=item, anchor=anchors.get(url)))
        if not item_lists:
            # Result pages may also publish one top-level Car/Vehicle/Product node per result.
            for node in self._vehicle_nodes(page):
                href = text_of(node.get("url"))
                url = self._detail_url(href, base) if href else None
                if url is not None and url not in seen:
                    seen.add(url)
                    cards.append(_Card(canonical_url=url, item=node, anchor=anchors.get(url)))
        if not item_lists and not cards:
            for url, anchor in anchors.items():
                cards.append(_Card(canonical_url=url, item=None, anchor=anchor))
        observations = tuple(self._observation(card, position) for position, card in enumerate(cards))
        return SearchExtraction(
            observations=observations,
            item_list_found=bool(item_lists),
            item_list_entries=entries,
            reported_count=reported,
            dropped_links=dropped,
            empty_marker=has_empty_result_marker(page),
        )

    # ------------------------------------------------------------------ helpers

    def _page_number(self, url: str) -> int:
        if not self._page_param:
            return 1
        for key, value in parse_qsl(urlsplit(url).query):
            if key == self._page_param and value.isdigit() and int(value) >= 1:
                return int(value)
        return 1

    def _id_from_url(self, canonical_url: str) -> str | None:
        if self._id_regex is None:
            return None
        match = self._id_regex.search(urlsplit(canonical_url).path)
        return clean_provider_id(match.group("id")) if match else None

    def _detail_url(self, href: str, base: str) -> str | None:
        try:
            url = self.policy.canonical(href, base=base)
        except ValidationFailed:
            return None
        return url if self.policy.is_detail_url(url) else None

    def _content_present(self, document: RawDocument, page: PageView | None) -> bool:
        if page is None:
            return False
        url = document.final_url or document.url
        has_vehicle = self._vehicle_node(page) is not None
        if self.policy.is_detail_url(url):
            return has_vehicle
        base = url
        has_results = (
            bool(self._item_lists(page))
            or any(self._detail_url(a.href, base) is not None for a in page.anchors)
            or has_empty_result_marker(page) is not None
        )
        return has_vehicle or has_results

    @staticmethod
    def _item_lists(page: PageView) -> list[dict[str, Any]]:
        return [n for n in page.json_ld.nodes if "ItemList" in local_type_names(n)]

    @staticmethod
    def _vehicle_nodes(page: PageView) -> list[dict[str, Any]]:
        nodes = page.json_ld.nodes
        vehicles = [n for n in nodes if local_type_names(n) & _VEHICLE_TYPES]
        products = [
            n
            for n in nodes
            if local_type_names(n) & _PRODUCT_TYPES
            and not local_type_names(n) & _VEHICLE_TYPES
            and n.get("offers") is not None
        ]
        return vehicles + products

    @classmethod
    def _vehicle_node(cls, page: PageView) -> dict[str, Any] | None:
        """First vehicle node; used only to test whether structured content is present."""
        nodes = cls._vehicle_nodes(page)
        return nodes[0] if nodes else None

    def _select_vehicle(self, nodes: list[dict[str, Any]], page_url: str) -> dict[str, Any] | None:
        """The node describing THIS detail page, or None when that cannot be decided.

        Detail pages often embed further Car/Product nodes ("similar vehicles"). A single node is
        the page's vehicle; otherwise the node whose `url` canonicalises to the page URL wins, then
        a sole node without a `url`. Picking "the first node" could attach another car's price,
        mileage and ID to this listing.
        """
        if len(nodes) == 1:
            return nodes[0]
        try:
            canonical = self.policy.canonical(page_url)
        except ValidationFailed:
            return None
        without_url: list[dict[str, Any]] = []
        for node in nodes:
            href = text_of(node.get("url"))
            if not href:
                without_url.append(node)
                continue
            try:
                if self.policy.canonical(href, base=page_url) == canonical:
                    return node
            except ValidationFailed:
                continue
        return without_url[0] if len(without_url) == 1 else None

    @staticmethod
    def _element_url(element: Any) -> tuple[str | None, dict[str, Any] | None]:
        if isinstance(element, str):
            return element, None
        if not isinstance(element, dict):
            return None, None
        item = element.get("item")
        item_dict = item if isinstance(item, dict) else None
        if isinstance(item, str):
            return item, None
        candidates = [element.get("url"), _get(item_dict, "url")]
        if (
            item_dict is not None
            and isinstance(item_dict.get("@id"), str)
            and item_dict["@id"].startswith("http")
        ):
            candidates.append(item_dict["@id"])
        if "ListItem" not in local_type_names(element) and item_dict is None:
            item_dict = element
        for candidate in candidates:
            text = text_of(candidate)
            if text:
                return text, item_dict
        return None, item_dict

    def _next_url(self, page: PageView, base: str, *, current: str) -> tuple[str | None, str | None]:
        refused = False
        for href in page.next_links:
            try:
                url = self.policy.canonical(href, base=base)
            except ValidationFailed:
                refused = True
                continue
            if url == current:
                refused = True
                continue
            if self.policy.is_search_url(url):
                return url, None
            refused = True
        return None, ("next link refused by search host/path policy" if refused else None)

    def _observation(self, card: _Card, position: int) -> SearchObservation:
        item = card.item
        anchor = card.anchor
        provider_id, _ = _provider_id(item)
        provider_id = provider_id or self._id_from_url(card.canonical_url)
        title_raw = (
            text_of(_get(item, "name"))
            or (anchor.title_attr if anchor else None)
            or (anchor.text if anchor else None)
        )
        title = bounded(collapse_ws(html_to_text(title_raw).text), 300) if title_raw else None

        price_minor: int | None = None
        currency: str | None = None
        price_raw: str | None = None
        offer = next(iter(_dicts(_get(item, "offers"))), None)
        if offer is not None:
            amount = decimal_from_json(scalar(offer.get("price")), kind="money")
            cur = text_of(offer.get("priceCurrency"))
            if (
                amount is not None
                and amount > 0
                and cur
                and not json_number_is_ambiguous(scalar(offer.get("price")))
            ):
                minor = _money_minor(amount, cur.upper())
                if minor is not None:
                    price_minor, currency = minor, cur.upper()
                    price_raw = _raw_repr(scalar(offer.get("price")), cur)
        if price_minor is None and anchor is not None:
            for money in find_money(anchor.container_text):
                if money.monthly or money.amount <= 0:
                    continue
                minor = _money_minor(money.amount, money.currency)
                if minor is not None:
                    price_minor, currency, price_raw = minor, money.currency, money.raw
                    break

        mileage_km: Decimal | None = None
        mileage_raw: str | None = None
        raw_val, unit = _quantity(_get(item, "mileageFromOdometer"))
        if raw_val is not None and not json_number_is_ambiguous(raw_val):
            amount = decimal_from_json(raw_val, kind="count")
            unit_l = (unit or "").lower()
            if amount is not None and unit_l in _KM_UNIT_CODES:
                mileage_km, mileage_raw = amount, _raw_repr(raw_val, unit)
            elif amount is not None and unit_l in _MI_UNIT_CODES:
                mileage_km, mileage_raw = miles_to_km(amount), _raw_repr(raw_val, unit)
        if mileage_km is None and mileage_raw is None and anchor is not None:
            found = find_mileages(anchor.container_text)
            if found:
                # A range ("150.000 - 160.000 km") has no exact value: keep the text, value unknown.
                mileage_raw = found[0].raw
                mileage_km = found[0].km if found[0].range_low is None else None
        if mileage_km is not None and not _mileage_plausible(mileage_km):
            mileage_km = None

        modified: datetime | None = None
        modified_raw = text_of(_get(item, "dateModified"))
        if modified_raw:
            stamp = parse_source_timestamp(modified_raw, self.config.source_timezone)
            modified = stamp.value if stamp else None

        material: dict[str, str | None] = {
            "canonical_url": card.canonical_url,
            "source_listing_id": provider_id,
            "title": title,
            "price_minor": None if price_minor is None else str(price_minor),
            "currency": currency,
            "mileage_km": None if mileage_km is None else plain(mileage_km),
            "source_modified_at": modified.isoformat() if modified else None,
        }
        return SearchObservation(
            source_listing_id=provider_id,
            canonical_url=card.canonical_url,
            title=title,
            card_price_raw=bounded(price_raw, 200),
            card_price_minor=price_minor,
            card_currency=currency,
            card_mileage_raw=bounded(mileage_raw, 200),
            card_mileage_km=mileage_km,
            source_modified_at=modified,
            position=position,
            card_hash=sha256_json(material),
            card_hash_material=material,
        )

    def _now(self) -> datetime:
        return self._clock.now()

    def _denied_document(self, url: str, reason: str) -> RawDocument:
        now = self._now()
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

    # ------------------------------------------------------------------ detail mapping

    def _build_listing(
        self, document: RawDocument, page: PageView, vehicle: dict[str, Any]
    ) -> NormalizedListing:
        source_url = document.final_url or document.url
        canonical = self.policy.canonical(source_url)
        col = _Collector(
            source_url=canonical, snapshot_id=document.snapshot_id, observed_at=document.fetched_at
        )
        offers = _dicts(vehicle.get("offers"))
        offer = next((o for o in offers if "AggregateOffer" not in local_type_names(o)), None)
        aggregate = next((o for o in offers if "AggregateOffer" in local_type_names(o)), None)

        provider_id, id_key = _provider_id(vehicle)
        if provider_id is None and offer is not None:
            provider_id, id_key = _provider_id(offer)
        if provider_id is None:
            provider_id = self._id_from_url(canonical)
            id_key = "url:detail_id_regex" if provider_id else None
        identity = build_identity(self.source_key, canonical, provider_id)
        col.prov(
            "source_listing_id",
            ExtractionMethod.JSON_LD if id_key and not id_key.startswith("url") else ExtractionMethod.DERIVED,
            Confidence.HIGH,
            selector=f"json_ld:{id_key}" if id_key and not id_key.startswith("url") else id_key,
            raw=identity.identity_material,
            transformation=identity.identity_method,
        )
        ld_url = text_of(vehicle.get("url"))
        if ld_url:
            try:
                if self.policy.canonical(ld_url, base=source_url) != canonical:
                    col.warn("JSON_LD_URL_MISMATCH")
            except ValidationFailed:
                col.warn("JSON_LD_URL_INVALID")
        try:
            if self.policy.canonical(document.url) != canonical:
                col.warn("DETAIL_REDIRECTED")  # identity follows the final URL; caller may need an alias
        except ValidationFailed:
            col.warn("DETAIL_REDIRECTED")

        name_raw = text_of(vehicle.get("name"))
        title = bounded(html_to_text(name_raw).text, 300) if name_raw else bounded(page.h1 or page.title, 300)
        # The vehicle name is seller-controlled text too: flag (never obey) injection-like wording.
        if name_raw:
            name_clean = html_to_text(name_raw)
            if name_clean.active_markup:
                col.warn("SELLER_TEXT_MARKUP_REMOVED")
            if name_clean.active_markup or injection_signals(name_clean.text):
                col.warn("SELLER_TEXT_SUSPICIOUS")

        # Untrusted seller text: bounded plain text, flagged, never interpreted.
        desc_value = scalar(vehicle.get("description"))
        seller_raw: str | None = None
        desc_method, desc_selector = ExtractionMethod.JSON_LD, "json_ld:description"
        if isinstance(desc_value, str) and desc_value.strip():
            seller_raw = desc_value
        elif page.microdata_description:
            seller_raw = page.microdata_description
            desc_method, desc_selector = ExtractionMethod.MICRODATA, "microdata:description"
        seller_text = clean_seller_text(seller_raw)
        for code in seller_text.warnings:
            col.warn(code)
        if seller_text.excerpt is not None:
            signals = f"; signals={','.join(seller_text.signals)}" if seller_text.signals else ""
            col.prov(
                "description_excerpt",
                desc_method,
                Confidence.HIGH,
                selector=desc_selector,
                transformation="untrusted seller text: markup removed, bounded, never interpreted" + signals,
            )

        vehicle_spec = self._vehicle_spec(vehicle, page, col, title, seller_text.excerpt)
        price = self._price(offer, aggregate, page, col)
        availability = self._availability(offer, page, col)
        seller_type = self._seller_type(offer, col)
        location = self._location(offer, col)
        documentation = self._documentation(vehicle, col)
        co2 = self._co2(vehicle, page, col)
        condition = self._condition(vehicle, offer, seller_text.excerpt, col)
        condition, documentation = self._inspection(page, condition, documentation, col)
        if (
            condition.damaged_vehicle == ClaimStatus.SELLER_CLAIMED
            and price.type == PriceType.FULL_VEHICLE_ASKING
        ):
            price = price.model_copy(update={"type": PriceType.PARTS_OR_DAMAGED})
            col.prov(
                "price.type",
                ExtractionMethod.JSON_LD,
                Confidence.MEDIUM,
                selector="json_ld:itemCondition",
                raw="DamagedCondition",
                transformation="damaged vehicle: not an ordinary payable price",
            )

        modified = None
        modified_raw = text_of(vehicle.get("dateModified"))
        if modified_raw:
            modified = parse_source_timestamp(modified_raw, self.config.source_timezone)
        if not vehicle_spec.make or not vehicle_spec.model:
            col.warn("MAKE_MODEL_MISSING")

        return NormalizedListing(
            source_key=self.source_key,
            source_listing_id=identity.source_listing_id,
            canonical_url=canonical,
            observed_at=document.fetched_at,
            language=page.lang,
            title=title,
            seller_type=seller_type,
            location=location,
            vehicle=vehicle_spec,
            price=price,
            availability=availability,
            condition=condition,
            documentation=documentation,
            co2=co2,
            source_modified_at=modified or SourceTimestamp(),
            description_excerpt=seller_text.excerpt,
            provenance=col.provenance,
            conflicts=tuple(col.conflicts),
            warnings=tuple(col.warnings),
            parser_version=ADAPTER_VERSION,
        )

    # -- vehicle

    def _vehicle_spec(
        self,
        vehicle: dict[str, Any],
        page: PageView,
        col: _Collector,
        title: str | None,
        description: str | None,
    ) -> VehicleSpec:
        jl = ExtractionMethod.JSON_LD
        make_raw = _get(vehicle, "brand") or _get(vehicle, "manufacturer")
        make = bounded(_label(make_raw), 80)
        if make:
            col.prov("vehicle.make", jl, Confidence.HIGH, selector="json_ld:brand|manufacturer", raw=make)
        model = bounded(_label(_get(vehicle, "model")), 120)
        if model:
            col.prov("vehicle.model", jl, Confidence.HIGH, selector="json_ld:model", raw=model)
        trim = bounded(_label(_get(vehicle, "vehicleConfiguration")), 200)
        if trim:
            col.prov("vehicle.trim", jl, Confidence.MEDIUM, selector="json_ld:vehicleConfiguration", raw=trim)

        model_year = year_from(scalar(_get(vehicle, "vehicleModelDate")))
        if model_year:
            col.prov(
                "vehicle.model_year", jl, Confidence.HIGH, selector="json_ld:vehicleModelDate", raw=model_year
            )
        production_year = year_from(scalar(_get(vehicle, "productionDate")))
        if production_year:
            col.prov(
                "vehicle.production_year",
                jl,
                Confidence.HIGH,
                selector="json_ld:productionDate",
                raw=production_year,
            )

        first_reg = PartialDate()
        reg_raw = text_of(_get(vehicle, "dateVehicleFirstRegistered"))
        if reg_raw:
            parsed = parse_partial_date(reg_raw)
            observed = col.observed_at
            # Compare at the stated precision: "2026-10" is not future on 2026-10-06, "2026-10-20" is.
            stated = parsed.value if parsed is not None else None
            observed_text = observed.date().isoformat()[: len(stated)] if stated else ""
            if parsed is None:
                col.warn("FIRST_REGISTRATION_UNPARSEABLE")
            elif stated is not None and stated > observed_text:
                col.warn("FIRST_REGISTRATION_IN_FUTURE")
            else:
                first_reg = parsed
                col.prov(
                    "vehicle.first_registration",
                    jl,
                    Confidence.HIGH,
                    selector="json_ld:dateVehicleFirstRegistered",
                    raw=reg_raw,
                    transformation=f"precision={parsed.precision.value}",
                )

        body = BodyType.UNKNOWN
        body_raw = text_of(_get(vehicle, "bodyType"))
        if body_raw:
            mapped = _first_rule(_BODY_RULES, body_raw)
            if mapped is None:
                col.warn("BODY_TYPE_UNMAPPED")
            else:
                body = mapped
                col.prov(
                    "vehicle.body_type",
                    jl,
                    Confidence.MEDIUM,
                    selector="json_ld:bodyType",
                    raw=body_raw,
                    transformation=f"wording -> {mapped.value}",
                )

        seats: int | None = None
        seats_val, _ = _quantity(_get(vehicle, "vehicleSeatingCapacity", "seatingCapacity"))
        seats_dec = decimal_from_json(seats_val, kind="count") if seats_val is not None else None
        if seats_dec is not None and seats_dec == seats_dec.to_integral_value() and 1 <= seats_dec <= 12:
            seats = int(seats_dec)
            col.prov(
                "vehicle.seats", jl, Confidence.HIGH, selector="json_ld:vehicleSeatingCapacity", raw=seats_val
            )

        engine = next(iter(_dicts(_get(vehicle, "vehicleEngine"))), None)
        fuel = Fuel.UNKNOWN
        fuel_raw = text_of(_get(vehicle, "fuelType")) or text_of(_get(engine, "fuelType"))
        if fuel_raw:
            mapped_fuel = _first_rule(_FUEL_RULES, fuel_raw)
            if mapped_fuel is None:
                col.warn("FUEL_UNMAPPED")
            else:
                fuel = mapped_fuel
                col.prov(
                    "vehicle.fuel",
                    jl,
                    Confidence.HIGH,
                    selector="json_ld:fuelType",
                    raw=fuel_raw,
                    transformation=f"wording -> {mapped_fuel.value}",
                )

        displacement = self._displacement(engine, col)
        power_kw = self._power(engine, page, col)

        gearbox = Gearbox.UNKNOWN
        gearbox_subtype: str | None = None
        trans_raw = text_of(_get(vehicle, "vehicleTransmission"))
        if trans_raw:
            mapped_gear = _first_rule(_GEARBOX_RULES, trans_raw)
            if mapped_gear is None:
                col.warn("GEARBOX_UNMAPPED")
            else:
                gearbox = mapped_gear
                col.prov(
                    "vehicle.gearbox",
                    jl,
                    Confidence.HIGH,
                    selector="json_ld:vehicleTransmission",
                    raw=trans_raw,
                    transformation=f"wording -> {mapped_gear.value}",
                )
            sub = _GEARBOX_SUBTYPE.search(trans_raw)
            if sub:
                gearbox_subtype = bounded(sub.group(1), 80)

        drive = Drive.UNKNOWN
        drive_raw = text_of(_get(vehicle, "driveWheelConfiguration"))
        if drive_raw:
            enum = (enum_name(drive_raw) or "").lower()
            if enum in _DRIVE_ENUM:
                drive = _DRIVE_ENUM[enum]
                col.prov(
                    "vehicle.drive",
                    jl,
                    Confidence.HIGH,
                    selector="json_ld:driveWheelConfiguration",
                    raw=drive_raw,
                    transformation="schema.org DriveWheelConfigurationValue",
                )
            else:
                mapped_drive = _first_rule(_DRIVE_RULES, drive_raw)
                if mapped_drive is None:
                    col.warn("DRIVE_UNMAPPED")
                else:
                    drive = mapped_drive
                    col.prov(
                        "vehicle.drive",
                        jl,
                        Confidence.LOW,
                        selector="json_ld:driveWheelConfiguration",
                        raw=drive_raw,
                        transformation=f"wording -> {mapped_drive.value}; permanent/selectable unverified",
                    )

        col.warn("ENGINE_CODE_UNVERIFIED")
        mileage_km, original, claim = self._mileage(vehicle, page, col, title, description)
        return VehicleSpec(
            make=make,
            model=model,
            trim=trim,
            model_year=model_year,
            first_registration=first_reg,
            production_year=production_year,
            body_type=body,
            seats=seats,
            fuel=fuel,
            engine_code=None,
            engine_displacement_cm3=displacement,
            power_kw=power_kw,
            gearbox=gearbox,
            gearbox_subtype=gearbox_subtype,
            drive=drive,
            mileage_km=mileage_km,
            mileage_original=original,
            mileage_claim=claim,
        )

    @staticmethod
    def _displacement(engine: dict[str, Any] | None, col: _Collector) -> int | None:
        raw_val, unit = _quantity(_get(engine, "engineDisplacement"))
        if raw_val is None:
            return None
        unit_l = (unit or "").strip().lower()
        if isinstance(raw_val, str) and unit is None:
            match = re.search(r"([\d.,'\u2019]+)\s*(cm³|cm3|ccm|cc|l)\b", raw_val, re.IGNORECASE)
            if match:
                raw_val, unit_l = match.group(1), match.group(2).lower()
        if json_number_is_ambiguous(raw_val):
            col.warn("DISPLACEMENT_FORMAT_AMBIGUOUS")
            return None
        value = decimal_from_json(raw_val, kind="count")
        if value is None:
            col.warn("DISPLACEMENT_UNPARSEABLE")
            return None
        if unit_l in {"cmq", "cm3", "cm³", "ccm", "cc"}:
            cm3 = value
            transformation = None
        elif unit_l in {"ltr", "l"}:
            cm3 = value * 1000
            transformation = "litres x 1000"
        else:
            col.warn("DISPLACEMENT_UNIT_UNKNOWN")
            return None
        result = round_half_up_int(cm3)
        if not 50 <= result <= 10000:
            col.warn("DISPLACEMENT_IMPLAUSIBLE")
            return None
        col.prov(
            "vehicle.engine_displacement_cm3",
            ExtractionMethod.JSON_LD,
            Confidence.HIGH,
            selector="json_ld:vehicleEngine.engineDisplacement",
            raw=_raw_repr(raw_val, unit),
            transformation=transformation,
        )
        return result

    @staticmethod
    def _power(engine: dict[str, Any] | None, page: PageView, col: _Collector) -> int | None:
        candidates = _dicts(_get(engine, "enginePower"))
        best = None
        for preferred in (True, False):
            for candidate in candidates:
                unit = (
                    text_of(candidate.get("unitCode")) or text_of(candidate.get("unitText")) or ""
                ).lower()
                if preferred != (unit in {"kwt", "kw"}):
                    continue
                raw_val = scalar(candidate.get("value"))
                value = decimal_from_json(raw_val, kind="count")
                if value is None:
                    continue
                best = power_from_value(value, unit, _raw_repr(raw_val, unit))
                if best is not None:
                    break
            if best is not None:
                break
        if best is None:
            raw_text = text_of(_get(engine, "enginePower"))
            if raw_text and not candidates:
                best = find_power(raw_text)
        if best is None or not 1 <= best.kw <= 2000:
            if best is not None:
                col.warn("POWER_IMPLAUSIBLE")
            return None
        if best.converted_from:
            col.warn("POWER_UNIT_CONVERTED")
        col.prov(
            "vehicle.power_kw",
            ExtractionMethod.JSON_LD,
            Confidence.HIGH if best.converted_from is None else Confidence.MEDIUM,
            selector="json_ld:vehicleEngine.enginePower",
            raw=best.raw,
            transformation=None if best.converted_from is None else f"{best.converted_from} -> kW, half-up",
        )
        return best.kw

    def _mileage(
        self,
        vehicle: dict[str, Any],
        page: PageView,
        col: _Collector,
        title: str | None,
        description: str | None,
    ) -> tuple[Decimal | None, MileageOriginal, OdometerClaim]:
        """Odometer value, original statement and claim status.

        The structured JSON-LD value wins over text; text mentions that disagree create an
        unresolved conflict (claim CONFLICTING) whose `values` are plain km decimals (ranges as
        `low-high`) so screening can compare them. Ranges and implausible values are never
        turned into an exact mileage.
        """
        quantity = scalar(_get(vehicle, "mileageFromOdometer"))
        raw_val, unit = _quantity(quantity)
        selector = "json_ld:mileageFromOdometer"
        structured: _MileageStatement | None = None
        original = MileageOriginal()
        if raw_val is not None:
            structured, original = self._structured_mileage(raw_val, unit, page, col, selector)
        elif isinstance(quantity, dict) and (
            quantity.get("minValue") is not None or quantity.get("maxValue") is not None
        ):
            structured, original = self._structured_mileage_range(quantity, unit, col, selector)

        # Text evidence: titles (always) and explicitly labelled odometer statements in the description.
        text_mentions: list[tuple[str, MileageMatch]] = []
        for location, text in (("json_ld:name", title), ("html:title", page.title), ("html:h1", page.h1)):
            if text:
                text_mentions.extend((location, m) for m in find_mileages(text) if _mileage_plausible(m.km))
        if description:
            text_mentions.extend(
                ("description:odometer_label", m)
                for m in find_labelled_odometer(description)
                if _mileage_plausible(m.km)
            )

        if structured is not None:
            differing = [(loc, m) for loc, m in text_mentions if not structured.consistent_with(m)]
            claim = (
                OdometerClaim.RANGE_ONLY
                if structured.low_km is not None
                else OdometerClaim.ESTIMATED
                if original.is_estimate
                else OdometerClaim.SELLER_REPORTED
            )
            if differing:
                values = [structured.value] + [_mileage_value(m) for _, m in differing]
                locations = [selector] + [f"{loc} '{m.raw}'" for loc, m in differing]
                col.conflict(
                    "vehicle.mileage_km",
                    values,
                    locations,
                    note="text mileage differs from structured odometer value; structured kept, unresolved",
                )
                col.warn("MILEAGE_CONFLICT")
                claim = OdometerClaim.CONFLICTING
            elif structured.low_km is not None:
                col.warn("MILEAGE_RANGE_ONLY")
            return structured.exact_km, original, claim

        if text_mentions:
            statements = list(dict.fromkeys(_mileage_value(m) for _, m in text_mentions))
            first_loc, first = text_mentions[0]
            col.warn("MILEAGE_FROM_TEXT_ONLY")
            col.prov(
                "vehicle.mileage_km",
                ExtractionMethod.REGEX,
                Confidence.LOW,
                selector=first_loc,
                raw=first.raw,
                transformation="no structured odometer value; text mention used",
            )
            original = MileageOriginal(
                amount=first.amount if first.range_low is None else None,
                unit=first.unit,
                text=bounded(first.raw, 200),
                is_estimate=first.is_estimate,
                range_low=first.range_low,
                range_high=first.amount if first.range_low is not None else None,
            )
            if len(statements) > 1:
                col.conflict(
                    "vehicle.mileage_km",
                    statements,
                    [f"{loc} '{m.raw}'" for loc, m in text_mentions],
                    note="several different text mileages; highest kept, unresolved",
                )
                col.warn("MILEAGE_CONFLICT")
                return max(m.km for _, m in text_mentions), original, OdometerClaim.CONFLICTING
            if first.range_low is not None:
                col.warn("MILEAGE_RANGE_ONLY")
                return None, original, OdometerClaim.RANGE_ONLY
            return (
                first.km,
                original,
                OdometerClaim.ESTIMATED if first.is_estimate else OdometerClaim.SELLER_REPORTED,
            )
        col.warn("MILEAGE_MISSING")
        return None, original, OdometerClaim.UNKNOWN

    @staticmethod
    def _structured_mileage(
        raw_val: Any, unit: str | None, page: PageView, col: _Collector, selector: str
    ) -> tuple[_MileageStatement | None, MileageOriginal]:
        raw_text = _raw_repr(raw_val, unit)
        amount: Decimal | None = None
        unit_l = (unit or "").strip().lower()
        estimate = False
        if isinstance(raw_val, str) and re.search(r"[A-Za-z]", raw_val):
            found = find_mileages(raw_val)
            if found and found[0].range_low is not None:
                ranged = found[0]
                low_km = ranged.range_low_km
                assert low_km is not None
                if not _mileage_plausible(ranged.km):
                    col.warn("MILEAGE_IMPLAUSIBLE")
                    return None, MileageOriginal()
                col.prov(
                    "vehicle.mileage_km",
                    ExtractionMethod.JSON_LD,
                    Confidence.HIGH,
                    selector=selector,
                    raw=raw_text,
                    transformation="range only; no exact odometer value",
                )
                original = MileageOriginal(
                    unit=ranged.unit,
                    text=bounded(raw_text, 200),
                    is_estimate=ranged.is_estimate,
                    range_low=ranged.range_low,
                    range_high=ranged.amount,
                )
                return _MileageStatement(
                    exact_km=None, low_km=low_km, high_km=ranged.km, unit=ranged.unit
                ), original
            if found:
                amount, unit_l, estimate = found[0].amount, found[0].unit, found[0].is_estimate
            else:
                col.warn("MILEAGE_UNPARSEABLE")
        elif json_number_is_ambiguous(raw_val):
            col.warn("MILEAGE_FORMAT_AMBIGUOUS")
        else:
            amount = decimal_from_json(raw_val, kind="count")
            if amount is None:
                col.warn("MILEAGE_UNPARSEABLE")
        if amount is None:
            return None, MileageOriginal()
        resolved: str | None = None
        method, confidence, transformation = ExtractionMethod.JSON_LD, Confidence.HIGH, None
        if unit_l in _KM_UNIT_CODES:
            resolved = "km"
        elif unit_l in _MI_UNIT_CODES:
            resolved = "mi"
        else:
            # Unit missing: accept only if the visible page states the same number with a unit.
            for mention in find_mileages(page.visible_text):
                if mention.range_low is None and mention.amount == amount:
                    resolved = mention.unit
                    method, confidence = ExtractionMethod.DERIVED, Confidence.MEDIUM
                    transformation = f"unit taken from visible text '{mention.raw}'"
                    break
        if resolved is None:
            col.warn("MILEAGE_UNIT_UNKNOWN")
            return None, MileageOriginal(amount=amount, unit="unknown", text=bounded(raw_text, 200))
        km = miles_to_km(amount) if resolved == "mi" else amount
        if not _mileage_plausible(km):
            col.warn("MILEAGE_IMPLAUSIBLE")
            return None, MileageOriginal()
        if resolved == "mi":
            transformation = (transformation + "; " if transformation else "") + "miles x 1.609344 (exact)"
        col.prov(
            "vehicle.mileage_km",
            method,
            confidence,
            selector=selector,
            raw=raw_text,
            transformation=transformation,
        )
        original = MileageOriginal(
            amount=amount,
            unit="mi" if resolved == "mi" else "km",
            text=bounded(raw_text, 200),
            is_estimate=estimate,
        )
        return _MileageStatement(
            exact_km=km, low_km=None, high_km=km, unit=resolved, exact_amount=amount
        ), original

    @staticmethod
    def _structured_mileage_range(
        quantity: dict[str, Any], unit: str | None, col: _Collector, selector: str
    ) -> tuple[_MileageStatement | None, MileageOriginal]:
        low_raw, high_raw = scalar(quantity.get("minValue")), scalar(quantity.get("maxValue"))
        if json_number_is_ambiguous(low_raw) or json_number_is_ambiguous(high_raw):
            col.warn("MILEAGE_FORMAT_AMBIGUOUS")
            return None, MileageOriginal()
        low = decimal_from_json(low_raw, kind="count")
        high = decimal_from_json(high_raw, kind="count")
        unit_l = (unit or "").strip().lower()
        resolved: Literal["km", "mi"] | None = (
            "km" if unit_l in _KM_UNIT_CODES else "mi" if unit_l in _MI_UNIT_CODES else None
        )
        if low is None or high is None or resolved is None or not low < high:
            col.warn("MILEAGE_UNPARSEABLE")
            return None, MileageOriginal()
        low_km = miles_to_km(low) if resolved == "mi" else low
        high_km = miles_to_km(high) if resolved == "mi" else high
        if not (_mileage_plausible(low_km) and _mileage_plausible(high_km)):
            col.warn("MILEAGE_IMPLAUSIBLE")
            return None, MileageOriginal()
        raw_text = f"{_raw_repr(low_raw)}-{_raw_repr(high_raw, unit)}"
        col.prov(
            "vehicle.mileage_km",
            ExtractionMethod.JSON_LD,
            Confidence.HIGH,
            selector=f"{selector}.minValue|maxValue",
            raw=raw_text,
            transformation="range only; no exact odometer value",
        )
        original = MileageOriginal(unit=resolved, text=bounded(raw_text, 200), range_low=low, range_high=high)
        return _MileageStatement(exact_km=None, low_km=low_km, high_km=high_km, unit=resolved), original

    # -- price

    def _price(
        self,
        offer: dict[str, Any] | None,
        aggregate: dict[str, Any] | None,
        page: PageView,
        col: _Collector,
    ) -> PriceInfo:
        jl = ExtractionMethod.JSON_LD
        text = page.visible_text
        currency: str | None = None
        amount: Decimal | None = None
        raw_text: str | None = None
        structured_basis: PriceBasis | None = None
        price_type = PriceType.UNKNOWN
        gross_minor: int | None = None
        net_minor: int | None = None
        instalment = False

        if offer is None and aggregate is not None:
            col.warn("PRICE_AGGREGATE_ONLY")
        if offer is not None:
            cur_raw = text_of(offer.get("priceCurrency"))
            currency = cur_raw.upper() if cur_raw else None
            raw_price = scalar(offer.get("price"))
            if raw_price is not None:
                if json_number_is_ambiguous(raw_price):
                    col.warn("PRICE_FORMAT_AMBIGUOUS")
                else:
                    amount = decimal_from_json(raw_price, kind="money")
                    if amount is None:
                        col.warn("PRICE_UNPARSEABLE")
                raw_text = _raw_repr(raw_price, cur_raw)
            specs = _dicts(offer.get("priceSpecification"))
            for spec in specs:
                spec_cur = (text_of(spec.get("priceCurrency")) or cur_raw or "").upper() or None
                spec_raw = scalar(spec.get("price"))
                spec_amount = (
                    None if json_number_is_ambiguous(spec_raw) else decimal_from_json(spec_raw, kind="money")
                )
                if spec_amount is not None and not _price_plausible(spec_amount):
                    spec_amount = None  # zero/negative/absurd component prices carry no amount
                is_billing = any(
                    spec.get(k) not in (None, "")
                    for k in ("billingDuration", "billingIncrement", "billingStart", "referenceQuantity")
                ) or (text_of(spec.get("unitCode")) or "").upper() in {"MON", "ANN", "WEE"}
                if is_billing:
                    if amount is not None and spec_amount == amount:
                        instalment = True
                    continue
                vat_flag = _bool(spec.get("valueAddedTaxIncluded"))
                if spec_amount is not None and spec_cur and vat_flag is not None:
                    minor = _money_minor(spec_amount, spec_cur)
                    if minor is not None and (currency is None or spec_cur == currency):
                        currency = currency or spec_cur
                        if vat_flag:
                            gross_minor = minor
                        else:
                            net_minor = minor
                if vat_flag is not None and (spec_amount is None or amount is None or spec_amount == amount):
                    structured_basis = PriceBasis.GROSS if vat_flag else PriceBasis.NET
                if amount is None and spec_amount is not None:
                    amount, raw_text = spec_amount, _raw_repr(spec_raw, spec_cur)
                    if vat_flag is not None:
                        structured_basis = PriceBasis.GROSS if vat_flag else PriceBasis.NET
            if structured_basis is not None:
                col.prov(
                    "price.basis",
                    jl,
                    Confidence.HIGH,
                    selector="json_ld:offers.priceSpecification.valueAddedTaxIncluded",
                    raw=str(structured_basis.value),
                )
            if _bool(offer.get("valueAddedTaxIncluded")) is not None and structured_basis is None:
                flag = _bool(offer.get("valueAddedTaxIncluded"))
                structured_basis = PriceBasis.GROSS if flag else PriceBasis.NET
                col.prov(
                    "price.basis",
                    jl,
                    Confidence.MEDIUM,
                    selector="json_ld:offers.valueAddedTaxIncluded",
                    raw=str(flag),
                )

        amount_minor: int | None = None
        if amount is not None:
            if amount == 0:
                col.warn("PRICE_ZERO_IGNORED")
            elif not _price_plausible(amount):
                col.warn("PRICE_IMPLAUSIBLE")
            elif currency is None:
                col.warn("PRICE_CURRENCY_MISSING")
            elif currency not in CURRENCY_EXPONENTS:
                col.warn("PRICE_CURRENCY_UNSUPPORTED")
            else:
                amount_minor = _money_minor(amount, currency)
                if amount_minor is None:
                    col.warn("PRICE_PRECISION_INVALID")
        if amount_minor is not None and currency is not None:
            visible = next(
                (
                    m
                    for m in find_money(text)
                    if m.currency == currency and m.amount == amount and not m.monthly
                ),
                None,
            )
            col.prov(
                "price.amount_minor",
                jl,
                Confidence.HIGH,
                selector="json_ld:offers.price",
                raw=raw_text,
                transformation=f"{currency} minor units"
                + (f"; matches visible '{visible.raw}'" if visible else ""),
            )
            col.prov(
                "price.currency", jl, Confidence.HIGH, selector="json_ld:offers.priceCurrency", raw=currency
            )
            raw_text = visible.raw if visible else raw_text
        else:
            col.warn("PRICE_MISSING")

        # Wording signals (seller statements only, never inferred entitlements).
        text_gross = _GROSS_WORDING.search(text)
        text_net = _NET_WORDING.search(text)
        vat_shown = _VAT_SHOWN_WORDING.search(text)
        margin = _MARGIN_WORDING.search(text)
        basis = PriceBasis.UNKNOWN
        if structured_basis is not None:
            basis = structured_basis
            contradicting = text_net if structured_basis == PriceBasis.GROSS else text_gross
            confirming = text_gross if structured_basis == PriceBasis.GROSS else text_net
            if contradicting and not confirming:
                col.conflict(
                    "price.basis",
                    [
                        f"{structured_basis.value} (json_ld)",
                        f"{'net' if text_net else 'gross'} ('{contradicting.group(0)}')",
                    ],
                    ["json_ld:priceSpecification", "visible_text"],
                    note="structured VAT flag contradicts page wording",
                )
                col.warn("PRICE_BASIS_CONFLICT")
                basis = PriceBasis.UNKNOWN
        elif text_gross and text_net:
            col.conflict(
                "price.basis",
                [f"gross ('{text_gross.group(0)}')", f"net ('{text_net.group(0)}')"],
                ["visible_text", "visible_text"],
                note="page states both gross and net wording without structured VAT flag",
            )
            col.warn("PRICE_BASIS_AMBIGUOUS")
        elif text_gross or text_net:
            match = text_gross or text_net
            assert match is not None
            basis = PriceBasis.GROSS if text_gross else PriceBasis.NET
            col.prov(
                "price.basis",
                ExtractionMethod.REGEX,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=match.group(0),
            )
        elif margin:
            basis = PriceBasis.GROSS
            col.prov(
                "price.basis",
                ExtractionMethod.DERIVED,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=margin.group(0),
                transformation="margin-scheme wording: advertised price is the total payable (no VAT shown)",
            )
        if basis == PriceBasis.UNKNOWN and amount_minor is not None:
            col.warn("PRICE_BASIS_UNKNOWN")

        vat_treatment = VatTreatment.NOT_STATED
        vat_reclaimable = Tristate.UNKNOWN
        if vat_shown and margin:
            col.conflict(
                "price.vat_treatment",
                [f"vat_shown ('{vat_shown.group(0)}')", f"margin_scheme ('{margin.group(0)}')"],
                ["visible_text", "visible_text"],
                note="contradictory VAT wording",
            )
            col.warn("VAT_WORDING_CONFLICT")
            vat_treatment = VatTreatment.UNKNOWN
        elif vat_shown:
            vat_treatment, vat_reclaimable = VatTreatment.VAT_SHOWN, Tristate.YES
            col.prov(
                "price.vat_treatment",
                ExtractionMethod.REGEX,
                Confidence.HIGH,
                selector="visible_text",
                raw=vat_shown.group(0),
                transformation="seller wording only; not a verified entitlement",
            )
        elif margin:
            vat_treatment, vat_reclaimable = VatTreatment.MARGIN_SCHEME, Tristate.NO
            col.prov(
                "price.vat_treatment",
                ExtractionMethod.REGEX,
                Confidence.HIGH,
                selector="visible_text",
                raw=margin.group(0),
                transformation="seller wording only",
            )
        else:
            private = _PRIVATE_SALE_WORDING.search(text)
            if private:
                vat_treatment = VatTreatment.PRIVATE_SALE
                col.prov(
                    "price.vat_treatment",
                    ExtractionMethod.REGEX,
                    Confidence.MEDIUM,
                    selector="visible_text",
                    raw=private.group(0),
                )
        vat_wording = vat_shown or margin
        if vat_reclaimable != Tristate.UNKNOWN and vat_wording is not None:
            col.prov(
                "price.vat_reclaimable",
                ExtractionMethod.REGEX,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=vat_wording.group(0),
                transformation="seller wording only; never an entitlement",
            )

        vat_rate: Decimal | None = None
        rate_match = _VAT_RATE.search(text)
        if rate_match:
            rate_raw = rate_match.group("a") or rate_match.group("b")
            rate_val = Decimal(rate_raw.replace(",", "."))
            if 0 < rate_val <= 30:
                vat_rate = rate_val
                col.prov(
                    "price.vat_rate_stated",
                    ExtractionMethod.REGEX,
                    Confidence.MEDIUM,
                    selector="visible_text",
                    raw=rate_match.group(0),
                )

        negotiable = Tristate.UNKNOWN
        fixed = _FIXED_PRICE.search(text)
        nego = _NEGOTIABLE.search(text)
        if fixed and not nego:
            negotiable = Tristate.NO
        elif nego and not fixed:
            negotiable = Tristate.YES
        elif fixed and nego:
            col.warn("NEGOTIABILITY_AMBIGUOUS")
        nego_wording = fixed if negotiable == Tristate.NO else nego if negotiable == Tristate.YES else None
        if nego_wording is not None:
            col.prov(
                "price.negotiable",
                ExtractionMethod.REGEX,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=nego_wording.group(0),
            )

        export_minor: int | None = None
        export = _EXPORT_PRICE.search(text)
        business_function = (enum_name(offer.get("businessFunction")) if offer else None) or ""
        if amount_minor is not None:
            if instalment:
                price_type = PriceType.INSTALMENT
                col.warn("PRICE_IS_INSTALMENT")
            elif business_function.lower() == "leaseout":
                price_type = PriceType.LEASING
                col.warn("PRICE_IS_LEASING")
            elif export and basis != PriceBasis.GROSS:
                price_type = PriceType.EXPORT_NET
                export_minor = amount_minor
                col.warn("PRICE_EXPORT_NET")
            else:
                price_type = PriceType.FULL_VEHICLE_ASKING
            type_raw = export.group(0) if export and price_type == PriceType.EXPORT_NET else business_function
            col.prov(
                "price.type",
                ExtractionMethod.DERIVED,
                Confidence.MEDIUM,
                selector="json_ld:offers",
                raw=type_raw or None,
                transformation=f"classified as {price_type.value}",
            )
        else:
            on_request = _ON_REQUEST.search(text)
            if on_request:
                price_type = PriceType.PRICE_ON_REQUEST
                col.warn("PRICE_ON_REQUEST")
                col.prov(
                    "price.type",
                    ExtractionMethod.REGEX,
                    Confidence.HIGH,
                    selector="visible_text",
                    raw=on_request.group(0),
                )
        if _SELLER_FEES.search(text):
            col.warn("SELLER_FEES_MENTIONED")

        if amount_minor is None and gross_minor is None and net_minor is None:
            currency = None
        return PriceInfo(
            raw_text=bounded(raw_text, 300) if amount_minor is not None else None,
            amount_minor=amount_minor,
            currency=currency,
            basis=basis,
            type=price_type,
            negotiable=negotiable,
            vat_treatment=vat_treatment,
            vat_rate_stated=vat_rate,
            vat_reclaimable=vat_reclaimable,
            gross_amount_minor=gross_minor,
            net_amount_minor=net_minor,
            export_net_price_minor=export_minor,
        )

    # -- availability / seller / location / documents / co2 / condition

    def _availability(self, offer: dict[str, Any] | None, page: PageView, col: _Collector) -> Availability:
        # Explicit removal wording in the page's own title/h1 (dealer chrome, not seller text).
        heading = f"{page.title or ''} {page.h1 or ''}".lower()
        banner = next((m for m in REMOVED_MARKERS if m in heading), None)
        raw = text_of(_get(offer, "availability"))
        stated: Availability | None = None
        if raw:
            key = (enum_name(raw) or "").lower()
            if key in _AVAILABILITY:
                stated = _AVAILABILITY[key]
            else:
                col.warn("AVAILABILITY_UNCLEAR" if key in _AVAILABILITY_UNCLEAR else "AVAILABILITY_UNMAPPED")
        if banner is not None and stated not in (Availability.SOLD_CLAIMED, Availability.REMOVED):
            if stated is not None:
                col.conflict(
                    "availability",
                    [stated.value, Availability.REMOVED.value],
                    ["json_ld:offers.availability", "html:title|h1"],
                    note=f"structured availability {raw!r} contradicts removal wording '{banner}'",
                )
                col.warn("AVAILABILITY_CONFLICT")
                return Availability.UNKNOWN
            col.prov(
                "availability",
                ExtractionMethod.REGEX,
                Confidence.HIGH,
                selector="html:title|h1",
                raw=banner,
                transformation="explicit removed-listing wording on the detail page",
            )
            col.warn("LISTING_REMOVED_EXPLICIT")
            return Availability.REMOVED
        if stated is not None:
            col.prov(
                "availability",
                ExtractionMethod.JSON_LD,
                Confidence.HIGH,
                selector="json_ld:offers.availability",
                raw=raw,
                transformation=f"-> {stated.value}",
            )
            return stated
        if raw:
            return Availability.UNKNOWN
        if offer is not None:
            col.prov(
                "availability",
                ExtractionMethod.DERIVED,
                Confidence.MEDIUM,
                selector="json_ld:offers",
                transformation="live detail page with an active offer and no availability statement",
            )
            return Availability.AVAILABLE
        return Availability.UNKNOWN

    def _seller_type(self, offer: dict[str, Any] | None, col: _Collector) -> SellerType:
        seller = next(iter(_dicts(_get(offer, "seller", "offeredBy"))), None)
        if seller is not None:
            types = local_type_names(seller)
            if "Person" in types:
                col.prov(
                    "seller_type",
                    ExtractionMethod.JSON_LD,
                    Confidence.HIGH,
                    selector="json_ld:offers.seller",
                    raw="Person",
                )
                return SellerType.PRIVATE
            if types & _DEALER_TYPES:
                col.prov(
                    "seller_type",
                    ExtractionMethod.JSON_LD,
                    Confidence.HIGH,
                    selector="json_ld:offers.seller",
                    raw=",".join(sorted(types)),
                )
                return SellerType.DEALER
        col.prov(
            "seller_type",
            ExtractionMethod.DERIVED,
            Confidence.MEDIUM,
            transformation="source is configured as a dealer inventory website",
        )
        return SellerType.DEALER

    def _location(self, offer: dict[str, Any] | None, col: _Collector) -> LocationInfo:
        seller = next(iter(_dicts(_get(offer, "seller", "offeredBy"))), None)
        place = next(iter(_dicts(_get(offer, "availableAtOrFrom"))), None)
        address = None
        for holder in (place, seller):
            address = next(iter(_dicts(_get(holder, "address"))), None)
            if address is not None:
                break
        country: str | None = None
        city = region = None
        if address is not None:
            country_raw = text_of(address.get("addressCountry"))
            if country_raw:
                candidate = country_raw.strip()
                if re.fullmatch(r"[A-Za-z]{2}", candidate):
                    country = candidate.upper()
                else:
                    country = _COUNTRY_NAMES.get(candidate.lower())
                if country:
                    col.prov(
                        "location.country",
                        ExtractionMethod.JSON_LD,
                        Confidence.HIGH,
                        selector="json_ld:address.addressCountry",
                        raw=country_raw,
                    )
            city = bounded(text_of(address.get("addressLocality")), 120)
            region = bounded(text_of(address.get("addressRegion")), 120)
            if city:
                col.prov(
                    "location.city",
                    ExtractionMethod.JSON_LD,
                    Confidence.HIGH,
                    selector="json_ld:address.addressLocality",
                    raw=city,
                )
        if country is None:
            country = self.config.country
            col.prov(
                "location.country",
                ExtractionMethod.DERIVED,
                Confidence.MEDIUM,
                transformation="source config country of the dealer website",
            )
        elif country != self.config.country:
            col.warn("SELLER_COUNTRY_DIFFERS_FROM_SOURCE")
        return LocationInfo(country=country, region=region, city=city)

    @staticmethod
    def _documentation(vehicle: dict[str, Any], col: _Collector) -> Documentation:
        vin_raw = scalar(_get(vehicle, "vehicleIdentificationNumber"))
        vin, valid = normalize_vin(vin_raw)
        if vin:
            col.prov(
                "documentation.vin",
                ExtractionMethod.JSON_LD,
                Confidence.HIGH,
                selector="json_ld:vehicleIdentificationNumber",
                raw=vin,
                transformation="format check only (ISO 3779 characters); not source-verified",
            )
        elif valid == Tristate.NO:
            col.warn("VIN_FORMAT_INVALID")
        owners: int | None = None
        owners_val, _ = _quantity(_get(vehicle, "numberOfPreviousOwners"))
        owners_dec = decimal_from_json(owners_val, kind="count") if owners_val is not None else None
        if owners_dec is not None and owners_dec == owners_dec.to_integral_value() and 0 <= owners_dec <= 50:
            owners = int(owners_dec)
            col.prov(
                "documentation.previous_owners",
                ExtractionMethod.JSON_LD,
                Confidence.HIGH,
                selector="json_ld:numberOfPreviousOwners",
                raw=owners_val,
            )
        emissions = bounded(text_of(_get(vehicle, "meetsEmissionStandard")), 40)
        if emissions:
            col.prov(
                "documentation.emissions_class",
                ExtractionMethod.JSON_LD,
                Confidence.HIGH,
                selector="json_ld:meetsEmissionStandard",
                raw=emissions,
            )
        return Documentation(
            vin=vin, vin_format_valid=valid, emissions_class=emissions, previous_owners=owners
        )

    def _inspection(
        self, page: PageView, condition: ConditionClaims, documentation: Documentation, col: _Collector
    ) -> tuple[ConditionClaims, Documentation]:
        """Technical-inspection wording in the visible page text (HU/TÜV, MFK/expertise, revisione).

        Seller wording only: ``condition.roadworthy`` is at most ``seller_claimed`` and the stated
        expiry is never a verified inspection result (spec 19). Negated ("ohne MFK", "nicht ab
        MFK", "Ab MFK: Nein"), expired and conditional ("ab MFK auf Wunsch") wording is never
        positive. A stated expiry before ``observed_at`` (its calendar day in the source time zone)
        adds ``INSPECTION_EXPIRED``.
        """
        text = page.visible_text
        if len(text) > MAX_INSPECTION_TEXT_LENGTH:
            col.warn("INSPECTION_TEXT_TOO_LONG")  # never truncated: a cut could drop a negation
            return condition, documentation
        # A stated expiry is a calendar date/month of the source's country: compare it with the
        # observation day there, not in UTC ("MFK bis 10/2026" observed 2026-10-31T23:30Z is
        # already November in Zurich). The zone was validated in __init__.
        observed_local = col.observed_at.astimezone(ZoneInfo(self.config.source_timezone)).date()
        result = parse_inspection(text, locale_for_country(self.config.country), observed_local)
        for code in result.warnings:
            if code != "EMPTY_INPUT":
                col.warn(code if code.startswith("INSPECTION_") else f"INSPECTION_{code}")
        raw = "; ".join(result.evidence) or None
        if result.roadworthy_claim != ClaimStatus.UNKNOWN:
            col.prov(
                "condition.roadworthy",
                ExtractionMethod.REGEX,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=raw,
                transformation=_inspection_note(result, "seller wording only; never a verified inspection"),
            )
            if result.roadworthy_claim == ClaimStatus.CONFLICTING:
                col.conflict(
                    "condition.roadworthy",
                    list(result.evidence),
                    ["visible_text"] * len(result.evidence),
                    note="contradictory inspection wording",
                )
            condition = condition.model_copy(update={"roadworthy": result.roadworthy_claim})
        expiry = result.inspection_expiry
        if expiry.value is not None:
            col.prov(
                "documentation.inspection_expiry",
                ExtractionMethod.REGEX,
                Confidence.MEDIUM,
                selector="visible_text",
                raw=raw,
                transformation=_inspection_note(
                    result, f"stated expiry {expiry.value} ({expiry.precision.value}); seller wording"
                ),
            )
            documentation = documentation.model_copy(update={"inspection_expiry": expiry})
        return condition, documentation

    @staticmethod
    def _co2(vehicle: dict[str, Any], page: PageView, col: _Collector) -> Co2Info:
        raw_val, _ = _quantity(_get(vehicle, "emissionsCO2"))
        value: Decimal | None = None
        if isinstance(raw_val, str):
            match = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)", raw_val)
            value = Decimal(match.group(1).replace(",", ".")) if match else None
        elif raw_val is not None and not json_number_is_ambiguous(raw_val):
            value = decimal_from_json(raw_val, kind="count")
        if value is None or not 0 <= value <= 1000:
            col.warn("CO2_MISSING")
            return Co2Info()
        cycle = Co2Cycle.UNKNOWN
        wltp, nedc = _CO2_WLTP.search(page.visible_text), _CO2_NEDC.search(page.visible_text)
        if wltp and not nedc:
            cycle = Co2Cycle.WLTP
        elif nedc and not wltp:
            cycle = Co2Cycle.NEDC
        else:
            col.warn("CO2_CYCLE_UNKNOWN")
        col.prov(
            "co2.g_per_km",
            ExtractionMethod.JSON_LD,
            Confidence.HIGH,
            selector="json_ld:emissionsCO2",
            raw=raw_val,
            transformation=f"cycle={cycle.value} (from page wording)" if cycle != Co2Cycle.UNKNOWN else None,
        )
        return Co2Info(g_per_km=value, cycle=cycle)

    @staticmethod
    def _condition(
        vehicle: dict[str, Any], offer: dict[str, Any] | None, description: str | None, col: _Collector
    ) -> ConditionClaims:
        damaged = ClaimStatus.UNKNOWN
        condition_raw = text_of(_get(offer, "itemCondition")) or text_of(_get(vehicle, "itemCondition"))
        if condition_raw and (enum_name(condition_raw) or "").lower() == "damagedcondition":
            damaged = ClaimStatus.SELLER_CLAIMED
            col.warn("DAMAGED_CONDITION")
            col.prov(
                "condition.damaged_vehicle",
                ExtractionMethod.JSON_LD,
                Confidence.HIGH,
                selector="json_ld:itemCondition",
                raw=condition_raw,
            )
        accident = ClaimStatus.UNKNOWN
        if description:
            free = _ACCIDENT_FREE.search(description)
            damage = _ACCIDENT_DAMAGE.search(description)
            if free and damage:
                accident = ClaimStatus.CONFLICTING
                col.conflict(
                    "condition.accident_free",
                    [free.group(0), damage.group(0)],
                    ["description", "description"],
                    note="contradictory accident wording",
                )
            elif free:
                accident = ClaimStatus.SELLER_CLAIMED
            elif damage:
                accident = ClaimStatus.SELLER_DENIED
            hit = free or damage
            if hit is not None:
                col.prov(
                    "condition.accident_free",
                    ExtractionMethod.REGEX,
                    Confidence.LOW,
                    selector="description",
                    raw=hit.group(0),
                    transformation="seller claim from untrusted description; unverified",
                )
        return ConditionClaims(accident_free=accident, damaged_vehicle=damaged)
