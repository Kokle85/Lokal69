"""SNIPER_MODE: maximum 2 trades per day, both must be A++ quality.

- Scores move to a 13-point scale: the 11-point base setup score plus a
  2-point liquidity-sweep bonus (sweep of the prior range, depth capped at
  max_sweep_atr * ATR, reclaimed by the latest close).
- The first trade executes only at >= min_execution_score (11/13); scores in
  [min_sniper_score, min_execution_score) are signal-only information.
- The second trade is never forced and is stricter than the first:
  score >= 11/13 after a win, and after a loss it exists only when
  allow_second_trade_after_loss is true (score >= 12/13, reduced risk).
- Default after a losing first trade: the day locks.
"""
from __future__ import annotations

import copy
from datetime import date

from loguru import logger

import indicators as ind
from config import BotConfig, DailyGoalsConfig, RiskConfig, SniperModeConfig
from daily_risk_governor import DailyRiskGovernor
from models import Direction, GovernorDecision, Signal
from regime_detector import MarketSnapshot

SNIPER_MAX_SCORE = 13
SWEEP_BONUS = 2


def apply_sniper_overrides(cfg: BotConfig) -> BotConfig:
    """Return a config copy with sniper-mode strategy/position overrides applied."""
    out = copy.deepcopy(cfg)
    s = out.sniper_mode
    rr = min(max(s.tp_r, s.min_tp_r), s.max_tp_r)
    out.strategies.high_precision_scalp.rr = rr
    out.strategies.momentum_scalp.rr = rr
    out.strategy.min_sl_atr = s.min_sl_atr
    out.strategy.max_sl_atr = s.max_sl_atr
    pm = out.position_management
    pm.move_to_breakeven_at_r = s.move_to_breakeven_at_r
    pm.partial_close_enabled = s.partial_close_enabled
    pm.partial_close_at_r = s.partial_close_at_r
    pm.partial_close_percent = s.partial_close_percent
    pm.time_exit_minutes = s.time_exit_minutes
    pm.max_trade_duration_minutes = s.max_trade_duration_minutes
    logger.info(
        "SNIPER_MODE active: max {} trades/day, TP {}R, execution score >= {}/13",
        s.max_trades_per_day, rr, s.min_execution_score,
    )
    return out


def sniper_adjust_signal(signal: Signal, snap: MarketSnapshot, sniper: SniperModeConfig) -> Signal:
    """Re-score a base signal onto the 13-point sniper scale (adds sweep bonus)."""
    swept = ind.detect_liquidity_sweep(
        snap.m1,
        bullish=signal.direction is Direction.BUY,
        atr_now=snap.atr_now,
        lookback=sniper.sweep_lookback_candles,
        max_sweep_atr=sniper.max_sweep_atr,
    )
    signal.score = signal.score + (SWEEP_BONUS if swept else 0)
    signal.max_score = SNIPER_MAX_SCORE
    if swept:
        signal.setup_reason += ", liquidity sweep reclaimed"
    return signal


class SniperGovernor(DailyRiskGovernor):
    """Daily risk governor for sniper mode (2-trade cap, stricter second trade)."""

    def __init__(
        self,
        risk_cfg: RiskConfig,
        sniper_cfg: SniperModeConfig,
        day: date,
        starting_equity: float = 0.0,
    ) -> None:
        goals = DailyGoalsConfig(
            daily_profit_target_usd=risk_cfg.daily_profit_target_usd,
            daily_profit_lock_usd=risk_cfg.daily_profit_lock_usd,
            max_daily_loss_usd=risk_cfg.max_daily_loss_usd,
            max_open_loss_usd=risk_cfg.max_open_loss_usd,
            default_risk_per_trade_usd=risk_cfg.default_risk_per_trade_usd,
            risk_after_win_usd=risk_cfg.risk_after_win_usd,
            risk_after_loss_usd=risk_cfg.risk_after_loss_usd,
            max_trades_per_day=sniper_cfg.max_trades_per_day,
            max_consecutive_losses=risk_cfg.max_consecutive_losses,
        )
        super().__init__(goals, day, starting_equity)
        self.sniper = sniper_cfg
        self.risk_cfg = risk_cfg

    # ------------------------------------------------------------- locks

    def check_locks(self) -> None:
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
            return
        if s.realized_pnl <= -c.max_daily_loss_usd:
            s.daily_max_loss_hit = True
            self._lock(
                "daily_max_loss",
                f"Daily risk limit reached. Trading locked for today. "
                f"Realized P/L: ${s.realized_pnl:.2f}",
            )
            return
        if s.trades_today >= self.sniper.max_trades_per_day:
            self._lock(
                "max_trades",
                f"Sniper mode: {s.trades_today} trades taken. Trading locked for today.",
            )
            return
        # After a losing trade the day locks by default. The aggressive opt-in
        # (allow_second_trade_after_loss) keeps the day open for ONE stricter trade.
        if s.consecutive_losses >= 1:
            second_chance = (
                self.sniper.allow_second_trade_after_loss
                and s.trades_today < self.sniper.max_trades_per_day
                and s.consecutive_losses == 1
            )
            if not second_chance:
                self._lock(
                    "loss_lock",
                    "First trade lost. Trading locked for today (stop_after_first_loss).",
                )

    # ------------------------------------------------------------- decisions

    def evaluate_new_trade(self, signal_score: int, open_loss_usd: float = 0.0) -> GovernorDecision:
        self.check_locks()
        s = self.state
        if s.locked:
            return GovernorDecision(False, 0.0, f"daily lock active: {s.lock_reason}")
        if open_loss_usd >= self.risk_cfg.max_open_loss_usd:
            return GovernorDecision(
                False, 0.0,
                f"open loss ${open_loss_usd:.2f} exceeds max_open_loss "
                f"${self.risk_cfg.max_open_loss_usd:.2f}",
            )

        if s.trades_today == 0:
            if signal_score < self.sniper.min_execution_score:
                return GovernorDecision(
                    False, 0.0,
                    f"execution score {signal_score}/{SNIPER_MAX_SCORE} below minimum "
                    f"{self.sniper.min_execution_score}",
                )
            risk = self.risk_cfg.default_risk_per_trade_usd
        elif s.last_trade_was_win:
            if signal_score < self.sniper.second_trade_min_score:
                return GovernorDecision(
                    False, 0.0,
                    f"second trade requires score >= {self.sniper.second_trade_min_score}"
                    f"/{SNIPER_MAX_SCORE} (got {signal_score})",
                )
            risk = self.risk_cfg.risk_after_win_usd
        else:
            # Second trade after a loss: only reachable with the aggressive opt-in.
            if not self.sniper.allow_second_trade_after_loss:
                return GovernorDecision(False, 0.0, "first trade lost - day is done")
            if signal_score < self.sniper.second_trade_after_loss_min_score:
                return GovernorDecision(
                    False, 0.0,
                    f"second trade after a loss requires score >= "
                    f"{self.sniper.second_trade_after_loss_min_score}/{SNIPER_MAX_SCORE} "
                    f"(got {signal_score})",
                )
            risk = self.risk_cfg.risk_after_loss_usd

        # Protect a green day: with the profit lock reached, never risk more
        # than the banked profit, so a losing final trade cannot turn the day red.
        if s.realized_pnl >= self.risk_cfg.daily_profit_lock_usd:
            risk = min(risk, s.realized_pnl)

        risk = min(risk, self.risk_cfg.max_risk_per_trade_usd)
        return GovernorDecision(True, risk, "ok")

    # ------------------------------------------------------------- reporting

    def second_trade_status(self) -> tuple[bool, str]:
        """(allowed, reason) for the /status display, assuming a perfect setup."""
        s = self.state
        if s.trades_today == 0:
            return True, "no trade taken yet - this would be trade 1"
        if s.locked:
            return False, f"daily lock active: {s.lock_reason}"
        if s.trades_today >= self.sniper.max_trades_per_day:
            return False, "2 trades already taken"
        if not s.last_trade_was_win and not self.sniper.allow_second_trade_after_loss:
            return False, "first trade lost (allow_second_trade_after_loss is false)"
        if not s.last_trade_was_win:
            return True, (
                f"only with score >= {self.sniper.second_trade_after_loss_min_score}/13 "
                f"and risk ${self.risk_cfg.risk_after_loss_usd:.0f}"
            )
        return True, f"only with score >= {self.sniper.second_trade_min_score}/13"

    def first_trade_result(self) -> str:
        if not self.state.trade_results:
            return "none yet"
        pnl = self.state.trade_results[0]
        return f"{'WIN' if pnl >= 0 else 'LOSS'} {pnl:+.2f} USD"
