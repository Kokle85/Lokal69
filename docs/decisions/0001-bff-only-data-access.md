# ADR 0001: BFF-only data access with workspace-scoped RLS for the backend role

Status: accepted (engineering decision; no owner input required)
Date: 2026-10-06

## Context

Spec section 12 requires choosing one data-access path: either the browser reads
`app` tables directly through the Supabase Data API under RLS, or a backend-for-frontend
(BFF) performs every validated, scoped read. An accidental hybrid is not allowed.

## Decision

1. **BFF-only.** The dashboard signs in with Supabase Auth (publishable key, user session)
   and then calls this project's backend API with the user's access token. The browser
   never queries `app` or `ops` tables. The `app` and `ops` schemas are **not** exposed
   to the Supabase Data API, and `anon`/`authenticated` receive **no** schema usage or
   table privileges on them (`revoke all` is explicit in migrations).
2. **Dedicated backend role.** Application processes use the NOLOGIN group role
   `suv_backend` (a deployment-specific LOGIN user is granted membership, or the process
   runs `SET ROLE suv_backend` after connecting). The role has only the table privileges
   it needs: no `TRUNCATE`, no `DELETE` on append-only history, no DDL.
3. **Workspace RLS as defence in depth.** Every workspace-owned table has RLS enabled
   with a `tenant_isolation` policy for `suv_backend`:
   `workspace_id = app.current_workspace_id()` for `USING` and `WITH CHECK`, where
   `app.current_workspace_id()` reads the transaction-local GUC `app.workspace_id`.
   The repository layer sets it with `select set_config('app.workspace_id', $1, true)`
   at the start of every transaction from the validated `ActorContext`. A server bug
   that forgets a `workspace_id` predicate therefore still cannot cross tenants.
4. **Membership bootstrap.** Resolving which workspaces a user belongs to happens before
   a workspace is selected. `app.memberships` additionally allows reading rows where
   `user_id = app.current_user_id()` (GUC `app.user_id`, set by the auth layer after the
   JWT is verified).
5. **Cross-workspace system work.** Schedulers and workers list workspaces through the
   narrowly scoped `SECURITY DEFINER` function `ops.active_workspace_ids()` (fixed
   `search_path`, `REVOKE EXECUTE FROM PUBLIC`, granted only to `suv_backend`), then
   process each workspace with its own GUC. Job claims are always per workspace.
6. **Supabase service-role/secret key** is a server-only fallback. If it is used instead
   of `suv_backend`, RLS is bypassed; application authorization (ActorContext checks and
   explicit `workspace_id` predicates in every query) remains mandatory and is tested.

## Consequences

- Positive/negative tests: `anon` and `authenticated` get `permission denied` on every
  `app`/`ops` table; `suv_backend` with workspace A cannot read or write workspace B rows;
  composite foreign keys reject cross-workspace links even for privileged roles;
  `suv_backend` cannot update/delete immutable history.
- Role/permission differences (owner/reviewer/viewer) are enforced in the application
  layer through `ActorContext` scopes and tested there.
- Superuser connections (used only by migrations and test setup) bypass RLS by design.
