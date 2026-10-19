-- =============================================================================
-- 20261006000900_backend_role_membership
-- Let the (non-superuser) migration owner switch into suv_backend.
--
-- On hosted Supabase the application connects as `postgres`, which is NOT a
-- superuser but has BYPASSRLS. ADR 0001 requires every application transaction
-- to run as suv_backend (`SET ROLE suv_backend`, Settings.database_set_role), so
-- that RLS really applies. SET ROLE needs membership with the SET option.
--
-- Membership is granted WITH INHERIT FALSE: the owner gains no suv_backend
-- privileges implicitly, it can only switch into the restricted role. This does
-- not make suv_backend a member of anything (ops.backend_role_problems stays []).
-- Superusers can SET ROLE without membership, so nothing happens for them.
-- =============================================================================

do $membership$
begin
  if not (select r.rolsuper from pg_catalog.pg_roles r where r.rolname = current_user)
     and not pg_catalog.pg_has_role(current_user, 'suv_backend', 'SET') then
    execute pg_catalog.format(
      'grant suv_backend to %I with inherit false, set true', current_user
    );
  end if;
end
$membership$;

do $verify$
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
$verify$;
