"""Unit tests for domain.notifications (spec 18, 22). All payloads, URLs and contacts are SYNTHETIC."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.enums import (
    Availability,
    CostCategory,
    Drive,
    EligibilityState,
    Fuel,
    Gearbox,
    OdometerClaim,
    OutboxState,
    ProfileKey,
    ReviewState,
    ValuationState,
)
from suv_deals.domain.money import Money
from suv_deals.domain.notifications import (
    EVENT_TYPE,
    MAX_PAYLOAD_BYTES,
    MCP_EVENT_NAME,
    AlertState,
    ComparableSummary,
    EvidenceFlags,
    MaterialityPolicy,
    MaterialityReason,
    OwnerAlertContent,
    QuietHours,
    RiskLevel,
    assert_external_routing_allowed,
    build_review_pending_event,
    check_owner_wording,
    dashboard_case_url,
    derive_readiness,
    evaluate_materiality,
    evaluate_quiet_hours,
    external_routing_blocker,
    forbidden_phrases_in,
    guard_payload,
    render_owner_message,
    sanitize_seller_text,
    to_mcp_occurrence,
)
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.reviews import ReviewCaseSnapshot
from suv_deals.errors import Forbidden, ValidationFailed

NOW = datetime(2026, 10, 6, 10, 5, tzinfo=UTC)
CASE_ID = UUID("44444444-4444-4444-8444-444444444444")
LISTING_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
BASE = "https://app.example"


def case(**overrides: Any) -> ReviewCaseSnapshot:
    data: dict[str, Any] = {
        "case_id": CASE_ID,
        "workspace_id": UUID(int=100),
        "listing_id": LISTING_ID,
        "profile_key": ProfileKey.PRIMARY,
        "state": ReviewState.PENDING,
        "row_version": 1,
        "revision_id": UUID(int=301),
        "listing_revision": 3,
    }
    data.update(overrides)
    return ReviewCaseSnapshot(**data)


def state(**overrides: Any) -> AlertState:
    data: dict[str, Any] = {
        "price_eur": Decimal("3000"),
        "eligibility": EligibilityState.ELIGIBLE_PRIMARY,
        "risk_level": RiskLevel.MEDIUM,
        "availability": Availability.AVAILABLE,
        "tax_rules_valid": True,
        "evidence_valid": True,
        "documentation_complete": False,
        "conservative_contribution_eur": Decimal("800"),
        "base_contribution_eur": Decimal("1400"),
        "contribution_meets_threshold": False,
    }
    data.update(overrides)
    return AlertState(**data)


APPROVED = MaterialityPolicy(policy_approved=True)


# ============================================================================================= materiality


def test_first_alert_is_material() -> None:
    decision = evaluate_materiality(None, state())
    assert decision.material and decision.realert_allowed
    assert decision.reasons == (MaterialityReason.FIRST_ALERT,)


def test_no_change_is_not_material() -> None:
    decision = evaluate_materiality(state(), state(), APPROVED)
    assert not decision.material and not decision.realert_allowed and not decision.invalidates_recommendation
    assert decision.reasons == ()


@pytest.mark.parametrize(
    ("before", "after", "material", "reason"),
    [
        ("3000", "2900", True, MaterialityReason.PRICE_DECREASE),  # exactly EUR 100
        ("3000", "2900.01", False, None),  # EUR 99.99 and 3.33 %
        ("3000", "3100", True, MaterialityReason.PRICE_INCREASE),
        ("1000", "950", True, MaterialityReason.PRICE_DECREASE),  # exactly 5 % (EUR 50)
        ("1000", "950.01", False, None),  # 4.999 %
        ("1000", "1050", True, MaterialityReason.PRICE_INCREASE),
        ("3000", "3000.00", False, None),
    ],
)
def test_price_threshold_boundaries(
    before: str, after: str, material: bool, reason: MaterialityReason | None
) -> None:
    decision = evaluate_materiality(
        state(price_eur=Decimal(before)), state(price_eur=Decimal(after)), APPROVED
    )
    assert decision.material is material
    assert decision.realert_allowed is material
    if reason:
        assert decision.reasons == (reason,)
        assert decision.invalidates_recommendation
    assert decision.price_change_eur == Decimal(after) - Decimal(before)


def test_unapproved_price_policy_blocks_price_only_realert() -> None:
    decision = evaluate_materiality(state(), state(price_eur=Decimal("2500")))
    assert decision.material
    assert not decision.policy_approved
    assert decision.realert_allowed is False
    assert decision.blockers == ("PRICE_REALERT_POLICY_UNAPPROVED",)
    assert any("PROPOSED" in note for note in decision.explanations)


def test_policy_from_business_config_is_proposed() -> None:
    config = load_business_config(Path(__file__).resolve().parents[2] / "config")
    policy = MaterialityPolicy.from_config(config)
    assert policy.price_abs_eur == Decimal("100") and policy.price_pct == Decimal("5")
    assert policy.policy_approved is False


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"eligibility": EligibilityState.NEEDS_FACTS}, MaterialityReason.ELIGIBILITY_CROSSED),
        ({"risk_level": RiskLevel.HIGH}, MaterialityReason.RISK_BOUNDARY_CROSSED),
        ({"availability": Availability.REMOVED}, MaterialityReason.LISTING_REMOVED),
        ({"availability": Availability.RESERVED}, MaterialityReason.LISTING_RESERVED),
        ({"availability": Availability.SOLD_CLAIMED}, MaterialityReason.LISTING_SOLD_CLAIMED),
        ({"availability": Availability.UNKNOWN}, MaterialityReason.AVAILABILITY_CHANGED),
        ({"tax_rules_valid": False}, MaterialityReason.TAX_NEWLY_INVALID),
        ({"tax_rules_valid": None}, MaterialityReason.TAX_NEWLY_INVALID),
        ({"evidence_valid": False}, MaterialityReason.EVIDENCE_NEWLY_INVALID),
        ({"price_eur": None}, MaterialityReason.PRICE_BECAME_UNKNOWN),
        ({"conservative_contribution_eur": Decimal("-50")}, MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED),
        ({"base_contribution_eur": None}, MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED),
        ({"contribution_meets_threshold": True}, MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED),
    ],
)
def test_always_material_changes_ignore_thresholds_and_policy(
    change: dict[str, Any], reason: MaterialityReason
) -> None:
    decision = evaluate_materiality(state(), state(**change))  # unapproved price policy
    assert decision.reasons == (reason,)
    assert decision.material and decision.realert_allowed
    assert decision.invalidates_recommendation
    assert decision.blockers == ()


def test_small_contribution_change_is_not_material() -> None:
    decision = evaluate_materiality(state(), state(conservative_contribution_eur=Decimal("850")), APPROVED)
    assert not decision.material


def test_documentation_resolved_realerts_without_invalidating() -> None:
    decision = evaluate_materiality(state(), state(documentation_complete=True))
    assert decision.reasons == (MaterialityReason.DOCUMENTATION_RESOLVED,)
    assert decision.realert_allowed and not decision.invalidates_recommendation


def test_combined_reasons_with_unapproved_price_policy() -> None:
    decision = evaluate_materiality(
        state(), state(price_eur=Decimal("2500"), availability=Availability.RESERVED)
    )
    assert set(decision.reasons) == {MaterialityReason.PRICE_DECREASE, MaterialityReason.LISTING_RESERVED}
    assert decision.realert_allowed  # the always-material reason carries the re-alert


def test_alert_state_rejects_float_and_non_positive_price() -> None:
    with pytest.raises(ValueError):
        state(price_eur=2900.0)
    with pytest.raises(ValueError):
        state(price_eur=Decimal("0"))


# ============================================================================================= readiness


@pytest.mark.parametrize(
    ("valuation", "unknown", "comparables", "expected"),
    [
        (None, (), "adequate", "not_valued"),
        (ValuationState.NOT_STARTED, (), "adequate", "not_valued"),
        (ValuationState.STALE, (), "adequate", "valuation_stale"),
        (ValuationState.INVALID, (), "adequate", "valuation_invalid"),
        (ValuationState.INCOMPLETE, (), "insufficient_comparables", "needs_comparables"),
        (ValuationState.INCOMPLETE, (CostCategory.IMPORT_VAT.value,), "adequate", "needs_import_costs"),
        (ValuationState.INCOMPLETE, (CostCategory.TRANSPORT.value,), "small_sample", "needs_costs"),
        (ValuationState.INCOMPLETE, (), "adequate", "incomplete"),
        (ValuationState.ESTIMATED, (), "adequate", "estimated"),
        (ValuationState.QUOTE_SUPPORTED, (), "adequate", "quote_supported"),
    ],
)
def test_derive_readiness(
    valuation: ValuationState | None, unknown: tuple[str, ...], comparables: str, expected: str
) -> None:
    assert (
        derive_readiness(valuation, unknown_cost_categories=unknown, comparable_status=comparables)
        == expected
    )


# ============================================================================================= outbox payload


def test_review_pending_payload_matches_spec_shape() -> None:
    draft = build_review_pending_event(
        case(),
        dashboard_base_url=BASE,
        event_id=EVENT_ID,
        occurred_at=NOW,
        readiness="needs_import_costs",
    )
    assert draft.payload == {
        "schema_version": "1.0",
        "event_id": "33333333-3333-4333-8333-333333333333",
        "type": "review.pending",
        "occurred_at": "2026-10-06T10:05:00Z",
        "case_id": "44444444-4444-4444-8444-444444444444",
        "case_version": 1,
        "listing_id": "11111111-1111-4111-8111-111111111111",
        "listing_revision": 3,
        "priority": "normal",
        "dashboard_url": "https://app.example/reviews/44444444-4444-4444-8444-444444444444",
        "summary": "New research candidate; import costs need verification.",
        "deduplication_key": "review.pending:44444444-4444-4444-8444-444444444444:1",
        "readiness": "needs_import_costs",
        "profile": "primary",
    }
    assert draft.dedup_key == draft.payload["deduplication_key"]
    assert draft.event_type == EVENT_TYPE
    assert draft.initial_state == OutboxState.PENDING and draft.blocker_code is None
    assert len(draft.payload_hash) == 64
    assert 0 < draft.payload_bytes < MAX_PAYLOAD_BYTES


def test_dedup_key_follows_case_version() -> None:
    draft = build_review_pending_event(
        case(row_version=4),
        dashboard_base_url=BASE,
        event_id=EVENT_ID,
        occurred_at=NOW,
        readiness="estimated",
        queue="primary",
    )
    assert draft.payload["deduplication_key"] == f"review.pending:{CASE_ID}:4"
    assert draft.payload["queue"] == "primary"


def test_fixture_event_is_blocked_and_never_routed() -> None:
    draft = build_review_pending_event(
        case(is_fixture=True),
        dashboard_base_url=BASE,
        event_id=EVENT_ID,
        occurred_at=NOW,
        readiness="estimated",
    )
    assert draft.is_fixture
    assert draft.initial_state == OutboxState.BLOCKED and draft.blocker_code == "FIXTURE_EVENT"
    assert draft.payload["summary"].startswith("[SYNTHETIC FIXTURE]")
    with pytest.raises(Forbidden):
        to_mcp_occurrence(draft)
    with pytest.raises(Forbidden):
        to_mcp_occurrence(draft.payload, is_fixture=True)
    assert external_routing_blocker(is_fixture=True) == "FIXTURE_EVENT"
    assert external_routing_blocker(is_fixture=False) is None
    assert_external_routing_allowed(is_fixture=False)


@pytest.mark.parametrize(
    "base",
    [
        "https://user:pass@app.example",
        "https://app.example/?token=abc",
        "https://app.example/#frag",
        "http://app.example",
        "ftp://app.example",
        "app.example",
    ],
)
def test_dashboard_links_never_embed_tokens_or_credentials(base: str) -> None:
    with pytest.raises(ValidationFailed):
        dashboard_case_url(base, CASE_ID)


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": ReviewState.SUPERSEDED, "superseded_by_id": UUID(int=9)},
        {
            "state": ReviewState.CLAIMED,
            "claim_holder": UUID(int=1),
            "claim_token_hash": "a" * 64,
            "claimed_at": NOW,
            "claim_expires_at": NOW + timedelta(minutes=5),
        },
    ],
)
def test_only_pending_cases_emit_review_pending(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        build_review_pending_event(
            case(**overrides),
            dashboard_base_url=BASE,
            event_id=EVENT_ID,
            occurred_at=NOW,
            readiness="estimated",
        )


def test_dashboard_url_local_http_and_trailing_slash() -> None:
    assert (
        dashboard_case_url("http://localhost:3000/app/", CASE_ID)
        == f"http://localhost:3000/app/reviews/{CASE_ID}"
    )


def test_event_input_validation() -> None:
    with pytest.raises(ValidationFailed):
        build_review_pending_event(
            case(), dashboard_base_url=BASE, event_id=EVENT_ID, occurred_at=NOW, readiness="Bad Label"
        )
    with pytest.raises(ValidationFailed):
        build_review_pending_event(
            case(),
            dashboard_base_url=BASE,
            event_id=EVENT_ID,
            occurred_at=NOW,
            readiness="estimated",
            queue="Primary queue",
        )
    with pytest.raises(ValidationFailed):
        build_review_pending_event(
            case(),
            dashboard_base_url=BASE,
            event_id=EVENT_ID,
            occurred_at=datetime(2026, 10, 6),
            readiness="estimated",
        )


def test_caller_summary_is_sanitised() -> None:
    draft = build_review_pending_event(
        case(),
        dashboard_base_url=BASE,
        event_id=EVENT_ID,
        occurred_at=NOW,
        readiness="estimated",
        summary="**Call +49 171 1234567** guaranteed profit http://evil.example",
    )
    summary = draft.payload["summary"]
    assert "+49" not in summary and "evil.example" not in summary and "*" not in summary
    assert "guaranteed" not in summary.lower()


# ============================================================================================= MCP occurrence


def test_mcp_occurrence_envelope() -> None:
    draft = build_review_pending_event(
        case(), dashboard_base_url=BASE, event_id=EVENT_ID, occurred_at=NOW, readiness="needs_import_costs"
    )
    occurrence = to_mcp_occurrence(draft)
    assert occurrence == {
        "eventId": "33333333-3333-4333-8333-333333333333",
        "name": MCP_EVENT_NAME,
        "timestamp": "2026-10-06T10:05:00Z",
        "data": {
            "case_id": "44444444-4444-4444-8444-444444444444",
            "case_version": 1,
            "listing_id": "11111111-1111-4111-8111-111111111111",
            "listing_revision": 3,
            "readiness": "needs_import_costs",
            "dashboard_url": "https://app.example/reviews/44444444-4444-4444-8444-444444444444",
        },
        "cursor": None,
    }
    assert to_mcp_occurrence(draft.payload) == occurrence  # also from the stored JSON payload


def test_mcp_occurrence_rejects_invalid_payload() -> None:
    draft = build_review_pending_event(
        case(), dashboard_base_url=BASE, event_id=EVENT_ID, occurred_at=NOW, readiness="estimated"
    )
    broken = dict(draft.payload)
    broken["type"] = "review.decided"
    with pytest.raises(ValidationFailed):
        to_mcp_occurrence(broken)
    with pytest.raises(ValidationFailed):
        to_mcp_occurrence({**draft.payload, "case_version": "1"})


def test_mcp_occurrence_compatible_with_event_bridge() -> None:
    bridge = pytest.importorskip("suv_deals.integrations.event_bridge")
    draft = build_review_pending_event(
        case(), dashboard_base_url=BASE, event_id=EVENT_ID, occurred_at=NOW, readiness="needs_import_costs"
    )
    assert bridge.build_occurrence(draft.payload) == to_mcp_occurrence(draft)
    signal = bridge.parse_signal(draft.payload)
    assert bridge.matches_filters({"profile": "primary"}, signal)


# ============================================================================================= payload guard


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"summary": "call +389 70 123 456"}, "PHONE_NUMBER"),
        ({"summary": "call 0049 171 1234567"}, "PHONE_NUMBER"),
        ({"summary": "mobile 070 123 456"}, "PHONE_NUMBER"),
        # regressions: parenthesised and Italian-mobile forms used to pass the guard
        ({"summary": "Tel. (0171) 1234567"}, "PHONE_NUMBER"),
        ({"summary": "Tel. +49 (0)171 1234567"}, "PHONE_NUMBER"),
        ({"summary": "cell (+39) 333 1234567"}, "PHONE_NUMBER"),
        ({"summary": "cell 333 1234567"}, "PHONE_NUMBER"),
        ({"summary": "Natel 079 123 45 67"}, "PHONE_NUMBER"),
        ({"summary": "reach me at seller@example.com"}, "EMAIL_ADDRESS"),
        ({"summary": "Bearer abcdefghijklmnop.qrstuv"}, "SECRET_TOKEN"),
        ({"summary": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"}, "SECRET_TOKEN"),
        ({"summary": "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"}, "SECRET_TOKEN"),
        ({"summary": "claim Zr3Qv9XkP2mLw8YtN4bJ6hD1sF5gA7cE0uIoKpRq"}, "SECRET_TOKEN"),
        ({"summary": "password=hunter2"}, "SECRET_TOKEN"),
        ({"summary": "<html><body>raw listing</body></html>"}, "RAW_HTML"),
        ({"summary": "x" * 4001}, "RAW_CONTENT_TOO_LONG"),
        ({"dashboard_url": "https://app.example/reviews/1?access_token=abc"}, "URL_TOKEN_PARAMETER"),
        ({"dashboard_url": "https://u:p@app.example/reviews/1"}, "URL_CREDENTIALS"),
        ({"claim_token": "x"}, "FORBIDDEN_KEY"),
        ({"seller_phone": "n/a"}, "FORBIDDEN_KEY"),
        ({"raw_listing": {}}, "FORBIDDEN_KEY"),
        ({"nested": [{"Email": "n/a"}]}, "FORBIDDEN_KEY"),
        ({"api_key": "n/a"}, "FORBIDDEN_KEY"),
    ],
)
def test_payload_guard_rejects(payload: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        guard_payload(payload)
    problems = exc.value.details["problems"]
    assert any(p.startswith(problem) for p in problems), problems
    # the error never echoes the offending value
    assert "hunter2" not in str(exc.value.details) and "123 456" not in str(exc.value.details)


def test_payload_guard_size_limit() -> None:
    big = {"items": ["a" * 1000] * 270}
    with pytest.raises(ValidationFailed) as exc:
        guard_payload(big)
    assert "PAYLOAD_TOO_LARGE" in exc.value.details["problems"]
    assert guard_payload({"items": ["a" * 1000] * 200}) < MAX_PAYLOAD_BYTES


def test_payload_guard_rejects_non_json() -> None:
    with pytest.raises(ValidationFailed):
        guard_payload({"amount": Decimal("1")})
    with pytest.raises(ValidationFailed):
        guard_payload({"x": float("nan")})


def test_payload_guard_accepts_ids_timestamps_and_prices() -> None:
    payload = {
        "case_id": "07123456-1234-4123-8123-123456789012",
        "occurred_at": "2026-10-06T10:05:00Z",
        "summary": "Asking 2,750.00 EUR, 187,500 km, band EUR 8,000-10,000, first reg. 2011-05",
        "hash": "a" * 64,
        "deduplication_key": "review.pending:07123456-1234-4123-8123-123456789012:12",
        "dashboard_url": "https://app.example/reviews/07123456-1234-4123-8123-123456789012",
    }
    assert guard_payload(payload) > 0


# ============================================================================================= owner message


def content(**overrides: Any) -> OwnerAlertContent:
    data: dict[str, Any] = {
        "make": "Example",
        "model": "Trail",
        "generation": "G2",
        "year": "2011-05",
        "fuel": Fuel.DIESEL,
        "gearbox": Gearbox.MANUAL,
        "drive": Drive.AWD,
        "power_kw": 103,
        "displacement_cm3": 1995,
        "asking_price": Money.of("2750", "EUR"),
        "mileage_km": Decimal("187500"),
        "mileage_claim": OdometerClaim.SELLER_REPORTED,
        "country": "DE",
        "last_checked_at": NOW - timedelta(hours=2),
        "comparables": ComparableSummary(
            status="small_sample",
            asking_count=2,
            median_asking=Money.of("8900", "EUR"),
            min_asking=Money.of("8000", "EUR"),
            max_asking=Money.of("9800", "EUR"),
            band_fit="within",
        ),
        "top_risks": ("Synthetic risk: AWD coupling noise reported by seller",),
        "unresolved_costs": ("import duty", "transport"),
        "source_url": "https://dealer.example/vehicles/TEST-204",
        "dashboard_url": f"https://app.example/reviews/{CASE_ID}",
    }
    data.update(overrides)
    return OwnerAlertContent(**data)


def test_owner_message_contents_and_labels() -> None:
    text = render_owner_message(content(), now=NOW)
    assert text.startswith("Research candidate - estimated figures")
    assert "Example Trail G2" in text and "diesel" in text and "AWD" in text and "103 kW" in text
    assert "2,750.00 EUR" in text and "payable amount not confirmed" in text
    assert "187,500 km (seller reported)" in text
    assert "Seller country: DE" in text
    assert "last checked 2 h ago (2026-10-06 10:05 Europe/Skopje)" in text
    assert "asking prices, not sale prices" in text and "(small sample)" in text
    assert "not shown - valuation incomplete" in text
    assert "- import duty" in text and "unknown, not zero" in text
    assert "Source: https://dealer.example/vehicles/TEST-204" in text
    assert "Dashboard (sign-in required)" in text


def test_contribution_only_when_complete() -> None:
    complete = content(
        valuation_complete=True,
        conservative_contribution=Money.of("800", "EUR"),
        base_contribution=Money.of("1400", "EUR"),
        unresolved_costs=(),
    )
    text = render_owner_message(complete, now=NOW)
    assert "conservative 800.00 EUR; base 1,400.00 EUR" in text
    assert "Estimated contribution before business tax" in text
    partial = content(valuation_complete=True, conservative_contribution=Money.of("800", "EUR"))
    assert "not shown" in render_owner_message(partial, now=NOW)


def test_non_eur_price_and_unknowns() -> None:
    text = render_owner_message(
        content(
            asking_price=Money.of("2700", "CHF"),
            asking_price_eur=Money.of("2903.23", "EUR"),
            mileage_km=None,
            country=None,
            last_checked_at=None,
            comparables=None,
            fuel=Fuel.UNKNOWN,
            year=None,
        ),
        now=NOW,
    )
    assert "2,700.00 CHF" in text and "approx. 2,903.23 EUR" in text
    assert "Mileage: unknown" in text and "Seller country: unknown" in text
    assert "no successful detail check recorded" in text
    assert "insufficient MK comparables; research needed" in text
    assert "fuel unknown" in text and "first registration unknown" in text


@pytest.mark.parametrize(
    "phrase",
    [
        "guaranteed profit",
        "Guaranteed",
        "verified accident-free",
        "Seller confirmed",
        "net profit",
        "risk-free",
    ],
)
def test_forbidden_phrases_rejected_without_evidence(phrase: str) -> None:
    with pytest.raises(ValidationFailed):
        check_owner_wording(f"Synthetic message: {phrase} deal")


def test_forbidden_phrases_allowed_only_with_supporting_evidence() -> None:
    flags = EvidenceFlags(
        accident_free_verified=True, seller_confirmation_evidence=True, business_tax_modelled=True
    )
    check_owner_wording("verified accident-free; seller confirmed availability; net profit 900 EUR", flags)
    with pytest.raises(ValidationFailed):
        check_owner_wording("guaranteed profit", flags)  # never supportable
    assert forbidden_phrases_in("accident free (seller claim)") == []


def test_seller_text_cannot_inject_claims_links_or_markup() -> None:
    hostile = content(
        model="Trail **GUARANTEED PROFIT** [click](http://evil.example) @channel <!here>",
        trim="verified accident-free\u202e, seller confirmed",
        top_risks=("Call 0171 1234567 or mail a@b.example\x00\x07", "x" * 500),
    )
    text = render_owner_message(hostile, now=NOW)
    lower = text.lower()
    assert "guaranteed" not in lower and "verified accident" not in lower and "seller confirmed" not in lower
    assert "evil.example" not in text and "@" not in text.split("Dashboard")[0]
    assert "*" not in text and "[click]" not in text and "<!here>" not in text
    assert "\u202e" not in text and "\x00" not in text
    assert "0171 1234567" not in text
    assert all(len(line) < 400 for line in text.splitlines())


def test_sanitize_seller_text() -> None:
    assert sanitize_seller_text(None) is None
    assert sanitize_seller_text("   ") is None
    assert sanitize_seller_text("  Example   Trail\n\tG2 ") == "Example Trail G2"
    assert sanitize_seller_text("a" * 200, max_length=20) == "a" * 17 + "..."
    assert "claim omitted" in (sanitize_seller_text("net_profit guaranteed") or "")
    with pytest.raises(ValidationFailed):
        sanitize_seller_text("x", max_length=2)


def test_fixture_owner_message_is_labelled() -> None:
    text = render_owner_message(content(is_fixture=True), now=NOW)
    assert text.startswith("[SYNTHETIC FIXTURE")


def test_owner_message_links_validated() -> None:
    with pytest.raises(ValueError):
        content(source_url="javascript:alert(1)")
    with pytest.raises(ValueError):
        content(dashboard_url="https://u:p@app.example/reviews/1")
    with pytest.raises(ValidationFailed):
        render_owner_message(content(source_url="https://dealer.example/v?token=abc"), now=NOW)


# ============================================================================================= quiet hours


def quiet(**overrides: Any) -> QuietHours:
    data: dict[str, Any] = {"start": time(22, 0), "end": time(7, 0), "approved": True}
    data.update(overrides)
    return QuietHours(**data)


def test_no_or_unapproved_quiet_hours_are_not_applied() -> None:
    assert evaluate_quiet_hours(NOW, None).deliver_now
    proposed = evaluate_quiet_hours(datetime(2026, 10, 6, 21, 0, tzinfo=UTC), quiet(approved=False))
    assert proposed.deliver_now and not proposed.applied
    assert "owner" in proposed.reason


def test_inside_wrapping_window_defers_to_local_end() -> None:
    # 21:30 UTC = 23:30 Europe/Skopje (CEST, UTC+2) -> defer to 07:00 local next day = 05:00 UTC
    decision = evaluate_quiet_hours(datetime(2026, 10, 6, 21, 30, tzinfo=UTC), quiet())
    assert not decision.deliver_now and decision.applied
    assert decision.deliver_at == datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
    # 03:00 UTC = 05:00 local -> same-day 07:00 local
    early = evaluate_quiet_hours(datetime(2026, 10, 7, 3, 0, tzinfo=UTC), quiet())
    assert early.deliver_at == datetime(2026, 10, 7, 5, 0, tzinfo=UTC)


def test_window_boundaries() -> None:
    at_start = evaluate_quiet_hours(datetime(2026, 10, 6, 20, 0, tzinfo=UTC), quiet())  # 22:00 local
    assert not at_start.deliver_now
    at_end = evaluate_quiet_hours(datetime(2026, 10, 7, 5, 0, tzinfo=UTC), quiet())  # 07:00 local
    assert at_end.deliver_now


def test_daytime_window_and_winter_offset() -> None:
    daytime = quiet(start=time(12, 0), end=time(14, 0))
    # 2026-12-01 11:30 UTC = 12:30 CET (UTC+1)
    decision = evaluate_quiet_hours(datetime(2026, 12, 1, 11, 30, tzinfo=UTC), daytime)
    assert decision.deliver_at == datetime(2026, 12, 1, 13, 0, tzinfo=UTC)
    assert evaluate_quiet_hours(datetime(2026, 12, 1, 14, 0, tzinfo=UTC), daytime).deliver_now


def test_dst_change_night() -> None:
    # Europe/Skopje leaves CEST on 2026-10-25 03:00 local -> 02:00 CET.
    decision = evaluate_quiet_hours(datetime(2026, 10, 24, 21, 0, tzinfo=UTC), quiet())  # 23:00 CEST
    assert decision.deliver_at == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)  # 07:00 CET


def test_urgent_bypass_only_when_owner_allows() -> None:
    night = datetime(2026, 10, 6, 21, 30, tzinfo=UTC)
    assert not evaluate_quiet_hours(night, quiet(), "urgent").deliver_now
    assert evaluate_quiet_hours(night, quiet(urgent_bypass=True), "urgent").deliver_now
    assert not evaluate_quiet_hours(night, quiet(urgent_bypass=True), "normal").deliver_now


def test_quiet_hours_validation() -> None:
    with pytest.raises(ValueError):
        quiet(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        quiet(start=time(22, 0, tzinfo=UTC))
    assert evaluate_quiet_hours(NOW, quiet(start=time(1, 0), end=time(1, 0))).deliver_now
    with pytest.raises(ValidationFailed):
        evaluate_quiet_hours(datetime(2026, 10, 6, 21, 30), quiet())


# ========================================================================================= review regressions


@pytest.mark.parametrize(
    "text",
    [
        "Asking 2,750.00 EUR, 187,500 km, first reg. 2011-05",
        "Inserat 412345678",  # 9-digit listing reference starting like a mobile prefix
        "Mileage 199.999 km (seller reported)",
        "CO2 189 g/km (WLTP) 2.0 TDI 103 kW (140 PS)",
        "Euro 5 (2011) 1995 cm3",
        "Preis 2.950 EUR VB (Verhandlungsbasis)",
        "HU bis 06/2027",
    ],
)
def test_phone_detection_has_no_false_positives_on_listing_text(text: str) -> None:
    assert guard_payload({"summary": text}) > 0
    assert sanitize_seller_text(text, max_length=200) is not None
    assert "contact removed" not in (sanitize_seller_text(text, max_length=200) or "")


@pytest.mark.parametrize(
    "raw", ["Call (0171) 1234567 today", "Ruf +49 (0)171 1234567", "chiamare (+39) 333 1234567"]
)
def test_sanitize_removes_parenthesised_phone_numbers(raw: str) -> None:
    cleaned = sanitize_seller_text(raw, max_length=200) or ""
    assert "contact removed" in cleaned
    assert "1234567" not in cleaned
    # and the owner message built from it renders instead of being rejected
    assert "1234567" not in render_owner_message(content(top_risks=(raw,)), now=NOW)


def test_stored_fixture_payload_is_refused_without_the_row_flag() -> None:
    draft = build_review_pending_event(
        case(is_fixture=True),
        dashboard_base_url=BASE,
        event_id=EVENT_ID,
        occurred_at=NOW,
        readiness="estimated",
    )
    assert draft.payload["fixture"] is True
    with pytest.raises(Forbidden):
        to_mcp_occurrence(dict(draft.payload))  # caller forgot is_fixture
    marker_only = {k: v for k, v in draft.payload.items() if k != "fixture"}
    with pytest.raises(Forbidden):
        to_mcp_occurrence(marker_only)  # the summary prefix alone still blocks it
    real = build_review_pending_event(
        case(), dashboard_base_url=BASE, event_id=EVENT_ID, occurred_at=NOW, readiness="estimated"
    )
    assert "fixture" not in real.payload
    assert to_mcp_occurrence({**real.payload, "fixture": False})["eventId"] == str(EVENT_ID)


def test_owner_message_accepts_mixed_case_listing_slug() -> None:
    url = "https://marketplace.example/auto/Example-Trail-2011-Torino-123456789.htm"
    text = render_owner_message(content(source_url=url), now=NOW)
    assert f"Source: {url}" in text


@pytest.mark.parametrize(
    "url",
    [
        "https://dealer.example/v?ref=Zr3Qv9XkP2mLw8YtN4bJ6hD1sF5gA7cE0uIoKpRq",  # token-like value
        "https://dealer.example/v/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl",  # JWT in path
        "https://dealer.example/v?contact=seller@example.com",
        "https://dealer.example/v?session=abc",
    ],
)
def test_owner_message_source_link_still_guarded(url: str) -> None:
    with pytest.raises(ValidationFailed):
        render_owner_message(content(source_url=url), now=NOW)


def test_long_owner_message_never_truncates_links() -> None:
    dashboard = f"https://app.example/reviews/{CASE_ID}"
    long_body = content(
        top_risks=tuple(f"risk {i} " + "r" * 150 for i in range(5)),
        unresolved_costs=tuple(f"cost {i} " + "c" * 110 for i in range(10)),
        trim="t" * 300,
        source_url="https://dealer.example/vehicles/" + "a" * 1200,
    )
    text = render_owner_message(long_body, now=NOW)
    assert len(text) <= 3500
    assert text.endswith(f"Dashboard (sign-in required): {dashboard}")
    assert "Source: https://dealer.example/vehicles/" + "a" * 1200 in text
    oversized_source = content(source_url="https://dealer.example/vehicles/" + "a" * 1900)
    text2 = render_owner_message(oversized_source, now=NOW)
    assert "link too long for this message" in text2
    assert text2.endswith(f"Dashboard (sign-in required): {dashboard}")


def test_incomplete_valuation_is_always_labelled_research_candidate() -> None:
    text = render_owner_message(content(research_candidate=False), now=NOW)
    assert text.startswith("Research candidate")
    complete = content(
        research_candidate=False,
        valuation_complete=True,
        conservative_contribution=Money.of("800", "EUR"),
        base_contribution=Money.of("1400", "EUR"),
    )
    assert render_owner_message(complete, now=NOW).startswith("Reviewed candidate")


def test_break_even_to_loss_is_a_contribution_sign_change() -> None:
    decision = evaluate_materiality(
        state(conservative_contribution_eur=Decimal("0")),
        state(conservative_contribution_eur=Decimal("-50")),
        APPROVED,
    )
    assert decision.reasons == (MaterialityReason.CONTRIBUTION_SCENARIO_CHANGED,)
    assert decision.invalidates_recommendation
