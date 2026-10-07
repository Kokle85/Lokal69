"""Property tests for domain.replies (spec 37.7/37.8 invariants). All generated data is SYNTHETIC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from suv_deals.domain.enums import EmailProviderKind, MessageLanguage, ReplyMessageType
from suv_deals.domain.notifications import text_problems
from suv_deals.domain.replies import (
    MAX_BODY_BYTES,
    CorrelationOutcome,
    InboundMessage,
    IngestDecisionKind,
    InquiryBinding,
    ReplyIngestRequest,
    SourceMessageIdentity,
    StoredReplyIngest,
    classify_message,
    correlate_reply,
    decide_ingest,
    extract_reply_claims,
    normalize_message_id,
    sanitize_reply_body,
)

MB = UUID("77777777-7777-4777-8777-777777777777")
INQ = UUID("66666666-6666-4666-8666-666666666666")
REPLY = UUID("88888888-8888-4888-8888-888888888888")
OUT_ID = "<synthetic-inquiry@example.invalid>"
SELLER = "seller@example.invalid"
BINDING = InquiryBinding(
    inquiry_id=INQ,
    binding_version=1,
    mailbox_binding_id=MB,
    provider=EmailProviderKind.OUTLOOK_LOCAL,
    outbound_message_ids=(OUT_ID,),
    verified_seller_aliases=(SELLER,),
    listing_references=("TEST-204",),
    listing_urls=("https://dealer.example/vehicles/TEST-204",),
)
SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=0x2FFF), min_size=0, max_size=600
)
WORDS = st.sampled_from(
    [
        "Ja",
        "noch",
        "verfügbar",
        "verkauft",
        "nicht",
        "Preis",
        "2.600",
        "€",
        "VB",
        "CoC",
        "anbei",
        "venduta",
        "disponible",
        "sold",
        "not",
        "between",
        "2,500",
        "and",
        "EUR",
        "Tel.",
        "0171",
        "1234567",
        "+49",
        "seller@example.invalid",
        "IBAN",
        "DE89",
        "3704",
        "https://x.example.invalid/a?token=1",
        "\n",
        ">",
        "Am 06.10.2026 schrieb X:",
        "Mit freundlichen Grüßen",
        "80331 München",
        "Musterstraße 12",
        "?",
        ".",
        ",",
    ]
)
SENTENCES = st.lists(WORDS, min_size=0, max_size=80).map(" ".join)


def request(**overrides: object) -> ReplyIngestRequest:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "inquiry_id": str(INQ),
        "binding_version": 1,
        "mailbox_binding_id": str(MB),
        "source_message": {
            "internet_message_id": "<synthetic-reply@example.invalid>",
            "outlook_entry_id": "entry",
            "outlook_store_id": "store",
            "received_at": "2026-10-06T18:00:00Z",
        },
        "headers": {"from": SELLER, "in_reply_to": OUT_ID, "references": [OUT_ID]},
        "subject": "Synthetic vehicle reply",
        "sanitized_body_text": "Synthetic fixture only: the vehicle is available.",
        "detected_language": "en",
        "attachments": [],
        "observed_at": "2026-10-06T18:00:02Z",
    }
    data.update(overrides)
    return ReplyIngestRequest.model_validate(data)


@SETTINGS
@given(
    headers=st.dictionaries(
        st.sampled_from(["From", "Subject", "Auto-Submitted", "X-Spam-Flag", "Content-Type", "In-Reply-To", "x y"]),
        st.one_of(TEXT, st.lists(TEXT, max_size=3), st.integers(), st.none()),
        max_size=7,
    ),
    body=st.one_of(TEXT, SENTENCES, st.none()),
    junk=st.booleans(),
)
def test_classification_is_total(headers: dict[str, object], body: str | None, junk: bool) -> None:
    result = classify_message(headers, body, in_junk_folder=junk)
    assert isinstance(result, ReplyMessageType)
    if junk:
        assert result == ReplyMessageType.SPAM


@SETTINGS
@given(body=st.one_of(TEXT, SENTENCES))
def test_sanitize_is_bounded_idempotent_and_contact_free(body: str) -> None:
    once = sanitize_reply_body(body)
    assert len(once.text.encode("utf-8")) <= MAX_BODY_BYTES
    assert sanitize_reply_body(once.text).text == once.text
    for line in once.text.split("\n"):
        problems = text_problems(line[:3000])
        assert "EMAIL_ADDRESS" not in problems
        assert "PHONE_NUMBER" not in problems


@SETTINGS
@given(
    entry=st.text(alphabet="abcdefXYZ0123456789-", min_size=1, max_size=40),
    store=st.text(alphabet="abcdefXYZ0123456789-", min_size=1, max_size=40),
    observed_shift=st.integers(min_value=0, max_value=10**6),
    received_shift=st.integers(min_value=0, max_value=10**6),
    language=st.sampled_from([None, "de", "it", "fr", "en"]),
    version=st.integers(min_value=1, max_value=50),
)
def test_fingerprint_ignores_locators_and_sync_metadata(
    entry: str, store: str, observed_shift: int, received_shift: int, language: str | None, version: int
) -> None:
    base = request()
    observed = datetime(2026, 10, 6, 18, 0, 2, tzinfo=UTC) + timedelta(seconds=observed_shift)
    received = datetime(2026, 10, 6, 18, 0, tzinfo=UTC) + timedelta(seconds=received_shift)
    moved = request(
        source_message={
            "internet_message_id": "<synthetic-reply@example.invalid>",
            "outlook_entry_id": entry,
            "outlook_store_id": store,
            "received_at": received.isoformat(),
        },
        observed_at=observed.isoformat(),
        detected_language=language,
        binding_version=version,
    )
    assert moved.fingerprint() == base.fingerprint()
    stored = StoredReplyIngest(
        reply_id=REPLY,
        dedup_key=base.dedup_key().as_string(),
        idempotency_key="idem-key-0001",
        fingerprint=base.fingerprint(),
        locators=(base.locator(),),  # type: ignore[arg-type]
    )
    decision = decide_ingest(
        dedup_key=moved.dedup_key(),
        idempotency_key="idem-key-0002",
        fingerprint=moved.fingerprint(),
        locator=moved.locator(),
        existing_by_dedup_key=stored,
        existing_by_idempotency_key=None,
    )
    assert decision.kind == IngestDecisionKind.DUPLICATE
    assert decision.reply_id == REPLY


@SETTINGS
@given(body=st.text(min_size=1, max_size=500))
def test_changed_body_under_same_identity_is_a_conflict(body: str) -> None:
    base = request()
    changed = request(sanitized_body_text=body.replace("\x00", ""))
    assume(changed.fingerprint() != base.fingerprint())
    stored = StoredReplyIngest(
        reply_id=REPLY,
        dedup_key=base.dedup_key().as_string(),
        idempotency_key="idem-key-0001",
        fingerprint=base.fingerprint(),
    )
    decision = decide_ingest(
        dedup_key=changed.dedup_key(),
        idempotency_key="idem-key-0003",
        fingerprint=changed.fingerprint(),
        existing_by_dedup_key=stored,
        existing_by_idempotency_key=None,
    )
    assert decision.kind == IngestDecisionKind.CONFLICT
    assert decision.error_code == "IDEMPOTENCY_CONFLICT" and decision.quarantine


@SETTINGS
@given(
    subject_extra=st.text(max_size=60),
    body=st.one_of(TEXT, SENTENCES),
    sender=st.sampled_from(["stranger@other.invalid", "friend@private.invalid", "not an address", ""]),
    refs=st.lists(st.text(alphabet="abcdef0123456789", min_size=1, max_size=12), max_size=3),
)
def test_subject_or_reference_text_alone_never_matches_and_never_uploads(
    subject_extra: str, body: str, sender: str, refs: list[str]
) -> None:
    headers: dict[str, object] = {
        "From": sender,
        "Subject": f"Re: Anfrage zu Example Trail – TEST-204 {subject_extra}",
        "References": " ".join(f"<{r}@unknown.invalid>" for r in refs),
    }
    msg = InboundMessage(
        identity=SourceMessageIdentity(
            mailbox_binding_id=MB,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            internet_message_id="<x@example.invalid>",
        ),
        headers=headers,  # type: ignore[arg-type]
        body_text=f"{body} TEST-204 https://dealer.example/vehicles/TEST-204",
    )
    result = correlate_reply(msg, [BINDING])
    assert result.outcome == CorrelationOutcome.UNMATCHED
    assert result.upload_scope == "none"


@SETTINGS
@given(text=st.one_of(TEXT, SENTENCES), language=st.sampled_from([None, *MessageLanguage]))
def test_claims_never_raise_and_quotes_are_never_accepted(text: str, language: MessageLanguage | None) -> None:
    claims = extract_reply_claims(text, language)
    for price in claims.prices:
        assert price.accepted is False
        assert price.status == "unaccepted_seller_quote"
        if price.kind == "range":
            assert price.low is not None and price.high is not None and 0 < price.low < price.high
        else:
            assert price.amount is not None and price.amount > 0
    for mention in claims.other_amounts:
        assert mention.amount is None or mention.amount > 0
    assert claims.availability_summary in {"available", "sold", "reserved", "not_available", "conflicting", "not_stated"}


@SETTINGS
@given(value=st.text(max_size=80))
def test_message_id_normalisation_is_idempotent(value: str) -> None:
    normalized = normalize_message_id(value)
    if normalized is not None:
        assert normalize_message_id(normalized) == normalized
        assert normalized.startswith("<") and normalized.endswith(">")
        assert not any(c.isspace() for c in normalized)
