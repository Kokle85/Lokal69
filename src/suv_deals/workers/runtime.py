"""Process wiring for the scheduler, worker, reconciler and dispatcher processes (spec 4, 9, 13, 26, 30).

`build_runtime(settings)` turns validated `Settings` into one `RuntimeContext`:

- ``Database`` with ``DATABASE_SET_ROLE`` (normally ``suv_backend``; ADR 0001) and a bounded pool;
- the crawl clients: `Crawl4AIClient.from_settings` only when ``SOURCE_NETWORK_ENABLED=true``, and an
  offline `FixtureCrawlClient` over saved fixture directories for ``mode: fixture`` sources. Every
  fetch of a job goes through a fresh `PolicyEnforcingCrawlClient` (URL policy, budget gate, DNS
  check, redirect validation) backed by a `DbBudgetGate` (persistent token bucket, Retry-After,
  circuit, access blocks shared by all workers);
- the snapshot store (``SNAPSHOT_STORAGE``, disabled by default), metrics, the cost profile, the
  optional vehicle taxonomy override and the injectable clock/sleep used by tests.

Safety defaults are enforced here, not by convention: a non-fixture source never gets a crawl client
while ``SOURCE_NETWORK_ENABLED=false`` (`SourceNetworkDisabled`, a typed blocker), and a fixture source
never reaches the network (its client only reads files; its DNS check uses an offline resolver).

The module also holds the small pieces every job handler shares: the system actor, the job execution
handle with its lease-loss flag, and `Disposition` -- the fenced job outcome a handler commits in the
same transaction as its domain writes (`apply_disposition`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import anyio

from suv_deals.adapters.base import CrawlClient, SourceAdapter
from suv_deals.adapters.crawl4ai_client import Crawl4AIClient
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.adapters.registry import build_adapter, registry_problems
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.crawling.policy_client import BudgetRefused, PolicyEnforcingCrawlClient
from suv_deals.crawling.rate_limits import DEFAULT_BACKOFF, BackoffPolicy, Deny, DenyReason, Wait, WaitReason
from suv_deals.crawling.url_policy import SourceUrlPolicy
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.costs import CostProfile, load_cost_profile
from suv_deals.domain.enums import JobState, SourceMode
from suv_deals.domain.taxonomy import VehicleTaxonomy
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode, SourcePaused, ValidationFailed
from suv_deals.netguard import Resolver, system_resolver
from suv_deals.observability.logging import configure_logging
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import jobs
from suv_deals.persistence.budgets import DbBudgetGate
from suv_deals.persistence.database import Conn, Database, fetch_all
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors
from suv_deals.persistence.jobs import ClaimedJob
from suv_deals.persistence.sources_repo import SourceRecord
from suv_deals.persistence.storage import SnapshotStore, snapshot_store_from_settings
from suv_deals.settings import REPO_ROOT, Settings

logger = logging.getLogger(__name__)

DEFAULT_COST_PROFILE: Final = Path("cost_profiles") / "default_unapproved.yaml"
FIXTURE_ROOT: Final = REPO_ROOT / "tests" / "adapters" / "fixtures"
# A fixture fetch never opens a connection; the policy client's fetch-time DNS check still runs, so
# it gets a constant public documentation-safe answer instead of real DNS (no lookup leaves the host).
OFFLINE_FIXTURE_ADDRESS: Final = "93.184.215.14"

Sleep = Callable[[float], Awaitable[None]]


class SourceNetworkDisabled(SourcePaused):
    """``SOURCE_NETWORK_ENABLED=false``: a non-fixture source may not make any real request."""

    blocker_code: Final = "source_network_disabled"

    def __init__(self) -> None:
        super().__init__("Source network access is disabled (SOURCE_NETWORK_ENABLED=false)")


class FixtureDataUnavailable(SourcePaused):
    """A ``mode: fixture`` source without saved fixture data in this process."""

    blocker_code: Final = "fixture_data_unavailable"

    def __init__(self) -> None:
        super().__init__("No fixture data is configured for this fixture source")


async def offline_fixture_resolver(host: str, port: int) -> list[str]:
    """Resolver for fixture sources only: the inner client never connects anywhere."""
    del host, port
    return [OFFLINE_FIXTURE_ADDRESS]


# --------------------------------------------------------------------------------------------
# Options and context
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Engineering defaults for the runtime processes (not provider-approved quotas)."""

    job_lease_seconds: float = 300.0
    #: Heartbeats run at this interval while a handler works (one third of the lease).
    heartbeat_seconds: float = 100.0
    idle_poll_seconds: float = 5.0
    #: Short host-spacing budget waits (minimum delay, another worker's navigation) are waited out
    #: inside the job up to this many seconds in total; longer ones release the job (`fail_retry`).
    max_inline_wait_seconds: float = 90.0
    max_inline_waits: int = 6
    #: Polling interval while another worker holds the host's navigation lease.
    inline_poll_seconds: float = 5.0
    #: After a failed crawler health/contract check, re-check at most this often.
    crawler_recheck_seconds: float = 300.0
    outbox_lease_seconds: float = 120.0
    outbox_batch: int = 20
    delivery_batch: int = 20
    #: Tax engine selection for MK import (category of the vehicle rule sets; jurisdiction).
    tax_jurisdiction: str = "MK"
    tax_vehicle_category: str = "passenger_car"

    def __post_init__(self) -> None:
        if not 0.05 <= self.job_lease_seconds <= 3600:
            raise ValueError("job_lease_seconds must be between 0.05 and 3600")
        if not 0 < self.heartbeat_seconds < self.job_lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        if self.max_inline_wait_seconds < 0 or self.max_inline_waits < 0:
            raise ValueError("inline wait limits must not be negative")
        if self.inline_poll_seconds <= 0:
            raise ValueError("inline_poll_seconds must be positive")


@dataclass(slots=True)
class CrawlSession:
    """The crawl client of ONE job and its budget gate (per-run page/detail caps live in the gate)."""

    client: PolicyEnforcingCrawlClient
    gate: DbBudgetGate
    adapter: SourceAdapter
    policy: SourceUrlPolicy


@dataclass(slots=True)
class RuntimeContext:
    """Everything a scheduler/worker/dispatcher process needs. Built by `build_runtime`."""

    settings: Settings
    db: Database
    clock: Clock = field(default_factory=SystemClock)
    options: RuntimeOptions = field(default_factory=RuntimeOptions)
    metrics: AppMetrics = field(default_factory=lambda: AppMetrics(process_metrics=False))
    network_client: CrawlClient | None = None
    fixture_client: FixtureCrawlClient | None = None
    resolver: Resolver = system_resolver
    snapshot_store: SnapshotStore | None = None
    cost_profile: CostProfile | None = None
    #: Vehicle taxonomy override for screening (None: the repository taxonomy).
    taxonomy: VehicleTaxonomy | None = None
    backoff: BackoffPolicy = DEFAULT_BACKOFF
    sleep: Sleep = anyio.sleep
    owns_db: bool = False
    #: Spec 8 startup check of the crawler service: last result (``None``: not checked yet).
    crawler_ready: bool | None = None
    crawler_checked_at: datetime | None = None
    crawler_problems: tuple[str, ...] = ()
    _closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def dashboard_base_url(self) -> str:
        return self.settings.app_base_url

    async def ensure_crawler_ready(self) -> None:
        """Health + contract inspection of the crawler before real fetches (spec 8).

        Runs at the first real fetch and again at most every ``crawler_recheck_seconds`` after a
        failed check; a failing crawler raises `DependencyUnavailable` (the job retries later with
        backoff), so no fetch is attempted through a misconfigured or unreachable service. Clients
        without an ``inspect_contract`` method (test doubles) are not checked.
        """
        inspect = getattr(self.network_client, "inspect_contract", None)
        if inspect is None:
            return
        now = ensure_utc(self.clock.now())
        recheck = timedelta(seconds=self.options.crawler_recheck_seconds)
        if self.crawler_checked_at is None or (
            not self.crawler_ready and now - self.crawler_checked_at >= recheck
        ):
            try:
                report = await inspect()
                problems = tuple(str(p) for p in getattr(report, "problems", ()))
                ready = bool(getattr(report, "ok", False))
            except AppError as exc:
                problems, ready = (exc.code.value,), False
            self.crawler_ready, self.crawler_problems, self.crawler_checked_at = ready, problems, now
            log = logger.info if ready else logger.warning
            log("crawler contract check", extra={"ready": ready, "problems": list(problems)[:10]})
        if not self.crawler_ready:
            raise DependencyUnavailable("The crawler failed its health/contract check")

    async def open_crawl_session(self, workspace_id: UUID, source: SourceRecord) -> CrawlSession:
        """`crawl_session` plus the crawler readiness check for real (non-fixture) sources."""
        session = self.crawl_session(workspace_id, source)
        if source.mode != SourceMode.FIXTURE:
            await self.ensure_crawler_ready()
        return session

    def crawl_session(self, workspace_id: UUID, source: SourceRecord) -> CrawlSession:
        """A fresh policy-enforcing client + budget gate for one job of ``source``.

        Raises `SourceNetworkDisabled` (non-fixture source while ``SOURCE_NETWORK_ENABLED=false``),
        `FixtureDataUnavailable`, or `SourcePaused` when the registry refuses the adapter.
        """
        config = source.source_config()
        problems = registry_problems(config)
        if problems:
            raise SourcePaused(f"source {source.source_key} is gated: {'; '.join(problems)}")
        adapter = build_adapter(config)
        inner: CrawlClient
        if source.mode == SourceMode.FIXTURE:
            if self.fixture_client is None:
                raise FixtureDataUnavailable()
            inner = self.fixture_client
            resolver: Resolver = offline_fixture_resolver
        else:
            if not self.settings.source_network_enabled:
                raise SourceNetworkDisabled()
            if self.network_client is None:
                raise DependencyUnavailable("The crawler client is not configured")
            inner = self.network_client
            resolver = self.resolver
        policy = SourceUrlPolicy.from_source(config)
        gate = DbBudgetGate(
            self.db,
            workspace_id,
            {source.source_key: source.rate_budget()},
            policy=self.backoff,
            source_hosts={source.source_key: source.budget_hosts()},
        )
        client = PolicyEnforcingCrawlClient(
            inner, {source.source_key: policy}, gate, resolver=resolver, clock=self.clock
        )
        return CrawlSession(client=client, gate=gate, adapter=adapter, policy=policy)

    def add_closer(self, closer: Callable[[], Awaitable[None]]) -> None:
        self._closers.append(closer)

    async def aclose(self) -> None:
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # closing must never mask the original shutdown reason
                logger.warning("runtime component failed to close", exc_info=True)
        self._closers.clear()
        if self.owns_db:
            await self.db.close()


def discover_fixture_dirs(root: Path = FIXTURE_ROOT) -> tuple[Path, ...]:
    """Every saved fixture directory (``<root>/<source_key>/MANIFEST.yaml``)."""
    if not root.is_dir():
        return ()
    return tuple(sorted(p.parent for p in root.glob("*/MANIFEST.yaml")))


async def build_runtime(
    settings: Settings,
    *,
    application_name: str = "suv-deals",
    clock: Clock | None = None,
    options: RuntimeOptions | None = None,
    fixture_dirs: Sequence[Path] | None = None,
    metrics: AppMetrics | None = None,
    taxonomy: VehicleTaxonomy | None = None,
    configure_logs: bool = False,
) -> RuntimeContext:
    """Open the database and construct every client from ``settings`` (see module docstring).

    ``fixture_dirs=None`` uses the repository's saved fixtures outside production; production gets
    no fixture data unless directories are passed explicitly. Process entry points pass
    ``configure_logs=True`` (JSON logs with redaction, `observability.logging.configure_logging`).
    """
    if configure_logs:
        configure_logging(settings)
    if settings.database_url is None or not settings.database_url.get_secret_value():
        raise ValidationFailed("DATABASE_URL is not configured")
    db = Database(
        settings.database_url.get_secret_value(),
        min_size=settings.database_pool_min,
        max_size=settings.database_pool_max,
        set_role=settings.database_set_role,
        application_name=application_name,
    )
    await db.open()
    resolved_clock = clock or SystemClock()
    ctx = RuntimeContext(
        settings=settings,
        db=db,
        clock=resolved_clock,
        options=options or RuntimeOptions(),
        metrics=metrics or AppMetrics(),
        taxonomy=taxonomy,
        owns_db=True,
    )
    try:
        if settings.source_network_enabled:
            crawler = Crawl4AIClient.from_settings(settings)
            ctx.network_client = crawler
            ctx.add_closer(crawler.aclose)
        dirs = (
            tuple(fixture_dirs)
            if fixture_dirs is not None
            else (() if settings.app_env == "production" else discover_fixture_dirs())
        )
        if dirs:
            ctx.fixture_client = FixtureCrawlClient(dirs, clock=resolved_clock)
        ctx.snapshot_store = snapshot_store_from_settings(
            mode=settings.snapshot_storage,
            local_dir=settings.snapshot_local_dir,
            supabase_url=settings.supabase_url,
            secret_key=settings.supabase_secret_key,
            bucket=settings.supabase_storage_bucket,
        )
        profile_path = settings.config_dir / DEFAULT_COST_PROFILE
        ctx.cost_profile = load_cost_profile(profile_path) if profile_path.is_file() else None
        ctx.metrics.set_build_info(settings.build_id, settings.app_env)
    except BaseException:
        await ctx.aclose()
        raise
    return ctx


# --------------------------------------------------------------------------------------------
# Workspace fan-out and the system actor
# --------------------------------------------------------------------------------------------


async def active_workspace_ids(db: Database) -> list[UUID]:
    """Active workspaces through the narrowly scoped ``ops.active_workspace_ids()`` (ADR 0001 point 5).

    Runs without a workspace GUC; the SECURITY DEFINER function is the only cross-workspace read.
    """
    async with mapped_errors(), db.transaction() as conn, mapped_errors():
        rows = await fetch_all(conn, "select id from ops.active_workspace_ids() as id")
    return [row["id"] for row in rows]


def system_actor(workspace_id: UUID, purpose: str) -> ActorContext:
    """The system principal for one workspace; the request id names the process step."""
    return ActorContext.system(workspace_id, request_id=f"{purpose[:40]}:{uuid4().hex[:12]}")


# --------------------------------------------------------------------------------------------
# Job execution and dispositions
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class JobExecution:
    """One leased job inside a handler. The runner's heartbeat flags a lost lease here."""

    job: ClaimedJob
    worker_id: str
    actor: ActorContext
    started_at: datetime
    lease_lost: bool = False

    def mark_lost(self) -> None:
        self.lease_lost = True

    def check_lease(self) -> None:
        """Stop before more network work or a commit attempt once the lease is known lost."""
        if self.lease_lost:
            raise LeaseLost()


DispositionKind = Literal["complete", "retry", "blocked", "dead_letter"]


@dataclass(frozen=True, slots=True)
class Disposition:
    """The fenced outcome of a job, applied inside the handler's commit transaction."""

    kind: DispositionKind
    code: str | None = None
    detail: str | None = None
    retry_at: datetime | timedelta | None = None
    result: dict[str, Any] | None = None

    @classmethod
    def complete(cls, result: dict[str, Any] | None = None) -> Disposition:
        return cls("complete", result=result)

    @classmethod
    def retry(
        cls, code: str, retry_at: datetime | timedelta | None, detail: str | None = None
    ) -> Disposition:
        return cls("retry", code=code, retry_at=retry_at, detail=detail)

    @classmethod
    def blocked(cls, code: str, detail: str | None = None) -> Disposition:
        return cls("blocked", code=code, detail=detail)

    @classmethod
    def dead(cls, code: str, detail: str | None = None) -> Disposition:
        return cls("dead_letter", code=code, detail=detail)


async def apply_disposition(conn: Conn, job: ClaimedJob, disposition: Disposition) -> JobState:
    """The guarded job update; `LeaseLost` (zero rows) rolls back the caller's whole transaction."""
    kind = disposition.kind
    if kind == "complete":
        await jobs.complete(conn, job, disposition.result)
        return JobState.SUCCEEDED
    code = disposition.code or ErrorCode.INTERNAL_ERROR.value
    if kind == "retry":
        retry_at = disposition.retry_at
        if isinstance(retry_at, datetime):
            retry_at = ensure_utc(retry_at)
        return await jobs.fail_retry(conn, job, code, retry_at, detail=disposition.detail)
    if kind == "blocked":
        await jobs.fail_blocked(conn, job, code, disposition.detail)
        return JobState.BLOCKED
    await jobs.dead_letter(conn, job, code, disposition.detail)
    return JobState.DEAD_LETTER


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """What a handler reports back to the runner (logs and metrics only; already committed)."""

    state: JobState
    code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


JobHandler = Callable[[RuntimeContext, JobExecution], Awaitable[JobOutcome]]


def backoff_delay(attempt: int, *, base_seconds: int = 30, max_seconds: int = 3600) -> timedelta:
    """Deterministic exponential backoff for job-level retries (attempt is 1-based)."""
    exponent = min(max(attempt, 1) - 1, 16)
    return timedelta(seconds=min(max_seconds, base_seconds * (1 << exponent)))


_INLINE_WAITS: Final = frozenset({WaitReason.MIN_DELAY, WaitReason.NAVIGATION_IN_FLIGHT})


async def call_with_budget[T](
    ctx: RuntimeContext, execution: JobExecution, operation: Callable[[], Awaitable[T]]
) -> T:
    """Run one budgeted fetch; wait out SHORT host-spacing waits inside the job.

    Only `WaitReason.MIN_DELAY` (the per-host spacing between navigations) and
    `WaitReason.NAVIGATION_IN_FLIGHT` (another worker's navigation on the same host; polled, because
    it usually ends before its lease) are waited for, at most ``max_inline_waits`` times and at most
    ``max_inline_wait_seconds`` in total. Every other refusal (Retry-After, backoff, circuit, daily
    budget, run cap, access block) and any longer wait propagates as `BudgetRefused`, so the job is
    released with that time (never evaded, never burst).
    """
    waits = 0
    waited = 0.0
    options = ctx.options
    while True:
        execution.check_lease()
        try:
            return await operation()
        except BudgetRefused as exc:
            decision = exc.decision
            if not isinstance(decision, Wait) or decision.reason not in _INLINE_WAITS:
                raise
            delay = max(0.0, (ensure_utc(decision.until) - ensure_utc(ctx.clock.now())).total_seconds())
            if decision.reason == WaitReason.NAVIGATION_IN_FLIGHT:
                delay = min(delay, options.inline_poll_seconds)
            if waits >= options.max_inline_waits or waited + delay > options.max_inline_wait_seconds:
                raise
            waits += 1
            waited += delay
            await ctx.sleep(delay + 0.05)


def refusal_disposition(exc: BudgetRefused, attempt: int) -> Disposition:
    """How a job is released when the budget gate refused a request (nothing was fetched)."""
    decision = exc.decision
    if isinstance(decision, Deny):
        if decision.reason == DenyReason.ACCESS_BLOCKED:
            return Disposition.blocked(
                "host_access_blocked", "the host is access-blocked; owner action needed"
            )
        if decision.reason == DenyReason.BUDGET_EXHAUSTED:
            return Disposition.retry("DAILY_BUDGET_EXHAUSTED", decision.until or backoff_delay(attempt))
        if decision.reason == DenyReason.CIRCUIT_OPEN:
            return Disposition.retry("CIRCUIT_OPEN", decision.until or backoff_delay(attempt))
        return Disposition.retry("RUN_CAP_REACHED", timedelta(minutes=15))
    if isinstance(decision, Wait):
        return Disposition.retry(f"BUDGET_WAIT_{decision.reason.value}".upper(), decision.until)
    return Disposition.retry("BUDGET_REFUSED", backoff_delay(attempt))  # pragma: no cover - Allow


def disposition_for_error(exc: BaseException, job: ClaimedJob) -> Disposition | None:
    """Map an exception that escaped a handler to a job outcome (``None``: the lease is lost).

    Typed blockers never get a retry timer; retryable dependency failures back off with bounded
    attempts (`fail_retry` dead-letters exhausted jobs); non-retryable application errors are
    dead letters that stay visible. Messages are redacted by `jobs` before they are stored.
    """
    if isinstance(exc, LeaseLost):
        return None
    if isinstance(exc, BudgetRefused):
        return refusal_disposition(exc, job.attempts)
    if isinstance(exc, SourceNetworkDisabled | FixtureDataUnavailable):
        return Disposition.blocked(exc.blocker_code, exc.message)
    if isinstance(exc, AppError):
        if exc.code == ErrorCode.SOURCE_PAUSED:
            return Disposition.blocked("source_paused", exc.message)
        if exc.code == ErrorCode.ACCESS_BLOCKED:
            return Disposition.blocked("access_blocked", exc.message)
        if exc.code == ErrorCode.RATE_LIMITED:
            wait = timedelta(seconds=exc.retry_after_seconds) if exc.retry_after_seconds else None
            return Disposition.retry("RATE_LIMITED", wait or backoff_delay(job.attempts), exc.message)
        if exc.retryable:
            return Disposition.retry(exc.code.value, backoff_delay(job.attempts), exc.message)
        return Disposition.dead(exc.code.value, exc.message)
    # Unexpected failures may be transient; attempts stay bounded (then dead letter).
    return Disposition.retry("UNEXPECTED_ERROR", backoff_delay(job.attempts), type(exc).__name__)


__all__ = [
    "DEFAULT_COST_PROFILE",
    "CrawlSession",
    "Disposition",
    "FixtureDataUnavailable",
    "JobExecution",
    "JobHandler",
    "JobOutcome",
    "RuntimeContext",
    "RuntimeOptions",
    "SourceNetworkDisabled",
    "active_workspace_ids",
    "apply_disposition",
    "backoff_delay",
    "build_runtime",
    "call_with_budget",
    "discover_fixture_dirs",
    "disposition_for_error",
    "offline_fixture_resolver",
    "refusal_disposition",
    "system_actor",
]
