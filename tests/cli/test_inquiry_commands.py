"""Spec v1.1 operator commands (marker ``db``): ``mail-worker credential issue|revoke|list``,
``sender-binding create|status``, ``inquiries status|pause|resume``, ``evaluation report``,
``reconcile --workspace`` and the v1.1 readiness section of ``doctor``.

Everything is SYNTHETIC (``*.example`` addresses, fixture sources); nothing reaches a network and
nothing is ever sent. The token printed by ``issue`` exists only in the test's memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar
from uuid import UUID

import psycopg
import pytest
from tests.api.v11_support import (
    account_report_body,
    heartbeat_body,
    issue_worker,
    outlook_world,
    owner_actor,
)
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import SENDER_ACCOUNT, SENDER_ADDRESS, World

from suv_deals.api.schemas import MailWorkerAccountReport, MailWorkerHeartbeatRequest
from suv_deals.domain.enums import SuppressionReason
from suv_deals.persistence import inquiries_repo, mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

T = TypeVar("T")
TOKEN_RE = re.compile(r"suvmail_[0-9a-f]{64}")


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


@pytest.fixture
def world(db_url: str, seed: Seed) -> Iterator[World]:
    built = _run(db_url, lambda db: outlook_world(db, seed, "CLI inquiries"))
    try:
        yield built
    finally:
        seed.conn.execute("update app.workspaces set active = false where id = %s", (built.workspace_id,))


def _json(output: str) -> Any:
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


# ---------------------------------------------------------------------------------- sender-binding


def test_sender_binding_create_and_status_never_print_an_address(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    args = [
        "sender-binding",
        "create",
        "--workspace",
        str(workspace),
        "--account-id",
        "synthetic-outlook-account-2",
        "--from-address",
        "owner-inquiries@synthetic-mail.example",
        "--display-name",
        "Synthetic Sender",
        "--reason",
        "owner-authorized sending identity (synthetic)",
    ]
    refused = run_cli(*args, env=db_env)
    assert refused.exit_code == 3  # --yes is required
    created = run_cli(*args, "--yes", env=db_env)
    assert created.exit_code == 0, created.output
    assert "unverified" in created.output
    assert "owner-inquiries@" not in created.output and "@synthetic-mail.example" in created.output
    wrong = run_cli(*args, "--vault-ref", "vault:kv/suv/gmail", "--yes", env=db_env)
    assert wrong.exit_code == 2  # outlook_local holds no provider secret
    status = run_cli("sender-binding", "status", "--workspace", str(workspace), "--json", env=db_env)
    assert status.exit_code == 0, status.output
    rows = _json(status.output)
    assert len(rows) == 1 and rows[0]["provider"] == "outlook_local"
    assert rows[0]["verified"] is False and rows[0]["usable"] is False
    assert rows[0]["from_domain"] == "@synthetic-mail.example"
    assert "owner-inquiries" not in status.output


def test_gmail_sender_binding_takes_a_secret_only_by_reference(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    created = run_cli(
        "sender-binding",
        "create",
        "--workspace",
        str(workspace),
        "--provider",
        "gmail_api",
        "--account-id",
        "synthetic-gmail-account",
        "--from-address",
        "gmail-inquiries@synthetic-mail.example",
        "--display-name",
        "Synthetic Sender",
        "--vault-ref",
        "vault:kv/suv-deals/gmail-sender",
        "--reason",
        "optional gmail_api route (synthetic)",
        "--yes",
        env=db_env,
    )
    assert created.exit_code == 0, created.output
    assert "vault:kv" not in created.output
    rows = _json(
        run_cli("sender-binding", "status", "--workspace", str(workspace), "--json", env=db_env).output
    )
    assert rows[0]["provider"] == "gmail_api" and rows[0]["has_secret"] is True


def test_sender_binding_verify_uses_the_desktop_workers_evidence(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID
) -> None:
    ws = str(workspace)
    address = "verify-inquiries@synthetic-mail.example"
    key = "synthetic-outlook-key-0001"
    created = run_cli(
        "sender-binding", "create", "--workspace", ws, "--account-id", key, "--from-address", address,
        "--display-name", "Synthetic Sender", "--reason", "owner-authorized sending identity (synthetic)",
        "--yes", env=db_env,
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    binding_id = _json(run_cli("sender-binding", "status", "--workspace", ws, "--json", env=db_env).output)[
        0
    ]["id"]
    verify = [
        "sender-binding",
        "verify",
        binding_id,
        "--workspace",
        ws,
        "--reason",
        "technical check (synthetic)",
    ]
    assert run_cli(*verify, env=db_env).exit_code == 3  # --yes required
    no_worker = run_cli(*verify, "--yes", env=db_env)
    assert no_worker.exit_code == 3 and "NO_ACTIVE_MAIL_WORKER" in no_worker.output
    issued = run_cli(
        "mail-worker", "credential", "issue", "--workspace", ws, "--sender-binding", binding_id,
        "--label", "Synthetic laptop", "--yes", env=db_env,
    )  # fmt: skip
    token = TOKEN_RE.findall(issued.output)[0]
    silent = run_cli(*verify, "--yes", env=db_env)
    assert silent.exit_code == 3
    assert "WORKER_ACCOUNT_UNKNOWN" in silent.output and "WORKER_HEARTBEAT_" in silent.output

    def worker_reports(stable_key: str) -> Callable[[Database], Awaitable[None]]:
        async def report(db: Database) -> None:
            async with db.transaction() as conn:
                worker = await mail_workers_repo.resolve_worker(conn, token)
            box = worker.mailbox_binding_id
            async with unit_of_work(db, worker.actor("req-cli-verify")) as conn:
                await mail_workers_repo.record_heartbeat(
                    conn,
                    worker,
                    MailWorkerHeartbeatRequest.model_validate(heartbeat_body(box)),
                    request_id="r1",
                )
                await mail_workers_repo.record_account_report(
                    conn,
                    worker,
                    MailWorkerAccountReport.model_validate(
                        account_report_body(box, address, stable_account_key=stable_key)
                    ),
                    request_id="r2",
                )

        return report

    _run(db_url, worker_reports("synthetic-other-account-9"))
    mismatch = run_cli(*verify, "--yes", env=db_env)
    assert mismatch.exit_code == 3 and "ACCOUNT_KEY_MISMATCH" in mismatch.output
    _run(db_url, worker_reports(key))
    verified = run_cli(*verify, "--yes", env=db_env)
    assert verified.exit_code == 0, verified.output
    assert "Verified sender binding" in verified.output and address not in verified.output
    rows = _json(run_cli("sender-binding", "status", "--workspace", ws, "--json", env=db_env).output)
    assert rows[0]["verified"] is True and rows[0]["usable"] is True and rows[0]["health"] == "healthy"


# ---------------------------------------------------------------------------------- mail-worker credential


def test_mail_worker_credential_issue_list_and_revoke(
    run_cli: Cli, db_env: dict[str, str], world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    args = [
        "mail-worker",
        "credential",
        "issue",
        "--workspace",
        ws,
        "--sender-binding",
        str(world.sender_binding_id),
    ]
    args += ["--label", "Owner laptop classic Outlook (synthetic)"]
    refused = run_cli(*args, env=db_env)
    assert refused.exit_code == 3 and not TOKEN_RE.search(refused.output)
    issued = run_cli(*args, "--expires", "30d", "--yes", env=db_env)
    assert issued.exit_code == 0, issued.output
    tokens = TOKEN_RE.findall(issued.output)
    assert len(tokens) == 1  # printed exactly once
    token = tokens[0]
    assert SENDER_ADDRESS not in issued.output
    box = seed.conn.execute(
        "select m.id, m.state, c.token_hash, c.credential_kind, c.scopes from ops.mail_worker_bindings m"
        " join ops.api_credentials c on c.id = m.credential_id where m.workspace_id = %s",
        (world.workspace_id,),
    ).fetchone()
    assert box is not None
    mailbox_id, state, token_hash, kind, scopes = box
    assert state == "active" and kind == "mail_worker" and list(scopes) == ["mail:ingest"]
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()  # only the hash is stored
    assert f"mailbox_binding_id = {mailbox_id}" in issued.output
    listed = run_cli("mail-worker", "credential", "list", "--workspace", ws, "--json", env=db_env)
    assert listed.exit_code == 0, listed.output
    assert [r["id"] for r in _json(listed.output)] == [str(mailbox_id)]
    assert token not in listed.output and SENDER_ADDRESS not in listed.output
    revoke = ["mail-worker", "credential", "revoke", str(mailbox_id), "--workspace", ws]
    revoke += ["--reason", "laptop replaced (synthetic)"]
    assert run_cli(*revoke, env=db_env).exit_code == 3
    done = run_cli(*revoke, "--yes", env=db_env)
    assert done.exit_code == 0 and "Revoked." in done.output
    again = run_cli(*revoke, "--yes", env=db_env)
    assert "Already revoked" in again.output
    revoked = seed.conn.execute(
        "select c.revoked_at is not null from ops.api_credentials c where c.token_hash = %s", (token_hash,)
    ).fetchone()
    assert revoked == (True,)
    assert (
        run_cli("mail-worker", "credential", "list", "--workspace", ws, "--json", env=db_env)
        .output.strip()
        .endswith("[]")
    )


# ---------------------------------------------------------------------------------- inquiries


def test_inquiries_status_pause_and_resume(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World, seed: Seed
) -> None:
    ws = str(world.workspace_id)
    # The process settings are part of the gate: with the default SELLER_INQUIRY_MODE nothing is
    # sent even though every database control is open.
    closed = _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)
    assert closed["mode"] == "automatic" and closed["process_mode"] == "disabled_until_sender_ready"
    assert closed["sending_possible"] is False
    killed = {**db_env, "SELLER_INQUIRY_MODE": "automatic", "SELLER_INQUIRY_KILL_SWITCH": "true"}
    killed_data = _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=killed).output)
    assert killed_data["process_kill_switch"] is True and killed_data["sending_possible"] is False
    # The runtime sends only from the CONFIGURED identity (SELLER_EMAIL_ACCOUNT_ID / _FROM /
    # _REPLY_TO): without it the status must not claim that sending is possible.
    unconfigured = {**db_env, "SELLER_INQUIRY_MODE": "automatic"}
    identity = _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=unconfigured).output)
    assert identity["sender_binding"] == "usable" and identity["sending_possible"] is False
    assert {"ACCOUNT_NOT_CONFIGURED", "FROM_NOT_CONFIGURED"} <= set(identity["sender_identity_problems"])
    db_env = {
        **unconfigured,
        "SELLER_EMAIL_ACCOUNT_ID": SENDER_ACCOUNT,
        "SELLER_EMAIL_FROM": SENDER_ADDRESS,
    }
    status = run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env)
    assert status.exit_code == 0, status.output
    data = _json(status.output)
    assert data["mode"] == "automatic" and data["kill_switch"] is False
    assert data["process_mode"] == "automatic" and data["process_kill_switch"] is False
    assert data["authorization"] == "active" and data["sender_binding"] == "usable"
    assert data["sender_identity_problems"] == []
    assert data["sending_possible"] is True
    version = data["version"]
    assert SENDER_ADDRESS not in status.output
    pause = ["inquiries", "pause", "--workspace", ws, "--reason", "Owner pause (synthetic)"]
    assert run_cli(*pause, "--expected-version", str(version), env=db_env).exit_code == 3
    paused = run_cli(*pause, "--expected-version", str(version), "--yes", env=db_env)
    assert paused.exit_code == 0, paused.output
    assert f"Paused (version {version + 1})" in paused.output
    stale = run_cli(*pause, "--expected-version", str(version), "--yes", env=db_env)
    assert stale.exit_code != 0
    assert (
        _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)[
            "sending_possible"
        ]
        is False
    )

    async def suppress(db: Database) -> None:
        admin = owner_actor(world.workspace_id)
        async with unit_of_work(db, admin) as conn:
            await inquiries_repo.add_suppression(
                conn,
                admin,
                scope="workspace",
                key=str(world.workspace_id),
                reason=SuppressionReason.KILL_SWITCH,
                evidence={"synthetic": True},
            )

    _run(db_url, suppress)
    resume = ["inquiries", "resume", "--workspace", ws, "--reason", "Owner resume (synthetic)"]
    resume += ["--expected-version", str(version + 1)]
    # C2: removal names the count the owner saw (`--expected-suppressions`, from `inquiries status`).
    removal = ["--remove-suppressions", "--expected-suppressions", "1"]
    no_owner = run_cli(*resume, *removal, "--yes", env=db_env)
    assert no_owner.exit_code == 2  # suppressions are never removed by a system principal
    stranger = seed.user()
    not_owner = run_cli(*resume, *removal, "--owner-user-id", str(stranger), "--yes", env=db_env)
    assert not_owner.exit_code == 1
    owner = seed.user()
    seed.membership(world.workspace_id, owner, "owner")
    resumed = run_cli(*resume, *removal, "--owner-user-id", str(owner), "--yes", env=db_env)
    assert resumed.exit_code == 0, resumed.output
    assert f"Resumed (version {version + 2}" in resumed.output
    assert "Removed 1 suppression(s)" in resumed.output
    assert (
        _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)["kill_switch"]
        is False
    )


def test_inquiries_status_without_controls_is_honest(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    status = run_cli("inquiries", "status", "--workspace", str(workspace), "--json", env=db_env)
    assert status.exit_code == 0, status.output
    data = _json(status.output)
    assert data["mode"] == "no_controls_yet" and data["sending_possible"] is False
    assert data["authorization"] == "missing" and data["sender_binding"] == "missing"


# ---------------------------------------------------------------------------------- evaluation, reconcile


def test_evaluation_report_is_fixed_to_15_days(run_cli: Cli, db_env: dict[str, str], workspace: UUID) -> None:
    report = run_cli(
        "evaluation", "report", "--workspace", str(workspace), "--days", "15", "--json", env=db_env
    )
    assert report.exit_code == 0, report.output
    data = _json(report.output)
    assert data["days"] == 15
    assert data["report"]["qualifying_deal_ids"] == []  # zero is reported as zero
    assert "THRESHOLD_PROPOSED" in data["warnings"]
    human = run_cli("evaluation", "report", "--workspace", str(workspace), env=db_env)
    assert human.exit_code == 0 and "15-day evaluation" in human.output
    assert (
        run_cli("evaluation", "report", "--workspace", str(workspace), "--days", "30", env=db_env).exit_code
        == 2
    )


def test_reconcile_for_one_workspace(run_cli: Cli, db_env: dict[str, str], workspace: UUID) -> None:
    result = run_cli("reconcile", "--workspace", str(workspace), "--dry-run", "--json", env=db_env)
    assert result.exit_code == 0, result.output
    reports = _json(result.output)
    assert isinstance(reports, list) and len(reports) == 1
    assert run_cli("reconcile", "--workspace", str(workspace), "--loop", env=db_env).exit_code == 2


# ---------------------------------------------------------------------------------- doctor


def test_doctor_reports_v11_readiness_without_values(
    run_cli: Cli, db_env: dict[str, str], db_url: str, world: World, db_conn: psycopg.Connection
) -> None:
    worker = _run(db_url, lambda db: issue_worker(db, world))
    result = run_cli("doctor", "--process", "scheduler", env=db_env)
    out = result.output
    prefix = str(world.workspace_id)[:8]
    single = sum(1 for line in out.splitlines() if "seller_inquiry/" in line and "authorization" in line) == 1
    tag = "seller_inquiry/" if single else f"seller_inquiry/{prefix}:"
    lines = [line for line in out.splitlines() if tag in line]
    names = " ".join(lines)
    for part in ("controls", "authorization", "sender_binding", "mail_worker"):
        assert f"{tag}{part}" in names, out
    assert any("mode=automatic" in line and "kill_switch=off" in line for line in lines)
    # The default process settings (SELLER_INQUIRY_MODE=disabled_until_sender_ready) close the gate.
    assert any("controls" in line and "process settings" in line for line in lines), out
    assert any("mail_worker" in line and "heartbeat" in line and "NOT active" in line for line in lines)
    opened = run_cli("doctor", "--process", "scheduler", env={**db_env, "SELLER_INQUIRY_MODE": "automatic"})
    assert not any("process settings" in line for line in opened.output.splitlines() if tag in line)
    # The runtime sends only from the configured identity: doctor says so when it is not set.
    assert f"{tag}sender_identity" in opened.output and "ACCOUNT_NOT_CONFIGURED" in opened.output
    configured = {
        **db_env,
        "SELLER_INQUIRY_MODE": "automatic",
        "SELLER_EMAIL_ACCOUNT_ID": SENDER_ACCOUNT,
        "SELLER_EMAIL_FROM": SENDER_ADDRESS,
    }
    matched = run_cli("doctor", "--process", "scheduler", env=configured)
    assert f"{tag}sender_identity" not in matched.output and f"{tag}sender_binding" in matched.output
    assert worker.token not in out and SENDER_ADDRESS not in out
    assert SENDER_ADDRESS not in opened.output and SENDER_ADDRESS not in matched.output
    del db_conn


def test_inquiries_authorize_set_mode_and_set_limits(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    ws = str(workspace)
    authorize = ["inquiries", "authorize", "--workspace", ws, "--reason", "owner standing authorization v1"]
    assert run_cli(*authorize, env=db_env).exit_code == 3  # --yes required
    done = run_cli(*authorize, "--yes", env=db_env)
    assert done.exit_code == 0, done.output
    assert "Standing authorization version 1" in done.output and "disabled_until_sender_ready" in done.output
    again = run_cli(*authorize, "--yes", env=db_env)
    assert again.exit_code == 0 and "version 1" in again.output  # identical latest version: no-op
    status = _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)
    assert status["mode"] == "disabled_until_sender_ready" and status["authorization"] == "active"
    assert status["sending_possible"] is False and status["max_per_24h"] == 2 and status["max_per_15d"] == 5
    version = status["version"]
    mode = ["inquiries", "set-mode", "--workspace", ws, "--reason", "owner decision (synthetic)"]
    refused = run_cli(
        *mode[:2], "automatic", *mode[2:], "--expected-version", str(version), "--yes", env=db_env
    )
    assert refused.exit_code == 3 and "sender_binding" in refused.output  # no usable sender yet
    limits = ["inquiries", "set-limits", "--workspace", ws, "--reason", "owner lowers the caps (synthetic)"]
    too_high = run_cli(
        *limits,
        "--max-per-24h",
        "3",
        "--max-per-15d",
        "5",
        "--expected-version",
        str(version),
        "--yes",
        env=db_env,
    )
    assert too_high.exit_code == 2
    lowered = run_cli(
        *limits,
        "--max-per-24h",
        "1",
        "--max-per-15d",
        "3",
        "--expected-version",
        str(version),
        "--yes",
        env=db_env,
    )
    assert lowered.exit_code == 0, lowered.output
    assert "Ceilings 1/24h and 3/15d" in lowered.output
    paused = run_cli(
        *mode[:2], "paused", *mode[2:], "--expected-version", str(version + 1), "--yes", env=db_env
    )
    assert paused.exit_code == 0, paused.output
    status = _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)
    assert status["mode"] == "paused" and status["max_per_24h"] == 1 and status["version"] == version + 2


def test_set_limits_never_loosens_the_seller_cooldown(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID
) -> None:
    """The seller cooldown (no burst of e-mails to one dealer, spec 37.5) is owner-REDUCIBLE in
    sending only: a longer cooldown is kept when ``--cooldown-days`` is omitted (never silently
    reset to the default) and nothing shorter than the v1.1 default (7 days) is accepted."""
    ws = str(workspace)
    authorize = ["inquiries", "authorize", "--workspace", ws, "--reason", "owner standing authorization v1"]
    assert run_cli(*authorize, "--yes", env=db_env).exit_code == 0

    def status() -> Any:
        return _json(run_cli("inquiries", "status", "--workspace", ws, "--json", env=db_env).output)

    initial = status()
    assert initial["seller_cooldown_days"] == 7
    limits = ["inquiries", "set-limits", "--workspace", ws, "--reason", "owner tightens limits (synthetic)"]
    longer = run_cli(
        *limits,
        "--max-per-24h",
        "2",
        "--max-per-15d",
        "5",
        "--cooldown-days",
        "30",
        "--expected-version",
        str(initial["version"]),
        "--yes",
        env=db_env,
    )
    assert longer.exit_code == 0, longer.output
    assert status()["seller_cooldown_days"] == 30
    caps_only = run_cli(
        *limits,
        "--max-per-24h",
        "1",
        "--max-per-15d",
        "3",
        "--expected-version",
        str(initial["version"] + 1),
        "--yes",
        env=db_env,
    )
    assert caps_only.exit_code == 0, caps_only.output
    after = status()
    assert after["max_per_24h"] == 1 and after["seller_cooldown_days"] == 30  # kept, not reset to 7
    shorter = run_cli(
        *limits,
        "--max-per-24h",
        "1",
        "--max-per-15d",
        "3",
        "--cooldown-days",
        "1",
        "--expected-version",
        str(after["version"]),
        "--yes",
        env=db_env,
    )
    assert shorter.exit_code == 2  # usage: below the v1.1 seller cooldown
    assert status()["seller_cooldown_days"] == 30
