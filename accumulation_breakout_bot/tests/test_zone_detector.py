"""Zone detector: accepts the textbook accumulation, rejects everything else."""
import numpy as np

from conftest import flat_zone_candles, make_candles
from indicators import atr
from zone_detector import detect_zone


def _window_and_atr(specs):
    df = make_candles(specs)
    atr_value = float(atr(df, 14).iloc[-1])
    return df, atr_value


def test_valid_accumulation_zone_detected(cfg):
    df, atr_value = _window_and_atr(flat_zone_candles())
    result = detect_zone(df, atr_value, cfg)
    assert result.zone is not None, f"rejected: {result.rejections}"
    zone = result.zone
    assert zone.high == 2001.0
    assert zone.low == 2000.0
    assert zone.upper_touches >= cfg.minimum_upper_wick_touches
    assert zone.lower_touches >= cfg.minimum_lower_wick_touches


def test_not_enough_upper_touches_rejected(cfg):
    df, atr_value = _window_and_atr(flat_zone_candles(upper_touches=2))
    result = detect_zone(df, atr_value, cfg)
    assert result.zone is None
    assert any("upper wick touches" in r for r in result.rejections)


def test_not_enough_lower_touches_rejected(cfg):
    df, atr_value = _window_and_atr(flat_zone_candles(lower_touches=2))
    result = detect_zone(df, atr_value, cfg)
    assert result.zone is None
    assert any("lower wick touches" in r for r in result.rejections)


def test_zone_too_small_rejected(cfg):
    # tight range while the wider-market ATR (passed in) is large:
    # the zone spans well under 0.8 x ATR and must be rejected
    specs = flat_zone_candles(low=2000.0, high=2000.2)
    df, _ = _window_and_atr(specs)
    result = detect_zone(df, atr_value=1.0, cfg=cfg)
    assert result.zone is None
    assert any("too small" in r for r in result.rejections)


def test_trending_window_rejected(cfg):
    # steady uptrend: same candle shape, drifting up 0.15/bar
    specs = []
    for i in range(30):
        base = 2000.0 + 0.15 * i
        specs.append((base, base + 0.3, base - 0.3, base + 0.1))
    df, atr_value = _window_and_atr(specs)
    result = detect_zone(df, atr_value, cfg)
    assert result.zone is None
    assert any("trending" in r or "too large" in r for r in result.rejections)


def test_bodies_outside_zone_rejected(cfg):
    # bodies regularly closing beyond the extremes -> messy, not accumulation
    specs = flat_zone_candles()
    # rewrite 40% of candles with bodies pinned to the top boundary
    for i in range(0, 12):
        specs[i] = (2000.9, 2001.0, 2000.7, 2001.0)  # body closes AT the high
    df, atr_value = _window_and_atr(specs)
    result = detect_zone(df, atr_value, cfg)
    # either the body-containment or the touch counting must kill it
    assert result.zone is None


def test_short_window_rejected(cfg):
    df, atr_value = _window_and_atr(flat_zone_candles(n=20))
    result = detect_zone(df, atr_value, cfg)
    assert result.zone is None
    assert any("not enough candles" in r for r in result.rejections)


def test_atr_unavailable_rejected(cfg):
    df, _ = _window_and_atr(flat_zone_candles())
    result = detect_zone(df, float("nan"), cfg)
    assert result.zone is None
