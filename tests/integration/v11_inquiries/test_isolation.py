"""Workspace isolation of the inquiry path (RLS as ``suv_backend``; spec 37.8, 37.10).

Another workspace's system principal can neither read, reserve, dispatch, report on, suppress
nor merge the inquiry records of this workspace: every lookup is scoped by the transaction's
workspace GUC and the row-level security policy, so foreign ids are simply not found.
"""

from __future__ import annotations

import pytest
from tests.integration.v11_inquiries.support import (
    World,
    accepted,
    dispatch,
    lease,
    readiness,
    reserve_and_queue,
    system,
)

from suv_deals.errors import AppError, NotFound
from suv_deals.persistence import inquiries_repo, sellers_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def test_other_workspace_cannot_see_or_touch_the_inquiry(
    db: Database, world: World, world_b: World
) -> None:
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None
    other = system(world_b.workspace_id)

    async def refused(operation: object) -> None:
        with pytest.raises(AppError):
            async with unit_of_work(db, other) as conn:
                await operation(conn)  # type: ignore[operator]

    with pytest.raises(NotFound):
        async with unit_of_work(db, other) as conn:
            await inquiries_repo.get_inquiry(conn, other, record.id)
    await refused(lambda conn: inquiries_repo.get_attempt(conn, other, result.attempt.attempt_id))  # type: ignore[union-attr]
    await refused(lambda conn: inquiries_repo.queue(conn, other, record.id))
    await refused(
        lambda conn: inquiries_repo.dispatch(
            conn, other, record.id, lease=lease(), message_approval_required=False
        )
    )
    await refused(
        lambda conn: inquiries_repo.record_outcome(
            conn,
            other,
            attempt_id=result.attempt.attempt_id,  # type: ignore[union-attr]
            lease_token=result.attempt.lease_token,  # type: ignore[union-attr]
            outcome=accepted(result),
        )
    )
    await refused(lambda conn: sender_bindings_repo.get_binding(conn, other, world.sender_binding_id))
    await refused(lambda conn: sellers_repo.get_contact(conn, other, world.vehicle.contact_id))
    await refused(
        lambda conn: inquiries_repo.add_suppression(
            conn,
            other,
            scope="seller",
            key=inquiries_repo.seller_suppression_key(world.seller_entity_id),
            reason="manual",
        )
    )
    await refused(
        lambda conn: sellers_repo.merge_sellers(
            conn,
            other,
            survivor_id=world_b.seller_entity_id,
            absorbed_id=world.seller_entity_id,
            reason="cross-workspace merge attempt",
        )
    )
    # The other workspace's own view is untouched by this workspace's quota and inquiries.
    async with unit_of_work(db, other) as conn:
        usage = await inquiries_repo.quota_usage(conn, other)
    assert usage.count_24h == 0
    _, decision = await readiness(db, world_b)
    assert decision.can_reserve_now
