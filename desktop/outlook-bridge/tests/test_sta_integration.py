"""The full worker over the STA facade with strict apartment checks (spec 37.6).

Every fake COM access off the STA thread raises and is recorded, so a run without violations shows
that all Outlook Object Model calls were marshalled onto the single STA thread, that NewMailEx is
delivered by the message pump on that thread, and that only plain data crossed back.
"""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest
from bridge_support import Harness
from outlook_bridge.outlook_adapter import StaMailbox
from outlook_bridge.sta_runtime import StaExecutor
from outlook_bridge.testing import INTERACTIVE_SESSION, FakeComApi, FakeComError
from outlook_bridge.worker import BridgeWorker


class RecordingWorker(BridgeWorker):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.event_threads: list[int] = []

    def on_new_mail(self, entry_ids: tuple[str, ...]) -> None:
        self.event_threads.append(threading.get_ident())
        super().on_new_mail(entry_ids)


def _wait(predicate: object, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


def test_worker_over_the_sta_thread_without_apartment_violations(harness: Harness) -> None:
    com = FakeComApi()
    executor = StaExecutor(com, INTERACTIVE_SESSION, pump_interval=0.005)
    executor.start()
    try:
        sta_ident = executor.call(threading.get_ident)
        outlook = harness.outlook
        outlook.use_com_api(com)
        outlook.strict_sta(sta_ident)
        mailbox = StaMailbox(executor, harness.session, timeout=10)
        worker = RecordingWorker(
            config=harness.config,
            store=harness.store,
            mailbox=mailbox,
            api=harness.api,
            credentials=harness.credentials,
            clock=harness.clock,
            compat=harness.compat,
        )
        first, second = uuid4(), uuid4()
        harness.bind(first)
        harness.bind(second)
        with outlook.root.controller():
            harness.deliver_reply(first, fire_event=False)
        worker.start()
        assert len(harness.reply_posts()) == 1
        with outlook.root.controller():
            harness.deliver_reply(second, fire_event=True)  # posted to the pump, like a real COM event
        assert _wait(lambda: worker.event_threads)
        report = worker.tick()
        assert report.events["uploaded"] == 1
        assert worker.event_threads == [sta_ident]  # NewMailEx ran on the STA thread
        assert outlook.violations == []
        assert len(harness.reply_posts()) == 2
        # Direct off-thread access is refused by the strict fake (the guard is real) ...
        with pytest.raises(FakeComError):
            _ = outlook.namespace.Accounts
        # ... and the session degrades to "Outlook unavailable" instead of crashing.
        assert harness.session.connection_state().outlook_running is False
        assert "Accounts" in outlook.violations
    finally:
        executor.stop()
    assert com.uninitialized == 1


def test_sta_mailbox_closes_event_sinks_on_shutdown(harness: Harness) -> None:
    com = FakeComApi()
    executor = StaExecutor(com, INTERACTIVE_SESSION, pump_interval=0.005)
    executor.start()
    mailbox = StaMailbox(executor, harness.session, timeout=10)
    mailbox.connect(harness.config.account_smtp_address)
    calls: list[tuple[str, ...]] = []
    mailbox.subscribe_new_mail(calls.append)
    handlers = list(harness.outlook.root.handlers)
    assert handlers and handlers[0]._bridge_sink is not None
    mailbox.close()
    assert handlers[0]._bridge_sink is None
    executor.stop()
    mailbox.close()  # closing again after the executor stopped is a no-op
