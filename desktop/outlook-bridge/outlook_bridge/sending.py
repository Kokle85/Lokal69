"""``outlook_local`` send intents: pull, revalidate, submit once, report evidence (spec 37.5/37.6).

The backend decides *whether* an inquiry is sent (bounded standing authorization, readiness,
recipient/language evidence, dedup reservation, caps, suppression, kill switch). This worker only
executes a validated intent through the bound classic-Outlook account, under these rules:

- The intent is recorded locally before anything else; an intent id is never attempted twice
  (a crash after ``attempting`` is resolved by looking for Sent Items/Outbox evidence and is
  otherwise reported as an uncertain ``send_call_failed`` - never re-sent, never sent from another
  account).
- Local refusals before any ``.Send``: expired intent, another mailbox binding, a sender address
  that is not the bound Outlook account, the kill switch, a failed integrity check (body hash,
  header/markup injection, one canonical recipient) or Outlook not being classic.
- Immediately before transmission the server re-validates the intent (``claim``); a transport
  failure of the claim means nothing is sent now.
- Local defence-in-depth ceilings equal the spec's initial workspace caps (2 per rolling 24 h,
  5 per rolling 15 days). They are ceilings, never targets: above them an intent simply waits
  (and may expire); the server's transactional caps remain authoritative.
- ``.Send`` success is reported as ``submitted_to_outbox`` (local submission only); Sent Items
  evidence is looked up later and reported as ``sent_items_confirmed``. No SMTP/provider receipt
  is fabricated.

No follow-ups, replies, offers or any other message: the worker can only send what an intent
contains, and an intent contains exactly one plain-text inquiry.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from outlook_bridge.api_client import BridgeApiClient, BridgeApiError
from outlook_bridge.config import BridgeConfig
from outlook_bridge.errors import CredentialError, OutlookUnavailable, StaError
from outlook_bridge.local_queue import IntentRow, IntentState, LocalStore
from outlook_bridge.log import event, get_logger
from outlook_bridge.outlook_adapter import MailboxAdapter, OutgoingMail, SentLookup
from outlook_bridge.wire import (
    RefusalReason,
    SubmissionState,
    Tristate,
    WorkerSendIntent,
    WorkerSendReport,
    intent_integrity_problems,
)

LOCAL_CEILING_24H: Final = 2
LOCAL_CEILING_15D: Final = 5
EVIDENCE_MARGIN: Final = timedelta(minutes=10)
EVIDENCE_WINDOW: Final = timedelta(hours=72)
_LOG = get_logger("sending")
_SUBMIT_REFUSALS: Final[dict[str, RefusalReason]] = {
    "account_mismatch": RefusalReason.ACCOUNT_MISMATCH,
    "intent_invalid": RefusalReason.INTENT_INVALID,
    "mailbox_unavailable": RefusalReason.MAILBOX_UNAVAILABLE,
}


class SendIntentProcessor:
    def __init__(
        self,
        *,
        config: BridgeConfig,
        store: LocalStore,
        mailbox: MailboxAdapter,
        api: BridgeApiClient,
        outlook_is_classic: Callable[[], bool],
        account_smtp: Callable[[], str | None],
        on_api_error: Callable[[BridgeApiError | CredentialError], None] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._mailbox = mailbox
        self._api = api
        self._classic = outlook_is_classic
        self._account_smtp = account_smtp
        self._on_api_error = on_api_error

    def _api_failed(self, exc: BridgeApiError | CredentialError) -> None:
        """Let the worker see 401/credential/outage failures (transmission stop, health)."""
        if self._on_api_error is not None:
            self._on_api_error(exc)

    # ------------------------------------------------------------------ reports

    def _report(
        self,
        intent: WorkerSendIntent,
        state: SubmissionState,
        *,
        now: datetime,
        refusal: RefusalReason | None = None,
        account_used: str | None = None,
        observed_message_id: str | None = None,
        outbox_pending: Tristate = Tristate.UNKNOWN,
        error_code: str | None = None,
        sent_at: datetime | None = None,
    ) -> WorkerSendReport:
        return WorkerSendReport(
            intent_id=intent.intent_id,
            inquiry_id=intent.inquiry_id,
            mailbox_binding_id=self._config.mailbox_binding_id,
            worker_id=self._config.worker_id,
            state=state,
            refusal_reason=refusal,
            account_smtp_address_used=account_used,
            observed_internet_message_id=observed_message_id,
            outbox_pending=outbox_pending,
            sent_items_present=state == SubmissionState.SENT_ITEMS_CONFIRMED,
            error_code=error_code,
            reported_at=now,
            sent_at=sent_at,
        )

    def _finish(
        self, intent: WorkerSendIntent, state: IntentState, report: WorkerSendReport, *, now: datetime
    ) -> None:
        self._store.finish_intent(
            intent.intent_id,
            state=state,
            report_json=report.model_dump_json(),
            now=now,
            error_code=report.error_code or (report.refusal_reason.value if report.refusal_reason else None),
            observed_message_id=report.observed_internet_message_id,
        )
        event(_LOG, "send_intent_" + state.value, intent_id=intent.intent_id, report_state=report.state)

    def push_reports(self) -> int:
        pushed = 0
        for row in self._store.intents():
            if row.report_json is None or row.report_acked:
                continue
            report = WorkerSendReport.model_validate_json(row.report_json)
            try:
                self._api.post_send_report(report)
            except (BridgeApiError, CredentialError) as exc:
                self._api_failed(exc)
                break  # the report stays stored and is re-sent later (idempotent per state)
            self._store.mark_report_acked(row.intent_id, row.report_json)
            pushed += 1
        return pushed

    # ------------------------------------------------------------------ evidence

    def _lookup(self, row: IntentRow) -> SentLookup | None:
        intent = WorkerSendIntent.model_validate_json(row.payload_json)
        since = (row.attempt_started_at or row.received_at) - EVIDENCE_MARGIN
        try:
            return self._mailbox.lookup_sent(OutgoingMail.from_intent(intent), since=since)
        except (OutlookUnavailable, StaError):
            return None

    def recover_and_confirm(self, now: datetime) -> Counter[str]:
        """Resolve interrupted attempts and look for Sent Items evidence of submitted ones."""
        counts: Counter[str] = Counter()
        for row in self._store.intents(
            [IntentState.ATTEMPTING, IntentState.SUBMITTED, IntentState.SEND_FAILED]
        ):
            started = row.attempt_started_at or row.received_at
            if row.state != IntentState.ATTEMPTING and now - started > EVIDENCE_WINDOW:
                continue
            lookup = self._lookup(row)
            if lookup is None:
                counts["outlook_unavailable"] += 1
                continue
            intent = WorkerSendIntent.model_validate_json(row.payload_json)
            if lookup.location == "sent_items":
                report = self._report(
                    intent,
                    SubmissionState.SENT_ITEMS_CONFIRMED,
                    now=now,
                    account_used=self._account_smtp(),
                    observed_message_id=lookup.internet_message_id,
                    outbox_pending=Tristate.NO,
                    sent_at=lookup.sent_at,
                )
                self._finish(intent, IntentState.CONFIRMED, report, now=now)
                counts["confirmed"] += 1
            elif row.state == IntentState.ATTEMPTING:
                if lookup.location == "outbox":
                    report = self._report(
                        intent, SubmissionState.SUBMITTED_TO_OUTBOX, now=now, outbox_pending=Tristate.YES
                    )
                    self._finish(intent, IntentState.SUBMITTED, report, now=now)
                else:
                    # Interrupted between "attempting" and the result: uncertain, never re-sent.
                    report = self._report(
                        intent, SubmissionState.SEND_CALL_FAILED, now=now, error_code="WORKER_INTERRUPTED"
                    )
                    self._finish(intent, IntentState.SEND_FAILED, report, now=now)
                counts["recovered"] += 1
            elif row.state == IntentState.SEND_FAILED and lookup.location == "outbox":
                report = self._report(
                    intent, SubmissionState.SUBMITTED_TO_OUTBOX, now=now, outbox_pending=Tristate.YES
                )
                self._finish(intent, IntentState.SUBMITTED, report, now=now)
                counts["queued_after_failure"] += 1
        return counts

    # ------------------------------------------------------------------ intake

    def _local_refusal(
        self, intent: WorkerSendIntent, *, kill_switch: bool, now: datetime
    ) -> RefusalReason | None:
        account = self._account_smtp()
        if kill_switch:
            return RefusalReason.KILL_SWITCH
        if intent.expired(now):
            return RefusalReason.INTENT_EXPIRED
        if intent.mailbox_binding_id != self._config.mailbox_binding_id:
            return RefusalReason.BINDING_MISMATCH
        if self._store.inquiry_attempted_elsewhere(intent.inquiry_id, intent_id=intent.intent_id):
            # An earlier intent of this inquiry may already have been submitted: never a second
            # message from here (the backend reconciles the uncertain send instead).
            return RefusalReason.DUPLICATE_INTENT
        if (
            intent.from_address.casefold() != self._config.account_smtp_address.casefold()
            or account is None
            or account.casefold() != intent.from_address.casefold()
        ):
            return RefusalReason.ACCOUNT_MISMATCH
        if intent_integrity_problems(intent):
            return RefusalReason.INTENT_INVALID
        if not self._classic():
            return RefusalReason.OUTLOOK_NOT_CLASSIC
        return None

    @staticmethod
    def _ceilings(now: datetime) -> tuple[tuple[datetime, int], ...]:
        return ((now - timedelta(hours=24), LOCAL_CEILING_24H), (now - timedelta(days=15), LOCAL_CEILING_15D))

    def _ceiling_reached(self, now: datetime) -> bool:
        return any(self._store.attempted_since(since) >= limit for since, limit in self._ceilings(now))

    def run_once(self, now: datetime) -> Counter[str]:
        counts = self.recover_and_confirm(now)
        self.push_reports()
        try:
            batch = self._api.fetch_send_intents(limit=10)
        except (BridgeApiError, CredentialError) as exc:
            self._api_failed(exc)
            counts["fetch_failed"] += 1
            return counts
        for intent in batch.intents:
            counts[self._handle(intent, kill_switch=batch.kill_switch_active, now=now)] += 1
        expired = self.expire_waiting(now, offered={i.intent_id for i in batch.intents})
        if expired:
            counts["expired"] += expired
        self.push_reports()
        return counts

    def expire_waiting(self, now: datetime, *, offered: set[UUID] | frozenset[UUID] = frozenset()) -> int:
        """Close locally waiting intents whose validity ended (never sent; reported honestly)."""
        expired = 0
        for row in self._store.intents([IntentState.RECEIVED]):
            if row.intent_id in offered:
                continue
            intent = WorkerSendIntent.model_validate_json(row.payload_json)
            if not intent.expired(now):
                continue
            report = self._report(
                intent, SubmissionState.REFUSED_BEFORE_SEND, now=now, refusal=RefusalReason.INTENT_EXPIRED
            )
            self._finish(intent, IntentState.REFUSED, report, now=now)
            expired += 1
        return expired

    def _handle(self, intent: WorkerSendIntent, *, kill_switch: bool, now: datetime) -> str:
        existing = self._store.intent(intent.intent_id)
        if existing is None:
            self._store.record_intent_received(
                intent_id=intent.intent_id,
                inquiry_id=intent.inquiry_id,
                payload_json=intent.model_dump_json(),
                now=now,
            )
        elif existing.state != IntentState.RECEIVED:
            return "already_handled"  # the stored report (re)tells the outcome; never a 2nd attempt
        elif existing.payload_json != intent.model_dump_json():
            report = self._report(
                intent,
                SubmissionState.REFUSED_BEFORE_SEND,
                now=now,
                refusal=RefusalReason.INTENT_INVALID,
                error_code="INTENT_CHANGED",
            )
            self._finish(intent, IntentState.REFUSED, report, now=now)
            return "refused"
        if self._account_smtp() is None and not kill_switch and not intent.expired(now):
            return "deferred_outlook_unavailable"  # nothing sent; retried while the intent is valid
        refusal = self._local_refusal(intent, kill_switch=kill_switch, now=now)
        if refusal is not None:
            report = self._report(intent, SubmissionState.REFUSED_BEFORE_SEND, now=now, refusal=refusal)
            self._finish(intent, IntentState.REFUSED, report, now=now)
            return "refused"
        if self._ceiling_reached(now):
            return "deferred_ceiling"
        try:
            decision = self._api.claim_send_intent(intent.intent_id)
        except (BridgeApiError, CredentialError) as exc:
            self._api_failed(exc)
            return "claim_unavailable"  # nothing sent; retried while the intent is valid
        if not decision.proceed:
            report = self._report(
                intent,
                SubmissionState.REFUSED_BEFORE_SEND,
                now=now,
                refusal=decision.refusal_reason or RefusalReason.INTENT_INVALID,
            )
            self._finish(intent, IntentState.REFUSED, report, now=now)
            return "refused"
        # The duplicate and ceiling checks are repeated atomically with the "attempting" commit
        # (another worker process on this store may have attempted something meanwhile).
        if not self._store.begin_attempt(
            intent.intent_id, now, inquiry_id=intent.inquiry_id, ceilings=self._ceilings(now)
        ):
            current = self._store.intent(intent.intent_id)
            if current is None or current.state != IntentState.RECEIVED:
                return "already_handled"
            if self._store.inquiry_attempted_elsewhere(intent.inquiry_id, intent_id=intent.intent_id):
                report = self._report(
                    intent,
                    SubmissionState.REFUSED_BEFORE_SEND,
                    now=now,
                    refusal=RefusalReason.DUPLICATE_INTENT,
                )
                self._finish(intent, IntentState.REFUSED, report, now=now)
                return "refused"
            return "deferred_ceiling"
        try:
            result = self._mailbox.submit(OutgoingMail.from_intent(intent))
        except (OutlookUnavailable, StaError) as exc:
            report = self._report(
                intent, SubmissionState.SEND_CALL_FAILED, now=now, error_code=f"SUBMIT_{type(exc).__name__}"
            )
            self._finish(intent, IntentState.SEND_FAILED, report, now=now)
            return "send_call_failed"
        if result.outcome == "refused":
            report = self._report(
                intent,
                SubmissionState.REFUSED_BEFORE_SEND,
                now=now,
                refusal=_SUBMIT_REFUSALS.get(result.refusal or "", RefusalReason.INTENT_INVALID),
                account_used=result.account_used,
                error_code=result.error_code,
            )
            self._finish(intent, IntentState.REFUSED, report, now=now)
            return "refused"
        if result.outcome == "send_call_failed":
            report = self._report(
                intent,
                SubmissionState.SEND_CALL_FAILED,
                now=now,
                account_used=result.account_used,
                error_code=result.error_code,
            )
            self._finish(intent, IntentState.SEND_FAILED, report, now=now)
            return "send_call_failed"
        report = self._report(
            intent,
            SubmissionState.SUBMITTED_TO_OUTBOX,
            now=now,
            account_used=result.account_used,
            outbox_pending=Tristate.YES,
        )
        self._finish(intent, IntentState.SUBMITTED, report, now=now)
        return "submitted"


__all__ = ["EVIDENCE_WINDOW", "LOCAL_CEILING_15D", "LOCAL_CEILING_24H", "SendIntentProcessor"]
