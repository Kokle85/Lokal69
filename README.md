# SUV deal system

A private, auditable deal-discovery system for one owner. It watches permitted European listing
sources for SUVs that can be bought in Germany, Italy or Switzerland for EUR 2,500 to 3,000
(inclusive) with strictly less than 200,000 km, compares them with North Macedonian resale
evidence (research band EUR 8,000 to 10,000), computes landed cost and contribution with explicit
unknowns, and puts the candidates in a review queue for the owner and his assistant dot (MCP).
Under the owner's standing authorization it can send ONE automatic initial inquiry per verified
vehicle/seller pair (availability, documents, lowest/final price) and route the seller's reply back
through the owner's own classic Outlook. The contract is
[docs/spec/suv-deal-system-build-spec.md](docs/spec/suv-deal-system-build-spec.md) (version 1.1).

> **Nothing is active.** Every source is disabled and every external effect is off by default. No
> live crawl, Crawl4AI service, real mailbox, Outlook on a real PC, Slack, dot or deployed service
> has been used. What is built and tested, and what is still blocked:
> [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) and [ACTIVATION_GATES.md](ACTIVATION_GATES.md).

## Architecture in brief

One Python codebase (`src/suv_deals`), one PostgreSQL database (Supabase in production) as the
single source of truth for records, queues, leases, reviews and the outbox, and separately
runnable processes:

```text
config/sources/*.yaml -> scheduler (15-min slots) -> ops.jobs
  -> worker: adapter via Crawl4AI REST under URL policy, robots and budgets
  -> listings, revisions, field evidence -> screening -> valuation (comparables, costs, tax)
  -> review case + outbox event (same transaction)
  -> dashboard API (/api) and MCP endpoint (/mcp) for review; dispatcher -> MCP Events / Slack
     only when an activation gate passed

seller inquiry (spec 37): plan -> reserve (caps, cooldown, dedup) -> send intent
  -> desktop worker on the owner's PC -> classic Outlook .Send -> report
seller reply: classic Outlook -> desktop worker (local correlation only) -> /v1/mail-workers
  -> outbox -> private Slack signal -> dot -> MCP seller_replies_get
```

The browser and MCP clients never touch the database directly (ADR 0001, backend-for-frontend
with row-level security). Details: [docs/architecture.md](docs/architecture.md).

## Repository map

| Path | What it is |
|---|---|
| `src/suv_deals/domain/` | Pure, deterministic rules: parsing, identity, screening, comparables, costs, tax engine, valuation, reviews, seller inquiries, templates, language, replies, lifecycle, evaluation |
| `src/suv_deals/adapters/`, `crawling/` | Source adapters (generic schema.org dealer adapter; placeholders for unverified sites), Crawl4AI client, URL policy, robots, rate limits, parser health, scheduler, ingest |
| `src/suv_deals/persistence/` | psycopg 3 repositories, jobs, outbox, idempotency, audit, activation gates, read queries |
| `src/suv_deals/workers/` | Job runner, handlers, reconciliation, outbox dispatcher, inquiry and reply handlers |
| `src/suv_deals/api/`, `mcp/`, `views/` | Dashboard API (FastAPI), mail-worker API, MCP server (official SDK), shared read models |
| `src/suv_deals/integrations/` | ECB FX, MCP Events webhooks, Slack, MIME builder, e-mail providers, secret box, safe HTTP |
| `src/suv_deals/cli.py`, `cli_commands/` | The `suv-deals` operator CLI (doctor, db, sources, crawl, inquiries, sender binding, mail worker, canary, market, fx, jobs, ...) |
| `supabase/migrations/` | 16 forward-only SQL migrations (applied to the hosted project through `20261008000200`) |
| `config/` | Business configuration, search profiles, source registry, taxonomy, cost profiles, tax-rule examples, the standing authorization record |
| `schemas/` | Exported JSON schemas of the MCP tools and API responses (checked against the code) |
| `dashboard/` | Private React dashboard (Vite, Supabase Auth, Vitest, Playwright) |
| `desktop/outlook-bridge/` | Windows desktop mail worker for classic Outlook (Linux-testable with fakes) |
| `tests/` | Unit, property, adapter, adversarial, contract, API, MCP, CLI, integration (real PostgreSQL) and E2E-harness tests |
| `scripts/` | Migrate, rollback policy, backup, restore check, release verification, schema export, log redaction |
| `docs/` | Specification, architecture, schema, API contract, runbook, activation guides, decisions, research |

## Quickstart (local development)

Requirements: [uv](https://docs.astral.sh/uv/) 0.11 (it provides Python 3.13), PostgreSQL 16
on `127.0.0.1:5432` and 17 on `127.0.0.1:5433` with the local test superuser `suv`/`suv` (local
test clusters only), Node 22.22+ and npm 10 for the dashboard. Nothing below needs network
access to a listing site, a mailbox, Slack or Supabase.

```bash
uv sync --frozen                     # = make install; locked versions only
make db-local-start                  # start the local PostgreSQL 16 and 17 clusters (or print how)
uv run suv-deals doctor --no-db      # presence-only configuration report (never prints values)

# Tests (database tests create and drop their own temporary databases)
uv run pytest -q                     # everything under tests/, PostgreSQL 16
TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres \
  TEST_DATABASE_MIGRATOR_ROLE=suv_migrator uv run pytest -q   # PostgreSQL 17, Supabase-like owner
uv run pytest -q desktop/outlook-bridge/tests                 # desktop worker, fake Outlook
make lint typecheck schemas-check    # ruff, mypy --strict, JSON schema snapshots

# Offline smoke: CLI, configuration, tax-rule files, the fixture end-to-end pipeline
make smoke-local

# Fixture mode: a local database with the synthetic fixture sources, network and notifications OFF
make db-migrate-local                # creates/migrates the LOCAL database suv_dev (loopback only)
#   one synthetic local owner and workspace (the Supabase emulation provides auth.users):
U=$(psql postgresql://suv:suv@127.0.0.1:5432/suv_dev -Atc "insert into auth.users (id, email)
     values (gen_random_uuid(), 'dev-owner@example.invalid') returning id" | head -1)
MAINTENANCE_DATABASE_URL=postgresql://suv:suv@127.0.0.1:5432/suv_dev \
  uv run suv-deals --no-env-file bootstrap owner --user-id "$U" --workspace-name "Local dev" --yes
DATABASE_URL=postgresql://suv:suv@127.0.0.1:5432/suv_dev DATABASE_SET_ROLE=suv_backend \
  uv run suv-deals --no-env-file config apply --reason "local development" --yes
make dev                             # syncs the fixture sources; api (127.0.0.1:8000) + worker + scheduler

# Dashboard
cd dashboard && npm ci && npm test && npm run lint && npm run typecheck
cp .env.example .env.local && npm run dev   # http://127.0.0.1:5173, /api proxied to :8000
cd .. && make e2e                    # Playwright vs mock Supabase Auth + real backend (PostgreSQL 16)
```

`make dev` refuses without a workspace in `suv_dev` ("no active workspace"), hence the bootstrap
lines above (verified on 2026-10-10). It starts neither the reconciler nor the dispatcher; run
`uv run suv-deals reconcile --loop` separately when needed. Without `SUPABASE_URL` the API answers
`/healthz` but refuses every `/api` request, `/readyz` reports `config: not_configured`, and the
MCP endpoint stays disabled until its settings are present (docs/runbook.md 2.1); the browser E2E
(`make e2e`) runs the whole stack with a mock Supabase Auth instead. `suv-deals doctor` exits 1
while required settings are missing and lists them. The exact commands and counts of the last full
verification: [docs/qa_evidence.md](docs/qa_evidence.md).

The desktop worker itself runs only on Windows with classic Outlook; its installation is part of
the owner's activation ([desktop/outlook-bridge/README.md](desktop/outlook-bridge/README.md)).

## Safety defaults

| Switch | Default | While it is off |
|---|---|---|
| `SOURCE_NETWORK_ENABLED` | `false` | no real source is fetched; only `mode: fixture` sources run against saved files |
| `ALLOW_EXTERNAL_NOTIFICATIONS` | `false` | nothing leaves the system; outbox events are recorded as blocked |
| `EVENT_BRIDGE_ENABLED`, `MCP_EVENTS_ENABLED`, `NOTIFICATION_PROVIDER` | `false`, `false`, `disabled` | no activation route for dot |
| `SELLER_INQUIRY_MODE` | `disabled_until_sender_ready` | no seller e-mail; even `automatic` also needs the workspace mode, kill switch off, the standing authorization, a verified sender and a complete activation canary |
| `SELLER_EMAIL_CANARY_SEND_ENABLED` | `false` | the owner's one-time canary cannot be sent |
| `FX_FETCH_ENABLED`, `LLM_EXTRACTION_ENABLED` | `false` | no ECB fetch; no LLM call |

Every source in `config/sources/` is `enabled: false`. Fixture data never notifies. Secrets come
only from the environment or a secret store, never from the repository; the owner's real mailbox
address never appears in it (tests use `example.invalid`). Every state-changing administrative CLI
command (configuration, sources, credentials, sender binding, inquiry controls, canary, market and
FX records, jobs, migrations) needs `--yes`; the process commands (`api serve`, `worker`,
`scheduler`, `reconcile`, `dispatcher`) and the gated, budget-bounded `crawl once` run without it.
Compose deployments enable a switch only through a dedicated `SUV_DEALS_*` variable
exported for that deployment (docs/runbook.md section 1). Security model: [SECURITY.md](SECURITY.md).

## Documentation

| Topic | Document |
|---|---|
| What is done, verified, blocked; spec 34 final status | [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) |
| Every activation gate: prerequisites, owner/operator steps, verification, evidence | [ACTIVATION_GATES.md](ACTIVATION_GATES.md) |
| Spec 31 test matrix and the 37.10 delta tests | [docs/acceptance_matrix.md](docs/acceptance_matrix.md) |
| QA commands and the latest measured counts | [docs/qa_evidence.md](docs/qa_evidence.md) |
| Security, secrets, residual risks | [SECURITY.md](SECURITY.md) |
| History of the build waves | [CHANGELOG.md](CHANGELOG.md) |
| Architecture and module contracts | [docs/architecture.md](docs/architecture.md), [docs/decisions/](docs/decisions/0001-bff-only-data-access.md) |
| Database schema, RLS, applied Supabase state | [docs/schema.md](docs/schema.md) |
| Dashboard, mail-worker and MCP API contracts | [docs/api_contract.md](docs/api_contract.md) |
| Operations: processes, setup, CLI, incidents, backup, release, activation | [docs/runbook.md](docs/runbook.md) |
| Connecting dot over MCP | [docs/connect_mcp.md](docs/connect_mcp.md) |
| MCP Events and the Slack routes | [docs/notification_bridge.md](docs/notification_bridge.md) |
| Seller e-mail activation and templates | [docs/seller_email_activation.md](docs/seller_email_activation.md), [docs/seller_email_templates.md](docs/seller_email_templates.md) |
| Source terms, robots and activation checklist | [docs/source_access_register.md](docs/source_access_register.md) |
| Switzerland as an acquisition market | [docs/markets/switzerland.md](docs/markets/switzerland.md) |
| Tax rule approval | [docs/tax_rule_approval.md](docs/tax_rule_approval.md) |
| Tested dependency versions | [docs/dependency_inventory.md](docs/dependency_inventory.md) |
| Dashboard | [dashboard/README.md](dashboard/README.md) |
| Desktop mail worker | [desktop/outlook-bridge/README.md](desktop/outlook-bridge/README.md) |
