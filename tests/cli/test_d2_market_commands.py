"""F2 (wave D2): ``suv-deals market import`` records owner MK evidence (marker ``db``).

Asking prices are recorded by the operator CLI acting as owner; an owner estimate needs an active
owner (``--owner-user-id``) and names that user in ``recorded_by``. Each row and the batch are
audited, a re-import records nothing new, ``--dry-run`` writes nothing. SYNTHETIC data only.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

pytestmark = pytest.mark.db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "market" / "asking_prices_synthetic.json"


def _import(run_cli: Cli, env: dict[str, str], workspace: UUID, path: Path, kind: str, *extra: str) -> object:
    return run_cli(
        "market", "import", str(path), "--workspace", str(workspace), "--evidence-kind", kind,
        "--reason", "owner research (synthetic)", "--json", *extra, env=env,
    )  # fmt: skip


def _rows(seed: Seed, workspace: UUID) -> list[tuple[object, ...]]:
    return seed.conn.execute(
        "select evidence_kind, market, currency, recorded_by, is_fixture, evidence ->> 'method'"
        " from app.market_observations where workspace_id = %s order by amount_minor",
        (workspace,),
    ).fetchall()


def test_market_import_records_asking_prices_once(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    refused = _import(run_cli, db_env, workspace, FIXTURE, "asking_price")
    assert refused.exit_code == 3  # --yes required
    dry = _import(run_cli, db_env, workspace, FIXTURE, "asking_price", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["would_create"] == 4 and _rows(seed, workspace) == []
    done = _import(run_cli, db_env, workspace, FIXTURE, "asking_price", "--yes")
    assert done.exit_code == 0, done.output
    report = json.loads(done.output)
    assert (report["created"], report["already_recorded"]) == (4, 0)
    rows = _rows(seed, workspace)
    assert rows == [("asking_price", "MK", "EUR", None, False, "owner_import")] * 4
    audits = seed.conn.execute(
        "select action, count(*) from ops.audit_events where workspace_id = %s"
        " and action in ('market.import', 'market.observation_record') group by action order by action",
        (workspace,),
    ).fetchall()
    assert audits == [("market.import", 1), ("market.observation_record", 4)]
    again = _import(run_cli, db_env, workspace, FIXTURE, "asking_price", "--yes")
    assert again.exit_code == 0 and json.loads(again.output)["created"] == 0
    assert len(_rows(seed, workspace)) == 4


def test_market_import_refuses_a_kind_mismatch_and_bad_files(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    mismatch = _import(
        run_cli, db_env, workspace, FIXTURE, "owner_estimate", "--owner-user-id", str(seed.user())
    )
    assert mismatch.exit_code == 1 and "--evidence-kind is owner_estimate" in mismatch.output
    contact = tmp_path / "contact.json"
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["observations"][0]["seller_phone"] = "+389 70 123 456"
    contact.write_text(json.dumps(document), encoding="utf-8")
    refused = _import(run_cli, db_env, workspace, contact, "asking_price", "--yes")
    assert refused.exit_code == 1 and "observations.0" in refused.output
    assert "+389" not in refused.output  # field names only, never values
    assert _rows(seed, workspace) == []


def test_owner_estimates_need_an_active_owner(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["evidence_kind"] = "owner_estimate"
    for row in document["observations"]:
        del row["url"]
    estimates = tmp_path / "estimates.json"
    estimates.write_text(json.dumps(document), encoding="utf-8")
    without = _import(run_cli, db_env, workspace, estimates, "owner_estimate", "--yes")
    assert without.exit_code == 2 and "--owner-user-id" in without.output
    stranger = seed.user()
    not_owner = _import(
        run_cli, db_env, workspace, estimates, "owner_estimate", "--owner-user-id", str(stranger), "--yes"
    )
    assert not_owner.exit_code == 1
    owner = seed.user()
    seed.membership(workspace, owner, "owner")
    done = _import(
        run_cli, db_env, workspace, estimates, "owner_estimate", "--owner-user-id", str(owner), "--yes"
    )
    assert done.exit_code == 0, done.output
    rows = _rows(seed, workspace)
    assert {r[0] for r in rows} == {"owner_estimate"} and {r[3] for r in rows} == {owner}
