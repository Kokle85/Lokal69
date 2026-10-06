"""Audit trail redaction and activation gates on real PostgreSQL (spec 11, 24, 32)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from tests.integration.db.helpers import Seed, World
from tests.integration.persistence_core.support import member, system

from suv_deals.domain.enums import GateStatus, Role
from suv_deals.errors import Forbidden, ValidationFailed
from suv_deals.observability.audit import AuditAction
from suv_deals.persistence import audit, gates
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import mapped_errors

pytestmark = pytest.mark.db

SECRET_BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJlLXN5bnRoZXRpYw"


async def test_audit_events_are_redacted_and_bound_to_the_verified_actor(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = member(ws, Role.REVIEWER, kind="mcp_client")
    target = uuid.uuid4()
    async with db.transaction(actor) as conn:
        audit_id = await audit.record(
            conn,
            actor,
            AuditAction.REVIEW_SUBMIT,
            "review_case",
            target,
            3,
            4,
            f"seller asked to call +49 171 1234567, token={SECRET_BEARER}",
            {
                "api_key": "sk-live-SyntheticKey1234567890abcdEFGH",
                "callback_url": "https://hooks.example.invalid/secret-path",
                "note": f"Authorization: {SECRET_BEARER}",
                "contact": "seller@example.com",
                "actor_principal_id": "spoofed-owner",
                "outcome_codes": ["SYNTHETIC"],
            },
        )
    row = seed.conn.execute(
        "select actor_principal_id, actor_kind, actor_role, action, target_type, target_id,"
        " prior_version, new_version, reason, request_id, metadata::text"
        " from ops.audit_events where id = %s",
        (audit_id,),
    ).fetchone()
    assert row is not None
    principal, kind, role, action, target_type, target_id, prior, new, reason, request_id, metadata = row
    assert (principal, kind, role) == (actor.principal_id, "mcp_client", "reviewer")
    assert (action, target_type, target_id, prior, new) == ("review.submit", "review_case", target, 3, 4)
    assert request_id == actor.request_id
    for leaked in ("eyJhbGci", "sk-live-SyntheticKey", "secret-path", "seller@example.com", "171 1234567"):
        assert leaked not in reason and leaked not in metadata
    assert '"outcome": "succeeded"' in metadata and "SYNTHETIC" in metadata
    # The trail is append-only for every role (SV001), and suv_backend has no UPDATE grant at all.
    with pytest.raises(psycopg.Error, match=r"append-only|SV001"):
        seed.conn.execute("update ops.audit_events set reason = 'x' where id = %s", (audit_id,))
    with pytest.raises(Forbidden):
        async with mapped_errors(), db.transaction(actor) as conn:
            await conn.execute("update ops.audit_events set reason = 'x' where id = %s", (audit_id,))
    owner = member(ws, Role.OWNER)
    async with db.transaction(owner) as conn:
        [listed] = await audit.list_for_target(conn, owner, "review_case", target)
        with pytest.raises(Forbidden):
            await audit.list_for_target(conn, actor, "review_case", target)
    assert listed.id == audit_id and listed.metadata["api_key"] == "[REDACTED]"


async def test_audit_rejects_malformed_names(db: Database, world_a: World) -> None:
    actor = system(world_a.workspace_id)
    async with db.transaction(actor) as conn:
        with pytest.raises(ValidationFailed):
            await audit.record(conn, actor, "Drop Table", "job", None)
        with pytest.raises(ValidationFailed):
            await audit.record(conn, actor, "job.cancel", "Job; --", None)
        with pytest.raises(ValidationFailed):
            await audit.record(conn, actor, "job.cancel", "job", None, prior_version=-1)


async def test_spec_gates_are_seeded_honestly_and_never_overwritten(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    owner = member(ws, Role.OWNER)
    reviewer = member(ws, Role.REVIEWER)
    async with db.transaction(reviewer) as conn:
        with pytest.raises(Forbidden):
            await gates.seed_spec_gates(conn, reviewer)
    async with db.transaction(owner) as conn:
        created = await gates.seed_spec_gates(conn, owner)
    assert len(created) == len(gates.SPEC_GATES) == 14
    statuses = {g.capability: g.status for g in created}
    assert set(statuses.values()) <= {GateStatus.IMPLEMENTED, GateStatus.BLOCKED, GateStatus.NOT_REQUESTED}
    assert statuses["tax_rules"] == GateStatus.BLOCKED
    assert statuses["slack_destination"] == GateStatus.NOT_REQUESTED
    # Progress recorded later is never overwritten by a re-seed.
    async with db.transaction(owner) as conn:
        updated = await gates.upsert_gate(
            conn,
            owner,
            capability="supabase",
            dependency="Approved Supabase project and server credentials",
            required_evidence="schema/RLS tests on the real project",
            status=GateStatus.INTEGRATION_VERIFIED,
            evidence={"suite": "tests/integration", "service_role_key": "sb_secret_SYNTHETIC123456"},
        )
        assert await gates.seed_spec_gates(conn, owner) == []
    assert updated.row_version == 2 and updated.evidence["service_role_key"] == "[REDACTED]"
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(viewer) as conn:
        listed = await gates.list_gates(conn, viewer)
    assert {g.capability: g.status for g in listed}["supabase"] == GateStatus.INTEGRATION_VERIFIED
    assert [g.capability for g in listed] == sorted(g.capability for g in listed)
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events"
            " where workspace_id = %s and action like 'activation_gate.%%'",
            (ws,),
        )
        == 15
    )


async def test_live_and_active_gates_require_evidence(db: Database, world_a: World) -> None:
    owner = member(world_a.workspace_id, Role.OWNER)
    async with db.transaction(owner) as conn:
        for status in (GateStatus.ACTIVE, GateStatus.LIVE_VERIFIED):
            with pytest.raises(ValidationFailed):
                await gates.upsert_gate(
                    conn,
                    owner,
                    capability="native_mcp_events",
                    dependency="dot support",
                    required_evidence="canary",
                    status=status,
                )
        record = await gates.upsert_gate(
            conn,
            owner,
            capability="native_mcp_events",
            dependency="dot support",
            required_evidence="canary",
            status=GateStatus.LIVE_VERIFIED,
            evidence={"canary_event_id": str(uuid.uuid4())},
        )
    assert record.checked_at is not None and record.status == GateStatus.LIVE_VERIFIED
