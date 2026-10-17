"""v1.1 runtime settings: inquiry caps, secrets directory, install home, pool timeout, API hosts,
private metrics (spec 37; F1 foundation). No network, no real secrets (synthetic values only)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from suv_deals.settings import (
    DEFAULT_METRICS_BIND,
    MAX_SELLER_INQUIRIES_PER_15D,
    MAX_SELLER_INQUIRIES_PER_24H,
    REPO_ROOT,
    SECRETS_DIR_ENV,
    Settings,
    parse_bind,
    secrets_dir_from_env,
)


def _settings(**values: object) -> Settings:
    """Settings without the developer's ``.env`` file (process environment still applies)."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        SECRETS_DIR_ENV,
        "SUV_DEALS_HOME",
        "CONFIG_DIR",
        "MIGRATIONS_DIR",
        "SNAPSHOT_LOCAL_DIR",
        "DATABASE_URL",
        "DATABASE_POOL_TIMEOUT_S",
        "API_ALLOWED_HOSTS",
        "METRICS_ENABLED",
        "METRICS_BIND",
        "SELLER_INQUIRY_MAX_PER_24H",
        "SELLER_INQUIRY_MAX_PER_ROLLING_15D",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- inquiry caps


def test_inquiry_caps_default_to_the_owner_ceilings() -> None:
    settings = _settings()
    assert (MAX_SELLER_INQUIRIES_PER_24H, MAX_SELLER_INQUIRIES_PER_15D) == (2, 5)
    assert settings.seller_inquiry_max_per_24h == 2
    assert settings.seller_inquiry_max_per_rolling_15d == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seller_inquiry_max_per_24h", 3),
        ("seller_inquiry_max_per_24h", -1),
        ("seller_inquiry_max_per_rolling_15d", 6),
        ("seller_inquiry_max_per_rolling_15d", -1),
    ],
)
def test_inquiry_caps_can_only_be_lowered(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        _settings(**{field: value})


def test_inquiry_caps_may_be_zero_and_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SELLER_INQUIRY_MAX_PER_24H", "0")
    monkeypatch.setenv("SELLER_INQUIRY_MAX_PER_ROLLING_15D", "1")
    settings = _settings()
    assert (settings.seller_inquiry_max_per_24h, settings.seller_inquiry_max_per_rolling_15d) == (0, 1)
    monkeypatch.setenv("SELLER_INQUIRY_MAX_PER_24H", "10")
    with pytest.raises(ValidationError):
        _settings()


def test_safety_defaults_stay_off() -> None:
    settings = _settings()
    assert settings.source_network_enabled is False
    assert settings.allow_external_notifications is False
    assert settings.seller_inquiry_mode == "disabled_until_sender_ready"
    assert settings.metrics_enabled is False


# --------------------------------------------------------------------------- secrets directory


def test_secrets_dir_is_used_only_when_it_exists(tmp_path: Path) -> None:
    assert secrets_dir_from_env({}) is None
    assert secrets_dir_from_env({SECRETS_DIR_ENV: "   "}) is None
    assert secrets_dir_from_env({SECRETS_DIR_ENV: str(tmp_path / "missing")}) is None
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")
    assert secrets_dir_from_env({SECRETS_DIR_ENV: str(a_file)}) is None
    assert secrets_dir_from_env({SECRETS_DIR_ENV: str(tmp_path)}) == tmp_path


def test_secret_files_fill_settings_below_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "database_url").write_text(
        "postgresql://synthetic:synthetic@db.example.invalid/suv\n", encoding="utf-8"
    )
    (secrets / "slack_bot_token").write_text("xoxb-synthetic-not-a-token\n", encoding="utf-8")
    monkeypatch.setenv(SECRETS_DIR_ENV, str(secrets))
    settings = _settings()
    assert settings.database_url is not None
    assert (
        settings.database_url.get_secret_value() == "postgresql://synthetic:synthetic@db.example.invalid/suv"
    )
    assert settings.suv_deals_secrets_dir == secrets
    # The process environment wins over a secret file.
    monkeypatch.setenv("DATABASE_URL", "postgresql://env:env@env.example.invalid/suv")
    assert _settings().database_url.get_secret_value().startswith("postgresql://env:")  # type: ignore[union-attr]
    # describe() reports presence only, never the value.
    report = _settings().describe()
    assert report["database_url"] == "set"
    assert "example.invalid" not in repr(report)


def test_secrets_dir_in_the_env_file_is_neither_used_nor_reported(tmp_path: Path) -> None:
    """Only the process environment selects the secrets source; a ``.env`` entry must not make
    ``describe()``/doctor report a secrets directory whose files were never read."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "database_url").write_text("postgresql://synthetic@db.example.invalid/suv", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{SECRETS_DIR_ENV}={secrets}\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.database_url is None
    assert settings.suv_deals_secrets_dir is None
    assert settings.describe()["suv_deals_secrets_dir"] == "missing"


def test_missing_secrets_dir_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRETS_DIR_ENV, str(tmp_path / "not-there"))
    settings = _settings()
    assert settings.database_url is None
    assert settings.suv_deals_secrets_dir is None


# --------------------------------------------------------------------------- install home


def test_repo_root_is_the_default_home() -> None:
    settings = _settings()
    assert settings.suv_deals_home is None
    assert settings.config_dir == REPO_ROOT / "config"
    assert settings.migrations_dir == REPO_ROOT / "supabase" / "migrations"
    assert settings.migrations_dir.is_dir()


def test_home_relocates_default_paths_but_not_explicit_ones(tmp_path: Path) -> None:
    settings = _settings(suv_deals_home=tmp_path)
    assert settings.config_dir == tmp_path / "config"
    assert settings.migrations_dir == tmp_path / "supabase" / "migrations"
    assert settings.snapshot_local_dir == tmp_path / "var" / "snapshots"
    explicit = _settings(suv_deals_home=tmp_path, config_dir=tmp_path / "elsewhere")
    assert explicit.config_dir == tmp_path / "elsewhere"
    assert explicit.migrations_dir == tmp_path / "supabase" / "migrations"


def test_home_and_dirs_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUV_DEALS_HOME", str(tmp_path))
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path / "migrations"))
    settings = _settings()
    assert settings.config_dir == tmp_path / "config"
    assert settings.migrations_dir == tmp_path / "migrations"
    monkeypatch.setenv("SUV_DEALS_HOME", "")
    assert _settings().suv_deals_home is None


@pytest.mark.parametrize("field", ["config_dir", "migrations_dir", "snapshot_local_dir"])
def test_empty_directory_settings_are_refused(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _settings(**{field: "  "})


# --------------------------------------------------------------------------- database pool


@pytest.mark.parametrize("value", [0, -1, 121])
def test_pool_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValidationError, match="database_pool_timeout_s"):
        _settings(database_pool_timeout_s=value)


def test_pool_timeout_defaults_to_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings().database_pool_timeout_s == 5
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT_S", "1.5")
    assert _settings().database_pool_timeout_s == 1.5


# --------------------------------------------------------------------------- API hosts and metrics


def test_extra_allowed_hosts_are_normalised() -> None:
    settings = _settings(api_allowed_hosts=" API.internal , ,probe-svc.local ")
    assert settings.extra_allowed_hosts() == ["api.internal", "probe-svc.local"]
    assert _settings().extra_allowed_hosts() == []


@pytest.mark.parametrize("value", ["*", "*.example.invalid", "api.internal,*"])
def test_wildcard_hosts_are_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        _settings(api_allowed_hosts=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:9464", ("127.0.0.1", 9464)),
        (" 0.0.0.0:9100 ", ("0.0.0.0", 9100)),
        ("[::1]:9464", ("::1", 9464)),
        ("localhost:0", ("localhost", 0)),
    ],
)
def test_parse_bind_accepts_ip_literals_and_localhost(value: str, expected: tuple[str, int]) -> None:
    assert parse_bind(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "9464",
        "127.0.0.1",
        ":9464",
        "127.0.0.1:65536",
        "127.0.0.1:-1",
        "metrics.example.invalid:9464",
        "::1:x",
    ],
)
def test_parse_bind_refuses_everything_else(value: str) -> None:
    with pytest.raises(ValueError, match="bind"):
        parse_bind(value)


def test_metrics_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    assert settings.metrics_bind == DEFAULT_METRICS_BIND == "127.0.0.1:9464"
    assert settings.metrics_address == ("127.0.0.1", 9464)
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("METRICS_BIND", "127.0.0.1:9500")
    enabled = _settings()
    assert enabled.metrics_enabled is True and enabled.metrics_address == ("127.0.0.1", 9500)
    with pytest.raises(ValidationError, match="metrics_bind"):
        _settings(metrics_bind="public.example.invalid:9464")


def test_describe_is_presence_only_for_new_fields() -> None:
    report = _settings(api_allowed_hosts="api.internal").describe()
    for name in (
        "suv_deals_home",
        "suv_deals_secrets_dir",
        "migrations_dir",
        "database_pool_timeout_s",
        "api_allowed_hosts",
        "metrics_enabled",
        "metrics_bind",
    ):
        assert report[name] in ("set", "missing"), name
    assert report["api_allowed_hosts"] == "set" and report["suv_deals_home"] == "missing"
    assert "api.internal" not in repr(report)
