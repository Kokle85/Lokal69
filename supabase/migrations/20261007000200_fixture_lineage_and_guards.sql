-- =============================================================================
-- 20261007000200_fixture_lineage_and_guards
-- Additive (expand) changes requested by the persistence/runtime reviews:
--
--   1. Fixture lineage frozen at ingest (spec 18: fixtures never leak into
--      reality). app.listings.is_fixture records whether the listing was first
--      stored from a `mode: fixture` source. It is set at insert from the
--      source's mode OF THAT MOMENT and can never change afterwards, so
--      switching a source from fixture to a real mode never turns earlier
--      fixture listings (their review cases and outbox events) into real,
--      deliverable data. Existing rows are backfilled from the source's current
--      mode (the best evidence available; nothing was ever switched before this
--      column existed).
--   2. API credential kind 'mail_worker': a narrowly scoped mailbox-worker
--      credential (spec 37.8) carrying ONLY mail:ingest, owner role, machine
--      principal. It is bound to exactly one mailbox through
--      ops.mail_worker_bindings.credential_id (one active binding per
--      credential, guarded there). Static-bearer/dev credentials keep their
--      existing rules.
--
-- Forward-only; safe on Supabase and on the plain-PostgreSQL test emulation.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. app.listings.is_fixture
-- -----------------------------------------------------------------------------
alter table app.listings add column if not exists is_fixture boolean;

-- Backfill without touching updated_at (listings_touch) or tripping the
-- listing guard: only the new column changes.
alter table app.listings disable trigger listings_touch;
update app.listings l
   set is_fixture = (s.mode = 'fixture')
  from app.sources s
 where s.workspace_id = l.workspace_id
   and s.id = l.source_id
   and l.is_fixture is null;
alter table app.listings enable trigger listings_touch;

alter table app.listings alter column is_fixture set not null;

comment on column app.listings.is_fixture is
  'Fixture lineage frozen at ingest: true when the listing was first stored from a mode=fixture source. Immutable.';

-- Insert: a missing value is derived from the source's mode now; an explicit
-- value must agree with it (a stale application read never mislabels data).
-- Update: the lineage never changes.
create or replace function app.listings_fixture_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_mode text;
begin
  if tg_op = 'INSERT' then
    select s.mode into v_mode
      from app.sources s
     where s.workspace_id = new.workspace_id
       and s.id = new.source_id;
    if v_mode is null then
      -- The composite foreign key reports the missing source; nothing to derive.
      return new;
    end if;
    if new.is_fixture is null then
      new.is_fixture := (v_mode = 'fixture');
    elsif new.is_fixture is distinct from (v_mode = 'fixture') then
      raise exception using
        errcode = 'SV003',
        message = 'a listing''s fixture lineage must match its source''s mode at ingest';
    end if;
    return new;
  end if;
  if new.is_fixture is distinct from old.is_fixture then
    raise exception using
      errcode = 'SV004',
      message = 'a listing''s fixture lineage is frozen at ingest and never changes';
  end if;
  return new;
end
$$;

drop trigger if exists listings_fixture_lineage on app.listings;
create trigger listings_fixture_lineage before insert or update of is_fixture on app.listings
  for each row execute function app.listings_fixture_lineage();

-- -----------------------------------------------------------------------------
-- 2. API credential kind 'mail_worker'
-- -----------------------------------------------------------------------------
-- Widening check (existing rows satisfy it); NOT VALID + VALIDATE keeps the
-- swap explicit and the constraint name stable.
alter table ops.api_credentials drop constraint if exists api_credentials_kind_ck;
alter table ops.api_credentials
  add constraint api_credentials_kind_ck
    check (credential_kind in ('static_bearer', 'dev_local', 'mail_worker')) not valid;
alter table ops.api_credentials validate constraint api_credentials_kind_ck;

-- A mailbox-worker credential is exactly the narrow worker identity: only
-- mail:ingest (api_credentials_mail_ingest_ck then also forces the owner role),
-- a machine principal (never a member's identity), and a suvmail_ token.
alter table ops.api_credentials drop constraint if exists api_credentials_mail_worker_ck;
alter table ops.api_credentials
  add constraint api_credentials_mail_worker_ck check (
    credential_kind <> 'mail_worker'
    or (scopes = array['mail:ingest']::text[]
        and role = 'owner'
        and principal_kind = 'mcp_client'
        and token_prefix is not null
        and token_prefix ~ '^suvmail_[0-9a-f]{6}$')) not valid;
alter table ops.api_credentials validate constraint api_credentials_mail_worker_ck;

call ops.apply_security_baseline();
