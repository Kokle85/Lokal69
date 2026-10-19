-- =============================================================================
-- 20261006000950_host_budgets_inflight_and_idempotency_scope
-- Additive (expand) changes requested by the persistence core:
--   1. ops.host_budgets gets real columns for the one-navigation-at-a-time lease
--      and the access-blocked marker (previously emulated with other columns).
--   2. Idempotency keys become unique per workspace. Every ActorContext.system
--      principal shares principal_id 00000000-..., so a key reused by system work in
--      two workspaces must not collide. The old (principal, operation, key)
--      constraint stays until the application uses the new key (contract later).
-- Forward-only; safe on Supabase and on the plain-PostgreSQL test emulation.
-- =============================================================================

alter table ops.host_budgets
  add column if not exists in_flight_until timestamptz,
  add column if not exists in_flight_token uuid,
  add column if not exists access_blocked_at timestamptz,
  add column if not exists access_blocked_reason text;

do $ck$
begin
  if not exists (select 1 from pg_catalog.pg_constraint
                  where conname = 'host_budgets_in_flight_ck'
                    and conrelid = 'ops.host_budgets'::regclass) then
    alter table ops.host_budgets
      add constraint host_budgets_in_flight_ck
        check ((in_flight_until is null) = (in_flight_token is null));
  end if;
  if not exists (select 1 from pg_catalog.pg_constraint
                  where conname = 'host_budgets_blocked_reason_ck'
                    and conrelid = 'ops.host_budgets'::regclass) then
    alter table ops.host_budgets
      add constraint host_budgets_blocked_reason_ck
        check (access_blocked_reason is null or pg_catalog.length(access_blocked_reason) <= 500);
  end if;
end
$ck$;

create unique index if not exists idempotency_records_ws_key_uidx
  on ops.idempotency_records (workspace_id, principal_id, operation, idempotency_key);

call ops.apply_security_baseline();
