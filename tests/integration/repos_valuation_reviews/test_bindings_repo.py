"""Destination bindings, notification preferences and activation routes (spec 22, ADR 0002).

SYNTHETIC identifiers only; everything runs as ``suv_backend`` under RLS.
"""

from __future__ import annotations

import asyncio
from datetime import time
from typing import Any
from uuid import UUID

import pytest
from tests.integration.repos_valuation_reviews.builders import RealWorld, owner, reviewer, run, system

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.notifications import QuietHours
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import bindings_repo
from suv_deals.persistence.bindings_repo import EventCategory, Provider
from suv_deals.persistence.database import Conn, Database

pytestmark = pytest.mark.db


async def _binding(
    db: Database, actor: ActorContext, provider: Provider = "slack", *, approve: bool = True
) -> Any:
    async def go(conn: Conn) -> Any:
        ids = (
            {"external_workspace_id": "T0SYNTH", "external_channel_id": f"C0{provider.upper()}"}
            if provider == "slack"
            else {"external_app_id": "app_SYNTHETIC"}
        )
        binding = await bindings_repo.create_binding(
            conn, actor, provider=provider, label=f"SYNTHETIC {provider} destination", **ids
        )
        if approve:
            binding = await bindings_repo.approve_binding(
                conn, actor, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
            )
            binding = await bindings_repo.set_binding_enabled(
                conn, actor, binding.id, True, expected_version=2
            )
        return binding

    return await run(db, actor, go)


async def _preferences(
    db: Database,
    actor: ActorContext,
    binding_id: UUID,
    categories: list[EventCategory],
    *,
    approve: bool = True,
) -> Any:
    async def go(conn: Conn) -> Any:
        prefs = await bindings_repo.upsert_preferences(conn, actor, binding_id, event_categories=categories)
        if approve:
            prefs = await bindings_repo.approve_preferences(
                conn, actor, prefs.id, approval_reference="SYNTHETIC owner approval", expected_version=1
            )
        return prefs

    return await run(db, actor, go)


async def _enable(db: Database, actor: ActorContext, prefs: Any) -> Any:
    return await run(
        db,
        actor,
        lambda c: bindings_repo.set_preferences_enabled(
            c, actor, prefs.id, True, expected_version=prefs.row_version
        ),
    )


async def test_bindings_are_owner_only_and_need_approval_to_enable(
    db: Database, world: RealWorld, other_world: RealWorld
) -> None:
    ws = world.workspace_id
    own = owner(ws)
    for actor in (system(ws), reviewer(ws)):
        with pytest.raises(Forbidden):
            await run(
                db,
                actor,
                lambda c, a=actor: bindings_repo.create_binding(
                    c, a, provider="mcp_events", label="SYNTHETIC"
                ),
            )
    with pytest.raises(ValidationFailed):  # Slack needs team and channel ids
        await run(
            db, own, lambda c: bindings_repo.create_binding(c, own, provider="slack", label="SYNTHETIC")
        )
    with pytest.raises(ValidationFailed):  # identifiers, never webhook URLs
        await run(
            db,
            own,
            lambda c: bindings_repo.create_binding(
                c,
                own,
                provider="slack",
                label="SYNTHETIC",
                external_workspace_id="T0SYNTH",
                external_channel_id="https://hooks.slack.example/services/SECRET",
            ),
        )
    binding = await _binding(db, own, approve=False)
    assert not binding.enabled and binding.approved_at is None
    with pytest.raises(ValidationFailed):
        await run(
            db, own, lambda c: bindings_repo.set_binding_enabled(c, own, binding.id, True, expected_version=1)
        )
    with pytest.raises(ValidationFailed):  # contact data is not an approval reference
        await run(
            db,
            own,
            lambda c: bindings_repo.approve_binding(
                c, own, binding.id, approval_reference="owner@example.com approved", expected_version=1
            ),
        )
    approved = await run(
        db,
        own,
        lambda c: bindings_repo.approve_binding(
            c, own, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        ),
    )
    assert approved.approved_by == own.principal_id and approved.row_version == 2
    with pytest.raises(VersionConflict):  # an approval record is never rewritten
        await run(
            db,
            own,
            lambda c: bindings_repo.approve_binding(
                c, own, binding.id, approval_reference="SYNTHETIC again", expected_version=2
            ),
        )
    with pytest.raises(VersionConflict):  # stale expected version
        await run(
            db, own, lambda c: bindings_repo.set_binding_enabled(c, own, binding.id, True, expected_version=1)
        )
    enabled = await run(
        db, own, lambda c: bindings_repo.set_binding_enabled(c, own, binding.id, True, expected_version=2)
    )
    assert enabled.enabled
    foreign = owner(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: bindings_repo.get_binding(c, foreign, binding.id))
    assert await run(db, foreign, lambda c: bindings_repo.list_bindings(c, foreign)) == []


async def test_one_active_route_per_category_and_provider_policy(db: Database, world: RealWorld) -> None:
    ws = world.workspace_id
    own = owner(ws)
    slack_a = await _binding(db, own)
    slack_b = await _binding(db, own)
    events = await _binding(db, own, "mcp_events")
    with pytest.raises(ValidationFailed):  # seller replies use the Slack route only (v1.1)
        await _preferences(db, own, events.id, ["seller_reply"])
    first = await _enable(db, own, await _preferences(db, own, slack_a.id, ["seller_reply"]))
    second = await _preferences(db, own, slack_b.id, ["seller_reply"])
    with pytest.raises(ValidationFailed):
        await _enable(db, own, second)
    discovery = await _enable(db, own, await _preferences(db, own, events.id, ["candidate_discovery"]))
    sys_actor = system(ws)
    route = await run(
        db, sys_actor, lambda c: bindings_repo.selected_route(c, sys_actor, "candidate_discovery")
    )
    assert route is not None and route.provider == "mcp_events" and route.preferred
    assert route.binding_id == events.id and route.preference_id == discovery.id
    reply = await run(db, sys_actor, lambda c: bindings_repo.selected_route(c, sys_actor, "seller_reply"))
    assert reply is not None and reply.binding_id == slack_a.id and reply.preference_id == first.id
    assert (
        await run(db, sys_actor, lambda c: bindings_repo.selected_route(c, sys_actor, "owner_alert")) is None
    )
    # Disabling the binding of the first route frees the category.
    await run(
        db,
        own,
        lambda c: bindings_repo.set_binding_enabled(
            c, own, slack_a.id, False, expected_version=slack_a.row_version
        ),
    )
    assert (
        await run(db, sys_actor, lambda c: bindings_repo.selected_route(c, sys_actor, "seller_reply")) is None
    )
    switched = await _enable(db, own, second)
    assert switched.enabled
    with pytest.raises(ValidationFailed):  # re-enabling the old binding would create two routes
        await run(
            db,
            own,
            lambda c: bindings_repo.set_binding_enabled(
                c, own, slack_a.id, True, expected_version=slack_a.row_version + 1
            ),
        )
    # Slack as a candidate-discovery fallback cannot be active while MCP Events is.
    widened = await run(
        db,
        own,
        lambda c: bindings_repo.upsert_preferences(
            c,
            own,
            slack_b.id,
            event_categories=["seller_reply", "candidate_discovery"],
            expected_version=switched.row_version,
        ),
    )
    assert not widened.enabled  # a new category withdraws the approval first
    reapproved = await run(
        db,
        own,
        lambda c: bindings_repo.approve_preferences(
            c,
            own,
            widened.id,
            approval_reference="SYNTHETIC wider approval",
            expected_version=widened.row_version,
        ),
    )
    with pytest.raises(ValidationFailed):
        await _enable(db, own, reapproved)
    routes = await run(db, own, lambda c: bindings_repo.active_routes(c, own))
    assert [(r.category, r.provider, r.binding_id) for r in routes] == [
        ("candidate_discovery", "mcp_events", events.id)
    ]


async def test_concurrent_enables_cannot_both_win(db: Database, world: RealWorld) -> None:
    own = owner(world.workspace_id)
    first = await _preferences(db, own, (await _binding(db, own)).id, ["owner_alert"])
    second = await _preferences(db, own, (await _binding(db, own)).id, ["owner_alert"])
    enabled = asyncio.Event()

    async def hold(conn: Conn) -> Any:
        result = await bindings_repo.set_preferences_enabled(conn, own, first.id, True, expected_version=2)
        enabled.set()
        await asyncio.sleep(0.3)
        return result

    async def compete() -> Any:
        await enabled.wait()
        return await run(
            db,
            own,
            lambda c: bindings_repo.set_preferences_enabled(c, own, second.id, True, expected_version=2),
        )

    outcomes = await asyncio.gather(run(db, own, hold), compete(), return_exceptions=True)
    assert outcomes[0].enabled  # type: ignore[union-attr]
    assert isinstance(outcomes[1], ValidationFailed)
    routes = await run(db, own, lambda c: bindings_repo.active_routes(c, own))
    assert [(r.category, r.preference_id) for r in routes] == [("owner_alert", first.id)]


async def test_adding_a_category_withdraws_the_approval(db: Database, world: RealWorld) -> None:
    own = owner(world.workspace_id)
    binding = await _binding(db, own)
    quiet = QuietHours(start=time(22, 0), end=time(7, 0))
    prefs = await _enable(db, own, await _preferences(db, own, binding.id, ["owner_alert"]))
    assert prefs.enabled and prefs.approved_by == own.principal_id
    widened = await run(
        db,
        own,
        lambda c: bindings_repo.upsert_preferences(
            c,
            own,
            binding.id,
            event_categories=["owner_alert", "candidate_discovery"],
            quiet_hours=quiet,
            expected_version=prefs.row_version,
        ),
    )
    assert not widened.enabled and widened.approved_at is None and widened.approval_reference is None
    assert widened.quiet_hours == quiet and not widened.quiet_hours.approved  # PROPOSED, never applied
    with pytest.raises(ValidationFailed):
        await _enable(db, own, widened)
    reapproved = await run(
        db,
        own,
        lambda c: bindings_repo.approve_preferences(
            c,
            own,
            widened.id,
            approval_reference="SYNTHETIC wider approval",
            expected_version=widened.row_version,
        ),
    )
    active = await _enable(db, own, reapproved)
    narrowed = await run(
        db,
        own,
        lambda c: bindings_repo.upsert_preferences(
            c, own, binding.id, event_categories=["owner_alert"], expected_version=active.row_version
        ),
    )
    assert narrowed.enabled and narrowed.approved_at is not None  # narrowing keeps the approval
    loaded = await run(db, own, lambda c: bindings_repo.get_preferences(c, own, binding.id))
    assert loaded == narrowed
    with pytest.raises(VersionConflict):
        await run(
            db,
            own,
            lambda c: bindings_repo.approve_preferences(
                c, own, narrowed.id, approval_reference="SYNTHETIC", expected_version=narrowed.row_version
            ),
        )


async def test_binding_enable_and_preference_enable_race_cannot_create_two_routes(
    db: Database, world: RealWorld
) -> None:
    """The two activation paths (enable a binding / enable preferences) serialize on the
    workspace's preference rows, so concurrent changes from both sides never both win."""
    own = owner(world.workspace_id)
    active_binding = await _binding(db, own)  # enabled
    dormant = await _binding(db, own)  # enabled, then disabled below
    dormant = await run(
        db,
        own,
        lambda c: bindings_repo.set_binding_enabled(c, own, dormant.id, False, expected_version=3),
    )
    # Preferences of the DISABLED binding may be enabled: they are not an active route yet.
    await _enable(db, own, await _preferences(db, own, dormant.id, ["owner_alert"]))
    waiting = await _preferences(db, own, active_binding.id, ["owner_alert"])
    enabled = asyncio.Event()

    async def enable_binding(conn: Conn) -> Any:
        result = await bindings_repo.set_binding_enabled(conn, own, dormant.id, True, expected_version=4)
        enabled.set()
        await asyncio.sleep(0.3)  # keep the preference locks while the competitor arrives
        return result

    async def enable_preferences() -> Any:
        await enabled.wait()
        return await _enable(db, own, waiting)

    outcomes = await asyncio.gather(
        run(db, own, enable_binding), enable_preferences(), return_exceptions=True
    )
    assert outcomes[0].enabled  # type: ignore[union-attr]
    assert isinstance(outcomes[1], ValidationFailed)
    routes = await run(db, own, lambda c: bindings_repo.active_routes(c, own))
    assert [(r.category, r.binding_id) for r in routes] == [("owner_alert", dormant.id)]
