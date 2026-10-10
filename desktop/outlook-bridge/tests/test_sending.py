"""``outlook_local`` send intents: one attempt, honest evidence, no blind resend (spec 37.5/37.6).

The worker only executes backend intents for the bounded initial inquiry; it never composes a
message, follow-up or reply itself. Local submission is reported separately from Sent Items
evidence, an interrupted or failed ``.Send`` is uncertain and never retried, and an empty Sent
Items search never releases the reservation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from bridge_support import MAILBOX_ID, OWNER, START, Harness, classic_registry
from outlook_bridge.compatibility import check_compatibility
from outlook_bridge.local_queue import IntentState
from outlook_bridge.outlook_adapter import INQUIRY_REF_PROPERTY, PR_INTERNET_MESSAGE_ID
from outlook_bridge.testing import SYNTHETIC_LISTING_URL, make_intent, rendered_inquiry
from outlook_bridge.wire import WorkerSendIntent, template_scope_problems

from suv_deals.domain.seller_templates import build_vehicle_label, render, render_preview_mk


def _intent(h: Harness, **overrides: Any) -> WorkerSendIntent:
    kwargs: dict[str, Any] = {
        "inquiry_id": uuid4(),
        "mailbox_binding_id": MAILBOX_ID,
        "from_address": OWNER,
        "created_at": h.clock.now() - timedelta(minutes=1),
    }
    kwargs.update(overrides)
    return make_intent(**kwargs)


def _reports(h: Harness, intent_id: UUID) -> list[dict[str, Any]]:
    return [r for r in h.backend.reports if r["intent_id"] == str(intent_id)]


def _poll(h: Harness) -> None:
    h.advance(timedelta(seconds=31))


def test_intent_is_submitted_once_then_confirmed_from_sent_items(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    worker = harness.worker()
    report = worker.start()
    assert report.sends["submitted"] == 1
    assert harness.backend.claims == [intent.intent_id]  # revalidated immediately before .Send
    assert harness.outlook.send_calls == [intent.subject]
    queued = harness.outlook.items_in(harness.outbox)
    assert len(queued) == 1
    props = queued[0].PropertyAccessor._props
    assert props[PR_INTERNET_MESSAGE_ID] == intent.rfc_message_id
    assert props[INQUIRY_REF_PROPERTY] == intent.inquiry_ref
    first = _reports(harness, intent.intent_id)
    assert [r["state"] for r in first] == ["submitted_to_outbox"]
    assert first[0]["outbox_pending"] == "yes" and first[0]["sent_items_present"] is False
    assert first[0]["account_smtp_address_used"] == OWNER
    # Outlook transmits later; only Sent Items evidence is reported as confirmed (no fabricated receipt).
    harness.outlook.deliver_outbox(harness.account)
    _poll(harness)
    worker.tick()
    states = [r["state"] for r in _reports(harness, intent.intent_id)]
    assert states == ["submitted_to_outbox", "sent_items_confirmed"]
    confirmed = _reports(harness, intent.intent_id)[-1]
    assert confirmed["observed_internet_message_id"] == intent.rfc_message_id
    assert confirmed["sent_items_present"] is True and confirmed["sent_at"] is not None
    for _ in range(3):
        _poll(harness)
        worker.tick()
    assert harness.outlook.send_calls == [intent.subject]  # never a second message
    row = harness.store.intent(intent.intent_id)
    assert row is not None and row.state == IntentState.CONFIRMED


def test_send_call_failure_is_uncertain_and_never_retried(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    harness.outlook.root.send_mode = "raise"
    worker = harness.worker()
    assert worker.start().sends["send_call_failed"] == 1
    harness.outlook.root.send_mode = "ok"
    harness.backend.open_intents.append(intent.intent_id)  # the backend offers it again
    for _ in range(3):
        _poll(harness)
        worker.tick()
    assert len(harness.outlook.send_calls) == 1
    states = [r["state"] for r in _reports(harness, intent.intent_id)]
    assert states and set(states) == {"send_call_failed"}  # uncertain; never "refused"
    assert harness.store.intent(intent.intent_id).state == IntentState.SEND_FAILED  # type: ignore[union-attr]


def test_send_that_queued_before_failing_is_found_in_the_outbox(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    harness.outlook.root.send_mode = "raise_after_queue"
    worker = harness.worker()
    worker.start()
    _poll(harness)
    worker.tick()
    states = [r["state"] for r in _reports(harness, intent.intent_id)]
    assert states == ["send_call_failed", "submitted_to_outbox"]
    assert len(harness.outlook.send_calls) == 1


def test_crash_after_attempt_commit_never_resends_and_empty_sent_items_proves_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)

    def crash(mail: object) -> object:
        raise SystemExit("worker process killed between 'attempting' and .Send")

    monkeypatch.setattr(harness.session, "submit", crash)
    with pytest.raises(SystemExit):
        harness.worker().start()
    assert harness.store.intent(intent.intent_id).state == IntentState.ATTEMPTING  # type: ignore[union-attr]
    monkeypatch.undo()
    restarted = harness.worker()  # the restarted worker, same durable store
    restarted.start()
    assert harness.outlook.send_calls == []  # never sent blindly after the crash
    reports = _reports(harness, intent.intent_id)
    assert [r["state"] for r in reports] == ["send_call_failed"]
    assert reports[0]["error_code"] == "WORKER_INTERRUPTED"
    for _ in range(3):
        _poll(harness)
        restarted.tick()
    assert harness.outlook.send_calls == []
    assert harness.store.intent(intent.intent_id).state == IntentState.SEND_FAILED  # type: ignore[union-attr]


def test_crash_after_send_queued_reports_the_outbox_evidence(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    original = harness.session.submit

    def send_then_crash(mail: Any) -> Any:
        original(mail)
        raise SystemExit("killed after .Send returned")

    monkeypatch.setattr(harness.session, "submit", send_then_crash)
    with pytest.raises(SystemExit):
        harness.worker().start()
    monkeypatch.undo()
    harness.worker().start()
    assert [r["state"] for r in _reports(harness, intent.intent_id)] == ["submitted_to_outbox"]
    assert len(harness.outlook.send_calls) == 1


def test_a_second_intent_for_an_attempted_inquiry_is_refused(harness: Harness) -> None:
    first = _intent(harness)
    harness.backend.add_intent(first)
    harness.outlook.root.send_mode = "raise"  # outcome unknown
    worker = harness.worker()
    worker.start()
    harness.outlook.root.send_mode = "ok"
    retry = _intent(harness, inquiry_id=first.inquiry_id, attempt_number=2)
    harness.backend.add_intent(retry)
    _poll(harness)
    worker.tick()
    assert len(harness.outlook.send_calls) == 1
    refused = _reports(harness, retry.intent_id)
    assert refused[0]["state"] == "refused_before_send" and refused[0]["refusal_reason"] == "duplicate_intent"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"from_address": "private@other.example.invalid"}, "account_mismatch"),
        ({"created_at": START - timedelta(hours=7)}, "intent_expired"),
        ({"body_hash": "f" * 64}, "intent_invalid"),
        ({"subject": "Anfrage\r\nBcc: someone@else.example.invalid"}, "intent_invalid"),
        ({"body": "<html><body>Anfrage</body></html>"}, "intent_invalid"),
        ({"to_address": "a@dealer.example.invalid,b@dealer.example.invalid"}, "intent_invalid"),
        ({"subject": "Anfrage \u202e evil"}, "intent_invalid"),
    ],
)
def test_local_refusals_happen_before_any_claim_or_send(
    harness: Harness, overrides: dict[str, Any], reason: str
) -> None:
    intent = _intent(harness, **overrides)
    harness.backend.add_intent(intent)
    harness.worker().start()
    reports = _reports(harness, intent.intent_id)
    assert reports[0]["state"] == "refused_before_send" and reports[0]["refusal_reason"] == reason
    assert harness.backend.claims == [] and harness.outlook.send_calls == []


def test_kill_switch_refuses_untransmitted_intents(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    harness.backend.kill_switch = True
    harness.worker().start()
    assert _reports(harness, intent.intent_id)[0]["refusal_reason"] == "kill_switch"
    assert harness.backend.claims == [] and harness.outlook.send_calls == []


def test_new_outlook_never_sends(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    new_outlook = check_compatibility(now=START, platform="win32", registry=classic_registry(toggle=1))
    harness.worker(compat=new_outlook).start()
    assert _reports(harness, intent.intent_id)[0]["refusal_reason"] == "outlook_not_classic"
    assert harness.outlook.send_calls == []
    assert harness.backend.account_reports[-1]["outlook_flavour"] == "new"


def test_server_revalidation_refusal_and_unreachable_claim(harness: Harness) -> None:
    refused = _intent(harness)
    harness.backend.add_intent(refused)
    harness.backend.claim_refusal = "binding_mismatch"
    worker = harness.worker()
    worker.start()
    assert _reports(harness, refused.intent_id)[0]["refusal_reason"] == "binding_mismatch"
    harness.backend.claim_refusal = None
    later = _intent(harness)
    harness.backend.add_intent(later)
    harness.backend.fail("/v1/mail-workers/send-intents/", harness.backend.status(503, code="UNAVAILABLE"))
    _poll(harness)
    assert worker.tick().sends["claim_unavailable"] == 1
    assert harness.outlook.send_calls == []  # nothing is sent without a successful claim
    _poll(harness)
    worker.tick()
    assert harness.outlook.send_calls == [later.subject]


def test_local_ceilings_are_never_exceeded(harness: Harness) -> None:
    intents = [_intent(harness, ttl=timedelta(hours=48)) for _ in range(3)]
    for intent in intents:
        harness.backend.add_intent(intent)
    worker = harness.worker()
    report = worker.start()
    assert report.sends["submitted"] == 2 and report.sends["deferred_ceiling"] == 1
    _poll(harness)
    worker.tick()
    assert len(harness.outlook.send_calls) == 2  # ceilings, never targets
    harness.advance(timedelta(hours=24, minutes=1))
    worker.tick()
    assert len(harness.outlook.send_calls) == 3


def test_changed_intent_payload_is_refused(harness: Harness) -> None:
    intent = _intent(harness)
    harness.store.record_intent_received(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        payload_json=intent.model_dump_json(),
        now=harness.clock.now(),
    )
    changed = intent.model_copy(update={"to_address": "other@dealer.example.invalid"})
    harness.backend.add_intent(changed)
    harness.worker().start()
    report = _reports(harness, intent.intent_id)[0]
    assert report["refusal_reason"] == "intent_invalid" and report["error_code"] == "INTENT_CHANGED"
    assert harness.outlook.send_calls == []


def test_waiting_intent_that_expires_is_reported_never_sent(harness: Harness) -> None:
    intents = [_intent(harness, ttl=timedelta(hours=1)) for _ in range(3)]
    for intent in intents:
        harness.backend.add_intent(intent)
    worker = harness.worker()
    worker.start()  # two sent, the third waits behind the ceiling
    waiting = intents[2]
    harness.backend.open_intents.remove(waiting.intent_id)
    harness.advance(timedelta(hours=2))
    worker.tick()
    reports = _reports(harness, waiting.intent_id)
    assert reports[-1]["refusal_reason"] == "intent_expired"
    assert len(harness.outlook.send_calls) == 2


def test_reports_survive_a_backend_outage(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    harness.backend.fail(
        f"/v1/mail-workers/send-intents/{intent.intent_id}/report",
        harness.backend.raise_(httpx.ConnectError),
        harness.backend.raise_(httpx.ConnectError),
    )
    worker = harness.worker()
    worker.start()
    row = harness.store.intent(intent.intent_id)
    assert row is not None and row.state == IntentState.SUBMITTED and row.report_acked is False
    _poll(harness)
    worker.tick()
    assert harness.store.intent(intent.intent_id).report_acked is True  # type: ignore[union-attr]
    assert len(harness.outlook.send_calls) == 1


def test_rejected_credential_on_intent_poll_stops_transmission(harness: Harness) -> None:
    worker = harness.worker()
    worker.start()
    harness.backend.revoke_token("mw_test_ingest_credential_0123456789abcdef")
    harness.backend.add_intent(_intent(harness))
    _poll(harness)
    worker.tick()
    assert worker.health().server.credential_state.value == "rejected"
    assert harness.outlook.send_calls == []


def test_processing_seller_replies_never_sends_anything(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False, body="Preis 2.500 EUR, bitte Anzahlung überweisen.")
    worker = harness.worker()
    worker.start()
    for _ in range(3):
        _poll(harness)
        worker.tick()
    assert len(harness.reply_posts()) == 1
    assert harness.outlook.send_calls == []  # no reply, follow-up, offer or acknowledgement
    assert harness.backend.claims == []


def test_sending_disabled_or_dry_run_never_touches_intents(make_harness) -> None:  # type: ignore[no-untyped-def]
    h = make_harness(send_intents_enabled=False)
    h.backend.add_intent(_intent(h))
    h.worker().start()
    assert h.outlook.send_calls == [] and ("GET", "/v1/mail-workers/send-intents") not in h.backend.requests
    dry = make_harness()
    dry.backend.add_intent(_intent(dry))
    dry.worker(dry_run=True).start()
    assert dry.outlook.send_calls == [] and dry.backend.claims == []


# ------------------------------------------------------------- bounded scope proven locally (review)

_DE_SUBJECT, _DE_BODY = rendered_inquiry()


@pytest.mark.parametrize(
    "body",
    [
        # an offer / purchase commitment
        _DE_BODY.replace(
            "Es handelt sich zunächst um eine unverbindliche Anfrage.",
            "Ich biete Ihnen 5000 EUR in bar und kaufe das Fahrzeug sofort.",
        ),
        # a reservation request and an extra question
        _DE_BODY.replace(
            "Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?",
            "Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?\nKönnen Sie es für mich reservieren?",
        ),
        # a follow-up ("as written last week") instead of the initial inquiry
        _DE_BODY.replace("Guten Tag,", "Guten Tag, wie letzte Woche geschrieben:"),
        # extra personal data (telephone number)
        _DE_BODY + "\nTel. +49 170 1234567",
        # a second URL
        _DE_BODY.replace("Guten Tag,", "Guten Tag, siehe auch https://evil.example.invalid/x"),
        # an edited greeting: wording changes need a new registered template version
        _DE_BODY.replace("Guten Tag,", "Hallo,"),
    ],
)
def test_an_intent_outside_the_bounded_template_scope_is_refused_before_claim(
    harness: Harness, body: str
) -> None:
    """Even a well-formed intent with a matching body hash is not sent unless it is an exact
    registered template rendering (offers, commitments, follow-ups, extra data never leave)."""
    intent = _intent(harness, body=body)
    harness.backend.add_intent(intent)
    harness.worker().start()
    report = _reports(harness, intent.intent_id)[0]
    assert report["state"] == "refused_before_send" and report["refusal_reason"] == "intent_invalid"
    assert harness.backend.claims == [] and harness.outlook.send_calls == []


def test_the_informational_macedonian_preview_is_never_sent(harness: Harness) -> None:
    rendered = render(
        "seller_initial_de_v1",
        build_vehicle_label("Synthetic", "SUV"),
        "REF-1234",
        SYNTHETIC_LISTING_URL,
        "Synthetic Sender",
        verified_listing_url=SYNTHETIC_LISTING_URL,
    )
    preview = render_preview_mk(rendered)
    intent = _intent(harness, subject=preview.subject, body=preview.body)
    harness.backend.add_intent(intent)
    harness.worker().start()
    assert _reports(harness, intent.intent_id)[0]["refusal_reason"] == "intent_invalid"
    assert harness.outlook.send_calls == []


def test_signature_must_be_the_verified_sender_display_name(harness: Harness) -> None:
    intent = _intent(harness, from_display_name="Another Name")
    harness.backend.add_intent(intent)
    harness.worker().start()
    assert _reports(harness, intent.intent_id)[0]["refusal_reason"] == "intent_invalid"
    assert harness.outlook.send_calls == []


@pytest.mark.parametrize(
    "template_id", ["seller_initial_it_v1", "seller_initial_fr_v1", "seller_initial_en_v1"]
)
def test_every_registered_seller_template_is_sendable(harness: Harness, template_id: str) -> None:
    subject, body = rendered_inquiry(template_id)
    intent = _intent(harness, subject=subject, body=body)
    harness.backend.add_intent(intent)
    assert harness.worker().start().sends["submitted"] == 1
    assert harness.outlook.send_calls == [subject]


def test_template_scope_helper_matches_the_domain_renderer() -> None:
    subject, body = rendered_inquiry()
    assert template_scope_problems(subject, body, from_display_name="Synthetic Sender") == ()
    assert template_scope_problems(subject, body + " ", from_display_name="Synthetic Sender") == (
        "NOT_TEMPLATE_RENDERING",
    )
    assert template_scope_problems(subject.replace("REF-1234", "REF-9"), body, from_display_name="x") == (
        "SENDER_DISPLAY_NAME_MISMATCH",
    )


# ------------------------------------------------------------- recipient / ceilings / races (review)


def test_wrongly_resolved_recipient_is_reported_refused_and_never_sent(harness: Harness) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    harness.outlook.root.resolve_overrides[intent.to_address] = "contact@other.example.invalid"
    harness.worker().start()
    report = _reports(harness, intent.intent_id)[0]
    assert report["state"] == "refused_before_send" and report["refusal_reason"] == "intent_invalid"
    assert report["error_code"] == "RECIPIENT_MISMATCH"
    assert harness.outlook.send_calls == []


def test_refusals_before_send_do_not_consume_the_local_ceilings(harness: Harness) -> None:
    refused = [_intent(harness, ttl=timedelta(hours=48)) for _ in range(2)]
    for intent in refused:
        harness.outlook.root.resolve_overrides[intent.to_address] = "contact@other.example.invalid"
        harness.backend.add_intent(intent)
    worker = harness.worker()
    assert worker.start().sends["refused"] == 2
    harness.outlook.root.resolve_overrides.clear()
    later = _intent(harness)
    harness.backend.add_intent(later)
    _poll(harness)
    assert worker.tick().sends["submitted"] == 1  # two proven non-sends left the ceiling untouched
    assert harness.outlook.send_calls == [later.subject]


def test_another_process_attempting_the_inquiry_during_the_claim_blocks_the_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate check is repeated atomically with the 'attempting' commit."""
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    original = harness.api.claim_send_intent

    def claim_while_another_worker_attempts(intent_id: UUID) -> Any:
        other = uuid4()  # a second worker process on the same store attempts another intent
        harness.store.record_intent_received(
            intent_id=other, inquiry_id=intent.inquiry_id, payload_json="{}", now=harness.clock.now()
        )
        assert harness.store.begin_attempt(other, harness.clock.now())
        return original(intent_id)

    monkeypatch.setattr(harness.api, "claim_send_intent", claim_while_another_worker_attempts)
    harness.worker().start()
    assert harness.outlook.send_calls == []
    report = _reports(harness, intent.intent_id)[0]
    assert report["refusal_reason"] == "duplicate_intent"


def test_ceiling_filled_by_another_process_during_the_claim_defers_the_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = _intent(harness)
    harness.backend.add_intent(intent)
    original = harness.api.claim_send_intent

    def claim_while_others_send(intent_id: UUID) -> Any:
        for _ in range(2):
            other = uuid4()
            harness.store.record_intent_received(
                intent_id=other, inquiry_id=uuid4(), payload_json="{}", now=harness.clock.now()
            )
            assert harness.store.begin_attempt(other, harness.clock.now())
        return original(intent_id)

    monkeypatch.setattr(harness.api, "claim_send_intent", claim_while_others_send)
    report = harness.worker().start()
    assert report.sends["deferred_ceiling"] == 1
    assert harness.outlook.send_calls == []
    row = harness.store.intent(intent.intent_id)
    assert row is not None and row.state == IntentState.RECEIVED  # waits; may expire, never forced


@pytest.mark.parametrize("reply_before_confirmation", [False, True])
def test_reply_to_an_outlook_assigned_message_id_is_never_silently_dropped(
    harness: Harness, reply_before_confirmation: bool
) -> None:
    """If Outlook does not keep the intent's Message-ID, a reply references the id Outlook used.
    Until the backend adds it to the binding the reply stays local, and an unresolved one is a
    surfaced matching gap - never silently "unrelated"."""
    intent = _intent(harness)
    harness.bind(intent.inquiry_id)  # the binding knows only the intended <inquiry-...> id
    harness.backend.add_intent(intent)
    harness.outlook.root.message_id_settable = False
    worker = harness.worker()
    worker.start()
    sent = harness.outlook.items_in(harness.outbox)[0]
    observed = sent.PropertyAccessor._props[PR_INTERNET_MESSAGE_ID]
    assert observed != intent.rfc_message_id

    def reply() -> None:
        harness.outlook.deliver(
            harness.inbox,
            subject="AW: Ihre Nachricht",
            body="Ja, noch da.",
            sender="seller@dealer.example.invalid",
            message_id=f"<r-{uuid4().hex[:8]}@dealer.example.invalid>",
            received_at=harness.clock.now(),
            in_reply_to=observed,
            references=(observed,),
            fire_event=True,
        )

    if reply_before_confirmation:
        reply()
        worker.tick()
    harness.outlook.deliver_outbox(harness.account)
    _poll(harness)
    worker.tick()
    confirmed = _reports(harness, intent.intent_id)[-1]
    assert confirmed["state"] == "sent_items_confirmed"
    assert confirmed["observed_internet_message_id"] == observed  # the backend learns the real id
    if not reply_before_confirmation:
        reply()
        worker.tick()
    assert harness.store.pending_count() == 1 and harness.reply_posts() == []
    harness.advance(timedelta(hours=25))
    worker.tick()
    assert harness.store.outcome_counts().get("matching_gap") == 1
    assert any(g.kind == "unresolved_reply_matching" for g in harness.store.gaps())
    assert harness.reply_posts() == []
