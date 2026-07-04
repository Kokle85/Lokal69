"""Time/session helpers and small shared utilities."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import SessionsConfig


def now_in_tz(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def in_session(cfg: SessionsConfig, now: datetime | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). Friday cutoff applies even if the session filter is disabled."""
    local = now.astimezone(ZoneInfo(cfg.timezone)) if now else now_in_tz(cfg.timezone)

    if local.weekday() == 4 and local.time() >= parse_hhmm(cfg.block_friday_after):
        return False, f"Friday after {cfg.block_friday_after} ({cfg.timezone}) is blocked"
    if local.weekday() >= 5:
        return False, "weekend"

    # Data-driven hour filter (from backtest pnl_by_hour); applies even when
    # the window filter is disabled.
    if cfg.allowed_hours and local.hour not in cfg.allowed_hours:
        return False, f"hour {local.hour:02d} not in allowed_hours {sorted(cfg.allowed_hours)}"

    if not cfg.enabled:
        return True, "session filter disabled"

    for window in cfg.windows:
        if parse_hhmm(window.start) <= local.time() < parse_hhmm(window.end):
            return True, f"inside session {window.start}-{window.end}"
    return False, "outside allowed trading sessions"


def price_to_points(distance: float, point: float) -> float:
    return distance / point if point > 0 else 0.0


def points_to_price(points: float, point: float) -> float:
    return points * point


def round_price(value: float, digits: int) -> float:
    return round(value, digits)


def fmt_usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"
