# SUV deals review dashboard

A small, private review dashboard for the SUV deal-discovery system (spec section 23). It is a
static single-page app (Vite 8 + React 19.3 + React Router 8 + TypeScript 6) that:

- signs users in with **Supabase Auth** (`@supabase/supabase-js`, email + password or magic link,
  PKCE);
- calls **only** the backend-for-frontend routes of [`docs/api_contract.md`](../docs/api_contract.md)
  (`/api/*`), always with `Authorization: Bearer <Supabase access token>`; the browser never reads
  database tables and never computes tax, costs or contributions;
- shows the seven screens of spec 23 (overview, candidate queue, candidate detail, economics,
  review, sources, settings) plus a placeholder for the v1.1 seller-inquiry screens.

All typed API shapes live in one module, [`src/api/types.ts`](src/api/types.ts), written by hand
from the contract and the backend view models (`src/suv_deals/views/*`, `schemas/*.json`). The
only HTTP client is [`src/api/client.ts`](src/api/client.ts).

## Setup

Requirements: Node `>= 22.22.0` (react-router 8 needs it), npm 10. Exact dependency versions are
pinned in `package.json` and locked in `package-lock.json` (produced by npm).

```bash
cd dashboard
npm ci
cp .env.example .env.local     # then fill in the two public values
npm run dev                    # http://127.0.0.1:5173, /api proxied to DASHBOARD_API_TARGET
```

### Environment variables (browser = public)

| Variable | Where | Meaning |
|---|---|---|
| `VITE_SUPABASE_URL` | browser bundle | Supabase project URL (`https://<ref>.supabase.co`; plain `http` only for `127.0.0.1`/`localhost`). Used for Auth only. |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | browser bundle | The **publishable** key (`sb_publishable_...`). Sent to Supabase Auth only (supabase-js puts it in the `apikey` header), never to the dashboard backend. |
| `DASHBOARD_API_TARGET` | dev/preview server only (Node) | Where `vite` and `vite preview` proxy `/api` (default `http://127.0.0.1:8000`). Not exposed to the bundle. |

Nothing else may be `VITE_`-prefixed: `vite.config.ts` refuses to start or build when another
`VITE_` variable exists, or when the publishable slot holds a **secret** key (`sb_secret_...` or a
legacy `service_role` JWT). The app shows a "not configured" page when a value is missing. Never put
server secrets (database URL, Supabase secret key, Slack tokens, cursor secrets) anywhere in this
directory.

### Dev proxy

`npm run dev` serves the app on `127.0.0.1:5173` and proxies `/api` to `DASHBOARD_API_TARGET`
(start the backend with `make dev`, which listens on `127.0.0.1:8000`). The browser therefore talks to
the API same-origin, exactly as in production; no CORS configuration is needed in development.

## Scripts

| Command | What it does |
|---|---|
| `npm run build` | `tsc -b` (app, tests, configs, E2E specs), `vite build` into `dist/`, then `scripts/check-security.mjs --dist` (CSP meta present, no inline scripts/handlers/styles, no secret key in the bundle) |
| `npm test` | Vitest 5 + Testing Library (jsdom 29.1.1): API client, auth gating, review idempotency/conflicts, unknown-money rendering, XSS escaping, mobile nav, static security grep |
| `npm run lint` | oxlint (correctness + React/a11y rules, `react/no-danger`, `jsx-no-target-blank`, no `eval`) and the source security grep |
| `npm run e2e` | Playwright 1.56.1 (chromium) against real local services (see below) |
| `npm audit --omit=dev` | production dependency audit (also `make dashboard-audit`) |

From the repository root: `make dashboard-install dashboard-lint dashboard-build dashboard-test`,
`make e2e` (Python harness self-test + Playwright), `make e2e-clean` (drops leftover
`suv_e2e_*` databases after an aborted run).

## End-to-end tests

`npm run e2e` (or `make e2e`) starts three real services through Playwright's `webServer`
(see `playwright.config.ts`); everything binds to `127.0.0.1` and all data is SYNTHETIC:

1. **Mock Supabase Auth** on `:54399` - `tests/e2e/mock_supabase_auth.py` (FastAPI/uvicorn). It
   emulates the GoTrue endpoints supabase-js uses (`/token?grant_type=password|refresh_token|pkce`,
   `/user`, `/logout`, `/otp`, `/verify`) and publishes a locally generated ES256 key at
   `/auth/v1/.well-known/jwks.json`. Tokens carry `iss=<mock>/auth/v1`, `aud=authenticated`,
   `role=authenticated` and the test user's UUID as `sub`.
2. **Backend** on `:8765` - `tests/e2e/run_backend.py` creates a fresh migrated PostgreSQL database
   (`tests/db_harness.py`, local cluster on `127.0.0.1:5432`), seeds it (`tests/e2e/seed.py`) and
   serves `suv_deals.api.app.create_app` with `SUPABASE_URL` pointing at the mock (the backend's
   real JWKS client fetches the mock's key), `DATABASE_SET_ROLE=suv_backend`, JWT leeway 0 and
   relaxed rate limits. The database is dropped when Playwright stops the server.
3. **Dashboard** on `:4173` - a production build (`vite build --outDir dist-e2e` with
   `VITE_SUPABASE_URL=http://127.0.0.1:54399`, the strict CSP included) served by `vite preview`.

**Chosen API approach for E2E:** `vite preview` has the same `/api` proxy as the dev server
(`DASHBOARD_API_TARGET=http://127.0.0.1:8765`), so the browser calls `/api` on the dashboard's own
origin, which mirrors the recommended production deployment (static files and API behind one
origin). The backend still only allows `http://127.0.0.1:4173` as a CORS origin.

The seed manifest (ids, titles, users) is written to `e2e/.generated/seed-manifest.json` (ignored by
git). Users: `owner@e2e.invalid`, `reviewer@e2e.invalid`, `reviewer2@e2e.invalid`,
`viewer@e2e.invalid`, `expiring@e2e.invalid` (access tokens really live 6 s while the response
advertises an hour, so the backend rejects them mid-review), `multi@e2e.invalid` (two workspaces)
and `stranger@e2e.invalid` (no membership); the shared test password is in `tests/e2e/users.py`.

Covered: password and magic-link sign-in, cancelled/expired magic link, sign-out, deep-link
redirect, workspace selection, no membership; overview; candidate queue filters + detail +
back/forward; unknown costs shown as `unknown`; malicious seller text rendered inert (no dialogs,
no injected elements, no CSP violations); viewer without mutation controls; owner pause with a
reason; claim + `watch` decision with a second reviewer seeing `ALREADY_CLAIMED` and then
`VERSION_CONFLICT`; token expiry mid-review (401 -> refresh -> identical retry with the same
idempotency key); network loss after the write (not confirmed -> same-key retry -> exactly one
decision); page reload while a decision is in flight (server state reported, nothing resent);
phone viewport (375x812) navigation without horizontal scroll; skip link and visible focus.

Playwright is pinned to **1.56.1** because the sandbox ships chromium build 1194
(`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). Do not run `playwright install`.

## Deployment

The build output (`dist/`) is static. Two supported layouts:

- **Same origin (recommended).** Serve `dist/` and reverse-proxy `/api` to the backend on the same
  host (e.g. `https://deals.example/` and `https://deals.example/api/`). No CORS is involved; the
  backend's `APP_BASE_URL` is that origin. Configure the static server to fall back to
  `index.html` for client-side routes (`/candidates/...`, `/reviews/...`, `/auth/callback`).
  The backend accepts only the `Host` names derived from `APP_BASE_URL`/`MCP_PUBLIC_URL` (plus
  loopback), so the proxy must forward the public host name.
- **Separate origins.** Host `dist/` elsewhere and keep `/api` on the backend's origin by placing
  a proxy in front of the static host, or change `ApiClient`'s `baseUrl` and add the dashboard
  origin to the backend's `API_ALLOWED_ORIGINS` allow-list (no wildcards; the API sends no
  credentials and no cookies are used).

Also configure in Supabase Auth: the site URL / redirect allow-list must contain
`https://<dashboard>/auth/callback` (magic links), sign-ups disabled (`shouldCreateUser: false` is
sent too), and users created deliberately with a membership row.

Recommended response headers from the static host (the build already contains a CSP `<meta>`):
`Content-Security-Policy` with `frame-ancestors 'none'` (cannot be set by `<meta>`),
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security`, and
`Cache-Control: no-cache` for `index.html` (hashed assets may be cached long).

## Security notes

- **Tokens.** Sessions live only in supabase-js' own storage (`persistSession`); the dashboard
  reads the access token with `getSession()` when sending a request and never copies tokens into
  React state, web storage, URLs or logs. The `onAuthStateChange` callback is synchronous. A
  `401` triggers one `refreshSession()` and one identical retry; a second `401` signs out locally.
- **Claim tokens** (review claims) are kept in memory only and forgotten on reload or sign-out;
  after a reload the reviewer claims again (the server rotates the token). A reload during a
  submission leaves only a non-secret marker (case id, idempotency key, outcome, version) in
  `sessionStorage` so the page can report what the server recorded; nothing is resent.
- **Idempotency.** Each logical mutation gets one key (`<operation>:<uuid>`), reused verbatim for
  retries of the identical body; a second click while a request is pending is ignored; while an
  outcome is unknown the form is locked, so an unknown outcome can never become two writes. "Saved"
  is only shown after a server 2xx (or an idempotent retry's 2xx).
- **Untrusted text.** Seller text, provenance excerpts and all other API strings are rendered as
  React text (escaped). There is no `dangerouslySetInnerHTML`, `innerHTML`, `eval` or
  `new Function` anywhere (oxlint + `scripts/check-security.mjs` + a unit test enforce it). Links
  are rendered only for absolute `http(s)` URLs without credentials, always with
  `target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"`.
- **CSP.** Production builds carry `default-src 'self'; script-src 'self'; style-src 'self';
  connect-src 'self' <supabase origin>; object-src 'none'; base-uri 'none'; frame-src 'none'` (no
  inline scripts or styles). The E2E suite fails on any CSP violation.
- **Roles.** Mutation controls are shown only for the scopes returned by `/api/me` (viewer:
  read-only; reviewer: claim/decide/notes/recheck; owner: also source pause and the owner-only
  administration details in Settings). The backend enforces every scope regardless.
- **Workspace.** With several memberships the user picks a workspace; the choice (an id, not a
  credential) is remembered per user in `localStorage` and sent as `X-Workspace-Id`, which the
  backend validates against the user's memberships.
