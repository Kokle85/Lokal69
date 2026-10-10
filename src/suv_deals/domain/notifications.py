"""Notification materiality, event payloads and safe owner wording (spec sections 18, 22).

Pure domain code; delivery lives in ``integrations/*`` and ``workers/*``.

Materiality (spec 18 "Invalidation and material changes"):

- Notification materiality is a separate, versioned policy (``MATERIALITY_POLICY_VERSION``).
- PROPOSED price re-alert thresholds: an absolute change of EUR 100 **or** 5 percent of the
  previous price; both boundaries are inclusive (exactly EUR 100 / exactly 5 % are material).
  They are not owner-approved: until ``policy_approved`` is true a price-only change is
  reported as material but ``realert_allowed`` stays false (``PRICE_REALERT_POLICY_UNAPPROVED``).
- Always material, regardless of numeric thresholds, and always invalidating a prior
  recommendation: eligibility crossing, risk boundary crossing, removal/reservation/sold claim
  or other availability change, a price becoming unknown, newly invalid tax rules or evidence,
  and a materially different contribution scenario (completeness change, three-way sign change
  of the conservative/base contribution, or a change in whether the threshold is met).
- Newly resolved documentation is a material re-alert reason that does not invalidate.

Payloads (spec 22):

- ``build_review_pending_event`` returns the internal outbox payload exactly in the spec 22
  shape, plus ``readiness`` and ``profile`` (and optionally a ``queue`` slug), which the native
  MCP Events provider needs for the occurrence data and its server-enforced subscription
  filters. ``deduplication_key = review.pending:<case_id>:<case_version>``.
- ``to_mcp_occurrence`` maps it to the native occurrence envelope (``review.pending.v1``,
  ``cursor: null``) with only the six minimal reference fields.
- ``guard_payload`` rejects credentials/secret tokens, phone numbers, e-mail addresses, raw
  listings/HTML, URLs with embedded credentials or token-like query parameters, and payloads
  over 256 KiB. Dashboard links never embed tokens; they require normal dashboard sign-in.
- Fixture/synthetic events are never routed externally: their outbox rows start ``blocked``
  with ``FIXTURE_EVENT``, their payload carries ``"fixture": true`` and a ``[SYNTHETIC FIXTURE]``
  summary prefix, and ``to_mcp_occurrence`` refuses any of those markers.
- Phone detection covers international (+/00), trunk-prefixed (0...) and Italian mobile (3xx)
  numbers, including parenthesised forms such as ``(0171) 1234567`` and ``+49 (0)171 ...``.

Owner-facing messages (spec 22 "Owner-facing opportunity messages"):

- Plain text, labelled "research candidate" and "estimated"; contribution figures only when the
  valuation is complete; unknown costs are listed, never shown as zero.
- A guard rejects "guaranteed profit", "guaranteed", "risk-free", "verified accident-free",
  "seller confirmed" and "net profit" unless explicit evidence flags support the claim (the
  "guaranteed"/"risk-free" family is never supportable).
- Seller-provided strings are untrusted: control/format characters, links, contact data and
  markdown/mention syntax are removed, length is bounded, and forbidden claims are neutralised.

Quiet hours are evaluated in Europe/Skopje and apply only after the owner has chosen them
(PROPOSED preferences are not applied).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Final, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    CostCategory,
    Drive,
    EligibilityState,
    Fuel,
    Gearbox,
    OdometerClaim,
    OutboxState,
    ProfileKey,
    ReviewState,
    ValuationState,
)
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.domain.reviews import ReviewCaseSnapshot
from suv_deals.errors import Forbidden, ValidationFailed

MATERIALITY_POLICY_VERSION: Final = "materiality@1.0.0"
PAYLOAD_SCHEMA_VERSION: Final = "1.0"
EVENT_TYPE: Final = "review.pending"
MCP_EVENT_NAME: Final = "review.pending.v1"
MAX_PAYLOAD_BYTES: Final = 256 * 1024
MAX_PAYLOAD_STRING: Final = 4000
OWNER_TIMEZONE: Final = "Europe/Skopje"
FIXTURE_BLOCKER: Final = "FIXTURE_EVENT"
FIXTURE_SUMMARY_PREFIX: Final = "[SYNTHETIC FIXTURE]"
PROPOSED_PRICE_REALERT_ABS_EUR: Final = Decimal("100")
PROPOSED_PRICE_REALERT_PCT: Final = Decimal("5")
_FROZEN = ConfigDict(frozen=True, extra="forbid")
_QUEUE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_READINESS_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HUNDRED: Final = Decimal(100)


# =============================================================================================
# Materiality
# =============================================================================================


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKING = "blocking"
    UNKNOWN = "unknown"


class MaterialityReason(StrEnum):
    FIRST_ALERT = "FIRST_ALERT"
    PRICE_DECREASE = "PRICE_DECREASE"
    PRICE_INCREASE = "PRICE_INCREASE"
    PRICE_BECAME_UNKNOWN = "PRICE_BECAME_UNKNOWN"
    PRICE_BECAME_KNOWN = "PRICE_BECAME_KNOWN"
    ELIGIBILITY_CROSSED = "ELIGIBILITY_CROSSED"
    RISK_BOUNDARY_CROSSED = "RISK_BOUNDARY_CROSSED"
    LISTING_REMOVED = "LISTING_REMOVED"
    LISTING_RESERVED = "LISTING_RESERVED"
    LISTING_SOLD_CLAIMED = "LISTING_SOLD_CLAIMED"
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
    TAX_NEWLY_INVALID = "TAX_NEWLY_INVALID"
    EVIDENCE_NEWLY_INVALID = "EVIDENCE_NEWLY_INVALID"
    CONTRIBUTION_SCENARIO_CHANGED = "CONTRIBUTION_SCENARIO_CHANGED"
    DOCUMENTATION_RESOLVED = "DOCUMENTATION_RESOLVED"


#: Reasons that are material and invalidate a prior recommendation regardless of thresholds.
ALWAYS_MATERIAL: Final = frozenset(
    {
        MaterialityReason.PRICE_BECAME_UNKNOWN,
        MaterialityReason.PRICE_BECAME_KNOWN,
        MaterialityReason.ELIGIBILITY_CROSSED,
        MaterialityReason.RISK_BOUNDARY_CROSSED,
        MaterialityReason.LISTING_REMOVED,
        MaterialityReason.LISTING_RESERVED,
        MaterialityReason.LISTING_SOLD_CLAIMED,
        MaterialityReason.AVAILABILITY_CHANGED,
        MaterialityReason.TAX_NEWLY_INVALID,
        MaterialityReason.EVIDENCE_NEWLY_INVALID,
        MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED,
    }
)
_PRICE_REASONS: Final = frozenset({MaterialityReason.PRICE_DECREASE, MaterialityReason.PRICE_INCREASE})


class AlertState(BaseModel):
    """What an owner alert was (or would be) based on. Unknown stays ``None``/``unknown``."""

    model_config = _FROZEN

    price_eur: Decimal | None = Field(default=None, gt=0)
    eligibility: EligibilityState | None = None
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    availability: Availability = Availability.UNKNOWN
    tax_rules_valid: bool | None = None
    evidence_valid: bool | None = None
    documentation_complete: bool | None = None
    conservative_contribution_eur: Decimal | None = None
    base_contribution_eur: Decimal | None = None
    contribution_meets_threshold: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_float(cls, data: Any) -> Any:
        return _refuse_floats(data)


class MaterialityPolicy(BaseModel):
    model_config = _FROZEN

    version: str = MATERIALITY_POLICY_VERSION
    price_abs_eur: Decimal = Field(default=PROPOSED_PRICE_REALERT_ABS_EUR, gt=0)
    price_pct: Decimal = Field(default=PROPOSED_PRICE_REALERT_PCT, gt=0, lt=100)
    policy_approved: bool = False

    @model_validator(mode="before")
    @classmethod
    def _no_float(cls, data: Any) -> Any:
        return _refuse_floats(data)

    @classmethod
    def from_config(cls, config: BusinessConfig) -> MaterialityPolicy:
        return cls(
            price_abs_eur=config.price_realert_abs_eur,
            price_pct=config.price_realert_pct,
            policy_approved=config.price_realert_policy_approved,
        )


class MaterialityDecision(BaseModel):
    model_config = _FROZEN

    policy_version: str
    policy_approved: bool
    material: bool
    realert_allowed: bool
    invalidates_recommendation: bool
    reasons: tuple[MaterialityReason, ...]
    blockers: tuple[str, ...]
    price_change_eur: Decimal | None
    price_change_pct: Decimal | None
    explanations: tuple[str, ...]


def evaluate_materiality(
    previous: AlertState | None, current: AlertState, policy: MaterialityPolicy | None = None
) -> MaterialityDecision:
    """Decide whether a change since the last alert is material (see module docstring)."""
    policy = policy or MaterialityPolicy()
    reasons: list[MaterialityReason] = []
    notes: list[str] = []
    change_eur: Decimal | None = None
    change_pct: Decimal | None = None
    if previous is None:
        reasons.append(MaterialityReason.FIRST_ALERT)
        notes.append("no previous alert for this candidate")
    else:
        change_eur, change_pct = _price_changes(previous, current, policy, reasons, notes)
        _state_changes(previous, current, reasons, notes)
    blockers: list[str] = []
    price_only = bool(reasons) and set(reasons) <= _PRICE_REASONS
    if price_only and not policy.policy_approved:
        blockers.append("PRICE_REALERT_POLICY_UNAPPROVED")
    invalidates = any(r in ALWAYS_MATERIAL or r in _PRICE_REASONS for r in reasons)
    material = bool(reasons)
    return MaterialityDecision(
        policy_version=policy.version,
        policy_approved=policy.policy_approved,
        material=material,
        realert_allowed=material and not blockers,
        invalidates_recommendation=invalidates,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        price_change_eur=change_eur,
        price_change_pct=change_pct,
        explanations=tuple(notes),
    )


def _price_changes(
    prev: AlertState,
    cur: AlertState,
    policy: MaterialityPolicy,
    reasons: list[MaterialityReason],
    notes: list[str],
) -> tuple[Decimal | None, Decimal | None]:
    if prev.price_eur is not None and cur.price_eur is None:
        reasons.append(MaterialityReason.PRICE_BECAME_UNKNOWN)
        notes.append("price is no longer known")
        return None, None
    if prev.price_eur is None and cur.price_eur is not None:
        reasons.append(MaterialityReason.PRICE_BECAME_KNOWN)
        notes.append(f"price became known: EUR {cur.price_eur}")
        return None, None
    if prev.price_eur is None or cur.price_eur is None:
        return None, None
    delta = cur.price_eur - prev.price_eur
    pct = abs(delta) * _HUNDRED / prev.price_eur
    if delta and (abs(delta) >= policy.price_abs_eur or pct >= policy.price_pct):
        reasons.append(MaterialityReason.PRICE_DECREASE if delta < 0 else MaterialityReason.PRICE_INCREASE)
        state = "approved" if policy.policy_approved else "PROPOSED, not owner-approved"
        notes.append(
            f"price changed by EUR {delta} ({pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} %); "
            f"thresholds EUR {policy.price_abs_eur} or {policy.price_pct} % ({state})"
        )
    return delta, pct


def _state_changes(
    prev: AlertState, cur: AlertState, reasons: list[MaterialityReason], notes: list[str]
) -> None:
    if prev.eligibility != cur.eligibility:
        reasons.append(MaterialityReason.ELIGIBILITY_CROSSED)
        notes.append(f"eligibility {_v(prev.eligibility)} -> {_v(cur.eligibility)}")
    if prev.risk_level != cur.risk_level:
        reasons.append(MaterialityReason.RISK_BOUNDARY_CROSSED)
        notes.append(f"risk level {prev.risk_level.value} -> {cur.risk_level.value}")
    if prev.availability != cur.availability:
        specific = {
            Availability.REMOVED: MaterialityReason.LISTING_REMOVED,
            Availability.RESERVED: MaterialityReason.LISTING_RESERVED,
            Availability.SOLD_CLAIMED: MaterialityReason.LISTING_SOLD_CLAIMED,
        }.get(cur.availability, MaterialityReason.AVAILABILITY_CHANGED)
        reasons.append(specific)
        notes.append(f"availability {prev.availability.value} -> {cur.availability.value}")
    if prev.tax_rules_valid is True and cur.tax_rules_valid is not True:
        reasons.append(MaterialityReason.TAX_NEWLY_INVALID)
        notes.append("tax rule basis is no longer valid")
    if prev.evidence_valid is True and cur.evidence_valid is not True:
        reasons.append(MaterialityReason.EVIDENCE_NEWLY_INVALID)
        notes.append("supporting evidence is no longer valid")
    if prev.documentation_complete is not True and cur.documentation_complete is True:
        reasons.append(MaterialityReason.DOCUMENTATION_RESOLVED)
        notes.append("documentation questions resolved")
    scenario_note = _contribution_change(prev, cur)
    if scenario_note:
        reasons.append(MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED)
        notes.append(scenario_note)


def _contribution_change(prev: AlertState, cur: AlertState) -> str | None:
    for name in ("conservative_contribution_eur", "base_contribution_eur"):
        before: Decimal | None = getattr(prev, name)
        after: Decimal | None = getattr(cur, name)
        if (before is None) != (after is None):
            return f"{name.removesuffix('_eur')} became {'unknown' if after is None else 'known'}"
        if before is not None and after is not None and _sign(before) != _sign(after):
            return f"{name.removesuffix('_eur')} changed sign (EUR {before} -> EUR {after})"
    if prev.contribution_meets_threshold != cur.contribution_meets_threshold:
        return "whether the contribution meets the threshold changed"
    return None


# =============================================================================================
# Internal outbox event and MCP occurrence
# =============================================================================================

Priority = Literal["low", "normal", "high"]

_IMPORT_CATEGORIES: Final = frozenset(
    {
        CostCategory.IMPORT_DUTY.value,
        CostCategory.MOTOR_VEHICLE_TAX.value,
        CostCategory.IMPORT_VAT.value,
        CostCategory.OTHER_IMPORT_CHARGES.value,
        CostCategory.CUSTOMS_BROKER.value,
        CostCategory.HOMOLOGATION_REGISTRATION.value,
    }
)
_READINESS_SUMMARY: Final[dict[str, str]] = {
    "not_valued": "New research candidate; valuation not started.",
    "valuation_stale": "Research candidate; valuation is being recalculated.",
    "valuation_invalid": "Research candidate; valuation needs review.",
    "needs_comparables": "New research candidate; MK comparables are insufficient.",
    "needs_import_costs": "New research candidate; import costs need verification.",
    "needs_costs": "New research candidate; some costs are still unknown.",
    "incomplete": "New research candidate; valuation incomplete.",
    "estimated": "New research candidate with an estimated, unverified valuation.",
    "quote_supported": "New research candidate; estimate partly supported by quotes.",
}


def derive_readiness(
    valuation_state: ValuationState | None,
    *,
    unknown_cost_categories: Sequence[str] = (),
    comparable_status: str | None = None,
) -> str:
    """Short machine readiness label for the pending-review signal (``^[a-z][a-z0-9_]*$``)."""
    if valuation_state is None or valuation_state == ValuationState.NOT_STARTED:
        return "not_valued"
    if valuation_state == ValuationState.STALE:
        return "valuation_stale"
    if valuation_state == ValuationState.INVALID:
        return "valuation_invalid"
    if comparable_status in (None, "insufficient_comparables"):
        return "needs_comparables"
    unknown = {str(c) for c in unknown_cost_categories}
    if unknown & _IMPORT_CATEGORIES:
        return "needs_import_costs"
    if unknown:
        return "needs_costs"
    if valuation_state == ValuationState.INCOMPLETE:
        return "incomplete"
    return valuation_state.value  # estimated / quote_supported


class OutboxEventDraft(BaseModel):
    """One ``ops.outbox`` row to insert in the same transaction as the case change."""

    model_config = _FROZEN

    event_id: UUID
    event_type: Literal["review.pending"] = EVENT_TYPE
    event_version: str = PAYLOAD_SCHEMA_VERSION
    aggregate_type: Literal["review_case"] = "review_case"
    aggregate_id: UUID
    aggregate_version: int
    dedup_key: str
    payload: dict[str, Any]
    payload_hash: str
    payload_bytes: int
    is_fixture: bool
    initial_state: OutboxState
    blocker_code: str | None


def dashboard_case_url(dashboard_base_url: str, case_id: UUID) -> str:
    """``<base>/reviews/<case_id>``. The base must be https (http only for localhost), without
    credentials, query or fragment, so the link never embeds an access token."""
    parts = urlsplit(dashboard_base_url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValidationFailed("dashboard base URL must be an absolute http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValidationFailed("dashboard base URL must use https outside local development")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValidationFailed("dashboard base URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ValidationFailed("dashboard base URL must not carry a query or fragment")
    base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    url = f"{base}/reviews/{case_id}"
    if len(url) > 2048:
        raise ValidationFailed("dashboard URL too long")
    return url


def build_review_pending_event(
    case: ReviewCaseSnapshot,
    *,
    dashboard_base_url: str,
    event_id: UUID,
    occurred_at: datetime,
    readiness: str,
    priority: Priority = "normal",
    summary: str | None = None,
    queue: str | None = None,
) -> OutboxEventDraft:
    """Internal ``review.pending`` outbox payload (spec 22) for the case's current version.

    Only a ``pending`` case version emits the signal (claimed, decided and superseded cases do not).

    ``readiness`` comes from ``derive_readiness``. ``summary`` defaults to a fixed template per
    readiness; a caller summary is sanitised and bounded. Fixture cases produce a draft that is
    ``blocked`` (``FIXTURE_EVENT``) and can never be routed externally.
    """
    if case.state != ReviewState.PENDING:
        raise ValidationFailed(
            "only a pending case version emits review.pending", details={"state": case.state.value}
        )
    if not _READINESS_RE.fullmatch(readiness):
        raise ValidationFailed("readiness must be a short lowercase label")
    if queue is not None and not _QUEUE_RE.fullmatch(queue):
        raise ValidationFailed("queue must be a lowercase slug")
    if priority not in ("low", "normal", "high"):
        raise ValidationFailed("priority must be low, normal or high")
    occurred = _aware(occurred_at)
    text = _READINESS_SUMMARY.get(readiness, "New research candidate awaiting review.")
    if summary is not None:
        text = sanitize_seller_text(summary, max_length=300) or text
    if case.is_fixture:
        text = f"{FIXTURE_SUMMARY_PREFIX} {text}"
    dedup = f"review.pending:{case.case_id}:{case.row_version}"
    payload: dict[str, Any] = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "event_id": str(event_id),
        "type": EVENT_TYPE,
        "occurred_at": _rfc3339(occurred),
        "case_id": str(case.case_id),
        "case_version": case.row_version,
        "listing_id": str(case.listing_id),
        "listing_revision": case.listing_revision,
        "priority": priority,
        "dashboard_url": dashboard_case_url(dashboard_base_url, case.case_id),
        "summary": text,
        "deduplication_key": dedup,
        "readiness": readiness,
        "profile": case.profile_key.value,
    }
    if queue is not None:
        payload["queue"] = queue
    if case.is_fixture:
        # Carried in the payload itself so a stored row can never lose its fixture status.
        payload["fixture"] = True
    size = guard_payload(payload)
    return OutboxEventDraft(
        event_id=event_id,
        aggregate_id=case.case_id,
        aggregate_version=case.row_version,
        dedup_key=dedup,
        payload=payload,
        payload_hash=sha256_json(payload),
        payload_bytes=size,
        is_fixture=case.is_fixture,
        initial_state=OutboxState.BLOCKED if case.is_fixture else OutboxState.PENDING,
        blocker_code=FIXTURE_BLOCKER if case.is_fixture else None,
    )


class _ReviewPendingView(BaseModel):
    """Strict read view used when mapping a stored payload to an MCP occurrence."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    event_id: UUID
    type: Literal["review.pending"]
    occurred_at: datetime
    case_id: UUID
    case_version: int = Field(ge=1, strict=True)
    listing_id: UUID
    listing_revision: int = Field(ge=1, strict=True)
    dashboard_url: str = Field(max_length=2048)
    readiness: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def to_mcp_occurrence(
    event: OutboxEventDraft | Mapping[str, Any], *, is_fixture: bool = False
) -> dict[str, Any]:
    """Native MCP Events occurrence for one outbox event; fixture events are refused.

    Fixture status is the union of the explicit flag, the draft's flag, the payload's ``fixture``
    marker and the ``[SYNTHETIC FIXTURE]`` summary prefix, so a stored payload mapped without
    its row flag still cannot leave the system (spec 18 "Synthetic arithmetic test").
    """
    if isinstance(event, OutboxEventDraft):
        is_fixture = is_fixture or event.is_fixture
        raw: Mapping[str, Any] = event.payload
    else:
        raw = event
    marker = raw.get("fixture")
    summary = raw.get("summary")
    if marker is not None and marker is not False:  # any present, non-false marker counts
        is_fixture = True
    if isinstance(summary, str) and summary.lstrip().upper().startswith(FIXTURE_SUMMARY_PREFIX):
        is_fixture = True
    assert_external_routing_allowed(is_fixture=is_fixture)
    try:
        view = _ReviewPendingView.model_validate(dict(raw))
    except ValueError as exc:
        raise ValidationFailed("invalid review.pending payload") from exc
    occurrence: dict[str, Any] = {
        "eventId": str(view.event_id),
        "name": MCP_EVENT_NAME,
        "timestamp": _rfc3339(view.occurred_at),
        "data": {
            "case_id": str(view.case_id),
            "case_version": view.case_version,
            "listing_id": str(view.listing_id),
            "listing_revision": view.listing_revision,
            "readiness": view.readiness,
            "dashboard_url": view.dashboard_url,
        },
        "cursor": None,
    }
    guard_payload(occurrence)
    return occurrence


def external_routing_blocker(*, is_fixture: bool) -> str | None:
    """Blocker code for external delivery, or ``None``. Fixture events are never routed out."""
    return FIXTURE_BLOCKER if is_fixture else None


def assert_external_routing_allowed(*, is_fixture: bool) -> None:
    if external_routing_blocker(is_fixture=is_fixture) is not None:
        raise Forbidden("Fixture/synthetic events are never routed to external destinations")


# =============================================================================================
# Payload guard
# =============================================================================================

_EMAIL_RE: Final = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
)
# Up to three separator characters between digits, including parentheses, so that common DE/IT/CH
# forms such as "(0171) 1234567", "+49 (0)171 1234567" and "(+39) 333 1234567" are caught.
# Separators and digits are disjoint character sets, so matching stays linear (no ReDoS).
_PHONE_SEP: Final = r"[ \t\u00a0./()\-]{0,3}"  # re expands \u00a0 (NBSP)
_PHONE_INTL_RE: Final = re.compile(
    r"(?<![\w.:/=-])(?:\+|00)[1-9]\d{0,3}(?:" + _PHONE_SEP + r"\d){6,13}(?!\w)"
)
_PHONE_LOCAL_RE: Final = re.compile(r"(?<![\w.:/+=-])0[1-9]\d(?:" + _PHONE_SEP + r"\d){6,9}(?!\w)")
# Italian mobile numbers are written without a trunk prefix: 3xx followed by 7 digits (10 total).
_PHONE_IT_MOBILE_RE: Final = re.compile(r"(?<![\w.:/+=-])3\d{2}(?:" + _PHONE_SEP + r"\d){7}(?!\w)")
_PHONE_PATTERNS: Final = (_PHONE_INTL_RE, _PHONE_LOCAL_RE, _PHONE_IT_MOBILE_RE)
_UUID_ANY_RE: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TIMESTAMP_RE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})?)?"
)
_SECRET_PATTERNS: Final = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:whsec|sk|pk|rk|ghp|gho|github_pat|xox[abposr]|sbp|sb_secret)[_-][A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*\S+"),
)
_TOKENISH_RE: Final = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_RAW_CONTENT_RE: Final = re.compile(r"<\s*(?:html|body|script|div|table|iframe|style)\b", re.IGNORECASE)
_SECRET_QUERY_RE: Final = re.compile(
    r"^(?:[\w-]*[_-])?(token|key|secret|signature|sig|code|password|passwd|pwd|auth|apikey|jwt|session)$",
    re.IGNORECASE,
)
_FORBIDDEN_KEY_PARTS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "phone",
    "telephone",
    "email",
    "iban",
    "cardnumber",
    "rawlisting",
    "rawhtml",
)
_FORBIDDEN_KEYS: Final = frozenset({"html", "description", "contact", "sellercontact", "mobile", "tel"})
_URL_IN_TEXT_RE: Final = re.compile(r"https?://[^\s<>\"']{1,2048}", re.IGNORECASE)


def guard_payload(payload: Any, *, max_bytes: int = MAX_PAYLOAD_BYTES) -> int:
    """Validate an outbound notification payload; return its UTF-8 JSON size in bytes.

    Raises ``ValidationFailed`` (with a list of problem codes, never the offending values) for
    non-JSON data, oversize payloads, forbidden keys, contact data, secrets/tokens, raw HTML or
    unsafe URLs.
    """
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("notification payload is not plain JSON") from exc
    size = len(encoded.encode("utf-8"))
    problems: list[str] = []
    if size > max_bytes:
        problems.append("PAYLOAD_TOO_LARGE")
    _walk(payload, "$", problems)
    if problems:
        raise ValidationFailed(
            "notification payload rejected by the safety guard", details={"problems": sorted(set(problems))}
        )
    return size


def _walk(value: Any, path: str, problems: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                problems.append(f"NON_STRING_KEY:{path}")
                continue
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _FORBIDDEN_KEYS or any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                problems.append(f"FORBIDDEN_KEY:{path}.{key}")
            _walk(item, f"{path}.{key}", problems)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", problems)
    elif isinstance(value, str):
        problems.extend(f"{code}:{path}" for code in text_problems(value))


def text_problems(text: str) -> list[str]:
    """Problem codes for one outbound string (contact data, secrets, raw content, unsafe URLs)."""
    problems: list[str] = []
    if len(text) > MAX_PAYLOAD_STRING:
        problems.append("RAW_CONTENT_TOO_LONG")
    if _RAW_CONTENT_RE.search(text):
        problems.append("RAW_HTML")
    if _EMAIL_RE.search(text):
        problems.append("EMAIL_ADDRESS")
    for url in _URL_IN_TEXT_RE.findall(text):
        problems.extend(_url_problems(url))
    scrubbed = _TIMESTAMP_RE.sub(" ", _UUID_ANY_RE.sub(" ", text))
    if any(regex.search(scrubbed) for regex in _PHONE_PATTERNS):
        problems.append("PHONE_NUMBER")
    if any(p.search(text) for p in _SECRET_PATTERNS):
        problems.append("SECRET_TOKEN")
    for candidate in _TOKENISH_RE.findall(_UUID_ANY_RE.sub(" ", text)):
        if (
            any(c.isupper() for c in candidate)
            and any(c.islower() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            problems.append("SECRET_TOKEN")
            break
    return problems


def _url_problems(url: str) -> list[str]:
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        has_userinfo = bool(parts.username or parts.password)
    except ValueError:
        return ["INVALID_URL"]
    problems: list[str] = []
    if has_userinfo:
        problems.append("URL_CREDENTIALS")
    if any(_SECRET_QUERY_RE.fullmatch(name) for name, _ in query):
        problems.append("URL_TOKEN_PARAMETER")
    return problems


# =============================================================================================
# Safe owner-facing messages
# =============================================================================================


class EvidenceFlags(BaseModel):
    """Evidence that explicitly supports otherwise forbidden wording (default: none)."""

    model_config = _FROZEN

    accident_free_verified: bool = False  # owner-verified inspection/document on file
    seller_confirmation_evidence: bool = False  # written seller confirmation stored as evidence
    business_tax_modelled: bool = False  # business tax modelled, so "net profit" is meaningful


#: phrase (normalised) -> enabling flag, or None when never supportable.
FORBIDDEN_PHRASES: Final[dict[str, str | None]] = {
    "guaranteed profit": None,
    "guaranteed": None,
    "guarantee": None,
    "risk free": None,
    "verified accident free": "accident_free_verified",
    "seller confirmed": "seller_confirmation_evidence",
    "net profit": "business_tax_modelled",
}


def _normalise_wording(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = re.sub("[\\u2010-\\u2015\\u2212_-]", " ", folded)  # hyphen/dash variants
    return re.sub(r"\s+", " ", folded)


def forbidden_phrases_in(text: str, evidence: EvidenceFlags | None = None) -> list[str]:
    """Forbidden phrases present in ``text`` that the evidence flags do not support."""
    evidence = evidence or EvidenceFlags()
    normalised = f" {_normalise_wording(text)} "
    found: list[str] = []
    for phrase, flag in FORBIDDEN_PHRASES.items():
        present = re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalised)
        if present and (flag is None or not getattr(evidence, flag)):
            found.append(phrase)
    return found


def check_owner_wording(text: str, evidence: EvidenceFlags | None = None) -> None:
    found = forbidden_phrases_in(text, evidence)
    if found:
        raise ValidationFailed("owner message uses unsupported claims", details={"phrases": sorted(found)})


_MARKUP_CHARS_RE: Final = re.compile(r"[*_~`\[\]()<>|#!@\\{}^]")
_BARE_LINK_RE: Final = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def sanitize_seller_text(text: str | None, *, max_length: int = 120) -> str | None:
    """Make untrusted seller-provided text safe to embed in an owner message.

    Removes control/format characters, links, e-mail addresses, phone numbers and markdown/
    mention syntax; neutralises forbidden claims; collapses whitespace; bounds the length.
    Returns ``None`` for empty input.
    """
    if text is None:
        return None
    if max_length < 4:
        raise ValidationFailed("max_length too small")
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = "".join(
        " " if unicodedata.category(c) in {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"} else c for c in cleaned
    )
    cleaned = _BARE_LINK_RE.sub(" [link removed] ", cleaned)
    cleaned = _EMAIL_RE.sub(" [contact removed] ", cleaned)
    scrub_target = cleaned
    for regex in _PHONE_PATTERNS:
        scrub_target = regex.sub(" [contact removed] ", scrub_target)
    cleaned = _MARKUP_CHARS_RE.sub(" ", scrub_target)
    for phrase in FORBIDDEN_PHRASES:
        pattern = (
            r"(?<![A-Za-z0-9])" + r"[\s_-]+".join(re.escape(w) for w in phrase.split()) + r"(?![A-Za-z0-9])"
        )
        cleaned = re.sub(pattern, " claim omitted ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 3].rstrip() + "..."
    return cleaned


class ComparableSummary(BaseModel):
    """Asking-price comparable evidence for the message (never sale prices)."""

    model_config = _FROZEN

    status: Literal["adequate", "small_sample", "insufficient_comparables"]
    asking_count: int = Field(ge=0)
    median_asking: Money | None = None
    min_asking: Money | None = None
    max_asking: Money | None = None
    band_fit: Literal["below", "within", "above", "unknown"] = "unknown"


class OwnerAlertContent(BaseModel):
    model_config = _FROZEN

    make: str | None = None
    model: str | None = None
    generation: str | None = None
    trim: str | None = None
    year: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2})?$")
    fuel: Fuel = Fuel.UNKNOWN
    gearbox: Gearbox = Gearbox.UNKNOWN
    drive: Drive = Drive.UNKNOWN
    power_kw: int | None = Field(default=None, ge=1, le=2000)
    displacement_cm3: int | None = Field(default=None, ge=50, le=10000)
    asking_price: Money | None
    asking_price_eur: Money | None = None
    mileage_km: Decimal | None = Field(default=None, ge=0)
    mileage_claim: OdometerClaim = OdometerClaim.UNKNOWN
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    last_checked_at: datetime | None = None
    comparables: ComparableSummary | None = None
    valuation_complete: bool = False
    conservative_contribution: Money | None = None
    base_contribution: Money | None = None
    top_risks: tuple[str, ...] = Field(default=(), max_length=10)
    unresolved_costs: tuple[str, ...] = Field(default=(), max_length=20)
    source_url: str | None = Field(default=None, max_length=2048)
    dashboard_url: str = Field(max_length=2048)
    research_candidate: bool = True
    evidence: EvidenceFlags = EvidenceFlags()
    is_fixture: bool = False

    @field_validator("last_checked_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("source_url", "dashboard_url")
    @classmethod
    def _http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("links must be absolute http(s) URLs")
        if parts.username or parts.password or any(c.isspace() for c in value):
            raise ValueError("links must not contain credentials or whitespace")
        return value


MAX_OWNER_MESSAGE_CHARS: Final = 3500


def render_owner_message(content: OwnerAlertContent, *, now: datetime) -> str:
    """Plain-text owner alert (spec 22). Raises ``ValidationFailed`` if the guard rejects it.

    - A candidate whose valuation is incomplete is always labelled a research candidate (spec 14:
      it cannot be promoted as a quantified opportunity), whatever ``research_candidate`` says.
    - The source and dashboard links are never truncated: the body is shortened instead, and an
      over-long source link is replaced by a pointer to the dashboard.
    - The public source link is checked with URL rules (credentials, token-like query names or
      values, known secret formats) rather than the generic long-token heuristic, which would
      reject ordinary mixed-case listing slugs.
    """
    now = _aware(now)
    lines: list[str] = []
    if content.is_fixture:
        lines.append("[SYNTHETIC FIXTURE - not a real offer; never sent externally]")
    if content.research_candidate or not content.valuation_complete:
        lines.append("Research candidate - estimated figures, not a verified opportunity")
    else:
        lines.append("Reviewed candidate - estimated figures; due diligence still required")
    lines.append(f"Vehicle: {_vehicle_line(content)}")
    lines.append(f"Asking price: {_price_line(content)}")
    lines.append(f"Mileage: {_mileage_line(content)}")
    lines.append(f"Seller country: {content.country or 'unknown'}")
    lines.append(f"Freshness: {_freshness_line(content.last_checked_at, now)}")
    lines.append(f"MK comparable asking evidence: {_comparables_line(content.comparables)}")
    lines.append(_contribution_line(content))
    risks = [r for r in (sanitize_seller_text(r, max_length=160) for r in content.top_risks[:5]) if r]
    lines.append("Top risks:" if risks else "Top risks: none recorded yet (not an inspection result)")
    lines.extend(f"- {risk}" for risk in risks)
    costs = [c for c in (sanitize_seller_text(c, max_length=120) for c in content.unresolved_costs[:10]) if c]
    lines.append("Unresolved costs (unknown, not zero):" if costs else "Unresolved costs: none recorded")
    lines.extend(f"- {cost}" for cost in costs)
    dashboard_line = f"Dashboard (sign-in required): {content.dashboard_url}"
    links = [dashboard_line]
    source = content.source_url
    if source:
        source_line = f"Source: {source}"
        if len(source_line) + len(dashboard_line) + 2 > MAX_OWNER_MESSAGE_CHARS // 2:
            source = None
            source_line = "Source: link too long for this message; open it from the dashboard"
        links.insert(0, source_line)
    link_text = "\n".join(links)
    body = "\n".join(lines)
    budget = MAX_OWNER_MESSAGE_CHARS - len(link_text) - 1
    if len(body) > budget:
        body = body[: budget - 3].rstrip() + "..."
    text = f"{body}\n{link_text}"
    check_owner_wording(text, content.evidence)
    problems = text_problems(text.replace(source, " ") if source else text)
    if source:
        problems.extend(_link_problems(source))
    if problems:
        raise ValidationFailed(
            "owner message rejected by the safety guard", details={"problems": sorted(set(problems))}
        )
    return text


def _link_problems(url: str) -> list[str]:
    """Problems of a public link: URL rules, known secret formats and token-like query values."""
    problems = _url_problems(url)
    if any(p.search(url) for p in _SECRET_PATTERNS):
        problems.append("SECRET_TOKEN")
    if _RAW_CONTENT_RE.search(url):
        problems.append("RAW_HTML")
    try:
        values = [value for _, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    except ValueError:
        return [*problems, "INVALID_URL"]
    if any("SECRET_TOKEN" in text_problems(value) for value in values):
        problems.append("SECRET_TOKEN")
    if _EMAIL_RE.search(url):
        problems.append("EMAIL_ADDRESS")
    return problems


def _vehicle_line(c: OwnerAlertContent) -> str:
    name = " ".join(
        p
        for p in (sanitize_seller_text(c.make, max_length=40), sanitize_seller_text(c.model, max_length=60))
        if p
    )
    parts = [name or "unknown make/model"]
    generation = sanitize_seller_text(c.generation, max_length=40)
    if generation:
        parts[0] += f" {generation}"
    trim = sanitize_seller_text(c.trim, max_length=60)
    if trim:
        parts.append(trim)
    parts.append(f"first reg. {c.year}" if c.year else "first registration unknown")
    parts.append(c.fuel.value if c.fuel != Fuel.UNKNOWN else "fuel unknown")
    parts.append(c.gearbox.value.replace("_", "-") if c.gearbox != Gearbox.UNKNOWN else "gearbox unknown")
    parts.append(c.drive.value.upper() if c.drive != Drive.UNKNOWN else "drive unknown")
    if c.power_kw is not None:
        parts.append(f"{c.power_kw} kW")
    if c.displacement_cm3 is not None:
        parts.append(f"{c.displacement_cm3} cm3")
    return ", ".join(parts)


def _price_line(c: OwnerAlertContent) -> str:
    if c.asking_price is None:
        return "unknown"
    text = f"{c.asking_price.display()} (seller asking price; payable amount not confirmed)"
    if c.asking_price.currency != "EUR" and c.asking_price_eur is not None:
        text += f"; approx. {c.asking_price_eur.display()} at the recorded reference rate"
    return text


def _mileage_line(c: OwnerAlertContent) -> str:
    if c.mileage_km is None:
        return "unknown"
    rounded = int(c.mileage_km.to_integral_value(rounding=ROUND_HALF_UP))
    approx = "" if c.mileage_km == rounded else "approx. "
    claim = c.mileage_claim.value.replace("_", " ")
    return f"{approx}{rounded:,} km ({claim})"


def _freshness_line(checked: datetime | None, now: datetime) -> str:
    if checked is None:
        return "no successful detail check recorded"
    local = checked.astimezone(_owner_zone())
    age = now - checked
    if age < timedelta(0):
        return f"checked {local:%Y-%m-%d %H:%M} {OWNER_TIMEZONE} (timestamp after now; verify)"
    hours = int(age.total_seconds() // 3600)
    ago = f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"
    return f"last checked {ago} ({local:%Y-%m-%d %H:%M} {OWNER_TIMEZONE})"


def _comparables_line(s: ComparableSummary | None) -> str:
    if s is None or s.status == "insufficient_comparables" or s.asking_count == 0 or s.median_asking is None:
        return "insufficient MK comparables; research needed"
    text = f"{s.asking_count} asking price(s), median {s.median_asking.display()}"
    if s.min_asking is not None and s.max_asking is not None:
        text += f", range {s.min_asking.display()} to {s.max_asking.display()}"
    if s.status == "small_sample":
        text += " (small sample)"
    return f"{text}; asking prices, not sale prices; EUR 8,000-10,000 band fit: {s.band_fit}"


def _contribution_line(c: OwnerAlertContent) -> str:
    if c.valuation_complete and c.conservative_contribution is not None and c.base_contribution is not None:
        return (
            "Estimated contribution before business tax (estimated scenarios, not a forecast): "
            f"conservative {c.conservative_contribution.display()}; base {c.base_contribution.display()}"
        )
    return "Estimated contribution: not shown - valuation incomplete (unknown costs remain unknown)"


# =============================================================================================
# Quiet hours
# =============================================================================================

Urgency = Literal["normal", "urgent"]


class QuietHours(BaseModel):
    """Owner quiet hours in local time. PROPOSED values (``approved=False``) are never applied."""

    model_config = _FROZEN

    start: time
    end: time
    timezone: str = OWNER_TIMEZONE
    approved: bool = False
    urgent_bypass: bool = False

    @field_validator("start", "end")
    @classmethod
    def _naive_local(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("quiet-hour bounds are local wall-clock times without a zone")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return value


class QuietHoursDecision(BaseModel):
    model_config = _FROZEN

    deliver_now: bool
    deliver_at: datetime | None
    applied: bool
    reason: str


def evaluate_quiet_hours(
    now: datetime, preferences: QuietHours | None, urgency: Urgency = "normal"
) -> QuietHoursDecision:
    """Whether to deliver now or defer to the end of the owner's (approved) quiet window."""
    now = _aware(now)
    if preferences is None:
        return QuietHoursDecision(
            deliver_now=True, deliver_at=None, applied=False, reason="no quiet hours configured"
        )
    if not preferences.approved:
        return QuietHoursDecision(
            deliver_now=True,
            deliver_at=None,
            applied=False,
            reason="quiet hours are a proposal awaiting the owner's choice; not applied",
        )
    if preferences.start == preferences.end:
        return QuietHoursDecision(
            deliver_now=True, deliver_at=None, applied=True, reason="empty quiet window"
        )
    zone = ZoneInfo(preferences.timezone)
    local = now.astimezone(zone)
    current = local.time().replace(tzinfo=None)
    start, end = preferences.start, preferences.end
    wraps = start > end
    inside = (current >= start or current < end) if wraps else (start <= current < end)
    if not inside:
        return QuietHoursDecision(
            deliver_now=True, deliver_at=None, applied=True, reason="outside quiet hours"
        )
    if urgency == "urgent" and preferences.urgent_bypass:
        return QuietHoursDecision(
            deliver_now=True, deliver_at=None, applied=True, reason="urgent event; owner allows bypass"
        )
    end_date = local.date() + timedelta(days=1) if wraps and current >= start else local.date()
    deliver_at = datetime.combine(end_date, end, tzinfo=zone).astimezone(UTC)
    deliver_at = max(deliver_at, now)  # DST shifts of a wall-clock end never defer into the past
    return QuietHoursDecision(
        deliver_now=False,
        deliver_at=deliver_at,
        applied=True,
        reason=f"inside quiet hours {start:%H:%M}-{end:%H:%M} {preferences.timezone}",
    )


# =============================================================================================
# Seller-reply owner alerts (spec 37.7)
# =============================================================================================

#: Internal outbox event type of an owner alert raised by a processed seller reply. Routed by
#: the dispatcher through the ``owner_alert`` category (never through native MCP Events).
SELLER_REPLY_OWNER_ALERT_EVENT_TYPE: Final = "seller_reply.owner_alert"
SellerReplyAlertKind = Literal["decision_needed", "opportunity_supported"]
SELLER_REPLY_ALERT_KINDS: Final[tuple[SellerReplyAlertKind, ...]] = (
    "decision_needed",
    "opportunity_supported",
)
#: Fixed status vocabulary of the alert (no seller text, amounts or contact data ever).
SELLER_REPLY_ALERT_STATUS: Final[dict[str, str]] = {
    "decision_needed": "seller reply: owner decision needed",
    "opportunity_supported": "seller reply: evidence supports a researched opportunity",
}
#: Human wording of the escalation and materiality reason codes (owner-facing text).
SELLER_REPLY_REASON_LABELS: Final[dict[str, str]] = {
    "payment_request": "payment request",
    "reservation_request": "reservation request",
    "identity_document_request": "request for identity documents",
    "appointment_request": "appointment request",
    "commitment_request": "request for a commitment",
    "price_acceptance_request": "request to accept a quoted price",
    "sensitive_attachment_withheld": "sensitive attachment withheld",
    "contradictory_reply": "contradictory availability statements",
    **{reason.value.lower(): reason.value.lower().replace("_", " ") for reason in MaterialityReason},
}
_ALERT_REASON_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_SELLER_REPLY_ALERT_REASONS: Final = 12


class SellerReplyOwnerAlertDraft(BaseModel):
    """One ``ops.outbox`` row for an owner alert derived from a processed seller reply.

    Only ids, a fixed-vocabulary status, typed reason codes and the authenticated dashboard link
    of the reply: never the reply body, an address, a name, an amount or a credential. It is a
    signal that a consequential decision (or a supported researched opportunity) needs the
    owner's attention; it grants nothing and never accepts a seller's request.
    """

    model_config = _FROZEN

    event_id: UUID
    event_type: Literal["seller_reply.owner_alert"] = SELLER_REPLY_OWNER_ALERT_EVENT_TYPE
    event_version: int = 1
    aggregate_type: Literal["seller_inquiry"] = "seller_inquiry"
    aggregate_id: UUID
    kind: SellerReplyAlertKind
    dedup_key: str
    payload: dict[str, Any]
    payload_hash: str
    payload_bytes: int
    is_fixture: bool
    priority: Priority


def seller_reply_alert_dedup_key(
    kind: SellerReplyAlertKind,
    *,
    inquiry_id: UUID,
    reasons: Sequence[str] = (),
    valuation_id: UUID | None = None,
) -> str:
    """Business dedup key: one decision alert per inquiry and reason set (a seller repeating the
    same request is not re-alerted); one opportunity alert per inquiry and valuation."""
    if kind == "decision_needed":
        return f"{SELLER_REPLY_OWNER_ALERT_EVENT_TYPE}:decision:{inquiry_id}:{'+'.join(sorted(set(reasons)))}"
    if valuation_id is None:
        raise ValidationFailed("an opportunity alert names the valuation it is based on")
    return f"{SELLER_REPLY_OWNER_ALERT_EVENT_TYPE}:opportunity:{inquiry_id}:{valuation_id}"


def build_seller_reply_owner_alert(
    *,
    event_id: UUID,
    kind: SellerReplyAlertKind,
    inquiry_id: UUID,
    reply_id: UUID,
    listing_id: UUID,
    dashboard_url: str,
    occurred_at: datetime,
    reasons: Sequence[str],
    valuation_id: UUID | None = None,
    is_fixture: bool = False,
) -> SellerReplyOwnerAlertDraft:
    """The minimal owner-alert payload (see `SellerReplyOwnerAlertDraft`).

    ``reasons`` are typed codes (``domain.replies.EscalationReason`` values for a decision,
    lower-case ``MaterialityReason`` values for an opportunity). Fixture lineage is stored but
    never routed (``fixture: true``; the outbox keeps it ``blocked``).
    """
    if kind not in SELLER_REPLY_ALERT_KINDS:
        raise ValidationFailed("unknown seller-reply alert kind")
    codes = list(dict.fromkeys(str(r) for r in reasons))
    if not codes or len(codes) > MAX_SELLER_REPLY_ALERT_REASONS:
        raise ValidationFailed("a seller-reply alert names 1-12 reason codes")
    if any(not _ALERT_REASON_RE.fullmatch(code) for code in codes):
        raise ValidationFailed("seller-reply alert reasons must be lower-case codes")
    if kind == "opportunity_supported" and valuation_id is None:
        raise ValidationFailed("an opportunity alert names the valuation it is based on")
    problem = _link_problems(dashboard_url)
    if problem:
        raise ValidationFailed(
            "dashboard URL is not a safe authenticated link", details={"problems": problem}
        )
    dedup = seller_reply_alert_dedup_key(
        kind, inquiry_id=inquiry_id, reasons=codes, valuation_id=valuation_id
    )
    priority: Priority = "high" if kind == "decision_needed" else "normal"
    payload: dict[str, Any] = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "event_id": str(event_id),
        "type": SELLER_REPLY_OWNER_ALERT_EVENT_TYPE,
        "occurred_at": _rfc3339(_aware(occurred_at)),
        "kind": kind,
        "reasons": codes,
        "inquiry_id": str(inquiry_id),
        "reply_id": str(reply_id),
        "listing_id": str(listing_id),
        "dashboard_url": dashboard_url,
        "status": SELLER_REPLY_ALERT_STATUS[kind],
        "priority": priority,
        "deduplication_key": dedup,
    }
    if valuation_id is not None:
        payload["valuation_id"] = str(valuation_id)
    if is_fixture:
        payload["fixture"] = True
    size = guard_payload(payload)
    return SellerReplyOwnerAlertDraft(
        event_id=event_id,
        aggregate_id=inquiry_id,
        kind=kind,
        dedup_key=dedup,
        payload=payload,
        payload_hash=sha256_json(payload),
        payload_bytes=size,
        is_fixture=is_fixture,
        priority=priority,
    )


def render_seller_reply_alert_text(kind: str, reasons: Sequence[str]) -> str:
    """Owner-facing plain wording of a seller-reply alert (no seller text, no amounts).

    A decision alert names the consequential requests (payment, reservation, identity documents,
    appointment, commitment, price acceptance) that only the owner may decide; nothing has been
    accepted or answered. An opportunity alert says the recalculated evidence supports a
    researched opportunity: estimated, not a binding agreement or a completed purchase.
    """
    labels = [SELLER_REPLY_REASON_LABELS.get(str(r), str(r).replace("_", " ")) for r in reasons][
        :MAX_SELLER_REPLY_ALERT_REASONS
    ]
    if kind == "decision_needed":
        text = (
            "Seller reply needs your decision: "
            + ", ".join(labels)
            + ". Nothing was accepted or answered; no reply is sent automatically."
        )
    elif kind == "opportunity_supported":
        text = (
            "Seller reply evidence supports a researched opportunity (estimated; not a binding"
            " agreement or a completed purchase). Changes: " + ", ".join(labels) + "."
        )
    else:
        raise ValidationFailed("unknown seller-reply alert kind")
    check_owner_wording(text)
    return text


# =============================================================================================
# Helpers
# =============================================================================================


def _owner_zone() -> ZoneInfo:
    return ZoneInfo(OWNER_TIMEZONE)


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _rfc3339(value: datetime) -> str:
    utc = ensure_utc(value)
    spec = "milliseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=spec).replace("+00:00", "Z")


def _sign(value: Decimal) -> int:
    """Three-way sign, so break-even (0) to loss is a sign change just like profit to loss."""
    return (value > 0) - (value < 0)


def _v(value: StrEnum | None) -> str:
    return "unknown" if value is None else value.value


def _refuse_floats(data: Any) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, float):
                raise ValueError(f"{key}: binary float is not allowed; use Decimal")
    return data


__all__ = [
    "ALWAYS_MATERIAL",
    "EVENT_TYPE",
    "FIXTURE_BLOCKER",
    "FIXTURE_SUMMARY_PREFIX",
    "FORBIDDEN_PHRASES",
    "MATERIALITY_POLICY_VERSION",
    "MAX_PAYLOAD_BYTES",
    "MAX_SELLER_REPLY_ALERT_REASONS",
    "MCP_EVENT_NAME",
    "SELLER_REPLY_ALERT_KINDS",
    "SELLER_REPLY_ALERT_STATUS",
    "SELLER_REPLY_OWNER_ALERT_EVENT_TYPE",
    "SELLER_REPLY_REASON_LABELS",
    "AlertState",
    "ComparableSummary",
    "EvidenceFlags",
    "MaterialityDecision",
    "MaterialityPolicy",
    "MaterialityReason",
    "OutboxEventDraft",
    "OwnerAlertContent",
    "ProfileKey",
    "QuietHours",
    "QuietHoursDecision",
    "RiskLevel",
    "SellerReplyAlertKind",
    "SellerReplyOwnerAlertDraft",
    "assert_external_routing_allowed",
    "build_review_pending_event",
    "build_seller_reply_owner_alert",
    "check_owner_wording",
    "dashboard_case_url",
    "derive_readiness",
    "evaluate_materiality",
    "evaluate_quiet_hours",
    "external_routing_blocker",
    "forbidden_phrases_in",
    "guard_payload",
    "render_owner_message",
    "render_seller_reply_alert_text",
    "sanitize_seller_text",
    "seller_reply_alert_dedup_key",
    "text_problems",
    "to_mcp_occurrence",
]
