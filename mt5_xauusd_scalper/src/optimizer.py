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


def evaluate_result(metrics: dict, total_days: int) -> tuple[bool, str, float]:
    """Return (accepted, rejection_reason, rank_score)."""
    trades = int(metrics.get("total_trades", 0))
    if trades < MIN_TRADES:
        return False, f"trades {trades} < {MIN_TRADES}", 0.0
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
    args = parser.parse_args()

    base_cfg = load_config(args.config)
    m1 = load_m1_from_csv(args.csv) if args.csv else load_m1_from_mt5(base_cfg, args.days)
    logger.info("Optimizing over {} M1 bars", len(m1))

    keys = list(PARAMETER_GRID)
    combos = [dict(zip(keys, values)) for values in itertools.product(*PARAMETER_GRID.values())]
    if args.sample and args.sample < len(combos):
        random.Random(args.seed).shuffle(combos)
        combos = combos[: args.sample]
    logger.info("Testing {} parameter combinations", len(combos))

    rows: list[dict] = []
    for idx, params in enumerate(combos, 1):
        cfg = apply_params(base_cfg, params)
        report = Backtester(cfg, m1, spread_points=args.spread_points).run()
        total_days = len(report.daily_pnl)
        accepted, reason, score = evaluate_result(report.metrics, total_days)
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
