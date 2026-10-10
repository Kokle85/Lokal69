"""Long-running processes: ``worker``, ``scheduler``, ``dispatcher``, ``reconcile`` and ``api serve``.

Every process uses the same image and settings; each one reads only what it needs (see
docs/runbook.md). Safety switches are enforced inside the processes themselves:
``SOURCE_NETWORK_ENABLED=false`` blocks every real fetch, ``ALLOW_EXTERNAL_NOTIFICATIONS=false``
blocks every external delivery, fixture data never notifies.

Extension points (for later packages, e.g. the spec v1.1 inquiry/reply wiring):

- ``worker --registry suv_deals.<module>:<factory>``: a `HandlerRegistry` factory that registers
  additional job types (``workers.handlers.HandlerRegistry.register``);
- ``api serve --app-factory suv_deals.<module>:<factory>``: a ``factory(settings) -> ASGI app``
  wrapper around ``api.app.build_app`` that passes ``extra_routers``/``options``.

Only factories inside the ``suv_deals`` package are accepted.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from typing import Any, Final
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CliContext,
    database_url,
    echo,
    emit_json,
    fail,
    load_settings,
    parse_csv,
    pass_cli,
    refuse,
    run_async,
    table,
    warn,
)

DEFAULT_QUEUES = "discovery,detail,recheck,valuation"
DEFAULT_REGISTRY = "suv_deals.workers.handlers:default_registry"
DEFAULT_APP_FACTORY = "suv_deals.api.app:build_app"
_FACTORY_RE = re.compile(r"^(suv_deals(?:\.[a-z_][a-z0-9_]*)+):([A-Za-z_][A-Za-z0-9_]*)$")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def load_factory(spec: str, *, option: str) -> Callable[..., Any]:
    """Import ``suv_deals.<module>:<callable>`` (nothing outside the package)."""
    match = _FACTORY_RE.fullmatch(spec.strip())
    if match is None:
        fail(f"{option} must look like suv_deals.<module>:<callable>", EXIT_USAGE)
    try:
        module = importlib.import_module(match.group(1))
    except ModuleNotFoundError:
        fail(f"{option}: module not found", EXIT_USAGE)
    factory = getattr(module, match.group(2), None)
    if not callable(factory):
        fail(f"{option}: not a callable", EXIT_USAGE)
    return factory  # type: ignore[no-any-return]


def parse_queues(value: str) -> list[Any]:
    from suv_deals.domain.enums import JobType

    names = parse_csv(value)
    valid = {t.value for t in JobType}
    unknown = [n for n in names if n not in valid]
    if unknown or not names:
        fail(f"unknown queue(s): {', '.join(unknown) or '-'}; known: {', '.join(sorted(valid))}", EXIT_USAGE)
    return [JobType(n) for n in dict.fromkeys(names)]


# --------------------------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------------------------


@click.command("worker")
@click.option(
    "--queues",
    default=DEFAULT_QUEUES,
    show_default=True,
    help="Comma-separated job types this worker claims.",
)
@click.option("--drain", is_flag=True, help="Process due jobs until none is claimable, then exit.")
@click.option(
    "--max-jobs", type=click.IntRange(1, 100_000), default=1000, show_default=True, help="With --drain."
)
@click.option(
    "--registry",
    "registry_spec",
    default=DEFAULT_REGISTRY,
    show_default=True,
    help="Handler registry factory (extension point; suv_deals.* only).",
)
@pass_cli
def worker(cli: CliContext, queues: str, drain: bool, max_jobs: int, registry_spec: str) -> None:
    """Claim and process durable jobs (leases, heartbeats, fenced commits; SIGTERM finishes the job)."""
    settings = load_settings(cli)
    job_types = parse_queues(queues)
    registry = load_factory(registry_spec, option="--registry")()
    missing = [t.value for t in job_types if t not in registry]
    if missing:
        fail(f"no handler is registered for: {', '.join(missing)}", EXIT_USAGE)

    async def body() -> int:
        from suv_deals.workers.runner import Worker, run_worker
        from suv_deals.workers.runtime import build_runtime

        if not drain:
            await run_worker(settings, job_types=job_types, registry=registry)
            return 0
        ctx = await build_runtime(settings, application_name="suv-deals-worker", configure_logs=True)
        try:
            reports = await Worker(ctx, registry, job_types=job_types).run_until_idle(max_jobs=max_jobs)
        finally:
            await ctx.aclose()
        rows = [
            {
                "job_id": r.job_id,
                "type": r.job_type.value,
                "attempt": r.attempt,
                "state": "lease_lost" if r.state is None else r.state.value,
                "code": r.code,
            }
            for r in reports
        ]
        echo(table(rows, ["job_id", "type", "attempt", "state", "code"]) if rows else "No due jobs.")
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------------------------


@click.command("scheduler")
@click.option("--once", is_flag=True, help="Evaluate the due schedules once and exit.")
@pass_cli
def scheduler(cli: CliContext, once: bool) -> None:
    """Enqueue due discovery slots every SCHEDULER_INTERVAL_SECONDS (restart-safe, no bursts)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.crawling.scheduler import run_scheduler_process, run_scheduler_tick
        from suv_deals.workers.runtime import build_runtime

        if not once:
            await run_scheduler_process(settings)
            return 0
        ctx = await build_runtime(settings, application_name="suv-deals-scheduler", configure_logs=True)
        try:
            report = await run_scheduler_tick(ctx.db, settings, ctx.clock, metrics=ctx.metrics)
        finally:
            await ctx.aclose()
        echo(
            f"Tick {report.outcome}: {report.enqueued} discovery job(s) enqueued "
            f"for slot {report.planned_slot}"
        )
        for tick in report.workspaces:
            skipped = ", ".join(f"{k}={v}" for k, v in sorted(tick.skipped.items())) or "-"
            echo(
                f"  {tick.workspace_id}: enqueued {len(tick.enqueued)}, already {tick.already_scheduled}, "
                f"not due {tick.not_due}, skipped {skipped}" + (f", error {tick.error}" if tick.error else "")
            )
        return 0 if report.outcome in ("ok", "skipped") else EXIT_PROBLEMS

    run_async(body)


# --------------------------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------------------------


@click.command("dispatcher")
@click.option("--once", is_flag=True, help="Process every workspace's due events once and exit.")
@pass_cli
def dispatcher(cli: CliContext, once: bool) -> None:
    """Deliver committed outbox events through the single approved route (gated; off by default)."""
    settings = load_settings(cli)
    if not settings.allow_external_notifications:
        warn(
            "ALLOW_EXTERNAL_NOTIFICATIONS=false: events are recorded as blocked, nothing leaves the system; "
            "the dashboard/MCP queue stays complete."
        )

    async def body() -> int:
        from suv_deals.integrations.safe_http import SafeHttpClient
        from suv_deals.workers.dispatcher import Dispatcher, run_dispatcher
        from suv_deals.workers.runtime import build_runtime

        if not once:
            await run_dispatcher(settings)
            return 0
        ctx = await build_runtime(settings, application_name="suv-deals-dispatcher", configure_logs=True)
        http = SafeHttpClient(resolver=ctx.resolver, proxy=settings.callback_egress_proxy_url)
        try:
            reports = await Dispatcher(ctx, http=http).run_once()
        finally:
            await http.aclose()
            await ctx.aclose()
        for report in reports:
            echo(
                f"Workspace {report.workspace_id}: {len(report.events)} event(s), "
                f"{len(report.deliveries)} delivery(ies)"
            )
            for event in report.events:
                state = "lease_lost" if event.state is None else event.state.value
                echo(
                    f"  {event.event_id} {event.event_type}: {state}"
                    + (f" ({event.code})" if event.code else "")
                )
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------------------------


#: ``ReconcileReport`` fields per output line of ``suv-deals reconcile`` (text mode). A field not
#: listed here is still printed (under ``other``): nothing a pass did is ever hidden.
RECONCILE_REPORT_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "queue",
        (
            "jobs_requeued",
            "jobs_dead_lettered",
            "jobs_blocked_uncertain",
            "jobs_exhausted",
            "stale_detail_jobs",
        ),
    ),
    ("outbox", ("events_retry", "events_uncertain", "events_dead_lettered", "deliveries_uncertain")),
    (
        "inquiries",
        (
            "inquiry_plan_jobs",
            "inquiry_replan_jobs",
            "inquiry_send_jobs",
            "inquiry_retry_jobs",
            "inquiry_reconcile_jobs",
            "inquiry_send_jobs_unblocked",
            "send_attempts_uncertain",
            "inquiries_marked_replied",
        ),
    ),
    ("replies", ("reply_process_jobs",)),
    ("valuations", ("valuations_expired", "valuations_invalidated", "recompute_jobs", "watch_rechecks")),
    (
        "housekeeping",
        ("claims_expired", "crawl_runs_closed", "snapshots_deleted", "idempotency_deleted"),
    ),
)


def reconcile_report_lines(report: Mapping[str, object]) -> list[str]:
    """One ``group: name=value, ...`` line per `RECONCILE_REPORT_GROUPS` entry (``ReconcileReport``
    counters without ``workspace_id`` / ``errors`` / ``dry_run``), then ``other:`` for any field no
    group names, so a new counter is printed even before it is grouped."""
    lines: list[str] = []
    seen: set[str] = set()
    for title, names in RECONCILE_REPORT_GROUPS:
        present = [name for name in names if name in report]
        seen.update(present)
        if present:
            lines.append(f"{title}: " + ", ".join(f"{name}={report[name]}" for name in present))
    rest = [name for name in report if name not in seen]
    if rest:
        lines.append("other: " + ", ".join(f"{name}={report[name]}" for name in rest))
    return lines


@click.command("reconcile")
@click.option("--dry-run", is_flag=True, help="Report what one pass would do; everything is rolled back.")
@click.option("--loop", is_flag=True, help="Run as a process: one pass every interval until SIGTERM.")
@click.option(
    "--workspace",
    "workspace",
    type=click.UUID,
    default=None,
    help="One pass for this (active) workspace only (default: every active workspace).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def reconcile(cli: CliContext, dry_run: bool, loop: bool, workspace: UUID | None, as_json: bool) -> None:
    """Reapers, housekeeping, stale-detail and valuation sweeps (no network I/O)."""
    if dry_run and loop:
        fail("--dry-run and --loop cannot be combined", EXIT_USAGE)
    if loop and workspace is not None:
        fail("--workspace and --loop cannot be combined (the process covers every workspace)", EXIT_USAGE)
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import resolve_workspaces
        from suv_deals.workers.reconciliation import Reconciler, run_reconciler, run_reconciliation
        from suv_deals.workers.runtime import build_runtime

        if loop:
            await run_reconciler(settings)
            return 0
        ctx = await build_runtime(settings, application_name="suv-deals-reconciler", configure_logs=True)
        try:
            if workspace is None:
                reports = await run_reconciliation(ctx, dry_run=dry_run)
            else:
                targets = await resolve_workspaces(ctx.db, workspace)
                reconciler = Reconciler(ctx)
                reports = [await reconciler.reconcile_workspace(ws, dry_run=dry_run) for ws in targets]
        finally:
            await ctx.aclose()
        data = [r.as_dict() for r in reports]
        if as_json:
            emit_json(data)
        else:
            echo(f"Reconciliation pass ({'dry run, rolled back' if dry_run else 'committed'}):")
            for item in data:
                echo(f"  workspace {item.pop('workspace_id')}:")
                errors = item.pop("errors")
                item.pop("dry_run", None)
                for line in reconcile_report_lines(item):
                    echo(f"    {line}")
                for error in errors:
                    echo(f"    error: {error}")
            if not data:
                echo("  no active workspace")
        return EXIT_PROBLEMS if any(r.errors for r in reports) else 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# api serve
# --------------------------------------------------------------------------------------------


@click.group("api")
def api_group() -> None:
    """The dashboard API + MCP endpoint process."""


@api_group.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", type=click.IntRange(1, 65535), default=8000, show_default=True)
@click.option(
    "--allow-non-loopback",
    is_flag=True,
    help="Required for a non-loopback bind (containers bind 0.0.0.0 behind a 127.0.0.1-only published port).",
)
@click.option(
    "--proxy-headers/--no-proxy-headers",
    default=True,
    show_default=True,
    help="Honour X-Forwarded-For/Proto from --forwarded-allow-ips only (the reverse proxy).",
)
@click.option(
    "--forwarded-allow-ips",
    default="127.0.0.1",
    show_default=True,
    help="Comma-separated proxy addresses whose forwarded headers are trusted.",
)
@click.option(
    "--app-factory",
    default=DEFAULT_APP_FACTORY,
    show_default=True,
    help="factory(settings) -> ASGI app (extension point; suv_deals.* only).",
)
@pass_cli
def serve(
    cli: CliContext,
    *,
    host: str,
    port: int,
    allow_non_loopback: bool,
    proxy_headers: bool,
    forwarded_allow_ips: str,
    app_factory: str,
) -> None:
    """Serve /api, /healthz, /readyz and /mcp with uvicorn (one process; HTTPS at the reverse proxy)."""
    if host not in _LOOPBACK and not allow_non_loopback:
        refuse("binding a non-loopback address needs --allow-non-loopback (publish it on 127.0.0.1 only)")
    if proxy_headers and "*" in parse_csv(forwarded_allow_ips):
        warn("--forwarded-allow-ips '*' trusts forwarded headers from any client; prefer the proxy address")
    settings = load_settings(cli)
    database_url(settings)  # the API needs its database (opened lazily by the app lifespan)
    factory = load_factory(app_factory, option="--app-factory")
    from suv_deals.cli_commands._common import describe_error, exit_code_for
    from suv_deals.errors import AppError

    try:
        app = factory(settings)
    except AppError as exc:  # e.g. an unsafe production configuration: no traceback, redacted
        fail(describe_error(exc), exit_code_for(exc))

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=None,  # keep the application's redacting JSON logging
        access_log=False,  # requests are logged by the application middleware (redacted)
        server_header=False,
        lifespan="on",
        timeout_graceful_shutdown=20,
    )
