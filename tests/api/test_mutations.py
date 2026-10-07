"""Mutation routes end to end (marker ``db``): review claim/release/submit, spec 19 dashboard
actions, notes, rechecks and source pauses.

Covers idempotent replay (same key + same body returns the original result; a different body is
``IDEMPOTENCY_CONFLICT``), optimistic concurrency (``VERSION_CONFLICT``), claim ownership
(``ALREADY_CLAIMED``/``CLAIM_EXPIRED``), double submits (sequential and concurrent) and a token that
expires mid-review followed by an idempotent retry with a fresh token.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from tests.api.conftest import DataHarness, error_of
from tests.integration.db.helpers import Seed

from suv_deals.api.routes import dashboard_action_request
from suv_deals.domain.due_diligence import DashboardAction

pytestmark = pytest.mark.db


def case_path(data_api: DataHarness, label: str, action: str) -> str:
    return f"/api/reviews/{data_api.data.cases[label]}/{action}"


async def claim(
    data_api: DataHarness, user: UUID, key: str, *, label: str = "priced", version: int = 1
) -> Any:
    return await data_api.post(
        case_path(data_api, label, "claim"), user, {"expected_version": version, "idempotency_key": key}
    )


def submit_body(token: str, key: str, data_api: DataHarness, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "claim_token": token,
        "expected_version": 2,
        "listing_revision": 2,
        "valuation_id": str(data_api.data.valuations["estimated"]),
        "outcome": "watch",
        "reason_codes": ["SYNTHETIC_PRICE_WATCH"],
        "summary": "SYNTHETIC: watch the price; comparables support the band but costs are unknown.",
        "evidence_ids": [],
        "idempotency_key": key,
    }
    body.update(overrides)
    return body


async def test_claim_submit_flow_with_replay_and_conflicts(data_api: DataHarness, seed: Seed) -> None:
    reviewer, other = data_api.users.reviewer, data_api.users.second_reviewer
    queue = (await data_api.get("/api/reviews", reviewer)).json()["data"]["items"]
    item = next(i for i in queue if i["case_id"] == str(data_api.data.cases["priced"]))
    assert item["case_version"] == 1
    assert item["claim"] == {"claimed": False, "held_by_caller": False, "expires_at": None}

    stale = await claim(data_api, reviewer, "claim-stale-0001", version=5)
    assert stale.status_code == 409
    assert error_of(stale)["code"] == "VERSION_CONFLICT"

    first = await claim(data_api, reviewer, "claim-flow-00001")
    assert first.status_code == 200, first.text
    grant = first.json()["data"]
    token = grant["claim_token"]
    assert isinstance(token, str) and grant["claim_token_redacted"] is False
    assert grant["case_version"] == 2 and grant["listing_revision"] == 2
    assert grant["valuation_id"] == str(data_api.data.valuations["estimated"])

    replay = await claim(data_api, reviewer, "claim-flow-00001")
    assert replay.status_code == 200
    assert replay.json()["data"]["claim_token"] is None
    assert replay.json()["data"]["claim_token_redacted"] is True
    # The replay reports the original expiry (stored at millisecond precision).
    assert replay.json()["data"]["expires_at"][:23] == grant["expires_at"][:23]

    reused = await claim(data_api, reviewer, "claim-flow-00001", version=2)
    assert reused.status_code == 409
    assert error_of(reused)["code"] == "IDEMPOTENCY_CONFLICT"

    taken = await claim(data_api, other, "claim-other-0001", version=2)
    assert taken.status_code == 409
    assert error_of(taken)["code"] == "ALREADY_CLAIMED"
    assert str(reviewer) not in taken.text

    other_submit = await data_api.post(
        case_path(data_api, "priced", "submit"), other, submit_body(token, "submit-other-01", data_api)
    )
    assert other_submit.status_code == 409
    assert error_of(other_submit)["code"] == "ALREADY_CLAIMED"

    wrong_version = await data_api.post(
        case_path(data_api, "priced", "submit"),
        reviewer,
        submit_body(token, "submit-stale-001", data_api, expected_version=1),
    )
    assert wrong_version.status_code == 409
    error = error_of(wrong_version)
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"]["current_version"] == 2

    wrong_revision = await data_api.post(
        case_path(data_api, "priced", "submit"),
        reviewer,
        submit_body(token, "submit-stale-002", data_api, listing_revision=1),
    )
    assert error_of(wrong_revision)["code"] == "VERSION_CONFLICT"

    bad_token = await data_api.post(
        case_path(data_api, "priced", "submit"),
        reviewer,
        submit_body("A" * 43, "submit-badtok-01", data_api),
    )
    assert bad_token.status_code == 409
    assert error_of(bad_token)["code"] == "CLAIM_EXPIRED"
    assert "A" * 43 not in bad_token.text

    body = submit_body(token, "submit-flow-0001", data_api)
    decided = await data_api.post(case_path(data_api, "priced", "submit"), reviewer, body)
    assert decided.status_code == 201, decided.text
    decision = decided.json()["data"]
    assert decision["outcome"] == "watch"
    assert decision["case_state"] == "watch"
    assert decision["actor"] == {"principal_id": str(reviewer), "principal_kind": "user", "role": "reviewer"}
    assert decision["tool_request_id"] == decided.headers["x-request-id"]
    assert token not in decided.text

    again = await data_api.post(case_path(data_api, "priced", "submit"), reviewer, body)
    assert again.status_code == 201
    assert again.json()["data"] == decision  # the same decision, not a second one

    changed = await data_api.post(
        case_path(data_api, "priced", "submit"),
        reviewer,
        {**body, "summary": "SYNTHETIC: a different rationale."},
    )
    assert changed.status_code == 409
    assert error_of(changed)["code"] == "IDEMPOTENCY_CONFLICT"

    rows = seed.scalar(
        "select count(*) from app.review_decisions where workspace_id = %s and case_id = %s",
        (data_api.workspace_id, data_api.data.cases["priced"]),
    )
    assert rows == 1
    case = (await data_api.get(f"/api/reviews/{data_api.data.cases['priced']}", data_api.users.viewer)).json()
    assert case["data"]["state"] == "watch"
    assert [d["decision_id"] for d in case["data"]["decisions"]] == [decision["decision_id"]]


async def test_concurrent_double_submit_returns_one_decision(data_api: DataHarness, seed: Seed) -> None:
    reviewer = data_api.users.reviewer
    token = (await claim(data_api, reviewer, "claim-double-001")).json()["data"]["claim_token"]
    body = submit_body(
        token, "submit-double-01", data_api, outcome="rejected", reason_codes=["SYNTHETIC_TOO_RISKY"]
    )
    path = case_path(data_api, "priced", "submit")
    responses = await asyncio.gather(*(data_api.post(path, reviewer, body) for _ in range(4)))
    assert {r.status_code for r in responses} == {201}, [r.text for r in responses]
    assert len({r.json()["data"]["decision_id"] for r in responses}) == 1
    count = seed.scalar(
        "select count(*) from app.review_decisions where case_id = %s", (data_api.data.cases["priced"],)
    )
    assert count == 1


async def test_token_expiry_mid_review_then_idempotent_retry_with_a_fresh_token(
    data_api: DataHarness,
) -> None:
    reviewer = data_api.users.reviewer
    token = (await claim(data_api, reviewer, "claim-expiry-001")).json()["data"]["claim_token"]
    body = submit_body(token, "submit-expiry-01", data_api)
    path = case_path(data_api, "priced", "submit")
    expired_headers = data_api.auth(reviewer, expires_in=60, issued_offset=-600)
    expired = await data_api.client.post(path, json=body, headers=expired_headers)
    assert expired.status_code == 401
    assert error_of(expired)["code"] == "UNAUTHENTICATED"
    fresh = await data_api.post(path, reviewer, body)  # the client refreshed its session
    assert fresh.status_code == 201, fresh.text
    retry = await data_api.post(path, reviewer, body)  # e.g. network loss after the write
    assert retry.json()["data"]["decision_id"] == fresh.json()["data"]["decision_id"]


async def test_release_is_idempotent_and_only_affects_the_callers_claim(data_api: DataHarness) -> None:
    reviewer, other = data_api.users.reviewer, data_api.users.second_reviewer
    token = (await claim(data_api, reviewer, "claim-release-01")).json()["data"]["claim_token"]
    not_held = await data_api.post(
        case_path(data_api, "priced", "release"),
        other,
        {"claim_token": token, "idempotency_key": "release-other-1"},
    )
    assert not_held.status_code == 200
    assert not_held.json()["data"]["released"] is False
    released = await data_api.post(
        case_path(data_api, "priced", "release"),
        reviewer,
        {"claim_token": token, "idempotency_key": "release-own-01"},
    )
    assert released.status_code == 200
    assert released.json()["data"]["released"] is True
    assert released.json()["data"]["state"] == "pending"
    replay = await data_api.post(
        case_path(data_api, "priced", "release"),
        reviewer,
        {"claim_token": token, "idempotency_key": "release-own-01"},
    )
    assert replay.json()["data"] == released.json()["data"]
    reclaim = await claim(data_api, data_api.users.second_reviewer, "claim-after-rel1", version=3)
    assert reclaim.status_code == 200


async def test_dashboard_actions_map_onto_needs_information(data_api: DataHarness) -> None:
    reviewer = data_api.users.reviewer
    token = (await claim(data_api, reviewer, "claim-action-001")).json()["data"]["claim_token"]
    path = case_path(data_api, "priced", "submit")
    misuse = submit_body(token, "submit-misuse-01", data_api, reason_codes=["needs_documents"])
    refused = await data_api.post(path, reviewer, misuse)
    assert refused.status_code == 422
    assert error_of(refused)["details"]["fields"] == ["outcome", "reason_codes"]
    missing = submit_body(
        token, "submit-missing-1", data_api, outcome="needs_information", reason_codes=["needs_inspection"]
    )
    assert (await data_api.post(path, reviewer, missing)).status_code == 422  # missing_information required
    action = dashboard_action_request(
        DashboardAction.NEEDS_INSPECTION,
        claim_token=token,
        expected_version=2,
        listing_revision=2,
        valuation_id=data_api.data.valuations["estimated"],
        missing_information=["SYNTHETIC: independent inspection of the AWD coupling and DPF"],
        idempotency_key="submit-action-01",
    )
    decided = await data_api.post(path, reviewer, action.model_dump(mode="json"))
    assert decided.status_code == 201, decided.text
    decision = decided.json()["data"]
    assert decision["outcome"] == "needs_information"
    assert decision["reason_codes"] == ["needs_inspection"]
    assert decision["missing_information"] == [
        "SYNTHETIC: independent inspection of the AWD coupling and DPF"
    ]
    for kind in DashboardAction:
        body = dashboard_action_request(
            kind,
            claim_token="x" * 32,
            expected_version=1,
            listing_revision=1,
            missing_information=["item"],
            idempotency_key="action-shape-001",
        )
        assert body.outcome.value == "needs_information" and body.reason_codes == (kind.value,)


async def test_submit_body_validation_names_fields_without_values(data_api: DataHarness) -> None:
    reviewer = data_api.users.reviewer
    path = case_path(data_api, "priced", "submit")
    secret_token = "SYNTHETICclaimtokenvalue-should-never-echo"
    cases: dict[str, tuple[dict[str, Any], list[str]]] = {
        "unknown field": (
            {**submit_body(secret_token, "submit-val-0001", data_api), "actor": "owner"},
            ["actor"],
        ),
        "string version": (
            submit_body(secret_token, "submit-val-0002", data_api, expected_version="2"),
            ["expected_version"],
        ),
        "bad outcome": (
            submit_body(secret_token, "submit-val-0003", data_api, outcome="bought"),
            ["outcome"],
        ),
        "duplicate codes": (
            submit_body(secret_token, "submit-val-0004", data_api, reason_codes=["a", "a"]),
            ["reason_codes"],
        ),
        "short key": (submit_body(secret_token, "short", data_api), ["idempotency_key"]),
    }
    for name, (body, fields) in cases.items():
        response = await data_api.post(path, reviewer, body)
        assert response.status_code == 422, name
        assert error_of(response)["details"]["fields"] == fields, name
        assert secret_token not in response.text
    not_json = await data_api.client.post(
        path, content=b"{not json", headers={**data_api.auth(reviewer), "Content-Type": "application/json"}
    )
    assert not_json.status_code == 422
    assert error_of(not_json)["details"]["fields"] == ["body"]
    mismatch = await data_api.post(
        path,
        reviewer,
        submit_body(secret_token, "submit-val-0005", data_api),
        headers={"Idempotency-Key": "other-key-0001"},
    )
    assert mismatch.status_code == 422
    assert error_of(mismatch)["details"]["fields"] == ["Idempotency-Key"]


async def test_notes_are_labelled_idempotent_and_listed(data_api: DataHarness) -> None:
    reviewer, owner = data_api.users.reviewer, data_api.users.owner
    listing = data_api.data.listings["priced"]
    path = f"/api/listings/{listing}/notes"
    body = {"note": "SYNTHETIC: ask for the timing-belt invoice.", "idempotency_key": "note-flow-00001"}
    created = await data_api.post(path, reviewer, body)
    assert created.status_code == 201, created.text
    note = created.json()["data"]
    assert note["label"] == "reviewer" and note["author_kind"] == "user"
    assert note["author_principal_id"] == str(reviewer)
    assert (await data_api.post(path, reviewer, body)).json()["data"] == note
    conflict = await data_api.post(path, reviewer, {**body, "note": "SYNTHETIC: something else"})
    assert error_of(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
    owner_note = await data_api.post(
        path, owner, {"note": "SYNTHETIC owner note", "idempotency_key": "note-owner-0001"}
    )
    assert owner_note.json()["data"]["label"] == "owner"
    detail = (await data_api.get(f"/api/candidates/{listing}", data_api.users.viewer)).json()["data"]
    assert {n["note_id"] for n in detail["notes"]} >= {note["note_id"], owner_note.json()["data"]["note_id"]}
    control = await data_api.post(
        path, reviewer, {"note": "bidi " + chr(0x202E) + " override", "idempotency_key": "note-ctrl-00001"}
    )
    assert control.status_code == 422


async def test_recheck_queues_a_bounded_job_for_registered_listings_only(
    data_api: DataHarness, seed: Seed
) -> None:
    reviewer = data_api.users.reviewer
    listing = data_api.data.listings["priced"]
    path = f"/api/listings/{listing}/recheck"
    body = {"reason": "SYNTHETIC: price changed on the source", "idempotency_key": "recheck-flow-001"}
    queued = await data_api.post(path, reviewer, body)
    assert queued.status_code == 202, queued.text
    result = queued.json()["data"]
    assert result["job_type"] == "recheck" and result["state"] == "queued" and result["deduplicated"] is False
    job = seed.conn.execute(
        "select job_type, listing_id, payload from ops.jobs where id = %s", (result["job_id"],)
    ).fetchone()
    assert job is not None and job[0] == "recheck" and job[1] == listing
    assert job[2]["url"].startswith("https://synthetic-dealer.example/vehicles/")  # the stored URL only
    assert (await data_api.post(path, reviewer, body)).json()["data"] == result
    second = await data_api.post(path, reviewer, {**body, "idempotency_key": "recheck-flow-002"})
    assert second.status_code == 202
    assert second.json()["data"]["deduplicated"] is True
    assert second.json()["data"]["job_id"] == result["job_id"]
    with_url = await data_api.post(
        path, reviewer, {**body, "url": "http://169.254.169.254/", "idempotency_key": "recheck-url-0001"}
    )
    assert with_url.status_code == 422
    assert error_of(with_url)["details"]["fields"] == ["url"]

    paused = await data_api.post(
        f"/api/listings/{data_api.data.listings['paused']}/recheck",
        reviewer,
        {**body, "idempotency_key": "recheck-paused-1"},
    )
    assert paused.status_code == 409
    assert error_of(paused)["code"] == "SOURCE_PAUSED"
    blocked_listing = seed.listing(data_api.workspace_id, data_api.data.sources["blocked"])
    blocked = await data_api.post(
        f"/api/listings/{blocked_listing}/recheck", reviewer, {**body, "idempotency_key": "recheck-blocked1"}
    )
    assert blocked.status_code == 409
    assert error_of(blocked)["code"] == "ACCESS_BLOCKED"
    audit = seed.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'recheck.request'",
        (data_api.workspace_id,),
    )
    assert audit == 2


async def test_owner_pauses_a_source_with_optimistic_concurrency(data_api: DataHarness) -> None:
    owner = data_api.users.owner
    sources = (await data_api.get("/api/sources", owner)).json()["data"]["items"]
    running = next(s for s in sources if s["source_id"] == str(data_api.data.sources["running"]))
    path = f"/api/sources/{running['source_id']}/pause"
    stale = await data_api.post(
        path,
        owner,
        {
            "expected_version": running["version"] + 1,
            "reason": "SYNTHETIC pause",
            "idempotency_key": "pause-stale-0001",
        },
    )
    assert stale.status_code == 409
    assert error_of(stale)["code"] == "VERSION_CONFLICT"
    body = {
        "expected_version": running["version"],
        "reason": "SYNTHETIC: parser drift suspected",
        "idempotency_key": "pause-flow-00001",
    }
    paused = await data_api.post(path, owner, body)
    assert paused.status_code == 200, paused.text
    result = paused.json()["data"]
    assert result["paused"] is True and result["already_paused"] is False
    assert result["version"] == running["version"] + 1
    assert "SOURCE_PAUSED" in {w["code"] for w in paused.json()["warnings"]}
    assert (await data_api.post(path, owner, body)).json()["data"] == result
    after = (await data_api.get("/api/sources", data_api.users.viewer)).json()["data"]["items"]
    assert next(s for s in after if s["source_id"] == running["source_id"])["state"] == "paused"
