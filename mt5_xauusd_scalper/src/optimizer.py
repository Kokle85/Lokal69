"""Parameter optimizer: grid search over strategy/risk parameters via backtest.

The objective explicitly punishes fragile results - never win-rate alone.

Usage:
    python src/optimizer.py --csv data/m1.csv --sample 40
    python src/optimizer.py --days 10 --sample 40      # data from MT5
"""
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import random
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from backtest import Backtester, load_m1_from_csv, load_m1_from_mt5
from config import BotConfig, load_config

PARAMETER_GRID: dict[str, list] = {
    "high_precision_rr": [0.9, 1.0, 1.1],
    "momentum_rr": [1.4, 1.6, 1.8],
    "min_precision_score": [8, 9, 10],
    "min_quality_score": [8, 9, 10],
    "max_spread_points": [20, 25, 30],
    "pullback_ema_tolerance_atr": [0.25, 0.35, 0.45],
    "max_distance_from_ema_atr": [0.6, 0.75, 0.9],
    "move_to_breakeven_at_r": [0.6, 0.7, 0.8],
    "partial_close_at_r": [0.8, 1.0, 1.2],
    "time_exit_minutes": [6, 8, 10],
}

# Grid for SNIPER_MODE geometry (used with --sniper-grid): tests the levers
# that control cost efficiency and winner-scratching, not the entry signal.
SNIPER_GRID: dict[str, list] = {
    "tp_r": [1.2, 1.4, 1.6],
    "min_sl_atr": [0.7, 0.8, 1.0],
    "max_sl_atr": [1.8, 2.0, 2.2],
    "move_to_breakeven_at_r": [0.5, 0.6, 0.7],
    "partial_close_at_r": [0.9, 1.0, 1.2],
    "time_exit_minutes": [12, 15, 20],
    "min_execution_score": [11, 12],
    "max_cost_to_tp_pct": [20, 25, 30],
}

# Grid for ORB mode (--orb-grid): the levers that control ORB net expectancy.
ORB_GRID: dict[str, list] = {
    "opening_range_minutes": [15, 30],
    "tp_r": [1.5, 2.0, 2.5],
    "trend_filter": ["none", "ema"],
    "min_range_atr": [0.4, 0.6, 0.8],
    "max_breakout_extension_atr": [0.5, 1.0],
    "entry_window_minutes": [60, 90, 120],
    "move_to_breakeven_at_r": [0.8, 1.0, 1.5],
}


def apply_orb_params(base: BotConfig, params: dict) -> BotConfig:
    cfg = copy.deepcopy(base)
    cfg.trading_style = "orb"
    o = cfg.orb
    o.opening_range_minutes = params["opening_range_minutes"]
    o.tp_r = params["tp_r"]
    o.trend_filter = params["trend_filter"]
    o.min_range_atr = params["min_range_atr"]
    o.max_breakout_extension_atr = params["max_breakout_extension_atr"]
    o.entry_window_minutes = params["entry_window_minutes"]
    o.move_to_breakeven_at_r = params["move_to_breakeven_at_r"]
    return cfg


# Hard rejection thresholds - results failing any of these are discarded.
MIN_PROFIT_FACTOR = 1.2
MIN_TRADES = 100
MAX_CONSECUTIVE_LOSSES = 4
MAX_LOSS_DAY_RATIO = 0.15  # max daily loss hit on >15% of days = too often
MAX_AVG_LOSS_TO_WIN = 1.6  # avg loss must not dwarf avg win


def apply_params(base: BotConfig, params: dict) -> BotConfig:
    cfg = copy.deepcopy(base)
    cfg.strategies.high_precision_scalp.rr = params["high_precision_rr"]
    cfg.strategies.momentum_scalp.rr = params["momentum_rr"]
    cfg.strategies.high_precision_scalp.min_precision_score = params["min_precision_score"]
    cfg.strategies.momentum_scalp.min_quality_score = params["min_quality_score"]
    cfg.trading.max_spread_points = params["max_spread_points"]
    cfg.strategy.pullback_ema_tolerance_atr = params["pullback_ema_tolerance_atr"]
    cfg.strategy.max_distance_from_ema_atr = params["max_distance_from_ema_atr"]
    cfg.position_management.move_to_breakeven_at_r = params["move_to_breakeven_at_r"]
    cfg.position_management.partial_close_at_r = params["partial_close_at_r"]
    cfg.position_management.time_exit_minutes = params["time_exit_minutes"]
    return cfg


def apply_sniper_params(base: BotConfig, params: dict) -> BotConfig:
    cfg = copy.deepcopy(base)
    cfg.sniper_mode.enabled = True
    s = cfg.sniper_mode
    s.tp_r = params["tp_r"]
    s.min_tp_r = min(s.min_tp_r, s.tp_r)
    s.max_tp_r = max(s.max_tp_r, s.tp_r)
    s.min_sl_atr = params["min_sl_atr"]
    s.max_sl_atr = params["max_sl_atr"]
    s.move_to_breakeven_at_r = params["move_to_breakeven_at_r"]
    s.partial_close_at_r = params["partial_close_at_r"]
    s.time_exit_minutes = params["time_exit_minutes"]
    s.min_execution_score = params["min_execution_score"]
    cfg.trading.max_cost_to_tp_pct = params["max_cost_to_tp_pct"]
    return cfg


def evaluate_result(metrics: dict, total_days: int, min_trades: int = MIN_TRADES) -> tuple[bool, str, float]:
    """Return (accepted, rejection_reason, rank_score)."""
    trades = int(metrics.get("total_trades", 0))
    if trades < min_trades:
        return False, f"trades {trades} < {min_trades}", 0.0
    pf = float(metrics.get("profit_factor", 0.0))
    if pf < MIN_PROFIT_FACTOR:
        return False, f"profit_factor {pf} < {MIN_PROFIT_FACTOR}", 0.0
    if int(metrics.get("max_consecutive_losses", 99)) > MAX_CONSECUTIVE_LOSSES:
        return False, "max_consecutive_losses > 4", 0.0
    loss_days = int(metrics.get("days_hitting_max_loss", 0))
    if total_days > 0 and loss_days / total_days > MAX_LOSS_DAY_RATIO:
        return False, "daily max loss hit too often", 0.0
    avg_win = float(metrics.get("average_win", 0.0))
    avg_loss = abs(float(metrics.get("average_loss", 0.0)))
    if avg_win > 0 and avg_loss / avg_win > MAX_AVG_LOSS_TO_WIN:
        return False, "average loss much larger than average win", 0.0

    # Rank score: profit factor + drawdown control + consistency, not just win rate.
    max_dd = float(metrics.get("max_drawdown", 1.0)) or 1.0
    net = float(metrics.get("net_pnl", 0.0))
    score = (
        2.0 * min(pf, 3.0)
        + 1.5 * (net / max_dd if max_dd > 0 else 0.0)
        + 0.03 * float(metrics.get("positive_days_pct", 0.0))
        + 0.01 * float(metrics.get("avg_daily_pnl", 0.0))
        + 0.3 * float(metrics.get("days_hitting_target", 0))
        - 0.5 * loss_days
        + 0.01 * float(metrics.get("win_rate_pct", 0.0))
        + 0.001 * trades
    )
    return True, "", round(score, 4)


def compare_trade_counts(base_cfg: BotConfig, m1: pd.DataFrame, spread_points: float) -> None:
    """Compare sniper max_trades_per_day = 1 vs 2 and reject 2 if the second
    trade does not earn its place."""
    results = {}
    for max_trades in (1, 2):
        cfg = copy.deepcopy(base_cfg)
        cfg.sniper_mode.enabled = True
        cfg.sniper_mode.max_trades_per_day = max_trades
        report = Backtester(cfg, m1, spread_points=spread_points).run()
        results[max_trades] = report.metrics
        logger.info(
            "max_trades={}: trades={} net={} pf={} dd={}",
            max_trades,
            report.metrics.get("total_trades", 0),
            report.metrics.get("net_pnl", 0),
            report.metrics.get("profit_factor", 0),
            report.metrics.get("max_drawdown", 0),
        )

    one, two = results[1], results[2]
    reasons: list[str] = []

    pf1 = float(two.get("profit_factor_trade1", 0) or 0)
    pf2 = float(two.get("profit_factor_trade2", 0) or 0)
    n2 = int(two.get("trades_as_no2", 0))
    if n2 == 0:
        reasons.append("no second trades were ever taken (nothing to justify max_trades=2)")
    else:
        if pf2 < pf1:
            reasons.append(f"second trade profit factor {pf2} < first trade {pf1}")
        dd1 = float(one.get("max_drawdown", 0) or 0)
        dd2 = float(two.get("max_drawdown", 0) or 0)
        if dd1 > 0 and dd2 > dd1 * 1.25:
            reasons.append(f"second trade increases drawdown too much ({dd1} -> {dd2})")
        if int(two.get("days_hitting_max_loss", 0)) > int(one.get("days_hitting_max_loss", 0)):
            reasons.append("second trade causes more daily max-loss hits")
        if float(two.get("expectancy", 0) or 0) < float(one.get("expectancy", 0) or 0):
            reasons.append("second trade reduces per-trade expectancy")
        # Proxy for "appears after bad market conditions": setup quality decay.
        e1 = float(two.get("expectancy_trade1", 0) or 0)
        e2 = float(two.get("expectancy_trade2", 0) or 0)
        if e2 < 0 <= e1:
            reasons.append("second trades lose on average while first trades win")

    print("\n===== TRADE COUNT COMPARISON (sniper mode) =====")
    for k in ("total_trades", "net_pnl", "profit_factor", "expectancy",
              "max_drawdown", "days_hitting_max_loss", "positive_days_pct"):
        print(f"{k:26s} 1-trade: {one.get(k, '-'):<12} 2-trade: {two.get(k, '-')}")
    for k in ("trades_as_no2", "win_rate_trade1_pct", "win_rate_trade2_pct",
              "profit_factor_trade1", "profit_factor_trade2",
              "days_second_trade_improved", "days_second_trade_reduced"):
        print(f"{k:26s} {two.get(k, '-')}")
    if reasons:
        print("\nVERDICT: keep max_trades_per_day = 1. The second trade is rejected:")
        for reason in reasons:
            print(f"  - {reason}")
    else:
        print("\nVERDICT: max_trades_per_day = 2 is acceptable on this sample.")
    print("================================================\n")


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper parameter optimizer")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--spread-points", type=float, default=18.0)
    parser.add_argument("--sample", type=int, default=40,
                        help="random sample size from the full grid (full grid = 59049 combos)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="data/optimizer_results.csv")
    parser.add_argument(
        "--compare-trade-counts", action="store_true",
        help="compare sniper max_trades_per_day 1 vs 2 instead of the parameter grid",
    )
    parser.add_argument(
        "--sniper-grid", action="store_true",
        help="optimize SNIPER_MODE geometry (tp_r, SL bounds, BE/partial, cost gate) "
             "instead of the base-strategy grid",
    )
    parser.add_argument(
        "--orb-grid", action="store_true",
        help="optimize ORB parameters (range length, tp_r, trend filter, range/entry filters)",
    )
    parser.add_argument("--symbol", default=None, help="instrument symbol for data + labeling")
    args = parser.parse_args()

    base_cfg = load_config(args.config)
    if args.symbol:
        base_cfg.trading.symbol = args.symbol
        inst = base_cfg.instrument_for(args.symbol)
        base_cfg.trading.allowed_symbol_aliases = inst.aliases if inst else [args.symbol]
    m1 = load_m1_from_csv(args.csv) if args.csv else load_m1_from_mt5(base_cfg, args.days)
    logger.info("Optimizing over {} M1 bars", len(m1))

    if args.compare_trade_counts:
        compare_trade_counts(base_cfg, m1, args.spread_points)
        return 0

    if args.orb_grid:
        grid, apply_fn = ORB_GRID, apply_orb_params
        logger.info("Optimizing ORB parameters ({} levers)", len(grid))
    elif args.sniper_grid:
        grid, apply_fn = SNIPER_GRID, apply_sniper_params
        logger.info("Optimizing SNIPER_MODE geometry ({} parameters)", len(grid))
    else:
        grid, apply_fn = PARAMETER_GRID, apply_params
        if base_cfg.sniper_mode.enabled:
            # The base grid explores strategy parameters (RR 0.9-1.8 etc.), which
            # sniper overrides would clobber. Use --sniper-grid for sniper tuning.
            logger.info("Grid search runs with sniper_mode disabled (use --sniper-grid for sniper)")
            base_cfg = copy.deepcopy(base_cfg)
            base_cfg.sniper_mode.enabled = False

    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*grid.values())]
    if args.sample and args.sample < len(combos):
        random.Random(args.seed).shuffle(combos)
        combos = combos[: args.sample]
    logger.info("Testing {} parameter combinations", len(combos))

    symbol = args.symbol or base_cfg.trading.symbol
    min_trades = 40 if args.orb_grid else MIN_TRADES  # ORB takes fewer, higher-quality trades
    rows: list[dict] = []
    for idx, params in enumerate(combos, 1):
        cfg = apply_fn(base_cfg, params)
        report = Backtester(cfg, m1, spread_points=args.spread_points, symbol=symbol).run()
        total_days = len(report.daily_pnl)
        accepted, reason, score = evaluate_result(report.metrics, total_days, min_trades=min_trades)
        row = {
            **params,
            "accepted": accepted,
            "reject_reason": reason,
            "rank_score": score,
            "total_trades": report.metrics.get("total_trades", 0),
            "profit_factor": report.metrics.get("profit_factor", 0),
            "win_rate_pct": report.metrics.get("win_rate_pct", 0),
            "net_pnl": report.metrics.get("net_pnl", 0),
            "max_drawdown": report.metrics.get("max_drawdown", 0),
            "max_consecutive_losses": report.metrics.get("max_consecutive_losses", 0),
            "positive_days_pct": report.metrics.get("positive_days_pct", 0),
            "avg_daily_pnl": report.metrics.get("avg_daily_pnl", 0),
            "days_hitting_target": report.metrics.get("days_hitting_target", 0),
            "days_hitting_max_loss": report.metrics.get("days_hitting_max_loss", 0),
        }
        rows.append(row)
        logger.info(
            "[{}/{}] pf={} trades={} accepted={} {}",
            idx, len(combos), row["profit_factor"], row["total_trades"], accepted, reason,
        )

    rows.sort(key=lambda r: (r["accepted"], r["rank_score"]), reverse=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Results written to {}", out_path)

    accepted = [r for r in rows if r["accepted"]]
    print(f"\nAccepted {len(accepted)}/{len(rows)} combinations. Top 5:")
    for row in accepted[:5]:
        print(
            f"  score={row['rank_score']:<8} pf={row['profit_factor']:<6} "
            f"wr={row['win_rate_pct']}% trades={row['total_trades']} net={row['net_pnl']}"
        )
    if not accepted:
        print("  No combination passed the safety filters. Do NOT force parameters to fit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
