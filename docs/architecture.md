# Architecture and module contracts

This document is the implementation contract for the SUV deal-discovery system.
The product contract is the specification (sections referenced as "spec §N").
Read this before changing code; keep it current when contracts change.

## Shape

A modular monorepo with a Python domain layer (`src/suv_deals`). One backend
service exposes the dashboard API (`/api/...`) and the MCP endpoint (`/mcp`).
The scheduler, worker and outbox dispatcher are separately runnable processes
(`suv-deals scheduler|worker|dispatcher`). PostgreSQL (Supabase) is the single source
of truth for records, queues, leases, watermarks, reviews and outbox. A running
browser/crawler process is never the authoritative queue.

```text
config/sources/*.yaml  ->  scheduler (15 min slots, ops.source_schedules)
  -> ops.jobs discovery job (idempotent slot key)
  -> worker: adapter.discover() via CrawlClient (Crawl4AI REST) under URL policy + budgets
  -> app.listing_observations (+ detail jobs for new/changed cards)
  -> worker: adapter.fetch_detail()/parse_detail() -> NormalizedListing
  -> app.listing_revisions (only on semantic change) + field evidence
  -> eligibility screening (domain.filters) -> valuation job
  -> comparables + costs + tax engine -> app.valuations (versioned, unknowns explicit)
  -> app.review_cases (pending) + ops.outbox (same transaction)
  -> dispatcher -> MCP Events webhook / Slack (only when activation gates pass)
  -> dot/owner review via MCP tools or dashboard -> app.review_decisions
```

## Package map and ownership

| Path | Responsibility | Depends on |
|---|---|---|
| `errors.py` | Typed `AppError` + `ErrorCode` (spec §21 codes) | – |
| `clock.py` | Injectable clock; `ensure_utc` | – |
| `settings.py` | Environment settings; presence-only `describe()` | – |
| `domain/enums.py` | All persisted enums | – |
| `domain/money.py` | `Money` (Decimal), `FxRate` with explicit direction | enums, errors |
| `domain/actor.py` | `ActorContext` (workspace, principal, role, scopes, request id) | enums |
| `domain/listings.py` | `NormalizedListing` and sub-models, `semantic_payload/hash`, `sha256_json` | money, provenance |
| `domain/provenance.py` | `FieldProvenance`, `FieldConflict` | enums |
| `domain/profiles.py` | `SearchProfile`, `BusinessConfig`, baseline validation, YAML loader | enums |
| `domain/sources.py` | `SourceConfig`, `RateBudget`, `activation_problems()` | enums |
| `domain/parsing.py` | Locale number/price/mileage/date parsing (DE/IT/CH/MK) | money, listings |
| `domain/identity.py` | URL canonicalisation, identity/card hashes, revision promotion rules | listings |
| `domain/taxonomy.py` | SUV make/model/generation taxonomy matching (`config/vehicle_taxonomy.yaml`) | enums |
| `domain/filters.py` | Deterministic eligibility screening per profile | profiles, money, taxonomy |
| `domain/comparables.py` | MK comparable selection/exclusion/statistics | listings |
| `domain/tax_engine.py` | Declarative, versioned, approval-gated import-tax rules | money |
| `domain/costs.py` | Cost lines, scenarios, cash/landed/ready-to-sell/contribution arithmetic | money, tax_engine |
| `domain/valuation.py` | Valuation assembly, dependency fingerprint, state | costs, comparables |
| `domain/ranking.py` | Transparent deterministic ranking with visible contributions | valuation |
| `domain/reviews.py` | Review/claim state machine (pure) | enums |
| `domain/notifications.py` | Materiality policy, payload builders, safe owner wording | reviews |
| `adapters/base.py` | `SourceAdapter`/`CrawlClient` protocols and DTOs | listings, profiles |
| `adapters/*.py` | Concrete adapters; unverified sites are explicit placeholders | base |
| `crawling/url_policy.py` | SSRF + per-source host/path policy, redirect validation | sources |
| `crawling/robots.py` | robots.txt fetch/parse/obey with stored revision hash | url_policy |
| `crawling/rate_limits.py` | Token bucket / circuit breaker / backoff decisions (pure) | sources |
| `crawling/parser_health.py` | Tripwires on parse outcomes | adapters.base |
| `crawling/scheduler.py`, `discovery.py`, `detail.py` | Job orchestration (DB-backed) | persistence |
| `persistence/*` | psycopg 3 access; every method takes `ActorContext` | domain |
| `workers/*` | Job runner, outbox dispatcher, reaper/reconciliation | persistence, crawling |
| `api/*` | Dashboard BFF (FastAPI) | persistence, domain |
| `mcp/*` | MCP server (official `mcp` SDK, Streamable HTTP), tools, auth, events | persistence, domain |
| `integrations/*` | FX (ECB), MCP Events webhook delivery, Slack adapter | domain |
| `observability/*` | JSON logging with redaction, Prometheus metrics, audit helpers | – |

## Coding conventions (binding)

- Python 3.13, `from __future__ import annotations`, full type hints; `mypy --strict` and
  `ruff check` must pass for every module.
- Money is `Decimal`/`Money` or integer minor units. Never `float` for money, mileage
  thresholds or FX. Unknown is `None`/`unknown`, never `0`.
- Datetimes are timezone-aware UTC (`ensure_utc`). Naive datetimes are rejected.
- Pydantic v2 models are `frozen=True, extra="forbid"` for contracts.
- Domain modules are pure (no I/O). I/O lives in adapters/crawling/persistence/integrations.
- Every persistence/domain operation that touches workspace data takes an `ActorContext`
  and checks scopes with `actor.require(...)`. No unscoped `get_by_id`, no generic SQL.
- SQL parameters are always bound (`%s`/`%(name)s`), never interpolated. Dynamic
  identifiers only via `psycopg.sql.Identifier` from fixed allow-lists.
- Raise `AppError` subclasses from `errors.py` for expected failures; never leak SQL,
  tokens, URLs with credentials or stack traces in messages.
- Seller-provided text is untrusted data: stored, bounded, escaped on output, never obeyed.
- No network I/O inside a database transaction. Short transactions; global lock order:
  `ops.jobs` row -> `app.sources` -> `app.listings` -> `app.listing_revisions`
  -> `app.valuations` -> `app.review_cases` -> `ops.outbox`.

## Database conventions

- Schemas: `app` (application records), `ops` (queues, auth mapping, audit internals;
  never exposed). See ADR 0001 for the BFF-only access model and RLS for `suv_backend`.
- Migrations: `supabase/migrations/<YYYYMMDDHHMMSS>_<name>.sql`, forward-only,
  expand/contract compatible. Tests apply them on PostgreSQL 16 with
  `supabase/tests/supabase_emulation.sql` providing `auth.users`, `auth.uid()` and the
  `anon`/`authenticated`/`service_role` roles when not running on a Supabase stack.
- UUID PKs (`gen_random_uuid()`), `timestamptz`, composite `(workspace_id, id)` uniqueness
  and composite foreign keys for every cross-table link.
- Money columns: `<name>_minor bigint` + `currency char(3)` (both present or both absent).
- Transactions set `app.workspace_id` (and `app.user_id` when acting for a user) with
  `set_config(..., true)`.

## Tests

- `tests/unit`, `tests/property` (Hypothesis): pure domain, deterministic clocks/IDs.
- `tests/integration`: real PostgreSQL via `tests/db_harness.py` (`db_url` fixture, marker `db`).
- `tests/adapters`: saved fixtures in `tests/adapters/fixtures/<source_key>/` with a
  `MANIFEST.yaml` (source, date, parser version, real/synthetic designation).
- `tests/contracts`: MCP tool and API schema contracts; JSON schema snapshots in `schemas/`.
- `tests/adversarial`: SSRF, injection, signature forgery, XSS payloads.
- `tests/e2e`: browser flows (Playwright) against a locally running stack.
- `tests/smoke`: live checks, marker `live`, never run automatically.
