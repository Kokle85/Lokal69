# MCP Events (ChatGPT / dots) and webhook signing: verified reference

Research date: 2026-10-06. Everything below was read from live sources or confirmed by running code in a
scratch venv (Python 3.13, `standardwebhooks==1.1.0`, `mcp==2.3.0`). Anything marked **UNVERIFIED**
could not be confirmed from a primary source.

| Source | How read | Version / date |
|---|---|---|
| https://developers.openai.com/plugins/build/mcp-events (`.md` variant) | raw markdown via curl | live 2026-10-06 |
| https://developers.openai.com/plugins/build/mcp-server (`.md`) | raw markdown | live 2026-10-06 |
| https://developers.openai.com/api/docs/guides/custom-mcp-server (`.md`) | raw markdown | live 2026-10-06 |
| github.com/modelcontextprotocol/experimental-ext-triggers-events | `git clone`: README + `docs/design-sketch-proposal.md` | HEAD `6682596` (merge PR #1, 2026-09-08). The doc header says "Draft proposal, 2026-02-19" |
| Standard Webhooks spec `spec/standard-webhooks.md` | raw.githubusercontent | main, 2026-10-06 |
| PyPI `standardwebhooks` | installed and source read | **1.1.0** (uploaded 2026-07-21), no runtime deps, py>=3.9 |
| PyPI `mcp` | installed and source read | **2.3.0** (deps include `mcp-types==2.3.0`, `httpx2`, `starlette 1.7`) |
| https://docs.slack.dev/authentication/verifying-requests-from-slack (`.md`) | raw markdown | live 2026-10-06 |

Tip: every developers.openai.com and docs.slack.dev page has a markdown copy at `<url>.md`.

---

## 1. What ChatGPT supports (OpenAI "MCP Events" page)

- **Where it works:** Work chats on ChatGPT web, Work chats in the desktop app with **Cloud** selected, and **dots**.
  Workspace controls for plugins and event-triggered tasks apply.
- **Protocol:** MCP 2.0 is required, protocol version **`2026-07-28`**. The server is configured in a *plugin*.
  It needs persistent subscription storage and outbound HTTPS to callback URLs.
- **Supported:** from the draft spec, only **webhook delivery and callback verification**.
- **NOT supported:** "Polling, streaming, and the draft's `gap` and `terminated` control notifications are not
  supported by this integration." This rules out `events/poll`, `events/stream`, `notifications/events/*`,
  the `gap` envelope and the `terminated` envelope.
- **Flow:** (1) the server lists events; (2) the user tells ChatGPT what to monitor and how to react; (3) ChatGPT calls
  `events/subscribe` with a callback URL and signing secret; (4) the server POSTs matching events; (5) ChatGPT
  runs the user's instructions in the subscribed chat. Events may be **batched into one task run**, depending on the
  task's batching settings.
- In the plugin page, discovered events appear next to tools. **Rescan the MCP server** whenever tools or events change.

## 2. Capability advertisement (`server/discover`)

OpenAI's example (the `events` key must appear in `capabilities`):

```json
{"jsonrpc":"2.0","id":1,"result":{"resultType":"complete","supportedVersions":["2026-07-28"],
  "capabilities":{"tools":{},"events":{}}}}
```

The draft uses `"events": {"listChanged": true}` together with `notifications/events/list_changed`. OpenAI's example
has an empty object and does not mention list_changed. Treat `listChanged` as optional, and whether ChatGPT reads it
is **UNVERIFIED**.

The three methods must be served **on the same authenticated MCP endpoint as the tools**:
`events/list`, `events/subscribe`, `events/unsubscribe`.

### mcp 2.3.0 SDK caveat (verified by running a server)
- `mcp` 2.3.0 has **no built-in events support**. A grep for `events/` in `mcp` and `mcp_types` finds nothing.
  It does implement `server/discover` (`mcp_types.version.MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)`).
- The default discover handler builds `types.ServerCapabilities(...)`. The runner then **sieves spec-method results
  through the wire model**, and `mcp_types._v2026_07_28.ServerCapabilities` has `extra="ignore"`. As a result, an
  `events` key is **silently dropped**. The probe confirmed this: without a fix, discover returned no `events`.
- A working fix that was tested over Streamable HTTP has two parts:
  1. Serve the methods with an `Extension` whose `methods()` returns `MethodBinding("events/list", ParamsModel, handler)`
     (and the same for subscribe and unsubscribe). Custom methods skip the spec sieve; the runner adds
     `resultType:"complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`. Side effect: the extension's
     identifier also appears under `capabilities.extensions`.
  2. Add a `ServerMiddleware` (`async def __call__(self, ctx, call_next)`) passed through `MCPServer(middleware=[...])`.
     It patches the already-serialized dict when `ctx.method == "server/discover"`:
     `result["capabilities"]["events"] = {}`. The middleware runs outside the sieve, so the key survives.
- Errors: `raise mcp.MCPError(code=-32015, message="CallbackEndpointError", data={"reason": "challenge_failed"})`
  produced `{"error":{"code":-32015,...,"data":{"reason":"challenge_failed"}}}`. Missing required params gave HTTP 400 with
  `-32602 "Invalid request parameters"`.
- Params models: subclass `mcp.types.RequestParams`. It uses a camelCase alias generator and validates **by alias**, so
  `ttl_ms` maps to `ttlMs`. Use `params.model_fields_set` to tell an omitted `ttlMs` (server default) apart from
  `ttlMs: null` (no expiry). Both were verified.
- Raw HTTP clients of the 2026-07-28 Streamable HTTP endpoint must send the `MCP-Protocol-Version: 2026-07-28` **and**
  `Mcp-Method: <method>` headers. Without the latter the server returns error `-32020` "mcp-method header does not match". ChatGPT's
  client handles this itself.

## 3. `events/list`

Params: `{ "cursor"?: string }`. Result: `{ "events": EventDescriptor[], "nextCursor"?: string }`.

EventDescriptor fields: `name` (stable; recommended form is a dotted `[a-zA-Z0-9_]` hierarchy), `description`,
`delivery` (for ChatGPT, `["webhook"]`), `inputSchema` (JSON Schema of the subscribe `arguments`), `payloadSchema` (JSON Schema of the
occurrence `data`), and optional `_meta`. OpenAI's guidance: expose filters such as document, project or queue IDs and
**apply them server-side before delivery**. Return only the events the connected account is allowed to discover.
Draft schema evolution: change schemas only additively. Make a breaking change by publishing a new event name
(for example `review.pending.v2`), because a refresh re-sends the old `arguments` and would fail with `-32602`.

## 4. `events/subscribe` (create OR refresh, idempotent)

Request:
```json
{"jsonrpc":"2.0","id":2,"method":"events/subscribe","params":{
  "name":"comment.created","arguments":{"document_id":"doc_123"},
  "delivery":{"mode":"webhook","url":"https://receiver.example.com/mcp-events/callback_123",
              "secret":"whsec_<base64-encoded-signing-key>"},
  "cursor":null, "ttlMs": 3600000}}
```
The draft also allows `maxAgeMs` (a replay floor). It is not mentioned by OpenAI, so accept it and ignore it when replay is not supported.

Result:
```json
{"jsonrpc":"2.0","id":2,"result":{"id":"sub_123","refreshBefore":"2026-10-02T12:00:00Z","cursor":null,"truncated":false}}
```
Optional draft field on refresh: `deliveryStatus {active, lastDeliveryAt, lastError, failedSince?, throttled?, retryAfterMs?}`.
`lastError` must be one of `connection_refused|timeout|tls_error|http_4xx|http_5xx|challenge_failed`, and must never contain a raw
endpoint response. Whether ChatGPT reads it is **UNVERIFIED**.

Server checklist from OpenAI, in order:
1. Authorize the user for this event and these arguments. In the draft, webhook mode requires an authenticated principal
   and returns `-32012 Forbidden` otherwise.
2. Validate the name and arguments against the definition. **Require a `whsec_` secret whose base64 decodes to 24–64 bytes**
   (otherwise `-32602`). The server never generates the secret; the client supplies it.
3. Validate the callback URL and verify it (section 5). `https://` only (otherwise `-32602`).
4. Store the subscription, owner, filters, URL, secret and expiration. **Keep it across restarts** for the granted lifetime.

**Identity:** a deterministic ID derived from **(authenticated principal, callback URL, event name, arguments)**.
Compare arguments as **canonical JSON**, so key order cannot create duplicates. All four parts are immutable. Changing the
filter or URL means unsubscribe followed by a new subscribe. The `id` is a routing handle, **not a capability**, and is never accepted as input.

**Lifetime / TTL:**
- `ttlMs` omitted → server default. `ttlMs: <n>` → grant **≤ n**. The one exception is clamping *up* to a server minimum
  so clients cannot cause refresh storms.
- `ttlMs: null` requests no expiry. Return `refreshBefore: null` **only** when granting that. Otherwise return a finite
  time and **stop delivering when it passes**. The draft's recommended finite grants run from minutes to about one day.
- **Refresh:** ChatGPT calls `events/subscribe` again before `refreshBefore`, with the same identity and the last saved cursor.
  The server updates the record and returns a new `refreshBefore`.
- **Secret rotation:** if a refresh carries a new secret, replace the stored one. During a short window, **sign with both**
  keys (space-separated signatures).
- **Cursor:** return `cursor: null` (and `truncated: false`) for event types that do not support replay. Events missed during an outage are
  then unrecoverable through the protocol. If replay is supported, never return a cursor that skips undelivered events, and set
  `truncated: true` when history is gone.
- Re-check the user's access during the lifetime and stop delivery if it is revoked. Because ChatGPT does not support
  `terminated`, delivery simply stops, and the next refresh returns `-32012`.

## 5. Callback verification challenge

Before sending any application data, POST a signed verification body to the callback URL:
```json
{"type":"verification","challenge":"a-single-use-random-value"}
```
- Headers are the same as for a delivery. Use a **unique `webhook-id`** such as `msg_verification_123` (draft form:
  `msg_<type>_<random>`), sign with the subscription's secret, and include `webhook-timestamp`, `webhook-signature` and
  `X-MCP-Subscription-Id`.
- ChatGPT replies with **2xx** and the body `{"challenge":"a-single-use-random-value"}`. Require a 2xx, then compare the echoed value in
  **constant time** (`hmac.compare_digest`) before activating delivery.
- The challenge must be **fresh, single-use and short-lived**. Neither OpenAI nor the draft gives an exact TTL or timeout:
  **UNVERIFIED**. A reasonable choice is a 32-byte `secrets.token_urlsafe`, valid for about 60 s, with a request timeout of about 10 s.
- **Cache** a successful verification per (authenticated principal, callback URL) for a bounded period, so that repeated
  subscribes and refreshes do not re-challenge. In the draft the cache covers all arguments for that pair.
- On failure, return JSON-RPC **`-32015` `CallbackEndpointError`** with `data.reason`, for example `challenge_failed` or `timeout`
  (the draft also lists `connection_refused` and `tls_error`).
- The draft also allows an allowlist, out-of-band verification, or `/.well-known/mcp-webhook-receiver.json` instead of the
  handshake. OpenAI documents **only the challenge**.

## 6. Event delivery (webhook POST)

Body: **exactly one** `EventOccurrence` per request.
```json
{"eventId":"evt_456","name":"comment.created","timestamp":"2026-10-01T12:05:00Z",
 "data":{"document_id":"doc_123","comment_id":"comment_456","text":"...","url":"https://..."},"cursor":null}
```
- `eventId`: unique, **kept the same across retries**. Prefer the upstream's stable ID. `timestamp`: the occurrence time as ISO 8601
  **with a timezone**. `name` must equal the subscribed event. `data` must match `payloadSchema`. Optional: `_meta`.
- Put application fields **inside `data`**. A top-level `type` marks a control envelope (only `verification` applies to ChatGPT).
- Keep payloads minimal: send a summary and expose a read tool for the full record. Treat user-authored text as data, and
  **never include instructions to the model** in a payload.

Headers (OpenAI table):

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `webhook-id` | **same value as body `eventId`** |
| `webhook-timestamp` | signing time, Unix **seconds** |
| `webhook-signature` | Standard Webhooks HMAC: `v1,<base64>` (space-separated list during rotation) |
| `X-MCP-Subscription-Id` | the `id` returned by `events/subscribe` |

**Signature:** `v1,` + base64( HMAC-SHA256( key = base64decode(secret without `whsec_`),
msg = `webhook-id + "." + webhook-timestamp + "." + raw_body_bytes` ) ). The id and timestamp prefix is UTF-8.
**Serialize the body once and send exactly those bytes.**

**Size:** the complete request body must be **≤ 256 KiB (262,144 bytes)**.

**Responses and retries:**
- **2xx** means received. ChatGPT processes the event asynchronously, so a 2xx does *not* mean the dot acted.
- Retry transient failures with **exponential backoff and a bounded number of attempts**. On each attempt keep the `eventId`/`webhook-id` and
  **generate a fresh timestamp and signature**. Draft guidance: 3–5 attempts over at most 10–15 minutes. A typical ack timeout is about 5 s
  (OpenAI's Node example uses a 10 s `AbortSignal.timeout`).
- **Do not retry `410` or `413`.** In the draft both are *non-retryable for that delivery only*. Neither ends the subscription.
  (This differs from the general Standard Webhooks spec, where 410 means "disable the endpoint".)
- In the draft, `503` or `425` from a receiver that does not yet know the subscription should be retried.
- Never follow redirects: use `redirect: "error"`, and in Python `follow_redirects=False`, treating 3xx as failure.
- Events can arrive **out of order and duplicated**. Make write tools idempotent and prevent feedback loops.

**SSRF (applies to both verification and delivery):** HTTPS only. Resolve DNS **at connection time**, reject non-public addresses
(the IANA special-purpose registries: 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fc00::/7, fe80::/10, ...), then connect
to the *validated IP* while keeping the original hostname for SNI, the Host header and certificate verification. No redirects.
Rate-limit verification per destination host.

## 7. `events/unsubscribe`

```json
{"jsonrpc":"2.0","id":3,"method":"events/unsubscribe","params":{"name":"comment.created",
  "arguments":{"document_id":"doc_123"},"delivery":{"mode":"webhook","url":"https://receiver.example.com/mcp-events/callback_123"}}}
```
The subscription is resolved by (principal from auth, url, name, canonical arguments), and the request has no `secret`. Stop delivery and return
`{"result":{}}`. The operation is **idempotent** and must be authorized against the connected account. OpenAI's text says to return an empty result.
The draft lists `-32011 NotFound` (`data.kind:"subscription"`) for an unknown key, but for ChatGPT it is safest to return `{}` even when nothing
matched, so that retries remain idempotent. The draft's position here is ambiguous: **UNVERIFIED** which one ChatGPT expects.

## 8. Error codes (draft, general-purpose, in the `[-32000,-32099]` range)

| Code | Name | Use |
|---|---|---|
| -32602 | InvalidParams | args do not match inputSchema, URL malformed or not https, bad `whsec_` |
| -32011 | NotFound | unknown event name (`data.kind:"event"`) / no subscription (`"subscription"`) |
| -32012 | Forbidden | principal not allowed, access revoked, or webhook call without authentication |
| -32013 | ResourceExhausted | quota reached (`data.limit:"subscriptions"`, optional `data.max`) |
| -32014 | Unsupported | e.g. `{"feature":"deliveryMode","value":"push"}` |
| -32015 | CallbackEndpointError | verification failed or unreachable; `data.reason` is one of the categories in section 4 |

## 9. Standard Webhooks spec facts (spec/standard-webhooks.md)
- Secret: random **24–64 bytes**, serialized as `whsec_` + base64. Asymmetric keys use `whsk_`/`whpk_` with `v1a,` signatures (ed25519).
- Headers: `webhook-id`, `webhook-timestamp` (integer Unix seconds) and `webhook-signature` (space-delimited list). Example:
  `v1,K5oZfzN95Z9UVu1EsfQmfVNQhnkZ2pj9o9NDN/H/pI4= v1a,hnO3...`.
- Signed content: `msg_id.timestamp.payload`. The id and timestamp must not be user-controlled and must not contain `.`.
- Verifiers use constant-time comparison, a timestamp tolerance check, and `webhook-id` as an idempotency key (for example a 5-minute cache).
- Recommended request timeout: 15–30 s. Respect `retry-after` on 429, 502, 503 and 504.
- Spec test vector, reproduced with the Python lib: secret `whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw`, id
  `msg_p5jXN8AQM9LWM0D4loKWxJek`, ts `1614265330`, body `{"test": 2432232314}` gives
  `v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=`.

## 10. Python `standardwebhooks` 1.1.0: exact API (source read and behavior tested)

```python
from standardwebhooks import Webhook, WebhookVerificationError      # __all__ = ["Webhook", "WebhookVerificationError"]
from standardwebhooks.webhooks import EmptyWebhookSecretError       # not re-exported

Webhook(whsecret: str | bytes)
Webhook.sign(self, msg_id: str, timestamp: datetime, data: str) -> str            # "v1,<base64>"
Webhook.verify(self, data: bytes | str, headers: dict[str, str], *, json_parse: bool = True) -> Any
```
- **Secret:** for a `str`, strip `whsec_` if present, then `base64.b64decode(s + "==")` (lenient: unpadded input is accepted and invalid
  characters are dropped). For `bytes`, the value is used raw. An empty secret raises `EmptyWebhookSecretError`. Badly malformed base64 raises `binascii.Error`.
  **No 24–64 byte length check** (a 3-byte secret was accepted), so validate it yourself (section 11).
- **sign():** `ts = floor(timestamp.replace(tzinfo=timezone.utc).timestamp())`.
  **Pitfall:** `.replace` *relabels* rather than converts. A non-UTC aware datetime (for example Europe/Skopje) produces a **wrong** timestamp
  (verified). Always pass `datetime.now(timezone.utc)`. An `int` raises `AttributeError`.
  **Pitfall:** `data` must be `str`. Passing `bytes` silently signs the `"b'...'"` repr and the signature is wrong (verified).
  The library emits only one `v1` signature. For rotation, join `f"{sig_new} {sig_old}"` yourself.
- **verify():** decodes bytes as UTF-8 and lowercases header names. It raises `WebhookVerificationError` for missing headers
  ("Missing required headers"), a non-numeric ts ("Invalid Signature Headers"), a timestamp more than **5 minutes** old or in the future ("Message timestamp too old/new";
  the tolerance is **hard-coded and not configurable**), and when nothing matches ("No matching signature found"). It skips any version other than
  `v1` (so `v1a` is ignored) and accepts if **any** space-separated `v1` signature matches (`hmac.compare_digest`).
  On success it returns `json.loads(body)`, or `None` when `json_parse=False`.
  **Pitfall:** a signature entry without a comma raises a bare **`ValueError`**, and bad base64 padding raises `binascii.Error` (a `ValueError`
  subclass). Catch `(WebhookVerificationError, ValueError)`.
- The library does **not** deduplicate on `webhook-id`. You must do that.

## 11. Verified Python helpers (all were run successfully)

```python
import base64, binascii, hashlib, json
from datetime import datetime, timezone
from standardwebhooks import Webhook, WebhookVerificationError

def parse_whsec(value: str) -> bytes:            # enforce whsec_ + base64(24..64 bytes) -> else JSON-RPC -32602
    if not isinstance(value, str) or not value.startswith("whsec_"):
        raise ValueError("secret must start with whsec_")
    b64 = value[6:]
    try:
        raw = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("secret is not valid base64") from exc
    if not 24 <= len(raw) <= 64:
        raise ValueError("secret must decode to 24..64 bytes")
    return raw

def canonical_json(obj) -> str:                  # approximation of canonical JSON (not full RFC 8785 for floats)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def subscription_id(principal: str, url: str, name: str, arguments: dict) -> str:
    key = canonical_json({"principal": principal, "url": url, "name": name, "arguments": arguments})
    return "sub_" + hashlib.sha256(key.encode()).hexdigest()[:32]

def signed_request(secret: str, webhook_id: str, sub_id: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    body = canonical_json(payload)               # serialize once; send these exact bytes
    raw = body.encode("utf-8")
    if len(raw) > 256 * 1024:
        raise ValueError("body exceeds 256 KiB")
    now = datetime.now(timezone.utc)             # UTC-aware, a fresh value per attempt
    return raw, {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,                # == eventId for events; msg_verification_<rand> for the challenge
        "webhook-timestamp": str(int(now.timestamp())),
        "webhook-signature": Webhook(secret).sign(webhook_id, now, body),
        "X-MCP-Subscription-Id": sub_id,
    }

def verify_inbound(secret: str, raw: bytes, headers: dict[str, str]):
    try:
        return Webhook(secret).verify(raw, headers)
    except (WebhookVerificationError, ValueError) as exc:
        raise PermissionError("bad signature") from exc
```
Send with `httpx`/`httpx2` using `follow_redirects=False` and a short timeout, connecting to the pre-validated IP (section 6).

## 12. Slack request signing (for the optional fallback)

- Headers: **`X-Slack-Signature`** and **`X-Slack-Request-Timestamp`**. Header names are case-insensitive.
- Base string: `"v0:" + timestamp + ":" + raw_body`. The body is the raw bytes **before** JSON or form parsing (in Flask, `request.get_data()`).
- Signature: `"v0=" + hex(HMAC-SHA256(key=signing_secret_as_UTF-8_string, base_string))`. The secret is **not** base64-decoded.
  Compare in constant time.
- Freshness: reject when `abs(now - timestamp) > 60*5` (5 minutes) to prevent replay.
- Doc test vector reproduced: secret `8f742231b10e8888abcd99yyyzzz85a5`, ts `1531420618`, the doc's slash-command body gives
  `v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503`.
- Applies to the Events API, shortcuts, slash commands and the Slackbot MCP Client, plus some legacy features. **mTLS** is an alternative:
  the client cert SAN is `DNS:platform-tls-client.slack.com` under a DigiCert root. Bolt for Python verifies signatures built in
  (`slack_bolt/middleware/request_verification`).
- Note: verifying *inbound* Slack events is unrelated to whether a dot reacts to Slack messages. See the project spec, section 22.

## 13. Mapping to Lokal69 (spec section 22, `review.pending.v1`)

- `events/list` → one descriptor `review.pending.v1`, `delivery:["webhook"]`. `inputSchema` holds the permitted profile/queue filters
  (`additionalProperties:false`). `payloadSchema` holds `case_id, case_version, listing_id, listing_revision, readiness, dashboard_url`.
- `ops.event_subscriptions` key = (principal, callback URL, name, canonical args), with a derived `sub_…` id. It stores the encrypted secret,
  `refreshBefore`, verification-cache time and revocation. `ops.event_deliveries` uniqueness = (subscription_id, event_id).
  The outbox `event_id` UUID becomes both `eventId` and `webhook-id` (UUIDs contain no `.`).
- MVP: `cursor: null` and `truncated: false` everywhere; catch-up goes through the pending-review read tool. Treat 410 and 413 as terminal for that delivery,
  and do not deactivate the subscription. Treat other non-2xx responses and timeouts as retry_wait with backoff, regenerating the signature.

## 14. Open / UNVERIFIED items
- Exact challenge lifetime and ChatGPT's timeout for the verification POST and for deliveries (no published numbers).
- Whether ChatGPT sends `ttlMs` (and which value), reads `deliveryStatus`, honours `capabilities.events.listChanged` /
  `notifications/events/list_changed`, or sends `maxAgeMs`.
- Whether `events/unsubscribe` for an unknown key should return `{}` (OpenAI wording) or `-32011` (draft).
- ChatGPT's egress IP ranges for callbacks. The draft defers this to SEP-2127 server cards. OpenAI publishes "ChatGPT connectors IP
  ranges" for *inbound* MCP traffic (https://developers.openai.com/api/docs/guides/ip-addresses), not for callback receivers.
- Whether ChatGPT's receiver itself returns 410 or 413 (the rule only states that senders must not retry them).
- That mcp SDK versions after 2.3.0 still need the discover-capability middleware. Re-check `mcp_types` for an events capability field.
