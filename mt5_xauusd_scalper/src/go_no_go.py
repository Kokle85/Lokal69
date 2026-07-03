"""Go/No-Go gate: the single honest verdict on whether to risk money.

Runs the full validation suite on real (or supplied) M1 data and applies strict,
non-negotiable criteria. Only a clean PASS across ALL of them earns a "GO".
A single failure is a "NO-GO" — the correct, capital-protecting outcome when the
data does not show a durable, cost-adjusted edge.

Usage:
    python src/go_no_go.py --days 180
    python src/go_no_go.py --csv data/m1.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from backtest import Backtester, load_m1_from_csv, load_m1_from_mt5
from config import load_config
from validation import breakeven_spread, monte_carlo, spread_sensitivity, walk_forward


# Strict acceptance criteria. These are intentionally hard to pass.
CRITERIA = {
    "min_oos_trades": 100,          # enough out-of-sample trades to trust the stats
    "min_oos_profit_factor": 1.20,  # after realistic costs, out-of-sample
    "min_oos_win_rate": 40.0,       # sanity floor given the RR
    "max_mc_prob_negative": 0.30,   # <=30% of bootstrap runs may lose money
    "max_mc_prob_dd_breach": 0.20,  # <=20% may breach the daily-loss-sized drawdown
    "min_breakeven_spread": 22.0,   # edge must survive above the ~18pt base spread
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    go: bool
    checks: list[Check] = field(default_factory=list)


def assemble_verdict(
    oos_trades: int,
    oos_profit_factor: float,
    oos_win_rate_pct: float,
    mc,  # MonteCarloResult | None
    be_spread: float | None,
    n_full_trades: int,
) -> Verdict:
    """Pure criteria application — the actual gate logic, unit-testable without
    running any backtest."""
    checks: list[Check] = []
    checks.append(Check(
        "OOS trade count",
        oos_trades >= CRITERIA["min_oos_trades"],
        f"{oos_trades} (need >= {CRITERIA['min_oos_trades']})",
    ))
    pf_ok = isinstance(oos_profit_factor, (int, float)) and oos_profit_factor >= CRITERIA["min_oos_profit_factor"]
    checks.append(Check(
        "OOS profit factor",
        bool(pf_ok),
        f"{oos_profit_factor} (need >= {CRITERIA['min_oos_profit_factor']})",
    ))
    checks.append(Check(
        "OOS win rate",
        oos_win_rate_pct >= CRITERIA["min_oos_win_rate"],
        f"{oos_win_rate_pct}% (need >= {CRITERIA['min_oos_win_rate']}%)",
    ))
    if mc is None:
        checks.append(Check("Monte Carlo", False, f"only {n_full_trades} trades (need >= 20)"))
    else:
        checks.append(Check(
            "MC prob(net<0)",
            mc.prob_negative <= CRITERIA["max_mc_prob_negative"],
            f"{mc.prob_negative:.1%} (need <= {CRITERIA['max_mc_prob_negative']:.0%})",
        ))
        checks.append(Check(
            "MC prob(drawdown breach)",
            mc.prob_drawdown_exceeds_limit <= CRITERIA["max_mc_prob_dd_breach"],
            f"{mc.prob_drawdown_exceeds_limit:.1%} (need <= {CRITERIA['max_mc_prob_dd_breach']:.0%})",
        ))
    checks.append(Check(
        "Break-even spread",
        be_spread is not None and be_spread >= CRITERIA["min_breakeven_spread"],
        f"{be_spread if be_spread is not None else 'none'} (need >= {CRITERIA['min_breakeven_spread']}pt)",
    ))
    return Verdict(go=all(c.passed for c in checks), checks=checks)


def evaluate(cfg, m1, folds: int = 4, iterations: int = 5000, wf_sample: int = 20) -> Verdict:
    """Full orchestration: run walk-forward, Monte Carlo and spread stress, then
    apply the gate. Heavy (many backtests) — this is the real thing, not a test."""
    wf = walk_forward(cfg, m1, folds=folds, sample=wf_sample)

    limit = cfg.risk.max_daily_loss_usd if cfg.sniper_mode.enabled else cfg.daily_goals.max_daily_loss_usd
    full = Backtester(cfg, m1).run()
    pnls = [t.realized for t in full.trades]
    mc = monte_carlo(pnls, iterations=iterations, drawdown_limit_usd=limit)

    rows = spread_sensitivity(cfg, m1, [10, 15, 18, 20, 22, 25, 30])
    be = breakeven_spread(rows)

    return assemble_verdict(
        oos_trades=wf.oos_trades,
        oos_profit_factor=wf.oos_profit_factor,
        oos_win_rate_pct=wf.oos_win_rate_pct,
        mc=mc,
        be_spread=be,
        n_full_trades=len(pnls),
    )


def print_verdict(v: Verdict) -> None:
    print("\n==================== GO / NO-GO ====================")
    for c in v.checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"  [{mark}] {c.name:28s} {c.detail}")
    print("---------------------------------------------------")
    if v.go:
        print("  VERDICT: GO — edge survived out-of-sample, bootstrap and spread stress.")
        print("           Next step: forward-test on DEMO for weeks before any real risk.")
    else:
        print("  VERDICT: NO-GO — the data does not show a durable, cost-adjusted edge.")
        print("           This is the system working: do NOT trade this as-is.")
    print("===================================================\n")


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper Go/No-Go gate")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5000)
    args = parser.parse_args()

    cfg = load_config(args.config)
    m1 = load_m1_from_csv(args.csv) if args.csv else load_m1_from_mt5(cfg, args.days)
    logger.info("Go/No-Go over {} M1 bars", len(m1))

    verdict = evaluate(cfg, m1, folds=args.folds, iterations=args.iterations)
    print_verdict(verdict)
    return 0 if verdict.go else 3  # non-zero exit on NO-GO for CI/automation


if __name__ == "__main__":
    sys.exit(main())
