"""Backtester: replays M5 history bar-by-bar through the SAME strategy engine
the live bot uses (BreakoutStrategy.on_bar), simulates SL/TP conservatively
(stop-first when a bar spans both), and produces the metric set + the
comparison matrix (entry mode x EMA filter x session filter).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from breakout_strategy import BreakoutStrategy
from config_loader import Settings
from models import BacktestReport, BacktestTrade, Direction, EntryMode, Signal
from session_filter import active_session

MAX_TRADE_CANDLES = 12 * 24  # safety: force-close after 24h in the market


# ------------------------------------------------------------------ data

def load_m5_csv(path: str | Path) -> pd.DataFrame:
    """Load M5 candles from CSV. Accepts:
    - native: time,open,high,low,close[,volume]
    - HistData MT (M1 or M5): no header, 'YYYY.MM.DD,HH:MM,O,H,L,C,V'
    M1 input is resampled to M5 automatically.
    """
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"CSV not found: {p}")
    with p.open("r", encoding="utf-8-sig", errors="replace") as fh:
        first = fh.readline()
    fields = first.rstrip("\n").split(",")
    has_header = any(c.isalpha() for c in fields[0])

    if not has_header and len(fields) >= 7 and ":" in fields[1]:
        df = pd.read_csv(p, header=None,
                         names=["date", "clock", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["date"] + " " + df["clock"],
                                    format="%Y.%m.%d %H:%M")
        df = df[["time", "open", "high", "low", "close", "volume"]]
    else:
        df = pd.read_csv(p)
        df.columns = [c.strip().lower() for c in df.columns]
        if "time" not in df.columns:
            raise SystemExit(f"CSV must have a 'time' column, found {list(df.columns)}")
        df["time"] = pd.to_datetime(df["time"])
        if df["time"].dt.tz is not None:
            df["time"] = df["time"].dt.tz_localize(None)  # broker-time semantics
        if "volume" not in df.columns:
            df["volume"] = 0

    df = df.dropna().sort_values("time").drop_duplicates("time").reset_index(drop=True)

    # resample M1 -> M5 when needed
    step = df["time"].diff().dropna().mode()
    if len(step) and step.iloc[0] <= pd.Timedelta(minutes=1):
        logger.info("Input looks like M1 - resampling to M5")
        df = (df.set_index("time")
                .resample("5min", label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last", "volume": "sum"})
                .dropna()
                .reset_index())
    logger.info("Loaded {} M5 candles ({} -> {})", len(df),
                df["time"].iloc[0], df["time"].iloc[-1])
    return df


# ------------------------------------------------------------------ engine

class Backtester:
    def __init__(self, cfg: Settings, m5: pd.DataFrame) -> None:
        self.cfg = cfg
        self.m5 = m5.reset_index(drop=True)
        self.spread_price = cfg.backtest.spread_points * cfg.backtest.point
        # Indicators computed ONCE over the whole history; recomputing them
        # inside on_bar for every bar would make the run O(n^2).
        from indicators import atr as atr_series, ema as ema_series

        self._atr = atr_series(self.m5, cfg.atr_period)
        self._ema = ema_series(self.m5["close"], cfg.ema_period)

    def run(self, label: str = "") -> BacktestReport:
        cfg = self.cfg
        strategy = BreakoutStrategy(cfg)
        trades: list[BacktestTrade] = []

        open_signal: Optional[Signal] = None
        open_index = -1
        trades_today = 0
        current_day: Optional[date] = None
        cooldown_until: Optional[pd.Timestamp] = None

        warmup = max(cfg.zone_lookback_candles + 1,
                     cfg.ema_period if cfg.use_ema_filter else 0,
                     cfg.atr_period + 2)

        for i in range(warmup, len(self.m5)):
            bar = self.m5.iloc[i]
            bar_time: pd.Timestamp = bar["time"]

            if bar_time.date() != current_day:
                current_day = bar_time.date()
                trades_today = 0

            # ---- manage the open simulated trade first
            if open_signal is not None:
                closed = self._manage(open_signal, bar, i - open_index, label)
                if closed is not None:
                    trades.append(closed)
                    cooldown_until = bar_time + pd.Timedelta(minutes=cfg.cooldown_minutes)
                    open_signal = None
                continue  # one position at a time; no scanning while in a trade

            # ---- gates before scanning
            if trades_today >= cfg.max_trades_per_day:
                continue
            if cooldown_until is not None and bar_time < cooldown_until:
                continue
            session = active_session(cfg.sessions, bar_time.to_pydatetime())
            if cfg.use_session_filter and session is None:
                continue

            window = self.m5.iloc[i - cfg.zone_lookback_candles - 1 : i + 1]
            ev = strategy.on_bar(window,
                                 atr_value=float(self._atr.iloc[i]),
                                 ema_value=float(self._ema.iloc[i])
                                 if cfg.use_ema_filter else 0.0)
            if ev.signal is None:
                continue

            signal = ev.signal
            signal.session_name = session or ""
            # entry cost: buys pay the ask (chart is bid) - shift the whole
            # geometry by the spread; sells enter at bid, exits pay it later,
            # approximated the same way for symmetry.
            if signal.direction is Direction.BUY:
                shift = self.spread_price
            else:
                shift = -self.spread_price
            risk = signal.risk_distance
            signal.entry_price += shift
            signal.stop_loss += shift
            signal.take_profit += shift
            assert abs(signal.risk_distance - risk) < 1e-9

            open_signal = signal
            open_index = i
            trades_today += 1

        report = _metrics(trades, label)
        return report

    def _manage(self, signal: Signal, bar: pd.Series, candles_open: int,
                label: str) -> Optional[BacktestTrade]:
        """Conservative fill model: if one bar spans both SL and TP, the STOP
        is assumed to hit first."""
        high, low = float(bar["high"]), float(bar["low"])
        buying = signal.direction is Direction.BUY
        sl_hit = low <= signal.stop_loss if buying else high >= signal.stop_loss
        tp_hit = high >= signal.take_profit if buying else low <= signal.take_profit

        if sl_hit:  # stop-first on both-hit bars
            return self._close(signal, bar, signal.stop_loss, -1.0, "LOSS")
        if tp_hit:
            return self._close(signal, bar, signal.take_profit,
                               signal.risk_reward_ratio, "WIN")
        if candles_open >= MAX_TRADE_CANDLES:
            close = float(bar["close"])
            r = ((close - signal.entry_price) if buying
                 else (signal.entry_price - close)) / signal.risk_distance
            return self._close(signal, bar, close, r, "TIMEOUT")
        return None

    @staticmethod
    def _close(signal: Signal, bar: pd.Series, price: float, r: float,
               result: str) -> BacktestTrade:
        return BacktestTrade(
            open_time=signal.timestamp,
            close_time=bar["time"].to_pydatetime(),
            direction=signal.direction,
            entry=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            exit_price=price,
            r_result=r,
            result=result,
            session_name=signal.session_name,
            entry_mode=signal.entry_mode,
            zone_high=signal.zone.high,
            zone_low=signal.zone.low,
        )


# ------------------------------------------------------------------ metrics

def _metrics(trades: list[BacktestTrade], label: str) -> BacktestReport:
    report = BacktestReport(label=label, trades=trades)
    if not trades:
        return report
    rs = np.array([t.r_result for t in trades])
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    report.total_trades = len(trades)
    report.winning_trades = int(len(wins))
    report.losing_trades = int(len(losses))
    report.win_rate_pct = 100.0 * len(wins) / len(trades)
    report.average_r = float(rs.mean())
    report.total_r = float(rs.sum())
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    report.profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    equity = np.cumsum(rs)
    report.max_drawdown_r = float((np.maximum.accumulate(equity) - equity).max())
    report.long_trades = sum(1 for t in trades if t.direction is Direction.BUY)
    report.short_trades = report.total_trades - report.long_trades
    report.best_trade_r = float(rs.max())
    report.worst_trade_r = float(rs.min())
    streak_l = streak_w = max_l = max_w = 0
    for r in rs:
        if r > 0:
            streak_w += 1; streak_l = 0
        else:
            streak_l += 1; streak_w = 0
        max_l = max(max_l, streak_l)
        max_w = max(max_w, streak_w)
    report.max_consecutive_losses = max_l
    report.max_consecutive_wins = max_w
    return report


# ------------------------------------------------------------------ matrix

def run_comparison(cfg: Settings, m5: pd.DataFrame) -> list[BacktestReport]:
    """The required comparison: DIRECT vs RETEST x EMA on/off x session on/off."""
    reports: list[BacktestReport] = []
    for mode in (EntryMode.BREAKOUT_RETEST, EntryMode.DIRECT_BREAKOUT):
        for ema_on in (True, False):
            for session_on in (True, False):
                variant = cfg.model_copy(deep=True)
                variant.entry_mode = mode
                variant.use_ema_filter = ema_on
                variant.use_session_filter = session_on
                label = (f"{mode.value}|ema_{'on' if ema_on else 'off'}"
                         f"|session_{'on' if session_on else 'off'}")
                logger.info("Backtesting {} ...", label)
                reports.append(Backtester(variant, m5).run(label))
    return reports
