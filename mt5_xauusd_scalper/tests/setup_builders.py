"""Hand-crafted market snapshots that satisfy (or nearly satisfy) each setup.

Prices are constructed so the documented entry rules hold: pullback near EMA,
rejection candle, SL swing within ATR bounds, RSI zone, structure break, retest.
"""
from __future__ import annotations

import pandas as pd

from conftest import START, downtrend_m15, downtrend_m5, make_candles, uptrend_m15, uptrend_m5, zigzag_closes
from regime_detector import MarketSnapshot, build_snapshot


def df_from_ohlc(bars: list[tuple[float, float, float, float]], start=START, freq_min=1) -> pd.DataFrame:
    times = [start + pd.Timedelta(minutes=freq_min * i) for i in range(len(bars))]
    return pd.DataFrame(
        {
            "time": times,
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "tick_volume": [100.0] * len(bars),
        }
    )


def _base_bars(closes: list[float], pad: float) -> list[tuple[float, float, float, float]]:
    bars = []
    prev = closes[0]
    for c in closes:
        o = prev
        bars.append((o, max(o, c) + pad, min(o, c) - pad, c))
        prev = c
    return bars


def hp_buy_m1() -> pd.DataFrame:
    """Uptrend, 5-bar pullback, 2 recovery bars, bullish rejection near EMA20."""
    closes = zigzag_closes(170, 2000.0, up=0.12, down=0.08, up_run=3, down_run=1)
    bars = _base_bars(closes, pad=0.12)
    p = closes[-1]
    tail = [
        (p, p + 0.06, p - 0.21, p - 0.15),
        (p - 0.15, p - 0.09, p - 0.36, p - 0.30),
        (p - 0.30, p - 0.24, p - 0.51, p - 0.45),
        (p - 0.45, p - 0.39, p - 0.66, p - 0.60),
        (p - 0.60, p - 0.54, p - 0.81, p - 0.75),   # swing low at p-0.81
        (p - 0.75, p - 0.56, p - 0.79, p - 0.60),
        (p - 0.60, p - 0.46, p - 0.64, p - 0.50),
        (p - 0.52, p - 0.42, p - 0.70, p - 0.44),   # bullish rejection
    ]
    return df_from_ohlc(bars + tail)


def hp_sell_m1() -> pd.DataFrame:
    """Mirror image: downtrend, rally, bearish rejection."""
    closes = zigzag_closes(170, 2030.0, up=0.12, down=0.08, up_run=3, down_run=1, rising=False)
    bars = _base_bars(closes, pad=0.12)
    q = closes[-1]
    tail = [
        (q, q + 0.21, q - 0.06, q + 0.15),
        (q + 0.15, q + 0.36, q + 0.09, q + 0.30),
        (q + 0.30, q + 0.51, q + 0.24, q + 0.45),
        (q + 0.45, q + 0.66, q + 0.39, q + 0.60),
        (q + 0.60, q + 0.81, q + 0.54, q + 0.75),   # swing high at q+0.81
        (q + 0.75, q + 0.79, q + 0.56, q + 0.60),
        (q + 0.60, q + 0.64, q + 0.46, q + 0.50),
        (q + 0.52, q + 0.70, q + 0.42, q + 0.44),   # bearish rejection
    ]
    return df_from_ohlc(bars + tail)


def hp_buy_snapshot(tuning, spread_points: float = 10.0) -> MarketSnapshot:
    return build_snapshot(uptrend_m15(), uptrend_m5(), hp_buy_m1(), tuning, spread_points, 0.01)


def hp_sell_snapshot(tuning, spread_points: float = 10.0) -> MarketSnapshot:
    return build_snapshot(downtrend_m15(), downtrend_m5(), hp_sell_m1(), tuning, spread_points, 0.01)


# ------------------------------------------------------------------ momentum

def momentum_buy_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(m5, m1): M5 structure-high break, M1 retest and continuation."""
    m5_closes = zigzag_closes(150, 1980.0, up=0.5, down=0.3, up_run=4, down_run=2)
    m5_bars = _base_bars(m5_closes, pad=0.15)
    lvl = max(b[1] for b in m5_bars[-20:])  # structure high of the last 20 base bars
    last = m5_closes[-1]
    m5_bars += [
        (last, lvl - 0.05, last - 0.30, lvl - 0.10),
        (lvl - 0.10, lvl + 0.85, lvl - 0.15, lvl + 0.80),  # strong breakout bar
        (lvl + 0.80, lvl + 0.85, lvl + 0.25, lvl + 0.35),
    ]
    m5 = df_from_ohlc(m5_bars, start=START - pd.Timedelta(minutes=5 * len(m5_bars)), freq_min=5)

    m1_closes = zigzag_closes(195, lvl - 13.8, up=0.12, down=0.08, up_run=3, down_run=1)
    m1_bars = _base_bars(m1_closes, pad=0.12)
    a = m1_closes[-1]
    m1_bars += [
        (a, lvl + 0.15, a - 0.05, lvl + 0.10),
        (lvl + 0.10, lvl + 0.50, lvl + 0.05, lvl + 0.45),
        (lvl + 0.45, lvl + 0.47, lvl + 0.05, lvl + 0.15),  # retest of the level
        (lvl + 0.15, lvl + 0.25, lvl + 0.10, lvl + 0.20),
        (lvl + 0.20, lvl + 0.37, lvl + 0.15, lvl + 0.35),  # continuation close
    ]
    m1 = df_from_ohlc(m1_bars)
    return m5, m1


def momentum_sell_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    m5_closes = zigzag_closes(150, 2050.0, up=0.5, down=0.3, up_run=4, down_run=2, rising=False)
    m5_bars = _base_bars(m5_closes, pad=0.15)
    lvl = min(b[2] for b in m5_bars[-20:])  # structure low
    last = m5_closes[-1]
    m5_bars += [
        (last, last + 0.30, lvl + 0.05, lvl + 0.10),
        (lvl + 0.10, lvl + 0.15, lvl - 0.55, lvl - 0.50),  # breakdown bar
        (lvl - 0.50, lvl - 0.25, lvl - 0.55, lvl - 0.35),
    ]
    m5 = df_from_ohlc(m5_bars, start=START - pd.Timedelta(minutes=5 * len(m5_bars)), freq_min=5)

    m1_closes = zigzag_closes(195, lvl + 13.8, up=0.12, down=0.08, up_run=3, down_run=1, rising=False)
    m1_bars = _base_bars(m1_closes, pad=0.12)
    a = m1_closes[-1]
    m1_bars += [
        (a, a + 0.05, lvl - 0.15, lvl - 0.10),
        (lvl - 0.10, lvl - 0.05, lvl - 0.50, lvl - 0.45),
        (lvl - 0.45, lvl - 0.05, lvl - 0.47, lvl - 0.15),  # retest of the level
        (lvl - 0.15, lvl - 0.10, lvl - 0.25, lvl - 0.20),
        (lvl - 0.20, lvl - 0.15, lvl - 0.37, lvl - 0.35),  # continuation close
    ]
    m1 = df_from_ohlc(m1_bars)
    return m5, m1


def momentum_buy_snapshot(tuning, spread_points: float = 10.0) -> MarketSnapshot:
    m5, m1 = momentum_buy_frames()
    return build_snapshot(uptrend_m15(), m5, m1, tuning, spread_points, 0.01)


def momentum_sell_snapshot(tuning, spread_points: float = 10.0) -> MarketSnapshot:
    m5, m1 = momentum_sell_frames()
    return build_snapshot(downtrend_m15(), m5, m1, tuning, spread_points, 0.01)
