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
| `SELLER_INQUIRY_MODE` | `disabled_until_sender_ready` | No seller email (spec §37; wiring is a later package). |
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
