"""Breakout strategy engine: one state machine shared by live loop and
backtester, so the two can never drift apart.

Flow per closed M5 candle:
  IDLE          -> detect an accumulation zone on the candles BEFORE this one,
                   then check whether THIS candle is a confirmed breakout.
  DIRECT mode   -> signal immediately on the breakout close.
  RETEST mode   -> arm a pending retest; enter only when price comes back to
                   the broken boundary and prints a rejection candle there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config_loader import Settings
from indicators import atr as atr_series, ema as ema_series
from models import Direction, EntryMode, Signal, Zone
from zone_detector import detect_zone


@dataclass
class PendingRetest:
    zone: Zone
    direction: Direction
    breakout_time: pd.Timestamp
    candles_waited: int = 0


@dataclass
class Evaluation:
    signal: Optional[Signal] = None
    rejections: list[str] = field(default_factory=list)
    zone: Optional[Zone] = None


class BreakoutStrategy:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.pending: Optional[PendingRetest] = None

    # ------------------------------------------------------------ helpers

    def _breakout_quality(self, candle: pd.Series, direction: Direction,
                          atr_value: float) -> Optional[str]:
        """Return a rejection reason if the breakout candle is weak, else None."""
        body = abs(float(candle["close"]) - float(candle["open"]))
        full = float(candle["high"]) - float(candle["low"])
        if body < self.cfg.min_breakout_body_atr_multiplier * atr_value:
            return (f"breakout body too weak: {body:.2f} < "
                    f"{self.cfg.min_breakout_body_atr_multiplier} x ATR")
        if full <= 0:
            return "degenerate breakout candle (no range)"
        if direction is Direction.BUY:
            wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
        else:
            wick = min(float(candle["open"]), float(candle["close"])) - float(candle["low"])
        wick_pct = 100.0 * wick / full
        if wick_pct > self.cfg.max_rejection_wick_percent:
            return (f"rejection wick too large: {wick_pct:.0f}% > "
                    f"{self.cfg.max_rejection_wick_percent:.0f}%")
        return None

    def _build_signal(self, candle: pd.Series, zone: Zone, direction: Direction,
                      entry_mode: EntryMode, atr_value: float, ema_value: float,
                      reason: str) -> Optional[Signal]:
        """Price the trade: SL beyond the opposite zone side, TP at rr x risk.
        Returns None (never a trade without a valid stop) if the stop distance
        is outside the allowed ATR band."""
        stop_buffer = self.cfg.stop_buffer_atr_multiplier * atr_value
        entry = float(candle["close"])
        # zone_opposite (spec): SL beyond the far zone side. zone_mid: SL
        # beyond the midpoint - half the risk, so 3R lands within reach.
        long_anchor = zone.low if self.cfg.stop_mode == "zone_opposite" else zone.mid
        short_anchor = zone.high if self.cfg.stop_mode == "zone_opposite" else zone.mid
        if direction is Direction.BUY:
            sl = long_anchor - stop_buffer
            risk = entry - sl
            tp = entry + self.cfg.risk_reward_ratio * risk
        else:
            sl = short_anchor + stop_buffer
            risk = sl - entry
            tp = entry - self.cfg.risk_reward_ratio * risk
        if risk <= 0:
            return None
        if risk < self.cfg.min_stop_distance_atr * atr_value:
            return None
        if risk > self.cfg.max_stop_distance_atr * atr_value:
            return None
        return Signal(
            timestamp=candle["time"].to_pydatetime(),
            symbol=self.cfg.symbol,
            timeframe=self.cfg.timeframe,
            entry_mode=entry_mode,
            direction=direction,
            zone=zone,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            risk_reward_ratio=self.cfg.risk_reward_ratio,
            atr=atr_value,
            ema_value=ema_value,
            reason_for_entry=reason,
        )

    def _stop_band_reason(self, candle: pd.Series, zone: Zone, direction: Direction,
                          atr_value: float) -> str:
        entry = float(candle["close"])
        stop_buffer = self.cfg.stop_buffer_atr_multiplier * atr_value
        long_anchor = zone.low if self.cfg.stop_mode == "zone_opposite" else zone.mid
        short_anchor = zone.high if self.cfg.stop_mode == "zone_opposite" else zone.mid
        risk = (entry - (long_anchor - stop_buffer)) if direction is Direction.BUY \
            else ((short_anchor + stop_buffer) - entry)
        return (f"stop distance {risk:.2f} outside "
                f"[{self.cfg.min_stop_distance_atr}, {self.cfg.max_stop_distance_atr}] x "
                f"ATR ({atr_value:.2f})")

    # ------------------------------------------------------------ main entry

    def on_bar(self, df: pd.DataFrame, atr_value: Optional[float] = None,
               ema_value: Optional[float] = None) -> Evaluation:
        """Evaluate the LAST (closed) candle of `df`. All candles must be closed.

        `atr_value`/`ema_value` may be supplied precomputed (the backtester
        computes both series once instead of per-bar); live passes None and
        they are derived from `df` right here. Same numbers either way.
        """
        cfg = self.cfg
        ev = Evaluation()
        self_computed = atr_value is None
        need = cfg.zone_lookback_candles + 1
        if self_computed:
            need = max(need, cfg.atr_period + 2,
                       cfg.ema_period if cfg.use_ema_filter else 0)
        if len(df) < need:
            ev.rejections.append(f"warmup: {len(df)}/{need} candles")
            return ev

        candle = df.iloc[-1]
        if self_computed:
            atr_value = float(atr_series(df, cfg.atr_period).iloc[-1])
            ema_value = (float(ema_series(df["close"], cfg.ema_period).iloc[-1])
                         if cfg.use_ema_filter else 0.0)
        ema_value = float(ema_value or 0.0)
        if atr_value <= 0 or pd.isna(atr_value):
            ev.rejections.append("ATR unavailable")
            return ev
        if cfg.use_ema_filter and pd.isna(ema_value):
            ev.rejections.append("EMA warmup")
            return ev

        # ---------- pending retest handling (BREAKOUT_RETEST mode)
        if self.pending is not None:
            return self._handle_retest(candle, atr_value, ema_value)

        # ---------- fresh zone scan: the zone lives BEFORE the breakout candle
        window = df.iloc[-(cfg.zone_lookback_candles + 1):-1]
        zres = detect_zone(window, atr_value, cfg)
        if zres.zone is None:
            ev.rejections.extend(zres.rejections)
            return ev
        zone = zres.zone
        ev.zone = zone

        # ---------- breakout confirmation on the current candle
        buffer = cfg.breakout_buffer_atr_multiplier * atr_value
        close = float(candle["close"])
        if close > zone.high + buffer:
            direction = Direction.BUY
        elif close < zone.low - buffer:
            direction = Direction.SELL
        else:
            ev.rejections.append("no breakout: close inside zone +/- buffer")
            return ev

        weak = self._breakout_quality(candle, direction, atr_value)
        if weak:
            ev.rejections.append(weak)
            return ev

        if cfg.use_ema_filter:
            if direction is Direction.BUY and close <= ema_value:
                ev.rejections.append(f"EMA filter: BUY below EMA{cfg.ema_period}")
                return ev
            if direction is Direction.SELL and close >= ema_value:
                ev.rejections.append(f"EMA filter: SELL above EMA{cfg.ema_period}")
                return ev

        if cfg.entry_mode == EntryMode.DIRECT_BREAKOUT:
            reason = (f"direct breakout of accumulation zone "
                      f"[{zone.low:.2f}-{zone.high:.2f}] "
                      f"({zone.upper_touches}U/{zone.lower_touches}L wick touches)")
            signal = self._build_signal(candle, zone, direction,
                                        EntryMode.DIRECT_BREAKOUT, atr_value,
                                        ema_value, reason)
            if signal is None:
                ev.rejections.append(self._stop_band_reason(candle, zone, direction, atr_value))
                return ev
            ev.signal = signal
            return ev

        # BREAKOUT_RETEST: arm and wait
        self.pending = PendingRetest(zone=zone, direction=direction,
                                     breakout_time=candle["time"])
        ev.rejections.append(
            f"breakout confirmed {direction.value}; waiting for retest of "
            f"{'zone high' if direction is Direction.BUY else 'zone low'}"
        )
        return ev

    # ------------------------------------------------------------ retest leg

    def _handle_retest(self, candle: pd.Series, atr_value: float,
                       ema_value: float) -> Evaluation:
        cfg = self.cfg
        ev = Evaluation(zone=self.pending.zone)
        pending = self.pending
        pending.candles_waited += 1
        zone = pending.zone
        tol = cfg.touch_tolerance_atr_multiplier * atr_value
        close = float(candle["close"])
        is_buy = pending.direction is Direction.BUY

        # give up: took too long
        if pending.candles_waited > cfg.retest_timeout_candles:
            self.pending = None
            ev.rejections.append(
                f"retest timeout after {cfg.retest_timeout_candles} candles"
            )
            return ev

        # give up: price fell back deep into the zone (failed breakout)
        penetration_limit = zone.size * cfg.retest_max_penetration_percent / 100.0
        if is_buy and close < zone.high - penetration_limit:
            self.pending = None
            ev.rejections.append("retest failed: price re-entered the zone")
            return ev
        if not is_buy and close > zone.low + penetration_limit:
            self.pending = None
            ev.rejections.append("retest failed: price re-entered the zone")
            return ev

        # the retest itself: touch the broken boundary, close back in the
        # breakout direction with a same-direction body (rejection candle)
        bullish = close > float(candle["open"])
        if is_buy:
            touched = float(candle["low"]) <= zone.high + tol
            rejected = close > zone.high and bullish
        else:
            touched = float(candle["high"]) >= zone.low - tol
            rejected = close < zone.low and not bullish

        if not (touched and rejected):
            ev.rejections.append(
                f"waiting for retest rejection ({pending.candles_waited}/"
                f"{cfg.retest_timeout_candles})"
            )
            return ev

        if cfg.use_ema_filter:
            if is_buy and close <= ema_value:
                self.pending = None
                ev.rejections.append(f"EMA filter: BUY below EMA{cfg.ema_period} at retest")
                return ev
            if not is_buy and close >= ema_value:
                self.pending = None
                ev.rejections.append(f"EMA filter: SELL above EMA{cfg.ema_period} at retest")
                return ev

        direction = pending.direction
        self.pending = None
        reason = (f"retest entry after {direction.value} breakout of zone "
                  f"[{zone.low:.2f}-{zone.high:.2f}] "
                  f"({zone.upper_touches}U/{zone.lower_touches}L wick touches)")
        signal = self._build_signal(candle, zone, direction,
                                    EntryMode.BREAKOUT_RETEST, atr_value,
                                    ema_value, reason)
        if signal is None:
            ev.rejections.append(self._stop_band_reason(candle, zone, direction, atr_value))
            return ev
        ev.signal = signal
        return ev
