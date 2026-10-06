-- =============================================================================
-- 20261006000300_queue_and_crawl_ops
-- Durable job queue (spec section 13) and crawl operations: schedules and
-- watermarks (section 9), persistent per-host budgets/circuit breakers
-- (section 9 adaptive backoff), robots revisions (section 5), crawl runs,
-- source snapshots and redacted fetch attempts (sections 8 and 11).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- ops.jobs
-- State machine (spec 13):
--   queued -> running -> succeeded | retry_wait | blocked | dead_letter
--   retry_wait -> running;  queued/retry_wait -> cancelled
--   running with expired lease -> retry_wait (attempts remain) or dead_letter
-- The claim, heartbeat and completion SQL live in the persistence layer; the
-- table enforces the invariants those statements rely on.
-- -----------------------------------------------------------------------------
create table ops.jobs (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  job_type text not null,
  dedup_key text not null,
  payload_version integer not null default 1,
  payload jsonb not null default '{}'::jsonb,
  priority integer not null default 0,
  available_at timestamptz not null default now(),
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  state text not null default 'queued',
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  last_heartbeat_at timestamptz,
  last_error_code text,
  last_error_detail text,
  blocker_code text,
  blocker_detail text,
  source_id uuid,
  profile_id uuid,
  partition_key text,
  -- Scheduler slot (database time, truncated to the 15-minute grid by the scheduler).
  scheduled_slot timestamptz,
  -- Detail/recheck binding: source identity (listing incarnation) + observation generation.
  listing_id uuid,
  generation bigint,
  result_reference jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint jobs_workspace_id_uk unique (workspace_id, id),
  constraint jobs_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint jobs_profile_fk foreign key (workspace_id, profile_id)
    references app.search_profiles (workspace_id, id),
  constraint jobs_type_ck check (job_type in (
    'discovery', 'detail', 'recheck', 'valuation', 'comparables', 'stale_sweep', 'reprocess')),
  constraint jobs_state_ck check (state in (
    'queued', 'running', 'succeeded', 'retry_wait', 'blocked', 'dead_letter', 'cancelled')),
  constraint jobs_dedup_key_ck check (pg_catalog.length(dedup_key) between 1 and 300),
  constraint jobs_payload_version_ck check (payload_version > 0),
  constraint jobs_payload_ck check (pg_catalog.jsonb_typeof(payload) = 'object'),
  constraint jobs_priority_ck check (priority between -1000 and 1000),
  constraint jobs_max_attempts_ck check (max_attempts between 1 and 50),
  constraint jobs_attempts_ck check (attempts >= 0 and attempts <= max_attempts),
  constraint jobs_running_lease_ck check (
    state <> 'running'
    or (lease_owner is not null and lease_token is not null and lease_expires_at is not null)),
  -- Waiting jobs carry no lease: the reaper clears it and a new claim mints a fresh token.
  constraint jobs_waiting_no_lease_ck check (
    state not in ('queued', 'retry_wait') or (lease_token is null and lease_expires_at is null)),
  constraint jobs_lease_owner_ck check (lease_owner is null or pg_catalog.length(lease_owner) between 1 and 200),
  constraint jobs_blocked_ck check (state <> 'blocked' or blocker_code is not null),
  constraint jobs_blocker_code_ck check (blocker_code is null or blocker_code ~ '^[A-Za-z0-9_.:-]{1,80}$'),
  constraint jobs_blocker_detail_ck check (blocker_detail is null or pg_catalog.length(blocker_detail) <= 2000),
  constraint jobs_error_code_ck check (last_error_code is null or last_error_code ~ '^[A-Za-z0-9_.:-]{1,80}$'),
  constraint jobs_error_detail_ck check (last_error_detail is null or pg_catalog.length(last_error_detail) <= 2000),
  constraint jobs_completed_at_ck check (
    (state in ('succeeded', 'dead_letter', 'cancelled') and completed_at is not null)
    or (state in ('queued', 'running', 'retry_wait') and completed_at is null)
    or state = 'blocked'),
  constraint jobs_partition_ck check (partition_key is null or partition_key ~ '^[A-Za-z0-9_:.-]{1,80}$'),
  constraint jobs_slot_ck check (
    scheduled_slot is null
    or (source_id is not null and profile_id is not null and partition_key is not null)),
  constraint jobs_generation_ck check (generation is null or generation >= 1),
  constraint jobs_detail_binding_ck check (
    job_type not in ('detail', 'recheck') or (listing_id is not null and generation is not null)),
  constraint jobs_result_ck check (result_reference is null or pg_catalog.jsonb_typeof(result_reference) = 'object')
);

-- One live job per business dedup key (terminal jobs do not block re-enqueue).
create unique index jobs_dedup_open_uidx
  on ops.jobs (workspace_id, dedup_key)
  where state in ('queued', 'running', 'retry_wait', 'blocked');
-- One discovery job per scheduler slot, forever (spec section 9): duplicate
-- schedulers racing for the same slot cannot both insert.
create unique index jobs_scheduler_slot_uidx
  on ops.jobs (workspace_id, source_id, profile_id, partition_key, scheduled_slot)
  where scheduled_slot is not null;
-- Due jobs (spec section 11). workspace_id leads because claims are per workspace.
create index jobs_due_idx
  on ops.jobs (workspace_id, available_at, priority, id)
  where state in ('queued', 'retry_wait');
-- Expired-lease reaper (spec section 11).
create index jobs_lease_expiry_idx on ops.jobs (lease_expires_at) where state = 'running';
-- Exhausted waiting jobs that must be reconciled into dead_letter.
create index jobs_exhausted_idx
  on ops.jobs (workspace_id, id)
  where state in ('queued', 'retry_wait') and attempts >= max_attempts;
create index jobs_listing_idx on ops.jobs (workspace_id, listing_id, generation) where listing_id is not null;
create index jobs_attention_idx on ops.jobs (workspace_id, state, updated_at) where state in ('blocked', 'dead_letter');

-- -----------------------------------------------------------------------------
-- ops.crawl_runs: one discovery traversal of a (source, profile, partition).
-- outcome 'running' until finished; then a Completeness value or 'cancelled'.
-- A budget-limited run is incomplete and never advances the complete watermark.
-- -----------------------------------------------------------------------------
create table ops.crawl_runs (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  profile_id uuid,
  partition_key text not null default 'default',
  job_id uuid,
  build_id text,
  adapter_version text not null,
  parser_version text,
  crawler_version text,
  coverage_mode text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  outcome text not null default 'running',
  pages_fetched integer not null default 0,
  cards_seen integer not null default 0,
  new_listings integer not null default 0,
  changed_listings integer not null default 0,
  detail_jobs_enqueued integer not null default 0,
  detail_jobs_deduplicated integer not null default 0,
  result_count_reported integer,
  page_depth integer,
  watermark_from timestamptz,
  watermark_to timestamptz,
  gap_reasons jsonb not null default '[]'::jsonb,
  access_state text,
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint crawl_runs_workspace_id_uk unique (workspace_id, id),
  constraint crawl_runs_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint crawl_runs_profile_fk foreign key (workspace_id, profile_id)
    references app.search_profiles (workspace_id, id),
  constraint crawl_runs_job_fk foreign key (workspace_id, job_id)
    references ops.jobs (workspace_id, id),
  constraint crawl_runs_partition_ck check (partition_key ~ '^[A-Za-z0-9_:.-]{1,80}$'),
  constraint crawl_runs_versions_ck check (
    pg_catalog.length(adapter_version) between 1 and 40
    and (parser_version is null or pg_catalog.length(parser_version) <= 120)
    and (crawler_version is null or pg_catalog.length(crawler_version) <= 80)
    and (build_id is null or pg_catalog.length(build_id) <= 120)),
  constraint crawl_runs_coverage_mode_ck check (coverage_mode in ('watermark', 'rolling_pages')),
  constraint crawl_runs_outcome_ck check (outcome in (
    'running', 'complete', 'budget_limited', 'partial', 'failed', 'blocked', 'cancelled')),
  constraint crawl_runs_finished_ck check ((outcome = 'running') = (finished_at is null)),
  constraint crawl_runs_timing_ck check (finished_at is null or finished_at >= started_at),
  constraint crawl_runs_counts_ck check (
    pages_fetched >= 0 and cards_seen >= 0 and new_listings >= 0 and changed_listings >= 0
    and detail_jobs_enqueued >= 0 and detail_jobs_deduplicated >= 0
    and (result_count_reported is null or result_count_reported >= 0)
    and (page_depth is null or page_depth >= 1)),
  -- Rolling-page coverage never fabricates a timestamp watermark (spec section 9).
  constraint crawl_runs_watermark_mode_ck check (
    coverage_mode = 'watermark' or (watermark_from is null and watermark_to is null)),
  constraint crawl_runs_watermark_order_ck check (
    watermark_from is null or watermark_to is null or watermark_to >= watermark_from),
  constraint crawl_runs_gap_reasons_ck check (pg_catalog.jsonb_typeof(gap_reasons) = 'array'),
  constraint crawl_runs_access_state_ck check (access_state is null or access_state in (
    'ok', 'access_blocked', 'rate_limited', 'not_found', 'removed', 'transient_error',
    'unexpected_content', 'policy_denied')),
  constraint crawl_runs_error_code_ck check (error_code is null or error_code ~ '^[A-Za-z0-9_.:-]{1,80}$')
);
create index crawl_runs_source_idx on ops.crawl_runs (workspace_id, source_id, started_at desc);

-- -----------------------------------------------------------------------------
-- ops.source_schedules: one row per (workspace, source, profile, partition).
-- The scheduler locks the row, inserts the slot job, then advances next_due_at
-- in the same short transaction; no network I/O inside (spec section 9).
-- -----------------------------------------------------------------------------
create table ops.source_schedules (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  profile_id uuid not null,
  partition_key text not null default 'default',
  interval_seconds integer not null default 900,
  next_due_at timestamptz not null,
  last_slot timestamptz,
  cursor jsonb,
  run_id uuid,
  coverage_mode text not null,
  complete_watermark timestamptz,
  page_depth integer,
  last_complete_traversal_at timestamptz,
  incomplete_since timestamptz,
  gap_reasons jsonb not null default '[]'::jsonb,
  backoff_until timestamptz,
  consecutive_failures integer not null default 0,
  paused boolean not null default false,
  pause_reason text,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint source_schedules_workspace_id_uk unique (workspace_id, id),
  constraint source_schedules_partition_uk unique (workspace_id, source_id, profile_id, partition_key),
  constraint source_schedules_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint source_schedules_profile_fk foreign key (workspace_id, profile_id)
    references app.search_profiles (workspace_id, id),
  constraint source_schedules_run_fk foreign key (workspace_id, run_id)
    references ops.crawl_runs (workspace_id, id),
  constraint source_schedules_partition_ck check (partition_key ~ '^[A-Za-z0-9_:.-]{1,80}$'),
  constraint source_schedules_interval_ck check (interval_seconds between 60 and 86400),
  constraint source_schedules_cursor_ck check (cursor is null or pg_catalog.jsonb_typeof(cursor) = 'object'),
  constraint source_schedules_coverage_mode_ck check (coverage_mode in ('watermark', 'rolling_pages')),
  constraint source_schedules_watermark_mode_ck check (coverage_mode = 'watermark' or complete_watermark is null),
  constraint source_schedules_page_depth_ck check (page_depth is null or page_depth between 1 and 1000),
  constraint source_schedules_gap_reasons_ck check (pg_catalog.jsonb_typeof(gap_reasons) = 'array'),
  constraint source_schedules_failures_ck check (consecutive_failures >= 0),
  constraint source_schedules_pause_ck check (not paused or pause_reason is not null),
  constraint source_schedules_row_version_ck check (row_version > 0)
);
create index source_schedules_due_idx on ops.source_schedules (workspace_id, next_due_at) where not paused;

-- -----------------------------------------------------------------------------
-- ops.host_budgets: persistent token bucket, daily budget counters and circuit
-- breaker per host. Engineering defaults, never provider-approved quotas.
-- -----------------------------------------------------------------------------
create table ops.host_budgets (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  host text not null,
  capacity numeric(12, 4) not null default 1,
  refill_per_second numeric(14, 8) not null,
  tokens numeric(12, 4) not null,
  refilled_at timestamptz not null,
  next_request_not_before timestamptz,
  circuit_state text not null default 'closed',
  open_until timestamptz,
  consecutive_failures integer not null default 0,
  budget_day date not null,
  requests_today integer not null default 0,
  bytes_today bigint not null default 0,
  daily_request_budget integer,
  daily_byte_budget bigint,
  last_retry_after_seconds integer,
  retry_after_until timestamptz,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint host_budgets_workspace_id_uk unique (workspace_id, id),
  constraint host_budgets_host_uk unique (workspace_id, host),
  constraint host_budgets_host_ck check (
    host = pg_catalog.lower(host) and host ~ '^[a-z0-9.-]{1,253}$' and host !~ '^[.-]'),
  constraint host_budgets_bucket_ck check (
    capacity > 0 and refill_per_second > 0 and tokens >= 0 and tokens <= capacity),
  constraint host_budgets_circuit_ck check (circuit_state in ('closed', 'open', 'half_open')),
  constraint host_budgets_open_until_ck check (circuit_state <> 'open' or open_until is not null),
  constraint host_budgets_counters_ck check (
    consecutive_failures >= 0 and requests_today >= 0 and bytes_today >= 0
    and (daily_request_budget is null or daily_request_budget >= 0)
    and (daily_byte_budget is null or daily_byte_budget >= 0)
    and (last_retry_after_seconds is null or last_retry_after_seconds >= 0)),
  constraint host_budgets_row_version_ck check (row_version > 0)
);

-- -----------------------------------------------------------------------------
-- ops.robots_revisions: every fetched robots.txt (bounded body + hash).
-- Absence of a prohibition is not a legal licence (spec section 5).
-- -----------------------------------------------------------------------------
create table ops.robots_revisions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  host text not null,
  fetched_at timestamptz not null,
  http_status integer,
  content_hash text,
  body text,
  body_truncated boolean not null default false,
  parse_ok boolean not null,
  user_agent text,
  created_at timestamptz not null default now(),
  constraint robots_revisions_workspace_id_uk unique (workspace_id, id),
  constraint robots_revisions_host_ck check (
    host = pg_catalog.lower(host) and host ~ '^[a-z0-9.-]{1,253}$' and host !~ '^[.-]'),
  constraint robots_revisions_status_ck check (http_status is null or http_status between 100 and 599),
  constraint robots_revisions_hash_ck check (content_hash is null or content_hash ~ '^[0-9a-f]{64}$'),
  constraint robots_revisions_body_ck check (body is null or pg_catalog.octet_length(body) <= 524288),
  constraint robots_revisions_ua_ck check (user_agent is null or pg_catalog.length(user_agent) <= 300)
);
create index robots_revisions_host_idx on ops.robots_revisions (workspace_id, host, fetched_at desc);

-- -----------------------------------------------------------------------------
-- ops.source_snapshots: metadata for raw source responses. The object lives in
-- private storage only when retention is permitted; otherwise only the hash.
-- No raw URLs (they may embed secrets): url_hash only.
-- -----------------------------------------------------------------------------
create table ops.source_snapshots (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  url_hash text not null,
  content_hash text not null,
  mime_type text,
  bytes bigint not null,
  fetched_at timestamptz not null,
  storage_backend text not null,
  object_key text,
  retention_policy text not null,
  retain_until timestamptz,
  redaction_status text not null default 'not_required',
  redacted_at timestamptz,
  purged_at timestamptz,
  created_at timestamptz not null default now(),
  constraint source_snapshots_workspace_id_uk unique (workspace_id, id),
  constraint source_snapshots_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint source_snapshots_url_hash_ck check (url_hash ~ '^[0-9a-f]{64}$'),
  constraint source_snapshots_content_hash_ck check (content_hash ~ '^[0-9a-f]{64}$'),
  constraint source_snapshots_mime_ck check (mime_type is null or pg_catalog.length(mime_type) <= 200),
  constraint source_snapshots_bytes_ck check (bytes >= 0),
  constraint source_snapshots_backend_ck check (storage_backend in ('disabled', 'local', 'supabase')),
  constraint source_snapshots_retention_ck check (retention_policy in ('hash_only', 'retain_until')),
  constraint source_snapshots_retention_shape_ck check (
    (retention_policy = 'hash_only' and storage_backend = 'disabled' and object_key is null and retain_until is null)
    or (retention_policy = 'retain_until' and storage_backend in ('local', 'supabase')
        and object_key is not null and retain_until is not null)),
  -- An object key is a private storage path, never a URL and never a traversal.
  constraint source_snapshots_object_key_ck check (
    object_key is null or (
      pg_catalog.length(object_key) between 1 and 1024
      and object_key !~ '^[A-Za-z][A-Za-z0-9+.-]*:'
      and object_key !~ '(^|/)\.\.(/|$)'
      and object_key !~ '^/')),
  constraint source_snapshots_redaction_ck check (
    redaction_status in ('not_required', 'pending', 'redacted', 'failed')),
  constraint source_snapshots_redacted_at_ck check (redaction_status <> 'redacted' or redacted_at is not null)
);
create index source_snapshots_retention_idx
  on ops.source_snapshots (retain_until) where retain_until is not null and purged_at is null;

-- -----------------------------------------------------------------------------
-- ops.fetch_attempts: redacted record of every fetch (adapters.base.FetchOutcome).
-- -----------------------------------------------------------------------------
create table ops.fetch_attempts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  job_id uuid,
  crawl_run_id uuid,
  purpose text not null,
  url_hash text not null,
  final_url_hash text,
  host text not null,
  http_status integer,
  success boolean not null,
  access_state text not null,
  error_code text,
  elapsed_ms integer,
  extraction_ms integer,
  bytes bigint not null default 0,
  redirect_count integer not null default 0,
  retry_after_seconds integer,
  response_headers jsonb not null default '{}'::jsonb,
  crawler_version text,
  snapshot_id uuid,
  fetched_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint fetch_attempts_workspace_id_uk unique (workspace_id, id),
  constraint fetch_attempts_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint fetch_attempts_job_fk foreign key (workspace_id, job_id)
    references ops.jobs (workspace_id, id),
  constraint fetch_attempts_run_fk foreign key (workspace_id, crawl_run_id)
    references ops.crawl_runs (workspace_id, id),
  constraint fetch_attempts_snapshot_fk foreign key (workspace_id, snapshot_id)
    references ops.source_snapshots (workspace_id, id),
  constraint fetch_attempts_purpose_ck check (purpose in ('search', 'detail', 'robots', 'diagnostic')),
  constraint fetch_attempts_url_hash_ck check (
    url_hash ~ '^[0-9a-f]{64}$' and (final_url_hash is null or final_url_hash ~ '^[0-9a-f]{64}$')),
  constraint fetch_attempts_host_ck check (
    host = pg_catalog.lower(host) and host ~ '^[a-z0-9.-]{1,253}$' and host !~ '^[.-]'),
  constraint fetch_attempts_status_ck check (http_status is null or http_status between 100 and 599),
  constraint fetch_attempts_access_state_ck check (access_state in (
    'ok', 'access_blocked', 'rate_limited', 'not_found', 'removed', 'transient_error',
    'unexpected_content', 'policy_denied')),
  constraint fetch_attempts_success_ck check (not success or access_state = 'ok'),
  constraint fetch_attempts_error_code_ck check (error_code is null or error_code ~ '^[A-Za-z0-9_.:-]{1,80}$'),
  constraint fetch_attempts_numbers_ck check (
    (elapsed_ms is null or elapsed_ms >= 0) and (extraction_ms is null or extraction_ms >= 0)
    and bytes >= 0 and redirect_count >= 0
    and (retry_after_seconds is null or retry_after_seconds >= 0)),
  constraint fetch_attempts_headers_ck check (pg_catalog.jsonb_typeof(response_headers) = 'object'),
  constraint fetch_attempts_crawler_version_ck check (crawler_version is null or pg_catalog.length(crawler_version) <= 80)
);
create index fetch_attempts_source_idx on ops.fetch_attempts (workspace_id, source_id, fetched_at desc);
create index fetch_attempts_job_idx on ops.fetch_attempts (workspace_id, job_id) where job_id is not null;

create trigger jobs_touch before update on ops.jobs
  for each row execute function app.touch_updated_at();
create trigger crawl_runs_touch before update on ops.crawl_runs
  for each row execute function app.touch_updated_at();
create trigger source_schedules_touch before update on ops.source_schedules
  for each row execute function app.touch_updated_at();
create trigger source_schedules_version_guard before update on ops.source_schedules
  for each row execute function app.guard_version('row_version');
create trigger host_budgets_touch before update on ops.host_budgets
  for each row execute function app.touch_updated_at();
create trigger host_budgets_version_guard before update on ops.host_budgets
  for each row execute function app.guard_version('row_version');
-- Snapshots: only redaction/purge lifecycle columns may change.
create trigger source_snapshots_frozen before update on ops.source_snapshots
  for each row execute function app.guard_frozen_columns('redaction_status', 'redacted_at', 'purged_at');

call ops.apply_security_baseline();
