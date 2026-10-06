"""Constraint and guard tests (spec sections 9, 10, 11, 13, 14, 16, 18, 22, 31).

Run as the superuser test role: composite foreign keys and checks must hold
even for privileged connections that bypass RLS. All data is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import (
    SV_FROZEN,
    SV_GENERATION,
    SV_MONOTONIC,
    SV_REFERENCE,
    SV_TRANSITION,
    T0,
    Seed,
    World,
    sha,
    unique,
)

pytestmark = pytest.mark.db


def _sqlstate(exc: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return exc.value.sqlstate


@contextmanager
def violates(*constraints: str) -> Iterator[None]:
    """Expect a CHECK violation of exactly one of the named constraints.

    PostgreSQL evaluates CHECK constraints in alphabetical order by name, so a
    row breaking several rules reports the first; tests name the rule they target.
    """
    with pytest.raises(errors.CheckViolation) as exc:
        yield
    assert exc.value.diag.constraint_name in constraints, exc.value.diag.constraint_name


# ---------------------------------------------------------------------------------------
# Cross-workspace composite foreign keys (spec 11, ADR 0001 consequences)
# ---------------------------------------------------------------------------------------


def test_listing_cannot_reference_source_of_another_workspace(
    seed: Seed, world_a: World, world_b: World
) -> None:
    with pytest.raises(errors.ForeignKeyViolation):
        seed.listing(world_a.workspace_id, world_b.source_id)


def test_revision_cannot_reference_listing_of_another_workspace(
    seed: Seed, world_a: World, world_b: World
) -> None:
    with pytest.raises(errors.ForeignKeyViolation):
        seed.revision(world_a.workspace_id, world_b.listing_id, 2)


def test_current_revision_must_belong_to_the_same_listing(seed: Seed, world_a: World) -> None:
    other_listing = seed.listing(world_a.workspace_id, world_a.source_id)
    with pytest.raises(errors.ForeignKeyViolation):
        seed.conn.execute(
            "update app.listings set current_revision_id = %s where id = %s",
            (world_a.revision_id, other_listing),
        )


def test_current_revision_fk_is_checked_at_commit(seed: Seed, world_a: World) -> None:
    """Deferred: a revision and its promotion may be written in either order in one transaction."""
    revision_id = uuid.uuid4()
    with seed.conn.transaction():
        seed.conn.execute(
            "update app.listings set current_revision_id = %s where id = %s",
            (revision_id, world_a.listing_id),
        )
        seed.revision(world_a.workspace_id, world_a.listing_id, 2, id=revision_id)
    assert seed.scalar(
        "select current_revision_id from app.listings where id = %s", (world_a.listing_id,)
    ) == (revision_id)


@pytest.mark.parametrize(
    "case",
    [
        "detail_observation_listing",
        "job_source",
        "review_case_revision_of_other_listing",
        "decision_listing_mismatch",
        "valuation_fx_of_other_workspace",
        "event_delivery_event_of_other_workspace",
        "alias_source_mismatch",
    ],
)
def test_other_cross_workspace_and_cross_parent_links_are_rejected(
    seed: Seed, world_a: World, world_b: World, case: str
) -> None:
    ws_a = world_a.workspace_id
    if case == "detail_observation_listing":
        with pytest.raises(errors.ForeignKeyViolation):
            seed.detail_observation(ws_a, world_b.listing_id, generation=1)
    elif case == "job_source":
        with pytest.raises(errors.ForeignKeyViolation):
            seed.job(ws_a, source_id=world_b.source_id)
    elif case == "review_case_revision_of_other_listing":
        other = seed.listing(ws_a, world_a.source_id)
        with pytest.raises(errors.ForeignKeyViolation):
            seed.review_case(ws_a, other, world_a.revision_id)
    elif case == "decision_listing_mismatch":
        case_id = seed.review_case(ws_a, world_a.listing_id, world_a.revision_id)
        other = seed.listing(ws_a, world_a.source_id)
        _, gen, obs = seed.detail_observation(ws_a, other)
        other_rev = seed.revision(ws_a, other, 1, detail_generation=gen, observation_id=obs)
        with pytest.raises(errors.ForeignKeyViolation):
            seed.decision(ws_a, case_id, other, other_rev)
    elif case == "valuation_fx_of_other_workspace":
        fx_b = seed.fx_rate(world_b.workspace_id)
        with pytest.raises(psycopg.Error) as exc:
            seed.valuation(ws_a, world_a.listing_id, world_a.revision_id, fx_rate_ids=[fx_b])
        assert _sqlstate(exc) == SV_REFERENCE
    elif case == "event_delivery_event_of_other_workspace":
        event_id = uuid.uuid4()
        seed.outbox(world_b.workspace_id, event_id=event_id, state="blocked", blocker_code="test_only")
        sub = seed.insert(
            "ops.event_subscriptions",
            workspace_id=ws_a,
            principal_id=uuid.uuid4(),
            event_name="review.pending.v1",
            filter_hash=sha(unique("filter")),
            callback_url="https://callbacks.example/hook",
            encrypted_secret=b"synthetic-ciphertext-0123456789",
            created_at=T0,
            expires_at=T0 + timedelta(days=365),
        )
        with pytest.raises(errors.ForeignKeyViolation):
            seed.insert("ops.event_deliveries", workspace_id=ws_a, subscription_id=sub, event_id=event_id)
    elif case == "alias_source_mismatch":
        other_source = seed.source(ws_a)
        with pytest.raises(errors.ForeignKeyViolation):
            seed.insert(
                "app.listing_aliases",
                workspace_id=ws_a,
                source_id=other_source,
                listing_id=world_a.listing_id,
                alias_url="https://synthetic-dealer.example/old/1",
                alias_hash=sha(unique("alias")),
                reason="synthetic url change",
            )


# ---------------------------------------------------------------------------------------
# Money: currency pairing, ranges, unknown-not-zero
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("asking", "currency"), [(275000, None), (None, "EUR")])
def test_revision_amount_and_currency_are_paired(
    seed: Seed, world_a: World, asking: int | None, currency: str | None
) -> None:
    with violates("listing_revisions_currency_pair_ck"):
        seed.revision(world_a.workspace_id, world_a.listing_id, 5, asking_minor=asking, currency=currency)


def test_revision_without_any_price_is_allowed(seed: Seed, world_a: World) -> None:
    seed.revision(world_a.workspace_id, world_a.listing_id, 6, asking_minor=None, currency=None)


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"amount_minor": 900000, "currency": None}, "market_observations_currency_pair_ck"),
        (
            {"evidence_kind": "seller_reported_sale", "amount_minor": None, "currency": "EUR"},
            "market_observations_currency_pair_ck",
        ),
        (
            {"evidence_kind": "asking_price", "amount_minor": None, "currency": None},
            "market_observations_amount_required_ck",
        ),
        ({"evidence_kind": "verified_sale", "evidence": {}}, "market_observations_verified_sale_ck"),
        ({"evidence_kind": "owner_estimate", "recorded_by": None}, "market_observations_owner_estimate_ck"),
        ({"evidence_kind": "asking_price", "url": None}, "market_observations_public_source_ck"),
        ({"evidence_kind": "realized_guess"}, "market_observations_kind_ck"),
        ({"local_registration_status": "maybe"}, "market_observations_registration_status_ck"),
    ],
)
def test_market_observation_rules(seed: Seed, world_a: World, cols: dict[str, Any], constraint: str) -> None:
    with violates(constraint):
        seed.market_observation(world_a.workspace_id, **cols)


def test_market_sale_claim_without_amount_is_allowed(seed: Seed, world_a: World) -> None:
    seed.market_observation(
        world_a.workspace_id, evidence_kind="seller_reported_sale", amount_minor=None, currency=None
    )


@pytest.mark.parametrize(
    ("low", "base", "high", "ok"),
    [
        (60000, 70000, 80000, True),
        (70000, 70000, 70000, True),
        (None, 70000, None, True),
        (60000, None, 80000, True),
        (80000, 70000, 90000, False),
        (60000, 90000, 80000, False),
        (90000, None, 80000, False),
    ],
)
def test_cost_evidence_low_base_high(
    seed: Seed, world_a: World, low: int | None, base: int | None, high: int | None, ok: bool
) -> None:
    def insert() -> None:
        seed.cost_evidence(world_a.workspace_id, low_minor=low, base_minor=base, high_minor=high)

    if ok:
        insert()
    else:
        with violates("cost_evidence_range_ck"):
            insert()


def test_cost_evidence_currency_pairing_and_quote_provider(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    with violates("cost_evidence_currency_pair_ck"):
        seed.cost_evidence(ws, currency=None)
    with violates("cost_evidence_has_amount_ck"):
        seed.cost_evidence(ws, base_minor=None, currency=None)  # no amount at all
    with violates("cost_evidence_quote_provider_ck"):
        seed.cost_evidence(ws, kind="quote", provider=None)
    with violates("cost_evidence_expiry_ck"):
        seed.cost_evidence(ws, obtained_at=T0, expires_at=T0)
    with violates("cost_evidence_amounts_ck"):
        seed.cost_evidence(ws, base_minor=-1)


def test_valuation_unknown_is_never_zero(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    with violates("valuations_unknown_not_zero_ck"):
        seed.valuation(
            ws, world_a.listing_id, world_a.revision_id, state="incomplete", base_contribution_minor=0
        )
    with violates("valuations_unknown_not_zero_ck"):
        seed.valuation(
            ws, world_a.listing_id, world_a.revision_id, state="not_started", upside_contribution_minor=1
        )
    with violates("valuations_complete_figures_ck"):
        seed.valuation(
            ws, world_a.listing_id, world_a.revision_id, state="estimated", base_contribution_minor=1
        )


def test_estimated_valuation_requires_approved_tax_rules(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    unapproved = seed.tax_rule(ws, is_fixture=False, status="draft")
    with pytest.raises(psycopg.Error) as exc:
        seed.valuation(
            ws,
            world_a.listing_id,
            world_a.revision_id,
            is_fixture=False,
            state="estimated",
            tax_rule_set_id=unapproved,
            base_contribution_minor=130000,
            conservative_contribution_minor=50000,
        )
    assert _sqlstate(exc) == SV_REFERENCE
    approved = seed.approved_tax_rule(ws)
    seed.valuation(
        ws,
        world_a.listing_id,
        world_a.revision_id,
        is_fixture=False,
        state="estimated",
        tax_rule_set_id=approved,
        base_contribution_minor=130000,
        conservative_contribution_minor=-20000,
    )


def test_real_valuation_cannot_depend_on_fixture_data(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    fixture_fx = seed.fx_rate(ws, is_fixture=True)
    with pytest.raises(psycopg.Error) as exc:
        seed.valuation(
            ws, world_a.listing_id, world_a.revision_id, is_fixture=False, fx_rate_ids=[fixture_fx]
        )
    assert _sqlstate(exc) == SV_REFERENCE
    real_fx = seed.fx_rate(ws, is_fixture=False)
    seed.valuation(ws, world_a.listing_id, world_a.revision_id, is_fixture=False, fx_rate_ids=[real_fx])


def test_valuation_state_moves_only_to_stale_or_invalid(seed: Seed, world_a: World) -> None:
    val = seed.valuation(world_a.workspace_id, world_a.listing_id, world_a.revision_id)
    conn = seed.conn
    with violates("valuations_stale_ck"):
        conn.execute("update app.valuations set state = 'stale' where id = %s", (val,))
    conn.execute(
        "update app.valuations set state = 'stale', stale_at = now(), stale_reason = 'fx changed'"
        " where id = %s",
        (val,),
    )
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.valuations set state = 'incomplete' where id = %s", (val,))
    assert _sqlstate(exc) == SV_TRANSITION
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.valuations set scenarios = '{\"x\": 1}' where id = %s", (val,))
    assert _sqlstate(exc) == SV_FROZEN
    conn.execute("update app.valuations set state = 'invalid' where id = %s", (val,))
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.valuations set state = 'stale' where id = %s", (val,))
    assert _sqlstate(exc) == SV_TRANSITION


def test_fx_rate_is_positive_and_directional(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    with violates("fx_rates_rate_ck"):
        seed.fx_rate(ws, rate="0")
    with violates("fx_rates_rate_ck"):
        seed.fx_rate(ws, rate="-1.5")
    with violates("fx_rates_currency_ck"):
        seed.fx_rate(ws, base="EUR", quote="EUR")
    with violates("fx_rates_purpose_ck"):
        seed.fx_rate(ws, purpose="parity_assumption")
    provider = unique("ecb_synthetic")
    seed.fx_rate(ws, provider=provider)
    with pytest.raises(errors.UniqueViolation):
        seed.fx_rate(ws, provider=provider)
    # The opposite direction and another purpose are distinct observations.
    seed.fx_rate(ws, provider=provider, base="CHF", quote="EUR", rate="1.0738831615")
    seed.fx_rate(ws, provider=provider, purpose="customs")


# ---------------------------------------------------------------------------------------
# Tax rule sets (spec 16)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["approved_by", "sources", "sha256", "valid_from", "currency"])
def test_approved_tax_rules_require_evidence(seed: Seed, world_a: World, missing: str) -> None:
    overrides: dict[str, Any] = {missing: Jsonb([]) if missing == "sources" else None}
    if missing == "approved_by":
        overrides["approved_at"] = None
    with violates("tax_rule_sets_approved_ck"):
        seed.approved_tax_rule(world_a.workspace_id, **overrides)


@pytest.mark.parametrize(
    ("valid_from", "valid_to"),
    [
        (date(2026, 10, 1), date(2026, 10, 1)),
        (date(2026, 10, 2), date(2026, 10, 1)),
        (None, date(2026, 10, 1)),
    ],
)
def test_tax_rule_valid_to_after_valid_from(
    seed: Seed, world_a: World, valid_from: date | None, valid_to: date
) -> None:
    with violates("tax_rule_sets_validity_ck"):
        seed.tax_rule(world_a.workspace_id, valid_from=valid_from, valid_to=valid_to)


def test_fixture_tax_rules_can_never_be_approved(seed: Seed, world_a: World) -> None:
    with violates("tax_rule_sets_fixture_ck"):
        seed.approved_tax_rule(world_a.workspace_id, is_fixture=True)
    with violates("tax_rule_sets_fixture_ck"):
        seed.approved_tax_rule(world_a.workspace_id, is_fixture=True, status="active")


def test_overlapping_active_tax_rules_are_rejected(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    seed.approved_tax_rule(ws, status="active", valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1))
    with pytest.raises(errors.ExclusionViolation):
        seed.approved_tax_rule(ws, status="active", valid_from=date(2026, 6, 1), valid_to=None)
    # Adjacent ([) ranges) and other categories do not overlap.
    seed.approved_tax_rule(ws, status="active", valid_from=date(2027, 1, 1), valid_to=None)
    seed.approved_tax_rule(ws, status="active", vehicle_category="light_truck", valid_from=date(2026, 6, 1))


def test_tax_rule_lifecycle_and_frozen_content(seed: Seed, world_a: World) -> None:
    conn = seed.conn
    rule = seed.tax_rule(world_a.workspace_id, is_fixture=False, status="draft")
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.tax_rule_sets set status = 'active' where id = %s", (rule,))
    assert _sqlstate(exc) == SV_TRANSITION
    conn.execute("update app.tax_rule_sets set rules = '{\"draft\": 2}' where id = %s", (rule,))
    conn.execute("update app.tax_rule_sets set status = 'under_review' where id = %s", (rule,))
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.tax_rule_sets set rules = '{\"sneaky\": 1}' where id = %s", (rule,))
    assert _sqlstate(exc) == SV_FROZEN
    conn.execute("update app.tax_rule_sets set status = 'revoked' where id = %s", (rule,))
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.tax_rule_sets set status = 'draft' where id = %s", (rule,))
    assert _sqlstate(exc) == SV_TRANSITION


def test_active_tax_rule_valid_to_may_only_shorten(seed: Seed, world_a: World) -> None:
    conn = seed.conn
    rule = seed.approved_tax_rule(
        world_a.workspace_id,
        status="active",
        vehicle_category=unique("cat").lower(),
        valid_from=date(2026, 1, 1),
    )
    conn.execute("update app.tax_rule_sets set valid_to = '2027-01-01' where id = %s", (rule,))
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.tax_rule_sets set valid_to = '2028-01-01' where id = %s", (rule,))
    assert _sqlstate(exc) == SV_FROZEN
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.tax_rule_sets set approved_by = %s where id = %s", (uuid.uuid4(), rule))
    assert _sqlstate(exc) == SV_FROZEN
    conn.execute("update app.tax_rule_sets set status = 'superseded' where id = %s", (rule,))


# ---------------------------------------------------------------------------------------
# Jobs (spec 13)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"state": "running"}, "jobs_running_lease_ck"),
        ({"state": "running", "lease_owner": "w1", "lease_token": uuid.uuid4()}, "jobs_running_lease_ck"),
        ({"state": "running", "lease_token": uuid.uuid4(), "lease_expires_at": T0}, "jobs_running_lease_ck"),
        ({"attempts": -1}, "jobs_attempts_ck"),
        ({"attempts": 6, "max_attempts": 5}, "jobs_attempts_ck"),
        ({"max_attempts": 0}, "jobs_max_attempts_ck"),
        ({"max_attempts": 51}, "jobs_max_attempts_ck"),
        ({"state": "blocked"}, "jobs_blocked_ck"),
        (
            {"state": "queued", "lease_token": uuid.uuid4(), "lease_expires_at": T0},
            "jobs_waiting_no_lease_ck",
        ),
        ({"state": "retry_wait", "lease_token": uuid.uuid4()}, "jobs_waiting_no_lease_ck"),
        ({"state": "succeeded"}, "jobs_completed_at_ck"),
        ({"state": "queued", "completed_at": T0}, "jobs_completed_at_ck"),
        ({"job_type": "detail"}, "jobs_detail_binding_ck"),
        ({"job_type": "recheck"}, "jobs_detail_binding_ck"),
        ({"job_type": "crawl_url"}, "jobs_type_ck"),
        # Unknown states also fail the completed_at rule, which sorts first.
        ({"state": "paused"}, "jobs_completed_at_ck"),
        ({"payload": "array"}, "jobs_payload_ck"),
        ({"dedup_key": ""}, "jobs_dedup_key_ck"),
        ({"priority": 1001}, "jobs_priority_ck"),
        ({"last_error_code": "boom with spaces"}, "jobs_error_code_ck"),
    ],
)
def test_job_invariants(seed: Seed, world_a: World, cols: dict[str, Any], constraint: str) -> None:
    if cols.get("payload") == "array":
        cols = {"payload": Jsonb([])}
    with violates(constraint):
        seed.job(world_a.workspace_id, **cols)


def test_valid_job_shapes(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    seed.job(
        ws, state="running", lease_owner="worker-1", lease_token=uuid.uuid4(), lease_expires_at=T0, attempts=1
    )
    seed.job(ws, state="blocked", blocker_code="SOURCE_PAUSED")
    seed.job(ws, state="succeeded", completed_at=T0, lease_owner="worker-1")
    seed.job(ws, state="dead_letter", completed_at=T0, attempts=5, max_attempts=5)
    gen = seed.allocate_generation(ws, world_a.listing_id)
    seed.job(
        ws, job_type="detail", listing_id=world_a.listing_id, generation=gen, source_id=world_a.source_id
    )


def test_scheduler_slot_is_unique_forever(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    slot = T0 + timedelta(minutes=15)
    common: dict[str, Any] = {
        "job_type": "discovery",
        "source_id": world_a.source_id,
        "profile_id": world_a.profile_id,
        "partition_key": "default",
        "scheduled_slot": slot,
    }
    first = seed.job(ws, dedup_key=unique("discovery"), **common)
    with pytest.raises(errors.UniqueViolation):
        seed.job(ws, dedup_key=unique("discovery"), **common)
    # Even after the first job finished, the same slot cannot be scheduled again.
    seed.conn.execute("update ops.jobs set state = 'succeeded', completed_at = now() where id = %s", (first,))
    with pytest.raises(errors.UniqueViolation):
        seed.job(ws, dedup_key=unique("discovery"), **common)
    # ON CONFLICT DO NOTHING is the scheduler's idempotent insert.
    row = seed.conn.execute(
        "insert into ops.jobs (workspace_id, job_type, dedup_key, source_id, profile_id, partition_key,"
        " scheduled_slot) values (%s, 'discovery', %s, %s, %s, 'default', %s)"
        " on conflict do nothing returning id",
        (ws, unique("discovery"), world_a.source_id, world_a.profile_id, slot),
    ).fetchone()
    assert row is None
    # The next slot is a different job.
    seed.job(ws, dedup_key=unique("discovery"), **{**common, "scheduled_slot": slot + timedelta(minutes=15)})
    with violates("jobs_slot_ck"):
        seed.job(ws, job_type="discovery", scheduled_slot=slot)


def test_dedup_key_is_unique_only_while_live(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    key = unique("valuation:listing")
    first = seed.job(ws, dedup_key=key)
    with pytest.raises(errors.UniqueViolation):
        seed.job(ws, dedup_key=key)
    seed.conn.execute("update ops.jobs set state = 'cancelled', completed_at = now() where id = %s", (first,))
    seed.job(ws, dedup_key=key)


# ---------------------------------------------------------------------------------------
# Reviews (spec 14, 21)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"state": "claimed"}, "review_cases_claimed_ck"),
        (
            {"state": "claimed", "claim_holder": uuid.uuid4(), "claim_token_hash": "a" * 64},
            "review_cases_claimed_ck",
        ),
        (
            {"state": "claimed", "claim_holder": uuid.uuid4(), "claim_expires_at": T0},
            "review_cases_claimed_ck",
        ),
        (
            {"state": "claimed", "claim_token_hash": "a" * 64, "claim_expires_at": T0},
            "review_cases_claimed_ck",
        ),
        ({"state": "pending", "claim_holder": uuid.uuid4()}, "review_cases_unclaimed_ck"),
        ({"state": "pending", "claim_expires_at": T0}, "review_cases_unclaimed_ck"),
        ({"state": "shortlisted"}, "review_cases_decided_ck"),
        ({"state": "rejected"}, "review_cases_decided_ck"),
        ({"state": "watch"}, "review_cases_decided_ck"),
        ({"state": "needs_information"}, "review_cases_decided_ck"),
        (
            {
                "state": "claimed",
                "claim_holder": uuid.uuid4(),
                "claim_token_hash": "not-a-hash",
                "claim_expires_at": T0,
            },
            "review_cases_claim_hash_ck",
        ),
        (
            {
                "state": "claimed",
                "claim_holder": uuid.uuid4(),
                "claim_token_hash": "a" * 64,
                "claimed_at": T0,
                "claim_expires_at": T0,
            },
            "review_cases_claim_window_ck",
        ),
        ({"state": "approved"}, "review_cases_state_ck"),
        ({"readiness": "Needs Import Costs!"}, "review_cases_readiness_ck"),
    ],
)
def test_review_case_claim_and_decision_invariants(
    seed: Seed, world_a: World, cols: dict[str, Any], constraint: str
) -> None:
    with violates(constraint):
        seed.review_case(world_a.workspace_id, world_a.listing_id, world_a.revision_id, **cols)


def test_review_claim_submit_flow_and_one_open_case(seed: Seed, world_a: World) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    conn = seed.conn
    case_id = seed.review_case(ws, listing, rev)
    with pytest.raises(errors.UniqueViolation):
        seed.review_case(ws, listing, rev)
    conn.execute(
        "update app.review_cases set state = 'claimed', claim_holder = %s, claim_token_hash = %s,"
        " claimed_at = now(), claim_expires_at = now() + interval '5 minutes' where id = %s",
        (uuid.uuid4(), sha("synthetic-claim-token"), case_id),
    )
    # Submit: decision + case update in one transaction (deferred latest_decision FK).
    with conn.transaction():
        decision = seed.decision(ws, case_id, listing, rev, outcome="shortlisted", case_version=1)
        conn.execute(
            "update app.review_cases set state = 'shortlisted', latest_decision_id = %s, row_version = 2,"
            " claim_holder = null, claim_token_hash = null, claimed_at = null, claim_expires_at = null"
            " where id = %s",
            (decision, case_id),
        )
    # A second decision against the same case version is rejected (timeout retry safety).
    with pytest.raises(errors.UniqueViolation):
        seed.decision(ws, case_id, listing, rev, case_version=1)
    # The latest decision must belong to this case.
    other_case_listing = seed.listing(ws, world_a.source_id)
    _, gen, obs = seed.detail_observation(ws, other_case_listing)
    other_rev = seed.revision(ws, other_case_listing, 1, detail_generation=gen, observation_id=obs)
    other_case = seed.review_case(ws, other_case_listing, other_rev)
    with pytest.raises(errors.ForeignKeyViolation):
        conn.execute(
            "update app.review_cases set latest_decision_id = %s, state = 'watch' where id = %s",
            (decision, other_case),
        )
    # Case versions never decrease.
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.review_cases set row_version = 1 where id = %s", (case_id,))
    assert _sqlstate(exc) == SV_MONOTONIC
    # Superseding frees the (listing, profile) slot for a new case.
    conn.execute("update app.review_cases set state = 'superseded' where id = %s", (case_id,))
    seed.review_case(ws, listing, rev)


def test_review_decision_field_bounds(seed: Seed, world_a: World) -> None:
    ws, listing, rev = world_a.workspace_id, world_a.listing_id, world_a.revision_id
    case_id = seed.review_case(ws, listing, rev, profile_key="primary")
    cases: list[tuple[dict[str, Any], str]] = [
        ({"reason_codes": []}, "review_decisions_reason_codes_ck"),
        ({"reason_codes": ["x" * 81]}, "review_decisions_reason_codes_ck"),
        ({"reason_codes": [f"R{i}" for i in range(21)]}, "review_decisions_reason_codes_ck"),
        ({"summary": "too short"}, "review_decisions_summary_ck"),
        ({"summary": "s" * 4001}, "review_decisions_summary_ck"),
        ({"outcome": "approved_purchase"}, "review_decisions_outcome_ck"),
        ({"missing_information": ["y" * 301]}, "review_decisions_missing_information_ck"),
        ({"evidence_ids": [uuid.uuid4() for _ in range(101)]}, "review_decisions_evidence_ids_ck"),
        ({"actor_kind": "owner_impersonation"}, "review_decisions_actor_kind_ck"),
        ({"input_hash": "short"}, "review_decisions_input_hash_ck"),
    ]
    for cols, constraint in cases:
        with violates(constraint):
            seed.decision(ws, case_id, listing, rev, **cols)


# ---------------------------------------------------------------------------------------
# Outbox and events (spec 13, 22)
# ---------------------------------------------------------------------------------------


def test_outbox_dedup_key_is_unique_per_workspace(seed: Seed, world_a: World, world_b: World) -> None:
    key = f"review.pending:{uuid.uuid4()}:1"
    seed.outbox(world_a.workspace_id, dedup_key=key)
    with pytest.raises(errors.UniqueViolation):
        seed.outbox(world_a.workspace_id, dedup_key=key)
    seed.outbox(world_b.workspace_id, dedup_key=key)  # another workspace is independent
    event_id = uuid.uuid4()
    seed.outbox(world_a.workspace_id, event_id=event_id)
    with pytest.raises(errors.UniqueViolation):
        seed.outbox(world_b.workspace_id, event_id=event_id)


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"state": "sending"}, "outbox_sending_lease_ck"),
        (
            {"state": "sending", "lease_owner": "dispatcher-1", "lease_token": uuid.uuid4()},
            "outbox_sending_lease_ck",
        ),
        ({"state": "pending", "lease_token": uuid.uuid4()}, "outbox_waiting_no_lease_ck"),
        ({"state": "delivered", "send_attempted_at": T0}, "outbox_delivered_ck"),
        ({"state": "blocked"}, "outbox_blocked_ck"),
        ({"is_fixture": True}, "outbox_fixture_ck"),
        (
            {
                "is_fixture": True,
                "state": "sending",
                "lease_owner": "d",
                "lease_token": uuid.uuid4(),
                "lease_expires_at": T0,
            },
            "outbox_fixture_ck",
        ),
        ({"owner_seen_at": T0, "send_attempted_at": T0}, "outbox_seen_ck"),
        ({"provider_accepted_at": T0}, "outbox_accepted_ck"),
        ({"payload": {"blob": "x" * 270000}}, "outbox_payload_size_ck"),
        ({"payload_hash": "nothex"}, "outbox_payload_hash_ck"),
        ({"attempts": 11}, "outbox_attempts_ck"),
        ({"event_type": "Review Pending"}, "outbox_event_type_ck"),
        ({"state": "acknowledged"}, "outbox_state_ck"),
    ],
)
def test_outbox_invariants(seed: Seed, world_a: World, cols: dict[str, Any], constraint: str) -> None:
    with violates(constraint):
        seed.outbox(world_a.workspace_id, **cols)


def test_outbox_delivery_lifecycle_is_explicit(seed: Seed, world_a: World) -> None:
    """event_created_at, send_attempted_at, provider_accepted_at and owner_seen_at stay separate."""
    row = seed.outbox(world_a.workspace_id)
    conn = seed.conn
    conn.execute(
        "update ops.outbox set state = 'sending', lease_owner = 'dispatcher-1', lease_token = %s,"
        " lease_expires_at = now() + interval '1 minute', send_attempted_at = now(), attempts = 1"
        " where id = %s",
        (uuid.uuid4(), row),
    )
    conn.execute(
        "update ops.outbox set state = 'uncertain', lease_owner = null, lease_token = null,"
        " lease_expires_at = null, last_error_code = 'PROVIDER_TIMEOUT' where id = %s",
        (row,),
    )
    conn.execute(
        "update ops.outbox set state = 'delivered', provider_accepted_at = now(), completed_at = now()"
        " where id = %s",
        (row,),
    )
    assert seed.scalar("select owner_seen_at from ops.outbox where id = %s", (row,)) is None


def test_fixture_outbox_rows_are_blocked_or_cancelled(seed: Seed, world_a: World) -> None:
    row = seed.outbox(
        world_a.workspace_id, is_fixture=True, state="blocked", blocker_code="fixture_no_external"
    )
    with violates("outbox_fixture_ck"):
        seed.conn.execute(
            "update ops.outbox set state = 'pending', blocker_code = null where id = %s", (row,)
        )
    seed.conn.execute("update ops.outbox set state = 'cancelled' where id = %s", (row,))


def test_event_subscription_identity_and_callback_rules(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    principal = uuid.uuid4()
    base: dict[str, Any] = {
        "workspace_id": ws,
        "principal_id": principal,
        "event_name": "review.pending.v1",
        "filter_hash": sha("filter-primary"),
        "callback_url": "https://callbacks.example/hooks/synthetic",
        "encrypted_secret": b"synthetic-ciphertext-0123456789",
        "created_at": T0,
        "expires_at": T0 + timedelta(days=30),
    }
    seed.insert("ops.event_subscriptions", **base)
    with pytest.raises(errors.UniqueViolation):
        seed.insert("ops.event_subscriptions", **base)
    for bad in (
        "http://callbacks.example/hook",
        "https://user:secret@callbacks.example/hook",
        "https://callbacks.example/hook with space",
        "https://callbacks.example@evil.example/hook",
        "javascript:alert(1)",
    ):
        with violates("event_subscriptions_callback_ck"):
            seed.insert("ops.event_subscriptions", **{**base, "callback_url": bad, "filter_hash": sha(bad)})
    with violates("event_subscriptions_lifetime_ck"):
        seed.insert("ops.event_subscriptions", **{**base, "filter_hash": sha("past"), "expires_at": T0})
    with violates("event_subscriptions_verified_ck"):
        seed.insert(
            "ops.event_subscriptions",
            **{**base, "filter_hash": sha("verified"), "verification_state": "verified"},
        )


def test_idempotency_key_scoped_by_principal_and_operation(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    principal = uuid.uuid4()
    base: dict[str, Any] = {
        "workspace_id": ws,
        "principal_id": principal,
        "operation": "reviews_submit",
        "idempotency_key": "synthetic-key-0001",
        "request_hash": sha("request-1"),
        "created_at": T0,
        "expires_at": T0 + timedelta(days=1),
    }
    record = seed.insert("ops.idempotency_records", **base)
    with pytest.raises(errors.UniqueViolation):
        seed.insert("ops.idempotency_records", **{**base, "request_hash": sha("request-2")})
    seed.insert("ops.idempotency_records", **{**base, "operation": "reviews_claim"})
    seed.insert("ops.idempotency_records", **{**base, "principal_id": uuid.uuid4()})
    with violates("idempotency_records_completed_ck"):
        seed.conn.execute("update ops.idempotency_records set state = 'completed' where id = %s", (record,))
    with violates("idempotency_records_key_ck"):
        seed.insert("ops.idempotency_records", **{**base, "idempotency_key": "short"})
    seed.conn.execute(
        "update ops.idempotency_records set state = 'completed', result = '{\"decision_id\": \"x\"}',"
        " completed_at = now() where id = %s",
        (record,),
    )
    with pytest.raises(psycopg.Error) as exc:
        seed.conn.execute(
            "update ops.idempotency_records set request_hash = %s where id = %s", (sha("other"), record)
        )
    assert _sqlstate(exc) == SV_FROZEN


# ---------------------------------------------------------------------------------------
# Listings, revisions, generations (spec 10)
# ---------------------------------------------------------------------------------------


def test_revisions_allow_semantic_reversion_a_b_a(seed: Seed, world_a: World) -> None:
    ws, listing = world_a.workspace_id, world_a.listing_id
    hash_a, hash_b = sha("semantic-A"), sha("semantic-B")
    seed.revision(ws, listing, 2, semantic_hash=hash_a, asking_minor=275000)
    seed.revision(ws, listing, 3, semantic_hash=hash_b, asking_minor=265000)
    seed.revision(ws, listing, 4, semantic_hash=hash_a, asking_minor=275000)
    count = seed.scalar(
        "select count(*) from app.listing_revisions where listing_id = %s and semantic_hash = %s",
        (listing, hash_a),
    )
    assert count == 2
    with pytest.raises(errors.UniqueViolation):
        seed.revision(ws, listing, 4)


def test_listing_identity_and_monotonic_guards(seed: Seed, world_a: World) -> None:
    conn = seed.conn
    listing = world_a.listing_id
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.listings set source_listing_id = 'REUSED' where id = %s", (listing,))
    assert _sqlstate(exc) == SV_FROZEN
    with pytest.raises(psycopg.Error) as exc:
        conn.execute(
            "update app.listings set last_seen_at = last_seen_at - interval '1 minute' where id = %s",
            (listing,),
        )
    assert _sqlstate(exc) == SV_MONOTONIC
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("update app.listings set detail_generation = 0 where id = %s", (listing,))
    assert _sqlstate(exc) == SV_MONOTONIC
    # last_seen_at = greatest(existing, observed) is fine.
    conn.execute(
        "update app.listings set last_seen_at = greatest(last_seen_at, %s) where id = %s",
        (T0 + timedelta(hours=1), listing),
    )
    # A relisted vehicle with a reused ID is a new incarnation, never an in-place change.
    seed.listing(
        world_a.workspace_id,
        world_a.source_id,
        source_listing_id=seed.scalar("select source_listing_id from app.listings where id = %s", (listing,)),
        identity_hash=seed.scalar("select identity_hash from app.listings where id = %s", (listing,)),
        incarnation=2,
    )


def test_late_older_generation_cannot_regress_current(seed: Seed, world_a: World) -> None:
    ws, listing = world_a.workspace_id, world_a.listing_id
    conn = seed.conn
    gen_old = seed.allocate_generation(ws, listing)
    gen_new = seed.allocate_generation(ws, listing)
    _, _, obs_new = seed.detail_observation(ws, listing, generation=gen_new, promoted=True)
    rev_new = seed.revision(ws, listing, 2, detail_generation=gen_new, observation_id=obs_new)
    seed.promote(ws, listing, rev_new, gen_new, obs_new)
    # The older generation completes late: retained as history, not promoted.
    _, _, obs_old = seed.detail_observation(ws, listing, generation=gen_old, promoted=False)
    with pytest.raises(psycopg.Error) as exc:
        seed.promote(ws, listing, rev_new, gen_old, obs_old)
    assert _sqlstate(exc) == SV_MONOTONIC
    assert (
        seed.scalar(
            "select count(*) from app.detail_observations where listing_id = %s and generation = %s",
            (listing, gen_old),
        )
        == 1
    )
    # Same generation replay of the same observation is idempotent via the unique key.
    row = conn.execute(
        "insert into app.detail_observations (workspace_id, listing_id, generation, observation_id,"
        " semantic_hash, normalized, provenance, parser_version, observed_at)"
        " values (%s, %s, %s, %s, %s, '{}', '{}', 'fixture@1', %s) on conflict do nothing returning id",
        (ws, listing, gen_new, obs_new, sha("replay"), T0),
    ).fetchone()
    assert row is None


def test_detail_generation_must_be_allocated(seed: Seed, world_a: World) -> None:
    allocated = seed.scalar("select detail_generation from app.listings where id = %s", (world_a.listing_id,))
    with pytest.raises(psycopg.Error) as exc:
        seed.detail_observation(world_a.workspace_id, world_a.listing_id, generation=allocated + 1)
    assert _sqlstate(exc) == SV_GENERATION


def test_current_observation_must_exist_for_listing(seed: Seed, world_a: World) -> None:
    gen = seed.allocate_generation(world_a.workspace_id, world_a.listing_id)
    with pytest.raises(errors.ForeignKeyViolation):
        seed.conn.execute(
            "update app.listings set current_generation = %s, current_observation_id = %s where id = %s",
            (gen, uuid.uuid4(), world_a.listing_id),
        )


def test_listing_hash_collision_is_not_merged(seed: Seed, world_a: World) -> None:
    identity_hash = seed.scalar("select identity_hash from app.listings where id = %s", (world_a.listing_id,))
    with pytest.raises(errors.UniqueViolation):
        seed.listing(world_a.workspace_id, world_a.source_id, identity_hash=identity_hash)


def test_mileage_threshold_is_not_a_table_constraint(seed: Seed, world_a: World) -> None:
    """Rejected observations (>= 200,000 km) are retained for audit (spec 11)."""
    seed.revision(world_a.workspace_id, world_a.listing_id, 7, mileage_km="200000")
    seed.revision(world_a.workspace_id, world_a.listing_id, 8, mileage_km="250000.5")


def test_listing_observation_ingestion_key_prevents_replay(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    run = seed.crawl_run(ws, world_a.source_id)
    cols: dict[str, Any] = {
        "workspace_id": ws,
        "source_id": world_a.source_id,
        "listing_id": world_a.listing_id,
        "crawl_run_id": run,
        "position": 0,
        "source_listing_id": "TEST-204",
        "card_hash": sha("card"),
        "card_material": {"price": "2750", "synthetic": True},
        "card_price_minor": 275000,
        "card_currency": "EUR",
        "observed_at": T0,
        "ingestion_key": sha(f"{run}:1:TEST-204:card"),
    }
    seed.insert("app.listing_observations", **cols)
    with pytest.raises(errors.UniqueViolation):
        seed.insert("app.listing_observations", **cols)
    with violates("listing_observations_currency_pair_ck"):
        seed.insert("app.listing_observations", **{**cols, "ingestion_key": sha("x"), "card_currency": None})


# ---------------------------------------------------------------------------------------
# Configuration baseline and sources (spec 3, 5)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "cols", "constraint"),
    [
        ("primary", {"max_price_eur": "4000.00"}, "search_profiles_primary_baseline_ck"),
        ("primary", {"max_price_eur": "3000.01"}, "search_profiles_primary_baseline_ck"),
        ("primary", {"min_price_eur": "2000.00"}, "search_profiles_primary_baseline_ck"),
        ("primary", {"max_price_inclusive": False}, "search_profiles_primary_baseline_ck"),
        ("primary", {"enabled": False}, "search_profiles_primary_baseline_ck"),
        ("primary", {"max_mileage_km_exclusive": "199999"}, "search_profiles_primary_baseline_ck"),
        ("primary", {"max_mileage_km_exclusive": "200001"}, "search_profiles_mileage_ck"),
        ("manual_4000", {"max_price_eur": "4500.00"}, "search_profiles_manual_4000_ck"),
        ("manual_4000", {"max_mileage_km_exclusive": "250000"}, "search_profiles_mileage_ck"),
        ("below_target_watch", {"max_price_inclusive": True}, "search_profiles_below_target_ck"),
        ("below_target_watch", {"max_price_eur": "2600.00"}, "search_profiles_below_target_ck"),
        ("primary", {"min_price_eur": "3500.00"}, "search_profiles_price_ck"),
    ],
)
def test_search_profile_baseline(seed: Seed, key: str, cols: dict[str, Any], constraint: str) -> None:
    ws = seed.workspace("Profile baseline")
    cfg = seed.config_revision(ws)
    base: dict[str, dict[str, Any]] = {
        "primary": {"min_price_eur": "2500.00", "max_price_eur": "3000.00", "enabled": True},
        "manual_4000": {"min_price_eur": "2500.00", "max_price_eur": "4000.00"},
        "below_target_watch": {"max_price_eur": "2500.00", "max_price_inclusive": False},
    }
    with violates(constraint):
        seed.insert(
            "app.search_profiles",
            workspace_id=ws,
            profile_key=key,
            label=f"Synthetic {key}",
            queue_label=f"Synthetic {key} queue",
            config_revision_id=cfg,
            **{**base[key], **cols},
        )


def test_optional_profiles_use_a_different_queue(seed: Seed) -> None:
    ws = seed.workspace("Queue labels")
    cfg = seed.config_revision(ws)
    seed.profile(ws, "primary", cfg)
    with pytest.raises(errors.UniqueViolation):
        seed.insert(
            "app.search_profiles",
            workspace_id=ws,
            profile_key="manual_4000",
            label="Synthetic manual",
            queue_label="Primary queue",
            min_price_eur="2500.00",
            max_price_eur="4000.00",
            config_revision_id=cfg,
        )
    seed.profile(ws, "manual_4000", cfg)
    seed.profile(ws, "below_target_watch", cfg)


def test_source_cannot_be_enabled_without_activation_gates(seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    ready: dict[str, Any] = {
        "adapter_version": "1.0.0",
        "technical_status": "fixture_tested",
        "terms_status": "permitted",
        "terms_decision": "proceed_permitted",
        "terms_decision_actor": "owner (synthetic)",
        "allowed_hosts": ["synthetic-dealer.example"],
        "allowed_search_paths": ["^/search$"],
        "allowed_detail_paths": ["^/vehicles/[A-Z0-9-]+$"],
    }
    seed.source(ws, enabled=True, **ready)
    gate = "sources_enable_gate_ck"
    cases: list[tuple[dict[str, Any], str]] = [
        ({"adapter_version": "unimplemented"}, gate),
        ({"terms_decision": "pending"}, gate),
        ({"terms_decision": "do_not_use"}, gate),
        ({"terms_status": "unreviewed"}, gate),
        ({"terms_decision_actor": None}, gate),
        ({"technical_status": "untested"}, gate),
        ({"technical_status": "access_blocked"}, gate),
        ({"technical_status": "parser_unhealthy"}, gate),
        ({"allowed_hosts": []}, gate),
        ({"allowed_detail_paths": []}, gate),
        ({"allowed_search_paths": []}, gate),
        ({"robots_policy": "ignore"}, "sources_robots_policy_ck"),
        ({"technical_denial_policy": "retry_with_proxy"}, "sources_denial_policy_ck"),
        ({"allowed_hosts": ["synthetic-dealer.example", None]}, "sources_allowed_hosts_ck"),
    ]
    for override, constraint in cases:
        with violates(constraint):
            seed.source(ws, enabled=True, **{**ready, **override})
    # A restricted-terms source with an acknowledged decision may be enabled (audit, not permission).
    seed.source(
        ws, enabled=True, **{**ready, "terms_status": "restricted", "terms_decision": "proceed_acknowledged"}
    )
    # An MK comparable source needs no search paths.
    seed.source(
        ws, enabled=True, **{**ready, "role": "mk_comparable", "allowed_search_paths": [], "country": "MK"}
    )
    with violates("sources_pause_ck"):
        seed.source(ws, paused=True)
    with violates("sources_key_ck"):
        seed.source(ws, source_key="Mobile.de")


def test_snapshot_object_keys_are_private_paths(seed: Seed, world_a: World) -> None:
    base: dict[str, Any] = {
        "workspace_id": world_a.workspace_id,
        "source_id": world_a.source_id,
        "url_hash": sha("u"),
        "content_hash": sha("c"),
        "bytes": 10,
        "fetched_at": T0,
        "storage_backend": "supabase",
        "retention_policy": "retain_until",
        "retain_until": T0 + timedelta(days=30),
    }
    seed.insert("ops.source_snapshots", object_key="synthetic/2026/10/06/a.html", **base)
    for key, constraint in (
        ("https://bucket.example/a.html", "source_snapshots_object_key_ck"),
        ("s3:bucket/a.html", "source_snapshots_object_key_ck"),
        ("synthetic/../escape.html", "source_snapshots_object_key_ck"),
        ("/abs/path.html", "source_snapshots_object_key_ck"),
        (None, "source_snapshots_retention_shape_ck"),
    ):
        with violates(constraint):
            seed.insert("ops.source_snapshots", object_key=key, **base)
    # Only redaction/purge lifecycle columns may change afterwards.
    snapshot = seed.snapshot(world_a.workspace_id, world_a.source_id)
    seed.conn.execute("update ops.source_snapshots set purged_at = now() where id = %s", (snapshot,))
    with pytest.raises(psycopg.Error) as exc:
        seed.conn.execute(
            "update ops.source_snapshots set content_hash = %s where id = %s", (sha("x"), snapshot)
        )
    assert _sqlstate(exc) == SV_FROZEN
