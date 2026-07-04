"""Validation tooling: prove (or disprove) that the edge is real, not curve-fit.

Three independent checks:

  1. walk_forward     - optimize on a train window, measure on the *next* window
                        the model never saw. Out-of-sample is the only honest test.
  2. monte_carlo      - bootstrap-resample the trade sequence to get confidence
                        intervals for net PnL and drawdown, and a probability of
                        a losing run. Tells you if a good result was luck.
  3. spread_sensitivity - re-run at a range of spreads to find the spread at which
                        the edge dies. Critical for gold, where spread is large.

Usage:
    python src/validation.py --csv data/m1.csv --all
    python src/validation.py --csv data/m1.csv --monte-carlo
    python src/validation.py --days 120 --walk-forward --folds 4
"""
from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from backtest import Backtester, load_m1_from_csv, load_m1_from_mt5
from config import BotConfig, load_config
from optimizer import (
    PARAMETER_GRID,
    SNIPER_GRID,
    apply_params,
    apply_sniper_params,
    evaluate_result,
)


# ============================================================ Monte Carlo

@dataclass
class MonteCarloResult:
    iterations: int
    net_pnl_mean: float
    net_pnl_p05: float
    net_pnl_p50: float
    net_pnl_p95: float
    max_drawdown_p50: float
    max_drawdown_p95: float
    prob_negative: float
    prob_drawdown_exceeds_limit: float


def monte_carlo(
    trade_pnls: list[float],
    iterations: int = 5000,
    seed: int = 20260703,
    drawdown_limit_usd: float = 150.0,
) -> MonteCarloResult | None:
    """Bootstrap-resample the trade order to bound net PnL and drawdown.

    Reshuffling (and resampling with replacement) breaks any lucky ordering:
    if the strategy only looks good because winners happened to cluster, the
    distribution here exposes it.
    """
    pnls = np.asarray(trade_pnls, dtype=float)
    if len(pnls) < 20:
        return None
    rng = np.random.default_rng(seed)
    nets = np.empty(iterations)
    max_dds = np.empty(iterations)
    n = len(pnls)
    for k in range(iterations):
        sample = rng.choice(pnls, size=n, replace=True)
        equity = np.cumsum(sample)
        peak = np.maximum.accumulate(equity)
        nets[k] = equity[-1]
        max_dds[k] = float((peak - equity).max())
    return MonteCarloResult(
        iterations=iterations,
        net_pnl_mean=round(float(nets.mean()), 2),
        net_pnl_p05=round(float(np.percentile(nets, 5)), 2),
        net_pnl_p50=round(float(np.percentile(nets, 50)), 2),
        net_pnl_p95=round(float(np.percentile(nets, 95)), 2),
        max_drawdown_p50=round(float(np.percentile(max_dds, 50)), 2),
        max_drawdown_p95=round(float(np.percentile(max_dds, 95)), 2),
        prob_negative=round(float((nets < 0).mean()), 4),
        prob_drawdown_exceeds_limit=round(float((max_dds > drawdown_limit_usd).mean()), 4),
    )


# ============================================================ spread sensitivity

def spread_sensitivity(
    cfg: BotConfig, m1: pd.DataFrame, spreads: list[float]
) -> list[dict]:
    """Run the backtest across a range of base spreads; find where the edge dies."""
    rows: list[dict] = []
    for s in spreads:
        report = Backtester(cfg, m1, spread_points=s).run()
        met = report.metrics
        rows.append(
            {
                "spread_points": s,
                "trades": met.get("total_trades", 0),
                "profit_factor": met.get("profit_factor", 0),
                "net_pnl": met.get("net_pnl", 0),
                "win_rate_pct": met.get("win_rate_pct", 0),
            }
        )
    return rows


def breakeven_spread(rows: list[dict]) -> float | None:
    """Largest spread at which profit_factor is still >= 1.0."""
    ok = [r["spread_points"] for r in rows
          if isinstance(r["profit_factor"], (int, float)) and r["profit_factor"] >= 1.0]
    return max(ok) if ok else None


# ============================================================ walk-forward

@dataclass
class WalkForwardResult:
    folds: int
    oos_trades: int
    oos_net_pnl: float
    oos_profit_factor: float
    oos_win_rate_pct: float
    per_fold: list[dict] = field(default_factory=list)


def _slice_metrics(pnls: list[float]) -> dict:
    arr = np.asarray(pnls, dtype=float)
    if len(arr) == 0:
        return {"trades": 0, "net": 0.0, "pf": 0.0, "win_rate": 0.0}
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gl = float(-losses.sum()) if len(losses) else 0.0
    return {
        "trades": len(arr),
        "net": round(float(arr.sum()), 2),
        "pf": round(float(wins.sum()) / gl, 3) if gl > 0 else float("inf"),
        "win_rate": round(100.0 * len(wins) / len(arr), 2),
    }


def walk_forward(
    cfg: BotConfig,
    m1: pd.DataFrame,
    folds: int = 4,
    sample: int = 20,
    seed: int = 7,
) -> WalkForwardResult:
    """Rolling train/test: optimize on each train block, score the next block.

    The test blocks are stitched into a single out-of-sample equity curve — that
    aggregate is the number that matters. In-sample profit is not evidence.
    """
    m1 = m1.reset_index(drop=True)
    n = len(m1)
    block = n // (folds + 1)
    if block < 2000:
        logger.warning("Walk-forward: only {} bars/block - results may be thin", block)

    # Tune the knobs that actually apply: sniper geometry when sniper mode is
    # on (its overrides would clobber the base-strategy grid), else the base grid.
    if cfg.sniper_mode.enabled:
        grid, apply_fn = SNIPER_GRID, apply_sniper_params
    else:
        grid, apply_fn = PARAMETER_GRID, apply_params
    keys = list(grid)
    import itertools
    import random

    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]
    random.Random(seed).shuffle(combos)
    combos = combos[: max(1, sample)]

    oos_pnls: list[float] = []
    per_fold: list[dict] = []

    for f in range(folds):
        train = m1.iloc[: block * (f + 1)]
        test = m1.iloc[block * (f + 1) : block * (f + 2)]
        if len(test) < 500:
            break

        best_params, best_score = None, -1e18
        for params in combos:
            trial = apply_fn(cfg, params)
            rep = Backtester(trial, train.copy()).run()
            accepted, _, score = evaluate_result(rep.metrics, len(rep.daily_pnl))
            if score > best_score:
                best_score, best_params = score, params

        tuned = apply_fn(cfg, best_params) if best_params else copy.deepcopy(cfg)
        test_rep = Backtester(tuned, test.copy()).run()
        fold_pnls = [t.realized for t in test_rep.trades]
        oos_pnls.extend(fold_pnls)
        per_fold.append(
            {
                "fold": f + 1,
                "train_bars": len(train),
                "test_bars": len(test),
                "best_params": best_params,
                **{f"oos_{k}": v for k, v in _slice_metrics(fold_pnls).items()},
            }
        )
        logger.info("Fold {}: OOS {}", f + 1, _slice_metrics(fold_pnls))

    agg = _slice_metrics(oos_pnls)
    return WalkForwardResult(
        folds=len(per_fold),
        oos_trades=agg["trades"],
        oos_net_pnl=agg["net"],
        oos_profit_factor=agg["pf"],
        oos_win_rate_pct=agg["win_rate"],
        per_fold=per_fold,
    )


# ============================================================ CLI

def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper strategy validation")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--monte-carlo", action="store_true")
    parser.add_argument("--spread-sensitivity", action="store_true")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5000)
    args = parser.parse_args()

    cfg = load_config(args.config)
    m1 = load_m1_from_csv(args.csv) if args.csv else load_m1_from_mt5(cfg, args.days)
    logger.info("Validation over {} M1 bars", len(m1))

    run_mc = args.monte_carlo or args.all
    run_ss = args.spread_sensitivity or args.all
    run_wf = args.walk_forward or args.all
    if not (run_mc or run_ss or run_wf):
        run_mc = run_ss = True  # sensible default

    if run_ss:
        print("\n===== SPREAD SENSITIVITY =====")
        rows = spread_sensitivity(cfg, m1, [10, 15, 18, 20, 25, 30, 40])
        for r in rows:
            print(f"  spread {r['spread_points']:>4}pt  trades {r['trades']:>4}  "
                  f"pf {r['profit_factor']}  net {r['net_pnl']}  wr {r['win_rate_pct']}%")
        be = breakeven_spread(rows)
        print(f"  break-even spread (pf>=1.0): {be if be is not None else 'NONE - no edge at any spread'}")

    if run_mc:
        print("\n===== MONTE CARLO (bootstrap) =====")
        limit = cfg.risk.max_daily_loss_usd if cfg.sniper_mode.enabled else cfg.daily_goals.max_daily_loss_usd
        report = Backtester(cfg, m1).run()
        pnls = [t.realized for t in report.trades]
        mc = monte_carlo(pnls, iterations=args.iterations, drawdown_limit_usd=limit)
        if mc is None:
            print(f"  Not enough trades ({len(pnls)}) for Monte Carlo (need >= 20).")
        else:
            print(f"  iterations           {mc.iterations}")
            print(f"  net PnL  p05/p50/p95 {mc.net_pnl_p05} / {mc.net_pnl_p50} / {mc.net_pnl_p95}")
            print(f"  max drawdown p50/p95 {mc.max_drawdown_p50} / {mc.max_drawdown_p95}")
            print(f"  probability net < 0  {mc.prob_negative:.1%}")
            print(f"  P(drawdown > {limit:.0f})   {mc.prob_drawdown_exceeds_limit:.1%}")

    if run_wf:
        print("\n===== WALK-FORWARD (out-of-sample) =====")
        wf = walk_forward(cfg, m1, folds=args.folds)
        print(f"  folds completed       {wf.folds}")
        print(f"  OOS trades            {wf.oos_trades}")
        print(f"  OOS net PnL           {wf.oos_net_pnl}")
        print(f"  OOS profit factor     {wf.oos_profit_factor}")
        print(f"  OOS win rate          {wf.oos_win_rate_pct}%")
        print("  (in-sample profit is NOT evidence; only these OOS numbers count)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
