"""Shared helpers for the persistence-core integration tests (synthetic data only)."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from tests.integration.db.helpers import Seed, unique

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext, PrincipalKind
from suv_deals.domain.enums import JobType, Role, Scope
from suv_deals.persistence.jobs import JobSpec


def system(workspace_id: UUID) -> ActorContext:
    return ActorContext.system(workspace_id, request_id=f"test-{uuid.uuid4().hex[:12]}")


def member(
    workspace_id: UUID,
    role: Role = Role.OWNER,
    *,
    principal_id: UUID | None = None,
    kind: PrincipalKind = "user",
    scopes: frozenset[Scope] | None = None,
) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=principal_id or uuid.uuid4(),
        principal_kind=kind,
        role=role,
        scopes=ROLE_SCOPES[role] if scopes is None else scopes,
        request_id=f"test-{uuid.uuid4().hex[:12]}",
    )


def spec(job_type: JobType = JobType.VALUATION, **kwargs: Any) -> JobSpec:
    values: dict[str, Any] = {"job_type": job_type, "dedup_key": unique("job")}
    values.update(kwargs)
    return JobSpec(**values)


def expire_job_lease(seed: Seed, job_id: UUID) -> None:
    """Simulate time passing: the job's lease is already expired in database time."""
    seed.conn.execute(
        "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 second' where id = %s",
        (job_id,),
    )


def expire_event_lease(seed: Seed, outbox_id: UUID) -> None:
    seed.conn.execute(
        "update ops.outbox set lease_expires_at = clock_timestamp() - interval '1 second' where id = %s",
        (outbox_id,),
    )


def job_row(seed: Seed, job_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select state, attempts, lease_token, lease_owner, lease_expires_at, last_error_code,"
        " last_error_detail, blocker_code, result_reference, completed_at, payload_version"
        " from ops.jobs where id = %s",
        (job_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))
