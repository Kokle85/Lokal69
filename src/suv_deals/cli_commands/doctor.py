"""``suv-deals doctor``: presence-only readiness report (spec 26, 27, 30).

What it checks (and never does):

- configuration per process (api, worker, scheduler, dispatcher, reconciler, crawler): required
  vs recommended vs optional settings, reported as ``set``/``missing`` only (`Settings.describe`);
- the confirmed business baseline and the configuration files (shared with ``config validate``);
- the database: reachability, server version, the migration ledger, the backend role
  (``ops.backend_role_problems()`` when the login role may call it, ``SET ROLE``), required schema
  markers (the same list ``/readyz`` uses) and active workspaces. Read-only;
- the crawler, ONLY with ``--crawler``: ``GET /health`` and the read-only contract inspection
  (`Crawl4AIClient.inspect_contract`: unauthenticated probe, ``/config/dump`` dry runs). It never
  crawls a page and never restarts or reconfigures the crawler;
- source activation gates from the YAML registry, offline OAuth/URL metadata checks, the
  notification/event route and the seller-inquiry switches.

It never prints a secret, URL or connection string, and it never creates accounts, keys or data.
Exit code 1 when any check reports an error.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import click

from suv_deals.cli_commands._checks import PROCESSES, Finding
from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CliContext,
    emit_json,
    exit_with,
    load_settings,
    pass_cli,
)

if TYPE_CHECKING:
    import httpx

    from suv_deals.settings import Settings

_MIGRATION_RE = re.compile(r"^([0-9]{14})_([a-z0-9_]+)\.sql$")
TESTED_MAJORS = frozenset({16, 17})


def _make_crawler_http() -> httpx.AsyncClient | None:
    """Injection point for tests (an ``httpx.MockTransport`` client); ``None`` = the real client."""
    return None


def migration_files(root: Path | None = None) -> list[tuple[str, str]]:
    """``(version, name)`` of every migration file in this checkout, in apply order."""
    from suv_deals.settings import REPO_ROOT

    directory = root or (REPO_ROOT / "supabase" / "migrations")
    found: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.sql")) if directory.is_dir() else []:
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


async def database_findings(settings: Settings) -> list[Finding]:
    """Read-only database checks (see module docstring)."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from suv_deals.errors import AppError

    area = "database"
    if settings.database_url is None or not settings.database_url.get_secret_value():
        return [Finding(area, "connection", "skipped", "DATABASE_URL is missing")]
    findings: list[Finding] = []
    try:
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
            prepare_threshold=None,
            connect_timeout=5,
            application_name="suv-deals-doctor",
        )
    except (psycopg.Error, OSError):
        return [
            Finding(area, "connection", "error", "database not reachable (check DATABASE_URL and network)")
        ]
    async with conn:

        async def scalar(query: Any, params: Any = None) -> Any:
            cur = await conn.execute(query, params)
            row = await cur.fetchone()
            return None if row is None else next(iter(row.values()))

        findings.append(Finding(area, "connection", "ok", "reachable"))
        version_num = int(await scalar("select current_setting('server_version_num')::int"))
        major, minor = divmod(version_num, 10000)
        version_text = f"PostgreSQL {major}.{minor}"
        if major < 15:
            findings.append(Finding(area, "server_version", "error", f"{version_text}: 15 or newer required"))
        elif major not in TESTED_MAJORS:
            findings.append(
                Finding(area, "server_version", "warn", f"{version_text}: tested on 16.15 and 17.11")
            )
        else:
            findings.append(Finding(area, "server_version", "ok", f"{version_text} (tested major version)"))

        # Migration ledger (supabase_migrations.schema_migrations; see docs/schema.md section 9).
        # A dedicated login (e.g. a LOGIN member of suv_backend) may lack USAGE on that schema:
        # to_regclass() itself then raises, which must not abort the remaining checks.
        files = migration_files()
        try:
            has_ledger = await scalar(
                "select to_regclass('supabase_migrations.schema_migrations') is not null"
            )
            applied: set[str] | None = None
            if has_ledger:
                cur = await conn.execute("select version from supabase_migrations.schema_migrations")
                applied = {str(r["version"]) for r in await cur.fetchall()}
        except psycopg.errors.InsufficientPrivilege:
            findings.append(Finding(area, "migration_ledger", "skipped", "no privilege to read the ledger"))
        else:
            if applied is not None:
                known = {v for v, _ in files}
                pending = [f"{v}_{n}" for v, n in files if v not in applied]
                unknown = sorted(applied - known)
                if pending:
                    findings.append(
                        Finding(
                            area, "migration_ledger", "warn", f"{len(pending)} pending: {', '.join(pending)}"
                        )
                    )
                else:
                    findings.append(
                        Finding(area, "migration_ledger", "ok", f"all {len(files)} files recorded")
                    )
                if unknown:
                    findings.append(
                        Finding(
                            area,
                            "migration_versions",
                            "warn",
                            f"{len(unknown)} ledger version(s) not in this checkout (connector-applied "
                            "versions are recorded under their apply time; see docs/schema.md "
                            "section 9)",
                        )
                    )
            else:
                findings.append(
                    Finding(
                        area,
                        "migration_ledger",
                        "info",
                        "no supabase_migrations ledger (test harness or manual apply); "
                        "the schema markers below decide",
                    )
                )

        # Backend role (ADR 0001): owner-only function; skipped when the login role may not call it.
        try:
            problems = await scalar("select ops.backend_role_problems('suv_backend')")
        except psycopg.errors.InsufficientPrivilege:
            findings.append(
                Finding(
                    area,
                    "backend_role",
                    "skipped",
                    "ops.backend_role_problems() is owner-only for this login",
                )
            )
        except (psycopg.errors.UndefinedFunction, psycopg.errors.InvalidSchemaName):
            findings.append(
                Finding(area, "backend_role", "error", "schema not migrated (ops functions missing)")
            )
        else:
            if problems:
                findings.append(
                    Finding(area, "backend_role", "error", "suv_backend is unsafe: " + ", ".join(problems))
                )
            else:
                findings.append(Finding(area, "backend_role", "ok", "suv_backend has no unsafe attributes"))

        role = settings.database_set_role
        if role:
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
                findings.append(
                    Finding(area, "set_role", "error", "DATABASE_SET_ROLE is not a valid role name")
                )
                return findings
            try:
                await conn.execute(sql.SQL("set role {}").format(sql.Identifier(role)))
            except psycopg.Error:
                findings.append(
                    Finding(area, "set_role", "error", "SET ROLE failed: the login role must be a member")
                )
                return findings
            findings.append(Finding(area, "set_role", "ok", f"SET ROLE {role} works"))
        else:
            findings.append(
                Finding(
                    area, "set_role", "warn", "DATABASE_SET_ROLE is not set; the login role is used directly"
                )
            )

        from suv_deals.persistence.queries.operations import SCHEMA_MARKERS, schema_markers_present

        try:
            present = await schema_markers_present(conn, SCHEMA_MARKERS)
        except AppError:
            findings.append(Finding(area, "schema", "error", "schema markers could not be read"))
            return findings
        missing = sorted({m.migration for m, ok in zip(SCHEMA_MARKERS, present, strict=True) if not ok})
        if missing:
            findings.append(Finding(area, "schema", "error", f"missing migrations: {', '.join(missing)}"))
        else:
            findings.append(Finding(area, "schema", "ok", "every required schema marker is present"))
        v11 = await scalar("select to_regclass('app.seller_inquiries') is not null")
        findings.append(
            Finding(
                area,
                "schema_v11",
                "ok" if v11 else "info",
                "spec v1.1 seller-inquiry tables (migration 20261006001000) "
                + ("present" if v11 else "not applied"),
            )
        )
        try:
            count = int(await scalar("select count(*) from ops.active_workspace_ids()"))
        except psycopg.Error:
            findings.append(Finding(area, "workspaces", "warn", "active workspaces could not be counted"))
        else:
            if count:
                findings.append(Finding(area, "workspaces", "ok", f"{count} active workspace(s)"))
            else:
                findings.append(
                    Finding(
                        area, "workspaces", "warn", "no active workspace (run `suv-deals bootstrap owner`)"
                    )
                )
    return findings


def crawler_topology(base_url: str) -> str:
    host = (urlsplit(base_url.strip()).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return "host-local loopback (a process on this host, e.g. 127.0.0.1:11235)"
    if host == "crawl4ai":
        return "Compose service DNS (crawl4ai:11235 on the private network)"
    return "another host (verify the private network path)"


async def crawler_findings(settings: Settings, http: httpx.AsyncClient | None = None) -> list[Finding]:
    """Read-only crawler health and contract inspection (only with ``doctor --crawler``)."""
    from suv_deals.adapters.crawl4ai_client import Crawl4AIClient

    area = "crawler"
    findings = [Finding(area, "topology", "info", crawler_topology(settings.crawl4ai_base_url))]
    try:
        client = Crawl4AIClient.from_settings(settings, http=http)
    except ValueError:
        return [
            *findings,
            Finding(area, "base_url", "error", "CRAWL4AI_BASE_URL is not a plain http(s) origin"),
        ]
    try:
        report = await client.inspect_contract()
    finally:
        await client.aclose()
    health = report.health
    if health.ok:
        findings.append(Finding(area, "health", "ok", f"healthy, version {health.version or 'unknown'}"))
    elif health.reachable:
        findings.append(
            Finding(area, "health", "error", f"reachable but not healthy ({health.error or 'status'})")
        )
    else:
        findings.append(Finding(area, "health", "error", "not reachable"))
    if report.version_matches is True:
        findings.append(Finding(area, "version", "ok", f"matches the pinned {report.expected_version}"))
    elif report.version_matches is False:
        findings.append(
            Finding(area, "version", "error", f"does not match the pinned {report.expected_version}")
        )
    findings.append(
        Finding(
            area,
            "token",
            "ok" if report.token_configured else "error",
            "CRAWL4AI_API_TOKEN: " + ("set" if report.token_configured else "missing"),
        )
    )
    if report.auth_enforced is True:
        findings.append(Finding(area, "auth", "ok", "unauthenticated requests are refused (401)"))
    elif report.auth_enforced is False:
        findings.append(Finding(area, "auth", "error", "the crawler accepts unauthenticated requests"))
    if report.problems:
        findings.append(Finding(area, "contract", "error", "problems: " + ", ".join(report.problems[:12])))
    else:
        findings.append(Finding(area, "contract", "ok", "read-only contract inspection passed"))
    return findings


def source_findings(settings: Settings, verbose: bool) -> list[Finding]:
    from suv_deals.adapters.registry import load_registry
    from suv_deals.errors import AppError

    try:
        registry = load_registry(settings.config_dir)
    except AppError as exc:
        return [Finding("sources", "registry", "error", exc.message)]
    findings: list[Finding] = []
    for gate in registry.gates():
        if gate.active:
            findings.append(Finding("sources", gate.source_key, "ok", f"active ({gate.mode})"))
            continue
        detail = f"not active: {len(gate.problems)} gate problem(s)"
        if verbose:
            detail += " - " + "; ".join(gate.problems)
        findings.append(Finding("sources", gate.source_key, "info", detail))
    return findings


def collect_offline(settings: Settings, processes: Sequence[str], verbose: bool) -> list[Finding]:
    from suv_deals.cli_commands import _checks

    return [
        *_checks.requirement_findings(settings, processes),
        *_checks.baseline_findings(settings),
        *_checks.config_file_findings(settings.config_dir, settings),
        *source_findings(settings, verbose),
        *_checks.auth_findings(settings),
        *_checks.switch_findings(settings),
        *_checks.notification_findings(settings),
        *_checks.seller_inquiry_findings(settings),
        *_checks.production_findings(settings),
    ]


@click.command("doctor")
@click.option(
    "--process",
    "process_csv",
    default=",".join(PROCESSES),
    show_default=True,
    help="Comma-separated processes whose configuration requirements are checked.",
)
@click.option("--no-db", is_flag=True, help="Skip the database checks.")
@click.option("--crawler", is_flag=True, help="Also run the read-only crawler health/contract inspection.")
@click.option("--verbose", "-v", is_flag=True, help="List every source gate problem.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def doctor(
    cli: CliContext, *, process_csv: str, no_db: bool, crawler: bool, verbose: bool, as_json: bool
) -> None:
    """Report missing configuration and dependency health without printing any value."""
    from suv_deals.cli_commands import _checks
    from suv_deals.cli_commands._common import fail, parse_csv, run_async

    processes = parse_csv(process_csv)
    unknown = [p for p in processes if p not in PROCESSES]
    if unknown or not processes:
        fail(
            f"unknown process(es): {', '.join(unknown) or '-'}; choose from {', '.join(PROCESSES)}",
            EXIT_USAGE,
        )
    settings = load_settings(cli)
    findings = collect_offline(settings, processes, verbose)

    async def online() -> int:
        from suv_deals.cli_commands._common import unexpected_error

        if not no_db:
            try:
                findings.extend(await database_findings(settings))
            except Exception as exc:  # report and continue: doctor never aborts half-way
                findings.append(Finding("database", "check", "error", unexpected_error(exc)))
        if crawler:
            try:
                findings.extend(await crawler_findings(settings, _make_crawler_http()))
            except Exception as exc:
                findings.append(Finding("crawler", "check", "error", unexpected_error(exc)))
        return 0

    run_async(online)
    ordered = _checks.sort_findings(findings)
    if as_json:
        emit_json({"app_env": settings.app_env, "findings": [f.as_dict() for f in ordered]})
    else:
        _checks.render(ordered, title=f"suv-deals doctor (APP_ENV={settings.app_env})")
    if _checks.has_errors(ordered):
        exit_with(EXIT_PROBLEMS)
