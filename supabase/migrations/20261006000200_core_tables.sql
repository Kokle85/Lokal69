-- =============================================================================
-- 20261006000200_core_tables
-- Workspaces, memberships, immutable configuration revisions, the source
-- registry and search profiles. Spec sections 3, 5, 11, 12 and 26.
-- =============================================================================

create table app.workspaces (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  display_timezone text not null default 'Europe/Skopje',
  -- Inactive workspaces are skipped by ops.active_workspace_ids() (schedulers/workers).
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint workspaces_name_ck check (pg_catalog.length(pg_catalog.btrim(name)) between 1 and 200),
  constraint workspaces_timezone_ck check (display_timezone ~ '^[A-Za-z0-9_+/-]{1,64}$')
);

create table app.memberships (
  workspace_id uuid not null references app.workspaces (id),
  user_id uuid not null references auth.users (id),
  role text not null,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, user_id),
  constraint memberships_role_ck check (role in ('owner', 'reviewer', 'viewer'))
);
create index memberships_user_idx on app.memberships (user_id, workspace_id) where active;

-- Immutable business-configuration history (spec section 26): actor, reason,
-- before/after and effective time. Append-only (trigger in the security migration).
create table app.config_revisions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  revision integer not null,
  config jsonb not null,
  config_hash text not null,
  before jsonb,
  author_principal_id uuid not null,
  author_kind text not null,
  author_label text,
  reason text not null,
  created_at timestamptz not null default now(),
  effective_at timestamptz not null default now(),
  constraint config_revisions_workspace_id_uk unique (workspace_id, id),
  constraint config_revisions_revision_uk unique (workspace_id, revision),
  constraint config_revisions_revision_ck check (revision > 0),
  constraint config_revisions_config_ck check (pg_catalog.jsonb_typeof(config) = 'object'),
  constraint config_revisions_before_ck check (before is null or pg_catalog.jsonb_typeof(before) = 'object'),
  constraint config_revisions_hash_ck check (config_hash ~ '^[0-9a-f]{64}$'),
  constraint config_revisions_author_kind_ck check (author_kind in ('user', 'mcp_client', 'system')),
  constraint config_revisions_author_label_ck check (author_label is null or pg_catalog.length(author_label) <= 200),
  constraint config_revisions_reason_ck check (pg_catalog.length(pg_catalog.btrim(reason)) between 3 and 2000)
);

-- Source registry (spec section 5). Mirrors suv_deals.domain.sources.SourceConfig.
-- Terms decisions and technical status are recorded independently. The full
-- validated SourceConfig (rate budget, search parameters, tracking parameters,
-- notes) is kept in `config`; query-critical fields are typed columns.
create table app.sources (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_key text not null,
  display_name text not null,
  country char(2) not null,
  role text not null,
  mode text not null,
  adapter text not null,
  adapter_version text not null default 'unimplemented',
  enabled boolean not null default false,
  technical_status text not null default 'untested',
  terms_status text not null default 'unreviewed',
  terms_url text,
  terms_reviewed_at timestamptz,
  terms_decision text not null default 'pending',
  terms_decision_actor text,
  terms_decision_note text,
  technical_denial_policy text not null default 'stop_and_report',
  robots_policy text not null default 'obey',
  allowed_hosts text[] not null default '{}',
  allowed_search_paths text[] not null default '{}',
  allowed_detail_paths text[] not null default '{}',
  source_timezone text not null default 'Europe/Berlin',
  config jsonb not null default '{}'::jsonb,
  paused boolean not null default false,
  pause_reason text,
  paused_by uuid,
  paused_at timestamptz,
  last_live_smoke_at timestamptz,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint sources_workspace_id_uk unique (workspace_id, id),
  constraint sources_key_uk unique (workspace_id, source_key),
  constraint sources_key_ck check (source_key ~ '^[a-z0-9_]{3,60}$'),
  constraint sources_display_name_ck check (pg_catalog.length(display_name) between 2 and 120),
  constraint sources_country_ck check (country ~ '^[A-Z]{2}$'),
  constraint sources_role_ck check (role in ('acquisition', 'mk_comparable')),
  constraint sources_mode_ck check (mode in ('public_html', 'official_api', 'fixture')),
  constraint sources_adapter_ck check (pg_catalog.length(adapter) between 3 and 80),
  constraint sources_adapter_version_ck check (pg_catalog.length(adapter_version) between 1 and 40),
  constraint sources_technical_status_ck check (technical_status in (
    'untested', 'fixture_tested', 'live_smoke_passed', 'degraded', 'parser_unhealthy', 'access_blocked')),
  constraint sources_terms_status_ck check (terms_status in (
    'unreviewed', 'permitted', 'no_restriction_found', 'restricted')),
  constraint sources_terms_decision_ck check (terms_decision in (
    'pending', 'proceed_acknowledged', 'proceed_permitted', 'do_not_use')),
  constraint sources_terms_url_ck check (terms_url is null or (terms_url ~ '^https?://' and pg_catalog.length(terms_url) <= 2048)),
  constraint sources_terms_actor_ck check (terms_decision_actor is null or pg_catalog.length(terms_decision_actor) <= 200),
  constraint sources_terms_note_ck check (terms_decision_note is null or pg_catalog.length(terms_decision_note) <= 2000),
  constraint sources_denial_policy_ck check (technical_denial_policy = 'stop_and_report'),
  constraint sources_robots_policy_ck check (robots_policy = 'obey'),
  constraint sources_allowed_hosts_ck check (app.text_array_ok(allowed_hosts, 50, 253)),
  constraint sources_allowed_search_paths_ck check (app.text_array_ok(allowed_search_paths, 50, 500)),
  constraint sources_allowed_detail_paths_ck check (app.text_array_ok(allowed_detail_paths, 50, 500)),
  constraint sources_timezone_ck check (source_timezone ~ '^[A-Za-z0-9_+/-]{1,64}$'),
  constraint sources_config_ck check (pg_catalog.jsonb_typeof(config) = 'object'),
  constraint sources_pause_ck check (not paused or (pause_reason is not null and paused_at is not null)),
  constraint sources_pause_reason_ck check (pause_reason is null or pg_catalog.length(pause_reason) between 3 and 2000),
  constraint sources_version_ck check (version > 0),
  -- Activation gate, mirroring domain.sources.activation_problems(): a source can
  -- only be enabled with an implemented adapter, a reviewed terms decision that
  -- names its actor, a tested and unblocked adapter and a host/path policy.
  constraint sources_enable_gate_ck check (
    not enabled or (
      adapter_version <> 'unimplemented'
      and terms_status <> 'unreviewed'
      and terms_decision in ('proceed_acknowledged', 'proceed_permitted')
      and terms_decision_actor is not null
      and technical_status not in ('untested', 'access_blocked', 'parser_unhealthy')
      and pg_catalog.cardinality(allowed_hosts) > 0
      and pg_catalog.cardinality(allowed_detail_paths) > 0
      and (role <> 'acquisition' or pg_catalog.cardinality(allowed_search_paths) > 0)
    )
  )
);

-- Search/eligibility profiles (spec section 3). The confirmed baseline is
-- enforced here as well as in domain.profiles.validate_baseline():
--   primary: EUR 2,500.00 <= price <= EUR 3,000.00 inclusive, enabled,
--            mileage strictly below 200,000 km;
--   manual_4000: ceiling EUR 4,000.00 (disabled by default, separate queue);
--   below_target_watch: strictly below EUR 2,500.00 (exclusive bound).
-- No profile may ever allow 200,000 km or more. Queue labels are unique so the
-- optional profiles are always a visibly different queue.
create table app.search_profiles (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  profile_key text not null,
  label text not null,
  queue_label text not null,
  enabled boolean not null default false,
  min_price_eur numeric(12, 2),
  max_price_eur numeric(12, 2) not null,
  max_price_inclusive boolean not null default true,
  max_mileage_km_exclusive numeric(14, 6) not null default 200000,
  criteria jsonb not null default '{}'::jsonb,
  config_revision_id uuid not null,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint search_profiles_workspace_id_uk unique (workspace_id, id),
  constraint search_profiles_key_uk unique (workspace_id, profile_key),
  constraint search_profiles_queue_label_uk unique (workspace_id, queue_label),
  constraint search_profiles_config_revision_fk foreign key (workspace_id, config_revision_id)
    references app.config_revisions (workspace_id, id),
  constraint search_profiles_key_ck check (profile_key in ('primary', 'manual_4000', 'below_target_watch')),
  constraint search_profiles_label_ck check (pg_catalog.length(label) between 3 and 120),
  constraint search_profiles_queue_label_ck check (pg_catalog.length(queue_label) between 3 and 120),
  constraint search_profiles_price_ck check (
    max_price_eur > 0 and (min_price_eur is null or (min_price_eur >= 0 and min_price_eur <= max_price_eur))),
  constraint search_profiles_mileage_ck check (max_mileage_km_exclusive > 0 and max_mileage_km_exclusive <= 200000),
  constraint search_profiles_criteria_ck check (pg_catalog.jsonb_typeof(criteria) = 'object'),
  constraint search_profiles_row_version_ck check (row_version > 0),
  constraint search_profiles_primary_baseline_ck check (
    profile_key <> 'primary' or (
      min_price_eur = 2500.00 and max_price_eur = 3000.00 and max_price_inclusive
      and max_mileage_km_exclusive = 200000 and enabled)),
  constraint search_profiles_manual_4000_ck check (
    profile_key <> 'manual_4000' or max_price_eur = 4000.00),
  constraint search_profiles_below_target_ck check (
    profile_key <> 'below_target_watch' or (max_price_eur = 2500.00 and not max_price_inclusive))
);
create trigger workspaces_touch before update on app.workspaces
  for each row execute function app.touch_updated_at();
create trigger memberships_touch before update on app.memberships
  for each row execute function app.touch_updated_at();
create trigger sources_touch before update on app.sources
  for each row execute function app.touch_updated_at();
create trigger sources_version_guard before update on app.sources
  for each row execute function app.guard_version('version');
create trigger search_profiles_touch before update on app.search_profiles
  for each row execute function app.touch_updated_at();
create trigger search_profiles_version_guard before update on app.search_profiles
  for each row execute function app.guard_version('row_version');

call ops.apply_security_baseline();
