"""Unit tests for the seller-inquiry MIME builder (spec 37.1, 37.3, 37.5)."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from uuid import UUID

import pytest
from pydantic import ValidationError

from suv_deals.domain.seller_templates import (
    RenderedMessage,
    build_vehicle_label,
    render,
    render_preview_mk,
    validate_scope,
)
from suv_deals.integrations.mime_builder import (
    CONTENT_TYPE_VALUE,
    INQUIRY_REF_HEADER,
    MAX_HEADER_LINE_OCTETS,
    MIME_BUILDER_VERSION,
    BuiltMessage,
    InquiryMessageRef,
    MimeBuildError,
    OutboundInquiryMessage,
    build_inquiry_message,
    build_message,
    built_message_problems,
    inquiry_message_id,
    inquiry_ref_value,
    mailbox,
    parse_inquiry_message_id,
    parse_inquiry_ref,
    verify_built_message,
)

INQUIRY_ID = UUID("66666666-6666-4666-8666-666666666666")
HEX_ID = UUID("abcdef01-2345-4678-89ab-cdef01234567")
SENDER_NAME = "Vasko Kičevski"
SENDER = "vasko.sender@example.com"
SELLER = "verkauf@autohaus-example.de"
LISTING_URL = "https://www.example.de/fahrzeuge/123456"
WHEN = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
TEMPLATES = ("seller_initial_de_v1", "seller_initial_it_v1", "seller_initial_fr_v1", "seller_initial_en_v1")


def rendered(template_id: str = "seller_initial_de_v1", name: str = SENDER_NAME) -> RenderedMessage:
    return render(
        template_id,
        build_vehicle_label("Toyota", "RAV4", "XA30"),
        "ABC-123",
        LISTING_URL,
        name,
        verified_listing_url=LISTING_URL,
    )


def built(
    template_id: str = "seller_initial_de_v1",
    *,
    attempt: int = 1,
    reply_to: str | None = None,
    encoding: str = "quoted-printable",
    recipient: str = SELLER,
) -> BuiltMessage:
    return build_inquiry_message(
        rendered(template_id),
        inquiry_id=INQUIRY_ID,
        attempt_number=attempt,
        sender=mailbox(SENDER, SENDER_NAME),
        recipient_address=recipient,
        reply_to_address=reply_to,
        date=WHEN,
        transfer_encoding=encoding,  # type: ignore[arg-type]
    )


def parse(raw: bytes):  # type: ignore[no-untyped-def]
    return BytesParser(policy=policy.default).parsebytes(raw)


def wire(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def header_names(raw: bytes) -> list[str]:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    return [line.split(":", 1)[0] for line in head.split("\r\n") if line and line[0] not in " \t"]


# --------------------------------------------------------------------------------------------- shape


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_inquiry_message_has_exactly_the_allowed_shape(template_id: str) -> None:
    message = built(template_id)
    parsed = parse(message.raw)
    assert header_names(message.raw) == [
        "From",
        "To",
        "Subject",
        "Date",
        "Message-ID",
        INQUIRY_REF_HEADER,
        "MIME-Version",
        "Content-Type",
        "Content-Transfer-Encoding",
    ]
    assert len(parsed["To"].addresses) == 1
    assert parsed["To"].addresses[0].addr_spec == SELLER
    assert parsed["To"].addresses[0].display_name == ""
    assert parsed["Cc"] is None and parsed["Bcc"] is None and parsed["Reply-To"] is None
    assert parsed["In-Reply-To"] is None and parsed["References"] is None
    assert not parsed.is_multipart()
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content_charset() == "utf-8"
    assert parsed.get_filename() is None and parsed.get_content_disposition() is None
    assert str(parsed["From"]) == f"{SENDER_NAME} <{SENDER}>"
    assert str(parsed["Subject"]) == message.subject
    assert parsed.get_content().replace("\r\n", "\n") == wire(message.body)
    assert message.builder_version == MIME_BUILDER_VERSION
    assert built_message_problems(message) == ()


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_built_message_matches_rendered_template_and_passes_scope(template_id: str) -> None:
    source = rendered(template_id)
    message = built(template_id)
    assert message.body_hash == source.body_hash
    assert message.subject == source.subject
    assert message.body == source.body
    result = validate_scope(source, envelope=message.envelope())
    assert result.ok, result.problems
    envelope = message.envelope()
    assert envelope.to == (SELLER,)
    assert envelope.cc == () and envelope.bcc == () and envelope.attachments == ()
    assert envelope.extra_headers["Content-Type"] == CONTENT_TYPE_VALUE
    assert INQUIRY_REF_HEADER not in envelope.extra_headers  # technical header, validated here


def test_bytes_are_deterministic_and_hash_identifies_them() -> None:
    first, second = built(), built()
    assert first.raw == second.raw
    assert first.raw_sha256 == second.raw_sha256 == hashlib.sha256(first.raw).hexdigest()
    assert first.size_bytes == len(first.raw)
    third = built(attempt=2)
    assert third.raw != first.raw
    assert third.rfc_message_id != first.rfc_message_id
    assert third.body_hash == first.body_hash


def test_crlf_line_endings_and_line_lengths() -> None:
    raw = built().raw
    lines = raw.split(b"\r\n")
    assert all(b"\n" not in line and b"\r" not in line for line in lines)
    assert all(len(line) <= MAX_HEADER_LINE_OCTETS for line in lines)
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert all(len(line) <= 78 for line in head.split(b"\r\n"))
    assert all(byte < 0x80 for byte in head)


def test_quoted_printable_and_base64_bodies() -> None:
    qp = built()
    assert b"Content-Transfer-Encoding: quoted-printable" in qp.raw
    assert b"verf=C3=BCgbar" in qp.raw
    b64 = built(encoding="base64")
    assert b"Content-Transfer-Encoding: base64" in b64.raw
    assert parse(b64.raw).get_content().replace("\r\n", "\n") == wire(b64.body)
    assert b64.body_hash == qp.body_hash and b64.raw != qp.raw


def test_non_ascii_subject_and_display_name_are_rfc2047_encoded() -> None:
    message = built()
    head = message.raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    assert "=?utf-8?" in head  # "\u2013" in the subject and "č" in the display name
    assert str(parse(message.raw)["Subject"]) == "Anfrage zu Toyota RAV4 XA30 \u2013 ABC-123"


def test_ascii_subject_is_not_encoded() -> None:
    spec = OutboundInquiryMessage(
        inquiry_id=INQUIRY_ID,
        attempt_number=1,
        sender=mailbox(SENDER, "Vasko K"),
        recipient=mailbox(SELLER),
        subject="Enquiry about Toyota RAV4 - ABC-123",
        body="Hello,\n\nIs the vehicle still available?\n\nVasko K",
        date=WHEN,
    )
    message = build_message(spec)
    assert b"Subject: Enquiry about Toyota RAV4 - ABC-123\r\n" in message.raw


def test_message_id_and_inquiry_ref_headers() -> None:
    message = built(attempt=3)
    assert message.rfc_message_id == f"<inquiry-{INQUIRY_ID}.3@example.com>"
    parsed = parse(message.raw)
    assert str(parsed["Message-ID"]) == message.rfc_message_id
    assert str(parsed[INQUIRY_REF_HEADER]) == f"inquiry-{INQUIRY_ID}"
    assert parse_inquiry_ref(str(parsed[INQUIRY_REF_HEADER])) == INQUIRY_ID
    assert message.inquiry_ref == inquiry_ref_value(INQUIRY_ID)
    assert message.sender_domain == "example.com"


def test_message_id_round_trip_and_strict_parsing() -> None:
    value = inquiry_message_id(INQUIRY_ID, 7, "example.com")
    assert parse_inquiry_message_id(value) == InquiryMessageRef(
        inquiry_id=INQUIRY_ID, attempt_number=7, domain="example.com"
    )
    for bad in (
        None,
        "",
        f"inquiry-{INQUIRY_ID}.1@example.com",  # no angle brackets
        f"<inquiry-{str(HEX_ID).upper()}.1@example.com>",
        f"<inquiry-{INQUIRY_ID}.0@example.com>",
        f"<inquiry-{INQUIRY_ID}.100@example.com>",
        f"<inquiry-{INQUIRY_ID}.01@example.com>",
        f"<inquiry-{INQUIRY_ID}@example.com>",
        f"<enquiry-{INQUIRY_ID}.1@example.com>",
        f"<inquiry-{INQUIRY_ID}.1@localhost>",
        f"<inquiry-{INQUIRY_ID}.1@EXAMPLE.com>",
        f"<inquiry-{INQUIRY_ID}.1@example.com> <x@y.z>",
    ):
        assert parse_inquiry_message_id(bad) is None, bad


@pytest.mark.parametrize("attempt", [0, -1, 100, 1000])
def test_message_id_attempt_bounds(attempt: int) -> None:
    with pytest.raises(MimeBuildError) as exc:
        inquiry_message_id(INQUIRY_ID, attempt, "example.com")
    assert exc.value.problems == ("ATTEMPT_NUMBER_INVALID",)


@pytest.mark.parametrize("domain", ["", "localhost", "exa mple.com", "-bad.com", "a..b", "x" * 254 + ".com"])
def test_message_id_domain_validation(domain: str) -> None:
    with pytest.raises(MimeBuildError):
        inquiry_message_id(INQUIRY_ID, 1, domain)


def test_inquiry_ref_parsing_is_strict() -> None:
    assert parse_inquiry_ref(f"inquiry-{INQUIRY_ID}") == INQUIRY_ID
    assert parse_inquiry_ref(f"inquiry-{HEX_ID}") == HEX_ID
    for bad in (None, "", str(INQUIRY_ID), f"inquiry-{INQUIRY_ID}; x", f"inquiry-{str(HEX_ID).upper()}"):
        assert parse_inquiry_ref(bad) is None


def test_reply_to_is_optional_and_exact() -> None:
    message = built(reply_to="replies@example.com")
    parsed = parse(message.raw)
    assert [a.addr_spec for a in parsed["Reply-To"].addresses] == ["replies@example.com"]
    assert message.reply_to_address == "replies@example.com"
    assert header_names(message.raw).index("Reply-To") == 2
    assert message.envelope().reply_to == ("replies@example.com",)


def test_idna_recipient_domain_is_ascii_encoded() -> None:
    message = built(recipient="verkauf@autohändler.de")
    ascii_domain = "autohändler.de".encode("idna").decode("ascii")
    assert ascii_domain.startswith("xn--")
    assert message.to_address == f"verkauf@{ascii_domain}"
    assert f"To: verkauf@{ascii_domain}\r\n".encode() in message.raw


def test_mailbox_canonicalises_domain_but_keeps_local_part() -> None:
    box = mailbox("Max.Mustermann+cars@Example.COM", "Max Mustermann")
    assert box.address == "Max.Mustermann+cars@example.com"
    assert box.local_part == "Max.Mustermann+cars"
    assert box.domain == "example.com"


def test_mailbox_model_requires_canonical_input() -> None:
    with pytest.raises(ValidationError):
        type(mailbox(SENDER))(address="A@Example.COM")


def test_raw_encodings_round_trip() -> None:
    message = built()
    assert base64.urlsafe_b64decode(message.raw_base64url()) == message.raw
    assert base64.b64decode(message.raw_base64()) == message.raw
    assert "+" not in message.raw_base64url() and "/" not in message.raw_base64url()


def test_date_header_is_utc_seconds() -> None:
    local = datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=2)))
    spec = OutboundInquiryMessage(
        inquiry_id=INQUIRY_ID,
        attempt_number=1,
        sender=mailbox(SENDER, "Vasko K"),
        recipient=mailbox(SELLER),
        subject="Enquiry about Toyota RAV4 - ABC-123",
        body="Hello",
        date=local,
    )
    message = build_message(spec)
    assert message.date == WHEN
    assert b"Date: Tue, 06 Oct 2026 10:00:00 +0000\r\n" in message.raw
    assert parse(message.raw)["Date"].datetime == WHEN


def test_naive_date_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient=mailbox(SELLER),
            subject="Enquiry",
            body="Hello",
            date=datetime(2026, 10, 6, 10, 0),
        )


def test_body_gets_exactly_one_trailing_newline_on_the_wire() -> None:
    for body in ("Hello", "Hello\n"):
        spec = OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient=mailbox(SELLER),
            subject="Enquiry",
            body=body,
            date=WHEN,
        )
        message = build_message(spec)
        assert parse(message.raw).get_content().replace("\r\n", "\n") == "Hello\n"
        assert message.body == body


def test_recipient_must_not_carry_display_name_or_be_the_sender() -> None:
    with pytest.raises(ValidationError):
        OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient=mailbox(SELLER, "Autohaus"),
            subject="Enquiry",
            body="Hello",
            date=WHEN,
        )
    with pytest.raises(ValidationError):
        OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient=mailbox(SENDER.upper().replace("EXAMPLE.COM", "example.com")),
            subject="Enquiry",
            body="Hello",
            date=WHEN,
        )


def test_sender_display_name_is_required() -> None:
    with pytest.raises(ValidationError):
        OutboundInquiryMessage(
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER),
            recipient=mailbox(SELLER),
            subject="Enquiry",
            body="Hello",
            date=WHEN,
        )


def test_spec_has_no_cc_bcc_attachment_or_threading_fields() -> None:
    base = {
        "inquiry_id": INQUIRY_ID,
        "attempt_number": 1,
        "sender": mailbox(SENDER, "Vasko K"),
        "recipient": mailbox(SELLER),
        "subject": "Enquiry",
        "body": "Hello",
        "date": WHEN,
    }
    for extra in ("cc", "bcc", "attachments", "in_reply_to", "references", "headers", "thread_id"):
        with pytest.raises(ValidationError):
            OutboundInquiryMessage(**base, **{extra: "x"})


def test_preview_is_never_sendable() -> None:
    preview = render_preview_mk(rendered())
    with pytest.raises(MimeBuildError) as exc:
        build_inquiry_message(
            preview,
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, SENDER_NAME),
            recipient_address=SELLER,
            reply_to_address=None,
            date=WHEN,
        )
    assert "NOT_A_SELLER_MESSAGE" in exc.value.problems


def test_from_display_name_must_equal_signature() -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_inquiry_message(
            rendered(),
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Someone Else"),
            recipient_address=SELLER,
            reply_to_address=None,
            date=WHEN,
        )
    assert exc.value.problems == ("SENDER_DISPLAY_NAME_MISMATCH",)


def test_subject_and_body_size_limits() -> None:
    base = {
        "inquiry_id": INQUIRY_ID,
        "attempt_number": 1,
        "sender": mailbox(SENDER, "Vasko K"),
        "recipient": mailbox(SELLER),
        "date": WHEN,
    }
    with pytest.raises(MimeBuildError) as exc:
        build_message(OutboundInquiryMessage(**base, subject="a " * 100 + "b", body="Hello"))
    assert "HEADER_VALUE_TOO_LONG" in exc.value.problems
    with pytest.raises(MimeBuildError) as exc:
        build_message(OutboundInquiryMessage(**base, subject="Enquiry", body="word " * 900))
    assert "BODY_TOO_LONG" in exc.value.problems
    with pytest.raises(MimeBuildError) as exc:
        build_message(OutboundInquiryMessage(**base, subject="Enquiry", body="  \n "))
    assert "BODY_EMPTY" in exc.value.problems


def test_verify_built_message_accepts_genuine_message() -> None:
    verify_built_message(built())
    verify_built_message(built(reply_to="replies@example.com", encoding="base64"))


def test_built_message_rejects_inconsistent_metadata() -> None:
    message = built()
    with pytest.raises(ValidationError):
        BuiltMessage.model_validate({**message.model_dump(), "raw_sha256": "0" * 64})
    with pytest.raises(ValidationError):
        BuiltMessage.model_validate({**message.model_dump(), "attempt_number": 2})
    with pytest.raises(ValidationError):
        BuiltMessage.model_validate({**message.model_dump(), "body": message.body + "!"})
    with pytest.raises(ValidationError):
        BuiltMessage.model_validate({**message.model_dump(), "inquiry_ref": "inquiry-x"})


def test_header_values_report_unfolded_values() -> None:
    values = built().header_values()
    assert values["To"] == SELLER
    assert values["Message-ID"] == f"<inquiry-{INQUIRY_ID}.1@example.com>"
    assert values["Content-Transfer-Encoding"] == "quoted-printable"
    assert "Reply-To" not in values
