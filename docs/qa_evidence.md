# QA evidence

The exact commands to reproduce every automated check of this build, and the counts measured for
the final handoff (the final verification pass after wave D3). Everything here ran offline on
the development machine against local PostgreSQL clusters, fakes and synthetic data: no listing
site, mailbox, Gmail/Graph, Slack, Supabase project or OpenAI service was contacted (the only
outbound requests were `npm ci` and `npm audit` to the npm registry). What the results do and do
not prove: [acceptance_matrix.md](acceptance_matrix.md).

## What was measured

| | |
|---|---|
| Date | 2026-10-10 (UTC), runs between 11:01 and 11:44 |
| Commit | `e5d47f4f80c488b6263ea09f0296ea3b4df79693` (`e5d47f4`, 2026-10-10 05:48 UTC, "docs/schema.md: record migration 20261008000200 as applied to Supabase") |
| Working tree | **not clean**: the uncommitted waves D1, D2 and D3 plus the final verification pass (101 modified tracked files, 4,867 insertions and 457 deletions in `git diff --shortstat`; 52 untracked paths, among them new source modules, tests, JSON schemas and the handoff documents). The results below belong to this working tree, not to `e5d47f4` alone, and are therefore NOT an exact-build release result (section "Exact-build release report") |
| Python / uv | 3.13.16 / 0.11.32 (`uv lock --check`: 71 packages resolved, lock up to date) |
| PostgreSQL | 16.15 on `127.0.0.1:5432`; 17.11 on `127.0.0.1:5433` with the Supabase-like non-superuser migration owner `suv_migrator` |
| Node / npm | 22.22.0 / 10.9.4; Playwright 1.56.1 (preinstalled chromium), Vitest 5.0.3, TypeScript 6.0.3 |
| Key libraries | mcp 2.3.0, pydantic 2.13.5, fastapi 0.142.2, psycopg 3.3.6 |

## Results

| # | Check | Command (from the repository root unless noted) | Result |
|---|---|---|---|
| 1 | Lint | `uv run ruff check src tests scripts desktop` (same result with `desktop/outlook-bridge`, as `make lint` runs it) | All checks passed |
| 2 | Formatting | `uv run ruff format --check src tests scripts desktop` | 493 files already formatted |
| 3 | Types (backend) | `uv run mypy` | Success: no issues found in 180 source files |
| 4 | Types (E2E harness) | `uv run mypy --strict tests/e2e` | Success: no issues found in 9 source files |
| 5 | Types (desktop worker) | `MYPYPATH=desktop/outlook-bridge uv run mypy --strict -p outlook_bridge` | Success: no issues found in 20 source files |
| 6 | JSON schema snapshots | `uv run python scripts/export_schemas.py --check` | schemas up to date |
| 7 | Full Python suite, PostgreSQL 16 | `PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider` | **7,951 passed, 1 skipped, 0 failed** (11 min 55 s; 11:13-11:25 UTC) |
| 8 | Full Python suite, PostgreSQL 17.11 | `PYTHONDONTWRITEBYTECODE=1 TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres TEST_DATABASE_MIGRATOR_ROLE=suv_migrator uv run pytest -q -p no:cacheprovider` | **7,952 passed, 0 failed** (11 min 24 s; 11:25-11:37 UTC) |
| 9 | Desktop worker (fake Outlook) | `PYTHONDONTWRITEBYTECODE=1 uv run pytest desktop/outlook-bridge/tests -q -p no:cacheprovider` | 317 passed (4.7 s) |
| 10 | E2E harness self-tests, PostgreSQL 16 | `TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5432/postgres uv run pytest -q tests/e2e` | 36 passed (also part of rows 7 and 8) |
| 11 | E2E harness self-tests, PostgreSQL 17 | `TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres TEST_DATABASE_MIGRATOR_ROLE=suv_migrator uv run pytest -q tests/e2e` | 36 passed |
| 12 | Dashboard install | `cd dashboard && npm ci` | installed from `package-lock.json`; found 0 vulnerabilities |
| 13 | Dashboard unit/component tests | `cd dashboard && npm test` | 12 files, 192 tests passed |
| 14 | Dashboard lint and static security check | `cd dashboard && npm run lint` | oxlint with `--deny-warnings` clean; "Security check passed (40 source files)" |
| 15 | Dashboard type check | `cd dashboard && npm run typecheck` | `tsc -b` exit 0 |
| 16 | Dashboard production build | `cd dashboard && VITE_SUPABASE_URL=https://synthetic-project.supabase.example VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_SYNTHETIC_build_check npm run build` (synthetic public values; `dist/` deleted afterwards) | built; "Security check passed (40 source files + build output)" |
| 17 | Dashboard dependency audit | `cd dashboard && npm audit --omit=dev` (and `npm audit` with dev dependencies) | found 0 vulnerabilities (both) |
| 18 | Browser E2E, PostgreSQL 16 | `cd dashboard && TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5432/postgres npx playwright test` | 46 passed (2.9 min) |
| 19 | Browser E2E, PostgreSQL 17 | `cd dashboard && TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres TEST_DATABASE_MIGRATOR_ROLE=suv_migrator npx playwright test` | 46 passed (2.9 min) |
| 20 | Handoff document checks | `uv run pytest -q tests/cli/test_d2_handoff_docs.py` | 7 passed (also part of rows 7 and 8; re-run after the last document edit) |
| 21 | Offline smoke | `make smoke-local` | CLI, configuration and tax-rule validation OK; 13 passed |
| 22 | README quickstart, fixture mode | `make db-migrate-local`; the bootstrap and `config apply` lines of README.md; `make dev` | 16 migrations applied to a new local `suv_dev`; workspace bootstrapped; configuration revision 1 recorded; `make dev` synced 17 sources (the 14 registry sources and 3 synthetic fixture sources; 0 enabled) and served `/healthz` (`alive`) with `/readyz` reporting `config: not_configured` and `/api/me` answering 503, as documented; the processes were stopped and the database dropped afterwards |
| 23 | Release verifier plan | `bash scripts/verify_release.sh --plan` | lists every step; E2E steps NOT RUN without `--with-e2e`; nothing executed |
| 24 | Make targets | `make -n <target>` for each of the 31 targets of the Makefile | 30 dry-run with exit 0; `db-reset-local` refuses (exit 2) without `CONFIRM_DB_RESET=yes-drop-suv_dev` as designed and dry-runs with it |
| 25 | CLI commands named in the documents | `uv run suv-deals --no-env-file <command> --help` for every `suv-deals` command named in README.md, the top-level handoff documents, docs/, dashboard/README.md and desktop/outlook-bridge/README.md; `python -m outlook_bridge [check\|run\|status\|credential [set]] --help`; `--help` of `scripts/verify_release.sh`, `migrate.sh`, `rollback.sh`, `backup.sh`, `restore_check.sh` and `redact_logs.py` | 46 distinct `suv-deals` command paths, every one exit 0, and every flag the documents use with them exists; the desktop and script commands exit 0 |
| 26 | Migration hygiene | ASCII and `DROP TRIGGER` scan of `supabase/migrations/*.sql` (also `tests/unit/test_migrations_ascii.py`) | 16 files, 0 non-ASCII; the only `DROP TRIGGER` text is a comment in `20261007000200` explaining why `CREATE OR REPLACE TRIGGER` is used; no migration was added or changed after `20261008000200` |
| 27 | Privacy grep | case-insensitive search of the working tree (excluding `node_modules`, `.venv`, `.git`) for the local part of the owner's mailbox address, plus a list of every e-mail domain in the tree | 0 hits in any file; addresses are synthetic (`example.invalid`, `example.com`, `*.example`, `e2e.invalid`, ...); the `gmail.com` hits are placeholder normalisation fixtures such as `john.doe@gmail.com`. In git history the only hit is the author metadata of one commit, not file content |

Rows 7 and 8 include the database tests (marker `db`), the API, MCP, CLI, contract, adversarial,
adapter, property and unit tests and the E2E harness tests (`testpaths = ["tests"]`, 7,952
collected). On PostgreSQL 16 one test is skipped by design:
`tests/integration/db/test_backend_membership.py:30` ("only meaningful with
TEST_DATABASE_MIGRATOR_ROLE (Supabase-like owner)").

## Earlier runs (kept for honesty)

| Run | Result | Cause |
|---|---|---|
| D3: full suite PostgreSQL 16, 10:09-10:20 UTC | 7,949 passed, **1 failed**, 1 skipped | the flaky assertion below |
| D3: full suite PostgreSQL 17, 10:21-10:33 UTC | 7,950 passed, **1 failed** | `tests/cli/test_d2_handoff_docs.py::test_readme_links_resolve` ran while the README already linked this file before it existed (an artifact of writing the documents during the run) |
| D3: final runs, 10:36-11:00 UTC | 7,950 passed, 1 skipped (PostgreSQL 16); 7,951 passed (PostgreSQL 17) | before the test fix below (one test fewer) |
| Final pass: full suite PostgreSQL 16 started 11:01 UTC | stopped by hand at about 44 % with no failure | it had collected `test_reply_escalation.py` before the fix below; rows 7 and 8 are the complete runs on the final tree |

**Flaky test (fixed in the final verification pass).**
`tests/integration/v11_runtime/test_reply_escalation.py::test_payment_request_alerts_once_and_routine_replies_never`
checked that the deposit amount `500` does not appear in the JSON of an owner-alert payload with a
plain substring test. The payload legitimately contains random UUIDs (also inside the dashboard
link and the dedup key) and a millisecond timestamp; in the failing D3 run a reply id ending
`...fa50025ae` matched. The same check could never catch a leaked `überweisen`, because
`json.dumps` escapes it to `überweisen`. The test now compares against
`_seller_text_view(...)`: JSON with non-ASCII kept literal and every UUID and RFC 3339 timestamp
blanked. `test_seller_text_view_ignores_random_ids_but_keeps_seller_text` (same file) shows both
old failure modes; the product code was already correct and is unchanged.

## Not run here

- Live tests: the `live` marker is declared but no live test exists; live source smoke, crawler
  health, mailbox, Slack, dot and MCP client checks are activation steps (ACTIVATION_GATES.md).
- Restore drill of the hosted project, rollback drill, image build and digests.

## Exact-build release report

Done for commit `cee1a15aaa87583d8e2847bb14253932273855d4` (clean tree, 2026-10-10): every step
passed, result **verified** -
[qa/cee1a15.../README.md](qa/cee1a15aaa87583d8e2847bb14253932273855d4/README.md) (6341 tests
without a database; database tests 1610 + 1 skipped on PostgreSQL 16 and 1611 on 17.11; desktop
317; vitest 192; tests/e2e 36 and Playwright 46 on each PostgreSQL version). For every later
release commit, run:

```bash
export VITE_SUPABASE_URL=<the target's project URL> VITE_SUPABASE_PUBLISHABLE_KEY=<publishable key>
scripts/verify_release.sh --with-e2e        # writes var/releases/<sha>_<ts>.txt
```

and copy the report plus the dashboard, desktop and E2E outputs into `docs/qa/<sha>/`
([qa/README.md](qa/README.md); docs/runbook.md section 8). Only a clean tree with every step
passed reports `result=verified`.

## Reproducing everything in one go

```bash
make db-local-start
make lint typecheck schemas-check
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider          # PostgreSQL 16
PYTHONDONTWRITEBYTECODE=1 TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres \
  TEST_DATABASE_MIGRATOR_ROLE=suv_migrator uv run pytest -q -p no:cacheprovider   # PostgreSQL 17
uv run pytest -q desktop/outlook-bridge/tests
cd dashboard && npm ci && npm test && npm run lint && npm run typecheck && npm audit --omit=dev
TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5432/postgres npx playwright test
TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres \
  TEST_DATABASE_MIGRATOR_ROLE=suv_migrator npx playwright test
```

Run the two full suites one after the other, never two full suites against the same cluster at
once. Playwright binds fixed local ports (mock auth 54399, backend 8765, dashboard 4173), so only
one browser run at a time.
