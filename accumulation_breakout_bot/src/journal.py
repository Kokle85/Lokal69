"""Trade journal: every signal, executed trade, and rejection lands in one
CSV per day under data/journals/. A journaling failure must never stop
trading, so all writes are guarded.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from models import Signal

FIELDS = [
    "timestamp", "record_type", "symbol", "timeframe", "entry_mode", "direction",
    "zone_high", "zone_low", "zone_size", "upper_wick_touches", "lower_wick_touches",
    "entry_price", "stop_loss", "take_profit", "risk_reward_ratio", "lot_size",
    "spread", "ema_value", "session_name", "trade_result", "profit_loss",
    "r_result", "reason_for_entry", "reason_for_rejection",
]


class Journal:
    def __init__(self, directory: str | Path = "data/journals") -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        return self.dir / f"journal_{datetime.now(timezone.utc):%Y%m%d}.csv"

    def _write(self, row: dict[str, Any]) -> None:
        try:
            path = self._path()
            new_file = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow({k: row.get(k, "") for k in FIELDS})
        except OSError as exc:
            logger.error("Journal write failed: {} (trading continues)", exc)

    # ------------------------------------------------------------- records

    def _signal_row(self, signal: Signal) -> dict[str, Any]:
        return {
            "timestamp": signal.timestamp.isoformat(),
            "symbol": signal.symbol,
            "timeframe": signal.timeframe,
            "entry_mode": signal.entry_mode.value,
            "direction": signal.direction.value,
            "zone_high": round(signal.zone.high, 2),
            "zone_low": round(signal.zone.low, 2),
            "zone_size": round(signal.zone.size, 2),
            "upper_wick_touches": signal.zone.upper_touches,
            "lower_wick_touches": signal.zone.lower_touches,
            "entry_price": round(signal.entry_price, 2),
            "stop_loss": round(signal.stop_loss, 2),
            "take_profit": round(signal.take_profit, 2),
            "risk_reward_ratio": signal.risk_reward_ratio,
            "lot_size": signal.lot_size,
            "spread": signal.spread_points,
            "ema_value": round(signal.ema_value, 2),
            "session_name": signal.session_name,
            "reason_for_entry": signal.reason_for_entry,
        }

    def log_signal(self, signal: Signal) -> None:
        row = self._signal_row(signal)
        row["record_type"] = "SIGNAL"
        self._write(row)

    def log_trade_open(self, signal: Signal, ticket: int) -> None:
        row = self._signal_row(signal)
        row["record_type"] = "TRADE_OPEN"
        row["trade_result"] = f"ticket {ticket}"
        self._write(row)

    def log_trade_close(self, signal: Signal, result: str, profit_loss: float,
                        r_result: float) -> None:
        row = self._signal_row(signal)
        row["record_type"] = "TRADE_CLOSE"
        row["trade_result"] = result
        row["profit_loss"] = round(profit_loss, 2)
        row["r_result"] = round(r_result, 2)
        self._write(row)

    def log_rejection(self, timestamp: datetime, symbol: str, timeframe: str,
                      reason: str, session_name: str = "",
                      signal: Optional[Signal] = None) -> None:
        row: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "record_type": "REJECTION",
            "symbol": symbol,
            "timeframe": timeframe,
            "session_name": session_name,
            "reason_for_rejection": reason,
        }
        if signal is not None:
            row.update(self._signal_row(signal))
            row["record_type"] = "REJECTION"
            row["reason_for_rejection"] = reason
        self._write(row)
