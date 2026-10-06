"""Tax engine tests (spec sections 16 and 31 "Tax" row; gate section 32 "Tax rules").

Every rule set here is SYNTHETIC: jurisdiction ``XX`` is a user-assigned code, and every
rate, bracket and amount is invented to exercise the engine. None is a real tax rate.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.enums import Co2Cycle, CostCategory, FxPurpose, TaxRuleStatus
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    ALLOWED_TRANSITIONS,
    HASH_EXCLUDED_FIELDS,
    Classification,
    ComponentResult,
    ComponentStatus,
    ExemptionClaim,
    IncludedCost,
    OriginProof,
    OriginProofStatus,
    Predicate,
    ReviewRecord,
    RoundingSpec,
    RuleSet,
    RuleSource,
    TaxCalculation,
    TaxInputs,
    apply_rounding,
    calculate,
    can_transition,
    compute_rule_set_sha256,
    find_active_overlaps,
    load_rule_set_file,
    parse_rule_set_json,
    rule_set_problems,
    seal_rule_set,
    select_rule_set,
    transition_rule_set,
    validate_rule_set,
)
from suv_deals.errors import ErrorCode, ValidationFailed

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json"
EXAMPLE = REPO / "config" / "tax_rules" / "example_unapproved.json"
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
DECL = date(2026, 6, 1)
CATEGORY = "passenger_car"
SYNTHETIC_SOURCE = RuleSource(
    url="https://example.invalid/SYNTHETIC-legal-source",
    title="SYNTHETIC source document (test only)",
    retrieved_at=datetime(2026, 5, 1, tzinfo=UTC),
    sha256="0" * 64,
)


# --------------------------------------------------------------------------- helpers


def fixture_doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)
    return doc


def variant(mutate: Callable[[dict[str, Any]], None] | None = None, **fields: Any) -> RuleSet:
    """A SYNTHETIC fixture variant (hash cleared so edits are allowed)."""
    doc = fixture_doc()
    doc["sha256"] = None
    doc.update(fields)
    if mutate is not None:
        mutate(doc)
    rule_set = RuleSet.model_validate(doc)
    validate_rule_set(rule_set)
    return rule_set


def component(doc: dict[str, Any], comp_id: str) -> dict[str, Any]:
    return next(c for c in doc["components"] if c["id"] == comp_id)


def make_active(
    mutate: Callable[[dict[str, Any]], None] | None = None,
    *,
    version: str = "synthetic-active-1",
    valid_from: str = "2026-01-01",
    valid_to: str | None = "2027-01-01",
    existing: tuple[RuleSet, ...] = (),
) -> RuleSet:
    """Drive a SYNTHETIC non-fixture rule set through the real lifecycle to ACTIVE."""
    doc = fixture_doc()
    doc.update(
        status="draft",
        is_fixture=False,
        sha256=None,
        version=version,
        valid_from=valid_from,
        valid_to=valid_to,
        sources=[SYNTHETIC_SOURCE.model_dump(mode="json")],
    )
    if mutate is not None:
        mutate(doc)
    draft = RuleSet.model_validate(doc)
    review_state = transition_rule_set(draft, TaxRuleStatus.UNDER_REVIEW, at=T0)
    review = ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=T0,
        content_sha256=compute_rule_set_sha256(review_state),
        scope="SYNTHETIC test review of every component",
    )
    approved = transition_rule_set(
        review_state, TaxRuleStatus.APPROVED, at=T0, approved_by="SYNTHETIC owner", review_record=review
    )
    return transition_rule_set(approved, TaxRuleStatus.ACTIVE, at=T0, existing=existing)


def approved_classification() -> Classification:
    return Classification(
        tariff_code="8703 23",
        vehicle_category=CATEGORY,
        evidence_ids=("SYNTHETIC-classification-evidence",),
        approval_status="approved",
        approved_by="SYNTHETIC owner",
    )


def no_proof() -> OriginProof:
    return OriginProof(proof_type="none presented", acceptance_status=OriginProofStatus.NOT_AVAILABLE)


def accepted_proof(country: str, *, preferential: bool | None = True, **kw: Any) -> OriginProof:
    return OriginProof(
        proof_type="SYNTHETIC preferential origin proof",
        issuing_authority="SYNTHETIC authority",
        origin_country=country,
        preferential=preferential,
        acceptance_status=OriginProofStatus.ACCEPTED,
        evidence_ids=("SYNTHETIC-origin-evidence",),
        **kw,
    )


def inputs(**overrides: Any) -> TaxInputs:
    values: dict[str, Any] = {
        "declaration_date": DECL,
        "jurisdiction": "XX",
        "classification": approved_classification(),
        "vehicle_condition": "used",
        "vehicle_age_years": Decimal("12"),
        "seller_country": "DE",
        "dispatch_country": "DE",
        "origin_proof": no_proof(),
        "invoice_price": Money.of("80000.00", "MKD"),
        "customs_value": Money.of("100000.00", "MKD"),
        "customs_value_basis": "SYNTHETIC transaction value",
        "co2_g_km": Decimal("120"),
        "co2_cycle": Co2Cycle.WLTP,
        "co2_source_document": "SYNTHETIC CoC",
        "engine_displacement_cm3": Decimal("1995"),
    }
    values.update(overrides)
    return TaxInputs(**values)


def amounts(calc: TaxCalculation) -> dict[str, Decimal | None]:
    return {c.component_id: (None if c.amount is None else c.amount.amount) for c in calc.components}


def statuses(calc: TaxCalculation) -> dict[str, ComponentStatus]:
    return {c.component_id: c.status for c in calc.components}


def customs_rate(base: str = "EUR", quote: str = "MKD", rate: str = "61.5", **kw: Any) -> FxRate:
    values: dict[str, Any] = {
        "base": base,
        "quote": quote,
        "rate": Decimal(rate),
        "rate_date": date(2026, 5, 29),
        "retrieved_at": datetime(2026, 5, 29, 12, tzinfo=UTC),
        "provider": "SYNTHETIC customs authority",
        "purpose": FxPurpose.CUSTOMS,
    }
    values.update(kw)
    return FxRate(**values)


# --------------------------------------------------------------------------- shipped config


def test_example_rule_set_has_exact_spec_shape_and_is_inert() -> None:
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert list(raw) == [
        "rule_set_id",
        "jurisdiction",
        "version",
        "status",
        "valid_from",
        "valid_to",
        "currency",
        "sources",
        "approved_by",
        "approved_at",
        "required_inputs",
        "components",
        "rounding_rules",
        "missing_input_behavior",
        "sha256",
    ]
    rule_set = load_rule_set_file(EXAMPLE)
    assert rule_set.status == TaxRuleStatus.UNAPPROVED
    assert rule_set.components == ()
    assert rule_set.missing_input_behavior == "return_incomplete"
    for allow in (False, True):
        assert select_rule_set([rule_set], "MK", CATEGORY, DECL, allow_unapproved=allow).rule_set is None
    calc = calculate(rule_set, TaxInputs(), T0)
    assert not calc.complete
    assert calc.total_import_cost is None
    assert calc.known_subtotal is None
    assert set(calc.missing_inputs) == set(rule_set.required_inputs)
    assert not calc.production_ready


def test_no_rule_set_shipped_in_config_has_rates_or_approval() -> None:
    for path in sorted((REPO / "config" / "tax_rules").glob("*.json")):
        rule_set = load_rule_set_file(path)
        assert rule_set.components == (), path.name
        assert rule_set.status in (TaxRuleStatus.UNAPPROVED, TaxRuleStatus.DRAFT), path.name
        assert rule_set.approved_by is None and rule_set.sources == (), path.name


# --------------------------------------------------------------------------- loading, hashing


def test_fixture_loads_with_matching_hash() -> None:
    rule_set = load_rule_set_file(FIXTURE)
    assert rule_set.is_fixture
    assert rule_set.sha256 == compute_rule_set_sha256(rule_set)
    assert "SYNTHETIC" in rule_set.rule_set_id


def test_tampered_content_is_detected_on_load() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert '"rate": "0.10"' in text
    with pytest.raises(ValidationFailed) as exc:
        parse_rule_set_json(text.replace('"rate": "0.10"', '"rate": "0.09"'))
    assert any("sha256" in p for p in exc.value.details["problems"])


def test_hash_ignores_lifecycle_fields_but_covers_content() -> None:
    rule_set = load_rule_set_file(FIXTURE)
    original = compute_rule_set_sha256(rule_set)
    lifecycle = rule_set.model_copy(update={"status": TaxRuleStatus.REVOKED, "approved_by": "x"})
    assert compute_rule_set_sha256(lifecycle) == original
    assert {"sha256", "status", "approved_by", "approved_at", "review_record"} == set(HASH_EXCLUDED_FIELDS)
    for update in ({"valid_to": date(2026, 12, 31)}, {"version": "other"}, {"vehicle_categories": ("suv",)}):
        assert compute_rule_set_sha256(rule_set.model_copy(update=update)) != original


def test_json_numbers_are_decimals_and_floats_are_refused() -> None:
    doc = fixture_doc()
    doc["sha256"] = None
    text = json.dumps(doc, default=str).replace('"rate": "0.10"', '"rate": 0.10')
    rule_set = parse_rule_set_json(text)
    standard = next(c for c in rule_set.components if c.id == "duty_standard")
    assert standard.rate == Decimal("0.10")  # type: ignore[union-attr]
    doc = fixture_doc()
    component(doc, "duty_standard")["rate"] = 0.1
    with pytest.raises(ValidationError, match="float"):
        RuleSet.model_validate(doc)


def test_duplicate_json_keys_and_non_objects_are_rejected() -> None:
    with pytest.raises(ValidationFailed, match="JSON"):
        parse_rule_set_json('{"rule_set_id": "a-b-c", "rule_set_id": "x-y-z"}')
    with pytest.raises(ValidationFailed, match="object"):
        parse_rule_set_json("[]")
    with pytest.raises(ValidationFailed, match="structure"):
        parse_rule_set_json('{"rule_set_id": "abc"}')
    with pytest.raises(ValidationFailed, match="too large"):
        parse_rule_set_json(b" " * 1_000_001)


# --------------------------------------------------------------------------- validation


def _dup_id(d: dict[str, Any]) -> None:
    d["components"].append(copy.deepcopy(component(d, "processing_fee")))


def _forward_ref(d: dict[str, Any]) -> None:
    vat = d["components"].pop()
    d["components"].insert(0, vat)


def _base_not_in_depends(d: dict[str, Any]) -> None:
    component(d, "vat")["depends_on"] = ["duty_listed_origin", "duty_standard"]


def _undeclared_predicate(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["applies_when"] = [{"input": "fuel", "op": "eq", "value": "diesel"}]


def _unknown_input(d: dict[str, Any]) -> None:
    d["required_inputs"].append("mileage_km")


def _missing_unit(d: dict[str, Any]) -> None:
    del d["input_units"]["co2_g_km"]


def _wrong_unit(d: dict[str, Any]) -> None:
    d["input_units"]["engine_displacement_cm3"] = "l"


def _unsorted(d: dict[str, Any]) -> None:
    rows = component(d, "age_fee")["brackets"]
    rows[0], rows[1] = rows[1], rows[0]


def _overlap(d: dict[str, Any]) -> None:
    component(d, "age_fee")["brackets"][1]["lower_inclusive"] = "4"


def _gap(d: dict[str, Any]) -> None:
    component(d, "age_fee")["brackets"][1]["lower_inclusive"] = "6"


def _open_not_last(d: dict[str, Any]) -> None:
    rows = component(d, "age_fee")["brackets"]
    rows[0]["upper_exclusive"] = None


def _co2_without_tables(d: dict[str, Any]) -> None:
    comp = component(d, "co2_charge")
    comp["brackets"] = comp.pop("tables")["wltp"]


def _tables_on_other_input(d: dict[str, Any]) -> None:
    comp = component(d, "age_fee")
    comp["tables"] = {"wltp": comp.pop("brackets")}


def _currency_mismatch(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["currency"] = "EUR"


def _id_shadows_input(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["id"] = "fuel"


def _lonely_group(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["alternative_group"] = "lonely"


def _rate_of_base_without_base(d: dict[str, Any]) -> None:
    component(d, "co2_charge")["base"] = []


def _ordering_on_text(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["applies_when"] = [
        {"input": "preferential_origin_country", "op": "lt", "value": "DE"}
    ]


def _list_on_number(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["applies_when"] = [{"input": "co2_g_km", "op": "in", "value": ["120"]}]


def _non_money_base(d: dict[str, Any]) -> None:
    component(d, "duty_standard")["base"] = ["co2_g_km"]


def _bad_predicate_number(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["applies_when"] = [{"input": "co2_g_km", "op": "gt", "value": "abc"}]


def _money_predicate(d: dict[str, Any]) -> None:
    component(d, "processing_fee")["applies_when"] = [{"input": "customs_value", "op": "gt", "value": "1"}]


def _undeclared_unit(d: dict[str, Any]) -> None:
    d["input_units"]["power_kw"] = "kW"


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_dup_id, "duplicate component id"),
        (_forward_ref, "earlier component"),
        (_base_not_in_depends, "depends_on"),
        (_undeclared_predicate, "not declared"),
        (_unknown_input, "unknown input name"),
        (_missing_unit, "unit declaration"),
        (_wrong_unit, "does not match engine unit"),
        (_unsorted, "sorted"),
        (_overlap, "overlap"),
        (_gap, "not contiguous"),
        (_open_not_last, "open-ended"),
        (_co2_without_tables, "keyed by CO2 cycle"),
        (_tables_on_other_input, "only valid for co2_g_km"),
        (_currency_mismatch, "differs from rule currency"),
        (_id_shadows_input, "collides with an input"),
        (_lonely_group, "at least two"),
        (_rate_of_base_without_base, "rate_of_base needs"),
        (_ordering_on_text, "not valid for text"),
        (_list_on_number, "only valid for text"),
        (_non_money_base, "not a money input"),
        (_bad_predicate_number, "not a decimal"),
        (_money_predicate, "cannot test money"),
        (_undeclared_unit, "undeclared input"),
    ],
)
def test_validation_problems(mutate: Callable[[dict[str, Any]], None], needle: str) -> None:
    doc = fixture_doc()
    doc["sha256"] = None
    mutate(doc)
    rule_set = RuleSet.model_validate(doc)
    problems = rule_set_problems(rule_set)
    assert any(needle in p for p in problems), problems
    with pytest.raises(ValidationFailed) as exc:
        validate_rule_set(rule_set)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_gap_allowed_when_table_declared_non_contiguous() -> None:
    def mutate(d: dict[str, Any]) -> None:
        _gap(d)
        component(d, "age_fee")["contiguous"] = False

    rule_set = variant(mutate)
    calc = calculate(rule_set, inputs(vehicle_age_years=Decimal("5.5")), T0)
    age = next(c for c in calc.components if c.component_id == "age_fee")
    assert age.status == ComponentStatus.UNKNOWN
    assert any("VALUE_OUTSIDE_BRACKETS" in w for w in age.warnings)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("duty_standard", "rate"), "-0.10"),
        (("duty_standard", "rate"), "1.5"),
        (("processing_fee", "amount"), "-1"),
        (("displacement_fee", "threshold"), "-1"),
        (("displacement_fee", "amount_per_unit"), "-0.5"),
    ],
)
def test_negative_or_out_of_range_rule_values_rejected(path: tuple[str, str], value: str) -> None:
    doc = fixture_doc()
    component(doc, path[0])[path[1]] = value
    with pytest.raises(ValidationError):
        RuleSet.model_validate(doc)


@pytest.mark.parametrize(
    "row",
    [
        {"lower_inclusive": "-5", "upper_exclusive": "5", "amount": "0"},
        {"lower_inclusive": "5", "upper_exclusive": "5", "amount": "0"},
        {"lower_inclusive": "0", "upper_exclusive": "5"},
        {"lower_inclusive": "0", "upper_exclusive": "5", "amount": "1", "rate_of_base": "0.1"},
        {"lower_inclusive": "0", "upper_exclusive": "5", "amount": "-1"},
    ],
)
def test_invalid_bracket_rows_rejected(row: dict[str, Any]) -> None:
    doc = fixture_doc()
    component(doc, "age_fee")["brackets"][0] = row
    with pytest.raises(ValidationError):
        RuleSet.model_validate(doc)


def test_validity_and_approval_pairs_are_structural() -> None:
    doc = fixture_doc()
    with pytest.raises(ValidationError, match="valid_to"):
        RuleSet.model_validate({**doc, "valid_to": "2026-01-01"})
    with pytest.raises(ValidationError, match="valid_to"):
        RuleSet.model_validate({**doc, "valid_from": None})
    with pytest.raises(ValidationError, match="together"):
        RuleSet.model_validate({**doc, "approved_by": "someone"})
    with pytest.raises(ValidationError):
        RuleSet.model_validate({**doc, "missing_input_behavior": "assume_zero"})


def test_approved_status_without_evidence_lists_every_gap() -> None:
    doc = fixture_doc()
    doc.update(status="approved", is_fixture=False, sha256=None, rounding_rules=None)
    problems = rule_set_problems(RuleSet.model_validate(doc))
    for needle in ("official sources", "approved_by", "content sha256", "review_record", "explicit rounding"):
        assert any(needle in p for p in problems), (needle, problems)


def test_fixture_cannot_hold_production_status() -> None:
    doc = fixture_doc()
    doc.update(status="active", sha256=None)
    assert any("fixture" in p for p in rule_set_problems(RuleSet.model_validate(doc)))


# --------------------------------------------------------------------------- lifecycle

_DB_TRIGGER_TRANSITIONS = {
    "draft": {"under_review", "revoked"},
    "under_review": {"draft", "approved", "revoked"},
    "approved": {"active", "superseded", "expired", "revoked"},
    "active": {"superseded", "expired", "revoked"},
    "unapproved": {"revoked"},
    "superseded": set(),
    "expired": set(),
    "revoked": set(),
}


@pytest.mark.parametrize("current", list(TaxRuleStatus))
@pytest.mark.parametrize("target", list(TaxRuleStatus))
def test_transition_table_matches_database_trigger(current: TaxRuleStatus, target: TaxRuleStatus) -> None:
    expected = target.value in _DB_TRIGGER_TRANSITIONS[current.value]
    assert can_transition(current, target) is expected
    assert (target in ALLOWED_TRANSITIONS[current]) is expected


def test_full_lifecycle_to_active_binds_hash_and_approver() -> None:
    active = make_active()
    assert active.status == TaxRuleStatus.ACTIVE
    assert active.sha256 == compute_rule_set_sha256(active)
    assert active.review_record is not None and active.review_record.content_sha256 == active.sha256
    assert active.approved_by == "SYNTHETIC owner" and active.approved_at == T0
    validate_rule_set(active)
    expired = transition_rule_set(active, TaxRuleStatus.EXPIRED, at=T0)
    assert expired.status == TaxRuleStatus.EXPIRED and expired.sha256 == active.sha256


def _under_review(**doc_updates: Any) -> RuleSet:
    doc = fixture_doc()
    doc.update(status="under_review", is_fixture=False, sha256=None, sources=[SYNTHETIC_SOURCE.model_dump()])
    doc.update(doc_updates)
    return RuleSet.model_validate(doc)


def _review(rule_set: RuleSet, content_hash: str | None = None) -> ReviewRecord:
    return ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=T0,
        content_sha256=content_hash or compute_rule_set_sha256(rule_set),
        scope="SYNTHETIC review",
    )


def test_approval_requires_reviewer_binding_hash_and_sources() -> None:
    pending = _under_review()
    with pytest.raises(ValidationFailed, match="approved_by"):
        transition_rule_set(pending, TaxRuleStatus.APPROVED, at=T0, review_record=_review(pending))
    with pytest.raises(ValidationFailed, match="approved_by"):
        transition_rule_set(pending, TaxRuleStatus.APPROVED, at=T0, approved_by="owner")
    with pytest.raises(ValidationFailed, match="content hash"):
        transition_rule_set(
            pending,
            TaxRuleStatus.APPROVED,
            at=T0,
            approved_by="owner",
            review_record=_review(pending, "f" * 64),
        )
    sourceless = _under_review(sources=[])
    with pytest.raises(ValidationFailed) as exc:
        transition_rule_set(
            sourceless, TaxRuleStatus.APPROVED, at=T0, approved_by="owner", review_record=_review(sourceless)
        )
    assert any("official sources" in p for p in exc.value.details["problems"])
    fixture = _under_review(is_fixture=True)
    with pytest.raises(ValidationFailed, match="fixture"):
        transition_rule_set(
            fixture, TaxRuleStatus.APPROVED, at=T0, approved_by="owner", review_record=_review(fixture)
        )


def test_illegal_transitions_and_stray_approval_data_refused() -> None:
    pending = _under_review()
    with pytest.raises(ValidationFailed, match="not permitted"):
        transition_rule_set(pending, TaxRuleStatus.ACTIVE, at=T0)
    with pytest.raises(ValidationFailed, match="only be supplied"):
        transition_rule_set(pending, TaxRuleStatus.DRAFT, at=T0, approved_by="owner")
    active = make_active()
    revoked = transition_rule_set(active, TaxRuleStatus.REVOKED, at=T0)
    for target in TaxRuleStatus:
        with pytest.raises(ValidationFailed):
            transition_rule_set(revoked, target, at=T0)


def test_activation_refuses_ambiguous_overlap() -> None:
    first = make_active(version="v1")
    with pytest.raises(ValidationFailed, match="overlapping"):
        make_active(version="v2", valid_from="2026-06-01", valid_to=None, existing=(first,))
    second = make_active(version="v2", valid_from="2027-01-01", valid_to=None, existing=(first,))
    assert find_active_overlaps([first, second]) == []
    assert find_active_overlaps([first, make_active(version="v3")]) == [
        (first.label(), "SYNTHETIC-TEST-ONLY-xx-passenger-import@v3")
    ]


# --------------------------------------------------------------------------- selection


def test_production_selects_only_active_non_fixture() -> None:
    fixture = load_rule_set_file(FIXTURE)
    active = make_active()
    approved = active.model_copy(update={"status": TaxRuleStatus.APPROVED, "version": "approved-only"})
    approved = seal_rule_set(approved)
    selection = select_rule_set([fixture, approved, active], "XX", CATEGORY, DECL)
    assert selection.rule_set is active
    assert "not production-ready" not in selection.reason
    only_inactive = select_rule_set([fixture], "XX", CATEGORY, DECL)
    assert only_inactive.rule_set is None
    assert "import costs unknown" in only_inactive.reason
    assert any("not active for production" in c for c in only_inactive.considered)


@pytest.mark.parametrize(
    ("on", "selected"),
    [
        (date(2025, 12, 31), False),
        (date(2026, 1, 1), True),
        (date(2026, 12, 31), True),
        (date(2027, 1, 1), False),
    ],
)
def test_selection_dates_around_effective_period(on: date, selected: bool) -> None:
    active = make_active()
    assert (select_rule_set([active], "XX", CATEGORY, on).rule_set is not None) is selected


def test_selection_switches_versions_at_effective_change() -> None:
    v1 = make_active(version="v1", valid_from="2026-01-01", valid_to="2026-07-01")
    v2 = make_active(version="v2", valid_from="2026-07-01", valid_to=None, existing=(v1,))
    assert select_rule_set([v1, v2], "XX", CATEGORY, date(2026, 6, 30)).rule_set is v1
    assert select_rule_set([v1, v2], "XX", CATEGORY, date(2026, 7, 1)).rule_set is v2


@pytest.mark.parametrize("status", [TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED, TaxRuleStatus.SUPERSEDED])
def test_closed_rule_sets_are_never_selected(status: TaxRuleStatus) -> None:
    closed = transition_rule_set(make_active(), status, at=T0)
    for allow in (False, True):
        result = select_rule_set([closed], "XX", CATEGORY, DECL, allow_unapproved=allow)
        assert result.rule_set is None
        assert any(status.value in c for c in result.considered)


def test_ambiguous_active_versions_raise() -> None:
    one = make_active(version="a")
    two = make_active(version="b")  # built without `existing`, so overlap was not checked
    with pytest.raises(ValidationFailed, match="ambiguous") as exc:
        select_rule_set([one, two], "XX", CATEGORY, DECL)
    assert len(exc.value.details["candidates"]) == 2


def test_tampered_active_rule_set_fails_closed() -> None:
    active = make_active()
    tampered = active.model_copy(update={"description": "edited after approval"})
    with pytest.raises(ValidationFailed) as exc:
        select_rule_set([tampered], "XX", CATEGORY, DECL)
    assert any("sha256" in p for p in exc.value.details["problems"])


def test_unapproved_selection_is_labelled_and_never_production_ready() -> None:
    fixture = load_rule_set_file(FIXTURE)
    selection = select_rule_set([fixture], "XX", CATEGORY, DECL, allow_unapproved=True)
    assert selection.rule_set is fixture
    assert "NOT production-ready" in selection.reason
    calc = calculate(fixture, inputs(), T0)
    assert calc.complete
    assert calc.rule_status == TaxRuleStatus.UNAPPROVED and calc.is_fixture
    assert not calc.production_ready
    assert any("FIXTURE_RULE_SET" in w for w in calc.warnings)
    assert any("RULE_SET_NOT_ACTIVE" in w for w in calc.warnings)


def test_selection_filters_jurisdiction_and_category() -> None:
    active = make_active()
    assert select_rule_set([active], "MK", CATEGORY, DECL).rule_set is None
    assert select_rule_set([active], "XX", "motorcycle", DECL).rule_set is None


# --------------------------------------------------------------------------- calculation


def test_golden_complete_calculation() -> None:
    calc = calculate(make_active(), inputs(), T0)
    assert amounts(calc) == {
        "duty_listed_origin": None,
        "duty_standard": Decimal("10000.00"),
        "co2_charge": Decimal("1000"),
        "age_fee": Decimal("1500.00"),
        "displacement_fee": Decimal("197.50"),
        "processing_fee": Decimal("123.45"),
        "vat": Decimal("22200.00"),
    }
    assert statuses(calc)["duty_listed_origin"] == ComponentStatus.NOT_APPLICABLE
    assert calc.complete and calc.production_ready
    assert calc.total_import_cost == Money.of("35020.95", "MKD")
    assert calc.known_subtotal is None and calc.unknown_components == ()
    vat = next(c for c in calc.components if c.component_id == "vat")
    assert vat.inputs_used["duty_listed_origin"].startswith("not_applicable")
    assert vat.inputs_used["rate"] == "0.20"
    assert vat.rule_set_id == calc.rule_set_id and vat.version == "synthetic-active-1"
    totals = calc.category_totals()
    assert totals[CostCategory.OTHER_IMPORT_CHARGES].amount == Money.of("1821.00", "MKD") - Money.of(
        "0.05", "MKD"
    )
    assert totals[CostCategory.IMPORT_DUTY].status == ComponentStatus.RESOLVED


@pytest.mark.parametrize(
    ("cycle", "co2", "expected"),
    [
        (Co2Cycle.WLTP, "0", "1000"),
        (Co2Cycle.WLTP, "99.99", "1000"),
        (Co2Cycle.WLTP, "100", "0"),
        (Co2Cycle.WLTP, "149.99", "2500"),  # 49.99 * 50 = 2499.5 -> half_up to 1
        (Co2Cycle.WLTP, "150", "5000"),  # 0.05 * customs value
        (Co2Cycle.NEDC, "89.99", "1200"),
        (Co2Cycle.NEDC, "90", "0"),
        (Co2Cycle.NEDC, "139", "2940"),
        (Co2Cycle.NEDC, "140", "9000"),
        (Co2Cycle.NEDC_CORRELATED, "94.99", "1100"),
        (Co2Cycle.NEDC_CORRELATED, "95", "5000"),
        (Co2Cycle.NEDC_CORRELATED, "400", "5000"),
    ],
)
def test_co2_bracket_boundaries_per_cycle(cycle: Co2Cycle, co2: str, expected: str) -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(co2_cycle=cycle, co2_g_km=Decimal(co2)), T0)
    assert amounts(calc)["co2_charge"] == Decimal(expected)
    assert calc.complete


def test_same_co2_value_differs_by_cycle_without_conversion() -> None:
    rule = load_rule_set_file(FIXTURE)
    results = {
        cycle: amounts(calculate(rule, inputs(co2_cycle=cycle, co2_g_km=Decimal("92")), T0))["co2_charge"]
        for cycle in (Co2Cycle.WLTP, Co2Cycle.NEDC, Co2Cycle.NEDC_CORRELATED)
    }
    assert results == {
        Co2Cycle.WLTP: Decimal("1000"),
        Co2Cycle.NEDC: Decimal("120"),
        Co2Cycle.NEDC_CORRELATED: Decimal("1100"),
    }


def test_unknown_co2_cycle_makes_charge_and_dependants_unknown() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(co2_cycle=Co2Cycle.UNKNOWN), T0)
    st = statuses(calc)
    assert st["co2_charge"] == ComponentStatus.UNKNOWN
    assert st["vat"] == ComponentStatus.UNKNOWN  # VAT base lists the CO2 charge
    assert not calc.complete and calc.total_import_cost is None
    assert "co2_cycle" in calc.missing_inputs
    assert set(calc.unknown_components) == {"co2_charge", "vat"}
    assert calc.known_subtotal == Money.of("11821.00", "MKD") - Money.of("0.05", "MKD")
    co2 = next(c for c in calc.components if c.component_id == "co2_charge")
    assert co2.amount is None and co2.unrounded_amount is None
    assert any("no WLTP/NEDC conversion" in w for w in co2.warnings)


def test_unsupported_cycle_is_unknown_not_converted() -> None:
    def drop_nedc(d: dict[str, Any]) -> None:
        del component(d, "co2_charge")["tables"]["nedc"]

    calc = calculate(variant(drop_nedc), inputs(co2_cycle=Co2Cycle.NEDC), T0)
    co2 = next(c for c in calc.components if c.component_id == "co2_charge")
    assert co2.status == ComponentStatus.UNKNOWN
    assert any("CO2_CYCLE_NOT_SUPPORTED:nedc" in w for w in co2.warnings)
    assert not calc.complete


def test_missing_co2_value_is_unknown() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(co2_g_km=None), T0)
    assert statuses(calc)["co2_charge"] == ComponentStatus.UNKNOWN
    assert "co2_g_km" in calc.missing_inputs


@pytest.mark.parametrize(
    ("age", "fee"),
    [
        ("0", "0.00"),
        ("4.99", "0.00"),
        ("5", "500.00"),
        ("9.99", "500.00"),
        ("10", "1500.00"),
        ("60", "1500.00"),
    ],
)
def test_age_bracket_boundaries(age: str, fee: str) -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(vehicle_age_years=Decimal(age)), T0)
    assert amounts(calc)["age_fee"] == Decimal(fee)


@pytest.mark.parametrize(
    ("displacement", "fee"), [("1200", "0.00"), ("1600", "0.00"), ("1600.5", "0.25"), ("1601", "0.50")]
)
def test_per_unit_threshold_boundaries(displacement: str, fee: str) -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(engine_displacement_cm3=Decimal(displacement)), T0)
    assert amounts(calc)["displacement_fee"] == Decimal(fee)


def test_missing_optional_input_still_makes_component_unknown() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(engine_displacement_cm3=None), T0)
    assert statuses(calc)["displacement_fee"] == ComponentStatus.UNKNOWN
    assert calc.missing_inputs == ()  # optional input; but the result is still incomplete
    assert not calc.complete and calc.total_import_cost is None
    assert calc.known_subtotal is not None


# --------------------------------------------------------------------------- origin


def test_accepted_preferential_proof_from_listed_country() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(origin_proof=accepted_proof("DE")), T0)
    assert statuses(calc)["duty_listed_origin"] == ComponentStatus.RESOLVED
    assert statuses(calc)["duty_standard"] == ComponentStatus.NOT_APPLICABLE
    assert amounts(calc)["duty_listed_origin"] == Decimal("2000.00")


def test_german_purchase_without_proof_never_implies_preferential_or_zero_duty() -> None:
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(origin_proof=None, seller_country="DE", dispatch_country="DE", origin_country="DE"),
        T0,
    )
    st = statuses(calc)
    assert st["duty_listed_origin"] == ComponentStatus.UNKNOWN
    assert st["duty_standard"] == ComponentStatus.UNKNOWN
    assert "origin_evidence" in calc.missing_inputs
    assert calc.total_import_cost is None
    duty = next(c for c in calc.components if c.component_id == "duty_standard")
    assert any("never derived from seller/dispatch" in w for w in duty.warnings)


def test_swiss_origin_is_not_interchangeable_with_listed_countries() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(origin_proof=accepted_proof("CH")), T0)
    assert statuses(calc)["duty_listed_origin"] == ComponentStatus.NOT_APPLICABLE
    assert amounts(calc)["duty_standard"] == Decimal("10000.00")
    assert calc.components[1].inputs_used["preferential_origin_country"] == "CH"


def test_swiss_purchase_without_proof_never_implies_eu_preferential_origin() -> None:
    # Bought from a Swiss seller and dispatched from Switzerland: CH is non-EU and the purchase
    # country is never origin evidence, so nothing resolves to the listed (preferential) rate.
    swiss = {"seller_country": "CH", "dispatch_country": "CH"}
    unproven = calculate(
        load_rule_set_file(FIXTURE), inputs(origin_proof=None, origin_country="DE", **swiss), T0
    )
    st = statuses(unproven)
    assert st["duty_listed_origin"] == ComponentStatus.UNKNOWN
    assert st["duty_standard"] == ComponentStatus.UNKNOWN
    assert "origin_evidence" in unproven.missing_inputs
    assert unproven.total_import_cost is None
    # Positively no proof: the standard duty applies, never the listed-country preference.
    no_preference = calculate(load_rule_set_file(FIXTURE), inputs(origin_proof=no_proof(), **swiss), T0)
    assert statuses(no_preference)["duty_listed_origin"] == ComponentStatus.NOT_APPLICABLE
    assert amounts(no_preference)["duty_standard"] == Decimal("10000.00")
    used = next(c for c in no_preference.components if c.component_id == "duty_standard").inputs_used
    assert used["preferential_origin_country"] == "none"


@pytest.mark.parametrize(
    ("proof", "listed", "standard"),
    [
        (OriginProof(proof_type="x", acceptance_status=OriginProofStatus.PENDING), "unknown", "unknown"),
        (OriginProof(proof_type="x", acceptance_status=OriginProofStatus.UNKNOWN), "unknown", "unknown"),
        (
            OriginProof(proof_type="x", acceptance_status=OriginProofStatus.REJECTED),
            "not_applicable",
            "resolved",
        ),
        (accepted_proof("DE", preferential=None), "unknown", "unknown"),
        (accepted_proof("DE", preferential=False), "not_applicable", "resolved"),
        (
            accepted_proof("DE", valid_from=date(2025, 1, 1), valid_to=date(2025, 12, 31)),
            "not_applicable",
            "resolved",
        ),
        (
            accepted_proof("DE", valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31)),
            "resolved",
            "not_applicable",
        ),
    ],
)
def test_origin_proof_states(proof: OriginProof, listed: str, standard: str) -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(origin_proof=proof), T0)
    assert statuses(calc)["duty_listed_origin"].value == listed
    assert statuses(calc)["duty_standard"].value == standard


def test_proof_validity_unverifiable_without_declaration_date() -> None:
    proof = accepted_proof("DE", valid_from=date(2026, 1, 1))
    calc = calculate(load_rule_set_file(FIXTURE), inputs(origin_proof=proof, declaration_date=None), T0)
    assert statuses(calc)["duty_listed_origin"] == ComponentStatus.UNKNOWN
    assert "declaration_date" in calc.missing_inputs
    assert any("DECLARATION_DATE_UNKNOWN" in w for w in calc.warnings)


def test_origin_proof_model_rules() -> None:
    with pytest.raises(ValidationError, match="evidence_ids"):
        OriginProof(proof_type="x", origin_country="DE", acceptance_status=OriginProofStatus.ACCEPTED)
    with pytest.raises(ValidationError, match="precedes"):
        OriginProof(proof_type="x", valid_from=date(2026, 2, 1), valid_to=date(2026, 1, 1))


def _gap_group(d: dict[str, Any]) -> None:
    component(d, "duty_standard")["applies_when"] = [
        {"input": "preferential_origin_country", "op": "eq", "value": "none"}
    ]


def _overlapping_group(d: dict[str, Any]) -> None:
    component(d, "duty_standard")["applies_when"] = []


def test_alternative_group_gap_is_unknown_not_zero() -> None:
    calc = calculate(variant(_gap_group), inputs(origin_proof=accepted_proof("CH")), T0)
    assert statuses(calc)["duty_listed_origin"] == ComponentStatus.UNKNOWN
    assert statuses(calc)["duty_standard"] == ComponentStatus.UNKNOWN
    assert any("ALTERNATIVE_GROUP_GAP:duty" in w for w in calc.components[0].warnings)
    assert not calc.complete


def test_alternative_group_ambiguity_is_unknown() -> None:
    calc = calculate(variant(_overlapping_group), inputs(origin_proof=accepted_proof("DE")), T0)
    assert statuses(calc)["duty_standard"] == ComponentStatus.UNKNOWN
    assert any("ALTERNATIVE_GROUP_AMBIGUOUS" in w for w in calc.components[1].warnings)


# --------------------------------------------------------------------------- customs value and FX


def test_customs_value_differs_from_invoice_price() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(invoice_price=Money.of("50000.00", "MKD")), T0)
    assert amounts(calc)["duty_standard"] == Decimal("10000.00")  # 10% of customs value, not invoice
    assert "100000.00 MKD" in calc.components[1].inputs_used["customs_value"]


def test_missing_customs_value_never_falls_back_to_invoice() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(customs_value=None), T0)
    assert statuses(calc)["duty_standard"] == ComponentStatus.UNKNOWN
    assert "customs_value" in calc.missing_inputs
    assert any("invoice price is never substituted" in w for w in calc.components[1].warnings)


def test_customs_value_without_basis_is_missing() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(customs_value_basis=None), T0)
    assert statuses(calc)["duty_standard"] == ComponentStatus.UNKNOWN
    assert any("CUSTOMS_VALUE_BASIS_MISSING" in w for w in calc.components[1].warnings)


def test_customs_fx_multiplies_in_stored_direction() -> None:
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(customs_value=Money.of("3000.00", "EUR"), customs_fx_rate=customs_rate()),
        T0,
    )
    assert amounts(calc)["duty_standard"] == Decimal("18450.00")  # 3000 * 61.5 * 0.10
    assert calc.customs_fx_rate == customs_rate()
    assert any("CUSTOMS_FX_EFFECTIVE_PERIOD_NOT_RECORDED" in w for w in calc.components[1].warnings)


def test_customs_fx_divides_for_inverse_quote() -> None:
    rate = customs_rate(base="MKD", quote="EUR", rate="0.016")
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(customs_value=Money.of("3000.00", "EUR"), customs_fx_rate=rate),
        T0,
    )
    assert amounts(calc)["duty_standard"] == Decimal("18750.00")  # 3000 / 0.016 * 0.10


@pytest.mark.parametrize(
    ("rate", "extra", "warning"),
    [
        (None, {}, "CUSTOMS_FX_RATE_MISSING"),
        (customs_rate(purpose=FxPurpose.REFERENCE), {}, "CUSTOMS_FX_RATE_WRONG_PURPOSE"),
        (customs_rate(purpose=FxPurpose.PAYMENT), {}, "CUSTOMS_FX_RATE_WRONG_PURPOSE"),
        (customs_rate(quote="CHF"), {}, "CUSTOMS_FX_RATE_PAIR_MISMATCH"),
        (customs_rate(), {"customs_fx_valid_from": date(2026, 6, 2)}, "CUSTOMS_FX_RATE_NOT_EFFECTIVE"),
        (customs_rate(), {"customs_fx_valid_to": date(2026, 5, 31)}, "CUSTOMS_FX_RATE_NOT_EFFECTIVE"),
    ],
)
def test_customs_conversion_requires_customs_rate(
    rate: FxRate | None, extra: dict[str, Any], warning: str
) -> None:
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(customs_value=Money.of("3000.00", "EUR"), customs_fx_rate=rate, **extra),
        T0,
    )
    duty = calc.components[1]
    assert duty.status == ComponentStatus.UNKNOWN
    assert any(warning in w for w in duty.warnings)
    assert not calc.complete and calc.customs_fx_rate is None


def test_customs_rate_within_effective_period() -> None:
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(
            customs_value=Money.of("3000.00", "EUR"),
            customs_fx_rate=customs_rate(),
            customs_fx_valid_from=date(2026, 6, 1),
            customs_fx_valid_to=date(2026, 6, 7),
        ),
        T0,
    )
    assert calc.complete
    assert not any("PERIOD" in w for w in calc.components[1].warnings)


def test_included_costs_total_input() -> None:
    def add_included(d: dict[str, Any]) -> None:
        d["optional_inputs"].append("included_costs_total")
        component(d, "duty_standard")["base"] = ["customs_value", "included_costs_total"]

    rule = variant(add_included)
    unknown = calculate(rule, inputs(), T0)
    assert statuses(unknown)["duty_standard"] == ComponentStatus.UNKNOWN
    none_included = calculate(rule, inputs(included_costs=()), T0)
    assert amounts(none_included)["duty_standard"] == Decimal("10000.00")
    included = (
        IncludedCost(
            label="SYNTHETIC freight to border",
            amount=Money.of("100.00", "EUR"),
            legal_basis="SYNTHETIC valuation article",
        ),
    )
    converted = calculate(rule, inputs(included_costs=included, customs_fx_rate=customs_rate()), T0)
    assert amounts(converted)["duty_standard"] == Decimal("10615.00")  # (100000 + 6150) * 0.10
    with pytest.raises(ValidationError):
        IncludedCost(label="x", amount=Money.of("1", "EUR"), legal_basis="")


# --------------------------------------------------------------------------- rounding


def _co2_stage(stage: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        component(d, "co2_charge")["rounding"]["stage"] = stage

    return mutate


def test_rounding_order_before_dependents_vs_reported_only() -> None:
    values = inputs(co2_g_km=Decimal("120.33"))  # 20.33 * 50 = 1016.5
    before = calculate(variant(_co2_stage("before_dependents")), values, T0)
    reported = calculate(variant(_co2_stage("reported_only")), values, T0)
    assert amounts(before)["co2_charge"] == amounts(reported)["co2_charge"] == Decimal("1017")
    assert amounts(before)["vat"] == Decimal("22203.40")  # (100000 + 10000 + 1017) * 0.2
    assert amounts(reported)["vat"] == Decimal("22203.30")  # (100000 + 10000 + 1016.5) * 0.2
    co2 = next(c for c in before.components if c.component_id == "co2_charge")
    assert co2.unrounded_amount == Decimal("1016.50") and co2.rounding is not None


def test_total_rounding_applies_after_summing_reported_amounts() -> None:
    def total(d: dict[str, Any]) -> None:
        d["rounding_rules"]["total"] = {"quantum": "1", "mode": "half_up"}

    calc = calculate(variant(total), inputs(), T0)
    assert calc.total_import_cost == Money.of("35021", "MKD")


@pytest.mark.parametrize(
    ("value", "quantum", "mode", "expected"),
    [
        ("2.5", "1", "half_up", "3"),
        ("2.5", "1", "half_even", "2"),
        ("3.5", "1", "half_even", "4"),
        ("2.9", "1", "down", "2"),
        ("2.1", "1", "up", "3"),
        ("1234", "10", "half_up", "1230"),
        ("1235", "10", "half_up", "1240"),
        ("1.024", "0.05", "half_up", "1.00"),
        ("1.025", "0.05", "half_up", "1.05"),
        ("0.005", "0.01", "half_up", "0.01"),
        ("0.005", "0.01", "half_even", "0.00"),
    ],
)
def test_apply_rounding_modes(value: str, quantum: str, mode: str, expected: str) -> None:
    spec = RoundingSpec(quantum=Decimal(quantum), mode=mode)
    assert apply_rounding(Decimal(value), spec) == Decimal(expected)


# --------------------------------------------------------------------------- inputs and guards


@pytest.mark.parametrize(
    "bad",
    [
        {"co2_g_km": Decimal("-1")},
        {"engine_displacement_cm3": Decimal("0")},
        {"engine_displacement_cm3": Decimal("-1600")},
        {"power_kw": Decimal("-10")},
        {"vehicle_age_years": Decimal("-1")},
        {"customs_value": Money.of("-1.00", "MKD")},
        {"invoice_price": Money.of("-1.00", "EUR")},
        {"jurisdiction": "mk"},
        {"customs_fx_valid_from": date(2026, 6, 2), "customs_fx_valid_to": date(2026, 6, 1)},
        {"exemptions": (ExemptionClaim(code="a"), ExemptionClaim(code="a"))},
    ],
)
def test_invalid_inputs_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        inputs(**bad)


def test_float_inputs_rejected() -> None:
    with pytest.raises(ValidationError, match="float"):
        TaxInputs(co2_g_km=120.5)


def test_unapproved_classification_is_a_missing_required_input() -> None:
    unapproved = Classification(tariff_code="8703 23", approval_status="unapproved")
    calc = calculate(load_rule_set_file(FIXTURE), inputs(classification=unapproved), T0)
    assert all(s != ComponentStatus.UNKNOWN for s in statuses(calc).values())
    assert calc.missing_inputs == ("classification",)
    assert not calc.complete and calc.total_import_cost is None
    assert calc.known_subtotal == Money.of("35020.95", "MKD")  # labelled subtotal, never a total
    assert all(t.status == ComponentStatus.UNKNOWN for t in calc.category_totals().values())
    with pytest.raises(ValidationError):
        Classification(tariff_code="8703", approval_status="approved")


def test_revoked_or_ineffective_rule_and_wrong_jurisdiction_refused() -> None:
    revoked = transition_rule_set(make_active(), TaxRuleStatus.REVOKED, at=T0)
    with pytest.raises(ValidationFailed, match="revoked"):
        calculate(revoked, inputs(), T0)
    with pytest.raises(ValidationFailed, match="not effective"):
        calculate(make_active(), inputs(declaration_date=date(2027, 1, 1)), T0)
    with pytest.raises(ValidationFailed, match="jurisdiction"):
        calculate(make_active(), inputs(jurisdiction="MK"), T0)
    with pytest.raises(ValueError, match="naive"):
        calculate(make_active(), inputs(), datetime(2026, 6, 1))


def test_expired_rule_recalculation_is_flagged_not_production_ready() -> None:
    expired = transition_rule_set(make_active(), TaxRuleStatus.EXPIRED, at=T0)
    calc = calculate(expired, inputs(), T0)
    assert calc.complete and not calc.production_ready
    assert any("RULE_SET_NOT_ACTIVE:expired" in w for w in calc.warnings)


def _exemption_component(d: dict[str, Any]) -> None:
    d["optional_inputs"].append("exemption_status:synthetic_relief")
    d["components"].append(
        {
            "id": "relief_dependent_fee",
            "kind": "fixed",
            "label": "SYNTHETIC fee waived by an accepted relief",
            "category": "other_import_charges",
            "currency": "MKD",
            "amount": "50",
            "applies_when": [{"input": "exemption_status:synthetic_relief", "op": "ne", "value": "accepted"}],
        }
    )


@pytest.mark.parametrize(
    ("exemptions", "status"),
    [
        (None, ComponentStatus.UNKNOWN),
        ((), ComponentStatus.RESOLVED),
        ((ExemptionClaim(code="synthetic_relief", acceptance_status="pending"),), ComponentStatus.UNKNOWN),
        (
            (ExemptionClaim(code="synthetic_relief", acceptance_status="accepted", proof_ids=("p",)),),
            ComponentStatus.NOT_APPLICABLE,
        ),
        ((ExemptionClaim(code="synthetic_relief", acceptance_status="rejected"),), ComponentStatus.RESOLVED),
    ],
)
def test_exemptions_need_accepted_proof(
    exemptions: tuple[ExemptionClaim, ...] | None, status: ComponentStatus
) -> None:
    calc = calculate(variant(_exemption_component), inputs(exemptions=exemptions), T0)
    assert statuses(calc)["relief_dependent_fee"] == status
    with pytest.raises(ValidationError):
        ExemptionClaim(code="x", acceptance_status="accepted")


def test_date_predicates() -> None:
    def dated(d: dict[str, Any]) -> None:
        component(d, "processing_fee")["applies_when"] = [
            {"input": "declaration_date", "op": "ge", "value": "2026-07-01"}
        ]

    rule = variant(dated)
    assert statuses(calculate(rule, inputs(), T0))["processing_fee"] == ComponentStatus.NOT_APPLICABLE
    july = calculate(rule, inputs(declaration_date=date(2026, 7, 1)), T0)
    assert statuses(july)["processing_fee"] == ComponentStatus.RESOLVED


def test_result_models_enforce_labels() -> None:
    calc = calculate(load_rule_set_file(FIXTURE), inputs(), T0)
    with pytest.raises(ValidationError, match="known_subtotal"):
        TaxCalculation.model_validate({**calc.model_dump(), "known_subtotal": calc.total_import_cost})
    incomplete = calculate(load_rule_set_file(FIXTURE), inputs(co2_cycle=Co2Cycle.UNKNOWN), T0)
    with pytest.raises(ValidationError, match="total_import_cost"):
        TaxCalculation.model_validate({**incomplete.model_dump(), "total_import_cost": Money.of("1", "MKD")})
    unknown = next(c for c in incomplete.components if c.status == ComponentStatus.UNKNOWN)
    with pytest.raises(ValidationError, match="resolved"):
        ComponentResult.model_validate({**unknown.model_dump(), "amount": Money.of("0", "MKD")})


# --------------------------------------------------------------------------- review regressions


def test_customs_rate_dated_after_declaration_is_never_used() -> None:
    late = customs_rate(rate_date=date(2026, 6, 2))  # observed after the 2026-06-01 declaration
    calc = calculate(
        load_rule_set_file(FIXTURE),
        inputs(customs_value=Money.of("3000.00", "EUR"), customs_fx_rate=late),
        T0,
    )
    duty = calc.components[1]
    assert duty.status == ComponentStatus.UNKNOWN
    assert any("CUSTOMS_FX_RATE_AFTER_DECLARATION" in w for w in duty.warnings)
    assert calc.customs_fx_rate is None and not calc.complete
    same_day = customs_rate(rate_date=DECL)
    ok = calculate(
        load_rule_set_file(FIXTURE),
        inputs(customs_value=Money.of("3000.00", "EUR"), customs_fx_rate=same_day),
        T0,
    )
    assert ok.components[1].status == ComponentStatus.RESOLVED


def test_absurd_amounts_fail_with_typed_errors_never_raw_decimal_errors() -> None:
    with pytest.raises(ValidationError, match="sanity bound"):
        inputs(customs_value=Money.of("1e60", "MKD"))
    with pytest.raises(ValidationError, match="sanity bound"):
        IncludedCost(label="x", amount=Money.of("1e13", "EUR"), legal_basis="SYNTHETIC basis")

    def huge_fee(d: dict[str, Any]) -> None:
        component(d, "processing_fee")["amount"] = "1e60"

    with pytest.raises(ValidationFailed, match="precision"):
        calculate(variant(huge_fee), inputs(), T0)
    with pytest.raises(ValidationFailed, match="precision"):
        apply_rounding(Decimal("1e60"), RoundingSpec(quantum=Decimal("0.01"), mode="half_up"))


def test_json_nan_and_infinity_constants_are_rejected() -> None:
    doc = fixture_doc()
    doc["sha256"] = None
    text = json.dumps(doc, default=str)
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValidationFailed, match="JSON"):
            parse_rule_set_json(text.replace('"rate": "0.10"', f'"rate": {constant}'))


def test_boolean_predicate_values_are_rejected() -> None:
    for value in (True, False, [True]):
        with pytest.raises(ValidationError, match="boolean"):
            Predicate(input="co2_g_km", op="gt", value=value)


def test_co2_cycle_tables_require_declared_cycle_input() -> None:
    doc = fixture_doc()
    doc["sha256"] = None
    doc["required_inputs"].remove("co2_cycle")
    problems = rule_set_problems(RuleSet.model_validate(doc))
    assert any("co2_cycle declared" in p for p in problems), problems


def test_fixture_rule_sets_never_enter_review() -> None:
    draft = RuleSet.model_validate({**fixture_doc(), "status": "draft", "sha256": None})
    assert draft.is_fixture
    with pytest.raises(ValidationFailed, match="fixture"):
        transition_rule_set(draft, TaxRuleStatus.UNDER_REVIEW, at=T0)
    assert transition_rule_set(draft, TaxRuleStatus.REVOKED, at=T0).status == TaxRuleStatus.REVOKED


def test_calculation_content_hash_binds_inputs_and_results() -> None:
    rule = load_rule_set_file(FIXTURE)
    first = calculate(rule, inputs(), T0)
    assert first.content_sha256() == calculate(rule, inputs(), T0).content_sha256()
    assert len(first.content_sha256()) == 64
    other = calculate(rule, inputs(customs_value=Money.of("100001.00", "MKD")), T0)
    assert other.content_sha256() != first.content_sha256()


def test_rule_set_for_another_vehicle_category_is_refused() -> None:
    motorcycle = Classification(
        tariff_code="8711 20",
        vehicle_category="motorcycle",
        evidence_ids=("SYNTHETIC-classification-evidence",),
        approval_status="approved",
        approved_by="SYNTHETIC owner",
    )
    with pytest.raises(ValidationFailed, match="category"):
        calculate(load_rule_set_file(FIXTURE), inputs(classification=motorcycle), T0)
