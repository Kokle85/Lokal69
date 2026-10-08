"""Dashboard routes of spec 37 (``api.schemas.V11_DASHBOARD_ROUTES``; docs/api_contract.md 10.2).

Inquiries, replies, the inquiry control (pause/resume), mail-worker health and coverage gaps,
lifecycle/lags and the 15-day evaluation, plus the review changes of this package
(``decided_by_caller`` and the configurable claim lease). Real ASGI app, real PostgreSQL, a
locally signed Supabase-shaped JWT, SYNTHETIC data only.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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
    controls_version,
    dispatch_intent,
    expire_attempt,
    heartbeat_body,
    issue_worker,
    latest_binding_version,
    outlook_world,
    owner_actor,
    reply_body,
    upload_reply,
)
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import CURSOR_SECRET, seed_workspace
from tests.integration.v11_inquiries.support import World, add_vehicle

from suv_deals.api.schemas import V11_ROUTE_INDEX, InquiryListQuery, ReplyListQuery
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope, SuppressionReason
from suv_deals.errors import ErrorCode, Forbidden
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import inquiries_repo, queries, query_snapshots
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


@dataclass
class InquiryHarness(ApiHarness):
    db: Database = None  # type: ignore[assignment]
    seed: Seed = None  # type: ignore[assignment]
    world: World = None  # type: ignore[assignment]
    worker: MailWorker = None  # type: ignore[assignment]


@pytest.fixture
async def dash(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[InquiryHarness]:
    world = await outlook_world(db, seed, "API inquiries A")
    users: Users = add_members(seed, world.workspace_id)
    worker = await issue_worker(db, world)
    metrics = AppMetrics(process_metrics=False)
    # The process gate is open (claims of the synthetic sends proceed); the default keeps it closed.
    app = build_test_app(make_settings(seller_inquiry_mode="automatic"), keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield InquiryHarness(
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
    body = response.json()
    assert set(body) >= {"schema_version", "request_id", "as_of", "data", "warnings", "next_cursor"}
    return body["data"]


# ---------------------------------------------------------------------------------- inquiries


async def test_inquiry_list_and_detail_hide_the_address_from_non_owners(dash: InquiryHarness) -> None:
    inquiry_id, _intent = await accepted_send(dash.client, dash.db, dash.world, dash.worker)
    address = str(dash.world.vehicle.address)
    listed = await dash.get("/api/inquiries", dash.users.reviewer)
    items = _data(listed)["items"]
    assert [i["inquiry_id"] for i in items] == [str(inquiry_id)]
    assert address not in listed.text and "Fahrzeug" not in listed.text  # lists carry no text
    owner_view = _data(await dash.get(f"/api/inquiries/{inquiry_id}", dash.users.owner))
    reviewer_resp = await dash.get(f"/api/inquiries/{inquiry_id}", dash.users.reviewer)
    reviewer_view = _data(reviewer_resp)
    assert owner_view["state"] == reviewer_view["state"] == "accepted"
    assert owner_view["approval_required"] is False
    assert owner_view["recipient"]["address"] == address
    assert reviewer_view["recipient"]["address"] is None
    assert address not in reviewer_resp.text
    assert reviewer_view["recipient"]["address_redacted"] is True
    assert reviewer_view["recipient"]["address_domain"] == address.rsplit("@", 1)[1]
    viewer = await dash.get("/api/inquiries", dash.users.viewer)
    assert viewer.status_code == 403
    missing = await dash.get(f"/api/inquiries/{uuid.uuid4()}", dash.users.owner)
    assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    bad_id = await dash.get("/api/inquiries/not-a-uuid", dash.users.owner)
    assert bad_id.status_code == 422


async def test_inquiry_list_filters_and_pagination(dash: InquiryHarness, db_conn: Any) -> None:
    accepted_id, _ = await accepted_send(dash.client, dash.db, dash.world, dash.worker)
    vehicle = await add_vehicle(dash.db, dash.seed, dash.world.workspace_id)
    second_world = dash.world.with_vehicle(vehicle)
    uncertain_id, intent = await dispatch_intent(dash.db, second_world, dash.worker.mailbox_id)
    expire_attempt(db_conn, intent.intent_id)
    await inquiries_repo.reap_expired_attempts(dash.db, dash.world.workspace_id)
    attention = _data(await dash.get("/api/inquiries?attention_only=true", dash.users.owner))
    assert [i["inquiry_id"] for i in attention["items"]] == [str(uncertain_id)]
    uncertain = _data(await dash.get("/api/inquiries?uncertain_only=true", dash.users.owner))
    assert [i["inquiry_id"] for i in uncertain["items"]] == [str(uncertain_id)]
    by_state = _data(await dash.get("/api/inquiries?state=accepted", dash.users.owner))
    assert [i["inquiry_id"] for i in by_state["items"]] == [str(accepted_id)]
    first = await dash.get("/api/inquiries?limit=1", dash.users.owner)
    cursor = first.json()["next_cursor"]
    assert len(_data(first)["items"]) == 1 and cursor
    second = await dash.get("/api/inquiries", dash.users.owner, params={"limit": "1", "cursor": cursor})
    seen = {_data(first)["items"][0]["inquiry_id"], _data(second)["items"][0]["inquiry_id"]}
    assert seen == {str(accepted_id), str(uncertain_id)}
    assert second.json()["next_cursor"] is None
    other_filter = await dash.get(
        "/api/inquiries", dash.users.owner, params={"limit": "1", "cursor": cursor, "state": "accepted"}
    )
    assert other_filter.status_code == 422  # a cursor is bound to its filters
    assert (await dash.get("/api/inquiries?state=approved", dash.users.owner)).status_code == 422


async def test_inquiries_read_alone_pages_the_inquiry_and_reply_snapshots_only(dash: InquiryHarness) -> None:
    """A principal holding ``inquiries:read`` but neither ``deals:read`` nor ``reviews:read`` (e.g.
    a narrowed credential) may page the seller inquiry/reply lists, and nothing else, through the
    frozen snapshots (``query_snapshots`` accepts it for those two queries only)."""
    accepted_id, _ = await accepted_send(dash.client, dash.db, dash.world, dash.worker)
    vehicle = await add_vehicle(dash.db, dash.seed, dash.world.workspace_id)
    second_id, _ = await accepted_send(dash.client, dash.db, dash.world.with_vehicle(vehicle), dash.worker)
    narrow = ActorContext(
        workspace_id=dash.world.workspace_id,
        principal_id=dash.users.reviewer,
        principal_kind="user",
        role=Role.REVIEWER,
        scopes=frozenset({Scope.INQUIRIES_READ}),
        request_id="req-narrow-inquiries-read",
    )
    async with unit_of_work(dash.db, narrow) as conn:
        first = await queries.list_inquiries(
            conn, narrow, InquiryListQuery.model_validate({"limit": "1"}), secret=CURSOR_SECRET
        )
        assert len(first.data.items) == 1 and first.next_cursor is not None
        second = await queries.list_inquiries(
            conn,
            narrow,
            InquiryListQuery.model_validate({"limit": "1", "cursor": first.next_cursor}),
            secret=CURSOR_SECRET,
        )
        seen = {first.data.items[0].inquiry_id, second.data.items[0].inquiry_id}
        assert seen == {accepted_id, second_id}
        replies = await queries.list_replies(conn, narrow, ReplyListQuery(), secret=CURSOR_SECRET)
        assert replies.data.items == ()
        with pytest.raises(Forbidden):
            await query_snapshots.create_snapshot(conn, narrow, "0" * 64, [], [], query_name="candidates")


# ---------------------------------------------------------------------------------- replies


async def test_reply_views_follow_the_address_visibility_rule(dash: InquiryHarness) -> None:
    inquiry_id, intent = await accepted_send(dash.client, dash.db, dash.world, dash.worker)
    ack = await upload_reply(dash.client, dash.world, dash.worker, inquiry_id, intent)
    reply_id = ack["reply_id"]
    address = str(dash.world.vehicle.address)
    listed = await dash.get(f"/api/replies?inquiry_id={inquiry_id}", dash.users.reviewer)
    items = _data(listed)["items"]
    assert [i["reply_id"] for i in items] == [reply_id]
    assert "verfuegbar" not in listed.text and address not in listed.text  # no bodies in lists
    owner = _data(await dash.get(f"/api/replies/{reply_id}", dash.users.owner))
    reviewer_resp = await dash.get(f"/api/replies/{reply_id}", dash.users.reviewer)
    reviewer = _data(reviewer_resp)
    assert owner["inquiry_id"] == reviewer["inquiry_id"] == str(inquiry_id)
    assert "verfuegbar" in reviewer["sanitized_body"]
    assert reviewer["message_type"] == "seller_reply" and reviewer["quarantined"] is False
    assert owner["sender"]["address"] == address
    assert reviewer["sender"]["address"] is None and address not in reviewer_resp.text
    quarantined = _data(await dash.get("/api/replies?quarantined_only=true", dash.users.owner))
    assert quarantined["items"] == []
    assert (await dash.get(f"/api/replies/{uuid.uuid4()}", dash.users.owner)).status_code == 404
    assert (await dash.get("/api/replies", dash.users.viewer)).status_code == 403


async def test_quarantined_reply_text_is_for_the_owner_only(dash: InquiryHarness) -> None:
    """A quarantined reply is an unverified possible match (here the inquiry's Message-ID quoted
    from an address that is not the verified seller's) and may be unrelated personal mail of the
    owner: like an address (``recipient_address_visible``), its text is shown to the owner only;
    a reviewer sees its metadata and quarantine reason (``content_withheld``)."""
    inquiry_id, intent = await accepted_send(dash.client, dash.db, dash.world, dash.worker)
    document = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=dash.worker.mailbox_id,
        binding_version=await latest_binding_version(dash.client, dash.worker, inquiry_id),
        from_address="family.member@private.example.invalid",
        in_reply_to=intent.rfc_message_id,
        subject="Re: Familienessen am Freitag (synthetic)",
        body="Liebe Gruesse, das Familienessen ist am Freitag um 19 Uhr. Synthetic private note.",
    )
    uploaded = await dash.client.post(
        f"{MAIL}/replies", headers=dash.worker.headers("mwr1-quarantine-dash-0001"), json=document
    )
    assert uploaded.status_code == 200, uploaded.text
    reply_id = uploaded.json()["reply_id"]
    owner = _data(await dash.get(f"/api/replies/{reply_id}", dash.users.owner))
    assert owner["quarantined"] is True and owner["content_withheld"] is False
    assert "Familienessen" in owner["sanitized_body"] and "Familienessen" in owner["subject"]
    reviewer_resp = await dash.get(f"/api/replies/{reply_id}", dash.users.reviewer)
    assert "Familienessen" not in reviewer_resp.text and "Freitag" not in reviewer_resp.text
    reviewer = _data(reviewer_resp)
    assert reviewer["quarantined"] is True and reviewer["quarantine_reason"] == owner["quarantine_reason"]
    assert reviewer["content_withheld"] is True
    assert reviewer["sanitized_body"] == "" and reviewer["subject"] == "" and reviewer["claims"] is None
    listed = _data(await dash.get("/api/replies?quarantined_only=true", dash.users.reviewer))
    assert [i["reply_id"] for i in listed["items"]] == [reply_id]  # its existence stays visible


# ---------------------------------------------------------------------------------- control


async def test_pause_is_idempotent_versioned_and_resume_is_owner_only(dash: InquiryHarness) -> None:
    control = _data(await dash.get("/api/inquiry-control", dash.users.reviewer))
    version = control["version"]
    assert control["kill_switch"] is False and control["approval_required"] is False
    assert control["removable_suppressions"] == 0
    body = {
        "expected_version": version,
        "reason": "Owner pause for maintenance",
        "idempotency_key": "pause-0001",
    }
    forbidden = await dash.post("/api/inquiry-control/pause", dash.users.reviewer, body)
    assert forbidden.status_code == 403  # inquiries:pause is owner-only
    paused = await dash.post("/api/inquiry-control/pause", dash.users.owner, body)
    result = _data(paused)
    assert result["version"] == version + 1 and result["already_paused"] is False
    replay = await dash.post("/api/inquiry-control/pause", dash.users.owner, body)
    assert _data(replay) == result
    changed = await dash.post(
        "/api/inquiry-control/pause", dash.users.owner, {**body, "reason": "Another reason entirely"}
    )
    assert changed.status_code == 409 and error_of(changed)["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await dash.post(
        "/api/inquiry-control/pause",
        dash.users.owner,
        {"expected_version": version, "reason": "Stale version", "idempotency_key": "pause-0002"},
    )
    assert stale.status_code == 409 and error_of(stale)["code"] == "VERSION_CONFLICT"
    header = await dash.post(
        "/api/inquiry-control/pause",
        dash.users.owner,
        {**body, "idempotency_key": "pause-0003"},
        headers={"Idempotency-Key": "pause-9999"},
    )
    assert header.status_code == 422
    after = _data(await dash.get("/api/inquiry-control", dash.users.owner))
    assert after["kill_switch"] is True and after["version"] == version + 1
    resume_body = {
        "expected_version": version + 1,
        "reason": "Owner resumes after maintenance",
        "idempotency_key": "resume-0001",
    }
    assert (
        await dash.post("/api/inquiry-control/resume", dash.users.reviewer, resume_body)
    ).status_code == 403
    resumed = _data(await dash.post("/api/inquiry-control/resume", dash.users.owner, resume_body))
    assert resumed["kill_switch"] is False and resumed["version"] == version + 2
    assert resumed["suppressions_removed"] == 0


async def test_resume_can_remove_kill_switch_suppressions_with_audit(dash: InquiryHarness) -> None:
    admin = owner_actor(dash.world.workspace_id)
    async with unit_of_work(dash.db, admin) as conn:
        await inquiries_repo.add_suppression(
            conn,
            admin,
            scope="workspace",
            key=str(dash.world.workspace_id),
            reason=SuppressionReason.KILL_SWITCH,
            evidence={"synthetic": True},
        )
        # Removable while the current standing authorization is effective (it is in this world).
        await inquiries_repo.add_suppression(
            conn,
            admin,
            scope="workspace",
            key=str(dash.world.workspace_id),
            reason=SuppressionReason.AUTHORIZATION_REVOKED,
            evidence={"synthetic": True},
        )
        await inquiries_repo.add_suppression(
            conn,
            admin,
            scope="seller",
            key=f"seller_entity:{dash.world.seller_entity_id}",
            reason=SuppressionReason.SELLER_OPT_OUT,
            evidence={"synthetic": True},
        )
    control = _data(await dash.get("/api/inquiry-control", dash.users.owner))
    assert control["removable_suppressions"] == 2  # the opt-out is never removable here
    version = await controls_version(dash.db, dash.world.workspace_id)
    resumed = _data(
        await dash.post(
            "/api/inquiry-control/resume",
            dash.users.owner,
            {
                "expected_version": version,
                "reason": "Owner clears the kill-switch suppressions",
                "idempotency_key": "resume-remove-0001",
                "remove_suppressions": True,
            },
        )
    )
    assert resumed["suppressions_removed"] == 2
    remaining = dash.seed.conn.execute(
        "select reason from ops.email_suppressions where workspace_id = %s and removed_at is null",
        (dash.world.workspace_id,),
    ).fetchall()
    assert [r[0] for r in remaining] == ["seller_opt_out"]
    audits = dash.seed.conn.execute(
        "select count(*) from ops.audit_events where workspace_id = %s and target_type = 'email_suppression'"
        " and action = 'email_suppression.remove' and actor_kind = 'user'",
        (dash.world.workspace_id,),
    ).fetchone()
    assert audits is not None and audits[0] == 2  # one audited removal each, by the owner


async def test_inquiry_control_before_any_controls_is_a_declared_not_found(bare_api: ApiHarness) -> None:
    """Until ``suv-deals inquiries authorize`` creates the controls row, the control view is a
    ``404 NOT_FOUND`` that the route contract declares, and a pause is the declared ``409``."""
    assert ErrorCode.NOT_FOUND in V11_ROUTE_INDEX["GET /api/inquiry-control"].errors
    missing = await bare_api.get("/api/inquiry-control", bare_api.users.owner)
    assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    pause = await bare_api.post(
        "/api/inquiry-control/pause",
        bare_api.users.owner,
        {"expected_version": 1, "reason": "Pause before setup", "idempotency_key": "pause-none-0001"},
    )
    assert pause.status_code == 409 and error_of(pause)["code"] == "VERSION_CONFLICT"
    assert error_of(pause)["details"]["reason"] == "inquiry_controls_missing"
    assert ErrorCode.VERSION_CONFLICT in V11_ROUTE_INDEX["POST /api/inquiry-control/pause"].errors


# ---------------------------------------------------------------------------------- health


async def test_mail_worker_health_and_coverage_gaps(dash: InquiryHarness) -> None:
    empty = _data(await dash.get("/api/mail-workers/health", dash.users.reviewer))
    assert [m["mailbox_binding_id"] for m in empty["mailboxes"]] == [str(dash.worker.mailbox_id)]
    assert empty["mailboxes"][0]["monitoring_active"] is False  # never claimed without evidence
    assert empty["mailboxes"][0]["heartbeat_status"] in ("unknown", "down")
    gap_start = datetime.now(UTC) - timedelta(hours=2)
    beat = heartbeat_body(
        dash.worker.mailbox_id,
        gaps=[
            {"kind": "worker_offline", "started_at": gap_start.isoformat(), "ended_at": None},
            {
                "kind": "outlook_closed",
                "started_at": (gap_start - timedelta(hours=3)).isoformat(),
                "ended_at": (gap_start - timedelta(hours=2)).isoformat(),
            },
        ],
    )
    sent = await dash.client.post(f"{MAIL}/heartbeat", headers=dash.worker.headers(), json=beat)
    assert sent.status_code == 200, sent.text
    health = await dash.get("/api/mail-workers/health", dash.users.owner)
    box = _data(health)["mailboxes"][0]
    assert box["heartbeat_status"] == "healthy" and box["outlook_status"] == "healthy"
    assert str(dash.world.vehicle.address) not in health.text
    gaps = _data(await dash.get("/api/mail-workers/coverage-gaps", dash.users.owner))
    kinds = [(i["gap"]["kind"], i["gap"]["open"]) for i in gaps["items"]]
    assert kinds[0] == ("worker_offline", True)  # open gaps first
    assert ("outlook_closed", False) in kinds
    assert gaps["open_gap_count"] >= 1
    assert (
        await dash.get("/api/mail-workers/health?include_revoked=maybe", dash.users.owner)
    ).status_code == 422
    assert (await dash.get("/api/mail-workers/health", dash.users.viewer)).status_code == 403


# ---------------------------------------------------------------------------------- lifecycle and evaluation


async def test_lifecycle_lags_and_listing_lifecycle(dash: InquiryHarness) -> None:
    lags = _data(await dash.get("/api/lifecycle/lags", dash.users.viewer))
    assert {"sources", "notification_processing_lag", "mail_reply_detection_lag"} <= set(lags)
    for lag in (lags["notification_processing_lag"], lags["mail_reply_detection_lag"]):
        assert (lag["status"] == "measured") == (lag["value_seconds"] is not None)  # unknown is never 0
    listing = _data(await dash.get(f"/api/listings/{dash.world.listing_id}/lifecycle", dash.users.viewer))
    assert listing["listing_id"] == str(dash.world.listing_id)
    assert listing["detection_delay"]["status"] != "measured" or listing["source_published_trusted"]
    missing = await dash.get(f"/api/listings/{uuid.uuid4()}/lifecycle", dash.users.viewer)
    assert missing.status_code == 404


async def test_evaluation_reports_zero_as_zero(dash: InquiryHarness) -> None:
    response = await dash.get("/api/evaluation", dash.users.owner)
    report = _data(response)
    assert report["optimises_for_volume"] is False
    assert report["qualifying_deal_ids"] == []  # zero suitable deals is reported as zero
    assert report["well_matched_vehicles"] == 0 and report["seller_replies"] == 0
    assert report["best_supported_economics"] is None
    codes = {w["code"] for w in response.json()["warnings"]}
    assert "THRESHOLD_PROPOSED" in codes
    assert (await dash.get("/api/evaluation?days=30", dash.users.owner)).status_code == 422
    assert (await dash.get("/api/evaluation?days=15", dash.users.owner)).status_code == 200
    assert (await dash.get("/api/evaluation", dash.users.viewer)).status_code == 403


# ---------------------------------------------------------------------------------- review changes


async def test_review_decisions_say_whether_the_caller_recorded_them(data_api: DataHarness) -> None:
    reviewer, other = data_api.users.reviewer, data_api.users.second_reviewer
    case_id = data_api.data.cases["priced"]
    claim = await data_api.post(
        f"/api/reviews/{case_id}/claim",
        reviewer,
        {"expected_version": 1, "idempotency_key": "claim-dbc-0001"},
    )
    token = claim.json()["data"]["claim_token"]
    submitted = await data_api.post(
        f"/api/reviews/{case_id}/submit",
        reviewer,
        {
            "claim_token": token,
            "expected_version": 2,
            "listing_revision": 2,
            "valuation_id": str(data_api.data.valuations["estimated"]),
            "outcome": "watch",
            "reason_codes": ["SYNTHETIC_PRICE_WATCH"],
            "summary": "SYNTHETIC: watch the price; comparables support the band.",
            "evidence_ids": [],
            "idempotency_key": "submit-dbc-0001",
        },
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["data"]["decided_by_caller"] is True
    mine = (await data_api.get(f"/api/reviews/{case_id}", reviewer)).json()["data"]["decisions"]
    theirs = (await data_api.get(f"/api/reviews/{case_id}", other)).json()["data"]["decisions"]
    assert [d["decided_by_caller"] for d in mine] == [True]
    assert [d["decided_by_caller"] for d in theirs] == [False]


async def test_review_claim_lease_follows_the_configured_duration(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    data = await seed_workspace(db, seed, "API claim lease")
    users = add_members(seed, data.workspace_id)
    app = build_test_app(make_settings(review_claim_duration_seconds=900), keys, db)
    async with running_client(app) as client:
        headers = {"Authorization": f"Bearer {tokens.mint(users.reviewer)}"}
        response = await client.post(
            f"/api/reviews/{data.cases['priced']}/claim",
            headers=headers,
            json={"expected_version": 1, "idempotency_key": "claim-lease-0001"},
        )
        assert response.status_code == 200, response.text
        expires = datetime.fromisoformat(response.json()["data"]["expires_at"].replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(response.json()["as_of"].replace("Z", "+00:00"))
        assert timedelta(seconds=880) <= expires - as_of <= timedelta(seconds=905)
    with pytest.raises(ValueError):
        make_settings(review_claim_duration_seconds=30)
    with pytest.raises(ValueError):
        make_settings(review_claim_duration_seconds=7200)
