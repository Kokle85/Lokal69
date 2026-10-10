"""Every command answers ``--help`` without configuration or a database (spec 27), lazily."""

from __future__ import annotations

import subprocess
import sys

import click
import pytest
from tests.cli.conftest import Cli

from suv_deals.cli import cli

SPEC_27_COMMANDS = [
    ("doctor",),
    ("config", "validate"),
    ("sources", "list"),
    ("sources", "inspect"),
    ("crawl", "once"),
    ("worker",),
    ("scheduler",),
    ("outbox", "inspect"),
    ("reviews", "list"),
    ("evidence", "verify"),
    ("tax-rules", "validate"),
    ("reconcile",),
]


def _paths(group: click.Group, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for name, command in sorted(group.commands.items()):
        path = (*prefix, name)
        found.append(path)
        if isinstance(command, click.Group):
            found.extend(_paths(command, path))
    return found


ALL_COMMANDS = _paths(cli)


def test_spec_27_commands_exist() -> None:
    assert set(SPEC_27_COMMANDS) <= set(ALL_COMMANDS)
    for extra in [("sources", "sync"), ("dispatcher",), ("db", "migrate"), ("api", "serve")]:
        assert extra in ALL_COMMANDS
    for extra in [("credentials", "create-mcp"), ("credentials", "revoke"), ("bootstrap", "owner")]:
        assert extra in ALL_COMMANDS
    v11 = [
        ("mail-worker", "credential", "issue"),
        ("mail-worker", "credential", "revoke"),
        ("mail-worker", "credential", "list"),
        ("sender-binding", "create"),
        ("sender-binding", "verify"),
        ("sender-binding", "status"),
        ("inquiries", "authorize"),
        ("inquiries", "set-mode"),
        ("inquiries", "set-limits"),
        ("inquiries", "status"),
        ("inquiries", "pause"),
        ("inquiries", "resume"),
        ("evaluation", "report"),
    ]
    assert set(v11) <= set(ALL_COMMANDS)
    # No command sends, replies to or approves a seller e-mail (spec 37.1: no approve button). The
    # only "send" is the owner's activation canary to an OWNER-CONTROLLED address (never a seller,
    # gated by every activation switch and --i-confirm-owner-controlled-address; work package C2).
    seller_paths = [path for path in ALL_COMMANDS if path[0] != "canary"]
    words = {part for path in seller_paths for name in path for part in name.split("-")}
    assert not words & {"send", "reply", "approve", "approval"}
    canary_words = {
        part for path in ALL_COMMANDS if path[0] == "canary" for part in "-".join(path).split("-")
    }
    assert not canary_words & {"reply", "approve", "approval"}
    assert ("canary", "send") in ALL_COMMANDS


def test_root_help(run_cli: Cli) -> None:
    result = run_cli("--help")
    assert result.exit_code == 0
    assert "doctor" in result.output and "crawl" in result.output


@pytest.mark.parametrize("path", ALL_COMMANDS, ids=" ".join)
def test_every_command_has_help(run_cli: Cli, path: tuple[str, ...]) -> None:
    result = run_cli(*path, "--help")
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_version(run_cli: Cli) -> None:
    result = run_cli("--version")
    assert result.exit_code == 0
    assert "suv-deals" in result.output


def test_help_imports_no_heavy_modules() -> None:
    """``suv-deals --help`` must work without a database and stay fast (lazy imports)."""
    code = (
        "import sys\n"
        "from suv_deals.cli import cli\n"
        "heavy = [m for m in ('psycopg', 'fastapi', 'mcp', 'uvicorn', 'httpx', 'suv_deals.persistence',"
        " 'suv_deals.workers', 'suv_deals.api') if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert completed.stdout.strip() == ""


def test_secrets_are_never_cli_options() -> None:
    """No option may carry a secret (process lists and shell history expose arguments)."""
    secret_words = ("password", "secret", "token", "api-key", "apikey", "database-url", "dsn")
    for path in ALL_COMMANDS:
        command: click.Command = cli
        for name in path:
            assert isinstance(command, click.Group)
            command = command.commands[name]
        for param in command.params:
            for opt in getattr(param, "opts", []):
                assert not any(word in opt.lower() for word in secret_words), (path, opt)


def test_crawl_once_accepts_no_url(run_cli: Cli) -> None:
    result = run_cli("crawl", "once", "--source", "autoscout24_de", "--url", "https://example.invalid/")
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_crawl_once_max_pages_is_bounded(run_cli: Cli) -> None:
    assert run_cli("crawl", "once", "--source", "x_source", "--max-pages", "0").exit_code == 2
    assert run_cli("crawl", "once", "--source", "x_source", "--max-pages", "21").exit_code == 2
