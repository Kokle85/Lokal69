-- =============================================================================
-- 20261006001000_seller_inquiries
-- Specification v1.1 section 37 (37.1-37.9): bounded automatic seller inquiries,
-- seller identity/contact evidence, the send state machine with quota, kill
-- switch and suppression, the local mailbox reply route (worker bindings,
-- checkpoints, ingest dedup, binding sync) and cross-site availability events.
-- docs/decisions/0002-spec-v1.1-adoption.md; docs/schema.md section 10.
--
-- Additive and forward-only (expand): new tables, functions and triggers, plus
-- widened ops.api_credentials scope checks for inquiries:read, inquiries:pause
-- and mail:ingest. Nothing existing is renamed or dropped.
--
-- Database-level guarantees (defence in depth behind domain.inquiries):
--   * one inquiry per (workspace, canonical vehicle, verified seller entity,
--     purpose): unique identity hash AND unique identity components, plus one
--     live inquiry per (qualification listing, seller), and one initial inquiry
--     per ACTUAL vehicle/seller pair across cluster confirmations, seller merges
--     and plausible-but-unresolved cross-site duplicates;
--   * the state machine of spec 37.5 exactly as domain.inquiries.ALLOWED_TRANSITIONS
--     (SV002), with guarded edges: reservation and dispatch re-check the kill
--     switch, mode, authorization version, sender binding, verified recipient
--     and language, suppressions, canonical vehicle identity, seller cooldown,
--     listing facts and quota; an uncertain send can never be re-queued;
--   * the sender/recipient/template/body binding is immutable once reserved (SV004);
--   * at commit, every state carries its evidence (quota debit, committed send
--     intent, acceptance/failure evidence, correlated reply) - SV003;
--   * rate caps (owner-reducible ceilings of 2 per rolling 24 h and 5 per rolling
--     15 days) are enforced transactionally on ops.inquiry_quota_ledger AND before
--     every transmission (a debit counts at max(reservation, send attempt)), so a
--     queued backlog never leaves in a burst; debits of possibly transmitted
--     inquiries are never released; lifecycle timestamps are database-owned;
--   * an uncertain attempt is resolved only by positive evidence (Sent Items or
--     provider hit, a Message-ID-linked inbound message, or a documented
--     pre-submission proof with no live worker and no pending Outbox item);
--   * suppression removal is never automatic and needs a matching audit event;
--   * replies can only be stored for an inquiry that was (possibly) sent, through
--     the active mailbox binding (live credential) of the inquiry's own sender
--     mailbox, under a published, non-tombstoned binding version; an automatic
--     match is linked by Message-ID or provider thread, never by subject alone;
--   * history tables are append-only (SV001); a mail:ingest credential carries
--     no other scope.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Immutable validation helpers (used by CHECK constraints; EXECUTE granted to
-- suv_backend below because CHECK expressions are evaluated as the caller).
-- -----------------------------------------------------------------------------

-- Conservative canonical e-mail address: RFC atext local part as shown (no dot
-- folding, no plus stripping), lower-case ASCII (IDNA) domain with >= 2 labels.
create or replace function app.email_address_ok(addr text)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select addr is not null
     and pg_catalog.length(addr) between 3 and 320
     and addr ~ '^[A-Za-z0-9!#$%&''*+/=?^_`{|}~.-]{1,64}@[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
     and addr !~ '^\.|\.\.|\.@'
$$;
comment on function app.email_address_ok(text) is
  'Canonical address check: local part kept as shown (no provider-specific folding), lower-case IDNA domain.';

-- Normalised RFC 5322 Message-ID: <left@right>, printable ASCII without <, >
-- or a second @, right part lower-cased (domain.replies.normalize_message_id).
create or replace function app.rfc_message_id_ok(mid text)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select mid is not null
     and pg_catalog.length(mid) between 5 and 998
     and mid ~ '^<[\x21-\x3b\x3d\x3f\x41-\x7e]+@[\x21-\x3b\x3d\x3f\x41-\x7e]+>$'
     and pg_catalog.split_part(mid, '@', 2) = pg_catalog.lower(pg_catalog.split_part(mid, '@', 2))
$$;
comment on function app.rfc_message_id_ok(text) is
  'True for a normalised Message-ID <left@right> (printable ASCII, right part lower-case).';

create or replace function app.rfc_message_id_array_ok(arr text[], max_items integer)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select arr is not null
     and pg_catalog.cardinality(arr) <= max_items
     and not exists (
       select 1
         from pg_catalog.unnest(arr) as e(v)
        where not coalesce(app.rfc_message_id_ok(e.v), false)
     )
$$;
comment on function app.rfc_message_id_array_ok(text[], integer) is
  'True when every element is a normalised Message-ID and the array is bounded.';

-- Opaque provider/locator identifier: no whitespace or control characters.
create or replace function app.opaque_ref_ok(ref text, max_len integer)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select ref is not null
     and pg_catalog.length(ref) between 1 and max_len
     and ref !~ '[[:space:][:cntrl:]]'
$$;
comment on function app.opaque_ref_ok(text, integer) is
  'True for a bounded opaque identifier without whitespace or control characters.';

-- Bounded array of sha256 hex digests (e.g. hashed Outlook folder identities).
create or replace function app.hex64_array_ok(arr text[], max_items integer)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select arr is not null
     and pg_catalog.cardinality(arr) <= max_items
     and not exists (
       select 1
         from pg_catalog.unnest(arr) as e(v)
        where e.v is null or e.v !~ '^[0-9a-f]{64}$'
     )
$$;
comment on function app.hex64_array_ok(text[], integer) is
  'True when the array is bounded and every element is a lower-case sha256 hex digest.';

-- Verified sender display name: header-safe (no CR/LF/controls, no address,
-- markup or URL characters), trimmed, at most 64 characters (seller_templates).
create or replace function app.sender_display_name_ok(name text)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select name is not null
     and pg_catalog.length(name) between 1 and 64
     and name = pg_catalog.btrim(name)
     and name !~ '[[:cntrl:]<>"@\\\[\]{}`|;:/]'
     and name !~ '[\u0085\u2028\u2029]'
$$;
comment on function app.sender_display_name_ok(text) is
  'Header-safe verified sender display name (no CR/LF, controls, address, markup or URL characters).';

-- domain.inquiries.InquiryIdentity.key(): sha256 over canonical JSON
-- {"purpose","seller","vehicle","workspace_id"} (sorted keys, compact). The
-- seller is always a persisted seller entity at this layer.
create or replace function app.seller_inquiry_identity_key(
  p_workspace_id uuid, p_vehicle_kind text, p_vehicle_id uuid, p_seller_entity_id uuid, p_purpose text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select pg_catalog.encode(
           pg_catalog.sha256(pg_catalog.convert_to(
             '{"purpose":' || pg_catalog.to_json(p_purpose)::text
             || ',"seller":' || pg_catalog.to_json('seller_entity:' || p_seller_entity_id::text)::text
             || ',"vehicle":' || pg_catalog.to_json(p_vehicle_kind || ':' || p_vehicle_id::text)::text
             || ',"workspace_id":' || pg_catalog.to_json(p_workspace_id::text)::text
             || '}', 'UTF8')),
           'hex')
$$;
comment on function app.seller_inquiry_identity_key(uuid, text, uuid, uuid, text) is
  'Inquiry identity hash, identical to domain.inquiries.InquiryIdentity.key().';

-- domain.seller_templates.message_body_hash(): sha256 over canonical JSON
-- {"body","subject"} of the NFC, LF-normalised subject and body.
create or replace function app.message_body_hash(p_subject text, p_body text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select pg_catalog.encode(
           pg_catalog.sha256(pg_catalog.convert_to(
             '{"body":' || pg_catalog.to_json(pg_catalog.normalize(
                 pg_catalog.replace(pg_catalog.replace(p_body, E'\r\n', E'\n'), E'\r', E'\n'), 'NFC'))::text
             || ',"subject":' || pg_catalog.to_json(pg_catalog.normalize(p_subject, 'NFC'))::text
             || '}', 'UTF8')),
           'hex')
$$;
comment on function app.message_body_hash(text, text) is
  'Message hash, identical to domain.seller_templates.message_body_hash(subject, body).';

-- Safe attachment METADATA only (spec 37.8): at most 20 entries of filename,
-- MIME type, byte count, sha256 and an opaque local reference (plus the policy
-- decision). No bytes, URLs, paths or traversal.
create or replace function app.reply_attachments_ok(arr jsonb)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select arr is not null
     and pg_catalog.jsonb_typeof(arr) = 'array'
     and pg_catalog.jsonb_array_length(arr) <= 20
     and not exists (
       select 1
         from pg_catalog.jsonb_array_elements(arr) as e(v)
        where pg_catalog.jsonb_typeof(e.v) <> 'object'
           or exists (
                select 1
                  from pg_catalog.jsonb_object_keys(e.v) as k(name)
                 where k.name not in ('filename', 'mime_type', 'byte_size', 'sha256', 'local_ref',
                                      'action', 'document_kind', 'reasons'))
           or pg_catalog.jsonb_typeof(e.v -> 'filename') is distinct from 'string'
           or pg_catalog.length(e.v ->> 'filename') not between 1 and 255
           or (e.v ->> 'filename') ~ '[[:cntrl:]/\\]'
           or (e.v ->> 'filename') in ('.', '..')
           or (e.v ->> 'filename') like '..%'
           or pg_catalog.jsonb_typeof(e.v -> 'mime_type') is distinct from 'string'
           or (e.v ->> 'mime_type') !~ '^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$'
           or pg_catalog.jsonb_typeof(e.v -> 'byte_size') is distinct from 'number'
           or (e.v ->> 'byte_size') !~ '^[0-9]{1,13}$'
           or pg_catalog.jsonb_typeof(e.v -> 'sha256') is distinct from 'string'
           or (e.v ->> 'sha256') !~ '^[0-9a-f]{64}$'
           or (pg_catalog.jsonb_typeof(e.v -> 'local_ref') is not null
               and pg_catalog.jsonb_typeof(e.v -> 'local_ref') <> 'null'
               and (pg_catalog.jsonb_typeof(e.v -> 'local_ref') <> 'string'
                    or (e.v ->> 'local_ref') !~ '^[A-Za-z0-9][A-Za-z0-9._:=-]{0,255}$'
                    or (e.v ->> 'local_ref') like '%..%'
                    or (e.v ->> 'local_ref') like '%://%'))
           or (pg_catalog.jsonb_typeof(e.v -> 'action') is not null
               and (e.v ->> 'action') is distinct from 'allow_vehicle_document'
               and (e.v ->> 'action') is distinct from 'quarantine_sensitive'
               and (e.v ->> 'action') is distinct from 'reject')
           or (pg_catalog.jsonb_typeof(e.v -> 'document_kind') is not null
               and pg_catalog.jsonb_typeof(e.v -> 'document_kind') <> 'null'
               and (e.v ->> 'document_kind') !~ '^[a-z][a-z0-9_]{0,39}$')
           or (pg_catalog.jsonb_typeof(e.v -> 'reasons') is not null
               and (pg_catalog.jsonb_typeof(e.v -> 'reasons') <> 'array'
                    or pg_catalog.jsonb_array_length(e.v -> 'reasons') > 20
                    or exists (
                         select 1
                           from pg_catalog.jsonb_array_elements(e.v -> 'reasons') as r(code)
                          where pg_catalog.jsonb_typeof(r.code) <> 'string'
                             or (r.code #>> '{}') !~ '^[A-Za-z0-9_.:-]{1,80}$')))
     )
$$;
comment on function app.reply_attachments_ok(jsonb) is
  'Attachment metadata shape (spec 37.8): <= 20 entries, safe filename/MIME/size/sha256/opaque local ref; no URLs or paths.';

-- =============================================================================
-- Seller identity, contact evidence, authorization and workspace controls
-- =============================================================================

-- A seller across sites (spec 37.5/37.8). Aliases carry the per-site evidence.
-- A merge points the absorbed entity at the surviving root (one level only).
create table app.seller_entities (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  seller_type text not null default 'unknown',
  -- Untrusted business name as shown by the seller; never used as an identifier.
  display_name text,
  evidence jsonb not null default '{}'::jsonb,
  verified_at timestamptz,
  merged_into_id uuid,
  merged_at timestamptz,
  merge_reason text,
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_entities_workspace_id_uk unique (workspace_id, id),
  constraint seller_entities_merged_into_fk foreign key (workspace_id, merged_into_id)
    references app.seller_entities (workspace_id, id),
  constraint seller_entities_type_ck check (seller_type in ('dealer', 'private', 'unknown')),
  constraint seller_entities_display_name_ck check (
    display_name is null
    or (pg_catalog.length(display_name) between 1 and 200 and display_name !~ '[[:cntrl:]]')),
  constraint seller_entities_evidence_ck check (
    pg_catalog.jsonb_typeof(evidence) = 'object' and pg_catalog.octet_length(evidence::text) <= 16384),
  constraint seller_entities_merge_ck check (
    (merged_into_id is null and merged_at is null and merge_reason is null)
    or (merged_into_id is not null and merged_into_id <> id and merged_at is not null
        and merge_reason is not null)),
  constraint seller_entities_merge_reason_ck check (
    merge_reason is null or pg_catalog.length(merge_reason) between 3 and 500),
  constraint seller_entities_row_version_ck check (row_version > 0)
);
create index seller_entities_merged_into_idx
  on app.seller_entities (workspace_id, merged_into_id) where merged_into_id is not null;

-- One evidenced appearance of a seller (marketplace seller id on one site, or a
-- site-free legal-entity id, VAT id or dealer website). A false-positive link is
-- unlinked (row kept), never deleted or re-pointed.
create table app.seller_entity_aliases (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  seller_entity_id uuid not null,
  source_id uuid,
  alias_kind text not null,
  reference text not null,
  -- sha256 of domain.seller_contacts.SellerAlias.alias_key() (kind:site:reference).
  alias_key_hash text not null,
  display_name text,
  evidence_kind text not null,
  evidence_excerpt text,
  source_url text,
  evidence jsonb not null default '{}'::jsonb,
  observed_at timestamptz not null,
  verified_at timestamptz,
  unlinked_at timestamptz,
  unlinked_by uuid,
  unlink_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_entity_aliases_workspace_id_uk unique (workspace_id, id),
  constraint seller_entity_aliases_entity_fk foreign key (workspace_id, seller_entity_id)
    references app.seller_entities (workspace_id, id),
  constraint seller_entity_aliases_source_fk foreign key (workspace_id, source_id)
    references app.sources (workspace_id, id),
  constraint seller_entity_aliases_kind_ck check (alias_kind in (
    'marketplace_seller_id', 'dealer_website_domain', 'legal_entity_id', 'vat_id')),
  constraint seller_entity_aliases_site_ck check (alias_kind <> 'marketplace_seller_id' or source_id is not null),
  constraint seller_entity_aliases_reference_ck check (
    pg_catalog.length(reference) between 1 and 200 and reference = pg_catalog.btrim(reference)
    and reference !~ '[[:cntrl:]]'
    and (alias_kind <> 'dealer_website_domain' or reference ~ '^[a-z0-9.-]+\.[a-z0-9-]+$')
    and (alias_kind not in ('vat_id', 'legal_entity_id')
         or (reference = pg_catalog.upper(reference) and reference !~ '[[:space:]./-]'))),
  constraint seller_entity_aliases_key_hash_ck check (alias_key_hash ~ '^[0-9a-f]{64}$'),
  constraint seller_entity_aliases_display_name_ck check (
    display_name is null or (pg_catalog.length(display_name) <= 200 and display_name !~ '[[:cntrl:]]')),
  constraint seller_entity_aliases_evidence_kind_ck check (evidence_kind in (
    'listing_seller_block', 'dealer_profile_link', 'same_legal_entity_id', 'same_vat_id',
    'same_dealer_website', 'manual_review')),
  constraint seller_entity_aliases_excerpt_ck check (evidence_excerpt is null or pg_catalog.length(evidence_excerpt) <= 500),
  constraint seller_entity_aliases_source_url_ck check (
    source_url is null
    or (pg_catalog.length(source_url) between 8 and 2048 and source_url ~* '^https?://[^[:space:]]+$')),
  constraint seller_entity_aliases_evidence_ck check (
    pg_catalog.jsonb_typeof(evidence) = 'object' and pg_catalog.octet_length(evidence::text) <= 16384),
  constraint seller_entity_aliases_unlink_ck check (
    (unlinked_at is null and unlinked_by is null and unlink_reason is null)
    or (unlinked_at is not null and unlinked_by is not null and unlink_reason is not null
        and pg_catalog.length(unlink_reason) between 3 and 500))
);
-- An alias identifies exactly one seller entity at a time within a workspace.
create unique index seller_entity_aliases_active_uidx
  on app.seller_entity_aliases (workspace_id, alias_key_hash) where unlinked_at is null;
create index seller_entity_aliases_entity_idx on app.seller_entity_aliases (workspace_id, seller_entity_id);
create index seller_entity_aliases_source_idx
  on app.seller_entity_aliases (workspace_id, source_id) where source_id is not null;

-- Exact-listing recipient evidence (spec 37.3). One verified recipient per
-- listing (no "contact every branch"); the inquiry references it through
-- composite keys that also pin the listing and the seller entity.
create table app.seller_contacts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  listing_id uuid not null,
  listing_revision_id uuid,
  listing_revision_number integer not null,
  seller_entity_id uuid not null,
  -- Canonical address: local part exactly as shown, lower-case domain. NULL when
  -- no e-mail is available (contact form, reveal restriction, none shown).
  address text,
  address_domain text generated always as (pg_catalog.split_part(address, '@', 2)) stored,
  contact_kind text,
  evidence_kind text not null,
  relay_listing_reference text,
  listing_reference text not null,
  listing_url text not null,
  evidence_url text,
  extraction_location text not null,
  extraction_excerpt text,
  language_code text,
  language_status text not null default 'language_unresolved',
  language_basis text not null default 'none',
  language_confidence numeric(3, 2) not null default 0,
  language_evidence_excerpt text,
  language_rules_version text,
  status text not null default 'unverified',
  status_reasons text[] not null default '{}',
  rules_version text not null,
  observed_at timestamptz not null,
  verified_at timestamptz,
  last_rechecked_at timestamptz,
  changed_at timestamptz,
  superseded_by_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_contacts_workspace_id_uk unique (workspace_id, id),
  constraint seller_contacts_listing_id_uk unique (workspace_id, listing_id, id),
  constraint seller_contacts_seller_id_uk unique (workspace_id, seller_entity_id, id),
  constraint seller_contacts_listing_fk foreign key (workspace_id, source_id, listing_id)
    references app.listings (workspace_id, source_id, id),
  constraint seller_contacts_revision_fk foreign key (workspace_id, listing_id, listing_revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint seller_contacts_entity_fk foreign key (workspace_id, seller_entity_id)
    references app.seller_entities (workspace_id, id),
  constraint seller_contacts_superseded_fk foreign key (workspace_id, superseded_by_id)
    references app.seller_contacts (workspace_id, id),
  constraint seller_contacts_revision_number_ck check (listing_revision_number >= 0),
  constraint seller_contacts_address_ck check (address is null or app.email_address_ok(address)),
  constraint seller_contacts_contact_kind_ck check (contact_kind is null or contact_kind in (
    'ad_email', 'marketplace_relay', 'official_dealer_contact')),
  constraint seller_contacts_evidence_kind_ck check (evidence_kind in (
    'email_on_advertisement', 'marketplace_relay_for_listing', 'official_dealer_contact_via_listing',
    'guessed_address', 'generic_search_result', 'unrelated_harvested', 'contact_form_only',
    'contact_reveal_restricted', 'no_email_found')),
  -- contact_kind names the accepted evidence kind; any other evidence has none.
  constraint seller_contacts_kind_mapping_ck check (
    (evidence_kind = 'email_on_advertisement' and contact_kind is not distinct from 'ad_email')
    or (evidence_kind = 'marketplace_relay_for_listing' and contact_kind is not distinct from 'marketplace_relay')
    or (evidence_kind = 'official_dealer_contact_via_listing'
        and contact_kind is not distinct from 'official_dealer_contact')
    or (evidence_kind not in ('email_on_advertisement', 'marketplace_relay_for_listing',
                              'official_dealer_contact_via_listing') and contact_kind is null)),
  constraint seller_contacts_relay_ck check (
    contact_kind is distinct from 'marketplace_relay' or relay_listing_reference is not null),
  constraint seller_contacts_relay_reference_ck check (
    relay_listing_reference is null
    or (pg_catalog.length(relay_listing_reference) between 1 and 200
        and relay_listing_reference !~ '[[:cntrl:]]')),
  constraint seller_contacts_listing_reference_ck check (
    pg_catalog.length(listing_reference) between 1 and 200 and listing_reference !~ '[[:cntrl:]]'),
  constraint seller_contacts_listing_url_ck check (
    pg_catalog.length(listing_url) between 8 and 2048 and listing_url ~* '^https?://[^[:space:]]+$'),
  constraint seller_contacts_evidence_url_ck check (
    evidence_url is null
    or (pg_catalog.length(evidence_url) between 8 and 2048 and evidence_url ~* '^https?://[^[:space:]]+$')),
  constraint seller_contacts_location_ck check (extraction_location in (
    'listing_contact_block', 'listing_description', 'listing_relay_contact',
    'dealer_page_linked_from_listing', 'marketplace_dealer_profile', 'other')),
  constraint seller_contacts_excerpt_ck check (extraction_excerpt is null or pg_catalog.length(extraction_excerpt) <= 500),
  constraint seller_contacts_language_code_ck check (language_code is null or language_code ~ '^[a-z]{2}$'),
  constraint seller_contacts_language_status_ck check (language_status in (
    'resolved', 'language_unresolved', 'unsupported_language')),
  constraint seller_contacts_language_basis_ck check (language_basis in (
    'verified_seller_preference', 'seller_ad_text', 'none')),
  -- A resolved language is a supported template language with an evidence basis
  -- (English only with positive evidence; country/navigation language never decide).
  constraint seller_contacts_language_resolved_ck check (
    language_status <> 'resolved'
    or (language_code is not null and language_code in ('de', 'it', 'fr', 'en') and language_basis <> 'none')),
  constraint seller_contacts_language_unsupported_ck check (
    language_status <> 'unsupported_language'
    or (language_code is not null and language_code not in ('de', 'it', 'fr', 'en'))),
  constraint seller_contacts_language_confidence_ck check (language_confidence between 0 and 1),
  constraint seller_contacts_language_excerpt_ck check (
    language_evidence_excerpt is null or pg_catalog.length(language_evidence_excerpt) <= 500),
  constraint seller_contacts_language_rules_ck check (
    language_rules_version is null or pg_catalog.length(language_rules_version) between 1 and 80),
  constraint seller_contacts_status_ck check (status in ('verified', 'unverified', 'unavailable', 'changed')),
  constraint seller_contacts_status_reasons_ck check (app.text_array_ok(status_reasons, 30, 80)),
  constraint seller_contacts_rules_version_ck check (pg_catalog.length(rules_version) between 1 and 80),
  -- Verified = an accepted evidence kind for this exact listing, with an address and a time.
  constraint seller_contacts_verified_ck check (
    status <> 'verified'
    or (address is not null and contact_kind is not null and verified_at is not null)),
  -- Where the verified address was found must fit its kind (domain.seller_contacts.verify_recipient):
  -- an ad e-mail on the advertisement itself, a relay bound to exactly this listing reference, an
  -- official dealer contact on a page reached through the listing. Never "other".
  constraint seller_contacts_verified_location_ck check (
    status <> 'verified'
    or (contact_kind = 'ad_email' and extraction_location in ('listing_contact_block', 'listing_description'))
    or (contact_kind = 'marketplace_relay' and extraction_location = 'listing_relay_contact'
        and pg_catalog.btrim(relay_listing_reference) = pg_catalog.btrim(listing_reference))
    or (contact_kind = 'official_dealer_contact'
        and extraction_location in ('dealer_page_linked_from_listing', 'marketplace_dealer_profile'))),
  constraint seller_contacts_unavailable_ck check (
    status <> 'unavailable'
    or (address is null and evidence_kind in ('contact_form_only', 'contact_reveal_restricted', 'no_email_found'))),
  constraint seller_contacts_no_address_kinds_ck check (
    evidence_kind not in ('contact_form_only', 'contact_reveal_restricted', 'no_email_found') or address is null),
  constraint seller_contacts_changed_ck check ((status = 'changed') = (changed_at is not null)),
  constraint seller_contacts_superseded_ck check (superseded_by_id is null or (status = 'changed' and superseded_by_id <> id))
);
-- One verified recipient per listing (never several branches/addresses).
create unique index seller_contacts_verified_uidx on app.seller_contacts (workspace_id, listing_id) where status = 'verified';
create index seller_contacts_listing_idx on app.seller_contacts (workspace_id, listing_id, source_id);
create index seller_contacts_entity_idx on app.seller_contacts (workspace_id, seller_entity_id);
create index seller_contacts_address_idx
  on app.seller_contacts (workspace_id, pg_catalog.lower(address)) where address is not null;
create index seller_contacts_revision_idx
  on app.seller_contacts (workspace_id, listing_id, listing_revision_id) where listing_revision_id is not null;
create index seller_contacts_superseded_idx
  on app.seller_contacts (workspace_id, superseded_by_id) where superseded_by_id is not null;

-- Versioned record of the owner's bounded standing authorization (spec 37.1).
-- Application audit only; immutable rows. A change or revocation is a new
-- version. Mirrors domain.inquiries.SellerInquiryAuthorization.
create table app.seller_inquiry_authorizations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  version integer not null,
  owner_label text not null,
  owner_principal_id uuid,
  effective_date date not null,
  recorded_at date not null,
  source text not null,
  approval_mode text not null default 'no_message_approval',
  purpose text not null default 'initial_availability_documents_price',
  questions text[] not null default array['availability', 'vehicle_documents', 'lowest_final_price']::text[],
  recipient_class text not null default 'verified_seller_of_exact_listing',
  max_inquiries_per_vehicle_seller_pair integer not null default 1,
  scope_version integer not null default 1,
  languages text[] not null,
  english_requires_positive_evidence boolean not null default true,
  allowed_outgoing_data_categories text[] not null,
  excluded_data_categories text[] not null,
  attachments_allowed boolean not null default false,
  cc_bcc_allowed boolean not null default false,
  additional_recipients_allowed boolean not null default false,
  follow_ups_allowed boolean not null default false,
  not_authorized text[] not null,
  profiles_in_scope text[] not null,
  revoked_at timestamptz,
  revoked_by text,
  revoke_reason text,
  record jsonb not null,
  record_hash text not null,
  created_by_principal_id uuid,
  created_at timestamptz not null default now(),
  constraint seller_inquiry_authorizations_workspace_id_uk unique (workspace_id, id),
  constraint seller_inquiry_authorizations_version_uk unique (workspace_id, version),
  constraint seller_inquiry_authorizations_id_version_uk unique (workspace_id, id, version),
  constraint seller_inquiry_authorizations_version_ck check (version > 0),
  constraint seller_inquiry_authorizations_owner_ck check (
    pg_catalog.length(owner_label) between 1 and 100 and owner_label !~ '[[:cntrl:]]'),
  constraint seller_inquiry_authorizations_dates_ck check (recorded_at >= effective_date or version > 1),
  constraint seller_inquiry_authorizations_source_ck check (pg_catalog.length(source) between 1 and 500),
  -- No per-message or first-template approval (spec 37.1).
  constraint seller_inquiry_authorizations_mode_ck check (approval_mode = 'no_message_approval'),
  constraint seller_inquiry_authorizations_purpose_ck check (purpose = 'initial_availability_documents_price'),
  constraint seller_inquiry_authorizations_questions_ck check (
    questions = array['availability', 'vehicle_documents', 'lowest_final_price']::text[]),
  constraint seller_inquiry_authorizations_recipient_ck check (recipient_class = 'verified_seller_of_exact_listing'),
  constraint seller_inquiry_authorizations_pair_ck check (max_inquiries_per_vehicle_seller_pair = 1),
  constraint seller_inquiry_authorizations_scope_ck check (scope_version >= 1),
  constraint seller_inquiry_authorizations_languages_ck check (
    pg_catalog.cardinality(languages) between 1 and 4
    and languages <@ array['de', 'it', 'fr', 'en']::text[]),
  constraint seller_inquiry_authorizations_english_ck check (english_requires_positive_evidence),
  constraint seller_inquiry_authorizations_allowed_data_ck check (
    pg_catalog.cardinality(allowed_outgoing_data_categories) = 6
    and allowed_outgoing_data_categories <@ array[
      'verified_sender_display_name', 'verified_sender_email', 'vehicle_make_model', 'listing_reference',
      'listing_url', 'three_permitted_questions']::text[]
    and allowed_outgoing_data_categories @> array[
      'verified_sender_display_name', 'verified_sender_email', 'vehicle_make_model', 'listing_reference',
      'listing_url', 'three_permitted_questions']::text[]),
  constraint seller_inquiry_authorizations_excluded_data_ck check (
    app.text_array_ok(excluded_data_categories, 50, 80)
    and excluded_data_categories @> array[
      'home_address', 'telephone', 'identity_documents', 'bank_details', 'finances',
      'acquisition_budget', 'target_resale_price', 'profit_calculation',
      'unrelated_business_information']::text[]
    and not (excluded_data_categories && allowed_outgoing_data_categories)),
  constraint seller_inquiry_authorizations_no_extras_ck check (
    not attachments_allowed and not cc_bcc_allowed and not additional_recipients_allowed
    and not follow_ups_allowed),
  constraint seller_inquiry_authorizations_not_authorized_ck check (
    app.text_array_ok(not_authorized, 50, 80)
    and not_authorized @> array[
      'follow_up', 'outgoing_reply', 'offer', 'price_acceptance', 'negotiation_beyond_lowest_price',
      'reservation', 'viewing_appointment', 'deposit', 'purchase', 'resale_promise', 'payment']::text[]),
  constraint seller_inquiry_authorizations_profiles_ck check (
    pg_catalog.cardinality(profiles_in_scope) between 1 and 3
    and profiles_in_scope <@ array['primary', 'manual_4000', 'below_target_watch']::text[]),
  constraint seller_inquiry_authorizations_revoke_ck check (
    (revoked_at is null and revoked_by is null and revoke_reason is null)
    or (revoked_at is not null and revoked_by is not null and revoke_reason is not null
        and pg_catalog.length(revoked_by) between 1 and 200
        and pg_catalog.length(revoke_reason) between 1 and 2000)),
  constraint seller_inquiry_authorizations_record_ck check (
    pg_catalog.jsonb_typeof(record) = 'object' and pg_catalog.octet_length(record::text) <= 65536),
  constraint seller_inquiry_authorizations_hash_ck check (record_hash ~ '^[0-9a-f]{64}$')
);

-- Workspace-level inquiry controls: kill switch, mode and owner-reducible caps,
-- with an optimistic version for seller_inquiries_pause (expected_version).
create table app.seller_inquiry_controls (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  mode text not null default 'disabled_until_sender_ready',
  kill_switch boolean not null default false,
  kill_switch_reason text,
  kill_switch_set_at timestamptz,
  kill_switch_set_by uuid,
  -- Ceilings, not targets (spec 37.5): the owner may reduce or pause, never raise.
  max_per_24h smallint not null default 2,
  max_per_15d smallint not null default 5,
  -- PROPOSED engineering default (domain.inquiries.SELLER_COOLDOWN).
  seller_cooldown interval not null default interval '7 days',
  version bigint not null default 1,
  updated_by uuid,
  update_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_inquiry_controls_workspace_id_uk unique (workspace_id, id),
  constraint seller_inquiry_controls_workspace_uk unique (workspace_id),
  constraint seller_inquiry_controls_mode_ck check (mode in ('disabled_until_sender_ready', 'automatic', 'paused')),
  constraint seller_inquiry_controls_kill_switch_ck check (
    not kill_switch
    or (kill_switch_set_at is not null and kill_switch_set_by is not null and kill_switch_reason is not null
        and pg_catalog.length(kill_switch_reason) between 3 and 2000)),
  constraint seller_inquiry_controls_kill_reason_ck check (
    kill_switch_reason is null or pg_catalog.length(kill_switch_reason) between 3 and 2000),
  constraint seller_inquiry_controls_caps_ck check (max_per_24h between 0 and 2 and max_per_15d between 0 and 5),
  constraint seller_inquiry_controls_cooldown_ck check (
    seller_cooldown >= interval '1 day' and seller_cooldown <= interval '365 days'),
  constraint seller_inquiry_controls_version_ck check (version > 0),
  constraint seller_inquiry_controls_reason_ck check (
    update_reason is null or pg_catalog.length(update_reason) between 3 and 2000)
);

-- =============================================================================
-- Sender account binding (ops: never client-visible)
-- =============================================================================

-- The configured, owner-authorized sending identity (spec 37.3). Provider,
-- account and From address are the identity: a different account is a new
-- binding row (never a silent switch). Credentials are never stored in clear:
-- either a secret-box ciphertext envelope or an external secret reference.
create table ops.email_sender_bindings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  provider text not null,
  account_id text not null,
  from_address text not null,
  display_name text not null,
  reply_to_address text,
  alias_verified boolean not null default false,
  alias_verified_at timestamptz,
  secret_envelope bytea,
  secret_reference text,
  health text not null default 'unknown',
  health_checked_at timestamptz,
  health_detail text,
  verified_at timestamptz,
  verified_by uuid,
  revoked_at timestamptz,
  revoked_by uuid,
  revoke_reason text,
  version bigint not null default 1,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint email_sender_bindings_workspace_id_uk unique (workspace_id, id),
  constraint email_sender_bindings_provider_ck check (provider in ('outlook_local', 'gmail_api', 'microsoft_graph')),
  constraint email_sender_bindings_account_ck check (app.opaque_ref_ok(account_id, 320)),
  constraint email_sender_bindings_from_ck check (app.email_address_ok(from_address)),
  constraint email_sender_bindings_display_name_ck check (app.sender_display_name_ok(display_name)),
  constraint email_sender_bindings_reply_to_ck check (reply_to_address is null or app.email_address_ok(reply_to_address)),
  constraint email_sender_bindings_alias_ck check (not alias_verified or alias_verified_at is not null),
  -- Never a raw token: a sealed envelope or a scheme:path reference, not both.
  constraint email_sender_bindings_secret_ck check (secret_envelope is null or secret_reference is null),
  -- integrations.secret_box envelope: format version 0x01, key id 1..255, 12-byte nonce,
  -- ciphertext + 16-byte GCM tag. A raw token stored as bytes does not have this shape.
  constraint email_sender_bindings_envelope_ck check (
    secret_envelope is null
    or case
         when pg_catalog.octet_length(secret_envelope) between 31 and 8192
           then pg_catalog.get_byte(secret_envelope, 0) = 1 and pg_catalog.get_byte(secret_envelope, 1) >= 1
         else false
       end),
  constraint email_sender_bindings_reference_ck check (
    secret_reference is null or secret_reference ~ '^[a-z][a-z0-9+.-]{1,30}:[A-Za-z0-9._/-]{1,200}$'),
  constraint email_sender_bindings_health_ck check (health in ('unknown', 'healthy', 'degraded', 'unhealthy')),
  constraint email_sender_bindings_health_detail_ck check (
    health_detail is null or (pg_catalog.length(health_detail) <= 500 and health_detail !~ '[[:cntrl:]]')),
  constraint email_sender_bindings_revoke_ck check (
    (revoked_at is null and revoked_by is null and revoke_reason is null)
    or (revoked_at is not null and revoked_at >= created_at and revoked_by is not null
        and revoke_reason is not null and pg_catalog.length(revoke_reason) between 3 and 500)),
  constraint email_sender_bindings_version_ck check (version > 0)
);
-- One active binding per From address (no ambiguous sender identity).
create unique index email_sender_bindings_active_from_uidx
  on ops.email_sender_bindings (workspace_id, pg_catalog.lower(from_address)) where revoked_at is null;

-- =============================================================================
-- Seller inquiries (spec 37.5 state machine and one-inquiry rule)
-- =============================================================================

create table app.seller_inquiries (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  -- Identity: workspace + canonical vehicle + verified seller entity + purpose.
  identity_key text not null,
  purpose text not null default 'initial_availability_documents_price',
  vehicle_kind text not null,
  vehicle_cluster_id uuid,
  vehicle_listing_id uuid,
  vehicle_key text generated always as (
    vehicle_kind || ':' || coalesce(vehicle_cluster_id, vehicle_listing_id)::text) stored,
  seller_entity_id uuid not null,
  seller_key text generated always as ('seller_entity:' || seller_entity_id::text) stored,
  -- Qualification snapshot (part of the immutable binding once reserved).
  qualification_listing_id uuid not null,
  qualification_revision_id uuid,
  qualification_revision_number integer,
  qualified_semantic_hash text,
  qualified_price_minor bigint,
  qualified_currency char(3),
  qualified_availability text,
  readiness text not null default 'needs_facts',
  readiness_reasons text[] not null default '{}',
  readiness_rationale_hash text,
  readiness_rules_version text,
  readiness_evaluated_at timestamptz,
  authorization_id uuid,
  authorization_version integer,
  authorization_fingerprint text,
  template_id text,
  template_version integer,
  template_hash text,
  template_set_version text,
  language text,
  scope_hash text,
  body_hash text,
  binding_hash text,
  original_subject text,
  original_body text,
  mk_preview_subject text,
  mk_preview_body text,
  mk_preview_hash text,
  sender_binding_id uuid,
  sender_binding_version bigint,
  sender_provider text,
  sender_account_id text,
  sender_from_address text,
  sender_display_name text,
  sender_reply_to_address text,
  recipient_contact_id uuid,
  recipient_address text,
  recipient_binding_hash text,
  -- Lifecycle.
  state text not null default 'candidate',
  state_reasons text[] not null default '{}',
  suppression_reason text,
  requalification_audit_id uuid,
  -- Provider references (set once when known; never fabricated).
  rfc_message_id text,
  provider_message_id text,
  provider_thread_id text,
  provider_receipt jsonb,
  reserved_at timestamptz,
  queued_at timestamptz,
  send_attempted_at timestamptz,
  accepted_at timestamptz,
  replied_at timestamptz,
  state_changed_at timestamptz not null default now(),
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_inquiries_workspace_id_uk unique (workspace_id, id),
  -- The one-inquiry rule (spec 37.5): one record per identity, ever. A cancelled
  -- never-transmitted record is re-qualified, not duplicated.
  constraint seller_inquiries_identity_uk unique (workspace_id, identity_key),
  constraint seller_inquiries_identity_parts_uk unique (workspace_id, purpose, vehicle_key, seller_entity_id),
  constraint seller_inquiries_vehicle_cluster_fk foreign key (workspace_id, vehicle_cluster_id)
    references app.vehicle_clusters (workspace_id, id),
  constraint seller_inquiries_vehicle_listing_fk foreign key (workspace_id, vehicle_listing_id)
    references app.listings (workspace_id, id),
  constraint seller_inquiries_seller_fk foreign key (workspace_id, seller_entity_id)
    references app.seller_entities (workspace_id, id),
  constraint seller_inquiries_listing_fk foreign key (workspace_id, qualification_listing_id)
    references app.listings (workspace_id, id),
  constraint seller_inquiries_revision_fk foreign key (workspace_id, qualification_listing_id, qualification_revision_id)
    references app.listing_revisions (workspace_id, listing_id, id),
  constraint seller_inquiries_authorization_fk foreign key (workspace_id, authorization_id, authorization_version)
    references app.seller_inquiry_authorizations (workspace_id, id, version),
  constraint seller_inquiries_sender_fk foreign key (workspace_id, sender_binding_id)
    references ops.email_sender_bindings (workspace_id, id),
  -- The recipient is evidence for THIS listing and THIS seller entity (spec 37.3).
  constraint seller_inquiries_recipient_listing_fk foreign key (workspace_id, qualification_listing_id, recipient_contact_id)
    references app.seller_contacts (workspace_id, listing_id, id),
  constraint seller_inquiries_recipient_seller_fk foreign key (workspace_id, seller_entity_id, recipient_contact_id)
    references app.seller_contacts (workspace_id, seller_entity_id, id),
  constraint seller_inquiries_requalification_fk foreign key (workspace_id, requalification_audit_id)
    references ops.audit_events (workspace_id, id),
  constraint seller_inquiries_purpose_ck check (purpose = 'initial_availability_documents_price'),
  constraint seller_inquiries_vehicle_ck check (
    (vehicle_kind = 'vehicle_cluster' and vehicle_cluster_id is not null and vehicle_listing_id is null)
    or (vehicle_kind = 'listing_incarnation' and vehicle_listing_id is not null and vehicle_cluster_id is null)),
  -- A listing-incarnation identity names exactly the qualifying listing.
  constraint seller_inquiries_incarnation_ck check (
    vehicle_kind <> 'listing_incarnation' or vehicle_listing_id = qualification_listing_id),
  constraint seller_inquiries_identity_key_ck check (
    identity_key ~ '^[0-9a-f]{64}$'
    and identity_key = app.seller_inquiry_identity_key(
      workspace_id, vehicle_kind, coalesce(vehicle_cluster_id, vehicle_listing_id), seller_entity_id, purpose)),
  constraint seller_inquiries_revision_number_ck check (
    qualification_revision_number is null or qualification_revision_number >= 0),
  constraint seller_inquiries_semantic_hash_ck check (
    qualified_semantic_hash is null or qualified_semantic_hash ~ '^[0-9a-f]{64}$'),
  constraint seller_inquiries_price_ck check (qualified_price_minor is null or qualified_price_minor >= 0),
  constraint seller_inquiries_currency_ck check (qualified_currency is null or qualified_currency ~ '^[A-Z]{3}$'),
  constraint seller_inquiries_currency_pair_ck check ((qualified_price_minor is null) = (qualified_currency is null)),
  constraint seller_inquiries_availability_ck check (qualified_availability is null or qualified_availability in (
    'available', 'reserved', 'removed', 'sold_claimed', 'unknown')),
  constraint seller_inquiries_readiness_ck check (readiness in (
    'inquiry_ready', 'needs_facts', 'needs_technical_review', 'not_eligible')),
  constraint seller_inquiries_readiness_reasons_ck check (app.text_array_ok(readiness_reasons, 60, 80)),
  constraint seller_inquiries_rationale_ck check (
    readiness_rationale_hash is null or readiness_rationale_hash ~ '^[0-9a-f]{64}$'),
  constraint seller_inquiries_rules_version_ck check (
    readiness_rules_version is null or pg_catalog.length(readiness_rules_version) between 1 and 80),
  constraint seller_inquiries_authorization_ck check (
    (authorization_id is null) = (authorization_version is null)
    and (authorization_fingerprint is null or authorization_fingerprint ~ '^[0-9a-f]{64}$')),
  constraint seller_inquiries_template_ck check (
    template_id is null
    or (template_id ~ '^seller_initial_(de|it|fr|en)_v[1-9][0-9]*$'
        and template_version is not null and template_id = 'seller_initial_' || pg_catalog.substr(template_id, 16, 2)
                                                             || '_v' || template_version::text)),
  constraint seller_inquiries_template_version_ck check (template_version is null or template_version >= 1),
  constraint seller_inquiries_template_set_ck check (
    template_set_version is null or pg_catalog.length(template_set_version) between 1 and 80),
  -- English is just another template language here; positive evidence is required
  -- through the verified recipient contact at reservation (never a fallback).
  constraint seller_inquiries_language_ck check (language is null or language in ('de', 'it', 'fr', 'en')),
  constraint seller_inquiries_template_language_ck check (
    template_id is null or language is null or pg_catalog.substr(template_id, 16, 2) = language),
  constraint seller_inquiries_hashes_ck check (
    (template_hash is null or template_hash ~ '^[0-9a-f]{64}$')
    and (scope_hash is null or scope_hash ~ '^[0-9a-f]{64}$')
    and (body_hash is null or body_hash ~ '^[0-9a-f]{64}$')
    and (binding_hash is null or binding_hash ~ '^[0-9a-f]{64}$')
    and (mk_preview_hash is null or mk_preview_hash ~ '^[0-9a-f]{64}$')
    and (recipient_binding_hash is null or recipient_binding_hash ~ '^[0-9a-f]{64}$')),
  -- The stored original/preview are exactly what the immutable hashes cover.
  constraint seller_inquiries_original_ck check (
    (original_subject is null) = (original_body is null)
    and (original_subject is null
         or (pg_catalog.length(original_subject) between 1 and 200
             and original_subject !~ '[[:cntrl:]\u0085\u2028\u2029]'
             and pg_catalog.is_normalized(original_subject, 'NFC')
             and pg_catalog.length(original_body) between 1 and 2000
             and original_body !~ '[\x01-\x09\x0b-\x1f\x7f\u0085\u2028\u2029]'
             and pg_catalog.is_normalized(original_body, 'NFC')))),
  constraint seller_inquiries_body_hash_ck check (
    original_body is null or body_hash is null
    or body_hash = app.message_body_hash(original_subject, original_body)),
  constraint seller_inquiries_preview_ck check (
    (mk_preview_subject is null) = (mk_preview_body is null)
    and (mk_preview_subject is null
         or (pg_catalog.length(mk_preview_subject) between 1 and 200
             and mk_preview_subject !~ '[[:cntrl:]\u0085\u2028\u2029]'
             and pg_catalog.is_normalized(mk_preview_subject, 'NFC')
             and pg_catalog.length(mk_preview_body) between 1 and 2000
             and mk_preview_body !~ '[\x01-\x09\x0b-\x1f\x7f\u0085\u2028\u2029]'
             and pg_catalog.is_normalized(mk_preview_body, 'NFC')))),
  constraint seller_inquiries_preview_hash_ck check (
    mk_preview_body is null or mk_preview_hash is null
    or mk_preview_hash = app.message_body_hash(mk_preview_subject, mk_preview_body)),
  constraint seller_inquiries_sender_ck check (
    (sender_binding_id is null) = (sender_binding_version is null)
    and (sender_binding_version is null or sender_binding_version >= 1)
    and (sender_provider is null or sender_provider in ('outlook_local', 'gmail_api', 'microsoft_graph'))
    and (sender_account_id is null or app.opaque_ref_ok(sender_account_id, 320))
    and (sender_from_address is null or app.email_address_ok(sender_from_address))
    and (sender_display_name is null or app.sender_display_name_ok(sender_display_name))
    and (sender_reply_to_address is null or app.email_address_ok(sender_reply_to_address))),
  constraint seller_inquiries_recipient_ck check (
    (recipient_address is null or app.email_address_ok(recipient_address))
    and (recipient_contact_id is null) = (recipient_address is null)),
  constraint seller_inquiries_recipient_not_sender_ck check (
    recipient_address is null
    or ((sender_from_address is null or pg_catalog.lower(recipient_address) <> pg_catalog.lower(sender_from_address))
        and (sender_reply_to_address is null
             or pg_catalog.lower(recipient_address) <> pg_catalog.lower(sender_reply_to_address)))),
  constraint seller_inquiries_state_ck check (state in (
    'candidate', 'qualifying', 'reserved', 'queued', 'sending', 'accepted', 'held_facts', 'uncertain',
    'suppressed', 'failed_definite', 'cancelled', 'replied', 'bounced', 'seller_opted_out', 'no_reply_yet')),
  constraint seller_inquiries_state_reasons_ck check (app.text_array_ok(state_reasons, 30, 80)),
  constraint seller_inquiries_suppression_ck check (
    (state = 'suppressed') = (suppression_reason is not null)
    and (suppression_reason is null or suppression_reason in (
      'hard_bounce', 'complaint', 'seller_opt_out', 'source_paused', 'sender_revoked',
      'unresolved_send_outcome', 'kill_switch', 'contradictory_availability', 'manual'))),
  -- Reserved and every later state carry the complete immutable binding.
  constraint seller_inquiries_binding_complete_ck check (
    state not in ('reserved', 'queued', 'sending', 'accepted', 'uncertain', 'failed_definite', 'replied',
                  'bounced', 'seller_opted_out', 'no_reply_yet')
    or (readiness = 'inquiry_ready'
        and qualification_revision_id is not null and qualification_revision_number is not null
        and qualified_semantic_hash is not null and qualified_availability is not null
        and readiness_rationale_hash is not null and readiness_rules_version is not null
        and readiness_evaluated_at is not null
        and authorization_id is not null and authorization_fingerprint is not null
        and template_id is not null and template_hash is not null and template_set_version is not null
        and language is not null and scope_hash is not null and body_hash is not null
        and binding_hash is not null and original_subject is not null
        and mk_preview_subject is not null and mk_preview_hash is not null
        and sender_binding_id is not null and sender_provider is not null and sender_account_id is not null
        and sender_from_address is not null and sender_display_name is not null
        and recipient_contact_id is not null and recipient_binding_hash is not null
        and reserved_at is not null)),
  constraint seller_inquiries_queued_ck check (
    state not in ('queued', 'sending', 'accepted', 'uncertain', 'failed_definite', 'replied', 'bounced',
                  'seller_opted_out', 'no_reply_yet')
    or queued_at is not null),
  constraint seller_inquiries_sent_ck check (
    state not in ('sending', 'accepted', 'uncertain', 'failed_definite', 'replied', 'bounced',
                  'seller_opted_out', 'no_reply_yet')
    or send_attempted_at is not null),
  constraint seller_inquiries_accepted_ck check (
    state not in ('accepted', 'replied', 'bounced', 'seller_opted_out', 'no_reply_yet') or accepted_at is not null),
  constraint seller_inquiries_replied_ck check (state <> 'replied' or replied_at is not null),
  constraint seller_inquiries_message_id_ck check (rfc_message_id is null or app.rfc_message_id_ok(rfc_message_id)),
  constraint seller_inquiries_provider_ids_ck check (
    (provider_message_id is null or app.opaque_ref_ok(provider_message_id, 512))
    and (provider_thread_id is null or app.opaque_ref_ok(provider_thread_id, 512))),
  constraint seller_inquiries_receipt_ck check (
    provider_receipt is null
    or (pg_catalog.jsonb_typeof(provider_receipt) = 'object'
        and pg_catalog.octet_length(provider_receipt::text) <= 16384)),
  constraint seller_inquiries_row_version_ck check (row_version > 0)
);
-- One live inquiry per (qualifying listing, seller): an identity merge (listing
-- incarnation -> confirmed cluster) must cancel the superseded record first.
create unique index seller_inquiries_listing_seller_uidx
  on app.seller_inquiries (workspace_id, qualification_listing_id, seller_entity_id) where state <> 'cancelled';
-- Correlation by stable Message-ID must be unambiguous.
create unique index seller_inquiries_message_id_uidx
  on app.seller_inquiries (workspace_id, rfc_message_id) where rfc_message_id is not null;
create index seller_inquiries_dispatch_idx on app.seller_inquiries (workspace_id, queued_at, id) where state = 'queued';
create index seller_inquiries_attention_idx
  on app.seller_inquiries (workspace_id, state, state_changed_at)
  where state in ('uncertain', 'failed_definite', 'held_facts', 'sending');
create index seller_inquiries_seller_idx on app.seller_inquiries (workspace_id, seller_entity_id, reserved_at);
create index seller_inquiries_cluster_idx
  on app.seller_inquiries (workspace_id, vehicle_cluster_id) where vehicle_cluster_id is not null;
create index seller_inquiries_vehicle_listing_idx
  on app.seller_inquiries (workspace_id, vehicle_listing_id) where vehicle_listing_id is not null;
create index seller_inquiries_revision_idx
  on app.seller_inquiries (workspace_id, qualification_listing_id, qualification_revision_id);
create index seller_inquiries_authorization_idx
  on app.seller_inquiries (workspace_id, authorization_id, authorization_version) where authorization_id is not null;
create index seller_inquiries_sender_idx
  on app.seller_inquiries (workspace_id, sender_binding_id) where sender_binding_id is not null;
create index seller_inquiries_recipient_listing_idx
  on app.seller_inquiries (workspace_id, qualification_listing_id, recipient_contact_id)
  where recipient_contact_id is not null;
create index seller_inquiries_recipient_seller_idx
  on app.seller_inquiries (workspace_id, seller_entity_id, recipient_contact_id) where recipient_contact_id is not null;
create index seller_inquiries_requalification_idx
  on app.seller_inquiries (workspace_id, requalification_audit_id) where requalification_audit_id is not null;

-- =============================================================================
-- Send attempts and the quota ledger
-- =============================================================================

-- One row per transmission attempt. The send intent (outcome 'running') is
-- committed BEFORE external I/O; the row is append-only except the one-time
-- outcome finalisation and the one-time reconciliation of an uncertain outcome.
create table ops.email_delivery_attempts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  inquiry_id uuid not null,
  attempt_id uuid not null,
  attempt_number integer not null,
  outbox_id uuid,
  job_id uuid,
  sender_binding_id uuid not null,
  sender_binding_version bigint not null,
  provider text not null,
  rfc_message_id text,
  fencing_token bigint not null,
  lease_owner text not null,
  lease_token uuid not null,
  lease_expires_at timestamptz not null,
  send_intent_committed_at timestamptz not null default now(),
  outcome text not null default 'running',
  finished_at timestamptz,
  pre_submission_proof text,
  provider_idempotency_key text,
  provider_idempotency_documented boolean not null default false,
  provider_message_id text,
  provider_thread_id text,
  provider_response jsonb,
  receipt jsonb,
  error_code text,
  reconciled_outcome text,
  reconciled_at timestamptz,
  reconciliation_evidence jsonb,
  submission_uncertain boolean generated always as (outcome = 'uncertain' and reconciled_outcome is null) stored,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint email_delivery_attempts_workspace_id_uk unique (workspace_id, id),
  constraint email_delivery_attempts_attempt_id_uk unique (attempt_id),
  constraint email_delivery_attempts_number_uk unique (workspace_id, inquiry_id, attempt_number),
  constraint email_delivery_attempts_inquiry_fk foreign key (workspace_id, inquiry_id)
    references app.seller_inquiries (workspace_id, id),
  constraint email_delivery_attempts_outbox_fk foreign key (workspace_id, outbox_id)
    references ops.outbox (workspace_id, id),
  constraint email_delivery_attempts_job_fk foreign key (workspace_id, job_id)
    references ops.jobs (workspace_id, id),
  constraint email_delivery_attempts_sender_fk foreign key (workspace_id, sender_binding_id)
    references ops.email_sender_bindings (workspace_id, id),
  -- domain.inquiries.MAX_SEND_ATTEMPTS = 3.
  constraint email_delivery_attempts_number_ck check (attempt_number between 1 and 3),
  constraint email_delivery_attempts_version_ck check (sender_binding_version >= 1),
  constraint email_delivery_attempts_provider_ck check (provider in ('outlook_local', 'gmail_api', 'microsoft_graph')),
  constraint email_delivery_attempts_message_id_ck check (rfc_message_id is null or app.rfc_message_id_ok(rfc_message_id)),
  constraint email_delivery_attempts_fencing_ck check (fencing_token >= 1),
  constraint email_delivery_attempts_lease_owner_ck check (pg_catalog.length(lease_owner) between 1 and 200),
  constraint email_delivery_attempts_outcome_ck check (outcome in (
    'running', 'accepted', 'pre_submission_failure', 'definite_rejection', 'uncertain')),
  constraint email_delivery_attempts_finished_ck check (
    (outcome = 'running') = (finished_at is null)
    and (finished_at is null or finished_at >= send_intent_committed_at)),
  constraint email_delivery_attempts_proof_ck check (
    (pre_submission_proof is null or pre_submission_proof in (
      'connection_refused_before_submit', 'local_validation_failed_before_submit',
      'credentials_rejected_before_submit', 'provider_documented_not_sent'))
    and (pre_submission_proof is null or outcome = 'pre_submission_failure')),
  constraint email_delivery_attempts_idempotency_ck check (
    (provider_idempotency_key is null or app.opaque_ref_ok(provider_idempotency_key, 200))
    and (not provider_idempotency_documented or provider_idempotency_key is not null)),
  constraint email_delivery_attempts_provider_ids_ck check (
    (provider_message_id is null or app.opaque_ref_ok(provider_message_id, 512))
    and (provider_thread_id is null or app.opaque_ref_ok(provider_thread_id, 512))),
  constraint email_delivery_attempts_response_ck check (
    provider_response is null
    or (pg_catalog.jsonb_typeof(provider_response) = 'object'
        and pg_catalog.octet_length(provider_response::text) <= 16384)),
  -- A receipt exists only for an accepted submission; never fabricated.
  constraint email_delivery_attempts_receipt_ck check (
    receipt is null
    or (pg_catalog.jsonb_typeof(receipt) = 'object' and pg_catalog.octet_length(receipt::text) <= 16384
        and (outcome = 'accepted' or reconciled_outcome is not distinct from 'accepted'))),
  constraint email_delivery_attempts_error_ck check (error_code is null or error_code ~ '^[A-Za-z0-9_.:-]{1,80}$'),
  constraint email_delivery_attempts_reconciled_ck check (
    (reconciled_outcome is null) = (reconciled_at is null)
    and (reconciled_outcome is null
         or (outcome = 'uncertain' and reconciled_outcome in ('accepted', 'proven_not_submitted')))
    and (reconciliation_evidence is null
         or (reconciled_outcome is not null and pg_catalog.jsonb_typeof(reconciliation_evidence) = 'object'
             and pg_catalog.octet_length(reconciliation_evidence::text) <= 16384))),
  -- Reconciliation resolves an uncertain send only on positive evidence, in the shape of
  -- domain.inquiries.ReconciliationEvidence (reconcile_uncertain): acceptance needs a Sent
  -- Items/provider hit or a correlated inbound message; non-submission needs a documented
  -- pre-submission proof with no live worker and no pending Outbox item. An empty search
  -- ("not_found") is never proof (spec 37.5).
  constraint email_delivery_attempts_reconciliation_proof_ck check (
    reconciled_outcome is null
    or (reconciled_outcome = 'accepted'
        and coalesce(reconciliation_evidence ->> 'sent_items' = 'found'
                     or reconciliation_evidence ->> 'provider_search' = 'found'
                     or reconciliation_evidence -> 'correlated_inbound' = 'true'::jsonb, false))
    or (reconciled_outcome = 'proven_not_submitted'
        and coalesce(
              reconciliation_evidence ->> 'proven_not_submitted' in (
                'connection_refused_before_submit', 'local_validation_failed_before_submit',
                'credentials_rejected_before_submit', 'provider_documented_not_sent')
              and reconciliation_evidence ->> 'worker_alive' = 'no'
              and reconciliation_evidence ->> 'outbox_pending' = 'no'
              and coalesce(reconciliation_evidence ->> 'sent_items', 'not_searched') <> 'found'
              and coalesce(reconciliation_evidence ->> 'provider_search', 'not_searched') <> 'found'
              and coalesce(reconciliation_evidence -> 'correlated_inbound', 'false'::jsonb) <> 'true'::jsonb,
              false)))
);
-- At most one attempt in flight per inquiry.
create unique index email_delivery_attempts_running_uidx
  on ops.email_delivery_attempts (workspace_id, inquiry_id) where outcome = 'running';
create index email_delivery_attempts_lease_idx on ops.email_delivery_attempts (lease_expires_at) where outcome = 'running';
create index email_delivery_attempts_uncertain_idx
  on ops.email_delivery_attempts (workspace_id, created_at) where submission_uncertain;
create index email_delivery_attempts_outbox_idx
  on ops.email_delivery_attempts (workspace_id, outbox_id) where outbox_id is not null;
create index email_delivery_attempts_job_idx on ops.email_delivery_attempts (workspace_id, job_id) where job_id is not null;
create index email_delivery_attempts_sender_idx on ops.email_delivery_attempts (workspace_id, sender_binding_id);

-- One debit per reserved inquiry. Caps are checked transactionally on insert
-- (workspace control row locked). Debits of possibly transmitted inquiries are
-- retained; only a never-transmitted cancelled/suppressed reservation is released.
create table ops.inquiry_quota_ledger (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  inquiry_id uuid not null,
  debited_at timestamptz not null default now(),
  released_at timestamptz,
  release_reason text,
  created_at timestamptz not null default now(),
  constraint inquiry_quota_ledger_workspace_id_uk unique (workspace_id, id),
  constraint inquiry_quota_ledger_inquiry_fk foreign key (workspace_id, inquiry_id)
    references app.seller_inquiries (workspace_id, id),
  constraint inquiry_quota_ledger_release_ck check (
    (released_at is null and release_reason is null)
    or (released_at is not null and released_at >= debited_at
        and release_reason is not null and release_reason ~ '^[A-Za-z0-9_.:-]{3,80}$'))
);
create unique index inquiry_quota_ledger_active_uidx
  on ops.inquiry_quota_ledger (workspace_id, inquiry_id) where released_at is null;
create index inquiry_quota_ledger_window_idx
  on ops.inquiry_quota_ledger (workspace_id, debited_at) where released_at is null;

-- =============================================================================
-- Local mailbox route: worker bindings, replies, locators, dedup, checkpoints,
-- binding sync (spec 37.6-37.8)
-- =============================================================================

-- A mailbox-bound worker identity. id is the stable mailbox_binding_id used in
-- dedup keys; the narrow mail:ingest credential can be rotated in place.
create table ops.mail_worker_bindings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  sender_binding_id uuid not null,
  credential_id uuid not null,
  provider text not null,
  account_address text not null,
  store_id_hash text,
  folder_scope text[] not null default '{}',
  worker_label text not null,
  state text not null default 'active',
  revoked_at timestamptz,
  revoked_by uuid,
  revoke_reason text,
  -- Allocator for ops.mail_binding_sync.sequence (monotonic per mailbox).
  sync_sequence bigint not null default 0,
  version bigint not null default 1,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint mail_worker_bindings_workspace_id_uk unique (workspace_id, id),
  constraint mail_worker_bindings_sender_fk foreign key (workspace_id, sender_binding_id)
    references ops.email_sender_bindings (workspace_id, id),
  constraint mail_worker_bindings_credential_fk foreign key (workspace_id, credential_id)
    references ops.api_credentials (workspace_id, id),
  constraint mail_worker_bindings_provider_ck check (provider in ('outlook_local', 'gmail_api', 'microsoft_graph')),
  constraint mail_worker_bindings_account_ck check (app.email_address_ok(account_address)),
  constraint mail_worker_bindings_store_ck check (store_id_hash is null or store_id_hash ~ '^[0-9a-f]{64}$'),
  constraint mail_worker_bindings_folders_ck check (app.hex64_array_ok(folder_scope, 20)),
  constraint mail_worker_bindings_label_ck check (
    pg_catalog.length(worker_label) between 1 and 120 and worker_label !~ '[[:cntrl:]]'),
  constraint mail_worker_bindings_state_ck check (state in ('active', 'revoked')),
  constraint mail_worker_bindings_revoke_ck check (
    (state = 'active' and revoked_at is null and revoked_by is null and revoke_reason is null)
    or (state = 'revoked' and revoked_at is not null and revoked_by is not null and revoke_reason is not null
        and pg_catalog.length(revoke_reason) between 3 and 500)),
  constraint mail_worker_bindings_sequence_ck check (sync_sequence >= 0),
  constraint mail_worker_bindings_version_ck check (version > 0)
);
-- One active consumer per mailbox (no duplicate processing) and one mailbox per credential.
create unique index mail_worker_bindings_active_mailbox_uidx
  on ops.mail_worker_bindings (workspace_id, sender_binding_id) where state = 'active';
create unique index mail_worker_bindings_active_credential_uidx
  on ops.mail_worker_bindings (credential_id) where state = 'active';
create index mail_worker_bindings_credential_idx on ops.mail_worker_bindings (workspace_id, credential_id);

-- Inquiry-correlated replies only (spec 37.7). Source content is immutable;
-- processing outputs (MK summary, claims) and quarantine handling may follow.
create table app.seller_replies (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  inquiry_id uuid not null,
  mailbox_binding_id uuid not null,
  binding_version integer not null,
  internet_message_id text,
  provider_message_id text,
  provider_thread_id text,
  from_address text not null,
  in_reply_to text,
  reference_ids text[] not null default '{}',
  -- Message-IDs of the returned original of a bounce/delivery notice (DSN), which usually
  -- carries no In-Reply-To (domain.replies.parse_delivery_report().original_message_ids).
  returned_message_ids text[] not null default '{}',
  -- Correlation links, computed by the insert trigger (never caller-supplied): the
  -- In-Reply-To/References/returned original names a Message-ID this system sent or
  -- published for the inquiry, or the provider thread id is the inquiry's own thread.
  header_linked boolean not null default false,
  thread_linked boolean not null default false,
  subject text not null default '',
  sanitized_body text not null,
  body_sanitizer_version text not null,
  source_fingerprint text not null,
  fingerprint_version text not null,
  received_at timestamptz not null,
  observed_at timestamptz not null,
  ingested_at timestamptz not null default now(),
  message_type text not null default 'seller_reply',
  correlation_status text not null default 'matched',
  correlation_reasons text[] not null default '{}',
  detected_language text,
  attachments jsonb not null default '[]'::jsonb,
  withheld_sensitive_attachments integer not null default 0,
  mk_summary text,
  mk_summary_version text,
  mk_summary_generated_at timestamptz,
  claims jsonb not null default '{}'::jsonb,
  claims_version text,
  processing_state text not null default 'stored',
  processed_at timestamptz,
  quarantined boolean not null default false,
  quarantine_reason text,
  quarantine_released_at timestamptz,
  quarantine_released_by uuid,
  quarantine_release_reason text,
  -- A conflicting body/header under an already ingested identity is kept here,
  -- quarantined for investigation, never overwriting the original (spec 37.8).
  conflict_of_reply_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint seller_replies_workspace_id_uk unique (workspace_id, id),
  constraint seller_replies_inquiry_id_uk unique (workspace_id, inquiry_id, id),
  constraint seller_replies_mailbox_id_uk unique (workspace_id, mailbox_binding_id, id),
  constraint seller_replies_inquiry_fk foreign key (workspace_id, inquiry_id)
    references app.seller_inquiries (workspace_id, id),
  constraint seller_replies_mailbox_fk foreign key (workspace_id, mailbox_binding_id)
    references ops.mail_worker_bindings (workspace_id, id),
  constraint seller_replies_conflict_fk foreign key (workspace_id, mailbox_binding_id, conflict_of_reply_id)
    references app.seller_replies (workspace_id, mailbox_binding_id, id),
  constraint seller_replies_binding_version_ck check (binding_version >= 1),
  constraint seller_replies_message_id_ck check (
    internet_message_id is null or app.rfc_message_id_ok(internet_message_id)),
  constraint seller_replies_provider_ids_ck check (
    (provider_message_id is null or app.opaque_ref_ok(provider_message_id, 512))
    and (provider_thread_id is null or app.opaque_ref_ok(provider_thread_id, 512))),
  constraint seller_replies_from_ck check (app.email_address_ok(from_address)),
  constraint seller_replies_in_reply_to_ck check (in_reply_to is null or app.rfc_message_id_ok(in_reply_to)),
  constraint seller_replies_references_ck check (app.rfc_message_id_array_ok(reference_ids, 200)),
  constraint seller_replies_returned_ids_ck check (app.rfc_message_id_array_ok(returned_message_ids, 20)),
  -- Never a subject-only (or unlinked) match (spec 37.7): an unquarantined automatic match is
  -- linked to this inquiry by Message-ID or by its provider thread. Unlinked possible matches
  -- are stored quarantined and become usable only through a recorded verification.
  constraint seller_replies_link_ck check (
    quarantined or correlation_status <> 'matched' or header_linked or thread_linked),
  constraint seller_replies_subject_ck check (
    pg_catalog.length(subject) <= 512 and subject !~ '[[:cntrl:]\u0085\u2028\u2029]'),
  constraint seller_replies_body_ck check (
    pg_catalog.octet_length(sanitized_body) <= 65536
    and sanitized_body !~ '[\x01-\x08\x0b-\x1f\x7f]'),
  constraint seller_replies_versions_ck check (
    pg_catalog.length(body_sanitizer_version) between 1 and 80
    and pg_catalog.length(fingerprint_version) between 1 and 80
    and (mk_summary_version is null or pg_catalog.length(mk_summary_version) between 1 and 80)
    and (claims_version is null or pg_catalog.length(claims_version) between 1 and 80)),
  constraint seller_replies_fingerprint_ck check (source_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint seller_replies_type_ck check (message_type in (
    'seller_reply', 'auto_reply', 'bounce', 'delivery_notice', 'spam', 'ambiguous')),
  constraint seller_replies_correlation_ck check (correlation_status in ('matched', 'quarantined', 'verified_match')),
  constraint seller_replies_correlation_reasons_ck check (app.text_array_ok(correlation_reasons, 30, 80)),
  constraint seller_replies_language_ck check (detected_language is null or detected_language ~ '^[a-z]{2}$'),
  constraint seller_replies_attachments_ck check (app.reply_attachments_ok(attachments)),
  constraint seller_replies_withheld_ck check (withheld_sensitive_attachments between 0 and 200),
  constraint seller_replies_mk_summary_ck check (
    (mk_summary is null) = (mk_summary_version is null)
    and (mk_summary is null or (pg_catalog.octet_length(mk_summary) <= 32768 and mk_summary_generated_at is not null))),
  constraint seller_replies_claims_ck check (
    pg_catalog.jsonb_typeof(claims) = 'object' and pg_catalog.octet_length(claims::text) <= 65536),
  constraint seller_replies_processing_ck check (
    processing_state in ('stored', 'processed', 'failed')
    and ((processing_state = 'stored') = (processed_at is null))),
  constraint seller_replies_quarantine_ck check (
    quarantined = (quarantine_reason is not null)
    and (quarantine_reason is null or quarantine_reason ~ '^[a-z][a-z0-9_]{2,79}$')),
  -- Quarantined possible matches never update a vehicle until verified; spam and
  -- unresolved ambiguous messages are always quarantined.
  constraint seller_replies_quarantine_required_ck check (
    (correlation_status <> 'quarantined' and message_type not in ('spam', 'ambiguous')
     and conflict_of_reply_id is null)
    or quarantined),
  -- A verified match is reached only by releasing a quarantined possible match.
  constraint seller_replies_verified_ck check (
    correlation_status <> 'verified_match' or quarantine_released_at is not null),
  constraint seller_replies_release_ck check (
    (quarantine_released_at is null and quarantine_released_by is null and quarantine_release_reason is null)
    or (quarantine_released_at is not null and quarantine_released_by is not null and not quarantined
        and correlation_status = 'verified_match' and quarantine_release_reason is not null
        and pg_catalog.length(quarantine_release_reason) between 3 and 500)),
  constraint seller_replies_conflict_ck check (
    conflict_of_reply_id is null
    or (conflict_of_reply_id <> id and quarantine_reason is not distinct from 'idempotency_conflict'))
);
-- Stable per-mailbox message identity (spec 37.6): Internet Message-ID, else
-- the provider message id, else the immutable content fingerprint.
create unique index seller_replies_message_uidx
  on app.seller_replies (workspace_id, mailbox_binding_id, internet_message_id)
  where internet_message_id is not null and conflict_of_reply_id is null;
create unique index seller_replies_provider_message_uidx
  on app.seller_replies (workspace_id, mailbox_binding_id, provider_message_id)
  where internet_message_id is null and provider_message_id is not null and conflict_of_reply_id is null;
create unique index seller_replies_fingerprint_uidx
  on app.seller_replies (workspace_id, mailbox_binding_id, source_fingerprint)
  where internet_message_id is null and provider_message_id is null and conflict_of_reply_id is null;
-- An inquiry never holds the same message twice, whatever the mailbox binding.
create unique index seller_replies_inquiry_message_uidx
  on app.seller_replies (workspace_id, inquiry_id, internet_message_id)
  where internet_message_id is not null and conflict_of_reply_id is null;
create unique index seller_replies_conflict_uidx
  on app.seller_replies (workspace_id, conflict_of_reply_id, source_fingerprint) where conflict_of_reply_id is not null;
create index seller_replies_inquiry_idx on app.seller_replies (workspace_id, inquiry_id, received_at desc);
create index seller_replies_mailbox_idx on app.seller_replies (workspace_id, mailbox_binding_id, ingested_at desc);
create index seller_replies_quarantine_idx
  on app.seller_replies (workspace_id, ingested_at) where quarantined;
create index seller_replies_conflict_of_idx
  on app.seller_replies (workspace_id, mailbox_binding_id, conflict_of_reply_id) where conflict_of_reply_id is not null;

-- Mutable Outlook locators (EntryID/StoreID/folder) as seen over time; never
-- part of the dedup key or fingerprint. Append-only history.
create table app.seller_reply_locators (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  reply_id uuid not null,
  mailbox_binding_id uuid not null,
  outlook_entry_id text,
  outlook_store_id text,
  folder_id text,
  seen_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint seller_reply_locators_workspace_id_uk unique (workspace_id, id),
  constraint seller_reply_locators_reply_fk foreign key (workspace_id, mailbox_binding_id, reply_id)
    references app.seller_replies (workspace_id, mailbox_binding_id, id),
  constraint seller_reply_locators_present_ck check (outlook_entry_id is not null or outlook_store_id is not null),
  constraint seller_reply_locators_refs_ck check (
    (outlook_entry_id is null or app.opaque_ref_ok(outlook_entry_id, 1024))
    and (outlook_store_id is null or app.opaque_ref_ok(outlook_store_id, 1024))
    and (folder_id is null or app.opaque_ref_ok(folder_id, 1024)))
);
create unique index seller_reply_locators_uidx
  on app.seller_reply_locators (
    workspace_id, reply_id,
    pg_catalog.md5(coalesce(outlook_entry_id, '') || '|' || coalesce(outlook_store_id, '') || '|'
                   || coalesce(folder_id, '')));
create index seller_reply_locators_reply_idx
  on app.seller_reply_locators (workspace_id, mailbox_binding_id, reply_id, seen_at desc);

-- Stable account/message identity -> stored reply (spec 37.8 replay protection).
-- Both the request idempotency key and the source identity are unique.
create table ops.mail_ingest_dedup (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  mailbox_binding_id uuid not null,
  credential_id uuid not null,
  dedup_kind text not null,
  dedup_key text not null,
  idempotency_key text not null,
  fingerprint text not null,
  fingerprint_version text not null,
  inquiry_id uuid not null,
  reply_id uuid not null,
  ingest_result text not null,
  first_ingested_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  duplicate_count integer not null default 0,
  conflict_count integer not null default 0,
  last_conflict_at timestamptz,
  last_conflict_fingerprint text,
  last_conflict_reply_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint mail_ingest_dedup_workspace_id_uk unique (workspace_id, id),
  constraint mail_ingest_dedup_key_uk unique (workspace_id, mailbox_binding_id, dedup_key),
  constraint mail_ingest_dedup_idempotency_uk unique (workspace_id, mailbox_binding_id, idempotency_key),
  -- One dedup record per stored reply (the reply's mailbox is pinned by the FK below).
  constraint mail_ingest_dedup_reply_uk unique (workspace_id, mailbox_binding_id, reply_id),
  constraint mail_ingest_dedup_mailbox_fk foreign key (workspace_id, mailbox_binding_id)
    references ops.mail_worker_bindings (workspace_id, id),
  constraint mail_ingest_dedup_credential_fk foreign key (workspace_id, credential_id)
    references ops.api_credentials (workspace_id, id),
  constraint mail_ingest_dedup_reply_fk foreign key (workspace_id, mailbox_binding_id, reply_id)
    references app.seller_replies (workspace_id, mailbox_binding_id, id),
  constraint mail_ingest_dedup_inquiry_reply_fk foreign key (workspace_id, inquiry_id, reply_id)
    references app.seller_replies (workspace_id, inquiry_id, id),
  constraint mail_ingest_dedup_conflict_reply_fk foreign key (workspace_id, mailbox_binding_id, last_conflict_reply_id)
    references app.seller_replies (workspace_id, mailbox_binding_id, id),
  constraint mail_ingest_dedup_kind_ck check (dedup_kind in ('internet_message_id', 'provider_message_id', 'content_hash')),
  -- domain.replies.ReplyDedupKey.as_string(): "<mailbox>:<kind>:<value>".
  constraint mail_ingest_dedup_key_ck check (
    pg_catalog.length(dedup_key) between 1 and 1200
    and pg_catalog.starts_with(dedup_key, mailbox_binding_id::text || ':' || dedup_kind || ':')
    and pg_catalog.length(dedup_key) > pg_catalog.length(mailbox_binding_id::text || ':' || dedup_kind || ':')
    and dedup_key !~ '[[:cntrl:]]'),
  constraint mail_ingest_dedup_idempotency_ck check (idempotency_key ~ '^[\x21-\x7e]{8,128}$'),
  constraint mail_ingest_dedup_fingerprint_ck check (
    fingerprint ~ '^[0-9a-f]{64}$'
    and pg_catalog.length(fingerprint_version) between 1 and 80
    and (last_conflict_fingerprint is null or last_conflict_fingerprint ~ '^[0-9a-f]{64}$')),
  constraint mail_ingest_dedup_result_ck check (ingest_result in ('stored', 'quarantined')),
  constraint mail_ingest_dedup_counts_ck check (duplicate_count >= 0 and conflict_count >= 0),
  constraint mail_ingest_dedup_conflict_ck check (
    (conflict_count = 0 and last_conflict_at is null and last_conflict_fingerprint is null
     and last_conflict_reply_id is null)
    or (conflict_count > 0 and last_conflict_at is not null and last_conflict_fingerprint is not null)),
  constraint mail_ingest_dedup_seen_ck check (last_seen_at >= first_ingested_at)
);
create index mail_ingest_dedup_credential_idx on ops.mail_ingest_dedup (workspace_id, credential_id);
create index mail_ingest_dedup_inquiry_idx on ops.mail_ingest_dedup (workspace_id, inquiry_id, reply_id);
create index mail_ingest_dedup_conflict_idx
  on ops.mail_ingest_dedup (workspace_id, mailbox_binding_id, last_conflict_reply_id)
  where last_conflict_reply_id is not null;

-- Reconciliation checkpoints per mailbox/store/folder (spec 37.6). Advanced only
-- after candidate replies and upload-queue entries are durably committed.
create table ops.mail_worker_checkpoints (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  mailbox_binding_id uuid not null,
  store_id_hash text not null,
  folder_id_hash text not null,
  folder_role text not null default 'other',
  cursor text,
  overlap_watermark timestamptz,
  last_complete_scan_at timestamptz,
  last_scan_started_at timestamptz,
  heartbeat_at timestamptz,
  backlog_count integer,
  backlog_oldest_at timestamptz,
  outlook_connected boolean,
  mailbox_sync_ok boolean,
  mailbox_last_sync_at timestamptz,
  gap_reasons text[] not null default '{}',
  row_version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint mail_worker_checkpoints_workspace_id_uk unique (workspace_id, id),
  constraint mail_worker_checkpoints_folder_uk unique (workspace_id, mailbox_binding_id, store_id_hash, folder_id_hash),
  constraint mail_worker_checkpoints_mailbox_fk foreign key (workspace_id, mailbox_binding_id)
    references ops.mail_worker_bindings (workspace_id, id),
  constraint mail_worker_checkpoints_hashes_ck check (
    store_id_hash ~ '^[0-9a-f]{64}$' and folder_id_hash ~ '^[0-9a-f]{64}$'),
  constraint mail_worker_checkpoints_role_ck check (folder_role in (
    'inbox', 'sent_items', 'outbox', 'junk', 'rule_target', 'other')),
  constraint mail_worker_checkpoints_cursor_ck check (cursor is null or app.opaque_ref_ok(cursor, 1024)),
  constraint mail_worker_checkpoints_backlog_ck check (
    (backlog_count is null or backlog_count >= 0)
    and (backlog_oldest_at is null or coalesce(backlog_count, 0) > 0)),
  constraint mail_worker_checkpoints_gaps_ck check (app.text_array_ok(gap_reasons, 30, 120)),
  constraint mail_worker_checkpoints_row_version_ck check (row_version > 0)
);
create index mail_worker_checkpoints_heartbeat_idx on ops.mail_worker_checkpoints (workspace_id, heartbeat_at);

-- Per-mailbox monotonically increasing binding change log for
-- GET /v1/mail-workers/inquiry-bindings (cursor = sequence). The sequence is
-- allocated under the mailbox row lock, so commit order equals sequence order
-- and a cursor never skips a change. Tombstones revoke stale local bindings.
create table ops.mail_binding_sync (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  mailbox_binding_id uuid not null,
  sequence bigint not null,
  inquiry_id uuid not null,
  binding_version integer not null,
  binding_state text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint mail_binding_sync_workspace_id_uk unique (workspace_id, id),
  constraint mail_binding_sync_sequence_uk unique (workspace_id, mailbox_binding_id, sequence),
  constraint mail_binding_sync_version_uk unique (workspace_id, mailbox_binding_id, inquiry_id, binding_version),
  constraint mail_binding_sync_mailbox_fk foreign key (workspace_id, mailbox_binding_id)
    references ops.mail_worker_bindings (workspace_id, id),
  constraint mail_binding_sync_inquiry_fk foreign key (workspace_id, inquiry_id)
    references app.seller_inquiries (workspace_id, id),
  constraint mail_binding_sync_sequence_ck check (sequence >= 1),
  constraint mail_binding_sync_version_ck check (binding_version >= 1),
  constraint mail_binding_sync_state_ck check (binding_state in ('active', 'suppressed', 'uncertain', 'tombstoned')),
  constraint mail_binding_sync_payload_ck check (
    pg_catalog.jsonb_typeof(payload) = 'object' and pg_catalog.octet_length(payload::text) <= 16384
    and (binding_state <> 'tombstoned' or payload = '{}'::jsonb))
);
create index mail_binding_sync_inquiry_idx on ops.mail_binding_sync (workspace_id, inquiry_id, binding_version desc);

-- =============================================================================
-- Suppressions and availability evidence
-- =============================================================================

-- Seller/address/vehicle/source/sender/workspace suppressions (spec 37.5).
-- Never removed automatically: removal needs a non-system principal, a reason
-- and an audit event about this suppression.
create table ops.email_suppressions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  scope text not null,
  scope_key text not null,
  -- Address suppressions match case-insensitively (the fail-closed direction).
  match_key text generated always as (
    case when scope = 'address' then pg_catalog.lower(scope_key) else scope_key end) stored,
  reason text not null,
  effective_at timestamptz not null default now(),
  evidence jsonb not null default '{}'::jsonb,
  inquiry_id uuid,
  reply_id uuid,
  created_by_principal_id uuid,
  created_by_kind text not null,
  removed_at timestamptz,
  removed_by_principal_id uuid,
  removed_by_kind text,
  removal_reason text,
  removal_audit_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint email_suppressions_workspace_id_uk unique (workspace_id, id),
  constraint email_suppressions_inquiry_fk foreign key (workspace_id, inquiry_id)
    references app.seller_inquiries (workspace_id, id),
  constraint email_suppressions_reply_fk foreign key (workspace_id, reply_id)
    references app.seller_replies (workspace_id, id),
  constraint email_suppressions_audit_fk foreign key (workspace_id, removal_audit_id)
    references ops.audit_events (workspace_id, id),
  constraint email_suppressions_scope_ck check (scope in ('workspace', 'seller', 'address', 'vehicle', 'source', 'sender')),
  constraint email_suppressions_key_ck check (
    pg_catalog.length(scope_key) between 1 and 320 and scope_key !~ '[[:space:][:cntrl:]]'
    and case scope
      when 'workspace' then scope_key = '*' or scope_key = workspace_id::text
      when 'seller' then scope_key ~ '^seller_(entity|alias):[0-9a-f-]{32,64}$'
      when 'address' then app.email_address_ok(pg_catalog.lower(scope_key))
      when 'vehicle' then scope_key ~ '^(vehicle_cluster|listing_incarnation):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      when 'source' then scope_key ~ '^[a-z0-9_]{3,60}$'
      when 'sender' then scope_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      else false
    end),
  constraint email_suppressions_reason_ck check (reason in (
    'hard_bounce', 'complaint', 'seller_opt_out', 'source_paused', 'sender_revoked',
    'unresolved_send_outcome', 'kill_switch', 'contradictory_availability', 'manual')),
  constraint email_suppressions_evidence_ck check (
    pg_catalog.jsonb_typeof(evidence) = 'object' and pg_catalog.octet_length(evidence::text) <= 16384),
  constraint email_suppressions_creator_ck check (created_by_kind in ('user', 'mcp_client', 'system')),
  -- Removal is explicit, audited and never automatic (no 'system' remover).
  constraint email_suppressions_removal_ck check (
    (removed_at is null and removed_by_principal_id is null and removed_by_kind is null
     and removal_reason is null and removal_audit_id is null)
    or (removed_at is not null and removed_at >= effective_at and removed_by_principal_id is not null
        and removed_by_kind is not null and removed_by_kind in ('user', 'mcp_client')
        and removal_reason is not null and pg_catalog.length(removal_reason) between 3 and 2000
        and removal_audit_id is not null))
);
create unique index email_suppressions_active_uidx
  on ops.email_suppressions (workspace_id, scope, match_key, reason) where removed_at is null;
create index email_suppressions_match_idx
  on ops.email_suppressions (workspace_id, scope, match_key) where removed_at is null;
create index email_suppressions_inquiry_idx on ops.email_suppressions (workspace_id, inquiry_id) where inquiry_id is not null;
create index email_suppressions_reply_idx on ops.email_suppressions (workspace_id, reply_id) where reply_id is not null;
create index email_suppressions_audit_idx
  on ops.email_suppressions (workspace_id, removal_audit_id) where removal_audit_id is not null;

-- Availability claims and observations across sites (spec 37.9). Canonical
-- listings.availability values only; the reason is a label, not a new status.
-- Nothing here establishes a purchase, a buyer or a transaction price.
create table app.availability_events (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  source_id uuid not null,
  listing_id uuid not null,
  vehicle_cluster_id uuid,
  old_availability text not null,
  new_availability text not null,
  evidence_kind text not null,
  reason text not null,
  crawl_run_id uuid,
  listing_observation_id uuid,
  detail_observation_id uuid,
  reply_id uuid,
  manual_principal_id uuid,
  source_reference text,
  effective_at timestamptz not null,
  observed_at timestamptz not null,
  confidence text not null,
  promote_current boolean not null default true,
  historical_only boolean not null default false,
  conflicts_with_current boolean not null default false,
  created_at timestamptz not null default now(),
  constraint availability_events_workspace_id_uk unique (workspace_id, id),
  constraint availability_events_listing_fk foreign key (workspace_id, source_id, listing_id)
    references app.listings (workspace_id, source_id, id),
  constraint availability_events_cluster_fk foreign key (workspace_id, vehicle_cluster_id)
    references app.vehicle_clusters (workspace_id, id),
  constraint availability_events_run_fk foreign key (workspace_id, source_id, crawl_run_id)
    references ops.crawl_runs (workspace_id, source_id, id),
  constraint availability_events_observation_fk foreign key (workspace_id, listing_observation_id)
    references app.listing_observations (workspace_id, id),
  constraint availability_events_detail_fk foreign key (workspace_id, detail_observation_id)
    references app.detail_observations (workspace_id, id),
  constraint availability_events_reply_fk foreign key (workspace_id, reply_id)
    references app.seller_replies (workspace_id, id),
  constraint availability_events_values_ck check (
    old_availability in ('available', 'reserved', 'removed', 'sold_claimed', 'unknown')
    and new_availability in ('available', 'reserved', 'removed', 'sold_claimed', 'unknown')),
  constraint availability_events_evidence_kind_ck check (evidence_kind in (
    'source_observation', 'source_sold_badge', 'source_removed_page', 'seller_reported_sold',
    'seller_reported_available', 'seller_reported_reserved', 'complete_scan_absence', 'manual')),
  -- Evidence determines the canonical value (spec 37.9): absence is never a sale.
  constraint availability_events_mapping_ck check (
    case evidence_kind
      when 'source_observation' then new_availability in ('available', 'reserved', 'unknown')
      when 'source_sold_badge' then new_availability = 'sold_claimed'
      when 'source_removed_page' then new_availability = 'removed'
      when 'seller_reported_sold' then new_availability = 'sold_claimed'
      when 'seller_reported_available' then new_availability = 'available'
      when 'seller_reported_reserved' then new_availability = 'reserved'
      when 'complete_scan_absence' then new_availability = 'unknown'
      else true
    end),
  constraint availability_events_reason_ck check (reason ~ '^[a-z][a-z0-9_]{0,79}$'),
  -- An absence label is only ever backed by a complete scan (never a lone missing result).
  constraint availability_events_absence_reason_ck check (
    reason <> 'not_seen_in_complete_scan' or evidence_kind = 'complete_scan_absence'),
  constraint availability_events_reference_ck check (
    case
      when evidence_kind in ('seller_reported_sold', 'seller_reported_available', 'seller_reported_reserved')
        then reply_id is not null
      when evidence_kind = 'manual' then manual_principal_id is not null
      when evidence_kind = 'complete_scan_absence' then crawl_run_id is not null
      else crawl_run_id is not null or listing_observation_id is not null or detail_observation_id is not null
           or source_reference is not null
    end),
  constraint availability_events_source_reference_ck check (
    source_reference is null
    or (pg_catalog.length(source_reference) between 1 and 200 and source_reference !~ '[[:cntrl:]]')),
  constraint availability_events_confidence_ck check (confidence in ('high', 'medium', 'low')),
  constraint availability_events_promotion_ck check (
    not promote_current or (not historical_only and not conflicts_with_current))
);
create index availability_events_listing_idx on app.availability_events (workspace_id, listing_id, effective_at desc);
create index availability_events_source_listing_idx on app.availability_events (workspace_id, source_id, listing_id);
create index availability_events_cluster_idx
  on app.availability_events (workspace_id, vehicle_cluster_id, effective_at desc) where vehicle_cluster_id is not null;
create index availability_events_run_idx
  on app.availability_events (workspace_id, source_id, crawl_run_id) where crawl_run_id is not null;
create index availability_events_observation_idx
  on app.availability_events (workspace_id, listing_observation_id) where listing_observation_id is not null;
create index availability_events_detail_idx
  on app.availability_events (workspace_id, detail_observation_id) where detail_observation_id is not null;
create index availability_events_reply_idx on app.availability_events (workspace_id, reply_id) where reply_id is not null;
create index availability_events_conflict_idx
  on app.availability_events (workspace_id, observed_at) where conflicts_with_current;

-- =============================================================================
-- API credential scopes (mirror domain.actor.ROLE_SCOPES, spec 37.8)
-- =============================================================================
-- inquiries:read for owner and reviewer, inquiries:pause for owner (may be
-- narrowly granted to dot through an owner-role credential), and mail:ingest
-- only for owner-role credentials that carry NO other scope (mailbox worker).
-- Widening checks: existing rows satisfy them; NOT VALID + VALIDATE keeps the
-- swap explicit and the constraint names stable.
alter table ops.api_credentials drop constraint if exists api_credentials_scopes_ck;
alter table ops.api_credentials
  add constraint api_credentials_scopes_ck check (
    pg_catalog.cardinality(scopes) between 1 and 11
    and app.text_array_ok(scopes, 11, 40)
    and scopes <@ array['deals:read', 'reviews:read', 'reviews:write', 'events:subscribe',
                        'rechecks:request', 'notes:write', 'sources:pause', 'config:admin',
                        'inquiries:read', 'inquiries:pause', 'mail:ingest']::text[]) not valid;
alter table ops.api_credentials validate constraint api_credentials_scopes_ck;

alter table ops.api_credentials drop constraint if exists api_credentials_role_scopes_ck;
alter table ops.api_credentials
  add constraint api_credentials_role_scopes_ck check (
    role = 'owner'
    or (role = 'reviewer'
        and scopes <@ array['deals:read', 'reviews:read', 'reviews:write', 'events:subscribe',
                            'rechecks:request', 'notes:write', 'inquiries:read']::text[])
    or (role = 'viewer' and scopes <@ array['deals:read', 'reviews:read']::text[])) not valid;
alter table ops.api_credentials validate constraint api_credentials_role_scopes_ck;

alter table ops.api_credentials drop constraint if exists api_credentials_mail_ingest_ck;
alter table ops.api_credentials
  add constraint api_credentials_mail_ingest_ck check (
    not ('mail:ingest' = any (scopes))
    or (scopes = array['mail:ingest']::text[] and role = 'owner')) not valid;
alter table ops.api_credentials validate constraint api_credentials_mail_ingest_ck;

-- =============================================================================
-- Guard functions
-- =============================================================================

-- Seller entities: a merge is one-way and targets an unmerged root; an entity
-- that already absorbed others cannot itself be merged (no chains).
create or replace function app.seller_entities_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.id <> old.id or new.workspace_id <> old.workspace_id or new.created_at <> old.created_at then
    raise exception using errcode = 'SV004', message = 'seller entity identity is immutable';
  end if;
  if old.merged_into_id is null and new.merged_into_id is not null then
    -- Serialise the merge with reservations/dispatches of the surviving entity (they lock
    -- its row), so a merge can never slip between a one-inquiry check and its commit.
    perform 1 from app.seller_entities t
      where t.workspace_id = new.workspace_id and t.id = new.merged_into_id
      for update;
  end if;
  if old.merged_into_id is not null
     and (new.merged_into_id is distinct from old.merged_into_id
          or new.merged_at is distinct from old.merged_at
          or new.merge_reason is distinct from old.merge_reason) then
    raise exception using errcode = 'SV004', message = 'a seller entity merge is permanent';
  end if;
  if old.merged_into_id is null and new.merged_into_id is not null then
    if exists (
         select 1 from app.seller_entities t
          where t.workspace_id = new.workspace_id and t.id = new.merged_into_id
            and t.merged_into_id is not null) then
      raise exception using
        errcode = 'SV003',
        message = 'a seller entity can only be merged into an unmerged root entity';
    end if;
    if exists (
         select 1 from app.seller_entities c
          where c.workspace_id = new.workspace_id and c.merged_into_id = new.id) then
      raise exception using
        errcode = 'SV003',
        message = 'a seller entity that absorbed other entities cannot be merged itself';
    end if;
  end if;
  if new.row_version < old.row_version then
    raise exception using errcode = 'SV005', message = 'seller entity row_version must not decrease';
  end if;
  return new;
end
$$;

-- Contacts: evidence is immutable; only status, reasons, recheck time and the
-- change marker move. 'changed' is terminal; verified_at is set once.
create or replace function app.seller_contacts_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  mutable constant text[] := array[
    'status', 'status_reasons', 'last_rechecked_at', 'changed_at', 'superseded_by_id', 'verified_at',
    'updated_at', 'address_domain'];
  changed text[];
begin
  select pg_catalog.array_agg(n.key order by n.key)
    into changed
    from pg_catalog.jsonb_each(pg_catalog.to_jsonb(new)) as n
    join pg_catalog.jsonb_each(pg_catalog.to_jsonb(old)) as o on o.key = n.key
   where n.value is distinct from o.value
     and not (n.key = any (mutable));
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = 'seller contact evidence is immutable (changed: '
             || pg_catalog.array_to_string(changed, ', ') || '); record new evidence instead';
  end if;
  if old.verified_at is not null and new.verified_at is distinct from old.verified_at then
    raise exception using errcode = 'SV004', message = 'seller contact verified_at is set once';
  end if;
  if new.status is distinct from old.status and old.status = 'changed' then
    raise exception using
      errcode = 'SV002',
      message = 'a changed (superseded) seller contact cannot become current again';
  end if;
  if old.changed_at is not null and new.changed_at is distinct from old.changed_at then
    raise exception using errcode = 'SV004', message = 'seller contact changed_at is set once';
  end if;
  return new;
end
$$;

-- Controls: every accepted change advances the optimistic version by exactly one.
create or replace function app.seller_inquiry_controls_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.workspace_id <> old.workspace_id or new.id <> old.id or new.created_at <> old.created_at then
    raise exception using errcode = 'SV004', message = 'seller inquiry controls identity is immutable';
  end if;
  if (pg_catalog.to_jsonb(new) - array['updated_at', 'version']) is distinct from
     (pg_catalog.to_jsonb(old) - array['updated_at', 'version']) then
    if new.version <> old.version + 1 then
      raise exception using
        errcode = 'SV005',
        message = pg_catalog.format(
          'seller inquiry controls changed without advancing the version by one (%s -> %s)',
          old.version, new.version);
    end if;
  elsif new.version < old.version then
    raise exception using errcode = 'SV005', message = 'seller inquiry controls version must not decrease';
  end if;
  return new;
end
$$;

-- Sender bindings: provider/account/From are the identity; revocation is
-- permanent; any binding-relevant change advances the version (the inquiry
-- dispatch check then sees a changed sender and refuses).
create or replace function ops.email_sender_bindings_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.workspace_id <> old.workspace_id or new.provider <> old.provider
     or new.account_id <> old.account_id or new.from_address <> old.from_address
     or new.created_at <> old.created_at or new.created_by is distinct from old.created_by then
    raise exception using
      errcode = 'SV004',
      message = 'sender binding identity (provider, account, From) is immutable; create a new binding';
  end if;
  if old.revoked_at is not null
     and (pg_catalog.to_jsonb(new) - 'updated_at') is distinct from (pg_catalog.to_jsonb(old) - 'updated_at') then
    raise exception using errcode = 'SV004', message = 'a revoked sender binding is frozen';
  end if;
  if new.version < old.version then
    raise exception using errcode = 'SV005', message = 'sender binding version must not decrease';
  end if;
  if (new.display_name, new.reply_to_address, new.alias_verified, new.alias_verified_at, new.secret_envelope,
      new.secret_reference, new.verified_at, new.verified_by)
     is distinct from
     (old.display_name, old.reply_to_address, old.alias_verified, old.alias_verified_at, old.secret_envelope,
      old.secret_reference, old.verified_at, old.verified_by)
     and new.version <= old.version then
    raise exception using
      errcode = 'SV005',
      message = 'a sender binding change must advance its version';
  end if;
  return new;
end
$$;

-- Active (not removed) suppressions that apply to an inquiry: its seller (and
-- the entities merged into it), recipient address (case-insensitive), vehicle
-- (the identity key, the qualifying listing incarnation, every active member of
-- its cluster, and any confirmed cluster containing the listing - so neither a
-- relisting nor an identity merge escapes a vehicle suppression), source,
-- sender account or the whole workspace. Effective time is not awaited (fail closed).
create or replace function ops.seller_inquiry_active_suppressions(
  p_workspace_id uuid, p_seller_entity_id uuid, p_address text, p_vehicle_key text, p_listing_id uuid,
  p_vehicle_cluster_id uuid, p_source_key text, p_sender_binding_id uuid)
returns text[]
language sql
stable
set search_path = ''
as $$
  select coalesce(pg_catalog.array_agg(distinct s.scope || ':' || s.reason), array[]::text[])
    from ops.email_suppressions s
   where s.workspace_id = p_workspace_id
     and s.removed_at is null
     and (
       (s.scope = 'workspace' and s.match_key in ('*', p_workspace_id::text))
       or (s.scope = 'seller' and s.match_key in (
             select 'seller_entity:' || e.id::text
               from app.seller_entities e
              where e.workspace_id = p_workspace_id
                and (e.id = p_seller_entity_id or e.merged_into_id = p_seller_entity_id)))
       or (s.scope = 'address' and s.match_key = pg_catalog.lower(p_address))
       or (s.scope = 'vehicle' and (
             s.match_key = p_vehicle_key
             or s.match_key = 'listing_incarnation:' || p_listing_id::text
             or s.match_key in (
                  select 'listing_incarnation:' || m.listing_id::text
                    from app.vehicle_cluster_members m
                   where m.workspace_id = p_workspace_id and m.cluster_id = p_vehicle_cluster_id
                     and m.unlinked_at is null)
             or s.match_key in (
                  select 'vehicle_cluster:' || m.cluster_id::text
                    from app.vehicle_cluster_members m
                   where m.workspace_id = p_workspace_id and m.listing_id = p_listing_id
                     and m.unlinked_at is null)))
       or (s.scope = 'source' and s.match_key = p_source_key)
       or (s.scope = 'sender' and s.match_key = p_sender_binding_id::text)
     )
$$;
comment on function ops.seller_inquiry_active_suppressions(uuid, uuid, text, text, uuid, uuid, text, uuid) is
  'Active suppressions (scope:reason) matching an inquiry; used by the reservation/dispatch guards.';

-- The canonical vehicle identity rule (domain.inquiries.canonical_vehicle_identity):
-- a confirmed cluster that contains the qualifying listing, else the listing
-- incarnation itself - never a listing that belongs to a confirmed cluster.
-- The seller must be an unmerged (surviving) entity.
create or replace function app.seller_inquiry_assert_identity(
  p_workspace_id uuid, p_vehicle_kind text, p_vehicle_cluster_id uuid, p_qualification_listing_id uuid,
  p_seller_entity_id uuid)
returns void
language plpgsql
stable
set search_path = ''
as $$
begin
  if exists (
       select 1 from app.seller_entities e
        where e.workspace_id = p_workspace_id and e.id = p_seller_entity_id and e.merged_into_id is not null) then
    raise exception using
      errcode = 'SV003',
      message = 'the inquiry seller entity was merged; the identity must use the surviving entity';
  end if;
  if p_vehicle_kind = 'vehicle_cluster' then
    if not exists (
         select 1
           from app.vehicle_clusters c
           join app.vehicle_cluster_members m
             on m.workspace_id = c.workspace_id and m.cluster_id = c.id
          where c.workspace_id = p_workspace_id and c.id = p_vehicle_cluster_id
            and c.review_status = 'confirmed'
            and m.listing_id = p_qualification_listing_id and m.unlinked_at is null) then
      raise exception using
        errcode = 'SV003',
        message = 'a cluster identity needs a confirmed vehicle cluster containing the qualifying listing';
    end if;
  elsif exists (
       select 1
         from app.vehicle_cluster_members m
         join app.vehicle_clusters c on c.workspace_id = m.workspace_id and c.id = m.cluster_id
        where m.workspace_id = p_workspace_id and m.listing_id = p_qualification_listing_id
          and m.unlinked_at is null and c.review_status = 'confirmed') then
    raise exception using
      errcode = 'SV003',
      message = 'the listing belongs to a confirmed vehicle cluster; the inquiry identity must use the cluster';
  end if;
end
$$;

-- Rolling-window quota usage (domain.inquiries.QuotaDebit.counted_at): every unreleased debit
-- occupies the windows at the later of its reservation and its inquiry's send attempt, so an
-- inquiry reserved days ago and transmitted now counts now. Re-checked before every
-- transmission, this keeps SENDS (not only reservations) within the caps: a queued backlog
-- (sender offline, owner pause) can never leave in a burst. Future-dated debits count.
create or replace function ops.inquiry_quota_usage(
  p_workspace_id uuid, p_exclude_inquiry_id uuid, out count_24h integer, out count_15d integer)
language sql
stable
set search_path = ''
as $$
  select (pg_catalog.count(*) filter (where u.counted_at > pg_catalog.now() - interval '24 hours'))::integer,
         (pg_catalog.count(*) filter (where u.counted_at > pg_catalog.now() - interval '15 days'))::integer
    from (select greatest(q.debited_at, i.send_attempted_at) as counted_at
            from ops.inquiry_quota_ledger q
            join app.seller_inquiries i on i.workspace_id = q.workspace_id and i.id = q.inquiry_id
           where q.workspace_id = p_workspace_id
             and q.released_at is null
             and q.inquiry_id is distinct from p_exclude_inquiry_id) as u
$$;
comment on function ops.inquiry_quota_usage(uuid, uuid) is
  'Unreleased quota debits in the rolling 24 h / 15 day windows, counted at max(reservation, send attempt).';

-- One initial inquiry per ACTUAL vehicle/seller pair (spec 37.1, 37.5), across identity and
-- seller merges: the identity key is unique, but a listing-identity inquiry sent before its
-- cluster was confirmed, or an inquiry to an entity later merged into this seller, is the same
-- pair under another key. Returns the first other inquiry of this seller (or of an entity
-- merged into it) about this vehicle that blocks the phase, else NULL.
-- The vehicle is the qualifying listing, the cluster's active members, and every active member
-- of a confirmed OR unreviewed cluster containing the listing (a plausible cross-site duplicate
-- is held until resolved; a rejected cluster or an unlinked member is not the same car).
-- 'reserve' is blocked by any reserved, queued or (possibly) transmitted inquiry (merge
-- reconciliation must cancel an unsent one first); 'dispatch' by a (possibly) transmitted one.
create or replace function app.seller_inquiry_vehicle_conflict(p app.seller_inquiries, p_phase text)
returns uuid
language sql
stable
set search_path = ''
as $$
  with vehicle_listings as (
    select p.qualification_listing_id as listing_id
    union
    select m.listing_id
      from app.vehicle_cluster_members m
     where m.workspace_id = p.workspace_id and m.cluster_id = p.vehicle_cluster_id and m.unlinked_at is null
    union
    select m2.listing_id
      from app.vehicle_cluster_members m1
      join app.vehicle_clusters c
        on c.workspace_id = m1.workspace_id and c.id = m1.cluster_id and c.review_status <> 'rejected'
      join app.vehicle_cluster_members m2
        on m2.workspace_id = m1.workspace_id and m2.cluster_id = m1.cluster_id and m2.unlinked_at is null
     where m1.workspace_id = p.workspace_id and m1.listing_id = p.qualification_listing_id
       and m1.unlinked_at is null
  )
  select o.id
    from app.seller_inquiries o
    join app.seller_entities e on e.workspace_id = o.workspace_id and e.id = o.seller_entity_id
   where o.workspace_id = p.workspace_id
     and o.id <> p.id
     and (o.seller_entity_id = p.seller_entity_id or e.merged_into_id = p.seller_entity_id)
     and (o.send_attempted_at is not null
          or (p_phase = 'reserve'
              and o.state in ('reserved', 'queued', 'sending', 'accepted', 'uncertain', 'failed_definite',
                              'no_reply_yet', 'replied', 'bounced', 'seller_opted_out')))
     and (o.qualification_listing_id in (select v.listing_id from vehicle_listings v)
          or o.vehicle_listing_id in (select v.listing_id from vehicle_listings v)
          or o.vehicle_cluster_id = p.vehicle_cluster_id
          or exists (
               select 1
                 from app.vehicle_cluster_members om
                where om.workspace_id = o.workspace_id and om.cluster_id = o.vehicle_cluster_id
                  and om.unlinked_at is null
                  and om.listing_id in (select v.listing_id from vehicle_listings v)))
   order by o.created_at, o.id
   limit 1
$$;
comment on function app.seller_inquiry_vehicle_conflict(app.seller_inquiries, text) is
  'Another inquiry of the same seller (family) about the same (or a plausibly same) vehicle that blocks reserve/dispatch.';

-- Re-validation immediately before reservation ('reserve'), queueing ('queue')
-- and transmission ('dispatch'): spec 37.2 check 6 and 37.5. Raises SV002 for
-- a refused flow and SV003 for an inconsistent reference. Lock order (after the
-- inquiry row itself): seller_inquiry_controls FOR UPDATE (every phase: it
-- serialises reservations, dispatches, the quota ledger and a concurrent pause
-- without share-to-exclusive upgrades) -> seller_entities FOR UPDATE (reserve
-- and dispatch: serialises one seller's sends and seller merges).
create or replace function app.seller_inquiry_preflight(p app.seller_inquiries, p_phase text)
returns void
language plpgsql
set search_path = ''
as $$
declare
  v_mode text;
  v_kill boolean;
  v_cooldown interval;
  v_max_24h smallint;
  v_max_15d smallint;
  v_usage record;
  v_conflict uuid;
  v_latest integer;
  v_auth_revoked timestamptz;
  v_auth_effective date;
  v_auth_languages text[];
  v_auth_profiles text[];
  v_found boolean;
  v_listing record;
  v_revision record;
  v_source record;
  v_sender record;
  v_contact record;
  v_hits text[];
  v_recent integer;
begin
  if p_phase not in ('reserve', 'queue', 'dispatch') then
    raise exception using errcode = 'invalid_parameter_value', message = 'unknown preflight phase';
  end if;

  -- 1. Workspace controls: kill switch and mode. Locked FOR UPDATE, so a
  --    concurrent pause either waits for this transaction or is seen by it, and
  --    two dispatches can never both take the last slot of a rolling window.
  select c.mode, c.kill_switch, c.seller_cooldown, c.max_per_24h, c.max_per_15d
    into v_mode, v_kill, v_cooldown, v_max_24h, v_max_15d
    from app.seller_inquiry_controls c
   where c.workspace_id = p.workspace_id
     for update;
  if v_mode is null then
    raise exception using errcode = 'SV002', message = 'seller inquiry controls are not initialised for this workspace';
  end if;
  if v_kill then
    raise exception using errcode = 'SV002', message = 'the seller inquiry kill switch is active';
  end if;
  if v_mode <> 'automatic' then
    raise exception using
      errcode = 'SV002',
      message = pg_catalog.format('automatic seller inquiries are not enabled (mode %s)', v_mode);
  end if;
  if p_phase = 'queue' then
    return;
  end if;

  -- 2. Standing authorization: the bound version is the current, effective,
  --    unrevoked one and covers the language and the listing's profile.
  select pg_catalog.max(a.version) into v_latest
    from app.seller_inquiry_authorizations a
   where a.workspace_id = p.workspace_id;
  select a.revoked_at, a.effective_date, a.languages, a.profiles_in_scope, true
    into v_auth_revoked, v_auth_effective, v_auth_languages, v_auth_profiles, v_found
    from app.seller_inquiry_authorizations a
   where a.workspace_id = p.workspace_id and a.id = p.authorization_id and a.version = p.authorization_version;
  if v_found is null or v_latest is distinct from p.authorization_version then
    raise exception using
      errcode = 'SV002',
      message = 'the inquiry is not bound to the current seller inquiry authorization version';
  end if;
  if v_auth_revoked is not null then
    raise exception using errcode = 'SV002', message = 'the seller inquiry authorization is revoked';
  end if;
  if v_auth_effective > (pg_catalog.now() at time zone 'UTC')::date then
    raise exception using errcode = 'SV002', message = 'the seller inquiry authorization is not yet effective';
  end if;
  if p.language is null or not coalesce(p.language = any (v_auth_languages), false) then
    raise exception using errcode = 'SV002', message = 'the inquiry language is not covered by the authorization';
  end if;

  -- 3. Identity: surviving seller entity, canonical vehicle identity, and one inquiry
  --    per actual vehicle/seller pair. The seller row is locked first (a concurrent merge
  --    into it, or another reservation/dispatch for it, waits; later statements see them).
  perform 1 from app.seller_entities e
    where e.workspace_id = p.workspace_id and e.id = p.seller_entity_id
    for update;
  perform app.seller_inquiry_assert_identity(
    p.workspace_id, p.vehicle_kind, p.vehicle_cluster_id, p.qualification_listing_id, p.seller_entity_id);
  v_conflict := app.seller_inquiry_vehicle_conflict(p, p_phase);
  if v_conflict is not null then
    raise exception using
      errcode = 'SV002',
      message = 'this seller already has an inquiry about this vehicle (possibly under another listing,'
             || ' cluster or merged seller identity); one initial inquiry per vehicle/seller pair';
  end if;

  -- 4. Listing and source facts (hard rules stay with screening; this re-checks them).
  select l.source_id, l.availability, l.current_revision_id, l.quarantined, l.identity_conflict,
         l.eligibility_state, l.eligibility_profile
    into v_listing
    from app.listings l
   where l.workspace_id = p.workspace_id and l.id = p.qualification_listing_id;
  if v_listing.quarantined is distinct from false or v_listing.identity_conflict is distinct from false then
    raise exception using errcode = 'SV002', message = 'the qualifying listing is quarantined or has an identity conflict';
  end if;
  if v_listing.availability in ('sold_claimed', 'removed') then
    raise exception using errcode = 'SV002', message = 'the vehicle is reported sold or removed';
  end if;
  if v_listing.eligibility_state is null
     or v_listing.eligibility_state not in ('eligible_primary', 'eligible_manual_profile')
     or v_listing.eligibility_profile is null
     or not (v_listing.eligibility_profile = any (v_auth_profiles)) then
    raise exception using
      errcode = 'SV002',
      message = 'the qualifying listing is not eligible under a profile covered by the authorization';
  end if;
  -- The bound qualification facts are still the listing's current facts (a changed price
  -- or availability cancels stale work), and the snapshot is exactly the bound revision.
  -- (An incomplete binding is reported by seller_inquiries_binding_complete_ck.)
  if p.qualification_revision_id is not null
     and v_listing.current_revision_id is distinct from p.qualification_revision_id then
    raise exception using
      errcode = 'SV002',
      message = 'the listing changed since qualification; cancel the stale inquiry';
  end if;
  if p.qualified_availability is not null
     and (v_listing.availability is distinct from p.qualified_availability or v_listing.availability = 'reserved') then
    raise exception using
      errcode = 'SV002',
      message = 'the listing availability changed since qualification; cancel the stale inquiry';
  end if;
  if p.qualification_revision_id is not null then
    select r.revision_number, r.semantic_hash, r.asking_minor, r.currency, r.quarantined
      into v_revision
      from app.listing_revisions r
     where r.workspace_id = p.workspace_id and r.listing_id = p.qualification_listing_id
       and r.id = p.qualification_revision_id;
    if v_revision.quarantined is distinct from false
       or (p.qualification_revision_number is not null
           and v_revision.revision_number is distinct from p.qualification_revision_number)
       or (p.qualified_semantic_hash is not null and v_revision.semantic_hash is distinct from p.qualified_semantic_hash)
       or ((p.qualified_price_minor is null) = (p.qualified_currency is null)
           and (v_revision.asking_minor, v_revision.currency)
               is distinct from (p.qualified_price_minor, p.qualified_currency)) then
      raise exception using
        errcode = 'SV003',
        message = 'the qualification snapshot is not the bound listing revision (number, semantic hash, price)';
    end if;
  end if;
  select s.source_key, s.enabled, s.paused
    into v_source
    from app.sources s
   where s.workspace_id = p.workspace_id and s.id = v_listing.source_id;
  if v_source.enabled is distinct from true or v_source.paused is distinct from false then
    raise exception using errcode = 'SV002', message = 'the listing source is disabled or paused';
  end if;

  -- 5. Sender: the exact bound account, verified, healthy, unchanged.
  select b.version, b.provider, b.account_id, b.from_address, b.display_name, b.reply_to_address,
         b.alias_verified, b.verified_at, b.health, b.revoked_at
    into v_sender
    from ops.email_sender_bindings b
   where b.workspace_id = p.workspace_id and b.id = p.sender_binding_id;
  if v_sender.revoked_at is not null then
    raise exception using errcode = 'SV002', message = 'the sender binding is revoked';
  end if;
  if v_sender.alias_verified is distinct from true or v_sender.verified_at is null
     or v_sender.health is distinct from 'healthy' then
    raise exception using errcode = 'SV002', message = 'the sender binding is not verified and healthy';
  end if;
  if v_sender.version is distinct from p.sender_binding_version
     or v_sender.provider is distinct from p.sender_provider
     or v_sender.account_id is distinct from p.sender_account_id
     or v_sender.from_address is distinct from p.sender_from_address
     or v_sender.display_name is distinct from p.sender_display_name
     or v_sender.reply_to_address is distinct from p.sender_reply_to_address then
    raise exception using
      errcode = 'SV002',
      message = 'the sender binding changed since the inquiry was bound; never switch accounts silently';
  end if;

  -- 6. Recipient: verified evidence for this listing and seller (composite FKs),
  --    unchanged address, positively resolved language equal to the template's.
  select c.status, c.address, c.language_status, c.language_code, c.contact_kind
    into v_contact
    from app.seller_contacts c
   where c.workspace_id = p.workspace_id and c.id = p.recipient_contact_id;
  if v_contact.contact_kind = 'official_dealer_contact' and not exists (
       select 1 from app.seller_entities e
        where e.workspace_id = p.workspace_id and e.id = p.seller_entity_id and e.seller_type = 'dealer') then
    raise exception using
      errcode = 'SV002',
      message = 'an official dealer contact is a recipient only for a dealer seller';
  end if;
  if v_contact.status is distinct from 'verified' then
    raise exception using errcode = 'SV002', message = 'the recipient contact is no longer verified';
  end if;
  if v_contact.address is distinct from p.recipient_address then
    raise exception using errcode = 'SV002', message = 'the recipient address differs from the verified contact';
  end if;
  if v_contact.language_status is distinct from 'resolved' or v_contact.language_code is distinct from p.language
     or p.language is null then
    raise exception using
      errcode = 'SV002',
      message = 'the inquiry language is not the positively resolved seller/advertisement language';
  end if;

  -- 7. Suppressions (re-checked at dispatch, not only at queue creation).
  v_hits := ops.seller_inquiry_active_suppressions(
    p.workspace_id, p.seller_entity_id, p.recipient_address,
    p.vehicle_kind || ':' || coalesce(p.vehicle_cluster_id, p.vehicle_listing_id)::text,
    p.qualification_listing_id, p.vehicle_cluster_id, v_source.source_key, p.sender_binding_id);
  if pg_catalog.cardinality(v_hits) > 0 then
    raise exception using
      errcode = 'SV002',
      message = 'an active suppression applies: ' || pg_catalog.array_to_string(v_hits, ', ');
  end if;

  if p_phase = 'reserve' then
    -- 8a. Seller-level cooldown, serialised per seller entity (locked in step 3): no
    --     burst of inquiries to one dealer about several cars through different aliases.
    select pg_catalog.count(*) into v_recent
      from app.seller_inquiries o
      join app.seller_entities e on e.workspace_id = o.workspace_id and e.id = o.seller_entity_id
     where o.workspace_id = p.workspace_id
       and o.id <> p.id
       and (o.seller_entity_id = p.seller_entity_id or e.merged_into_id = p.seller_entity_id)
       and o.state in ('reserved', 'queued', 'failed_definite', 'sending', 'uncertain', 'accepted',
                       'no_reply_yet', 'replied', 'bounced', 'seller_opted_out')
       and coalesce(o.send_attempted_at, o.reserved_at) > pg_catalog.now() - v_cooldown;
    if v_recent > 0 then
      raise exception using
        errcode = 'SV002',
        message = 'seller cooldown: this seller was contacted or reserved recently';
    end if;
  else
    -- 8b. Dispatch: the reservation still holds its quota debit and no earlier
    --     attempt may have reached the provider (never a blind resend).
    if not exists (
         select 1 from ops.inquiry_quota_ledger q
          where q.workspace_id = p.workspace_id and q.inquiry_id = p.id and q.released_at is null) then
      raise exception using errcode = 'SV002', message = 'the inquiry holds no quota debit';
    end if;
    -- The rolling caps are re-checked before every transmission (the controls row is
    -- locked): a backlog reserved while the sender was offline leaves at the cap rate.
    select u.count_24h, u.count_15d into v_usage from ops.inquiry_quota_usage(p.workspace_id, p.id) as u;
    if v_usage.count_24h >= v_max_24h then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('seller inquiry cap reached at dispatch: %s per rolling 24 hours', v_max_24h);
    end if;
    if v_usage.count_15d >= v_max_15d then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('seller inquiry cap reached at dispatch: %s per rolling 15 days', v_max_15d);
    end if;
    -- The seller cooldown is re-checked against (possibly) transmitted inquiries: two
    -- reservations made more than a cooldown apart, or for entities merged since, never
    -- leave together.
    select pg_catalog.count(*) into v_recent
      from app.seller_inquiries o
      join app.seller_entities e on e.workspace_id = o.workspace_id and e.id = o.seller_entity_id
     where o.workspace_id = p.workspace_id
       and o.id <> p.id
       and (o.seller_entity_id = p.seller_entity_id or e.merged_into_id = p.seller_entity_id)
       and o.send_attempted_at > pg_catalog.now() - v_cooldown;
    if v_recent > 0 then
      raise exception using
        errcode = 'SV002',
        message = 'seller cooldown: another inquiry to this seller was transmitted recently';
    end if;
    if exists (
         select 1 from ops.email_delivery_attempts a
          where a.workspace_id = p.workspace_id and a.inquiry_id = p.id
            and (a.outcome in ('running', 'accepted')
                 or a.reconciled_outcome = 'accepted'
                 or (a.outcome = 'uncertain' and a.reconciled_outcome is null))) then
      raise exception using
        errcode = 'SV002',
        message = 'an earlier send attempt is running, accepted or unresolved; it must be reconciled first';
    end if;
  end if;
end
$$;

-- Evidence each state needs at commit (deferred; order-independent within the
-- transaction). Called by constraint triggers on inquiries and attempts.
create or replace function app.seller_inquiry_assert_evidence(p_workspace_id uuid, p_inquiry_id uuid)
returns void
language plpgsql
stable
set search_path = ''
as $$
declare
  v_state text;
  v_has_debit boolean;
  v_attempts integer;
  v_last_outcome text;
  v_last_proof text;
  v_last_reconciled text;
  v_accepted boolean;
  v_correlated boolean;
begin
  select i.state into v_state
    from app.seller_inquiries i
   where i.workspace_id = p_workspace_id and i.id = p_inquiry_id;
  if v_state is null then
    return;
  end if;
  v_has_debit := exists (
    select 1 from ops.inquiry_quota_ledger q
     where q.workspace_id = p_workspace_id and q.inquiry_id = p_inquiry_id and q.released_at is null);
  select pg_catalog.count(*) into v_attempts
    from ops.email_delivery_attempts a
   where a.workspace_id = p_workspace_id and a.inquiry_id = p_inquiry_id;
  select a.outcome, a.pre_submission_proof, a.reconciled_outcome
    into v_last_outcome, v_last_proof, v_last_reconciled
    from ops.email_delivery_attempts a
   where a.workspace_id = p_workspace_id and a.inquiry_id = p_inquiry_id
   order by a.attempt_number desc
   limit 1;
  -- Provider acceptance: an accepted attempt, or an uncertain one reconciled by a Sent
  -- Items/provider hit. A reconciliation that cites a correlated inbound message counts
  -- through that message (v_correlated) only.
  v_accepted := exists (
    select 1 from ops.email_delivery_attempts a
     where a.workspace_id = p_workspace_id and a.inquiry_id = p_inquiry_id
       and (a.outcome = 'accepted'
            or (a.reconciled_outcome = 'accepted'
                and (a.reconciliation_evidence ->> 'sent_items' = 'found'
                     or a.reconciliation_evidence ->> 'provider_search' = 'found'))));
  -- An inbound message proves submission only when it references a Message-ID of this
  -- inquiry (In-Reply-To/References or a bounce's returned original); a thread-only or
  -- quarantined possible match never does (domain.replies.correlate_reply).
  v_correlated := exists (
    select 1 from app.seller_replies r
     where r.workspace_id = p_workspace_id and r.inquiry_id = p_inquiry_id and not r.quarantined
       and r.header_linked);

  if v_state in ('reserved', 'queued', 'sending') and not v_has_debit then
    raise exception using
      errcode = 'SV003',
      message = pg_catalog.format('a %s seller inquiry must hold a quota debit (ops.inquiry_quota_ledger)', v_state);
  end if;
  if v_state in ('cancelled', 'suppressed') and v_attempts = 0 and v_has_debit then
    raise exception using
      errcode = 'SV003',
      message = 'a never-transmitted cancelled or suppressed inquiry must release its quota debit';
  end if;
  if v_state = 'sending' and v_last_outcome is distinct from 'running' then
    raise exception using
      errcode = 'SV003',
      message = 'a sending inquiry needs its committed send intent (a running ops.email_delivery_attempts row)';
  end if;
  if v_state <> 'sending' and v_last_outcome = 'running' then
    raise exception using
      errcode = 'SV003',
      message = pg_catalog.format('a %s seller inquiry cannot keep a running send attempt', v_state);
  end if;
  if v_state = 'uncertain'
     and not (v_last_outcome is not distinct from 'uncertain' and v_last_reconciled is null) then
    raise exception using
      errcode = 'SV003',
      message = 'an uncertain inquiry needs its unresolved uncertain send attempt';
  end if;
  if v_state = 'failed_definite'
     and not ((v_last_outcome is not distinct from 'pre_submission_failure' and v_last_proof is not null)
              or v_last_outcome is not distinct from 'definite_rejection'
              or v_last_reconciled is not distinct from 'proven_not_submitted') then
    raise exception using
      errcode = 'SV003',
      message = 'a definite failure needs proof of non-submission or a definite provider rejection';
  end if;
  if v_state in ('accepted', 'no_reply_yet', 'replied', 'bounced', 'seller_opted_out')
     and not (v_accepted or v_correlated) then
    raise exception using
      errcode = 'SV003',
      message = 'provider acceptance needs an accepted attempt or a correlated inbound message';
  end if;
  if v_state = 'replied' and not exists (
       select 1 from app.seller_replies r
        where r.workspace_id = p_workspace_id and r.inquiry_id = p_inquiry_id
          and not r.quarantined and r.message_type = 'seller_reply') then
    raise exception using errcode = 'SV003', message = 'a replied inquiry needs a correlated seller reply';
  end if;
end
$$;

-- Seller inquiries: initial states, frozen identity, frozen binding once
-- reserved, set-once provider references, the spec 37.5 transition graph and
-- its guarded edges. State timestamps are maintained here.
create or replace function app.seller_inquiries_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  pre_reservation constant text[] := array['candidate', 'qualifying', 'held_facts'];
  identity_cols constant text[] := array[
    'id', 'workspace_id', 'identity_key', 'purpose', 'vehicle_kind', 'vehicle_cluster_id', 'vehicle_listing_id',
    'seller_entity_id', 'created_at'];
  binding_cols constant text[] := array[
    'qualification_listing_id', 'qualification_revision_id', 'qualification_revision_number',
    'qualified_semantic_hash', 'qualified_price_minor', 'qualified_currency', 'qualified_availability',
    'readiness', 'readiness_reasons', 'readiness_rationale_hash', 'readiness_rules_version',
    'readiness_evaluated_at', 'authorization_id', 'authorization_version', 'authorization_fingerprint',
    'template_id', 'template_version', 'template_hash', 'template_set_version', 'language', 'scope_hash',
    'body_hash', 'binding_hash', 'original_subject', 'original_body', 'mk_preview_subject', 'mk_preview_body',
    'mk_preview_hash', 'sender_binding_id', 'sender_binding_version', 'sender_provider', 'sender_account_id',
    'sender_from_address', 'sender_display_name', 'sender_reply_to_address', 'recipient_contact_id',
    'recipient_address', 'recipient_binding_hash'];
  set_once_cols constant text[] := array['rfc_message_id', 'provider_message_id', 'provider_thread_id',
                                         'provider_receipt'];
  o jsonb;
  n jsonb;
  changed text[];
  allowed boolean;
  v_attempts integer;
  v_last_outcome text;
  v_last_proof text;
  v_last_reconciled text;
begin
  if tg_op = 'INSERT' then
    if not (new.state = any (pre_reservation)) then
      raise exception using
        errcode = 'SV002',
        message = 'a seller inquiry is created as candidate, qualifying or held_facts and reserved by a transition';
    end if;
    -- Lifecycle timestamps are evidence (cooldown, rolling caps, "possibly transmitted"):
    -- only the transitions below set them.
    if new.reserved_at is not null or new.queued_at is not null or new.send_attempted_at is not null
       or new.accepted_at is not null or new.replied_at is not null then
      raise exception using
        errcode = 'SV004',
        message = 'seller inquiry lifecycle timestamps are maintained by the database';
    end if;
    perform app.seller_inquiry_assert_identity(
      new.workspace_id, new.vehicle_kind, new.vehicle_cluster_id, new.qualification_listing_id,
      new.seller_entity_id);
    new.state_changed_at := pg_catalog.now();
    return new;
  end if;

  o := pg_catalog.to_jsonb(old);
  n := pg_catalog.to_jsonb(new);
  select pg_catalog.array_agg(c order by c) into changed
    from pg_catalog.unnest(identity_cols) as c
   where n -> c is distinct from o -> c;
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = 'seller inquiry identity is immutable (changed: ' || pg_catalog.array_to_string(changed, ', ') || ')';
  end if;
  if not (old.state = any (pre_reservation)) then
    select pg_catalog.array_agg(c order by c) into changed
      from pg_catalog.unnest(binding_cols) as c
     where n -> c is distinct from o -> c;
    if changed is not null then
      raise exception using
        errcode = 'SV004',
        message = 'the inquiry binding is immutable once reserved (changed: '
               || pg_catalog.array_to_string(changed, ', ') || ')';
    end if;
  end if;
  select pg_catalog.array_agg(c order by c) into changed
    from pg_catalog.unnest(set_once_cols) as c
   where pg_catalog.jsonb_typeof(o -> c) is distinct from 'null'
     and n -> c is distinct from o -> c;
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = 'provider references are set once (changed: ' || pg_catalog.array_to_string(changed, ', ') || ')';
  end if;
  if new.row_version < old.row_version then
    raise exception using errcode = 'SV005', message = 'seller inquiry row_version must not decrease';
  end if;
  -- reserved_at/queued_at/send_attempted_at/state_changed_at are set only by this trigger;
  -- accepted_at/replied_at may carry the provider/receipt time, but only in the very
  -- transition into accepted/replied, and only once. Backdating them would escape the seller
  -- cooldown and the rolling caps; clearing send_attempted_at would hide a transmission.
  if (new.reserved_at, new.queued_at, new.send_attempted_at, new.state_changed_at)
     is distinct from (old.reserved_at, old.queued_at, old.send_attempted_at, old.state_changed_at)
     or (new.accepted_at is distinct from old.accepted_at
         and not (old.accepted_at is null and new.state = 'accepted' and old.state <> 'accepted'))
     or (new.replied_at is distinct from old.replied_at
         and not (old.replied_at is null and new.state = 'replied' and old.state <> 'replied')) then
    raise exception using
      errcode = 'SV004',
      message = 'seller inquiry lifecycle timestamps are maintained by the database';
  end if;

  if new.state is distinct from old.state then
    -- domain.inquiries.ALLOWED_TRANSITIONS (spec 37.5).
    allowed := case old.state
      when 'candidate' then new.state in ('qualifying', 'held_facts', 'suppressed', 'cancelled')
      when 'qualifying' then new.state in ('reserved', 'held_facts', 'suppressed', 'cancelled')
      when 'held_facts' then new.state in ('qualifying', 'suppressed', 'cancelled')
      when 'reserved' then new.state in ('queued', 'suppressed', 'cancelled')
      when 'queued' then new.state in ('sending', 'suppressed', 'cancelled')
      when 'sending' then new.state in ('accepted', 'uncertain', 'failed_definite')
      when 'uncertain' then new.state in ('accepted', 'failed_definite')
      when 'failed_definite' then new.state in ('queued')
      when 'accepted' then new.state in ('replied', 'bounced', 'seller_opted_out', 'no_reply_yet')
      when 'no_reply_yet' then new.state in ('replied', 'bounced', 'seller_opted_out')
      when 'replied' then new.state in ('seller_opted_out')
      when 'cancelled' then new.state in ('qualifying')
      when 'suppressed' then new.state in ('qualifying')
      else false
    end;
    if not allowed then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('seller inquiry state %s -> %s is not permitted', old.state, new.state);
    end if;

    select pg_catalog.count(*) into v_attempts
      from ops.email_delivery_attempts a
     where a.workspace_id = old.workspace_id and a.inquiry_id = old.id;

    -- Re-qualification only for never-transmitted records; a suppressed one also
    -- needs the audit event of its explicit suppression removal.
    if old.state in ('cancelled', 'suppressed') then
      if old.send_attempted_at is not null or v_attempts > 0 then
        raise exception using
          errcode = 'SV002',
          message = 'a (possibly) transmitted inquiry can never be re-qualified';
      end if;
      if old.state = 'suppressed' then
        if new.requalification_audit_id is null
           or new.requalification_audit_id is not distinct from old.requalification_audit_id
           or not exists (
                select 1 from ops.audit_events ae
                 where ae.workspace_id = new.workspace_id and ae.id = new.requalification_audit_id
                   and ae.target_type = 'seller_inquiry' and ae.target_id = new.id) then
          raise exception using
            errcode = 'SV002',
            message = 'leaving suppression needs a new audit event about this inquiry (requalification_audit_id)';
        end if;
      end if;
    end if;

    if new.state = 'reserved' then
      perform app.seller_inquiry_preflight(new, 'reserve');
      new.reserved_at := pg_catalog.now();
    elsif new.state = 'queued' then
      perform app.seller_inquiry_preflight(new, 'queue');
      if old.state = 'failed_definite' then
        -- Guarded retry (domain.inquiries.should_retry): same account, a proven
        -- pre-submission failure (or a reconciled proof of non-submission), no
        -- attempt that is running, accepted or unresolved, attempts remaining.
        select a.outcome, a.pre_submission_proof, a.reconciled_outcome
          into v_last_outcome, v_last_proof, v_last_reconciled
          from ops.email_delivery_attempts a
         where a.workspace_id = old.workspace_id and a.inquiry_id = old.id
         order by a.attempt_number desc
         limit 1;
        if v_attempts >= 3 then
          raise exception using errcode = 'SV002', message = 'send attempts are exhausted';
        end if;
        if not ((v_last_outcome is not distinct from 'pre_submission_failure' and v_last_proof is not null)
                or v_last_reconciled is not distinct from 'proven_not_submitted') then
          raise exception using
            errcode = 'SV002',
            message = 'a retry needs proof that the previous attempt never reached the provider';
        end if;
        if exists (
             select 1 from ops.email_delivery_attempts a
              where a.workspace_id = old.workspace_id and a.inquiry_id = old.id
                and (a.outcome in ('running', 'accepted') or a.reconciled_outcome = 'accepted'
                     or (a.outcome = 'uncertain' and a.reconciled_outcome is null))) then
          raise exception using
            errcode = 'SV002',
            message = 'an earlier send attempt is running, accepted or unresolved; never resend blindly';
        end if;
      end if;
      new.queued_at := pg_catalog.now();
    elsif new.state = 'sending' then
      perform app.seller_inquiry_preflight(new, 'dispatch');
      new.send_attempted_at := pg_catalog.now();
    elsif new.state = 'accepted' then
      new.accepted_at := coalesce(new.accepted_at, pg_catalog.now());
    elsif new.state = 'replied' then
      new.replied_at := coalesce(new.replied_at, pg_catalog.now());
    end if;
    new.state_changed_at := pg_catalog.now();
  end if;
  return new;
end
$$;

-- Constraint-trigger wrappers (fire at commit).
create or replace function app.seller_inquiries_check_evidence()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  perform app.seller_inquiry_assert_evidence(new.workspace_id, new.id);
  return null;
end
$$;

create or replace function ops.email_delivery_attempts_check_evidence()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  perform app.seller_inquiry_assert_evidence(new.workspace_id, new.inquiry_id);
  return null;
end
$$;

-- Attempts: a send intent is only recorded for a 'sending' inquiry, through the
-- bound account, numbered consecutively, with a growing fencing token, and only
-- when no earlier attempt may have reached the provider. Afterwards only the
-- one-time outcome finalisation and the one-time reconciliation may change.
create or replace function ops.email_delivery_attempts_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  finalisation constant text[] := array[
    'outcome', 'finished_at', 'pre_submission_proof', 'provider_message_id', 'provider_thread_id',
    'provider_response', 'receipt', 'error_code', 'provider_idempotency_key', 'provider_idempotency_documented'];
  reconciliation constant text[] := array['reconciled_outcome', 'reconciled_at', 'reconciliation_evidence'];
  v_inquiry record;
  v_max_number integer;
  v_max_fencing bigint;
  changed text[];
begin
  if tg_op = 'INSERT' then
    select i.state, i.sender_binding_id, i.sender_binding_version, i.sender_provider, i.rfc_message_id
      into v_inquiry
      from app.seller_inquiries i
     where i.workspace_id = new.workspace_id and i.id = new.inquiry_id;
    if v_inquiry.state is distinct from 'sending' then
      raise exception using
        errcode = 'SV002',
        message = 'a send intent is recorded only for an inquiry that just moved to sending';
    end if;
    if new.outcome <> 'running' or new.finished_at is not null or new.reconciled_outcome is not null then
      raise exception using
        errcode = 'SV002',
        message = 'a send attempt is recorded as a running send intent before any external I/O';
    end if;
    if new.lease_expires_at <= pg_catalog.now() then
      raise exception using errcode = 'SV002', message = 'a send intent needs a live lease';
    end if;
    if new.sender_binding_id is distinct from v_inquiry.sender_binding_id
       or new.sender_binding_version is distinct from v_inquiry.sender_binding_version
       or new.provider is distinct from v_inquiry.sender_provider then
      raise exception using
        errcode = 'SV003',
        message = 'a send attempt must use exactly the bound sender account (never another account)';
    end if;
    if v_inquiry.rfc_message_id is not null and new.rfc_message_id is distinct from v_inquiry.rfc_message_id then
      raise exception using errcode = 'SV003', message = 'a send attempt must reuse the inquiry''s stable Message-ID';
    end if;
    select pg_catalog.max(a.attempt_number), pg_catalog.max(a.fencing_token)
      into v_max_number, v_max_fencing
      from ops.email_delivery_attempts a
     where a.workspace_id = new.workspace_id and a.inquiry_id = new.inquiry_id;
    if new.attempt_number <> coalesce(v_max_number, 0) + 1 then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('send attempt numbers are consecutive (expected %s)', coalesce(v_max_number, 0) + 1);
    end if;
    if v_max_fencing is not null and new.fencing_token <= v_max_fencing then
      raise exception using errcode = 'SV005', message = 'the fencing token must grow with every attempt';
    end if;
    if exists (
         select 1 from ops.email_delivery_attempts a
          where a.workspace_id = new.workspace_id and a.inquiry_id = new.inquiry_id
            and (a.outcome in ('running', 'accepted') or a.reconciled_outcome = 'accepted'
                 or (a.outcome = 'uncertain' and a.reconciled_outcome is null))) then
      raise exception using
        errcode = 'SV002',
        message = 'an earlier send attempt is running, accepted or unresolved; never resend blindly';
    end if;
    new.send_intent_committed_at := pg_catalog.now();
    return new;
  end if;

  select pg_catalog.array_agg(n.key order by n.key)
    into changed
    from pg_catalog.jsonb_each(pg_catalog.to_jsonb(new)) as n
    join pg_catalog.jsonb_each(pg_catalog.to_jsonb(old)) as o on o.key = n.key
   where n.value is distinct from o.value
     and not (n.key = any (finalisation || reconciliation || array['updated_at', 'submission_uncertain']));
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = 'send attempts are append-only except the outcome (changed: '
             || pg_catalog.array_to_string(changed, ', ') || ')';
  end if;
  -- Once the lease has expired the worker may still be submitting (spec 37.5): the attempt is
  -- then finalised as 'uncertain' (or by positive provider evidence), never as a proven
  -- pre-submission failure that would allow a retry. A late proof is a reconciliation.
  if old.outcome = 'running' and new.outcome = 'pre_submission_failure'
     and pg_catalog.now() > old.lease_expires_at then
    raise exception using
      errcode = 'SV002',
      message = 'the attempt lease expired: finalise it as uncertain and reconcile with evidence';
  end if;
  if old.outcome <> 'running' then
    select pg_catalog.array_agg(c order by c) into changed
      from pg_catalog.unnest(finalisation) as c
     where pg_catalog.to_jsonb(new) -> c is distinct from pg_catalog.to_jsonb(old) -> c;
    if changed is not null then
      raise exception using
        errcode = 'SV004',
        message = 'a finalised send attempt outcome is immutable; record reconciliation evidence instead';
    end if;
  end if;
  if old.reconciled_outcome is not null
     and (new.reconciled_outcome, new.reconciled_at, new.reconciliation_evidence)
         is distinct from (old.reconciled_outcome, old.reconciled_at, old.reconciliation_evidence) then
    raise exception using errcode = 'SV004', message = 'an attempt reconciliation is recorded once';
  end if;
  return new;
end
$$;

-- Quota ledger: caps are checked under the workspace control row lock, so two
-- workers can never both take the last slot; no backdated debits; release only
-- for a never-transmitted cancelled/suppressed inquiry.
create or replace function ops.inquiry_quota_ledger_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_max_24h smallint;
  v_max_15d smallint;
  v_count_24h integer;
  v_count_15d integer;
  v_state text;
  v_sent timestamptz;
  v_maintenance boolean;
begin
  v_maintenance := pg_catalog.current_setting('app.history_maintenance', true) = 'on'
    and pg_catalog.pg_has_role(
          current_user, (select c.relowner from pg_catalog.pg_class c where c.oid = tg_relid), 'USAGE');
  if tg_op = 'INSERT' then
    select c.max_per_24h, c.max_per_15d into v_max_24h, v_max_15d
      from app.seller_inquiry_controls c
     where c.workspace_id = new.workspace_id
       for update;
    if not found then
      raise exception using errcode = 'SV002', message = 'seller inquiry controls are not initialised for this workspace';
    end if;
    if new.released_at is not null then
      raise exception using errcode = 'SV002', message = 'a quota debit is recorded unreleased';
    end if;
    if not v_maintenance and new.debited_at < pg_catalog.now() - interval '5 minutes' then
      raise exception using errcode = 'SV002', message = 'a quota debit cannot be backdated';
    end if;
    select i.state into v_state
      from app.seller_inquiries i
     where i.workspace_id = new.workspace_id and i.id = new.inquiry_id;
    if v_state is null or v_state not in ('qualifying', 'reserved') then
      raise exception using
        errcode = 'SV002',
        message = 'a quota debit is taken only when the inquiry is reserved';
    end if;
    -- Rolling windows (now - window, now], each debit counted at the later of its
    -- reservation and its send attempt; future-dated debits still count.
    select u.count_24h, u.count_15d into v_count_24h, v_count_15d
      from ops.inquiry_quota_usage(new.workspace_id, new.inquiry_id) as u;
    if v_count_24h >= v_max_24h then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('seller inquiry cap reached: %s per rolling 24 hours', v_max_24h);
    end if;
    if v_count_15d >= v_max_15d then
      raise exception using
        errcode = 'SV002',
        message = pg_catalog.format('seller inquiry cap reached: %s per rolling 15 days', v_max_15d);
    end if;
    return new;
  end if;

  if (pg_catalog.to_jsonb(new) - array['released_at', 'release_reason'])
     is distinct from (pg_catalog.to_jsonb(old) - array['released_at', 'release_reason']) then
    raise exception using errcode = 'SV004', message = 'a quota debit is immutable except its release';
  end if;
  if old.released_at is not null and (new.released_at, new.release_reason) is distinct from (old.released_at, old.release_reason) then
    raise exception using errcode = 'SV004', message = 'a quota debit release is recorded once';
  end if;
  if old.released_at is null and new.released_at is not null then
    select i.state, i.send_attempted_at into v_state, v_sent
      from app.seller_inquiries i
     where i.workspace_id = new.workspace_id and i.id = new.inquiry_id;
    if v_state is null or v_state not in ('cancelled', 'suppressed') or v_sent is not null or exists (
         select 1 from ops.email_delivery_attempts a
          where a.workspace_id = new.workspace_id and a.inquiry_id = new.inquiry_id) then
      raise exception using
        errcode = 'SV002',
        message = 'only a never-transmitted cancelled or suppressed inquiry releases its quota debit';
    end if;
  end if;
  return new;
end
$$;

-- Suppressions: content is immutable; removal is set once, by a non-system
-- principal (CHECK), with an audit event about exactly this suppression.
create or replace function ops.email_suppressions_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  removal constant text[] := array[
    'removed_at', 'removed_by_principal_id', 'removed_by_kind', 'removal_reason', 'removal_audit_id',
    'updated_at', 'match_key'];
begin
  if (pg_catalog.to_jsonb(new) - removal) is distinct from (pg_catalog.to_jsonb(old) - removal) then
    raise exception using errcode = 'SV004', message = 'a suppression is immutable; only its explicit removal is recorded';
  end if;
  if old.removed_at is not null
     and (pg_catalog.to_jsonb(new) - array['updated_at', 'match_key']) is distinct from
         (pg_catalog.to_jsonb(old) - array['updated_at', 'match_key']) then
    raise exception using errcode = 'SV004', message = 'a removed suppression is frozen; suppress again with a new row';
  end if;
  if old.removed_at is null and new.removed_at is not null and not exists (
       select 1 from ops.audit_events ae
        where ae.workspace_id = new.workspace_id and ae.id = new.removal_audit_id
          and ae.target_type = 'email_suppression' and ae.target_id = new.id) then
    raise exception using
      errcode = 'SV003',
      message = 'a suppression removal needs an audit event about this suppression';
  end if;
  return new;
end
$$;

-- Mailbox worker bindings: the credential must be a live mail:ingest-only
-- credential of the workspace; the mailbox identity is frozen; revocation is
-- permanent; credential rotation advances the version.
create or replace function ops.mail_worker_bindings_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_scopes text[];
  v_revoked timestamptz;
  v_expires timestamptz;
begin
  if tg_op = 'UPDATE' then
    if new.workspace_id <> old.workspace_id or new.sender_binding_id <> old.sender_binding_id
       or new.provider <> old.provider or new.account_address <> old.account_address
       or new.store_id_hash is distinct from old.store_id_hash or new.created_at <> old.created_at
       or new.created_by is distinct from old.created_by then
      raise exception using
        errcode = 'SV004',
        message = 'the mailbox identity of a worker binding is immutable; bindings are never reassigned';
    end if;
    if old.state = 'revoked'
       and (pg_catalog.to_jsonb(new) - 'updated_at') is distinct from (pg_catalog.to_jsonb(old) - 'updated_at') then
      raise exception using errcode = 'SV004', message = 'a revoked mailbox worker binding is frozen';
    end if;
    if new.version < old.version or new.sync_sequence < old.sync_sequence then
      raise exception using errcode = 'SV005', message = 'worker binding version and sync sequence never decrease';
    end if;
    if (new.credential_id, new.folder_scope, new.worker_label) is distinct from
       (old.credential_id, old.folder_scope, old.worker_label)
       and new.version <= old.version then
      raise exception using errcode = 'SV005', message = 'a worker binding change must advance its version';
    end if;
    if new.credential_id = old.credential_id then
      return new;
    end if;
  end if;
  if new.state = 'active' then
    select c.scopes, c.revoked_at, c.expires_at into v_scopes, v_revoked, v_expires
      from ops.api_credentials c
     where c.workspace_id = new.workspace_id and c.id = new.credential_id;
    if v_scopes is distinct from array['mail:ingest']::text[] or v_revoked is not null
       or v_expires <= pg_catalog.now() then
      raise exception using
        errcode = 'SV003',
        message = 'a mailbox worker binding needs a live credential carrying only mail:ingest';
    end if;
  end if;
  return new;
end
$$;

-- Replies: only for an inquiry that was (possibly) sent, through the active
-- worker binding of the inquiry's own sender mailbox (no cross-mailbox
-- injection), under a published and not tombstoned binding version. Source
-- content is immutable; quarantine is released only with a recorded verification.
create or replace function app.seller_replies_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  mutable constant text[] := array[
    'mk_summary', 'mk_summary_version', 'mk_summary_generated_at', 'claims', 'claims_version',
    'processing_state', 'processed_at', 'quarantined', 'quarantine_reason', 'quarantine_released_at',
    'quarantine_released_by', 'quarantine_release_reason', 'correlation_status', 'message_type', 'updated_at'];
  changed text[];
  v_inquiry record;
  v_mailbox record;
  v_latest_state text;
  v_ids text[];
  v_threads text[];
begin
  if tg_op = 'INSERT' then
    select i.state, i.send_attempted_at, i.sender_binding_id into v_inquiry
      from app.seller_inquiries i
     where i.workspace_id = new.workspace_id and i.id = new.inquiry_id;
    if v_inquiry.send_attempted_at is null then
      raise exception using
        errcode = 'SV003',
        message = 'replies are stored only for an inquiry that was (possibly) transmitted';
    end if;
    select m.state, m.sender_binding_id, m.credential_id into v_mailbox
      from ops.mail_worker_bindings m
     where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id;
    if v_mailbox.state is distinct from 'active' then
      raise exception using errcode = 'SV002', message = 'the mailbox worker binding is revoked';
    end if;
    if not exists (
         select 1 from ops.api_credentials c
          where c.workspace_id = new.workspace_id and c.id = v_mailbox.credential_id
            and c.revoked_at is null and (c.expires_at is null or c.expires_at > pg_catalog.now())) then
      raise exception using
        errcode = 'SV002',
        message = 'the mailbox worker credential is revoked or expired; the local backlog waits for a new one';
    end if;
    if v_mailbox.sender_binding_id is distinct from v_inquiry.sender_binding_id then
      raise exception using
        errcode = 'SV003',
        message = 'the reply mailbox is not the mailbox the inquiry was sent from';
    end if;
    if not exists (
         select 1 from ops.mail_binding_sync s
          where s.workspace_id = new.workspace_id and s.mailbox_binding_id = new.mailbox_binding_id
            and s.inquiry_id = new.inquiry_id and s.binding_version = new.binding_version
            and s.binding_state <> 'tombstoned') then
      raise exception using
        errcode = 'SV003',
        message = 'the binding version was never published to this mailbox (or is a tombstone)';
    end if;
    select s.binding_state into v_latest_state
      from ops.mail_binding_sync s
     where s.workspace_id = new.workspace_id and s.mailbox_binding_id = new.mailbox_binding_id
       and s.inquiry_id = new.inquiry_id
     order by s.binding_version desc
     limit 1;
    if v_latest_state = 'tombstoned' then
      raise exception using errcode = 'SV002', message = 'the inquiry binding was revoked (tombstoned) for this mailbox';
    end if;
    if new.quarantine_released_at is not null then
      raise exception using errcode = 'SV002', message = 'a reply is stored before any quarantine release';
    end if;
    -- Correlation links (never caller-supplied). Outbound identities: the inquiry's stable
    -- Message-ID, every send intent's Message-ID, and the ids/threads published to this
    -- mailbox in the binding version the worker matched against.
    select coalesce(pg_catalog.array_agg(distinct x.mid) filter (where x.mid is not null), array[]::text[])
      into v_ids
      from (
        select i.rfc_message_id as mid
          from app.seller_inquiries i
         where i.workspace_id = new.workspace_id and i.id = new.inquiry_id
        union all
        select a.rfc_message_id
          from ops.email_delivery_attempts a
         where a.workspace_id = new.workspace_id and a.inquiry_id = new.inquiry_id
        union all
        select pg_catalog.jsonb_array_elements_text(
                 case when pg_catalog.jsonb_typeof(s.payload -> k.key) = 'array' then s.payload -> k.key
                      else '[]'::jsonb end)
          from ops.mail_binding_sync s
          cross join (values ('outbound_message_ids'), ('send_intent_message_ids')) as k(key)
         where s.workspace_id = new.workspace_id and s.mailbox_binding_id = new.mailbox_binding_id
           and s.inquiry_id = new.inquiry_id and s.binding_version = new.binding_version
      ) as x;
    select coalesce(pg_catalog.array_agg(distinct x.tid) filter (where x.tid is not null), array[]::text[])
      into v_threads
      from (
        select i.provider_thread_id as tid
          from app.seller_inquiries i
         where i.workspace_id = new.workspace_id and i.id = new.inquiry_id
        union all
        select a.provider_thread_id
          from ops.email_delivery_attempts a
         where a.workspace_id = new.workspace_id and a.inquiry_id = new.inquiry_id
        union all
        select pg_catalog.jsonb_array_elements_text(
                 case when pg_catalog.jsonb_typeof(s.payload -> 'provider_thread_ids') = 'array'
                      then s.payload -> 'provider_thread_ids' else '[]'::jsonb end)
          from ops.mail_binding_sync s
         where s.workspace_id = new.workspace_id and s.mailbox_binding_id = new.mailbox_binding_id
           and s.inquiry_id = new.inquiry_id and s.binding_version = new.binding_version
      ) as x;
    new.header_linked := (pg_catalog.array_remove(array[new.in_reply_to], null) || new.reference_ids
                          || new.returned_message_ids) && v_ids;
    new.thread_linked := coalesce(new.provider_thread_id = any (v_threads), false);
    return new;
  end if;

  select pg_catalog.array_agg(n.key order by n.key)
    into changed
    from pg_catalog.jsonb_each(pg_catalog.to_jsonb(new)) as n
    join pg_catalog.jsonb_each(pg_catalog.to_jsonb(old)) as o on o.key = n.key
   where n.value is distinct from o.value
     and not (n.key = any (mutable));
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = 'stored reply source content is immutable (changed: ' || pg_catalog.array_to_string(changed, ', ')
             || '); a correction is a separate versioned operation';
  end if;
  if old.quarantine_released_at is not null
     and (new.quarantined, new.quarantine_reason, new.quarantine_released_at, new.quarantine_released_by,
          new.quarantine_release_reason, new.correlation_status, new.message_type)
         is distinct from
         (old.quarantined, old.quarantine_reason, old.quarantine_released_at, old.quarantine_released_by,
          old.quarantine_release_reason, old.correlation_status, old.message_type) then
    raise exception using errcode = 'SV004', message = 'a quarantine release is recorded once';
  end if;
  if old.quarantined and not new.quarantined then
    if old.conflict_of_reply_id is not null or old.message_type = 'spam' then
      raise exception using
        errcode = 'SV002',
        message = 'idempotency conflicts and spam stay quarantined';
    end if;
    if new.quarantine_released_at is null then
      raise exception using errcode = 'SV002', message = 'releasing a quarantine needs a recorded verification';
    end if;
  end if;
  if new.message_type is distinct from old.message_type
     and not (old.message_type = 'ambiguous' and old.quarantined and not new.quarantined
              and new.message_type in ('seller_reply', 'auto_reply', 'bounce', 'delivery_notice')) then
    raise exception using
      errcode = 'SV004',
      message = 'the message type changes only when a verified ambiguous match is released';
  end if;
  if new.correlation_status is distinct from old.correlation_status
     and not (old.correlation_status = 'quarantined' and new.correlation_status = 'verified_match'
              and old.quarantined and not new.quarantined) then
    raise exception using
      errcode = 'SV004',
      message = 'the correlation status changes only when a quarantined match is verified';
  end if;
  return new;
end
$$;

-- Availability events: absence evidence needs a complete scan; a seller
-- statement needs a verified (non-quarantined) seller reply about this vehicle.
create or replace function app.availability_events_check()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_outcome text;
  v_finished timestamptz;
  v_reply record;
begin
  -- (A missing run reference is reported by availability_events_reference_ck.)
  if new.evidence_kind = 'complete_scan_absence' and new.crawl_run_id is not null then
    select r.outcome, r.finished_at into v_outcome, v_finished
      from ops.crawl_runs r
     where r.workspace_id = new.workspace_id and r.id = new.crawl_run_id;
    if v_outcome is distinct from 'complete' or v_finished is null then
      raise exception using
        errcode = 'SV003',
        message = 'absence is availability evidence only for a finished complete scan';
    end if;
  end if;
  if new.reply_id is not null then
    select r.quarantined, r.message_type, i.qualification_listing_id, i.vehicle_cluster_id
      into v_reply
      from app.seller_replies r
      join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
     where r.workspace_id = new.workspace_id and r.id = new.reply_id;
    if v_reply.quarantined or v_reply.message_type <> 'seller_reply' then
      raise exception using
        errcode = 'SV003',
        message = 'a seller availability statement needs a verified, non-quarantined seller reply';
    end if;
    if v_reply.qualification_listing_id <> new.listing_id
       and not (v_reply.vehicle_cluster_id is not null and exists (
                  select 1 from app.vehicle_cluster_members m
                   where m.workspace_id = new.workspace_id and m.cluster_id = v_reply.vehicle_cluster_id
                     and m.listing_id = new.listing_id and m.unlinked_at is null)) then
      raise exception using
        errcode = 'SV003',
        message = 'the seller reply is about another vehicle than this listing';
    end if;
  end if;
  return new;
end
$$;

-- Checkpoints: frozen location, monotonic complete-scan time, writes only
-- through an active mailbox binding.
create or replace function ops.mail_worker_checkpoints_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if not exists (
       select 1 from ops.mail_worker_bindings m
        where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id and m.state = 'active') then
    raise exception using errcode = 'SV002', message = 'the mailbox worker binding is revoked';
  end if;
  if tg_op = 'UPDATE' then
    if new.mailbox_binding_id <> old.mailbox_binding_id or new.store_id_hash <> old.store_id_hash
       or new.folder_id_hash <> old.folder_id_hash or new.created_at <> old.created_at then
      raise exception using errcode = 'SV004', message = 'a checkpoint location is immutable';
    end if;
    if old.last_complete_scan_at is not null
       and (new.last_complete_scan_at is null or new.last_complete_scan_at < old.last_complete_scan_at) then
      raise exception using errcode = 'SV005', message = 'the last complete scan time never moves backwards';
    end if;
    if new.row_version < old.row_version then
      raise exception using errcode = 'SV005', message = 'checkpoint row_version must not decrease';
    end if;
  end if;
  return new;
end
$$;

-- Ingest dedup: recorded with the mailbox's current credential; identity is
-- frozen; replay/conflict counters only grow.
create or replace function ops.mail_ingest_dedup_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  mutable constant text[] := array[
    'last_seen_at', 'duplicate_count', 'conflict_count', 'last_conflict_at', 'last_conflict_fingerprint',
    'last_conflict_reply_id', 'updated_at'];
begin
  if tg_op = 'INSERT' then
    if not exists (
         select 1 from ops.mail_worker_bindings m
          where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id
            and m.state = 'active' and m.credential_id = new.credential_id) then
      raise exception using
        errcode = 'SV003',
        message = 'ingest is recorded only through the active mailbox binding''s own credential';
    end if;
    if not exists (
         select 1 from ops.api_credentials c
          where c.workspace_id = new.workspace_id and c.id = new.credential_id
            and c.revoked_at is null and (c.expires_at is null or c.expires_at > pg_catalog.now())) then
      raise exception using
        errcode = 'SV002',
        message = 'the mailbox worker credential is revoked or expired; the local backlog waits for a new one';
    end if;
    return new;
  end if;
  if (pg_catalog.to_jsonb(new) - mutable) is distinct from (pg_catalog.to_jsonb(old) - mutable) then
    raise exception using errcode = 'SV004', message = 'an ingest dedup identity is immutable';
  end if;
  if new.duplicate_count < old.duplicate_count or new.conflict_count < old.conflict_count
     or new.last_seen_at < old.last_seen_at then
    raise exception using errcode = 'SV005', message = 'ingest replay counters never decrease';
  end if;
  return new;
end
$$;

-- Binding sync: the sequence is allocated from the mailbox row (locked until
-- commit, so sequences commit in order), only for an active mailbox and for an
-- inquiry sent from that mailbox; binding versions grow; a tombstone is final.
create or replace function ops.mail_binding_sync_allocate()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_sender uuid;
  v_inquiry_sender uuid;
  v_latest_version integer;
  v_latest_state text;
  v_sequence bigint;
begin
  select i.sender_binding_id into v_inquiry_sender
    from app.seller_inquiries i
   where i.workspace_id = new.workspace_id and i.id = new.inquiry_id;
  select m.sender_binding_id into v_sender
    from ops.mail_worker_bindings m
   where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id and m.state = 'active';
  if v_sender is null then
    raise exception using errcode = 'SV002', message = 'bindings are published only to an active mailbox worker binding';
  end if;
  if v_inquiry_sender is distinct from v_sender then
    raise exception using
      errcode = 'SV003',
      message = 'an inquiry binding is published only to the mailbox it was sent from';
  end if;
  update ops.mail_worker_bindings m
     set sync_sequence = m.sync_sequence + 1
   where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id
  returning m.sync_sequence into v_sequence;
  select s.binding_version, s.binding_state into v_latest_version, v_latest_state
    from ops.mail_binding_sync s
   where s.workspace_id = new.workspace_id and s.mailbox_binding_id = new.mailbox_binding_id
     and s.inquiry_id = new.inquiry_id
   order by s.binding_version desc
   limit 1;
  if v_latest_state = 'tombstoned' then
    raise exception using errcode = 'SV002', message = 'a tombstoned inquiry binding is never re-published';
  end if;
  if v_latest_version is not null and new.binding_version <= v_latest_version then
    raise exception using errcode = 'SV005', message = 'binding versions grow for every published change';
  end if;
  new.sequence := v_sequence;
  return new;
end
$$;

-- =============================================================================
-- Triggers
-- =============================================================================

create trigger seller_entities_guard before update on app.seller_entities
  for each row execute function app.seller_entities_guard();
create trigger seller_entities_touch before update on app.seller_entities
  for each row execute function app.touch_updated_at();

create trigger seller_entity_aliases_frozen before update on app.seller_entity_aliases
  for each row execute function app.guard_frozen_columns('unlinked_at', 'unlinked_by', 'unlink_reason', 'updated_at');
create trigger seller_entity_aliases_touch before update on app.seller_entity_aliases
  for each row execute function app.touch_updated_at();
create trigger seller_entity_aliases_no_delete before delete on app.seller_entity_aliases
  for each row execute function app.reject_history_mutation();

create trigger seller_contacts_guard before update on app.seller_contacts
  for each row execute function app.seller_contacts_guard();
create trigger seller_contacts_touch before update on app.seller_contacts
  for each row execute function app.touch_updated_at();
create trigger seller_contacts_no_delete before delete on app.seller_contacts
  for each row execute function app.reject_history_mutation();

create trigger seller_inquiry_authorizations_append_only before update or delete on app.seller_inquiry_authorizations
  for each row execute function app.reject_history_mutation();

create trigger seller_inquiry_controls_guard before update on app.seller_inquiry_controls
  for each row execute function app.seller_inquiry_controls_guard();
create trigger seller_inquiry_controls_touch before update on app.seller_inquiry_controls
  for each row execute function app.touch_updated_at();

create trigger email_sender_bindings_guard before update on ops.email_sender_bindings
  for each row execute function ops.email_sender_bindings_guard();
create trigger email_sender_bindings_touch before update on ops.email_sender_bindings
  for each row execute function app.touch_updated_at();
create trigger email_sender_bindings_no_delete before delete on ops.email_sender_bindings
  for each row execute function app.reject_history_mutation();

create trigger seller_inquiries_guard before insert or update on app.seller_inquiries
  for each row execute function app.seller_inquiries_guard();
create trigger seller_inquiries_touch before update on app.seller_inquiries
  for each row execute function app.touch_updated_at();
create trigger seller_inquiries_no_delete before delete on app.seller_inquiries
  for each row execute function app.reject_history_mutation();
create constraint trigger seller_inquiries_evidence after insert or update on app.seller_inquiries
  deferrable initially deferred
  for each row execute function app.seller_inquiries_check_evidence();

create trigger email_delivery_attempts_guard before insert or update on ops.email_delivery_attempts
  for each row execute function ops.email_delivery_attempts_guard();
create trigger email_delivery_attempts_touch before update on ops.email_delivery_attempts
  for each row execute function app.touch_updated_at();
create trigger email_delivery_attempts_no_delete before delete on ops.email_delivery_attempts
  for each row execute function app.reject_history_mutation();
create constraint trigger email_delivery_attempts_evidence after insert or update on ops.email_delivery_attempts
  deferrable initially deferred
  for each row execute function ops.email_delivery_attempts_check_evidence();

create trigger inquiry_quota_ledger_guard before insert or update on ops.inquiry_quota_ledger
  for each row execute function ops.inquiry_quota_ledger_guard();
create trigger inquiry_quota_ledger_no_delete before delete on ops.inquiry_quota_ledger
  for each row execute function app.reject_history_mutation();

create trigger mail_worker_bindings_guard before insert or update on ops.mail_worker_bindings
  for each row execute function ops.mail_worker_bindings_guard();
create trigger mail_worker_bindings_touch before update on ops.mail_worker_bindings
  for each row execute function app.touch_updated_at();
create trigger mail_worker_bindings_no_delete before delete on ops.mail_worker_bindings
  for each row execute function app.reject_history_mutation();

create trigger seller_replies_guard before insert or update on app.seller_replies
  for each row execute function app.seller_replies_guard();
create trigger seller_replies_touch before update on app.seller_replies
  for each row execute function app.touch_updated_at();
create trigger seller_replies_no_delete before delete on app.seller_replies
  for each row execute function app.reject_history_mutation();

create trigger seller_reply_locators_append_only before update or delete on app.seller_reply_locators
  for each row execute function app.reject_history_mutation();

create trigger mail_ingest_dedup_guard before insert or update on ops.mail_ingest_dedup
  for each row execute function ops.mail_ingest_dedup_guard();
create trigger mail_ingest_dedup_touch before update on ops.mail_ingest_dedup
  for each row execute function app.touch_updated_at();
create trigger mail_ingest_dedup_no_delete before delete on ops.mail_ingest_dedup
  for each row execute function app.reject_history_mutation();

create trigger mail_worker_checkpoints_guard before insert or update on ops.mail_worker_checkpoints
  for each row execute function ops.mail_worker_checkpoints_guard();
create trigger mail_worker_checkpoints_touch before update on ops.mail_worker_checkpoints
  for each row execute function app.touch_updated_at();

create trigger mail_binding_sync_allocate before insert on ops.mail_binding_sync
  for each row execute function ops.mail_binding_sync_allocate();
create trigger mail_binding_sync_append_only before update or delete on ops.mail_binding_sync
  for each row execute function app.reject_history_mutation();

create trigger email_suppressions_guard before update on ops.email_suppressions
  for each row execute function ops.email_suppressions_guard();
create trigger email_suppressions_touch before update on ops.email_suppressions
  for each row execute function app.touch_updated_at();
create trigger email_suppressions_no_delete before delete on ops.email_suppressions
  for each row execute function app.reject_history_mutation();

create trigger availability_events_check before insert on app.availability_events
  for each row execute function app.availability_events_check();
create trigger availability_events_append_only before update or delete on app.availability_events
  for each row execute function app.reject_history_mutation();

-- =============================================================================
-- Security: baseline (RLS + tenant_isolation, no client access), then explicit
-- least-privilege grants to suv_backend. No DELETE anywhere; append-only
-- tables are SELECT/INSERT only; column grants freeze identity/content.
-- =============================================================================
call ops.apply_security_baseline();

grant execute on function
  app.email_address_ok(text),
  app.rfc_message_id_ok(text),
  app.rfc_message_id_array_ok(text[], integer),
  app.opaque_ref_ok(text, integer),
  app.hex64_array_ok(text[], integer),
  app.sender_display_name_ok(text),
  app.seller_inquiry_identity_key(uuid, text, uuid, uuid, text),
  app.message_body_hash(text, text),
  app.reply_attachments_ok(jsonb),
  ops.seller_inquiry_active_suppressions(uuid, uuid, text, text, uuid, uuid, text, uuid),
  ops.inquiry_quota_usage(uuid, uuid),
  app.seller_inquiry_vehicle_conflict(app.seller_inquiries, text),
  app.seller_inquiry_assert_identity(uuid, text, uuid, uuid, uuid),
  app.seller_inquiry_preflight(app.seller_inquiries, text),
  app.seller_inquiry_assert_evidence(uuid, uuid)
to suv_backend;

grant select, insert,
  update (seller_type, display_name, evidence, verified_at, merged_into_id, merged_at, merge_reason, row_version)
  on table app.seller_entities to suv_backend;
grant select, insert, update (unlinked_at, unlinked_by, unlink_reason)
  on table app.seller_entity_aliases to suv_backend;
grant select, insert, update (status, status_reasons, verified_at, last_rechecked_at, changed_at, superseded_by_id)
  on table app.seller_contacts to suv_backend;
grant select, insert on table app.seller_inquiry_authorizations to suv_backend;
grant select, insert,
  update (mode, kill_switch, kill_switch_reason, kill_switch_set_at, kill_switch_set_by, max_per_24h, max_per_15d,
          seller_cooldown, version, updated_by, update_reason)
  on table app.seller_inquiry_controls to suv_backend;
grant select, insert,
  update (display_name, reply_to_address, alias_verified, alias_verified_at, secret_envelope, secret_reference,
          health, health_checked_at, health_detail, verified_at, verified_by, revoked_at, revoked_by, revoke_reason,
          version)
  on table ops.email_sender_bindings to suv_backend;
grant select, insert,
  update (qualification_listing_id, qualification_revision_id, qualification_revision_number, qualified_semantic_hash,
          qualified_price_minor, qualified_currency, qualified_availability, readiness, readiness_reasons,
          readiness_rationale_hash, readiness_rules_version, readiness_evaluated_at, authorization_id,
          authorization_version, authorization_fingerprint, template_id, template_version, template_hash,
          template_set_version, language, scope_hash, body_hash, binding_hash, original_subject, original_body,
          mk_preview_subject, mk_preview_body, mk_preview_hash, sender_binding_id, sender_binding_version,
          sender_provider, sender_account_id, sender_from_address, sender_display_name, sender_reply_to_address,
          recipient_contact_id, recipient_address, recipient_binding_hash, state, state_reasons, suppression_reason,
          requalification_audit_id, rfc_message_id, provider_message_id, provider_thread_id, provider_receipt,
          accepted_at, replied_at, row_version)
  on table app.seller_inquiries to suv_backend;
grant select, insert,
  update (outcome, finished_at, pre_submission_proof, provider_idempotency_key, provider_idempotency_documented,
          provider_message_id, provider_thread_id, provider_response, receipt, error_code, reconciled_outcome,
          reconciled_at, reconciliation_evidence)
  on table ops.email_delivery_attempts to suv_backend;
grant select, insert, update (released_at, release_reason) on table ops.inquiry_quota_ledger to suv_backend;
grant select, insert,
  update (credential_id, folder_scope, worker_label, state, revoked_at, revoked_by, revoke_reason, sync_sequence,
          version)
  on table ops.mail_worker_bindings to suv_backend;
grant select, insert,
  update (mk_summary, mk_summary_version, mk_summary_generated_at, claims, claims_version, processing_state,
          processed_at, quarantined, quarantine_reason, quarantine_released_at, quarantine_released_by,
          quarantine_release_reason, correlation_status, message_type)
  on table app.seller_replies to suv_backend;
grant select, insert on table app.seller_reply_locators to suv_backend;
grant select, insert,
  update (last_seen_at, duplicate_count, conflict_count, last_conflict_at, last_conflict_fingerprint,
          last_conflict_reply_id)
  on table ops.mail_ingest_dedup to suv_backend;
grant select, insert,
  update (folder_role, cursor, overlap_watermark, last_complete_scan_at, last_scan_started_at, heartbeat_at,
          backlog_count, backlog_oldest_at, outlook_connected, mailbox_sync_ok, mailbox_last_sync_at, gap_reasons,
          row_version)
  on table ops.mail_worker_checkpoints to suv_backend;
grant select, insert on table ops.mail_binding_sync to suv_backend;
grant select, insert,
  update (removed_at, removed_by_principal_id, removed_by_kind, removal_reason, removal_audit_id)
  on table ops.email_suppressions to suv_backend;
grant select, insert on table app.availability_events to suv_backend;

-- Fail closed if the cluster-global backend role became unsafe (migration 0100).
do $verify_role$
declare
  problems text[] := ops.backend_role_problems('suv_backend');
begin
  if pg_catalog.cardinality(problems) > 0 then
    raise exception using
      errcode = 'insufficient_privilege',
      message = 'role suv_backend is unsafe for tenant isolation: '
             || pg_catalog.array_to_string(problems, ', ');
  end if;
end
$verify_role$;
