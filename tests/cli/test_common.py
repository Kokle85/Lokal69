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
