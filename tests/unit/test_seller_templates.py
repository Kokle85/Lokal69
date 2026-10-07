"""Unit tests for domain.seller_templates (spec 37.1, 37.3, 37.4).

Every vehicle, listing reference, URL, name and address is SYNTHETIC test data.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.enums import Confidence, MessageLanguage
from suv_deals.domain.seller_templates import (
    MK_PREVIEW_TEMPLATE_ID,
    PERMITTED_QUESTIONS,
    SCOPE_CONTRACT,
    SCOPE_HASH,
    SELLER_INITIAL_DE_V1,
    SELLER_INITIAL_EN_V1,
    SELLER_INITIAL_FR_V1,
    SELLER_INITIAL_IT_V1,
    SELLER_INITIAL_MK_PREVIEW_V1,
    TEMPLATE_BY_LANGUAGE,
    TEMPLATES,
    MessageEnvelope,
    QuestionId,
    RenderedMessage,
    SellerTemplate,
    TemplateRenderError,
    VehicleLabel,
    build_vehicle_label,
    canonical_message,
    get_template,
    listing_url_problems,
    message_body_hash,
    render,
    render_preview_mk,
    require_scope,
    template_for_language,
    validate_scope,
    vehicle_label_from_taxonomy,
)
from suv_deals.domain.taxonomy import TaxonomyMatch
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
URL = "https://www.example-marketplace.invalid/angebote/suv-12345678"
REF = "12345678"
NAME = "Vasko K."
LABEL = build_vehicle_label("BMW", "X5", "E70")
SELLER_IDS = [t for t in TEMPLATE_BY_LANGUAGE.values()]


def _blocks(text: str, start: str, end: str | None) -> list[str]:
    section = text[text.index(start) : text.index(end) if end else None]
    return re.findall(r"```text\n(.*?)```", section, re.S)


def spec_blocks() -> list[str]:
    spec = (REPO / "docs/spec/suv-deal-system-build-spec.md").read_text(encoding="utf-8")
    return _blocks(spec, "### 37 4 Safe inquiry templates", "### 37 5")


def rendered(template_id: str = "seller_initial_de_v1", **overrides: Any) -> RenderedMessage:
    args: dict[str, Any] = {
        "vehicle_label": LABEL,
        "listing_reference": REF,
        "listing_url": URL,
        "sender_display_name": NAME,
        "verified_listing_url": URL,
    }
    args.update(overrides)
    return render(
        template_id,
        args["vehicle_label"],
        args["listing_reference"],
        args["listing_url"],
        args["sender_display_name"],
        verified_listing_url=args["verified_listing_url"],
    )


def variant(
    message: RenderedMessage, *, subject: str | None = None, body: str | None = None
) -> RenderedMessage:
    """A modified message (as a wording change or an attack would produce), hash recomputed."""
    new_subject = message.subject if subject is None else subject
    new_body = message.body if body is None else body
    return RenderedMessage(
        **{
            **message.model_dump(),
            "subject": new_subject,
            "body": new_body,
            "body_hash": message_body_hash(new_subject, new_body),
        }
    )


def problems_of(exc: pytest.ExceptionInfo[TemplateRenderError]) -> set[str]:
    return set(exc.value.problems)


# ---------------------------------------------------------------------------------------------
# Exact texts
# ---------------------------------------------------------------------------------------------


def test_templates_are_the_exact_spec_texts() -> None:
    blocks = spec_blocks()
    templates = [
        SELLER_INITIAL_DE_V1,
        SELLER_INITIAL_IT_V1,
        SELLER_INITIAL_FR_V1,
        SELLER_INITIAL_EN_V1,
        SELLER_INITIAL_MK_PREVIEW_V1,
    ]
    assert len(blocks) == 5
    for block, template in zip(blocks, templates, strict=True):
        assert template.document_text() == block, template.template_id


def test_docs_reproduce_the_templates_verbatim() -> None:
    docs = (REPO / "docs/seller_email_templates.md").read_text(encoding="utf-8")
    documented = re.findall(r"```text\n(.*?)```", docs, re.S)
    assert documented == spec_blocks()
    assert "no first-template approval" in docs
    assert "Ordinary safe wording fixes within the same scope need no approval" in docs


def test_template_registry() -> None:
    assert set(TEMPLATES) == {*SELLER_IDS, MK_PREVIEW_TEMPLATE_ID}
    assert {t.language for t in TEMPLATES.values()} == {"de", "it", "fr", "en", "mk"}
    assert TEMPLATES[MK_PREVIEW_TEMPLATE_ID].kind == "owner_preview"
    assert all(TEMPLATES[t].kind == "seller_inquiry" for t in SELLER_IDS)
    for language in MessageLanguage:
        assert template_for_language(language).language == language.value
    assert len({t.template_hash() for t in TEMPLATES.values()}) == 5
    with pytest.raises(ValidationFailed):
        get_template("seller_initial_nl_v1")


def test_templates_are_immutable() -> None:
    with pytest.raises(ValidationError):
        SELLER_INITIAL_DE_V1.body = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        TEMPLATES["x"] = SELLER_INITIAL_DE_V1  # type: ignore[index]


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Anfrage {{listing_reference}}", SELLER_INITIAL_DE_V1.body),  # label missing
        (SELLER_INITIAL_DE_V1.subject, "Hallo {{listing_url}}"),  # signature missing
        (SELLER_INITIAL_DE_V1.subject, SELLER_INITIAL_DE_V1.body + "{{listing_url}}"),  # twice
        (SELLER_INITIAL_DE_V1.subject + " {{seller_note}}", SELLER_INITIAL_DE_V1.body),
    ],
)
def test_template_placeholder_contract(subject: str, body: str) -> None:
    with pytest.raises(ValidationError):
        SellerTemplate(
            template_id="seller_initial_de_v2",
            version=2,
            kind="seller_inquiry",
            language="de",
            subject_prefix="Betreff: ",
            subject=subject,
            body=body,
        )


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------


def test_german_rendering_is_exact() -> None:
    msg = rendered()
    assert msg.subject == "Anfrage zu BMW X5 E70 \u2013 12345678"
    assert msg.body == (
        "Guten Tag,\n\n"
        f"ich schreibe wegen dieses Fahrzeugs: {URL}\n\n"
        "Ist das Fahrzeug noch verfügbar?\n"
        "Könnten Sie mir die vorhandenen Fahrzeugunterlagen zusenden, insbesondere die "
        "Zulassungsunterlagen und das CoC, falls vorhanden? Bitte schwärzen Sie persönliche Daten.\n"
        "Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?\n\n"
        "Es handelt sich zunächst um eine unverbindliche Anfrage.\n\n"
        "Freundliche Grüße\n"
        "Vasko K.\n"
    )
    assert msg.template_id == "seller_initial_de_v1"
    assert msg.template_version == 1
    assert msg.language == "de"
    assert msg.kind == "seller_inquiry"
    assert msg.requires_approval is False
    assert msg.scope_hash == SCOPE_HASH


@pytest.mark.parametrize("template_id", SELLER_IDS)
def test_all_languages_ask_identical_questions_without_commitments(template_id: str) -> None:
    msg = rendered(template_id)
    result = validate_scope(msg)
    assert result.ok, result.problems
    assert result.questions == PERMITTED_QUESTIONS
    assert msg.body.count("?") == 3
    assert "{{" not in msg.subject + msg.body
    assert URL in msg.body and REF in msg.subject and LABEL.text in msg.subject
    assert msg.body.rstrip("\n").endswith(NAME)


@pytest.mark.parametrize("template_id", SELLER_IDS)
def test_macedonian_preview_mirrors_the_message(template_id: str) -> None:
    msg = rendered(template_id)
    preview = render_preview_mk(msg)
    assert preview.kind == "owner_preview"
    assert preview.language == "mk"
    assert preview.template_id == MK_PREVIEW_TEMPLATE_ID
    assert preview.placeholders == msg.placeholders
    assert preview.source_body_hash == msg.body_hash
    assert preview.requires_approval is False
    result = validate_scope(preview)
    assert result.ok, result.problems
    assert result.questions == PERMITTED_QUESTIONS
    for value in (URL, REF, LABEL.text, NAME):
        assert value in preview.subject + preview.body
    with pytest.raises(TemplateRenderError):
        render_preview_mk(preview)


def test_preview_cannot_be_rendered_as_a_seller_message() -> None:
    with pytest.raises(TemplateRenderError) as exc:
        rendered(MK_PREVIEW_TEMPLATE_ID)
    assert problems_of(exc) == {"NOT_A_SELLER_TEMPLATE"}


def test_hashes() -> None:
    a = rendered()
    assert a.body_hash == rendered().body_hash
    assert a.body_hash == message_body_hash(a.subject, a.body)
    assert rendered(listing_reference="99999999").body_hash != a.body_hash
    assert rendered(sender_display_name="Vasko").body_hash != a.body_hash
    assert {rendered(t).scope_hash for t in SELLER_IDS} == {SCOPE_HASH}
    assert len({rendered(t).template_hash for t in SELLER_IDS}) == 4
    assert SCOPE_CONTRACT["questions"] == [q.value for q in PERMITTED_QUESTIONS]
    assert SCOPE_CONTRACT["attachments"] is False and SCOPE_CONTRACT["cc_bcc"] is False


def test_canonical_message_normalizes_line_endings_and_unicode() -> None:
    body = "Grüße\r\nVasko\n"
    nfd = unicodedata.normalize("NFD", body)
    assert canonical_message("S", nfd)["body"] == "Grüße\nVasko\n"
    assert message_body_hash("S", nfd) == message_body_hash("S", "Grüße\nVasko\n")


def test_tampered_hash_is_rejected() -> None:
    msg = rendered()
    with pytest.raises(ValidationError):
        RenderedMessage(**{**msg.model_dump(), "body": msg.body + "x"})


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_missing_listing_reference_blocks(reference: str | None) -> None:
    with pytest.raises(TemplateRenderError) as exc:
        rendered(listing_reference=reference)
    assert "LISTING_REFERENCE_MISSING" in problems_of(exc)


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("listing_reference", "123\r\nBcc: victim@example.invalid", "HEADER_INJECTION"),
        ("listing_reference", "123\nX", "HEADER_INJECTION"),
        ("listing_reference", "123\u2028X", "HEADER_INJECTION"),
        ("sender_display_name", "Vasko\r\nBcc: victim@example.invalid", "HEADER_INJECTION"),
        ("sender_display_name", "Vasko\x85K", "HEADER_INJECTION"),
        ("listing_url", URL + "%0d%0aBcc:%20victim", "HEADER_INJECTION"),
        ("listing_url", URL + "\r\n", "HEADER_INJECTION"),
        ("listing_reference", "123\u200b45", "CONTROL_CHARACTER"),
        ("sender_display_name", "Vasko\u202eK", "CONTROL_CHARACTER"),
        ("listing_reference", "12\x0045", "CONTROL_CHARACTER"),
        ("sender_display_name", "<b>Vasko</b>", "RAW_MARKUP"),
        ("sender_display_name", "Vasko &amp; Co", "RAW_MARKUP"),
        ("listing_reference", "[click](x)", "RAW_MARKUP"),
        ("listing_reference", "{{listing_url}}", "TEMPLATE_SYNTAX"),
        ("sender_display_name", "vasko@example.invalid", "PERSONAL_DATA_EMAIL"),
        ("listing_reference", "www.evil.example", "URL_NOT_ALLOWED"),
        ("sender_display_name", "http://evil.example", "URL_NOT_ALLOWED"),
        ("sender_display_name", "Vasko 0171 1234567", "SENDER_DISPLAY_NAME_INVALID_CHARACTERS"),
        ("sender_display_name", "V" * 65, "SENDER_DISPLAY_NAME_TOO_LONG"),
        ("sender_display_name", "", "SENDER_DISPLAY_NAME_MISSING"),
        ("listing_reference", "R" * 65, "LISTING_REFERENCE_TOO_LONG"),
        ("listing_reference", "ref;drop", "LISTING_REFERENCE_INVALID_CHARACTERS"),
        ("listing_reference", "a  b", "LISTING_REFERENCE_INVALID_CHARACTERS"),
        ("listing_reference", "AB12 ", "LISTING_REFERENCE_INVALID_CHARACTERS"),
        ("sender_display_name", "Vasko ", "SENDER_DISPLAY_NAME_INVALID_CHARACTERS"),
        ("sender_display_name", " Vasko", "SENDER_DISPLAY_NAME_INVALID_CHARACTERS"),
    ],
)
def test_placeholder_injection_is_rejected(field: str, value: str, problem: str) -> None:
    overrides: dict[str, Any] = {field: value}
    if field == "listing_url":
        overrides["verified_listing_url"] = value
    with pytest.raises(TemplateRenderError) as exc:
        rendered(**overrides)
    assert problem in problems_of(exc)
    if value.strip():
        assert value not in str(exc.value.details)  # codes only, never the offending value


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("javascript:alert(1)", "LISTING_URL_SCHEME"),
        ("ftp://files.example.invalid/x", "LISTING_URL_SCHEME"),
        ("data:text/html,hi", "LISTING_URL_SCHEME"),
        ("https://localhost/x", "LISTING_URL_HOST"),
        ("https://192.0.2.10/listing/1", "LISTING_URL_HOST"),
        ("https://[2001:db8::1]/listing/1", "LISTING_URL_HOST"),
        ("https://user:pw@www.example.invalid/x", "URL_CREDENTIALS"),
        ("https://www.example.invalid:8443/x", "LISTING_URL_PORT"),
        ("https://www.example.invalid/x#frag", "LISTING_URL_FRAGMENT"),
        ("https://www.example.invalid/x?token=abcdef", "URL_TOKEN_PARAMETER"),
        ("https://www.example.invalid/" + "a" * 600, "LISTING_URL_TOO_LONG"),
        ("https://www.example.invalid/a b", "LISTING_URL_INVALID_CHARACTERS"),
        ('https://www.example.invalid/a"b', "LISTING_URL_INVALID_CHARACTERS"),
        ("https://www.example.invalid/<x>", "RAW_MARKUP"),
        ("", "LISTING_URL_MISSING"),
        ("https://[::1/x", "LISTING_URL_INVALID"),
    ],
)
def test_listing_url_rules(url: str, problem: str) -> None:
    assert problem in listing_url_problems(url)
    with pytest.raises(TemplateRenderError) as exc:
        rendered(listing_url=url, verified_listing_url=url)
    assert problem in problems_of(exc)


def test_listing_url_must_be_the_verified_listing_url() -> None:
    assert listing_url_problems(URL) == []
    with pytest.raises(TemplateRenderError) as exc:
        rendered(listing_url=URL, verified_listing_url=URL + "?other=1")
    assert problems_of(exc) == {"LISTING_URL_NOT_VERIFIED"}
    with pytest.raises(TemplateRenderError):
        rendered(listing_url="https://www.other-site.invalid/x", verified_listing_url=URL)


def test_seller_text_cannot_enter_through_the_label() -> None:
    with pytest.raises(TemplateRenderError):
        build_vehicle_label("BMW", "X5\nBcc: victim@example.invalid")
    with pytest.raises(TemplateRenderError):
        build_vehicle_label("BMW", "X5 <script>")
    with pytest.raises(TemplateRenderError):
        build_vehicle_label("BMW", "http://evil.example")
    with pytest.raises(ValidationError):
        VehicleLabel(text="call me now!", make="call", model="me")
    # A label built from words that would add a commitment fails the scope check (fail closed).
    with pytest.raises(TemplateRenderError) as exc:
        rendered(vehicle_label=build_vehicle_label("Cash", "Deal"))
    assert {"COMMITMENT_DEPOSIT_OR_PAYMENT", "COMMITMENT_PRICE_ACCEPTANCE"} <= problems_of(exc)


# ---------------------------------------------------------------------------------------------
# Vehicle label
# ---------------------------------------------------------------------------------------------


def test_vehicle_label_rules() -> None:
    assert build_vehicle_label("BMW", "X5", "E70").text == "BMW X5 E70"
    assert build_vehicle_label(" BMW ", "  X5  ", None).text == "BMW X5"
    dup = build_vehicle_label("Land Rover", "Land Rover Discovery Sport")
    assert dup.text == "Land Rover Discovery Sport"
    assert build_vehicle_label("Kia", "Sorento XM", "XM").text == "Kia Sorento XM"
    assert build_vehicle_label("Škoda", "Kodiaq", "NS7").text == "Škoda Kodiaq NS7"
    long_gen = build_vehicle_label("Mercedes-Benz", "GLE 350 d 4MATIC Coupe", "C292 (2015-2019) facelift")
    assert long_gen.text == "Mercedes-Benz GLE 350 d 4MATIC Coupe"
    assert long_gen.shortened is True
    assert long_gen.generation is None


@pytest.mark.parametrize(
    ("make", "model", "problem"),
    [
        (None, "X5", "VEHICLE_MAKE_MISSING"),
        ("BMW", None, "VEHICLE_MODEL_MISSING"),
        ("", "", "VEHICLE_MAKE_MISSING"),
        ("A" * 41, "X5", "LABEL_PART_TOO_LONG"),
        ("BMW", "X5 & Co", "LABEL_INVALID_CHARACTERS"),
    ],
)
def test_vehicle_label_failures(make: str | None, model: str | None, problem: str) -> None:
    with pytest.raises(TemplateRenderError) as exc:
        build_vehicle_label(make, model)
    assert problem in problems_of(exc)


def test_vehicle_label_too_long_even_without_generation() -> None:
    with pytest.raises(TemplateRenderError) as exc:
        build_vehicle_label("M" * 30, "N" * 35)
    assert problems_of(exc) == {"VEHICLE_LABEL_TOO_LONG"}


def _match(**overrides: Any) -> TaxonomyMatch:
    data: dict[str, Any] = {
        "make": "BMW",
        "model": "X5",
        "generation": "E70",
        "is_suv": True,
        "confidence": Confidence.HIGH,
        "matched_via": "fields",
        "taxonomy_version": "synthetic",
        "verification": "unverified_reference",
    }
    data.update(overrides)
    return TaxonomyMatch(**data)


def test_label_from_taxonomy() -> None:
    assert vehicle_label_from_taxonomy(_match()).text == "BMW X5 E70"
    assert vehicle_label_from_taxonomy(_match(generation=None)).text == "BMW X5"
    for bad in (_match(matched_via="title"), _match(confidence=Confidence.LOW), _match(model=None)):
        with pytest.raises(TemplateRenderError):
            vehicle_label_from_taxonomy(bad)


@pytest.mark.parametrize(
    ("make", "model", "generation"),
    [
        ("BMW", "X5", "E70"),
        ("Mercedes-Benz", "GLE", "W166"),
        ("Land Rover", "Discovery Sport", "L550"),
        ("Škoda", "Kodiaq", None),
        ("Citroën", "C5 Aircross", None),
        ("VW", "Touareg", "7P"),
        ("Hyundai", "Santa Fe", "CM"),
        ("Dacia", "Duster", "HS"),
        ("Toyota", "RAV4", "XA30"),
        ("Volvo", "XC90", None),
    ],
)
def test_realistic_labels_render_in_every_language(make: str, model: str, generation: str | None) -> None:
    label = build_vehicle_label(make, model, generation)
    for template_id in SELLER_IDS:
        msg = rendered(template_id, vehicle_label=label, listing_reference="AB-123/45")
        assert validate_scope(msg).ok
        assert validate_scope(render_preview_mk(msg)).ok


# ---------------------------------------------------------------------------------------------
# Scope validator
# ---------------------------------------------------------------------------------------------


def _inject(msg: RenderedMessage, sentence: str) -> RenderedMessage:
    marker = {"de": "Es handelt", "it": "Si tratta", "fr": "Il s\u2019agit", "en": "This is", "mk": "Ова е"}  # noqa: RUF001 (Cyrillic)
    return variant(msg, body=msg.body.replace(marker[msg.language], sentence + " " + marker[msg.language], 1))


@pytest.mark.parametrize(
    ("template_id", "sentence", "problem"),
    [
        ("seller_initial_de_v1", "Ich kaufe das Fahrzeug sofort.", "COMMITMENT_PURCHASE"),
        ("seller_initial_de_v1", "Gerne mache ich ein Angebot.", "COMMITMENT_OFFER"),
        ("seller_initial_de_v1", "Ich bin mit Ihrem Preis einverstanden.", "COMMITMENT_PRICE_ACCEPTANCE"),
        ("seller_initial_de_v1", "Bitte reservieren Sie das Fahrzeug.", "COMMITMENT_RESERVATION"),
        ("seller_initial_de_v1", "Eine Anzahlung ist möglich.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_de_v1", "Ich zahle in bar.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_de_v1", "Gerne vereinbare ich einen Besichtigungstermin.", "APPOINTMENT_OR_VIEWING"),
        ("seller_initial_de_v1", "Ich hole es selbst ab, Abholung jederzeit.", "TRAVEL_OR_COLLECTION"),
        ("seller_initial_de_v1", "Ist ein Rabatt drin.", "NEGOTIATION"),
        ("seller_initial_de_v1", "Meine Adresse folgt.", "PERSONAL_DATA_ADDRESS"),
        ("seller_initial_de_v1", "Mein Budget ist begrenzt.", "BUDGET_OR_PROFIT"),
        ("seller_initial_de_v1", "Das Auto geht in den Export.", "UNRELATED_BUSINESS"),
        ("seller_initial_de_v1", "Rufen Sie mich per Telefon an.", "PERSONAL_DATA_CONTACT_CHANNEL"),
        (
            "seller_initial_de_v1",
            "Bitte senden Sie Ihren Personalausweis.",
            "PERSONAL_DATA_IDENTITY_DOCUMENT",
        ),
        ("seller_initial_de_v1", "Das ist eine verbindliche Anfrage.", "COMMITMENT_PRICE_ACCEPTANCE"),
        ("seller_initial_it_v1", "Posso lasciare una caparra.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_it_v1", "Vorrei fissare un appuntamento.", "APPOINTMENT_OR_VIEWING"),
        ("seller_initial_it_v1", "Le faccio un'offerta.", "COMMITMENT_OFFER"),
        ("seller_initial_it_v1", "Pago in contanti.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_fr_v1", "Je peux réserver le véhicule.", "COMMITMENT_RESERVATION"),
        ("seller_initial_fr_v1", "J\u2019accepte votre prix.", "COMMITMENT_PRICE_ACCEPTANCE"),
        ("seller_initial_fr_v1", "Je viens le chercher en voyage.", "TRAVEL_OR_COLLECTION"),
        ("seller_initial_fr_v1", "Je paie en espèces.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_en_v1", "I accept your price.", "COMMITMENT_PRICE_ACCEPTANCE"),
        ("seller_initial_en_v1", "I will buy it today.", "COMMITMENT_PURCHASE"),
        ("seller_initial_en_v1", "I can pay a deposit.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
        ("seller_initial_en_v1", "We could arrange a test drive.", "APPOINTMENT_OR_VIEWING"),
        ("seller_initial_en_v1", "I can travel to collect it.", "TRAVEL_OR_COLLECTION"),
        ("seller_initial_en_v1", "Please send your bank account and IBAN.", "PERSONAL_DATA_FINANCES"),
        ("seller_initial_en_v1", "Call me on WhatsApp.", "PERSONAL_DATA_CONTACT_CHANNEL"),
        ("seller_initial_en_v1", "My budget is limited.", "BUDGET_OR_PROFIT"),
        ("seller_initial_en_v1", "I resell cars in Macedonia.", "UNRELATED_BUSINESS"),
        ("seller_initial_en_v1", "See the attached file.", "ATTACHMENT_REFERENCE"),
        ("seller_initial_en_v1", "Please hold the car for me.", "COMMITMENT_RESERVATION"),
        ("seller_initial_mk_preview_v1", "Ќе платам капар.", "COMMITMENT_DEPOSIT_OR_PAYMENT"),
    ],
)
def test_scope_validator_rejects_commitments_and_extra_data(
    template_id: str, sentence: str, problem: str
) -> None:
    base = rendered() if template_id == MK_PREVIEW_TEMPLATE_ID else rendered(template_id)
    msg = render_preview_mk(base) if template_id == MK_PREVIEW_TEMPLATE_ID else base
    result = validate_scope(_inject(msg, sentence))
    assert not result.ok
    assert problem in result.problems


@pytest.mark.parametrize("template_id", SELLER_IDS)
def test_price_numbers_and_currencies_are_rejected(template_id: str) -> None:
    msg = rendered(template_id)
    result = validate_scope(_inject(msg, "2500 EUR."))
    assert {"PRICE_OR_NUMBER", "CURRENCY"} <= set(result.problems)
    assert "PRICE_OR_NUMBER" in validate_scope(_inject(msg, "Max. 2.500.")).problems
    assert "CURRENCY" in validate_scope(_inject(msg, "Preis in €.")).problems


def test_personal_data_is_rejected() -> None:
    msg = rendered("seller_initial_en_v1")
    assert "PERSONAL_DATA_PHONE" in validate_scope(_inject(msg, "Phone +49 171 1234567.")).problems
    assert "PERSONAL_DATA_EMAIL" in validate_scope(_inject(msg, "Write to vasko@example.invalid.")).problems
    iban = validate_scope(_inject(msg, "DE89 3704 0044 0532 0130 00"))
    assert "PERSONAL_DATA_FINANCES" in iban.problems


def test_question_structure() -> None:
    msg = rendered()
    no_availability = variant(msg, body=msg.body.replace("Ist das Fahrzeug noch verfügbar?\n", ""))
    assert "QUESTION_MISSING:availability" in validate_scope(no_availability).problems
    duplicate = variant(msg, body=msg.body.replace("Ist das", "Ist das Fahrzeug verfügbar?\nIst das", 1))
    assert "QUESTION_DUPLICATED:availability" in validate_scope(duplicate).problems
    extra = _inject(msg, "Wie alt sind die Reifen?")
    assert "UNRECOGNISED_QUESTION" in validate_scope(extra).problems
    two_in_one = _inject(msg, "Rost? Unfall?")
    assert "EXTRA_QUESTION" in validate_scope(two_in_one).problems
    no_coc = variant(msg, body=msg.body.replace(" und das CoC", ""))
    assert "DOCUMENTS_QUESTION_INCOMPLETE" in validate_scope(no_coc).problems
    plain_price = variant(msg, body=msg.body.replace("niedrigster Verkaufspreis", "Preis"))
    assert "PRICE_QUESTION_NOT_LOWEST_FINAL" in validate_scope(plain_price).problems
    no_disclaimer = variant(
        msg, body=msg.body.replace("Es handelt sich zunächst um eine unverbindliche Anfrage.\n", "")
    )
    assert "NON_BINDING_STATEMENT_MISSING" in validate_scope(no_disclaimer).problems
    subject_question = variant(msg, subject=msg.subject + "?")
    assert "EXTRA_QUESTION" in validate_scope(subject_question).problems


def test_urls_markup_headers_and_signature() -> None:
    msg = rendered("seller_initial_en_v1")
    assert "EXTRA_URL" in validate_scope(_inject(msg, "See https://other.example.invalid/x")).problems
    assert "EXTRA_URL" in validate_scope(_inject(msg, "See www.other.example.invalid")).problems
    assert "EXTRA_URL" in validate_scope(_inject(msg, "Visit my-cars.de")).problems
    assert "LISTING_URL_MISSING" in validate_scope(variant(msg, body=msg.body.replace(URL, "x"))).problems
    assert "EXTRA_URL" in validate_scope(variant(msg, body=msg.body + URL + "\n")).problems
    assert "RAW_MARKUP" in validate_scope(_inject(msg, "<a href='x'>here</a>")).problems
    header = variant(msg, body="Bcc: victim@example.invalid\n" + msg.body)
    assert "HEADER_INJECTION" in validate_scope(header).problems
    assert "HEADER_INJECTION" in validate_scope(variant(msg, subject=msg.subject + "\r\nBcc: x")).problems
    assert "HEADER_INJECTION" in validate_scope(variant(msg, body=msg.body.replace("\n", "\r\n"))).problems
    assert "UNRENDERED_PLACEHOLDER" in validate_scope(_inject(msg, "{{seller_note}}")).problems
    assert "SIGNATURE_MISMATCH" in validate_scope(variant(msg, body=msg.body + "PS\n")).problems
    assert "BODY_TOO_LONG" in validate_scope(variant(msg, body="Hello.\n" * 400 + msg.body)).problems
    assert (
        "SUBJECT_TOO_LONG"
        in validate_scope(variant(msg, subject="Enquiry " + "a" * 300 + " " + REF)).problems
    )
    assert "LISTING_REFERENCE_MISSING" in validate_scope(variant(msg, subject="Enquiry")).problems
    assert (
        "CONTROL_CHARACTER"
        in validate_scope(variant(msg, body=msg.body.replace("Hello", "He\u200bllo"))).problems
    )


def test_ordinary_wording_fix_within_scope_passes() -> None:
    en = rendered("seller_initial_en_v1")
    fixed = variant(
        en,
        body=en.body.replace("Hello,", "Good afternoon,").replace(
            "Is the vehicle still available?", "Is the car still available?"
        ),
    )
    assert validate_scope(fixed).ok
    de = rendered()
    fixed_de = variant(de, body=de.body.replace("Guten Tag,", "Sehr geehrte Damen und Herren,"))
    assert validate_scope(fixed_de).ok


def test_envelope_rules() -> None:
    msg = rendered("seller_initial_en_v1")
    ok = MessageEnvelope(to=("seller@example.invalid",), reply_to=("vasko@example.invalid",))
    assert validate_scope(msg, envelope=ok).ok
    cases = {
        "EXTRA_RECIPIENT": MessageEnvelope(to=("a@example.invalid", "b@example.invalid")),
        "RECIPIENT_MISSING": MessageEnvelope(to=()),
        "CC_BCC": MessageEnvelope(to=("a@example.invalid",), cc=("c@example.invalid",)),
        "ATTACHMENT": MessageEnvelope(to=("a@example.invalid",), attachments=("passport.pdf",)),
        "EXTRA_REPLY_TO": MessageEnvelope(to=("a@example.invalid",), reply_to=("x@e.invalid", "y@e.invalid")),
        "HEADER_NOT_ALLOWED": MessageEnvelope(
            to=("a@example.invalid",), extra_headers={"Bcc": "x@example.invalid"}
        ),
    }
    for problem, envelope in cases.items():
        assert problem in validate_scope(msg, envelope=envelope).problems, problem
    bcc_only = MessageEnvelope(to=("a@example.invalid",), bcc=("hidden@example.invalid",))
    assert "CC_BCC" in validate_scope(msg, envelope=bcc_only).problems
    injected = MessageEnvelope(to=("a@example.invalid\r\nBcc: x@example.invalid",))
    assert "HEADER_INJECTION" in validate_scope(msg, envelope=injected).problems
    header_value = MessageEnvelope(to=("a@example.invalid",), extra_headers={"Message-ID": "<a@b>\r\nBcc: x"})
    assert "HEADER_INJECTION" in validate_scope(msg, envelope=header_value).problems


def test_require_scope() -> None:
    msg = rendered()
    assert require_scope(msg).ok
    with pytest.raises(TemplateRenderError) as exc:
        require_scope(_inject(msg, "Ich kaufe es."))
    assert "COMMITMENT_PURCHASE" in problems_of(exc)
    assert exc.value.details == {"problems": list(exc.value.problems)}


def test_question_ids_are_stable() -> None:
    assert [q.value for q in QuestionId] == ["availability", "vehicle_documents", "lowest_final_price"]
