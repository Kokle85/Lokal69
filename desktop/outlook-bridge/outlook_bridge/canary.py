"""The ``outlook_local`` activation canary on the desktop (spec 37.10; F3, wave D2).

The owner's ``suv-deals canary send`` publishes ONE claimed canary to this mailbox worker. The
worker:

1. pulls it (``GET /canary-intents``) and records it locally before anything else;
2. refuses it locally (reported ``refused_before_send``; the canary fails, nothing was sent) when
   it expired, names another mailbox, another sender account than the bound Outlook account, a
   malformed sender identity, when Outlook is not classic, or when ``canary_target_address`` is
   not configured on THIS machine, does not hash to the canary's target hash, or is the sending
   account itself. The target address never comes from the backend (it only knows the hash);
3. claims it immediately before ``.Send`` (always fresh; one worker id only) and sends the fixed
   canary text ONCE (``begin_canary_attempt`` commits ``attempting`` first: a crash afterwards is
   resolved from Sent Items/Outbox evidence and otherwise reported ``send_call_failed`` - never
   re-sent);
4. reports ``submitted_to_outbox`` and, once found in Sent Items, ``sent_items_confirmed``;
5. watches the scanned folders (`on_item`, called for every newly processed item) for a reply whose
   ``In-Reply-To``/``References`` names a canary it sent, and uploads the reply's headers and the
   sender's target hash only (no body, no address).

Canaries never count against the inquiry ceilings and never touch the inquiry send intents.
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
from outlook_bridge.local_queue import CanaryRow, IntentState, LocalStore, message_id_hash
from outlook_bridge.log import event, get_logger
from outlook_bridge.outlook_adapter import MailboxAdapter, MailSnapshot, OutgoingMail, SentLookup
from outlook_bridge.wire import (
    CanaryReplyUpload,
    WorkerCanaryIntent,
    WorkerCanaryReport,
    canary_intent_problems,
)
from suv_deals.domain.canary import (
    CanaryRefusalReason,
    CanarySubmissionState,
    canary_reference,
    canary_target_hash,
)
from suv_deals.domain.replies import (
    MessageHeaders,
    normalize_message_id,
    parse_address_list,
    parse_message_id_list,
)
from suv_deals.domain.seller_contacts import AddressError

EVIDENCE_MARGIN: Final = timedelta(minutes=10)
EVIDENCE_WINDOW: Final = timedelta(hours=72)
_LOG = get_logger("canary")
_SUBMIT_REFUSALS: Final[dict[str, CanaryRefusalReason]] = {
    "account_mismatch": "account_mismatch",
    "intent_invalid": "intent_invalid",
    "mailbox_unavailable": "mailbox_unavailable",
}


def _target_hash(address: str | None) -> str | None:
    if not address:
        return None
    try:
        return canary_target_hash(address)
    except AddressError:
        return None


class CanaryProcessor:
    """Pull, refuse or claim-and-send, confirm and correlate the replies of activation canaries."""

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
        if self._on_api_error is not None:
            self._on_api_error(exc)

    # ------------------------------------------------------------------ reports

    def _report(
        self,
        intent: WorkerCanaryIntent,
        state: CanarySubmissionState,
        *,
        now: datetime,
        refusal: CanaryRefusalReason | None = None,
        observed_message_id: str | None = None,
        error_code: str | None = None,
        sent_at: datetime | None = None,
    ) -> WorkerCanaryReport:
        return WorkerCanaryReport(
            canary_id=intent.canary_id,
            mailbox_binding_id=self._config.mailbox_binding_id,
            worker_id=self._config.worker_id,
            state=state,
            refusal_reason=refusal,
            observed_internet_message_id=normalize_message_id(observed_message_id),
            sent_items_present=state == "sent_items_confirmed",
            error_code=error_code,
            reported_at=now,
            sent_at=sent_at,
        )

    def _finish(self, intent: WorkerCanaryIntent, state: IntentState, report: WorkerCanaryReport) -> None:
        self._store.finish_canary(intent.canary_id, state=state, report_json=report.model_dump_json())
        event(_LOG, "canary_" + state.value, canary_id=intent.canary_id, report_state=report.state)

    def push(self) -> int:
        """Send every stored, unacknowledged report and reply (idempotent; kept on failure)."""
        pushed = 0
        for row in self._store.canaries():
            try:
                if row.report_json is not None and not row.report_acked:
                    self._api.post_canary_report(WorkerCanaryReport.model_validate_json(row.report_json))
                    self._store.mark_canary_report_acked(row.canary_id, row.report_json)
                    pushed += 1
                if row.reply_json is not None and not row.reply_acked:
                    self._api.post_canary_reply(CanaryReplyUpload.model_validate_json(row.reply_json))
                    self._store.mark_canary_reply_acked(row.canary_id)
                    pushed += 1
            except (BridgeApiError, CredentialError) as exc:
                self._api_failed(exc)
                break
        return pushed

    # ------------------------------------------------------------------ evidence

    def _outgoing(self, intent: WorkerCanaryIntent, target: str) -> OutgoingMail:
        return OutgoingMail(
            intent_id=str(intent.canary_id),
            from_address=intent.from_address,
            to_address=target,
            reply_to_address=intent.reply_to_address,
            subject=intent.subject,
            body_text=intent.body_text,
            rfc_message_id=intent.rfc_message_id,
            inquiry_ref=canary_reference(intent.canary_id),
        )

    def _lookup(self, row: CanaryRow, intent: WorkerCanaryIntent) -> SentLookup | None:
        target = self._config.canary_target_address
        if target is None:
            return None
        since = (row.attempt_started_at or row.received_at) - EVIDENCE_MARGIN
        try:
            return self._mailbox.lookup_sent(self._outgoing(intent, target), since=since)
        except (OutlookUnavailable, StaError):
            return None

    def confirm(self, now: datetime) -> Counter[str]:
        """Resolve interrupted attempts and look for Sent Items evidence of submitted canaries."""
        counts: Counter[str] = Counter()
        for row in self._store.canaries(
            [IntentState.ATTEMPTING, IntentState.SUBMITTED, IntentState.SEND_FAILED]
        ):
            started = row.attempt_started_at or row.received_at
            if row.state != IntentState.ATTEMPTING and now - started > EVIDENCE_WINDOW:
                continue
            intent = WorkerCanaryIntent.model_validate_json(row.payload_json)
            lookup = self._lookup(row, intent)
            if lookup is None:
                counts["outlook_unavailable"] += 1
                continue
            if lookup.location == "sent_items":
                report = self._report(
                    intent,
                    "sent_items_confirmed",
                    now=now,
                    observed_message_id=lookup.internet_message_id,
                    sent_at=lookup.sent_at,
                )
                self._finish(intent, IntentState.CONFIRMED, report)
                counts["confirmed"] += 1
            elif row.state == IntentState.ATTEMPTING:
                if lookup.location == "outbox":
                    self._finish(
                        intent, IntentState.SUBMITTED, self._report(intent, "submitted_to_outbox", now=now)
                    )
                else:
                    report = self._report(
                        intent, "send_call_failed", now=now, error_code="WORKER_INTERRUPTED"
                    )
                    self._finish(intent, IntentState.SEND_FAILED, report)
                counts["recovered"] += 1
        return counts

    # ------------------------------------------------------------------ intake

    def _local_refusal(
        self, intent: WorkerCanaryIntent, *, kill_switch: bool, now: datetime
    ) -> tuple[CanaryRefusalReason, str | None] | None:
        account = self._account_smtp()
        target = self._config.canary_target_address
        if intent.is_expired(now):
            return "intent_expired", None  # even while paused: the canary fails honestly
        if kill_switch:
            return "kill_switch", None
        if intent.mailbox_binding_id != self._config.mailbox_binding_id:
            return "binding_mismatch", None
        if (
            intent.from_address.casefold() != self._config.account_smtp_address.casefold()
            or account is None
            or account.casefold() != intent.from_address.casefold()
        ):
            return "account_mismatch", None
        if canary_intent_problems(intent):
            return "intent_invalid", canary_intent_problems(intent)[0]
        if target is None:
            return "intent_invalid", "CANARY_TARGET_NOT_CONFIGURED"
        if _target_hash(target) != intent.target_address_hash:
            return "intent_invalid", "CANARY_TARGET_MISMATCH"
        if target.casefold() in {intent.from_address.casefold(), (intent.reply_to_address or "").casefold()}:
            return "intent_invalid", "CANARY_TARGET_IS_SENDER"
        if not self._classic():
            return "outlook_not_classic", None
        return None

    def run_once(self, now: datetime) -> Counter[str]:
        counts = self.confirm(now)
        self.push()
        try:
            batch = self._api.fetch_canary_intents()
        except (BridgeApiError, CredentialError) as exc:
            self._api_failed(exc)
            counts["fetch_failed"] += 1
            return counts
        for intent in batch.intents:
            counts[self._handle(intent, kill_switch=batch.kill_switch_active, now=now)] += 1
        self.push()
        return counts

    def _handle(self, intent: WorkerCanaryIntent, *, kill_switch: bool, now: datetime) -> str:
        existing = self._store.canary(intent.canary_id)
        if existing is None:
            self._store.record_canary_received(
                canary_id=intent.canary_id,
                payload_json=intent.model_dump_json(),
                message_id=intent.rfc_message_id,
                now=now,
            )
        elif existing.state != IntentState.RECEIVED:
            return "already_handled"  # the stored report (re)tells the outcome; never a 2nd send
        elif not WorkerCanaryIntent.model_validate_json(existing.payload_json).same_canary(intent):
            report = self._report(
                intent, "refused_before_send", now=now, refusal="intent_invalid", error_code="CANARY_CHANGED"
            )
            self._finish(intent, IntentState.REFUSED, report)
            return "refused"
        if self._account_smtp() is None and not kill_switch and not intent.is_expired(now):
            return "deferred_outlook_unavailable"
        refusal = self._local_refusal(intent, kill_switch=kill_switch, now=now)
        if refusal is not None:
            if refusal[0] == "kill_switch":
                return "deferred_kill_switch"  # nothing sent; the canary waits while it is valid
            report = self._report(
                intent, "refused_before_send", now=now, refusal=refusal[0], error_code=refusal[1]
            )
            self._finish(intent, IntentState.REFUSED, report)
            return "refused"
        try:
            decision = self._api.claim_canary(intent.canary_id)
        except (BridgeApiError, CredentialError) as exc:
            self._api_failed(exc)
            return "claim_unavailable"  # nothing sent; claimed again at the next poll
        if not decision.proceed:
            if decision.refusal_reason == "kill_switch":
                return "deferred_kill_switch"
            report = self._report(
                intent, "refused_before_send", now=now, refusal=decision.refusal_reason or "intent_invalid"
            )
            self._finish(intent, IntentState.REFUSED, report)
            return "refused"
        if not self._store.begin_canary_attempt(intent.canary_id, now):
            return "already_handled"
        target = self._config.canary_target_address
        assert target is not None  # checked by _local_refusal
        try:
            result = self._mailbox.submit(self._outgoing(intent, target))
        except (OutlookUnavailable, StaError) as exc:
            report = self._report(
                intent, "send_call_failed", now=now, error_code=f"SUBMIT_{type(exc).__name__}"[:64]
            )
            self._finish(intent, IntentState.SEND_FAILED, report)
            return "send_call_failed"
        if result.outcome == "refused":
            report = self._report(
                intent,
                "refused_before_send",
                now=now,
                refusal=_SUBMIT_REFUSALS.get(result.refusal or "", "intent_invalid"),
                error_code=result.error_code,
            )
            self._finish(intent, IntentState.REFUSED, report)
            return "refused"
        if result.outcome == "send_call_failed":
            report = self._report(intent, "send_call_failed", now=now, error_code=result.error_code)
            self._finish(intent, IntentState.SEND_FAILED, report)
            return "send_call_failed"
        self._finish(intent, IntentState.SUBMITTED, self._report(intent, "submitted_to_outbox", now=now))
        return "submitted"

    # ------------------------------------------------------------------ replies

    def on_item(self, snapshot: MailSnapshot, *, now: datetime) -> bool:
        """Keep the correlated test reply of a canary this worker sent (``True`` when kept)."""
        sent = self._store.sent_canary_message_hashes()
        if not sent:
            return False
        headers = MessageHeaders.from_raw(snapshot.headers)
        in_reply_to = parse_message_id_list(headers.get_all("in-reply-to"))
        references = parse_message_id_list(headers.get_all("references"))
        canary_id: UUID | None = None
        for value in (*in_reply_to, *references):
            canary_id = sent.get(message_id_hash(value))
            if canary_id is not None:
                break
        message_id = normalize_message_id(snapshot.internet_message_id)
        if canary_id is None or message_id is None:
            return False
        senders = [a for value in headers.get_all("from") for a in parse_address_list(value) if a]
        from_hash = _target_hash(snapshot.sender_smtp) or (
            _target_hash(senders[0]) if len(senders) == 1 else None
        )
        if from_hash is None:
            return False
        try:
            reply = CanaryReplyUpload(
                canary_id=canary_id,
                mailbox_binding_id=self._config.mailbox_binding_id,
                worker_id=self._config.worker_id,
                internet_message_id=message_id,
                in_reply_to=in_reply_to[:20],
                references=references[:50],
                from_address_hash=from_hash,
                received_at=snapshot.ref.received_at or now,
            )
        except ValueError:
            return False
        kept = self._store.record_canary_reply(canary_id, reply.model_dump_json())
        if kept:
            event(_LOG, "canary_reply_seen", canary_id=canary_id)
        return kept


__all__ = ["EVIDENCE_WINDOW", "CanaryProcessor"]
