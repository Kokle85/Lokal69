# Notification bridge: pending reviews, native MCP Events and the optional Slack fallback

Status: `implemented` + `fixture_verified` (unit/adversarial tests with synthetic data).
No subscription, Slack destination, callback or token exists yet. Every external route is
**disabled by default** and `bridge_status=unavailable` until the gates in section 7 pass.

Sources of truth: spec sections 13, 20, 22, 24, 30–32;
`docs/research/mcp_events_and_webhooks.md` (verified wire facts, 2026-10-06);
`docs/research/mcp_python_sdk.md` sections 0, 6, 7.

## 1. Two separate concepts

| Concept | What it is | Where it lives | Default |
|---|---|---|---|
| **Internal review availability** | A durable `pending` review case that the dashboard and the MCP tools (`reviews_list_pending`, `reviews_claim`, ...) can always retrieve | `app.review_cases` + `ops.outbox`, written in the same transaction | Always on |
| **External activation / notification** | A verified channel that *delivers* an event and, if the client supports it, starts dot on that case | `integrations/event_bridge.py` (native MCP Events) or `integrations/slack.py` (fallback) | Off (`bridge_status=unavailable`) |

MCP connectivity satisfies the first, not the second. A row in the database, a Supabase
webhook or an arbitrary HTTP endpoint does not wake dot. A successfully posted Slack message
does not prove that dot reads that channel or that bot-authored messages trigger anything.

### Why tools alone do not wake dot

MCP tools are *pulled*: dot calls them during a turn it is already running. Nothing in a tool
definition can start a new turn when a review case appears. A new turn needs a subscription
the client created itself (`events/subscribe`) and a callback the server POSTs to. Without
that subscription, the pending-review tool is the only path, and dot sees new cases only when
the owner (or a schedule the owner configured in the client) asks it to look.

## 2. Activation route selection

`event_bridge.select_activation_route(settings)` returns `(route, bridge_status, blockers)`:

- `route=none`, `bridge_status=unavailable` unless `ALLOW_EXTERNAL_NOTIFICATIONS=true` **and**
  `EVENT_BRIDGE_ENABLED=true`.
- `route=none`, `bridge_status=unavailable` with named blockers when the selected route cannot
  work: native events without a valid `MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY` (subscription
  secrets could not be stored), or Slack without `NOTIFICATION_PROVIDER=slack`, `SLACK_BOT_TOKEN`,
  `SLACK_SIGNING_SECRET` and `SLACK_CHANNEL_ID`. Blockers name settings, never values.
- `bridge_status=configured` when a route is selected but `EVENT_BRIDGE_VERIFIED_AT` (an aware
  ISO timestamp recorded after a successful end-to-end canary, section 8) is missing, invalid or
  in the future.
- `bridge_status=verified` only with that recorded timestamp.
- It **raises** `ActivationRouteConflict` instead of guessing when:
  - native events (`MCP_EVENTS_ENABLED`, `EVENT_BRIDGE_PROVIDER=mcp_events` or
    `NOTIFICATION_PROVIDER=mcp_events`) and Slack (`EVENT_BRIDGE_PROVIDER=slack` or
    `NOTIFICATION_PROVIDER=slack`) are both enabled. One selected route prevents duplicate
    review runs; Slack owner alerts are refused while native events are on, because a dot
    that also reads that channel would be triggered twice;
  - `EVENT_BRIDGE_ENABLED=true` with `EVENT_BRIDGE_PROVIDER=disabled`;
  - `EVENT_BRIDGE_PROVIDER=mcp_events` without `MCP_EVENTS_ENABLED=true`.

## 3. Preferred route: native MCP Events (`review.pending.v1`)

### 3.1 Verified wire behaviour (implemented)

| Topic | Behaviour | Code |
|---|---|---|
| Protocol | MCP `2026-07-28`; ChatGPT supports only **webhook delivery + callback verification** (no `events/poll`, `events/stream`, `gap`, `terminated`) | – |
| Capability | `server/discover` must contain `capabilities.events = {}`. `mcp` 2.3.0 drops unknown capability keys, so the MCP server package must add it in a `ServerMiddleware` that patches the serialized discover result, and serve the methods through an `Extension` with `MethodBinding`s | MCP server package |
| Methods | `events/list`, `events/subscribe`, `events/unsubscribe` on the **same authenticated endpoint** as the tools | MCP server package calls the functions below |
| `events/list` | One descriptor: `name`, `description`, `delivery:["webhook"]`, `inputSchema` (`additionalProperties:false`; required `profile` in `primary`, `manual_4000`, `below_target_watch`, optional `queue` label), `payloadSchema` (`case_id`, `case_version`, `listing_id`, `listing_revision`, `readiness`, `dashboard_url`). Only principals with `reviews:read` + `events:subscribe` see it; the profile enum can be narrowed to the workspace's enabled profiles | `list_events`, `event_descriptor` |
| Authorization | Requires `reviews:read` **and** `events:subscribe`; system principals are refused (`-32012`). A subscription grants no `reviews:write` | `validate_subscribe_params` |
| Secret | Client-supplied `whsec_` + strict base64 of **24–64 bytes**, else `-32602`. Never generated by the server, never logged | `webhook_signing.parse_whsec` |
| Callback URL | `https://` on port 443 only, public DNS name or public IP literal, no credentials/fragment, ≤ 2048 chars, else `-32602` | `validate_callback_url` |
| Identity | `sub_` + first 32 hex of SHA-256 over canonical JSON of {principal (from auth), workspace, callback URL, event name, arguments}. Key order cannot create duplicates; body fields cannot change the principal. The id is a routing handle, never accepted as input | `subscription_identity` |
| TTL | omitted → server default; number → grant ≤ n, **clamped up** to the server minimum; `null` (no expiry requested) → MVP still grants a finite lifetime; `refreshBefore` is never null; delivery stops when it passes. `maxAgeMs` accepted and ignored | `grant_ttl` |
| Cursor | Always `cursor: null, truncated: false`, the verified rule for an event type without replay (research doc sections 4 and 13). A client-supplied cursor is accepted and ignored (we never issue one). Catch-up is the pending-review tool | `subscribe_result` |
| Refresh / rotation | Same identity → update the existing record. A new secret replaces the stored one; during `secret_rotation_overlap` deliveries are signed with both keys (space-separated, newest first). Rotation invalidates the cached verification | `signing_secrets`, `needs_verification` |
| Verification | Before any application data: POST `{"type":"verification","challenge":<32 random bytes, urlsafe>}` with `webhook-id: msg_verification_<random>`, signed headers and `X-MCP-Subscription-Id`. Require **2xx** and a constant-time-equal echo `{"challenge": ...}`. Single use, accepted only within 60 s of issue | `run_verification_challenge`, `ChallengeLedger` |
| Verification failures | `-32015 CallbackEndpointError` with `data.reason` ∈ `challenge_failed`, `timeout`, `connection_refused`, `tls_error`, `http_4xx`, `http_5xx` (a refused redirect or bad echo is `challenge_failed`; a destination our SSRF policy refuses is `connection_refused`) | `VerificationResult.raise_for_failure` |
| Verification cache | Per (principal, workspace, callback URL), covers all arguments, bounded TTL; invalidated by secret change or a future-dated record | `verification_cache_key`, `needs_verification` |
| Verification rate limit | Per destination host sliding window (process-local helper) → `-32013` | `VerificationRateLimiter` |
| Occurrence | Exactly one per request: `{eventId, name, timestamp, data, cursor:null}`. `eventId` = outbox `event_id` UUID (no `.`), `timestamp` = occurrence time in RFC 3339 with `Z`, `data` = the six payload fields only (no summary, seller text, money or tokens). Dashboard links must not embed tokens | `build_occurrence` |
| Headers | `Content-Type: application/json`, `webhook-id` (= `eventId`), `webhook-timestamp` (Unix seconds of signing), `webhook-signature` (`v1,<base64>`), `X-MCP-Subscription-Id` | `webhook_signing.build_signed_request` |
| Signature | Standard Webhooks HMAC-SHA256 over `id.timestamp.body` via the `standardwebhooks` library; body serialized **once** (canonical JSON) and those exact bytes sent; UTC-converted timestamps (the library relabels instead of converting); `str` body (bytes would sign their repr) | `sign` |
| Size | Complete body ≤ **262,144 bytes**; larger payloads are never sent (`FAILED payload_too_large`) | `WEBHOOK_BODY_LIMIT_BYTES` |
| Responses | 2xx = **receipt** (`provider_accepted_at`), never review completion. 410/413 = terminal for that delivery only (subscription stays). 3xx = refused, never followed. 408/425/429/5xx and failures before sending = retry with exponential backoff + jitter, honouring `Retry-After`. Other 4xx = terminal. When the status line arrived but the body or deadline then failed, the outcome follows that status (a 410 followed by a reset is still terminal, never uncertain). A timeout or connection loss after the request was sent **without** a status = **UNCERTAIN** | `deliver` |
| Retries | Same `eventId`/`webhook-id` on every attempt; fresh `webhook-timestamp` and signature each time; bounded attempts and time window | `deliver`, `RetryPolicy` |
| Unsubscribe | Resolved from (principal from auth, workspace, url, name, canonical args); no secret; idempotent; always returns `{}` | `validate_unsubscribe_params`, `unsubscribe_result` |
| SSRF | Validate at subscribe time, then resolve DNS **at connection time** (every answer must be public; mixed answers fail), connect to the validated IP with the hostname for SNI, certificate verification and `Host`; no redirects; no environment proxies; keep-alive disabled so a TLS session for one hostname is never reused for another | `safe_http.SafeHttpClient` |

JSON-RPC errors (`EventsProtocolError.to_jsonrpc_error()` / `.to_mcp_error()`):

| Code | Name | Raised for |
|---|---|---|
| -32602 | Invalid params | arguments not matching `inputSchema`, bad URL/secret/ttl/cursor |
| -32011 | NotFound | unknown event name (`data.kind:"event"`) |
| -32012 | Forbidden | missing `reviews:read`/`events:subscribe`, system principal, revoked access |
| -32013 | ResourceExhausted | per-principal subscription quota, verification rate, outstanding challenges |
| -32014 | Unsupported | `delivery.mode` other than `webhook` (`{"feature":"deliveryMode","value":...}`) |
| -32015 | CallbackEndpointError | verification failed (`data.reason`) |

### 3.2 Dispatch rules (outbox dispatcher)

1. Load the committed outbox event and the subscription; decrypt secrets with `SecretBox`
   (AAD = workspace + subscription id + secret version).
2. `decide_dispatch(...)` with the existing `(subscription_id, event_id)` delivery state and the
   case's *current* version/state:
   `SEND`, `SKIP_DUPLICATE` (already delivered), `SKIP_IN_FLIGHT`, `HOLD_UNCERTAIN`,
   `SKIP_FINAL`, `SKIP_FILTER_MISMATCH` (server-side profile/queue filter; an event without a
   profile never matches), `SUPPRESS_STALE` (newer case version = out-of-order event, or the
   case is no longer `pending`), `SKIP_INACTIVE` (expired, revoked, unsubscribed, unverified).
3. `deliver(...)` runs the membership/scope recheck hook (`access_check`) **before every
   delivery**. `False` → nothing is sent and `revoke_subscription=True`; an exception fails
   closed (retry later, nothing sent).
4. Record `send_attempted_at`, `provider_accepted_at`, the outcome and `next_attempt_at` on the
   unique delivery row. `owner_seen_at` stays unknown: a 2xx is not evidence that dot acted.

### 3.3 Uncertain deliveries (spec 13)

MCP Events has no provider lookup API, so reconciliation is impossible. The documented
conservative rule (`decide_uncertain_followup`): hold for 2 minutes, then re-send **at most
once with the same `eventId`/`webhook-id`** (the receiver deduplicates on the Standard Webhooks
idempotency key), then keep the delivery visibly `uncertain`. Events missed this way are still
reachable through the pending-review tool.

### 3.4 Engineering defaults (PROPOSED; not published by OpenAI)

| Setting | Value | Where |
|---|---|---|
| Default / minimum / maximum TTL | 12 h / 5 min / 24 h | `SubscriptionPolicy` |
| Verification cache | 24 h | `SubscriptionPolicy.verification_cache_ttl` |
| Challenge lifetime / verification timeout | 60 s / 10 s | `SubscriptionPolicy` |
| Subscriptions per principal | 10 | `SubscriptionPolicy` |
| Secret rotation overlap | 1 h | `SubscriptionPolicy` |
| Delivery attempts / base delay / max delay / window | 5 / 30 s / 5 min / 15 min | `RetryPolicy` |
| Max honoured `Retry-After` | 15 min (beyond → dead letter) | `RetryPolicy` |
| Request timeout / total deadline | 10 s / 15 s | `RetryPolicy`, `HttpLimits` |
| Uncertain hold / resends | 2 min / 1 | `UncertainPolicy` |

### 3.5 UNVERIFIED items (re-check before activation)

- Exact challenge lifetime and ChatGPT's timeouts for verification and delivery POSTs.
- Whether ChatGPT sends `ttlMs` (and which value), reads `deliveryStatus`, honours
  `capabilities.events.listChanged`, or sends `maxAgeMs`.
- Whether `events/unsubscribe` for an unknown key should return `{}` (OpenAI wording, used here)
  or `-32011` (draft).
- ChatGPT's egress IP ranges for callbacks (an allow-list is therefore not used).
- Whether ChatGPT's receiver returns 410/413 itself, and whether it deduplicates on `webhook-id`
  (the uncertain-resend rule assumes Standard Webhooks behaviour).
- That `mcp` releases after 2.3.0 still need the discover-capability middleware.
- That the deployed dot actually supports discovery, subscription and event handling for this
  plugin (only an end-to-end canary proves it).

## 4. Secrets at rest

`integrations/secret_box.py`: AES-256-GCM with a versioned envelope
(`format version | key id | 96-bit nonce | ciphertext+tag`). The associated data binds the
ciphertext to the workspace, subscription id and secret version **and** to the envelope header,
so ciphertext copied into another row, or a key id edited in place, fails to decrypt.

`MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY` is either one base64 32-byte key (key id 1) or a
keyring `"<id>:<base64>,<id>:<base64>"` whose **first** entry is the current key. Rotation:
prepend the new key → `needs_rewrap`/`rewrap` every row → remove the old entry. Wrong-length
keys are rejected at startup. Decrypt failures raise `SecretDecryptionFailed` with a generic
message; plaintext is never logged or placed in errors or reprs.

## 5. Optional Slack fallback (disabled provider)

`integrations/slack.py` refuses to send (raises `SlackSendBlocked`, makes no request) unless
`ALLOW_EXTERNAL_NOTIFICATIONS=true`, `NOTIFICATION_PROVIDER=slack`, a destination binding
approval reference is recorded, the channel matches `SLACK_CHANNEL_ID`, and native MCP Events is
not the selected route. It implements:

- `chat.postMessage` with the bot token in the `Authorization` header, a minimal escaped text
  that ends with a stable short reference `[ref SDR-XXXXXXXXXXXX]` and an https dashboard link that
  passes the same no-token/no-credential check as the MCP Events payload
  (`event_bridge.dashboard_url_problem`), and message metadata
  `{"event_type":"suv_deals.review_pending","event_payload":{"event_ref","dedup_key"}}`;
- classification: `ok:true` → posted (receipt); `ratelimited`/429/`service_unavailable`/
  `request_timeout` → retry; `internal_error`/`fatal_error`, Slack 5xx, unparseable 2xx and
  timeouts after sending → **uncertain**; other errors → failed;
- `reconcile_uncertain_post`: `conversations.history?include_all_metadata=true` from one minute
  before the attempt, bounded pages, matching our metadata `event_ref` (or the text reference)
  only on messages authored by our bot id / app id (without a configured identity: by some app or
  bot). A human quoting the reference never counts as the receipt. Result `found` / `not_found` / `unknown` (lookup failed: keep `uncertain`
  visible, do not resend). Needs a token with `channels:history`/`groups:history`; usable only
  once a real token exists;
- inbound verification: `v0=` HMAC-SHA256 over `v0:<timestamp>:<raw body bytes>` with the signing
  secret as a UTF-8 key, 5-minute window, constant-time compare, body-size bound, all **before**
  parsing; then team/app/channel binding, event-type allow-list, provider `event_id` dedup
  (`ProviderEventDedup`; the durable implementation inserts with `ON CONFLICT DO NOTHING` and is
  the "persist before acknowledging" step), and own-message loop prevention (messages from our
  bot id/user or app id, with our metadata type, or bot-authored messages carrying our event
  references are ignored; a human quoting a reference is processed and the reference returned).

### Slack activation checklist (spec 22; all required)

- [ ] Verified workspace and channel ID belonging to the authorized destination
- [ ] Clear user approval for the channel, event category and data included (recorded as the
      destination binding approval reference)
- [ ] A connected posting identity with minimum required scope (`chat:write`, plus
      `channels:history`/`groups:history` for reconciliation) and private-channel membership.
      Register the custom metadata type `suv_deals.review_pending` under
      `metadata.event_subscriptions` in the app manifest: Slack ignores unregistered metadata
      (with only a warning), and reconciliation then has only the text reference
- [ ] A separately verified consumer/automation route capable of receiving the relevant event type
- [ ] Confirmation that bot-authored messages are not filtered out by that route
- [ ] A tested mechanism for the consumer to call the authenticated MCP tools
- [ ] One end-to-end canary proving case creation, channel event, consumer processing and
      persisted review or acknowledgement

Do not invent an automation webhook URL or claim that generic webhooks can start dot. If the
actual product only supports scheduled polling, configure that only with user authorization.
If no supported wake route exists, keep `bridge_status=unavailable` and rely on the complete
dashboard/MCP queue.

### 5.1 Spec v1.1 category signals (seller replies and seller-reply owner alerts)

Native MCP Events stay the route for candidate discovery only. The two v1.1 event types take
their own path in the dispatcher (`workers.dispatcher._handle_signal`) and are posted ONLY to the
Slack route selected for their category, never through MCP Events:

| Outbox event type | Category | Slack metadata `event_type` | Built by |
|---|---|---|---|
| `seller.reply.received` (`seller.reply.received.v1`) | `seller_reply` (Slack only) | `suv_deals.seller_reply_received` | `slack.build_seller_reply_post_body` |
| `seller_reply.owner_alert` (decision needed / opportunity supported) | `owner_alert` | `suv_deals.owner_alert` | `slack.build_owner_alert_post_body` |

- Gates (`slack.signal_send_blockers` and the route check): `ALLOW_EXTERNAL_NOTIFICATIONS=true`,
  `SELLER_REPLY_SIGNAL_PROVIDER=slack` (seller replies), and an enabled, owner-approved and
  **verified** Slack destination binding selected for the category (`bindings_repo.selected_route`).
  A failing gate leaves the event visibly `blocked` with a code; no request is made.
- Payloads carry ids, a fixed-vocabulary status, typed reason codes and the authenticated
  dashboard link `<APP_BASE_URL>/inquiries/<inquiry_id>/replies/<reply_id>`; never a body,
  address, attachment or credential. Fixture-lineage replies never alert.
- A decision-needed owner alert is raised only for a reason not yet alerted for that inquiry
  (at most one alert per distinct reason per inquiry, decided under the inquiry row lock).
- Every Slack category posts to the single configured `SLACK_CHANNEL_ID` (the binding's channel
  must equal it), so an owner alert lands in the channel dot watches. **dot's trigger must match
  only messages whose metadata `event_type` is `suv_deals.seller_reply_received`**; owner alerts
  (`suv_deals.owner_alert`) are for the owner. Without that filter one escalating reply would
  start dot twice. Register both metadata types under `metadata.event_subscriptions` in the app
  manifest, like `suv_deals.review_pending`.
- **dot trigger filter checklist** (record it with the activation evidence): the trigger's
  channel is `SLACK_CHANNEL_ID`; its condition is the message metadata `event_type ==
  suv_deals.seller_reply_received` (not message text, not "any bot message", not the channel
  alone); `suv_deals.owner_alert` and `suv_deals.review_pending` messages do NOT fire it; a
  hand-written message that imitates the text of a signal does NOT fire it (it carries no app
  metadata); the action is "call `seller_replies_get` / `seller_inquiries_get` over MCP", never a
  reply or a send.
- Signal flood control (`replies_repo.ingest_reply`): a reply arriving while the inquiry's previous
  signal is still to be posted (`pending`, `retry_wait`, `sending` before its attempt began) is
  `coalesced` into it; at most `MAX_SIGNALS_PER_INQUIRY_24H` (PROPOSED 6) signals are emitted per
  inquiry and rolling 24 hours, later replies are stored `rate_limited` without a signal. Both are
  visible on the reply (`signal_status`) and in `GET /api/mail-workers/health`
  (`reply_signals`: emitted / coalesced / rate_limited / inquiries at the cap, plus a coverage-gap
  warning). A `blocked` signal (for example while `ALLOW_EXTERNAL_NOTIFICATIONS=false`) is terminal
  and never swallows a newer reply. When a signal that replies were coalesced into ends
  `dead_letter` or `cancelled` without posting, the dispatcher re-emits ONE new signal for the
  inquiry (its newest coalesced reply; deduplicated, never repeated) so dot is not muted; the cap
  counts these signal events too (`replies_repo.reemit_muted_signal`, docs/schema.md 11.12).

## 6. Observability

- `observability/logging.py`: JSON lines with `request_id`, `run_id`, `job_id`, `case_id`,
  `event_id`, `source_key`, `adapter_version`, `build_id`. A handler-level filter redacts
  Authorization/Bearer credentials, JWTs, PostgreSQL URL credentials (host kept), URL
  credentials, sensitive query parameters (`token`, `key`, `secret`, `signature`, `code`,
  `password`, ...), `whsec_` secrets, Slack tokens and incoming-webhook URLs, Supabase
  `sb_secret_`/`sb_publishable_` keys, key=value secrets, cookies, PEM keys, e-mail addresses and
  DE/IT/CH/MK/international phone numbers, including inside exception messages and tracebacks.
  `httpx`/`httpcore` loggers are raised to WARNING because they log full request URLs. Never log
  callback URLs; use `safe_url_for_log()` (scheme + host). `scripts/redact_logs.py` applies the
  same function to existing log files. The patterns run in linear time on untrusted text (seller
  descriptions can reach logs), and identifier fields such as `source_key`/`dedup_key` are kept.
- `observability/metrics.py`: `suv_deals_outbox_events{provider,state}`,
  `suv_deals_outbox_delivery_attempts_total{provider,outcome}`,
  `suv_deals_outbox_delivery_latency_seconds{provider}`,
  `suv_deals_callback_verifications_total{result}` and
  `suv_deals_inbound_webhook_rejections_total{provider,reason}`; no URL/host/ID labels. Source,
  API-route and MCP-tool labels are restricted to the configured sets (`source_keys`, `api_routes`,
  `tool_names`) and capped at `max_dynamic_label_values` distinct values each (overflow is `other`).
- `observability/audit.py`: audit `event_subscription.create|refresh|verify|unsubscribe|revoke`,
  `secret.rotate` and `notification.route_change` with the verified actor; metadata is redacted
  (secret-named keys lose their values).

## 7. Activation gates (spec 32)

| Capability | State now | Evidence needed |
|---|---|---|
| Pending-review queue via MCP/dashboard | implemented (other packages) | authenticated retrieval in the exact build |
| Native MCP Events code | implemented, fixture_verified | – |
| Native MCP Events activation | **blocked** | dot supports discovery/subscription for this plugin; approved scope; callback security; stored lifecycle; canary + unsubscribe |
| Slack fallback code | implemented, fixture_verified | – |
| Slack as the candidate-discovery fallback (gate `slack_destination`) | **not_requested** (optional; native MCP Events are the candidate route) | every checklist item in section 5 |
| Slack seller-reply route, category `seller_reply` (gate `seller_reply_slack_route`) | **selected by the owner, blocked** (code implemented, fixture/integration verified; nothing posted) | private channel id, approved `seller_reply` destination binding, `ALLOW_EXTERNAL_NOTIFICATIONS=true`, `SELLER_REPLY_SIGNAL_PROVIDER=slack`, the dispatcher's Slack settings (`doctor --process dispatcher`), the dot trigger filtered on `suv_deals.seller_reply_received` (docs/connect_mcp.md 6.2) and a correlated test reply Outlook -> backend -> Slack -> dot -> MCP with dot's processing evidence |
| Automatic dot activation | **blocked** | one selected route + correlated canary (section 8) |

The gates live in `ops.activation_gates`; the reconciler inserts any missing spec 32 gate into
every active workspace (insert-only, recorded progress is never reset), and `deals_health` / the
dashboard Overview list every gate that is neither `active` nor `not_requested` as a blocker.
Per-gate owner actions: `ACTIVATION_GATES.md`.

Smallest owner action to unblock native events: deploy the MCP server with events enabled on an
approved HTTPS domain, add it as a plugin in a Work chat/dot, rescan, and ask dot to monitor the
primary review queue; then run the canary.

## 8. What an end-to-end canary must capture

A canary is an **isolated, labelled synthetic** review case (never a fake market event), plus a
current live listing where practical; their claims stay separate.

1. Commit SHA and running image digest; configuration/source/parser versions.
2. `server/discover` result showing `capabilities.events`, and the `events/list` response.
3. The `events/subscribe` request as received (secret redacted), the derived `sub_...` id, the
   granted `refreshBefore`, and the encrypted-secret row (ciphertext only).
4. The verification challenge exchange: `msg_verification_...` id, status, echo match, timing.
5. The synthetic case's outbox `event_id`, the `(subscription_id, event_id)` delivery row,
   `send_attempted_at`, the 2xx status and `provider_accepted_at`.
6. dot's resulting activity: which read tools it called (MCP request logs with the same case id
   and request ids) and that it performed only the owner-authorized response (for example a
   note or a `needs_information` decision), with the persisted review/decision id.
7. A duplicate-delivery check (same `eventId` re-sent → no second review action) and an
   out-of-order check (older case version suppressed at dispatch).
8. `events/unsubscribe` returning `{}` and a subsequent event producing no callback.
9. Redacted logs for the whole window (`scripts/redact_logs.py`), showing no secrets or callback
   paths, and the metric deltas for deliveries and verifications.

Only after this evidence exists may `EVENT_BRIDGE_VERIFIED_AT` be set, which moves
`bridge_status` from `configured` to `verified`. Until then the honest state is
`bridge_status=unavailable` (or `configured`), and the dashboard/MCP queue remains the
complete working path.
