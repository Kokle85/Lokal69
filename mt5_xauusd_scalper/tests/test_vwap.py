"""VWAP calculation and daily reset tests."""
import pandas as pd

import indicators as ind
from conftest import make_candles


def test_vwap_formula():
    df = make_candles([2000.0, 2001.0, 2002.0], pad=0.5)
    out = ind.vwap(df, "UTC")
    # manual: typical = (H+L+C)/3, equal volume -> running mean of typicals
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    expected0 = typical.iloc[0]
    expected1 = (typical.iloc[0] + typical.iloc[1]) / 2.0
    assert abs(out.iloc[0] - expected0) < 1e-9
    assert abs(out.iloc[1] - expected1) < 1e-9


def test_vwap_weights_by_volume():
    df = make_candles([2000.0, 2010.0], pad=0.0)
    df.loc[0, "tick_volume"] = 1.0
    df.loc[1, "tick_volume"] = 99.0
    out = ind.vwap(df, "UTC")
    # heavy volume on the second candle pulls VWAP close to its typical price
    typical1 = (df["high"].iloc[1] + df["low"].iloc[1] + df["close"].iloc[1]) / 3.0
    assert abs(out.iloc[1] - typical1) < 0.2


def test_vwap_daily_reset():
    # two candles late on day 1, two candles early on day 2 (UTC dates differ)
    times = [
        pd.Timestamp("2026-01-05 23:58", tz="UTC"),
        pd.Timestamp("2026-01-05 23:59", tz="UTC"),
        pd.Timestamp("2026-01-06 00:00", tz="UTC"),
        pd.Timestamp("2026-01-06 00:01", tz="UTC"),
    ]
    df = make_candles([2000.0, 2000.0, 2100.0, 2100.0], pad=0.0)
    df["time"] = times
    out = ind.vwap(df, "UTC")
    # day 2 VWAP must ignore day 1 prices entirely
    typical_day2 = (df["high"].iloc[2] + df["low"].iloc[2] + df["close"].iloc[2]) / 3.0
    assert abs(out.iloc[2] - typical_day2) < 1e-9
    assert out.iloc[2] > 2050  # not dragged down by day 1


def test_vwap_reset_respects_timezone():
    # 22:30 and 23:30 UTC are the same calendar day in UTC but different days
    # in Europe/Skopje (UTC+1) -> reset must happen between them.
    times = [
        pd.Timestamp("2026-01-05 22:30", tz="UTC"),
        pd.Timestamp("2026-01-05 23:30", tz="UTC"),
    ]
    df = make_candles([2000.0, 2020.0], pad=0.0)
    df["time"] = times
    out = ind.vwap(df, "Europe/Skopje")
    typical1 = (df["high"].iloc[1] + df["low"].iloc[1] + df["close"].iloc[1]) / 3.0
    assert abs(out.iloc[1] - typical1) < 1e-9
