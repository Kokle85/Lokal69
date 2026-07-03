"""Backtester: replays M1 history through the live strategy/risk stack.

M5/M15 candles are resampled from M1 so all timeframes stay aligned.
Spread is charged as a fixed cost per trade (points -> USD) which is the
conservative, transparent choice for bar-based simulation.

Usage:
    python src/backtest.py --days 30              # pull M1 history from MT5
    python src/backtest.py --csv data/m1.csv      # or use a CSV export
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from config import BotConfig, load_config
from daily_risk_governor import DailyRiskGovernor
from models import Direction, Regime, StrategyName, SymbolSpec
from regime_detector import RegimeDetector, build_snapshot
from risk_manager import calculate_lot, validate_sl_distance
from sniper_mode import SniperGovernor, apply_sniper_overrides, sniper_adjust_signal
from strategy_high_precision import HighPrecisionStrategy
from strategy_momentum import MomentumStrategy
from strategy_selector import StrategySelector
from utils import in_session

# Default XAUUSD contract spec used when MT5 is not available to query.
DEFAULT_SPEC = SymbolSpec(
    name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2,
)

M1_WINDOW = 400
M5_WINDOW = 200
M15_WINDOW = 150


@dataclass
class SimTrade:
    direction: Direction
    strategy: StrategyName
    regime: Regime
    entry: float
    sl: float
    tp: float
    initial_sl: float
    lot: float
    initial_lot: float
    risk_usd: float
    open_time: pd.Timestamp
    open_idx: int
    session_hour: int
    trade_no: int = 1  # 1st or 2nd trade of its day
    score: int = 0
    realized: float = 0.0
    partial_done: bool = False
    breakeven_done: bool = False
    close_time: Optional[pd.Timestamp] = None
    close_reason: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.initial_sl)


@dataclass
class BacktestReport:
    trades: list[SimTrade] = field(default_factory=list)
    daily_pnl: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float | int | str] = field(default_factory=dict)


def resample_m1(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    df = m1.set_index("time")
    out = pd.DataFrame(
        {
            "open": df["open"].resample(f"{minutes}min").first(),
            "high": df["high"].resample(f"{minutes}min").max(),
            "low": df["low"].resample(f"{minutes}min").min(),
            "close": df["close"].resample(f"{minutes}min").last(),
            "tick_volume": df["tick_volume"].resample(f"{minutes}min").sum(),
        }
    ).dropna()
    return out.reset_index()


class Backtester:
    def __init__(
        self,
        cfg: BotConfig,
        m1: pd.DataFrame,
        spec: SymbolSpec = DEFAULT_SPEC,
        spread_points: float = 18.0,
    ) -> None:
        if cfg.sniper_mode.enabled:
            cfg = apply_sniper_overrides(cfg)
        self.cfg = cfg
        self.sniper = cfg.sniper_mode if cfg.sniper_mode.enabled else None
        self.m1 = m1.reset_index(drop=True)
        self.spec = spec
        self.spread_points = spread_points
        self.m5 = resample_m1(m1, 5)
        self.m15 = resample_m1(m1, 15)
        self.detector = RegimeDetector(cfg.regime, cfg.trading.max_spread_points)
        self.selector = StrategySelector(
            HighPrecisionStrategy(
                cfg.strategies.high_precision_scalp, cfg.strategy,
                cfg.trading.max_spread_points, cfg.regime.min_atr_points, cfg.regime.max_atr_points,
            ),
            MomentumStrategy(
                cfg.strategies.momentum_scalp, cfg.strategy,
                cfg.trading.max_spread_points, cfg.regime.min_atr_points, cfg.regime.max_atr_points,
            ),
        )
        self._usd_per_unit = spec.tick_value / spec.tick_size  # USD per 1.0 price move per lot

    # ------------------------------------------------------------------ run

    def run(self) -> BacktestReport:
        report = BacktestReport()
        pm = self.cfg.position_management
        governor: Optional[DailyRiskGovernor] = None
        current_day = None
        open_trade: Optional[SimTrade] = None

        m5_times = self.m5["time"].astype("int64").to_numpy()
        m15_times = self.m15["time"].astype("int64").to_numpy()

        for i in range(M1_WINDOW, len(self.m1)):
            bar = self.m1.iloc[i]
            bar_time: pd.Timestamp = bar["time"]
            day = bar_time.date()

            if day != current_day:
                if governor is not None:
                    report.daily_pnl[str(current_day)] = governor.state.realized_pnl
                governor = self._new_governor(day)
                current_day = day

            # ---- manage the open trade first
            if open_trade is not None:
                closed = self._process_bar(open_trade, bar, i, pm)
                if closed:
                    governor.on_trade_closed(open_trade.realized)
                    report.trades.append(open_trade)
                    open_trade = None

            # ---- new entries only on closed M1 candles inside sessions
            allowed, _ = in_session(self.cfg.sessions, bar_time.to_pydatetime())
            if open_trade is not None or not allowed:
                continue
            governor.check_locks()
            if governor.state.locked:
                continue

            open_trade = self._try_open(i, bar_time, m5_times, m15_times, governor)

        # flush the last day / force-close a dangling trade at the last close
        if open_trade is not None:
            last = self.m1.iloc[-1]
            self._close_trade(open_trade, float(last["close"]), open_trade.lot, last["time"], "END_OF_DATA")
            governor.on_trade_closed(open_trade.realized)
            report.trades.append(open_trade)
        if governor is not None:
            report.daily_pnl[str(current_day)] = governor.state.realized_pnl

        report.metrics = self._compute_metrics(report)
        return report

    def _new_governor(self, day) -> DailyRiskGovernor:
        if self.sniper:
            return SniperGovernor(self.cfg.risk, self.sniper, day)
        return DailyRiskGovernor(self.cfg.daily_goals, day)

    # ------------------------------------------------------------- entries

    def _try_open(
        self, i: int, bar_time: pd.Timestamp, m5_times, m15_times, governor: DailyRiskGovernor
    ) -> Optional[SimTrade]:
        m1_win = self.m1.iloc[i - M1_WINDOW + 1 : i + 1]
        bar_ns = bar_time.value
        n5 = int(np.searchsorted(m5_times, bar_ns, side="right")) - 1
        n15 = int(np.searchsorted(m15_times, bar_ns, side="right")) - 1
        if n5 < M5_WINDOW // 2 or n15 < M15_WINDOW // 2:
            return None
        # use only fully closed higher-timeframe candles (candle open <= bar_time - period)
        m5_win = self.m5.iloc[max(0, n5 - M5_WINDOW) : n5]
        m15_win = self.m15.iloc[max(0, n15 - M15_WINDOW) : n15]

        snap = build_snapshot(
            m15_win.reset_index(drop=True),
            m5_win.reset_index(drop=True),
            m1_win.reset_index(drop=True),
            self.cfg.strategy,
            self.spread_points,
            self.spec.point,
            self.cfg.sessions.timezone,
        )
        regime = self.detector.detect(snap)
        selection = self.selector.select(snap, regime.regime)
        if selection.signal is None:
            return None
        signal = selection.signal
        if self.sniper:
            sniper_adjust_signal(signal, snap, self.sniper)
            if signal.score < self.sniper.min_sniper_score:
                return None

        decision = governor.evaluate_new_trade(signal.score)
        if not decision.allowed:
            return None
        signal.risk_usd = decision.risk_usd

        sl_check = validate_sl_distance(signal.entry, signal.sl, snap.atr_now, self.cfg.strategy)
        if not sl_check.ok:
            return None
        lot_result = calculate_lot(signal.risk_usd, signal.entry, signal.sl, self.spec)
        if not lot_result.ok:
            return None

        spread_price = self.spread_points * self.spec.point
        entry = signal.entry + spread_price if signal.direction is Direction.BUY else signal.entry
        return SimTrade(
            direction=signal.direction,
            strategy=signal.strategy,
            regime=signal.regime,
            entry=entry,
            sl=signal.sl,
            tp=signal.tp,
            initial_sl=signal.sl,
            lot=lot_result.lot,
            initial_lot=lot_result.lot,
            risk_usd=signal.risk_usd,
            open_time=bar_time,
            open_idx=i,
            session_hour=bar_time.tz_convert(self.cfg.sessions.timezone).hour
            if bar_time.tzinfo
            else bar_time.hour,
            trade_no=governor.state.trades_today + 1,
            score=signal.score,
        )

    # ------------------------------------------------------------- trade sim

    def _pnl(self, trade: SimTrade, exit_price: float, lot: float) -> float:
        diff = exit_price - trade.entry if trade.direction is Direction.BUY else trade.entry - exit_price
        return diff * self._usd_per_unit * lot

    def _close_trade(
        self, trade: SimTrade, price: float, lot: float, when: pd.Timestamp, reason: str
    ) -> None:
        trade.realized += self._pnl(trade, price, lot)
        trade.lot = round(trade.lot - lot, 8)
        if trade.lot <= 1e-9:
            trade.close_time = when
            trade.close_reason = reason

    def _process_bar(self, trade: SimTrade, bar: pd.Series, i: int, pm) -> bool:
        """Advance the open trade one M1 bar. Returns True when fully closed.
        Conservative intra-bar ordering: stop loss is always assumed to hit first."""
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        when = bar["time"]
        buying = trade.direction is Direction.BUY
        risk = trade.risk_distance
        minutes_open = i - trade.open_idx

        sl_hit = low <= trade.sl if buying else high >= trade.sl
        if sl_hit:
            self._close_trade(trade, trade.sl, trade.lot, when, "STOP_LOSS")
            return True

        tp_hit = high >= trade.tp if buying else low <= trade.tp
        if tp_hit:
            self._close_trade(trade, trade.tp, trade.lot, when, "TAKE_PROFIT")
            return True

        best = high if buying else low
        best_r = ((best - trade.entry) if buying else (trade.entry - best)) / risk if risk > 0 else 0.0

        if not trade.breakeven_done and best_r >= pm.move_to_breakeven_at_r:
            trade.sl = trade.entry
            trade.breakeven_done = True

        if pm.partial_close_enabled and not trade.partial_done and best_r >= pm.partial_close_at_r:
            partial_price = (
                trade.entry + pm.partial_close_at_r * risk
                if buying
                else trade.entry - pm.partial_close_at_r * risk
            )
            close_lot = round(trade.initial_lot * pm.partial_close_percent / 100.0, 8)
            close_lot = min(close_lot, trade.lot)
            if close_lot > 0 and trade.lot - close_lot > 1e-9:
                self._close_trade(trade, partial_price, close_lot, when, "PARTIAL")
                trade.partial_done = True

        close_r = ((close - trade.entry) if buying else (trade.entry - close)) / risk if risk > 0 else 0.0
        if minutes_open >= pm.max_trade_duration_minutes:
            self._close_trade(trade, close, trade.lot, when, "MAX_DURATION")
            return True
        if minutes_open >= pm.time_exit_minutes and close_r < pm.time_exit_min_r:
            self._close_trade(trade, close, trade.lot, when, "TIME_EXIT")
            return True
        return False

    # ------------------------------------------------------------- metrics

    def _compute_metrics(self, report: BacktestReport) -> dict:
        trades = report.trades
        m: dict[str, float | int | str] = {}
        m["total_trades"] = len(trades)
        if not trades:
            m["note"] = "no trades generated"
            return m

        pnls = np.array([t.realized for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        m["win_rate_pct"] = round(100.0 * len(wins) / len(pnls), 2)
        gross_win = float(wins.sum()) if len(wins) else 0.0
        gross_loss = float(-losses.sum()) if len(losses) else 0.0
        m["profit_factor"] = round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf")
        m["average_win"] = round(float(wins.mean()), 2) if len(wins) else 0.0
        m["average_loss"] = round(float(losses.mean()), 2) if len(losses) else 0.0
        m["expectancy"] = round(float(pnls.mean()), 2)
        m["net_pnl"] = round(float(pnls.sum()), 2)

        equity = np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        m["max_drawdown"] = round(float((peak - equity).max()), 2)

        streak = worst = 0
        for p in pnls:
            streak = streak + 1 if p <= 0 else 0
            worst = max(worst, streak)
        m["max_consecutive_losses"] = worst

        durations = [
            (t.close_time - t.open_time).total_seconds() / 60.0
            for t in trades
            if t.close_time is not None
        ]
        m["avg_trade_duration_min"] = round(float(np.mean(durations)), 2) if durations else 0.0

        daily = report.daily_pnl
        trade_days = [d for d, v in daily.items() if v != 0.0] or list(daily)
        m["trades_per_day"] = round(len(trades) / max(1, len(daily)), 2)
        m["no_trade_days"] = sum(1 for v in daily.values() if v == 0.0)
        m["days_hitting_target"] = sum(
            1 for v in daily.values() if v >= self.cfg.daily_goals.daily_profit_target_usd
        )
        m["days_hitting_max_loss"] = sum(
            1 for v in daily.values() if v <= -self.cfg.daily_goals.max_daily_loss_usd
        )
        vals = list(daily.values())
        m["avg_daily_pnl"] = round(float(np.mean(vals)), 2) if vals else 0.0
        m["median_daily_pnl"] = round(float(np.median(vals)), 2) if vals else 0.0
        m["positive_days_pct"] = (
            round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 2) if vals else 0.0
        )
        m["best_day"] = round(max(vals), 2) if vals else 0.0
        m["worst_day"] = round(min(vals), 2) if vals else 0.0

        # --- first vs second trade of the day (sniper-mode reporting)
        for trade_no in (1, 2):
            subset = [t.realized for t in trades if t.trade_no == trade_no]
            m[f"trades_as_no{trade_no}"] = len(subset)
            if subset:
                arr = np.array(subset)
                sub_wins = arr[arr > 0]
                sub_losses = arr[arr <= 0]
                m[f"win_rate_trade{trade_no}_pct"] = round(100.0 * len(sub_wins) / len(arr), 2)
                gw = float(sub_wins.sum()) if len(sub_wins) else 0.0
                gl = float(-sub_losses.sum()) if len(sub_losses) else 0.0
                m[f"profit_factor_trade{trade_no}"] = (
                    round(gw / gl, 3) if gl > 0 else float("inf")
                )
                m[f"net_pnl_trade{trade_no}"] = round(float(arr.sum()), 2)
                m[f"expectancy_trade{trade_no}"] = round(float(arr.mean()), 2)

        trades_by_day: dict[str, list[SimTrade]] = _group(
            trades, key=lambda t: str(t.open_time.date())
        )
        counts = {d: len(g) for d, g in trades_by_day.items()}
        total_days = max(1, len(report.daily_pnl))
        m["days_with_0_trades"] = total_days - len(counts)
        m["days_with_1_trade"] = sum(1 for c in counts.values() if c == 1)
        m["days_with_2_trades"] = sum(1 for c in counts.values() if c >= 2)
        improved = reduced = 0
        for day_trades in trades_by_day.values():
            second = [t for t in day_trades if t.trade_no == 2]
            if second:
                pnl2 = sum(t.realized for t in second)
                if pnl2 > 0:
                    improved += 1
                elif pnl2 < 0:
                    reduced += 1
        m["days_second_trade_improved"] = improved
        m["days_second_trade_reduced"] = reduced

        for name, group in _group(trades, key=lambda t: t.strategy.value).items():
            m[f"pnl_by_strategy[{name}]"] = round(sum(t.realized for t in group), 2)
        for name, group in _group(trades, key=lambda t: t.regime.value).items():
            m[f"pnl_by_regime[{name}]"] = round(sum(t.realized for t in group), 2)
        for name, group in _group(trades, key=lambda t: t.session_hour).items():
            m[f"pnl_by_hour[{name:02d}]"] = round(sum(t.realized for t in group), 2)
        for name, group in _group(
            trades, key=lambda t: "morning" if t.session_hour < 13 else "afternoon"
        ).items():
            m[f"pnl_by_session[{name}]"] = round(sum(t.realized for t in group), 2)
        return m


def _group(items, key):
    out: dict = {}
    for item in items:
        out.setdefault(key(item), []).append(item)
    return out


# ---------------------------------------------------------------- data loading

def load_m1_from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"CSV is missing columns: {missing}")
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 1.0
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").reset_index(drop=True)


def load_m1_from_mt5(cfg: BotConfig, days: int) -> pd.DataFrame:
    from mt5_connector import MT5Connector

    connector = MT5Connector(cfg.mt5, cfg.trading)
    connector.connect()
    try:
        connector.resolve_symbol()
        bars = days * 1440
        return connector.rates("M1", min(bars, 200_000))
    finally:
        connector.shutdown()


def print_report(report: BacktestReport) -> None:
    print("\n===== BACKTEST REPORT =====")
    for key, value in report.metrics.items():
        print(f"{key:36s} {value}")
    print("===========================\n")


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper backtest")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=30, help="days of M1 history from MT5")
    parser.add_argument("--csv", default=None, help="CSV with M1 candles instead of MT5")
    parser.add_argument("--spread-points", type=float, default=18.0)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.csv:
        m1 = load_m1_from_csv(args.csv)
    else:
        m1 = load_m1_from_mt5(cfg, args.days)
    logger.info("Backtesting {} M1 bars ({} -> {})", len(m1), m1['time'].iloc[0], m1['time'].iloc[-1])

    report = Backtester(cfg, m1, spread_points=args.spread_points).run()
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
