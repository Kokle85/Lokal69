"""The mailbox-worker API ``/v1/mail-workers/*`` (spec 37.8; docs/api_contract.md section 10.1).

Real ASGI app, real PostgreSQL (``Database(set_role="suv_backend")``), a real ``suvmail_``
credential issued through the repository, SYNTHETIC data only. Nothing is ever sent: a send intent
is a committed attempt that only a desktop worker could execute.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from tests.api.conftest import (
    UNREACHABLE_DB,
    SigningKeys,
    TokenFactory,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.api.v11_support import (
    MAIL,
    MailWorker,
    account_report_body,
    claim_body,
    controls_version,
    dispatch_intent,
    expire_attempt,
    heartbeat_body,
    issue_worker,
    one,
    outlook_world,
    owner_actor,
    reply_body,
    report_body,
    rows,
)
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import SENDER_ADDRESS, World, system

from suv_deals.api.middleware import DEFAULT_PREAUTH_LIMIT, PrincipalRateLimiter, RateLimit
from suv_deals.integrations.email_providers.outlook_local import OutlookSendIntent
from suv_deals.persistence import inquiries_repo, mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings

pytestmark = pytest.mark.db


@dataclass
class MailHarness:
    client: httpx.AsyncClient
    db: Database
    seed: Seed
    world: World
    worker: MailWorker

    async def get(self, path: str, *, worker: MailWorker | None = None, **kwargs: Any) -> httpx.Response:
        headers = {**(worker or self.worker).headers(), **kwargs.pop("headers", {})}
        return await self.client.get(MAIL + path, headers=headers, **kwargs)

    async def post(
        self,
        path: str,
        body: Any,
        *,
        key: str | None = None,
        worker: MailWorker | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {**(worker or self.worker).headers(key), **kwargs.pop("headers", {})}
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
        return await self.client.post(MAIL + path, headers=headers, content=content, **kwargs)

    async def bindings(self, cursor: str | None = None) -> dict[str, Any]:
        params = {} if cursor is None else {"cursor": cursor}
        response = await self.get("/inquiry-bindings", params=params)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data


@pytest.fixture
async def mail(db: Database, seed: Seed, keys: SigningKeys) -> AsyncIterator[MailHarness]:
    world = await outlook_world(db, seed, "API mail worker A")
    worker = await issue_worker(db, world)
    # The process gate (SELLER_INQUIRY_MODE / SELLER_INQUIRY_KILL_SWITCH) is open, as in a
    # workspace that may send; the default settings keep it closed.
    app = build_test_app(make_settings(seller_inquiry_mode="automatic"), keys, db)
    async with running_client(app) as client:
        yield MailHarness(client=client, db=db, seed=seed, world=world, worker=worker)


async def _accepted_send(h: MailHarness) -> tuple[UUID, OutlookSendIntent]:
    """Dispatch the world's vehicle and report Sent Items evidence through the API."""
    inquiry_id, intent = await dispatch_intent(h.db, h.world, h.worker.mailbox_id)
    claim = await h.post(
        f"/send-intents/{intent.intent_id}/claim",
        claim_body(intent, h.worker.mailbox_id),
        key=f"claim-{intent.intent_id}-{uuid.uuid4().hex}",
    )
    assert claim.status_code == 200 and claim.json()["proceed"] is True, claim.text
    report = await h.post(
        f"/send-intents/{intent.intent_id}/report",
        report_body(intent, "sent_items_confirmed"),
        key=f"report-{intent.intent_id}-sent_items_confirmed",
    )
    assert report.status_code == 200, report.text
    return inquiry_id, intent


async def _latest_version(h: MailHarness, inquiry_id: UUID) -> int:
    items = [i for i in (await h.bindings())["items"] if i["inquiry_id"] == str(inquiry_id)]
    return max(i["binding_version"] for i in items)


# ---------------------------------------------------------------------------------- authentication


async def test_requests_without_a_worker_credential_are_401(mail: MailHarness, tokens: TokenFactory) -> None:
    missing = await mail.client.get(MAIL + "/inquiry-bindings")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert error_of(missing)["code"] == "UNAUTHENTICATED"
    dashboard = await mail.client.get(
        MAIL + "/inquiry-bindings", headers={"Authorization": f"Bearer {tokens.mint(uuid.uuid4())}"}
    )
    assert dashboard.status_code == 401  # a dashboard JWT is never a worker credential
    mcp_like = await mail.client.get(
        MAIL + "/inquiry-bindings", headers={"Authorization": "Bearer suvmcp_" + "a" * 64}
    )
    assert mcp_like.status_code == 401
    unknown = await mail.client.get(
        MAIL + "/inquiry-bindings", headers={"Authorization": "Bearer suvmail_" + "b" * 64}
    )
    assert unknown.status_code == 401
    assert error_of(unknown)["details"] in ({}, None) or "reason" not in error_of(unknown)["details"]
    doubled = await mail.client.get(
        MAIL + "/inquiry-bindings",
        headers=[("Authorization", f"Bearer {mail.worker.token}"), ("Authorization", "Bearer x")],
    )
    assert doubled.status_code == 401


async def test_other_token_kinds_are_refused_by_format_without_a_database_lookup(
    settings: Settings, keys: SigningKeys, tokens: TokenFactory
) -> None:
    """With an unreachable database a format-rejected token is still ``401`` (never ``503``)."""
    db = Database(UNREACHABLE_DB, set_role="suv_backend", min_size=0, max_size=1, pool_timeout_s=0.5)
    app = build_test_app(settings, keys, db)
    async with running_client(app) as client:
        for token in (tokens.mint(uuid.uuid4()), "suvmcp_" + "c" * 64, "suvdev_" + "d" * 64, "garbage"):
            response = await client.get(MAIL + "/send-intents", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, (token[:8], response.text)
        well_formed = await client.get(
            MAIL + "/send-intents", headers={"Authorization": "Bearer suvmail_" + "e" * 64}
        )
        assert well_formed.status_code == 503  # only a well-formed worker token reaches the database


async def test_revoked_credential_is_401_with_a_stable_reason(mail: MailHarness) -> None:
    assert (await mail.get("/send-intents")).status_code == 200
    actor = system(mail.world.workspace_id)
    async with unit_of_work(mail.db, actor) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, actor, mail.worker.mailbox_id, reason="laptop replaced (synthetic)"
        )
    response = await mail.get("/send-intents")
    assert response.status_code == 401
    assert error_of(response)["details"]["reason"] == "mail_worker_credential_revoked"
    body = await mail.post("/heartbeat", heartbeat_body(mail.worker.mailbox_id))
    assert body.status_code == 401


async def test_tokens_are_never_accepted_in_the_query_string(mail: MailHarness) -> None:
    response = await mail.client.get(MAIL + "/inquiry-bindings", params={"access_token": mail.worker.token})
    assert response.status_code == 401  # no Authorization header: the query is never read
    response = await mail.get("/inquiry-bindings", params={"access_token": mail.worker.token})
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == ["query.access_token"] or "access_token" in str(
        error_of(response)["details"]
    )
    assert mail.worker.token not in response.text


async def test_failed_authentications_exhaust_the_client_budget_before_verification(
    mail: MailHarness,
) -> None:
    for _ in range(DEFAULT_PREAUTH_LIMIT.capacity):
        bad = await mail.client.get(
            MAIL + "/send-intents", headers={"Authorization": "Bearer suvmail_" + "f" * 64}
        )
        assert bad.status_code == 401
    limited = await mail.get("/send-intents")  # even a valid credential: refused before lookup
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert error_of(limited)["code"] == "RATE_LIMITED"


async def test_unknown_mail_worker_paths_are_api_404_and_wrong_methods_405(mail: MailHarness) -> None:
    missing = await mail.get("/nothing-here")
    assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    wrong = await mail.client.delete(MAIL + "/heartbeat", headers=mail.worker.headers())
    assert wrong.status_code == 405
    assert "POST" in wrong.headers["allow"]


# ---------------------------------------------------------------------------------- bindings


async def test_binding_sync_returns_the_mailbox_changes_with_an_opaque_cursor(mail: MailHarness) -> None:
    first = await mail.bindings()
    assert first["schema_version"] == "1.0" and first["items"] == [] and first["has_more"] is False
    inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    page = await mail.bindings(first["next_cursor"])
    assert [i["inquiry_id"] for i in page["items"]] == [str(inquiry_id)]
    item = page["items"][0]
    assert item["mailbox_binding_id"] == str(mail.worker.mailbox_id)
    assert item["state"] == "active" and item["provider"] == "outlook_local"
    assert intent.rfc_message_id in item["send_intent_message_ids"]
    assert page["next_cursor"] and page["next_cursor"] != first["next_cursor"]
    again = await mail.bindings(page["next_cursor"])
    assert again["items"] == []
    cursor = page["next_cursor"]
    flipped = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")  # always a DIFFERENT signature
    tampered = await mail.get("/inquiry-bindings", params={"cursor": flipped})
    assert tampered.status_code == 422
    too_many = await mail.get("/inquiry-bindings", params={"limit": "101"})
    assert too_many.status_code == 422


async def test_binding_items_publish_the_observed_message_id_of_a_sent_copy(mail: MailHarness) -> None:
    inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    claim = await mail.post(
        f"/send-intents/{intent.intent_id}/claim",
        claim_body(intent, mail.worker.mailbox_id),
        key="claim-" + "1" * 32,
    )
    assert claim.json()["proceed"] is True
    rewritten = "<rewritten-by-client-0001@synthetic-mail.example>"
    report = await mail.post(
        f"/send-intents/{intent.intent_id}/report",
        report_body(intent, "sent_items_confirmed", observed=rewritten),
        key=f"report-{intent.intent_id}-sent_items_confirmed",
    )
    assert report.status_code == 200, report.text
    items = [i for i in (await mail.bindings())["items"] if i["inquiry_id"] == str(inquiry_id)]
    latest = max(items, key=lambda i: i["binding_version"])
    assert latest["state"] == "active"
    assert rewritten in latest["outbound_message_ids"]
    assert intent.rfc_message_id in latest["outbound_message_ids"]


# ---------------------------------------------------------------------------------- replies


async def test_reply_upload_requires_an_idempotency_key_header(mail: MailHarness) -> None:
    inquiry_id, intent = await _accepted_send(mail)
    body = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=mail.worker.mailbox_id,
        binding_version=await _latest_version(mail, inquiry_id),
        from_address=str(mail.world.vehicle.address),
        in_reply_to=intent.rfc_message_id,
    )
    missing = await mail.post("/replies", body)
    assert missing.status_code == 400
    assert error_of(missing)["details"]["fields"] == ["Idempotency-Key"]
    malformed = await mail.post("/replies", body, key="short")
    assert malformed.status_code == 422


async def test_correlated_reply_is_stored_once_with_its_signal_and_replayed_as_duplicate(
    mail: MailHarness,
) -> None:
    inquiry_id, intent = await _accepted_send(mail)
    body = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=mail.worker.mailbox_id,
        binding_version=await _latest_version(mail, inquiry_id),
        from_address=str(mail.world.vehicle.address),
        in_reply_to=intent.rfc_message_id,
    )
    stored = await mail.post("/replies", body, key="mwr1-synthetic-key-0001")
    assert stored.status_code == 200, stored.text
    ack = stored.json()
    assert ack["schema_version"] == "1.0" and ack["inquiry_id"] == str(inquiry_id)
    assert ack["ingest_status"] == "stored" and ack["duplicate"] is False
    assert ack["request_id"] == stored.headers["x-request-id"]
    reply = one(
        mail.seed.conn,
        "select id, quarantined, message_type from app.seller_replies where id = %s",
        UUID(ack["reply_id"]),
    )
    assert reply["quarantined"] is False and reply["message_type"] == "seller_reply"
    signal = rows(
        mail.seed.conn,
        "select payload from ops.outbox where workspace_id = %s and event_type = 'seller.reply.received'",
        mail.world.workspace_id,
    )
    assert len(signal) == 1
    payload = json.dumps(signal[0]["payload"])
    assert str(mail.world.vehicle.address) not in payload and "verfuegbar" not in payload
    job = rows(
        mail.seed.conn,
        "select state from ops.jobs where workspace_id = %s and job_type = 'seller_reply_process'",
        mail.world.workspace_id,
    )
    assert len(job) == 1  # the processing job is queued with the reply
    # The same key and message again (e.g. a lost acknowledgement): the existing reply id.
    again = await mail.post("/replies", body, key="mwr1-synthetic-key-0001")
    assert again.status_code == 200
    assert again.json()["reply_id"] == ack["reply_id"] and again.json()["duplicate"] is True
    # The same source identity under ANOTHER key after a folder move: still a duplicate.
    moved = {**body, "source_message": {**body["source_message"], "outlook_entry_id": "moved-locator"}}
    replay = await mail.post("/replies", moved, key="mwr1-synthetic-key-0002")
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    # A conflicting body under the same immutable identity: 409 and quarantined, never overwritten.
    changed = {**body, "sanitized_body_text": "Synthetic: a DIFFERENT body under the same Message-ID."}
    conflict = await mail.post("/replies", changed, key="mwr1-synthetic-key-0001")
    assert conflict.status_code == 409
    assert error_of(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(rows(mail.seed.conn, "select id from app.seller_inquiries where id = %s", inquiry_id)) == 1


async def test_unknown_and_foreign_inquiries_are_the_same_403_without_existence_leak(
    mail: MailHarness, db: Database, seed: Seed
) -> None:
    other_world = await outlook_world(db, seed, "API mail worker B")
    other_worker = await issue_worker(db, other_world)
    foreign_inquiry, foreign_intent = await dispatch_intent(db, other_world, other_worker.mailbox_id)
    responses = []
    for target in (foreign_inquiry, uuid.uuid4()):
        body = reply_body(
            inquiry_id=target,
            mailbox_id=mail.worker.mailbox_id,
            binding_version=1,
            from_address=str(other_world.vehicle.address),
            in_reply_to=foreign_intent.rfc_message_id,
        )
        response = await mail.post("/replies", body, key=f"mwr1-cross-{target.hex[:20]}")
        assert response.status_code == 403
        responses.append(error_of(response))
    assert {r["details"]["reason"] for r in responses} == {"mailbox_binding_mismatch"}
    assert responses[0]["message"] == responses[1]["message"]
    # Naming another mailbox in the body (cross-mailbox injection) is the same refusal.
    injected = reply_body(
        inquiry_id=foreign_inquiry,
        mailbox_id=other_worker.mailbox_id,
        binding_version=1,
        from_address=str(other_world.vehicle.address),
        in_reply_to=foreign_intent.rfc_message_id,
    )
    response = await mail.post("/replies", injected, key="mwr1-cross-mailbox-0001")
    assert response.status_code == 403
    assert error_of(response)["details"]["reason"] == "mailbox_binding_mismatch"
    assert rows(seed.conn, "select id from app.seller_replies where inquiry_id = %s", foreign_inquiry) == []


async def test_reply_body_limits_and_extensions(mail: MailHarness) -> None:
    inquiry_id, intent = await _accepted_send(mail)
    version = await _latest_version(mail, inquiry_id)
    base = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=mail.worker.mailbox_id,
        binding_version=version,
        from_address=str(mail.world.vehicle.address),
        in_reply_to=intent.rfc_message_id,
    )
    oversized = json.dumps({**base, "sanitized_body_text": "x" * (130 * 1024)}).encode()
    too_large = await mail.post("/replies", oversized, key="mwr1-oversized-0001")
    assert too_large.status_code == 413
    returned = {**base, "returned_message_ids": [intent.rfc_message_id]}
    not_a_bounce = await mail.post("/replies", returned, key="mwr1-returned-0001")
    assert not_a_bounce.status_code == 422  # returned originals belong to bounces only
    unknown_field = await mail.post(
        "/replies", {**base, "workspace_id": str(uuid.uuid4())}, key="mwr1-extra-0001"
    )
    assert unknown_field.status_code == 422
    wrong_type = await mail.client.post(
        MAIL + "/replies",
        headers={**mail.worker.headers("mwr1-type-0001"), "Content-Type": "text/plain"},
        content=json.dumps(base).encode(),
    )
    assert wrong_type.status_code == 415


async def test_bounce_with_returned_message_ids_links_to_the_inquiry(mail: MailHarness) -> None:
    inquiry_id, intent = await _accepted_send(mail)
    version = await _latest_version(mail, inquiry_id)
    bounce = reply_body(
        inquiry_id=inquiry_id,
        mailbox_id=mail.worker.mailbox_id,
        binding_version=version,
        from_address="mailer-daemon@synthetic-relay.example",
        in_reply_to=None,
        subject="Undelivered Mail Returned to Sender",
        body=(
            "This is the mail system at host synthetic-relay.example.\n"
            "I'm sorry to have to inform you that your message could not be delivered.\n"
            f"Final-Recipient: rfc822; {mail.world.vehicle.address}\n"
            "Action: failed\nStatus: 5.1.1\n"
            "Diagnostic-Code: smtp; 550 5.1.1 user unknown\n"
        ),
        message_type="bounce",
        returned_message_ids=[intent.rfc_message_id],
    )
    stored = await mail.post("/replies", bounce, key="mwr1-bounce-0001")
    assert stored.status_code == 200, stored.text
    row = one(
        mail.seed.conn,
        "select message_type, returned_message_ids, quarantined from app.seller_replies where id = %s",
        UUID(stored.json()["reply_id"]),
    )
    assert row["message_type"] == "bounce"
    assert intent.rfc_message_id in row["returned_message_ids"]
    assert row["quarantined"] is False


# ---------------------------------------------------------------------------------- send intents


async def test_send_intents_claim_and_report_lifecycle(mail: MailHarness) -> None:
    inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    listed = await mail.get("/send-intents")
    assert listed.status_code == 200
    batch = listed.json()
    assert batch["kill_switch_active"] is False
    assert [i["intent_id"] for i in batch["intents"]] == [str(intent.intent_id)]
    assert batch["intents"][0]["expired"] is False
    assert batch["intents"][0]["inquiry_ref"] == f"inquiry-{inquiry_id}"
    path = f"/send-intents/{intent.intent_id}"
    no_key = await mail.post(path + "/claim", claim_body(intent, mail.worker.mailbox_id))
    assert no_key.status_code == 400
    mismatch = await mail.post(
        path + "/claim",
        claim_body(intent, mail.worker.mailbox_id, intent_id=str(uuid.uuid4())),
        key="claim-x-1",
    )
    assert mismatch.status_code == 422 and error_of(mismatch)["details"]["fields"] == ["intent_id"]
    other_box = await mail.post(path + "/claim", claim_body(intent, uuid.uuid4()), key="claim-other-mailbox")
    assert other_box.status_code == 403
    first = await mail.post(
        path + "/claim", claim_body(intent, mail.worker.mailbox_id), key="claim-fresh-0001"
    )
    assert first.status_code == 200 and first.json() == {
        "schema_version": "1.0",
        "intent_id": str(intent.intent_id),
        "proceed": True,
        "refusal_reason": None,
    }
    key = f"report-{intent.intent_id}-sent_items_confirmed"
    sent = report_body(intent, "sent_items_confirmed")
    report = await mail.post(path + "/report", sent, key=key)
    assert report.status_code == 200 and report.json() == {"schema_version": "1.0", "accepted": True}
    replay = await mail.post(path + "/report", sent, key=key)
    assert replay.status_code == 200
    other = await mail.post(path + "/report", report_body(intent, "submitted_to_outbox"), key=key)
    assert other.status_code == 409 and error_of(other)["code"] == "IDEMPOTENCY_CONFLICT"
    state = one(mail.seed.conn, "select state from app.seller_inquiries where id = %s", inquiry_id)["state"]
    assert state == "accepted"
    after = await mail.get("/send-intents")
    assert after.json()["intents"] == []  # a finished intent is never offered again
    late = await mail.post(path + "/claim", claim_body(intent, mail.worker.mailbox_id), key="claim-late-0001")
    assert late.json()["proceed"] is False and late.json()["refusal_reason"] == "intent_invalid"


async def test_claim_refusals_kill_switch_and_not_now(mail: MailHarness) -> None:
    _inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    path = f"/send-intents/{intent.intent_id}/claim"
    admin = owner_actor(mail.world.workspace_id)
    async with unit_of_work(mail.db, admin) as conn:
        controls = await inquiries_repo.get_controls(conn, admin)
        assert controls is not None
        await inquiries_repo.set_limits(
            conn,
            admin,
            expected_version=controls.version,
            max_per_24h=0,
            max_per_15d=controls.max_per_15d,
            seller_cooldown=controls.seller_cooldown,
            reason="owner lowered the cap (synthetic)",
        )
    waiting = await mail.post(path, claim_body(intent, mail.worker.mailbox_id), key="claim-cap-0001")
    assert waiting.json()["proceed"] is False
    assert waiting.json()["refusal_reason"] == "not_now"  # a waiting condition, never kill_switch
    version = await controls_version(mail.db, mail.world.workspace_id)
    async with unit_of_work(mail.db, admin) as conn:
        await inquiries_repo.pause(conn, admin, expected_version=version, reason="owner pause (synthetic)")
    paused = await mail.post(path, claim_body(intent, mail.worker.mailbox_id), key="claim-kill-0001")
    assert paused.json()["proceed"] is False and paused.json()["refusal_reason"] == "kill_switch"
    listed = await mail.get("/send-intents")
    assert listed.json()["kill_switch_active"] is True


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        (
            {"seller_inquiry_mode": "automatic", "seller_inquiry_kill_switch": True},
            "SETTINGS_KILL_SWITCH_ACTIVE",
        ),
        ({"seller_inquiry_mode": "disabled_until_sender_ready"}, "SETTINGS_MODE_NOT_AUTOMATIC"),
        ({"seller_inquiry_mode": "paused"}, "SETTINGS_MODE_NOT_AUTOMATIC"),
    ],
)
async def test_a_closed_process_gate_refuses_every_claim_like_the_kill_switch(
    db: Database, seed: Seed, keys: SigningKeys, overrides: dict[str, Any], detail: str
) -> None:
    """Nothing is sent unless SELLER_INQUIRY_MODE=automatic and SELLER_INQUIRY_KILL_SWITCH is off:
    the claim right before ``.Send`` is the last guard, even for an intent committed earlier."""
    world = await outlook_world(db, seed, f"API mail worker gate {detail}")
    worker = await issue_worker(db, world)
    inquiry_id, intent = await dispatch_intent(db, world, worker.mailbox_id)  # database controls: open
    other_world = await outlook_world(db, seed, f"API mail worker gate other {detail}")
    other_worker = await issue_worker(db, other_world)
    _foreign, foreign_intent = await dispatch_intent(db, other_world, other_worker.mailbox_id)
    app = build_test_app(make_settings(**overrides), keys, db)
    async with running_client(app) as client:
        listed = await client.get(MAIL + "/send-intents", headers=worker.headers())
        assert listed.status_code == 200
        assert listed.json()["kill_switch_active"] is True  # the worker refuses, never sends
        assert [i["intent_id"] for i in listed.json()["intents"]] == [str(intent.intent_id)]
        claim = await client.post(
            f"{MAIL}/send-intents/{intent.intent_id}/claim",
            headers=worker.headers(f"claim-{intent.intent_id}-{uuid.uuid4().hex}"),
            json=claim_body(intent, worker.mailbox_id),
        )
        assert claim.status_code == 200, claim.text
        assert claim.json()["proceed"] is False and claim.json()["refusal_reason"] == "kill_switch"
        # Another mailbox's intent is still the same 403 (no existence leak behind the gate).
        foreign = await client.post(
            f"{MAIL}/send-intents/{foreign_intent.intent_id}/claim",
            headers=worker.headers(f"claim-{foreign_intent.intent_id}-{uuid.uuid4().hex}"),
            json=claim_body(foreign_intent, worker.mailbox_id),
        )
        assert foreign.status_code == 403
    audits = rows(
        seed.conn,
        "select metadata, reason from ops.audit_events where target_id = %s and action = 'send_intent.claim'",
        inquiry_id,
    )
    assert [(a["metadata"]["proceed"], a["metadata"]["detail"], a["reason"]) for a in audits] == [
        (False, detail, "claim refused")
    ]


async def test_reaped_unclaimed_intent_is_listed_expired_and_its_report_reconciles(
    mail: MailHarness, db_conn: psycopg.Connection
) -> None:
    inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    expire_attempt(db_conn, intent.intent_id)
    reaped = await inquiries_repo.reap_expired_attempts(mail.db, mail.world.workspace_id)
    assert intent.intent_id in reaped  # attempt ids
    assert (
        one(db_conn, "select state from app.seller_inquiries where id = %s", inquiry_id)["state"]
        == "uncertain"
    )
    listed = (await mail.get("/send-intents")).json()
    assert [(i["intent_id"], i["expired"]) for i in listed["intents"]] == [(str(intent.intent_id), True)]
    refused = report_body(intent, "refused_before_send", refusal="intent_expired")
    response = await mail.post(
        f"/send-intents/{intent.intent_id}/report",
        refused,
        key=f"report-{intent.intent_id}-refused_before_send",
    )
    assert response.status_code == 200, response.text
    state = one(db_conn, "select state from app.seller_inquiries where id = %s", inquiry_id)["state"]
    assert state == "failed_definite"  # proven non-submission: no longer uncertain forever
    attempt = one(
        db_conn,
        "select reconciled_outcome from ops.email_delivery_attempts where attempt_id = %s",
        intent.intent_id,
    )
    assert attempt["reconciled_outcome"] == "proven_not_submitted"
    assert (await mail.get("/send-intents")).json()["intents"] == []


async def test_claimed_then_reaped_intent_is_never_listed_expired(
    mail: MailHarness, db_conn: psycopg.Connection
) -> None:
    """A granted claim means ``.Send`` may have run: the reaped intent stays uncertain."""
    inquiry_id, intent = await dispatch_intent(mail.db, mail.world, mail.worker.mailbox_id)
    claim = await mail.post(
        f"/send-intents/{intent.intent_id}/claim",
        claim_body(intent, mail.worker.mailbox_id),
        key="claim-2" * 2,
    )
    assert claim.json()["proceed"] is True
    expire_attempt(db_conn, intent.intent_id)
    await inquiries_repo.reap_expired_attempts(mail.db, mail.world.workspace_id)
    assert (await mail.get("/send-intents")).json()["intents"] == []
    assert (
        one(db_conn, "select state from app.seller_inquiries where id = %s", inquiry_id)["state"]
        == "uncertain"
    )


async def test_reports_name_only_the_workers_own_intents(mail: MailHarness, db: Database, seed: Seed) -> None:
    other_world = await outlook_world(db, seed, "API mail worker C")
    other_worker = await issue_worker(db, other_world)
    _foreign, intent = await dispatch_intent(db, other_world, other_worker.mailbox_id)
    body = report_body(intent, "refused_before_send", refusal="kill_switch")
    response = await mail.post(
        f"/send-intents/{intent.intent_id}/report",
        {**body, "mailbox_binding_id": str(mail.worker.mailbox_id)},
        key="report-foreign-0001",
    )
    assert response.status_code == 403
    assert error_of(response)["details"]["reason"] == "mailbox_binding_mismatch"


# ---------------------------------------------------------------------------------- health


async def test_heartbeat_and_gap_validation(mail: MailHarness) -> None:
    ack = await mail.post("/heartbeat", heartbeat_body(mail.worker.mailbox_id))
    assert ack.status_code == 200, ack.text
    assert ack.json()["schema_version"] == "1.0" and ack.json()["downstream"]["backend"] == "ok"
    backwards = heartbeat_body(
        mail.worker.mailbox_id,
        gaps=[
            {
                "kind": "worker_offline",
                "started_at": "2026-10-07T10:00:00Z",
                "ended_at": "2026-10-07T09:00:00Z",
            }
        ],
    )
    refused = await mail.post("/heartbeat", backwards)
    assert refused.status_code == 422
    foreign = heartbeat_body(uuid.uuid4())
    assert (await mail.post("/heartbeat", foreign)).status_code == 403


async def test_account_report_verifies_or_records_a_refusal(mail: MailHarness) -> None:
    ok = await mail.post("/account-report", account_report_body(mail.worker.mailbox_id, SENDER_ADDRESS))
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"schema_version": "1.0", "accepted": True}
    with_version = {**account_report_body(mail.worker.mailbox_id, SENDER_ADDRESS), "schema_version": "1.0"}
    assert (await mail.post("/account-report", with_version)).status_code == 422
    weakened = account_report_body(mail.worker.mailbox_id, SENDER_ADDRESS, security_settings_unchanged=False)
    assert (await mail.post("/account-report", weakened)).status_code == 422
    wrong = await mail.post(
        "/account-report", account_report_body(mail.worker.mailbox_id, "someone-else@synthetic-mail.example")
    )
    assert wrong.status_code == 409
    assert error_of(wrong)["details"]["reason"] == "mail_worker_account_mismatch"
    assert "synthetic-mail.example" not in wrong.text
    status = one(
        mail.seed.conn,
        "select gap_reasons from ops.mail_worker_checkpoints where mailbox_binding_id = %s"
        " and store_id_hash = %s",
        mail.worker.mailbox_id,
        mail_workers_repo.HEALTH_ROW_HASH,
    )
    assert "account:mismatch" in status["gap_reasons"]  # recorded although the request failed


async def test_per_credential_rate_limits(
    db: Database, seed: Seed, settings: Settings, keys: SigningKeys
) -> None:
    world = await outlook_world(db, seed, "API mail worker rate limit")
    worker = await issue_worker(db, world)
    other_world = await outlook_world(db, seed, "API mail worker rate limit B")
    other = await issue_worker(db, other_world)
    app = build_test_app(settings, keys, db)
    app.state.suv_api.mail_worker_limiter = PrincipalRateLimiter(
        mutations=RateLimit(capacity=2, per_seconds=60.0), reads=RateLimit(capacity=2, per_seconds=60.0)
    )
    async with running_client(app) as client:
        for _ in range(2):
            ok = await client.get(MAIL + "/send-intents", headers=worker.headers())
            assert ok.status_code == 200
        limited = await client.get(MAIL + "/send-intents", headers=worker.headers())
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        assert error_of(limited)["code"] == "RATE_LIMITED"
        beat = await client.post(
            MAIL + "/heartbeat", headers=worker.headers(), json=heartbeat_body(worker.mailbox_id)
        )
        assert beat.status_code == 200  # mutations have their own bucket
        assert (await client.get(MAIL + "/send-intents", headers=other.headers())).status_code == 200
