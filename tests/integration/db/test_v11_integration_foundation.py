"""Migration 20261007000100_v11_integration_foundation and the v1.1 typed guard mapping.

- Enum <-> CHECK parity: every CHECK that mirrors ``domain.enums.JobType``,
  ``SuppressionReason`` or ``AvailabilityEvidenceKind`` lists exactly the enum's values, and the
  new values are accepted (and mapped) by the real tables.
- Real guard triggers: refusals of migration 20261006001000 (kill switch, mode, caps at reserve
  and dispatch, seller cooldown, suppression, one inquiry per vehicle/seller pair, stale
  listing, revoked authorization/sender/credential, cross-mailbox, tombstone, unresolved
  attempt) surface as typed ``AppError``s with a stable ``details["reason"]``; an ``SV003``
  raised at COMMIT surfaces typed from ``Database.transaction``.
- The two read-path indexes are used by the real candidate-list and outbox-attention queries
  (EXPLAIN on a seeded, isolated database) and the plans degrade without them.

Everything is SYNTHETIC (reserved example domains, fixture sources); nothing is ever sent.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.db_harness import create_migrated_database, db_available, drop_database
from tests.integration.db.helpers import T0, Seed, backend
from tests.integration.v11_db.support import (
    InquiryWorld,
    arrange_inquiry_history,
    attempt_values,
    authorization,
    binding_values,
    confirmed_cluster,
    contact,
    dispatch,
    finish_attempt,
    inquiry_world,
    insert_attempt,
    insert_inquiry,
    insert_reply,
    mailbox,
    publish_binding,
    queue,
    reply_values,
    reserve,
    seller_entity,
    sender_binding,
    sent_inquiry,
    state_of,
    suppression,
    update_inquiry,
    vehicle,
    with_vehicle,
)

from suv_deals.domain.enums import AvailabilityEvidenceKind, JobType, SuppressionReason
from suv_deals.errors import (
    AppError,
    EmailDeliveryUncertain,
    Forbidden,
    RateLimited,
    SourcePaused,
    Unauthenticated,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.persistence.database import Database
from suv_deals.persistence.errors_map import guard_reason, map_db_error, mapped_errors
from suv_deals.persistence.queries.candidates import _LIST_SQL, CANDIDATE_STATES
from suv_deals.persistence.queries.operations import _OUTBOX_SQL, ATTENTION_STATES
from suv_deals.views.inquiries import ReplySummaryView, SendAttemptSummary

pytestmark = pytest.mark.db


@pytest.fixture
def iw(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "V11 foundation")


def _mapped(action: Callable[[], object]) -> AppError:
    """Run a guarded write; return the typed error its database refusal maps to."""
    with pytest.raises(psycopg.Error) as caught:
        action()
    return map_db_error(caught.value)


def _check_values(conn: psycopg.Connection, table: str, constraint: str) -> set[str]:
    row = conn.execute(
        "select pg_get_constraintdef(c.oid) from pg_constraint c"
        " where c.conrelid = %s::regclass and c.conname = %s and c.contype = 'c' and c.convalidated",
        (table, constraint),
    ).fetchone()
    assert row is not None, f"{table}.{constraint} missing or not validated"
    match = re.search(r"ANY \(ARRAY\[(?P<items>[^\]]*)\]\)", row[0])
    assert match is not None, row[0]
    return set(re.findall(r"'([a-z_]+)'::text", match.group("items")))


# =========================================================================== enum <-> CHECK parity


def test_job_type_check_mirrors_the_enum(db_conn: psycopg.Connection) -> None:
    assert _check_values(db_conn, "ops.jobs", "jobs_type_ck") == {t.value for t in JobType}


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        ("app.seller_inquiries", "seller_inquiries_suppression_ck"),
        ("ops.email_suppressions", "email_suppressions_reason_ck"),
    ],
)
def test_suppression_reason_checks_mirror_the_enum(
    db_conn: psycopg.Connection, table: str, constraint: str
) -> None:
    assert _check_values(db_conn, table, constraint) == {r.value for r in SuppressionReason}


def test_availability_evidence_check_mirrors_the_enum(db_conn: psycopg.Connection) -> None:
    values = _check_values(db_conn, "app.availability_events", "availability_events_evidence_kind_ck")
    assert values == {k.value for k in AvailabilityEvidenceKind}


def test_availability_mapping_names_every_source_and_seller_kind(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute(
        "select pg_get_constraintdef(c.oid) from pg_constraint c"
        " where c.conname = 'availability_events_mapping_ck' and c.convalidated"
    ).fetchone()
    assert row is not None
    named = set(re.findall(r"WHEN '([a-z_]+)'::text THEN", row[0]))
    # Only manual evidence may state any value (it carries the owner's own decision).
    assert named == {k.value for k in AvailabilityEvidenceKind} - {AvailabilityEvidenceKind.MANUAL.value}


@pytest.mark.parametrize(
    "job_type",
    [
        JobType.SELLER_INQUIRY_PLAN,
        JobType.SELLER_INQUIRY_SEND,
        JobType.SELLER_INQUIRY_RECONCILE,
        JobType.SELLER_REPLY_PROCESS,
    ],
)
def test_new_job_types_are_accepted(seed: Seed, job_type: JobType) -> None:
    ws = seed.workspace("V11 jobs")
    job = seed.job(ws, job_type=job_type.value)
    assert seed.scalar("select job_type from ops.jobs where id = %s", (job,)) == job_type.value
    with pytest.raises(psycopg.errors.CheckViolation):
        seed.job(ws, job_type="seller_inquiry_purchase")


def test_authorization_revoked_is_a_suppression_reason(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="suppressed", suppression_reason="authorization_revoked")
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(),"
            " release_reason = 'AUTHORIZATION_REVOKED' where inquiry_id = %s and released_at is null",
            (inquiry,),
        )
    assert state_of(db_conn, inquiry) == "suppressed"
    suppression(iw.seed, iw.workspace_id, "workspace", "*", reason="authorization_revoked")


def _event(iw: InquiryWorld, **cols: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workspace_id": iw.workspace_id,
        "source_id": iw.vehicle.source_id,
        "listing_id": iw.listing_id,
        "old_availability": "available",
        "source_reference": "detail fetch (synthetic)",
        "effective_at": T0,
        "observed_at": T0,
        "confidence": "medium",
    }
    values.update(cols)
    return values


def _insert_event(conn: psycopg.Connection, values: Mapping[str, Any]) -> None:
    query = sql.SQL("insert into app.availability_events ({}) values ({})").format(
        sql.SQL(", ").join(sql.Identifier(k) for k in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    conn.execute(query, list(values.values()))


@pytest.mark.parametrize(
    ("kind", "reason", "allowed", "refused"),
    [
        (
            "source_reserved_badge",
            "source_reserved_badge",
            "reserved",
            ("available", "sold_claimed", "unknown"),
        ),
        ("source_detail_not_found", "detail_not_found", "unknown", ("removed", "sold_claimed", "available")),
    ],
)
def test_new_availability_evidence_determines_the_canonical_value(
    db_conn: psycopg.Connection,
    iw: InquiryWorld,
    kind: str,
    reason: str,
    allowed: str,
    refused: tuple[str, ...],
) -> None:
    with backend(db_conn, iw.workspace_id):
        _insert_event(db_conn, _event(iw, evidence_kind=kind, reason=reason, new_availability=allowed))
    for value in refused:
        with (
            pytest.raises(psycopg.errors.CheckViolation, match="availability_events_mapping_ck"),
            backend(db_conn, iw.workspace_id),
        ):
            _insert_event(db_conn, _event(iw, evidence_kind=kind, reason=reason, new_availability=value))
    # A source kind still needs a source reference (never an unsupported claim).
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="availability_events_reference_ck"),
        backend(db_conn, iw.workspace_id),
    ):
        _insert_event(
            db_conn,
            _event(iw, evidence_kind=kind, reason=reason, new_availability=allowed, source_reference=None),
        )


# =========================================================================== guard triggers (SV002/SV003)


def _set_controls(conn: psycopg.Connection, iw: InquiryWorld, **cols: Any) -> None:
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder()) for k in cols
    )
    conn.execute(
        sql.SQL(
            "update app.seller_inquiry_controls set {}, version = version + 1 where workspace_id = {}"
        ).format(assignments, sql.Placeholder()),
        [*cols.values(), iw.workspace_id],
    )


def _queued(conn: psycopg.Connection, iw: InquiryWorld) -> uuid.UUID:
    inquiry = insert_inquiry(conn, iw)
    reserve(conn, iw, inquiry)
    queue(conn, iw, inquiry)
    return inquiry


def _other_seller(conn: psycopg.Connection, iw: InquiryWorld) -> tuple[InquiryWorld, uuid.UUID]:
    world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    return world, insert_inquiry(conn, world)


def _kill(conn: psycopg.Connection, iw: InquiryWorld) -> None:
    _set_controls(
        conn,
        iw,
        kill_switch=True,
        kill_switch_reason="owner pause (synthetic)",
        kill_switch_set_at=datetime.now(UTC),
        kill_switch_set_by=uuid.uuid4(),
    )


def test_kill_switch_maps_to_a_typed_conflict_at_reserve_and_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    other, candidate = _other_seller(db_conn, iw)
    _kill(db_conn, iw)
    for error in (
        _mapped(lambda: reserve(db_conn, other, candidate)),
        _mapped(lambda: dispatch(db_conn, iw, queued)),
    ):
        assert isinstance(error, VersionConflict)
        assert guard_reason(error) == "inquiry_kill_switch"
    assert state_of(db_conn, queued) == "queued"


@pytest.mark.parametrize("mode", ["disabled_until_sender_ready", "paused"])
def test_mode_maps_with_the_mode_token(db_conn: psycopg.Connection, iw: InquiryWorld, mode: str) -> None:
    _set_controls(db_conn, iw, mode=mode)
    inquiry = insert_inquiry(db_conn, iw)
    error = _mapped(lambda: reserve(db_conn, iw, inquiry))
    assert isinstance(error, VersionConflict)
    assert error.details["reason"] == "inquiry_mode_not_automatic" and error.details["mode"] == mode


def test_missing_controls_map_to_a_typed_conflict(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    db_conn.execute("delete from app.seller_inquiry_controls where workspace_id = %s", (iw.workspace_id,))
    assert guard_reason(_mapped(lambda: reserve(db_conn, iw, inquiry))) == "inquiry_controls_missing"


def test_cap_at_reserve_maps_to_rate_limited(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    _set_controls(db_conn, iw, max_per_24h=0)
    inquiry = insert_inquiry(db_conn, iw)
    error = _mapped(lambda: reserve(db_conn, iw, inquiry))
    assert isinstance(error, RateLimited)
    assert error.details == {"reason": "inquiry_cap_reached", "phase": "reserve", "window": "24h", "limit": 0}
    assert state_of(db_conn, inquiry) == "qualifying"


@pytest.mark.parametrize(("column", "window"), [("max_per_24h", "24h"), ("max_per_15d", "15d")])
def test_cap_at_dispatch_maps_to_rate_limited(
    db_conn: psycopg.Connection, iw: InquiryWorld, column: str, window: str
) -> None:
    queued = _queued(db_conn, iw)
    _set_controls(db_conn, iw, **{column: 0})
    error = _mapped(lambda: dispatch(db_conn, iw, queued))
    assert isinstance(error, RateLimited)
    assert error.details == {
        "reason": "inquiry_cap_reached",
        "phase": "dispatch",
        "window": window,
        "limit": 0,
    }
    assert state_of(db_conn, queued) == "queued"


def test_seller_cooldown_maps_at_reserve_and_dispatch(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    first = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, first)
    second_car = with_vehicle(iw)  # same seller entity, another car
    second = insert_inquiry(db_conn, second_car)
    error = _mapped(lambda: reserve(db_conn, second_car, second))
    assert isinstance(error, RateLimited)
    assert error.details == {"reason": "seller_cooldown", "phase": "reserve"}
    # Reserved more than a cooldown apart, transmitted together: refused at dispatch.
    queue(db_conn, iw, first)
    long_ago = datetime.now(UTC) - timedelta(days=8)
    arrange_inquiry_history(db_conn, first, reserved_at=long_ago, queued_at=long_ago)
    reserve(db_conn, second_car, second)
    queue(db_conn, second_car, second)
    dispatch(db_conn, iw, first)
    error = _mapped(lambda: dispatch(db_conn, second_car, second))
    assert isinstance(error, RateLimited)
    assert error.details == {"reason": "seller_cooldown", "phase": "dispatch"}


def test_suppression_maps_with_its_scope_reason_codes(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    queued = _queued(db_conn, iw)
    suppression(
        iw.seed, iw.workspace_id, "seller", f"seller_entity:{iw.seller_entity_id}", reason="seller_opt_out"
    )
    error = _mapped(lambda: dispatch(db_conn, iw, queued))
    assert isinstance(error, VersionConflict)
    assert error.details["reason"] == "inquiry_suppressed"
    assert error.details["suppressions"] == ["seller:seller_opt_out"]


def test_one_inquiry_per_vehicle_seller_pair_maps_to_a_typed_conflict(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    first, _ = sent_inquiry(db_conn, iw)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    arrange_inquiry_history(
        db_conn,
        first,
        reserved_at=long_ago,
        queued_at=long_ago,
        send_attempted_at=long_ago,
        accepted_at=long_ago,
    )
    twin_vehicle = vehicle(seed, iw.workspace_id)  # the same car on another site
    contact_id, address = contact(
        seed,
        iw.workspace_id,
        twin_vehicle,
        iw.seller_entity_id,
        evidence_kind="marketplace_relay_for_listing",
    )
    twin = InquiryWorld(
        iw.workspace_id,
        seed,
        twin_vehicle,
        iw.seller_entity_id,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    second = insert_inquiry(db_conn, twin, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    error = _mapped(lambda: reserve(db_conn, twin, second))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_vehicle_seller_conflict"
    # A listing identity for a member of a confirmed cluster is not the canonical identity (SV003).
    error = _mapped(lambda: insert_inquiry(db_conn, twin))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_identity_not_canonical"


def test_stale_listing_maps_to_a_typed_conflict(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    _, gen, obs = seed.detail_observation(iw.workspace_id, iw.listing_id, promoted=True)
    revision = seed.revision(
        iw.workspace_id, iw.listing_id, 2, detail_generation=gen, observation_id=obs, asking_minor=240000
    )
    seed.promote(iw.workspace_id, iw.listing_id, revision, gen, obs)
    error = _mapped(lambda: dispatch(db_conn, iw, queued))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_listing_stale"


def test_revoked_authorization_and_sender_map_to_forbidden(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    revoked = authorization(
        iw.seed,
        iw.workspace_id,
        version=2,
        revoked_at=T0,
        revoked_by="Synthetic owner",
        revoke_reason="synthetic revocation for a test",
    )
    error = _mapped(lambda: reserve(db_conn, iw, inquiry, authorization_id=revoked, authorization_version=2))
    assert isinstance(error, Forbidden)
    assert guard_reason(error) == "inquiry_authorization_revoked"

    other = inquiry_world(iw.seed, "V11 foundation sender")
    queued = _queued(db_conn, other)
    db_conn.execute(
        "update ops.email_sender_bindings set revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic token revoked' where id = %s",
        (uuid.uuid4(), other.sender_binding_id),
    )
    error = _mapped(lambda: dispatch(db_conn, other, queued))
    assert isinstance(error, Forbidden)
    assert guard_reason(error) == "sender_binding_revoked"


def test_unresolved_earlier_attempt_maps_to_email_delivery_uncertain(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    dispatch(db_conn, iw, queued)  # attempt 1 is running (possibly submitted)

    def second_attempt() -> None:
        with backend(db_conn, iw.workspace_id):
            insert_attempt(db_conn, attempt_values(iw, queued, 2, fencing_token=2))

    error = _mapped(second_attempt)
    assert isinstance(error, EmailDeliveryUncertain)
    assert guard_reason(error) == "send_attempt_unresolved"
    assert error.retryable is False


def test_illegal_transition_maps_with_both_states(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    queued = _queued(db_conn, iw)

    def back_to_candidate() -> None:
        with backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, queued, state="candidate")

    error = _mapped(back_to_candidate)
    assert isinstance(error, VersionConflict)
    assert error.details["reason"] == "inquiry_transition_not_permitted"
    assert (error.details["from_state"], error.details["to_state"]) == ("queued", "candidate")


# --- mailbox route ---------------------------------------------------------------------------


def _sent_with_mailbox(conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> tuple[uuid.UUID, uuid.UUID]:
    inquiry, _ = sent_inquiry(conn, iw)
    box = mailbox(seed, iw)
    publish_binding(conn, iw, box, inquiry)
    return inquiry, box


def _reply(
    conn: psycopg.Connection, iw: InquiryWorld, inquiry: uuid.UUID, box: uuid.UUID
) -> Callable[[], object]:
    def run() -> None:
        with backend(conn, iw.workspace_id):
            insert_reply(conn, reply_values(iw, inquiry, box))

    return run


def test_revoked_worker_credential_maps_to_unauthenticated(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    row = db_conn.execute(
        "select credential_id from ops.mail_worker_bindings where id = %s", (box,)
    ).fetchone()
    assert row is not None
    db_conn.execute(
        "update ops.api_credentials set revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic laptop lost' where id = %s",
        (uuid.uuid4(), row[0]),
    )
    error = _mapped(_reply(db_conn, iw, inquiry, box))
    assert isinstance(error, Unauthenticated)
    assert guard_reason(error) == "mail_worker_credential_revoked"


def test_cross_mailbox_injection_maps_to_forbidden(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, _own = _sent_with_mailbox(db_conn, seed, iw)
    other_sender = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")
    foreign = mailbox(
        seed, iw, sender_binding_id=other_sender, account_address="second@synthetic-mail.example"
    )
    error = _mapped(_reply(db_conn, iw, inquiry, foreign))
    assert isinstance(error, Forbidden)
    assert guard_reason(error) == "mailbox_binding_mismatch"


def test_revoked_mailbox_and_tombstoned_binding_map_to_forbidden(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    publish_binding(db_conn, iw, box, inquiry, 2, state="tombstoned")
    error = _mapped(_reply(db_conn, iw, inquiry, box))
    assert isinstance(error, Forbidden)
    assert guard_reason(error) == "inquiry_binding_tombstoned"
    # Re-publishing a tombstone is a conflict, not an authorization failure.
    error = _mapped(lambda: publish_binding(db_conn, iw, box, inquiry, 3))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_binding_tombstoned"

    other = inquiry_world(seed, "V11 foundation mailbox")
    inquiry_b, box_b = _sent_with_mailbox(db_conn, seed, other)
    db_conn.execute(
        "update ops.mail_worker_bindings set state = 'revoked', revoked_at = now(), revoked_by = %s,"
        " revoke_reason = 'synthetic revocation' where id = %s",
        (uuid.uuid4(), box_b),
    )
    error = _mapped(_reply(db_conn, other, inquiry_b, box_b))
    assert isinstance(error, Forbidden)
    assert guard_reason(error) == "mailbox_binding_revoked"


# --- review additions: the remaining refusals the brief names, through the real triggers ---------


def test_stale_qualification_snapshot_maps_to_a_typed_conflict(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    error = _mapped(lambda: reserve(db_conn, iw, inquiry, qualified_price_minor=199000))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_qualification_mismatch"
    assert state_of(db_conn, inquiry) == "qualifying"


def test_changed_availability_maps_to_a_typed_conflict_at_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    with db_conn.transaction():  # TEST ARRANGEMENT: the source now shows a reserved badge
        db_conn.execute("set local session_replication_role = replica")
        db_conn.execute("update app.listings set availability = 'reserved' where id = %s", (iw.listing_id,))
    error = _mapped(lambda: dispatch(db_conn, iw, queued))
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "inquiry_availability_stale"
    assert state_of(db_conn, queued) == "queued"


def test_paused_source_maps_to_source_paused_at_dispatch(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    db_conn.execute(
        "update app.sources set paused = true, paused_at = now(), pause_reason = 'synthetic pause'"
        " where id = %s",
        (iw.vehicle.source_id,),
    )
    error = _mapped(lambda: dispatch(db_conn, iw, queued))
    assert isinstance(error, SourcePaused)
    assert guard_reason(error) == "inquiry_source_paused"


def test_attempt_through_another_sender_account_maps_to_a_typed_conflict(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    other = sender_binding(seed, iw.workspace_id, from_address="second@synthetic-mail.example")

    def switched_account() -> None:
        with backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, queued, state="sending")
            insert_attempt(db_conn, attempt_values(iw, queued, sender_binding_id=other))

    error = _mapped(switched_account)
    assert isinstance(error, VersionConflict)
    assert guard_reason(error) == "sender_binding_mismatch"
    assert state_of(db_conn, queued) == "queued"


def test_expired_attempt_lease_maps_to_email_delivery_uncertain(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    attempt = dispatch(db_conn, iw, queued)
    with db_conn.transaction():  # TEST ARRANGEMENT: the worker's lease ran out mid-send
        db_conn.execute("set local session_replication_role = replica")
        db_conn.execute(
            "update ops.email_delivery_attempts set lease_expires_at = now() - interval '1 minute'"
            " where id = %s",
            (attempt,),
        )

    def claim_never_submitted() -> None:
        with backend(db_conn, iw.workspace_id):
            finish_attempt(
                db_conn,
                attempt,
                "pre_submission_failure",
                pre_submission_proof="connection_refused_before_submit",
            )

    error = _mapped(claim_never_submitted)
    assert isinstance(error, EmailDeliveryUncertain)
    assert guard_reason(error) == "send_attempt_lease_expired"
    assert error.retryable is False
    assert state_of(db_conn, queued) == "sending"


def test_retry_without_proof_maps_to_email_delivery_uncertain(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    queued = _queued(db_conn, iw)
    attempt = dispatch(db_conn, iw, queued)
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "definite_rejection", error_code="RECIPIENT_REJECTED")
        update_inquiry(db_conn, queued, state="failed_definite")

    def requeue() -> None:
        with backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, queued, state="queued")

    error = _mapped(requeue)
    assert isinstance(error, EmailDeliveryUncertain)
    assert guard_reason(error) == "retry_without_proof"
    assert state_of(db_conn, queued) == "failed_definite"


def test_expired_worker_credential_maps_to_unauthenticated(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with db_conn.transaction():  # TEST ARRANGEMENT: time passed beyond the credential's lifetime
        db_conn.execute("set local session_replication_role = replica")
        db_conn.execute(
            "update ops.api_credentials set created_at = now() - interval '40 days',"
            " expires_at = now() - interval '1 second'"
            " where id = (select credential_id from ops.mail_worker_bindings where id = %s)",
            (box,),
        )
    error = _mapped(_reply(db_conn, iw, inquiry, box))
    assert isinstance(error, Unauthenticated)
    assert guard_reason(error) == "mail_worker_credential_revoked"


def test_read_models_accept_what_the_reply_and_attempt_tables_store(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """View patterns never drift stricter than the CHECKs: a Dutch reply and an attempt error
    code without a leading letter are valid rows and must stay readable (no 500 on read)."""
    inquiry, box = _sent_with_mailbox(db_conn, seed, iw)
    with backend(db_conn, iw.workspace_id):
        insert_reply(db_conn, reply_values(iw, inquiry, box, detected_language="nl"))
    row = db_conn.execute(
        "select detected_language from app.seller_replies where inquiry_id = %s", (inquiry,)
    ).fetchone()
    assert row is not None and row[0] == "nl"
    ReplySummaryView.model_validate(
        {
            "reply_id": uuid.uuid4(),
            "inquiry_id": inquiry,
            "vehicle": {
                "vehicle_kind": "listing_incarnation",
                "vehicle_cluster_id": None,
                "listing_id": iw.listing_id,
                "source_key": None,
                "listing_reference": None,
                "listing_url": None,
            },
            "message_type": "seller_reply",
            "original_language": row[0],
            "availability": None,
            "quarantined": False,
            "processing_state": "stored",
            "received_at": T0,
            "ingested_at": T0,
        }
    )
    other = inquiry_world(seed, "V11 foundation views")
    queued = _queued(db_conn, other)
    attempt = dispatch(db_conn, other, queued)
    with backend(db_conn, other.workspace_id):
        finish_attempt(db_conn, attempt, "uncertain", error_code="_LEASE_EXPIRED")
        update_inquiry(db_conn, queued, state="uncertain")
    stored = db_conn.execute(
        "select attempt_number, provider, outcome, send_intent_committed_at, finished_at, error_code"
        " from ops.email_delivery_attempts where id = %s",
        (attempt,),
    ).fetchone()
    assert stored is not None
    summary = SendAttemptSummary(
        attempt_number=stored[0],
        provider=stored[1],
        outcome=stored[2],
        send_intent_committed_at=stored[3],
        finished_at=stored[4],
        reconciled_outcome=None,
        reconciled_at=None,
        submission_uncertain=True,
        error_code=stored[5],
    )
    assert summary.error_code == "_LEASE_EXPIRED"


# --- through the application's Database ----------------------------------------------------------


def _adapt(value: Any) -> Any:
    return Jsonb(value) if isinstance(value, Mapping) else value


def _reserve_without_debit_sql(iw: InquiryWorld, inquiry: uuid.UUID) -> tuple[sql.Composed, list[Any]]:
    cols = {"state": "reserved", **binding_values(iw)}
    query = sql.SQL("update app.seller_inquiries set {} where id = {}").format(
        sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder()) for k in cols),
        sql.Placeholder(),
    )
    return query, [*(_adapt(v) for v in cols.values()), inquiry]


@pytest.fixture
async def app_db(db_url: str) -> Any:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=2, pool_timeout_s=5)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def test_evidence_missing_at_commit_surfaces_typed_from_the_transaction(
    app_db: Database, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    query, params = _reserve_without_debit_sql(iw, inquiry)
    with pytest.raises(ValidationFailed) as caught:
        async with app_db.transaction(workspace_id=iw.workspace_id) as conn:
            await conn.execute(query, params)  # immediate guards pass; the debit check is deferred
    assert caught.value.details == {
        "guard": "evidence",
        "reason": "inquiry_evidence_missing",
        "evidence": "quota_debit",
        "state": "reserved",
    }
    assert isinstance(caught.value.__cause__, psycopg.Error) and caught.value.__cause__.sqlstate == "SV003"
    assert state_of(db_conn, inquiry) == "qualifying"  # nothing was committed


async def test_immediate_guard_inside_the_transaction_maps_through_mapped_errors(
    app_db: Database, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    _kill(db_conn, iw)
    query, params = _reserve_without_debit_sql(iw, inquiry)
    with pytest.raises(VersionConflict) as caught:
        async with mapped_errors(), app_db.transaction(workspace_id=iw.workspace_id) as conn:
            await conn.execute(query, params)
    assert guard_reason(caught.value) == "inquiry_kill_switch"
    assert state_of(db_conn, inquiry) == "qualifying"


# =========================================================================== read-path indexes (EXPLAIN)

_BIG = 15_000
_SMALL = 2_500


def _seed_workspace(conn: psycopg.Connection, seed: Seed, listings: int, outbox: int) -> uuid.UUID:
    """A synthetic (inactive) workspace with many listings and deliveries (triggers bypassed)."""
    ws = seed.workspace("V11 index probe")
    src = seed.source(ws, source_key=f"idx_probe_{uuid.uuid4().hex[:8]}")
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            """
            insert into app.listings (workspace_id, source_id, source_listing_id, canonical_url,
              identity_method, identity_material, identity_hash, identity_confidence,
              first_seen_at, last_seen_at, availability, eligibility_state, eligibility_profile,
              screening, screening_version, screened_at, created_at, updated_at, is_fixture)
            select %(ws)s, %(src)s, 'L' || g, 'https://probe.example.invalid/v/' || g, 'provider_id',
                   'm' || g,
                   encode(sha256(convert_to(%(ws)s::text || g, 'UTF8')), 'hex'), 'high',
                   now() - g * interval '1 minute', now(), 'available',
                   case when g %% 10 = 0 then 'eligible_primary' else 'rejected' end, 'primary',
                   '{}'::jsonb, 'v1', now(), now() - g * interval '1 minute', now(), false
              from generate_series(1, %(n)s) g
            """,
            {"ws": ws, "src": src, "n": listings},
        )
        conn.execute(
            """
            insert into app.listing_revisions (workspace_id, listing_id, revision_number, semantic_hash,
              asking_minor, currency, seller_country, make, model, normalized, provenance, parser_version,
              observed_at)
            select l.workspace_id, l.id, 1, encode(sha256(convert_to(l.id::text, 'UTF8')), 'hex'),
                   275000, 'EUR', 'DE', 'Example', 'Trail', '{"title": "x"}'::jsonb, '{}'::jsonb, 'p1', now()
              from app.listings l where l.workspace_id = %(ws)s
            """,
            {"ws": ws},
        )
        conn.execute(
            "update app.listings l set current_revision_id = r.id from app.listing_revisions r"
            " where r.workspace_id = l.workspace_id and r.listing_id = l.id and l.workspace_id = %(ws)s",
            {"ws": ws},
        )
        conn.execute(
            """
            insert into ops.outbox (workspace_id, event_type, aggregate_type, aggregate_id, payload,
              payload_hash, dedup_key, state, blocker_code, send_attempted_at, provider_accepted_at,
              event_created_at)
            select %(ws)s, 'review.pending', 'review_case', gen_random_uuid(), '{}'::jsonb, repeat('a', 64),
                   'probe-' || g,
                   case when g %% 50 = 0 then 'uncertain' when g %% 77 = 0 then 'retry_wait'
                        when g %% 91 = 0 then 'blocked' else 'delivered' end,
                   case when g %% 91 = 0 and g %% 50 <> 0 and g %% 77 <> 0 then 'X' end,
                   now(), case when g %% 50 <> 0 and g %% 77 <> 0 and g %% 91 <> 0 then now() end,
                   now() - g * interval '1 second'
              from generate_series(1, %(n)s) g
            """,
            {"ws": ws, "n": outbox},
        )
    return ws


@pytest.fixture(scope="module")
def probe_db() -> Iterator[tuple[str, uuid.UUID]]:
    """An isolated migrated database with one large and two small workspaces, analysed."""
    if not db_available():
        pytest.skip("PostgreSQL not reachable via TEST_DATABASE_ADMIN_URL")
    dbname, url = create_migrated_database("suv_v11idx")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            seed = Seed(conn)
            big = _seed_workspace(conn, seed, _BIG, _BIG * 2)
            for _ in range(2):
                _seed_workspace(conn, seed, _SMALL, _SMALL * 2)
            conn.execute("analyze")
        yield url, big
    finally:
        drop_database(dbname)


def _plan(conn: psycopg.Connection, ws: uuid.UUID, query: Any, params: Mapping[str, Any]) -> str:
    statement = sql.SQL("explain (format json) ") + (
        query if isinstance(query, sql.Composable) else sql.SQL(query)
    )
    with backend(conn, ws):
        row = conn.execute(statement, params).fetchone()
    assert row is not None
    plan = row[0] if not isinstance(row[0], str) else json.loads(row[0])
    return json.dumps(plan)


def _list_params(ws: uuid.UUID) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "ws": ws,
        "now": now,
        "as_of": now,
        "candidate_states": list(CANDIDATE_STATES),
        "profile": None,
        "country": None,
        "status": None,
        "changed_since": None,
        "after_created": None,
        "after_id": None,
        "limit": 26,
    }


def _outbox_params(ws: uuid.UUID, states: list[str] | None = None) -> dict[str, Any]:
    return {
        "ws": ws,
        "states": states or list(ATTENTION_STATES),
        "as_of": datetime.now(UTC),
        "after_created": None,
        "after_id": None,
        "limit": 26,
    }


def test_candidate_list_uses_the_created_keyset_index(probe_db: tuple[str, uuid.UUID]) -> None:
    url, big = probe_db
    with psycopg.connect(url, autocommit=True) as conn:
        plan = _plan(conn, big, _LIST_SQL, _list_params(big))
        assert '"Index Name": "listings_created_idx"' in plan, plan
        # Without the index the whole workspace is read and sorted for every page (the drop is
        # rolled back: psycopg.Rollback aborts the transaction block quietly).
        with conn.transaction():
            conn.execute("drop index app.listings_created_idx")
            without = _plan(conn, big, _LIST_SQL, _list_params(big))
            assert "listings_created_idx" not in without
            assert '"Node Type": "Sort"' in without or '"Node Type": "Incremental Sort"' in without, without
            raise psycopg.Rollback()


@pytest.mark.parametrize("states", [None, ["uncertain"], ["retry_wait"]])
def test_outbox_attention_list_uses_the_partial_index(
    probe_db: tuple[str, uuid.UUID], states: list[str] | None
) -> None:
    url, big = probe_db
    with psycopg.connect(url, autocommit=True) as conn:
        plan = _plan(conn, big, _OUTBOX_SQL, _outbox_params(big, states))
        assert '"Index Name": "outbox_attention_created_idx"' in plan, plan
        assert '"Node Type": "Seq Scan"' not in plan, plan


def test_outbox_attention_list_falls_back_to_a_scan_without_the_index(
    probe_db: tuple[str, uuid.UUID],
) -> None:
    url, big = probe_db
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("drop index ops.outbox_attention_created_idx")
        without = _plan(conn, big, _OUTBOX_SQL, _outbox_params(big))
        assert "outbox_attention_created_idx" not in without
        # The older partial index excludes retry_wait, so it cannot serve the attention list.
        assert '"Index Name": "outbox_attention_idx"' not in without, without
        raise psycopg.Rollback()


def test_indexes_survive_the_rollback_of_the_probes(probe_db: tuple[str, uuid.UUID]) -> None:
    url, _ = probe_db
    with psycopg.connect(url, autocommit=True) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "select indexname from pg_indexes where indexname in"
                " ('listings_created_idx', 'outbox_attention_created_idx')"
            ).fetchall()
        }
    assert names == {"listings_created_idx", "outbox_attention_created_idx"}
