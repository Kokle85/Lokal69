"""Run the ORB pipeline across every configured instrument and compare them.

Each instrument uses its effective ORB config from config.yaml: gold trades
London + New York with tp_r 1.3; the index CFDs (US100/US500) trade the New
York open ONLY with RR 3:1. Pulls each instrument's own M1 history from MT5
(resolving the instrument's own aliases, never a gold fallback), backtests it,
optionally walk-forward validates it, and prints a side-by-side table so you
can see which instruments carry a real edge.

Usage:
    python src/run_instruments.py --days 90                 # backtest each instrument
    python src/run_instruments.py --days 120 --validate     # + out-of-sample walk-forward
    python src/run_instruments.py --only US100,US500        # subset
    python src/run_instruments.py --days 90 --out data/instruments.csv
"""
from __future__ import annotations

import argparse
import copy
import csv as csv_mod
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from backtest import Backtester, load_m1_from_mt5
from config import BotConfig, load_config
from mt5_connector import MT5Error
from validation import walk_forward


def _prep_cfg(base: BotConfig, inst) -> BotConfig:
    """Config clone pointed at one instrument (symbol + its own aliases)."""
    cfg = copy.deepcopy(base)
    cfg.trading_style = "orb"
    cfg.trading.symbol = inst.symbol
    cfg.trading.allowed_symbol_aliases = inst.aliases or [inst.symbol]
    return cfg


def run(base: BotConfig, days: int, only: list[str], validate: bool,
        folds: int, spread_points) -> list[dict]:
    rows: list[dict] = []
    instruments = [i for i in base.instruments if i.enabled]
    if only:
        want = {s.upper() for s in only}
        instruments = [i for i in instruments if i.symbol.upper() in want]

    for inst in instruments:
        eff = base.effective_orb(inst.symbol)
        logger.info(
            "=== {} | opens={} | tp_r={} | max_trades/day={} ===",
            inst.symbol, inst.opens, eff.tp_r, eff.max_trades_per_day,
        )
        cfg = _prep_cfg(base, inst)
        try:
            m1 = load_m1_from_mt5(cfg, days)
        except MT5Error as exc:
            logger.warning("SKIP {}: {}", inst.symbol, exc)
            rows.append({"symbol": inst.symbol, "status": "NOT AVAILABLE",
                         "detail": str(exc)[:80]})
            continue

        report = Backtester(cfg, m1, spread_points=spread_points, symbol=inst.symbol).run()
        m = report.metrics
        row = {
            "symbol": inst.symbol,
            "status": "ok",
            "opens": "+".join(inst.opens),
            "tp_r": eff.tp_r,
            "trades": m.get("total_trades", 0),
            "win_rate_pct": m.get("win_rate_pct", 0),
            "profit_factor": m.get("profit_factor", 0),
            "net_pnl": m.get("net_pnl", 0),
            "max_drawdown": m.get("max_drawdown", 0),
            "positive_days_pct": m.get("positive_days_pct", 0),
            "avg_daily_pnl": m.get("avg_daily_pnl", 0),
        }
        if validate:
            wf = walk_forward(cfg, m1, folds=folds, symbol=inst.symbol)
            row["oos_trades"] = wf.oos_trades
            row["oos_profit_factor"] = wf.oos_profit_factor
            row["oos_net_pnl"] = wf.oos_net_pnl
            row["oos_win_rate_pct"] = wf.oos_win_rate_pct
        rows.append(row)
    return rows


def print_table(rows: list[dict], validate: bool) -> None:
    print("\n================= INSTRUMENT COMPARISON (ORB) =================")
    hdr = f"{'symbol':8} {'opens':14} {'tpR':4} {'trades':>6} {'WR%':>6} {'PF':>6} {'net':>9} {'DD':>8} {'pos%':>6}"
    if validate:
        hdr += f" {'OOS_PF':>7} {'OOS_net':>9} {'OOS_tr':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r.get("status") != "ok":
            print(f"{r['symbol']:8} {'-- ' + r.get('status', ''):40}")
            continue
        line = (f"{r['symbol']:8} {r['opens']:14} {r['tp_r']:<4} {r['trades']:>6} "
                f"{r['win_rate_pct']:>6} {r['profit_factor']:>6} {r['net_pnl']:>9} "
                f"{r['max_drawdown']:>8} {r['positive_days_pct']:>6}")
        if validate:
            line += f" {r.get('oos_profit_factor',''):>7} {r.get('oos_net_pnl',''):>9} {r.get('oos_trades',''):>7}"
        print(line)
    print("=" * len(hdr))
    print("Read: PF and OOS_PF > 1.0 = profitable; OOS is the honest number.")
    print("Index rows 'NOT AVAILABLE' mean the broker lacks that symbol - fix")
    print("the name/aliases in config.yaml (instruments) from MT5 Market Watch.\n")


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="Run ORB across all instruments")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--only", default=None, help="comma-separated subset, e.g. US100,US500")
    parser.add_argument("--validate", action="store_true", help="also run walk-forward OOS")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--spread-points", type=float, default=None)
    parser.add_argument("--out", default="data/instruments.csv")
    args = parser.parse_args()

    base = load_config(args.config)
    if base.trading_style != "orb":
        logger.warning("trading_style is '{}' - forcing ORB for this comparison", base.trading_style)
    only = [s.strip() for s in args.only.split(",")] if args.only else []

    rows = run(base, args.days, only, args.validate, args.folds, args.spread_points)
    print_table(rows, args.validate)

    ok = [r for r in rows if r.get("status") == "ok"]
    if ok:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as fh:
            writer = csv_mod.DictWriter(fh, fieldnames=list(ok[0].keys()))
            writer.writeheader()
            writer.writerows(ok)
        logger.info("Comparison written to {}", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
