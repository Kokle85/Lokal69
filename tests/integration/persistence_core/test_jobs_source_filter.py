"""`jobs.claim(source_key=...)`: an owner's smoke drain claims only one source's jobs.

``suv-deals worker --drain --source KEY`` (docs/runbook.md 9.1): without the filter a drain claims
the due jobs of EVERY source in ``priority desc, available_at, id`` order, so queued detail jobs of
an earlier, since disabled smoke source come first and are blocked ``source_paused``. With it
only the jobs whose ``source_id`` is the workspace's source with that key are claimed; jobs of
other sources and source-less jobs (valuation, seller inquiries) are never touched.

Everything runs as ``suv_backend`` under workspace RLS; data is SYNTHETIC.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, World
from tests.integration.persistence_core.support import job_row, spec, system

from suv_deals.domain.enums import JobState, JobType
from suv_deals.errors import ValidationFailed
from suv_deals.persistence import jobs
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

DETAIL_AND_VALUATION = [JobType.DETAIL, JobType.VALUATION]


def _key(seed: Seed, source_id: UUID) -> str:
    return str(seed.scalar("select source_key from app.sources where id = %s", (source_id,)))


def _ago(seed: Seed, interval: str) -> datetime:
    value = seed.scalar("select now() - %s::interval", (interval,))
    assert isinstance(value, datetime)
    return value


async def _enqueue(db: Database, ws: UUID, **kwargs: object) -> UUID:
    actor = system(ws)
    async with db.transaction(actor) as conn:
        job_id, created = await jobs.enqueue(conn, actor, spec(**kwargs))
    assert created
    return job_id


async def _detail(db: Database, seed: Seed, ws: UUID, source_id: UUID, **kwargs: object) -> UUID:
    """A queued detail job of ``source_id`` (bound to a fresh synthetic listing of that source)."""
    listing = seed.listing(ws, source_id)
    return await _enqueue(
        db, ws, job_type=JobType.DETAIL, source_id=source_id, listing_id=listing, generation=1, **kwargs
    )


def _untouched(seed: Seed, *job_ids: UUID) -> None:
    for job_id in job_ids:
        row = job_row(seed, job_id)
        assert row["state"] == "queued" and row["attempts"] == 0 and row["lease_token"] is None, row


async def test_a_source_claim_takes_only_that_sources_jobs_in_priority_order(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    left = world_a.source_id  # e.g. the earlier smoke source, disabled again with jobs still queued
    smoke = seed.source(ws)
    left_old = await _detail(db, seed, ws, left, available_at=_ago(seed, "2 hours"))
    left_high = await _detail(db, seed, ws, left, priority=100)
    sourceless = await _enqueue(db, ws, priority=1000)  # a valuation job: no source_id
    smoke_low = await _detail(db, seed, ws, smoke, available_at=_ago(seed, "1 hour"))
    smoke_high = await _detail(db, seed, ws, smoke, priority=50)

    claimed = []
    while (
        job := await jobs.claim(db, ws, "smoke", DETAIL_AND_VALUATION, 60, source_key=_key(seed, smoke))
    ) is not None:
        claimed.append(job)
    assert [j.id for j in claimed] == [smoke_high, smoke_low]
    assert all(j.source_id == smoke and j.state == JobState.RUNNING and j.attempts == 1 for j in claimed)
    _untouched(seed, left_old, left_high, sourceless)

    # Without the filter the claim is unchanged: every source, highest priority, then oldest.
    order = [await jobs.claim(db, ws, "any", DETAIL_AND_VALUATION, 60) for _ in range(4)]
    assert [None if j is None else j.id for j in order] == [sourceless, left_high, left_old, None]


async def test_without_a_source_key_the_oldest_job_of_any_source_comes_first(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    smoke = seed.source(ws)
    leftover = await _detail(db, seed, ws, world_a.source_id, available_at=_ago(seed, "1 day"))
    mine = await _detail(db, seed, ws, smoke, available_at=_ago(seed, "1 minute"))
    # The reason for the filter: an unfiltered drain reaches the other source's older job first.
    assert "source_key" not in jobs._CLAIM_SQL.as_string(None)
    first = await jobs.claim(db, ws, "any", [JobType.DETAIL], 60)
    assert first is not None and first.id == leftover
    second = await jobs.claim(db, ws, "any", [JobType.DETAIL], 60)
    assert second is not None and second.id == mine


async def test_a_source_claim_finds_nothing_while_only_other_jobs_are_due(
    db: Database, world_a: World, world_b: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    smoke = seed.source(ws)
    smoke_key = _key(seed, smoke)
    leftover = await _detail(db, seed, ws, world_a.source_id)
    sourceless = await _enqueue(db, ws)
    future = seed.scalar("select now() + interval '1 hour'")
    not_due = await _detail(db, seed, ws, smoke, available_at=future)
    assert await jobs.claim(db, ws, "smoke", DETAIL_AND_VALUATION, 60, source_key=smoke_key) is None
    # A well-formed key that names no source of this workspace claims nothing either, also the
    # key of another workspace's source (the key is resolved in the claiming workspace only).
    assert await jobs.claim(db, ws, "smoke", DETAIL_AND_VALUATION, 60, source_key="no_such_source") is None
    other_ws_job = await _detail(db, seed, world_b.workspace_id, world_b.source_id)
    other_key = _key(seed, world_b.source_id)
    assert await jobs.claim(db, ws, "smoke", DETAIL_AND_VALUATION, 60, source_key=other_key) is None
    _untouched(seed, leftover, sourceless, not_due, other_ws_job)
    there = await jobs.claim(db, world_b.workspace_id, "smoke", [JobType.DETAIL], 60, source_key=other_key)
    assert there is not None and there.id == other_ws_job


@pytest.mark.parametrize(
    "key",
    [
        "",
        "ab",
        "AutoScout24_it",
        "autoscout24-it",
        "autoscout24_it\n",
        "a" * 61,
        "x' or '1'='1",
        " autoscout24",
    ],
)
async def test_a_malformed_source_key_is_refused_before_any_claim(
    db: Database, world_a: World, seed: Seed, key: str
) -> None:
    ws = world_a.workspace_id
    job_id = await _detail(db, seed, ws, world_a.source_id)
    with pytest.raises(ValidationFailed):
        await jobs.claim(db, ws, "smoke", [JobType.DETAIL], 60, source_key=key)
    _untouched(seed, job_id)


async def test_incompatible_payloads_are_skipped_within_the_source_only(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    smoke = seed.source(ws)
    left_v2 = await _detail(db, seed, ws, world_a.source_id, payload_version=2, priority=100)
    smoke_v2 = await _detail(db, seed, ws, smoke, payload_version=2, priority=50)
    smoke_v1 = await _detail(db, seed, ws, smoke, payload_version=1, priority=1)
    job = await jobs.claim(
        db,
        ws,
        "old-worker",
        [JobType.DETAIL],
        60,
        payload_versions={JobType.DETAIL: {1}},
        source_key=_key(seed, smoke),
    )
    assert job is not None and job.id == smoke_v1
    blocked = job_row(seed, smoke_v2)
    assert blocked["state"] == "blocked" and blocked["blocker_code"] == jobs.INCOMPATIBLE_PAYLOAD_VERSION
    _untouched(seed, left_v2)  # another source's incompatible job is not even looked at


async def test_concurrent_source_claims_never_share_a_job_or_leave_the_source(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    smoke = seed.source(ws)
    smoke_key = _key(seed, smoke)
    expected = {await _detail(db, seed, ws, smoke) for _ in range(16)}
    others = [await _detail(db, seed, ws, world_a.source_id, priority=500) for _ in range(8)]

    async def drain(name: str) -> list[UUID]:
        mine: list[UUID] = []
        while (
            job := await jobs.claim(db, ws, name, [JobType.DETAIL], 120, source_key=smoke_key)
        ) is not None:
            assert job.source_id == smoke and job.lease_owner == name
            mine.append(job.id)
        return mine

    results = await asyncio.gather(*(drain(f"smoke-{i}") for i in range(4)))
    claimed = [job_id for result in results for job_id in result]
    assert len(claimed) == len(set(claimed)) and set(claimed) == expected
    _untouched(seed, *others)
