-- =============================================================================
-- 20261006000600_reviews_and_notifications
-- Review cases and immutable decisions (spec 14 and 21), watchlists, private
-- owner/assistant notes, approved destination bindings and notification
-- preferences (spec 22).
-- =============================================================================

-- Review case per (listing, profile). row_version is the optimistic-concurrency
-- case version that clients pass as expected_version; every accepted change
-- (claim, release, submit, new material information) increments it
-- (domain.reviews). Claims store only a SHA-256 of the opaque claim token.
create table app.review_cases (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  revision_id uuid not null,
  valuation_id uuid,
  profile_key text not null,
  queue_label text not null,
  state text not null default 'pending',
  readiness text not null,
  priority integer not null default 0,
  ranking jsonb not null default '{}'::jsonb,
  ranking_version text,
  row_version bigint not null default 1,
  claim_holder uuid,
  claim_token_hash text,
  claimed_at timestamptz,
  claim_expires_at timestamptz,
  latest_decision_id uuid,
  reason text,
  superseded_by_id uuid,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint review_cases_workspace_id_uk unique (workspace_id, id),
  constraint review_cases_listing_id_uk unique (workspace_id, listing_id, id),
  constraint review_cases_revision_fk foreign key (workspace_id, listing_id, revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint review_cases_valuation_fk foreign key (workspace_id, listing_id, valuation_id)
    references app.valuations (workspace_id, listing_id, id),
  constraint review_cases_profile_fk foreign key (workspace_id, profile_key)
    references app.search_profiles (workspace_id, profile_key),
  -- A successor case belongs to the same listing (a relisted vehicle is a new
  -- listing incarnation and never inherits review history, spec 10).
  constraint review_cases_superseded_by_fk foreign key (workspace_id, listing_id, superseded_by_id)
    references app.review_cases (workspace_id, listing_id, id),
  constraint review_cases_state_ck check (state in (
    'pending', 'claimed', 'needs_information', 'watch', 'shortlisted', 'rejected', 'superseded')),
  constraint review_cases_queue_label_ck check (pg_catalog.length(queue_label) between 3 and 120),
  constraint review_cases_readiness_ck check (readiness ~ '^[a-z0-9_]{1,80}$'),
  constraint review_cases_priority_ck check (priority between -100000 and 100000),
  constraint review_cases_ranking_ck check (pg_catalog.jsonb_typeof(ranking) = 'object'),
  constraint review_cases_ranking_version_ck check (ranking_version is null or pg_catalog.length(ranking_version) <= 80),
  constraint review_cases_row_version_ck check (row_version > 0),
  constraint review_cases_claim_hash_ck check (claim_token_hash is null or claim_token_hash ~ '^[0-9a-f]{64}$'),
  -- A claim requires holder, token hash and expiry (spec 11).
  constraint review_cases_claimed_ck check (
    state <> 'claimed'
    or (claim_holder is not null and claim_token_hash is not null and claim_expires_at is not null)),
  -- Non-claimed states carry no claim data (release/submit clear it).
  constraint review_cases_unclaimed_ck check (
    state = 'claimed'
    or (claim_holder is null and claim_token_hash is null and claim_expires_at is null and claimed_at is null)),
  constraint review_cases_claim_window_ck check (
    claimed_at is null or claim_expires_at is null or claim_expires_at > claimed_at),
  -- A decided case references its decision (spec 11).
  constraint review_cases_decided_ck check (
    state not in ('needs_information', 'watch', 'shortlisted', 'rejected') or latest_decision_id is not null),
  constraint review_cases_superseded_ck check (superseded_by_id is null or state = 'superseded'),
  constraint review_cases_reason_ck check (reason is null or pg_catalog.length(reason) <= 2000)
);
-- One non-superseded case per (listing, profile): a new qualifying revision
-- updates the existing case (new version) instead of creating a duplicate.
create unique index review_cases_open_uidx
  on app.review_cases (workspace_id, listing_id, profile_key) where state <> 'superseded';
-- Review queue (spec 11).
create index review_queue_idx on app.review_cases (workspace_id, state, priority desc, created_at, id);
create index review_cases_claim_expiry_idx on app.review_cases (claim_expires_at) where state = 'claimed';

-- Immutable decisions citing exact versions (spec 14, 21). One decision per
-- case version, so a timeout retry cannot submit twice against one version.
create table app.review_decisions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  case_id uuid not null,
  case_version bigint not null,
  listing_id uuid not null,
  listing_revision_id uuid not null,
  valuation_id uuid,
  actor_principal_id uuid not null,
  actor_kind text not null,
  actor_role text,
  outcome text not null,
  reason_codes text[] not null,
  summary text not null,
  evidence_ids uuid[] not null default '{}',
  missing_information text[] not null default '{}',
  model_name text,
  model_version text,
  model_run_id text,
  prompt_template_version text,
  tool_request_id text,
  input_hash text,
  supersedes_id uuid,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  constraint review_decisions_workspace_id_uk unique (workspace_id, id),
  constraint review_decisions_case_id_uk unique (workspace_id, case_id, id),
  constraint review_decisions_case_version_uk unique (workspace_id, case_id, case_version),
  constraint review_decisions_case_fk foreign key (workspace_id, listing_id, case_id)
    references app.review_cases (workspace_id, listing_id, id),
  constraint review_decisions_revision_fk foreign key (workspace_id, listing_id, listing_revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint review_decisions_valuation_fk foreign key (workspace_id, listing_id, valuation_id)
    references app.valuations (workspace_id, listing_id, id),
  constraint review_decisions_supersedes_fk foreign key (workspace_id, case_id, supersedes_id)
    references app.review_decisions (workspace_id, case_id, id),
  constraint review_decisions_case_version_ck check (case_version > 0),
  constraint review_decisions_actor_kind_ck check (actor_kind in ('user', 'mcp_client', 'system')),
  constraint review_decisions_actor_role_ck check (actor_role is null or actor_role in ('owner', 'reviewer', 'viewer')),
  constraint review_decisions_outcome_ck check (outcome in ('needs_information', 'watch', 'shortlisted', 'rejected')),
  constraint review_decisions_reason_codes_ck check (
    pg_catalog.cardinality(reason_codes) >= 1 and app.text_array_ok(reason_codes, 20, 80)),
  constraint review_decisions_summary_ck check (pg_catalog.length(summary) between 10 and 4000),
  constraint review_decisions_evidence_ids_ck check (app.uuid_array_ok(evidence_ids, 100)),
  constraint review_decisions_missing_information_ck check (app.text_array_ok(missing_information, 30, 300)),
  constraint review_decisions_model_ck check (
    (model_name is null or pg_catalog.length(model_name) <= 200)
    and (model_version is null or pg_catalog.length(model_version) <= 200)
    and (model_run_id is null or pg_catalog.length(model_run_id) <= 200)
    and (prompt_template_version is null or pg_catalog.length(prompt_template_version) <= 200)
    and (tool_request_id is null or pg_catalog.length(tool_request_id) <= 200)),
  constraint review_decisions_input_hash_ck check (input_hash is null or input_hash ~ '^[0-9a-f]{64}$')
);
create index review_decisions_case_idx on app.review_decisions (workspace_id, case_id, created_at desc);

alter table app.review_cases
  add constraint review_cases_latest_decision_fk
  foreign key (workspace_id, id, latest_decision_id)
  references app.review_decisions (workspace_id, case_id, id)
  deferrable initially deferred;

create table app.watchlists (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  created_by uuid not null,
  reason text not null,
  expires_at timestamptz,
  recheck_interval_seconds integer,
  next_recheck_at timestamptz,
  active boolean not null default true,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint watchlists_workspace_id_uk unique (workspace_id, id),
  constraint watchlists_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint watchlists_reason_ck check (pg_catalog.length(reason) between 3 and 2000),
  constraint watchlists_expiry_ck check (expires_at is null or expires_at > created_at),
  -- Requested recheck frequency is bounded: at most hourly, at least monthly.
  constraint watchlists_interval_ck check (
    recheck_interval_seconds is null or recheck_interval_seconds between 3600 and 2592000),
  constraint watchlists_row_version_ck check (row_version > 0)
);
create unique index watchlists_active_uidx on app.watchlists (workspace_id, listing_id, created_by) where active;
create index watchlists_recheck_idx on app.watchlists (workspace_id, next_recheck_at) where active;

-- Private notes, kept separate from extracted seller claims (spec 11).
create table app.owner_notes (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  case_id uuid,
  author_principal_id uuid not null,
  author_kind text not null,
  label text not null,
  body text not null,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint owner_notes_workspace_id_uk unique (workspace_id, id),
  constraint owner_notes_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint owner_notes_case_fk foreign key (workspace_id, listing_id, case_id)
    references app.review_cases (workspace_id, listing_id, id),
  constraint owner_notes_author_kind_ck check (author_kind in ('user', 'mcp_client', 'system')),
  constraint owner_notes_label_ck check (label in ('owner', 'reviewer', 'assistant')),
  constraint owner_notes_body_ck check (pg_catalog.length(body) between 1 and 4000),
  constraint owner_notes_row_version_ck check (row_version > 0)
);
create index owner_notes_listing_idx on app.owner_notes (workspace_id, listing_id, created_at desc);

-- Approved external destinations (spec 22). Enabling requires a recorded owner
-- approval. External IDs are identifiers, never secrets or webhook URLs.
create table app.destination_bindings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  provider text not null,
  label text not null,
  external_workspace_id text,
  external_channel_id text,
  external_app_id text,
  approval_reference text,
  approved_by uuid,
  approved_at timestamptz,
  enabled boolean not null default false,
  verified_at timestamptz,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint destination_bindings_workspace_id_uk unique (workspace_id, id),
  constraint destination_bindings_provider_ck check (provider in ('slack', 'mcp_events')),
  constraint destination_bindings_label_ck check (pg_catalog.length(label) between 3 and 120),
  constraint destination_bindings_external_ids_ck check (
    (external_workspace_id is null or external_workspace_id ~ '^[A-Za-z0-9_.:-]{1,100}$')
    and (external_channel_id is null or external_channel_id ~ '^[A-Za-z0-9_.:-]{1,100}$')
    and (external_app_id is null or external_app_id ~ '^[A-Za-z0-9_.:-]{1,100}$')),
  constraint destination_bindings_slack_ids_ck check (
    provider <> 'slack' or (external_workspace_id is not null and external_channel_id is not null)),
  constraint destination_bindings_approval_pair_ck check ((approved_by is null) = (approved_at is null)),
  constraint destination_bindings_enabled_ck check (
    not enabled or (approved_by is not null and approved_at is not null and approval_reference is not null)),
  constraint destination_bindings_approval_reference_ck check (
    approval_reference is null or pg_catalog.length(approval_reference) between 3 and 500),
  constraint destination_bindings_row_version_ck check (row_version > 0)
);

create table app.notification_preferences (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  destination_binding_id uuid not null,
  event_categories text[] not null default '{}',
  -- e.g. {"start": "22:00", "end": "07:00", "timezone": "Europe/Skopje"}; owner-chosen.
  quiet_hours jsonb,
  urgency_policy jsonb,
  enabled boolean not null default false,
  approval_reference text,
  approved_by uuid,
  approved_at timestamptz,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notification_preferences_workspace_id_uk unique (workspace_id, id),
  constraint notification_preferences_binding_uk unique (workspace_id, destination_binding_id),
  constraint notification_preferences_binding_fk foreign key (workspace_id, destination_binding_id)
    references app.destination_bindings (workspace_id, id),
  constraint notification_preferences_categories_ck check (
    app.text_array_ok(event_categories, 30, 80)
    and pg_catalog.array_to_string(event_categories, ',') ~ '^[a-z0-9_.,]*$'),
  constraint notification_preferences_quiet_hours_ck check (
    quiet_hours is null or pg_catalog.jsonb_typeof(quiet_hours) = 'object'),
  constraint notification_preferences_urgency_ck check (
    urgency_policy is null or pg_catalog.jsonb_typeof(urgency_policy) = 'object'),
  constraint notification_preferences_approval_pair_ck check ((approved_by is null) = (approved_at is null)),
  constraint notification_preferences_enabled_ck check (
    not enabled or (approved_by is not null and approved_at is not null and approval_reference is not null)),
  constraint notification_preferences_row_version_ck check (row_version > 0)
);

-- Fixture data never flows into real review records (spec 18: fixtures are
-- visibly labelled and cannot generate notifications or real opportunities).
create or replace function app.review_cases_check_fixture_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if not new.is_fixture and new.valuation_id is not null and exists (
       select 1
         from app.valuations v
        where v.workspace_id = new.workspace_id
          and v.id = new.valuation_id
          and v.is_fixture) then
    raise exception using
      errcode = 'SV003',
      message = 'a non-fixture review case cannot reference a fixture valuation';
  end if;
  return new;
end
$$;

-- A decision carries its case's fixture flag, and a real decision never cites
-- a fixture valuation.
create or replace function app.review_decisions_check_fixture_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  case_fixture boolean;
begin
  select c.is_fixture
    into case_fixture
    from app.review_cases c
   where c.workspace_id = new.workspace_id
     and c.id = new.case_id;
  if case_fixture is not null and case_fixture <> new.is_fixture then
    raise exception using
      errcode = 'SV003',
      message = 'a review decision must carry the is_fixture flag of its case';
  end if;
  if not new.is_fixture and new.valuation_id is not null and exists (
       select 1
         from app.valuations v
        where v.workspace_id = new.workspace_id
          and v.id = new.valuation_id
          and v.is_fixture) then
    raise exception using
      errcode = 'SV003',
      message = 'a non-fixture review decision cannot cite a fixture valuation';
  end if;
  return new;
end
$$;

create trigger review_cases_fixture_lineage before insert or update of valuation_id, is_fixture
  on app.review_cases
  for each row execute function app.review_cases_check_fixture_lineage();
create trigger review_decisions_fixture_lineage before insert on app.review_decisions
  for each row execute function app.review_decisions_check_fixture_lineage();
create trigger review_cases_touch before update on app.review_cases
  for each row execute function app.touch_updated_at();
create trigger review_cases_version_guard before update on app.review_cases
  for each row execute function app.guard_version('row_version');
create trigger review_cases_identity_frozen before update on app.review_cases
  for each row execute function app.guard_frozen_columns(
    'revision_id', 'valuation_id', 'queue_label', 'state', 'readiness', 'priority', 'ranking',
    'ranking_version', 'row_version', 'claim_holder', 'claim_token_hash', 'claimed_at',
    'claim_expires_at', 'latest_decision_id', 'reason', 'superseded_by_id', 'updated_at');
create trigger watchlists_touch before update on app.watchlists
  for each row execute function app.touch_updated_at();
create trigger watchlists_version_guard before update on app.watchlists
  for each row execute function app.guard_version('row_version');
create trigger owner_notes_touch before update on app.owner_notes
  for each row execute function app.touch_updated_at();
create trigger owner_notes_version_guard before update on app.owner_notes
  for each row execute function app.guard_version('row_version');
create trigger owner_notes_frozen before update on app.owner_notes
  for each row execute function app.guard_frozen_columns('body', 'row_version', 'updated_at');
create trigger destination_bindings_touch before update on app.destination_bindings
  for each row execute function app.touch_updated_at();
create trigger destination_bindings_version_guard before update on app.destination_bindings
  for each row execute function app.guard_version('row_version');
create trigger notification_preferences_touch before update on app.notification_preferences
  for each row execute function app.touch_updated_at();
create trigger notification_preferences_version_guard before update on app.notification_preferences
  for each row execute function app.guard_version('row_version');

call ops.apply_security_baseline();
