"""Planning and transmission gates of the bounded automatic inquiry (spec 37.1-37.5, 37.10 U3/U6).

- (b) ``SELLER_INQUIRY_MODE=disabled_until_sender_ready``: readiness is recorded, nothing is
  reserved, queued or sent;
- (c) fixture lineage never reserves (plan) and is never transmitted (send);
- (d) the kill switch between reservation and send holds the send job (released, no attempt
  consumed) and nothing is published;
- (e) the third candidate within 24 hours waits for the rolling window (released with
  ``available_at``; never dead-lettered) and is reserved once the window frees;
- (g) a stale (or missing) desktop-worker heartbeat holds the send; a fresh one publishes;
- planning is idempotent per listing revision, and the reconciliation sweep plans a listing
  whose seller contact was recorded after its first plan found nobody to ask.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    PipelineEnv,
    build_env,
    run,
    seed_comparables,
    seed_fx,
)
from tests.integration.v11_inquiries.support import World, add_vehicle, backdate, reserve_and_queue
from tests.integration.v11_runtime.support import (
    age_heartbeat,
    attempts_of,
    debits_of,
    eligible_live_listing,
    expire_job_wait,
    heartbeat,
    ingest,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    pending_intents,
    prepare_sender,
    pull_kill_switch,
    runtime_settings,
    with_settings,
    work,
)

from suv_deals.domain.enums import InquiryState, JobType
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.inquiry_handlers import (
    InquiryRuntimeOptions,
    enqueue_plan_job,
    enqueue_send_job,
    inquiry_runtime,
)
from suv_deals.workers.reconciliation import Reconciler

pytestmark = pytest.mark.db


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def _planned(env: PipelineEnv) -> dict[str, object]:
    """Ingest, link the seller, run valuation + plan only (send jobs wait)."""
    listing = await eligible_live_listing(env)
    await link_seller(env, listing)
    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    return listing


# --------------------------------------------------------------------------------------------
# (b) mode disabled_until_sender_ready
# --------------------------------------------------------------------------------------------


async def test_disabled_mode_records_readiness_only(env: PipelineEnv) -> None:
    disabled = with_settings(
        env, runtime_settings(_db_url(env), seller_inquiry_mode="disabled_until_sender_ready")
    )
    sender = await prepare_sender(disabled, automatic=False)
    assert sender.worker is not None
    listing = await eligible_live_listing(disabled)
    await link_seller(disabled, listing)
    reports = await work(disabled)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "succeeded"
    result = plan["result_reference"]
    assert result["outcome"] == "readiness_recorded"
    assert result["reservation"] == "seller_inquiry_mode_not_automatic"
    [inquiry] = inquiries_of(env)
    # Visible, informational readiness: sender not ready -> technical review, never reserved.
    assert inquiry["state"] == InquiryState.HELD_FACTS
    assert inquiry["readiness"] == "needs_technical_review"
    assert "SENDER_NOT_READY" in inquiry["readiness_reasons"]
    assert inquiry["sender_binding_id"] is None  # nothing bound
    assert debits_of(env) == 0
    assert jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []
    assert attempts_of(env, inquiry["id"]) == []
    assert (await pending_intents(env, sender.worker)).intents == ()
    assert all(r.job_type != JobType.SELLER_INQUIRY_SEND for r in reports)
    no_approval_state(env)

    # Even with the settings in automatic mode, the workspace controls (disabled) still refuse.
    automatic = with_settings(env, runtime_settings(_db_url(env)))
    job = await _replan(automatic, listing)
    await work(automatic, JobType.SELLER_INQUIRY_PLAN)
    [row] = [j for j in jobs_of(env, JobType.SELLER_INQUIRY_PLAN) if j["id"] == job]
    assert row["state"] == "succeeded" and row["result_reference"]["outcome"] in (
        "readiness_recorded",
        "held",
    )
    assert debits_of(env) == 0 and jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []


async def _replan(env: PipelineEnv, listing: dict[str, object]) -> object:
    async with env.ctx.db.transaction(env.system) as conn:
        job = await enqueue_plan_job(
            conn,
            env.system,
            listing_id=listing["id"],  # type: ignore[arg-type]
            revision_id=listing["current_revision_id"],  # type: ignore[arg-type]
            reason="test",
            suffix="again",
        )
    assert job is not None
    return job


# --------------------------------------------------------------------------------------------
# (c) fixture lineage
# --------------------------------------------------------------------------------------------


@pytest.fixture
async def fixture_env(db_url: str, seed: Seed) -> PipelineEnv:
    """The FIXTURE-mode dealer source (fixture lineage) with automatic settings."""
    built = await build_env(db_url, seed, settings=runtime_settings(db_url), name="Fixture runtime")
    try:
        yield built  # type: ignore[misc]
    finally:
        await built.close()


async def test_fixture_lineage_never_reserves_or_sends(fixture_env: PipelineEnv) -> None:
    env = fixture_env
    sender = await prepare_sender(env)
    assert sender.worker is not None
    await seed_comparables(env)
    await seed_fx(env)
    listings = await ingest(env)
    listing = listings["TEST-204"]
    assert listing["is_fixture"] is True and listing["eligibility_state"] == "eligible_primary"
    await link_seller(env, listing, source_key="fixture_dealer_de")
    await work(env)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "succeeded"
    assert plan["result_reference"]["outcome"] == "readiness_recorded"
    assert plan["result_reference"]["reservation"] == "fixture_lineage"
    assert debits_of(env) == 0 and jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []

    # A fixture-lineage inquiry that WAS queued (arranged through the repository) is never
    # transmitted by the send job: it is cancelled before any intent exists.
    vehicle = await add_vehicle(env.ctx.db, env.seed, env.workspace_id, fixture=True)
    assert env.scalar("select is_fixture from app.listings where id = %s", vehicle.listing_id) is True
    world = World(
        workspace_id=env.workspace_id, seed=env.seed, sender_binding_id=sender.binding_id, vehicle=vehicle
    )
    queued = await reserve_and_queue(env.ctx.db, world, vehicle)
    async with env.ctx.db.transaction(env.system) as conn:
        await enqueue_send_job(
            conn, env.system, inquiry_id=queued.id, listing_id=vehicle.listing_id, attempt_number=1
        )
    await work(env, JobType.SELLER_INQUIRY_SEND)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "succeeded" and send["result_reference"]["outcome"] == "fixture_lineage"
    assert attempts_of(env, queued.id) == []
    assert (await pending_intents(env, sender.worker)).intents == ()
    state = env.scalar("select state from app.seller_inquiries where id = %s", queued.id)
    assert state == InquiryState.CANCELLED


# --------------------------------------------------------------------------------------------
# (d) kill switch between reservation and send
# --------------------------------------------------------------------------------------------


async def test_kill_switch_between_reserve_and_send_holds_everything(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    await _planned(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and debits_of(env) == 1
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued"

    await pull_kill_switch(env)
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["SEND_HELD_PAUSED"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued" and send["attempts"] == 0  # released: no attempt consumed
    assert send["last_error_code"] == "SEND_HELD_PAUSED"
    assert send["available_at"] > datetime.now(UTC) + timedelta(minutes=5)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED
    assert attempts_of(env, inquiry["id"]) == []
    assert (await pending_intents(env, sender.worker)).intents == ()

    # The settings-level kill switch holds as well (independent of the workspace row).
    switched = with_settings(env, runtime_settings(_db_url(env), seller_inquiry_kill_switch=True))
    expire_job_wait(env, send["id"])
    reports = await work(switched, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["SEND_HELD_PAUSED"]
    assert attempts_of(env, inquiry["id"]) == []
    no_approval_state(env)


# --------------------------------------------------------------------------------------------
# (e) rolling caps
# --------------------------------------------------------------------------------------------


async def test_third_candidate_within_24h_waits_for_the_window(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    # TEST ARRANGEMENT: two inquiries of this workspace reserved earlier today (through the
    # repository, other sellers) -- the owner's cap is 2 per 24 hours.
    earlier = []
    for _ in range(2):
        vehicle = await add_vehicle(env.ctx.db, env.seed, env.workspace_id)
        world = World(
            workspace_id=env.workspace_id, seed=env.seed, sender_binding_id=sender.binding_id, vehicle=vehicle
        )
        earlier.append(await reserve_and_queue(env.ctx.db, world, vehicle))
    assert debits_of(env) == 2

    listing = await _planned(env)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "queued" and plan["attempts"] == 0  # released, never failed
    assert plan["last_error_code"] == "INQUIRY_WAIT_RATE_CAP_REACHED"
    assert plan["available_at"] > datetime.now(UTC) + timedelta(hours=20)
    live = [i for i in inquiries_of(env) if i["qualification_listing_id"] == listing["id"]]
    assert len(live) == 1 and live[0]["state"] not in (InquiryState.RESERVED, InquiryState.QUEUED)
    assert debits_of(env) == 2 and jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []

    # More passes before the window: nothing is claimed, nothing is dead-lettered.
    assert await work(env, JobType.SELLER_INQUIRY_PLAN) == []
    assert jobs_of(env, JobType.SELLER_INQUIRY_PLAN)[0]["state"] == "queued"

    # 25 hours later (TEST ARRANGEMENT: the two debits age out) the candidate is reserved.
    for record in earlier:
        backdate(env.seed.conn, record.id, to=datetime.now(UTC) - timedelta(hours=25))
    expire_job_wait(env, plan["id"])
    await work(env, JobType.SELLER_INQUIRY_PLAN)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "succeeded" and plan["result_reference"]["outcome"] == "reserved"
    assert debits_of(env) == 3
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued"
    no_approval_state(env)


# --------------------------------------------------------------------------------------------
# (g) desktop worker heartbeat
# --------------------------------------------------------------------------------------------


async def test_stale_worker_heartbeat_holds_the_send(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    await _planned(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED

    age_heartbeat(env, sender.worker, minutes=30)
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["WORKER_HEARTBEAT_STALE"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued" and send["attempts"] == 0
    assert attempts_of(env, inquiry["id"]) == []
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED

    # The worker comes back: the next run publishes exactly one intent.
    await heartbeat(env, sender.worker)
    expire_job_wait(env, send["id"])
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["intent_published"]
    [intent] = (await pending_intents(env, sender.worker)).intents
    assert intent.inquiry_id == inquiry["id"]
    assert len(attempts_of(env, inquiry["id"])) == 1


async def test_missing_desktop_worker_holds_the_send(env: PipelineEnv) -> None:
    await prepare_sender(env, desktop_worker=False)
    await _planned(env)
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["MAILBOX_WORKER_MISSING"]
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and attempts_of(env, inquiry["id"]) == []


# --------------------------------------------------------------------------------------------
# Idempotent planning and the reconciliation sweep
# --------------------------------------------------------------------------------------------


async def test_one_plan_per_revision_and_the_contact_sweep(env: PipelineEnv, tmp_path: Path) -> None:
    del tmp_path
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["result_reference"]["outcome"] == "seller_not_linked"
    assert inquiries_of(env) == []

    # The same revision is never planned twice by the pipeline hook.
    async with env.ctx.db.transaction(env.system) as conn:
        again = await enqueue_plan_job(
            conn,
            env.system,
            listing_id=listing["id"],
            revision_id=listing["current_revision_id"],
            reason="valuation",
        )
    assert again is None

    # Without seller evidence the sweep plans nothing; with it, exactly once.
    reconciler = Reconciler(env.ctx)
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_plan_jobs == 0 and report.errors == []
    await link_seller(env, listing)
    dry = await reconciler.reconcile_workspace(env.workspace_id, dry_run=True)
    assert dry.inquiry_plan_jobs == 1
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_PLAN)) == 1  # the dry run committed nothing
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_plan_jobs == 1
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_plan_jobs == 0
    await work(env, JobType.SELLER_INQUIRY_PLAN, JobType.SELLER_INQUIRY_SEND)
    plans = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert [p["result_reference"]["outcome"] for p in plans] == ["seller_not_linked", "reserved"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["result_reference"]["outcome"] == "intent_published"
    # An inquiry record now exists: no further plan jobs.
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_plan_jobs == 0


async def test_reconciliation_skips_a_workspace_without_business_configuration(
    db_url: str, seed: Seed, env: PipelineEnv
) -> None:
    """A workspace whose configuration revision is not a business configuration is skipped by
    the plan sweep and the configuration invalidation (no VALIDATION_ERROR step)."""
    del db_url
    bare = seed.workspace("Runtime bare workspace")
    seed.config_revision(bare)
    report = await Reconciler(env.ctx).reconcile_workspace(bare, dry_run=True)
    assert report.errors == []
    assert report.inquiry_plan_jobs == 0


# --------------------------------------------------------------------------------------------
# A long owner pause: suppression of the queued work, then resume and a fresh reservation
# --------------------------------------------------------------------------------------------


async def test_long_pause_suppresses_queued_work_and_resume_reserves_again(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    await _planned(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and debits_of(env) == 1
    await pull_kill_switch(env)
    # The pause has lasted longer than the hold window (PROPOSED default 24 h; 0 here).
    rt = inquiry_runtime(env.ctx)
    rt.options = InquiryRuntimeOptions(max_send_hold=timedelta(0))
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["suppressed"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "succeeded" and send["result_reference"]["outcome"] == "suppressed"
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SUPPRESSED
    assert debits_of(env) == 0 and attempts_of(env, inquiry["id"]) == []  # never transmitted
    assert (await pending_intents(env, sender.worker)).intents == ()

    # The owner resumes: re-qualified (nothing sent by the resume itself) ...
    owner = env.owner

    async def resume(conn: Conn) -> None:
        controls = await inquiries_repo.get_controls(conn, owner)
        assert controls is not None
        await inquiries_repo.resume(
            conn, owner, expected_version=controls.version, reason="owner resumed inquiries"
        )

    await run(env.ctx, owner, resume)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUALIFYING
    # ... and the next reconciliation pass decides it again: a NEW reservation with its own send job.
    report = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert report.inquiry_replan_jobs == 1 and report.errors == []
    await work(env)
    sends = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert [s["result_reference"]["outcome"] for s in sends] == ["suppressed", "intent_published"]
    assert sends[0]["dedup_key"] != sends[1]["dedup_key"]
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING and debits_of(env) == 1
    assert len(attempts_of(env, inquiry["id"])) == 1
    no_approval_state(env)
