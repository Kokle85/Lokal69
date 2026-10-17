"""Prometheus metrics for every spec section 30 signal, on a dedicated registry.

Label cardinality is bounded by construction: labels are enumerations
(job type, state, outcome, profile, error code, ...) or configured source keys.
URLs, hosts, listing/case/event/user IDs, e-mails and free text are never
label values. Use the helper methods, which normalise every value through
`bounded_label`/`route_label`/`source_label`; `assert_bounded_labels`
verifies a registry in tests.

Labels whose values come from configuration or routing (source keys, API route
templates, MCP tool names) are additionally capped: `source_keys`, `tool_names`
and `api_routes` restrict them to the configured sets, and in every case at most
`max_dynamic_label_values` distinct values per label are ever emitted; anything
beyond that is recorded as `other`. A caller passing a raw request path or an
attacker-chosen tool name therefore cannot create unbounded series.

"Requests/bytes and LLM tokens/cost by source/day" are monotonic counters per
source; the per-day view is `increase(metric[1d])` in PromQL, not a date label.
LLM cost is counted in integer micro-EUR from a Decimal, so no float money
arithmetic happens in application code.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)

from suv_deals.clock import ensure_utc
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

NAMESPACE: Final = "suv_deals"
OTHER: Final = "other"
DEFAULT_MAX_DYNAMIC_LABEL_VALUES: Final = 64

ALLOWED_LABEL_NAMES: Final = frozenset(
    {
        "source_key",
        "page_type",
        "outcome",
        "reason",
        "completeness",
        "job_type",
        "state",
        "profile",
        "eligibility",
        "fact",
        "quality",
        "decision",
        "provider",
        "result",
        "surface",
        "route",
        "status_class",
        "code",
        "kind",
        "store",
        "version",
        "app_env",
    }
)
# Substrings that must never appear in a label name (high cardinality or sensitive).
FORBIDDEN_LABEL_FRAGMENTS: Final = (
    "url",
    "uri",
    "host",
    "path",
    "email",
    "user",
    "principal",
    "listing",
    "case",
    "event_id",
    "request_id",
    "subscription",
    "ip",
    "token",
    "query",
    "message",
)

PAGE_TYPES: Final = frozenset({"search", "detail", "robots", "fx", "other"})
BLOCK_REASONS: Final = frozenset(
    {AccessState.ACCESS_BLOCKED.value, AccessState.RATE_LIMITED.value, AccessState.POLICY_DENIED.value}
)
CRITICAL_FACTS: Final = frozenset(
    {
        "price",
        "price_basis",
        "mileage",
        "first_registration",
        "fuel",
        "gearbox",
        "drive",
        "body_type",
        "taxonomy",
        "fx_rate",
        "vat",
        "co2",
        "engine",
    }
)
COMPARABLE_QUALITY: Final = frozenset({"sufficient", "sparse", "stale", "none"})
PROVIDERS: Final = frozenset({"mcp_events", "slack"})
DELIVERY_OUTCOMES: Final = frozenset({"delivered", "retry", "failed", "uncertain", "dead_letter", "skipped"})
VERIFICATION_RESULTS: Final = frozenset(
    {"ok", "challenge_failed", "timeout", "connection_refused", "tls_error", "http_4xx", "http_5xx"}
)
SURFACES: Final = frozenset({"api", "mcp"})
AUTH_DENIAL_REASONS: Final = frozenset(
    {
        "missing_token",
        "invalid_token",
        "expired_token",
        "wrong_issuer",
        "wrong_audience",
        "insufficient_scope",
        "not_member",
        "revoked",
    }
)
STORES: Final = frozenset({"snapshots", "database", "evidence"})
DB_CONNECTION_STATES: Final = frozenset({"in_use", "idle", "max"})
TOKEN_KINDS: Final = frozenset({"input", "output"})
SCHEDULER_OUTCOMES: Final = frozenset({"ok", "partial", "failed", "skipped"})
INBOUND_REJECT_REASONS: Final = frozenset(
    {
        "missing_headers",
        "invalid_id",
        "malformed_timestamp",
        "stale_timestamp",
        "future_timestamp",
        "malformed_signature",
        "no_matching_signature",
        "bad_signature",
        "body_too_large",
        "invalid_body",
        "replayed",
        "unsupported_envelope",
        "wrong_team",
        "wrong_app",
        "wrong_channel",
        "event_type_not_allowed",
        "invalid_event_id",
    }
)

_SOURCE_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NUMBER_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_{}./-]{0,79}$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_/]{0,47}$")

_SECONDS_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_DELAY_BUCKETS: Final = (1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
_AGE_BUCKETS: Final = (60, 300, 900, 3600, 4 * 3600, 12 * 3600, 86400, 3 * 86400, 7 * 86400)
_SAMPLE_BUCKETS: Final = (0, 1, 2, 3, 5, 8, 13, 21, 34, 55)


def bounded_label(value: object, allowed: Collection[str]) -> str:
    """Return `value` if it is in the allow-list, otherwise `other`."""
    text = getattr(value, "value", value)
    return text if isinstance(text, str) and text in allowed else OTHER


def route_label(route: str) -> str:
    """A route *template* (`/api/reviews/{id}`); IDs, numbers and query strings are removed."""
    path = route.split("?", 1)[0].split("#", 1)[0]
    path = _UUID_RE.sub("{id}", path)
    path = _NUMBER_SEGMENT_RE.sub("/{n}", path)
    return path if _ROUTE_RE.fullmatch(path) else OTHER


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else OTHER


def error_code_label(code: ErrorCode | str) -> str:
    return bounded_label(code, {c.value for c in ErrorCode})


class AppMetrics:
    """All application metrics on one registry (a fresh registry per instance)."""

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        source_keys: Collection[str] | None = None,
        tool_names: Collection[str] | None = None,
        api_routes: Collection[str] | None = None,
        process_metrics: bool = True,
        max_dynamic_label_values: int = DEFAULT_MAX_DYNAMIC_LABEL_VALUES,
    ) -> None:
        if max_dynamic_label_values < 1:
            raise ValueError("max_dynamic_label_values must be positive")
        self.registry = registry if registry is not None else CollectorRegistry(auto_describe=True)
        self._source_keys = frozenset(source_keys) if source_keys is not None else None
        self._tool_names = frozenset(tool_names) if tool_names is not None else None
        self._api_routes = frozenset(route_label(r) for r in api_routes) if api_routes is not None else None
        self._max_dynamic = max_dynamic_label_values
        self._dynamic_seen: dict[str, set[str]] = {"source_key": set(), "route": set(), "tool": set()}
        self._dynamic_lock = threading.Lock()
        reg = self.registry
        ns = NAMESPACE
        if process_metrics:
            ProcessCollector(namespace=ns, registry=reg)  # CPU seconds, resident memory, open fds

        self.build_info = Gauge(
            "build_info", "Running build (value 1)", ["version", "app_env"], namespace=ns, registry=reg
        )

        # Scheduler
        self.scheduler_delay_seconds = Histogram(
            "scheduler_delay_seconds",
            "Delay between the planned 15-minute slot and the actual start",
            namespace=ns,
            registry=reg,
            buckets=_DELAY_BUCKETS,
        )
        self.scheduler_last_success_timestamp = Gauge(
            "scheduler_last_success_timestamp_seconds",
            "Unix time of the last successful scheduler cycle",
            namespace=ns,
            registry=reg,
        )
        self.scheduler_cycles_total = Counter(
            "scheduler_cycles", "Scheduler cycles by outcome", ["outcome"], namespace=ns, registry=reg
        )

        # Per-source crawling
        self.source_fetch_attempts_total = Counter(
            "source_fetch_attempts",
            "Fetch attempts per source, page type and access outcome",
            ["source_key", "page_type", "outcome"],
            namespace=ns,
            registry=reg,
        )
        self.source_pages_succeeded_total = Counter(
            "source_pages_succeeded",
            "Successfully fetched and parsed pages",
            ["source_key", "page_type"],
            namespace=ns,
            registry=reg,
        )
        self.source_pages_blocked_total = Counter(
            "source_pages_blocked",
            "Pages refused by the source or by our own policy",
            ["source_key", "reason"],
            namespace=ns,
            registry=reg,
        )
        self.parser_failures_total = Counter(
            "parser_failures", "Parser failures", ["source_key", "page_type"], namespace=ns, registry=reg
        )
        self.source_requests_total = Counter(
            "source_requests", "Outbound requests per source", ["source_key"], namespace=ns, registry=reg
        )
        self.source_response_bytes_total = Counter(
            "source_response_bytes", "Response bytes per source", ["source_key"], namespace=ns, registry=reg
        )
        self.coverage_scans_total = Counter(
            "coverage_scans",
            "Discovery scans by completeness",
            ["source_key", "completeness"],
            namespace=ns,
            registry=reg,
        )
        self.coverage_last_scan_complete = Gauge(
            "coverage_last_scan_complete",
            "1 when the last scan reached its cutoff/cursor end, else 0",
            ["source_key"],
            namespace=ns,
            registry=reg,
        )
        self.watermark_lag_seconds = Gauge(
            "watermark_lag_seconds",
            "Now minus the discovery watermark",
            ["source_key"],
            namespace=ns,
            registry=reg,
        )
        self.detail_jobs_enqueued_total = Counter(
            "detail_jobs_enqueued", "Detail jobs enqueued", ["source_key"], namespace=ns, registry=reg
        )
        self.detail_jobs_deduplicated_total = Counter(
            "detail_jobs_deduplicated",
            "Detail jobs skipped because an identical job existed",
            ["source_key"],
            namespace=ns,
            registry=reg,
        )

        # Queue
        self.queue_depth = Gauge(
            "queue_depth", "Jobs per type and state", ["job_type", "state"], namespace=ns, registry=reg
        )
        self.queue_oldest_age_seconds = Gauge(
            "queue_oldest_age_seconds",
            "Age of the oldest runnable job",
            ["job_type"],
            namespace=ns,
            registry=reg,
        )
        self.lease_expirations_total = Counter(
            "lease_expirations",
            "Expired leases recovered by the reaper",
            ["job_type"],
            namespace=ns,
            registry=reg,
        )
        self.dead_letters_total = Counter(
            "dead_letters", "Jobs moved to dead letter", ["job_type"], namespace=ns, registry=reg
        )

        # Eligibility, comparables, valuations
        self.candidates_screened_total = Counter(
            "candidates_screened",
            "Eligibility screening results",
            ["profile", "eligibility"],
            namespace=ns,
            registry=reg,
        )
        self.missing_critical_facts_total = Counter(
            "missing_critical_facts",
            "Screenings blocked by a missing critical fact",
            ["profile", "fact"],
            namespace=ns,
            registry=reg,
        )
        self.comparable_sample_size = Histogram(
            "comparable_sample_size",
            "Selected MK comparables per valuation",
            ["profile"],
            namespace=ns,
            registry=reg,
            buckets=_SAMPLE_BUCKETS,
        )
        self.comparable_sets_total = Counter(
            "comparable_sets", "Comparable sets by quality", ["quality"], namespace=ns, registry=reg
        )
        self.stale_valuations = Gauge(
            "stale_valuations", "Valuations currently stale", namespace=ns, registry=reg
        )

        # Reviews
        self.review_oldest_pending_age_seconds = Gauge(
            "review_oldest_pending_age_seconds",
            "Age of the oldest pending review case",
            ["profile"],
            namespace=ns,
            registry=reg,
        )
        self.review_age_at_decision_seconds = Histogram(
            "review_age_at_decision_seconds",
            "Pending-to-decision time",
            ["profile"],
            namespace=ns,
            registry=reg,
            buckets=_AGE_BUCKETS,
        )
        self.review_claim_conflicts_total = Counter(
            "review_claim_conflicts", "Claim attempts lost to another reviewer", namespace=ns, registry=reg
        )
        self.review_decisions_total = Counter(
            "review_decisions", "Review decisions", ["decision"], namespace=ns, registry=reg
        )

        # Outbox and delivery
        self.outbox_events = Gauge(
            "outbox_events",
            "Outbox rows by provider and state",
            ["provider", "state"],
            namespace=ns,
            registry=reg,
        )
        self.outbox_delivery_latency_seconds = Histogram(
            "outbox_delivery_latency_seconds",
            "Event created to provider acceptance (receipt, not review)",
            ["provider"],
            namespace=ns,
            registry=reg,
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3600),
        )
        self.outbox_delivery_attempts_total = Counter(
            "outbox_delivery_attempts",
            "Delivery attempts by outcome",
            ["provider", "outcome"],
            namespace=ns,
            registry=reg,
        )
        self.callback_verifications_total = Counter(
            "callback_verifications", "MCP Events callback challenges", ["result"], namespace=ns, registry=reg
        )
        self.inbound_rejections_total = Counter(
            "inbound_webhook_rejections",
            "Rejected inbound provider requests",
            ["provider", "reason"],
            namespace=ns,
            registry=reg,
        )

        # API / MCP
        self.request_duration_seconds = Histogram(
            "request_duration_seconds",
            "API/MCP request latency",
            ["surface", "route", "status_class"],
            namespace=ns,
            registry=reg,
            buckets=_SECONDS_BUCKETS,
        )
        self.authorization_denials_total = Counter(
            "authorization_denials",
            "Authentication/authorization denials",
            ["surface", "reason"],
            namespace=ns,
            registry=reg,
        )
        self.errors_total = Counter(
            "errors", "Typed application errors returned", ["surface", "code"], namespace=ns, registry=reg
        )

        # Resources
        self.browser_sessions_active = Gauge(
            "browser_sessions_active", "Concurrent crawler browser sessions", namespace=ns, registry=reg
        )
        self.db_connections = Gauge(
            "db_connections", "Database pool connections", ["state"], namespace=ns, registry=reg
        )
        self.storage_bytes = Gauge(
            "storage_bytes", "Bytes used per store", ["store"], namespace=ns, registry=reg
        )

        # LLM budget
        self.llm_tokens_total = Counter(
            "llm_tokens", "LLM extraction tokens", ["source_key", "kind"], namespace=ns, registry=reg
        )
        self.llm_cost_eur_micros_total = Counter(
            "llm_cost_eur_micros", "LLM cost in micro-EUR", ["source_key"], namespace=ns, registry=reg
        )

    # ------------------------------------------------------------------ label helpers

    def _capped(self, label: str, value: str) -> str:
        """Admit at most `max_dynamic_label_values` distinct values per dynamic label."""
        if value == OTHER:
            return OTHER
        with self._dynamic_lock:
            seen = self._dynamic_seen[label]
            if value in seen:
                return value
            if len(seen) >= self._max_dynamic:
                return OTHER
            seen.add(value)
            return value

    def source_label(self, source_key: str) -> str:
        if not isinstance(source_key, str) or not _SOURCE_KEY_RE.fullmatch(source_key):
            return OTHER
        if self._source_keys is not None and source_key not in self._source_keys:
            return OTHER
        return self._capped("source_key", source_key)

    def tool_label(self, tool: str) -> str:
        if not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool):
            return OTHER
        if self._tool_names is not None and tool not in self._tool_names:
            return OTHER
        return self._capped("tool", tool)

    def api_route_label(self, route: str) -> str:
        """Route template label; pass the matched router template, never the raw path."""
        if not isinstance(route, str):
            return OTHER
        label = route_label(route)
        if self._api_routes is not None and label not in self._api_routes:
            return OTHER
        return self._capped("route", label)

    # ------------------------------------------------------------------ recording helpers

    def set_build_info(self, build_id: str, app_env: str) -> None:
        version = build_id if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", build_id or "") else OTHER
        env = bounded_label(app_env, {"development", "test", "staging", "production"})
        self.build_info.clear()
        self.build_info.labels(version=version, app_env=env).set(1)

    def record_scheduler_cycle(self, *, planned_at: datetime, started_at: datetime, outcome: str) -> None:
        delay = (ensure_utc(started_at) - ensure_utc(planned_at)).total_seconds()
        self.scheduler_delay_seconds.observe(max(delay, 0.0))
        label = bounded_label(outcome, SCHEDULER_OUTCOMES)
        self.scheduler_cycles_total.labels(outcome=label).inc()
        if label == "ok":
            self.scheduler_last_success_timestamp.set(ensure_utc(started_at).timestamp())

    def record_fetch(
        self, source_key: str, *, page_type: str, outcome: AccessState | str, response_bytes: int = 0
    ) -> None:
        source = self.source_label(source_key)
        page = bounded_label(page_type, PAGE_TYPES)
        access = bounded_label(outcome, {s.value for s in AccessState})
        self.source_fetch_attempts_total.labels(source_key=source, page_type=page, outcome=access).inc()
        self.source_requests_total.labels(source_key=source).inc()
        if response_bytes > 0:
            self.source_response_bytes_total.labels(source_key=source).inc(response_bytes)
        if access == AccessState.OK.value:
            self.source_pages_succeeded_total.labels(source_key=source, page_type=page).inc()
        elif access in BLOCK_REASONS:
            self.source_pages_blocked_total.labels(source_key=source, reason=access).inc()

    def record_parser_failure(self, source_key: str, *, page_type: str) -> None:
        self.parser_failures_total.labels(
            source_key=self.source_label(source_key), page_type=bounded_label(page_type, PAGE_TYPES)
        ).inc()

    def record_scan(
        self, source_key: str, *, completeness: Completeness | str, watermark_lag: timedelta | None
    ) -> None:
        source = self.source_label(source_key)
        label = bounded_label(completeness, {c.value for c in Completeness})
        self.coverage_scans_total.labels(source_key=source, completeness=label).inc()
        self.coverage_last_scan_complete.labels(source_key=source).set(
            1 if label == Completeness.COMPLETE.value else 0
        )
        if watermark_lag is not None:
            self.watermark_lag_seconds.labels(source_key=source).set(max(watermark_lag.total_seconds(), 0.0))

    def record_detail_job(self, source_key: str, *, deduplicated: bool) -> None:
        source = self.source_label(source_key)
        metric = self.detail_jobs_deduplicated_total if deduplicated else self.detail_jobs_enqueued_total
        metric.labels(source_key=source).inc()

    def set_queue_depth(self, counts: Mapping[tuple[JobType | str, JobState | str], int]) -> None:
        job_types = {t.value for t in JobType}
        states = {s.value for s in JobState}
        self.queue_depth.clear()
        for (job_type, state), count in counts.items():
            self.queue_depth.labels(
                job_type=bounded_label(job_type, job_types), state=bounded_label(state, states)
            ).set(count)

    def set_queue_oldest_age(self, job_type: JobType | str, age: timedelta) -> None:
        label = bounded_label(job_type, {t.value for t in JobType})
        self.queue_oldest_age_seconds.labels(job_type=label).set(max(age.total_seconds(), 0.0))

    def record_lease_expiration(self, job_type: JobType | str) -> None:
        self.lease_expirations_total.labels(
            job_type=bounded_label(job_type, {t.value for t in JobType})
        ).inc()

    def record_dead_letter(self, job_type: JobType | str) -> None:
        self.dead_letters_total.labels(job_type=bounded_label(job_type, {t.value for t in JobType})).inc()

    def record_screening(
        self,
        profile: ProfileKey | str,
        eligibility: EligibilityState | str,
        missing_facts: Iterable[str] = (),
    ) -> None:
        prof = bounded_label(profile, {p.value for p in ProfileKey})
        self.candidates_screened_total.labels(
            profile=prof, eligibility=bounded_label(eligibility, {e.value for e in EligibilityState})
        ).inc()
        for fact in set(missing_facts):
            self.missing_critical_facts_total.labels(
                profile=prof, fact=bounded_label(fact, CRITICAL_FACTS)
            ).inc()

    def record_comparables(self, profile: ProfileKey | str, *, sample_size: int, quality: str) -> None:
        prof = bounded_label(profile, {p.value for p in ProfileKey})
        self.comparable_sample_size.labels(profile=prof).observe(max(sample_size, 0))
        self.comparable_sets_total.labels(quality=bounded_label(quality, COMPARABLE_QUALITY)).inc()

    def record_review_decision(
        self, profile: ProfileKey | str, decision: ReviewOutcome | str, age: timedelta
    ) -> None:
        prof = bounded_label(profile, {p.value for p in ProfileKey})
        self.review_decisions_total.labels(
            decision=bounded_label(decision, {o.value for o in ReviewOutcome})
        ).inc()
        self.review_age_at_decision_seconds.labels(profile=prof).observe(max(age.total_seconds(), 0.0))

    def record_claim_conflict(self) -> None:
        self.review_claim_conflicts_total.inc()

    def set_outbox_counts(self, provider: str, counts: Mapping[OutboxState | str, int]) -> None:
        prov = bounded_label(provider, PROVIDERS)
        states = {s.value for s in OutboxState}
        for state in states:
            self.outbox_events.labels(provider=prov, state=state).set(0)
        for state, count in counts.items():
            self.outbox_events.labels(provider=prov, state=bounded_label(state, states)).set(count)

    def record_delivery(self, provider: str, outcome: str, *, latency: timedelta | None = None) -> None:
        prov = bounded_label(provider, PROVIDERS)
        label = bounded_label(outcome, DELIVERY_OUTCOMES)
        self.outbox_delivery_attempts_total.labels(provider=prov, outcome=label).inc()
        if latency is not None and label == "delivered":
            self.outbox_delivery_latency_seconds.labels(provider=prov).observe(
                max(latency.total_seconds(), 0.0)
            )

    def record_verification(self, result: str) -> None:
        self.callback_verifications_total.labels(result=bounded_label(result, VERIFICATION_RESULTS)).inc()

    def record_inbound_rejection(self, provider: str, reason: str) -> None:
        self.inbound_rejections_total.labels(
            provider=bounded_label(provider, PROVIDERS), reason=bounded_label(reason, INBOUND_REJECT_REASONS)
        ).inc()

    def observe_request(self, surface: str, route_or_tool: str, status: int, seconds: float) -> None:
        surf = bounded_label(surface, SURFACES)
        route = self.tool_label(route_or_tool) if surf == "mcp" else self.api_route_label(route_or_tool)
        self.request_duration_seconds.labels(
            surface=surf, route=route, status_class=status_class(status)
        ).observe(max(seconds, 0.0))

    def record_auth_denial(self, surface: str, reason: str) -> None:
        self.authorization_denials_total.labels(
            surface=bounded_label(surface, SURFACES), reason=bounded_label(reason, AUTH_DENIAL_REASONS)
        ).inc()

    def record_error(self, surface: str, code: ErrorCode | str) -> None:
        self.errors_total.labels(surface=bounded_label(surface, SURFACES), code=error_code_label(code)).inc()

    def set_db_connections(self, *, in_use: int, idle: int, maximum: int) -> None:
        self.db_connections.labels(state="in_use").set(in_use)
        self.db_connections.labels(state="idle").set(idle)
        self.db_connections.labels(state="max").set(maximum)

    def set_storage_bytes(self, store: str, size: int) -> None:
        self.storage_bytes.labels(store=bounded_label(store, STORES)).set(max(size, 0))

    def record_llm_usage(
        self, source_key: str, *, input_tokens: int, output_tokens: int, cost_eur: Decimal
    ) -> None:
        source = self.source_label(source_key)
        if input_tokens > 0:
            self.llm_tokens_total.labels(source_key=source, kind="input").inc(input_tokens)
        if output_tokens > 0:
            self.llm_tokens_total.labels(source_key=source, kind="output").inc(output_tokens)
        micros = int((cost_eur * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
        if micros > 0:
            self.llm_cost_eur_micros_total.labels(source_key=source).inc(micros)


def render_metrics(metrics: AppMetrics) -> tuple[bytes, str]:
    """Body and content type for a `/metrics` endpoint (expose only on a private port)."""
    return generate_latest(metrics.registry), CONTENT_TYPE_LATEST


def label_names(registry: CollectorRegistry) -> dict[str, set[str]]:
    """Metric family name -> label names actually present in samples."""
    found: dict[str, set[str]] = {}
    for family in registry.collect():
        names = found.setdefault(family.name, set())
        for sample in family.samples:
            names.update(k for k in sample.labels if k not in {"le", "quantile"})
    return found


def assert_bounded_labels(registry: CollectorRegistry) -> None:
    """Raise ValueError when any application metric uses a non-allow-listed label name."""
    problems: list[str] = []
    for family, names in label_names(registry).items():
        if not family.startswith(f"{NAMESPACE}_"):
            continue
        for name in names:
            lowered = name.lower()
            if name not in ALLOWED_LABEL_NAMES or any(f in lowered for f in FORBIDDEN_LABEL_FRAGMENTS):
                problems.append(f"{family}:{name}")
    if problems:
        raise ValueError(f"unbounded or sensitive metric labels: {', '.join(sorted(problems))}")


_default: AppMetrics | None = None


def get_metrics() -> AppMetrics:
    """Process-wide metrics instance (created lazily)."""
    global _default  # noqa: PLW0603 - process singleton
    if _default is None:
        _default = AppMetrics()
    return _default
