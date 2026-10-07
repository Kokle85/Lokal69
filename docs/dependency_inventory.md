# Dependency inventory

Exact versions this build was tested with (spec §29, §34). Source of truth for Python packages:
`uv.lock` (lock revision 3, `requires-python = ">=3.12, <3.14"`); `make install` runs
`uv sync --frozen`, so a clean install reproduces exactly these versions. Re-record this file
whenever `uv.lock`, an image tag/digest or a tested server version changes. Recorded 2026-10-07.

## 1. Toolchain and runtimes

| Component | Version | Where verified |
|---|---|---|
| Python | 3.13.16 (`.python-version` = 3.13) | `uv run python --version` in the build environment |
| uv | 0.11.32 | `uv --version` |
| PostgreSQL (tests) | 16.15 (port 5432) and 17.11 (port 5433, `TEST_DATABASE_MIGRATOR_ROLE=suv_migrator`) | `make test-db` runs every database test on both |
| Supabase project `Lokal69- Sub` | PostgreSQL 17.11, eu-west-1 | docs/schema.md section 9 |
| Supabase CLI local stack | `major_version = 17` in `supabase/config.toml` | configuration only; not run here |
| pg_dump / pg_restore / psql | matching the server major (`/usr/lib/postgresql/<major>/bin`, else `PG_BIN_DIR`) | `scripts/backup.sh`, `scripts/restore_check.sh` |

## 2. Container images

| Image | Reference | Status |
|---|---|---|
| Application base | `python:3.13-slim` (Dockerfile `PYTHON_IMAGE`) | tag pinned; **pin the verified digest at release** (`python:3.13-slim@sha256:...`) |
| uv binary | `ghcr.io/astral-sh/uv:0.11.32` (Dockerfile `UV_IMAGE`) | tag pinned; pin the verified digest at release |
| Application image | built from `Dockerfile` | its digest is recorded per release (`RELEASE_IMAGE_DIGEST`, `scripts/verify_release.sh`) |
| Crawler | `unclecode/crawl4ai:0.9.4` | tag pinned; production requires `CRAWL4AI_IMAGE_DIGEST` (compose refuses to start without it). Never `latest`. |

No image digest is recorded yet: none was built or pulled in the implementation environment.

## 3. External contracts (verified research, not memory)

| Contract | Version | Reference |
|---|---|---|
| Crawl4AI self-hosted REST server | 0.9.4 (`/health` reports `"version": "0.9.4"`; client pins `EXPECTED_CRAWLER_VERSION`) | docs/research/crawl4ai_rest_contract.md |
| MCP Python SDK | `mcp` 2.3.0 + `mcp-types` 2.3.0 (low-level `Server`, Streamable HTTP) | docs/research/mcp_python_sdk.md |
| MCP protocol revision | 2026-07-28 (`LATEST_PROTOCOL_VERSION`; handshake versions 2024-11-05 … 2025-11-25 still served) | docs/research/mcp_python_sdk.md |
| MCP Events (OpenAI dots/ChatGPT) | webhook delivery + callback verification only | docs/research/mcp_events_and_webhooks.md |
| Standard Webhooks | `standardwebhooks` 1.1.0 | docs/research/mcp_events_and_webhooks.md |
| Supabase Auth JWT / JWKS, Data API exposure | checked 2026-10-06 | docs/research/frontend_and_supabase.md |

## 4. Python runtime dependencies (`uv.lock`)

Direct dependencies (pyproject `[project.dependencies]`) and their locked versions:

| Package | Locked | Constraint |
|---|---|---|
| pydantic | 2.13.5 (pydantic-core 2.46.5) | >=2.13,<3 |
| pydantic-settings | 2.15.0 | >=2.11,<3 |
| pyyaml | 6.0.3 | >=6.0.2,<7 |
| psycopg[binary,pool] | 3.3.6 (psycopg-binary 3.3.6, psycopg-pool 3.3.3) | >=3.3,<4 |
| httpx | 0.28.1 (httpcore 1.0.9, h11 0.16.0) | >=0.28,<0.29 |
| fastapi | 0.142.2 | >=0.142,<0.143 |
| starlette | 1.7.0 | >=1.7,<2 |
| uvicorn[standard] | 0.54.0 (uvloop 0.23.0, httptools 0.8.0, websockets 17.2, watchfiles 1.3.0, python-dotenv 1.2.4) | >=0.54,<0.55 |
| click | 8.5.0 | >=8.1,<9 |
| pyjwt[crypto] | 2.15.1 | >=2.15,<3 |
| cryptography | 50.0.2 (cffi 2.1.1, pycparser 3.0) | >=46 |
| standardwebhooks | 1.1.0 | >=1.1,<2 |
| mcp | 2.3.0 (mcp-types 2.3.0, httpx2 2.13.1, httpcore2 2.13.1, sse-starlette 3.5.0, jsonschema 4.26.0, opentelemetry-api 1.45.1, python-multipart 0.0.32) | >=2.3,<2.4 |
| prometheus-client | 0.26.0 | >=0.23,<1 |
| lxml | 6.1.3 | >=6.0,<7 |
| anyio | 4.15.1 | >=4.10,<5 |

Other locked transitive packages of the runtime closure: annotated-doc 0.0.5, annotated-types
0.8.0, attrs 26.1.0, certifi 2026.7.22, httpx2-jsfetch 1.0, idna 3.20, jsonschema-specifications
2025.9.1, referencing 0.37.0, rpds-py 2026.9.1, truststore 0.10.4, typing-extensions 4.16.0,
typing-inspection 0.4.4, tzdata 2026.5, pywin32 312 (Windows only).

## 5. Python development dependencies

| Package | Locked |
|---|---|
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |
| pytest-cov | 7.1.0 |
| hypothesis | 6.168.5 |
| respx | 0.23.1 |
| ruff | 0.16.10 |
| mypy | 1.20.2 |
| types-pyyaml | 6.0.12.20260906 |
| freezegun | 1.5.5 |

## 6. Other components

| Component | State |
|---|---|
| Dashboard (`dashboard/`, work in progress in a parallel package; re-record when it lands) | `package-lock.json` lockfileVersion 3, exact pins in `package.json`; Node `>=22.22.0` (tested with Node 22.22.0, npm 10.9.4). Runtime: `@supabase/supabase-js` 2.117.3, `react` / `react-dom` 19.3.0, `react-router` 8.4.0. Development: `vite` 8.3.3, `@vitejs/plugin-react` 6.1.2, `typescript` 6.0.3, `vitest` 5.0.3, `jsdom` 29.1.1, `@testing-library/react` 16.3.3, `@testing-library/dom` 10.4.2, `@testing-library/jest-dom` 7.0.1, `@testing-library/user-event` 14.6.7, `@playwright/test` 1.56.1, `oxlint` 1.87.0, `@types/node` 22.20.5, `@types/react` / `@types/react-dom` 19.3.0. `make dashboard-install` runs `npm ci` (lockfile only). Research pins: docs/research/frontend_and_supabase.md section 2. |
| Local Outlook reply worker (`desktop/outlook-bridge/requirements-windows.txt`) | pydantic 2.13.5, httpx 0.28.1, PyYAML 6.0.3 match `uv.lock`; it pins `pywin32==311` while `uv.lock` resolves `pywin32` 312 (Windows-only, lazily imported) — re-verify at activation. |

## 7. How to refresh

```bash
uv lock --check            # the lock matches pyproject
uv tree --frozen           # dependency tree with versions
uv run python --version; uv --version
psql "$PG16_ADMIN_URL" -Atc 'show server_version'; psql "$PG17_ADMIN_URL" -Atc 'show server_version'
scripts/verify_release.sh  # records lock hashes, versions and migration hashes per release
```

Read the current Supabase changelog before changing runtime dependencies (spec §29); do not pin
old tutorial versions.
