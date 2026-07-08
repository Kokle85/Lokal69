"""Typed configuration loaded from config/config.yaml (pydantic-validated).

Strategy logic files never hardcode parameters: everything tunable lives in
the YAML and arrives here, validated, or the bot refuses to start.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, field_validator, model_validator

from models import EntryMode


class SessionWindow(BaseModel):
    start: str = "08:00"
    end: str = "12:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ValueError(f"session time must be HH:MM, got {v!r}")
        return v


class SessionsConfig(BaseModel):
    london: SessionWindow = SessionWindow(start="08:00", end="12:00")
    newyork: SessionWindow = SessionWindow(start="13:30", end="17:00")


class MT5Config(BaseModel):
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    path: Optional[str] = None
    magic_number: int = 26110701
    deviation_points: int = 20


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE .env parser (no python-dotenv dependency)."""
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


class TelegramConfig(BaseModel):
    """Shares the ORB scalper's Telegram bot: token/chat id resolve from (in
    order) explicit yaml values, process environment, then the env_file -
    which by default points at the scalper project's .env."""

    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    env_file: str = "../mt5_xauusd_scalper/.env"

    @model_validator(mode="after")
    def _merge_env(self) -> "TelegramConfig":
        import os

        file_vals: dict[str, str] = {}
        if self.env_file:
            file_vals = _read_env_file(Path(self.env_file))
        self.bot_token = (self.bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
                          or file_vals.get("TELEGRAM_BOT_TOKEN", ""))
        self.chat_id = (self.chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
                        or file_vals.get("TELEGRAM_CHAT_ID", ""))
        return self


class BacktestConfig(BaseModel):
    spread_points: float = 25.0
    point: float = 0.01


class Settings(BaseModel):
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    entry_mode: EntryMode = EntryMode.BREAKOUT_RETEST

    risk_per_trade_percent: float = 0.5
    risk_reward_ratio: float = 3.0

    zone_lookback_candles: int = 30
    minimum_upper_wick_touches: int = 3
    minimum_lower_wick_touches: int = 3
    touch_tolerance_atr_multiplier: float = 0.15
    min_zone_size_atr: float = 0.8
    max_zone_size_atr: float = 2.0
    min_body_inside_zone_percent: float = 70.0
    max_zone_slope_atr: float = 0.5

    breakout_buffer_atr_multiplier: float = 0.15
    min_breakout_body_atr_multiplier: float = 0.4
    max_rejection_wick_percent: float = 40.0

    retest_timeout_candles: int = 12
    retest_max_penetration_percent: float = 50.0

    stop_buffer_atr_multiplier: float = 0.10
    min_stop_distance_atr: float = 0.5
    max_stop_distance_atr: float = 2.5
    # "zone_opposite" = SL beyond the far side of the zone (original spec);
    # "zone_mid" = SL beyond the zone midpoint (half the risk, so the 3R
    # target sits at a reachable distance on M5).
    stop_mode: str = "zone_opposite"

    use_ema_filter: bool = True
    ema_period: int = 200
    use_session_filter: bool = True
    sessions: SessionsConfig = SessionsConfig()
    max_spread_points: float = 35.0
    max_trades_per_day: int = 2
    cooldown_minutes: int = 60

    use_news_filter: bool = False
    news_block_minutes_before: int = 30
    news_block_minutes_after: int = 30

    atr_period: int = 14
    live_trading_enabled: bool = False

    mt5: MT5Config = MT5Config()
    telegram: TelegramConfig = TelegramConfig()
    backtest: BacktestConfig = BacktestConfig()

    @model_validator(mode="after")
    def _sanity(self) -> "Settings":
        if self.timeframe != "M5":
            raise ValueError("this bot trades the M5 timeframe only (timeframe: M5)")
        if self.risk_reward_ratio <= 0:
            raise ValueError("risk_reward_ratio must be positive")
        if not 0 < self.risk_per_trade_percent <= 5:
            raise ValueError("risk_per_trade_percent must be in (0, 5]")
        if self.min_zone_size_atr >= self.max_zone_size_atr:
            raise ValueError("min_zone_size_atr must be < max_zone_size_atr")
        if self.min_stop_distance_atr >= self.max_stop_distance_atr:
            raise ValueError("min_stop_distance_atr must be < max_stop_distance_atr")
        if self.zone_lookback_candles < 10:
            raise ValueError("zone_lookback_candles must be >= 10")
        if not 0 < self.min_body_inside_zone_percent <= 100:
            raise ValueError("min_body_inside_zone_percent must be in (0, 100]")
        if not 0 < self.max_rejection_wick_percent <= 100:
            raise ValueError("max_rejection_wick_percent must be in (0, 100]")
        if self.stop_mode not in {"zone_opposite", "zone_mid"}:
            raise ValueError("stop_mode must be 'zone_opposite' or 'zone_mid'")
        return self


class ConfigError(Exception):
    pass


def load_settings(path: str | Path = "config/config.yaml") -> Settings:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    try:
        return Settings(**raw)
    except Exception as exc:  # pydantic ValidationError -> readable message
        raise ConfigError(f"invalid configuration: {exc}") from exc
