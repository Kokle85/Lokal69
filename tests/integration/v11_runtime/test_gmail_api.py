"""The ``gmail_api`` send route with a ``httpx.MockTransport`` Gmail (spec 37.5; test (f)).

- success: the stored rendering is rebuilt by ``mime_builder``, handed to Gmail OUTSIDE any
  transaction with the ``BindingTokenProvider`` (sealed grant -> token endpoint double), and the
  attempt is ``accepted``;
- a timeout after the request was written is ``uncertain``: the send job is blocked with
  ``EMAIL_DELIVERY_UNCERTAIN``, the inquiry waits, and nothing is ever resent;
- reconciliation by Message-ID: a found SENT copy -> ``accepted``; nothing found -> stays
  ``uncertain`` (an empty search never releases the reservation) and is still never resent;
- a PROVEN pre-submission failure (the token endpoint refused before any Gmail request) follows
  ``domain.inquiries.should_retry``: the same job retries through the guarded repository retry,
  with the same account, and the second attempt is accepted (one message reaches Gmail).
"""

from __future__ import annotations

import pytest
from tests.integration.pipeline.support import PipelineEnv
from tests.integration.v11_runtime.support import (
    GmailApi,
    age_uncertainty,
    attach_gmail,
    attempts_of,
    box,
    debits_of,
    eligible_live_listing,
    expire_job_wait,
    gmail_settings,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    prepare_sender,
    with_settings,
    work,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryState, JobType
from suv_deals.workers.reconciliation import Reconciler

pytestmark = pytest.mark.db


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def _gmail_env(env: PipelineEnv, api: GmailApi) -> PipelineEnv:
    sealed = box()
    sender = await prepare_sender(env, provider=EmailProviderKind.GMAIL_API, box=sealed)
    live = with_settings(env, gmail_settings(_db_url(env), sender.binding_id))
    return attach_gmail(live, api, sealed)


async def test_gmail_send_is_accepted(env: PipelineEnv) -> None:
    api = GmailApi()
    live = await _gmail_env(env, api)
    listing = await eligible_live_listing(live)
    seller = await link_seller(live, listing)
    await work(live)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "succeeded", send
    assert send["result_reference"]["outcome"] == "accepted"
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["provider"] == "gmail_api" and attempt["outcome"] == "accepted"
    assert len(api.sent) == 1
    assert api.message_id() == attempt["rfc_message_id"]
    # The refresh token and the access token never reach the job result or the attempt row.
    assert "synthetic-access-token" not in str(send) and "synthetic-refresh-token" not in str(attempt)
    assert seller.address not in str(send["result_reference"])
    assert debits_of(env) == 1
    no_approval_state(env)


async def _uncertain(env: PipelineEnv, api: GmailApi) -> tuple[PipelineEnv, dict[str, object]]:
    live = await _gmail_env(env, api)
    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == "EMAIL_DELIVERY_UNCERTAIN", send
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["outcome"] == "uncertain"
    assert len(api.sent) == 1
    return live, inquiry


async def test_timeout_is_uncertain_and_reconciled_by_message_id(env: PipelineEnv) -> None:
    api = GmailApi(send_mode="timeout", search_finds=True)
    live, inquiry = await _uncertain(env, api)

    # The reconciliation pass schedules a reconcile job once the uncertainty is old enough.
    reconciler = Reconciler(live.ctx)
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_reconcile_jobs == 0
    age_uncertainty(env, inquiry["id"], minutes=60)  # type: ignore[arg-type]
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_reconcile_jobs == 1
    await work(live, JobType.SELLER_INQUIRY_RECONCILE, JobType.SELLER_INQUIRY_SEND)
    [job] = jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)
    assert job["state"] == "succeeded" and job["result_reference"]["outcome"] == "accepted", job
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["reconciled_outcome"] == "accepted"
    assert len(api.sent) == 1  # never resent
    assert debits_of(env) == 1


async def test_uncertain_send_not_found_stays_uncertain_and_is_never_resent(env: PipelineEnv) -> None:
    api = GmailApi(send_mode="timeout", search_finds=False)
    live, inquiry = await _uncertain(env, api)
    age_uncertainty(env, inquiry["id"], minutes=60)  # type: ignore[arg-type]
    await Reconciler(live.ctx).reconcile_workspace(env.workspace_id)
    await work(live)
    [job] = jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)
    assert job["state"] == "succeeded" and job["result_reference"]["outcome"] == "still_uncertain", job
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN
    assert debits_of(env) == 1  # an empty search never releases the reservation
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked"  # no new send job, no second transmission
    assert len(api.sent) == 1 and len(attempts_of(env, inquiry["id"])) == 1
    # The next pass in the same hour does not schedule another reconcile job.
    assert (await Reconciler(live.ctx).reconcile_workspace(env.workspace_id)).inquiry_reconcile_jobs == 0
    no_approval_state(env)


async def test_proven_pre_submission_failure_is_retried_through_the_guard(env: PipelineEnv) -> None:
    api = GmailApi(token_failures=1)
    live = await _gmail_env(env, api)
    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "retry_wait" and send["last_error_code"] == "PRE_SUBMISSION_FAILURE", send
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.FAILED_DEFINITE
    [first] = attempts_of(env, inquiry["id"])
    assert first["outcome"] == "pre_submission_failure" and api.sent == []
    assert debits_of(env) == 1  # the reservation and its debit stay

    expire_job_wait(env, send["id"])
    await work(live, JobType.SELLER_INQUIRY_SEND)
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "succeeded" and send["result_reference"]["outcome"] == "accepted", send
    attempts = attempts_of(env, inquiry["id"])
    assert [a["outcome"] for a in attempts] == ["pre_submission_failure", "accepted"]
    assert [a["attempt_number"] for a in attempts] == [1, 2]
    assert len(api.sent) == 1  # exactly one message ever reached the provider
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    assert debits_of(env) == 1
    no_approval_state(env)
