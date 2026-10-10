"""Regression tests for defects found in the independent WP7b1 review.

Each test fails on the reviewed implementation and pins the corrected behaviour:

- the complete-scan absence pass overrode a fresher detail check, and swept listings after a parser
  incident or an empty "complete" traversal (spec 9, 25, 37.9);
- a late finish of an older complete run moved ``last_complete_traversal_at`` backwards (spec 9);
- a crawl run in another coverage mode than its schedule could never be finished (spec 9);
- a configuration revision that swaps queue labels failed with a misleading ``VersionConflict``,
  and duplicate queue labels were not reported as a validation error (spec 3, 26);
- concurrent access blocks of one source must still open exactly one review item (spec 9).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.repos_sources_listings.support import (
    HOST,
    PARSER,
    Env,
    card,
    claim_detail,
    ingest,
    page,
    parsed,
    start_run,
    vehicle,
)

from suv_deals.domain.enums import Completeness, CoverageMode, ProfileKey, TechnicalStatus
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.errors import ValidationFailed, VersionConflict
from suv_deals.persistence import config_repo, listings_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.listings_repo import DetailSnapshotRef
from suv_deals.persistence.sources_repo import CrawlRunRecord, RunOutcome, SourceRoute
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _finish(db: Database, env: Env, run: CrawlRunRecord, completeness: str = "complete") -> None:
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.finish_crawl_run(conn, env.system, run.id, RunOutcome(completeness=completeness))


async def _absences(db: Database, env: Env, run: CrawlRunRecord) -> listings_repo.AbsenceReport:
    async with unit_of_work(db, env.system) as conn:
        return await listings_repo.mark_complete_scan_absences(conn, env.system, run.id)


def _availability(seed: Seed, listing_id: UUID) -> str:
    return str(seed.scalar("select availability from app.listings where id = %s", (listing_id,)))


async def _two_available_listings(db: Database, env: Env, seed: Seed) -> tuple[UUID, UUID]:
    """Two listings seen by one complete traversal, both currently 'available'."""
    first = await start_run(db, env)
    report = await ingest(db, env, first, page(env.source_key, [card(unique("SYN")), card(unique("SYN"))]))
    ids = sorted({j.listing_id for j in report.detail_jobs})
    seed.conn.execute("update app.listings set availability = 'available' where id = any(%s)", (ids,))
    await _finish(db, env, first)
    return ids[0], ids[1]


# --------------------------------------------------------------------------------------------
# Complete-scan absence
# --------------------------------------------------------------------------------------------


async def test_absence_never_overrides_a_fresher_detail_check(db: Database, env: Env, seed: Seed) -> None:
    checked, absent = await _two_available_listings(db, env, seed)
    seed.conn.execute(
        "update ops.jobs set state = 'cancelled', completed_at = now() where listing_id = %s", (absent,)
    )
    second = await start_run(db, env)
    # Neither listing is on the second traversal (it shows only a third car) ...
    await ingest(db, env, second, page(env.source_key, [card(unique("SYN"))]))
    # ... but a detail check made AFTER the traversal started confirms the first one is available.
    job = await claim_detail(db, env)
    assert job.listing_id == checked
    slid = str(seed.scalar("select source_listing_id from app.listings where id = %s", (checked,)))
    async with unit_of_work(db, env.system) as conn:
        await listings_repo.ingest_detail(
            conn,
            env.system,
            job,
            checked,
            parsed(vehicle(slid)),
            DetailSnapshotRef(parser_version=PARSER, observed_at=datetime.now(UTC)),
        )
    await _finish(db, env, second)
    report = await _absences(db, env, second)
    assert report.marked_unknown == (absent,) and report.skipped_reason is None  # control: absence works
    assert _availability(seed, checked) == "available"  # direct, fresher evidence wins
    assert _availability(seed, absent) == "unknown"


@pytest.mark.parametrize("incident", ["parser_unhealthy", "empty_traversal"])
async def test_parser_incident_or_empty_traversal_is_no_absence_evidence(
    db: Database, env: Env, seed: Seed, incident: str
) -> None:
    first_id, second_id = await _two_available_listings(db, env, seed)
    run = await start_run(db, env)
    if incident == "parser_unhealthy":
        await ingest(db, env, run, page(env.source_key, [card(unique("SYN"))]))
        async with unit_of_work(db, env.system) as conn:
            await sources_repo.set_technical_status(
                conn, env.system, env.source_id, TechnicalStatus.PARSER_UNHEALTHY, reason="synthetic drift"
            )
    else:
        await ingest(db, env, run, page(env.source_key, []))  # parsed as "complete" but found nothing
    await _finish(db, env, run)
    report = await _absences(db, env, run)
    assert report.marked_unknown == () and report.skipped_reason == incident
    assert {_availability(seed, first_id), _availability(seed, second_id)} == {"available"}


# --------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------


async def test_late_finish_of_an_older_run_never_moves_coverage_back(db: Database, env: Env) -> None:
    older = await start_run(db, env)
    newer = await start_run(db, env)
    assert older.started_at < newer.started_at
    await _finish(db, env, newer)
    async with unit_of_work(db, env.system) as conn:
        late = await sources_repo.finish_crawl_run(
            conn, env.system, older.id, RunOutcome(completeness="complete")
        )
    assert late.schedule is not None
    assert late.schedule.last_complete_traversal_at == newer.started_at


async def test_run_coverage_mode_must_match_its_schedule(db: Database, env: Env) -> None:
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(ValidationFailed):
            await sources_repo.start_crawl_run(
                conn,
                env.system,
                source_id=env.source_id,
                profile_id=env.profiles["primary"],  # the default partition is a rolling_pages schedule
                coverage_mode=CoverageMode.WATERMARK,
                adapter_version="fixture@1.0.0",
                watermark_from=datetime.now(UTC) - timedelta(hours=48),
            )
        # A partition without a schedule (ad-hoc diagnostic run) may use either mode.
        adhoc = await sources_repo.start_crawl_run(
            conn,
            env.system,
            source_id=env.source_id,
            profile_id=env.profiles["primary"],
            partition_key="adhoc-watermark",
            coverage_mode=CoverageMode.WATERMARK,
            adapter_version="fixture@1.0.0",
        )
        done = await sources_repo.finish_crawl_run(
            conn,
            env.system,
            adhoc.id,
            RunOutcome(completeness=Completeness.COMPLETE, watermark_to=datetime.now(UTC)),
        )
    assert done.run.outcome == "complete" and done.schedule is None


# --------------------------------------------------------------------------------------------
# Configuration queue labels
# --------------------------------------------------------------------------------------------


def _with_profiles(
    config: BusinessConfig, changes: dict[ProfileKey, dict[str, Any] | None]
) -> BusinessConfig:
    profiles: dict[ProfileKey, Any] = dict(config.profiles)
    for key, update in changes.items():
        if update is None:
            profiles.pop(key, None)
        else:
            profiles[key] = profiles[key].model_copy(update=update)
    return BusinessConfig.model_validate({**config.model_dump(), "profiles": profiles})


async def _labels(db: Database, env: Env) -> dict[str, tuple[str, bool]]:
    async with unit_of_work(db, env.owner) as conn:
        rows = await config_repo.list_profiles(conn, env.owner)
    return {p.profile_key.value: (p.queue_label, p.enabled) for p in rows}


async def test_revision_can_swap_queue_labels(db: Database, env: Env) -> None:
    async with unit_of_work(db, env.owner) as conn:
        _, current = await config_repo.current_config(conn, env.owner)
    manual = current.profiles[ProfileKey.MANUAL_4000].queue_label
    below = current.profiles[ProfileKey.BELOW_TARGET_WATCH].queue_label
    swapped = _with_profiles(
        current,
        {
            ProfileKey.MANUAL_4000: {"queue_label": below},
            ProfileKey.BELOW_TARGET_WATCH: {"queue_label": manual},
        },
    )
    async with unit_of_work(db, env.owner) as conn:
        result = await config_repo.record_config_revision(
            conn, env.owner, swapped, "swap queue labels", current
        )
    assert result.created and result.revision.revision == 2
    labels = await _labels(db, env)
    assert labels["manual_4000"][0] == below and labels["below_target_watch"][0] == manual
    # A dropped profile whose label is reused keeps its row (disabled) under a retired label.
    dropped = _with_profiles(
        swapped, {ProfileKey.MANUAL_4000: None, ProfileKey.BELOW_TARGET_WATCH: {"queue_label": below}}
    )
    async with unit_of_work(db, env.owner) as conn:
        third = await config_repo.record_config_revision(
            conn, env.owner, dropped, "drop manual queue", swapped
        )
    labels = await _labels(db, env)
    assert third.revision.revision == 3 and labels["below_target_watch"][0] == below
    assert labels["manual_4000"][0].startswith("retired manual_4000 queue") and not labels["manual_4000"][1]
    assert len({label for label, _ in labels.values()}) == 3


async def test_duplicate_queue_labels_are_a_validation_error(db: Database, env: Env) -> None:
    async with unit_of_work(db, env.owner) as conn:
        _, current = await config_repo.current_config(conn, env.owner)
    same = current.profiles[ProfileKey.MANUAL_4000].queue_label
    clash = _with_profiles(current, {ProfileKey.BELOW_TARGET_WATCH: {"queue_label": same}})
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(ValidationFailed):
            await config_repo.record_config_revision(
                conn, env.owner, clash, "two profiles, one queue", current
            )
        record, _ = await config_repo.current_config(conn, env.owner)
    assert record.revision == 1


# --------------------------------------------------------------------------------------------
# Access blocks under concurrency
# --------------------------------------------------------------------------------------------


async def test_concurrent_access_blocks_open_exactly_one_review_item(
    db: Database, env: Env, seed: Seed
) -> None:
    async def block(marker: str) -> sources_repo.AccessBlockResult:
        async with unit_of_work(db, env.system) as conn:
            return await sources_repo.record_access_block(
                conn,
                env.system,
                env.source_id,
                SourceRoute(host=HOST, purpose="search"),
                f"HTTP 403 {marker}",
            )

    results = await asyncio.gather(*(block(f"worker-{i}") for i in range(4)))
    assert sum(r.created for r in results) == 1 and len({r.review_item_id for r in results}) == 1
    assert sum(not r.already_blocked for r in results) == 1
    assert (
        seed.scalar(
            "select count(*) from ops.activation_gates where workspace_id = %s and capability = %s",
            (env.workspace_id, sources_repo.access_gate_capability(env.source_key)),
        )
        == 1
    )
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'source.access_blocked'",
            (env.source_id,),
        )
        == 1
    )


async def test_writer_that_loses_the_race_after_its_check_gets_version_conflict(
    db: Database, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic interleaving: B checks ``before`` against revision 1, A then commits revision
    2, and only then does B insert. B must fail instead of stacking revision 3 on a configuration
    it never saw (the reviewed code computed ``max(revision) + 1`` at insert time: lost update)."""
    async with unit_of_work(db, env.owner) as conn:
        _, current = await config_repo.current_config(conn, env.owner)
    checked, committed = asyncio.Event(), asyncio.Event()
    original = config_repo._current

    async def paused_current(conn: Any, workspace_id: UUID) -> Any:
        record = await original(conn, workspace_id)
        if not checked.is_set():
            checked.set()
            await committed.wait()  # B holds its (stale) check while A commits
        return record

    monkeypatch.setattr(config_repo, "_current", paused_current)

    def threshold(amount: str) -> BusinessConfig:
        data: dict[str, Any] = current.model_dump()
        data["contribution_threshold"] = {**data["contribution_threshold"], "amount_eur": amount}
        return BusinessConfig.model_validate(data)

    async def writer_b() -> Any:
        async with unit_of_work(db, env.owner) as conn:
            return await config_repo.record_config_revision(
                conn, env.owner, threshold("1900"), "writer B", current
            )

    async def writer_a() -> Any:
        await asyncio.wait_for(checked.wait(), timeout=10)
        try:
            async with unit_of_work(db, env.owner) as conn:
                return await config_repo.record_config_revision(
                    conn, env.owner, threshold("1800"), "writer A", current
                )
        finally:
            committed.set()

    outcomes: list[Any] = list(await asyncio.gather(writer_b(), writer_a(), return_exceptions=True))
    b, a = outcomes
    assert isinstance(a, config_repo.ConfigRevisionResult) and a.revision.revision == 2
    assert isinstance(b, VersionConflict)
    monkeypatch.setattr(config_repo, "_current", original)
    async with unit_of_work(db, env.owner) as conn:
        revisions = await config_repo.list_config_revisions(conn, env.owner)
    assert [r.revision for r in revisions] == [2, 1] and revisions[0].reason == "writer A"
