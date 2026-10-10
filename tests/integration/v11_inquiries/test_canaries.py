"""Owner-controlled activation canaries (spec 37.10 activation evidence; C1 item 12).

A canary is NOT an ``app.seller_inquiries`` row and never debits the quota ledger: these tests
record a canary, its provider outcome and a correlated test reply through
``persistence.canaries_repo`` and prove that the caps, the quota usage and the 15-day evaluation
are unchanged (the evaluation only counts the canary as an excluded synthetic record). The
database guard is exercised directly too. Nothing is sent: the target is a reserved
``example.invalid`` address and the repository only records evidence.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import psycopg
import pytest
from tests.integration.db.helpers import SV_FROZEN, SV_MONOTONIC, SV_REFERENCE, SV_TRANSITION, Seed, backend
from tests.integration.v11_inquiries.support import (
    SENDER_ADDRESS,
    World,
    now_utc,
    owner,
    scalar,
    send,
    system,
)
from tests.integration.v11_inquiries.test_send_intents import _worker

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import EmailProviderKind, Role
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.integrations.mime_builder import parse_inquiry_message_id
from suv_deals.persistence import (
    canaries_repo,
    inquiries_repo,
    mail_workers_repo,
    queries,
    sender_bindings_repo,
)
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

TARGET = "activation-canary@owner-test.example.invalid"
OWNER_REPLY_ID = "<owner-reply-1@owner-test.example.invalid>"


def _reviewer(workspace_id: uuid.UUID) -> ActorContext:
    """A reviewer reads inquiries and deals but has no ``config:admin``."""
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.REVIEWER,
        scopes=ROLE_SCOPES[Role.REVIEWER],
        request_id="req-reviewer",
        display_name="Synthetic reviewer",
    )


async def _canary(db: Database, world: World, **kwargs: object) -> canaries_repo.CanaryRecord:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        return await canaries_repo.create_canary(
            conn,
            boss,
            sender_binding_id=world.sender_binding_id,
            target_address=str(kwargs.pop("target", TARGET)),
            purpose=str(kwargs.pop("purpose", "activation evidence row 4 (synthetic)")),
            **kwargs,  # type: ignore[arg-type]
        )


async def _outcome(
    db: Database, world: World, canary_id: uuid.UUID, outcome: canaries_repo.CanaryOutcome, **kwargs: object
) -> canaries_repo.CanaryRecord:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        return await canaries_repo.record_canary_outcome(
            conn,
            boss,
            canary_id,
            outcome=outcome,
            **kwargs,  # type: ignore[arg-type]
        )


async def _reply(
    db: Database, world: World, record: canaries_repo.CanaryRecord, **kwargs: object
) -> canaries_repo.CanaryRecord:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        return await canaries_repo.record_canary_reply(
            conn,
            boss,
            record.id,
            reply_message_id=str(
                kwargs.pop("reply_message_id", "<owner-reply-1@owner-test.example.invalid>")
            ),
            in_reply_to=kwargs.pop("in_reply_to", record.rfc_message_id),  # type: ignore[arg-type]
            received_at=kwargs.pop("received_at", now_utc()),  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------------------------
# Lifecycle through the repository
# ---------------------------------------------------------------------------------------------


async def test_canary_lifecycle_records_evidence_without_the_address(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    record = await _canary(db, world)
    assert record.state == "prepared" and record.version == 1
    assert record.provider == EmailProviderKind.OUTLOOK_LOCAL
    assert record.mailbox_binding_id == worker.mailbox_binding_id  # the sender's active mailbox
    assert record.target_address_hash == hashlib.sha256(TARGET.lower().encode()).hexdigest()
    assert record.rfc_message_id == f"<canary-{record.id}@synthetic-mail.example>"
    assert SENDER_ADDRESS.endswith("@synthetic-mail.example")
    # A canary Message-ID never parses as an inquiry Message-ID (no reply can correlate to one).
    assert parse_inquiry_message_id(record.rfc_message_id) is None

    accepted = await _outcome(
        db, world, record.id, "accepted", evidence={"submission": "sent_items_confirmed", "found_sent": True}
    )
    assert accepted.state == "accepted" and accepted.version == 2 and accepted.accepted_at is not None
    assert accepted.outcome_evidence == {
        "submission": "sent_items_confirmed",
        "found_sent": True,
        "outcome": "accepted",
    }
    # Recording the same outcome again is an idempotent replay.
    again = await _outcome(db, world, record.id, "accepted")
    assert again.version == 2

    replied = await _reply(
        db,
        world,
        accepted,
        references=["<unrelated@owner-test.example.invalid>"],
        from_address=TARGET.upper().replace("@OWNER-TEST.EXAMPLE.INVALID", "@owner-test.example.invalid"),
    )
    assert replied.state == "reply_correlated" and replied.correlated and replied.finished
    assert replied.reply_message_id == "<owner-reply-1@owner-test.example.invalid>"
    assert replied.reply_evidence == {"correlated_by": "message_id", "from_matches_target": True}
    replay = await _reply(db, world, accepted)
    assert replay.version == replied.version

    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        listed = await canaries_repo.list_canaries(conn, boss)
        fetched = await canaries_repo.get_canary(conn, boss, record.id)
    assert [c.id for c in listed] == [record.id] and fetched == replied

    # The audit trail names the canary but never the address or its hash.
    audit_rows = await scalar(
        db,
        world,
        "select string_agg(action || ' ' || metadata::text, ' | ' order by occurred_at)"
        " from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s",
        {"id": record.id},
    )
    assert "inquiry_canary.create" in audit_rows and "inquiry_canary.reply" in audit_rows
    assert "@" not in audit_rows and record.target_address_hash not in audit_rows


async def test_canary_never_touches_the_quota_or_the_inquiries(db: Database, world: World) -> None:
    await _worker(db, world)
    real, _ = await send(db, world)  # one real (synthetic) seller inquiry, debited
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        before = await inquiries_repo.quota_usage(conn, boss)
    record = await _canary(db, world)
    await _outcome(db, world, record.id, "uncertain", evidence={"reason": "worker_no_result"})
    await _outcome(db, world, record.id, "accepted")
    await _reply(db, world, record)
    async with unit_of_work(db, boss) as conn:
        after = await inquiries_repo.quota_usage(conn, boss)
    assert after == before and before.count_24h == 1
    ledger = await scalar(
        db,
        world,
        "select count(*) from ops.inquiry_quota_ledger where workspace_id = %(ws)s",
        {},
    )
    inquiries = await scalar(
        db, world, "select count(*) from app.seller_inquiries where workspace_id = %(ws)s", {}
    )
    assert (ledger, inquiries) == (1, 1)
    assert real.id != record.id


async def test_evaluation_excludes_canaries_and_only_counts_them(db: Database, world: World) -> None:
    viewer = _reviewer(world.workspace_id)
    async with unit_of_work(db, viewer) as conn:
        baseline = (await queries.evaluation_report(conn, viewer)).data
    record = await _canary(db, world, mailbox_binding_id=(await _worker(db, world)).mailbox_binding_id)
    await _outcome(db, world, record.id, "accepted")
    await _reply(db, world, record)
    async with unit_of_work(db, viewer) as conn:
        inputs = await queries.evaluation_inputs(conn, viewer)
        report = (await queries.evaluation_report(conn, viewer)).data
    assert [i.inquiry_id for i in inputs.inquiries if i.is_canary] == [record.id]
    assert [r.inquiry_id for r in inputs.replies if r.is_canary] == [record.id]
    assert report.inquiries == baseline.inquiries
    assert report.seller_replies == baseline.seller_replies
    assert report.qualifying_deal_ids == baseline.qualifying_deal_ids
    # The canary inquiry and its reply are reported only as excluded synthetic records.
    assert report.excluded_synthetic_records == baseline.excluded_synthetic_records + 2


# ---------------------------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------------------------


async def test_canary_refusals(db: Database, world: World) -> None:
    # outlook_local without an active mailbox worker of the sender.
    with pytest.raises(ValidationFailed) as no_box:
        await _canary(db, world)
    assert no_box.value.details["reason"] == "canary_mailbox_missing"
    await _worker(db, world)
    # Never a seller: the world's seller contact address is refused (case-insensitively).
    assert world.vehicle.address is not None
    with pytest.raises(ValidationFailed) as seller:
        await _canary(db, world, target=world.vehicle.address.upper())
    assert seller.value.details["reason"] == "canary_target_is_seller_contact"
    with pytest.raises(ValidationFailed) as invalid:
        await _canary(db, world, target="not an address")
    assert invalid.value.details["reason"] == "canary_target_invalid"
    with pytest.raises(ValidationFailed):
        await _canary(db, world, purpose="x")
    # Only the owner (config:admin) or a system principal writes.
    viewer = _reviewer(world.workspace_id)
    with pytest.raises(Forbidden):
        async with unit_of_work(db, viewer) as conn:
            await canaries_repo.create_canary(
                conn, viewer, sender_binding_id=world.sender_binding_id, target_address=TARGET, purpose="test"
            )

    record = await _canary(db, world)
    # Evidence is allow-listed: no address, no nested object, lower-case keys.
    for bad in ({"to": TARGET}, {"nested": {"a": 1}}, {"Bad-Key": 1}, {f"k{i}": i for i in range(21)}):
        with pytest.raises(ValidationFailed):
            await _outcome(db, world, record.id, "accepted", evidence=bad)
    # A reply before any outcome, or one that does not reference the canary, is refused.
    with pytest.raises(VersionConflict):
        await _reply(db, world, record)
    await _outcome(db, world, record.id, "accepted")
    with pytest.raises(ValidationFailed) as unlinked:
        await _reply(db, world, record, in_reply_to="<other@owner-test.example.invalid>")
    assert unlinked.value.details["reason"] == "canary_reply_not_correlated"
    with pytest.raises(ValidationFailed) as stranger:
        await _reply(db, world, record, from_address="someone-else@owner-test.example.invalid")
    assert stranger.value.details["reason"] == "canary_reply_from_mismatch"
    with pytest.raises(ValidationFailed):
        await _reply(db, world, record, received_at=record.created_at - timedelta(hours=1))
    # accepted -> failed is not a legal step; a stale expected version conflicts.
    with pytest.raises(VersionConflict):
        await _outcome(db, world, record.id, "failed")
    with pytest.raises(VersionConflict):
        await _outcome(db, world, record.id, "uncertain", expected_version=1)
    # A different reply after correlation is refused; the first stays.
    await _reply(db, world, record)
    with pytest.raises(VersionConflict):
        await _reply(db, world, record, reply_message_id="<owner-reply-2@owner-test.example.invalid>")
    # Cancel only a prepared canary.
    other = await _canary(db, world)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        cancelled = await canaries_repo.cancel_canary(conn, boss, other.id, reason="not needed (synthetic)")
    assert cancelled.state == "cancelled" and cancelled.finished
    with pytest.raises(VersionConflict):
        await _outcome(db, world, other.id, "accepted")
    with pytest.raises(NotFound):
        async with unit_of_work(db, boss) as conn:
            await canaries_repo.get_canary(conn, boss, uuid.uuid4())


async def test_canary_needs_a_verified_unrevoked_sender(db: Database, world: World) -> None:
    await _worker(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        binding = await sender_bindings_repo.get_binding(conn, actor, world.sender_binding_id)
        await sender_bindings_repo.revoke_binding(conn, actor, binding.id, reason="revoked (synthetic)")
    with pytest.raises(ValidationFailed) as exc:
        await _canary(db, world)
    assert exc.value.details["problems"] == ["sender_binding_revoked"]


async def test_canaries_are_workspace_isolated(db: Database, world: World, world_b: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    boss_b = owner(world_b.workspace_id)
    async with unit_of_work(db, boss_b) as conn:
        assert await canaries_repo.list_canaries(conn, boss_b) == []
    with pytest.raises(NotFound):
        async with unit_of_work(db, boss_b) as conn:
            await canaries_repo.get_canary(conn, boss_b, record.id)


# ---------------------------------------------------------------------------------------------
# Database guard (direct SQL as suv_backend)
# ---------------------------------------------------------------------------------------------


async def test_database_guard_freezes_identity_and_steps(db: Database, seed: Seed, world: World) -> None:
    worker = await _worker(db, world)
    record = await _canary(db, world)

    def code(sql: str, *params: object) -> str | None:
        try:
            with backend(seed.conn, world.workspace_id) as conn:
                conn.execute(sql, params)  # type: ignore[arg-type]
        except psycopg.Error as exc:
            return exc.sqlstate
        return None

    table = "ops.inquiry_activation_canaries"
    # Identity is frozen (and not even granted).
    assert code(f"update {table} set purpose = 'changed' where id = %s", record.id) == "42501"
    # Illegal steps and versions.
    assert (
        code(
            f"update {table} set state = 'reply_correlated', reply_message_id = '<r@x.example>',"
            " reply_received_at = now(), reply_recorded_at = now(), outcome_recorded_at = now(),"
            " version = version + 1 where id = %s",
            record.id,
        )
        == SV_TRANSITION
    )
    assert (
        code(f"update {table} set state = 'failed', outcome_recorded_at = now() where id = %s", record.id)
        == SV_MONOTONIC
    )
    # No delete.
    assert code(f"delete from {table} where id = %s", record.id) == "42501"
    # Insert binds the CURRENT version of a verified, unrevoked sender and its active mailbox.
    insert = (
        f"insert into {table} (workspace_id, sender_binding_id, sender_binding_version, provider,"
        " mailbox_binding_id, target_address_hash, rfc_message_id, purpose, created_by)"
        " values (%s, %s, %s, 'outlook_local', %s, %s, %s, 'guard test', %s)"
    )
    stale = code(
        insert,
        world.workspace_id,
        world.sender_binding_id,
        record.sender_binding_version + 7,
        worker.mailbox_binding_id,
        "0" * 64,
        f"<canary-{uuid.uuid4()}@synthetic-mail.example>",
        uuid.uuid4(),
    )
    assert stale == SV_REFERENCE
    no_box = code(
        insert.replace("'outlook_local', %s", "'outlook_local', null"),
        world.workspace_id,
        world.sender_binding_id,
        record.sender_binding_version,
        "0" * 64,
        f"<canary-{uuid.uuid4()}@synthetic-mail.example>",
        uuid.uuid4(),
    )
    assert no_box == "23514"  # outlook_local needs the mailbox (CHECK)
    raw_address = code(
        insert,
        world.workspace_id,
        world.sender_binding_id,
        record.sender_binding_version,
        worker.mailbox_binding_id,
        TARGET,
        f"<canary-{uuid.uuid4()}@synthetic-mail.example>",
        uuid.uuid4(),
    )
    assert raw_address == "23514"  # only a SHA-256 hex digest, never the address
    # A revoked mailbox can no longer carry a new canary.
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as session:
        await mail_workers_repo.revoke_mail_worker(
            session, actor, worker.mailbox_binding_id, reason="revoked (synthetic)"
        )
    revoked = code(
        insert,
        world.workspace_id,
        world.sender_binding_id,
        record.sender_binding_version,
        worker.mailbox_binding_id,
        "0" * 64,
        f"<canary-{uuid.uuid4()}@synthetic-mail.example>",
        uuid.uuid4(),
    )
    assert revoked == SV_REFERENCE
    # A finished canary is frozen.
    await _outcome(db, world, record.id, "failed", evidence={"refusal": "kill_switch"})
    assert (
        code(
            f"update {table} set outcome_evidence = '{{}}'::jsonb, version = version + 1 where id = %s",
            record.id,
        )
        == SV_FROZEN
    )
