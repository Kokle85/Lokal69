"""Inquiry controls and operator views hardened in work package C1 (items 2, 3, 10 and 11).

- Item 2: the seller cooldown is at least 7 days (``set_limits``, ``RateCapPolicy`` and the
  database CHECK of migration ``20261008000200``, whose backfill raises shorter rows first).
- Item 3: ``set_mode('automatic')`` refuses, naming every missing technical prerequisite, unless
  the configured sender binding is usable and the standing authorization is active; pausing and
  disabling are always allowed. The control view reports both.
- Item 10: ``jobs.resolve_blocked`` closes a reconciled ``EMAIL_DELIVERY_UNCERTAIN`` send job
  (audited) and refuses while the inquiry is uncertain.
- Item 11: inquiry summaries carry a typed waiting reason and ``attention_only`` includes waits.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    AUTHORIZATION,
    SENDER_ACCOUNT,
    SENDER_ADDRESS,
    SENDER_NAME,
    World,
    dispatch,
    now_utc,
    owner,
    reserve_and_queue,
    scalar,
    system,
)
from tests.integration.v11_inquiries.test_send_intents import _intent, _worker

from suv_deals.api.schemas import InquiryListQuery
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import EmailProviderKind, InquiryState, JobState, JobType, Role
from suv_deals.domain.inquiries import AuthorizationRevocation, ReconciliationEvidence
from suv_deals.errors import EmailDeliveryUncertain, Forbidden, ValidationFailed, VersionConflict
from suv_deals.persistence import inquiries_repo, jobs, queries, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.inquiries_repo import AutomaticModePrerequisitesMissing
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "supabase" / "migrations" / "20261008000200_inquiry_hardening.sql"
CURSOR_SECRET = b"synthetic-query-cursor-key-c1-0001-abcdefghijklmnop"


def _reviewer(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.REVIEWER,
        scopes=ROLE_SCOPES[Role.REVIEWER],
        request_id="req-reviewer",
        display_name="Synthetic reviewer",
    )


async def _controls(db: Database, ws: UUID) -> inquiries_repo.InquiryControls:
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        controls = await inquiries_repo.get_controls(conn, actor)
    assert controls is not None
    return controls


# ---------------------------------------------------------------------------------------------
# Item 2: the 7-day seller cooldown floor
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("days", [0, 1, 6])
async def test_set_limits_refuses_a_cooldown_below_seven_days(db: Database, world: World, days: int) -> None:
    boss = owner(world.workspace_id)
    controls = await _controls(db, world.workspace_id)
    with pytest.raises(ValidationFailed) as exc:
        async with unit_of_work(db, boss) as conn:
            await inquiries_repo.set_limits(
                conn,
                boss,
                expected_version=controls.version,
                max_per_24h=2,
                max_per_15d=5,
                seller_cooldown=timedelta(days=days, hours=23),
                reason="shorten the cooldown (synthetic)",
            )
    assert exc.value.details["reason"] == "seller_cooldown_out_of_range"
    assert (await _controls(db, world.workspace_id)).seller_cooldown == timedelta(days=7)


async def test_set_limits_may_widen_the_cooldown(db: Database, world: World) -> None:
    boss = owner(world.workspace_id)
    controls = await _controls(db, world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        updated = await inquiries_repo.set_limits(
            conn,
            boss,
            expected_version=controls.version,
            max_per_24h=1,
            max_per_15d=3,
            seller_cooldown=timedelta(days=30),
            reason="widen the cooldown (synthetic)",
        )
        view = await inquiries_repo.control_view(conn, boss)
    assert updated.seller_cooldown == timedelta(days=30)
    assert view.seller_cooldown_seconds == 30 * 86_400
    with pytest.raises(ValidationFailed):
        async with unit_of_work(db, boss) as conn:
            await inquiries_repo.set_limits(
                conn,
                boss,
                expected_version=updated.version,
                max_per_24h=1,
                max_per_15d=3,
                seller_cooldown=timedelta(days=366),
                reason="too long (synthetic)",
            )


def test_database_check_enforces_the_seven_day_floor(seed: Seed, world: World) -> None:
    conn = seed.conn
    for value in ("6 days 23 hours", "1 hour", "366 days"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "update app.seller_inquiry_controls set seller_cooldown = %s::interval,"
                " version = version + 1 where workspace_id = %s",
                (value, world.workspace_id),
            )
    with conn.transaction():
        conn.execute(
            "update app.seller_inquiry_controls set seller_cooldown = interval '8 days',"
            " version = version + 1 where workspace_id = %s",
            (world.workspace_id,),
        )
    constraint = conn.execute(
        "select pg_get_constraintdef(oid) from pg_constraint"
        " where conname = 'seller_inquiry_controls_cooldown_ck'"
    ).fetchone()
    assert constraint is not None and "7 days" in constraint[0] and "365 days" in constraint[0]


def _section_one() -> str:
    text = MIGRATION.read_text(encoding="ascii")
    start = text.index("-- 1. Seller cooldown floor (7..365 days)")
    end = text.index("-- 2. Seller-reply signal status (flood control)")
    body = text[start:end]
    return "\n".join(line for line in body.splitlines() if not re.match(r"^-- -{10,}$", line))


def test_migration_backfill_raises_short_cooldowns_before_validating(seed: Seed, world: World) -> None:
    """Replays section 1 of the migration on a legacy 1-day row (rolled back afterwards)."""
    conn = seed.conn
    try:
        with conn.transaction():
            conn.execute(
                "alter table app.seller_inquiry_controls drop constraint seller_inquiry_controls_cooldown_ck"
            )
            conn.execute(
                "update app.seller_inquiry_controls set seller_cooldown = interval '1 day',"
                " version = version + 1 where workspace_id = %s",
                (world.workspace_id,),
            )
            before = conn.execute(
                "select version from app.seller_inquiry_controls where workspace_id = %s",
                (world.workspace_id,),
            ).fetchone()
            assert before is not None
            conn.execute(_section_one())  # type: ignore[arg-type]
            after = conn.execute(
                "select seller_cooldown, version, update_reason from app.seller_inquiry_controls"
                " where workspace_id = %s",
                (world.workspace_id,),
            ).fetchone()
            assert after is not None
            assert after[0] == timedelta(days=7) and after[1] == before[0] + 1
            assert "7-day floor" in after[2]
            raise _Undo
    except _Undo:
        pass
    still = conn.execute(
        "select count(*) from pg_constraint where conname = 'seller_inquiry_controls_cooldown_ck'"
    ).fetchone()
    assert still == (1,)


class _Undo(Exception):
    """Rolls the arranged transaction back."""


# ---------------------------------------------------------------------------------------------
# Item 3: automatic mode only with a usable sender and an active authorization
# ---------------------------------------------------------------------------------------------


async def _bare_workspace(
    db: Database, seed: Seed, *, authorize: bool, verified: bool, health: str = "healthy"
) -> tuple[UUID, UUID]:
    ws = seed.workspace(f"C1 mode {uuid.uuid4().hex[:6]}")
    seed.profile(ws, "primary")
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.ensure_controls(conn, actor)
        if authorize:
            await inquiries_repo.record_authorization(conn, actor, AUTHORIZATION, reason="standing v1")
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id=SENDER_ACCOUNT,
            from_address=SENDER_ADDRESS,
            display_name=SENDER_NAME,
            reason="owner-authorized sending identity (synthetic)",
        )
        if verified:
            binding = await sender_bindings_repo.record_verification(
                conn,
                actor,
                binding.id,
                expected_version=binding.version,
                verified=True,
                alias_verified=True,
                health=health,  # type: ignore[arg-type]
                reason="technical verification passed (synthetic)",
            )
    return ws, binding.id


async def _set_mode(
    db: Database, ws: UUID, mode: str, *, sender_binding_id: UUID | None = None
) -> inquiries_repo.InquiryControls:
    boss = owner(ws)
    controls = await _controls(db, ws)
    async with unit_of_work(db, boss) as conn:
        return await inquiries_repo.set_mode(
            conn,
            boss,
            expected_version=controls.version,
            mode=mode,  # type: ignore[arg-type]
            reason="mode change (synthetic)",
            sender_binding_id=sender_binding_id,
        )


async def test_automatic_mode_names_every_missing_prerequisite(db: Database, seed: Seed) -> None:
    ws, _binding = await _bare_workspace(db, seed, authorize=False, verified=False)
    with pytest.raises(AutomaticModePrerequisitesMissing) as exc:
        await _set_mode(db, ws, "automatic")
    assert exc.value.details["missing"] == [
        "sender_binding_unverified",
        "sender_alias_unverified",
        "sender_binding_unhealthy",
        "standing_authorization_missing",
    ]
    assert (await _controls(db, ws)).mode == "disabled_until_sender_ready"
    boss = owner(ws)
    async with unit_of_work(db, boss) as conn:
        view = await inquiries_repo.control_view(conn, boss)
    assert view.authorization_status == "missing" and view.sender_readiness == "unverified"
    assert view.sender_provider == EmailProviderKind.OUTLOOK_LOCAL
    assert view.sender_problems == (
        "sender_binding_unverified",
        "sender_alias_unverified",
        "sender_binding_unhealthy",
    )
    # Pausing and disabling never wait for anything.
    assert (await _set_mode(db, ws, "paused")).mode == "paused"
    assert (await _set_mode(db, ws, "disabled_until_sender_ready")).mode == "disabled_until_sender_ready"


async def test_automatic_mode_refuses_an_unhealthy_or_revoked_sender(db: Database, seed: Seed) -> None:
    ws, binding_id = await _bare_workspace(db, seed, authorize=True, verified=True, health="degraded")
    with pytest.raises(AutomaticModePrerequisitesMissing) as unhealthy:
        await _set_mode(db, ws, "automatic")
    assert unhealthy.value.details["missing"] == ["sender_binding_unhealthy"]
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        await sender_bindings_repo.revoke_binding(conn, actor, binding_id, reason="revoked (synthetic)")
    with pytest.raises(AutomaticModePrerequisitesMissing) as revoked:
        await _set_mode(db, ws, "automatic", sender_binding_id=binding_id)
    assert revoked.value.details["missing"] == ["sender_binding_revoked"]
    with pytest.raises(AutomaticModePrerequisitesMissing) as missing:
        await _set_mode(db, ws, "automatic")  # no unrevoked binding left
    assert missing.value.details["missing"] == ["sender_binding_missing"]


async def test_automatic_mode_refuses_a_revoked_authorization(db: Database, seed: Seed) -> None:
    ws, _binding = await _bare_workspace(db, seed, authorize=True, verified=True)
    assert (await _set_mode(db, ws, "automatic")).mode == "automatic"
    assert (await _set_mode(db, ws, "paused")).mode == "paused"
    actor = system(ws)
    revoked = AUTHORIZATION.model_copy(
        update={
            "version": AUTHORIZATION.version + 1,
            "revocation": AuthorizationRevocation(
                revoked=True,
                revoked_at=datetime.now(UTC),
                revoked_by="Synthetic owner",
                reason="revoked for a test (synthetic)",
            ),
        }
    )
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.record_authorization(conn, actor, revoked, reason="revoked (synthetic)")
    with pytest.raises(AutomaticModePrerequisitesMissing) as exc:
        await _set_mode(db, ws, "automatic")
    assert exc.value.details["missing"] == ["standing_authorization_revoked"]
    boss = owner(ws)
    async with unit_of_work(db, boss) as conn:
        view = await inquiries_repo.control_view(conn, boss)
    assert view.authorization_status == "revoked" and view.authorization_version == 2
    assert view.sender_readiness == "ready" and view.sender_problems == ()


async def test_control_view_reports_a_ready_configuration(db: Database, world: World) -> None:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        view = await inquiries_repo.control_view(conn, boss, sender_binding_id=world.sender_binding_id)
    assert view.mode == "automatic" and view.authorization_status == "active"
    assert view.authorization_version == 1 and view.sender_readiness == "ready"
    assert view.sender_binding_version is not None and view.seller_cooldown_seconds == 7 * 86_400


# ---------------------------------------------------------------------------------------------
# Item 10: resolving a reconciled EMAIL_DELIVERY_UNCERTAIN send job
# ---------------------------------------------------------------------------------------------


def _expire(conn: psycopg.Connection, attempt_id: UUID) -> None:
    """TEST ARRANGEMENT ONLY: the attempt's lease ran out (the worker crashed after the hand-over)."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts set lease_expires_at = now() - interval '1 second'"
            " where attempt_id = %s",
            (attempt_id,),
        )


async def _blocked_send_job(db: Database, seed: Seed, world: World) -> tuple[UUID, UUID]:
    ws = world.workspace_id
    record = await reserve_and_queue(db, world)
    result = await dispatch(db, world, record.id)
    assert result.attempt is not None
    job_id = seed.job(
        ws,
        job_type=JobType.SELLER_INQUIRY_SEND.value,
        state="running",
        attempts=1,
        lease_owner="c1-crashed-worker",
        lease_token=uuid.uuid4(),
        lease_expires_at=now_utc() - timedelta(seconds=5),
        payload={"inquiry_id": str(record.id)},
    )
    _expire(seed.conn, result.attempt.attempt_id)
    assert await inquiries_repo.reap_expired_attempts(db, ws) == (result.attempt.attempt_id,)
    reaped = await jobs.reap_expired(db, ws, retry_delay_seconds=0)
    assert reaped.blocked_uncertain == (job_id,)
    return record.id, job_id


async def test_resolve_blocked_send_job_after_reconciliation(db: Database, seed: Seed, world: World) -> None:
    inquiry_id, job_id = await _blocked_send_job(db, seed, world)
    actor = system(world.workspace_id)
    # Refused while the inquiry is uncertain: the e-mail may have left.
    with pytest.raises(EmailDeliveryUncertain) as uncertain:
        async with unit_of_work(db, actor) as conn:
            await jobs.resolve_blocked(
                conn, actor, job_id, outcome="succeeded", reason="too early (synthetic)"
            )
    assert uncertain.value.details["reason"] == "inquiry_still_uncertain"
    # A reviewer may not resolve jobs at all.
    reviewer = _reviewer(world.workspace_id)
    with pytest.raises(Forbidden):
        async with unit_of_work(db, reviewer) as conn:
            await jobs.resolve_blocked(conn, reviewer, job_id, outcome="succeeded", reason="not mine")

    async with unit_of_work(db, actor) as conn:
        found = await inquiries_repo.reconcile(
            conn, actor, inquiry_id, evidence=ReconciliationEvidence(sent_items="found")
        )
    assert found.inquiry_state == InquiryState.ACCEPTED
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        resolved = await jobs.resolve_blocked(
            conn, boss, job_id, outcome="succeeded", reason="found in Sent Items (synthetic)"
        )
    assert resolved.state == JobState.SUCCEEDED and resolved.blocker_code is None
    assert resolved.last_error_code == jobs.RESOLVED_AFTER_RECONCILIATION
    audited = await scalar(
        db,
        world,
        "select count(*) from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s"
        " and action = 'job.resolve_blocked'",
        {"id": job_id},
    )
    assert audited == 1
    # A resolved job is no longer blocked: a second resolve conflicts.
    with pytest.raises(VersionConflict) as again:
        async with unit_of_work(db, boss) as conn:
            await jobs.resolve_blocked(conn, boss, job_id, outcome="cancelled", reason="again (synthetic)")
    assert again.value.details["reason"] == "not_an_uncertain_send_job"


async def test_resolve_blocked_refuses_other_jobs(db: Database, seed: Seed, world: World) -> None:
    other = seed.job(
        world.workspace_id,
        job_type="valuation",
        state="blocked",
        blocker_code="SOME_BLOCKER",
        payload={"inquiry_id": str(uuid.uuid4())},
    )
    boss = owner(world.workspace_id)
    with pytest.raises(VersionConflict) as exc:
        async with unit_of_work(db, boss) as conn:
            await jobs.resolve_blocked(conn, boss, other, outcome="cancelled", reason="not a send job")
    assert exc.value.details["reason"] == "not_an_uncertain_send_job"


# ---------------------------------------------------------------------------------------------
# Item 11: typed waiting reasons and attention_only
# ---------------------------------------------------------------------------------------------


async def _summaries(db: Database, world: World, *, attention_only: bool = False) -> dict[UUID, str | None]:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        page = await queries.list_inquiries(
            conn, boss, InquiryListQuery(limit=100), secret=CURSOR_SECRET, attention_only=attention_only
        )
    return {item.inquiry_id: item.waiting_reason for item in page.data.items}


async def test_waiting_reasons_and_attention(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    queued = await reserve_and_queue(db, world)
    # A send job waiting for the rate cap.
    seed.job(
        ws,
        job_type=JobType.SELLER_INQUIRY_SEND.value,
        state="retry_wait",
        attempts=1,
        available_at=now_utc() + timedelta(hours=3),
        last_error_code="INQUIRY_WAIT_RATE_CAP_REACHED",
        listing_id=world.listing_id,
        payload={"inquiry_id": str(queued.id)},
    )
    plain = await _summaries(db, world)
    assert plain[queued.id] == "RATE_CAP_REACHED"
    attention = await _summaries(db, world, attention_only=True)
    assert attention.get(queued.id) == "RATE_CAP_REACHED"  # a waiting inquiry needs attention

    boss = owner(ws)
    async with unit_of_work(db, boss) as conn:
        detail = await queries.get_inquiry(conn, boss, queued.id)
    assert detail.data.state == InquiryState.QUEUED

    controls = await _controls(db, ws)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.pause(conn, boss, expected_version=controls.version, reason="stop (synthetic)")
    paused = await _summaries(db, world, attention_only=True)
    assert paused[queued.id] == "INQUIRIES_PAUSED"


async def test_uncertain_delivery_wait(db: Database, seed: Seed, world: World) -> None:
    inquiry_id, _job = await _blocked_send_job(db, seed, world)
    summaries = await _summaries(db, world, attention_only=True)
    assert summaries[inquiry_id] == "UNCERTAIN_DELIVERY"


async def test_worker_offline_wait_for_a_running_desktop_intent(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, _intent_record = await _intent(db, world, worker)
    # The intent is committed (``sending``) but the desktop worker never sent a heartbeat.
    summaries = await _summaries(db, world, attention_only=True)
    assert summaries[inquiry_id] == "WORKER_OFFLINE"
