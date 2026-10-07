"""Single STA thread with a message pump; interactive-session guard (spec 37.6)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from outlook_bridge.errors import ComObjectLeak, NonInteractiveSession, StaError, StaNotRunning, StaTimeout
from outlook_bridge.sta_runtime import (
    SessionContext,
    StaExecutor,
    ensure_plain,
    probe_session_context,
    require_interactive_session,
    session_refusal_reasons,
)
from outlook_bridge.testing import INTERACTIVE_SESSION, FakeComApi, FakeOutlook
from pydantic import BaseModel

SYSTEM_SERVICE = SessionContext(
    platform="win32",
    user_sid="S-1-5-18",
    session_id=0,
    window_station="Service-0x0-3e7$",
    window_station_visible=False,
)


# -------------------------------------------------------------------------------------------- guard


def test_interactive_user_session_is_accepted() -> None:
    assert session_refusal_reasons(INTERACTIVE_SESSION) == ()
    require_interactive_session(INTERACTIVE_SESSION)


def test_system_service_context_is_refused_before_any_com_call() -> None:
    com = FakeComApi()
    with pytest.raises(NonInteractiveSession) as info:
        StaExecutor(com, SYSTEM_SERVICE)
    assert {"SYSTEM_ACCOUNT", "SERVICE_SESSION_0", "NON_INTERACTIVE_WINDOW_STATION"} <= set(
        info.value.reasons
    )
    assert com.initialized_threads == []  # CoInitialize never ran
    assert info.value.code == "NON_INTERACTIVE_SESSION"


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-19",
                session_id=1,
                window_station="WinSta0",
                window_station_visible=True,
            ),
            "LOCAL_SERVICE_ACCOUNT",
        ),
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-20",
                session_id=1,
                window_station="WinSta0",
                window_station_visible=True,
            ),
            "NETWORK_SERVICE_ACCOUNT",
        ),
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-21-1-2-3-1001",
                session_id=0,
                window_station="WinSta0",
                window_station_visible=True,
            ),
            "SERVICE_SESSION_0",
        ),
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-21-1-2-3-1001",
                session_id=2,
                window_station="Service-0x0-3e5$",
                window_station_visible=False,
            ),
            "NON_INTERACTIVE_WINDOW_STATION",
        ),
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-21-1-2-3-1001",
                session_id=2,
                window_station="WinSta0",
                window_station_visible=False,
            ),
            "NON_INTERACTIVE_WINDOW_STATION",
        ),
        (SessionContext(platform="win32"), "USER_UNKNOWN"),
        (
            SessionContext(
                platform="win32",
                user_sid="S-1-5-21-1",
                session_id=None,
                window_station="WinSta0",
                window_station_visible=True,
            ),
            "SESSION_UNKNOWN",
        ),
        (SessionContext(platform="win32", user_sid="S-1-5-21-1", session_id=1), "WINDOW_STATION_UNKNOWN"),
        (
            SessionContext(
                platform="linux",
                user_sid="S-1-5-21-1",
                session_id=1,
                window_station="WinSta0",
                window_station_visible=True,
            ),
            "NOT_WINDOWS",
        ),
    ],
)
def test_non_interactive_contexts_are_refused(context: SessionContext, reason: str) -> None:
    assert reason in session_refusal_reasons(context)
    with pytest.raises(NonInteractiveSession):
        StaExecutor(FakeComApi(), context)


def test_unknown_context_fails_closed_on_this_platform() -> None:
    """On Linux (tests) the real probe cannot verify an interactive Windows session."""
    reasons = session_refusal_reasons(probe_session_context())
    assert reasons  # never an empty refusal list outside a verified interactive Windows session


# ----------------------------------------------------------------------------------------- executor


def test_calls_run_on_one_dedicated_initialised_sta_thread() -> None:
    com = FakeComApi()
    with StaExecutor(com, INTERACTIVE_SESSION, pump_interval=0.005) as sta:
        idents = {sta.call(threading.get_ident) for _ in range(5)}
        assert len(idents) == 1
        sta_ident = idents.pop()
        assert sta_ident != threading.get_ident()
        assert com.initialized_threads == [sta_ident]
        assert sta.call(sta.on_sta_thread) is True
        assert sta.on_sta_thread() is False
        time.sleep(0.03)
        assert com.pumps > 0  # live message pump between tasks
    assert com.uninitialized == 1
    assert sta.running is False


def test_events_posted_to_the_pump_run_on_the_sta_thread() -> None:
    com = FakeComApi()
    seen: list[int] = []
    done = threading.Event()
    with StaExecutor(com, INTERACTIVE_SESSION, pump_interval=0.005) as sta:
        sta_ident = sta.call(threading.get_ident)

        def callback() -> None:
            seen.append(threading.get_ident())
            done.set()

        com.post(callback)
        assert done.wait(2)
    assert seen == [sta_ident]


@dataclass(frozen=True)
class _Plain:
    when: datetime
    names: tuple[str, ...]


class _Model(BaseModel):
    value: int


class _ComLike:
    """Stands in for a COM object accidentally returned from the STA thread."""


def test_plain_results_pass_and_com_objects_never_leave_the_sta_thread() -> None:
    with StaExecutor(FakeComApi(), INTERACTIVE_SESSION, pump_interval=0.005) as sta:

        def plain() -> dict[str, list[object]]:
            return {"a": [1, 2.5, None, b"x"], "b": [_Plain(datetime.now(UTC), ("x",))]}

        assert sta.call(plain)["a"][0] == 1
        assert sta.call(lambda: _Model(value=3)).value == 3
        assert sta.call(uuid4) is not None
        with pytest.raises(ComObjectLeak):
            sta.call(_ComLike)
        with pytest.raises(ComObjectLeak):
            sta.call(lambda: [{"nested": _ComLike()}])
        future = sta.submit(_ComLike)
        with pytest.raises(ComObjectLeak):  # verified on the STA thread, before the hand-over
            future.result(2)


def test_fake_outlook_objects_are_not_plain() -> None:
    outlook = FakeOutlook(lambda: datetime.now(UTC))
    with pytest.raises(ComObjectLeak):
        ensure_plain(outlook.app)
    with pytest.raises(ComObjectLeak):
        ensure_plain([[[[[[[[[[[[[[1]]]]]]]]]]]]]])  # nesting too deep to verify


def test_exceptions_propagate_and_the_thread_survives() -> None:
    def boom() -> None:
        raise ValueError("bad")

    with StaExecutor(FakeComApi(), INTERACTIVE_SESSION, pump_interval=0.005) as sta:
        with pytest.raises(ValueError, match="bad"):
            sta.call(boom)
        assert sta.call(lambda: 41 + 1) == 42


def test_timeout_and_stopped_executor() -> None:
    release = threading.Event()
    sta = StaExecutor(FakeComApi(), INTERACTIVE_SESSION, pump_interval=0.005)
    sta.start()
    try:
        with pytest.raises(StaTimeout):
            sta.call(lambda: release.wait(5), timeout=0.05)
    finally:
        release.set()
        sta.stop()
    with pytest.raises(StaNotRunning):
        sta.call(lambda: 1)
    with pytest.raises(StaError):
        sta.start()  # an executor is single-use


def test_com_initialisation_failure_is_reported() -> None:
    sta = StaExecutor(FakeComApi(fail_initialize=True), INTERACTIVE_SESSION)
    with pytest.raises(StaError, match="COM initialisation failed"):
        sta.start()
    assert sta.running is False


def test_shutdown_hooks_run_on_the_sta_thread_before_uninitialize() -> None:
    com = FakeComApi()
    order: list[str] = []
    sta = StaExecutor(com, INTERACTIVE_SESSION, pump_interval=0.005)
    sta.start()
    sta_ident = sta.call(threading.get_ident)
    hook_threads: list[int] = []

    def hook() -> None:
        hook_threads.append(threading.get_ident())
        order.append(f"hook-uninit={com.uninitialized}")

    def failing_hook() -> None:
        raise RuntimeError("release failed")

    sta.add_shutdown_hook(failing_hook)
    sta.add_shutdown_hook(hook)
    sta.stop()
    assert hook_threads == [sta_ident]
    assert order == ["hook-uninit=0"]
    assert com.uninitialized == 1


def test_pump_interval_validation() -> None:
    with pytest.raises(ValueError, match="pump_interval"):
        StaExecutor(FakeComApi(), INTERACTIVE_SESSION, pump_interval=5)
