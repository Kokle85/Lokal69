# Implementation status

Status of the build against [docs/spec/suv-deal-system-build-spec.md](docs/spec/suv-deal-system-build-spec.md)
(version 1.1) on **2026-10-10**, after wave D3 and the final verification pass. Code state: base
commit `e5d47f4` plus the uncommitted waves D1, D2 and D3 and the final verification pass (exact
commands, counts and the working-tree note: [docs/qa_evidence.md](docs/qa_evidence.md)).

States are those of spec 32: `implemented`, `fixture_verified`, `integration_verified`,
`live_verified`, `active`, `blocked`, `not_requested`. Here `fixture_verified` means proven on
saved or synthetic fixtures, and `integration_verified` means proven against a real PostgreSQL
(16 and 17.11) with every external service replaced by a fake. **Nothing is `live_verified` or
`active`.** No marketplace was crawled, no Crawl4AI service was reached, no e-mail was sent or
read, and no Slack, dot or Outlook on a real PC was used. Every source is disabled and every
external effect is off by default.

- The activation steps for each gate: [ACTIVATION_GATES.md](ACTIVATION_GATES.md)
- The spec 31 test matrix with results: [docs/acceptance_matrix.md](docs/acceptance_matrix.md)
- Security: [SECURITY.md](SECURITY.md). History: [CHANGELOG.md](CHANGELOG.md)

## Milestones (spec 33)

| Milestone | Status | Evidence | Missing for its exit criterion |
|---|---|---|---|
| M0 Audit and reuse | done | docs/architecture.md, docs/dependency_inventory.md, docs/source_access_register.md, docs/decisions/0001 and 0002, docs/research/ | - |
| M1 Domain contracts and deterministic tests | done | `src/suv_deals/domain/`; `tests/unit`, `tests/property` (price EUR 2,500-3,000 inclusive, mileage strictly under 200,000 km, unknown is never zero) | - |
| M2 Database and reliability foundation | done | 16 forward-only migrations, RLS on every `app`/`ops` table, durable jobs with leases, idempotency, outbox, audit; `tests/integration/db`, `tests/integration/persistence_core` on PostgreSQL 16 and 17.11 | - |
| M3 Crawl4AI integration and first source | code done, exit **blocked** | Crawl4AI 0.9.4 REST client, URL policy, robots, rate limits, parser health, scheduler; generic dealer adapter `fixture_verified` (`tests/adapters`, `tests/integration/pipeline/test_e2e_fixture_pipeline.py`) | a permitted live search/detail smoke on one real source (gates `existing_crawler`, `source_access`) |
| M4 Additional sources and MK comparables | partial, **blocked** | source registry with 14 disabled sources and their gates; MK evidence import (`suv-deals market import`) and `mk_comparable` detail routing (`tests/integration/pipeline/test_d2_mk_comparable_routing.py`) | 12 of 14 sources have no adapter; mobile.de and AutoScout24 are not crawled (owner decision); no MK evidence in any real workspace |
| M5 Costs and tax framework | done for the framework; real tax readiness **blocked** | versioned tax engine, approvals, cost evidence, FX, scenarios (`tests/unit/test_tax_engine.py`, `test_costs.py`, `test_valuation.py`, `test_fx.py`) | an approved rule set (gate `tax_rules`), owner cost assumptions (gate `cost_assumptions`) |
| M6 Dashboard and MCP | done offline; real client **blocked** | 12 + 3 MCP tools, scopes, schemas (`tests/mcp`, `tests/contracts`); dashboard screens and browser flows (`dashboard/src/test`, `dashboard/e2e`) | a real dot client connection (gate `mcp_authentication`); a deployed dashboard (gate `hosting`) |
| M7 Notifications and optional activation bridge | done offline; activation **blocked** | MCP Events provider and subscriptions, Slack fallback and the selected Slack seller-reply route, destination bindings, dedup, uncertainty handling (`tests/integration/pipeline/test_dispatcher.py`, `tests/integration/v11_runtime`, `tests/mcp/test_events.py`) | delivery receipts and dot's processing evidence; an operator command for destination bindings (ACTIVATION_GATES.md section 19) |
| M7a Bounded automatic seller inquiries | `integration_verified`; activation **blocked** | U1-U12 below; `tests/integration/v11_*`, `tests/integration/mail_worker_e2e`, `desktop/outlook-bridge/tests` | the owner's classic Outlook, the activation canary (rows 4-6) and the Slack -> dot leg; no actual seller e-mail is claimed |
| M8 Release hardening | partial | independent reviews after every wave (security, spec acceptance, operations), the final review findings fixed in D1/D2, this acceptance matrix with passed/blocked/not-run rows | exact-build release report on a commit; restore and rollback drills; image digests |

## Spec 37.10 upgrade items (U1-U12)

Category: *tested offline* (implemented and proven by automated tests here), *activation-pending*
(the remaining evidence needs a live service, account or owner step), *not done*.

| Item | Status | Category | Evidence | What is missing |
|---|---|---|---|---|
| U1 | `implemented` | done, tested offline | Audit and additive adoption plan: docs/decisions/0002-spec-v1.1-adoption.md; v1.0 components and data kept; forward-only migrations (`tests/unit/test_migrations_ascii.py`, `tests/integration/db/test_schema_catalogue.py`) | - |
| U2 | `fixture_verified` (dealer search/detail slice on synthetic sources); live `blocked` | tested offline; activation-pending (live source) | `tests/adapters/test_dealer_search.py`, `tests/adapters/test_dealer_detail.py`, `tests/integration/pipeline/test_e2e_fixture_pipeline.py`; first/last-seen and availability evidence `tests/integration/repos_sources_listings/test_availability_events.py`, `tests/unit/test_lifecycle.py`; lags and coverage gaps `tests/integration/read_queries/test_views.py`, `tests/integration/repos_sources_listings/test_schedules.py` | an actually working live source (gate `source_access`); 12 of 14 sources have no adapter |
| U3 | `integration_verified` | tested offline | inquiry readiness separate from profit readiness; CoC/document unknowns resolvable by the inquiry; hard price/mileage rules kept (`tests/unit/test_inquiries.py`, `tests/integration/v11_inquiries/test_qualification_and_reservation.py`, `tests/integration/v11_runtime/test_plan_and_send.py`) | real readiness needs MK evidence, FX rates and an active source (gate `seller_inquiry`) |
| U4 | `integration_verified` (code); activation `blocked` | tested offline; activation-pending (real account, canary) | sender binding, technical verification from the desktop worker's evidence, standing authorization, no-approval automatic mode, process gate, reservation gate `activation_canary_incomplete` (`tests/integration/v11_inquiries/test_senders_and_contacts.py`, `test_d2_activation_canary_gate.py`, `tests/cli/test_inquiry_commands.py`, `tests/integration/v11_runtime/test_operator_and_policy.py`) | no real account bound or verified; `gmail_api` cannot be activated with this build (OPS-09) |
| U5 | `integration_verified` (synthetic dealer pages) | tested offline; activation-pending (real permitted source) | exact-ad seller, recipient and language evidence produced by detail ingest (`tests/integration/v11_runtime/test_d2_seller_evidence_pipeline.py`, `tests/adapters/test_d2_seller_contact_evidence.py`); Swiss DE/FR/IT from text evidence (`tests/unit/test_language.py`); deterministic DE/IT/FR/verified-EN templates and the informational Macedonian preview (`tests/unit/test_seller_templates.py`, `tests/unit/test_mime_builder.py`) | evidence from a real permitted source; only adapters declaring `seller_contact_evidence` (today the dealer adapter) produce it |
| U6 | `integration_verified` | tested offline | pair dedup reservations across sites and aliases, outbox delivery, caps 2/24 h and 5/rolling 15 days, 7-day cooldown floor, kill switch, bounce/opt-out suppression, one claimant per Outlook intent, uncertain-send reconciliation without blind resend (`tests/integration/v11_db`, `tests/integration/v11_inquiries`, `tests/integration/v11_runtime/test_reconcile_and_sweeps.py`) | - (live delivery is gate `seller_email_sender`) |
| U7 | `fixture_verified`; live `blocked` | tested offline (fakes); activation-pending (owner's PC) | classic-Outlook detection (new Outlook refused), STA thread, interactive-session guard, mailbox/folder binding against fakes (`desktop/outlook-bridge/tests/test_compatibility.py`, `test_sta_runtime.py`, `test_sta_integration.py`, `test_outlook_adapter.py`) | the owner's Windows PC with classic Outlook; the installation is documented (desktop/outlook-bridge/README.md) but not performed |
| U8 | `integration_verified` (real backend + fake Outlook); live `blocked` | tested offline; activation-pending (owner's PC) | NewMailEx plus startup and periodic (120 s) reconciliation, durable SQLite backlog, checkpoints, overlap recovery, moved-message dedup, sleep/offline gaps, health (`desktop/outlook-bridge/tests/test_reconciliation_flows.py`, `test_worker_flows.py`, `test_health_matching.py`, `tests/integration/mail_worker_e2e/test_desktop_worker_e2e.py`) | the 120-second interval checked against the real runtime/provider limits on the owner's PC |
| U9 | `integration_verified` (correlated replies, MCP retrieval); vehicle documents partial | tested offline; document contents not done (manual) | only inquiry-correlated replies are stored through the authenticated API (`tests/integration/v11_replies/test_reply_ingest.py`, `tests/api/test_mail_worker_routes.py`); scoped MCP `seller_replies_get` (`tests/mcp/test_v11_tools.py`); attachment metadata only, identity documents withheld | document contents (CoC, registration, CO2) are verified manually by the owner in the mailbox (docs/seller_email_activation.md section 9) |
| U10 | `integration_verified` (code); live `blocked` | tested offline; activation-pending (Slack, dot) | Outlook -> backend -> outbox -> Slack signal (metadata `suv_deals.seller_reply_received`) with flood control and uncertain-post reconciliation; MCP Events stay candidate discovery only (`tests/integration/v11_runtime/test_signal_delivery.py`, `test_signal_gates.py`, `test_e2e_runtime.py`, `tests/integration/v11_replies/test_reply_signal_flood.py`) | Slack app/channel, destination binding, dot trigger and dot's processing evidence (gate `seller_reply_slack_route`) |
| U11 | `integration_verified` | tested offline; activation-pending (tax and cost approvals) | Macedonian reply summary, deterministic claims (a price is an unaccepted seller quote), availability/evidence updates, valuation recalculation, owner alerts only for decisions or supported opportunities (`tests/unit/test_replies.py`, `tests/integration/v11_runtime/test_reply_escalation.py`, `test_opportunity_decision.py`) | approved tax rules and cost assumptions for evidence-supported recalculation; the Slack owner-alert route is not activated |
| U12 | `blocked` | activation-pending (commit, release report, canary) | the extended suite passes on the working tree (counts in docs/qa_evidence.md); the 15-day evaluation reports zero as zero (`tests/unit/test_evaluation.py`, `suv-deals evaluation report --days 15`); runbook and status updated | an exact-build release report on a commit and the live activation canary |

## What is NOT done or NOT verified

- **Live crawling.** No source was fetched live; no live smoke ever ran. 12 of the 14 registered
  sources have no adapter. No permitted dealer has been selected.
- **mobile.de and AutoScout24 are not crawled.** Work on them was denied by an automated policy
  classifier during this build; whether to pursue them (ideally with permission or an agreement)
  remains the owner's decision. Their terms restrictions are recorded in
  docs/source_access_register.md.
- **Crawl4AI service.** No crawler was reached; `doctor --crawler` and the firewall verification
  have not run against a real service.
- **Real e-mail.** No account is bound or verified, no OAuth consent was granted, no message was
  sent or read. Real seller inquiries sent: 0, uncertain: 0, suppressed: 0. No provider receipt
  exists.
- **Classic Outlook on the owner's PC.** The desktop worker ran only on Linux against an in-memory
  fake Outlook; no Windows installation, credential store or Task Scheduler entry exists.
- **Activation canary.** The `outlook_local` transport is built and tested with fakes; rows 4-6
  (send, Sent Items reconciliation, correlated reply) were never produced. `gmail_api` and
  `microsoft_graph` have no canary transport (`CANARY_TRANSPORT_UNAVAILABLE`).
- **Slack and dot.** No Slack app, channel, destination binding or dot trigger exists; no signal was
  posted; dot never connected to the MCP server; no MCP Events subscription exists.
- **Supabase serving.** The hosted project has the schema but no backend process, user, workspace,
  backup or restore drill.
- **Hosting.** No image was built, no digest recorded, nothing deployed.
- **Tax rules.** No approved, sourced North Macedonian rule set; every real valuation keeps import
  charges unknown.
- **Owner approval of PROPOSED values:** the EUR 1,500 contribution threshold, 48 h observation
  freshness, the 7-day cooldown default, 3 send attempts, `MAX_SIGNALS_PER_INQUIRY_24H` = 6,
  `MAIL_WORKER_REPLY_LIMIT`, `MAX_NEW_REPLIES_PER_MAILBOX_PER_HOUR` = 120,
  `MAX_CANARIES_PER_24H` = 5, the 24 h canary window (ACTIVATION_GATES.md section 20).
- **Exact-build release.** D1 to D3 are not committed, so no `scripts/verify_release.sh
  --with-e2e` report exists for an exact commit; restore and rollback drills were not performed.
- **Activation tooling not built:** no command records a gate status change; no command creates,
  approves or verifies a destination binding; no command stores, approves or activates a tax rule
  set in the database; no command approves a cost profile; no command records the `gmail_api`
  provider verification.

## Known limitations

- Vehicle documents (CoC, registration, CO2) stay in the owner's mailbox; only attachment metadata
  reaches the backend and the facts are verified manually (F10).
- The canary reply proves only Outlook -> backend; it emits no Slack signal.
- Owner-facing opportunity alerts of the review pipeline (spec 22) have no message builder: such
  events end `blocked` with `OWNER_ALERT_MESSAGE_UNAVAILABLE`. Seller-reply owner alerts have one.
- The desktop worker covers the mailbox only while the PC is awake, the owner is signed in and
  Outlook runs; every other interval is a reported coverage gap, and inquiries wait in the queue.
- A sending inquiry whose worker credential expired shows no `WORKER_OFFLINE` waiting reason for up
  to 5 minutes (heartbeat-based); a dead credential cannot heartbeat, so it resolves itself.
- `GET /api/activation/canary-evidence` reads the newest 20 canaries; with more, an older
  correlated reply is not counted (can only under-claim).
- Blocked reply signals are never re-emitted; the re-emit of a muted coalesced reply looks back 7
  days.
- The dashboard's process gate reflects the API process's settings only; the worker's settings
  are checked by the worker itself.
- The Playwright E2E covers the closed process gate only (no browser test with the gate open).
- MK comparables are asking prices or owner estimates, never realised sales. The ECB publishes no
  MKD rate: EUR/MKD must be recorded by hand (`suv-deals fx record`).
- The backup manifest and restore check verify the v1.0 tables only (OPS-13).
- No documented end-to-end deletion procedure for personal data exists yet; the database has the
  owner-only history-maintenance bypass for it (docs/schema.md section 5).

## Review findings and open items

Final review lenses (security-privacy, spec-acceptance, operations) with their verdicts, and what
happened to each. "Fixed" items have tests that failed on the old code (wave D2 unless noted).

| Finding | Verdict | Disposition |
|---|---|---|
| SEC-1 unlimited claims for one Outlook intent | real, low | fixed: one claimant per intent, per-store worker id (`tests/integration/v11_inquiries/test_d2_single_claimant.py`) |
| SEC-2 SET ROLE per pooled connection | not real | no change; transaction-pooler caveat documented (docs/runbook.md 3.3) |
| SEC-3 / OPS-07 crawler firewall only on FORWARD | real | fixed in the runbook: INPUT rules and fixed bridge names (documented, not executable here: no Docker) |
| SEC-4 crawler container hardening | not real | residual risk documented (SECURITY.md) |
| F1 no seller identity/recipient/language evidence producer | real, high | fixed (`tests/integration/v11_runtime/test_d2_seller_evidence_pipeline.py`) |
| F2 no MK comparable ingestion | real, high | fixed: `suv-deals market import` and `mk_comparable` routing |
| F3 / OPS-04 no canary transport; automatic mode not gated on live evidence | real | fixed for `outlook_local` (transport, reservation gate `activation_canary_incomplete`); `gmail_api` / `microsoft_graph` have none |
| F4 v1.1 gates missing / Slack reply route mislabelled | real | fixed: three gates seeded `blocked` (`tests/integration/pipeline/test_d2_activation_gates.py`) |
| F5 / OPS-11 handoff documents missing | real | fixed in D2 and rewritten in D3; the exact-build evidence still needs a commit |
| F6 / OPS-08 release verifier skipped desktop, dashboard, E2E | real | fixed (`tests/cli/test_d2_release_verifier.py`) |
| F7 downtime catch-up untested | real, low | fixed (`tests/integration/repos_sources_listings/test_schedules.py`) |
| F8 no browser test for a new revision before submit | real, low | fixed (`dashboard/e2e/review.spec.ts`) |
| F9 live-smoke harness absent | real, low | fixed in the docs: the live smoke is the gated `crawl once` procedure |
| F10 seller documents never reach the backend | real, low | documented limitation (manual verification) |
| OPS-01 worker did not claim the v1.1 job types | real, high | fixed: default queues cover every job type |
| OPS-02 blank placeholders parsed as '' | real, high | fixed (`tests/unit/test_d2_settings_blank.py`) |
| OPS-03 sources without adapter looked activatable | real, low | fixed: "not activatable (no adapter)" |
| OPS-05 doctor ignored Slack for reply signals | real | fixed |
| OPS-06 ECB FX fetch wired to nothing | real | fixed: reconciler refresh, `fx refresh`, `fx record` |
| OPS-09 `gmail_api` cannot be activated | real | **partial**: `sender-binding store-secret` seals the grant; provider verification and canary transport remain open |
| OPS-10 runbook asks for the Outlook account key before any tool shows it | real, low | **open**; workaround: the SMTP address as account id (warning only) |
| OPS-12 `config apply --dry-run` needs `--reason` | real, low | **open** (pass any `--reason` with `--dry-run`) |
| OPS-13 backup/restore verify v1.0 tables only | real, low | **open** |
| OPS-14 `make e2e-clean` uses a text regex instead of the libpq-aware guard | real, low | **open** |
| OPS-15 dev compose: token required for the whole file, two switches pinned, fixtures not in the image, `make dev` starts no reconciler | real, low | **open** |
| OPS-16 `rollback.sh --status` passes `DATABASE_URL` to psql's argv | real, low | **open** |
| OPS-17 `.env.example` misses two settings and lists inert ones | real, low | **open** |
| OPS-18 scheduled-task start and revision check underspecified | real, low | **open** |
| OPS-19 migrations directory ignored | real, low | **partial**: `doctor` and the psycopg engine honour `MIGRATIONS_DIR` / `SUV_DEALS_HOME`; `scripts/migrate.sh` (psql engine) still reads `supabase/migrations` next to the script |

Further open items carried from the D1 review (all low): `canaries_repo.claim_for_send` holds the
mailbox binding row but not the API credential row `FOR SHARE` (a revocation committed during the
claim statement equals one right after it); the waiting-reason, canary-evidence, re-emit and
process-gate limitations listed under "Known limitations".

Found in wave D3 and fixed in the final verification pass:

- **Flaky test (fixed)** `tests/integration/v11_runtime/test_reply_escalation.py`
  (`test_payment_request_alerts_once_and_routine_replies_never`) asserted that the substring `500`
  (the deposit amount) does not occur in the JSON of an owner-alert payload, but the payload
  contains random UUIDs and a millisecond timestamp, which contain `500` in a few percent of runs
  (it failed once in the D3 PostgreSQL 16 full run on the UUID `...fa50025ae`). The same check
  could never catch a leaked `überweisen`, because `json.dumps` escapes it. The product code was
  correct (ids, codes and the dashboard link only); the test now blanks UUIDs and RFC 3339
  timestamps and keeps non-ASCII text literal before the check
  (`test_seller_text_view_ignores_random_ids_but_keeps_seller_text` shows both old failure modes).

Still open:

- **Activation tooling missing** for gate status changes, destination bindings, tax rule sets and
  cost-profile approval (ACTIVATION_GATES.md sections 6, 7, 19).

## Spec 34 handoff contents

| Spec 34 item | Where |
|---|---|
| Repository location, exact final commit/build ids | `/home/user/Lokal69`; base `e5d47f4` + uncommitted D1-D3; no build id yet (docs/qa_evidence.md) |
| Local setup commands and tested dependency versions | README.md "Quickstart", docs/dependency_inventory.md, docs/runbook.md 4 |
| Migrations and reproducible local seed fixtures | `supabase/migrations/` (16 files), `supabase/tests/supabase_emulation.sql`, `tests/e2e/seed.py`, `tests/e2e/seed_v11.py`, `tests/adapters/fixtures/` |
| Source registry with activation/access/parser coverage | docs/source_access_register.md, `config/sources/`, ACTIVATION_GATES.md section 4 |
| JSON schemas and MCP tool contract exports | `schemas/tools/`, `schemas/api/` (`scripts/export_schemas.py --check`), docs/api_contract.md |
| Dashboard and MCP URLs | none: nothing is deployed |
| Test reports and exact-build E2E evidence | docs/qa_evidence.md (working tree), docs/acceptance_matrix.md; exact-build: not yet (docs/qa/README.md) |
| Configuration guide and secrets placement | `.env.example`, docs/runbook.md 2.1 and 3, SECURITY.md "Secrets" |
| Tax-rule approval workflow and status | docs/tax_rule_approval.md; status: no approved rule set |
| Notification/trigger setup and what was verified | docs/notification_bridge.md, docs/connect_mcp.md; verified only with fakes |
| Seller-email status, templates, scope, counts, receipts, reply mapping | below |
| Runbook, backup/restore evidence, rollback | docs/runbook.md 6-8; no restore drill evidence yet |
| Remaining gates with minimal owner actions | ACTIVATION_GATES.md |
| Known limitations, coverage gaps, maintenance responsibilities | this file; maintenance below |

**Seller e-mail status:** sender binding none (no account bound); template set `seller_templates@1`,
scope version 1 (docs/seller_email_templates.md); standing authorization version 1 of 2026-10-06
(`config/seller_inquiry_authorization.yaml`: one inquiry per vehicle/seller pair asking
availability, vehicle documents and the lowest/final price; no message approval); real inquiries
sent 0, uncertain 0, suppressed 0; provider receipts none; reply mapping by Message-ID,
In-Reply-To and References, never by subject alone, with ambiguous matches quarantined.

**Maintenance responsibilities.** Owner: terms decisions per source, tax rules, cost assumptions,
thresholds, destination approvals, the sender account, the Windows PC and its Outlook, credential
rotation decisions, the activation canary. Operator: deployments from an exact commit with the
release report, migrations (forward-only), backups and restore drills, crawler and firewall,
parser repairs with fixtures, dependency and image upgrades (docs/dependency_inventory.md),
reviewing `doctor`, coverage gaps and blocked jobs.

## Final status (spec 34)

- **What runs now?** Nothing runs in production. On the development machine the API with the MCP
  endpoint, worker, scheduler, reconciler, dispatcher, the dashboard and the desktop worker run
  against local PostgreSQL 16 or 17 and fakes, with every source, notification route and seller
  e-mail off.
- **Where does it run?** Only on the development machine. Compose files and a runbook exist for a
  VPS (gate `hosting`). The hosted Supabase project has the full schema (migrations through
  `20261008000200`, docs/schema.md section 9) and serves nothing yet.
- **Which sources are truly working?** None live. The generic dealer adapter is `fixture_verified`
  on synthetic pages; 12 of 14 registered sources have no adapter; mobile.de and AutoScout24 are
  not crawled (owner decision).
- **When was each last checked?** No source has ever been checked live. The source register
  (docs/source_access_register.md) is dated 2026-10-06: terms were reviewed that day for
  `mobile_de_public`, `autoscout24_de` and `autoscout24_it` only (all restrictive) and every other
  source is "not reviewed"; the only recorded robots.txt reading is `autoscout24_ch` (2026-10-06,
  a fact, not a permission). The fixture suites last passed on 2026-10-10 (docs/qa_evidence.md).
- **Which calculations are evidence-supported?** Parsing, screening, comparables, costs, tax
  arithmetic, FX conversion and scenarios are deterministic and tested on synthetic data. No real
  valuation is evidence-supported: no MK market evidence exists in a real workspace (so every
  valuation is `insufficient_comparables`), no approved tax rule set and no owner-approved cost
  assumptions.
- **Can dot read the queue?** Not yet. The MCP server and its tools are complete and tested with
  the official SDK client in process, but no real dot client has connected (gate
  `mcp_authentication`).
- **Can dot actually be triggered?** No. Neither native MCP Events nor the Slack route is
  configured or verified (gates `native_mcp_events`, `seller_reply_slack_route`,
  `automatic_dot_activation`).
- **Were notifications accepted by the intended destination?** No notification has been sent
  anywhere; no destination binding exists (gate `production_notifications`).
- **What remains blocked?** Every gate except `implementation_environment` and `existing_crawler`
  (code) and the two `not_requested` ones: source access, Supabase production use, hosting, MCP
  access, the sender mailbox and activation canary, seller inquiries, the Slack reply route, MCP
  Events, automatic dot activation, production notifications, tax rules, cost assumptions and the
  contribution threshold. See [ACTIVATION_GATES.md](ACTIVATION_GATES.md).
