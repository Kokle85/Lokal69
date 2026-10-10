"""Work package C2 operator commands (marker ``db`` unless noted): ``jobs blocked | unblock |
resolve-blocked``, ``inquiries resume --expected-suppressions``, ``inquiries set-mode`` against the
CONFIGURED sender binding, the grouped ``reconcile`` report, ``canary prepare | status | cancel |
send`` and the v1.1 ``doctor`` additions (secret reference, canary evidence).

Everything is SYNTHETIC (``example.invalid`` / ``*.example`` addresses); nothing reaches a network
and nothing is ever sent: ``canary send`` is exercised only up to its refusals and, once, with an
in-memory fake transport injected into ``CANARY_TRANSPORTS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, TypeVar
from uuid import UUID

import pytest
from tests.api.v11_support import issue_worker, outlook_world, owner_actor
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    AUTHORIZATION,
    SENDER_ACCOUNT,
    SENDER_ADDRESS,
    World,
    dispatch,
    now_utc,
    reserve_and_queue,
    system,
)

from suv_deals.cli_commands import canary as canary_cli
from suv_deals.domain.enums import EmailProviderKind, InquiryState, JobType, SuppressionReason
from suv_deals.domain.inquiries import ReconciliationEvidence, SellerInquiryAuthorization
from suv_deals.persistence import canaries_repo, inquiries_repo, jobs, mail_workers_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.reconciliation import ReconcileReport

pytestmark = pytest.mark.db

T = TypeVar("T")
TARGET = "activation-canary@owner-test.example.invalid"


def _run[T](db_url: str, work: Callable[[Database], Awaitable[T]]) -> T:
    """Arrange with the repositories in a private event loop (the CLI runs its own loop)."""

    async def main() -> T:
        db = Database(db_url, set_role="suv_backend", min_size=1, max_size=4)
        await db.open()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(main())


def _json(output: str) -> Any:
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


@pytest.fixture
def world(db_url: str, seed: Seed) -> Iterator[World]:
    built = _run(db_url, lambda db: outlook_world(db, seed, "CLI C2"))
    try:
        yield built
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (built.workspace_id,))


@pytest.fixture
def configured(db_env: dict[str, str]) -> dict[str, str]:
    """The configured sending identity of the synthetic world (outlook_local is the default)."""
    return {**db_env, "SELLER_EMAIL_ACCOUNT_ID": SENDER_ACCOUNT, "SELLER_EMAIL_FROM": SENDER_ADDRESS}


def _owner(seed: Seed, workspace_id: UUID) -> UUID:
    user = seed.user()
    seed.membership(workspace_id, user, "owner")
    return user


# ---------------------------------------------------------------------------------- jobs


def _expire_attempt(seed: Seed, attempt_id: UUID) -> None:
    """TEST ARRANGEMENT ONLY: the attempt's lease ran out (the worker crashed after the hand-over)."""
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.email_delivery_attempts set lease_expires_at = now() - interval '1 second'"
            " where attempt_id = %s",
            (attempt_id,),
        )


def _blocked_send_job(db_url: str, seed: Seed, world: World) -> tuple[UUID, UUID]:
    """A reaped ``seller_inquiry_send`` job blocked ``EMAIL_DELIVERY_UNCERTAIN`` (its e-mail may
    have left) and its now ``uncertain`` inquiry."""
    ws = world.workspace_id

    async def dispatched(db: Database) -> tuple[UUID, UUID]:
        record = await reserve_and_queue(db, world)
        result = await dispatch(db, world, record.id)
        assert result.attempt is not None
        return record.id, result.attempt.attempt_id

    inquiry_id, attempt_id = _run(db_url, dispatched)
    job_id = seed.job(
        ws,
        job_type=JobType.SELLER_INQUIRY_SEND.value,
        state="running",
        attempts=1,
        lease_owner="c2-crashed-worker",
        lease_token=uuid.uuid4(),
        lease_expires_at=now_utc() - timedelta(seconds=5),
        payload={"inquiry_id": str(inquiry_id)},
    )
    _expire_attempt(seed, attempt_id)

    async def reap(db: Database) -> None:
        assert await inquiries_repo.reap_expired_attempts(db, ws) == (attempt_id,)
        reaped = await jobs.reap_expired(db, ws, retry_delay_seconds=0)
        assert reaped.blocked_uncertain == (job_id,)

    _run(db_url, reap)
    return inquiry_id, job_id


def test_jobs_blocked_unblock_needs_the_owners_acknowledgement(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    _inquiry, job_id = _blocked_send_job(db_url, seed, world)
    other = seed.job(world.workspace_id, state="blocked", blocker_code="SYNTHETIC_BLOCKER")
    listed = run_cli("jobs", "blocked", "--workspace", ws, "--json", env=db_env)
    assert listed.exit_code == 0, listed.output
    rows = {r["id"]: r for r in _json(listed.output)}
    assert rows[str(job_id)]["blocker"] == "EMAIL_DELIVERY_UNCERTAIN"
    assert rows[str(job_id)]["inquiry_id"] == str(_inquiry)
    assert rows[str(other)]["inquiry_id"] is None
    unblock = ["jobs", "unblock", str(job_id), "--workspace", ws, "--reason", "Owner checked Outlook"]
    assert run_cli(*unblock, env=db_env).exit_code == 3  # --yes required
    refused = run_cli(*unblock, "--yes", env=db_env)
    assert refused.exit_code == 1 and "EMAIL_DELIVERY_UNCERTAIN" in refused.output
    no_owner = run_cli(*unblock, "--acknowledge-uncertain-delivery", "--yes", env=db_env)
    assert no_owner.exit_code == 2  # the acknowledgement is an owner decision
    stranger = seed.user()
    not_owner = run_cli(
        *unblock, "--acknowledge-uncertain-delivery", "--owner-user-id", str(stranger), "--yes", env=db_env
    )
    assert not_owner.exit_code == 1
    assert seed.conn.execute("select state from ops.jobs where id = %s", (job_id,)).fetchone() == ("blocked",)
    owner = _owner(seed, world.workspace_id)
    done = run_cli(
        *unblock, "--acknowledge-uncertain-delivery", "--owner-user-id", str(owner), "--yes", env=db_env
    )
    assert done.exit_code == 0, done.output
    assert "state queued" in done.output
    audit = seed.conn.execute(
        "select actor_principal_id, metadata ->> 'acknowledged_uncertain_delivery' from ops.audit_events"
        " where workspace_id = %s and target_id = %s and action = 'job.unblock'",
        (world.workspace_id, job_id),
    ).fetchall()
    assert audit == [(owner, "true")]  # the audit names the owner who acknowledged
    plain = run_cli(
        "jobs", "unblock", str(other), "--workspace", ws, "--reason", "Retry it", "--yes", env=db_env
    )
    assert plain.exit_code == 0, plain.output


def test_jobs_resolve_blocked_only_after_reconciliation(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    inquiry_id, job_id = _blocked_send_job(db_url, seed, world)
    resolve = ["jobs", "resolve-blocked", str(job_id), "--workspace", ws, "--outcome", "succeeded"]
    resolve += ["--reason", "Found in Sent Items (synthetic)"]
    assert run_cli(*resolve, env=db_env).exit_code == 3  # --yes required
    early = run_cli(*resolve, "--yes", env=db_env)
    assert early.exit_code == 1 and "still uncertain" in early.output

    async def reconcile(db: Database) -> None:
        actor = system(world.workspace_id)
        async with unit_of_work(db, actor) as conn:
            found = await inquiries_repo.reconcile(
                conn, actor, inquiry_id, evidence=ReconciliationEvidence(sent_items="found")
            )
        assert found.inquiry_state == InquiryState.ACCEPTED

    _run(db_url, reconcile)
    done = run_cli(*resolve, "--yes", env=db_env)
    assert done.exit_code == 0, done.output
    assert "succeeded" in done.output and "RESOLVED_AFTER_RECONCILIATION" in done.output
    state = seed.conn.execute("select state, blocker_code from ops.jobs where id = %s", (job_id,)).fetchone()
    assert state == ("succeeded", None)  # closed, never re-queued
    again = run_cli(*resolve, "--yes", env=db_env)
    assert again.exit_code == 1


# ------------------------------------------------------------------------- inquiries resume / set-mode


def _suppress(db_url: str, world: World, reason: SuppressionReason) -> None:
    async def add(db: Database) -> None:
        admin = owner_actor(world.workspace_id)
        async with unit_of_work(db, admin) as conn:
            await inquiries_repo.add_suppression(
                conn,
                admin,
                scope="workspace",
                key=str(world.workspace_id),
                reason=reason,
                evidence={"synthetic": True},
            )

    _run(db_url, add)


def _status(run_cli: Cli, env: dict[str, str], ws: str) -> dict[str, Any]:
    result = run_cli("inquiries", "status", "--workspace", ws, "--json", env=env)
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = _json(result.output)
    return data


def test_resume_removes_only_the_suppressions_the_owner_saw(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    owner = _owner(seed, world.workspace_id)
    _suppress(db_url, world, SuppressionReason.KILL_SWITCH)
    seen = _status(run_cli, db_env, ws)
    assert seen["removable_suppressions"] == 1
    resume = ["inquiries", "resume", "--workspace", ws, "--reason", "Owner resume (synthetic)"]
    resume += ["--expected-version", str(seen["version"]), "--owner-user-id", str(owner)]
    missing = run_cli(*resume, "--remove-suppressions", "--yes", env=db_env)
    assert missing.exit_code == 2 and "--expected-suppressions" in missing.output
    stray = run_cli(*resume, "--expected-suppressions", "1", "--yes", env=db_env)
    assert stray.exit_code == 2  # the count names suppressions to remove: only with removal
    # Another removable suppression appears after the owner looked: the whole resume is refused.
    _suppress(db_url, world, SuppressionReason.AUTHORIZATION_REVOKED)
    args = [*resume, "--remove-suppressions", "--expected-suppressions", "1", "--yes"]
    refused = run_cli(*args, env=db_env)
    assert refused.exit_code == 1, refused.output
    assert "2 removable suppression(s) now, 1 expected" in refused.output
    after = _status(run_cli, db_env, ws)
    assert after["version"] == seen["version"] and after["removable_suppressions"] == 2
    active = seed.conn.execute(
        "select count(*) from ops.email_suppressions where workspace_id = %s and removed_at is null",
        (world.workspace_id,),
    ).fetchone()
    assert active == (2,)
    args = [*resume, "--remove-suppressions", "--expected-suppressions", "2", "--yes"]
    done = run_cli(*args, env=db_env)
    assert done.exit_code == 0, done.output
    assert "Removed 2 suppression(s)" in done.output


def test_set_mode_automatic_checks_the_configured_binding_and_lists_what_is_missing(
    run_cli: Cli, configured: dict[str, str], db_url: str, world: World
) -> None:
    ws = str(world.workspace_id)

    async def newer_unverified(db: Database) -> None:
        admin = owner_actor(world.workspace_id)
        async with unit_of_work(db, admin) as conn:
            await sender_bindings_repo.create_binding(
                conn,
                admin,
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                account_id="synthetic-other-account",
                from_address="other-sender@synthetic-mail.example",
                display_name="Other Synthetic Sender",
                reply_to_address=None,
                reason="C2 test: a newer, unverified binding of another account",
            )

    _run(db_url, newer_unverified)
    version = _status(run_cli, configured, ws)["version"]
    paused = run_cli(
        "inquiries", "set-mode", "paused", "--workspace", ws, "--reason", "Owner pause (synthetic)",
        "--expected-version", str(version), "--yes", env=configured,
    )  # fmt: skip
    assert paused.exit_code == 0, paused.output
    # The CONFIGURED (verified) binding is the one checked, not the newest (unverified) one.
    automatic = run_cli(
        "inquiries", "set-mode", "automatic", "--workspace", ws, "--reason", "Owner enables (synthetic)",
        "--expected-version", str(version + 1), "--yes", env=configured,
    )  # fmt: skip
    assert automatic.exit_code == 0, automatic.output
    assert "Mode automatic" in automatic.output


def test_set_mode_automatic_names_every_missing_prerequisite(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    ws = str(workspace)
    authorize = ["inquiries", "authorize", "--workspace", ws, "--reason", "Owner standing authorization"]
    assert run_cli(*authorize, "--yes", env=db_env).exit_code == 0
    version = _status(run_cli, db_env, ws)["version"]
    refused = run_cli(
        "inquiries", "set-mode", "automatic", "--workspace", ws, "--reason", "Too early (synthetic)",
        "--expected-version", str(version), "--yes", env=db_env,
    )  # fmt: skip
    assert refused.exit_code == 3, refused.output
    assert "missing: sender_binding_missing" in refused.output
    assert _status(run_cli, db_env, ws)["mode"] == "disabled_until_sender_ready"


# ---------------------------------------------------------------------------------- reconcile


def test_reconcile_text_report_groups_every_counter(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    result = run_cli("reconcile", "--workspace", str(workspace), "--dry-run", env=db_env)
    assert result.exit_code == 0, result.output
    lines = {line.strip().split(":", 1)[0]: line for line in result.output.splitlines() if ":" in line}
    assert "inquiry_retry_jobs=0" in lines["inquiries"]
    assert "inquiry_send_jobs_unblocked=0" in lines["inquiries"]
    assert "reply_process_jobs=0" in lines["replies"]
    counters = set(ReconcileReport.__dataclass_fields__) - {"workspace_id", "dry_run", "errors"}
    for name in counters:
        assert result.output.count(f"{name}=") == 1, name
    assert "other:" not in result.output  # every counter has its group


# ---------------------------------------------------------------------------------- canary


def _set_kill_switch(db_url: str, world: World, *, on: bool) -> None:
    async def flip(db: Database) -> None:
        admin = owner_actor(world.workspace_id)
        async with unit_of_work(db, admin) as conn:
            controls = await inquiries_repo.get_controls(conn, admin)
            assert controls is not None
            if on:
                await inquiries_repo.pause(
                    conn, admin, expected_version=controls.version, reason="Owner pause"
                )
            else:
                await inquiries_repo.resume(
                    conn, admin, expected_version=controls.version, reason="Owner resume"
                )

    _run(db_url, flip)


def _canary_env(configured: dict[str, str], **extra: str) -> dict[str, str]:
    return {**configured, "SUV_CANARY_TARGET_ADDRESS": TARGET, **extra}


def _prepare(run_cli: Cli, env: dict[str, str], ws: str) -> str:
    result = run_cli(
        "canary", "prepare", "--workspace", ws, "--purpose", "Activation route check (synthetic)", "--yes",
        env=env,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert TARGET not in result.output and "owner-test" not in result.output
    assert hashlib.sha256(TARGET.encode()).hexdigest() not in result.output
    line = next(line for line in result.output.splitlines() if line.startswith("Prepared canary "))
    return line.split()[2]


def test_canary_prepare_status_and_cancel_never_show_the_address(
    run_cli: Cli, configured: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    _run(db_url, lambda db: issue_worker(db, world))
    without_yes = run_cli(
        "canary", "prepare", "--workspace", ws, "--purpose", "Check", env=_canary_env(configured)
    )
    assert without_yes.exit_code == 3
    no_target = run_cli(
        "canary", "prepare", "--workspace", ws, "--purpose", "Activation check", "--yes", env=configured
    )
    assert no_target.exit_code == 2 and "SUV_CANARY_TARGET_ADDRESS" in no_target.output
    unconfigured = run_cli(
        "canary", "prepare", "--workspace", ws, "--purpose", "Activation check", "--yes",
        env={**_canary_env(configured), "SELLER_EMAIL_FROM": "someone-else@synthetic-mail.example"},
    )  # fmt: skip
    assert unconfigured.exit_code == 3 and "SENDER_BINDING_MISMATCH" in unconfigured.output
    canary_id = _prepare(run_cli, _canary_env(configured), ws)
    stored = seed.conn.execute(
        "select state, target_address_hash from ops.inquiry_activation_canaries where id = %s", (canary_id,)
    ).fetchone()
    assert stored == ("prepared", hashlib.sha256(TARGET.encode()).hexdigest())
    status = run_cli("canary", "status", "--workspace", ws, "--json", env=configured)
    assert status.exit_code == 0, status.output
    data = _json(status.output)
    assert data["evidence"] == "prepared" and [c["id"] for c in data["canaries"]] == [canary_id]
    assert "target_address_hash" not in status.output and stored[1] not in status.output
    human = run_cli("canary", "status", "--workspace", ws, env=configured)
    assert "Activation canary evidence: prepared" in human.output
    cancel = ["canary", "cancel", canary_id, "--workspace", ws, "--reason", "Not needed (synthetic)"]
    assert run_cli(*cancel, env=configured).exit_code == 3
    cancelled = run_cli(*cancel, "--yes", env=configured)
    assert cancelled.exit_code == 0 and "cancelled" in cancelled.output


def test_canary_send_is_impossible_unless_every_switch_and_the_flag_are_set(
    run_cli: Cli,
    configured: dict[str, str],
    db_url: str,
    world: World,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = str(world.workspace_id)
    _run(db_url, lambda db: issue_worker(db, world))
    canary_id = _prepare(run_cli, _canary_env(configured), ws)
    calls: list[tuple[str, str]] = []

    async def fake_transport(record: Any, target: str) -> tuple[str, dict[str, Any]]:
        calls.append((str(record.id), target))
        return "accepted", {"synthetic_transport": True}

    send = ["canary", "send", canary_id, "--workspace", ws, "--yes"]
    # 1. Without the separate confirmation flag nothing is even read from the database.
    no_flag = run_cli(*send, env=_canary_env(configured, SELLER_EMAIL_CANARY_SEND_ENABLED="true"))
    assert no_flag.exit_code == 3 and "--i-confirm-owner-controlled-address" in no_flag.output
    flagged = [*send, "--i-confirm-owner-controlled-address"]
    # 2. Default switches: every closed gate is named, nothing changes.
    closed = run_cli(*flagged, env=_canary_env(configured))
    assert closed.exit_code == 3
    for code in ("CANARY_SEND_SWITCH_OFF", "SELLER_INQUIRY_MODE_NOT_AUTOMATIC"):
        assert code in closed.output
    open_env = _canary_env(
        configured, SELLER_EMAIL_CANARY_SEND_ENABLED="true", SELLER_INQUIRY_MODE="automatic"
    )
    killed = run_cli(*flagged, env={**open_env, "SELLER_INQUIRY_KILL_SWITCH": "true"})
    assert killed.exit_code == 3 and "SELLER_INQUIRY_KILL_SWITCH_ON" in killed.output
    # 3. Every switch on, but the address is not the canary's target.
    wrong = run_cli(
        *flagged, env={**open_env, "SUV_CANARY_TARGET_ADDRESS": "other@owner-test.example.invalid"}
    )
    assert wrong.exit_code == 3 and "CANARY_TARGET_MISMATCH" in wrong.output
    assert "other@owner-test" not in wrong.output
    # 4. Every gate open: no provider has a canary transport yet - the precise blocker, no send.
    unavailable = run_cli(*flagged, env=open_env)
    assert unavailable.exit_code == 3 and "CANARY_TRANSPORT_UNAVAILABLE" in unavailable.output
    assert TARGET not in unavailable.output
    assert seed.conn.execute(
        "select state, version from ops.inquiry_activation_canaries where id = %s", (canary_id,)
    ).fetchone() == ("prepared", 1)
    # 5. With an (in-memory) transport the workspace kill switch still stops it ...
    monkeypatch.setitem(canary_cli.CANARY_TRANSPORTS, "outlook_local", fake_transport)
    _set_kill_switch(db_url, world, on=True)
    paused = run_cli(*flagged, env=open_env)
    assert paused.exit_code == 3 and "KILL_SWITCH_ACTIVE" in paused.output and calls == []
    _set_kill_switch(db_url, world, on=False)
    # ... and only with every gate open is it called, exactly once, and the outcome recorded.
    sent = run_cli(*flagged, env=open_env)
    assert sent.exit_code == 0, sent.output
    assert calls == [(canary_id, TARGET)] and TARGET not in sent.output
    assert seed.conn.execute(
        "select state from ops.inquiry_activation_canaries where id = %s", (canary_id,)
    ).fetchone() == ("accepted",)
    again = run_cli(*flagged, env=open_env)
    assert again.exit_code == 3 and "CANARY_NOT_PREPARED" in again.output and len(calls) == 1


# ---------------------------------------------------------------------------------- doctor


def test_doctor_reports_the_canary_evidence_state(
    run_cli: Cli, configured: dict[str, str], db_url: str, world: World
) -> None:
    _run(db_url, lambda db: issue_worker(db, world))

    def canary_line(output: str) -> str:
        # Other test workspaces may be active: pick this workspace's line (prefixed when several).
        prefix = str(world.workspace_id)[:8]
        lines = [line for line in output.splitlines() if "activation_canary" in line]
        mine = [line for line in lines if f"/{prefix}:activation_canary" in line]
        if not mine:
            assert len(lines) == 1, output
            mine = lines
        return mine[0]

    result = run_cli("doctor", "--process", "scheduler", env=configured)
    assert "none:" in canary_line(result.output), result.output
    assert "secret_reference" not in result.output  # outlook_local holds no provider secret
    _prepare(run_cli, _canary_env(configured), str(world.workspace_id))
    again = run_cli("doctor", "--process", "scheduler", env=configured)
    assert "prepared:" in canary_line(again.output)
    assert TARGET not in again.output


def test_set_mode_automatic_never_falls_back_to_another_providers_binding(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID
) -> None:
    """The configured provider is ``outlook_local`` (the default); a verified ``gmail_api`` binding of
    the workspace is not the configured sender, so ``automatic`` is refused, never accepted on it."""
    ws = str(workspace)
    assert (
        run_cli(
            "inquiries",
            "authorize",
            "--workspace",
            ws,
            "--reason",
            "Owner authorization",
            "--yes",
            env=db_env,
        ).exit_code
        == 0
    )

    async def verified_gmail(db: Database) -> None:
        admin = owner_actor(workspace)
        async with unit_of_work(db, admin) as conn:
            record = await sender_bindings_repo.create_binding(
                conn,
                admin,
                provider=EmailProviderKind.GMAIL_API,
                account_id="gmail-inquiries@synthetic-mail.example",
                from_address="gmail-inquiries@synthetic-mail.example",
                display_name="Synthetic Sender",
                reply_to_address=None,
                reason="C2 test: a verified binding of a provider that is not configured",
            )
            await sender_bindings_repo.record_verification(
                conn,
                admin,
                record.id,
                expected_version=record.version,
                verified=True,
                alias_verified=True,
                health="healthy",
                reason="C2 test verification (synthetic)",
            )

    _run(db_url, verified_gmail)
    version = _status(run_cli, db_env, ws)["version"]
    refused = run_cli(
        "inquiries", "set-mode", "automatic", "--workspace", ws, "--reason", "Owner enables (synthetic)",
        "--expected-version", str(version), "--yes", env=db_env,
    )  # fmt: skip
    assert refused.exit_code == 3, refused.output
    assert "missing: sender_binding_missing" in refused.output
    assert _status(run_cli, db_env, ws)["mode"] == "disabled_until_sender_ready"


# ---------------------------------------------------------------------------------- C2 review r1


def test_set_mode_automatic_refuses_a_binding_that_is_not_the_configured_identity(
    run_cli: Cli, configured: dict[str, str], world: World
) -> None:
    """The workspace's verified ``outlook_local`` binding is not the CONFIGURED identity
    (``SELLER_EMAIL_FROM`` names another address): ``automatic`` is refused with the identity codes
    (the same ``sender_identity_*`` codes as the control view), never accepted with a warning."""
    ws = str(world.workspace_id)
    version = _status(run_cli, configured, ws)["version"]
    paused = run_cli(
        "inquiries", "set-mode", "paused", "--workspace", ws, "--reason", "Owner pause (synthetic)",
        "--expected-version", str(version), "--yes", env=configured,
    )  # fmt: skip
    assert paused.exit_code == 0, paused.output
    other = {**configured, "SELLER_EMAIL_FROM": "someone-else@synthetic-mail.example"}
    refused = run_cli(
        "inquiries", "set-mode", "automatic", "--workspace", ws, "--reason", "Owner enables (synthetic)",
        "--expected-version", str(version + 1), "--yes", env=other,
    )  # fmt: skip
    assert refused.exit_code == 3, refused.output
    assert "missing: sender_identity_sender_binding_mismatch" in refused.output
    assert "someone-else" not in refused.output and SENDER_ADDRESS not in refused.output
    status = _status(run_cli, configured, ws)
    assert status["mode"] == "paused" and status["version"] == version + 1  # nothing changed


def test_canary_is_committed_uncertain_before_the_transport_runs(
    run_cli: Cli,
    configured: dict[str, str],
    db_url: str,
    world: World,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 37.5: the send attempt is durably committed BEFORE the external I/O. The canary is
    ``uncertain`` (committed) while its transport runs, so a crash in or after the hand-over, or a
    second ``canary send`` started meanwhile, can never transmit it again."""
    ws = str(world.workspace_id)
    _run(db_url, lambda db: issue_worker(db, world))
    canary_id = _prepare(run_cli, _canary_env(configured), ws)
    seen: list[tuple[str, int]] = []

    async def crashing_transport(record: Any, target: str) -> tuple[str, dict[str, Any]]:
        # What a second `canary send` (or the owner after a crash) would read at this moment.
        row = seed.conn.execute(
            "select state, version from ops.inquiry_activation_canaries where id = %s", (canary_id,)
        ).fetchone()
        assert row is not None
        seen.append((str(row[0]), int(row[1])))
        raise RuntimeError("synthetic crash after the hand-over")

    monkeypatch.setitem(canary_cli.CANARY_TRANSPORTS, "outlook_local", crashing_transport)
    open_env = _canary_env(
        configured, SELLER_EMAIL_CANARY_SEND_ENABLED="true", SELLER_INQUIRY_MODE="automatic"
    )
    flagged = ["canary", "send", canary_id, "--workspace", ws, "--yes"]
    flagged.append("--i-confirm-owner-controlled-address")
    first = run_cli(*flagged, env=open_env)
    assert first.exit_code == 0, first.output
    assert seen == [("uncertain", 2)]  # committed before the transport was called
    assert seed.conn.execute(
        "select state from ops.inquiry_activation_canaries where id = %s", (canary_id,)
    ).fetchone() == ("uncertain",)  # the crash never turns into "failed" (no proof of non-submission)
    again = run_cli(*flagged, env=open_env)
    assert again.exit_code == 3 and "CANARY_NOT_PREPARED" in again.output
    assert len(seen) == 1 and TARGET not in first.output + again.output


def test_canary_prepare_safety_refusals_are_refusals_with_the_wait(
    run_cli: Cli, configured: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    """The repository's canary bounds (C1) reach the operator as refusals (exit 3, nothing recorded),
    not as a generic problem: the kill switch, and the rolling 24-hour cap with its wait."""
    ws = str(world.workspace_id)
    _run(db_url, lambda db: issue_worker(db, world))
    env = _canary_env(configured)
    prepare = ["canary", "prepare", "--workspace", ws, "--purpose", "Activation route check", "--yes"]

    def stored() -> int:
        row = seed.conn.execute(
            "select count(*) from ops.inquiry_activation_canaries where workspace_id = %s",
            (world.workspace_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    _set_kill_switch(db_url, world, on=True)
    paused = run_cli(*prepare, env=env)
    assert paused.exit_code == 3, paused.output
    assert "kill switch" in paused.output and stored() == 0
    _set_kill_switch(db_url, world, on=False)
    for _ in range(canaries_repo.MAX_CANARIES_PER_24H):
        _prepare(run_cli, env, ws)
    capped = run_cli(*prepare, env=env)
    assert capped.exit_code == 3, capped.output
    assert "retry after" in capped.output and "nothing recorded" in capped.output
    assert stored() == canaries_repo.MAX_CANARIES_PER_24H
    assert TARGET not in paused.output + capped.output


# ---------------------------------------------------------------------------------- C2 review r2 (security)


def _in_thread[T](db_url: str, work: Callable[[Database], Awaitable[T]]) -> T:
    """`_run` from inside the CLI's running event loop: a private loop in another thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run, db_url, work).result()


def _revoked_authorization(version: int) -> SellerInquiryAuthorization:
    data = AUTHORIZATION.model_dump(mode="json")
    data["version"] = version
    data["revocation"] = {
        "revoked": True,
        "revoked_at": (now_utc() - timedelta(seconds=1)).isoformat(),
        "revoked_by": "Synthetic owner",
        "reason": "owner revoked the standing authorization (synthetic)",
    }
    return SellerInquiryAuthorization.model_validate(data)


async def _race_revoke_sender(db: Database, world: World, mailbox_id: UUID) -> None:
    admin = owner_actor(world.workspace_id)
    async with unit_of_work(db, admin) as conn:
        await sender_bindings_repo.revoke_binding(
            conn, admin, world.sender_binding_id, reason="Owner revokes the sender (synthetic)"
        )


async def _race_revoke_authorization(db: Database, world: World, mailbox_id: UUID) -> None:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        current = await inquiries_repo.current_authorization(conn, actor)
        assert current is not None
        revoked = _revoked_authorization(current.version + 1)
        await inquiries_repo.record_authorization(conn, actor, revoked, reason="revocation (synthetic)")


async def _race_revoke_mailbox(db: Database, world: World, mailbox_id: UUID) -> None:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, actor, mailbox_id, reason="Owner revokes the desktop worker (synthetic)"
        )


_CANARY_RACES: dict[str, tuple[Callable[[Database, World, UUID], Awaitable[None]], str]] = {
    "sender_binding_revoked": (_race_revoke_sender, "SENDER_BINDING_"),
    "authorization_revoked": (_race_revoke_authorization, "STANDING_AUTHORIZATION_REVOKED"),
    "mailbox_revoked": (_race_revoke_mailbox, "CANARY_MAILBOX_NOT_ACTIVE"),
}


@pytest.mark.parametrize("race", sorted(_CANARY_RACES))
def test_canary_send_re_checks_every_gate_under_the_controls_lock(
    run_cli: Cli,
    configured: dict[str, str],
    db_url: str,
    world: World,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    """TOCTOU: a revocation committed AFTER ``canary send`` read its gates (the target address is
    read in between, from a hidden prompt) but before the canary is claimed must stop the send.
    Before this review only the kill switch and the mode were re-read under the controls lock: a
    revoked sender binding, a revoked standing authorization or a revoked desktop mailbox was
    still handed to the transport."""
    ws = str(world.workspace_id)
    worker = _run(db_url, lambda db: issue_worker(db, world))
    canary_id = _prepare(run_cli, _canary_env(configured), ws)
    calls: list[str] = []

    async def fake_transport(record: Any, target: str) -> tuple[str, dict[str, Any]]:
        calls.append(str(record.id))
        return "accepted", {"synthetic_transport": True}

    revoke, code = _CANARY_RACES[race]

    def racing_target(environ: Any = None) -> str:
        # Between the gate reads and the claim: the owner (or another operator) revokes something.
        _in_thread(db_url, lambda db: revoke(db, world, worker.issued.mailbox_binding_id))
        return TARGET

    monkeypatch.setitem(canary_cli.CANARY_TRANSPORTS, "outlook_local", fake_transport)
    monkeypatch.setattr(canary_cli, "read_target_address", racing_target)
    open_env = _canary_env(
        configured, SELLER_EMAIL_CANARY_SEND_ENABLED="true", SELLER_INQUIRY_MODE="automatic"
    )
    flagged = ["canary", "send", canary_id, "--workspace", ws, "--yes"]
    flagged.append("--i-confirm-owner-controlled-address")
    result = run_cli(*flagged, env=open_env)
    assert result.exit_code == 3, result.output
    assert code in result.output and calls == []
    assert seed.conn.execute(
        "select state, version from ops.inquiry_activation_canaries where id = %s", (canary_id,)
    ).fetchone() == ("prepared", 1)  # nothing claimed, nothing sent
    assert TARGET not in result.output
