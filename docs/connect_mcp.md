# Connecting the MCP server to dot

Spec §28. This page separates what the code and tests verify from what only a real deployment and
the owner's actual ChatGPT/dot account can verify. Nothing here has been connected to a real
client yet: **no deployed URL exists, no real client was tested, no subscription was created.**
Replace the placeholders below with the actual values when that happens and record the evidence
listed in step 8. The first route is decided (owner, 2026-10-18): dot reads through OpenAI's
Secure MCP Tunnel from a local API on the owner's WSL machine, read-only (section 8,
`scripts/local_mcp.sh`); its live test with the owner's ChatGPT account is still to be done.

## 1. What the server provides (verified in tests)

| Item | Value | Evidence |
|---|---|---|
| Endpoint | `https://<approved-domain>/mcp` (Streamable HTTP; served by `suv-deals api serve` behind the HTTPS reverse proxy) | tests/mcp (in-process client) |
| SDK / protocol | official `mcp` 2.3.0, protocol revision 2026-07-28 (older handshake revisions still served, stateless) | docs/research/mcp_python_sdk.md, tests/mcp |
| Tools (12 + 3 + 1) and scopes | `deals_health`, `deals_list_candidates`, `deals_get_candidate`, `deals_get_comparables`, `deals_get_valuation` (`deals:read`); `reviews_list_pending` (`reviews:read`); `reviews_claim`, `reviews_release`, `reviews_submit` (`reviews:write`); `deals_request_recheck` (`rechecks:request`); `deals_add_note` (`notes:write`); `sources_pause` (`sources:pause`); spec 37.8: `seller_inquiries_get`, `seller_replies_get` (`inquiries:read`), `seller_inquiries_pause` (`inquiries:pause`); import-cost calculator: `deals_estimate_import_costs` (`deals:read`, read-only; section 7) | `schemas/tools/*.json`, tests/contracts, tests/mcp/test_v11_tools.py, tests/mcp/test_calc_tools.py |
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
| `static_bearer` | a scoped private credential for a private compatible client | Not OAuth compliance; ChatGPT itself cannot send it (section 3.1). Created with the CLI (section 3); only the SHA-256 hash is stored. On the Secure MCP Tunnel route (phase 1, owner decision 2026-10-18, section 8) OpenAI's `tunnel-client` adds it as a static header read from a file (section 3.1). |
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

### 3.1 The token in an authorization file (`--authorization-file`)

ChatGPT's custom MCP servers offer only "OAuth", "No authentication" or a mix of both; ChatGPT itself
cannot send a custom API key or bearer header (research of 2026-10-18, official OpenAI
documentation; the owner confirmed on 2026-10-18 that the account offers "Add custom MCP server"
with Connection: Tunnel). The first route, approved by the owner on 2026-10-18 (section 8), is
OpenAI's Secure MCP Tunnel: OpenAI's `tunnel-client` runs in the owner's WSL, makes outbound HTTPS
connections only and forwards MCP requests to a separate local API at `http://127.0.0.1:8010/mcp`,
and it adds a static header to every forwarded request from a file (`mcp.extra_headers:
{Authorization: "file:/path"}`). `scripts/local_mcp.sh setup` runs the command for that route
(label `dot read-only (tunnel)`, file `~/.config/suv-deals/tunnel/mcp-authorization`). For a
client like that the token can go into a file instead of the terminal:

```bash
install -d -m 700 ~/.config/suv-deals
uv run suv-deals credentials create-mcp --workspace <workspace-id> \
    --label "dot via tunnel-client (read-only)" --scopes deals:read,reviews:read --role viewer \
    --expires 30d --authorization-file ~/.config/suv-deals/dot.authorization --yes
```

- The file holds exactly `Bearer <token>` (no trailing newline). It is always a NEW file (created
  with `O_EXCL`, never overwritten, never through a symlink), mode 0600 whatever the umask, and the
  token is **not printed**; the output shows the credential metadata and the file path only.
- A file is acceptable only when **all** of these hold: the client cannot hold a secret-store entry
  itself and reads a static header from a file (such as `tunnel-client`'s `mcp.extra_headers`); the
  file is 0600 and outside the repository, on the Linux filesystem of the machine that runs the
  client (not under `/mnt/c`); and the owner approved this route. Otherwise keep the printed token in
  the client's secret store as above.
- Refused with a usage error (exit 2) before anything is created: a path that exists or is a
  symlink; a path inside the repository or inside a git working tree that contains the current
  directory (symlinks resolved); a parent directory that is missing, not owned by the current user,
  or readable or writable by group or others (create it with `install -d -m 700`).
- The credential is inserted and the file written in one transaction: if writing fails, nothing is
  committed and no credential exists. If the commit outcome is unknown after the write (for example
  a dropped connection), the command revokes the credential and removes the file; if even that
  fails, it keeps the file (the token may be live) and prints the `credentials revoke` command.
  An interrupt (Ctrl-C) after the write does no further database work: it keeps the file, names
  the credential that may be live and prints the `credentials revoke` command (check the file with
  `mcp probe --authorization-file`; an interrupt before the write leaves no file).
- Treat the file like the token: do not copy, sync or commit it. To rotate, create a new credential
  into a new file, switch the client to it, then revoke the old credential and delete its file.
- Check the result with `suv-deals mcp probe --authorization-file <file>` (section 4.1).

## 4. Connection steps (spec §28)

| # | Step | How | Status |
|---|---|---|---|
| 1 | Local contract tests with an MCP client | `uv run pytest tests/mcp tests/contracts -q` (in-process official SDK client); optionally the official MCP Inspector against `http://127.0.0.1:8000/mcp` with a `dev_local` credential | verified in tests (fixture data) |
| 2 | Deploy only after the owner authorizes destination and cost; HTTPS and correct auth | docs/runbook.md sections 3 and 8 | **blocked**: no approved host/domain. Phase 1 needs no deployment: a local API behind the Secure MCP Tunnel (section 8) |
| 3 | Unauthenticated private calls fail; authorized reads succeed | `curl -i https://<domain>/mcp` must be `401`; then a read with the credential. `suv-deals mcp probe --url <url> --authorization-file <file>` runs both (section 4.1) | phase 1: `scripts/local_mcp.sh start` runs the probe against the local API before the tunnel starts (section 8.6: passed in the build container on 2026-10-18); not run on the owner's machine yet |
| 4 | Add the private custom MCP connection in the product | OpenAI documentation checked 2026-10-06 describes the web flow: open ChatGPT Plugins, select the plus button, choose *Add custom MCP server*, enter the server URL, configure authentication, review the risk warning, create and install the plugin. Availability depends on the account/workspace. No other menu or hidden setting is assumed. Phase 1: Connection: Tunnel, the tunnel `lokal69-mcp`, Authentication: No authentication (section 8.3, step 4) | not run |
| 5 | Read scopes first; verify `deals_health`, `reviews_list_pending`, one specific candidate and `deals_estimate_import_costs` for it in the intended dot conversation | ask dot to call those tools; check that dot relays the preview banner (section 7) | not run |
| 6 | Review writes only after approval; test claim/submit on a clearly labelled canary case | a SYNTHETIC canary review case in a non-production workspace | not run |
| 7 | Source and dashboard links open for the owner | open the links returned by `deals_get_candidate` | not run |
| 8 | Record evidence | client version/surface, negotiated protocol, SDK version (2.3.0), tool-list hash (`sha256` of the `tools/list` result), exact successful test IDs/request ids; `suv-deals mcp probe` prints the server side of it (section 4.1) | not run |
| 9 | Native MCP Events first, Slack fallback only if needed | section 5 | **blocked** |

### 4.1 `suv-deals mcp probe`: the server-side evidence for steps 3 and 8

```bash
uv run suv-deals mcp probe --url http://127.0.0.1:8000/mcp \
    --authorization-file ~/.config/suv-deals/dot.authorization          # add --json for a record
```

It reads no settings and no database and checks, in order: (a) an unauthenticated `initialize`
POST answers `401` with a `WWW-Authenticate: Bearer` challenge; (b) with the file, the official
`mcp` SDK client (Streamable HTTP, `initialize` handshake) runs `initialize` and `tools/list` and
calls `deals_health`. Without `--authorization-file` only (a) runs. It prints:

| Evidence | What it is |
|---|---|
| challenge | the parameters of the `401` challenge (`error`, `error_description`, `scope`; `resource_metadata` in `oauth` mode) |
| protocol version | the version negotiated by the `initialize` handshake (`2025-11-25` with SDK 2.3.0) |
| server | name, version and title from `initialize` (`suv-deals <version>`) |
| tools | the tool names the credential discovers (7 for `deals:read,reviews:read`) |
| tools/list SHA-256 | over `{"tools": [...]}` with the tools sorted by name, `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, UTF-8; equal to the hash of the server's raw `tools/list` result for the same credential |
| `deals_health` | status fields only: `ready` and each readiness status, `source_network_enabled`, `bridge_status`, `notification_route`, sources by state, the number of activation blockers, warning codes |
| request ids | every `X-Request-Id` the server returned, plus the tool envelope's `request_id` |

Exit `0` when every check passed, `1` when one failed (the line names the check and the reason,
e.g. `HTTP 401: the server refused the credential ...` for `initialize`), `2` for usage errors.
Reasons it names for a failed check:

- `HTTP 3xx redirect to <Location> (not followed; use that URL as --url)`: the URL is not the
  endpoint itself (a trailing slash, or a proxy upgrading http to https); only a `2xx` answer
  without a token is reported as `the endpoint is open`.
- `HTTP 400 ... Host allow-list` (with the server's plain-text reason): `suv-deals api serve`
  refused the URL's host; it accepts the hosts of `MCP_PUBLIC_URL` and `APP_BASE_URL`,
  `127.0.0.1`, `localhost` and `API_ALLOWED_HOSTS`. `HTTP 421` for `initialize`: the host passed
  that list but does not match `MCP_PUBLIC_URL` (for example `localhost` when it names
  `127.0.0.1`).
- `no answer within N s`: one request took longer than `--timeout` (default 10 s);
  `the checks did not finish within M s`: the authenticated checks together took longer than
  4 x `--timeout`.
- `cannot connect (...): is the server running at this URL?` (nothing listens), `TLS failed (...)`
  (certificate, or https against a plain-http server) or `the host name cannot be resolved (...)`.

The token and the `Authorization` header are never printed or logged. The file is refused when it
is a symlink, accessible to group or others or not exactly `Bearer suvmcp_...` / `Bearer <JWT>`; it
is sent only over https or to a loopback address. The unauthenticated check follows no redirect;
the SDK follows one only within the endpoint's origin (a `307`/`308` to the same scheme, host and
port, or from http to https on the same host), so the header never leaves that origin, and it
refuses any other redirect. The probe shows what this server offers this credential; what dot
itself negotiates still has to be recorded from the client.

## 5. Native MCP Events subscription (preferred activation route)

Tools alone never wake dot: a new turn needs a subscription the client creates itself.

1. Enable on the deployment (only after the gates in docs/notification_bridge.md section 7):
   `MCP_EVENTS_ENABLED=true`, `EVENT_BRIDGE_ENABLED=true`, `EVENT_BRIDGE_PROVIDER=mcp_events`,
   `ALLOW_EXTERNAL_NOTIFICATIONS=true`, `MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY` set, and an
   approved, verified destination binding. With `compose.production.yaml` the switches are set
   only through `SUV_DEALS_ENABLE_MCP_EVENTS=true`, `SUV_DEALS_ENABLE_EVENT_BRIDGE=true` and
   `SUV_DEALS_ENABLE_EXTERNAL_NOTIFICATIONS=true` exported for that deployment (env files cannot
   enable them; docs/runbook.md section 1); `EVENT_BRIDGE_PROVIDER` and the key live in the api and
   dispatcher env files. `suv-deals doctor --process api,dispatcher` must show the route without
   conflicts.
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

## 6. Spec v1.1 seller inquiries and replies (Slack signal -> dot -> MCP)

### 6.1 The three inquiry tools (served; verified in tests)

| Tool | Scope | Input (exactly spec 37.8) | Result `data` |
|---|---|---|---|
| `seller_inquiries_get` | `inquiries:read` | `inquiry_id` (uuid) | `InquiryView`: state, qualification, authorization/template versions, sender as provider/binding reference, recipient domain and verification evidence, send-attempt summary, `reply_count` / `latest_reply_id`, `waiting_reason` (why it waits: `UNCERTAIN_DELIVERY`, `INQUIRIES_PAUSED`, `SENDER_SETUP_INCOMPLETE`, `WORKER_OFFLINE`, `RATE_CAP_REACHED`, ...; `null` when not waiting), `approval_required: false` |
| `seller_replies_get` | `inquiries:read` | `reply_id` (uuid) | `ReplyView`: inquiry/vehicle ids, original language and sanitised body, Macedonian summary, sender identity (domain and correlation evidence), received/ingested times, extracted claims (a price is an `unaccepted_seller_quote`), safe attachment metadata, current valuation status, `signal_status` (`emitted` / `coalesced` / `rate_limited`). A quarantined (unverified) reply returns metadata only (`content_withheld: true`) |
| `seller_inquiries_pause` | `inquiries:pause` | `expected_version` (>= 1), `reason` (3-2000), `idempotency_key` (8-128) | `InquiryPauseResult` (kill switch on; `already_paused` when it already was) |

- A retryable `VERSION_CONFLICT` tool error carries `details.reason`: `busy` (a lock timeout or a
  lost race; retry) or `in_progress` (a `seller_inquiries_pause` with the same `idempotency_key` is
  still running; retry later with the same key, never a new one).
- The workspace always comes from the token; ids of another workspace are `NOT_FOUND`, exactly like
  unknown ids. `tools/list` shows a tool only to a caller holding its scope: reviewers get the two
  read tools, viewers none, and `inquiries:pause` is an owner scope (grant it to dot only on the
  owner's explicit decision; e.g. `credentials create-mcp --role owner --scopes
  deals:read,reviews:read,inquiries:read,inquiries:pause`).
- A seller's e-mail address is never returned on MCP: `recipient_address_visible` requires
  `config:admin`, which is never effective on MCP. Replies carry no secret, no Outlook locator, no
  unrelated thread message and no signed external link; seller text is untrusted data. A
  quarantined reply (forwarded, changed address, ambiguous: possibly unrelated personal mail) is
  returned without subject, body, summary, claims or attachment metadata (`content_withheld:
  true`) until the owner verified it on the dashboard.
- There is **no** send, reply, follow-up, resume or approve tool. Pausing is idempotent (same key
  and request replay the same result on MCP and on the dashboard, which share the operation),
  versioned (`VERSION_CONFLICT` with `current_version`) and never resumes anything; resuming is an
  owner action on the dashboard (`POST /api/inquiry-control/resume`) or the CLI.

### 6.2 The reply route (spec 37.6)

```text
classic Outlook (owner's PC) -> desktop worker (local correlation only)
  -> POST /v1/mail-workers/replies (mailbox-bound suvmail_ credential)
  -> app.seller_replies + ops.mail_ingest_dedup + outbox seller.reply.received (one transaction)
  -> dispatcher -> private Slack channel (ID-only signal: event id, inquiry id, reply id,
     listing reference, dashboard link, fixed status words)
  -> dot's Slack trigger -> seller_replies_get / seller_inquiries_get over MCP
```

- Native MCP Events stay the route for candidate discovery (`review.pending.v1`); seller replies
  and seller-reply owner alerts go to the Slack route selected for their category only, so one
  reply never activates dot twice (`workers.dispatcher._handle_signal`). Both post to the single
  `SLACK_CHANNEL_ID`: dot's Slack trigger must match only the message metadata `event_type`
  `suv_deals.seller_reply_received` (owner alerts carry `suv_deals.owner_alert`), see
  docs/notification_bridge.md section 5.1.
- **dot trigger filter (required):** dot's Slack trigger fires ONLY for messages in
  `SLACK_CHANNEL_ID` whose message metadata `event_type` is exactly
  `suv_deals.seller_reply_received`. It must ignore `suv_deals.owner_alert` (for the owner),
  `suv_deals.review_pending` (candidate discovery, which dot reaches through native MCP Events or
  the review queue, never through this trigger) and any message without our metadata (text that
  merely looks like a signal). Matching on text, on the channel alone or on every bot message would
  start dot twice for one reply or let any channel member start it.
- The Slack post carries no body, address or attachment; dot must call `seller_replies_get` to read
  the reply. A Slack 2xx is a provider receipt, never proof that dot processed the signal.
- One signal can stand for several replies: a reply that arrives while the inquiry's previous
  signal is still to be posted is `coalesced` into it, and after `MAX_SIGNALS_PER_INQUIRY_24H`
  (PROPOSED 6) signals per inquiry and rolling 24 hours a reply is stored without its own signal
  (`rate_limited`). On every signal dot therefore also reads `seller_inquiries_get(inquiry_id)`
  (`reply_count`, `latest_reply_id`, `waiting_reason`) and reads the latest reply too; the
  dashboard lists every reply. The cap and its effect are visible in
  `GET /api/mail-workers/health` (`reply_signals`) and on each reply (`signal_status`).
- Delivery needs `ALLOW_EXTERNAL_NOTIFICATIONS=true`, `SELLER_REPLY_SIGNAL_PROVIDER=slack` and an
  approved, enabled and verified Slack destination binding (docs/notification_bridge.md). The
  dispatcher then needs `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID` and
  `SLACK_DESTINATION_APPROVAL_REF` even when native MCP Events carry candidate discovery
  (`suv-deals doctor --process dispatcher` reports them as required in that topology;
  `SLACK_TEAM_ID` / `SLACK_BOT_USER_ID` are recommended). The route is gated by
  `seller_reply_slack_route` (`ACTIVATION_GATES.md`).

### 6.3 What is verified and what is not

| Item | Status |
|---|---|
| Tool schemas, scope filtering, workspace isolation, address hiding, idempotent/versioned pause | verified in tests (tests/mcp/test_v11_tools.py, synthetic data) |
| Desktop worker -> API -> stored reply -> outbox signal | verified in tests with the REAL desktop worker code and a fake Outlook (tests/integration/mail_worker_e2e) |
| Classic Outlook on the owner's PC, the real mailbox, the Windows credential store | **not verified** (no machine connected) |
| Private Slack channel, posting identity, dot's subscription/trigger and bot-message handling | **not verified** (no channel or dot trigger configured) |
| dot actually calling `seller_replies_get` after a signal | **not verified**; record the end-to-end evidence of docs/seller_email_activation.md section "Activation evidence" |

Nothing in this page enables sending: seller e-mail is sent only by the pipeline under the standing
authorization once docs/seller_email_activation.md is complete.

## 7. Import-cost calculator for dot (`deals_estimate_import_costs`)

The owner asked for a customs calculator that dot can use: for one vehicle it computes the North
Macedonian **customs duty**, **motor-vehicle tax (ДМВ)** and **import VAT**, and the **landed cost**
with the owner's fixed **EUR 700** transport per vehicle (the `transport` line of
`config/cost_profiles/default_unapproved.yaml`). The same read backs the dashboard and the CLI.

| Item | Value |
|---|---|
| Scope | `deals:read` (the read credential of section 3 already includes it; no new scope) |
| Behaviour | read-only, idempotent, not open-world; nothing is stored: no valuation, cost line, alert, rate or audit row |
| Rate limit | the expensive per-principal bucket (30 calls/minute), like paginated reads |
| Rules | an ACTIVE, owner-approved rule set (mode `active`, exactly what valuations use); without one, an **indicative preview** on the best rule set that is not ACTIVE (approved but not yet activated, then under review, then draft; stored or `config/tax_rules/drafts/`), mode `indicative_preview`; without any, mode `unavailable` |
| Contract | `schemas/tools/deals_estimate_import_costs.json`, docs/api_contract.md section 7 |

No MCP tool approves or activates tax rules; that stays an owner act with a review record
(docs/tax_rule_approval.md). Today no MK rule set is approved, so every result is an indicative
preview.

### 7.1 Example call

Give a `listing_id` (from `deals_list_candidates`), `vehicle` facts, or both. Every `vehicle` field
you set replaces the listing's fact; fields you omit keep the listing's or stay unknown. Decimals
are strings (`"3000.00"`, never `3000`).

```json
{
  "listing_id": "<listing uuid>",
  "vehicle": {
    "price": {"amount": "3000.00", "currency": "EUR"},
    "price_basis": "invoice",
    "declaration_date": "2026-10-13",
    "fuel": "diesel",
    "co2": [{"cycle": "nedc", "g_km": "189"}],
    "co2_document": "coc",
    "origin_proof": {"kind": "none"}
  },
  "customs_eur_mkd_rate": "61.5337"
}
```

- `co2`: the figures exactly as printed, one per cycle; WLTP is used when present, otherwise NEDC.
  Nothing is converted between cycles.
- `co2_document`: where the figure is printed (`coc`, `registration_consent`,
  `manufacturer_certificate`, `technical_assessment_certificate`), `listing_claim_unverified` for an
  advertised figure, `none_available` only when no document exists (the rules then apply their
  deemed charge, which is much higher). Omitted: the motor-vehicle tax stays unknown.
- `origin_proof`: the proof the importer holds (`none`: standard duty; `unknown`: the duty stays
  unknown; `eu`, `efta`, `tr`, `uk`, `cefta`, `ua`, `us`: a held proof, assumed accepted, with the
  standard-duty alternative as a second scenario). The seller's country is never taken as origin.
- `hs_code`: only from a broker, a ruling or the owner (HS 8703); a plug-in hybrid needs it.
- `transport_after_entry_eur` with `transport_after_entry_separately_invoiced: true` only when the
  inland leg is invoiced separately; otherwise all EUR 700 is in the customs value.
- `price` is the REAL purchase price (what is actually paid): every landed cost, and so the profit,
  uses it. `estimated_customs_value_eur` with `estimated_customs_value_source` (both or neither;
  the vehicle only, without transport, e.g. `"4200"` and `"SchwackeNet catalogue"`) is the value the
  `primary` scenario's charges are computed on, plus the transport to the MK place of entry (whether
  Customs adds that transport to a catalogue value is UNCERTAIN). With it, the `transaction_value`
  scenario shows the charges if Customs accepts the price paid (Царински закон чл. 28, its primary
  method; another method applies only on founded doubt).
- With a `listing_id`, the owner's RECORDED values of that listing are used automatically: the
  recorded real purchase price instead of the advertised price (basis `recorded_actual`,
  `recorded_quote` or `recorded_estimate`), and the recorded estimated customs value. The owner
  records them on the dashboard (`POST /api/listings/{listing_id}/import-values`) or with
  `suv-deals import-costs record-values` (docs/runbook.md); **dot cannot record them** (no tool
  writes them). `fact_sources` then says `recorded`, `purchase_price.source` /
  `customs_value_for_duties.source` name the recorded value, `listing.recorded_purchase_price_id` /
  `listing.recorded_customs_value_id` carry the row ids and `notes` say `RECORDED_*_USED`. A `price`
  or estimate dot passes replaces the recorded one for that estimate only (notes
  `RECORDED_*_OVERRIDDEN`); nothing is stored, so the next call uses the recorded values again.
- `customs_eur_mkd_rate`: the Customs monthly list rate of the declaration month, used only when
  none is recorded (`suv-deals fx record --purpose customs ...`); labelled and never stored.

Two values, never mixed (owner decision 2026-10-13): Customs officially uses the SchwackeNet
catalogue to assess used vehicles, but the Customs Law makes the price actually paid (transaction
value, чл. 28) the primary method, with the other methods only in the legal order and on founded
doubt. So a car bought for EUR 2,800 that the catalogue values at EUR 4,200 keeps EUR 2,800 as its
real purchase price (profit, landed cost) and EUR 4,200 as its estimated customs value (import
charges); the result shows both scenarios side by side. Dot should say which value the duties rest
on and that the price paid stays the basis of the profit.

### 7.2 Reading the result

| Field (`data.`) | Meaning | What dot says |
|---|---|---|
| `mode` | `active`, `indicative_preview` or `unavailable` | in a preview: "indicative", and relay the banner, which names the rule status (draft, under review, or approved but not yet active); never call an approved set unapproved; unavailable: "import charges unknown" |
| `banner.mk` / `banner.en` | the preview label | relay it with the figures, unchanged |
| `production_ready` | `false` in every preview | never present a preview as a customs assessment or a final total |
| `purchase_price` | the REAL purchase price in whole euros, its `basis` (`invoice`, `advertised`, `recorded_actual`, `recorded_quote`, `recorded_estimate`) and `source` | the price the profit rests on |
| `customs_value_for_duties` | the customs value the `primary` charges use, in whole euros incl. the transport to the MK place of entry; `basis` `estimated` (with its `source`) or `transaction_value` | "duties computed on ... (estimated from ...)" when estimated |
| `fact_sources`, `listing.recorded_*_id`, `notes` | where each fact came from (`request`, `recorded`, `listing`, `default`); the owner's recorded value rows used; `RECORDED_*_USED` / `RECORDED_*_OVERRIDDEN` | "using the owner's recorded purchase price / customs value" when `recorded` |
| `facts` | the facts as entered (e.g. `"3000.00"`); the computed amounts above are whole units | not a result; never quote it as a charge |
| `scenarios[0]` (`primary`) | `indicative_total` (MKD, a preview) or `total` (active rules), `indicative_total_eur` / `total_eur`, `indicative_landed_cost_eur` / `landed_cost_eur`, `customs_value_kind`, `customs_value` (its own currency) with `customs_value_eur` (whole euros), `categories` (import duty, motor-vehicle tax, import VAT) and `components` (helpers flagged `intermediate` are never added); every amount in whole units | the total, the three charges and the landed cost, with the mode |
| `scenarios[0].complete: false` | some charge is unknown: the total is `{"status": "unknown", "amount": null}`, `known_subtotal` shows what resolved | "incomplete"; never call the subtotal a total, never treat an unknown charge as 0 |
| other `scenarios` | `standard_duty_if_proof_rejected` (with a claimed proof of origin), `transaction_value` (only with an estimated customs value: Customs accepts the price paid) | the alternative next to the primary result; the landed cost of every scenario uses the real purchase price |
| `missing_facts`, `missing_inputs` | what to supply (e.g. `co2_document`, `co2`, `origin_proof`, `customs_fx_rate`, `hs_code`, `fuel`; `price_in_eur_or_mkd` when a CHF price cannot be converted) | ask the owner for those documents or figures |
| `customs_fx_rate`, `customs_fx_source` | the customs rate actually used (`recorded`, `caller_supplied`, `not_needed`, `none`, or `recorded_not_effective`: the newest recorded rate is of an earlier month and was NOT used) | with `recorded_not_effective` or `none`, ask the owner to record this month's Customs rate |
| `assumptions`, `warnings`, `unverified_inputs`, `uncertain_items` | the labelled assumptions (fixed transport, no proof means standard duty), claims that are not evidence (an advertised price, a listing CO2 figure) and rule statements still marked UNCERTAIN; warnings such as `HS_CODE_FUEL_MISMATCH` (the attested tariff code and the fuel disagree), `CO2_ZERO_FOR_COMBUSTION_VEHICLE` (under 1 g/km is not a figure for a combustion car), `FIRST_REGISTRATION_IMPLAUSIBLE` and `RULE_SET_DOES_NOT_COVER_VEHICLE` (no fact completes the charge under these rules) | mention the ones the figures rest on; for a mismatch ask which fact is right |
| `gates.importable` | the Euro-class import gate (`yes`, `no`, `unknown`); it never changes an amount | warn when `no` or `unknown` |
| envelope `warnings` | `TAX_RULES_UNAPPROVED` (preview), `UNKNOWN_COSTS`, `SELLER_CLAIMS_UNVERIFIED`, `FIXTURE_DATA` | as above |

SYNTHETIC example (the call above without `listing_id`, today's unapproved draft): mode
`indicative_preview`; `indicative_total` `"168155"` MKD in whole denars (import duty 11,384 +
motor-vehicle tax 96,390 + import VAT 60,381; the VAT base includes the ДМВ), `indicative_total_eur`
`"2733"` EUR at the customs rate (2,732.73 rounded half-up) and `indicative_landed_cost_eur` `"6433"`
EUR (3,000 + 700 + 2,732.73, summed exactly and rounded once). Every amount is in whole units
without decimals (owner decision 2026-10-13). The same facts with an EU proof of origin give
154,722 MKD, with the 168,155 MKD standard-duty alternative next to it. Bought for EUR 2,800 with
`estimated_customs_value_eur` `"4200"`: `primary` 185,803 MKD on EUR 4,900 (landed cost EUR 6,520 =
2,800 + 700 + 3,019.53) and `transaction_value` 165,213 MKD on EUR 3,500 (landed cost EUR 6,185).
The same figures come back for `{"listing_id": "<id>", "vehicle": {"co2_document": "coc"}}` once the
owner has recorded EUR 2,800 (actual) and EUR 4,200 (SchwackeNet catalogue) for that listing, with
`purchase_price.basis` `recorded_actual` and both facts sourced `recorded`.

### 7.3 Errors

- `VALIDATION_ERROR` names the failing fields, never their values: `listing_id` and `vehicle` when
  neither is given, nested paths such as `vehicle.price.amount` or `vehicle.co2.0.g_km`, and both
  `vehicle.transport_after_entry_eur` and `vehicle.transport_after_entry_separately_invoiced` when
  only one is set, and both `vehicle.estimated_customs_value_eur` and
  `vehicle.estimated_customs_value_source` when only one is set.
- `NOT_FOUND` for an unknown listing, a listing of another workspace (the same answer) or one
  without a current, non-quarantined revision; `FORBIDDEN` without `deals:read`; `RATE_LIMITED`
  with `retry_after_seconds`.

### 7.4 What is verified and what is not

| Item | Status |
|---|---|
| Tool schema, scope filtering, validation, isolation, rate bucket, preview labelling, golden totals of the draft, nothing persisted | verified in tests (tests/contracts/test_calc_tool_schemas.py, tests/mcp/test_calc_tools.py, synthetic data) |
| The two values (real purchase price vs estimated customs value), whole-unit amounts, the owner's recorded listing values used automatically, a dot-supplied value replacing one for a single call, no tool that records them | verified in tests (same files; the owner's EUR 2,800 / EUR 4,200 example, synthetic listing) |
| The MK draft rule set's legal content | **not approved**: every `UNCERTAIN - confirm` marker of the draft must be resolved and the owner must approve and activate it first (docs/tax_rule_approval.md) |
| dot calling the tool and relaying the banner in the real conversation | **not verified** (no deployment, section 4 step 5) |

## 8. Connecting dot through the OpenAI Secure MCP Tunnel (phase 1)

### 8.1 The owner's decision

On **2026-10-18** the owner approved phase 1, verbatim: **"ChatGPT-план е dot: Pro, да има и постои
Tunnel, одобрувам"**. Meaning: the ChatGPT plan behind dot is Pro, the account offers "Add custom
MCP server" with Connection: Tunnel, and the owner approves this route. Phase 1 is read-only: one
viewer credential with `deals:read` and `reviews:read`, valid 30 days, used by dot only while a
window on the owner's WSL machine runs `scripts/local_mcp.sh start`. Write scopes
(`reviews:write`, `inquiries:*`) and Events need a new owner decision.

### 8.2 Data flow

```text
dot (ChatGPT, OpenAI)
  -> OpenAI tunnel service (/v1/mcp/{tunnel_id}; only the owner may use the tunnel, S1)
  <- tunnel-client on the owner's WSL machine, OUTBOUND HTTPS only to api.openai.com:443
     (long-poll with the restricted runtime API key, S2; no inbound port, no public URL)
  -> http://127.0.0.1:8010/mcp, with "Authorization: Bearer suvmcp_..." added by tunnel-client
     from ~/.config/suv-deals/tunnel/mcp-authorization (ChatGPT itself sends no Authorization)
  -> suv-deals api serve (127.0.0.1 only; MCP_AUTH_MODE=static_bearer) -> PostgreSQL (.env)
```

- The API of this route is a separate process (default port 8010, `LOCAL_MCP_PORT`); the dashboard
  API on port 8000 may run next to it. It gets `MCP_AUTH_MODE=static_bearer`,
  `MCP_PUBLIC_URL=http://127.0.0.1:<port>/mcp`, `MCP_EVENTS_ENABLED=false` and
  `SOURCE_NETWORK_ENABLED=false` as process environment (it never fetches from a source), runs
  without proxy headers (`--no-proxy-headers`: only tunnel-client connects, from 127.0.0.1) and
  reads `DATABASE_URL` and every other setting from `.env` itself; the script never reads, prints
  or passes `.env`. `APP_BASE_URL` is not set by the script: `doctor --process api` and the Host
  check accept the `.env` value or the default (loopback hosts are accepted on any port), so the
  links tools return keep the owner's dashboard address.
- tunnel-client forwards MCP requests to the configured URL (`/mcp`); the dashboard routes
  (`/api/...`) are not its target. Its health and admin endpoints (`/healthz`, `/readyz`,
  `/metrics` and the embedded UI with the live logs, the runtime log-level switch and the log
  export) are served on a Unix socket in the 0700 tunnel directory (`--health.unix-socket`), which
  only the owner's user can reach. Whether the socket **replaces** the default TCP listener
  (`127.0.0.1:8080`) is **UNVERIFIED** for the binary the owner installs: the flag help in the
  vendor's public source (github.com/openai/tunnel-client, the `run` flags at commit `3e52fa04` of
  2026-10-17) says the socket is served "instead of binding TCP" and that `--health.listen-addr` is
  "Ignored when health.unix-socket is set", but the vendor's configuration.md (embedded-stub
  section) adds "Existing TCP defaults remain available". `start` therefore also passes the
  documented `--health.listen-addr 127.0.0.1:0`: if a build keeps a TCP listener, it gets an
  ephemeral loopback port instead of 8080 and cannot collide with another program there. Such a
  listener would still answer every local process (and possibly Windows programs through WSL's
  localhost forwarding), so the live test records which listeners exist (8.3 step 5: `ss -ltnp |
  grep tunnel-client` must print nothing).
- While the tunnel runs, the start window stops it as soon as the API process is gone (a crash, an
  out-of-memory kill): tunnel-client would otherwise add the credential to requests for whatever
  program binds `127.0.0.1:8010` next. Before its probe sends the credential, `start` checks that
  every socket listening on the port belongs to the API it launched (a process of the API's own
  session, read from `/proc`), not to another program that won the race for the port.
- **What leaves the machine:** every tool result dot reads (candidates, prices, valuations,
  comparables, import-cost estimates, the review queue, `deals_health` status) goes to OpenAI as
  part of dot's conversation. No seller e-mail address (never returned on MCP) and no secret.

### 8.3 The owner's steps

1. **Platform** (signed in as the owner):
   1. Tunnels management, <https://platform.openai.com/settings/organization/tunnels>: create a
      tunnel named `lokal69-mcp` and note its id (`tunnel_` + 32 lowercase letters or digits, or
      `tunnel_` + a 4-character namespace + `_` + 32).
   2. **S1:** restrict who may *use* the tunnel to the owner only (roles and groups:
      <https://platform.openai.com/settings/organization/people/roles>,
      <https://platform.openai.com/settings/organization/people/groups>; Tunnels **Read + Use**
      for the owner only, **Manage** only for whoever administers the tunnel).
   3. **S2:** create a **restricted runtime API key**
      (<https://platform.openai.com/settings/organization/api-keys>) with only the Tunnels
      permission **Read + Use** (for this tunnel only, if the page offers that choice) and set a
      usage limit for its project. Never an admin key: `setup` refuses a key starting with
      `sk-admin-`.
   4. Download `tunnel-client` for **Linux x86-64** from the Tunnels page. The vendor's readme
      says its download button points at the supported public release on
      <https://github.com/openai/tunnel-client/releases> (file
      `tunnel-client-vX.Y.Z-linux-amd64.zip`; this repository pins no version). This binary reads
      both secrets and every tool result, so **check the ZIP before unpacking it**, in WSL, with
      `vX.Y.Z` the version in its name:

      ```bash
      cd /mnt/c/Users/<you>/Downloads
      curl -fLO https://github.com/openai/tunnel-client/releases/download/vX.Y.Z/SHA256SUMS.txt
      sha256sum --check --ignore-missing SHA256SUMS.txt  # must print: tunnel-client-vX.Y.Z-linux-amd64.zip: OK
      ```

      No line or `FAILED` means another or a damaged file: do not install it (if the name differs,
      compare `sha256sum <file>.zip` with the line in `SHA256SUMS.txt` by hand). The vendor's full
      provenance check (`gh attestation verify ... --repo openai/tunnel-client` with the release's
      Sigstore bundle) is in its readme, section "Validate a downloaded release". Then unpack it
      and install the full `tunnel-client` binary (not `tunnel-client-runtime`, which has no
      `doctor`): `mkdir -p ~/.local/bin && cp <unpacked>/tunnel-client ~/.local/bin/ && chmod +x
      ~/.local/bin/tunnel-client`, then `tunnel-client --version` (open a new WSL window if
      `~/.local/bin` was just created, or set `TUNNEL_CLIENT_BIN`). `setup` prints and records the
      version and the binary's SHA-256 (`tunnel-client.version`).
2. **`scripts/local_mcp.sh setup`** in `~/Lokal69` (once; running it again changes nothing that
   exists). It checks `tunnel-client` and records its version, creates
   `~/.config/suv-deals/tunnel` (0700; refused inside the repository, inside a git working tree
   around the repository or around the tunnel directory itself, such as a dotfiles repository at
   `~/.config` or `~` (a separate git directory, `--git-dir ... --work-tree ~`, leaves no `.git` to
   detect: never add this directory to one), under `/mnt`, as a symlink, owned by someone else or
   open to group/others), asks for the tunnel id
   and the runtime API key without echo (the key is checked and written by the shell builtin
   `printf` into a new 0600 file: it never appears on a command line, in another process's
   environment or in any output), creates the viewer credential with
   `uv run suv-deals credentials create-mcp --label "dot read-only (tunnel)" --role viewer --scopes
   deals:read,reviews:read --expires 30d --authorization-file ~/.config/suv-deals/tunnel/mcp-authorization --yes`
   (the token is never shown; the credential id, workspace and expiry go to `credential.json`),
   writes `tunnel-client.yaml` and runs `tunnel-client doctor --config <yaml> --explain`:

   | File in `~/.config/suv-deals/tunnel/` | Content |
   |---|---|
   | `control-plane-api-key` (0600) | the runtime API key, exactly as entered (secret) |
   | `tunnel-id` (0600) | the tunnel id (not secret) |
   | `mcp-authorization` (0600) | exactly `Bearer suvmcp_...` (secret) |
   | `credential.json` (0600) | credential id, workspace id, label, role, scopes, `expires_at` (from the `create-mcp` output), no secret |
   | `tunnel-client.yaml` (0600) | see below; rewritten when `LOCAL_MCP_PORT` changes |
   | `tunnel-client.version`, `health.sock` | the binary's `--version` output and SHA-256; the health and admin socket while the tunnel runs |

   ```yaml
   config_version: 1
   control_plane:
     tunnel_id: "tunnel_..."
     api_key: "file:/home/<you>/.config/suv-deals/tunnel/control-plane-api-key"
   mcp:
     server_urls:
       - channel: main
         url: "http://127.0.0.1:8010/mcp"
     extra_headers:
       Authorization: "file:/home/<you>/.config/suv-deals/tunnel/mcp-authorization"
   log:
     level: info
   ```

   If `doctor` reports only the MCP server as unreachable, that is expected before `start` (the
   API is not running yet; UNVERIFIED whether `doctor` checks it at all).
3. **`scripts/local_mcp.sh start`** and keep that window open. It refuses while a previous run is
   still running, when port 8010 is taken (choose another with `LOCAL_MCP_PORT=<port>`), when setup
   is incomplete and when the credential has expired (it warns 5 days before). Then: `suv-deals
   doctor --process api` with the route's environment; the API in the background
   (`var/local_mcp/logs/api-<UTC>.log`); `/readyz` (up to 60 s, `LOCAL_MCP_READY_TIMEOUT`);
   `suv-deals mcp probe --url http://127.0.0.1:8010/mcp --authorization-file <file> --json`
   (`401` without the header; `initialize`, `tools/list` and `deals_health` with it; it prints
   the protocol, the tool count and the tools/list SHA-256 and keeps the report as
   `var/local_mcp/logs/probe-<UTC>.json`), sent only after the check that the socket on the port is
   the API's own (8.2); finally `tunnel-client run --config <yaml> --health.unix-socket
   ~/.config/suv-deals/tunnel/health.sock --health.listen-addr 127.0.0.1:0` in the foreground of
   the window (the vendor's advice for a manual daemon; its output also goes to
   `var/local_mcp/logs/tunnel-<UTC>.log`). The window says "Keep this window open while dot uses
   the system; Ctrl+C stops the tunnel and the API" and, once the tunnel's `/readyz` answers 200,
   "tunnel ready". When the tunnel ends for any reason (Ctrl+C, `stop`, a crash, the window
   closed), the API is stopped too; when the API ends first, the tunnel is stopped within a second
   and `start` prints the API log's last lines. Exit status: 0 after Ctrl+C or `stop`, 1 for any
   other end, 130 (Ctrl+C), 143 (SIGTERM) or 129 (window closed) when it is interrupted before the
   tunnel runs; the API it launched is stopped in every case.
4. **ChatGPT**, only while the window says "tunnel ready": open ChatGPT Plugins (the vendor's
   connector settings page is <https://chatgpt.com/#settings/Connectors>), select the plus
   button, *Add custom MCP server*, **Connection: Tunnel**, select the tunnel `lokal69-mcp`,
   **Authentication: No authentication**, review the risk warning, create and install. dot should
   discover **7 tools** (the viewer scopes; counted by the probe and the end-to-end run of 8.6):
   `deals_health`, `deals_list_candidates`, `deals_get_candidate`, `deals_get_comparables`,
   `deals_get_valuation`, `deals_estimate_import_costs` (`deals:read`) and `reviews_list_pending`
   (`reviews:read`). Any other number means another credential or build: stop and compare with
   the probe report.
5. **Test with dot** (in the intended conversation), one prompt each:
   - "Call deals_health and tell me whether the system is ready."
   - "List the pending reviews (reviews_list_pending)."
   - "Get candidate <listing id from the list> (deals_get_candidate)."
   - "Estimate the import costs for that candidate (deals_estimate_import_costs)", and check that
     dot relays the preview banner (section 7).

   Record in the evidence log of ACTIVATION_GATES.md (gate 13), without any secret: the date; the
   release you verified (the ZIP's line in `SHA256SUMS.txt`) and `tunnel-client.version` (version
   and the binary's SHA-256); the negotiated protocol and the tools/list SHA-256 from the probe
   report; what dot shows (the tool list, or a screenshot without the token); the request ids of
   dot's calls from `var/local_mcp/logs/api-<UTC>.log`: one `mcp tool call` line per tool call,
   with `tool`, `status` and `request_id` (the same id is in the tool result's envelope and in the
   response's `X-Request-Id` header; `initialize` and `tools/list` leave no log line, and the
   `request served` lines of that log are only `start`'s and `status`'s own `/readyz` checks); and,
   while the tunnel runs, the output of `ss -ltnp | grep tunnel-client` (expected: nothing; a line
   means a TCP health/admin listener, 8.2).

### 8.4 Every day: status, logs, stop, rotate, turning it off

```bash
scripts/local_mcp.sh status   # another window: API /readyz, tunnel /readyz, days left, last log lines
scripts/local_mcp.sh logs     # follow the API and tunnel logs (Ctrl+C stops following only)
scripts/local_mcp.sh stop     # SIGTERM to the tunnel, then the API (or Ctrl+C in the start window)
scripts/local_mcp.sh rotate   # new credential file (atomic rename), revokes the old one; then stop + start
```

- The credential expires after 30 days (S7): `start` and `status` warn 5 days before; `start`
  refuses an expired one. `rotate` creates the new credential into a new file, renames it over
  `mcp-authorization` only after it succeeded, updates `credential.json` and revokes the old
  credential (`credentials revoke <old id> --workspace <id> --reason ... --yes`); if the
  revocation fails it says so loudly, keeps the new file and prints the command to run. A running
  tunnel keeps sending the old (now revoked) header until it is restarted.
- No credential is left behind that no file records. When `mcp-authorization` is gone (deleted,
  or a symlink removed as the script asks) but `credential.json` still names a credential, `setup`
  revokes that credential before it creates a new one (a leaked copy stops working); if revoking
  fails it creates nothing and prints the command. `setup` and `rotate` write the new
  `credential.json` before anything is replaced: if that fails, the new credential is revoked again
  and nothing changes. If only the final rename of `credential.json` fails, `rotate` still revokes
  the old credential and prints the `mv` command that finishes the job.
- `stop` and `status` read whole command lines (an exported `COLUMNS` does not cut them). A
  `stop` or `start` with another `LOCAL_MCP_TUNNEL_DIR` than the running tunnel's changes nothing
  and names the running tunnel: use the same directory.
- **Turning it off**, from the quickest to the most complete: Ctrl+C in the start window or
  `scripts/local_mcp.sh stop` (nothing listens any more); revoke the credential, `uv run suv-deals
  credentials revoke <credential id from status> --reason "..." --yes` (the next request is
  `401`); disable or delete the tunnel on the Platform, or remove the plugin in ChatGPT; revoke the
  runtime API key on the Platform and delete `control-plane-api-key`.
- The scripts write only `var/local_mcp/` (git-ignored) and the tunnel directory; they are
  separate from `scripts/local_crawl.sh` (its own state, lock and processes).

### 8.5 Security rules of this route

- **S1** only the owner may use the tunnel; **S2** the runtime key is restricted to Tunnels Read +
  Use for this tunnel and has a usage limit (8.3 step 1).
- **S3** never `--log.http-raw-unsafe` (or `LOG_HTTP_RAW_UNSAFE`): it logs request and response
  bodies and headers, including the injected `Authorization`. The script never passes it, the
  YAML has no such setting, and tunnel-client runs with a cleaned environment (only `PATH`,
  `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `TZ`, locale, terminal, proxy and `SSL_CERT_*`
  variables), so no `CONTROL_PLANE_*`,
  `OPENAI_*`, `MCP_*`, `LOG_*` or `TUNNEL_CLIENT_*` variable of the shell can change its key, its
  target, its headers or its logging. The admin UI's runtime log-level switch (`GET`/`PUT
  /api/log-level`: `debug`, `info`, `warn`) is served on the health socket, which only the owner's
  user reaches (8.2); leave it at `info` (what `debug` logs is UNVERIFIED).
- **S4** every tunneled request reaches the API from 127.0.0.1, so the failed-authentication
  limiter in front of the token check (20 failures, then one more per 3 s, per client address) is
  per process here, not per client: a wrong or revoked token makes the next calls `429` for a
  while. `start`'s probe uses one unit of that budget for its unauthenticated check.
- **S7** 30 days; warning 5 days before; refused when expired (8.4).
- **Never connect the owner's PC as dot's "computer"** (remote computer use): that would expose
  `.env` with `DATABASE_URL` and the other secrets, the Outlook mailbox and everything else on the
  PC. dot gets this read-only MCP tunnel only.
- The data dot reads goes to OpenAI (8.2).

### 8.6 What is verified and what is not

| Item | Status |
|---|---|
| `scripts/local_mcp.sh`: help and exit codes (0 after Ctrl+C with the tunnel running, 130 after Ctrl+C before it), the tunnel directory and file modes, refusals (inside the repository, inside a git working tree around the tunnel directory such as `~/.config` or `~`, symlink, group/other access, foreign owner, `/mnt`), tunnel-id and key validation, both prompts without echo on a real terminal (pty), idempotent setup, the recorded credential revoked before `setup` creates another, the YAML, the step order of `start` with the exact environment, the credential sent only to the API's own socket, the foreground tunnel with `--health.unix-socket` and `--health.listen-addr 127.0.0.1:0` and never raw logging, the API stopped when the tunnel exits and the tunnel stopped when the API dies, a reused API pid never signalled, the refusals of `start`, the expiry warning, `stop` order, `status` and `stop` with a narrow `COLUMNS` or another tunnel directory, `rotate` (revoke with the old id; the new file replaces the old one only after success; a `credential.json` that cannot be written takes the new credential back); the API key and the token never in the output, in the argv of any command the script runs (every external command is wrapped and records its argv; a static test forbids an external `printf` and commands by absolute path), in the stubs' environment or in the YAML | verified in tests (tests/cli/test_local_mcp_script.py, stub executables) |
| The chain local API + real credential + `mcp probe` + YAML-driven header injection | verified on 2026-10-18 in the build container with a **fake** `tunnel-client` (below) |
| The real `tunnel-client` with this YAML (`doctor`, `run`, `file:` references, `--health.unix-socket`) | **UNVERIFIED** (the vendor binary is not available here; flags and keys from its documentation) |
| The injected header survives with ChatGPT's "No authentication" (connector-forwarded headers are applied last and override static ones case-insensitively; with No authentication ChatGPT sends no `Authorization`) | **UNVERIFIED** until the owner's live test |
| dot discovering the 7 tools and calling them | **not run** |

End-to-end run of 2026-10-18 (build container; scratch database, SYNTHETIC workspace; the real
script, `uv`, `suv-deals api serve` and credential; a fake `tunnel-client` that parsed the
generated YAML, read `mcp.extra_headers.Authorization` from its `file:` reference and sent MCP
requests to `mcp.server_urls[main]`, as the vendor's client would after the control plane):
`setup` exit 0 (second run: everything kept, YAML unchanged); `doctor --process api` 0 errors
without `APP_BASE_URL`; `/readyz` 200; the probe passed all 4 checks (unauthenticated `initialize`
401 with `WWW-Authenticate: Bearer error="invalid_token"`; protocol `2025-11-25`; server
`suv-deals 0.1.0`; **7 tools**; tools/list SHA-256
`bcb1df66242b775812f9623bcde1dc3774e65fad313e661741118fb2fe9d266a`; `deals_health` ready=true,
`source_network_enabled=false`); through the fake tunnel: `initialize` 401 without the header and
200 with it, `notifications/initialized` 202, `tools/list` 200 with the same 7 tools and the
same SHA-256, `tools/call deals_health` 200; the tunnel's `/readyz` on the socket 200 after 1 s;
the API stopped when the tunnel exited; `rotate` created a new credential and revoked the old one
(`revoked_at` set); the environment tunnel-client saw held only `HOME`, `PATH`, `TERM`,
`LC_CTYPE`, `SSL_CERT_FILE` and the proxy variables (no `OPENAI_API_KEY`, no `DATABASE_URL`); the
API key, the token and a planted shell secret were in no output, log, YAML or `credential.json`;
modes 0700/0600; no process left; the scratch database dropped. The SHA-256 belongs to this
build's tool schemas: record the one the owner's own probe prints. That run predates the review
fixes of the same day (the listening-socket check before the probe, the tunnel stopped when the API
dies, `--health.listen-addr 127.0.0.1:0`, the binary's SHA-256 in `tunnel-client.version`); those
are covered by the stub tests in the first row only.

Points not confirmed in the vendor documentation (re-check them in the live test):

- that the header injected from `mcp.extra_headers` reaches the API unchanged when ChatGPT uses
  "No authentication" (above);
- that `--health.unix-socket` replaces the TCP health and admin listener (default `127.0.0.1:8080`)
  rather than adding to it (the vendor's flag help says it does, its configuration.md says
  "Existing TCP defaults remain available"; 8.2), that it serves `/readyz` there, and that
  `--health.listen-addr 127.0.0.1:0` is accepted next to it (flag help: "Ignored when
  health.unix-socket is set"); the live test records `ss -ltnp | grep tunnel-client`;
- that the Tunnels page's download is the public GitHub release asset
  `tunnel-client-vX.Y.Z-linux-amd64.zip` listed in that release's `SHA256SUMS.txt` (the vendor's
  readme says the button points at the public release; asset name from its validation example);
- whether `tunnel-client doctor` contacts the control plane or the MCP server (setup runs it
  before the API is up);
- whether `file:` values are used byte for byte (both files have no trailing newline either way);
- the Platform pages: restricting a runtime key to one tunnel, the usage limit and whether tunnel
  traffic counts against it, restricting a tunnel's Use to one person, the download's archive
  and binary name; the ChatGPT labels (Plugins or Settings -> Connectors, "Connection: Tunnel");
- that admin keys start with `sk-admin-` (setup's refusal is a heuristic);
- that the forwarded requests carry no browser `Origin` and a loopback `Host` (otherwise the API
  answers 403 or 421; `status`, the API log and the tunnel log show it);
- how tunnel-client treats this server's 401 challenge without OAuth metadata during its startup
  discovery (the vendor says non-timeout discovery failures settle readiness).
