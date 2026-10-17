"""Operator guard and the no-approval policy (spec 37.1, 37.5; tests (h) and (j)).

- (h) ``jobs.unblock`` refuses a send job blocked with ``EMAIL_DELIVERY_UNCERTAIN`` unless the
  operator acknowledges explicitly (audited); even an acknowledged unblock never transmits a
  second e-mail (the inquiry is no longer ``queued``);
- (j) there is no approval state or approval wait anywhere: no enum value, no job code, no
  handler path; the standing authorization reserves and dispatches without a human click, and
  the owner's explicit ``SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL`` setting DISABLES automatic
  sending (readiness recorded, nothing queued) instead of creating a wait.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from tests.integration.pipeline.support import PipelineEnv, run
from tests.integration.v11_runtime.support import (
    GmailApi,
    attach_gmail,
    attempts_of,
    box,
    debits_of,
    eligible_live_listing,
    gmail_settings,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    prepare_sender,
    runtime_settings,
    with_settings,
    work,
)

from suv_deals.domain.enums import (
    EmailProviderKind,
    InquiryReadiness,
    InquiryState,
    JobState,
    JobType,
    OutboxState,
)
from suv_deals.errors import EmailDeliveryUncertain
from suv_deals.persistence import jobs
from suv_deals.persistence.database import Conn
from suv_deals.settings import Settings
from suv_deals.workers import dispatcher, handlers, inquiry_handlers, reconciliation, reply_handlers

pytestmark = pytest.mark.db

_APPROVAL_WAIT_WORDS = ("awaiting_approval", "approval_required", "pending_approval", "needs_approval")


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


# --------------------------------------------------------------------------------------------
# (h) unblock of an uncertain send
# --------------------------------------------------------------------------------------------


async def test_unblock_of_an_uncertain_send_needs_an_explicit_acknowledgement(env: PipelineEnv) -> None:
    sealed = box()
    api = GmailApi(send_mode="timeout")
    sender = await prepare_sender(env, provider=EmailProviderKind.GMAIL_API, box=sealed)
    live = attach_gmail(with_settings(env, gmail_settings(_db_url(env), sender.binding_id)), api, sealed)
    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == jobs.EMAIL_DELIVERY_UNCERTAIN
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN
    owner = env.owner

    async def plain(conn: Conn) -> object:
        return await jobs.unblock(conn, owner, send["id"], reason="operator wants to retry")

    with pytest.raises(EmailDeliveryUncertain) as refused:
        await run(env.ctx, owner, plain)
    assert (refused.value.details or {}).get("reason") == "uncertain_delivery_ack_required"
    [still] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert still["state"] == "blocked" and still["blocker_code"] == jobs.EMAIL_DELIVERY_UNCERTAIN
    assert (
        env.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s and action = 'job.unblock'",
            env.workspace_id,
        )
        == 0
    )

    async def acknowledged(conn: Conn) -> jobs.JobRecord:
        return await jobs.unblock(
            conn,
            owner,
            send["id"],
            reason="checked the Gmail Sent folder; reconcile instead of resending",
            acknowledge_uncertain_delivery=True,
        )

    unblocked = await run(env.ctx, owner, acknowledged)
    assert unblocked.state == JobState.QUEUED
    [event] = env.rows(
        "select metadata from ops.audit_events where workspace_id = %s and action = 'job.unblock'",
        env.workspace_id,
    )
    assert event["metadata"]["acknowledged_uncertain_delivery"] is True
    assert event["metadata"]["blocker_code"] == jobs.EMAIL_DELIVERY_UNCERTAIN

    # The acknowledged job runs, but the inquiry is no longer queued: never a second message.
    await work(live, JobType.SELLER_INQUIRY_SEND)
    [done] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert done["state"] == "succeeded" and done["result_reference"]["outcome"] == "not_queued"
    assert len(api.sent) == 1 and len(attempts_of(env, inquiry["id"])) == 1
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN and debits_of(env) == 1


# --------------------------------------------------------------------------------------------
# (j) no approval state or approval wait anywhere
# --------------------------------------------------------------------------------------------


def test_no_enum_or_default_setting_expresses_an_approval_wait() -> None:
    for enum in (InquiryState, InquiryReadiness, JobState, JobType, OutboxState):
        assert not [m.value for m in enum if "approv" in m.value], enum
    assert Settings.model_fields["seller_inquiry_require_message_approval"].default is False
    # The v1.1 job types are exactly the four runtime handlers: no approval job type exists.
    registry = handlers.default_registry()
    v11 = {
        JobType.SELLER_INQUIRY_PLAN,
        JobType.SELLER_INQUIRY_SEND,
        JobType.SELLER_INQUIRY_RECONCILE,
        JobType.SELLER_REPLY_PROCESS,
    }
    assert v11 <= set(registry.job_types())


@pytest.mark.parametrize("module", [inquiry_handlers, reply_handlers, dispatcher, reconciliation])
def test_no_handler_code_path_produces_an_approval_wait(module: object) -> None:
    """Every string a v1.1 runtime module can write (job codes, results, reasons, blockers) is
    free of approval-wait vocabulary; the only approval reference is the owner's explicit
    setting passed through to the dispatch (default ``False``)."""
    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    literals = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    for literal in literals:
        assert not any(word in literal for word in _APPROVAL_WAIT_WORDS), literal
    path = Path(inspect.getfile(module))  # type: ignore[arg-type]
    assert path.suffix == ".py"


async def test_standing_authorization_needs_no_click_and_the_owner_setting_disables(
    env: PipelineEnv,
) -> None:
    # The owner's explicit setting: automatic sending is OFF (no wait, no approval queue).
    strict = with_settings(env, runtime_settings(_db_url(env), seller_inquiry_require_message_approval=True))
    sender = await prepare_sender(strict)
    assert sender.worker is not None
    listing = await eligible_live_listing(strict)
    await link_seller(strict, listing)
    await work(strict)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["state"] == "succeeded"
    assert plan["result_reference"]["outcome"] == "readiness_recorded"
    assert plan["result_reference"]["reservation"] == "automatic_sending_disabled_by_owner_setting"
    assert debits_of(env) == 0 and jobs_of(env, JobType.SELLER_INQUIRY_SEND) == []
    assert not [j for j in jobs_of(env, JobType.SELLER_INQUIRY_PLAN) if j["state"] != "succeeded"]

    # Default configuration: the recheck reserves and publishes without any human action.
    default = with_settings(env, runtime_settings(_db_url(env)))
    async with default.ctx.db.transaction(default.system) as conn:
        job = await inquiry_handlers.enqueue_plan_job(
            conn,
            default.system,
            listing_id=listing["id"],
            revision_id=listing["current_revision_id"],
            reason="test",
            suffix="default-settings",
        )
    assert job is not None
    await work(default)
    outcomes = [j["result_reference"]["outcome"] for j in jobs_of(env, JobType.SELLER_INQUIRY_PLAN)]
    assert outcomes == ["readiness_recorded", "reserved"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["result_reference"]["outcome"] == "intent_published"
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING and inquiry["readiness"] == "inquiry_ready"
    no_approval_state(env)
