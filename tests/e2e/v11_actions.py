"""TEST-ONLY actions on the running E2E backend's SYNTHETIC database, for the Playwright specs.

``add-kill-switch-suppression`` records one more ``kill_switch`` suppression through the REAL
repository (``inquiries_repo.add_suppression`` as the system principal, under the controls lock,
exactly as an operator stop would), for the manifest's screening-rejected ``rejected`` listing
(never part of an inquiry, so no inquiry changes state). It is the concurrent change the owner did
not see: a resume the owner confirmed for the previous ``removable_suppressions`` count is then
refused by the server (``409 VERSION_CONFLICT``, ``details.reason = suppressions_changed``).
It prints the new removable count as JSON. Nothing is sent and nothing leaves 127.0.0.1.

``add-listing-revision --listing KEY`` (spec 23 "new listing revision arriving before submit"; F8,
wave D2) writes one new promoted detail observation and listing revision of the manifest's
listing ``KEY`` (its current normalized document with a price EUR 25 lower and a fresh observation
time), exactly as a detail recheck would leave them (`tests.integration.read_queries.dataset.
add_revision`). A reviewer holding the listing's case then gets ``409 VERSION_CONFLICT`` on submit.
It prints the new revision's id and number as JSON.

Guards: the PostgreSQL admin URL (``TEST_DATABASE_ADMIN_URL``) must be loopback and the database
must be an E2E database (``suv_e2e_<12 hex>``) named by the manifest ``run_backend.py`` wrote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg

from suv_deals.api.inquiry_routes import removable_suppressions
from suv_deals.domain.enums import SuppressionReason
from suv_deals.domain.listings import NormalizedListing
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database, fetch_one
from suv_deals.persistence.transactions import unit_of_work
from tests.db_harness import admin_url
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import add_revision
from tests.integration.v11_inquiries.support import system

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO / "dashboard" / "e2e" / ".generated" / "seed-manifest.json"
E2E_DATABASE = re.compile(r"^suv_e2e_[0-9a-f]{12}$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})
#: Parameters that load connection settings (a host among them) from a service file.
SERVICE_PARAMS = ("service", "servicefile")


def loopback_url(url: str) -> bool:
    """Whether libpq would connect ONLY to a loopback host for this URL / conninfo string (never a
    remote or managed database): exactly one ``host``, a loopback name, as libpq parses it (so a
    ``?host=`` query parameter, which overrides the URL authority, decides); a ``hostaddr`` (the
    address libpq actually dials, ``host`` then only names it) that is loopback too; and no service
    file indirection. Never a substring test of the URL."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return False
    host = info.get("host")
    if host is None or str(host) not in LOOPBACK_HOSTS:
        return False
    hostaddr = info.get("hostaddr")
    if hostaddr is not None and str(hostaddr) not in LOOPBACK_ADDRESSES:
        return False
    return not any(info.get(key) for key in SERVICE_PARAMS)


def database_url(dbname: str) -> str:
    """The E2E database on the admin URL's loopback cluster (same credentials)."""
    if not E2E_DATABASE.fullmatch(dbname):
        raise ValueError("not an E2E database name")
    base = admin_url()
    if not loopback_url(base):
        raise ValueError("TEST_DATABASE_ADMIN_URL must point to a loopback PostgreSQL")
    info = psycopg.conninfo.conninfo_to_dict(base)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo("", **{k: v for k, v in info.items() if v is not None})


async def add_kill_switch_suppression(url: str, workspace_id: UUID, listing_id: UUID) -> dict[str, Any]:
    """One more active ``kill_switch`` suppression (idempotent per vehicle) and the new count."""
    db = Database(url, set_role="suv_backend", min_size=1, max_size=1)
    await db.open()
    try:
        actor = system(workspace_id)
        async with unit_of_work(db, actor) as conn:
            added = await inquiries_repo.add_suppression(
                conn,
                actor,
                scope="vehicle",
                key=f"listing_incarnation:{listing_id}",
                reason=SuppressionReason.KILL_SWITCH,
                evidence={"synthetic": True, "note": "SYNTHETIC E2E: a concurrent operator stop"},
            )
            row = await fetch_one(conn, "select now() as now")
            assert row is not None
            now: datetime = row["now"]
            removable = await removable_suppressions(conn, actor, now)
        return {"created": added.created, "removable_suppressions": len(removable)}
    finally:
        await db.close()


#: The price change of the SYNTHETIC new revision (EUR 25, in minor units).
REVISION_PRICE_STEP_MINOR = 2_500


def add_listing_revision(url: str, workspace_id: UUID, listing_id: UUID) -> dict[str, Any]:
    """One new promoted revision of ``listing_id`` (the next revision number, a changed price)."""
    with psycopg.connect(url, autocommit=True) as conn:
        seed = Seed(conn)
        row = conn.execute(
            "select r.revision_number, r.normalized from app.listings l"
            " join app.listing_revisions r on r.workspace_id = l.workspace_id"
            " and r.id = l.current_revision_id where l.workspace_id = %s and l.id = %s",
            (workspace_id, listing_id),
        ).fetchone()
        if row is None:
            raise ValueError("the listing has no current revision")
        number, document = int(row[0]), NormalizedListing.model_validate(row[1])
        observed = conn.execute("select now()").fetchone()
        assert observed is not None and document.price.amount_minor is not None
        price = document.price.model_copy(
            update={"amount_minor": document.price.amount_minor - REVISION_PRICE_STEP_MINOR}
        )
        changed = document.model_copy(update={"price": price, "observed_at": observed[0]})
        revision_id = add_revision(seed, workspace_id, listing_id, number + 1, changed)
    return {"revision_id": str(revision_id), "revision_number": number + 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TEST-ONLY actions on the SYNTHETIC E2E database")
    parser.add_argument("action", choices=["add-kill-switch-suppression", "add-listing-revision"])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--listing", default="hotel", help="manifest listing key (add-listing-revision)")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True:
        parser.error("the manifest is not a SYNTHETIC E2E manifest")
    dbname = str(manifest.get("database", ""))
    try:
        url = database_url(dbname)
    except ValueError as exc:
        parser.error(str(exc))
    workspace_id = UUID(manifest["workspace_id"])
    if args.action == "add-listing-revision":
        listings: dict[str, str] = manifest["listings"]
        if args.listing not in listings:
            parser.error(f"unknown manifest listing: {args.listing}")
        result = add_listing_revision(url, workspace_id, UUID(listings[args.listing]))
    else:
        result = asyncio.run(
            add_kill_switch_suppression(url, workspace_id, UUID(manifest["listings"]["rejected"]))
        )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
