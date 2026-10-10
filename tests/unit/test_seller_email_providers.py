"""Seller-email provider adapters, registry and send-safety semantics (spec 37.3, 37.5, 37.8).

All HTTP is mocked with respx (``assert_all_mocked``): no request can reach a real provider.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from suv_deals.clock import FrozenClock
from suv_deals.domain.enums import EmailProviderKind, InquiryState, Tristate
from suv_deals.domain.inquiries import (
    SendAttemptEvidence,
    SendAttemptOutcome,
    SenderBinding,
    SenderStatus,
    reconcile_uncertain,
    requires_message_approval,
    should_retry,
)
from suv_deals.domain.replies import CorrelationOutcome, InquiryBinding, InquiryBindingState
from suv_deals.domain.seller_templates import build_vehicle_label, render
from suv_deals.errors import ValidationFailed
from suv_deals.integrations import seller_email
from suv_deals.integrations.email_providers.base import (
    AccessToken,
    AliasStatus,
    ProviderHealthStatus,
    ReconcileFoundSent,
    ReconcileNotFoundYet,
    ReconcileProvenNotSubmitted,
    ReconcileProviderUnavailable,
    ReconcileWindow,
    SendAccepted,
    SendDefiniteFailure,
    SenderProvider,
    SenderVerification,
    SendFailureReason,
    SendUncertain,
    TokenUnavailable,
    UncertainReason,
    attempt_outcome,
    http_call,
    reconciliation_evidence,
    safe_opaque_id,
    safe_token,
)
from suv_deals.integrations.email_providers.gmail_api import (
    SCOPE_GMAIL_READONLY,
    SCOPE_GMAIL_SEND,
    GmailApiProvider,
    GmailApiSettings,
)
from suv_deals.integrations.email_providers.microsoft_graph import (
    GraphMailProvider,
    GraphSettings,
    normalize_permissions,
)
from suv_deals.integrations.email_providers.outlook_local import (
    REFUSED_AFTER_GRANTED_CLAIM,
    IntentNotStored,
    OutlookAccountReport,
    OutlookHeartbeat,
    OutlookLocalProvider,
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
    OutlookSubmissionState,
    build_send_intent,
    map_outlook_report,
    reconcile_from_reports,
)
from suv_deals.integrations.mime_builder import (
    BuiltMessage,
    OutboundInquiryMessage,
    build_inquiry_message,
    build_message,
    mailbox,
)
from suv_deals.settings import Settings

WHEN = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
ACCOUNT = "vasko.sender@example.com"
SENDER_NAME = "Vasko K"
SELLER = "verkauf@autohaus-example.de"
LISTING_URL = "https://www.example.de/fahrzeuge/123456"
INQUIRY_ID = UUID("66666666-6666-4666-8666-666666666666")
ATTEMPT_ID = UUID("77777777-7777-4777-8777-777777777777")
MAILBOX_ID = UUID("88888888-8888-4888-8888-888888888888")
BINDING_ID = UUID("99999999-9999-4999-8999-999999999999")
GRAPH_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
GMAIL_ROOT = f"https://gmail.googleapis.com/gmail/v1/users/{ACCOUNT}"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
KEY = "inquiry-66666666:attempt-1"
WINDOW = ReconcileWindow(start=WHEN - timedelta(hours=1), end=WHEN + timedelta(hours=1))


# ============================================================================================ fakes


class FakeTokens:
    def __init__(
        self,
        scopes: Sequence[str] = (SCOPE_GMAIL_SEND, SCOPE_GMAIL_READONLY),
        *,
        fail: Exception | None = None,
    ) -> None:
        self.scopes = frozenset(scopes)
        self.fail = fail
        self.calls = 0

    async def access_token(self, *, force_refresh: bool = False) -> AccessToken:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return AccessToken(token=SecretStr("ya29.test-only-token"), scopes=self.scopes)


def binding(
    provider: EmailProviderKind = EmailProviderKind.GMAIL_API,
    *,
    account_id: str = ACCOUNT,
    from_address: str = ACCOUNT,
    reply_to: str | None = None,
) -> SenderBinding:
    return SenderBinding(
        binding_id=BINDING_ID,
        binding_version=1,
        provider=provider,
        account_id=account_id,
        from_address=from_address,
        display_name=SENDER_NAME,
        reply_to_address=reply_to,
    )


def message(*, attempt: int = 1, sender: str = ACCOUNT, reply_to: str | None = None) -> BuiltMessage:
    rendered = render(
        "seller_initial_en_v1",
        build_vehicle_label("Toyota", "RAV4"),
        "ABC-123",
        LISTING_URL,
        SENDER_NAME,
        verified_listing_url=LISTING_URL,
    )
    return build_inquiry_message(
        rendered,
        inquiry_id=INQUIRY_ID,
        attempt_number=attempt,
        sender=mailbox(sender, SENDER_NAME),
        recipient_address=SELLER,
        reply_to_address=reply_to,
        date=WHEN,
    )


def attempt_evidence(outcome: SendAccepted | SendDefiniteFailure | SendUncertain) -> SendAttemptEvidence:
    mapping = attempt_outcome(outcome)
    return SendAttemptEvidence(
        attempt_id=outcome.attempt_id,
        inquiry_id=outcome.inquiry_id,
        sender_binding_id=BINDING_ID,
        provider=outcome.provider,
        fencing_token=1,
        started_at=WHEN,
        finished_at=WHEN,
        outcome=mapping.outcome,
        pre_submission_proof=mapping.pre_submission_proof,
        worker_alive=Tristate.NO,
        stable_message_id=outcome.rfc_message_id,
    )


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        yield client


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


def gmail(http: httpx.AsyncClient, tokens: FakeTokens | None = None, **kwargs: Any) -> GmailApiProvider:
    # Reply retrieval is off by default (SELLER_REPLY_INGEST_MODE=provider_api only): on for unit tests.
    kwargs.setdefault("settings", GmailApiSettings(reply_retrieval_enabled=True))
    return GmailApiProvider(
        binding=kwargs.pop("binding_", binding(reply_to=kwargs.pop("reply_to", None))),
        token_provider=tokens or FakeTokens(),
        http=http,
        clock=FrozenClock(WHEN),
        **kwargs,
    )


def profile_ok(router: respx.MockRouter, email: str = ACCOUNT) -> respx.Route:
    return router.get(f"{GMAIL_ROOT}/profile").mock(
        return_value=httpx.Response(200, json={"emailAddress": email, "historyId": "1000"})
    )


def gmail_error(status: int, reason: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        json={"error": {"code": status, "message": "x", "errors": [{"reason": reason}], "status": "X"}},
    )


# ============================================================================================ Gmail send


async def test_gmail_send_success_records_only_returned_ids(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    built = message()
    profile_ok(router)
    send = router.post(f"{GMAIL_ROOT}/messages/send").mock(
        return_value=httpx.Response(
            200, json={"id": "18f0a1b2c3", "threadId": "18f0a1b2c3", "labelIds": ["SENT"]}
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/18f0a1b2c3", params={"format": "metadata"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "18f0a1b2c3",
                "payload": {
                    "headers": [{"name": "Message-ID", "value": "<CAGmail-rewritten@mail.gmail.com>"}]
                },
            },
        )
    )
    outcome = await gmail(http).send(built, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY)
    assert isinstance(outcome, SendAccepted)
    assert outcome.provider_message_id == "18f0a1b2c3"
    assert outcome.provider_thread_id == "18f0a1b2c3"
    assert outcome.rfc_message_id == built.rfc_message_id
    assert outcome.observed_rfc_message_id == "<CAGmail-rewritten@mail.gmail.com>"
    assert outcome.receipt.kind == "gmail_message_resource" and outcome.receipt.label_ids == ("SENT",)
    assert outcome.raw_sha256 == built.raw_sha256
    request = send.calls.last.request
    assert request.headers["Authorization"] == "Bearer ya29.test-only-token"
    payload = json.loads(request.content)
    assert set(payload) == {"raw"}  # an initial inquiry never carries a threadId
    assert base64.urlsafe_b64decode(payload["raw"]) == built.raw
    assert "/users/me/" not in str(request.url)
    assert attempt_outcome(outcome).outcome == SendAttemptOutcome.ACCEPTED


async def test_gmail_send_accepted_even_if_message_id_read_back_fails(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(
        return_value=httpx.Response(200, json={"id": "abc123", "threadId": "abc123"})
    )
    router.get(f"{GMAIL_ROOT}/messages/abc123").mock(return_value=httpx.Response(503))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendAccepted)
    assert outcome.observed_rfc_message_id is None
    assert outcome.provider_message_id == "abc123"


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"id": "has spaces !"}', b"[1,2]"])
async def test_gmail_2xx_without_usable_ids_never_fabricates_them(
    http: httpx.AsyncClient, router: respx.MockRouter, body: bytes
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=httpx.Response(200, content=body))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendAccepted)
    assert outcome.provider_message_id is None and outcome.provider_thread_id is None
    assert outcome.receipt.response_complete is False


async def test_gmail_body_read_failure_after_200_is_still_accepted() -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"id": "abc'
            raise httpx.ReadError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": ACCOUNT})
        return httpx.Response(200, stream=BrokenStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await gmail(client).send(
            message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
        )
    assert isinstance(outcome, SendAccepted)
    assert outcome.provider_message_id is None
    assert outcome.receipt.response_complete is False


@pytest.mark.parametrize(
    ("status", "reason", "expected", "retryable", "proof"),
    [
        (
            400,
            "badRequest",
            SendFailureReason.PROVIDER_REJECTED_INVALID,
            False,
            "provider_documented_not_sent",
        ),
        (
            400,
            "failedPrecondition",
            SendFailureReason.PROVIDER_REJECTED_INVALID,
            False,
            "provider_documented_not_sent",
        ),
        (
            401,
            "authError",
            SendFailureReason.CREDENTIALS_REJECTED,
            True,
            "credentials_rejected_before_submit",
        ),
        (
            403,
            "insufficientPermissions",
            SendFailureReason.INSUFFICIENT_SCOPE,
            False,
            "provider_documented_not_sent",
        ),
        (
            403,
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            SendFailureReason.INSUFFICIENT_SCOPE,
            False,
            "provider_documented_not_sent",
        ),
        (403, "domainPolicy", SendFailureReason.ACCOUNT_NOT_PERMITTED, False, "provider_documented_not_sent"),
        (404, "notFound", SendFailureReason.PROVIDER_NOT_FOUND, False, "provider_documented_not_sent"),
        (413, "tooLarge", SendFailureReason.MESSAGE_TOO_LARGE, False, "provider_documented_not_sent"),
    ],
)
async def test_gmail_documented_4xx_are_definite_pre_submission_failures(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    status: int,
    reason: str,
    expected: SendFailureReason,
    retryable: bool,
    proof: str,
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=gmail_error(status, reason))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.pre_submission is True
    assert outcome.reason == expected
    assert outcome.retryable is retryable
    assert outcome.proof == proof
    assert outcome.http_status == status
    decision = should_retry(attempt_evidence(outcome), now=WHEN)
    assert decision.retry is retryable
    expected_outcome = (
        SendAttemptOutcome.PRE_SUBMISSION_FAILURE if retryable else SendAttemptOutcome.DEFINITE_REJECTION
    )
    assert attempt_outcome(outcome).outcome == expected_outcome


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            gmail_error(429, "rateLimitExceeded", headers={"Retry-After": "30"}),
            UncertainReason.PROVIDER_THROTTLED,
        ),
        (gmail_error(403, "userRateLimitExceeded"), UncertainReason.PROVIDER_THROTTLED),
        (gmail_error(403, "dailyLimitExceeded"), UncertainReason.PROVIDER_THROTTLED),
        (gmail_error(500, "backendError"), UncertainReason.PROVIDER_SERVER_ERROR),
        (gmail_error(502, "backendError"), UncertainReason.PROVIDER_SERVER_ERROR),
        (httpx.Response(503), UncertainReason.PROVIDER_SERVER_ERROR),
        (httpx.Response(504), UncertainReason.PROVIDER_SERVER_ERROR),
        (
            httpx.Response(302, headers={"Location": "https://evil.example/"}),
            UncertainReason.UNEXPECTED_STATUS,
        ),
        (httpx.Response(409), UncertainReason.UNEXPECTED_STATUS),
        (httpx.Response(408), UncertainReason.UNEXPECTED_STATUS),
    ],
)
async def test_gmail_statuses_after_receipt_are_uncertain_and_never_retried(
    http: httpx.AsyncClient, router: respx.MockRouter, response: httpx.Response, expected: UncertainReason
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=response)
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendUncertain)
    assert outcome.reason == expected
    assert outcome.http_status == response.status_code
    if response.status_code == 429:
        assert outcome.retry_after_seconds == 30
    decision = should_retry(attempt_evidence(outcome), now=WHEN)
    assert decision.retry is False and "HOLD_FOR_RECONCILIATION" in decision.reasons
    assert router.calls.call_count == 2  # profile pre-check + one send; never a second send


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("connect timeout"), httpx.PoolTimeout("pool")],
)
async def test_gmail_connection_failures_before_any_byte_are_retryable_pre_submission(
    http: httpx.AsyncClient, router: respx.MockRouter, error: Exception
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(side_effect=error)
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.pre_submission and outcome.retryable
    assert outcome.reason == SendFailureReason.CONNECTION_FAILED
    assert outcome.proof == "connection_refused_before_submit"
    decision = should_retry(attempt_evidence(outcome), now=WHEN)
    assert decision.retry is True and decision.sender_binding_id == BINDING_ID


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("read timeout"), UncertainReason.READ_TIMEOUT),
        (httpx.WriteTimeout("write timeout"), UncertainReason.WRITE_INTERRUPTED),
        (httpx.WriteError("broken pipe"), UncertainReason.WRITE_INTERRUPTED),
        (httpx.ReadError("reset"), UncertainReason.CONNECTION_LOST),
        (httpx.RemoteProtocolError("server disconnected"), UncertainReason.CONNECTION_LOST),
        (httpx.LocalProtocolError("bad"), UncertainReason.UNEXPECTED_ERROR),
    ],
)
async def test_gmail_failures_after_transmission_may_have_started_are_uncertain(
    http: httpx.AsyncClient, router: respx.MockRouter, error: Exception, expected: UncertainReason
) -> None:
    profile_ok(router)
    router.post(f"{GMAIL_ROOT}/messages/send").mock(side_effect=error)
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendUncertain)
    assert outcome.reason == expected
    evidence = attempt_evidence(outcome)
    assert should_retry(evidence, now=WHEN).retry is False


async def test_gmail_total_deadline_after_hand_over_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": ACCOUNT})
        await anyio.sleep(5)
        return httpx.Response(200, json={"id": "late"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = gmail(client, settings=GmailApiSettings(send_timeout_s=0.05))
        outcome = await provider.send(
            message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
        )
    assert isinstance(outcome, SendUncertain)
    assert outcome.reason == UncertainReason.DEADLINE_EXCEEDED


async def test_gmail_refuses_token_for_another_account_before_sending(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    profile_ok(router, email="someone.else@example.com")
    send = router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=httpx.Response(200, json={"id": "x"}))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == SendFailureReason.ACCOUNT_MISMATCH
    assert outcome.pre_submission and not outcome.retryable
    assert not send.called


@pytest.mark.parametrize(
    ("profile", "retryable"),
    [
        (httpx.Response(503), True),
        (httpx.Response(429), True),
        (httpx.Response(403, json={"error": {"code": 403}}), False),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), True),
    ],
)
async def test_gmail_identity_precheck_failure_never_issues_the_send(
    http: httpx.AsyncClient, router: respx.MockRouter, profile: Any, retryable: bool
) -> None:
    route = router.get(f"{GMAIL_ROOT}/profile")
    if isinstance(profile, Exception):
        route.mock(side_effect=profile)
    else:
        route.mock(return_value=profile)
    send = router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=httpx.Response(200, json={"id": "x"}))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == SendFailureReason.PRECHECK_FAILED
    assert outcome.retryable is retryable
    assert outcome.proof == "local_validation_failed_before_submit"
    assert not send.called


async def test_gmail_precheck_401_is_credentials_rejected(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=httpx.Response(401))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == SendFailureReason.CREDENTIALS_REJECTED and outcome.retryable


@pytest.mark.parametrize(
    ("fail", "reason", "retryable"),
    [
        # Revoked access is retryable on purpose: the domain retry + dispatch preflight then
        # suppresses the inquiry while access stays revoked (spec 37.5), never failing it for good.
        (TokenUnavailable(revoked=True, code="invalid_grant"), SendFailureReason.CREDENTIALS_REVOKED, True),
        (TokenUnavailable(revoked=False, code="store_down"), SendFailureReason.CREDENTIALS_UNAVAILABLE, True),
        (RuntimeError("secret store exploded"), SendFailureReason.CREDENTIALS_UNAVAILABLE, True),
    ],
)
async def test_gmail_token_unavailable_is_pre_submission(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    fail: Exception,
    reason: SendFailureReason,
    retryable: bool,
) -> None:
    outcome = await gmail(http, FakeTokens(fail=fail)).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == reason and outcome.retryable is retryable
    assert outcome.proof == "credentials_rejected_before_submit"
    assert router.calls.call_count == 0
    graph_outcome = await graph(http, FakeTokens(fail=fail)).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(graph_outcome, SendDefiniteFailure)
    assert graph_outcome.reason == reason and graph_outcome.retryable is retryable
    assert router.calls.call_count == 0
    assert attempt_outcome(outcome).outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE


async def test_gmail_token_without_send_scope_is_refused_locally(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outcome = await gmail(http, FakeTokens(scopes=(SCOPE_GMAIL_READONLY,))).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == SendFailureReason.INSUFFICIENT_SCOPE
    assert router.calls.call_count == 0


@pytest.mark.parametrize(
    ("built", "inquiry_id", "key", "expected_reason", "code"),
    [
        (
            message(sender="other.sender@example.com"),
            INQUIRY_ID,
            KEY,
            SendFailureReason.SENDER_MISMATCH,
            "SENDER_MISMATCH",
        ),
        (
            message(reply_to="replies@example.com"),
            INQUIRY_ID,
            KEY,
            SendFailureReason.SENDER_MISMATCH,
            "REPLY_TO_MISMATCH",
        ),
        (message(), uuid4(), KEY, SendFailureReason.LOCAL_VALIDATION_FAILED, "INQUIRY_MISMATCH"),
        (
            message(),
            INQUIRY_ID,
            "short",
            SendFailureReason.LOCAL_VALIDATION_FAILED,
            "IDEMPOTENCY_KEY_INVALID",
        ),
        (
            message(),
            INQUIRY_ID,
            "key with spaces!!",
            SendFailureReason.LOCAL_VALIDATION_FAILED,
            "IDEMPOTENCY_KEY_INVALID",
        ),
    ],
)
async def test_gmail_local_preconditions_refuse_without_io(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    built: BuiltMessage,
    inquiry_id: UUID,
    key: str,
    expected_reason: SendFailureReason,
    code: str,
) -> None:
    tokens = FakeTokens()
    outcome = await gmail(http, tokens).send(
        built, inquiry_id=inquiry_id, attempt_id=ATTEMPT_ID, idempotency_key=key
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == expected_reason
    assert code in outcome.problems
    assert outcome.proof == "local_validation_failed_before_submit" and not outcome.retryable
    assert router.calls.call_count == 0 and tokens.calls == 0


async def test_gmail_display_name_mismatch_is_refused(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    provider = GmailApiProvider(
        binding=binding().model_copy(update={"display_name": "Somebody Else"}),
        token_provider=FakeTokens(),
        http=http,
    )
    outcome = await provider.send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert "SENDER_DISPLAY_NAME_MISMATCH" in outcome.problems
    assert router.calls.call_count == 0


def test_gmail_provider_requires_gmail_binding_with_email_account(http: httpx.AsyncClient) -> None:
    with pytest.raises(ValidationFailed):
        GmailApiProvider(
            binding=binding(EmailProviderKind.MICROSOFT_GRAPH), token_provider=FakeTokens(), http=http
        )
    with pytest.raises(ValidationFailed):
        GmailApiProvider(binding=binding(account_id="not-an-email"), token_provider=FakeTokens(), http=http)
    with pytest.raises(ValidationError):
        GmailApiSettings(api_root="https://evil.example/gmail/v1")
    with pytest.raises(ValidationError):
        GmailApiSettings(api_root="http://gmail.googleapis.com/gmail/v1")


def test_provider_reprs_never_contain_tokens(http: httpx.AsyncClient) -> None:
    provider = gmail(http)
    assert "ya29" not in repr(provider)
    token = AccessToken(token=SecretStr("ya29.secret"), scopes=frozenset())
    assert "ya29" not in repr(token) and "ya29" not in str(token.model_dump())
    with pytest.raises(ValidationError):
        AccessToken(token=SecretStr("has space"))
    assert isinstance(provider, SenderProvider)


# ============================================================================================ Gmail verify


def send_as(*entries: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"sendAs": list(entries)})


PRIMARY = {"sendAsEmail": ACCOUNT, "displayName": SENDER_NAME, "isPrimary": True, "isDefault": True}


async def test_gmail_verify_primary_account(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY))
    result = await gmail(http).verify_account()
    assert result.verified, result.problems
    assert result.from_status == AliasStatus.PRIMARY
    assert result.stable_account_id == ACCOUNT and result.account_email == ACCOUNT
    assert result.provider_display_name == SENDER_NAME
    assert result.missing_scopes == () and result.health == ProviderHealthStatus.OK
    assert result.capabilities.documented_send_idempotency is False


@pytest.mark.parametrize(
    ("entry", "status", "problem"),
    [
        (
            {"sendAsEmail": "alias@example.com", "verificationStatus": "accepted"},
            AliasStatus.VERIFIED_ALIAS,
            None,
        ),
        (
            {"sendAsEmail": "alias@example.com", "verificationStatus": "pending"},
            AliasStatus.PENDING,
            "FROM_ALIAS_PENDING",
        ),
        ({"sendAsEmail": "alias@example.com"}, AliasStatus.NOT_VERIFIABLE, "FROM_NOT_VERIFIED"),
        (
            {"sendAsEmail": "other@example.com", "verificationStatus": "accepted"},
            AliasStatus.NOT_FOUND,
            "FROM_NOT_VERIFIED",
        ),
    ],
)
async def test_gmail_verify_alias_states(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    entry: dict[str, Any],
    status: AliasStatus,
    problem: str | None,
) -> None:
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY, entry))
    provider = gmail(http, binding_=binding(from_address="alias@example.com"))
    result = await provider.verify_account()
    assert result.from_status == status
    if problem is None:
        assert result.verified
    else:
        assert problem in result.problems and not result.verified


async def test_gmail_verify_reply_to_must_be_verified_alias(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY))
    result = await gmail(http, reply_to="replies@example.com").verify_account()
    assert result.reply_to_status == AliasStatus.NOT_FOUND
    assert "REPLY_TO_NOT_VERIFIED" in result.problems and not result.verified


async def test_gmail_verify_detects_account_mismatch_and_skips_alias_check(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    profile_ok(router, email="other@example.com")
    aliases = router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY))
    result = await gmail(http).verify_account()
    assert "ACCOUNT_MISMATCH" in result.problems and not result.verified
    assert not aliases.called


async def test_gmail_verify_reports_scope_gaps_and_broader_scopes(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY))
    tokens = FakeTokens(scopes=("https://mail.google.com/",))
    result = await gmail(http, tokens).verify_account()
    assert result.missing_scopes == ()  # the full scope covers send+read ...
    assert "SCOPES_BROADER_THAN_NEEDED" in result.warnings  # ... but is broader than needed
    narrow = await gmail(http, FakeTokens(scopes=(SCOPE_GMAIL_SEND,))).verify_account()
    assert narrow.missing_scopes == (SCOPE_GMAIL_READONLY,)
    assert "MISSING_SCOPES" in narrow.problems and not narrow.verified


@pytest.mark.parametrize(
    ("fail", "health", "problem"),
    [
        (TokenUnavailable(revoked=True), ProviderHealthStatus.CREDENTIALS_REVOKED, "CREDENTIALS_REVOKED"),
        (TokenUnavailable(revoked=False), ProviderHealthStatus.UNAVAILABLE, "CREDENTIALS_UNAVAILABLE"),
    ],
)
async def test_gmail_verify_without_credentials(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    fail: TokenUnavailable,
    health: ProviderHealthStatus,
    problem: str,
) -> None:
    result = await gmail(http, FakeTokens(fail=fail)).verify_account()
    assert result.health == health and problem in result.problems and not result.verified
    assert result.credentials_revoked is (health == ProviderHealthStatus.CREDENTIALS_REVOKED)


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (httpx.Response(503), "PROVIDER_UNAVAILABLE"),
        (httpx.Response(401), "CREDENTIALS_REJECTED"),
        (httpx.Response(403, json={}), "ACCOUNT_ACCESS_DENIED"),
        (httpx.Response(200, json={"emailAddress": 5}), "PROVIDER_RESPONSE_INVALID"),
    ],
)
async def test_gmail_verify_profile_failures(
    http: httpx.AsyncClient, router: respx.MockRouter, response: httpx.Response, problem: str
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=response)
    result = await gmail(http).verify_account()
    assert problem in result.problems and not result.verified


async def test_gmail_verify_alias_listing_failure(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=httpx.Response(500))
    result = await gmail(http).verify_account()
    assert "ALIAS_CHECK_FAILED" in result.problems and result.from_status == AliasStatus.NOT_CHECKED


# ============================================================================================ Gmail reconcile


async def test_gmail_reconcile_finds_sent_copy_by_rfc_message_id(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    built = message()
    search = router.get(
        f"{GMAIL_ROOT}/messages", params={"q": f"rfc822msgid:{built.rfc_message_id[1:-1]}"}
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]}))
    router.get(f"{GMAIL_ROOT}/messages/m1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m1",
                "threadId": "t1",
                "labelIds": ["SENT"],
                "internalDate": "1791280800000",
                "payload": {"headers": [{"name": "Message-Id", "value": built.rfc_message_id}]},
            },
        )
    )
    result = await gmail(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileFoundSent)
    assert result.evidence.provider_message_id == "m1" and result.evidence.provider_thread_id == "t1"
    assert result.evidence.location == "gmail:SENT"
    assert result.evidence.sent_at is not None
    assert search.calls.last.request.url.params["includeSpamTrash"] == "true"
    decision = reconcile_uncertain(reconciliation_evidence(result))
    assert decision.next_state == InquiryState.ACCEPTED


async def test_gmail_reconcile_empty_search_is_not_proof(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    built = message()
    router.get(f"{GMAIL_ROOT}/messages").mock(
        return_value=httpx.Response(200, json={"resultSizeEstimate": 0})
    )
    result = await gmail(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileNotFoundYet)
    assert result.proves_non_submission is False
    decision = reconcile_uncertain(reconciliation_evidence(result))
    assert decision.next_state is None  # stays uncertain, reservation retained
    assert decision.release_reservation is False
    assert "EMPTY_SEARCH_IS_NOT_PROOF" in decision.reasons


async def test_gmail_reconcile_ignores_non_sent_copies_and_wrong_ids(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    built = message()
    router.get(f"{GMAIL_ROOT}/messages").mock(
        return_value=httpx.Response(
            200, json={"messages": [{"id": "draft1"}, {"id": "other"}, {"id": "gone"}]}
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/draft1").mock(
        return_value=httpx.Response(
            200,
            json={
                "labelIds": ["DRAFT"],
                "payload": {"headers": [{"name": "Message-ID", "value": built.rfc_message_id}]},
            },
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/other").mock(
        return_value=httpx.Response(
            200,
            json={
                "labelIds": ["SENT"],
                "payload": {"headers": [{"name": "Message-ID", "value": "<x@y.example>"}]},
            },
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/gone").mock(return_value=httpx.Response(404))
    result = await gmail(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileNotFoundYet)
    assert result.pending_in_drafts_or_outbox == Tristate.YES


async def test_gmail_reconcile_by_provider_message_id(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    built = message()
    router.get(f"{GMAIL_ROOT}/messages").mock(return_value=httpx.Response(200, json={}))
    router.get(f"{GMAIL_ROOT}/messages/prov1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "prov1",
                "labelIds": ["SENT"],
                "payload": {"headers": [{"name": "Message-ID", "value": "<g@x.y>"}]},
            },
        )
    )
    result = await gmail(http).reconcile(
        inquiry_id=INQUIRY_ID,
        rfc_message_ids=[built.rfc_message_id],
        window=WINDOW,
        provider_message_ids=["prov1"],
    )
    assert isinstance(result, ReconcileFoundSent)
    assert result.evidence.matched_by == "provider_message_id"
    assert result.evidence.rfc_message_id == "<g@x.y>"


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(401), httpx.Response(429)])
async def test_gmail_reconcile_provider_unavailable(
    http: httpx.AsyncClient, router: respx.MockRouter, response: httpx.Response
) -> None:
    router.get(f"{GMAIL_ROOT}/messages").mock(return_value=response)
    result = await gmail(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[message().rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileProviderUnavailable)
    assert reconcile_uncertain(reconciliation_evidence(result)).next_state is None


async def test_gmail_reconcile_validates_inputs(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    provider = gmail(http)
    for ids in ([], ["not-a-message-id"], [f"<a{i}@b.example>" for i in range(21)]):
        with pytest.raises(ValidationFailed):
            await provider.reconcile(inquiry_id=INQUIRY_ID, rfc_message_ids=ids, window=WINDOW)
    with pytest.raises(ValidationFailed):
        await provider.reconcile(
            inquiry_id=INQUIRY_ID,
            rfc_message_ids=["<a@b.example>"],
            window=WINDOW,
            provider_message_ids=["../x"],
        )
    result = await gmail(http, FakeTokens(fail=TokenUnavailable(revoked=True))).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=["<a@b.example>"], window=WINDOW
    )
    assert isinstance(result, ReconcileProviderUnavailable) and result.reason == "credentials_revoked"
    with pytest.raises(ValidationError):
        ReconcileWindow(start=WHEN, end=WHEN)
    with pytest.raises(ValidationError):
        ReconcileWindow(start=WHEN, end=WHEN + timedelta(days=61))


# ============================================================================================ Gmail replies


def reply_binding(
    outbound: str, *, state: InquiryBindingState = InquiryBindingState.ACTIVE
) -> InquiryBinding:
    return InquiryBinding(
        inquiry_id=INQUIRY_ID,
        binding_version=1,
        mailbox_binding_id=MAILBOX_ID,
        provider=EmailProviderKind.GMAIL_API,
        state=state,
        outbound_message_ids=(outbound,),
        provider_thread_ids=("thread-1",),
        verified_seller_aliases=(SELLER,),
        listing_references=("ABC-123",),
        listing_urls=(LISTING_URL,),
    )


def gmail_headers(**values: str) -> list[dict[str, str]]:
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mock_reply_messages(router: respx.MockRouter, outbound: str) -> dict[str, respx.Route]:
    reply_headers = gmail_headers(
        From=f"Autohaus Example <{SELLER}>",
        To=ACCOUNT,
        Subject="Re: Enquiry about Toyota RAV4 \u2013 ABC-123",
        Message_ID="<reply-1@autohaus-example.de>",
        In_Reply_To=outbound,
        References=outbound,
    )
    personal_headers = gmail_headers(
        From="friend@private.example", Subject="Dinner tonight?", In_Reply_To="<abc@private.example>"
    )
    routes = {
        "reply_meta": router.get(f"{GMAIL_ROOT}/messages/m-reply", params={"format": "metadata"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "m-reply",
                    "threadId": "thread-1",
                    "labelIds": ["INBOX"],
                    "payload": {"headers": reply_headers},
                },
            )
        ),
        "reply_full": router.get(f"{GMAIL_ROOT}/messages/m-reply", params={"format": "full"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "m-reply",
                    "threadId": "thread-1",
                    "labelIds": ["INBOX"],
                    "internalDate": "1791284400000",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "headers": reply_headers,
                        "parts": [
                            {
                                "mimeType": "multipart/alternative",
                                "parts": [
                                    {
                                        "mimeType": "text/plain",
                                        "headers": [
                                            {"name": "Content-Type", "value": 'text/plain; charset="utf-8"'}
                                        ],
                                        "body": {
                                            "data": b64url("Hello, yes the vehicle is still available.")
                                        },
                                    },
                                    {"mimeType": "text/html", "body": {"data": b64url("<p>Hello</p>")}},
                                ],
                            },
                            {
                                "mimeType": "application/pdf",
                                "filename": "../../coc.pdf",
                                "body": {"attachmentId": "att-1", "size": 1234},
                            },
                        ],
                    },
                },
            )
        ),
        "personal_meta": router.get(f"{GMAIL_ROOT}/messages/m-personal", params={"format": "metadata"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "m-personal",
                    "threadId": "t-x",
                    "labelIds": ["INBOX"],
                    "payload": {"headers": personal_headers},
                },
            )
        ),
        "personal_full": router.get(f"{GMAIL_ROOT}/messages/m-personal", params={"format": "full"}).mock(
            return_value=httpx.Response(200, json={"id": "m-personal", "payload": {}})
        ),
    }
    return routes


async def test_gmail_history_fetch_returns_only_correlated_replies(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "history": [
                    {
                        "id": "1001",
                        "messagesAdded": [
                            {"message": {"id": "m-reply", "threadId": "thread-1", "labelIds": ["INBOX"]}},
                            {"message": {"id": "m-personal", "threadId": "t-x", "labelIds": ["INBOX"]}},
                            {"message": {"id": "m-own", "threadId": "thread-1", "labelIds": ["SENT"]}},
                        ],
                    }
                ],
                "historyId": "1005",
            },
        )
    )
    routes = mock_reply_messages(router, outbound)
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(outbound)], since=WHEN, cursor="1000"
    )
    assert result.complete and not result.gap_detected
    assert result.next_cursor == "1005"
    assert result.scanned == 2 and result.skipped_unrelated == 1
    assert len(result.replies) == 1
    reply = result.replies[0]
    assert reply.correlation.outcome == CorrelationOutcome.MATCHED
    assert reply.correlation.inquiry_id == INQUIRY_ID
    assert reply.message.identity.provider_thread_id == "thread-1"
    assert reply.message.identity.internet_message_id == "<reply-1@autohaus-example.de>"
    assert "still available" in reply.message.body_text
    assert reply.attachment_refs[0].filename == "coc.pdf"
    assert reply.attachment_refs[0].provider_attachment_id == "att-1"
    assert routes["reply_full"].called
    assert routes["personal_meta"].called
    assert not routes["personal_full"].called  # unrelated personal mail: body never fetched
    assert all(r.message.identity.provider_message_id != "m-personal" for r in result.replies)


async def test_gmail_expired_history_falls_back_to_window_search(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    router.get(f"{GMAIL_ROOT}/history").mock(return_value=httpx.Response(404))
    router.get(f"{GMAIL_ROOT}/profile").mock(
        return_value=httpx.Response(200, json={"emailAddress": ACCOUNT, "historyId": "2000"})
    )
    listing = router.get(f"{GMAIL_ROOT}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "m-reply"}]})
    )
    mock_reply_messages(router, outbound)
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(outbound)], since=WHEN, cursor="1"
    )
    assert result.gap_detected and result.complete
    assert result.next_cursor == "2000"
    assert len(result.replies) == 1
    query = listing.calls.last.request.url.params["q"]
    assert query == f"after:{int(WHEN.timestamp())} -in:sent -in:drafts"


async def test_gmail_window_fetch_without_cursor_and_pending_race(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=httpx.Response(200, json={"historyId": "3000"}))
    router.get(f"{GMAIL_ROOT}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "m-race"}]})
    )
    unknown = f"<inquiry-{uuid4()}.1@example.com>"
    router.get(f"{GMAIL_ROOT}/messages/m-race", params={"format": "metadata"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m-race",
                "payload": {"headers": gmail_headers(From="x@y.example", In_Reply_To=unknown)},
            },
        )
    )
    full = router.get(f"{GMAIL_ROOT}/messages/m-race", params={"format": "full"}).mock(
        return_value=httpx.Response(200, json={})
    )
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(message().rfc_message_id)], since=WHEN
    )
    assert result.pending_retry_locators == ("m-race",)
    assert result.replies == ()
    assert not full.called
    assert result.next_cursor == "3000"


async def test_gmail_revoked_binding_never_grants_access(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200, json={"history": [{"messagesAdded": [{"message": {"id": "m-old"}}]}], "historyId": "1001"}
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/m-old", params={"format": "metadata"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m-old",
                "payload": {"headers": gmail_headers(From="x@y.example", In_Reply_To=outbound)},
            },
        )
    )
    full = router.get(f"{GMAIL_ROOT}/messages/m-old", params={"format": "full"}).mock(
        return_value=httpx.Response(200, json={})
    )
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID,
        bindings=[reply_binding(outbound, state=InquiryBindingState.TOMBSTONED)],
        since=WHEN,
        cursor="1000",
    )
    assert result.replies == () and result.pending_retry_locators == ()
    assert not full.called


async def test_gmail_fetch_provider_unavailable_keeps_cursor(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/history").mock(return_value=httpx.Response(503))
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
    )
    assert not result.complete and result.next_cursor == "1000"
    assert result.problems == ("PROVIDER_UNAVAILABLE",)
    revoked = await gmail(http, FakeTokens(fail=TokenUnavailable(revoked=True))).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
    )
    assert revoked.problems == ("CREDENTIALS_REVOKED",) and revoked.next_cursor == "1000"


async def test_gmail_fetch_message_errors_keep_old_cursor(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200, json={"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}], "historyId": "9"}
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/m1").mock(return_value=httpx.Response(500))
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="5"
    )
    assert not result.complete and result.next_cursor == "5"


async def test_gmail_health(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    profile_ok(router)
    assert (await gmail(http).health()).status == ProviderHealthStatus.OK
    degraded = await gmail(http, FakeTokens(scopes=(SCOPE_GMAIL_SEND,))).health()
    assert degraded.status == ProviderHealthStatus.DEGRADED and "MISSING_SCOPES" in degraded.problems
    revoked = await gmail(http, FakeTokens(fail=TokenUnavailable(revoked=True))).health()
    assert revoked.status == ProviderHealthStatus.CREDENTIALS_REVOKED


# ============================================================================================ Graph


def graph(http: httpx.AsyncClient, tokens: FakeTokens | None = None, **kwargs: Any) -> GraphMailProvider:
    return GraphMailProvider(
        binding=kwargs.pop(
            "binding_",
            binding(
                EmailProviderKind.MICROSOFT_GRAPH, account_id=GRAPH_ID, reply_to=kwargs.pop("reply_to", None)
            ),
        ),
        token_provider=tokens
        or FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.ReadBasic", "offline_access")),
        http=http,
        clock=FrozenClock(WHEN),
        **kwargs,
    )


def me_ok(router: respx.MockRouter, user_id: str = GRAPH_ID, mail: str = ACCOUNT) -> respx.Route:
    return router.get(GRAPH_ME).mock(
        return_value=httpx.Response(
            200, json={"id": user_id, "mail": mail, "userPrincipalName": mail, "displayName": SENDER_NAME}
        )
    )


async def test_graph_send_202_records_no_ids(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    built = message()
    me_ok(router)
    send = router.post(f"{GRAPH_ME}/sendMail").mock(
        return_value=httpx.Response(202, headers={"request-id": "req-1"})
    )
    outcome = await graph(http).send(built, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY)
    assert isinstance(outcome, SendAccepted)
    assert outcome.provider_message_id is None and outcome.provider_thread_id is None
    assert outcome.receipt.kind == "graph_202_accepted" and outcome.receipt.provider_request_id == "req-1"
    request = send.calls.last.request
    assert request.headers["Content-Type"] == "text/plain"
    assert request.headers["client-request-id"] == str(ATTEMPT_ID)
    assert base64.b64decode(request.content) == built.raw
    with pytest.raises(ValidationError):
        SendAccepted.model_validate({**outcome.model_dump(), "provider_message_id": "fabricated"})


async def test_graph_refuses_token_of_another_account(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    me_ok(router, user_id="11111111-2222-4333-8444-555555555555")
    send = router.post(f"{GRAPH_ME}/sendMail").mock(return_value=httpx.Response(202))
    outcome = await graph(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure) and outcome.reason == SendFailureReason.ACCOUNT_MISMATCH
    assert not send.called


@pytest.mark.parametrize(
    ("response", "kind", "detail"),
    [
        (
            httpx.Response(400, json={"error": {"code": "ErrorMimeContentInvalidBase64String"}}),
            "definite",
            SendFailureReason.PROVIDER_REJECTED_INVALID,
        ),
        (
            httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}),
            "definite",
            SendFailureReason.CREDENTIALS_REJECTED,
        ),
        (
            httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}),
            "definite",
            SendFailureReason.INSUFFICIENT_SCOPE,
        ),
        (
            httpx.Response(403, json={"error": {"code": "ErrorSendAsDenied"}}),
            "definite",
            SendFailureReason.ACCOUNT_NOT_PERMITTED,
        ),
        (httpx.Response(404), "definite", SendFailureReason.PROVIDER_NOT_FOUND),
        (httpx.Response(413), "definite", SendFailureReason.MESSAGE_TOO_LARGE),
        (httpx.Response(429, headers={"Retry-After": "12"}), "uncertain", UncertainReason.PROVIDER_THROTTLED),
        (
            httpx.Response(503, headers={"Retry-After": "5"}),
            "uncertain",
            UncertainReason.PROVIDER_SERVER_ERROR,
        ),
        (httpx.Response(507), "uncertain", UncertainReason.PROVIDER_SERVER_ERROR),
        (httpx.Response(200), "accepted", None),
    ],
)
async def test_graph_status_classification(
    http: httpx.AsyncClient, router: respx.MockRouter, response: httpx.Response, kind: str, detail: Any
) -> None:
    me_ok(router)
    router.post(f"{GRAPH_ME}/sendMail").mock(return_value=response)
    outcome = await graph(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert (
        outcome.status
        == {"definite": "definite_failure", "uncertain": "uncertain", "accepted": "accepted"}[kind]
    )
    if isinstance(outcome, SendDefiniteFailure):
        assert outcome.reason == detail and outcome.pre_submission
    if isinstance(outcome, SendUncertain):
        assert outcome.reason == detail
        retry_after = response.headers.get("Retry-After")
        assert outcome.retry_after_seconds == (int(retry_after) if retry_after else None)


@pytest.mark.parametrize(
    ("error", "status"),
    [(httpx.ConnectError("refused"), "definite_failure"), (httpx.ReadTimeout("slow"), "uncertain")],
)
async def test_graph_transport_classification(
    http: httpx.AsyncClient, router: respx.MockRouter, error: Exception, status: str
) -> None:
    me_ok(router)
    router.post(f"{GRAPH_ME}/sendMail").mock(side_effect=error)
    outcome = await graph(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert outcome.status == status


async def test_graph_precheck_failures(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    router.get(GRAPH_ME).mock(return_value=httpx.Response(401))
    send = router.post(f"{GRAPH_ME}/sendMail").mock(return_value=httpx.Response(202))
    outcome = await graph(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert (
        isinstance(outcome, SendDefiniteFailure) and outcome.reason == SendFailureReason.CREDENTIALS_REJECTED
    )
    router.get(GRAPH_ME).mock(return_value=httpx.Response(503))
    outcome = await graph(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure) and outcome.reason == SendFailureReason.PRECHECK_FAILED
    assert outcome.retryable
    assert not send.called
    no_scope = await graph(http, FakeTokens(scopes=("User.Read",))).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert (
        isinstance(no_scope, SendDefiniteFailure) and no_scope.reason == SendFailureReason.INSUFFICIENT_SCOPE
    )


async def test_graph_verify_primary_only(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    me_ok(router)
    result = await graph(http).verify_account()
    assert result.verified, result.problems
    assert result.from_status == AliasStatus.PRIMARY and result.stable_account_id == GRAPH_ID
    alias = await graph(
        http,
        binding_=binding(
            EmailProviderKind.MICROSOFT_GRAPH, account_id=GRAPH_ID, from_address="alias@example.com"
        ),
    ).verify_account()
    assert alias.from_status == AliasStatus.NOT_VERIFIABLE and "FROM_ALIAS_NOT_VERIFIABLE" in alias.problems
    assert "preserves_client_message_id" in alias.capabilities.unverified_offline


async def test_graph_verify_problems(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    me_ok(router, user_id="11111111-2222-4333-8444-555555555555")
    mismatch = await graph(http).verify_account()
    assert "ACCOUNT_MISMATCH" in mismatch.problems
    tokens = FakeTokens(scopes=("https://graph.microsoft.com/Mail.Send", "Mail.ReadWrite"))
    scoped = await graph(http, tokens).verify_account()
    assert "User.Read" in scoped.missing_scopes and "SCOPES_BROADER_THAN_NEEDED" in scoped.warnings
    replies_enabled = await graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.ReadBasic")),
        settings=GraphSettings(reply_retrieval_enabled=True),
    ).verify_account()
    assert replies_enabled.missing_scopes == ("Mail.Read",)
    router.get(GRAPH_ME).mock(return_value=httpx.Response(503))
    assert "PROVIDER_UNAVAILABLE" in (await graph(http).verify_account()).problems


def test_graph_permission_normalisation_and_account_id(http: httpx.AsyncClient) -> None:
    assert normalize_permissions(
        frozenset({"https://graph.microsoft.com/Mail.Send", "User.Read"})
    ) == frozenset({"mail.send", "user.read"})
    with pytest.raises(ValidationFailed):
        GraphMailProvider(
            binding=binding(EmailProviderKind.MICROSOFT_GRAPH), token_provider=FakeTokens(), http=http
        )
    with pytest.raises(ValidationError):
        GraphSettings(api_root="https://graph.evil.example/v1.0")


async def test_graph_reconcile(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    built = message()
    route = router.get(f"{GRAPH_ME}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "d1", "internetMessageId": built.rfc_message_id, "isDraft": True},
                    {
                        "id": "s1",
                        "internetMessageId": built.rfc_message_id,
                        "isDraft": False,
                        "conversationId": "conv-1",
                        "sentDateTime": "2026-10-06T10:00:05Z",
                    },
                ]
            },
        )
    )
    found = await graph(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(found, ReconcileFoundSent)
    assert found.evidence.provider_thread_id == "conv-1"
    assert found.evidence.sent_at == datetime(2026, 10, 6, 10, 0, 5, tzinfo=UTC)
    assert route.calls.last.request.url.params["$filter"] == f"internetMessageId eq '{built.rfc_message_id}'"
    route.mock(
        return_value=httpx.Response(
            200, json={"value": [{"internetMessageId": built.rfc_message_id, "isDraft": True}]}
        )
    )
    draft = await graph(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(draft, ReconcileNotFoundYet) and draft.pending_in_drafts_or_outbox == Tristate.YES
    route.mock(return_value=httpx.Response(200, json={"value": []}))
    empty = await graph(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(empty, ReconcileNotFoundYet) and not empty.proves_non_submission
    route.mock(return_value=httpx.Response(500))
    down = await graph(http).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(down, ReconcileProviderUnavailable)


async def test_graph_reply_retrieval(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    outbound = message().rfc_message_id
    disabled = await graph(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN
    )
    assert disabled.problems == ("REPLY_RETRIEVAL_DISABLED",)
    graph_binding = reply_binding(outbound).model_copy(
        update={"provider": EmailProviderKind.MICROSOFT_GRAPH, "provider_thread_ids": ()}
    )
    router.get(f"{GRAPH_ME}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "r1",
                        "receivedDateTime": "2026-10-06T11:00:00Z",
                        "internetMessageHeaders": [
                            {"name": "From", "value": SELLER},
                            {"name": "In-Reply-To", "value": outbound},
                        ],
                    },
                    {
                        "id": "p1",
                        "receivedDateTime": "2026-10-06T11:05:00Z",
                        "internetMessageHeaders": [{"name": "From", "value": "friend@private.example"}],
                    },
                ],
                "@odata.nextLink": "https://evil.example/next",
            },
        )
    )
    full = router.get(f"{GRAPH_ME}/messages/r1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "r1",
                "internetMessageId": "<reply-1@autohaus-example.de>",
                "receivedDateTime": "2026-10-06T11:00:00Z",
                "internetMessageHeaders": [
                    {"name": "From", "value": SELLER},
                    {"name": "Subject", "value": "Re: Enquiry about Toyota RAV4 \u2013 ABC-123"},
                    {"name": "In-Reply-To", "value": outbound},
                ],
                "body": {"contentType": "text", "content": "Yes, the vehicle is still available."},
            },
        )
    )
    personal = router.get(f"{GRAPH_ME}/messages/p1").mock(return_value=httpx.Response(200, json={}))
    provider = graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.Read")),
        settings=GraphSettings(reply_retrieval_enabled=True),
    )
    result = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[graph_binding], since=WHEN
    )
    assert len(result.replies) == 1 and result.replies[0].correlation.inquiry_id == INQUIRY_ID
    assert full.calls.last.request.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert not personal.called
    assert result.complete is False  # the foreign-host nextLink was refused
    assert result.next_cursor == "2026-10-06T11:05:00Z"


async def test_graph_health(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    me_ok(router)
    assert (await graph(http).health()).status == ProviderHealthStatus.OK
    router.get(GRAPH_ME).mock(return_value=httpx.Response(500))
    assert (await graph(http).health()).status == ProviderHealthStatus.UNAVAILABLE


# ============================================================================================ Outlook local


class FakeGateway:
    def __init__(self) -> None:
        self.intents: list[OutlookSendIntent] = []
        self.fail: Exception | None = None
        self.account: OutlookAccountReport | None = None
        self.heartbeat: OutlookHeartbeat | None = None
        self.reports: list[OutlookSendReport] = []
        self.claimed: set[UUID] = set()

    async def publish_intent(self, intent: OutlookSendIntent) -> None:
        if self.fail is not None:
            raise self.fail
        self.intents.append(intent)

    async def latest_account_report(self, mailbox_binding_id: UUID) -> OutlookAccountReport | None:
        return self.account

    async def latest_heartbeat(self, mailbox_binding_id: UUID) -> OutlookHeartbeat | None:
        return self.heartbeat

    async def reports_for(self, inquiry_id: UUID) -> Sequence[OutlookSendReport]:
        return self.reports

    async def intents_for(self, inquiry_id: UUID) -> Sequence[OutlookSendIntent]:
        return [i for i in self.intents if i.inquiry_id == inquiry_id]

    async def claimed_intents(self, inquiry_id: UUID) -> frozenset[UUID]:
        return frozenset(
            i.intent_id for i in self.intents if i.inquiry_id == inquiry_id and i.intent_id in self.claimed
        )


def outlook(gateway: FakeGateway, **kwargs: Any) -> OutlookLocalProvider:
    return OutlookLocalProvider(
        binding=kwargs.pop("binding_", binding(EmailProviderKind.OUTLOOK_LOCAL)),
        mailbox_binding_id=MAILBOX_ID,
        gateway=gateway,
        clock=FrozenClock(WHEN),
        **kwargs,
    )


def heartbeat(*, age: timedelta = timedelta(seconds=30), running: bool = True) -> OutlookHeartbeat:
    return OutlookHeartbeat(
        mailbox_binding_id=MAILBOX_ID,
        worker_id="laptop-1",
        at=WHEN - age,
        outlook_running=running,
        mailbox_connected=True,
    )


def account_report(**overrides: Any) -> OutlookAccountReport:
    values: dict[str, Any] = {
        "mailbox_binding_id": MAILBOX_ID,
        "worker_id": "laptop-1",
        "reported_at": WHEN - timedelta(minutes=1),
        "outlook_flavour": "classic",
        "stable_account_key": "acct:9f2c1e7a",
        "account_smtp_address": ACCOUNT,
        "account_display_name": SENDER_NAME,
        "account_type": "imap",
    }
    values.update(overrides)
    return OutlookAccountReport(**values)


def report(intent: OutlookSendIntent, state: OutlookSubmissionState, **overrides: Any) -> OutlookSendReport:
    values: dict[str, Any] = {
        "intent_id": intent.intent_id,
        "inquiry_id": intent.inquiry_id,
        "mailbox_binding_id": intent.mailbox_binding_id,
        "worker_id": "laptop-1",
        "state": state,
        "reported_at": WHEN + timedelta(minutes=1),
    }
    values.update(overrides)
    return OutlookSendReport(**values)


async def test_outlook_send_hands_intent_to_worker_and_is_uncertain_until_reported() -> None:
    gateway = FakeGateway()
    built = message()
    outcome = await outlook(gateway).send(
        built, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendUncertain)
    assert outcome.reason == UncertainReason.LOCAL_WORKER_HANDOFF and outcome.awaiting_local_worker
    intent = gateway.intents[0]
    assert intent.intent_id == ATTEMPT_ID and intent.inquiry_id == INQUIRY_ID
    assert intent.to_address == SELLER and intent.from_address == ACCOUNT
    assert intent.subject == built.subject and intent.body_text == built.body
    assert intent.rfc_message_id == built.rfc_message_id and intent.mime_sha256 == built.raw_sha256
    assert intent.not_after - intent.created_at == timedelta(hours=6)
    assert not intent.expired(WHEN) and intent.expired(WHEN + timedelta(hours=6))
    assert not hasattr(intent, "cc") and not hasattr(intent, "attachments")
    assert should_retry(attempt_evidence(outcome), now=WHEN).retry is False


async def test_outlook_handoff_failure_semantics() -> None:
    gateway = FakeGateway()
    gateway.fail = IntentNotStored()
    stored_not = await outlook(gateway).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(stored_not, SendDefiniteFailure)
    assert stored_not.reason == SendFailureReason.LOCAL_HANDOFF_FAILED and stored_not.retryable
    gateway.fail = RuntimeError("connection dropped after commit?")
    unknown = await outlook(gateway).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(unknown, SendUncertain) and unknown.reason == UncertainReason.LOCAL_WORKER_HANDOFF


async def test_outlook_send_local_refusal() -> None:
    gateway = FakeGateway()
    outcome = await outlook(gateway).send(
        message(sender="other.sender@example.com"),
        inquiry_id=INQUIRY_ID,
        attempt_id=ATTEMPT_ID,
        idempotency_key=KEY,
    )
    assert isinstance(outcome, SendDefiniteFailure) and outcome.reason == SendFailureReason.SENDER_MISMATCH
    assert gateway.intents == []


def intent_for() -> OutlookSendIntent:
    return build_send_intent(
        message(),
        attempt_id=ATTEMPT_ID,
        idempotency_key=KEY,
        mailbox_binding_id=MAILBOX_ID,
        binding=binding(EmailProviderKind.OUTLOOK_LOCAL),
        created_at=WHEN,
    )


def test_outlook_report_mapping_distinguishes_local_submission_from_sent_items() -> None:
    intent = intent_for()
    outbox = map_outlook_report(
        intent, report(intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX), observed_at=WHEN
    )
    assert isinstance(outbox, SendUncertain)
    assert outbox.reason == UncertainReason.LOCAL_SUBMISSION_PENDING and outbox.outbox_pending == Tristate.YES
    sent = map_outlook_report(
        intent,
        report(
            intent,
            OutlookSubmissionState.SENT_ITEMS_CONFIRMED,
            sent_items_present=True,
            observed_internet_message_id="<ABC123@outlook.example>",
            account_smtp_address_used=ACCOUNT,
            sent_at=WHEN + timedelta(seconds=30),
        ),
        observed_at=WHEN,
    )
    assert isinstance(sent, SendAccepted)
    assert sent.receipt.kind == "outlook_sent_items"
    assert sent.provider_message_id is None and sent.provider_thread_id is None  # nothing fabricated
    assert sent.observed_rfc_message_id == "<ABC123@outlook.example>"
    assert sent.accepted_at == WHEN + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("reason", "expected", "retryable"),
    [
        (OutlookRefusalReason.INTENT_EXPIRED, SendFailureReason.LOCAL_WORKER_REFUSED, True),
        (OutlookRefusalReason.MAILBOX_UNAVAILABLE, SendFailureReason.LOCAL_WORKER_REFUSED, True),
        (OutlookRefusalReason.OUTLOOK_NOT_CLASSIC, SendFailureReason.LOCAL_WORKER_REFUSED, True),
        (OutlookRefusalReason.KILL_SWITCH, SendFailureReason.KILL_SWITCH_ACTIVE, True),
        (OutlookRefusalReason.ACCOUNT_MISMATCH, SendFailureReason.ACCOUNT_MISMATCH, False),
        (OutlookRefusalReason.BINDING_MISMATCH, SendFailureReason.LOCAL_WORKER_REFUSED, False),
        (OutlookRefusalReason.INTENT_INVALID, SendFailureReason.LOCAL_WORKER_REFUSED, False),
    ],
)
def test_outlook_worker_refusals_are_pre_submission(
    reason: OutlookRefusalReason, expected: SendFailureReason, retryable: bool
) -> None:
    intent = intent_for()
    outcome = map_outlook_report(
        intent,
        report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal_reason=reason),
        observed_at=WHEN,
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.pre_submission and outcome.reason == expected and outcome.retryable is retryable


def test_outlook_ambiguous_reports_are_uncertain_or_definite_after_hand_over() -> None:
    intent = intent_for()
    duplicate = map_outlook_report(
        intent,
        report(
            intent,
            OutlookSubmissionState.REFUSED_BEFORE_SEND,
            refusal_reason=OutlookRefusalReason.DUPLICATE_INTENT,
        ),
        observed_at=WHEN,
    )
    assert isinstance(duplicate, SendUncertain) and duplicate.reason == UncertainReason.LOCAL_WORKER_NO_RESULT
    failed_call = map_outlook_report(
        intent, report(intent, OutlookSubmissionState.SEND_CALL_FAILED), observed_at=WHEN
    )
    assert (
        isinstance(failed_call, SendUncertain)
        and failed_call.reason == UncertainReason.LOCAL_SEND_CALL_FAILED
    )
    wrong_account = map_outlook_report(
        intent,
        report(
            intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX, account_smtp_address_used="other@example.com"
        ),
        observed_at=WHEN,
    )
    assert isinstance(wrong_account, SendUncertain)
    assert wrong_account.reason == UncertainReason.LOCAL_ACCOUNT_MISMATCH_REPORTED
    rejected = map_outlook_report(
        intent,
        report(intent, OutlookSubmissionState.TRANSPORT_REJECTED, error_code="550 5.7.1"),
        observed_at=WHEN,
    )
    assert isinstance(rejected, SendDefiniteFailure)
    assert not rejected.pre_submission and not rejected.retryable
    assert attempt_outcome(rejected).outcome == SendAttemptOutcome.DEFINITE_REJECTION
    assert rejected.provider_error == safe_token("550 5.7.1")


def test_outlook_report_validation() -> None:
    intent = intent_for()
    with pytest.raises(ValidationFailed):
        map_outlook_report(
            intent,
            report(intent, OutlookSubmissionState.SEND_CALL_FAILED, intent_id=uuid4()),
            observed_at=WHEN,
        )
    with pytest.raises(ValidationFailed):
        map_outlook_report(
            intent,
            report(intent, OutlookSubmissionState.SEND_CALL_FAILED, mailbox_binding_id=uuid4()),
            observed_at=WHEN,
        )
    with pytest.raises(ValidationError):
        report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED)  # no Sent Items evidence
    with pytest.raises(ValidationError):
        report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND)  # no refusal reason
    with pytest.raises(ValidationError):
        report(intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX, sent_items_present=True)
    with pytest.raises(ValidationError):
        OutlookSendIntent.model_validate({**intent.model_dump(), "not_after": WHEN + timedelta(days=3)})
    with pytest.raises(ValidationError):
        OutlookSendIntent.model_validate(
            {**intent.model_dump(), "rfc_message_id": "<inquiry-x.1@example.com>"}
        )
    with pytest.raises(ValidationError):
        OutlookSendIntent.model_validate({**intent.model_dump(), "cc": [SELLER]})
    with pytest.raises(ValidationFailed):
        build_send_intent(
            message(),
            attempt_id=ATTEMPT_ID,
            idempotency_key=KEY,
            mailbox_binding_id=MAILBOX_ID,
            binding=binding(),
            created_at=WHEN,
        )


def test_outlook_reconciliation_from_reports() -> None:
    intent = intent_for()
    ids = [intent.rfc_message_id]
    pending = reconcile_from_reports(
        INQUIRY_ID, ids, [report(intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX)], worker_online=True
    )
    assert isinstance(pending, ReconcileNotFoundYet) and pending.pending_in_drafts_or_outbox == Tristate.YES
    decision = reconcile_uncertain(reconciliation_evidence(pending))
    assert decision.next_state is None and "OUTBOX_MAY_STILL_SUBMIT" in decision.reasons
    nothing = reconcile_from_reports(INQUIRY_ID, ids, [], worker_online=True)
    assert (
        isinstance(nothing, ReconcileNotFoundYet) and nothing.pending_in_drafts_or_outbox == Tristate.UNKNOWN
    )
    offline = reconcile_from_reports(INQUIRY_ID, ids, [], worker_online=False)
    assert isinstance(offline, ReconcileProviderUnavailable) and offline.reason == "worker_offline"
    confirmed = reconcile_from_reports(
        INQUIRY_ID,
        ids,
        [report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED, sent_items_present=True)],
        worker_online=False,
    )
    assert isinstance(confirmed, ReconcileFoundSent)
    assert reconcile_uncertain(reconciliation_evidence(confirmed)).next_state == InquiryState.ACCEPTED
    other = reconcile_from_reports(
        INQUIRY_ID,
        ids,
        [
            report(
                intent,
                OutlookSubmissionState.SENT_ITEMS_CONFIRMED,
                sent_items_present=True,
                inquiry_id=uuid4(),
            )
        ],
        worker_online=True,
    )
    assert isinstance(other, ReconcileNotFoundYet)


async def test_outlook_provider_reconcile_and_replies() -> None:
    gateway = FakeGateway()
    gateway.heartbeat = heartbeat()
    intent = intent_for()
    gateway.reports = [report(intent, OutlookSubmissionState.SENT_ITEMS_CONFIRMED, sent_items_present=True)]
    result = await outlook(gateway).reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[intent.rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileFoundSent)
    replies = await outlook(gateway).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="c1"
    )
    assert replies.retrieval_mode == "local_worker_push" and replies.next_cursor == "c1"


async def test_outlook_verify_account_from_worker_report() -> None:
    gateway = FakeGateway()
    gateway.heartbeat = heartbeat()
    assert "WORKER_REPORT_MISSING" in (await outlook(gateway).verify_account()).problems
    gateway.account = account_report()
    ok = await outlook(
        gateway, binding_=binding(EmailProviderKind.OUTLOOK_LOCAL, account_id="acct:9f2c1e7a")
    ).verify_account()
    assert ok.verified, ok.problems
    assert ok.stable_account_id == "acct:9f2c1e7a" and ok.from_status == AliasStatus.PRIMARY
    by_address = await outlook(gateway).verify_account()
    assert by_address.verified and "ACCOUNT_ID_IS_ADDRESS" in by_address.warnings
    gateway.account = account_report(outlook_flavour="new")
    assert "NEW_OUTLOOK_UNSUPPORTED" in (await outlook(gateway).verify_account()).problems
    gateway.account = account_report(reported_at=WHEN - timedelta(hours=1))
    assert "WORKER_REPORT_STALE" in (await outlook(gateway).verify_account()).problems
    gateway.account = account_report(account_smtp_address="other@example.com")
    mismatch = await outlook(gateway).verify_account()
    assert "ACCOUNT_MISMATCH" in mismatch.problems and "FROM_NOT_VERIFIED" in mismatch.problems
    # A report claiming weakened security never reaches verification: the wire model refuses it
    # (``security_settings_unchanged`` is ``Literal[True]``; the mail-worker API answers 422).
    with pytest.raises(ValidationError):
        account_report(security_settings_unchanged=False)
    # The provider's own check stays as defence in depth (an unvalidated copy still fails).
    gateway.account = account_report().model_copy(update={"security_settings_unchanged": False})
    assert "SECURITY_SETTINGS_WEAKENED" in (await outlook(gateway).verify_account()).problems
    gateway.account = account_report()
    gateway.heartbeat = heartbeat(age=timedelta(hours=1))
    offline = await outlook(gateway).verify_account()
    assert "WORKER_OFFLINE" in offline.problems and not offline.verified


async def test_outlook_health() -> None:
    gateway = FakeGateway()
    assert (await outlook(gateway).health()).status == ProviderHealthStatus.UNAVAILABLE
    gateway.heartbeat = heartbeat()
    healthy = await outlook(gateway).health()
    assert healthy.status == ProviderHealthStatus.OK and healthy.last_worker_heartbeat_age_seconds == 30
    gateway.heartbeat = heartbeat(running=False)
    stopped = await outlook(gateway).health()
    assert stopped.status == ProviderHealthStatus.UNAVAILABLE and "OUTLOOK_NOT_RUNNING" in stopped.problems
    with pytest.raises(ValidationFailed):
        OutlookLocalProvider(binding=binding(), mailbox_binding_id=MAILBOX_ID, gateway=gateway)
    with pytest.raises(ValidationFailed):
        OutlookLocalProvider(
            binding=binding(EmailProviderKind.OUTLOOK_LOCAL),
            mailbox_binding_id=MAILBOX_ID,
            gateway=gateway,
            intent_ttl=timedelta(days=5),
        )


# ============================================================================================ outcome models


def test_outcome_model_invariants() -> None:
    common: dict[str, Any] = {
        "provider": EmailProviderKind.GMAIL_API,
        "inquiry_id": INQUIRY_ID,
        "attempt_id": ATTEMPT_ID,
        "rfc_message_id": "<a@b.example>",
        "raw_sha256": "0" * 64,
    }
    with pytest.raises(ValidationError):  # pre-submission without proof
        SendDefiniteFailure(
            **common, pre_submission=True, reason=SendFailureReason.CONNECTION_FAILED, retryable=True
        )
    with pytest.raises(ValidationError):  # after hand-over can never be retryable
        SendDefiniteFailure(
            **common, pre_submission=False, reason=SendFailureReason.TRANSPORT_REJECTED, retryable=True
        )
    with pytest.raises(ValidationError):  # ids only from a complete response
        SendAccepted(
            **common,
            provider_message_id="x",
            receipt={"kind": "gmail_message_resource", "response_complete": False, "observed_at": WHEN},
            accepted_at=WHEN,
        )
    not_found = ReconcileNotFoundYet(provider=EmailProviderKind.GMAIL_API, inquiry_id=INQUIRY_ID)
    with pytest.raises(ValidationError):
        ReconcileNotFoundYet.model_validate({**not_found.model_dump(), "proves_non_submission": True})


async def test_http_call_rejects_nothing_silently_and_bounds_bodies(router: respx.MockRouter) -> None:
    router.get("https://gmail.googleapis.com/big").mock(return_value=httpx.Response(200, content=b"x" * 5000))
    async with httpx.AsyncClient() as client:
        result = await http_call(
            client, "GET", "https://gmail.googleapis.com/big", headers={}, timeout_s=5, max_response_bytes=100
        )
    assert result.status_code == 200 and len(result.body) == 100 and not result.body_complete
    assert result.json() is None


# ============================================================================================ registry


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "seller_inquiry_mode": "automatic",
        "seller_email_provider": "gmail_api",
        "seller_email_account_id": ACCOUNT,
        "seller_email_from": ACCOUNT,
        "seller_email_oauth_secret_reference": "secretbox:ops.email_sender_bindings/99999999",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def sender_status(cfg: Settings, **overrides: Any) -> SenderStatus:
    values: dict[str, Any] = {
        "binding_id": BINDING_ID,
        "binding_version": 1,
        "display_name": SENDER_NAME,
        "alias_verified": True,
        "verified_at": WHEN,
        "health_ok": True,
    }
    values.update(overrides)
    return SenderStatus.from_settings(cfg, **values)


async def kill_switch_off() -> bool:
    return False


def deps(http: httpx.AsyncClient, **overrides: Any) -> seller_email.ProviderDependencies:
    values: dict[str, Any] = {
        "token_provider": FakeTokens(),
        "http_client": http,
        "clock": FrozenClock(WHEN),
        "kill_switch_probe": kill_switch_off,
    }
    values.update(overrides)
    return seller_email.ProviderDependencies(**values)


async def test_registry_builds_gated_provider_when_all_prerequisites_hold(http: httpx.AsyncClient) -> None:
    cfg = settings()
    provider = seller_email.build_sender_provider(cfg, sender=sender_status(cfg), deps=deps(http))
    assert provider.kind == EmailProviderKind.GMAIL_API
    assert provider.binding.account_id == ACCOUNT
    assert isinstance(provider, SenderProvider)
    assert requires_message_approval(cfg) is False


@pytest.mark.parametrize(
    ("overrides", "status_overrides", "problem"),
    [
        ({"seller_inquiry_mode": "disabled_until_sender_ready"}, {}, "SELLER_INQUIRY_MODE_NOT_AUTOMATIC"),
        ({"seller_inquiry_mode": "paused"}, {}, "SELLER_INQUIRY_MODE_NOT_AUTOMATIC"),
        ({"seller_inquiry_kill_switch": True}, {}, "KILL_SWITCH_ACTIVE"),
        ({"seller_inquiry_require_message_approval": True}, {}, "OWNER_REQUIRES_MESSAGE_APPROVAL"),
        ({}, {"verified_at": None}, "SENDER_NOT_VERIFIED"),
        ({}, {"alias_verified": False}, "SENDER_ALIAS_NOT_VERIFIED"),
        ({}, {"health_ok": False}, "SENDER_UNHEALTHY"),
        ({}, {"credentials_revoked": True}, "SENDER_REVOKED"),
        ({}, {"display_name": None}, "SENDER_DISPLAY_NAME_MISSING"),
        ({"seller_email_oauth_secret_reference": None}, {}, "SECRET_REFERENCE_MISSING"),
        (
            {"seller_email_oauth_secret_reference": "ya29.a0AfH6SMBx"},
            {},
            "SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL",
        ),
        (
            {"seller_email_oauth_secret_reference": "1//0gLx-refresh"},
            {},
            "SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL",
        ),
        (
            {"seller_email_oauth_secret_reference": "eyJhbGciOi.eyJzdWIi.sig"},
            {},
            "SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL",
        ),
        ({"seller_email_oauth_secret_reference": "just text"}, {}, "SECRET_REFERENCE_INVALID"),
        ({"seller_email_provider": ""}, {}, "PROVIDER_NOT_CONFIGURED"),
        ({"seller_email_from": "not-an-address"}, {}, "FROM_NOT_CONFIGURED"),
        ({"seller_email_reply_to": "bad reply"}, {}, "REPLY_TO_INVALID"),
    ],
)
def test_registry_refuses_without_technical_prerequisites(
    http: httpx.AsyncClient, overrides: dict[str, Any], status_overrides: dict[str, Any], problem: str
) -> None:
    cfg = settings(**overrides)
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(
            cfg, sender=sender_status(cfg, **status_overrides), deps=deps(http)
        )
    assert problem in exc.value.problems


def test_registry_refuses_binding_that_is_not_the_configured_one(http: httpx.AsyncClient) -> None:
    cfg = settings()
    other = sender_status(
        settings(
            seller_email_from="other.sender@example.com", seller_email_account_id="other.sender@example.com"
        )
    )
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(cfg, sender=other, deps=deps(http))
    assert "SENDER_BINDING_MISMATCH" in exc.value.problems
    killed = sender_status(cfg).model_copy(update={"kill_switch": True})
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(cfg, sender=killed, deps=deps(http))
    assert "KILL_SWITCH_ACTIVE" in exc.value.problems


def test_registry_requires_dependencies(http: httpx.AsyncClient) -> None:
    cfg = settings()
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(
            cfg, sender=sender_status(cfg), deps=seller_email.ProviderDependencies()
        )
    assert {"TOKEN_PROVIDER_MISSING", "HTTP_CLIENT_MISSING", "KILL_SWITCH_PROBE_MISSING"} <= set(
        exc.value.problems
    )
    local = settings(seller_email_provider="outlook_local", seller_email_oauth_secret_reference=None)
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(
            local, sender=sender_status(local), deps=seller_email.ProviderDependencies()
        )
    assert {"OUTLOOK_GATEWAY_MISSING", "MAILBOX_BINDING_MISSING"} <= set(exc.value.problems)
    built = seller_email.build_sender_provider(
        local,
        sender=sender_status(local),
        deps=seller_email.ProviderDependencies(
            outlook_gateway=FakeGateway(), mailbox_binding_id=MAILBOX_ID, kill_switch_probe=kill_switch_off
        ),
    )
    assert built.kind == EmailProviderKind.OUTLOOK_LOCAL
    graph_cfg = settings(seller_email_provider="microsoft_graph", seller_email_account_id=GRAPH_ID)
    graph_provider = seller_email.build_sender_provider(
        graph_cfg, sender=sender_status(graph_cfg), deps=deps(http)
    )
    assert graph_provider.kind == EmailProviderKind.MICROSOFT_GRAPH


class RecordingProvider:
    """A fake inner provider: records sends; it has no approval concept at all."""

    def __init__(self) -> None:
        self.sent: list[UUID] = []

    @property
    def kind(self) -> EmailProviderKind:
        return EmailProviderKind.GMAIL_API

    @property
    def binding(self) -> SenderBinding:
        return binding()

    @property
    def capabilities(self) -> Any:
        return None

    async def verify_account(self) -> Any:
        return None

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted:
        self.sent.append(attempt_id)
        return SendAccepted(
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=inquiry_id,
            attempt_id=attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            receipt={"kind": "gmail_message_resource", "observed_at": WHEN},  # type: ignore[arg-type]
            accepted_at=WHEN,
        )

    async def reconcile(self, **kwargs: Any) -> Any:
        return "reconciled"

    async def fetch_correlated_replies(self, **kwargs: Any) -> Any:
        return "replies"

    async def health(self) -> Any:
        return "healthy"


async def test_send_never_waits_for_approval_when_not_required() -> None:
    cfg = settings()
    assert cfg.seller_inquiry_require_message_approval is False
    inner = RecordingProvider()
    gated = seller_email.GatedSenderProvider(inner, kill_switch_probe=None)  # type: ignore[arg-type]
    with anyio.fail_after(1):  # nothing to wait for: no approval queue, no human in the loop
        outcome = await gated.send(
            message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
        )
    assert isinstance(outcome, SendAccepted)
    assert inner.sent == [ATTEMPT_ID]
    assert not [name for name in dir(gated) if "approv" in name.lower()]
    assert not [name for name in dir(seller_email) if "approv" in name.lower()]
    assert await gated.reconcile(inquiry_id=INQUIRY_ID, rfc_message_ids=[], window=WINDOW) == "reconciled"
    assert (
        await gated.fetch_correlated_replies(mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN)
        == "replies"
    )
    assert await gated.health() == "healthy"


async def test_live_kill_switch_stops_untransmitted_work() -> None:
    inner = RecordingProvider()

    async def active() -> bool:
        return True

    async def broken() -> bool:
        raise RuntimeError("config store down")

    for probe in (active, broken):
        gated = seller_email.GatedSenderProvider(inner, kill_switch_probe=probe)  # type: ignore[arg-type]
        outcome = await gated.send(
            message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
        )
        assert isinstance(outcome, SendDefiniteFailure)
        assert outcome.reason == SendFailureReason.KILL_SWITCH_ACTIVE
        # Proven pre-submission and retryable: the pause stops this transmission only and never
        # fails the inquiry permanently (the preflight suppresses while the switch is on).
        assert outcome.pre_submission and outcome.retryable
        assert attempt_outcome(outcome).outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE
    assert inner.sent == []

    async def inactive() -> bool:
        return False

    gated = seller_email.GatedSenderProvider(inner, kill_switch_probe=inactive)  # type: ignore[arg-type]
    assert isinstance(
        await gated.send(message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY),
        SendAccepted,
    )


async def test_account_verifier_works_before_automatic_mode_and_cannot_send(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    cfg = settings(seller_inquiry_mode="disabled_until_sender_ready")
    candidate = seller_email.candidate_binding_from_settings(
        cfg, binding_id=BINDING_ID, binding_version=1, display_name=SENDER_NAME
    )
    verifier = seller_email.build_account_verifier(cfg, binding=candidate, deps=deps(http))
    assert not hasattr(verifier, "send")
    profile_ok(router)
    router.get(f"{GMAIL_ROOT}/settings/sendAs").mock(return_value=send_as(PRIMARY))
    verification = await verifier.verify_account()
    assert verification.verified
    status = seller_email.sender_status_from_verification(cfg, verification, binding=candidate)
    assert status.identity_problems() == ()
    assert status.mode == "disabled_until_sender_ready"  # verification never enables sending
    with pytest.raises(seller_email.SenderSetupError):
        seller_email.build_sender_provider(cfg, sender=status, deps=deps(http))
    automatic = settings()
    assert seller_email.build_sender_provider(
        automatic,
        sender=seller_email.sender_status_from_verification(automatic, verification, binding=candidate),
        deps=deps(http),
    )
    assert (await verifier.health()).status == ProviderHealthStatus.OK
    assert (
        verifier.kind == EmailProviderKind.GMAIL_API
        and verifier.capabilities.kind == EmailProviderKind.GMAIL_API
    )


def test_account_verifier_and_candidate_binding_validation(http: httpx.AsyncClient) -> None:
    cfg = settings()
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.candidate_binding_from_settings(
            settings(seller_email_provider="", seller_email_from=None),
            binding_id=BINDING_ID,
            binding_version=1,
            display_name=" ",
        )
    assert {"PROVIDER_NOT_CONFIGURED", "FROM_NOT_CONFIGURED", "SENDER_DISPLAY_NAME_MISSING"} <= set(
        exc.value.problems
    )
    wrong = binding(from_address="other.sender@example.com")
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_account_verifier(cfg, binding=wrong, deps=deps(http))
    assert "FROM_MISMATCH" in exc.value.problems
    failed = SenderStatus.from_settings(
        cfg,
        binding_id=BINDING_ID,
        binding_version=1,
        display_name=SENDER_NAME,
        alias_verified=False,
        verified_at=None,
        health_ok=False,
    )
    with pytest.raises(seller_email.SenderSetupError):
        seller_email.sender_binding_from_status(failed)


def test_secret_reference_rules() -> None:
    assert seller_email.secret_reference_problems("secretbox:ops.email_sender_bindings/99999999") == []
    assert seller_email.secret_reference_problems("vault:mail/gmail-sender") == []
    assert seller_email.secret_reference_problems("") == ["SECRET_REFERENCE_MISSING"]
    assert seller_email.secret_reference_problems("Bearer abc") == ["SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL"]
    assert seller_email.secret_reference_problems("x" * 130) == ["SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL"]


def test_default_http_client_is_safe() -> None:
    client = seller_email.default_http_client()
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
    finally:
        anyio.run(client.aclose)


def test_attempt_outcome_mapping_for_uncertain_and_accepted_outcomes() -> None:
    uncertain = SendUncertain(
        provider=EmailProviderKind.GMAIL_API,
        inquiry_id=INQUIRY_ID,
        attempt_id=ATTEMPT_ID,
        rfc_message_id="<a@b.example>",
        raw_sha256="0" * 64,
        reason=UncertainReason.READ_TIMEOUT,
    )
    mapping = attempt_outcome(uncertain)
    assert mapping.outcome == SendAttemptOutcome.UNCERTAIN and mapping.requires_reconciliation
    retry = should_retry(attempt_evidence(uncertain), now=WHEN, retry_sender_binding_id=uuid4())
    assert retry.retry is False and "DIFFERENT_ACCOUNT_FORBIDDEN" in retry.reasons


# ============================================================================================ regressions


async def test_graph_reply_pull_never_moves_cursor_past_an_unreadable_candidate(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    graph_binding = reply_binding(outbound).model_copy(
        update={"provider": EmailProviderKind.MICROSOFT_GRAPH, "provider_thread_ids": ()}
    )
    listing = router.get(f"{GRAPH_ME}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "p0",
                        "receivedDateTime": "2026-10-06T10:30:00Z",
                        "internetMessageHeaders": [{"name": "From", "value": "friend@private.example"}],
                    },
                    {
                        "id": "r1",
                        "receivedDateTime": "2026-10-06T11:00:00Z",
                        "internetMessageHeaders": [
                            {"name": "From", "value": SELLER},
                            {"name": "In-Reply-To", "value": outbound},
                        ],
                    },
                    {
                        "id": "p2",
                        "receivedDateTime": "2026-10-06T11:30:00Z",
                        "internetMessageHeaders": [{"name": "From", "value": "friend@private.example"}],
                    },
                ]
            },
        )
    )
    router.get(f"{GRAPH_ME}/messages/r1").mock(return_value=httpx.Response(503))
    provider = graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.Read")),
        settings=GraphSettings(reply_retrieval_enabled=True),
    )
    result = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[graph_binding], since=WHEN, cursor="2026-10-06T10:00:00Z"
    )
    assert result.complete is False and "MESSAGE_FETCH_FAILED" in result.problems
    assert result.next_cursor == "2026-10-06T10:30:00Z"  # not past the unreadable reply r1
    assert result.replies == ()
    query = listing.calls.last.request.url.params["$filter"]
    assert query == "receivedDateTime ge 2026-10-06T09:55:00Z"  # checkpoint minus overlap


async def test_gmail_precheck_garbage_profile_is_retryable_and_never_sends(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=httpx.Response(200, content=b"<html>"))
    send = router.post(f"{GMAIL_ROOT}/messages/send").mock(return_value=httpx.Response(200, json={"id": "x"}))
    outcome = await gmail(http).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.reason == SendFailureReason.PRECHECK_FAILED and outcome.retryable
    assert not send.called


def test_registry_reports_unusable_account_id_as_setup_problem(http: httpx.AsyncClient) -> None:
    cfg = settings(seller_email_account_id="not-an-email-address")
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.build_sender_provider(cfg, sender=sender_status(cfg), deps=deps(http))
    assert exc.value.problems == ("ACCOUNT_ID_INVALID",)


def test_opaque_ids_reject_path_segments() -> None:
    assert safe_opaque_id("18f0a1b2c3") == "18f0a1b2c3"
    assert safe_opaque_id("AAMkAGI2T+/x==") == "AAMkAGI2T+/x=="
    for bad in ("../x", "..", ".hidden", "/abs", "a b", "", None, 5, "x" * 513):
        assert safe_opaque_id(bad) is None


async def test_gmail_oversized_reply_is_correlated_on_headers_instead_of_blocking_the_pull(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200, json={"history": [{"messagesAdded": [{"message": {"id": "m-reply"}}]}], "historyId": "1001"}
        )
    )
    routes = mock_reply_messages(router, outbound)
    routes["reply_full"].mock(
        return_value=httpx.Response(200, content=b'{"id": "m-reply", "payload": ' + b"x" * 70_000)
    )
    provider = gmail(
        http, settings=GmailApiSettings(max_full_message_bytes=64 * 1024, reply_retrieval_enabled=True)
    )
    result = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(outbound)], since=WHEN, cursor="1000"
    )
    assert result.complete and result.next_cursor == "1001"
    assert len(result.replies) == 1
    reply = result.replies[0]
    assert reply.body_truncated and reply.message.body_text == ""
    assert reply.correlation.outcome == CorrelationOutcome.MATCHED


async def test_gmail_recheck_locators_after_binding_sync(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    late = message(attempt=2).rfc_message_id  # outbound Message-ID not yet in the synced bindings
    headers = gmail_headers(
        From=SELLER,
        Subject="Re: Enquiry about Toyota RAV4 - ABC-123",
        Message_ID="<r9@x.example>",
        In_Reply_To=late,
    )
    router.get(f"{GMAIL_ROOT}/messages/m-race", params={"format": "metadata"}).mock(
        return_value=httpx.Response(200, json={"id": "m-race", "payload": {"headers": headers}})
    )
    router.get(f"{GMAIL_ROOT}/messages/m-race", params={"format": "full"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m-race",
                "payload": {
                    "headers": headers,
                    "mimeType": "text/plain",
                    "body": {"data": b64url("Still available.")},
                },
            },
        )
    )
    stale = reply_binding(message().rfc_message_id).model_copy(update={"verified_seller_aliases": ()})
    before = await gmail(http).recheck_locators(
        mailbox_binding_id=MAILBOX_ID, bindings=[stale], locators=["m-race", "../x"]
    )
    assert before.pending_retry_locators == ("m-race",) and before.replies == ()
    synced = reply_binding(late).model_copy(update={"binding_version": 2})
    after = await gmail(http).recheck_locators(
        mailbox_binding_id=MAILBOX_ID, bindings=[stale, synced], locators=["m-race"]
    )
    assert after.pending_retry_locators == ()
    assert len(after.replies) == 1 and after.replies[0].correlation.outcome == CorrelationOutcome.MATCHED
    revoked = await gmail(http, FakeTokens(fail=TokenUnavailable(revoked=True))).recheck_locators(
        mailbox_binding_id=MAILBOX_ID, bindings=[synced], locators=["m-race"]
    )
    assert revoked.pending_retry_locators == ("m-race",) and not revoked.complete


async def test_graph_oversized_candidate_is_correlated_on_listed_headers(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    graph_binding = reply_binding(outbound).model_copy(
        update={"provider": EmailProviderKind.MICROSOFT_GRAPH, "provider_thread_ids": ()}
    )
    router.get(f"{GRAPH_ME}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "r1",
                        "internetMessageId": "<reply-1@autohaus-example.de>",
                        "receivedDateTime": "2026-10-06T11:00:00Z",
                        "internetMessageHeaders": [
                            {"name": "From", "value": SELLER},
                            {"name": "In-Reply-To", "value": outbound},
                        ],
                    }
                ]
            },
        )
    )
    router.get(f"{GRAPH_ME}/messages/r1").mock(return_value=httpx.Response(200, content=b"{" + b"x" * 70_000))
    provider = graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.Read")),
        settings=GraphSettings(reply_retrieval_enabled=True, max_full_message_bytes=64 * 1024),
    )
    result = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[graph_binding], since=WHEN
    )
    assert result.complete and len(result.replies) == 1
    assert result.replies[0].body_truncated
    assert result.replies[0].correlation.outcome == CorrelationOutcome.MATCHED


# ============================================================================== review regressions


def free_text_message() -> BuiltMessage:
    """Well-formed MIME that is NOT a template rendering (an offer the scope forbids)."""
    return build_message(
        OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(ACCOUNT, SENDER_NAME),
            recipient=mailbox(SELLER),
            subject="Offer for your Toyota RAV4",
            body=f"I will pay 3000 EUR cash today.\n\n{SENDER_NAME}",
            date=WHEN,
        )
    )


async def test_every_provider_refuses_messages_outside_the_template_scope_without_io(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    hostile = free_text_message()
    tokens = FakeTokens(scopes=(SCOPE_GMAIL_SEND, SCOPE_GMAIL_READONLY, "Mail.Send", "User.Read"))
    gateway = FakeGateway()
    providers: list[SenderProvider] = [
        gmail(http, tokens),
        graph(http, tokens),
        outlook(gateway),
    ]
    for provider in providers:
        outcome = await provider.send(
            hostile, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
        )
        assert isinstance(outcome, SendDefiniteFailure), provider
        assert outcome.pre_submission and not outcome.retryable
        assert "SCOPE:NOT_A_TEMPLATE_INQUIRY" in outcome.problems
    assert router.calls.call_count == 0 and tokens.calls == 0 and gateway.intents == []
    swapped = message().model_copy(update={"subject": "Offer", "body": "I will pay 3000 EUR.\n"})
    outcome = await gmail(http, tokens).send(
        swapped, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure) and "SCOPE:BODY_HASH_MISMATCH" in outcome.problems
    assert router.calls.call_count == 0


async def test_kill_switch_refusal_never_fails_the_inquiry_permanently() -> None:
    async def active() -> bool:
        return True

    inner = RecordingProvider()
    gated = seller_email.GatedSenderProvider(inner, kill_switch_probe=active)  # type: ignore[arg-type]
    refused = await gated.send(message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY)
    assert inner.sent == []
    decision = should_retry(attempt_evidence(refused), now=WHEN)
    assert decision.retry and decision.reasons == ("PROVEN_PRE_SUBMISSION_FAILURE",)
    assert decision.sender_binding_id == BINDING_ID  # only through the same account
    intent = intent_for()
    worker = map_outlook_report(
        intent,
        report(
            intent,
            OutlookSubmissionState.REFUSED_BEFORE_SEND,
            refusal_reason=OutlookRefusalReason.KILL_SWITCH,
        ),
        observed_at=WHEN,
    )
    assert should_retry(attempt_evidence(worker), now=WHEN).retry


# ---------------------------------------------------------------------------- Gmail pull progress


def gmail_mailbox_handler(
    total: int, *, failing: frozenset[str] = frozenset(), page_size: int = 500
) -> tuple[Any, list[str]]:
    """A synthetic Gmail mailbox: message ``m<i>`` is added by history record ``1001 + i``."""
    inspected: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            start = int(request.url.params["startHistoryId"])
            offset = int(request.url.params.get("pageToken", "0"))
            pending = [i for i in range(total) if 1001 + i > start]
            page = pending[offset : offset + page_size]
            body: dict[str, Any] = {
                "history": [
                    {
                        "id": str(1001 + i),
                        "messagesAdded": [{"message": {"id": f"m{i}", "labelIds": ["INBOX"]}}],
                    }
                    for i in page
                ],
                "historyId": str(1000 + total),
            }
            if offset + page_size < len(pending):
                body["nextPageToken"] = str(offset + page_size)
            return httpx.Response(200, json=body)
        if "/messages/" in path:
            mid = path.rsplit("/", 1)[1]
            if mid in failing:
                return httpx.Response(503)
            inspected.append(mid)
            return httpx.Response(
                200,
                json={
                    "id": mid,
                    "labelIds": ["INBOX"],
                    "payload": {"headers": [{"name": "From", "value": "news@shop.example"}]},
                },
            )
        return httpx.Response(404)

    return handler, inspected


async def test_gmail_history_backlog_beyond_the_message_budget_makes_progress() -> None:
    handler, inspected = gmail_mailbox_handler(300)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = gmail(client)
        first = await provider.fetch_correlated_replies(
            mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
        )
        assert not first.complete and first.scanned == 200
        assert first.next_cursor == "1200"  # the last fully processed history record
        second = await provider.fetch_correlated_replies(
            mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor=first.next_cursor
        )
    assert second.complete and second.scanned == 100 and second.next_cursor == "1300"
    assert inspected == [f"m{i}" for i in range(300)]  # every message exactly once, none skipped


async def test_gmail_history_transient_failure_checkpoints_before_the_failed_record() -> None:
    handler, inspected = gmail_mailbox_handler(10, failing=frozenset({"m5"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gmail(client).fetch_correlated_replies(
            mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
        )
    assert not result.complete and result.next_cursor == "1005"  # m0..m4 done, m5 is re-read
    assert inspected == [f"m{i}" for i in range(5)]


async def test_gmail_history_beyond_the_page_budget_continues_from_the_last_record() -> None:
    handler, inspected = gmail_mailbox_handler(30, page_size=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = gmail(client, settings=GmailApiSettings(max_history_pages=1, reply_retrieval_enabled=True))
        cursor: str | None = "1000"
        cursors = []
        for _ in range(3):
            result = await provider.fetch_correlated_replies(
                mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor=cursor
            )
            cursor = result.next_cursor
            cursors.append((cursor, result.complete))
    assert cursors == [("1010", False), ("1020", False), ("1030", True)]
    assert inspected == [f"m{i}" for i in range(30)]


async def test_gmail_window_pull_hands_back_uninspected_messages_and_moves_to_history(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=httpx.Response(200, json={"historyId": "5000"}))
    router.get(f"{GMAIL_ROOT}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": f"w{i}"} for i in range(30)]})
    )
    meta = router.get(url__regex=rf"{GMAIL_ROOT}/messages/w\d+").mock(
        return_value=httpx.Response(
            200, json={"payload": {"headers": [{"name": "From", "value": "a@b.example"}]}}
        )
    )
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, max_messages=10
    )
    assert result.scanned == 10 and meta.call_count == 10
    assert result.pending_retry_locators == tuple(f"w{i}" for i in range(10, 30))
    assert result.next_cursor == "5000" and not result.complete and not result.gap_detected
    rechecked = await gmail(http).recheck_locators(
        mailbox_binding_id=MAILBOX_ID, bindings=[], locators=result.pending_retry_locators
    )
    assert rechecked.complete and rechecked.skipped_unrelated == 20 and rechecked.pending_retry_locators == ()


async def test_gmail_window_listing_beyond_the_page_budget_is_an_explicit_gap(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.get(f"{GMAIL_ROOT}/profile").mock(return_value=httpx.Response(200, json={"historyId": "5000"}))
    router.get(f"{GMAIL_ROOT}/messages").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "messages": [{"id": f"p{request.url.params.get('pageToken', '0')}x{i}"} for i in range(2)],
                "nextPageToken": str(int(request.url.params.get("pageToken", "0")) + 1),
            },
        )
    )
    router.get(url__regex=rf"{GMAIL_ROOT}/messages/p\d+x\d+").mock(
        return_value=httpx.Response(200, json={"payload": {"headers": []}})
    )
    provider = gmail(http, settings=GmailApiSettings(max_list_pages=2, reply_retrieval_enabled=True))
    result = await provider.fetch_correlated_replies(mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN)
    assert result.gap_detected and not result.complete and result.problems == ("WINDOW_TRUNCATED",)
    assert result.next_cursor == "5000" and result.scanned == 4


async def test_gmail_recheck_keeps_locators_beyond_its_budget() -> None:
    handler, inspected = gmail_mailbox_handler(0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gmail(client).recheck_locators(
            mailbox_binding_id=MAILBOX_ID, bindings=[], locators=[f"x{i}" for i in range(520)]
        )
    assert len(inspected) == 500 and not result.complete
    assert result.pending_retry_locators == tuple(f"x{i}" for i in range(500, 520))


async def test_gmail_naive_since_is_refused(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    with pytest.raises(ValueError, match="naive"):
        await gmail(http).fetch_correlated_replies(
            mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN.replace(tzinfo=None)
        )
    assert router.calls.call_count == 0


async def test_gmail_message_from_the_owner_never_leaves_the_mailbox(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    outbound = message().rfc_message_id
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "history": [{"id": "1001", "messagesAdded": [{"message": {"id": "m-own"}}]}],
                "historyId": "1001",
            },
        )
    )
    headers = gmail_headers(From=f"{SENDER_NAME} <{ACCOUNT}>", In_Reply_To=outbound, Subject="Re: Enquiry")
    router.get(f"{GMAIL_ROOT}/messages/m-own", params={"format": "metadata"}).mock(
        return_value=httpx.Response(
            200, json={"id": "m-own", "labelIds": ["INBOX"], "payload": {"headers": headers}}
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/m-own", params={"format": "full"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m-own",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": headers,
                    "mimeType": "text/plain",
                    "body": {"data": b64url("private note")},
                },
            },
        )
    )
    result = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(outbound)], since=WHEN, cursor="1000"
    )
    assert result.replies == () and result.skipped_unrelated == 1 and result.complete


# ---------------------------------------------------------------------------- reply retrieval gate


async def test_gmail_reply_retrieval_is_off_unless_provider_api_ingest_is_selected(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    tokens = FakeTokens()
    default = GmailApiProvider(binding=binding(), token_provider=tokens, http=http, clock=FrozenClock(WHEN))
    pulled = await default.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
    )
    rechecked = await default.recheck_locators(mailbox_binding_id=MAILBOX_ID, bindings=[], locators=["m1"])
    assert pulled.problems == ("REPLY_RETRIEVAL_DISABLED",) and pulled.next_cursor == "1000"
    assert rechecked.problems == ("REPLY_RETRIEVAL_DISABLED",) and rechecked.pending_retry_locators == ("m1",)
    assert router.calls.call_count == 0 and tokens.calls == 0


@pytest.mark.parametrize(
    ("provider_kind", "account", "probe_url"),
    [
        ("gmail_api", ACCOUNT, f"{GMAIL_ROOT}/history"),
        ("microsoft_graph", GRAPH_ID, f"{GRAPH_ME}/mailFolders/inbox/messages"),
    ],
)
async def test_registry_enables_api_reply_retrieval_only_for_provider_api_ingest(
    http: httpx.AsyncClient, router: respx.MockRouter, provider_kind: str, account: str, probe_url: str
) -> None:
    route = router.get(probe_url).mock(return_value=httpx.Response(503))
    explicit = deps(
        http,
        gmail_settings=GmailApiSettings(reply_retrieval_enabled=True),
        graph_settings=GraphSettings(reply_retrieval_enabled=True),
        token_provider=FakeTokens(scopes=(SCOPE_GMAIL_SEND, SCOPE_GMAIL_READONLY, "Mail.Send", "User.Read")),
    )
    for mode, enabled in (("local_classic_outlook", False), ("disabled", False), ("provider_api", True)):
        cfg = settings(
            seller_email_provider=provider_kind,
            seller_email_account_id=account,
            seller_reply_ingest_mode=mode,
        )
        for dependencies in (deps(http), explicit):
            provider = seller_email.build_sender_provider(cfg, sender=sender_status(cfg), deps=dependencies)
            before = route.call_count
            result = await provider.fetch_correlated_replies(
                mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN, cursor="1000"
            )
            assert (route.call_count > before) is enabled, (mode, dependencies)
            assert ("REPLY_RETRIEVAL_DISABLED" in result.problems) is not enabled
    off = deps(http, gmail_settings=GmailApiSettings(), graph_settings=GraphSettings())
    cfg = settings(
        seller_email_provider=provider_kind,
        seller_email_account_id=account,
        seller_reply_ingest_mode="provider_api",
    )
    provider = seller_email.build_sender_provider(cfg, sender=sender_status(cfg), deps=off)
    result = await provider.fetch_correlated_replies(mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN)
    assert result.problems == ("REPLY_RETRIEVAL_DISABLED",)  # an explicit "off" is never overridden


# ---------------------------------------------------------------------------- Graph window


async def test_graph_reply_window_is_always_expressed_in_utc(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    listing = router.get(f"{GRAPH_ME}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    provider = graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.Read")),
        settings=GraphSettings(reply_retrieval_enabled=True),
    )
    skopje = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    result = await provider.fetch_correlated_replies(mailbox_binding_id=MAILBOX_ID, bindings=[], since=skopje)
    assert listing.calls.last.request.url.params["$filter"] == "receivedDateTime ge 2026-10-06T10:00:00Z"
    assert result.complete and result.next_cursor is None
    garbage = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[], since=skopje, cursor="not-a-timestamp"
    )
    assert listing.calls.last.request.url.params["$filter"] == "receivedDateTime ge 2026-10-06T10:00:00Z"
    assert garbage.next_cursor is None  # an unusable cursor is dropped, never echoed forever
    with pytest.raises(ValueError, match="naive"):
        await provider.fetch_correlated_replies(
            mailbox_binding_id=MAILBOX_ID, bindings=[], since=WHEN.replace(tzinfo=None)
        )


# ---------------------------------------------------------------------------- Outlook proof of non-submission


def refused(intent: OutlookSendIntent, reason: OutlookRefusalReason, **overrides: Any) -> OutlookSendReport:
    return report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal_reason=reason, **overrides)


def second_intent() -> OutlookSendIntent:
    return build_send_intent(
        message(attempt=2),
        attempt_id=uuid4(),
        idempotency_key="inquiry-66666666:attempt-2",
        mailbox_binding_id=MAILBOX_ID,
        binding=binding(EmailProviderKind.OUTLOOK_LOCAL),
        created_at=WHEN,
    )


def test_outlook_worker_refusal_of_every_intent_resolves_the_uncertain_inquiry() -> None:
    intent = intent_for()
    expired = refused(intent, OutlookRefusalReason.INTENT_EXPIRED)
    result = reconcile_from_reports(
        INQUIRY_ID, [intent.rfc_message_id], [expired], worker_online=False, intents=[intent]
    )
    assert isinstance(result, ReconcileProvenNotSubmitted)
    assert result.refused_intent_ids == (intent.intent_id,) and result.refusal_reasons == ("intent_expired",)
    decision = reconcile_uncertain(reconciliation_evidence(result))
    assert decision.next_state == InquiryState.FAILED_DEFINITE and decision.release_reservation is False
    attempt = attempt_evidence(map_outlook_report(intent, expired, observed_at=WHEN))
    assert should_retry(attempt, now=WHEN).retry  # a new intent, same account, after the preflight
    with pytest.raises(ValidationError):  # a direct-API search can never prove non-submission
        ReconcileProvenNotSubmitted(
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=INQUIRY_ID,
            proof="local_validation_failed_before_submit",
            refused_intent_ids=(intent.intent_id,),
        )


@pytest.mark.parametrize(
    "case",
    [
        "no_intents_known",
        "duplicate_intent",
        "second_intent_unreported",
        "searched_id_not_an_intent",
        "refused_by_another_mailbox",
        "contradicting_report",
        "outbox_pending",
    ],
)
def test_outlook_non_submission_is_never_assumed_without_complete_proof(case: str) -> None:
    intent = intent_for()
    ids = [intent.rfc_message_id]
    intents: list[OutlookSendIntent] = [intent]
    reports = [refused(intent, OutlookRefusalReason.MAILBOX_UNAVAILABLE)]
    if case == "no_intents_known":
        intents = []
    elif case == "duplicate_intent":
        reports = [refused(intent, OutlookRefusalReason.DUPLICATE_INTENT)]
    elif case == "second_intent_unreported":
        intents.append(second_intent())
    elif case == "searched_id_not_an_intent":
        ids.append(message(attempt=3).rfc_message_id)
    elif case == "refused_by_another_mailbox":
        reports = [refused(intent, OutlookRefusalReason.MAILBOX_UNAVAILABLE, mailbox_binding_id=uuid4())]
    elif case == "contradicting_report":
        reports.append(report(intent, OutlookSubmissionState.SEND_CALL_FAILED))
    elif case == "outbox_pending":
        reports = [refused(intent, OutlookRefusalReason.MAILBOX_UNAVAILABLE, outbox_pending=Tristate.YES)]
    result = reconcile_from_reports(INQUIRY_ID, ids, reports, worker_online=True, intents=intents)
    assert isinstance(result, ReconcileNotFoundYet), case
    assert reconcile_uncertain(reconciliation_evidence(result)).next_state is None


async def test_outlook_provider_reconcile_proves_refusal_of_its_own_intents() -> None:
    gateway = FakeGateway()
    provider = outlook(gateway)
    built = message()
    handed_over = await provider.send(
        built, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(handed_over, SendUncertain)
    intent = gateway.intents[0]
    gateway.reports = [refused(intent, OutlookRefusalReason.INTENT_EXPIRED)]
    result = await provider.reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(result, ReconcileProvenNotSubmitted)
    gateway.reports.append(report(intent, OutlookSubmissionState.SUBMITTED_TO_OUTBOX))
    gateway.heartbeat = heartbeat()
    pending = await provider.reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert isinstance(pending, ReconcileNotFoundYet) and pending.pending_in_drafts_or_outbox == Tristate.YES


@pytest.mark.parametrize("reason", [r for r in OutlookRefusalReason if r != OutlookRefusalReason.NOT_NOW])
def test_outlook_refusal_after_a_granted_claim_is_uncertain(reason: OutlookRefusalReason) -> None:
    """C1 item 4: the worker calls .Send only after a granted claim, so a refusal reported after
    one (e.g. by a stolen worker credential) is never a proven pre-submission failure."""
    intent = intent_for()
    outcome = map_outlook_report(intent, refused(intent, reason), observed_at=WHEN, claim_granted=True)
    assert isinstance(outcome, SendUncertain)
    assert outcome.reason == UncertainReason.LOCAL_WORKER_NO_RESULT
    assert outcome.provider_error == REFUSED_AFTER_GRANTED_CLAIM
    assert attempt_outcome(outcome).outcome == SendAttemptOutcome.UNCERTAIN


def test_outlook_claimed_intent_is_never_proven_unsent() -> None:
    intent = intent_for()
    expired = refused(intent, OutlookRefusalReason.INTENT_EXPIRED)
    ids = [intent.rfc_message_id]
    online = reconcile_from_reports(
        INQUIRY_ID,
        ids,
        [expired],
        worker_online=True,
        intents=[intent],
        claimed_intent_ids={intent.intent_id},
    )
    assert isinstance(online, ReconcileNotFoundYet)
    assert reconcile_uncertain(reconciliation_evidence(online)).next_state is None
    offline = reconcile_from_reports(
        INQUIRY_ID,
        ids,
        [expired],
        worker_online=False,
        intents=[intent],
        claimed_intent_ids=[intent.intent_id],
    )
    assert not isinstance(offline, ReconcileProvenNotSubmitted)
    # Without a granted claim the same refusal stays proof (a refusal at or before the claim).
    unclaimed = reconcile_from_reports(INQUIRY_ID, ids, [expired], worker_online=False, intents=[intent])
    assert isinstance(unclaimed, ReconcileProvenNotSubmitted)


async def test_outlook_provider_reconcile_respects_granted_claims() -> None:
    gateway = FakeGateway()
    provider = outlook(gateway)
    built = message()
    await provider.send(built, inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY)
    intent = gateway.intents[0]
    gateway.reports = [refused(intent, OutlookRefusalReason.INTENT_EXPIRED)]
    gateway.claimed.add(intent.intent_id)
    result = await provider.reconcile(
        inquiry_id=INQUIRY_ID, rfc_message_ids=[built.rfc_message_id], window=WINDOW
    )
    assert not isinstance(result, ReconcileProvenNotSubmitted)


async def test_outlook_reports_of_another_mailbox_never_vouch_for_this_one() -> None:
    gateway = FakeGateway()
    other = uuid4()
    gateway.account = account_report(mailbox_binding_id=other)
    gateway.heartbeat = heartbeat().model_copy(update={"mailbox_binding_id": other})
    verification = await outlook(gateway).verify_account()
    assert "MAILBOX_BINDING_MISMATCH" in verification.problems and not verification.verified
    assert "WORKER_OFFLINE" in verification.problems
    assert (await outlook(gateway).health()).status == ProviderHealthStatus.UNAVAILABLE


# ---------------------------------------------------------------------------- registry


def test_verification_of_another_binding_never_marks_this_one_verified(http: httpx.AsyncClient) -> None:
    cfg = settings()
    candidate = seller_email.candidate_binding_from_settings(
        cfg, binding_id=BINDING_ID, binding_version=1, display_name=SENDER_NAME
    )
    verification = SenderVerification(
        provider=EmailProviderKind.GMAIL_API,
        checked_at=WHEN,
        configured_account_id="other.sender@example.com",
        configured_from="other.sender@example.com",
        configured_reply_to=None,
        configured_display_name="Someone Else",
        from_status=AliasStatus.PRIMARY,
        capabilities=gmail(http).capabilities,
        health=ProviderHealthStatus.OK,
    )
    assert verification.verified
    with pytest.raises(seller_email.SenderSetupError) as exc:
        seller_email.sender_status_from_verification(cfg, verification, binding=candidate)
    assert {"ACCOUNT_MISMATCH", "FROM_MISMATCH", "DISPLAY_NAME_MISMATCH"} <= set(exc.value.problems)


async def test_outlook_intent_that_cannot_be_built_is_a_local_refusal_never_an_exception() -> None:
    gateway = FakeGateway()
    oversized = binding(EmailProviderKind.OUTLOOK_LOCAL, account_id="a" * 321)  # beyond the wire limit
    outcome = await outlook(gateway, binding_=oversized).send(
        message(), inquiry_id=INQUIRY_ID, attempt_id=ATTEMPT_ID, idempotency_key=KEY
    )
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.pre_submission and outcome.problems == ("INTENT_INVALID",)
    assert gateway.intents == []


async def test_subject_only_match_never_fetches_or_returns_a_body(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    """Spec 37.7/37.10: a matching subject alone never correlates (Gmail and Graph pre-filters)."""
    outbound = message().rfc_message_id
    subject = "Re: Enquiry about Toyota RAV4 \u2013 ABC-123"
    router.get(f"{GMAIL_ROOT}/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "history": [{"id": "1001", "messagesAdded": [{"message": {"id": "m-subj"}}]}],
                "historyId": "1001",
            },
        )
    )
    router.get(f"{GMAIL_ROOT}/messages/m-subj", params={"format": "metadata"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m-subj",
                "threadId": "t-other",
                "labelIds": ["INBOX"],
                "payload": {"headers": gmail_headers(From="stranger@private.example", Subject=subject)},
            },
        )
    )
    gmail_full = router.get(f"{GMAIL_ROOT}/messages/m-subj", params={"format": "full"}).mock(
        return_value=httpx.Response(200, json={})
    )
    pulled = await gmail(http).fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[reply_binding(outbound)], since=WHEN, cursor="1000"
    )
    assert pulled.replies == () and pulled.pending_retry_locators == () and not gmail_full.called
    graph_binding = reply_binding(outbound).model_copy(
        update={"provider": EmailProviderKind.MICROSOFT_GRAPH, "provider_thread_ids": ()}
    )
    router.get(f"{GRAPH_ME}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "g-subj",
                        "receivedDateTime": "2026-10-06T11:00:00Z",
                        "internetMessageHeaders": [
                            {"name": "From", "value": "stranger@private.example"},
                            {"name": "Subject", "value": subject},
                        ],
                    }
                ]
            },
        )
    )
    graph_full = router.get(f"{GRAPH_ME}/messages/g-subj").mock(return_value=httpx.Response(200, json={}))
    provider = graph(
        http,
        FakeTokens(scopes=("Mail.Send", "User.Read", "Mail.Read")),
        settings=GraphSettings(reply_retrieval_enabled=True),
    )
    listed = await provider.fetch_correlated_replies(
        mailbox_binding_id=MAILBOX_ID, bindings=[graph_binding], since=WHEN
    )
    assert listed.replies == () and listed.skipped_unrelated == 1 and not graph_full.called
