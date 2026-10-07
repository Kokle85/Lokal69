"""Unit tests for domain.seller_contacts (spec 37.3 recipient evidence, 37.5 seller identity).

All addresses, sellers, listings and URLs are SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from suv_deals.domain.enums import SellerType
from suv_deals.domain.seller_contacts import (
    RECIPIENT_EVIDENCE_MAX_AGE,
    AddressEquivalenceEvidence,
    AddressError,
    ContactChange,
    DealerMatchEvidence,
    ExtractionLocation,
    RecipientDecision,
    RecipientEvidence,
    RecipientEvidenceKind,
    RecipientReason,
    RecipientStatus,
    SellerAlias,
    SellerIdentity,
    addresses_equivalent,
    canonicalize_address,
    detect_contact_change,
    seller_identity_key,
    seller_relation,
    verify_recipient,
)

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
LISTING = UUID(int=11)
INCARNATION = UUID(int=12)
REVISION = UUID(int=13)
ENTITY = UUID(int=21)
URL = "https://www.example-marketplace.invalid/listing/12345678"
DEALER_SITE = "https://www.autohaus-example.invalid/kontakt"
RELAY_DOMAIN = "relay.example-marketplace.invalid"


def alias(
    reference: str = "dealer-1",
    kind: str = "marketplace_seller_id",
    source_key: str | None = "fixture_market_de",
    evidence: str = "listing_seller_block",
) -> SellerAlias:
    return SellerAlias(
        alias_kind=kind,  # type: ignore[arg-type]
        source_key=source_key,
        reference=reference,
        evidence_kind=evidence,  # type: ignore[arg-type]
        observed_at=NOW,
    )


def seller(
    *aliases: SellerAlias, entity: UUID | None = None, seller_type: SellerType = SellerType.DEALER
) -> SellerIdentity:
    return SellerIdentity(seller_entity_id=entity, seller_type=seller_type, aliases=aliases or (alias(),))


def evidence(**overrides: Any) -> RecipientEvidence:
    data: dict[str, Any] = {
        "kind": RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
        "address": "Verkauf@Autohaus-Example.INVALID",
        "listing_id": LISTING,
        "listing_incarnation_id": INCARNATION,
        "listing_revision_id": REVISION,
        "listing_revision_number": 3,
        "source_key": "fixture_market_de",
        "listing_reference": "12345678",
        "listing_url": URL,
        "evidence_url": URL,
        "extraction_location": ExtractionLocation.LISTING_CONTACT_BLOCK,
        "extraction_excerpt": "Kontakt: Verkauf@Autohaus-Example.INVALID",
        "distinct_addresses_on_page": 1,
        "seller": seller(),
        "observed_at": NOW - timedelta(hours=2),
        "verified_at": NOW - timedelta(hours=1),
    }
    data.update(overrides)
    return RecipientEvidence(**data)


def relay(**overrides: Any) -> RecipientEvidence:
    data: dict[str, Any] = {
        "kind": RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING,
        "address": f"reply-8f3a@{RELAY_DOMAIN}",
        "extraction_location": ExtractionLocation.LISTING_RELAY_CONTACT,
        "relay_listing_reference": "12345678",
        "extraction_excerpt": None,
    }
    data.update(overrides)
    return evidence(**data)


def dealer(**overrides: Any) -> RecipientEvidence:
    data: dict[str, Any] = {
        "kind": RecipientEvidenceKind.OFFICIAL_DEALER_CONTACT_VIA_LISTING,
        "address": "verkauf@autohaus-example.invalid",
        "extraction_location": ExtractionLocation.DEALER_PAGE_LINKED_FROM_LISTING,
        "evidence_url": DEALER_SITE,
        "extraction_excerpt": "E-Mail: verkauf@autohaus-example.invalid",
        "dealer_match": DealerMatchEvidence(
            reached_via_listing_link=True,
            listing_link_url="https://www.autohaus-example.invalid/",
            matched_identifiers=("vat_id", "business_name"),
        ),
    }
    data.update(overrides)
    return evidence(**data)


def verify(ev: RecipientEvidence, **kwargs: Any) -> RecipientDecision:
    kwargs.setdefault("relay_domains", (RELAY_DOMAIN,))
    return verify_recipient(ev, now=NOW, **kwargs)


# ---------------------------------------------------------------------------------------------
# Address canonicalisation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Verkauf@Autohaus-Example.INVALID", "Verkauf@autohaus-example.invalid"),
        ("John.Doe+cars@GMail.COM", "John.Doe+cars@gmail.com"),  # no Gmail folding, ever
        ("  seller@example.invalid  ", "seller@example.invalid"),
        ("mailto:seller@example.invalid", "seller@example.invalid"),
        ("seller@example.invalid.", "seller@example.invalid"),
        ("o'brien@example.invalid", "o'brien@example.invalid"),
        ("verkauf@autohäuser.de", "verkauf@" + "autohäuser".encode("idna").decode() + ".de"),
    ],
)
def test_canonicalization_is_conservative(raw: str, canonical: str) -> None:
    assert canonicalize_address(raw).canonical == canonical


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("seller@example.invalid\r\nBcc: x@example.invalid", "HEADER_INJECTION"),
        ("seller@example.invalid\n", "HEADER_INJECTION"),
        ("mailto:seller@example.invalid?cc=victim@example.invalid", "MAILTO_PARAMETERS"),
        ("mailto:seller@example.invalid&bcc=x", "MAILTO_PARAMETERS"),
        ("seller.example.invalid", "INVALID_ADDRESS"),
        ("a@b@example.invalid", "INVALID_ADDRESS"),
        ("se ller@example.invalid", "INVALID_ADDRESS"),
        ('"quoted"@example.invalid', "INVALID_ADDRESS"),
        ("seller(comment)@example.invalid", "INVALID_ADDRESS"),
        ("Seller <seller@example.invalid>", "INVALID_ADDRESS"),
        ("seller@[192.0.2.1]", "INVALID_ADDRESS"),
        ("seller@localhost", "INVALID_ADDRESS"),
        ("seller@example.123", "INVALID_ADDRESS"),
        ("seller@-bad.example.invalid", "INVALID_ADDRESS"),
        ("seller@exa..mple.invalid", "INVALID_ADDRESS"),
        ("se..ller@example.invalid", "INVALID_ADDRESS"),
        (".seller@example.invalid", "INVALID_ADDRESS"),
        ("@example.invalid", "INVALID_ADDRESS"),
        ("x" * 65 + "@example.invalid", "INVALID_ADDRESS"),
        ("a@" + "b" * 250 + ".de", "INVALID_ADDRESS"),
        ("verkäufer@example.invalid", "INVALID_ADDRESS"),
        ("", "INVALID_ADDRESS"),
    ],
)
def test_invalid_addresses(raw: str, problem: str) -> None:
    with pytest.raises(AddressError) as exc:
        canonicalize_address(raw)
    assert exc.value.problem == problem
    assert exc.value.details == {"problems": [problem]}


def test_no_equivalence_without_evidence() -> None:
    assert addresses_equivalent("Seller@Example.INVALID", "Seller@example.invalid")
    assert not addresses_equivalent("john.doe@gmail.com", "johndoe@gmail.com")
    assert not addresses_equivalent("john+cars@gmail.com", "john@gmail.com")
    assert not addresses_equivalent("John@example.invalid", "john@example.invalid")
    proof = AddressEquivalenceEvidence(
        first="john.doe@gmail.com",
        second="johndoe@gmail.com",
        kind="provider_documented_alias",
        evidence_ref="synthetic-note-1",
    )
    assert addresses_equivalent(
        "johndoe@gmail.com", "John.Doe@GMAIL.com".replace("John.Doe", "john.doe"), evidence=[proof]
    )
    assert not addresses_equivalent("john@gmail.com", "johndoe@gmail.com", evidence=[proof])


# ---------------------------------------------------------------------------------------------
# Seller identity
# ---------------------------------------------------------------------------------------------


def test_alias_normalization() -> None:
    assert (
        alias("WWW.Autohaus-Example.INVALID", "dealer_website_domain", None).reference
        == "autohaus-example.invalid"
    )
    assert alias("de 123.456-789", "vat_id", None).reference == "DE123456789"
    with pytest.raises(ValidationError):
        alias("not a domain!", "dealer_website_domain", None)
    with pytest.raises(ValidationError):
        alias("dealer-1", "marketplace_seller_id", None)
    with pytest.raises(ValidationError):
        alias("   ", "vat_id", None)
    with pytest.raises(ValidationError):
        seller(alias(), alias())


def test_seller_identity_key() -> None:
    assert seller_identity_key(seller(entity=ENTITY)) == f"seller_entity:{ENTITY}"
    a, b = alias("dealer-1"), alias("dealer-9", source_key="fixture_market_it")
    key_ab = seller_identity_key(seller(a, b))
    assert key_ab == seller_identity_key(seller(b, a))
    assert key_ab.startswith("seller_alias:")
    assert key_ab != seller_identity_key(seller(b))


def test_three_sites_one_seller_entity_one_key() -> None:
    aliases = [
        alias("dealer-1", source_key="fixture_market_de"),
        alias("4711", source_key="fixture_market_ch"),
        alias("autohaus-example.invalid", "dealer_website_domain", None, "same_dealer_website"),
    ]
    keys = {seller_identity_key(seller(a, entity=ENTITY)) for a in aliases}
    assert keys == {f"seller_entity:{ENTITY}"}


def test_seller_relation() -> None:
    a = seller(alias("dealer-1"))
    same_site = seller(alias("dealer-1"), alias("DE1", "vat_id", None, "same_vat_id"))
    assert seller_relation(a, same_site) == "same"
    assert seller_relation(seller(entity=ENTITY), seller(alias("x"), entity=ENTITY)) == "same"
    assert seller_relation(seller(entity=ENTITY), seller(alias("x"), entity=UUID(int=99))) == "different"
    vat_a = seller(alias("a"), alias("DE1", "vat_id", None, "same_vat_id"))
    vat_b = seller(alias("b"), alias("DE2", "vat_id", None, "same_vat_id"))
    assert seller_relation(vat_a, vat_b) == "different"
    assert seller_relation(seller(alias("a")), seller(alias("b"))) == "unknown"


# ---------------------------------------------------------------------------------------------
# verify_recipient
# ---------------------------------------------------------------------------------------------


def test_email_on_advertisement_is_verified_and_bound() -> None:
    decision = verify(evidence())
    assert decision.status == RecipientStatus.VERIFIED
    assert decision.reasons == (RecipientReason.VERIFIED_EMAIL_ON_ADVERTISEMENT,)
    binding = decision.binding
    assert binding is not None
    assert binding.canonical_address == "Verkauf@autohaus-example.invalid"
    assert binding.address_domain == "autohaus-example.invalid"
    assert binding.listing_url == URL and binding.evidence_url == URL
    assert binding.listing_revision_id == REVISION and binding.listing_revision_number == 3
    assert binding.extraction_location == ExtractionLocation.LISTING_CONTACT_BLOCK
    assert binding.seller_identity_key == seller_identity_key(seller())
    assert binding.verified_at == NOW - timedelta(hours=1)
    assert len(binding.fingerprint()) == 64
    assert decision.evidence_excerpt is not None and "@" not in decision.evidence_excerpt


def test_address_in_description_is_accepted() -> None:
    ev = evidence(
        extraction_location=ExtractionLocation.LISTING_DESCRIPTION,
        extraction_excerpt="Fragen gerne an verkauf@autohaus-example.invalid",
        address="verkauf@autohaus-example.invalid",
    )
    assert verify(ev).verified


def test_evidence_url_fragment_does_not_matter_but_page_does() -> None:
    assert verify(evidence(evidence_url=URL + "#contact")).verified
    other_page = verify(evidence(evidence_url="https://www.example-marketplace.invalid/listing/999"))
    assert other_page.status == RecipientStatus.REJECTED
    assert other_page.reasons == (RecipientReason.EVIDENCE_NOT_ON_LISTING,)
    assert verify(evidence(evidence_url=None)).status == RecipientStatus.REJECTED


def test_guessed_info_address_is_rejected() -> None:
    guessed = verify(evidence(address="info@autohaus-example.invalid"))
    assert guessed.status == RecipientStatus.REJECTED
    assert guessed.reasons == (RecipientReason.EXCERPT_DOES_NOT_SHOW_ADDRESS,)
    labelled = verify(evidence(kind=RecipientEvidenceKind.GUESSED_ADDRESS))
    assert labelled.reasons == (RecipientReason.GUESSED_ADDRESS,)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (RecipientEvidenceKind.GENERIC_SEARCH_RESULT, RecipientReason.GENERIC_SEARCH_RESULT),
        (RecipientEvidenceKind.UNRELATED_HARVESTED, RecipientReason.UNRELATED_HARVESTED),
        (RecipientEvidenceKind.GUESSED_ADDRESS, RecipientReason.GUESSED_ADDRESS),
    ],
)
def test_untied_addresses_are_rejected(kind: RecipientEvidenceKind, reason: RecipientReason) -> None:
    decision = verify(evidence(kind=kind))
    assert decision.status == RecipientStatus.REJECTED
    assert decision.reasons == (reason,)
    assert decision.binding is None


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (RecipientEvidenceKind.CONTACT_FORM_ONLY, RecipientReason.CONTACT_FORM_ONLY),
        (RecipientEvidenceKind.CONTACT_REVEAL_RESTRICTED, RecipientReason.CONTACT_REVEAL_RESTRICTED),
        (RecipientEvidenceKind.NO_EMAIL_FOUND, RecipientReason.NO_EMAIL_FOUND),
    ],
)
def test_contact_forms_and_restrictions_mean_email_unavailable(
    kind: RecipientEvidenceKind, reason: RecipientReason
) -> None:
    decision = verify(evidence(kind=kind, address=None))
    assert decision.status == RecipientStatus.SELLER_EMAIL_UNAVAILABLE
    assert decision.reasons == (reason,)


def test_missing_or_invalid_address() -> None:
    assert verify(evidence(address=None)).reasons == (RecipientReason.ADDRESS_MISSING,)
    assert verify(evidence(address="  ")).status == RecipientStatus.SELLER_EMAIL_UNAVAILABLE
    bad = verify(evidence(address="seller@example.invalid\r\nBcc: x@example.invalid"))
    assert bad.status == RecipientStatus.REJECTED
    assert bad.reasons == (RecipientReason.INVALID_ADDRESS,)


def test_non_deliverable_and_own_addresses() -> None:
    noreply = verify(
        evidence(
            address="noreply@example-marketplace.invalid",
            extraction_excerpt="noreply@example-marketplace.invalid",
        )
    )
    assert noreply.reasons == (RecipientReason.NON_DELIVERABLE_ROLE,)
    own = verify(evidence(), sender_addresses=["verkauf@AUTOHAUS-example.invalid", "broken address"])
    assert own.reasons == (RecipientReason.RECIPIENT_IS_SENDER,)


def test_multiple_addresses_or_branches_are_rejected() -> None:
    assert verify(evidence(distinct_addresses_on_page=2)).reasons == (RecipientReason.MULTIPLE_ADDRESSES,)
    assert verify(dealer(branch_count=3)).reasons == (RecipientReason.MULTIPLE_BRANCHES,)
    assert verify(dealer(branch_count=1)).verified


def test_wrong_extraction_location() -> None:
    decision = verify(evidence(extraction_location=ExtractionLocation.OTHER))
    assert decision.reasons == (RecipientReason.WRONG_EXTRACTION_LOCATION,)


def test_relay_bound_to_listing() -> None:
    decision = verify(relay())
    assert decision.verified
    assert decision.reasons == (RecipientReason.VERIFIED_RELAY_FOR_LISTING,)
    assert verify(relay(relay_listing_reference="99999")).reasons == (
        RecipientReason.RELAY_NOT_BOUND_TO_LISTING,
    )
    assert verify(relay(relay_listing_reference=None)).status == RecipientStatus.REJECTED
    unregistered = verify(relay(), relay_domains=())
    assert unregistered.status == RecipientStatus.NEEDS_TECHNICAL_REVIEW
    assert unregistered.reasons == (RecipientReason.RELAY_DOMAIN_UNREGISTERED,)
    other_host = verify(relay(evidence_url="https://other.example.invalid/x"))
    assert other_host.reasons == (RecipientReason.EVIDENCE_NOT_ON_LISTING,)
    wrong_location = verify(relay(extraction_location=ExtractionLocation.LISTING_DESCRIPTION))
    assert wrong_location.reasons == (RecipientReason.WRONG_EXTRACTION_LOCATION,)


def test_three_relays_of_one_seller_share_the_seller_key() -> None:
    keys = set()
    for n, site in enumerate(("fixture_market_de", "fixture_market_it", "fixture_market_ch")):
        decision = verify(
            relay(
                address=f"reply-{n}@{RELAY_DOMAIN}",
                source_key=site,
                seller=seller(alias(f"dealer-{n}", source_key=site), entity=ENTITY),
            )
        )
        assert decision.binding is not None
        keys.add(decision.binding.seller_identity_key)
    assert keys == {f"seller_entity:{ENTITY}"}


def test_official_dealer_contact_via_listing() -> None:
    decision = verify(dealer())
    assert decision.verified
    assert decision.reasons == (RecipientReason.VERIFIED_DEALER_CONTACT_VIA_LISTING,)
    private = verify(dealer(seller=seller(seller_type=SellerType.PRIVATE)))
    assert private.reasons == (RecipientReason.DEALER_SELLER_NOT_DEALER,)
    not_linked = verify(dealer(dealer_match=DealerMatchEvidence(reached_via_listing_link=False)))
    assert not_linked.reasons == (RecipientReason.DEALER_NOT_REACHED_VIA_LISTING,)
    other_site = verify(dealer(evidence_url="https://www.similar-name-dealer.invalid/kontakt"))
    assert other_site.reasons == (RecipientReason.DEALER_NOT_REACHED_VIA_LISTING,)
    no_match = verify(dealer(dealer_match=None))
    assert no_match.reasons == (RecipientReason.DEALER_NOT_REACHED_VIA_LISTING,)
    conflict = verify(
        dealer(
            dealer_match=DealerMatchEvidence(
                reached_via_listing_link=True,
                listing_link_url="https://www.autohaus-example.invalid/",
                matched_identifiers=("business_name",),
                conflicting_identifiers=("vat_id",),
            )
        )
    )
    assert conflict.reasons == (RecipientReason.DEALER_MATCH_CONFLICT,)
    weak = verify(
        dealer(
            dealer_match=DealerMatchEvidence(
                reached_via_listing_link=True,
                listing_link_url="https://www.autohaus-example.invalid/",
                matched_identifiers=("business_name",),
            )
        )
    )
    assert weak.status == RecipientStatus.NEEDS_TECHNICAL_REVIEW
    assert weak.reasons == (RecipientReason.DEALER_MATCH_WEAK,)
    guessed = verify(dealer(address="info@autohaus-example.invalid"))
    assert guessed.reasons == (RecipientReason.EXCERPT_DOES_NOT_SHOW_ADDRESS,)
    wrong_place = verify(dealer(extraction_location=ExtractionLocation.LISTING_CONTACT_BLOCK))
    assert wrong_place.reasons == (RecipientReason.WRONG_EXTRACTION_LOCATION,)


def test_evidence_times() -> None:
    reversed_times = verify(
        evidence(observed_at=NOW - timedelta(hours=1), verified_at=NOW - timedelta(hours=2))
    )
    assert reversed_times.reasons == (RecipientReason.INVALID_EVIDENCE_TIME,)
    future = verify(evidence(verified_at=NOW + timedelta(minutes=1), observed_at=NOW))
    assert future.status == RecipientStatus.NEEDS_TECHNICAL_REVIEW
    stale = verify(
        evidence(
            observed_at=NOW - RECIPIENT_EVIDENCE_MAX_AGE - timedelta(hours=2),
            verified_at=NOW - RECIPIENT_EVIDENCE_MAX_AGE - timedelta(hours=1),
        )
    )
    assert stale.status == RecipientStatus.RECHECK_REQUIRED
    assert RecipientReason.EVIDENCE_STALE in stale.reasons
    assert stale.binding is None


def test_invalid_listing_url_needs_review() -> None:
    decision = verify(
        evidence(
            listing_url="https://user:pw@www.example.invalid/x",
            evidence_url="https://user:pw@www.example.invalid/x",
        )
    )
    assert decision.reasons == (RecipientReason.INVALID_LISTING_URL,)
    assert decision.status == RecipientStatus.NEEDS_TECHNICAL_REVIEW


def test_naive_time_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        evidence(observed_at=datetime(2026, 10, 6, 9, 0))


def test_decision_invariant() -> None:
    with pytest.raises(ValidationError):
        RecipientDecision(status=RecipientStatus.VERIFIED, reasons=())
    good = verify(evidence())
    with pytest.raises(ValidationError):
        RecipientDecision(status=RecipientStatus.REJECTED, reasons=(), binding=good.binding)


# ---------------------------------------------------------------------------------------------
# Material change detection
# ---------------------------------------------------------------------------------------------


def _bound() -> Any:
    decision = verify(evidence())
    assert decision.binding is not None
    return decision.binding


def test_no_change_and_staleness() -> None:
    bound = _bound()
    fresh = detect_contact_change(bound, None, now=NOW)
    assert not fresh.material_change and not fresh.recheck_required and fresh.changes == ()
    later = detect_contact_change(bound, None, now=NOW + RECIPIENT_EVIDENCE_MAX_AGE + timedelta(hours=1))
    assert later.recheck_required and not later.material_change
    assert later.changes == (ContactChange.EVIDENCE_STALE,)


@pytest.mark.parametrize(
    ("overrides", "change"),
    [
        (
            {
                "address": "andere@autohaus-example.invalid",
                "extraction_excerpt": "andere@autohaus-example.invalid",
            },
            ContactChange.ADDRESS_CHANGED,
        ),
        ({"seller": seller(alias("dealer-2"))}, ContactChange.SELLER_CHANGED),
        ({"listing_id": UUID(int=77)}, ContactChange.LISTING_CHANGED),
        ({"listing_incarnation_id": UUID(int=78)}, ContactChange.LISTING_CHANGED),
        ({"listing_url": URL + "?v=2", "evidence_url": URL + "?v=2"}, ContactChange.LISTING_URL_CHANGED),
    ],
)
def test_material_changes(overrides: dict[str, Any], change: ContactChange) -> None:
    current = verify(evidence(**overrides))
    assert current.verified
    result = detect_contact_change(_bound(), current, now=NOW)
    assert result.material_change and result.recheck_required
    assert change in result.changes


def test_kind_change_and_lost_verification_are_material() -> None:
    bound = _bound()
    via_relay = verify(relay())
    assert ContactChange.EVIDENCE_KIND_CHANGED in detect_contact_change(bound, via_relay, now=NOW).changes
    lost = verify(evidence(kind=RecipientEvidenceKind.CONTACT_FORM_ONLY, address=None))
    result = detect_contact_change(bound, lost, now=NOW)
    assert result.material_change
    assert result.changes == (ContactChange.RECIPIENT_NO_LONGER_VERIFIED,)


def test_revision_change_alone_is_not_a_contact_change() -> None:
    current = verify(evidence(listing_revision_number=4, listing_revision_id=UUID(int=14)))
    result = detect_contact_change(_bound(), current, now=NOW)
    assert not result.material_change and not result.recheck_required
    assert result.changes == (ContactChange.REVISION_CHANGED,)


@pytest.mark.parametrize(
    ("excerpt", "shown"),
    [
        ("E-Mail: info@autohaus-example.invalid", True),
        ("E-Mail: info@AUTOHAUS-example.invalid.", True),  # sentence end; domain case-insensitive
        ("(info@autohaus-example.invalid)", True),
        ("E-Mail: sales.info@autohaus-example.invalid", False),  # longer address on the left
        ("E-Mail: my-info@autohaus-example.invalid", False),
        ("E-Mail: info@autohaus-example.invalid.example", False),  # longer domain on the right
        ("E-Mail: info@autohaus-example.invalid-shop.example", False),
        ("E-Mail: INFO@autohaus-example.invalid", False),  # the local part is never case-folded
        ("E-Mail: info [at] autohaus-example.invalid", False),  # obfuscated: not shown, not guessed
    ],
)
def test_excerpt_must_show_exactly_this_address(excerpt: str, shown: bool) -> None:
    decision = verify(evidence(address="info@autohaus-example.invalid", extraction_excerpt=excerpt))
    assert decision.verified == shown
    if not shown:
        assert decision.reasons == (RecipientReason.EXCERPT_DOES_NOT_SHOW_ADDRESS,)
    dealer_decision = verify(dealer(address="info@autohaus-example.invalid", extraction_excerpt=excerpt))
    assert dealer_decision.verified == shown


# ---------------------------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "verkauf@straße.de",  # IDNA 2003 would silently address "strasse.de", another domain
        "verkauf@autohaus-ＥＸＡＭＰＬＥ.de",  # fullwidth compatibility forms  # noqa: RUF001
        "verkauf@autohaus-exámple.de",  # decomposed accent: not the shown label
        "verkauf@ὀδυσσεύς.example",  # final sigma would be folded
    ],
)
def test_idna_mapping_that_changes_the_domain_is_rejected(raw: str) -> None:
    with pytest.raises(AddressError) as exc:
        canonicalize_address(raw)
    assert exc.value.problem == "INVALID_ADDRESS"


def test_idna_labels_that_round_trip_are_encoded() -> None:
    expected = "verkauf@" + "müller-autohaus".encode("idna").decode() + ".de"
    assert canonicalize_address("verkauf@müller-autohaus.de").canonical == expected
    assert canonicalize_address("verkauf@MÜLLER-autohaus.DE").canonical == expected  # case only
    # The IDNA 2008 form of "straße.de" is accepted exactly as shown.
    assert canonicalize_address("verkauf@xn--strae-oqa.de").canonical == "verkauf@xn--strae-oqa.de"


def test_extractor_must_count_the_addresses_on_the_page() -> None:
    data = evidence().model_dump()
    del data["distinct_addresses_on_page"]
    with pytest.raises(ValidationError):
        RecipientEvidence(**data)


def test_stale_current_evidence_holds_for_a_recheck_instead_of_cancelling() -> None:
    old = NOW - RECIPIENT_EVIDENCE_MAX_AGE - timedelta(hours=1)
    stale = verify(evidence(observed_at=old - timedelta(hours=1), verified_at=old))
    assert stale.status == RecipientStatus.RECHECK_REQUIRED
    result = detect_contact_change(_bound(), stale, now=NOW)
    assert not result.material_change and result.recheck_required
    assert result.changes == (ContactChange.EVIDENCE_STALE,)
    # A stale bound recipient plus a stale current decision is still one staleness hold.
    late = NOW + RECIPIENT_EVIDENCE_MAX_AGE + timedelta(hours=2)
    both = detect_contact_change(_bound(), stale, now=late)
    assert both.changes == (ContactChange.EVIDENCE_STALE,) and not both.material_change
