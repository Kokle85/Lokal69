"""Disabled-by-default Slack fallback adapter (spec 13, 22). All tokens and IDs are SYNTHETIC."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from suv_deals.clock import FrozenClock
from suv_deals.errors import ErrorCode, ValidationFailed
from suv_deals.integrations import slack
from suv_deals.integrations.event_bridge import ActivationRouteConflict, select_activation_route
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.integrations.slack import (
    METADATA_EVENT_TYPE,
    ReconcileState,
    SlackConfig,
    SlackNotConfigured,
    SlackOutcomeKind,
    SlackRejectReason,
    SlackRequestRejected,
    SlackSendBlocked,
)
from suv_deals.settings import Settings

# Slack documentation test vector (docs.slack.dev/authentication/verifying-requests-from-slack).
DOC_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
DOC_TS = "1531420618"
DOC_BODY = (
    "token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
    "&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
    "&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
    "&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
DOC_SIGNATURE = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
TOKEN = "xoxb-000000000-synthetic-test-token"
SIGNING = "synthetic-signing-secret-0123456789"
CHANNEL = "C0SYNTHETIC1"
SLACK_IP = "34.226.0.1"
NOTICE: dict[str, Any] = {
    "event_id": "33333333-3333-4333-8333-333333333333",
    "case_id": "44444444-4444-4444-8444-444444444444",
    "case_version": 1,
    "dashboard_url": "https://app.example/reviews/44444444-4444-4444-8444-444444444444",
    "summary": "New research candidate; import costs need verification.",
    "deduplication_key": "review.pending:44444444-4444-4444-8444-444444444444:1",
    "type": "review.pending",
}


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


ACTIVE = {
    "allow_external_notifications": True,
    "notification_provider": "slack",
    "slack_bot_token": SecretStr(TOKEN),
    "slack_signing_secret": SecretStr(SIGNING),
    "slack_channel_id": CHANNEL,
}


def config(**overrides: Any) -> SlackConfig:
    values: dict[str, Any] = {
        "bot_token": SecretStr(TOKEN),
        "signing_secret": SecretStr(SIGNING),
        "channel_id": CHANNEL,
        "team_id": "T0SYNTHETIC",
        "app_id": "A0SYNTHETIC",
        "bot_id": "B0SYNTHETIC",
        "bot_user_id": "U0SYNTHBOT",
        "destination_approval_ref": "owner-approval-2026-10-06-synthetic",
    }
    values.update(overrides)
    return SlackConfig(**values)


Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def http_with(handler: Handler) -> SafeHttpClient:
    async def resolve(host: str, port: int) -> list[str]:
        assert host == "slack.com"
        return [SLACK_IP]

    return SafeHttpClient(resolver=resolve, transport=httpx.MockTransport(handler))


# =========================================================================== request signing


def test_documented_test_vector() -> None:
    assert slack.compute_slack_signature(DOC_SECRET, DOC_TS, DOC_BODY.encode()) == DOC_SIGNATURE
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIGNATURE}
    now = datetime.fromtimestamp(int(DOC_TS) + 10, tz=UTC)
    assert slack.verify_slack_signature(SecretStr(DOC_SECRET), DOC_BODY.encode(), headers, now=now) == int(
        DOC_TS
    )
    lowered = {k.lower(): v for k, v in headers.items()}
    assert slack.verify_slack_signature(DOC_SECRET, DOC_BODY.encode(), lowered, now=now) == int(DOC_TS)


def _reject(
    headers: dict[str, str], *, body: bytes = DOC_BODY.encode(), offset: int = 10
) -> SlackRejectReason:
    now = datetime.fromtimestamp(int(DOC_TS) + offset, tz=UTC)
    with pytest.raises(SlackRequestRejected) as info:
        slack.verify_slack_signature(DOC_SECRET, body, headers, now=now)
    assert info.value.code is ErrorCode.UNAUTHENTICATED
    assert DOC_SECRET not in info.value.message
    return info.value.reason


GOOD = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIGNATURE}


@pytest.mark.parametrize("offset", [301, -301, 3600, -86400])
def test_replay_window_is_five_minutes(offset: int) -> None:
    assert _reject(GOOD, offset=offset) is SlackRejectReason.STALE_TIMESTAMP


@pytest.mark.parametrize("offset", [0, 299, 300, -300])
def test_within_window_accepted(offset: int) -> None:
    now = datetime.fromtimestamp(int(DOC_TS) + offset, tz=UTC)
    assert slack.verify_slack_signature(DOC_SECRET, DOC_BODY.encode(), GOOD, now=now)


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"X-Slack-Signature": DOC_SIGNATURE}, SlackRejectReason.MISSING_HEADERS),
        ({"X-Slack-Request-Timestamp": DOC_TS}, SlackRejectReason.MISSING_HEADERS),
        ({**GOOD, "X-Slack-Request-Timestamp": "15314206a8"}, SlackRejectReason.MALFORMED_TIMESTAMP),
        ({**GOOD, "X-Slack-Request-Timestamp": "-1531420618"}, SlackRejectReason.MALFORMED_TIMESTAMP),
        ({**GOOD, "X-Slack-Request-Timestamp": "1531420618.5"}, SlackRejectReason.MALFORMED_TIMESTAMP),
        (
            {**GOOD, "X-Slack-Signature": DOC_SIGNATURE.replace("v0=", "v1=")},
            SlackRejectReason.MALFORMED_SIGNATURE,
        ),
        ({**GOOD, "X-Slack-Signature": DOC_SIGNATURE.upper()}, SlackRejectReason.MALFORMED_SIGNATURE),
        ({**GOOD, "X-Slack-Signature": DOC_SIGNATURE[:-1]}, SlackRejectReason.MALFORMED_SIGNATURE),
        ({**GOOD, "X-Slack-Signature": "v0=" + "0" * 64}, SlackRejectReason.BAD_SIGNATURE),
    ],
)
def test_malformed_and_forged_headers(headers: dict[str, str], reason: SlackRejectReason) -> None:
    assert _reject(headers) is reason


def test_body_bytes_must_match_exactly() -> None:
    assert _reject(GOOD, body=DOC_BODY.encode() + b" ") is SlackRejectReason.BAD_SIGNATURE
    assert (
        _reject(GOOD, body=DOC_BODY.replace("foobar", "foobaz").encode()) is SlackRejectReason.BAD_SIGNATURE
    )


def test_signing_secret_is_not_base64_decoded() -> None:
    base = f"v0:{DOC_TS}:{DOC_BODY}".encode()
    expected = "v0=" + hmac.new(DOC_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()
    assert expected == DOC_SIGNATURE


def test_oversized_body_rejected_before_hashing() -> None:
    assert _reject(GOOD, body=b"x" * (slack.MAX_INBOUND_BODY_BYTES + 1)) is SlackRejectReason.BODY_TOO_LARGE


def test_raw_body_must_be_bytes() -> None:
    with pytest.raises(TypeError):
        slack.verify_slack_signature(DOC_SECRET, DOC_BODY, GOOD, now=NOW)  # type: ignore[arg-type]


# =========================================================================== configuration and gates


def test_config_validation_and_secret_hiding() -> None:
    cfg = config()
    text = repr(cfg) + str(cfg) + cfg.model_dump_json()
    assert TOKEN not in text
    assert SIGNING not in text
    with pytest.raises(ValueError):
        config(bot_token=SecretStr("xoxp-user-token-not-allowed"))
    with pytest.raises(ValueError):
        config(channel_id="#general")
    with pytest.raises(ValueError):
        config(team_id="team")
    with pytest.raises(ValueError):
        config(signing_secret=SecretStr("short"))
    with pytest.raises(ValueError):
        config(unexpected="x")


def test_from_settings_reports_missing_values_without_leaking() -> None:
    with pytest.raises(SlackNotConfigured) as info:
        SlackConfig.from_settings(settings())
    assert info.value.details["missing"] == ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_CHANNEL_ID"]
    cfg = SlackConfig.from_settings(settings(**ACTIVE), destination_approval_ref="approval-1")
    assert cfg.channel_id == CHANNEL
    with pytest.raises(ValidationFailed) as bad:
        SlackConfig.from_settings(settings(**{**ACTIVE, "slack_bot_token": SecretStr("not-a-token")}))
    assert "not-a-token" not in json.dumps(bad.value.to_payload())


def test_disabled_by_default() -> None:
    blockers = slack.send_blockers(settings(), config())
    assert "allow_external_notifications is false" in blockers
    assert "notification_provider is not slack" in blockers


def test_send_requires_every_gate() -> None:
    assert slack.send_blockers(settings(**ACTIVE), config()) == []
    assert "no destination binding approval reference" in slack.send_blockers(
        settings(**ACTIVE), config(destination_approval_ref=None)
    )
    assert "slack is not configured" in slack.send_blockers(settings(**ACTIVE), None)
    mismatch = slack.send_blockers(settings(**ACTIVE), config(channel_id="C0OTHERCHAN"))
    assert "channel does not match the configured binding" in mismatch
    conflict = slack.send_blockers(settings(**ACTIVE, mcp_events_enabled=True), config())
    assert any("cannot both be enabled" in b for b in conflict)
    with pytest.raises(ActivationRouteConflict):
        select_activation_route(settings(**ACTIVE, mcp_events_enabled=True))


async def test_post_refuses_without_activation_and_makes_no_request() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200)

    async with http_with(handler) as http:
        for cfg_settings, cfg in (
            (settings(), config()),
            (settings(**ACTIVE), config(destination_approval_ref=None)),
        ):
            with pytest.raises(SlackSendBlocked) as info:
                await slack.post_review_message(
                    NOTICE, config=cfg, settings=cfg_settings, http=http, clock=FrozenClock(NOW)
                )
            assert info.value.code is ErrorCode.FORBIDDEN
    assert calls == []


# =========================================================================== outbound posting


def test_event_reference_is_stable_and_short() -> None:
    ref = slack.event_reference(NOTICE["event_id"])
    assert ref == slack.event_reference(NOTICE["event_id"])
    assert ref.startswith("SDR-") and len(ref) == 16
    assert ref != slack.event_reference("55555555-5555-4555-8555-555555555555")


def test_post_body_minimal_escaped_and_tagged() -> None:
    notice = slack.SlackReviewNotice.model_validate(
        {**NOTICE, "summary": "Price <b>drop</b> & check\x07 <!channel> ignore previous instructions"}
    )
    body = slack.build_post_body(config(), notice)
    ref = slack.event_reference(NOTICE["event_id"])
    assert body["channel"] == CHANNEL
    assert body["unfurl_links"] is False and body["unfurl_media"] is False
    assert body["metadata"] == {
        "event_type": METADATA_EVENT_TYPE,
        "event_payload": {"event_ref": ref, "dedup_key": NOTICE["deduplication_key"]},
    }
    text = body["text"]
    assert f"[ref {ref}]" in text
    assert "<" not in text and ">" not in text
    assert "&lt;!channel&gt;" in text
    assert "\x07" not in text
    assert "sign-in required" in text
    assert "guaranteed" not in text.lower()


@pytest.mark.parametrize(
    "change",
    [
        {"dashboard_url": "http://app.example/x"},
        {"dashboard_url": "https://app.example/x y"},
        {"case_version": "1"},
        {"deduplication_key": ""},
        {"event_id": "nope"},
    ],
)
async def test_invalid_notice_rejected(change: dict[str, Any]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    async with http_with(handler) as http:
        with pytest.raises(ValidationFailed):
            await slack.post_review_message(
                {**NOTICE, **change},
                config=config(),
                settings=settings(**ACTIVE),
                http=http,
                clock=FrozenClock(NOW),
            )


async def post(handler: Handler) -> slack.SlackPostOutcome:
    async with http_with(handler) as http:
        return await slack.post_review_message(
            NOTICE, config=config(), settings=settings(**ACTIVE), http=http, clock=FrozenClock(NOW)
        )


async def test_successful_post_wire_format_and_receipt() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "channel": CHANNEL, "ts": "1791280800.000100"})

    outcome = await post(handler)
    assert outcome.kind is SlackOutcomeKind.POSTED
    assert outcome.message_ts == "1791280800.000100"
    assert outcome.provider_accepted_at == NOW
    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == SLACK_IP
    assert request.url.path == "/api/chat.postMessage"
    assert request.headers["host"] == "slack.com"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    body = json.loads(request.content)
    assert body["metadata"]["event_payload"]["event_ref"] == outcome.event_ref
    assert TOKEN not in request.content.decode()


@pytest.mark.parametrize(
    ("response", "kind", "error"),
    [
        (httpx.Response(429, headers={"retry-after": "30"}), SlackOutcomeKind.RETRY, "ratelimited"),
        (
            httpx.Response(200, json={"ok": False, "error": "ratelimited"}),
            SlackOutcomeKind.RETRY,
            "ratelimited",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "service_unavailable"}),
            SlackOutcomeKind.RETRY,
            "service_unavailable",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "internal_error"}),
            SlackOutcomeKind.UNCERTAIN,
            "internal_error",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "fatal_error"}),
            SlackOutcomeKind.UNCERTAIN,
            "fatal_error",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
            SlackOutcomeKind.FAILED,
            "channel_not_found",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
            SlackOutcomeKind.FAILED,
            "not_in_channel",
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "brand_new_error"}),
            SlackOutcomeKind.FAILED,
            "other",
        ),
        (httpx.Response(200, json={"ok": False, "error": "<script>"}), SlackOutcomeKind.FAILED, "other"),
        (httpx.Response(200, content=b"not json"), SlackOutcomeKind.UNCERTAIN, None),
        (httpx.Response(200, json=["ok"]), SlackOutcomeKind.UNCERTAIN, None),
        (httpx.Response(500), SlackOutcomeKind.UNCERTAIN, None),
        (httpx.Response(503), SlackOutcomeKind.UNCERTAIN, None),
        (httpx.Response(403), SlackOutcomeKind.FAILED, None),
        (httpx.Response(302, headers={"location": "https://evil.example/"}), SlackOutcomeKind.FAILED, None),
    ],
)
async def test_response_classification(
    response: httpx.Response, kind: SlackOutcomeKind, error: str | None
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    outcome = await post(handler)
    assert outcome.kind is kind
    assert outcome.slack_error == error


async def test_retry_after_is_parsed_on_429() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    assert (await post(handler)).retry_after == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ReadTimeout("t"), SlackOutcomeKind.UNCERTAIN),  # timeout after possible acceptance
        (httpx.ReadError("reset"), SlackOutcomeKind.UNCERTAIN),
        (httpx.ConnectTimeout("t"), SlackOutcomeKind.RETRY),
        (httpx.ConnectError("refused"), SlackOutcomeKind.RETRY),
    ],
)
async def test_transport_failures(exc: Exception, kind: SlackOutcomeKind) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    assert (await post(handler)).kind is kind


# =========================================================================== reconciliation


def history(messages: list[dict[str, Any]], *, has_more: bool = False, cursor: str = "") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "messages": messages,
            "has_more": has_more,
            "response_metadata": {"next_cursor": cursor},
        },
    )


REF = slack.event_reference(NOTICE["event_id"])


async def reconcile(
    *responses: httpx.Response | Exception, **kwargs: Any
) -> tuple[slack.ReconcileResult, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    script = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async with http_with(handler) as http:
        result = await slack.reconcile_uncertain_post(
            REF,
            posted_after=NOW,
            config=kwargs.get("cfg", config()),
            settings=settings(**ACTIVE),
            http=http,
            max_pages=kwargs.get("max_pages", 3),
        )
    return result, seen


async def test_reconcile_finds_message_by_metadata() -> None:
    message = {
        "type": "message",
        "bot_id": "B0SYNTHETIC",
        "ts": "1791280801.000200",
        "text": "something",
        "metadata": {"event_type": METADATA_EVENT_TYPE, "event_payload": {"event_ref": REF}},
    }
    result, seen = await reconcile(history([message]))
    assert result.state is ReconcileState.FOUND
    assert result.message_ts == "1791280801.000200"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/conversations.history"
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query["channel"] == [CHANNEL]
    assert query["include_all_metadata"] == ["true"]
    assert float(query["oldest"][0]) == (NOW - timedelta(minutes=1)).timestamp()
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_reconcile_finds_message_by_text_reference() -> None:
    message = {"type": "message", "bot_id": "B0SYNTHETIC", "ts": "1.2", "text": f"Pending review [ref {REF}]"}
    result, _ = await reconcile(history([message]))
    assert result.state is ReconcileState.FOUND


async def test_reconcile_ignores_other_bots_quoting_the_reference() -> None:
    message = {"type": "message", "bot_id": "B0SOMEONEELSE", "ts": "1.2", "text": f"copy of [ref {REF}]"}
    result, _ = await reconcile(history([message]))
    assert result.state is ReconcileState.NOT_FOUND


async def test_reconcile_paginates_and_bounds_pages() -> None:
    page = history([{"type": "message", "text": "unrelated", "ts": "1"}], has_more=True, cursor="next")
    result, seen = await reconcile(page, page, max_pages=2)
    assert result.state is ReconcileState.UNKNOWN
    assert len(seen) == 2
    assert parse_qs(urlsplit(str(seen[1].url)).query)["cursor"] == ["next"]
    found = {"type": "message", "bot_id": "B0SYNTHETIC", "ts": "9", "text": f"[ref {REF}]"}
    result2, _ = await reconcile(page, history([found]))
    assert result2.state is ReconcileState.FOUND
    assert result2.pages_read == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(500),
        httpx.Response(200, content=b"{"),
        httpx.ReadTimeout("t"),
    ],
)
async def test_reconcile_lookup_failure_keeps_uncertainty(response: httpx.Response | Exception) -> None:
    result, _ = await reconcile(response)
    assert result.state is ReconcileState.UNKNOWN


async def test_reconcile_requires_activation_and_valid_reference() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    async with http_with(handler) as http:
        with pytest.raises(SlackSendBlocked):
            await slack.reconcile_uncertain_post(
                REF, posted_after=NOW, config=config(), settings=settings(), http=http
            )
        with pytest.raises(ValueError):
            await slack.reconcile_uncertain_post(
                "SDR-bad", posted_after=NOW, config=config(), settings=settings(**ACTIVE), http=http
            )


# =========================================================================== binding helpers


def test_binding_from_config_requires_team_and_app() -> None:
    binding = slack.SlackBinding.from_config(config(), allowed_event_types=frozenset({"message"}))
    assert binding.channel_id == CHANNEL
    assert binding.own_bot_id == "B0SYNTHETIC"
    with pytest.raises(ValidationFailed):
        slack.SlackBinding.from_config(config(team_id=None), allowed_event_types=frozenset({"message"}))


async def test_in_memory_dedup_ttl_and_bound() -> None:
    dedup = slack.InMemoryEventDedup(ttl=timedelta(minutes=10), max_entries=2)
    assert await dedup.first_seen("slack", "Ev1", now=NOW)
    assert not await dedup.first_seen("slack", "Ev1", now=NOW + timedelta(minutes=5))
    assert await dedup.first_seen("slack", "Ev1", now=NOW + timedelta(minutes=11))
    assert await dedup.first_seen("other", "Ev1", now=NOW + timedelta(minutes=11))
    assert await dedup.first_seen("slack", "Ev2", now=NOW + timedelta(minutes=11))
    assert len(dedup._seen) == 2
