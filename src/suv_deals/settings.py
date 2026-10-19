"""Process settings loaded from the environment (spec section 26).

Business configuration lives in config/*.yaml and config_revisions; this
module only holds runtime wiring and secrets. `describe()` reports presence
of values, never the values themselves.

Sources, highest precedence first: explicit init values, process environment, the ``.env``
file, then - only when ``SUV_DEALS_SECRETS_DIR`` names an existing directory (Docker/Compose
secrets, e.g. ``/run/secrets``) - one file per setting named like the variable
(``database_url`` or ``DATABASE_URL``; surrounding whitespace is stripped). A missing
directory is ignored (no warning, no failure), so the same image runs with or without secrets.

Installed (non-editable) deployments locate ``config/`` and ``supabase/migrations`` through
``SUV_DEALS_HOME`` (or the explicit ``CONFIG_DIR`` / ``MIGRATIONS_DIR``); the repository root
(``REPO_ROOT``) stays the default for a source checkout.
"""

from __future__ import annotations

import ipaddress
import os
import types
import typing
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: Environment variable naming the optional secrets directory (read before any other source).
SECRETS_DIR_ENV: Final = "SUV_DEALS_SECRETS_DIR"
#: Default private bind of the optional Prometheus endpoint (``METRICS_ENABLED=true``).
DEFAULT_METRICS_BIND: Final = "127.0.0.1:9464"
#: Hard v1.1 ceilings (spec 37.5); the database CHECK ``seller_inquiry_controls_caps_ck`` agrees.
MAX_SELLER_INQUIRIES_PER_24H: Final = 2
MAX_SELLER_INQUIRIES_PER_15D: Final = 5


def secrets_dir_from_env(environ: Mapping[str, str] | None = None) -> Path | None:
    """The configured secrets directory when it exists and is a directory, else ``None``."""
    env = os.environ if environ is None else environ
    raw = (env.get(SECRETS_DIR_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def parse_bind(value: str) -> tuple[str, int]:
    """``host:port`` (IPv4, ``[IPv6]`` or ``localhost``) -> ``(host, port)``; ``ValueError`` otherwise.

    Port 0 (an ephemeral port) is accepted for tests. Host names other than ``localhost`` are
    refused so a typo can never bind a public interface through DNS.
    """
    text = value.strip()
    host, sep, port_text = text.rpartition(":")
    if not sep or not host or not port_text.isdigit() or len(port_text) > 5:
        raise ValueError("bind must be host:port")
    port = int(port_text)
    if port > 65535:
        raise ValueError("bind port must be 0-65535")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("bind host must be an IP address or localhost") from None
    return host, port


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Optional Docker secrets directory (``SUV_DEALS_SECRETS_DIR``), lowest precedence."""
        secrets_dir = secrets_dir_from_env()
        if secrets_dir is None:
            return init_settings, env_settings, dotenv_settings, file_secret_settings
        secrets = SecretsSettingsSource(settings_cls, secrets_dir=secrets_dir)
        return init_settings, env_settings, dotenv_settings, secrets

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_base_url: str = "http://127.0.0.1:8000"
    display_timezone: str = "Europe/Skopje"
    log_level: str = "INFO"
    #: Installation root holding ``config/`` and ``supabase/migrations`` (non-editable installs).
    suv_deals_home: Path | None = None
    #: Informational: the secrets directory in use (read from the process environment only).
    suv_deals_secrets_dir: Path | None = None
    config_dir: Path = REPO_ROOT / "config"
    migrations_dir: Path = REPO_ROOT / "supabase" / "migrations"
    build_id: str = "dev"

    database_url: SecretStr | None = None
    database_pool_min: int = 1
    database_pool_max: int = 5
    #: Seconds a request waits for a pooled connection before failing with 503
    #: (``DependencyUnavailable``) instead of hanging ~30 s while the database is down.
    database_pool_timeout_s: float = Field(default=5.0, gt=0, le=120)
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
    # Dashboard API CORS allow-list (comma-separated origins). Empty = origin of APP_BASE_URL only.
    api_allowed_origins: str = ""
    # Extra Host header names accepted by the API (comma-separated, e.g. a reverse proxy's
    # internal name or a probe's service DNS name), added to the APP_BASE_URL/MCP_PUBLIC_URL hosts
    # and loopback. Wildcards are refused.
    api_allowed_hosts: str = ""
    # Optional Prometheus endpoint on a separate private bind (never on the public API port).
    metrics_enabled: bool = False
    metrics_bind: str = DEFAULT_METRICS_BIND

    # Review claim lease of the dashboard and MCP ``reviews_claim`` (60 s to 1 h; default 5 min).
    review_claim_duration_seconds: int = Field(default=300, ge=60, le=3600)

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
    # Ceilings, not targets: the owner may reduce (or set 0), never raise them; a misconfigured
    # value fails at start-up.
    seller_inquiry_max_per_24h: int = Field(default=2, ge=0, le=MAX_SELLER_INQUIRIES_PER_24H)
    seller_inquiry_max_per_rolling_15d: int = Field(default=5, ge=0, le=MAX_SELLER_INQUIRIES_PER_15D)
    seller_email_provider: Literal["", "outlook_local", "gmail_api", "microsoft_graph"] = ""
    seller_email_account_id: str | None = None
    seller_email_from: str | None = None
    seller_email_reply_to: str | None = None
    seller_email_oauth_secret_reference: str | None = None
    # Owner-controlled activation canary (spec 37.10; docs/seller_email_activation.md section 8).
    # OFF by default: `suv-deals canary send` refuses unless this switch AND every inquiry
    # activation switch are on and the owner passes --i-confirm-owner-controlled-address.
    seller_email_canary_send_enabled: bool = False
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

    @field_validator("suv_deals_home", "suv_deals_secrets_dir", mode="before")
    @classmethod
    def _optional_path(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("*", mode="before")
    @classmethod
    def _blank_optional_text_is_unset(cls, value: object, info: ValidationInfo) -> object:
        """A blank or whitespace-only value of an optional text/secret setting means "unset".

        ``.env.example`` ships ``NAME=`` placeholders and the runbook says ``cp .env.example .env``;
        without this the placeholders loaded as ``''`` and broke consumers that treat ``None`` as
        unset (the dispatcher's egress proxy, the Slack identity checks, the reply-to check).
        """
        if info.field_name in _OPTIONAL_TEXT_FIELDS:
            text = value.get_secret_value() if isinstance(value, SecretStr) else value
            if isinstance(text, str) and not text.strip():
                return None
        return value

    @field_validator("config_dir", "migrations_dir", "snapshot_local_dir", mode="before")
    @classmethod
    def _required_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a directory path; omit the variable to use the default")
        return value

    @field_validator("metrics_bind")
    @classmethod
    def _metrics_bind(cls, value: str) -> str:
        parse_bind(value)
        return value.strip()

    @field_validator("api_allowed_hosts")
    @classmethod
    def _allowed_hosts(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("wildcard hosts are not allowed")
        return value

    @model_validator(mode="after")
    def _home_paths(self) -> Settings:
        """``SUV_DEALS_HOME`` relocates the default config, migrations and snapshot paths unless
        each one is configured explicitly."""
        if self.suv_deals_home is not None:
            home = self.suv_deals_home.expanduser()
            explicit = self.model_fields_set
            if "config_dir" not in explicit:
                self.config_dir = home / "config"
            if "migrations_dir" not in explicit:
                self.migrations_dir = home / "supabase" / "migrations"
            if "snapshot_local_dir" not in explicit:
                self.snapshot_local_dir = home / "var" / "snapshots"
        # Informational only: the directory actually in use. Like `settings_customise_sources` it
        # is read from the PROCESS environment only, so a value in `.env` (which cannot enable the
        # secrets source) is never reported as if secret files were loaded.
        self.suv_deals_secrets_dir = secrets_dir_from_env()
        return self

    @property
    def metrics_address(self) -> tuple[str, int]:
        """``(host, port)`` of ``METRICS_BIND``."""
        return parse_bind(self.metrics_bind)

    def extra_allowed_hosts(self) -> list[str]:
        """``API_ALLOWED_HOSTS`` as a list of lower-case host names (empty entries dropped)."""
        return [h.strip().lower() for h in self.api_allowed_hosts.split(",") if h.strip()]

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


def _optional_text_fields() -> frozenset[str]:
    """``str | None`` / ``SecretStr | None`` settings: blank values of these mean "unset"."""
    names: set[str] = set()
    for name, info in Settings.model_fields.items():
        if typing.get_origin(info.annotation) not in (typing.Union, types.UnionType):
            continue
        args = set(typing.get_args(info.annotation))
        if type(None) in args and args & {str, SecretStr}:
            names.add(name)
    return frozenset(names)


_OPTIONAL_TEXT_FIELDS: Final[frozenset[str]] = _optional_text_fields()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
