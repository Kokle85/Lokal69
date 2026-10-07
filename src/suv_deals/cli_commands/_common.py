"""Shared plumbing for the operator CLI: settings, exit codes, safe output, database and actors.

Exit codes (stable; scripts and the Makefile rely on them):

- ``0`` success / every check passed
- ``1`` problems found (failed checks, invalid configuration, verification mismatches, not found)
- ``2`` usage error (click)
- ``3`` refused for safety (missing ``--yes``, a closed activation gate, network disabled,
  production guard)
- ``4`` a dependency is unavailable (database, crawler)

Nothing here prints a secret: errors pass through `observability.logging.redact`, settings are
reported by presence only, and database targets are shown without the password.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn
from uuid import UUID, uuid4

import click

if TYPE_CHECKING:
    from suv_deals.domain.actor import ActorContext
    from suv_deals.persistence.database import Database
    from suv_deals.settings import Settings

EXIT_OK: Final = 0
EXIT_PROBLEMS: Final = 1
EXIT_USAGE: Final = 2
EXIT_REFUSED: Final = 3
EXIT_UNAVAILABLE: Final = 4

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
#: Environment variable holding the PRIVILEGED maintenance connection (bootstrap only).
MAINTENANCE_URL_ENV: Final = "MAINTENANCE_DATABASE_URL"


@dataclass(slots=True)
class CliContext:
    """Global options (``--env-file`` / ``--no-env-file``)."""

    env_file: Path | None = None
    use_env_file: bool = True

    def resolved_env_file(self) -> Path | None:
        if not self.use_env_file:
            return None
        if self.env_file is not None:
            return self.env_file
        default = Path(".env")
        return default if default.is_file() else None


pass_cli = click.make_pass_decorator(CliContext, ensure=True)


# --------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------


def safe(text: object) -> str:
    """Redact credentials/personal data from any text that may reach the terminal."""
    from suv_deals.observability.logging import redact

    return redact(str(text))


def echo(text: str = "") -> None:
    click.echo(text)


def warn(text: str) -> None:
    click.echo(safe(text), err=True)


def emit_json(value: object) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


class CliAbort(Exception):
    """Stop the command: the root group prints ``error: <message>`` (redacted) and exits."""

    def __init__(self, message: str, code: int = EXIT_PROBLEMS) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def fail(message: str, code: int = EXIT_PROBLEMS) -> NoReturn:
    """Abort with ``error: <message>`` on stderr and exit code ``code`` (also inside async code)."""
    raise CliAbort(message, code)


def exit_with(code: int) -> NoReturn:
    """Leave the command with ``code`` (no message)."""
    raise click.exceptions.Exit(code)


def report_abort(error: CliAbort) -> None:
    click.echo(f"error: {safe(error.message)}", err=True)


def refuse(message: str) -> NoReturn:
    fail(f"refused: {message}", EXIT_REFUSED)


def require_yes(yes: bool, what: str) -> None:
    """Safety confirmation: state-changing commands never run without an explicit ``--yes``."""
    if not yes:
        refuse(f"{what} changes state; re-run with --yes after checking the target above")


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def exit_code_for(error: BaseException) -> int:
    from suv_deals.errors import AppError, ErrorCode

    if not isinstance(error, AppError):
        return EXIT_PROBLEMS
    if error.code == ErrorCode.DEPENDENCY_UNAVAILABLE:
        return EXIT_UNAVAILABLE
    if error.code in (
        ErrorCode.SOURCE_PAUSED,
        ErrorCode.ACCESS_BLOCKED,
        ErrorCode.FORBIDDEN,
        ErrorCode.UNAUTHENTICATED,
    ):
        return EXIT_REFUSED
    return EXIT_PROBLEMS


def describe_error(error: BaseException) -> str:
    from suv_deals.errors import AppError

    if isinstance(error, AppError):
        text = f"{error.code.value}: {error.message}"
        problems = error.details.get("problems") if error.details else None
        if isinstance(problems, list) and problems:
            text += "".join(f"\n  - {p}" for p in problems[:50])
        return text
    return type(error).__name__


def run_async(operation: Callable[[], Awaitable[int | None]]) -> None:
    """Run one async command body; map application errors to exit codes (never tracebacks)."""
    import anyio

    from suv_deals.errors import AppError

    async def main() -> int | None:
        return await operation()

    try:
        result = anyio.run(main)
    except AppError as exc:
        fail(describe_error(exc), exit_code_for(exc))
    except (CliAbort, click.exceptions.Exit):
        raise
    except KeyboardInterrupt:
        fail("interrupted", EXIT_PROBLEMS)
    except Exception as exc:
        if env_flag("SUV_DEALS_DEBUG"):
            raise
        fail(unexpected_error(exc), EXIT_PROBLEMS)
    if result:
        raise click.exceptions.Exit(result)


def unexpected_error(exc: BaseException) -> str:
    """A one-line, redacted description of an unexpected error (``SUV_DEALS_DEBUG=1`` re-raises).

    Database errors are reduced to their SQLSTATE: server messages can quote row values.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if type(exc).__module__.split(".", 1)[0] == "psycopg":
        return f"database error ({type(exc).__name__}, SQLSTATE {sqlstate or 'unknown'})"
    return f"unexpected {type(exc).__name__}: {safe(str(exc))[:300]} (set SUV_DEALS_DEBUG=1 for a traceback)"


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------


def load_settings(cli: CliContext) -> Settings:
    """Settings from the environment (+ ``.env``); invalid values are reported by NAME only."""
    from pydantic import ValidationError

    from suv_deals.settings import Settings

    env_file = cli.resolved_env_file()
    if env_file is not None and not env_file.is_file():
        fail(f"env file not found: {env_file.name}", EXIT_USAGE)
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in err['loc']).upper() or '<settings>'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
        fail("invalid settings:\n  " + "\n  ".join(lines[:50]), EXIT_PROBLEMS)


def database_url(settings: Settings) -> str:
    if settings.database_url is None or not settings.database_url.get_secret_value():
        fail("DATABASE_URL is not configured (server-side secret; set it in the environment)", EXIT_USAGE)
    return settings.database_url.get_secret_value()


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """Where a connection string points, WITHOUT the password."""

    host: str
    port: str
    dbname: str
    user: str
    password_set: bool

    @property
    def is_local(self) -> bool:
        return self.host in LOOPBACK_HOSTS or self.host.startswith("/") or self.host == "local socket"

    def lines(self) -> list[str]:
        return [
            f"  host     : {self.host}",
            f"  port     : {self.port}",
            f"  database : {self.dbname}",
            f"  user     : {self.user}",
            f"  password : {'set (not shown)' if self.password_set else 'not set'}",
        ]


def database_target(url: str) -> DatabaseTarget:
    """Parse a libpq URL/DSN without connecting; the password is never returned."""
    import psycopg

    try:
        info = psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        fail("the database connection string cannot be parsed", EXIT_USAGE)
    host = str(info.get("host") or os.environ.get("PGHOST") or "local socket")
    return DatabaseTarget(
        host=host.split(",", maxsplit=1)[0],
        port=str(info.get("port") or os.environ.get("PGPORT") or "5432"),
        dbname=str(info.get("dbname") or info.get("user") or "-"),
        user=str(info.get("user") or "-"),
        password_set=bool(info.get("password") or os.environ.get("PGPASSWORD")),
    )


# --------------------------------------------------------------------------------------------
# Database and actors
# --------------------------------------------------------------------------------------------


@asynccontextmanager
async def open_database(settings: Settings, *, application_name: str) -> AsyncIterator[Database]:
    """A small pool as ``DATABASE_SET_ROLE`` (normally ``suv_backend``; ADR 0001)."""
    from suv_deals.persistence.database import Database

    db = Database(
        database_url(settings),
        min_size=1,
        max_size=2,
        set_role=settings.database_set_role,
        application_name=application_name,
    )
    await db.open()
    try:
        yield db
    finally:
        await db.close()


def operator_actor(workspace_id: UUID, purpose: str, *, admin: bool = False) -> ActorContext:
    """The operator's actor for one workspace.

    ``admin=False``: the worker system principal (no ``config:admin``). ``admin=True``: the
    operator CLI acting as the owner (``config:admin``) for owner-level maintenance such as
    syncing the source registry or recording a configuration revision; audited as
    ``system``/``operator-cli``. Never ``mail:ingest``.
    """
    from suv_deals.domain.actor import ActorContext
    from suv_deals.domain.enums import Role, Scope

    request_id = f"cli:{purpose[:40]}:{uuid4().hex[:12]}"
    if not admin:
        return ActorContext.system(workspace_id, request_id=request_id)
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=UUID(int=0),
        principal_kind="system",
        role=Role.OWNER,
        scopes=frozenset(Scope) - {Scope.MAIL_INGEST},
        request_id=request_id,
        display_name="operator-cli",
    )


async def active_workspaces(db: Database) -> list[UUID]:
    from suv_deals.workers.runtime import active_workspace_ids

    return await active_workspace_ids(db)


async def resolve_workspaces(db: Database, requested: UUID | None) -> list[UUID]:
    """``--workspace`` (must be active) or every active workspace."""
    workspaces = await active_workspaces(db)
    if requested is None:
        return workspaces
    if requested not in workspaces:
        fail("the workspace is unknown or inactive", EXIT_PROBLEMS)
    return [requested]


async def resolve_workspace(db: Database, requested: UUID | None) -> UUID:
    """``--workspace``, or the only active workspace (several -> the operator must choose)."""
    workspaces = await resolve_workspaces(db, requested)
    if not workspaces:
        fail("no active workspace; create one with `suv-deals bootstrap owner`", EXIT_PROBLEMS)
    if len(workspaces) > 1:
        fail("several active workspaces; choose one with --workspace", EXIT_USAGE)
    return workspaces[0]


def workspace_option(func: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--workspace",
        "workspace",
        type=click.UUID,
        default=None,
        help="Workspace id (default: the only active workspace / every active workspace).",
    )(func)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def table(rows: Iterable[Mapping[str, object]], columns: list[str]) -> str:
    """A plain fixed-width table (values are redacted)."""
    data = [[safe("" if row.get(col) is None else row.get(col)) for col in columns] for row in rows]
    widths = [max([len(col), *(len(r[i]) for r in data)]) for i, col in enumerate(columns)]
    lines = ["  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip()]
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in data)
    return "\n".join(lines)


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()
