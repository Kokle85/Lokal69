"""Configuration loading and validation (config.yaml + .env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from models import BotMode


class AccountConfig(BaseModel):
    size_usd: float = 25000


class MT5Config(BaseModel):
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    path: Optional[str] = None

    @model_validator(mode="after")
    def _merge_env(self) -> "MT5Config":
        """Values from .env take precedence over config.yaml (secrets stay out of yaml)."""
        env_login = os.getenv("MT5_LOGIN")
        if env_login:
            self.login = int(env_login)
        self.password = os.getenv("MT5_PASSWORD") or self.password
        self.server = os.getenv("MT5_SERVER") or self.server
        self.path = os.getenv("MT5_PATH") or self.path
        return self


class TradingConfig(BaseModel):
    symbol: str = "XAUUSD"
    allowed_symbol_aliases: list[str] = Field(default_factory=lambda: ["XAUUSD", "GOLD", "XAUUSDm"])
    magic_number: int = 26070301
    max_spread_points: float = 25
    max_entry_slippage_points: float = 20
    deviation_points: int = 20
    max_positions: int = 1
    require_stop_loss: bool = True
    allow_trade_without_sl: bool = False
    allow_live_auto: bool = False
    allow_kill_close_position: bool = False

    @field_validator("symbol")
    @classmethod
    def _gold_only(cls, v: str) -> str:
        allowed = {"XAUUSD", "GOLD", "XAUUSDM"}
        if v.upper() not in allowed:
            raise ValueError(
                f"symbol '{v}' is not allowed. This bot trades Gold only "
                f"(XAUUSD / GOLD / XAUUSDm)."
            )
        return v

    @model_validator(mode="after")
    def _sl_mandatory(self) -> "TradingConfig":
        if not self.require_stop_loss or self.allow_trade_without_sl:
            raise ValueError(
                "Stop loss is mandatory: require_stop_loss must be true and "
                "allow_trade_without_sl must be false."
            )
        return self


class DailyGoalsConfig(BaseModel):
    daily_profit_target_usd: float = 200
    daily_profit_lock_usd: float = 150
    max_daily_loss_usd: float = 250
    max_open_loss_usd: float = 150
    default_risk_per_trade_usd: float = 75
    risk_after_win_usd: float = 100
    risk_after_loss_usd: float = 50
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 2

    @model_validator(mode="after")
    def _sanity(self) -> "DailyGoalsConfig":
        if self.risk_after_loss_usd > self.default_risk_per_trade_usd:
            raise ValueError("risk_after_loss_usd must not exceed default risk (never increase risk after a loss).")
        if self.daily_profit_lock_usd > self.daily_profit_target_usd:
            raise ValueError("daily_profit_lock_usd must be <= daily_profit_target_usd.")
        return self


class HighPrecisionConfig(BaseModel):
    enabled: bool = True
    rr: float = 1.0
    min_precision_score: int = 8
    max_trades_per_day: int = 3


class MomentumConfig(BaseModel):
    enabled: bool = True
    rr: float = 1.6
    min_quality_score: int = 8
    max_trades_per_day: int = 1


class StrategiesConfig(BaseModel):
    high_precision_scalp: HighPrecisionConfig = HighPrecisionConfig()
    momentum_scalp: MomentumConfig = MomentumConfig()


class StrategyTuningConfig(BaseModel):
    main_timeframe: str = "M15"
    confirmation_timeframe: str = "M5"
    entry_timeframe: str = "M1"
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    use_vwap: bool = True
    pullback_ema_tolerance_atr: float = 0.35
    max_distance_from_ema_atr: float = 0.75
    rejection_wick_body_ratio: float = 1.5
    choppy_wick_body_ratio: float = 1.8
    spike_atr_multiplier: float = 2.5
    min_sl_atr: float = 0.8
    max_sl_atr: float = 2.2
    m5_structure_lookback: int = 20
    retest_tolerance_atr: float = 0.30


class PositionManagementConfig(BaseModel):
    move_to_breakeven_at_r: float = 0.7
    partial_close_enabled: bool = True
    partial_close_at_r: float = 1.0
    partial_close_percent: float = 50
    time_exit_minutes: int = 8
    max_trade_duration_minutes: int = 12
    time_exit_min_r: float = 0.25


class RegimeConfig(BaseModel):
    enabled: bool = True
    block_choppy: bool = True
    block_spike: bool = True
    block_dead_market: bool = True
    spike_atr_multiplier: float = 2.5
    choppy_wick_body_ratio: float = 1.8
    min_atr_points: float = 15
    max_atr_points: float = 350


class SessionWindow(BaseModel):
    start: str
    end: str


class SessionsConfig(BaseModel):
    timezone: str = "Europe/Skopje"
    enabled: bool = True
    windows: list[SessionWindow] = Field(
        default_factory=lambda: [
            SessionWindow(start="09:00", end="12:00"),
            SessionWindow(start="15:00", end="18:00"),
        ]
    )
    block_friday_after: str = "20:00"


class SniperModeConfig(BaseModel):
    """Highly selective 2-trades-per-day profile. When enabled it overrides
    strategy RR, SL bounds, position management and the daily risk governor."""

    enabled: bool = True
    max_trades_per_day: int = 2
    min_sniper_score: int = 10        # minimum 13-scale score to emit a signal at all
    min_execution_score: int = 11     # minimum 13-scale score to execute the first trade
    allow_no_trade_days: bool = True
    allow_second_trade_after_loss: bool = False
    second_trade_min_score: int = 11
    second_trade_after_loss_min_score: int = 12
    tp_r: float = 0.5
    min_tp_r: float = 0.35
    max_tp_r: float = 0.6
    move_to_breakeven_at_r: float = 0.25
    partial_close_enabled: bool = True
    partial_close_at_r: float = 0.35
    partial_close_percent: float = 50
    time_exit_minutes: int = 5
    max_trade_duration_minutes: int = 8
    sweep_lookback_candles: int = 20
    max_sweep_atr: float = 1.2
    min_sl_atr: float = 0.7
    max_sl_atr: float = 1.8

    @model_validator(mode="after")
    def _sanity(self) -> "SniperModeConfig":
        if not self.min_tp_r <= self.tp_r <= self.max_tp_r:
            raise ValueError("sniper_mode.tp_r must lie between min_tp_r and max_tp_r.")
        if self.max_trades_per_day > 2:
            raise ValueError("sniper_mode.max_trades_per_day cannot exceed 2.")
        if self.second_trade_after_loss_min_score < self.second_trade_min_score:
            raise ValueError(
                "second_trade_after_loss_min_score must be >= second_trade_min_score "
                "(a trade after a loss must be stricter, never looser)."
            )
        return self


class RiskConfig(BaseModel):
    """Risk settings used when sniper mode is enabled."""

    account_size_usd: float = 25000
    default_risk_per_trade_usd: float = 100
    max_risk_per_trade_usd: float = 150
    risk_after_win_usd: float = 100
    risk_after_loss_usd: float = 50
    max_daily_loss_usd: float = 150
    max_open_loss_usd: float = 150
    max_consecutive_losses: int = 1
    stop_after_first_loss: bool = True
    stop_after_second_trade: bool = True
    daily_profit_target_usd: float = 200
    daily_profit_lock_usd: float = 100

    @model_validator(mode="after")
    def _sanity(self) -> "RiskConfig":
        if self.risk_after_loss_usd > self.default_risk_per_trade_usd:
            raise ValueError("risk_after_loss_usd must not exceed the default risk.")
        if self.default_risk_per_trade_usd > self.max_risk_per_trade_usd:
            raise ValueError("default_risk_per_trade_usd must not exceed max_risk_per_trade_usd.")
        return self


class TelegramConfig(BaseModel):
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    approval_timeout_seconds: int = 60

    @model_validator(mode="after")
    def _merge_env(self) -> "TelegramConfig":
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or self.bot_token
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID") or self.chat_id
        return self


class BacktestConfig(BaseModel):
    """Realistic simulation costs and fill assumptions for backtest/optimizer.

    Defaults are deliberately conservative for XAUUSD on a retail/ECN account:
    a ~18-point base spread, per-side commission, adverse slippage on every
    fill, and an intrabar rule that assumes the stop is hit before the target
    when a single bar spans both.
    """

    base_spread_points: float = 18.0
    spread_std_points: float = 6.0          # random spread variation (seeded per trade)
    max_spread_points: float = 60.0         # spread never modelled above this
    commission_per_lot_per_side_usd: float = 3.5
    entry_slippage_points: float = 3.0      # adverse slippage crossing in
    exit_slippage_points: float = 3.0       # adverse slippage crossing out (market exits)
    requote_probability: float = 0.05       # fraction of entries that slip an extra tick
    requote_extra_points: float = 5.0
    intrabar_fill: str = "conservative"     # conservative | optimistic | random
    seed: int = 12345

    @field_validator("intrabar_fill")
    @classmethod
    def _valid_fill(cls, v: str) -> str:
        if v not in {"conservative", "optimistic", "random"}:
            raise ValueError("backtest.intrabar_fill must be conservative, optimistic or random.")
        return v


class BotConfig(BaseModel):
    mode: BotMode = BotMode.SIGNAL_ONLY
    account: AccountConfig = AccountConfig()
    mt5: MT5Config = MT5Config()
    trading: TradingConfig = TradingConfig()
    daily_goals: DailyGoalsConfig = DailyGoalsConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    strategy: StrategyTuningConfig = StrategyTuningConfig()
    position_management: PositionManagementConfig = PositionManagementConfig()
    regime: RegimeConfig = RegimeConfig()
    sessions: SessionsConfig = SessionsConfig()
    telegram: TelegramConfig = TelegramConfig()
    sniper_mode: SniperModeConfig = SniperModeConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()

    @model_validator(mode="after")
    def _live_auto_guard(self) -> "BotConfig":
        if self.mode is BotMode.LIVE_AUTO and not self.trading.allow_live_auto:
            raise ValueError(
                "mode is LIVE_AUTO but trading.allow_live_auto is false. "
                "LIVE_AUTO requires explicit confirmation via trading.allow_live_auto: true. "
                "Use demo mode first."
            )
        return self


class ConfigError(Exception):
    pass


def load_config(path: str | Path = "config.yaml", env_path: str | Path | None = None) -> BotConfig:
    """Load .env then config.yaml, returning a fully validated BotConfig."""
    load_dotenv(dotenv_path=env_path)
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path.resolve()}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from exc
