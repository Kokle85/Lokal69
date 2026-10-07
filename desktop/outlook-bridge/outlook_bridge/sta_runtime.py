"""Single dedicated STA thread for every Outlook Object Model call (spec 37.6).

Rules enforced here:

- The worker refuses to touch Outlook unless it runs as the signed-in *interactive* user: not
  ``SYSTEM``/``LocalService``/``NetworkService``, not in the services session (session 0), and on
  the visible interactive window station ``WinSta0``. When the context cannot be verified the
  answer is a refusal (fail closed). Nothing here changes Outlook, Trust Center or antivirus
  settings.
- One thread calls ``CoInitializeEx(COINIT_APARTMENTTHREADED)``, runs a live message pump
  (``PumpWaitingMessages``) between tasks so Outlook events (``NewMailEx``) are delivered, and
  executes every marshalled call. Other threads only submit callables and receive results.
- Results must be plain data (scalars, containers, dataclasses, pydantic models). A COM object
  never leaves the STA thread (``ComObjectLeak``); callbacks copy plain values and return
  immediately, heavy work (matching, uploads) happens on the worker thread.

pywin32 is imported lazily; ``ComApi``/``SessionProbe`` are injectable so tests run on Linux.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any, Final, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

from outlook_bridge.errors import ComObjectLeak, NonInteractiveSession, StaError, StaNotRunning, StaTimeout

T = TypeVar("T")

#: Well-known service principals that must never automate Outlook.
SERVICE_SIDS: Final[dict[str, str]] = {
    "S-1-5-18": "SYSTEM_ACCOUNT",
    "S-1-5-19": "LOCAL_SERVICE_ACCOUNT",
    "S-1-5-20": "NETWORK_SERVICE_ACCOUNT",
}
INTERACTIVE_WINDOW_STATION: Final = "winsta0"
_MAX_PLAIN_DEPTH: Final = 12


def _import(name: str) -> Any:
    """Import an optional (Windows-only) module lazily; typed ``Any`` on purpose."""
    return importlib.import_module(name)


# =============================================================================================
# Session guard
# =============================================================================================


@dataclasses.dataclass(frozen=True, slots=True)
class SessionContext:
    """What the process knows about its own logon/session context (no secrets)."""

    platform: str
    user_sid: str | None = None
    session_id: int | None = None
    window_station: str | None = None
    window_station_visible: bool | None = None
    probe_errors: tuple[str, ...] = ()


class SessionProbe(Protocol):
    def __call__(self) -> SessionContext: ...


def probe_session_context() -> SessionContext:
    """Inspect the current Windows logon session via pywin32 (read-only)."""
    if sys.platform != "win32":
        return SessionContext(platform=sys.platform)
    errors: list[str] = []
    sid: str | None = None
    session_id: int | None = None
    station: str | None = None
    visible: bool | None = None
    try:
        win32api = _import("win32api")
        win32security = _import("win32security")
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid = str(win32security.ConvertSidToStringSid(user_sid))
    except Exception as exc:  # pragma: no cover - Windows only
        errors.append(f"user_sid:{type(exc).__name__}")
    try:
        win32ts = _import("win32ts")
        session_id = int(win32ts.ProcessIdToSessionId(os.getpid()))
    except Exception as exc:  # pragma: no cover - Windows only
        errors.append(f"session_id:{type(exc).__name__}")
    try:
        win32service = _import("win32service")
        win32con = _import("win32con")
        handle = win32service.GetProcessWindowStation()
        station = str(win32service.GetUserObjectInformation(handle, win32con.UOI_NAME))
        flags = win32service.GetUserObjectInformation(handle, win32con.UOI_FLAGS)
        visible = bool(int(flags["Flags"]) & win32con.WSF_VISIBLE)
    except Exception as exc:  # pragma: no cover - Windows only
        errors.append(f"window_station:{type(exc).__name__}")
    return SessionContext(
        platform=sys.platform,
        user_sid=sid,
        session_id=session_id,
        window_station=station,
        window_station_visible=visible,
        probe_errors=tuple(errors),
    )


def session_refusal_reasons(ctx: SessionContext) -> tuple[str, ...]:
    """Why Outlook automation is refused in this context; empty means interactive user."""
    reasons: list[str] = []
    if ctx.platform != "win32":
        reasons.append("NOT_WINDOWS")
    if ctx.user_sid is None:
        reasons.append("USER_UNKNOWN")
    elif ctx.user_sid.upper() in SERVICE_SIDS:
        reasons.append(SERVICE_SIDS[ctx.user_sid.upper()])
    if ctx.session_id is None:
        reasons.append("SESSION_UNKNOWN")
    elif ctx.session_id == 0:
        reasons.append("SERVICE_SESSION_0")
    if ctx.window_station is None or ctx.window_station_visible is None:
        reasons.append("WINDOW_STATION_UNKNOWN")
    elif ctx.window_station.casefold() != INTERACTIVE_WINDOW_STATION or not ctx.window_station_visible:
        reasons.append("NON_INTERACTIVE_WINDOW_STATION")
    return tuple(dict.fromkeys(reasons))


def require_interactive_session(ctx: SessionContext) -> None:
    reasons = session_refusal_reasons(ctx)
    if reasons:
        raise NonInteractiveSession(reasons)


# =============================================================================================
# COM apartment API
# =============================================================================================


class ComApi(Protocol):
    """The three pythoncom calls the STA thread needs (injectable for tests)."""

    def co_initialize(self) -> None: ...

    def co_uninitialize(self) -> None: ...

    def pump_waiting_messages(self) -> None: ...


class PythonComApi:  # pragma: no cover - requires pywin32 on Windows
    """Real implementation over ``pythoncom``."""

    def __init__(self) -> None:
        self._pythoncom: Any = None

    def co_initialize(self) -> None:
        self._pythoncom = _import("pythoncom")
        self._pythoncom.CoInitializeEx(self._pythoncom.COINIT_APARTMENTTHREADED)

    def co_uninitialize(self) -> None:
        if self._pythoncom is not None:
            self._pythoncom.CoUninitialize()

    def pump_waiting_messages(self) -> None:
        if self._pythoncom is not None:
            self._pythoncom.PumpWaitingMessages()


# =============================================================================================
# Plain-data guard
# =============================================================================================

_PLAIN_SCALARS: Final = (type(None), bool, int, float, str, bytes, datetime, date, Decimal, UUID, Enum, PurePath)
_COM_MODULE_PREFIXES: Final = ("win32com", "pythoncom", "pywintypes", "win32")


def ensure_plain(value: object, *, _depth: int = 0) -> None:
    """Raise ``ComObjectLeak`` unless ``value`` is plain, thread-safe data."""
    if _depth > _MAX_PLAIN_DEPTH:
        raise ComObjectLeak("result nesting too deep to verify")
    if isinstance(value, _PLAIN_SCALARS):
        if isinstance(value, datetime | date) or not type(value).__module__.startswith(_COM_MODULE_PREFIXES):
            return
    if isinstance(value, list | tuple | set | frozenset):
        for item in value:
            ensure_plain(item, _depth=_depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_plain(key, _depth=_depth + 1)
            ensure_plain(item, _depth=_depth + 1)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            ensure_plain(getattr(value, f.name), _depth=_depth + 1)
        return
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            ensure_plain(getattr(value, name), _depth=_depth + 1)
        return
    raise ComObjectLeak(f"non-plain value of type {type(value).__name__} must not leave the STA thread")


# =============================================================================================
# STA executor
# =============================================================================================


class _Task:
    __slots__ = ("fn", "future")

    def __init__(self, fn: Callable[[], Any], future: Future[Any]) -> None:
        self.fn = fn
        self.future = future


class StaExecutor:
    """Runs callables on one dedicated COM STA thread with a live message pump."""

    def __init__(
        self,
        com: ComApi,
        session: SessionContext,
        *,
        pump_interval: float = 0.05,
        start_timeout: float = 30.0,
        name: str = "outlook-sta",
    ) -> None:
        require_interactive_session(session)  # refuse SYSTEM/service/non-interactive before any COM
        if not 0.001 <= pump_interval <= 1.0:
            raise ValueError("pump_interval out of range")
        self._com = com
        self._pump_interval = pump_interval
        self._start_timeout = start_timeout
        self._name = name
        self._tasks: queue.Queue[_Task | None] = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._init_error: BaseException | None = None
        self._shutdown_hooks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise StaError("STA executor already started")
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
        if not self._ready.wait(self._start_timeout):
            raise StaTimeout("STA thread did not initialise in time")
        if self._init_error is not None:
            raise StaError(f"COM initialisation failed ({type(self._init_error).__name__})")

    def stop(self, timeout: float = 10.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._tasks.put(None)
        thread.join(timeout)

    def __enter__(self) -> StaExecutor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._init_error is None

    def on_sta_thread(self) -> bool:
        return self._thread_ident is not None and threading.get_ident() == self._thread_ident

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        """Run ``hook`` on the STA thread before ``CoUninitialize`` (release COM references)."""
        self._shutdown_hooks.append(hook)

    # ------------------------------------------------------------------ calls

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        if not self.running or self._stop.is_set():
            raise StaNotRunning("STA thread is not running")
        future: Future[T] = Future()
        self._tasks.put(_Task(fn, future))
        return future

    def call(self, fn: Callable[[], T], *, timeout: float = 120.0) -> T:
        """Run ``fn`` on the STA thread and return its (verified plain) result."""
        if self.on_sta_thread():
            result = fn()
        else:
            future = self.submit(fn)
            try:
                result = future.result(timeout)
            except FutureTimeout:
                raise StaTimeout("Outlook call timed out on the STA thread") from None
        ensure_plain(result)
        return result

    # ------------------------------------------------------------------ thread body

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        try:
            self._com.co_initialize()
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                self._com.pump_waiting_messages()
                try:
                    task = self._tasks.get(timeout=self._pump_interval)
                except queue.Empty:
                    continue
                if task is None:
                    continue
                if not task.future.set_running_or_notify_cancel():
                    continue
                try:
                    task.future.set_result(task.fn())
                except BaseException as exc:
                    task.future.set_exception(exc)
        finally:
            self._drain_pending()
            for hook in self._shutdown_hooks:
                try:
                    hook()
                except Exception:  # noqa: S110 - releasing COM references must not block shutdown
                    pass
            self._com.co_uninitialize()

    def _drain_pending(self) -> None:
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                return
            if task is not None and task.future.set_running_or_notify_cancel():
                task.future.set_exception(StaNotRunning("STA thread stopped"))


__all__ = [
    "INTERACTIVE_WINDOW_STATION",
    "SERVICE_SIDS",
    "ComApi",
    "PythonComApi",
    "SessionContext",
    "SessionProbe",
    "StaExecutor",
    "ensure_plain",
    "probe_session_context",
    "require_interactive_session",
    "session_refusal_reasons",
]
