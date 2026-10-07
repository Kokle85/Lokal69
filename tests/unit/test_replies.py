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
from suv_deals.domain.inquiries import ALLOWED_TRANSITIONS
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


# =============================================================================================
# Review regressions (resumed package)
# =============================================================================================


class TestReviewRegressions:
    def test_uncertain_send_resolved_when_its_stable_message_id_is_also_outbound(self) -> None:
        uncertain = binding(
            state=InquiryBindingState.UNCERTAIN,
            outbound_message_ids=(INTENT_ID,),
            send_intent_message_ids=(INTENT_ID,),
        )
        headers = reply_headers(**{"In-Reply-To": INTENT_ID, "References": INTENT_ID})
        result = correlate_reply(message(headers, "Ja, TEST-204 ist noch da."), [uncertain])
        assert result.outcome == CorrelationOutcome.MATCHED
        assert result.resolves_uncertain_send
        decision = decide_reply_processing(result, extract_reply_claims("Ja, noch da.", "de"))
        assert decision.resolves_uncertain_send and decision.inquiry_transition == InquiryState.REPLIED

    def test_bounce_of_an_uncertain_send_proves_submission(self) -> None:
        uncertain = binding(
            state=InquiryBindingState.UNCERTAIN, outbound_message_ids=(), send_intent_message_ids=(INTENT_ID,)
        )
        headers = {
            "From": "MAILER-DAEMON@mx.example.invalid",
            "Subject": "Undelivered Mail Returned to Sender",
            "Content-Type": "multipart/report; report-type=delivery-status",
        }
        body = (
            f"Final-Recipient: rfc822; {SELLER}\nAction: failed\nStatus: 5.1.1\n\nMessage-ID: {INTENT_ID}\n"
        )
        result = correlate_reply(message(headers, body), [uncertain])
        assert result.outcome == CorrelationOutcome.MATCHED and result.resolves_uncertain_send
        decision = decide_reply_processing(result, bounce=parse_delivery_report(body))
        assert decision.inquiry_transition == InquiryState.BOUNCED
        assert decision.suppressions == (SuppressionReason.HARD_BOUNCE,)

    def test_thread_only_link_never_resolves_an_uncertain_send(self) -> None:
        uncertain = binding(
            state=InquiryBindingState.UNCERTAIN,
            outbound_message_ids=(),
            send_intent_message_ids=(INTENT_ID,),
            provider_thread_ids=("conv-1",),
        )
        result = correlate_reply(
            message({"From": SELLER, "Subject": "Anfrage TEST-204"}, "Ja", thread="conv-1"), [uncertain]
        )
        assert result.outcome == CorrelationOutcome.MATCHED
        assert not result.resolves_uncertain_send
        assert CorrelationReason.RESOLVES_UNCERTAIN_SEND not in result.reasons

    @pytest.mark.parametrize(
        "subject",
        [
            "Re: Anfrage zu Example Trail – TEST-204 (absence of rust?)",
            "AW: Abwesenheit von Mängeln – TEST-204",
            "R: assenza di ruggine",
            "RE : absence de rouille",
            "Re: vacation plans and the car",
        ],
    )
    def test_ordinary_words_after_a_reply_prefix_are_not_an_auto_reply(self, subject: str) -> None:
        assert classify_message({"From": SELLER, "Subject": subject}, "Ja.") == ReplyMessageType.SELLER_REPLY

    @pytest.mark.parametrize(
        "subject",
        ["Abwesend: Anfrage", "Assente: Richiesta", "Absent : Renseignements", "Urlaub: Anfrage zu X"],
    )
    def test_ordinary_words_as_subject_prefix_are_an_auto_reply(self, subject: str) -> None:
        assert classify_message({"From": SELLER, "Subject": subject}, "x") == ReplyMessageType.AUTO_REPLY

    def test_strong_auto_reply_phrase_counts_anywhere(self) -> None:
        subject = "Re: Anfrage (Automatic reply)"
        assert classify_message({"From": SELLER, "Subject": subject}, "x") == ReplyMessageType.AUTO_REPLY

    def test_hostile_header_named_values_never_breaks_parsing(self) -> None:
        msg = message({"values": {"x": 1}, "From": SELLER, "In-Reply-To": OUT_ID}, "Ja TEST-204")
        assert msg.headers.get("from") == SELLER
        assert correlate_reply(msg, [binding()]).outcome == CorrelationOutcome.MATCHED
        only_values = InboundMessage(
            identity=SourceMessageIdentity(mailbox_binding_id=MB, provider=EmailProviderKind.OUTLOOK_LOCAL),
            headers={"values": {"subject": 7}},  # type: ignore[arg-type]
        )
        assert only_values.headers.values == {}

    def test_headers_built_directly_are_cleaned_and_bounded(self) -> None:
        headers = MessageHeaders(
            values={
                "Subject": ("Re: Anfrage\r\nBcc: victim@example.invalid",),
                "bad name": ("x",),
                "X-Many": tuple(str(i) for i in range(200)),
            }
        )
        assert headers.get("subject") == "Re: Anfrage  Bcc: victim@example.invalid"
        assert "\n" not in (headers.get("subject") or "")
        assert headers.get("bad name") is None
        assert len(headers.get_all("x-many")) == 50
        serialised = InboundMessage(
            identity=SourceMessageIdentity(mailbox_binding_id=MB, provider=EmailProviderKind.OUTLOOK_LOCAL),
            headers=headers.model_dump(),  # type: ignore[arg-type]
        )
        assert serialised.headers == headers

    def test_money_like_number_next_to_a_stated_price_is_preserved_not_quoted(self) -> None:
        claims = extract_reply_claims("The car is 2,800 euros, not less than 2,600.", "en")
        (price,) = claims.prices
        assert price.amount == Decimal(2800) and price.currency == "EUR"
        (mention,) = claims.other_amounts
        assert mention.amount == Decimal(2600) and mention.currency is None
        assert mention.context == AmountContext.UNLABELLED
        assert "AMOUNT_WITHOUT_PRICE_CONTEXT" in mention.warnings
        summary = build_mk_summary(claims, MessageLanguage.EN)
        assert "2.800 EUR" in summary.text
        assert "2.600 (валута не е наведена)" in summary.text
        assert "2.600 (валута не е наведена)" in summary.amounts_preserved

    @pytest.mark.parametrize(
        ("text", "lang", "amount"),
        [
            ("Preis 2.900 €, Baujahr 2012, 150.000 km, 2 Schlüssel.", "de", "2900"),
            ("Preis ist 2.900, Kilometerstand 150000, 2 Vorbesitzer", "de", "2900"),
            ("Preis 2600, Kilometerstand: ca. 150.000", "de", "2600"),
            ("Il prezzo è 2.900, chilometri 150000, 5 porte", "it", "2900"),
            ("Prix 2900, kilométrage 150000, 2 propriétaires", "fr", "2900"),
            ("Lowest price 2600, mileage of about 145000, 2 owners", "en", "2600"),
            ("I have the registration papers and 2600 is my lowest price", "en", "2600"),
        ],
    )
    def test_mileage_years_and_counts_next_to_a_price_word_are_never_quotes(
        self, text: str, lang: str, amount: str
    ) -> None:
        claims = extract_reply_claims(text, lang)
        assert [p.amount for p in claims.prices] == [Decimal(amount)]
        assert all(p.accepted is False for p in claims.prices)
        assert claims.other_amounts == ()

    def test_implausibly_small_bare_number_is_not_a_quote(self) -> None:
        assert extract_reply_claims("Preis 2,8", "de").prices == ()

    def test_postcode_and_city_after_a_removed_street_are_removed(self) -> None:
        body = "Bonjour, le véhicule est vendu. Cordialement, Jean, 12 rue de la Paix, 75002 Paris, merci"
        clean = sanitize_reply_body(body)
        assert "75002" not in clean.text and "Paris" not in clean.text
        assert "rue de la Paix" not in clean.text
        assert "le véhicule est vendu" in clean.text
        assert sanitize_reply_body(clean.text).text == clean.text

    def test_ingest_request_carries_the_local_classification(self) -> None:
        headers = reply_headers(
            Subject="Abwesenheitsnotiz: Anfrage TEST-204", **{"Auto-Submitted": "auto-replied"}
        )
        msg = message(headers, "Ich bin nicht im Büro.")
        corr = correlate_reply(msg, [binding()])
        req = build_ingest_request(
            msg,
            corr,
            sanitized=sanitize_reply_body(msg.body_text),
            attachment_decisions=(),
            detected_language=MessageLanguage.DE,
            observed_at=NOW,
        )
        assert req.message_type == ReplyMessageType.AUTO_REPLY and req.correlation_status == "matched"
        # The classification is not source content: it never changes the fingerprint.
        assert (
            req.fingerprint()
            == req.model_copy(update={"message_type": ReplyMessageType.SELLER_REPLY}).fingerprint()
        )
        assert spec_request().message_type == ReplyMessageType.SELLER_REPLY  # spec shape default
        with pytest.raises(ValueError, match="quarantined"):
            spec_request(message_type="spam")
        assert spec_request(message_type="spam", correlation_status="quarantined").message_type == (
            ReplyMessageType.SPAM
        )

    def test_huge_bodies_are_scrubbed_on_a_bounded_prefix_and_marked_truncated(self) -> None:
        line = "Preis 2.600 € VB, Tel. 0171 1234567, a@b.example.invalid\n"
        clean = sanitize_reply_body(line * 20_000)  # ~1.1 MB
        assert clean.truncated and clean.text.endswith("[truncated]")
        assert len(clean.text.encode("utf-8")) <= MAX_BODY_BYTES
        assert "1234567" not in clean.text and "@" not in clean.text
        assert sanitize_reply_body(clean.text).text == clean.text
        short_after_scrub = sanitize_reply_body("x\n" * 70_000)  # long input, short output
        assert short_after_scrub.truncated and short_after_scrub.text.endswith("[truncated]")


# =============================================================================================
# Independent review (second round): each test pins a defect found and fixed in review
# =============================================================================================


class TestIndependentReviewRegressions:
    # --- dedup: moved/copied messages ---------------------------------------------------------

    def test_moved_graph_message_with_a_new_provider_id_is_a_duplicate_not_a_conflict(self) -> None:
        # Microsoft Graph message ids change when a message is moved; Gmail/Outlook copies in two
        # folders differ. Same Internet Message-ID + same content is the same message (37.10).
        first = spec_request(source_message={"provider_message_id": "graph-id-inbox"})
        moved = spec_request(
            source_message={
                "provider_message_id": "graph-id-after-rule-move",
                "outlook_entry_id": "other-entry",
            }
        )
        assert moved.fingerprint() == first.fingerprint()
        assert moved.dedup_key() == first.dedup_key()
        stored = StoredReplyIngest(
            reply_id=REPLY_ID,
            dedup_key=first.dedup_key().as_string(),
            idempotency_key="idem-key-0001",
            fingerprint=first.fingerprint(),
            locators=(first.locator(),),  # type: ignore[arg-type]
        )
        decision = decide_ingest(
            dedup_key=moved.dedup_key(),
            idempotency_key="idem-key-0002",
            fingerprint=moved.fingerprint(),
            locator=moved.locator(),
            existing_by_dedup_key=stored,
            existing_by_idempotency_key=None,
        )
        assert decision.kind == IngestDecisionKind.DUPLICATE and decision.reply_id == REPLY_ID
        assert decision.locator_changed and decision.record_locator == moved.locator()

    def test_provider_id_still_distinguishes_messages_without_an_internet_message_id(self) -> None:
        a = ReplySourceContent(provider_message_id="gmail-1", from_address=SELLER, body_text="Ja")
        b = a.model_copy(update={"provider_message_id": "gmail-2"})
        assert source_content_fingerprint(a) != source_content_fingerprint(b)
        assert reply_dedup_key(MB, a).kind == "provider_message_id"

    # --- classification: human subjects with delivery wording ------------------------------------

    @pytest.mark.parametrize(
        "subject",
        [
            "AW: Anfrage – leider nicht zustellbar?",
            "Re: Enquiry – returned mail?",
            "AW: Anfrage zu Example Trail – TEST-204 (Antwort verzögert)",
            "R: Richiesta – consegna ritardata",
            "RE : Renseignements – non remis ?",
            "Re: Delivery receipt for the car papers",
        ],
    )
    def test_delivery_wording_behind_a_human_reply_prefix_is_a_seller_reply(self, subject: str) -> None:
        headers = reply_headers(Subject=subject)
        assert classify_message(headers, "Das Auto ist verkauft.") == ReplyMessageType.SELLER_REPLY
        result = correlate_reply(message(headers, "Das Auto ist verkauft. TEST-204"), [binding()])
        decision = decide_reply_processing(result, extract_reply_claims("Das Auto ist verkauft.", "de"))
        # Never a hard bounce that would suppress the seller's address and lose the reply.
        assert decision.inquiry_transition == InquiryState.REPLIED
        assert SuppressionReason.HARD_BOUNCE not in decision.suppressions

    def test_structural_bounce_evidence_still_wins_over_a_reply_prefix(self) -> None:
        daemon = {"From": "MAILER-DAEMON@mx.example.invalid", "Subject": "AW: Anfrage"}
        assert classify_message(daemon, "Action: failed") == ReplyMessageType.BOUNCE
        failed = {"From": SELLER, "Subject": "Re: x", "X-Failed-Recipients": SELLER}
        assert classify_message(failed, "") == ReplyMessageType.BOUNCE
        ndr = {"From": "postmaster@mx.example.invalid", "Subject": "Undeliverable: RE: Anfrage"}
        assert classify_message(ndr, "") == ReplyMessageType.BOUNCE

    # --- correlation privacy --------------------------------------------------------------------

    @pytest.mark.parametrize(
        "item_class", ["IPM.Schedule.Meeting.Request", "IPM.Appointment", "IPM.Sharing", "IPM.Task"]
    )
    def test_non_mail_items_in_the_thread_never_leave_the_mailbox(self, item_class: str) -> None:
        msg = InboundMessage(
            identity=SourceMessageIdentity(
                mailbox_binding_id=MB,
                provider=EmailProviderKind.OUTLOOK_LOCAL,
                internet_message_id="<meeting@example.invalid>",
                received_at=NOW,
            ),
            headers=reply_headers(),
            body_text="Besichtigung Montag 10 Uhr, TEST-204",
            message_class=item_class,
        )
        result = correlate_reply(msg, [binding()])
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert result.reasons == (CorrelationReason.NON_MAIL_ITEM,)
        assert result.upload_scope == "none" and not should_emit_reply_signal(result)
        with pytest.raises(Forbidden):
            build_ingest_request(
                msg,
                result,
                sanitized=sanitize_reply_body(msg.body_text),
                attachment_decisions=(),
                detected_language=MessageLanguage.DE,
                observed_at=NOW,
            )

    def test_owner_messages_in_the_seller_thread_stay_local(self) -> None:
        own = "owner@example.invalid"
        headers = {
            "From": f"Owner <{own}>",
            "In-Reply-To": "<seller-reply@example.invalid>",
            "References": f"{OUT_ID} <seller-reply@example.invalid>",
            "Subject": "Re: Anfrage zu Example Trail – TEST-204",
        }
        msg = message(headers, "Danke, ich melde mich. TEST-204")
        # Without the owner's addresses it would be uploaded as a changed-address possible match.
        assert correlate_reply(msg, [binding()]).upload_scope == "quarantine"
        result = correlate_reply(msg, [binding()], own_addresses=(own,))
        assert result.outcome == CorrelationOutcome.UNMATCHED
        assert result.reasons == (CorrelationReason.OWN_MESSAGE,) and result.upload_scope == "none"
        # The seller's own reply is unaffected by the owner list.
        seller_reply = correlate_reply(message(reply_headers(), "Ja"), [binding()], own_addresses=(own,))
        assert seller_reply.outcome == CorrelationOutcome.MATCHED

    def test_owner_controlled_test_seller_address_stays_matchable(self) -> None:
        # Activation evidence uses an owner-controlled test address as the "seller" (37.10).
        test_seller = "owner-test@example.invalid"
        canary = binding(verified_seller_aliases=(test_seller,), is_canary=True)
        headers = reply_headers(From=test_seller)
        result = correlate_reply(message(headers, "Canary reply"), [canary], own_addresses=(test_seller,))
        assert result.outcome == CorrelationOutcome.MATCHED and result.is_canary
        decision = decide_reply_processing(result, extract_reply_claims("Canary reply", "en"))
        assert decision.counts_as_real_reply is False

    def test_verified_sender_without_thread_or_listing_reference_stays_local(self) -> None:
        msg = message(
            {"From": SELLER, "Subject": "Our October offers"}, "New cars every week!", msgid="<n@x.invalid>"
        )
        result = correlate_reply(msg, [binding()])
        assert result.outcome == CorrelationOutcome.UNMATCHED and result.upload_scope == "none"
        assert result.sender_verified
        assert CorrelationReason.SENDER_ONLY_NO_THREAD in result.reasons
        assert CorrelationReason.REFERENCE_NOT_FOUND in result.reasons
        with pytest.raises(Forbidden):
            build_ingest_request(
                msg,
                result,
                sanitized=sanitize_reply_body(msg.body_text),
                attachment_decisions=(),
                detected_language=MessageLanguage.EN,
                observed_at=NOW,
            )

    def test_verified_sender_with_listing_reference_is_only_a_quarantined_possible_match(self) -> None:
        msg = message(
            {"From": SELLER, "Subject": "Your question"}, "TEST-204 is sold.", msgid="<n@x.invalid>"
        )
        result = correlate_reply(msg, [binding()])
        assert result.outcome == CorrelationOutcome.QUARANTINED and result.inquiry_id == INQ
        assert result.upload_scope == "quarantine" and result.reference_corroborated
        decision = decide_reply_processing(result, extract_reply_claims(msg.body_text, "en"))
        assert decision.apply_to_vehicle is False and decision.availability_evidence is None
        # Two of this dealer's inquiries referenced: no single candidate, nothing leaves.
        second = binding(inquiry_id=INQ2, listing_references=("TEST-999",), listing_urls=())
        both = message(
            {"From": SELLER, "Subject": "TEST-204 and TEST-999"}, "Both sold.", msgid="<b@x.invalid>"
        )
        multi = correlate_reply(both, [binding(), second])
        assert multi.outcome == CorrelationOutcome.QUARANTINED and multi.upload_scope == "none"
        assert set(multi.candidate_inquiry_ids) == {INQ, INQ2}

    def test_listing_url_must_match_as_a_whole(self) -> None:
        short = binding(listing_references=(), listing_urls=("https://dealer.example/vehicles/123456",))
        longer = message(
            {"From": SELLER, "Subject": "Re"},
            "see https://dealer.example/vehicles/1234567",
            msgid="<u@x.invalid>",
        )
        assert correlate_reply(longer, [short]).outcome == CorrelationOutcome.UNMATCHED
        for text in (
            "see https://dealer.example/vehicles/123456.",
            "https://dealer.example/vehicles/123456?ref=mail",
            "dealer.example/vehicles/123456/details",
        ):
            same = message({"From": SELLER, "Subject": "Re"}, text, msgid="<u@x.invalid>")
            assert correlate_reply(same, [short]).outcome == CorrelationOutcome.QUARANTINED

    # --- quoted text ----------------------------------------------------------------------------

    def test_bottom_posted_reply_after_a_quoted_block_is_kept(self) -> None:
        body = (
            "Am 06.10.2026 um 10:00 schrieb Synthetic Sender <owner@example.invalid>:\n"
            "> Ist das Fahrzeug noch verfügbar?\n"
            "> Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?\n"
            "\n"
            "Ja, noch verfügbar. Letzter Preis 2.600 €.\n"
            "\n"
            "Gruß\nHans"
        )
        text, removed = strip_quoted_text(body)
        assert (
            removed and "Ja, noch verfügbar" in text and "niedrigster" not in text and "schrieb" not in text
        )
        clean = sanitize_reply_body(body)
        assert "Letzter Preis 2.600 €" in clean.text
        claims = extract_reply_claims(clean.text, "de")
        assert claims.availability_summary == "available"
        assert [(p.amount, p.currency) for p in claims.prices] == [(Decimal(2600), "EUR")]

    def test_unprefixed_quote_after_an_attribution_is_still_cut(self) -> None:
        body = "Ja.\n\nOn Mon, 6 Oct 2026 Synthetic Sender wrote:\nIs the vehicle still available?\nPrice?"
        assert strip_quoted_text(body) == ("Ja.\n", True)
        top_posted = "Yes, sold.\n\nOn Mon, X wrote:\n> Is it available?\n"
        text, _ = strip_quoted_text(top_posted)
        assert text.strip() == "Yes, sold." and "available" not in text

    # --- claims: signatures, disclaimers and boilerplate ----------------------------------------

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("The car is still available.\n\nKind regards\nDealer Ltd. All rights reserved.", "en"),
            ("È ancora disponibile.\nCordiali saluti\nQuesta email contiene informazioni riservate.", "it"),
            ("L'auto è ancora disponibile. Tutti i diritti riservati.", "it"),
            (
                "Toujours disponible.\nCordialement\n"
                "Ce courriel est réservé à l'usage exclusif du destinataire.",
                "fr",
            ),
            ("Le véhicule est toujours disponible, sous réserve de vente.", "fr"),
            (
                "Das Fahrzeug ist noch verfügbar.\nMit freundlichen Grüßen\n"
                "Autohaus\nFahrzeug reserviert? Nein.",
                "de",
            ),
        ],
    )
    def test_footer_and_disclaimer_wording_is_not_an_availability_statement(
        self, text: str, lang: str
    ) -> None:
        claims = extract_reply_claims(text, lang)
        assert claims.availability_summary == "available"
        result = correlate_reply(message(reply_headers(), text), [binding()])
        decision = decide_reply_processing(result, claims)
        assert decision.availability_evidence is not None
        assert decision.availability_evidence.availability == Availability.AVAILABLE
        assert EscalationReason.CONTRADICTORY_REPLY not in decision.escalation_reasons

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Prezzo 2.800 €, salvo il venduto.", "it"),
            ("Available unless sold.", "en"),
            ("Alle Angebote freibleibend, Zwischenverkauf vorbehalten.", "de"),
            ("Tutti i diritti riservati.", "it"),
            ("Tous droits réservés.", "fr"),
        ],
    )
    def test_prior_sale_and_rights_boilerplate_is_never_sold_or_reserved(self, text: str, lang: str) -> None:
        statuses = {c.status for c in extract_reply_claims(text, lang).availability}
        assert not statuses & {AvailabilityClaimStatus.SOLD, AvailabilityClaimStatus.RESERVED}

    def test_bank_details_in_the_signature_are_not_a_payment_request(self) -> None:
        text = (
            "Ja, das Fahrzeug ist noch verfügbar.\n\nMit freundlichen Grüßen\nAutohaus Example GmbH\n"
            "Bankverbindung: IBAN [bank details removed]\nZahlung per Überweisung"
        )
        claims = extract_reply_claims(text, "de")
        assert claims.requests == () and claims.escalation_kinds == ()
        assert claims.availability_summary == "available"
        # A payment request in the message itself still escalates.
        asked = extract_reply_claims("Bitte überweisen Sie 300 € Anzahlung.\nGruß\nHans", "de")
        assert RequestKind.PAYMENT in asked.escalation_kinds

    def test_postscript_after_the_sign_off_is_still_read(self) -> None:
        text = "Sì, è ancora disponibile.\nCordiali saluti\nMario\n\nPS: prezzo finale 2.600 €"
        claims = extract_reply_claims(text, "it")
        assert [(p.amount, p.currency) for p in claims.prices] == [(Decimal(2600), "EUR")]
        assert PriceCondition.FINAL_OR_LOWEST in claims.prices[0].conditions
        # A sign-off word as the very first line is not a signature start.
        assert extract_reply_claims("Regards\nThe car is still available.", "en").availability_summary == (
            "available"
        )

    # --- claims: qualifications, deposits, documents, interjections ----------------------------

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Für 2.500 € können Sie es haben, wenn Sie es diese Woche abholen.", "de"),
            ("If you pick it up this week, 2,500 EUR.", "en"),
            ("Se lo ritira subito, 2.500 €.", "it"),
            ("2 500 € si vous venez cette semaine.", "fr"),
        ],
    )
    def test_conditional_quote_keeps_its_qualification(self, text: str, lang: str) -> None:
        (price,) = extract_reply_claims(text, lang).prices
        assert price.amount == Decimal(2500) and price.currency == "EUR"
        assert PriceCondition.CONDITIONAL in price.conditions and price.accepted is False
        summary = build_mk_summary(extract_reply_claims(text, lang), MessageLanguage(lang))
        assert "условена понуда" in summary.text and "2.500 EUR" in summary.text

    def test_deposit_verb_is_a_deposit_and_a_payment_request_not_a_quote(self) -> None:
        text = "Ich kann Ihnen den Wagen für 2.700 Euro reservieren, wenn Sie 300 Euro anzahlen."
        claims = extract_reply_claims(text, "de")
        assert [p.amount for p in claims.prices] == [Decimal(2700)]
        (deposit,) = claims.other_amounts
        assert deposit.context == AmountContext.DEPOSIT_OR_PAYMENT and deposit.amount == Decimal(300)
        assert {RequestKind.PAYMENT, RequestKind.RESERVATION} <= set(claims.escalation_kinds)
        # The noun "Anzahl" (number of owners) is not a deposit.
        counted = extract_reply_claims("Anzahl Vorbesitzer: 2. Preis 2.800 €.", "de")
        assert [p.amount for p in counted.prices] == [Decimal(2800)] and counted.other_amounts == ()
        assert counted.requests == ()

    @pytest.mark.parametrize(
        ("text", "lang", "expected"),
        [
            ("Fahrzeugbrief ja, CoC nicht.", "de", {(REG, "mentioned"), (COC, "not_available")}),
            ("Fahrzeugbrief ist da, CoC nicht.", "de", {(REG, "available"), (COC, "not_available")}),
            (
                "Den Fahrzeugschein, den CoC und das Serviceheft habe ich.",
                "de",
                {(REG, "available"), (COC, "available"), (DocumentKind.SERVICE_HISTORY, "available")},
            ),
            (
                "Fahrzeugschein, CoC und Serviceheft fehlen.",
                "de",
                {
                    (REG, "not_available"),
                    (COC, "not_available"),
                    (DocumentKind.SERVICE_HISTORY, "not_available"),
                },
            ),
            ("CoC vorhanden, Fahrzeugbrief auch.", "de", {(COC, "available"), (REG, "available")}),
        ],
    )
    def test_document_status_is_shared_only_by_bare_list_items(
        self, text: str, lang: str, expected: set[tuple[DocumentKind, str]]
    ) -> None:
        claims = extract_reply_claims(text, lang)
        assert {(d.kind, d.status.value) for d in claims.documents} == expected

    def test_clause_initial_no_is_an_answer_not_a_negation(self) -> None:
        assert extract_reply_claims("No it's sold.", "en").availability_summary == "sold"
        assert extract_reply_claims("No it's not sold.", "en").availability_summary == "not_stated"
        assert extract_reply_claims("It is not sold.", "en").availability_summary == "not_stated"
        # Requests keep "no" as a negation.
        assert extract_reply_claims("No reservation needed.", "en").requests == ()

    # --- sanitising: bank details and the uploaded subject --------------------------------------

    def test_lower_case_iban_is_removed_only_with_a_valid_checksum(self) -> None:
        clean = sanitize_reply_body("iban de89370400440532013000 bitte")
        assert "370400440532013000" not in clean.text and clean.removed.bank_details == 1
        kept = sanitize_reply_body("Code de12abcd1234efgh")
        assert kept.text == "Code de12abcd1234efgh" and kept.removed.bank_details == 0

    def test_uploaded_subject_loses_contact_data_but_keeps_the_listing_reference(self) -> None:
        headers = reply_headers(
            Subject="Re: Anfrage – 412345678 – Tel. 0171 1234567 oder max@example.invalid"
        )
        msg = message(headers, "Ja, verfügbar. TEST-204")
        req = build_ingest_request(
            msg,
            correlate_reply(msg, [binding()]),
            sanitized=sanitize_reply_body(msg.body_text),
            attachment_decisions=(),
            detected_language=MessageLanguage.DE,
            observed_at=NOW,
        )
        assert "@" not in req.subject and "1234567" not in req.subject.replace("412345678", "")
        assert "412345678" in req.subject
        assert req.subject == "Re: Anfrage – 412345678 – Tel. [phone removed] oder [email removed]"

    # --- state-machine steps ----------------------------------------------------------------------

    def test_transition_path_follows_the_inquiry_state_machine(self) -> None:
        uncertain = binding(state=InquiryBindingState.UNCERTAIN, outbound_message_ids=(OUT_ID,))
        reply = correlate_reply(message(reply_headers(), "Ja, noch verfügbar."), [uncertain])
        decision = decide_reply_processing(reply, extract_reply_claims("Ja, noch verfügbar.", "de"))
        assert decision.transition_path(InquiryState.UNCERTAIN) == (
            InquiryState.ACCEPTED,
            InquiryState.REPLIED,
        )
        assert decision.transition_path(InquiryState.ACCEPTED) == (InquiryState.REPLIED,)
        assert decision.transition_path(InquiryState.NO_REPLY_YET) == (InquiryState.REPLIED,)
        assert decision.transition_path(InquiryState.REPLIED) == ()  # a second reply
        auto = correlate_reply(
            message(reply_headers(Subject="Automatic reply: Anfrage"), "Ich bin nicht im Büro."), [uncertain]
        )
        assert decide_reply_processing(auto).transition_path(InquiryState.UNCERTAIN) == (
            InquiryState.ACCEPTED,
        )
        bounce_headers = {"From": "MAILER-DAEMON@mx.example.invalid", "Subject": "Undelivered Mail"}
        bounce_body = (
            f"Final-Recipient: rfc822; {SELLER}\nAction: failed\nStatus: 5.1.1\nMessage-ID: {OUT_ID}\n"
        )
        bounce = decide_reply_processing(
            correlate_reply(message(bounce_headers, bounce_body), [binding()]),
            bounce=parse_delivery_report(bounce_body),
        )
        assert bounce.transition_path(InquiryState.REPLIED) == ()  # late bounce after a reply
        assert bounce.suppressions == (SuppressionReason.HARD_BOUNCE,)  # still suppresses
        opt_out = decide_reply_processing(reply, extract_reply_claims("Bitte keine weiteren Anfragen.", "de"))
        assert opt_out.transition_path(InquiryState.REPLIED) == (InquiryState.SELLER_OPTED_OUT,)
        # Every step this package proposes is legal in domain.inquiries.
        for current in InquiryState:
            for candidate in (decision, bounce, opt_out):
                state = current
                for step in candidate.transition_path(current):
                    assert step in ALLOWED_TRANSITIONS[state], (current, step)
                    state = step

    # --- Swiss registration documents (CH is a primary source market) ---------------------------

    @pytest.mark.parametrize(
        ("name", "mime"),
        [
            ("Fahrzeugausweis.pdf", "application/pdf"),
            ("Fahrzeug-Ausweis.pdf", "application/pdf"),
            ("permis de circulation.pdf", "application/pdf"),
            ("licenza di circolazione.jpg", "image/jpeg"),
        ],
    )
    def test_swiss_registration_document_is_a_vehicle_document_not_an_identity_document(
        self, name: str, mime: str
    ) -> None:
        (decision,) = evaluate_attachments((attachment(name, mime),), body_text="Anbei der Fahrzeug-Ausweis.")
        assert decision.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT
        assert decision.document_kind == REG and decision.redaction_check_required
        # Real identity documents are still withheld.
        for identity in ("Personalausweis.jpg", "permis de conduire.jpg", "Ausweis Kopie.jpg"):
            (withheld,) = evaluate_attachments((attachment(identity, "image/jpeg"),))
            assert withheld.action == AttachmentAction.QUARANTINE_SENSITIVE, identity

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Der Fahrzeugausweis ist vorhanden.", "de"),
            ("Den Fahrzeug-Ausweis schicke ich Ihnen.", "de"),
            ("Le permis de circulation est disponible.", "fr"),
            ("La licenza di circolazione è disponibile.", "it"),
        ],
    )
    def test_swiss_registration_wording_is_a_document_claim_not_an_identity_request(
        self, text: str, lang: str
    ) -> None:
        claims = extract_reply_claims(text, lang)
        assert {(d.kind, d.status) for d in claims.documents} == {(REG, DocumentClaimStatus.AVAILABLE)}
        assert claims.requests == ()
        assert InquiryQuestion.DOCUMENTS not in claims.unanswered_questions()
        assert extract_reply_claims("Bitte senden Sie mir eine Ausweiskopie.", "de").escalation_kinds == (
            RequestKind.IDENTITY_DOCUMENT,
        )


# =============================================================================================
# Independent review (third round): each test pins a defect found and fixed in review
# =============================================================================================


class TestThirdReviewRegressions:
    # --- Unicode hygiene: hostile/malformed characters never crash or reach storage -------------

    def test_lone_surrogates_are_repaired_never_a_poison_message(self) -> None:
        body = "Ja, noch verfügbar \ud800 TEST-204. Preis 2.800 €"
        clean = sanitize_reply_body(body)  # used to raise UnicodeEncodeError
        assert "\ud800" not in clean.text and "\N{REPLACEMENT CHARACTER}" in clean.text
        msg = message(reply_headers(Subject="Re: Anfrage \udc00 TEST-204"), body)
        result = correlate_reply(msg, [binding()])
        assert result.outcome == CorrelationOutcome.MATCHED
        req = build_ingest_request(
            msg,
            result,
            sanitized=clean,
            attachment_decisions=(),
            detected_language=MessageLanguage.DE,
            observed_at=NOW,
        )
        assert req.fingerprint() == source_content_fingerprint(req.source_content())
        assert [p.amount for p in extract_reply_claims(body, "de").prices] == [Decimal(2800)]
        assert source_content_fingerprint(ReplySourceContent(body_text="a\ud800")) == (
            source_content_fingerprint(ReplySourceContent(body_text="a\N{REPLACEMENT CHARACTER}"))
        )

    def test_bidi_and_c1_controls_never_reach_the_stored_or_analysed_text(self) -> None:
        # An RTL override makes "2.800" display as "008.2"; CSI (\x9b) is a terminal escape.
        body = "Preis \N{RIGHT-TO-LEFT OVERRIDE}2.800\N{POP DIRECTIONAL FORMATTING} € \x9b31m ok\x85Gruß"
        clean = sanitize_reply_body(body)
        assert clean.text == "Preis 2.800 €  31m ok\nGruß"
        assert [p.amount for p in extract_reply_claims(body, "de").prices] == [Decimal(2800)]
        for bad in ("\N{RIGHT-TO-LEFT OVERRIDE}", "\x9b", "\x85", "\N{LINE SEPARATOR}", "\N{SOFT HYPHEN}"):
            with pytest.raises(ValueError):
                spec_request(sanitized_body_text=f"Synthetic {bad} body")

    @pytest.mark.parametrize(
        "bad", ["\x85", "\x9b", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\N{RIGHT-TO-LEFT OVERRIDE}"]
    )
    def test_subject_never_carries_characters_the_database_rejects(self, bad: str) -> None:
        # app.seller_replies rejects [[:cntrl:]] (C0 and C1), NEL, LS and PS in the subject.
        headers = reply_headers(Subject=f"Re: Anfrage {bad}  zu  TEST-204")
        msg = message(headers, "Ja")
        req = build_ingest_request(
            msg,
            correlate_reply(msg, [binding()]),
            sanitized=sanitize_reply_body("Ja"),
            attachment_decisions=(),
            detected_language=MessageLanguage.DE,
            observed_at=NOW,
        )
        assert req.subject == "Re: Anfrage zu TEST-204"
        with pytest.raises(ValueError):
            spec_request(subject=f"Synthetic {bad} subject")

    @pytest.mark.parametrize("bad", ["\x85", "\x9b", "\N{LINE SEPARATOR}", "\N{RIGHT-TO-LEFT OVERRIDE}"])
    def test_attachment_names_with_controls_or_bidi_overrides_are_rejected_and_made_safe(
        self, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            attachment(f"invoice{bad}fdp.exe")
        safe = safe_filename(f"Fahrzeugschein{bad}.pdf")
        assert bad not in safe and safe.endswith(".pdf")
        assert attachment(safe).filename == safe

    @pytest.mark.parametrize("bad", ["\N{NO-BREAK SPACE}", "\N{EM SPACE}", "\x85", "\N{ZERO WIDTH SPACE}"])
    def test_provider_identifiers_reject_unicode_whitespace_and_controls(self, bad: str) -> None:
        # The database's opaque_ref_ok rejects [[:space:][:cntrl:]].
        with pytest.raises(ValueError):
            SourceMessageIdentity(
                mailbox_binding_id=MB, provider=EmailProviderKind.GMAIL_API, provider_message_id=f"id{bad}1"
            )
        with pytest.raises(ValueError):
            spec_request(source_message={"outlook_entry_id": f"entry{bad}1"})

    # --- claims: complaint/opt-out false positives -------------------------------------------

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Ihre Mail war im Spam-Ordner. Das Auto ist noch verfügbar.", "de"),
            ("Ihre Nachricht ist im Spam gelandet, das Auto ist noch verfügbar.", "de"),
            ("Your email went to my spam folder, the car is still available.", "en"),
            ("Found your message in spam - it is still available.", "en"),
            ("La sua mail era nello spam. È ancora disponibile.", "it"),
            ("Votre message était dans le dossier spam. Toujours disponible.", "fr"),
        ],
    )
    def test_spam_folder_report_is_not_a_complaint(self, text: str, lang: str) -> None:
        claims = extract_reply_claims(text, lang)
        assert not claims.complaint and claims.requests == ()
        decision = decide_reply_processing(
            correlate_reply(message(reply_headers(), text), [binding()]), claims
        )
        assert decision.inquiry_transition == InquiryState.REPLIED
        assert decision.suppressions == ()

    def test_real_complaints_still_suppress(self) -> None:
        for text, lang in (("Das ist Spam!", "de"), ("Stop this spam.", "en"), ("Questo è spam.", "it")):
            assert extract_reply_claims(text, lang).complaint, text

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Non scrivo il prezzo per email.", "it"),
            ("Non contattarmi su WhatsApp, solo email. È ancora disponibile.", "it"),
            ("Please do not contact me by phone, e-mail only.", "en"),
            ("Ich habe keine Nachrichten von Ihnen bekommen.", "de"),
            ("Wir können das Auto vor dem Export für Sie abmelden.", "de"),
            ("Ne me contactez plus par téléphone, seulement par e-mail.", "fr"),
        ],
    )
    def test_channel_preferences_and_lookalikes_are_not_opt_outs(self, text: str, lang: str) -> None:
        claims = extract_reply_claims(text, lang)
        assert not claims.opted_out
        decision = decide_reply_processing(
            correlate_reply(message(reply_headers(), text), [binding()]), claims
        )
        assert SuppressionReason.SELLER_OPT_OUT not in decision.suppressions
        assert decision.inquiry_transition == InquiryState.REPLIED

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Non contattatemi più.", "it"),
            ("Non mi scriva più, grazie.", "it"),
            ("Non scriveteci più.", "it"),
            ("Bitte keine weiteren Anfragen.", "de"),
            ("Bitte melden Sie mich ab.", "de"),
            ("Bitte keine E-Mails mehr.", "de"),
            ("Please do not contact me again.", "en"),
            ("Ne me contactez plus.", "fr"),
        ],
    )
    def test_genuine_opt_outs_are_still_honoured(self, text: str, lang: str) -> None:
        assert extract_reply_claims(text, lang).opted_out, text

    # --- claims: negated amounts are never quotes ---------------------------------------------

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Der Preis ist nicht 2.500 €, sondern 2.800 €.", "de"),
            ("Il prezzo non è 2.500 € ma 2.800 €.", "it"),
            ("Prezzo: non 2.500 € ma 2.800 €.", "it"),
            ("Ce n'est pas 2 500 € mais 2 800 €.", "fr"),
            ("The price is not 2,500 EUR but 2,800 EUR.", "en"),
        ],
    )
    def test_negated_amount_is_kept_as_a_negated_mention_not_a_quote(self, text: str, lang: str) -> None:
        claims = extract_reply_claims(text, lang)
        assert [(p.amount, p.currency) for p in claims.prices] == [(Decimal(2800), "EUR")]
        negated = [m for m in claims.other_amounts if m.context == AmountContext.NEGATED]
        assert [(m.amount, m.currency) for m in negated] == [(Decimal(2500), "EUR")]
        summary = build_mk_summary(claims, MessageLanguage(lang))
        assert "2.800 EUR" in summary.text

    def test_negated_mention_is_preserved_in_the_mk_summary(self) -> None:
        claims = extract_reply_claims("Nicht 2.500 €, sondern 2.800 €.", "de")
        (negated,) = claims.other_amounts
        assert negated.context == AmountContext.NEGATED and negated.amount == Decimal(2500)
        summary = build_mk_summary(claims, MessageLanguage.DE)
        assert "2.500 EUR" in summary.text and "не е понуда" in summary.text
        assert "2.500 EUR" in summary.amounts_preserved
        # "nicht unter" stays a minimum, never a negation.
        (minimum,) = extract_reply_claims("Nicht unter 2.700 €.", "de").prices
        assert minimum.kind == "minimum" and minimum.amount == Decimal(2700)

    # --- sanitising: obfuscated e-mail addresses -----------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "Schreiben Sie an max (at) example (dot) invalid",
            "mail: max[at]example.invalid",
            "max {at} example [punkt] invalid",
            "scrivete a mario (chiocciola) example (punto) invalid",
            "max at example dot invalid",
        ],
    )
    def test_obfuscated_email_addresses_are_removed(self, text: str) -> None:
        clean = sanitize_reply_body(text)
        assert "example" not in clean.text and "[email removed]" in clean.text
        assert clean.removed.emails == 1
        headers = reply_headers(Subject=f"Re: TEST-204 {text}")
        msg = message(headers, "Ja")
        req = build_ingest_request(
            msg,
            correlate_reply(msg, [binding()]),
            sanitized=sanitize_reply_body("Ja"),
            attachment_decisions=(),
            detected_language=MessageLanguage.DE,
            observed_at=NOW,
        )
        assert "example" not in req.subject and "TEST-204" in req.subject

    def test_ordinary_text_with_at_is_kept(self) -> None:
        text = "Look at the car at 10:00. Preis (ab) 2.800 €"
        assert sanitize_reply_body(text).text == text

    # --- claims: listing references and sentence ends are never prices --------------------------

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("Ja, noch verfügbar TEST-204. Preis 2.800 €", "de"),  # sentence ending in a number
            ("Ja, noch verfügbar. Preis 2.800 €, Inserat TEST-204", "de"),
            ("Inserat 412345678 kostet 2.800 €", "de"),
            ("Ref. 55123, Preis 2.800 €", "de"),
            ("Rif. 55123 - prezzo 2.800 €", "it"),
            ("Annonce AB-77812 : prix 2 800 €", "fr"),
            ("Listing 99231 is still available, price 2,800 EUR", "en"),
        ],
    )
    def test_listing_reference_digits_are_never_a_quote(self, text: str, lang: str) -> None:
        claims = extract_reply_claims(text, lang)
        assert [(p.amount, p.currency) for p in claims.prices] == [(Decimal(2800), "EUR")]
        assert "MULTIPLE_PRICE_STATEMENTS" not in claims.warnings

    def test_full_stop_after_a_number_ends_the_sentence_before_a_capital(self) -> None:
        claims = extract_reply_claims("Der Preis ist 2.900. Das Auto hat 150.000 km. Baujahr 2012.", "de")
        assert [p.amount for p in claims.prices] == [Decimal(2900)]
        assert claims.other_amounts == ()
        # A currency-less number far beyond any vehicle price is not a quote either.
        assert extract_reply_claims("Preis 412345678", "de").prices == ()
