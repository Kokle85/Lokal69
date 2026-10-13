"""``suv-deals import-costs estimate | tax-rules list | tax-rules store`` on a real database (marker
``db``).

The calculator runs as the operator's system actor (``deals:read``) through the shared read
service and writes nothing; ``tax-rules store`` stores the shipped DRAFT file as the verified
ACTIVE owner (audited) and never approves or activates it. Every workspace, listing and rate is
SYNTHETIC; 61.5337 is the fact sheet's golden-case customs rate (test value only) and G01 the
golden total of the shipped draft. Where a test needs ACTIVE rules, the draft is activated through
the real owner transitions in a throwaway test workspace only.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from click.testing import Result
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed, unique
from tests.integration.import_costs.support import (
    CUSTOMS_RATE,
    DRAFT_FILE,
    activate,
    add_listing,
    counts,
    mk_draft,
)
from tests.integration.import_costs.support import owner as owner_actor

from suv_deals.domain.tax_engine import compute_rule_set_sha256
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[2]
G01 = "168154.45"  # D1: diesel, NEDC 189 (COC), EUR 3,000 + EUR 700 transport, no proof of origin
LABEL = "mk-passenger-car-import@2026.10-draft.1"
FACTS = (
    "--price", "3000", "--fuel", "diesel", "--co2", "nedc:189", "--co2-document", "coc",
    "--declaration-date", "2026-10-13",
)  # fmt: skip


def _run[T](db_url: str, work: Callable[[Database], Awaitable[T]]) -> T:
    """Arrange with the repositories in a private event loop (the CLI runs its own loop)."""

    async def main() -> T:
        db = Database(db_url, set_role="suv_backend", min_size=1, max_size=4)
        await db.open()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(main())


@pytest.fixture
def source(seed: Seed, workspace: UUID) -> tuple[UUID, str]:
    key = unique("synthetic_src").lower()
    return seed.source(workspace, source_key=key, mode="public_html", adapter="synthetic_adapter"), key


def _estimate(run_cli: Cli, env: dict[str, str], workspace: UUID, *args: str) -> Result:
    return run_cli("import-costs", "estimate", "--workspace", str(workspace), *args, env=env)


def _record_customs_rate(run_cli: Cli, env: dict[str, str], workspace: UUID) -> None:
    """The operator's documented path: `fx record --purpose customs` (SYNTHETIC source)."""
    recorded = run_cli(
        "fx", "record", "--workspace", str(workspace), "--base", "EUR", "--quote", "MKD",
        "--rate", CUSTOMS_RATE, "--date", "2026-10-01", "--purpose", "customs",
        "--provider", "SYNTHETIC customs list", "--source-ref", "SYNTHETIC monthly list",
        "--reason", "CLI calculator test", "--yes", env=env,
    )  # fmt: skip
    assert recorded.exit_code == 0, recorded.output


def _owner(seed: Seed, workspace: UUID) -> UUID:
    user = seed.user()
    seed.membership(workspace, user, "owner")
    return user


def _stored(seed: Seed, workspace: UUID) -> list[tuple[Any, ...]]:
    return seed.conn.execute(
        "select status, version, created_by from app.tax_rule_sets where workspace_id = %s order by id",
        (workspace,),
    ).fetchall()


# --------------------------------------------------------------------------- import-costs estimate


def test_estimate_from_facts_is_an_indicative_preview_and_writes_nothing(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    before = counts(seed, workspace)
    raw = _estimate(run_cli, db_env, workspace, *FACTS, "--customs-eur-mkd-rate", CUSTOMS_RATE, "--json")
    assert raw.exit_code == 0, raw.output
    assert counts(seed, workspace) == before  # read-only: no valuation, rate, rule, audit, job row
    result = json.loads(raw.output)
    data = result["data"]
    assert data["mode"] == "indicative_preview" and data["production_ready"] is False
    assert data["rule"]["rule_set_id"] == "mk-passenger-car-import" and data["rule"]["status"] == "draft"
    legal = data["scenarios"][0]
    assert legal["name"] == "legal_base" and legal["total"] is None
    assert legal["indicative_total"]["amount"] == G01
    assert data["customs_fx_source"] == "caller_supplied"
    assert data["banner"]["mk"].startswith("ИНДИКАТИВНА ПРОЦЕНКА")
    assert {w["code"] for w in result["warnings"]} >= {"TAX_RULES_UNAPPROVED", "SELLER_CLAIMS_UNVERIFIED"}
    human = _estimate(run_cli, db_env, workspace, *FACTS, "--customs-eur-mkd-rate", CUSTOMS_RATE)
    assert human.exit_code == 0, human.output
    assert "!! INDICATIVE PREVIEW" in human.output and "mode: indicative_preview" in human.output
    assert f"{G01} MKD  (2732.72 EUR)" in human.output and "indicative_total" in human.output
    assert "CALLER_SUPPLIED_CUSTOMS_RATE:1 EUR = 61.5337 MKD" in human.output
    assert counts(seed, workspace) == before


def test_estimate_from_a_listing_with_a_recorded_customs_rate(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, source: tuple[UUID, str]
) -> None:
    listing = add_listing(seed, workspace, source[0], source[1])
    _record_customs_rate(run_cli, db_env, workspace)
    args = ("--listing-id", str(listing), "--co2-document", "coc", "--declaration-date", "2026-10-13")
    raw = _estimate(run_cli, db_env, workspace, *args, "--json")
    assert raw.exit_code == 0, raw.output
    data = json.loads(raw.output)["data"]
    assert data["listing"]["listing_id"] == str(listing) and data["customs_fx_source"] == "recorded"
    assert data["scenarios"][0]["indicative_total"]["amount"] == G01
    sources = {s["fact"]: s["source"] for s in data["fact_sources"]}
    assert (
        sources["price"] == "listing" and sources["co2"] == "listing" and sources["co2_document"] == "request"
    )
    human = _estimate(run_cli, db_env, workspace, *args, "-v")
    assert human.exit_code == 0, human.output
    assert f"listing                     : {listing} revision 1" in human.output
    assert "price                     : 3000.00 EUR (advertised)  [listing]" in human.output
    assert "[helper, not counted]" in human.output
    unknown = _estimate(run_cli, db_env, workspace, "--listing-id", str(uuid4()))
    assert unknown.exit_code == 1 and "NOT_FOUND" in unknown.output


def test_missing_customs_rate_leaves_charges_unknown_and_names_the_fx_command(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    result = _estimate(run_cli, db_env, workspace, *FACTS)
    assert result.exit_code == 0, result.output
    assert "CUSTOMS_FX_RATE_NOT_RECORDED" in result.output
    assert (
        "suv-deals fx record --base EUR --quote MKD" in result.output and "--purpose customs" in result.output
    )
    assert "indicative_total" in result.output and "unknown (the calculation is incomplete" in result.output
    assert "(NOT a total)" in result.output and "warning codes" in result.output
    assert "UNKNOWN_COSTS" in result.output


def test_an_active_rule_set_gives_final_totals(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID, seed: Seed
) -> None:
    _run(db_url, lambda db: activate(db, owner_actor(workspace), mk_draft()))
    args = (*FACTS, "--price-basis", "invoice", "--customs-eur-mkd-rate", CUSTOMS_RATE)
    raw = _estimate(run_cli, db_env, workspace, *args, "--json")
    assert raw.exit_code == 0, raw.output
    data = json.loads(raw.output)["data"]
    assert data["mode"] == "active" and data["production_ready"] is True and data["banner"] is None
    assert data["scenarios"][0]["total"]["amount"] == G01 and data["scenarios"][0]["indicative_total"] is None
    human = _estimate(run_cli, db_env, workspace, *args)
    assert "mode: active  production-ready: yes" in human.output and "!!" not in human.output
    assert seed.scalar("select count(*) from app.valuations where workspace_id = %s", (workspace,)) == 0


def test_without_any_rule_set_the_estimate_is_unavailable(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, tmp_path: Path
) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config, ignore=shutil.ignore_patterns("drafts"))
    env = {**db_env, "CONFIG_DIR": str(config)}
    result = _estimate(run_cli, env, workspace, *FACTS, "--customs-eur-mkd-rate", CUSTOMS_RATE)
    assert result.exit_code == 0, result.output
    assert "mode: unavailable" in result.output and "!! NO APPLICABLE RULES" in result.output
    assert "Missing facts: tax_rule_set" in result.output and "Scenario" not in result.output


# --------------------------------------------------------------------------- tax-rules list / store


def test_list_then_store_the_shipped_draft_as_the_owner(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    listed = run_cli("tax-rules", "list", "--workspace", str(workspace), "--json", env=db_env)
    assert listed.exit_code == 0, listed.output
    report = json.loads(listed.output)
    files = [r for r in report["rule_sets"] if r["origin"] == "file"]
    assert [(r["file"], r["status"], r["sha256_recorded"]) for r in files] == [
        ("drafts/" + DRAFT_FILE.name, "draft", False)
    ]
    assert (
        files[0]["sha256"] == compute_rule_set_sha256(mk_draft()) and files[0]["valid_from"] == "2025-07-01"
    )
    assert "not stored in this workspace" in files[0]["note"]
    assert not [r for r in report["rule_sets"] if r["origin"] == "stored"]
    assert report["calculator"]["mode"] == "indicative_preview" and report["calculator"]["rule_set"] == LABEL

    store = ("tax-rules", "store", str(DRAFT_FILE), "--workspace", str(workspace))
    refused = run_cli(*store, "--owner-user-id", str(seed.user()), env=db_env)
    assert refused.exit_code == 3 and "re-run with --yes" in refused.output
    stranger = run_cli(*store, "--owner-user-id", str(seed.user()), "--yes", env=db_env)
    assert stranger.exit_code == 1 and "not an active owner" in stranger.output
    assert _stored(seed, workspace) == []

    owner = _owner(seed, workspace)
    stored = run_cli(*store, "--owner-user-id", str(owner), "--yes", env=db_env)
    assert stored.exit_code == 0, stored.output
    assert f"Stored {LABEL} as draft" in stored.output and "NOT approved" in stored.output
    assert _stored(seed, workspace) == [("draft", "2026.10-draft.1", owner)]
    audits = seed.conn.execute(
        "select actor_principal_id, actor_kind, metadata ->> 'rule_set' from ops.audit_events"
        " where workspace_id = %s and action = 'tax_rules.store'",
        (workspace,),
    ).fetchall()
    assert audits == [(owner, "user", LABEL)]
    again = run_cli(*store, "--owner-user-id", str(owner), "--yes", env=db_env)
    assert again.exit_code == 0 and "Already stored (draft, identical content)" in again.output
    assert len(_stored(seed, workspace)) == 1

    human = run_cli("tax-rules", "list", "--workspace", str(workspace), env=db_env)
    assert human.exit_code == 0, human.output
    assert "stored in this workspace (draft), same content" in human.output
    assert (
        "import-costs estimate (MK passenger_car, " in human.output
        and f"indicative_preview, {LABEL}" in human.output
    )
    rows = json.loads(
        run_cli("tax-rules", "list", "--workspace", str(workspace), "--json", env=db_env).output
    )
    stored_rows = [r for r in rows["rule_sets"] if r["origin"] == "stored"]
    assert [(r["rule_set"], r["status"], r["sha256"]) for r in stored_rows] == [
        (LABEL, "draft", compute_rule_set_sha256(mk_draft()))
    ]
    other = run_cli(
        "tax-rules", "list", "--workspace", str(workspace), "--jurisdiction", "XX", "--json", env=db_env
    )
    assert json.loads(other.output)["rule_sets"] == []


def test_store_refuses_different_content_under_a_stored_version(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    owner = _owner(seed, workspace)
    first = run_cli(
        "tax-rules", "store", str(DRAFT_FILE), "--workspace", str(workspace), "--owner-user-id", str(owner),
        "--yes", env=db_env,
    )  # fmt: skip
    assert first.exit_code == 0, first.output
    edited = json.loads(DRAFT_FILE.read_text(encoding="utf-8"))
    edited["description"] = "SYNTHETIC edited copy with the same id and version"
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
    second = run_cli(
        "tax-rules", "store", str(path), "--workspace", str(workspace), "--owner-user-id", str(owner),
        "--yes", env=db_env,
    )  # fmt: skip
    assert second.exit_code == 1 and "already stored with DIFFERENT content" in second.output
    assert len(_stored(seed, workspace)) == 1
    listed = json.loads(
        run_cli("tax-rules", "list", "--workspace", str(workspace), "--json", env=db_env).output
    )
    assert listed["calculator"]["mode"] == "indicative_preview"  # the stored copy equals the file
