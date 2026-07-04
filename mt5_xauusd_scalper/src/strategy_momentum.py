"""Momentum Scalp strategy: M5 structure break + M1 retest continuation."""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

import indicators as ind
from config import MomentumConfig, StrategyTuningConfig
from models import Direction, Regime, Signal, StrategyName
from regime_detector import MarketSnapshot
from strategy_high_precision import EvaluationResult

MAX_SCORE = 11


class MomentumStrategy:
    name = StrategyName.MOMENTUM

    def __init__(
        self,
        cfg: MomentumConfig,
        tuning: StrategyTuningConfig,
        max_spread_points: float,
        min_atr_points: float,
        max_atr_points: float,
    ) -> None:
        self.cfg = cfg
        self.tuning = tuning
        self.max_spread_points = max_spread_points
        self.min_atr_points = min_atr_points
        self.max_atr_points = max_atr_points

    def evaluate(self, snap: MarketSnapshot, regime: Regime) -> EvaluationResult:
        if not self.cfg.enabled:
            return EvaluationResult(None, ["momentum strategy disabled"])
        if regime is Regime.TREND_UP:
            return self._evaluate_direction(snap, regime, Direction.BUY)
        if regime is Regime.TREND_DOWN:
            return self._evaluate_direction(snap, regime, Direction.SELL)
        return EvaluationResult(None, [f"regime {regime.value} not tradeable for momentum"])

    # ------------------------------------------------------------------ core

    def _evaluate_direction(
        self, snap: MarketSnapshot, regime: Regime, direction: Direction
    ) -> EvaluationResult:
        rejections: list[str] = []
        t = self.tuning
        buying = direction is Direction.BUY
        price = snap.price
        atr_now = snap.atr_now
        if atr_now <= 0:
            return EvaluationResult(None, ["ATR not available"])

        m15_aligned = (
            float(snap.m15_ema_fast.iloc[-1]) > float(snap.m15_ema_slow.iloc[-1])
        ) == buying
        m5_aligned = (
            float(snap.m5_ema_fast.iloc[-1]) > float(snap.m5_ema_slow.iloc[-1])
        ) == buying
        vwap_aligned = (price > snap.vwap_now) == buying
        if not vwap_aligned:
            rejections.append("price on wrong side of VWAP")

        # --- structure break: recent M5 extreme (excluding the freshest bars)
        lookback = t.m5_structure_lookback
        m5_hist = snap.m5.iloc[-(lookback + 3) : -3]
        if len(m5_hist) < 5:
            return EvaluationResult(None, ["not enough M5 history for structure"])
        level = float(m5_hist["high"].max()) if buying else float(m5_hist["low"].min())

        recent_m5 = snap.m5.iloc[-3:]
        if buying:
            broke = float(recent_m5["high"].max()) > level
        else:
            broke = float(recent_m5["low"].min()) < level
        if not broke:
            rejections.append("no recent M5 structure break")

        # --- breakout candle strong but not a spike
        breakout_bar = recent_m5.iloc[
            int(recent_m5["high"].to_numpy().argmax()) if buying
            else int(recent_m5["low"].to_numpy().argmin())
        ]
        breakout_range = ind.candle_range(breakout_bar)
        m5_atr_now = float(snap.m5_atr.iloc[-1])
        strong = m5_atr_now > 0 and breakout_range >= 0.8 * m5_atr_now
        spikey = m5_atr_now > 0 and breakout_range > t.spike_atr_multiplier * m5_atr_now
        if not strong:
            rejections.append("breakout candle too weak")
        if spikey:
            rejections.append("breakout candle is a spike")
        valid_break = broke and strong and not spikey

        # --- M1 retest of the broken level + continuation close
        retest_tol = t.retest_tolerance_atr * atr_now
        m1_recent = snap.m1.tail(5)
        if buying:
            touched = bool((m1_recent["low"] <= level + retest_tol).any())
            confirmed = float(m1_recent["close"].iloc[-1]) > level and ind.is_bullish(
                m1_recent.iloc[-1]
            )
        else:
            touched = bool((m1_recent["high"] >= level - retest_tol).any())
            confirmed = float(m1_recent["close"].iloc[-1]) < level and ind.is_bearish(
                m1_recent.iloc[-1]
            )
        clean_retest = touched and confirmed
        if not touched:
            rejections.append("no M1 retest of broken level")
        if not confirmed:
            rejections.append("M1 close does not confirm continuation")

        # --- spread / volatility
        spread_ok = snap.spread_points <= self.max_spread_points
        if not spread_ok:
            rejections.append(f"spread {snap.spread_points:.0f}pt above limit")
        atr_ok = self.min_atr_points <= snap.atr_points <= self.max_atr_points
        if not atr_ok:
            rejections.append(f"ATR {snap.atr_points:.0f}pt outside allowed range")

        # --- stop loss: M5 swing in M5 geometry mode (wider, cost-efficient),
        # otherwise the M1 retest extreme
        atr_geo = m5_atr_now if t.sl_timeframe == "M5" else atr_now
        if t.sl_timeframe == "M5":
            if buying:
                swing = ind.recent_swing_low(snap.m5, lookback=t.m5_structure_lookback)
                sl = swing if swing is not None and swing < price else None
            else:
                swing = ind.recent_swing_high(snap.m5, lookback=t.m5_structure_lookback)
                sl = swing if swing is not None and swing > price else None
        elif buying:
            retest_low = float(m1_recent["low"].min())
            sl = retest_low if retest_low < price else None
        else:
            retest_high = float(m1_recent["high"].max())
            sl = retest_high if retest_high > price else None
        if sl is None:
            rejections.append(f"no valid {t.sl_timeframe} extreme for stop loss")
            return EvaluationResult(None, rejections)

        sl_distance = abs(price - sl)
        if atr_geo <= 0:
            rejections.append("geometry ATR not available")
        elif sl_distance < t.min_sl_atr * atr_geo:
            rejections.append(f"SL distance {sl_distance / atr_geo:.2f} ATR too small")
        elif sl_distance > t.max_sl_atr * atr_geo:
            rejections.append(f"SL distance {sl_distance / atr_geo:.2f} ATR too large")

        # --- quality score
        score = 0
        score += 2 if m15_aligned else 0
        score += 2 if m5_aligned else 0
        score += 1 if vwap_aligned else 0
        score += 2 if valid_break else 0
        score += 2 if clean_retest else 0
        score += 1 if atr_ok else 0
        score += 1 if spread_ok else 0

        if score < self.cfg.min_quality_score:
            rejections.append(
                f"quality score {score}/{MAX_SCORE} below minimum {self.cfg.min_quality_score}"
            )

        if rejections:
            logger.debug("Momentum {} rejected: {}", direction.value, "; ".join(rejections))
            return EvaluationResult(None, rejections)

        rr = self.cfg.rr
        tp = price + rr * sl_distance if buying else price - rr * sl_distance
        setup = (
            f"M5 structure {'high' if buying else 'low'} break, M1 retest, "
            f"continuation close, trend {'up' if buying else 'down'}"
        )
        signal = Signal(
            symbol="",
            direction=direction,
            strategy=self.name,
            regime=regime,
            entry=price,
            sl=float(sl),
            tp=float(tp),
            rr=rr,
            score=score,
            max_score=MAX_SCORE,
            setup_reason=setup,
            spread_points=snap.spread_points,
            created_at=datetime.now(timezone.utc),
        )
        return EvaluationResult(signal, [])
