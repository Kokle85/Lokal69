"""Bounded automatic seller inquiries (spec 37.1, 37.2, 37.5). Pure domain logic, no I/O.

Contents:

- ``SellerInquiryAuthorization``: the versioned application audit record of Vasko's bounded
  STANDING authorization (``config/seller_inquiry_authorization.yaml``). One initial inquiry per
  actual vehicle/seller pair asking only availability, vehicle documents and the lowest/final
  price. ``approval_mode`` is fixed to ``no_message_approval``.
- ``InquiryIdentity``: workspace + canonical vehicle identity (confirmed vehicle cluster, else the
  listing incarnation) + verified seller identity + purpose. Never a source listing id alone, so
  one car advertised on three sites by one seller is one inquiry whatever relay address is shown.
  ``evaluate_duplicate_contact`` applies the one-inquiry rule (a price change, relisting, profile
  switch, sender change or retry never resets it) and suppresses the additional send while a
  plausible cross-site duplicate is unresolved; ``reconcile_identity_merge`` keeps exactly one
  inquiry when identities merge later.
- ``evaluate_inquiry_readiness``: ``inquiry_ready`` is separate from investment readiness and
  implements the six checks of spec 37.2. Missing CoC/origin/CO2/registration copies/last price are
  the reason to ask, never blockers; an unapproved tax rule or the PROPOSED EUR 1,500 threshold
  never blocks (the candidate is flagged ``economics_incomplete``). No approval wait exists.
- The inquiry state machine of spec 37.5 with guarded edges, and the immutable
  scope/template/body-hash plus sender/recipient binding once reserved.
- Rolling 24 h / 15 day rate caps (ceilings, not targets; transactional enforcement is the
  database layer's), seller-level cooldown, suppression matching and ``dispatch_preflight``
  (proceed / cancel_stale / hold) immediately before transmission.
- The uncertain-send policy: ``should_retry`` only after a proven pre-submission failure with no
  possibly running prior attempt, or with provider-documented idempotency; never another account.
  An empty Sent Items search is not proof of non-submission; uncertain sends keep their
  reservation and quota debit.
- ``requires_message_approval`` returns the owner setting (default ``False``); it is the only
  place where a message approval can come from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.comparables import ComparableSetResult
from suv_deals.domain.costs import ScenarioSet
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Co2Cycle,
    Confidence,
    Drive,
    EligibilityState,
    EmailProviderKind,
    EvidenceKind,
    Fuel,
    Gearbox,
    InquiryReadiness,
    InquiryState,
    JobState,
    MessageLanguage,
    ProfileKey,
    ScenarioName,
    SuppressionReason,
    Tristate,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.language import LanguageDecision, LanguageStatus
from suv_deals.domain.listings import Co2Info, Documentation, NormalizedListing, sha256_json
from suv_deals.domain.money import Money
from suv_deals.domain.seller_contacts import (
    AddressError,
    ContactRecheck,
    RecipientBinding,
    RecipientDecision,
    RecipientStatus,
    SellerIdentity,
    canonicalize_address,
    seller_identity_key,
)
from suv_deals.domain.seller_templates import (
    ALLOWED_OUTGOING_DATA_CATEGORIES,
    EXCLUDED_DATA_CATEGORIES,
    INQUIRY_PURPOSE,
    PERMITTED_QUESTIONS,
    SCOPE_HASH,
    SCOPE_VERSION,
    MessageEnvelope,
    QuestionId,
    RenderedMessage,
    validate_scope,
)
from suv_deals.errors import IdempotencyConflict, ValidationFailed
from suv_deals.settings import Settings

INQUIRY_RULES_VERSION: Final = "inquiry_readiness@1.0.0"
DEFAULT_AUTHORIZATION_PATH: Final = (
    Path(__file__).resolve().parents[3] / "config" / "seller_inquiry_authorization.yaml"
)
#: PROPOSED engineering default: a qualifying observation must be this fresh (else recheck first).
MAX_OBSERVATION_AGE: Final = timedelta(hours=48)
#: PROPOSED engineering default: no second inquiry to the same seller (any car) within this window.
SELLER_COOLDOWN: Final = timedelta(days=7)
#: Spec 37.5 initial engineering safety defaults; ceilings, not targets. Owner may only reduce.
MAX_INQUIRIES_PER_24H: Final = 2
MAX_INQUIRIES_PER_15D: Final = 5
WINDOW_24H: Final = timedelta(hours=24)
WINDOW_15D: Final = timedelta(days=15)
MAX_SEND_ATTEMPTS: Final = 3
EMAIL_DELIVERY_UNCERTAIN: Final = "EMAIL_DELIVERY_UNCERTAIN"

_FROZEN = ConfigDict(frozen=True, extra="forbid")

NOT_AUTHORIZED_ACTIONS: Final[tuple[str, ...]] = (
    "follow_up",
    "outgoing_reply",
    "offer",
    "price_acceptance",
    "negotiation_beyond_lowest_price",
    "reservation",
    "viewing_appointment",
    "deposit",
    "purchase",
    "resale_promise",
    "payment",
)


def _utc(value: datetime) -> datetime:
    return ensure_utc(value)


# =============================================================================================
# Standing authorization (application audit record)
# =============================================================================================


class AuthorizationRevocation(BaseModel):
    model_config = _FROZEN

    revoked: bool = False
    revoked_at: datetime | None = None
    revoked_by: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("revoked_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _complete(self) -> AuthorizationRevocation:
        fields = (self.revoked_at, self.revoked_by, self.reason)
        if self.revoked and any(f is None for f in fields):
            raise ValueError("a revocation needs revoked_at, revoked_by and reason")
        if not self.revoked and any(f is not None for f in fields):
            raise ValueError("revocation details are only recorded with revoked: true")
        return self


class SellerInquiryAuthorization(BaseModel):
    """Versioned record of the bounded standing authorization (spec 37.1). Audit, not a prompt rule."""

    model_config = _FROZEN

    kind: Literal["seller_inquiry_authorization"]
    record_type: Literal["application_audit_record"]
    version: int = Field(ge=1)
    owner: str = Field(min_length=1, max_length=100)
    effective_date: date
    recorded_at: date
    source: str = Field(min_length=1, max_length=500)
    approval_mode: Literal["no_message_approval"]
    purpose: Literal["initial_availability_documents_price"]
    questions: tuple[QuestionId, ...]
    recipient_class: Literal["verified_seller_of_exact_listing"]
    max_inquiries_per_vehicle_seller_pair: Literal[1]
    scope_version: int = Field(ge=1)
    languages: tuple[MessageLanguage, ...] = Field(min_length=1)
    english_requires_positive_evidence: Literal[True]
    allowed_outgoing_data_categories: tuple[str, ...]
    excluded_data_categories: tuple[str, ...]
    attachments_allowed: Literal[False]
    cc_bcc_allowed: Literal[False]
    additional_recipients_allowed: Literal[False]
    follow_ups_allowed: Literal[False]
    not_authorized: tuple[str, ...]
    profiles_in_scope: tuple[ProfileKey, ...] = Field(min_length=1)
    revocation: AuthorizationRevocation = AuthorizationRevocation()

    @model_validator(mode="after")
    def _bounded(self) -> SellerInquiryAuthorization:
        if self.questions != PERMITTED_QUESTIONS:
            raise ValueError("questions must be exactly availability, vehicle_documents, lowest_final_price")
        if self.scope_version != SCOPE_VERSION:
            raise ValueError(f"scope_version must be {SCOPE_VERSION}")
        if set(self.allowed_outgoing_data_categories) != set(ALLOWED_OUTGOING_DATA_CATEGORIES) or len(
            self.allowed_outgoing_data_categories
        ) != len(ALLOWED_OUTGOING_DATA_CATEGORIES):
            raise ValueError("allowed_outgoing_data_categories must be exactly the spec 37.1 list")
        missing_excluded = set(EXCLUDED_DATA_CATEGORIES) - set(self.excluded_data_categories)
        if missing_excluded:
            raise ValueError("excluded_data_categories must list every spec 37.1 exclusion")
        if set(self.excluded_data_categories) & set(self.allowed_outgoing_data_categories):
            raise ValueError("a data category cannot be both allowed and excluded")
        missing_forbidden = set(NOT_AUTHORIZED_ACTIONS) - set(self.not_authorized)
        if missing_forbidden:
            raise ValueError("not_authorized must list every action outside the bounded inquiry")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("duplicate languages")
        if len(set(self.profiles_in_scope)) != len(self.profiles_in_scope):
            raise ValueError("duplicate profiles_in_scope")
        if self.recorded_at < self.effective_date and self.version == 1:
            raise ValueError("the first version cannot be recorded before it takes effect")
        return self

    def problems_at(self, at: datetime) -> tuple[str, ...]:
        """Why the authorization does not cover sending at ``at`` (empty = it does)."""
        at = _utc(at)
        problems: list[str] = []
        if at.date() < self.effective_date:
            problems.append("AUTHORIZATION_NOT_EFFECTIVE")
        if self.revocation.revoked and (
            self.revocation.revoked_at is None or self.revocation.revoked_at <= at
        ):
            problems.append("AUTHORIZATION_REVOKED")
        return tuple(problems)

    def covers_profile(self, profile: ProfileKey | None) -> bool:
        return profile is not None and profile in self.profiles_in_scope

    def fingerprint(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


def load_seller_inquiry_authorization(path: Path | None = None) -> SellerInquiryAuthorization:
    """Load and validate ``config/seller_inquiry_authorization.yaml`` (or ``path``)."""
    target = path or DEFAULT_AUTHORIZATION_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationFailed(f"cannot read seller inquiry authorization {target.name}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed("the seller inquiry authorization must be a mapping")
    try:
        return SellerInquiryAuthorization.model_validate(raw)
    except ValidationError as exc:
        raise ValidationFailed(
            "invalid seller inquiry authorization",
            details={"problems": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:50]},
        ) from exc


def requires_message_approval(settings: Settings) -> bool:
    """The ONLY source of a message-approval requirement: the explicit owner setting.

    Default ``False`` (standing authorization, spec 37.1). No other code path may add approval.
    """
    return settings.seller_inquiry_require_message_approval


# =============================================================================================
# Inquiry identity and the one-inquiry rule
# =============================================================================================


class VehicleIdentityRef(BaseModel):
    """Canonical vehicle identity: a confirmed vehicle cluster, else one listing incarnation."""

    model_config = _FROZEN

    kind: Literal["vehicle_cluster", "listing_incarnation"]
    id: UUID

    def key(self) -> str:
        return f"{self.kind}:{self.id}"


def canonical_vehicle_identity(
    *, vehicle_cluster_id: UUID | None, listing_incarnation_id: UUID
) -> VehicleIdentityRef:
    if vehicle_cluster_id is not None:
        return VehicleIdentityRef(kind="vehicle_cluster", id=vehicle_cluster_id)
    return VehicleIdentityRef(kind="listing_incarnation", id=listing_incarnation_id)


class InquiryIdentity(BaseModel):
    """workspace + canonical vehicle + verified seller + purpose (spec 37.5)."""

    model_config = _FROZEN

    workspace_id: UUID
    vehicle: VehicleIdentityRef
    seller_key: str = Field(pattern=r"^seller_(entity|alias):[0-9a-f-]{32,64}$")
    purpose: Literal["initial_availability_documents_price"] = INQUIRY_PURPOSE

    def key(self) -> str:
        return sha256_json(
            {
                "workspace_id": str(self.workspace_id),
                "vehicle": self.vehicle.key(),
                "seller": self.seller_key,
                "purpose": self.purpose,
            }
        )


def build_inquiry_identity(
    workspace_id: UUID,
    *,
    vehicle_cluster_id: UUID | None,
    listing_incarnation_id: UUID,
    seller: SellerIdentity,
) -> InquiryIdentity:
    return InquiryIdentity(
        workspace_id=workspace_id,
        vehicle=canonical_vehicle_identity(
            vehicle_cluster_id=vehicle_cluster_id, listing_incarnation_id=listing_incarnation_id
        ),
        seller_key=seller_identity_key(seller),
    )


#: States in which no reservation exists yet: the same record simply continues.
PRE_RESERVATION_STATES: Final = frozenset(
    {InquiryState.CANDIDATE, InquiryState.QUALIFYING, InquiryState.HELD_FACTS}
)
#: States in which the message may have left (or did leave) the system.
POSSIBLY_TRANSMITTED_STATES: Final = frozenset(
    {
        InquiryState.SENDING,
        InquiryState.UNCERTAIN,
        InquiryState.ACCEPTED,
        InquiryState.NO_REPLY_YET,
        InquiryState.REPLIED,
        InquiryState.BOUNCED,
        InquiryState.SELLER_OPTED_OUT,
    }
)


class ExistingInquiry(BaseModel):
    """An inquiry already recorded for this workspace (as read under the identity locks)."""

    model_config = _FROZEN

    inquiry_id: UUID
    identity_key: str
    vehicle: VehicleIdentityRef
    seller_key: str
    state: InquiryState
    transmission_attempts: int = Field(default=0, ge=0)
    reserved_at: datetime | None = None
    last_contact_at: datetime | None = None  # reservation/transmission time, for seller cooldown

    @field_validator("reserved_at", "last_contact_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @property
    def possibly_transmitted(self) -> bool:
        return self.state in POSSIBLY_TRANSMITTED_STATES or self.transmission_attempts > 0


def seller_contact_times(existing: Sequence[ExistingInquiry], seller_key: str) -> tuple[datetime, ...]:
    """Reservation/transmission times of every inquiry to this seller (input to the cooldown)."""
    times: list[datetime] = []
    for item in existing:
        moment = item.last_contact_at or item.reserved_at
        if item.seller_key == seller_key and moment is not None:
            times.append(moment)
    return tuple(times)


class RelatedListingLink(BaseModel):
    """A cross-site link from the candidate listing to another listing (identity.possible_same_vehicle)."""

    model_config = _FROZEN

    related_listing_id: UUID
    relation: Literal["confirmed_same_vehicle", "possible_same_unresolved", "rejected_not_same"]
    related_inquiry_state: InquiryState | None = None
    related_vehicle: VehicleIdentityRef | None = None


DuplicateOutcome = Literal[
    "clear",  # no inquiry exists for this identity
    "continue_existing",  # the same pre-reservation record (or never-transmitted cancellation) continues
    "in_progress",  # reserved/queued for this identity
    "prior_inquiry",  # already (possibly) sent: the one inquiry is used up
    "suppressed",  # suppressed; only an explicit, audited removal re-opens it
    "possible_duplicate",  # plausible cross-site duplicate unresolved: hold the additional send
]


class DuplicateDecision(BaseModel):
    model_config = _FROZEN

    outcome: DuplicateOutcome
    reasons: tuple[str, ...] = ()
    existing_inquiry_id: UUID | None = None

    @property
    def blocks(self) -> bool:
        return self.outcome not in {"clear", "continue_existing"}


_ACTIVE_STATES_FOR_OTHERS: Final = (
    frozenset({InquiryState.RESERVED, InquiryState.QUEUED})
    | POSSIBLY_TRANSMITTED_STATES
    | {InquiryState.FAILED_DEFINITE}
)


def evaluate_duplicate_contact(
    identity: InquiryIdentity,
    existing: Sequence[ExistingInquiry],
    related: Sequence[RelatedListingLink] = (),
) -> DuplicateDecision:
    """Apply the one-inquiry rule and the unresolved cross-site duplicate hold (spec 37.5)."""
    key = identity.key()
    same = [e for e in existing if e.identity_key == key]
    rank = {"continue_existing": 0, "in_progress": 2, "suppressed": 3, "prior_inquiry": 4}
    worst: DuplicateDecision | None = None
    for item in same:
        if item.possibly_transmitted:
            decision = DuplicateDecision(
                outcome="prior_inquiry", reasons=("PRIOR_INQUIRY",), existing_inquiry_id=item.inquiry_id
            )
        elif item.state in PRE_RESERVATION_STATES or item.state == InquiryState.CANCELLED:
            decision = DuplicateDecision(outcome="continue_existing", existing_inquiry_id=item.inquiry_id)
        elif item.state == InquiryState.SUPPRESSED:
            decision = DuplicateDecision(
                outcome="suppressed", reasons=("INQUIRY_SUPPRESSED",), existing_inquiry_id=item.inquiry_id
            )
        elif item.state == InquiryState.FAILED_DEFINITE:
            decision = DuplicateDecision(
                outcome="prior_inquiry", reasons=("PRIOR_INQUIRY",), existing_inquiry_id=item.inquiry_id
            )
        else:  # reserved / queued
            decision = DuplicateDecision(
                outcome="in_progress", reasons=("INQUIRY_IN_PROGRESS",), existing_inquiry_id=item.inquiry_id
            )
        if worst is None or rank[decision.outcome] > rank[worst.outcome]:
            worst = decision
    if worst is not None and worst.outcome != "continue_existing":
        return worst

    reasons: list[str] = []
    for other in existing:
        if other.identity_key == key or other.state not in _ACTIVE_STATES_FOR_OTHERS:
            continue
        if other.vehicle == identity.vehicle and other.seller_key != identity.seller_key:
            reasons.append("SAME_VEHICLE_OTHER_SELLER_IDENTITY")
    for link in related:
        if link.relation == "rejected_not_same" or link.related_inquiry_state is None:
            continue
        if link.related_inquiry_state in PRE_RESERVATION_STATES:
            continue
        if link.related_inquiry_state == InquiryState.CANCELLED:
            continue
        if link.relation == "possible_same_unresolved":
            reasons.append("POSSIBLE_SAME_VEHICLE_UNRESOLVED")
        elif link.related_vehicle != identity.vehicle:
            reasons.append("IDENTITY_MERGE_PENDING")
    if reasons:
        return DuplicateDecision(outcome="possible_duplicate", reasons=tuple(dict.fromkeys(reasons)))
    return worst or DuplicateDecision(outcome="clear")


class MergeReconciliation(BaseModel):
    """Outcome of reconciling inquiries that now share one identity after a cluster/alias merge."""

    model_config = _FROZEN

    keep: UUID | None
    cancel: tuple[UUID, ...]  # never transmitted: cancel (and release their quota debits)
    transmitted_duplicates: tuple[UUID, ...]  # already (possibly) sent: flag for owner review
    conflict: bool  # more than one (possibly) transmitted inquiry for one identity


_ADVANCEMENT: Final[dict[InquiryState, int]] = {
    InquiryState.QUEUED: 3,
    InquiryState.RESERVED: 2,
    InquiryState.QUALIFYING: 1,
    InquiryState.HELD_FACTS: 1,
    InquiryState.CANDIDATE: 0,
}


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _reservation_order(item: ExistingInquiry) -> tuple[bool, datetime, str]:
    """Earliest reservation first; unreserved records last; ties by id (deterministic)."""
    return (item.reserved_at is None, item.reserved_at or _EPOCH, str(item.inquiry_id))


def reconcile_identity_merge(inquiries: Sequence[ExistingInquiry]) -> MergeReconciliation:
    """Keep exactly one inquiry for a merged identity so two aliases can never both send.

    A (possibly) transmitted inquiry always wins; otherwise the most advanced, earliest reserved
    one (ties by id). Every other never-transmitted inquiry is cancelled. Terminal suppressed or
    cancelled records are left as they are.
    """
    live = [
        i
        for i in inquiries
        if i.state not in {InquiryState.CANCELLED, InquiryState.SUPPRESSED} or i.possibly_transmitted
    ]
    sent = sorted((i for i in live if i.possibly_transmitted), key=_reservation_order)
    pending = [i for i in live if not i.possibly_transmitted]
    if sent:
        keep = sent[0]
        return MergeReconciliation(
            keep=keep.inquiry_id,
            cancel=tuple(sorted((i.inquiry_id for i in pending), key=str)),
            transmitted_duplicates=tuple(i.inquiry_id for i in sent[1:]),
            conflict=len(sent) > 1,
        )
    if not pending:
        return MergeReconciliation(keep=None, cancel=(), transmitted_duplicates=(), conflict=False)
    pending.sort(key=lambda i: (-_ADVANCEMENT.get(i.state, 0), *_reservation_order(i)))
    return MergeReconciliation(
        keep=pending[0].inquiry_id,
        cancel=tuple(sorted((i.inquiry_id for i in pending[1:]), key=str)),
        transmitted_duplicates=(),
        conflict=False,
    )


# =============================================================================================
# Readiness inputs
# =============================================================================================


class VehicleIdentification(BaseModel):
    """Verified make/model/generation/specification used for meaningful comparison."""

    model_config = _FROZEN

    make: str | None
    model: str | None
    generation: str | None
    generation_candidates: tuple[str, ...] = ()
    is_suv: bool | None
    confidence: Confidence | None
    matched_via: Literal["fields", "title", "none"]
    fuel: Fuel = Fuel.UNKNOWN
    gearbox: Gearbox = Gearbox.UNKNOWN
    drive: Drive = Drive.UNKNOWN

    @classmethod
    def from_screening(cls, screening: ScreeningResult, listing: NormalizedListing) -> VehicleIdentification:
        match = screening.taxonomy_match
        vehicle = listing.vehicle
        if match is None:
            return cls(
                make=None,
                model=None,
                generation=None,
                is_suv=None,
                confidence=None,
                matched_via="none",
                fuel=vehicle.fuel,
                gearbox=vehicle.gearbox,
                drive=vehicle.drive,
            )
        return cls(
            make=match.make,
            model=match.model,
            generation=match.generation,
            generation_candidates=match.generation_candidates,
            is_suv=match.is_suv,
            confidence=match.confidence,
            matched_via=match.matched_via,
            fuel=vehicle.fuel,
            gearbox=vehicle.gearbox,
            drive=vehicle.drive,
        )


class SourceObservationFacts(BaseModel):
    model_config = _FROZEN

    source_key: str = Field(min_length=1, max_length=80)
    source_enabled: bool
    source_paused: bool = False
    terms_blocked: bool = False  # owner decision do_not_use
    access_blocked: bool = False
    last_detail_success_at: datetime | None
    availability: Availability

    @field_validator("last_detail_success_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class ComparableEvidence(BaseModel):
    """MK asking/verified-sale comparable evidence with a concrete matching rationale."""

    model_config = _FROZEN

    status: Literal["adequate", "small_sample", "insufficient_comparables"]
    asking_count: int = Field(ge=0)
    verified_sale_count: int = Field(ge=0, default=0)
    matching_rationale: str | None = Field(default=None, max_length=1000)
    comparable_set_id: UUID | None = None
    criteria_version: str | None = None
    mk_band_fit: Literal["below", "within", "above", "unknown"] = "unknown"

    @classmethod
    def from_comparable_set(
        cls, result: ComparableSetResult, *, comparable_set_id: UUID | None = None
    ) -> ComparableEvidence:
        asking = [s for s in result.selected if s.evidence_kind == EvidenceKind.ASKING_PRICE]
        sales = [s for s in result.selected if s.evidence_kind == EvidenceKind.VERIFIED_SALE]
        levels = {
            lvl: sum(1 for s in result.selected if s.match_level == lvl)
            for lvl in ("exact", "close", "widened")
        }
        target = result.target
        identity = (
            " ".join(p for p in (target.make, target.model, target.generation) if p) or "unknown vehicle"
        )
        rationale = None
        if asking or sales:
            rationale = (
                f"{result.criteria.criteria_version}: {identity}; "
                f"year window ±{result.criteria.year_window}, "
                f"mileage window ±{result.criteria.mileage_window_km} km; "
                f"{levels['exact']} exact, {levels['close']} close, {levels['widened']} widened; "
                f"{len(asking)} MK asking price(s), {len(sales)} verified sale(s); "
                "asking prices are not realized sale prices"
            )
        return cls(
            status=result.status,
            asking_count=len(asking),
            verified_sale_count=len(sales),
            matching_rationale=rationale,
            comparable_set_id=comparable_set_id,
            criteria_version=result.criteria.criteria_version,
            mk_band_fit=result.mk_band_fit,
        )


class CostEvidence(BaseModel):
    """What the available cost evidence says, without ever turning unknowns into zero."""

    model_config = _FROZEN

    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    best_case_known_costs: Money | None  # known modelled costs in the most favourable scenario
    best_case_proceeds: Money | None  # most favourable supported proceeds (None = unknown)
    unknown_cost_items: tuple[str, ...] = ()
    complete: bool = False
    threshold_approved: bool = False
    tax_rule_approved: bool | None = None  # None = not established
    cost_model_version: str | None = None

    @classmethod
    def from_scenario_set(
        cls, scenarios: ScenarioSet, *, tax_rule_approved: bool | None = None
    ) -> CostEvidence:
        upside = scenarios.scenario(ScenarioName.UPSIDE)
        return cls(
            currency=scenarios.currency,
            best_case_known_costs=upside.known_subtotal,
            best_case_proceeds=upside.expected_realized_proceeds,
            unknown_cost_items=tuple(dict.fromkeys(u.item for u in scenarios.unknown_lines)),
            complete=scenarios.complete,
            threshold_approved=scenarios.threshold.approval_status == "approved"
            and not scenarios.threshold.proposed_only,
            tax_rule_approved=tax_rule_approved,
            cost_model_version=scenarios.model_version,
        )

    def disproves_opportunity(self) -> bool:
        """Known costs alone reach the most favourable proceeds: no positive contribution possible."""
        if self.best_case_known_costs is None or self.best_case_proceeds is None:
            return False
        if self.best_case_known_costs.currency != self.best_case_proceeds.currency:
            return False
        return self.best_case_known_costs.amount >= self.best_case_proceeds.amount


SuppressionScope = Literal["workspace", "seller", "address", "vehicle", "source", "sender"]


class SuppressionRecord(BaseModel):
    """An ``ops.email_suppressions`` row. Removal needs an explicit audit record."""

    model_config = _FROZEN

    suppression_id: UUID | None = None
    scope: SuppressionScope
    key: str = Field(min_length=1, max_length=320)
    reason: SuppressionReason
    effective_at: datetime
    removed_at: datetime | None = None
    removal_audit_id: UUID | None = None

    @field_validator("effective_at", "removed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _removal(self) -> SuppressionRecord:
        if (self.removed_at is None) != (self.removal_audit_id is None):
            raise ValueError("a suppression is removed only with an explicit removal audit record")
        if self.removed_at is not None and self.removed_at < self.effective_at:
            raise ValueError("removed_at precedes effective_at")
        return self

    def active_at(self, at: datetime) -> bool:
        at = _utc(at)
        return self.effective_at <= at and (self.removed_at is None or self.removed_at > at)


class SuppressionTargets(BaseModel):
    """What a dispatch would touch; suppressions on any of these block it."""

    model_config = _FROZEN

    workspace_id: UUID
    seller_key: str
    canonical_address: str | None = None
    vehicle_key: str | None = None
    source_key: str | None = None
    sender_binding_id: UUID | None = None


def _address_match_key(address: str) -> str:
    try:
        return canonicalize_address(address).canonical.casefold()
    except AddressError:
        return address.strip().casefold()


def matching_suppressions(
    records: Sequence[SuppressionRecord], targets: SuppressionTargets, *, at: datetime
) -> tuple[SuppressionRecord, ...]:
    """Active suppressions that apply to ``targets`` at ``at``.

    Address suppressions match the canonical address case-insensitively: for *suppression* the
    broader match is the safe (fail-closed) direction. A seller-scope suppression keeps applying
    when the seller's address changes or the advertisement reappears.
    """
    wanted: dict[str, set[str]] = {
        "workspace": {str(targets.workspace_id), "*"},
        "seller": {targets.seller_key},
        "address": set(),
        "vehicle": {targets.vehicle_key} if targets.vehicle_key else set(),
        "source": {targets.source_key} if targets.source_key else set(),
        "sender": {str(targets.sender_binding_id)} if targets.sender_binding_id else set(),
    }
    if targets.canonical_address:
        wanted["address"].add(_address_match_key(targets.canonical_address))
    result: list[SuppressionRecord] = []
    for record in records:
        if not record.active_at(at):
            continue
        key = _address_match_key(record.key) if record.scope == "address" else record.key
        if key in wanted[record.scope]:
            result.append(record)
    return tuple(result)


class DisqualifierFacts(BaseModel):
    model_config = _FROZEN

    fraud_warnings: tuple[str, ...] = ()
    identity_conflict_open: bool = False
    availability_conflict: bool = False  # e.g. seller says sold while an active duplicate exists
    seller_opted_out: bool = False
    active_suppressions: tuple[SuppressionRecord, ...] = ()  # already matched for this inquiry


SenderMode = Literal["disabled_until_sender_ready", "automatic", "paused"]


class SenderStatus(BaseModel):
    """The configured, owner-authorized sending identity and its verified health."""

    model_config = _FROZEN

    mode: SenderMode
    kill_switch: bool = False
    provider: EmailProviderKind | None = None
    binding_id: UUID | None = None
    binding_version: int | None = Field(default=None, ge=1)
    account_id: str | None = Field(default=None, max_length=320)
    from_address: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=64)
    reply_to_address: str | None = Field(default=None, max_length=320)
    alias_verified: bool = False
    verified_at: datetime | None = None
    health_ok: bool = False
    credentials_revoked: bool = False

    @field_validator("verified_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        binding_id: UUID | None,
        binding_version: int | None,
        display_name: str | None,
        alias_verified: bool,
        verified_at: datetime | None,
        health_ok: bool,
        credentials_revoked: bool = False,
    ) -> SenderStatus:
        provider = (
            EmailProviderKind(settings.seller_email_provider) if settings.seller_email_provider else None
        )
        return cls(
            mode=settings.seller_inquiry_mode,
            kill_switch=settings.seller_inquiry_kill_switch,
            provider=provider,
            binding_id=binding_id,
            binding_version=binding_version,
            account_id=settings.seller_email_account_id,
            from_address=settings.seller_email_from,
            display_name=display_name,
            reply_to_address=settings.seller_email_reply_to,
            alias_verified=alias_verified,
            verified_at=verified_at,
            health_ok=health_ok,
            credentials_revoked=credentials_revoked,
        )

    def identity_problems(self) -> tuple[str, ...]:
        """Technical sender prerequisites (setup/verification), never message approval."""
        problems: list[str] = []
        if self.provider is None:
            problems.append("SENDER_PROVIDER_MISSING")
        if not self.account_id:
            problems.append("SENDER_ACCOUNT_MISSING")
        for name, value in (
            ("SENDER_FROM_INVALID", self.from_address),
            ("REPLY_TO_INVALID", self.reply_to_address),
        ):
            if value is None:
                if name == "SENDER_FROM_INVALID":
                    problems.append(name)
                continue
            try:
                canonicalize_address(value)
            except AddressError:
                problems.append(name)
        if not self.display_name or not self.display_name.strip():
            problems.append("SENDER_DISPLAY_NAME_MISSING")
        if self.binding_id is None or self.binding_version is None or self.verified_at is None:
            problems.append("SENDER_NOT_VERIFIED")
        if not self.alias_verified:
            problems.append("SENDER_ALIAS_NOT_VERIFIED")
        if self.credentials_revoked:
            problems.append("SENDER_REVOKED")
        if not self.health_ok:
            problems.append("SENDER_UNHEALTHY")
        return tuple(problems)


# =============================================================================================
# Rate caps and seller cooldown
# =============================================================================================


class RateCapPolicy(BaseModel):
    """Workspace ceilings (spec 37.5). The owner may reduce or pause (0), never raise them here."""

    model_config = _FROZEN

    max_per_24h: int = Field(default=MAX_INQUIRIES_PER_24H, ge=0, le=MAX_INQUIRIES_PER_24H)
    max_per_15d: int = Field(default=MAX_INQUIRIES_PER_15D, ge=0, le=MAX_INQUIRIES_PER_15D)
    seller_cooldown: timedelta = SELLER_COOLDOWN

    @classmethod
    def from_settings(cls, settings: Settings) -> RateCapPolicy:
        if (
            settings.seller_inquiry_max_per_24h > MAX_INQUIRIES_PER_24H
            or settings.seller_inquiry_max_per_rolling_15d > MAX_INQUIRIES_PER_15D
        ):
            raise ValidationFailed(
                "seller inquiry caps above 2 per 24 h / 5 per 15 days are outside the v1.1 scope",
                details={"problems": ["RATE_CAP_ABOVE_CEILING"]},
            )
        return cls(
            max_per_24h=settings.seller_inquiry_max_per_24h,
            max_per_15d=settings.seller_inquiry_max_per_rolling_15d,
        )


class QuotaDebit(BaseModel):
    """One unit of the workspace quota: a reservation (or later transmission) at ``at``.

    Uncertain sends keep their debit; only never-transmitted cancellations release it.
    """

    model_config = _FROZEN

    inquiry_id: UUID
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class RateCapDecision(BaseModel):
    model_config = _FROZEN

    allowed: bool
    count_24h: int
    count_15d: int
    limit_24h: int
    limit_15d: int
    next_allowed_at: datetime | None
    reasons: tuple[str, ...] = ()


def _window_next(times: list[datetime], limit: int, window: timedelta) -> datetime:
    """When the in-window count drops below ``limit`` (times sorted ascending, len >= limit)."""
    return times[len(times) - limit] + window


def evaluate_rate_caps(
    debits: Sequence[QuotaDebit],
    *,
    now: datetime,
    policy: RateCapPolicy,
    exclude_inquiry_id: UUID | None = None,
) -> RateCapDecision:
    """May ONE more inquiry use the quota at ``now``? Rolling windows ``(now - window, now]``.

    A debit exactly ``window`` old has expired; future-dated debits (clock skew) still count.
    ``exclude_inquiry_id`` lets a dispatch re-check exclude its own reservation debit.
    """
    now = _utc(now)
    times = sorted(d.at for d in debits if d.inquiry_id != exclude_inquiry_id)
    in_24 = [t for t in times if t > now - WINDOW_24H]
    in_15 = [t for t in times if t > now - WINDOW_15D]
    reasons: list[str] = []
    candidates: list[datetime] = []
    never = False
    for count_list, limit, window, code in (
        (in_24, policy.max_per_24h, WINDOW_24H, "RATE_CAP_24H_REACHED"),
        (in_15, policy.max_per_15d, WINDOW_15D, "RATE_CAP_15D_REACHED"),
    ):
        if limit == 0:
            reasons.append("RATE_CAP_ZERO")
            never = True
        elif len(count_list) >= limit:
            reasons.append(code)
            candidates.append(_window_next(count_list, limit, window))
    allowed = not reasons
    next_at = None if allowed or never else max(candidates)
    return RateCapDecision(
        allowed=allowed,
        count_24h=len(in_24),
        count_15d=len(in_15),
        limit_24h=policy.max_per_24h,
        limit_15d=policy.max_per_15d,
        next_allowed_at=next_at,
        reasons=tuple(dict.fromkeys(reasons)),
    )


class CooldownDecision(BaseModel):
    model_config = _FROZEN

    active: bool
    last_contact_at: datetime | None
    until: datetime | None


def evaluate_seller_cooldown(
    seller_contact_times: Sequence[datetime], *, now: datetime, cooldown: timedelta = SELLER_COOLDOWN
) -> CooldownDecision:
    """Seller-level cooldown across all of the seller's vehicles (no bursts to one dealer)."""
    now = _utc(now)
    if not seller_contact_times:
        return CooldownDecision(active=False, last_contact_at=None, until=None)
    last = max(_utc(t) for t in seller_contact_times)
    until = last + cooldown
    return CooldownDecision(active=now < until, last_contact_at=last, until=until)


# =============================================================================================
# Readiness evaluation (spec 37.2)
# =============================================================================================


class ReadinessSeverity(StrEnum):
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_FACTS = "needs_facts"
    NEEDS_TECHNICAL_REVIEW = "needs_technical_review"
    HOLD = "hold"  # qualified, but dispatch must wait (kill switch, pause, caps, cooldown)
    INFO = "info"


class ReadinessReason(BaseModel):
    model_config = _FROZEN

    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    check: int = Field(ge=0, le=6)  # spec 37.2 check number; 0 = authorization/general
    severity: ReadinessSeverity
    message: str = Field(max_length=300)


class InquiryReadinessInputs(BaseModel):
    """Everything the readiness decision may look at (assembled by the caller from records)."""

    model_config = _FROZEN

    as_of: datetime
    listing_id: UUID
    authorization: SellerInquiryAuthorization
    identity: InquiryIdentity | None = None
    screening: ScreeningResult
    vehicle: VehicleIdentification
    source: SourceObservationFacts
    comparables: ComparableEvidence | None
    costs: CostEvidence | None
    documentation: Documentation | None = None
    co2: Co2Info | None = None
    disqualifiers: DisqualifierFacts = DisqualifierFacts()
    duplicate: DuplicateDecision
    recipient: RecipientDecision | None
    language: LanguageDecision | None
    sender: SenderStatus
    rate_caps: RateCapDecision | None = None
    seller_cooldown: CooldownDecision | None = None
    max_observation_age: timedelta = MAX_OBSERVATION_AGE

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class InquiryReadinessDecision(BaseModel):
    model_config = _FROZEN

    readiness: InquiryReadiness
    reasons: tuple[ReadinessReason, ...]
    rationale_version: str = INQUIRY_RULES_VERSION
    evidence: dict[str, object]
    economics_incomplete: bool
    open_questions: tuple[str, ...]  # what the bounded inquiry asks/resolves
    can_reserve_now: bool
    next_attempt_at: datetime | None = None
    rationale_hash: str

    def codes(self) -> set[str]:
        return {r.code for r in self.reasons}


_SEVERITY_ORDER: Final[tuple[tuple[ReadinessSeverity, InquiryReadiness], ...]] = (
    (ReadinessSeverity.NOT_ELIGIBLE, InquiryReadiness.NOT_ELIGIBLE),
    (ReadinessSeverity.NEEDS_FACTS, InquiryReadiness.NEEDS_FACTS),
    (ReadinessSeverity.NEEDS_TECHNICAL_REVIEW, InquiryReadiness.NEEDS_TECHNICAL_REVIEW),
)
_UNAVAILABLE: Final = frozenset({Availability.SOLD_CLAIMED, Availability.REMOVED})


class _Collector:
    def __init__(self) -> None:
        self.reasons: list[ReadinessReason] = []

    def add(self, code: str, check: int, severity: ReadinessSeverity, message: str) -> None:
        self.reasons.append(ReadinessReason(code=code, check=check, severity=severity, message=message[:300]))


def _check_authorization(i: InquiryReadinessInputs, c: _Collector) -> None:
    for problem in i.authorization.problems_at(i.as_of):
        c.add(problem, 0, ReadinessSeverity.NOT_ELIGIBLE, "standing authorization does not cover this time")


def _check_rules_and_vehicle(i: InquiryReadinessInputs, c: _Collector) -> None:
    s = i.screening
    nf, ne = ReadinessSeverity.NEEDS_FACTS, ReadinessSeverity.NOT_ELIGIBLE
    if s.state == EligibilityState.REJECTED:
        c.add("HARD_RULE_FAILED", 1, ne, "primary price/mileage/SUV rules are not met")
    elif s.state == EligibilityState.NEEDS_FACTS:
        missing = ", ".join(s.missing_facts[:10]) or "screening facts"
        c.add("SCREENING_NEEDS_FACTS", 1, nf, f"hard-rule facts missing: {missing}")
    elif s.state == EligibilityState.ELIGIBLE_MANUAL_PROFILE and not i.authorization.covers_profile(
        s.profile
    ):
        c.add("PROFILE_OUT_OF_INQUIRY_SCOPE", 1, ne, "research profile is not in the automatic inquiry scope")
    elif s.state == EligibilityState.ELIGIBLE_PRIMARY and not i.authorization.covers_profile(
        ProfileKey.PRIMARY
    ):
        c.add("PROFILE_OUT_OF_INQUIRY_SCOPE", 1, ne, "primary profile is not in the automatic inquiry scope")

    v = i.vehicle
    if v.is_suv is False:
        c.add("NOT_SUV", 1, ne, "taxonomy says this model is not an SUV")
    elif v.is_suv is None:
        c.add("SUV_IDENTITY_UNKNOWN", 1, nf, "SUV identity is not established")
    if v.make is None or v.model is None:
        c.add("VEHICLE_MODEL_UNIDENTIFIED", 1, nf, "make/model not identified from verified fields")
    elif v.matched_via != "fields" or v.confidence in (None, Confidence.LOW):
        c.add(
            "VEHICLE_IDENTIFICATION_WEAK", 1, nf, "make/model identified only weakly (title/low confidence)"
        )
    if v.generation is None:
        if len(v.generation_candidates) > 1:
            c.add("GENERATION_AMBIGUOUS", 1, nf, "generation ambiguous near a model change")
        else:
            c.add("GENERATION_UNKNOWN", 1, nf, "generation not identified")
    if v.fuel == Fuel.UNKNOWN:
        c.add("SPEC_FUEL_UNKNOWN", 1, nf, "fuel not established")
    if v.gearbox == Gearbox.UNKNOWN:
        c.add("SPEC_GEARBOX_UNKNOWN", 1, nf, "gearbox not established")
    if v.drive == Drive.UNKNOWN:
        c.add("SPEC_DRIVE_UNKNOWN", 1, ReadinessSeverity.INFO, "drive not established; comparables flag it")


def _check_market(i: InquiryReadinessInputs, c: _Collector) -> None:
    src = i.source
    if src.terms_blocked:
        c.add(
            "SOURCE_NOT_PERMITTED", 2, ReadinessSeverity.NOT_ELIGIBLE, "source terms decision is do_not_use"
        )
    elif not src.source_enabled or src.source_paused or src.access_blocked:
        c.add(
            "SOURCE_NOT_ACTIVE", 2, ReadinessSeverity.NEEDS_TECHNICAL_REVIEW, "source paused/disabled/blocked"
        )
    if src.last_detail_success_at is None or i.as_of - src.last_detail_success_at > i.max_observation_age:
        c.add(
            "OBSERVATION_STALE",
            2,
            ReadinessSeverity.NEEDS_FACTS,
            "listing must be re-observed before inquiry",
        )
    comps = i.comparables
    if comps is None:
        c.add("COMPARABLES_MISSING", 2, ReadinessSeverity.NEEDS_FACTS, "no MK comparable evaluation yet")
    elif comps.status == "insufficient_comparables" or comps.asking_count + comps.verified_sale_count == 0:
        c.add(
            "INSUFFICIENT_COMPARABLES", 2, ReadinessSeverity.NEEDS_FACTS, "no credible MK comparable evidence"
        )
    elif not comps.matching_rationale or not comps.matching_rationale.strip():
        c.add(
            "MATCHING_RATIONALE_MISSING",
            2,
            ReadinessSeverity.NEEDS_TECHNICAL_REVIEW,
            "comparables lack a concrete matching rationale",
        )
    elif comps.status == "small_sample":
        c.add("SMALL_COMPARABLE_SAMPLE", 2, ReadinessSeverity.INFO, "few MK comparables; asking prices only")


def _check_costs(i: InquiryReadinessInputs, c: _Collector) -> bool:
    """Returns ``economics_incomplete``."""
    costs = i.costs
    info = ReadinessSeverity.INFO
    if costs is None:
        c.add("COSTS_NOT_EVALUATED", 3, info, "no cost scenarios yet; all costs unknown, never zero")
        return True
    if costs.disproves_opportunity():
        c.add(
            "KNOWN_COSTS_EXCEED_PROCEEDS",
            3,
            ReadinessSeverity.NOT_ELIGIBLE,
            "known costs alone reach the most favourable supported proceeds",
        )
    incomplete = not costs.complete
    if costs.unknown_cost_items:
        c.add("UNKNOWN_COSTS_LISTED", 3, info, "unknown: " + ", ".join(costs.unknown_cost_items[:15]))
        incomplete = True
    if costs.best_case_proceeds is None:
        c.add("PROCEEDS_UNKNOWN", 3, info, "supported proceeds unknown")
        incomplete = True
    if not costs.threshold_approved:
        c.add(
            "PROPOSED_THRESHOLD_NOT_APPLIED",
            3,
            info,
            "PROPOSED EUR 1,500 threshold is unapproved; not a gate",
        )
        incomplete = True
    if costs.tax_rule_approved is not True:
        c.add("TAX_RULE_NOT_APPROVED", 3, info, "no approved production tax rule; not a gate for the inquiry")
        incomplete = True
    return incomplete


def _check_disqualifiers(i: InquiryReadinessInputs, c: _Collector) -> None:
    d = i.disqualifiers
    ne, tr = ReadinessSeverity.NOT_ELIGIBLE, ReadinessSeverity.NEEDS_TECHNICAL_REVIEW
    if d.fraud_warnings:
        c.add("FRAUD_WARNING", 4, ne, "fraud warning: " + ", ".join(d.fraud_warnings[:5]))
    if d.identity_conflict_open:
        c.add("IDENTITY_CONFLICT", 4, tr, "open listing identity conflict")
    availability = i.source.availability
    if availability in _UNAVAILABLE:
        c.add("VEHICLE_UNAVAILABLE", 4, ne, f"listing availability is {availability.value}")
    elif availability == Availability.RESERVED:
        c.add("VEHICLE_RESERVED", 4, ReadinessSeverity.NEEDS_FACTS, "listing is marked reserved")
    if d.availability_conflict:
        c.add("CONTRADICTORY_AVAILABILITY", 4, tr, "contradictory availability evidence; outreach stopped")
    if d.seller_opted_out:
        c.add("SELLER_OPTED_OUT", 4, ne, "seller asked not to be contacted")
    for record in d.active_suppressions:
        c.add(f"SUPPRESSED_{record.reason.value.upper()}", 4, ne, f"active {record.scope} suppression")
    dup = i.duplicate
    if dup.outcome == "prior_inquiry":
        c.add("PRIOR_INQUIRY", 4, ne, "the one inquiry for this vehicle/seller pair was already used")
    elif dup.outcome == "in_progress":
        c.add("INQUIRY_IN_PROGRESS", 4, ne, "an inquiry for this vehicle/seller pair is already reserved")
    elif dup.outcome == "suppressed":
        c.add("INQUIRY_SUPPRESSED", 4, ne, "this vehicle/seller inquiry is suppressed")
    elif dup.outcome == "possible_duplicate":
        c.add(
            "POSSIBLE_DUPLICATE_CONTACT", 4, tr, "unresolved cross-site duplicate: " + ", ".join(dup.reasons)
        )


def _check_recipient_language(i: InquiryReadinessInputs, c: _Collector) -> None:
    nf, tr = ReadinessSeverity.NEEDS_FACTS, ReadinessSeverity.NEEDS_TECHNICAL_REVIEW
    r = i.recipient
    if r is None:
        c.add("RECIPIENT_UNKNOWN", 5, nf, "no seller contact evidence yet")
    elif r.status == RecipientStatus.SELLER_EMAIL_UNAVAILABLE:
        c.add("SELLER_EMAIL_UNAVAILABLE", 5, nf, "no e-mail address on the exact listing")
    elif r.status == RecipientStatus.RECHECK_REQUIRED:
        c.add("RECIPIENT_RECHECK_REQUIRED", 5, nf, "recipient evidence must be refreshed")
    elif r.status == RecipientStatus.REJECTED:
        c.add(
            "RECIPIENT_REJECTED",
            5,
            tr,
            "recipient evidence rejected: " + ", ".join(x.value for x in r.reasons),
        )
    elif r.status == RecipientStatus.NEEDS_TECHNICAL_REVIEW:
        c.add(
            "RECIPIENT_NEEDS_REVIEW",
            5,
            tr,
            "recipient evidence needs review: " + ", ".join(x.value for x in r.reasons),
        )
    elif r.binding is not None:
        if r.binding.listing_id != i.listing_id:
            c.add("RECIPIENT_NOT_FOR_THIS_LISTING", 5, tr, "recipient evidence belongs to another listing")
        if i.identity is not None and r.binding.seller_identity_key != i.identity.seller_key:
            c.add("RECIPIENT_SELLER_MISMATCH", 5, tr, "recipient evidence names another seller identity")
    lang = i.language
    if lang is None:
        c.add("LANGUAGE_UNKNOWN", 5, nf, "advertisement/seller language not evaluated")
    elif lang.status == LanguageStatus.LANGUAGE_UNRESOLVED:
        c.add(
            "LANGUAGE_UNRESOLVED", 5, nf, f"language unresolved ({lang.reason.value}); English is no fallback"
        )
    elif lang.status == LanguageStatus.UNSUPPORTED_LANGUAGE:
        c.add("LANGUAGE_UNSUPPORTED", 5, tr, "no template for this language; held, not replaced by English")
    elif lang.language is not None and lang.language not in i.authorization.languages:
        c.add("LANGUAGE_NOT_AUTHORIZED", 5, tr, "language not listed in the authorization record")


def _check_sender(i: InquiryReadinessInputs, c: _Collector) -> datetime | None:
    s = i.sender
    tr, hold = ReadinessSeverity.NEEDS_TECHNICAL_REVIEW, ReadinessSeverity.HOLD
    if s.mode == "disabled_until_sender_ready":
        c.add("SENDER_NOT_READY", 6, tr, "sending stays off until the sender account is verified")
    for problem in s.identity_problems():
        c.add(problem, 6, tr, "sender account prerequisite not met (technical setup, not approval)")
    if s.mode == "paused":
        c.add("INQUIRIES_PAUSED", 6, hold, "owner paused automatic inquiries")
    if s.kill_switch:
        c.add("KILL_SWITCH_ACTIVE", 6, hold, "inquiry kill switch is active")
    next_at: datetime | None = None
    if i.rate_caps is not None and not i.rate_caps.allowed:
        c.add(
            "RATE_CAP_REACHED",
            6,
            hold,
            "workspace inquiry ceiling reached: " + ", ".join(i.rate_caps.reasons),
        )
        next_at = i.rate_caps.next_allowed_at
    if i.seller_cooldown is not None and i.seller_cooldown.active:
        c.add("SELLER_COOLDOWN", 6, hold, "this seller was contacted recently")
        until = i.seller_cooldown.until
        if until is not None and (next_at is None or until > next_at):
            next_at = until
    return next_at


def _open_questions(i: InquiryReadinessInputs) -> tuple[str, ...]:
    questions = ["availability", "lowest_final_price"]
    doc = i.documentation
    if doc is None or doc.coc_available in (ClaimStatus.UNKNOWN, ClaimStatus.CONFLICTING):
        questions.append("coc")
    if doc is None or doc.registration_documents in (ClaimStatus.UNKNOWN, ClaimStatus.CONFLICTING):
        questions.append("registration_documents")
    if doc is None or doc.origin_evidence is None:
        questions.append("origin_evidence")
    co2 = i.co2
    if co2 is None or co2.g_per_km is None or co2.cycle == Co2Cycle.UNKNOWN:
        questions.append("co2_and_cycle")
    return tuple(questions)


def _evidence(i: InquiryReadinessInputs) -> dict[str, object]:
    r = i.recipient
    lang = i.language
    return {
        "authorization": {"version": i.authorization.version, "fingerprint": i.authorization.fingerprint()},
        "identity_key": i.identity.key() if i.identity else None,
        "screening": {
            "state": i.screening.state.value,
            "profile": i.screening.profile.value if i.screening.profile else None,
            "version": i.screening.screening_version,
        },
        "vehicle": {"make": i.vehicle.make, "model": i.vehicle.model, "generation": i.vehicle.generation},
        "comparables": None
        if i.comparables is None
        else {
            "status": i.comparables.status,
            "asking_count": i.comparables.asking_count,
            "verified_sale_count": i.comparables.verified_sale_count,
            "rationale": i.comparables.matching_rationale,
            "comparable_set_id": str(i.comparables.comparable_set_id)
            if i.comparables.comparable_set_id
            else None,
        },
        "costs": None
        if i.costs is None
        else {
            "best_case_known_costs": str(i.costs.best_case_known_costs.amount)
            if i.costs.best_case_known_costs
            else None,
            "best_case_proceeds": str(i.costs.best_case_proceeds.amount)
            if i.costs.best_case_proceeds
            else None,
            "currency": i.costs.currency,
            "unknown_cost_items": list(i.costs.unknown_cost_items),
            "cost_model_version": i.costs.cost_model_version,
        },
        "recipient": None
        if r is None
        else {
            "status": r.status.value,
            "evidence_kind": r.binding.evidence_kind.value if r.binding else None,
            "binding_fingerprint": r.binding.fingerprint() if r.binding else None,
        },
        "language": None
        if lang is None
        else {
            "status": lang.status.value,
            "language": lang.language.value if lang.language else None,
            "confidence": str(lang.confidence),
            "reason": lang.reason.value,
            "evidence_excerpt": lang.evidence_excerpt,
            "rules_version": lang.rules_version,
        },
        "sender": {
            "binding_id": str(i.sender.binding_id) if i.sender.binding_id else None,
            "binding_version": i.sender.binding_version,
            "mode": i.sender.mode,
        },
        "duplicate": i.duplicate.outcome,
    }


def evaluate_inquiry_readiness(inputs: InquiryReadinessInputs) -> InquiryReadinessDecision:
    """The six automatic checks of spec 37.2. No human click, no approval wait.

    Severity precedence: not_eligible > needs_facts > needs_technical_review > inquiry_ready.
    ``hold`` reasons (kill switch, owner pause, rate caps, seller cooldown) keep a qualified
    candidate ``inquiry_ready`` but set ``can_reserve_now=False`` (and ``next_attempt_at`` when
    computable).
    """
    c = _Collector()
    _check_authorization(inputs, c)
    _check_rules_and_vehicle(inputs, c)
    _check_market(inputs, c)
    incomplete = _check_costs(inputs, c)
    _check_disqualifiers(inputs, c)
    _check_recipient_language(inputs, c)
    next_at = _check_sender(inputs, c)
    questions = _open_questions(inputs)
    c.add("INQUIRY_RESOLVES_UNKNOWNS", 0, ReadinessSeverity.INFO, "asks: " + ", ".join(questions))
    if incomplete:
        c.add(
            "ECONOMICS_INCOMPLETE", 3, ReadinessSeverity.INFO, "economics incomplete; not a proven purchase"
        )

    severities = {r.severity for r in c.reasons}
    readiness = InquiryReadiness.INQUIRY_READY
    for severity, state in _SEVERITY_ORDER:
        if severity in severities:
            readiness = state
            break
    can_reserve = readiness == InquiryReadiness.INQUIRY_READY and ReadinessSeverity.HOLD not in severities
    evidence = _evidence(inputs)
    reasons = tuple(c.reasons)
    rationale_hash = sha256_json(
        {
            "version": INQUIRY_RULES_VERSION,
            "readiness": readiness.value,
            "reasons": [r.model_dump(mode="json") for r in reasons],
            "evidence": evidence,
        }
    )
    return InquiryReadinessDecision(
        readiness=readiness,
        reasons=reasons,
        evidence=evidence,
        economics_incomplete=incomplete,
        open_questions=questions,
        can_reserve_now=can_reserve,
        next_attempt_at=next_at if not can_reserve and readiness == InquiryReadiness.INQUIRY_READY else None,
        rationale_hash=rationale_hash,
    )


# =============================================================================================
# State machine (spec 37.5)
# =============================================================================================

ALLOWED_TRANSITIONS: Final[dict[InquiryState, frozenset[InquiryState]]] = {
    InquiryState.CANDIDATE: frozenset(
        {InquiryState.QUALIFYING, InquiryState.HELD_FACTS, InquiryState.SUPPRESSED, InquiryState.CANCELLED}
    ),
    InquiryState.QUALIFYING: frozenset(
        {InquiryState.RESERVED, InquiryState.HELD_FACTS, InquiryState.SUPPRESSED, InquiryState.CANCELLED}
    ),
    InquiryState.HELD_FACTS: frozenset(
        {InquiryState.QUALIFYING, InquiryState.SUPPRESSED, InquiryState.CANCELLED}
    ),
    InquiryState.RESERVED: frozenset({InquiryState.QUEUED, InquiryState.SUPPRESSED, InquiryState.CANCELLED}),
    InquiryState.QUEUED: frozenset({InquiryState.SENDING, InquiryState.SUPPRESSED, InquiryState.CANCELLED}),
    InquiryState.SENDING: frozenset(
        {InquiryState.ACCEPTED, InquiryState.UNCERTAIN, InquiryState.FAILED_DEFINITE}
    ),
    # Reconciliation only: never straight back to the queue.
    InquiryState.UNCERTAIN: frozenset({InquiryState.ACCEPTED, InquiryState.FAILED_DEFINITE}),
    # Guarded retry after a proven pre-submission failure (same account).
    InquiryState.FAILED_DEFINITE: frozenset({InquiryState.QUEUED}),
    InquiryState.ACCEPTED: frozenset(
        {InquiryState.REPLIED, InquiryState.BOUNCED, InquiryState.SELLER_OPTED_OUT, InquiryState.NO_REPLY_YET}
    ),
    InquiryState.NO_REPLY_YET: frozenset(
        {InquiryState.REPLIED, InquiryState.BOUNCED, InquiryState.SELLER_OPTED_OUT}
    ),
    InquiryState.REPLIED: frozenset({InquiryState.SELLER_OPTED_OUT}),
    # Re-qualification only for never-transmitted records, with audit for suppression removal.
    InquiryState.CANCELLED: frozenset({InquiryState.QUALIFYING}),
    InquiryState.SUPPRESSED: frozenset({InquiryState.QUALIFYING}),
    InquiryState.BOUNCED: frozenset(),
    InquiryState.SELLER_OPTED_OUT: frozenset(),
}
TERMINAL_STATES: Final = frozenset(s for s, targets in ALLOWED_TRANSITIONS.items() if not targets)
#: States whose binding (scope/template/body hash, sender, recipient) is immutable.
BINDING_IMMUTABLE_STATES: Final = frozenset(InquiryState) - PRE_RESERVATION_STATES


def can_transition(current: InquiryState, target: InquiryState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class SendAttemptOutcome(StrEnum):
    RUNNING = "running"
    ACCEPTED = "accepted"  # the provider accepted the submission (not delivery, not reading)
    PRE_SUBMISSION_FAILURE = "pre_submission_failure"  # failed before anything reached the provider
    DEFINITE_REJECTION = "definite_rejection"  # the provider definitively refused; nothing sent
    UNCERTAIN = "uncertain"  # timeout/crash/lease expiry after the hand-over may have happened


PreSubmissionProof = Literal[
    "connection_refused_before_submit",
    "local_validation_failed_before_submit",
    "credentials_rejected_before_submit",
    "provider_documented_not_sent",
]


class SendAttemptEvidence(BaseModel):
    """One durable send attempt (``ops.email_delivery_attempts``)."""

    model_config = _FROZEN

    attempt_id: UUID
    inquiry_id: UUID
    sender_binding_id: UUID
    provider: EmailProviderKind
    fencing_token: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    outcome: SendAttemptOutcome
    pre_submission_proof: PreSubmissionProof | None = None
    worker_alive: Tristate = Tristate.UNKNOWN
    lease_expires_at: datetime | None = None
    provider_idempotency_key: str | None = Field(default=None, max_length=200)
    provider_idempotency_documented: bool = False
    stable_message_id: str | None = Field(default=None, max_length=998)
    sent_items_search: Literal["not_searched", "found", "not_found"] = "not_searched"
    provider_search: Literal["not_searched", "found", "not_found", "unsupported"] = "not_searched"

    @field_validator("started_at", "finished_at", "lease_expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> SendAttemptEvidence:
        if (
            self.pre_submission_proof is not None
            and self.outcome != SendAttemptOutcome.PRE_SUBMISSION_FAILURE
        ):
            raise ValueError("pre-submission proof belongs to a pre-submission failure")
        return self

    def may_still_run(self, now: datetime) -> bool:
        """Could this attempt still be (or become) submitted by a live worker?"""
        if self.outcome == SendAttemptOutcome.RUNNING:
            return True
        if self.finished_at is not None:
            return False
        if self.worker_alive != Tristate.NO:
            return True
        return self.lease_expires_at is not None and self.lease_expires_at > _utc(now)

    @property
    def found_submitted(self) -> bool:
        return (
            self.outcome == SendAttemptOutcome.ACCEPTED
            or self.sent_items_search == "found"
            or self.provider_search == "found"
        )


class RetryDecision(BaseModel):
    model_config = _FROZEN

    retry: bool
    reasons: tuple[str, ...]
    sender_binding_id: UUID  # a retry may only use the original account
    idempotency_key: str | None = None


def should_retry(
    attempt: SendAttemptEvidence,
    *,
    now: datetime,
    other_attempts: Sequence[SendAttemptEvidence] = (),
    retry_sender_binding_id: UUID | None = None,
) -> RetryDecision:
    """Uncertain-send policy (spec 37.5). Never a blind resend, never another account."""
    now = _utc(now)
    attempts = [attempt, *(a for a in other_attempts if a.attempt_id != attempt.attempt_id)]
    no = attempt.sender_binding_id

    def deny(*reasons: str) -> RetryDecision:
        return RetryDecision(retry=False, reasons=reasons, sender_binding_id=no)

    if retry_sender_binding_id is not None and retry_sender_binding_id != attempt.sender_binding_id:
        return deny("DIFFERENT_ACCOUNT_FORBIDDEN")
    if any(a.inquiry_id != attempt.inquiry_id for a in attempts):
        raise ValidationFailed("attempts of different inquiries cannot be combined")
    if any(a.found_submitted for a in attempts):
        return deny("ALREADY_SUBMITTED")
    if any(a.may_still_run(now) for a in attempts):
        return deny("PRIOR_ATTEMPT_MAY_STILL_RUN")
    if len(attempts) >= MAX_SEND_ATTEMPTS:
        return deny("ATTEMPTS_EXHAUSTED")
    if attempt.outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE:
        if attempt.pre_submission_proof is None:
            return deny("NO_PROOF_OF_NON_SUBMISSION")
        return RetryDecision(retry=True, reasons=("PROVEN_PRE_SUBMISSION_FAILURE",), sender_binding_id=no)
    if attempt.outcome == SendAttemptOutcome.UNCERTAIN:
        if (
            attempt.provider_idempotency_documented
            and attempt.provider_idempotency_key
            and attempt.provider != EmailProviderKind.OUTLOOK_LOCAL  # Outlook .Send has none
        ):
            return RetryDecision(
                retry=True,
                reasons=("PROVIDER_DOCUMENTED_IDEMPOTENCY",),
                sender_binding_id=no,
                idempotency_key=attempt.provider_idempotency_key,
            )
        reasons = ["HOLD_FOR_RECONCILIATION"]
        if attempt.sent_items_search == "not_found" or attempt.provider_search == "not_found":
            reasons.append("EMPTY_SEARCH_IS_NOT_PROOF")
        return deny(*reasons)
    if attempt.outcome == SendAttemptOutcome.DEFINITE_REJECTION:
        return deny("DEFINITE_REJECTION")
    return deny("NOT_RETRYABLE")


class InterruptedSendOutcome(BaseModel):
    """What a crashed/expired ``sending`` attempt becomes. The reaper never requeues it."""

    model_config = _FROZEN

    inquiry_state: Literal[InquiryState.UNCERTAIN] = InquiryState.UNCERTAIN
    job_state: Literal[JobState.BLOCKED] = JobState.BLOCKED
    job_blocked_reason: str = EMAIL_DELIVERY_UNCERTAIN
    suppression_reason: Literal[SuppressionReason.UNRESOLVED_SEND_OUTCOME] = (
        SuppressionReason.UNRESOLVED_SEND_OUTCOME
    )
    retain_reservation: Literal[True] = True
    retain_quota_debit: Literal[True] = True


def on_sending_interrupted(state: InquiryState) -> InterruptedSendOutcome:
    if state != InquiryState.SENDING:
        raise ValidationFailed("only a sending inquiry can be interrupted", details={"from": state.value})
    return InterruptedSendOutcome()


class ReconciliationEvidence(BaseModel):
    """Search results for an uncertain send (configured account/provider only)."""

    model_config = _FROZEN

    sent_items: Literal["not_searched", "found", "not_found"] = "not_searched"
    provider_search: Literal["not_searched", "found", "not_found", "unsupported"] = "not_searched"
    outbox_pending: Tristate = Tristate.UNKNOWN  # local Outlook Outbox still holds the item
    proven_not_submitted: PreSubmissionProof | None = None
    worker_alive: Tristate = Tristate.UNKNOWN


class ReconcileDecision(BaseModel):
    model_config = _FROZEN

    next_state: InquiryState | None  # None = stay uncertain
    release_reservation: Literal[False] = False
    reasons: tuple[str, ...]


def reconcile_uncertain(evidence: ReconciliationEvidence) -> ReconcileDecision:
    """Resolve ``uncertain`` only on positive evidence; an empty search never releases anything."""
    if evidence.sent_items == "found" or evidence.provider_search == "found":
        return ReconcileDecision(next_state=InquiryState.ACCEPTED, reasons=("SUBMISSION_FOUND",))
    if (
        evidence.proven_not_submitted is not None
        and evidence.worker_alive == Tristate.NO
        and evidence.outbox_pending == Tristate.NO
    ):
        return ReconcileDecision(next_state=InquiryState.FAILED_DEFINITE, reasons=("PROVEN_NOT_SUBMITTED",))
    reasons = ["STILL_UNCERTAIN"]
    if evidence.sent_items == "not_found" or evidence.provider_search == "not_found":
        reasons.append("EMPTY_SEARCH_IS_NOT_PROOF")
    if evidence.outbox_pending != Tristate.NO:
        reasons.append("OUTBOX_MAY_STILL_SUBMIT")
    return ReconcileDecision(next_state=None, reasons=tuple(reasons))


class TransitionContext(BaseModel):
    """Evidence required by guarded edges of the state machine."""

    model_config = _FROZEN

    transmission_attempts: int = Field(default=0, ge=0)
    suppression_removal_audit_id: UUID | None = None
    attempt: SendAttemptEvidence | None = None
    retry: RetryDecision | None = None
    reconciliation: ReconcileDecision | None = None


def require_transition(
    current: InquiryState, target: InquiryState, context: TransitionContext | None = None
) -> None:
    """Raise ``ValidationFailed`` unless the edge exists and its guard evidence is present."""
    ctx = context or TransitionContext()

    def refuse(problem: str) -> ValidationFailed:
        return ValidationFailed(
            f"inquiry state cannot change from {current.value} to {target.value}",
            details={"from": current.value, "to": target.value, "problem": problem},
        )

    if not can_transition(current, target):
        raise refuse("TRANSITION_NOT_ALLOWED")
    if current in {InquiryState.CANCELLED, InquiryState.SUPPRESSED} and ctx.transmission_attempts > 0:
        raise refuse("ALREADY_ATTEMPTED")
    if current == InquiryState.SUPPRESSED and ctx.suppression_removal_audit_id is None:
        raise refuse("SUPPRESSION_REMOVAL_AUDIT_REQUIRED")
    if current == InquiryState.SENDING:
        outcome = ctx.attempt.outcome if ctx.attempt else None
        if target == InquiryState.ACCEPTED and outcome != SendAttemptOutcome.ACCEPTED:
            raise refuse("ACCEPTANCE_EVIDENCE_REQUIRED")
        if target == InquiryState.FAILED_DEFINITE and not (
            (
                outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE
                and ctx.attempt
                and ctx.attempt.pre_submission_proof
            )
            or outcome == SendAttemptOutcome.DEFINITE_REJECTION
        ):
            raise refuse("DEFINITE_FAILURE_EVIDENCE_REQUIRED")
    if current == InquiryState.UNCERTAIN and (
        ctx.reconciliation is None or ctx.reconciliation.next_state != target
    ):
        raise refuse("RECONCILIATION_EVIDENCE_REQUIRED")
    if current == InquiryState.FAILED_DEFINITE and (ctx.retry is None or not ctx.retry.retry):
        raise refuse("RETRY_POLICY_REQUIRED")


def releases_quota(current: InquiryState, target: InquiryState) -> bool:
    """Only a never-transmitted reservation that is cancelled/suppressed gives its debit back."""
    return current in {InquiryState.RESERVED, InquiryState.QUEUED} and target in {
        InquiryState.CANCELLED,
        InquiryState.SUPPRESSED,
    }


# =============================================================================================
# Immutable binding
# =============================================================================================


class SenderBinding(BaseModel):
    model_config = _FROZEN

    binding_id: UUID
    binding_version: int = Field(ge=1)
    provider: EmailProviderKind
    account_id: str
    from_address: str
    display_name: str
    reply_to_address: str | None = None


class ListingFactsSnapshot(BaseModel):
    """The listing facts an inquiry qualified on (compared again before dispatch)."""

    model_config = _FROZEN

    listing_id: UUID
    listing_incarnation_id: UUID | None = None
    revision_id: UUID | None = None
    revision_number: int = Field(ge=0)
    semantic_hash: str
    price_amount_minor: int | None = Field(default=None, ge=0)
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    availability: Availability


class InquiryBinding(BaseModel):
    """Immutable once reserved: scope/template/body hash plus exact sender and recipient."""

    model_config = _FROZEN

    inquiry_id: UUID
    identity_key: str
    vehicle_key: str
    authorization_version: int
    authorization_fingerprint: str
    template_id: str
    template_version: int
    template_hash: str
    language: MessageLanguage
    scope_hash: str
    body_hash: str
    sender: SenderBinding
    recipient: RecipientBinding
    qualified_listing: ListingFactsSnapshot
    readiness_rationale_hash: str

    def binding_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


def bind_inquiry(
    *,
    at: datetime,
    inquiry_id: UUID,
    identity: InquiryIdentity,
    authorization: SellerInquiryAuthorization,
    readiness: InquiryReadinessDecision,
    language: LanguageDecision,
    message: RenderedMessage,
    sender: SenderStatus,
    recipient: RecipientDecision,
    listing: ListingFactsSnapshot,
) -> InquiryBinding:
    """Create the binding stored at reservation. Every cross-reference must agree exactly."""
    problems: list[str] = []
    if readiness.readiness != InquiryReadiness.INQUIRY_READY:
        problems.append("NOT_INQUIRY_READY")
    if not language.resolved or language.language is None:
        problems.append("LANGUAGE_NOT_RESOLVED")
    if message.kind != "seller_inquiry":
        problems.append("NOT_A_SELLER_MESSAGE")
    if language.language is not None and message.language != language.language.value:
        problems.append("MESSAGE_LANGUAGE_MISMATCH")
    if message.scope_hash != SCOPE_HASH or not validate_scope(message).ok:
        problems.append("SCOPE_VALIDATION_FAILED")
    if sender.identity_problems():
        problems.append("SENDER_NOT_USABLE")
    if sender.display_name != message.placeholders.sender_display_name:
        problems.append("SENDER_DISPLAY_NAME_MISMATCH")
    if recipient.binding is None:
        problems.append("RECIPIENT_NOT_VERIFIED")
    else:
        if recipient.binding.listing_url != message.placeholders.listing_url:
            problems.append("LISTING_URL_MISMATCH")
        if recipient.binding.listing_reference != message.placeholders.listing_reference:
            problems.append("LISTING_REFERENCE_MISMATCH")
        if recipient.binding.listing_id != listing.listing_id:
            problems.append("RECIPIENT_LISTING_MISMATCH")
        if recipient.binding.seller_identity_key != identity.seller_key:
            problems.append("RECIPIENT_SELLER_MISMATCH")
    if authorization.problems_at(at):
        problems.append("AUTHORIZATION_INACTIVE")
    if problems or recipient.binding is None or language.language is None:
        raise ValidationFailed("inquiry binding refused", details={"problems": sorted(set(problems))})
    assert sender.binding_id is not None and sender.binding_version is not None  # identity_problems checked
    assert sender.provider is not None and sender.account_id and sender.from_address and sender.display_name
    return InquiryBinding(
        inquiry_id=inquiry_id,
        identity_key=identity.key(),
        vehicle_key=identity.vehicle.key(),
        authorization_version=authorization.version,
        authorization_fingerprint=authorization.fingerprint(),
        template_id=message.template_id,
        template_version=message.template_version,
        template_hash=message.template_hash,
        language=language.language,
        scope_hash=message.scope_hash,
        body_hash=message.body_hash,
        sender=SenderBinding(
            binding_id=sender.binding_id,
            binding_version=sender.binding_version,
            provider=sender.provider,
            account_id=sender.account_id,
            from_address=canonicalize_address(sender.from_address).canonical,
            display_name=sender.display_name,
            reply_to_address=canonicalize_address(sender.reply_to_address).canonical
            if sender.reply_to_address
            else None,
        ),
        recipient=recipient.binding,
        qualified_listing=listing,
        readiness_rationale_hash=readiness.rationale_hash,
    )


def apply_binding(
    state: InquiryState, existing: InquiryBinding | None, proposed: InquiryBinding
) -> InquiryBinding:
    """Binding may be (re)set only before reservation; afterwards it is immutable."""
    if state in PRE_RESERVATION_STATES:
        return proposed
    if existing is None:
        raise ValidationFailed(
            "a reserved inquiry must already carry its binding", details={"state": state.value}
        )
    if existing.binding_hash() != proposed.binding_hash():
        raise IdempotencyConflict("the inquiry binding is immutable once reserved")
    return existing


# =============================================================================================
# Dispatch preflight (immediately before transmission)
# =============================================================================================


class PreflightOutcome(StrEnum):
    PROCEED = "proceed"
    CANCEL_STALE = "cancel_stale"  # leave the queue: cancelled (requalify) or suppressed
    HOLD = "hold"  # keep queued, do not transmit now


class DispatchFacts(BaseModel):
    """Current facts re-read under lock immediately before a send attempt is recorded."""

    model_config = _FROZEN

    now: datetime
    state: InquiryState
    binding: InquiryBinding
    authorization: SellerInquiryAuthorization
    workspace_id: UUID
    current_listing: ListingFactsSnapshot
    source: SourceObservationFacts
    sender: SenderStatus
    recipient_recheck: ContactRecheck
    current_language: LanguageDecision | None = None
    suppressions: tuple[SuppressionRecord, ...] = ()
    rate_caps: RateCapDecision  # evaluated excluding this inquiry's own debit
    quota_debit_present: bool
    attempts: tuple[SendAttemptEvidence, ...] = ()
    message: RenderedMessage
    envelope: MessageEnvelope
    message_approval_required: bool = False  # only ever requires_message_approval(settings)
    message_approval_recorded: bool = False

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class PreflightDecision(BaseModel):
    model_config = _FROZEN

    outcome: PreflightOutcome
    target_state: InquiryState | None = None  # for cancel_stale: cancelled or suppressed
    suppression_reason: SuppressionReason | None = None
    reasons: tuple[str, ...] = ()
    next_attempt_at: datetime | None = None


def _sender_changed(bound: SenderBinding, current: SenderStatus) -> bool:
    try:
        from_now = canonicalize_address(current.from_address).canonical if current.from_address else None
        reply_now = (
            canonicalize_address(current.reply_to_address).canonical if current.reply_to_address else None
        )
    except AddressError:
        return True
    return (
        current.binding_id != bound.binding_id
        or current.binding_version != bound.binding_version
        or current.provider != bound.provider
        or current.account_id != bound.account_id
        or from_now != bound.from_address
        or current.display_name != bound.display_name
        or reply_now != bound.reply_to_address
    )


def dispatch_preflight(facts: DispatchFacts) -> PreflightDecision:
    """Revalidate everything immediately before transmission (spec 37.5).

    Precedence: suppression (kill switch, source pause, revoked sender, matching suppression
    records, revoked authorization) > cancellation of stale work (listing revision/price/
    availability, sender or recipient change, language change, binding/scope mismatch) > hold
    (mode not automatic, running prior attempt, stale recipient evidence, sender health, quota,
    owner-configured approval). Only ``proceed`` may lead to a send attempt.
    """
    b = facts.binding
    now = facts.now
    suppress: list[tuple[SuppressionReason, str]] = []
    cancel: list[str] = []
    hold: list[str] = []
    next_at: datetime | None = None

    if facts.state != InquiryState.QUEUED:
        return PreflightDecision(outcome=PreflightOutcome.HOLD, reasons=("NOT_QUEUED",))

    # -- suppression ------------------------------------------------------------------------
    auth_problems = facts.authorization.problems_at(now)
    if "AUTHORIZATION_REVOKED" in auth_problems:
        suppress.append((SuppressionReason.KILL_SWITCH, "AUTHORIZATION_REVOKED"))
    if facts.sender.kill_switch:
        suppress.append((SuppressionReason.KILL_SWITCH, "KILL_SWITCH_ACTIVE"))
    src = facts.source
    if src.source_paused or not src.source_enabled or src.terms_blocked or src.access_blocked:
        suppress.append((SuppressionReason.SOURCE_PAUSED, "SOURCE_NOT_ACTIVE"))
    if facts.sender.credentials_revoked:
        suppress.append((SuppressionReason.SENDER_REVOKED, "SENDER_REVOKED"))
    targets = SuppressionTargets(
        workspace_id=facts.workspace_id,
        seller_key=b.recipient.seller_identity_key,
        canonical_address=b.recipient.canonical_address,
        vehicle_key=b.vehicle_key,
        source_key=b.recipient.source_key,
        sender_binding_id=b.sender.binding_id,
    )
    for record in matching_suppressions(facts.suppressions, targets, at=now):
        suppress.append((record.reason, f"SUPPRESSED_{record.reason.value.upper()}"))

    # -- stale work ---------------------------------------------------------------------------
    if (
        "AUTHORIZATION_NOT_EFFECTIVE" in auth_problems
        or facts.authorization.version != b.authorization_version
    ):
        cancel.append("AUTHORIZATION_CHANGED")
    q, cur = b.qualified_listing, facts.current_listing
    if (cur.listing_id, cur.listing_incarnation_id) != (q.listing_id, q.listing_incarnation_id):
        cancel.append("LISTING_IDENTITY_CHANGED")
    if (cur.price_amount_minor, cur.price_currency) != (q.price_amount_minor, q.price_currency):
        cancel.append("PRICE_CHANGED")
    if cur.availability != q.availability:
        cancel.append("AVAILABILITY_CHANGED")
    if cur.availability in _UNAVAILABLE or cur.availability == Availability.RESERVED:
        cancel.append("VEHICLE_UNAVAILABLE")
    if src.availability in _UNAVAILABLE:
        cancel.append("VEHICLE_UNAVAILABLE")
    if cur.revision_number != q.revision_number or cur.semantic_hash != q.semantic_hash:
        cancel.append("LISTING_REVISION_CHANGED")
    if _sender_changed(b.sender, facts.sender):
        cancel.append("SENDER_CHANGED")
    if facts.recipient_recheck.material_change:
        cancel.append("RECIPIENT_CHANGED")
    lang = facts.current_language
    if lang is not None and (not lang.resolved or lang.language != b.language):
        cancel.append("LANGUAGE_CHANGED")
    msg = facts.message
    if (
        msg.body_hash != b.body_hash
        or msg.template_hash != b.template_hash
        or msg.template_id != b.template_id
        or msg.scope_hash != b.scope_hash
        or msg.placeholders.listing_url != b.recipient.listing_url
    ):
        cancel.append("MESSAGE_BINDING_MISMATCH")
    scope = validate_scope(msg, envelope=facts.envelope)
    if not scope.ok:
        cancel.append("SCOPE_VALIDATION_FAILED")
    try:
        to = [canonicalize_address(a).canonical for a in facts.envelope.to]
        reply_to = [canonicalize_address(a).canonical for a in facts.envelope.reply_to]
    except AddressError:
        to, reply_to = [], ["<invalid>"]
    if to != [b.recipient.canonical_address]:
        cancel.append("RECIPIENT_BINDING_MISMATCH")
    if reply_to and reply_to != [b.sender.reply_to_address]:
        cancel.append("REPLY_TO_BINDING_MISMATCH")

    # -- hold -----------------------------------------------------------------------------------
    if facts.sender.mode != "automatic":
        hold.append("MODE_NOT_AUTOMATIC")
    if any(a.may_still_run(now) for a in facts.attempts):
        hold.append("PRIOR_ATTEMPT_MAY_STILL_RUN")
    if any(a.found_submitted or a.outcome == SendAttemptOutcome.UNCERTAIN for a in facts.attempts):
        hold.append("UNRESOLVED_PRIOR_ATTEMPT")
    if facts.recipient_recheck.recheck_required and not facts.recipient_recheck.material_change:
        hold.append("RECIPIENT_RECHECK_REQUIRED")
    sender_problems = set(facts.sender.identity_problems()) - {"SENDER_REVOKED"}
    if sender_problems:
        hold.append("SENDER_NOT_USABLE")
    if not facts.quota_debit_present:
        hold.append("QUOTA_DEBIT_MISSING")
    if not facts.rate_caps.allowed:
        hold.append("RATE_CAP_REACHED")
        next_at = facts.rate_caps.next_allowed_at
    if facts.message_approval_required and not facts.message_approval_recorded:
        hold.append("OWNER_CONFIGURED_MESSAGE_APPROVAL")

    if suppress:
        reasons = tuple(dict.fromkeys([code for _, code in suppress] + cancel + hold))
        return PreflightDecision(
            outcome=PreflightOutcome.CANCEL_STALE,
            target_state=InquiryState.SUPPRESSED,
            suppression_reason=suppress[0][0],
            reasons=reasons,
        )
    if cancel:
        return PreflightDecision(
            outcome=PreflightOutcome.CANCEL_STALE,
            target_state=InquiryState.CANCELLED,
            reasons=tuple(dict.fromkeys(cancel + hold)),
        )
    if hold:
        return PreflightDecision(
            outcome=PreflightOutcome.HOLD, reasons=tuple(dict.fromkeys(hold)), next_attempt_at=next_at
        )
    return PreflightDecision(outcome=PreflightOutcome.PROCEED, reasons=("ALL_CHECKS_PASSED",))
