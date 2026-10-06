"""Evidence-based language determination for the bounded seller inquiry (spec 37.3).

Pure functions, no I/O. Business rules:

- Precedence: a *verified* seller language preference, then the language of the actual
  seller-written advertisement text supported by text evidence. Nothing else decides.
- Website navigation language and the listing country are recorded for audit only; alone they
  are insufficient. Switzerland may need German, French or Italian, decided by text evidence.
- English is used only for an English advertisement or a positively established English
  preference. It is never the unknown-language fallback.
- Mixed or insufficient evidence yields ``language_unresolved`` (gather more evidence or route to
  technical review). A detected language without a template (e.g. Dutch, Polish) yields
  ``unsupported_language``: held for template implementation, never replaced with English.
- Machine-translated or platform-generated text is not seller-written and is ignored.

Detection (``detect_text_language``) is deterministic lexical scoring: function words and
car-advertisement vocabulary per language (a word listed for several languages is split between
them and is never decisive), plus small capped character/n-gram signals (umlauts, ``ß``, ``ç``,
``-zione``...). A language is reported only with a minimum number of distinct exclusive evidence
words and a clear margin over the runner-up. English additionally needs exclusive English
*function* words, because English automotive terms are widely borrowed in DE/IT/FR ads.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import MessageLanguage
from suv_deals.domain.notifications import sanitize_seller_text
from suv_deals.domain.provenance import FieldProvenance

LANGUAGE_RULES_VERSION: Final = "inquiry_language@1.0.0"

SUPPORTED_LANGUAGES: Final[frozenset[str]] = frozenset(m.value for m in MessageLanguage)
#: Languages the detector can positively recognise. Only ``SUPPORTED_LANGUAGES`` have templates;
#: the others exist so an ad in such a language is ``unsupported_language``, not a guess.
DETECTABLE_LANGUAGES: Final[tuple[str, ...]] = ("de", "it", "fr", "en", "nl", "pl", "es", "pt", "cs", "hr")

MIN_EVIDENCE_TOKENS: Final = 3  # distinct exclusive lexicon words for the winning language
MIN_ENGLISH_FUNCTION_WORDS: Final = 3  # distinct exclusive English function words
MARGIN_RATIO: Final = Decimal(2)  # winner score must be >= 2x the runner-up score
MIXED_SECOND_EVIDENCE: Final = 3  # runner-up with this much evidence inside the margin = mixed
FULL_EVIDENCE_TOKENS: Final = 6  # evidence count at which coverage stops limiting confidence
MAX_TEXT_CHARS: Final = 20_000
MAX_TOKENS: Final = 4_000
EXCERPT_CHARS: Final = 160

_FUNCTION_WEIGHT: Final = Decimal(1)
_DOMAIN_WEIGHT: Final = Decimal("1.25")
_CHAR_WEIGHT: Final = Decimal("0.5")
_NGRAM_WEIGHT: Final = Decimal("0.15")
_SIGNAL_CAP: Final = Decimal(3)
_CENT: Final = Decimal("0.01")

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# ---------------------------------------------------------------------------------------------
# Lexicons (lower-case, NFKC). Function words carry grammar; domain words are car-ad vocabulary.
# ---------------------------------------------------------------------------------------------

_FUNCTION_WORDS: Final[Mapping[str, str]] = {
    "de": (
        "der die das und ist mit nicht ein eine einen einem einer auf für im dem den des sich auch "
        "wird wurde wurden sind hat haben von zu zum zur bei aus nach noch sehr nur oder aber wie "
        "alle keine kein ohne über unter durch wir ich werden kann können bitte gerne vom beim "
        "dieses dieser diese neu neuen neuer neues gut guter gutem sowie bereits in es"
    ),
    "it": (
        "il lo gli di del della dei delle degli è con per un una uno non che sono molto anche "
        "come più ma solo ancora già nel nella sul sulla al alla dal dalla ha ho questo questa "
        "tutti tutto senza dopo sempre ed nei sui alle agli ogni la le i e a in si da"
    ),
    "fr": (
        "le la les de des du un une et est avec pour dans sur pas ne que qui au aux ce cette ces "
        "son sa ses très tout tous plus mais ou sans été sont nous vous je il elle à par bon "
        "bonne bien leur en"
    ),
    "en": (
        "the and is with for this that of to it on has have been are was very not all from will "
        "be an as at by or but no any our we you your can just only which there these those a in "
        "would should into"
    ),
    "nl": (
        "de het een en van is met voor op niet zijn ook aan er maar nog wel als dan bij uit deze "
        "dit wordt worden zeer goed goede heeft geen"
    ),
    "pl": (
        "i w z na do nie się jest to że o od po ze jak ale tak są oraz przez dla bardzo jego "
        "który która"
    ),
    "es": (
        "el la los las de del y en es un una con por para que no muy se su sus al lo más como "
        "pero sin está tiene"
    ),
    "pt": (
        "o a os as de do da dos das e em um uma com para por não que se no na muito mais tem são"
    ),
    "cs": ("a v na je se s z k o do to že pro jako ale nebo jsem jsou byl bylo velmi"),
    "hr": ("i u je na se za od da su s sa ne bez ili ali vrlo bio bilo"),
}

_DOMAIN_WORDS: Final[Mapping[str, str]] = {
    "de": (
        "fahrzeug unfallfrei scheckheftgepflegt scheckheft gepflegt zustand tüv anhängerkupplung "
        "klimaanlage klimaautomatik sitzheizung zahnriemen inspektion nichtraucher "
        "nichtraucherfahrzeug allrad schaltgetriebe reifen winterreifen sommerreifen "
        "kilometerstand rechnung mwst ausweisbar händler gewährleistung leder standheizung "
        "fahrbereit getriebe kupplung bremsen lack beulen kratzer rost wartung zulassung "
        "abgemeldet angemeldet papiere fahrzeugbrief fahrzeugschein ausstattung verkaufe "
        "besitzer vorbesitzer"
    ),
    "it": (
        "vettura veicolo ottime ottimo ottima condizioni perfette tagliandi tagliandata tagliando "
        "gomme pneumatici cerchi climatizzatore unico proprietario garanzia chilometri revisione "
        "cinghia distribuzione frizione carrozzeria interni pelle navigatore sensori parcheggio "
        "trattabile prezzo permuta finanziamento disponibile visibile sede iva esposta "
        "neopatentati cambio manuale automatico trazione integrale gancio traino vendo"
    ),
    "fr": (
        "véhicule voiture état entretien entretenu révision première main carnet contrôle "
        "technique pneus jantes climatisation embrayage boîte vitesses cuir kilométrage prix tva "
        "récupérable attelage factures propriétaire aucun frais prévoir roulant vends vendue"
    ),
    "en": (
        "condition history owner owners tyres tires miles mileage previous alloy wheels leather "
        "heated seats mot warranty gearbox automatic clean drives recently replaced "
        "cambelt timing belt sale please selling good"
    ),
    "nl": (
        "auto onderhoud onderhouden nieuwe nieuw banden velgen airco trekhaak apk btw inruil "
        "prijs schade schadevrij eigenaar rijklaar onderhoudsboekjes distributieriem koppeling "
        "versnellingsbak"
    ),
    "pl": (
        "stan samochód pojazd przebieg serwisowany bezwypadkowy właściciel opony felgi "
        "klimatyzacja sprowadzony zarejestrowany ubezpieczony cena faktura skrzynia biegów napęd "
        "silnik sprzedam polecam zadbany oryginalny lakier"
    ),
    "es": (
        "coche vehículo estado perfecto buen revisiones neumáticos llantas aire acondicionado "
        "kilómetros único propietario garantía precio automático tracción transferible itv "
        "financiación vendo"
    ),
    "pt": (
        "carro viatura estado revisões pneus jantes ar condicionado quilómetros único dono "
        "garantia preço caixa"
    ),
    "cs": (
        "vůz auto stav servisní kniha najeto nehavarováno majitel pneu kola klimatizace cena dph "
        "tažné zařízení převodovka pohon"
    ),
    "hr": (
        "auto vozilo stanje servisna knjiga prešao prvi vlasnik gume felge klima cijena "
        "registriran uvezen mjenjač pogon"
    ),
}

#: Exclusive characters (each occurrence adds ``_CHAR_WEIGHT``, capped per language).
_CHAR_SIGNALS: Final[Mapping[str, str]] = {
    "de": "äöüß",
    "it": "ìò",
    "fr": "çœêâîôûëï",
    "es": "ñ¿¡",
    "pt": "ãõ",
    "pl": "ąęłśźżń",
    "cs": "ěřů",
    "hr": "đ",
}

#: Character n-grams typical for a language (each occurrence adds ``_NGRAM_WEIGHT``, capped).
_NGRAM_SIGNALS: Final[Mapping[str, tuple[str, ...]]] = {
    "de": ("sch", "ung", "eit", "cht", "lich"),
    "it": ("zione", "gli", "cch", "ggi", "zz"),
    "fr": ("eau", "aux", "eux", "oir", "ée"),
    "en": ("th", "ing", "wh", "ght"),
    "nl": ("ij", "aa", "uu", "oe"),
    "es": ("ción", "ll"),
    "pt": ("ção", "ões", "nh", "lh"),
    "pl": ("rz", "sz", "cz", "ść", "ię"),
    "cs": ("ř", "ě", "ů"),
    "hr": ("lj", "nj", "dž"),
}


def _build_lexicon() -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    owners: dict[str, list[str]] = {}
    function_words: dict[str, set[str]] = {lang: set() for lang in DETECTABLE_LANGUAGES}
    for table, is_function in ((_FUNCTION_WORDS, True), (_DOMAIN_WORDS, False)):
        for lang, words in table.items():
            for raw in words.split():
                word = unicodedata.normalize("NFKC", raw).lower()
                langs = owners.setdefault(word, [])
                if lang not in langs:
                    langs.append(lang)
                if is_function:
                    function_words[lang].add(word)
    return (
        {word: tuple(sorted(langs)) for word, langs in owners.items()},
        {lang: frozenset(words) for lang, words in function_words.items()},
    )


_WORD_OWNERS, _LANG_FUNCTION_WORDS = _build_lexicon()

_URL_RE: Final = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE: Final = re.compile(r"\S+@\S+")
# Words are maximal runs of letters, optionally joined by an internal apostrophe ("it's").
_WORD_RE: Final = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")


# ---------------------------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------------------------


class DetectionReason(StrEnum):
    DETECTED = "detected"
    NO_TEXT = "no_text"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AMBIGUOUS = "ambiguous"  # no clear margin between the two best languages
    MIXED_LANGUAGES = "mixed_languages"  # two languages each with real evidence
    ENGLISH_EVIDENCE_TOO_WEAK = "english_evidence_too_weak"
    NON_LATIN_SCRIPT = "non_latin_script"


class LanguageScore(BaseModel):
    model_config = _FROZEN

    language: str
    score: Decimal
    evidence_tokens: int = Field(ge=0)  # distinct exclusive lexicon words
    function_tokens: int = Field(ge=0)  # distinct exclusive function words


class TextLanguageDetection(BaseModel):
    """Deterministic detection result for one text."""

    model_config = _FROZEN

    language: str | None  # ISO 639-1 from DETECTABLE_LANGUAGES; None = not established
    confidence: Decimal = Field(ge=0, le=1)
    reason: DetectionReason
    evidence_tokens: tuple[str, ...] = ()  # exclusive words that support ``language`` (bounded)
    evidence_excerpt: str | None = None  # sanitized seller-text window (no links/contacts)
    scores: tuple[LanguageScore, ...] = ()  # sorted by score desc, then language
    token_count: int = Field(default=0, ge=0)
    non_latin_letter_share: Decimal = Decimal(0)
    rules_version: str = LANGUAGE_RULES_VERSION

    @property
    def supported(self) -> bool:
        return self.language in SUPPORTED_LANGUAGES


class LanguageStatus(StrEnum):
    RESOLVED = "resolved"
    LANGUAGE_UNRESOLVED = "language_unresolved"
    UNSUPPORTED_LANGUAGE = "unsupported_language"


class LanguageReason(StrEnum):
    VERIFIED_SELLER_PREFERENCE = "verified_seller_preference"
    VERIFIED_PREFERENCE_UNSUPPORTED = "verified_preference_unsupported"
    SELLER_AD_TEXT = "seller_ad_text"
    AD_TEXT_UNSUPPORTED_LANGUAGE = "ad_text_unsupported_language"
    AD_TEXT_NON_LATIN_SCRIPT = "ad_text_non_latin_script"
    NO_SELLER_WRITTEN_TEXT = "no_seller_written_text"
    INSUFFICIENT_TEXT_EVIDENCE = "insufficient_text_evidence"
    AMBIGUOUS_TEXT_EVIDENCE = "ambiguous_text_evidence"
    MIXED_LANGUAGE_TEXT = "mixed_language_text"
    FRAGMENTS_DISAGREE = "fragments_disagree"
    ENGLISH_EVIDENCE_TOO_WEAK = "english_evidence_too_weak"


PreferenceEvidenceKind = Literal["seller_stated_language", "prior_seller_correspondence"]
LanguageBasis = Literal["verified_seller_preference", "seller_ad_text", "none"]


def _normalize_code(value: str) -> str:
    code = value.strip().replace("_", "-").split("-", 1)[0].casefold()
    if not re.fullmatch(r"[a-z]{2}", code):
        raise ValueError("language must be an ISO 639-1 code, optionally with a region (de-CH)")
    return code


class SellerLanguagePreference(BaseModel):
    """A seller's language preference. Only ``verified=True`` preferences are used.

    Positive establishment means the seller stated it (e.g. "English spoken" in the ad) or wrote
    to us in that language. A marketplace UI language or the listing country is not a preference.
    """

    model_config = _FROZEN

    language: str
    verified: bool
    evidence_kind: PreferenceEvidenceKind
    evidence_excerpt: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=2048)
    observed_at: datetime

    @field_validator("language", mode="before")
    @classmethod
    def _code(cls, value: object) -> object:
        return _normalize_code(value) if isinstance(value, str) else value

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _evidence(self) -> SellerLanguagePreference:
        if self.verified and not (self.evidence_excerpt and self.evidence_excerpt.strip()):
            raise ValueError("a verified language preference needs an evidence excerpt")
        return self


AdTextField = Literal["title", "description", "seller_note"]


class AdTextFragment(BaseModel):
    """A piece of advertisement text with provenance.

    Only seller-written free text counts. Adapters set ``seller_written=False`` for platform
    labels/equipment lists and ``machine_translated=True`` when the platform marks a text as an
    automatic translation; such fragments are recorded but never decide the language.
    """

    model_config = _FROZEN

    field: AdTextField
    text: str = Field(max_length=MAX_TEXT_CHARS)
    seller_written: bool
    machine_translated: bool = False
    provenance: FieldProvenance

    @property
    def eligible(self) -> bool:
        return self.seller_written and not self.machine_translated and bool(self.text.strip())


class LanguageDecision(BaseModel):
    """Inquiry language decision with its evidence (stored with the seller contact)."""

    model_config = _FROZEN

    language: MessageLanguage | None
    status: LanguageStatus
    confidence: Decimal = Field(ge=0, le=1)
    evidence_excerpt: str | None
    reason: LanguageReason
    basis: LanguageBasis
    detected_language: str | None = None  # raw detected/preferred code, also for unsupported ones
    country: str | None = None  # recorded only; never decisive
    site_navigation_language: str | None = None  # recorded only; never decisive
    notes: tuple[str, ...] = ()
    rules_version: str = LANGUAGE_RULES_VERSION

    @model_validator(mode="after")
    def _consistent(self) -> LanguageDecision:
        if (self.language is not None) != (self.status == LanguageStatus.RESOLVED):
            raise ValueError("a language is set exactly when the status is resolved")
        if self.status == LanguageStatus.RESOLVED and self.basis == "none":
            raise ValueError("a resolved language needs a basis")
        return self

    @property
    def resolved(self) -> bool:
        return self.status == LanguageStatus.RESOLVED


# ---------------------------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------------------------


def _prepare(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    normalized = _URL_RE.sub(" ", normalized)
    return _EMAIL_RE.sub(" ", normalized)


def _non_latin_share(text: str) -> Decimal:
    latin = other = 0
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            other += 1
            continue
        if name.startswith("LATIN"):
            latin += 1
        else:
            other += 1
    total = latin + other
    if total == 0:
        return Decimal(0)
    return (Decimal(other) / Decimal(total)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _excerpt(original: str, word: str | None) -> str | None:
    """A short sanitized window of seller text around the first evidence word."""
    prepared = _prepare(original)
    start = 0
    if word:
        match = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", prepared.lower())
        if match:
            start = max(0, match.start() - EXCERPT_CHARS // 4)
    window = prepared[start : start + EXCERPT_CHARS * 2]
    return sanitize_seller_text(window, max_length=EXCERPT_CHARS)


def detect_text_language(text: str | None) -> TextLanguageDetection:
    """Deterministically score ``text`` and return the language only with clear evidence."""
    if text is None or not text.strip():
        return TextLanguageDetection(language=None, confidence=Decimal(0), reason=DetectionReason.NO_TEXT)
    prepared = _prepare(text)
    folded = prepared.lower()  # not casefold(): it would turn "ß" into "ss"
    non_latin = _non_latin_share(folded)
    tokens = [m.group(0).replace("’", "'") for m in _WORD_RE.finditer(folded)][:MAX_TOKENS]
    if non_latin >= Decimal("0.5"):
        return TextLanguageDetection(
            language=None,
            confidence=Decimal(0),
            reason=DetectionReason.NON_LATIN_SCRIPT,
            token_count=len(tokens),
            non_latin_letter_share=non_latin,
        )

    scores: dict[str, Decimal] = dict.fromkeys(DETECTABLE_LANGUAGES, Decimal(0))
    evidence: dict[str, list[str]] = {lang: [] for lang in DETECTABLE_LANGUAGES}
    for token in tokens:
        candidates = [token]
        if "'" in token:  # French elision / English contraction: score both halves too
            candidates.extend(part for part in token.split("'") if part)
        for candidate in candidates:
            owners = _WORD_OWNERS.get(candidate)
            if not owners:
                continue
            for lang in owners:
                weight = _FUNCTION_WEIGHT if candidate in _LANG_FUNCTION_WORDS[lang] else _DOMAIN_WEIGHT
                scores[lang] += weight / len(owners)
            if len(owners) == 1 and candidate not in evidence[owners[0]]:
                evidence[owners[0]].append(candidate)

    for lang in DETECTABLE_LANGUAGES:
        chars = _CHAR_SIGNALS.get(lang, "")
        char_score = sum((_CHAR_WEIGHT for c in folded if c in chars), Decimal(0))
        ngram_score = sum(
            (_NGRAM_WEIGHT * folded.count(gram) for gram in _NGRAM_SIGNALS.get(lang, ())), Decimal(0)
        )
        scores[lang] += min(char_score, _SIGNAL_CAP) + min(ngram_score, _SIGNAL_CAP)

    table = tuple(
        sorted(
            (
                LanguageScore(
                    language=lang,
                    score=scores[lang].quantize(_CENT, rounding=ROUND_HALF_UP),
                    evidence_tokens=len(evidence[lang]),
                    function_tokens=sum(1 for w in evidence[lang] if w in _LANG_FUNCTION_WORDS[lang]),
                )
                for lang in DETECTABLE_LANGUAGES
            ),
            key=lambda s: (-s.score, s.language),
        )
    )
    best, second = table[0], table[1]
    common = {"token_count": len(tokens), "scores": table, "non_latin_letter_share": non_latin}

    if best.score == 0 or best.evidence_tokens < MIN_EVIDENCE_TOKENS:
        return TextLanguageDetection(
            language=None, confidence=Decimal(0), reason=DetectionReason.INSUFFICIENT_EVIDENCE, **common
        )
    if best.score == second.score or best.score < second.score * MARGIN_RATIO:
        reason = (
            DetectionReason.MIXED_LANGUAGES
            if second.evidence_tokens >= MIXED_SECOND_EVIDENCE
            else DetectionReason.AMBIGUOUS
        )
        return TextLanguageDetection(language=None, confidence=Decimal(0), reason=reason, **common)
    if best.language == "en" and best.function_tokens < MIN_ENGLISH_FUNCTION_WORDS:
        return TextLanguageDetection(
            language=None, confidence=Decimal(0), reason=DetectionReason.ENGLISH_EVIDENCE_TOO_WEAK, **common
        )

    share = best.score / (best.score + second.score)
    coverage = min(Decimal(1), Decimal(best.evidence_tokens) / Decimal(FULL_EVIDENCE_TOKENS))
    confidence = (share * (Decimal("0.5") + Decimal("0.5") * coverage)).quantize(_CENT, rounding=ROUND_HALF_UP)
    words = tuple(evidence[best.language][:12])
    return TextLanguageDetection(
        language=best.language,
        confidence=min(confidence, Decimal(1)),
        reason=DetectionReason.DETECTED,
        evidence_tokens=words,
        evidence_excerpt=_excerpt(text, words[0] if words else None),
        **common,
    )


# ---------------------------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------------------------

_DETECTION_TO_REASON: Final[dict[DetectionReason, LanguageReason]] = {
    DetectionReason.NO_TEXT: LanguageReason.NO_SELLER_WRITTEN_TEXT,
    DetectionReason.INSUFFICIENT_EVIDENCE: LanguageReason.INSUFFICIENT_TEXT_EVIDENCE,
    DetectionReason.AMBIGUOUS: LanguageReason.AMBIGUOUS_TEXT_EVIDENCE,
    DetectionReason.MIXED_LANGUAGES: LanguageReason.MIXED_LANGUAGE_TEXT,
    DetectionReason.ENGLISH_EVIDENCE_TOO_WEAK: LanguageReason.ENGLISH_EVIDENCE_TOO_WEAK,
}


def _country_code(country: str | None) -> str | None:
    if country is None:
        return None
    code = country.strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def resolve_inquiry_language(
    seller_preference: SellerLanguagePreference | None,
    ad_texts: Sequence[AdTextFragment],
    site_navigation_language: str | None = None,
    country: str | None = None,
) -> LanguageDecision:
    """Decide the inquiry language from verified preference, then seller-written ad text.

    ``site_navigation_language`` and ``country`` are recorded in the decision for audit but never
    decide it. English is never a fallback; an unsupported language is held, not replaced.
    """
    nav = None
    if site_navigation_language:
        try:
            nav = _normalize_code(site_navigation_language)
        except ValueError:
            nav = None
    cc = _country_code(country)
    notes: list[str] = []
    if nav is not None:
        notes.append("site navigation language recorded only; insufficient to choose a language")
    if cc is not None:
        notes.append("listing country recorded only; insufficient to choose a language")
    if cc == "CH":
        notes.append("Switzerland: German, French or Italian is decided by text evidence only")
    context = {"country": cc, "site_navigation_language": nav}

    if seller_preference is not None and seller_preference.verified:
        code = seller_preference.language
        excerpt = sanitize_seller_text(seller_preference.evidence_excerpt, max_length=EXCERPT_CHARS)
        if code in SUPPORTED_LANGUAGES:
            return LanguageDecision(
                language=MessageLanguage(code),
                status=LanguageStatus.RESOLVED,
                confidence=Decimal(1),
                evidence_excerpt=excerpt,
                reason=LanguageReason.VERIFIED_SELLER_PREFERENCE,
                basis="verified_seller_preference",
                detected_language=code,
                notes=tuple(notes),
                **context,
            )
        return LanguageDecision(
            language=None,
            status=LanguageStatus.UNSUPPORTED_LANGUAGE,
            confidence=Decimal(1),
            evidence_excerpt=excerpt,
            reason=LanguageReason.VERIFIED_PREFERENCE_UNSUPPORTED,
            basis="verified_seller_preference",
            detected_language=code,
            notes=(*notes, "held for template implementation; never replaced with English"),
            **context,
        )
    if seller_preference is not None:
        notes.append("unverified seller language preference ignored")

    eligible = [f for f in ad_texts if f.eligible]
    ignored = len(ad_texts) - len(eligible)
    if ignored:
        notes.append(f"{ignored} non-seller-written, translated or empty fragment(s) ignored")
    if not eligible:
        return LanguageDecision(
            language=None,
            status=LanguageStatus.LANGUAGE_UNRESOLVED,
            confidence=Decimal(0),
            evidence_excerpt=None,
            reason=LanguageReason.NO_SELLER_WRITTEN_TEXT,
            basis="none",
            notes=tuple(notes),
            **context,
        )

    # Description first so its evidence leads the excerpt; deterministic order otherwise.
    order = {"description": 0, "seller_note": 1, "title": 2}
    eligible.sort(key=lambda f: (order[f.field], f.text))
    combined = detect_text_language("\n".join(f.text for f in eligible))
    per_fragment = [detect_text_language(f.text) for f in eligible]
    fragment_languages = {d.language for d in per_fragment if d.language is not None}

    if combined.reason == DetectionReason.NON_LATIN_SCRIPT:
        return LanguageDecision(
            language=None,
            status=LanguageStatus.UNSUPPORTED_LANGUAGE,
            confidence=Decimal(0),
            evidence_excerpt=None,
            reason=LanguageReason.AD_TEXT_NON_LATIN_SCRIPT,
            basis="seller_ad_text",
            notes=(*notes, "held for template implementation; never replaced with English"),
            **context,
        )
    if combined.language is None:
        return LanguageDecision(
            language=None,
            status=LanguageStatus.LANGUAGE_UNRESOLVED,
            confidence=combined.confidence,
            evidence_excerpt=None,
            reason=_DETECTION_TO_REASON[combined.reason],
            basis="none",
            notes=tuple(notes),
            **context,
        )
    if fragment_languages - {combined.language}:
        return LanguageDecision(
            language=None,
            status=LanguageStatus.LANGUAGE_UNRESOLVED,
            confidence=Decimal(0),
            evidence_excerpt=None,
            reason=LanguageReason.FRAGMENTS_DISAGREE,
            basis="none",
            detected_language=None,
            notes=(*notes, "fragments detected as: " + ", ".join(sorted(fragment_languages))),
            **context,
        )
    if combined.language not in SUPPORTED_LANGUAGES:
        return LanguageDecision(
            language=None,
            status=LanguageStatus.UNSUPPORTED_LANGUAGE,
            confidence=combined.confidence,
            evidence_excerpt=combined.evidence_excerpt,
            reason=LanguageReason.AD_TEXT_UNSUPPORTED_LANGUAGE,
            basis="seller_ad_text",
            detected_language=combined.language,
            notes=(*notes, "held for template implementation; never replaced with English"),
            **context,
        )
    return LanguageDecision(
        language=MessageLanguage(combined.language),
        status=LanguageStatus.RESOLVED,
        confidence=combined.confidence,
        evidence_excerpt=combined.evidence_excerpt,
        reason=LanguageReason.SELLER_AD_TEXT,
        basis="seller_ad_text",
        detected_language=combined.language,
        notes=tuple(notes),
        **context,
    )
