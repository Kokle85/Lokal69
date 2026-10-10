"""Read-only operator views: ``outbox inspect``, ``reviews list``, ``evidence verify``.

Spec sections 13, 14, 27 and 29.

All of them run as ``DATABASE_SET_ROLE`` with the worker system principal of each workspace (RLS
scoped); none of them changes data. Payloads, URLs and seller text are never printed.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CliContext,
    echo,
    emit_json,
    fail,
    load_settings,
    parse_csv,
    pass_cli,
    run_async,
    table,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.persistence.database import Conn

# --------------------------------------------------------------------------------------------
# outbox inspect
# --------------------------------------------------------------------------------------------


@click.group("outbox")
def outbox_group() -> None:
    """Transactional outbox state (delivery is the dispatcher's job)."""


@outbox_group.command("inspect")
@workspace_option
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True, help="Attention rows.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def outbox_inspect(cli: CliContext, workspace: UUID | None, limit: int, as_json: bool) -> None:
    """Counts per state plus the uncertain/blocked/dead-letter events (never their payloads)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspaces
        from suv_deals.persistence import outbox
        from suv_deals.persistence.transactions import unit_of_work

        result: list[dict[str, Any]] = []
        async with open_database(settings, application_name="suv-deals-cli") as db:
            for workspace_id in await resolve_workspaces(db, workspace):
                actor = operator_actor(workspace_id, "outbox-inspect")
                async with unit_of_work(db, actor) as conn:
                    stats = await outbox.outbox_stats(conn, actor)
                    attention = await outbox.list_attention(conn, actor, limit=limit)
                result.append(
                    {
                        "workspace_id": str(workspace_id),
                        "as_of": stats.as_of.isoformat(),
                        "counts": {state.value: count for state, count in sorted(stats.counts.items())},
                        "oldest_due_age_seconds": (
                            None
                            if stats.oldest_due_age is None
                            else int(stats.oldest_due_age.total_seconds())
                        ),
                        "attention": [
                            {
                                "event_id": str(e.event_id),
                                "event_type": e.event_type,
                                "state": e.state.value,
                                "attempts": e.attempts,
                                "last_error_code": e.last_error_code,
                                "blocker_code": e.blocker_code,
                                "is_fixture": e.is_fixture,
                                "created": e.event_created_at.isoformat(),
                            }
                            for e in attention
                        ],
                    }
                )
        if as_json:
            emit_json(result)
            return 0
        if not result:
            echo("No active workspace.")
        for item in result:
            counts = ", ".join(f"{k}={v}" for k, v in item["counts"].items()) or "no open events"
            echo(f"Workspace {item['workspace_id']} (as of {item['as_of']}): {counts}")
            if item["oldest_due_age_seconds"] is not None:
                echo(f"  oldest due event waits {item['oldest_due_age_seconds']} s")
            if item["attention"]:
                echo("  needs attention (never blindly resent):")
                columns = ["event_id", "event_type", "state", "attempts", "last_error_code", "blocker_code"]
                echo("  " + table(item["attention"], columns).replace("\n", "\n  "))
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# reviews list
# --------------------------------------------------------------------------------------------


@click.group("reviews")
def reviews_group() -> None:
    """Review queue (decisions are made in the dashboard or through MCP, never here)."""


@reviews_group.command("list")
@click.option(
    "--status",
    "status_csv",
    default="pending",
    show_default=True,
    help="Comma-separated review states (pending, claimed, needs_information, watch, shortlisted, ...).",
)
@workspace_option
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def reviews_list(cli: CliContext, status_csv: str, workspace: UUID | None, limit: int, as_json: bool) -> None:
    """List review cases by state (fixture cases are labelled)."""
    from suv_deals.domain.enums import ReviewState

    valid = {s.value for s in ReviewState}
    states = parse_csv(status_csv)
    unknown = [s for s in states if s not in valid]
    if unknown or not states:
        fail(f"unknown state(s): {', '.join(unknown) or '-'}; known: {', '.join(sorted(valid))}", EXIT_USAGE)
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspaces
        from suv_deals.persistence import reviews_repo
        from suv_deals.persistence.transactions import unit_of_work

        wanted = [ReviewState(s) for s in states]
        rows: list[dict[str, Any]] = []
        async with open_database(settings, application_name="suv-deals-cli") as db:
            for workspace_id in await resolve_workspaces(db, workspace):
                actor = operator_actor(workspace_id, "reviews-list")
                async with unit_of_work(db, actor) as conn:
                    found = await reviews_repo.list_cases_by_state(conn, actor, wanted, limit=limit)
                rows.extend(
                    {
                        "workspace_id": workspace_id,
                        "id": case.case_id,
                        "state": case.state.value,
                        "profile_key": case.profile_key.value,
                        "queue_label": case.queue_label,
                        "readiness": case.readiness,
                        "priority": case.priority,
                        "is_fixture": case.is_fixture,
                        "created_at": case.created_at,
                        "claim_expires_at": case.claim_expires_at,
                        "source_key": case.source_key,
                    }
                    for case in found
                )
        if as_json:
            emit_json(rows)
            return 0
        if not rows:
            echo(f"No review cases in state(s): {', '.join(states)}.")
            return 0
        columns = [
            "id",
            "state",
            "profile_key",
            "readiness",
            "priority",
            "source_key",
            "is_fixture",
            "created_at",
        ]
        echo(table(rows, columns))
        echo(f"{len(rows)} case(s). Decide in the dashboard or through the MCP review tools.")
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# evidence verify
# --------------------------------------------------------------------------------------------

_REVISIONS_SQL: Final = """
select id, semantic_hash, normalized
  from app.listing_revisions
 where workspace_id = %(ws)s
 order by created_at desc, id
 limit %(limit)s
"""

_SNAPSHOTS_SQL: Final = """
select id, storage_backend, object_key, content_hash
  from ops.source_snapshots
 where workspace_id = %(ws)s and object_key is not null and purged_at is null
 order by fetched_at desc, id
 limit %(limit)s
"""


def revision_hash_status(semantic_hash: str, normalized: object) -> str:
    """``ok`` | ``mismatch`` | ``unreadable`` for one stored revision."""
    from pydantic import ValidationError

    from suv_deals.domain.listings import NormalizedListing

    try:
        listing = NormalizedListing.model_validate(normalized)
    except ValidationError:
        return "unreadable"
    return "ok" if listing.semantic_hash() == semantic_hash else "mismatch"


async def _verify_revisions(conn: Conn, workspace_id: UUID, limit: int) -> dict[str, list[str]]:
    from suv_deals.persistence.database import fetch_all

    result: dict[str, list[str]] = {"ok": [], "mismatch": [], "unreadable": []}
    for row in await fetch_all(conn, _REVISIONS_SQL, {"ws": workspace_id, "limit": limit}):
        result[revision_hash_status(row["semantic_hash"], row["normalized"])].append(str(row["id"]))
    return result


@click.group("evidence")
def evidence_group() -> None:
    """Evidence integrity checks."""


@evidence_group.command("verify")
@workspace_option
@click.option(
    "--limit", type=click.IntRange(1, 100_000), default=1000, show_default=True, help="Rows per check."
)
@click.option("--skip-objects", is_flag=True, help="Do not read retained snapshot objects from storage.")
@pass_cli
def evidence_verify(cli: CliContext, workspace: UUID | None, limit: int, skip_objects: bool) -> None:
    """Recompute revision semantic hashes and retained snapshot content hashes (read-only).

    Exit 1 on any mismatch or missing retained object. Unreadable revisions (an older schema) are
    reported but are not a mismatch.
    """
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspaces
        from suv_deals.errors import AppError
        from suv_deals.persistence.database import fetch_all
        from suv_deals.persistence.errors_map import mapped_errors
        from suv_deals.persistence.storage import content_sha256, snapshot_store_from_settings
        from suv_deals.persistence.transactions import unit_of_work

        # Built only when objects are read: --skip-objects (e.g. the restore check) needs no
        # storage configuration at all.
        store = (
            None
            if skip_objects
            else snapshot_store_from_settings(
                mode=settings.snapshot_storage,
                local_dir=settings.snapshot_local_dir,
                supabase_url=settings.supabase_url,
                secret_key=settings.supabase_secret_key,
                bucket=settings.supabase_storage_bucket,
            )
        )
        failed = False
        async with open_database(settings, application_name="suv-deals-cli") as db:
            for workspace_id in await resolve_workspaces(db, workspace):
                actor = operator_actor(workspace_id, "evidence-verify")
                async with unit_of_work(db, actor) as conn, mapped_errors():
                    revisions = await _verify_revisions(conn, workspace_id, limit)
                    snapshots = await fetch_all(conn, _SNAPSHOTS_SQL, {"ws": workspace_id, "limit": limit})
                echo(f"Workspace {workspace_id}:")
                echo(
                    f"  revisions: {len(revisions['ok'])} ok, {len(revisions['mismatch'])} hash mismatch, "
                    f"{len(revisions['unreadable'])} unreadable"
                )
                for revision_id in revisions["mismatch"][:20]:
                    echo(f"    mismatch: revision {revision_id}")
                failed = failed or bool(revisions["mismatch"])
                checked = missing = mismatched = skipped = 0
                for row in snapshots:
                    if store is None or row["storage_backend"] != store.backend:
                        skipped += 1
                        continue
                    try:
                        data = await store.get(row["object_key"])
                    except AppError:
                        missing += 1
                        echo(f"    missing or unreadable object: snapshot {row['id']}")
                        continue
                    checked += 1
                    if content_sha256(data) != row["content_hash"]:
                        mismatched += 1
                        echo(f"    content hash mismatch: snapshot {row['id']}")
                echo(
                    f"  retained snapshots: {checked} verified, {mismatched} mismatch, {missing} missing, "
                    f"{skipped} skipped (other backend or --skip-objects)"
                )
                failed = failed or bool(missing or mismatched)
        echo("Storage objects are backed up separately from the database (docs/runbook.md).")
        return EXIT_PROBLEMS if failed else 0

    run_async(body)
