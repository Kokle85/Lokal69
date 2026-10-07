"""Start the REAL backend for the dashboard browser E2E tests on a fresh SYNTHETIC database.

1. creates a uniquely named, migrated PostgreSQL database (``tests.db_harness``; local cluster on
   127.0.0.1:5432 by default, ``TEST_DATABASE_ADMIN_URL`` to override);
2. seeds the SYNTHETIC dataset (``tests/e2e/seed.py``) and writes the manifest the Playwright specs
   read (ids, titles, users);
3. serves ``suv_deals.api.app.create_app`` with uvicorn: ``SUPABASE_URL`` points at the mock auth
   server (the real JWKS client fetches its keys), the database is used through
   ``SET ROLE suv_backend`` (least privilege, RLS as in production), the dashboard origin is the
   only CORS origin, JWT leeway is 0 (so the short-lived E2E tokens expire on time) and the
   per-principal rate limits are relaxed for the test burst;
4. drops the database on exit (SIGTERM/SIGINT from Playwright), unless ``--keep-db``.

Nothing here touches a non-loopback service: network-facing workers, notifications and the event
bridge stay disabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
from pydantic import SecretStr

from suv_deals.api.app import create_app
from suv_deals.api.deps import ApiOptions
from suv_deals.api.middleware import PrincipalRateLimiter, RateLimit
from suv_deals.settings import Settings
from tests.db_harness import admin_url, create_migrated_database, drop_database
from tests.e2e.seed import seed_e2e

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO / "dashboard" / "e2e" / ".generated" / "seed-manifest.json"
#: SYNTHETIC cursor-signing secret for the throwaway E2E backend (not a real secret).
E2E_CURSOR_SECRET = "SYNTHETIC-e2e-cursor-signing-secret-0123456789abcdef"


def _loopback(url: str) -> bool:
    return "@127.0.0.1:" in url or "@localhost:" in url or "@127.0.0.1/" in url or "@localhost/" in url


def _exit_on_signal(signum: int, _frame: object) -> None:
    """Turn SIGTERM into SystemExit so ``finally`` drops the database.

    uvicorn restores the previous handler after a graceful shutdown and re-raises the signal it
    captured; with the default SIGTERM action the process would die before the cleanup runs.
    """
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real backend on a fresh SYNTHETIC database (E2E only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--supabase-url", default="http://127.0.0.1:54399")
    parser.add_argument("--app-origin", default="http://127.0.0.1:4173")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--keep-db", action="store_true", help="do not drop the database on exit")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("the E2E backend binds to loopback only")
    if not _loopback(admin_url()):
        parser.error("TEST_DATABASE_ADMIN_URL must point to a loopback PostgreSQL")

    signal.signal(signal.SIGTERM, _exit_on_signal)
    dbname, db_url = create_migrated_database(prefix="suv_e2e")
    print(f"[e2e-backend] created SYNTHETIC database {dbname}", flush=True)
    try:
        manifest = asyncio.run(seed_e2e(db_url))
        manifest["database"] = dbname
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[e2e-backend] seeded; manifest at {args.manifest}", flush=True)

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="test",
            app_base_url=args.app_origin,
            api_allowed_origins=args.app_origin,
            supabase_url=args.supabase_url,
            database_url=SecretStr(db_url),
            database_set_role="suv_backend",
            mcp_cursor_signing_secret=SecretStr(E2E_CURSOR_SECRET),
            build_id="e2e-synthetic",
            source_network_enabled=False,
            allow_external_notifications=False,
            event_bridge_enabled=False,
            log_level=os.environ.get("E2E_BACKEND_LOG_LEVEL", "WARNING"),
        )
        app = create_app(
            settings,
            options=ApiOptions(jwt_leeway=timedelta(0)),
            limiter=PrincipalRateLimiter(
                mutations=RateLimit(capacity=200, per_seconds=0.1),
                reads=RateLimit(capacity=2000, per_seconds=0.01),
            ),
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    finally:
        if args.keep_db:
            print(f"[e2e-backend] keeping database {dbname}", flush=True)
        else:
            drop_database(dbname)
            print(f"[e2e-backend] dropped database {dbname}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
