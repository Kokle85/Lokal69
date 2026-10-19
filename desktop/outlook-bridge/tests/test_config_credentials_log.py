"""Configuration, the narrow worker credential and content-free logging."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from bridge_support import MAILBOX_ID, OWNER, START, TOKEN, TOKEN_2, make_config
from outlook_bridge.config import default_data_dir, ensure_private_dir, load_config, parse_config
from outlook_bridge.credentials import (
    CredentialManager,
    CredentialState,
    InMemoryCredentialStore,
    WorkerCredential,
    credential_fingerprint,
    validate_worker_token,
)
from outlook_bridge.errors import ConfigError, CredentialMissing, CredentialUnusable, ForbiddenCredentialKind
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.log import REDACTED, SafeJsonFormatter, event, get_logger, safe_text

# ------------------------------------------------------------------------------------------- config


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "api_base_url": "https://api.example.invalid/",
        "mailbox_binding_id": str(MAILBOX_ID),
        "worker_id": "desktop-1",
        "account_smtp_address": "Owner.Name@Example.INVALID",
    }
    raw.update(overrides)
    return raw


def test_valid_config_defaults_and_normalisation() -> None:
    config = parse_config(_raw())
    assert config.api_base_url == "https://api.example.invalid"
    assert config.account_smtp_address == "Owner.Name@example.invalid"  # local part kept, domain lower
    assert [f.role for f in config.folders] == ["inbox", "junk"]
    assert config.reconcile_interval_seconds == 120  # the proposed two-minute default
    assert config.overlap == timedelta(minutes=30)
    assert config.credential_target() == f"SUVDeals.OutlookBridge/{MAILBOX_ID}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_base_url": "http://api.example.invalid"},  # plain http
        {"api_base_url": "https://user:pw@api.example.invalid"},  # embedded credentials
        {"api_base_url": "https://api.example.invalid/?token=x"},
        {"api_base_url": "ftp://api.example.invalid"},
        {"account_smtp_address": "not an address"},
        {"account_smtp_address": "a..b@example.invalid"},
        {"worker_id": "bad id with spaces"},
        {"reconcile_interval_seconds": 5},
        {"folders": [{"role": "junk"}]},  # no inbox
        {"folders": [{"role": "inbox"}, {"role": "inbox"}]},
        {"folders": [{"role": "inbox"}, {"role": "rule_target"}]},  # rule target needs a path
        {"folders": [{"role": "inbox"}, {"role": "rule_target", "path": "Inbox/../Other"}]},
        {"folders": [{"role": "inbox"}, {"role": "rule_target", "path": "Inbox//Cars"}]},
        {"folders": [{"role": "inbox", "path": "Inbox"}]},
        {"unknown_key": 1},
        {"max_catchup_days": 1, "unmatched_retry_hours": 48},
        {"supabase_service_role_key": "x"},  # no secrets in the configuration at all
    ],
)
def test_invalid_configs_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        parse_config(_raw(**overrides))


def test_loopback_http_needs_explicit_development_flag() -> None:
    with pytest.raises(ConfigError):
        parse_config(_raw(api_base_url="http://127.0.0.1:8000"))
    config = parse_config(_raw(api_base_url="http://127.0.0.1:8000", allow_insecure_loopback=True))
    assert config.api_base_url == "http://127.0.0.1:8000"
    with pytest.raises(ConfigError):
        parse_config(_raw(api_base_url="http://api.example.invalid", allow_insecure_loopback=True))


def test_config_errors_never_echo_file_contents(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_base_url = "https://x.example.invalid"\nsecret_value = "hunter2-do-not-echo"\n')
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "hunter2" not in str(info.value)
    path.write_text("this is = = not toml hunter2")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "hunter2" not in str(info.value)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_load_config_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'api_base_url = "https://api.example.invalid"\nmailbox_binding_id = "{MAILBOX_ID}"\n'
        f'worker_id = "desktop-1"\naccount_smtp_address = "{OWNER}"\n'
        '[[folders]]\nrole = "inbox"\n[[folders]]\nrole = "rule_target"\npath = "Inbox/Cars"\n'
    )
    config = load_config(path)
    assert [f.segments() for f in config.folders] == [(), ("Inbox", "Cars")]


def test_default_data_dir_per_platform(tmp_path: Path) -> None:
    windows = default_data_dir({"LOCALAPPDATA": r"C:\Users\v\AppData\Local"}, platform="win32")
    assert windows.parts[-2:] == ("SUVDeals", "OutlookBridge")
    with pytest.raises(ConfigError):
        default_data_dir({}, platform="win32")
    posix = default_data_dir({"XDG_STATE_HOME": str(tmp_path)}, platform="linux")
    assert posix == tmp_path / "suv-deals-outlook-bridge"
    private = ensure_private_dir(tmp_path / "private")
    if os.name == "posix":
        assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_example_config_file_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    config = load_config(example)
    assert config.reconcile_interval_seconds == 120
    assert {f.role for f in config.folders} >= {"inbox"}


# -------------------------------------------------------------------------------------- credentials


def _jwt(payload: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


@pytest.mark.parametrize(
    "token",
    [
        "postgresql://suv:pw@db.example.invalid:5432/postgres",
        "postgres://suv:pw@db.example.invalid/postgres",
        "sb_secret_0123456789abcdefghijklmnop",
        "sb_publishable_0123456789abcdefghijkl",
        "sbp_0123456789abcdefghijklmnopqrstuvwxyz",
        "service_role_0123456789abcdefghijklmn",
        _jwt({"role": "service_role", "iss": "supabase"}),
        _jwt({"role": "anon", "iss": "supabase"}),
        _jwt({"role": "authenticated", "iss": "https://project.supabase.co/auth/v1"}),
        _jwt({"sub": "x", "iss": "https://project.supabase.co/auth/v1"}),
        "short",
        "contains whitespace 0123456789abcdefghij",
        "x" * 5000,
    ],
)
def test_database_and_supabase_credentials_are_refused(token: str) -> None:
    with pytest.raises(ForbiddenCredentialKind):
        validate_worker_token(token)
    with pytest.raises(ForbiddenCredentialKind):
        InMemoryCredentialStore().save(WorkerCredential(token=token))


def test_narrow_worker_credential_is_accepted_and_never_shown() -> None:
    validate_worker_token(TOKEN)
    validate_worker_token(_jwt({"role": "mail_worker", "iss": "https://api.example.invalid", "mbx": "1"}))
    credential = WorkerCredential(token=TOKEN)
    assert TOKEN not in repr(credential)
    assert credential.fingerprint == credential_fingerprint(TOKEN)
    assert len(credential.fingerprint) == 16


def test_credential_states_missing_active_expired_rejected() -> None:
    store = LocalStore.in_memory(MAILBOX_ID)
    secrets = InMemoryCredentialStore()
    manager = CredentialManager(secrets, store)
    assert manager.state(START) == CredentialState.MISSING
    with pytest.raises(CredentialMissing):
        manager.require(START)
    manager.replace(WorkerCredential(token=TOKEN, expires_at=START + timedelta(hours=1)))
    assert manager.state(START) == CredentialState.ACTIVE
    assert manager.token(START) == TOKEN
    assert manager.state(START + timedelta(hours=1)) == CredentialState.EXPIRED
    with pytest.raises(CredentialUnusable):
        manager.require(START + timedelta(hours=2))
    # A rejection names the credential actually used; a replaced credential is not marked.
    assert manager.mark_rejected(credential_fingerprint(TOKEN_2)) is False
    assert manager.state(START) == CredentialState.ACTIVE
    assert manager.mark_rejected(credential_fingerprint(TOKEN)) is True
    assert manager.state(START) == CredentialState.REJECTED
    # Only a *different* credential re-enables transmission.
    manager.replace(WorkerCredential(token=TOKEN))
    assert manager.state(START) == CredentialState.REJECTED
    manager.replace(WorkerCredential(token=TOKEN_2))
    assert manager.state(START) == CredentialState.ACTIVE
    # The rejection memory lives in the local store and survives a restart of the manager.
    assert CredentialManager(secrets, store).state(START) == CredentialState.ACTIVE
    manager.delete()
    assert manager.state(START) == CredentialState.MISSING
    assert manager.mark_rejected() is False
    store.close()


# ------------------------------------------------------------------------------------------ logging


def _capture(logger_name: str) -> tuple[logging.Logger, io.StringIO]:
    logger = get_logger(logger_name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_events_carry_only_scalar_token_like_fields() -> None:
    logger, stream = _capture("test.events")
    event(
        logger,
        "reply_queued",
        inquiry_id=MAILBOX_ID,
        count=3,
        ok=True,
        subject="AW: Anfrage zu Synthetic SUV with private words",
        sender="seller@dealer.example.invalid",
        when=START,
        code="UPLOAD_QUEUED",
    )
    line = json.loads(stream.getvalue())
    fields = line["fields"]
    assert fields["inquiry_id"] == str(MAILBOX_ID)
    assert fields["count"] == 3
    assert fields["subject"] == REDACTED
    assert fields["sender"] == REDACTED
    assert fields["code"] == "UPLOAD_QUEUED"
    assert fields["when"].startswith("2026-10-06T18:00:00")
    assert "seller@" not in stream.getvalue()


def test_free_text_masks_tokens_and_addresses() -> None:
    masked = safe_text(f"Authorization: Bearer {TOKEN} failed for owner@example.invalid")
    assert TOKEN not in masked
    assert "owner@example.invalid" not in masked
    jwt = _jwt({"role": "mail_worker"})
    masked = safe_text(f"password=\"p w\" secret='s t' token={TOKEN} bearer {TOKEN_2} raw {jwt}")
    for leaked in ("p w", "s t", TOKEN, TOKEN_2, jwt):
        assert leaked not in masked
    logger, stream = _capture("test.exc")
    try:
        raise RuntimeError(f"token={TOKEN} for seller@dealer.example.invalid")
    except RuntimeError:
        logger.exception("upload failed")
    output = stream.getvalue()
    assert TOKEN not in output
    assert "seller@dealer" not in output
    assert json.loads(output)["error_type"] == "RuntimeError"


def test_invalid_event_names_are_replaced() -> None:
    logger, stream = _capture("test.names")
    event(logger, "subject: Private matter", value=1)
    assert json.loads(stream.getvalue())["event"] == "invalid_event_name"


def test_make_config_helper_matches_harness_defaults(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert config.store_path() == tmp_path / "bridge-state.sqlite3"
