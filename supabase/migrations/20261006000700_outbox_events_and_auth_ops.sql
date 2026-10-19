-- =============================================================================
-- 20261006000700_outbox_events_and_auth_ops
-- Transactional outbox and delivery records (spec 13, 22), MCP Events
-- subscriptions and per-subscription deliveries (spec 22), frozen query
-- snapshots for stable pagination and idempotency records (spec 21), audit
-- events, activation gates (spec 32) and hashed API credentials (spec 20).
-- =============================================================================

-- Outbox states (spec 13): pending, sending (leased), retry_wait, delivered,
-- uncertain, blocked, dead_letter, cancelled. The domain transaction inserts the
-- row together with the domain change. External delivery is at-least-once or
-- uncertain; a row existing never means delivered.
create table ops.outbox (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  event_id uuid not null default gen_random_uuid(),
  event_type text not null,
  event_version integer not null default 1,
  aggregate_type text not null,
  aggregate_id uuid not null,
  aggregate_version bigint,
  destination_binding_id uuid,
  payload jsonb not null,
  payload_hash text not null,
  dedup_key text not null,
  state text not null default 'pending',
  attempts integer not null default 0,
  max_attempts integer not null default 10,
  available_at timestamptz not null default now(),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  last_heartbeat_at timestamptz,
  event_created_at timestamptz not null default now(),
  send_attempted_at timestamptz,
  provider_accepted_at timestamptz,
  owner_seen_at timestamptz,
  last_error_code text,
  blocker_code text,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint outbox_workspace_id_uk unique (workspace_id, id),
  constraint outbox_event_id_uk unique (event_id),
  constraint outbox_workspace_event_uk unique (workspace_id, event_id),
  constraint outbox_dedup_uk unique (workspace_id, dedup_key),
  constraint outbox_binding_fk foreign key (workspace_id, destination_binding_id)
    references app.destination_bindings (workspace_id, id),
  constraint outbox_event_type_ck check (event_type ~ '^[a-z][a-z0-9_.]{2,79}$'),
  constraint outbox_event_version_ck check (event_version > 0),
  constraint outbox_aggregate_type_ck check (aggregate_type ~ '^[a-z][a-z0-9_]{2,59}$'),
  constraint outbox_aggregate_version_ck check (aggregate_version is null or aggregate_version > 0),
  constraint outbox_payload_ck check (pg_catalog.jsonb_typeof(payload) = 'object'),
  -- One event per request within the 256 KiB delivery ceiling (spec 22).
  constraint outbox_payload_size_ck check (pg_catalog.octet_length(payload::text) <= 262144),
  constraint outbox_payload_hash_ck check (payload_hash ~ '^[0-9a-f]{64}$'),
  constraint outbox_dedup_key_ck check (pg_catalog.length(dedup_key) between 1 and 300),
  constraint outbox_state_ck check (state in (
    'pending', 'sending', 'retry_wait', 'delivered', 'uncertain', 'blocked', 'dead_letter', 'cancelled')),
  constraint outbox_max_attempts_ck check (max_attempts between 1 and 50),
  constraint outbox_attempts_ck check (attempts >= 0 and attempts <= max_attempts),
  constraint outbox_sending_lease_ck check (
    state <> 'sending'
    or (lease_owner is not null and lease_token is not null and lease_expires_at is not null)),
  constraint outbox_waiting_no_lease_ck check (
    state not in ('pending', 'retry_wait') or (lease_token is null and lease_expires_at is null)),
  constraint outbox_lease_owner_ck check (lease_owner is null or pg_catalog.length(lease_owner) between 1 and 200),
  -- Separate timestamps: created, send attempted, provider accepted, owner seen.
  constraint outbox_delivered_ck check (state <> 'delivered' or provider_accepted_at is not null),
  constraint outbox_accepted_ck check (provider_accepted_at is null or send_attempted_at is not null),
  constraint outbox_seen_ck check (owner_seen_at is null or provider_accepted_at is not null),
  constraint outbox_blocked_ck check (state <> 'blocked' or blocker_code is not null),
  constraint outbox_codes_ck check (
    (last_error_code is null or last_error_code ~ '^[A-Za-z0-9_.:-]{1,80}$')
    and (blocker_code is null or blocker_code ~ '^[A-Za-z0-9_.:-]{1,80}$')),
  -- Fixtures can never generate external notifications (spec 18): they may only
  -- exist as blocked or cancelled rows.
  constraint outbox_fixture_ck check (not is_fixture or state in ('blocked', 'cancelled'))
);
-- Pending/retry outbox rows (spec 11); workspace_id leads for per-workspace dispatch.
create index outbox_due_idx on ops.outbox (workspace_id, available_at, id) where state in ('pending', 'retry_wait');
create index outbox_lease_expiry_idx on ops.outbox (lease_expires_at) where state = 'sending';
create index outbox_attention_idx
  on ops.outbox (workspace_id, state, event_created_at) where state in ('uncertain', 'blocked', 'dead_letter');
create index outbox_aggregate_idx on ops.outbox (workspace_id, aggregate_type, aggregate_id);

-- One row per provider send attempt (append-only). attempt_id is the stable
-- per-attempt reference sent to the provider where supported.
create table ops.delivery_attempts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  outbox_id uuid not null,
  attempt_id uuid not null,
  attempt_number integer not null,
  provider text not null,
  sent_at timestamptz not null,
  completed_at timestamptz,
  external_receipt text,
  response_code integer,
  error_code text,
  error_detail text,
  uncertain boolean not null default false,
  created_at timestamptz not null default now(),
  constraint delivery_attempts_workspace_id_uk unique (workspace_id, id),
  constraint delivery_attempts_attempt_id_uk unique (attempt_id),
  constraint delivery_attempts_number_uk unique (workspace_id, outbox_id, attempt_number),
  constraint delivery_attempts_outbox_fk foreign key (workspace_id, outbox_id)
    references ops.outbox (workspace_id, id),
  constraint delivery_attempts_number_ck check (attempt_number >= 1),
  constraint delivery_attempts_provider_ck check (provider in ('slack', 'mcp_events')),
  constraint delivery_attempts_timing_ck check (completed_at is null or completed_at >= sent_at),
  constraint delivery_attempts_receipt_ck check (external_receipt is null or pg_catalog.length(external_receipt) <= 500),
  constraint delivery_attempts_status_ck check (response_code is null or response_code between 100 and 599),
  constraint delivery_attempts_error_ck check (
    (error_code is null or error_code ~ '^[A-Za-z0-9_.:-]{1,80}$')
    and (error_detail is null or pg_catalog.length(error_detail) <= 1000)),
  -- An uncertain outcome has no receipt; reconciliation records a new attempt row.
  constraint delivery_attempts_uncertain_ck check (not uncertain or external_receipt is null)
);

-- MCP Events subscriptions (spec 22). Identity = (principal, callback, event,
-- canonical filter); a refresh updates the existing row. The callback secret is
-- stored only encrypted (key held by the event-bridge process, never logged).
create table ops.event_subscriptions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  principal_id uuid not null,
  credential_id uuid,
  event_name text not null,
  canonical_filter jsonb not null default '{}'::jsonb,
  filter_hash text not null,
  callback_url text not null,
  encrypted_secret bytea not null,
  secret_version integer not null default 1,
  -- Rotation window: the previous secret (still only ciphertext) may sign
  -- deliveries until previous_secret_valid_until; then it is cleared.
  previous_encrypted_secret bytea,
  previous_secret_valid_until timestamptz,
  verification_state text not null default 'pending',
  verification_challenge_hash text,
  challenge_expires_at timestamptz,
  verified_at timestamptz,
  expires_at timestamptz not null,
  refresh_deadline timestamptz,
  revoked_at timestamptz,
  revoke_reason text,
  -- NULL until protocol replay is implemented and tested (spec 22 MVP rule).
  replay_position text,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint event_subscriptions_workspace_id_uk unique (workspace_id, id),
  constraint event_subscriptions_identity_uk unique (principal_id, callback_url, event_name, filter_hash),
  constraint event_subscriptions_event_name_ck check (event_name ~ '^[a-z][a-z0-9_.]{2,99}$'),
  constraint event_subscriptions_filter_ck check (pg_catalog.jsonb_typeof(canonical_filter) = 'object'),
  constraint event_subscriptions_filter_hash_ck check (filter_hash ~ '^[0-9a-f]{64}$'),
  -- HTTPS only, no embedded credentials (DNS/IP/redirect checks happen at send time).
  constraint event_subscriptions_callback_ck check (
    callback_url ~ '^https://[^/?#@[:space:]]+([/?#][^[:space:]]*)?$'
    and pg_catalog.length(callback_url) <= 2048),
  constraint event_subscriptions_secret_ck check (pg_catalog.octet_length(encrypted_secret) between 16 and 4096),
  constraint event_subscriptions_secret_version_ck check (secret_version > 0),
  constraint event_subscriptions_previous_secret_ck check (
    (previous_encrypted_secret is null) = (previous_secret_valid_until is null)
    and (previous_encrypted_secret is null
         or pg_catalog.octet_length(previous_encrypted_secret) between 16 and 4096)),
  constraint event_subscriptions_verification_ck check (verification_state in ('pending', 'verified', 'failed')),
  constraint event_subscriptions_verified_ck check (verification_state <> 'verified' or verified_at is not null),
  constraint event_subscriptions_challenge_hash_ck check (
    verification_challenge_hash is null or verification_challenge_hash ~ '^[0-9a-f]{64}$'),
  -- Finite lifetimes with refresh deadlines (spec 22).
  constraint event_subscriptions_lifetime_ck check (expires_at > created_at),
  constraint event_subscriptions_refresh_ck check (refresh_deadline is null or refresh_deadline <= expires_at),
  constraint event_subscriptions_revoke_ck check (revoked_at is null or revoke_reason is not null),
  constraint event_subscriptions_revoke_reason_ck check (revoke_reason is null or pg_catalog.length(revoke_reason) <= 500),
  constraint event_subscriptions_replay_ck check (replay_position is null or pg_catalog.length(replay_position) <= 200),
  constraint event_subscriptions_version_ck check (version > 0)
);
create index event_subscriptions_dispatch_idx
  on ops.event_subscriptions (workspace_id, event_name, expires_at)
  where revoked_at is null and verification_state = 'verified';

-- One delivery record per (subscription, event); the event ID is stable across
-- retries. 410/413 are terminal ('failed'). 2xx means receipt, not review.
create table ops.event_deliveries (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  subscription_id uuid not null,
  event_id uuid not null,
  state text not null default 'pending',
  attempts integer not null default 0,
  max_attempts integer not null default 10,
  next_attempt_at timestamptz not null default now(),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  replay_sequence bigint,
  last_response_code integer,
  safe_error text,
  accepted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint event_deliveries_workspace_id_uk unique (workspace_id, id),
  constraint event_deliveries_pair_uk unique (subscription_id, event_id),
  constraint event_deliveries_subscription_fk foreign key (workspace_id, subscription_id)
    references ops.event_subscriptions (workspace_id, id),
  constraint event_deliveries_event_fk foreign key (workspace_id, event_id)
    references ops.outbox (workspace_id, event_id),
  constraint event_deliveries_state_ck check (state in (
    'pending', 'sending', 'retry_wait', 'accepted', 'uncertain', 'failed', 'dead_letter', 'cancelled')),
  constraint event_deliveries_max_attempts_ck check (max_attempts between 1 and 50),
  constraint event_deliveries_attempts_ck check (attempts >= 0 and attempts <= max_attempts),
  constraint event_deliveries_sending_lease_ck check (
    state <> 'sending'
    or (lease_owner is not null and lease_token is not null and lease_expires_at is not null)),
  -- Waiting deliveries carry no lease; the next claim mints a fresh token (fencing).
  constraint event_deliveries_waiting_no_lease_ck check (
    state not in ('pending', 'retry_wait') or (lease_token is null and lease_expires_at is null)),
  constraint event_deliveries_accepted_ck check (state <> 'accepted' or accepted_at is not null),
  constraint event_deliveries_sequence_ck check (replay_sequence is null or replay_sequence > 0),
  constraint event_deliveries_status_ck check (last_response_code is null or last_response_code between 100 and 599),
  constraint event_deliveries_error_ck check (safe_error is null or pg_catalog.length(safe_error) <= 500)
);
create index event_deliveries_due_idx
  on ops.event_deliveries (workspace_id, next_attempt_at, id) where state in ('pending', 'retry_wait');
create index event_deliveries_lease_expiry_idx on ops.event_deliveries (lease_expires_at) where state = 'sending';

-- Frozen ordered result membership + display projections for stable queue
-- pagination, bound to principal/workspace/filter (spec 21). Short-lived.
create table ops.query_snapshots (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  principal_id uuid not null,
  query_name text not null,
  filter_hash text not null,
  result_ids uuid[] not null,
  projections jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  constraint query_snapshots_workspace_id_uk unique (workspace_id, id),
  constraint query_snapshots_query_name_ck check (query_name ~ '^[a-z][a-z0-9_]{2,79}$'),
  constraint query_snapshots_filter_hash_ck check (filter_hash ~ '^[0-9a-f]{64}$'),
  constraint query_snapshots_result_ids_ck check (app.uuid_array_ok(result_ids, 10000)),
  constraint query_snapshots_projections_ck check (pg_catalog.jsonb_typeof(projections) = 'array'),
  constraint query_snapshots_expiry_ck check (
    expires_at > created_at and expires_at <= created_at + interval '1 day')
);
create index query_snapshots_expiry_idx on ops.query_snapshots (expires_at);

-- Idempotency scoped by authenticated principal and operation (spec 21).
create table ops.idempotency_records (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  principal_id uuid not null,
  operation text not null,
  idempotency_key text not null,
  request_hash text not null,
  state text not null default 'in_progress',
  result jsonb,
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  expires_at timestamptz not null,
  constraint idempotency_records_workspace_id_uk unique (workspace_id, id),
  constraint idempotency_records_key_uk unique (principal_id, operation, idempotency_key),
  constraint idempotency_records_operation_ck check (operation ~ '^[a-z][a-z0-9_.]{2,79}$'),
  constraint idempotency_records_key_ck check (pg_catalog.length(idempotency_key) between 8 and 128),
  constraint idempotency_records_request_hash_ck check (request_hash ~ '^[0-9a-f]{64}$'),
  constraint idempotency_records_state_ck check (state in ('in_progress', 'completed', 'failed')),
  constraint idempotency_records_completed_ck check (state <> 'completed' or result is not null),
  constraint idempotency_records_failed_ck check (state <> 'failed' or error_code is not null),
  constraint idempotency_records_finished_ck check ((state = 'in_progress') = (completed_at is null)),
  constraint idempotency_records_error_code_ck check (error_code is null or error_code ~ '^[A-Z_]{3,40}$'),
  constraint idempotency_records_expiry_ck check (expires_at > created_at)
);
create index idempotency_records_expiry_idx on ops.idempotency_records (expires_at);

-- Immutable audit trail (redacted metadata only; never secrets).
create table ops.audit_events (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  actor_principal_id uuid not null,
  actor_kind text not null,
  actor_role text,
  action text not null,
  target_type text not null,
  target_id uuid,
  prior_version bigint,
  new_version bigint,
  reason text,
  request_id text,
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint audit_events_workspace_id_uk unique (workspace_id, id),
  constraint audit_events_actor_kind_ck check (actor_kind in ('user', 'mcp_client', 'system')),
  constraint audit_events_actor_role_ck check (actor_role is null or actor_role in ('owner', 'reviewer', 'viewer')),
  constraint audit_events_action_ck check (action ~ '^[a-z][a-z0-9_.:]{2,99}$'),
  constraint audit_events_target_type_ck check (target_type ~ '^[a-z][a-z0-9_.]{2,59}$'),
  constraint audit_events_versions_ck check (
    (prior_version is null or prior_version >= 0) and (new_version is null or new_version >= 0)),
  constraint audit_events_reason_ck check (reason is null or pg_catalog.length(reason) <= 2000),
  constraint audit_events_request_id_ck check (request_id is null or pg_catalog.length(request_id) <= 200),
  constraint audit_events_metadata_ck check (pg_catalog.jsonb_typeof(metadata) = 'object')
);
create index audit_events_recent_idx on ops.audit_events (workspace_id, occurred_at desc, id);
create index audit_events_target_idx on ops.audit_events (workspace_id, target_type, target_id, occurred_at desc);

-- Honest activation states (spec 32). live_verified/active require evidence.
create table ops.activation_gates (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  capability text not null,
  dependency text not null,
  required_evidence text not null,
  status text not null default 'not_requested',
  owner text,
  next_action text,
  evidence jsonb not null default '{}'::jsonb,
  checked_at timestamptz,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint activation_gates_workspace_id_uk unique (workspace_id, id),
  constraint activation_gates_capability_uk unique (workspace_id, capability),
  constraint activation_gates_capability_ck check (capability ~ '^[a-z][a-z0-9_.]{2,79}$'),
  constraint activation_gates_dependency_ck check (pg_catalog.length(dependency) between 1 and 500),
  constraint activation_gates_required_evidence_ck check (pg_catalog.length(required_evidence) between 1 and 2000),
  constraint activation_gates_status_ck check (status in (
    'not_requested', 'implemented', 'fixture_verified', 'integration_verified', 'live_verified', 'active',
    'blocked')),
  constraint activation_gates_owner_ck check (owner is null or pg_catalog.length(owner) <= 200),
  constraint activation_gates_next_action_ck check (next_action is null or pg_catalog.length(next_action) <= 2000),
  constraint activation_gates_evidence_ck check (pg_catalog.jsonb_typeof(evidence) = 'object'),
  constraint activation_gates_live_evidence_ck check (
    status not in ('live_verified', 'active') or (checked_at is not null and evidence <> '{}'::jsonb)),
  constraint activation_gates_row_version_ck check (row_version > 0)
);

-- Static-bearer / local-dev MCP credentials. Only a SHA-256 hash of the token
-- is stored, never the token. Scopes narrow (never widen) the member role.
create table ops.api_credentials (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  principal_id uuid not null,
  principal_kind text not null,
  role text not null,
  credential_kind text not null,
  token_hash text not null,
  token_prefix text,
  scopes text[] not null,
  label text not null,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  revoked_by uuid,
  revoke_reason text,
  created_by uuid,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  constraint api_credentials_workspace_id_uk unique (workspace_id, id),
  constraint api_credentials_token_hash_uk unique (token_hash),
  constraint api_credentials_principal_kind_ck check (principal_kind in ('user', 'mcp_client')),
  constraint api_credentials_role_ck check (role in ('owner', 'reviewer', 'viewer')),
  constraint api_credentials_kind_ck check (credential_kind in ('static_bearer', 'dev_local')),
  constraint api_credentials_token_hash_ck check (token_hash ~ '^[0-9a-f]{64}$'),
  constraint api_credentials_prefix_ck check (token_prefix is null or token_prefix ~ '^[A-Za-z0-9_]{1,16}$'),
  constraint api_credentials_scopes_ck check (
    pg_catalog.cardinality(scopes) between 1 and 8
    and app.text_array_ok(scopes, 8, 40)
    and scopes <@ array['deals:read', 'reviews:read', 'reviews:write', 'events:subscribe',
                        'rechecks:request', 'notes:write', 'sources:pause', 'config:admin']::text[]),
  -- Scopes narrow, never widen, the member role (mirrors domain.actor.ROLE_SCOPES), so an
  -- over-scoped credential can never be minted (spec 20: config:admin is owner-only).
  constraint api_credentials_role_scopes_ck check (
    role = 'owner'
    or (role = 'reviewer'
        and scopes <@ array['deals:read', 'reviews:read', 'reviews:write', 'events:subscribe',
                            'rechecks:request', 'notes:write']::text[])
    or (role = 'viewer' and scopes <@ array['deals:read', 'reviews:read']::text[])),
  constraint api_credentials_label_ck check (pg_catalog.length(label) between 1 and 120),
  constraint api_credentials_expiry_ck check (expires_at > created_at),
  constraint api_credentials_revoke_ck check (
    revoked_at is null or (revoked_at >= created_at and revoke_reason is not null)),
  constraint api_credentials_revoke_reason_ck check (revoke_reason is null or pg_catalog.length(revoke_reason) <= 500)
);

-- The credential a static-bearer subscription was created with (NULL for OAuth
-- principals, whose tokens are not stored here).
alter table ops.event_subscriptions
  add constraint event_subscriptions_credential_fk foreign key (workspace_id, credential_id)
  references ops.api_credentials (workspace_id, id);

-- An outbox event is immutable once written: the event ID is stable across
-- attempts (spec 22), the payload matches its payload_hash, and is_fixture can
-- never be flipped to turn a blocked fixture row into a deliverable event.
-- Only delivery lifecycle columns (and late routing to a destination) change.
create trigger outbox_identity_frozen before update on ops.outbox
  for each row execute function app.guard_frozen_columns(
    'state', 'attempts', 'max_attempts', 'available_at', 'lease_owner', 'lease_token',
    'lease_expires_at', 'last_heartbeat_at', 'send_attempted_at', 'provider_accepted_at',
    'owner_seen_at', 'last_error_code', 'blocker_code', 'destination_binding_id', 'updated_at',
    'completed_at');

-- A fixture review case never produces a deliverable (non-fixture) event
-- (spec 18). Deferred to commit so the check holds whichever of the case and
-- the outbox row is written first in the domain transaction.
create or replace function ops.outbox_check_fixture_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if not new.is_fixture and new.aggregate_type = 'review_case' and exists (
       select 1
         from app.review_cases c
        where c.workspace_id = new.workspace_id
          and c.id = new.aggregate_id
          and c.is_fixture) then
    raise exception using
      errcode = 'SV003',
      message = 'a fixture review case cannot produce a non-fixture outbox event';
  end if;
  return null;
end
$$;
create constraint trigger outbox_fixture_lineage after insert on ops.outbox
  deferrable initially deferred
  for each row execute function ops.outbox_check_fixture_lineage();

create trigger outbox_touch before update on ops.outbox
  for each row execute function app.touch_updated_at();
create trigger event_subscriptions_touch before update on ops.event_subscriptions
  for each row execute function app.touch_updated_at();
create trigger event_subscriptions_version_guard before update on ops.event_subscriptions
  for each row execute function app.guard_version('version');
create trigger event_deliveries_touch before update on ops.event_deliveries
  for each row execute function app.touch_updated_at();
create trigger idempotency_records_touch before update on ops.idempotency_records
  for each row execute function app.touch_updated_at();
-- An idempotency record's identity and request hash never change.
create trigger idempotency_records_frozen before update on ops.idempotency_records
  for each row execute function app.guard_frozen_columns(
    'state', 'result', 'error_code', 'completed_at', 'expires_at', 'updated_at');
create trigger activation_gates_touch before update on ops.activation_gates
  for each row execute function app.touch_updated_at();
create trigger activation_gates_version_guard before update on ops.activation_gates
  for each row execute function app.guard_version('row_version');
-- Credentials: only usage and revocation columns may change; rotation = new row.
create trigger api_credentials_frozen before update on ops.api_credentials
  for each row execute function app.guard_frozen_columns(
    'last_used_at', 'revoked_at', 'revoked_by', 'revoke_reason');

call ops.apply_security_baseline();
