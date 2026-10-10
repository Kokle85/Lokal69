"""SYNTHETIC helpers for the WP9 runtime pipeline tests (scheduler, worker, reconciler, dispatcher).

Everything here is synthetic: the ``fixture_dealer_de`` source serves hand-written pages for the
reserved host ``dealer.example`` through `FixtureCrawlClient` (no network), the comparables, FX
rate, taxonomy entry and callback host are invented and visibly labelled. Arrangement that the
application cannot do itself (users, time travel, budget refills) uses the superuser ``seed``
connection; everything under test runs as ``suv_backend`` through the runtime's own `Database`.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from tests.integration.db.helpers import Seed

from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.comparables import MarketObservation
from suv_deals.domain.enums import (
    Confidence,
    Drive,
    EvidenceKind,
    Fuel,
    FxPurpose,
    Gearbox,
    JobType,
    Role,
    TermsDecision,
    TermsStatus,
)
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.sources import SourceConfig
from suv_deals.domain.taxonomy import VehicleTaxonomy, parse_taxonomy
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.integrations.webhook_signing import parse_whsec
from suv_deals.persistence import (
    bindings_repo,
    config_repo,
    market_repo,
    sources_repo,
    subscriptions_repo,
    valuation_repo,
)
from suv_deals.persistence.database import Conn
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.runtime import RuntimeContext, RuntimeOptions, build_runtime

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "adapters" / "fixtures"
DEALER_DE = FIXTURES / "fixture_dealer_de"
SOURCE_KEY = "fixture_dealer_de"
SEARCH_URL = "https://dealer.example/suche?typ=suv"
BLOCKED_SEARCH_URL = "https://dealer.example/suche?typ=gesperrt"  # captcha served with 403
RATE_LIMITED_SEARCH_URL = "https://dealer.example/suche?typ=viel"  # 429 with Retry-After: 120
CALLBACK = "https://callback.synthetic.example/hooks/review"
DASHBOARD = "https://dashboard.synthetic.example"
ENCRYPTION_KEY = base64.b64encode(b"S" * 32).decode("ascii")  # SYNTHETIC test key


def system(workspace_id: UUID) -> ActorContext:
    return ActorContext.system(workspace_id, request_id=f"test-{uuid.uuid4().hex[:12]}")


def user_actor(workspace_id: UUID, user_id: UUID, role: Role = Role.OWNER) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=user_id,
        principal_kind="user",
        role=role,
        scopes=ROLE_SCOPES[role],
        request_id=f"test-{uuid.uuid4().hex[:12]}",
    )


async def run[T](ctx: RuntimeContext, actor: ActorContext, fn: Callable[[Conn], Awaitable[T]]) -> T:
    async with unit_of_work(ctx.db, actor) as conn:
        return await fn(conn)


# --------------------------------------------------------------------------------------------
# Synthetic configuration
# --------------------------------------------------------------------------------------------


def synthetic_taxonomy() -> VehicleTaxonomy:
    """The repository taxonomy has no "Example Trail": add a clearly synthetic SUV entry."""
    return parse_taxonomy(
        {
            "verification": "unverified_reference",
            "version": "synthetic-pipeline-1",
            "note": "SYNTHETIC test taxonomy for the fixture dealer pages",
            "makes": [
                {
                    "canonical": "Example",
                    "models": [
                        {
                            "canonical": "Trail",
                            "class": "suv",
                            "generations": [
                                {"code": "SYN1", "label": "Trail I (synthetic)", "from_year": 2006},
                            ],
                        }
                    ],
                }
            ],
        }
    )


def fixture_source_config(*, search_url: str = SEARCH_URL, **overrides: Any) -> SourceConfig:
    """``fixture_dealer_de`` enabled in memory with a SYNTHETIC terms decision (never in the YAML)."""
    data = yaml.safe_load((DEALER_DE / "source.yaml").read_text(encoding="utf-8"))
    config = SourceConfig.model_validate(data)
    update: dict[str, Any] = {
        "enabled": True,
        "terms_status": TermsStatus.PERMITTED,
        "terms_decision": TermsDecision.PROCEED_PERMITTED,
        "terms_decision_actor": "synthetic-fixture",
        "terms_decision_note": "synthetic fixture source on a reserved example host; no real provider",
        "search": {**config.search, "search_url": search_url},
    }
    update.update(overrides)
    return SourceConfig.model_validate({**config.model_dump(), **update})


def pipeline_settings(db_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "app_base_url": DASHBOARD,
        "database_url": db_url,
        "database_set_role": "suv_backend",
        "database_pool_min": 1,
        "database_pool_max": 6,
        "build_id": "pipeline-test",
        "scheduler_interval_seconds": 900,
        "source_network_enabled": False,
        "allow_external_notifications": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def events_settings(db_url: str, **overrides: Any) -> Settings:
    """Native MCP Events selected and allowed (still needs an approved + verified binding)."""
    values: dict[str, Any] = {
        "allow_external_notifications": True,
        "event_bridge_enabled": True,
        "event_bridge_provider": "mcp_events",
        "mcp_events_enabled": True,
        "mcp_event_subscription_secret_encryption_key": ENCRYPTION_KEY,
    }
    values.update(overrides)
    return pipeline_settings(db_url, **values)


# --------------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------------


@dataclass
class PipelineEnv:
    ctx: RuntimeContext
    seed: Seed
    workspace_id: UUID
    owner: ActorContext
    system: ActorContext
    source_id: UUID
    profiles: dict[str, UUID]

    def refill_budgets(self) -> None:
        """Fast-forward the persistent per-host token buckets (the minimum delay "passed")."""
        self.seed.conn.execute(
            "update ops.host_budgets set tokens = capacity, refilled_at = clock_timestamp()"
            " where workspace_id = %s",
            (self.workspace_id,),
        )

    async def close(self) -> None:
        """Deactivate the test workspace (other tests' processes never visit it) and close."""
        self.seed.conn.execute("update app.workspaces set active = false where id = %s", (self.workspace_id,))
        await self.ctx.aclose()

    def scalar(self, query: str, *params: Any) -> Any:
        return self.seed.scalar(query, params)

    def rows(self, query: str, *params: Any) -> list[dict[str, Any]]:
        cur = self.seed.conn.execute(query, params)
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


async def build_env(
    db_url: str,
    seed: Seed,
    *,
    settings: Settings | None = None,
    source: SourceConfig | None = None,
    clock: Clock | None = None,
    options: RuntimeOptions | None = None,
    name: str = "Pipeline",
) -> PipelineEnv:
    """A workspace with an owner, the business config, the fixture source and a runtime."""
    ws = seed.workspace(f"{name} {uuid.uuid4().hex[:6]}")
    owner_id = seed.user()
    seed.membership(ws, owner_id, "owner")
    owner = user_actor(ws, owner_id)
    ctx = await build_runtime(
        settings or pipeline_settings(db_url),
        application_name="suv-deals-pipeline-test",
        clock=clock or SystemClock(),
        options=options or RuntimeOptions(job_lease_seconds=120, heartbeat_seconds=40),
        fixture_dirs=[DEALER_DE],
        taxonomy=synthetic_taxonomy(),
    )
    env = PipelineEnv(
        ctx=ctx,
        seed=seed,
        workspace_id=ws,
        owner=owner,
        system=system(ws),
        source_id=uuid.UUID(int=0),
        profiles={},
    )

    async def sleep(seconds: float) -> None:
        del seconds
        env.refill_budgets()

    ctx.sleep = sleep
    result = await run(
        ctx,
        owner,
        lambda c: config_repo.record_config_revision(
            c, owner, load_business_config(REPO / "config"), "synthetic pipeline configuration", None
        ),
    )
    env.profiles = {p.profile_key.value: p.id for p in result.profiles}
    actor = env.system

    async def sync(conn: Conn) -> UUID:
        await sources_repo.sync_sources_from_yaml(conn, actor, [source or fixture_source_config()])
        return (await sources_repo.get_source_by_key(conn, actor, SOURCE_KEY)).id

    env.source_id = await run(ctx, actor, sync)
    return env


# --------------------------------------------------------------------------------------------
# Market evidence, FX
# --------------------------------------------------------------------------------------------


def comparable(amount: str, *, year: int = 2011, mileage: str = "180000") -> MarketObservation:
    """A SYNTHETIC fixture-lineage MK asking-price comparable for the "Example Trail" fixture car."""
    return MarketObservation(
        id=uuid.uuid4(),
        source_key="fixture_mk_comparables",
        url=f"https://mk-classifieds.example/ad/{uuid.uuid4().hex[:10]}",
        observed_at=datetime.now(UTC) - timedelta(days=2),
        evidence_kind=EvidenceKind.ASKING_PRICE,
        amount=Money.of(amount, "EUR"),
        vehicle=VehicleSpec(
            make="Example",
            model="Trail",
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            first_registration=PartialDate(value=str(year), precision="year"),
            mileage_km=Decimal(mileage),
            engine_displacement_cm3=1995,
            power_kw=103,
        ),
        local_registration_status="locally_registered",
        is_fixture=True,
    )


async def seed_comparables(
    env: PipelineEnv, amounts: tuple[str, ...] = ("8600", "8900", "9100", "9400")
) -> None:
    actor = env.system

    async def insert(conn: Conn) -> None:
        for index, amount in enumerate(amounts):
            await market_repo.insert_market_observation(
                conn,
                actor,
                comparable(amount, mileage=str(170000 + 10000 * index)),
                confidence=Confidence.MEDIUM,
            )

    await run(env.ctx, actor, insert)


async def seed_fx(env: PipelineEnv) -> None:
    """SYNTHETIC EUR->MKD reference rate of the fixture lineage (not an ECB observation)."""
    actor = env.system
    rate = FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.5"),
        rate_date=date.today(),
        retrieved_at=datetime.now(UTC) - timedelta(hours=1),
        provider="SYNTHETIC fixture FX",
        purpose=FxPurpose.REFERENCE,
    )
    await run(env.ctx, actor, lambda c: valuation_repo.upsert_fx_rate(c, actor, rate, is_fixture=True))


# --------------------------------------------------------------------------------------------
# Notification route + MCP Events subscription
# --------------------------------------------------------------------------------------------


def new_secret() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode("ascii")


async def approve_events_route(env: PipelineEnv, *, verified: bool = True) -> UUID:
    """Owner approves, (verifies) and enables the native MCP Events route for candidates."""
    owner = env.owner

    async def go(conn: Conn) -> UUID:
        binding = await bindings_repo.create_binding(
            conn,
            owner,
            provider="mcp_events",
            label="SYNTHETIC dot events route",
            external_app_id="synthetic-app",
        )
        binding = await bindings_repo.approve_binding(
            conn,
            owner,
            binding.id,
            approval_reference="SYNTHETIC owner approval",
            expected_version=binding.row_version,
        )
        if verified:
            binding = await bindings_repo.mark_binding_verified(
                conn, owner, binding.id, expected_version=binding.row_version
            )
        preference = await bindings_repo.upsert_preferences(
            conn, owner, binding.id, event_categories=["candidate_discovery"]
        )
        preference = await bindings_repo.approve_preferences(
            conn,
            owner,
            preference.id,
            approval_reference="SYNTHETIC owner approval of candidate events",
            expected_version=preference.row_version,
        )
        await bindings_repo.set_preferences_enabled(
            conn, owner, preference.id, True, expected_version=preference.row_version
        )
        await bindings_repo.set_binding_enabled(
            conn, owner, binding.id, True, expected_version=binding.row_version
        )
        return binding.id

    return await run(env.ctx, owner, go)


@dataclass(frozen=True)
class Subscriber:
    actor: ActorContext
    secret: str
    record: subscriptions_repo.SubscriptionRecord


async def verified_subscriber(
    env: PipelineEnv, *, profile: str = "primary", url: str = CALLBACK
) -> Subscriber:
    """A reviewer with a membership, a subscription and a recorded successful verification."""
    user = env.seed.user()
    env.seed.membership(env.workspace_id, user, "reviewer")
    actor = user_actor(env.workspace_id, user, Role.REVIEWER)
    secret = new_secret()
    box = SecretBox.from_settings(env.ctx.settings)
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": profile},
        "delivery": {"mode": "webhook", "url": url, "secret": secret},
        "ttlMs": 3_600_000,
    }
    request = eb.validate_subscribe_params(params, actor, clock=SystemClock())
    created = await run(
        env.ctx, actor, lambda c: subscriptions_repo.create_or_refresh_subscription(c, actor, request, box)
    )
    now = datetime.now(UTC)
    result = eb.VerificationResult(
        ok=True,
        reason=None,
        detail=None,
        status_code=200,
        webhook_id="msg_SYNTHETIC",
        attempted_at=now,
        verified_at=now,
        secret_fingerprint=parse_whsec(secret).fingerprint,
    )
    record = await run(
        env.ctx,
        actor,
        lambda c: subscriptions_repo.record_verification(c, actor, created.record.id, result, box),
    )
    return Subscriber(actor=actor, secret=secret, record=record)


# --------------------------------------------------------------------------------------------
# Queue inspection
# --------------------------------------------------------------------------------------------


def jobs_of(env: PipelineEnv, job_type: JobType) -> list[dict[str, Any]]:
    return env.rows(
        "select id, state, attempts, last_error_code, blocker_code, available_at, result_reference,"
        " listing_id from ops.jobs where workspace_id = %s and job_type = %s order by created_at, id",
        env.workspace_id,
        job_type.value,
    )


def by_slid(env: PipelineEnv) -> Mapping[str, dict[str, Any]]:
    rows = env.rows(
        "select id, source_listing_id, eligibility_state, eligibility_profile, availability,"
        " current_revision_id from app.listings where workspace_id = %s",
        env.workspace_id,
    )
    return {r["source_listing_id"]: r for r in rows}


# --------------------------------------------------------------------------------------------
# A NON-fixture review case (for external delivery tests)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RealCase:
    listing_id: UUID
    revision_id: UUID
    case_id: UUID
    case_version: int
    event_id: UUID


async def real_case(env: PipelineEnv) -> RealCase:
    """A synthetic but NON-fixture source/listing with a pending case and its review.pending event."""
    from tests.integration.repos_valuation_reviews.builders import (  # noqa: PLC0415 - test helpers
        make_eligible,
        make_listing,
        primary_profile,
        screening,
    )

    from suv_deals.persistence import reviews_repo  # noqa: PLC0415

    source_id = env.seed.source(
        env.workspace_id,
        source_key=f"synthetic_public_{uuid.uuid4().hex[:8]}",
        mode="public_html",
        adapter="synthetic_adapter",
    )
    listing_id, revision_id = make_listing(env.seed, env.workspace_id, source_id, eligible=False)
    make_eligible(env.seed, env.workspace_id, listing_id)
    actor = env.system
    result = await run(
        env.ctx,
        actor,
        lambda c: reviews_repo.upsert_review_case(
            c,
            actor,
            listing_id,
            revision_id,
            screening(),
            None,
            primary_profile(),
            dashboard_base_url=DASHBOARD,
        ),
    )
    assert result.case_id is not None and result.case_version is not None and result.event_id is not None
    return RealCase(listing_id, revision_id, result.case_id, result.case_version, result.event_id)


def outbox_row(env: PipelineEnv, event_id: UUID) -> dict[str, Any]:
    [row] = env.rows(
        "select event_id, state, blocker_code, last_error_code, attempts, is_fixture, send_attempted_at,"
        " provider_accepted_at, owner_seen_at, destination_binding_id from ops.outbox"
        " where workspace_id = %s and event_id = %s",
        env.workspace_id,
        event_id,
    )
    return row


def deliveries_of(env: PipelineEnv, event_id: UUID) -> list[dict[str, Any]]:
    return env.rows(
        "select id, subscription_id, state, attempts, last_response_code, safe_error, accepted_at"
        " from ops.event_deliveries where workspace_id = %s and event_id = %s order by created_at",
        env.workspace_id,
        event_id,
    )


async def run_pipeline(env: PipelineEnv) -> None:
    """Scheduler tick + worker until idle (the fixture source, comparables and FX seeded first)."""
    from suv_deals.crawling.scheduler import run_scheduler_tick  # noqa: PLC0415
    from suv_deals.workers.runner import Worker  # noqa: PLC0415

    await seed_comparables(env)
    await seed_fx(env)
    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])
    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-setup").run_until_idle()


SLACK_CHANNEL = "C0SYNTHETIC1"


def slack_settings(db_url: str, **overrides: Any) -> Settings:
    """The optional Slack fallback selected (SYNTHETIC token values; nothing reaches Slack)."""
    values: dict[str, Any] = {
        "allow_external_notifications": True,
        "event_bridge_enabled": True,
        "event_bridge_provider": "slack",
        "notification_provider": "slack",
        "slack_bot_token": "xoxb-SYNTHETIC-test-token-not-real",
        "slack_signing_secret": "synthetic-signing-secret",
        "slack_channel_id": SLACK_CHANNEL,
    }
    values.update(overrides)
    return pipeline_settings(db_url, **values)


async def approve_slack_route(env: PipelineEnv) -> UUID:
    owner = env.owner

    async def go(conn: Conn) -> UUID:
        binding = await bindings_repo.create_binding(
            conn,
            owner,
            provider="slack",
            label="SYNTHETIC private review channel",
            external_workspace_id="T0SYNTHETIC1",
            external_channel_id=SLACK_CHANNEL,
        )
        binding = await bindings_repo.approve_binding(
            conn, owner, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        )
        binding = await bindings_repo.mark_binding_verified(
            conn, owner, binding.id, expected_version=binding.row_version
        )
        preference = await bindings_repo.upsert_preferences(
            conn, owner, binding.id, event_categories=["candidate_discovery"]
        )
        preference = await bindings_repo.approve_preferences(
            conn, owner, preference.id, approval_reference="SYNTHETIC approval", expected_version=1
        )
        await bindings_repo.set_preferences_enabled(
            conn, owner, preference.id, True, expected_version=preference.row_version
        )
        await bindings_repo.set_binding_enabled(
            conn, owner, binding.id, True, expected_version=binding.row_version
        )
        return binding.id

    return await run(env.ctx, owner, go)
