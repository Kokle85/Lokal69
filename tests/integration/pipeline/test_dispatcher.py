"""Outbox dispatcher (spec 13, 18, 22): gates, revalidation, MCP Events delivery semantics.

Callbacks are served by an in-process ``httpx.MockTransport`` behind the real `SafeHttpClient`
(SSRF policy, signing, no redirects); nothing leaves the machine. Every event and subscription is
SYNTHETIC; the callback host is a reserved example name resolved by a fake resolver.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tests.integration.pipeline.support import (
    SLACK_CHANNEL,
    PipelineEnv,
    approve_events_route,
    approve_slack_route,
    deliveries_of,
    events_settings,
    outbox_row,
    real_case,
    run,
    slack_settings,
    verified_subscriber,
)

from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.safe_http import SafeHttpClient, SafeHttpError, SafeHttpFailure, SafeResponse
from suv_deals.integrations.webhook_signing import parse_whsec, verify_inbound
from suv_deals.persistence import bindings_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.dispatcher import Dispatcher

pytestmark = pytest.mark.db

PUBLIC_IP = "93.184.215.14"  # documentation-style public answer of the fake resolver


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return [PUBLIC_IP]


class Callback:
    """Records every POST and answers with the configured status sequence."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses) or [200]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, json={"ok": status < 300})

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


class TimeoutThenOk:
    """A `SafeHttp` whose first POST times out after the request was sent (possibly delivered)."""

    def __init__(self) -> None:
        self.calls: list[Mapping[str, str]] = []

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str],
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        del url, content, timeout_s, max_response_bytes
        self.calls.append(dict(headers))
        if len(self.calls) == 1:
            raise SafeHttpError(SafeHttpFailure.TIMEOUT_AFTER_SEND, possibly_delivered=True)
        return SafeResponse(
            status_code=202,
            headers={},
            body=b"{}",
            truncated=False,
            elapsed=timedelta(0),
            pinned_ip=PUBLIC_IP,
        )

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:  # pragma: no cover - MCP Events never reads
        raise AssertionError("no GET expected")


def events_env(env: PipelineEnv) -> PipelineEnv:
    """The same workspace/runtime with native MCP Events selected (the runtime is shared, not closed)."""
    settings = events_settings(str(env.ctx.settings.database_url.get_secret_value()))  # type: ignore[union-attr]
    return dataclasses.replace(env, ctx=dataclasses.replace(env.ctx, settings=settings, owns_db=False))


def dispatcher(env: PipelineEnv, http: Any, **kwargs: Any) -> Dispatcher:
    return Dispatcher(env.ctx, http=http, dispatcher_id="dispatcher-test", **kwargs)


async def test_external_delivery_disabled_blocks_a_real_event(env: PipelineEnv) -> None:
    case = await real_case(env)
    callback = Callback()
    report = await dispatcher(env, callback.client()).run_workspace(env.workspace_id)
    assert [(e.event_id, e.state, e.code) for e in report.events] == [
        (case.event_id, "blocked", "EXTERNAL_NOTIFICATIONS_DISABLED")
    ]
    row = outbox_row(env, case.event_id)
    assert row["state"] == "blocked" and row["blocker_code"] == "EXTERNAL_NOTIFICATIONS_DISABLED"
    assert row["send_attempted_at"] is None and callback.requests == []
    # The internal review queue is unaffected: the case is still pending.
    assert env.scalar("select state from app.review_cases where id = %s", case.case_id) == "pending"


async def test_fixture_events_are_never_claimed_even_with_an_active_route(env: PipelineEnv) -> None:
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    fixture_event = live.seed.outbox(
        live.workspace_id,
        is_fixture=True,
        state="blocked",
        blocker_code="FIXTURE_EVENT",
        payload={"schema_version": "1.0", "fixture": True, "summary": "[SYNTHETIC FIXTURE] test"},
    )
    callback = Callback()
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert report.events == [] and report.deliveries == [] and callback.requests == []
    state = live.scalar("select state from ops.outbox where id = %s", fixture_event)
    assert state == "blocked"


async def test_verified_subscription_receives_a_signed_occurrence(env: PipelineEnv) -> None:
    live = events_env(env)
    binding_id = await approve_events_route(live)
    subscriber = await verified_subscriber(live)
    case = await real_case(live)
    assert outbox_row(live, case.event_id)["destination_binding_id"] == binding_id
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state) for e in report.events] == [(case.event_id, "delivered")]
    [request] = callback.requests
    assert request.headers["host"] == "callback.synthetic.example"
    assert request.url.path == "/hooks/review"
    verified = verify_inbound(parse_whsec(subscriber.secret), request.content, dict(request.headers))
    assert verified.webhook_id == str(case.event_id)
    assert verified.subscription_id == subscriber.record.subscription_id
    body = json.loads(request.content)
    assert body["name"] == eb.EVENT_NAME and body["eventId"] == str(case.event_id) and body["cursor"] is None
    assert body["data"]["case_id"] == str(case.case_id) and body["data"]["case_version"] == case.case_version
    assert set(body["data"]) == {
        "case_id",
        "case_version",
        "listing_id",
        "listing_revision",
        "readiness",
        "dashboard_url",
    }
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "accepted" and delivery["last_response_code"] == 200
    row = outbox_row(live, case.event_id)
    assert row["state"] == "delivered" and row["provider_accepted_at"] is not None
    assert row["owner_seen_at"] is None  # a 2xx is a receipt, never evidence that dot reviewed it
    attempts = live.rows(
        "select provider, external_receipt, uncertain from ops.delivery_attempts where outbox_id ="
        " (select id from ops.outbox where event_id = %s)",
        case.event_id,
    )
    assert attempts == [
        {"provider": "mcp_events", "external_receipt": "mcp_events:1/1 accepted", "uncertain": False}
    ]
    # A second cycle sends nothing (the event is delivered; the delivery is unique per subscription).
    again = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert again.events == [] and again.deliveries == [] and len(callback.requests) == 1


async def test_410_is_terminal_for_that_delivery_only(env: PipelineEnv) -> None:
    live = events_env(env)
    await approve_events_route(live)
    subscriber = await verified_subscriber(live)
    case = await real_case(live)
    callback = Callback(410)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in report.events] == [("dead_letter", "DELIVERY_FAILED")]
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "failed" and delivery["last_response_code"] == 410
    assert delivery["safe_error"] == "http_410_gone"
    assert len(callback.requests) == 1  # never retried
    # The subscription itself stays active (410 is terminal for the delivery, not the subscription).
    revoked = live.scalar(
        "select revoked_at from ops.event_subscriptions where id = %s", subscriber.record.id
    )
    assert revoked is None
    assert outbox_row(live, case.event_id)["state"] == "dead_letter"  # visible, not discarded


async def test_timeout_after_send_is_uncertain_then_resent_once_with_the_same_event_id(
    env: PipelineEnv,
) -> None:
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    http = TimeoutThenOk()
    holding = dispatcher(live, http)
    first = await holding.run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in first.events] == [("uncertain", "DELIVERY_UNCERTAIN")]
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "uncertain" and delivery["safe_error"] == "timeout_after_send"
    assert outbox_row(live, case.event_id)["state"] == "uncertain"
    # Within the hold period nothing is re-sent (never a blind resend).
    again = await holding.run_workspace(live.workspace_id)
    assert again.events == [] and again.reconciled == [] and len(http.calls) == 1
    # After the hold: the documented conservative rule schedules ONE resend with the same event id.
    released = dispatcher(live, http, uncertain_policy=eb.UncertainPolicy(hold_for=timedelta(0)))
    follow = await released.run_workspace(live.workspace_id)
    assert follow.reconciled == [(case.event_id, "resend_scheduled")]
    assert outbox_row(live, case.event_id)["state"] == "retry_wait"
    resend = await released.run_workspace(live.workspace_id)
    assert [(e.event_id, e.state) for e in resend.events] == [(case.event_id, "delivered")]
    assert len(http.calls) == 2
    assert http.calls[0]["webhook-id"] == http.calls[1]["webhook-id"] == str(case.event_id)
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "accepted" and delivery["attempts"] == 2


async def test_stale_case_version_is_cancelled_immediately_before_dispatch(env: PipelineEnv) -> None:
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    # New material information after the event was committed: the case moved to a new version.
    live.seed.conn.execute(
        "update app.review_cases set row_version = row_version + 1 where id = %s", (case.case_id,)
    )
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in report.events] == [("cancelled", "STALE_CASE_VERSION")]
    assert callback.requests == [] and deliveries_of(live, case.event_id) == []
    audit = live.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'outbox.cancel_stale'",
        live.workspace_id,
    )
    assert audit == 1


@pytest.mark.parametrize(
    ("verified", "subscribe", "code"),
    [(False, True, "DESTINATION_NOT_VERIFIED"), (True, False, "NO_MATCHING_SUBSCRIPTION")],
)
async def test_unverified_destination_or_missing_subscriber_blocks(
    env: PipelineEnv, verified: bool, subscribe: bool, code: str
) -> None:
    live = events_env(env)
    await approve_events_route(live, verified=verified)
    if subscribe:
        await verified_subscriber(live)
    case = await real_case(live)
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state, e.code) for e in report.events] == [(case.event_id, "blocked", code)]
    assert callback.requests == []


async def test_subscription_filter_mismatch_receives_nothing(env: PipelineEnv) -> None:
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live, profile="manual_4000")
    case = await real_case(live)
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in report.events] == [("blocked", "NO_MATCHING_SUBSCRIPTION")]
    assert callback.requests == [] and deliveries_of(live, case.event_id) == []


def slack_env(env: PipelineEnv) -> PipelineEnv:
    settings = slack_settings(str(env.ctx.settings.database_url.get_secret_value()))  # type: ignore[union-attr]
    return dataclasses.replace(env, ctx=dataclasses.replace(env.ctx, settings=settings, owns_db=False))


class SlackApi:
    """In-process stand-in for chat.postMessage / conversations.history (SYNTHETIC responses)."""

    def __init__(self, *, post_status: int = 200) -> None:
        self.post_status = post_status
        self.requests: list[httpx.Request] = []
        self.posted: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"].startswith("Bearer xoxb-")
        if request.url.path.endswith("/chat.postMessage"):
            self.posted = json.loads(request.content)
            if self.post_status >= 500:
                return httpx.Response(self.post_status, text="upstream failure")
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100", "channel": SLACK_CHANNEL})
        assert request.url.path.endswith("/conversations.history")
        assert self.posted is not None
        message = {
            "ts": "1700000000.000100",
            "bot_id": "B0SYNTHETIC1",
            "text": self.posted["text"],
            "metadata": self.posted["metadata"],
        }
        return httpx.Response(200, json={"ok": True, "messages": [message], "has_more": False})

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


async def test_slack_fallback_posts_only_when_it_is_the_selected_route(env: PipelineEnv) -> None:
    live = slack_env(env)
    await approve_slack_route(live)
    case = await real_case(live)
    api = SlackApi()
    report = await dispatcher(live, api.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state, e.provider) for e in report.events] == [
        (case.event_id, "delivered", "slack")
    ]
    assert api.posted is not None and api.posted["channel"] == SLACK_CHANNEL
    assert str(case.case_id) in api.posted["text"] and "[ref SDR-" in api.posted["text"]
    receipt = live.scalar(
        "select external_receipt from ops.delivery_attempts where outbox_id ="
        " (select id from ops.outbox where event_id = %s)",
        case.event_id,
    )
    assert receipt == f"{SLACK_CHANNEL}:1700000000.000100"
    assert outbox_row(live, case.event_id)["owner_seen_at"] is None


async def test_slack_uncertain_post_is_reconciled_by_lookup_not_resent(env: PipelineEnv) -> None:
    live = slack_env(env)
    await approve_slack_route(live)
    case = await real_case(live)
    api = SlackApi(post_status=503)
    first = await dispatcher(live, api.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.provider) for e in first.events] == [("uncertain", "slack")]
    released = dispatcher(live, api.client(), uncertain_policy=eb.UncertainPolicy(hold_for=timedelta(0)))
    follow = await released.run_workspace(live.workspace_id)
    assert follow.reconciled == [(case.event_id, "delivered")]
    posts = [r for r in api.requests if r.url.path.endswith("/chat.postMessage")]
    assert len(posts) == 1  # found by history lookup: never re-posted
    row = outbox_row(live, case.event_id)
    assert row["state"] == "delivered" and row["provider_accepted_at"] is not None


def make_due(env: PipelineEnv, event_id: Any, *, ago: str = "1 second") -> None:
    """Time travel: the event's waiting deliveries are due now (their retry delay passed)."""
    env.seed.conn.execute(
        "update ops.event_deliveries set next_attempt_at = now() - %s::interval where event_id = %s",
        (ago, event_id),
    )


async def test_disabling_the_destination_stops_pending_retries_at_once(env: PipelineEnv) -> None:
    """Spec 22: the approved + verified binding is rechecked immediately before EVERY delivery,
    including retries swept from earlier cycles; disabling it stops them (nothing is sent)."""
    live = events_env(env)
    binding_id = await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    callback = Callback(503, 200)
    first = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in first.events] == [("retry_wait", "DELIVERY_RETRY")]
    assert len(callback.requests) == 1

    async def disable(conn: Conn) -> None:
        binding = await bindings_repo.get_binding(conn, live.owner, binding_id)
        await bindings_repo.set_binding_enabled(
            conn, live.owner, binding_id, False, expected_version=binding.row_version
        )

    await run(live.ctx, live.owner, disable)
    make_due(live, case.event_id)
    second = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert len(callback.requests) == 1  # the retry was NOT sent to the disabled destination
    assert [d.kind for d in second.deliveries] == ["cancelled"]
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "cancelled" and delivery["attempts"] == 2
    # The event itself becomes visibly blocked on its next claim (never silently discarded).
    live.seed.conn.execute(
        "update ops.outbox set available_at = now() - interval '1 second' where event_id = %s",
        (case.event_id,),
    )
    third = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.state, e.code) for e in third.events] == [("blocked", "NO_ACTIVE_ROUTE")]
    assert len(callback.requests) == 1


async def test_approved_quiet_hours_defer_a_pending_retry(env: PipelineEnv) -> None:
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    callback = Callback(503, 200)
    await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert len(callback.requests) == 1
    # The owner's APPROVED quiet window (UTC) now covers the current time.
    now = datetime.now(UTC)
    quiet = {
        "start": (now - timedelta(hours=1)).strftime("%H:%M:%S"),
        "end": (now + timedelta(hours=1)).strftime("%H:%M:%S"),
        "timezone": "UTC",
        "approved": True,
        "urgent_bypass": False,
    }
    live.seed.conn.execute(
        "update app.notification_preferences set quiet_hours = %s::jsonb where workspace_id = %s",
        (json.dumps(quiet), live.workspace_id),
    )
    make_due(live, case.event_id)
    deferred = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert len(callback.requests) == 1  # nothing is sent inside the quiet window
    assert [d.kind for d in deferred.deliveries] == ["retry_wait"]
    later = live.scalar(
        "select next_attempt_at > now() + interval '50 minutes' from ops.event_deliveries"
        " where event_id = %s",
        case.event_id,
    )
    assert later is True


async def test_a_backlog_of_older_deliveries_does_not_burn_the_events_attempts(env: PipelineEnv) -> None:
    """While the dispatcher holds an event's lease it claims THAT event's deliveries directly
    (``claim_due_deliveries(event_ids=...)``), so a backlog of older due deliveries of other events
    can neither delay it nor consume its outbox attempts; the end-of-cycle sweep then sends the
    older ones in the same cycle."""
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    cases = [await real_case(live) for _ in range(6)]
    failing = Callback(503)
    await dispatcher(live, failing.client()).run_workspace(live.workspace_id)
    assert len(failing.requests) == 6
    held, *older = cases
    for case in older:
        make_due(live, case.event_id, ago="10 minutes")
        live.seed.conn.execute(
            "update ops.outbox set available_at = now() + interval '1 hour' where event_id = %s",
            (case.event_id,),
        )
    make_due(live, held.event_id, ago="1 minute")  # the youngest due delivery: claimed last
    live.seed.conn.execute(
        "update ops.outbox set available_at = now() - interval '1 minute' where event_id = %s",
        (held.event_id,),
    )
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state) for e in report.events] == [(held.event_id, "delivered")]
    assert len(callback.requests) == 6  # the held event's delivery, then the five older ones
    row = outbox_row(live, held.event_id)
    assert row["state"] == "delivered" and row["attempts"] == 2  # claimed once per cycle
    assert all(deliveries_of(live, c.event_id)[0]["state"] == "accepted" for c in cases)


@pytest.mark.parametrize(
    ("refusal", "state"),
    [("STALE_CASE_VERSION", "cancelled"), ("DESTINATION_NOT_VERIFIED", "blocked")],
)
async def test_a_refusal_right_before_sending_settles_the_held_event(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch, refusal: str, state: str
) -> None:
    """The per-delivery guard runs again immediately before the request; if the event became stale
    (or its route was withdrawn) after it was prepared, nothing is sent and the held event is
    cancelled (stale) or blocked (route) with that reason instead of being dead-lettered."""
    from suv_deals.workers import dispatcher as dispatcher_module  # noqa: PLC0415

    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    callback = Callback(200)
    held = dispatcher(live, callback.client())

    async def refuse(*_args: Any) -> Any:
        return dispatcher_module._Gate(refusal=refusal)

    monkeypatch.setattr(held, "_delivery_gate", refuse)
    report = await held.run_workspace(live.workspace_id)
    assert [(e.event_id, e.state, e.code) for e in report.events] == [(case.event_id, state, refusal)]
    assert callback.requests == []
    [delivery] = deliveries_of(live, case.event_id)
    assert delivery["state"] == "cancelled"
    # The truthful guard code is recorded on the delivery (not a generic "invalid occurrence").
    assert (
        live.scalar("select safe_error from ops.event_deliveries where event_id = %s", case.event_id)
        == refusal
    )
    assert [(d.kind, d.reason) for d in report.deliveries] == [("cancelled", refusal)]
    row = outbox_row(live, case.event_id)
    assert row["state"] == state and row["send_attempted_at"] is None


async def test_suspected_parser_drift_pauses_opportunity_alerts(env: PipelineEnv) -> None:
    """Spec 25: a ``degraded`` source (suspected parser drift) pauses new alerts like an unhealthy
    one: the event is visibly blocked with SOURCE_ALERTS_PAUSED and nothing is sent."""
    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    case = await real_case(live)
    live.seed.conn.execute(
        "update app.sources set technical_status = 'degraded', version = version + 1 where id ="
        " (select source_id from app.listings where id = %s)",
        (case.listing_id,),
    )
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state, e.code) for e in report.events] == [
        (case.event_id, "blocked", "SOURCE_ALERTS_PAUSED")
    ]
    assert callback.requests == [] and deliveries_of(live, case.event_id) == []


async def test_fixture_lineage_is_frozen_at_ingest_for_dispatch(env: PipelineEnv) -> None:
    """A source switched from fixture to a real mode: events about its earlier fixture listings stay
    fixture (blocked, never claimed, never delivered), and even a non-fixture event row about a
    fixture listing (legacy data) is blocked by the dispatch guard, which reads the LISTING's
    frozen lineage, never the source's current mode."""
    from tests.integration.repos_valuation_reviews.builders import (  # noqa: PLC0415
        add_revision,
        make_listing,
        primary_profile,
        screening,
    )

    from suv_deals.persistence import reviews_repo  # noqa: PLC0415

    live = events_env(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    source = live.seed.source(live.workspace_id)  # mode 'fixture'
    listing, revision = make_listing(live.seed, live.workspace_id, source)

    async def upsert(revision_id: Any) -> Any:
        return await run(
            live.ctx,
            live.system,
            lambda c: reviews_repo.upsert_review_case(
                c,
                live.system,
                listing,
                revision_id,
                screening(),
                None,
                primary_profile(),
                dashboard_base_url="https://dash.synthetic.example",
            ),
        )

    first = await upsert(revision)
    live.seed.conn.execute(
        "update app.sources set mode = 'public_html', version = version + 1 where id = %s", (source,)
    )
    newer = add_revision(live.seed, live.workspace_id, listing, 2, make="Example", model="Trail")
    second = await upsert(newer)
    assert second.case_id == first.case_id and second.event_id is not None
    events = live.rows(
        "select event_id, is_fixture, state from ops.outbox where workspace_id = %s and aggregate_id = %s",
        live.workspace_id,
        first.case_id,
    )
    assert len(events) == 2 and all(e["is_fixture"] and e["state"] == "blocked" for e in events)
    callback = Callback(200)
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert report.events == [] and report.deliveries == [] and callback.requests == []
    # Legacy data: a real-looking case/event pair about a listing that was ingested as fixture.
    case = await real_case(live)
    live.seed.conn.execute("set session_replication_role = replica")  # bypass the frozen-lineage trigger
    try:
        live.seed.conn.execute("update app.listings set is_fixture = true where id = %s", (case.listing_id,))
    finally:
        live.seed.conn.execute("set session_replication_role = origin")
    report = await dispatcher(live, callback.client()).run_workspace(live.workspace_id)
    assert [(e.event_id, e.state, e.code) for e in report.events] == [
        (case.event_id, "blocked", "FIXTURE_EVENT")
    ]
    assert callback.requests == [] and deliveries_of(live, case.event_id) == []
