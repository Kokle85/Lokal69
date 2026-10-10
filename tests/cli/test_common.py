"""Shared CLI plumbing: settings files, connection-target parsing, error mapping (no network)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from suv_deals.cli import cli
from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_UNAVAILABLE,
    database_target,
    describe_error,
    exit_code_for,
    password_free,
    unexpected_error,
)
from suv_deals.errors import DependencyUnavailable, SourcePaused, ValidationFailed


def test_env_file_is_read_and_baseline_enforced(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("PRIMARY_MAX_PRICE_EUR=4000\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["--env-file", str(env_file), "config", "validate"])
    assert result.exit_code == 1
    assert "PRIMARY_MAX_PRICE_EUR differs" in result.output
    ignored = CliRunner().invoke(cli, ["--no-env-file", "--env-file", str(env_file), "config", "validate"])
    assert ignored.exit_code == 0


def test_missing_env_file_is_a_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--env-file", str(tmp_path / "absent.env"), "config", "validate"])
    assert result.exit_code == 2
    assert "env file not found" in result.output


@pytest.mark.parametrize(
    ("url", "host", "port", "dbname", "user"),
    [
        (
            "postgresql://alice:FAKE-pw-1@db.example.invalid:6543/prod",
            "db.example.invalid",
            "6543",
            "prod",
            "alice",
        ),
        (
            "host=127.0.0.1 port=5433 dbname=dev user=bob password=FAKE-pw-2",
            "127.0.0.1",
            "5433",
            "dev",
            "bob",
        ),
        ("postgresql:///localdb?host=/var/run/postgresql", "/var/run/postgresql", "5432", "localdb", "-"),
    ],
)
def test_database_target_never_returns_the_password(
    url: str, host: str, port: str, dbname: str, user: str
) -> None:
    target = database_target(url)
    assert (target.host, target.port, target.dbname, target.user) == (host, port, dbname, user)
    rendered = "\n".join(target.lines())
    assert "FAKE-pw" not in rendered
    assert target.is_local == (host != "db.example.invalid")


def test_error_mapping_and_redaction() -> None:
    assert exit_code_for(DependencyUnavailable("database unavailable")) == EXIT_UNAVAILABLE
    assert exit_code_for(SourcePaused("gated")) == EXIT_REFUSED
    assert exit_code_for(ValidationFailed("bad")) == EXIT_PROBLEMS
    detail = describe_error(ValidationFailed("invalid", details={"problems": ["a", "b"]}))
    assert detail.startswith("VALIDATION_ERROR: invalid") and "  - b" in detail
    db_error = psycopg.errors.UniqueViolation("Key (email)=(someone@example.invalid) already exists")
    text = unexpected_error(db_error)
    assert text == "database error (UniqueViolation, SQLSTATE 23505)"
    other = unexpected_error(RuntimeError("token=FAKE-secret-value-123456 failed"))
    assert other.startswith("unexpected RuntimeError")
    assert "FAKE-secret-value-123456" not in other


# --------------------------------------------------------------------------------------------
# Local-target detection must follow libpq, not the URL text (review regression tests)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # A query parameter overrides the host in the authority part (libpq).
        "postgresql://suv:FAKE-pw@127.0.0.1:5432/suv_dev?host=db.example.invalid",
        # hostaddr is used INSTEAD of resolving the host name.
        "postgresql://suv:FAKE-pw@127.0.0.1:5432/suv_dev?hostaddr=10.20.30.40",
        # libpq tries every listed host in turn.
        "postgresql://suv:FAKE-pw@127.0.0.1:5432,db.example.invalid:5432/suv_dev",
        "host=localhost,db.example.invalid dbname=suv_dev",
        # A service definition may supply any parameter.
        "postgresql://suv@localhost/suv_dev?service=production",
    ],
)
def test_database_target_is_not_local_when_any_part_can_leave_the_machine(url: str) -> None:
    target = database_target(url)
    assert not target.is_local
    assert "FAKE-pw" not in "\n".join(target.lines())


@pytest.mark.parametrize(
    ("env", "local"),
    [
        ({"PGHOST": "db.example.invalid"}, False),  # used when the string names no host
        ({"PGHOSTADDR": "10.20.30.40"}, False),
        ({"PGSERVICE": "production"}, False),
        ({"PGHOST": "/var/run/postgresql"}, True),
        ({"PGHOSTADDR": "127.0.0.1"}, True),
    ],
)
def test_database_target_honours_libpq_environment_defaults(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], local: bool
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert database_target("dbname=suv_dev user=suv").is_local is local


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://suv:FAKE-pw@127.0.0.1:5432/suv_dev",
        "postgresql://suv:FAKE-pw@[::1]:5433/suv_dev?sslmode=disable",
        "postgresql:///suv_dev?host=/var/run/postgresql",
        "host=127.0.0.1 port=5433 dbname=dev",
    ],
)
def test_database_target_accepts_loopback_and_sockets(url: str) -> None:
    assert database_target(url).is_local


@pytest.mark.parametrize(
    ("url", "password"),
    [
        ("postgresql://alice:FAKE%40pw%3A1@db.example.invalid:6543/prod?sslmode=require", "FAKE@pw:1"),
        ("host=127.0.0.1 dbname=dev user=bob password='FAKE pw 2'", "FAKE pw 2"),
        ("postgresql://carol@db.example.invalid/prod?password=FAKE-pw-3", "FAKE-pw-3"),
        ("postgresql://dave@db.example.invalid/prod", None),
    ],
)
def test_password_free_moves_the_password_out_of_the_connection_string(
    url: str, password: str | None
) -> None:
    stripped, found = password_free(url)
    assert found == password
    assert "FAKE" not in stripped
    info = psycopg.conninfo.conninfo_to_dict(stripped)
    assert "password" not in info
    original = psycopg.conninfo.conninfo_to_dict(url)
    original.pop("password", None)
    assert info == original  # nothing else changed
