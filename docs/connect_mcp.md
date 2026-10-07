# Connecting the MCP server to dot

Spec §28. This page separates what the code and tests verify from what only a real deployment and
the owner's actual ChatGPT/dot account can verify. Nothing here has been connected to a real
client yet: **no deployed URL exists, no real client was tested, no subscription was created.**
Replace the placeholders below with the actual values when that happens and record the evidence
listed in step 8.

## 1. What the server provides (verified in tests)

| Item | Value | Evidence |
|---|---|---|
| Endpoint | `https://<approved-domain>/mcp` (Streamable HTTP; served by `suv-deals api serve` behind the HTTPS reverse proxy) | tests/mcp (in-process client) |
| SDK / protocol | official `mcp` 2.3.0, protocol revision 2026-07-28 (older handshake revisions still served, stateless) | docs/research/mcp_python_sdk.md, tests/mcp |
| Tools (12) and scopes | `deals_health`, `deals_list_candidates`, `deals_get_candidate`, `deals_get_comparables`, `deals_get_valuation` (`deals:read`); `reviews_list_pending` (`reviews:read`); `reviews_claim`, `reviews_release`, `reviews_submit` (`reviews:write`); `deals_request_recheck` (`rechecks:request`); `deals_add_note` (`notes:write`); `sources_pause` (`sources:pause`) | `schemas/tools/*.json`, tests/contracts |
| Discovery | `tools/list` shows only the tools the credential's scopes allow | tests/mcp |
| Unauthenticated access | every private call is `401` with a `WWW-Authenticate: Bearer` challenge; a missing identity provider or database is `503`, never an open endpoint | tests/mcp |
| Events (optional) | `events/list`, `events/subscribe`, `events/unsubscribe` for `review.pending.v1` only when `MCP_EVENTS_ENABLED=true` (and the encryption key is set) | tests/mcp, docs/notification_bridge.md |

The server never exposes SQL, shell, arbitrary HTTP fetching, purchasing, seller contact,
tax-rule approval or account administration (`config:admin` and `mail:ingest` are never
effective on MCP).

## 2. Authentication modes (one per deployment, `MCP_AUTH_MODE`)

| Mode | Use | Notes |
|---|---|---|
| `oauth` (preferred production) | OAuth access tokens (JWT, ES256/RS256) from the configured authorization server | Needs `MCP_PUBLIC_URL` (ending in `/mcp`), `MCP_OAUTH_ISSUER`, `MCP_OAUTH_JWKS_URL` (optional `MCP_OAUTH_AUDIENCE`). The server publishes RFC 9728 protected-resource metadata at `/.well-known/oauth-protected-resource/mcp` and validates issuer, audience/resource, signature, expiry and scopes. A user `sub` maps to an active workspace membership; machine clients need an explicit principal mapping. **Not verified:** which authorization server the owner uses and whether the actual dot client completes this flow (Supabase Auth alone is not assumed to provide MCP OAuth discovery/registration). |
| `static_bearer` | a scoped private credential for a private compatible client | Not OAuth compliance and not assumed to be supported by the actual dot connection. Created with the CLI (section 3); only the SHA-256 hash is stored. |
| `dev_local` | local tests only | Allowed only with `APP_ENV=development/test`, a loopback URL and loopback clients. |

`suv-deals doctor --process api` checks the configured URLs offline (scheme, `/mcp` path, no
credentials in URLs, https outside loopback development) without printing them.

## 3. Creating a scoped credential (static bearer)

Owner operation, on a trusted host with `DATABASE_URL` in the environment:

```bash
uv run suv-deals credentials create-mcp --workspace <workspace-id> \
    --label "dot read-only" --scopes deals:read,reviews:read --role viewer --expires 30d --yes
```

- The token (`suvmcp_...`) is printed **once**; store it directly in the client's secret store.
  Never paste it into a chat, a ticket or a file in the repository.
- Grant read scopes first (`deals:read,reviews:read`). Add `reviews:write` (role `reviewer`) only
  after the owner approves persistent write access (step 6). `events:subscribe` is a separate
  scope and does not imply review writes.
- `uv run suv-deals credentials list --workspace <id>` shows metadata only;
  `uv run suv-deals credentials revoke <credential-id> --reason "..." --yes` revokes immediately
  (the next request is `401`).

## 4. Connection steps (spec §28)

| # | Step | How | Status |
|---|---|---|---|
| 1 | Local contract tests with an MCP client | `uv run pytest tests/mcp tests/contracts -q` (in-process official SDK client); optionally the official MCP Inspector against `http://127.0.0.1:8000/mcp` with a `dev_local` credential | verified in tests (fixture data) |
| 2 | Deploy only after the owner authorizes destination and cost; HTTPS and correct auth | docs/runbook.md sections 3 and 8 | **blocked**: no approved host/domain |
| 3 | Unauthenticated private calls fail; authorized reads succeed | `curl -i https://<domain>/mcp` must be `401`; then a read with the credential | not run (no deployment) |
| 4 | Add the private custom MCP connection in the product | OpenAI documentation checked 2026-10-06 describes the web flow: open ChatGPT Plugins, select the plus button, choose *Add custom MCP server*, enter the server URL, configure authentication, review the risk warning, create and install the plugin. Availability depends on the account/workspace. No other menu or hidden setting is assumed. | not run |
| 5 | Read scopes first; verify `deals_health`, `reviews_list_pending` and one specific candidate in the intended dot conversation | ask dot to call those tools | not run |
| 6 | Review writes only after approval; test claim/submit on a clearly labelled canary case | a SYNTHETIC canary review case in a non-production workspace | not run |
| 7 | Source and dashboard links open for the owner | open the links returned by `deals_get_candidate` | not run |
| 8 | Record evidence | client version/surface, negotiated protocol, SDK version (2.3.0), tool-list hash (`sha256` of the `tools/list` result), exact successful test IDs/request ids | not run |
| 9 | Native MCP Events first, Slack fallback only if needed | section 5 | **blocked** |

## 5. Native MCP Events subscription (preferred activation route)

Tools alone never wake dot: a new turn needs a subscription the client creates itself.

1. Enable on the deployment (only after the gates in docs/notification_bridge.md section 7):
   `MCP_EVENTS_ENABLED=true`, `EVENT_BRIDGE_ENABLED=true`, `EVENT_BRIDGE_PROVIDER=mcp_events`,
   `ALLOW_EXTERNAL_NOTIFICATIONS=true`, `MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY` set, and an
   approved, verified destination binding. `suv-deals doctor --process api,dispatcher` must show
   the route without conflicts.
2. In the plugin page, **rescan the MCP server** so the `review.pending.v1` event appears next to
   the tools (documented behaviour: events are discovered like tools; rescan after changes).
3. In the intended dot conversation, ask dot to monitor the primary review queue. The client
   calls `events/subscribe` with its own callback URL and `whsec_` signing secret; the server
   runs the callback verification challenge and stores the subscription (secret encrypted).
4. Send a labelled SYNTHETIC canary and capture the evidence list in docs/notification_bridge.md
   section 8 (subscription id, challenge, delivery receipt, dot's tool calls, duplicate check).
5. Test stopping: `events/unsubscribe` returns `{}` and a later event produces no callback.
6. Only then set `EVENT_BRIDGE_VERIFIED_AT` (bridge status `verified`).

Unverified (re-check before activation): whether the owner's dot surface supports MCP Events for
this plugin, ChatGPT's challenge/delivery timeouts, `ttlMs` usage, callback egress ranges
(docs/research/mcp_events_and_webhooks.md section 14). Tool availability is not proof of an
active subscription. If events are unavailable, evaluate the separately approved Slack fallback
(docs/notification_bridge.md section 5); otherwise the dashboard/MCP pull queue remains complete.

## 6. Spec v1.1 seller replies (Slack signal route; later package)

For seller replies the owner chose a different route (spec §37.6): local classic Outlook worker
-> authenticated backend API -> minimal private Slack signal -> dot -> authenticated MCP reply
tool. Native MCP Events stay the route for candidate discovery; routing is per event category so
one reply never activates dot twice. The Slack post is an ID-only signal: dot reads the full,
correlated reply only through the authenticated MCP reply tools, which belong to the later v1.1
wiring package (not yet mounted on this server). Nothing in this page enables seller email.
