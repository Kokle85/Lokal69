"""OPS-02: the empty placeholders of ``.env.example`` mean "unset" (``None``), never ``''``.

``cp .env.example .env`` is the documented first step (runbook section 4). Before the fix every
blank optional string loaded as ``''``: the dispatcher built ``SafeHttpClient(proxy='')`` and
crashed at start (``Unknown scheme for proxy URL``), the MCP endpoint fell back to the disabled
app, and ``SlackConfig.from_settings`` refused ``app_id=''`` so every seller-reply signal was
``SLACK_BLOCKED``. Synthetic values only; nothing is sent.
"""

from __future__ import annotations

import types
import typing
from pathlib import Path

import pytest
from pydantic import SecretStr

from suv_deals.domain.inquiries import SenderStatus
from suv_deals.integrations import slack
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.settings import REPO_ROOT, SECRETS_DIR_ENV, Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _template_names() -> list[str]:
    names = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.append(stripped.split("=", 1)[0])
    return names


def _optional_text_fields() -> list[str]:
    fields = []
    for name, info in Settings.model_fields.items():
        args = typing.get_args(info.annotation)
        is_union = typing.get_origin(info.annotation) in (typing.Union, types.UnionType)
        if is_union and type(None) in args and ({str, SecretStr} & set(args)):
            fields.append(name)
    return fields


@pytest.fixture
def template_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """``Settings`` loaded from the shipped template alone (the process environment is cleared)."""
    for name in [*_template_names(), SECRETS_DIR_ENV]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    return Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]


def test_blank_template_placeholders_load_as_none(template_settings: Settings) -> None:
    blank = {
        name.lower()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (name := line.split("=", 1)[0]) and line.endswith("=") and not line.startswith("#")
    }
    optional = set(_optional_text_fields())
    checked = sorted(blank & optional)
    assert {"callback_egress_proxy_url", "slack_app_id", "seller_email_reply_to"} <= set(checked)
    for name in checked:
        assert getattr(template_settings, name) is None, name


def test_whitespace_only_optional_values_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRETS_DIR_ENV, raising=False)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, slack_bot_id="   ", slack_bot_token=SecretStr(" "), database_set_role=""
    )
    assert settings.slack_bot_id is None
    assert settings.database_set_role is None
    # SecretStr given as text is normalised the same way.
    assert Settings(_env_file=None, slack_signing_secret="  ").slack_signing_secret is None  # type: ignore[call-arg]
    # Real values are untouched (no stripping of a set value).
    kept = Settings(_env_file=None, slack_app_id="A0SYNTH01")  # type: ignore[call-arg]
    assert kept.slack_app_id == "A0SYNTH01"


def test_dispatcher_egress_client_builds_from_the_template(template_settings: Settings) -> None:
    client = SafeHttpClient(proxy=template_settings.callback_egress_proxy_url)
    assert client is not None


def test_slack_config_builds_with_the_dispatcher_argument_shape(template_settings: Settings) -> None:
    configured = template_settings.model_copy(
        update={
            "slack_bot_token": SecretStr("xoxb-" + "synthetic-test-token"),
            "slack_signing_secret": SecretStr("synthetic-signing-secret"),
            "slack_channel_id": "C0SYNTHETIC",
        }
    )
    config = slack.SlackConfig.from_settings(
        configured,
        destination_approval_ref="approval-ref-synthetic",
        team_id=configured.slack_team_id or "T0SYNTHETIC",
        app_id=configured.slack_app_id,
        bot_id=configured.slack_bot_id,
        bot_user_id=configured.slack_bot_user_id,
    )
    assert config.app_id is None and config.bot_id is None and config.bot_user_id is None


def test_sender_status_has_no_reply_to_problem_from_the_template(template_settings: Settings) -> None:
    status = SenderStatus.from_settings(
        template_settings,
        binding_id=None,
        binding_version=None,
        display_name=None,
        alias_verified=False,
        verified_at=None,
        health_ok=False,
    )
    assert "REPLY_TO_INVALID" not in status.identity_problems()


def test_env_example_path_exists() -> None:
    assert isinstance(ENV_EXAMPLE, Path) and ENV_EXAMPLE.is_file()
