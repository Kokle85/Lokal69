"""Contract: the backend mail-worker API models match the desktop worker's wire EXACTLY.

The Windows worker (``desktop/outlook-bridge``) owns the client side of ``/v1/mail-workers``
(``outlook_bridge.wire`` and ``outlook_bridge.api_client``). ``api.schemas`` declares the server
side. This test imports the desktop modules and checks, model by model, that both sides have the
same fields, types, limits, defaults and ``schema_version``; then it drives the REAL desktop
client against an in-memory server built only from the backend models (httpx MockTransport): every
request the worker sends must validate against the backend request model, and every backend
response must parse in the worker. Synthetic ``example.invalid`` data only; nothing is sent.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel

from suv_deals.api.schemas import (
    IDEMPOTENCY_HEADER,
    MAIL_WORKER_BODY_LIMIT,
    MAIL_WORKER_PREFIX,
    MAIL_WORKER_ROUTES,
    MailWorkerAccepted,
    MailWorkerAccountReport,
    MailWorkerBindingItem,
    MailWorkerBindingPage,
    MailWorkerBindingsQuery,
    MailWorkerCheckpointReport,
    MailWorkerClaimDecision,
    MailWorkerClaimRequest,
    MailWorkerGapReport,
    MailWorkerHeartbeatAck,
    MailWorkerHeartbeatRequest,
    MailWorkerReplyAck,
    MailWorkerReplyRequest,
    MailWorkerSendIntent,
    MailWorkerSendIntentBatch,
    MailWorkerSendIntentsQuery,
    MailWorkerSendReport,
)
from suv_deals.domain.enums import Scope
from suv_deals.domain.replies import MAX_REQUEST_BYTES, ReplyIngestRequest
from suv_deals.views.jsonschema import model_schema

DESKTOP = Path(__file__).resolve().parents[2] / "desktop" / "outlook-bridge"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

from outlook_bridge import api_client, wire  # noqa: E402
from outlook_bridge.matching import encode_payload, idempotency_key_for, wire_payload  # noqa: E402
from outlook_bridge.testing import inquiry_message_id, make_intent  # noqa: E402

MAILBOX = UUID("77777777-7777-4777-8777-777777777777")
INQUIRY = UUID("66666666-6666-4666-8666-666666666666")
OWNER = "owner@example.invalid"
SELLER = "seller@dealer.example.invalid"
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
TOKEN = "mw_contract_test_credential_0123456789"

#: (desktop model, backend model) pairs that must be identical on the wire.
PAIRS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (api_client.BindingSyncItem, MailWorkerBindingItem),
    (api_client.BindingPage, MailWorkerBindingPage),
    (ReplyIngestRequest, MailWorkerReplyRequest),
    (api_client.IngestAck, MailWorkerReplyAck),
    (wire.WorkerSendIntent, MailWorkerSendIntent),
    (wire.SendIntentBatch, MailWorkerSendIntentBatch),
    (wire.ClaimDecision, MailWorkerClaimDecision),
    (wire.WorkerSendReport, MailWorkerSendReport),
    (wire.CheckpointReport, MailWorkerCheckpointReport),
    (wire.GapReport, MailWorkerGapReport),
    (wire.HeartbeatEnvelope, MailWorkerHeartbeatRequest),
    (wire.HeartbeatAck, MailWorkerHeartbeatAck),
    (wire.WorkerAccountReport, MailWorkerAccountReport),
]


def _normalized(model: type[BaseModel]) -> Any:
    """The resolved validation schema without titles/descriptions; ``required`` as a set."""
    schema = model_schema(model, mode="validation", keep_object_titles=False)
    schema.pop("$schema", None)

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in ("title", "description")}
            if isinstance(out.get("required"), list):
                out["required"] = sorted(out["required"])
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return clean(schema)


@pytest.mark.parametrize(("desktop", "backend"), PAIRS, ids=lambda m: m.__name__)
def test_models_are_field_for_field_identical(desktop: type[BaseModel], backend: type[BaseModel]) -> None:
    assert set(desktop.model_fields) == set(backend.model_fields), backend.__name__
    for name, field in desktop.model_fields.items():
        ours = backend.model_fields[name]
        assert ours.is_required() == field.is_required(), f"{backend.__name__}.{name} required"
        if not field.is_required():
            assert ours.get_default(call_default_factory=True) == field.get_default(
                call_default_factory=True
            ), f"{backend.__name__}.{name} default"
    # Types, limits, patterns, enums and defaults as published.
    assert _normalized(backend) == _normalized(desktop), backend.__name__


def test_schema_versions_are_1_0() -> None:
    for desktop, backend in PAIRS:
        if "schema_version" in desktop.model_fields:
            assert (
                backend.model_fields["schema_version"].annotation
                == desktop.model_fields["schema_version"].annotation
            )
    assert wire.WIRE_SCHEMA_VERSION == "1.0"


def test_paths_limits_and_headers_match_the_client() -> None:
    paths = {route.key for route in MAIL_WORKER_ROUTES}
    assert paths == {
        f"GET {api_client.BINDINGS_PATH}",
        f"POST {api_client.REPLIES_PATH}",
        f"GET {api_client.SEND_INTENTS_PATH}",
        f"POST {api_client.SEND_INTENTS_PATH}/{{intent_id}}/claim",
        f"POST {api_client.SEND_INTENTS_PATH}/{{intent_id}}/report",
        f"POST {api_client.HEARTBEAT_PATH}",
        f"POST {api_client.ACCOUNT_REPORT_PATH}",
    }
    assert all(route.path.startswith(MAIL_WORKER_PREFIX) for route in MAIL_WORKER_ROUTES)
    assert all(
        route.auth == "mail_worker" and route.scope == Scope.MAIL_INGEST for route in MAIL_WORKER_ROUTES
    )
    assert MAIL_WORKER_BODY_LIMIT == MAX_REQUEST_BYTES == 128 * 1024
    assert IDEMPOTENCY_HEADER == "Idempotency-Key"
    assert MailWorkerBindingsQuery.model_validate({"limit": "100"}).limit == 100
    with pytest.raises(ValueError, match="limit"):
        MailWorkerBindingsQuery.model_validate({"limit": "101"})
    with pytest.raises(ValueError, match="cursor"):
        MailWorkerBindingsQuery.model_validate({"cursor": "has space"})
    assert MailWorkerSendIntentsQuery.model_validate({"limit": "50"}).limit == 50
    with pytest.raises(ValueError, match="limit"):
        MailWorkerSendIntentsQuery.model_validate({"limit": "51"})
    with pytest.raises(ValueError, match="extra"):
        MailWorkerBindingsQuery.model_validate({"workspace_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------------------------
# The real desktop client against a server made of the backend models
# ---------------------------------------------------------------------------------------------


def _reply_request(**overrides: Any) -> ReplyIngestRequest:
    """The spec 37.8 example (synthetic fixture only; not a real e-mail or address)."""
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "inquiry_id": str(INQUIRY),
        "binding_version": 2,
        "mailbox_binding_id": str(MAILBOX),
        "source_message": {
            "internet_message_id": "<synthetic-reply@example.invalid>",
            "provider_message_id": None,
            "outlook_entry_id": "synthetic-local-locator",
            "outlook_store_id": "synthetic-store-locator",
            "received_at": "2026-10-06T18:00:00Z",
        },
        "headers": {
            "from": "seller@example.invalid",
            "in_reply_to": "<synthetic-inquiry@example.invalid>",
            "references": ["<synthetic-inquiry@example.invalid>"],
        },
        "subject": "Synthetic vehicle reply",
        "sanitized_body_text": "Synthetic fixture only: the vehicle is available.",
        "detected_language": "en",
        "attachments": [],
        "observed_at": "2026-10-06T18:00:02Z",
    }
    data.update(overrides)
    return ReplyIngestRequest.model_validate(data)


class ContractServer:
    """Answers with backend models only; validates every request with the backend models."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        #: Route key (``METHOD /path/{param}``) -> whether the request carried an Idempotency-Key.
        self.idempotency: dict[str, bool] = {}
        self.intent = MailWorkerSendIntent.model_validate_json(
            make_intent(
                inquiry_id=INQUIRY, mailbox_binding_id=MAILBOX, from_address=OWNER, created_at=NOW
            ).model_dump_json()
        )

    def _json(self, model: BaseModel) -> httpx.Response:
        return httpx.Response(200, content=model.model_dump_json().encode("utf-8"))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append((request.method, path))
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        template = re.sub(r"/send-intents/[0-9a-f-]{36}/", "/send-intents/{intent_id}/", path)
        self.idempotency[f"{request.method} {template}"] = IDEMPOTENCY_HEADER in request.headers

        def body[M: BaseModel](model: type[M]) -> M:
            return model.model_validate_json(request.content)

        if request.method == "GET" and path == api_client.BINDINGS_PATH:
            MailWorkerBindingsQuery.model_validate(dict(request.url.params))
            items = (
                MailWorkerBindingItem(
                    inquiry_id=INQUIRY,
                    binding_version=2,
                    mailbox_binding_id=MAILBOX,
                    state="active",  # type: ignore[arg-type]
                    provider="outlook_local",  # type: ignore[arg-type]
                    outbound_message_ids=(inquiry_message_id(INQUIRY),),
                    send_intent_message_ids=(inquiry_message_id(INQUIRY, 2),),
                    verified_seller_aliases=(SELLER,),
                    listing_references=("REF-1234",),
                    listing_urls=("https://listing.example.invalid/ad/1234",),
                ),
                MailWorkerBindingItem(
                    inquiry_id=uuid.UUID(int=5),
                    binding_version=3,
                    mailbox_binding_id=MAILBOX,
                    state="tombstoned",  # type: ignore[arg-type]
                ),
            )
            return self._json(
                MailWorkerBindingPage(schema_version="1.0", items=items, next_cursor="s:42", has_more=False)
            )
        if request.method == "POST" and path == api_client.REPLIES_PATH:
            assert len(request.content) <= MAIL_WORKER_BODY_LIMIT
            key = request.headers[IDEMPOTENCY_HEADER]
            reply = body(MailWorkerReplyRequest)
            assert isinstance(reply, MailWorkerReplyRequest) and reply.mailbox_binding_id == MAILBOX
            assert 8 <= len(key) <= 128
            return self._json(
                MailWorkerReplyAck(
                    schema_version="1.0",
                    reply_id=uuid.UUID(int=9),
                    inquiry_id=reply.inquiry_id,
                    ingest_status="stored",
                    duplicate=False,
                    request_id="req-contract-1",
                    ingested_at=NOW,
                )
            )
        if request.method == "GET" and path == api_client.SEND_INTENTS_PATH:
            MailWorkerSendIntentsQuery.model_validate(dict(request.url.params))
            return self._json(MailWorkerSendIntentBatch(intents=(self.intent,), kill_switch_active=False))
        if request.method == "POST" and path.endswith("/claim"):
            claim = body(MailWorkerClaimRequest)
            assert isinstance(claim, MailWorkerClaimRequest)
            assert path == f"{api_client.SEND_INTENTS_PATH}/{claim.intent_id}/claim"
            assert request.headers[IDEMPOTENCY_HEADER] == f"claim-{claim.intent_id}-{claim.claim_attempt_id}"
            return self._json(MailWorkerClaimDecision(intent_id=claim.intent_id, proceed=True))
        if request.method == "POST" and path.endswith("/report"):
            report = body(MailWorkerSendReport)
            assert isinstance(report, MailWorkerSendReport)
            return self._json(MailWorkerAccepted())
        if request.method == "POST" and path == api_client.HEARTBEAT_PATH:
            body(MailWorkerHeartbeatRequest)
            return self._json(
                MailWorkerHeartbeatAck(received_at=NOW, downstream={"slack_signal": "unverified"})
            )
        if request.method == "POST" and path == api_client.ACCOUNT_REPORT_PATH:
            body(MailWorkerAccountReport)
            return self._json(MailWorkerAccepted())
        return httpx.Response(404, json={})


@pytest.fixture
def server_and_client() -> tuple[ContractServer, api_client.BridgeApiClient]:
    server = ContractServer()
    client = api_client.BridgeApiClient(
        "https://api.example.invalid",
        token_provider=lambda: TOKEN,
        identity=api_client.ClientIdentity(mailbox_binding_id=MAILBOX, worker_id="desktop-contract"),
        transport=httpx.MockTransport(server.handle),
    )
    return server, client


def test_the_desktop_client_round_trips_every_route(
    server_and_client: tuple[ContractServer, api_client.BridgeApiClient],
) -> None:
    server, client = server_and_client
    page = client.fetch_binding_page(None, limit=100)
    assert [item.state.value for item in page.items] == ["active", "tombstoned"]
    assert page.next_cursor == "s:42" and page.items[0].to_record().binding is not None

    request = _reply_request()
    payload = wire_payload(request)
    ack = client.post_reply(
        encode_payload(payload), idempotency_key=idempotency_key_for(request), expected_inquiry_id=INQUIRY
    )
    assert ack.ingest_status == "stored" and ack.duplicate is False

    batch = client.fetch_send_intents(limit=10)
    assert batch.intents[0].intent_id == server.intent.intent_id
    assert wire.intent_integrity_problems(batch.intents[0]) == ()

    decision = client.claim_send_intent(server.intent.intent_id)
    assert decision.proceed is True

    client.post_send_report(
        wire.WorkerSendReport(
            intent_id=server.intent.intent_id,
            inquiry_id=INQUIRY,
            mailbox_binding_id=MAILBOX,
            worker_id="desktop-contract",
            state=wire.SubmissionState.SENT_ITEMS_CONFIRMED,
            account_smtp_address_used=OWNER,
            observed_internet_message_id=server.intent.rfc_message_id,
            outbox_pending=wire.Tristate.NO,
            sent_items_present=True,
            reported_at=NOW,
            sent_at=NOW,
        )
    )
    ack_hb = client.post_heartbeat(
        wire.HeartbeatEnvelope(
            heartbeat=wire.WorkerHeartbeat(
                mailbox_binding_id=MAILBOX,
                worker_id="desktop-contract",
                at=NOW,
                outlook_running=True,
                mailbox_connected=True,
            ),
            checkpoints=(
                wire.CheckpointReport(store_id_hash="a" * 64, folder_id_hash="b" * 64, folder_role="inbox"),
            ),
            gaps=(wire.GapReport(kind="worker_offline", started_at=NOW - timedelta(hours=1), ended_at=NOW),),
        )
    )
    assert ack_hb.downstream == {"slack_signal": "unverified"}
    client.post_account_report(
        wire.WorkerAccountReport(
            mailbox_binding_id=MAILBOX,
            worker_id="desktop-contract",
            reported_at=NOW,
            outlook_flavour="classic",
            stable_account_key="account-key-0001",
            account_smtp_address=OWNER,
        )
    )
    assert [method for method, _ in server.seen] == ["GET", "POST", "GET", "POST", "POST", "POST", "POST"]
    # The Idempotency-Key header rule of every route is exactly what the desktop client sends:
    # required on replies/claim/report, absent on heartbeat/account-report and the GET routes
    # (a server that required it there would refuse the worker's health reports).
    assert server.idempotency == {
        route.key: route.idempotency_header == "required" for route in MAIL_WORKER_ROUTES
    }


def test_reply_extensions_and_limits_are_accepted_exactly_like_the_domain() -> None:
    """The worker's optional extensions validate; out-of-scope uploads are refused."""
    quarantined = _reply_request(
        message_type="ambiguous",
        correlation_status="quarantined",
        correlation_reasons=["THREAD_ONLY_UNCORROBORATED"],
        withheld_sensitive_attachments=1,
    )
    body = wire_payload(quarantined)
    assert MailWorkerReplyRequest.model_validate(body).correlation_status == "quarantined"
    for bad in (
        {"subject": "x" * 513},
        {"sanitized_body_text": "x" * (64 * 1024 + 1)},
        {
            "attachments": [
                {
                    "filename": "../etc/passwd",
                    "mime_type": "application/pdf",
                    "byte_size": 1,
                    "sha256": "a" * 64,
                }
            ]
        },
        {
            "attachments": [
                {
                    "filename": "ok.pdf",
                    "mime_type": "application/pdf",
                    "byte_size": 1,
                    "sha256": "a" * 64,
                    "local_ref": "https://x",
                }
            ]
        },
        {"workspace_id": str(uuid.uuid4())},
    ):
        with pytest.raises(ValueError):
            MailWorkerReplyRequest.model_validate(
                {**json.loads(_reply_request().model_dump_json(by_alias=True)), **bad}
            )
    too_many = [
        {"filename": f"doc{i}.pdf", "mime_type": "application/pdf", "byte_size": 1, "sha256": "a" * 64}
        for i in range(21)
    ]
    with pytest.raises(ValueError):
        MailWorkerReplyRequest.model_validate(
            {**json.loads(_reply_request().model_dump_json(by_alias=True)), "attachments": too_many}
        )


def test_backend_models_refuse_what_the_worker_refuses() -> None:
    with pytest.raises(ValueError, match="tombstone"):
        MailWorkerBindingItem(
            inquiry_id=INQUIRY,
            binding_version=1,
            mailbox_binding_id=MAILBOX,
            state="tombstoned",
            listing_urls=("https://x.example.invalid/1",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="provider"):
        MailWorkerBindingItem(
            inquiry_id=INQUIRY, binding_version=1, mailbox_binding_id=MAILBOX, state="active"
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="refusal"):
        MailWorkerClaimDecision(intent_id=INQUIRY, proceed=True, refusal_reason="kill_switch")  # type: ignore[arg-type]
    intent = json.loads(
        make_intent(
            inquiry_id=INQUIRY, mailbox_binding_id=MAILBOX, from_address=OWNER, created_at=NOW
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match="inquiry reference"):
        MailWorkerSendIntent.model_validate({**intent, "inquiry_ref": f"inquiry-{uuid.UUID(int=3)}"})
    with pytest.raises(ValueError):
        MailWorkerAccountReport.model_validate(
            {
                "mailbox_binding_id": str(MAILBOX),
                "worker_id": "w1",
                "reported_at": NOW.isoformat(),
                "outlook_flavour": "classic",
                "stable_account_key": "account-key-0001",
                "account_smtp_address": OWNER,
                "security_settings_unchanged": False,
            }
        )
    with pytest.raises(ValueError, match="short codes"):
        MailWorkerHeartbeatAck(downstream={"slack": "has space"})


def test_schema_version_is_present_exactly_where_the_wire_has_it() -> None:
    """Every top-level request/response carries ``schema_version: "1.0"`` except the
    account-report body (the desktop ``WorkerAccountReport`` has none, so a server that required
    it would refuse every account verification)."""
    without = {
        route.key
        for route in MAIL_WORKER_ROUTES
        for model in (route.request_model, route.response_model)
        if model is not None
        and model not in (MailWorkerBindingsQuery, MailWorkerSendIntentsQuery)
        and "schema_version" not in model.model_fields
    }
    assert without == {"POST /v1/mail-workers/account-report"}
    assert "schema_version" not in wire.WorkerAccountReport.model_fields
