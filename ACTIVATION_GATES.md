# Activation gates

What must happen before each capability of the SUV deal system may run against real services,
who does it, how it is done, how it is verified and what evidence is kept. Contract: spec section
32 (activation gates and honest completion states), section 31 (exact-build evidence) and section
37.10 (activation evidence for the seller-email route). Status as of **2026-10-10** (wave D3).

**Nothing is active and nothing is `live_verified`.** No live crawl, no Crawl4AI service, no real
e-mail sent or read, no Slack, no dot, no Outlook on a real PC and no deployed service has been
used. Every source is disabled and every external effect is off by default (README "Safety
defaults"). The work these gates protect is complete and tested offline
([IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md), [docs/acceptance_matrix.md](docs/acceptance_matrix.md)).

## How to read this file

- **States** (spec 32): `implemented`, `fixture_verified`, `integration_verified`,
  `live_verified`, `active`, `blocked`, `not_requested`. A gate never moves to `live_verified`
  or `active` without the evidence listed for it.
- **Where the state lives.** Every workspace has one row per gate in `ops.activation_gates`,
  seeded from `src/suv_deals/persistence/gates.py` (`SPEC_GATES`; the reconciler inserts missing
  gates and never overwrites a recorded one). The MCP tool `deals_health` and the dashboard
  Overview/Settings show them (`persistence/queries/operations.py`); `suv-deals doctor` reports
  the source registry gates and the seller-inquiry readiness, not these rows.
- **Recording a new state is not tooled yet.** No CLI command or API route changes a gate row
  (only the automatic source access-block path writes one, as `blocked`). Until such a command
  exists, the seeded status in the database stays as listed below, and the evidence log at the
  end of this file is the authoritative record of activation progress. Adding that command is
  an open item ([IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md), "Review findings and open items").
- **Who.** *Owner*: Vasko. He makes the decisions, owns the accounts and the Windows PC, approves
  destinations and rules, and personally runs the activation canary send. *Operator*: whoever
  runs the deployment and the `suv-deals` CLI on the owner's behalf. An agent never runs an
  owner step.
- **Evidence rules.** Record ids, timestamps, versions, hashes and result codes only. Never record
  a token, password, OAuth secret, the owner's mailbox address, a seller's address or a message
  body. Redact logs with `uv run python scripts/redact_logs.py` before attaching them.

## Gate summary

Seeded status = the status every workspace gets from `SPEC_GATES` today.

| Gate | Seeded status | Spec 32 evidence needed | Who | Section |
|---|---|---|---|---|
| `implementation_environment` | `implemented` | Authorized repository and actual access | operator | [1](#1-implementation_environment) |
| `existing_crawler` | `implemented` | Runtime version, health, topology, auth/request contract | operator, owner approves the host | [2](#2-existing_crawler-crawl4ai-service) |
| `supabase` | `blocked` | Approved project, server credentials, schema/RLS tests, backup choice | owner decides, operator executes | [3](#3-supabase-project) |
| `source_access` | `blocked` | Per source: configuration, terms decision, robots, unblocked live smoke | owner decides terms, operator implements and smokes | [4](#4-source_access-per-source) |
| `credentials_api_accounts` | `not_requested` | Owner-approved account, scope, successful test | owner | [5](#5-credentials_api_accounts) |
| `tax_rules` | `blocked` | Current sourced rule set, applicability, recorded approval | owner (with a qualified adviser) | [6](#6-tax_rules) |
| `cost_assumptions` | `blocked` | Owner-selected assumptions/quotes, currency treatment | owner | [7](#7-cost_assumptions) |
| `contribution_threshold` | `blocked` | Explicit choice; EUR 1,500 stays a proposal until then | owner | [8](#8-contribution_threshold-and-other-proposed-values) |
| `seller_email_sender` | `blocked` | Verified mailbox, provider contract, dedup/suppression, live test evidence (activation canary rows 4-6) | owner (PC, account, canary send), operator (backend) | [9](#9-seller_email_sender-sender-binding-desktop-worker-and-activation-canary) |
| `seller_inquiry` | `blocked` | Current qualifying candidate with exact-ad seller, address and language evidence; budget and kill-switch checks | owner (switches mode), operator | [10](#10-seller_inquiry) |
| `seller_reply_slack_route` | `blocked` | Private channel, approved `seller_reply` binding, dot trigger on the signal metadata, correlated reply Outlook -> backend -> Slack -> dot -> MCP with dot's processing evidence | owner (Slack, dot), operator | [11](#11-seller_reply_slack_route-slack-app-channel-and-dot-trigger) |
| `hosting` | `blocked` | Approved provider, region, budget, domain/TLS, deployment authority | owner decides, operator deploys | [12](#12-hosting) |
| `mcp_authentication` | `blocked` | Approved persistent access, issuer/audience/scopes, real client success | owner (dot account), operator | [13](#13-mcp_authentication-connecting-dot) |
| `slack_destination` | `not_requested` | Verified private channel and approved event data (optional candidate fallback) | owner | [14](#14-slack_destination) |
| `native_mcp_events` | `blocked` | dot supports discovery/subscription, callback security, lifecycle, canary and unsubscribe | owner (dot), operator | [15](#15-native_mcp_events) |
| `automatic_dot_activation` | `blocked` | A selected route and a correlated end-to-end canary | owner | [16](#16-automatic_dot_activation) |
| `production_notifications` | `blocked` | Correct destination, dedup and uncertainty handling tested | owner | [17](#17-production_notifications) |

Further activation items that are not separate gate rows are in sections 18 to 20 (exact-build
release, destination-binding tooling, thresholds).

## Recommended order

Each step only needs the steps above it; any step can stop without affecting the others.

1. Commit the working tree and produce the exact-build release report (section 18).
2. Supabase production decision, backups and restore drill (3); hosting and domain (12).
3. MCP access for dot in pull mode (13). From here dot can read the review queue.
4. Crawl4AI service and firewall (2); ONE permitted dealer source end to end (4).
5. MK market evidence and FX rates (10, prerequisites); tax rules (6), cost assumptions (7) and
   the contribution threshold (8) for evidence-supported valuations.
6. Sender binding, desktop worker and the activation canary on the owner's PC (9).
7. Slack app, private channel, destination binding and the dot trigger (11, 19).
8. Automatic seller inquiries (10); native MCP Events for candidate discovery (15, 16, 17).

---

## 1. `implementation_environment`

- **State:** `implemented`. Repository `/home/user/Lokal69`; base commit `e5d47f4` plus the
  uncommitted waves D1 to D3 (see [docs/qa_evidence.md](docs/qa_evidence.md)).
- **Remaining:** commit the working tree (owner or operator decision; agents in this build never
  commit). Everything else in this file assumes one exact commit.
- **Evidence:** `git rev-parse HEAD`, `git status --short` empty, the release report of section 18.

## 2. `existing_crawler` (Crawl4AI service)

- **State:** `implemented` (client, URL policy, SSRF guards, fixture pipeline). No Crawl4AI
  service has been reached; `doctor --crawler` has never run against a real one.
- **Prerequisites:** an approved host (gate `hosting` or the owner's workstation); the pinned
  image `unclecode/crawl4ai:0.9.4` with its tested digest; an API token; the host firewall rules.
- **Who:** operator provisions; owner approves the host.
- **Steps:**
  1. Compose (VPS): put the token into `/etc/suv-deals/secrets/crawl4ai_api_token` (root, 0600),
     export `CRAWL4AI_IMAGE_DIGEST=sha256:<tested digest>` and start the crawler service of
     `compose.production.yaml`. A crawler already running on the host is used as is: host
     processes point `CRAWL4AI_BASE_URL` at `http://127.0.0.1:11235` (docs/runbook.md 3.1); never
     restart or reconfigure a shared crawler.
  2. Apply BOTH firewall rule sets of docs/runbook.md 3.2 (DOCKER-USER for routed egress, INPUT
     for host-local services; bridges `br-suv-crawl` and `br-suv-crawlint`) and persist them.
  3. `uv run suv-deals doctor --process worker --crawler`: health, pinned version 0.9.4,
     unauthenticated requests refused, read-only `/config/dump` contract, tested topology.
  4. Run the in-container reachability check of docs/runbook.md 3.2: every host address on a
     listening port must be unreachable from the crawler.
- **Verified when:** doctor reports the crawler healthy at 0.9.4 with authentication enforced, and
  the reachability check prints no `REACHABLE` line.
- **Evidence:** doctor output (topology, version, auth result), image digest, the firewall rule
  listing (`iptables -S DOCKER-USER`, `iptables -S INPUT`) and the reachability check output.
- **Residual risk:** the crawler container drops all capabilities, sets `no-new-privileges` and
  has CPU/memory/pid limits, but its root file system is writable and it runs as the image's
  default user (SECURITY.md, residual risks).

## 3. Supabase project

- **State:** `blocked`. Project `Lokal69- Sub` (ref `olkcgrahvkvzgnnsqspr`, eu-west-1,
  PostgreSQL 17.11) has every migration through `20261008000200` applied (docs/schema.md section
  9; 16 migration files). It serves nothing: no backend process has connected to it, no user or
  workspace was bootstrapped, no backup was restored.
- **Prerequisites (owner decisions):** approve the project and region for production; choose the
  backup capability actually bought (plan/PITR); decide on the two advisor warnings about the
  project-provided `public.rls_auto_enable()` (EXECUTE for `anon`/`authenticated`; not created by
  these migrations).
- **Project settings (operator, in the Supabase dashboard):**
  - Auth: site URL and redirect allow-list contain `https://<dashboard host>/auth/callback`;
    sign-ups disabled; users created deliberately (dashboard/README.md "Deployment").
  - Data API: do not expose `app` or `ops` (the migrations grant nothing to `anon`,
    `authenticated` or `service_role`; ADR 0001). Keep `pg_graphql` off.
  - Server credentials: a LOGIN role that is a member of `suv_backend`, or the owner connection
    with `DATABASE_SET_ROLE=suv_backend` on a direct or session-mode connection (docs/runbook.md
    3.3). Secrets go into `/etc/suv-deals/<process>.env` (root, 0600), never into the repository.
  - Storage: a private bucket `source-evidence-private` only if `SNAPSHOT_STORAGE=supabase`.
  - Migration ledger: the connector recorded apply-time versions; reconcile them before using
    `scripts/migrate.sh` or `supabase db push` against this project (docs/runbook.md 4.1).
- **Steps:**
  1. `DATABASE_URL=<owner connection> uv run suv-deals db migrate --dry-run` (prints the target
     without the password; expect no pending file).
  2. `uv run suv-deals doctor --process api` and `GET /readyz` on the deployed API.
  3. Bootstrap (privileged connection in `MAINTENANCE_DATABASE_URL`, environment only):
     `uv run suv-deals bootstrap owner --user-id <auth user id> --workspace-name "Deals" --yes`,
     then `suv-deals config apply --reason "initial configuration" --yes` and
     `suv-deals sources sync --dry-run` / `--yes`.
  4. Backup and restore drill (docs/runbook.md 7): `scripts/backup.sh`, then
     `scripts/restore_check.sh var/backups/suv-deals_<ts>.manifest` into an isolated local
     database. Record the elapsed time.
- **Verified when:** doctor shows the schema markers and `SET ROLE suv_backend` working, `/readyz`
  is ready, the restore check passes.
- **Evidence:** migration ledger listing, doctor output, restore-check report (row counts,
  hashes, elapsed time), the chosen backup plan.
- **Known gap:** the backup manifest and restore check count and verify the v1.0 tables only; the
  v1.1 tables are inside the dump but not individually verified (OPS-13, open).

## 4. `source_access` (per source)

- **State:** `blocked`. 14 sources are registered in `config/sources/*.yaml`; all are
  `enabled: false` and none was ever fetched live. Only the generic dealer adapter
  (`schemaorg_dealer@1.1.0`, used by the two dealer templates) is implemented; it is
  `fixture_verified` on synthetic pages. The other 12 sources have no adapter
  (`adapter_version: unimplemented`; `sources inspect` and `doctor` say "not activatable (no
  adapter)"), so configuration alone can never activate them.
- **mobile.de and AutoScout24 are not crawled.** Building or running crawlers for them was denied
  by an automated policy classifier during this build; it stays an owner decision (terms
  restrictions are recorded in docs/source_access_register.md). Proceeding would need the
  owner's decision, preferably a permission or agreement (`proceed_permitted`), and then the same
  seven steps. The optional official mobile.de Search API is gate 5.
- **Who:** owner records the terms decision; operator verifies routes, implements/configures the
  adapter, captures fixtures and runs the smoke; owner enables.
- **Steps** (docs/source_access_register.md "Activation checklist", in order):
  1. Terms decision record in the source YAML (`terms_status`, `terms_url`, `terms_reviewed_at`,
     `terms_decision`, actor and note).
  2. Permitted routes: exact `allowed_hosts`, anchored search/detail path regexes, dealer
     `search.search_url`.
  3. Robots check through the robots module (revision hash and time stored).
  4. Adapter: for a dealer, copy `config/sources/example_dealer_template_de.yaml` (or `_ch`) and
     confirm schema.org JSON-LD on the permitted pages; for a marketplace, a real adapter.
  5. Saved fixtures with `MANIFEST.yaml` covering the spec 31 set; `uv run pytest tests/adapters -q`;
     then `technical_status: fixture_tested`.
  6. Live smoke by hand: with `SOURCE_NETWORK_ENABLED=true` (Compose:
     `SUV_DEALS_ENABLE_SOURCE_NETWORK=true` exported for that run only),
     `uv run suv-deals crawl once --source <key> --max-pages 1`, then
     `uv run suv-deals worker --drain --queues detail`; check `suv-deals sources inspect <key>
     --from-db` and `doctor`. Then `technical_status: live_smoke_passed`.
  7. Enable through an auditable configuration revision (`sources sync --yes`).
- **Verified when:** `access_state: ok`, healthy parser outcomes, and the persisted listing,
  revision and evidence ids of the smoke.
- **Evidence (spec 31 exact-build list):** commit SHA and image digest, configuration revision and
  `adapter_version`, source URL, actual observation timestamp, crawl run and job ids with the
  redacted fetch outcome, persisted listing/revision/evidence ids. Record each source separately;
  one source never proves another.

| Source | Adapter | Terms decision | What blocks it |
|---|---|---|---|
| `example_dealer_template_de`, `example_dealer_template_ch` | `schemaorg_dealer@1.1.0` (fixture_verified) | none (templates) | a real permitted dealer: copy, steps 1-7 |
| `mobile_de_public`, `autoscout24_de`, `autoscout24_it`, `autoscout24_ch` | placeholder | pending | not crawled: owner decision; restrictive terms (CH terms not identified) |
| `subito_it`, `automobile_it`, `carforyou_ch`, `tutti_ch`, `comparis_ch` | placeholder | pending | domain, terms, routes unverified; an adapter |
| `pazar3_mk`, `reklama5_mk` (MK comparables) | placeholder | pending | terms and an adapter; until then MK evidence comes from `suv-deals market import` |
| `mobile_de_api` | skeleton (raises until entitlement) | pending | gate 5 |

## 5. `credentials_api_accounts`

- **State:** `not_requested`. The only candidate is the optional mobile.de Search API; its
  adapter is a skeleton that refuses with `DependencyUnavailable` until an entitlement exists.
- **If wanted:** the owner applies for the account; the operator stores the credentials
  server-side (`MOBILE_DE_API_CREDENTIALS`, `MOBILE_DE_API_ENTITLEMENT_REFERENCE`,
  `MOBILE_DE_API_ENABLED=true`), implements the adapter from the official documentation, records
  fixtures and runs a low-volume test. Marketplace login credentials are not API credentials.
- **Evidence:** entitlement reference, scope, the low-volume test's ids.

## 6. `tax_rules`

- **State:** `blocked`. The engine, validation, versioning and lifecycle are implemented and
  tested with synthetic rule sets. No production North Macedonian rule set exists, so every
  non-fixture valuation keeps import charges `unknown` and stays incomplete. The tax rule is not a
  gate for the seller inquiry (`TAX_RULE_NOT_APPROVED` is informational there).
- **Prerequisites:** a current, sourced rule set (official sources with retrieval time and
  SHA-256), reviewed against golden cases.
- **Who:** owner, with a qualified customs broker or tax professional.
- **Steps:** docs/tax_rule_approval.md (draft, validate, review, approve, activate). Validation is
  tooled: `uv run suv-deals tax-rules validate <file>`.
- **Tooling gap:** storing a rule set in the database and moving it through
  review/approval/activation (`valuation_repo.store_rule_set` / `transition_tax_rule_set`) has no
  CLI command or API route; the valuation pipeline reads rule sets only from the database. An
  operator command is needed before this gate can pass (open item).
- **Evidence:** rule set id/version and content SHA-256, review record, approver, activation time,
  `TAX_RULE_SET_ID` and the configuration revision.

## 7. `cost_assumptions`

- **State:** `blocked`. Costs are computed from `config/cost_profiles`; unknown costs stay
  unknown, never zero. The profile is stored unapproved.
- **Prerequisites:** the owner's choice of logistics, inspection, registration and reserve
  assumptions, or real quotes, with their currency treatment.
- **Steps:** edit the cost profile YAML, `uv run suv-deals config validate`, then
  `suv-deals config apply --reason "<what changed>" --yes`.
- **Tooling gap:** approving a stored cost profile (`valuation_repo.approve_cost_profile`) has no
  CLI command or API route (open item).
- **Evidence:** configuration revision, profile id, the quotes or assumption notes.

## 8. `contribution_threshold` and other PROPOSED values

- **State:** `blocked`. The EUR 1,500 minimum contribution is a proposal
  (`config/defaults.yaml`, `PROPOSED_MIN_CONTRIBUTION_EUR`, `CONTRIBUTION_THRESHOLD_APPROVED=false`);
  `doctor` and `inquiries status` say so.
- **Steps (owner):** confirm or change the amount in `config/defaults.yaml`, set
  `CONTRIBUTION_THRESHOLD_APPROVED=true` in the runtime environment, then
  `suv-deals config apply --reason "owner approved the contribution threshold" --yes`.
- **Evidence:** configuration revision and the owner's decision note.
- Other PROPOSED values that need an explicit owner confirmation are in section 20.

## 9. `seller_email_sender` (sender binding, desktop worker and activation canary)

- **State:** `blocked`. Sender binding, verification, desktop worker, canary transport on
  `outlook_local` and the reservation gate `activation_canary_incomplete` are implemented and
  tested with fakes. No account is bound, no Windows PC was used, no canary was sent.
- **Owner decisions in force:** default path `outlook_local` (classic Outlook on the owner's PC
  with his Gmail account); optional `gmail_api`, which cannot be activated with this build (no
  provider-verification command and no canary transport; OPS-09).
- **Prerequisites:** Supabase and hosting (the desktop worker needs an HTTPS backend URL), a
  Windows PC with classic Outlook (not "new Outlook") and the mailbox synchronising.
- **Steps** (docs/runbook.md 10.1 to 10.6, docs/seller_email_activation.md sections 3 and 8):
  1. Operator: `uv run suv-deals inquiries authorize --reason "owner standing authorization" --yes`
     (controls row in mode `disabled_until_sender_ready`, standing authorization from
     `config/seller_inquiry_authorization.yaml`).
  2. Owner/operator: `uv run suv-deals sender-binding create --provider outlook_local
     --account-id <id> --from-address <the owner's mailbox> --display-name "<signature name>"
     --reason "owner-authorized sending identity" --yes`. No shipped tool shows the stable
     Outlook account key before this step (OPS-10, open): use the SMTP address as `--account-id`
     (accepted with the warning `ACCOUNT_ID_IS_THE_SMTP_ADDRESS`), or re-create the binding with
     the key once the worker has reported it. Type the address only on the command line or in the
     runtime `.env`; never into a file in the repository.
  3. Operator: `uv run suv-deals mail-worker credential issue --sender-binding <id> --label "Owner
     PC classic Outlook" --expires 90d --yes` prints the mailbox binding id and the `suvmail_`
     token ONCE.
  4. Owner on the PC: install the worker (desktop/outlook-bridge/README.md "Installation") from the
     SAME commit as the backend; `config.toml` with `api_base_url` (https), the mailbox binding
     id, the SMTP address, the rule-target folders; `python -m outlook_bridge credential set
     --expires-at <expiry>` (hidden input, Windows Credential Manager);
     `python -m outlook_bridge check` must exit 0; start `python -m outlook_bridge run` at logon
     (Task Scheduler "At log on", "Run only when user is logged on"; never a service).
  5. Operator: after the first account report and heartbeat,
     `uv run suv-deals sender-binding verify <id> --reason "technical verification" --yes`; it
     refuses with codes (`NO_ACTIVE_MAIL_WORKER`, `WORKER_ACCOUNT_*`, `WORKER_HEARTBEAT_*`,
     `ACCOUNT_KEY_MISMATCH`, ...) until the evidence holds. Set `SELLER_EMAIL_PROVIDER`,
     `SELLER_EMAIL_ACCOUNT_ID`, `SELLER_EMAIL_FROM` (and `SELLER_EMAIL_REPLY_TO` only if the
     binding has one) to exactly the binding's values.
  6. Activation canary (docs/runbook.md 10.6; seller_email_activation.md rows 4-6): hold real
     inquiries with `suv-deals inquiries set-limits --max-per-24h 0 --max-per-15d 0 --reason ...
     --expected-version N --yes`; `SUV_CANARY_TARGET_ADDRESS=<owner test address> suv-deals canary
     prepare --purpose "activation route check" --yes`; put the same address as
     `canary_target_address` and `send_intents_enabled = true` into the worker's `config.toml`;
     set `SELLER_EMAIL_CANARY_SEND_ENABLED=true`, `SELLER_INQUIRY_MODE=automatic` and workspace
     mode automatic (`suv-deals inquiries set-mode automatic --reason ... --expected-version N
     --yes`). Then **the owner himself** runs `suv-deals canary send <id>
     --i-confirm-owner-controlled-address --yes`. The worker sends it once; the owner replies from
     the test mailbox. Afterwards set `SELLER_EMAIL_CANARY_SEND_ENABLED=false` and decide on the
     caps (section 10).
- **Verified when:** `suv-deals sender-binding status` shows `verified`, `usable`, `healthy`;
  `suv-deals canary status` shows `complete` for the binding's CURRENT version; the dashboard
  Mail workers page shows rows 4-6 from `GET /api/activation/canary-evidence`; a pause makes the
  worker's next claim answer `kill_switch` (row 7).
- **Evidence (seller_email_activation.md section 8, rows 1-7):** binding id and version,
  verification audit id, classic Outlook version from `check`, worker revision = backend commit,
  canary id, Message-ID match flag, `sent_items_confirmed` time, reply correlation time, the
  unrelated-mail check (nothing uploaded), the kill-switch claim refusal. Record whether Gmail's
  Sent Items copy appeared in classic Outlook (UNVERIFIED for the account type).
- **Without a `complete` canary** every real inquiry is refused at reservation
  (`activation_canary_incomplete`; nothing debited). A re-verified sender needs a new canary.

## 10. `seller_inquiry`

- **State:** `blocked`. Readiness, reservation, caps 2/24 h and 5/rolling 15 days, the 7-day
  seller cooldown, dedup, suppression, kill switch and uncertain-send reconciliation are
  integration-verified. No real inquiry has ever been planned: sent, uncertain and suppressed
  real-inquiry counts are all 0.
- **Prerequisites** (each one is checked by readiness or reservation; codes in
  `domain.inquiries`):
  - gate 9 complete (verified sender, `complete` canary);
  - a permitted ACTIVE source whose adapter declares `seller_contact_evidence` (today only the
    generic dealer adapter) and a fresh observation (PROPOSED 48 h);
  - MK comparable evidence: `uv run suv-deals market import <file> --evidence-kind asking_price
    --reason "..." --dry-run`, then `--yes` (schema `suv_deals.market_import/1`); without it every
    valuation is `insufficient_comparables` and nothing becomes `inquiry_ready`;
  - current FX rates: `suv-deals fx refresh --yes` with `FX_FETCH_ENABLED=true` (ECB, EUR->CHF
    only) and `suv-deals fx record --base EUR --quote MKD --rate <d> --date <day> --source-ref
    "..." --reason "..." --yes` (the ECB publishes no MKD rate); `suv-deals fx status` shows ages;
  - a cost profile (gate 7 for evidence-supported economics; unknown costs are listed, not zero).
- **Steps (owner):** restore the caps (`uv run suv-deals inquiries set-limits --max-per-24h 2
  --max-per-15d 5 --reason "..." --expected-version N --yes`, or lower values), keep
  `SELLER_INQUIRY_MODE=automatic` for the worker AND the API process (Compose:
  `SUV_DEALS_SELLER_INQUIRY_MODE`) and the workspace mode `automatic`. No per-message or
  first-template approval exists anywhere.
- **Verified when:** `uv run suv-deals inquiries status` shows `sending_possible: true`,
  `activation_canary: complete`, `sender_identity_problems: []`; the dashboard says "Automatic
  inquiries possible now: yes"; the first real inquiry reaches `accepted` with Sent Items
  evidence.
- **Evidence:** inquiry id, template id/version/hash, authorization version, recipient
  verification evidence id, attempt id, `sent_items_confirmed` time; `suv-deals evaluation report
  --days 15` for the 15-day view. Never the seller's address or the body.

## 11. `seller_reply_slack_route` (Slack app, channel and dot trigger)

- **State:** `blocked`. Reply ingest, the outbox signal `seller.reply.received.v1`, the Slack
  builder, flood control and reconciliation of uncertain posts are integration-verified with
  fakes. No Slack app, channel or dot trigger exists; nothing was posted.
- **Prerequisites:** gate 9 (the worker uploads correlated replies); gate 13 (dot can call MCP);
  the destination-binding tooling of section 19.
- **Steps:**
  1. Owner: create a private channel and a Slack app with `chat:write` and
     `channels:history`/`groups:history` (reconciliation); register the metadata event types
     `suv_deals.seller_reply_received`, `suv_deals.owner_alert` and `suv_deals.review_pending`
     under `metadata.event_subscriptions` in the app manifest; invite the app to the channel.
  2. Operator: dispatcher environment `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
     `SLACK_CHANNEL_ID`, `SLACK_DESTINATION_APPROVAL_REF`, recommended `SLACK_TEAM_ID`,
     `SLACK_BOT_USER_ID` (blank = unset); `ALLOW_EXTERNAL_NOTIFICATIONS=true` (Compose:
     `SUV_DEALS_ENABLE_EXTERNAL_NOTIFICATIONS=true`), `SELLER_REPLY_SIGNAL_PROVIDER=slack`.
     `uv run suv-deals doctor --process dispatcher` must list them as present.
  3. Owner + operator: an approved, enabled and verified destination binding for the category
     `seller_reply` on that channel (section 19: no shipped command yet).
  4. Owner: configure dot's Slack trigger on `SLACK_CHANNEL_ID` with the condition message
     metadata `event_type == suv_deals.seller_reply_received` (not text, not "any bot message",
     not the channel alone); `suv_deals.owner_alert` and `suv_deals.review_pending` must not fire
     it; the action is "call `seller_replies_get` / `seller_inquiries_get` over MCP", never a reply
     or a send (docs/connect_mcp.md 6.2, docs/notification_bridge.md 5.1).
  5. Correlated test reply through the whole route. The activation canary reply proves only
     Outlook -> backend (it emits no Slack signal); this leg needs the first correlated real seller
     reply or a separately built canary signal (owner decision; not built).
- **Verified when:** the Slack post's receipt (`provider_accepted_at`) AND dot's own processing
  evidence (its MCP `seller_replies_get` call with the same reply id) exist. A Slack 2xx alone is
  never proof that dot processed the signal.
- **Evidence:** outbox event id, Slack message `ts`, MCP request id of dot's read, the trigger
  configuration (screenshot or export without tokens).

## 12. `hosting`

- **State:** `blocked`. Dockerfile, `compose.production.yaml` and the runbook exist; no image was
  built or pushed, no digest recorded, no host provisioned.
- **Prerequisites (owner):** provider, region, monthly budget, domain, TLS and deployment
  authority.
- **Steps (operator):** docs/runbook.md 3 and 8: env files under `/etc/suv-deals/`, reverse proxy
  with TLS for `/api` and `/mcp` (published ports bound to `127.0.0.1`), pinned base digests,
  `RELEASE_IMAGE_DIGEST`, deploy with every switch off, then `/readyz`, an authenticated
  `/api/me` and `deals_health` over MCP.
- **Evidence:** image digest, crawler digest, release report, smoke results.

## 13. `mcp_authentication` (connecting dot)

- **State:** `blocked`. The MCP server (12 + 3 tools, scopes, OAuth protected-resource metadata,
  static bearer credentials) is tested with the official SDK client in process. No deployed URL,
  no real client.
- **Steps:** docs/connect_mcp.md section 4. In short: deploy (12); choose `MCP_AUTH_MODE`
  (`oauth` preferred; `static_bearer` only for a client that supports it); for a static credential
  `uv run suv-deals credentials create-mcp --label "dot read-only" --scopes
  deals:read,reviews:read --role viewer --expires 30d --yes` (token printed once; into dot's
  secret store only); check that an unauthenticated `curl -i https://<domain>/mcp` is `401`; add
  the custom MCP server in the owner's ChatGPT/dot account; ask dot to call `deals_health`,
  `reviews_list_pending` and one candidate. Write scopes (`reviews:write`, `inquiries:read`,
  `inquiries:pause`) only after the owner approves them.
- **Evidence:** client surface and version, negotiated protocol, SDK version 2.3.0, SHA-256 of the
  `tools/list` result, request ids of the successful calls.

## 14. `slack_destination`

- **State:** `not_requested`. Slack as a fallback for candidate discovery is optional; native MCP
  Events are the chosen candidate route. If the owner wants it, every item of the Slack checklist
  in docs/notification_bridge.md section 5 applies, with the category `candidate_discovery`.

## 15. `native_mcp_events`

- **State:** `blocked`. The provider, subscriptions, signed callback verification and delivery
  are implemented and tested; whether the owner's dot surface supports MCP Events for this plugin
  is unverified.
- **Steps:** docs/connect_mcp.md section 5: enable `MCP_EVENTS_ENABLED`, `EVENT_BRIDGE_ENABLED`,
  `EVENT_BRIDGE_PROVIDER=mcp_events`, `ALLOW_EXTERNAL_NOTIFICATIONS` (Compose: the
  `SUV_DEALS_ENABLE_*` switches) and `MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY`; an approved
  `candidate_discovery` destination binding (section 19); rescan the plugin; ask dot to monitor
  the review queue (dot subscribes itself); send a labelled synthetic canary; unsubscribe; only
  then set `EVENT_BRIDGE_VERIFIED_AT`.
- **Evidence:** docs/notification_bridge.md section 8 (subscription id, challenge exchange,
  delivery receipt, dot's tool calls, duplicate and out-of-order checks, unsubscribe).

## 16. `automatic_dot_activation`

- **State:** `blocked`. Needs one selected route per category and its correlated end-to-end
  canary: native MCP Events for candidate discovery (15) and the Slack route for seller replies
  (11). Until then pull mode (dashboard and MCP) is the complete working path.

## 17. `production_notifications`

- **State:** `blocked`. Outbox, dedup and uncertainty handling are tested with fakes. Needs an
  approved destination binding and a delivery canary. Owner-facing opportunity alerts of the
  review pipeline have no message builder yet: such events end `blocked` with
  `OWNER_ALERT_MESSAGE_UNAVAILABLE` (seller-reply owner alerts have their Slack builder).

## 18. Exact-build release report (not a gate row; spec 31 and 34)

- **State:** not produced. The waves D1 to D3 are uncommitted; `scripts/verify_release.sh` marks a
  dirty tree "not releasable".
- **Steps:** commit, then `scripts/verify_release.sh --with-e2e` with the target's
  `VITE_SUPABASE_URL` / `VITE_SUPABASE_PUBLISHABLE_KEY` exported; copy `var/releases/<sha>_<ts>.txt`
  and the outputs into `docs/qa/<sha>/` (docs/qa/README.md, docs/runbook.md 8).
- **Verified when:** the report says `result=verified` (clean tree, every step passed, E2E run).

## 19. Destination bindings (tooling gap for gates 11, 14, 15, 17)

Every external delivery goes to exactly one approved, enabled and verified destination binding
per event category (`app.destination_bindings`, `persistence.bindings_repo`). The repository
functions exist and are tested (`create_binding`, `approve_binding`, `set_binding_enabled`,
`mark_binding_verified`, preferences; `tests/integration/repos_valuation_reviews/test_bindings_repo.py`),
but **no CLI command, API route or dashboard control calls them**. Without a binding the
dispatcher blocks every event visibly (`NO_ACTIVE_ROUTE`). An owner-only operator command is
needed before gates 11, 14, 15 or 17 can pass (open item).

## 20. PROPOSED values awaiting the owner's confirmation

| Value | Where | Current |
|---|---|---|
| Tax rules | gate 6 | none approved |
| Minimum contribution EUR 1,500 | `config/defaults.yaml`, `CONTRIBUTION_THRESHOLD_APPROVED` | proposal |
| Observation freshness 48 h for an inquiry | `domain/inquiries.py` `MAX_OBSERVATION_AGE` | proposal |
| Seller cooldown 7 days (floor; owner may lengthen to 365) | `domain/inquiries.py` `SELLER_COOLDOWN`, DB CHECK | proposal for the default |
| 3 send attempts per inquiry | `domain/inquiries.py` `MAX_SEND_ATTEMPTS` | proposal |
| `MAX_SIGNALS_PER_INQUIRY_24H` = 6 | `persistence/replies_repo.py` | proposal |
| `MAX_NEW_REPLIES_PER_MAILBOX_PER_HOUR` = 120 | `persistence/replies_repo.py` | proposal |
| `MAIL_WORKER_REPLY_LIMIT` = 30 burst, 10 per minute per credential | `api/deps.py` | proposal |
| `MAX_CANARIES_PER_24H` = 5 | `persistence/canaries_repo.py` | proposal |
| Canary publication window 24 h | `domain/canary.py` `CANARY_INTENT_TTL` | proposal |

The owner-decided values are not proposals: acquisition DE/IT/CH at EUR 2,500-3,000 inclusive and
under 200,000 km, MK resale research band EUR 8,000-10,000, ONE automatic initial inquiry per
verified vehicle/seller pair without message approval, caps 2/24 h and 5/rolling 15 days,
`outlook_local` as the default send path.

## Evidence log

Append one row per completed activation step (newest last). Ids, versions, hashes and codes only.

| Date (UTC) | Gate / step | Commit | Evidence (ids, codes) | Recorded by |
|---|---|---|---|---|
| - | none yet | - | - | - |
