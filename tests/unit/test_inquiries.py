"""Unit tests for domain.inquiries (spec 37.1, 37.2, 37.5 and the 37.10 delta tests).

Every listing, seller, address, URL, amount and identifier is SYNTHETIC test data. Nothing here
sends e-mail or touches a network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from suv_deals.domain.comparables import (
    ComparableTarget,
    MarketObservation,
    proceeds_from_comparables,
    select_comparables,
)
from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    CostLine,
    ProceedsEstimate,
    PurchaseInput,
    compute_scenarios,
)
from suv_deals.domain.enums import (
    Availability,
    BodyType,
    ClaimStatus,
    Confidence,
    CostCategory,
    CostLineStatus,
    Drive,
    EligibilityState,
    EmailProviderKind,
    EvidenceKind,
    ExtractionMethod,
    Fuel,
    Gearbox,
    InquiryReadiness,
    InquiryState,
    JobState,
    MessageLanguage,
    OdometerClaim,
    PriceBasis,
    PriceType,
    ProfileKey,
    SellerType,
    SuppressionReason,
    Tristate,
)
from suv_deals.domain.filters import ScreeningResult, screen
from suv_deals.domain.inquiries import (
    ALLOWED_TRANSITIONS,
    BINDING_IMMUTABLE_STATES,
    DEFAULT_AUTHORIZATION_PATH,
    EMAIL_DELIVERY_UNCERTAIN,
    MAX_READINESS_AGE,
    MAX_SEND_ATTEMPTS,
    POSSIBLY_TRANSMITTED_STATES,
    PRE_RESERVATION_STATES,
    SELLER_COOLDOWN,
    TERMINAL_STATES,
    WINDOW_15D,
    WINDOW_24H,
    ComparableEvidence,
    CostEvidence,
    DispatchFacts,
    DisqualifierFacts,
    DuplicateDecision,
    ExistingInquiry,
    InquiryBinding,
    InquiryIdentity,
    InquiryReadinessDecision,
    InquiryReadinessInputs,
    ListingFactsSnapshot,
    PreflightDecision,
    PreflightOutcome,
    QuotaDebit,
    RateCapPolicy,
    ReadinessCode,
    ReadinessSeverity,
    ReconciliationEvidence,
    RelatedListingLink,
    SellerInquiryAuthorization,
    SendAttemptEvidence,
    SendAttemptOutcome,
    SenderStatus,
    SourceObservationFacts,
    SuppressionRecord,
    SuppressionTargets,
    TransitionContext,
    VehicleIdentification,
    VehicleIdentityRef,
    apply_binding,
    bind_inquiry,
    build_inquiry_identity,
    can_transition,
    canonical_vehicle_identity,
    dispatch_preflight,
    evaluate_duplicate_contact,
    evaluate_inquiry_readiness,
    evaluate_rate_caps,
    evaluate_seller_cooldown,
    load_seller_inquiry_authorization,
    matching_suppressions,
    on_sending_interrupted,
    reconcile_identity_merge,
    reconcile_uncertain,
    releases_quota,
    require_transition,
    requires_message_approval,
    seller_contact_times,
    should_retry,
)
from suv_deals.domain.language import AdTextFragment, resolve_inquiry_language
from suv_deals.domain.listings import (
    Co2Info,
    Documentation,
    LocationInfo,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import ContributionThreshold, load_business_config
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.seller_contacts import (
    RECIPIENT_EVIDENCE_MAX_AGE,
    ContactRecheck,
    ExtractionLocation,
    RecipientEvidence,
    RecipientEvidenceKind,
    SellerAlias,
    SellerIdentity,
    verify_recipient,
)
from suv_deals.domain.seller_templates import (
    MessageEnvelope,
    RenderedMessage,
    build_vehicle_label,
    message_body_hash,
    render,
    render_preview_mk,
)
from suv_deals.domain.taxonomy import default_taxonomy
from suv_deals.errors import IdempotencyConflict, ValidationFailed
from suv_deals.settings import Settings

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
WS = UUID(int=1)
LISTING = UUID(int=11)
INCARNATION = UUID(int=12)
REVISION = UUID(int=13)
CLUSTER = UUID(int=14)
ENTITY = UUID(int=21)
SENDER_BINDING = UUID(int=31)
INQUIRY = UUID(int=41)
URL = "https://www.example-marketplace.invalid/listing/SYNTH-1"
REF = "SYNTH-1"
SELLER_ADDRESS = "verkauf@autohaus-example.invalid"
REPLY_TO = "vasko@example.invalid"
DE_TEXT = (
    "Verkaufe unseren gepflegten Geländewagen. Fahrzeug ist unfallfrei, TÜV neu, Scheckheft gepflegt. "
    "Nichtraucherfahrzeug mit Anhängerkupplung und Sitzheizung."
)
FR_TEXT = "Véhicule en très bon état, première main, carnet d'entretien complet, contrôle technique OK."

AUTH = load_seller_inquiry_authorization()


# ---------------------------------------------------------------------------------------------
# Synthetic world: one VW Tiguan screened, compared and costed by the real domain code
# ---------------------------------------------------------------------------------------------


def _listing(**overrides: Any) -> NormalizedListing:
    data: dict[str, Any] = {
        "source_key": "fixture_dealer_de",
        "source_listing_id": REF,
        "canonical_url": URL,
        "observed_at": NOW - timedelta(hours=1),
        "location": LocationInfo(country="DE"),
        "availability": Availability.AVAILABLE,
        "seller_type": SellerType.DEALER,
        "vehicle": VehicleSpec(
            make="Volkswagen",
            model="Tiguan",
            body_type=BodyType.SUV,
            mileage_km=Decimal("187500"),
            mileage_claim=OdometerClaim.SELLER_REPORTED,
            first_registration=PartialDate(value="2011-05", precision="month"),
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            power_kw=103,
            engine_displacement_cm3=1968,
        ),
        "price": PriceInfo(
            amount_minor=275000,
            currency="EUR",
            basis=PriceBasis.GROSS,
            type=PriceType.FULL_VEHICLE_ASKING,
            required_seller_fees_known=Tristate.NO,
        ),
        "parser_version": "fixture@1.0.0",
    }
    data.update(overrides)
    return NormalizedListing(**data)


LISTING_OBJ = _listing()
CONFIG = load_business_config(REPO / "config")
SCREENING = screen(LISTING_OBJ, CONFIG, [], NOW.date(), default_taxonomy())
TARGET = ComparableTarget.from_listing(LISTING_OBJ).model_copy(update={"generation": "5N"})


def _observation(n: int, amount: str) -> MarketObservation:
    return MarketObservation(
        id=UUID(int=500 + n),
        source_key="fixture_mk",
        observed_at=NOW - timedelta(days=3),
        evidence_kind=EvidenceKind.ASKING_PRICE,
        amount=Money.of(amount, "EUR"),
        vehicle=VehicleSpec(
            make="Volkswagen",
            model="Tiguan",
            generation="5N",
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            power_kw=103,
            engine_displacement_cm3=1968,
            first_registration=PartialDate(value="2011", precision="year"),
            mileage_km=Decimal("180000"),
        ),
    )


COMPARABLES = select_comparables(
    TARGET, [_observation(1, "8500"), _observation(2, "9000"), _observation(3, "9500")], CONFIG, NOW
)


def _scenarios(purchase: str = "2750", transport: str | None = None) -> Any:
    proceeds = ProceedsEstimate(**proceeds_from_comparables(COMPARABLES, None).as_proceeds_estimate_kwargs())
    lines = []
    for category in sorted(REQUIRED_CATEGORIES):
        if category == CostCategory.TRANSPORT and transport is not None:
            lines.append(
                CostLine(
                    category=category,
                    label="SYNTHETIC transport",
                    status=CostLineStatus.ESTIMATED,
                    currency="EUR",
                    base=Money.of(transport, "EUR"),
                )
            )
        else:
            lines.append(
                CostLine(
                    category=category,
                    label=f"SYNTHETIC {category.value}",
                    status=CostLineStatus.UNKNOWN,
                    currency="EUR",
                )
            )
    return compute_scenarios(
        PurchaseInput(status=CostLineStatus.ESTIMATED, amount=Money.of(purchase, "EUR")),
        lines,
        proceeds,
        [],
        ContributionThreshold(),
        as_of=NOW,
    )


SCENARIOS = _scenarios()


def _alias(reference: str, source_key: str) -> SellerAlias:
    return SellerAlias(
        alias_kind="marketplace_seller_id",
        source_key=source_key,
        reference=reference,
        evidence_kind="listing_seller_block",
        observed_at=NOW,
    )


SELLER = SellerIdentity(
    seller_entity_id=ENTITY, seller_type=SellerType.DEALER, aliases=(_alias("dealer-1", "fixture_dealer_de"),)
)
IDENTITY = build_inquiry_identity(
    WS, vehicle_cluster_id=None, listing_incarnation_id=INCARNATION, seller=SELLER
)


def _recipient_evidence(**overrides: Any) -> RecipientEvidence:
    data: dict[str, Any] = {
        "kind": RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
        "address": SELLER_ADDRESS,
        "listing_id": LISTING,
        "listing_incarnation_id": INCARNATION,
        "listing_revision_id": REVISION,
        "listing_revision_number": 1,
        "source_key": "fixture_dealer_de",
        "listing_reference": REF,
        "listing_url": URL,
        "evidence_url": URL,
        "extraction_location": ExtractionLocation.LISTING_CONTACT_BLOCK,
        "extraction_excerpt": f"E-Mail: {SELLER_ADDRESS}",
        "distinct_addresses_on_page": 1,
        "seller": SELLER,
        "observed_at": NOW - timedelta(hours=2),
        "verified_at": NOW - timedelta(hours=1),
    }
    data.update(overrides)
    return RecipientEvidence(**data)


RECIPIENT = verify_recipient(_recipient_evidence(), now=NOW)


def _fragment(text: str) -> AdTextFragment:
    return AdTextFragment(
        field="description",
        text=text,
        seller_written=True,
        provenance=FieldProvenance(method=ExtractionMethod.CSS, confidence=Confidence.HIGH, observed_at=NOW),
    )


LANGUAGE_DE = resolve_inquiry_language(None, [_fragment(DE_TEXT)], "de", "DE")

SNAPSHOT = ListingFactsSnapshot(
    listing_id=LISTING,
    listing_incarnation_id=INCARNATION,
    revision_id=REVISION,
    revision_number=1,
    semantic_hash=LISTING_OBJ.semantic_hash(),
    price_amount_minor=275000,
    price_currency="EUR",
    availability=Availability.AVAILABLE,
)


def sender(**overrides: Any) -> SenderStatus:
    data: dict[str, Any] = {
        "mode": "automatic",
        "provider": EmailProviderKind.GMAIL_API,
        "binding_id": SENDER_BINDING,
        "binding_version": 1,
        "account_id": "synthetic-account-1",
        "from_address": "vasko@example.invalid",
        "display_name": "Vasko K.",
        "reply_to_address": "vasko@example.invalid",
        "alias_verified": True,
        "verified_at": NOW - timedelta(days=1),
        "health_ok": True,
    }
    data.update(overrides)
    return SenderStatus(**data)


def inputs(**overrides: Any) -> InquiryReadinessInputs:
    data: dict[str, Any] = {
        "as_of": NOW,
        "listing_id": LISTING,
        "listing_facts": SNAPSHOT,
        "authorization": AUTH,
        "identity": IDENTITY,
        "screening": SCREENING,
        "vehicle": VehicleIdentification.from_screening(SCREENING, LISTING_OBJ),
        "source": SourceObservationFacts(
            source_key="fixture_dealer_de",
            source_enabled=True,
            last_detail_success_at=NOW - timedelta(hours=1),
            availability=Availability.AVAILABLE,
        ),
        "comparables": ComparableEvidence.from_comparable_set(COMPARABLES),
        "costs": CostEvidence.from_scenario_set(SCENARIOS),
        "documentation": LISTING_OBJ.documentation,
        "co2": LISTING_OBJ.co2,
        "duplicate": DuplicateDecision(outcome="clear"),
        "recipient": RECIPIENT,
        "language": LANGUAGE_DE,
        "sender": sender(),
    }
    data.update(overrides)
    return InquiryReadinessInputs(**data)


def readiness(**overrides: Any) -> InquiryReadinessDecision:
    return evaluate_inquiry_readiness(inputs(**overrides))


def severity_of(decision: InquiryReadinessDecision, code: str) -> ReadinessSeverity:
    return next(r.severity for r in decision.reasons if r.code == code)


# ---------------------------------------------------------------------------------------------
# Standing authorization record
# ---------------------------------------------------------------------------------------------


def test_authorization_record_is_the_bounded_standing_scope() -> None:
    assert DEFAULT_AUTHORIZATION_PATH == REPO / "config" / "seller_inquiry_authorization.yaml"
    assert AUTH.owner == "Vasko"
    assert AUTH.effective_date == date(2026, 10, 6)
    assert AUTH.record_type == "application_audit_record"
    assert AUTH.approval_mode == "no_message_approval"
    assert AUTH.purpose == "initial_availability_documents_price"
    assert [q.value for q in AUTH.questions] == ["availability", "vehicle_documents", "lowest_final_price"]
    assert AUTH.recipient_class == "verified_seller_of_exact_listing"
    assert AUTH.max_inquiries_per_vehicle_seller_pair == 1
    assert AUTH.attachments_allowed is False and AUTH.cc_bcc_allowed is False
    assert AUTH.follow_ups_allowed is False and AUTH.additional_recipients_allowed is False
    assert AUTH.profiles_in_scope == (ProfileKey.PRIMARY,)
    assert set(AUTH.languages) == set(MessageLanguage)
    assert AUTH.revocation.revoked is False
    assert AUTH.problems_at(NOW) == ()
    assert AUTH.problems_at(datetime(2026, 10, 5, 23, 59, tzinfo=UTC)) == ("AUTHORIZATION_NOT_EFFECTIVE",)
    assert (
        len(AUTH.fingerprint()) == 64
        and AUTH.fingerprint() == load_seller_inquiry_authorization().fingerprint()
    )
    assert "telephone" in AUTH.excluded_data_categories
    assert "purchase" in AUTH.not_authorized and "follow_up" in AUTH.not_authorized


def _raw_authorization() -> dict[str, Any]:
    raw = yaml.safe_load(DEFAULT_AUTHORIZATION_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    "change",
    [
        {"approval_mode": "per_message_approval"},
        {"purpose": "negotiate"},
        {"questions": ["availability", "vehicle_documents"]},
        {"questions": ["availability", "vehicle_documents", "lowest_final_price", "viewing_appointment"]},
        {"max_inquiries_per_vehicle_seller_pair": 2},
        {"attachments_allowed": True},
        {"cc_bcc_allowed": True},
        {"follow_ups_allowed": True},
        {"english_requires_positive_evidence": False},
        {"recipient_class": "any_contact"},
        {"scope_version": 2},
        {"not_authorized": ["follow_up"]},
        {"excluded_data_categories": ["telephone"]},
        {
            "allowed_outgoing_data_categories": [
                "verified_sender_display_name",
                "verified_sender_email",
                "vehicle_make_model",
                "listing_reference",
                "listing_url",
                "three_permitted_questions",
                "telephone",
            ]
        },
        {"profiles_in_scope": []},
        {"profiles_in_scope": ["primary", "primary"]},
        {"languages": ["de", "de"]},
        {"languages": ["nl"]},
        {"revocation": {"revoked": True, "revoked_at": None, "revoked_by": None, "reason": None}},
        {
            "revocation": {
                "revoked": False,
                "revoked_at": "2026-10-07T00:00:00Z",
                "revoked_by": "x",
                "reason": "y",
            }
        },
        {"recorded_at": "2026-10-01"},
        {"unexpected": True},
    ],
)
def test_authorization_scope_cannot_be_widened(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SellerInquiryAuthorization.model_validate({**_raw_authorization(), **change})


def test_authorization_revocation(tmp_path: Path) -> None:
    revoked_at = NOW + timedelta(hours=1)
    raw = {
        **_raw_authorization(),
        "version": 2,
        "revocation": {
            "revoked": True,
            "revoked_at": revoked_at.isoformat(),
            "revoked_by": "Vasko",
            "reason": "pause",
        },
    }
    path = tmp_path / "auth.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    auth = load_seller_inquiry_authorization(path)
    assert auth.problems_at(NOW) == ()
    assert auth.problems_at(revoked_at) == ("AUTHORIZATION_REVOKED",)
    decision = readiness(authorization=auth, as_of=revoked_at)
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE
    assert "AUTHORIZATION_REVOKED" in decision.codes()


def test_authorization_loader_errors(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        load_seller_inquiry_authorization(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_seller_inquiry_authorization(bad)
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: [unclosed", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_seller_inquiry_authorization(broken)
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        yaml.safe_dump({**_raw_authorization(), "approval_mode": "per_message"}), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed) as exc:
        load_seller_inquiry_authorization(invalid)
    assert exc.value.details["problems"]


def test_message_approval_comes_only_from_the_owner_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL", raising=False)
    assert requires_message_approval(Settings(_env_file=None)) is False  # type: ignore[call-arg]
    explicit = Settings(_env_file=None, seller_inquiry_require_message_approval=True)  # type: ignore[call-arg]
    assert requires_message_approval(explicit) is True


# ---------------------------------------------------------------------------------------------
# Readiness (spec 37.2)
# ---------------------------------------------------------------------------------------------


def test_synthetic_world_is_consistent() -> None:
    assert SCREENING.state == EligibilityState.ELIGIBLE_PRIMARY
    assert SCREENING.taxonomy_match is not None and SCREENING.taxonomy_match.generation == "5N"
    assert COMPARABLES.status == "adequate"
    assert RECIPIENT.verified
    assert LANGUAGE_DE.language == MessageLanguage.DE
    assert LISTING_OBJ.documentation.coc_available == ClaimStatus.UNKNOWN


def test_candidate_lacking_coc_and_documents_is_inquiry_ready() -> None:
    decision = readiness()
    assert decision.readiness == InquiryReadiness.INQUIRY_READY, [r.code for r in decision.reasons]
    assert decision.can_reserve_now is True
    assert decision.next_attempt_at is None
    assert {
        "availability",
        "lowest_final_price",
        "coc",
        "registration_documents",
        "origin_evidence",
        "co2_and_cycle",
    } <= set(decision.open_questions)
    assert decision.economics_incomplete is True
    assert {
        "ECONOMICS_INCOMPLETE",
        "UNKNOWN_COSTS_LISTED",
        "TAX_RULE_NOT_APPROVED",
        "PROPOSED_THRESHOLD_NOT_APPLIED",
    } <= decision.codes()
    for code in (
        "ECONOMICS_INCOMPLETE",
        "UNKNOWN_COSTS_LISTED",
        "TAX_RULE_NOT_APPROVED",
        "PROPOSED_THRESHOLD_NOT_APPLIED",
    ):
        assert severity_of(decision, code) == ReadinessSeverity.INFO
    assert decision.evidence["language"]["language"] == "de"  # type: ignore[index]
    assert decision.evidence["recipient"]["binding_fingerprint"] == RECIPIENT.binding.fingerprint()  # type: ignore[index, union-attr]
    assert decision.rationale_version == "inquiry_readiness@1.0.0"
    assert decision.rationale_hash == readiness().rationale_hash


@pytest.mark.parametrize("coc", list(ClaimStatus))
def test_documents_status_never_blocks_the_inquiry(coc: ClaimStatus) -> None:
    documentation = Documentation(coc_available=coc, registration_documents=coc, origin_evidence=None)
    decision = readiness(documentation=documentation, co2=Co2Info())
    assert decision.readiness == InquiryReadiness.INQUIRY_READY


def test_documents_known_shrink_the_open_questions_only() -> None:
    known = Documentation(
        coc_available=ClaimStatus.SELLER_CLAIMED,
        registration_documents=ClaimStatus.SELLER_CLAIMED,
        origin_evidence="seller states German first registration",
    )
    decision = readiness(documentation=known, co2=Co2Info(g_per_km=Decimal("199"), cycle="nedc"))
    assert decision.open_questions == ("availability", "lowest_final_price")
    assert decision.readiness == InquiryReadiness.INQUIRY_READY


def test_no_approval_wait_is_ever_inserted() -> None:
    cases = [
        readiness(),
        readiness(recipient=None),
        readiness(sender=sender(mode="paused")),
        readiness(language=None),
    ]
    for decision in cases:
        for reason in decision.reasons:
            # Only informational economics notes mention approval (tax rule / PROPOSED threshold);
            # nothing ever waits for a human to approve a message or a template.
            if "APPROV" in reason.code:
                assert reason.severity == ReadinessSeverity.INFO
                assert reason.code in {"TAX_RULE_NOT_APPROVED"}
            assert "MESSAGE_APPROVAL" not in reason.code and "TEMPLATE_APPROVAL" not in reason.code
        assert "approval" not in {s.value for s in InquiryReadiness}
    assert not any("approv" in s.value for s in InquiryState)


@pytest.mark.parametrize(
    ("overrides", "code", "expected"),
    [
        ({"recipient": None}, "RECIPIENT_UNKNOWN", InquiryReadiness.NEEDS_FACTS),
        (
            {
                "recipient": verify_recipient(
                    _recipient_evidence(kind=RecipientEvidenceKind.CONTACT_FORM_ONLY, address=None), now=NOW
                )
            },
            "SELLER_EMAIL_UNAVAILABLE",
            InquiryReadiness.NEEDS_FACTS,
        ),
        (
            {
                "recipient": verify_recipient(
                    _recipient_evidence(address="info@autohaus-example.invalid"), now=NOW
                )
            },
            "RECIPIENT_REJECTED",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
        (
            {"recipient": verify_recipient(_recipient_evidence(), now=NOW + timedelta(days=5))},
            "RECIPIENT_RECHECK_REQUIRED",
            InquiryReadiness.NEEDS_FACTS,
        ),
        (
            {"recipient": verify_recipient(_recipient_evidence(listing_id=UUID(int=999)), now=NOW)},
            "RECIPIENT_NOT_FOR_THIS_LISTING",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
        (
            {
                "recipient": verify_recipient(
                    _recipient_evidence(
                        seller=SellerIdentity(
                            seller_type=SellerType.DEALER, aliases=(_alias("other", "fixture_dealer_de"),)
                        )
                    ),
                    now=NOW,
                )
            },
            "RECIPIENT_SELLER_MISMATCH",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
    ],
)
def test_unknown_or_unverified_seller_address_cannot_qualify(
    overrides: dict[str, Any], code: str, expected: InquiryReadiness
) -> None:
    decision = readiness(**overrides)
    assert decision.readiness == expected
    assert code in decision.codes()
    assert decision.can_reserve_now is False


def test_unknown_or_unresolved_language_cannot_qualify() -> None:
    unknown = readiness(language=None)
    assert unknown.readiness == InquiryReadiness.NEEDS_FACTS and "LANGUAGE_UNKNOWN" in unknown.codes()
    swiss = resolve_inquiry_language(None, [], "de", "CH")
    unresolved = readiness(language=swiss)
    assert (
        unresolved.readiness == InquiryReadiness.NEEDS_FACTS and "LANGUAGE_UNRESOLVED" in unresolved.codes()
    )
    dutch = resolve_inquiry_language(
        None, [_fragment("Auto is in goede staat, onderhoud bijgehouden, nieuwe banden, APK geldig.")]
    )
    held = readiness(language=dutch)
    assert (
        held.readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW and "LANGUAGE_UNSUPPORTED" in held.codes()
    )
    only_german = AUTH.model_copy(update={"languages": (MessageLanguage.DE,)})
    french = resolve_inquiry_language(None, [_fragment(FR_TEXT)], None, "CH")
    not_authorized = readiness(authorization=only_german, language=french)
    assert "LANGUAGE_NOT_AUTHORIZED" in not_authorized.codes()
    assert readiness(language=french).readiness == InquiryReadiness.INQUIRY_READY


def _screening(**overrides: Any) -> ScreeningResult:
    return SCREENING.model_copy(update=overrides)


def test_hard_rules_and_profile_scope() -> None:
    rejected = readiness(screening=_screening(state=EligibilityState.REJECTED))
    assert rejected.readiness == InquiryReadiness.NOT_ELIGIBLE and "HARD_RULE_FAILED" in rejected.codes()
    facts = readiness(screening=_screening(state=EligibilityState.NEEDS_FACTS, missing_facts=("mileage",)))
    assert facts.readiness == InquiryReadiness.NEEDS_FACTS and "SCREENING_NEEDS_FACTS" in facts.codes()
    for profile in (ProfileKey.MANUAL_4000, ProfileKey.BELOW_TARGET_WATCH):
        manual = _screening(state=EligibilityState.ELIGIBLE_MANUAL_PROFILE, profile=profile)
        out_of_scope = readiness(screening=manual)
        assert out_of_scope.readiness == InquiryReadiness.NOT_ELIGIBLE
        assert "PROFILE_OUT_OF_INQUIRY_SCOPE" in out_of_scope.codes()
        owner_included = AUTH.model_copy(update={"profiles_in_scope": (ProfileKey.PRIMARY, profile)})
        assert (
            readiness(screening=manual, authorization=owner_included).readiness
            == InquiryReadiness.INQUIRY_READY
        )
    no_primary = AUTH.model_copy(update={"profiles_in_scope": (ProfileKey.MANUAL_4000,)})
    assert readiness(authorization=no_primary).readiness == InquiryReadiness.NOT_ELIGIBLE


def _vehicle(**overrides: Any) -> VehicleIdentification:
    return VehicleIdentification.from_screening(SCREENING, LISTING_OBJ).model_copy(update=overrides)


@pytest.mark.parametrize(
    ("overrides", "code", "expected"),
    [
        (
            {"generation": None, "generation_candidates": ("5N", "AD1")},
            "GENERATION_AMBIGUOUS",
            InquiryReadiness.NEEDS_FACTS,
        ),
        (
            {"generation": None, "generation_candidates": ()},
            "GENERATION_UNKNOWN",
            InquiryReadiness.NEEDS_FACTS,
        ),
        ({"fuel": Fuel.UNKNOWN}, "SPEC_FUEL_UNKNOWN", InquiryReadiness.NEEDS_FACTS),
        ({"gearbox": Gearbox.UNKNOWN}, "SPEC_GEARBOX_UNKNOWN", InquiryReadiness.NEEDS_FACTS),
        ({"matched_via": "title"}, "VEHICLE_IDENTIFICATION_WEAK", InquiryReadiness.NEEDS_FACTS),
        ({"confidence": Confidence.LOW}, "VEHICLE_IDENTIFICATION_WEAK", InquiryReadiness.NEEDS_FACTS),
        ({"model": None}, "VEHICLE_MODEL_UNIDENTIFIED", InquiryReadiness.NEEDS_FACTS),
        ({"is_suv": None}, "SUV_IDENTITY_UNKNOWN", InquiryReadiness.NEEDS_FACTS),
        ({"is_suv": False}, "NOT_SUV", InquiryReadiness.NOT_ELIGIBLE),
    ],
)
def test_vehicle_must_be_sufficiently_identified(
    overrides: dict[str, Any], code: str, expected: InquiryReadiness
) -> None:
    decision = readiness(vehicle=_vehicle(**overrides))
    assert decision.readiness == expected
    assert code in decision.codes()


def test_single_generation_candidate_is_still_ambiguous() -> None:
    decision = readiness(vehicle=_vehicle(generation=None, generation_candidates=("5N",)))
    assert "GENERATION_AMBIGUOUS" in decision.codes()


def test_listing_generation_is_used_only_without_taxonomy_generation_data() -> None:
    match = SCREENING.taxonomy_match
    assert match is not None
    stated = LISTING_OBJ.model_copy(
        update={"vehicle": LISTING_OBJ.vehicle.model_copy(update={"generation": "5N"})}
    )
    no_data = SCREENING.model_copy(
        update={"taxonomy_match": match.model_copy(update={"generation": None, "generation_candidates": ()})}
    )
    assert VehicleIdentification.from_screening(no_data, stated).generation == "5N"
    ambiguous = SCREENING.model_copy(
        update={
            "taxonomy_match": match.model_copy(
                update={"generation": None, "generation_candidates": ("5N", "AD1")}
            )
        }
    )
    assert VehicleIdentification.from_screening(ambiguous, stated).generation is None


def test_every_reason_is_a_typed_code() -> None:
    for decision in (
        readiness(),
        readiness(recipient=None, language=None, sender=SenderStatus(mode="paused")),
    ):
        assert all(isinstance(r.code, ReadinessCode) for r in decision.reasons)
    for reason in SuppressionReason:
        assert ReadinessCode(f"SUPPRESSED_{reason.value.upper()}")


def test_unknown_drive_is_informational() -> None:
    decision = readiness(vehicle=_vehicle(drive=Drive.UNKNOWN))
    assert decision.readiness == InquiryReadiness.INQUIRY_READY
    assert severity_of(decision, "SPEC_DRIVE_UNKNOWN") == ReadinessSeverity.INFO


def test_vehicle_identification_without_taxonomy_match() -> None:
    bare = VehicleIdentification.from_screening(
        SCREENING.model_copy(update={"taxonomy_match": None}), LISTING_OBJ
    )
    assert bare.make is None and bare.matched_via == "none"
    assert readiness(vehicle=bare).readiness == InquiryReadiness.NEEDS_FACTS


def _source(**overrides: Any) -> SourceObservationFacts:
    return inputs().source.model_copy(update=overrides)


def test_market_evidence_rules() -> None:
    missing = readiness(comparables=None)
    assert missing.readiness == InquiryReadiness.NEEDS_FACTS and "COMPARABLES_MISSING" in missing.codes()
    insufficient = ComparableEvidence(status="insufficient_comparables", asking_count=0)
    assert "INSUFFICIENT_COMPARABLES" in readiness(comparables=insufficient).codes()
    no_rationale = ComparableEvidence(status="adequate", asking_count=3, matching_rationale="  ")
    assert readiness(comparables=no_rationale).readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW
    small = ComparableEvidence(
        status="small_sample", asking_count=1, matching_rationale="exact Tiguan 5N match"
    )
    small_decision = readiness(comparables=small)
    assert small_decision.readiness == InquiryReadiness.INQUIRY_READY
    assert severity_of(small_decision, "SMALL_COMPARABLE_SAMPLE") == ReadinessSeverity.INFO
    stale = readiness(source=_source(last_detail_success_at=NOW - timedelta(hours=49)))
    assert stale.readiness == InquiryReadiness.NEEDS_FACTS and "OBSERVATION_STALE" in stale.codes()
    never = readiness(source=_source(last_detail_success_at=None))
    assert "OBSERVATION_STALE" in never.codes()
    paused = readiness(source=_source(source_paused=True))
    assert (
        paused.readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW and "SOURCE_NOT_ACTIVE" in paused.codes()
    )
    forbidden = readiness(source=_source(terms_blocked=True))
    assert forbidden.readiness == InquiryReadiness.NOT_ELIGIBLE


def test_comparable_rationale_is_concrete() -> None:
    evidence = ComparableEvidence.from_comparable_set(COMPARABLES, comparable_set_id=UUID(int=77))
    assert evidence.asking_count == 3
    assert evidence.matching_rationale is not None
    assert "Volkswagen Tiguan 5N" in evidence.matching_rationale
    assert "not realized sale prices" in evidence.matching_rationale
    assert evidence.comparable_set_id == UUID(int=77)
    empty = select_comparables(TARGET, [], CONFIG, NOW)
    assert ComparableEvidence.from_comparable_set(empty).matching_rationale is None


def test_known_costs_exceeding_proceeds_disprove_the_opportunity() -> None:
    disproving = CostEvidence.from_scenario_set(_scenarios(transport="7000"))
    assert disproving.disproves_opportunity()
    decision = readiness(costs=disproving)
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE
    assert "KNOWN_COSTS_EXCEED_PROCEEDS" in decision.codes()
    equal = CostEvidence(
        best_case_known_costs=Money.of("9500", "EUR"), best_case_proceeds=Money.of("9500", "EUR")
    )
    assert equal.disproves_opportunity()
    below = CostEvidence(
        best_case_known_costs=Money.of("9499.99", "EUR"), best_case_proceeds=Money.of("9500", "EUR")
    )
    assert not below.disproves_opportunity()
    other_ccy = CostEvidence(
        best_case_known_costs=Money.of("9999", "CHF"), best_case_proceeds=Money.of("9500", "EUR")
    )
    assert not other_ccy.disproves_opportunity()


def test_unknown_costs_are_listed_never_zero() -> None:
    costs = CostEvidence.from_scenario_set(SCENARIOS)
    assert costs.best_case_known_costs == Money.of("2750", "EUR")
    assert costs.best_case_proceeds is not None
    assert "transport" in costs.unknown_cost_items
    decision = readiness(costs=costs)
    unknown = next(r for r in decision.reasons if r.code == "UNKNOWN_COSTS_LISTED")
    assert "transport" in unknown.message
    none = readiness(costs=None)
    assert none.readiness == InquiryReadiness.INQUIRY_READY
    assert {"COSTS_NOT_EVALUATED", "ECONOMICS_INCOMPLETE"} <= none.codes()
    no_proceeds = readiness(costs=CostEvidence(best_case_known_costs=None, best_case_proceeds=None))
    assert "PROCEEDS_UNKNOWN" in no_proceeds.codes()
    complete = CostEvidence(
        best_case_known_costs=Money.of("5000", "EUR"),
        best_case_proceeds=Money.of("9000", "EUR"),
        complete=True,
        threshold_approved=True,
        tax_rule_approved=True,
    )
    assert readiness(costs=complete).economics_incomplete is False


@pytest.mark.parametrize(
    ("overrides", "code", "expected"),
    [
        (
            {"disqualifiers": DisqualifierFacts(fraud_warnings=("payment_via_courier",))},
            "FRAUD_WARNING",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {"disqualifiers": DisqualifierFacts(identity_conflict_open=True)},
            "IDENTITY_CONFLICT",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
        (
            {"disqualifiers": DisqualifierFacts(availability_conflict=True)},
            "CONTRADICTORY_AVAILABILITY",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
        (
            {"disqualifiers": DisqualifierFacts(seller_opted_out=True)},
            "SELLER_OPTED_OUT",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {
                "disqualifiers": DisqualifierFacts(
                    active_suppressions=(
                        SuppressionRecord(
                            scope="address",
                            key=SELLER_ADDRESS,
                            reason=SuppressionReason.HARD_BOUNCE,
                            effective_at=NOW,
                        ),
                    )
                )
            },
            "SUPPRESSED_HARD_BOUNCE",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {"duplicate": DuplicateDecision(outcome="prior_inquiry")},
            "PRIOR_INQUIRY",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {"duplicate": DuplicateDecision(outcome="in_progress")},
            "INQUIRY_IN_PROGRESS",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {"duplicate": DuplicateDecision(outcome="suppressed")},
            "INQUIRY_SUPPRESSED",
            InquiryReadiness.NOT_ELIGIBLE,
        ),
        (
            {
                "duplicate": DuplicateDecision(
                    outcome="possible_duplicate", reasons=("POSSIBLE_SAME_VEHICLE_UNRESOLVED",)
                )
            },
            "POSSIBLE_DUPLICATE_CONTACT",
            InquiryReadiness.NEEDS_TECHNICAL_REVIEW,
        ),
        ({"source": "sold"}, "VEHICLE_UNAVAILABLE", InquiryReadiness.NOT_ELIGIBLE),
        ({"source": "removed"}, "VEHICLE_UNAVAILABLE", InquiryReadiness.NOT_ELIGIBLE),
        ({"source": "reserved"}, "VEHICLE_RESERVED", InquiryReadiness.NEEDS_FACTS),
    ],
)
def test_disqualifiers(overrides: dict[str, Any], code: str, expected: InquiryReadiness) -> None:
    if "source" in overrides:
        availability = {
            "sold": Availability.SOLD_CLAIMED,
            "removed": Availability.REMOVED,
            "reserved": Availability.RESERVED,
        }
        overrides = {"source": _source(availability=availability[overrides["source"]])}
    decision = readiness(**overrides)
    assert decision.readiness == expected
    assert code in decision.codes()


def test_unknown_availability_is_what_the_inquiry_asks() -> None:
    decision = readiness(source=_source(availability=Availability.UNKNOWN))
    assert decision.readiness == InquiryReadiness.INQUIRY_READY


def test_sender_prerequisites_are_technical_not_approval() -> None:
    not_ready = readiness(sender=sender(mode="disabled_until_sender_ready"))
    assert not_ready.readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW
    assert "SENDER_NOT_READY" in not_ready.codes()
    unconfigured = readiness(
        sender=SenderStatus(mode="automatic")  # nothing configured or verified
    )
    assert {
        "SENDER_PROVIDER_MISSING",
        "SENDER_ACCOUNT_MISSING",
        "SENDER_FROM_INVALID",
        "SENDER_DISPLAY_NAME_MISSING",
        "SENDER_NOT_VERIFIED",
        "SENDER_ALIAS_NOT_VERIFIED",
        "SENDER_UNHEALTHY",
    } <= unconfigured.codes()
    assert (
        readiness(sender=sender(credentials_revoked=True)).readiness
        == InquiryReadiness.NEEDS_TECHNICAL_REVIEW
    )
    assert "REPLY_TO_INVALID" in readiness(sender=sender(reply_to_address="not an address")).codes()
    assert readiness(sender=sender(reply_to_address=None)).readiness == InquiryReadiness.INQUIRY_READY


def test_sender_status_from_settings() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        seller_inquiry_mode="automatic",
        seller_email_provider="gmail_api",
        seller_email_account_id="synthetic-account-1",
        seller_email_from="vasko@example.invalid",
        seller_email_reply_to="vasko@example.invalid",
    )
    status = SenderStatus.from_settings(
        settings,
        binding_id=SENDER_BINDING,
        binding_version=1,
        display_name="Vasko K.",
        alias_verified=True,
        verified_at=NOW,
        health_ok=True,
    )
    assert status.identity_problems() == ()
    assert status.provider == EmailProviderKind.GMAIL_API
    default = SenderStatus.from_settings(
        Settings(_env_file=None),  # type: ignore[call-arg]
        binding_id=None,
        binding_version=None,
        display_name=None,
        alias_verified=False,
        verified_at=None,
        health_ok=False,
    )
    assert default.mode == "disabled_until_sender_ready" and default.provider is None


def test_holds_keep_readiness_but_block_reservation() -> None:
    paused = readiness(sender=sender(mode="paused"))
    assert paused.readiness == InquiryReadiness.INQUIRY_READY and not paused.can_reserve_now
    assert severity_of(paused, "INQUIRIES_PAUSED") == ReadinessSeverity.HOLD
    killed = readiness(sender=sender(kill_switch=True))
    assert killed.readiness == InquiryReadiness.INQUIRY_READY and not killed.can_reserve_now
    caps = evaluate_rate_caps(
        [QuotaDebit(inquiry_id=UUID(int=900 + i), at=NOW - timedelta(hours=i + 1)) for i in range(2)],
        now=NOW,
        policy=RateCapPolicy(),
    )
    capped = readiness(rate_caps=caps)
    assert not capped.can_reserve_now and "RATE_CAP_REACHED" in capped.codes()
    assert capped.next_attempt_at == NOW - timedelta(hours=2) + WINDOW_24H
    cooldown = evaluate_seller_cooldown([NOW - timedelta(days=1)], now=NOW)
    cooled = readiness(seller_cooldown=cooldown, rate_caps=caps)
    assert cooled.next_attempt_at == NOW - timedelta(days=1) + SELLER_COOLDOWN
    blocked = readiness(rate_caps=caps, recipient=None)
    assert blocked.next_attempt_at is None  # not qualified: no retry time is promised


def test_severity_precedence() -> None:
    decision = readiness(
        recipient=None,  # needs_facts
        sender=sender(mode="disabled_until_sender_ready"),  # technical review
        disqualifiers=DisqualifierFacts(seller_opted_out=True),  # not eligible
    )
    assert decision.readiness == InquiryReadiness.NOT_ELIGIBLE
    facts_and_review = readiness(recipient=None, sender=sender(mode="disabled_until_sender_ready"))
    assert facts_and_review.readiness == InquiryReadiness.NEEDS_FACTS


# ---------------------------------------------------------------------------------------------
# Identity and the one-inquiry rule (spec 37.5)
# ---------------------------------------------------------------------------------------------


def test_identity_uses_cluster_then_incarnation_never_listing_id() -> None:
    assert canonical_vehicle_identity(
        vehicle_cluster_id=CLUSTER, listing_incarnation_id=INCARNATION
    ) == VehicleIdentityRef(kind="vehicle_cluster", id=CLUSTER)
    assert IDENTITY.vehicle == VehicleIdentityRef(kind="listing_incarnation", id=INCARNATION)
    assert IDENTITY.seller_key == f"seller_entity:{ENTITY}"
    assert IDENTITY.purpose == "initial_availability_documents_price"
    assert (
        IDENTITY.key()
        == build_inquiry_identity(
            WS, vehicle_cluster_id=None, listing_incarnation_id=INCARNATION, seller=SELLER
        ).key()
    )
    other_ws = build_inquiry_identity(
        UUID(int=2), vehicle_cluster_id=None, listing_incarnation_id=INCARNATION, seller=SELLER
    )
    assert other_ws.key() != IDENTITY.key()
    assert "source_listing_id" not in InquiryIdentity.model_fields
    with pytest.raises(ValidationError):
        InquiryIdentity(workspace_id=WS, vehicle=IDENTITY.vehicle, seller_key="SYNTH-1")


def test_three_cross_site_ads_and_aliases_produce_one_inquiry_identity() -> None:
    sites = ("fixture_market_de", "fixture_market_it", "fixture_market_ch")
    identities = set()
    for n, site in enumerate(sites):
        seller_on_site = SellerIdentity(
            seller_entity_id=ENTITY,
            seller_type=SellerType.DEALER,
            aliases=(_alias(f"seller-{n}", site),),
        )
        identity = build_inquiry_identity(
            WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=UUID(int=600 + n), seller=seller_on_site
        )
        recipient = verify_recipient(
            _recipient_evidence(
                kind=RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING,
                address=f"reply-{n}@relay.{site.replace('_', '-')}.invalid",
                extraction_location=ExtractionLocation.LISTING_RELAY_CONTACT,
                relay_listing_reference=REF,
                source_key=site,
                seller=seller_on_site,
            ),
            now=NOW,
            relay_domains=(f"relay.{site.replace('_', '-')}.invalid",),
        )
        assert recipient.verified  # three different relay addresses
        identities.add(identity.key())
    assert len(identities) == 1
    first = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=UUID(int=600), seller=SELLER
    )
    existing = [_existing(InquiryState.ACCEPTED, identity=first)]
    second = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=UUID(int=602), seller=SELLER
    )
    assert evaluate_duplicate_contact(second, existing).outcome == "prior_inquiry"


def _existing(
    state: InquiryState,
    *,
    identity: InquiryIdentity = IDENTITY,
    attempts: int = 0,
    inquiry_id: UUID = INQUIRY,
    reserved_at: datetime | None = None,
    seller_key: str | None = None,
) -> ExistingInquiry:
    return ExistingInquiry(
        inquiry_id=inquiry_id,
        identity_key=identity.key(),
        vehicle=identity.vehicle,
        seller_key=seller_key or identity.seller_key,
        state=state,
        transmission_attempts=attempts,
        reserved_at=reserved_at,
    )


@pytest.mark.parametrize(
    ("state", "attempts", "outcome"),
    [
        (InquiryState.CANDIDATE, 0, "continue_existing"),
        (InquiryState.QUALIFYING, 0, "continue_existing"),
        (InquiryState.HELD_FACTS, 0, "continue_existing"),
        (InquiryState.CANCELLED, 0, "continue_existing"),
        (InquiryState.CANCELLED, 1, "prior_inquiry"),
        (InquiryState.RESERVED, 0, "in_progress"),
        (InquiryState.QUEUED, 0, "in_progress"),
        (InquiryState.SENDING, 1, "prior_inquiry"),
        (InquiryState.UNCERTAIN, 1, "prior_inquiry"),
        (InquiryState.ACCEPTED, 1, "prior_inquiry"),
        (InquiryState.NO_REPLY_YET, 1, "prior_inquiry"),
        (InquiryState.REPLIED, 1, "prior_inquiry"),
        (InquiryState.BOUNCED, 1, "prior_inquiry"),
        (InquiryState.SELLER_OPTED_OUT, 1, "prior_inquiry"),
        (InquiryState.FAILED_DEFINITE, 1, "prior_inquiry"),
        (InquiryState.SUPPRESSED, 0, "suppressed"),
    ],
)
def test_one_inquiry_rule(state: InquiryState, attempts: int, outcome: str) -> None:
    decision = evaluate_duplicate_contact(IDENTITY, [_existing(state, attempts=attempts)])
    assert decision.outcome == outcome
    assert decision.blocks == (outcome not in {"clear", "continue_existing"})
    if outcome != "clear":
        assert decision.existing_inquiry_id == INQUIRY


def test_price_change_relisting_profile_or_sender_change_does_not_reset_the_rule() -> None:
    # The identity has no price, listing id, profile or sender in it: a relisted ad of the same
    # clustered vehicle and seller maps to the same identity and finds the earlier inquiry.
    sent = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=UUID(int=700), seller=SELLER
    )
    relisted = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=UUID(int=701), seller=SELLER
    )
    assert sent.key() == relisted.key()
    assert (
        evaluate_duplicate_contact(relisted, [_existing(InquiryState.NO_REPLY_YET, identity=sent)]).outcome
        == "prior_inquiry"
    )


def test_clear_when_nothing_exists() -> None:
    assert evaluate_duplicate_contact(IDENTITY, []).outcome == "clear"
    unrelated = build_inquiry_identity(
        WS, vehicle_cluster_id=UUID(int=55), listing_incarnation_id=UUID(int=56), seller=SELLER
    )
    assert (
        evaluate_duplicate_contact(IDENTITY, [_existing(InquiryState.ACCEPTED, identity=unrelated)]).outcome
        == "clear"
    )


def test_unresolved_possible_same_vehicle_suppresses_the_additional_send() -> None:
    link = RelatedListingLink(
        related_listing_id=UUID(int=801),
        relation="possible_same_unresolved",
        related_inquiry_state=InquiryState.ACCEPTED,
    )
    decision = evaluate_duplicate_contact(IDENTITY, [], [link])
    assert decision.outcome == "possible_duplicate"
    assert decision.reasons == ("POSSIBLE_SAME_VEHICLE_UNRESOLVED",)
    for state in (
        InquiryState.RESERVED,
        InquiryState.QUEUED,
        InquiryState.UNCERTAIN,
        InquiryState.FAILED_DEFINITE,
    ):
        assert evaluate_duplicate_contact(
            IDENTITY, [], [link.model_copy(update={"related_inquiry_state": state})]
        ).blocks
    for state in (None, InquiryState.CANDIDATE, InquiryState.QUALIFYING, InquiryState.CANCELLED):
        free = link.model_copy(update={"related_inquiry_state": state})
        assert evaluate_duplicate_contact(IDENTITY, [], [free]).outcome == "clear"
    rejected = link.model_copy(update={"relation": "rejected_not_same"})
    assert evaluate_duplicate_contact(IDENTITY, [], [rejected]).outcome == "clear"
    merge_pending = RelatedListingLink(
        related_listing_id=UUID(int=802),
        relation="confirmed_same_vehicle",
        related_inquiry_state=InquiryState.QUEUED,
        related_vehicle=VehicleIdentityRef(kind="vehicle_cluster", id=CLUSTER),
    )
    assert evaluate_duplicate_contact(IDENTITY, [], [merge_pending]).reasons == ("IDENTITY_MERGE_PENDING",)
    same_ref = merge_pending.model_copy(update={"related_vehicle": IDENTITY.vehicle})
    assert evaluate_duplicate_contact(IDENTITY, [], [same_ref]).outcome == "clear"


def test_same_vehicle_with_an_unlinked_seller_identity_is_held() -> None:
    other_seller = _existing(
        InquiryState.ACCEPTED, inquiry_id=UUID(int=901), seller_key="seller_alias:" + "a" * 64
    )
    other = other_seller.model_copy(update={"identity_key": "other"})
    decision = evaluate_duplicate_contact(IDENTITY, [other])
    assert decision.outcome == "possible_duplicate"
    assert decision.reasons == ("SAME_VEHICLE_OTHER_SELLER_IDENTITY",)
    pending = other.model_copy(update={"state": InquiryState.CANDIDATE})
    assert evaluate_duplicate_contact(IDENTITY, [pending]).outcome == "clear"


def test_identity_merge_keeps_exactly_one_inquiry() -> None:
    a = _existing(InquiryState.QUEUED, inquiry_id=UUID(int=1001), reserved_at=NOW - timedelta(hours=2))
    b = _existing(InquiryState.RESERVED, inquiry_id=UUID(int=1002), reserved_at=NOW - timedelta(hours=3))
    c = _existing(InquiryState.QUALIFYING, inquiry_id=UUID(int=1003))
    for order in ([a, b, c], [c, b, a], [b, c, a]):
        result = reconcile_identity_merge(order)
        assert result.keep == a.inquiry_id
        assert result.cancel == (b.inquiry_id, c.inquiry_id)
        assert not result.conflict
    sent = _existing(
        InquiryState.UNCERTAIN, inquiry_id=UUID(int=1004), attempts=1, reserved_at=NOW - timedelta(hours=1)
    )
    with_sent = reconcile_identity_merge([a, sent])
    assert with_sent.keep == sent.inquiry_id and with_sent.cancel == (a.inquiry_id,)
    sent2 = _existing(
        InquiryState.ACCEPTED, inquiry_id=UUID(int=1005), attempts=1, reserved_at=NOW - timedelta(hours=5)
    )
    conflict = reconcile_identity_merge([sent, sent2])
    assert conflict.conflict and conflict.keep == sent2.inquiry_id
    assert conflict.transmitted_duplicates == (sent.inquiry_id,)
    done = reconcile_identity_merge([_existing(InquiryState.CANCELLED), _existing(InquiryState.SUPPRESSED)])
    assert done.keep is None and done.cancel == ()
    same_time = [
        _existing(InquiryState.QUEUED, inquiry_id=UUID(int=2002), reserved_at=NOW),
        _existing(InquiryState.QUEUED, inquiry_id=UUID(int=2001), reserved_at=NOW),
    ]
    assert reconcile_identity_merge(same_time).keep == UUID(int=2001)


def test_seller_contact_times() -> None:
    items = [
        _existing(InquiryState.ACCEPTED, inquiry_id=UUID(int=1), reserved_at=NOW - timedelta(days=2)),
        _existing(InquiryState.CANDIDATE, inquiry_id=UUID(int=2)),
        _existing(
            InquiryState.QUEUED,
            inquiry_id=UUID(int=3),
            reserved_at=NOW,
            seller_key="seller_entity:" + "0" * 36,
        ),
    ]
    assert seller_contact_times(items, IDENTITY.seller_key) == (NOW - timedelta(days=2),)


# ---------------------------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------------------------


def test_spec_state_diagram_edges() -> None:
    edges = [
        (InquiryState.CANDIDATE, InquiryState.QUALIFYING),
        (InquiryState.QUALIFYING, InquiryState.RESERVED),
        (InquiryState.QUALIFYING, InquiryState.HELD_FACTS),
        (InquiryState.QUALIFYING, InquiryState.SUPPRESSED),
        (InquiryState.QUALIFYING, InquiryState.CANCELLED),
        (InquiryState.RESERVED, InquiryState.QUEUED),
        (InquiryState.QUEUED, InquiryState.SENDING),
        (InquiryState.SENDING, InquiryState.ACCEPTED),
        (InquiryState.SENDING, InquiryState.UNCERTAIN),
        (InquiryState.SENDING, InquiryState.FAILED_DEFINITE),
        (InquiryState.ACCEPTED, InquiryState.REPLIED),
        (InquiryState.ACCEPTED, InquiryState.BOUNCED),
        (InquiryState.ACCEPTED, InquiryState.SELLER_OPTED_OUT),
        (InquiryState.ACCEPTED, InquiryState.NO_REPLY_YET),
    ]
    for current, target in edges:
        assert can_transition(current, target), (current, target)
    assert set(ALLOWED_TRANSITIONS) == set(InquiryState)
    assert {InquiryState.BOUNCED, InquiryState.SELLER_OPTED_OUT} == TERMINAL_STATES


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (InquiryState.ACCEPTED, InquiryState.QUEUED),
        (InquiryState.ACCEPTED, InquiryState.SENDING),
        (InquiryState.UNCERTAIN, InquiryState.QUEUED),
        (InquiryState.UNCERTAIN, InquiryState.SENDING),
        (InquiryState.UNCERTAIN, InquiryState.CANCELLED),
        (InquiryState.SENDING, InquiryState.QUEUED),
        (InquiryState.SENDING, InquiryState.CANCELLED),
        (InquiryState.REPLIED, InquiryState.QUEUED),
        (InquiryState.NO_REPLY_YET, InquiryState.SENDING),
        (InquiryState.BOUNCED, InquiryState.QUEUED),
        (InquiryState.CANDIDATE, InquiryState.RESERVED),
        (InquiryState.QUALIFYING, InquiryState.QUEUED),
        (InquiryState.RESERVED, InquiryState.SENDING),
        (InquiryState.HELD_FACTS, InquiryState.RESERVED),
    ],
)
def test_forbidden_transitions(current: InquiryState, target: InquiryState) -> None:
    assert not can_transition(current, target)
    with pytest.raises(ValidationFailed) as exc:
        require_transition(current, target)
    assert exc.value.details["problem"] == "TRANSITION_NOT_ALLOWED"


def _attempt(**overrides: Any) -> SendAttemptEvidence:
    data: dict[str, Any] = {
        "attempt_id": UUID(int=3001),
        "inquiry_id": INQUIRY,
        "sender_binding_id": SENDER_BINDING,
        "provider": EmailProviderKind.GMAIL_API,
        "fencing_token": 1,
        "started_at": NOW - timedelta(minutes=10),
        "finished_at": NOW - timedelta(minutes=9),
        "outcome": SendAttemptOutcome.UNCERTAIN,
        "worker_alive": Tristate.NO,
        "lease_expires_at": NOW - timedelta(minutes=5),
    }
    data.update(overrides)
    return SendAttemptEvidence(**data)


def test_guarded_edges() -> None:
    accepted = _attempt(outcome=SendAttemptOutcome.ACCEPTED)
    require_transition(InquiryState.SENDING, InquiryState.ACCEPTED, TransitionContext(attempt=accepted))
    with pytest.raises(ValidationFailed, match="sending"):
        require_transition(InquiryState.SENDING, InquiryState.ACCEPTED)
    with pytest.raises(ValidationFailed):
        require_transition(InquiryState.SENDING, InquiryState.ACCEPTED, TransitionContext(attempt=_attempt()))
    require_transition(InquiryState.SENDING, InquiryState.UNCERTAIN)  # always allowed (timeouts/crash)
    proven = _attempt(
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="connection_refused_before_submit",
    )
    require_transition(InquiryState.SENDING, InquiryState.FAILED_DEFINITE, TransitionContext(attempt=proven))
    rejected = _attempt(outcome=SendAttemptOutcome.DEFINITE_REJECTION)
    require_transition(
        InquiryState.SENDING, InquiryState.FAILED_DEFINITE, TransitionContext(attempt=rejected)
    )
    unproven = _attempt(outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE)
    with pytest.raises(ValidationFailed):
        require_transition(
            InquiryState.SENDING, InquiryState.FAILED_DEFINITE, TransitionContext(attempt=unproven)
        )
    found = reconcile_uncertain(ReconciliationEvidence(sent_items="found"))
    require_transition(InquiryState.UNCERTAIN, InquiryState.ACCEPTED, TransitionContext(reconciliation=found))
    with pytest.raises(ValidationFailed):
        require_transition(
            InquiryState.UNCERTAIN, InquiryState.FAILED_DEFINITE, TransitionContext(reconciliation=found)
        )
    with pytest.raises(ValidationFailed):
        require_transition(InquiryState.UNCERTAIN, InquiryState.ACCEPTED)
    retry = should_retry(proven, now=NOW)
    require_transition(InquiryState.FAILED_DEFINITE, InquiryState.QUEUED, TransitionContext(retry=retry))
    with pytest.raises(ValidationFailed):
        require_transition(
            InquiryState.FAILED_DEFINITE,
            InquiryState.QUEUED,
            TransitionContext(retry=should_retry(_attempt(), now=NOW)),
        )
    require_transition(InquiryState.CANCELLED, InquiryState.QUALIFYING)
    with pytest.raises(ValidationFailed):
        require_transition(
            InquiryState.CANCELLED, InquiryState.QUALIFYING, TransitionContext(transmission_attempts=1)
        )
    with pytest.raises(ValidationFailed):
        require_transition(InquiryState.SUPPRESSED, InquiryState.QUALIFYING)
    require_transition(
        InquiryState.SUPPRESSED,
        InquiryState.QUALIFYING,
        TransitionContext(suppression_removal_audit_id=UUID(int=5)),
    )


def test_quota_release_only_for_never_transmitted_reservations() -> None:
    assert releases_quota(InquiryState.RESERVED, InquiryState.CANCELLED)
    assert releases_quota(InquiryState.QUEUED, InquiryState.SUPPRESSED)
    for current in (
        InquiryState.SENDING,
        InquiryState.UNCERTAIN,
        InquiryState.ACCEPTED,
        InquiryState.FAILED_DEFINITE,
    ):
        for target in InquiryState:
            assert not releases_quota(current, target)


def test_crashed_sending_becomes_uncertain_and_keeps_its_reservation() -> None:
    outcome = on_sending_interrupted(InquiryState.SENDING)
    assert outcome.inquiry_state == InquiryState.UNCERTAIN
    assert outcome.job_state == JobState.BLOCKED
    assert outcome.job_blocked_reason == EMAIL_DELIVERY_UNCERTAIN == "EMAIL_DELIVERY_UNCERTAIN"
    assert outcome.suppression_reason == SuppressionReason.UNRESOLVED_SEND_OUTCOME
    assert outcome.retain_reservation is True and outcome.retain_quota_debit is True
    with pytest.raises(ValidationFailed):
        on_sending_interrupted(InquiryState.QUEUED)


# ---------------------------------------------------------------------------------------------
# Uncertain-send policy
# ---------------------------------------------------------------------------------------------


def test_proven_pre_submission_failure_may_retry_on_the_same_account_only() -> None:
    proven = _attempt(
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="local_validation_failed_before_submit",
    )
    decision = should_retry(proven, now=NOW)
    assert decision.retry and decision.sender_binding_id == SENDER_BINDING
    assert decision.reasons == ("PROVEN_PRE_SUBMISSION_FAILURE",)
    other_account = should_retry(proven, now=NOW, retry_sender_binding_id=UUID(int=999))
    assert not other_account.retry and other_account.reasons == ("DIFFERENT_ACCOUNT_FORBIDDEN",)
    same_account = should_retry(proven, now=NOW, retry_sender_binding_id=SENDER_BINDING)
    assert same_account.retry


def test_uncertain_sends_are_never_blindly_retried() -> None:
    timeout = should_retry(_attempt(), now=NOW)
    assert not timeout.retry and timeout.reasons == ("HOLD_FOR_RECONCILIATION",)
    empty_sent_items = should_retry(
        _attempt(sent_items_search="not_found", provider_search="not_found"), now=NOW
    )
    assert not empty_sent_items.retry
    assert "EMPTY_SEARCH_IS_NOT_PROOF" in empty_sent_items.reasons
    no_proof = should_retry(_attempt(outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE), now=NOW)
    assert no_proof.reasons == ("NO_PROOF_OF_NON_SUBMISSION",)
    assert should_retry(_attempt(outcome=SendAttemptOutcome.DEFINITE_REJECTION), now=NOW).reasons == (
        "DEFINITE_REJECTION",
    )


def test_documented_provider_idempotency() -> None:
    idem = _attempt(provider_idempotency_documented=True, provider_idempotency_key="idem-synthetic-1")
    decision = should_retry(idem, now=NOW)
    assert decision.retry and decision.idempotency_key == "idem-synthetic-1"
    outlook = _attempt(
        provider=EmailProviderKind.OUTLOOK_LOCAL,
        provider_idempotency_documented=True,
        provider_idempotency_key="k",
    )
    assert not should_retry(outlook, now=NOW).retry
    without_key = _attempt(provider_idempotency_documented=True)
    assert not should_retry(without_key, now=NOW).retry


@pytest.mark.parametrize(
    "attempt_overrides",
    [
        {"outcome": SendAttemptOutcome.RUNNING, "finished_at": None},
        {"finished_at": None, "worker_alive": Tristate.UNKNOWN},
        {"finished_at": None, "worker_alive": Tristate.YES},
        {"finished_at": None, "worker_alive": Tristate.NO, "lease_expires_at": NOW + timedelta(minutes=1)},
    ],
)
def test_possibly_running_prior_attempt_blocks_retry(attempt_overrides: dict[str, Any]) -> None:
    proven = _attempt(
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="connection_refused_before_submit",
    )
    running = _attempt(attempt_id=UUID(int=3002), **attempt_overrides)
    decision = should_retry(proven, now=NOW, other_attempts=[running])
    assert not decision.retry and decision.reasons == ("PRIOR_ATTEMPT_MAY_STILL_RUN",)


def test_retry_limits_and_submitted_evidence() -> None:
    proven = _attempt(
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="connection_refused_before_submit",
    )
    found = _attempt(attempt_id=UUID(int=3003), sent_items_search="found")
    assert should_retry(proven, now=NOW, other_attempts=[found]).reasons == ("ALREADY_SUBMITTED",)
    earlier = [
        proven.model_copy(update={"attempt_id": UUID(int=3100 + i)}) for i in range(MAX_SEND_ATTEMPTS - 1)
    ]
    assert should_retry(proven, now=NOW, other_attempts=earlier).reasons == ("ATTEMPTS_EXHAUSTED",)
    foreign = proven.model_copy(update={"attempt_id": UUID(int=3200), "inquiry_id": UUID(int=4242)})
    with pytest.raises(ValidationFailed):
        should_retry(proven, now=NOW, other_attempts=[foreign])
    with pytest.raises(ValidationError):
        _attempt(
            outcome=SendAttemptOutcome.UNCERTAIN, pre_submission_proof="connection_refused_before_submit"
        )


def test_reconciliation_never_releases_on_an_empty_search() -> None:
    assert reconcile_uncertain(ReconciliationEvidence(sent_items="found")).next_state == InquiryState.ACCEPTED
    assert (
        reconcile_uncertain(ReconciliationEvidence(provider_search="found")).next_state
        == InquiryState.ACCEPTED
    )
    empty = reconcile_uncertain(ReconciliationEvidence(sent_items="not_found", provider_search="not_found"))
    assert empty.next_state is None
    assert "EMPTY_SEARCH_IS_NOT_PROOF" in empty.reasons
    assert empty.release_reservation is False
    pending = reconcile_uncertain(
        ReconciliationEvidence(proven_not_submitted="provider_documented_not_sent", worker_alive=Tristate.NO)
    )
    assert pending.next_state is None and "OUTBOX_MAY_STILL_SUBMIT" in pending.reasons
    proven = reconcile_uncertain(
        ReconciliationEvidence(
            proven_not_submitted="provider_documented_not_sent",
            worker_alive=Tristate.NO,
            outbox_pending=Tristate.NO,
        )
    )
    assert proven.next_state == InquiryState.FAILED_DEFINITE
    alive = reconcile_uncertain(
        ReconciliationEvidence(
            proven_not_submitted="provider_documented_not_sent",
            worker_alive=Tristate.UNKNOWN,
            outbox_pending=Tristate.NO,
        )
    )
    assert alive.next_state is None


# ---------------------------------------------------------------------------------------------
# Rate caps and cooldown
# ---------------------------------------------------------------------------------------------


def _debits(*times: datetime) -> list[QuotaDebit]:
    return [QuotaDebit(inquiry_id=UUID(int=7000 + i), at=t) for i, t in enumerate(times)]


def test_caps_exactly_at_two_per_rolling_24h() -> None:
    policy = RateCapPolicy()
    t0 = NOW - timedelta(hours=20)
    assert evaluate_rate_caps([], now=NOW, policy=policy).allowed
    assert evaluate_rate_caps(_debits(t0), now=NOW, policy=policy).allowed
    two = _debits(t0, t0 + timedelta(hours=1))
    denied = evaluate_rate_caps(two, now=NOW, policy=policy)
    assert not denied.allowed and denied.reasons == ("RATE_CAP_24H_REACHED",)
    assert denied.count_24h == 2 and denied.limit_24h == 2
    assert denied.next_allowed_at == t0 + WINDOW_24H
    just_before = evaluate_rate_caps(two, now=t0 + WINDOW_24H - timedelta(microseconds=1), policy=policy)
    assert not just_before.allowed
    at_boundary = evaluate_rate_caps(two, now=t0 + WINDOW_24H, policy=policy)
    assert at_boundary.allowed and at_boundary.count_24h == 1


def test_caps_exactly_at_five_per_rolling_15_days() -> None:
    policy = RateCapPolicy()
    oldest = NOW - timedelta(days=14)
    five = _debits(*(oldest + timedelta(days=3 * i) for i in range(5)))  # never 2 within 24 h
    denied = evaluate_rate_caps(five, now=NOW, policy=policy)
    assert not denied.allowed and denied.reasons == ("RATE_CAP_15D_REACHED",)
    assert denied.count_15d == 5 and denied.next_allowed_at == oldest + WINDOW_15D
    assert not evaluate_rate_caps(
        five, now=oldest + WINDOW_15D - timedelta(microseconds=1), policy=policy
    ).allowed
    assert evaluate_rate_caps(five, now=oldest + WINDOW_15D, policy=policy).allowed
    four = five[1:]
    assert evaluate_rate_caps(four, now=NOW, policy=policy).allowed


def test_caps_combined_next_time_is_the_later_window() -> None:
    policy = RateCapPolicy()
    times = [
        NOW - timedelta(days=13),
        NOW - timedelta(days=10),
        NOW - timedelta(days=5),
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
    ]
    decision = evaluate_rate_caps(_debits(*times), now=NOW, policy=policy)
    assert set(decision.reasons) == {"RATE_CAP_24H_REACHED", "RATE_CAP_15D_REACHED"}
    assert decision.next_allowed_at == max(times[3] + WINDOW_24H, times[0] + WINDOW_15D)


def test_caps_zero_future_debits_and_exclusion() -> None:
    paused = evaluate_rate_caps([], now=NOW, policy=RateCapPolicy(max_per_24h=0))
    assert not paused.allowed and paused.reasons == ("RATE_CAP_ZERO",) and paused.next_allowed_at is None
    future = evaluate_rate_caps(
        _debits(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), now=NOW, policy=RateCapPolicy()
    )
    assert not future.allowed  # clock-skewed debits still count
    own = _debits(NOW - timedelta(hours=1), NOW - timedelta(minutes=30))
    assert evaluate_rate_caps(
        own, now=NOW, policy=RateCapPolicy(), exclude_inquiry_id=own[0].inquiry_id
    ).allowed


def test_rate_cap_policy_is_a_ceiling() -> None:
    assert RateCapPolicy() == RateCapPolicy(max_per_24h=2, max_per_15d=5)
    with pytest.raises(ValidationError):
        RateCapPolicy(max_per_24h=3)
    with pytest.raises(ValidationError):
        RateCapPolicy(max_per_15d=6)
    default = RateCapPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    assert (default.max_per_24h, default.max_per_15d) == (2, 5)
    reduced = RateCapPolicy.from_settings(
        Settings(_env_file=None, seller_inquiry_max_per_24h=1, seller_inquiry_max_per_rolling_15d=0)  # type: ignore[call-arg]
    )
    assert (reduced.max_per_24h, reduced.max_per_15d) == (1, 0)
    with pytest.raises(ValidationFailed):
        RateCapPolicy.from_settings(Settings(_env_file=None, seller_inquiry_max_per_24h=10))  # type: ignore[call-arg]


def test_seller_cooldown() -> None:
    assert not evaluate_seller_cooldown([], now=NOW).active
    recent = evaluate_seller_cooldown([NOW - timedelta(days=8), NOW - timedelta(days=2)], now=NOW)
    assert recent.active and recent.until == NOW - timedelta(days=2) + SELLER_COOLDOWN
    expired = evaluate_seller_cooldown([NOW - SELLER_COOLDOWN], now=NOW)
    assert not expired.active


# ---------------------------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------------------------


def _targets() -> SuppressionTargets:
    return SuppressionTargets(
        workspace_id=WS,
        seller_key=IDENTITY.seller_key,
        canonical_address=SELLER_ADDRESS,
        vehicle_key=IDENTITY.vehicle.key(),
        source_key="fixture_dealer_de",
        sender_binding_id=SENDER_BINDING,
    )


def test_suppression_matching() -> None:
    records = [
        SuppressionRecord(
            scope="seller",
            key=IDENTITY.seller_key,
            reason=SuppressionReason.SELLER_OPT_OUT,
            effective_at=NOW - timedelta(days=30),
        ),
        SuppressionRecord(
            scope="address",
            key="VERKAUF@autohaus-example.invalid",
            reason=SuppressionReason.HARD_BOUNCE,
            effective_at=NOW,
        ),
        SuppressionRecord(
            scope="vehicle",
            key=IDENTITY.vehicle.key(),
            reason=SuppressionReason.CONTRADICTORY_AVAILABILITY,
            effective_at=NOW,
        ),
        SuppressionRecord(
            scope="source", key="fixture_dealer_de", reason=SuppressionReason.SOURCE_PAUSED, effective_at=NOW
        ),
        SuppressionRecord(
            scope="sender", key=str(SENDER_BINDING), reason=SuppressionReason.SENDER_REVOKED, effective_at=NOW
        ),
        SuppressionRecord(scope="workspace", key="*", reason=SuppressionReason.KILL_SWITCH, effective_at=NOW),
    ]
    assert len(matching_suppressions(records, _targets(), at=NOW)) == 6
    # The seller-scope opt-out survives an address change and a reappearing advertisement.
    moved = _targets().model_copy(update={"canonical_address": "new-address@autohaus-example.invalid"})
    assert records[0] in matching_suppressions(records, moved, at=NOW)
    other = SuppressionTargets(workspace_id=UUID(int=2), seller_key="seller_entity:" + "1" * 36)
    assert matching_suppressions(records, other, at=NOW) == (records[5],)


def test_suppression_lifecycle() -> None:
    future = SuppressionRecord(
        scope="seller",
        key=IDENTITY.seller_key,
        reason=SuppressionReason.MANUAL,
        effective_at=NOW + timedelta(hours=1),
    )
    assert matching_suppressions([future], _targets(), at=NOW) == ()
    removed = SuppressionRecord(
        scope="seller",
        key=IDENTITY.seller_key,
        reason=SuppressionReason.MANUAL,
        effective_at=NOW - timedelta(days=2),
        removed_at=NOW - timedelta(days=1),
        removal_audit_id=UUID(int=8),
    )
    assert matching_suppressions([removed], _targets(), at=NOW) == ()
    assert removed.active_at(NOW - timedelta(days=1, hours=12))
    with pytest.raises(ValidationError):
        SuppressionRecord(
            scope="seller", key="x", reason=SuppressionReason.MANUAL, effective_at=NOW, removed_at=NOW
        )
    with pytest.raises(ValidationError):
        SuppressionRecord(
            scope="seller",
            key="x",
            reason=SuppressionReason.MANUAL,
            effective_at=NOW,
            removed_at=NOW - timedelta(days=1),
            removal_audit_id=UUID(int=9),
        )


# ---------------------------------------------------------------------------------------------
# Binding and dispatch preflight
# ---------------------------------------------------------------------------------------------

LABEL = build_vehicle_label("Volkswagen", "Tiguan", "5N")
MESSAGE = render("seller_initial_de_v1", LABEL, REF, URL, "Vasko K.", verified_listing_url=URL)


def _bind(**overrides: Any) -> InquiryBinding:
    args: dict[str, Any] = {
        "at": NOW,
        "inquiry_id": INQUIRY,
        "identity": IDENTITY,
        "authorization": AUTH,
        "readiness": readiness(),
        "language": LANGUAGE_DE,
        "message": MESSAGE,
        "sender": sender(),
        "recipient": RECIPIENT,
        "listing": SNAPSHOT,
    }
    args.update(overrides)
    return bind_inquiry(**args)


BINDING = _bind()


def test_binding_records_exact_scope_template_body_sender_and_recipient() -> None:
    assert BINDING.body_hash == MESSAGE.body_hash
    assert BINDING.scope_hash == MESSAGE.scope_hash
    assert BINDING.template_hash == MESSAGE.template_hash
    assert BINDING.template_id == "seller_initial_de_v1" and BINDING.language == MessageLanguage.DE
    assert BINDING.sender.binding_id == SENDER_BINDING and BINDING.sender.account_id == "synthetic-account-1"
    assert BINDING.recipient.canonical_address == SELLER_ADDRESS
    assert BINDING.identity_key == IDENTITY.key() and BINDING.vehicle_key == IDENTITY.vehicle.key()
    assert BINDING.authorization_version == AUTH.version
    assert BINDING.binding_hash() == _bind().binding_hash()


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"readiness": readiness(recipient=None)}, "NOT_INQUIRY_READY"),
        ({"language": resolve_inquiry_language(None, [_fragment(FR_TEXT)])}, "MESSAGE_LANGUAGE_MISMATCH"),
        ({"language": resolve_inquiry_language(None, [])}, "LANGUAGE_NOT_RESOLVED"),
        ({"message": render_preview_mk(MESSAGE)}, "NOT_A_SELLER_MESSAGE"),
        ({"sender": sender(display_name="Someone Else")}, "SENDER_DISPLAY_NAME_MISMATCH"),
        ({"sender": sender(health_ok=False)}, "SENDER_NOT_USABLE"),
        (
            {
                "recipient": verify_recipient(
                    _recipient_evidence(kind=RecipientEvidenceKind.NO_EMAIL_FOUND, address=None), now=NOW
                )
            },
            "RECIPIENT_NOT_VERIFIED",
        ),
        (
            {
                "message": render(
                    "seller_initial_de_v1",
                    LABEL,
                    REF,
                    URL + "?x=1",
                    "Vasko K.",
                    verified_listing_url=URL + "?x=1",
                )
            },
            "LISTING_URL_MISMATCH",
        ),
        (
            {
                "message": render(
                    "seller_initial_de_v1", LABEL, "OTHER-1", URL, "Vasko K.", verified_listing_url=URL
                )
            },
            "LISTING_REFERENCE_MISMATCH",
        ),
        (
            {"listing": SNAPSHOT.model_copy(update={"listing_id": UUID(int=404)})},
            "RECIPIENT_LISTING_MISMATCH",
        ),
        ({"at": datetime(2026, 10, 1, tzinfo=UTC)}, "AUTHORIZATION_INACTIVE"),
        ({"message": MESSAGE.model_copy(update={"template_hash": "0" * 64})}, "TEMPLATE_NOT_REGISTERED"),
        (
            {"message": MESSAGE.model_copy(update={"template_id": "seller_initial_de_v9"})},
            "TEMPLATE_NOT_REGISTERED",
        ),
    ],
)
def test_binding_refuses_inconsistent_inputs(overrides: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        _bind(**overrides)
    assert problem in exc.value.details["problems"]


def test_binding_is_immutable_once_reserved() -> None:
    changed = BINDING.model_copy(update={"body_hash": "0" * 64})
    for state in PRE_RESERVATION_STATES:
        assert apply_binding(state, BINDING, changed) is changed
        assert apply_binding(state, None, BINDING) is BINDING
    for state in BINDING_IMMUTABLE_STATES:
        assert apply_binding(state, BINDING, BINDING.model_copy()) is BINDING
        with pytest.raises(IdempotencyConflict):
            apply_binding(state, BINDING, changed)
        with pytest.raises(ValidationFailed):
            apply_binding(state, None, BINDING)
    other_sender = BINDING.model_copy(
        update={"sender": BINDING.sender.model_copy(update={"account_id": "other"})}
    )
    with pytest.raises(IdempotencyConflict):
        apply_binding(InquiryState.QUEUED, BINDING, other_sender)


RESERVED_AT = NOW - timedelta(hours=1)


def _facts(**overrides: Any) -> DispatchFacts:
    data: dict[str, Any] = {
        "now": NOW,
        "state": InquiryState.QUEUED,
        "binding": BINDING,
        "identity": IDENTITY,
        "reserved_at": RESERVED_AT,
        "authorization": AUTH,
        "workspace_id": WS,
        "current_listing": SNAPSHOT,
        "source": inputs().source,
        "disqualifiers": DisqualifierFacts(),
        "other_inquiries": (),
        "related_links": (),
        "sender": sender(),
        "recipient_recheck": ContactRecheck(material_change=False, recheck_required=False, changes=()),
        "current_language": LANGUAGE_DE,
        "suppressions": (),
        "attempts": (),
        "rate_caps": evaluate_rate_caps([], now=NOW, policy=RateCapPolicy()),
        "quota_debit_present": True,
        "message_approval_required": False,
        "message": MESSAGE,
        "envelope": MessageEnvelope(to=(SELLER_ADDRESS,), reply_to=("vasko@example.invalid",)),
    }
    data.update(overrides)
    return DispatchFacts(**data)


def test_preflight_proceeds_without_any_approval() -> None:
    decision = dispatch_preflight(_facts())
    assert decision.outcome == PreflightOutcome.PROCEED
    assert decision.reasons == ("ALL_CHECKS_PASSED",)
    approval_configured = dispatch_preflight(_facts(message_approval_required=True))
    assert approval_configured.outcome == PreflightOutcome.HOLD
    assert approval_configured.reasons == ("OWNER_CONFIGURED_MESSAGE_APPROVAL",)
    approved = dispatch_preflight(_facts(message_approval_required=True, message_approval_recorded=True))
    assert approved.outcome == PreflightOutcome.PROCEED


@pytest.mark.parametrize(
    ("listing_update", "codes"),
    [
        ({"price_amount_minor": 260000}, {"PRICE_CHANGED"}),
        ({"price_currency": "CHF"}, {"PRICE_CHANGED"}),
        ({"availability": Availability.SOLD_CLAIMED}, {"AVAILABILITY_CHANGED", "VEHICLE_UNAVAILABLE"}),
        ({"availability": Availability.UNKNOWN}, {"AVAILABILITY_CHANGED"}),
        ({"availability": Availability.RESERVED}, {"AVAILABILITY_CHANGED", "VEHICLE_UNAVAILABLE"}),
        ({"revision_number": 2, "semantic_hash": "f" * 64}, {"LISTING_REVISION_CHANGED"}),
        ({"listing_incarnation_id": UUID(int=99)}, {"LISTING_IDENTITY_CHANGED"}),
    ],
)
def test_changed_price_or_availability_cancels_stale_queued_message(
    listing_update: dict[str, Any], codes: set[str]
) -> None:
    decision = dispatch_preflight(_facts(current_listing=SNAPSHOT.model_copy(update=listing_update)))
    assert decision.outcome == PreflightOutcome.CANCEL_STALE
    assert decision.target_state == InquiryState.CANCELLED
    assert codes <= set(decision.reasons)
    assert releases_quota(InquiryState.QUEUED, decision.target_state)


def test_source_availability_claim_cancels() -> None:
    sold = dispatch_preflight(_facts(source=_source(availability=Availability.SOLD_CLAIMED)))
    assert sold.outcome == PreflightOutcome.CANCEL_STALE and "VEHICLE_UNAVAILABLE" in sold.reasons


@pytest.mark.parametrize(
    ("overrides", "reason", "code"),
    [
        ({"sender": sender(kill_switch=True)}, SuppressionReason.KILL_SWITCH, "KILL_SWITCH_ACTIVE"),
        ({"source": "paused"}, SuppressionReason.SOURCE_PAUSED, "SOURCE_NOT_ACTIVE"),
        ({"sender": sender(credentials_revoked=True)}, SuppressionReason.SENDER_REVOKED, "SENDER_REVOKED"),
        (
            {
                "suppressions": (
                    SuppressionRecord(
                        scope="seller",
                        key=IDENTITY.seller_key,
                        reason=SuppressionReason.SELLER_OPT_OUT,
                        effective_at=NOW - timedelta(minutes=1),
                    ),
                )
            },
            SuppressionReason.SELLER_OPT_OUT,
            "SUPPRESSED_SELLER_OPT_OUT",
        ),
        (
            {
                "suppressions": (
                    SuppressionRecord(
                        scope="address",
                        key=SELLER_ADDRESS,
                        reason=SuppressionReason.HARD_BOUNCE,
                        effective_at=NOW - timedelta(minutes=1),
                    ),
                )
            },
            SuppressionReason.HARD_BOUNCE,
            "SUPPRESSED_HARD_BOUNCE",
        ),
        (
            {
                "suppressions": (
                    SuppressionRecord(
                        scope="vehicle",
                        key=IDENTITY.vehicle.key(),
                        reason=SuppressionReason.COMPLAINT,
                        effective_at=NOW,
                    ),
                )
            },
            SuppressionReason.COMPLAINT,
            "SUPPRESSED_COMPLAINT",
        ),
    ],
)
def test_kill_switch_bounce_and_opt_out_suppress_sending(
    overrides: dict[str, Any], reason: SuppressionReason, code: str
) -> None:
    if overrides.get("source") == "paused":
        overrides = {"source": _source(source_paused=True)}
    decision = dispatch_preflight(_facts(**overrides))
    assert decision.outcome == PreflightOutcome.CANCEL_STALE
    assert decision.target_state == InquiryState.SUPPRESSED
    assert decision.suppression_reason == reason
    assert code in decision.reasons


def test_revoked_authorization_suppresses() -> None:
    revoked = AUTH.model_copy(
        update={
            "revocation": AUTH.revocation.model_copy(
                update={
                    "revoked": True,
                    "revoked_at": NOW - timedelta(minutes=1),
                    "revoked_by": "Vasko",
                    "reason": "stop",
                }
            )
        }
    )
    decision = dispatch_preflight(_facts(authorization=revoked))
    assert decision.target_state == InquiryState.SUPPRESSED and "AUTHORIZATION_REVOKED" in decision.reasons
    newer = AUTH.model_copy(update={"version": 2})
    changed = dispatch_preflight(_facts(authorization=newer))
    assert changed.target_state == InquiryState.CANCELLED and "AUTHORIZATION_CHANGED" in changed.reasons


@pytest.mark.parametrize(
    "sender_update",
    [
        {"binding_id": UUID(int=32)},
        {"binding_version": 2},
        {"account_id": "synthetic-account-2"},
        {"from_address": "other@example.invalid"},
        {"display_name": "V. K."},
        {"provider": EmailProviderKind.MICROSOFT_GRAPH},
        {"reply_to_address": "other@example.invalid"},
    ],
)
def test_sender_change_cancels_never_silently_switches(sender_update: dict[str, Any]) -> None:
    decision = dispatch_preflight(_facts(sender=sender(**sender_update)))
    assert decision.outcome == PreflightOutcome.CANCEL_STALE
    assert "SENDER_CHANGED" in decision.reasons


def test_recipient_and_language_rechecks() -> None:
    changed = dispatch_preflight(
        _facts(recipient_recheck=ContactRecheck(material_change=True, recheck_required=True, changes=()))
    )
    assert changed.target_state == InquiryState.CANCELLED and "RECIPIENT_CHANGED" in changed.reasons
    stale = dispatch_preflight(
        _facts(recipient_recheck=ContactRecheck(material_change=False, recheck_required=True, changes=()))
    )
    assert stale.outcome == PreflightOutcome.HOLD and stale.reasons == ("RECIPIENT_RECHECK_REQUIRED",)
    french = resolve_inquiry_language(None, [_fragment(FR_TEXT)])
    assert "LANGUAGE_CHANGED" in dispatch_preflight(_facts(current_language=french)).reasons
    unresolved = resolve_inquiry_language(None, [])
    assert "LANGUAGE_CHANGED" in dispatch_preflight(_facts(current_language=unresolved)).reasons
    assert dispatch_preflight(_facts(current_language=LANGUAGE_DE)).outcome == PreflightOutcome.PROCEED


def _tampered(body: str) -> RenderedMessage:
    return RenderedMessage(
        **{**MESSAGE.model_dump(), "body": body, "body_hash": message_body_hash(MESSAGE.subject, body)}
    )


def test_message_and_envelope_must_match_the_binding() -> None:
    tampered = dispatch_preflight(_facts(message=_tampered(MESSAGE.body.replace("Guten Tag", "Hallo"))))
    assert (
        tampered.outcome == PreflightOutcome.CANCEL_STALE and "MESSAGE_BINDING_MISMATCH" in tampered.reasons
    )
    cc = dispatch_preflight(_facts(envelope=MessageEnvelope(to=(SELLER_ADDRESS,), cc=("x@example.invalid",))))
    assert "SCOPE_VALIDATION_FAILED" in cc.reasons
    attachment = dispatch_preflight(
        _facts(envelope=MessageEnvelope(to=(SELLER_ADDRESS,), attachments=("a.pdf",)))
    )
    assert "SCOPE_VALIDATION_FAILED" in attachment.reasons
    other_to = dispatch_preflight(_facts(envelope=MessageEnvelope(to=("other@example.invalid",))))
    assert "RECIPIENT_BINDING_MISMATCH" in other_to.reasons
    broken = dispatch_preflight(_facts(envelope=MessageEnvelope(to=("not an address",))))
    assert "RECIPIENT_BINDING_MISMATCH" in broken.reasons
    other_reply = dispatch_preflight(
        _facts(envelope=MessageEnvelope(to=(SELLER_ADDRESS,), reply_to=("x@example.invalid",)))
    )
    assert "REPLY_TO_BINDING_MISMATCH" in other_reply.reasons
    case_only = dispatch_preflight(
        _facts(
            envelope=MessageEnvelope(
                to=("verkauf@AUTOHAUS-example.invalid",), reply_to=("vasko@EXAMPLE.invalid",)
            )
        )
    )
    assert case_only.outcome == PreflightOutcome.PROCEED  # domain case is canonicalised
    missing_reply_to = dispatch_preflight(_facts(envelope=MessageEnvelope(to=(SELLER_ADDRESS,))))
    assert "REPLY_TO_BINDING_MISMATCH" in missing_reply_to.reasons  # replies must reach the bound mailbox
    local_case = dispatch_preflight(
        _facts(envelope=MessageEnvelope(to=("Verkauf@autohaus-example.invalid",), reply_to=(REPLY_TO,)))
    )
    assert "RECIPIENT_BINDING_MISMATCH" in local_case.reasons  # the local part is never case-folded


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"sender": "@paused"}, "MODE_NOT_AUTOMATIC"),
        ({"sender": "@disabled"}, "MODE_NOT_AUTOMATIC"),
        ({"sender": "@unhealthy"}, "SENDER_NOT_USABLE"),
        ({"quota_debit_present": False}, "QUOTA_DEBIT_MISSING"),
        ({"attempts": "@running"}, "PRIOR_ATTEMPT_MAY_STILL_RUN"),
        ({"attempts": "@uncertain"}, "UNRESOLVED_PRIOR_ATTEMPT"),
        ({"state": InquiryState.RESERVED}, "NOT_QUEUED"),
        ({"state": InquiryState.UNCERTAIN}, "NOT_QUEUED"),
    ],
)
def test_preflight_holds(overrides: dict[str, Any], code: str) -> None:
    mapping: dict[str, Any] = {
        "@paused": sender(mode="paused"),
        "@disabled": sender(mode="disabled_until_sender_ready"),
        "@unhealthy": sender(health_ok=False),
        "@running": (_attempt(outcome=SendAttemptOutcome.RUNNING, finished_at=None),),
        "@uncertain": (_attempt(),),
    }
    resolved = {k: mapping.get(v, v) if isinstance(v, str) else v for k, v in overrides.items()}
    decision = dispatch_preflight(_facts(**resolved))
    assert decision.outcome == PreflightOutcome.HOLD
    assert code in decision.reasons


def test_rate_cap_hold_reports_next_time() -> None:
    caps = evaluate_rate_caps(
        _debits(NOW - timedelta(hours=3), NOW - timedelta(hours=2)), now=NOW, policy=RateCapPolicy()
    )
    decision = dispatch_preflight(_facts(rate_caps=caps))
    assert decision.outcome == PreflightOutcome.HOLD and decision.reasons == ("RATE_CAP_REACHED",)
    assert decision.next_attempt_at == NOW - timedelta(hours=3) + WINDOW_24H


def test_preflight_precedence_suppress_then_cancel_then_hold() -> None:
    everything = dispatch_preflight(
        _facts(
            sender=sender(kill_switch=True, mode="paused"),
            current_listing=SNAPSHOT.model_copy(update={"price_amount_minor": 1}),
        )
    )
    assert everything.target_state == InquiryState.SUPPRESSED
    assert {"KILL_SWITCH_ACTIVE", "PRICE_CHANGED", "MODE_NOT_AUTOMATIC"} <= set(everything.reasons)
    cancel_and_hold = dispatch_preflight(
        _facts(
            sender=sender(mode="paused"),
            current_listing=SNAPSHOT.model_copy(update={"price_amount_minor": 1}),
        )
    )
    assert cancel_and_hold.target_state == InquiryState.CANCELLED


def test_no_follow_up_is_possible_after_acceptance() -> None:
    reachable: set[InquiryState] = set()
    frontier = [InquiryState.ACCEPTED]
    while frontier:
        state = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS[state]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == {
        InquiryState.REPLIED,
        InquiryState.BOUNCED,
        InquiryState.SELLER_OPTED_OUT,
        InquiryState.NO_REPLY_YET,
    }
    assert not reachable & {InquiryState.QUEUED, InquiryState.SENDING, InquiryState.RESERVED}
    assert reachable <= POSSIBLY_TRANSMITTED_STATES


# ---------------------------------------------------------------------------------------------
# Identity prerequisites, binding cross-checks and dispatch-time re-checks (hardening)
# ---------------------------------------------------------------------------------------------


def test_inquiry_identity_needs_a_linked_seller_entity() -> None:
    missing = readiness(identity=None)
    assert missing.readiness == InquiryReadiness.NEEDS_FACTS
    assert "INQUIRY_IDENTITY_MISSING" in missing.codes()
    alias_only = SellerIdentity(
        seller_type=SellerType.DEALER, aliases=(_alias("dealer-1", "fixture_dealer_de"),)
    )
    alias_identity = build_inquiry_identity(
        WS, vehicle_cluster_id=None, listing_incarnation_id=INCARNATION, seller=alias_only
    )
    alias_recipient = verify_recipient(_recipient_evidence(seller=alias_only), now=NOW)
    unlinked = readiness(identity=alias_identity, recipient=alias_recipient)
    assert unlinked.readiness == InquiryReadiness.NEEDS_FACTS
    assert unlinked.codes() & {"SELLER_ENTITY_UNLINKED", "RECIPIENT_SELLER_MISMATCH"} == {
        "SELLER_ENTITY_UNLINKED"
    }
    assert severity_of(unlinked, "SELLER_ENTITY_UNLINKED") == ReadinessSeverity.NEEDS_FACTS
    other_incarnation = build_inquiry_identity(
        WS, vehicle_cluster_id=None, listing_incarnation_id=UUID(int=4040), seller=SELLER
    )
    mismatch = readiness(identity=other_incarnation)
    assert mismatch.readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW
    assert "INQUIRY_IDENTITY_MISMATCH" in mismatch.codes()
    clustered = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=INCARNATION, seller=SELLER
    )
    assert readiness(identity=clustered).readiness == InquiryReadiness.INQUIRY_READY


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"readiness": readiness(sender=sender(kill_switch=True))}, "RESERVATION_ON_HOLD"),
        (
            {
                "identity": build_inquiry_identity(
                    WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=INCARNATION, seller=SELLER
                )
            },
            "READINESS_FOR_OTHER_IDENTITY",
        ),
        (
            {"listing": SNAPSHOT.model_copy(update={"listing_incarnation_id": UUID(int=4041)})},
            "LISTING_IDENTITY_MISMATCH",
        ),
        (
            {
                "message": RenderedMessage(
                    **{
                        **MESSAGE.model_dump(),
                        "body": MESSAGE.body.replace("Guten Tag,", "Sehr geehrte Damen und Herren,"),
                        "body_hash": message_body_hash(
                            MESSAGE.subject,
                            MESSAGE.body.replace("Guten Tag,", "Sehr geehrte Damen und Herren,"),
                        ),
                    }
                )
            },
            "NOT_TEMPLATE_RENDERING",
        ),
        ({"sender": sender(from_address="not an address")}, "SENDER_NOT_USABLE"),
    ],
)
def test_binding_cross_checks(overrides: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        _bind(**overrides)
    assert problem in exc.value.details["problems"]


def test_cooldown_counts_only_live_or_transmitted_contacts() -> None:
    def item(n: int, state: InquiryState, attempts: int = 0) -> ExistingInquiry:
        return _existing(
            state, inquiry_id=UUID(int=5000 + n), attempts=attempts, reserved_at=NOW - timedelta(days=n)
        )

    items = [
        item(1, InquiryState.CANCELLED),  # never transmitted: contacted nobody
        item(2, InquiryState.SUPPRESSED),
        item(3, InquiryState.QUALIFYING),
        item(4, InquiryState.RESERVED),
        item(5, InquiryState.QUEUED),
        item(6, InquiryState.FAILED_DEFINITE, attempts=1),
        item(7, InquiryState.CANCELLED, attempts=1),  # an attempt existed: it may have left
        item(8, InquiryState.NO_REPLY_YET, attempts=1),
    ]
    times = seller_contact_times(items, IDENTITY.seller_key)
    assert times == tuple(NOW - timedelta(days=n) for n in (4, 5, 6, 7, 8))
    assert evaluate_seller_cooldown(times, now=NOW).active
    assert not evaluate_seller_cooldown(seller_contact_times(items[:3], IDENTITY.seller_key), now=NOW).active


def test_correlated_reply_or_bounce_resolves_an_uncertain_send() -> None:
    decision = reconcile_uncertain(ReconciliationEvidence(correlated_inbound=True, sent_items="not_found"))
    assert decision.next_state == InquiryState.ACCEPTED
    assert decision.reasons == ("CORRELATED_INBOUND_MESSAGE",)
    assert decision.release_reservation is False
    require_transition(
        InquiryState.UNCERTAIN, InquiryState.ACCEPTED, TransitionContext(reconciliation=decision)
    )


def _racing(
    n: int, *, reserved_at: datetime | None, state: InquiryState = InquiryState.QUEUED
) -> ExistingInquiry:
    return ExistingInquiry(
        inquiry_id=UUID(int=n),
        identity_key=IDENTITY.key(),
        vehicle=IDENTITY.vehicle,
        seller_key=IDENTITY.seller_key,
        state=state,
        reserved_at=reserved_at,
    )


def test_concurrent_reservations_of_one_identity_never_both_send() -> None:
    # Two workers reserved the same vehicle/seller pair (e.g. through two aliases before a merge).
    me = BINDING.inquiry_id
    earlier = _racing(1, reserved_at=RESERVED_AT - timedelta(minutes=1))
    later = _racing(2, reserved_at=RESERVED_AT + timedelta(minutes=1))
    loses = dispatch_preflight(_facts(other_inquiries=(earlier,)))
    assert loses.outcome == PreflightOutcome.CANCEL_STALE and "DUPLICATE_INQUIRY" in loses.reasons
    assert loses.target_state == InquiryState.CANCELLED
    wins = dispatch_preflight(_facts(other_inquiries=(later,)))
    assert wins.outcome == PreflightOutcome.PROCEED
    same_time_smaller_id = _racing(int(me.int) - 1, reserved_at=RESERVED_AT)
    assert "DUPLICATE_INQUIRY" in dispatch_preflight(_facts(other_inquiries=(same_time_smaller_id,))).reasons
    unknown_time = _racing(3, reserved_at=None)
    assert "DUPLICATE_INQUIRY" in dispatch_preflight(_facts(other_inquiries=(unknown_time,))).reasons
    sent_later = _racing(4, reserved_at=RESERVED_AT + timedelta(hours=1), state=InquiryState.SENDING)
    assert "DUPLICATE_INQUIRY" in dispatch_preflight(_facts(other_inquiries=(sent_later,))).reasons
    myself = _racing(me.int, reserved_at=RESERVED_AT)
    assert dispatch_preflight(_facts(other_inquiries=(myself,))).outcome == PreflightOutcome.PROCEED


def test_identity_merge_at_dispatch_cannot_double_send() -> None:
    # After a cluster merge the queued inquiry's identity changed: it is cancelled and requalified.
    merged = build_inquiry_identity(
        WS, vehicle_cluster_id=CLUSTER, listing_incarnation_id=INCARNATION, seller=SELLER
    )
    changed = dispatch_preflight(_facts(identity=merged))
    assert changed.target_state == InquiryState.CANCELLED and "INQUIRY_IDENTITY_CHANGED" in changed.reasons
    # The same vehicle under another (alias) seller identity was already contacted: hold.
    other_seller = ExistingInquiry(
        inquiry_id=UUID(int=6001),
        identity_key="another-identity",
        vehicle=IDENTITY.vehicle,
        seller_key="seller_alias:" + "b" * 64,
        state=InquiryState.ACCEPTED,
        transmission_attempts=1,
        reserved_at=RESERVED_AT + timedelta(minutes=5),
    )
    held = dispatch_preflight(_facts(other_inquiries=(other_seller,)))
    assert held.outcome == PreflightOutcome.HOLD and "POSSIBLE_DUPLICATE_CONTACT" in held.reasons
    later_reserved = other_seller.model_copy(
        update={"state": InquiryState.QUEUED, "transmission_attempts": 0}
    )
    assert dispatch_preflight(_facts(other_inquiries=(later_reserved,))).outcome == PreflightOutcome.PROCEED
    earlier_reserved = later_reserved.model_copy(update={"reserved_at": RESERVED_AT - timedelta(minutes=5)})
    assert (
        "POSSIBLE_DUPLICATE_CONTACT"
        in dispatch_preflight(_facts(other_inquiries=(earlier_reserved,))).reasons
    )


def test_possible_same_vehicle_link_is_rechecked_at_dispatch() -> None:
    link = RelatedListingLink(
        related_listing_id=UUID(int=7001),
        relation="possible_same_unresolved",
        related_inquiry_state=InquiryState.ACCEPTED,
        related_inquiry_id=UUID(int=7002),
        related_reserved_at=RESERVED_AT + timedelta(hours=2),
    )
    held = dispatch_preflight(_facts(related_links=(link,)))
    assert held.outcome == PreflightOutcome.HOLD and held.reasons == ("POSSIBLE_DUPLICATE_CONTACT",)
    queued_later = link.model_copy(update={"related_inquiry_state": InquiryState.QUEUED})
    assert dispatch_preflight(_facts(related_links=(queued_later,))).outcome == PreflightOutcome.PROCEED
    queued_earlier = queued_later.model_copy(update={"related_reserved_at": RESERVED_AT - timedelta(hours=2)})
    assert dispatch_preflight(_facts(related_links=(queued_earlier,))).outcome == PreflightOutcome.HOLD
    self_link = queued_earlier.model_copy(update={"related_inquiry_id": BINDING.inquiry_id})
    assert dispatch_preflight(_facts(related_links=(self_link,))).outcome == PreflightOutcome.PROCEED


@pytest.mark.parametrize(
    ("disqualifiers", "outcome", "target", "code"),
    [
        (
            DisqualifierFacts(fraud_warnings=("payment_before_viewing",)),
            "cancel",
            InquiryState.CANCELLED,
            "FRAUD_WARNING",
        ),
        (
            DisqualifierFacts(identity_conflict_open=True),
            "cancel",
            InquiryState.CANCELLED,
            "IDENTITY_CONFLICT",
        ),
        (DisqualifierFacts(seller_opted_out=True), "suppress", InquiryState.SUPPRESSED, "SELLER_OPTED_OUT"),
        (
            DisqualifierFacts(availability_conflict=True),
            "suppress",
            InquiryState.SUPPRESSED,
            "CONTRADICTORY_AVAILABILITY",
        ),
        (
            DisqualifierFacts(
                active_suppressions=(
                    SuppressionRecord(
                        scope="seller",
                        key=IDENTITY.seller_key,
                        reason=SuppressionReason.COMPLAINT,
                        effective_at=NOW - timedelta(minutes=1),
                    ),
                )
            ),
            "suppress",
            InquiryState.SUPPRESSED,
            "SUPPRESSED_COMPLAINT",
        ),
    ],
)
def test_disqualifiers_are_rechecked_at_dispatch(
    disqualifiers: DisqualifierFacts, outcome: str, target: InquiryState, code: str
) -> None:
    decision = dispatch_preflight(_facts(disqualifiers=disqualifiers))
    assert decision.outcome == PreflightOutcome.CANCEL_STALE
    assert decision.target_state == target and code in decision.reasons
    if outcome == "suppress":
        assert decision.suppression_reason is not None


def test_seller_cooldown_is_rechecked_at_dispatch() -> None:
    other_car = build_inquiry_identity(
        WS, vehicle_cluster_id=UUID(int=8001), listing_incarnation_id=UUID(int=8002), seller=SELLER
    )
    contacted = ExistingInquiry(
        inquiry_id=UUID(int=8003),
        identity_key=other_car.key(),
        vehicle=other_car.vehicle,
        seller_key=other_car.seller_key,
        state=InquiryState.ACCEPTED,
        transmission_attempts=1,
        reserved_at=NOW - timedelta(days=2),
        last_contact_at=NOW - timedelta(days=2),
    )
    held = dispatch_preflight(_facts(other_inquiries=(contacted,)))
    assert held.outcome == PreflightOutcome.HOLD and held.reasons == ("SELLER_COOLDOWN",)
    assert held.next_attempt_at == NOW - timedelta(days=2) + SELLER_COOLDOWN
    old = contacted.model_copy(
        update={"last_contact_at": NOW - timedelta(days=8), "reserved_at": NOW - timedelta(days=8)}
    )
    assert dispatch_preflight(_facts(other_inquiries=(old,))).outcome == PreflightOutcome.PROCEED
    racing_later = contacted.model_copy(
        update={
            "state": InquiryState.QUEUED,
            "transmission_attempts": 0,
            "last_contact_at": None,
            "reserved_at": RESERVED_AT + timedelta(minutes=1),
        }
    )
    assert dispatch_preflight(_facts(other_inquiries=(racing_later,))).outcome == PreflightOutcome.PROCEED
    racing_earlier = racing_later.model_copy(update={"reserved_at": RESERVED_AT - timedelta(minutes=1)})
    assert "SELLER_COOLDOWN" in dispatch_preflight(_facts(other_inquiries=(racing_earlier,))).reasons
    no_time = contacted.model_copy(update={"last_contact_at": None, "reserved_at": None})
    unknown = dispatch_preflight(_facts(other_inquiries=(no_time,)))
    assert unknown.outcome == PreflightOutcome.HOLD and unknown.next_attempt_at is None
    cancelled_unsent = racing_earlier.model_copy(update={"state": InquiryState.CANCELLED})
    assert dispatch_preflight(_facts(other_inquiries=(cancelled_unsent,))).outcome == PreflightOutcome.PROCEED


def test_hold_reports_the_latest_known_retry_time() -> None:
    caps = evaluate_rate_caps(
        _debits(NOW - timedelta(hours=3), NOW - timedelta(hours=2)), now=NOW, policy=RateCapPolicy()
    )
    other_car = build_inquiry_identity(
        WS, vehicle_cluster_id=UUID(int=8101), listing_incarnation_id=UUID(int=8102), seller=SELLER
    )
    contacted = ExistingInquiry(
        inquiry_id=UUID(int=8103),
        identity_key=other_car.key(),
        vehicle=other_car.vehicle,
        seller_key=other_car.seller_key,
        state=InquiryState.NO_REPLY_YET,
        transmission_attempts=1,
        last_contact_at=NOW - timedelta(days=1),
    )
    decision = dispatch_preflight(_facts(rate_caps=caps, other_inquiries=(contacted,)))
    assert set(decision.reasons) == {"RATE_CAP_REACHED", "SELLER_COOLDOWN"}
    assert decision.next_attempt_at == NOW - timedelta(days=1) + SELLER_COOLDOWN
    zero = evaluate_rate_caps([], now=NOW, policy=RateCapPolicy(max_per_24h=0))
    assert dispatch_preflight(_facts(rate_caps=zero)).next_attempt_at is None


def test_only_the_exact_template_rendering_is_dispatched() -> None:
    reworded = MESSAGE.body.replace("Guten Tag,", "Sehr geehrte Damen und Herren,")
    in_scope_but_hand_built = RenderedMessage(
        **{
            **MESSAGE.model_dump(),
            "body": reworded,
            "body_hash": message_body_hash(MESSAGE.subject, reworded),
        }
    )
    decision = dispatch_preflight(_facts(message=in_scope_but_hand_built))
    assert decision.target_state == InquiryState.CANCELLED
    assert {"MESSAGE_NOT_TEMPLATE_RENDERING", "MESSAGE_BINDING_MISMATCH"} <= set(decision.reasons)
    html = MessageEnvelope(
        to=(SELLER_ADDRESS,), reply_to=(REPLY_TO,), extra_headers={"Content-Type": "text/html; charset=utf-8"}
    )
    assert "SCOPE_VALIDATION_FAILED" in dispatch_preflight(_facts(envelope=html)).reasons
    multipart = MessageEnvelope(
        to=(SELLER_ADDRESS,),
        reply_to=(REPLY_TO,),
        extra_headers={"Content-Type": "multipart/mixed; boundary=x"},
    )
    assert "SCOPE_VALIDATION_FAILED" in dispatch_preflight(_facts(envelope=multipart)).reasons
    plain = MessageEnvelope(
        to=(SELLER_ADDRESS,),
        reply_to=(REPLY_TO,),
        extra_headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Transfer-Encoding": "quoted-printable",
            "MIME-Version": "1.0",
            "Message-ID": "<synthetic-inquiry-1@example.invalid>",
        },
    )
    assert dispatch_preflight(_facts(envelope=plain)).outcome == PreflightOutcome.PROCEED


def test_dispatch_facts_require_the_rechecks() -> None:
    data = _facts().model_dump()
    assert DispatchFacts(**data) == _facts()
    for field in (
        "identity",
        "reserved_at",
        "disqualifiers",
        "other_inquiries",
        "related_links",
        "current_language",
        "suppressions",
        "attempts",
        "message_approval_required",
    ):
        partial = {k: v for k, v in data.items() if k != field}
        with pytest.raises(ValidationError):
            DispatchFacts(**partial)


# ---------------------------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------------------------


def test_quota_debit_counts_at_its_send_attempt() -> None:
    reserved = QuotaDebit(inquiry_id=UUID(int=1), at=NOW - timedelta(days=3))
    assert reserved.counted_at == reserved.at
    sent = reserved.model_copy(update={"send_attempted_at": NOW - timedelta(hours=1)})
    assert sent.counted_at == NOW - timedelta(hours=1)
    # A held reservation debited days ago occupies the 24 h window again once it is sent.
    assert evaluate_rate_caps([reserved], now=NOW, policy=RateCapPolicy()).count_24h == 0
    assert evaluate_rate_caps([sent], now=NOW, policy=RateCapPolicy()).count_24h == 1
    with pytest.raises(ValidationError):
        QuotaDebit(inquiry_id=UUID(int=1), at=NOW, send_attempted_at=datetime(2026, 10, 6))  # naive


def test_queued_backlog_cannot_leave_in_one_burst() -> None:
    """Five reservations taken while the sender was offline must not all go out at once."""
    t0 = NOW - timedelta(days=6)
    reserved = [t0, t0 + timedelta(minutes=5), t0 + timedelta(days=1), t0 + timedelta(days=1, minutes=5)]
    reserved.append(t0 + timedelta(days=2))
    debits = {
        UUID(int=9000 + i): QuotaDebit(inquiry_id=UUID(int=9000 + i), at=t) for i, t in enumerate(reserved)
    }
    sent: list[datetime] = []
    for minute, inquiry_id in enumerate(sorted(debits)):
        now = NOW + timedelta(minutes=minute)
        decision = evaluate_rate_caps(
            list(debits.values()), now=now, policy=RateCapPolicy(), exclude_inquiry_id=inquiry_id
        )
        if decision.allowed:
            debits[inquiry_id] = debits[inquiry_id].model_copy(update={"send_attempted_at": now})
            sent.append(now)
        else:
            assert decision.reasons == ("RATE_CAP_24H_REACHED",)
            assert decision.next_allowed_at == NOW + WINDOW_24H
    assert len(sent) == 2  # the ceiling, not five within minutes
    later = evaluate_rate_caps(
        list(debits.values()),
        now=NOW + WINDOW_24H,
        policy=RateCapPolicy(),
        exclude_inquiry_id=sorted(debits)[2],
    )
    assert later.allowed


def test_retry_is_denied_after_an_unresolved_prior_attempt() -> None:
    uncertain = _attempt(attempt_id=UUID(int=3301))
    proven = _attempt(
        attempt_id=UUID(int=3302),
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="connection_refused_before_submit",
    )
    decision = should_retry(proven, now=NOW, other_attempts=[uncertain])
    assert not decision.retry and decision.reasons == ("PRIOR_ATTEMPT_UNRESOLVED",)
    # Documented provider idempotency covers the retry only with the very same key.
    keyed = {"provider_idempotency_documented": True, "provider_idempotency_key": "idem-synthetic-1"}
    first = _attempt(attempt_id=UUID(int=3303), **keyed)
    second = _attempt(attempt_id=UUID(int=3304), **keyed)
    assert should_retry(second, now=NOW, other_attempts=[first]).retry
    other_key = _attempt(
        attempt_id=UUID(int=3305), provider_idempotency_documented=True, provider_idempotency_key="idem-2"
    )
    assert should_retry(second, now=NOW, other_attempts=[other_key]).reasons == ("PRIOR_ATTEMPT_UNRESOLVED",)
    assert should_retry(second, now=NOW, other_attempts=[uncertain]).reasons == ("PRIOR_ATTEMPT_UNRESOLVED",)
    # A definite earlier failure is resolved and does not block a proven retry.
    rejected = _attempt(attempt_id=UUID(int=3306), outcome=SendAttemptOutcome.DEFINITE_REJECTION)
    assert should_retry(proven, now=NOW, other_attempts=[rejected]).retry


def test_retry_is_denied_when_attempts_span_accounts() -> None:
    proven = _attempt(
        outcome=SendAttemptOutcome.PRE_SUBMISSION_FAILURE,
        pre_submission_proof="connection_refused_before_submit",
    )
    elsewhere = proven.model_copy(update={"attempt_id": UUID(int=3401), "sender_binding_id": UUID(int=999)})
    decision = should_retry(proven, now=NOW, other_attempts=[elsewhere])
    assert not decision.retry and decision.reasons == ("DIFFERENT_ACCOUNT_FORBIDDEN",)
    assert decision.sender_binding_id == SENDER_BINDING


def test_queued_to_sending_needs_a_proceed_preflight() -> None:
    with pytest.raises(ValidationFailed) as exc:
        require_transition(InquiryState.QUEUED, InquiryState.SENDING)
    assert exc.value.details["problem"] == "PREFLIGHT_PROCEED_REQUIRED"
    for decision in (
        PreflightDecision(outcome=PreflightOutcome.HOLD, reasons=("RATE_CAP_REACHED",)),
        PreflightDecision(outcome=PreflightOutcome.CANCEL_STALE, target_state=InquiryState.CANCELLED),
    ):
        with pytest.raises(ValidationFailed):
            require_transition(
                InquiryState.QUEUED, InquiryState.SENDING, TransitionContext(preflight=decision)
            )
    proceed = dispatch_preflight(_facts())
    require_transition(InquiryState.QUEUED, InquiryState.SENDING, TransitionContext(preflight=proceed))
    # Leaving the queue without sending needs no preflight.
    require_transition(InquiryState.QUEUED, InquiryState.CANCELLED)


def test_language_must_be_rechecked_at_dispatch() -> None:
    held = dispatch_preflight(_facts(current_language=None))
    assert held.outcome == PreflightOutcome.HOLD and held.reasons == ("LANGUAGE_NOT_RECHECKED",)
    unresolved = dispatch_preflight(_facts(current_language=resolve_inquiry_language(None, [])))
    assert unresolved.target_state == InquiryState.CANCELLED and "LANGUAGE_CHANGED" in unresolved.reasons


def test_suppression_recorded_under_the_merged_identity_suppresses() -> None:
    merged_seller = "seller_entity:" + str(UUID(int=2222))
    merged = IDENTITY.model_copy(update={"seller_key": merged_seller})
    opt_out = SuppressionRecord(
        scope="seller",
        key=merged_seller,
        reason=SuppressionReason.SELLER_OPT_OUT,
        effective_at=NOW - timedelta(minutes=5),
    )
    decision = dispatch_preflight(_facts(identity=merged, suppressions=(opt_out,)))
    assert decision.target_state == InquiryState.SUPPRESSED
    assert decision.suppression_reason == SuppressionReason.SELLER_OPT_OUT
    assert {"SUPPRESSED_SELLER_OPT_OUT", "INQUIRY_IDENTITY_CHANGED"} <= set(decision.reasons)
    cluster_key = VehicleIdentityRef(kind="vehicle_cluster", id=CLUSTER).key()
    in_cluster = IDENTITY.model_copy(
        update={"vehicle": VehicleIdentityRef(kind="vehicle_cluster", id=CLUSTER)}
    )
    vehicle_hold = SuppressionRecord(
        scope="vehicle",
        key=cluster_key,
        reason=SuppressionReason.CONTRADICTORY_AVAILABILITY,
        effective_at=NOW - timedelta(minutes=5),
    )
    by_vehicle = dispatch_preflight(_facts(identity=in_cluster, suppressions=(vehicle_hold,)))
    assert by_vehicle.target_state == InquiryState.SUPPRESSED
    # The same suppression recorded twice is reported once.
    twice = dispatch_preflight(_facts(suppressions=(opt_out, opt_out), identity=merged))
    assert twice.reasons.count("SUPPRESSED_SELLER_OPT_OUT") == 1


def test_binding_requires_a_fresh_readiness_for_the_same_listing_facts() -> None:
    repriced = SNAPSHOT.model_copy(update={"price_amount_minor": 290000, "revision_number": 2})
    with pytest.raises(ValidationFailed) as exc:
        _bind(listing=repriced)
    assert "READINESS_FOR_OTHER_LISTING_FACTS" in exc.value.details["problems"]
    assert _bind(listing=repriced, readiness=readiness(listing_facts=repriced)).qualified_listing == repriced
    with pytest.raises(ValidationFailed) as exc:
        _bind(at=NOW + MAX_READINESS_AGE + timedelta(seconds=1))
    assert exc.value.details["problems"] == ["READINESS_STALE"]
    assert _bind(at=NOW + MAX_READINESS_AGE).binding_hash()
    with pytest.raises(ValidationFailed) as exc:
        _bind(at=NOW - timedelta(seconds=1))
    assert exc.value.details["problems"] == ["READINESS_FROM_FUTURE"]


def test_binding_rejects_a_recipient_verified_on_another_incarnation() -> None:
    other = verify_recipient(_recipient_evidence(listing_incarnation_id=UUID(int=4040)), now=NOW)
    assert other.verified
    with pytest.raises(ValidationFailed) as exc:
        _bind(recipient=other)
    assert "RECIPIENT_LISTING_MISMATCH" in exc.value.details["problems"]
    decision = readiness(recipient=other)
    assert "RECIPIENT_NOT_FOR_THIS_LISTING" in decision.codes()
    assert decision.readiness == InquiryReadiness.NEEDS_TECHNICAL_REVIEW


def test_a_stored_recipient_decision_goes_stale() -> None:
    then = NOW - RECIPIENT_EVIDENCE_MAX_AGE - timedelta(hours=2)
    old = verify_recipient(
        _recipient_evidence(observed_at=then - timedelta(hours=1), verified_at=then),
        now=then + timedelta(hours=1),
    )
    assert old.verified  # fresh when it was decided
    decision = readiness(recipient=old)
    assert decision.readiness == InquiryReadiness.NEEDS_FACTS
    assert severity_of(decision, "RECIPIENT_RECHECK_REQUIRED") == ReadinessSeverity.NEEDS_FACTS
    assert readiness().readiness == InquiryReadiness.INQUIRY_READY


def test_readiness_records_and_checks_the_listing_facts() -> None:
    decision = readiness()
    assert decision.as_of == NOW
    assert decision.evidence["listing"] == SNAPSHOT.model_dump(mode="json")
    sold = readiness(listing_facts=SNAPSHOT.model_copy(update={"availability": Availability.SOLD_CLAIMED}))
    assert sold.readiness == InquiryReadiness.NOT_ELIGIBLE and "VEHICLE_UNAVAILABLE" in sold.codes()
    reserved = readiness(listing_facts=SNAPSHOT.model_copy(update={"availability": Availability.RESERVED}))
    assert "VEHICLE_RESERVED" in reserved.codes()
    with pytest.raises(ValidationError):
        inputs(listing_facts=SNAPSHOT.model_copy(update={"listing_id": UUID(int=404)}))


def test_identity_merge_never_cancels_a_definite_failure() -> None:
    failed = _existing(InquiryState.FAILED_DEFINITE, inquiry_id=UUID(int=1101), reserved_at=NOW)
    queued = _existing(InquiryState.QUEUED, inquiry_id=UUID(int=1102), reserved_at=NOW - timedelta(hours=1))
    result = reconcile_identity_merge([queued, failed])
    assert result.keep == failed.inquiry_id and result.cancel == (queued.inquiry_id,)
    assert not can_transition(InquiryState.FAILED_DEFINITE, InquiryState.CANCELLED)
    accepted = _existing(InquiryState.ACCEPTED, inquiry_id=UUID(int=1103), attempts=1, reserved_at=NOW)
    with_sent = reconcile_identity_merge([failed, accepted, queued])
    assert with_sent.keep == accepted.inquiry_id
    assert failed.inquiry_id not in with_sent.cancel and not with_sent.conflict
    assert with_sent.transmitted_duplicates == ()


def test_an_edited_authorization_record_cancels_queued_work() -> None:
    narrowed = AUTH.model_copy(update={"languages": (MessageLanguage.DE,)})
    assert narrowed.version == AUTH.version and narrowed.fingerprint() != AUTH.fingerprint()
    decision = dispatch_preflight(_facts(authorization=narrowed))
    assert decision.target_state == InquiryState.CANCELLED
    assert decision.reasons == ("AUTHORIZATION_CHANGED",)
