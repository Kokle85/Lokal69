-- =============================================================================
-- 20261006000100_extensions_roles
-- Schemas, the backend role, tenant GUC helpers and shared guard functions.
-- Spec sections 11 and 12; docs/decisions/0001-bff-only-data-access.md.
--
-- Forward-only. Every statement is safe on a Supabase stack and on plain
-- PostgreSQL 16 with supabase/tests/supabase_emulation.sql applied (tests).
--
-- Project SQLSTATEs raised by guard functions (documented in docs/schema.md):
--   SV001 append-only history may not be updated or deleted
--   SV002 state transition not permitted
--   SV003 dangling / cross-workspace / fixture-contaminated reference
--   SV004 frozen (immutable) column modified
--   SV005 monotonic value would decrease (row version, generation, last_seen_at)
--   SV006 detail generation was never allocated for the listing
-- =============================================================================

-- Preconditions: a Supabase stack (or the test emulation) must be present.
do $pre$
declare
  missing text[] := array[]::text[];
  r text;
begin
  if not exists (select 1 from pg_catalog.pg_namespace where nspname = 'auth') then
    missing := missing || 'schema auth'::text;
  elsif pg_catalog.to_regclass('auth.users') is null then
    missing := missing || 'table auth.users'::text;
  end if;
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if not exists (select 1 from pg_catalog.pg_roles where rolname = r) then
      missing := missing || ('role ' || r);
    end if;
  end loop;
  if pg_catalog.cardinality(missing) > 0 then
    raise exception using
      errcode = 'object_not_in_prerequisite_state',
      message = 'Supabase prerequisites missing: ' || pg_catalog.array_to_string(missing, ', '),
      hint = 'Run against a Supabase stack. Plain PostgreSQL test databases must first apply '
          || 'supabase/tests/supabase_emulation.sql (tests only).';
  end if;
end
$pre$;

create schema if not exists extensions;
-- btree_gist provides the equality operator classes used by the exclusion
-- constraint that forbids overlapping active tax rule sets (spec section 16).
create extension if not exists btree_gist with schema extensions;

-- Backend group role (ADR 0001). NOLOGIN: a deployment-specific LOGIN user is
-- granted membership, or the process runs SET ROLE suv_backend. Roles are
-- cluster-global, so creation tolerates a concurrent creator.
do $role$
begin
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'suv_backend') then
    begin
      create role suv_backend nologin noinherit nosuperuser nocreatedb nocreaterole
        noreplication nobypassrls;
    exception
      when duplicate_object or unique_violation then
        null;  -- created concurrently by another session
    end;
  end if;
end
$role$;

create schema if not exists app;
create schema if not exists ops;
comment on schema app is
  'Application records. Not exposed to the Supabase Data API (BFF-only, ADR 0001).';
comment on schema ops is
  'Queues, leases, outbox, auth mapping and audit internals. Never exposed.';

-- -----------------------------------------------------------------------------
-- Tenant GUC helpers. The repository layer sets these transaction-locally with
-- set_config(name, value, true). Unset or empty means NULL, so every policy
-- comparison fails closed. A malformed UUID raises (also fails closed).
-- -----------------------------------------------------------------------------
create or replace function app.current_workspace_id()
returns uuid
language sql
stable
set search_path = ''
as $$
  select nullif(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
$$;
comment on function app.current_workspace_id() is
  'Workspace selected for this transaction (GUC app.workspace_id); NULL when unset.';

create or replace function app.current_user_id()
returns uuid
language sql
stable
set search_path = ''
as $$
  select nullif(pg_catalog.current_setting('app.user_id', true), '')::uuid
$$;
comment on function app.current_user_id() is
  'Verified auth user for this transaction (GUC app.user_id); NULL when unset.';

create or replace function app.current_credential_hash()
returns text
language sql
stable
set search_path = ''
as $$
  select nullif(pg_catalog.current_setting('app.credential_hash', true), '')
$$;
comment on function app.current_credential_hash() is
  'SHA-256 hex of the presented API credential (GUC app.credential_hash) for credential bootstrap.';

-- -----------------------------------------------------------------------------
-- Immutable check helpers (usable in CHECK constraints).
-- -----------------------------------------------------------------------------
create or replace function app.text_array_ok(arr text[], max_items integer, max_len integer)
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
        where e.v is null
           or pg_catalog.length(e.v) = 0
           or pg_catalog.length(e.v) > max_len
     )
$$;
comment on function app.text_array_ok(text[], integer, integer) is
  'True when the array is non-null, has at most max_items elements, and every element is a non-empty string of at most max_len characters.';

create or replace function app.uuid_array_ok(arr uuid[], max_items integer)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select arr is not null
     and pg_catalog.cardinality(arr) <= max_items
     and pg_catalog.array_position(arr, null) is null
$$;
comment on function app.uuid_array_ok(uuid[], integer) is
  'True when the UUID array is non-null, bounded and contains no NULL element.';

-- -----------------------------------------------------------------------------
-- Shared trigger functions.
-- -----------------------------------------------------------------------------

-- Append-only history (spec section 11: immutable revision/evidence history).
-- Rejects UPDATE and DELETE for everyone, including superusers. The only
-- bypass is documented maintenance (e.g. the privacy deletion process of spec
-- section 24): a member of the table-owner role sets the transaction-local GUC
-- app.history_maintenance = 'on'. suv_backend is never a member of the owner.
create or replace function app.reject_history_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if pg_catalog.current_setting('app.history_maintenance', true) = 'on'
     and pg_catalog.pg_has_role(
           current_user,
           (select c.relowner from pg_catalog.pg_class c where c.oid = tg_relid),
           'USAGE') then
    if tg_op = 'DELETE' then
      return old;
    end if;
    return new;
  end if;
  raise exception using
    errcode = 'SV001',
    message = pg_catalog.format('%I.%I is append-only history; %s is not permitted',
                                tg_table_schema, tg_table_name, tg_op),
    hint = 'Insert a new (superseding) row instead of changing history.';
end
$$;

-- Maintains updated_at on mutable tables.
create or replace function app.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := pg_catalog.now();
  return new;
end
$$;

-- Optimistic-concurrency versions never decrease (prevents ABA resets).
-- tg_argv[0] names the version column.
create or replace function app.guard_version()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  col text := tg_argv[0];
  old_v bigint := (pg_catalog.to_jsonb(old) ->> col)::bigint;
  new_v bigint := (pg_catalog.to_jsonb(new) ->> col)::bigint;
begin
  if new_v < old_v then
    raise exception using
      errcode = 'SV005',
      message = pg_catalog.format('%I.%I.%I must not decrease (%s -> %s)',
                                  tg_table_schema, tg_table_name, col, old_v, new_v);
  end if;
  return new;
end
$$;

-- Freezes every column except those named in tg_argv (mutable lifecycle columns).
create or replace function app.guard_frozen_columns()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  changed text[];
begin
  select pg_catalog.array_agg(n.key order by n.key)
    into changed
    from pg_catalog.jsonb_each(pg_catalog.to_jsonb(new)) as n
    join pg_catalog.jsonb_each(pg_catalog.to_jsonb(old)) as o on o.key = n.key
   where n.value is distinct from o.value
     and not (n.key = any (tg_argv));
  if changed is not null then
    raise exception using
      errcode = 'SV004',
      message = pg_catalog.format('%I.%I: columns %s are immutable',
                                  tg_table_schema, tg_table_name,
                                  pg_catalog.array_to_string(changed, ', '));
  end if;
  return new;
end
$$;

-- -----------------------------------------------------------------------------
-- Security baseline, re-applied by every migration that adds objects:
--   * no privileges at all for PUBLIC/anon/authenticated/service_role on the
--     private schemas, their tables, sequences and routines (ADR 0001);
--   * RLS enabled on every app/ops table;
--   * a tenant_isolation policy for suv_backend on every table that has a
--     workspace_id column.
-- Grants to suv_backend are explicit per table in the migrations. Owner-only:
-- EXECUTE is revoked from everyone else by the procedure itself.
-- -----------------------------------------------------------------------------
create or replace procedure ops.apply_security_baseline()
language plpgsql
set search_path = ''
as $$
declare
  r record;
begin
  revoke all on schema app, ops from public, anon, authenticated, service_role;
  revoke all on all tables in schema app, ops from public, anon, authenticated, service_role;
  revoke all on all sequences in schema app, ops from public, anon, authenticated, service_role;
  revoke all on all routines in schema app, ops from public, anon, authenticated, service_role;

  for r in
    select c.oid::pg_catalog.regclass as rel,
           exists (
             select 1
               from pg_catalog.pg_attribute a
              where a.attrelid = c.oid
                and a.attname = 'workspace_id'
                and a.attnum > 0
                and not a.attisdropped
           ) as has_workspace
      from pg_catalog.pg_class c
      join pg_catalog.pg_namespace n on n.oid = c.relnamespace
     where n.nspname in ('app', 'ops')
       and c.relkind in ('r', 'p')
  loop
    execute pg_catalog.format('alter table %s enable row level security', r.rel);
    if r.has_workspace and not exists (
      select 1 from pg_catalog.pg_policy p
       where p.polrelid = r.rel and p.polname = 'tenant_isolation'
    ) then
      execute pg_catalog.format(
        'create policy tenant_isolation on %s as permissive for all to suv_backend '
        'using (workspace_id = (select app.current_workspace_id())) '
        'with check (workspace_id = (select app.current_workspace_id()))',
        r.rel);
    end if;
  end loop;
end
$$;
comment on procedure ops.apply_security_baseline() is
  'Owner-only maintenance: revoke client/public access to app/ops, enable RLS everywhere and add tenant_isolation policies. Call at the end of every migration that adds tables or routines.';

-- Future objects created by the migration role: no default client access.
-- Per-schema entries remove any schema-scoped defaults a platform may add; the
-- global entry stops new functions from being executable by PUBLIC (PostgreSQL
-- otherwise grants EXECUTE to PUBLIC by default on every new function).
alter default privileges in schema app, ops
  revoke all on tables from public, anon, authenticated, service_role;
alter default privileges in schema app, ops
  revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges in schema app, ops
  revoke all on functions from public, anon, authenticated, service_role;
alter default privileges
  revoke execute on functions from public;

call ops.apply_security_baseline();
