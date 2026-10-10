"""TEST-ONLY actions on the running E2E backend's SYNTHETIC database, for the Playwright specs.

``add-kill-switch-suppression`` records one more ``kill_switch`` suppression through the REAL
repository (``inquiries_repo.add_suppression`` as the system principal, under the controls lock,
exactly as an operator stop would), for the manifest's screening-rejected ``rejected`` listing
(never part of an inquiry, so no inquiry changes state). It is the concurrent change the owner did
not see: a resume the owner confirmed for the previous ``removable_suppressions`` count is then
refused by the server (``409 VERSION_CONFLICT``, ``details.reason = suppressions_changed``).
It prints the new removable count as JSON. Nothing is sent and nothing leaves 127.0.0.1.

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
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database, fetch_one
from suv_deals.persistence.transactions import unit_of_work
from tests.db_harness import admin_url
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TEST-ONLY actions on the SYNTHETIC E2E database")
    parser.add_argument("action", choices=["add-kill-switch-suppression"])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True:
        parser.error("the manifest is not a SYNTHETIC E2E manifest")
    dbname = str(manifest.get("database", ""))
    try:
        url = database_url(dbname)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(
        add_kill_switch_suppression(
            url, UUID(manifest["workspace_id"]), UUID(manifest["listings"]["rejected"])
        )
    )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
