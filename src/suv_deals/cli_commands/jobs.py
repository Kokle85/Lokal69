"""Queue operator commands: ``jobs blocked | unblock | resolve-blocked`` (spec 13, 37.5).

``jobs blocked``
    Read-only: the workspace's ``blocked`` jobs (id, type, blocker code, the inquiry id of a send
    job, last change). Never a payload value beyond that inquiry id.
``jobs unblock JOB_ID --reason ... --yes``
    Move a ``blocked`` job back to ``queued`` (``persistence.jobs.unblock``, audited
    ``job.unblock``). A send job blocked ``EMAIL_DELIVERY_UNCERTAIN`` (its e-mail may have left,
    spec 37.5) is refused unless ``--acknowledge-uncertain-delivery`` is given, and that
    acknowledgement is an OWNER decision: it needs ``--owner-user-id`` (an active owner of the
    workspace), so the audit names the owner. Even then the unblocked job cannot transmit a second
    e-mail: its inquiry is no longer ``queued``, so the dispatch holds.
``jobs resolve-blocked JOB_ID --outcome succeeded|cancelled --reason ... --yes``
    Close a ``blocked`` ``EMAIL_DELIVERY_UNCERTAIN`` send job whose inquiry was reconciled
    (``persistence.jobs.resolve_blocked``, audited ``job.resolve_blocked``). It never re-queues
    anything and refuses while the inquiry is still uncertain (reconcile it first).

Every state-changing command needs ``--yes`` and a reason. Nothing here sends anything.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_USAGE,
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

MAX_BLOCKED_ROWS: Final = 200
_BLOCKED_SQL: Final = """
select id, job_type, blocker_code, attempts, max_attempts, updated_at,
       case when payload ->> 'inquiry_id' ~ '^[0-9a-f-]{36}$' then payload ->> 'inquiry_id' end as inquiry_id
  from ops.jobs
 where workspace_id = %(ws)s and state = 'blocked'
 order by updated_at desc, id
 limit %(limit)s
"""


@click.group("jobs")
def jobs_group() -> None:
    """Blocked queue jobs: list, unblock (audited) and resolve reconciled send jobs."""


@jobs_group.command("blocked")
@workspace_option
@click.option("--limit", type=click.IntRange(1, MAX_BLOCKED_ROWS), default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def jobs_blocked(cli: CliContext, workspace: UUID | None, limit: int, as_json: bool) -> None:
    """List blocked jobs (ids and codes only; read-only)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.database import fetch_all
        from suv_deals.persistence.errors_map import mapped_errors
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "jobs-blocked")
            async with unit_of_work(db, actor) as conn, mapped_errors():
                found = await fetch_all(conn, _BLOCKED_SQL, {"ws": workspace_id, "limit": limit})
        rows: list[dict[str, Any]] = [
            {
                "id": r["id"],
                "job_type": r["job_type"],
                "blocker": r["blocker_code"],
                "inquiry_id": r["inquiry_id"],
                "attempts": f"{r['attempts']}/{r['max_attempts']}",
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in found
        ]
        if as_json:
            emit_json(rows)
        elif not rows:
            echo("No blocked jobs.")
        else:
            echo(table(rows, list(rows[0])))
        return 0

    run_async(body)


@jobs_group.command("unblock")
@click.argument("job_id", type=click.UUID)
@workspace_option
@click.option("--reason", required=True, help="Why (3-2000 characters; audited).")
@click.option(
    "--acknowledge-uncertain-delivery",
    is_flag=True,
    help=(
        "Required for a send job blocked EMAIL_DELIVERY_UNCERTAIN: the owner acknowledges that its"
        " e-mail may already have left (needs --owner-user-id; recorded in the audit event)."
    ),
)
@click.option(
    "--owner-user-id",
    type=click.UUID,
    default=None,
    help="The owner's user id (required with --acknowledge-uncertain-delivery).",
)
@click.option("--yes", is_flag=True, help="Confirm moving the job back to the queue.")
@pass_cli
def jobs_unblock(
    cli: CliContext,
    job_id: UUID,
    *,
    workspace: UUID | None,
    reason: str,
    acknowledge_uncertain_delivery: bool,
    owner_user_id: UUID | None,
    yes: bool,
) -> None:
    """Move a blocked job back to queued (audited; the uncertain-delivery case is an owner decision)."""
    if acknowledge_uncertain_delivery and owner_user_id is None:
        fail(
            "--acknowledge-uncertain-delivery needs --owner-user-id"
            " (an owner decision, audited as the owner)",
            EXIT_USAGE,
        )
    settings = load_settings(cli)
    echo(f"Job to unblock: {job_id}")
    if acknowledge_uncertain_delivery:
        echo("  The owner acknowledges that this send job's e-mail may already have left (audited).")
        echo("  No second e-mail can follow: the inquiry is no longer queued, so the dispatch holds.")
    require_yes(yes, "jobs unblock")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.cli_commands.inquiries import owner_actor_for
        from suv_deals.persistence import jobs
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "jobs-unblock", admin=True)
            if owner_user_id is not None:
                async with unit_of_work(db, actor) as conn:
                    actor = await owner_actor_for(conn, workspace_id, owner_user_id, "jobs-unblock")
            async with unit_of_work(db, actor) as conn:
                record = await jobs.unblock(
                    conn,
                    actor,
                    job_id,
                    reason=reason,
                    acknowledge_uncertain_delivery=acknowledge_uncertain_delivery,
                )
        echo(f"Unblocked job {record.id} ({record.job_type.value}): state {record.state.value} (audited).")
        return 0

    run_async(body)


@jobs_group.command("resolve-blocked")
@click.argument("job_id", type=click.UUID)
@workspace_option
@click.option(
    "--outcome",
    type=click.Choice(["succeeded", "cancelled"]),
    required=True,
    help="succeeded: the reconciled inquiry was accepted; cancelled: it was proven unsent / handled.",
)
@click.option("--reason", required=True, help="Why (3-2000 characters; audited).")
@click.option("--yes", is_flag=True, help="Confirm closing the blocked send job.")
@pass_cli
def jobs_resolve_blocked(
    cli: CliContext, job_id: UUID, *, workspace: UUID | None, outcome: str, reason: str, yes: bool
) -> None:
    """Close a blocked uncertain-delivery send job after its inquiry was reconciled (never re-queues)."""
    settings = load_settings(cli)
    echo(f"Blocked send job to close as {outcome}: {job_id} (nothing is re-queued or sent)")
    require_yes(yes, "jobs resolve-blocked")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import jobs
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "jobs-resolve-blocked", admin=True)
            async with unit_of_work(db, actor) as conn:
                record = await jobs.resolve_blocked(
                    conn,
                    actor,
                    job_id,
                    outcome="succeeded" if outcome == "succeeded" else "cancelled",
                    reason=reason,
                )
        echo(f"Closed job {record.id} as {record.state.value} ({record.last_error_code}; audited).")
        return 0

    run_async(body)


__all__ = ["jobs_group"]
