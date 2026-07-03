"""SQLite journal: every signal, trade, daily stat and bot event is persisted."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from models import Signal, SignalStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    risk_usd REAL NOT NULL,
    lot REAL NOT NULL,
    rr REAL NOT NULL,
    score INTEGER NOT NULL,
    setup_reason TEXT NOT NULL,
    spread_points REAL NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_at TEXT,
    rejected_at TEXT,
    expired_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    lot REAL NOT NULL,
    risk_usd REAL NOT NULL,
    ticket INTEGER,
    status TEXT NOT NULL,
    close_price REAL,
    profit REAL,
    closed_at TEXT,
    close_reason TEXT,
    duration_minutes REAL,
    FOREIGN KEY (signal_id) REFERENCES signals (id)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    starting_equity REAL,
    current_equity REAL,
    realized_pnl REAL DEFAULT 0,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    consecutive_losses INTEGER DEFAULT 0,
    daily_locked INTEGER DEFAULT 0,
    daily_target_hit INTEGER DEFAULT 0,
    daily_max_loss_hit INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, db_path: str | Path = "data/trading_bot.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        logger.info("Journal database ready at {}", self.db_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- signals

    def record_signal(self, signal: Signal, status: SignalStatus = SignalStatus.NEW) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals
               (created_at, symbol, direction, strategy, regime, entry, sl, tp,
                risk_usd, lot, rr, score, setup_reason, spread_points, mode, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.created_at.isoformat(), signal.symbol, signal.direction.value,
                signal.strategy.value, signal.regime.value, signal.entry, signal.sl,
                signal.tp, signal.risk_usd, signal.lot, signal.rr, signal.score,
                signal.setup_reason, signal.spread_points, signal.mode.value, status.value,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_signal_status(self, signal_id: int, status: SignalStatus) -> None:
        ts_col = {
            SignalStatus.APPROVED: "approved_at",
            SignalStatus.REJECTED: "rejected_at",
            SignalStatus.EXPIRED: "expired_at",
        }.get(status)
        if ts_col:
            self.conn.execute(
                f"UPDATE signals SET status = ?, {ts_col} = ? WHERE id = ?",
                (status.value, _now(), signal_id),
            )
        else:
            self.conn.execute(
                "UPDATE signals SET status = ? WHERE id = ?", (status.value, signal_id)
            )
        self.conn.commit()

    # ------------------------------------------------------------- trades

    def record_trade(self, signal: Signal, ticket: int, fill_price: float, lot: float) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades
               (signal_id, created_at, symbol, direction, strategy, entry, sl, tp,
                lot, risk_usd, ticket, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.signal_id, _now(), signal.symbol, signal.direction.value,
                signal.strategy.value, fill_price, signal.sl, signal.tp, lot,
                signal.risk_usd, ticket, "OPEN",
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_trade(
        self,
        trade_id: int,
        close_price: float,
        profit: float,
        close_reason: str,
        duration_minutes: float,
    ) -> None:
        self.conn.execute(
            """UPDATE trades SET status='CLOSED', close_price=?, profit=?, closed_at=?,
               close_reason=?, duration_minutes=? WHERE id=?""",
            (close_price, profit, _now(), close_reason, duration_minutes, trade_id),
        )
        self.conn.commit()

    def update_trade_sl(self, trade_id: int, new_sl: float) -> None:
        self.conn.execute("UPDATE trades SET sl=? WHERE id=?", (new_sl, trade_id))
        self.conn.commit()

    def update_trade_lot(self, trade_id: int, lot: float) -> None:
        self.conn.execute("UPDATE trades SET lot=? WHERE id=?", (lot, trade_id))
        self.conn.commit()

    # ------------------------------------------------------------- daily stats

    def upsert_daily_stats(
        self,
        day: date,
        starting_equity: float,
        current_equity: float,
        realized_pnl: float,
        trades_count: int,
        wins: int,
        losses: int,
        consecutive_losses: int,
        daily_locked: bool,
        daily_target_hit: bool,
        daily_max_loss_hit: bool,
    ) -> None:
        self.conn.execute(
            """INSERT INTO daily_stats
               (date, starting_equity, current_equity, realized_pnl, trades_count,
                wins, losses, consecutive_losses, daily_locked, daily_target_hit,
                daily_max_loss_hit)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 current_equity=excluded.current_equity,
                 realized_pnl=excluded.realized_pnl,
                 trades_count=excluded.trades_count,
                 wins=excluded.wins,
                 losses=excluded.losses,
                 consecutive_losses=excluded.consecutive_losses,
                 daily_locked=excluded.daily_locked,
                 daily_target_hit=excluded.daily_target_hit,
                 daily_max_loss_hit=excluded.daily_max_loss_hit""",
            (
                day.isoformat(), starting_equity, current_equity, realized_pnl,
                trades_count, wins, losses, consecutive_losses,
                int(daily_locked), int(daily_target_hit), int(daily_max_loss_hit),
            ),
        )
        self.conn.commit()

    def daily_stats(self, day: date) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM daily_stats WHERE date=?", (day.isoformat(),))
        return cur.fetchone()

    # ------------------------------------------------------------- events

    def log_event(
        self, level: str, event_type: str, message: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO bot_events (created_at, level, event_type, message, payload_json) "
            "VALUES (?,?,?,?,?)",
            (_now(), level, event_type, message, json.dumps(payload) if payload else None),
        )
        self.conn.commit()
