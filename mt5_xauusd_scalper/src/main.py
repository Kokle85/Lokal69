"""MT5 XAUUSD Scalper MVP - main entry point and scan loop."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

# Allow `python src/main.py` from the project root.
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    BotConfig,
    ConfigError,
    DailyGoalsConfig,
    load_config,
    orb_position_management,
)
from daily_risk_governor import DailyRiskGovernor
from execution import ExecutionEngine
from journal import Journal
from market_data import MarketData
from models import (
    BotMode,
    CloseReason,
    Direction,
    ManagedPosition,
    PositionActionType,
    Signal,
    SignalStatus,
)
from mt5_connector import MT5Connector, MT5Error
from position_manager import apply_action_to_state, evaluate_position
from regime_detector import RegimeDetector, build_snapshot, geometry_atr
from risk_manager import RiskManager, orb_position_risk
from sniper_mode import SniperGovernor, apply_sniper_overrides, sniper_adjust_signal
from strategy_orb import ORBStrategy
from strategy_high_precision import HighPrecisionStrategy
from strategy_momentum import MomentumStrategy
from strategy_selector import StrategySelector
from telegram_bot import TelegramService
from utils import fmt_usd, in_session, now_in_tz

SCAN_INTERVAL_SECONDS = 5


def _normalize_bar_timeline(m1, wall_utc: datetime):
    """Shift broker-server bar labels onto real UTC.

    The last CLOSED M1 bar's close (label + 1 min) should sit within a minute
    of the wall clock while the market trades; any surplus is the broker's
    server-clock offset (brokers use half-hour steps). Returns (shifted_frame,
    offset_minutes). A scan only runs right after a fresh closed candle, so
    the "stale bars because the market is shut" case never reaches this path.
    """
    from datetime import timedelta

    last_close = m1["time"].iloc[-1].to_pydatetime()
    if last_close.tzinfo is None:
        from datetime import timezone as _tz
        last_close = last_close.replace(tzinfo=_tz.utc)
    last_close += timedelta(minutes=1)
    offset_min = int(round((last_close - wall_utc).total_seconds() / 1800.0)) * 30
    offset_min = max(-720, min(720, offset_min))
    if offset_min:
        m1 = m1.copy()
        m1["time"] = m1["time"] - timedelta(minutes=offset_min)
    return m1, offset_min


def _orb_goals(cfg: BotConfig, o) -> DailyGoalsConfig:
    """Daily risk limits for ORB mode (risk section + the effective ORB config)."""
    r = cfg.risk
    max_loss = o.daily_risk_budget_usd if o.risk_mode == "daily_budget" else r.max_daily_loss_usd
    base_risk = o.daily_risk_budget_usd if o.risk_mode == "daily_budget" else o.risk_per_trade_usd
    return DailyGoalsConfig(
        daily_profit_target_usd=r.daily_profit_target_usd,
        daily_profit_lock_usd=r.daily_profit_target_usd,
        max_daily_loss_usd=max_loss,
        max_open_loss_usd=max(max_loss, r.max_open_loss_usd),
        default_risk_per_trade_usd=base_risk,
        risk_after_win_usd=base_risk,
        risk_after_loss_usd=base_risk,
        max_trades_per_day=o.max_trades_per_day,
        max_consecutive_losses=max(o.max_trades_per_day, 2),
    )


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/bot.log", level="DEBUG", rotation="10 MB", retention="14 days")
    # python-telegram-bot and its HTTP stack log via stdlib logging, not loguru.
    # If Telegram connectivity drops mid-run they flood stderr with retry
    # tracebacks; keep them to real errors so the trading log stays readable.
    import logging

    for noisy in ("telegram", "telegram.ext", "httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)


class ScalperBot:
    def __init__(self, cfg: BotConfig) -> None:
        self.orb_mode = cfg.trading_style == "orb"
        if cfg.sniper_mode.enabled and not self.orb_mode:
            cfg = apply_sniper_overrides(cfg)
        self.cfg = cfg
        self.sniper = cfg.sniper_mode if (cfg.sniper_mode.enabled and not self.orb_mode) else None
        self.journal = Journal()
        self.connector = MT5Connector(cfg.mt5, cfg.trading)
        self.market_data = MarketData(self.connector)
        self.regime_detector = RegimeDetector(cfg.regime, cfg.trading.max_spread_points)
        self.risk_manager = RiskManager(cfg.trading, cfg.strategy)
        self.selector = StrategySelector(
            HighPrecisionStrategy(
                cfg.strategies.high_precision_scalp, cfg.strategy,
                cfg.trading.max_spread_points, cfg.regime.min_atr_points, cfg.regime.max_atr_points,
            ),
            MomentumStrategy(
                cfg.strategies.momentum_scalp, cfg.strategy,
                cfg.trading.max_spread_points, cfg.regime.min_atr_points, cfg.regime.max_atr_points,
            ),
        )
        self.telegram = TelegramService(cfg.telegram, self)
        self.execution = ExecutionEngine(
            self.connector, cfg.trading, notify=self.telegram.send_nowait
        )
        today = now_in_tz(cfg.sessions.timezone).date()
        if self.orb_mode:
            self.orb = cfg.effective_orb(cfg.trading.symbol)  # per-instrument tp_r etc.
            self.orb_strategy = ORBStrategy(self.orb, cfg.session_opens)
            self.governor: DailyRiskGovernor = DailyRiskGovernor(_orb_goals(cfg, self.orb), today)
            # ORB trades hold to TP/SL for up to max_trade_minutes; the scalp
            # position_management (8-min time exit, 0.7R breakeven) would
            # strangle a 3R runner within minutes.
            self.pm = orb_position_management(self.orb)
        elif self.sniper:
            self.governor = SniperGovernor(cfg.risk, self.sniper, today)
            self.pm = cfg.position_management
        else:
            self.governor = DailyRiskGovernor(cfg.daily_goals, today)
            self.pm = cfg.position_management
        self._traded_sessions: set = set()
        self._notified_blocks: set = set()

        self.paused = False
        self.position: Optional[ManagedPosition] = None
        self.pending_signals: dict[int, Signal] = {}
        self.current_regime = "UNKNOWN"
        self.active_strategy = "-"
        self.auto_trading_allowed = False

    # ================================================================ startup

    def startup(self) -> None:
        self.connector.connect()
        self.connector.resolve_symbol()
        spec = self.connector.symbol_spec()
        if not spec.trade_allowed:
            raise MT5Error(f"Symbol {spec.name} is not tradeable on this account.")
        # Sizing sanity line: verify this against the broker's contract spec
        # (right-click symbol -> Specification). A $1.00 move on 1.0 lot of
        # standard 100oz gold = $100.
        per_dollar = (spec.contract_size
                      or (spec.tick_value / spec.tick_size if spec.tick_size > 0 else 0))
        logger.info(
            "Contract math: contract_size={} tick={}@{} -> $1.00 move on 1.0 lot = ${:.2f}",
            spec.contract_size, spec.tick_value, spec.tick_size, per_dollar,
        )

        account = self.connector.account_info()
        is_demo = self.connector.is_demo_account()
        mode = self.cfg.mode

        if mode is BotMode.DEMO_AUTO:
            if not is_demo:
                raise MT5Error(
                    "Mode is DEMO_AUTO but the connected MT5 account is LIVE. "
                    "Auto trading is blocked. Use a demo account or SIGNAL_ONLY."
                )
            self.auto_trading_allowed = True
        elif mode is BotMode.LIVE_AUTO:
            # config.py already refuses LIVE_AUTO without allow_live_auto: true
            self.auto_trading_allowed = True
            logger.warning("LIVE_AUTO enabled by explicit config confirmation. Trade carefully.")
        elif mode is BotMode.SEMI_AUTO:
            if not is_demo and not self.cfg.trading.allow_live_auto:
                raise MT5Error(
                    "SEMI_AUTO on a LIVE account requires trading.allow_live_auto: true. "
                    "Execution is blocked for safety."
                )
            self.auto_trading_allowed = True  # only after manual approval

        today = now_in_tz(self.cfg.sessions.timezone).date()
        self.governor.reset_for_day(today, float(account.equity))
        self._restore_daily_state(today)
        self._adopt_open_position(spec)
        self.journal.log_event(
            "INFO", "startup",
            f"Bot started in {mode.value} mode on {spec.name}",
            {"balance": account.balance, "equity": account.equity, "demo": is_demo},
        )
        logger.info("Startup complete | mode={} | symbol={} | demo={}", mode.value, spec.name, is_demo)

    def _restore_daily_state(self, today) -> None:
        """Re-seed the governor from the journal after a restart. Without this,
        every restart re-arms a fresh daily budget: N restarts on a bad day = N x
        the daily loss limit - a prop-account killer."""
        row = self.journal.daily_stats(today)
        if row is None:
            return
        s = self.governor.state
        s.realized_pnl = float(row["realized_pnl"] or 0.0)
        s.trades_today = int(row["trades_count"] or 0)
        s.wins = int(row["wins"] or 0)
        s.losses = int(row["losses"] or 0)
        s.consecutive_losses = int(row["consecutive_losses"] or 0)
        if row["starting_equity"] is not None and float(row["starting_equity"]) > 0:
            s.starting_equity = float(row["starting_equity"])
        # Locks re-arm from the restored numbers (realized loss / trade count).
        self.governor.check_locks()
        logger.info(
            "Restored daily state after restart: realized {} | trades {} | locked {}",
            fmt_usd(s.realized_pnl), s.trades_today, s.locked,
        )

    def _adopt_open_position(self, spec) -> None:
        """Adopt a broker position left open by a previous run (crash/restart)
        so it keeps getting managed and its close reaches the governor."""
        from models import StrategyName

        mine = [
            p for p in self.connector.open_positions()
            if getattr(p, "magic", 0) == self.cfg.trading.magic_number
        ]
        if not mine:
            return
        p = mine[0]
        direction = Direction.BUY if p.type == 0 else Direction.SELL
        risk = 0.0
        if p.sl and spec.tick_size > 0:
            risk = abs(p.price_open - p.sl) / spec.tick_size * spec.tick_value * p.volume
        self.position = ManagedPosition(
            ticket=p.ticket, symbol=p.symbol, direction=direction,
            entry=p.price_open, sl=p.sl, tp=p.tp, lot=p.volume,
            initial_lot=p.volume, initial_sl=p.sl, risk_usd=risk,
            # True open time is in server time; counting the duration cap from
            # the restart is the safe approximation (never closes too early).
            opened_at=datetime.now(timezone.utc),
            strategy=StrategyName.ORB if self.orb_mode else StrategyName.HIGH_PRECISION,
            trade_id=None,  # journal rows for it belong to the previous run
        )
        logger.warning(
            "Adopted open position from previous run: {} {} lot @ {} (ticket {})",
            direction.value, p.volume, p.price_open, p.ticket,
        )
        self.journal.log_event(
            "WARNING", "adopted_position",
            f"Adopted {direction.value} {p.volume} lot @ {p.price_open} ticket {p.ticket}",
        )

    # ================================================================ main loop

    async def run(self) -> None:
        self.startup()
        await self.telegram.start()
        await self.telegram.send(
            f"🤖 XAUUSD Scalper started\nMode: {self.cfg.mode.value}\nSymbol: {self.connector.symbol}"
        )
        try:
            while True:
                try:
                    await self.tick()
                except MT5Error as exc:
                    logger.error("MT5 error in loop: {}", exc)
                    if not self.connector.reconnect():
                        logger.error("Reconnect failed; retrying next cycle")
                except Exception as exc:  # noqa: BLE001 - loop must survive
                    logger.exception("Unexpected error in scan loop: {}", exc)
                    self.journal.log_event("ERROR", "loop_error", str(exc))
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        finally:
            await self.telegram.stop()
            self.journal.close()
            self.connector.shutdown()

    async def tick(self) -> None:
        if not self.connector.is_alive():
            raise MT5Error("MT5 terminal connection lost")

        self._rollover_day_if_needed()
        await self._expire_pending_approvals()
        await self._manage_open_position()
        self._sync_closed_position()
        self._persist_daily_stats()

        if self.paused:
            return

        # Governor runs before every scan.
        account = self.connector.account_info()
        self.governor.check_equity_drawdown(float(account.equity))
        self.governor.check_locks()
        await self._flush_lock_events()
        if self.governor.state.locked:
            return

        if not self.orb_mode:
            allowed, session_reason = in_session(self.cfg.sessions)
            if not allowed:
                logger.debug("Skipping scan: {}", session_reason)
                return

        m1 = self.market_data.fetch_closed("M1")
        if not self.market_data.new_m1_candle(m1):
            return  # evaluate entries only on a new closed M1 candle

        if self.orb_mode:
            await self._scan_orb(m1)
        else:
            await self._scan(m1)

    # ================================================================ scanning

    async def _scan_orb(self, m1) -> None:
        from datetime import timezone as _tz

        import indicators as _ind

        spec = self.connector.symbol_spec()
        spread = self.connector.spread_points()
        m5 = self.market_data.fetch_closed("M5")
        m5_atr = float(_ind.atr(m5, self.cfg.strategy.atr_period).iloc[-1]) if len(m5) > 20 else 0.0
        if m5_atr <= 0:
            return
        trend_up = None
        period = self.orb.trend_ema_period
        if len(m5) > period:
            ema = _ind.ema(m5["close"], period)
            trend_up = float(m5["close"].iloc[-1]) > float(ema.iloc[-1])

        inst = self.cfg.instrument_for(self.connector.symbol)
        opens = inst.opens if inst else ["london", "newyork"]
        # Time base: MT5 bar labels are BROKER-SERVER time (FundingPips ~UTC+3),
        # not UTC. Left as-is, the Skopje session windows fire HOURS away from
        # the real opens (NY "opened" at 13:59 local, range formed in the dead
        # lunch lull -> 12pt SL -> 24-lot sizing -> silently rejected). Measure
        # the server offset against the wall clock and shift the bars to real
        # UTC so window and bars share one TRUE timeline.
        m1, offset_min = _normalize_bar_timeline(m1, datetime.now(_tz.utc))
        if offset_min != getattr(self, "_last_offset_min", None):
            self._last_offset_min = offset_min
            logger.info("Broker server clock offset vs UTC: {:+d} minutes", offset_min)
        now = m1["time"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=_tz.utc)
        ev = self.orb_strategy.evaluate(
            m1, now, self.connector.symbol, opens, m5_atr, spec.point, spread, trend_up=trend_up
        )
        if ev.signal is None:
            if ev.rejections:
                logger.debug("ORB no signal: {}", ev.rejections[0])
            return

        sess_key = (now_in_tz(self.cfg.session_opens.timezone).date(), ev.session_name)
        if sess_key in self._traded_sessions:
            logger.debug("ORB: {} session already traded today", ev.session_name)
            return

        signal = ev.signal
        signal.mode = self.cfg.mode
        signal.trade_number = self.governor.state.trades_today + 1
        signal.max_trades_today = self.orb.max_trades_per_day
        self.current_regime = f"ORB/{ev.session_name}"
        self.active_strategy = signal.strategy.value

        decision = self.governor.evaluate_new_trade(signal.score, self._open_loss_usd())
        await self._flush_lock_events()
        if not decision.allowed:
            logger.warning("ORB signal blocked by governor: {}", decision.reason)
            self.journal.log_event("WARNING", "risk_block", decision.reason)
            return
        signal.risk_usd = orb_position_risk(
            self.orb, self.governor.state.trades_today, self.governor.state.realized_pnl
        )

        check = self.risk_manager.validate_signal(
            signal, atr_value=abs(signal.entry - signal.sl), spread_points=spread,
            open_positions=len(self.connector.open_positions()),
            symbol_tradeable=spec.trade_allowed, point=spec.point,
        )
        if not check.ok:
            logger.warning("ORB signal rejected by risk manager: {}", check.reason)
            self.journal.log_event("WARNING", "risk_block", check.reason)
            await self._notify_blocked_once(sess_key, check.reason)
            return

        # cap_to_max: a tight ORB range can ask for more lots than the broker
        # allows (FundingPips caps XAUUSD at 5.0). Trade the capped lot at the
        # correspondingly smaller dollar risk instead of silently skipping.
        lot_result = self.risk_manager.size_position(signal, spec, cap_to_max=True)
        if not lot_result.ok:
            self.journal.log_event("WARNING", "lot_skip", lot_result.reason)
            await self._notify_blocked_once(sess_key, lot_result.reason)
            return
        signal.lot = lot_result.lot
        if lot_result.loss_at_sl_usd < signal.risk_usd - 1.0:
            cap = self.cfg.trading.max_lot or spec.volume_max
            logger.warning(
                "Lot capped at max {}: risk reduced ${:.0f} -> ${:.0f}",
                cap, signal.risk_usd, lot_result.loss_at_sl_usd,
            )
            signal.risk_usd = round(lot_result.loss_at_sl_usd, 2)

        signal.signal_id = self.journal.record_signal(signal, SignalStatus.SENT)
        # One signal per session in EVERY mode: without this, SIGNAL_ONLY and
        # SEMI_AUTO would re-emit the same breakout on each new M1 candle that
        # keeps the conditions alive (Telegram spam + duplicate journal rows).
        self._traded_sessions.add(sess_key)
        logger.info(
            "ORB SIGNAL {} {} entry {:.2f} SL {:.2f} TP {:.2f} lot {} ({})",
            signal.direction.value, signal.symbol, signal.entry, signal.sl, signal.tp,
            signal.lot, signal.setup_reason,
        )
        await self.telegram.send_signal(signal, self.governor.state.realized_pnl, self.cfg.mode)
        if self.cfg.mode is BotMode.SEMI_AUTO:
            self.pending_signals[signal.signal_id] = signal
        if self.cfg.mode in (BotMode.DEMO_AUTO, BotMode.LIVE_AUTO):
            await self._execute_signal(signal)

    async def _scan(self, m1) -> None:
        m5 = self.market_data.fetch_closed("M5")
        m15 = self.market_data.fetch_closed("M15")
        spec = self.connector.symbol_spec()
        spread = self.connector.spread_points()

        snap = build_snapshot(
            m15, m5, m1, self.cfg.strategy, spread, spec.point, self.cfg.sessions.timezone
        )
        regime_result = self.regime_detector.detect(snap)
        self.current_regime = regime_result.regime.value
        logger.info("Regime: {} ({})", self.current_regime, "; ".join(regime_result.reasons))

        selection = self.selector.select(snap, regime_result.regime)
        if selection.signal is None:
            if selection.rejections:
                logger.info("No signal: {}", selection.reason)
                for reason in selection.rejections[:6]:
                    logger.debug("  rejected: {}", reason)
            return

        signal = selection.signal
        signal.symbol = self.connector.symbol
        signal.mode = self.cfg.mode
        self.active_strategy = signal.strategy.value

        signal_only_info = False
        if self.sniper:
            sniper_adjust_signal(signal, snap, self.sniper)
            signal.trade_number = self.governor.state.trades_today + 1
            signal.max_trades_today = self.sniper.max_trades_per_day
            if signal.score < self.sniper.min_sniper_score:
                reason = (
                    f"sniper score {signal.score}/{signal.max_score} below minimum "
                    f"{self.sniper.min_sniper_score}"
                )
                logger.info("Signal rejected: {}", reason)
                self.journal.log_event("INFO", "sniper_reject", reason)
                return

        # Governor runs before every signal.
        open_loss = self._open_loss_usd()
        decision = self.governor.evaluate_new_trade(signal.score, open_loss)
        await self._flush_lock_events()
        if not decision.allowed:
            # Sniper scores in [min_sniper_score, min_execution_score) are worth
            # seeing but never trading: pass them through as signal-only info.
            if (
                self.sniper
                and self.cfg.mode is BotMode.SIGNAL_ONLY
                and "score" in decision.reason
            ):
                signal_only_info = True
                signal.note = f"not tradeable: {decision.reason}"
            else:
                logger.warning("Signal blocked by governor: {}", decision.reason)
                self.journal.log_event("WARNING", "risk_block", decision.reason)
                return
        signal.risk_usd = decision.risk_usd

        if not signal_only_info:
            check = self.risk_manager.validate_signal(
                signal,
                atr_value=geometry_atr(snap, self.cfg.strategy),
                spread_points=spread,
                open_positions=len(self.connector.open_positions()),
                symbol_tradeable=spec.trade_allowed,
                point=spec.point,
            )
            if not check.ok:
                logger.warning("Signal rejected by risk manager: {}", check.reason)
                self.journal.log_event("WARNING", "risk_block", check.reason)
                return

            lot_result = self.risk_manager.size_position(signal, spec)
            if not lot_result.ok:
                self.journal.log_event("WARNING", "lot_skip", lot_result.reason)
                return
            signal.lot = lot_result.lot

        signal.signal_id = self.journal.record_signal(signal, SignalStatus.SENT)
        logger.info(
            "SIGNAL {} {} entry {:.2f} SL {:.2f} TP {:.2f} lot {} score {}/{}",
            signal.strategy.value, signal.direction.value, signal.entry,
            signal.sl, signal.tp, signal.lot, signal.score, signal.max_score,
        )
        await self.telegram.send_signal(signal, self.governor.state.realized_pnl, self.cfg.mode)
        if signal_only_info:
            return
        if self.cfg.mode is BotMode.SEMI_AUTO:
            self.pending_signals[signal.signal_id] = signal

        if self.cfg.mode in (BotMode.DEMO_AUTO, BotMode.LIVE_AUTO):
            await self._execute_signal(signal)
        # SEMI_AUTO waits for the Telegram approval callback; SIGNAL_ONLY stops here.

    # ================================================================ execution

    async def _execute_signal(self, signal: Signal) -> None:
        if not self.auto_trading_allowed and self.cfg.mode is not BotMode.SEMI_AUTO:
            logger.warning("Auto trading not allowed - signal not executed")
            return

        spec = self.connector.symbol_spec()

        # Governor runs before every order.
        decision = self.governor.evaluate_new_trade(signal.score, self._open_loss_usd())
        if not decision.allowed:
            logger.warning("Order blocked by governor: {}", decision.reason)
            self.journal.update_signal_status(signal.signal_id, SignalStatus.CANCELLED)
            return

        result = self.execution.place_market_order(signal, spec, signal.lot)
        if not result.ok:
            self.journal.update_signal_status(signal.signal_id, SignalStatus.CANCELLED)
            self.journal.log_event("ERROR", "order_failed", result.comment)
            return

        trade_id = self.journal.record_trade(signal, result.ticket, result.price, result.volume)
        self.journal.update_signal_status(signal.signal_id, SignalStatus.EXECUTED)
        self.position = ManagedPosition(
            ticket=result.ticket,
            symbol=signal.symbol,
            direction=signal.direction,
            entry=result.price,
            sl=signal.sl,
            tp=signal.tp,
            lot=result.volume,
            initial_lot=result.volume,
            initial_sl=signal.sl,
            risk_usd=signal.risk_usd,
            opened_at=datetime.now(timezone.utc),
            strategy=signal.strategy,
            trade_id=trade_id,
        )
        await self.telegram.send(
            f"📈 Trade opened: {signal.direction.value} {result.volume} lot @ {result.price:.2f}\n"
            f"SL {signal.sl:.2f} | TP {signal.tp:.2f} | Risk ${signal.risk_usd:.0f}"
        )

    # ================================================================ position mgmt

    async def _manage_open_position(self) -> None:
        if self.position is None:
            return
        pos = self.position
        broker_positions = {p.ticket: p for p in self.connector.open_positions()}
        if pos.ticket not in broker_positions:
            return  # closed - handled by _sync_closed_position

        tick = self.connector.tick()
        current = tick.bid if pos.direction is Direction.BUY else tick.ask
        spec = self.connector.symbol_spec()

        for action in evaluate_position(pos, current, datetime.now(timezone.utc), self.pm):
            if action.action is PositionActionType.MOVE_BREAKEVEN:
                result = self.execution.modify_sl(pos.ticket, action.new_sl, pos.tp, spec)
                if result.ok:
                    apply_action_to_state(pos, action)
                    if pos.trade_id is not None:
                        self.journal.update_trade_sl(pos.trade_id, action.new_sl)
                    await self.telegram.send(f"🔒 SL moved to breakeven ({action.reason})")
            elif action.action is PositionActionType.PARTIAL_CLOSE:
                close_lot = self._round_lot(pos.lot * action.close_fraction, spec)
                if close_lot >= spec.volume_min and (pos.lot - close_lot) >= spec.volume_min:
                    result = self.execution.close_position(
                        pos.ticket, pos.direction, close_lot, spec, "partial"
                    )
                    if result.ok:
                        apply_action_to_state(pos, action)
                        if pos.trade_id is not None:
                            self.journal.update_trade_lot(pos.trade_id, pos.lot)
                        await self.telegram.send(f"✂️ Partial close {close_lot} lot ({action.reason})")
                else:
                    logger.info("Partial close skipped: lot too small to split")
                    pos.partial_done = True
            elif action.action in (PositionActionType.TIME_EXIT, PositionActionType.MAX_DURATION_EXIT):
                result = self.execution.close_position(
                    pos.ticket, pos.direction, pos.lot, spec, action.action.value.lower()
                )
                if result.ok:
                    if result.volume < pos.lot:  # partial close fill: retry rest next tick
                        pos.lot = round(pos.lot - result.volume, 8)
                    else:
                        await self.telegram.send(f"⏱ Position closed: {action.reason}")
                break

    def _sync_closed_position(self) -> None:
        if self.position is None:
            return
        pos = self.position
        if any(p.ticket == pos.ticket for p in self.connector.open_positions()):
            return

        day_start = now_in_tz(self.cfg.sessions.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        deals = self.connector.today_deals(day_start.astimezone(timezone.utc))
        matched = [d for d in deals if getattr(d, "position_id", None) == pos.ticket]
        outs = [d for d in matched if d.entry != 0]
        if not outs:
            # The closing deal hasn't shown up in history yet (server lag).
            # Committing now would book a real stop-out as a $0 "win" and the
            # daily-loss lock would never trip. Retry a few ticks first.
            self._sync_misses = getattr(self, "_sync_misses", 0) + 1
            if self._sync_misses < 12:
                logger.warning(
                    "Position {} gone but no closing deal in history yet (attempt {}) - retrying",
                    pos.ticket, self._sync_misses,
                )
                return
            logger.error(
                "Position {} closed but its deal never appeared in history; recording UNKNOWN $0.",
                pos.ticket,
            )
        self._sync_misses = 0
        # profit from the OUT deals; commission/swap from ALL deals of the
        # position (the entry deal carries its half of the commission too).
        profit = sum(d.profit for d in outs) + sum(
            getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0) for d in matched
        )
        close_price = outs[-1].price if outs else 0.0

        duration = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60.0
        reason = self._infer_close_reason(pos, close_price, duration)
        logger.info(
            "Position {} closed: {} @ {} for {} (held {:.1f} min)",
            pos.ticket, reason, close_price, fmt_usd(profit), duration,
        )
        if pos.trade_id is not None:
            self.journal.close_trade(pos.trade_id, close_price, profit, reason, duration)
        events = self.governor.on_trade_closed(profit)
        self.position = None
        self.telegram.send_nowait(
            f"🏁 Trade closed {fmt_usd(profit)} | Daily P/L: {fmt_usd(self.governor.state.realized_pnl)}"
        )
        for event in events:
            self.telegram.send_nowait(event.message)
            self.journal.log_event("WARNING", "daily_lock", event.message, {"reason": event.reason})

    def _infer_close_reason(self, pos: ManagedPosition, close_price: float, duration: float) -> str:
        """Classify how a position closed by comparing the fill to its SL/TP.

        The broker doesn't hand us a labelled reason, so we reconcile the close
        price against the trade's levels (tolerance = a few points) and fall back
        to the time-based exits the bot itself would have issued.
        """
        if close_price <= 0:
            return CloseReason.UNKNOWN.value
        spec_point = 0.01
        try:
            spec_point = self.connector.symbol_spec().point
        except MT5Error:
            pass
        tol = 25 * spec_point  # within ~25 points of a level counts as that level
        if abs(close_price - pos.tp) <= tol:
            return CloseReason.TAKE_PROFIT.value
        if abs(close_price - pos.sl) <= tol:
            # SL at/beyond breakeven that got hit still reports as STOP_LOSS.
            return CloseReason.STOP_LOSS.value
        if duration >= self.pm.max_trade_duration_minutes:
            return CloseReason.MAX_DURATION.value
        if duration >= self.pm.time_exit_minutes:
            return CloseReason.TIME_EXIT.value
        return CloseReason.MANUAL.value

    # ================================================================ helpers

    def _open_loss_usd(self) -> float:
        loss = 0.0
        for p in self.connector.open_positions():
            if p.profit < 0:
                loss += -p.profit
        return loss

    @staticmethod
    def _round_lot(lot: float, spec) -> float:
        step = spec.volume_step or 0.01
        return round(int(lot / step + 1e-9) * step, 8)

    def _rollover_day_if_needed(self) -> None:
        today = now_in_tz(self.cfg.sessions.timezone).date()
        if today != self.governor.state.day:
            account = self.connector.account_info()
            self.governor.reset_for_day(today, float(account.equity))

    def _persist_daily_stats(self) -> None:
        s = self.governor.state
        try:
            equity = float(self.connector.account_info().equity)
        except MT5Error:
            equity = 0.0
        self.journal.upsert_daily_stats(
            s.day, s.starting_equity, equity, s.realized_pnl, s.trades_today,
            s.wins, s.losses, s.consecutive_losses, s.locked,
            s.daily_target_hit, s.daily_max_loss_hit,
        )

    async def _flush_lock_events(self) -> None:
        s = self.governor.state
        for event in s.lock_events:
            if not getattr(event, "_notified", False):
                await self.telegram.send(event.message)
                self.journal.log_event("WARNING", "daily_lock", event.message, {"reason": event.reason})
                event._notified = True  # type: ignore[attr-defined]

    async def _notify_blocked_once(self, sess_key, reason: str) -> None:
        """A signal that fires but cannot be traded must NEVER be silent -
        that is how the operator misses a session. One notice per session."""
        if sess_key in self._notified_blocks:
            return
        self._notified_blocks.add(sess_key)
        await self.telegram.send(
            f"⚠️ ORB signal on {self.connector.symbol} BLOCKED: {reason}\n"
            f"(session {sess_key[1]}, {sess_key[0]})"
        )

    async def _expire_pending_approvals(self) -> None:
        for pending in self.telegram.pop_expired():
            sid = pending.signal.signal_id
            if sid is not None:
                self.pending_signals.pop(sid, None)
                self.journal.update_signal_status(sid, SignalStatus.EXPIRED)
                logger.info("Signal {} expired without approval", sid)
                await self.telegram.send(f"⏱ Signal #{sid} expired (no approval within 60s).")

    # ================================================================ BotControl (Telegram)

    def status_text(self) -> str:
        s = self.governor.state
        try:
            account = self.connector.account_info()
            balance, equity = account.balance, account.equity
        except (MT5Error, Exception):
            balance = equity = 0.0
        pos_text = "none"
        if self.position:
            pos_text = (
                f"{self.position.direction.value} {self.position.lot} lot "
                f"@ {self.position.entry:.2f} (ticket {self.position.ticket})"
            )
        perfect_score = 13 if self.sniper else 11
        next_risk = self.governor.evaluate_new_trade(signal_score=perfect_score).risk_usd
        target = (
            self.cfg.risk.daily_profit_target_usd
            if self.sniper
            else self.cfg.daily_goals.daily_profit_target_usd
        )
        sniper_lines = ""
        if self.sniper and isinstance(self.governor, SniperGovernor):
            allowed, reason = self.governor.second_trade_status()
            sniper_lines = (
                f"First trade result: {self.governor.first_trade_result()}\n"
                f"Second trade allowed: {'yes' if allowed else 'NO'}\n"
                f"Second trade condition: {reason}\n"
            )
        trades_line = (
            f"Trades today: {s.trades_today}/{self.sniper.max_trades_per_day}\n"
            if self.sniper
            else f"Trades today: {s.trades_today}\n"
        )
        return (
            f"Account size: {fmt_usd(self.cfg.account.size_usd)}\n"
            f"Balance: {fmt_usd(balance)}\n"
            f"Equity: {fmt_usd(equity)}\n"
            f"Daily realized P/L: {fmt_usd(s.realized_pnl)}\n"
            f"Daily target: {fmt_usd(target)}\n"
            f"Daily lock: {'YES - ' + s.lock_reason if s.locked else 'no'}\n"
            f"{trades_line}"
            f"{sniper_lines}"
            f"Consecutive losses: {s.consecutive_losses}\n"
            f"Regime: {self.current_regime}\n"
            f"Active strategy: {self.active_strategy}\n"
            f"Open position: {pos_text}\n"
            f"Risk for next trade: {fmt_usd(next_risk)}\n"
            f"Mode: {self.cfg.mode.value}\n"
            f"Paused: {'yes' if self.paused else 'no'}"
        )

    def summary_text(self) -> str:
        s = self.governor.state
        return (
            f"Summary {s.day}\n"
            f"Realized P/L: {fmt_usd(s.realized_pnl)}\n"
            f"Trades: {s.trades_today} (W {s.wins} / L {s.losses})\n"
            f"Consecutive losses: {s.consecutive_losses}\n"
            f"Target hit: {'yes' if s.daily_target_hit else 'no'}\n"
            f"Max loss hit: {'yes' if s.daily_max_loss_hit else 'no'}\n"
            f"Locked: {'yes - ' + s.lock_reason if s.locked else 'no'}"
        )

    def mode_text(self) -> str:
        return f"Mode: {self.cfg.mode.value}"

    def pause(self) -> str:
        self.paused = True
        self.journal.log_event("INFO", "pause", "Bot paused via Telegram")
        return "⏸ Bot paused. Scanning and trading stopped."

    def resume(self) -> str:
        self.paused = False
        self.journal.log_event("INFO", "resume", "Bot resumed via Telegram")
        return "▶️ Bot resumed."

    async def kill(self) -> str:
        self.paused = True
        self.journal.log_event("WARNING", "kill", "Emergency stop via Telegram")
        if self.cfg.trading.allow_kill_close_position and self.position:
            spec = self.connector.symbol_spec()
            result = self.execution.close_position(
                self.position.ticket, self.position.direction, self.position.lot, spec, "kill"
            )
            return f"🛑 KILLED. Position close: {'ok' if result.ok else result.comment}"
        return "🛑 KILLED. Bot paused. (allow_kill_close_position is false - position untouched)"

    async def on_signal_approved(self, signal_id: int) -> str:
        signal = self.pending_signals.pop(signal_id, None)
        if signal is None:
            return "Signal not found or already handled."
        # Recheck governor and risk limits before executing an approved signal;
        # the execution engine rechecks spread/price drift/positions itself.
        decision = self.governor.evaluate_new_trade(signal.score, self._open_loss_usd())
        if not decision.allowed:
            self.journal.update_signal_status(signal_id, SignalStatus.CANCELLED)
            return f"Cancelled: {decision.reason}"
        if not self.orb_mode:
            # ORB risk was sized by orb_position_risk at signal time and the lot
            # is already computed from it; the governor's flat base risk would
            # mislabel the trade (e.g. $300 on a lot sized for a $150 share).
            signal.risk_usd = decision.risk_usd
        self.journal.update_signal_status(signal_id, SignalStatus.APPROVED)
        await self._execute_signal(signal)
        return "Execution attempted - check trade notifications."

    def on_signal_rejected(self, signal_id: int) -> str:
        self.pending_signals.pop(signal_id, None)
        self.telegram.cancel_pending(signal_id)
        self.journal.update_signal_status(signal_id, SignalStatus.REJECTED)
        return "Signal rejected."


def main() -> int:
    setup_logging()
    try:
        cfg = load_config("config.yaml")
    except ConfigError as exc:
        logger.error("{}", exc)
        return 2

    bot = ScalperBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl+C)")
        return 0
    except MT5Error as exc:
        logger.error("MT5 error: {}", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
