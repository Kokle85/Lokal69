"""F1 (wave D2): the detail pipeline itself produces exact-ad seller, recipient and language evidence.

Before D2 no production code created a seller entity or a ``app.seller_contacts`` row, so every
eligible listing stopped at ``seller_not_linked`` and only tests (``link_seller``) could reach
``inquiry_ready``. These tests run the real pipeline on SYNTHETIC in-memory pages:

- the contact-form-only page records the seller (its website domain) and ``unavailable``
  evidence; the plan records the readiness ``SELLER_EMAIL_UNAVAILABLE`` (never guessed, never
  ``seller_not_linked``), reserves nothing;
- the page that shows ONE address: fixture -> detail ingest -> owner-imported MK comparables
  (``market import``) -> valuation -> plan -> a reserved and QUEUED inquiry, with no test-side
  ``link_seller`` / ``record_contact``;
- our own sending address on the page is never a recipient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import REPO, PipelineEnv, run
from tests.integration.v11_runtime.support import (
    ELIGIBLE_REF,
    SELLER_PAGE_ADDRESS,
    build_live_env,
    debits_of,
    eligible_live_listing,
    inquiries_of,
    jobs_of,
    now_utc,
    prepare_sender,
    runtime_settings,
    seller_email_page,
    work,
)

from suv_deals.domain.enums import EvidenceKind, FxPurpose, InquiryState, JobType
from suv_deals.domain.market_import import parse_import
from suv_deals.domain.money import FxRate
from suv_deals.persistence import listings_repo, market_repo, valuation_repo
from suv_deals.persistence.database import Conn

pytestmark = pytest.mark.db

MARKET_FILE = REPO / "tests" / "fixtures" / "market" / "asking_prices_synthetic.json"


@dataclass(frozen=True)
class _Recheck:
    listing_id: UUID
    reason: str
    idempotency_key: str


def _contacts(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select c.id, c.status, c.evidence_kind, c.address, c.language_code, c.language_status,"
        " c.seller_entity_id, c.listing_revision_id, c.extraction_location, c.extraction_excerpt,"
        " c.verified_at from app.seller_contacts c join app.listings l"
        " on l.workspace_id = c.workspace_id and l.id = c.listing_id"
        " where c.workspace_id = %s and l.source_listing_id = %s order by c.created_at, c.id",
        env.workspace_id,
        ELIGIBLE_REF,
    )


def _aliases(env: PipelineEnv, entity_id: Any) -> list[tuple[str, str]]:
    rows = env.rows(
        "select alias_kind, reference from app.seller_entity_aliases"
        " where workspace_id = %s and seller_entity_id = %s and unlinked_at is null order by alias_kind",
        env.workspace_id,
        entity_id,
    )
    return [(r["alias_kind"], r["reference"]) for r in rows]


async def _import_market(env: PipelineEnv) -> None:
    """The owner's ``market import`` of SYNTHETIC MK asking prices (observed two days ago) and a
    reference EUR/MKD rate: the only MK evidence while no MK source can be crawled (F2)."""
    document = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    for index, row in enumerate(document["observations"]):
        row["observed_at"] = (now_utc() - timedelta(days=2, minutes=index)).isoformat()
    data = json.dumps(document).encode("utf-8")
    parsed = parse_import(data, expected_kind=EvidenceKind.ASKING_PRICE)
    owner = env.owner

    async def go(conn: Conn) -> None:
        report = await market_repo.import_observations(
            conn, owner, parsed, file_sha256="0" * 64, reason="SYNTHETIC MK asking prices (test)"
        )
        assert report.created == len(parsed.observations)
        rate = FxRate(
            base="EUR",
            quote="MKD",
            rate=Decimal("61.5"),
            rate_date=date.today(),
            retrieved_at=now_utc() - timedelta(hours=1),
            provider="SYNTHETIC reference rate (test)",
            purpose=FxPurpose.REFERENCE,
        )
        await valuation_repo.upsert_fx_rate(conn, owner, rate, is_fixture=False)

    await run(env.ctx, owner, go)


async def test_contact_form_page_records_unavailable_evidence_and_never_guesses(env: PipelineEnv) -> None:
    await prepare_sender(env)
    listing = await eligible_live_listing(env)
    [contact] = _contacts(env)
    assert contact["status"] == "unavailable"
    assert contact["evidence_kind"] == "contact_form_only"
    assert contact["address"] is None and contact["verified_at"] is None
    assert contact["listing_revision_id"] == listing["current_revision_id"]
    # The dealer's own website is the seller identity the page shows (no guessed address).
    assert _aliases(env, contact["seller_entity_id"]) == [("dealer_website_domain", "dealer.example")]

    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["result_reference"]["outcome"] == "readiness_recorded"
    assert "SELLER_EMAIL_UNAVAILABLE" in plan["result_reference"]["reasons"]
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] not in (InquiryState.RESERVED, InquiryState.QUEUED)
    assert "SELLER_EMAIL_UNAVAILABLE" in inquiry["readiness_reasons"]
    assert debits_of(env) == 0


async def test_seller_email_on_the_page_reaches_a_queued_inquiry_without_test_arrangement(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    env = await build_live_env(
        db_url,
        seed,
        tmp_path,
        name="Runtime D2 evidence",
        market=False,
        page_overrides={"detail_normal.html": seller_email_page()},
    )
    try:
        await _import_market(env)
        await prepare_sender(env)
        listing = await eligible_live_listing(env)
        [contact] = _contacts(env)
        assert contact["status"] == "verified" and contact["address"] == SELLER_PAGE_ADDRESS
        assert contact["evidence_kind"] == "email_on_advertisement"
        assert contact["extraction_location"] == "listing_contact_block"
        # The stored excerpt is the redacted contact block (the address lives in ``address`` only).
        assert contact["extraction_excerpt"].startswith("Ihr Ansprechpartner im Verkauf")
        assert SELLER_PAGE_ADDRESS not in contact["extraction_excerpt"]
        assert contact["language_code"] == "de" and contact["language_status"] == "resolved"
        assert ("dealer_website_domain", "dealer.example") in _aliases(env, contact["seller_entity_id"])

        await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
        [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
        assert plan["result_reference"]["outcome"] == "reserved", plan["result_reference"]
        [inquiry] = inquiries_of(env)
        assert inquiry["state"] == InquiryState.QUEUED and inquiry["readiness"] == "inquiry_ready"
        assert debits_of(env) == 1

        # A re-fetch of the unchanged page re-verifies the same contact: no new row, no 2nd debit.
        owner = env.owner
        recheck = _Recheck(listing["id"], "synthetic recheck", "d2-evidence-recheck-1")
        await run(env.ctx, owner, lambda c: listings_repo.request_recheck(c, owner, recheck))
        reports = await work(env, JobType.RECHECK)
        assert [r.details.get("seller_evidence") for r in reports] == ["verified"], [
            (r.code, r.details) for r in reports
        ]
        [again] = _contacts(env)
        assert again["id"] == contact["id"] and again["status"] == "verified"
        assert env.scalar("select last_rechecked_at from app.seller_contacts where id = %s", contact["id"])
        assert debits_of(env) == 1
    finally:
        await env.close()


async def test_our_own_sender_address_on_the_page_is_never_a_recipient(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    page = seller_email_page().replace(SELLER_PAGE_ADDRESS, "owner-inquiries@example.invalid")
    env = await build_live_env(
        db_url,
        seed,
        tmp_path,
        settings=runtime_settings(db_url),
        name="Runtime D2 own address",
        page_overrides={"detail_normal.html": page},
    )
    try:
        await prepare_sender(env)
        await eligible_live_listing(env)
        [contact] = _contacts(env)
        assert contact["status"] == "unverified"  # recorded for the owner to see, never a recipient
        await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
        assert all(i["state"] not in (InquiryState.RESERVED, InquiryState.QUEUED) for i in inquiries_of(env))
        assert debits_of(env) == 0
    finally:
        await env.close()
