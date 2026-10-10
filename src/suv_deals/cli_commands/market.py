"""``suv-deals market import FILE --evidence-kind asking_price|owner_estimate`` (spec 15; F2, wave D2).

The owner's way to record MK market evidence while no MK comparable source can be crawled (both
MK adapters are unimplemented placeholders whose terms are unreviewed). Without MK evidence
every valuation is ``insufficient_comparables`` and no listing can become ``inquiry_ready``.

- The file format and every validation rule are `domain.market_import` (bounded size and rows,
  no seller contact data, exact decimals, time-zone-aware timestamps, ad URL required for asking
  prices). The file's ``evidence_kind`` must equal ``--evidence-kind``.
- ``asking_price`` rows are recorded by the operator CLI acting as owner; ``owner_estimate``
  rows are the OWNER's assumption and need ``--owner-user-id`` (an active owner of the
  workspace; ``recorded_by`` names that user).
- One transaction; each observation is audited (``market.observation_record``) and the batch too
  (``market.import`` with the file's SHA-256). Re-importing the same file records nothing new.
  ``--dry-run`` validates and prints the counts without writing.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
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
    pass_cli,
    require_yes,
    run_async,
    workspace_option,
)


@click.group("market")
def market_group() -> None:
    """MK market evidence (comparables) recorded by the owner (spec 15)."""


@market_group.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@workspace_option
@click.option(
    "--evidence-kind",
    type=click.Choice(["asking_price", "owner_estimate"]),
    required=True,
    help="Must equal the file's evidence_kind (an asking price is never a sale).",
)
@click.option("--owner-user-id", type=click.UUID, default=None, help="Required for owner_estimate.")
@click.option("--reason", required=True, help="Why this evidence is imported (3-500 characters; audited).")
@click.option("--dry-run", is_flag=True, help="Validate and count only; nothing is written.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--yes", is_flag=True, help="Confirm the database write.")
@pass_cli
def market_import(
    cli: CliContext,
    path: Path,
    *,
    workspace: UUID | None,
    evidence_kind: str,
    owner_user_id: UUID | None,
    reason: str,
    dry_run: bool,
    as_json: bool,
    yes: bool,
) -> None:
    """Import owner-recorded MK asking prices or owner estimates (validated, audited, idempotent)."""
    from suv_deals.domain.enums import EvidenceKind
    from suv_deals.domain.market_import import MAX_IMPORT_BYTES, future_rows, parse_import, to_observation
    from suv_deals.errors import AppError

    settings = load_settings(cli)
    kind = EvidenceKind(evidence_kind)
    if kind == EvidenceKind.OWNER_ESTIMATE and owner_user_id is None:
        fail(
            "--evidence-kind owner_estimate needs --owner-user-id (an owner estimate is the owner's)",
            EXIT_USAGE,
        )
    if path.stat().st_size > MAX_IMPORT_BYTES:
        fail(f"the import file exceeds {MAX_IMPORT_BYTES} bytes", EXIT_USAGE)
    data = path.read_bytes()
    try:
        parsed = parse_import(data, expected_kind=kind)
    except AppError as exc:
        echo(f"INVALID  {path.name}: {exc.message}")
        problems = exc.details.get("problems") if exc.details else None
        for problem in problems if isinstance(problems, list) else []:
            echo(f"  - {problem}")
        fail("nothing was imported", EXIT_PROBLEMS)
    sha = hashlib.sha256(data).hexdigest()
    if not dry_run:
        require_yes(yes, "market import")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.cli_commands.inquiries import owner_actor_for
        from suv_deals.persistence import market_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            operator = operator_actor(workspace_id, "market-import", admin=True)
            async with unit_of_work(db, operator) as conn:
                late = future_rows(parsed, await db_now(conn))
                if late:
                    fail(f"rows observed in the future: {late[:10]}; nothing was imported", EXIT_PROBLEMS)
                actor = (
                    operator
                    if owner_user_id is None
                    else await owner_actor_for(conn, workspace_id, owner_user_id, "market-import")
                )
                if dry_run:
                    known = await market_repo.get_market_observations(
                        conn, operator, [to_observation(row, kind).id for row in parsed.observations]
                    )
                    report: dict[str, Any] = {
                        "dry_run": True,
                        "evidence_kind": kind.value,
                        "rows": len(parsed.observations),
                        "would_create": len(parsed.observations) - len(known),
                        "already_recorded": len(known),
                        "file_sha256": sha,
                    }
                else:
                    result = await market_repo.import_observations(
                        conn, actor, parsed, file_sha256=sha, reason=reason
                    )
                    report = {
                        "dry_run": False,
                        "evidence_kind": kind.value,
                        "rows": len(result.observation_ids),
                        "created": result.created,
                        "already_recorded": result.unchanged,
                        "file_sha256": sha,
                    }
        if as_json:
            emit_json(report)
        else:
            echo(" ".join(f"{key}={value}" for key, value in report.items()))
        return 0

    run_async(body)


__all__ = ["market_group"]
