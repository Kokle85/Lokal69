"""Adversarial regressions of the B2a runtime (independent safety/privacy/security review).

Every test arranges what an attacker (or a confused configuration) controls -- the seller's
replies, a second sending identity, the process-wide secret reference -- and asserts the safe
outcome. Each one failed on the code before its fix:

- an adversarial seller varying the consequential requests across replies cannot fan out one
  high-priority owner alert per reason COMBINATION (up to 2^8 - 1 per inquiry): a decision alert
  is raised only when the reply asks for something not yet alerted for that inquiry;
- the default ``outlook_local`` route sends only from the CONFIGURED sending identity
  (``SELLER_EMAIL_ACCOUNT_ID`` / ``SELLER_EMAIL_FROM``), exactly like the API route: the plan never
  binds another (newer) verified binding, and a queued inquiry bound to an identity that is no
  longer the configured one is never published to a desktop worker;
- the API route never opens the sealed OAuth grant of a binding OTHER than the inquiry's own
  (``SELLER_EMAIL_OAUTH_SECRET_REFERENCE`` naming another binding is a setup problem, not a
  credential to borrow), and a missing secret-box key is a visible blocker, not dead-letter churn;
- a proven pre-submission refusal reported by the desktop worker (claim refused while paused) is
  retried through the GUARDED retry after the resume instead of ending the authorized inquiry;
- a revoked (e.g. stolen) desktop-worker credential never receives a send intent, however fresh
  its last heartbeat;
- waiting for the recalculation a reply queued writes no audit row per poll (a seller cannot
  flood the audit trail by sending e-mails).
"""

from __future__ import annotations

import itertools
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.pipeline.support import PipelineEnv, run
from tests.integration.v11_runtime.support import (
    SENDER_ADDRESS,
    WORKER_REQ,
    GmailApi,
    attempts_of,
    box,
    debits_of,
    eligible_live_listing,
    gmail_settings,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    now_utc,
    outbox_of,
    pending_intents,
    prepare_sender,
    pull_kill_switch,
    runtime_settings,
    seller_replies,
    with_settings,
    work,
    worker_sends,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryState, JobType
from suv_deals.domain.notifications import SELLER_REPLY_OWNER_ALERT_EVENT_TYPE
from suv_deals.integrations.email_providers.outlook_local import (
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
    OutlookSubmissionState,
)
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.persistence import credentials_repo, inquiries_repo, send_intents_repo, sender_bindings_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.mail_workers_repo import WorkerIdentity
from suv_deals.persistence.sender_bindings_repo import OAuthRefreshGrant
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.inquiry_handlers import inquiry_runtime
from suv_deals.workers.reconciliation import Reconciler

pytestmark = pytest.mark.db

OTHER_ACCOUNT = "synthetic-other-account"
OTHER_ADDRESS = "other-mailbox@example.invalid"
OTHER_REFRESH = "synthetic-refresh-token-of-another-binding"


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def _process(env: PipelineEnv, reply_id: UUID) -> dict[str, Any]:
    """Run the worker until the reply's processing job finished (see test_reply_escalation)."""
    for _ in range(4):
        await work(env)
        [job] = [
            j
            for j in jobs_of(env, JobType.SELLER_REPLY_PROCESS)
            if j["payload"].get("reply_id") == str(reply_id)
        ]
        if job["state"] == "succeeded":
            return job
        assert job["state"] == "queued", job
        env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (job["id"],))
    raise AssertionError("the reply processing job did not finish")


async def _other_binding(
    env: PipelineEnv,
    *,
    provider: EmailProviderKind,
    sealed: SecretBox | None = None,
    verified: bool = True,
) -> UUID:
    """A SECOND owner-registered sending identity (another account and From address)."""
    actor = env.system

    async def go(conn: Conn) -> UUID:
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=provider,
            account_id=OTHER_ACCOUNT if provider == EmailProviderKind.OUTLOOK_LOCAL else OTHER_ADDRESS,
            from_address=OTHER_ADDRESS,
            display_name="Synthetic Other",
            reason="a second owner-registered sending identity (synthetic)",
        )
        if sealed is not None:
            binding = await sender_bindings_repo.store_secret(
                conn,
                actor,
                binding.id,
                grant=OAuthRefreshGrant(
                    client_id="synthetic-client.apps.example.invalid",
                    client_secret=SecretStr("synthetic-client-secret"),
                    refresh_token=SecretStr(OTHER_REFRESH),
                ),
                box=sealed,
                expected_version=binding.version,
                reason="sealed OAuth grant of the other identity (synthetic)",
            )
        if verified:
            binding = await sender_bindings_repo.record_verification(
                conn,
                actor,
                binding.id,
                expected_version=binding.version,
                verified=True,
                alias_verified=True,
                health="healthy",
                reason="technical verification passed (synthetic)",
            )
        return binding.id

    return await run(env.ctx, actor, go)


# --------------------------------------------------------------------------------------------
# Owner-alert amplification by an adversarial seller
# --------------------------------------------------------------------------------------------

_FRAGMENTS = {
    "payment_request": "Bitte überweisen Sie vorab eine Anzahlung.",
    "reservation_request": "Ich kann das Fahrzeug für Sie reservieren.",
    "appointment_request": "Gerne können Sie zur Besichtigung vorbeikommen.",
}


async def test_seller_cannot_fan_out_owner_alerts_by_varying_requests(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED

    # Every non-empty combination of three consequential requests, one reply each (7 replies).
    reasons = sorted(_FRAGMENTS)
    combos = [c for size in (1, 2, 3) for c in itertools.combinations(reasons, size)]
    for combo in combos:
        body = (
            "Guten Tag, das Fahrzeug ist noch verfügbar. "
            + " ".join(_FRAGMENTS[r] for r in combo)
            + " Synthetic fixture reply."
        )
        reply = await seller_replies(
            env,
            sender.worker,
            inquiry["id"],
            in_reply_to=intent.rfc_message_id,
            from_address=seller.address,
            body=body,
        )
        job = await _process(env, reply.reply_id)
        assert sorted(job["result_reference"]["decisions"]) == list(combo), job["result_reference"]

    alerts = outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)
    # One decision alert per NEW consequential request, never one per combination: at most one
    # alert per distinct reason (here 3), not 7.
    assert len(alerts) == 3, [a["payload"]["reasons"] for a in alerts]
    assert sorted(r for a in alerts for r in a["payload"]["reasons"]) == reasons
    assert all(a["payload"]["kind"] == "decision_needed" for a in alerts)
    # Each reply still produced its own minimal receipt signal; nothing was sent to the seller.
    assert len(outbox_of(env, "seller.reply.received")) == len(combos)
    assert len(attempts_of(env, inquiry["id"])) == 1
    no_approval_state(env)


async def test_a_new_request_in_a_mixed_reply_still_alerts_with_all_its_reasons(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)

    async def reply(*kinds: str) -> dict[str, Any]:
        body = "Guten Tag. " + " ".join(_FRAGMENTS[k] for k in kinds) + " Synthetic fixture reply."
        stored = await seller_replies(
            env,
            sender.worker,  # type: ignore[arg-type]
            inquiry["id"],
            in_reply_to=intent.rfc_message_id,
            from_address=seller.address,
            body=body,
        )
        return await _process(env, stored.reply_id)

    first = await reply("payment_request")
    assert len(first["result_reference"]["owner_alert_event_ids"]) == 1
    second = await reply("payment_request", "reservation_request")
    [event_id] = second["result_reference"]["owner_alert_event_ids"]
    [alert] = [
        a for a in outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE) if str(a["event_id"]) == event_id
    ]
    # The owner sees the whole reply's decisions (context), raised because one of them is new.
    assert alert["payload"]["reasons"] == ["payment_request", "reservation_request"]
    third = await reply("reservation_request")
    assert third["result_reference"]["owner_alert_event_ids"] == []
    assert len(outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)) == 2


# --------------------------------------------------------------------------------------------
# outlook_local: only the configured sending identity
# --------------------------------------------------------------------------------------------


async def test_outlook_plan_binds_only_the_configured_sender(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)  # the configured identity (SELLER_EMAIL_ACCOUNT_ID/FROM)
    assert sender.worker is not None
    # A newer, verified outlook_local identity the configuration does NOT name.
    other = await _other_binding(env, provider=EmailProviderKind.OUTLOOK_LOCAL)
    listing = await eligible_live_listing(env)
    await link_seller(env, listing)
    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [inquiry] = inquiries_of(env)
    assert inquiry["sender_binding_id"] == sender.binding_id != other
    assert inquiry["state"] == InquiryState.QUEUED
    await work(env, JobType.SELLER_INQUIRY_SEND)
    [intent] = (await pending_intents(env, sender.worker)).intents
    assert intent.inquiry_id == inquiry["id"]


async def test_outlook_plan_refuses_when_no_binding_is_the_configured_sender(env: PipelineEnv) -> None:
    misconfigured = with_settings(
        env,
        runtime_settings(
            _db_url(env), seller_email_account_id=OTHER_ACCOUNT, seller_email_from=OTHER_ADDRESS
        ),
    )
    sender = await prepare_sender(misconfigured)  # a verified identity, but not the configured one
    assert sender.worker is not None
    listing = await eligible_live_listing(misconfigured)
    await link_seller(misconfigured, listing)
    await work(misconfigured)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "succeeded", plan
    assert plan["result_reference"]["outcome"] == "readiness_recorded"
    assert plan["result_reference"]["reservation"] == "sender_identity_not_configured"
    [inquiry] = inquiries_of(env)
    assert inquiry["sender_binding_id"] is None
    assert debits_of(env) == 0 and jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []
    assert (await pending_intents(env, sender.worker)).intents == ()
    no_approval_state(env)


async def test_queued_inquiry_of_a_no_longer_configured_sender_is_never_published(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    await link_seller(env, listing)
    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and inquiry["sender_binding_id"] == sender.binding_id
    # The owner reconfigured the process to another sending identity before the send ran.
    reconfigured = with_settings(
        env,
        runtime_settings(
            _db_url(env), seller_email_account_id=OTHER_ACCOUNT, seller_email_from=OTHER_ADDRESS
        ),
    )
    await work(reconfigured, JobType.SELLER_INQUIRY_SEND)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == "SENDER_SETUP_INCOMPLETE", send
    assert (await pending_intents(env, sender.worker)).intents == ()
    assert attempts_of(env, inquiry["id"]) == []
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED  # nothing transmitted, nothing lost


# --------------------------------------------------------------------------------------------
# gmail_api: never another binding's sealed grant
# --------------------------------------------------------------------------------------------


class _RecordingGmail:
    """The Gmail double, plus the refresh tokens presented at the token endpoint."""

    def __init__(self, api: GmailApi) -> None:
        self.api = api
        self.refresh_tokens: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/token"):
            form = dict(httpx.QueryParams(request.content.decode("ascii")))
            self.refresh_tokens.append(form.get("refresh_token", ""))
        return self.api(request)


async def test_api_send_never_borrows_another_bindings_sealed_grant(env: PipelineEnv) -> None:
    sealed = box()
    # The OTHER identity (created first) holds a sealed grant; the process-wide secret reference
    # names it instead of the configured, bound sender.
    other = await _other_binding(env, provider=EmailProviderKind.GMAIL_API, sealed=sealed, verified=False)
    sender = await prepare_sender(env, provider=EmailProviderKind.GMAIL_API, box=sealed)
    api = GmailApi()
    recorder = _RecordingGmail(api)
    live = with_settings(env, gmail_settings(_db_url(env), other))
    rt = inquiry_runtime(live.ctx)
    rt.http_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    rt.secret_box = sealed
    live.ctx.add_closer(rt.http_client.aclose)

    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live)
    [inquiry] = inquiries_of(env)
    assert inquiry["sender_binding_id"] == sender.binding_id
    assert OTHER_REFRESH not in recorder.refresh_tokens  # the other binding's grant is never opened
    assert api.sent == []  # nothing reached Gmail
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == "SENDER_SETUP_INCOMPLETE", send
    detail = env.scalar("select blocker_detail from ops.jobs where id = %s", send["id"])
    assert detail == "SECRET_REFERENCE_BINDING_MISMATCH"  # a code, never a value
    assert attempts_of(env, inquiry["id"]) == []
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED


# --------------------------------------------------------------------------------------------
# outlook_local: a proven pre-submission refusal at the desktop claim is retried (guarded)
# --------------------------------------------------------------------------------------------


async def _worker_refuses(env: PipelineEnv, worker: WorkerIdentity, intent: OutlookSendIntent) -> None:
    """The desktop worker's claim is refused (kill switch) and it reports a refusal BEFORE .Send."""
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        claim = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="claim-refused",
            worker_id="synthetic-desktop-1",
            request_id=WORKER_REQ,
        )
    assert not claim.proceed and claim.refusal_reason == OutlookRefusalReason.KILL_SWITCH
    report = OutlookSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=intent.mailbox_binding_id,
        worker_id="synthetic-desktop-1",
        state=OutlookSubmissionState.REFUSED_BEFORE_SEND,
        refusal_reason=OutlookRefusalReason.KILL_SWITCH,
        account_smtp_address_used=SENDER_ADDRESS,
        sent_items_present=False,
        reported_at=now_utc(),
    )
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        await send_intents_repo.report(conn, worker, report=report, request_id=WORKER_REQ)


async def test_refused_outlook_claim_is_retried_after_resume_not_lost(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    await link_seller(env, listing)
    await work(env)
    [first] = (await pending_intents(env, sender.worker)).intents
    await pull_kill_switch(env)  # the owner pauses after the intent was published
    await _worker_refuses(env, sender.worker, first)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.FAILED_DEFINITE  # proven never handed to Outlook

    # While paused, nothing is retried or published.
    await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    await work(env)
    assert (await pending_intents(env, sender.worker)).intents == ()
    assert [a["attempt_number"] for a in attempts_of(env, inquiry["id"])] == [1]

    async def resume(conn: Conn) -> None:
        controls = await inquiries_repo.get_controls(conn, env.owner)
        assert controls is not None
        await inquiries_repo.resume(conn, env.owner, expected_version=controls.version, reason="resume")

    await run(env.ctx, env.owner, resume)
    for job in jobs_of(env, JobType.SELLER_INQUIRY_SEND):
        if job["state"] == "queued":
            env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (job["id"],))
    await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    await work(env)
    # The guarded retry (proven pre-submission failure, same account) publishes ONE new intent.
    [second] = (await pending_intents(env, sender.worker)).intents
    assert second.intent_id != first.intent_id and second.inquiry_id == inquiry["id"]
    assert [a["attempt_number"] for a in attempts_of(env, inquiry["id"])] == [1, 2]
    await worker_sends(env, sender.worker, second)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    # Nothing more is ever scheduled for it.
    await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    await work(env)
    assert (await pending_intents(env, sender.worker)).intents == ()
    assert len(attempts_of(env, inquiry["id"])) == 2
    no_approval_state(env)


# --------------------------------------------------------------------------------------------
# outlook_local: a revoked (stolen) worker credential never receives a send intent
# --------------------------------------------------------------------------------------------


async def test_revoked_worker_credential_holds_the_send_despite_a_fresh_heartbeat(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    worker = sender.worker  # its heartbeat is fresh (seconds old)

    async def revoke(conn: Conn) -> None:
        assert await credentials_repo.revoke_credential(
            conn, env.owner, worker.credential_id, reason="credential reported stolen (synthetic)"
        )

    await run(env.ctx, env.owner, revoke)
    listing = await eligible_live_listing(env)
    await link_seller(env, listing)
    await work(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED
    # Nothing was published for a mailbox whose credential no longer speaks for it: no attempt,
    # no intent (an unclaimable intent would expire into an uncertain, never-resolvable send).
    assert attempts_of(env, inquiry["id"]) == []
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued" and send["last_error_code"] == "MAILBOX_WORKER_CREDENTIAL_NOT_LIVE", send
    no_approval_state(env)


# --------------------------------------------------------------------------------------------
# Seller replies cannot flood the audit trail while a recalculation is pending
# --------------------------------------------------------------------------------------------


async def test_waiting_for_the_recalculation_writes_no_audit_event_per_poll(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    stored = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body="Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply.",
    )
    # Only the reply job runs: the recalculation the ingest queued stays pending, so each run
    # of the reply job is a poll that releases it (no attempt consumed).
    for _ in range(3):
        await work(env, JobType.SELLER_REPLY_PROCESS)
        [job] = [
            j
            for j in jobs_of(env, JobType.SELLER_REPLY_PROCESS)
            if j["payload"].get("reply_id") == str(stored.reply_id)
        ]
        assert job["state"] == "queued" and job["last_error_code"] == "RECALCULATION_PENDING", job
        env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (job["id"],))
    polls = env.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'job.release'"
        " and target_id = %s",
        env.workspace_id,
        job["id"],
    )
    assert polls == 0  # the reason stays visible on the job; no audit row per poll
    assert job["attempts"] == 0


async def test_missing_secret_box_key_blocks_visibly_instead_of_dead_letter_churn(env: PipelineEnv) -> None:
    sealed = box()
    sender = await prepare_sender(env, provider=EmailProviderKind.GMAIL_API, box=sealed)
    api = GmailApi()
    live = with_settings(
        env,
        gmail_settings(_db_url(env), sender.binding_id, mcp_event_subscription_secret_encryption_key=None),
    )
    rt = inquiry_runtime(live.ctx)
    rt.http_client = httpx.AsyncClient(transport=httpx.MockTransport(api))  # no secret box injected
    live.ctx.add_closer(rt.http_client.aclose)
    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == "SENDER_SETUP_INCOMPLETE", send
    assert (
        env.scalar("select blocker_detail from ops.jobs where id = %s", send["id"])
        == "SECRET_BOX_NOT_CONFIGURED"
    )
    assert api.calls == []  # nothing reached Google
    # A visible blocker, not a dead letter the orphan sweep would replace every pass.
    await Reconciler(live.ctx).reconcile_workspace(env.workspace_id)
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_SEND)) == 1
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and attempts_of(env, inquiry["id"]) == []
