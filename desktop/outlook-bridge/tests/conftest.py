"""Fixtures for the desktop Outlook bridge tests (Linux; in-memory fakes only).

Run with ``cd /home/user/Lokal69 && uv run pytest desktop/outlook-bridge/tests -q``. The repository
root ``testpaths`` is ``["tests"]``, so these tests run only when targeted explicitly.

No real Outlook, COM, network, mailbox or e-mail is touched: Outlook is ``FakeOutlook``, the backend
is ``FakeBackend`` behind ``httpx.MockTransport``, the credential store is in memory, and all
addresses are synthetic ``example.invalid`` values.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
for _path in (_TESTS_DIR.parent, _TESTS_DIR):  # the outlook_bridge package and bridge_support
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bridge_support import Harness  # noqa: E402


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        h.close()


@pytest.fixture
def make_harness(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    created: list[Harness] = []

    def factory(**config_overrides: Any) -> Harness:
        h = Harness(tmp_path / f"h{len(created)}", config_overrides=config_overrides)
        created.append(h)
        return h

    try:
        yield factory
    finally:
        for h in created:
            h.close()
