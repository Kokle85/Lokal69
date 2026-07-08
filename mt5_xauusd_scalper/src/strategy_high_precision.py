"""High Precision Scalp strategy: pullback + rejection entries in a clean trend."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

import indicators as ind
from config import HighPrecisionConfig, StrategyTuningConfig
from models import Direction, Regime, Signal, StrategyName
from regime_detector import MarketSnapshot

MAX_SCORE = 11

RSI_BUY_RANGE = (45.0, 65.0)
RSI_SELL_RANGE = (35.0, 55.0)


@dataclass
class EvaluationResult:
    signal: Signal | None
    rejections: list[str] = field(default_factory=list)


class HighPrecisionStrategy:
    name = StrategyName.HIGH_PRECISION

    def __init__(
        self,
        cfg: HighPrecisionConfig,
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
            return EvaluationResult(None, ["high precision strategy disabled"])
        if regime is Regime.TREND_UP:
            return self._evaluate_direction(snap, regime, Direction.BUY)
        if regime is Regime.TREND_DOWN:
            return self._evaluate_direction(snap, regime, Direction.SELL)
        return EvaluationResult(None, [f"regime {regime.value} not tradeable for high precision"])

    # ------------------------------------------------------------------ core

    def _evaluate_direction(
        self, snap: MarketSnapshot, regime: Regime, direction: Direction
    ) -> EvaluationResult:
        rejections: list[str] = []
        t = self.tuning
        last = snap.m1.iloc[-1]
        price = snap.price
        atr_now = snap.atr_now
        buying = direction is Direction.BUY

        if atr_now <= 0:
            return EvaluationResult(None, ["ATR not available"])

        # --- alignment (scored, trend regimes already guarantee most of it)
        m15_aligned = (
            float(snap.m15_ema_fast.iloc[-1]) > float(snap.m15_ema_slow.iloc[-1])
        ) == buying
        m5_aligned = (
            float(snap.m5_ema_fast.iloc[-1]) > float(snap.m5_ema_slow.iloc[-1])
        ) == buying
        vwap_aligned = (price > snap.vwap_now) == buying

        # --- pullback to M1 EMA20/EMA50
        ema_fast_now = float(snap.m1_ema_fast.iloc[-1])
        ema_slow_now = float(snap.m1_ema_slow.iloc[-1])
        tolerance = t.pullback_ema_tolerance_atr * atr_now
        near_ema = (
            min(abs(price - ema_fast_now), abs(price - ema_slow_now)) <= tolerance
            or (min(ema_fast_now, ema_slow_now) - tolerance)
            <= float(last["low" if buying else "high"])
            <= (max(ema_fast_now, ema_slow_now) + tolerance)
        )
        if not near_ema:
            rejections.append("no pullback near M1 EMA20/EMA50")

        distance_from_ema = abs(price - ema_fast_now)
        if distance_from_ema > t.max_distance_from_ema_atr * atr_now:
            rejections.append(
                f"price too far from EMA20 ({distance_from_ema / atr_now:.2f} ATR)"
            )
        clean_pullback = near_ema and distance_from_ema <= t.max_distance_from_ema_atr * atr_now

        # --- RSI zone
        rsi_now = float(snap.m1_rsi.iloc[-1])
        lo, hi = RSI_BUY_RANGE if buying else RSI_SELL_RANGE
        rsi_ok = lo <= rsi_now <= hi
        if not rsi_ok:
            rejections.append(f"RSI {rsi_now:.1f} outside {lo}-{hi}")

        # --- rejection candle
        if buying:
            rejection_candle = ind.is_bullish_rejection(last, t.rejection_wick_body_ratio)
        else:
            rejection_candle = ind.is_bearish_rejection(last, t.rejection_wick_body_ratio)
        if not rejection_candle:
            rejections.append("no rejection candle on M1")

        # --- candle quality
        choppy, choppy_reason = ind.is_choppy_market(
            snap.m1, snap.m1_ema_fast, t.choppy_wick_body_ratio
        )
        if choppy:
            rejections.append(f"candle quality not clean: {choppy_reason}")

        # --- spread / volatility
        spread_ok = snap.spread_points <= self.max_spread_points
        if not spread_ok:
            rejections.append(f"spread {snap.spread_points:.0f}pt above limit")
        atr_ok = self.min_atr_points <= snap.atr_points <= self.max_atr_points
        if not atr_ok:
            rejections.append(f"ATR {snap.atr_points:.0f}pt outside allowed range")

        # --- stop loss from the geometry timeframe's swing (M5 by default:
        # wider stops keep fixed costs small relative to the target)
        atr_geo = float(snap.m5_atr.iloc[-1]) if t.sl_timeframe == "M5" else atr_now
        sl_df = snap.m5 if t.sl_timeframe == "M5" else snap.m1
        if buying:
            swing = ind.recent_swing_low(sl_df)
            sl = swing if swing is not None and swing < price else None
        else:
            swing = ind.recent_swing_high(sl_df)
            sl = swing if swing is not None and swing > price else None
        if sl is None:
            rejections.append(f"no valid {t.sl_timeframe} swing for stop loss")
            return EvaluationResult(None, rejections)

        sl_distance = abs(price - sl)
        if atr_geo <= 0:
            rejections.append("geometry ATR not available")
        elif sl_distance < t.min_sl_atr * atr_geo:
            rejections.append(f"SL distance {sl_distance / atr_geo:.2f} ATR too small")
        elif sl_distance > t.max_sl_atr * atr_geo:
            rejections.append(f"SL distance {sl_distance / atr_geo:.2f} ATR too large")

        # --- precision score
        score = 0
        score += 2 if m15_aligned else 0
        score += 2 if m5_aligned else 0
        score += 1 if vwap_aligned else 0
        score += 1 if clean_pullback else 0
        score += 2 if rejection_candle else 0
        score += 1 if rsi_ok else 0
        score += 1 if spread_ok else 0
        score += 1 if atr_ok else 0

        if score < self.cfg.min_precision_score:
            rejections.append(
                f"precision score {score}/{MAX_SCORE} below minimum {self.cfg.min_precision_score}"
            )

        if rejections:
            logger.debug("High precision {} rejected: {}", direction.value, "; ".join(rejections))
            return EvaluationResult(None, rejections)

        rr = self.cfg.rr
        tp = price + rr * sl_distance if buying else price - rr * sl_distance
        setup = (
            f"M15 {'up' if buying else 'down'}trend, M5 confirmation, M1 pullback, "
            f"{'bullish' if buying else 'bearish'} rejection"
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
