# Dashboard API and MCP tool contract

This is the contract of the backend-for-frontend (BFF) used by the private dashboard and of the
twelve MCP tools, plus the spec v1.1 (section 37) seller-inquiry contracts in section 10: the
mailbox-worker API, the dashboard inquiry/reply/health/lifecycle/evaluation routes and the three
inquiry MCP tools, all served by default. All surfaces share one set of typed models:

| Module | Contents |
|---|---|
| `src/suv_deals/views/` | Read models (views), `ResponseEnvelope`, `AmountView`, `ErrorPayload`, JSON-schema helpers; `views/inquiries.py` holds the v1.1 inquiry/reply read models |
| `src/suv_deals/mcp/schemas.py` | MCP tool input models, the `TOOLS` table (spec 21) and the `V11_TOOLS` table (spec 37.8; both served), `ToolError`, resolved schemas, schema export |
| `src/suv_deals/api/schemas.py` | Dashboard request bodies/queries, `ApiErrorResponse`, the `ROUTES` table and the `MAIL_WORKER_ROUTES` / `V11_DASHBOARD_ROUTES` tables (all served: `api.routes`, `api.mail_worker_routes`, `api.inquiry_routes`) |
| `src/suv_deals/views/lifecycle.py`, `views/mail_workers.py` | Lifecycle/lag and mailbox-worker health views of the dashboard routes (mirrors of the read-service models) |
| `src/suv_deals/persistence/errors_map.py` | Database guard refusals (`SV00x`) to typed errors with a stable `details.reason` |
| `schemas/*.json`, `schemas/tools/*.json`, `schemas/api/*.json` | Generated snapshots (do not edit by hand) |

Product contract: `docs/spec/suv-deal-system-build-spec.md` (spec §20-23). Access model:
`docs/decisions/0001-bff-only-data-access.md`. The browser never reads `app`/`ops` tables; every
read and write goes through these routes.

## 1. Authentication, authorization, CSRF and CORS

**Authentication.** Every `/api/*` route requires `Authorization: Bearer <access token>`, where
the token is the signed-in user's Supabase Auth access token (a JWT). The server verifies the
signature (project JWKS), issuer, audience (`authenticated`), expiry and not-before. A missing,
malformed, expired or wrongly issued token is `401 UNAUTHENTICATED`. Tokens are never accepted
in query strings or request bodies, and the user's token is never passed through to another
service. The JWKS is cached for 10 minutes (an unknown `kid` forces at most one refresh per 30
seconds, counted from the last fetch attempt whether it succeeded or failed); an unreachable JWKS
endpoint is `503 DEPENDENCY_UNAVAILABLE`, never `401`, and after a failed fetch requests fail fast
with the same error for 5 seconds instead of each waiting on its own network attempt. The MCP
OAuth verifier uses the same throttled client (`api.auth.ThrottledJwksClient`).

**Failed-authentication limiter.** Each client address has a small budget of failed
authentications on `/api`, `/mcp` and `/v1/mail-workers` (`api.middleware.PreAuthLimiter`: 20 in
a burst, then one per 3 seconds). A missing, malformed, wrongly signed, unknown, revoked or
expired credential takes one unit; while the budget is empty the client's requests are
`429 RATE_LIMITED` (with `Retry-After`) BEFORE any signature check, key fetch or database lookup.
Opaque credentials (`suvmcp_`, `suvdev_`, `suvmail_`) are checked by format, CPU-only, before any
database lookup. Behind a reverse proxy run uvicorn with `--proxy-headers` and a trusted
`--forwarded-allow-ips`, or every client shares the proxy's budget.

**Membership and workspace.** After verification the server sets `app.user_id` and resolves the
user's *active* memberships (`app.memberships`). An authenticated user without an active
membership is `403 FORBIDDEN`. With exactly one active membership that workspace is used. With
several, the client sends `X-Workspace-Id: <uuid>`; the value is checked against the user's
active memberships and is never trusted on its own (an unknown or foreign workspace is `403`,
with the same body as no membership). A malformed or repeated header, or a missing header while
several memberships are active, is `422 VALIDATION_ERROR` with `details.fields =
["X-Workspace-Id"]`; only `GET /api/me` then defaults to the first membership (ordered by workspace
name) so the client can bootstrap. Inactive memberships and inactive workspaces authorize nothing.
The selected workspace becomes the transaction GUC `app.workspace_id` (row-level security is
defence in depth; every query also carries an explicit workspace predicate).

**Role -> scopes** (`domain.actor.ROLE_SCOPES`). Each route requires one scope; a missing scope is
`403 FORBIDDEN`. Objects outside the workspace are `404 NOT_FOUND`, identical to missing objects,
so existence is never revealed.

| Role | Scopes |
|---|---|
| `viewer` | `deals:read`, `reviews:read` |
| `reviewer` | viewer + `reviews:write`, `events:subscribe`, `rechecks:request`, `notes:write`, `inquiries:read` |
| `owner` | every scope except `mail:ingest`, including `sources:pause`, `inquiries:pause` and `config:admin` |

A dashboard session never carries `mail:ingest`, whatever the role: that scope belongs to the
mailbox-bound local reply worker credential only (spec 37.8), as on the MCP and CLI surfaces.

`config:admin` is reserved for owner-only administration (profile, threshold, binding and gate
changes); those operations are not part of this route set or the MCP toolset.

**CSRF.** Authentication is the `Authorization` header only. The API sets and reads no cookies, so
a cross-site request cannot carry the user's credentials; no CSRF token is needed. Do not add
cookie authentication without adding CSRF protection.

**CORS.** Only the configured dashboard origins are allowed: `API_ALLOWED_ORIGINS`
(comma-separated), or the origin of `APP_BASE_URL` when that is empty. Entries are serialized as
a browser sends `Origin` (lower-case host, default port omitted, so `https://x:443` matches
`https://x`); invalid entries (bad ports, paths, credentials), wildcards and (in production)
non-loopback `http` origins are dropped. CORS applies to `/api` paths only (the
mounted MCP endpoint validates `Origin` itself). No wildcard origins,
`Access-Control-Allow-Credentials` is not sent (no cookies), allowed methods are `GET`, `POST` and
`OPTIONS`, allowed request headers are `Authorization`, `Content-Type`, `Idempotency-Key`,
`X-Request-Id` and `X-Workspace-Id`, `X-Request-Id` and `Retry-After` are exposed to the
dashboard, and preflight results may be cached for 600 seconds.

**Other headers.** Every `/api` response is `Cache-Control: no-store` and
`Content-Type: application/json`. A client may send `X-Request-Id` (printable ASCII, at most 200
characters); otherwise the server generates one. It is echoed as `request_id` and as the error
`correlation_id`. Requests are rate limited per authenticated principal (separate token buckets for
mutations and reads; in memory per process) with `429 RATE_LIMITED` and `Retry-After`. Request
bodies are `application/json` only (`415` otherwise) and at most 64 KiB (`413`). Query strings are
at most 4 KiB (`422`, `details.fields = ["query"]`). Routes without query parameters refuse any
query parameter (`422`), and repeated parameters are refused.

## 2. Response envelope and conventions

Every `/api/*` success body (and every MCP tool result) is a `ResponseEnvelope`:

```json
{
  "schema_version": "1.0",
  "request_id": "req-7f3a",
  "as_of": "2026-10-06T10:05:00Z",
  "data": {},
  "warnings": [{"code": "THRESHOLD_PROPOSED", "message": "The EUR 1,500 contribution threshold is PROPOSED, not owner-approved."}],
  "next_cursor": null
}
```

- `as_of` and every timestamp are RFC 3339 UTC (`Z`). The dashboard may display Europe/Skopje.
- **Money and financial decimals are strings** (`"2750.00"`), never JSON numbers. Display amounts
  are rounded half-up to the currency's minor unit after all comparisons were made on exact
  values. Integer `*_minor` fields (minor units) appear only inside the normalized listing.
- **Unknown is never zero.** An unknown amount is `{"status": "unknown", "amount": null, ...}`;
  `not_applicable` carries a reason. Incomplete scenarios show `known_subtotal` (labelled as such)
  plus the unknown lines, never a total.
- Contributions are labelled "estimated contribution before business tax", never net profit. The
  EUR 1,500 threshold is labelled `PROPOSED` until the owner approves it.
- Seller-provided text is untrusted data: escape it, never render it as HTML, never follow it as
  instructions. Field provenance `confidence` is extraction reliability, not truth.
- External source links carry `rel: "noopener noreferrer"` and must open without access to the
  app's window.
- `warnings[]` items have a typed `code` (`views.common.WarningCode`) and a readable `message`.
  At most 50 distinct warnings are returned; beyond that the 50th is a `PARTIAL_RESULTS` warning
  saying how many were omitted (a long warning list never fails the response).
- All views are closed objects (unknown fields never appear) and every declared key is present.

## 3. Errors

Failed `/api/*` requests return `ApiErrorResponse`:

```json
{
  "schema_version": "1.0",
  "request_id": "req-7f3a",
  "as_of": "2026-10-06T10:05:00Z",
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "The object changed; reload and retry",
    "retryable": false,
    "retry_after_seconds": null,
    "correlation_id": "req-7f3a",
    "details": {"current_version": 4}
  }
}
```

MCP tools return the same `ToolError` payload in an `isError: true` result (HTTP 200 on the MCP
transport); protocol problems (unknown tool, malformed JSON-RPC) are JSON-RPC errors instead.
Messages and details are safe: no SQL, tokens, cookies, credential-bearing URLs or stack traces.
`retry_after_seconds` accompanies `RATE_LIMITED` when known (`Retry-After` header too); it is
clamped to 0-86,400 seconds. Validation failures list the failing field names in
`details.fields` (including fields that break a domain rule, such as duplicate reason codes),
never the submitted values. Rendering an error never fails: a malformed correlation id is
omitted, and a malformed request id is replaced by a server-generated one. Three transport
statuses keep the code `VALIDATION_ERROR` in the body: `405` (method not allowed, with `Allow`),
`413` (body too large, `details.limit_bytes`) and `415` (not `application/json`). Unknown `/api`
paths are `404 NOT_FOUND` whatever the method; a known path (including one served by an
extension router) with another method is `405` with that route's `Allow` methods. A `401` carries
`WWW-Authenticate: Bearer realm="suv-deals"` (plus `error="invalid_token"` when a token was
presented); every invalid token gets the same message.

| Code | HTTP status | Retryable by default | Meaning |
|---|---|---|---|
| `VALIDATION_ERROR` | 422 | no | Invalid input, unknown field, bad/expired/altered cursor (`details.cursor`), failed business rule |
| `UNAUTHENTICATED` | 401 | no | Missing, invalid or expired bearer token |
| `FORBIDDEN` | 403 | no | No active membership, or the route's scope is missing |
| `NOT_FOUND` | 404 | no | Missing or foreign-workspace object (indistinguishable) |
| `VERSION_CONFLICT` | 409 | no | `expected_version`/revision/valuation is stale; reload |
| `ALREADY_CLAIMED` | 409 | no | Another reviewer holds an active claim |
| `CLAIM_EXPIRED` | 409 | no | The caller's claim expired or the token is not current |
| `IDEMPOTENCY_CONFLICT` | 409 | no | Same `idempotency_key` reused with a different request |
| `SOURCE_PAUSED` | 409 | no | The source is paused or not enabled |
| `ACCESS_BLOCKED` | 409 | no | The source reported an access block; the path is stopped |
| `RATE_LIMITED` | 429 | yes | Too many requests or source budget exhausted |
| `INSUFFICIENT_DATA` | 422 | no | Not enough data to answer (e.g. insufficient comparables) |
| `DEPENDENCY_UNAVAILABLE` | 503 | yes | Database or another dependency is unavailable |
| `INTERNAL_ERROR` | 500 | yes | Unexpected failure (details logged server-side only) |
| `EMAIL_DELIVERY_UNCERTAIN` | 409 | no | A seller e-mail send attempt may have reached the provider; it is held for reconciliation with positive evidence and never resent blindly (spec 37.5) |

## 4. Pagination

- No offset pagination. `limit` is 1-100 (default 25). `next_cursor` in the envelope is `null` on
  the last page; pass it back as `cursor` with the **same filters**.
- Cursors are opaque, HMAC-signed (`domain.pagination`), at most 2,048 characters, short-lived
  (30 minutes by default, never more than one day or beyond their snapshot) and bound to the
  query, workspace, principal and filter hash. An altered,
  expired, foreign or filter-mismatched cursor is `422 VALIDATION_ERROR` with `details.cursor`
  (`malformed`, `tampered`, `expired` or `mismatch`); restart from the first page.
- **Candidates and outbox** use keyset cursors over a stable sort ending in the unique id.
- **Review queue** pages come from a frozen `ops.query_snapshots` projection (membership and
  order fixed when the first page is requested; snapshot expiry is `snapshot_expires_at`). A
  projection is not the current state: claim and submit always revalidate current versions.
  Re-query to see new or reprioritised cases.
- **Comparable members** are paginated by their stable ordinal (selected first, then excluded).

## 5. Idempotency and optimistic concurrency

- Every mutation body has `idempotency_key` (8-128 characters of `A-Z a-z 0-9 . _ : -`), scoped by
  authenticated principal and operation. The same key with the same canonical request returns
  the original result; with a different request it is `409 IDEMPOTENCY_CONFLICT`. A replayed
  claim returns `claim_token: null, claim_token_redacted: true` (the token is shown only once).
- `expected_version` is the case `case_version` (review routes) or the source `version`
  (pause). A stale value is `409 VERSION_CONFLICT`. Decisions also cite `listing_revision` and,
  when relevant, `valuation_id`; anything newer than the citation is a conflict.
- Request bodies never contain actor fields; the actor is the authenticated principal.

## 6. Routes

Mutation bodies are the MCP tool input models without the id that the path supplies
(`<Body>.to_tool_input(path_id)` produces the exact tool input). Query models are the
string-parsing counterparts of the MCP list inputs and convert with `to_tool_input()`.
Path ids must be canonical UUID strings (`422` otherwise). Every authenticated route can also
return `UNAUTHENTICATED`, `FORBIDDEN`, `VALIDATION_ERROR`, `RATE_LIMITED`,
`DEPENDENCY_UNAVAILABLE` and `INTERNAL_ERROR`; the "Extra errors" column lists the rest.

| Method and path | Scope | Request | Response `data` | Success | Extra errors | MCP tool |
|---|---|---|---|---|---|---|
| `GET /healthz` | none (public) | - | `LivenessView` (not enveloped) | 200 | - | - |
| `GET /readyz` | none (public) | - | `ReadinessView` (not enveloped) | 200 / 503 | - | - |
| `GET /api/me` | any active membership | - | `MeView` | 200 | - | - |
| `GET /api/overview` | `deals:read` | - | `OverviewView` | 200 | - | - |
| `GET /api/candidates` | `deals:read` | query `CandidateListQuery` | `CandidateListView` | 200 | - | `deals_list_candidates` |
| `GET /api/candidates/{listing_id}` | `deals:read` | query `CandidateDetailQuery` | `CandidateDetail` | 200 | `NOT_FOUND` | `deals_get_candidate` |
| `GET /api/comparables/{set_id}` | `deals:read` | query `ComparablesQuery` | `ComparableSetView` | 200 | `NOT_FOUND` | `deals_get_comparables` |
| `GET /api/valuations/{valuation_id}` | `deals:read` | - | `ValuationView` | 200 | `NOT_FOUND` | `deals_get_valuation` |
| `GET /api/reviews` | `reviews:read` | query `ReviewQueueQuery` | `ReviewQueuePage` | 200 | - | `reviews_list_pending` |
| `GET /api/reviews/{case_id}` | `reviews:read` | - | `ReviewCaseView` | 200 | `NOT_FOUND` | - |
| `POST /api/reviews/{case_id}/claim` | `reviews:write` | body `ClaimRequest` | `ClaimResult` | 200 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT`, `ALREADY_CLAIMED` | `reviews_claim` |
| `POST /api/reviews/{case_id}/release` | `reviews:write` | body `ReleaseRequest` | `ReleaseResultView` | 200 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `CLAIM_EXPIRED` | `reviews_release` |
| `POST /api/reviews/{case_id}/submit` | `reviews:write` | body `SubmitReviewRequest` | `ReviewDecisionView` | 201 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT`, `ALREADY_CLAIMED`, `CLAIM_EXPIRED` | `reviews_submit` |
| `POST /api/listings/{listing_id}/notes` | `notes:write` | body `AddNoteRequest` | `NoteView` | 201 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT` | `deals_add_note` |
| `POST /api/listings/{listing_id}/recheck` | `rechecks:request` | body `RecheckRequest` | `RecheckRequestResult` | 202 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `SOURCE_PAUSED`, `ACCESS_BLOCKED` | `deals_request_recheck` |
| `GET /api/sources` | `deals:read` | - | `SourceListView` | 200 | - | - |
| `POST /api/sources/{source_id}/pause` | `sources:pause` | body `PauseSourceRequest` | `SourcePauseResult` | 200 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT` | `sources_pause` |
| `GET /api/settings` | `deals:read` | - | `SettingsView` | 200 | - | - |
| `GET /api/outbox` | `reviews:read` | query `OutboxQuery` | `OutboxPage` | 200 | - | - |

Route notes:

- **`/healthz`** is process liveness only and never touches dependencies. **`/readyz`** checks the
  database, schema compatibility and critical configuration and returns 503 with
  `ready: false` when any check fails (the result is cached for about a second, and concurrent
  probes share one database check). Both also answer
  `HEAD` for load-balancer probes. Neither is authenticated, so neither returns versions of
  dependencies, hostnames, URLs or secrets. Source health is not part of readiness; it is in
  `deals_health` and `/api/overview` (authenticated). Free text in readiness details, gate notes
  and coverage-gap reasons that looks like a credential (credential URLs, connection strings,
  bearer tokens, `token=`-style parameters, `password:`-style pairs) is replaced by
  `[redacted: text resembled a credential]` instead of failing the response.
- **`/api/me`** lists the principal's memberships (bootstrap before a workspace is chosen) and the
  scopes of the selected workspace's role.
- **`/api/overview`** shows running/paused sources, the last successful scan, coverage gaps
  (never scanned, incomplete, budget-limited, blocked, parser-unhealthy, paused), pending review
  counts per queue, failed deliveries and activation blockers.
- **`/api/candidates`** filters: `profile` (`primary`, `manual_4000`, `below_target_watch`),
  `country` (seller country, `^[A-Z]{2}$`), `status` (`pending`, `needs_information`, `watch`,
  `shortlisted`, `rejected`), `changed_since` (RFC 3339 `date-time` with an offset, e.g.
  `2026-10-06T10:00:00Z`; epoch numbers, a space separator or `+0200` offsets are refused),
  plus `cursor`/`limit`.
- **`/api/candidates/{listing_id}`** returns the current revision unless `revision` is given; an
  older revision adds a `REVISION_NOT_CURRENT` warning. It includes provenance (extraction
  confidence, not truth; the source URL only when it is a safe http(s) link), conflicts, availability and price history, screening reasons, the
  latest valuation and comparable references, the due-diligence checklist and notes.
- **`/api/comparables/{set_id}`**: `include_excluded` (default `false`) adds excluded evidence with
  reasons. Asking-price, seller-reported-sale and verified-sale statistics are separate fields.
  Each member carries its duplicate cluster (`duplicate_cluster_id`) when the observation is
  loaded.
- **`/api/valuations/{valuation_id}`**: scenario lines with statuses, totals only when complete,
  `known_subtotal` otherwise, threshold `PROPOSED`/`APPROVED`, dependency fingerprint, versions,
  expiry and the fixture label. `threshold.alert_eligible` is never true unless the valuation
  itself is `alert_eligible` (a stale or fixture valuation is never alert eligible).
- **`/api/reviews`**: `include_needs_information` (default `true`). Items expose the claim state
  (`claimed`, `held_by_caller`, `expires_at`) but never a token or another reviewer's identity.
- **Claim** returns the claim token once with its expiry, the case version and the exact revision
  ids. **Release** is idempotent and only affects the caller's claim (`not_held`/`not_claimed` are
  no-ops). **Submit** checks claim ownership, expiry, case version, listing revision and valuation
  applicability in one transaction; outcomes are `needs_information`, `watch`, `shortlisted` and
  `rejected`, never a purchase or a seller contact. The spec 19 dashboard actions "needs
  inspection", "needs documents" and "price confirmation needed" are submitted here as a
  `needs_information` decision whose `reason_codes` include `needs_inspection`, `needs_documents`
  or `price_confirmation_needed` (`domain.due_diligence.DashboardAction`) and whose
  `missing_information` lists the open items (`api.routes.dashboard_action_request` builds the
  body); those reason codes with any other outcome are `422` (`details.fields =
  ["outcome", "reason_codes"]`). An optional `Idempotency-Key` header must equal the body's
  `idempotency_key` on every mutation.
- **Notes** are private, labelled (`owner`, `reviewer`, `assistant`) and separate from extracted
  claims. **Recheck** queues a budget-controlled job for a registered listing (never an arbitrary
  URL) and returns `202` with the job id.
- **Sources** show the terms status/decision separately from the technical status (a terms
  decision is an audit record, not permission), parser health, robots handling, the rate budget,
  recent runs, pause state and `version`. **Pause** never resumes or enables a source.
- **Settings** are read-only here: profiles (the optional EUR 4,000 manual profile is labelled
  `DISABLED - optional manual-review profile ...` while disabled), the MK band, the contribution
  threshold (`PROPOSED` until approved), the re-alert policy, destination bindings (external ids
  only, never secrets) and gate states.
- **Outbox** lists only deliveries needing attention (`uncertain`, `blocked`, `dead_letter`,
  `retry_wait`, filter `state`), never payloads. `owner_seen_at` is `null` (unknown) unless the
  channel provides trustworthy read evidence.

## 7. MCP tools

The MCP endpoint exposes exactly the tools in `mcp.schemas.TOOLS` (spec §21). There is no
`buy_vehicle`, `send_seller_message`, `create_payment`, `approve_tax_rules`, `execute_sql` or
`crawl_url` tool. `tools/list` hides tools whose scope the credential lacks
(`tools_for_scopes`), and every call re-checks the scope (`require_tool_scope`).

| Tool | Scope | Annotations (read-only / destructive / idempotent / open-world) | Result `data` |
|---|---|---|---|
| `deals_health` | `deals:read` | yes / no / yes / no | `HealthView` |
| `deals_list_candidates` | `deals:read` | yes / no / yes / no | `CandidateListView` |
| `deals_get_candidate` | `deals:read` | yes / no / yes / no | `CandidateDetail` |
| `deals_get_comparables` | `deals:read` | yes / no / yes / no | `ComparableSetView` |
| `deals_get_valuation` | `deals:read` | yes / no / yes / no | `ValuationView` |
| `reviews_list_pending` | `reviews:read` | yes / no / yes / no | `ReviewQueuePage` |
| `reviews_claim` | `reviews:write` | no / no / yes / no | `ClaimResult` |
| `reviews_release` | `reviews:write` | no / no / yes / no | `ReleaseResultView` |
| `reviews_submit` | `reviews:write` | no / no / yes / no | `ReviewDecisionView` |
| `deals_request_recheck` | `rechecks:request` | no / no / yes / yes | `RecheckRequestResult` |
| `deals_add_note` | `notes:write` | no / no / yes / no | `NoteView` |
| `sources_pause` | `sources:pause` | no / no / yes / no | `SourcePauseResult` |

Mutations are idempotent through `idempotency_key`. `deals_request_recheck` is open-world because
the queued job later contacts the registered source (within its budget and access decision).
The three spec 37.8 seller-inquiry tools live in the separate `mcp.schemas.V11_TOOLS` registry
(section 10.4); they are not in `TOOLS` and are not served until the inquiry package registers
their handlers through `build_mcp(extra_tools=...)`.

Input schemas (`tool_input_schema`) are the spec §21 schema map, resolved: no `$ref`,
`additionalProperties: false` on every object, JSON Schema 2020-12. They add only narrowing
keywords the domain already enforces (character patterns for idempotency keys, claim tokens and
reason codes; `uniqueItems`) and descriptions. Validation is strict: JSON integers and booleans
only, canonical UUID strings, timestamps with a timezone, `null` refused for optional filters the
spec does not make nullable, and control/bidi-override characters refused in free text.
`validate_tool_input` reports failing field names only, never the submitted values (claim tokens
can therefore never be echoed). Output schemas (`tool_output_schema`) are the resolved
`ResponseEnvelope[<view>]` schemas. Results should carry `structuredContent` plus the compact
text from `ResponseEnvelope.to_text()`.

## 8. Events

- `schemas/event.schema.json` is the **internal** `review.pending` outbox payload
  (`views.reviews.ReviewPendingEventPayload`, built by
  `domain.notifications.build_review_pending_event`): case id/version, listing id/revision,
  priority, authenticated dashboard URL (no tokens), a bounded summary, the deduplication key
  `review.pending:<case_id>:<case_version>`, readiness, profile, optional queue and, only for
  synthetic fixtures, `fixture: true` (never routed externally).
- Native MCP Events subscribers receive `review.pending.v1` occurrences
  (`views.reviews.ReviewPendingOccurrence`, built by `integrations.event_bridge.build_occurrence`)
  whose `data` holds only the six reference fields published in `events/list`
  (`event_bridge.payload_schema()`); `cursor` is always `null` (no replay in the MVP; the
  pending-review tool is the catch-up path).

## 9. Schema exports

`uv run python scripts/export_schemas.py` regenerates `schemas/listing.schema.json`
(`ListingRevisionDocument`), `schemas/review.schema.json` (`ReviewCaseView`),
`schemas/valuation.schema.json` (`ValuationView`), `schemas/event.schema.json` and
`schemas/tools/<tool>.json` (MCP `Tool` object with `inputSchema`, `outputSchema`,
`annotations`, `requiredScope`, `paginated`, `idempotencyOperation` and `errorSchema`; the twelve
`TOOLS` plus the three prepared `V11_TOOLS`) and `schemas/api/<route>.json` (one file per spec 37
route of section 10: `method`, `path`, `auth`, `requiredScope`, `requestLocation`, the `request`
validation schema, the `response` serialization schema, `successStatus`, `errors`, `paginated`,
`mcpTool`, `summary` and `idempotencyKeyHeader` (`required`, `optional` or `null`); the file
name is the path without `/v1`/`/api`, dots for slashes, plus
the lower-case method, e.g. `mail-workers.send-intents.intent_id.claim.post.json`).
`--check` verifies the snapshots without writing. `tests/contracts/test_schema_snapshots.py`
fails when a model change is not re-exported. Output documents use serialization schemas (what
the server emits); the event schema and tool inputs use validation schemas (what a producer or
client must send).

## 10. Spec v1.1 seller-inquiry contracts (spec §37)

The models and route tables below are the contract, and the routes and tools are served by default
(`api.app.create_app` includes `api.mail_worker_routes.router` and `api.inquiry_routes.router`;
`mcp.tools.ToolRegistry.default` serves `V11_TOOLS` after the twelve spec 21 tools). Owner decisions
in force: one automatic initial inquiry per verified vehicle/seller pair, no per-message approval,
hard caps of 2 inquiries per rolling 24 hours and 5 per rolling 15 days
(`SELLER_INQUIRY_MAX_PER_24H` / `SELLER_INQUIRY_MAX_PER_ROLLING_15D` can only lower them), default
send path `outlook_local`, optional `gmail_api`. Nothing is sent unless the mode is `automatic`,
the kill switch is off, the standing authorization is active and the sender binding is verified.
No route or tool accepts a recipient, an e-mail body or a sender account; the pipeline sends from
validated domain records.

### 10.1 Mailbox-worker API (`/v1/mail-workers`)

Used only by the Windows desktop worker (`desktop/outlook-bridge`). Table:
`api.schemas.MAIL_WORKER_ROUTES`; handlers: `api.mail_worker_routes`.

- **Authentication**: `Authorization: Bearer suvmail_<64 hex>` (exactly one header), a revocable,
  narrow `mail:ingest` credential bound to one workspace and one mailbox binding
  (`ops.mail_worker_bindings`), kept in the operating system's protected credential store after
  activation; issue it with `suv-deals mail-worker credential issue`. It is never a dashboard user
  token, an MCP credential, a Supabase service-role key or a database credential: any other token
  kind is `401` by format alone (no database lookup). The identity is resolved in a transaction
  opened without a workspace (the credential alone names workspace and mailbox). A revoked or
  expired credential is `401` with `details.reason = "mail_worker_credential_revoked"` (the
  worker stops transmitting and keeps its backlog); an unknown token is `401`; a revoked mailbox
  binding is `403` (`mailbox_binding_revoked`). Tokens are never accepted in URLs: every route
  refuses unknown query parameters (`422`).
- **Rate limits**: per credential (keyed by the token's SHA-256 before the database lookup):
  mutations 120 in a burst then 2 per second, reads 60 in a burst then 1 per second; `429` with
  `Retry-After`. Failed authentications also draw on the client's pre-auth budget (section 1).
- **Server-derived scope**: the server derives workspace and mailbox from the credential only.
  No request selects a workspace, mailbox or account; ids in a body (`mailbox_binding_id`,
  `inquiry_id`, `intent_id`) are checked against the credential's mailbox and a mismatch (or an
  unknown inquiry/intent) is `403 FORBIDDEN` (`details.reason = "mailbox_binding_mismatch"`),
  never a silent reassignment and never an existence leak.
- **Idempotency**: `POST /replies`, `POST /send-intents/{intent_id}/claim` and
  `POST /send-intents/{intent_id}/report` REQUIRE an `Idempotency-Key` header (8-128 characters of
  `A-Z a-z 0-9 . _ : -`; missing -> `400`, malformed or repeated -> `422`). `POST /heartbeat` and
  `POST /account-report` carry none (latest-state reports). The rule is
  `ApiRoute.idempotency_header` (`idempotencyKeyHeader` in `schemas/api/*.json`). For
  `POST /replies` the server checks the key **and** the stable source identity
  (`ReplyIngestRequest.dedup_key`: internet message id, provider id or local store/entry
  locator) and never trusts one alone: the same key/message with the same immutable source
  content returns the existing `reply_id` with `duplicate: true` (also after a folder move; the
  changed locator goes to locator history), while different content under the same identity is
  `409 IDEMPOTENCY_CONFLICT` and is quarantined, never overwritten. The claim key
  (`claim-<intent>-<claim_attempt_id>`) is required but **never stored or replayed**: every claim
  is evaluated fresh, so an earlier `proceed: true` can never come back. The report key
  (`report-<intent>-<state>`) is stored with the request hash (`persistence.idempotency`,
  operation `mail_worker.send_report`): the same key and report replay the acknowledgement,
  another report under the same key is `409 IDEMPOTENCY_CONFLICT`. The `{intent_id}` path segment
  must equal the body's `intent_id` (`422 VALIDATION_ERROR` otherwise).
- **Bodies and responses** are top-level JSON objects with `schema_version: "1.0"` (no
  `ResponseEnvelope`), except the account-report body, which carries no `schema_version` (exactly
  like the desktop `WorkerAccountReport`; a server must not require one); they are closed
  (unknown fields are `422`), and are the desktop wire models exactly;
  `tests/contracts/test_mail_worker_contract.py` compares every pair field by field and
  `tests/integration/mail_worker_e2e` drives the real desktop worker against the real app and
  PostgreSQL.
- **Limits**: every request body <= 128 KiB (`MAIL_WORKER_BODY_LIMIT`, applied by default to the
  `/v1/mail-workers` prefix, `413` above it); `sanitized_body_text` <= 64 KiB, `subject` <= 512
  characters, <= 20 attachment metadata entries (safe filename, MIME type, byte count, SHA-256,
  opaque local reference; no URLs, paths or bytes); binding pages <= 100 items, send-intent
  batches <= 50.
- **Errors**: `ApiErrorResponse` bodies with typed codes: `400 VALIDATION_ERROR` (missing
  `Idempotency-Key`), `422 VALIDATION_ERROR`, `401 UNAUTHENTICATED`, `403 FORBIDDEN`,
  `409` (`VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`), `413`, `429 RATE_LIMITED` (with
  `Retry-After`) and `503 DEPENDENCY_UNAVAILABLE`. Database guard refusals carry a stable
  `details.reason` (section 10.5). The worker keeps its local queue on `429`/`5xx`/transport
  failures. Unknown paths under `/v1/mail-workers` are `404` API errors (never the MCP mount).

| Route | Request | Response | Success | Extra errors |
|---|---|---|---|---|
| `GET /v1/mail-workers/inquiry-bindings` | query `MailWorkerBindingsQuery` (`cursor`, `limit` 1-100) | `MailWorkerBindingPage` of `MailWorkerBindingItem` | 200 | - |
| `POST /v1/mail-workers/replies` | body `MailWorkerReplyRequest` | `MailWorkerReplyAck` | 200 | `VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT` |
| `GET /v1/mail-workers/send-intents` | query `MailWorkerSendIntentsQuery` (`limit` 1-50) | `MailWorkerSendIntentBatch` of `MailWorkerSendIntent` | 200 | - |
| `POST /v1/mail-workers/send-intents/{intent_id}/claim` | body `MailWorkerClaimRequest` | `MailWorkerClaimDecision` | 200 | `NOT_FOUND`, `VERSION_CONFLICT` |
| `POST /v1/mail-workers/send-intents/{intent_id}/report` | body `MailWorkerSendReport` | `MailWorkerAccepted` | 200 | `NOT_FOUND`, `VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT` |
| `POST /v1/mail-workers/heartbeat` | body `MailWorkerHeartbeatRequest` (`MailWorkerCheckpointReport`, `MailWorkerGapReport`) | `MailWorkerHeartbeatAck` | 200 | - |
| `POST /v1/mail-workers/account-report` | body `MailWorkerAccountReport` | `MailWorkerAccepted` | 200 | `VERSION_CONFLICT` |

Every route also lists `UNAUTHENTICATED`, `FORBIDDEN`, `VALIDATION_ERROR`, `RATE_LIMITED`,
`DEPENDENCY_UNAVAILABLE` and `INTERNAL_ERROR`. Route notes:

- **Bindings** return changes of the worker's own mailbox, including uncertain sends with their
  send-intent message ids and tombstones/revocations (a tombstone carries identity, version and
  state only). Besides the generated Message-IDs, a binding publishes every Message-ID the
  provider or the worker OBSERVED on a sent copy (`observed_rfc_message_id` of an accepted
  attempt, `observed_internet_message_id` of a worker report): under `outbound_message_ids` for an
  accepted attempt, else under `send_intent_message_ids`, so a reply quoting a rewritten header
  still correlates. `next_cursor` is an opaque signed position returned only after a complete
  page; the worker persists page and cursor atomically.
- **Replies** are stored only for a valid, published, non-tombstoned binding of the worker's
  mailbox whose references corroborate the message; the backend inserts reply, ingest-dedup
  record and processing event atomically (and the minimal `seller.reply.received.v1` outbox
  signal for a matched seller reply), and only then may the worker advance its acknowledged
  checkpoint. A bounce/delivery notice may carry `returned_message_ids` (<= 20, normalised,
  bounce/delivery-notice types only): the returned original's Message-IDs the worker read from the
  RAW report before its sanitiser removed the quoted original. The server uses them for
  correlation (like the domain's own parse of the report body) only when its own classification
  of the uploaded fields is a delivery report too; the stored body is the uploaded one.
- **Send intents** carry the composed message of an already authorized inquiry plus
  `kill_switch_active`. An intent with `expired: true` is one whose attempt the server reaped
  (its validity ended) although no worker ever claimed it and no report exists: the worker refuses
  it as `intent_expired` without a claim (never sends it), which proves non-submission and lets the
  backend reconcile the inquiry instead of holding it uncertain. **Claim** is a fresh server
  revalidation (kill switch, mode, suppression, cancellation, binding/authorization version,
  listing facts, recipient, cooldown, caps) immediately before `.Send` and answers
  `proceed: false` with a `refusal_reason` instead of an error for a business refusal:
  `kill_switch` (kill switch on or mode not automatic; the worker reports a retryable
  pre-submission refusal), `not_now` (rolling caps, seller cooldown or a paused source: the worker
  keeps the intent, claims again after 10 minutes while it is valid and reports `intent_expired`
  once its validity ends; nothing is reported for `not_now` itself), or a final reason
  (`intent_invalid`, `intent_expired`, `binding_mismatch`). **Report** records the submission
  evidence, and an uncertain outcome stays uncertain (`EMAIL_DELIVERY_UNCERTAIN` is never
  converted into a resend).
- **Heartbeat** records worker/Outlook/mailbox health, hashed store/folder checkpoints, backlog
  and coverage gaps (never claimed coverage; a gap that ends before it starts is `422`); the
  acknowledgement carries downstream health as short codes. **Account report** is the
  classic-Outlook account verification (no credentials; `security_settings_unchanged` is
  `Literal[true]`, a report claiming weakened security is `422`); a refused report is recorded
  first, then answered `409 VERSION_CONFLICT` (`mail_worker_account_mismatch`).

Where the desktop wire contract (`outlook_bridge/wire.py`, `outlook_bridge/api_client.py`) and
the spec 37.8 prose differ, the wire contract wins:

1. `MailWorkerReplyAck.ingest_status` is `stored` **or `quarantined`** (spec names `stored`
   only); a quarantined reply is acknowledged so the worker does not resend it.
2. The reply body accepts the worker's optional extensions `message_type`,
   `correlation_status`, `correlation_reasons`, `withheld_sensitive_attachments` and
   `returned_message_ids` (omitted when default, so a matched seller reply is exactly the spec
   v1.0 shape); `detected_language` is `de`, `it`, `fr`, `en` or `null`.
3. Binding items carry `state` (`active`, `suppressed`, `uncertain`, `tombstoned`) instead of an
   active/suppressed flag, and a binding page carries `has_more`; the desktop client accepts up
   to 1000 items per page while the server sends at most `limit` (<= 100).
4. Send intents, claims, reports, heartbeats and account reports are not described in the spec
   JSON; their shapes are the wire models (`intent_id`, `claim_attempt_id` (32 lower-case hex),
   `mailbox_binding_id`, `worker_id` in the claim body; length limits 254/998/64,
   `inquiry_ref == "inquiry-<inquiry_id>"` (checked on `OutlookSendIntent` itself) and the
   listing-only `expired` flag on intents; the `not_now` refusal reason).
5. The server models are closed (`extra="forbid"`) while the desktop models ignore unknown
   fields, so the backend can never send or accept a field the worker does not know.

### 10.2 Dashboard inquiry routes

User-JWT routes like section 6, enveloped, with the same error conventions. Table:
`api.schemas.V11_DASHBOARD_ROUTES`; handlers: `api.inquiry_routes`.

| Route | Scope | Request | Response `data` | Success | Extra errors | MCP tool |
|---|---|---|---|---|---|---|
| `GET /api/inquiries` | `inquiries:read` | query `InquiryListQuery` (`cursor`, `limit`, `state`, `uncertain_only`, `attention_only`) | `InquiryListView` of `InquirySummaryView` | 200 | - | - |
| `GET /api/inquiries/{inquiry_id}` | `inquiries:read` | - | `InquiryView` | 200 | `NOT_FOUND` | `seller_inquiries_get` |
| `GET /api/replies` | `inquiries:read` | query `ReplyListQuery` (`cursor`, `limit`, `inquiry_id`, `quarantined_only`) | `ReplyListView` of `ReplySummaryView` | 200 | - | - |
| `GET /api/replies/{reply_id}` | `inquiries:read` | - | `ReplyView` | 200 | `NOT_FOUND` | `seller_replies_get` |
| `GET /api/inquiry-control` | `inquiries:read` | - | `InquiryControlView` | 200 | - | - |
| `POST /api/inquiry-control/pause` | `inquiries:pause` | body `InquiryPauseRequest` | `InquiryPauseResult` | 200 | `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT` | `seller_inquiries_pause` |
| `POST /api/inquiry-control/resume` | `config:admin` | body `InquiryResumeRequest` | `InquiryResumeResult` | 200 | `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT` | - |
| `GET /api/mail-workers/health` | `inquiries:read` | query `MailWorkerHealthQuery` (`include_revoked`) | `MailWorkerHealthView` of `MailboxHealthView` | 200 | - | - |
| `GET /api/mail-workers/coverage-gaps` | `inquiries:read` | query `MailWorkerHealthQuery` | `MailCoverageGapListView` of `MailCoverageGapItem` | 200 | - | - |
| `GET /api/lifecycle/lags` | `deals:read` | - | `CoverageLagsView` (`SourceLagView`, `LagView`) | 200 | - | - |
| `GET /api/listings/{listing_id}/lifecycle` | `deals:read` | - | `ListingLifecycleView` | 200 | `NOT_FOUND` | - |
| `GET /api/evaluation` | `inquiries:read` (+ `deals:read`) | query `EvaluationQuery` (`days` = 15) | `EvaluationReport` | 200 | - | - |

Pause and resume take their `idempotency_key` in the body; an optional `Idempotency-Key` header must
equal it (`idempotency_header="optional"`, as on the other dashboard mutations). Pause uses the
idempotency operation `seller_inquiries_pause` (shared with the MCP tool, so a retry on either
surface replays the same result); resume uses `inquiry_control_resume`.

- Inquiry views show state, qualification, authorization/template versions, the sender as a
  provider/binding reference (never an address or account id) and send-attempt summaries;
  `approval_required` is always `false` (no per-message approval). The recipient address is shown
  only to `config:admin` holders (`views.inquiries.recipient_address_visible`); others see its
  domain and verification evidence. Lists never contain message text. `attention_only` lists
  uncertain, held, suppressed, failed and stuck-sending inquiries.
- Languages: an inquiry's `language` is one of the four template languages (`de`, `it`, `fr`,
  `en`); a recipient contact's `language` and a reply's `original_language` are any ISO 639-1
  code (`^[a-z]{2}$`, as stored), because a contact may be in an unsupported language (the
  inquiry is held, never sent in English instead) and a seller may reply in another language.
- Reply views carry inquiry/vehicle ids, the original language and sanitized body, the
  Macedonian summary, the verified sender identity, received/ingested times, extracted claims
  (a price quote is an `unaccepted_seller_quote`, never accepted), safe attachment metadata
  (no local reference) and the current valuation status. Lists never contain bodies.
- **Inquiry control** shows the kill switch, mode, owner-reducible caps, current usage and
  `removable_suppressions`: how many active `kill_switch` suppressions (and, while the current
  standing authorization is effective, `authorization_revoked` ones) a resume could remove.
- **Pause** activates the inquiry kill switch against the control `expected_version` with a
  reason (same rules as the MCP tool; pausing an already paused control answers
  `already_paused: true`). **Resume** is owner-only and dashboard-only; it is never an MCP tool.
  With `remove_suppressions: true` it also removes those suppressions, one audited removal each
  (`suppressions_removed` in the result); every other suppression (opt-out, bounce, complaint,
  sender revoked, unresolved send, manual) needs its own explicit owner decision.
- **Mail-worker health** reports each mailbox worker's separate dimensions (heartbeat, Outlook,
  mailbox sync lag, last reconciliation, backlog age, unresolved matching gaps, account
  verification) and its coverage gaps; monitoring is reported only while all of them are fresh.
  Store/folder identities are hashes; no address, subject or body appears.
- **Lifecycle/lags** show separate lags (source scan, detail freshness, detection delay,
  notification processing, mail-reply detection); `unknown` and `inconsistent` carry no value,
  never zero, and a configured interval is context only.
- **Evaluation** is the 15-day quality report from stored evidence (`domain.evaluation`); zero
  suitable deals is reported as zero, and the contribution threshold is used only when
  `CONTRIBUTION_THRESHOLD_APPROVED=true` (otherwise a `THRESHOLD_PROPOSED` warning).
- Review decisions (`ReviewDecisionView`, in `GET /api/reviews/{case_id}` and the submit result,
  and the MCP `reviews_submit` result) carry `decided_by_caller`: whether the authenticated caller
  recorded that decision. The review claim lease is `REVIEW_CLAIM_DURATION_SECONDS` (section 10.3).

### 10.3 Configuration

`SELLER_INQUIRY_MAX_PER_24H` (0-2) and `SELLER_INQUIRY_MAX_PER_ROLLING_15D` (0-5) can only lower
the owner caps. `API_ALLOWED_HOSTS` (comma separated host names, no wildcard) extends the API
host check; `METRICS_ENABLED`/`METRICS_BIND` (default off, `127.0.0.1:9464`) start a private
Prometheus listener separate from the API port; `DATABASE_POOL_TIMEOUT_S` (default 5) bounds the
wait for a pooled connection (`503 DEPENDENCY_UNAVAILABLE` after it).
`REVIEW_CLAIM_DURATION_SECONDS` (60-3600, default 300) is the review claim lease of the dashboard
and the MCP `reviews_claim` tool. `MAIL_RECONCILE_INTERVAL_SECONDS` (default 120) is the expected
worker reconciliation interval used by the health view (context only, never a latency claim).
`/readyz` shows each failing check's name and status with a generic detail only; the specific
reason is logged server-side.

### 10.4 MCP tools (`V11_TOOLS`)

Served by default after the twelve spec 21 tools; `tools/list` shows each only to a caller holding
its scope (reviewers see the two read tools; `seller_inquiries_pause` needs `inquiries:pause`).

| Tool | Scope | Annotations (read-only / destructive / idempotent / open-world) | Result `data` |
|---|---|---|---|
| `seller_inquiries_get` | `inquiries:read` | yes / no / yes / no | `InquiryView` |
| `seller_replies_get` | `inquiries:read` | yes / no / yes / no | `ReplyView` |
| `seller_inquiries_pause` | `inquiries:pause` | no / no / yes / no | `InquiryPauseResult` |

Input schemas are exactly the spec 37.8 JSON (`seller_inquiries_get`: `inquiry_id`;
`seller_replies_get`: `reply_id`; `seller_inquiries_pause`: `expected_version` >= 1, `reason`
3-2000 characters, `idempotency_key` 8-128 characters), plus the printable-character `pattern`
every idempotency key already has. The workspace comes from the token. Results return only the
caller's workspace records and never a secret, an unrelated thread message or a signed external
access credential. There is no send, reply or resume tool.

### 10.5 Database guard refusals

The seller-inquiry migration refuses unsafe writes in triggers (`SV002` immediately, `SV003` at
statement end or at `COMMIT` for deferred checks). `persistence.errors_map.map_db_error` turns
each refusal into a typed error with a stable machine reason in `details.reason` (plus safe
extras such as `phase`, `window`, `limit` or `evidence`), and `Database.transaction` raises the
typed error also when the refusal happens at `COMMIT`. Messages stay generic; no row data is
returned.

| Code | `details.reason` values |
|---|---|
| `VERSION_CONFLICT` | `inquiry_kill_switch`, `inquiry_mode_not_automatic` (+`mode`), `inquiry_controls_missing`, `inquiry_vehicle_seller_conflict` (one inquiry per vehicle/seller pair), `inquiry_suppressed` (+`suppressions`), `inquiry_listing_stale`, `inquiry_availability_stale`, `inquiry_qualification_mismatch`, `inquiry_listing_quarantined`, `inquiry_vehicle_unavailable`, `inquiry_profile_out_of_scope`, `inquiry_authorization_changed`, `inquiry_authorization_not_effective`, `inquiry_language_not_authorized`, `inquiry_language_unresolved`, `recipient_not_dealer`, `recipient_unverified`, `recipient_changed`, `sender_binding_not_ready`, `sender_binding_changed`, `sender_binding_mismatch`, `binding_version_unpublished`, `inquiry_binding_tombstoned` (re-publish), `inquiry_transition_not_permitted` (+`from_state`, `to_state`), `inquiry_initial_state_invalid`, `inquiry_requalification_refused`, `inquiry_requalification_audit_missing`, `inquiry_quota_debit_missing`, `quota_debit_invalid`, `quota_release_refused`, `send_intent_invalid`, `send_attempts_exhausted`, `message_id_mismatch`, `inquiry_identity_not_canonical`, `inquiry_not_transmitted`, `seller_entity_merged`, `seller_contact_superseded`, `reply_release_invalid`, `reply_quarantine_release_refused` |
| `RATE_LIMITED` | `inquiry_cap_reached` (+`phase` `reserve`/`dispatch`, `window` `24h`/`15d`, `limit`), `seller_cooldown` (+`phase`) |
| `SOURCE_PAUSED` | `inquiry_source_paused` |
| `FORBIDDEN` | `inquiry_authorization_revoked`, `sender_binding_revoked`, `mailbox_binding_revoked`, `mailbox_binding_mismatch` (cross-mailbox), `mail_worker_credential_mismatch`, `inquiry_binding_tombstoned` |
| `UNAUTHENTICATED` | `mail_worker_credential_revoked` (revoked or expired worker credential) |
| `EMAIL_DELIVERY_UNCERTAIN` | `send_attempt_unresolved`, `send_attempt_lease_expired`, `retry_without_proof` |
| `VALIDATION_ERROR` | `inquiry_evidence_missing` (+`evidence`: `quota_debit`, `quota_release`, `send_intent`, `running_attempt`, `uncertain_attempt`, `non_submission_proof`, `acceptance`, `seller_reply`), `availability_evidence_invalid`, `mail_worker_credential_invalid`, `seller_merge_invalid`, `suppression_removal_audit_missing` |

`persistence.errors_map.guard_reason(error)` returns the reason of a mapped error.
