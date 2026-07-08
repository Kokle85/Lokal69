"""London / New York session windows in BROKER time.

The datetimes compared here must come from the SAME clock as the configured
windows - i.e. M5 bar labels (broker/server time), never the local wall
clock. Mixing time bases is the classic way session filters silently break.
"""
from __future__ import annotations

from datetime import datetime, time

from config_loader import SessionsConfig


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def active_session(sessions: SessionsConfig, now: datetime) -> str | None:
    """Name of the session `now` falls into, or None. Weekend = None."""
    if now.weekday() >= 5:  # Saturday/Sunday on the broker clock
        return None
    t = now.time()
    for name in ("london", "newyork"):
        window = getattr(sessions, name)
        if _parse(window.start) <= t < _parse(window.end):
            return name
    return None
