"""Accumulation zone detection.

A valid zone is a sideways range whose boundaries were repeatedly REJECTED
with wicks: highs poke at the zone high but bodies close back below it, lows
poke at the zone low but bodies close back above it. Trending, messy, too
small or too large windows are rejected - each with an explicit reason so the
journal can explain every "no trade" day.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config_loader import Settings
from indicators import linear_slope
from models import Zone, ZoneResult


def detect_zone(window: pd.DataFrame, atr_value: float, cfg: Settings) -> ZoneResult:
    """Scan one lookback window of CLOSED M5 candles for an accumulation zone.

    `window` must contain exactly the candles to examine (the breakout candle
    itself is NOT part of the window - the zone must exist before the break).
    """
    result = ZoneResult()
    if len(window) < cfg.zone_lookback_candles:
        result.rejections.append(
            f"not enough candles: {len(window)} < {cfg.zone_lookback_candles}"
        )
        return result
    if atr_value <= 0 or np.isnan(atr_value):
        result.rejections.append("ATR unavailable")
        return result

    zone_high = float(window["high"].max())
    zone_low = float(window["low"].min())
    size = zone_high - zone_low

    # ---- size filters
    if size < cfg.min_zone_size_atr * atr_value:
        result.rejections.append(
            f"zone too small: {size:.2f} < {cfg.min_zone_size_atr} x ATR ({atr_value:.2f})"
        )
        return result
    if size > cfg.max_zone_size_atr * atr_value:
        result.rejections.append(
            f"zone too large: {size:.2f} > {cfg.max_zone_size_atr} x ATR ({atr_value:.2f})"
        )
        return result

    # ---- trend/slope filter: a range must be flat, not a channel
    slope = linear_slope(window["close"].to_numpy())
    total_drift = abs(slope) * len(window)
    if total_drift > cfg.max_zone_slope_atr * atr_value:
        result.rejections.append(
            f"window is trending: drift {total_drift:.2f} > "
            f"{cfg.max_zone_slope_atr} x ATR ({atr_value:.2f})"
        )
        return result

    # ---- body containment: bodies must live inside the range
    tol = cfg.touch_tolerance_atr_multiplier * atr_value
    body_hi = np.maximum(window["open"].to_numpy(), window["close"].to_numpy())
    body_lo = np.minimum(window["open"].to_numpy(), window["close"].to_numpy())
    inside = (body_hi <= zone_high + 1e-12) & (body_lo >= zone_low - 1e-12)
    inside_pct = 100.0 * float(inside.mean())
    if inside_pct < cfg.min_body_inside_zone_percent:
        result.rejections.append(
            f"too many bodies outside the zone: {inside_pct:.0f}% inside < "
            f"{cfg.min_body_inside_zone_percent:.0f}% required"
        )
        return result

    # ---- wick-touch counting (the signature of accumulation)
    highs = window["high"].to_numpy()
    lows = window["low"].to_numpy()
    closes = window["close"].to_numpy()
    upper_touches = int(np.sum((highs >= zone_high - tol) & (closes < zone_high)))
    lower_touches = int(np.sum((lows <= zone_low + tol) & (closes > zone_low)))

    if upper_touches < cfg.minimum_upper_wick_touches:
        result.rejections.append(
            f"not enough upper wick touches: {upper_touches} < {cfg.minimum_upper_wick_touches}"
        )
        return result
    if lower_touches < cfg.minimum_lower_wick_touches:
        result.rejections.append(
            f"not enough lower wick touches: {lower_touches} < {cfg.minimum_lower_wick_touches}"
        )
        return result

    result.zone = Zone(
        high=zone_high,
        low=zone_low,
        upper_touches=upper_touches,
        lower_touches=lower_touches,
        start_time=window["time"].iloc[0].to_pydatetime(),
        end_time=window["time"].iloc[-1].to_pydatetime(),
        atr=float(atr_value),
    )
    return result
