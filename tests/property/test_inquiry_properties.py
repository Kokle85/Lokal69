"""Property tests for the bounded seller-inquiry domain (spec 37.2-37.5).

All generated listings, sellers, addresses and texts are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Co2Cycle,
    Confidence,
    Drive,
    EligibilityState,
    EmailProviderKind,
    ExtractionMethod,
    Fuel,
    Gearbox,
    InquiryReadiness,
    InquiryState,
    MessageLanguage,
    ProfileKey,
    SellerType,
)
from suv_deals.domain.filters import ScreeningResult
from suv_deals.domain.inquiries import (
    ALLOWED_TRANSITIONS,
    WINDOW_15D,
    WINDOW_24H,
    ComparableEvidence,
    CostEvidence,
    DisqualifierFacts,
    DuplicateDecision,
    InquiryReadinessInputs,
    QuotaDebit,
    RateCapPolicy,
    SenderStatus,
    SourceObservationFacts,
    VehicleIdentification,
    build_inquiry_identity,
    evaluate_inquiry_readiness,
    evaluate_rate_caps,
    load_seller_inquiry_authorization,
)
from suv_deals.domain.language import (
    AdTextFragment,
    SellerLanguagePreference,
    detect_text_language,
    resolve_inquiry_language,
)
from suv_deals.domain.listings import Co2Info, Documentation
from suv_deals.domain.money import Money
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.seller_contacts import (
    AddressError,
    ExtractionLocation,
    RecipientEvidence,
    RecipientEvidenceKind,
    SellerAlias,
    SellerIdentity,
    canonicalize_address,
    verify_recipient,
)
from suv_deals.domain.seller_templates import (
    PERMITTED_QUESTIONS,
    TEMPLATE_BY_LANGUAGE,
    TemplateRenderError,
    build_vehicle_label,
    render,
    render_preview_mk,
    validate_scope,
)

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
WS = UUID(int=1)
LISTING = UUID(int=11)
ENTITY = UUID(int=21)
URL = "https://www.example-marketplace.invalid/listing/SYNTH-9"
AUTH = load_seller_inquiry_authorization()

TEXTS = {
    "de": "Fahrzeug ist unfallfrei, TÜV neu, Scheckheft gepflegt. Nichtraucherfahrzeug mit Anhängerkupplung.",
    "it": "Vendo SUV in ottime condizioni, tagliandi regolari, gomme nuove, unico proprietario.",
    "fr": "Véhicule en très bon état, première main, carnet d'entretien complet, contrôle technique OK.",
    "en": "Full service history, one owner from new, very clean condition. The car has been well maintained.",
    "nl": "Auto is in goede staat, onderhoud bijgehouden, nieuwe banden, APK geldig. Rijklaar.",
    "short": "BMW X5 3.0d",
    "mixed": (
        "Fahrzeug ist unfallfrei und in sehr gutem Zustand. "
        "The vehicle is in very good condition with history."
    ),
    "cyrillic": "Продавам автомобил во одлична состојба, прв сопственик.",
}


def _fragment(key: str, seller_written: bool, translated: bool) -> AdTextFragment:
    return AdTextFragment(
        field="description",
        text=TEXTS[key],
        seller_written=seller_written,
        machine_translated=translated,
        provenance=FieldProvenance(method=ExtractionMethod.CSS, confidence=Confidence.HIGH, observed_at=NOW),
    )


fragments = st.lists(
    st.builds(_fragment, st.sampled_from(sorted(TEXTS)), st.booleans(), st.booleans()), max_size=3
)
countries = st.sampled_from([None, "DE", "IT", "CH", "FR", "AT", "GB", "NL", "MK"])
navigation = st.sampled_from([None, "de", "it", "fr", "en", "nl"])
preferences = st.one_of(
    st.none(),
    st.builds(
        lambda code, verified: SellerLanguagePreference(
            language=code,
            verified=verified,
            evidence_kind="seller_stated_language",
            evidence_excerpt="seller statement" if verified else None,
            observed_at=NOW,
        ),
        st.sampled_from(["de", "it", "fr", "en", "nl", "pl"]),
        st.booleans(),
    ),
)


# ---------------------------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(preferences, fragments, navigation, countries)
def test_navigation_language_and_country_never_decide(
    preference: SellerLanguagePreference | None,
    frags: list[AdTextFragment],
    nav: str | None,
    country: str | None,
) -> None:
    with_context = resolve_inquiry_language(preference, frags, nav, country)
    without = resolve_inquiry_language(preference, frags, None, None)
    keys = ("language", "status", "reason", "basis", "detected_language", "confidence", "evidence_excerpt")
    assert {k: getattr(with_context, k) for k in keys} == {k: getattr(without, k) for k in keys}


@settings(max_examples=200, deadline=None)
@given(preferences, fragments, navigation, countries)
def test_english_only_with_positive_evidence(
    preference: SellerLanguagePreference | None,
    frags: list[AdTextFragment],
    nav: str | None,
    country: str | None,
) -> None:
    decision = resolve_inquiry_language(preference, frags, nav, country)
    if decision.language == MessageLanguage.EN:
        preferred = preference is not None and preference.verified and preference.language == "en"
        detected = any(f.eligible and detect_text_language(f.text).language == "en" for f in frags)
        assert preferred or detected
    if decision.language is not None:
        assert decision.status.value == "resolved"
    else:
        assert decision.status.value in {"language_unresolved", "unsupported_language"}


# ---------------------------------------------------------------------------------------------
# Rate caps
# ---------------------------------------------------------------------------------------------

offsets = st.lists(st.integers(min_value=-20 * 86400, max_value=2 * 3600), max_size=12)


def _brute_count(times: list[datetime], now: datetime, window: timedelta) -> int:
    return sum(1 for t in times if now - window < t)


@settings(max_examples=300, deadline=None)
@given(offsets, st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=5))
def test_rate_caps_are_exact_rolling_ceilings(seconds: list[int], cap24: int, cap15: int) -> None:
    policy = RateCapPolicy(max_per_24h=cap24, max_per_15d=cap15)
    debits = [
        QuotaDebit(inquiry_id=UUID(int=i + 1), at=NOW + timedelta(seconds=s)) for i, s in enumerate(seconds)
    ]
    times = [d.at for d in debits]
    decision = evaluate_rate_caps(debits, now=NOW, policy=policy)
    c24, c15 = _brute_count(times, NOW, WINDOW_24H), _brute_count(times, NOW, WINDOW_15D)
    assert (decision.count_24h, decision.count_15d) == (c24, c15)
    assert decision.allowed == (cap24 > 0 and cap15 > 0 and c24 < cap24 and c15 < cap15)
    if decision.allowed:
        assert decision.next_allowed_at is None
        return
    if cap24 == 0 or cap15 == 0:
        assert decision.next_allowed_at is None
        return
    nxt = decision.next_allowed_at
    assert nxt is not None and nxt > NOW
    assert evaluate_rate_caps(debits, now=nxt, policy=policy).allowed
    assert not evaluate_rate_caps(debits, now=nxt - timedelta(microseconds=1), policy=policy).allowed


# ---------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------

alias_refs = st.lists(st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True), min_size=1, max_size=4, unique=True)


@settings(max_examples=100, deadline=None)
@given(alias_refs, st.permutations(list(range(4))), st.integers(min_value=0, max_value=50))
def test_identity_ignores_alias_order_relay_address_and_site(
    refs: list[str], order: list[int], n: int
) -> None:
    aliases = [
        SellerAlias(
            alias_kind="marketplace_seller_id",
            source_key=f"site_{i}",
            reference=ref,
            evidence_kind="listing_seller_block",
            observed_at=NOW,
        )
        for i, ref in enumerate(refs)
    ]
    shuffled = [aliases[i] for i in order if i < len(aliases)]
    a = SellerIdentity(seller_type=SellerType.DEALER, aliases=tuple(aliases))
    b = SellerIdentity(seller_type=SellerType.DEALER, aliases=tuple(shuffled))
    cluster = UUID(int=99)
    ia = build_inquiry_identity(
        WS, vehicle_cluster_id=cluster, listing_incarnation_id=UUID(int=500 + n), seller=a
    )
    ib = build_inquiry_identity(
        WS, vehicle_cluster_id=cluster, listing_incarnation_id=UUID(int=900 + n), seller=b
    )
    assert ia.key() == ib.key()
    linked = [SellerIdentity(seller_entity_id=ENTITY, aliases=(x,)) for x in aliases]
    keys = {
        build_inquiry_identity(
            WS, vehicle_cluster_id=cluster, listing_incarnation_id=UUID(int=7), seller=s
        ).key()
        for s in linked
    }
    assert len(keys) == 1


# ---------------------------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=10), max_size=40))
def test_no_second_send_after_acceptance(choices: list[int]) -> None:
    state = InquiryState.CANDIDATE
    accepted_seen = False
    accepted_count = 0
    for choice in choices:
        options = sorted(ALLOWED_TRANSITIONS[state], key=lambda s: s.value)
        if not options:
            break
        nxt = options[choice % len(options)]
        if accepted_seen:
            assert nxt not in {InquiryState.QUEUED, InquiryState.SENDING, InquiryState.RESERVED}
        if nxt == InquiryState.ACCEPTED:
            accepted_count += 1
            accepted_seen = True
        state = nxt
    assert accepted_count <= 1


# ---------------------------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------------------------

SELLER = SellerIdentity(
    seller_entity_id=ENTITY,
    seller_type=SellerType.DEALER,
    aliases=(
        SellerAlias(
            alias_kind="marketplace_seller_id",
            source_key="fixture_market_de",
            reference="dealer-1",
            evidence_kind="listing_seller_block",
            observed_at=NOW,
        ),
    ),
)
IDENTITY = build_inquiry_identity(
    WS, vehicle_cluster_id=None, listing_incarnation_id=UUID(int=12), seller=SELLER
)
RECIPIENT = verify_recipient(
    RecipientEvidence(
        kind=RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
        address="verkauf@autohaus-example.invalid",
        listing_id=LISTING,
        listing_revision_number=1,
        source_key="fixture_market_de",
        listing_reference="SYNTH-9",
        listing_url=URL,
        evidence_url=URL,
        extraction_location=ExtractionLocation.LISTING_CONTACT_BLOCK,
        extraction_excerpt="verkauf@autohaus-example.invalid",
        seller=SELLER,
        observed_at=NOW - timedelta(hours=2),
        verified_at=NOW - timedelta(hours=1),
    ),
    now=NOW,
)
LANGUAGE = resolve_inquiry_language(None, [_fragment("de", True, False)])

BASE: dict[str, Any] = {
    "as_of": NOW,
    "listing_id": LISTING,
    "authorization": AUTH,
    "identity": IDENTITY,
    "screening": ScreeningResult(
        state=EligibilityState.ELIGIBLE_PRIMARY,
        profile=ProfileKey.PRIMARY,
        queue_label="primary",
        eur_amount=Decimal("2750"),
        payable_amount=Money.of("2750", "EUR"),
        fx_rate_used=None,
        reasons=(),
        missing_facts=(),
    ),
    "vehicle": VehicleIdentification(
        make="Volkswagen",
        model="Tiguan",
        generation="5N",
        is_suv=True,
        confidence=Confidence.HIGH,
        matched_via="fields",
        fuel=Fuel.DIESEL,
        gearbox=Gearbox.MANUAL,
        drive=Drive.AWD,
    ),
    "source": SourceObservationFacts(
        source_key="fixture_market_de",
        source_enabled=True,
        last_detail_success_at=NOW - timedelta(hours=1),
        availability=Availability.AVAILABLE,
    ),
    "comparables": ComparableEvidence(
        status="adequate", asking_count=3, matching_rationale="exact 5N matches"
    ),
    "costs": CostEvidence(
        best_case_known_costs=Money.of("2750", "EUR"), best_case_proceeds=Money.of("9500", "EUR")
    ),
    "duplicate": DuplicateDecision(outcome="clear"),
    "recipient": RECIPIENT,
    "language": LANGUAGE,
    "sender": SenderStatus(
        mode="automatic",
        provider=EmailProviderKind.GMAIL_API,
        binding_id=UUID(int=31),
        binding_version=1,
        account_id="synthetic-account",
        from_address="vasko@example.invalid",
        display_name="Vasko K.",
        alias_verified=True,
        verified_at=NOW,
        health_ok=True,
    ),
}

#: name -> (overrides, readiness it forces on its own)
BLOCKERS: dict[str, tuple[dict[str, Any], InquiryReadiness]] = {
    "rejected": (
        {"screening": BASE["screening"].model_copy(update={"state": EligibilityState.REJECTED})},
        InquiryReadiness.NOT_ELIGIBLE,
    ),
    "opt_out": ({"disqualifiers": DisqualifierFacts(seller_opted_out=True)}, InquiryReadiness.NOT_ELIGIBLE),
    "prior": ({"duplicate": DuplicateDecision(outcome="prior_inquiry")}, InquiryReadiness.NOT_ELIGIBLE),
    "costs": (
        {
            "costs": CostEvidence(
                best_case_known_costs=Money.of("9600", "EUR"), best_case_proceeds=Money.of("9500", "EUR")
            )
        },
        InquiryReadiness.NOT_ELIGIBLE,
    ),
    "no_recipient": ({"recipient": None}, InquiryReadiness.NEEDS_FACTS),
    "no_language": ({"language": None}, InquiryReadiness.NEEDS_FACTS),
    "no_comparables": ({"comparables": None}, InquiryReadiness.NEEDS_FACTS),
    "fuel": (
        {"vehicle": BASE["vehicle"].model_copy(update={"fuel": Fuel.UNKNOWN})},
        InquiryReadiness.NEEDS_FACTS,
    ),
    "sender": (
        {"sender": BASE["sender"].model_copy(update={"mode": "disabled_until_sender_ready"})},
        InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
    ),
    "duplicate": (
        {"duplicate": DuplicateDecision(outcome="possible_duplicate")},
        InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
    ),
}
_ORDER = [
    InquiryReadiness.NOT_ELIGIBLE,
    InquiryReadiness.NEEDS_FACTS,
    InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
]


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sets(st.sampled_from(sorted(BLOCKERS))),
    st.sampled_from(list(ClaimStatus)),
    st.sampled_from(list(ClaimStatus)),
    st.one_of(st.none(), st.just("seller states first registration in DE")),
    st.one_of(
        st.none(), st.builds(Co2Info, g_per_km=st.just(Decimal("199")), cycle=st.sampled_from(list(Co2Cycle)))
    ),
)
def test_readiness_is_decided_by_blockers_never_by_documents(
    chosen: set[str], coc: ClaimStatus, registration: ClaimStatus, origin: str | None, co2: Co2Info | None
) -> None:
    data = dict(BASE)
    data["documentation"] = Documentation(
        coc_available=coc, registration_documents=registration, origin_evidence=origin
    )
    data["co2"] = co2
    for name in sorted(chosen):
        data.update(BLOCKERS[name][0])
    decision = evaluate_inquiry_readiness(InquiryReadinessInputs(**data))
    if not chosen:
        assert decision.readiness == InquiryReadiness.INQUIRY_READY
        assert decision.can_reserve_now
    else:
        expected = min((BLOCKERS[name][1] for name in chosen), key=_ORDER.index)
        assert decision.readiness == expected
        assert not decision.can_reserve_now
    assert {"availability", "lowest_final_price"} <= set(decision.open_questions)
    assert all(
        "APPROV" not in r.code or r.severity.value == "info" for r in decision.reasons
    )  # never an approval wait


# ---------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------

label_part = st.from_regex(r"[A-Z][a-z0-9]{0,8}( [A-Z0-9][a-z0-9]{0,5})?", fullmatch=True)
references = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,20}", fullmatch=True)
names = st.from_regex(r"[A-Z][a-z]{1,10}( [A-Z][a-z]{0,10}\.?)?", fullmatch=True)


@settings(max_examples=150, deadline=None)
@given(label_part, label_part, references, names, st.sampled_from(sorted(TEMPLATE_BY_LANGUAGE.values())))
def test_rendering_either_refuses_or_stays_in_scope(
    make: str, model: str, ref: str, name: str, template_id: str
) -> None:
    try:
        label = build_vehicle_label(make, model)
        message = render(template_id, label, ref, URL, name, verified_listing_url=URL)
    except TemplateRenderError:
        return  # fail closed (e.g. a generated word that is on the forbidden lexicon)
    result = validate_scope(message)
    assert result.ok and result.questions == PERMITTED_QUESTIONS
    assert message.body.count(URL) == 1
    preview = render_preview_mk(message)
    assert preview.placeholders == message.placeholders and validate_scope(preview).ok


@settings(max_examples=150, deadline=None)
@given(
    st.text(max_size=10),
    st.sampled_from(["\r", "\n", "\r\n", "\u2028", "\u0085"]),
    st.text(max_size=10),
    st.sampled_from(["reference", "name"]),
)
def test_line_breaks_in_placeholders_are_always_rejected(
    prefix: str, brk: str, suffix: str, where: str
) -> None:
    value = prefix + brk + suffix
    label = build_vehicle_label("Volkswagen", "Tiguan")
    ref = value if where == "reference" else "SYNTH-9"
    name = value if where == "name" else "Vasko K."
    try:
        render("seller_initial_en_v1", label, ref, URL, name, verified_listing_url=URL)
    except TemplateRenderError as exc:
        assert exc.problems
    else:  # pragma: no cover - would be a header-injection hole
        raise AssertionError("a line break was accepted in a placeholder")


# ---------------------------------------------------------------------------------------------
# Address canonicalisation
# ---------------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    st.from_regex(r"[A-Za-z0-9_%+'-]{1,10}(\.[A-Za-z0-9_%+'-]{1,6}){0,2}", fullmatch=True),
    st.from_regex(r"[A-Za-z0-9]{1,10}(-[A-Za-z0-9]{1,5})?\.[A-Za-z]{2,6}", fullmatch=True),
)
def test_canonicalisation_is_idempotent_and_keeps_the_local_part(local: str, domain: str) -> None:
    try:
        first = canonicalize_address(f"{local}@{domain}")
    except AddressError:
        return
    assert first.local_part == local
    assert first.domain == domain.lower()
    assert canonicalize_address(first.canonical) == first
