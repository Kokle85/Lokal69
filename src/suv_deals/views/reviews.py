"""Review queue, case, claim and decision read models plus the ``review.pending`` event (spec 14, 21, 22).

- A queue item exposes the claim *state* only: whether the case is claimed, whether the caller
  holds the claim and when it expires. It never exposes a claim token, its hash or another
  reviewer's identity.
- ``ClaimResult`` carries the opaque claim token exactly once; an idempotent replay returns the
  stored, redacted result (``claim_token: null, claim_token_redacted: true``).
- ``ReviewPendingEventPayload`` is the internal outbox payload built by
  ``domain.notifications.build_review_pending_event``; ``ReviewPendingOccurrence`` is the native
  MCP Events occurrence built by ``integrations.event_bridge.build_occurrence``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import PrincipalKind
from suv_deals.domain.enums import (
    EligibilityState,
    ProfileKey,
    ReviewOutcome,
    ReviewState,
    Role,
    ValuationState,
)
from suv_deals.domain.reviews import ClaimGrant, ReleaseResult, ReviewCaseSnapshot, SubmitDecision
from suv_deals.views.candidates import CandidateSummary, RankSummary, ValuationRef
from suv_deals.views.common import (
    AmountView,
    DecimalStr,
    MachineLabel,
    Sha256Hex,
    UtcDatetime,
    ViewModel,
)

CLAIM_TOKEN_PATTERN: Final = r"^[A-Za-z0-9_-]{20,256}$"  # noqa: S105 - a format, not a secret
CLAIM_TOKEN_NOTICE: Final = (
    "Opaque claim token, returned once. Keep it private; it is never stored or shown again."  # noqa: S105
)
DECISION_NOTICE: Final = (
    "A reviewer's recommendation with its evidence trail; it does not turn seller claims into verified facts."
)
QUEUE_PROJECTION_NOTICE: Final = (
    "Frozen queue projection for stable pagination; claim and submit revalidate current versions."
)


# --------------------------------------------------------------------------- claim state


class ClaimStateView(ViewModel):
    """Claim status visible to every reader. No token, no hash, no other holder identity."""

    claimed: bool
    held_by_caller: bool
    expires_at: UtcDatetime | None

    @model_validator(mode="after")
    def _consistent(self) -> ClaimStateView:
        if not self.claimed and (self.held_by_caller or self.expires_at is not None):
            raise ValueError("an unclaimed case has no holder or expiry")
        if self.claimed and self.expires_at is None:
            raise ValueError("a claimed case has an expiry")
        return self

    @classmethod
    def of(cls, case: ReviewCaseSnapshot, *, caller_id: UUID, now: datetime) -> ClaimStateView:
        """Active claims only: an expired claim reads as unclaimed (it is released implicitly)."""
        if not case.claim_active(ensure_utc(now)):
            return cls(claimed=False, held_by_caller=False, expires_at=None)
        return cls(
            claimed=True, held_by_caller=case.claim_holder == caller_id, expires_at=case.claim_expires_at
        )


# --------------------------------------------------------------------------- queue


class ReviewQueueItem(ViewModel):
    """One pending-queue entry with eligibility/readiness and the case version (spec 21)."""

    case_id: UUID
    case_version: int = Field(ge=1)
    listing_id: UUID
    revision_id: UUID
    listing_revision: int = Field(ge=1)
    valuation_id: UUID | None
    profile: ProfileKey
    queue_label: str = Field(max_length=120)
    state: ReviewState
    eligibility: EligibilityState | None
    readiness: MachineLabel
    valuation_state: ValuationState
    priority: int = Field(ge=-100_000, le=100_000)
    rank: RankSummary | None
    claim: ClaimStateView
    title: str | None = Field(max_length=300)
    make: str | None = Field(max_length=80)
    model: str | None = Field(max_length=120)
    seller_country: str | None = Field(pattern=r"^[A-Z]{2}$")
    payable: AmountView
    payable_eur: AmountView
    mileage_km: DecimalStr | None
    research_candidate: bool
    is_fixture: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> ReviewQueueItem:
        if self.state == ReviewState.SUPERSEDED:
            raise ValueError("superseded cases are not queue items")
        if self.valuation_id is None and self.valuation_state != ValuationState.NOT_STARTED:
            raise ValueError("a valuation state other than not_started needs a valuation id")
        return self


class ReviewQueuePage(ViewModel):
    """A page of a frozen ``ops.query_snapshots`` projection; the cursor is in the envelope."""

    items: tuple[ReviewQueueItem, ...] = Field(max_length=100)
    total: int = Field(ge=0, le=10_000)
    snapshot_created_at: UtcDatetime
    snapshot_expires_at: UtcDatetime
    include_needs_information: bool
    notice: str = QUEUE_PROJECTION_NOTICE

    @model_validator(mode="after")
    def _bounds(self) -> ReviewQueuePage:
        if self.snapshot_expires_at <= self.snapshot_created_at:
            raise ValueError("snapshot expiry must follow its creation")
        if len(self.items) > self.total:
            raise ValueError("a page cannot hold more items than the snapshot")
        allowed = {ReviewState.PENDING, ReviewState.CLAIMED}
        if self.include_needs_information:
            allowed.add(ReviewState.NEEDS_INFORMATION)
        if any(item.state not in allowed for item in self.items):
            raise ValueError("queue pages hold pending (and optionally needs_information) cases only")
        return self


# --------------------------------------------------------------------------- decisions


class DecisionActorView(ViewModel):
    """The authenticated actor recorded by the server (request bodies carry no actor fields)."""

    principal_id: UUID
    principal_kind: PrincipalKind
    role: Role


class ReviewDecisionView(ViewModel):
    """One immutable ``app.review_decisions`` row (spec 14, 21)."""

    decision_id: UUID
    case_id: UUID
    case_version: int = Field(ge=1)
    case_state: ReviewState
    new_case_version: int = Field(ge=1)
    listing_id: UUID
    listing_revision_id: UUID
    listing_revision: int = Field(ge=1)
    valuation_id: UUID | None
    outcome: ReviewOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=10, max_length=4000)
    evidence_ids: tuple[UUID, ...] = Field(max_length=100)
    missing_information: tuple[str, ...] = Field(max_length=30)
    actor: DecisionActorView
    model_name: str | None = Field(max_length=200)
    model_version: str | None = Field(max_length=200)
    model_run_id: str | None = Field(max_length=200)
    prompt_template_version: str | None = Field(max_length=200)
    tool_request_id: str = Field(max_length=200)
    input_hash: Sha256Hex
    decided_at: UtcDatetime
    supersedes_decision_id: UUID | None
    is_fixture: bool
    decided_by_caller: bool = Field(
        default=False,
        description="True when the authenticated caller of this response recorded the decision.",
    )
    notice: str = DECISION_NOTICE

    def for_caller(self, principal_id: UUID) -> ReviewDecisionView:
        """This decision with ``decided_by_caller`` set for the authenticated ``principal_id``."""
        return self.model_copy(update={"decided_by_caller": self.actor.principal_id == principal_id})

    @model_validator(mode="after")
    def _versions(self) -> ReviewDecisionView:
        if self.new_case_version <= self.case_version:
            raise ValueError("a decision increments the case version")
        expected = {
            ReviewOutcome.NEEDS_INFORMATION: ReviewState.NEEDS_INFORMATION,
            ReviewOutcome.WATCH: ReviewState.WATCH,
            ReviewOutcome.SHORTLISTED: ReviewState.SHORTLISTED,
            ReviewOutcome.REJECTED: ReviewState.REJECTED,
        }[self.outcome]
        if self.case_state != expected:
            raise ValueError("case state must follow the decision outcome")
        return self

    @classmethod
    def of(cls, decision: SubmitDecision, *, decision_id: UUID) -> ReviewDecisionView:
        return cls(
            decision_id=decision_id,
            case_id=decision.case_id,
            case_version=decision.case_version,
            case_state=decision.new_state,
            new_case_version=decision.row_version,
            listing_id=decision.listing_id,
            listing_revision_id=decision.listing_revision_id,
            listing_revision=decision.listing_revision,
            valuation_id=decision.valuation_id,
            outcome=decision.outcome,
            reason_codes=decision.reason_codes,
            summary=decision.summary,
            evidence_ids=decision.evidence_ids,
            missing_information=decision.missing_information,
            actor=DecisionActorView(
                principal_id=decision.actor_principal_id,
                principal_kind=decision.actor_kind,
                role=decision.actor_role,
            ),
            model_name=decision.model_name,
            model_version=decision.model_version,
            model_run_id=decision.model_run_id,
            prompt_template_version=decision.prompt_template_version,
            tool_request_id=decision.tool_request_id,
            input_hash=decision.input_hash,
            decided_at=decision.decided_at,
            supersedes_decision_id=decision.supersedes_decision_id,
            is_fixture=decision.is_fixture,
        )


class ReviewCaseView(ViewModel):
    """``schemas/review.schema.json``: one review case with its candidate and decision history."""

    case_id: UUID
    case_version: int = Field(ge=1)
    state: ReviewState
    listing_id: UUID
    revision_id: UUID
    listing_revision: int = Field(ge=1)
    profile: ProfileKey
    queue_label: str = Field(max_length=120)
    readiness: MachineLabel
    priority: int = Field(ge=-100_000, le=100_000)
    claim: ClaimStateView
    candidate: CandidateSummary
    valuation: ValuationRef | None
    latest_decision_id: UUID | None
    decisions: tuple[ReviewDecisionView, ...] = Field(max_length=200)
    superseded_by_id: UUID | None
    reason: str | None = Field(max_length=2000)
    is_fixture: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime

    def for_caller(self, principal_id: UUID) -> ReviewCaseView:
        """This case with every decision's ``decided_by_caller`` set for ``principal_id``."""
        return self.model_copy(
            update={"decisions": tuple(d.for_caller(principal_id) for d in self.decisions)}
        )

    @model_validator(mode="after")
    def _consistent(self) -> ReviewCaseView:
        if self.candidate.listing_id != self.listing_id:
            raise ValueError("the candidate must be the case's listing")
        decided = {
            ReviewState.NEEDS_INFORMATION,
            ReviewState.WATCH,
            ReviewState.SHORTLISTED,
            ReviewState.REJECTED,
        }
        if self.state in decided and self.latest_decision_id is None:
            raise ValueError("a decided case references its decision")
        if self.superseded_by_id is not None and self.state != ReviewState.SUPERSEDED:
            raise ValueError("only a superseded case names its successor")
        if self.claim.claimed and self.state != ReviewState.CLAIMED:
            raise ValueError("an active claim implies the claimed state")
        if any(d.case_id != self.case_id for d in self.decisions):
            raise ValueError("decisions must belong to this case")
        ids = {d.decision_id for d in self.decisions}
        if self.latest_decision_id is not None and self.decisions and self.latest_decision_id not in ids:
            raise ValueError("the latest decision must be in the listed history")
        return self


# --------------------------------------------------------------------------- claim / release


class ClaimResult(ViewModel):
    """``reviews_claim`` result: opaque token (once), expiry, case version and exact revision."""

    case_id: UUID
    claim_token: str | None = Field(pattern=CLAIM_TOKEN_PATTERN, repr=False)
    claim_token_redacted: bool
    token_notice: str = CLAIM_TOKEN_NOTICE
    expires_at: UtcDatetime
    case_version: int = Field(ge=1)
    listing_id: UUID
    revision_id: UUID
    listing_revision: int = Field(ge=1)
    valuation_id: UUID | None
    rotated: bool
    took_over_expired: bool

    @model_validator(mode="after")
    def _token(self) -> ClaimResult:
        if (self.claim_token is None) != self.claim_token_redacted:
            raise ValueError("the token is present exactly when the result is not redacted")
        return self

    @classmethod
    def of(cls, grant: ClaimGrant) -> ClaimResult:
        """Fresh claim: the plaintext token is included this one time."""
        return cls(
            case_id=grant.case_id,
            claim_token=grant.claim_token.get_secret_value(),
            claim_token_redacted=False,
            expires_at=grant.expires_at,
            case_version=grant.row_version,
            listing_id=grant.listing_id,
            revision_id=grant.revision_id,
            listing_revision=grant.listing_revision,
            valuation_id=grant.valuation_id,
            rotated=grant.rotated,
            took_over_expired=grant.took_over_expired,
        )

    @classmethod
    def from_stored(cls, result: Mapping[str, Any]) -> ClaimResult:
        """Idempotent replay of ``ClaimGrant.redacted_result()``: never a plaintext token."""
        return cls(
            case_id=result["case_id"],
            claim_token=None,
            claim_token_redacted=True,
            expires_at=result["expires_at"],
            case_version=result["case_version"],
            listing_id=result["listing_id"],
            revision_id=result["revision_id"],
            listing_revision=result["listing_revision"],
            valuation_id=result.get("valuation_id"),
            rotated=bool(result.get("rotated", False)),
            took_over_expired=bool(result.get("took_over_expired", False)),
        )


class ReleaseResultView(ViewModel):
    """``reviews_release`` result. Releasing a claim you do not hold is a no-op, not an error."""

    case_id: UUID
    released: bool
    reason: Literal["released", "expired", "not_claimed", "not_held"]
    state: ReviewState
    case_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _released(self) -> ReleaseResultView:
        if self.released != (self.reason in ("released", "expired")):
            raise ValueError("released matches the reason")
        return self

    @classmethod
    def of(cls, result: ReleaseResult) -> ReleaseResultView:
        return cls(
            case_id=result.case_id,
            released=result.changed,
            reason=result.reason,
            state=result.new_state,
            case_version=result.row_version,
        )


# --------------------------------------------------------------------------- events

_READINESS_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_QUEUE_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class ReviewPendingEventPayload(ViewModel):
    """``schemas/event.schema.json``: internal ``review.pending`` outbox payload (spec 22).

    Built by ``domain.notifications.build_review_pending_event``. Not the MCP wire envelope: the
    native provider maps it to ``ReviewPendingOccurrence``. ``fixture`` is present (and true)
    only on synthetic fixture events, which are never routed externally.
    """

    schema_version: Literal["1.0"]
    event_id: UUID
    type: Literal["review.pending"]
    occurred_at: UtcDatetime
    case_id: UUID
    case_version: int = Field(ge=1, strict=True)
    listing_id: UUID
    listing_revision: int = Field(ge=1, strict=True)
    priority: Literal["low", "normal", "high"]
    dashboard_url: str = Field(min_length=8, max_length=2048)
    summary: str = Field(min_length=1, max_length=400)
    deduplication_key: str = Field(min_length=1, max_length=300)
    readiness: str = Field(pattern=_READINESS_PATTERN)
    profile: ProfileKey
    queue: str | None = Field(default=None, pattern=_QUEUE_PATTERN)
    fixture: Literal[True] | None = None

    @model_validator(mode="after")
    def _dedup(self) -> ReviewPendingEventPayload:
        if self.deduplication_key != f"review.pending:{self.case_id}:{self.case_version}":
            raise ValueError("deduplication_key must be review.pending:<case_id>:<case_version>")
        if not self.dashboard_url.startswith(("https://", "http://")):
            raise ValueError("dashboard_url must be an absolute http(s) URL")
        return self


class ReviewPendingOccurrenceData(ViewModel):
    """Minimal reference fields delivered to MCP Events subscribers (no seller text or money)."""

    case_id: UUID
    case_version: int = Field(ge=1, strict=True)
    listing_id: UUID
    listing_revision: int = Field(ge=1, strict=True)
    readiness: str = Field(pattern=_READINESS_PATTERN, max_length=64)
    dashboard_url: str = Field(max_length=2048)


class ReviewPendingOccurrence(ViewModel):
    """Native MCP Events occurrence for ``review.pending.v1``; replay is not implemented (cursor null)."""

    eventId: UUID  # MCP wire name (camelCase)
    name: Literal["review.pending.v1"]
    timestamp: UtcDatetime
    data: ReviewPendingOccurrenceData
    cursor: None
