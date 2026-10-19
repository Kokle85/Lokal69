-- =============================================================================
-- 20261006000400_listings
-- Source listings, detail observations, immutable revisions, aliases, search
-- card observations, possible-same-vehicle clusters and field evidence.
-- Spec sections 7, 10 and 11.
--
-- Revision semantics (spec 10):
--   * only a changed semantic payload creates a new listing_revisions row;
--   * A -> B -> A is a new chronological revision, so there is deliberately NO
--     unique constraint on (listing_id, semantic_hash);
--   * every accepted detail parse (including late/older generations) is kept
--     in detail_observations; promotion of current facts is a separate step
--     serialized under the listing row lock and may never regress.
-- Mileage < 200,000 km is NOT a table constraint: rejected observations are
-- retained for audit and to avoid rediscovering them (spec 11).
-- =============================================================================

create table app.listings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  source_listing_id text not null,
  incarnation integer not null default 1,
  canonical_url text not null,
  identity_method text not null,
  identity_material text not null,
  identity_hash text not null,
  identity_confidence text not null,
  identity_conflict boolean not null default false,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  last_detail_success_at timestamptz,
  last_availability_check_at timestamptz,
  availability text not null default 'unknown',
  current_revision_id uuid,
  -- Monotonic allocator: the last generation handed to a fresh detail/recheck job.
  detail_generation bigint not null default 0,
  -- (current_generation, current_observation_id) of the promoted observation.
  current_generation bigint,
  current_observation_id uuid,
  eligibility_state text,
  eligibility_profile text,
  screening jsonb,
  screening_version text,
  screened_at timestamptz,
  quarantined boolean not null default false,
  quarantine_reason text,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint listings_workspace_id_uk unique (workspace_id, id),
  constraint listings_source_id_uk unique (workspace_id, source_id, id),
  constraint listings_identity_uk unique (workspace_id, source_id, source_listing_id, incarnation),
  -- A hash collision with different material fails here; the caller compares the
  -- canonical material, quarantines the mismatch and alerts (never merges).
  constraint listings_identity_hash_uk unique (workspace_id, source_id, identity_hash, incarnation),
  constraint listings_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint listings_source_listing_id_ck check (pg_catalog.length(source_listing_id) between 1 and 200),
  constraint listings_incarnation_ck check (incarnation > 0),
  constraint listings_canonical_url_ck check (
    pg_catalog.length(canonical_url) between 8 and 2048 and canonical_url ~* '^https?://'),
  constraint listings_identity_method_ck check (identity_method in ('provider_id', 'canonical_url')),
  constraint listings_identity_material_ck check (pg_catalog.length(identity_material) between 1 and 2048),
  constraint listings_identity_hash_ck check (identity_hash ~ '^[0-9a-f]{64}$'),
  constraint listings_identity_confidence_ck check (identity_confidence in ('high', 'medium', 'low')),
  constraint listings_seen_ck check (last_seen_at >= first_seen_at),
  constraint listings_availability_ck check (availability in (
    'available', 'reserved', 'removed', 'sold_claimed', 'unknown')),
  constraint listings_generation_ck check (
    detail_generation >= 0
    and (current_generation is null or (current_generation >= 1 and current_generation <= detail_generation))),
  constraint listings_current_observation_ck check ((current_generation is null) = (current_observation_id is null)),
  constraint listings_eligibility_state_ck check (eligibility_state is null or eligibility_state in (
    'eligible_primary', 'eligible_manual_profile', 'needs_facts', 'rejected')),
  constraint listings_eligibility_profile_ck check (eligibility_profile is null or eligibility_profile in (
    'primary', 'manual_4000', 'below_target_watch')),
  constraint listings_screening_ck check (
    eligibility_state is null
    or (screening is not null and screening_version is not null and screened_at is not null)),
  constraint listings_screening_shape_ck check (screening is null or pg_catalog.jsonb_typeof(screening) = 'object'),
  constraint listings_screening_version_ck check (screening_version is null or pg_catalog.length(screening_version) <= 80),
  constraint listings_quarantine_ck check (not quarantined or quarantine_reason is not null),
  constraint listings_quarantine_reason_ck check (quarantine_reason is null or pg_catalog.length(quarantine_reason) <= 500),
  constraint listings_row_version_ck check (row_version > 0)
);
create index listing_recent_idx on app.listings (workspace_id, last_seen_at desc, id);

-- Every accepted detail parse, including late completions of older generations
-- (historical evidence, promoted = false). observation_id is the durable
-- accepted-observation identity (tie-breaker within a generation); a replay of
-- the same (generation, observation_id) is idempotent and a conflicting replay
-- payload is an incident for the application to raise.
create table app.detail_observations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  generation bigint not null,
  observation_id uuid not null,
  job_id uuid,
  fetch_attempt_id uuid,
  snapshot_id uuid,
  semantic_hash text not null,
  raw_content_hash text,
  normalized jsonb not null,
  provenance jsonb not null,
  parser_version text not null,
  crawler_version text,
  page_type text not null default 'detail',
  availability text not null default 'unknown',
  observed_at timestamptz not null,
  ingested_at timestamptz not null default now(),
  promoted boolean not null default false,
  not_promoted_reason text,
  quarantined boolean not null default false,
  created_at timestamptz not null default now(),
  constraint detail_observations_workspace_id_uk unique (workspace_id, id),
  constraint detail_observations_identity_uk unique (workspace_id, listing_id, generation, observation_id),
  constraint detail_observations_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint detail_observations_job_fk foreign key (workspace_id, job_id)
    references ops.jobs (workspace_id, id),
  constraint detail_observations_fetch_fk foreign key (workspace_id, fetch_attempt_id)
    references ops.fetch_attempts (workspace_id, id),
  constraint detail_observations_snapshot_fk foreign key (workspace_id, snapshot_id)
    references ops.source_snapshots (workspace_id, id),
  constraint detail_observations_generation_ck check (generation >= 1),
  constraint detail_observations_semantic_hash_ck check (semantic_hash ~ '^[0-9a-f]{64}$'),
  constraint detail_observations_raw_hash_ck check (raw_content_hash is null or raw_content_hash ~ '^[0-9a-f]{64}$'),
  constraint detail_observations_normalized_ck check (pg_catalog.jsonb_typeof(normalized) = 'object'),
  constraint detail_observations_provenance_ck check (pg_catalog.jsonb_typeof(provenance) = 'object'),
  constraint detail_observations_parser_ck check (pg_catalog.length(parser_version) between 1 and 120),
  constraint detail_observations_crawler_ck check (crawler_version is null or pg_catalog.length(crawler_version) <= 80),
  constraint detail_observations_page_type_ck check (page_type in (
    'detail', 'search', 'removed', 'challenge', 'login', 'paywall', 'empty_shell', 'unknown')),
  constraint detail_observations_availability_ck check (availability in (
    'available', 'reserved', 'removed', 'sold_claimed', 'unknown')),
  constraint detail_observations_promotion_ck check (
    (promoted and not_promoted_reason is null)
    or (not promoted and (not_promoted_reason is null or pg_catalog.length(not_promoted_reason) <= 200)))
);
create index detail_observations_observed_idx on app.detail_observations (workspace_id, listing_id, observed_at desc);

create table app.listing_revisions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  revision_number integer not null,
  observed_at timestamptz not null,
  semantic_hash text not null,
  asking_minor bigint,
  currency char(3),
  price_basis text not null default 'unknown',
  price_type text not null default 'unknown',
  mileage_km numeric(14, 6),
  availability text not null default 'unknown',
  -- Typed, query-critical copies of normalized fields (filters and comparables).
  seller_country char(2),
  make text,
  model text,
  vehicle_generation text,
  registration_year smallint,
  registration_month smallint,
  fuel text not null default 'unknown',
  gearbox text not null default 'unknown',
  drive text not null default 'unknown',
  body_type text not null default 'unknown',
  normalized jsonb not null,
  provenance jsonb not null,
  parser_version text not null,
  detail_generation bigint,
  observation_id uuid,
  quarantined boolean not null default false,
  created_at timestamptz not null default now(),
  constraint listing_revisions_workspace_id_uk unique (workspace_id, id),
  constraint listing_revisions_listing_id_uk unique (workspace_id, listing_id, id),
  constraint listing_revisions_number_uk unique (workspace_id, listing_id, revision_number),
  constraint listing_revisions_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint listing_revisions_observation_fk foreign key (workspace_id, listing_id, detail_generation, observation_id)
    references app.detail_observations (workspace_id, listing_id, generation, observation_id),
  constraint listing_revisions_number_ck check (revision_number > 0),
  constraint listing_revisions_semantic_hash_ck check (semantic_hash ~ '^[0-9a-f]{64}$'),
  constraint listing_revisions_asking_ck check (asking_minor is null or asking_minor >= 0),
  constraint listing_revisions_currency_ck check (currency is null or currency ~ '^[A-Z]{3}$'),
  constraint listing_revisions_currency_pair_ck check ((asking_minor is null) = (currency is null)),
  constraint listing_revisions_basis_ck check (price_basis in ('gross', 'net', 'unknown')),
  constraint listing_revisions_price_type_ck check (price_type in (
    'full_vehicle_asking', 'instalment', 'leasing', 'deposit', 'auction_start', 'auction_current_bid',
    'export_net', 'parts_or_damaged', 'price_on_request', 'unknown')),
  constraint listing_revisions_mileage_ck check (mileage_km is null or mileage_km >= 0),
  constraint listing_revisions_availability_ck check (availability in (
    'available', 'reserved', 'removed', 'sold_claimed', 'unknown')),
  constraint listing_revisions_country_ck check (seller_country is null or seller_country ~ '^[A-Z]{2}$'),
  constraint listing_revisions_vehicle_text_ck check (
    (make is null or pg_catalog.length(make) <= 80)
    and (model is null or pg_catalog.length(model) <= 120)
    and (vehicle_generation is null or pg_catalog.length(vehicle_generation) <= 80)),
  constraint listing_revisions_registration_ck check (
    (registration_year is null or registration_year between 1950 and 2100)
    and (registration_month is null or (registration_month between 1 and 12 and registration_year is not null))),
  constraint listing_revisions_fuel_ck check (fuel in (
    'diesel', 'petrol', 'hybrid_petrol', 'hybrid_diesel', 'plugin_hybrid', 'lpg', 'cng', 'electric',
    'other', 'unknown')),
  constraint listing_revisions_gearbox_ck check (gearbox in ('manual', 'automatic', 'semi_automatic', 'unknown')),
  constraint listing_revisions_drive_ck check (drive in ('fwd', 'rwd', 'awd', '4wd', 'unknown')),
  constraint listing_revisions_body_type_ck check (body_type in (
    'suv', 'offroad', 'crossover', 'pickup', 'estate', 'sedan', 'hatchback', 'van', 'other', 'unknown')),
  constraint listing_revisions_normalized_ck check (pg_catalog.jsonb_typeof(normalized) = 'object'),
  constraint listing_revisions_provenance_ck check (pg_catalog.jsonb_typeof(provenance) = 'object'),
  constraint listing_revisions_parser_ck check (pg_catalog.length(parser_version) between 1 and 120),
  constraint listing_revisions_observation_pair_ck check ((detail_generation is null) = (observation_id is null))
);
-- (workspace_id, listing_id, revision_number desc) lookups are served by the
-- listing_revisions_number_uk index scanned backwards.

-- Current-revision and current-observation pointers must belong to the same
-- listing. Deferred so a revision and its promotion commit together.
alter table app.listings
  add constraint current_revision_belongs_to_listing
  foreign key (workspace_id, id, current_revision_id)
  references app.listing_revisions (workspace_id, listing_id, id)
  deferrable initially deferred;
alter table app.listings
  add constraint current_observation_belongs_to_listing
  foreign key (workspace_id, id, current_generation, current_observation_id)
  references app.detail_observations (workspace_id, listing_id, generation, observation_id)
  deferrable initially deferred;

alter table ops.jobs
  add constraint jobs_listing_fk foreign key (workspace_id, listing_id)
  references app.listings (workspace_id, id);

-- Aliases link a changed/alternative URL of the same source listing (with evidence).
create table app.listing_aliases (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  listing_id uuid not null,
  alias_url text not null,
  alias_hash text not null,
  reason text not null,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint listing_aliases_workspace_id_uk unique (workspace_id, id),
  constraint listing_aliases_source_alias_uk unique (workspace_id, source_id, alias_hash),
  constraint listing_aliases_listing_fk foreign key (workspace_id, source_id, listing_id)
    references app.listings (workspace_id, source_id, id),
  constraint listing_aliases_url_ck check (
    pg_catalog.length(alias_url) between 8 and 2048 and alias_url ~* '^https?://'),
  constraint listing_aliases_hash_ck check (alias_hash ~ '^[0-9a-f]{64}$'),
  constraint listing_aliases_reason_ck check (pg_catalog.length(reason) between 3 and 500),
  constraint listing_aliases_evidence_ck check (pg_catalog.jsonb_typeof(evidence) = 'object')
);
create index listing_aliases_listing_idx on app.listing_aliases (workspace_id, listing_id);

-- Search-card observations. ingestion_key (e.g. hash of source, run, page,
-- source_listing_id, card_hash) prevents replaying the same observation twice.
create table app.listing_observations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  listing_id uuid not null,
  crawl_run_id uuid not null,
  job_id uuid,
  page_number integer not null default 1,
  position integer not null,
  source_listing_id text not null,
  card_hash text not null,
  card_material jsonb not null,
  card_price_minor bigint,
  card_currency char(3),
  card_mileage_km numeric(14, 6),
  observed_at timestamptz not null,
  source_published_at timestamptz,
  source_modified_at timestamptz,
  ingestion_key text not null,
  created_at timestamptz not null default now(),
  constraint listing_observations_workspace_id_uk unique (workspace_id, id),
  constraint listing_observations_ingestion_uk unique (workspace_id, ingestion_key),
  constraint listing_observations_listing_fk foreign key (workspace_id, source_id, listing_id)
    references app.listings (workspace_id, source_id, id),
  constraint listing_observations_run_fk foreign key (workspace_id, source_id, crawl_run_id)
    references ops.crawl_runs (workspace_id, source_id, id),
  constraint listing_observations_job_fk foreign key (workspace_id, job_id)
    references ops.jobs (workspace_id, id),
  constraint listing_observations_page_ck check (page_number >= 1 and position >= 0),
  constraint listing_observations_source_listing_id_ck check (pg_catalog.length(source_listing_id) between 1 and 200),
  constraint listing_observations_card_hash_ck check (card_hash ~ '^[0-9a-f]{64}$'),
  constraint listing_observations_material_ck check (pg_catalog.jsonb_typeof(card_material) = 'object'),
  constraint listing_observations_price_ck check (card_price_minor is null or card_price_minor >= 0),
  constraint listing_observations_currency_ck check (card_currency is null or card_currency ~ '^[A-Z]{3}$'),
  constraint listing_observations_currency_pair_ck check ((card_price_minor is null) = (card_currency is null)),
  constraint listing_observations_mileage_ck check (card_mileage_km is null or card_mileage_km >= 0),
  constraint listing_observations_ingestion_key_ck check (pg_catalog.length(ingestion_key) between 16 and 200)
);
create index listing_observations_listing_idx on app.listing_observations (workspace_id, listing_id, observed_at desc);
create index listing_observations_run_idx on app.listing_observations (workspace_id, crawl_run_id);

-- Possible-same-vehicle clusters across sources: never merge or delete listings.
create table app.vehicle_clusters (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  confidence text not null,
  review_status text not null default 'unreviewed',
  -- Summary of matching signals; never plate numbers or personal contact data.
  match_basis jsonb not null default '{}'::jsonb,
  reviewed_by uuid,
  reviewed_at timestamptz,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint vehicle_clusters_workspace_id_uk unique (workspace_id, id),
  constraint vehicle_clusters_confidence_ck check (confidence in ('high', 'medium', 'low')),
  constraint vehicle_clusters_review_status_ck check (review_status in ('unreviewed', 'confirmed', 'rejected')),
  constraint vehicle_clusters_reviewed_ck check (
    review_status = 'unreviewed' or (reviewed_by is not null and reviewed_at is not null)),
  constraint vehicle_clusters_basis_ck check (pg_catalog.jsonb_typeof(match_basis) = 'object'),
  constraint vehicle_clusters_row_version_ck check (row_version > 0)
);

create table app.vehicle_cluster_members (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  cluster_id uuid not null,
  listing_id uuid not null,
  evidence jsonb not null default '{}'::jsonb,
  confidence text not null,
  manually_confirmed boolean not null default false,
  confirmed_by uuid,
  confirmed_at timestamptz,
  linked_at timestamptz not null default now(),
  unlinked_at timestamptz,
  unlinked_by uuid,
  unlink_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint vehicle_cluster_members_workspace_id_uk unique (workspace_id, id),
  constraint vehicle_cluster_members_cluster_fk foreign key (workspace_id, cluster_id)
    references app.vehicle_clusters (workspace_id, id),
  constraint vehicle_cluster_members_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint vehicle_cluster_members_evidence_ck check (pg_catalog.jsonb_typeof(evidence) = 'object'),
  constraint vehicle_cluster_members_confidence_ck check (confidence in ('high', 'medium', 'low')),
  constraint vehicle_cluster_members_confirmed_ck check (
    not manually_confirmed or (confirmed_by is not null and confirmed_at is not null)),
  -- False-positive unlinking keeps the row with who/when/why (spec 10).
  constraint vehicle_cluster_members_unlink_ck check (
    unlinked_at is null
    or (unlinked_at >= linked_at and unlinked_by is not null and unlink_reason is not null)),
  constraint vehicle_cluster_members_unlink_reason_ck check (
    unlink_reason is null or pg_catalog.length(unlink_reason) between 3 and 500)
);
create unique index vehicle_cluster_members_active_uidx
  on app.vehicle_cluster_members (workspace_id, cluster_id, listing_id) where unlinked_at is null;
create index vehicle_cluster_members_listing_idx on app.vehicle_cluster_members (workspace_id, listing_id);

-- Field-level evidence (spec 7). Confidence measures extraction reliability;
-- claim_status 'verified' requires an owner verification record.
-- Verification of earlier evidence is a NEW row that supersedes it.
create table app.field_evidence (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  listing_id uuid not null,
  revision_id uuid,
  field_path text not null,
  snapshot_id uuid,
  document_ref text,
  raw_excerpt text,
  method text not null,
  transformation text,
  confidence text not null,
  claim_status text,
  verified_by uuid,
  verified_at timestamptz,
  supersedes_id uuid,
  observed_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint field_evidence_workspace_id_uk unique (workspace_id, id),
  constraint field_evidence_listing_fk foreign key (workspace_id, listing_id)
    references app.listings (workspace_id, id),
  constraint field_evidence_revision_fk foreign key (workspace_id, listing_id, revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint field_evidence_snapshot_fk foreign key (workspace_id, snapshot_id)
    references ops.source_snapshots (workspace_id, id),
  constraint field_evidence_supersedes_fk foreign key (workspace_id, supersedes_id)
    references app.field_evidence (workspace_id, id),
  constraint field_evidence_field_path_ck check (field_path ~ '^[A-Za-z0-9_.\[\]-]{1,200}$'),
  constraint field_evidence_document_ref_ck check (document_ref is null or pg_catalog.length(document_ref) <= 500),
  constraint field_evidence_excerpt_ck check (raw_excerpt is null or pg_catalog.length(raw_excerpt) <= 500),
  constraint field_evidence_method_ck check (method in (
    'json_ld', 'microdata', 'css', 'xpath', 'regex', 'api_field', 'llm_fallback', 'manual', 'derived')),
  constraint field_evidence_transformation_ck check (transformation is null or pg_catalog.length(transformation) <= 200),
  constraint field_evidence_confidence_ck check (confidence in ('high', 'medium', 'low')),
  constraint field_evidence_claim_status_ck check (claim_status is null or claim_status in (
    'verified', 'seller_claimed', 'seller_denied', 'conflicting', 'unknown')),
  constraint field_evidence_verified_pair_ck check ((verified_by is null) = (verified_at is null)),
  constraint field_evidence_verified_claim_ck check (claim_status is distinct from 'verified' or verified_by is not null)
);
create index field_evidence_field_idx on app.field_evidence (workspace_id, listing_id, field_path, observed_at desc);
create index field_evidence_revision_idx on app.field_evidence (workspace_id, revision_id) where revision_id is not null;

-- -----------------------------------------------------------------------------
-- Listing guards: identity is immutable (a relisted/reused ID is a new
-- incarnation), the generation allocator and promoted generation never move
-- backwards, last_seen_at only grows (greatest(existing, observed_at)) and the
-- row version never decreases.
-- -----------------------------------------------------------------------------
create or replace function app.listings_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.workspace_id <> old.workspace_id
     or new.source_id <> old.source_id
     or new.source_listing_id <> old.source_listing_id
     or new.incarnation <> old.incarnation
     or new.identity_method <> old.identity_method
     or new.identity_material <> old.identity_material
     or new.identity_hash <> old.identity_hash then
    raise exception using
      errcode = 'SV004',
      message = 'listing identity is immutable; create a new incarnation instead';
  end if;
  if new.detail_generation < old.detail_generation then
    raise exception using errcode = 'SV005', message = 'listing detail_generation allocator must not decrease';
  end if;
  if old.current_generation is not null
     and (new.current_generation is null or new.current_generation < old.current_generation) then
    raise exception using errcode = 'SV005', message = 'promoted detail generation must not regress';
  end if;
  if new.last_seen_at < old.last_seen_at then
    raise exception using errcode = 'SV005', message = 'listing last_seen_at must not decrease';
  end if;
  if new.row_version < old.row_version then
    raise exception using errcode = 'SV005', message = 'listing row_version must not decrease';
  end if;
  return new;
end
$$;

-- A detail observation may only carry a generation that the listing allocator
-- has already handed out (spec 10: generations are allocated when scheduling).
create or replace function app.detail_observations_check_generation()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  allocated bigint;
begin
  select l.detail_generation
    into allocated
    from app.listings l
   where l.workspace_id = new.workspace_id
     and l.id = new.listing_id;
  if allocated is not null and new.generation > allocated then
    raise exception using
      errcode = 'SV006',
      message = pg_catalog.format('detail generation %s was never allocated (listing allocator is at %s)',
                                  new.generation, allocated);
  end if;
  return new;
end
$$;

create trigger listings_guard before update on app.listings
  for each row execute function app.listings_guard();
create trigger listings_touch before update on app.listings
  for each row execute function app.touch_updated_at();
create trigger detail_observations_generation before insert on app.detail_observations
  for each row execute function app.detail_observations_check_generation();
create trigger vehicle_clusters_touch before update on app.vehicle_clusters
  for each row execute function app.touch_updated_at();
create trigger vehicle_clusters_version_guard before update on app.vehicle_clusters
  for each row execute function app.guard_version('row_version');
create trigger vehicle_cluster_members_touch before update on app.vehicle_cluster_members
  for each row execute function app.touch_updated_at();
-- Membership rows are only confirmed or unlinked, never re-pointed.
create trigger vehicle_cluster_members_frozen before update on app.vehicle_cluster_members
  for each row execute function app.guard_frozen_columns(
    'manually_confirmed', 'confirmed_by', 'confirmed_at', 'unlinked_at', 'unlinked_by', 'unlink_reason',
    'updated_at');

call ops.apply_security_baseline();
