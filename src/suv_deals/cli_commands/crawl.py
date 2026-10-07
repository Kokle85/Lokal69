"""``suv-deals crawl once``: one bounded discovery run for ONE registered source (spec 9, 27).

Safety rules (none of them can be overridden from the command line):

- the source must exist in the registry, be enabled, unpaused, not access-blocked or
  parser-unhealthy, and pass every activation gate (YAML pre-check, then the runtime record);
- a real (non-fixture) source additionally needs ``SOURCE_NETWORK_ENABLED=true``; a
  ``mode: fixture`` source only reads saved fixture files;
- there is no URL argument: the adapter builds its own search request from the profile, and every
  request still passes the URL policy, robots handling and the persistent budget gate;
- ``--max-pages`` can only LOWER the source's per-run page budget (the budget gate enforces it).

The run is an ordinary discovery job (highest priority, one attempt) processed by an in-process
worker, so leases, fencing, evidence, parser health and access-block handling are exactly those of
the worker. Detail jobs it enqueues are left for ``suv-deals worker``.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    CliContext,
    echo,
    fail,
    load_settings,
    pass_cli,
    refuse,
    run_async,
    safe,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.persistence.sources_repo import SourceRecord
    from suv_deals.workers.runtime import RuntimeContext

MAX_PAGES_LIMIT = 20


@click.group("crawl")
def crawl_group() -> None:
    """Bounded manual crawling through the normal gates."""


def page_capped_runtime(ctx: RuntimeContext, page_cap: int) -> RuntimeContext:
    """The same runtime, but every crawl session's budget gate allows at most ``page_cap`` search
    pages per run (never more than the source's own budget)."""
    from suv_deals.workers.runtime import CrawlSession, RuntimeContext

    @dataclasses.dataclass(slots=True)
    class PageCappedRuntime(RuntimeContext):
        page_cap: int = 1

        def crawl_session(self, workspace_id: UUID, source: SourceRecord) -> CrawlSession:
            budget = source.rate_budget()
            capped = budget.model_copy(
                update={"max_search_pages_per_run": min(self.page_cap, budget.max_search_pages_per_run)}
            )
            config = {**source.config, "rate_budget": capped.model_dump(mode="json")}
            return RuntimeContext.crawl_session(
                self, workspace_id, source.model_copy(update={"config": config})
            )

    values: dict[str, Any] = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)}
    return PageCappedRuntime(**values, page_cap=page_cap)


def runtime_refusals(record: SourceRecord, *, network_enabled: bool, fixture_data: bool) -> list[str]:
    """Why the runtime registry entry may not run now (empty: allowed)."""
    from suv_deals.adapters.registry import registry_problems
    from suv_deals.domain.enums import SourceMode, TechnicalStatus
    from suv_deals.errors import AppError

    problems: list[str] = []
    if not record.enabled:
        problems.append("source is not enabled in the runtime registry")
    if record.paused:
        problems.append("source is paused")
    if record.technical_status in (TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY):
        problems.append(f"technical status is {record.technical_status.value} (owner action required)")
    problems.extend(record.activation_problems())
    try:
        problems.extend(registry_problems(record.source_config()))
    except AppError:  # an unreadable stored configuration is itself a refusal
        problems.append("stored source configuration is invalid")
    if record.mode == SourceMode.FIXTURE:
        if not fixture_data:
            problems.append("no saved fixture data is available to this process")
    elif not network_enabled:
        problems.append("SOURCE_NETWORK_ENABLED=false blocks every real fetch")
    return list(dict.fromkeys(problems))


@crawl_group.command("once")
@click.option("--source", "source_key", required=True, help="Registered source key (no URLs are accepted).")
@click.option(
    "--profile",
    type=click.Choice(["primary", "manual_4000", "below_target_watch"]),
    default="primary",
    show_default=True,
    help="Search profile (must be enabled in the recorded configuration).",
)
@click.option(
    "--max-pages",
    type=click.IntRange(1, MAX_PAGES_LIMIT),
    default=1,
    show_default=True,
    help="Search pages for this run; never more than the source's per-run budget.",
)
@workspace_option
@pass_cli
def once(cli: CliContext, source_key: str, profile: str, max_pages: int, workspace: UUID | None) -> None:
    """Run one bounded discovery for SOURCE through every source and runtime gate."""
    from suv_deals.adapters.registry import gate_status, load_registry
    from suv_deals.domain.enums import SourceMode
    from suv_deals.errors import AppError, NotFound

    settings = load_settings(cli)
    try:
        registry = load_registry(settings.config_dir)
    except AppError as exc:
        fail(exc.message)
    yaml_config = next((c for c in registry.configs if c.source_key == source_key), None)
    if yaml_config is not None:
        gate = gate_status(yaml_config)
        if not gate.active:
            refuse(f"source {source_key} is not active: " + "; ".join(gate.problems))
        if yaml_config.mode != SourceMode.FIXTURE and not settings.source_network_enabled:
            refuse("SOURCE_NETWORK_ENABLED=false blocks every real fetch")

    async def body() -> int:
        from suv_deals.cli_commands._common import resolve_workspace
        from suv_deals.crawling.discovery import DEFAULT_PARTITION
        from suv_deals.domain.enums import JobType, ProfileKey
        from suv_deals.persistence import config_repo, jobs, sources_repo
        from suv_deals.persistence.transactions import unit_of_work
        from suv_deals.workers.handlers import default_registry
        from suv_deals.workers.runner import Worker
        from suv_deals.workers.runtime import build_runtime, system_actor

        base = await build_runtime(settings, application_name="suv-deals-crawl-once", configure_logs=True)
        try:
            workspace_id = await resolve_workspace(base.db, workspace)
            actor = system_actor(workspace_id, "crawl-once")
            async with unit_of_work(base.db, actor) as conn:
                try:
                    record = await sources_repo.get_source_by_key(conn, actor, source_key)
                except NotFound:
                    fail(f"source {source_key} is not registered in this workspace (run `sources sync`)")
                try:
                    await config_repo.current_config(conn, actor)
                    configured = True
                except NotFound:
                    configured = False
                profiles = await config_repo.list_profiles(conn, actor)
            refusals = runtime_refusals(
                record,
                network_enabled=settings.source_network_enabled,
                fixture_data=base.fixture_client is not None,
            )
            if refusals:
                refuse(f"source {source_key} may not run: " + "; ".join(refusals))
            if not configured:
                fail("no business configuration is recorded (run `suv-deals config apply`)")
            stored = next((p for p in profiles if p.profile_key == ProfileKey(profile)), None)
            if stored is None or not stored.enabled:
                refuse(f"profile {profile} is not enabled in the recorded configuration")
            budget = record.rate_budget().max_search_pages_per_run
            cap = min(max_pages, budget)
            if max_pages > budget:
                echo(f"--max-pages {max_pages} exceeds the source budget; capped to {budget}.")
            spec = jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key=f"cli-crawl-once:{source_key}:{profile}:{uuid4().hex}",
                payload={
                    "source_id": str(record.id),
                    "profile_id": str(stored.id),
                    "partition_key": DEFAULT_PARTITION,
                    "requested_by": "operator-cli",
                    "max_pages": cap,
                },
                priority=1000,
                max_attempts=1,
                partition_key=DEFAULT_PARTITION,
                source_id=record.id,
                profile_id=stored.id,
            )
            async with unit_of_work(base.db, actor) as conn:
                job_id, _created = await jobs.enqueue(conn, actor, spec)
            echo(f"Enqueued discovery job {job_id} ({source_key}, profile {profile}, at most {cap} page(s)).")
            ctx = page_capped_runtime(base, cap)
            worker = Worker(
                ctx,
                default_registry(),
                worker_id=f"cli-crawl-once:{uuid4().hex[:12]}",
                job_types=(JobType.DISCOVERY,),
                workspace_ids=(workspace_id,),
            )
            report = await worker.run_once()
        finally:
            await base.aclose()
        if report is None:
            fail("the job could not be claimed; it stays queued for `suv-deals worker`")
        if report.job_id != job_id:
            fail(
                "another due discovery job was claimed first and processed; "
                f"job {job_id} stays queued for `suv-deals worker`"
            )
        state = "lease lost" if report.state is None else report.state.value
        echo(f"Job state: {state}" + (f" ({report.code})" if report.code else ""))
        for key, value in sorted(report.details.items()):
            echo(f"  {key}: {safe(value)}")
        echo("Detail jobs from this run (if any) are processed by `suv-deals worker`.")
        return 0 if report.state is not None and report.state.value == "succeeded" else EXIT_PROBLEMS

    run_async(body)
