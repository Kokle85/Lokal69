-- =============================================================================
-- 20261006000800_security_rls_grants
-- Append-only history protection, workspace RLS for suv_backend and explicit
-- least-privilege grants. Spec sections 11 and 12; ADR 0001 (BFF-only).
--
--   * anon / authenticated / service_role / PUBLIC: no schema usage, no table,
--     sequence or routine privileges on app/ops (apply_security_baseline).
--   * suv_backend: schema usage plus exactly the per-table privileges below.
--     No TRUNCATE, no DDL, no DELETE except ops.query_snapshots and
--     ops.idempotency_records expiry. Immutable history is INSERT/SELECT only.
--   * Every workspace-owned table: RLS + tenant_isolation policy
--     (workspace_id = app.current_workspace_id()) for USING and WITH CHECK.
--   * Superuser/owner connections (migrations, tests) bypass RLS by design.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Append-only history: BEFORE UPDATE OR DELETE triggers reject changes for
-- every role (defence in depth beyond the missing grants). See
-- app.reject_history_mutation() for the documented maintenance bypass.
-- -----------------------------------------------------------------------------
create trigger config_revisions_append_only before update or delete on app.config_revisions
  for each row execute function app.reject_history_mutation();
create trigger listing_revisions_append_only before update or delete on app.listing_revisions
  for each row execute function app.reject_history_mutation();
create trigger detail_observations_append_only before update or delete on app.detail_observations
  for each row execute function app.reject_history_mutation();
create trigger listing_observations_append_only before update or delete on app.listing_observations
  for each row execute function app.reject_history_mutation();
create trigger listing_aliases_append_only before update or delete on app.listing_aliases
  for each row execute function app.reject_history_mutation();
create trigger field_evidence_append_only before update or delete on app.field_evidence
  for each row execute function app.reject_history_mutation();
create trigger market_observations_append_only before update or delete on app.market_observations
  for each row execute function app.reject_history_mutation();
create trigger comparable_sets_append_only before update or delete on app.comparable_sets
  for each row execute function app.reject_history_mutation();
create trigger comparable_set_members_append_only before update or delete on app.comparable_set_members
  for each row execute function app.reject_history_mutation();
create trigger fx_rates_append_only before update or delete on app.fx_rates
  for each row execute function app.reject_history_mutation();
create trigger cost_evidence_append_only before update or delete on app.cost_evidence
  for each row execute function app.reject_history_mutation();
create trigger review_decisions_append_only before update or delete on app.review_decisions
  for each row execute function app.reject_history_mutation();
create trigger audit_events_append_only before update or delete on ops.audit_events
  for each row execute function app.reject_history_mutation();
create trigger delivery_attempts_append_only before update or delete on ops.delivery_attempts
  for each row execute function app.reject_history_mutation();

-- -----------------------------------------------------------------------------
-- Cross-workspace system work (ADR 0001 point 5): schedulers and workers list
-- active workspaces through this narrowly scoped SECURITY DEFINER function,
-- then process each workspace under its own GUC. Returns identifiers only.
-- -----------------------------------------------------------------------------
create or replace function ops.active_workspace_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select w.id
    from app.workspaces w
   where w.active
   order by w.id
$$;
comment on function ops.active_workspace_ids() is
  'SECURITY DEFINER: ids of active workspaces for scheduler/worker fan-out. EXECUTE granted to suv_backend only.';

-- Revoke client/public access everywhere, enable RLS and add tenant policies.
call ops.apply_security_baseline();

-- -----------------------------------------------------------------------------
-- Policies beyond tenant_isolation.
-- -----------------------------------------------------------------------------
-- app.workspaces has no workspace_id column: isolate on id.
create policy tenant_isolation on app.workspaces
  as permissive for all to suv_backend
  using (id = (select app.current_workspace_id()))
  with check (id = (select app.current_workspace_id()));
-- Workspace bootstrap: a verified user may read the workspaces they are an
-- active member of before one is selected (GUC app.user_id).
create policy workspace_member_read on app.workspaces
  as permissive for select to suv_backend
  using (exists (
    select 1
      from app.memberships m
     where m.workspace_id = workspaces.id
       and m.user_id = (select app.current_user_id())
       and m.active));
-- Membership bootstrap (ADR 0001 point 4).
create policy membership_self_read on app.memberships
  as permissive for select to suv_backend
  using (user_id = (select app.current_user_id()));
-- Credential bootstrap: the auth layer sets app.credential_hash to the SHA-256
-- of the presented bearer token and can read exactly that credential row.
create policy credential_lookup on ops.api_credentials
  as permissive for select to suv_backend
  using (token_hash = (select app.current_credential_hash()));

-- -----------------------------------------------------------------------------
-- Grants to suv_backend (explicit, least privilege).
-- -----------------------------------------------------------------------------
grant usage on schema app, ops to suv_backend;

grant execute on function
  app.current_workspace_id(),
  app.current_user_id(),
  app.current_credential_hash(),
  app.text_array_ok(text[], integer, integer),
  app.uuid_array_ok(uuid[], integer),
  ops.active_workspace_ids()
to suv_backend;

-- Core.
grant select, update (name, display_timezone) on table app.workspaces to suv_backend;
grant select, insert, update (role, active) on table app.memberships to suv_backend;
grant select, insert on table app.config_revisions to suv_backend;
grant select, insert, update on table app.sources to suv_backend;
grant select, insert, update on table app.search_profiles to suv_backend;

-- Queue and crawl operations.
grant select, insert, update on table ops.jobs to suv_backend;
grant select, insert, update on table ops.crawl_runs to suv_backend;
grant select, insert, update on table ops.source_schedules to suv_backend;
grant select, insert, update on table ops.host_budgets to suv_backend;
grant select, insert on table ops.robots_revisions to suv_backend;
grant select, insert, update (redaction_status, redacted_at, purged_at) on table ops.source_snapshots to suv_backend;
grant select, insert on table ops.fetch_attempts to suv_backend;

-- Listings and evidence.
grant select, insert, update on table app.listings to suv_backend;
grant select, insert on table app.detail_observations to suv_backend;
grant select, insert on table app.listing_revisions to suv_backend;
grant select, insert on table app.listing_aliases to suv_backend;
grant select, insert on table app.listing_observations to suv_backend;
grant select, insert, update on table app.vehicle_clusters to suv_backend;
grant select, insert,
  update (manually_confirmed, confirmed_by, confirmed_at, unlinked_at, unlinked_by, unlink_reason)
  on table app.vehicle_cluster_members to suv_backend;
grant select, insert on table app.field_evidence to suv_backend;

-- Market, costs, tax and valuations.
grant select, insert on table app.market_observations to suv_backend;
grant select, insert on table app.comparable_sets to suv_backend;
grant select, insert on table app.comparable_set_members to suv_backend;
grant select, insert on table app.fx_rates to suv_backend;
grant select, insert, update on table app.tax_rule_sets to suv_backend;
grant select, insert, update (approval_status, approved_by, approved_at) on table app.cost_profiles to suv_backend;
grant select, insert on table app.cost_evidence to suv_backend;
grant select, insert, update (state, stale_at, stale_reason) on table app.valuations to suv_backend;

-- Reviews and notifications.
grant select, insert, update on table app.review_cases to suv_backend;
grant select, insert on table app.review_decisions to suv_backend;
grant select, insert, update on table app.watchlists to suv_backend;
grant select, insert, update (body, row_version) on table app.owner_notes to suv_backend;
grant select, insert, update on table app.destination_bindings to suv_backend;
grant select, insert, update on table app.notification_preferences to suv_backend;

-- Outbox, events, pagination, idempotency, audit, gates, credentials.
grant select, insert, update on table ops.outbox to suv_backend;
grant select, insert on table ops.delivery_attempts to suv_backend;
grant select, insert, update on table ops.event_subscriptions to suv_backend;
grant select, insert, update on table ops.event_deliveries to suv_backend;
grant select, insert, delete on table ops.query_snapshots to suv_backend;
grant select, insert, update, delete on table ops.idempotency_records to suv_backend;
grant select, insert on table ops.audit_events to suv_backend;
grant select, insert, update on table ops.activation_gates to suv_backend;
grant select, insert, update (last_used_at, revoked_at, revoked_by, revoke_reason)
  on table ops.api_credentials to suv_backend;

-- Fail closed if, after all grants, the backend role could bypass RLS or escalate
-- (see ops.backend_role_problems() in migration 0100).
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
