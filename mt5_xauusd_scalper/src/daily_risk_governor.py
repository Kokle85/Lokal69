"""Daily Risk Governor: the account-level circuit breaker.

Runs before every scan, signal and order, and after every closed trade.
Capital protection always wins over the daily target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from loguru import logger

from config import DailyGoalsConfig
from models import GovernorDecision, LockEvent


@dataclass
class DailyState:
    day: date
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    last_trade_was_win: bool | None = None
    trade_results: list[float] = field(default_factory=list)
    locked: bool = False
    lock_reason: str = ""
    daily_target_hit: bool = False
    daily_max_loss_hit: bool = False
    lock_events: list[LockEvent] = field(default_factory=list)


class DailyRiskGovernor:
    def __init__(self, cfg: DailyGoalsConfig, day: date, starting_equity: float = 0.0) -> None:
        self.cfg = cfg
        self.state = DailyState(day=day, starting_equity=starting_equity)

    # ------------------------------------------------------------- lifecycle

    def reset_for_day(self, day: date, starting_equity: float) -> None:
        logger.info("Daily risk governor reset for {} (starting equity {})", day, starting_equity)
        self.state = DailyState(day=day, starting_equity=starting_equity)

    def _lock(self, reason: str, message: str) -> LockEvent:
        if not self.state.locked:
            self.state.locked = True
            self.state.lock_reason = reason
            event = LockEvent(reason=reason, message=message)
            self.state.lock_events.append(event)
            logger.warning("DAILY LOCK [{}]: {}", reason, message)
            return event
        return LockEvent(reason=self.state.lock_reason, message="already locked")

    # ------------------------------------------------------------- checks

    def check_locks(self) -> None:
        """Re-evaluate lock conditions from current state. Called every loop."""
        s, c = self.state, self.cfg
        if s.locked:
            return
        if s.realized_pnl >= c.daily_profit_target_usd:
            s.daily_target_hit = True
            self._lock(
                "daily_target",
                f"Daily target reached. Trading locked for today. "
                f"Realized P/L: ${s.realized_pnl:.2f}",
            )
        elif s.realized_pnl <= -c.max_daily_loss_usd:
            s.daily_max_loss_hit = True
            self._lock(
                "daily_max_loss",
                f"Daily risk limit reached. Trading locked for today. "
                f"Realized P/L: ${s.realized_pnl:.2f}",
            )
        elif s.trades_today >= c.max_trades_per_day:
            self._lock(
                "max_trades",
                f"Max trades per day ({c.max_trades_per_day}) reached. Trading locked for today.",
            )
        elif s.consecutive_losses >= c.max_consecutive_losses:
            self._lock(
                "consecutive_losses",
                f"{s.consecutive_losses} consecutive losses. Trading locked for today.",
            )

    def check_equity_drawdown(self, current_equity: float) -> None:
        """Lock if account equity (incl. floating) is down by the daily loss limit."""
        s = self.state
        if s.locked or s.starting_equity <= 0:
            return
        drawdown = s.starting_equity - current_equity
        if drawdown >= self.cfg.max_daily_loss_usd:
            s.daily_max_loss_hit = True
            self._lock(
                "equity_drawdown",
                f"Equity drawdown ${drawdown:.2f} reached daily loss limit. Trading locked.",
            )

    # ------------------------------------------------------------- decisions

    def evaluate_new_trade(self, signal_score: int, open_loss_usd: float = 0.0) -> GovernorDecision:
        """Decide whether a new trade may open and at what risk."""
        self.check_locks()
        s, c = self.state, self.cfg

        if s.locked:
            return GovernorDecision(False, 0.0, f"daily lock active: {s.lock_reason}")
        if open_loss_usd >= c.max_open_loss_usd:
            return GovernorDecision(
                False, 0.0,
                f"open loss ${open_loss_usd:.2f} exceeds max_open_loss ${c.max_open_loss_usd:.2f}",
            )

        risk = self._base_risk(signal_score)

        # Profit-lock zone: protect banked profit. Stricter of the two spec rules:
        # require score >= 9 AND halve the risk for any further trade.
        if s.realized_pnl >= c.daily_profit_lock_usd:
            if signal_score < 9:
                return GovernorDecision(
                    False, 0.0,
                    f"profit lock active (${s.realized_pnl:.2f} banked): "
                    f"score {signal_score} < 9 required for a final trade",
                )
            risk = risk * 0.5

        return GovernorDecision(True, risk, "ok")

    def _base_risk(self, signal_score: int) -> float:
        c, s = self.cfg, self.state
        if s.trades_today == 0 or s.last_trade_was_win is None:
            return c.default_risk_per_trade_usd
        if not s.last_trade_was_win:
            # Never increase risk after a loss.
            return min(c.risk_after_loss_usd, c.default_risk_per_trade_usd)
        # After a win, allow the larger size only for high-quality setups.
        if signal_score >= 8:
            return c.risk_after_win_usd
        return c.default_risk_per_trade_usd

    # ------------------------------------------------------------- events

    def on_trade_closed(self, profit_usd: float) -> list[LockEvent]:
        s = self.state
        s.realized_pnl += profit_usd
        s.trades_today += 1
        s.trade_results.append(profit_usd)
        if profit_usd >= 0:
            s.wins += 1
            s.consecutive_losses = 0
            s.last_trade_was_win = True
        else:
            s.losses += 1
            s.consecutive_losses += 1
            s.last_trade_was_win = False
        logger.info(
            "Trade closed for {:+.2f} USD | daily realized {:+.2f} | trades {} | consec losses {}",
            profit_usd, s.realized_pnl, s.trades_today, s.consecutive_losses,
        )
        before = len(s.lock_events)
        self.check_locks()
        return s.lock_events[before:]
