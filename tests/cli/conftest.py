"""Fixtures for the operator CLI tests (SYNTHETIC data; no network, no real credentials).

- Every test starts from an environment without any application setting (``clean_env``), so a
  developer's shell or ``.env`` can never leak into a test; commands run with ``--no-env-file``.
- The CLI runs in-process through ``click.testing.CliRunner``. Database tests (marker ``db``) use
  the session's migrated database with ``DATABASE_SET_ROLE=suv_backend``; arrangement uses the
  superuser ``Seed`` (read-only reuse of tests/integration/db/helpers.py). Each test creates its
  own active workspace and deactivates it afterwards, and always names it with ``--workspace``.
- Commands that configure JSON logging attach a handler to the root logger; ``restore_logging``
  removes it again so later tests are unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from click.testing import CliRunner, Result
from tests.integration.db.helpers import Seed

from suv_deals.cli import cli
from suv_deals.settings import Settings

#: Variables that are not Settings fields but are read by the CLI or libpq.
_EXTRA_VARS = (
    "MAINTENANCE_DATABASE_URL",
    "MIGRATE_URL",
    "LOCAL_ADMIN_URL",
    "LOCAL_DATABASE_URL",
    "RESTORE_ADMIN_URL",
    "PGPASSWORD",
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGUSER",
    "PGDATABASE",
    "PGSERVICE",
    "PGSERVICEFILE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    for name in _EXTRA_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)


@dataclass
class Cli:
    runner: CliRunner

    def __call__(self, *args: str, env: Mapping[str, str] | None = None) -> Result:
        return self.runner.invoke(cli, ["--no-env-file", *args], env=dict(env or {}), catch_exceptions=False)


@pytest.fixture
def run_cli() -> Cli:
    return Cli(CliRunner())


def all_output(result: Result) -> str:
    """stdout + stderr as a user would see them."""
    return result.output


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
def workspace(seed: Seed) -> Iterator[UUID]:
    """A fresh active SYNTHETIC workspace, deactivated after the test."""
    workspace_id = seed.workspace("CLI test workspace")
    try:
        yield workspace_id
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (workspace_id,))


@pytest.fixture
def db_env(db_url: str) -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "DATABASE_URL": db_url,
        "DATABASE_SET_ROLE": "suv_backend",
        "DATABASE_POOL_MAX": "3",
        "BUILD_ID": "cli-test",
    }
