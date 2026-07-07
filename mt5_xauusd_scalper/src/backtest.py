"""Backtester: replays M1 history through the live strategy/risk stack.

M5/M15 candles are resampled from M1 so all timeframes stay aligned.
Costs are modelled realistically (cost_model.py): variable spread, per-side
commission, adverse slippage on every fill, occasional requotes, and a
configurable intrabar rule for bars that span both stop and target.
Position management goes through the SAME decide_actions() the live bot uses,
so backtest behaviour cannot silently drift from live behaviour.

Usage:
    python src/backtest.py --days 30                     # pull M1 history from MT5
    python src/backtest.py --csv data/m1.csv             # or use a CSV export
    python src/backtest.py --csv data/m1.csv --export-trades data/trades.csv
"""
from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from config import BotConfig, load_config
from cost_model import CostModel
from daily_risk_governor import DailyRiskGovernor
from models import (
    NO_TRADE_REGIMES,
    Direction,
    PositionActionType,
    Regime,
    StrategyName,
    SymbolSpec,
)
from position_manager import decide_actions
from regime_detector import RegimeDetector, build_snapshot, geometry_atr
from risk_manager import calculate_lot, check_cost_to_tp, validate_sl_distance
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
    entry: float            # actual fill price (adverse-adjusted, drives PnL)
    intended_entry: float   # signal entry (drives R geometry: TP/SL/partials)
    sl: float
    tp: float
    initial_sl: float
    lot: float
    initial_lot: float
    risk_usd: float
    open_time: pd.Timestamp
    open_idx: int
    session_hour: int
    trade_index: int = 0    # global trade counter (seeds the cost model)
    trade_no: int = 1       # 1st or 2nd trade of its day
    score: int = 0
    realized: float = 0.0   # net of commission and slippage
    commission_usd: float = 0.0
    partial_done: bool = False
    breakeven_done: bool = False
    close_time: Optional[pd.Timestamp] = None
    close_reason: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.intended_entry - self.initial_sl)


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
        spread_points: Optional[float] = None,
    ) -> None:
        cfg = apply_sniper_overrides(cfg) if cfg.sniper_mode.enabled else copy.deepcopy(cfg)
        if spread_points is not None:  # CLI/optimizer override of the base spread
            cfg.backtest.base_spread_points = spread_points
        self.cfg = cfg
        self.sniper = cfg.sniper_mode if cfg.sniper_mode.enabled else None
        self.m1 = m1.reset_index(drop=True)
        self.spec = spec
        # The signal/regime layer sees the model's base spread so its spread
        # filters behave as they would live; per-fill cost uses the full model.
        self.spread_points = cfg.backtest.base_spread_points
        self.cost = CostModel(cfg.backtest, spec)
        self._trade_counter = 0
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
            self._exit(open_trade, float(last["close"]), open_trade.lot, last["time"], "END_OF_DATA")
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

    # ------------------------------------------------------------- diagnostics

    def diagnose(self) -> dict[str, int]:
        """Replay every in-window bar and tally WHY it did or didn't produce a
        tradeable signal. Turns '0 trades' into an actionable funnel."""
        from collections import Counter

        tally: Counter[str] = Counter()
        m5_times = self.m5["time"].astype("int64").to_numpy()
        m15_times = self.m15["time"].astype("int64").to_numpy()

        for i in range(M1_WINDOW, len(self.m1)):
            tally["bars_scanned"] += 1
            bar_time = self.m1.iloc[i]["time"]
            allowed, _ = in_session(self.cfg.sessions, bar_time.to_pydatetime())
            if not allowed:
                tally["1_outside_session"] += 1
                continue

            bar_ns = bar_time.value
            n5 = int(np.searchsorted(m5_times, bar_ns, side="right")) - 1
            n15 = int(np.searchsorted(m15_times, bar_ns, side="right")) - 1
            if n5 < M5_WINDOW // 2 or n15 < M15_WINDOW // 2:
                tally["2_warming_up"] += 1
                continue

            snap = build_snapshot(
                self.m15.iloc[max(0, n15 - M15_WINDOW): n15].reset_index(drop=True),
                self.m5.iloc[max(0, n5 - M5_WINDOW): n5].reset_index(drop=True),
                self.m1.iloc[i - M1_WINDOW + 1: i + 1].reset_index(drop=True),
                self.cfg.strategy, self.spread_points, self.spec.point,
                self.cfg.sessions.timezone,
            )
            regime = self.detector.detect(snap)
            if regime.regime in NO_TRADE_REGIMES or regime.regime is Regime.RANGE:
                reason = regime.reasons[0] if regime.reasons else ""
                import re

                reason = re.sub(r"[-+]?\d[\d.]*", "", reason)
                reason = re.sub(r"\s+", " ", reason).strip()[:40]
                tally[f"3_regime_{regime.regime.value}: {reason}"] += 1
                continue

            selection = self.selector.select(snap, regime.regime)
            if selection.signal is None:
                reason = selection.rejections[0] if selection.rejections else "no signal"
                # keep the human reason after the strategy prefix, and strip
                # variable numbers/parentheticals so similar reasons group into one
                import re

                short = reason.split(":", 1)[-1].strip()
                short = re.sub(r"\([^)]*\)", "", short)
                short = re.sub(r"[-+]?\d[\d.]*", "", short)
                short = re.sub(r"\s+", " ", short).strip()[:45]
                tally[f"4_strat: {short}"] += 1
                continue

            signal = selection.signal
            if self.sniper:
                sniper_adjust_signal(signal, snap, self.sniper)
                if signal.score < self.sniper.min_sniper_score:
                    tally[f"5_score_below_min_sniper ({self.sniper.min_sniper_score})"] += 1
                    continue
                if signal.score < self.sniper.min_execution_score:
                    tally[f"6_score_signal_only ({signal.score}<{self.sniper.min_execution_score})"] += 1
                    continue

            sl_check = validate_sl_distance(
                signal.entry, signal.sl, geometry_atr(snap, self.cfg.strategy), self.cfg.strategy
            )
            if not sl_check.ok:
                tally["7_sl_bounds"] += 1
                continue
            cost_check = check_cost_to_tp(
                signal.entry, signal.tp, self.spread_points, self.spec.point, self.cfg.trading
            )
            if not cost_check.ok:
                tally["8_cost_gate"] += 1
                continue
            tally["9_PASSES_ALL_FILTERS"] += 1

        return dict(sorted(tally.items()))

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

        sl_check = validate_sl_distance(
            signal.entry, signal.sl, geometry_atr(snap, self.cfg.strategy), self.cfg.strategy
        )
        if not sl_check.ok:
            return None
        cost_check = check_cost_to_tp(
            signal.entry, signal.tp, self.spread_points, self.spec.point, self.cfg.trading
        )
        if not cost_check.ok:
            return None
        lot_result = calculate_lot(signal.risk_usd, signal.entry, signal.sl, self.spec)
        if not lot_result.ok:
            return None

        self._trade_counter += 1
        idx = self._trade_counter
        fill = self.cost.entry_fill(signal.entry, signal.direction, idx)
        entry_commission = self.cost.commission(lot_result.lot)
        return SimTrade(
            direction=signal.direction,
            strategy=signal.strategy,
            regime=signal.regime,
            entry=fill.price,
            intended_entry=signal.entry,
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
            trade_index=idx,
            trade_no=governor.state.trades_today + 1,
            score=signal.score,
            realized=-entry_commission,
            commission_usd=entry_commission,
        )

    # ------------------------------------------------------------- trade sim

    def _pnl(self, trade: SimTrade, exit_price: float, lot: float) -> float:
        diff = exit_price - trade.entry if trade.direction is Direction.BUY else trade.entry - exit_price
        return diff * self._usd_per_unit * lot

    def _exit(
        self,
        trade: SimTrade,
        level_price: float,
        lot: float,
        when: pd.Timestamp,
        reason: str,
        apply_costs: bool = True,
    ) -> None:
        """Close `lot` of the trade at `level_price`, applying exit slippage/spread
        and per-lot commission. Fully closes the trade when no volume remains."""
        fill = self.cost.exit_fill(level_price, trade.direction, trade.trade_index) if apply_costs else level_price
        commission = self.cost.commission(lot)
        trade.realized += self._pnl(trade, fill, lot) - commission
        trade.commission_usd += commission
        trade.lot = round(trade.lot - lot, 8)
        if trade.lot <= 1e-9:
            trade.close_time = when
            trade.close_reason = reason

    def _process_bar(self, trade: SimTrade, bar: pd.Series, i: int, pm) -> bool:
        """Advance the open trade one M1 bar. Returns True when fully closed.

        Management decisions go through the shared decide_actions() so the
        backtest applies the SAME breakeven/partial/time rules as the live bot.
        Fills (SL/TP/market) are simulated here with the realistic cost model.
        """
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        when = bar["time"]
        buying = trade.direction is Direction.BUY
        risk = trade.risk_distance
        minutes_open = i - trade.open_idx

        sl_hit = low <= trade.sl if buying else high >= trade.sl
        tp_hit = high >= trade.tp if buying else low <= trade.tp
        if sl_hit and tp_hit:
            # One bar spans both: intrabar rule decides which fills first.
            if self.cost.stop_hit_first(trade.trade_index):
                self._exit(trade, trade.sl, trade.lot, when, "STOP_LOSS")
            else:
                self._exit(trade, trade.tp, trade.lot, when, "TAKE_PROFIT")
            return True
        if sl_hit:
            self._exit(trade, trade.sl, trade.lot, when, "STOP_LOSS")
            return True
        if tp_hit:
            self._exit(trade, trade.tp, trade.lot, when, "TAKE_PROFIT")
            return True

        ref = trade.intended_entry
        best = high if buying else low
        favorable_r = ((best - ref) if buying else (ref - best)) / risk if risk > 0 else 0.0
        close_r = ((close - ref) if buying else (ref - close)) / risk if risk > 0 else 0.0

        for action in decide_actions(
            favorable_r=favorable_r,
            current_r=close_r,
            minutes_open=minutes_open,
            breakeven_done=trade.breakeven_done,
            partial_done=trade.partial_done,
            entry_price=trade.entry,
            cfg=pm,
        ):
            if action.action is PositionActionType.MOVE_BREAKEVEN:
                trade.sl = trade.entry  # risk-free at the actual entry fill
                trade.breakeven_done = True
            elif action.action is PositionActionType.PARTIAL_CLOSE:
                partial_level = (
                    ref + pm.partial_close_at_r * risk
                    if buying
                    else ref - pm.partial_close_at_r * risk
                )
                close_lot = round(trade.initial_lot * pm.partial_close_percent / 100.0, 8)
                close_lot = min(close_lot, trade.lot)
                if close_lot > 0 and trade.lot - close_lot > 1e-9:
                    self._exit(trade, partial_level, close_lot, when, "PARTIAL")
                    trade.partial_done = True
            elif action.action in (
                PositionActionType.TIME_EXIT,
                PositionActionType.MAX_DURATION_EXIT,
            ):
                self._exit(trade, close, trade.lot, when, action.action.value)
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
        m["total_commission"] = round(sum(t.commission_usd for t in trades), 2)
        gross_before_costs = m["net_pnl"] + m["total_commission"]
        m["cost_drag_pct"] = (
            round(100.0 * m["total_commission"] / abs(gross_before_costs), 2)
            if gross_before_costs
            else 0.0
        )

        equity = np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        m["max_drawdown"] = round(float((peak - equity).max()), 2)

        # Risk-adjusted return per trade (Sharpe/Sortino on the trade series).
        std = float(pnls.std(ddof=1)) if len(pnls) > 1 else 0.0
        m["sharpe_per_trade"] = round(float(pnls.mean()) / std, 3) if std > 0 else 0.0
        downside = pnls[pnls < 0]
        dstd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
        m["sortino_per_trade"] = round(float(pnls.mean()) / dstd, 3) if dstd > 0 else 0.0

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
        target = (
            self.cfg.risk.daily_profit_target_usd if self.sniper
            else self.cfg.daily_goals.daily_profit_target_usd
        )
        max_loss = (
            self.cfg.risk.max_daily_loss_usd if self.sniper
            else self.cfg.daily_goals.max_daily_loss_usd
        )
        m["days_hitting_target"] = sum(1 for v in daily.values() if v >= target)
        m["days_hitting_max_loss"] = sum(1 for v in daily.values() if v <= -max_loss)
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
    from mt5_connector import MT5Connector, MT5Error

    connector = MT5Connector(cfg.mt5, cfg.trading)
    connector.connect()
    try:
        connector.resolve_symbol()
        requested = min(days * 1440, 200_000)
        # The terminal only caches history it has downloaded. If it has fewer M1
        # bars than requested, copy_rates_from_pos can return nothing rather than
        # a partial set - so step the request down until the terminal answers.
        for count in (requested, 50_000, 20_000, 5_000, 1_000):
            if count > requested:
                continue
            try:
                df = connector.rates("M1", count)
            except MT5Error:
                logger.warning("No M1 history for {} bars, trying fewer...", count)
                continue
            got_days = len(df) / 1440.0
            if len(df) < requested:
                logger.warning(
                    "Terminal returned {} M1 bars (~{:.1f} days) of the {} requested. "
                    "Open the XAUUSD M1 chart in MT5 and scroll back to download more "
                    "history (Tools > Options > Charts > Max bars in chart).",
                    len(df), got_days, requested,
                )
            return df
        raise MT5Error(
            "MT5 returned no M1 history for XAUUSD. Open the XAUUSD chart in the "
            "terminal, switch to M1, press Home / scroll left to download history, "
            "raise 'Max bars in chart' in Tools > Options > Charts, then retry "
            "(start with --days 7)."
        )
    finally:
        connector.shutdown()


def print_report(report: BacktestReport) -> None:
    print("\n===== BACKTEST REPORT =====")
    for key, value in report.metrics.items():
        print(f"{key:36s} {value}")
    print("===========================\n")


def export_trades_csv(report: BacktestReport, path: str) -> None:
    """Write the per-trade ledger (with running equity) for external analysis."""
    import csv

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    equity = 0.0
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["trade_index", "open_time", "close_time", "direction", "strategy", "regime",
             "trade_no", "score", "entry", "sl", "tp", "lot", "close_reason",
             "commission_usd", "pnl", "equity"]
        )
        for t in report.trades:
            equity += t.realized
            writer.writerow(
                [t.trade_index, t.open_time, t.close_time, t.direction.value, t.strategy.value,
                 t.regime.value, t.trade_no, t.score, round(t.entry, 2), round(t.sl, 2),
                 round(t.tp, 2), t.initial_lot, t.close_reason, round(t.commission_usd, 2),
                 round(t.realized, 2), round(equity, 2)]
            )
    logger.info("Trade ledger written to {}", out)


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper backtest")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=30, help="days of M1 history from MT5")
    parser.add_argument("--csv", default=None, help="CSV with M1 candles instead of MT5")
    parser.add_argument("--spread-points", type=float, default=None,
                        help="override backtest.base_spread_points")
    parser.add_argument("--export-trades", default=None, help="write per-trade ledger CSV")
    parser.add_argument("--diagnose", action="store_true",
                        help="print a rejection funnel (why bars did/didn't produce trades) "
                             "instead of running the trade simulation")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.csv:
        m1 = load_m1_from_csv(args.csv)
    else:
        m1 = load_m1_from_mt5(cfg, args.days)
    logger.info("Backtesting {} M1 bars ({} -> {})", len(m1), m1['time'].iloc[0], m1['time'].iloc[-1])

    bt = Backtester(cfg, m1, spread_points=args.spread_points)
    if args.diagnose:
        funnel = bt.diagnose()
        scanned = funnel.pop("bars_scanned", 0)
        print("\n===== REJECTION FUNNEL =====")
        print(f"{'bars_scanned':40s} {scanned}")
        for stage, count in funnel.items():
            pct = 100.0 * count / scanned if scanned else 0.0
            print(f"{stage:40s} {count:6d}  ({pct:4.1f}%)")
        print("============================")
        print("Stages 1-2 are expected to hold most bars (outside sessions / warm-up).")
        print("Look at stages 3-8: the biggest one is your tightest filter.\n")
        return 0

    report = bt.run()
    print_report(report)
    if args.export_trades:
        export_trades_csv(report, args.export_trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
