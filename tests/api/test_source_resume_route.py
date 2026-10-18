"""The owner's source resume: ``POST /api/sources/{source_id}/resume`` (marker ``db``).

The dashboard's per-source on/off switch is pause (``sources:pause``) and this resume
(``config:admin``, owner only, never an MCP tool). A resume clears the pause and requeues the
source's ``source_paused`` jobs in the same transaction; it never enables a source and never
clears ``access_blocked``. Real ASGI app, real PostgreSQL (16 and 17), locally signed
Supabase-shaped tokens, SYNTHETIC data only.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from tests.api.conftest import DataHarness, error_of
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import seed_foreign_workspace

from suv_deals.api import auth as api_auth
from suv_deals.api.schemas import ROUTES, V11_ROUTE_INDEX
from suv_deals.domain.enums import JobType, Role, Scope
from suv_deals.mcp.schemas import CALC_TOOLS, TOOLS, V11_TOOLS
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

KEY = "POST /api/sources/{source_id}/resume"
SNAPSHOT = Path(__file__).resolve().parents[2] / "schemas/api/sources.source_id.resume.post.json"


@cache
def _response_validator() -> Draft202012Validator:
    schema = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["response"]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(response: httpx.Response) -> dict[str, Any]:
    """A 200 enveloped result that matches the COMMITTED snapshot schema."""
    assert response.status_code == 200, (response.status_code, response.text[:800])
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    errors = sorted(_response_validator().iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    assert body["request_id"] == response.headers["x-request-id"]
    return body


async def source_view(api: DataHarness, label: str) -> dict[str, Any]:
    items = (await api.get("/api/sources", api.users.viewer)).json()["data"]["items"]
    found: dict[str, Any] = next(s for s in items if s["source_id"] == str(api.data.sources[label]))
    return found


def resume_path(api: DataHarness, label: str) -> str:
    return f"/api/sources/{api.data.sources[label]}/resume"


def body(version: int, key: str, reason: str = "SYNTHETIC: maintenance finished") -> dict[str, Any]:
    return {"expected_version": version, "reason": reason, "idempotency_key": key}


def blocked_job(seed: Seed, ws: UUID, source_id: UUID | None, blocker: str = "source_paused") -> UUID:
    return seed.job(
        ws,
        job_type=JobType.DISCOVERY.value,
        state="blocked",
        blocker_code=blocker,
        blocker_detail=f"SYNTHETIC {blocker}",
        attempts=1,
        source_id=source_id,
    )


def job_state(seed: Seed, job_id: UUID) -> tuple[str, str | None]:
    row = seed.conn.execute("select state, blocker_code from ops.jobs where id = %s", (job_id,)).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def source_row(seed: Seed, source_id: UUID) -> tuple[bool, bool, str, int]:
    row = seed.conn.execute(
        "select enabled, paused, technical_status, version from app.sources where id = %s", (source_id,)
    ).fetchone()
    assert row is not None
    return bool(row[0]), bool(row[1]), str(row[2]), int(row[3])


async def test_owner_resumes_a_paused_source_and_its_waiting_jobs_are_requeued(
    data_api: DataHarness, seed: Seed
) -> None:
    ws, owner = data_api.workspace_id, data_api.users.owner
    paused, running = data_api.data.sources["paused"], data_api.data.sources["running"]
    view = await source_view(data_api, "paused")
    assert view["paused"] is True and view["enabled"] is True and view["network_allowed"] is False
    mine = [blocked_job(seed, ws, paused) for _ in range(2)]
    other_blocker = blocked_job(seed, ws, paused, "incompatible_payload_version")
    other_source = blocked_job(seed, ws, running)

    response = await data_api.post(
        resume_path(data_api, "paused"),
        owner,
        body(view["version"], "resume-flow-00001"),
        headers={"Idempotency-Key": "resume-flow-00001"},
    )
    envelope = valid(response)
    data = envelope["data"]
    assert data["source_id"] == str(paused) and data["source_key"] == view["source_key"]
    assert data["paused"] is False and data["was_paused"] is True
    assert data["requeued_jobs"] == 2 and data["version"] == view["version"] + 1
    assert data["enabled"] is True and data["network_allowed"] is True
    assert data["technical_status"] == view["technical"]["status"]
    assert "never enables a source" in data["notice"]
    assert envelope["warnings"] == []  # nothing else keeps this source off
    assert [job_state(seed, job) for job in mine] == [("queued", None), ("queued", None)]
    assert job_state(seed, other_blocker) == ("blocked", "incompatible_payload_version")
    assert job_state(seed, other_source) == ("blocked", "source_paused")

    after = await source_view(data_api, "paused")
    assert after["paused"] is False and after["pause_reason"] is None and after["paused_at"] is None
    assert after["network_allowed"] is True and after["state"] != "paused"
    assert after["version"] == view["version"] + 1
    audit = seed.conn.execute(
        "select actor_principal_id, actor_kind, reason, metadata from ops.audit_events"
        " where workspace_id = %s and action = 'source.resume'",
        (ws,),
    ).fetchall()
    assert len(audit) == 1
    principal, kind, reason, metadata = audit[0]
    assert principal == owner and kind == "user" and reason == "SYNTHETIC: maintenance finished"
    assert metadata["requeued_jobs"] == 2


async def test_resume_of_a_source_that_is_not_paused_is_a_no_op(data_api: DataHarness, seed: Seed) -> None:
    ws, owner = data_api.workspace_id, data_api.users.owner
    running = data_api.data.sources["running"]
    view = await source_view(data_api, "running")
    gated = blocked_job(seed, ws, running)
    data = valid(
        await data_api.post(
            resume_path(data_api, "running"), owner, body(view["version"], "resume-noop-0001")
        )
    )["data"]
    assert data["was_paused"] is False and data["requeued_jobs"] == 0 and data["paused"] is False
    assert data["version"] == view["version"]
    assert job_state(seed, gated) == ("blocked", "source_paused")
    assert source_row(seed, running)[3] == view["version"]
    assert (
        seed.scalar(
            "select count(*) from ops.audit_events where workspace_id = %s and action = 'source.resume'",
            (ws,),
        )
        == 0
    )


async def test_stale_version_is_a_conflict_and_changes_nothing(data_api: DataHarness, seed: Seed) -> None:
    owner = data_api.users.owner
    view = await source_view(data_api, "paused")
    stale = await data_api.post(
        resume_path(data_api, "paused"), owner, body(view["version"] + 1, "resume-stale-0001")
    )
    assert stale.status_code == 409
    error = error_of(stale)
    assert error["code"] == "VERSION_CONFLICT" and error["details"]["current_version"] == view["version"]
    assert source_row(seed, data_api.data.sources["paused"])[1] is True
    # The refused attempt left no idempotency record: the same key works with the current version.
    retry = await data_api.post(
        resume_path(data_api, "paused"), owner, body(view["version"], "resume-stale-0001")
    )
    assert valid(retry)["data"]["was_paused"] is True


async def test_sources_pause_alone_cannot_resume(
    data_api: DataHarness, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = data_api.data.sources["paused"]
    view = await source_view(data_api, "paused")
    for user in (data_api.users.reviewer, data_api.users.viewer):
        refused = await data_api.post(
            resume_path(data_api, "paused"), user, body(view["version"], "resume-role-0001")
        )
        assert refused.status_code == 403 and error_of(refused)["code"] == "FORBIDDEN"
    # A session that may pause sources but holds no config:admin (the owner role narrowed).
    narrowed = frozenset({Scope.DEALS_READ, Scope.SOURCES_PAUSE})
    original = api_auth.dashboard_scopes
    monkeypatch.setattr(
        api_auth, "dashboard_scopes", lambda role: narrowed if Role(role) == Role.OWNER else original(role)
    )
    me = (await data_api.get("/api/me", data_api.users.owner)).json()["data"]
    assert set(me["scopes"]) == {"deals:read", "sources:pause"}
    refused = await data_api.post(
        resume_path(data_api, "paused"), data_api.users.owner, body(view["version"], "resume-scope-0001")
    )
    assert refused.status_code == 403 and error_of(refused)["code"] == "FORBIDDEN"
    assert source_row(seed, paused)[1] is True
    # The same session can still pause (the switch's off direction).
    running = await source_view(data_api, "running")
    paused_now = await data_api.post(
        f"/api/sources/{running['source_id']}/pause",
        data_api.users.owner,
        {
            "expected_version": running["version"],
            "reason": "SYNTHETIC pause",
            "idempotency_key": "pause-scope-0001",
        },
    )
    assert paused_now.status_code == 200, paused_now.text


async def test_idempotent_replay_header_check_and_key_reuse(data_api: DataHarness, seed: Seed) -> None:
    owner = data_api.users.owner
    paused = data_api.data.sources["paused"]
    view = await source_view(data_api, "paused")
    first_job = blocked_job(seed, data_api.workspace_id, paused)
    request = body(view["version"], "resume-replay-001")
    mismatch = await data_api.post(
        resume_path(data_api, "paused"), owner, request, headers={"Idempotency-Key": "resume-other-0001"}
    )
    assert mismatch.status_code == 422 and error_of(mismatch)["details"]["fields"] == ["Idempotency-Key"]
    first = valid(await data_api.post(resume_path(data_api, "paused"), owner, request))["data"]
    assert first["requeued_jobs"] == 1 and job_state(seed, first_job)[0] == "queued"
    later = blocked_job(seed, data_api.workspace_id, paused)  # blocked after the resume
    replay = valid(await data_api.post(resume_path(data_api, "paused"), owner, request))["data"]
    assert replay == first
    assert job_state(seed, later) == ("blocked", "source_paused")  # a replay changes nothing
    changed = await data_api.post(
        resume_path(data_api, "paused"), owner, {**request, "reason": "another reason"}
    )
    assert changed.status_code == 409 and error_of(changed)["code"] == "IDEMPOTENCY_CONFLICT"
    other = await data_api.post(resume_path(data_api, "running"), owner, request)
    assert other.status_code == 409 and error_of(other)["code"] == "IDEMPOTENCY_CONFLICT"
    for bad in (
        {**request, "reason": "no"},
        {**request, "expected_version": "3"},
        {**request, "enabled": True},
    ):
        refused = await data_api.post(
            resume_path(data_api, "paused"), owner, {**bad, "idempotency_key": "resume-bad-00001"}
        )
        assert refused.status_code == 422, refused.text


async def test_resume_never_enables_a_disabled_source(data_api: DataHarness, seed: Seed) -> None:
    owner = data_api.users.owner
    mk = data_api.data.sources["mk"]
    seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'SYNTHETIC pause', paused_at = now(),"
        " version = version + 1 where id = %s",
        (mk,),
    )
    view = await source_view(data_api, "mk")
    assert view["enabled"] is False and view["paused"] is True and view["activation_problems"]
    job = blocked_job(seed, data_api.workspace_id, mk)
    envelope = valid(
        await data_api.post(resume_path(data_api, "mk"), owner, body(view["version"], "resume-mk-000001"))
    )
    data = envelope["data"]
    assert data["was_paused"] is True and data["enabled"] is False and data["network_allowed"] is False
    assert [w["code"] for w in envelope["warnings"]] == ["ACTIVATION_BLOCKED"]
    assert "sources sync" in envelope["warnings"][0]["message"]
    assert source_row(seed, mk)[:2] == (False, False)
    assert job_state(seed, job)[0] == "queued"  # the worker blocks it again before any request
    after = await source_view(data_api, "mk")
    assert after["enabled"] is False and after["network_allowed"] is False and after["state"] == "disabled"


async def test_resume_never_clears_access_blocked(data_api: DataHarness, seed: Seed) -> None:
    owner = data_api.users.owner
    blocked = data_api.data.sources["blocked"]
    view = await source_view(data_api, "blocked")
    assert view["technical"]["status"] == "access_blocked" and view["network_allowed"] is False
    pause = await data_api.post(
        f"/api/sources/{blocked}/pause",
        owner,
        {
            "expected_version": view["version"],
            "reason": "SYNTHETIC pause",
            "idempotency_key": "pause-blocked-01",
        },
    )
    assert pause.status_code == 200, pause.text
    version = pause.json()["data"]["version"]
    envelope = valid(
        await data_api.post(resume_path(data_api, "blocked"), owner, body(version, "resume-block-0001"))
    )
    data = envelope["data"]
    assert data["technical_status"] == "access_blocked" and data["enabled"] is False
    assert data["network_allowed"] is False
    assert [w["code"] for w in envelope["warnings"]] == ["SOURCE_BLOCKED"]
    assert source_row(seed, blocked)[:3] == (False, False, "access_blocked")


async def test_unknown_and_foreign_sources_are_not_found(
    data_api: DataHarness, db: Database, seed: Seed
) -> None:
    owner = data_api.users.owner
    foreign = await seed_foreign_workspace(db, seed, "resume")
    foreign_source = foreign.sources["running"]
    seed.conn.execute(
        "update app.sources set paused = true, pause_reason = 'SYNTHETIC pause', paused_at = now()"
        " where id = %s",
        (foreign_source,),
    )
    for source_id in (uuid4(), foreign_source):
        missing = await data_api.post(
            f"/api/sources/{source_id}/resume", owner, body(1, f"resume-nf-{str(source_id)[:8]}")
        )
        assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    assert source_row(seed, foreign_source)[1] is True  # another workspace's source stays paused
    bad = await data_api.post("/api/sources/not-a-uuid/resume", owner, body(1, "resume-path-0001"))
    assert bad.status_code == 422


def test_resume_is_owner_only_and_never_an_mcp_tool() -> None:
    route = V11_ROUTE_INDEX[KEY]
    assert route.scope == Scope.CONFIG_ADMIN and route.mcp_tool is None
    assert route.idempotency_header == "optional" and route.request_location == "body"
    assert KEY not in {r.key for r in ROUTES}  # the ROUTES mutations are all MCP tool inputs
    names = [*TOOLS, *V11_TOOLS, *CALC_TOOLS]
    assert not any("resume" in name or "enable" in name for name in names)
    assert TOOLS["sources_pause"].scope == Scope.SOURCES_PAUSE
