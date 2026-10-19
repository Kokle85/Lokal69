"""Unit tests for domain.due_diligence (spec 19). All listings and evidence are SYNTHETIC."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.due_diligence import (
    DRAFT_MARKER,
    SELLER_QUESTIONS,
    TOPIC_QUESTIONS,
    ChecklistTopic,
    DashboardAction,
    ItemStatus,
    TopicEvidence,
    VerificationKind,
    build_checklist,
    draft_seller_questions,
)
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Co2Cycle,
    Confidence,
    Drive,
    Fuel,
    Gearbox,
    OdometerClaim,
    PriceBasis,
    PriceType,
    Tristate,
    ValuationState,
)
from suv_deals.domain.listings import (
    Co2Info,
    ConditionClaims,
    Documentation,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.errors import ValidationFailed

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
FORBIDDEN_IN_DRAFTS = ("guaranteed", "net profit", "seller confirmed", "verified accident")


def listing(**overrides: Any) -> NormalizedListing:
    data: dict[str, Any] = {
        "source_key": "fixture_dealer_de",
        "source_listing_id": "TEST-204",
        "canonical_url": "https://dealer.example/vehicles/TEST-204",
        "observed_at": NOW,
        "parser_version": "fixture@1.0.0",
        "availability": Availability.AVAILABLE,
        "vehicle": VehicleSpec(
            make="Example",
            model="Trail",
            generation="G2",
            facelift=Tristate.NO,
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            engine_displacement_cm3=1995,
            power_kw=103,
            engine_code="SYN20",
            first_registration=PartialDate(value="2011-05", precision="month"),
            mileage_km=Decimal("187500"),
            mileage_claim=OdometerClaim.SELLER_REPORTED,
        ),
        "price": PriceInfo(
            amount_minor=275000, currency="EUR", basis=PriceBasis.GROSS, type=PriceType.FULL_VEHICLE_ASKING
        ),
    }
    data.update(overrides)
    return NormalizedListing(**data)


def test_checklist_covers_every_spec_19_topic_in_order() -> None:
    checklist = build_checklist(listing(), ValuationState.INCOMPLETE, "adequate")
    assert [i.topic for i in checklist.items] == list(ChecklistTopic)
    assert len(checklist.items) == 13
    for item in checklist.items:
        assert item.question == TOPIC_QUESTIONS[item.topic]
        assert item.status in set(ItemStatus)
    assert checklist.ready is False
    assert checklist.needs_inspection and checklist.needs_documents and checklist.price_confirmation_needed
    assert "cannot certify mechanical condition" in checklist.photo_limitation


def test_price_is_never_confirmed_by_the_listing() -> None:
    item = build_checklist(listing(), None, None).item(ChecklistTopic.AVAILABILITY_PRICE)
    assert item.status == ItemStatus.PRICE_CONFIRMATION_NEEDED
    assert item.actions == (DashboardAction.PRICE_CONFIRMATION_NEEDED,)
    net = listing(
        availability=Availability.RESERVED,
        price=PriceInfo(
            amount_minor=250000,
            currency="EUR",
            basis=PriceBasis.NET,
            type=PriceType.EXPORT_NET,
            refundable_deposit_minor=40000,
            negotiable=Tristate.YES,
        ),
    )
    notes = " | ".join(build_checklist(net, None, None).item(ChecklistTopic.AVAILABILITY_PRICE).notes)
    assert "not shown as available" in notes and "price basis net" in notes
    assert "export_net" in notes and "refundable deposit" in notes and "negotiable" in notes


def test_vin_status() -> None:
    missing = build_checklist(listing(), None, None).item(ChecklistTopic.VIN)
    assert missing.status == ItemStatus.NEEDS_DOCUMENTS
    present = build_checklist(listing(documentation=Documentation(vin="WVWZZZ1JZXW000001")), None, None).item(
        ChecklistTopic.VIN
    )
    assert present.status == ItemStatus.SELLER_CLAIM_ONLY
    assert DashboardAction.NEEDS_DOCUMENTS in present.actions
    assert any("third-party AI" in n for n in present.notes)


def test_spec_match_depends_on_known_spec_and_comparables() -> None:
    ok = build_checklist(listing(), None, "adequate").item(ChecklistTopic.SPEC_MATCH)
    assert ok.status == ItemStatus.SELLER_CLAIM_ONLY
    no_comparables = build_checklist(listing(), None, "insufficient_comparables").item(
        ChecklistTopic.SPEC_MATCH
    )
    assert no_comparables.status == ItemStatus.UNKNOWN
    vague = listing(vehicle=VehicleSpec(make="Example", model="Trail"))
    unknown = build_checklist(vague, None, "adequate").item(ChecklistTopic.SPEC_MATCH)
    assert unknown.status == ItemStatus.UNKNOWN
    assert "generation" in unknown.notes[0] and "drive" in unknown.notes[0]


@pytest.mark.parametrize(
    ("claim", "status"),
    [
        (OdometerClaim.VERIFIED, ItemStatus.ANSWERED_BY_EVIDENCE),
        (OdometerClaim.DOCUMENTED, ItemStatus.SELLER_CLAIM_ONLY),
        (OdometerClaim.SELLER_REPORTED, ItemStatus.NEEDS_DOCUMENTS),
        (OdometerClaim.CONFLICTING, ItemStatus.NEEDS_DOCUMENTS),
        (OdometerClaim.UNKNOWN, ItemStatus.NEEDS_DOCUMENTS),
    ],
)
def test_odometer_records(claim: OdometerClaim, status: ItemStatus) -> None:
    base = listing()
    changed = base.model_copy(update={"vehicle": base.vehicle.model_copy(update={"mileage_claim": claim})})
    assert build_checklist(changed, None, None).item(ChecklistTopic.ODOMETER_RECORDS).status == status


def test_condition_topics_need_inspection_and_quote_faults_safely() -> None:
    faulty = listing(
        condition=ConditionClaims(
            accident_free=ClaimStatus.SELLER_CLAIMED,
            running=ClaimStatus.SELLER_DENIED,
            mechanical_faults=("Synthetic: DPF warning; call +49 171 1234567 **now**",),
        )
    )
    checklist = build_checklist(faulty, ValuationState.ESTIMATED, "adequate")
    mech = checklist.item(ChecklistTopic.MECHANICAL_FAULTS)
    assert mech.status == ItemStatus.NEEDS_INSPECTION
    assert any("DPF warning" in n for n in mech.notes)
    assert not any("1234567" in n or "*" in n for n in mech.notes)
    accident = checklist.item(ChecklistTopic.ACCIDENT_DAMAGE)
    assert accident.status == ItemStatus.SELLER_CLAIM_ONLY  # a seller claim is not an inspection
    assert DashboardAction.NEEDS_INSPECTION in accident.actions
    running = checklist.item(ChecklistTopic.RUNNING_TRANSPORT)
    assert running.status == ItemStatus.NEEDS_INSPECTION
    assert any("non-running surcharges" in n for n in running.notes)
    wear = checklist.item(ChecklistTopic.WEAR_ITEMS)
    assert wear.status == ItemStatus.NEEDS_INSPECTION and "repair estimate" in wear.notes[0]


@pytest.mark.parametrize(
    ("accident", "damaged", "status"),
    [
        (ClaimStatus.VERIFIED, ClaimStatus.UNKNOWN, ItemStatus.ANSWERED_BY_EVIDENCE),
        (ClaimStatus.SELLER_CLAIMED, ClaimStatus.SELLER_CLAIMED, ItemStatus.NEEDS_INSPECTION),
        (ClaimStatus.SELLER_DENIED, ClaimStatus.UNKNOWN, ItemStatus.NEEDS_INSPECTION),
        (ClaimStatus.CONFLICTING, ClaimStatus.UNKNOWN, ItemStatus.NEEDS_INSPECTION),
        (ClaimStatus.UNKNOWN, ClaimStatus.UNKNOWN, ItemStatus.UNKNOWN),
    ],
)
def test_accident_statuses(accident: ClaimStatus, damaged: ClaimStatus, status: ItemStatus) -> None:
    item = build_checklist(
        listing(condition=ConditionClaims(accident_free=accident, damaged_vehicle=damaged)), None, None
    ).item(ChecklistTopic.ACCIDENT_DAMAGE)
    assert item.status == status


def test_documents_co2_and_inspection() -> None:
    documented = listing(
        documentation=Documentation(
            registration_documents=ClaimStatus.VERIFIED,
            coc_available=ClaimStatus.VERIFIED,
            inspection_expiry=PartialDate(value="2027-03", precision="month"),
            origin_evidence="Synthetic: EUR.1 mentioned by seller",
        ),
        co2=Co2Info(g_per_km=Decimal("189"), cycle=Co2Cycle.NEDC),
    )
    checklist = build_checklist(documented, None, None)
    assert checklist.item(ChecklistTopic.REGISTRATION_EXPORT_DOCS).status == ItemStatus.ANSWERED_BY_EVIDENCE
    co2 = checklist.item(ChecklistTopic.CO2_ORIGIN)
    assert co2.status == ItemStatus.NEEDS_DOCUMENTS
    assert any("seller-stated" in n for n in co2.notes)
    assert any("does not prove preferential origin" in n for n in co2.notes)
    inspection = checklist.item(ChecklistTopic.INSPECTION)
    assert inspection.status == ItemStatus.SELLER_CLAIM_ONLY and "2027-03" in inspection.notes[0]
    unknown_cycle = build_checklist(listing(), None, None).item(ChecklistTopic.CO2_ORIGIN)
    assert any("never converted between NEDC and WLTP" in n for n in unknown_cycle.notes)


def test_buyer_side_topics_never_answered_by_listing() -> None:
    checklist = build_checklist(listing(), ValuationState.QUOTE_SUPPORTED, "adequate")
    assert checklist.item(ChecklistTopic.EXPORT_PLATES_INSURANCE).status == ItemStatus.UNKNOWN
    ownership = checklist.item(ChecklistTopic.OWNERSHIP_PAYMENT)
    assert ownership.status == ItemStatus.NEEDS_DOCUMENTS
    assert any("independently" in n for n in ownership.notes)


def test_qualifying_evidence_answers_topics() -> None:
    evidence = [
        TopicEvidence(
            topic=ChecklistTopic.VIN,
            evidence_id=UUID(int=1),
            kind=VerificationKind.DOCUMENT,
            confidence=Confidence.HIGH,
        ),
        TopicEvidence(
            topic=ChecklistTopic.MECHANICAL_FAULTS,
            evidence_id=UUID(int=2),
            kind=VerificationKind.INSPECTION,
            confidence=Confidence.MEDIUM,
        ),
    ]
    checklist = build_checklist(listing(), None, None, evidence=evidence)
    vin = checklist.item(ChecklistTopic.VIN)
    assert vin.status == ItemStatus.ANSWERED_BY_EVIDENCE and vin.evidence_ids == (UUID(int=1),)
    assert vin.actions == ()
    assert checklist.item(ChecklistTopic.MECHANICAL_FAULTS).status == ItemStatus.ANSWERED_BY_EVIDENCE


def test_photos_never_certify_condition() -> None:
    photo = TopicEvidence(
        topic=ChecklistTopic.ACCIDENT_DAMAGE,
        evidence_id=UUID(int=3),
        kind=VerificationKind.PHOTO_OBSERVATION,
        confidence=Confidence.HIGH,
        note="Synthetic: no visible dents on the left side",
    )
    item = build_checklist(listing(), None, None, evidence=[photo]).item(ChecklistTopic.ACCIDENT_DAMAGE)
    assert item.status != ItemStatus.ANSWERED_BY_EVIDENCE
    assert DashboardAction.NEEDS_INSPECTION in item.actions
    assert any("photo observation" in n and "not a certification" in n for n in item.notes)
    assert item.evidence_ids == (UUID(int=3),)


def test_document_cannot_certify_condition_and_low_confidence_does_not_answer() -> None:
    doc = TopicEvidence(
        topic=ChecklistTopic.RUNNING_TRANSPORT,
        evidence_id=UUID(int=4),
        kind=VerificationKind.DOCUMENT,
        confidence=Confidence.HIGH,
    )
    weak = TopicEvidence(
        topic=ChecklistTopic.VIN,
        evidence_id=UUID(int=5),
        kind=VerificationKind.DOCUMENT,
        confidence=Confidence.LOW,
    )
    seller_doc_for_ownership = TopicEvidence(
        topic=ChecklistTopic.OWNERSHIP_PAYMENT,
        evidence_id=UUID(int=6),
        kind=VerificationKind.DOCUMENT,
        confidence=Confidence.HIGH,
    )
    checklist = build_checklist(listing(), None, None, evidence=[doc, weak, seller_doc_for_ownership])
    assert checklist.item(ChecklistTopic.RUNNING_TRANSPORT).status != ItemStatus.ANSWERED_BY_EVIDENCE
    assert checklist.item(ChecklistTopic.VIN).status != ItemStatus.ANSWERED_BY_EVIDENCE
    assert checklist.item(ChecklistTopic.OWNERSHIP_PAYMENT).status != ItemStatus.ANSWERED_BY_EVIDENCE


def test_ready_only_when_every_topic_answered() -> None:
    kinds = {
        topic: (
            VerificationKind.OWNER_VERIFIED
            if topic == ChecklistTopic.OWNERSHIP_PAYMENT
            else VerificationKind.INSPECTION
        )
        for topic in ChecklistTopic
    }
    evidence = [
        TopicEvidence(topic=t, evidence_id=UUID(int=100 + i), kind=k, confidence=Confidence.HIGH)
        for i, (t, k) in enumerate(kinds.items())
    ]
    checklist = build_checklist(listing(), ValuationState.ESTIMATED, "adequate", evidence=evidence)
    assert checklist.ready
    assert not (
        checklist.needs_inspection or checklist.needs_documents or checklist.price_confirmation_needed
    )


# --------------------------------------------------------------------------------------------- drafts


@pytest.mark.parametrize("language", ["de", "it", "en"])
def test_seller_question_drafts_are_marked_and_never_sent(language: str) -> None:
    draft = draft_seller_questions(listing(), language)  # type: ignore[arg-type]
    assert draft.status == "draft_not_sent"
    assert draft.requires_owner_approval is True
    assert draft.text.startswith(f"[{DRAFT_MARKER}]")
    assert "requires owner approval to send" in draft.text
    assert DRAFT_MARKER not in draft.body
    assert ChecklistTopic.EXPORT_PLATES_INSURANCE not in draft.topics
    assert len(draft.topics) == 12
    for topic in draft.topics:
        assert SELLER_QUESTIONS[topic][language] in draft.body
    assert "TEST-204" in draft.body
    lower = draft.text.lower()
    assert not any(p in lower for p in FORBIDDEN_IN_DRAFTS)


def test_draft_language_specific_text() -> None:
    assert draft_seller_questions(listing(), "de").body.startswith("Guten Tag,")
    assert draft_seller_questions(listing(), "it").body.startswith("Buongiorno,")
    assert "ENTWURF" in draft_seller_questions(listing(), "de").text
    with pytest.raises(ValidationFailed):
        draft_seller_questions(listing(), "fr")  # type: ignore[arg-type]


def test_draft_skips_answered_topics_and_respects_selection() -> None:
    evidence = [
        TopicEvidence(
            topic=ChecklistTopic.VIN,
            evidence_id=UUID(int=1),
            kind=VerificationKind.DOCUMENT,
            confidence=Confidence.HIGH,
        )
    ]
    checklist = build_checklist(listing(), None, None, evidence=evidence)
    draft = draft_seller_questions(listing(), "en", checklist=checklist)
    assert ChecklistTopic.VIN not in draft.topics
    selected = draft_seller_questions(
        listing(), "en", topics=[ChecklistTopic.AVAILABILITY_PRICE, ChecklistTopic.CO2_ORIGIN]
    )
    assert selected.topics == (ChecklistTopic.AVAILABILITY_PRICE, ChecklistTopic.CO2_ORIGIN)
    assert "1. Is the vehicle still available" in selected.body


def test_draft_listing_reference_is_sanitised() -> None:
    hostile = listing(source_listing_id="ID-1 [click](http://evil.example) **x**")
    draft = draft_seller_questions(hostile, "en")
    assert "evil.example" not in draft.text and "**" not in draft.text and "[click]" not in draft.text


def test_every_seller_question_has_all_languages() -> None:
    for topic, texts in SELLER_QUESTIONS.items():
        assert set(texts) == {"de", "it", "en"}, topic
        for text in texts.values():
            assert text.endswith("?")


def test_draft_rejects_checklist_of_another_listing() -> None:
    other = listing(source_listing_id="TEST-999")
    with pytest.raises(ValidationFailed):
        draft_seller_questions(listing(), "en", checklist=build_checklist(other, None, None))
    own = build_checklist(listing(), None, None)
    assert draft_seller_questions(listing(), "en", checklist=own).requires_owner_approval is True
