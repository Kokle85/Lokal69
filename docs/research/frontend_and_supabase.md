# Frontend (React/Vite) and Supabase Auth/DB: verified reference (2026-10-06)

How this was checked: `npm view` against the live registry, plus installed packages read in throwaway dirs under
`/tmp/claude-0/.../scratchpad/{frontend,pyjwt,pgtest}`. A probe app (Vite 8 + React 19.3 + React Router 8 + supabase-js)
was built with `tsc -b && vite build` and tested with Vitest 5 and Playwright 1.56.1 using the preinstalled Chromium. Every
step passed. A PyJWT 2.15.1 JWKS test (ES256 + RS256 + HS256) and the auth-emulation SQL (on local PostgreSQL 16.15) also ran
and passed. Supabase docs come from `supabase.com/docs/guides/*.md`. The CLI, Postgres and Auth sources come from git HEAD of
`supabase/cli` (2026-10-06), `supabase/postgres` (2026-10-06) and `supabase/auth` (2026-09-22).
Legend: **[V]** = verified by running or reading installed code. **[D]** = taken from official docs or source. **UNVERIFIED** = flagged inline.

## 1. Environment facts (this sandbox)

| Item | Value |
|---|---|
| Node / npm | **v22.22.0** / 10.9.4 |
| Playwright browsers | `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`, `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`; `chromium-1194` (Chromium 141.0.7390.37), `chromium_headless_shell-1194`, `ffmpeg-1011`; installed by `/opt/node-tools` playwright **1.56.1** |
| PostgreSQL | `/usr/lib/postgresql/16` (psql 16.15). Can run a throwaway cluster with `runuser -u postgres -- initdb/pg_ctl` |
| Docker | Client 29.8.2 is installed, but **no daemon** (`/var/run/docker.sock` is missing), so `supabase start` **cannot run here**. Use the plain-Postgres harness (§7) |
| uv | 0.11.32 |

## 2. Versions (npm `latest` as of 2026-10-06) and recommended pins

| Package | latest | engines.node | Recommended pin / notes |
|---|---|---|---|
| vite | **8.3.3** (8.0.0 released 2026-03-12) | `^20.19.0 \|\| >=22.12.0` | `^8.3.3`. Bundler is Rolldown + Oxc (`build.rolldownOptions`, not `rollupOptions`) [V] |
| @vitejs/plugin-react | **6.1.2** | `^20.19.0 \|\| >=22.12.0` | peer `vite ^8`. Babel is optional (`@rolldown/plugin-babel`, `babel-plugin-react-compiler` are optional peers) |
| react / react-dom | **19.3.0** (2026-09-09) | n/a | `^19.3.0` |
| @types/react / @types/react-dom | 19.3.0 / 19.3.0 | n/a | |
| react-router | **8.4.0** | `>=22.22.0` | peer `react >=19.2.7`. Node 22.22.0 just meets it |
| typescript | **7.0.2** (native Go port), `next` 7.1.0-dev | `>=16.20.0` | **Use `~6.0.2`** (create-vite 9.2.1 template pins `~6.0.2`; resolves to 6.0.3). TS 7 `tsc -b` worked on the probe [V], but `import('typescript')` only exposes `version`/`versionMajorMinor`, **with no compiler JS API**, so typescript-eslint and other API consumers break |
| vitest | **5.0.3** | `^22.12.0 \|\| ^24 \|\| >=26` | peer `vite ^6.4 \|\| ^7 \|\| ^8` |
| jsdom | **30.1.2** | `^22.22.2 \|\| ^24.15.0 \|\| >=26` | **EBADENGINE warning on Node 22.22.0** (only a warning; tests passed [V]). To avoid the warning, use `jsdom@29.1.1` (`^22.13.0`) or `happy-dom@20.14.5` (`>=20`) |
| @testing-library/react | **16.3.3** | `>=18` | peer `@testing-library/dom ^10` → install `@testing-library/dom@10.4.2` explicitly |
| @testing-library/jest-dom | **7.0.1** | `>=22` | exports `.`, `./vitest`, `./matchers`, `./jest-globals` |
| @testing-library/user-event | 14.6.7 | | |
| @playwright/test | latest **1.63.0** (needs chromium rev **1243**/153.0) | `>=20` | **Pin `1.56.1`**. 1.56.0/1.56.1 use chromium rev **1194** = the preinstalled build (1.55.1 = 1193, 1.57.0 = 1200) [V via browsers.json] |
| @supabase/supabase-js | **2.117.2** (2026-09-25); sub-packages auth-js/postgrest-js/realtime-js/storage-js/functions-js all 2.117.2 | `>=22.0.0` | `^2.117.2` |
| supabase (CLI on npm) | **2.120.0** (2026-10-06) | | `npm i -D supabase`, then run as `npx supabase ...`. Binaries come via `@supabase/cli-<os>-<arch>` optionalDependencies |
| create-vite | 9.2.1 | | template `react-ts`: react ^19.2.8, vite ^8.3.0, plugin-react ^6.1.1, typescript ~6.0.2, oxlint ^1.81.0 |

## 3. Vite + React + TS dashboard skeleton (verified build + tests)

`vite.config.ts` [V] (`tsc -b` type-checks this file through `tsconfig.node.json`; the triple-slash reference adds the `test` key):
```ts
/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8000' } },   // FastAPI backend
  test: { environment: 'jsdom', globals: true, setupFiles: ['./src/test/setup.ts'],
          include: ['src/**/*.test.{ts,tsx}'] },
})
```
- `src/test/setup.ts`: `import '@testing-library/jest-dom/vitest'` [V]
- In `tsconfig.app.json` (create-vite template: target es2023, `moduleResolution: "bundler"`, `verbatimModuleSyntax`, `erasableSyntaxOnly`, `noEmit`, `jsx: react-jsx`), set `"types": ["vite/client", "vitest/globals"]`.
- Scripts: `"build": "tsc -b && vite build"`, `"test": "vitest run"`, `"e2e": "playwright test"`.
- Env vars must be prefixed `VITE_` (`import.meta.env.VITE_SUPABASE_URL`, `import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY`).

### React Router 8 (8.4.0) [V]
- v8 **removed the `react-router-dom` package**. `RouterProvider` and `HydratedRouter` now come from **`react-router/dom`**, and everything else comes from **`react-router`**. Packages are ESM-only; the minimum is Node 22.22.0 and React 19.2.7. Middleware is always on.
- Data mode (SPA): `createBrowserRouter(routes)` + `<RouterProvider router={router} />`. Exports confirmed: `createBrowserRouter, createMemoryRouter, RouterProvider, Link, NavLink, Navigate, Outlet, useNavigate, useLoaderData, useParams, useSearchParams, redirect, ...`.
- For tests, `createMemoryRouter(routes, { initialEntries: ['/login'] })` with `RouterProvider` imported from `react-router` works in jsdom [V].
```tsx
// main.tsx
import { RouterProvider } from 'react-router/dom'
import { createBrowserRouter } from 'react-router'
createRoot(document.getElementById('root')!).render(<StrictMode><RouterProvider router={createBrowserRouter(routes)} /></StrictMode>)
```

### Playwright config (passed with preinstalled chromium, no download) [V]
```ts
import { defineConfig, devices } from '@playwright/test'   // @playwright/test@1.56.1
export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'on-first-retry' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: { command: 'npx vite preview --host 127.0.0.1 --port 4173 --strictPort',
               url: 'http://127.0.0.1:4173', reuseExistingServer: !process.env.CI },
})
```
`vite preview` serves the SPA fallback for deep links such as `/login` [V]. Do not run `npx playwright install`: downloads are disabled.

## 4. @supabase/supabase-js 2.117.2 browser auth API [V from installed .d.ts + runtime]

```ts
import { createClient, type Session } from '@supabase/supabase-js'
export const supabase = createClient(url, publishableKey, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true /*, flowType: 'pkce' */ },
})
```
- Signature: `createClient<Database = any, SchemaNameOrClientOptions, SchemaName>(supabaseUrl: string, supabaseKey: string, options?: SupabaseClientOptions<SchemaName>)`. The auth options are `autoRefreshToken, storageKey, persistSession, detectSessionInUrl, storage, userStorage, flowType, debug`. A third-party auth `accessToken?: () => Promise<string|null>` option disables the `auth` namespace.
- `auth.signInWithPassword({ email, password, options?: { captchaToken } }): Promise<AuthTokenResponsePassword>` returns `{ data: { user, session }, error }`.
- `auth.signInWithOtp({ email, options?: { emailRedirectTo?, shouldCreateUser? (default true), data?, captchaToken? } })` (or `{ phone, options }`) returns `AuthOtpResponse`. The magic link/OTP is completed by `detectSessionInUrl` or by `auth.verifyOtp({ email, token, type: 'email' })`.
- `auth.getSession()` resolves to `{ data: { session: Session | null }, error }`. `Session` has `access_token, refresh_token, expires_in, expires_at?, token_type: 'bearer', user`. In browsers it returns a valid (auto-refreshed) token. The docs now call it "low-level" and recommend `getClaims()`/`getUser()` for identity, and say the user object must not be trusted when it comes from insecure storage. That warning matters for servers; for the SPA → backend Bearer flow, use `session.access_token`.
- `auth.onAuthStateChange((event, session) => void)` returns `{ data: { subscription } }`. Events: `INITIAL_SESSION | SIGNED_IN | SIGNED_OUT | TOKEN_REFRESHED | USER_UPDATED | PASSWORD_RECOVERY | MFA_CHALLENGE_VERIFIED`. **The async-callback overload is `@deprecated`** (it can deadlock on a nested refresh), so keep callbacks synchronous. It fires `INITIAL_SESSION` immediately [V].
- `auth.getClaims(jwt?, { allowExpired?, jwks? })` returns `{ data: { claims, header, signature } }`. It verifies locally with WebCrypto against `${url}/auth/v1/.well-known/jwks.json` (JWKS_TTL = 10 min in auth-js). With HS256 projects it falls back to a server call.
- New-format keys: supabase-js detects the `sb_publishable_`/`sb_secret_` prefixes, always sends them in the **`apikey` header**, and does not treat them as JWTs. Auto-refresh tick = 30 s, expiry margin = 90 s.

Calling the FastAPI backend [V compiled + unit-tested]:
```ts
export async function apiFetch(path: string, init: RequestInit = {}) {
  const { data: { session } } = await supabase.auth.getSession()
  const headers = new Headers(init.headers)
  if (session) headers.set('Authorization', `Bearer ${session.access_token}`)
  return fetch(`/api${path}`, { ...init, headers })
}
```

## 5. Supabase Auth JWTs: what a Python backend must verify [D]

- **JWKS**: `GET <SUPABASE_URL>/auth/v1/.well-known/jwks.json` returns only the public asymmetric keys (`kid`, `alg`, `use:"sig"`, `key_ops:["verify"]`). It returns **no keys** while the project still signs with the legacy HS256 secret. The edge caches it for 10 min and client libraries cache it for another 10 min, so a rotation or revocation can take about 20 min to propagate.
- **Issuer**: `iss = "<SUPABASE_URL>/auth/v1"` (hosted example `https://<ref>.supabase.co/auth/v1`). The docs say "If you append `/.well-known/jwks.json` to this URL you'll get access to the public keys". Locally, iss = `http://127.0.0.1:54321/auth/v1` (CLI sets `GOTRUE_JWT_ISSUER` = auth external URL = `${api_url}/auth/v1` unless `[auth].jwt_issuer` is set) [D: CLI source].
- **Audience**: `aud = "authenticated"` for signed-in users (`GOTRUE_JWT_AUD=authenticated`). The docs type it as `string | string[]`.
- **Required claims** (docs: "always present... cannot be removed"): `iss, aud, exp, iat, sub (user UUID), role, aal ("aal1"|"aal2"), session_id, email, phone, is_anonymous`. Optional: `jti, nbf, app_metadata, user_metadata, amr ([{method,timestamp}])`. `ref` appears only in anon/service_role API-key JWTs.
- **Algorithms**: `ES256` (P-256, recommended), `RS256` (RSA 2048), `EdDSA` ("coming soon"), and `HS256` (legacy shared secret, "Not recommended for production"). Header: `{alg, typ:"JWT", kid}`.
  - **UNVERIFIED**: which algorithm a *new hosted* project gets by default in Oct 2026. A 2024 changelog said "RS256 by default" for projects after 2025-05-01, and the current docs recommend ES256. **Accept `["ES256","RS256"]`** and let the JWK decide.
  - Local CLI (HEAD source): GoTrue signs user tokens with a built-in **ES256** key (`kid b81269f1-21d8-4f2e-b719-c2240a840d90`) unless `[auth].signing_keys_path` is set; `GOTRUE_JWT_VALIDMETHODS=HS256,RS256,ES256`.
- **Legacy HS256**: all projects were migrated into the signing-keys system on 2025-10-01. If a project is still on the legacy secret, the docs prefer calling `GET <url>/auth/v1/user` with `Authorization: Bearer <jwt>` + `apikey` over local HS256 verification. The local CLI legacy secret is `super-secret-jwt-token-with-at-least-32-characters-long`. Legacy anon/service_role JWT keys carry `iss:"supabase"` (local `"supabase-demo"`), `role`, and no `aud`/`sub`, so **requiring `aud=authenticated` + `sub` rejects them**.
- **API keys**: publishable `sb_publishable_...` (maps to role `anon`, or `authenticated` once a user JWT is sent; safe in browsers) and secret `sb_secret_...` (maps to `service_role`, BYPASSRLS, backend only). A secret key **returns 401 in browsers** (User-Agent check). Neither is a JWT. Send them in the **`apikey` header, never as `Authorization: Bearer`**. **Legacy `anon`/`service_role` keys are deprecated "by the end of 2026"**. Local defaults (CLI source): `sb_publishable_ACJWlzQHlZjBrEguHvfOxg_3BJgxAaH`, `sb_secret_N7UND0UgjKTVK-Uodkm0Hg_xSvEMPvz`. `supabase status` prints the Publishable and Secret keys.

## 6. PyJWT 2.15.1 verification (`pyjwt[crypto]`, cryptography 50.0.2, requires Python >=3.9; released 2026-09-28) [V]

`PyJWKClient.__init__(uri, cache_keys=False, max_cached_keys=16, cache_jwk_set=True, lifespan=300, headers=None, timeout=30, ssl_context=None, cooldown_duration=30)`
- Only http/https URIs are accepted. Fetching uses **synchronous urllib** with `_NoRedirectHandler` (**redirects are not followed**), and the env proxy is honored (`NO_PROXY` covers 127.0.0.1 here).
- An unknown `kid` triggers one forced refresh, at most once per `cooldown_duration`. Methods: `get_signing_key_from_jwt(token) -> PyJWK`, `get_signing_key(kid)`, `get_jwk_set(refresh=False)`, `get_signing_keys()`. A failed fetch raises `PyJWKClientConnectionError` but does not wipe the cache.
- Exceptions (all subclass `jwt.PyJWTError`): `ExpiredSignatureError, InvalidAudienceError, InvalidIssuerError, InvalidAlgorithmError, MissingRequiredClaimError, InvalidSignatureError, DecodeError, PyJWKClientError, PyJWKClientConnectionError, PyJWKSetError` (an empty JWKS gives "The JWK Set did not contain any keys").
- `jwt.decode(jwt, key, algorithms, options, audience, subject, issuer, leeway, ...)`. `key` may be the **`PyJWK` itself** (its alg is enforced against `algorithms`) or `PyJWK.key`. `options["require"]` lists the claims that must be present.

```python
import jwt
from jwt import PyJWKClient

ISSUER = f"{SUPABASE_URL.rstrip('/')}/auth/v1"
_jwks = PyJWKClient(f"{ISSUER}/.well-known/jwks.json", lifespan=600, timeout=5)

def verify_supabase_jwt(token: str) -> dict:
    signing_key = _jwks.get_signing_key_from_jwt(token)          # PyJWK (kid lookup, cached)
    claims = jwt.decode(
        token, signing_key, algorithms=["ES256", "RS256"],
        audience="authenticated", issuer=ISSUER, leeway=30,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )
    if claims.get("role") != "authenticated":
        raise jwt.InvalidTokenError("not an authenticated user token")
    return claims   # sub, role, aal, session_id, email, is_anonymous, amr ...

# legacy HS256 fallback (only if JWKS is empty / project not migrated):
# jwt.decode(token, LEGACY_JWT_SECRET, algorithms=["HS256"], audience="authenticated", issuer=ISSUER)
```
Test results: ES256 and RS256 tokens served from a local JWKS verified. Wrong `aud` raised `InvalidAudienceError`, wrong `iss` raised `InvalidIssuerError`, ES256 key with `algorithms=["RS256"]` raised `InvalidAlgorithmError`, an unknown kid raised `PyJWKClientError`, and an expired token raised `ExpiredSignatureError`. In async FastAPI, wrap the first call in `run_in_threadpool`/`anyio.to_thread.run_sync`, or warm the cache at startup, because the urllib fetch blocks. For tests, mint tokens with `jwt.encode(claims, ec_private_key, algorithm="ES256", headers={"kid": kid})`, publish `ECAlgorithm.to_jwk(pub)` (+`kid`,`alg`,`use`) from a local HTTP server, and point the client at it.

## 7. Supabase Postgres roles and auth helpers, plus a faithful plain-Postgres emulation

Roles, from `supabase/postgres` `migrations/db/init-scripts/00000000000000-initial-schema.sql` [D]:
```sql
create role anon          nologin noinherit;
create role authenticated nologin noinherit;
create role service_role  nologin noinherit bypassrls;
create user authenticator noinherit;            -- PostgREST login role
grant anon, authenticated, service_role, supabase_admin to authenticator;
grant usage on schema public, extensions to postgres, anon, authenticated, service_role;
alter role anon set statement_timeout = '3s'; alter role authenticated set statement_timeout = '8s';
-- later migrations: 20230529180330 => ALTER ROLE anon/authenticated/service_role INHERIT;
-- authenticator: statement_timeout '8s', lock_timeout '8s'
```
The initial schema also has `alter default privileges in schema public grant all on tables/functions/sequences to anon, authenticated, service_role`, which §8 now revokes.

Current auth helpers, from `supabase/auth` migrations `20220224000811_update_auth_functions` + `20220531120530_add_auth_jwt_function`. They override the old init-script versions, which only read `request.jwt.claim.<x>` [D]:
```sql
create or replace function auth.uid() returns uuid language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$;
create or replace function auth.role() returns text language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.role', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'))::text $$;
create or replace function auth.email() returns text language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.email', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email'))::text $$;
create or replace function auth.jwt() returns jsonb language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim', true), ''),
                  nullif(current_setting('request.jwt.claims', true), ''))::jsonb $$;
-- comments: uid()/role()/email() are 'Deprecated. Use auth.jwt() -> ''sub'' instead.' (still universally used)
grant usage on schema auth to anon, authenticated, service_role;
```
PostgREST per-request behavior (docs `references/transactions.rst`, `auth.rst`; image `postgrest/postgrest:v16.4` in the CLI): `START TRANSACTION; <transaction-scoped settings>; SET LOCAL ROLE <jwt role claim or anon>; <main query>; END`. `request.jwt.claims` holds the whole claim set as JSON, and `request.headers`/`request.cookies` are JSON with lower-cased header names. `request.method`/`request.path` are text. All are set transaction-locally (`set_config(..., true)`). **After the transaction they read as `''`, not NULL**, which is why the helpers use `nullif`.

Emulation for the PG16 test harness (the role and helper SQL above, plus this pattern) [V on PG 16.15]:
```sql
begin;
select set_config('request.jwt.claims',
  '{"sub":"1111...","role":"authenticated","aal":"aal1","session_id":"s1"}', true);
set local role authenticated;
-- RLS policy: using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)
insert into public.watchlist(note) values ('mine');   -- user_id default auth.uid()
commit;
```
Results: user A saw 1 row and user B saw 0 rows. `anon` without a GRANT got `permission denied for table` (SQLSTATE 42501), which matches the new no-auto-grant default. After commit, `current_setting('request.jwt.claims', true)` = `''` and `auth.uid()` IS NULL. Python harness (psycopg) equivalent: run `SELECT set_config('request.jwt.claims', %s, true)` with `json.dumps(claims)`, then `SET LOCAL ROLE authenticated` inside one transaction. Note that `set local role` needs the connecting user to be a member of the role (superuser or `authenticator`). Supabase hosted/local runs **PG 17**. A PG16 harness is fine for this SQL subset.

## 8. Data API exposure change, Postgres versions, CLI local dev

**Breaking change** (changelog 2026-04-28, GitHub discussion #45329) [D]: new tables in `public` are **no longer automatically granted** to `anon`/`authenticated`/`service_role`, so they are not exposed to the Data API or GraphQL. Timeline: opt-in from 2026-04-28, **default for new projects from 2026-05-30**, **applied to all existing projects on 2026-10-30** (existing tables keep their grants; only objects created afterwards need explicit GRANTs). Missing grants produce PostgREST error `42501` with a hint naming the exact GRANT. `pg_graphql` is also off by default (postgres migration `20260421000000_pg_graphql-off-by-default`: `drop extension if exists pg_graphql`). **Migrations must contain explicit grants together with RLS**:
```sql
grant select on table public.deals to authenticated;            -- plus anon only if public
grant select, insert, update, delete on table public.watchlist to authenticated;
grant select, insert, update, delete on table public.deals to service_role;  -- service_role also needs grants
grant execute on function public.fn() to authenticated;
alter table public.deals enable row level security;
```
The CLI applies this SQL on `supabase start`/`db reset` when `[api].auto_expose_new_tables` is unset or false (`apps/cli-go/internal/db/start/start.go`, `RevokeDefaultDataApiPrivilegesSql`):
```sql
alter default privileges for role postgres in schema public revoke select, insert, update, delete on tables from anon, authenticated, service_role;
alter default privileges for role postgres in schema public revoke usage, select on sequences from anon, authenticated, service_role;
alter default privileges for role postgres in schema public revoke execute on functions from anon, authenticated, service_role;
```
`auto_expose_new_tables = true` keeps the legacy behavior; it is deprecated and the field is removed on 2026-10-30.

**Postgres versions** [D]: `supabase/postgres` `ansible/vars.yml` builds `postgres17: 17.11.0.004`, `postgres15: 15.19.0.004`, and `postgresorioledb-17: 17.11.0.004-orioledb`. There is **no PG 18 image**. The changelog of 2026-09-25 says "Postgres minor upgrade to 15.19/17.11 fixes 44 CVEs". CLI `config.toml` default `major_version = 17`. Self-hosted docker-compose moved from 15 to 17 in June 2026. UNVERIFIED: an explicit statement that new hosted projects default to 17 (strongly implied).

**Supabase CLI** 2.120.0 (npm, 2026-10-06; the repo is now a monorepo with `apps/cli` TS and `apps/cli-go`) [D]:
- Needs a Docker-compatible runtime: Docker, OrbStack (recommended on macOS), Rancher Desktop, Podman or colima. The first `supabase start` pulls the whole stack. Commands: `npx supabase init` / `start` / `status` / `stop` / `db reset` / `migration new <name>` / `gen types typescript --local`.
- Local URLs: API/Project `http://127.0.0.1:54321` (REST `/rest/v1`, Auth `/auth/v1`, GraphQL `/graphql/v1`, Functions `/functions/v1`, MCP `/mcp`). DB `postgresql://postgres:postgres@127.0.0.1:54322/postgres`. Studio on 54323, Mailpit (local_smtp) on 54324, analytics on 54327, shadow DB on 54320, pooler on 54329 (disabled).
- Images pinned at HEAD: `supabase/postgres:17.11.0.004`, `postgrest/postgrest:v16.4`, `supabase/gotrue:v2.197.0`, `supabase/realtime:v2.140.10`, `supabase/storage-api:v1.79.36`, `kong:2.8.1`.

`supabase/config.toml` essentials (template at CLI HEAD):
```toml
project_id = "lokal69"
[api]
enabled = true
port = 54321
schemas = ["public", "graphql_public"]       # add "api" etc. if you expose a dedicated schema
extra_search_path = ["public", "extensions"]
max_rows = 1000
# auto_expose_new_tables = true             # legacy, removed 2026-10-30
[db]
port = 54322
shadow_port = 54320
major_version = 17
[db.seed]
enabled = true                              # sql_paths = ["./seed.sql"]
[auth]
enabled = true
site_url = "http://127.0.0.1:3000"          # set to the Vite origin, e.g. http://127.0.0.1:5173
additional_redirect_urls = ["https://127.0.0.1:3000"]
jwt_expiry = 3600
# jwt_issuer = ""                           # default = external_url (= <api>/auth/v1)
# signing_keys_path = "./signing_keys.json" # `supabase gen signing-key --algorithm ES256`
enable_refresh_token_rotation = true
enable_signup = true
enable_anonymous_sign_ins = false
minimum_password_length = 6
[auth.email]
enable_signup = true
enable_confirmations = false
```
Custom schemas must be listed in `[api].schemas` (and in Dashboard → Data API on hosted projects), and they still need explicit grants (`docs/guides/api/using-custom-schemas`).

## 9. UNVERIFIED / caveats
- The default signing algorithm (RS256 vs ES256) for newly created hosted projects in Oct 2026. Backend code accepts both.
- Exact hosted default Postgres major for new projects (17 inferred from images, CLI default and changelog).
- The CLI facts were read from `supabase/cli` develop HEAD (2026-10-06), which matches the 2.120.0 publish date but was not diffed against the release tag. `supabase start` could not be run here (no Docker daemon).
- jsdom 30 on Node 22.22.0 is outside its declared engines range. It worked here, but pin 29.1.1 if CI uses `engine-strict`.

## Sources
supabase.com/docs/guides/{auth/signing-keys, auth/jwts, auth/jwt-fields, api/api-keys, api/securing-your-api, api/using-custom-schemas, local-development/cli/getting-started}.md;
supabase.com/changelog (2026-04-28 Data API exposure; 2026-09-25 PG 15.19/17.11); github.com/orgs/supabase/discussions/45329;
supabase.com/blog/jwt-signing-keys (2025-07-14 timeline); github.com/supabase/{cli,postgres,auth} (HEAD source);
github.com/PostgREST/postgrest docs/references/{transactions,auth}.rst; npm registry; PyPI PyJWT 2.15.1.
