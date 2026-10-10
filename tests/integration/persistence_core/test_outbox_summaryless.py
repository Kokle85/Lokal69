"""REGRESSION (C1 item 10): ``outbox.claim_events`` never leased an event without a ``summary``.

The fixture-suspicion predicate tested ``jsonb_typeof(payload -> 'summary') = 'string' and ...``;
for a payload without ``summary`` that is NULL, the whole OR became NULL and ``not NULL`` dropped
the row from the claim - it was neither leased nor refused (category signals such as
``seller.reply.received`` never carry a summary, so the dispatcher needed a separate NULL-safe
claim). The predicate is now ``coalesce(..., false)``.
"""

from __future__ import annotations

import uuid

import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import system

from suv_deals.domain.enums import OutboxState
from suv_deals.domain.notifications import FIXTURE_BLOCKER, FIXTURE_SUMMARY_PREFIX
from suv_deals.persistence import outbox
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db


async def _enqueue(db: Database, world: World, payload: dict[str, object]) -> uuid.UUID:
    actor = system(world.workspace_id)
    async with db.transaction(actor) as conn:
        event_id, created = await outbox.enqueue_event(
            conn,
            actor,
            event_type="review.pending",
            event_version=1,
            aggregate_type="review_case",
            aggregate_id=uuid.uuid4(),
            aggregate_version=1,
            payload=payload,
            dedup_key=unique("summaryless"),
        )
    assert created
    return event_id


async def test_events_without_a_string_summary_are_claimed(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    no_summary = await _enqueue(db, world_a, {"schema_version": "1.0", "inquiry_id": str(uuid.uuid4())})
    numeric = await _enqueue(db, world_a, {"schema_version": "1.0", "summary": 42})
    null_summary = await _enqueue(db, world_a, {"schema_version": "1.0", "summary": None})
    claimed = await outbox.claim_events(db, ws, "dispatcher-c1", 60, 10)
    assert {e.event_id for e in claimed} == {no_summary, numeric, null_summary}
    assert all(e.state == OutboxState.SENDING for e in claimed)


async def test_a_fixture_summary_is_still_refused(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    contaminated = seed.outbox(
        ws, payload={"schema_version": "1.0", "summary": f"  {FIXTURE_SUMMARY_PREFIX.lower()} synthetic"}
    )
    plain = await _enqueue(db, world_a, {"schema_version": "1.0"})
    claimed = await outbox.claim_events(db, ws, "dispatcher-c1", 60, 10)
    assert [e.event_id for e in claimed] == [plain]
    assert seed.scalar("select state from ops.outbox where id = %s", (contaminated,)) == "blocked"
    assert (
        seed.scalar("select blocker_code from ops.outbox where id = %s", (contaminated,)) == FIXTURE_BLOCKER
    )
