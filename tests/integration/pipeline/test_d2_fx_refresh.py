"""OPS-06 (wave D2): ECB reference rates are actually fetched and stored, behind FX_FETCH_ENABLED.

Before D2 ``fetch_ecb_daily`` and ``upsert_fx_rate`` had no caller, so the advertised switch was
inert and every CH listing stayed ``needs_facts`` (``fx:CHF/EUR``). Now:

- ``refresh_ecb_rates`` refuses without any request while the switch is off; with it on it
  fetches the file ONCE and records the EUR->CHF reference rate in each workspace, idempotently,
  with provenance (URL + sha256) and an audit event;
- the reconciler runs it at most every 6 hours (a failure waits 1 hour and never stops a pass);
  with the switch off it never builds or calls a client.

The HTTP client is an in-memory fake serving the SYNTHETIC ECB-format fixture: no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.integration.pipeline.support import PipelineEnv

from suv_deals.errors import AppError, ErrorCode
from suv_deals.workers import fx_refresh
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler

pytestmark = pytest.mark.db

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "fx" / "ecb_daily_synthetic.xml"


@dataclass
class _Response:
    status_code: int
    body: bytes
    truncated: bool = False


class FakeEcb:
    """Serves the SYNTHETIC ECB-format file; counts calls; can fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def get(self, url: str, **_: Any) -> _Response:
        self.calls.append(url)
        if self.fail:
            return _Response(503, b"")
        return _Response(200, FIXTURE.read_bytes())


def _chf_rows(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select base, quote, rate, rate_date, provider, purpose, source_ref, is_fixture from app.fx_rates"
        " where workspace_id = %s and quote = 'CHF'",
        env.workspace_id,
    )


def _enabled(env: PipelineEnv) -> Any:
    return env.ctx.settings.model_copy(update={"fx_fetch_enabled": True})


async def test_refresh_refuses_without_a_request_while_disabled(env: PipelineEnv) -> None:
    client = FakeEcb()
    with pytest.raises(AppError) as caught:
        await fx_refresh.refresh_ecb_rates(env.ctx.settings, env.ctx.db, client, [env.workspace_id])
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert client.calls == [] and _chf_rows(env) == []


async def test_refresh_records_the_chf_reference_rate_once_with_provenance(env: PipelineEnv) -> None:
    client = FakeEcb()
    result = await fx_refresh.refresh_ecb_rates(_enabled(env), env.ctx.db, client, [env.workspace_id])
    assert len(client.calls) == 1 and client.calls[0].startswith("https://www.ecb.europa.eu/")
    assert result.recorded == {env.workspace_id: 1}
    rows = _chf_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert (row["base"], row["quote"], row["provider"], row["purpose"]) == ("EUR", "CHF", "ECB", "reference")
    assert row["rate"] == Decimal("0.94") and row["rate_date"] == date(2026, 10, 5)
    assert row["is_fixture"] is False and f"sha256={result.sha256}" in row["source_ref"]
    assert (
        env.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s and action = 'fx_rate.record'",
            env.workspace_id,
        )
        == 1
    )
    # Only the CH market's currency is recorded (no USD/JPY/... rows, never an MKD rate).
    assert (
        env.scalar(
            "select count(*) from app.fx_rates where workspace_id = %s and provider = 'ECB'", env.workspace_id
        )
        == 1
    )
    again = await fx_refresh.refresh_ecb_rates(_enabled(env), env.ctx.db, FakeEcb(), [env.workspace_id])
    assert again.recorded == {env.workspace_id: 0} and len(_chf_rows(env)) == 1


async def test_reconciler_refreshes_at_most_every_six_hours(env: PipelineEnv) -> None:
    env.ctx.settings = _enabled(env)
    client = FakeEcb()
    reconciler = Reconciler(env.ctx, ReconcileOptions(), fx_refresher=fx_refresh.FxRefresher(client))
    reports = {r.workspace_id: r for r in await reconciler.run_once()}
    assert reports[env.workspace_id].fx_rates_recorded == 1
    assert len(_chf_rows(env)) == 1
    await reconciler.run_once()
    assert len(client.calls) == 1  # not due again within REFRESH_EVERY


async def test_reconciler_reports_a_failed_fetch_and_carries_on(env: PipelineEnv) -> None:
    env.ctx.settings = _enabled(env)
    client = FakeEcb(fail=True)
    reconciler = Reconciler(env.ctx, ReconcileOptions(), fx_refresher=fx_refresh.FxRefresher(client))
    report = next(r for r in await reconciler.run_once() if r.workspace_id == env.workspace_id)
    assert "fx:DEPENDENCY_UNAVAILABLE" in report.errors
    assert report.gates_seeded > 0  # the rest of the pass ran
    assert _chf_rows(env) == []


async def test_reconciler_never_fetches_while_disabled(env: PipelineEnv) -> None:
    client = FakeEcb()
    reconciler = Reconciler(env.ctx, ReconcileOptions(), fx_refresher=fx_refresh.FxRefresher(client))
    await reconciler.run_once()
    assert client.calls == [] and _chf_rows(env) == []
