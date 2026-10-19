-- =============================================================================
-- Supabase emulation for plain PostgreSQL test databases
-- =============================================================================
--
-- TEST-ONLY. NEVER apply this file to a real Supabase project (hosted or
-- `supabase start`). A real Supabase stack already provides everything below;
-- the test harness (tests/db_harness.py) only applies this file when the target
-- database has no `auth` schema at all.
--
-- What it emulates (mirroring supabase/postgres init scripts and supabase/auth
-- migrations as recorded in docs/research/frontend_and_supabase.md section 7):
--   * cluster roles anon, authenticated (NOLOGIN) and service_role
--     (NOLOGIN, BYPASSRLS);
--   * schema `auth` with a minimal `auth.users` table;
--   * auth.uid(), auth.role(), auth.email(), auth.jwt() reading the
--     transaction-local GUCs that PostgREST sets (`request.jwt.claims` JSON and
--     the legacy `request.jwt.claim.<name>` settings);
--   * schema `extensions` with pgcrypto installed into it, as on Supabase.
--
-- Safety and idempotency:
--   * Roles are cluster-global and test databases are created concurrently, so
--     roles are created only when missing and concurrent creation by another
--     session is tolerated (duplicate_object / unique_violation are ignored).
--   * Every other statement is idempotent (IF NOT EXISTS / CREATE OR REPLACE),
--     so applying this file twice to the same database is a no-op.
--   * If an `auth` schema exists that was NOT created by this emulation (i.e. a
--     real Supabase auth schema), the file refuses to run and changes nothing.
-- =============================================================================

do $guard$
declare
  auth_comment text;
begin
  if exists (select 1 from pg_catalog.pg_namespace where nspname = 'auth') then
    select pg_catalog.obj_description(n.oid, 'pg_namespace')
      into auth_comment
      from pg_catalog.pg_namespace n
     where n.nspname = 'auth';
    if coalesce(auth_comment, '') not like 'suv-deals test emulation%' then
      raise exception using
        errcode = 'object_not_in_prerequisite_state',
        message = 'schema auth already exists and was not created by the suv-deals test emulation',
        hint = 'This file is test-only; never apply it to a Supabase project.';
    end if;
  end if;
end
$guard$;

-- Cluster-global roles. Current Supabase roles are NOLOGIN and INHERIT
-- (supabase/postgres migration 20230529180330 switched them to INHERIT).
do $roles$
declare
  r record;
begin
  for r in
    select v.role_name, v.bypass_rls
      from (values ('anon', false), ('authenticated', false), ('service_role', true))
           as v(role_name, bypass_rls)
  loop
    if not exists (select 1 from pg_catalog.pg_roles where rolname = r.role_name) then
      begin
        if r.bypass_rls then
          execute format('create role %I nologin inherit bypassrls', r.role_name);
        else
          execute format('create role %I nologin inherit', r.role_name);
        end if;
      exception
        when duplicate_object or unique_violation then
          -- Another test session created the role concurrently; nothing to do.
          null;
      end;
    end if;
  end loop;
end
$roles$;

create schema if not exists auth;
comment on schema auth is
  'suv-deals test emulation of the Supabase auth schema (test-only; never apply to Supabase)';

create schema if not exists extensions;
comment on schema extensions is
  'suv-deals test emulation of the Supabase extensions schema (test-only)';

create extension if not exists pgcrypto with schema extensions;

-- Minimal subset of Supabase auth.users (the real table has many more columns).
create table if not exists auth.users (
  instance_id uuid,
  id uuid not null primary key,
  aud varchar(255),
  role varchar(255),
  email varchar(255),
  email_confirmed_at timestamptz,
  raw_app_meta_data jsonb,
  raw_user_meta_data jsonb,
  is_anonymous boolean not null default false,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Helper functions exactly as current Supabase defines them. After a
-- PostgREST transaction ends the GUCs read as '' (not NULL), hence nullif.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

create or replace function auth.role()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text
$$;

create or replace function auth.email()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.email', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')
  )::text
$$;

create or replace function auth.jwt()
returns jsonb
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')
  )::jsonb
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant usage on schema extensions to anon, authenticated, service_role;
grant execute on function auth.uid(), auth.role(), auth.email(), auth.jwt()
  to anon, authenticated, service_role;
