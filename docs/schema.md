# Database schema (Milestone M2)

The PostgreSQL schema for the SUV deal-discovery system: tables, invariants, the
row-level-security and grant model, lock order, append-only history, and how to apply
migrations. It is the reference for the persistence, worker, API and MCP packages.
Product contract: `docs/spec/suv-deal-system-build-spec.md` (cited below as "spec §N").
Access model: `docs/decisions/0001-bff-only-data-access.md` (ADR 0001).

| Path | Purpose |
|---|---|
| `supabase/migrations/2026100600{01..08}00_*.sql` | Forward-only migrations, applied in name order |
| `supabase/tests/supabase_emulation.sql` | **Test-only** Supabase emulation for plain PostgreSQL |
| `supabase/seed.sql` | **Local development only**: one synthetic workspace |
| `supabase/config.toml` | Supabase CLI local config (`app`/`ops` not exposed) |
| `scripts/migrate.sh`, `scripts/rollback.sh` | Guarded apply, and the forward-fix policy |
| `tests/integration/db/` | Real-PostgreSQL tests (marker `db`) |

## 1. Applying migrations

### Production and staging: `scripts/migrate.sh`

```bash
DATABASE_URL=postgresql://... scripts/migrate.sh --dry-run   # show target + pending, change nothing
DATABASE_URL=postgresql://... scripts/migrate.sh             # asks you to type the database name
DATABASE_URL=postgresql://... scripts/migrate.sh --yes       # non-interactive (CI/automation)
```

- The script prints the target **as reported by the server**: database, address, port, role
  and version. It never prints the URL or the password. It then lists the pending files.
- It applies each pending file with `psql -X -v ON_ERROR_STOP=1 --single-transaction`. The
  same transaction inserts the ledger row into `supabase_migrations.schema_migrations`, the
  ledger the Supabase CLI uses, so `supabase migration list` agrees.
- `PGOPTIONS` sets `lock_timeout=10s`, `idle_in_transaction_session_timeout=120s` and
  `client_min_messages=warning`.
- The script refuses (exit 3) when:
  - there is no terminal and `--yes` is absent;
  - the typed name does not match;
  - `app`/`ops` exist but there is no ledger;
  - the ledger contains a version this checkout does not have;
  - a file name does not match `^[0-9]{14}_[a-z0-9_]+\.sql$`.
- It never applies the test emulation or `seed.sql`.
- Before production (spec §29): take a backup, run the full suite on the exact build, and
  apply expand-first migrations only.
- psql receives the URL as an argument, so run the script only on a trusted host.

### Rollback: forward fixes only

`scripts/rollback.sh` prints the policy and exits 0. `--status` shows the ledger read-only.
Every other argument (a version, `--down`, ...) is refused with exit 2. To roll back:

1. Redeploy the previous image digest. Migrations are expand/contract compatible.
2. Write a new `..._fix_<topic>.sql` migration.
3. Never delete history as a deployment repair.
4. Destructive recovery means restoring a backup into an isolated environment with
   crawling and notifications disabled (spec §29).

### Tests and local plain PostgreSQL

`tests/db_harness.py` creates one uniquely named database per pytest session. When there
is no `auth` schema it first applies `supabase/tests/supabase_emulation.sql`. It then
applies every migration as one multi-statement string through psycopg. That is why the
migrations contain plain SQL and `DO` blocks only: no psql meta-commands and no
`BEGIN`/`COMMIT`. Run the suite with:

```bash
uv run pytest tests/integration/db -q      # needs TEST_DATABASE_ADMIN_URL (superuser), default suv@127.0.0.1
```

The emulation provides what Supabase provides:

- the roles `anon` and `authenticated` (NOLOGIN), and `service_role` (NOLOGIN, BYPASSRLS);
- `auth.users` (a minimal subset);
- `auth.uid()`, `auth.role()`, `auth.email()` and `auth.jwt()`, reading `request.jwt.claims`
  or `request.jwt.claim.<x>`;
- the `extensions` schema with pgcrypto.

It is idempotent, and it tolerates concurrent role creation, because roles are
cluster-global. It **refuses** to touch an `auth` schema that it did not create, so it
cannot damage a real Supabase project. Migration 0100 itself refuses to run without these
prerequisites.

Supabase ships PostgreSQL 15/17 (`config.toml` uses 17). The tests run on PostgreSQL 16.15
and use only features available in 15 and later. Re-run the suite against the real stack
before the Supabase activation gate.

## 2. Security model (ADR 0001, spec §12)

### Roles

| Role | Access to `app`/`ops` |
|---|---|
| `anon`, `authenticated`, `service_role`, `PUBLIC` | **None.** No schema USAGE, and no table, sequence or routine privileges. `app`/`ops` are not in `[api].schemas`, so the Data API cannot expose them. Do not add them on the hosted dashboard either. |
| `suv_backend` (NOLOGIN, NOINHERIT, no BYPASSRLS) | Schema USAGE plus the explicit per-table grants below. A deployment LOGIN user is made a member, or the process runs `SET ROLE suv_backend` (`Settings.database_set_role`). |
| migration owner (`postgres` on Supabase, the superuser in tests) | Owns every object, bypasses RLS (RLS is not FORCEd). Used only for migrations, tests and documented maintenance. |

Roles are cluster-global, so `suv_backend` may already exist when a migration runs. The
owner-only function `ops.backend_role_problems()` lists anything that would void tenant
isolation (SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB, REPLICATION, LOGIN, or membership
in another role, which would allow `SET ROLE` to e.g. the table owner). Migrations 0100
and 0800 refuse to proceed (`42501`) when it is non-empty.

`service_role` gets no grants. With the BFF-only choice the service key cannot reach
`app`/`ops` through the Data API anyway. A direct-SQL fallback must use a LOGIN member of
`suv_backend`. If the service role is ever needed, it requires a new migration with
explicit grants and tests.

### Transaction GUCs

The repository layer sets these with `select set_config(name, value, true)`, which is
transaction-local, at the start of every transaction:

| GUC | Helper | Used by |
|---|---|---|
| `app.workspace_id` | `app.current_workspace_id()` | `tenant_isolation` on every workspace-owned table |
| `app.user_id` | `app.current_user_id()` | `membership_self_read`, `workspace_member_read` (bootstrap) |
| `app.credential_hash` | `app.current_credential_hash()` | `credential_lookup` on `ops.api_credentials` (bootstrap) |

An unset or empty GUC is NULL, so every comparison fails closed and a backend transaction
without a workspace sees **nothing**. A malformed UUID raises `22P02`.

### Policies

- **`tenant_isolation`**, `FOR ALL TO suv_backend`, on every table with `workspace_id`:
  `USING` and `WITH CHECK (workspace_id = (select app.current_workspace_id()))`. A row
  therefore cannot be read, written or moved across workspaces. `app.workspaces` uses
  `id = ...`.
- **`membership_self_read`** (SELECT): `user_id = app.current_user_id()`, for the
  membership bootstrap before a workspace is chosen.
- **`workspace_member_read`** (SELECT) on `app.workspaces`: workspaces where the current
  user has an *active* membership.
- **`credential_lookup`** (SELECT) on `ops.api_credentials`: the row whose `token_hash`
  equals the SHA-256 of the presented bearer token. Updating `last_used_at` still needs the
  workspace GUC.
- **`ops.active_workspace_ids()`**: the only `SECURITY DEFINER` routine. It returns the ids
  of active workspaces for scheduler/worker fan-out. It is `STABLE`, sets
  `search_path = ''`, has EXECUTE revoked from everyone and is granted only to
  `suv_backend`.
- Composite foreign keys are checked by PostgreSQL bypassing RLS. Cross-workspace links are
  therefore rejected even for privileged connections.

### Grants to `suv_backend`

| Privilege | Tables |
|---|---|
| SELECT, INSERT (append-only) | `app.config_revisions`, `app.detail_observations`, `app.listing_revisions`, `app.listing_aliases`, `app.listing_observations`, `app.field_evidence`, `app.market_observations`, `app.comparable_sets`, `app.comparable_set_members`, `app.fx_rates`, `app.cost_evidence`, `app.review_decisions`, `ops.audit_events`, `ops.delivery_attempts`, `ops.robots_revisions`, `ops.fetch_attempts` |
| SELECT, INSERT, UPDATE | `app.sources`, `app.search_profiles`, `app.listings`, `app.vehicle_clusters`, `app.tax_rule_sets` (trigger-guarded), `app.review_cases`, `app.watchlists`, `app.destination_bindings`, `app.notification_preferences`, `ops.jobs`, `ops.crawl_runs`, `ops.source_schedules`, `ops.host_budgets`, `ops.outbox`, `ops.event_subscriptions`, `ops.event_deliveries`, `ops.activation_gates` |
| SELECT, INSERT, column UPDATE | `app.valuations (state, stale_at, stale_reason)`, `app.cost_profiles (approval_status, approved_by, approved_at)`, `ops.source_snapshots (redaction_status, redacted_at, purged_at)`, `ops.api_credentials (last_used_at, revoked_at, revoked_by, revoke_reason)`, `app.vehicle_cluster_members (manually_confirmed, confirmed_by, confirmed_at, unlinked_at, unlinked_by, unlink_reason)`, `app.memberships (role, active)`, `app.owner_notes (body, row_version)` |
| SELECT, column UPDATE | `app.workspaces (name, display_timezone)`. Workspaces are provisioned by the owner role. |
| SELECT, INSERT, DELETE | `ops.query_snapshots` (expiry) |
| SELECT, INSERT, UPDATE, DELETE | `ops.idempotency_records` (expiry) |
| EXECUTE | `app.current_workspace_id()`, `app.current_user_id()`, `app.current_credential_hash()`, `app.text_array_ok()`, `app.uuid_array_ok()` (used by CHECK constraints, which need EXECUTE), `ops.active_workspace_ids()` |

`suv_backend` has no TRUNCATE, no REFERENCES/TRIGGER and no DDL. It cannot call
`ops.apply_security_baseline()`.

### Baseline and default privileges

`ops.apply_security_baseline()` is an owner-only procedure that every migration calls at
its end. It:

- revokes everything on `app`/`ops` (schemas, tables, sequences, routines) from `PUBLIC`,
  `anon`, `authenticated` and `service_role`;
- enables RLS on every `app`/`ops` table;
- creates a `tenant_isolation` policy on every table that has `workspace_id` and lacks one.

Migration 0100 also sets these default privileges for the migration role:

- `ALTER DEFAULT PRIVILEGES IN SCHEMA app, ops REVOKE ALL ... FROM PUBLIC, anon,
  authenticated, service_role`;
- the global `ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC`. Without
  it, PostgreSQL would make every new function executable by PUBLIC.

**Checklist for a new migration:**

- Add `workspace_id uuid not null`, `unique (workspace_id, id)`, composite FKs and
  `created_at timestamptz`.
- Add explicit `grant`s to `suv_backend`.
- End with `call ops.apply_security_baseline();`.
- Add the table to `ALL_TABLES` in `tests/integration/db/test_schema_catalogue.py`.
  `test_spec_table_catalogue_exists` fails for any undocumented table.

## 3. Conventions and invariants

- UUID primary keys (`gen_random_uuid()`) and `timestamptz` everywhere. There is no
  `timestamp without time zone` and no float column (both are tested).
- **Workspace ownership.** Every table except `app.workspaces` has `workspace_id NOT NULL`
  and `unique (workspace_id, id)` (except `memberships`, whose PK is
  `(workspace_id, user_id)`). Every cross-table link is a composite FK that includes
  `workspace_id`. Where a parent relation matters, the FK also includes the parent:
  - `listing_revisions(workspace_id, listing_id, id)`: a review case, valuation, decision,
    comparable set or field evidence must cite a revision **of the same listing**;
  - `listings(workspace_id, source_id, id)` for aliases and card observations;
  - `review_decisions(workspace_id, case_id, id)` for `latest_decision_id` and
    `supersedes_id`.
- **Money** is `<name>_minor bigint` plus `currency char(3)`, both present or both absent
  (CHECK). FX rates are `numeric(20,10) > 0`. Mileage is `numeric(14,6)`. Price thresholds
  are `numeric(12,2)`. Unknown is NULL, never 0:
  - not-started/incomplete valuations must have NULL contributions;
  - estimated/quote-supported valuations must have both base and conservative
    contributions.
- **Enum columns** are `text` with CHECKs whose values mirror
  `src/suv_deals/domain/enums.py` exactly. Renaming a value needs a migration.
- **Business baseline** (spec §3), enforced in `app.search_profiles` as well as in
  `domain.profiles`:
  - `primary` must be exactly EUR 2,500.00 to 3,000.00 inclusive, enabled, with mileage
    `< 200000`;
  - `manual_4000` has a ceiling of EUR 4,000.00;
  - `below_target_watch` is `< 2500.00` (exclusive);
  - no profile may allow `>= 200000` km;
  - queue labels are unique, so optional profiles are a visibly different queue.

  The mileage rule is deliberately **not** a constraint on listing tables: rejected
  observations are retained (spec §11).
- **Source activation gate** (`sources_enable_gate_ck`) mirrors
  `domain.sources.activation_problems()`. `enabled = true` requires:
  - an implemented adapter;
  - reviewed terms, with a `proceed_*` decision that names its actor;
  - a technical status that is not untested, access_blocked or parser_unhealthy;
  - allowed hosts; detail paths unless `detail_mode = 'card_only'` (a robots/terms
    decision that limits the source to search cards); search paths for acquisition
    sources.

  `robots_policy = 'obey'` and `technical_denial_policy = 'stop_and_report'` are fixed.
- **Fixtures never leak into reality** (spec §18), and `is_fixture` is immutable wherever
  rows can be updated:
  - `tax_rule_sets.is_fixture` can never be approved or active, and cannot be flipped even
    in `draft` (`SV004`);
  - `cost_profiles.is_fixture` is never approved;
  - a non-fixture valuation cannot depend on fixture FX rates, cost evidence, tax rules,
    cost profiles or comparable sets (insert trigger);
  - a non-fixture review case cannot reference a fixture valuation; a review decision
    carries its case's `is_fixture`, and a real decision cannot cite a fixture valuation
    (`SV003`);
  - a fixture `ops.outbox` row can only be `blocked` or `cancelled`; its `is_fixture` (and
    every other identity column) is frozen, and a fixture review case cannot produce a
    non-fixture `review_case` event (deferred constraint trigger, checked at commit
    whatever the write order). Fixtures therefore cannot produce external notifications.

### Guard SQLSTATEs (raised by triggers; map them in the persistence layer)

| SQLSTATE | Meaning | Raised by |
|---|---|---|
| `SV001` | Append-only history: UPDATE/DELETE refused | `app.reject_history_mutation()` |
| `SV002` | State transition not permitted | tax rule lifecycle, valuation state |
| `SV003` | Dangling, cross-workspace or fixture-contaminated reference, or an estimated valuation without an approved/active tax rule set | `app.valuations_check_dependencies()`, `app.review_cases_check_fixture_lineage()`, `app.review_decisions_check_fixture_lineage()`, `ops.outbox_check_fixture_lineage()` (at commit) |
| `SV004` | Frozen column modified | `app.guard_frozen_columns()` (incl. outbox event identity), listing identity, tax rule content, approval and `is_fixture` |
| `SV005` | Monotonic value would decrease (row/case/source version, listing `detail_generation`, promoted `current_generation`, `last_seen_at`) | `app.guard_version()`, `app.listings_guard()` |
| `SV006` | Detail observation generation was never allocated | `app.detail_observations_check_generation()` |

RLS violations raise `42501`. Constraint violations raise standard `23xxx` codes; the
constraint names are stable and are asserted by the tests.

## 4. Lock order and transactions

The global lock order, which every code path must follow:

```text
ops.jobs row -> app.sources -> app.listings -> app.listing_revisions
  -> app.valuations -> app.review_cases -> ops.outbox
```

- Network I/O never happens inside a transaction.
- A worker commits a result like this:
  1. Lock the job row and revalidate state, token, owner and `lease_expires_at >
     clock_timestamp()`.
  2. Lock rows in the order above and apply idempotent writes.
  3. Run the guarded completion update. If it updates 0 rows, roll back everything.
- The same transaction writes the domain change and its `ops.outbox` row.
- `current_revision_id` and the `(current_generation, current_observation_id)` pointer are
  `DEFERRABLE INITIALLY DEFERRED`. A revision and its promotion may be written in either
  order inside one transaction.

## 5. Append-only history

These tables are append-only:

`app.config_revisions`, `app.listing_revisions`, `app.detail_observations`,
`app.listing_observations`, `app.listing_aliases`, `app.field_evidence`,
`app.market_observations`, `app.comparable_sets`, `app.comparable_set_members`,
`app.fx_rates`, `app.cost_evidence`, `app.review_decisions`, `ops.audit_events`,
`ops.delivery_attempts`.

Two layers protect them:

1. `suv_backend` has no UPDATE or DELETE privilege on them.
2. A `BEFORE UPDATE OR DELETE` trigger raises `SV001` for **every** role, superusers
   included.

A correction is a new superseding row (for example `field_evidence.supersedes_id` or
`cost_evidence.supersedes_id`) or a new revision.

**Documented maintenance bypass.** This exists only for the owner-approved privacy
deletion process of spec §24, never for rollbacks. A member of the table-owner role sets
`select set_config('app.history_maintenance', 'on', true)` inside the maintenance
transaction. `suv_backend` can never qualify. `ops.robots_revisions` and
`ops.fetch_attempts` are insert-only by grant but have no trigger, so retention purges can
run as the owner.

Content is also frozen on some lifecycle tables (`SV004`):

| Table | What may change |
|---|---|
| valuations | only `state`/`stale_*`, and only "mark stale" |
| outbox events | only delivery lifecycle columns and `destination_binding_id` (event id, type, aggregate, payload + hash, dedup key, `is_fixture` frozen) |
| cost profiles | only approval |
| snapshots | only redaction/purge |
| credentials | only usage/revocation |
| cluster members | only confirm/unlink |
| idempotency records | only outcome/expiry |
| owner notes | only body/version |
| review cases | identity: listing, profile, fixture flag (everything else is lifecycle) |
| tax rule sets | everything except lifecycle columns, once out of draft |

## 6. Table catalogue

Spec §11 lists 37 tables, and all of them exist. This system adds six more:
`app.detail_observations`, `app.comparable_set_members`, `app.destination_bindings`,
`ops.host_budgets`, `ops.robots_revisions` and `ops.api_credentials`.

### Core (migration 0200)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `app.workspaces` | Tenant: name, `display_timezone` (Europe/Skopje), `active` | `tenant_isolation` on `id`; `workspace_member_read` |
| `app.memberships` | user to workspace, role owner/reviewer/viewer, `active` | PK `(workspace_id, user_id)`; FK `auth.users`; `memberships_user_idx (user_id, workspace_id) WHERE active` |
| `app.config_revisions` | Immutable business-config history: `revision`, `config`, `config_hash`, `before`, author principal/kind/label, `reason`, `effective_at` | `unique (workspace_id, revision)`; append-only |
| `app.sources` | Source registry mirroring `SourceConfig`. Typed: key, country, role, mode, adapter(+version), terms status/decision/actor/note/url/reviewed_at, robots/denial policy, host/path allow-lists, timezone, `detail_mode` (`fetch`/`card_only`), pause fields, `last_live_smoke_at`. Full config in `config` jsonb; row `version` | `unique (workspace_id, source_key)`; activation gate CHECK; `version` never decreases |
| `app.search_profiles` | `profile_key` primary/manual_4000/below_target_watch, labels, `enabled`, `min/max_price_eur numeric(12,2)`, `max_price_inclusive`, `max_mileage_km_exclusive numeric(14,6)`, `criteria`, `config_revision_id` | baseline CHECKs; unique key and queue label per workspace |

### Queue and crawl operations (migration 0300)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `ops.jobs` | Durable queue (spec §13 fields), plus `source_id`, `profile_id`, `partition_key`, `scheduled_slot`, `listing_id` and `generation` (detail/recheck binding), `blocker_code/detail`, `last_error_detail` | See the job invariants below |
| `ops.crawl_runs` | One traversal: versions, start/end, `outcome` (`running` or a Completeness value or `cancelled`), page/card/new/changed/detail counts, watermarks, `gap_reasons`, `access_state` | `outcome='running'` iff `finished_at` is null; rolling_pages runs carry no watermark; `unique (workspace_id, source_id, id)` so card observations, fetch attempts and schedules can only cite a run **of their own source** |
| `ops.source_schedules` | Per (source, profile, partition): `next_due_at`, `last_slot`, `cursor`, `run_id`, `coverage_mode`, `complete_watermark`, `page_depth`, `last_complete_traversal_at`, `incomplete_since`, `gap_reasons`, `backoff_until`, failures, pause, `row_version` | `unique (workspace_id, source_id, profile_id, partition_key)`; `coverage_mode='rolling_pages'` ⇒ `complete_watermark IS NULL` (never fabricated) |
| `ops.host_budgets` | Persistent token bucket (`capacity`, `refill_per_second`, `tokens`, `refilled_at`, `next_request_not_before`), circuit breaker, daily counters, Retry-After | `0 <= tokens <= capacity`; `open` needs `open_until`; host lower-case |
| `ops.robots_revisions` | Every robots.txt fetch: host, time, status, `content_hash`, body (max 512 KiB), `parse_ok` | index `(workspace_id, host, fetched_at desc)` |
| `ops.source_snapshots` | Raw-response metadata: `url_hash` (never the raw URL), `content_hash`, MIME, bytes, retention (`hash_only` or `retain_until` with a private `object_key`), redaction/purge | `object_key` is never a URL, absolute path or traversal |
| `ops.fetch_attempts` | Redacted `FetchOutcome`: purpose, url hash(es), host, status, `access_state` (AccessState), error code, timings, bytes, redirects, Retry-After, allow-listed headers, snapshot | `success` ⇒ `access_state='ok'` |

**Job invariants:**

- `state` is in the 7 JobState values.
- `0 <= attempts <= max_attempts`, and `max_attempts` is between 1 and 50.
- `running` ⇒ `lease_owner`, `lease_token` and `lease_expires_at` are all set.
- `queued`/`retry_wait` ⇒ no lease token or expiry (a new claim mints a fresh token).
- `blocked` ⇒ `blocker_code`.
- `succeeded`/`dead_letter`/`cancelled` ⇒ `completed_at` is set; waiting and running jobs
  have none.
- `detail`/`recheck` jobs carry `listing_id` and `generation`.
- `scheduled_slot` ⇒ source, profile and partition are set.

**Job indexes:**

| Index | Definition |
|---|---|
| `jobs_dedup_open_uidx` | UNIQUE `(workspace_id, dedup_key)` WHERE state is queued, running, retry_wait or blocked |
| `jobs_scheduler_slot_uidx` | UNIQUE `(workspace_id, source_id, profile_id, partition_key, scheduled_slot)`, for **all** states, so a slot can never be scheduled twice |
| `jobs_due_idx` | `(workspace_id, available_at, priority, id)` WHERE queued/retry_wait |
| `jobs_lease_expiry_idx` | `(lease_expires_at)` WHERE running |
| `jobs_exhausted_idx` | waiting jobs with `attempts >= max_attempts` |

### Listings and evidence (migration 0400)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `app.listings` | Source listing (offer, not a physical vehicle). Identity: `source_listing_id`, `incarnation`, `canonical_url`, `identity_method/material/hash/confidence`, `identity_conflict`. First/last seen, detail/availability check times, `availability`, `current_revision_id`. Allocator `detail_generation`, promoted `(current_generation, current_observation_id)`. Screening: `eligibility_state/profile`, `screening`, `screening_version`, `screened_at`. `quarantined`, `row_version` | `unique (workspace_id, source_id, source_listing_id, incarnation)`; `unique (..., identity_hash, incarnation)` (a hash collision fails and is never merged); deferred composite FKs to the current revision/observation; guard trigger (identity immutable, allocator/generation/last_seen/version monotonic); `listing_recent_idx (workspace_id, last_seen_at desc, id)` |
| `app.detail_observations` | Every accepted detail parse, including late older generations (`promoted=false`): generation, `observation_id` (the durable tie-breaker), job/fetch/snapshot, `semantic_hash`, normalized, provenance, versions, page type, availability | `unique (workspace_id, listing_id, generation, observation_id)` (an idempotent replay is `ON CONFLICT DO NOTHING`); the generation must already be allocated (`SV006`); append-only |
| `app.listing_revisions` | Immutable semantic revisions: `revision_number`, `semantic_hash`, price (`asking_minor`+`currency`, basis, type), `mileage_km`, availability, typed vehicle columns (country, make, model, generation, registration year/month, fuel, gearbox, drive, body), normalized, provenance, parser, `(detail_generation, observation_id)`, `quarantined` | `unique (workspace_id, listing_id, revision_number)`; **no** unique on `semantic_hash` (A→B→A allowed); append-only. `revisions_listing_idx` is served by the unique index scanned backwards |
| `app.listing_aliases` | Alternative or changed URL with reason and evidence | `unique (workspace_id, source_id, alias_hash)`; alias source = listing source |
| `app.listing_observations` | Search cards: run, page, position, `card_hash`, `card_material`, card price/currency/mileage, source timestamps, `ingestion_key` | `unique (workspace_id, ingestion_key)` (no replay); append-only |
| `app.vehicle_clusters` | `possible_same_vehicle` grouping: confidence, review status, match basis (no plates or contact data) | reviewed ⇒ reviewer and time |
| `app.vehicle_cluster_members` | Listing in cluster: evidence, confidence, `manually_confirmed`; false-positive unlink keeps the row with `unlinked_at/by/reason` | one *active* membership per (cluster, listing) |
| `app.field_evidence` | Field-path evidence: revision, snapshot or `document_ref`, raw excerpt (max 500), method, confidence, `claim_status`, verification, `supersedes_id` | `claim_status='verified'` ⇒ `verified_by`; append-only |

### Market, tax, costs and valuations (migration 0500)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `app.market_observations` | MK evidence: `evidence_kind` (asking_price, seller_reported_sale, verified_sale, owner_estimate), amount+currency, basis, normalized plus typed comparable columns, `local_registration_status`, seller type, cluster, URL or archived snapshot, confidence, evidence | currency pairing; only `seller_reported_sale` may lack an amount; `verified_sale` needs evidence; `owner_estimate` needs `recorded_by`; comparable index `(workspace_id, market, make, model, vehicle_generation, registration_year, fuel, gearbox, drive, observed_at desc)`; append-only |
| `app.comparable_sets` | Reproducible selection for a target revision: criteria (+version), counts, `sample_quality` (adequate, small or insufficient), statistics, date span, rationale | FK `(workspace_id, listing_id, target_revision_id)`; append-only |
| `app.comparable_set_members` | Selected or excluded observation with `reasons[]`, `differences`, `widened_dimensions[]`, `weight` | excluded ⇒ at least one reason and no weight; append-only |
| `app.fx_rates` | `1 base = rate quote`: rate date, retrieval time, provider, purpose (reference, customs or payment) | `rate > 0`, `base <> quote`, `unique (workspace_id, base, quote, rate_date, provider, purpose)`; append-only |
| `app.tax_rule_sets` | Versioned owner-supplied rule sets: jurisdiction, `vehicle_category`, version, status (8 TaxRuleStatus values), `valid_from/valid_to date`, currency, `rules`, `sources`, `sha256`, approval | See the tax rule invariants below |
| `app.cost_profiles` | Versioned cost assumptions with approval status | approved ⇒ approver and time; fixture never approved |
| `app.cost_evidence` | quote/estimate/actual, category (CostCategory), provider, `low/base/high_minor` plus currency, obtained/expiry, `scope`, evidence, optional listing, `supersedes_id` | `low <= base <= high` for every present pair; currency iff an amount; quote ⇒ provider; append-only |
| `app.valuations` | One reproducible valuation | See the valuation invariants below |

**Tax rule invariants:**

- `valid_to > valid_from`.
- Approved/active rule sets require approver, approval time, at least one source, `sha256`,
  `valid_from` and currency.
- A fixture rule set is never approved or active.
- An exclusion constraint (btree_gist) forbids overlapping **active** ranges per
  (workspace, jurisdiction, category).
- Allowed lifecycle transitions: draft→under_review→approved→active→superseded, expired or
  revoked; under_review→draft for rework; revoke from any open state.
- Content is frozen after draft, `valid_to` may only shorten, and the approval record is
  immutable.

**Valuation contents:** revision, comparable set (of the same listing), tax rule set, cost
profile, config revision, `fx_rate_ids[]`, `cost_evidence_ids[]`,
`dependency_fingerprint`, `state` (ValuationState), `scenarios`, `unknowns`, `warnings`,
`calculation_version`, currency, base/conservative/upside contribution, expiry,
`stale_at/reason` and `is_fixture`.

**Valuation invariants** (identical to `domain.valuation.Valuation`, so every row loads):

- Unknown is never zero: not_started/incomplete/invalid carry no contribution figures;
  estimated/quote_supported carry base and conservative contributions.
- `stale_at`/`stale_reason` are set exactly when the state is `stale`.
- Insert trigger: array ids must exist in the workspace, there is no fixture contamination,
  and a non-fixture estimated/quote-supported valuation needs an approved or active tax
  rule set.
- Content is immutable. The only state change is "mark stale" (from not_started,
  incomplete, estimated or quote_supported; `domain.valuation.mark_stale`). `invalid` is
  assigned at insert; `stale` and `invalid` are terminal. A recalculation is a new row.

### Reviews and notifications (migration 0600)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `app.review_cases` | Case per (listing, profile): revision, valuation, `queue_label`, `state` (ReviewState), `readiness`, `priority`, `ranking` + version, `row_version` (the case version clients send), claim (`claim_holder`, `claim_token_hash` (only a SHA-256), `claimed_at`, `claim_expires_at`), `latest_decision_id`, `superseded_by_id`, `is_fixture` | See the review invariants below |
| `app.review_decisions` | Immutable decision: case + `case_version`, listing revision, valuation, actor principal/kind/role, outcome (ReviewOutcome), `reason_codes[]` (1 to 20, max 80 characters each), summary (10 to 4000), `evidence_ids[]` (max 100), `missing_information[]` (max 30 × 300), model name/version/run, prompt template version, tool request id, `input_hash`, `supersedes_id` | `unique (workspace_id, case_id, case_version)` (one decision per case version, safe for timeout retries); decision listing = case listing; append-only |
| `app.watchlists` | Member watch: reason, expiry, recheck interval (1 h to 30 d), `next_recheck_at` | one active watch per (listing, member) |
| `app.owner_notes` | Private notes: label owner/reviewer/assistant, body (1 to 4000), `row_version` | separate from extracted claims |
| `app.destination_bindings` | Approved slack/mcp_events destination: external ids (never secrets or webhook URLs), approval reference/by/at, `enabled`, `verified_at` | enabled ⇒ an approval is recorded; slack ⇒ team and channel ids |
| `app.notification_preferences` | Per binding: event categories, quiet hours, urgency policy, `enabled`, approval | enabled ⇒ an approval is recorded |

**Review case invariants:**

- `claimed` ⇒ holder, token hash and expiry are all set.
- Any non-claimed state ⇒ no claim data.
- Decided states (needs_information, watch, shortlisted, rejected) ⇒ `latest_decision_id`
  is set, via a deferred FK that also checks the decision belongs to this case.
- At most one non-superseded case per (workspace, listing, profile):
  `review_cases_open_uidx`.
- `superseded_by_id` names a case of the **same listing** (a relisted vehicle is a new
  incarnation and never inherits review history).
- The queue index is `review_queue_idx (workspace_id, state, priority desc, created_at,
  id)`.
- `row_version` is the optimistic-concurrency case version (`expected_version`). Every
  accepted change (claim, release, submit, new material information) increments it, as
  `domain.reviews` does; it never decreases.
- Fixture lineage: see "Fixtures never leak into reality" above.

### Outbox, events, pagination, idempotency, audit, gates, credentials (migration 0700)

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `ops.outbox` | Transactional outbox: `event_id` (globally unique), type, version, aggregate type/id/version, destination binding, payload (max 256 KiB) + `payload_hash`, `dedup_key`, state (8 OutboxState values), attempts, lease, `event_created_at`, `send_attempted_at`, `provider_accepted_at`, `owner_seen_at`, `last_error_code`, `blocker_code`, `is_fixture` | See the outbox invariants below |
| `ops.delivery_attempts` | One row per send: `attempt_id` (unique), number, provider, `sent_at`, `completed_at`, receipt, response code, error, `uncertain` | uncertain ⇒ no receipt (reconciliation appends a new row); append-only |
| `ops.event_subscriptions` | MCP Events subscription: principal, credential, `event_name`, `canonical_filter` + `filter_hash`, HTTPS `callback_url`, `encrypted_secret bytea` (only ciphertext; the key stays with the event bridge), `secret_version`, optional `previous_encrypted_secret` + `previous_secret_valid_until` for a short rotation window (both or neither), `credential_id` (FK to `ops.api_credentials` of the same workspace; NULL for OAuth principals), verification state/challenge hash/verified_at, `expires_at`, `refresh_deadline`, revocation, `replay_position` (NULL in the MVP), `version` | `unique (principal_id, callback_url, event_name, filter_hash)` (refresh, never duplicate); finite lifetime; callback must be HTTPS without credentials |
| `ops.event_deliveries` | Per (subscription, event): state, attempts, lease, `next_attempt_at`, `replay_sequence`, `last_response_code`, `safe_error`, `accepted_at` | `unique (subscription_id, event_id)`; FK `(workspace_id, event_id)` to the outbox |
| `ops.query_snapshots` | Frozen ordered `result_ids[]` (max 10k) + `projections` bound to principal/workspace/`query_name`/`filter_hash` | `expires_at` within 1 day; DELETE for expiry |
| `ops.idempotency_records` | (principal, operation, `idempotency_key`) to `request_hash`, state in_progress/completed/failed, `result`, `error_code`, expiry | unique key; identity/hash frozen; completed ⇒ result; failed ⇒ error code |
| `ops.audit_events` | Actor, action, target, prior/new version, reason, request id, redacted metadata | append-only; indexes by time and target |
| `ops.activation_gates` | Spec §32 gates per workspace: capability, dependency, required evidence, `status` (GateStatus), owner, next action, evidence, `checked_at` | `unique (workspace_id, capability)`; `live_verified`/`active` ⇒ evidence and `checked_at` |
| `ops.api_credentials` | Static-bearer/dev MCP credentials: principal and kind, role, `token_hash` (SHA-256, **never the token**), optional non-secret prefix, `scopes[]` (subset of the Scope values), label, finite `expires_at`, revocation, `last_used_at` | `unique (token_hash)`; scopes are Scope values and never exceed the member role (`api_credentials_role_scopes_ck` mirrors `domain.actor.ROLE_SCOPES`); rotation means a new row |

**Outbox invariants:**

- `sending` ⇒ a lease; `pending`/`retry_wait` ⇒ no lease.
- `delivered` ⇒ `provider_accepted_at`.
- Accepted ⇒ attempted, and seen ⇒ accepted.
- `blocked` ⇒ `blocker_code`.
- A fixture row is only ever `blocked` or `cancelled`; a fixture review case cannot
  produce a non-fixture event.
- Event identity is immutable (`outbox_identity_frozen`): only lifecycle columns and
  `destination_binding_id` (late routing) change, so the event ID is stable across
  attempts and the payload always matches `payload_hash`.
- `unique (workspace_id, dedup_key)`.
- `event_version` is an integer major version; the payload's own `schema_version`
  string (e.g. `"1.0"`) lives inside `payload`.
- `ops.event_deliveries`: `sending` ⇒ a lease; `pending`/`retry_wait` ⇒ no lease token or
  expiry (a new claim mints a fresh token).
- Indexes: `outbox_due_idx (workspace_id, available_at, id)` WHERE pending/retry_wait, a
  lease-expiry index, and an attention index (uncertain, blocked, dead_letter).

## 7. Queue SQL contracts (tested in `tests/integration/db/test_queue.py`)

The persistence layer must use these shapes, always with bound parameters, as
`suv_backend` with the workspace GUC set.

**Claim.** Use the spec §13 statement verbatim: `... for update skip locked limit 1`, then
set `state='running'`, a fresh `lease_token`, `lease_owner`, `lease_expires_at = now() +
:lease`, `last_heartbeat_at` and `attempts = attempts + 1`. Two concurrent workers never
receive the same job.

**Heartbeat and completion.** Both update with `WHERE id = :id AND state = 'running' AND
lease_token = :token AND lease_owner = :worker AND lease_expires_at > clock_timestamp()`.
If 0 rows change, the worker lost the lease and must roll back. A late duplicate
completion cannot overwrite the result.

**Reaper.** For `state='running' AND lease_expires_at <= clock_timestamp()`:

- if attempts remain, move to `retry_wait`;
- otherwise move to `dead_letter` and set `completed_at`;
- always clear the lease fields and set `last_error_code`.

Exhausted waiting jobs are reconciled into `dead_letter` through `jobs_exhausted_idx`.

**Scheduler.** `INSERT ... (scheduled_slot = :slot) ON CONFLICT DO NOTHING`, then advance
`ops.source_schedules.next_due_at` in the same short transaction.

## 8. Decisions and deviations

- **FX rates are workspace-owned.** Payment rates are private, and uniform RLS and
  composite references outweigh duplicating public ECB rows.
- **`host_budgets` and `activation_gates` are per workspace**, for uniform tenant
  isolation. The system is single-owner in practice. A second workspace crawling the same
  host has its own bucket, which is documented as a known limitation.
- The search-profile key column is `profile_key`, not `key`, consistent with
  `review_cases.profile_key`. Fixture flags are uniformly `is_fixture`.
- Queue and outbox due indexes lead with `workspace_id`, because claims are always per
  workspace under RLS (spec §11 lists `(available_at, priority, id)`).
  `revisions_listing_idx` is not created because the unique index covers it.
- `ops.robots_revisions` and `ops.fetch_attempts` are insert-only by grant, without the
  history trigger, so that retention purges remain possible.
- **Independent review hardening** (all derived from the spec and the domain contracts):
  valuation rows obey exactly the `domain.valuation.Valuation` invariants (only "mark
  stale" after insert); `app.sources.detail_mode` mirrors `SourceConfig.detail_mode` so a
  `card_only` source can be enabled without detail paths, as `activation_problems()`
  allows; runs cited by card observations, fetch attempts and schedules belong to the
  same source; review successors belong to the same listing; outbox events are immutable
  and fixture lineage is enforced (spec §18); credential scopes never exceed the role;
  migrations verify the cluster-global `suv_backend` role is safe.
- `ops.outbox.event_version` is an integer. The domain draft (`domain.notifications`)
  carries the payload schema version string `"1.0"`; persistence stores the integer major
  version (1) and keeps the string inside `payload.schema_version`.
- Retroactive quarantine of existing revisions is recorded on `app.listings.quarantined`
  plus `ops.audit_events`. Revisions themselves are immutable, and `quarantined` on a
  revision or observation is set at insert time.

## 9. Applied environments

### Supabase project `Lokal69- Sub` (ref `olkcgrahvkvzgnnsqspr`, eu-west-1, PostgreSQL 17.11)

Migrations 0100–0900 were applied on 2026-10-06 through the Supabase connector
(`apply_migration`), in filename order. The stored SQL matches the files byte for byte (sha256
checked). Supabase recorded each one under its apply time, not the filename prefix:

| File | Recorded version |
|---|---|
| 20261006000100_extensions_roles | 20261006201953 |
| 20261006000200_core_tables | 20261006202042 |
| 20261006000300_queue_and_crawl_ops | 20261006202508 |
| 20261006000400_listings | 20261006202919 |
| 20261006000500_market_and_valuation | 20261006203049 |
| 20261006000600_reviews_and_notifications | 20261006203140 |
| 20261006000700_outbox_events_and_auth_ops | 20261006203308 |
| 20261006000800_security_rls_grants | 20261006203337 |
| 20261006000900_backend_role_membership | 20261006203427 |

Before using `supabase db push` against this project, mark the files as applied with
`supabase migration repair --status applied <filename-version>` (and revert the connector-recorded
versions), or keep applying new migrations through the same connector/`scripts/migrate.sh` path.

Verified after applying: 27 `app` + 16 `ops` tables; RLS enabled on 43/43; no grants to
`anon`/`authenticated`/`service_role`/`PUBLIC` on `app`/`ops`; `ops.backend_role_problems()` is `[]`;
`postgres` can `SET ROLE suv_backend`; `btree_gist` installed in `extensions`.

Advisor notes (2026-10-06):
- Security WARN ×2 concern `public.rls_auto_enable()` (SECURITY DEFINER, executable by
  `anon`/`authenticated`). It was not created by these migrations (it came with the project and
  backs an event trigger). Revoking `EXECUTE` from `anon`/`authenticated` is the owner's call.
- Performance INFO: 48 unindexed foreign keys and "unused index" on an empty database; add covering
  indexes only where real query plans need them (spec §11). Three "multiple permissive policies"
  warnings are the intended self-lookup policies for memberships, workspaces and API credentials.
