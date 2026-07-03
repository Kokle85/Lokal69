"""Market regime classification from M15/M5/M1 data."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import indicators as ind
from config import RegimeConfig, StrategyTuningConfig
from models import Regime, RegimeResult


@dataclass
class MarketSnapshot:
    """Pre-computed inputs the regime detector and strategies share."""

    m15: pd.DataFrame
    m5: pd.DataFrame
    m1: pd.DataFrame
    m15_ema_fast: pd.Series
    m15_ema_slow: pd.Series
    m5_ema_fast: pd.Series
    m5_ema_slow: pd.Series
    m1_ema_fast: pd.Series
    m1_ema_slow: pd.Series
    m1_atr: pd.Series
    m5_atr: pd.Series
    m1_rsi: pd.Series
    vwap: pd.Series
    spread_points: float
    point: float

    @property
    def price(self) -> float:
        return float(self.m1["close"].iloc[-1])

    @property
    def vwap_now(self) -> float:
        return float(self.vwap.iloc[-1])

    @property
    def atr_now(self) -> float:
        return float(self.m1_atr.iloc[-1])

    @property
    def atr_points(self) -> float:
        return self.atr_now / self.point if self.point > 0 else 0.0


def build_snapshot(
    m15: pd.DataFrame,
    m5: pd.DataFrame,
    m1: pd.DataFrame,
    tuning: StrategyTuningConfig,
    spread_points: float,
    point: float,
    vwap_tz: str = "UTC",
) -> MarketSnapshot:
    return MarketSnapshot(
        m15=m15,
        m5=m5,
        m1=m1,
        m15_ema_fast=ind.ema(m15["close"], tuning.ema_fast),
        m15_ema_slow=ind.ema(m15["close"], tuning.ema_slow),
        m5_ema_fast=ind.ema(m5["close"], tuning.ema_fast),
        m5_ema_slow=ind.ema(m5["close"], tuning.ema_slow),
        m1_ema_fast=ind.ema(m1["close"], tuning.ema_fast),
        m1_ema_slow=ind.ema(m1["close"], tuning.ema_slow),
        m1_atr=ind.atr(m1, tuning.atr_period),
        m5_atr=ind.atr(m5, tuning.atr_period),
        m1_rsi=ind.rsi(m1["close"], tuning.rsi_period),
        vwap=ind.vwap(m1, vwap_tz),
        spread_points=spread_points,
        point=point,
    )


class RegimeDetector:
    def __init__(self, cfg: RegimeConfig, max_spread_points: float) -> None:
        self.cfg = cfg
        self.max_spread_points = max_spread_points

    def detect(self, snap: MarketSnapshot) -> RegimeResult:
        reasons: list[str] = []

        # --- SPIKE (checked first: safety over opportunity)
        spike, spike_reason = ind.is_spike_candle(
            snap.m1, snap.m1_atr, self.cfg.spike_atr_multiplier
        )
        if spike:
            return RegimeResult(Regime.SPIKE, [spike_reason])
        if snap.spread_points > 2.0 * self.max_spread_points:
            return RegimeResult(
                Regime.SPIKE, [f"spread {snap.spread_points:.0f}pt widened suddenly"]
            )

        # --- DEAD_MARKET
        if snap.atr_points < self.cfg.min_atr_points:
            return RegimeResult(
                Regime.DEAD_MARKET,
                [f"M1 ATR {snap.atr_points:.0f}pt below minimum {self.cfg.min_atr_points:.0f}pt"],
            )
        recent_ranges = (snap.m1["high"] - snap.m1["low"]).tail(5)
        if float(recent_ranges.mean()) / snap.point < self.cfg.min_atr_points * 0.5:
            return RegimeResult(Regime.DEAD_MARKET, ["recent candle ranges too small"])
        if snap.spread_points > 0 and snap.spread_points > 0.8 * snap.atr_points:
            return RegimeResult(
                Regime.DEAD_MARKET, ["spread too large relative to ATR"]
            )

        # --- CHOPPY
        choppy, choppy_reason = ind.is_choppy_market(
            snap.m1, snap.m1_ema_fast, self.cfg.choppy_wick_body_ratio
        )
        m15_up = float(snap.m15_ema_fast.iloc[-1]) > float(snap.m15_ema_slow.iloc[-1])
        m5_up = float(snap.m5_ema_fast.iloc[-1]) > float(snap.m5_ema_slow.iloc[-1])
        if m15_up != m5_up:
            choppy, choppy_reason = True, "M15 and M5 trend conflict"
        if choppy:
            return RegimeResult(Regime.CHOPPY, [choppy_reason])

        atr_normal = self.cfg.min_atr_points <= snap.atr_points <= self.cfg.max_atr_points
        spread_ok = snap.spread_points <= self.max_spread_points

        # --- TREND_UP
        if (
            m15_up
            and m5_up
            and snap.price > snap.vwap_now
            and ind.has_higher_highs_higher_lows(snap.m5)
            and atr_normal
            and spread_ok
        ):
            reasons.append("M15+M5 EMA bullish, price above VWAP, HH/HL structure")
            return RegimeResult(Regime.TREND_UP, reasons)

        # --- TREND_DOWN
        if (
            not m15_up
            and not m5_up
            and snap.price < snap.vwap_now
            and ind.has_lower_highs_lower_lows(snap.m5)
            and atr_normal
            and spread_ok
        ):
            reasons.append("M15+M5 EMA bearish, price below VWAP, LH/LL structure")
            return RegimeResult(Regime.TREND_DOWN, reasons)

        return RegimeResult(Regime.RANGE, ["no clean trend alignment"])
