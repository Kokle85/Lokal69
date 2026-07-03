"""Telegram integration: notifications, signal approval flow, and commands.

If telegram is disabled (or the token is missing) every method becomes a
no-op so the rest of the bot keeps running.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

from loguru import logger

from config import TelegramConfig
from models import BotMode, Signal
from utils import fmt_usd

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )

    TELEGRAM_AVAILABLE = True
except ImportError:  # pragma: no cover
    TELEGRAM_AVAILABLE = False


class BotControl(Protocol):
    """Interface main.py provides to the Telegram layer."""

    def status_text(self) -> str: ...
    def summary_text(self) -> str: ...
    def mode_text(self) -> str: ...
    def pause(self) -> str: ...
    def resume(self) -> str: ...
    async def kill(self) -> str: ...
    async def on_signal_approved(self, signal_id: int) -> str: ...
    def on_signal_rejected(self, signal_id: int) -> str: ...


@dataclass
class PendingApproval:
    signal: Signal
    sent_at: datetime
    message_id: int = 0

    def expired(self, timeout_seconds: int) -> bool:
        return (datetime.now(timezone.utc) - self.sent_at).total_seconds() > timeout_seconds


def format_signal_message(signal: Signal, daily_pnl: float, mode: BotMode) -> str:
    scalp = "BUY SCALP" if signal.direction.value == "BUY" else "SELL SCALP"
    strategy_label = (
        "High Precision Scalp" if signal.strategy.value == "HIGH_PRECISION_SCALP" else "Momentum Scalp"
    )
    return (
        f"XAUUSD {scalp}\n"
        f"\n"
        f"Strategy: {strategy_label}\n"
        f"Regime: {signal.regime.value}\n"
        f"Entry: {signal.entry:.2f}\n"
        f"SL: {signal.sl:.2f}\n"
        f"TP: {signal.tp:.2f}\n"
        f"Risk: ${signal.risk_usd:.0f}\n"
        f"Lot: {signal.lot:.2f}\n"
        f"RR: {signal.rr:.1f}\n"
        f"Score: {signal.score}/{signal.max_score}\n"
        f"Setup: {signal.setup_reason}\n"
        f"Spread: OK\n"
        f"Daily P/L: {fmt_usd(daily_pnl)}\n"
        f"Mode: {mode.value}"
    )


class TelegramService:
    def __init__(self, cfg: TelegramConfig, control: BotControl) -> None:
        self.cfg = cfg
        self.control = control
        self.app: Optional["Application"] = None
        self.pending: dict[int, PendingApproval] = {}
        self.enabled = bool(cfg.enabled and TELEGRAM_AVAILABLE and cfg.bot_token and cfg.chat_id)
        if cfg.enabled and not self.enabled:
            logger.warning(
                "Telegram disabled: missing token/chat id or python-telegram-bot. "
                "Bot continues without Telegram."
            )

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.enabled:
            return
        self.app = Application.builder().token(self.cfg.bot_token).build()
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("kill", self._cmd_kill))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode))
        self.app.add_handler(CommandHandler("summary", self._cmd_summary))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.app = None

    # ------------------------------------------------------------- outbound

    async def send(self, text: str) -> None:
        if not self.enabled or not self.app:
            return
        try:
            await self.app.bot.send_message(chat_id=self.cfg.chat_id, text=text)
        except Exception as exc:  # noqa: BLE001 - Telegram failures must never kill the bot
            logger.error("Telegram send failed: {}", exc)

    def send_nowait(self, text: str) -> None:
        """Fire-and-forget send for sync callers inside the running event loop."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.send(text))
        except RuntimeError:
            logger.debug("No running loop for Telegram send: {}", text)

    async def send_signal(self, signal: Signal, daily_pnl: float, mode: BotMode) -> None:
        text = format_signal_message(signal, daily_pnl, mode)
        if not self.enabled or not self.app:
            logger.info("Telegram disabled - signal:\n{}", text)
            return
        if mode is BotMode.SEMI_AUTO and signal.signal_id is not None:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{signal.signal_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{signal.signal_id}"),
                    ]
                ]
            )
            msg = await self.app.bot.send_message(
                chat_id=self.cfg.chat_id, text=text, reply_markup=keyboard
            )
            self.pending[signal.signal_id] = PendingApproval(
                signal=signal, sent_at=datetime.now(timezone.utc), message_id=msg.message_id
            )
        else:
            await self.send(text)

    # ------------------------------------------------------------- approvals

    def pop_expired(self) -> list[PendingApproval]:
        expired = [
            p for p in self.pending.values() if p.expired(self.cfg.approval_timeout_seconds)
        ]
        for p in expired:
            if p.signal.signal_id is not None:
                self.pending.pop(p.signal.signal_id, None)
        return expired

    def cancel_pending(self, signal_id: int) -> Optional[PendingApproval]:
        return self.pending.pop(signal_id, None)

    async def _on_button(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        await query.answer()
        try:
            action, raw_id = query.data.split(":", 1)
            signal_id = int(raw_id)
        except (ValueError, AttributeError):
            return

        pending = self.pending.pop(signal_id, None)
        if pending is None:
            await query.edit_message_text(query.message.text + "\n\n⏱ Signal no longer pending.")
            return

        if pending.expired(self.cfg.approval_timeout_seconds):
            await query.edit_message_text(query.message.text + "\n\n⏱ Signal expired.")
            self.control.on_signal_rejected(signal_id)
            return

        if action == "approve":
            logger.info("Telegram approval received for signal {}", signal_id)
            outcome = await self.control.on_signal_approved(signal_id)
            await query.edit_message_text(query.message.text + f"\n\n✅ Approved. {outcome}")
        else:
            logger.info("Telegram rejection received for signal {}", signal_id)
            outcome = self.control.on_signal_rejected(signal_id)
            await query.edit_message_text(query.message.text + f"\n\n❌ Rejected. {outcome}")

    # ------------------------------------------------------------- commands

    async def _reply(self, update: "Update", text: str) -> None:
        if update.message:
            await update.message.reply_text(text)

    async def _cmd_status(self, update: "Update", _ctx) -> None:
        await self._reply(update, self.control.status_text())

    async def _cmd_pause(self, update: "Update", _ctx) -> None:
        await self._reply(update, self.control.pause())

    async def _cmd_resume(self, update: "Update", _ctx) -> None:
        await self._reply(update, self.control.resume())

    async def _cmd_kill(self, update: "Update", _ctx) -> None:
        await self._reply(update, await self.control.kill())

    async def _cmd_mode(self, update: "Update", _ctx) -> None:
        await self._reply(update, self.control.mode_text())

    async def _cmd_summary(self, update: "Update", _ctx) -> None:
        await self._reply(update, self.control.summary_text())

    async def _cmd_help(self, update: "Update", _ctx) -> None:
        await self._reply(
            update,
            "Commands:\n"
            "/status - account, daily P/L, locks, regime, open position\n"
            "/pause - pause scanning and trading\n"
            "/resume - resume scanning\n"
            "/kill - emergency stop (optionally closes position)\n"
            "/mode - show current bot mode\n"
            "/summary - today's summary\n"
            "/help - this message",
        )
