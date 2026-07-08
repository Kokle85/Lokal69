"""Telegram notifications - outbound only, stdlib HTTP, zero new dependencies.

Uses the SAME Telegram bot as the ORB scalper: credentials come from the
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables or from the .env
file configured in telegram.env_file (default points at the scalper project's
.env, so both bots share one bot token and one chat).

A Telegram failure must never affect trading: every send is guarded, and a
DNS preflight disables the notifier cleanly when api.telegram.org is
unreachable (ISP block / no network) instead of flooding the log.
"""
from __future__ import annotations

import json
import socket
import urllib.request
from typing import Optional

from loguru import logger

from config_loader import Settings
from models import Signal

API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, cfg: Settings) -> None:
        tg = cfg.telegram
        self.token = tg.bot_token
        self.chat_id = tg.chat_id
        self.enabled = bool(tg.enabled and self.token and self.chat_id)
        if tg.enabled and not self.enabled:
            logger.warning(
                "Telegram disabled: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "(set them in {} or the environment). Bot continues without it.",
                tg.env_file or ".env",
            )
        if self.enabled and not self._preflight():
            logger.warning(
                "Telegram disabled for this run: api.telegram.org unreachable "
                "(DNS/ISP block). Signals go to the log and journal only."
            )
            self.enabled = False

    @staticmethod
    def _preflight() -> bool:
        try:
            socket.getaddrinfo("api.telegram.org", 443)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------- sending

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
            req = urllib.request.Request(
                f"{API}/bot{self.token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("Telegram send returned HTTP {}", resp.status)
        except Exception as exc:  # noqa: BLE001 - notifications never break trading
            logger.warning("Telegram send failed: {} (trading continues)", exc)

    def send_signal(self, signal: Signal, risk_usd: float, mode_label: str) -> None:
        self.send(format_signal_message(signal, risk_usd, mode_label))

    def send_startup(self, symbol: str, mode_label: str) -> None:
        self.send(
            f"🤖 Accumulation Breakout Bot started\n"
            f"Symbol: {symbol} (M5)\nMode: {mode_label}"
        )

    def send_trade_open(self, signal: Signal, ticket: int, price: float,
                        volume: float) -> None:
        self.send(
            f"📈 Trade opened: {signal.direction.value} {volume} lot @ {price:.2f}\n"
            f"SL {signal.stop_loss:.2f} | TP {signal.take_profit:.2f} (ticket {ticket})"
        )

    def send_error(self, text: str) -> None:
        self.send(f"⚠️ {text}")


def format_signal_message(signal: Signal, risk_usd: float, mode_label: str) -> str:
    """Order card: lot / entry / SL / TP first, with dollar outcomes."""
    arrow = "🟢 BUY" if signal.direction.value == "BUY" else "🔴 SELL"
    win_usd = risk_usd * signal.risk_reward_ratio
    zone = signal.zone
    return (
        f"{arrow} {signal.symbol} — Accumulation Breakout\n"
        f"\n"
        f"Lot: {signal.lot_size:.2f}\n"
        f"Entry: {signal.entry_price:.2f}\n"
        f"SL: {signal.stop_loss:.2f}  (−${risk_usd:.0f})\n"
        f"TP: {signal.take_profit:.2f}  (+${win_usd:.0f}, {signal.risk_reward_ratio:.1f}R)\n"
        f"\n"
        f"Mode: {signal.entry_mode.value}\n"
        f"Zone: [{zone.low:.2f} – {zone.high:.2f}] "
        f"({zone.upper_touches}U/{zone.lower_touches}L wick touches)\n"
        f"Session: {signal.session_name or '-'}\n"
        f"Spread: {signal.spread_points:.0f}pt\n"
        f"Setup: {signal.reason_for_entry}\n"
        f"Bot mode: {mode_label}"
    )
