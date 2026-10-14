"""``suv-deals import-costs estimate | values | record-values | tax-rules list | tax-rules store`` on
a real database (marker ``db``).

The calculator runs as the operator's system actor (``deals:read``) through the shared read
service and writes nothing; ``tax-rules store`` stores the shipped DRAFT file as the verified
ACTIVE owner (audited) and never approves or activates it. ``import-costs record-values`` records a
listing's real purchase price and estimated customs value as the verified ACTIVE owner (audited,
superseding, valuations invalidated) and ``estimate`` / ``values`` then use and show them (owner
decision 2026-10-13: bought for EUR 2,800, SchwackeNet EUR 4,200). Every workspace, listing and rate is
SYNTHETIC; 61.5337 is the fact sheet's golden-case customs rate (test value only) and G01 the
golden total of the shipped draft. Where a test needs ACTIVE rules, the draft is activated through
the real owner transitions in a throwaway test workspace only.
"""

from __future__ import annotations

import asyncio
import json
import re
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
#: Whole denars without a fraction (owner decision of 2026-10-13: amounts without decimals).
G01 = "168155"  # D1: diesel, NEDC 189 (COC), EUR 3,000 + EUR 700 transport, no proof of origin
LABEL = "mk-passenger-car-import@2026.10-draft.3"
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
    assert legal["name"] == "primary" and legal["total"] is None
    assert legal["indicative_total"]["amount"] == G01
    assert data["customs_fx_source"] == "caller_supplied"
    assert data["banner"]["mk"].startswith("ИНДИКАТИВНА ПРОЦЕНКА")
    assert {w["code"] for w in result["warnings"]} >= {"TAX_RULES_UNAPPROVED", "SELLER_CLAIMS_UNVERIFIED"}
    human = _estimate(run_cli, db_env, workspace, *FACTS, "--customs-eur-mkd-rate", CUSTOMS_RATE)
    assert human.exit_code == 0, human.output
    assert "!! INDICATIVE PREVIEW" in human.output and "mode: indicative_preview" in human.output
    assert f"{G01} MKD  (2733 EUR)" in human.output and "indicative_total" in human.output
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
    assert "real purchase price       : 3000 EUR (advertised)  [listing]" in human.output
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


# --------------------------------------------------------------------------- values / record-values

PAID = ("--purchase-price", "2800", "--purchase-kind", "actual", "--purchase-source", "seller invoice")
SCHWACKE = (
    "--estimated-customs-value-eur",
    "4200",
    "--estimated-customs-value-source",
    "SchwackeNet catalogue",
)


def _plain(output: str) -> str:
    """The output with each label's padding collapsed (``label : value``)."""
    return re.sub(r"(?<=\S) {2,}: ", " : ", output)


def _import_values(seed: Seed, workspace: UUID) -> list[tuple[Any, ...]]:
    return seed.conn.execute(
        "select category, kind, base_minor, currency, provider, document_ref, recorded_by, supersedes_id"
        " from app.cost_evidence where workspace_id = %s and category in ('purchase', 'customs_value')"
        " order by created_at, category = 'customs_value', id",  # one call's rows share a time
        (workspace,),
    ).fetchall()


def test_the_owner_records_both_values_and_the_estimate_uses_them(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, source: tuple[UUID, str]
) -> None:
    listing = add_listing(seed, workspace, source[0], source[1])  # advertised EUR 3,000
    _record_customs_rate(run_cli, db_env, workspace)
    record = ("import-costs", "record-values", "--workspace", str(workspace), "--listing-id", str(listing))
    refused = run_cli(*record, "--owner-user-id", str(seed.user()), *PAID, env=db_env)
    assert refused.exit_code == 3 and "re-run with --yes" in refused.output
    stranger = run_cli(*record, "--owner-user-id", str(seed.user()), *PAID, "--yes", env=db_env)
    assert stranger.exit_code == 1 and "not an active owner" in stranger.output
    reviewer = seed.user()
    seed.membership(workspace, reviewer, "reviewer")
    not_owner = run_cli(*record, "--owner-user-id", str(reviewer), *PAID, "--yes", env=db_env)
    assert not_owner.exit_code == 1 and "not an active owner" in not_owner.output
    assert _import_values(seed, workspace) == []

    owner = _owner(seed, workspace)
    recorded = run_cli(
        *record, "--owner-user-id", str(owner), *PAID, *SCHWACKE, "--document-ref", "INV-SYNTHETIC-1",
        "--yes", env=db_env,
    )  # fmt: skip
    assert recorded.exit_code == 0, recorded.output
    out = _plain(recorded.output)
    assert f"Recorded for listing {listing} (owner {owner}; audited import_values.record):" in out
    assert "real purchase price : 2800 EUR  (seller invoice, the first value of its kind)" in out
    assert "estimated customs value : 4200 EUR  (SchwackeNet catalogue, the first value of its kind)" in out
    assert "valuations marked stale : 0" in out and "recomputation queued : job " in out
    rows = _import_values(seed, workspace)
    assert [r[:7] for r in rows] == [
        ("purchase", "actual", 280000, "EUR", "seller invoice", "INV-SYNTHETIC-1", owner),
        ("customs_value", "estimate", 420000, "EUR", "SchwackeNet catalogue", "INV-SYNTHETIC-1", owner),
    ]
    audits = seed.conn.execute(
        "select actor_principal_id, actor_kind from ops.audit_events"
        " where workspace_id = %s and action = 'import_values.record'",
        (workspace,),
    ).fetchall()
    assert audits == [(owner, "user")]
    jobs = seed.scalar(
        "select count(*) from ops.jobs where workspace_id = %s and listing_id = %s"
        " and job_type = 'valuation'",
        (workspace, listing),
    )
    assert jobs == 1  # the listing's valuation is recomputed with the recorded values

    # The calculator's listing mode uses both recorded values automatically.
    args = ("--listing-id", str(listing), "--co2-document", "coc", "--declaration-date", "2026-10-13")
    raw = _estimate(run_cli, db_env, workspace, *args, "--json")
    assert raw.exit_code == 0, raw.output
    data = json.loads(raw.output)["data"]
    totals = {
        s["name"]: (s["indicative_total"]["amount"], s["indicative_landed_cost_eur"]["amount"])
        for s in data["scenarios"]
    }
    assert totals == {"primary": ("185803", "6520"), "transaction_value": ("165213", "6185")}
    assert data["purchase_price"]["basis"] == "recorded_actual"
    assert data["customs_value_for_duties"]["basis"] == "estimated"
    sources = {s["fact"]: s["source"] for s in data["fact_sources"]}
    assert sources["price"] == "recorded" and sources["estimated_customs_value_eur"] == "recorded"
    human = _estimate(run_cli, db_env, workspace, *args)
    assert human.exit_code == 0, human.output
    text = _plain(human.output)
    assert "real purchase price : 2800 EUR (recorded_actual)  [recorded]" in text  # the facts used
    assert "real purchase price : 2800 EUR  (recorded_actual: recorded actual purchase price" in text
    assert "customs value for duties : 4900 EUR  (estimated: recorded estimated customs value" in text
    assert "estimated customs value : 4200 EUR, without transport (SchwackeNet catalogue)  [recorded]" in text
    assert "RECORDED_PURCHASE_PRICE_USED" in text and "RECORDED_CUSTOMS_VALUE_USED" in text
    # A value given here replaces the recorded one for this estimate only.
    override = _estimate(
        run_cli, db_env, workspace, *args, "--price", "3000", "--price-basis", "invoice", "--json"
    )
    odata = json.loads(override.output)["data"]
    assert odata["purchase_price"]["basis"] == "invoice"
    assert any(n.startswith("RECORDED_PURCHASE_PRICE_OVERRIDDEN") for n in odata["notes"])

    # values shows the current values; a new value supersedes the old one.
    shown = run_cli(
        "import-costs", "values", "--workspace", str(workspace), "--listing-id", str(listing), env=db_env
    )
    assert shown.exit_code == 0, shown.output
    text = _plain(shown.output)
    assert f"IMPORT VALUES  listing {listing}" in text
    assert (
        "real purchase price : 2800 EUR  (actual; source: seller invoice; document INV-SYNTHETIC-1;" in text
    )
    assert f"by {owner})" in text
    assert (
        "estimated customs value : 4200 EUR  (estimate, without transport; source: SchwackeNet catalogue;"
        in text
    )
    first_purchase = rows[0]
    again = run_cli(
        *record, "--owner-user-id", str(owner), "--purchase-price", "2750.50", "--purchase-kind", "quote",
        "--purchase-source", "seller offer", "--yes", "--json", env=db_env,
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    result = json.loads(again.output)["data"]
    assert len(result["recorded_ids"]) == 1 and len(result["superseded_ids"]) == 1
    assert result["values"]["purchase_price"]["amount"]["amount"] == "2751"  # whole euros, half-up
    assert result["values"]["purchase_price"]["amount_exact"] == "2750.50"
    assert result["values"]["estimated_customs_value"]["source"] == "SchwackeNet catalogue"
    assert len(_import_values(seed, workspace)) == 3 and first_purchase[7] is None
    listed = run_cli(
        "import-costs",
        "values",
        "--workspace",
        str(workspace),
        "--listing-id",
        str(listing),
        "--json",
        env=db_env,
    )
    view = json.loads(listed.output)["data"]
    assert view["purchase_price"]["kind"] == "quote"
    history = [(h["category"], h["kind"], h["current"]) for h in view["history"]]
    # Newest first; the first call's two rows share one transaction time (either order).
    assert history[0] == ("purchase", "quote", True)
    assert sorted(history[1:]) == [("customs_value", "estimate", True), ("purchase", "actual", False)]
    text = run_cli(
        "import-costs", "values", "--workspace", str(workspace), "--listing-id", str(listing), env=db_env
    ).output
    assert "2751 EUR (exact 2750.50)" in text and "superseded" in text


def test_import_values_of_a_missing_or_foreign_listing_are_not_found(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    owner = _owner(seed, workspace)
    other = seed.workspace("CLI foreign workspace")
    try:
        key = unique("synthetic_src").lower()
        foreign_source = seed.source(other, source_key=key, mode="public_html", adapter="synthetic_adapter")
        foreign = add_listing(seed, other, foreign_source, key)
        for listing in (uuid4(), foreign):
            recorded = run_cli(
                "import-costs", "record-values", "--workspace", str(workspace), "--listing-id", str(listing),
                "--owner-user-id", str(owner), *SCHWACKE, "--yes", env=db_env,
            )  # fmt: skip
            assert recorded.exit_code == 1 and "NOT_FOUND" in recorded.output, recorded.output
            shown = run_cli(
                "import-costs",
                "values",
                "--workspace",
                str(workspace),
                "--listing-id",
                str(listing),
                env=db_env,
            )
            assert shown.exit_code == 1 and "NOT_FOUND" in shown.output, shown.output
        assert _import_values(seed, workspace) == [] and _import_values(seed, other) == []
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (other,))


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
    assert _stored(seed, workspace) == [("draft", "2026.10-draft.3", owner)]
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


def _production_line(output: str, workspace: UUID) -> str:
    """The doctor's ``tax_rules/production`` line of ``workspace`` (prefixed by its id when the
    database holds several active workspaces)."""
    lines = [ln for ln in output.splitlines() if "tax_rules/" in ln and "production" in ln]
    mine = [ln for ln in lines if f"tax_rules/{str(workspace)[:8]}:production" in ln]
    plain = [ln for ln in lines if "tax_rules/production" in ln]
    found = mine or plain
    assert len(found) == 1, output
    return found[0]


def test_doctor_reports_the_stored_active_rule_set(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID
) -> None:
    """Regression: doctor judged production readiness from files only, so after the owner
    activated the MK set in the database it still said "no approved/active rule set"."""
    before = run_cli("doctor", "--process", "scheduler", env=db_env)
    line = _production_line(before.output, workspace)
    assert "no ACTIVE stored rule set applies for MK/passenger_car" in line
    _run(db_url, lambda db: activate(db, owner_actor(workspace), mk_draft()))
    after = run_cli("doctor", "--process", "scheduler", env=db_env)
    line = _production_line(after.output, workspace)
    assert line.lstrip().startswith("OK") and f"ACTIVE {LABEL}" in line, after.output
