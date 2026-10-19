"""``suv-deals config validate|apply`` (spec 26, 27).

``validate`` is offline: settings by presence, the confirmed business baseline (a primary maximum
other than EUR 3,000 fails), the YAML business configuration, source registry, cost profile,
taxonomy, tax rule files, the activation-route combination and the seller-inquiry switches.

``apply`` records the YAML business configuration as a new ``app.config_revisions`` row for one
workspace (audited, optimistic: it refuses when the stored revision changed meanwhile). It never
runs without ``--yes``.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    CliContext,
    echo,
    emit_json,
    exit_with,
    fail,
    load_settings,
    pass_cli,
    require_yes,
    run_async,
    workspace_option,
)


@click.group("config")
def config_group() -> None:
    """Validate and record the business configuration."""


def _config_dir(settings_dir: Path, override: Path | None) -> Path:
    return override if override is not None else settings_dir


@config_group.command("validate")
@click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Configuration directory (default: CONFIG_DIR / ./config).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def validate(cli: CliContext, config_dir: Path | None, as_json: bool) -> None:
    """Validate settings and configuration files offline (exit 1 on any error)."""
    from suv_deals.cli_commands import _checks

    settings = load_settings(cli)
    directory = _config_dir(settings.config_dir, config_dir)
    findings = [
        *_checks.baseline_findings(settings),
        *_checks.config_file_findings(directory, settings),
        *_checks.switch_findings(settings),
        *_checks.notification_findings(settings),
        *_checks.seller_inquiry_findings(settings),
        *_checks.production_findings(settings),
    ]
    findings = _checks.sort_findings(findings)
    if as_json:
        emit_json([f.as_dict() for f in findings])
    else:
        _checks.render(findings, title=f"suv-deals config validate (APP_ENV={settings.app_env})")
    if _checks.has_errors(findings):
        exit_with(EXIT_PROBLEMS)


@config_group.command("apply")
@workspace_option
@click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Configuration directory (default: CONFIG_DIR / ./config).",
)
@click.option("--reason", required=True, help="Why the configuration changes (stored in the audit trail).")
@click.option(
    "--dry-run", is_flag=True, help="Show whether a new revision would be recorded; change nothing."
)
@click.option("--yes", is_flag=True, help="Confirm recording the revision.")
@pass_cli
def apply(
    cli: CliContext,
    *,
    workspace: UUID | None,
    config_dir: Path | None,
    reason: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Record config/defaults.yaml + config/profiles/*.yaml as a configuration revision."""
    settings = load_settings(cli)
    directory = _config_dir(settings.config_dir, config_dir)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.profiles import load_business_config
        from suv_deals.errors import NotFound
        from suv_deals.persistence import config_repo
        from suv_deals.persistence.transactions import unit_of_work

        config = load_business_config(directory)
        new_hash = config_repo.config_hash(config)
        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "config-apply", admin=True)
            async with unit_of_work(db, actor) as conn:
                try:
                    record, before = await config_repo.current_config(conn, actor)
                except NotFound:
                    record, before = None, None
            echo(f"Workspace        : {workspace_id}")
            echo(f"Stored revision  : {'none' if record is None else record.revision}")
            unchanged = record is not None and record.config_hash == new_hash
            echo(f"YAML config hash : {new_hash[:16]}... ({'unchanged' if unchanged else 'differs'})")
            if unchanged:
                echo("Nothing to record.")
                return 0
            if dry_run:
                echo("Dry run: a new revision would be recorded; nothing changed.")
                return 0
            require_yes(yes, "config apply")
            async with unit_of_work(db, actor) as conn:
                result = await config_repo.record_config_revision(conn, actor, config, reason, before)
            echo(f"Recorded revision {result.revision.revision} ({len(result.profiles)} profile(s)).")
            for key, enabled in sorted(result.enabled_changes.items()):
                echo(f"  profile {key}: {'enabled' if enabled else 'disabled'}")
            return 0

    if not directory.is_dir():
        fail("the configuration directory does not exist")
    run_async(body)
