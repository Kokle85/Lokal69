"""Source registry: YAML sync gate, pause idempotency, access blocks, technical status (spec 5, 9, 21, 25)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.persistence_core.support import member
from tests.integration.repos_sources_listings.support import HOST, T0, Env, source_config

from suv_deals.adapters.base import FetchOutcome, ParserHealth
from suv_deals.domain.enums import (
    AccessState,
    GateStatus,
    Role,
    Scope,
    TechnicalStatus,
    TermsDecision,
    TermsStatus,
)
from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, SourcePaused, VersionConflict
from suv_deals.persistence import sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import SourceRoute
from suv_deals.persistence.transactions import lock_source, unit_of_work
from suv_deals.views.operations import SourceRunState

pytestmark = pytest.mark.db


def _source(seed: Seed, source_id: Any) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select enabled, technical_status, terms_status, terms_decision, paused, pause_reason, version,"
        " config from app.sources where id = %s",
        (source_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def _sync(db: Database, env: Env, *configs: Any) -> sources_repo.SourceSyncReport:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.sync_sources_from_yaml(conn, env.system, list(configs))


async def _id(db: Database, env: Env, key: str) -> uuid.UUID:
    async with unit_of_work(db, env.system) as conn:
        return (await sources_repo.get_source_by_key(conn, env.system, key)).id


# --------------------------------------------------------------------------------------------
# YAML sync
# --------------------------------------------------------------------------------------------


async def test_sync_never_enables_unapproved_or_unimplemented_sources(
    db: Database, env: Env, seed: Seed
) -> None:
    pending, unimplemented, blocked = (unique(p).lower() for p in ("pending", "unimpl", "blocked"))
    # SourceConfig itself refuses enabled=True with problems, so build "enabled" requests that
    # only become problematic through the stored runtime state or by bypassing the model guard.
    pending_cfg = source_config(pending).model_copy(
        update={"terms_decision": TermsDecision.PENDING, "terms_decision_actor": None}
    )
    unimpl_cfg = source_config(unimplemented).model_copy(update={"adapter_version": "unimplemented"})
    report = await _sync(
        db, env, source_config(env.source_key), pending_cfg, unimpl_cfg, source_config(blocked)
    )
    assert set(report.created) == {pending, unimplemented, blocked}
    assert pending in report.not_enabled and unimplemented in report.not_enabled
    assert any("terms decision is pending" in p for p in report.not_enabled[pending])
    assert _source(seed, await _id(db, env, pending))["enabled"] is False
    assert _source(seed, await _id(db, env, unimplemented))["enabled"] is False
    blocked_id = await _id(db, env, blocked)
    assert _source(seed, blocked_id)["enabled"] is True
    # A runtime access block survives a later sync that still says "fixture_tested, enabled".
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.record_access_block(
            conn, env.system, blocked_id, SourceRoute(host=HOST, purpose="search"), "HTTP 403"
        )
    again = await _sync(
        db, env, source_config(env.source_key), pending_cfg, unimpl_cfg, source_config(blocked)
    )
    assert again.preserved_runtime_status == {blocked: TechnicalStatus.ACCESS_BLOCKED}
    row = _source(seed, blocked_id)
    assert row["enabled"] is False and row["technical_status"] == "access_blocked"


async def test_sync_keeps_terms_and_technical_status_separate_and_audits(
    db: Database, env: Env, seed: Seed
) -> None:
    key = unique("terms").lower()
    await _sync(db, env, source_config(env.source_key), source_config(key))
    source_id = await _id(db, env, key)
    before = _source(seed, source_id)
    restricted = source_config(
        key,
        enabled=False,
        terms_status=TermsStatus.RESTRICTED,
        terms_decision=TermsDecision.DO_NOT_USE,
        terms_decision_note="synthetic: owner decided not to use",
    )
    report = await _sync(db, env, source_config(env.source_key), restricted)
    assert report.updated == (key,) and report.terms_changed == (key,)
    row = _source(seed, source_id)
    assert (row["terms_status"], row["terms_decision"]) == ("restricted", "do_not_use")
    assert row["technical_status"] == "fixture_tested" and row["enabled"] is False
    assert row["version"] == before["version"] + 1
    audit = seed.conn.execute(
        "select prior_version, new_version, metadata from ops.audit_events"
        " where target_id = %s and action = 'source.sync_update'",
        (source_id,),
    ).fetchone()
    assert audit is not None and (audit[0], audit[1]) == (before["version"], before["version"] + 1)
    assert "terms_decision" in audit[2]["changed_fields"]
    # An identical sync changes nothing and writes no audit row.
    unchanged = await _sync(db, env, source_config(env.source_key), restricted)
    assert key in unchanged.unchanged and _source(seed, source_id)["version"] == row["version"]


async def test_sync_disables_sources_missing_from_yaml(db: Database, env: Env, seed: Seed) -> None:
    key = unique("gone").lower()
    await _sync(db, env, source_config(env.source_key), source_config(key))
    report = await _sync(db, env, source_config(env.source_key))
    assert report.disabled_missing == (key,)
    row = _source(seed, await _id(db, env, key))
    assert row["enabled"] is False  # never deleted


async def test_sync_requires_system_or_owner(db: Database, env: Env) -> None:
    reviewer = member(env.workspace_id, Role.REVIEWER)
    async with unit_of_work(db, reviewer) as conn:
        with pytest.raises(Forbidden):
            await sources_repo.sync_sources_from_yaml(conn, reviewer, [source_config(env.source_key)])


async def test_source_views(db: Database, env: Env) -> None:
    viewer = member(env.workspace_id, Role.VIEWER)
    async with unit_of_work(db, viewer) as conn:
        view = await sources_repo.get_source(conn, viewer, env.source_id)
        listing = await sources_repo.list_sources(conn, viewer)
    assert view.source_key == env.source_key and view.enabled
    assert view.state == SourceRunState.NOT_SCANNED
    assert view.terms.decision == TermsDecision.PROCEED_ACKNOWLEDGED
    assert view.technical.status == TechnicalStatus.FIXTURE_TESTED
    assert [s.source_id for s in listing.items] == [env.source_id]


# --------------------------------------------------------------------------------------------
# Pause (sources_pause tool)
# --------------------------------------------------------------------------------------------


async def test_pause_is_idempotent_and_version_checked(db: Database, env: Env, seed: Seed) -> None:
    owner = env.owner
    version = _source(seed, env.source_id)["version"]
    key = f"pause-{uuid.uuid4().hex[:12]}"
    async with unit_of_work(db, owner) as conn:
        first = await sources_repo.pause_source(
            conn, owner, env.source_id, version, "synthetic pause test", key
        )
    assert first.already_paused is False and first.version == version + 1
    async with unit_of_work(db, owner) as conn:
        replay = await sources_repo.pause_source(
            conn, owner, env.source_id, version, "synthetic pause test", key
        )
    assert replay == first  # same key + same request -> original result, nothing changes
    row = _source(seed, env.source_id)
    assert row["paused"] and row["enabled"] and row["version"] == version + 1  # pause never disables
    async with unit_of_work(db, owner) as conn:
        with pytest.raises(IdempotencyConflict):
            await sources_repo.pause_source(conn, owner, env.source_id, version, "another reason here", key)
    async with unit_of_work(db, owner) as conn:
        with pytest.raises(VersionConflict) as stale:
            await sources_repo.pause_source(
                conn, owner, env.source_id, version, "stale version", "pause-stale-1"
            )
    assert stale.value.details == {"current_version": version + 1}
    async with unit_of_work(db, owner) as conn:
        again = await sources_repo.pause_source(
            conn, owner, env.source_id, version + 1, "already paused", "pause-again-1"
        )
    assert again.already_paused and again.version == version + 1
    # A paused source refuses new network work but keeps its enabled flag (no implicit resume).
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(SourcePaused):
            await lock_source(conn, env.workspace_id, env.source_id, for_network=True)
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'source.pause'",
            (env.source_id,),
        )
        == 1
    )


async def test_pause_scope_and_workspace_isolation(db: Database, env: Env, env_b: Env, seed: Seed) -> None:
    reviewer = member(env.workspace_id, Role.REVIEWER)
    async with unit_of_work(db, reviewer) as conn:
        with pytest.raises(Forbidden):
            await sources_repo.pause_source(conn, reviewer, env.source_id, 1, "not allowed", "pause-rev-1")
    # Owner of workspace B cannot see (or pause) a source of workspace A: indistinguishable from missing.
    async with unit_of_work(db, env_b.owner) as conn:
        with pytest.raises(NotFound):
            await sources_repo.pause_source(
                conn, env_b.owner, env.source_id, 1, "foreign pause", "pause-foreign-1"
            )
    assert _source(seed, env.source_id)["paused"] is False


async def test_resume_is_a_separate_owner_action(db: Database, env: Env, seed: Seed) -> None:
    version = _source(seed, env.source_id)["version"]
    async with unit_of_work(db, env.owner) as conn:
        paused = await sources_repo.pause_source(
            conn, env.owner, env.source_id, version, "pause first", "pause-r-1"
        )
    scoped = member(env.workspace_id, Role.OWNER, scopes=frozenset({Scope.SOURCES_PAUSE, Scope.DEALS_READ}))
    async with unit_of_work(db, scoped) as conn:
        with pytest.raises(Forbidden):
            await sources_repo.resume_source(
                conn, scoped, env.source_id, paused.version, reason="no admin scope"
            )
    async with unit_of_work(db, env.owner) as conn:
        resumed = await sources_repo.resume_source(
            conn, env.owner, env.source_id, paused.version, reason="resume"
        )
    assert not resumed.paused and resumed.version == paused.version + 1


# --------------------------------------------------------------------------------------------
# Access blocks, technical status, robots, fetch attempts
# --------------------------------------------------------------------------------------------


async def test_access_block_pauses_route_and_creates_exactly_one_review_item(
    db: Database, env: Env, seed: Seed
) -> None:
    route = SourceRoute(host=HOST, purpose="detail", path="/vehicles/x?token=abc")
    async with unit_of_work(db, env.system) as conn:
        first = await sources_repo.record_access_block(
            conn, env.system, env.source_id, route, {"http_status": 403, "marker": "captcha"}
        )
    assert first.created and not first.already_blocked
    row = _source(seed, env.source_id)
    assert row["technical_status"] == "access_blocked" and row["enabled"] is False
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(SourcePaused):
            await lock_source(conn, env.workspace_id, env.source_id, for_network=True)
    for _ in range(2):
        async with unit_of_work(db, env.system) as conn:
            repeat = await sources_repo.record_access_block(
                conn, env.system, env.source_id, route, "HTTP 403"
            )
        assert not repeat.created and repeat.already_blocked and repeat.review_item_id == first.review_item_id
    gates = seed.conn.execute(
        "select status, evidence from ops.activation_gates where workspace_id = %s and capability = %s",
        (env.workspace_id, sources_repo.access_gate_capability(env.source_key)),
    ).fetchall()
    assert len(gates) == 1 and gates[0][0] == GateStatus.BLOCKED.value
    assert "token=abc" not in str(gates[0][1])  # evidence and route labels are redacted
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'source.access_blocked'",
            (env.source_id,),
        )
        == 1
    )
    async with unit_of_work(db, env.owner) as conn:
        assert await sources_repo.alert_pause_reason(conn, env.owner, env.source_id) == "access_blocked"


async def test_technical_status_transitions(db: Database, env: Env, seed: Seed) -> None:
    health = ParserHealth(
        status="unhealthy",
        sample_size=40,
        reasons=("price coverage fell",),
        recommended_actions=("pause_new_alerts",),
    )
    async with unit_of_work(db, env.system) as conn:
        record = await sources_repo.set_technical_status(
            conn,
            env.system,
            env.source_id,
            TechnicalStatus.PARSER_UNHEALTHY,
            reason="tripwire",
            parser_health=health,
        )
        assert record.technical_status == TechnicalStatus.PARSER_UNHEALTHY and not record.enabled
        assert await sources_repo.alert_pause_reason(conn, env.system, env.source_id) == "parser_unhealthy"
        view = await sources_repo.get_source(conn, env.system, env.source_id)
    assert (
        view.technical.parser_health.status == "unhealthy" and view.technical.parser_health.sample_size == 40
    )
    assert view.state == SourceRunState.PARSER_UNHEALTHY
    # Leaving a blocking status needs the owner (config:admin); a system check may not clear it.
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(Forbidden):
            await sources_repo.set_technical_status(
                conn, env.system, env.source_id, TechnicalStatus.FIXTURE_TESTED, reason="auto clear"
            )
    async with unit_of_work(db, env.owner) as conn:
        cleared = await sources_repo.set_technical_status(
            conn,
            env.owner,
            env.source_id,
            TechnicalStatus.LIVE_SMOKE_PASSED,
            reason="repaired parser, smoke ok",
        )
    assert cleared.technical_status == TechnicalStatus.LIVE_SMOKE_PASSED
    assert cleared.last_live_smoke_at is not None and cleared.enabled is False  # never re-enabled implicitly
    assert _source(seed, env.source_id)["version"] == record.version + 1


async def test_robots_revision_change_flags_sources(db: Database, env: Env, seed: Seed) -> None:
    async with unit_of_work(db, env.system) as conn:
        first = await sources_repo.record_robots_revision(
            conn,
            env.system,
            host=HOST.upper(),
            fetched_at=T0,
            http_status=200,
            body="User-agent: *\nAllow: /\n",
            parse_ok=True,
        )
        same = await sources_repo.record_robots_revision(
            conn,
            env.system,
            host=HOST,
            fetched_at=T0 + timedelta(hours=1),
            http_status=200,
            body="User-agent: *\nAllow: /\n",
            parse_ok=True,
        )
        changed = await sources_repo.record_robots_revision(
            conn,
            env.system,
            host=HOST,
            fetched_at=T0 + timedelta(hours=2),
            http_status=200,
            body="User-agent: *\nDisallow: /vehicles/\n",
            parse_ok=True,
        )
    assert first.host == HOST and not first.changed and not same.changed
    assert changed.changed and changed.affected_source_ids == (env.source_id,)
    assert changed.previous_hash == sha("User-agent: *\nAllow: /\n")
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where target_id = %s and action = 'source.robots_changed'",
            (env.source_id,),
        )
        == 1
    )


async def test_fetch_attempt_is_redacted(db: Database, env: Env, seed: Seed) -> None:
    url = f"https://{HOST}/vehicles/1?session=secret-value"
    outcome = FetchOutcome(
        requested_url=url,
        final_url=f"https://{HOST}/vehicles/1",
        http_status=403,
        success=False,
        access_state=AccessState.ACCESS_BLOCKED,
        error_code="captcha challenge!",
        response_headers={"Content-Type": "text/html", "Set-Cookie": "sid=secret", "Retry-After": "120"},
        fetched_at=T0,
    )
    async with unit_of_work(db, env.system) as conn:
        attempt_id = await sources_repo.record_fetch_attempt(
            conn, env.system, source_id=env.source_id, purpose="detail", outcome=outcome
        )
    row = seed.conn.execute(
        "select url_hash, final_url_hash, host, success, access_state, error_code, response_headers"
        " from ops.fetch_attempts where id = %s",
        (attempt_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == sha(url) and row[2] == HOST and row[3] is False and row[4] == "access_blocked"
    assert row[5] == "captcha_challenge_"
    assert row[6] == {"content-type": "text/html", "retry-after": "120"}  # allow-listed headers only
    assert "secret" not in str(row)


async def test_suspected_parser_drift_pauses_new_alerts(db: Database, env: Env) -> None:
    """Spec 25: on suspected drift (``degraded``) new opportunity alerts pause, as for an unhealthy
    parser; network work and prior evidence are untouched."""
    health = ParserHealth(
        status="degraded",
        sample_size=40,
        reasons=("mileage coverage fell",),
        recommended_actions=("pause_new_alerts",),
    )
    async with unit_of_work(db, env.system) as conn:
        assert await sources_repo.alert_pause_reason(conn, env.system, env.source_id) is None
        record = await sources_repo.set_technical_status(
            conn,
            env.system,
            env.source_id,
            TechnicalStatus.DEGRADED,
            reason="tripwire",
            parser_health=health,
        )
        assert record.technical_status == TechnicalStatus.DEGRADED
        assert await sources_repo.alert_pause_reason(conn, env.system, env.source_id) == "parser_degraded"
