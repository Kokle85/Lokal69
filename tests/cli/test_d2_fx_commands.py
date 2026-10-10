"""OPS-06 (wave D2): ``suv-deals fx record | status | refresh`` (marker ``db`` unless noted).

The owner can store a rate by hand (audited, exact decimals, never as ``ECB``), see the newest
stored rate and its age, and ``fx refresh`` refuses while ``FX_FETCH_ENABLED`` is off (nothing is
fetched). SYNTHETIC values only; no network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import pytest
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

pytestmark = pytest.mark.db


def _record(run_cli: Cli, env: dict[str, str], workspace: UUID, *extra: str) -> object:
    return run_cli(
        "fx",
        "record",
        "--workspace",
        str(workspace),
        "--base",
        "EUR",
        "--quote",
        "CHF",
        "--rate",
        "0.9412",
        "--date",
        "2026-10-02",
        "--source-ref",
        "SYNTHETIC bank statement 4711",
        "--reason",
        "ECB fetch not yet enabled",
        *extra,
        env=env,
    )


def test_fx_record_stores_an_audited_owner_rate_once(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed
) -> None:
    refused = _record(run_cli, db_env, workspace)
    assert refused.exit_code == 3  # --yes required
    done = _record(run_cli, db_env, workspace, "--yes")
    assert done.exit_code == 0, done.output
    assert "recorded" in done.output
    row = seed.conn.execute(
        "select base, quote, rate = 0.9412, rate_date::text, provider, purpose, source_ref, is_fixture"
        " from app.fx_rates where workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert row == [
        ("EUR", "CHF", True, "2026-10-02", "owner", "reference", "SYNTHETIC bank statement 4711", False)
    ]
    audits = seed.conn.execute(
        "select (metadata ->> 'rate')::numeric = 0.9412, reason from ops.audit_events"
        " where workspace_id = %s and action = 'fx_rate.record'",
        (workspace,),
    ).fetchall()
    assert audits == [(True, "ECB fetch not yet enabled")]
    again = _record(run_cli, db_env, workspace, "--yes")
    assert again.exit_code == 0 and "already recorded" in again.output
    conflicting = run_cli(
        "fx", "record", "--workspace", str(workspace), "--base", "EUR", "--quote", "CHF", "--rate", "0.95",
        "--date", "2026-10-02", "--source-ref", "other", "--reason", "typo test", "--yes", env=db_env,
    )  # fmt: skip
    assert conflicting.exit_code != 0  # never overwrites a stored observation
    status = run_cli("fx", "status", "--workspace", str(workspace), "--json", env=db_env)
    assert status.exit_code == 0, status.output
    data = json.loads(status.output)
    assert data["fx_fetch_enabled"] is False
    assert [(r["pair"], Decimal(r["rate"]), r["provider"]) for r in data["rates"]] == [
        ("EUR/CHF", Decimal("0.9412"), "owner")
    ]
    assert data["rates"][0]["age_days"] >= 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--rate", "0,94"), "--rate must be a positive decimal"),
        (("--rate", "0"), "--rate must be positive"),
        (("--provider", "ECB"), "ECB is reserved"),
        (("--quote", "EUR"), "--base and --quote must differ"),
        (("--date", "2999-01-01"), "must not be in the future"),
    ],
)
def test_fx_record_refuses_bad_input(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, args: tuple[str, ...], message: str
) -> None:
    base = {
        "--base": "EUR",
        "--quote": "CHF",
        "--rate": "0.9412",
        "--date": "2026-10-02",
        "--provider": "owner",
        "--source-ref": "SYNTHETIC",
        "--reason": "bad input test",
    }
    base.update(dict(zip(args[::2], args[1::2], strict=True)))
    flat = [item for pair in base.items() for item in pair]
    result = run_cli("fx", "record", "--workspace", str(workspace), *flat, "--yes", env=db_env)
    assert result.exit_code == 2 and message in result.output


@pytest.mark.parametrize("enabled", ["false", ""])
def test_fx_refresh_refuses_while_fetching_is_disabled(
    run_cli: Cli, db_env: dict[str, str], enabled: str
) -> None:
    env = {**db_env, "FX_FETCH_ENABLED": enabled} if enabled else db_env
    result = run_cli("fx", "refresh", "--yes", env=env)
    assert result.exit_code == 1 and "FX_FETCH_ENABLED=false" in result.output
