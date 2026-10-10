"""``suv-deals tax-rules validate PATH`` (spec 16; docs/tax_rule_approval.md).

Parses a rule-set JSON (or every ``*.json`` in a directory) with the engine's own loader:
exact decimals, no duplicate keys, the closed input vocabulary, component/bracket checks, the
canonical content hash and the approval evidence required by approved/active statuses. Validation
never approves anything; it reports whether a file could be selected for production valuations.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from pathlib import Path

import click

from suv_deals.cli_commands._common import EXIT_PROBLEMS, echo, exit_with, fail


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
