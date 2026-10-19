"""Fixture lineage, immutable outbox events and parent consistency (spec 10, 11, 13, 18, 22).

Independent-review additions for WP2:
- a fixture can never become, or feed, a real review/notification (spec 18: fixtures
  "cannot generate external notifications" and are never shown as a real opportunity);
- an outbox event is immutable once written: the event ID is stable across attempts and
  the payload matches its hash (spec 13, 22);
- composite foreign keys keep children consistent with their parent source/listing;
- the source activation gate mirrors domain.sources.activation_problems() exactly,
  including card_only sources (no detail paths needed);
- delivery fencing: waiting deliveries carry no lease.

All data is synthetic.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import errors, sql
from tests.integration.db.helpers import (
    SV_FROZEN,
    SV_REFERENCE,
    T0,
    Seed,
    World,
    backend,
    sha,
    unique,
)

pytestmark = pytest.mark.db


def _violation(exc: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return exc.value.sqlstate


def _check_name(exc: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return exc.value.diag.constraint_name


# ---------------------------------------------------------------------------------------
# Outbox events are immutable (spec 13, 18, 22)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("is_fixture", False),
        ("event_id", uuid.uuid4()),
        ("payload", '{"summary": "rewritten"}'),
        ("payload_hash", "f" * 64),
        ("dedup_key", "review.pending:rewritten"),
        ("event_type", "review.decided"),
        ("aggregate_id", uuid.uuid4()),
        ("aggregate_version", 99),
    ],
)
def test_outbox_event_identity_is_immutable(
    db_conn: psycopg.Connection, seed: Seed, world_a: World, column: str, value: Any
) -> None:
    row = seed.outbox(world_a.workspace_id, is_fixture=True, state="blocked", blocker_code="FIXTURE_EVENT")
    statement = sql.SQL("update ops.outbox set {} = %s where id = %s").format(sql.Identifier(column))
    # Even the owner cannot rewrite an event (trigger), and neither can suv_backend.
    with pytest.raises(psycopg.Error) as exc:
        db_conn.execute(statement, (value, row))
    assert _violation(exc) == SV_FROZEN, column
    with pytest.raises(psycopg.Error) as exc, backend(db_conn, world_a.workspace_id):
        db_conn.execute(statement, (value, row))
    assert _violation(exc) == SV_FROZEN, column


def test_fixture_outbox_row_cannot_be_turned_into_a_deliverable_event(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    """The review finding: flipping is_fixture used to make a blocked fixture row pending."""
    row = seed.outbox(world_a.workspace_id, is_fixture=True, state="blocked", blocker_code="FIXTURE_EVENT")
    with pytest.raises(psycopg.Error) as exc, backend(db_conn, world_a.workspace_id):
        db_conn.execute(
            "update ops.outbox set is_fixture = false, state = 'pending', blocker_code = null where id = %s",
            (row,),
        )
    assert _violation(exc) == SV_FROZEN
    # Lifecycle columns stay mutable: the fixture row may still be cancelled.
    with backend(db_conn, world_a.workspace_id):
        assert (
            db_conn.execute(
                "update ops.outbox set state = 'cancelled', completed_at = now() where id = %s", (row,)
            ).rowcount
            == 1
        )
    assert seed.scalar("select (state, is_fixture)::text from ops.outbox where id = %s", (row,)) == (
        "(cancelled,t)"
    )


# ---------------------------------------------------------------------------------------
# Fixture lineage (spec 18)
# ---------------------------------------------------------------------------------------


def test_fixture_case_cannot_emit_a_real_outbox_event(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    fixture_case = seed.review_case(ws, listing, rev, is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.outbox(ws, aggregate_id=fixture_case, is_fixture=False)
    assert _violation(exc) == SV_REFERENCE
    # Deferred to commit: writing the outbox row before the case is caught as well.
    other_listing = seed.listing(ws, world_a.source_id)
    _, gen, obs = seed.detail_observation(ws, other_listing)
    other_rev = seed.revision(ws, other_listing, 1, detail_generation=gen, observation_id=obs)
    case_id = uuid.uuid4()
    with pytest.raises(psycopg.Error) as exc, backend(db_conn, ws):
        db_conn.execute(
            "insert into ops.outbox (workspace_id, event_type, aggregate_type, aggregate_id, payload,"
            " payload_hash, dedup_key) values (%s, 'review.pending', 'review_case', %s, '{}', %s, %s)",
            (ws, case_id, sha("{}"), unique("review.pending")),
        )
        db_conn.execute(
            "insert into app.review_cases (id, workspace_id, listing_id, revision_id, profile_key,"
            " queue_label, readiness, is_fixture)"
            " values (%s, %s, %s, %s, 'primary', 'Primary queue', 'not_valued', true)",
            (case_id, ws, other_listing, other_rev),
        )
    assert _violation(exc) == SV_REFERENCE
    assert seed.scalar("select count(*) from app.review_cases where id = %s", (case_id,)) == 0
    # The legitimate shapes: a blocked fixture event for a fixture case, a real event for a real case.
    seed.outbox(ws, aggregate_id=fixture_case, is_fixture=True, state="blocked", blocker_code="FIXTURE_EVENT")
    real_case = seed.review_case(ws, other_listing, other_rev, is_fixture=False)
    seed.outbox(ws, aggregate_id=real_case, is_fixture=False)


def test_real_review_case_cannot_use_a_fixture_valuation(seed: Seed, world_a: World) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    fixture_valuation = seed.valuation(ws, listing, rev, is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.review_case(ws, listing, rev, is_fixture=False, valuation_id=fixture_valuation)
    assert _violation(exc) == SV_REFERENCE
    real_case = seed.review_case(ws, listing, rev, is_fixture=False)
    with pytest.raises(psycopg.Error) as exc:
        seed.conn.execute(
            "update app.review_cases set valuation_id = %s, row_version = row_version + 1 where id = %s",
            (fixture_valuation, real_case),
        )
    assert _violation(exc) == SV_REFERENCE
    real_valuation = seed.valuation(ws, listing, rev, is_fixture=False)
    seed.conn.execute(
        "update app.review_cases set valuation_id = %s, row_version = row_version + 1 where id = %s",
        (real_valuation, real_case),
    )
    # A fixture case may use fixture (or real) valuations.
    seed.conn.execute("update app.review_cases set state = 'superseded' where id = %s", (real_case,))
    seed.review_case(ws, listing, rev, is_fixture=True, valuation_id=fixture_valuation)


def test_review_decision_fixture_flag_follows_its_case(seed: Seed, world_a: World) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    fixture_case = seed.review_case(ws, listing, rev, is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.decision(ws, fixture_case, listing, rev, is_fixture=False)
    assert _violation(exc) == SV_REFERENCE
    seed.conn.execute("update app.review_cases set state = 'superseded' where id = %s", (fixture_case,))
    real_case = seed.review_case(ws, listing, rev, is_fixture=False)
    with pytest.raises(psycopg.Error) as exc:
        seed.decision(ws, real_case, listing, rev, is_fixture=True)
    assert _violation(exc) == SV_REFERENCE
    fixture_valuation = seed.valuation(ws, listing, rev, is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.decision(ws, real_case, listing, rev, is_fixture=False, valuation_id=fixture_valuation)
    assert _violation(exc) == SV_REFERENCE
    seed.decision(ws, real_case, listing, rev, is_fixture=False)


def test_tax_rule_fixture_flag_is_immutable_even_in_draft(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    fixture_draft = seed.tax_rule(ws, status="draft", is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.conn.execute("update app.tax_rule_sets set is_fixture = false where id = %s", (fixture_draft,))
    assert _violation(exc) == SV_FROZEN
    real_draft = seed.tax_rule(ws, status="draft", is_fixture=False)
    with pytest.raises(psycopg.Error) as exc:
        seed.conn.execute("update app.tax_rule_sets set is_fixture = true where id = %s", (real_draft,))
    assert _violation(exc) == SV_FROZEN
    # Draft content itself remains editable.
    seed.conn.execute(
        "update app.tax_rule_sets set rules = '{\"synthetic\": 2}' where id = %s", (fixture_draft,)
    )


# ---------------------------------------------------------------------------------------
# Source activation gate parity with domain.sources.activation_problems()
# ---------------------------------------------------------------------------------------

_READY: dict[str, Any] = {
    "adapter_version": "1.0.0",
    "technical_status": "fixture_tested",
    "terms_status": "permitted",
    "terms_decision": "proceed_permitted",
    "terms_decision_actor": "owner (synthetic)",
    "allowed_hosts": ["synthetic-dealer.example"],
    "allowed_search_paths": ["^/search$"],
}


def test_card_only_source_needs_no_detail_paths(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    seed.source(ws, enabled=True, detail_mode="card_only", allowed_detail_paths=[], **_READY)
    with pytest.raises(errors.CheckViolation) as exc:
        seed.source(ws, enabled=True, detail_mode="fetch", allowed_detail_paths=[], **_READY)
    assert _check_name(exc) == "sources_enable_gate_ck"
    with pytest.raises(errors.CheckViolation) as exc:
        seed.source(ws, detail_mode="browse_everything")
    assert _check_name(exc) == "sources_detail_mode_ck"
    assert (
        seed.scalar(
            "select column_default from information_schema.columns where table_schema = 'app'"
            " and table_name = 'sources' and column_name = 'detail_mode'"
        )
        == "'fetch'::text"
    )


# ---------------------------------------------------------------------------------------
# Parent consistency: runs belong to the same source; successors to the same listing
# ---------------------------------------------------------------------------------------


def test_crawl_run_references_are_source_consistent(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    other_source = seed.source(ws)
    foreign_run = seed.crawl_run(ws, other_source)
    own_run = seed.crawl_run(ws, world_a.source_id)
    card: dict[str, Any] = {
        "workspace_id": ws,
        "source_id": world_a.source_id,
        "listing_id": world_a.listing_id,
        "position": 0,
        "source_listing_id": "TEST-204",
        "card_hash": sha("card"),
        "card_material": {"synthetic": True},
        "observed_at": T0,
    }
    with pytest.raises(errors.ForeignKeyViolation):
        seed.insert(
            "app.listing_observations", crawl_run_id=foreign_run, ingestion_key=sha(unique("k")), **card
        )
    seed.insert("app.listing_observations", crawl_run_id=own_run, ingestion_key=sha(unique("k")), **card)
    attempt: dict[str, Any] = {
        "workspace_id": ws,
        "source_id": world_a.source_id,
        "purpose": "search",
        "url_hash": sha("u"),
        "host": "synthetic-dealer.example",
        "success": True,
        "access_state": "ok",
        "fetched_at": T0,
    }
    with pytest.raises(errors.ForeignKeyViolation):
        seed.insert("ops.fetch_attempts", crawl_run_id=foreign_run, **attempt)
    seed.insert("ops.fetch_attempts", crawl_run_id=own_run, **attempt)
    schedule: dict[str, Any] = {
        "workspace_id": ws,
        "source_id": world_a.source_id,
        "profile_id": world_a.profile_id,
        "next_due_at": T0,
        "coverage_mode": "rolling_pages",
    }
    with pytest.raises(errors.ForeignKeyViolation):
        seed.insert("ops.source_schedules", run_id=foreign_run, **schedule)
    seed.insert("ops.source_schedules", run_id=own_run, **schedule)


def test_successor_case_must_belong_to_the_same_listing(seed: Seed, world_a: World) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    old_case = seed.review_case(ws, listing, rev)
    other_listing = seed.listing(ws, world_a.source_id)
    _, gen, obs = seed.detail_observation(ws, other_listing)
    other_rev = seed.revision(ws, other_listing, 1, detail_generation=gen, observation_id=obs)
    foreign_case = seed.review_case(ws, other_listing, other_rev)
    with pytest.raises(errors.ForeignKeyViolation):
        seed.conn.execute(
            "update app.review_cases set state = 'superseded', superseded_by_id = %s where id = %s",
            (foreign_case, old_case),
        )
    with seed.conn.transaction():
        seed.conn.execute("update app.review_cases set state = 'superseded' where id = %s", (old_case,))
        successor = seed.review_case(ws, listing, rev)
        seed.conn.execute(
            "update app.review_cases set superseded_by_id = %s where id = %s", (successor, old_case)
        )


# ---------------------------------------------------------------------------------------
# Event subscriptions and deliveries (spec 22)
# ---------------------------------------------------------------------------------------


def _subscription(seed: Seed, workspace_id: UUID, **cols: Any) -> UUID:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "principal_id": uuid.uuid4(),
        "event_name": "review.pending.v1",
        "filter_hash": sha(unique("filter")),
        "callback_url": "https://callbacks.example/hooks/synthetic",
        "encrypted_secret": b"synthetic-ciphertext-0123456789",
        "created_at": T0,
        "expires_at": T0 + timedelta(days=30),
    }
    values.update(cols)
    return seed.insert_id("ops.event_subscriptions", **values)


def _credential(seed: Seed, workspace_id: UUID) -> UUID:
    return seed.insert_id(
        "ops.api_credentials",
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="mcp_client",
        role="reviewer",
        credential_kind="dev_local",
        token_hash=sha(unique("token")),
        scopes=["reviews:read", "events:subscribe"],
        label="Synthetic local credential",
        created_at=T0,
        expires_at=T0 + timedelta(days=30),
    )


def test_event_subscription_credential_is_same_workspace_and_rotation_is_paired(
    seed: Seed, world_a: World, world_b: World
) -> None:
    own = _credential(seed, world_a.workspace_id)
    foreign = _credential(seed, world_b.workspace_id)
    _subscription(seed, world_a.workspace_id, credential_id=own)
    with pytest.raises(errors.ForeignKeyViolation):
        _subscription(seed, world_a.workspace_id, credential_id=foreign)
    with pytest.raises(errors.ForeignKeyViolation):
        _subscription(seed, world_a.workspace_id, credential_id=uuid.uuid4())
    _subscription(
        seed,
        world_a.workspace_id,
        previous_encrypted_secret=b"synthetic-previous-ciphertext-01",
        previous_secret_valid_until=T0 + timedelta(hours=1),
    )
    for cols in (
        {"previous_encrypted_secret": b"synthetic-previous-ciphertext-01"},
        {"previous_secret_valid_until": T0 + timedelta(hours=1)},
        {"previous_encrypted_secret": b"short", "previous_secret_valid_until": T0},
    ):
        with pytest.raises(errors.CheckViolation) as exc:
            _subscription(seed, world_a.workspace_id, **cols)
        assert _check_name(exc) == "event_subscriptions_previous_secret_ck"


@pytest.mark.parametrize("state", ["pending", "retry_wait"])
def test_waiting_event_delivery_carries_no_lease(seed: Seed, world_a: World, state: str) -> None:
    ws = world_a.workspace_id
    sub = _subscription(seed, ws)
    event_id = uuid.uuid4()
    seed.outbox(ws, event_id=event_id)
    with pytest.raises(errors.CheckViolation) as exc:
        seed.insert(
            "ops.event_deliveries",
            workspace_id=ws,
            subscription_id=sub,
            event_id=event_id,
            state=state,
            lease_token=uuid.uuid4(),
        )
    assert _check_name(exc) == "event_deliveries_waiting_no_lease_ck"
    delivery = seed.insert_id(
        "ops.event_deliveries", workspace_id=ws, subscription_id=sub, event_id=event_id, state=state
    )
    # Claim -> sending with a lease; the reaper must clear it when returning to retry_wait.
    seed.conn.execute(
        "update ops.event_deliveries set state = 'sending', lease_owner = 'bridge-1', lease_token = %s,"
        " lease_expires_at = now() + interval '1 minute', attempts = 1 where id = %s",
        (uuid.uuid4(), delivery),
    )
    with pytest.raises(errors.CheckViolation) as exc:
        seed.conn.execute("update ops.event_deliveries set state = 'retry_wait' where id = %s", (delivery,))
    assert _check_name(exc) == "event_deliveries_waiting_no_lease_ck"
    seed.conn.execute(
        "update ops.event_deliveries set state = 'retry_wait', lease_owner = null, lease_token = null,"
        " lease_expires_at = null where id = %s",
        (delivery,),
    )


@pytest.mark.parametrize(
    ("role", "scopes", "ok"),
    [
        ("viewer", ["deals:read", "reviews:read"], True),
        ("viewer", ["reviews:write"], False),
        ("viewer", ["notes:write"], False),
        ("reviewer", ["reviews:write", "events:subscribe", "rechecks:request", "notes:write"], True),
        ("reviewer", ["sources:pause"], False),
        ("reviewer", ["deals:read", "config:admin"], False),
        ("owner", ["sources:pause", "config:admin"], True),
    ],
)
def test_credential_scopes_never_exceed_the_member_role(
    seed: Seed, world_a: World, role: str, scopes: list[str], ok: bool
) -> None:
    """Mirrors domain.actor.ROLE_SCOPES: a credential narrows, never widens, its role (spec 20)."""

    def insert() -> None:
        seed.insert(
            "ops.api_credentials",
            workspace_id=world_a.workspace_id,
            principal_id=uuid.uuid4(),
            principal_kind="mcp_client",
            role=role,
            credential_kind="static_bearer",
            token_hash=sha(unique("token")),
            scopes=scopes,
            label="Synthetic scoped credential",
            created_at=T0,
            expires_at=T0 + timedelta(days=30),
        )

    if ok:
        insert()
    else:
        with pytest.raises(errors.CheckViolation) as exc:
            insert()
        assert _check_name(exc) == "api_credentials_role_scopes_ck"
