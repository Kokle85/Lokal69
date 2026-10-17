"""Unit tests for domain.identity (spec 10, 31: Identity and Revisions rows).

All listings, URLs (``*.example``), IDs and VINs are SYNTHETIC test data, not real vehicles.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from suv_deals.domain.enums import Availability, Confidence, Drive, Fuel, Gearbox, Precision
from suv_deals.domain.identity import (
    CARD_HASH_FIELDS,
    DetailObservation,
    IdentityConflictCode,
    ListingCurrentState,
    PromotionOutcome,
    canonicalize_url,
    card_hash,
    compare_identity,
    decide_promotion,
    detect_identity_conflict,
    identity_from,
    ingestion_key,
    is_vin_format_valid,
    merge_seen_window,
    needs_url_alias,
    normalize_vin,
    possible_same_vehicle,
    vin_check_digit_status,
)
from suv_deals.domain.listings import (
    Documentation,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.domain.taxonomy import default_taxonomy
from suv_deals.errors import ValidationFailed

TRACKING = ("utm_source", "utm_medium", "gclid", "fbclid", "ref")
OBSERVED = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
# Synthetic VINs. VALID_NA_VIN is the textbook ISO 3779 example with check digit 'X'.
VALID_NA_VIN = "1M8GDM9AXKP042788"
SYNTHETIC_EU_VIN = "WVWZZZ5NZBW000001"  # synthetic; position 9 'Z' is not a check digit
OTHER_EU_VIN = "WVWZZZ5NZBW000002"


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def uid(n: int) -> UUID:
    return UUID(int=n)


def make_listing(**overrides: Any) -> NormalizedListing:
    vehicle_overrides = overrides.pop("vehicle", {})
    vehicle_data: dict[str, Any] = {
        "make": "Volkswagen",
        "model": "Tiguan",
        "first_registration": PartialDate(value="2011-05", precision=Precision.MONTH),
        "fuel": Fuel.DIESEL,
        "gearbox": Gearbox.MANUAL,
        "drive": Drive.AWD,
        "power_kw": 103,
        "engine_displacement_cm3": 1968,
        "mileage_km": Decimal("187500"),
    }
    vehicle_data.update(vehicle_overrides)
    vehicle = VehicleSpec(**vehicle_data)
    data: dict[str, Any] = {
        "source_key": "fixture_dealer_de",
        "source_listing_id": "SYNTH-1",
        "canonical_url": "https://dealer.example/vehicles/SYNTH-1",
        "observed_at": OBSERVED,
        "vehicle": vehicle,
        "price": PriceInfo(amount_minor=275000, currency="EUR"),
        "parser_version": "fixture@1.0.0",
    }
    data.update(overrides)
    return NormalizedListing(**data)


# ---------------------------------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Dealer.EXAMPLE/vehicles/ABC", "https://dealer.example/vehicles/ABC"),
        ("HTTPS://dealer.example:443/vehicles/ABC", "https://dealer.example/vehicles/ABC"),
        ("http://dealer.example:80/x", "http://dealer.example/x"),
        ("http://dealer.example:8080/x", "http://dealer.example:8080/x"),
        ("https://dealer.example/vehicles/ABC#gallery", "https://dealer.example/vehicles/ABC"),
        ("https://dealer.example", "https://dealer.example/"),
        (
            "https://dealer.example/d?utm_source=x&id=42&utm_campaign=y&gclid=z&lang=de",
            "https://dealer.example/d?id=42&lang=de",
        ),
        ("https://dealer.example/d?b=2&a=1", "https://dealer.example/d?b=2&a=1"),  # order preserved
        ("https://dealer.example/d?id=A%2FB&x=1", "https://dealer.example/d?id=A%2FB&x=1"),  # encoding kept
        ("https://dealer.example/d?UTM_Medium=x&id=1", "https://dealer.example/d?id=1"),
        ("https://dealer.example/d?ref=home&id=1&&", "https://dealer.example/d?id=1"),
        ("https://dealer.example/d?utm_source=x", "https://dealer.example/d"),
        ("https://dealer.example/Vehicles/AbC", "https://dealer.example/Vehicles/AbC"),  # path case kept
        ("https://bücher.example/x", "https://xn--bcher-kva.example/x"),
        ("https://dealer.example./x", "https://dealer.example/x"),
        ("https://[2001:DB8::1]:443/x", "https://[2001:db8::1]/x"),
        ("https://dealer.example/d?reference=7", "https://dealer.example/d?reference=7"),  # not 'ref'
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url, TRACKING) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://dealer.example/x",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:secret@dealer.example/x",
        "https://user@dealer.example/x",
        "https:///nohost",
        "https://dealer.example:99999/x",
        "https://dealer.example/a b",
        "",
        "   ",
        "https://dealer.example/" + "a" * 2100,
        "https://dealer.example:0/x",  # port 0 is not a usable port
        "https://dealer%2Eexample/x",  # percent-encoded host
        # IDNA 2003 nameprep would silently rewrite these into a *different* host
        # (straße -> strasse, full-width letters -> ASCII); they are refused, never rewritten.
        "https://straße.example/x",
        "https://\uff44ealer.example/x",  # full-width "d"
    ],
)
def test_canonicalize_url_rejects(url: str) -> None:
    with pytest.raises(ValidationFailed):
        canonicalize_url(url, TRACKING)


def test_canonicalize_url_does_not_drop_unlisted_params() -> None:
    url = "https://dealer.example/search?listingId=99&page=2"
    assert canonicalize_url(url, ()) == url


def test_rejection_message_does_not_leak_credentials() -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        canonicalize_url("https://bob:hunter2@dealer.example/x", TRACKING)
    assert "hunter2" not in excinfo.value.message


# ---------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------


def test_identity_prefers_provider_id() -> None:
    identity = identity_from(
        "fixture_dealer_de", " SYNTH-204 ", "https://dealer.example/v/204?utm_source=x", TRACKING
    )
    assert identity.identity_method == "provider_id"
    assert identity.source_listing_id == "SYNTH-204"
    assert identity.identity_material == "fixture_dealer_de:SYNTH-204"
    assert identity.identity_hash == h("fixture_dealer_de:SYNTH-204")
    assert identity.canonical_url == "https://dealer.example/v/204"


def test_identity_falls_back_to_canonical_url() -> None:
    identity = identity_from("fixture_dealer_de", None, "https://dealer.example/v/204?gclid=abc", TRACKING)
    assert identity.identity_method == "canonical_url"
    assert identity.identity_material == "fixture_dealer_de:https://dealer.example/v/204"
    assert identity.identity_hash == h(identity.identity_material)
    assert identity.source_listing_id == "urlsha256:" + identity.identity_hash


def test_identity_stable_under_tracking_parameters() -> None:
    a = identity_from("s1", None, "https://dealer.example/v/1?utm_source=a&id=7", TRACKING)
    b = identity_from("s1", None, "https://DEALER.example/v/1?id=7&fbclid=q#top", TRACKING)
    assert a.identity_hash == b.identity_hash


def test_identity_is_source_scoped() -> None:
    a = identity_from("source_a", "SYNTH-1", "https://a.example/1", TRACKING)
    b = identity_from("source_b", "SYNTH-1", "https://b.example/1", TRACKING)
    assert a.identity_hash != b.identity_hash


def test_identity_blank_provider_id_uses_url() -> None:
    assert identity_from("s1", "   ", "https://a.example/1", TRACKING).identity_method == "canonical_url"


@pytest.mark.parametrize(
    ("source_key", "provider_id"),
    [("", "X"), ("bad key", "X"), ("a:b", "X"), ("s1", "x" * 201), ("s1", "bad\x00id")],
)
def test_identity_rejects_bad_inputs(source_key: str, provider_id: str) -> None:
    with pytest.raises(ValidationFailed):
        identity_from(source_key, provider_id, "https://a.example/1", TRACKING)


def test_compare_identity_paths() -> None:
    identity = identity_from("s1", "SYNTH-1", "https://a.example/1", TRACKING)
    assert compare_identity(identity.identity_material, identity.identity_hash, identity) == "same"
    other = identity_from("s1", "SYNTH-2", "https://a.example/2", TRACKING)
    assert compare_identity(other.identity_material, other.identity_hash, identity) == "different"
    # Simulated hash collision: equal hash, different material -> quarantine, never merge.
    assert compare_identity("s1:SYNTH-OTHER", identity.identity_hash, identity) == "hash_collision"
    # Corrupt stored hash for identical material is also refused.
    assert compare_identity(identity.identity_material, "0" * 64, identity) == "hash_collision"


def test_url_alias_needed_when_provider_url_changes() -> None:
    old = identity_from("s1", "SYNTH-1", "https://a.example/old-slug", TRACKING)
    new = identity_from("s1", "SYNTH-1", "https://a.example/new-slug", TRACKING)
    assert needs_url_alias(old, new)
    assert not needs_url_alias(old, old)
    url_based = identity_from("s1", None, "https://a.example/new-slug", TRACKING)
    assert not needs_url_alias(old, url_based)


# ---------------------------------------------------------------------------------------------
# Card hash and ingestion key
# ---------------------------------------------------------------------------------------------

CARD: dict[str, str | None] = {
    "source_listing_id": "SYNTH-1",
    "canonical_url": "https://dealer.example/vehicles/SYNTH-1",
    "title": "VW Tiguan 2.0 TDI",
    "price_minor": "275000",
    "currency": "EUR",
    "mileage_km": "187500",
    "source_modified_at": None,
}


def test_card_hash_ignores_promotion_badges_and_views() -> None:
    base_hash, material = card_hash(CARD)
    noisy = {**CARD, "position": "1", "badges": "TOP", "view_count": "999", "promoted": "true"}
    assert card_hash(noisy)[0] == base_hash
    assert set(material) == set(CARD_HASH_FIELDS)


def test_card_hash_normalises_cosmetics() -> None:
    cosmetic = {
        **CARD,
        "title": "  VW\u00a0Tiguan   2.0 TDI ",
        "mileage_km": "187500.000",
        "currency": "eur",
    }
    assert card_hash(cosmetic)[0] == card_hash(CARD)[0]


def test_card_hash_missing_and_empty_fields_equal_none() -> None:
    sparse = {k: v for k, v in CARD.items() if k != "source_modified_at"}
    assert card_hash(sparse)[0] == card_hash(CARD)[0]
    assert card_hash({**CARD, "title": "   "})[1]["title"] is None


@pytest.mark.parametrize(
    ("field", "value"), [("price_minor", "275001"), ("mileage_km", "187600"), ("title", "VW Tiguan 2.0 TSI")]
)
def test_card_hash_changes_on_meaningful_fields(field: str, value: str) -> None:
    assert card_hash({**CARD, field: value})[0] != card_hash(CARD)[0]


@pytest.mark.parametrize(
    ("field", "value"), [("price_minor", "12.5"), ("price_minor", "abc"), ("mileage_km", "-5")]
)
def test_card_hash_rejects_bad_numbers(field: str, value: str) -> None:
    with pytest.raises(ValidationFailed):
        card_hash({**CARD, field: value})


def test_ingestion_key_deterministic_and_distinct() -> None:
    ch = card_hash(CARD)[0]
    run = uid(7)
    key = ingestion_key("s1", run, 1, "SYNTH-1", ch)
    assert key == ingestion_key("s1", str(run), 1, "SYNTH-1", ch)  # replay -> same key
    assert key != ingestion_key("s1", run, 2, "SYNTH-1", ch)
    assert key != ingestion_key("s1", uid(8), 1, "SYNTH-1", ch)
    assert key != ingestion_key("s1", run, 1, None, ch)
    assert len(key) == 64


@pytest.mark.parametrize(("page", "digest"), [(0, "a" * 64), (1, "XYZ"), (1, "A" * 64)])
def test_ingestion_key_validates(page: int, digest: str) -> None:
    with pytest.raises(ValidationFailed):
        ingestion_key("s1", "run", page, "x", digest)


def test_merge_seen_window() -> None:
    t0 = OBSERVED
    first, last = merge_seen_window(None, None, t0)
    assert first == last == t0
    first, last = merge_seen_window(t0, t0, t0 - timedelta(hours=1))  # late, older observation
    assert first == t0 - timedelta(hours=1)
    assert last == t0  # greatest(existing, observed): never moves backwards
    first, last = merge_seen_window(t0, t0, t0 + timedelta(hours=1))
    assert (first, last) == (t0, t0 + timedelta(hours=1))


# ---------------------------------------------------------------------------------------------
# Revision promotion (spec 10 out-of-order observations)
# ---------------------------------------------------------------------------------------------

HASH_A, HASH_B = h("A"), h("B")


def obs(
    generation: int, observation: int, digest: str, availability: Availability = Availability.AVAILABLE
) -> DetailObservation:
    return DetailObservation(
        generation=generation,
        observation_id=uid(observation),
        semantic_hash=digest,
        availability=availability,
        observed_at=OBSERVED,
    )


def apply(
    state: ListingCurrentState, incoming: DetailObservation
) -> tuple[ListingCurrentState, PromotionOutcome]:
    """Apply a decision the way the persistence layer must: write state only if told to."""
    decision = decide_promotion(state, incoming)
    if not decision.update_listing_state:
        assert decision.current_generation == state.current_generation
        assert decision.accepted_observation_id == state.accepted_observation_id
        assert decision.current_semantic_hash == state.current_semantic_hash
        assert decision.revision_number == state.revision_number
        return state, decision.outcome
    new_state = ListingCurrentState(
        current_generation=decision.current_generation,
        accepted_observation_id=decision.accepted_observation_id,
        current_semantic_hash=decision.current_semantic_hash,
        revision_number=decision.revision_number,
        availability=decision.availability,
    )
    return new_state, decision.outcome


def test_first_observation_creates_revision_one() -> None:
    decision = decide_promotion(ListingCurrentState(), obs(1, 10, HASH_A))
    assert decision.outcome == PromotionOutcome.PROMOTE_NEW_REVISION
    assert decision.revision_number == 1
    assert decision.create_revision and decision.refresh_last_detail_success


def test_a_b_a_price_reversion_creates_new_revisions() -> None:
    state = ListingCurrentState()
    outcomes = []
    for gen, digest in ((1, HASH_A), (2, HASH_B), (3, HASH_A)):
        state, outcome = apply(state, obs(gen, gen * 10, digest))
        outcomes.append(outcome)
    assert outcomes == [PromotionOutcome.PROMOTE_NEW_REVISION] * 3
    assert state.revision_number == 3
    assert state.current_semantic_hash == HASH_A


def test_unchanged_newer_generation_confirms_only() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    decision = decide_promotion(state, obs(2, 20, HASH_A))
    assert decision.outcome == PromotionOutcome.CONFIRM_UNCHANGED
    assert not decision.create_revision
    assert decision.refresh_last_detail_success
    assert decision.revision_number == 1
    assert decision.current_generation == 2


def test_late_older_generation_never_regresses() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    state, _ = apply(state, obs(3, 30, HASH_B, Availability.REMOVED))  # newer: unavailable
    decision = decide_promotion(
        state, obs(2, 20, HASH_A, Availability.AVAILABLE)
    )  # late, after lease recovery
    assert decision.outcome == PromotionOutcome.HISTORICAL_ONLY
    assert decision.store_historical_evidence
    assert not decision.promote_current and not decision.create_revision
    assert decision.availability == Availability.REMOVED
    assert decision.current_semantic_hash == HASH_B
    assert decision.revision_number == state.revision_number
    assert decision.current_generation == 3


def test_duplicate_replay_is_noop() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    decision = decide_promotion(state, obs(1, 10, HASH_A))
    assert decision.outcome == PromotionOutcome.DUPLICATE_REPLAY
    assert not decision.store_historical_evidence
    assert not decision.incident
    assert decision.revision_number == 1


def test_equivalent_retry_same_generation_tie_breaks_to_lower_id() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 20, HASH_A))
    decision = decide_promotion(state, obs(1, 10, HASH_A))
    assert decision.outcome == PromotionOutcome.DUPLICATE_REPLAY
    assert decision.accepted_observation_id == uid(10)
    assert decision.store_historical_evidence
    assert not decision.incident


def test_replayed_id_with_different_payload_is_incident() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    decision = decide_promotion(state, obs(1, 10, HASH_B))
    assert decision.outcome == PromotionOutcome.INCIDENT_CONFLICTING_REPLAY
    assert decision.incident_code == "REPLAY_PAYLOAD_MISMATCH"
    assert not decision.promote_current
    assert decision.current_semantic_hash == HASH_A


def test_conflicting_same_generation_lower_incoming_id_wins() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 20, HASH_A))
    decision = decide_promotion(state, obs(1, 10, HASH_B))
    assert decision.outcome == PromotionOutcome.INCIDENT_CONFLICTING_REPLAY
    assert decision.incident
    assert decision.promote_current and decision.create_revision
    assert decision.accepted_observation_id == uid(10)
    assert decision.superseded_observation_id == uid(20)
    assert decision.revision_number == 2


def test_conflicting_same_generation_higher_incoming_id_loses() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    decision = decide_promotion(state, obs(1, 20, HASH_B))
    assert decision.outcome == PromotionOutcome.INCIDENT_CONFLICTING_REPLAY
    assert decision.incident_code == "CONFLICTING_SAME_GENERATION"
    assert not decision.promote_current
    assert decision.accepted_observation_id == uid(10)
    assert decision.current_semantic_hash == HASH_A


def test_update_listing_state_flag() -> None:
    state, _ = apply(ListingCurrentState(), obs(1, 20, HASH_A))
    assert decide_promotion(state, obs(2, 30, HASH_A)).update_listing_state  # confirm: new generation
    assert decide_promotion(state, obs(2, 30, HASH_B)).update_listing_state  # new revision
    assert not decide_promotion(state, obs(1, 20, HASH_A)).update_listing_state  # pure replay
    assert not decide_promotion(state, obs(1, 30, HASH_A)).update_listing_state  # higher-ID retry
    lower_retry = decide_promotion(state, obs(1, 10, HASH_A))
    assert lower_retry.update_listing_state and not lower_retry.promote_current
    assert lower_retry.accepted_observation_id == uid(10)
    assert not decide_promotion(state, obs(1, 20, HASH_B)).update_listing_state  # payload mismatch
    assert not decide_promotion(state, obs(1, 30, HASH_B)).update_listing_state  # loses tie-break
    state, _ = apply(state, obs(3, 40, HASH_B))
    assert not decide_promotion(state, obs(2, 35, HASH_A)).update_listing_state  # late, older


def test_unchanged_html_with_same_semantic_content_only_confirms() -> None:
    # Cosmetic/HTML-only differences (raw text, provenance, parser version, observation time) do not
    # change NormalizedListing.semantic_hash, so a newer generation confirms without a revision.
    first = make_listing()
    cosmetic = make_listing(
        observed_at=OBSERVED + timedelta(hours=6),
        parser_version="fixture@1.0.1",
        price=PriceInfo(amount_minor=275000, currency="EUR", raw_text="2.750,- EUR (neu formatiert)"),
        title="VW  Tiguan -- TOP",
    )
    assert first.semantic_hash() == cosmetic.semantic_hash()
    state, _ = apply(ListingCurrentState(), obs(1, 10, first.semantic_hash()))
    decision = decide_promotion(state, obs(2, 20, cosmetic.semantic_hash()))
    assert decision.outcome == PromotionOutcome.CONFIRM_UNCHANGED
    assert not decision.create_revision
    changed = make_listing(price=PriceInfo(amount_minor=265000, currency="EUR"))
    assert decide_promotion(state, obs(2, 20, changed.semantic_hash())).create_revision


@pytest.mark.parametrize(
    "payloads",
    [
        {10: HASH_A, 20: HASH_B, 30: HASH_A},
        {10: HASH_B, 20: HASH_A, 30: HASH_A},
        {10: HASH_A, 20: HASH_A, 30: HASH_B},
        {15: HASH_B, 20: HASH_A, 30: HASH_B, 40: h("C")},
    ],
)
def test_same_generation_outcome_is_independent_of_completion_order(payloads: dict[int, str]) -> None:
    """Spec 10 deterministic tie-break: whatever order retries of one generation complete in, the
    accepted observation is the lowest ID and the current facts are that observation's facts."""
    lowest = min(payloads)
    for order in itertools.permutations(payloads):
        state, _ = apply(ListingCurrentState(), obs(1, 1, h("previous generation")))
        for observation in order:
            state, _ = apply(state, obs(2, observation, payloads[observation]))
        assert state.accepted_observation_id == uid(lowest), order
        assert state.current_semantic_hash == payloads[lowest], order
        assert state.current_generation == 2


@settings(max_examples=200)
@given(
    payloads=st.dictionaries(
        st.integers(min_value=2, max_value=10_000),
        st.sampled_from([HASH_A, HASH_B, h("C")]),
        min_size=1,
        max_size=5,
    ),
    data=st.data(),
)
def test_same_generation_order_independence_property(payloads: dict[int, str], data: st.DataObject) -> None:
    order = data.draw(st.permutations(list(payloads)))
    state, _ = apply(ListingCurrentState(), obs(1, 1, h("previous generation")))
    for observation in order:
        state, _ = apply(state, obs(2, observation, payloads[observation]))
    assert state.accepted_observation_id == uid(min(payloads))
    assert state.current_semantic_hash == payloads[min(payloads)]


def test_tie_break_is_order_independent() -> None:
    first, _ = apply(ListingCurrentState(), obs(1, 10, HASH_A))
    first, _ = apply(first, obs(1, 20, HASH_B))
    second, _ = apply(ListingCurrentState(), obs(1, 20, HASH_B))
    second, _ = apply(second, obs(1, 10, HASH_A))
    assert first.accepted_observation_id == second.accepted_observation_id == uid(10)
    assert first.current_semantic_hash == second.current_semantic_hash == HASH_A


@pytest.mark.parametrize(
    "kwargs",
    [
        {"current_generation": 1},
        {"current_generation": 1, "accepted_observation_id": uid(1), "current_semantic_hash": HASH_A},
        {"revision_number": 2},
        {"current_semantic_hash": "not-a-hash"},
    ],
)
def test_listing_state_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ListingCurrentState(**kwargs)


def test_observation_requires_aware_time_and_hash() -> None:
    with pytest.raises(ValueError):
        DetailObservation(
            generation=1, observation_id=uid(1), semantic_hash=HASH_A, observed_at=datetime(2026, 1, 1)
        )
    with pytest.raises(ValueError):
        DetailObservation(generation=1, observation_id=uid(1), semantic_hash="x", observed_at=OBSERVED)


# ---------------------------------------------------------------------------------------------
# VIN
# ---------------------------------------------------------------------------------------------


def test_vin_helpers() -> None:
    assert normalize_vin(" 1m8gdm9a-xkp 042788 ") == VALID_NA_VIN
    assert normalize_vin("  ") is None
    assert normalize_vin(None) is None
    assert is_vin_format_valid(VALID_NA_VIN)
    assert not is_vin_format_valid("1M8GDM9AXKP04278")  # 16 chars
    assert not is_vin_format_valid("1M8GDM9AXKP04278I")  # I is forbidden
    assert not is_vin_format_valid("OM8GDM9AXKP042788")  # O is forbidden
    assert not is_vin_format_valid("QM8GDM9AXKP042788")  # Q is forbidden
    assert not is_vin_format_valid(None)


def test_vin_check_digit_status() -> None:
    assert vin_check_digit_status(VALID_NA_VIN) == "valid"
    assert vin_check_digit_status("1M8GDM9A1KP042788") == "invalid"  # North America: mandatory
    assert vin_check_digit_status(SYNTHETIC_EU_VIN) == "not_applicable"  # EU: never rejected
    assert vin_check_digit_status("TOO-SHORT") == "not_applicable"


# ---------------------------------------------------------------------------------------------
# Identity conflicts (relisting / reused IDs)
# ---------------------------------------------------------------------------------------------


def test_identity_conflict_none_for_same_vehicle() -> None:
    previous = make_listing()
    incoming = make_listing(vehicle={"mileage_km": Decimal("188000")})
    assert detect_identity_conflict(previous, incoming) == []


def test_identity_conflict_detects_each_reason() -> None:
    previous = make_listing(documentation=Documentation(vin=SYNTHETIC_EU_VIN))
    incoming = make_listing(
        documentation=Documentation(vin=OTHER_EU_VIN),
        vehicle={
            "make": "Toyota",
            "first_registration": PartialDate(value="2014", precision=Precision.YEAR),
            "fuel": Fuel.PETROL,
            "mileage_km": Decimal("120000"),
        },
    )
    codes = {r.code for r in detect_identity_conflict(previous, incoming)}
    assert codes == {
        IdentityConflictCode.MAKE_CHANGED,
        IdentityConflictCode.VIN_CHANGED,
        IdentityConflictCode.FIRST_REGISTRATION_YEAR_CHANGED,
        IdentityConflictCode.FUEL_CHANGED,
        IdentityConflictCode.MILEAGE_DECREASED,
    }


def test_identity_conflict_model_change() -> None:
    previous = make_listing()
    incoming = make_listing(vehicle={"model": "Touareg"})
    assert [r.code for r in detect_identity_conflict(previous, incoming)] == [
        IdentityConflictCode.MODEL_CHANGED
    ]


def test_identity_conflict_thresholds_are_exclusive() -> None:
    previous = make_listing()
    within = make_listing(
        vehicle={
            "mileage_km": Decimal("182500"),  # exactly 5,000 km lower
            "first_registration": PartialDate(value="2012-01", precision=Precision.MONTH),
        }
    )
    assert detect_identity_conflict(previous, within) == []
    beyond = make_listing(vehicle={"mileage_km": Decimal("182499.9")})
    assert [r.code for r in detect_identity_conflict(previous, beyond)] == [
        IdentityConflictCode.MILEAGE_DECREASED
    ]


def test_identity_conflict_unknown_is_never_a_change() -> None:
    previous = make_listing()
    incoming = make_listing(
        vehicle={
            "make": None,
            "model": None,
            "fuel": Fuel.UNKNOWN,
            "mileage_km": None,
            "first_registration": PartialDate(),
        }
    )
    assert detect_identity_conflict(previous, incoming) == []


def test_identity_conflict_trim_text_and_aliases() -> None:
    previous = make_listing(vehicle={"make": "VW", "model": "Tiguan"})
    incoming = make_listing(vehicle={"make": "Volkswagen", "model": "Tiguan 2.0 TDI 4Motion"})
    assert detect_identity_conflict(previous, incoming, taxonomy=default_taxonomy()) == []
    # Without the taxonomy, alias resolution is unavailable and VW vs Volkswagen differs.
    codes = [r.code for r in detect_identity_conflict(previous, incoming)]
    assert codes == [IdentityConflictCode.MAKE_CHANGED]


def test_identity_conflict_with_taxonomy_model_alias() -> None:
    previous = make_listing(vehicle={"make": "Mercedes-Benz", "model": "ML 320 CDI"})
    incoming = make_listing(vehicle={"make": "MB", "model": "M-Klasse"})
    assert detect_identity_conflict(previous, incoming, taxonomy=default_taxonomy()) == []


# ---------------------------------------------------------------------------------------------
# possible_same_vehicle
# ---------------------------------------------------------------------------------------------


def other_source(**overrides: Any) -> NormalizedListing:
    return make_listing(
        source_key="fixture_portal_it",
        source_listing_id="SYNTH-IT-9",
        canonical_url="https://portal.example/annunci/9",
        **overrides,
    )


def test_same_vehicle_vin_match_is_strong() -> None:
    a = make_listing(documentation=Documentation(vin=SYNTHETIC_EU_VIN))
    b = other_source(documentation=Documentation(vin=SYNTHETIC_EU_VIN))
    suggestion = possible_same_vehicle(a, b)
    assert suggestion.suggest
    assert suggestion.confidence == Confidence.HIGH
    assert "VIN_MATCH" in {s.code for s in suggestion.signals}


def test_same_vehicle_vin_match_with_spec_conflict_is_only_low_confidence() -> None:
    a = make_listing(documentation=Documentation(vin=SYNTHETIC_EU_VIN))
    b = other_source(
        documentation=Documentation(vin=SYNTHETIC_EU_VIN), vehicle={"make": "Toyota", "model": "RAV4"}
    )
    suggestion = possible_same_vehicle(a, b)
    codes = {s.code for s in suggestion.signals}
    assert {"VIN_MATCH", "SPEC_CONFLICT"} <= codes
    assert suggestion.suggest  # surfaced for human review ...
    assert suggestion.confidence == Confidence.LOW  # ... but never as a strong match


def test_same_vehicle_vin_mismatch_never_suggested() -> None:
    a = make_listing(documentation=Documentation(vin=SYNTHETIC_EU_VIN))
    b = other_source(documentation=Documentation(vin=OTHER_EU_VIN))
    suggestion = possible_same_vehicle(a, b, photo_similarity=Decimal("0.99"), same_seller=True)
    assert not suggestion.suggest
    assert suggestion.vin_mismatch


def test_same_vehicle_supporting_signals_without_vin() -> None:
    a = make_listing()
    b = other_source(
        vehicle={"mileage_km": Decimal("190000")}, price=PriceInfo(amount_minor=290000, currency="EUR")
    )
    suggestion = possible_same_vehicle(a, b)
    codes = {s.code for s in suggestion.signals}
    assert {"SPEC_MATCH", "FIRST_REGISTRATION_MATCH", "MILEAGE_WITHIN_2PCT", "PRICE_WITHIN_10PCT"} <= codes
    assert suggestion.suggest
    assert suggestion.confidence == Confidence.MEDIUM
    assert suggestion.score == Decimal("0.70")


def test_same_vehicle_spec_conflict_lowers_score() -> None:
    a = make_listing()
    b = other_source(vehicle={"fuel": Fuel.PETROL, "power_kw": 125})
    suggestion = possible_same_vehicle(a, b)
    assert not suggestion.suggest
    assert "SPEC_CONFLICT" in {s.code for s in suggestion.signals}


def test_same_vehicle_mileage_and_price_bounds() -> None:
    a = make_listing(
        vehicle={"mileage_km": Decimal("100000")}, price=PriceInfo(amount_minor=100000, currency="EUR")
    )
    near = other_source(
        vehicle={"mileage_km": Decimal("102000")}, price=PriceInfo(amount_minor=111100, currency="EUR")
    )
    near_codes = {s.code for s in possible_same_vehicle(a, near).signals}
    assert {"MILEAGE_WITHIN_2PCT", "PRICE_WITHIN_10PCT"} <= near_codes  # 1.96 % and 9.99 % apart
    far = other_source(
        vehicle={"mileage_km": Decimal("102100")}, price=PriceInfo(amount_minor=111200, currency="EUR")
    )
    far_codes = {s.code for s in possible_same_vehicle(a, far).signals}
    assert "MILEAGE_WITHIN_2PCT" not in far_codes  # 2.06 % of the larger value
    assert "PRICE_WITHIN_10PCT" not in far_codes  # 10.07 %
    other_currency = other_source(price=PriceInfo(amount_minor=100000, currency="CHF"))
    assert "PRICE_WITHIN_10PCT" not in {s.code for s in possible_same_vehicle(a, other_currency).signals}


def test_same_vehicle_optional_inputs() -> None:
    a = make_listing(vehicle={"mileage_km": None, "power_kw": None})
    b = other_source(vehicle={"mileage_km": None, "power_kw": None})
    base = possible_same_vehicle(a, b)
    boosted = possible_same_vehicle(a, b, photo_similarity=Decimal("0.95"), same_seller=True)
    assert boosted.score > base.score
    with pytest.raises(ValidationFailed):
        possible_same_vehicle(a, b, photo_similarity=Decimal("1.5"))


def test_same_vehicle_requires_two_listings() -> None:
    a = make_listing()
    with pytest.raises(ValidationFailed):
        possible_same_vehicle(a, a)
