"""Review case state machine, claims, submissions and idempotency (spec sections 14, 18, 21).

Pure domain rules. The persistence layer loads a ``ReviewCaseSnapshot`` with ``FOR UPDATE``,
calls one ``evaluate_*`` function inside the same short transaction, applies the returned
update with ``WHERE row_version = expected_row_version`` and appends history/outbox rows.
Nothing here performs I/O.

States (spec 14): ``pending``, ``claimed``, ``needs_information``, ``watch``, ``shortlisted``,
``rejected``, ``superseded``. ``ALLOWED_TRANSITIONS`` is the complete transition table.

Claims (spec 21 "Claim and optimistic concurrency"):

- A claim is an application record independent of MCP connections. The token is random
  (``secrets.token_urlsafe(32)``: 256 bits), returned once, and only its SHA-256 is stored.
  Comparisons are constant time.
- Claimable states: pending, needs_information, watch, shortlisted, plus ``claimed`` whose claim
  has expired (an expired claim is released implicitly). ``rejected`` and ``superseded`` are
  closed; a new material revision reopens a rejected case as pending.
- Another reviewer with an unexpired claim -> ``ALREADY_CLAIMED`` (never a silent override).
  ``expected_version`` mismatch -> ``VERSION_CONFLICT``. Every claim, release, submission,
  revision update and supersede increments ``row_version``.
- Same-holder re-claim while the claim is active: allowed when ``expected_version`` is current;
  it **rotates** the token (the old one stops working) and extends the expiry. An exact retry
  of the same request is answered by the idempotency record instead (same key and hash -> the
  original result; the stored claim result never contains the plaintext token, see
  ``ClaimGrant.redacted_result``).
- Release: only the caller's current claim, idempotently. Not claimed / held by someone else ->
  no-op. Held by the caller but presented with a stale token -> ``CLAIM_EXPIRED``.

Submission (``evaluate_submit``) checks, in this order: scope, workspace, case id, superseded,
claim ownership (``ALREADY_CLAIMED`` if another holder is active, else ``CLAIM_EXPIRED``),
claim expiry, expected case version, listing revision, valuation applicability, outcome rules
(shortlist needs a current valuation and passes the eligibility/availability/freshness/
fingerprint guard of spec 18). The decision records the authenticated actor from
``ActorContext`` only; request bodies have no actor fields (``extra="forbid"``), so caller text
cannot impersonate an owner. The summary is a concise rationale and evidence trail, never
hidden chain-of-thought.

A new material revision during review (``apply_new_revision``) creates a new case version,
clears the (now stale) valuation reference and returns decided cases to pending; previous
decisions remain in ``app.review_decisions`` history.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext, PrincipalKind
from suv_deals.domain.enums import (
    Availability,
    EligibilityState,
    ProfileKey,
    ReviewOutcome,
    ReviewState,
    Role,
    Scope,
    ValuationState,
)
from suv_deals.domain.listings import sha256_json
from suv_deals.errors import (
    AlreadyClaimed,
    ClaimExpired,
    IdempotencyConflict,
    NotFound,
    ValidationFailed,
    VersionConflict,
)

REVIEW_RULES_VERSION: Final = "reviews@1.0.0"
CLAIM_TOKEN_BYTES: Final = 32
MIN_CLAIM_DURATION: Final = timedelta(seconds=60)
MAX_CLAIM_DURATION: Final = timedelta(seconds=3600)
#: PROPOSED default claim duration (spec 21); configurable via BusinessConfig.claim_duration_seconds.
DEFAULT_CLAIM_DURATION: Final = timedelta(seconds=300)

_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_REASON_CODE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
# C0 controls except tab/newline/CR, DEL, zero-width and bidi override/isolate characters.
_CONTROL_RE: Final = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)
_HIDDEN_REASONING_RE: Final = re.compile(
    r"</?\s*(thinking|scratchpad|reasoning|inner_monologue|chain[_ -]?of[_ -]?thought)\b", re.IGNORECASE
)
_FROZEN = ConfigDict(frozen=True, extra="forbid")

ALLOWED_TRANSITIONS: Final[dict[ReviewState, frozenset[ReviewState]]] = {
    ReviewState.PENDING: frozenset({ReviewState.CLAIMED, ReviewState.SUPERSEDED}),
    ReviewState.CLAIMED: frozenset(
        {
            ReviewState.PENDING,  # release/expiry without a still-valid prior decision
            ReviewState.CLAIMED,  # same-holder rotation or takeover of an expired claim
            ReviewState.NEEDS_INFORMATION,
            ReviewState.WATCH,
            ReviewState.SHORTLISTED,
            ReviewState.REJECTED,
            ReviewState.SUPERSEDED,
        }
    ),
    ReviewState.NEEDS_INFORMATION: frozenset(
        {ReviewState.CLAIMED, ReviewState.PENDING, ReviewState.SUPERSEDED}
    ),
    ReviewState.WATCH: frozenset({ReviewState.CLAIMED, ReviewState.PENDING, ReviewState.SUPERSEDED}),
    ReviewState.SHORTLISTED: frozenset({ReviewState.CLAIMED, ReviewState.PENDING, ReviewState.SUPERSEDED}),
    ReviewState.REJECTED: frozenset({ReviewState.PENDING, ReviewState.SUPERSEDED}),
    ReviewState.SUPERSEDED: frozenset(),
}
CLAIMABLE_STATES: Final = frozenset(
    {ReviewState.PENDING, ReviewState.NEEDS_INFORMATION, ReviewState.WATCH, ReviewState.SHORTLISTED}
)
DECIDED_STATES: Final = frozenset(
    {ReviewState.NEEDS_INFORMATION, ReviewState.WATCH, ReviewState.SHORTLISTED, ReviewState.REJECTED}
)
_OUTCOME_STATE: Final[dict[ReviewOutcome, ReviewState]] = {
    ReviewOutcome.NEEDS_INFORMATION: ReviewState.NEEDS_INFORMATION,
    ReviewOutcome.WATCH: ReviewState.WATCH,
    ReviewOutcome.SHORTLISTED: ReviewState.SHORTLISTED,
    ReviewOutcome.REJECTED: ReviewState.REJECTED,
}
_SHORTLIST_VALUATION_STATES: Final = frozenset(
    {ValuationState.INCOMPLETE, ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED}
)
_UNUSABLE_VALUATION_STATES: Final = frozenset({ValuationState.STALE, ValuationState.INVALID})


def can_transition(current: ReviewState, target: ReviewState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def require_transition(current: ReviewState, target: ReviewState) -> None:
    if not can_transition(current, target):
        raise ValidationFailed(
            f"review state cannot change from {current.value} to {target.value}",
            details={"from": current.value, "to": target.value},
        )


def outcome_state(outcome: ReviewOutcome) -> ReviewState:
    return _OUTCOME_STATE[outcome]


# ---------------------------------------------------------------------------------------------
# Claim tokens
# ---------------------------------------------------------------------------------------------


def new_claim_token() -> str:
    """A fresh random opaque claim handle (256 bits, URL-safe base64, 43 characters)."""
    return secrets.token_urlsafe(CLAIM_TOKEN_BYTES)


def is_well_formed_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def hash_claim_token(token: str) -> str:
    """SHA-256 hex of the token; the only form ever stored (``review_cases.claim_token_hash``)."""
    if not is_well_formed_token(token):
        raise ValidationFailed("malformed claim token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def verify_claim_token(token: object, stored_hash: str | None) -> bool:
    """Constant-time comparison of a presented token against the stored hash."""
    if stored_hash is None or not is_well_formed_token(token) or _HASH_RE.fullmatch(stored_hash) is None:
        return False
    assert isinstance(token, str)
    presented = hashlib.sha256(token.encode("ascii")).hexdigest()
    return hmac.compare_digest(presented.encode("ascii"), stored_hash.encode("ascii"))


# ---------------------------------------------------------------------------------------------
# Snapshot and results
# ---------------------------------------------------------------------------------------------


class ReviewCaseSnapshot(BaseModel):
    """The committed state of one ``app.review_cases`` row, read under ``FOR UPDATE``.

    The validator mirrors the database invariants so an inconsistent row is never evaluated.
    ``latest_decision_listing_revision`` lets release/expiry restore the last decided state only
    while that decision still refers to the current listing revision.
    """

    model_config = _FROZEN

    case_id: UUID
    workspace_id: UUID
    listing_id: UUID
    profile_key: ProfileKey
    state: ReviewState
    row_version: int = Field(ge=1)
    revision_id: UUID
    listing_revision: int = Field(ge=1)
    valuation_id: UUID | None = None
    valuation_state: ValuationState | None = None
    claim_holder: UUID | None = None
    claim_token_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    latest_decision_id: UUID | None = None
    latest_decision_outcome: ReviewOutcome | None = None
    latest_decision_listing_revision: int | None = Field(default=None, ge=1)
    superseded_by_id: UUID | None = None
    is_fixture: bool = False

    @field_validator("claimed_at", "claim_expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> ReviewCaseSnapshot:
        claim = (self.claim_holder, self.claim_token_hash, self.claim_expires_at)
        if self.state == ReviewState.CLAIMED:
            if any(v is None for v in claim):
                raise ValueError("a claimed case needs holder, token hash and expiry")
        elif any(v is not None for v in (*claim, self.claimed_at)):
            raise ValueError("a non-claimed case carries no claim data")
        if self.claimed_at and self.claim_expires_at and self.claim_expires_at <= self.claimed_at:
            raise ValueError("claim expiry must be after the claim time")
        if self.state in DECIDED_STATES and self.latest_decision_id is None:
            raise ValueError("a decided case references its decision")
        if (self.latest_decision_id is None) != (self.latest_decision_outcome is None):
            raise ValueError("latest decision id and outcome go together")
        if self.superseded_by_id is not None and self.state != ReviewState.SUPERSEDED:
            raise ValueError("only a superseded case names its successor")
        if self.valuation_id is None and self.valuation_state is not None:
            raise ValueError("valuation_state needs a valuation_id")
        return self

    def claim_active(self, now: datetime) -> bool:
        return (
            self.state == ReviewState.CLAIMED
            and self.claim_expires_at is not None
            and _aware(now) < self.claim_expires_at
        )

    def restore_state(self) -> ReviewState:
        """State after a claim ends without a decision: the still-valid prior decision, else pending."""
        if (
            self.latest_decision_outcome is not None
            and self.latest_decision_listing_revision == self.listing_revision
        ):
            return _OUTCOME_STATE[self.latest_decision_outcome]
        return ReviewState.PENDING


class ClaimGrant(BaseModel):
    """Result of a successful claim. ``claim_token`` is shown once and never stored."""

    model_config = _FROZEN

    case_id: UUID
    claim_token: SecretStr
    claim_token_hash: str
    holder: UUID
    claimed_at: datetime
    expires_at: datetime
    expected_row_version: int
    row_version: int
    listing_id: UUID
    revision_id: UUID
    listing_revision: int
    valuation_id: UUID | None
    rotated: bool = False
    took_over_expired: bool = False

    def public_result(self) -> dict[str, Any]:
        """Wire result for the caller (contains the plaintext token exactly once)."""
        return {**self.redacted_result(), "claim_token": self.claim_token.get_secret_value()}

    def redacted_result(self) -> dict[str, Any]:
        """Result safe to persist in ``ops.idempotency_records`` (no plaintext token)."""
        return {
            "case_id": str(self.case_id),
            "claim_token": None,
            "claim_token_redacted": True,
            "expires_at": _rfc3339(self.expires_at),
            "case_version": self.row_version,
            "listing_id": str(self.listing_id),
            "revision_id": str(self.revision_id),
            "listing_revision": self.listing_revision,
            "valuation_id": None if self.valuation_id is None else str(self.valuation_id),
        }


class ReleaseResult(BaseModel):
    model_config = _FROZEN

    case_id: UUID
    changed: bool
    reason: Literal["released", "expired", "not_claimed", "not_held"]
    new_state: ReviewState
    expected_row_version: int
    row_version: int


class CaseUpdate(BaseModel):
    """A non-claim change to a case (new revision, supersede)."""

    model_config = _FROZEN

    case_id: UUID
    expected_row_version: int
    row_version: int
    state: ReviewState
    revision_id: UUID
    listing_revision: int
    valuation_id: UUID | None
    valuation_state: ValuationState | None
    clear_claim: bool
    superseded_by_id: UUID | None = None
    reason: str = Field(max_length=2000)


class SubmitRequest(BaseModel):
    """``reviews_submit`` input (spec 21 schema map). There are deliberately no actor fields."""

    model_config = _FROZEN

    case_id: UUID
    claim_token: str = Field(min_length=20, max_length=256, repr=False)
    expected_version: int = Field(ge=1)
    listing_revision: int = Field(ge=1)
    valuation_id: UUID | None = None
    outcome: ReviewOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=10, max_length=4000)
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    missing_information: tuple[str, ...] = Field(default=(), max_length=30)
    model_run_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("reason_codes")
    @classmethod
    def _codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for code in value:
            if not _REASON_CODE_RE.fullmatch(code):
                raise ValueError("reason codes are short identifiers (letters, digits, _ . : -; max 80)")
        if len(set(value)) != len(value):
            raise ValueError("duplicate reason codes")
        return value

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        if _CONTROL_RE.search(value):
            raise ValueError("summary contains control characters")
        if _HIDDEN_REASONING_RE.search(value):
            raise ValueError("summary must be a concise rationale, not hidden reasoning transcripts")
        stripped = value.strip()
        if len(stripped) < 10:
            raise ValueError("summary is too short")
        return stripped

    @field_validator("missing_information")
    @classmethod
    def _missing(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for item in value:
            if len(item) > 300 or _CONTROL_RE.search(item):
                raise ValueError("missing_information items are plain text of at most 300 characters")
            if item.strip():
                cleaned.append(item.strip())
        return tuple(cleaned)

    @field_validator("evidence_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate evidence ids")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return validate_idempotency_key(value)

    @field_validator("model_run_id")
    @classmethod
    def _run_id(cls, value: str | None) -> str | None:
        if value is not None and _CONTROL_RE.search(value):
            raise ValueError("model_run_id contains control characters")
        return value


def parse_submit_request(data: Mapping[str, Any]) -> SubmitRequest:
    """Validate a raw ``reviews_submit`` payload, mapping schema errors to ``VALIDATION_ERROR``."""
    try:
        return SubmitRequest.model_validate(dict(data))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "request" for err in exc.errors()})
        raise ValidationFailed("invalid reviews_submit request", details={"fields": fields}) from None


class SubmitGuard(BaseModel):
    """Committed facts re-read immediately before a decision (spec 18 pre-shortlist validation)."""

    model_config = _FROZEN

    eligibility: EligibilityState | None
    availability: Availability
    freshness_ok: bool
    valuation_fingerprint_current: bool | None = None


class SubmitDecision(BaseModel):
    """Everything the transaction appends (``app.review_decisions``) and changes on the case."""

    model_config = _FROZEN

    rules_version: str = REVIEW_RULES_VERSION
    case_id: UUID
    case_version: int  # the version the decision was made against
    expected_row_version: int
    row_version: int
    new_state: ReviewState
    listing_id: UUID
    listing_revision_id: UUID
    listing_revision: int
    valuation_id: UUID | None
    actor_principal_id: UUID
    actor_kind: PrincipalKind
    actor_role: Role
    outcome: ReviewOutcome
    reason_codes: tuple[str, ...]
    summary: str
    evidence_ids: tuple[UUID, ...]
    missing_information: tuple[str, ...]
    model_name: str | None
    model_version: str | None
    model_run_id: str | None
    prompt_template_version: str | None
    tool_request_id: str
    input_hash: str
    decided_at: datetime
    supersedes_decision_id: UUID | None
    clear_claim: Literal[True] = True
    notification_candidate: bool
    is_fixture: bool


# ---------------------------------------------------------------------------------------------
# Claim / release / expiry
# ---------------------------------------------------------------------------------------------


def evaluate_claim(
    case: ReviewCaseSnapshot,
    actor: ActorContext,
    *,
    expected_version: int,
    now: datetime,
    duration: timedelta = DEFAULT_CLAIM_DURATION,
    token_factory: Callable[[], str] = new_claim_token,
) -> ClaimGrant:
    """Decide a ``reviews_claim`` request (see module docstring for the full rule set)."""
    actor.require(Scope.REVIEWS_WRITE)
    _same_workspace(case, actor)
    now = _aware(now)
    if not MIN_CLAIM_DURATION <= duration <= MAX_CLAIM_DURATION:
        raise ValidationFailed("claim duration must be between 60 seconds and 1 hour")
    if case.state == ReviewState.SUPERSEDED:
        raise _superseded(case)
    rotated = took_over = False
    if case.claim_active(now):
        if case.claim_holder != actor.principal_id:
            raise AlreadyClaimed()
        rotated = True
    elif case.state == ReviewState.CLAIMED:
        took_over = True  # an expired claim is released implicitly
    elif case.state not in CLAIMABLE_STATES:
        raise ValidationFailed(
            f"a {case.state.value} case is closed for review claims", details={"state": case.state.value}
        )
    if expected_version != case.row_version:
        raise VersionConflict(expected_version=expected_version, current_version=case.row_version)
    token = token_factory()
    token_hash = hash_claim_token(token)
    return ClaimGrant(
        case_id=case.case_id,
        claim_token=SecretStr(token),
        claim_token_hash=token_hash,
        holder=actor.principal_id,
        claimed_at=now,
        expires_at=now + duration,
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
        listing_id=case.listing_id,
        revision_id=case.revision_id,
        listing_revision=case.listing_revision,
        valuation_id=case.valuation_id,
        rotated=rotated,
        took_over_expired=took_over,
    )


def evaluate_release(
    case: ReviewCaseSnapshot, actor: ActorContext, claim_token: str, now: datetime
) -> ReleaseResult:
    """Release only the caller's current claim. Idempotent: nothing to release is a no-op."""
    actor.require(Scope.REVIEWS_WRITE)
    _same_workspace(case, actor)
    _aware(now)
    if case.state != ReviewState.CLAIMED:
        return _no_change(case, "not_claimed")
    if case.claim_holder != actor.principal_id:
        return _no_change(case, "not_held")
    if not verify_claim_token(claim_token, case.claim_token_hash):
        raise ClaimExpired("The claim token is not the current handle for this case")
    return ReleaseResult(
        case_id=case.case_id,
        changed=True,
        reason="released",
        new_state=case.restore_state(),
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
    )


def expire_claim(case: ReviewCaseSnapshot, now: datetime) -> ReleaseResult | None:
    """Reaper rule: an expired claim returns the case to its restore state. ``None`` if not expired."""
    now = _aware(now)
    if case.state != ReviewState.CLAIMED or case.claim_active(now):
        return None
    return ReleaseResult(
        case_id=case.case_id,
        changed=True,
        reason="expired",
        new_state=case.restore_state(),
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
    )


def _no_change(case: ReviewCaseSnapshot, reason: Literal["not_claimed", "not_held"]) -> ReleaseResult:
    return ReleaseResult(
        case_id=case.case_id,
        changed=False,
        reason=reason,
        new_state=case.state,
        expected_row_version=case.row_version,
        row_version=case.row_version,
    )


# ---------------------------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------------------------


def evaluate_submit(
    case: ReviewCaseSnapshot,
    actor: ActorContext,
    request: SubmitRequest,
    *,
    now: datetime,
    guard: SubmitGuard | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    prompt_template_version: str | None = None,
) -> SubmitDecision:
    """Validate a ``reviews_submit`` against the committed case inside the decision transaction.

    Raises ``Forbidden``, ``NotFound`` (foreign workspace), ``ValidationFailed``,
    ``AlreadyClaimed``, ``ClaimExpired`` or ``VersionConflict``. ``model_name``/``version`` and
    ``prompt_template_version`` come from authenticated server context, never the request body.
    """
    actor.require(Scope.REVIEWS_WRITE)
    _same_workspace(case, actor)
    now = _aware(now)
    if request.case_id != case.case_id:
        raise ValidationFailed("case_id does not match the loaded case")
    if case.state == ReviewState.SUPERSEDED:
        raise _superseded(case)
    if case.state != ReviewState.CLAIMED or case.claim_holder != actor.principal_id:
        if case.claim_active(now):
            raise AlreadyClaimed()
        raise ClaimExpired()
    if not verify_claim_token(request.claim_token, case.claim_token_hash):
        raise ClaimExpired("The claim token is not the current handle for this case")
    if not case.claim_active(now):
        raise ClaimExpired()
    if request.expected_version != case.row_version:
        raise VersionConflict(expected_version=request.expected_version, current_version=case.row_version)
    if request.listing_revision != case.listing_revision:
        raise VersionConflict(
            "The listing has a newer revision; reload before deciding",
            expected_listing_revision=request.listing_revision,
            current_listing_revision=case.listing_revision,
        )
    _check_valuation(case, request)
    _check_outcome(case, request, guard)
    new_state = _OUTCOME_STATE[request.outcome]
    require_transition(case.state, new_state)
    return SubmitDecision(
        case_id=case.case_id,
        case_version=case.row_version,
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
        new_state=new_state,
        listing_id=case.listing_id,
        listing_revision_id=case.revision_id,
        listing_revision=case.listing_revision,
        valuation_id=request.valuation_id,
        actor_principal_id=actor.principal_id,
        actor_kind=actor.principal_kind,
        actor_role=actor.role,
        outcome=request.outcome,
        reason_codes=request.reason_codes,
        summary=request.summary,
        evidence_ids=request.evidence_ids,
        missing_information=request.missing_information,
        model_name=_bounded_meta(model_name),
        model_version=_bounded_meta(model_version),
        model_run_id=request.model_run_id,
        prompt_template_version=_bounded_meta(prompt_template_version),
        tool_request_id=actor.request_id,
        input_hash=canonical_request_hash("reviews_submit", request),
        decided_at=now,
        supersedes_decision_id=case.latest_decision_id,
        notification_candidate=request.outcome == ReviewOutcome.SHORTLISTED and not case.is_fixture,
        is_fixture=case.is_fixture,
    )


def _check_valuation(case: ReviewCaseSnapshot, request: SubmitRequest) -> None:
    if request.valuation_id is None:
        return
    if request.valuation_id != case.valuation_id:
        raise VersionConflict(
            "The cited valuation is not the current valuation for this case",
            cited_valuation_id=str(request.valuation_id),
        )
    if case.valuation_state in _UNUSABLE_VALUATION_STATES:
        raise VersionConflict(
            "The current valuation is stale or invalid; wait for recalculation",
            valuation_state=case.valuation_state.value if case.valuation_state else None,
        )


def _check_outcome(case: ReviewCaseSnapshot, request: SubmitRequest, guard: SubmitGuard | None) -> None:
    if request.outcome == ReviewOutcome.NEEDS_INFORMATION and not request.missing_information:
        raise ValidationFailed("needs_information decisions must list the missing information")
    if request.outcome != ReviewOutcome.SHORTLISTED:
        return
    if request.valuation_id is None or case.valuation_state not in _SHORTLIST_VALUATION_STATES:
        raise ValidationFailed("a shortlist decision must cite the current, usable valuation")
    if guard is None:
        raise ValidationFailed("a shortlist decision needs the pre-decision guard facts")
    stale: list[str] = []
    business: list[str] = []
    if not guard.freshness_ok:
        stale.append("LISTING_FRESHNESS_EXPIRED")
    if guard.valuation_fingerprint_current is not True:
        stale.append("VALUATION_DEPENDENCIES_CHANGED")
    if guard.eligibility not in (EligibilityState.ELIGIBLE_PRIMARY, EligibilityState.ELIGIBLE_MANUAL_PROFILE):
        business.append("LISTING_NOT_ELIGIBLE")
    if guard.availability != Availability.AVAILABLE:
        business.append("LISTING_NOT_AVAILABLE")
    if stale:
        raise VersionConflict(
            "Current facts changed or expired; recheck/recalculation queued", blockers=stale + business
        )
    if business:
        raise ValidationFailed("The listing cannot be shortlisted now", details={"blockers": business})


# ---------------------------------------------------------------------------------------------
# Material revisions and supersede
# ---------------------------------------------------------------------------------------------


def apply_new_revision(
    case: ReviewCaseSnapshot,
    *,
    revision_id: UUID,
    listing_revision: int,
    reason: str = "new material revision",
) -> CaseUpdate:
    """A new qualifying/material listing revision arrives (spec 14, 18).

    New case version; the old valuation reference is cleared (stale until recomputed); decided
    cases return to pending; an active claim is kept, but submissions against the old
    version/revision now fail with ``VERSION_CONFLICT``. Previous decisions stay in history.
    """
    if case.state == ReviewState.SUPERSEDED:
        raise ValidationFailed("a superseded case cannot take new revisions; use the open case")
    if listing_revision <= case.listing_revision:
        raise VersionConflict(
            "Revision is not newer than the case revision",
            current_listing_revision=case.listing_revision,
            offered_listing_revision=listing_revision,
        )
    state = ReviewState.CLAIMED if case.state == ReviewState.CLAIMED else ReviewState.PENDING
    if state != case.state:
        require_transition(case.state, state)
    return CaseUpdate(
        case_id=case.case_id,
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
        state=state,
        revision_id=revision_id,
        listing_revision=listing_revision,
        valuation_id=None,
        valuation_state=None,
        clear_claim=False,
        reason=reason[:2000],
    )


def mark_superseded(case: ReviewCaseSnapshot, *, superseded_by_id: UUID | None, reason: str) -> CaseUpdate:
    """Close a case that has been replaced (e.g. identity merge or profile change). Clears any claim."""
    if case.state == ReviewState.SUPERSEDED:
        raise ValidationFailed("case is already superseded")
    if superseded_by_id == case.case_id:
        raise ValidationFailed("a case cannot supersede itself")
    require_transition(case.state, ReviewState.SUPERSEDED)
    return CaseUpdate(
        case_id=case.case_id,
        expected_row_version=case.row_version,
        row_version=case.row_version + 1,
        state=ReviewState.SUPERSEDED,
        revision_id=case.revision_id,
        listing_revision=case.listing_revision,
        valuation_id=case.valuation_id,
        valuation_state=case.valuation_state,
        clear_claim=True,
        superseded_by_id=superseded_by_id,
        reason=reason[:2000],
    )


# ---------------------------------------------------------------------------------------------
# Idempotency (spec 21)
# ---------------------------------------------------------------------------------------------

IdempotencyOperation = Literal[
    "reviews_claim",
    "reviews_release",
    "reviews_submit",
    "deals_request_recheck",
    "deals_add_note",
    "sources_pause",
]


class IdempotencyOutcome(StrEnum):
    PROCEED = "proceed"  # no record: run the operation and store the result in the same transaction
    REPLAY_RESULT = "replay_result"  # same key, same hash, completed: return the stored result
    REPLAY_ERROR = "replay_error"  # same key, same hash, failed: return the stored error code
    IN_PROGRESS = "in_progress"  # same key, same hash, still running: retryable, do not run again


class StoredIdempotency(BaseModel):
    """View of an ``ops.idempotency_records`` row (scoped by principal and operation)."""

    model_config = _FROZEN

    principal_id: UUID
    operation: str = Field(max_length=80)
    idempotency_key: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["in_progress", "completed", "failed"]
    result: dict[str, Any] | None = None
    error_code: str | None = None


class IdempotencyCheck(BaseModel):
    model_config = _FROZEN

    outcome: IdempotencyOutcome
    stored: StoredIdempotency | None = None


def validate_idempotency_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("idempotency_key must be 8-128 characters of letters, digits and . _ : -")
    return key


def canonical_request_hash(operation: str, request: Mapping[str, Any] | BaseModel) -> str:
    """SHA-256 over the canonical request without ``idempotency_key``.

    A plaintext ``claim_token`` is replaced by its SHA-256 first, so no stored hash input ever
    contains the token itself.
    """
    if isinstance(request, BaseModel):
        payload: dict[str, Any] = request.model_dump(mode="json")
    else:
        payload = dict(request)
    payload.pop("idempotency_key", None)
    token = payload.get("claim_token")
    if isinstance(token, str):
        payload["claim_token"] = (
            "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest() if token else token
        )
    return sha256_json({"operation": operation, "request": payload})


def check_idempotency(
    stored: StoredIdempotency | None,
    *,
    principal_id: UUID,
    operation: str,
    idempotency_key: str,
    request_hash: str,
) -> IdempotencyCheck:
    """Compare a stored record with a new request. Different hash -> ``IDEMPOTENCY_CONFLICT``."""
    if stored is None:
        return IdempotencyCheck(outcome=IdempotencyOutcome.PROCEED)
    if (stored.principal_id, stored.operation, stored.idempotency_key) != (
        principal_id,
        operation,
        idempotency_key,
    ):
        raise ValidationFailed("idempotency record scope does not match the request")
    if not hmac.compare_digest(stored.request_hash, request_hash):
        raise IdempotencyConflict()
    outcome = {
        "completed": IdempotencyOutcome.REPLAY_RESULT,
        "failed": IdempotencyOutcome.REPLAY_ERROR,
        "in_progress": IdempotencyOutcome.IN_PROGRESS,
    }[stored.state]
    return IdempotencyCheck(outcome=outcome, stored=stored)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _same_workspace(case: ReviewCaseSnapshot, actor: ActorContext) -> None:
    if case.workspace_id != actor.workspace_id:
        raise NotFound()  # never reveal that a foreign-workspace case exists


def _superseded(case: ReviewCaseSnapshot) -> VersionConflict:
    return VersionConflict(
        "The case was superseded; reload the current case",
        current_version=case.row_version,
        superseded_by_id=None if case.superseded_by_id is None else str(case.superseded_by_id),
    )


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _bounded_meta(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_RE.sub("", value).strip()
    return cleaned[:200] or None


def _rfc3339(value: datetime) -> str:
    utc = ensure_utc(value)
    spec = "milliseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=spec).replace("+00:00", "Z")
