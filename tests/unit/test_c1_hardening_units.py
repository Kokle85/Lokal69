"""Pure rules of work package C1: the 7-day seller-cooldown floor (item 2), the transient
conflict reasons (item 9) and the canary helpers (item 12). No database, no network."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from suv_deals.domain.inquiries import MAX_SELLER_COOLDOWN, SELLER_COOLDOWN, RateCapPolicy
from suv_deals.errors import ErrorCode, ValidationFailed
from suv_deals.integrations.mime_builder import parse_inquiry_message_id
from suv_deals.persistence import canaries_repo
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.views.inquiries import MIN_SELLER_COOLDOWN_SECONDS, InquiryControlView


@pytest.mark.parametrize(
    "cooldown",
    [timedelta(0), timedelta(hours=1), timedelta(days=6, hours=23, minutes=59), timedelta(days=366)],
)
def test_rate_cap_policy_refuses_a_cooldown_outside_7_to_365_days(cooldown: timedelta) -> None:
    with pytest.raises(ValidationError):
        RateCapPolicy(seller_cooldown=cooldown)


@pytest.mark.parametrize("cooldown", [SELLER_COOLDOWN, timedelta(days=30), MAX_SELLER_COOLDOWN])
def test_rate_cap_policy_accepts_a_wider_cooldown(cooldown: timedelta) -> None:
    assert RateCapPolicy(seller_cooldown=cooldown).seller_cooldown == cooldown


def test_default_policy_is_the_owner_decision() -> None:
    policy = RateCapPolicy()
    assert (policy.max_per_24h, policy.max_per_15d, policy.seller_cooldown) == (2, 5, timedelta(days=7))
    assert int(SELLER_COOLDOWN.total_seconds()) == MIN_SELLER_COOLDOWN_SECONDS


def test_control_view_never_reports_a_cooldown_below_the_floor() -> None:
    fields = InquiryControlView.model_fields["seller_cooldown_seconds"]
    assert any(getattr(m, "ge", None) == MIN_SELLER_COOLDOWN_SECONDS for m in fields.metadata)


def test_transient_conflict_reasons_keep_the_retryable_code() -> None:
    busy = TransientConflict()
    assert busy.code == ErrorCode.VERSION_CONFLICT and busy.retryable
    assert busy.details == {"reason": "busy"}
    running = TransientConflict.in_progress()
    assert running.code == ErrorCode.VERSION_CONFLICT and running.retryable
    assert running.details == {"reason": "in_progress"}
    assert "in progress" in running.message
    with pytest.raises(ValueError, match="unknown transient conflict reason"):
        TransientConflict(reason="other")  # type: ignore[arg-type]


def test_canary_message_id_is_never_an_inquiry_message_id() -> None:
    canary_id = uuid.uuid4()
    value = canaries_repo.canary_message_id(canary_id, "Inquiries@Synthetic-Mail.EXAMPLE")
    assert value == f"<canary-{canary_id}@synthetic-mail.example>"
    assert parse_inquiry_message_id(value) is None
    with pytest.raises(ValidationFailed):
        canaries_repo.canary_message_id(canary_id, "no-domain")


def test_canary_target_hash_is_case_insensitive_and_never_the_address() -> None:
    lower = canaries_repo.target_address_hash("owner-test@canary.example.invalid")
    upper = canaries_repo.target_address_hash("Owner-Test@CANARY.example.invalid")
    assert lower == upper and len(lower) == 64 and "@" not in lower
    with pytest.raises(ValidationFailed):
        canaries_repo.target_address_hash("owner-test@canary.example.invalid\r\nBcc: x@y.invalid")


@pytest.mark.parametrize(
    "evidence",
    [
        {"to": "owner-test@canary.example.invalid"},
        {"nested": {"a": 1}},
        {"list": [1, 2]},
        {"Upper": 1},
        {"text": "line\nbreak"},
        {"text": "x" * 201},
        {"ratio": float("nan")},
        {f"k{i}": i for i in range(21)},
    ],
)
def test_canary_evidence_is_allow_listed(evidence: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        canaries_repo.sanitize_evidence(evidence)


def test_canary_evidence_keeps_codes_counts_and_flags() -> None:
    clean = canaries_repo.sanitize_evidence({"submission": "sent_items_confirmed", "attempts": 1, "ok": True})
    assert clean == {"submission": "sent_items_confirmed", "attempts": 1, "ok": True}
    assert canaries_repo.sanitize_evidence(None) == {}
