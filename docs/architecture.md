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

Spec v1.1 (section 37, ADR 0002): one automatic initial inquiry per verified vehicle/seller pair
under the owner's standing authorization (no per-message approval), and the reply route back.
Nothing is sent unless `SELLER_INQUIRY_MODE=automatic` (process) and the workspace controls are
`automatic`, the kill switch is off, the standing authorization is effective and the sender
binding is verified; fixture-lineage listings never reserve or send.

```text
valuation pipeline (new revision) / reconciliation sweep
  -> ops.jobs seller_inquiry_plan (workers.inquiry_handlers)
     readiness (domain.inquiries) -> reserve: quota debit + immutable binding (inquiries_repo)
  -> ops.jobs seller_inquiry_send: dispatch preflight + DB guards, attempt committed BEFORE I/O
     outlook_local (default): send intent = the committed running attempt (send_intents_repo)
       -> GET /v1/mail-workers/send-intents -> desktop worker (classic Outlook, owner's PC)
       -> POST .../claim (fresh revalidation: kill_switch | not_now | intent_invalid) -> .Send
       -> POST .../report -> inquiries_repo.record_outcome (uncertain stays uncertain)
     gmail_api (optional): provider call outside any transaction -> record_outcome
  -> reconciliation: reaped/uncertain attempts -> seller_inquiry_reconcile (evidence only, never
     a blind resend); proven-unsent failures -> one guarded retry per attempt (max 3)

seller reply in the owner's mailbox
  -> desktop worker (local correlation only; unrelated mail never uploaded)
  -> POST /v1/mail-workers/replies -> app.seller_replies + ops.mail_ingest_dedup
     + ops.outbox seller.reply.received + ops.jobs seller_reply_process (one transaction)
  -> dispatcher -> Slack route of category seller_reply ONLY (ids + dashboard link; never MCP
     Events, which stay candidate discovery only) -> dot -> MCP seller_replies_get
  -> seller_reply_process (workers.reply_handlers): state steps, recalculation,
     seller_reply.owner_alert (category owner_alert) for decisions/opportunities
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
| `domain/parsing.py` | Locale number/price/mileage/date parsing (DE/IT/CH/MK), technical-inspection wording (HU/TÜV, MFK, revisione) | money, listings |
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
| `views/*` | Read models (pydantic) shared by the dashboard API and MCP tools; address/quarantined-text visibility rules (`views.inquiries`) | domain |
| `cli.py`, `cli_commands/*` | `suv-deals` operator CLI (processes, doctor, db, sources, credentials, `inquiries`, `sender-binding`, `mail-worker`, `evaluation`) | persistence, workers, api |

Spec v1.1 seller-inquiry and reply modules (section 37, ADR 0002):

| Path | Responsibility | Depends on |
|---|---|---|
| `domain/inquiries.py` | Inquiry state machine, readiness, caps/cooldown, dispatch preflight, retry policy (pure) | filters, costs, seller_contacts, language, seller_templates |
| `domain/seller_templates.py` | Versioned deterministic templates and the template-scope validator | seller_contacts, taxonomy, notifications |
| `domain/language.py` | Evidence-based inquiry language (de/it/fr/en; unsupported is held) | provenance, notifications |
| `domain/seller_contacts.py` | Exact-listing contact evidence, address canonicalisation, seller identity | listings, notifications |
| `domain/replies.py` | Reply classification, correlation, dedup, claims, processing decisions, `dashboard_reply_url` | listings, parsing, notifications |
| `domain/lifecycle.py`, `domain/evaluation.py` | Availability/freshness lags; the 15-day quality evaluation | listings, costs |
| `integrations/seller_email.py`, `integrations/email_providers/*` | Provider registry; `outlook_local` (default, desktop worker), `gmail_api` (optional), `microsoft_graph` (skeleton) | domain.inquiries, domain.replies, mime_builder |
| `integrations/mime_builder.py` | RFC 5322/MIME rendering of the stored, immutable inquiry | seller_templates, seller_contacts |
| `integrations/secret_box.py` | AES-GCM envelopes (MCP Events secrets, sealed sender OAuth grants) | – |
| `persistence/sellers_repo.py`, `inquiries_repo.py`, `sender_bindings_repo.py` | Seller identity/merges, reservation/queue/dispatch/outcome/retry under the controls lock, sending identities | domain |
| `persistence/send_intents_repo.py` | Outlook send intents (derived from attempts), the worker claim and reports | inquiries_repo |
| `persistence/mail_workers_repo.py`, `replies_repo.py`, `availability_repo.py` | Worker identity/bindings sync/health, correlated reply ingest, availability evidence | domain |
| `persistence/queries/inquiries.py`, `queries/lifecycle.py` | Paged reads for the dashboard/MCP (inquiries, replies, health, evaluation, lags) | views |
| `workers/inquiry_handlers.py` | `seller_inquiry_plan` / `_send` / `_reconcile` jobs | persistence, integrations |
| `workers/reply_handlers.py` | `seller_reply_process` job: state steps, recalculation wait, owner alerts | persistence |
| `workers/reconciliation.py` | Also the bounded inquiry/reply sweeps (plan, send, guarded retry, reconcile, reply jobs) | persistence |
| `workers/dispatcher.py` | Also the v1.1 category signals: `seller.reply.received` / `seller_reply.owner_alert` -> Slack route of their category only | integrations.slack |
| `api/mail_worker_routes.py` | `/v1/mail-workers` (mailbox-bound `suvmail_` credential) | persistence |
| `api/inquiry_routes.py` | Dashboard inquiry/reply/control/health/lifecycle/evaluation routes | persistence, views |
| `mcp/tools.py` (`V11_TOOLS`) | `seller_inquiries_get`, `seller_replies_get`, `seller_inquiries_pause` (no send/resume tool) | persistence, views |
| `desktop/outlook-bridge/` | Windows desktop worker (classic Outlook Object Model): local correlation, claim-then-send, reports, heartbeats; wire models in `outlook_bridge/wire.py` | the backend only over `/v1/mail-workers` |
| `dashboard/` | Private React dashboard (BFF routes only, ADR 0001) including the v1.1 inquiry screens | `/api/...` |

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
  -> `app.valuations` -> `app.review_cases` -> `ops.outbox`. The seller-inquiry tail
  (`app.seller_inquiry_controls` first, never `app.listings` after it) is in docs/schema.md 11.5.

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
- `tests/contracts`: MCP tool and API schema contracts; JSON schema snapshots in `schemas/`; the
  backend/desktop mail-worker wire parity and backend dashboard links vs. dashboard routes.
- `tests/integration/v11_*`, `tests/integration/mail_worker_e2e`: the v1.1 database, repositories,
  runtime workers and the real desktop worker against the real app (synthetic `example.invalid`
  data only; nothing is ever sent).
- `desktop/outlook-bridge/tests`: the desktop worker with in-memory fakes (`outlook_bridge.testing`).
- `tests/adversarial`: SSRF, injection, signature forgery, XSS payloads.
- `tests/e2e`: browser flows (Playwright) against a locally running stack.
- `tests/smoke`: live checks, marker `live`, never run automatically.
