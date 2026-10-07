# ruff: noqa: RUF001
# (RUF001: Macedonian summaries are Cyrillic and inquiry subjects use the template's EN DASH.)
"""Unit tests for domain.replies (spec 37.6-37.8, 37.10 delta tests).

Every address, Message-ID, listing and message here is SYNTHETIC (``example.invalid``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.enums import (
    Availability,
    AvailabilityEvidenceKind,
    EmailProviderKind,
    InquiryState,
    MessageLanguage,
    OutboxState,
    PriceBasis,
    ReplyMessageType,
    SuppressionReason,
)
from suv_deals.domain.notifications import text_problems
from suv_deals.domain.replies import (
    MAX_BODY_BYTES,
    AmountContext,
    AttachmentAction,
    AttachmentMeta,
    AvailabilityClaimStatus,
    BounceDetails,
    ClassificationSignal,
    CorrelationOutcome,
    CorrelationReason,
    CorrelationResult,
    DocumentClaimStatus,
    DocumentKind,
    EscalationReason,
    InboundMessage,
    IngestDecisionKind,
    InquiryBinding,
    InquiryBindingState,
    InquiryQuestion,
    MessageHeaders,
    MessageLocator,
    MkReplySummary,
    PriceCondition,
    PriceQuoteClaim,
    ReplyClaims,
    ReplyIngestRequest,
    ReplySignalStatus,
    ReplySourceContent,
    RequestKind,
    SourceMessageIdentity,
    StoredReplyIngest,
    TranslationProvider,
    attach_full_translation,
    build_ingest_request,
    build_mk_summary,
    build_seller_reply_signal,
    canonical_address,
    classify_message,
    correlate_reply,
    dashboard_reply_url,
    decide_ingest,
    decide_reply_processing,
    evaluate_attachments,
    explain_classification,
    extract_reply_claims,
    format_amount_mk,
    is_forwarded,
    is_processable_item,
    normalize_message_id,
    parse_address_list,
    parse_delivery_report,
    parse_message_id_list,
    raise_for_ingest_conflict,
    reply_dedup_key,
    reply_signal_route,
    safe_filename,
    sanitize_reply_body,
    should_emit_reply_signal,
    source_content_fingerprint,
    strip_quoted_text,
    unmatched_retention,
)
from suv_deals.errors import Forbidden, IdempotencyConflict, ValidationFailed

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
MB = UUID("77777777-7777-4777-8777-777777777777")
MB_OTHER = UUID("77777777-7777-4777-8777-777777777778")
INQ = UUID("66666666-6666-4666-8666-666666666666")
INQ2 = UUID("66666666-6666-4666-8666-666666666667")
REPLY_ID = UUID("88888888-8888-4888-8888-888888888888")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
LISTING_ID = UUID("11111111-1111-4111-8111-111111111111")
OUT_ID = "<synthetic-inquiry@example.invalid>"
INTENT_ID = "<synthetic-intent@example.invalid>"
SELLER = "seller@example.invalid"
HASH_A = "a" * 64
REG, COC, GEN = DocumentKind.REGISTRATION, DocumentKind.COC, DocumentKind.GENERAL
HASH_B = "b" * 64
OUTBOUND_DE = (
    "Guten Tag,\n\nich schreibe wegen dieses Fahrzeugs: https://dealer.example/vehicles/TEST-204\n\n"
    "Ist das Fahrzeug noch verfügbar?\n"
    "Könnten Sie mir die vorhandenen Fahrzeugunterlagen zusenden, insbesondere die Zulassungsunterlagen "
    "und das CoC, falls vorhanden? Bitte schwärzen Sie persönliche Daten.\n"
    "Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?\n\n"
    "Es handelt sich zunächst um eine unverbindliche Anfrage.\n\nFreundliche Grüße\nSynthetic Sender"
)


def binding(**overrides: Any) -> InquiryBinding:
    data: dict[str, Any] = {
        "inquiry_id": INQ,
        "binding_version": 2,
        "mailbox_binding_id": MB,
        "provider": EmailProviderKind.OUTLOOK_LOCAL,
        "outbound_message_ids": (OUT_ID,),
        "verified_seller_aliases": (SELLER,),
        "listing_references": ("TEST-204",),
        "listing_urls": ("https://dealer.example/vehicles/TEST-204",),
        "provider_thread_ids": ("conv-1",),
        "listing_id": LISTING_ID,
    }
    data.update(overrides)
    return InquiryBinding(**data)


OTHER_BINDING = InquiryBinding(
    inquiry_id=INQ2,
    binding_version=1,
    mailbox_binding_id=MB,
    provider=EmailProviderKind.OUTLOOK_LOCAL,
    outbound_message_ids=("<other-inquiry@example.invalid>",),
    verified_seller_aliases=("dealer2@example.invalid",),
    listing_references=("TEST-999",),
)


def message(
    headers: dict[str, Any],
    body: str = "",
    *,
    mailbox: UUID = MB,
    msgid: str | None = "<reply-1@example.invalid>",
    thread: str | None = None,
    attachments: tuple[AttachmentMeta, ...] = (),
    received_at: datetime | None = NOW,
    entry_id: str | None = "synthetic-entry",
    provider: EmailProviderKind = EmailProviderKind.OUTLOOK_LOCAL,
) -> InboundMessage:
    identity = SourceMessageIdentity(
        mailbox_binding_id=mailbox,
        provider=provider,
        internet_message_id=msgid,
        provider_thread_id=thread,
        outlook_entry_id=entry_id,
        outlook_store_id="synthetic-store",
        received_at=received_at,
    )
    return InboundMessage(identity=identity, headers=headers, body_text=body, attachments=attachments)


def reply_headers(**extra: str) -> dict[str, str]:
    headers = {
        "From": f"Synthetic Seller <{SELLER}>",
        "In-Reply-To": OUT_ID,
        "References": OUT_ID,
        "Subject": "Re: Anfrage zu Example Trail – TEST-204",
    }
    headers.update(extra)
    return headers


def attachment(
    name: str, mime: str = "application/pdf", size: int = 1000, digest: str = HASH_A
) -> AttachmentMeta:
    return AttachmentMeta(filename=name, mime_type=mime, byte_size=size, sha256=digest, local_ref="loc-1")


def spec_request(**overrides: Any) -> ReplyIngestRequest:
    """The synthetic spec 37.8 request fixture."""
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "inquiry_id": str(INQ),
        "binding_version": 2,
        "mailbox_binding_id": str(MB),
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
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return ReplyIngestRequest.model_validate(data)


# =============================================================================================
# Classification (37.10: auto-reply/bounce/DSN classification in DE/IT/FR/EN)
# =============================================================================================


class TestClassification:
    @pytest.mark.parametrize(
        "subject",
        [
            "Abwesenheitsnotiz: Anfrage zu Example Trail",
            "Automatische Antwort: Anfrage",
            "Fuori sede: Richiesta su Example Trail",
            "Risposta automatica: Richiesta",
            "Absence : Renseignements sur Example Trail",
            "Réponse automatique : Renseignements",
            "Out of office: Enquiry about Example Trail",
            "Automatic reply: Enquiry",
        ],
    )
    def test_auto_reply_subjects_in_four_languages(self, subject: str) -> None:
        assert classify_message({"From": SELLER, "Subject": subject}, "Text") == ReplyMessageType.AUTO_REPLY

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("Auto-Submitted", "auto-replied"),
            ("Auto-Submitted", "auto-generated; owner-email=x"),
            ("X-Autoreply", "yes"),
            ("X-Autorespond", "1"),
            ("Precedence", "auto_reply"),
        ],
    )
    def test_auto_reply_headers(self, name: str, value: str) -> None:
        result = explain_classification({"From": SELLER, "Subject": "Re: Anfrage", name: value}, "x")
        assert result.message_type == ReplyMessageType.AUTO_REPLY

    def test_auto_submitted_no_is_a_human_reply(self) -> None:
        assert (
            classify_message({"From": SELLER, "Subject": "Re: Anfrage", "Auto-Submitted": "no"}, "Ja")
            == ReplyMessageType.SELLER_REPLY
        )

    def test_precedence_bulk_alone_is_not_an_auto_reply(self) -> None:
        assert (
            classify_message({"From": SELLER, "Subject": "Re: x", "Precedence": "bulk"}, "Ja")
            == ReplyMessageType.SELLER_REPLY
        )

    @pytest.mark.parametrize(
        "subject",
        [
            "Unzustellbar: Anfrage zu Example Trail",
            "Nicht zustellbar: Anfrage",
            "Non recapitabile: Richiesta su Example Trail",
            "Impossibile recapitare il messaggio",
            "Non remis : Renseignements sur Example Trail",
            "Échec de la remise du message",
            "Undelivered Mail Returned to Sender",
            "Undeliverable: Enquiry",
            "Delivery Status Notification (Failure)",
        ],
    )
    def test_bounce_subjects_in_four_languages(self, subject: str) -> None:
        assert classify_message({"From": SELLER, "Subject": subject}, "x") == ReplyMessageType.BOUNCE

    def test_dsn_multipart_report_failed_is_bounce(self) -> None:
        headers = {
            "From": "Mail Delivery System <MAILER-DAEMON@mx.example.invalid>",
            "Subject": "Undelivered Mail Returned to Sender",
            "Content-Type": 'multipart/report; report-type="delivery-status"; boundary=b',
        }
        body = "Final-Recipient: rfc822; seller@example.invalid\nAction: failed\nStatus: 5.1.1\n"
        result = explain_classification(headers, body)
        assert result.message_type == ReplyMessageType.BOUNCE
        assert ClassificationSignal.DSN_ACTION_FAILED in result.signals

    def test_dsn_delayed_is_a_delivery_notice(self) -> None:
        headers = {
            "From": "postmaster@mx.example.invalid",
            "Subject": "Delivery Status Notification (Delay)",
            "Content-Type": "multipart/report; report-type=delivery-status",
        }
        body = "Action: delayed\nStatus: 4.4.7\n"
        assert classify_message(headers, body) == ReplyMessageType.DELIVERY_NOTICE

    def test_dsn_part_as_attachment_is_detected(self) -> None:
        dsn = AttachmentMeta(
            filename="details.txt", mime_type="message/delivery-status", byte_size=10, sha256=HASH_A
        )
        assert classify_message(
            {"From": "x@mx.example.invalid", "Subject": "Hinweis"}, "Action: failed", attachments=(dsn,)
        ) == (ReplyMessageType.BOUNCE)

    def test_dsn_relayed_or_delivered_is_a_delivery_notice(self) -> None:
        headers = {
            "From": "postmaster@mx.example.invalid",
            "Content-Type": "multipart/report; report-type=delivery-status",
        }
        assert (
            classify_message(headers, "Action: delivered\nStatus: 2.0.0") == ReplyMessageType.DELIVERY_NOTICE
        )

    def test_mailer_daemon_and_failed_recipients_are_bounces(self) -> None:
        assert classify_message({"From": "MAILER-DAEMON@mx.example.invalid", "Subject": "Hinweis"}, "x") == (
            ReplyMessageType.BOUNCE
        )
        assert classify_message(
            {"From": "someone@mx.example.invalid", "Subject": "x", "X-Failed-Recipients": SELLER}, "x"
        ) == (ReplyMessageType.BOUNCE)

    @pytest.mark.parametrize(
        "subject",
        [
            "Zugestellt: Anfrage",
            "Gelesen: Anfrage",
            "Consegnato: Richiesta",
            "Letto: Richiesta",
            "Remis : Objet",
            "Lu : Objet",
            "Delivered: Enquiry",
            "Read: Enquiry",
            "Lesebestätigung",
            "Accusé de lecture",
        ],
    )
    def test_delivery_notice_subjects(self, subject: str) -> None:
        assert classify_message({"From": SELLER, "Subject": subject}, "x") == ReplyMessageType.DELIVERY_NOTICE

    def test_mdn_report_is_a_delivery_notice(self) -> None:
        headers = {"From": SELLER, "Content-Type": "multipart/report; report-type=disposition-notification"}
        assert classify_message(headers, "x") == ReplyMessageType.DELIVERY_NOTICE

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Spam-Flag": "YES"},
            {"X-Spam-Status": "Yes, score=12.0"},
            {"X-MS-Exchange-Organization-SCL": "9"},
            {"X-Forefront-Antispam-Report": "CIP:1.2.3.4;SCL:6;SRV:BULK"},
        ],
    )
    def test_spam_headers(self, headers: dict[str, str]) -> None:
        assert (
            classify_message({"From": SELLER, "Subject": "Re: Anfrage", **headers}, "x")
            == ReplyMessageType.SPAM
        )

    def test_spam_wins_over_bounce_and_junk_folder_counts(self) -> None:
        headers = {"From": SELLER, "Subject": "Undeliverable", "X-Spam-Flag": "yes"}
        assert classify_message(headers, "x") == ReplyMessageType.SPAM
        assert (
            classify_message({"From": SELLER, "Subject": "Re"}, "x", in_junk_folder=True)
            == ReplyMessageType.SPAM
        )
        assert classify_message({"From": SELLER, "X-MS-Exchange-Organization-SCL": "-1"}, "x") == (
            ReplyMessageType.SELLER_REPLY
        )

    def test_outlook_message_classes(self) -> None:
        base = {"From": SELLER, "Subject": "Re: Anfrage"}
        assert classify_message(base, "x", message_class="REPORT.IPM.Note.NDR") == ReplyMessageType.BOUNCE
        assert (
            classify_message(base, "x", message_class="REPORT.IPM.Note.DR")
            == ReplyMessageType.DELIVERY_NOTICE
        )
        assert (
            classify_message(base, "x", message_class="REPORT.IPM.Note.IPNRN")
            == ReplyMessageType.DELIVERY_NOTICE
        )
        assert classify_message(base, "x", message_class="IPM.Note.Rules.OofTemplate.Microsoft") == (
            ReplyMessageType.AUTO_REPLY
        )
        assert classify_message(base, "x", message_class="IPM.Note") == ReplyMessageType.SELLER_REPLY

    def test_meetings_and_non_mail_items_are_never_processed_as_replies(self) -> None:
        result = explain_classification({"From": SELLER}, "x", message_class="IPM.Schedule.Meeting.Request")
        assert result.message_type == ReplyMessageType.AMBIGUOUS
        assert ClassificationSignal.NON_MAIL_ITEM in result.signals
        assert not is_processable_item("IPM.Schedule.Meeting.Request")
        assert not is_processable_item("IPM.Sharing")
        assert not is_processable_item("IPM.Appointment")
        assert not is_processable_item(None)
        assert not is_processable_item("ipm note; drop table")
        assert is_processable_item("IPM.Note")
        assert is_processable_item("IPM.Note.SMIME")
        assert is_processable_item("REPORT.IPM.Note.NDR")

    @pytest.mark.parametrize(
        ("headers", "signal"),
        [
            ({"Subject": "Re: x"}, ClassificationSignal.MISSING_SENDER),
            (
                {"From": f"{SELLER}, other@example.invalid", "Subject": "Re"},
                ClassificationSignal.MULTIPLE_SENDERS,
            ),
            ({"From": "not an address", "Subject": "Re"}, ClassificationSignal.INVALID_SENDER),
            (
                {"From": SELLER, "Content-Type": "multipart/report; report-type=weird"},
                ClassificationSignal.UNKNOWN_REPORT_TYPE,
            ),
        ],
    )
    def test_ambiguous_messages(self, headers: dict[str, str], signal: ClassificationSignal) -> None:
        result = explain_classification(headers, "Ja")
        assert result.message_type == ReplyMessageType.AMBIGUOUS
        assert signal in result.signals

    @pytest.mark.parametrize(
        "body",
        [
            "Ich bin bis zum 12.10. nicht im Büro.",
            "Sono attualmente fuori ufficio.",
            "Je suis actuellement absent.",
            "I am currently out of the office.",
        ],
    )
    def test_out_of_office_phrase_only_in_body_is_ambiguous(self, body: str) -> None:
        assert (
            classify_message({"From": SELLER, "Subject": "Re: Anfrage"}, body) == ReplyMessageType.AMBIGUOUS
        )

    def test_plain_reply_and_hostile_headers_never_raise(self) -> None:
        assert classify_message(reply_headers(), "Ja, noch verfügbar.") == ReplyMessageType.SELLER_REPLY
        hostile: dict[str, Any] = {
            "From": SELLER,
            "Subject": "Re: x\r\nBcc: victim@example.invalid",
            "bad name with spaces": "x",
            "X-Number": 5,
            "X-List": ["a", 1, None],
        }
        headers = MessageHeaders.from_raw(hostile)
        assert headers.get("subject") == "Re: x  Bcc: victim@example.invalid"
        assert "bad name with spaces" not in headers.values
        assert "x-number" not in headers.values
        assert headers.get_all("x-list") == ("a",)
        assert classify_message(hostile, "x") == ReplyMessageType.SELLER_REPLY

    def test_headers_must_be_a_mapping(self) -> None:
        with pytest.raises(ValidationFailed):
            MessageHeaders.from_raw(["From", SELLER])  # type: ignore[arg-type]


# =============================================================================================
# Addresses, Message-IDs, delivery reports
# =============================================================================================


class TestIdentifiers:
    def test_message_id_normalisation(self) -> None:
        assert normalize_message_id("<Abc.Def@Example.INVALID>") == "<Abc.Def@example.invalid>"
        assert normalize_message_id("  abc@example.invalid ") == "<abc@example.invalid>"
        for bad in (None, "", "no-at-sign", "<a b@example.invalid>", "<a@b@c>", "<<a@b>>", "<@b>", "a\r\n@b"):
            assert normalize_message_id(bad) is None

    def test_message_id_list_keeps_order_dedupes_and_bounds(self) -> None:
        refs = parse_message_id_list("<a@x.invalid> <b@x.invalid>\r\n <A@X.invalid> <a@x.invalid>")
        assert refs == ("<a@x.invalid>", "<b@x.invalid>", "<A@x.invalid>")
        many = " ".join(f"<m{i}@x.invalid>" for i in range(500))
        bounded = parse_message_id_list(many)
        assert len(bounded) == 200
        assert bounded[0] == "<m0@x.invalid>"
        assert bounded[-1] == "<m499@x.invalid>"

    def test_canonical_address_is_conservative(self) -> None:
        assert canonical_address("Seller.Name+tag@EXAMPLE.Invalid") == "Seller.Name+tag@example.invalid"
        assert canonical_address("<seller@example.invalid>") == "seller@example.invalid"
        assert canonical_address("a.b@gmail.com") != canonical_address("ab@gmail.com")  # no Gmail folding
        assert canonical_address("käufer@münchen.example") is None  # non-ASCII local part never verifies
        assert canonical_address("x@münchen.example") == "x@xn--mnchen-3ya.example"
        for bad in (None, "", "no-at", "a@b", "a..b@example.invalid", ".a@example.invalid", "a@-x.invalid"):
            assert canonical_address(bad) is None

    def test_address_lists(self) -> None:
        assert parse_address_list('"Seller, A" <seller@example.invalid>, b@example.invalid') == (
            "seller@example.invalid",
            "b@example.invalid",
        )
        assert parse_address_list("garbage<<>>") == (None,)
        assert parse_address_list(None) == ()

    def test_delivery_report_parsing(self) -> None:
        body = (
            "Reporting-MTA: dns; mx.example.invalid\n\n"
            "Final-Recipient: rfc822; seller@example.invalid\nAction: delayed\nAction: failed\n"
            "Status: 5.1.1\n\nMessage-ID: <synthetic-inquiry@Example.invalid>\n"
        )
        details = parse_delivery_report(body)
        assert details == BounceDetails(
            action="failed",
            status_code="5.1.1",
            permanent=True,
            final_recipients=("seller@example.invalid",),
            original_message_ids=("<synthetic-inquiry@example.invalid>",),
        )
        exchange = parse_delivery_report("Remote Server returned '550 5.7.1 rejected' from 10.1.1.1")
        assert exchange.status_code == "5.7.1"
        assert parse_delivery_report("Status: 4.4.7").permanent is False
        assert parse_delivery_report("nothing").status_code is None


# =============================================================================================
# Correlation (37.10: replies map by IDs/headers not subject; forwarded/changed address quarantined)
# =============================================================================================


class TestCorrelation:
    def test_in_reply_to_with_verified_sender_and_reference_matches(self) -> None:
        result = correlate_reply(message(reply_headers(), "Ja, noch verfügbar."), [binding(), OTHER_BINDING])
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.inquiry_id == INQ
        assert result.binding_version == 2
        assert result.sender_verified
        assert result.reference_corroborated is True
        assert result.upload_scope == "full"
        assert CorrelationReason.HEADER_REFERENCE_MATCH in result.reasons

    def test_references_header_alone_is_a_header_match(self) -> None:
        headers = reply_headers()
        del headers["In-Reply-To"]
        headers["References"] = f"<older@example.invalid> {OUT_ID}"
        assert correlate_reply(message(headers, "Ja"), [binding()]).outcome == CorrelationOutcome.MATCHED

    def test_header_match_without_listing_reference_still_matches_but_says_so(self) -> None:
        headers = reply_headers(Subject="Ihre Nachricht")
        result = correlate_reply(message(headers, "Ja."), [binding()])
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.reference_corroborated is False
        assert CorrelationReason.REFERENCE_NOT_FOUND in result.reasons

    def test_subject_match_alone_never_correlates(self) -> None:
        headers = {"From": "stranger@other.invalid", "Subject": "Re: Anfrage zu Example Trail – TEST-204"}
        result = correlate_reply(
            message(headers, "TEST-204 https://dealer.example/vehicles/TEST-204"), [binding()]
        )
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert result.upload_scope == "none"
        assert CorrelationReason.SUBJECT_ONLY in result.reasons

    def test_subject_match_from_verified_seller_is_only_a_quarantined_possible_match(self) -> None:
        headers = {"From": SELLER, "Subject": "Anfrage zu Example Trail – TEST-204"}
        result = correlate_reply(message(headers, "Ja"), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.SENDER_ONLY_NO_THREAD in result.reasons
        assert result.upload_scope == "quarantine"

    def test_changed_address_is_quarantined(self) -> None:
        headers = reply_headers(From="seller-new@example.invalid")
        result = correlate_reply(message(headers, "Ja"), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.CHANGED_ADDRESS in result.reasons
        assert result.inquiry_id == INQ

    def test_case_changed_local_part_is_not_assumed_equivalent(self) -> None:
        result = correlate_reply(message(reply_headers(From="Seller@Example.invalid"), "Ja"), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.CHANGED_ADDRESS in result.reasons

    @pytest.mark.parametrize("prefix", ["Fwd:", "FW:", "WG:", "I:", "TR:", "Re: Fwd:"])
    def test_forwarded_subject_is_quarantined(self, prefix: str) -> None:
        headers = reply_headers(Subject=f"{prefix} Anfrage TEST-204")
        result = correlate_reply(message(headers, "x"), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.FORWARDED in result.reasons

    @pytest.mark.parametrize(
        "marker",
        [
            "---------- Forwarded message ---------",
            "-------- Weitergeleitete Nachricht --------",
            "-------- Messaggio inoltrato --------",
            "-------- Message transféré --------",
        ],
    )
    def test_forwarded_body_marker_is_quarantined(self, marker: str) -> None:
        body = f"Siehe unten.\n{marker}\nVon: someone"
        assert is_forwarded("Re: Anfrage", body)
        result = correlate_reply(message(reply_headers(), body), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED

    def test_multiple_senders_are_quarantined(self) -> None:
        headers = reply_headers(From=f"{SELLER}, other@example.invalid")
        result = correlate_reply(message(headers, "x"), [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.AMBIGUOUS_SENDER in result.reasons

    def test_thread_id_only_needs_sender_and_reference(self) -> None:
        headers = {"From": SELLER, "Subject": "Auto"}
        weak = correlate_reply(message(headers, "Ja", thread="conv-1"), [binding()])
        assert weak.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.THREAD_ONLY_UNCORROBORATED in weak.reasons
        strong = correlate_reply(
            message({**headers, "Subject": "Auto TEST-204"}, "Ja", thread="conv-1"), [binding()]
        )
        assert strong.outcome == CorrelationOutcome.MATCHED

    def test_thread_id_of_another_provider_is_not_a_link(self) -> None:
        msg = message(
            {"From": SELLER, "Subject": "x"}, "Ja", thread="conv-1", provider=EmailProviderKind.GMAIL_API
        )
        result = correlate_reply(msg, [binding()])
        assert CorrelationReason.THREAD_MATCH not in result.reasons
        assert result.outcome != CorrelationOutcome.MATCHED

    def test_links_to_two_inquiries_are_quarantined_and_stay_local(self) -> None:
        headers = reply_headers(References=f"{OUT_ID} <other-inquiry@example.invalid>")
        result = correlate_reply(message(headers, "x"), [binding(), OTHER_BINDING])
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert set(result.candidate_inquiry_ids) == {INQ, INQ2}
        assert result.inquiry_id is None
        assert result.upload_scope == "none"

    def test_reference_to_another_inquiry_is_a_conflict(self) -> None:
        result = correlate_reply(
            message(reply_headers(Subject="Re: TEST-999"), "Ja"), [binding(), OTHER_BINDING]
        )
        assert result.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.CONFLICTING_REFERENCE in result.reasons

    def test_spam_in_thread_is_quarantined(self) -> None:
        result = correlate_reply(message(reply_headers(**{"X-Spam-Flag": "YES"}), "x"), [binding()])
        assert result.message_type == ReplyMessageType.SPAM
        assert result.outcome == CorrelationOutcome.QUARANTINED

    def test_bounce_correlates_by_returned_message_id_and_recipient(self) -> None:
        headers = {
            "From": "MAILER-DAEMON@mx.example.invalid",
            "Subject": "Undelivered Mail Returned to Sender",
            "Content-Type": "multipart/report; report-type=delivery-status",
        }
        body = f"Final-Recipient: rfc822; {SELLER}\nAction: failed\nStatus: 5.1.1\n\nMessage-ID: {OUT_ID}\n"
        result = correlate_reply(message(headers, body), [binding()])
        assert result.message_type == ReplyMessageType.BOUNCE
        assert result.outcome == CorrelationOutcome.MATCHED
        assert CorrelationReason.DSN_RECIPIENT_VERIFIED in result.reasons
        wrong = body.replace(SELLER, "someone-else@example.invalid")
        mismatch = correlate_reply(message(headers, wrong), [binding()])
        assert mismatch.outcome == CorrelationOutcome.QUARANTINED
        assert CorrelationReason.DSN_RECIPIENT_MISMATCH in mismatch.reasons

    def test_tombstoned_binding_never_grants_access(self) -> None:
        bindings = [
            binding(binding_version=2),
            binding(binding_version=3, state=InquiryBindingState.TOMBSTONED),
        ]
        result = correlate_reply(message(reply_headers(), "Ja"), bindings)
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert CorrelationReason.BINDING_REVOKED in result.reasons
        assert not result.retry_after_binding_sync
        assert result.upload_scope == "none"

    def test_newest_binding_version_wins(self) -> None:
        bindings = [
            binding(binding_version=1, state=InquiryBindingState.TOMBSTONED),
            binding(binding_version=4, verified_seller_aliases=("relay-7@market.example.invalid",)),
        ]
        result = correlate_reply(
            message(reply_headers(From="relay-7@market.example.invalid"), "Ja"), bindings
        )
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.binding_version == 4

    def test_two_payloads_under_one_version_are_treated_as_revoked(self) -> None:
        bindings = [binding(), binding(verified_seller_aliases=("other@example.invalid",))]
        assert (
            correlate_reply(message(reply_headers(), "Ja"), bindings).outcome == CorrelationOutcome.UNMATCHED
        )

    def test_cross_mailbox_injection_does_not_match(self) -> None:
        result = correlate_reply(message(reply_headers(), "Ja", mailbox=MB_OTHER), [binding()])
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert CorrelationReason.OTHER_MAILBOX_BINDING in result.reasons
        assert not result.retry_after_binding_sync

    def test_reply_before_binding_sync_is_retained_as_locator_only_then_surfaced(self) -> None:
        headers = reply_headers(**{"In-Reply-To": "<not-yet-synced@example.invalid>", "References": ""})
        result = correlate_reply(message(headers, "Ja"), [OTHER_BINDING])  # INQ's binding not synced yet
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert result.retry_after_binding_sync
        assert result.upload_scope == "none"
        keep = unmatched_retention(result, first_seen_locally_at=NOW, now=NOW + timedelta(hours=1))
        assert keep.retain_locator and not keep.surface_matching_gap
        assert keep.retry_until == NOW + timedelta(hours=24)
        expired = unmatched_retention(result, first_seen_locally_at=NOW, now=NOW + timedelta(hours=25))
        assert not expired.retain_locator and expired.surface_matching_gap
        # Once the binding arrives, the same message matches.
        synced = binding(outbound_message_ids=(OUT_ID, "<not-yet-synced@example.invalid>"))
        assert correlate_reply(message(headers, "Ja"), [synced]).outcome == CorrelationOutcome.MATCHED

    def test_unmatched_retention_only_for_waiting_messages(self) -> None:
        unrelated = correlate_reply(
            message({"From": "friend@private.invalid", "Subject": "Dinner"}, "x"), [binding()]
        )
        decision = unmatched_retention(unrelated, first_seen_locally_at=NOW, now=NOW)
        assert not decision.retain_locator and not decision.surface_matching_gap
        with pytest.raises(ValidationFailed):
            unmatched_retention(unrelated, first_seen_locally_at=NOW, now=NOW, window=timedelta(0))

    def test_uncertain_send_is_resolved_by_an_actual_reply(self) -> None:
        uncertain = binding(
            state=InquiryBindingState.UNCERTAIN, outbound_message_ids=(), send_intent_message_ids=(INTENT_ID,)
        )
        headers = reply_headers(**{"In-Reply-To": INTENT_ID, "References": INTENT_ID})
        result = correlate_reply(message(headers, "Ja"), [uncertain])
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.resolves_uncertain_send
        assert CorrelationReason.RESOLVES_UNCERTAIN_SEND in result.reasons
        # A quarantined possible match does not resolve it.
        changed = correlate_reply(message({**headers, "From": "x@example.invalid"}, "Ja"), [uncertain])
        assert changed.outcome == CorrelationOutcome.QUARANTINED
        assert not changed.resolves_uncertain_send
        # An already accepted inquiry has nothing to resolve.
        accepted = binding(send_intent_message_ids=(INTENT_ID,))
        assert not correlate_reply(message(headers, "Ja"), [accepted]).resolves_uncertain_send

    def test_auto_reply_in_thread_is_matched_as_auto_reply(self) -> None:
        headers = reply_headers(
            Subject="Abwesenheitsnotiz: Anfrage TEST-204", **{"Auto-Submitted": "auto-replied"}
        )
        result = correlate_reply(message(headers, "Ich bin nicht im Büro"), [binding()])
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.message_type == ReplyMessageType.AUTO_REPLY
        assert not should_emit_reply_signal(result)

    def test_unrelated_personal_mail_never_leaves_the_mailbox(self) -> None:
        msg = message({"From": "friend@private.invalid", "Subject": "Dinner"}, "Private text, IBAN DE89...")
        result = correlate_reply(msg, [binding()])
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert result.upload_scope == "none"
        with pytest.raises(Forbidden):
            build_ingest_request(
                msg,
                result,
                sanitized=sanitize_reply_body(msg.body_text),
                attachment_decisions=(),
                detected_language=None,
                observed_at=NOW,
            )

    def test_correlation_result_invariants(self) -> None:
        with pytest.raises(ValueError, match="spam and ambiguous"):
            CorrelationResult(
                outcome=CorrelationOutcome.MATCHED,
                message_type=ReplyMessageType.SPAM,
                inquiry_id=INQ,
                binding_version=1,
            )
        with pytest.raises(ValueError, match="exactly one inquiry"):
            CorrelationResult(outcome=CorrelationOutcome.MATCHED, message_type=ReplyMessageType.SELLER_REPLY)
        with pytest.raises(ValueError, match="uncertain send"):
            CorrelationResult(
                outcome=CorrelationOutcome.QUARANTINED,
                message_type=ReplyMessageType.SELLER_REPLY,
                resolves_uncertain_send=True,
            )

    def test_binding_validation(self) -> None:
        with pytest.raises(ValueError):
            binding(outbound_message_ids=("not a message id",))
        with pytest.raises(ValueError):
            binding(verified_seller_aliases=("not-an-address",))
        with pytest.raises(ValueError):
            binding(listing_references=("bad\nreference",))


# =============================================================================================
# Fingerprint, dedup and ingest idempotency (37.10: duplicate + moved message dedup; conflict)
# =============================================================================================


class TestDedup:
    def test_fingerprint_excludes_locators_observed_at_and_sync_metadata(self) -> None:
        first = spec_request()
        moved = spec_request(
            source_message={
                "outlook_entry_id": "moved-entry",
                "outlook_store_id": "moved-store",
                "received_at": "2026-10-06T19:00:00Z",
            },
            observed_at="2026-10-07T08:00:00Z",
            detected_language="de",
            binding_version=3,
            attachments=[],
        )
        assert first.fingerprint() == moved.fingerprint()
        assert first.dedup_key() == moved.dedup_key()
        assert first.locator() != moved.locator()

    def test_fingerprint_changes_with_content(self) -> None:
        base = spec_request()
        assert base.fingerprint() != spec_request(sanitized_body_text="The vehicle is sold.").fingerprint()
        assert base.fingerprint() != spec_request(subject="Other subject").fingerprint()
        assert base.fingerprint() != spec_request(headers={"from": "other@example.invalid"}).fingerprint()

    def test_fingerprint_normalisation_and_attachment_order(self) -> None:
        a, b = attachment("coc.pdf"), attachment("schein.pdf", digest=HASH_B)
        one = ReplySourceContent(body_text="Ja\r\nnoch da  \n", subject=" Re:  x ", attachments=(a, b))
        two = ReplySourceContent(body_text="Ja\nnoch da\n", subject="Re: x", attachments=(b, a))
        assert source_content_fingerprint(one) == source_content_fingerprint(two)
        changed = ReplySourceContent(
            body_text="Ja\nnoch da\n",
            subject="Re: x",
            attachments=(a, attachment("schein.pdf", digest="c" * 64)),
        )
        assert source_content_fingerprint(changed) != source_content_fingerprint(two)
        relabelled = ReplySourceContent(
            body_text="Ja\nnoch da\n",
            subject="Re: x",
            attachments=(a, b.model_copy(update={"local_ref": "another-local-ref"})),
        )
        assert source_content_fingerprint(relabelled) == source_content_fingerprint(two)

    def test_dedup_key_fallbacks(self) -> None:
        with_id = ReplySourceContent(internet_message_id="<a@x.invalid>", provider_message_id="p1")
        assert reply_dedup_key(MB, with_id).kind == "internet_message_id"
        provider_only = ReplySourceContent(provider_message_id="p1")
        assert reply_dedup_key(MB, provider_only).kind == "provider_message_id"
        content_only = ReplySourceContent(body_text="x")
        key = reply_dedup_key(MB, content_only)
        assert key.kind == "content_hash"
        assert key.value == source_content_fingerprint(content_only)
        assert key.as_string().startswith(f"{MB}:content_hash:")
        assert reply_dedup_key(MB_OTHER, content_only) != key

    def test_new_duplicate_and_moved_message(self) -> None:
        req = spec_request()
        new = decide_ingest(
            dedup_key=req.dedup_key(),
            idempotency_key="idem-key-0001",
            fingerprint=req.fingerprint(),
            locator=req.locator(),
            existing_by_dedup_key=None,
            existing_by_idempotency_key=None,
        )
        assert new.kind == IngestDecisionKind.NEW
        stored = StoredReplyIngest(
            reply_id=REPLY_ID,
            dedup_key=req.dedup_key().as_string(),
            idempotency_key="idem-key-0001",
            fingerprint=req.fingerprint(),
            locators=(req.locator(),),  # type: ignore[arg-type]
        )
        moved = spec_request(source_message={"outlook_entry_id": "after-move", "outlook_store_id": "store-2"})
        duplicate = decide_ingest(
            dedup_key=moved.dedup_key(),
            idempotency_key="idem-key-0002",
            fingerprint=moved.fingerprint(),
            locator=moved.locator(),
            existing_by_dedup_key=stored,
            existing_by_idempotency_key=None,
        )
        assert duplicate.kind == IngestDecisionKind.DUPLICATE
        assert duplicate.duplicate and duplicate.reply_id == REPLY_ID
        assert duplicate.locator_changed
        assert duplicate.record_locator == MessageLocator(
            outlook_entry_id="after-move", outlook_store_id="store-2"
        )
        replay = decide_ingest(
            dedup_key=req.dedup_key(),
            idempotency_key="idem-key-0001",
            fingerprint=req.fingerprint(),
            locator=req.locator(),
            existing_by_dedup_key=stored,
            existing_by_idempotency_key=stored,
        )
        assert replay.kind == IngestDecisionKind.DUPLICATE and not replay.locator_changed
        raise_for_ingest_conflict(replay)  # no exception

    def test_conflicting_content_under_same_identity_is_quarantined(self) -> None:
        req = spec_request()
        stored = StoredReplyIngest(
            reply_id=REPLY_ID,
            dedup_key=req.dedup_key().as_string(),
            idempotency_key="idem-key-0001",
            fingerprint=req.fingerprint(),
        )
        tampered = spec_request(sanitized_body_text="Different body under the same Message-ID.")
        decision = decide_ingest(
            dedup_key=tampered.dedup_key(),
            idempotency_key="idem-key-0003",
            fingerprint=tampered.fingerprint(),
            existing_by_dedup_key=stored,
            existing_by_idempotency_key=None,
        )
        assert decision.kind == IngestDecisionKind.CONFLICT
        assert decision.error_code == "IDEMPOTENCY_CONFLICT"
        assert decision.quarantine
        assert decision.reply_id == REPLY_ID
        with pytest.raises(IdempotencyConflict):
            raise_for_ingest_conflict(decision)

    def test_idempotency_key_reused_for_another_message_is_a_conflict(self) -> None:
        stored = StoredReplyIngest(
            reply_id=REPLY_ID, dedup_key="other-key", idempotency_key="idem-key-0001", fingerprint=HASH_A
        )
        decision = decide_ingest(
            dedup_key=spec_request().dedup_key(),
            idempotency_key="idem-key-0001",
            fingerprint=HASH_A,
            existing_by_dedup_key=None,
            existing_by_idempotency_key=stored,
        )
        assert decision.kind == IngestDecisionKind.CONFLICT and decision.quarantine

    def test_fingerprint_version_mismatch_requires_recompute(self) -> None:
        stored = StoredReplyIngest(
            reply_id=REPLY_ID,
            dedup_key="k",
            idempotency_key="idem-key-0001",
            fingerprint=HASH_A,
            fingerprint_version="reply-source-fingerprint/0",
        )
        decision = decide_ingest(
            dedup_key="k",
            idempotency_key="idem-key-0001",
            fingerprint=HASH_B,
            existing_by_dedup_key=stored,
            existing_by_idempotency_key=None,
        )
        assert decision.kind == IngestDecisionKind.FINGERPRINT_VERSION_MISMATCH

    def test_decide_ingest_validates_inputs(self) -> None:
        with pytest.raises(ValidationFailed):
            decide_ingest(
                dedup_key="k",
                idempotency_key="short",
                fingerprint=HASH_A,
                existing_by_dedup_key=None,
                existing_by_idempotency_key=None,
            )
        with pytest.raises(ValidationFailed):
            decide_ingest(
                dedup_key="k",
                idempotency_key="long-enough-key",
                fingerprint="not-a-hash",
                existing_by_dedup_key=None,
                existing_by_idempotency_key=None,
            )


class TestIngestRequest:
    def test_spec_fixture_validates(self) -> None:
        req = spec_request()
        assert req.headers.from_address == SELLER
        assert req.correlation_status == "matched"
        assert req.model_dump(mode="json", by_alias=True)["headers"]["from"] == SELLER

    @pytest.mark.parametrize(
        "overrides",
        [
            {"sanitized_body_text": "x" * (MAX_BODY_BYTES + 1)},
            {"subject": "s" * 513},
            {"subject": "line\r\nBcc: x@example.invalid"},
            {"attachments": [attachment(f"d{i}.pdf").model_dump() for i in range(21)]},
            {"attachments": [{**attachment("x.pdf").model_dump(), "filename": "../../etc/passwd"}]},
            {"attachments": [{**attachment("x.pdf").model_dump(), "local_ref": "https://evil.example/x"}]},
            {"headers": {"from": "Two <a@example.invalid>, b@example.invalid"}},
            {"headers": {"in_reply_to": "not a message id"}},
            {"source_message": {"internet_message_id": "<a b@c>"}},
            {"source_message": {"received_at": "2026-10-06T18:00:00"}},
            {"schema_version": "2.0"},
            {"inquiry_id": "not-a-uuid"},
            {"workspace_id": str(INQ)},
            {"headers": {"references": [f"<{'r' * 900}{i}@example.invalid>" for i in range(150)]}},
        ],
    )
    def test_invalid_requests_are_rejected(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            spec_request(**overrides)

    def test_build_ingest_request_for_matched_reply(self) -> None:
        pdf = attachment("coc.pdf")
        passport = attachment("passport_scan.jpg", mime="image/jpeg", digest=HASH_B)
        exe = attachment("invoice.pdf.exe", mime="application/octet-stream")
        msg = message(reply_headers(), "Ja, noch verfügbar. CoC anbei.", attachments=(pdf, passport, exe))
        corr = correlate_reply(msg, [binding()])
        decisions = evaluate_attachments(msg.attachments, body_text=msg.body_text)
        req = build_ingest_request(
            msg,
            corr,
            sanitized=sanitize_reply_body(msg.body_text),
            attachment_decisions=decisions,
            detected_language=MessageLanguage.DE,
            observed_at=NOW + timedelta(seconds=2),
        )
        assert req.inquiry_id == INQ and req.binding_version == 2 and req.mailbox_binding_id == MB
        assert [a.filename for a in req.attachments] == ["coc.pdf"]
        assert req.withheld_sensitive_attachments == 1
        assert req.headers.in_reply_to == OUT_ID
        assert req.correlation_status == "matched"
        assert req.source_message.outlook_entry_id == "synthetic-entry"

    def test_build_ingest_request_marks_quarantine_and_requires_received_time(self) -> None:
        msg = message(reply_headers(From="seller-new@example.invalid"), "Ja")
        corr = correlate_reply(msg, [binding()])
        req = build_ingest_request(
            msg,
            corr,
            sanitized=sanitize_reply_body(msg.body_text),
            attachment_decisions=(),
            detected_language=None,
            observed_at=NOW,
        )
        assert req.correlation_status == "quarantined"
        assert CorrelationReason.CHANGED_ADDRESS in req.correlation_reasons
        no_time = message(reply_headers(), "Ja", received_at=None)
        with pytest.raises(ValidationFailed):
            build_ingest_request(
                no_time,
                correlate_reply(no_time, [binding()]),
                sanitized=sanitize_reply_body("Ja"),
                attachment_decisions=(),
                detected_language=None,
                observed_at=NOW,
            )


# =============================================================================================
# Sanitising
# =============================================================================================


class TestSanitize:
    @pytest.mark.parametrize(
        "quote",
        [
            "Am 06.10.2026 um 18:00 schrieb Synthetic Sender <sender@example.invalid>:\n> Ist es verfügbar?",
            "Il giorno mar 6 ott 2026 alle ore 18:00 Sender <s@example.invalid> ha scritto:\n> Il veicolo?",
            "Le mar. 6 oct. 2026 à 18:00, Synthetic Sender <s@example.invalid> a écrit :\n> Le véhicole ?",
            "On Tue, Oct 6, 2026 at 6:00 PM Synthetic Sender <s@example.invalid>\nwrote:\n> Is it available?",
            "-----Original Message-----\nFrom: x\nIs the vehicle still available?",
            "-----Ursprüngliche Nachricht-----\nVon: x\nIst das Fahrzeug noch verfügbar?",
            "________________________________\nVon: Sender\nGesendet: Dienstag\nAn: seller\nBetreff: Anfrage",
            "Da: Sender\nInviato: martedì\nA: venditore\nOggetto: Richiesta\nIl veicolo è disponibile?",
        ],
    )
    def test_strips_clearly_delimited_quotes(self, quote: str) -> None:
        body = f"Leider schon verkauft.\n\n{quote}"
        clean = sanitize_reply_body(body)
        assert clean.quoted_text_removed
        assert clean.text == "Leider schon verkauft."

    def test_inline_quote_lines_are_removed_but_own_text_kept(self) -> None:
        text, removed = strip_quoted_text("> Ist das Fahrzeug noch verfügbar?\nJa.\n> Preis?\n2.600 €")
        assert removed
        assert text == "Ja.\n2.600 €"

    def test_removes_contact_pii_but_keeps_vehicle_facts(self) -> None:
        body = (
            "Ja, noch verfügbar. Preis 2.600 € VB, 150.000 km, EZ 05/2011, Termin am 06.10.2026 möglich.\n"
            "Schreiben Sie an seller.private@example.invalid oder rufen Sie 0171 1234567 an.\n"
            "WhatsApp: +49 171 7654321, Italia 333 1234567, Schweiz 079 123 45 67.\n"
            "IBAN DE89 3704 0044 0532 0130 00 für die Anzahlung.\n"
            "Abholung in der Musterstraße 12 oder Via Roma 1, oder 12 rue de la Paix.\n"
            "Unterlagen hier: https://files.example.invalid/share?token=abcdef\n"
            "Mit freundlichen Grüßen\nSynthetic Seller\nAutohaus Beispiel\n80331 München\nTel.: 089 / 123456"
        )
        clean = sanitize_reply_body(body)
        for secret in (
            "seller.private@",
            "1234567",
            "7654321",
            "079 123",
            "DE89",
            "Musterstraße 12",
            "Via Roma 1",
            "rue de la Paix",
            "80331",
            "token=",
            "123456",
        ):
            assert secret not in clean.text, secret
        for kept in (
            "2.600 € VB",
            "150.000 km",
            "05/2011",
            "06.10.2026",
            "Synthetic Seller",
            "Autohaus Beispiel",
        ):
            assert kept in clean.text, kept
        assert "[link removed: files.example.invalid]" in clean.text
        assert clean.removed.emails == 1
        assert clean.removed.bank_details == 1
        assert clean.removed.phones >= 4
        assert clean.removed.addresses >= 3
        assert clean.signature_block_detected
        for line in clean.text.split("\n"):
            problems = text_problems(line)
            assert "EMAIL_ADDRESS" not in problems and "PHONE_NUMBER" not in problems, line

    def test_drops_outbound_echo_and_device_footers(self) -> None:
        body = "Ja.\nIst das Fahrzeug noch verfügbar?\nVon meinem iPhone gesendet\nSent from my iPhone"
        clean = sanitize_reply_body(body, known_outbound_text=OUTBOUND_DE)
        assert clean.text == "Ja."
        assert clean.removed.outbound_echo_lines == 1
        assert clean.removed.device_footer_lines == 2

    def test_bounded_to_64_kib_and_idempotent(self) -> None:
        clean = sanitize_reply_body("Wort " * 40_000)
        assert clean.truncated
        assert len(clean.text.encode("utf-8")) <= MAX_BODY_BYTES
        assert clean.text.endswith("[truncated]")
        sample = (
            "Hallo\r\n\r\n\r\n\r\nPreis 2.600 €\u200b, Mail: a@example.invalid, https://www.example.invalid/x"
        )
        once = sanitize_reply_body(sample).text
        assert sanitize_reply_body(once).text == once
        assert "\u200b" not in once and "\r" not in once

    def test_numeric_link_hosts_do_not_leave_digits(self) -> None:
        assert sanitize_reply_body("siehe http://192.168.1.10/x").text == "siehe [link removed]"

    def test_empty_input(self) -> None:
        clean = sanitize_reply_body(None)
        assert clean.text == "" and not clean.quoted_text_removed and clean.original_chars == 0


# =============================================================================================
# Claim extraction (37.10: negations; quotes not accepted)
# =============================================================================================


def statuses(text: str, lang: str | None) -> list[AvailabilityClaimStatus]:
    return [c.status for c in extract_reply_claims(text, lang).availability]


class TestAvailabilityClaims:
    @pytest.mark.parametrize(
        ("text", "lang", "summary"),
        [
            ("Ja, das Fahrzeug ist noch verfügbar.", "de", "available"),
            ("Leider schon verkauft.", "de", "sold"),
            ("Das Auto ist reserviert.", "de", "reserved"),
            ("Das Auto ist nicht mehr verfügbar.", "de", "not_available"),
            ("La macchina è ancora disponibile.", "it", "available"),
            ("Mi dispiace, è già venduta.", "it", "sold"),
            ("L'auto è prenotata.", "it", "reserved"),
            ("Non è più disponibile.", "it", "not_available"),
            ("Le véhicule est toujours disponible.", "fr", "available"),
            ("Désolé, la voiture est déjà vendue.", "fr", "sold"),
            ("Elle est réservée.", "fr", "reserved"),
            ("Elle n'est plus disponible.", "fr", "not_available"),
            ("Yes, it is still available.", "en", "available"),
            ("Sorry, it has been sold.", "en", "sold"),
            ("The car is reserved.", "en", "reserved"),
            ("It is no longer available.", "en", "not_available"),
        ],
    )
    def test_four_languages(self, text: str, lang: str, summary: str) -> None:
        assert extract_reply_claims(text, lang).availability_summary == summary

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Das Auto ist nicht verkauft.", "de"),
            ("Die Unterlagen sind nicht reserviert.", "de"),
            ("La macchina non è venduta.", "it"),
            ("La voiture n'est pas vendue.", "fr"),
            ("It isn't sold.", "en"),
            ("The car is sold as seen.", "en"),
            ("Der Wagen wird nur an Händler verkauft.", "de"),
            ("The car will be sold to the highest bidder.", "en"),
            ("La macchina viene venduta senza garanzia.", "it"),
        ],
    )
    def test_negations_and_for_sale_wording_are_not_sold(self, text: str, lang: str) -> None:
        assert AvailabilityClaimStatus.SOLD not in statuses(text, lang)

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Das Auto ist noch nicht verkauft.", "de"),
            ("Non è ancora venduta.", "it"),
            ("Elle n'est pas encore vendue.", "fr"),
            ("It is not sold yet.", "en"),
        ],
    )
    def test_not_yet_sold_implies_available(self, text: str, lang: str) -> None:
        claims = extract_reply_claims(text, lang)
        assert claims.availability_summary == "available"
        assert all(c.implied for c in claims.availability)

    def test_consistent_combinations_collapse(self) -> None:
        sold = extract_reply_claims("Leider schon verkauft. Das Auto ist nicht mehr verfügbar.", "de")
        assert sold.availability_summary == "sold"
        reserved = extract_reply_claims("Noch nicht verkauft, aber reserviert.", "de")
        assert reserved.availability_summary == "reserved"
        assert "CONFLICTING_AVAILABILITY_STATEMENTS" not in reserved.warnings

    def test_contradiction_is_reported(self) -> None:
        claims = extract_reply_claims("Das Auto ist noch verfügbar. Es ist leider verkauft.", "de")
        assert claims.availability_summary == "conflicting"
        assert "CONFLICTING_AVAILABILITY_STATEMENTS" in claims.warnings

    def test_questions_and_conditionals_are_not_statements(self) -> None:
        assert statuses("Ist das Fahrzeug noch verfügbar?", "de") == []
        assert statuses("Falls noch verfügbar, melde ich mich.", "de") == []
        assert statuses("Se è ancora disponibile, la chiamo.", "it") == []
        assert statuses("If it is still available I will call.", "en") == []
        assert statuses("Ich weiß nicht, ob es noch verfügbar ist.", "de") == []
        assert statuses("Ja, noch verfügbar, wenn Sie wollen können Sie kommen.", "de") == [
            AvailabilityClaimStatus.AVAILABLE
        ]

    def test_quoted_inquiry_never_produces_claims(self) -> None:
        body = (
            f"Danke.\n\nAm 06.10.2026 um 18:00 schrieb Synthetic Sender <s@example.invalid>:\n{OUTBOUND_DE}"
        )
        claims = extract_reply_claims(body, "de")
        assert claims.availability == () and claims.prices == () and claims.documents == ()
        assert claims.requests == ()
        inline = "\n".join(f"> {line}" for line in OUTBOUND_DE.split("\n"))
        assert extract_reply_claims(f"Danke.\n{inline}", "de").documents == ()

    def test_unknown_language_applies_all_vocabularies(self) -> None:
        claims = extract_reply_claims("Già venduta.", None)
        assert claims.availability_summary == "sold"
        assert "LANGUAGE_UNKNOWN_ALL_VOCABULARIES_APPLIED" in claims.warnings
        assert claims.availability[0].confidence == "medium"
        assert extract_reply_claims("Schon verkauft.", "de-CH").language == MessageLanguage.DE
        assert extract_reply_claims("Schon verkauft.", "xx").language is None


class TestPriceClaims:
    def test_single_quote_with_negotiable_marker(self) -> None:
        claims = extract_reply_claims("Mein Preis ist 2.600 € VB.", "de", quoted_at=NOW)
        (price,) = claims.prices
        assert price.kind == "single"
        assert price.amount == Decimal("2600") and price.currency == "EUR"
        assert PriceCondition.NEGOTIABLE in price.conditions
        assert price.accepted is False and price.status == "unaccepted_seller_quote"
        assert price.quoted_at == NOW
        assert price.basis == PriceBasis.UNKNOWN  # never assumed gross

    @pytest.mark.parametrize(
        ("text", "lang", "amount", "currency", "condition"),
        [
            ("Letzter Preis 2.450 Euro, Festpreis.", "de", "2450", "EUR", PriceCondition.FIXED),
            ("Ultimo prezzo 2.500 €, non trattabile.", "it", "2500", "EUR", PriceCondition.FINAL_OR_LOWEST),
            ("Prezzo 2.700 euro trattabile.", "it", "2700", "EUR", PriceCondition.NEGOTIABLE),
            ("Dernier prix : 2 800 € TTC.", "fr", "2800", "EUR", PriceCondition.FINAL_OR_LOWEST),
            ("My lowest price is €2,650.", "en", "2650", "EUR", PriceCondition.FINAL_OR_LOWEST),
            ("Preis CHF 2'600.- bar.", "de", "2600", "CHF", PriceCondition.CASH_PAYMENT),
        ],
    )
    def test_quotes_in_four_languages(
        self, text: str, lang: str, amount: str, currency: str, condition: PriceCondition
    ) -> None:
        (price,) = extract_reply_claims(text, lang).prices
        assert price.amount == Decimal(amount)
        assert price.currency == currency
        assert condition in price.conditions

    def test_basis_wording(self) -> None:
        assert extract_reply_claims("Dernier prix : 2 800 € TTC.", "fr").prices[0].basis == PriceBasis.GROSS
        assert extract_reply_claims("Preis 2.300 € netto zzgl. MwSt.", "de").prices[0].basis == PriceBasis.NET
        assert extract_reply_claims("Prezzo 2.300 € + IVA.", "it").prices[0].basis == PriceBasis.NET

    @pytest.mark.parametrize(
        ("text", "lang", "low", "high"),
        [
            ("Preis 2.500 - 2.700 € je nach Zustand.", "de", "2500", "2700"),
            ("Zwischen 2.500 und 2.700 €.", "de", "2500", "2700"),
            ("Prezzo tra 2.500 e 2.700 euro.", "it", "2500", "2700"),
            ("Prix entre 2 500 et 2 700 €.", "fr", "2500", "2700"),
            ("Price between 2,500 and 2,700 EUR.", "en", "2500", "2700"),
        ],
    )
    def test_ranges(self, text: str, lang: str, low: str, high: str) -> None:
        (price,) = extract_reply_claims(text, lang).prices
        assert price.kind == "range"
        assert (price.low, price.high, price.currency) == (Decimal(low), Decimal(high), "EUR")

    def test_minimum_statement(self) -> None:
        claims = extract_reply_claims("Unter 2.800 € nicht. Also nicht unter 2.800 €.", "de")
        assert any(p.kind == "minimum" and p.amount == Decimal("2800") for p in claims.prices)

    def test_missing_currency_is_not_invented(self) -> None:
        (price,) = extract_reply_claims("Der Preis ist 2.600.", "de").prices
        assert price.currency is None
        assert "CURRENCY_NOT_STATED" in price.warnings

    def test_ambiguous_number_is_kept_unparsed(self) -> None:
        claims = extract_reply_claims("Preis 2,600 €.", "de")
        assert claims.prices == ()
        (mention,) = claims.other_amounts
        assert mention.amount is None and mention.raw == "2,600"
        assert "AMOUNT_NOT_PARSED" in mention.warnings

    def test_other_amounts_are_separate_from_the_quote(self) -> None:
        claims = extract_reply_claims(
            "Preis 2.600 €, Anzahlung 500 € per Überweisung. Transport 300 € extra. Früher statt 3.000 €.",
            "de",
        )
        assert [p.amount for p in claims.prices] == [Decimal("2600")]
        contexts = {(m.context, m.amount) for m in claims.other_amounts}
        assert (AmountContext.DEPOSIT_OR_PAYMENT, Decimal("500")) in contexts
        assert (AmountContext.OTHER_COST, Decimal("300")) in contexts
        assert (AmountContext.PREVIOUS_PRICE, Decimal("3000")) in contexts

    def test_included_costs_do_not_reclassify_the_price(self) -> None:
        (price,) = extract_reply_claims("Preis 2.600 € inkl. Überführung.", "de").prices
        assert price.amount == Decimal("2600")

    def test_non_money_numbers_are_ignored(self) -> None:
        claims = extract_reply_claims(
            "Erstzulassung 05/2011, 150.000 km, 103 kW, Baujahr 2011, Euro 5, 4x4, Termin 18:00.", "de"
        )
        assert claims.prices == () and claims.other_amounts == ()

    def test_questions_with_prices_are_not_quotes(self) -> None:
        assert extract_reply_claims("Würden Sie 2.400 € zahlen?", "de").prices == ()

    def test_quote_can_never_be_marked_accepted(self) -> None:
        evidence = extract_reply_claims("Preis 2.600 €", "de").prices[0].evidence
        with pytest.raises(ValueError):
            PriceQuoteClaim(kind="single", amount=Decimal(1), accepted=True, evidence=evidence)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            PriceQuoteClaim(kind="range", low=Decimal(3), high=Decimal(2), evidence=evidence)
        with pytest.raises(ValueError):
            PriceQuoteClaim(kind="single", amount=Decimal(0), evidence=evidence)

    def test_multiple_statements_warn(self) -> None:
        claims = extract_reply_claims("Lowest price is €2,650, not below 2,600 EUR.", "en")
        assert len(claims.prices) == 2
        assert "MULTIPLE_PRICE_STATEMENTS" in claims.warnings


class TestDocumentClaims:
    @pytest.mark.parametrize(
        ("text", "lang", "expected"),
        [
            ("Fahrzeugschein und CoC anbei.", "de", {(REG, "attached"), (COC, "attached")}),
            (
                "Der Fahrzeugbrief ist vorhanden, CoC nicht.",
                "de",
                {(REG, "available"), (COC, "not_available")},
            ),
            ("Unterlagen schicke ich keine, nur vor Ort.", "de", {(GEN, "refused")}),
            (
                "Il libretto di circolazione c'è, il CoC no.",
                "it",
                {(REG, "available"), (COC, "not_available")},
            ),
            ("In allegato il certificato di conformità.", "it", {(COC, "attached")}),
            ("Je n'envoie pas les documents, uniquement sur place.", "fr", {(GEN, "refused")}),
            ("Ci-joint la carte grise.", "fr", {(REG, "attached")}),
            ("I can send the registration documents.", "en", {(REG, "available")}),
            ("There is no CoC.", "en", {(COC, "not_available")}),
        ],
    )
    def test_four_languages(self, text: str, lang: str, expected: set[tuple[DocumentKind, str]]) -> None:
        claims = extract_reply_claims(text, lang, attachment_count=1)
        assert {(d.kind, d.status.value) for d in claims.documents} == expected

    def test_service_book_is_not_registration(self) -> None:
        claims = extract_reply_claims("Il libretto tagliandi è disponibile.", "it")
        assert {d.kind for d in claims.documents} == {DocumentKind.SERVICE_HISTORY}

    def test_attachment_claim_without_attachment_warns(self) -> None:
        claims = extract_reply_claims("CoC anbei.", "de", attachment_count=0)
        assert claims.documents[0].status == DocumentClaimStatus.ATTACHED
        assert "ATTACHMENT_CLAIMED_NOT_PRESENT" in claims.warnings

    def test_mentioned_without_status(self) -> None:
        claims = extract_reply_claims("Zum CoC sage ich später etwas.", "de")
        assert claims.documents[0].status == DocumentClaimStatus.MENTIONED
        assert InquiryQuestion.DOCUMENTS in claims.unanswered_questions()


class TestRequests:
    @pytest.mark.parametrize(
        ("text", "lang", "kind"),
        [
            ("Bitte überweisen Sie eine Anzahlung.", "de", RequestKind.PAYMENT),
            ("Serve una caparra tramite bonifico.", "it", RequestKind.PAYMENT),
            ("Merci de faire un virement d'acompte.", "fr", RequestKind.PAYMENT),
            ("Please send a deposit by bank transfer.", "en", RequestKind.PAYMENT),
            ("Ich kann es für Sie reservieren.", "de", RequestKind.RESERVATION),
            ("Posso prenotarla per lei.", "it", RequestKind.RESERVATION),
            ("Je peux la réserver pour vous.", "fr", RequestKind.RESERVATION),
            ("I can reserve it for you.", "en", RequestKind.RESERVATION),
            ("Schicken Sie mir eine Kopie Ihres Personalausweises.", "de", RequestKind.IDENTITY_DOCUMENT),
            ("Mi mandi la carta d'identità.", "it", RequestKind.IDENTITY_DOCUMENT),
            ("Envoyez-moi votre pièce d'identité.", "fr", RequestKind.IDENTITY_DOCUMENT),
            ("Please send a copy of your passport.", "en", RequestKind.IDENTITY_DOCUMENT),
            ("Wann möchten Sie zur Besichtigung kommen?", "de", RequestKind.APPOINTMENT),
            ("Possiamo fissare un appuntamento?", "it", RequestKind.APPOINTMENT),
            ("Voulez-vous prendre rendez-vous ?", "fr", RequestKind.APPOINTMENT),
            ("Come for a test drive tomorrow.", "en", RequestKind.APPOINTMENT),
            ("Ich brauche eine verbindliche Zusage.", "de", RequestKind.COMMITMENT),
            ("Serve un impegno scritto.", "it", RequestKind.COMMITMENT),
            ("Il faut signer le bon de commande.", "fr", RequestKind.COMMITMENT),
            ("We need a binding commitment.", "en", RequestKind.COMMITMENT),
            ("Sind Sie mit 2.600 € einverstanden?", "de", RequestKind.PRICE_ACCEPTANCE),
            ("Bitte keine weiteren Anfragen.", "de", RequestKind.OPT_OUT),
            ("Non contattatemi più.", "it", RequestKind.OPT_OUT),
            ("Ne me contactez plus.", "fr", RequestKind.OPT_OUT),
            ("Please do not contact me again.", "en", RequestKind.OPT_OUT),
            ("Das ist Spam.", "de", RequestKind.COMPLAINT),
        ],
    )
    def test_requests_are_flagged(self, text: str, lang: str, kind: RequestKind) -> None:
        assert kind in {r.kind for r in extract_reply_claims(text, lang).requests}

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Keine Anzahlung nötig.", "de"),
            ("No deposit needed.", "en"),
            ("Senza caparra.", "it"),
            ("Sans acompte.", "fr"),
            ("Der Preis ist MwSt. ausweisbar.", "de"),
            ("Es handelt sich um eine unverbindliche Anfrage.", "de"),
            ("Il y a une visite technique récente.", "fr"),
        ],
    )
    def test_negated_or_lookalike_wording_is_not_a_request(self, text: str, lang: str) -> None:
        kinds = {r.kind for r in extract_reply_claims(text, lang).requests}
        assert not kinds & {
            RequestKind.PAYMENT,
            RequestKind.IDENTITY_DOCUMENT,
            RequestKind.COMMITMENT,
            RequestKind.APPOINTMENT,
        }

    def test_escalation_and_opt_out_properties(self) -> None:
        claims = extract_reply_claims("Bitte Anzahlung überweisen. Bitte keine weiteren Anfragen.", "de")
        assert claims.escalation_kinds == (RequestKind.PAYMENT,)
        assert claims.opted_out
        assert not claims.complaint


# =============================================================================================
# Macedonian summary (37.10: MK summary preserves amounts/currency)
# =============================================================================================


class TestMkSummary:
    def test_preserves_amounts_currency_qualifications_and_unanswered_questions(self) -> None:
        claims = extract_reply_claims(
            "Noch verfügbar. Letzter Preis 2.600 € VB. Transport 350 € extra.", "de", quoted_at=NOW
        )
        summary = build_mk_summary(claims, MessageLanguage.DE)
        assert isinstance(summary, MkReplySummary)
        assert "2.600 EUR" in summary.text
        assert "350 EUR" in summary.text
        assert "германски" in summary.text
        assert "НЕ е прифатена" in summary.text
        assert "договорлива" in summary.text and "последна/најниска" in summary.text
        assert "противречни услови" in summary.text
        assert "2026-10-06" in summary.text
        assert summary.unanswered == (InquiryQuestion.DOCUMENTS,)
        assert "Неодговорени прашања: документи" in summary.text
        assert summary.amounts_preserved == ("2.600 EUR", "350 EUR")
        assert summary.translation_status == "structured_summary_only"
        assert summary.full_translation is None

    def test_ranges_chf_minimum_and_sold(self) -> None:
        claims = extract_reply_claims(
            "Prix entre 2 500 et 2 700 CHF. Pas en dessous de 2 400 CHF. La voiture est déjà vendue.", "fr"
        )
        summary = build_mk_summary(claims, MessageLanguage.FR)
        assert "2.500-2.700 CHF" in summary.text
        assert "не помалку од 2.400 CHF" in summary.text
        assert "продадено" in summary.text and "не утврдува купувач" in summary.text
        assert "француски" in summary.text

    def test_requests_attachments_and_nothing_answered(self) -> None:
        decisions = evaluate_attachments(
            (attachment("coc.pdf"), attachment("ausweis.jpg", mime="image/jpeg")), body_text=""
        )
        summary = build_mk_summary(
            extract_reply_claims("Bitte Anzahlung per Überweisung.", "de"), None, attachments=decisions
        )
        assert "непознат" in summary.text
        assert "уплата" in summary.text and "не одговара автоматски" in summary.text
        assert "1 чувствителни прилози задржани" in summary.text
        assert InquiryQuestion.DOCUMENTS not in summary.unanswered  # a vehicle document is attached
        assert InquiryQuestion.LOWEST_PRICE in summary.unanswered

    def test_seller_excerpts_are_sanitised(self) -> None:
        claims = extract_reply_claims("Preis 2.600 € - Infos unter https://x.example.invalid/a", "de")
        text = build_mk_summary(claims, MessageLanguage.DE).text
        assert "https://" not in text

    def test_format_amount_mk_is_exact(self) -> None:
        assert format_amount_mk(Decimal("2600")) == "2.600"
        assert format_amount_mk(Decimal("2600.50")) == "2.600,50"
        assert format_amount_mk(Decimal("1234567.01")) == "1.234.567,01"
        assert format_amount_mk(Decimal("-5")) == "-5"

    def test_full_translation_must_preserve_numbers(self) -> None:
        summary = build_mk_summary(ReplyClaims(language=MessageLanguage.DE), MessageLanguage.DE)
        original = "Preis 2.600 €, 150.000 km, Ref TEST-204"
        good = attach_full_translation(
            summary,
            original_text=original,
            translated_text="Цена 2.600 €, 150.000 km, реф. TEST-204",
            provider_id="approved-llm",
        )
        assert good.translation_status == "structured_summary_with_translation"
        assert good.full_translation is not None
        with pytest.raises(ValidationFailed):
            attach_full_translation(
                summary, original_text=original, translated_text="Цена 2.500 €", provider_id="approved-llm"
            )
        with pytest.raises(ValidationFailed):
            attach_full_translation(summary, original_text="x", translated_text="y", provider_id="")

    def test_translation_provider_protocol(self) -> None:
        class Fake:
            provider_id = "fake"

            def translate(
                self, text: str, *, source_language: MessageLanguage | None, target_language: str
            ) -> str:
                return text

        assert isinstance(Fake(), TranslationProvider)


# =============================================================================================
# Attachments (37.10: identity-document attachment quarantined)
# =============================================================================================


class TestAttachments:
    def test_allowed_vehicle_documents(self) -> None:
        files = (
            attachment("CoC.pdf"),
            attachment("Fahrzeugschein.jpg", mime="image/jpeg"),
            attachment("photo.png", mime="image/png"),
            attachment("IMG_0001.HEIC", mime="image/heic"),
        )
        decisions = evaluate_attachments(files)
        assert [d.action for d in decisions] == [AttachmentAction.ALLOW_VEHICLE_DOCUMENT] * 4
        assert decisions[0].document_kind == DocumentKind.COC
        assert decisions[1].document_kind == DocumentKind.REGISTRATION
        assert decisions[1].redaction_check_required
        assert all(d.malware_scan_required and d.forward_externally is False for d in decisions)
        assert decisions[0].safe_metadata is not None and decisions[0].safe_metadata["sha256"] == HASH_A

    @pytest.mark.parametrize(
        "name",
        [
            "Personalausweis_vorne.jpg",
            "Reisepass.pdf",
            "carta_identita.jpg",
            "passaporto.pdf",
            "carte d'identité.jpg",
            "passeport.pdf",
            "passport scan.jpg",
            "ID-card.png",
            "Führerschein.jpg",
            "patente.pdf",
        ],
    )
    def test_identity_documents_are_quarantined_and_never_forwarded(self, name: str) -> None:
        (decision,) = evaluate_attachments(
            (attachment(name, mime="image/jpeg" if name.endswith("jpg") else "application/pdf"),)
        )
        assert decision.action == AttachmentAction.QUARANTINE_SENSITIVE
        assert decision.forward_externally is False
        assert decision.safe_metadata is not None
        assert decision.safe_metadata["filename"] == "withheld-sensitive-attachment"
        assert "sha256" not in decision.safe_metadata and "local_ref" not in decision.safe_metadata

    def test_identity_mentioned_in_body_quarantines_unlabelled_images_only(self) -> None:
        files = (attachment("scan1.jpg", mime="image/jpeg"), attachment("coc.pdf"))
        decisions = evaluate_attachments(files, body_text="Anbei mein Personalausweis und das CoC.")
        assert decisions[0].action == AttachmentAction.QUARANTINE_SENSITIVE
        assert "IDENTITY_DOCUMENT_MENTIONED" in decisions[0].reasons
        assert decisions[1].action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT

    def test_financial_documents_are_quarantined(self) -> None:
        (decision,) = evaluate_attachments((attachment("Kontoauszug.pdf"),))
        assert decision.action == AttachmentAction.QUARANTINE_SENSITIVE

    def test_lookalike_names_are_not_identity_documents(self) -> None:
        (decision,) = evaluate_attachments((attachment("VW_Passat.jpg", mime="image/jpeg"),))
        assert decision.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT

    @pytest.mark.parametrize(
        ("meta", "reason"),
        [
            (attachment("x.exe", mime="application/octet-stream"), "MIME_NOT_ALLOWED"),
            (attachment("x.zip", mime="application/zip"), "MIME_NOT_ALLOWED"),
            (attachment("x.svg", mime="image/svg+xml"), "MIME_NOT_ALLOWED"),
            (attachment("x.html", mime="text/html"), "MIME_NOT_ALLOWED"),
            (attachment("coc.docm", mime="application/pdf"), "EXTENSION_MIME_MISMATCH"),
            (attachment("coc.exe.pdf"), "DANGEROUS_EXTENSION"),
            (attachment("coc.pdf", size=0), "EMPTY_FILE"),
            (attachment("coc.pdf", size=21 * 1024 * 1024), "FILE_TOO_LARGE"),
            (attachment("photo.jpg", mime="image/jpeg", size=16 * 1024 * 1024), "FILE_TOO_LARGE"),
        ],
    )
    def test_rejections(self, meta: AttachmentMeta, reason: str) -> None:
        (decision,) = evaluate_attachments((meta,))
        assert decision.action == AttachmentAction.REJECT
        assert reason in decision.reasons
        assert decision.safe_metadata is None

    def test_count_and_total_size_limits(self) -> None:
        many = tuple(attachment(f"doc{i}.pdf") for i in range(22))
        decisions = evaluate_attachments(many)
        assert [d.action for d in decisions[20:]] == [AttachmentAction.REJECT, AttachmentAction.REJECT]
        big = tuple(attachment(f"doc{i}.pdf", size=19 * 1024 * 1024) for i in range(3))
        sized = evaluate_attachments(big)
        assert sized[2].action == AttachmentAction.REJECT
        assert "TOTAL_SIZE_EXCEEDED" in sized[2].reasons

    def test_metadata_validation_and_safe_filenames(self) -> None:
        for bad in ("../x.pdf", "a/b.pdf", "a\\b.pdf", "x\x00.pdf", ".."):
            with pytest.raises(ValueError):
                attachment(bad)
        with pytest.raises(ValueError):
            AttachmentMeta(filename="x.pdf", mime_type="application/pdf", byte_size=1, sha256="XYZ")
        with pytest.raises(ValueError):
            AttachmentMeta(filename="x.pdf", mime_type="pdf", byte_size=1, sha256=HASH_A)
        assert attachment("x.pdf", mime="Application/PDF; name=x").mime_type == "application/pdf"
        assert safe_filename("C:\\Users\\x\\..\\coc.pdf") == "coc.pdf"
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename("") == "attachment"
        assert len(safe_filename("a" * 400 + ".pdf")) == 255
        assert safe_filename("a" * 400 + ".pdf").endswith(".pdf")


# =============================================================================================
# seller.reply.received.v1 signal
# =============================================================================================


class TestSignal:
    def test_minimal_payload(self) -> None:
        draft = build_seller_reply_signal(
            event_id=EVENT_ID,
            inquiry_id=INQ,
            reply_id=REPLY_ID,
            listing_id=LISTING_ID,
            dashboard_base_url="https://app.example/",
            occurred_at=NOW,
        )
        assert set(draft.payload) == {
            "schema_version",
            "event_id",
            "type",
            "event_name",
            "occurred_at",
            "inquiry_id",
            "reply_id",
            "listing_id",
            "dashboard_url",
            "status",
            "deduplication_key",
            "route",
        }
        assert draft.payload["dashboard_url"] == f"https://app.example/inquiries/{INQ}/replies/{REPLY_ID}"
        assert draft.payload["status"] == "seller reply received"
        assert draft.payload["occurred_at"] == "2026-10-06T18:00:00Z"
        assert draft.dedup_key == f"seller.reply.received:{REPLY_ID}"
        assert draft.route == "slack_seller_reply"
        assert draft.initial_state == OutboxState.PENDING and draft.blocker_code is None
        assert draft.payload_bytes < 1024

    def test_fixture_is_blocked_and_canary_is_marked(self) -> None:
        fixture = build_seller_reply_signal(
            event_id=EVENT_ID,
            inquiry_id=INQ,
            reply_id=REPLY_ID,
            listing_id=None,
            dashboard_base_url="https://app.example",
            occurred_at=NOW,
            is_fixture=True,
        )
        assert fixture.initial_state == OutboxState.BLOCKED and fixture.blocker_code == "FIXTURE_EVENT"
        assert fixture.payload["fixture"] is True
        canary = build_seller_reply_signal(
            event_id=EVENT_ID,
            inquiry_id=INQ,
            reply_id=REPLY_ID,
            listing_id=None,
            vehicle_cluster_id=LISTING_ID,
            dashboard_base_url="https://app.example",
            occurred_at=NOW,
            status=ReplySignalStatus.DECISION_NEEDED,
            is_canary=True,
        )
        assert canary.payload["canary"] is True and canary.initial_state == OutboxState.PENDING
        assert canary.payload["vehicle_cluster_id"] == str(LISTING_ID)
        assert canary.payload["status"] == "seller reply received; owner decision needed"

    @pytest.mark.parametrize(
        "base",
        [
            "http://app.example",
            "https://user:pw@app.example",
            "https://app.example/?token=x",
            "ftp://app.example",
            "",
        ],
    )
    def test_unsafe_dashboard_urls(self, base: str) -> None:
        with pytest.raises(ValidationFailed):
            dashboard_reply_url(base, INQ, REPLY_ID)
        assert dashboard_reply_url("http://localhost:8000", INQ, REPLY_ID).startswith(
            "http://localhost:8000/"
        )

    def test_status_must_be_fixed_vocabulary_and_routing(self) -> None:
        with pytest.raises(ValidationFailed):
            build_seller_reply_signal(
                event_id=EVENT_ID,
                inquiry_id=INQ,
                reply_id=REPLY_ID,
                listing_id=None,
                dashboard_base_url="https://app.example",
                occurred_at=NOW,
                status="Seller wrote: call 0171 1234567",  # type: ignore[arg-type]
            )
        assert reply_signal_route("slack") == "slack"
        assert reply_signal_route("disabled") is None
        with pytest.raises(ValidationFailed):
            reply_signal_route("mcp_events")


# =============================================================================================
# Processing decision (37.10: sold reply -> seller_reported_sold only; no auto follow-up)
# =============================================================================================


def matched(mtype: ReplyMessageType = ReplyMessageType.SELLER_REPLY, **kw: Any) -> CorrelationResult:
    return CorrelationResult(
        outcome=CorrelationOutcome.MATCHED, message_type=mtype, inquiry_id=INQ, binding_version=2, **kw
    )


class TestProcessing:
    def test_sold_reply_yields_seller_reported_sold_only(self) -> None:
        claims = extract_reply_claims("Leider schon verkauft.", "de")
        decision = decide_reply_processing(matched(), claims)
        evidence = decision.availability_evidence
        assert evidence is not None
        assert evidence.availability == Availability.SOLD_CLAIMED
        assert evidence.evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_SOLD
        assert evidence.establishes_purchase is False and evidence.realized_price is None
        assert decision.inquiry_transition == InquiryState.REPLIED
        assert decision.signal_status == ReplySignalStatus.SELLER_REPORTED_SOLD
        assert decision.auto_response is False and decision.follow_up is False
        assert decision.suppressions == ()

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("Bitte Anzahlung überweisen.", EscalationReason.PAYMENT_REQUEST),
            ("Ich kann es für Sie reservieren.", EscalationReason.RESERVATION_REQUEST),
            ("Bitte schicken Sie Ihren Personalausweis.", EscalationReason.IDENTITY_DOCUMENT_REQUEST),
            ("Wann kommen Sie zur Besichtigung?", EscalationReason.APPOINTMENT_REQUEST),
            ("Ich brauche eine verbindliche Zusage.", EscalationReason.COMMITMENT_REQUEST),
            ("Sind Sie mit 2.600 € einverstanden?", EscalationReason.PRICE_ACCEPTANCE_REQUEST),
        ],
    )
    def test_consequential_requests_escalate_and_never_auto_respond(
        self, text: str, reason: EscalationReason
    ) -> None:
        decision = decide_reply_processing(matched(), extract_reply_claims(text, "de"))
        assert decision.escalate_to_owner
        assert reason in decision.escalation_reasons
        assert decision.signal_status == ReplySignalStatus.DECISION_NEEDED
        assert decision.auto_response is False and decision.follow_up is False

    def test_price_quote_invalidates_valuation_but_is_not_accepted(self) -> None:
        claims = extract_reply_claims("Noch verfügbar, letzter Preis 2.500 €.", "de")
        decision = decide_reply_processing(matched(), claims)
        assert decision.invalidate_valuation and decision.queue_recalculation
        assert decision.availability_evidence is not None
        assert (
            decision.availability_evidence.evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE
        )
        assert any("unaccepted" in note for note in decision.notes)
        assert decision.counts_as_real_reply
        assert decision.emit_signal

    def test_opt_out_and_complaint_suppress_without_acknowledgement(self) -> None:
        opt = decide_reply_processing(matched(), extract_reply_claims("Bitte keine weiteren Anfragen.", "de"))
        assert opt.inquiry_transition == InquiryState.SELLER_OPTED_OUT
        assert opt.suppressions == (SuppressionReason.SELLER_OPT_OUT,)
        assert opt.auto_response is False
        complaint = decide_reply_processing(matched(), extract_reply_claims("Das ist Spam!", "de"))
        assert SuppressionReason.COMPLAINT in complaint.suppressions

    def test_bounces(self) -> None:
        hard = decide_reply_processing(
            matched(ReplyMessageType.BOUNCE),
            bounce=BounceDetails(action="failed", status_code="5.1.1", permanent=True),
        )
        assert hard.inquiry_transition == InquiryState.BOUNCED
        assert hard.suppressions == (SuppressionReason.HARD_BOUNCE,)
        assert not hard.emit_signal
        soft = decide_reply_processing(
            matched(ReplyMessageType.BOUNCE),
            bounce=BounceDetails(action="failed", status_code="4.2.2", permanent=False),
        )
        assert soft.inquiry_transition == InquiryState.BOUNCED and soft.suppressions == ()
        unknown = decide_reply_processing(matched(ReplyMessageType.BOUNCE))
        assert unknown.suppressions == (SuppressionReason.HARD_BOUNCE,)

    def test_auto_reply_and_delivery_notice_change_nothing(self) -> None:
        auto = decide_reply_processing(matched(ReplyMessageType.AUTO_REPLY))
        assert auto.inquiry_transition is None and not auto.emit_signal and not auto.apply_to_vehicle
        notice = decide_reply_processing(
            matched(ReplyMessageType.DELIVERY_NOTICE, resolves_uncertain_send=True)
        )
        assert notice.resolves_uncertain_send and notice.inquiry_transition is None

    def test_quarantined_and_unmatched_are_never_applied(self) -> None:
        quarantined = CorrelationResult(
            outcome=CorrelationOutcome.QUARANTINED,
            message_type=ReplyMessageType.SELLER_REPLY,
            inquiry_id=INQ,
            binding_version=2,
        )
        decision = decide_reply_processing(quarantined, extract_reply_claims("Schon verkauft.", "de"))
        assert not decision.apply_to_vehicle and decision.needs_verification
        assert decision.availability_evidence is None and not decision.emit_signal
        assert not should_emit_reply_signal(quarantined)
        unmatched = CorrelationResult(
            outcome=CorrelationOutcome.UNMATCHED, message_type=ReplyMessageType.SELLER_REPLY
        )
        assert not decide_reply_processing(unmatched).apply_to_vehicle

    def test_contradictory_reply_and_sensitive_attachment_escalate(self) -> None:
        claims = extract_reply_claims("Noch verfügbar. Leider verkauft.", "de")
        decisions = evaluate_attachments((attachment("Reisepass.pdf"),))
        decision = decide_reply_processing(matched(), claims, attachments=decisions)
        assert decision.availability_evidence is None
        assert EscalationReason.CONTRADICTORY_REPLY in decision.escalation_reasons
        assert EscalationReason.SENSITIVE_ATTACHMENT_WITHHELD in decision.escalation_reasons

    def test_not_available_is_shown_but_not_recorded_as_sold(self) -> None:
        decision = decide_reply_processing(matched(), extract_reply_claims("Nicht mehr verfügbar.", "de"))
        assert decision.availability_evidence is None
        assert decision.invalidate_valuation
        assert any("not recorded as sold" in note for note in decision.notes)

    def test_documents_attached_signal_status_and_canary(self) -> None:
        decisions = evaluate_attachments((attachment("coc.pdf"),))
        decision = decide_reply_processing(
            matched(is_canary=True), extract_reply_claims("Danke.", "de"), attachments=decisions
        )
        assert decision.signal_status == ReplySignalStatus.DOCUMENTS_ATTACHED
        assert not decision.counts_as_real_reply
        assert should_emit_reply_signal(matched())
