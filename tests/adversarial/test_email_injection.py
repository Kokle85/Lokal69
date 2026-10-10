"""Adversarial header/MIME injection tests for the seller inquiry (spec 37.3, 37.10).

Every attempt to add a recipient, a header, a MIME part or an attachment - through the display
name, subject, recipient/Reply-To address, listing URL, body, a tampered rendered template or
tampered final bytes - must be refused before anything could reach a provider.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.inquiries import SenderBinding
from suv_deals.domain.seller_templates import build_vehicle_label, render
from suv_deals.integrations.email_providers.base import AccessToken, SendDefiniteFailure
from suv_deals.integrations.email_providers.gmail_api import GmailApiProvider
from suv_deals.integrations.mime_builder import (
    INQUIRY_REF_HEADER,
    MimeBuildError,
    OutboundInquiryMessage,
    build_inquiry_message,
    build_message,
    built_message_problems,
    mailbox,
)

INQUIRY_ID = UUID("66666666-6666-4666-8666-666666666666")
SENDER = "vasko.sender@example.com"
SELLER = "verkauf@autohaus-example.de"
WHEN = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
LISTING_URL = "https://www.example.de/fahrzeuge/123456"
ALLOWED_HEADERS = {
    "from",
    "to",
    "reply-to",
    "subject",
    "date",
    "message-id",
    INQUIRY_REF_HEADER.lower(),
    "mime-version",
    "content-type",
    "content-transfer-encoding",
}
LINE_BREAKERS = ["\r\n", "\n", "\r", "\x00", "\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1e"]


def spec(**overrides: object) -> OutboundInquiryMessage:
    values: dict[str, object] = {
        "inquiry_id": INQUIRY_ID,
        "attempt_number": 1,
        "sender": mailbox(SENDER, "Vasko K"),
        "recipient": mailbox(SELLER),
        "subject": "Enquiry about Toyota RAV4 - ABC-123",
        "body": "Hello,\n\nIs the vehicle still available?\n\nVasko K",
        "date": WHEN,
    }
    values.update(overrides)
    return OutboundInquiryMessage.model_validate(values)


def parse(raw: bytes):  # type: ignore[no-untyped-def]
    return BytesParser(policy=policy.default).parsebytes(raw)


def header_keys(raw: bytes) -> list[str]:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    return [line.split(":", 1)[0].lower() for line in head.split("\r\n") if line and line[0] not in " \t"]


# ---------------------------------------------------------------------------- display name


@pytest.mark.parametrize("breaker", LINE_BREAKERS)
def test_line_breaks_in_display_name_are_rejected(breaker: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        mailbox(SENDER, f"Vasko{breaker}Bcc: evil@attacker.example")
    assert {"HEADER_INJECTION", "CONTROL_CHARACTER"} & set(exc.value.problems)


@pytest.mark.parametrize(
    "name",
    [
        "Vasko <evil@attacker.example>",
        'Vasko" <evil@attacker.example>, "x',
        "evil@attacker.example",
        "Vasko, Other",
        "Vasko; Other",
        "Vasko (comment)",
        "Vasko [x]",
        "Vasko\\",
        "Vasko: group",
    ],
)
def test_display_name_specials_are_rejected(name: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        mailbox(SENDER, name)
    assert "DISPLAY_NAME_SPECIALS" in exc.value.problems


@pytest.mark.parametrize(
    "name",
    [
        "=?utf-8?q?Bcc=3A_evil=40attacker=2Eexample?=",
        "=?utf-8?b?QmNjOiBldmlsQGF0dGFja2VyLmV4YW1wbGU=?=",
        "Vasko =?iso-8859-1?q?K?=",
        "Vasko ?= K",
    ],
)
def test_encoded_words_in_display_name_are_rejected(name: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        mailbox(SENDER, name)
    assert "ENCODED_WORD_NOT_ALLOWED" in exc.value.problems


@pytest.mark.parametrize("char", ["\u202e", "\u200b", "\u2066", "\ufeff", "\ue000", "\x7f", "\t"])
def test_invisible_and_bidi_characters_in_display_name_are_rejected(char: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        mailbox(SENDER, f"Vasko{char}K")
    assert "CONTROL_CHARACTER" in exc.value.problems


def test_overlong_display_name_is_rejected() -> None:
    with pytest.raises(MimeBuildError) as exc:
        mailbox(SENDER, "V" * 65)
    assert "HEADER_VALUE_TOO_LONG" in exc.value.problems


# ---------------------------------------------------------------------------- subject


@pytest.mark.parametrize("breaker", LINE_BREAKERS)
def test_line_breaks_in_subject_are_rejected(breaker: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(subject=f"Enquiry{breaker}Bcc: evil@attacker.example"))
    assert {"HEADER_INJECTION", "CONTROL_CHARACTER"} & set(exc.value.problems)


@pytest.mark.parametrize(
    "subject",
    [
        "=?utf-8?b?QmNjOiBldmlsQGF0dGFja2VyLmV4YW1wbGU=?=",
        "Enquiry =?utf-8?q?=0D=0ABcc:_evil@attacker.example?=",
        "Enquiry ?= trailing",
    ],
)
def test_encoded_word_abuse_in_subject_is_rejected(subject: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(subject=subject))
    assert "ENCODED_WORD_NOT_ALLOWED" in exc.value.problems


def test_very_long_subject_is_rejected_and_long_tokens_stay_within_rfc_limits() -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(subject="x" * 201))
    assert "HEADER_VALUE_TOO_LONG" in exc.value.problems
    message = build_message(spec(subject="x" * 200))  # one unbreakable token at the limit
    assert all(len(line) <= 998 for line in message.raw.split(b"\r\n"))
    assert str(parse(message.raw)["Subject"]) == "x" * 200


def test_subject_with_bidi_override_is_rejected() -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(subject="Enquiry \u202egnp.exe"))
    assert "CONTROL_CHARACTER" in exc.value.problems


# ---------------------------------------------------------------------------- addresses


@pytest.mark.parametrize(
    "address",
    [
        "verkauf@autohaus-example.de\r\nBcc: evil@attacker.example",
        "verkauf@autohaus-example.de\nBcc: evil@attacker.example",
        "verkauf@autohaus-example.de\u2028Bcc: evil@attacker.example",
        "verkauf@autohaus-example.de, evil@attacker.example",
        "verkauf@autohaus-example.de;evil@attacker.example",
        "Autohaus <verkauf@autohaus-example.de>",
        "<verkauf@autohaus-example.de>",
        '"verkauf x"@autohaus-example.de',
        "verkauf@autohaus-example.de (comment)",
        "group: verkauf@autohaus-example.de;",
        "@route.example:verkauf@autohaus-example.de",
        "mailto:verkauf@autohaus-example.de?cc=evil@attacker.example",
        "verkauf@[127.0.0.1]",
        "verkauf@localhost",
        "verkäufer@autohaus-example.de",
        "verkauf@@autohaus-example.de",
        " verkauf@autohaus-example.de",
        "verkauf@autohaus-example.de\x00",
        "verkauf@autohaus-example.de\u200b",
        "a" * 65 + "@autohaus-example.de",
        "verkauf@" + "a" * 250 + ".de",
    ],
)
def test_hostile_recipient_addresses_are_rejected(address: str) -> None:
    with pytest.raises(MimeBuildError):
        mailbox(address)


def test_hostile_reply_to_is_rejected_by_the_inquiry_builder() -> None:
    source = render(
        "seller_initial_de_v1",
        build_vehicle_label("Toyota", "RAV4"),
        "ABC-123",
        LISTING_URL,
        "Vasko K",
        verified_listing_url=LISTING_URL,
    )
    with pytest.raises(MimeBuildError):
        build_inquiry_message(
            source,
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient_address=SELLER,
            reply_to_address="replies@example.com, evil@attacker.example",
            date=WHEN,
        )


# ---------------------------------------------------------------------------- body / URL


@pytest.mark.parametrize("breaker", ["\r\n", "\r", "\u2028", "\u2029", "\x85", "\x00", "\x0b"])
def test_listing_url_with_line_breaks_in_body_is_rejected(breaker: str) -> None:
    body = f"Hello,\n\nhttps://www.example.de/a{breaker}Bcc: evil@attacker.example\n\nVasko K"
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(body=body))
    assert {"BODY_LINE_BREAK_INVALID", "CONTROL_CHARACTER"} & set(exc.value.problems)


def test_header_lookalikes_and_boundaries_in_body_stay_body_text() -> None:
    body = (
        "Hello,\n\nBcc: evil@attacker.example\nContent-Type: multipart/mixed; boundary=x\n\n"
        "--x\nContent-Type: text/html\nContent-Disposition: attachment; filename=a.exe\n\n<script>\n--x--\n"
        "\n.\nFrom attacker\nVasko K"
    )
    message = build_message(spec(body=body))
    parsed = parse(message.raw)
    assert set(header_keys(message.raw)) <= ALLOWED_HEADERS
    assert parsed["Bcc"] is None
    assert not parsed.is_multipart()
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_filename() is None
    assert parsed.get_content().replace("\r\n", "\n") == body + "\n"
    assert built_message_problems(message) == ()


@pytest.mark.parametrize("char", ["\u202e", "\u200b", "\x7f", "\x1b", "\ud800"[:0] + "\ue000"])
def test_control_and_invisible_characters_in_body_are_rejected(char: str) -> None:
    with pytest.raises(MimeBuildError) as exc:
        build_message(spec(body=f"Hello {char} there"))
    assert "CONTROL_CHARACTER" in exc.value.problems


def test_tampered_rendered_template_cannot_inject_headers() -> None:
    source = render(
        "seller_initial_en_v1",
        build_vehicle_label("Toyota", "RAV4"),
        "ABC-123",
        LISTING_URL,
        "Vasko K",
        verified_listing_url=LISTING_URL,
    )
    hostile = source.model_copy(update={"subject": source.subject + "\r\nBcc: evil@attacker.example"})
    with pytest.raises(MimeBuildError) as exc:
        build_inquiry_message(
            hostile,
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient_address=SELLER,
            reply_to_address=None,
            date=WHEN,
        )
    assert "HEADER_INJECTION" in exc.value.problems


def test_tampered_template_body_fails_hash_or_scope() -> None:
    source = render(
        "seller_initial_en_v1",
        build_vehicle_label("Toyota", "RAV4"),
        "ABC-123",
        LISTING_URL,
        "Vasko K",
        verified_listing_url=LISTING_URL,
    )
    hostile = source.model_copy(update={"body": source.body + "\nI will pay 3000 EUR deposit today.\n"})
    with pytest.raises(MimeBuildError) as exc:
        build_inquiry_message(
            hostile,
            inquiry_id=INQUIRY_ID,
            attempt_number=1,
            sender=mailbox(SENDER, "Vasko K"),
            recipient_address=SELLER,
            reply_to_address=None,
            date=WHEN,
        )
    # Not the exact rendering of a registered template (and outside the scope): never sendable.
    assert "NOT_TEMPLATE_RENDERING" in exc.value.problems


# ---------------------------------------------------------------------------- tampered final bytes


def _tampered(raw_transform) -> object:  # type: ignore[no-untyped-def]
    message = build_message(spec())
    return message.model_copy(update={"raw": raw_transform(message.raw)})


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        (
            lambda raw: raw.replace(b"Subject:", b"Bcc: evil@attacker.example\r\nSubject:", 1),
            "FORBIDDEN_HEADER",
        ),
        (
            lambda raw: raw.replace(b"Subject:", b"Cc: evil@attacker.example\r\nSubject:", 1),
            "FORBIDDEN_HEADER",
        ),
        (
            lambda raw: raw.replace(
                b"To: " + SELLER.encode(), b"To: " + SELLER.encode() + b", evil@attacker.example"
            ),
            "RECIPIENT_COUNT",
        ),
        (
            lambda raw: raw.replace(b"Subject:", b"To: evil@attacker.example\r\nSubject:", 1),
            "HEADER_DUPLICATED:to",
        ),
        (lambda raw: raw.replace(b"Subject:", b"X-Evil: 1\r\nSubject:", 1), "HEADER_NOT_ALLOWED:x-evil"),
        (
            lambda raw: raw.replace(b"Subject:", b"In-Reply-To: <a@b.example>\r\nSubject:", 1),
            "HEADER_NOT_ALLOWED:in-reply-to",
        ),
        (
            lambda raw: raw.replace(b"text/plain", b"multipart/mixed; boundary=x", 1),
            "NOT_SINGLE_TEXT_PART",
        ),
        (
            lambda raw: raw.replace(b"\r\n\r\n", b"\r\nContent-Disposition: attachment\r\n\r\n", 1),
            "ATTACHMENT",
        ),
        (lambda raw: raw.replace(b"\r\n", b"\n", 1), "BARE_LINE_BREAK"),
        (lambda raw: raw + b"\r\nBcc: evil@attacker.example", "RAW_HASH_MISMATCH"),
    ],
)
def test_tampered_bytes_are_detected(transform, expected: str) -> None:  # type: ignore[no-untyped-def]
    message = _tampered(transform)
    problems = built_message_problems(message)  # type: ignore[arg-type]
    assert expected in problems, problems


async def test_providers_refuse_tampered_bytes_without_any_request() -> None:
    message = build_message(spec())
    hostile = message.model_copy(
        update={"raw": message.raw.replace(b"Subject:", b"Bcc: evil@attacker.example\r\nSubject:", 1)}
    )

    class Tokens:
        calls = 0

        async def access_token(self, *, force_refresh: bool = False) -> AccessToken:
            Tokens.calls += 1
            return AccessToken(token="t" * 20, scopes=frozenset())  # type: ignore[arg-type]

    binding = SenderBinding(
        binding_id=uuid4(),
        binding_version=1,
        provider=EmailProviderKind.GMAIL_API,
        account_id=SENDER,
        from_address=SENDER,
        display_name="Vasko K",
    )
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_mocked=True) as router:
            provider = GmailApiProvider(binding=binding, token_provider=Tokens(), http=http)
            outcome = await provider.send(
                hostile, inquiry_id=INQUIRY_ID, attempt_id=uuid4(), idempotency_key="key-12345678"
            )
            assert router.calls.call_count == 0
    assert isinstance(outcome, SendDefiniteFailure)
    assert outcome.pre_submission and not outcome.retryable
    assert any(p.startswith("MIME:") for p in outcome.problems)
    assert Tokens.calls == 0


# ---------------------------------------------------------------------------- property-based fuzzing

_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
    min_size=1,
    max_size=80,
)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(subject=_TEXT, name=_TEXT)
def test_fuzzed_headers_are_rejected_or_round_trip_exactly(subject: str, name: str) -> None:
    try:
        sender = mailbox(SENDER, name)
        message = build_message(spec(subject=subject, sender=sender))
    except MimeBuildError:
        return
    parsed = parse(message.raw)
    assert set(header_keys(message.raw)) <= ALLOWED_HEADERS
    assert [k for k in header_keys(message.raw) if k == "to"] == ["to"]
    assert [a.addr_spec for a in parsed["To"].addresses] == [SELLER]
    assert parsed["Cc"] is None and parsed["Bcc"] is None
    assert str(parsed["Subject"]) == message.subject
    assert parsed["From"].addresses[0].display_name == message.from_display_name
    assert parsed["From"].addresses[0].addr_spec == SENDER
    assert built_message_problems(message) == ()


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    body=st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=400)
)
def test_fuzzed_bodies_are_rejected_or_stay_a_single_text_part(body: str) -> None:
    try:
        message = build_message(spec(body=body))
    except MimeBuildError:
        return
    parsed = parse(message.raw)
    assert not parsed.is_multipart()
    assert set(header_keys(message.raw)) <= ALLOWED_HEADERS
    decoded = parsed.get_content().replace("\r\n", "\n")
    assert decoded == (message.body if message.body.endswith("\n") else message.body + "\n")
    assert base64.b64decode(message.raw_base64()) == message.raw
