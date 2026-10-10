"""Reconciliation passes (spec 9, 13, 18, 30): reapers, housekeeping, bounded sweeps, dry run.

Time travel (expired leases, old detail checks, passed deadlines) is arranged through the
superuser ``seed`` connection; every pass runs as ``suv_backend``. All data is SYNTHETIC.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from tests.integration.pipeline.support import (
    REPO,
    PipelineEnv,
    by_slid,
    jobs_of,
    run,
    run_pipeline,
    user_actor,
)

from suv_deals.domain.enums import JobType, Role
from suv_deals.domain.profiles import load_business_config
from suv_deals.persistence import config_repo, jobs, notes_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db


def reconciler(env: PipelineEnv, **options: object) -> Reconciler:
    return Reconciler(env.ctx, ReconcileOptions(**options))  # type: ignore[arg-type]


def age_listing_details(env: PipelineEnv) -> None:
    """Time travel: the last detail check (and the detail jobs that made it) were three days ago."""
    env.seed.conn.execute(
        "update app.listings set last_detail_success_at = now() - interval '3 days' where workspace_id = %s",
        (env.workspace_id,),
    )
    env.seed.conn.execute(
        "update ops.jobs set created_at = now() - interval '3 days'"
        " where workspace_id = %s and job_type in ('detail', 'recheck')",
        (env.workspace_id,),
    )


async def _running_job(env: PipelineEnv) -> uuid.UUID:
    async def enqueue(conn: Conn) -> None:
        await jobs.enqueue(
            conn, env.system, jobs.JobSpec(job_type=JobType.VALUATION, dedup_key=f"valuation:{uuid.uuid4()}")
        )

    await run(env.ctx, env.system, enqueue)
    claimed = await jobs.claim(env.ctx.db, env.workspace_id, "crashed-worker", [JobType.VALUATION], 60)
    assert claimed is not None
    env.seed.conn.execute(
        "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 second' where id = %s",
        (claimed.id,),
    )
    return claimed.id


async def test_dry_run_reports_and_changes_nothing_then_a_pass_applies(env: PipelineEnv) -> None:
    job_id = await _running_job(env)
    snapshot = env.seed.insert_id(
        "ops.query_snapshots",
        workspace_id=env.workspace_id,
        principal_id=uuid.uuid4(),
        query_name="review_queue",
        filter_hash="0" * 64,
        result_ids=[],
        created_at=env.scalar("select now() - interval '2 hours'"),
        expires_at=env.scalar("select now() - interval '1 hour'"),
    )
    dry = await reconciler(env).reconcile_workspace(env.workspace_id, dry_run=True)
    assert dry.dry_run and dry.errors == []
    assert dry.jobs_requeued == 1 and dry.snapshots_deleted == 1
    assert env.scalar("select state from ops.jobs where id = %s", job_id) == "running"
    assert env.scalar("select count(*) from ops.query_snapshots where id = %s", snapshot) == 1

    applied = await reconciler(env, job_retry_delay_seconds=0).reconcile_workspace(env.workspace_id)
    assert applied.errors == [] and not applied.dry_run
    assert applied.jobs_requeued == 1 and applied.snapshots_deleted == 1
    [row] = env.rows("select state, last_error_code from ops.jobs where id = %s", job_id)
    assert row == {"state": "retry_wait", "last_error_code": "LEASE_EXPIRED"}
    assert env.scalar("select count(*) from ops.query_snapshots where id = %s", snapshot) == 0


async def test_exhausted_lease_is_dead_lettered_never_left_invisible(env: PipelineEnv) -> None:
    job_id = await _running_job(env)
    env.seed.conn.execute("update ops.jobs set max_attempts = attempts where id = %s", (job_id,))
    report = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert report.jobs_dead_lettered == 1
    [row] = env.rows("select state, last_error_code from ops.jobs where id = %s", job_id)
    assert row == {"state": "dead_letter", "last_error_code": "LEASE_EXPIRED"}


async def test_stale_detail_sweep_is_bounded_and_deduplicated(env: PipelineEnv) -> None:
    await run_pipeline(env)
    age_listing_details(env)
    before = len(jobs_of(env, JobType.DETAIL))
    # A per-source cap of 2 detail fetches per run: never more than one run's budget per pass.
    env.seed.conn.execute(
        "update app.sources set config = jsonb_set(config, '{rate_budget}',"
        " coalesce(config -> 'rate_budget', '{}'::jsonb) || '{\"max_detail_jobs_per_run\": 2}'::jsonb)"
        " where id = %s",
        (env.source_id,),
    )
    first = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert first.errors == [] and first.stale_detail_jobs == 2
    second = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert second.stale_detail_jobs == 1  # the third candidate; the two queued ones are deduplicated
    third = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert third.stale_detail_jobs == 0
    queued = [j for j in jobs_of(env, JobType.DETAIL)][before:]
    assert len(queued) == 3 and all(j["state"] == "queued" for j in queued)
    slids = {r["id"]: s for s, r in by_slid(env).items()}
    assert {slids[j["listing_id"]] for j in queued} == {"TEST-204", "TEST-206", "TEST-207"}  # never rejected
    # A paused source gets nothing (the earlier sweep jobs ended a sweep age ago, so only the
    # pause holds the listings back: resuming queues them again).
    env.seed.conn.execute(
        "update ops.jobs set state = 'cancelled', completed_at = now() - interval '2 days',"
        " created_at = now() - interval '2 days' where id = any(%s)",
        ([j["id"] for j in queued],),
    )
    env.seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'SYNTHETIC pause', paused_at = now()"
        " where id = %s",
        (env.source_id,),
    )
    paused = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert paused.stale_detail_jobs == 0
    env.seed.conn.execute(
        "update app.sources set paused = false, pause_reason = null, paused_at = null where id = %s",
        (env.source_id,),
    )
    resumed = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert resumed.stale_detail_jobs == 2  # the per-run detail budget again


async def test_stale_detail_sweep_never_refetches_a_failing_page_every_pass(env: PipelineEnv) -> None:
    """A sweep job that failed (dead letter) is not re-queued on the next pass: at most one
    refresh per listing per sweep age (spec 9: never silently increase traffic)."""
    await run_pipeline(env)
    age_listing_details(env)
    before = len(jobs_of(env, JobType.DETAIL))
    first = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert first.stale_detail_jobs == 3
    swept = jobs_of(env, JobType.DETAIL)[before:]
    env.seed.conn.execute(
        "update ops.jobs set state = 'dead_letter', completed_at = now(),"
        " last_error_code = 'detail_parse_failed' where id = any(%s)",
        ([j["id"] for j in swept],),
    )
    again = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert again.errors == [] and again.stale_detail_jobs == 0
    assert len(jobs_of(env, JobType.DETAIL)) == before + 3
    # Once a sweep age has passed since that attempt, the listing is due again (one more try).
    env.seed.conn.execute(
        "update ops.jobs set created_at = now() - interval '25 hours' where id = any(%s)",
        ([j["id"] for j in swept],),
    )
    later = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert later.stale_detail_jobs == 3


async def test_a_paused_sources_backlog_never_starves_the_stale_detail_sweep(env: PipelineEnv) -> None:
    """Candidates are chosen per network-eligible source: older stale listings of a paused source
    can never fill the per-pass window and keep every other source from being swept."""
    from tests.integration.repos_valuation_reviews.builders import make_listing  # noqa: PLC0415

    await run_pipeline(env)
    age_listing_details(env)  # the fixture source's listings: last checked three days ago
    paused = env.seed.source(
        env.workspace_id, source_key=f"synthetic_paused_{uuid.uuid4().hex[:8]}", detail_mode="fetch"
    )
    env.seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'SYNTHETIC pause', paused_at = now()"
        " where id = %s",
        (paused,),
    )
    for _ in range(3):
        make_listing(env.seed, env.workspace_id, paused)  # eligible
    env.seed.conn.execute(
        "update app.listings set last_detail_success_at = now() - interval '5 days' where source_id = %s",
        (paused,),
    )
    report = await reconciler(env, stale_detail_candidates=3).reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.stale_detail_jobs == 3
    swept = env.rows(
        "select source_id from ops.jobs where workspace_id = %s and job_type = 'detail'"
        " and payload ->> 'reason' = 'stale_detail_sweep'",
        env.workspace_id,
    )
    assert [r["source_id"] for r in swept] == [env.source_id] * 3  # nothing for the paused source


async def test_due_watch_recheck_becomes_a_recheck_job(env: PipelineEnv) -> None:
    await run_pipeline(env)
    listing_id = by_slid(env)["TEST-204"]["id"]
    user = env.seed.user()
    env.seed.membership(env.workspace_id, user, "reviewer")
    reviewer = user_actor(env.workspace_id, user, Role.REVIEWER)
    watch = await run(
        env.ctx,
        reviewer,
        lambda c: notes_repo.add_watch(
            c, reviewer, listing_id, reason="SYNTHETIC watch", recheck_interval=timedelta(hours=6)
        ),
    )
    env.seed.conn.execute(
        "update app.watchlists set next_recheck_at = now() - interval '1 minute' where id = %s", (watch.id,)
    )
    report = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert report.watch_rechecks == 1
    [recheck] = jobs_of(env, JobType.RECHECK)
    assert recheck["listing_id"] == listing_id and recheck["state"] == "queued"
    next_at = env.scalar(
        "select next_recheck_at > now() + interval '5 hours' from app.watchlists where id = %s", watch.id
    )
    assert next_at is True
    # The worker runs the recheck through the shared detail handler (a bounded refresh).
    done = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-recheck"
    ).run_until_idle()
    assert [(r.job_type, r.state) for r in done if r.job_type == JobType.RECHECK] == [
        (JobType.RECHECK, "succeeded")
    ]


async def test_watches_on_a_paused_source_never_starve_due_watch_rechecks(env: PipelineEnv) -> None:
    """More overdue watches on a source that may not fetch than the window never hide a fetchable one."""
    from tests.integration.repos_valuation_reviews.builders import make_listing  # noqa: PLC0415

    await run_pipeline(env)
    listing_id = by_slid(env)["TEST-204"]["id"]
    user = env.seed.user()
    env.seed.membership(env.workspace_id, user, "reviewer")
    reviewer = user_actor(env.workspace_id, user, Role.REVIEWER)
    paused = env.seed.source(
        env.workspace_id, source_key=f"synthetic_paused_{uuid.uuid4().hex[:8]}", detail_mode="fetch"
    )
    env.seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'SYNTHETIC pause', paused_at = now()"
        " where id = %s",
        (paused,),
    )
    stuck: list[uuid.UUID] = []
    for _ in range(3):
        other, _rev = make_listing(env.seed, env.workspace_id, paused)
        watch = await run(
            env.ctx,
            reviewer,
            lambda c, li=other: notes_repo.add_watch(c, reviewer, li, reason="SYNTHETIC stuck"),
        )
        stuck.append(watch.id)
    due = await run(
        env.ctx,
        reviewer,
        lambda c: notes_repo.add_watch(
            c, reviewer, listing_id, reason="SYNTHETIC watch", recheck_interval=timedelta(hours=6)
        ),
    )
    env.seed.conn.execute(
        "update app.watchlists set next_recheck_at = now() - interval '2 hours' where id = any(%s)", (stuck,)
    )
    env.seed.conn.execute(
        "update app.watchlists set next_recheck_at = now() - interval '1 minute' where id = %s", (due.id,)
    )
    report = await reconciler(env, watch_recheck_limit=2).reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.watch_rechecks == 1
    [recheck] = jobs_of(env, JobType.RECHECK)
    assert recheck["listing_id"] == listing_id
    # The paused source's watches stay due (never advanced) until that source may fetch again.
    still_due = env.scalar(
        "select count(*) from app.watchlists where id = any(%s) and next_recheck_at <= now()", stuck
    )
    assert still_due == 3


async def test_expired_valuation_is_marked_stale_and_recomputed(env: PipelineEnv) -> None:
    await run_pipeline(env)
    listing_id = by_slid(env)["TEST-204"]["id"]
    [valuation_id] = [
        r["id"]
        for r in env.rows(
            "select id from app.valuations where workspace_id = %s and listing_id = %s",
            env.workspace_id,
            listing_id,
        )
    ]
    dry = await reconciler(env).reconcile_workspace(env.workspace_id, dry_run=True)
    assert dry.valuations_expired == 0
    # Time travel: the deadline is frozen by a guard trigger, so it is moved with triggers disabled.
    with env.seed.conn.transaction():
        env.seed.conn.execute("set local session_replication_role = replica")
        env.seed.conn.execute(
            "update app.valuations set expires_at = now() - interval '1 minute' where id = %s",
            (valuation_id,),
        )
    report = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert report.valuations_expired == 1 and report.recompute_jobs >= 1
    [row] = env.rows("select state, stale_reason from app.valuations where id = %s", valuation_id)
    assert row["state"] == "stale" and row["stale_reason"].startswith("freshness_deadline")
    recompute = [j for j in jobs_of(env, JobType.VALUATION) if j["state"] == "queued"]
    assert [j["listing_id"] for j in recompute] == [listing_id]
    done = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-recompute"
    ).run_until_idle()
    assert [(r.job_type, r.state) for r in done] == [(JobType.VALUATION, "succeeded")]
    states = sorted(
        r["state"]
        for r in env.rows(
            "select state from app.valuations where workspace_id = %s and listing_id = %s",
            env.workspace_id,
            listing_id,
        )
    )
    assert states == ["incomplete", "stale"]  # the old one stays stale; history is kept


async def test_new_configuration_invalidates_dependent_valuations(env: PipelineEnv) -> None:
    await run_pipeline(env)
    open_before = env.scalar(
        "select count(*) from app.valuations where workspace_id = %s and state <> 'stale'", env.workspace_id
    )
    assert open_before == 3
    config = load_business_config(REPO / "config")
    changed = config.model_copy(update={"comparable_max_age_days": config.comparable_max_age_days - 1})
    await run(
        env.ctx,
        env.owner,
        lambda c: config_repo.record_config_revision(
            c, env.owner, changed, "SYNTHETIC config change", config
        ),
    )
    report = await reconciler(env).reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.valuations_invalidated == 3 and report.recompute_jobs == 3
    assert (
        env.scalar(
            "select count(*) from app.valuations where workspace_id = %s and state = 'stale'",
            env.workspace_id,
        )
        == 3
    )
