"""Listing identity, deduplication hashes and revision-promotion rules (spec section 10).

Pure functions, no I/O. Business rules:

- A provider's stable listing ID is the primary identity; otherwise a *source-scoped* canonical
  URL. Both the raw identity material and its SHA-256 are stored; a hash match whose material
  differs is a collision to quarantine, never a silent merge.
- URL canonicalisation applies only transformations that are safe for every HTTP(S) source:
  lowercase scheme/host, IDNA host, drop default port, drop fragment, drop *listed* tracking
  parameters (exact names plus the ``utm_*`` prefix). Every other query parameter keeps its order
  and exact encoding because it may carry the listing identity.
- Three hashes have different purposes: ``raw_content_hash`` (bytes, see ``listings.sha256_bytes``),
  ``semantic_hash`` (``NormalizedListing.semantic_hash``) and ``card_hash`` (stable search-card
  fields, here). Promotion position, badges, tracking and view counts never change a card hash.
- Out-of-order detail observations are resolved by generation and a deterministic tie-breaker
  (``decide_promotion``); an older generation never regresses current facts.
- Identity-critical changes (make/model, VIN, first registration, fuel, mileage decrease) open a
  new ``listing_incarnation`` instead of inheriting old evidence.
- Cross-source matching only *suggests* a ``possible_same_vehicle`` cluster; it never merges.
  Plate numbers and personal contact data are not inputs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.adapters.base import CanonicalIdentity
from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import Availability, Confidence, Fuel
from suv_deals.domain.listings import NormalizedListing, canonical_json, sha256_json
from suv_deals.domain.taxonomy import VehicleTaxonomy
from suv_deals.errors import ValidationFailed

_FROZEN = ConfigDict(frozen=True, extra="forbid")

MAX_URL_LENGTH = 2048
MAX_PROVIDER_ID_LENGTH = 200
URL_IDENTITY_PREFIX = "urlsha256:"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# URL canonicalisation and identity
# ---------------------------------------------------------------------------------------------


_HOST_CHARS = re.compile(r"^[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?$")


def _canonical_host(hostname: str) -> str:
    """Lower-case ASCII host; internationalised labels become IDNA ``xn--`` labels.

    Python's ``idna`` codec (IDNA 2003 nameprep) silently *maps* some characters to different
    ones (``straße`` -> ``strasse``, full-width letters -> ASCII), which would turn the URL into a
    different host. A host whose encoding does not decode back to the same lower-cased name is
    therefore rejected instead of being rewritten. Percent signs and other characters that are not
    valid in a DNS name are rejected too.
    """
    name = unicodedata.normalize("NFC", hostname.rstrip(".")).lower()
    if not name:
        raise ValidationFailed("URL has no host")
    try:
        host = name.encode("idna").decode("ascii").lower()
        round_trip = host.encode("ascii").decode("idna").lower()
    except UnicodeError as exc:
        raise ValidationFailed("URL host is not a valid internationalised domain name") from exc
    if round_trip != name:
        raise ValidationFailed("URL host uses characters whose IDNA mapping would change the host")
    if not _HOST_CHARS.match(host):
        raise ValidationFailed("URL host contains characters that are not valid in a host name")
    return host


def _is_tracking(name: str, tracking: frozenset[str]) -> bool:
    decoded = unquote(name.replace("+", " ")).strip().lower()
    return decoded in tracking or decoded.startswith("utm_")


def canonicalize_url(url: str, tracking_params: Iterable[str]) -> str:
    """Return the canonical form of an HTTP(S) listing URL.

    Raises ``ValidationFailed`` for non-http(s) schemes, embedded credentials, control characters,
    a missing/invalid host or port (including port 0 and IDNA mappings that would change the host,
    see ``_canonical_host``), or a URL longer than 2048 characters. Path case and every
    non-tracking query parameter (order, value and percent-encoding) are preserved; dot segments
    and ``;``-separated parameters are left untouched because their meaning is source specific.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValidationFailed("URL is empty")
    candidate = url.strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise ValidationFailed("URL is too long")
    if _CONTROL.search(candidate):
        raise ValidationFailed("URL contains whitespace or control characters")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ValidationFailed("URL is malformed") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValidationFailed("only http and https URLs are accepted")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValidationFailed("URLs with embedded credentials are rejected")
    hostname = parts.hostname
    if not hostname:
        raise ValidationFailed("URL has no host")
    # An IPv6 literal keeps its brackets; every other host goes through _canonical_host.
    host = f"[{hostname.lower()}]" if ":" in hostname else _canonical_host(hostname)
    if port == 0:
        raise ValidationFailed("URL port is invalid")
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = parts.path or "/"
    tracking = frozenset(p.strip().lower() for p in tracking_params if p and p.strip())
    kept = [
        piece
        for piece in parts.query.split("&")
        if piece and not _is_tracking(piece.split("=", 1)[0], tracking)
    ]
    query = "&".join(kept)
    result = f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")
    if len(result) > MAX_URL_LENGTH:
        raise ValidationFailed("URL is too long")
    return result


def identity_from(
    source_key: str,
    provider_listing_id: str | None,
    url: str,
    tracking_params: Iterable[str],
) -> CanonicalIdentity:
    """Build the canonical identity of a source listing (spec 10).

    - Provider ID present: ``identity_method='provider_id'``, material ``f"{source_key}:{id}"``.
    - Otherwise: ``identity_method='canonical_url'``, material ``f"{source_key}:{canonical_url}"``
      and a synthetic ``source_listing_id`` ``"urlsha256:<identity_hash>"`` (the raw URL can exceed
      the 200-character ID limit).
    ``identity_hash`` is the SHA-256 of the exact material; both are returned for storage.
    """
    key = (source_key or "").strip()
    if not key or _CONTROL.search(key) or ":" in key:
        raise ValidationFailed("source_key must be a non-empty identifier without ':' or whitespace")
    canonical = canonicalize_url(url, tracking_params)
    provider_id = provider_listing_id.strip() if provider_listing_id is not None else ""
    if provider_id:
        if len(provider_id) > MAX_PROVIDER_ID_LENGTH or re.search(r"[\x00-\x1f\x7f]", provider_id):
            raise ValidationFailed("provider listing ID is too long or contains control characters")
        material = f"{key}:{provider_id}"
        digest = _sha256_text(material)
        return CanonicalIdentity(
            source_key=key,
            source_listing_id=provider_id,
            canonical_url=canonical,
            identity_method="provider_id",
            identity_material=material,
            identity_hash=digest,
        )
    material = f"{key}:{canonical}"
    if len(material) > MAX_URL_LENGTH:
        raise ValidationFailed("URL identity material is too long")
    digest = _sha256_text(material)
    return CanonicalIdentity(
        source_key=key,
        source_listing_id=f"{URL_IDENTITY_PREFIX}{digest}",
        canonical_url=canonical,
        identity_method="canonical_url",
        identity_material=material,
        identity_hash=digest,
    )


IdentityComparison = Literal["same", "different", "hash_collision"]


def compare_identity(
    stored_material: str, stored_hash: str, incoming: CanonicalIdentity
) -> IdentityComparison:
    """Compare an incoming identity with a stored one by *material*, not only by hash.

    ``hash_collision`` (equal hashes, different material) must be quarantined and alerted; it is
    never merged (spec 10). Material equal but hash different means the stored hash is corrupt and
    is also reported as ``hash_collision`` so it cannot be silently trusted.
    """
    same_hash = stored_hash == incoming.identity_hash
    same_material = stored_material == incoming.identity_material
    if same_hash and same_material:
        return "same"
    if same_hash != same_material:
        return "hash_collision"
    return "different"


def needs_url_alias(existing: CanonicalIdentity, incoming: CanonicalIdentity) -> bool:
    """True when the same provider-ID identity is now seen under a different canonical URL.

    The new URL must be linked through an alias record with evidence (spec 10); the identity
    itself does not change.
    """
    return (
        existing.identity_method == "provider_id"
        and incoming.identity_method == "provider_id"
        and existing.identity_hash == incoming.identity_hash
        and existing.canonical_url != incoming.canonical_url
    )


# ---------------------------------------------------------------------------------------------
# Card hash and ingestion key
# ---------------------------------------------------------------------------------------------

# Documented card-hash field list (spec 8, 10). Anything else -- promotion position, badges,
# "top ad"/highlight flags, tracking parameters, view or watch counts -- is ignored.
CARD_HASH_FIELDS: tuple[str, ...] = (
    "source_listing_id",
    "canonical_url",
    "title",
    "price_minor",
    "currency",
    "mileage_km",
    "source_modified_at",
)
_WS = re.compile(r"\s+")


def _norm_text(value: str) -> str | None:
    text = _WS.sub(" ", unicodedata.normalize("NFC", value)).strip()
    return text or None


def _norm_decimal(name: str, value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationFailed(f"card field {name} is not a decimal string") from exc
    if not number.is_finite() or number < 0:
        raise ValidationFailed(f"card field {name} must be a finite non-negative number")
    if name == "price_minor" and number != number.to_integral_value():
        raise ValidationFailed("card field price_minor must be an integer")
    normalized = format(number.normalize(), "f")
    return "0" if normalized in ("-0", "") else normalized


def card_hash(material: Mapping[str, str | None]) -> tuple[str, dict[str, str | None]]:
    """Return ``(card_hash, normalized_material)`` for a search-result card.

    Normalisation (stable across cosmetic changes): only ``CARD_HASH_FIELDS`` are used, each present
    (missing -> ``None``); text is NFC-normalised with collapsed whitespace; ``price_minor`` and
    ``mileage_km`` are canonical decimal strings (``'187500.0'`` == ``'187500'``); ``currency`` is
    upper-case; empty strings become ``None``. The hash is ``sha256_json`` of that dict, and the
    dict itself must be stored next to the hash (spec 8).
    """
    normalized: dict[str, str | None] = {}
    for name in CARD_HASH_FIELDS:
        value = material.get(name)
        if value is None:
            normalized[name] = None
        elif name in ("price_minor", "mileage_km"):
            normalized[name] = _norm_decimal(name, value)
        elif name == "currency":
            normalized[name] = value.strip().upper() or None
        else:
            normalized[name] = _norm_text(value)
    return sha256_json(normalized), normalized


def ingestion_key(
    source_key: str,
    run_id: str | UUID,
    page_number: int,
    source_listing_id: str | None,
    observed_card_hash: str,
) -> str:
    """Event-ingestion key ``(source, run, page, source_listing_id, card_hash)`` (spec 10).

    Replaying the same observation yields the same key so it is stored once.
    """
    if page_number < 1:
        raise ValidationFailed("page_number must be >= 1")
    if not re.fullmatch(r"[0-9a-f]{64}", observed_card_hash):
        raise ValidationFailed("card hash must be a lowercase SHA-256 hex digest")
    material = [source_key, str(run_id), page_number, source_listing_id, observed_card_hash]
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def merge_seen_window(
    first_seen_at: datetime | None, last_seen_at: datetime | None, observed_at: datetime
) -> tuple[datetime, datetime]:
    """``first_seen = min(trustworthy observations)``, ``last_seen = greatest(existing, observed)``."""
    observed = ensure_utc(observed_at)
    first = observed if first_seen_at is None else min(ensure_utc(first_seen_at), observed)
    last = observed if last_seen_at is None else max(ensure_utc(last_seen_at), observed)
    return first, last


# ---------------------------------------------------------------------------------------------
# Revision promotion for out-of-order detail observations (spec 10)
# ---------------------------------------------------------------------------------------------


class PromotionOutcome(StrEnum):
    PROMOTE_NEW_REVISION = "promote_new_revision"
    CONFIRM_UNCHANGED = "confirm_unchanged"
    HISTORICAL_ONLY = "historical_only"
    DUPLICATE_REPLAY = "duplicate_replay"
    INCIDENT_CONFLICTING_REPLAY = "incident_conflicting_replay"


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check_hash(value: str | None) -> str | None:
    if value is not None and not _HEX64.match(value):
        raise ValueError("semantic hash must be a lowercase SHA-256 hex digest")
    return value


class ListingCurrentState(BaseModel):
    """Current facts of one listing incarnation, read under the listing row lock.

    ``current_generation is None`` means no detail observation has been accepted yet
    (``revision_number`` is then 0).
    """

    model_config = _FROZEN

    current_generation: int | None = Field(default=None, ge=1)
    accepted_observation_id: UUID | None = None
    current_semantic_hash: str | None = None
    revision_number: int = Field(default=0, ge=0)
    availability: Availability = Availability.UNKNOWN

    @field_validator("current_semantic_hash")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        return _check_hash(value)

    def model_post_init(self, _context: object) -> None:
        accepted = (self.current_generation, self.accepted_observation_id, self.current_semantic_hash)
        if any(v is None for v in accepted) and any(v is not None for v in accepted):
            raise ValueError("generation, accepted observation and semantic hash are set together")
        if self.current_generation is None and self.revision_number != 0:
            raise ValueError("a listing without an accepted observation has revision_number 0")
        if self.current_generation is not None and self.revision_number < 1:
            raise ValueError("an accepted observation implies revision_number >= 1")


class DetailObservation(BaseModel):
    """A completed detail fetch. Retries of the same scheduled observation share ``generation``."""

    model_config = _FROZEN

    generation: int = Field(ge=1)
    observation_id: UUID
    semantic_hash: str
    availability: Availability = Availability.UNKNOWN
    observed_at: datetime

    @field_validator("semantic_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        checked = _check_hash(value)
        assert checked is not None
        return checked

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class PromotionDecision(BaseModel):
    """What the caller must do under the listing row lock.

    ``current_generation``, ``accepted_observation_id``, ``current_semantic_hash``,
    ``revision_number`` and ``availability`` are the listing's state *after* applying the decision.
    The caller writes them whenever ``update_listing_state`` is true -- which includes the
    same-generation equivalent retry whose only change is the tie-break moving
    ``accepted_observation_id`` to the lower ID (``promote_current`` is false there because the
    current *facts* do not change). ``promote_current`` means the incoming observation's facts
    become the current facts; ``create_revision`` means a new immutable revision row is inserted.
    """

    model_config = _FROZEN

    outcome: PromotionOutcome
    update_listing_state: bool
    promote_current: bool
    create_revision: bool
    revision_number: int
    current_generation: int | None
    accepted_observation_id: UUID | None
    current_semantic_hash: str | None
    availability: Availability
    store_historical_evidence: bool
    refresh_last_detail_success: bool
    incident: bool = False
    incident_code: str | None = None
    superseded_observation_id: UUID | None = None
    explanation: str


def decide_promotion(state: ListingCurrentState, incoming: DetailObservation) -> PromotionDecision:
    """Decide how an incoming detail observation affects current listing facts (spec 10).

    1. ``incoming.generation > current_generation`` (or nothing accepted yet):
       same semantic hash -> ``CONFIRM_UNCHANGED`` (refresh ``last_detail_success`` only);
       different hash -> ``PROMOTE_NEW_REVISION`` with ``revision_number + 1``. A return to an
       earlier value (A -> B -> A) is a new chronological revision.
    2. ``incoming.generation < current_generation``: ``HISTORICAL_ONLY``. A late older result is
       retained as evidence but never regresses revision, price, availability or verified facts.
    3. Same generation (a retry/replay of the same scheduled observation):
       a. same observation ID and same hash -> ``DUPLICATE_REPLAY`` (no-op, nothing stored);
       b. different observation ID, same hash -> ``DUPLICATE_REPLAY`` (an equivalent retry; kept as
          evidence; the accepted ID becomes the lower of the two IDs, the deterministic tie-break,
          reported with ``update_listing_state=True`` when it changes). Keeping the lowest ID
          accepted is what makes the final facts independent of completion order;
       c. same observation ID, different hash -> ``INCIDENT_CONFLICTING_REPLAY``: the replayed
          payload contradicts the accepted one; current facts stay, the payload is kept as incident
          evidence (``incident_code='REPLAY_PAYLOAD_MISMATCH'``);
       d. different observation ID, different hash -> ``INCIDENT_CONFLICTING_REPLAY`` with the
          deterministic tie-break: the observation with the *lower* UUID is authoritative. If that is
          the incoming one it is promoted as a new revision (``revision_number + 1``) and the
          previously accepted observation is reported in ``superseded_observation_id``; otherwise
          current facts stay. Either way ``incident_code='CONFLICTING_SAME_GENERATION'``.
    """
    current_gen = state.current_generation
    if current_gen is None or incoming.generation > current_gen:
        unchanged = state.current_semantic_hash == incoming.semantic_hash
        return PromotionDecision(
            outcome=PromotionOutcome.CONFIRM_UNCHANGED
            if unchanged
            else PromotionOutcome.PROMOTE_NEW_REVISION,
            update_listing_state=True,
            promote_current=True,
            create_revision=not unchanged,
            revision_number=state.revision_number if unchanged else state.revision_number + 1,
            current_generation=incoming.generation,
            accepted_observation_id=incoming.observation_id,
            current_semantic_hash=incoming.semantic_hash,
            availability=incoming.availability,
            store_historical_evidence=True,
            refresh_last_detail_success=True,
            explanation=(
                "newer generation with unchanged semantic content"
                if unchanged
                else "newer generation with changed semantic content"
            ),
        )
    if incoming.generation < current_gen:
        return _keep_current(
            state,
            PromotionOutcome.HISTORICAL_ONLY,
            store_historical_evidence=True,
            explanation=f"generation {incoming.generation} completed after generation {current_gen}",
        )
    accepted = state.accepted_observation_id
    assert accepted is not None
    same_id = incoming.observation_id == accepted
    if incoming.semantic_hash == state.current_semantic_hash:
        return _keep_current(
            state,
            PromotionOutcome.DUPLICATE_REPLAY,
            store_historical_evidence=not same_id,
            accepted_observation_id=min(accepted, incoming.observation_id),
            explanation="replay of the accepted observation"
            if same_id
            else "equivalent retry in same generation",
        )
    if same_id:
        return _keep_current(
            state,
            PromotionOutcome.INCIDENT_CONFLICTING_REPLAY,
            store_historical_evidence=True,
            incident_code="REPLAY_PAYLOAD_MISMATCH",
            explanation="replayed observation ID carries a different payload",
        )
    if incoming.observation_id < accepted:
        return PromotionDecision(
            outcome=PromotionOutcome.INCIDENT_CONFLICTING_REPLAY,
            update_listing_state=True,
            promote_current=True,
            create_revision=True,
            revision_number=state.revision_number + 1,
            current_generation=current_gen,
            accepted_observation_id=incoming.observation_id,
            current_semantic_hash=incoming.semantic_hash,
            availability=incoming.availability,
            store_historical_evidence=True,
            refresh_last_detail_success=False,
            incident=True,
            incident_code="CONFLICTING_SAME_GENERATION",
            superseded_observation_id=accepted,
            explanation="conflicting observations in one generation; lower observation ID is authoritative",
        )
    return _keep_current(
        state,
        PromotionOutcome.INCIDENT_CONFLICTING_REPLAY,
        store_historical_evidence=True,
        incident_code="CONFLICTING_SAME_GENERATION",
        explanation="conflicting observations in one generation; accepted (lower) observation ID kept",
    )


def _keep_current(
    state: ListingCurrentState,
    outcome: PromotionOutcome,
    *,
    store_historical_evidence: bool,
    explanation: str,
    incident_code: str | None = None,
    accepted_observation_id: UUID | None = None,
) -> PromotionDecision:
    accepted = accepted_observation_id or state.accepted_observation_id
    return PromotionDecision(
        outcome=outcome,
        update_listing_state=accepted != state.accepted_observation_id,
        promote_current=False,
        create_revision=False,
        revision_number=state.revision_number,
        current_generation=state.current_generation,
        accepted_observation_id=accepted,
        current_semantic_hash=state.current_semantic_hash,
        availability=state.availability,
        store_historical_evidence=store_historical_evidence,
        refresh_last_detail_success=False,
        incident=incident_code is not None,
        incident_code=incident_code,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------------------------
# VIN helpers (ISO 3779)
# ---------------------------------------------------------------------------------------------

_VIN_FORMAT = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_VIN_VALUES = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
    **dict(zip("JKLMN", range(1, 6), strict=True)),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10), strict=True)),
}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
# World manufacturer identifiers whose regions mandate the position-9 check digit (North America).
_CHECK_DIGIT_MANDATORY_PREFIXES = frozenset("12345")

VinCheckDigitStatus = Literal["valid", "invalid", "not_applicable"]


def normalize_vin(text: str | None) -> str | None:
    """Upper-case and strip spaces/hyphens. Returns None for empty input (no validation)."""
    if text is None:
        return None
    cleaned = re.sub(r"[\s\-]", "", text).upper()
    return cleaned or None


def is_vin_format_valid(vin: str | None) -> bool:
    """17 characters from ``A-Z0-9`` excluding ``I``, ``O`` and ``Q``."""
    return vin is not None and bool(_VIN_FORMAT.match(vin))


def vin_check_digit_status(vin: str | None) -> VinCheckDigitStatus:
    """ISO 3779 check digit at position 9.

    ``valid`` when it matches. A mismatch is ``invalid`` only for North-American WMIs (first
    character 1-5), where the check digit is mandatory; elsewhere (EU and others) manufacturers need
    not use it, so a mismatch is ``not_applicable`` -- an EU VIN is never rejected for it.
    Malformed VINs are ``not_applicable`` (check ``is_vin_format_valid`` first).
    """
    if vin is None or not is_vin_format_valid(vin):
        return "not_applicable"
    total = sum(_VIN_VALUES[c] * w for c, w in zip(vin, _VIN_WEIGHTS, strict=True))
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)
    if vin[8] == expected:
        return "valid"
    return "invalid" if vin[0] in _CHECK_DIGIT_MANDATORY_PREFIXES else "not_applicable"


# ---------------------------------------------------------------------------------------------
# Identity conflicts -> new listing_incarnation (spec 10 "Relisted advertisements")
# ---------------------------------------------------------------------------------------------

MILEAGE_DECREASE_TOLERANCE_KM = Decimal("5000")
FIRST_REGISTRATION_YEAR_TOLERANCE = 1


class IdentityConflictCode(StrEnum):
    MAKE_CHANGED = "MAKE_CHANGED"
    MODEL_CHANGED = "MODEL_CHANGED"
    VIN_CHANGED = "VIN_CHANGED"
    FIRST_REGISTRATION_YEAR_CHANGED = "FIRST_REGISTRATION_YEAR_CHANGED"
    FUEL_CHANGED = "FUEL_CHANGED"
    MILEAGE_DECREASED = "MILEAGE_DECREASED"


class IdentityConflictReason(BaseModel):
    model_config = _FROZEN

    code: IdentityConflictCode
    field: str
    previous: str
    incoming: str
    message: str


def _norm_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]+", " ", ascii_only.casefold()).strip()


def _same_model_text(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    return longer == shorter or longer.startswith(shorter + " ")


def detect_identity_conflict(
    previous: NormalizedListing,
    incoming: NormalizedListing,
    *,
    taxonomy: VehicleTaxonomy | None = None,
) -> list[IdentityConflictReason]:
    """Typed reasons why ``incoming`` is implausibly the same car as ``previous``.

    A non-empty result means: open an identity-conflict case and a new ``listing_incarnation``;
    never inherit tax, review or sale evidence. Only fields known on *both* sides are compared
    (unknown is never a change). Rules: different make or model (aliases resolved through the
    taxonomy when given; ``'Tiguan'`` vs ``'Tiguan 2.0 TDI'`` is the same model); two different
    format-valid VINs; first-registration year differing by more than 1; a different known fuel;
    mileage decreasing by more than 5,000 km.
    """
    reasons: list[IdentityConflictReason] = []
    pv, iv = previous.vehicle, incoming.vehicle

    if pv.make and iv.make:
        p_make = taxonomy.canonical_make(pv.make) if taxonomy else None
        i_make = taxonomy.canonical_make(iv.make) if taxonomy else None
        make_changed = p_make != i_make if p_make and i_make else _norm_name(pv.make) != _norm_name(iv.make)
        if make_changed:
            reasons.append(
                IdentityConflictReason(
                    code=IdentityConflictCode.MAKE_CHANGED,
                    field="vehicle.make",
                    previous=pv.make,
                    incoming=iv.make,
                    message="make changed between observations",
                )
            )
        elif pv.model and iv.model:
            p_model = i_model = None
            if taxonomy is not None:
                p_model = taxonomy.match_vehicle(pv.make, pv.model, None, None).model
                i_model = taxonomy.match_vehicle(iv.make, iv.model, None, None).model
            model_changed = (
                p_model != i_model if p_model and i_model else not _same_model_text(pv.model, iv.model)
            )
            if model_changed:
                reasons.append(
                    IdentityConflictReason(
                        code=IdentityConflictCode.MODEL_CHANGED,
                        field="vehicle.model",
                        previous=pv.model,
                        incoming=iv.model,
                        message="model changed between observations",
                    )
                )

    p_vin, i_vin = previous.documentation.vin, incoming.documentation.vin
    if is_vin_format_valid(p_vin) and is_vin_format_valid(i_vin) and p_vin != i_vin:
        assert p_vin is not None and i_vin is not None
        reasons.append(
            IdentityConflictReason(
                code=IdentityConflictCode.VIN_CHANGED,
                field="documentation.vin",
                previous=p_vin,
                incoming=i_vin,
                message="a different valid VIN was stated",
            )
        )

    p_year, i_year = pv.first_registration.year, iv.first_registration.year
    if p_year is not None and i_year is not None and abs(p_year - i_year) > FIRST_REGISTRATION_YEAR_TOLERANCE:
        reasons.append(
            IdentityConflictReason(
                code=IdentityConflictCode.FIRST_REGISTRATION_YEAR_CHANGED,
                field="vehicle.first_registration",
                previous=str(p_year),
                incoming=str(i_year),
                message="first-registration year changed by more than one year",
            )
        )

    if Fuel.UNKNOWN not in (pv.fuel, iv.fuel) and pv.fuel != iv.fuel:
        reasons.append(
            IdentityConflictReason(
                code=IdentityConflictCode.FUEL_CHANGED,
                field="vehicle.fuel",
                previous=pv.fuel.value,
                incoming=iv.fuel.value,
                message="fuel type changed",
            )
        )

    if (
        pv.mileage_km is not None
        and iv.mileage_km is not None
        and pv.mileage_km - iv.mileage_km > MILEAGE_DECREASE_TOLERANCE_KM
    ):
        reasons.append(
            IdentityConflictReason(
                code=IdentityConflictCode.MILEAGE_DECREASED,
                field="vehicle.mileage_km",
                previous=str(pv.mileage_km),
                incoming=str(iv.mileage_km),
                message="mileage decreased by more than 5,000 km",
            )
        )
    return reasons


# ---------------------------------------------------------------------------------------------
# Cross-source possible_same_vehicle suggestions (never merges)
# ---------------------------------------------------------------------------------------------

# PROPOSED engineering weights; they rank suggestions for human review and are not probabilities.
SIGNAL_WEIGHTS: dict[str, Decimal] = {
    "VIN_MATCH": Decimal("0.90"),
    "SPEC_MATCH": Decimal("0.25"),
    "FIRST_REGISTRATION_MATCH": Decimal("0.15"),
    "FIRST_REGISTRATION_YEAR_MATCH": Decimal("0.05"),
    "MILEAGE_WITHIN_2PCT": Decimal("0.20"),
    "PRICE_WITHIN_10PCT": Decimal("0.10"),
    "PHOTO_SIMILAR": Decimal("0.20"),
    "SAME_SELLER": Decimal("0.10"),
    "SPEC_CONFLICT": Decimal("-0.50"),
}
SUGGESTION_MIN_SCORE = Decimal("0.50")
MEDIUM_CONFIDENCE_SCORE = Decimal("0.70")
PHOTO_SIMILARITY_THRESHOLD = Decimal("0.90")
MILEAGE_MATCH_PCT = Decimal("2")
PRICE_MATCH_PCT = Decimal("10")
POWER_MATCH_TOLERANCE_KW = 2


class SameVehicleSignal(BaseModel):
    model_config = _FROZEN

    code: str
    strength: Literal["strong", "supporting", "negative"]
    weight: Decimal
    detail: str


class SameVehicleSuggestion(BaseModel):
    """A *suggested* ``possible_same_vehicle`` link for human review; never a merge."""

    model_config = _FROZEN

    suggest: bool
    confidence: Confidence | None
    score: Decimal
    signals: tuple[SameVehicleSignal, ...]
    vin_mismatch: bool = False


def _within_pct(a: Decimal, b: Decimal, pct: Decimal) -> bool:
    largest = max(a, b)
    if largest == 0:
        return a == b
    return abs(a - b) * 100 <= largest * pct


def possible_same_vehicle(
    a: NormalizedListing,
    b: NormalizedListing,
    *,
    photo_similarity: Decimal | None = None,
    same_seller: bool | None = None,
) -> SameVehicleSuggestion:
    """Score whether two listings (normally from different sources) may be the same vehicle.

    Strong evidence: equal VINs, only when both are format-valid (HIGH confidence unless the
    specifications conflict, which downgrades the suggestion to LOW). Two different valid VINs mean
    different vehicles (``vin_mismatch=True``, never suggested). Supporting evidence: exact
    specification (make, model, fuel, gearbox, drive, power within 2 kW, displacement), identical
    first registration, mileage within 2 %, price within 10 % (same currency only; no FX here),
    optional photo similarity (0..1, from a separate comparison) and same-seller flag. Plate numbers
    and personal contact data are deliberately not inputs.
    """
    if a.source_key == b.source_key and a.source_listing_id == b.source_listing_id:
        raise ValidationFailed("possible_same_vehicle compares two different listings")
    signals: list[SameVehicleSignal] = []

    def add(code: str, strength: Literal["strong", "supporting", "negative"], detail: str) -> None:
        signals.append(
            SameVehicleSignal(code=code, strength=strength, weight=SIGNAL_WEIGHTS[code], detail=detail)
        )

    va, vb = a.documentation.vin, b.documentation.vin
    if is_vin_format_valid(va) and is_vin_format_valid(vb):
        if va != vb:
            return SameVehicleSuggestion(
                suggest=False,
                confidence=None,
                score=Decimal(0),
                vin_mismatch=True,
                signals=(
                    SameVehicleSignal(
                        code="VIN_MISMATCH",
                        strength="negative",
                        weight=Decimal(-1),
                        detail="different valid VINs",
                    ),
                ),
            )
        add("VIN_MATCH", "strong", "identical format-valid VIN")

    sa, sb = a.vehicle, b.vehicle
    spec_pairs: list[tuple[str, object, object]] = [
        ("make", _norm_name(sa.make) if sa.make else None, _norm_name(sb.make) if sb.make else None),
        ("model", sa.model, sb.model),
        ("fuel", None if sa.fuel == Fuel.UNKNOWN else sa.fuel, None if sb.fuel == Fuel.UNKNOWN else sb.fuel),
        (
            "gearbox",
            sa.gearbox if sa.gearbox.value != "unknown" else None,
            sb.gearbox if sb.gearbox.value != "unknown" else None,
        ),
        (
            "drive",
            sa.drive if sa.drive.value != "unknown" else None,
            sb.drive if sb.drive.value != "unknown" else None,
        ),
        ("engine_displacement_cm3", sa.engine_displacement_cm3, sb.engine_displacement_cm3),
    ]
    conflicts = []
    all_known_equal = True
    for name, left, right in spec_pairs:
        if left is None or right is None:
            all_known_equal = False
            continue
        same = _same_model_text(str(left), str(right)) if name == "model" else left == right
        if not same:
            conflicts.append(name)
    if sa.power_kw is not None and sb.power_kw is not None:
        if abs(sa.power_kw - sb.power_kw) > POWER_MATCH_TOLERANCE_KW:
            conflicts.append("power_kw")
    else:
        all_known_equal = False
    if conflicts:
        add("SPEC_CONFLICT", "negative", "differs: " + ", ".join(conflicts))
    elif all_known_equal:
        add("SPEC_MATCH", "supporting", "make, model, fuel, gearbox, drive, power and displacement agree")

    fa, fb = sa.first_registration, sb.first_registration
    if fa.value is not None and fb.value is not None:
        if fa.value == fb.value and fa.precision == fb.precision and len(fa.value) >= 7:
            add("FIRST_REGISTRATION_MATCH", "supporting", f"first registration {fa.value}")
        elif fa.year == fb.year:
            add("FIRST_REGISTRATION_YEAR_MATCH", "supporting", f"first-registration year {fa.year}")

    if (
        sa.mileage_km is not None
        and sb.mileage_km is not None
        and _within_pct(sa.mileage_km, sb.mileage_km, MILEAGE_MATCH_PCT)
    ):
        add("MILEAGE_WITHIN_2PCT", "supporting", f"{sa.mileage_km} km vs {sb.mileage_km} km")

    pa, pb = a.price, b.price
    if (
        pa.amount_minor is not None
        and pb.amount_minor is not None
        and pa.currency
        and pa.currency == pb.currency
        and _within_pct(Decimal(pa.amount_minor), Decimal(pb.amount_minor), PRICE_MATCH_PCT)
    ):
        add("PRICE_WITHIN_10PCT", "supporting", f"prices within 10 % ({pa.currency})")

    if photo_similarity is not None:
        if not Decimal(0) <= photo_similarity <= Decimal(1):
            raise ValidationFailed("photo_similarity must be within 0..1")
        if photo_similarity >= PHOTO_SIMILARITY_THRESHOLD:
            add("PHOTO_SIMILAR", "supporting", f"photo similarity {photo_similarity}")
    if same_seller:
        add("SAME_SELLER", "supporting", "same seller reference")

    score = sum((s.weight for s in signals), Decimal(0))
    score = min(max(score, Decimal(0)), Decimal(1))
    vin_match = any(s.code == "VIN_MATCH" for s in signals)
    spec_conflict = any(s.code == "SPEC_CONFLICT" for s in signals)
    if vin_match and spec_conflict:
        # Same VIN but e.g. a different make/fuel: a copied, placeholder or mistyped VIN is as likely
        # as a data error, so it is surfaced for human review at LOW confidence, never HIGH.
        confidence: Confidence | None = Confidence.LOW
    elif vin_match:
        confidence = Confidence.HIGH
    elif score >= MEDIUM_CONFIDENCE_SCORE:
        confidence = Confidence.MEDIUM
    elif score >= SUGGESTION_MIN_SCORE:
        confidence = Confidence.LOW
    else:
        confidence = None
    return SameVehicleSuggestion(
        suggest=confidence is not None, confidence=confidence, score=score, signals=tuple(signals)
    )
