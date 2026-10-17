-- =============================================================================
-- 20261007000400_mail_worker_credential_kind
-- Spec v1.1 section 37.8 (forward-only, expand): a mailbox-worker binding is bound
-- to exactly one NARROW mailbox-worker credential.
--
-- Migration 20261007000200 introduced ops.api_credentials.credential_kind
-- 'mail_worker' (a suvmail_ token carrying only mail:ingest, owner role, machine
-- principal; api_credentials_mail_worker_ck). The worker binding guard still
-- accepted any live credential whose scopes were exactly {mail:ingest}, for
-- example a static_bearer (suvmcp_) credential. Such a token is an MCP token
-- shape and must never act as the desktop worker identity.
--
-- ops.mail_worker_bindings_guard() is replaced: an ACTIVE binding (insert,
-- credential rotation) needs a live credential of kind 'mail_worker' that
-- carries only mail:ingest. The refusal keeps its exact message (SQLSTATE
-- SV003), so persistence.errors_map keeps mapping it to VALIDATION_ERROR with
-- details.reason = 'mail_worker_credential_invalid'. Everything else in the
-- guard is unchanged (frozen mailbox identity, permanent revocation, monotonic
-- version and sync sequence, a rotation advances the version).
--
-- Existing rows are not rewritten: the guard runs on insert and on a credential
-- change only. persistence.mail_workers_repo authenticates workers with
-- credential kind 'mail_worker' only, so a binding that still points at an
-- older static_bearer credential cannot be used and must be rotated.
-- =============================================================================

create or replace function ops.mail_worker_bindings_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_scopes text[];
  v_kind text;
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
    select c.scopes, c.credential_kind, c.revoked_at, c.expires_at
      into v_scopes, v_kind, v_revoked, v_expires
      from ops.api_credentials c
     where c.workspace_id = new.workspace_id and c.id = new.credential_id;
    if v_scopes is distinct from array['mail:ingest']::text[]
       or v_kind is distinct from 'mail_worker'
       or v_revoked is not null
       or v_expires <= pg_catalog.now() then
      raise exception using
        errcode = 'SV003',
        message = 'a mailbox worker binding needs a live credential carrying only mail:ingest';
    end if;
  end if;
  return new;
end
$$;

comment on function ops.mail_worker_bindings_guard() is
  'Mailbox worker binding guard: frozen mailbox identity, permanent revocation, monotonic version, and a live mail_worker credential carrying only mail:ingest for every active binding.';

-- --- security baseline (unchanged model; re-applied by every migration) --------------------
call ops.apply_security_baseline();

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
