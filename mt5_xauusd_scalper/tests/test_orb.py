"""ORB strategy tests: opening range, breakout entries, filters, session opens."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config import ORBConfig, SessionOpensConfig
from models import Direction, StrategyName
from strategy_orb import ORBStrategy, opening_range
from utils import active_session_open

TZ = "Europe/Skopje"


def m1_session(range_high, range_low, breakout_close, *, day="2026-06-01",
               open_hhmm="15:30", n_before=60, n_range=15, n_after=5):
    """Build M1 candles: warm-up, a 15-min opening range with given high/low,
    then a breakout candle closing at breakout_close."""
    rows = []
    start = pd.Timestamp(f"{day} 13:00", tz="UTC")  # 15:00 Skopje = 13:00 UTC (summer +2)
    # warm-up bars before the session open
    price = (range_high + range_low) / 2
    t = start
    for _ in range(n_before):
        rows.append((t, price, price + 0.2, price - 0.2, price))
        t += pd.Timedelta(minutes=1)
    # opening range 15:30-15:45 Skopje = 13:30-13:45 UTC
    open_utc = pd.Timestamp(f"{day} 13:30", tz="UTC")
    t = open_utc
    for k in range(n_range):
        hi = range_high if k == 3 else range_high - 0.3
        lo = range_low if k == 7 else range_low + 0.3
        rows.append((t, price, hi, lo, price))
        t += pd.Timedelta(minutes=1)
    # after the range: a breakout candle
    for k in range(n_after):
        c = breakout_close
        rows.append((t, price, max(price, c) + 0.1, min(price, c) - 0.1, c))
        t += pd.Timedelta(minutes=1)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["tick_volume"] = 100.0
    return df, open_utc + pd.Timedelta(minutes=15 + n_after - 1)


def make_strategy(**orb_over):
    opens = SessionOpensConfig(timezone=TZ, london="09:00", newyork="15:30")
    return ORBStrategy(ORBConfig(**orb_over), opens)


def test_opening_range_computation():
    df, _ = m1_session(2005.0, 2000.0, 2006.0)
    rng = opening_range(df, pd.Timestamp("2026-06-01 13:30", tz="UTC"),
                        pd.Timestamp("2026-06-01 13:45", tz="UTC"))
    assert rng is not None
    hi, lo = rng
    assert hi == pytest.approx(2005.0)
    assert lo == pytest.approx(2000.0)


def test_long_breakout_signal():
    df, now = m1_session(2005.0, 2000.0, 2006.5)  # closes above the range high
    strat = make_strategy()
    ev = strat.evaluate(df, now.to_pydatetime(), "XAUUSD", ["newyork"],
                        m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is not None, ev.rejections
    s = ev.signal
    assert s.direction is Direction.BUY
    assert s.strategy is StrategyName.ORB
    assert s.sl < s.entry < s.tp
    # TP = 2R
    assert (s.tp - s.entry) == pytest.approx(2.0 * (s.entry - s.sl))
    assert ev.session_name == "newyork"


def test_short_breakout_signal():
    df, now = m1_session(2005.0, 2000.0, 1998.5)  # closes below the range low
    ev = make_strategy().evaluate(df, now.to_pydatetime(), "XAUUSD", ["newyork"],
                                  m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is not None, ev.rejections
    assert ev.signal.direction is Direction.SELL
    assert ev.signal.tp < ev.signal.entry < ev.signal.sl


def test_no_breakout_inside_range():
    df, now = m1_session(2005.0, 2000.0, 2002.5)  # stays inside the range
    ev = make_strategy().evaluate(df, now.to_pydatetime(), "XAUUSD", ["newyork"],
                                  m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is None
    assert any("no breakout" in r for r in ev.rejections)


def test_range_too_small_rejected():
    df, now = m1_session(2000.5, 2000.0, 2001.5)  # 0.5 range, ATR 3 -> 0.17 ATR < min 0.5
    ev = make_strategy().evaluate(df, now.to_pydatetime(), "XAUUSD", ["newyork"],
                                  m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is None
    assert any("too small" in r for r in ev.rejections)


def test_chasing_breakout_rejected():
    df, now = m1_session(2005.0, 2000.0, 2011.0)  # closes 6 beyond level, ATR 3 -> 2 ATR extension
    ev = make_strategy(max_breakout_extension_atr=1.0).evaluate(
        df, now.to_pydatetime(), "XAUUSD", ["newyork"], m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is None
    assert any("chasing" in r or "extended" in r for r in ev.rejections)


def test_outside_session_no_signal():
    df, now = m1_session(2005.0, 2000.0, 2006.5)
    # instrument only trades london, but this is the NY window
    ev = make_strategy().evaluate(df, now.to_pydatetime(), "XAUUSD", ["london"],
                                  m5_atr=3.0, point=0.01, spread_points=18)
    assert ev.signal is None
    assert any("session" in r for r in ev.rejections)


# ------------------------------------------------------------------ session opens

def test_active_session_open_detects_ny_window():
    opens = SessionOpensConfig(timezone=TZ, newyork="15:30")
    now = datetime(2026, 6, 1, 16, 0, tzinfo=ZoneInfo(TZ))  # Monday, 30 min after NY open
    res = active_session_open(opens, ["newyork"], now, range_minutes=15, entry_window_minutes=90)
    assert res is not None
    name, open_dt, range_end, deadline = res
    assert name == "newyork"
    assert open_dt.hour == 15 and open_dt.minute == 30


def test_active_session_open_outside_window():
    opens = SessionOpensConfig(timezone=TZ, newyork="15:30")
    now = datetime(2026, 6, 1, 20, 0, tzinfo=ZoneInfo(TZ))  # well past the entry window
    assert active_session_open(opens, ["newyork"], now, 15, 90) is None


def test_active_session_open_skips_weekend():
    opens = SessionOpensConfig(timezone=TZ, newyork="15:30")
    sat = datetime(2026, 6, 6, 16, 0, tzinfo=ZoneInfo(TZ))  # Saturday
    assert active_session_open(opens, ["newyork"], sat, 15, 90) is None
