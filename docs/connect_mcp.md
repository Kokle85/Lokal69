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
| 5 | Read scopes first; verify `deals_health`, `reviews_list_pending`, one specific candidate and `deals_estimate_import_costs` for it in the intended dot conversation | ask dot to call those tools; check that dot relays the preview banner (section 7) | not run |
| 6 | Review writes only after approval; test claim/submit on a clearly labelled canary case | a SYNTHETIC canary review case in a non-production workspace | not run |
| 7 | Source and dashboard links open for the owner | open the links returned by `deals_get_candidate` | not run |
| 8 | Record evidence | client version/surface, negotiated protocol, SDK version (2.3.0), tool-list hash (`sha256` of the `tools/list` result), exact successful test IDs/request ids | not run |
| 9 | Native MCP Events first, Slack fallback only if needed | section 5 | **blocked** |

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
