# SUV deals review dashboard

A small, private review dashboard for the SUV deal-discovery system (spec section 23). It is a
static single-page app (Vite 8 + React 19.3 + React Router 8 + TypeScript 6) that:

- signs users in with **Supabase Auth** (`@supabase/supabase-js`, email + password or magic link,
  PKCE);
- calls **only** the backend-for-frontend routes of [`docs/api_contract.md`](../docs/api_contract.md)
  (`/api/*`), always with `Authorization: Bearer <Supabase access token>`; the browser never reads
  database tables and never computes tax, costs or contributions;
- shows the seven screens of spec 23 (overview, candidate queue, candidate detail, economics,
  review, sources, settings) and the spec v1.1 screens (section 37): seller inquiries (attention
  section, list, detail with the original message and the informational Macedonian preview),
  seller replies (original text, Macedonian summary, unaccepted quotes, escalations), inquiry
  control (pause/resume), mail-worker health and coverage gaps, coverage and lags (per source and
  per listing) and the 15-day evaluation.

**Standing authorization (spec 37.1):** there is no approve, send or reply control anywhere. The
pipeline sends one initial inquiry per verified vehicle/seller pair from validated records only
(mode `automatic`, kill switch off, authorization active, sender verified); the dashboard shows
what happened and lets the owner pause or resume.

What the v1.1 screens show beyond the raw states:

- **Why an inquiry waits** (`waiting_reason`, typed by the server): every list row, the inquiry
  detail and the attention section (one group per reason: rolling caps reached, seller cooldown,
  inquiries paused, desktop mail worker offline, sender setup incomplete, send held; uncertain
  delivery and missing facts keep their own groups). None of them is an approval wait.
- **Sending readiness** on the inquiry control: the standing authorization (`active` / `not
  recorded` / `not effective` / `revoked`) and the CONFIGURED sending identity's readiness with its
  problem codes (never an address). Since D1 it also shows the backend's PROCESS-level gate
  (`SELLER_INQUIRY_MODE`, the process kill switch, the owner's message-approval setting) and the
  server's `automatic_inquiries_possible`: automatic inquiries are described as possible only when
  the server says so, never because the database mode alone is `automatic` (nor while an owner
  cap of 0 holds every inquiry, or a rolling cap is used up: those are named too); the inquiry
  list's control summary says "nothing can be sent now" in that case. Since D2 the readiness also
  shows the configured sender's activation canary ("Activation canary": complete / not complete,
  `activation_canary_complete`): until it is complete the server reserves no real inquiry
  (`activation_canary_incomplete`), and the sending state names that reason.
- **Resume with suppression removal** sends `expected_removable_suppressions`, the count the owner
  was shown and ticked. When the server counts differently (`409 VERSION_CONFLICT`,
  `details.reason = suppressions_changed`, nothing changed) the controls are reloaded, the new count
  is shown and must be confirmed again before anything can be resumed, even when the reloaded count
  is the same number again (it need not be the same set); a count that moves on a plain reload also
  un-confirms the tick, and it stays un-confirmed if the count later moves back.
- **Transient conflicts** use `details.reason`: `busy` (this request was rolled back) and
  `in_progress` (a request with the same key is still running, so the outcome stays unknown and only
  a same-key retry is possible, also on a first send); `retryable` without a reason is the fallback.
- **Replies** show their dot signal (`emitted`, `coalesced`, `rate_limited` by the per-inquiry cap:
  stored and visible but no new dot activation, `not_applicable`).
- **Mail workers** show each listed worker's credential (`active` / `expiring` within 14 days /
  `expired` / `revoked`; an expired or revoked credential of an active worker is a coverage gap and
  that worker is never shown as monitoring, in the summary, its card or the activation evidence,
  however fresh its last heartbeat),
  the revoked workers (counted even when not listed), the reply-signal flood control (counts, the
  PROPOSED per-inquiry cap) and a read-only **activation evidence** table: sender readiness, runtime
  monitoring and the standing authorization from the API; the owner-controlled canary (rows 4-6 of
  docs/seller_email_activation.md) comes, for the owner only, from the read-only
  `GET /api/activation/canary-evidence` (the same state as `suv-deals canary status`; shown as met
  only when the server reports `complete`, unknown when it cannot be read) with the canaries' ids,
  states and times (never the target); other roles never request it and see it as "owner only".
  There is no canary, test or send control.
- **Candidates** have the dashboard-only audit filter "include screening-rejected (audit)"
  (`include_screening_rejected=true` in the URL and the query, kept for "load more"); such rows are
  labelled "screening rejected (audit)" and are not candidates.

All typed API shapes live in one module, [`src/api/types.ts`](src/api/types.ts), written by hand
from the contract and the backend view models (`src/suv_deals/views/*`, `schemas/*.json`,
`schemas/api/*.json` for the v1.1 routes of `api.schemas.V11_DASHBOARD_ROUTES`). The only HTTP
client is [`src/api/client.ts`](src/api/client.ts); it has one method per contract route and
deliberately none that could send, approve or answer anything.

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

Nothing else may be `VITE_`-prefixed: `vite.config.ts` refuses to start the dev server or preview,
and refuses every build mode, when another `VITE_` variable exists or when either variable holds a
**secret** key (`sb_secret_...` anywhere in the value, also padded with whitespace, quoted or in
another case, or a legacy `service_role` JWT; `src/configRules.ts` `isSecretKey`, shared with the
app). Vite inlines every `VITE_` value into the JavaScript it serves, so this check runs first. A **production build** (`npm run build`, mode `production`) also fails
when either required variable is missing or invalid (`src/configRules.ts`, shared with the app), so
a bundle that could only say "not configured" is never shipped. The dev server, the unit tests and
`vite build --mode development` keep the in-app "not configured" page. Never put server secrets
(database URL, Supabase secret key, Slack tokens, cursor secrets) anywhere in this directory.

### Dev proxy

`npm run dev` serves the app on `127.0.0.1:5173` and proxies `/api` to `DASHBOARD_API_TARGET`
(start the backend with `make dev`, which listens on `127.0.0.1:8000`). The browser therefore talks to
the API same-origin, exactly as in production; no CORS configuration is needed in development.

## Scripts

| Command | What it does |
|---|---|
| `npm run build` | `tsc -b` (app, tests, configs, E2E specs), `vite build` into `dist/` (fails without the two `VITE_` variables), then `scripts/check-security.mjs --dist` (CSP meta present, no inline scripts/handlers/styles, no secret key in the bundle). Screens are code-split: the entry chunk keeps React, the router and supabase-js (needed at once to restore the session), each screen is its own chunk |
| `npm test` | Vitest 5 + Testing Library (jsdom 29.1.1): API client (incl. every v1.1 route), auth gating, review idempotency/conflicts and `decided_by_caller`, unknown-money rendering, XSS escaping, mobile nav, static security grep, and the v1.1 screens (no approve/send control, informational preview label, withheld addresses and quarantined text, escalations, unaccepted quotes, unknown lags never zero, powered-off worker as a coverage gap, zero deals as zero, pause/resume incl. outcome-unknown retry and reload marker) |
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
   (`tests/db_harness.py`, local cluster on `127.0.0.1:5432`), seeds it (`tests/e2e/seed.py`, plus
   the v1.1 world of `tests/e2e/seed_v11.py`) and serves `suv_deals.api.app.create_app` with
   `SUPABASE_URL` pointing at the mock (the backend's real JWKS client fetches the mock's key),
   `DATABASE_SET_ROLE=suv_backend`, JWT leeway 0, a 60-second review claim lease
   (`REVIEW_CLAIM_DURATION_SECONDS`, `--claim-duration-seconds`; the real claim-expiry test waits
   for it) and relaxed rate limits. The database is dropped when Playwright stops the server.
3. **Dashboard** on `:4173` - a production build (`vite build --outDir dist-e2e` with
   `VITE_SUPABASE_URL=http://127.0.0.1:54399`, the strict CSP included) served by `vite preview`.

**Chosen API approach for E2E:** `vite preview` has the same `/api` proxy as the dev server
(`DASHBOARD_API_TARGET=http://127.0.0.1:8765`), so the browser calls `/api` on the dashboard's own
origin, which mirrors the recommended production deployment (static files and API behind one
origin). The backend still only allows `http://127.0.0.1:4173` as a CORS origin.

The seed manifest (ids, titles, users, the v1.1 inquiry/reply ids) is written to
`e2e/.generated/seed-manifest.json` (ignored by git).

The **v1.1 world** (`tests/e2e/seed_v11.py`, checked by `tests/e2e/test_seed_v11.py` on its own
database) is created through the real repositories and the real mail-worker API (in-process, the
path the desktop reply worker uses): inquiry controls in `automatic` mode, the standing
authorization, a verified `outlook_local` sender, and inquiries that are `replied` (a seller reply
with a final price and a deposit/reservation request, plus a quarantined possible match from
another address), `uncertain` (handed to the Outbox without proof), `held_facts` (no resolvable
language), `suppressed` (seller opt-out), `sending` with an unclaimed desktop intent while the PC is
off (`WORKER_OFFLINE`), the same dealer's second vehicle in its seller cooldown and another
vehicle while the 24-hour cap is used up (both waiting on their REAL plan jobs, released with the
plan handler's own wait codes), and a `kill_switch` suppression a resume can remove. There is a
retired (revoked) mail worker and the active one, whose credential expires within 14 days; the
active worker's heartbeat is moved 6 hours back so the owner's PC looks powered off. The E2E
backend's configured sending identity is that synthetic sender (its mode stays
`disabled_until_sender_ready`). The resume-conflict test records one concurrent `kill_switch`
suppression through the real repository with `tests/e2e/v11_actions.py` (loopback `suv_e2e_*`
databases only). Every address is
`...@example.invalid`; the E2E backend runs no worker, so nothing is ever sent. Users: `owner@e2e.invalid`, `reviewer@e2e.invalid`, `reviewer2@e2e.invalid`,
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
a double-clicked submit (exactly one request); a second tab of the same reviewer re-claiming (the
first tab's stale handle gets `CLAIM_EXPIRED` with a reload path, nothing saved); phone viewport
(375x812) navigation with no horizontal scroll and no clipped content (`overflow-x: hidden` is not
used to hide overflow); skip link and visible focus; the real backend refusing forged (foreign key
with the mock's `kid`), tampered, unsigned and misplaced tokens; no tokens in URLs or in web storage
outside supabase-js' own key; a REAL server-side claim expiry (60-second lease, browser clock
skewed behind the server) refused with `CLAIM_EXPIRED`, draft kept, re-claim and save.

v1.1 (`e2e/inquiries.spec.ts`): attention groups (uncertain, held, suppressed) and the caps wait,
filters in the URL, inquiry detail with the informational Macedonian preview, the recipient address
withheld from a reviewer and shown to the owner, send attempts and timeline, reply escalations and
an unaccepted quote, a quarantined reply as metadata only (its text for the owner only, its derived
availability withheld in the list, and its deposit request attributed to an unverified sender with
a do-not-pay warning for the owner), the owner's Slack link `/inquiries/<id>/replies/<id>` opening the
reply after sign-in, no
approve/send control on any of these pages, a viewer without the area, a powered-off PC shown as an
open coverage gap (never "monitoring") whose last report is never shown as current, unknown lags
as unknown (never 0 s), detection delay unknown
without a trusted source time, zero suitable deals as 0, owner pause with a lost response resolved
by a same-key retry then resume, a reviewer seeing the controls read-only, and every v1.1 screen at
375 px without horizontal scrolling. Work package C3 adds: the typed waiting reasons in the list,
detail and attention groups (worker offline, seller cooldown, caps; inquiries paused after a pause),
the authorization and sender readiness, the dot-signal state of replies, worker credentials
(expiring, revoked) and the reply-signal cap, the read-only activation evidence with the canary
never assumed (D1: the owner's canary rows from the API, "owner only" for a reviewer), the
process-level gate on the inquiry control (the E2E backend keeps `SELLER_INQUIRY_MODE` at its
default, so "nothing can be sent now"), the candidates audit filter, and a resume whose confirmed suppression count is
refused by the real server after a concurrent change, shown and confirmed again before a second
attempt.

`tests/e2e/test_mock_auth.py` (run by `make e2e` first) checks that the backend's real
`SupabaseJwtVerifier`, fed with the mock's JWKS, accepts the mock's tokens and refuses everything
Supabase would not issue (foreign issuer or audience, `anon`/`service_role` roles, anonymous users,
expired / future / not-yet-valid tokens, non-UUID or missing subjects, foreign signatures, HS256
algorithm confusion, `alg: none`, edited payloads), and that the JWKS publishes only the public key.

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
  submission leaves only a non-secret marker (case id, user id, idempotency key, outcome, version)
  in `sessionStorage` so the page can report what the server recorded; nothing is resent. The
  marker is shown only to the user who sent it.
- **Idempotency.** Each logical mutation gets one key (`<operation>:<uuid>`), reused verbatim for
  retries of the identical body; a second click while a request is pending is ignored; while an
  outcome is unknown the form is locked, so an unknown outcome can never become two writes. A
  retry that is refused before the server evaluates it (`401`, `403`, `429`) leaves the attempt
  unconfirmed (it says nothing about the first send), so it can only be retried with the same key.
  "Saved" is only shown after a server 2xx (or an idempotent retry's 2xx).
- **Untrusted text.** Seller text, provenance excerpts and all other API strings are rendered as
  React text (escaped). There is no `dangerouslySetInnerHTML`, `innerHTML`, `eval` or
  `new Function` anywhere (oxlint + `scripts/check-security.mjs` + a unit test enforce it). Links
  are rendered only for absolute `http(s)` URLs without credentials, always with
  `target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"`.
- **CSP.** Production builds carry `default-src 'self'; script-src 'self'; style-src 'self';
  connect-src 'self' <supabase origin>; object-src 'none'; base-uri 'none'; frame-src 'none'` (no
  inline scripts or styles). The E2E suite fails on any CSP violation.
- **Roles.** Mutation controls are shown only for the scopes returned by `/api/me` (viewer:
  read-only, no seller-inquiry area; reviewer: claim/decide/notes/recheck and the inquiry screens
  read-only; owner: also source pause, inquiry pause/resume and the owner-only administration
  details in Settings). The backend enforces every scope regardless.
- **Seller inquiries (spec 37).** No approve, send or reply control exists. The Macedonian preview
  is labelled as an informational audit translation, never an approval draft. Seller addresses
  and the text of a quarantined reply appear only when the API returns them (owner only); seller
  text is untrusted and rendered as text. A quoted price is always shown as an unaccepted seller
  quote; requests that need an owner decision (payment, reservation, identity documents, ...) are
  highlighted. Pause/resume use the same idempotent mutation as reviews (one key per attempt,
  same-key retry when the outcome is unknown, and a non-secret reload marker that reports the
  server state without resending). The owner can also run the resume while the kill switch is
  already off when the control view still counts removable kill-switch / revoked-authorization
  suppressions ("Re-qualify suppressed inquiries"); after a reload such a request is reported as
  undeterminable rather than as applied, because it leaves the control version unchanged. Unknown
  lags and counts are shown as `unknown`, never 0; a worker without a fresh heartbeat is a
  coverage gap, never healthy, and the dimensions it reports about itself (mailbox sync, sync lag,
  backlog, matching gaps) are shown as "unknown now" with the last report for context only.
  Filter forms follow Back/Forward, and the replies of an inquiry are paged, never truncated.
  A quarantined reply's derived availability is withheld from everyone but the owner in the reply
  lists (the owner sees it labelled as an unverified match). The payment / reservation / identity
  requests of a reply whose sender is not verified as the seller (quarantined, or not the verified
  recipient) are attributed to "the sender" with a do-not-pay warning, never to "the seller". The
  owner's Slack links (`/inquiries/<inquiry_id>/replies/<reply_id>`, the backend's
  `dashboard_reply_url`) open the reply, which is loaded by its own id only.
- **Unknown outcomes stay unknown.** A retry that the server turns away as `retryable` (a
  `409 VERSION_CONFLICT` with `retryable: true`: a lock timeout, serialization failure or deadlock,
  or the same request still in progress) proves only that the retry was rolled back, so the attempt
  stays "not confirmed" and locked to the same key; a busy refusal of a FIRST send is reported as
  "busy", not as "changed".
- **Workspace.** With several memberships the user picks a workspace; the choice (an id, not a
  credential) is remembered per user in `localStorage` and sent as `X-Workspace-Id`, which the
  backend validates against the user's memberships.
