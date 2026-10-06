"""Process settings loaded from the environment (spec section 26).

Business configuration lives in config/*.yaml and config_revisions; this
module only holds runtime wiring and secrets. `describe()` reports presence
of values, never the values themselves.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_base_url: str = "http://127.0.0.1:8000"
    display_timezone: str = "Europe/Skopje"
    log_level: str = "INFO"
    config_dir: Path = REPO_ROOT / "config"
    build_id: str = "dev"

    database_url: SecretStr | None = None
    database_pool_min: int = 1
    database_pool_max: int = 5
    # Optional `SET ROLE` executed on every pooled connection (ADR 0001), e.g. suv_backend.
    database_set_role: str | None = None
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_storage_bucket: str = "source-evidence-private"
    snapshot_storage: Literal["disabled", "local", "supabase"] = "disabled"
    snapshot_local_dir: Path = REPO_ROOT / "var" / "snapshots"

    crawl4ai_base_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: SecretStr | None = None
    crawl4ai_image: str = "unclecode/crawl4ai:0.9.4"
    crawl4ai_image_digest: str | None = None

    mcp_public_url: str | None = None
    mcp_auth_mode: Literal["oauth", "static_bearer", "dev_local"] = "oauth"
    mcp_oauth_issuer: str | None = None
    mcp_oauth_audience: str | None = None
    mcp_oauth_jwks_url: str | None = None
    mcp_oauth_client_id: str | None = None
    mcp_oauth_client_secret: SecretStr | None = None
    mcp_cursor_signing_secret: SecretStr | None = None
    mcp_allowed_origins: str = ""  # comma-separated

    scheduler_interval_seconds: int = Field(default=900, ge=60)
    source_network_enabled: bool = False
    allow_external_notifications: bool = False
    notification_provider: Literal["disabled", "slack", "mcp_events"] = "disabled"
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel_id: str | None = None
    # Non-secret Slack destination binding (all must match inbound/outbound events).
    slack_team_id: str | None = None
    slack_app_id: str | None = None
    slack_bot_id: str | None = None
    slack_bot_user_id: str | None = None
    slack_destination_approval_ref: str | None = None
    # Optional explicit egress proxy for webhook callbacks (loses IP pinning; documented).
    callback_egress_proxy_url: str | None = None
    event_bridge_enabled: bool = False
    event_bridge_provider: Literal["disabled", "mcp_events", "slack"] = "disabled"
    mcp_events_enabled: bool = False
    mcp_event_subscription_secret_encryption_key: SecretStr | None = None
    event_bridge_verified_at: str | None = None

    # --- Spec v1.1 section 37: bounded automatic seller inquiry ------------------------
    # Standing owner authorization (2026-10-06): one inquiry per vehicle/seller pair asking
    # availability, documents and lowest/final price. No per-message approval gate. Sending stays
    # off until the sender account is technically verified (mode disabled_until_sender_ready).
    seller_inquiry_mode: Literal["disabled_until_sender_ready", "automatic", "paused"] = (
        "disabled_until_sender_ready"
    )
    seller_inquiry_kill_switch: bool = False
    seller_inquiry_authorization_scope: Literal["availability_documents_lowest_price_once"] = (
        "availability_documents_lowest_price_once"
    )
    seller_inquiry_require_message_approval: bool = False
    seller_inquiry_max_per_24h: int = Field(default=2, ge=0, le=20)
    seller_inquiry_max_per_rolling_15d: int = Field(default=5, ge=0, le=100)
    seller_email_provider: Literal["", "outlook_local", "gmail_api", "microsoft_graph"] = ""
    seller_email_account_id: str | None = None
    seller_email_from: str | None = None
    seller_email_reply_to: str | None = None
    seller_email_oauth_secret_reference: str | None = None
    seller_reply_ingest_mode: Literal["local_classic_outlook", "provider_api", "disabled"] = (
        "local_classic_outlook"
    )
    seller_reply_signal_provider: Literal["slack", "disabled"] = "slack"
    mail_reconcile_interval_seconds: int = Field(default=120, ge=30, le=3600)
    mail_worker_ingest_api_url: str | None = None
    mail_worker_credential_reference: str | None = None

    llm_extraction_enabled: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_daily_budget_eur: Decimal = Decimal(0)

    # Optional official mobile.de Search API (entitlement NOT verified; disabled).
    mobile_de_api_enabled: bool = False
    mobile_de_api_credentials: SecretStr | None = None
    mobile_de_api_entitlement_reference: str | None = None

    fx_fetch_enabled: bool = False
    fx_ecb_daily_url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

    tax_rule_set_id: str | None = None
    primary_min_price_eur: Decimal = Decimal(2500)
    primary_max_price_eur: Decimal = Decimal(3000)
    max_mileage_km_exclusive: Decimal = Decimal(200000)
    mk_asking_band_min_eur: Decimal = Decimal(8000)
    mk_asking_band_max_eur: Decimal = Decimal(10000)
    manual_4000_profile_enabled: bool = False
    below_target_watch_enabled: bool = False
    proposed_min_contribution_eur: Decimal = Decimal(1500)
    contribution_threshold_approved: bool = False

    def describe(self) -> dict[str, str]:
        """Presence-only report for `doctor`; never includes secret or URL values."""
        report: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                report[name] = "set" if value.get_secret_value() else "missing"
            elif value is None or value == "":
                report[name] = "missing"
            else:
                report[name] = "set"
        return report


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
