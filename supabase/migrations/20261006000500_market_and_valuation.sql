-- =============================================================================
-- 20261006000500_market_and_valuation
-- MK comparable evidence (spec 15), FX observations (spec 18 FX rules),
-- versioned approval-gated tax rule sets (spec 16), cost profiles and cost
-- evidence (spec 18) and reproducible valuations (spec 18).
--
-- No tax rates, coefficients or formulas are shipped here: tax_rule_sets only
-- stores owner-supplied rule sets with their approval evidence, and fixture
-- rule sets can never become approved or active.
--
-- FX ownership decision: FX observations are WORKSPACE-OWNED. Reference rates
-- are public data, but payment (bank) rates are private to the owner, and a
-- uniform workspace_id keeps RLS and composite references identical for every
-- table. Duplicating a few ECB rows per workspace is negligible.
-- =============================================================================

-- MK market evidence. evidence_kind separates advertised asking prices from
-- (unverified or verified) sale evidence and explicit owner assumptions. A
-- removed or sold-marked listing does not reveal a realized price.
create table app.market_observations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid,
  listing_id uuid,
  source_listing_id text,
  evidence_kind text not null,
  market char(2) not null default 'MK',
  amount_minor bigint,
  currency char(3),
  price_basis text not null default 'unknown',
  normalized jsonb not null,
  make text,
  model text,
  vehicle_generation text,
  facelift text not null default 'unknown',
  engine_code text,
  engine_displacement_cm3 integer,
  power_kw integer,
  registration_year smallint,
  fuel text not null default 'unknown',
  gearbox text not null default 'unknown',
  drive text not null default 'unknown',
  mileage_km numeric(14, 6),
  local_registration_status text not null default 'unknown',
  seller_type text not null default 'unknown',
  cluster_id uuid,
  observed_at timestamptz not null,
  source_published_at timestamptz,
  url text,
  archived_snapshot_id uuid,
  confidence text not null,
  evidence jsonb not null default '{}'::jsonb,
  recorded_by uuid,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  constraint market_observations_workspace_id_uk unique (workspace_id, id),
  constraint market_observations_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint market_observations_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint market_observations_cluster_fk foreign key (workspace_id, cluster_id)
    references app.vehicle_clusters (workspace_id, id),
  constraint market_observations_snapshot_fk foreign key (workspace_id, archived_snapshot_id)
    references ops.source_snapshots (workspace_id, id),
  constraint market_observations_kind_ck check (evidence_kind in (
    'asking_price', 'seller_reported_sale', 'verified_sale', 'owner_estimate')),
  constraint market_observations_market_ck check (market ~ '^[A-Z]{2}$'),
  constraint market_observations_amount_ck check (amount_minor is null or amount_minor >= 0),
  constraint market_observations_currency_ck check (currency is null or currency ~ '^[A-Z]{3}$'),
  constraint market_observations_currency_pair_ck check ((amount_minor is null) = (currency is null)),
  -- A sale claim without a stated amount is allowed; every other kind needs one.
  constraint market_observations_amount_required_ck check (
    evidence_kind = 'seller_reported_sale' or amount_minor is not null),
  constraint market_observations_verified_sale_ck check (
    evidence_kind <> 'verified_sale' or evidence <> '{}'::jsonb),
  constraint market_observations_owner_estimate_ck check (
    evidence_kind <> 'owner_estimate' or recorded_by is not null),
  constraint market_observations_public_source_ck check (
    evidence_kind not in ('asking_price', 'seller_reported_sale') or source_id is not null or url is not null),
  constraint market_observations_basis_ck check (price_basis in ('gross', 'net', 'unknown')),
  constraint market_observations_normalized_ck check (pg_catalog.jsonb_typeof(normalized) = 'object'),
  constraint market_observations_vehicle_text_ck check (
    (make is null or pg_catalog.length(make) <= 80)
    and (model is null or pg_catalog.length(model) <= 120)
    and (vehicle_generation is null or pg_catalog.length(vehicle_generation) <= 80)
    and (engine_code is null or pg_catalog.length(engine_code) <= 40)
    and (source_listing_id is null or pg_catalog.length(source_listing_id) <= 200)),
  constraint market_observations_facelift_ck check (facelift in ('yes', 'no', 'unknown')),
  constraint market_observations_engine_ck check (
    (engine_displacement_cm3 is null or engine_displacement_cm3 between 50 and 10000)
    and (power_kw is null or power_kw between 1 and 2000)),
  constraint market_observations_year_ck check (registration_year is null or registration_year between 1950 and 2100),
  constraint market_observations_fuel_ck check (fuel in (
    'diesel', 'petrol', 'hybrid_petrol', 'hybrid_diesel', 'plugin_hybrid', 'lpg', 'cng', 'electric',
    'other', 'unknown')),
  constraint market_observations_gearbox_ck check (gearbox in ('manual', 'automatic', 'semi_automatic', 'unknown')),
  constraint market_observations_drive_ck check (drive in ('fwd', 'rwd', 'awd', '4wd', 'unknown')),
  constraint market_observations_mileage_ck check (mileage_km is null or mileage_km >= 0),
  constraint market_observations_registration_status_ck check (local_registration_status in (
    'locally_registered', 'imported_unregistered', 'unknown')),
  constraint market_observations_seller_type_ck check (seller_type in ('dealer', 'private', 'unknown')),
  constraint market_observations_url_ck check (
    url is null or (pg_catalog.length(url) between 8 and 2048 and url ~* '^https?://')),
  constraint market_observations_confidence_ck check (confidence in ('high', 'medium', 'low')),
  constraint market_observations_evidence_ck check (pg_catalog.jsonb_typeof(evidence) = 'object')
);
-- Comparable lookup (spec 11): market, make/model/generation, year,
-- fuel/gearbox/drive and observation time.
create index market_observations_comparable_idx
  on app.market_observations (workspace_id, market, make, model, vehicle_generation, registration_year,
                              fuel, gearbox, drive, observed_at desc);

-- An inspectable, reproducible comparable selection for one target revision.
create table app.comparable_sets (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  target_revision_id uuid not null,
  criteria_version text not null,
  criteria jsonb not null,
  sample_size integer not null,
  selected_count integer not null,
  excluded_count integer not null,
  sample_quality text not null,
  currency char(3),
  statistics jsonb,
  date_span_from timestamptz,
  date_span_to timestamptz,
  rationale text,
  is_fixture boolean not null default false,
  computed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint comparable_sets_workspace_id_uk unique (workspace_id, id),
  constraint comparable_sets_listing_id_uk unique (workspace_id, listing_id, id),
  constraint comparable_sets_revision_fk foreign key (workspace_id, listing_id, target_revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint comparable_sets_criteria_version_ck check (pg_catalog.length(criteria_version) between 1 and 80),
  constraint comparable_sets_criteria_ck check (pg_catalog.jsonb_typeof(criteria) = 'object'),
  constraint comparable_sets_counts_ck check (
    sample_size >= 0 and selected_count >= 0 and excluded_count >= 0 and selected_count <= sample_size),
  -- A small set is labelled small; no adequate set means insufficient_comparables.
  constraint comparable_sets_quality_ck check (sample_quality in ('adequate', 'small', 'insufficient')),
  constraint comparable_sets_quality_count_ck check (sample_quality <> 'adequate' or selected_count > 0),
  constraint comparable_sets_currency_ck check (currency is null or currency ~ '^[A-Z]{3}$'),
  constraint comparable_sets_statistics_ck check (
    statistics is null or (pg_catalog.jsonb_typeof(statistics) = 'object' and currency is not null)),
  constraint comparable_sets_span_ck check (
    date_span_from is null or date_span_to is null or date_span_to >= date_span_from),
  constraint comparable_sets_rationale_ck check (rationale is null or pg_catalog.length(rationale) <= 4000)
);
create index comparable_sets_revision_idx on app.comparable_sets (workspace_id, target_revision_id, computed_at desc);

create table app.comparable_set_members (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  comparable_set_id uuid not null,
  market_observation_id uuid not null,
  disposition text not null,
  reasons text[] not null default '{}',
  differences jsonb not null default '{}'::jsonb,
  widened_dimensions text[] not null default '{}',
  weight numeric(9, 6),
  created_at timestamptz not null default now(),
  constraint comparable_set_members_workspace_id_uk unique (workspace_id, id),
  constraint comparable_set_members_member_uk unique (workspace_id, comparable_set_id, market_observation_id),
  constraint comparable_set_members_set_fk foreign key (workspace_id, comparable_set_id)
    references app.comparable_sets (workspace_id, id),
  constraint comparable_set_members_observation_fk foreign key (workspace_id, market_observation_id)
    references app.market_observations (workspace_id, id),
  constraint comparable_set_members_disposition_ck check (disposition in ('selected', 'excluded')),
  constraint comparable_set_members_reasons_ck check (app.text_array_ok(reasons, 30, 200)),
  -- Every excluded candidate records why (spec 15).
  constraint comparable_set_members_excluded_reason_ck check (
    disposition <> 'excluded' or pg_catalog.cardinality(reasons) > 0),
  constraint comparable_set_members_differences_ck check (pg_catalog.jsonb_typeof(differences) = 'object'),
  constraint comparable_set_members_widened_ck check (app.text_array_ok(widened_dimensions, 10, 80)),
  constraint comparable_set_members_weight_ck check (
    (disposition = 'selected' and (weight is null or weight >= 0))
    or (disposition = 'excluded' and weight is null))
);
create index comparable_set_members_set_idx on app.comparable_set_members (workspace_id, comparable_set_id, disposition);

-- FX observations with explicit direction: 1 unit of base = rate units of quote.
-- Converting quote -> base divides (spec 18). No parity assumptions.
create table app.fx_rates (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  base char(3) not null,
  quote char(3) not null,
  rate numeric(20, 10) not null,
  rate_date date not null,
  retrieved_at timestamptz not null,
  provider text not null,
  purpose text not null,
  source_ref text,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  constraint fx_rates_workspace_id_uk unique (workspace_id, id),
  constraint fx_rates_observation_uk unique (workspace_id, base, quote, rate_date, provider, purpose),
  constraint fx_rates_currency_ck check (base ~ '^[A-Z]{3}$' and quote ~ '^[A-Z]{3}$' and base <> quote),
  constraint fx_rates_rate_ck check (rate > 0),
  constraint fx_rates_provider_ck check (pg_catalog.length(provider) between 1 and 100),
  constraint fx_rates_purpose_ck check (purpose in ('reference', 'customs', 'payment')),
  constraint fx_rates_source_ref_ck check (source_ref is null or pg_catalog.length(source_ref) <= 500)
);
create index fx_rates_latest_idx on app.fx_rates (workspace_id, base, quote, purpose, rate_date desc);

-- Versioned, approval-gated tax rule sets. Lifecycle (spec 16):
--   draft -> under_review -> approved -> active -> superseded | expired | revoked
-- plus under_review -> draft (rework) and revocation from any open state.
-- 'unapproved' marks example/fixture rule sets that are never selectable.
-- Content is frozen once a rule set leaves draft (trigger below); approved and
-- active rule sets carry approver, approval time, sources, hash and validity.
create table app.tax_rule_sets (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  rule_set_id text not null,
  jurisdiction char(2) not null,
  vehicle_category text not null default 'passenger_car',
  version text not null,
  status text not null default 'draft',
  valid_from date,
  valid_to date,
  currency char(3),
  rules jsonb not null default '{}'::jsonb,
  sources jsonb not null default '[]'::jsonb,
  sha256 text,
  approved_by uuid,
  approved_at timestamptz,
  approval_reference text,
  is_fixture boolean not null default false,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint tax_rule_sets_workspace_id_uk unique (workspace_id, id),
  constraint tax_rule_sets_version_uk unique (workspace_id, rule_set_id, version),
  constraint tax_rule_sets_rule_set_id_ck check (rule_set_id ~ '^[A-Za-z0-9_.:-]{3,200}$'),
  constraint tax_rule_sets_jurisdiction_ck check (jurisdiction ~ '^[A-Z]{2}$'),
  constraint tax_rule_sets_category_ck check (vehicle_category ~ '^[a-z0-9_]{1,80}$'),
  constraint tax_rule_sets_version_ck check (pg_catalog.length(version) between 1 and 80),
  constraint tax_rule_sets_status_ck check (status in (
    'draft', 'under_review', 'approved', 'active', 'superseded', 'expired', 'revoked', 'unapproved')),
  constraint tax_rule_sets_validity_ck check (
    valid_to is null or (valid_from is not null and valid_to > valid_from)),
  constraint tax_rule_sets_currency_ck check (currency is null or currency ~ '^[A-Z]{3}$'),
  constraint tax_rule_sets_rules_ck check (pg_catalog.jsonb_typeof(rules) = 'object'),
  constraint tax_rule_sets_sources_ck check (pg_catalog.jsonb_typeof(sources) = 'array'),
  constraint tax_rule_sets_sha256_ck check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$'),
  constraint tax_rule_sets_approval_pair_ck check ((approved_by is null) = (approved_at is null)),
  constraint tax_rule_sets_approved_ck check (
    status not in ('approved', 'active')
    or (approved_by is not null and approved_at is not null
        and pg_catalog.jsonb_array_length(sources) > 0
        and sha256 is not null and valid_from is not null and currency is not null)),
  constraint tax_rule_sets_approval_reference_ck check (
    approval_reference is null or pg_catalog.length(approval_reference) <= 500),
  -- Fixture/example rule sets are never approved or activated (no invented taxes).
  constraint tax_rule_sets_fixture_ck check (not is_fixture or status in ('draft', 'unapproved', 'revoked')),
  -- No overlapping active versions with ambiguous applicability (spec 16).
  constraint tax_rule_sets_no_active_overlap exclude using gist (
    workspace_id with =,
    jurisdiction with =,
    vehicle_category with =,
    daterange(valid_from, valid_to, '[)') with &&
  ) where (status = 'active')
);
create index tax_rule_sets_lookup_idx on app.tax_rule_sets (workspace_id, jurisdiction, vehicle_category, status);

create or replace function app.tax_rule_sets_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  allowed boolean;
  lifecycle_cols constant text[] := array[
    'status', 'approved_by', 'approved_at', 'approval_reference', 'valid_to', 'updated_at'];
begin
  if old.status <> 'draft'
     and (pg_catalog.to_jsonb(new) - lifecycle_cols) is distinct from (pg_catalog.to_jsonb(old) - lifecycle_cols) then
    raise exception using
      errcode = 'SV004',
      message = 'tax rule set content is frozen once it leaves draft; create a new version';
  end if;
  if old.status <> 'draft' and new.valid_to is distinct from old.valid_to
     and (new.valid_to is null or (old.valid_to is not null and new.valid_to > old.valid_to)) then
    raise exception using
      errcode = 'SV004',
      message = 'valid_to of a submitted tax rule set may only be shortened, never extended';
  end if;
  if old.approved_at is not null
     and (new.approved_by is distinct from old.approved_by or new.approved_at is distinct from old.approved_at) then
    raise exception using errcode = 'SV004', message = 'tax rule set approval record is immutable';
  end if;
  if new.status is distinct from old.status then
    allowed := case old.status
      when 'draft' then new.status in ('under_review', 'revoked')
      when 'under_review' then new.status in ('draft', 'approved', 'revoked')
      when 'approved' then new.status in ('active', 'superseded', 'expired', 'revoked')
      when 'active' then new.status in ('superseded', 'expired', 'revoked')
      when 'unapproved' then new.status in ('revoked')
      else false
    end;
    if not allowed then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('tax rule set status %s -> %s is not permitted', old.status, new.status);
    end if;
  end if;
  return new;
end
$$;

create trigger tax_rule_sets_guard before update on app.tax_rule_sets
  for each row execute function app.tax_rule_sets_guard();
create trigger tax_rule_sets_touch before update on app.tax_rule_sets
  for each row execute function app.touch_updated_at();

-- Versioned cost assumptions (logistics, inspection, repairs, reserves). A new
-- version is a new row; only the approval lifecycle columns may change.
create table app.cost_profiles (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  profile_key text not null,
  version integer not null,
  basis text not null,
  assumptions jsonb not null,
  currency char(3) not null,
  approval_status text not null default 'unapproved',
  approved_by uuid,
  approved_at timestamptz,
  config_revision_id uuid,
  is_fixture boolean not null default false,
  created_by uuid,
  created_at timestamptz not null default now(),
  constraint cost_profiles_workspace_id_uk unique (workspace_id, id),
  constraint cost_profiles_version_uk unique (workspace_id, profile_key, version),
  constraint cost_profiles_config_revision_fk foreign key (workspace_id, config_revision_id)
    references app.config_revisions (workspace_id, id),
  constraint cost_profiles_key_ck check (profile_key ~ '^[a-z0-9_]{1,80}$'),
  constraint cost_profiles_version_ck check (version > 0),
  constraint cost_profiles_basis_ck check (pg_catalog.length(basis) between 3 and 2000),
  constraint cost_profiles_assumptions_ck check (pg_catalog.jsonb_typeof(assumptions) = 'object'),
  constraint cost_profiles_currency_ck check (currency ~ '^[A-Z]{3}$'),
  constraint cost_profiles_approval_ck check (approval_status in ('unapproved', 'approved')),
  constraint cost_profiles_approved_ck check (
    approval_status = 'unapproved' or (approved_by is not null and approved_at is not null)),
  constraint cost_profiles_fixture_ck check (not is_fixture or approval_status = 'unapproved')
);
create trigger cost_profiles_frozen before update on app.cost_profiles
  for each row execute function app.guard_frozen_columns('approval_status', 'approved_by', 'approved_at');

-- Cost evidence: quotes, estimates and actuals with explicit scope and expiry.
-- A quote for one city/vehicle condition cannot silently apply to another.
create table app.cost_evidence (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  kind text not null,
  category text not null,
  provider text,
  low_minor bigint,
  base_minor bigint,
  high_minor bigint,
  currency char(3),
  obtained_at timestamptz not null,
  expires_at timestamptz,
  scope jsonb not null,
  evidence jsonb not null default '{}'::jsonb,
  document_ref text,
  listing_id uuid,
  supersedes_id uuid,
  recorded_by uuid,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  constraint cost_evidence_workspace_id_uk unique (workspace_id, id),
  constraint cost_evidence_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint cost_evidence_supersedes_fk foreign key (workspace_id, supersedes_id)
    references app.cost_evidence (workspace_id, id),
  constraint cost_evidence_kind_ck check (kind in ('quote', 'estimate', 'actual')),
  constraint cost_evidence_category_ck check (category in (
    'purchase', 'bank_fx_charges', 'travel_inspection', 'transport', 'export_plates_insurance',
    'customs_broker', 'import_duty', 'motor_vehicle_tax', 'import_vat', 'other_import_charges',
    'homologation_registration', 'repairs', 'preparation', 'risk_reserve', 'storage_holding',
    'selling_costs', 'refundable_deposit')),
  constraint cost_evidence_provider_ck check (provider is null or pg_catalog.length(provider) between 1 and 200),
  constraint cost_evidence_quote_provider_ck check (kind <> 'quote' or provider is not null),
  constraint cost_evidence_amounts_ck check (
    (low_minor is null or low_minor >= 0) and (base_minor is null or base_minor >= 0)
    and (high_minor is null or high_minor >= 0)),
  constraint cost_evidence_has_amount_ck check (
    low_minor is not null or base_minor is not null or high_minor is not null),
  constraint cost_evidence_currency_ck check (currency is null or currency ~ '^[A-Z]{3}$'),
  constraint cost_evidence_currency_pair_ck check (
    (low_minor is null and base_minor is null and high_minor is null) = (currency is null)),
  -- low <= base <= high for every present pair (implies the three-way rule).
  constraint cost_evidence_range_ck check (
    (low_minor is null or base_minor is null or low_minor <= base_minor)
    and (base_minor is null or high_minor is null or base_minor <= high_minor)
    and (low_minor is null or high_minor is null or low_minor <= high_minor)),
  constraint cost_evidence_expiry_ck check (expires_at is null or expires_at > obtained_at),
  constraint cost_evidence_scope_ck check (pg_catalog.jsonb_typeof(scope) = 'object'),
  constraint cost_evidence_evidence_ck check (pg_catalog.jsonb_typeof(evidence) = 'object'),
  constraint cost_evidence_document_ref_ck check (document_ref is null or pg_catalog.length(document_ref) <= 500)
);
create index cost_evidence_category_idx on app.cost_evidence (workspace_id, category, obtained_at desc);
create index cost_evidence_listing_idx on app.cost_evidence (workspace_id, listing_id) where listing_id is not null;

-- Reproducible valuations (spec 18). One valuation references one listing
-- revision, comparable set, tax rule set, FX observations, cost profile and
-- configuration revision, plus a dependency fingerprint used for invalidation.
-- Unknown stays unknown: not_started/incomplete valuations carry no
-- contribution figures; estimated/quote_supported ones carry both base and
-- conservative contributions. Content is immutable; only state may move to
-- stale/invalid (with reason).
create table app.valuations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  listing_revision_id uuid not null,
  comparable_set_id uuid,
  tax_rule_set_id uuid,
  cost_profile_id uuid,
  config_revision_id uuid not null,
  fx_rate_ids uuid[] not null default '{}',
  cost_evidence_ids uuid[] not null default '{}',
  dependency_fingerprint text not null,
  state text not null,
  scenarios jsonb not null default '{}'::jsonb,
  unknowns jsonb not null default '[]'::jsonb,
  warnings jsonb not null default '[]'::jsonb,
  calculation_version text not null,
  currency char(3) not null default 'EUR',
  base_contribution_minor bigint,
  conservative_contribution_minor bigint,
  upside_contribution_minor bigint,
  expires_at timestamptz,
  stale_at timestamptz,
  stale_reason text,
  is_fixture boolean not null default false,
  created_at timestamptz not null default now(),
  constraint valuations_workspace_id_uk unique (workspace_id, id),
  constraint valuations_listing_id_uk unique (workspace_id, listing_id, id),
  constraint valuations_revision_fk foreign key (workspace_id, listing_id, listing_revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint valuations_comparable_set_fk foreign key (workspace_id, listing_id, comparable_set_id)
    references app.comparable_sets (workspace_id, listing_id, id),
  constraint valuations_tax_rule_set_fk foreign key (workspace_id, tax_rule_set_id)
    references app.tax_rule_sets (workspace_id, id),
  constraint valuations_cost_profile_fk foreign key (workspace_id, cost_profile_id)
    references app.cost_profiles (workspace_id, id),
  constraint valuations_config_revision_fk foreign key (workspace_id, config_revision_id)
    references app.config_revisions (workspace_id, id),
  constraint valuations_fx_ids_ck check (app.uuid_array_ok(fx_rate_ids, 50)),
  constraint valuations_cost_evidence_ids_ck check (app.uuid_array_ok(cost_evidence_ids, 200)),
  constraint valuations_fingerprint_ck check (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint valuations_state_ck check (state in (
    'not_started', 'incomplete', 'estimated', 'quote_supported', 'stale', 'invalid')),
  constraint valuations_scenarios_ck check (pg_catalog.jsonb_typeof(scenarios) = 'object'),
  constraint valuations_unknowns_ck check (pg_catalog.jsonb_typeof(unknowns) = 'array'),
  constraint valuations_warnings_ck check (pg_catalog.jsonb_typeof(warnings) = 'array'),
  constraint valuations_calculation_version_ck check (pg_catalog.length(calculation_version) between 1 and 80),
  constraint valuations_currency_ck check (currency ~ '^[A-Z]{3}$'),
  constraint valuations_unknown_not_zero_ck check (
    state not in ('not_started', 'incomplete')
    or (base_contribution_minor is null and conservative_contribution_minor is null
        and upside_contribution_minor is null)),
  constraint valuations_complete_figures_ck check (
    state not in ('estimated', 'quote_supported')
    or (base_contribution_minor is not null and conservative_contribution_minor is not null)),
  constraint valuations_stale_ck check (state <> 'stale' or (stale_at is not null and stale_reason is not null)),
  constraint valuations_stale_reason_ck check (stale_reason is null or pg_catalog.length(stale_reason) <= 500)
);
create index valuations_revision_idx on app.valuations (workspace_id, listing_revision_id, created_at desc);
create index valuations_listing_idx on app.valuations (workspace_id, listing_id, created_at desc);
create index valuations_fx_idx on app.valuations using gin (fx_rate_ids);
create index valuations_tax_rule_idx on app.valuations (workspace_id, tax_rule_set_id) where tax_rule_set_id is not null;

-- Insert-time dependency validation (arrays cannot carry foreign keys):
--   * every FX and cost-evidence ID exists in the same workspace;
--   * a non-fixture valuation never depends on fixture data;
--   * a non-fixture estimated/quote_supported valuation uses an approved or
--     active tax rule set (otherwise import costs are unknown -> incomplete).
create or replace function app.valuations_check_dependencies()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  tax_status text;
  tax_fixture boolean;
begin
  if exists (
    select 1 from pg_catalog.unnest(new.fx_rate_ids) as f(id)
     where not exists (select 1 from app.fx_rates r where r.workspace_id = new.workspace_id and r.id = f.id)
  ) then
    raise exception using errcode = 'SV003', message = 'valuation references an unknown FX rate for this workspace';
  end if;
  if exists (
    select 1 from pg_catalog.unnest(new.cost_evidence_ids) as e(id)
     where not exists (select 1 from app.cost_evidence c where c.workspace_id = new.workspace_id and c.id = e.id)
  ) then
    raise exception using errcode = 'SV003', message = 'valuation references unknown cost evidence for this workspace';
  end if;
  if new.tax_rule_set_id is not null then
    select t.status, t.is_fixture into tax_status, tax_fixture
      from app.tax_rule_sets t
     where t.workspace_id = new.workspace_id and t.id = new.tax_rule_set_id;
  end if;
  if not new.is_fixture then
    if coalesce(tax_fixture, false)
       or exists (select 1 from app.cost_profiles p
                   where p.workspace_id = new.workspace_id and p.id = new.cost_profile_id and p.is_fixture)
       or exists (select 1 from app.fx_rates r
                   where r.workspace_id = new.workspace_id and r.id = any (new.fx_rate_ids) and r.is_fixture)
       or exists (select 1 from app.cost_evidence c
                   where c.workspace_id = new.workspace_id and c.id = any (new.cost_evidence_ids) and c.is_fixture)
       or exists (select 1 from app.comparable_sets s
                   where s.workspace_id = new.workspace_id and s.id = new.comparable_set_id and s.is_fixture) then
      raise exception using
        errcode = 'SV003',
        message = 'a non-fixture valuation cannot depend on fixture data';
    end if;
    if new.state in ('estimated', 'quote_supported')
       and (tax_status is null or tax_status not in ('approved', 'active')) then
      raise exception using
        errcode = 'SV003',
        message = 'an estimated valuation requires an approved or active tax rule set; use state incomplete';
    end if;
  end if;
  return new;
end
$$;

-- State may only move forward to stale or invalid; invalid is terminal.
create or replace function app.valuations_guard_state()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.state is distinct from old.state
     and (old.state = 'invalid' or new.state not in ('stale', 'invalid')) then
    raise exception using
      errcode = 'SV002',
      message = pg_catalog.format('valuation state %s -> %s is not permitted', old.state, new.state);
  end if;
  return new;
end
$$;

create trigger valuations_dependencies before insert on app.valuations
  for each row execute function app.valuations_check_dependencies();
create trigger valuations_frozen before update on app.valuations
  for each row execute function app.guard_frozen_columns('state', 'stale_at', 'stale_reason');
create trigger valuations_state before update on app.valuations
  for each row execute function app.valuations_guard_state();

call ops.apply_security_baseline();
