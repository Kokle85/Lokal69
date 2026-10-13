"""``suv-deals tax-rules validate PATH | list | store PATH`` (spec 16; docs/tax_rule_approval.md).

- ``validate`` parses a rule-set JSON (or every ``*.json`` in a directory) with the engine's own
  loader: exact decimals, no duplicate keys, the closed input vocabulary, component/bracket checks,
  the canonical content hash and the approval evidence required by approved/active statuses.
  Validation never approves anything; it reports whether a file could be selected for production
  valuations.
- ``list`` (read-only) shows the workspace's stored versions and the repository drafts
  (``config/tax_rules/drafts``): status, content hash, effective dates, whether a draft file is
  stored, and which rule set the import-cost calculator would use today.
- ``store`` puts a validated ``status: draft`` file into the workspace as a draft, as the verified
  ACTIVE owner (``--owner-user-id``; audited ``tax_rules.store``; ``--yes``). Review, approval and
  activation are owner steps outside the CLI: a file never approves or activates anything, and a
  draft never reaches valuations or alerts.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
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
    refuse,
    require_yes,
    run_async,
    table,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.domain.tax_engine import RuleSet
    from suv_deals.persistence.valuation_repo import StoredRuleSet

_JURISDICTION_RE: Final = re.compile(r"^[A-Z]{2}$")


@click.group("tax-rules")
def tax_rules_group() -> None:
    """Versioned import-tax rule sets (approval stays a separate owner step)."""


@tax_rules_group.command("validate")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def validate(path: Path) -> None:
    """Validate one rule-set JSON file or every *.json file in a directory."""
    from suv_deals.domain.enums import TaxRuleStatus
    from suv_deals.domain.tax_engine import compute_rule_set_sha256, load_rule_set_file
    from suv_deals.errors import AppError

    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    if not files:
        fail("no *.json rule-set files found")
    invalid = 0
    for file in files:
        try:
            rule_set = load_rule_set_file(file)
        except AppError as exc:
            invalid += 1
            echo(f"INVALID  {file.name}: {exc.message}")
            problems = exc.details.get("problems") if exc.details else None
            for problem in problems if isinstance(problems, list) else []:
                echo(f"  - {problem}")
            continue
        usable = rule_set.status in (TaxRuleStatus.ACTIVE, TaxRuleStatus.APPROVED) and not rule_set.is_fixture
        echo(f"VALID    {file.name}: {rule_set.label()}")
        echo(f"  jurisdiction       : {rule_set.jurisdiction}")
        echo(
            f"  status             : {rule_set.status.value}"
            + ("  (SYNTHETIC fixture)" if rule_set.is_fixture else "")
        )
        echo(f"  valid_from / to    : {rule_set.valid_from or 'unset'} / {rule_set.valid_to or 'open'}")
        echo(f"  components         : {len(rule_set.components)}")
        echo(
            "  content sha256     : "
            + (
                "matches the recorded hash"
                if rule_set.sha256
                else f"not recorded (computed {compute_rule_set_sha256(rule_set)[:16]}...)"
            )
        )
        echo(
            "  production use     : "
            + (
                "selectable once its period applies"
                if usable
                else "NOT selectable (needs owner approval/activation)"
            )
        )
    if invalid:
        exit_with(EXIT_PROBLEMS)


# --------------------------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------------------------


def _hash(rule_set: RuleSet) -> tuple[str, bool]:
    """The recorded content hash, or the one computed from the content of an unsealed draft."""
    from suv_deals.domain.tax_engine import compute_rule_set_sha256

    return (rule_set.sha256 or compute_rule_set_sha256(rule_set)), rule_set.sha256 is not None


def _row(rule_set: RuleSet, *, origin: str, file: str | None, note: str) -> dict[str, Any]:
    sha256, recorded = _hash(rule_set)
    return {
        "origin": origin,
        "file": file,
        "rule_set": rule_set.label(),
        "rule_set_id": rule_set.rule_set_id,
        "version": rule_set.version,
        "jurisdiction": rule_set.jurisdiction,
        "vehicle_categories": list(rule_set.vehicle_categories),
        "status": rule_set.status.value,
        "valid_from": None if rule_set.valid_from is None else rule_set.valid_from.isoformat(),
        "valid_to": None if rule_set.valid_to is None else rule_set.valid_to.isoformat(),
        "sha256": sha256,
        "sha256_recorded": recorded,
        "is_fixture": rule_set.is_fixture,
        "note": note,
    }


def stored_rows(stored: Sequence[StoredRuleSet]) -> list[dict[str, Any]]:
    """One row per stored version (its vehicle categories together)."""
    rows = []
    for item in stored:
        note = f"stored {item.created_at.date().isoformat()}"
        if item.rule_set.approved_at is not None:
            note += f", approved {item.rule_set.approved_at.date().isoformat()}"
        if item.rule_set.is_fixture:
            note += ", SYNTHETIC fixture"
        rows.append(_row(item.rule_set, origin="stored", file=None, note=note))
    return rows


def draft_file_rows(directory: Path, stored: Sequence[StoredRuleSet]) -> list[dict[str, Any]]:
    """One row per ``*.json`` under ``config/tax_rules/drafts``, with whether the workspace holds
    the same version and whether its content is identical (a different copy blocks the preview)."""
    from suv_deals.domain.enums import TaxRuleStatus
    from suv_deals.domain.tax_engine import compute_rule_set_sha256, load_rule_set_file
    from suv_deals.errors import AppError

    by_label = {item.rule_set.label(): item.rule_set for item in stored}
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        name = f"drafts/{path.name}"
        try:
            rule_set = load_rule_set_file(path)
        except AppError as exc:
            rows.append(
                {"origin": "file", "file": name, "status": "invalid", "note": f"INVALID: {exc.message}"}
            )
            continue
        copy = by_label.get(rule_set.label())
        if rule_set.status != TaxRuleStatus.DRAFT or rule_set.is_fixture:
            note = "ignored by the calculator: files under tax_rules/drafts are non-fixture drafts only"
        elif copy is None:
            note = "not stored in this workspace (`tax-rules store` stores it)"
        elif compute_rule_set_sha256(copy) == compute_rule_set_sha256(rule_set):
            note = f"stored in this workspace ({copy.status.value}), same content"
        else:
            note = (
                "a stored copy of this version has DIFFERENT content: no preview until one of them "
                "gets a new version"
            )
        rows.append(_row(rule_set, origin="file", file=name, note=note))
    return rows


def calculator_selection(stored: Sequence[StoredRuleSet], config_dir: Path, on: date) -> dict[str, Any]:
    """Which rule set ``import-costs estimate`` would use on ``on`` (its own selection)."""
    from suv_deals.domain.import_estimate import DEFAULT_JURISDICTION, DEFAULT_VEHICLE_CATEGORY
    from suv_deals.errors import ValidationFailed
    from suv_deals.persistence.queries.import_costs import (
        DRAFT_RULES_DIR,
        load_draft_rule_sets,
        select_calculator_rule_set,
    )

    files, _notes = load_draft_rule_sets(config_dir / DRAFT_RULES_DIR)
    result: dict[str, Any] = {
        "jurisdiction": DEFAULT_JURISDICTION,
        "vehicle_category": DEFAULT_VEHICLE_CATEGORY,
        "date": on.isoformat(),
        "mode": "unavailable",
        "rule_set": None,
        "status": None,
    }
    try:
        selection = select_calculator_rule_set(
            stored, files, DEFAULT_JURISDICTION, DEFAULT_VEHICLE_CATEGORY, on
        )
    except ValidationFailed as exc:  # e.g. overlapping ACTIVE versions: never a silent choice
        return {**result, "reason": exc.message}
    chosen = selection.rule_set
    if chosen is not None:
        result.update(mode=selection.mode, rule_set=chosen.label(), status=chosen.status.value)
    return {**result, "reason": selection.reason}


def _jurisdiction(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not _JURISDICTION_RE.fullmatch(value.strip().upper()):
        raise click.BadParameter("a two-letter jurisdiction code (e.g. MK)")
    return None if value is None else value.strip().upper()


@tax_rules_group.command("list")
@workspace_option
@click.option(
    "--jurisdiction", default=None, callback=_jurisdiction, help="Only this jurisdiction (e.g. MK)."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def list_rule_sets(cli: CliContext, workspace: UUID | None, jurisdiction: str | None, as_json: bool) -> None:
    """Stored rule-set versions and the repository drafts: status, hash, dates (read-only).

    Also shows which rule set the import-cost calculator would use today: an ACTIVE, owner-approved
    version, otherwise an INDICATIVE PREVIEW of the newest unapproved one, otherwise none."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import valuation_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.queries.import_costs import DRAFT_RULES_DIR
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "tax-rules-list")
            async with unit_of_work(db, actor) as conn:
                stored = await valuation_repo.list_rule_sets(conn, actor)
                today = (await db_now(conn)).date()
        rows = [
            row
            for row in [*stored_rows(stored), *draft_file_rows(settings.config_dir / DRAFT_RULES_DIR, stored)]
            if jurisdiction is None or row.get("jurisdiction") in (None, jurisdiction)
        ]
        selection = calculator_selection(stored, settings.config_dir, today)
        if as_json:
            emit_json({"workspace_id": str(workspace_id), "rule_sets": rows, "calculator": selection})
            return 0
        for row in rows:
            if "sha256" in row:
                row["hash"] = f"{row['sha256'][:16]}..." + ("" if row["sha256_recorded"] else " (computed)")
            row["source"] = row["file"] or "stored"
        echo(f"Workspace {workspace_id}")
        columns = ["source", "rule_set", "status", "jurisdiction", "valid_from", "valid_to", "hash", "note"]
        echo(table(rows, columns) if rows else "No stored rule set and no draft file.")
        echo()
        chosen = (
            f"{selection['rule_set']} ({selection['status']})" if selection["rule_set"] else "no rule set"
        )
        echo(
            f"import-costs estimate ({selection['jurisdiction']} {selection['vehicle_category']}, "
            f"{selection['date']}): {selection['mode']}, {chosen}"
        )
        echo(f"  {selection['reason']}")
        echo(
            "Review, approval and activation are owner steps outside the CLI (docs/tax_rule_approval.md); "
            "valuations and alerts use only an ACTIVE, owner-approved version."
        )
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------------------------


@tax_rules_group.command("store")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@workspace_option
@click.option(
    "--owner-user-id",
    type=click.UUID,
    required=True,
    help="The owner's user id: an ACTIVE owner of the workspace stores the draft (audited as that owner).",
)
@click.option("--yes", is_flag=True, help="Confirm the database write.")
@pass_cli
def store(cli: CliContext, path: Path, *, workspace: UUID | None, owner_user_id: UUID, yes: bool) -> None:
    """Store a validated DRAFT rule-set file in the workspace, as status draft (owner; audited).

    Only ``status: draft``, non-fixture files. An identical stored version is reported and left
    alone; a different one under the same id and version is refused (content is versioned, never
    overwritten). A stored draft is still unapproved: valuations and alerts never use it, and the
    import-cost calculator shows it only as an INDICATIVE PREVIEW."""
    from suv_deals.domain.enums import TaxRuleStatus
    from suv_deals.domain.tax_engine import compute_rule_set_sha256, load_rule_set_file
    from suv_deals.errors import AppError

    try:
        rule_set = load_rule_set_file(path)
    except AppError as exc:
        echo(f"INVALID  {path.name}: {exc.message}")
        problems = exc.details.get("problems") if exc.details else None
        for problem in problems if isinstance(problems, list) else []:
            echo(f"  - {problem}")
        fail("nothing was stored", EXIT_PROBLEMS)
    if rule_set.status != TaxRuleStatus.DRAFT:
        refuse(
            f"{path.name} has status {rule_set.status.value}; only drafts are stored here (approval and "
            "activation are owner transitions, never taken from a file)"
        )
    if rule_set.is_fixture:
        refuse(f"{path.name} is a SYNTHETIC fixture; fixtures are never stored in a workspace")
    content = compute_rule_set_sha256(rule_set)
    echo(f"Rule set           : {rule_set.label()} ({path.name})")
    echo(f"  jurisdiction     : {rule_set.jurisdiction} {', '.join(rule_set.vehicle_categories)}")
    echo(f"  valid_from / to  : {rule_set.valid_from or 'unset'} / {rule_set.valid_to or 'open'}")
    echo(f"  components       : {len(rule_set.components)}")
    echo(f"  content sha256   : {content}")
    echo("  stored as        : draft (NOT approved; never used for valuations or alerts)")
    require_yes(yes, "tax-rules store")
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.cli_commands.inquiries import owner_actor_for
        from suv_deals.persistence import valuation_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            operator = operator_actor(workspace_id, "tax-rules-store", admin=True)
            async with unit_of_work(db, operator) as conn:
                owner = await owner_actor_for(conn, workspace_id, owner_user_id, "tax-rules-store")
            async with unit_of_work(db, owner) as conn:
                existing = await valuation_repo.list_rule_sets(
                    conn, owner, jurisdiction=rule_set.jurisdiction
                )
                copy = next((s.rule_set for s in existing if s.rule_set.label() == rule_set.label()), None)
                if copy is None:
                    await valuation_repo.store_rule_set(conn, owner, rule_set)
        if copy is not None:
            if compute_rule_set_sha256(copy) != content:
                fail(
                    f"{rule_set.label()} is already stored with DIFFERENT content ({copy.status.value}); "
                    "nothing was stored. Give the file a new version",
                    EXIT_PROBLEMS,
                )
            echo(f"Already stored ({copy.status.value}, identical content); nothing changed.")
            return 0
        echo(
            f"Stored {rule_set.label()} as draft in workspace {workspace_id} (owner {owner_user_id}; "
            "audited tax_rules.store)."
        )
        echo(
            "It is NOT approved: valuations and alerts never use it; `import-costs estimate` shows it only "
            "as an INDICATIVE PREVIEW. Review, approval and activation stay owner steps outside the CLI "
            "(docs/tax_rule_approval.md)."
        )
        return 0

    run_async(body)
