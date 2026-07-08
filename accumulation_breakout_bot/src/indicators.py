"""Hand-rolled indicators: no TA library dependency, deterministic, testable."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR over an OHLC frame with columns high/low/close."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI over a close series (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_loss.notna(), other=float("nan"))


def last_closed_htf_rsi(m5: pd.DataFrame, minutes: int, period: int = 14):
    """RSI on a higher timeframe resampled from M5, using ONLY fully closed
    HTF bars relative to the last closed M5 candle (no peeking into the
    still-forming H1/H4 bar). Returns (rsi_last, rsi_prev) or (nan, nan)."""
    closes = (m5.set_index("time")["close"]
                .resample(f"{minutes}min", label="left", closed="left").last().dropna())
    if len(closes) < 3:
        return float("nan"), float("nan")
    last_m5_close = m5["time"].iloc[-1] + pd.Timedelta(minutes=5)
    # a HTF bar opened at O is closed once O + minutes <= last_m5_close
    closed = closes[closes.index + pd.Timedelta(minutes=minutes) <= last_m5_close]
    if len(closed) < period + 2:
        return float("nan"), float("nan")
    series = rsi(closed, period)
    return float(series.iloc[-1]), float(series.iloc[-2])


def linear_slope(values: np.ndarray) -> float:
    """Least-squares slope per bar of a value series (price units / bar)."""
    n = len(values)
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=float)
    return float(np.polyfit(x, values.astype(float), 1)[0])
