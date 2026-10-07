"""Configuration revisions, search profiles, workspaces and memberships (spec 3, 11, 12, 26; ADR 0001)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import member
from tests.integration.repos_sources_listings.support import Env, business_config

from suv_deals.domain.enums import ProfileKey, Role
from suv_deals.domain.profiles import BusinessConfig
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import config_repo, workspaces
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


@pytest.fixture
async def admin_db(db_url: str) -> AsyncIterator[Database]:
    """A privileged (migration-owner) connection, as setup scripts use for workspace bootstrap."""
    database = Database(db_url, min_size=1, max_size=2)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def _with_threshold(config: BusinessConfig, amount: str) -> BusinessConfig:
    data: dict[str, Any] = config.model_dump()
    data["contribution_threshold"] = {**data["contribution_threshold"], "amount_eur": amount}
    return BusinessConfig.model_validate(data)


# --------------------------------------------------------------------------------------------
# Configuration revisions
# --------------------------------------------------------------------------------------------


async def test_revisions_are_monotonic_immutable_and_audited(db: Database, env: Env, seed: Seed) -> None:
    async with unit_of_work(db, env.owner) as conn:
        record, current = await config_repo.current_config(conn, env.owner)
        assert record.revision == 1 and current == business_config()
        same = await config_repo.record_config_revision(conn, env.owner, current, "no change at all", current)
        assert not same.created and same.revision.revision == 1
        changed = _with_threshold(current, "1600")
        second = await config_repo.record_config_revision(
            conn, env.owner, changed, "raise threshold", current
        )
    assert second.created and second.revision.revision == 2
    assert second.revision.before == current.model_dump(mode="json")
    assert second.revision.author_principal_id == env.owner_user_id and second.revision.author_kind == "user"
    assert {p.config_revision_id for p in second.profiles} == {second.revision.id}
    # The edited configuration must be the current one (optimistic concurrency on the content).
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(VersionConflict):
            await config_repo.record_config_revision(
                conn, env.owner, _with_threshold(current, "1700"), "stale", current
            )
    with pytest.raises(Exception) as history:
        seed.conn.execute(
            "update app.config_revisions set reason = 'rewrite' where id = %s", (second.revision.id,)
        )
    assert getattr(history.value, "sqlstate", None) == "SV001"
    audit = seed.conn.execute(
        "select prior_version, new_version from ops.audit_events"
        " where target_id = %s and action = 'config.revision'",
        (second.revision.id,),
    ).fetchone()
    assert audit == (1, 2)


async def test_concurrent_revision_writers_one_wins(db: Database, env: Env) -> None:
    async with unit_of_work(db, env.owner) as conn:
        _, current = await config_repo.current_config(conn, env.owner)

    async def write(amount: str) -> Any:
        async with unit_of_work(db, env.owner) as conn:
            return await config_repo.record_config_revision(
                conn, env.owner, _with_threshold(current, amount), f"set {amount}", current
            )

    results = await asyncio.gather(write("1800"), write("1900"), return_exceptions=True)
    assert sum(1 for r in results if isinstance(r, VersionConflict)) == 1
    async with unit_of_work(db, env.owner) as conn:
        revisions = await config_repo.list_config_revisions(conn, env.owner)
    assert [r.revision for r in revisions] == [2, 1]


async def test_enabling_manual_profile_is_a_new_revision_with_its_own_queue(
    db: Database, env: Env, seed: Seed
) -> None:
    async with unit_of_work(db, env.owner) as conn:
        result = await config_repo.set_profile_enabled(
            conn,
            env.owner,
            ProfileKey.MANUAL_4000,
            True,
            expected_revision=1,
            reason="owner opts into EUR 4,000",
        )
        profiles = {p.profile_key: p for p in await config_repo.list_profiles(conn, env.owner)}
    assert (
        result.created and result.revision.revision == 2 and result.enabled_changes == {"manual_4000": True}
    )
    manual, primary = profiles[ProfileKey.MANUAL_4000], profiles[ProfileKey.PRIMARY]
    assert manual.enabled and str(manual.max_price_eur) == "4000.00"
    assert manual.queue_label != primary.queue_label and manual.config_revision_id == result.revision.id
    assert (
        seed.scalar(
            "select metadata->'profile_enabled_changes'->>'manual_4000' from ops.audit_events"
            " where target_id = %s and action = 'config.revision'",
            (result.revision.id,),
        )
        == "true"
    )
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(VersionConflict):
            await config_repo.set_profile_enabled(
                conn, env.owner, ProfileKey.BELOW_TARGET_WATCH, True, expected_revision=1, reason="stale view"
            )
        with pytest.raises(ValidationFailed):
            await config_repo.set_profile_enabled(
                conn, env.owner, ProfileKey.PRIMARY, False, expected_revision=2, reason="never allowed"
            )
        unchanged = await config_repo.set_profile_enabled(
            conn, env.owner, ProfileKey.MANUAL_4000, True, expected_revision=2, reason="already enabled"
        )
    assert not unchanged.created and unchanged.revision.revision == 2


async def test_config_writes_need_config_admin(db: Database, env: Env) -> None:
    reviewer = member(env.workspace_id, Role.REVIEWER)
    async with unit_of_work(db, reviewer) as conn:
        _, current = await config_repo.current_config(conn, reviewer)  # deals:read suffices
        with pytest.raises(Forbidden):
            await config_repo.record_config_revision(
                conn, reviewer, _with_threshold(current, "1550"), "nope", current
            )
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(Forbidden):
            await config_repo.set_profile_enabled(
                conn, env.system, ProfileKey.MANUAL_4000, True, expected_revision=1, reason="system may not"
            )


async def test_config_is_workspace_scoped(db: Database, env: Env, env_b: Env) -> None:
    async with unit_of_work(db, env_b.owner) as conn:
        with pytest.raises(NotFound):
            await config_repo.get_config_revision(conn, env_b.owner, env.config_revision_id)


# --------------------------------------------------------------------------------------------
# Workspaces and memberships
# --------------------------------------------------------------------------------------------


async def test_membership_bootstrap_sees_only_own_active_memberships(
    db: Database, env: Env, env_b: Env, seed: Seed
) -> None:
    user = seed.user()
    seed.membership(env.workspace_id, user, "reviewer")
    seed.membership(env_b.workspace_id, user, "viewer", active=False)
    found = await workspaces.resolve_memberships_for_user(db, user)
    assert [(m.workspace_id, m.role) for m in found] == [(env.workspace_id, Role.REVIEWER)]
    assert await workspaces.resolve_memberships_for_user(db, uuid.uuid4()) == []
    # Inside workspace B's transaction the membership of A is invisible.
    async with unit_of_work(db, env_b.owner) as conn:
        assert await workspaces.get_membership(conn, env.workspace_id, user) is None
        inactive = await workspaces.get_membership(conn, env_b.workspace_id, user)
    assert inactive is not None and not inactive.active


async def test_owner_manages_memberships_and_last_owner_is_protected(
    db: Database, env: Env, seed: Seed
) -> None:
    user = seed.user()
    async with unit_of_work(db, env.owner) as conn:
        added = await workspaces.add_membership(
            conn, env.owner, user, Role.REVIEWER, reason="synthetic reviewer"
        )
        promoted = await workspaces.add_membership(conn, env.owner, user, Role.OWNER, reason="second owner")
        deactivated = await workspaces.deactivate_membership(conn, env.owner, user, reason="leaves the team")
    assert added.role == Role.REVIEWER and promoted.role == Role.OWNER and not deactivated.active
    actions = [
        r[0]
        for r in seed.conn.execute(
            "select action from ops.audit_events where target_id = %s and target_type = 'membership'"
            " order by occurred_at, id",
            (user,),
        ).fetchall()
    ]
    # One transaction: identical timestamps, so compare as a set of distinct audited actions.
    assert sorted(actions) == ["membership.add", "membership.deactivate", "membership.update"]
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(VersionConflict):
            await workspaces.deactivate_membership(conn, env.owner, env.owner_user_id, reason="last owner")
        with pytest.raises(VersionConflict):
            await workspaces.add_membership(
                conn, env.owner, env.owner_user_id, Role.VIEWER, reason="demote last"
            )
        with pytest.raises(NotFound):
            await workspaces.deactivate_membership(conn, env.owner, uuid.uuid4(), reason="unknown user")
    with pytest.raises(NotFound):  # FK to auth.users: the failed statement aborts its transaction
        async with unit_of_work(db, env.owner) as conn:
            await workspaces.add_membership(conn, env.owner, uuid.uuid4(), Role.VIEWER, reason="no auth user")


async def test_membership_admin_requires_signed_in_owner(db: Database, env: Env, seed: Seed) -> None:
    user = seed.user()
    for actor in (
        member(env.workspace_id, Role.REVIEWER),
        member(env.workspace_id, Role.OWNER, kind="mcp_client"),
        env.system,
    ):
        async with unit_of_work(db, actor) as conn:
            with pytest.raises(Forbidden):
                await workspaces.add_membership(conn, actor, user, Role.VIEWER, reason="not permitted")


async def test_create_workspace_bootstrap(db: Database, admin_db: Database, seed: Seed) -> None:
    user = seed.user()
    name = unique("Synthetic bootstrap")
    async with admin_db.transaction() as conn:
        created = await workspaces.create_workspace(
            conn, name=name, owner_user_id=user, request_id="setup-test"
        )
    assert (
        created.owner.role == Role.OWNER
        and created.owner.active
        and created.display_timezone == "Europe/Skopje"
    )
    assert [m.workspace_id for m in await workspaces.resolve_memberships_for_user(db, user)] == [
        created.workspace_id
    ]
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'workspace.create'",
            (created.workspace_id,),
        )
        == 1
    )
    # The restricted backend role cannot provision workspaces.
    async with db.transaction(user_id=user) as conn:
        with pytest.raises(Forbidden):
            await workspaces.create_workspace(conn, name="Not allowed", owner_user_id=user, request_id="x")
    with pytest.raises(ValidationFailed):
        async with admin_db.transaction() as conn:
            await workspaces.create_workspace(conn, name="  ", owner_user_id=user, request_id="x")
