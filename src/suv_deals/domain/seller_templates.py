"""Versioned deterministic seller-inquiry templates and the template-scope validator (spec 37.4).

Pure functions, no I/O. Business rules:

- The template texts are the exact versioned texts of spec 37.4 (``seller_initial_de_v1``,
  ``_it_v1``, ``_fr_v1``, ``_en_v1``) plus the Macedonian *informational* preview for Vasko.
  The preview and the stored original are audit artifacts, never approval drafts: nothing in this
  module or its callers waits for a human to approve a message or a template.
- Only the bounded placeholders are replaced: a vehicle label built from verified make/model
  (optionally generation) facts, the exact listing reference, the verified listing URL and the
  verified sender display name. Seller text is never inserted. A missing listing reference blocks
  rendering; a too-long label is shortened only by dropping the optional generation.
- Placeholder values are rejected (never silently cleaned) when they contain CR/LF (header
  injection), other control/format characters, raw HTML/markup, template syntax, URLs (other than
  the verified listing URL in its own slot), e-mail addresses or characters outside a strict class.
  The listing URL must be http(s), without credentials, ports or encoded CR/LF, at most
  ``MAX_LISTING_URL_LENGTH`` characters, and identical to the verified listing URL.
- ``validate_scope`` is semantic, so ordinary wording fixes inside the same scope keep passing
  while scope changes fail: exactly three questions (availability, vehicle documents incl.
  registration documents and CoC, lowest/final price), the non-binding statement, no commitments
  (offer, purchase, price acceptance, reservation, deposit/payment, appointment/viewing, travel,
  cash, negotiation beyond asking the lowest price), no price numbers or currencies, no extra
  personal data (phone, e-mail, address, identity documents, bank/IBAN, finances, budget,
  profit/resale), no other URLs, no markup, no header lines and, when an envelope is supplied,
  exactly one recipient with no CC/BCC and no attachments. Lexicons are applied across all
  languages (fail closed).
- ``body_hash`` is the SHA-256 of the canonical rendered subject+body; ``scope_hash`` fingerprints
  the bounded scope contract the message was validated against; ``template_hash`` fingerprints the
  template text.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from suv_deals.domain.enums import MessageLanguage
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.notifications import text_problems
from suv_deals.domain.seller_contacts import AddressError, canonicalize_address
from suv_deals.domain.taxonomy import TaxonomyMatch
from suv_deals.errors import ValidationFailed

TEMPLATE_SET_VERSION: Final = "seller_templates@1"
SCOPE_VERSION: Final = 1
INQUIRY_PURPOSE: Final = "initial_availability_documents_price"
MK_PREVIEW_TEMPLATE_ID: Final = "seller_initial_mk_preview_v1"

MAX_VEHICLE_LABEL_LENGTH: Final = 60
MAX_LABEL_PART_LENGTH: Final = 40
MAX_LISTING_REFERENCE_LENGTH: Final = 64
MAX_LISTING_URL_LENGTH: Final = 512
MAX_SENDER_DISPLAY_NAME_LENGTH: Final = 64
MAX_SUBJECT_LENGTH: Final = 200
MAX_BODY_LENGTH: Final = 2_000
MAX_BODY_LINES: Final = 25

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class QuestionId(StrEnum):
    AVAILABILITY = "availability"
    VEHICLE_DOCUMENTS = "vehicle_documents"
    LOWEST_FINAL_PRICE = "lowest_final_price"


PERMITTED_QUESTIONS: Final[tuple[QuestionId, ...]] = (
    QuestionId.AVAILABILITY,
    QuestionId.VEHICLE_DOCUMENTS,
    QuestionId.LOWEST_FINAL_PRICE,
)

#: Spec 37.1: the only data an initial inquiry may carry.
ALLOWED_OUTGOING_DATA_CATEGORIES: Final[tuple[str, ...]] = (
    "verified_sender_display_name",
    "verified_sender_email",
    "vehicle_make_model",
    "listing_reference",
    "listing_url",
    "three_permitted_questions",
)
EXCLUDED_DATA_CATEGORIES: Final[tuple[str, ...]] = (
    "home_address",
    "telephone",
    "identity_documents",
    "bank_details",
    "finances",
    "acquisition_budget",
    "target_resale_price",
    "profit_calculation",
    "unrelated_business_information",
)

#: The bounded scope every rendered message is validated against (hashed into ``scope_hash``).
SCOPE_CONTRACT: Final[Mapping[str, object]] = MappingProxyType(
    {
        "scope_version": SCOPE_VERSION,
        "purpose": INQUIRY_PURPOSE,
        "questions": [q.value for q in PERMITTED_QUESTIONS],
        "documents_requested": ["registration_documents", "coc"],
        "non_binding_statement": True,
        "allowed_outgoing_data": list(ALLOWED_OUTGOING_DATA_CATEGORIES),
        "excluded_data": list(EXCLUDED_DATA_CATEGORIES),
        "recipients": 1,
        "cc_bcc": False,
        "attachments": False,
        "follow_ups": False,
        "commitments": False,
    }
)
SCOPE_HASH: Final = sha256_json(dict(SCOPE_CONTRACT))

TemplateKind = Literal["seller_inquiry", "owner_preview"]
TemplateLanguage = Literal["de", "it", "fr", "en", "mk"]
_PLACEHOLDER_RE: Final = re.compile(r"\{\{([a-z_]+)\}\}")
_SUBJECT_PLACEHOLDERS: Final = frozenset({"vehicle_label", "listing_reference"})
_BODY_PLACEHOLDERS: Final = frozenset({"listing_url", "verified_sender_display_name"})


class SellerTemplate(BaseModel):
    """One immutable versioned template. ``document_text()`` is the spec 37.4 block verbatim."""

    model_config = _FROZEN

    template_id: str = Field(pattern=r"^seller_initial_[a-z]{2}(_preview)?_v[1-9][0-9]*$")
    version: int = Field(ge=1)
    kind: TemplateKind
    language: TemplateLanguage
    subject_prefix: str  # the documented subject label, e.g. "Betreff: " (not sent)
    subject: str
    body: str

    @model_validator(mode="after")
    def _placeholders(self) -> SellerTemplate:
        if set(_PLACEHOLDER_RE.findall(self.subject)) != _SUBJECT_PLACEHOLDERS:
            raise ValueError("subject must use exactly vehicle_label and listing_reference")
        found = _PLACEHOLDER_RE.findall(self.body)
        if set(found) != _BODY_PLACEHOLDERS or len(found) != len(_BODY_PLACEHOLDERS):
            raise ValueError("body must use listing_url and verified_sender_display_name once each")
        if not self.template_id.endswith(f"_v{self.version}"):
            raise ValueError("template_id must end with its version")
        return self

    def document_text(self) -> str:
        return f"{self.subject_prefix}{self.subject}\n\n{self.body}"

    def template_hash(self) -> str:
        return sha256_json(
            {
                "template_id": self.template_id,
                "version": self.version,
                "kind": self.kind,
                "language": self.language,
                "subject": self.subject,
                "body": self.body,
            }
        )


# ---------------------------------------------------------------------------------------------
# Exact spec 37.4 texts. Non-ASCII punctuation is escaped: \u2013 is EN DASH and \u2019 is
# RIGHT SINGLE QUOTATION MARK. The Macedonian preview keeps the spec's Latin "è" in "сè".
# ---------------------------------------------------------------------------------------------

SELLER_INITIAL_DE_V1: Final = SellerTemplate(
    template_id="seller_initial_de_v1",
    version=1,
    kind="seller_inquiry",
    language="de",
    subject_prefix="Betreff: ",
    subject="Anfrage zu {{vehicle_label}} \u2013 {{listing_reference}}",
    body=(
        "Guten Tag,\n"
        "\n"
        "ich schreibe wegen dieses Fahrzeugs: {{listing_url}}\n"
        "\n"
        "Ist das Fahrzeug noch verfügbar?\n"
        "Könnten Sie mir die vorhandenen Fahrzeugunterlagen zusenden, insbesondere die "
        "Zulassungsunterlagen und das CoC, falls vorhanden? Bitte schwärzen Sie persönliche Daten.\n"
        "Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?\n"
        "\n"
        "Es handelt sich zunächst um eine unverbindliche Anfrage.\n"
        "\n"
        "Freundliche Grüße\n"
        "{{verified_sender_display_name}}\n"
    ),
)

SELLER_INITIAL_IT_V1: Final = SellerTemplate(
    template_id="seller_initial_it_v1",
    version=1,
    kind="seller_inquiry",
    language="it",
    subject_prefix="Oggetto: ",
    subject="Richiesta su {{vehicle_label}} \u2013 {{listing_reference}}",
    body=(
        "Buongiorno,\n"
        "\n"
        "scrivo per questo veicolo: {{listing_url}}\n"
        "\n"
        "Il veicolo è ancora disponibile?\n"
        "Potrebbe inviarmi i documenti disponibili del veicolo, in particolare la carta di "
        "circolazione e il certificato di conformità CoC, se presente? La prego di oscurare i dati "
        "personali.\n"
        "Qual è il prezzo minimo finale a cui sarebbe disposto a venderlo?\n"
        "\n"
        "Si tratta soltanto di una richiesta di informazioni.\n"
        "\n"
        "Cordiali saluti,\n"
        "{{verified_sender_display_name}}\n"
    ),
)

SELLER_INITIAL_FR_V1: Final = SellerTemplate(
    template_id="seller_initial_fr_v1",
    version=1,
    kind="seller_inquiry",
    language="fr",
    subject_prefix="Objet : ",
    subject="Renseignements sur {{vehicle_label}} \u2013 {{listing_reference}}",
    body=(
        "Bonjour,\n"
        "\n"
        "je vous contacte au sujet de ce véhicule : {{listing_url}}\n"
        "\n"
        "Le véhicule est-il toujours disponible ?\n"
        "Pourriez-vous m\u2019envoyer les documents disponibles du véhicule, notamment le certificat "
        "d\u2019immatriculation et le certificat de conformité CoC, si vous en disposez ? Merci de "
        "masquer les données personnelles.\n"
        "Quel est votre dernier prix, le plus bas auquel vous accepteriez de le vendre ?\n"
        "\n"
        "Il s\u2019agit uniquement d\u2019une demande de renseignements.\n"
        "\n"
        "Cordialement,\n"
        "{{verified_sender_display_name}}\n"
    ),
)

SELLER_INITIAL_EN_V1: Final = SellerTemplate(
    template_id="seller_initial_en_v1",
    version=1,
    kind="seller_inquiry",
    language="en",
    subject_prefix="Subject: ",
    subject="Enquiry about {{vehicle_label}} \u2013 {{listing_reference}}",
    body=(
        "Hello,\n"
        "\n"
        "I\u2019m writing about this vehicle: {{listing_url}}\n"
        "\n"
        "Is the vehicle still available?\n"
        "Could you send the available vehicle documents, particularly the registration documents "
        "and the CoC if available? Please redact personal details.\n"
        "What is your lowest final selling price for the vehicle?\n"
        "\n"
        "This is an information enquiry only.\n"
        "\n"
        "Kind regards,\n"
        "{{verified_sender_display_name}}\n"
    ),
)

SELLER_INITIAL_MK_PREVIEW_V1: Final = SellerTemplate(
    template_id=MK_PREVIEW_TEMPLATE_ID,
    version=1,
    kind="owner_preview",
    language="mk",
    subject_prefix="Предмет: ",
    subject="Прашање за {{vehicle_label}} \u2013 {{listing_reference}}",
    body=(
        "Здраво,\n"
        "\n"
        "Ви пишувам за ова возило: {{listing_url}}\n"
        "\n"
        "Дали возилото е сè уште достапно?\n"  # noqa: RUF001 (Cyrillic)
        "Може ли да ми ги испратите достапните документи за возилото, особено сообраќајната "
        "документација и CoC ако е достапен? Ве молам скријте ги личните податоци.\n"  # noqa: RUF001 (Cyrillic)
        "Која е вашата последна, најниска продажна цена за возилото?\n"  # noqa: RUF001 (Cyrillic)
        "\n"
        "Ова е само барање за информации.\n"  # noqa: RUF001 (Cyrillic)
        "\n"
        "Поздрав,\n"
        "{{verified_sender_display_name}}\n"
    ),
)

TEMPLATES: Final[Mapping[str, SellerTemplate]] = MappingProxyType(
    {
        t.template_id: t
        for t in (
            SELLER_INITIAL_DE_V1,
            SELLER_INITIAL_IT_V1,
            SELLER_INITIAL_FR_V1,
            SELLER_INITIAL_EN_V1,
            SELLER_INITIAL_MK_PREVIEW_V1,
        )
    }
)
#: Current seller-inquiry template per supported language (English only with evidence: the
#: language decision, not this table, guarantees that).
TEMPLATE_BY_LANGUAGE: Final[Mapping[MessageLanguage, str]] = MappingProxyType(
    {
        MessageLanguage.DE: SELLER_INITIAL_DE_V1.template_id,
        MessageLanguage.IT: SELLER_INITIAL_IT_V1.template_id,
        MessageLanguage.FR: SELLER_INITIAL_FR_V1.template_id,
        MessageLanguage.EN: SELLER_INITIAL_EN_V1.template_id,
    }
)


def get_template(template_id: str) -> SellerTemplate:
    try:
        return TEMPLATES[template_id]
    except KeyError as exc:
        raise ValidationFailed("unknown seller template", details={"problems": ["UNKNOWN_TEMPLATE"]}) from exc


def template_for_language(language: MessageLanguage) -> SellerTemplate:
    return TEMPLATES[TEMPLATE_BY_LANGUAGE[language]]


# ---------------------------------------------------------------------------------------------
# Placeholder values
# ---------------------------------------------------------------------------------------------


class TemplateRenderError(ValidationFailed):
    """Rendering refused. ``problems`` are codes only; offending values are never echoed."""

    def __init__(self, message: str, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(sorted(set(problems)))
        super().__init__(message, details={"problems": list(self.problems)})


_LABEL_PART_RE: Final = re.compile(r"^[^\W_](?:[^\W_]|[ .\-/+()])*$")
_REFERENCE_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/#-]| (?! ))*$")
_DISPLAY_NAME_RE: Final = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .\-'\u2019](?![ .\-'\u2019]))*$")
_URLISH_RE: Final = re.compile(r"(?:[a-z][a-z0-9+.-]*:|www\.|//)", re.IGNORECASE)
_MARKUP_RE: Final = re.compile(r"[<>\[\]{}`\\|]|&(?:#\d+|#x[0-9a-f]+|[a-z]+);", re.IGNORECASE)
_ENCODED_BREAK_RE: Final = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)


def _char_problems(value: str) -> list[str]:
    problems: list[str] = []
    if "\r" in value or "\n" in value or "\u2028" in value or "\u2029" in value or "\x85" in value:
        problems.append("HEADER_INJECTION")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"} for c in value):
        problems.append("CONTROL_CHARACTER")
    if "{{" in value or "}}" in value:
        problems.append("TEMPLATE_SYNTAX")
    if _MARKUP_RE.search(value):
        problems.append("RAW_MARKUP")
    if "@" in value:
        problems.append("PERSONAL_DATA_EMAIL")
    return problems


class VehicleLabel(BaseModel):
    """Vehicle label built only from verified make/model (and optional generation) facts."""

    model_config = _FROZEN

    text: str = Field(min_length=1, max_length=MAX_VEHICLE_LABEL_LENGTH)
    make: str
    model: str
    generation: str | None = None
    shortened: bool = False  # True when the generation was dropped to respect the length cap

    @model_validator(mode="after")
    def _safe(self) -> VehicleLabel:
        problems = [p for part in (self.text, self.make, self.model) for p in _label_part_problems(part)]
        if self.generation is not None:
            problems.extend(_label_part_problems(self.generation))
        if problems:
            raise ValueError("unsafe vehicle label: " + ", ".join(sorted(set(problems))))
        # The text is composed from the verified parts only; nothing else can be smuggled in.
        expected = " ".join(p for p in (self.make, self.model, self.generation) if p is not None)
        if self.text != expected:
            raise ValueError("vehicle label text must be exactly 'make model [generation]'")
        if self.shortened and self.generation is not None:
            raise ValueError("a shortened label has dropped its generation")
        return self


def _label_part_problems(value: str) -> list[str]:
    problems = _char_problems(value)
    if not value or value != value.strip() or "  " in value:
        problems.append("LABEL_WHITESPACE")
    if _URLISH_RE.search(value):
        problems.append("URL_NOT_ALLOWED")
    if not _LABEL_PART_RE.fullmatch(value):
        problems.append("LABEL_INVALID_CHARACTERS")
    return problems


def _clean_part(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"[ \t]+", " ", unicodedata.normalize("NFC", value)).strip() or None


def build_vehicle_label(make: str | None, model: str | None, generation: str | None = None) -> VehicleLabel:
    """Build the label from verified facts: ``make model [generation]``.

    Shortening rule: when the full label exceeds ``MAX_VEHICLE_LABEL_LENGTH`` the optional
    generation is dropped; nothing else is ever truncated or invented. Missing make/model or a
    label that is still too long raises ``TemplateRenderError``.
    """
    make_c, model_c, gen_c = _clean_part(make), _clean_part(model), _clean_part(generation)
    problems: list[str] = []
    if make_c is None:
        problems.append("VEHICLE_MAKE_MISSING")
    if model_c is None:
        problems.append("VEHICLE_MODEL_MISSING")
    for part in (make_c, model_c, gen_c):
        if part is not None:
            problems.extend(_label_part_problems(part))
            if len(part) > MAX_LABEL_PART_LENGTH:
                problems.append("LABEL_PART_TOO_LONG")
    if problems or make_c is None or model_c is None:
        raise TemplateRenderError("vehicle label cannot be built from verified facts", problems)
    if model_c.casefold().startswith(make_c.casefold() + " "):
        model_c = model_c[len(make_c) + 1 :]
    if gen_c is not None and gen_c.casefold() in model_c.casefold().split():
        gen_c = None  # generation already part of the model name
    base = f"{make_c} {model_c}"
    if gen_c is not None:
        full = f"{base} {gen_c}"
        if len(full) <= MAX_VEHICLE_LABEL_LENGTH:
            return VehicleLabel(text=full, make=make_c, model=model_c, generation=gen_c)
    if len(base) > MAX_VEHICLE_LABEL_LENGTH:
        raise TemplateRenderError("vehicle label too long", ["VEHICLE_LABEL_TOO_LONG"])
    return VehicleLabel(text=base, make=make_c, model=model_c, generation=None, shortened=gen_c is not None)


def vehicle_label_from_taxonomy(match: TaxonomyMatch) -> VehicleLabel:
    """Label from a taxonomy match on structured fields (never a title-only/low-confidence guess)."""
    if match.matched_via != "fields" or match.make is None or match.model is None:
        raise TemplateRenderError("vehicle not identified from verified fields", ["VEHICLE_NOT_VERIFIED"])
    if match.confidence is None or match.confidence.value == "low":
        raise TemplateRenderError("vehicle identification confidence too low", ["VEHICLE_NOT_VERIFIED"])
    return build_vehicle_label(match.make, match.model, match.generation)


def _reference_problems(reference: str | None) -> list[str]:
    if reference is None or not reference.strip():
        return ["LISTING_REFERENCE_MISSING"]
    problems = _char_problems(reference)
    if reference != reference.strip():
        problems.append("LISTING_REFERENCE_INVALID_CHARACTERS")
    if len(reference) > MAX_LISTING_REFERENCE_LENGTH:
        problems.append("LISTING_REFERENCE_TOO_LONG")
    if _URLISH_RE.search(reference):
        problems.append("URL_NOT_ALLOWED")
    if not _REFERENCE_RE.fullmatch(reference):
        problems.append("LISTING_REFERENCE_INVALID_CHARACTERS")
    return problems


def _display_name_problems(name: str | None) -> list[str]:
    if name is None or not name.strip():
        return ["SENDER_DISPLAY_NAME_MISSING"]
    problems = _char_problems(name)
    if name != name.strip():
        problems.append("SENDER_DISPLAY_NAME_INVALID_CHARACTERS")
    if len(name) > MAX_SENDER_DISPLAY_NAME_LENGTH:
        problems.append("SENDER_DISPLAY_NAME_TOO_LONG")
    if _URLISH_RE.search(name):
        problems.append("URL_NOT_ALLOWED")
    if not _DISPLAY_NAME_RE.fullmatch(name):
        problems.append("SENDER_DISPLAY_NAME_INVALID_CHARACTERS")
    return problems


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def listing_url_problems(url: str | None) -> list[str]:
    """Problems with a listing URL placed into an inquiry (codes only)."""
    if url is None or not url.strip():
        return ["LISTING_URL_MISSING"]
    problems = _char_problems(url)
    if len(url) > MAX_LISTING_URL_LENGTH:
        problems.append("LISTING_URL_TOO_LONG")
    if any(c.isspace() for c in url) or any(c in url for c in "\"'"):
        problems.append("LISTING_URL_INVALID_CHARACTERS")
    if _ENCODED_BREAK_RE.search(url):
        problems.append("HEADER_INJECTION")
    try:
        parts = urlsplit(url)
        port = parts.port
        has_userinfo = parts.username is not None or parts.password is not None or "@" in parts.netloc
    except ValueError:
        return sorted({*problems, "LISTING_URL_INVALID"})
    if parts.scheme not in {"http", "https"}:
        problems.append("LISTING_URL_SCHEME")
    if not parts.hostname or "." not in parts.hostname or _is_ip_literal(parts.hostname):
        problems.append("LISTING_URL_HOST")
    if has_userinfo:
        problems.append("URL_CREDENTIALS")
    if port is not None:
        problems.append("LISTING_URL_PORT")
    if parts.fragment:
        problems.append("LISTING_URL_FRAGMENT")
    problems.extend(
        p.split(":", 1)[0]
        for p in text_problems(url)
        if p.split(":", 1)[0] in {"URL_CREDENTIALS", "URL_TOKEN_PARAMETER", "SECRET_TOKEN", "INVALID_URL"}
    )
    return sorted(set(problems))


class InquiryPlaceholders(BaseModel):
    """The only values ever substituted into a template."""

    model_config = _FROZEN

    vehicle_label: str
    listing_reference: str
    listing_url: str
    sender_display_name: str

    def as_template_values(self) -> dict[str, str]:
        return {
            "vehicle_label": self.vehicle_label,
            "listing_reference": self.listing_reference,
            "listing_url": self.listing_url,
            "verified_sender_display_name": self.sender_display_name,
        }


class RenderedMessage(BaseModel):
    """A rendered, scope-validated message (seller inquiry) or informational MK preview."""

    model_config = _FROZEN

    template_id: str
    template_version: int
    template_hash: str
    template_set_version: str = TEMPLATE_SET_VERSION
    kind: TemplateKind
    language: TemplateLanguage
    subject: str
    body: str
    placeholders: InquiryPlaceholders
    body_hash: str
    scope_hash: str
    source_body_hash: str | None = None  # preview: body_hash of the seller message it mirrors
    requires_approval: Literal[False] = False  # informational preview / standing authorization

    @model_validator(mode="after")
    def _hash(self) -> RenderedMessage:
        if self.body_hash != message_body_hash(self.subject, self.body):
            raise ValueError("body_hash does not match subject and body")
        if (self.kind == "owner_preview") != (self.source_body_hash is not None):
            raise ValueError("only an owner preview references a source message")
        return self


def canonical_message(subject: str, body: str) -> dict[str, str]:
    """Canonical form hashed into ``body_hash``: NFC text with LF line endings."""
    return {
        "subject": unicodedata.normalize("NFC", subject),
        "body": unicodedata.normalize("NFC", body.replace("\r\n", "\n").replace("\r", "\n")),
    }


def message_body_hash(subject: str, body: str) -> str:
    return sha256_json(canonical_message(subject, body))


def _substitute(text: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


def render(
    template_id: str,
    vehicle_label: VehicleLabel,
    listing_reference: str | None,
    listing_url: str,
    sender_display_name: str,
    *,
    verified_listing_url: str,
) -> RenderedMessage:
    """Render a seller-inquiry template with validated placeholders, then validate its scope.

    ``verified_listing_url`` is the listing URL bound in the recipient evidence; the URL placed in
    the message must be identical. Raises ``TemplateRenderError`` with problem codes.
    """
    template = get_template(template_id)
    if template.kind != "seller_inquiry":
        raise TemplateRenderError("previews are rendered with render_preview_mk", ["NOT_A_SELLER_TEMPLATE"])
    problems: list[str] = []
    problems.extend(_reference_problems(listing_reference))
    problems.extend(_display_name_problems(sender_display_name))
    problems.extend(listing_url_problems(listing_url))
    if not problems and listing_url != verified_listing_url:
        problems.append("LISTING_URL_NOT_VERIFIED")
    if problems or listing_reference is None:
        raise TemplateRenderError("inquiry placeholders rejected", problems)
    placeholders = InquiryPlaceholders(
        vehicle_label=vehicle_label.text,
        listing_reference=listing_reference,
        listing_url=listing_url,
        sender_display_name=sender_display_name,
    )
    return _render_with(template, placeholders, source_body_hash=None)


def _render_with(
    template: SellerTemplate, placeholders: InquiryPlaceholders, *, source_body_hash: str | None
) -> RenderedMessage:
    values = placeholders.as_template_values()
    subject = _substitute(template.subject, values)
    body = _substitute(template.body, values)
    message = RenderedMessage(
        template_id=template.template_id,
        template_version=template.version,
        template_hash=template.template_hash(),
        kind=template.kind,
        language=template.language,
        subject=subject,
        body=body,
        placeholders=placeholders,
        body_hash=message_body_hash(subject, body),
        scope_hash=SCOPE_HASH,
        source_body_hash=source_body_hash,
    )
    result = validate_scope(message)
    if not result.ok:
        raise TemplateRenderError("rendered message is outside the bounded inquiry scope", result.problems)
    return message


def render_preview_mk(message: RenderedMessage) -> RenderedMessage:
    """Macedonian informational preview with identical placeholders. Never an approval draft.

    The preview mirrors only an exact rendering of a registered seller template, so Vasko always
    sees a translation of what can actually be sent (never of a hand-built or edited message).
    """
    if message.kind != "seller_inquiry":
        raise TemplateRenderError("a preview mirrors a seller inquiry", ["NOT_A_SELLER_MESSAGE"])
    problems = rendering_problems(message)
    if problems:
        raise TemplateRenderError("a preview mirrors only an exact template rendering", problems)
    return _render_with(
        SELLER_INITIAL_MK_PREVIEW_V1, message.placeholders, source_body_hash=message.body_hash
    )


def rendering_problems(message: RenderedMessage) -> tuple[str, ...]:
    """Why ``message`` is not the exact deterministic rendering of a registered template.

    Empty means: the template id/version/hash/kind/language are registered, every placeholder
    value passes the same validation as ``render``, subject and body are byte-identical to the
    template with those placeholders substituted, and the scope validator passes. A hand-built or
    edited ``RenderedMessage`` (even one whose wording stays inside the scope) is therefore never
    bound to an inquiry or dispatched; a wording fix must be a new registered template version.
    """
    template = TEMPLATES.get(message.template_id)
    if template is None:
        return ("TEMPLATE_NOT_REGISTERED",)
    problems: list[str] = []
    if (
        template.template_hash() != message.template_hash
        or template.version != message.template_version
        or template.kind != message.kind
        or template.language != message.language
    ):
        problems.append("TEMPLATE_NOT_REGISTERED")
    if message.template_set_version != TEMPLATE_SET_VERSION:
        problems.append("TEMPLATE_SET_MISMATCH")
    if message.scope_hash != SCOPE_HASH:
        problems.append("SCOPE_HASH_MISMATCH")
    ph = message.placeholders
    problems.extend(_reference_problems(ph.listing_reference))
    problems.extend(_display_name_problems(ph.sender_display_name))
    problems.extend(listing_url_problems(ph.listing_url))
    problems.extend(_label_part_problems(ph.vehicle_label))
    if len(ph.vehicle_label) > MAX_VEHICLE_LABEL_LENGTH:
        problems.append("VEHICLE_LABEL_TOO_LONG")
    values = ph.as_template_values()
    if (
        _substitute(template.subject, values) != message.subject
        or _substitute(template.body, values) != message.body
    ):
        problems.append("NOT_TEMPLATE_RENDERING")
    if not validate_scope(message).ok:
        problems.append("SCOPE_VALIDATION_FAILED")
    return tuple(sorted(set(problems)))


# ---------------------------------------------------------------------------------------------
# Scope validator
# ---------------------------------------------------------------------------------------------

#: Question classification keys per language, applied in priority order documents > price >
#: availability (the documents question also says "available documents").
_QUESTION_KEYS: Final[Mapping[str, tuple[tuple[QuestionId, re.Pattern[str]], ...]]] = {
    "de": (
        (QuestionId.VEHICLE_DOCUMENTS, re.compile(r"unterlagen|dokument|papiere")),
        (QuestionId.LOWEST_FINAL_PRICE, re.compile(r"preis")),
        (QuestionId.AVAILABILITY, re.compile(r"verfügbar|erhältlich|zu haben")),
    ),
    "it": (
        (QuestionId.VEHICLE_DOCUMENTS, re.compile(r"document|carta di circolazione|libretto")),
        (QuestionId.LOWEST_FINAL_PRICE, re.compile(r"prezzo")),
        (QuestionId.AVAILABILITY, re.compile(r"disponibile|in vendita")),
    ),
    "fr": (
        (QuestionId.VEHICLE_DOCUMENTS, re.compile(r"document|certificat")),
        (QuestionId.LOWEST_FINAL_PRICE, re.compile(r"prix")),
        (QuestionId.AVAILABILITY, re.compile(r"disponible")),
    ),
    "en": (
        (QuestionId.VEHICLE_DOCUMENTS, re.compile(r"document|paperwork")),
        (QuestionId.LOWEST_FINAL_PRICE, re.compile(r"price")),
        (QuestionId.AVAILABILITY, re.compile(r"available|for sale")),
    ),
    "mk": (
        (QuestionId.VEHICLE_DOCUMENTS, re.compile(r"документ")),
        (QuestionId.LOWEST_FINAL_PRICE, re.compile(r"цена")),
        (QuestionId.AVAILABILITY, re.compile(r"достапн")),
    ),
}
_REGISTRATION_KEYS: Final[Mapping[str, re.Pattern[str]]] = {
    "de": re.compile(r"zulassung"),
    "it": re.compile(r"circolazione|libretto"),
    "fr": re.compile(r"immatriculation"),
    "en": re.compile(r"registration"),
    "mk": re.compile(r"сообраќајн"),
}
_LOWEST_KEYS: Final[Mapping[str, re.Pattern[str]]] = {
    "de": re.compile(r"niedrigst|letzte|endpreis"),
    "it": re.compile(r"minimo|finale"),
    "fr": re.compile(r"plus bas|dernier"),
    "en": re.compile(r"lowest|final"),
    "mk": re.compile(r"најниск|последн"),
}
_NON_BINDING_KEYS: Final[Mapping[str, re.Pattern[str]]] = {
    "de": re.compile(r"\bunverbindliche\w* anfrage"),
    "it": re.compile(r"richiesta di informazioni"),
    "fr": re.compile(r"demande de renseignements"),
    "en": re.compile(r"information (?:enquiry|inquiry|request)"),
    "mk": re.compile(r"барање за информации"),
}

# Forbidden lexicon, applied to every message whatever its language (fail closed).
# ``word``: whole word; ``prefix``: word starting with it; ``infix``: anywhere (DE compounds).
_LexEntry = tuple[str, Literal["word", "prefix", "infix"]]


def _lex(kind: Literal["word", "prefix", "infix"], words: str) -> tuple[_LexEntry, ...]:
    return tuple((w.replace("_", " "), kind) for w in words.split())


_FORBIDDEN: Final[Mapping[str, tuple[_LexEntry, ...]]] = {
    "COMMITMENT_OFFER": (
        *_lex("infix", "angebot"),
        *_lex("word", "biete bieten anbieten offerta offerte offro offriamo offrire proposta propongo"),
        *_lex("word", "offre offrir propose proposition offer offers offering bid bidding proposal"),
        *_lex("prefix", "понуд"),
    ),
    "COMMITMENT_PURCHASE": (
        *_lex("word", "kaufe kaufen kauf ankauf sofortkauf kaufvertrag erwerben erwerbe"),
        *_lex("word", "comprare compro comprerei compriamo acquistare acquisto acquisterei"),
        *_lex("word", "acheter achète achèterais achat acquérir buy buying purchase purchasing"),
        *_lex("word", "interested_in_buying купам купувам купи купување"),
    ),
    "COMMITMENT_PRICE_ACCEPTANCE": (
        *_lex("word", "akzeptiere akzeptieren einverstanden zusage zusagen nehme verbindlich"),
        *_lex("word", "verbindliche verbindlichen verbindlicher accetto accettiamo accordo"),
        *_lex("word", "accepte acceptons accord accept accepted accepting agree agreed deal"),
        *_lex("word", "commit commitment promise guarantee binding прифаќам прифатам"),
        *_lex("prefix", "договор"),
    ),
    "COMMITMENT_RESERVATION": (
        *_lex("infix", "reservier zurücklegen"),
        *_lex("word", "prenotare prenotazione prenoto riservare riservo bloccare réserver"),
        *_lex("word", "réservation réserve bloquer reserve reserved reservation hold"),
        *_lex("prefix", "резервир резервациј"),
    ),
    "COMMITMENT_DEPOSIT_OR_PAYMENT": (
        *_lex("infix", "zahlung überweis bargeld kaution"),
        *_lex("word", "zahle zahlen bezahle bezahlen bar in_bar caparra acconto anticipo contanti"),
        *_lex("word", "pagamento pagare pago pagherei pagherò bonifico acompte arrhes avance"),
        *_lex("word", "espèces liquide paiement payer paie paierai paierais virement deposit"),
        *_lex("word", "downpayment down_payment cash pay paying payment wire paypal депозит"),
        *_lex("word", "капар аванс кеш"),
        *_lex("prefix", "готовин плаќањ платам плати уплат"),
    ),
    "APPOINTMENT_OR_VIEWING": (
        *_lex("infix", "termin besichtig probefahrt"),
        *_lex("word", "appuntamento visita visitare visionare prova provare rendez-vous visite"),
        *_lex("word", "visiter essai essayer appointment viewing view visit inspect inspection"),
        *_lex("word", "test_drive test-drive средба термин"),
        *_lex("prefix", "разгледув"),
    ),
    "TRAVEL_OR_COLLECTION": (
        *_lex("infix", "abhol"),
        *_lex("word", "komme vorbeikommen anreise anreisen reise reisen ritiro ritirare viaggio"),
        *_lex("word", "viaggiare vengo venire verrei venir viens viendrai voyage récupérer"),
        *_lex("word", "collect collection pick_up pick-up pickup travel trip come_to come_and"),
        *_lex("word", "доаѓам дојдам"),
        *_lex("prefix", "патув"),
    ),
    "NEGOTIATION": (
        *_lex("infix", "rabatt preisnachlass"),
        *_lex("word", "verhandeln verhandlung sconto trattare trattativa trattabile remise rabais"),
        *_lex("word", "négocier négociation discount negotiate negotiation попуст"),
        *_lex("prefix", "преговар"),
    ),
    "PERSONAL_DATA_FINANCES": (
        *_lex("infix", "finanzier kredit konto"),
        *_lex("word", "finanziamento rate conto banca financement crédit compte banque"),
        *_lex("word", "finance financing loan credit bank account iban bic swift кредит"),
        *_lex("prefix", "банк сметк"),
    ),
    "PERSONAL_DATA_CONTACT_CHANNEL": (
        *_lex("infix", "telefon"),
        *_lex("word", "handy anrufen rufen whatsapp telegram sms chiamare chiamarmi cellulare"),
        *_lex("word", "appeler appelez portable phone call mobile телефон"),
        *_lex("prefix", "јавет"),
    ),
    "PERSONAL_DATA_ADDRESS": (
        *_lex("infix", "adresse anschrift wohnort straße strasse"),
        *_lex("word", "indirizzo residenza domicile address street"),
        *_lex("prefix", "адрес улиц"),
    ),
    "PERSONAL_DATA_IDENTITY_DOCUMENT": (
        *_lex("infix", "ausweis reisepass"),
        *_lex("word", "passaporto carta_d'identità passeport carte_d'identité passport id_card"),
        *_lex("word", "identity_card лична_карта пасош"),
    ),
    "BUDGET_OR_PROFIT": (
        *_lex("infix", "budget gewinn weiterverkauf"),
        *_lex("word", "marge guadagno profitto rivendita rivendere bénéfice revente revendre"),
        *_lex("word", "profit margin resale resell буџет профит"),
        *_lex("prefix", "заработ препродаж"),
    ),
    "UNRELATED_BUSINESS": (
        *_lex("infix", "export mazedon"),
        *_lex("word", "esportazione esportare macedonia macédoine exportation exporter skopje"),
        *_lex("prefix", "извоз македониј скопје"),
    ),
    "ATTACHMENT_REFERENCE": (
        *_lex("word", "anbei anhang allegato allegati ci-joint pièce_jointe attached attachment"),
        *_lex("word", "attachments прилог"),
    ),
}


def _compile_lexicon() -> dict[str, re.Pattern[str]]:
    compiled: dict[str, re.Pattern[str]] = {}
    for category, entries in _FORBIDDEN.items():
        parts: list[str] = []
        for text, kind in entries:
            escaped = re.escape(text.lower())
            if kind == "word":
                parts.append(rf"(?<![\w-]){escaped}(?![\w-])")
            elif kind == "prefix":
                parts.append(rf"(?<![\w-]){escaped}")
            else:
                parts.append(escaped)
        compiled[category] = re.compile("|".join(parts))
    return compiled


_FORBIDDEN_RE: Final = _compile_lexicon()
_CURRENCY_RE: Final = re.compile(
    r"[€$£¥₣]|(?<![\w-])(?:eur|euro|euros|chf|usd|gbp|mkd|денари|франк\w*)(?![\w-])"
)
_IBAN_RE: Final = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_HEADER_LINE_RE: Final = re.compile(
    r"^\s*(?:to|cc|bcc|from|reply-to|sender|subject|content-type|content-transfer-encoding|"
    r"mime-version|x-[\w-]+)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_URL_RE: Final = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_DOMAIN_RE: Final = re.compile(
    r"(?<![\w@.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|info|biz|eu|de|it|fr|ch|at|mk|io|co|uk|nl|be)"
    r"(?![\w-])",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE: Final = re.compile(r"(?<=[.!?])\s+")


class MessageEnvelope(BaseModel):
    """Transport envelope of an outgoing inquiry, checked against the scope (spec 37.1)."""

    model_config = _FROZEN

    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()  # names/ids of any attachment; must be empty
    extra_headers: Mapping[str, str] = Field(default_factory=dict)


ALLOWED_EXTRA_HEADERS: Final = frozenset(
    {"message-id", "date", "mime-version", "content-type", "content-transfer-encoding"}
)


class ScopeValidation(BaseModel):
    model_config = _FROZEN

    ok: bool
    problems: tuple[str, ...]
    questions: tuple[QuestionId, ...]
    language: str
    scope_hash: str = SCOPE_HASH


def _normalize_for_scan(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return folded.replace("\u2019", "'").replace("\u2018", "'")


#: A single plain-text part only: no HTML alternative, no multipart container (attachments).
_CONTENT_TYPE_RE: Final = re.compile(r'^text/plain(?:\s*;\s*charset="?(?:utf-8|us-ascii)"?)?$', re.IGNORECASE)
_TRANSFER_ENCODINGS: Final = frozenset({"7bit", "8bit", "quoted-printable", "base64"})
_MESSAGE_ID_RE: Final = re.compile(r"^<[^\s<>@\"(),:;\[\]\\]{1,200}@[A-Za-z0-9.-]{1,253}>$")
_MAX_HEADER_VALUE_LENGTH: Final = 256


def _extra_header_problems(name: str, value: str) -> list[str]:
    problems: list[str] = []
    if any(c in value or c in name for c in "\r\n\x00\x85\u2028\u2029"):
        problems.append("HEADER_INJECTION")
    lowered = name.strip().lower()
    if lowered not in ALLOWED_EXTRA_HEADERS or name != name.strip():
        problems.append("HEADER_NOT_ALLOWED")
    if not value.isascii() or not value.isprintable() or len(value) > _MAX_HEADER_VALUE_LENGTH:
        problems.append("HEADER_VALUE_INVALID")
    stripped = value.strip()
    if lowered == "content-type" and not _CONTENT_TYPE_RE.fullmatch(stripped):
        problems.append("MIME_NOT_PLAIN_TEXT")  # multipart (attachments) or HTML is never sent
    valid = {
        "content-transfer-encoding": stripped.lower() in _TRANSFER_ENCODINGS,
        "mime-version": stripped == "1.0",
        "message-id": _MESSAGE_ID_RE.fullmatch(stripped) is not None,
    }
    if not valid.get(lowered, True):
        problems.append("HEADER_VALUE_INVALID")
    return problems


def _envelope_problems(envelope: MessageEnvelope) -> list[str]:
    problems: list[str] = []
    if len(envelope.to) != 1:
        problems.append("EXTRA_RECIPIENT" if len(envelope.to) > 1 else "RECIPIENT_MISSING")
    if envelope.cc or envelope.bcc:
        problems.append("CC_BCC")
    if len(envelope.reply_to) > 1:
        problems.append("EXTRA_REPLY_TO")
    if envelope.attachments:
        problems.append("ATTACHMENT")
    seen: set[str] = set()
    for name, value in envelope.extra_headers.items():
        problems.extend(_extra_header_problems(name, value))
        lowered = name.strip().lower()
        if lowered in seen:
            problems.append("HEADER_DUPLICATED")
        seen.add(lowered)
    for address in (*envelope.to, *envelope.reply_to):
        if any(c in address for c in "\r\n\x00\x85\u2028\u2029,;<>"):
            problems.append("HEADER_INJECTION")
            continue
        try:
            canonicalize_address(address)
        except AddressError as exc:
            problems.append("HEADER_INJECTION" if exc.problem == "HEADER_INJECTION" else "INVALID_ADDRESS")
    return problems


def validate_scope(message: RenderedMessage, *, envelope: MessageEnvelope | None = None) -> ScopeValidation:
    """Prove that ``message`` stays inside the bounded three-question scope (see module doc)."""
    language = message.language
    subject, body = message.subject, message.body
    ph = message.placeholders
    problems: list[str] = []

    for value in (subject, body):
        if "{{" in value or "}}" in value:
            problems.append("UNRENDERED_PLACEHOLDER")
        if any(unicodedata.category(c) in {"Cc", "Cf", "Co", "Cs", "Cn"} and c != "\n" for c in value):
            problems.append("CONTROL_CHARACTER")
        if _MARKUP_RE.search(value):
            problems.append("RAW_MARKUP")
    if any(c in subject for c in "\r\n\u2028\u2029\x85"):
        problems.append("HEADER_INJECTION")
    if "\r" in body or "\u2028" in body or "\u2029" in body:
        problems.append("HEADER_INJECTION")
    if _HEADER_LINE_RE.search(body):
        problems.append("HEADER_INJECTION")
    if len(subject) > MAX_SUBJECT_LENGTH:
        problems.append("SUBJECT_TOO_LONG")
    if len(body) > MAX_BODY_LENGTH or body.count("\n") > MAX_BODY_LINES:
        problems.append("BODY_TOO_LONG")
    if ph.listing_reference not in subject:
        problems.append("LISTING_REFERENCE_MISSING")
    problems.extend(listing_url_problems(ph.listing_url))

    # URLs: exactly one, the verified listing URL, in the body only.
    urls = _URL_RE.findall(body)
    if urls.count(ph.listing_url) != 1 or body.count(ph.listing_url) != 1:
        problems.append("LISTING_URL_MISSING" if ph.listing_url not in body else "EXTRA_URL")
    if any(url != ph.listing_url for url in urls) or _URL_RE.search(subject):
        problems.append("EXTRA_URL")
    without_url = body.replace(ph.listing_url, " ")
    if _DOMAIN_RE.search(without_url) or _DOMAIN_RE.search(subject):
        problems.append("EXTRA_URL")

    scan_text = f"{subject}\n{without_url}"
    for code in text_problems(scan_text):
        base = code.split(":", 1)[0]
        mapped = {
            "PHONE_NUMBER": "PERSONAL_DATA_PHONE",
            "EMAIL_ADDRESS": "PERSONAL_DATA_EMAIL",
            "SECRET_TOKEN": "SECRET_TOKEN",
            "RAW_HTML": "RAW_MARKUP",
        }.get(base)
        if mapped:
            problems.append(mapped)
    if "@" in scan_text:
        problems.append("PERSONAL_DATA_EMAIL")
    if _IBAN_RE.search(unicodedata.normalize("NFKC", scan_text).upper()):
        problems.append("PERSONAL_DATA_FINANCES")

    normalized = _normalize_for_scan(scan_text)
    for category, pattern in _FORBIDDEN_RE.items():
        if pattern.search(normalized):
            problems.append(category)
    if _CURRENCY_RE.search(normalized):
        problems.append("CURRENCY")
    # Numbers are allowed only inside the bounded placeholder values (label, reference).
    stripped = scan_text
    for value in sorted({ph.vehicle_label, ph.listing_reference}, key=len, reverse=True):
        stripped = stripped.replace(value, " ")
    if any(unicodedata.category(c) == "Nd" or c in "½¼¾" for c in stripped):
        problems.append("PRICE_OR_NUMBER")

    # Signature: the last non-empty body line is exactly the verified sender display name.
    lines = [line for line in body.split("\n") if line.strip()]
    if not lines or lines[-1] != ph.sender_display_name:
        problems.append("SIGNATURE_MISMATCH")

    # Questions: exactly the three permitted ones, once each.
    found: list[QuestionId] = []
    keys = _QUESTION_KEYS[language]
    non_binding = False
    question_count = 0
    for line in without_url.split("\n"):  # a "?" inside the listing URL is not a question
        for sentence in _SENTENCE_SPLIT_RE.split(line.strip()):
            text = _normalize_for_scan(sentence)
            if not text:
                continue
            if text.count("?") > 1 or "¿" in text:
                problems.append("EXTRA_QUESTION")
            if text.endswith("?"):
                question_count += 1
                matched = next((qid for qid, rx in keys if rx.search(text)), None)
                if matched is None:
                    problems.append("UNRECOGNISED_QUESTION")
                    continue
                found.append(matched)
                if matched == QuestionId.VEHICLE_DOCUMENTS and (
                    "coc" not in text or not _REGISTRATION_KEYS[language].search(text)
                ):
                    problems.append("DOCUMENTS_QUESTION_INCOMPLETE")
                if matched == QuestionId.LOWEST_FINAL_PRICE and not _LOWEST_KEYS[language].search(text):
                    problems.append("PRICE_QUESTION_NOT_LOWEST_FINAL")
            elif "?" in text:
                problems.append("EXTRA_QUESTION")
            elif _NON_BINDING_KEYS[language].search(text):
                non_binding = True
    if "?" in subject or question_count > len(PERMITTED_QUESTIONS):
        problems.append("EXTRA_QUESTION")
    for qid in PERMITTED_QUESTIONS:
        count = found.count(qid)
        if count == 0:
            problems.append(f"QUESTION_MISSING:{qid.value}")
        elif count > 1:
            problems.append(f"QUESTION_DUPLICATED:{qid.value}")
    if not non_binding:
        problems.append("NON_BINDING_STATEMENT_MISSING")
    if envelope is not None:
        problems.extend(_envelope_problems(envelope))

    unique = tuple(sorted(set(problems)))
    return ScopeValidation(ok=not unique, problems=unique, questions=tuple(found), language=language)


def require_scope(message: RenderedMessage, *, envelope: MessageEnvelope | None = None) -> ScopeValidation:
    result = validate_scope(message, envelope=envelope)
    if not result.ok:
        raise TemplateRenderError("message is outside the bounded inquiry scope", result.problems)
    return result


__all__ = [
    "ALLOWED_EXTRA_HEADERS",
    "ALLOWED_OUTGOING_DATA_CATEGORIES",
    "EXCLUDED_DATA_CATEGORIES",
    "INQUIRY_PURPOSE",
    "MAX_LISTING_URL_LENGTH",
    "MAX_VEHICLE_LABEL_LENGTH",
    "MK_PREVIEW_TEMPLATE_ID",
    "PERMITTED_QUESTIONS",
    "SCOPE_CONTRACT",
    "SCOPE_HASH",
    "SCOPE_VERSION",
    "SELLER_INITIAL_DE_V1",
    "SELLER_INITIAL_EN_V1",
    "SELLER_INITIAL_FR_V1",
    "SELLER_INITIAL_IT_V1",
    "SELLER_INITIAL_MK_PREVIEW_V1",
    "TEMPLATES",
    "TEMPLATE_BY_LANGUAGE",
    "TEMPLATE_SET_VERSION",
    "InquiryPlaceholders",
    "MessageEnvelope",
    "QuestionId",
    "RenderedMessage",
    "ScopeValidation",
    "SellerTemplate",
    "TemplateRenderError",
    "VehicleLabel",
    "build_vehicle_label",
    "canonical_message",
    "get_template",
    "listing_url_problems",
    "message_body_hash",
    "render",
    "render_preview_mk",
    "rendering_problems",
    "require_scope",
    "template_for_language",
    "validate_scope",
    "vehicle_label_from_taxonomy",
]
