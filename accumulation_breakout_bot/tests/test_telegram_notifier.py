"""Telegram notifier: message card format and shared-credential resolution."""
from datetime import datetime, timezone

from config_loader import TelegramConfig
from models import Direction, EntryMode, Signal, Zone
from telegram_notifier import TelegramNotifier, format_signal_message


def _signal() -> Signal:
    zone = Zone(high=2001.0, low=2000.0, upper_touches=4, lower_touches=3,
                start_time=datetime.now(timezone.utc),
                end_time=datetime.now(timezone.utc), atr=0.7)
    return Signal(
        timestamp=datetime.now(timezone.utc), symbol="XAUUSD", timeframe="M5",
        entry_mode=EntryMode.BREAKOUT_RETEST, direction=Direction.BUY,
        zone=zone, entry_price=2001.40, stop_loss=2000.30, take_profit=2004.70,
        risk_reward_ratio=3.0, atr=0.7, lot_size=1.13, spread_points=19,
        session_name="london", reason_for_entry="retest entry after BUY breakout",
    )


def test_signal_card_leads_with_lot_entry_sl_tp():
    text = format_signal_message(_signal(), risk_usd=125.0, mode_label="SIGNAL-ONLY")
    lines = text.splitlines()
    assert lines[0].startswith("🟢 BUY XAUUSD")
    assert lines[2] == "Lot: 1.13"
    assert lines[3] == "Entry: 2001.40"
    assert "SL: 2000.30" in lines[4] and "$125" in lines[4]
    assert "TP: 2004.70" in lines[5] and "$375" in lines[5] and "3.0R" in lines[5]
    assert "4U/3L wick touches" in text
    assert "SIGNAL-ONLY" in text


def test_env_file_credentials_are_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc:123\nTELEGRAM_CHAT_ID=42\n")
    cfg = TelegramConfig(env_file=str(env))
    assert cfg.bot_token == "abc:123"
    assert cfg.chat_id == "42"


def test_env_vars_beat_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=1\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    cfg = TelegramConfig(env_file=str(env))
    assert cfg.bot_token == "env-token"
    assert cfg.chat_id == "99"


def test_disabled_without_credentials_and_never_raises(cfg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg.telegram = TelegramConfig(env_file="/nonexistent/.env")
    notifier = TelegramNotifier(cfg)
    assert not notifier.enabled
    notifier.send("must be a silent no-op")  # would raise if it tried the network
