"""Command line: check, run, status, reconcile-once, credential and --dry-run (Linux fakes)."""

from __future__ import annotations

import importlib.abc
import io
import json
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from bridge_support import MAILBOX_ID, OWNER, TOKEN, FakeRegistry, Harness, classic_registry
from outlook_bridge import __main__ as entry
from outlook_bridge.cli import (
    EXIT_CONFIG,
    EXIT_CREDENTIAL,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_UNSUPPORTED,
    CliDeps,
    main,
)
from outlook_bridge.config import BridgeConfig
from outlook_bridge.credentials import CredentialStore, InMemoryCredentialStore, WorkerCredential
from outlook_bridge.errors import StaError
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.outlook_adapter import MailboxAdapter
from outlook_bridge.sta_runtime import SessionContext
from outlook_bridge.testing import INTERACTIVE_SESSION

SYSTEM = SessionContext(
    platform="win32",
    user_sid="S-1-5-18",
    session_id=0,
    window_station="Service-0x0-3e7$",
    window_station_visible=False,
)


def _write_config(path: Path, data_dir: Path, **extra: str) -> Path:
    lines = [
        'api_base_url = "https://api.example.invalid"',
        f'mailbox_binding_id = "{MAILBOX_ID}"',
        'worker_id = "desktop-cli-1"',
        f'account_smtp_address = "{OWNER}"',
        f'data_dir = "{data_dir.as_posix()}"',
        "tick_seconds = 0.2",
        *(f"{key} = {value}" for key, value in extra.items()),
        '[[folders]]\nrole = "inbox"',
        '[[folders]]\nrole = "rule_target"\npath = "Inbox/Cars"',
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


class Env:
    def __init__(self, harness: Harness, tmp_path: Path) -> None:
        self.h = harness
        self.config_path = _write_config(tmp_path / "config.toml", tmp_path / "data")
        self.secrets = InMemoryCredentialStore(WorkerCredential(token=TOKEN))
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.factory_calls = 0
        self.stop = threading.Event()

    def mailbox_factory(
        self, config: BridgeConfig, session: SessionContext, clock: Any
    ) -> tuple[MailboxAdapter, Callable[[], None]]:
        self.factory_calls += 1
        return self.h.session, lambda: None

    def deps(self, **overrides: Any) -> CliDeps:
        values: dict[str, Any] = {
            "platform": "win32",
            "session_probe": lambda: INTERACTIVE_SESSION,
            "registry": classic_registry(),
            "processes": lambda: frozenset({"outlook.exe"}),
            "credential_store": lambda config: self.secrets,
            "mailbox_factory": self.mailbox_factory,
            "transport": self.h.backend.transport(),
            "clock": self.h.clock,
            "out": self.out,
            "err": self.err,
            "read_secret": lambda prompt: TOKEN,
            "configure_logs": False,
            "stop_event": self.stop,
        }
        values.update(overrides)
        return CliDeps(**values)

    def run(self, args: Sequence[str], **overrides: Any) -> int:
        self.out.seek(0)
        self.out.truncate()
        return main(["--config", str(self.config_path), *args], self.deps(**overrides))

    def json(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.out.getvalue())
        return data


@pytest.fixture
def env(harness: Harness, tmp_path: Path) -> Env:
    return Env(harness, tmp_path)


# -------------------------------------------------------------------------------------------- check


def test_check_ready_on_classic_outlook_in_an_interactive_session(env: Env) -> None:
    assert env.run(["check"]) == EXIT_OK
    report = env.json()
    assert report["ready"] is True
    assert report["compatibility"]["flavour"] == "classic"
    assert report["session"] == {"interactive_user": True, "refusal_reasons": []}
    assert report["credential"] == {"state": "active"}
    assert report["reply_semantics"]["available"] is True
    assert report["settings_modified"] is False
    assert TOKEN not in env.out.getvalue()


def test_check_reports_new_outlook_as_unsupported(env: Env) -> None:
    assert env.run(["check"], registry=classic_registry(toggle=1)) == EXIT_UNSUPPORTED
    report = env.json()
    assert report["ready"] is False
    assert report["compatibility"]["flavour"] == "new"
    assert "NEW_OUTLOOK_ENABLED" in report["compatibility"]["problems"]


def test_check_on_linux_and_in_a_service_session(env: Env) -> None:
    assert env.run(["check"], platform="linux", session_probe=lambda: SessionContext(platform="linux")) == (
        EXIT_UNSUPPORTED
    )
    assert env.json()["compatibility"]["problems"] == ["NOT_WINDOWS"]
    assert env.run(["check"], session_probe=lambda: SYSTEM) == EXIT_UNSUPPORTED
    assert "SYSTEM_ACCOUNT" in env.json()["session"]["refusal_reasons"]


def test_check_reports_invalid_config_and_missing_credential(env: Env, tmp_path: Path) -> None:
    env.secrets.delete()
    assert env.run(["check"]) == EXIT_CREDENTIAL
    assert env.json()["credential"] == {"state": "missing"}
    env.config_path.write_text("api_base_url = 'ftp://x'\n")
    assert env.run(["check"]) == EXIT_CONFIG
    assert env.json()["config"]["valid"] is False


def test_check_without_outlook_com_registration(env: Env) -> None:
    assert env.run(["check"], registry=FakeRegistry(), processes=frozenset) == EXIT_UNSUPPORTED
    assert env.json()["compatibility"]["flavour"] == "not_installed"


# --------------------------------------------------------------------------------------- credential


def test_credential_set_status_delete_never_print_the_secret(env: Env) -> None:
    env.secrets.delete()
    assert env.run(["credential", "set", "--expires-at", "2026-12-31T00:00:00+00:00"]) == EXIT_OK
    stored = env.json()
    assert stored["stored"] is True and len(stored["fingerprint"]) == 16
    assert env.secrets.load() is not None
    assert env.run(["credential", "status"]) == EXIT_OK
    assert env.json()["state"] == "active"
    assert TOKEN not in env.out.getvalue()
    assert env.run(["credential", "delete"]) == EXIT_OK
    assert env.secrets.load() is None


def test_credential_set_refuses_database_keys_and_bad_expiry(env: Env) -> None:
    code = env.run(
        ["credential", "set"], read_secret=lambda prompt: "postgresql://suv:pw@db.example.invalid/x"
    )
    assert code == EXIT_CREDENTIAL
    assert "postgresql://" not in env.err.getvalue()
    assert env.run(["credential", "set", "--expires-at", "tomorrow"]) == EXIT_CONFIG
    assert env.run(["credential", "set", "--expires-at", "2026-12-31T00:00:00"]) == EXIT_CONFIG  # naive


# ------------------------------------------------------------------------------------------- status


def test_status_without_and_with_local_state(env: Env, tmp_path: Path) -> None:
    assert env.run(["status"]) == EXIT_OK
    assert env.json() == {"status": "no local state yet", "monitoring_24h_claimed": False}
    store = LocalStore.open(tmp_path / "data" / "bridge-state.sqlite3", MAILBOX_ID)
    store.close()
    assert env.run(["status"]) == EXIT_OK
    snapshot = env.json()
    assert snapshot["monitoring_24h_claimed"] is False
    assert "not 24-hour monitoring" in snapshot["coverage_statement"]
    assert snapshot["server"]["credential_state"] == "active"
    assert snapshot["downstream"] == {"mcp": "not_observed", "slack_signal": "not_observed"}


# ---------------------------------------------------------------------------------------------- run


def test_reconcile_once_uploads_correlated_replies(env: Env) -> None:
    inquiry = uuid4()
    env.h.bind(inquiry)
    env.h.deliver_reply(inquiry, fire_event=False)
    assert env.run(["reconcile-once"]) == EXIT_OK
    output = env.json()
    assert output["dry_run"] is False and output["connected"] is True
    assert output["uploads"] == {"acked": 1}
    assert output["health"]["monitoring_24h_claimed"] is False
    assert len(env.h.reply_posts()) == 1
    assert (env.config_path.parent / "data" / "bridge-state.sqlite3").exists()


def test_reconcile_once_dry_run_transmits_no_mail_data(env: Env) -> None:
    inquiry = uuid4()
    env.h.bind(inquiry)
    env.h.deliver_reply(inquiry, fire_event=False)
    assert env.run(["reconcile-once", "--dry-run"]) == EXIT_OK
    output = env.json()
    assert output["dry_run"] is True and output["scan"]["uploaded"] == 1
    assert {method for method, _ in env.h.backend.requests} == {"GET"}
    assert env.h.reply_posts() == []
    assert not (env.config_path.parent / "data" / "bridge-state.sqlite3").exists()  # throw-away store


def test_run_loop_ticks_and_stops(env: Env) -> None:
    inquiry = uuid4()
    env.h.bind(inquiry)
    env.h.deliver_reply(inquiry, fire_event=False)
    assert env.run(["run"], max_ticks=2) == EXIT_OK
    assert len(env.h.reply_posts()) == 1


def test_run_refuses_service_context_new_outlook_and_missing_credential(env: Env) -> None:
    assert env.run(["run"], session_probe=lambda: SYSTEM) == EXIT_UNSUPPORTED
    assert "SYSTEM_ACCOUNT" in env.err.getvalue()
    assert env.factory_calls == 0  # Outlook was never touched
    assert env.run(["run"], registry=classic_registry(toggle=1)) == EXIT_UNSUPPORTED
    assert "NEW_OUTLOOK_ENABLED" in env.err.getvalue()
    env.secrets.delete()
    assert env.run(["run"]) == EXIT_CREDENTIAL
    assert env.factory_calls == 0


def test_run_reports_an_sta_start_failure(env: Env) -> None:
    def broken(
        config: BridgeConfig, session: SessionContext, clock: Any
    ) -> tuple[MailboxAdapter, Callable[[], None]]:
        raise StaError("COM initialisation failed")

    assert env.run(["reconcile-once"], mailbox_factory=broken) == EXIT_RUNTIME


def test_missing_config_file_is_a_config_error(env: Env, tmp_path: Path) -> None:
    code = main(["--config", str(tmp_path / "absent.toml"), "status"], env.deps())
    assert code == EXIT_CONFIG


def test_credential_store_factory_receives_the_config(env: Env) -> None:
    seen: list[str] = []

    def factory(config: BridgeConfig) -> CredentialStore:
        seen.append(config.credential_target())
        return env.secrets

    env.run(["credential", "status"], credential_store=factory)
    assert seen == [f"SUVDeals.OutlookBridge/{MAILBOX_ID}"]


# -------------------------------------------------------------------------------------- entry point


class _MissingSharedSemantics(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None) -> None:
        if fullname == "outlook_bridge.cli":
            raise ModuleNotFoundError("No module named 'suv_deals'", name="suv_deals")


def test_entry_point_refuses_clearly_without_the_shared_semantics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "outlook_bridge.cli")
    monkeypatch.setattr(sys, "meta_path", [_MissingSharedSemantics(), *sys.meta_path])
    assert entry._main() == entry.EXIT_RUNTIME
    assert "shared reply semantics" in capsys.readouterr().err
