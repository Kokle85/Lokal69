"""Operator CLI: ``suv-deals`` (entry point ``suv_deals.cli:main``; spec section 27).

Commands (``suv-deals <command> --help`` for details)::

    doctor                         presence-only readiness report (no values printed)
    config validate | apply        offline validation / record a configuration revision (--yes)
    sources list | inspect | sync  registry, activation gates, YAML -> DB sync (--dry-run, --yes)
    sources set-technical-status | resume
                                   audited owner recovery actions (never enable a source; --yes)
    crawl once                     one bounded discovery through every gate (no URLs accepted)
    worker | scheduler | dispatcher | reconcile [--workspace]
                                   the runtime processes (one image, different commands)
    outbox inspect                 outbox counts and events needing attention
    reviews list                   review cases by state
    evidence verify                recompute revision and snapshot hashes (read-only)
    tax-rules validate PATH        tax rule-set validation (never approves)
    db migrate | target            print the target, then apply migrations (--yes) / only print it
                                   (--local-only: exit 3 unless it stays on this machine)
    api serve                      uvicorn for /api, /healthz, /readyz and /mcp (127.0.0.1 default)
    credentials create-mcp | revoke | list
                                   scoped MCP credentials (token shown once; --yes)
    bootstrap owner                link an existing Supabase Auth user as workspace owner (--yes)
    mail-worker credential issue | revoke | list
                                   the desktop mail worker's mailbox binding and credential
                                   (token shown once; --yes)
    sender-binding create | verify | status
                                   the owner-authorized sending identity (secret by reference only;
                                   verify = outlook_local technical check from the worker's evidence)
    inquiries authorize | status | set-mode | set-limits | pause | resume
                                   seller-inquiry controls and the standing authorization record
                                   (reason + expected version; --yes; no message approval exists)
    evaluation report --days 15    the 15-day quality evaluation from stored evidence

Design rules: secrets are never command-line arguments (environment / ``.env`` only); every
state-changing command needs an explicit ``--yes``; network and notification switches cannot be
overridden here; heavy modules are imported lazily so ``--help`` works without a database.
Exit codes are documented in `suv_deals.cli_commands._common`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

import suv_deals
from suv_deals.cli_commands import (
    bootstrap,
    config_cmd,
    crawl,
    credentials,
    db,
    doctor,
    inquiries,
    ops,
    processes,
    sources,
    tax,
)
from suv_deals.cli_commands._common import CliAbort, CliContext, report_abort


class SafeGroup(click.Group):
    """Turns `CliAbort` into ``error: ...`` on stderr plus the documented exit code."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except CliAbort as exc:
            report_abort(exc)
            ctx.exit(exc.code)


@click.group(
    cls=SafeGroup, context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110}
)
@click.version_option(suv_deals.__version__, prog_name="suv-deals")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Read settings from this file in addition to the environment (default: ./.env when present).",
)
@click.option("--no-env-file", is_flag=True, help="Ignore .env files; use the process environment only.")
@click.pass_context
def cli(ctx: click.Context, env_file: Path | None, no_env_file: bool) -> None:
    """SUV deal-discovery operator CLI (private; see docs/runbook.md)."""
    ctx.obj = CliContext(env_file=env_file, use_env_file=not no_env_file)


cli.add_command(doctor.doctor)
cli.add_command(config_cmd.config_group)
cli.add_command(sources.sources_group)
cli.add_command(crawl.crawl_group)
cli.add_command(processes.worker)
cli.add_command(processes.scheduler)
cli.add_command(processes.dispatcher)
cli.add_command(processes.reconcile)
cli.add_command(processes.api_group)
cli.add_command(ops.outbox_group)
cli.add_command(ops.reviews_group)
cli.add_command(ops.evidence_group)
cli.add_command(tax.tax_rules_group)
cli.add_command(db.db_group)
cli.add_command(credentials.credentials_group)
cli.add_command(bootstrap.bootstrap_group)
cli.add_command(inquiries.mail_worker_group)
cli.add_command(inquiries.sender_binding_group)
cli.add_command(inquiries.inquiries_group)
cli.add_command(inquiries.evaluation_group)


def main(argv: Sequence[str] | None = None) -> None:
    """Console-script entry point."""
    cli.main(args=list(argv) if argv is not None else None, prog_name="suv-deals")


__all__ = ["SafeGroup", "cli", "main"]
