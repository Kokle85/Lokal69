"""Tenant isolation, scope enforcement and degraded health of the read-query service.

- Foreign-workspace and missing ids raise ``NOT_FOUND`` with an identical error payload.
- RLS is defence in depth: a ``suv_backend`` transaction scoped to workspace A cannot read
  workspace B rows even with NO workspace predicate (proved by direct probes).
- Scopes: ``deals:read`` and ``reviews:read`` guard their views; a viewer reads everything it may
  and never receives a claim token or another reviewer's identity.

SYNTHETIC data only; every query runs as ``suv_backend``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import (
    CURSOR_SECRET,
    SeededWorkspace,
    member,
    run,
    seed_foreign_workspace,
    viewer,
)
from tests.integration.read_queries.schema_check import assert_valid

from suv_deals.api.schemas import OutboxQuery
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound
from suv_deals.mcp.schemas import (
    DealsListCandidatesInput,
    ReviewsListPendingInput,
)
from suv_deals.persistence import queries
from suv_deals.persistence.database import Conn, Database, fetch_all
from suv_deals.persistence.queries.operations import SchemaMarker
from suv_deals.settings import Settings
from suv_deals.views.common import ErrorPayload

pytestmark = pytest.mark.db

Reader = Callable[[Conn, ActorContext, UUID], Awaitable[Any]]

FOREIGN_READS: dict[str, tuple[str, Reader]] = {
    "candidate": (
        "listing",
        queries.get_candidate,
    ),
    "older_revision": (
        "listing",
        lambda c, a, i: queries.get_candidate(c, a, i, 1),
    ),
    "valuation": (
        "valuation",
        queries.get_valuation,
    ),
    "comparables": (
        "comparable_set",
        lambda c, a, i: queries.get_comparables(c, a, i, secret=CURSOR_SECRET),
    ),
    "review_case": ("case", queries.get_review_case),
    "lifecycle": ("listing", queries.listing_lifecycle_view),
}


def _foreign_id(other: SeededWorkspace, kind: str) -> UUID:
    if kind == "listing":
        return other.listings["priced"]
    if kind == "valuation":
        return other.valuations["not_started"]
    if kind == "comparable_set":
        assert other.comparable_set_id is not None
        return other.comparable_set_id
    return other.cases["priced"]


async def _not_found(db: Database, actor: ActorContext, reader: Reader, target: UUID) -> ErrorPayload:
    with pytest.raises(NotFound) as error:
        await run(db, actor, lambda c: reader(c, actor, target))
    return ErrorPayload.from_app_error(error.value, correlation_id="req-synthetic")


@pytest.mark.parametrize("name", sorted(FOREIGN_READS))
async def test_foreign_and_missing_ids_are_indistinguishable(
    db: Database, seed: Seed, data: SeededWorkspace, name: str
) -> None:
    other = await seed_foreign_workspace(db, seed, name)
    kind, reader = FOREIGN_READS[name]
    actor = viewer(data.workspace_id)
    foreign = await _not_found(db, actor, reader, _foreign_id(other, kind))
    missing = await _not_found(db, actor, reader, uuid.uuid4())
    assert foreign.code == ErrorCode.NOT_FOUND
    assert foreign.model_dump() == missing.model_dump()
    # The owner of the foreign workspace can read the very same id.
    own_actor = viewer(other.workspace_id)
    await run(db, own_actor, lambda c: reader(c, own_actor, _foreign_id(other, kind)))


async def test_lists_never_include_another_workspace(db: Database, seed: Seed, data: SeededWorkspace) -> None:
    other = await seed_foreign_workspace(db, seed, "lists")
    actor = viewer(data.workspace_id)
    candidates = await run(
        db,
        actor,
        lambda c: queries.list_candidates(
            c, actor, DealsListCandidatesInput(limit=100), secret=CURSOR_SECRET
        ),
    )
    ids = {i.listing_id for i in candidates.data.items}
    assert ids == data.candidate_ids and other.listings["priced"] not in ids
    outbox = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(c, actor, OutboxQuery(limit=100), secret=CURSOR_SECRET),
    )
    assert other.outbox["uncertain"] not in {i.outbox_id for i in outbox.data.items}
    queue = await run(
        db,
        actor,
        lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(limit=100), secret=CURSOR_SECRET),
    )
    assert other.cases["priced"] not in {i.case_id for i in queue.data.items}
    sources = await run(db, actor, lambda c: queries.sources_view(c, actor))
    assert other.sources["running"] not in {s.source_id for s in sources.data.items}


#: Every table the read-query service reads (directly or through the repositories it calls).
PROBES = (
    "app.listings",
    "app.listing_revisions",
    "app.listing_observations",
    "app.field_evidence",
    "app.valuations",
    "app.comparable_sets",
    "app.review_cases",
    "app.review_decisions",
    "app.owner_notes",
    "app.sources",
    "app.search_profiles",
    "app.config_revisions",
    "app.destination_bindings",
    "ops.outbox",
    "ops.activation_gates",
    "ops.crawl_runs",
    "ops.source_schedules",
    "ops.audit_events",
    "ops.query_snapshots",
)


def _populate_foreign(seed: Seed, other: SeededWorkspace) -> None:
    """Rows of the foreign workspace in the tables its minimal seed leaves empty."""
    ws, source = other.workspace_id, other.sources["running"]
    listing, revision = other.listings["priced"], other.revisions["priced"][-1]
    run_id = seed.crawl_run(ws, source)
    seed.insert_id(
        "app.listing_observations",
        workspace_id=ws,
        source_id=source,
        listing_id=listing,
        crawl_run_id=run_id,
        position=1,
        source_listing_id=seed.scalar("select source_listing_id from app.listings where id = %s", (listing,)),
        card_hash="0" * 64,
        card_material=Jsonb({"synthetic": True}),
        observed_at=datetime.now(UTC),
        ingestion_key=f"synthetic:{uuid.uuid4()}",
    )
    seed.insert_id(
        "app.field_evidence",
        workspace_id=ws,
        listing_id=listing,
        revision_id=revision,
        field_path="price.amount_minor",
        method="css",
        confidence="high",
        observed_at=datetime.now(UTC),
    )
    profile = seed.scalar("select id from app.search_profiles where workspace_id = %s limit 1", (ws,))
    seed.insert_id(
        "ops.source_schedules",
        workspace_id=ws,
        source_id=source,
        profile_id=profile,
        next_due_at=datetime.now(UTC),
        coverage_mode="rolling_pages",
    )
    seed.insert_id(
        "app.destination_bindings",
        workspace_id=ws,
        provider="slack",
        label="SYNTHETIC foreign Slack channel",
        external_workspace_id="T0FOREIGN",
        external_channel_id="C0FOREIGN",
    )
    seed.decision(ws, other.cases["priced"], listing, revision, is_fixture=False)
    seed.insert_id(
        "app.owner_notes",
        workspace_id=ws,
        listing_id=listing,
        author_principal_id=uuid.uuid4(),
        author_kind="user",
        label="owner",
        body="SYNTHETIC foreign note",
    )


async def test_rls_blocks_cross_workspace_reads_without_any_predicate(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    other = await seed_foreign_workspace(db, seed, "rls")
    _populate_foreign(seed, other)
    foreign_actor = viewer(other.workspace_id)  # the foreign review queue stores its snapshot
    await run(
        db,
        foreign_actor,
        lambda c: queries.review_queue(c, foreign_actor, ReviewsListPendingInput(), secret=CURSOR_SECRET),
    )
    for table in PROBES:  # not vacuous: the foreign workspace has rows in every probed table
        count = seed.scalar(f"select count(*) from {table} where workspace_id = %s", (other.workspace_id,))
        assert count > 0, table
    async with db.transaction(viewer(data.workspace_id)) as conn:
        role = await fetch_all(conn, "select current_user as role")
        assert role[0]["role"] == "suv_backend"
        for table in PROBES:
            # Deliberately NO workspace predicate: only RLS stands between the tenants.
            rows = await fetch_all(conn, f"select distinct workspace_id from {table}")
            assert {r["workspace_id"] for r in rows} <= {data.workspace_id}, table
        direct = await fetch_all(
            conn, "select id from app.listings where id = %s", (other.listings["priced"],)
        )
        assert direct == []
        named = await fetch_all(
            conn, "select count(*) as n from app.valuations where workspace_id = %s", (other.workspace_id,)
        )
        assert named[0]["n"] == 0
    # Without a workspace GUC the backend role sees nothing at all.
    async with db.transaction() as conn:
        for table in PROBES:
            rows = await fetch_all(conn, f"select count(*) as n from {table}")
            assert rows[0]["n"] == 0, table


# --------------------------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------------------------


def _actor(ws: UUID, scopes: set[Scope]) -> ActorContext:
    return member(ws, Role.VIEWER, scopes=frozenset(scopes))


async def test_reads_require_their_scope(db: Database, data: SeededWorkspace, settings: Settings) -> None:
    ws = data.workspace_id
    deals_only = _actor(ws, {Scope.DEALS_READ})
    reviews_only = _actor(ws, {Scope.REVIEWS_READ})
    review_reads: list[Callable[[Conn], Awaitable[Any]]] = [
        lambda c: queries.review_queue(c, deals_only, ReviewsListPendingInput(), secret=CURSOR_SECRET),
        lambda c: queries.get_review_case(c, deals_only, data.cases["priced"]),
        lambda c: queries.outbox_attention_view(c, deals_only, OutboxQuery(), secret=CURSOR_SECRET),
    ]
    for read in review_reads:
        with pytest.raises(Forbidden):
            await run(db, deals_only, read)
    deal_reads: list[Callable[[Conn], Awaitable[Any]]] = [
        lambda c: queries.list_candidates(c, reviews_only, DealsListCandidatesInput(), secret=CURSOR_SECRET),
        lambda c: queries.get_candidate(c, reviews_only, data.listings["priced"]),
        lambda c: queries.get_valuation(c, reviews_only, data.valuations["estimated"]),
        lambda c: queries.overview_view(c, reviews_only, settings),
        lambda c: queries.sources_view(c, reviews_only),
        lambda c: queries.settings_view(c, reviews_only),
        lambda c: queries.coverage_lags_view(c, reviews_only),
    ]
    for read in deal_reads:
        with pytest.raises(Forbidden):
            await run(db, reviews_only, read)
    with pytest.raises(Forbidden):
        await queries.health_view(db, reviews_only, settings)
    # The denial happens before any work: the forbidden queue read stored no snapshot and the
    # error payload carries no data.
    with pytest.raises(Forbidden) as denied:
        await run(db, deals_only, review_reads[0])
    snapshots = await run(
        db,
        deals_only,
        lambda c: fetch_all(c, "select count(*) as n from ops.query_snapshots where workspace_id = %s", [ws]),
    )
    assert snapshots[0]["n"] == 0
    payload = ErrorPayload.from_app_error(denied.value, correlation_id="req-synthetic")
    assert payload.code == ErrorCode.FORBIDDEN and payload.details is None


async def test_viewer_reads_every_view_without_write_scopes(
    db: Database, data: SeededWorkspace, settings: Settings
) -> None:
    actor = viewer(data.workspace_id)
    assert not actor.has(Scope.REVIEWS_WRITE) and not actor.has(Scope.CONFIG_ADMIN)
    queue = await run(
        db, actor, lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(), secret=CURSOR_SECRET)
    )
    document = assert_valid(queue, tool="reviews_list_pending")
    text = str(document)
    assert "claim_token" not in text and "token_hash" not in text
    assert all(not item.claim.held_by_caller for item in queue.data.items)
    settings_view = await run(db, actor, lambda c: queries.settings_view(c, actor))
    assert settings_view.data.can_administer is False


# --------------------------------------------------------------------------------------------
# Health degradation and configuration
# --------------------------------------------------------------------------------------------


async def test_health_degrades_when_the_database_is_unreachable(settings: Settings) -> None:
    unreachable = Database("postgresql://suv:suv@127.0.0.1:1/suv_unreachable", set_role="suv_backend")
    actor = viewer(uuid.uuid4())
    result = await queries.health_view(unreachable, actor, settings)
    assert_valid(result, tool="deals_health")
    assert result.data.ready is False
    checks = {c.name: c.status for c in result.data.readiness}
    assert checks == {"database": "unavailable", "schema": "unknown", "config": "ok"}
    assert result.data.sources == () and result.data.activation_blockers == ()
    assert "127.0.0.1" not in result.envelope("req-synthetic").to_text()


async def test_health_reports_a_missing_migration(
    db: Database, data: SeededWorkspace, settings: Settings
) -> None:
    markers = (*queries.SCHEMA_MARKERS, SchemaMarker("29991231000000_future_change", "app.not_yet_created"))
    result = await queries.health_view(db, viewer(data.workspace_id), settings, markers=markers)
    assert_valid(result, tool="deals_health")
    schema = next(c for c in result.data.readiness if c.name == "schema")
    assert schema.status == "unavailable" and schema.detail is not None
    assert "29991231000000_future_change" in schema.detail
    assert result.data.ready is False and result.data.sources == ()


async def test_health_requires_a_cursor_secret(
    db: Database, data: SeededWorkspace, settings: Settings
) -> None:
    unconfigured = settings.model_copy(update={"mcp_cursor_signing_secret": None})
    result = await queries.health_view(db, viewer(data.workspace_id), unconfigured)
    config = next(c for c in result.data.readiness if c.name == "config")
    assert config.status == "not_configured" and result.data.ready is False
    with pytest.raises(AppError) as error:
        queries.cursor_secret(unconfigured.mcp_cursor_signing_secret)
    assert error.value.code == ErrorCode.INTERNAL_ERROR
    with pytest.raises(AppError):
        queries.cursor_secret(SecretStr("too-short"))
    assert queries.cursor_secret(settings.mcp_cursor_signing_secret) == CURSOR_SECRET
