# Changelog

The build waves in order, derived from `git log --oneline` and the wave documentation
(docs/schema.md sections 9 to 11, docs/decisions/). Dates are commit dates. "WIP checkpoint"
commits are omitted; they only preserved interrupted work. **No wave activated anything against
a real service**: no live crawl, no real e-mail, no Slack, dot or Outlook on a real PC.

## Unreleased (working tree on top of `e5d47f4`, not committed)

### Final verification pass (2026-10-10)

- Every gate re-run on the working tree with fresh counts (docs/qa_evidence.md): lint, format,
  types, schema snapshots, both full Python suites, desktop worker, E2E harness and browser E2E on
  PostgreSQL 16 and 17, dashboard `npm ci`/build/test/lint/audit, `make -n` for every target,
  `--help` for every CLI command the documents name, migration ASCII/`DROP TRIGGER` checks and the
  privacy grep.
- Test fix: the flaky D3 assertion in `tests/integration/v11_runtime/test_reply_escalation.py`
  (a random UUID or millisecond timestamp containing `500`; a non-ASCII token that `json.dumps`
  escaped so it could never fail) now checks a view with ids/timestamps blanked and non-ASCII
  kept, with a test showing both old failure modes. No product code changed.
- Wrong claims corrected: README and docs/runbook.md section 1 said every state-changing CLI
  command needs `--yes` (`crawl once` and the process commands run without it);
  IMPLEMENTATION_STATUS.md said the source register's terms/robots reviews are dated 2026-10-06
  (only three sources have a terms review and only one a recorded robots.txt reading).

### Wave D3: final handoff documentation (2026-10-10)

- README.md, IMPLEMENTATION_STATUS.md (milestones M0-M8 and M7a, U1-U12, what is not done or not
  verified, known limitations, review findings and open items, spec 34 contents and final
  status), ACTIVATION_GATES.md (every gate with prerequisites, owner/operator steps, verification
  and evidence; tooling gaps; PROPOSED values; evidence log), SECURITY.md, this changelog,
  docs/acceptance_matrix.md (spec 31 rows plus the 37.10 delta tests) and docs/qa_evidence.md
  (exact commands and measured counts) written from the code and fresh test runs.
- No source code changed.

### Wave D2: final review findings (2026-10-10)

- Seller-contact evidence producer: detail ingest links the seller, verifies the ad-shown
  recipient and resolves the inquiry language (F1).
- MK market evidence: `suv-deals market import` and `mk_comparable` detail routing (F2).
- Activation canary transport on `outlook_local` (desktop worker list/claim/send/report/reply,
  `/v1/mail-workers/canary-intents`) and the reservation gate `activation_canary_incomplete`,
  shown by `inquiries status`, `doctor` and the dashboard (F3, OPS-04).
- v1.1 activation gates `seller_email_sender`, `seller_inquiry`, `seller_reply_slack_route`
  seeded as `blocked` (F4).
- First handoff documents (F5); the release verifier covers the desktop worker, the dashboard and
  the browser E2E (`--with-e2e`, `--skip-dashboard`; F6, OPS-08).
- Tests for scheduler downtime catch-up (F7) and for a new listing revision arriving before a
  review submit in the browser (F8); the live smoke documented as the gated `crawl once`
  procedure (F9); the vehicle-documents limitation documented (F10).
- One claimant per Outlook send intent and a per-store desktop worker id (SEC-1); crawler INPUT
  firewall rules and fixed bridge names (SEC-3, OPS-07).
- Worker default queues cover every job type (OPS-01); blank optional settings mean unset
  (OPS-02); "not activatable (no adapter)" (OPS-03); doctor requires the Slack settings for reply
  signals (OPS-05); ECB FX refresh by the reconciler, `suv-deals fx refresh` and `fx record`
  (OPS-06); `sender-binding store-secret` for the optional `gmail_api` grant (OPS-09, partial).

### Wave D1: open wave C review items (2026-10-10)

- A resume that removes suppressions must carry the count the owner saw
  (`expected_removable_suppressions`).
- Mailbox health and monitoring account for expired or revoked worker credentials.
- Atomic canary claim (`canaries_repo.claim_for_send`) re-checking every database gate in the
  committing statement.
- A muted coalesced reply signal (its carrier ended `dead_letter`/`cancelled`) is re-emitted once.
- Fixture-lineage refusal at reservation and dispatch in the persistence layer.
- Read-only `GET /api/activation/canary-evidence` and the dashboard rows 4-6.
- The inquiry control view reports the process-level gate; `automatic_inquiries_possible` and
  `sending_possible` also need room under both rolling caps (`caps_leave_room`).

## Wave C: v1.1 review items (2026-10-10, `fa283cc`, `e5d47f4`)

- Migration `20261008000200_inquiry_hardening` (applied to the hosted project).
- C1 persistence: fixture lineage refused at the worker's claim; 7-day seller cooldown floor in
  domain, repository and a CHECK; automatic mode needs a verified configured sender and an active
  authorization; a refusal after a granted claim is never proof of non-submission; reply-signal
  flood control (one undelivered signal per inquiry, per-inquiry 24 h cap, per-mailbox hourly
  ingest cap); credential revocation tombstones mailbox bindings; quarantined text owner-only;
  typed waiting reasons; `jobs resolve-blocked`; the owner-controlled activation canary table.
- C2 interfaces: resume bound to the suppressions the owner saw, candidates screening-rejected
  filter, per-credential reply upload limit, CLI `jobs` and `canary`, doctor checks.
- C3 dashboard: waiting reasons, readiness, signal and credential state, canary evidence.

## Wave B2: v1.1 runtime and interfaces (2026-10-08, `19b709b`, `42f9c4b`)

- Migration `20261008000100_inquiry_quota_backstop` (quota counted at the latest hand-over).
- Jobs `seller_inquiry_plan` / `_send` / `_reconcile` and `seller_reply_process`; `outlook_local`
  intents only to a live, fresh desktop worker; `gmail_api` sends outside transactions; uncertain
  results never resent; `seller.reply.received` to its own Slack category only.
- `/v1/mail-workers/*` for the desktop worker; dashboard inquiry, reply, control, worker health,
  lifecycle and 15-day evaluation routes; MCP `seller_inquiries_get`, `seller_replies_get`,
  `seller_inquiries_pause`; CLI for mail-worker credentials, sender bindings and inquiry controls.
- Dashboard inquiry screens without any approve or send control.

## Wave B1: v1.1 persistence (2026-10-07, `a3b1b79`, `ed55313`, `09a193c`, `8e81128`)

- Contracts and consolidation: inquiry caps bounded at start-up, 85 database guard refusals mapped
  to typed errors, mail-worker wire models matching the desktop worker field by field, fixture
  lineage frozen at ingest, `migrate.sh` without a password in argv.
- Repositories for sellers, inquiries, sender bindings, send intents, mail workers, replies and
  availability; one reservation per real vehicle/seller pair across sites; caps 2/24 h and
  5/15 days; uncertain sends blocked, never requeued.
- Migrations `20261007000100`, `20261007000200`, `20261007000400`; migration 1000 made pure ASCII
  and migration 0200 free of `DROP TRIGGER` for the Supabase connector; the v1.1 migrations
  applied to the hosted project (docs/schema.md section 9).

## Wave 4: dashboard (2026-10-07, `5711c6c`)

- Private React 19 + Vite 8 dashboard: overview, candidates, detail, economics, review queue and
  case, sources, settings; same-origin `/api`; CSP; no secret key in the bundle; idempotent review
  mutations; mock Supabase Auth and a real backend for Playwright E2E.

## Wave 3: runtime, API, MCP, CLI and operations (2026-10-07, `5d311e7`)

- Scheduler slots, discovery/detail/valuation handlers, runner, reconciliation and the outbox
  dispatcher with lease fencing; the dashboard API with Supabase JWT auth; the MCP server
  (Streamable HTTP, read and review tools, native MCP Events); the `suv-deals` CLI; Dockerfile,
  Compose files, backup/restore/verify scripts and the runbook.

## Spec v1.1 wave A: seller-inquiry domain (2026-10-07, `670be85`)

- Inquiry readiness separate from investment readiness, versioned DE/IT/FR/EN templates and the
  Macedonian preview with a scope validator, evidence-based language, exact-listing recipient
  verification, cross-site identity, state machine, rate caps, dispatch preflight.
- Reply classification and correlation by Message-ID headers, claim extraction, Macedonian
  summary, lifecycle and the 15-day evaluation model.
- Migration `20261006001000_seller_inquiries`; MIME builder; Gmail API, Microsoft Graph and
  `outlook_local` providers; the desktop Outlook worker core.

## Wave 2b: repositories and read queries (2026-10-07, `86e41d0`)

- Workspaces, configuration, sources, listings ingest, clusters, evidence, market, FX, tax rule
  sets, cost profiles, valuations, review cases with same-transaction outbox events, notes,
  destination bindings, event subscriptions; read queries for every dashboard and MCP view.

## Switzerland (2026-10-06, `21977a1`)

- CH as an acquisition market: MFK/TUV/revisione wording, MWST/TVA/export wording, CH cost lines,
  disabled CH candidate sources and a CH dealer template (docs/markets/switzerland.md).

## Wave 2a: persistence core and read models (2026-10-06, `0c555bb`)

- Durable jobs with SKIP LOCKED claims, fenced heartbeats and reaper; transactional outbox with
  uncertain handling and fixture blocking; idempotency; audit; frozen query snapshots; host budget
  gate; activation gates; the 12 MCP tool input models and resolved JSON schemas; migration 0950.

## Spec v1.1 adoption (2026-10-06, `e58c688`, `fb65749`)

- Specification v1.1 added; ADR 0002 (additive adoption; the owner's sender mailbox and the
  default `outlook_local` path); inquiry/reply enums, scopes and settings.

## Wave 1b: crawl infrastructure, adapters and integrations (2026-10-06, `d888637`, `b965dc8`)

- Crawl4AI 0.9.4 REST client, URL policy, RFC 9309 robots, rate limits, parser health.
- Generic schema.org dealer adapter, fixture crawl client, adapter registry with activation gates,
  synthetic DE/IT/CH fixture suites, placeholder adapters for unverified marketplaces and the
  optional mobile.de API; MCP Events provider, safe HTTP, secret box, Slack fallback; logging with
  redaction, metrics, audit; docs/source_access_register.md and docs/notification_bridge.md.

## Wave 1a: deterministic domain and database (2026-10-06, `e2c8248`, `3af592e`, `956613a`)

- Parsing, identity, screening, comparables, tax engine, costs, valuation, ranking, reviews,
  notifications, pagination and FX with unit and property tests.
- Migrations 0100 to 0900 with RLS, grants and the backend role; database tests on PostgreSQL 16
  and on 17.11 with a Supabase-like non-superuser owner; `.env.example`; migrations 0100 to 0900
  applied to the hosted project.

## Foundation (2026-10-06, `c6996fe`, `4ccfa24`, `ba6fd67`, `f727e4a`)

- Project skeleton, typed core domain contracts, business-rule profiles, real-PostgreSQL test
  harness, the architecture contract and ADR 0001; the shared SSRF guard and async database
  access; the specification and verified research notes. (`fbb9a8f` initialised the empty
  repository.)
