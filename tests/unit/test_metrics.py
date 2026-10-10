"""Prometheus metrics (spec 30): every required signal exists and label cardinality is bounded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from prometheus_client import CollectorRegistry, Counter

from suv_deals.domain.enums import (
    AccessState,
    Completeness,
    EligibilityState,
    JobState,
    JobType,
    OutboxState,
    ProfileKey,
    ReviewOutcome,
)
from suv_deals.errors import ErrorCode
from suv_deals.observability.metrics import (
    ALLOWED_LABEL_NAMES,
    OTHER,
    AppMetrics,
    assert_bounded_labels,
    bounded_label,
    error_code_label,
    get_metrics,
    label_names,
    render_metrics,
    route_label,
    status_class,
)

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)

# One metric family per spec section 30 bullet (prefix suv_deals_).
REQUIRED_FAMILIES = {
    # Scheduler delay and last successful cycle
    "suv_deals_scheduler_delay_seconds",
    "suv_deals_scheduler_last_success_timestamp_seconds",
    # Per-source attempts, successful pages, blocked pages and parser failures
    "suv_deals_source_fetch_attempts",
    "suv_deals_source_pages_succeeded",
    "suv_deals_source_pages_blocked",
    "suv_deals_parser_failures",
    # Coverage completeness and watermark lag
    "suv_deals_coverage_scans",
    "suv_deals_coverage_last_scan_complete",
    "suv_deals_watermark_lag_seconds",
    # Detail jobs enqueued versus deduplicated
    "suv_deals_detail_jobs_enqueued",
    "suv_deals_detail_jobs_deduplicated",
    # Queue depth, oldest age, lease expirations and dead letters
    "suv_deals_queue_depth",
    "suv_deals_queue_oldest_age_seconds",
    "suv_deals_lease_expirations",
    "suv_deals_dead_letters",
    # Candidate eligibility distribution and missing critical facts
    "suv_deals_candidates_screened",
    "suv_deals_missing_critical_facts",
    # Comparable sample size/quality and stale valuation count
    "suv_deals_comparable_sample_size",
    "suv_deals_comparable_sets",
    "suv_deals_stale_valuations",
    # Review age, claim conflicts and decisions
    "suv_deals_review_oldest_pending_age_seconds",
    "suv_deals_review_age_at_decision_seconds",
    "suv_deals_review_claim_conflicts",
    "suv_deals_review_decisions",
    # Outbox pending/uncertain/dead-letter counts and delivery latency
    "suv_deals_outbox_events",
    "suv_deals_outbox_delivery_latency_seconds",
    "suv_deals_outbox_delivery_attempts",
    # API/MCP latency, authorization denials and error codes
    "suv_deals_request_duration_seconds",
    "suv_deals_authorization_denials",
    "suv_deals_errors",
    # CPU/memory/browser concurrency, database connections and storage growth
    "suv_deals_process_cpu_seconds",
    "suv_deals_process_resident_memory_bytes",
    "suv_deals_browser_sessions_active",
    "suv_deals_db_connections",
    "suv_deals_storage_bytes",
    # Requests/bytes and optional LLM tokens/cost by source/day
    "suv_deals_source_requests",
    "suv_deals_source_response_bytes",
    "suv_deals_llm_tokens",
    "suv_deals_llm_cost_eur_micros",
}


def exercise(metrics: AppMetrics) -> None:
    """Touch every helper with realistic and hostile values."""
    metrics.set_build_info("build-2026.10.06", "production")
    metrics.record_scheduler_cycle(planned_at=NOW, started_at=NOW + timedelta(seconds=4), outcome="ok")
    metrics.record_scheduler_cycle(planned_at=NOW, started_at=NOW - timedelta(seconds=4), outcome="weird")
    metrics.record_fetch("mobile_de", page_type="search", outcome=AccessState.OK, response_bytes=1234)
    metrics.record_fetch("mobile_de", page_type="detail", outcome=AccessState.ACCESS_BLOCKED)
    metrics.record_fetch("https://evil.example/x", page_type="anything", outcome="teapot")
    metrics.record_parser_failure("autoscout24_it", page_type="detail")
    metrics.record_scan(
        "mobile_de", completeness=Completeness.BUDGET_LIMITED, watermark_lag=timedelta(minutes=20)
    )
    metrics.record_scan("mobile_de", completeness=Completeness.COMPLETE, watermark_lag=None)
    metrics.record_detail_job("mobile_de", deduplicated=False)
    metrics.record_detail_job("mobile_de", deduplicated=True)
    metrics.set_queue_depth({(JobType.DETAIL, JobState.QUEUED): 5, ("bogus", "bogus"): 1})
    metrics.set_queue_oldest_age(JobType.DISCOVERY, timedelta(minutes=3))
    metrics.record_lease_expiration(JobType.DETAIL)
    metrics.record_dead_letter("not-a-job-type")
    metrics.record_screening(
        ProfileKey.PRIMARY, EligibilityState.NEEDS_FACTS, ["mileage", "seller phone +49 151"]
    )
    metrics.record_comparables(ProfileKey.PRIMARY, sample_size=4, quality="sparse")
    metrics.stale_valuations.set(2)
    metrics.review_oldest_pending_age_seconds.labels(profile="primary").set(600)
    metrics.record_review_decision("primary", ReviewOutcome.WATCH, timedelta(hours=2))
    metrics.record_claim_conflict()
    metrics.set_outbox_counts("mcp_events", {OutboxState.PENDING: 3, OutboxState.UNCERTAIN: 1})
    metrics.record_delivery("mcp_events", "delivered", latency=timedelta(seconds=2))
    metrics.record_delivery("slack", "uncertain")
    metrics.record_delivery("https://receiver.example.com/cb", "delivered")
    metrics.record_verification("challenge_failed")
    metrics.record_inbound_rejection("slack", "wrong_channel")
    metrics.observe_request("api", "/api/reviews/44444444-4444-4444-8444-444444444444/claim?x=1", 409, 0.05)
    metrics.observe_request("api", "/api/listings/12345", 200, 0.01)
    metrics.observe_request("mcp", "reviews_claim", 200, 0.02)
    metrics.observe_request("mcp", "owner@example.com", 200, 0.02)
    metrics.record_auth_denial("mcp", "insufficient_scope")
    metrics.record_auth_denial("api", "user 99999999-9999-4999-8999-999999999999")
    metrics.record_error("api", ErrorCode.VERSION_CONFLICT)
    metrics.record_error("mcp", "DROP TABLE")
    metrics.browser_sessions_active.set(2)
    metrics.set_db_connections(in_use=2, idle=3, maximum=5)
    metrics.set_storage_bytes("snapshots", 10_000)
    metrics.record_llm_usage("mobile_de", input_tokens=1200, output_tokens=300, cost_eur=Decimal("0.0123456"))


def test_every_spec_section_30_metric_family_is_registered() -> None:
    metrics = AppMetrics()
    exercise(metrics)
    families = {family.name for family in metrics.registry.collect()}
    missing = REQUIRED_FAMILIES - families
    assert not missing, missing


def test_registry_has_no_high_cardinality_or_sensitive_labels() -> None:
    metrics = AppMetrics(source_keys={"mobile_de", "autoscout24_it"}, tool_names={"reviews_claim"})
    exercise(metrics)
    assert_bounded_labels(metrics.registry)
    for family, names in label_names(metrics.registry).items():
        if family.startswith("suv_deals_"):
            assert names <= ALLOWED_LABEL_NAMES, (family, names)


def test_label_values_never_contain_urls_ids_or_contact_data() -> None:
    metrics = AppMetrics(source_keys={"mobile_de", "autoscout24_it"}, tool_names={"reviews_claim"})
    exercise(metrics)
    values = {
        value
        for family in metrics.registry.collect()
        for sample in family.samples
        for key, value in sample.labels.items()
        if key not in {"le", "quantile"}
    }
    for value in values:
        assert "://" not in value
        assert "@" not in value
        assert "99999999" not in value and "44444444" not in value
        assert " " not in value
        assert len(value) <= 80


def test_unbounded_label_detection_works() -> None:
    registry = CollectorRegistry()
    bad = Counter("suv_deals_bad", "bad", ["listing_id"], registry=registry)
    bad.labels(listing_id="11111111-1111-4111-8111-111111111111").inc()
    with pytest.raises(ValueError, match="listing_id"):
        assert_bounded_labels(registry)
    registry2 = CollectorRegistry()
    sneaky = Counter("suv_deals_sneaky", "bad", ["callback_url"], registry=registry2)
    sneaky.labels(callback_url="https://x.example").inc()
    with pytest.raises(ValueError, match="callback_url"):
        assert_bounded_labels(registry2)


def test_label_helpers() -> None:
    assert bounded_label("primary", {"primary"}) == "primary"
    assert bounded_label(ProfileKey.PRIMARY, {"primary"}) == "primary"
    assert bounded_label("x", {"primary"}) == OTHER
    assert bounded_label(None, {"primary"}) == OTHER
    assert route_label("/api/reviews/44444444-4444-4444-8444-444444444444/claim") == "/api/reviews/{id}/claim"
    assert route_label("/api/listings/123?token=abc") == "/api/listings/{n}"
    assert route_label("https://evil.example/x") == OTHER
    assert route_label("/" + "a" * 200) == OTHER
    assert status_class(204) == "2xx"
    assert status_class(503) == "5xx"
    assert status_class(42) == OTHER
    assert error_code_label(ErrorCode.NOT_FOUND) == "NOT_FOUND"
    assert error_code_label("SELECT 1") == OTHER


def test_source_and_tool_labels_are_restricted_to_configuration() -> None:
    metrics = AppMetrics(source_keys={"mobile_de"}, tool_names={"reviews_claim"}, process_metrics=False)
    assert metrics.source_label("mobile_de") == "mobile_de"
    assert metrics.source_label("unregistered_source") == OTHER
    assert metrics.source_label("Mobile.de") == OTHER
    assert metrics.tool_label("reviews_claim") == "reviews_claim"
    assert metrics.tool_label("execute_sql") == OTHER
    open_metrics = AppMetrics(process_metrics=False)
    assert open_metrics.source_label("any_source_key") == "any_source_key"
    assert open_metrics.source_label("x" * 41) == OTHER


def _distinct(metrics: AppMetrics, family: str, label: str) -> set[str]:
    for fam in metrics.registry.collect():
        if fam.name == family:
            return {s.labels[label] for s in fam.samples if label in s.labels}
    return set()


def test_route_and_tool_label_values_are_capped_even_for_raw_paths() -> None:
    # Regression: raw request paths (404 scans) and attacker-chosen tool names used to
    # create one series each; 1000 calls produced 2000 distinct label values.
    metrics = AppMetrics(process_metrics=False, max_dynamic_label_values=8)
    for i in range(500):
        metrics.observe_request("api", f"/api/scan-{i:04d}abc", 404, 0.01)
        metrics.observe_request("mcp", f"tool_{i}", 200, 0.01)
        metrics.record_fetch(f"source_{i}", page_type="search", outcome="ok")
    routes = _distinct(metrics, "suv_deals_request_duration_seconds", "route")
    assert len(routes) <= 2 * 8 + 1  # 8 routes + 8 tools + "other"
    assert OTHER in routes
    assert len(_distinct(metrics, "suv_deals_source_fetch_attempts", "source_key")) <= 8 + 1
    # Values admitted before the cap keep being recorded under their own label.
    metrics.observe_request("api", "/api/scan-0000abc", 200, 0.01)
    assert "/api/scan-0000abc" in _distinct(metrics, "suv_deals_request_duration_seconds", "route")
    with pytest.raises(ValueError):
        AppMetrics(process_metrics=False, max_dynamic_label_values=0)


def test_api_routes_allow_list() -> None:
    metrics = AppMetrics(process_metrics=False, api_routes={"/api/reviews/{id}/claim", "/api/health"})
    assert metrics.api_route_label("/api/reviews/44444444-4444-4444-8444-444444444444/claim") == (
        "/api/reviews/{id}/claim"
    )
    assert metrics.api_route_label("/api/health?x=1") == "/api/health"
    assert metrics.api_route_label("/wp-admin/setup.php") == OTHER
    metrics.observe_request("api", "/.env", 404, 0.001)
    assert _distinct(metrics, "suv_deals_request_duration_seconds", "route") == {OTHER}


def test_values_recorded() -> None:
    metrics = AppMetrics(process_metrics=False)
    exercise(metrics)
    reg = metrics.registry
    assert (
        reg.get_sample_value("suv_deals_scheduler_last_success_timestamp_seconds")
        == (NOW + timedelta(seconds=4)).timestamp()
    )
    assert reg.get_sample_value("suv_deals_scheduler_cycles_total", {"outcome": "other"}) == 1
    assert (
        reg.get_sample_value(
            "suv_deals_source_pages_blocked_total", {"source_key": "mobile_de", "reason": "access_blocked"}
        )
        == 1
    )
    assert (
        reg.get_sample_value(
            "suv_deals_source_fetch_attempts_total",
            {"source_key": OTHER, "page_type": OTHER, "outcome": OTHER},
        )
        == 1
    )
    assert reg.get_sample_value("suv_deals_coverage_last_scan_complete", {"source_key": "mobile_de"}) == 1
    assert reg.get_sample_value("suv_deals_watermark_lag_seconds", {"source_key": "mobile_de"}) == 1200
    assert reg.get_sample_value("suv_deals_queue_depth", {"job_type": "detail", "state": "queued"}) == 5
    assert reg.get_sample_value("suv_deals_queue_depth", {"job_type": OTHER, "state": OTHER}) == 1
    assert (
        reg.get_sample_value(
            "suv_deals_missing_critical_facts_total", {"profile": "primary", "fact": "mileage"}
        )
        == 1
    )
    assert (
        reg.get_sample_value("suv_deals_missing_critical_facts_total", {"profile": "primary", "fact": OTHER})
        == 1
    )
    assert (
        reg.get_sample_value("suv_deals_outbox_events", {"provider": "mcp_events", "state": "uncertain"}) == 1
    )
    assert (
        reg.get_sample_value("suv_deals_outbox_events", {"provider": "mcp_events", "state": "dead_letter"})
        == 0
    )
    assert (
        reg.get_sample_value("suv_deals_outbox_delivery_latency_seconds_count", {"provider": "mcp_events"})
        == 1
    )
    assert (
        reg.get_sample_value(
            "suv_deals_request_duration_seconds_count",
            {"surface": "api", "route": "/api/reviews/{id}/claim", "status_class": "4xx"},
        )
        == 1
    )
    assert reg.get_sample_value("suv_deals_errors_total", {"surface": "mcp", "code": OTHER}) == 1
    assert reg.get_sample_value("suv_deals_llm_cost_eur_micros_total", {"source_key": "mobile_de"}) == 12346
    assert (
        reg.get_sample_value("suv_deals_llm_tokens_total", {"source_key": "mobile_de", "kind": "input"})
        == 1200
    )
    assert (
        reg.get_sample_value("suv_deals_build_info", {"version": "build-2026.10.06", "app_env": "production"})
        == 1
    )


def test_render_metrics_exposition() -> None:
    metrics = AppMetrics(process_metrics=False)
    exercise(metrics)
    body, content_type = render_metrics(metrics)
    assert content_type.startswith("text/plain")
    text = body.decode()
    assert "# TYPE suv_deals_queue_depth gauge" in text
    assert "evil.example" not in text
    assert "owner@example.com" not in text


def test_separate_instances_do_not_collide_and_default_is_singleton() -> None:
    AppMetrics(process_metrics=False)
    AppMetrics(process_metrics=False)  # a fresh registry each time: no duplicate-timeseries error
    assert get_metrics() is get_metrics()
