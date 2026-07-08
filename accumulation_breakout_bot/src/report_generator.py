"""Backtest report output: CSV + JSON under data/reports/, plus a console table."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from models import BacktestReport


def write_reports(reports: list[BacktestReport],
                  directory: str | Path = "data/reports") -> tuple[Path, Path]:
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"backtest_{stamp}.csv"
    json_path = out_dir / f"backtest_{stamp}.json"

    rows = [r.as_row() for r in reports]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)

    logger.info("Reports written: {} and {}", csv_path, json_path)
    return csv_path, json_path


def print_table(reports: list[BacktestReport]) -> None:
    header = (f"{'configuration':<44} {'trd':>4} {'WR%':>6} {'avgR':>6} "
              f"{'totR':>7} {'PF':>6} {'ddR':>6} {'mcl':>4}")
    print("\n" + header)
    print("-" * len(header))
    for r in reports:
        pf = f"{r.profit_factor:6.2f}" if r.profit_factor != float("inf") else "   inf"
        print(f"{r.label:<44} {r.total_trades:>4} {r.win_rate_pct:>6.1f} "
              f"{r.average_r:>6.2f} {r.total_r:>7.1f} {pf} "
              f"{r.max_drawdown_r:>6.1f} {r.max_consecutive_losses:>4}")
    print()
