"""Work package C2: interface follow-ups of the C1 hardening (dashboard API and MCP error bodies).

- Resume TOCTOU: ``expected_removable_suppressions`` refuses a resume whose removable suppressions
  changed after the owner read the control view (``409``, ``suppressions_changed``; nothing
  changes), so only the suppressions the owner saw are removed.
- ``GET /api/candidates?include_screening_rejected=true``: the audit filter, bound into the
  cursor's filter hash; never part of the spec 21 ``deals_list_candidates`` input.
- ``details.reason`` of a ``TransientConflict`` (``busy`` / ``in_progress``) reaches the API and
  MCP error bodies; idempotent requests still running answer ``in_progress``.

Real ASGI app, real PostgreSQL, locally signed Supabase-shaped JWTs, SYNTHETIC data only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from tests.api.conftest import (
    ApiHarness,
    DataHarness,
    SigningKeys,
    TokenFactory,
    Users,
    add_members,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.api.v11_support import (
    MAIL,
    MailWorker,
    accepted_send,
    claim_body,
    controls_version,
    dispatch_intent,
    heartbeat_body,
    issue_worker,
    latest_binding_version,
    outlook_world,
    owner_actor,
    reply_body,
    report_body,
    upload_reply,
)
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import SENDER_ACCOUNT, SENDER_ADDRESS, World

from suv_deals.api.deps import MAIL_WORKER_MUTATION_LIMIT, MAIL_WORKER_REPLY_LIMIT
from suv_deals.api.inquiry_routes import RESUME_OPERATION
from suv_deals.api.mail_worker_routes import REPORT_OPERATION
from suv_deals.api.middleware import PrincipalRateLimiter, RateLimit
from suv_deals.api.schemas import (
    CandidateListQuery,
    InquiryPauseRequest,
    InquiryResumeRequest,
    MailWorkerSendReport,
)
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import EmailProviderKind, Role, SuppressionReason
from suv_deals.mcp.schemas import DealsListCandidatesInput, tool_input_schema
from suv_deals.mcp.tools import PAUSE_OPERATION
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import (
    idempotency,
    inquiries_repo,
    mail_workers_repo,
    replies_repo,
    sender_bindings_repo,
)
from suv_deals.persistence.database import Database
from suv_deals.persistence.queries.inquiries import credential_status
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


@dataclass
class ControlHarness(ApiHarness):
    db: Database = None  # type: ignore[assignment]
    seed: Seed = None  # type: ignore[assignment]
    world: World = None  # type: ignore[assignment]
    worker: MailWorker = None  # type: ignore[assignment]


@pytest.fixture
async def control_api(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[ControlHarness]:
    world = await outlook_world(db, seed, "C2 control")
    users: Users = add_members(seed, world.workspace_id)
    worker = await issue_worker(db, world)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(make_settings(seller_inquiry_mode="automatic"), keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield ControlHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=world.workspace_id,
            metrics=metrics,
            db=db,
            seed=seed,
            world=world,
            worker=worker,
        )


def _data(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _suppress(api: ControlHarness, reason: SuppressionReason, key: str | None = None) -> None:
    admin = owner_actor(api.world.workspace_id)
    async with unit_of_work(api.db, admin) as conn:
        await inquiries_repo.add_suppression(
            conn,
            admin,
            scope="workspace" if key is None else "seller",
            key=key or str(api.world.workspace_id),
            reason=reason,
            evidence={"synthetic": True},
        )


def _active_suppressions(api: ControlHarness) -> list[str]:
    rows = api.seed.conn.execute(
        "select reason from ops.email_suppressions where workspace_id = %s and removed_at is null"
        " order by reason",
        (api.world.workspace_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------------- resume TOCTOU


async def test_resume_refuses_when_the_removable_suppressions_changed_after_the_owner_looked(
    control_api: ControlHarness,
) -> None:
    """The owner saw ONE removable suppression; a second one was added before the resume arrived.
    The resume is refused as a whole (kill switch still on, nothing removed), and a resume naming
    the current count succeeds."""
    api = control_api
    await _suppress(api, SuppressionReason.KILL_SWITCH)
    pause = await api.post(
        "/api/inquiry-control/pause",
        api.users.owner,
        {
            "expected_version": await controls_version(api.db, api.world.workspace_id),
            "reason": "Owner pause before the check",
            "idempotency_key": "c2-pause-0001",
        },
    )
    assert pause.status_code == 200, pause.text
    seen = _data(await api.get("/api/inquiry-control", api.users.owner))
    assert seen["kill_switch"] is True and seen["removable_suppressions"] == 1
    # After the owner looked: one more removable suppression (here: authorization_revoked while the
    # authorization is effective again).
    await _suppress(api, SuppressionReason.AUTHORIZATION_REVOKED)
    body = {
        "expected_version": seen["version"],
        "reason": "Owner resumes after the check",
        "idempotency_key": "c2-resume-0001",
        "remove_suppressions": True,
        "expected_removable_suppressions": seen["removable_suppressions"],
    }
    refused = await api.post("/api/inquiry-control/resume", api.users.owner, body)
    assert refused.status_code == 409, refused.text
    error = error_of(refused)
    assert error["code"] == "VERSION_CONFLICT" and error["retryable"] is False
    assert error["details"] == {
        "reason": "suppressions_changed",
        "expected_removable_suppressions": 1,
        "current_removable_suppressions": 2,
    }
    after = _data(await api.get("/api/inquiry-control", api.users.owner))
    assert after["kill_switch"] is True and after["version"] == seen["version"]  # nothing changed
    assert _active_suppressions(api) == ["authorization_revoked", "kill_switch"]
    # The refused key was not consumed (the transaction rolled back): the owner retries with it
    # after reloading, naming the count they now see.
    retried = _data(
        await api.post(
            "/api/inquiry-control/resume",
            api.users.owner,
            {**body, "expected_removable_suppressions": after["removable_suppressions"]},
        )
    )
    assert retried["kill_switch"] is False and retried["suppressions_removed"] == 2
    assert _active_suppressions(api) == []


async def test_resume_with_the_seen_count_removes_exactly_those_and_never_other_reasons(
    control_api: ControlHarness,
) -> None:
    api = control_api
    await _suppress(api, SuppressionReason.KILL_SWITCH)
    await _suppress(api, SuppressionReason.SELLER_OPT_OUT, key=f"seller_entity:{api.world.seller_entity_id}")
    seen = _data(await api.get("/api/inquiry-control", api.users.owner))
    assert seen["removable_suppressions"] == 1  # the opt-out is never removable by a resume
    resumed = _data(
        await api.post(
            "/api/inquiry-control/resume",
            api.users.owner,
            {
                "expected_version": seen["version"],
                "reason": "Owner clears the kill-switch suppression",
                "idempotency_key": "c2-resume-0002",
                "remove_suppressions": True,
                "expected_removable_suppressions": 1,
            },
        )
    )
    assert resumed["suppressions_removed"] == 1
    assert _active_suppressions(api) == ["seller_opt_out"]


async def test_resume_count_is_validated_and_ignored_without_removal(control_api: ControlHarness) -> None:
    api = control_api
    version = await controls_version(api.db, api.world.workspace_id)
    base = {"expected_version": version, "reason": "Owner resume", "idempotency_key": "c2-resume-0003"}
    for bad in (-1, "1", 1.5, 5001):
        invalid = await api.post(
            "/api/inquiry-control/resume", api.users.owner, {**base, "expected_removable_suppressions": bad}
        )
        assert invalid.status_code == 422, bad
        assert error_of(invalid)["details"]["fields"] == ["expected_removable_suppressions"]
    await _suppress(api, SuppressionReason.KILL_SWITCH)
    # Without remove_suppressions the count names nothing to remove: it is not checked.
    resumed = _data(
        await api.post(
            "/api/inquiry-control/resume", api.users.owner, {**base, "expected_removable_suppressions": 7}
        )
    )
    assert resumed["suppressions_removed"] == 0
    assert _active_suppressions(api) == ["kill_switch"]
    schema = InquiryResumeRequest.model_json_schema()
    assert "expected_removable_suppressions" in schema["properties"]


async def test_a_resume_still_in_progress_answers_in_progress(control_api: ControlHarness) -> None:
    """An idempotency record committed ``in_progress`` (the same key still running elsewhere) is a
    retryable ``409 VERSION_CONFLICT`` with ``details.reason = in_progress``, never ``busy``."""
    api = control_api
    body = InquiryResumeRequest(
        expected_version=await controls_version(api.db, api.world.workspace_id),
        reason="Owner resume in flight",
        idempotency_key="c2-resume-inflight-01",
    )
    owner = await _member_actor(api, api.users.owner)
    async with unit_of_work(api.db, owner) as conn:
        started = await idempotency.begin(
            conn,
            owner,
            RESUME_OPERATION,
            body.idempotency_key,
            idempotency.request_hash_for(RESUME_OPERATION, body),
        )
    assert isinstance(started, idempotency.NewRequest)
    response = await api.post("/api/inquiry-control/resume", api.users.owner, body.model_dump())
    assert response.status_code == 409, response.text
    error = error_of(response)
    assert error["code"] == "VERSION_CONFLICT" and error["retryable"] is True
    assert error["details"] == {"reason": "in_progress"}
    assert response.headers.get("retry-after") == "1"


async def test_a_pause_still_in_progress_answers_in_progress(control_api: ControlHarness) -> None:
    api = control_api
    body = {
        "expected_version": await controls_version(api.db, api.world.workspace_id),
        "reason": "Owner pause in flight",
        "idempotency_key": "c2-pause-inflight-01",
    }
    owner = await _member_actor(api, api.users.owner)
    tool = InquiryPauseRequest.model_validate(body).to_tool_input()
    async with unit_of_work(api.db, owner) as conn:
        await idempotency.begin(
            conn,
            owner,
            PAUSE_OPERATION,
            tool.idempotency_key,
            idempotency.request_hash_for(PAUSE_OPERATION, tool),
        )
    response = await api.post("/api/inquiry-control/pause", api.users.owner, body)
    assert response.status_code == 409, response.text
    assert error_of(response)["details"] == {"reason": "in_progress"}


async def _member_actor(api: ControlHarness, user: UUID) -> ActorContext:
    """The same principal the API resolves for ``user`` (owner role, its scopes)."""
    return ActorContext(
        workspace_id=api.world.workspace_id,
        principal_id=user,
        principal_kind="user",
        role=Role.OWNER,
        scopes=ROLE_SCOPES[Role.OWNER],
        request_id="req-c2-owner",
        display_name="Synthetic owner",
    )


# ---------------------------------------------------------------------------------- candidates audit filter


async def test_candidates_audit_filter_lists_screening_rejected_observations(data_api: DataHarness) -> None:
    d = data_api.data
    viewer = data_api.users.viewer
    default = _data(await data_api.get("/api/candidates", viewer))
    listed = {item["listing_id"] for item in default["items"]}
    assert str(d.listings["rejected"]) not in listed  # rejected observations are not candidates
    audit = _data(await data_api.get("/api/candidates?include_screening_rejected=true", viewer))
    audit_ids = {item["listing_id"]: item for item in audit["items"]}
    assert set(audit_ids) == listed | {str(d.listings["rejected"])}
    assert audit_ids[str(d.listings["rejected"])]["eligibility"] == "rejected"
    explicit_false = _data(await data_api.get("/api/candidates?include_screening_rejected=false", viewer))
    assert {i["listing_id"] for i in explicit_false["items"]} == listed
    bad = await data_api.get("/api/candidates?include_screening_rejected=maybe", viewer)
    assert bad.status_code == 422 and error_of(bad)["details"]["fields"] == ["include_screening_rejected"]


async def test_candidates_audit_filter_is_bound_into_the_cursor(data_api: DataHarness) -> None:
    viewer = data_api.users.viewer
    first = (await data_api.get("/api/candidates?include_screening_rejected=true&limit=1", viewer)).json()
    cursor = first["next_cursor"]
    assert cursor is not None
    mismatch = await data_api.get(f"/api/candidates?limit=1&cursor={cursor}", viewer)
    assert mismatch.status_code == 422 and error_of(mismatch)["details"]["cursor"] == "mismatch"
    plain = (await data_api.get("/api/candidates?limit=1", viewer)).json()
    other = await data_api.get(
        f"/api/candidates?include_screening_rejected=true&limit=1&cursor={plain['next_cursor']}", viewer
    )
    assert other.status_code == 422 and error_of(other)["details"]["cursor"] == "mismatch"
    seen: list[str] = [first["data"]["items"][0]["listing_id"]]
    while cursor is not None:
        path = f"/api/candidates?include_screening_rejected=true&limit=1&cursor={cursor}"
        page = (await data_api.get(path, viewer)).json()
        seen.extend(item["listing_id"] for item in page["data"]["items"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) and str(data_api.data.listings["rejected"]) in seen


def test_the_audit_filter_is_never_a_spec_21_tool_input() -> None:
    query = CandidateListQuery.model_validate({"include_screening_rejected": "true"})
    assert query.include_screening_rejected is True
    assert query.to_tool_input() == DealsListCandidatesInput()  # not forwarded to the tool input
    assert "include_screening_rejected" not in tool_input_schema("deals_list_candidates")["properties"]
    with pytest.raises(ValueError):
        DealsListCandidatesInput.model_validate({"include_screening_rejected": True})


# ------------------------------------------------------------------ control view: configured sender


async def test_control_view_reports_the_configured_sender_not_the_newest_binding(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    """The readiness describes the binding the runtime would send from (the configured identity).
    A newer, unverified binding of another account must not make the configured one look unready,
    and without a configured identity nothing is reported ready."""
    world = await outlook_world(db, seed, "C2 configured sender")
    users = add_members(seed, world.workspace_id)
    admin = owner_actor(world.workspace_id)
    async with unit_of_work(db, admin) as conn:
        newer = await sender_bindings_repo.create_binding(
            conn,
            admin,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id="synthetic-other-account",
            from_address="other-sender@synthetic-mail.example",
            display_name="Other Synthetic Sender",
            reply_to_address=None,
            reason="C2 test: a newer unverified binding",
        )
    assert newer.verified_at is None
    configured = make_settings(
        seller_inquiry_mode="automatic",
        seller_email_provider="outlook_local",
        seller_email_account_id=SENDER_ACCOUNT,
        seller_email_from=SENDER_ADDRESS,
    )
    async with running_client(build_test_app(configured, keys, db)) as client:
        api = ApiHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=world.workspace_id,
            metrics=AppMetrics(process_metrics=False),
        )
        view = _data(await api.get("/api/inquiry-control", users.reviewer))
    assert view["sender_readiness"] == "ready" and view["sender_problems"] == []
    assert view["sender_provider"] == "outlook_local" and view["authorization_status"] == "active"
    unconfigured = make_settings(seller_inquiry_mode="automatic")
    async with running_client(build_test_app(unconfigured, keys, db)) as client:
        api = ApiHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=world.workspace_id,
            metrics=AppMetrics(process_metrics=False),
        )
        response = await api.get("/api/inquiry-control", users.owner)
    missing = _data(response)
    assert missing["sender_readiness"] == "missing"
    assert "sender_identity_sender_binding_mismatch" in missing["sender_problems"]
    assert "sender_identity_from_not_configured" in missing["sender_problems"]
    assert SENDER_ADDRESS not in response.text and "synthetic-mail.example" not in response.text


# ---------------------------------------------------------------------------------- replies and health


@pytest.fixture
async def reply_api(control_api: ControlHarness) -> AsyncIterator[tuple[ControlHarness, str, UUID]]:
    inquiry_id, intent = await accepted_send(
        control_api.client, control_api.db, control_api.world, control_api.worker
    )
    ack = await upload_reply(control_api.client, control_api.world, control_api.worker, inquiry_id, intent)
    yield control_api, str(ack["reply_id"]), inquiry_id


async def test_reply_detail_and_health_show_the_signal_state(
    reply_api: tuple[ControlHarness, str, UUID],
) -> None:
    api, reply_id, _inquiry = reply_api
    detail = _data(await api.get(f"/api/replies/{reply_id}", api.users.reviewer))
    assert detail["signal_status"] == "emitted"
    health = _data(await api.get("/api/mail-workers/health", api.users.reviewer))
    assert health["reply_signals"] == {
        "window_hours": 24,
        "cap_per_inquiry": replies_repo.MAX_SIGNALS_PER_INQUIRY_24H,
        "emitted": 1,
        "coalesced": 0,
        "rate_limited": 0,
        "inquiries_at_cap": 0,
    }


async def test_rate_limited_signals_are_visible_in_health_and_the_reply(
    reply_api: tuple[ControlHarness, str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _first, inquiry_id = reply_api
    monkeypatch.setattr(replies_repo, "MAX_SIGNALS_PER_INQUIRY_24H", 1)
    # Deliver the first signal so the next reply is not coalesced into a pending one.
    api.seed.conn.execute(
        "update ops.outbox set state = 'delivered', send_attempted_at = now(),"
        " provider_accepted_at = now(), completed_at = now() where workspace_id = %s"
        " and event_type = 'seller.reply.received'",
        (api.world.workspace_id,),
    )
    document = await _correlated_reply(
        api, inquiry_id, "Guten Tag, eine zweite Antwort. Synthetic second reply."
    )
    second = await api.client.post(
        f"{MAIL}/replies", headers=api.worker.headers("mwr1-c2-second-01"), json=document
    )
    assert second.status_code == 200, second.text
    reply = _data(await api.get(f"/api/replies/{second.json()['reply_id']}", api.users.reviewer))
    assert reply["signal_status"] == "rate_limited"
    health_response = await api.get("/api/mail-workers/health", api.users.owner)
    health = _data(health_response)
    assert health["reply_signals"]["rate_limited"] == 1 and health["reply_signals"]["inquiries_at_cap"] == 1
    messages = [w["message"] for w in health_response.json()["warnings"]]
    assert any("signal cap" in m for m in messages)


async def test_health_shows_revoked_and_expired_worker_credentials(control_api: ControlHarness) -> None:
    api = control_api
    health = _data(await api.get("/api/mail-workers/health", api.users.reviewer))
    assert health["revoked_mailboxes"] == 0
    [credential] = health["credentials"]
    assert credential["mailbox_binding_id"] == str(api.worker.mailbox_id)
    assert credential["credential_status"] == "active" and credential["revoked_at"] is None
    # Expired credential on an ACTIVE mailbox: the worker gets 401 and is visibly unable to work.
    with api.seed.conn.transaction():  # TEST ARRANGEMENT ONLY: expires_at is frozen by a guard
        api.seed.conn.execute("set local session_replication_role = replica")
        api.seed.conn.execute(
            "update ops.api_credentials set created_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 hour' where id = %s",
            (api.worker.credential_id,),
        )
    expired_response = await api.get("/api/mail-workers/health", api.users.reviewer)
    expired = _data(expired_response)
    assert expired["credentials"][0]["credential_status"] == "expired"
    assert any(
        "credential is expired or revoked" in w["message"] for w in expired_response.json()["warnings"]
    )
    assert api.worker.token not in expired_response.text
    # A revoked worker is counted even when the default view does not list it.
    admin = owner_actor(api.world.workspace_id)
    async with unit_of_work(api.db, admin) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, admin, api.worker.mailbox_id, reason="C2 test revocation"
        )
    default = _data(await api.get("/api/mail-workers/health", api.users.reviewer))
    assert default["mailboxes"] == [] and default["credentials"] == []
    assert default["revoked_mailboxes"] == 1
    listed = _data(await api.get("/api/mail-workers/health?include_revoked=true", api.users.reviewer))
    assert [c["credential_status"] for c in listed["credentials"]] == ["revoked"]
    assert listed["credentials"][0]["binding_state"] == "revoked"


async def _correlated_reply(api: ControlHarness, inquiry_id: UUID, body: str) -> dict[str, Any]:
    """Another correlated seller reply to the inquiry's sent message (a new Message-ID)."""
    sent = api.seed.conn.execute(
        "select rfc_message_id from ops.email_delivery_attempts where workspace_id = %s and inquiry_id = %s",
        (api.world.workspace_id, inquiry_id),
    ).fetchone()
    assert sent is not None
    return reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=api.worker.mailbox_id,
        binding_version=await latest_binding_version(api.client, api.worker, inquiry_id),
        from_address=str(api.world.vehicle.address),
        in_reply_to=str(sent[0]),
        body=body,
    )


def test_credential_status_thresholds() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert credential_status(now + timedelta(days=30), None, now) == "active"
    assert credential_status(now + timedelta(days=13), None, now) == "expiring"
    assert credential_status(now, None, now) == "expired"
    assert credential_status(now + timedelta(days=30), now, now) == "revoked"


# ---------------------------------------------------------------------------------- reply upload limits


async def test_reply_uploads_have_their_own_tighter_bucket(
    reply_api: tuple[ControlHarness, str, UUID],
) -> None:
    api, _reply, inquiry_id = reply_api
    assert MAIL_WORKER_REPLY_LIMIT.capacity < MAIL_WORKER_MUTATION_LIMIT.capacity
    assert MAIL_WORKER_REPLY_LIMIT.per_seconds > MAIL_WORKER_MUTATION_LIMIT.per_seconds
    state = api.client._transport.app.state.suv_api  # type: ignore[attr-defined]
    state.mail_worker_reply_limiter = PrincipalRateLimiter(
        mutations=RateLimit(capacity=1, per_seconds=60.0), reads=RateLimit(capacity=1, per_seconds=60.0)
    )
    document = await _correlated_reply(api, inquiry_id, "Synthetic reply inside the reply bucket.")
    first = await api.client.post(
        f"{MAIL}/replies", headers=api.worker.headers("mwr1-c2-limit-01"), json=document
    )
    assert first.status_code == 200, first.text
    limited = await api.client.post(
        f"{MAIL}/replies", headers=api.worker.headers("mwr1-c2-limit-02"), json=document
    )
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert error_of(limited)["code"] == "RATE_LIMITED"
    # Other worker mutations keep their own (generic) bucket.
    beat = await api.client.post(
        f"{MAIL}/heartbeat", headers=api.worker.headers(), json=heartbeat_body(api.worker.mailbox_id)
    )
    assert beat.status_code == 200, beat.text


async def test_the_hourly_new_reply_cap_is_a_429_with_retry_after(
    reply_api: tuple[ControlHarness, str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _reply, inquiry_id = reply_api
    monkeypatch.setattr(replies_repo, "MAX_NEW_REPLIES_PER_MAILBOX_PER_HOUR", 1)
    document = await _correlated_reply(api, inquiry_id, "Synthetic second message beyond the hourly cap.")
    limited = await api.client.post(
        f"{MAIL}/replies", headers=api.worker.headers("mwr1-c2-volume-01"), json=document
    )
    assert limited.status_code == 429, limited.text
    assert int(limited.headers["retry-after"]) >= 1
    error = error_of(limited)
    assert error["code"] == "RATE_LIMITED" and error["details"] == {"reason": "mail_worker_ingest_volume"}


async def test_a_worker_report_still_being_recorded_answers_in_progress(control_api: ControlHarness) -> None:
    """The same report key still running elsewhere is a retryable ``in_progress`` conflict (the
    worker retries later); it is never applied twice."""
    api = control_api
    _inquiry_id, intent = await dispatch_intent(api.db, api.world, api.worker.mailbox_id)
    claim = await api.client.post(
        f"{MAIL}/send-intents/{intent.intent_id}/claim",
        headers=api.worker.headers(f"claim-{intent.intent_id}-c2"),
        json=claim_body(intent, api.worker.mailbox_id),
    )
    assert claim.status_code == 200 and claim.json()["proceed"] is True, claim.text
    key = f"report-{intent.intent_id}-sent_items_confirmed"
    body = report_body(intent, "sent_items_confirmed")
    async with api.db.transaction() as conn:
        identity = await mail_workers_repo.resolve_worker(conn, api.worker.token)
    actor = identity.actor("req-c2-report")
    request_hash = idempotency.request_hash_for(REPORT_OPERATION, MailWorkerSendReport.model_validate(body))
    async with unit_of_work(api.db, actor) as conn:
        started = await idempotency.begin(conn, actor, REPORT_OPERATION, key, request_hash)
    assert isinstance(started, idempotency.NewRequest)
    response = await api.client.post(
        f"{MAIL}/send-intents/{intent.intent_id}/report", headers=api.worker.headers(key), json=body
    )
    assert response.status_code == 409, response.text
    error = error_of(response)
    assert error["retryable"] is True and error["details"] == {"reason": "in_progress"}
