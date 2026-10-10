# Exact-build QA evidence for commit cee1a15

`scripts/verify_release.sh --with-e2e` ran on commit `cee1a15aaa87583d8e2847bb14253932273855d4`
with a clean working tree (`dirty_working_tree=no`), finished 2026-10-10, result **verified**.
The full machine report is [release_report.txt](release_report.txt).

| Step | Result | Count |
|---|---|---|
| lint (ruff check + format: src, tests, scripts, desktop/outlook-bridge) | passed | |
| typecheck (mypy --strict: suv_deals, tests/e2e, outlook_bridge) | passed | |
| JSON schema snapshots | passed | |
| tests without a database | passed | 6341 passed |
| database tests, PostgreSQL 16 | passed | 1610 passed, 1 skipped (migrator-role-only test) |
| database tests, PostgreSQL 17.11 (Supabase-like migrator role) | passed | 1611 passed |
| desktop worker tests (fakes, no Outlook) | passed | 317 passed |
| dashboard npm ci / build / vitest / lint / audit | passed | vitest 192 passed; audit 0 vulnerabilities |
| browser E2E, PostgreSQL 16 (harness + Playwright) | passed | 36 + 46 passed |
| browser E2E, PostgreSQL 17.11 (harness + Playwright) | passed | 36 + 46 passed |

Notes:
- The dashboard build used synthetic `VITE_SUPABASE_URL` / `VITE_SUPABASE_PUBLISHABLE_KEY`
  values; the deployable bundle must be built with the target project's publishable values.
- No image digests were recorded (no image was built in this environment).
- Everything is offline-verified only: no live marketplace, Crawl4AI service, email, Outlook,
  Slack, dot or MCP Events activation took place (see ACTIVATION_GATES.md).
