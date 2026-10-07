"""``suv-deals sources list|inspect|sync`` (spec 5, 27, 32).

``list``/``inspect`` read the YAML registry (``config/sources/*.yaml``) and its activation gates
offline; ``--from-db`` shows the runtime registry of a workspace instead (runtime technical
status, pauses, recent runs). ``sync`` upserts the YAML registry into ``app.sources`` through
`sources_repo.sync_sources_from_yaml` (a source is never enabled while any gate reports a problem,
runtime safety states win over the YAML, missing sources are disabled, never deleted).
``--dry-run`` runs the sync inside a transaction that is rolled back.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    CliContext,
    echo,
    emit_json,
    fail,
    load_settings,
    pass_cli,
    require_yes,
    run_async,
    table,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.domain.sources import SourceConfig
    from suv_deals.persistence.sources_repo import SourceSyncReport
    from suv_deals.settings import Settings

_CONFIG_DIR_OPTION = click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Configuration directory (default: CONFIG_DIR / ./config).",
)


@click.group("sources")
def sources_group() -> None:
    """Source registry, activation gates and registry sync."""


def _registry(settings: Settings, config_dir: Path | None) -> Any:
    from suv_deals.adapters.registry import load_registry

    return load_registry(config_dir or settings.config_dir)


@sources_group.command("list")
@_CONFIG_DIR_OPTION
@click.option("--from-db", is_flag=True, help="Show the runtime registry of a workspace instead of the YAML.")
@workspace_option
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def list_sources(
    cli: CliContext, config_dir: Path | None, from_db: bool, workspace: UUID | None, as_json: bool
) -> None:
    """List sources with separate terms/technical status and whether every gate passes."""
    from suv_deals.errors import AppError

    settings = load_settings(cli)
    if from_db:

        async def body() -> int:
            from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
            from suv_deals.persistence import sources_repo
            from suv_deals.persistence.transactions import unit_of_work

            async with open_database(settings, application_name="suv-deals-cli") as db:
                workspace_id = await resolve_workspace(db, workspace)
                actor = operator_actor(workspace_id, "sources-list")
                async with unit_of_work(db, actor) as conn:
                    view = await sources_repo.list_sources(conn, actor)
            rows = [
                {
                    "source_key": item.source_key,
                    "country": item.country,
                    "role": item.role,
                    "state": item.state.value,
                    "enabled": item.enabled,
                    "paused": item.paused,
                    "gate_problems": len(item.activation_problems),
                }
                for item in view.items
            ]
            if as_json:
                emit_json([item.model_dump(mode="json") for item in view.items])
            else:
                echo(table(rows, list(rows[0]) if rows else ["source_key"]))
            return 0

        run_async(body)
        return
    try:
        registry = _registry(settings, config_dir)
    except AppError as exc:
        fail(exc.message)
    gates = registry.gates()
    if as_json:
        emit_json([g.as_dict() for g in gates])
        return
    rows = [
        {
            "source_key": g.source_key,
            "country": g.country,
            "role": g.role,
            "mode": g.mode,
            "enabled": g.enabled,
            "technical": g.technical_status.value,
            "terms": g.terms_status.value,
            "decision": g.terms_decision.value,
            "active": g.active,
            "gate_problems": len(g.problems),
        }
        for g in gates
    ]
    echo(table(rows, list(rows[0]) if rows else ["source_key"]))
    active = sum(1 for g in gates if g.active)
    echo(f"{len(gates)} source(s); {active} active. A source is active only when every gate passes.")


@sources_group.command("inspect")
@click.argument("source_key")
@_CONFIG_DIR_OPTION
@click.option("--from-db", is_flag=True, help="Show the runtime record of a workspace instead of the YAML.")
@workspace_option
@pass_cli
def inspect(
    cli: CliContext, source_key: str, config_dir: Path | None, from_db: bool, workspace: UUID | None
) -> None:
    """Show one source's configuration, gate problems and (with --from-db) runtime state."""
    from suv_deals.errors import AppError

    settings = load_settings(cli)
    if from_db:

        async def body() -> int:
            from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
            from suv_deals.persistence import sources_repo
            from suv_deals.persistence.transactions import unit_of_work

            async with open_database(settings, application_name="suv-deals-cli") as db:
                workspace_id = await resolve_workspace(db, workspace)
                actor = operator_actor(workspace_id, "sources-inspect")
                async with unit_of_work(db, actor) as conn:
                    record = await sources_repo.get_source_by_key(conn, actor, source_key)
                    view = await sources_repo.get_source(conn, actor, record.id)
            emit_json(view.model_dump(mode="json"))
            return 0

        run_async(body)
        return
    try:
        registry = _registry(settings, config_dir)
        config = registry.config(source_key)
    except AppError as exc:
        fail(exc.message)
    from suv_deals.adapters.registry import gate_status

    gate = gate_status(config)
    echo(f"source_key        : {config.source_key}")
    echo(f"display_name      : {config.display_name}")
    echo(f"country / role    : {config.country} / {config.role}")
    echo(f"mode / adapter    : {config.mode.value} / {config.adapter} ({config.adapter_version})")
    echo(f"enabled           : {str(config.enabled).lower()}")
    echo(f"technical_status  : {config.technical_status.value}")
    echo(f"terms             : status {config.terms_status.value}, decision {config.terms_decision.value}")
    echo(f"terms_url         : {config.terms_url or '-'}")
    echo(f"robots_policy     : {config.robots_policy}; denial policy {config.technical_denial_policy}")
    echo(f"detail_mode       : {config.detail_mode}")
    echo(f"allowed_hosts     : {', '.join(config.allowed_hosts) or '-'}")
    budget = config.rate_budget
    echo(
        f"rate_budget       : {budget.max_search_pages_per_run} search page(s)/run, "
        f"{budget.max_detail_jobs_per_run} detail job(s)/run, {budget.daily_request_budget} requests/day, "
        f"min delay {budget.min_delay_seconds}s"
    )
    echo(f"active            : {str(gate.active).lower()}")
    if gate.problems:
        echo("gate problems     :")
        for problem in gate.problems:
            echo(f"  - {problem}")


def _fixture_source_configs() -> list[SourceConfig]:
    """SYNTHETIC fixture sources (``tests/adapters/fixtures/*/source.yaml``), exactly as stored."""
    import yaml

    from suv_deals.domain.sources import SourceConfig
    from suv_deals.workers.runtime import discover_fixture_dirs

    configs: list[SourceConfig] = []
    for directory in discover_fixture_dirs():
        path = directory / "source.yaml"
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            configs.append(SourceConfig.model_validate(data))
    return configs


def _print_report(report: SourceSyncReport) -> None:
    for label, keys in (
        ("created", report.created),
        ("updated", report.updated),
        ("unchanged", report.unchanged),
        ("terms changed", report.terms_changed),
        ("disabled (missing from YAML)", report.disabled_missing),
    ):
        echo(f"  {label:<30}: {', '.join(keys) if keys else '-'}")
    for key, problems in sorted(report.not_enabled.items()):
        echo(f"  not enabled {key}: {'; '.join(problems)}")
    for key, status in sorted(report.preserved_runtime_status.items()):
        echo(f"  kept runtime status {key}: {status.value}")


class _DryRunRollback(Exception):
    def __init__(self, report: SourceSyncReport) -> None:
        super().__init__("dry run")
        self.report = report


@sources_group.command("sync")
@_CONFIG_DIR_OPTION
@workspace_option
@click.option(
    "--fixture-sources",
    is_flag=True,
    help="Also register the SYNTHETIC fixture sources (development/test only; they stay gated).",
)
@click.option("--dry-run", is_flag=True, help="Run the sync in a transaction that is rolled back.")
@click.option("--yes", is_flag=True, help="Confirm writing the registry.")
@pass_cli
def sync(
    cli: CliContext,
    *,
    config_dir: Path | None,
    workspace: UUID | None,
    fixture_sources: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Upsert config/sources/*.yaml into the workspace registry (audited; never enables a gated source)."""
    from suv_deals.errors import AppError

    settings = load_settings(cli)
    if fixture_sources and settings.app_env not in ("development", "test"):
        from suv_deals.cli_commands._common import refuse

        refuse("--fixture-sources is only allowed when APP_ENV is development or test")
    try:
        configs = list(_registry(settings, config_dir).configs)
        if fixture_sources:
            configs.extend(_fixture_source_configs())
    except AppError as exc:
        fail(exc.message)
    except ValueError as exc:
        fail(f"invalid fixture source configuration: {type(exc).__name__}")
    if not dry_run:
        require_yes(yes, "sources sync")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import sources_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sources-sync", admin=True)
            echo(f"Workspace: {workspace_id} ({len(configs)} source configuration(s))")
            try:
                async with unit_of_work(db, actor) as conn:
                    report = await sources_repo.sync_sources_from_yaml(conn, actor, configs)
                    if dry_run:
                        raise _DryRunRollback(report)
            except _DryRunRollback as rollback:
                echo("Dry run (rolled back; nothing changed):")
                _print_report(rollback.report)
                return 0
        echo("Synced:")
        _print_report(report)
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# Owner recovery actions (audited; neither one ever enables a source)
# --------------------------------------------------------------------------------------------

TECHNICAL_STATUSES = (
    "untested",
    "fixture_tested",
    "live_smoke_passed",
    "degraded",
    "parser_unhealthy",
    "access_blocked",
)


@sources_group.command("set-technical-status")
@click.argument("source_key")
@click.argument("status", type=click.Choice(TECHNICAL_STATUSES))
@workspace_option
@click.option("--reason", required=True, help="Evidence for the change (3-1000 characters; audited).")
@click.option("--yes", is_flag=True, help="Confirm the change.")
@pass_cli
def set_technical_status(
    cli: CliContext, *, source_key: str, status: str, workspace: UUID | None, reason: str, yes: bool
) -> None:
    """Record a technical status (e.g. clear access_blocked after permitted access is restored).

    Leaving access_blocked/parser_unhealthy is an explicit owner action; a blocking status disables
    the source; nothing here re-enables it (a later `sources sync` does, when every gate passes).
    """
    settings = load_settings(cli)
    require_yes(yes, "sources set-technical-status")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.enums import TechnicalStatus
        from suv_deals.persistence import sources_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sources-technical-status", admin=True)
            async with unit_of_work(db, actor) as conn:
                current = await sources_repo.get_source_by_key(conn, actor, source_key)
                record = await sources_repo.set_technical_status(
                    conn,
                    actor,
                    current.id,
                    TechnicalStatus(status),
                    reason=reason,
                    expected_version=current.version,
                )
        echo(
            f"{source_key}: technical status {current.technical_status.value} -> "
            f"{record.technical_status.value}; enabled={str(record.enabled).lower()} (never re-enabled here)"
        )
        return 0

    run_async(body)


@sources_group.command("resume")
@click.argument("source_key")
@workspace_option
@click.option("--reason", required=True, help="Why the pause can end (3-2000 characters; audited).")
@click.option("--yes", is_flag=True, help="Confirm clearing the pause.")
@pass_cli
def resume(cli: CliContext, *, source_key: str, workspace: UUID | None, reason: str, yes: bool) -> None:
    """Clear an operator pause (owner action). Never enables a disabled or gated source."""
    settings = load_settings(cli)
    require_yes(yes, "sources resume")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import sources_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sources-resume", admin=True)
            async with unit_of_work(db, actor) as conn:
                current = await sources_repo.get_source_by_key(conn, actor, source_key)
                record = await sources_repo.resume_source(
                    conn, actor, current.id, current.version, reason=reason
                )
        echo(
            f"{source_key}: paused={str(record.paused).lower()}, enabled={str(record.enabled).lower()}"
            + (" (was not paused)" if not current.paused else "")
        )
        return 0

    run_async(body)
