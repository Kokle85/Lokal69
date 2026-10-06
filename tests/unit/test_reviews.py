"""Unit tests for domain.reviews (spec 14, 18, 21). All ids and cases are SYNTHETIC."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.actor import ActorContext
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
from suv_deals.domain.reviews import (
    ALLOWED_TRANSITIONS,
    CLAIMABLE_STATES,
    IdempotencyOutcome,
    ReviewCaseSnapshot,
    StoredIdempotency,
    SubmitGuard,
    SubmitRequest,
    apply_new_revision,
    can_transition,
    canonical_request_hash,
    check_idempotency,
    evaluate_claim,
    evaluate_release,
    evaluate_submit,
    expire_claim,
    hash_claim_token,
    mark_superseded,
    new_claim_token,
    parse_submit_request,
    require_transition,
    verify_claim_token,
)
from suv_deals.errors import (
    AlreadyClaimed,
    ClaimExpired,
    ErrorCode,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    ValidationFailed,
    VersionConflict,
)

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
WS = UUID(int=100)
CASE = UUID(int=200)
LISTING = UUID(int=300)
REVISION = UUID(int=301)
VALUATION = UUID(int=400)
ALICE = UUID(int=1)
BOB = UUID(int=2)
TOKEN = "synthetic-claim-token-AAAAAAAAAAAAAAAAAAAAAAAAAA"


def actor(
    principal: UUID = ALICE, *, role: Role = Role.REVIEWER, workspace: UUID = WS, **kw: Any
) -> ActorContext:
    scopes = kw.pop("scopes", None)
    return ActorContext(
        workspace_id=workspace,
        principal_id=principal,
        principal_kind=kw.pop("kind", "mcp_client"),
        role=role,
        scopes=frozenset(scopes)
        if scopes is not None
        else frozenset({Scope.REVIEWS_READ, Scope.REVIEWS_WRITE}),
        request_id="req-synthetic-1",
    )


def case(**overrides: Any) -> ReviewCaseSnapshot:
    data: dict[str, Any] = {
        "case_id": CASE,
        "workspace_id": WS,
        "listing_id": LISTING,
        "profile_key": ProfileKey.PRIMARY,
        "state": ReviewState.PENDING,
        "row_version": 1,
        "revision_id": REVISION,
        "listing_revision": 3,
        "valuation_id": VALUATION,
        "valuation_state": ValuationState.INCOMPLETE,
    }
    data.update(overrides)
    return ReviewCaseSnapshot(**data)


def claimed(
    holder: UUID = ALICE, *, expires: datetime | None = None, version: int = 2, **kw: Any
) -> ReviewCaseSnapshot:
    return case(
        state=ReviewState.CLAIMED,
        row_version=version,
        claim_holder=holder,
        claim_token_hash=hash_claim_token(TOKEN),
        claimed_at=NOW - timedelta(minutes=1),
        claim_expires_at=expires or NOW + timedelta(minutes=4),
        **kw,
    )


def submit(**overrides: Any) -> SubmitRequest:
    data: dict[str, Any] = {
        "case_id": CASE,
        "claim_token": TOKEN,
        "expected_version": 2,
        "listing_revision": 3,
        "valuation_id": VALUATION,
        "outcome": ReviewOutcome.WATCH,
        "reason_codes": ("PRICE_IN_BAND", "COMPARABLES_SMALL"),
        "summary": "Synthetic rationale: in band, comparables small, import costs unknown.",
        "evidence_ids": (UUID(int=900),),
        "idempotency_key": "synthetic-key-0001",
    }
    data.update(overrides)
    return SubmitRequest(**data)


GOOD_GUARD = SubmitGuard(
    eligibility=EligibilityState.ELIGIBLE_PRIMARY,
    availability=Availability.AVAILABLE,
    freshness_ok=True,
    valuation_fingerprint_current=True,
)


# --------------------------------------------------------------------------------------------- tokens


def test_claim_token_is_random_urlsafe_256_bit() -> None:
    tokens = {new_claim_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) >= 43  # 32 random bytes, base64url
        assert verify_claim_token(token, hash_claim_token(token))


def test_only_hash_is_stored_and_compare_is_strict() -> None:
    digest = hash_claim_token(TOKEN)
    assert digest == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert TOKEN not in digest
    assert not verify_claim_token(TOKEN + "x", digest)
    assert not verify_claim_token(TOKEN, None)
    assert not verify_claim_token("short", digest)
    assert not verify_claim_token(None, digest)
    assert not verify_claim_token(TOKEN, "not-a-hash")
    with pytest.raises(ValidationFailed):
        hash_claim_token("bad token with spaces!!!!!")


# --------------------------------------------------------------------------------------------- state machine


def test_transition_table_is_complete_and_superseded_is_terminal() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(ReviewState)
    assert ALLOWED_TRANSITIONS[ReviewState.SUPERSEDED] == frozenset()
    for state in ReviewState:
        if state != ReviewState.SUPERSEDED:
            assert can_transition(state, ReviewState.SUPERSEDED)
    assert not can_transition(ReviewState.PENDING, ReviewState.SHORTLISTED)  # decisions need a claim
    assert not can_transition(ReviewState.REJECTED, ReviewState.CLAIMED)
    with pytest.raises(ValidationFailed):
        require_transition(ReviewState.SUPERSEDED, ReviewState.PENDING)
    assert ReviewState.REJECTED not in CLAIMABLE_STATES


def test_snapshot_mirrors_database_invariants() -> None:
    with pytest.raises(ValueError):
        case(state=ReviewState.CLAIMED)  # no claim data
    with pytest.raises(ValueError):
        case(claim_holder=ALICE)  # claim data on a pending case
    with pytest.raises(ValueError):
        case(state=ReviewState.WATCH)  # decided without decision
    with pytest.raises(ValueError):
        case(superseded_by_id=UUID(int=5))
    with pytest.raises(ValueError):
        case(valuation_id=None, valuation_state=ValuationState.ESTIMATED)
    with pytest.raises(ValueError):
        claimed(expires=NOW - timedelta(minutes=2))  # expiry before claimed_at


# --------------------------------------------------------------------------------------------- claim


def test_claim_pending_case() -> None:
    grant = evaluate_claim(case(), actor(), expected_version=1, now=NOW, token_factory=lambda: TOKEN)
    assert grant.holder == ALICE
    assert grant.expected_row_version == 1 and grant.row_version == 2
    assert grant.expires_at == NOW + timedelta(minutes=5)
    assert grant.claim_token_hash == hash_claim_token(TOKEN)
    assert grant.listing_revision == 3 and grant.revision_id == REVISION and grant.valuation_id == VALUATION
    public = grant.public_result()
    assert public["claim_token"] == TOKEN and public["case_version"] == 2
    stored = grant.redacted_result()
    assert stored["claim_token"] is None and stored["claim_token_redacted"] is True
    assert TOKEN not in repr(grant)


def test_claim_by_other_reviewer_is_already_claimed() -> None:
    with pytest.raises(AlreadyClaimed) as exc:
        evaluate_claim(claimed(ALICE), actor(BOB), expected_version=2, now=NOW)
    assert exc.value.code == ErrorCode.ALREADY_CLAIMED
    # even with a stale version the more precise ALREADY_CLAIMED wins
    with pytest.raises(AlreadyClaimed):
        evaluate_claim(claimed(ALICE), actor(BOB), expected_version=1, now=NOW)


def test_claim_version_conflict() -> None:
    with pytest.raises(VersionConflict) as exc:
        evaluate_claim(case(row_version=4), actor(), expected_version=3, now=NOW)
    assert exc.value.details == {"expected_version": 3, "current_version": 4}


def test_expired_claim_can_be_taken_over() -> None:
    old = claimed(ALICE, expires=NOW)  # expiry == now counts as expired
    grant = evaluate_claim(old, actor(BOB), expected_version=2, now=NOW, token_factory=lambda: TOKEN)
    assert grant.took_over_expired is True and grant.holder == BOB and grant.row_version == 3


def test_same_holder_reclaim_rotates_token() -> None:
    new_token = "synthetic-rotated-token-BBBBBBBBBBBBBBBBBBBBB"
    grant = evaluate_claim(
        claimed(ALICE), actor(ALICE), expected_version=2, now=NOW, token_factory=lambda: new_token
    )
    assert grant.rotated is True
    assert grant.claim_token_hash != hash_claim_token(TOKEN)
    assert grant.row_version == 3
    with pytest.raises(VersionConflict):
        evaluate_claim(claimed(ALICE), actor(ALICE), expected_version=1, now=NOW)


@pytest.mark.parametrize("state", [ReviewState.NEEDS_INFORMATION, ReviewState.WATCH, ReviewState.SHORTLISTED])
def test_decided_open_states_are_claimable(state: ReviewState) -> None:
    decided = case(
        state=state,
        latest_decision_id=UUID(int=700),
        latest_decision_outcome=ReviewOutcome(state.value),
        latest_decision_listing_revision=3,
    )
    assert evaluate_claim(decided, actor(), expected_version=1, now=NOW).row_version == 2


def test_rejected_and_superseded_not_claimable() -> None:
    rejected = case(
        state=ReviewState.REJECTED,
        latest_decision_id=UUID(int=700),
        latest_decision_outcome=ReviewOutcome.REJECTED,
        latest_decision_listing_revision=3,
    )
    with pytest.raises(ValidationFailed):
        evaluate_claim(rejected, actor(), expected_version=1, now=NOW)
    superseded = case(state=ReviewState.SUPERSEDED, superseded_by_id=UUID(int=201))
    with pytest.raises(VersionConflict) as exc:
        evaluate_claim(superseded, actor(), expected_version=1, now=NOW)
    assert exc.value.details["superseded_by_id"] == str(UUID(int=201))


def test_claim_scope_workspace_and_duration_checks() -> None:
    viewer = actor(role=Role.VIEWER, scopes={Scope.REVIEWS_READ})
    with pytest.raises(Forbidden):
        evaluate_claim(case(), viewer, expected_version=1, now=NOW)
    with pytest.raises(NotFound):
        evaluate_claim(case(), actor(workspace=UUID(int=999)), expected_version=1, now=NOW)
    with pytest.raises(ValidationFailed):
        evaluate_claim(case(), actor(), expected_version=1, now=NOW, duration=timedelta(seconds=59))
    with pytest.raises(ValidationFailed):
        evaluate_claim(case(), actor(), expected_version=1, now=NOW, duration=timedelta(hours=2))
    with pytest.raises(ValidationFailed):
        evaluate_claim(case(), actor(), expected_version=1, now=datetime(2026, 10, 6, 10, 0))


def test_malformed_token_factory_output_rejected() -> None:
    with pytest.raises(ValidationFailed):
        evaluate_claim(case(), actor(), expected_version=1, now=NOW, token_factory=lambda: "short")


# --------------------------------------------------------------------------------------------- release


def test_release_own_claim_restores_pending() -> None:
    result = evaluate_release(claimed(ALICE), actor(ALICE), TOKEN, NOW)
    assert result.changed and result.reason == "released"
    assert result.new_state == ReviewState.PENDING
    assert (result.expected_row_version, result.row_version) == (2, 3)


def test_release_restores_still_valid_prior_decision() -> None:
    prior = claimed(
        ALICE,
        latest_decision_id=UUID(int=700),
        latest_decision_outcome=ReviewOutcome.WATCH,
        latest_decision_listing_revision=3,
    )
    assert evaluate_release(prior, actor(ALICE), TOKEN, NOW).new_state == ReviewState.WATCH
    outdated = claimed(
        ALICE,
        latest_decision_id=UUID(int=700),
        latest_decision_outcome=ReviewOutcome.WATCH,
        latest_decision_listing_revision=2,
    )
    assert evaluate_release(outdated, actor(ALICE), TOKEN, NOW).new_state == ReviewState.PENDING


def test_release_is_idempotent_noop() -> None:
    not_claimed = evaluate_release(case(), actor(ALICE), TOKEN, NOW)
    assert not not_claimed.changed and not_claimed.reason == "not_claimed"
    assert not_claimed.row_version == not_claimed.expected_row_version == 1
    other = evaluate_release(claimed(BOB), actor(ALICE), TOKEN, NOW)
    assert not other.changed and other.reason == "not_held"
    assert other.new_state == ReviewState.CLAIMED


def test_release_with_stale_token_is_claim_expired() -> None:
    with pytest.raises(ClaimExpired):
        evaluate_release(claimed(ALICE), actor(ALICE), "synthetic-other-token-CCCCCCCCCCCCCCC", NOW)


def test_release_requires_write_scope() -> None:
    with pytest.raises(Forbidden):
        evaluate_release(claimed(ALICE), actor(role=Role.VIEWER, scopes={Scope.REVIEWS_READ}), TOKEN, NOW)


def test_expire_claim_for_reaper() -> None:
    assert expire_claim(claimed(ALICE), NOW) is None
    assert expire_claim(case(), NOW) is None
    result = expire_claim(claimed(ALICE, expires=NOW - timedelta(seconds=1)), NOW)
    assert result is not None and result.reason == "expired" and result.new_state == ReviewState.PENDING


# --------------------------------------------------------------------------------------------- submit


def test_submit_happy_path_records_authenticated_actor() -> None:
    decision = evaluate_submit(
        claimed(ALICE),
        actor(ALICE),
        submit(),
        now=NOW,
        model_name="synthetic-model",
        model_version="v1",
        prompt_template_version="review-template@1",
    )
    assert decision.new_state == ReviewState.WATCH
    assert decision.case_version == 2 and decision.row_version == 3
    assert decision.actor_principal_id == ALICE
    assert decision.actor_kind == "mcp_client" and decision.actor_role == Role.REVIEWER
    assert decision.listing_revision_id == REVISION and decision.listing_revision == 3
    assert decision.valuation_id == VALUATION
    assert decision.tool_request_id == "req-synthetic-1"
    assert decision.input_hash == canonical_request_hash("reviews_submit", submit())
    assert decision.clear_claim is True
    assert decision.notification_candidate is False
    assert decision.decided_at == NOW


def test_request_cannot_carry_actor_identity() -> None:
    raw = submit().model_dump()
    raw["actor_principal_id"] = str(BOB)
    with pytest.raises(ValidationFailed) as exc:
        parse_submit_request(raw)
    assert "actor_principal_id" in exc.value.details["fields"]


def test_submit_by_non_holder() -> None:
    with pytest.raises(AlreadyClaimed):
        evaluate_submit(claimed(BOB), actor(ALICE), submit(), now=NOW)
    with pytest.raises(ClaimExpired):
        evaluate_submit(case(), actor(ALICE), submit(), now=NOW)
    with pytest.raises(ClaimExpired):  # someone else's claim already expired; caller holds nothing
        evaluate_submit(claimed(BOB, expires=NOW), actor(ALICE), submit(), now=NOW)


def test_submit_after_expiry_is_claim_expired() -> None:
    with pytest.raises(ClaimExpired) as exc:
        evaluate_submit(claimed(ALICE, expires=NOW), actor(ALICE), submit(), now=NOW)
    assert exc.value.code == ErrorCode.CLAIM_EXPIRED


def test_submit_with_rotated_token_is_claim_expired() -> None:
    with pytest.raises(ClaimExpired):
        evaluate_submit(
            claimed(ALICE), actor(ALICE), submit(claim_token="synthetic-old-token-DDDDDDDDDDDD"), now=NOW
        )


def test_submit_version_conflict() -> None:
    with pytest.raises(VersionConflict) as exc:
        evaluate_submit(claimed(ALICE, version=5), actor(ALICE), submit(expected_version=2), now=NOW)
    assert exc.value.details == {"expected_version": 2, "current_version": 5}


def test_new_listing_revision_before_submit_is_version_conflict() -> None:
    current = claimed(ALICE)
    update = apply_new_revision(current, revision_id=UUID(int=302), listing_revision=4)
    after = current.model_copy(
        update={
            "row_version": update.row_version,
            "revision_id": update.revision_id,
            "listing_revision": update.listing_revision,
            "valuation_id": None,
            "valuation_state": None,
        }
    )
    assert after.state == ReviewState.CLAIMED  # claim kept, but the old view is now outdated
    with pytest.raises(VersionConflict):
        evaluate_submit(after, actor(ALICE), submit(expected_version=2, listing_revision=3), now=NOW)
    with pytest.raises(VersionConflict) as exc:
        evaluate_submit(after, actor(ALICE), submit(expected_version=3, listing_revision=3), now=NOW)
    assert exc.value.details["current_listing_revision"] == 4
    ok = evaluate_submit(
        after, actor(ALICE), submit(expected_version=3, listing_revision=4, valuation_id=None), now=NOW
    )
    assert ok.listing_revision == 4


def test_submit_superseded_case() -> None:
    superseded = case(state=ReviewState.SUPERSEDED, superseded_by_id=UUID(int=201))
    with pytest.raises(VersionConflict):
        evaluate_submit(superseded, actor(ALICE), submit(), now=NOW)


def test_submit_valuation_applicability() -> None:
    with pytest.raises(VersionConflict):
        evaluate_submit(claimed(ALICE), actor(ALICE), submit(valuation_id=UUID(int=401)), now=NOW)
    stale = claimed(ALICE, valuation_state=ValuationState.STALE)
    with pytest.raises(VersionConflict):
        evaluate_submit(stale, actor(ALICE), submit(), now=NOW)
    # a non-shortlist decision may omit the valuation
    assert evaluate_submit(stale, actor(ALICE), submit(valuation_id=None), now=NOW).valuation_id is None


def test_shortlist_requires_valuation_and_guard() -> None:
    estimated = claimed(ALICE, valuation_state=ValuationState.ESTIMATED)
    shortlist = submit(outcome=ReviewOutcome.SHORTLISTED)
    with pytest.raises(ValidationFailed):
        evaluate_submit(estimated, actor(ALICE), shortlist, now=NOW)  # no guard
    with pytest.raises(ValidationFailed):
        evaluate_submit(
            estimated,
            actor(ALICE),
            submit(outcome=ReviewOutcome.SHORTLISTED, valuation_id=None),
            now=NOW,
            guard=GOOD_GUARD,
        )
    decision = evaluate_submit(estimated, actor(ALICE), shortlist, now=NOW, guard=GOOD_GUARD)
    assert decision.new_state == ReviewState.SHORTLISTED
    assert decision.notification_candidate is True
    fixture = claimed(ALICE, valuation_state=ValuationState.ESTIMATED, is_fixture=True)
    assert (
        evaluate_submit(fixture, actor(ALICE), shortlist, now=NOW, guard=GOOD_GUARD).notification_candidate
        is False
    )


def test_shortlist_guard_blockers() -> None:
    estimated = claimed(ALICE, valuation_state=ValuationState.ESTIMATED)
    shortlist = submit(outcome=ReviewOutcome.SHORTLISTED)
    stale_guard = GOOD_GUARD.model_copy(
        update={"freshness_ok": False, "valuation_fingerprint_current": False}
    )
    with pytest.raises(VersionConflict) as exc:
        evaluate_submit(estimated, actor(ALICE), shortlist, now=NOW, guard=stale_guard)
    assert set(exc.value.details["blockers"]) == {
        "LISTING_FRESHNESS_EXPIRED",
        "VALUATION_DEPENDENCIES_CHANGED",
    }
    removed = GOOD_GUARD.model_copy(
        update={"availability": Availability.REMOVED, "eligibility": EligibilityState.NEEDS_FACTS}
    )
    with pytest.raises(ValidationFailed) as exc2:
        evaluate_submit(estimated, actor(ALICE), shortlist, now=NOW, guard=removed)
    assert set(exc2.value.details["blockers"]) == {"LISTING_NOT_ELIGIBLE", "LISTING_NOT_AVAILABLE"}


def test_needs_information_requires_missing_items() -> None:
    with pytest.raises(ValidationFailed):
        evaluate_submit(
            claimed(ALICE), actor(ALICE), submit(outcome=ReviewOutcome.NEEDS_INFORMATION), now=NOW
        )
    decision = evaluate_submit(
        claimed(ALICE),
        actor(ALICE),
        submit(outcome=ReviewOutcome.NEEDS_INFORMATION, missing_information=("CO2 value and cycle",)),
        now=NOW,
    )
    assert decision.new_state == ReviewState.NEEDS_INFORMATION


def test_submit_case_id_mismatch_and_scope() -> None:
    with pytest.raises(ValidationFailed):
        evaluate_submit(claimed(ALICE), actor(ALICE), submit(case_id=UUID(int=999)), now=NOW)
    with pytest.raises(Forbidden):
        evaluate_submit(
            claimed(ALICE), actor(role=Role.VIEWER, scopes={Scope.REVIEWS_READ}), submit(), now=NOW
        )
    with pytest.raises(NotFound):
        evaluate_submit(claimed(ALICE), actor(ALICE, workspace=UUID(int=999)), submit(), now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "too short"),
        ("summary", "Rationale <thinking>step by step hidden</thinking> text"),
        ("summary", "Synthetic rationale with a bell \x07 control char"),
        ("reason_codes", ()),
        ("reason_codes", ("has space",)),
        ("reason_codes", ("A",) * 21),
        ("reason_codes", ("DUP", "DUP")),
        ("evidence_ids", (UUID(int=1), UUID(int=1))),
        ("missing_information", ("x" * 301,)),
        ("idempotency_key", "short"),
        ("idempotency_key", "bad key with spaces"),
        ("claim_token", "tooshort"),
    ],
)
def test_submit_request_validation(field: str, value: Any) -> None:
    raw = submit().model_dump()
    raw[field] = value
    with pytest.raises(ValidationFailed):
        parse_submit_request(raw)


def test_decided_outcome_reopened_by_new_revision() -> None:
    watched = case(
        state=ReviewState.WATCH,
        row_version=3,
        latest_decision_id=UUID(int=700),
        latest_decision_outcome=ReviewOutcome.WATCH,
        latest_decision_listing_revision=3,
    )
    update = apply_new_revision(watched, revision_id=UUID(int=302), listing_revision=4)
    assert update.state == ReviewState.PENDING
    assert (update.expected_row_version, update.row_version) == (3, 4)
    assert update.valuation_id is None and update.valuation_state is None  # stale calc invalidated
    assert update.clear_claim is False
    rejected = case(
        state=ReviewState.REJECTED,
        latest_decision_id=UUID(int=701),
        latest_decision_outcome=ReviewOutcome.REJECTED,
        latest_decision_listing_revision=3,
    )
    assert (
        apply_new_revision(rejected, revision_id=UUID(int=302), listing_revision=4).state
        == ReviewState.PENDING
    )


def test_new_revision_must_be_newer_and_case_open() -> None:
    with pytest.raises(VersionConflict):
        apply_new_revision(case(), revision_id=UUID(int=302), listing_revision=3)
    with pytest.raises(ValidationFailed):
        apply_new_revision(
            case(state=ReviewState.SUPERSEDED, superseded_by_id=UUID(int=9)),
            revision_id=UUID(int=302),
            listing_revision=4,
        )


def test_mark_superseded_clears_claim() -> None:
    update = mark_superseded(claimed(ALICE), superseded_by_id=UUID(int=201), reason="identity merge")
    assert update.state == ReviewState.SUPERSEDED and update.clear_claim is True
    assert update.superseded_by_id == UUID(int=201)
    with pytest.raises(ValidationFailed):
        mark_superseded(
            case(state=ReviewState.SUPERSEDED, superseded_by_id=UUID(int=9)),
            superseded_by_id=None,
            reason="x",
        )
    with pytest.raises(ValidationFailed):
        mark_superseded(case(), superseded_by_id=CASE, reason="self")


# --------------------------------------------------------------------------------------------- idempotency


def _stored(request_hash: str, state: str = "completed", **kw: Any) -> StoredIdempotency:
    return StoredIdempotency(
        principal_id=kw.pop("principal_id", ALICE),
        operation=kw.pop("operation", "reviews_submit"),
        idempotency_key=kw.pop("key", "synthetic-key-0001"),
        request_hash=request_hash,
        state=state,
        result={"decision_id": str(UUID(int=800))} if state == "completed" else None,
        error_code="CLAIM_EXPIRED" if state == "failed" else None,
    )


def test_request_hash_ignores_key_and_hides_token() -> None:
    a = canonical_request_hash("reviews_submit", submit(idempotency_key="synthetic-key-0001"))
    b = canonical_request_hash("reviews_submit", submit(idempotency_key="synthetic-key-0002"))
    assert a == b
    assert canonical_request_hash("reviews_submit", submit(summary="A different synthetic rationale.")) != a
    assert canonical_request_hash("reviews_claim", submit()) != a  # scoped by operation
    raw = {"case_id": str(CASE), "claim_token": TOKEN, "idempotency_key": "k" * 8}
    assert canonical_request_hash("reviews_release", raw) == canonical_request_hash(
        "reviews_release", {"case_id": str(CASE), "claim_token": TOKEN}
    )


def test_idempotency_same_key_same_hash_replays() -> None:
    h = canonical_request_hash("reviews_submit", submit())
    check = check_idempotency(
        _stored(h),
        principal_id=ALICE,
        operation="reviews_submit",
        idempotency_key="synthetic-key-0001",
        request_hash=h,
    )
    assert check.outcome == IdempotencyOutcome.REPLAY_RESULT
    assert check.stored is not None and check.stored.result == {"decision_id": str(UUID(int=800))}
    failed = check_idempotency(
        _stored(h, "failed"),
        principal_id=ALICE,
        operation="reviews_submit",
        idempotency_key="synthetic-key-0001",
        request_hash=h,
    )
    assert failed.outcome == IdempotencyOutcome.REPLAY_ERROR
    running = check_idempotency(
        _stored(h, "in_progress"),
        principal_id=ALICE,
        operation="reviews_submit",
        idempotency_key="synthetic-key-0001",
        request_hash=h,
    )
    assert running.outcome == IdempotencyOutcome.IN_PROGRESS


def test_idempotency_same_key_different_hash_conflicts() -> None:
    h1 = canonical_request_hash("reviews_submit", submit())
    h2 = canonical_request_hash("reviews_submit", submit(outcome=ReviewOutcome.REJECTED))
    with pytest.raises(IdempotencyConflict) as exc:
        check_idempotency(
            _stored(h1),
            principal_id=ALICE,
            operation="reviews_submit",
            idempotency_key="synthetic-key-0001",
            request_hash=h2,
        )
    assert exc.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_new_key_proceeds_and_scope_is_checked() -> None:
    h = canonical_request_hash("reviews_submit", submit())
    proceed = check_idempotency(
        None,
        principal_id=ALICE,
        operation="reviews_submit",
        idempotency_key="synthetic-key-0001",
        request_hash=h,
    )
    assert proceed.outcome == IdempotencyOutcome.PROCEED
    with pytest.raises(ValidationFailed):
        check_idempotency(
            _stored(h),
            principal_id=BOB,
            operation="reviews_submit",
            idempotency_key="synthetic-key-0001",
            request_hash=h,
        )


# ----------------------------------------------------------------------------------------- review regressions


def test_shortlist_must_cite_evidence() -> None:
    estimated = claimed(ALICE, valuation_state=ValuationState.ESTIMATED)
    with pytest.raises(ValidationFailed) as exc:
        evaluate_submit(
            estimated,
            actor(ALICE),
            submit(outcome=ReviewOutcome.SHORTLISTED, evidence_ids=()),
            now=NOW,
            guard=GOOD_GUARD,
        )
    assert "evidence" in exc.value.message
    # other outcomes may be decided without cited evidence (e.g. a rejection on listing facts)
    assert (
        evaluate_submit(
            claimed(ALICE), actor(ALICE), submit(outcome=ReviewOutcome.REJECTED, evidence_ids=()), now=NOW
        ).new_state
        == ReviewState.REJECTED
    )


def test_same_holder_reclaim_after_own_expiry_is_not_a_takeover() -> None:
    expired = claimed(ALICE, expires=NOW)
    own = evaluate_claim(expired, actor(ALICE), expected_version=2, now=NOW, token_factory=lambda: TOKEN)
    assert own.took_over_expired is False and own.rotated is False and own.row_version == 3
    other = evaluate_claim(expired, actor(BOB), expected_version=2, now=NOW, token_factory=lambda: TOKEN)
    assert other.took_over_expired is True


def test_holder_can_release_an_expired_claim_with_the_current_token() -> None:
    result = evaluate_release(claimed(ALICE, expires=NOW - timedelta(seconds=1)), actor(ALICE), TOKEN, NOW)
    assert result.changed and result.new_state == ReviewState.PENDING


def test_snapshot_rejects_decision_newer_than_case_revision() -> None:
    with pytest.raises(ValueError):
        case(
            state=ReviewState.WATCH,
            latest_decision_id=UUID(int=700),
            latest_decision_outcome=ReviewOutcome.WATCH,
            latest_decision_listing_revision=4,  # case is at revision 3
        )


def test_request_hash_is_stable_for_equivalent_validated_requests() -> None:
    # The idempotency hash is computed from the validated model, so an omitted default and an
    # explicit default (or surrounding whitespace in the summary) are the same request.
    raw = submit().model_dump(mode="json")
    explicit = parse_submit_request({**raw, "missing_information": [], "model_run_id": None})
    omitted = parse_submit_request(
        {k: v for k, v in raw.items() if k not in ("missing_information", "model_run_id")}
    )
    padded = parse_submit_request({**raw, "summary": "  " + raw["summary"] + "  "})
    hashes = {canonical_request_hash("reviews_submit", r) for r in (explicit, omitted, padded)}
    assert len(hashes) == 1
