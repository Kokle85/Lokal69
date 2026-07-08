"""Technical indicators implemented with pandas/numpy only (no TA-Lib).

All candle DataFrames are expected to have columns:
    time (datetime64), open, high, low, close, tick_volume
sorted ascending by time, with only CLOSED candles.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- core series

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, bounded to [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0: RSI is 100 if there were gains, neutral 50 if flat.
    out = out.where(avg_loss > 0, np.where(avg_gain > 0, 100.0, 50.0))
    return out.clip(0.0, 100.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def vwap(df: pd.DataFrame, tz: str = "UTC") -> pd.Series:
    """Intraday VWAP from M1 data, reset at each local midnight in `tz`.

    vwap = cum(typical_price * volume) / cum(volume), typical = (H+L+C)/3
    """
    times = pd.to_datetime(df["time"])
    if times.dt.tz is None:
        times = times.dt.tz_localize("UTC")
    local_dates = times.dt.tz_convert(tz).dt.date

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["tick_volume"].astype(float).replace(0.0, 1.0)
    pv = typical * volume

    cum_pv = pv.groupby(local_dates).cumsum()
    cum_vol = volume.groupby(local_dates).cumsum()
    return cum_pv / cum_vol


# ------------------------------------------------------------- candle anatomy

def candle_body(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def upper_wick(row: pd.Series) -> float:
    return float(row["high"]) - max(float(row["open"]), float(row["close"]))


def lower_wick(row: pd.Series) -> float:
    return min(float(row["open"]), float(row["close"])) - float(row["low"])


def candle_range(row: pd.Series) -> float:
    return float(row["high"]) - float(row["low"])


def wick_body_ratio(row: pd.Series) -> float:
    body = candle_body(row)
    wick = upper_wick(row) + lower_wick(row)
    if body <= 0:
        return float("inf") if wick > 0 else 0.0
    return wick / body


def is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


# ----------------------------------------------------------- rejection candles

def is_bullish_rejection(row: pd.Series, min_wick_body_ratio: float = 1.5) -> bool:
    """Closed bullish candle, lower wick >= ratio * body, close in upper 40% of range."""
    rng = candle_range(row)
    body = candle_body(row)
    if rng <= 0 or body <= 0 or not is_bullish(row):
        return False
    if lower_wick(row) < min_wick_body_ratio * body:
        return False
    close_pos = (float(row["close"]) - float(row["low"])) / rng
    return close_pos >= 0.60


def is_bearish_rejection(row: pd.Series, min_wick_body_ratio: float = 1.5) -> bool:
    """Closed bearish candle, upper wick >= ratio * body, close in lower 40% of range."""
    rng = candle_range(row)
    body = candle_body(row)
    if rng <= 0 or body <= 0 or not is_bearish(row):
        return False
    if upper_wick(row) < min_wick_body_ratio * body:
        return False
    close_pos = (float(row["close"]) - float(row["low"])) / rng
    return close_pos <= 0.40


# ------------------------------------------------------------------ swings

def swing_high_flags(df: pd.DataFrame, window: int = 2) -> pd.Series:
    """True where high is greater than the prior `window` highs and >= the next ones.

    Ties with following bars count (first bar of an equal-high group is the swing);
    ties with preceding bars do not, so each swing is flagged exactly once.
    """
    highs = df["high"]
    flags = pd.Series(False, index=df.index)
    for i in range(window, len(df) - window):
        h = highs.iloc[i]
        prev = highs.iloc[i - window : i]
        nxt = highs.iloc[i + 1 : i + window + 1]
        if (prev < h).all() and (nxt <= h).all():
            flags.iloc[i] = True
    return flags


def swing_low_flags(df: pd.DataFrame, window: int = 2) -> pd.Series:
    lows = df["low"]
    flags = pd.Series(False, index=df.index)
    for i in range(window, len(df) - window):
        low = lows.iloc[i]
        prev = lows.iloc[i - window : i]
        nxt = lows.iloc[i + 1 : i + window + 1]
        if (prev > low).all() and (nxt >= low).all():
            flags.iloc[i] = True
    return flags


def recent_swing_low(df: pd.DataFrame, window: int = 2, lookback: int = 30) -> Optional[float]:
    tail = df.tail(lookback).reset_index(drop=True)
    flags = swing_low_flags(tail, window)
    lows = tail.loc[flags, "low"]
    return float(lows.iloc[-1]) if not lows.empty else None


def recent_swing_high(df: pd.DataFrame, window: int = 2, lookback: int = 30) -> Optional[float]:
    tail = df.tail(lookback).reset_index(drop=True)
    flags = swing_high_flags(tail, window)
    highs = tail.loc[flags, "high"]
    return float(highs.iloc[-1]) if not highs.empty else None


# ------------------------------------------------------------- structure

def _last_swing_values(df: pd.DataFrame, window: int, lookback: int) -> tuple[list[float], list[float]]:
    tail = df.tail(lookback).reset_index(drop=True)
    hi_flags = swing_high_flags(tail, window)
    lo_flags = swing_low_flags(tail, window)
    highs = [float(v) for v in tail.loc[hi_flags, "high"]]
    lows = [float(v) for v in tail.loc[lo_flags, "low"]]
    return highs, lows


def has_higher_highs_higher_lows(df: pd.DataFrame, window: int = 2, lookback: int = 40) -> bool:
    """Last two swing highs ascending AND last two swing lows ascending."""
    highs, lows = _last_swing_values(df, window, lookback)
    if len(highs) < 2 or len(lows) < 2:
        return False
    return highs[-1] > highs[-2] and lows[-1] > lows[-2]


def has_lower_highs_lower_lows(df: pd.DataFrame, window: int = 2, lookback: int = 40) -> bool:
    """Last two swing highs descending AND last two swing lows descending."""
    highs, lows = _last_swing_values(df, window, lookback)
    if len(highs) < 2 or len(lows) < 2:
        return False
    return highs[-1] < highs[-2] and lows[-1] < lows[-2]


# ------------------------------------------------------------- liquidity sweep

def detect_liquidity_sweep(
    df: pd.DataFrame,
    bullish: bool,
    atr_now: float,
    lookback: int = 20,
    max_sweep_atr: float = 1.2,
    recent: int = 5,
) -> bool:
    """True when price recently swept liquidity beyond the prior range and reclaimed it.

    Bullish: one of the last `recent` bars dipped below the low of the preceding
    `lookback` bars (stop hunt) by at most max_sweep_atr * ATR, and the latest
    close is back above that level. Bearish is the mirror image.
    """
    if atr_now <= 0 or len(df) < lookback + recent:
        return False
    window = df.iloc[-(lookback + recent) : -recent]
    tail = df.iloc[-recent:]
    if bullish:
        ref = float(window["low"].min())
        extreme = float(tail["low"].min())
        swept = extreme < ref
        depth_ok = (ref - extreme) <= max_sweep_atr * atr_now
        reclaimed = float(tail["close"].iloc[-1]) > ref
    else:
        ref = float(window["high"].max())
        extreme = float(tail["high"].max())
        swept = extreme > ref
        depth_ok = (extreme - ref) <= max_sweep_atr * atr_now
        reclaimed = float(tail["close"].iloc[-1]) < ref
    return swept and depth_ok and reclaimed


# --------------------------------------------------------------- filters

def is_choppy_market(
    df: pd.DataFrame,
    ema_fast: pd.Series,
    wick_body_ratio_limit: float = 1.8,
    lookback: int = 5,
    ema_cross_limit: int = 3,
) -> tuple[bool, str]:
    """Choppy if candles alternate with large wicks, wicks dominate bodies,
    or price keeps crossing the fast EMA."""
    tail = df.tail(lookback)
    if len(tail) < 3:
        return False, ""

    bodies = (tail["close"] - tail["open"]).abs()
    wicks = (tail["high"] - tail["low"]) - bodies
    avg_body = float(bodies.mean())
    avg_wick = float(wicks.mean())
    if avg_body <= 0 or avg_wick > wick_body_ratio_limit * avg_body:
        return True, "average wick dominates body"

    directions = np.sign(tail["close"].to_numpy() - tail["open"].to_numpy())
    nonzero = directions[directions != 0]
    if len(nonzero) >= 3:
        alternating = all(nonzero[i] != nonzero[i + 1] for i in range(len(nonzero) - 1))
        wicky = avg_wick > avg_body
        if alternating and wicky:
            return True, "candles alternate direction with large wicks"

    ema_tail = ema_fast.tail(lookback)
    above = (tail["close"].to_numpy() > ema_tail.to_numpy()).astype(int)
    crossings = int(np.abs(np.diff(above)).sum())
    if crossings >= ema_cross_limit:
        return True, f"price crossed EMA {crossings} times in last {lookback} candles"

    return False, ""


def is_spike_candle(
    df: pd.DataFrame,
    atr_series: pd.Series,
    atr_multiplier: float = 2.5,
) -> tuple[bool, str]:
    """Spike if the current or previous candle range exceeds atr_multiplier * ATR."""
    if len(df) < 2 or len(atr_series) < 2:
        return False, ""
    atr_now = float(atr_series.iloc[-1])
    if atr_now <= 0 or np.isnan(atr_now):
        return False, ""
    for offset, label in ((-1, "current"), (-2, "previous")):
        row = df.iloc[offset]
        if candle_range(row) > atr_multiplier * atr_now:
            return True, f"{label} candle range > {atr_multiplier}x ATR"
    return False, ""
