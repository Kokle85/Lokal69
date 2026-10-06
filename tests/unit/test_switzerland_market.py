"""Switzerland (CH) as an acquisition market: cost-profile scoping (spec sections 3, 16, 17, 18).

The CH-scoped profile lines carry no amounts (unknown is never zero) and apply only to a
purchase in Switzerland. All data here is SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    CostScope,
    ProceedsEstimate,
    PurchaseInput,
    ScenarioSet,
    compute_scenarios,
    load_cost_profile,
)
from suv_deals.domain.enums import CostCategory, CostLineStatus
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import ContributionThreshold
from suv_deals.domain.tax_engine import IMPORT_CATEGORIES

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "config" / "cost_profiles" / "default_unapproved.yaml"
AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
CH_LABEL_PREFIX = "CH purchase:"


def _scenarios(target: CostScope | None, *, lines_target: CostScope | None) -> ScenarioSet:
    profile = load_cost_profile(PROFILE)
    return compute_scenarios(
        PurchaseInput(status=CostLineStatus.ESTIMATED, amount=Money.of("2800.00", "EUR")),
        list(profile.lines(lines_target)),
        ProceedsEstimate(
            status=CostLineStatus.ESTIMATED,
            currency="EUR",
            base=Money.of("8000.00", "EUR"),
            basis="owner_estimate",
        ),
        [],
        ContributionThreshold(),
        as_of=AS_OF,
        target_scope=target,
    )


def test_ch_lines_load_unknown_scoped_and_labelled() -> None:
    profile = load_cost_profile(PROFILE)
    assert profile.approval_status == "unapproved" and profile.version == 2
    ch = [a for a in profile.assumptions if a.scope is not None and a.scope.origin_country == "CH"]
    assert {(a.category, a.label.startswith(CH_LABEL_PREFIX)) for a in ch} == {
        (CostCategory.CUSTOMS_BROKER, True),
        (CostCategory.EXPORT_PLATES_INSURANCE, True),
        (CostCategory.REFUNDABLE_DEPOSIT, True),
    }
    assert sum(a.category == CostCategory.CUSTOMS_BROKER for a in ch) == 2  # export declaration + transit
    for assumption in ch:
        assert assumption.status == CostLineStatus.UNKNOWN
        assert (assumption.low, assumption.base, assumption.high) == (None, None, None)
        assert assumption.note.startswith("ch_purchase")
        assert assumption.scope is not None and "ch_purchase" in (assumption.scope.note or "")
    # Scoped lines are additions: every unscoped line is still there and covers every category.
    general = [a for a in profile.assumptions if a.scope is None]
    assert {a.category for a in general} == REQUIRED_CATEGORIES
    assert all(not a.label.startswith(CH_LABEL_PREFIX) for a in general)


def test_ch_lines_never_model_import_tax() -> None:
    # Origin and duty stay in the tax engine: no CH line uses an import category (a manual import
    # line would also be a stray source next to a recorded tax calculation).
    profile = load_cost_profile(PROFILE)
    ch = [a for a in profile.assumptions if a.scope is not None]
    assert not {a.category for a in ch} & IMPORT_CATEGORIES
    notes = " ".join(profile.notes)
    assert "does NOT carry EU preferential origin" in notes
    assert "never hardcoded" in notes  # the Swiss VAT rate


def test_ch_notes_cover_export_transit_plates_and_vat() -> None:
    profile = load_cost_profile(PROFILE)
    text = " ".join(a.note for a in profile.assumptions if a.scope is not None)
    for topic in ("Ausfuhrdeklaration", "export declaration", "T1", "guarantee", "export plates", "truck"):
        assert topic in text or topic in " ".join(a.label for a in profile.assumptions), topic
    assert "never assumed" in text  # invoicing an export sale without Swiss VAT
    assert "counted twice" in text  # CH forms of general lines say which one to drop


def test_profile_lines_follow_the_purchase_country() -> None:
    profile = load_cost_profile(PROFILE)
    everything = profile.lines()
    german = profile.lines(CostScope(origin_country="DE", origin_city="SYNTHETIC-Musterstadt"))
    swiss = profile.lines(CostScope(origin_country="CH"))
    unknown_country = profile.lines(CostScope(origin_city="SYNTHETIC-city"))
    assert len(everything) == len(profile.assumptions) == len(swiss) == len(unknown_country)
    assert not any(line.label.startswith(CH_LABEL_PREFIX) for line in german)
    assert {line.category for line in german} == REQUIRED_CATEGORIES
    assert len(german) == len(everything) - 4


def test_ch_lines_do_not_leak_into_a_german_valuation() -> None:
    german = CostScope(origin_country="DE")
    filtered = _scenarios(german, lines_target=german)
    assert not any("COST_SCOPE_MISMATCH" in w for w in filtered.warnings)
    assert not any(u.label.startswith(CH_LABEL_PREFIX) for u in filtered.unknown_lines)
    # Unfiltered profile lines against a German target are flagged, never applied silently.
    unfiltered = _scenarios(german, lines_target=None)
    assert any("COST_SCOPE_MISMATCH" in w for w in unfiltered.warnings)


def test_swiss_valuation_lists_the_ch_unknowns() -> None:
    swiss = CostScope(origin_country="CH", origin_city="SYNTHETIC-Beispielhausen")
    result = _scenarios(swiss, lines_target=swiss)
    unknown = {u.label for u in result.unknown_lines}
    assert sum(label.startswith(CH_LABEL_PREFIX) for label in unknown) == 4
    assert not result.complete
