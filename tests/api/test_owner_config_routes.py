"""The owner's approval path for tax rule sets and cost profiles (``api.owner_config_routes``;
docs/api_contract.md 10.2): ``/api/settings/tax-rules*`` and ``/api/settings/cost-profiles*``.

Covered: the route table (``config:admin``, body idempotency key, no MCP tool) and the published
envelope schemas; owner-only access (reviewers and viewers get ``403`` on every route and change
nothing); listing the repository draft and the stored versions; storing a draft by file NAME only
(path traversal, separators, ``..`` and a symlink leaving the drafts directory are refused); the
content hash binding of every act; optimistic concurrency on the expected status; illegal
transitions; the approval's named approver and review record (required, bound to the hash, not in
the future, within the 500-character approval record); the whole lifecycle up to ACTIVE and
revoked; idempotent replays and key reuse; the cost profile's transport line and its approval.

Real ASGI app, real PostgreSQL, locally signed Supabase-shaped JWTs. The MK rule set is the shipped
DRAFT file, copied into a temporary config directory; it is stored, reviewed, approved and
activated ONLY in the throwaway test database of each test (SYNTHETIC review records and names).
Nothing here touches the repository's configuration or any real workspace.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator
from tests.api.conftest import (
    ApiHarness,
    SigningKeys,
    TokenFactory,
    add_members,
    build_test_app,
    error_of,
    make_settings,
    running_client,
)
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.schema_check import walk_numbers

from suv_deals.api import owner_config_routes
from suv_deals.api.schemas import V11_ROUTE_INDEX, route_slug
from suv_deals.domain.costs import load_cost_profile
from suv_deals.domain.enums import Scope
from suv_deals.domain.tax_engine import compute_rule_set_sha256, load_rule_set_file
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.views.jsonschema import model_schema

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"
DRAFT_NAME = "mk_passenger_car_import_2026.10-draft.2.json"
DRAFT = CONFIG / "tax_rules" / "drafts" / DRAFT_NAME
PROFILE = CONFIG / "cost_profiles" / "default_unapproved.yaml"
RULE_SET_ID = "mk-passenger-car-import"
VERSION = "2026.10-draft.2"
TAX_RULES = "/api/settings/tax-rules"
STORE = "/api/settings/tax-rules/store"
TRANSITION = "/api/settings/tax-rules/transition"
PROFILES = "/api/settings/cost-profiles"
APPROVE = "/api/settings/cost-profiles/approve"
ROUTE_KEYS = (
    f"GET {TAX_RULES}",
    f"POST {STORE}",
    f"POST {TRANSITION}",
    f"GET {PROFILES}",
    f"POST {APPROVE}",
)


@cache
def draft_hash() -> str:
    return compute_rule_set_sha256(load_rule_set_file(DRAFT))


@cache
def profile_hash() -> str:
    return owner_config_routes.profile_content_sha256(load_cost_profile(PROFILE))


@cache
def _validator(key: str) -> Draft202012Validator:
    schema = model_schema(V11_ROUTE_INDEX[key].response_model, mode="serialization")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(response: httpx.Response, key: str) -> dict[str, Any]:
    """The response's ``data``, after validating the envelope against the published schema."""
    assert response.status_code == 200, (response.status_code, response.text[:800])
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    errors = sorted(_validator(key).iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    assert walk_numbers(body["data"]) == []  # amounts and decimals are strings, never JSON numbers
    data: dict[str, Any] = body["data"]
    return data


def key(name: str) -> str:
    return f"{name}:{uuid.uuid4().hex}"


def review_record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "reviewer": "SYNTHETIC reviewer",
        "reviewed_at": (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0).isoformat(),
        "scope": "SYNTHETIC test review: every component, source and golden case",
        "evidence_refs": ["SYNTHETIC-evidence-1"],
        "content_sha256": draft_hash(),
    }
    record.update(changes)
    return record


def transition(target: str, expected: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_set_id": RULE_SET_ID,
        "version": VERSION,
        "expected_status": expected,
        "target": target,
        "content_sha256": draft_hash(),
        "idempotency_key": key(f"transition-{target}"),
    }
    if target == "approved":
        body.update(approved_by="SYNTHETIC owner", review_record=review_record())
    body.update(changes)
    return body


@dataclass
class OwnerHarness(ApiHarness):
    seed: Seed = None  # type: ignore[assignment]
    config_dir: Path = None  # type: ignore[assignment]

    def count(self, query: str) -> int:
        return int(self.seed.scalar(query, (self.workspace_id,)))

    def rule_rows(self) -> int:
        return self.count("select count(*) from app.tax_rule_sets where workspace_id = %s")

    def audit(self, action: str) -> int:
        return int(
            self.seed.scalar(
                "select count(*) from ops.audit_events where workspace_id = %s and action = %s",
                (self.workspace_id, action),
            )
        )

    async def store(self, **changes: Any) -> httpx.Response:
        body = {"file_name": DRAFT_NAME, "content_sha256": draft_hash(), "idempotency_key": key("store")}
        body.update(changes)
        return await self.post(STORE, self.users.owner, body)

    async def move(self, target: str, expected: str, **changes: Any) -> httpx.Response:
        return await self.post(TRANSITION, self.users.owner, transition(target, expected, **changes))

    async def listing(self) -> dict[str, Any]:
        return valid(await self.get(TAX_RULES, self.users.owner), f"GET {TAX_RULES}")

    async def summary(self) -> dict[str, Any]:
        """The MK rule set's entry in the listing (stored or draft file)."""
        rows = [r for r in (await self.listing())["rule_sets"] if r["rule_set_id"] == RULE_SET_ID]
        assert len(rows) == 1, rows
        row: dict[str, Any] = rows[0]
        return row


def config_copy(tmp_path: Path) -> Path:
    """A temporary config directory with the shipped MK draft and the configured cost profile."""
    target = tmp_path / "config"
    (target / "tax_rules" / "drafts").mkdir(parents=True)
    (target / "cost_profiles").mkdir(parents=True)
    shutil.copy(DRAFT, target / "tax_rules" / "drafts" / DRAFT_NAME)
    shutil.copy(PROFILE, target / "cost_profiles" / PROFILE.name)
    return target


@pytest.fixture
async def owner_api(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory, tmp_path: Path
) -> AsyncIterator[OwnerHarness]:
    workspace_id = seed.workspace(f"API owner config {uuid.uuid4().hex[:6]}")
    users = add_members(seed, workspace_id)
    config_dir = config_copy(tmp_path)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(make_settings(config_dir=config_dir), keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield OwnerHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=workspace_id,
            metrics=metrics,
            seed=seed,
            config_dir=config_dir,
        )


# ------------------------------------------------------------------------------- route table


def test_route_table_is_owner_only_with_body_idempotency_and_no_tool() -> None:
    for route_key in ROUTE_KEYS:
        route = V11_ROUTE_INDEX[route_key]
        assert route.scope == Scope.CONFIG_ADMIN and route.auth == "user_jwt"
        assert route.mcp_tool is None  # never an MCP tool (no tool approves tax rules)
        if route.method == "POST":
            assert route.idempotency_header == "optional" and route.request_location == "body"
        else:
            assert route.idempotency_header is None and route.request_model is None
    assert route_slug(V11_ROUTE_INDEX[f"POST {TRANSITION}"]) == "settings.tax-rules.transition.post"
    assert {owner_config_routes.STORE.key, owner_config_routes.APPROVE_PROFILE.key} <= set(ROUTE_KEYS)


# ------------------------------------------------------------------------------- access


async def test_reviewers_and_viewers_are_refused_on_every_route(owner_api: OwnerHarness) -> None:
    bodies: dict[str, dict[str, Any]] = {
        STORE: {"file_name": DRAFT_NAME, "content_sha256": draft_hash(), "idempotency_key": key("s")},
        TRANSITION: transition("under_review", "draft"),
        APPROVE: {
            "profile_key": "default",
            "version": 3,
            "content_sha256": profile_hash(),
            "idempotency_key": key("a"),
        },
    }
    for user in (owner_api.users.reviewer, owner_api.users.viewer):
        for path in (TAX_RULES, PROFILES):
            refused = await owner_api.get(path, user)
            assert refused.status_code == 403 and error_of(refused)["code"] == "FORBIDDEN"
        for path, body in bodies.items():
            refused = await owner_api.post(path, user, body)
            assert refused.status_code == 403 and error_of(refused)["code"] == "FORBIDDEN"
    stranger = await owner_api.get(TAX_RULES, owner_api.users.stranger)
    assert stranger.status_code == 403
    assert owner_api.rule_rows() == 0
    assert owner_api.count("select count(*) from app.cost_profiles where workspace_id = %s") == 0


# ------------------------------------------------------------------------------- listing


async def test_listing_shows_the_unstored_draft_with_hash_uncertain_items_and_preview(
    owner_api: OwnerHarness,
) -> None:
    listing = await owner_api.listing()
    (row,) = listing["rule_sets"]
    assert row["origin"] == "draft_file" and row["file_name"] == DRAFT_NAME
    assert (row["rule_set_id"], row["version"], row["status"]) == (RULE_SET_ID, VERSION, "draft")
    assert row["content_sha256"] == draft_hash() and row["sha256_sealed"] is False
    assert row["allowed_actions"] == ["store"] and row["production_selectable"] is False
    assert row["approval"] is None and row["created_at"] is None
    assert len(row["components"]) == 21 and len(row["sources"]) == 8
    assert any(c["intermediate"] for c in row["components"])
    assert row["uncertain_items"] and all("UNCERTAIN" in item for item in row["uncertain_items"])
    assert listing["calculator"]["mode"] == "indicative_preview"
    assert listing["calculator"]["rule_set"] == f"{RULE_SET_ID}@{VERSION}"
    assert "ACTIVE, owner-approved" in listing["production_note"]
    assert listing["file_notes"] == []


async def test_listing_reports_ignored_draft_files(owner_api: OwnerHarness) -> None:
    drafts = owner_api.config_dir / "tax_rules" / "drafts"
    (drafts / "broken.json").write_text("{not json", encoding="utf-8")
    listing = await owner_api.listing()
    assert [r["file_name"] for r in listing["rule_sets"]] == [DRAFT_NAME]
    assert any(note.startswith("broken.json: unreadable") for note in listing["file_notes"])


# ------------------------------------------------------------------------------- store


async def test_owner_stores_the_draft_once_with_audit_and_idempotent_replay(owner_api: OwnerHarness) -> None:
    body = {"file_name": DRAFT_NAME, "content_sha256": draft_hash(), "idempotency_key": key("store")}
    first = await owner_api.post(
        STORE, owner_api.users.owner, body, headers={"Idempotency-Key": body["idempotency_key"]}
    )
    stored = valid(first, f"POST {STORE}")
    assert stored["action"] == "store" and stored["status"] == "draft" and stored["previous_status"] is None
    assert stored["content_sha256"] == draft_hash() and stored["sha256_sealed"] is False
    assert "NOT approved" in stored["production_effect"]
    rows = owner_api.rule_rows()
    assert rows == 1 and owner_api.audit("tax_rules.store") == 1

    replay = await owner_api.post(STORE, owner_api.users.owner, body)
    assert valid(replay, f"POST {STORE}") == stored  # the original result, nothing stored again
    assert owner_api.rule_rows() == rows and owner_api.audit("tax_rules.store") == 1

    reused = await owner_api.post(STORE, owner_api.users.owner, {**body, "content_sha256": "0" * 64})
    assert reused.status_code == 409 and error_of(reused)["code"] == "IDEMPOTENCY_CONFLICT"

    again = await owner_api.store()
    error = error_of(again)
    assert again.status_code == 409 and error["code"] == "VERSION_CONFLICT"
    assert error["details"]["reason"] == owner_config_routes.ALREADY_STORED

    row = await owner_api.summary()
    assert row["origin"] == "stored" and row["file_name"] is None and row["status"] == "draft"
    assert row["allowed_actions"] == ["under_review", "revoked"] and row["created_at"] is not None


async def test_store_refuses_a_wrong_hash_and_a_mismatched_header(owner_api: OwnerHarness) -> None:
    wrong = await owner_api.store(content_sha256="f" * 64)
    assert wrong.status_code == 422 and error_of(wrong)["details"]["fields"] == ["content_sha256"]
    body = {"file_name": DRAFT_NAME, "content_sha256": draft_hash(), "idempotency_key": key("store")}
    header = await owner_api.post(
        STORE, owner_api.users.owner, body, headers={"Idempotency-Key": "other-key-0001"}
    )
    assert header.status_code == 422 and error_of(header)["details"]["fields"] == ["Idempotency-Key"]
    assert owner_api.rule_rows() == 0


@pytest.mark.parametrize(
    "file_name",
    [
        "../cost_profiles/default_unapproved.yaml",
        "../../../etc/passwd.json",
        f"../drafts/{DRAFT_NAME}",
        "sub/draft.json",
        "..\\draft.json",
        "draft..json",
        "/etc/draft.json",
        ".hidden.json",
        "draft.json\x00.json",
    ],
)
async def test_store_refuses_paths_instead_of_file_names(owner_api: OwnerHarness, file_name: str) -> None:
    refused = await owner_api.store(file_name=file_name)
    assert refused.status_code == 422, refused.text
    assert error_of(refused)["details"]["fields"] == ["file_name"]
    assert file_name not in refused.text  # the value is never echoed
    assert owner_api.rule_rows() == 0


async def test_store_refuses_unknown_names_and_symlinks_leaving_the_drafts_directory(
    owner_api: OwnerHarness, tmp_path: Path
) -> None:
    missing = await owner_api.store(file_name="missing.json")
    assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(DRAFT, outside / "escape.json")
    (owner_api.config_dir / "tax_rules" / "drafts" / "escape.json").symlink_to(outside / "escape.json")
    escaped = await owner_api.store(file_name="escape.json")
    assert escaped.status_code == 404 and error_of(escaped)["code"] == "NOT_FOUND"
    listing = await owner_api.listing()
    assert any(note.startswith("escape.json: ignored") for note in listing["file_notes"])
    assert owner_api.rule_rows() == 0


async def test_store_refuses_a_file_that_is_not_a_draft(owner_api: OwnerHarness) -> None:
    drafts = owner_api.config_dir / "tax_rules" / "drafts"
    text = DRAFT.read_text(encoding="utf-8").replace('"status": "draft"', '"status": "unapproved"', 1)
    (drafts / "not_a_draft.json").write_text(text, encoding="utf-8")
    listing = await owner_api.listing()
    assert any(note.startswith("not_a_draft.json: ignored") for note in listing["file_notes"])
    refused = await owner_api.store(
        file_name="not_a_draft.json",
        content_sha256=compute_rule_set_sha256(load_rule_set_file(drafts / "not_a_draft.json")),
    )
    assert refused.status_code == 422 and error_of(refused)["details"]["fields"] == ["file_name"]
    assert owner_api.rule_rows() == 0


# ------------------------------------------------------------------------------- lifecycle


async def test_full_lifecycle_store_review_approve_activate_revoke(owner_api: OwnerHarness) -> None:
    """Only in this throwaway test database: the owner's explicit acts, one by one."""
    valid(await owner_api.store(), f"POST {STORE}")

    review = valid(await owner_api.move("under_review", "draft"), f"POST {TRANSITION}")
    assert (review["previous_status"], review["status"]) == ("draft", "under_review")
    assert review["sha256_sealed"] is True and review["content_sha256"] == draft_hash()
    assert (await owner_api.summary())["allowed_actions"] == ["approved", "revoked"]

    approved = valid(await owner_api.move("approved", "under_review"), f"POST {TRANSITION}")
    assert approved["status"] == "approved" and approved["approved_by"] == "SYNTHETIC owner"
    assert "until it is activated" in approved["production_effect"]
    row = await owner_api.summary()
    assert row["production_selectable"] is False and row["allowed_actions"] == ["active", "revoked"]
    approval = row["approval"]
    assert approval["approved_by"] == "SYNTHETIC owner"
    assert approval["approved_by_principal"] == str(owner_api.users.owner)  # the authenticated owner
    assert approval["review_record"]["content_sha256"] == draft_hash()
    assert approval["review_record"]["evidence_refs"] == ["SYNTHETIC-evidence-1"]
    assert (await owner_api.listing())["calculator"]["mode"] == "indicative_preview"  # approved is not active

    active = valid(await owner_api.move("active", "approved"), f"POST {TRANSITION}")
    assert active["status"] == "active" and "ACTIVE" in active["production_effect"]
    listing = await owner_api.listing()
    assert listing["calculator"]["mode"] == "active"
    assert listing["calculator"]["rule_set"] == f"{RULE_SET_ID}@{VERSION}"
    assert (await owner_api.summary())["production_selectable"] is True

    revoked = valid(await owner_api.move("revoked", "active"), f"POST {TRANSITION}")
    assert revoked["status"] == "revoked" and (await owner_api.summary())["allowed_actions"] == []
    assert owner_api.audit("tax_rules.transition") == 3 and owner_api.audit("tax_rules.approve") == 1


async def test_transition_checks_hash_expected_status_and_lifecycle(owner_api: OwnerHarness) -> None:
    valid(await owner_api.store(), f"POST {STORE}")
    wrong_hash = await owner_api.move("under_review", "draft", content_sha256="a" * 64)
    assert wrong_hash.status_code == 422 and error_of(wrong_hash)["details"]["fields"] == ["content_sha256"]

    stale = await owner_api.move("revoked", "under_review")
    error = error_of(stale)
    assert stale.status_code == 409 and error["code"] == "VERSION_CONFLICT"
    assert error["details"] == {
        "reason": "status_changed",
        "expected_status": "under_review",
        "current_status": "draft",
    }

    illegal = await owner_api.move("active", "draft")
    assert illegal.status_code == 422 and "not permitted" in error_of(illegal)["message"]
    skipped = await owner_api.move("approved", "draft")
    assert skipped.status_code == 422  # approval only from under_review

    missing = await owner_api.post(
        TRANSITION, owner_api.users.owner, transition("under_review", "draft", rule_set_id="unknown-rule-set")
    )
    assert missing.status_code == 404 and error_of(missing)["code"] == "NOT_FOUND"
    assert (await owner_api.summary())["status"] == "draft"


async def test_approval_needs_a_named_approver_and_a_bound_review_record(owner_api: OwnerHarness) -> None:
    valid(await owner_api.store(), f"POST {STORE}")
    valid(await owner_api.move("under_review", "draft"), f"POST {TRANSITION}")
    body = transition("approved", "under_review")

    without = await owner_api.post(TRANSITION, owner_api.users.owner, {**body, "review_record": None})
    assert without.status_code == 422 and "review_record" in error_of(without)["details"]["fields"]
    nameless = await owner_api.post(
        TRANSITION, owner_api.users.owner, {k: v for k, v in body.items() if k != "approved_by"}
    )
    assert nameless.status_code == 422 and "approved_by" in error_of(nameless)["details"]["fields"]
    extra = await owner_api.post(
        TRANSITION,
        owner_api.users.owner,
        transition("revoked", "under_review", approved_by="SYNTHETIC owner"),
    )
    assert extra.status_code == 422 and "approved_by" in error_of(extra)["details"]["fields"]

    other_hash = await owner_api.post(
        TRANSITION, owner_api.users.owner, {**body, "review_record": review_record(content_sha256="b" * 64)}
    )
    assert other_hash.status_code == 422
    assert error_of(other_hash)["details"]["fields"] == ["review_record.content_sha256"]
    future = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0).isoformat()
    later = await owner_api.post(
        TRANSITION, owner_api.users.owner, {**body, "review_record": review_record(reviewed_at=future)}
    )
    assert later.status_code == 422 and error_of(later)["details"]["fields"] == ["review_record.reviewed_at"]
    too_long = await owner_api.post(
        TRANSITION,
        owner_api.users.owner,
        {**body, "review_record": review_record(scope="SYNTHETIC scope " * 30)},
    )
    assert too_long.status_code == 422 and error_of(too_long)["details"]["limit"] == 500

    row = await owner_api.summary()
    assert row["status"] == "under_review" and row["approval"] is None
    assert owner_api.audit("tax_rules.approve") == 0


async def test_transition_replays_the_original_result_for_the_same_key(owner_api: OwnerHarness) -> None:
    valid(await owner_api.store(), f"POST {STORE}")
    body = transition("under_review", "draft")
    first = valid(await owner_api.post(TRANSITION, owner_api.users.owner, body), f"POST {TRANSITION}")
    replay = valid(await owner_api.post(TRANSITION, owner_api.users.owner, body), f"POST {TRANSITION}")
    assert replay == first and owner_api.audit("tax_rules.transition") == 1
    changed = await owner_api.post(TRANSITION, owner_api.users.owner, {**body, "target": "revoked"})
    assert changed.status_code == 409 and error_of(changed)["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await owner_api.summary())["status"] == "under_review"


# ------------------------------------------------------------------------------- cost profiles


async def test_cost_profile_listing_shows_the_configured_fixed_transport(owner_api: OwnerHarness) -> None:
    view = valid(await owner_api.get(PROFILES, owner_api.users.owner), f"GET {PROFILES}")
    configured = view["configured"]
    assert (
        view["configured_file"] == "cost_profiles/default_unapproved.yaml"
        and view["configured_problem"] is None
    )
    assert (configured["origin"], configured["profile_key"], configured["version"]) == (
        "configured_file",
        "default",
        3,
    )
    assert configured["approval_status"] == "not_stored" and configured["can_approve"] is True
    assert configured["content_sha256"] == profile_hash() and configured["matches_stored"] is None
    transport = configured["transport"]
    assert (transport["status"], transport["currency"]) == ("estimated", "EUR")
    assert (transport["low"], transport["base"], transport["high"]) == ("700.00", "700.00", "700.00")
    unknown = [line for line in configured["lines"] if line["status"] == "unknown"]
    assert unknown and all(line["base"] is None for line in unknown)  # unknown is null, never 0
    assert view["profiles"] == []


async def test_owner_approves_the_configured_cost_profile_bound_to_its_hash(owner_api: OwnerHarness) -> None:
    body = {
        "profile_key": "default",
        "version": 3,
        "content_sha256": profile_hash(),
        "idempotency_key": key("approve"),
    }
    wrong = await owner_api.post(
        APPROVE, owner_api.users.owner, {**body, "content_sha256": "c" * 64, "idempotency_key": key("a")}
    )
    assert wrong.status_code == 422 and error_of(wrong)["details"]["fields"] == ["content_sha256"]
    unknown = await owner_api.post(
        APPROVE, owner_api.users.owner, {**body, "version": 99, "idempotency_key": key("a")}
    )
    assert unknown.status_code == 404
    assert owner_api.count("select count(*) from app.cost_profiles where workspace_id = %s") == 0

    approved = valid(await owner_api.post(APPROVE, owner_api.users.owner, body), f"POST {APPROVE}")
    assert approved["approval_status"] == "approved" and approved["stored_now"] is True
    assert approved["content_sha256"] == profile_hash()
    assert owner_api.audit("cost_profile.store") == 1 and owner_api.audit("cost_profile.approve") == 1
    principal = owner_api.seed.scalar(
        "select approved_by from app.cost_profiles where workspace_id = %s and id = %s",
        (owner_api.workspace_id, UUID(approved["id"])),
    )
    assert principal == owner_api.users.owner

    replay = valid(await owner_api.post(APPROVE, owner_api.users.owner, body), f"POST {APPROVE}")
    assert replay == approved and owner_api.audit("cost_profile.approve") == 1
    again = await owner_api.post(APPROVE, owner_api.users.owner, {**body, "idempotency_key": key("approve")})
    assert again.status_code == 409 and error_of(again)["code"] == "VERSION_CONFLICT"

    view = valid(await owner_api.get(PROFILES, owner_api.users.owner), f"GET {PROFILES}")
    assert view["configured"]["approval_status"] == "approved" and view["configured"]["can_approve"] is False
    assert view["configured"]["matches_stored"] is True
    (stored,) = view["profiles"]
    assert stored["origin"] == "stored" and stored["id"] == approved["id"]
    assert stored["approved_by_principal"] == str(owner_api.users.owner)
    assert stored["transport"]["base"] == "700.00"
