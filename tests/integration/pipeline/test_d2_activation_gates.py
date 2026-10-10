"""F4 (wave D2): the spec 32 v1.1 gates exist and every workspace actually gets the gate list.

- ``SPEC_GATES`` holds all 16 spec 32 rows (``seller_email_sender`` and ``seller_inquiry`` were
  missing) plus ``seller_reply_slack_route``: the private Slack signal is the owner-selected
  seller-reply route (spec 37.6), so it is a BLOCKED requirement, not the ``not_requested``
  optional fallback that ``slack_destination`` (candidate discovery) stays;
- nothing in production seeded the gate list before (only tests did), so ``deals_health`` and the
  dashboard Overview showed source-access blocks only. The reconciler now inserts the missing spec
  gates of every active workspace on each pass, never overwriting a gate that already exists
  (recorded progress survives), so the hosted workspace gets the three new gates too;
- the new gates are activation blockers (``deals_health``).

SYNTHETIC data only; nothing external is contacted.
"""

from __future__ import annotations

import pytest
from tests.integration.pipeline.support import PipelineEnv, run

from suv_deals.domain.enums import GateStatus
from suv_deals.persistence import gates
from suv_deals.persistence.database import Conn
from suv_deals.persistence.queries import operations
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler

pytestmark = pytest.mark.db

SPEC_32_ROWS = {
    "implementation_environment",
    "existing_crawler",
    "supabase",
    "source_access",
    "credentials_api_accounts",
    "tax_rules",
    "cost_assumptions",
    "contribution_threshold",
    "seller_email_sender",
    "seller_inquiry",
    "hosting",
    "mcp_authentication",
    "slack_destination",
    "native_mcp_events",
    "automatic_dot_activation",
    "production_notifications",
}
NEW_GATES = {"seller_email_sender", "seller_inquiry", "seller_reply_slack_route"}


def test_spec_gate_list_covers_every_spec_32_row_and_the_reply_route() -> None:
    by_name = {g.capability: g for g in gates.SPEC_GATES}
    assert set(by_name) == SPEC_32_ROWS | {"seller_reply_slack_route"}
    for name in NEW_GATES:
        assert by_name[name].status == GateStatus.BLOCKED, name
    assert "no message-approval gate" in by_name["seller_email_sender"].required_evidence
    assert "live test evidence" in by_name["seller_email_sender"].required_evidence
    assert "kill-switch" in by_name["seller_inquiry"].required_evidence
    assert "dot" in by_name["seller_reply_slack_route"].required_evidence
    # The candidate-discovery Slack fallback stays optional.
    assert by_name["slack_destination"].status == GateStatus.NOT_REQUESTED


async def _capabilities(env: PipelineEnv) -> dict[str, gates.GateRecord]:
    async def go(conn: Conn) -> list[gates.GateRecord]:
        return await gates.list_gates(conn, env.system)

    return {g.capability: g for g in await run(env.ctx, env.system, go)}


async def test_reconciler_seeds_missing_gates_without_overwriting(env: PipelineEnv) -> None:
    # An existing workspace with recorded progress on one gate and none of the others.
    async def progress(conn: Conn) -> gates.GateRecord:
        return await gates.upsert_gate(
            conn,
            env.system,
            "supabase",
            "Approved Supabase project and server credentials",
            "schema/RLS tests on the real project",
            GateStatus.INTEGRATION_VERIFIED,
            {"suite": "synthetic"},
        )

    recorded = await run(env.ctx, env.system, progress)
    before = await _capabilities(env)
    assert not NEW_GATES & set(before)

    report = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
    assert report.errors == []
    after = await _capabilities(env)
    assert {g.capability for g in gates.SPEC_GATES} <= set(after)
    assert report.gates_seeded == len(gates.SPEC_GATES) - 1  # all but the recorded one
    kept = after["supabase"]
    assert kept.status == GateStatus.INTEGRATION_VERIFIED and kept.row_version == recorded.row_version

    # A second pass seeds nothing; a dry run never writes.
    again = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
    assert again.gates_seeded == 0


async def test_dry_run_reports_but_does_not_seed(env: PipelineEnv) -> None:
    report = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id, dry_run=True)
    assert report.gates_seeded == len(gates.SPEC_GATES)
    assert not NEW_GATES & set(await _capabilities(env))


async def test_new_gates_are_activation_blockers_in_deals_health(env: PipelineEnv) -> None:
    await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
    result = await operations.health_view(env.ctx.db, env.owner, env.ctx.settings)
    blockers = {g.capability for g in result.data.activation_blockers}
    assert blockers >= NEW_GATES
    assert "slack_destination" not in blockers  # optional, not requested
