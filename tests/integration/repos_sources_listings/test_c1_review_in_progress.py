"""C1 review (item 9): an idempotent ``sources_pause`` whose same key is still being processed is a
``TransientConflict`` with the stable ``details.reason = in_progress`` (retryable
``VERSION_CONFLICT``), not an untyped conflict without a reason. Synthetic; nothing is fetched."""

from __future__ import annotations

import uuid

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_sources_listings.support import Env

from suv_deals.errors import ErrorCode
from suv_deals.mcp.schemas import SourcesPauseInput
from suv_deals.persistence import idempotency, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def test_pause_with_a_running_same_key_request_is_in_progress(
    db: Database, env: Env, seed: Seed
) -> None:
    owner = env.owner
    version = seed.scalar("select version from app.sources where id = %s", (env.source_id,))
    key = f"pause-{uuid.uuid4().hex[:12]}"
    reason = "synthetic pause in progress"
    request = SourcesPauseInput(
        source_id=env.source_id, expected_version=version, reason=reason, idempotency_key=key
    )
    # The same request started in another (committed) transaction and has not completed yet.
    async with unit_of_work(db, owner) as conn:
        started = await idempotency.begin(
            conn,
            owner,
            sources_repo.PAUSE_OPERATION,
            key,
            idempotency.request_hash_for(sources_repo.PAUSE_OPERATION, request),
        )
    assert isinstance(started, idempotency.NewRequest)
    with pytest.raises(TransientConflict) as exc:
        async with unit_of_work(db, owner) as conn:
            await sources_repo.pause_source(conn, owner, env.source_id, version, reason, key)
    assert exc.value.code == ErrorCode.VERSION_CONFLICT and exc.value.retryable
    assert exc.value.details == {"reason": "in_progress"}
    assert seed.scalar("select paused from app.sources where id = %s", (env.source_id,)) is False
