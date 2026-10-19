"""Which budget-gate refusals release a job for free (``crawling.detail.lifts_on_its_own``).

A refusal that ends by itself at a known time (a wait, an open circuit, an exhausted daily
budget) is budget pressure: the job is released without consuming an attempt. A refusal without
a known end (a zero per-run cap refuses the first request of every run; a source without a
budget) is configuration and must consume attempts, otherwise the job would cycle forever and
never become a visible dead letter. An access block is never released.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from suv_deals.crawling.detail import lifts_on_its_own
from suv_deals.crawling.policy_client import BudgetRefused
from suv_deals.crawling.rate_limits import BudgetDecision, Deny, DenyReason, Wait, WaitReason

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=10)


@pytest.mark.parametrize(
    ("decision", "released"),
    [
        (Wait(until=LATER, reason=WaitReason.RETRY_AFTER), True),
        (Wait(until=LATER, reason=WaitReason.MIN_DELAY), True),
        (Wait(until=LATER, reason=WaitReason.BACKOFF), True),
        (Wait(until=LATER, reason=WaitReason.NAVIGATION_IN_FLIGHT), True),
        (Deny(reason=DenyReason.CIRCUIT_OPEN, until=LATER), True),
        (Deny(reason=DenyReason.BUDGET_EXHAUSTED, until=LATER), True),
        (Deny(reason=DenyReason.RUN_CAP_REACHED), False),
        (Deny(reason=DenyReason.BUDGET_EXHAUSTED), False),  # no budget for the source at all
        (Deny(reason=DenyReason.CIRCUIT_OPEN), False),
        (Deny(reason=DenyReason.ACCESS_BLOCKED), False),
        (Deny(reason=DenyReason.ACCESS_BLOCKED, until=LATER), False),
        (Deny(reason=DenyReason.RUN_CAP_REACHED, until=LATER), False),
    ],
)
def test_only_refusals_with_a_known_end_are_released_for_free(
    decision: BudgetDecision, released: bool
) -> None:
    assert lifts_on_its_own(BudgetRefused(decision, now=NOW)) is released
