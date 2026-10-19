"""Regression tests from the independent C1 review: pure rules (no database, no network).

- ``queries.inquiries.waiting_reason``: the initial mode ``disabled_until_sender_ready`` is an
  incomplete sender setup (never "paused": the owner paused nothing); a ``failed_definite``
  inquiry is not waiting for the workspace pause unless a send job holds its guarded retry (a
  definite rejection is final); a rolling cap the owner set to 0 is a visible wait.
- ``errors_map.map_db_error``: lock timeouts, serialization failures and deadlocks keep
  ``VERSION_CONFLICT`` + retryable with the stable ``details.reason = busy``.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from suv_deals.errors import ErrorCode
from suv_deals.persistence.errors_map import TransientConflict, map_db_error
from suv_deals.persistence.queries.inquiries import waiting_reason


def _row(state: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "state": state,
        "controls_kill_switch": False,
        "controls_mode": "automatic",
        "controls_max_per_24h": 2,
        "controls_max_per_15d": 5,
        "send_job_code": None,
        "plan_job_code": None,
        "local_intent_running": False,
        "worker_box_id": None,
        "worker_fresh": False,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("state", ["qualifying", "reserved", "queued"])
def test_initial_mode_is_an_incomplete_sender_setup_not_a_pause(state: str) -> None:
    assert (
        waiting_reason(_row(state, controls_mode="disabled_until_sender_ready")) == "SENDER_SETUP_INCOMPLETE"
    )
    assert waiting_reason(_row(state, controls_mode=None)) == "SENDER_SETUP_INCOMPLETE"
    assert waiting_reason(_row(state, controls_mode="paused")) == "INQUIRIES_PAUSED"
    assert waiting_reason(_row(state, controls_kill_switch=True)) == "INQUIRIES_PAUSED"
    # The kill switch wins over an incomplete setup (the owner's explicit stop is the reason).
    assert (
        waiting_reason(_row(state, controls_kill_switch=True, controls_mode="disabled_until_sender_ready"))
        == "INQUIRIES_PAUSED"
    )


def test_a_final_failure_is_not_waiting_for_the_pause() -> None:
    for mode in ("paused", "disabled_until_sender_ready"):
        assert waiting_reason(_row("failed_definite", controls_mode=mode)) is None
    assert waiting_reason(_row("failed_definite", controls_kill_switch=True)) is None
    # A proven-unsent failure whose send job holds the guarded retry during the pause waits.
    held = _row("failed_definite", controls_kill_switch=True, send_job_code="SEND_HELD_PAUSED")
    assert waiting_reason(held) == "INQUIRIES_PAUSED"


def test_a_cap_set_to_zero_is_a_visible_wait() -> None:
    assert waiting_reason(_row("qualifying", controls_max_per_24h=0)) == "RATE_CAP_REACHED"
    assert waiting_reason(_row("queued", controls_max_per_15d=0)) == "RATE_CAP_REACHED"
    # The open job's own wait code is more specific.
    cooling = _row("queued", controls_max_per_24h=0, send_job_code="INQUIRY_WAIT_SELLER_COOLDOWN")
    assert waiting_reason(cooling) == "SELLER_COOLDOWN"
    assert waiting_reason(_row("queued")) is None
    assert waiting_reason(_row("failed_definite", controls_max_per_24h=0)) is None


def test_in_flight_and_final_states_keep_their_reasons() -> None:
    assert waiting_reason(_row("uncertain", controls_mode="paused")) == "UNCERTAIN_DELIVERY"
    assert waiting_reason(_row("held_facts")) == "NEEDS_FACTS"
    assert waiting_reason(_row("accepted", controls_mode="paused")) is None
    offline = _row("sending", controls_mode="paused", local_intent_running=True, worker_box_id=None)
    assert waiting_reason(offline) == "WORKER_OFFLINE"
    online = _row("sending", local_intent_running=True, worker_box_id="box", worker_fresh=True)
    assert waiting_reason(online) is None


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.LockNotAvailable(),
        psycopg.errors.SerializationFailure(),
        psycopg.errors.DeadlockDetected(),
    ],
)
def test_database_contention_maps_to_a_busy_transient_conflict(error: psycopg.Error) -> None:
    mapped = map_db_error(error)
    assert isinstance(mapped, TransientConflict)
    assert mapped.code == ErrorCode.VERSION_CONFLICT and mapped.retryable
    assert mapped.details == {"reason": "busy"}
