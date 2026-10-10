# Operations runbook

How to run, check, back up, restore, roll back and recover the SUV deal system. Product contract:
`docs/spec/suv-deal-system-build-spec.md` (cited as "spec §N"). Database reference:
`docs/schema.md`. MCP client connection: `docs/connect_mcp.md`. Versions: `docs/dependency_inventory.md`.

Every command below is real: it is implemented by `suv-deals` (`src/suv_deals/cli.py`), the
`Makefile` or `scripts/`. Nothing in this runbook creates accounts, keys or production data by
itself, and nothing activates a source, notification route or seller email.

## 1. Safety defaults (read first)

| Switch | Default | Effect while off |
|---|---|---|
| `SOURCE_NETWORK_ENABLED` | `false` | Every real source fetch is blocked (`source_network_disabled`); only `mode: fixture` sources run against saved files. |
| `ALLOW_EXTERNAL_NOTIFICATIONS` | `false` | The dispatcher records events as blocked; nothing leaves the system. The dashboard/MCP queue stays complete (pull mode). |
| `EVENT_BRIDGE_ENABLED`, `MCP_EVENTS_ENABLED`, `NOTIFICATION_PROVIDER` | `false`, `false`, `disabled` | No activation route; `bridge_status=unavailable`. |
| `SELLER_INQUIRY_MODE` | `disabled_until_sender_ready` | No seller e-mail. Even with `automatic`, nothing is sent unless the kill switch is off, the standing authorization is active and the sender binding is verified (section 10; `suv-deals inquiries status` shows `sending_possible`). |
| `SELLER_EMAIL_CANARY_SEND_ENABLED` | `false` | `suv-deals canary send` refuses (the owner's one-time activation canary, section 10.6; it also needs every inquiry switch and `--i-confirm-owner-controlled-address`). |
| `LLM_EXTRACTION_ENABLED` | `false` | No LLM calls; budget 0. |

- Fixture data never notifies (fixture outbox rows are blocked at enqueue time).
- Secrets are never command-line arguments: they come from the environment or `.env` (local) or
  root-owned env files / Docker secrets (VPS). `suv-deals doctor` reports presence only.
- Every state-changing CLI command needs `--yes`; `crawl once` cannot override a gate or take a URL.
- The `make db-*` targets (and `make dev`) only accept loopback targets and never read
  `DATABASE_URL` from `.env`. Their single guard is `suv-deals db target --local-only --url-env
  NAME`, which parses the connection string like libpq (every comma-separated host, `?host=` /
  `?hostaddr=` parameters, `PGHOST`/`PGHOSTADDR`/`PGSERVICE`) and exits 3 unless all of it stays on
  this machine; `scripts/restore_check.sh` uses the same guard.
- Under Docker Compose the switches are interpolated from dedicated names, never from the
  application's own variables: `SUV_DEALS_ENABLE_SOURCE_NETWORK`,
  `SUV_DEALS_ENABLE_EXTERNAL_NOTIFICATIONS` (both compose files) and, in
  `compose.production.yaml`, `SUV_DEALS_ENABLE_EVENT_BRIDGE`, `SUV_DEALS_ENABLE_MCP_EVENTS`,
  `SUV_DEALS_NOTIFICATION_PROVIDER`, `SUV_DEALS_ENABLE_FX_FETCH`, `SUV_DEALS_SELLER_INQUIRY_MODE`.
  Compose also reads a project `.env` for interpolation, so `SOURCE_NETWORK_ENABLED=true` in `.env`
  or in an env file changes nothing there; export the `SUV_DEALS_*` switch in the shell for that
  deployment only (never put it in `.env`).

## 2. Processes

One image, one settings model, separate processes (spec §4):

| Process | Command | Needs | Notes |
|---|---|---|---|
| API + MCP | `suv-deals api serve` | DB, auth config | `/api`, `/healthz` (liveness), `/readyz` (DB + schema + critical config), `/mcp`. Binds `127.0.0.1:8000` by default; containers bind `0.0.0.0` with `--allow-non-loopback` behind a `127.0.0.1`-only published port. HTTPS terminates at the reverse proxy. |
| Worker | `suv-deals worker --queues discovery,detail,recheck,valuation` | DB; crawler when the network is enabled | Leases + heartbeats; SIGTERM finishes the current job. `--drain` processes due jobs and exits. |
| Scheduler | `suv-deals scheduler` | DB | One discovery job per due 15-minute slot; missed slots become coverage gaps, never bursts. `--once` for a single tick. |
| Reconciler | `suv-deals reconcile --loop` | DB | Reapers, housekeeping, stale-detail and valuation sweeps; `--dry-run` reports a pass and rolls it back. |
| Dispatcher | `suv-deals dispatcher` | DB; route secrets | Only the selected, verified route; `--once` for one pass. |
| Crawler | `unclecode/crawl4ai:0.9.4` (pinned digest in production) | its own token | Private network; never receives database or Supabase keys. |

Extension points for the later spec v1.1 inquiry/reply package (no code change in the CLI needed):
`suv-deals worker --registry suv_deals.<module>:<factory>` (a `HandlerRegistry` with extra job
types) and `suv-deals api serve --app-factory suv_deals.<module>:<factory>` (a wrapper around
`api.app.build_app(settings, extra_routers=..., options=...)`).

### 2.1 Environment variables per process

`suv-deals doctor --process api,worker,...` checks exactly this table (presence only):

| Variable | api | worker | scheduler / reconciler | dispatcher | crawler |
|---|---|---|---|---|---|
| `DATABASE_URL` | required | required | required | required | never |
| `DATABASE_SET_ROLE=suv_backend` | recommended | recommended | recommended | recommended | never |
| `APP_BASE_URL` | required | – | – | required (links) | never |
| `MCP_CURSOR_SIGNING_SECRET` | required | – | – | – | never |
| `SUPABASE_URL` (dashboard JWT issuer/JWKS) | required | if `SNAPSHOT_STORAGE=supabase` | – | – | never |
| `SUPABASE_SECRET_KEY` | only `SNAPSHOT_STORAGE=supabase` | only `SNAPSHOT_STORAGE=supabase` | – | – | never |
| `MCP_PUBLIC_URL`, `MCP_OAUTH_ISSUER`, `MCP_OAUTH_JWKS_URL` | required with `MCP_AUTH_MODE=oauth` | – | – | – | never |
| `MCP_OAUTH_AUDIENCE` | optional (default: the resource URL) | – | – | – | never |
| `CRAWL4AI_BASE_URL`, `CRAWL4AI_API_TOKEN` | – | required with `SOURCE_NETWORK_ENABLED=true` | – | – | token only |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`, `SLACK_DESTINATION_APPROVAL_REF` | – | – | – | required when Slack is the route | never |
| `MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY` | required when MCP Events are on | – | – | required when MCP Events are on | never |
| `LLM_API_KEY` | – | only `LLM_EXTRACTION_ENABLED=true` | – | – | never |

`SUPABASE_PUBLISHABLE_KEY` belongs to the dashboard frontend only (browser-safe). The backend
uses a LOGIN member of `suv_backend` or `SET ROLE suv_backend` (ADR 0001); it does not need the
Supabase secret key unless evidence objects go to Supabase Storage.

## 3. Local versus VPS

| | Local (laptop / workstation) | VPS (optional, owner-approved) |
|---|---|---|
| Runs | `make dev` (api + worker + scheduler on the LOCAL database, network/notifications off) or `docker compose up` | `docker compose -f compose.production.yaml up -d` |
| Coverage | Only while the computer, Docker and network are up; a sleeping laptop is a visible coverage gap | 24 h, subject to the approved provider, region and budget |
| Database | Local PostgreSQL 16/17 clusters (tests, `make db-migrate-local`) or `supabase start` | The Supabase project (session/direct connection) |
| Secrets | `.env` (never committed) | `/etc/suv-deals/<process>.env` (root, 0600) + `/etc/suv-deals/secrets/crawl4ai_api_token` |
| HTTPS | not needed on loopback | reverse proxy on the host terminates TLS for `/api`, `/mcp` |
| Evidence files (`SNAPSHOT_STORAGE=local`) | `var/snapshots` (host processes) | the worker's `snapshots` volume (`/app/var/snapshots`; the containers' root file system is read-only); prefer the private Supabase bucket in production |

### 3.1 Crawl4AI topology

- A crawler started on the host (the user-reported one) is `http://127.0.0.1:11235` **for host
  processes only**. Inside a container `127.0.0.1` is the container itself.
- In Compose the crawler is `http://crawl4ai:11235` on the internal `crawler` network shared only
  with the worker (the compose files set `CRAWL4AI_BASE_URL` accordingly).
- Record which topology was tested: `suv-deals doctor --process worker --crawler` prints it
  ("host-local loopback" / "Compose service DNS") together with health, pinned version
  (0.9.4), whether unauthenticated requests are refused and the read-only `/config/dump`
  contract result. It never crawls a page and never restarts or reconfigures the crawler.
- Do not replace, restart or upgrade an existing shared crawler: point `CRAWL4AI_BASE_URL` at it.

### 3.2 Crawler egress isolation

Docker networks keep the crawler off the application network, but its internet egress could
still reach a public database endpoint. On the VPS add a host firewall rule for the crawler's
egress network (example with iptables; adjust the bridge name shown by `docker network inspect
suv-deals_crawler_egress`):

```bash
# Block the crawler bridge from private ranges and the database endpoint; allow the rest.
sudo iptables -I DOCKER-USER -i br-<crawler_egress_id> -d 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 -j DROP
sudo iptables -I DOCKER-USER -i br-<crawler_egress_id> -p tcp --dport 5432:6543 -j DROP
```

Crawl4AI's own SSRF guard stays on (`CRAWL4AI_ALLOW_INTERNAL_URLS` unset = false). The
application additionally enforces its URL policy, DNS checks and redirect validation per source.

### 3.3 Supabase connection modes

- Persistent processes (api, worker, scheduler, reconciler, dispatcher) use the **direct** or
  **session-mode pooler** connection string with a bounded pool (`DATABASE_POOL_MIN/MAX`).
- A **transaction-mode pooler** is only acceptable with settings that do not rely on session
  state: the app already disables prepared statements (`prepare_threshold=None`), sets every GUC
  transaction-locally and holds no session advisory locks. `SET ROLE` runs per pooled connection
  (`DATABASE_SET_ROLE`), which a transaction pooler may not preserve: prefer a LOGIN user that is
  a member of `suv_backend` there.
- Backups (`scripts/backup.sh`) need a direct/session connection (exported snapshots).
- Migrations need the schema-owner role (`postgres` on Supabase), never `suv_backend`.

## 4. First setup (spec §27)

```bash
cp .env.example .env              # fill secrets via the approved secure method; never commit
make doctor                       # presence-only report; exit 1 lists what is missing
make install                      # uv sync --frozen (locked; no system runtime upgrade)
make test-unit
make db-local-start               # local PG 16/17 test clusters (or prints how)
make db-migrate-local             # LOCAL database only; prints the target first
make test-db                      # database tests on PostgreSQL 16 AND 17
make test-integration
make dev                          # api + worker + scheduler, network/notifications OFF
make smoke-local                  # offline smoke incl. the fixture E2E pipeline
```

Workspace bootstrap (any environment; the Auth user must already exist in Supabase Auth):

```bash
export MAINTENANCE_DATABASE_URL=...   # privileged schema-owner connection (environment only)
uv run suv-deals bootstrap owner --email owner@example.com --workspace-name "Deals" --yes
uv run suv-deals config apply --workspace <id> --reason "initial configuration" --yes
uv run suv-deals sources sync --workspace <id> --dry-run    # then --yes
uv run suv-deals credentials create-mcp --workspace <id> --label "dot read-only" \
    --scopes deals:read,reviews:read --role viewer --expires 30d --yes   # token printed ONCE
```

### 4.1 Applying migrations to the Supabase project

1. Back up first (section 7).
2. `DATABASE_URL=<owner connection> uv run suv-deals db migrate --dry-run` prints the target
   (host, port, database, user; never the password) and the pending files.
3. `... suv-deals db migrate --yes` applies them through `scripts/migrate.sh` (or the psycopg
   engine where `psql` is missing), each file together with its ledger row.
4. Migration-version note (`docs/schema.md` section 9): migrations 0100–0900 were applied to the
   project `Lokal69- Sub` through the Supabase connector, which recorded them under their apply
   time, not the filename prefix. `migrate.sh` refuses a ledger with versions this checkout does
   not contain. Before using `scripts/migrate.sh`/`supabase db push` against that project,
   reconcile the ledger (`supabase migration repair --status applied <filename-version>` and
   revert the connector-recorded versions), or keep applying new files through the same
   connector path. `suv-deals doctor` reports such foreign ledger versions as a warning.
5. Verify: `suv-deals doctor --process api` (schema markers, `SET ROLE suv_backend`,
   `ops.backend_role_problems()` when the login may call it) and `GET /readyz`.

## 5. CLI reference

Exit codes: `0` ok, `1` problems found, `2` usage, `3` refused for safety, `4` dependency unavailable.

| Command | Purpose |
|---|---|
| `doctor [--process ...] [--no-db] [--crawler] [-v] [--json]` | presence-only configuration, baseline, database, sources, OAuth metadata (offline), notification and seller-inquiry modes |
| `config validate [--config-dir] [--json]` | offline; a primary maximum other than EUR 3,000 fails |
| `config apply --reason ... [--dry-run] --yes` | records the YAML business configuration as a revision |
| `sources list / inspect KEY [--from-db]` | gates with separate terms and technical status |
| `sources sync [--fixture-sources] [--dry-run] --yes` | YAML -> `app.sources`; never enables a gated source |
| `sources set-technical-status KEY STATUS --reason ... --yes`, `sources resume KEY --reason ... --yes` | audited owner recovery actions; neither one enables a source |
| `crawl once --source KEY [--profile primary] [--max-pages 1]` | one bounded discovery through every gate; if this command does not get its own job (another due discovery job first, or a running worker claimed it), the job is cancelled while it still waits, so the page cap can never be skipped silently |
| `worker`, `scheduler`, `dispatcher`, `reconcile [--dry-run / --loop]` | runtime processes |
| `outbox inspect`, `reviews list --status pending`, `evidence verify` | read-only checks |
| `tax-rules validate PATH` | validates; never approves |
| `db migrate [--dry-run] [--local-only] [--url-env NAME] --yes` | prints the target first; `psql` gets the connection string without its password (`PGPASSWORD`) |
| `db target [--url-env NAME] [--local-only]` | prints where a connection string points (never the password, never connects); `--local-only` exits 3 unless every host is loopback / a local socket |
| `api serve [--host 127.0.0.1] [--proxy-headers] [--forwarded-allow-ips]` | uvicorn |
| `credentials create-mcp / revoke / list` | scoped MCP credentials (hash stored, token shown once) |
| `bootstrap owner` | link an existing Auth user as workspace owner |
| `reconcile --workspace ID [--dry-run]` | one reconciliation pass for one active workspace (not with `--loop`) |
| `mail-worker credential issue --sender-binding ID --label ... [--expires 90d] --yes` | binds the desktop mail worker to the sender binding's mailbox (`ops.mail_worker_bindings`) and prints its `suvmail_` token ONCE (only the hash is stored) |
| `mail-worker credential revoke MAILBOX_ID --reason ... --yes`, `mail-worker credential list [--all]` | permanent revocation (the worker's next request is `401`, its local backlog is kept); health dimensions without tokens or addresses |
| `sender-binding create --provider outlook_local\|gmail_api --account-id ... --from-address ... --display-name ... [--reply-to ...] [--vault-ref scheme:path] --reason ... --yes` | registers the owner-authorized sending identity, UNVERIFIED; a provider secret only as an external reference (never a value); output shows only the address domain |
| `sender-binding verify ID --reason ... --yes` | `outlook_local` only: records the technical verification from the desktop worker's evidence (classic-Outlook account report of the bound address, fresh heartbeat, stable account key); refuses (exit 3) with problem codes otherwise. A prerequisite, never a message approval |
| `sender-binding status [--all] [--json]` | verification, alias, health and secret presence (domains only) |
| `inquiries authorize [--file PATH] --reason ... --yes` | creates the workspace controls (mode `disabled_until_sender_ready`) and records the owner's versioned standing authorization from `config/seller_inquiry_authorization.yaml` (idempotent for an identical latest version; an audit record, never a message approval) |
| `inquiries set-mode disabled_until_sender_ready\|automatic\|paused --reason ... --expected-version N --yes` | workspace mode; `automatic` is checked against the CONFIGURED sender binding (`SELLER_EMAIL_PROVIDER`/`_ACCOUNT_ID`/`_FROM`/`_REPLY_TO`, never merely the newest binding) and refused (exit 3, nothing changed) with every missing prerequisite listed by code (`missing: sender_binding_unverified`, `standing_authorization_missing`, ...); a binding of the configured provider that is not exactly the configured identity is refused too (`missing: sender_identity_sender_binding_mismatch`, `sender_identity_from_not_configured`, ...; the control view's codes, never a value) |
| `inquiries set-limits --max-per-24h 0..2 --max-per-15d 0..5 [--cooldown-days 7..365] --reason ... --expected-version N --yes` | owner-reducible ceilings; never above 2/24 h and 5/15 days; the seller cooldown is never shorter than 7 days and is kept when `--cooldown-days` is omitted |
| `inquiries status [--json]` | mode, kill switch, caps/usage, authorization, sender, mail workers, removable suppressions and `sending_possible` |
| `inquiries pause --reason ... --expected-version N --yes` | activates the kill switch (never resumes) |
| `inquiries resume --reason ... --expected-version N [--remove-suppressions --expected-suppressions K --owner-user-id UID] --yes` | owner action: clears the kill switch; optionally removes kill-switch/authorization-revoked suppressions (each audited, as the owner). `K` is the `removable_suppressions` count `inquiries status` showed: a different current count refuses the whole resume (exit 1, nothing changed), so only the suppressions the owner saw are removed |
| `jobs blocked [--json]` | blocked queue jobs: id, type, blocker code, the inquiry id of a send job (read-only) |
| `jobs unblock JOB_ID --reason ... [--acknowledge-uncertain-delivery --owner-user-id UID] --yes` | moves a blocked job back to `queued` (audited `job.unblock`). A send job blocked `EMAIL_DELIVERY_UNCERTAIN` is refused unless the OWNER acknowledges that its e-mail may already have left (`--acknowledge-uncertain-delivery` needs `--owner-user-id`; the audit names the owner). No second e-mail can follow: the inquiry is no longer `queued`, so the dispatch holds |
| `jobs resolve-blocked JOB_ID --outcome succeeded\|cancelled --reason ... --yes` | closes a blocked `EMAIL_DELIVERY_UNCERTAIN` send job AFTER its inquiry was reconciled (audited `job.resolve_blocked`); refused while the inquiry is still `uncertain` or has an unresolved attempt; never re-queues anything |
| `canary prepare --purpose ... --yes`, `canary status [--json]`, `canary cancel ID --reason ... --yes` | the owner-controlled activation canary of the configured sender (section 10.6); the test address comes from `SUV_CANARY_TARGET_ADDRESS` or a hidden prompt and is stored as a SHA-256 only; output never shows the address or its hash |
| `canary send ID --i-confirm-owner-controlled-address --yes` | the OWNER's one-time activation step (section 10.6): refuses (exit 3, nothing sent, nothing changed) unless `SELLER_EMAIL_CANARY_SEND_ENABLED=true` and every inquiry activation switch is on; today it ends with `CANARY_TRANSPORT_UNAVAILABLE` |
| `evaluation report --days 15 [--json]` | the 15-day quality evaluation from stored evidence (zero is reported as zero) |
| `reconcile --workspace ID [--dry-run]` (text output) | one line per group: `queue`, `outbox`, `inquiries` (`inquiry_plan_jobs`, `inquiry_replan_jobs`, `inquiry_send_jobs`, `inquiry_retry_jobs`, `inquiry_reconcile_jobs`, `inquiry_send_jobs_unblocked`, `send_attempts_uncertain`, `inquiries_marked_replied`), `replies`, `valuations`, `housekeeping`; a counter without a group is printed under `other` |

## 6. Incidents (spec §30)

| Incident | Immediate safe action (commands) | Recovery evidence |
|---|---|---|
| CAPTCHA / 401 / 403 on a source | The worker already records `access_blocked` and stops that source (one operational review item). Confirm: `suv-deals sources inspect KEY --from-db`. Pause the route explicitly if needed through the MCP/dashboard `sources_pause` action. Preserve evidence: do not delete fetch records. | Permitted access restored and recorded (terms/technical decision); the blocking technical status is cleared only by the explicit, audited owner action `suv-deals sources set-technical-status KEY fixture_tested --reason "..." --yes` (it never re-enables the source), then `suv-deals sources sync --yes` and a low-rate `suv-deals crawl once --source KEY --max-pages 1` smoke pass. |
| Parser drift | Source becomes `parser_unhealthy` automatically (new revisions quarantined, opportunity alerts paused). Check `suv-deals sources inspect KEY --from-db` (parser health section). | Fixtures repaired, `uv run pytest tests/adapters -q` green, then a live low-volume `crawl once` smoke. |
| Database outage | Processes back off on their own; stop schedulers if the outage is long: `docker compose stop scheduler dispatcher`. Do not delete leases or jobs. | After reconnect: `suv-deals reconcile --dry-run` then `suv-deals reconcile` (expired leases requeued, exhausted jobs dead-lettered visibly), `suv-deals outbox inspect`, `suv-deals doctor`. |
| Worker crash | Nothing manual: the reaper requeues expired leases (`suv-deals reconcile`). | The same job completes once logically: `suv-deals worker --drain` then `outbox inspect`; no duplicate revisions (`evidence verify`). |
| Notification uncertainty | Never resend blindly. `suv-deals outbox inspect` lists `uncertain` events; the dispatcher's follow-up rules (Slack lookup / single same-id resend for MCP Events) run on their own. | Provider receipt recorded or the event stays visibly `uncertain` with its reason. |
| Stale tax/FX rules | Valuations are marked stale/incomplete automatically (`reconcile`). Validate candidate rule files: `suv-deals tax-rules validate config/tax_rules`. | An approved current rule set (docs/tax_rule_approval.md) and `suv-deals reconcile` recalculation. |
| Secret exposure | Stop the affected integration (e.g. `docker compose stop dispatcher`, revoke MCP credentials: `suv-deals credentials revoke ID --reason ... --yes`), request controlled rotation from the owner. Run `uv run python scripts/redact_logs.py` on logs before sharing. | Old access revoked, new scoped credential tested, `suv-deals doctor` clean, logs reviewed. |
| Uncertain seller-inquiry send | Never resend, never cancel: an `uncertain` inquiry keeps its reservation and quota debit. Look: dashboard Inquiries (attention filter) or `GET /api/inquiries?uncertain_only=true`; `suv-deals inquiries status`. If the desktop worker is offline, start it (it reports Outbox / Sent Items evidence when it reconnects); a reply quoting the Message-ID also proves submission. If the cause is unclear, pause: `suv-deals inquiries pause --reason "uncertain send" --expected-version N --yes`. | The inquiry moves to `accepted` only on positive evidence (Sent Items / provider hit / correlated reply), or to `failed_definite` only on proof of non-submission (e.g. the worker refused an expired, never-claimed intent `intent_expired`). An empty Sent Items search proves nothing. |
| Stale or offline desktop mail worker | `suv-deals mail-worker credential list` (heartbeat age, monitoring) or dashboard Mail workers; check the PC is awake, classic Outlook runs and the account syncs. Coverage gaps are recorded, never hidden. Replies keep arriving in Outlook; nothing is lost while the worker is off. | Heartbeat fresh, `monitoring=true`, gap closed in `GET /api/mail-workers/coverage-gaps`; the worker's local backlog drains (reconciliation over the overlap window recovers mail received while it was off). |
| Lost or compromised desktop credential | `suv-deals mail-worker credential revoke MAILBOX_ID --reason ... --yes` (the worker stops at once and keeps its backlog), then issue a new binding (section 10.2) and store the new token on the PC. | New heartbeat with the new credential; old token `401` (`mail_worker_credential_revoked`). |
| Revoked or expired worker credential (seen in health) | `GET /api/mail-workers/health` (`credentials[].credential_status` `expired`/`revoked`, `revoked_mailboxes`, warning "credential is expired or revoked") or `suv-deals mail-worker credential list --all`. Revoking a credential revokes its mailbox binding too: every published inquiry binding is tombstoned and a claim or report that was waiting for the revocation is refused `403` (`mailbox_binding_revoked`), so the worker never calls `.Send`. Nothing else to undo. | A new binding issued (10.2) and its first heartbeat; send jobs released `MAILBOX_WORKER_CREDENTIAL_NOT_LIVE` continue on their own; the revoked mailbox stays counted in health. |
| Reply signal cap reached | Health `reply_signals.rate_limited` > 0 / warning "per-inquiry signal cap" and the reply's `signal_status = rate_limited` (PROPOSED cap `MAX_SIGNALS_PER_INQUIRY_24H` = 6 signals per inquiry per rolling 24 h; `coalesced` replies rode on a signal still to be posted). Every reply is stored and listed; only the extra dot activation is skipped. Read the inquiry's replies on the dashboard (Replies, filter by inquiry) or with `seller_inquiries_get` (`reply_count`, `latest_reply_id`). A worker uploading new replies beyond 120 per hour is answered `429` (`mail_worker_ingest_volume`) and keeps its backlog. | The window rolls on: the next reply after the 24 h window emits its own signal; investigate a seller or loop that keeps replying (pause the inquiries if needed). |
| Uncertain after a contradicting worker report | A worker reported `refused_before_send` for an intent whose claim was GRANTED (`.Send` may have been called): the attempt is `uncertain` with `REFUSED_AFTER_GRANTED_CLAIM`, the inquiry keeps its reservation and quota debit, and the guarded retry refuses it (`claim_granted_before_refusal`). Never resend. Check classic Outlook on the PC (Outbox, Sent Items) for the inquiry's Message-ID; if the report cannot be explained, treat the worker credential as compromised (revoke it, row above). | Positive evidence (Sent Items report, a correlated reply) moves the inquiry to `accepted`; then close its blocked send job with `jobs resolve-blocked` (row below). Without evidence it stays `uncertain` (visible attention item). |
| Blocked send job after reconciliation | `suv-deals jobs blocked` lists `EMAIL_DELIVERY_UNCERTAIN` send jobs. Once the inquiry left `uncertain` (accepted, or proven unsent and handled by the guarded retry): `suv-deals jobs resolve-blocked JOB_ID --outcome succeeded\|cancelled --reason ... --yes`. It refuses while the inquiry is still uncertain and never re-queues. Do NOT `jobs unblock` such a job unless the owner explicitly acknowledges the possible delivery (`--acknowledge-uncertain-delivery --owner-user-id`). | The job is `succeeded`/`cancelled` with `RESOLVED_AFTER_RECONCILIATION`, audited `job.resolve_blocked`. |
| Activation canary | `suv-deals canary status` (evidence state) and `suv-deals doctor` (`seller_inquiry/activation_canary`). A canary is never a seller inquiry; the send is the owner's step (section 10.6). Keep `SELLER_EMAIL_CANARY_SEND_ENABLED=false` outside that step. | `complete` (correlated test reply for the binding's current version) recorded in the activation log. |
| Disk / memory pressure | Reduce concurrency: run one worker, stop optional snapshots (`SNAPSHOT_STORAGE=disabled`), restart the affected service. | Resource recovery without losing queue state: `outbox inspect`, `reconcile --dry-run`. |

Readiness versus liveness: `/healthz` is process liveness only; `/readyz` includes database,
schema compatibility and critical configuration. Source health is separate (`deals_health`,
`sources list --from-db`): "no matching listings", "not scanned", "blocked" and "parser
unhealthy" stay distinct.

## 7. Backup and restore (spec §29)

Document the actually purchased Supabase backup/PITR capability for the project; do not assume a
plan feature. PROPOSED targets: at most a 24 h recoverable data gap and restore within 4 h, subject
to an approved plan and a measured drill.

```bash
# 1. Logical backup of app/ops (+ ledger) in one snapshot, with counts and hashes:
BACKUP_DATABASE_URL=<owner direct/session URL> scripts/backup.sh            # -> var/backups/
BACKUP_DATABASE_URL=... scripts/backup.sh --with-local-snapshots var/snapshots   # local evidence store

# 2. Prove it: restore into an ISOLATED local database (same or newer PostgreSQL major):
RESTORE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres \
  scripts/restore_check.sh var/backups/suv-deals_<ts>.manifest
```

- Neither script puts a password into a process argument list: the URL is split, `psql`,
  `pg_dump` and `pg_restore` get it without the password and libpq reads `PGPASSWORD`. Use the URL
  form (a key=value DSN with an inline password cannot be split; use `PGPASSWORD`/`~/.pgpass`).
- The member-id file contains auth user ids only (no e-mail, no password hashes); the restore
  check inserts them as minimal `auth.users` rows so memberships restore.
- The restore check verifies schema, every manifest row count, job/outbox states, lease
  invariants, latest revision/config/decision/audit timestamps, active-owner memberships, the
  migration ledger and revision evidence hashes (as `suv_backend`, which also proves grants and
  RLS), records the elapsed time and drops the database (`--keep` to inspect it).
- No worker, scheduler or dispatcher is ever started against a restored copy; restored jobs,
  subscriptions and outbox rows could act externally. Never point a process at it with network
  or notification switches on.
- **Storage objects are not in a database backup.** Back up the private evidence bucket (or
  `var/snapshots` / the worker's `snapshots` volume) separately with its retention, and verify by
  content hash
  (`suv-deals evidence verify` reads retained objects and compares hashes).
- A backup that has never been restored is not accepted as proven recovery.

## 8. Release and rollback

Release (spec §29, §31):

1. Commit; `scripts/verify_release.sh` on the exact commit (lint, mypy, schema snapshots, tests
   without DB, DB tests on PostgreSQL 16 and 17, migration hashes, lock hashes, YAML configuration
   hash, source adapter versions, MCP SDK/protocol) -> `var/releases/<sha>_<ts>.txt`. Add the
   stored configuration revision of the target (`suv-deals config apply --dry-run`).
2. Build the image with pinned base digests (Dockerfile header), record its digest
   (`RELEASE_IMAGE_DIGEST`) and the tested crawler digest (`CRAWL4AI_IMAGE_DIGEST`).
3. Back up (section 7); apply expand-first migrations (section 4.1).
4. Deploy with every switch off; smoke: `/readyz`, an authenticated `/api/me`, `deals_health` via
   MCP, `suv-deals worker --drain` on fixture data in a non-production workspace if available.
5. Enable only sources/destinations that passed their activation gates (Compose: export the
   matching `SUV_DEALS_*` switch from section 1 for that deployment, then `docker compose -f
   compose.production.yaml up -d`).

Rollback:

1. Redeploy the previous image digest (`SUV_DEALS_IMAGE=...@sha256:<previous>`). Migrations are
   expand/contract compatible; prefer forward fixes (`scripts/rollback.sh` prints the policy).
2. Verify: the running digest (`docker inspect`), `suv-deals doctor` (schema markers),
   `suv-deals reconcile --dry-run` (leases), `suv-deals sources list --from-db` (pauses kept),
   `suv-deals outbox inspect` (no duplicate deliveries). Old workers block jobs with newer payload
   versions (`incompatible_payload_version`) instead of misreading them.
3. Never delete historical listing/review/valuation data as a deployment repair.

## 9. Activation checklist (spec §32)

Each capability stays `implemented`/`fixture_verified` until its own evidence exists:
crawler (`doctor --crawler` against the real runtime), each source (terms decision + robots +
`crawl once` live smoke), Supabase (migrations + `doctor` + restore drill), MCP auth (real client,
`docs/connect_mcp.md`), MCP Events / Slack (docs/notification_bridge.md canary), tax rules
(docs/tax_rule_approval.md), seller email (docs/seller_email_activation.md).

## 10. Seller inquiries and the desktop mail worker (spec §37)

Owner decisions in force: ONE automatic initial inquiry per verified vehicle/seller pair
(availability, documents, lowest price) with no per-message or first-template approval (there is
no approve button anywhere); caps 2 per rolling 24 hours and 5 per rolling 15 days; default send
path `outlook_local` (classic Outlook on the owner's PC with the owner's Gmail account in it),
optional `gmail_api`. Nothing is sent unless `SELLER_INQUIRY_MODE=automatic`, the kill switch is
off, the standing authorization is active and the sender binding is verified. Fixture-lineage
listings never reserve or send. The owner's real mailbox address appears only in the runtime
configuration and the database, never in the repository or a ticket.

### 10.1 Sender binding (owner, once)

1. `suv-deals sender-binding create --workspace W --provider outlook_local --account-id
   <stable Outlook account key> --from-address <the owner's mailbox> --display-name "<name used as
   signature>" --reason "owner-authorized sending identity" --yes` (unverified, health `unknown`).
   For `gmail_api` add `--vault-ref <scheme:path>` naming the external secret store entry; never a
   token value.
2. After the desktop worker runs (10.2, 10.3) and has sent its account report and heartbeat:
   `suv-deals sender-binding verify <id> --reason "technical verification" --yes`. It refuses with
   problem codes (`NO_ACTIVE_MAIL_WORKER`, `WORKER_ACCOUNT_MISMATCH`, `WORKER_ACCOUNT_NOT_CLASSIC`,
   `WORKER_HEARTBEAT_STALE`, `ACCOUNT_KEY_MISMATCH`, `REPLY_TO_NOT_VERIFIABLE_ON_OUTLOOK_LOCAL`)
   until the evidence holds; then the binding is verified and `healthy` (`suv-deals sender-binding
   status`, `suv-deals doctor`). `gmail_api` is verified through the provider check of
   docs/seller_email_activation.md section 4/6 instead.

### 10.1a Controls, authorization and mode

1. `suv-deals inquiries authorize --workspace W --reason "owner standing authorization" --yes`
   creates the controls row (mode `disabled_until_sender_ready`, kill switch off, caps 2/5) and
   records the versioned standing authorization (spec 37.1) from
   `config/seller_inquiry_authorization.yaml`.
2. After 10.1-10.3 and the activation evidence (docs/seller_email_activation.md section 8):
   `suv-deals inquiries set-mode automatic --reason ... --expected-version N --yes` (run it with
   `SELLER_EMAIL_ACCOUNT_ID` / `SELLER_EMAIL_FROM` already set: a binding that is not exactly that
   configured identity is refused, `sender_identity_*`) and set the process setting
   `SELLER_INQUIRY_MODE=automatic` (both must be automatic; under Compose
   `SUV_DEALS_SELLER_INQUIRY_MODE`) for the worker AND the API process: the dispatcher plans
   nothing and the desktop worker's claim (served by the API) is refused `kill_switch` while the
   process setting is not `automatic` or `SELLER_INQUIRY_KILL_SWITCH=true`. `suv-deals inquiries
   status` (run with the same environment) then shows `process_mode: automatic` and
   `sending_possible: true` once `SELLER_EMAIL_ACCOUNT_ID` / `SELLER_EMAIL_FROM` (and
   `SELLER_EMAIL_REPLY_TO`, if the binding has one) name exactly the verified binding
   (`sender_identity_problems: []`; the runtime never sends from another identity, 10.5).
   Lower the caps any time with `inquiries set-limits`.

### 10.2 Issuing the mail-worker credential

`suv-deals mail-worker credential issue --workspace W --sender-binding <id> --label "Owner laptop
classic Outlook" --expires 90d --yes` prints, ONCE: the mailbox binding id and the `suvmail_`
token. The token is narrow (`mail:ingest` only, one workspace, one mailbox), revocable and stored
only as a hash. Type it directly into the PC's credential store (10.3); never paste it into a chat,
file, ticket or e-mail. Rotate by issuing a new binding and revoking the old one.

### 10.3 Installing the desktop worker on Windows (classic Outlook)

Follow `desktop/outlook-bridge/README.md` ("Installation"); in short:

- Classic Outlook for Windows with the owner's mailbox account added and synchronising (new Outlook
  has no Object Model; the worker reports it as unsupported). Do not change Trust Center,
  Object Model Guard, registry or antivirus settings to make it run.
- Python 3.12/3.13 venv, `requirements-windows.txt`, and this repository's revision of `suv_deals`
  installed `--no-deps` (the SAME revision as the backend).
- `config.toml` (no secrets): `api_base_url` (https), `mailbox_binding_id` from 10.2, the account
  SMTP address, the folders mailbox rules move mail into.
- `python -m outlook_bridge credential set --expires-at <expiry from 10.2>` (hidden input; Windows
  Credential Manager, current user).
- `python -m outlook_bridge check` must exit 0, then run `python -m outlook_bridge run` in the
  signed-in user session (never as a service/SYSTEM). The PC must be awake for timely replies;
  offline periods are recorded as coverage gaps.

### 10.4 Pausing, resuming and limits

- Pause (kill switch) from the dashboard, the MCP tool `seller_inquiries_pause` (`inquiries:pause`)
  or `suv-deals inquiries pause --reason ... --expected-version N --yes`. Untransmitted work stops
  at the next guard (reservation, dispatch, the worker's claim right before `.Send`).
- Resume is an owner action only: dashboard (`POST /api/inquiry-control/resume`) or
  `suv-deals inquiries resume --reason ... --expected-version N --yes`. With
  `--remove-suppressions --expected-suppressions K --owner-user-id <owner's user id>` (dashboard:
  `remove_suppressions` + `expected_removable_suppressions`), the kill-switch suppressions (and,
  while the authorization is effective, authorization-revoked ones) are removed, each with its own
  audit event. `K` is the `removable_suppressions` count the owner saw (`inquiries status`,
  `GET /api/inquiry-control`): if it changed meanwhile the whole resume is refused (`409
  VERSION_CONFLICT`, `suppressions_changed`; CLI exit 1) and nothing changes. Opt-out, bounce,
  complaint and other suppressions need their own explicit owner decision.
- The caps can only be lowered (`max_per_24h` 0..2, `max_per_15d` 0..5) and the seller cooldown only
  lengthened (7..365 days; omitted = unchanged). A claim refused for caps,
  seller cooldown or a paused source answers `not_now`: the worker keeps the intent and asks again
  later; it expires honestly (`intent_expired`) if the wait outlasts it.

### 10.5 Why nothing was sent (send job codes)

The job rows (`ops.jobs.last_error_code`, `blocker_code`) carry ids and codes only, never an address or
a body. The runtime codes of a `seller_inquiry_plan` / `seller_inquiry_send` job (`workers.inquiry_handlers`):

| Code | Where | Meaning and fix |
|---|---|---|
| `sender_identity_not_configured` (plan refusal) | plan | No verified binding of `SELLER_EMAIL_PROVIDER` is exactly the configured identity (`SELLER_EMAIL_ACCOUNT_ID`, `SELLER_EMAIL_FROM`, `SELLER_EMAIL_REPLY_TO`). Set them to the verified binding's values (`suv-deals doctor` requires them in automatic mode); another verified identity is never used. |
| `SENDER_SETUP_INCOMPLETE` (blocked) | send | The inquiry's binding is no longer the configured identity, or the sender secret is unusable: `SECRET_REFERENCE_BINDING_MISMATCH` (`SELLER_EMAIL_OAUTH_SECRET_REFERENCE` must name the bound binding itself), `SECRET_REFERENCE_UNSUPPORTED` (only `secretbox:` references), `SECRET_BOX_NOT_CONFIGURED` (the secret-box key is missing). Fix the setting; nothing was transmitted. |
| `SEND_HELD_PAUSED` (released) | send | Kill switch on or mode not automatic; the job waits without consuming attempts. |
| `MAILBOX_WORKER_MISSING`, `WORKER_HEARTBEAT_STALE` (released) | send, `outlook_local` | No active desktop worker for the sender, or it is offline; nothing was published. Start the worker (10.3). |
| `MAILBOX_WORKER_CREDENTIAL_NOT_LIVE` (released) | send, `outlook_local` | The mailbox's worker credential is revoked or expired; issue a new one (10.2). Its `ops.mail_worker_bindings` row stays `active` until a new binding replaces it. |
| `EMAIL_DELIVERY_UNCERTAIN` (blocked) | send | The message may have left; it is never resent. The reconciliation pass resolves it from evidence (Sent Items report, provider search, a correlated reply). |

A proven pre-submission refusal (for example a claim refused while paused) leaves the inquiry
`failed_definite`; the reconciliation pass enqueues one guarded-retry send job per attempt
number (`inquiry_retry_jobs` in `suv-deals reconcile`), at most three attempts in total. A
refusal reported AFTER a granted claim is not such a proof (section 6, "Uncertain after a
contradicting worker report").

### 10.6 Activation canary (the owner's one-time step)

The canary is the activation evidence of docs/seller_email_activation.md section 8 (rows 4-6): one
synthetic message from the configured sender to an OWNER-CONTROLLED test address, never a seller.
It is not a seller inquiry (no listing, seller, reservation or quota debit; never one of the
15-day deals). At most 5 canaries per rolling 24 hours (PROPOSED), none while the kill switch is
on (`canary prepare` refuses both with exit 3 and records nothing; the cap names the wait).

1. `SUV_CANARY_TARGET_ADDRESS=<owner test address> suv-deals canary prepare --workspace W --purpose
   "activation route check" --yes` (or omit the variable and type the address at the hidden
   prompt). The address is never echoed and is stored only as its SHA-256; unset the variable
   afterwards. The canary binds the configured sender binding's current version.
2. `suv-deals canary status` shows the canaries and the evidence state (`prepared`, `accepted`,
   `uncertain`, `failed`, `complete`, `stale` after a binding change); `doctor` reports the same.
3. The send is the OWNER's step, never run by an operator script or an agent:
   `suv-deals canary send <id> --i-confirm-owner-controlled-address --yes`. It refuses (exit 3,
   nothing sent, nothing changed) unless `SELLER_EMAIL_CANARY_SEND_ENABLED=true`,
   `SELLER_INQUIRY_MODE=automatic`, `SELLER_INQUIRY_KILL_SWITCH=false`, the workspace controls are
   `automatic` with the kill switch off, the standing authorization is active, the configured sender
   binding is usable and the canary is `prepared` for its current version (an `outlook_local`
   canary's desktop worker still active); the address is entered again and must match; right
   before the transport the controls are locked, EVERY gate is read again (a pause, a revoked
   authorization, sender binding or desktop worker committed meanwhile stops it) and the canary is
   committed `uncertain`, so a crash or a second `canary send` can never send it twice
   (an `uncertain` canary is reconciled, never re-sent). Hold real seller inquiries meanwhile
   with `inquiries set-limits --max-per-24h 0 --max-per-15d 0` (the caps do not apply to a
   canary).
4. **Current blocker:** no route has a canary transport yet (the providers send only registered
   seller templates and the desktop worker claims only inquiry intents), so step 3 ends with
   `CANARY_TRANSPORT_UNAVAILABLE` after every other gate. Rows 4-6 of the activation checklist stay
   open; keep `SELLER_EMAIL_CANARY_SEND_ENABLED=false` and report this blocker.
