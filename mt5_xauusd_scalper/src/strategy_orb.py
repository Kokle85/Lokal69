"""Opening Range Breakout (ORB) strategy.

Forms an opening range over the first N minutes of a session open (London / New
York), then trades a breakout beyond that range: long above the high, short
below the low, stop on the opposite side, target at tp_r x risk.

Instrument-agnostic: every distance filter is expressed relative to M5 ATR, so
the same settings work for XAUUSD and index CFDs (US100, US500) without
per-point tuning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from loguru import logger

from config import ORBConfig, SessionOpensConfig
from models import Direction, Regime, Signal, StrategyName
from utils import active_session_open

MAX_SCORE = 10


@dataclass
class ORBEvaluation:
    signal: Signal | None
    rejections: list[str] = field(default_factory=list)
    session_name: str = ""


def opening_range(m1: pd.DataFrame, start: datetime, end: datetime) -> tuple[float, float] | None:
    """High/low of M1 candles with open time in [start, end). Times compare in
    UTC; start/end may be tz-aware in any zone."""
    times = pd.to_datetime(m1["time"])
    if times.dt.tz is None:
        times = times.dt.tz_localize("UTC")
    start_utc = pd.Timestamp(start).tz_convert("UTC")
    end_utc = pd.Timestamp(end).tz_convert("UTC")
    mask = (times >= start_utc) & (times < end_utc)
    seg = m1[mask.to_numpy()]
    if seg.empty:
        return None
    return float(seg["high"].max()), float(seg["low"].min())


class ORBStrategy:
    def __init__(self, cfg: ORBConfig, opens_cfg: SessionOpensConfig) -> None:
        self.cfg = cfg
        self.opens = opens_cfg

    def evaluate(
        self,
        m1: pd.DataFrame,
        now: datetime,
        symbol: str,
        instrument_opens: list[str],
        m5_atr: float,
        point: float,
        spread_points: float,
        trend_up: bool | None = None,
    ) -> ORBEvaluation:
        c = self.cfg
        if m5_atr <= 0 or point <= 0:
            return ORBEvaluation(None, ["ATR/point unavailable"])

        sess = active_session_open(
            self.opens, instrument_opens, now, c.opening_range_minutes, c.entry_window_minutes
        )
        if sess is None:
            return ORBEvaluation(None, ["outside any tradeable session-open window"])
        name, open_dt, range_end, _deadline = sess

        if now < range_end:
            return ORBEvaluation(None, [f"{name}: opening range still forming"], name)

        rng = opening_range(m1, open_dt, range_end)
        if rng is None:
            return ORBEvaluation(None, [f"{name}: no opening-range candles in window"], name)
        hi, lo = rng
        range_dist = hi - lo
        range_atr = range_dist / m5_atr if m5_atr > 0 else 0.0
        if range_atr < c.min_range_atr:
            return ORBEvaluation(None, [f"{name}: range {range_atr:.2f} ATR too small"], name)
        if range_atr > c.max_range_atr:
            return ORBEvaluation(None, [f"{name}: range {range_atr:.2f} ATR too large"], name)

        last = m1.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        buffer = c.breakout_buffer_atr * m5_atr
        sl_buffer = c.sl_buffer_atr * m5_atr

        long_level = hi + buffer
        short_level = lo - buffer
        if c.entry_on_close:
            broke_long = close > long_level
            broke_short = close < short_level
        else:
            broke_long = high > long_level
            broke_short = low < short_level

        if broke_long:
            direction = Direction.BUY
            entry = close if c.entry_on_close else long_level
            extension = (entry - long_level) / m5_atr
            sl = lo - sl_buffer
            tp = entry + c.tp_r * (entry - sl)
        elif broke_short:
            direction = Direction.SELL
            entry = close if c.entry_on_close else short_level
            extension = (short_level - entry) / m5_atr
            sl = hi + sl_buffer
            tp = entry - c.tp_r * (sl - entry)
        else:
            return ORBEvaluation(None, [f"{name}: no breakout beyond the opening range"], name)

        # Directional filter: only trade with the higher-timeframe trend.
        if c.trend_filter == "ema" and trend_up is not None:
            if direction is Direction.BUY and not trend_up:
                return ORBEvaluation(None, [f"{name}: long breakout against down-trend (EMA filter)"], name)
            if direction is Direction.SELL and trend_up:
                return ORBEvaluation(None, [f"{name}: short breakout against up-trend (EMA filter)"], name)

        if extension > c.max_breakout_extension_atr:
            return ORBEvaluation(
                None, [f"{name}: breakout extended {extension:.2f} ATR beyond level (chasing)"], name
            )

        risk_dist = abs(entry - sl)
        if risk_dist <= 0:
            return ORBEvaluation(None, [f"{name}: degenerate stop distance"], name)

        score = self._score(range_atr, extension, last, direction, spread_points)
        if score < c.min_score:
            return ORBEvaluation(None, [f"{name}: ORB score {score}/{MAX_SCORE} < {c.min_score}"], name)

        setup = (
            f"{name.upper()} open ORB: {c.opening_range_minutes}m range "
            f"[{lo:.2f}-{hi:.2f}] {range_atr:.1f}xATR, breakout "
            f"{'above' if direction is Direction.BUY else 'below'}, TP {c.tp_r:.1f}R"
        )
        signal = Signal(
            symbol=symbol,
            direction=direction,
            strategy=StrategyName.ORB,
            regime=Regime.TREND_UP if direction is Direction.BUY else Regime.TREND_DOWN,
            entry=entry,
            sl=sl,
            tp=tp,
            rr=c.tp_r,
            score=score,
            max_score=MAX_SCORE,
            setup_reason=setup,
            spread_points=spread_points,
            created_at=datetime.now(timezone.utc),
        )
        return ORBEvaluation(signal, [], name)

    def _score(
        self, range_atr: float, extension: float, candle: pd.Series,
        direction: Direction, spread_points: float,
    ) -> int:
        """A light 0-10 quality score for journaling / optional gating."""
        score = 0
        # healthy (not tiny, not blown-out) range
        if 0.8 <= range_atr <= 2.5:
            score += 3
        elif range_atr < self.cfg.max_range_atr:
            score += 1
        # not chasing far beyond the level
        if extension <= 0.3:
            score += 2
        elif extension <= 0.6:
            score += 1
        # decisive breakout candle in the trade direction
        body = float(candle["close"]) - float(candle["open"])
        rng = float(candle["high"]) - float(candle["low"])
        if rng > 0:
            body_ratio = abs(body) / rng
            aligned = (body > 0) == (direction is Direction.BUY)
            if aligned and body_ratio >= 0.5:
                score += 3
            elif aligned and body_ratio >= 0.3:
                score += 1
        # tolerable spread
        if spread_points <= 25:
            score += 2
        elif spread_points <= 40:
            score += 1
        return min(score, MAX_SCORE)
