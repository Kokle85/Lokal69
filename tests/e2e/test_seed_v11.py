"""The SYNTHETIC spec v1.1 E2E dataset (``tests/e2e/seed_v11.py``) seeds what the browser specs rely
on, through the real repositories, and only ``example.invalid`` addresses (never a real mailbox).

Runs the whole E2E seed on a fresh migrated database (no network; ``TEST_DATABASE_ADMIN_URL``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from suv_deals.api.schemas import InquiryListQuery
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.persistence import queries
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from tests.db_harness import create_migrated_database, db_available, drop_database
from tests.e2e.seed import seed_e2e

pytestmark = pytest.mark.db

ADDRESS_COLUMNS = (
    ("app.seller_contacts", "address"),
    ("ops.email_sender_bindings", "from_address"),
    ("ops.email_sender_bindings", "reply_to_address"),
    ("app.seller_inquiries", "sender_from_address"),
    ("app.seller_inquiries", "sender_reply_to_address"),
    ("app.seller_inquiries", "recipient_address"),
    ("ops.mail_worker_bindings", "account_address"),
    ("app.seller_replies", "from_address"),
)


@pytest.fixture
async def seeded() -> AsyncIterator[tuple[str, dict[str, Any]]]:
    if not db_available():
        pytest.skip("PostgreSQL not reachable via TEST_DATABASE_ADMIN_URL")
    dbname, url = create_migrated_database(prefix="suv_e2e_seedtest")
    try:
        yield url, await seed_e2e(url)
    finally:
        drop_database(dbname)


def _owner(manifest: dict[str, Any]) -> ActorContext:
    return ActorContext(
        workspace_id=UUID(manifest["workspace_id"]),
        principal_id=UUID(manifest["users"]["owner"]["user_id"]),
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset(Scope) - {Scope.MAIL_INGEST},
        request_id="req-e2e-seed-test",
    )


async def test_the_v11_world_has_every_state_the_specs_use(seeded: tuple[str, dict[str, Any]]) -> None:
    url, manifest = seeded
    v11 = manifest["v11"]
    assert set(v11["inquiries"]) == {"suppressed", "replied", "uncertain", "held", "waiting"}
    assert set(v11["replies"]) == {"seller", "quarantined"}
    actor = _owner(manifest)
    db = Database(url, set_role="suv_backend", min_size=1, max_size=2)
    await db.open()
    try:
        async with unit_of_work(db, actor) as conn:
            views = {
                key: (await queries.get_inquiry(conn, actor, UUID(value))).data
                for key, value in v11["inquiries"].items()
            }
            assert views["suppressed"].state == "suppressed"
            assert views["suppressed"].suppression_reason == "seller_opt_out"
            assert views["replied"].state == "replied"
            assert views["replied"].reply_count == 2
            assert views["uncertain"].state == "uncertain" and views["uncertain"].delivery_uncertain
            assert views["held"].state == "held_facts"
            assert "LANGUAGE_UNRESOLVED" in views["held"].state_reasons
            assert views["waiting"].state == "qualifying"
            assert "RATE_CAP_REACHED" in views["waiting"].state_reasons
            assert all(view.approval_required is False for view in views.values())
            assert views["replied"].message is not None
            assert views["replied"].message.preview_is_informational is True

            seller = (await queries.get_reply(conn, actor, UUID(v11["replies"]["seller"]))).data
            assert seller.claims is not None
            assert set(seller.claims.escalations) == {"payment", "reservation"}
            [quote] = seller.claims.price_quotes
            assert (quote.amount, quote.currency, quote.accepted) == ("26500", "EUR", False)
            quarantined = (await queries.get_reply(conn, actor, UUID(v11["replies"]["quarantined"]))).data
            assert quarantined.quarantined is True
            # The unverified sender's deposit request is extracted (the owner sees it, attributed to
            # "the sender", never to the seller); no owner alert or vehicle update follows from it.
            assert quarantined.claims is not None
            assert "payment" in quarantined.claims.escalations
            assert quarantined.sender.matches_verified_recipient is False

            attention = await queries.list_inquiries(
                conn, actor, InquiryListQuery(attention_only=True), secret=b"x" * 32, attention_only=True
            )
            assert {str(item.inquiry_id) for item in attention.data.items} >= {
                v11["inquiries"][key] for key in ("suppressed", "uncertain", "held")
            }

            health = await queries.mail_worker_health_view(
                conn, actor, include_revoked=False, reconcile_interval=timedelta(seconds=120)
            )
            [box] = health.data.mailboxes
            assert box.worker_label == v11["worker_label"]
            assert box.monitoring_active is False  # the PC looks powered off: a gap, never healthy
            assert box.open_gap_count >= 1
    finally:
        await db.close()


async def test_only_example_invalid_addresses_are_stored(seeded: tuple[str, dict[str, Any]]) -> None:
    url, manifest = seeded
    assert uuid.UUID(manifest["v11"]["mailbox_id"])
    with psycopg.connect(url, autocommit=True) as conn:
        for table, column in ADDRESS_COLUMNS:
            rows = conn.execute(f"select {column} from {table} where {column} is not null").fetchall()
            for (value,) in rows:
                assert str(value).endswith("@example.invalid"), (table, column)
        # Nothing reached an external destination: the seller-reply signals stay internal.
        delivered = conn.execute(
            "select count(*) from ops.outbox where event_type = 'seller.reply.received'"
            " and state in ('sending', 'delivered', 'uncertain')"
        ).fetchone()
        assert delivered == (0,)
