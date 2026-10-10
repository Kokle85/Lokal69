"""Every read view builds from database rows and validates against its published JSON schema.

Runs as ``suv_backend`` under RLS with the SYNTHETIC dataset of ``dataset.py``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import (
    CREDENTIAL_URL,
    CURSOR_SECRET,
    INJECTION_TEXT,
    REPO,
    SeededWorkspace,
    owner,
    reviewer,
    run,
    viewer,
)
from tests.integration.read_queries.schema_check import amounts, assert_valid, walk_numbers

from suv_deals.api.schemas import OutboxQuery
from suv_deals.domain.enums import (
    EligibilityState,
    GateStatus,
    ProfileKey,
    ReviewState,
    ValuationState,
)
from suv_deals.domain.profiles import load_business_config
from suv_deals.errors import AppError, ErrorCode
from suv_deals.mcp.schemas import (
    DealsListCandidatesInput,
    ReviewsListPendingInput,
)
from suv_deals.persistence import queries
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings
from suv_deals.views.common import WarningCode
from suv_deals.views.operations import SourceRunState

pytestmark = pytest.mark.db


def codes(result: queries.QueryResult[object]) -> set[WarningCode]:
    return {w.code for w in result.warnings}


# --------------------------------------------------------------------------------------------
# Health, overview, sources, settings, outbox
# --------------------------------------------------------------------------------------------


async def test_health_view_reports_readiness_coverage_and_blockers(
    db: Database, data: SeededWorkspace, settings: Settings
) -> None:
    result = await queries.health_view(db, viewer(data.workspace_id), settings)
    document = assert_valid(result, tool="deals_health")
    view = result.data
    assert view.ready is True
    assert [c.name for c in view.readiness] == ["database", "schema", "config"]
    assert view.build.build_id == "synthetic-build.1"
    states = {s.source_key: s.state for s in view.sources}
    assert states == {
        data.source_keys["running"]: SourceRunState.RUNNING,
        data.source_keys["paused"]: SourceRunState.PAUSED,
        data.source_keys["blocked"]: SourceRunState.BLOCKED,
        data.source_keys["never"]: SourceRunState.NOT_SCANNED,
        data.source_keys["mk"]: SourceRunState.DISABLED,
    }
    running = next(s for s in view.sources if s.source_key == data.source_keys["running"])
    assert running.last_successful_scan_at is not None
    assert running.incomplete_since is not None
    assert "SYNTHETIC daily request budget reached after page 5" in running.gap_reasons
    assert view.activation_blockers, "spec 32 gates are seeded blocked/implemented"
    assert all(
        g.status not in (GateStatus.ACTIVE, GateStatus.NOT_REQUESTED) for g in view.activation_blockers
    )
    # The MCP events binding is enabled, approved and verified for the configured provider.
    assert view.bridge_status == "verified"
    assert view.notification_route == "none"  # external notifications are not allowed in settings
    assert {WarningCode.SOURCE_PAUSED, WarningCode.SOURCE_BLOCKED, WarningCode.COVERAGE_GAP} <= codes(result)
    assert WarningCode.ACTIVATION_BLOCKED in codes(result)
    text = str(document)
    assert CURSOR_SECRET.decode() not in text
    assert "postgresql://" not in text


async def test_overview_view_counts_and_gaps(db: Database, data: SeededWorkspace, settings: Settings) -> None:
    actor = viewer(data.workspace_id)
    result = await run(db, actor, lambda c: queries.overview_view(c, actor, settings))
    assert_valid(result)
    view = result.data
    assert view.running_sources == 1
    assert view.paused_sources == 1
    assert view.last_successful_scan_at is not None
    kinds = {(g.source_key, g.kind) for g in view.coverage_gaps}
    assert (data.source_keys["paused"], "paused") in kinds
    assert (data.source_keys["blocked"], "blocked") in kinds
    assert (data.source_keys["never"], "not_scanned") in kinds
    assert (data.source_keys["running"], "budget_limited") in kinds
    budget_gap = next(g for g in view.coverage_gaps if g.kind == "budget_limited")
    assert budget_gap.profile == ProfileKey.PRIMARY and budget_gap.partition_key == "deep_pages"
    # Disabled comparable source: listed, but not a coverage gap.
    assert all(g.source_key != data.source_keys["mk"] for g in view.coverage_gaps)
    counts = view.pending_reviews
    assert (counts.pending, counts.claimed, counts.watch, counts.needs_information) == (1, 1, 1, 0)
    primary = next(q for q in counts.by_queue if q.profile == ProfileKey.PRIMARY)
    assert (primary.pending, primary.claimed) == (1, 1)
    deliveries = view.failed_deliveries
    # The fixture row is blocked too; it stays visible (never silently discarded).
    assert (deliveries.uncertain, deliveries.blocked, deliveries.dead_letter, deliveries.retry_wait) == (
        1,
        2,
        1,
        1,
    )
    assert view.bridge_status == "verified"


async def test_sources_view_validates(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    result = await run(db, actor, lambda c: queries.sources_view(c, actor))
    assert_valid(result)
    by_key = {s.source_key: s for s in result.data.items}
    paused = by_key[data.source_keys["paused"]]
    assert paused.paused and paused.pause_reason == "SYNTHETIC owner pause for maintenance"
    assert by_key[data.source_keys["blocked"]].state == SourceRunState.BLOCKED
    assert {WarningCode.SOURCE_PAUSED, WarningCode.SOURCE_BLOCKED} <= codes(result)


async def test_settings_view_labels_manual_profile_and_proposed_threshold(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    result = await run(db, actor, lambda c: queries.settings_view(c, actor))
    assert_valid(result)
    view = result.data
    assert view.config_revision is not None and view.config_revision.revision == 1
    manual = next(p for p in view.profiles if p.profile_key == ProfileKey.MANUAL_4000)
    assert manual.enabled is False
    assert manual.status_label.startswith("DISABLED - optional manual-review profile up to EUR 4,000.00")
    assert manual.row_version is not None
    assert view.contribution_threshold.label == "PROPOSED"
    assert view.contribution_threshold.amount_eur == "1500.00"
    assert view.can_administer is False
    providers = {b.provider: b for b in view.destination_bindings}
    assert providers["mcp_events"].enabled and providers["mcp_events"].approval_recorded
    assert not providers["slack"].enabled and not providers["slack"].approval_recorded
    assert view.gates and any(g.status == GateStatus.BLOCKED for g in view.gates)
    assert WarningCode.THRESHOLD_PROPOSED in codes(result)
    owner_view = await run(
        db, owner(data.workspace_id), lambda c: queries.settings_view(c, owner(data.workspace_id))
    )
    assert owner_view.data.can_administer is True


async def test_settings_view_without_a_recorded_revision_uses_the_fallback(db: Database, seed: Seed) -> None:
    ws = seed.workspace("Read queries settings fallback")
    actor = viewer(ws)
    with pytest.raises(AppError) as missing:
        await run(db, actor, lambda c: queries.settings_view(c, actor))
    assert missing.value.code == ErrorCode.INSUFFICIENT_DATA
    fallback = load_business_config(REPO / "config")
    result = await run(db, actor, lambda c: queries.settings_view(c, actor, fallback_config=fallback))
    assert_valid(result)
    assert result.data.config_revision is None
    assert {p.profile_key for p in result.data.profiles} == set(ProfileKey)


async def test_outbox_attention_view_lists_attention_states_without_payloads(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    result = await run(
        db, actor, lambda c: queries.outbox_attention_view(c, actor, OutboxQuery(), secret=CURSOR_SECRET)
    )
    document = assert_valid(result)
    states = sorted(i.state.value for i in result.data.items)
    assert states == ["blocked", "blocked", "dead_letter", "retry_wait", "uncertain"]
    uncertain = next(i for i in result.data.items if i.state.value == "uncertain")
    assert uncertain.uncertain_notice and uncertain.owner_seen_at is None
    assert "payload" not in str(document)
    assert WarningCode.FIXTURE_DATA in codes(result)
    only = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(
            c, actor, OutboxQuery(state="dead_letter"), secret=CURSOR_SECRET
        ),
    )
    assert [i.outbox_id for i in only.data.items] == [data.outbox["dead_letter"]]


# --------------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------------


async def test_list_candidates_builds_summaries(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    result = await run(
        db,
        actor,
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(), secret=CURSOR_SECRET),
    )
    document = assert_valid(result, tool="deals_list_candidates")
    ids = [i.listing_id for i in result.data.items]
    # Newest first; rejected and never-fetched listings are not candidates.
    assert ids == [data.listings[k] for k in ("paused", "needs_facts", "incomplete", "priced")]
    assert result.next_cursor is None
    priced = next(i for i in result.data.items if i.listing_id == data.listings["priced"])
    assert priced.price.payable.amount == "2750.00" and priced.price.payable.currency == "EUR"
    assert priced.price.eur_equivalent.amount == "2750.00"
    assert priced.revision_number == 2
    assert priced.valuation_state == ValuationState.ESTIMATED and priced.research_candidate is False
    assert priced.review_state == ReviewState.PENDING and priced.rank is not None
    assert priced.mileage_km == "187500"
    needs = next(i for i in result.data.items if i.listing_id == data.listings["needs_facts"])
    assert needs.eligibility == EligibilityState.NEEDS_FACTS
    assert needs.price.payable.amount == "2800.00" and needs.price.payable.currency == "CHF"
    assert needs.price.eur_equivalent.status == "unknown" and needs.price.eur_equivalent.amount is None
    assert needs.case_id is None and needs.valuation_state == ValuationState.NOT_STARTED
    paused = next(i for i in result.data.items if i.listing_id == data.listings["paused"])
    assert paused.review_state == ReviewState.WATCH and paused.freshness.stale
    assert WarningCode.SOURCE_PAUSED in codes(result)
    assert not walk_numbers(document)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"profile": "primary"}, ("paused", "incomplete", "priced")),
        ({"country": "IT"}, ("paused", "needs_facts")),
        # A claim is a lock, not a review status: the actively claimed case is still pending.
        ({"status": "pending"}, ("incomplete", "priced")),
        ({"status": "claimed"}, ()),  # not a filter value: claimed is not a candidate status
        ({"status": "watch"}, ("paused",)),
    ],
)
async def test_list_candidates_filters(
    db: Database, data: SeededWorkspace, filters: dict[str, str], expected: tuple[str, ...]
) -> None:
    actor = viewer(data.workspace_id)
    if filters.get("status") == "claimed":
        with pytest.raises(PydanticValidationError):
            DealsListCandidatesInput.model_validate(filters)
        return
    query = DealsListCandidatesInput.model_validate(filters)
    result = await run(db, actor, lambda c: queries.list_candidates(c, actor, query, secret=CURSOR_SECRET))
    assert [i.listing_id for i in result.data.items] == [data.listings[k] for k in expected]


async def test_list_candidates_changed_since(db: Database, seed: Seed, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    marker = seed.scalar("select clock_timestamp()")
    seed.conn.execute(
        "update app.listings set row_version = row_version + 1 where workspace_id = %s and id = %s",
        (data.workspace_id, data.listings["needs_facts"]),
    )
    query = DealsListCandidatesInput.model_validate({"changed_since": marker.isoformat()})
    result = await run(db, actor, lambda c: queries.list_candidates(c, actor, query, secret=CURSOR_SECRET))
    assert [i.listing_id for i in result.data.items] == [data.listings["needs_facts"]]


async def test_get_candidate_exact_revision_with_history_and_references(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    listing_id = data.listings["priced"]
    result = await run(db, actor, lambda c: queries.get_candidate(c, actor, listing_id))
    document = assert_valid(result, tool="deals_get_candidate")
    detail = result.data
    assert detail.revision.is_current and detail.revision.revision_number == 2
    assert [p.change for p in detail.price_history] == ["initial", "decrease"]
    assert [p.payable.amount for p in detail.price_history] == ["2900.00", "2750.00"]
    history = detail.availability_history
    assert [p.revision_number for p in history if p.revision_number is not None] == [1, 2]
    events = [p for p in history if p.revision_number is None]  # listing.availability audit events
    assert [p.availability.value for p in events] == ["reserved", "available"]
    assert [p.observed_at for p in history] == sorted(p.observed_at for p in history)
    assert detail.latest_valuation is not None
    assert detail.latest_valuation.valuation_id == data.valuations["estimated"]
    assert detail.latest_valuation.base_contribution.status == "known"
    assert (
        detail.comparable_set is not None
        and detail.comparable_set.comparable_set_id == data.comparable_set_id
    )
    assert detail.review_case is not None and detail.review_case.case_id == data.cases["priced"]
    assert detail.due_diligence is not None and not detail.due_diligence.ready
    assert [n.body for n in detail.notes] == ["SYNTHETIC note: ask for the service records first."]
    assert detail.rank is not None and detail.rank.is_probability is False
    assert detail.screening is not None and detail.screening.payable_eur.amount == "2750.00"
    # Provenance: extraction confidence only; evidence id joined; credential URL dropped.
    prov = {p.field_path: p for p in detail.field_provenance}
    assert prov["price.amount_minor"].evidence_id is not None
    assert prov["price.amount_minor"].claim_status is not None
    assert prov["vehicle.mileage_km"].source_url is None
    assert CREDENTIAL_URL not in str(document)
    assert detail.conflicts and detail.conflicts[0].field == "vehicle.mileage_km"
    # Seller text is data, labelled untrusted.
    assert detail.seller_text.description_excerpt == INJECTION_TEXT
    assert detail.seller_text.trust == "untrusted_seller_text"
    assert detail.source_link.rel == "noopener noreferrer"
    assert {WarningCode.SELLER_CLAIMS_UNVERIFIED, WarningCode.ASKING_NOT_SALE} <= codes(result)
    assert WarningCode.REVISION_NOT_CURRENT not in codes(result)


async def test_get_candidate_older_revision_is_labelled(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    listing_id = data.listings["priced"]
    result = await run(db, actor, lambda c: queries.get_candidate(c, actor, listing_id, 1))
    assert_valid(result, tool="deals_get_candidate")
    assert result.data.revision.revision_number == 1
    assert result.data.revision.current_revision_number == 2 and not result.data.revision.is_current
    assert result.data.normalized.price.amount_minor == 290000
    assert result.data.latest_valuation is None  # valuations are bound to their exact revision
    assert WarningCode.REVISION_NOT_CURRENT in codes(result)


async def test_get_candidate_with_unknown_eur_and_no_valuation(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    listing_id = data.listings["needs_facts"]
    result = await run(db, actor, lambda c: queries.get_candidate(c, actor, listing_id))
    assert_valid(result, tool="deals_get_candidate")
    assert result.data.summary.price.eur_equivalent.amount is None
    assert result.data.screening is not None
    assert result.data.screening.payable_eur.status == "unknown"
    assert result.data.screening.missing_facts == ("fx_rate",)
    assert result.data.latest_valuation is None and result.data.review_case is None


# --------------------------------------------------------------------------------------------
# Valuations and comparables
# --------------------------------------------------------------------------------------------


async def test_get_valuation_reconstructs_estimated_breakdown(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    valuation_id = data.valuations["estimated"]
    result = await run(db, actor, lambda c: queries.get_valuation(c, actor, valuation_id))
    document = assert_valid(result, tool="deals_get_valuation")
    view = result.data
    assert view.state == ValuationState.ESTIMATED
    assert view.contributions.base.status == "known"
    assert isinstance(document["data"]["contributions"]["base"]["amount"], str)
    Decimal(document["data"]["contributions"]["base"]["amount"])  # a decimal string
    assert {s.scenario.value for s in view.scenarios} == {"conservative", "base", "upside"}
    assert view.threshold is not None and view.threshold.label == "PROPOSED"
    assert view.threshold.alert_eligible is False
    assert view.contribution_label == "estimated contribution before business tax"
    assert WarningCode.THRESHOLD_PROPOSED in codes(result)
    assert not walk_numbers(document)
    for amount in amounts(document):
        assert amount["amount"] is None or isinstance(amount["amount"], str)


async def test_get_valuation_unknown_is_never_zero(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    valuation_id = data.valuations["not_started"]
    result = await run(db, actor, lambda c: queries.get_valuation(c, actor, valuation_id))
    document = assert_valid(result, tool="deals_get_valuation")
    view = result.data
    assert view.state == ValuationState.NOT_STARTED
    for figure in (view.contributions.conservative, view.contributions.base, view.contributions.upside):
        assert figure.status == "unknown" and figure.amount is None
    assert view.scenarios == () and view.threshold is None
    assert {WarningCode.VALUATION_INCOMPLETE, WarningCode.TAX_RULES_UNAPPROVED} <= codes(result)
    found = amounts(document)
    assert found and all(a["amount"] not in ("0", "0.00", 0) for a in found)


async def test_get_comparables_members_and_statistics(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    assert data.comparable_set_id is not None
    set_id = data.comparable_set_id
    result = await run(db, actor, lambda c: queries.get_comparables(c, actor, set_id, secret=CURSOR_SECRET))
    document = assert_valid(result, tool="deals_get_comparables")
    view = result.data
    assert view.selected_count == 4 and view.excluded_count >= 1
    assert all(m.role == "selected" for m in view.members)
    assert view.asking_price_stats is not None and view.asking_price_stats.median == "9100.00"
    assert view.seller_reported_sale_stats is None and view.verified_sale_stats is None
    assert WarningCode.ASKING_NOT_SALE in codes(result)
    assert not walk_numbers(document)
    full = await run(
        db,
        actor,
        lambda c: queries.get_comparables(c, actor, set_id, include_excluded=True, secret=CURSOR_SECRET),
    )
    assert_valid(full, tool="deals_get_comparables")
    excluded = [m for m in full.data.members if m.role == "excluded"]
    assert excluded and all(m.exclusion_reasons for m in excluded)


# --------------------------------------------------------------------------------------------
# Reviews and lifecycle
# --------------------------------------------------------------------------------------------


async def test_review_queue_and_case_views(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    result = await run(
        db, actor, lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(), secret=CURSOR_SECRET)
    )
    document = assert_valid(result, tool="reviews_list_pending")
    items = {i.case_id: i for i in result.data.items}
    assert set(items) == {data.cases["priced"], data.cases["incomplete"]}
    claimed = items[data.cases["incomplete"]]
    assert claimed.claim.claimed and not claimed.claim.held_by_caller
    assert str(data.claim_holder) not in str(document)  # never another reviewer's identity
    assert "claim_token" not in str(document)
    assert WarningCode.FROZEN_QUEUE_PROJECTION in codes(result)
    case = await run(db, actor, lambda c: queries.get_review_case(c, actor, data.cases["paused"]))
    assert_valid(case)
    assert case.data.state == ReviewState.WATCH and len(case.data.decisions) == 1


async def test_lifecycle_views_report_lags_honestly(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    lags = await run(db, actor, lambda c: queries.coverage_lags_view(c, actor))
    by_key = {s.source_key: s for s in lags.data.sources}
    running = by_key[data.source_keys["running"]]
    assert running.source_scan_lag.status == "measured"
    assert running.source_scan_lag.value_seconds is not None
    assert running.source_scan_lag.configured_interval_seconds == 900
    assert by_key[data.source_keys["never"]].source_scan_lag.status == "unknown"
    assert lags.data.notification_processing_lag.status == "measured"
    assert lags.data.mail_reply_detection_lag.status == "unknown"
    assert lags.data.mail_reply_detection_lag.value_seconds is None
    assert {t.relation for t in lags.data.v11_tables} == set(queries.V11_TABLES)
    # Local (not yet shared) models: pydantic JSON round trip instead of a published schema.
    assert queries.CoverageLagsView.model_validate_json(lags.data.model_dump_json()) == lags.data
    lifecycle = await run(
        db, actor, lambda c: queries.listing_lifecycle_view(c, actor, data.listings["priced"])
    )
    view = lifecycle.data
    assert queries.ListingLifecycleView.model_validate_json(view.model_dump_json()) == view
    assert view.detail_freshness.status == "measured"
    assert view.detection_delay.status == "unknown" and view.detection_delay.value_seconds is None
    assert view.source_published_at is not None and view.source_published_trusted is False
    assert view.last_complete_source_scan_at is not None
    # Detail checked three hours before seeding; no search card observed: unknown, never "now".
    assert view.detail_freshness.value_seconds is not None
    assert 3 * 3600 <= view.detail_freshness.value_seconds < 3 * 3600 + 600
    assert view.last_seen_on_search_at is None
    assert view.first_seen_at < view.last_complete_source_scan_at - timedelta(days=2)
    running_lag = running.source_scan_lag.value_seconds
    assert 110 * 60 <= running_lag < 110 * 60 + 600  # last complete run finished 1 h 50 min ago


async def test_reviewer_and_viewer_see_the_same_read_models(db: Database, data: SeededWorkspace) -> None:
    listing_id = data.listings["priced"]
    as_viewer = await run(
        db,
        viewer(data.workspace_id),
        lambda c: queries.get_candidate(c, viewer(data.workspace_id), listing_id),
    )
    as_reviewer = await run(
        db,
        reviewer(data.workspace_id),
        lambda c: queries.get_candidate(c, reviewer(data.workspace_id), listing_id),
    )
    assert as_viewer.data.model_dump(exclude={"summary": {"freshness"}}) == as_reviewer.data.model_dump(
        exclude={"summary": {"freshness"}}
    )


async def test_overview_redacts_credential_like_free_text(
    db: Database, seed: Seed, data: SeededWorkspace, settings: Settings
) -> None:
    seed.conn.execute(
        "update app.sources set pause_reason = %s where workspace_id = %s and id = %s",
        ("SYNTHETIC pause: see https://ops.example/?token=abc123", data.workspace_id, data.sources["paused"]),
    )
    actor = viewer(data.workspace_id)
    result = await run(db, actor, lambda c: queries.overview_view(c, actor, settings))
    text = result.envelope("req-synthetic").to_text()
    assert "abc123" not in text
    paused = next(g for g in result.data.coverage_gaps if g.kind == "paused")
    assert paused.reasons == ("[redacted: text resembled a credential]",)
