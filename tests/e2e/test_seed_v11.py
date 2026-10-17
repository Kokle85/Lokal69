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

from suv_deals.api.inquiry_routes import removable_suppressions
from suv_deals.api.schemas import CandidateListQuery, InquiryListQuery
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.persistence import inquiries_repo, queries
from suv_deals.persistence.database import Database, fetch_one
from suv_deals.persistence.transactions import unit_of_work
from tests.db_harness import create_migrated_database, db_available, drop_database
from tests.e2e import run_backend, v11_actions
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
    assert set(v11["inquiries"]) == {
        "suppressed",
        "offline",
        "replied",
        "cooldown",
        "uncertain",
        "held",
        "waiting",
        "killswitch",
    }
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
            # Typed waiting reasons, from the stored state and the REAL plan jobs' wait codes.
            assert v11["wait_codes"] == {
                "cooldown": "INQUIRY_WAIT_SELLER_COOLDOWN",
                "waiting": "INQUIRY_WAIT_RATE_CAP_REACHED",
            }
            assert {key: view.waiting_reason for key, view in views.items()} == {
                "suppressed": None,
                "offline": "WORKER_OFFLINE",
                "replied": None,
                "cooldown": "SELLER_COOLDOWN",
                "uncertain": "UNCERTAIN_DELIVERY",
                "held": "NEEDS_FACTS",
                "waiting": "RATE_CAP_REACHED",
                "killswitch": None,
            }
            assert views["offline"].state == "sending" and not views["offline"].delivery_uncertain
            assert views["killswitch"].state == "suppressed"
            assert views["killswitch"].suppression_reason == "kill_switch"
            assert views["cooldown"].seller_entity_id == views["replied"].seller_entity_id
            assert all(view.approval_required is False for view in views.values())
            assert views["replied"].message is not None
            assert views["replied"].message.preview_is_informational is True

            seller = (await queries.get_reply(conn, actor, UUID(v11["replies"]["seller"]))).data
            assert seller.signal_status == "emitted"
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
                v11["inquiries"][key]
                for key in ("suppressed", "offline", "cooldown", "uncertain", "held", "waiting", "killswitch")
            }

            health = await queries.mail_worker_health_view(
                conn, actor, include_revoked=False, reconcile_interval=timedelta(seconds=120)
            )
            [box] = health.data.mailboxes
            assert box.worker_label == v11["worker_label"]
            assert box.monitoring_active is False  # the PC looks powered off: a gap, never healthy
            assert box.open_gap_count >= 1
            [active] = health.data.credentials
            assert (active.worker_label, active.credential_status) == (v11["worker_label"], "expiring")
            assert health.data.revoked_mailboxes == 1
            assert health.data.reply_signals is not None and health.data.reply_signals.emitted >= 1
            listed = await queries.mail_worker_health_view(
                conn, actor, include_revoked=True, reconcile_interval=timedelta(seconds=120)
            )
            statuses = {
                c.worker_label: (c.binding_state, c.credential_status) for c in listed.data.credentials
            }
            assert statuses[v11["retired_worker_label"]] == ("revoked", "revoked")

            row = await fetch_one(conn, "select now() as now")
            assert row is not None
            assert len(await removable_suppressions(conn, actor, row["now"])) == 1
            control = await inquiries_repo.control_view(conn, actor)
            assert (control.authorization_status, control.sender_readiness) == ("active", "ready")

            # The screening-rejected audit listing is listed only with the dashboard's audit filter.
            plain = await queries.list_candidates(
                conn, actor, CandidateListQuery(limit=100).to_tool_input(), secret=b"x" * 32
            )
            audit = await queries.list_candidates(
                conn,
                actor,
                CandidateListQuery(limit=100).to_tool_input(),
                secret=b"x" * 32,
                include_screening_rejected=True,
            )
            audit_id = UUID(manifest["listings"]["audit_rejected"])
            assert audit_id not in {item.listing_id for item in plain.data.items}
            assert audit_id in {item.listing_id for item in audit.data.items}
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


async def test_the_kill_switch_action_adds_one_removable_suppression(
    seeded: tuple[str, dict[str, Any]],
) -> None:
    """The Playwright resume-conflict test's concurrent change (``v11_actions``), via the REAL
    repository: one more removable suppression, no inquiry changes state; a second call is
    idempotent (per vehicle)."""
    url, manifest = seeded
    ws = UUID(manifest["workspace_id"])
    listing = UUID(manifest["listings"]["rejected"])
    first = await v11_actions.add_kill_switch_suppression(url, ws, listing)
    assert first == {"created": True, "removable_suppressions": 2}
    again = await v11_actions.add_kill_switch_suppression(url, ws, listing)
    assert again == {"created": False, "removable_suppressions": 2}


async def test_the_listing_revision_action_makes_the_claimed_case_stale(
    seeded: tuple[str, dict[str, Any]],
) -> None:
    """F8 (wave D2): the Playwright "new listing revision arriving before submit" test's concurrent
    change (``v11_actions add-listing-revision``): the ``hotel`` listing gets a new PROMOTED
    revision (next number, EUR 25 lower), so its pending case now cites a revision that is no
    longer the listing's current one (the submit is refused ``VERSION_CONFLICT``)."""
    url, manifest = seeded
    ws, listing = UUID(manifest["workspace_id"]), UUID(manifest["listings"]["hotel"])
    case = UUID(manifest["cases"]["hotel"])
    query = (
        "select l.current_revision_id, r.revision_number, r.asking_minor, c.revision_id as case_revision"
        " from app.listings l join app.listing_revisions r on r.id = l.current_revision_id"
        " join app.review_cases c on c.listing_id = l.id and c.id = %s"
        " where l.workspace_id = %s and l.id = %s"
    )
    with psycopg.connect(url) as conn:
        before = conn.execute(query, (case, ws, listing)).fetchone()
    assert before is not None and before[0] == before[3]  # the case cites the current revision
    result = v11_actions.add_listing_revision(url, ws, listing)
    with psycopg.connect(url) as conn:
        after = conn.execute(query, (case, ws, listing)).fetchone()
    assert after is not None
    assert result == {"revision_id": str(after[0]), "revision_number": before[1] + 1}
    assert after[1] == before[1] + 1 and after[2] == before[2] - v11_actions.REVISION_PRICE_STEP_MINOR
    assert after[3] == before[3] != after[0]  # the claimed case is now stale


def test_the_actions_refuse_anything_but_a_loopback_e2e_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_ADMIN_URL", "postgresql://suv:suv@127.0.0.1:5432/postgres")
    assert "dbname=suv_e2e_0123456789ab" in v11_actions.database_url("suv_e2e_0123456789ab")
    for name in ("postgres", "suv_test_0123456789ab", "suv_e2e_0123456789ab; drop", "suv_e2e_XYZ"):
        with pytest.raises(ValueError, match="not an E2E database"):
            v11_actions.database_url(name)
    monkeypatch.setenv("TEST_DATABASE_ADMIN_URL", "postgresql://suv:suv@db.example.invalid:5432/postgres")
    with pytest.raises(ValueError, match="loopback"):
        v11_actions.database_url("suv_e2e_0123456789ab")


#: libpq connection strings that NAME a loopback host but connect elsewhere: ``hostaddr`` is the
#: address libpq actually dials (``host`` is then only the TLS/auth name), a ``?host=`` query
#: parameter overrides the URL authority, a service name loads the host from a service file, and a
#: host list falls through to the next host.
REDIRECTED_ADMIN_URLS = (
    "postgresql://suv:suv@127.0.0.1:5432/postgres?hostaddr=10.0.0.5",
    "postgresql://suv:suv@localhost:5432/postgres?hostaddr=192.0.2.10",
    "postgresql://suv:suv@127.0.0.1:5432/postgres?host=db.example.invalid",
    "postgresql://suv:suv@127.0.0.1:5432/postgres?service=suv_remote",
    "postgresql://suv:suv@127.0.0.1:5432/postgres?servicefile=/tmp/pg_service.conf",
    "postgresql://suv:suv@127.0.0.1,db.example.invalid:5432/postgres",
    "host=127.0.0.1 hostaddr=10.0.0.5 dbname=postgres user=suv",
)


@pytest.mark.parametrize("url", REDIRECTED_ADMIN_URLS)
def test_the_loopback_guards_refuse_a_url_that_connects_elsewhere(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 review: both E2E guards (the backend that creates and DROPS a database, and the TEST-ONLY
    action) must judge where libpq really connects, never a substring of the URL."""
    assert not v11_actions.loopback_url(url)
    assert not run_backend._loopback(url)
    monkeypatch.setenv("TEST_DATABASE_ADMIN_URL", url)
    with pytest.raises(ValueError, match="loopback"):
        v11_actions.database_url("suv_e2e_0123456789ab")


@pytest.mark.parametrize(
    "url",
    (
        "postgresql://suv:suv@127.0.0.1:5432/postgres",
        "postgresql://suv:suv@127.0.0.1:5433/postgres",
        "postgresql://suv:suv@localhost:5432/postgres",
        "postgresql://suv:suv@127.0.0.1:5432/postgres?hostaddr=127.0.0.1",
        "postgresql://suv:suv@[::1]:5432/postgres",
    ),
)
def test_the_loopback_guards_accept_a_loopback_cluster(url: str) -> None:
    assert v11_actions.loopback_url(url)
    assert run_backend._loopback(url)
