# Dashboard API and MCP tool contract

This is the contract of the backend-for-frontend (BFF) used by the private dashboard and of the
twelve MCP tools. Both surfaces share one set of typed models:

| Module | Contents |
|---|---|
| `src/suv_deals/views/` | Read models (views), `ResponseEnvelope`, `AmountView`, `ErrorPayload`, JSON-schema helpers |
| `src/suv_deals/mcp/schemas.py` | MCP tool input models, the `TOOLS` registry, `ToolError`, resolved schemas, schema export |
| `src/suv_deals/api/schemas.py` | Dashboard request bodies/queries, `ApiErrorResponse`, the `ROUTES` table |
| `schemas/*.json`, `schemas/tools/*.json` | Generated snapshots (do not edit by hand) |

Product contract: `docs/spec/suv-deal-system-build-spec.md` (spec §20-23). Access model:
`docs/decisions/0001-bff-only-data-access.md`. The browser never reads `app`/`ops` tables; every
read and write goes through these routes.

## 1. Authentication, authorization, CSRF and CORS

**Authentication.** Every `/api/*` route requires `Authorization: Bearer <access token>`, where
the token is the signed-in user's Supabase Auth access token (a JWT). The server verifies the
signature (project JWKS), issuer, audience (`authenticated`), expiry and not-before. A missing,
malformed, expired or wrongly issued token is `401 UNAUTHENTICATED`. Tokens are never accepted
in query strings or request bodies, and the user's token is never passed through to another
service.

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
| `reviewer` | viewer + `reviews:write`, `events:subscribe`, `rechecks:request`, `notes:write` |
| `owner` | every scope, including `sources:pause` and `config:admin` |

`config:admin` is reserved for owner-only administration (profile, threshold, binding and gate
changes); those operations are not part of this route set or the MCP toolset.

**CSRF.** Authentication is the `Authorization` header only. The API sets and reads no cookies, so
a cross-site request cannot carry the user's credentials; no CSRF token is needed. Do not add
cookie authentication without adding CSRF protection.

**CORS.** Only the configured dashboard origins are allowed: `API_ALLOWED_ORIGINS`
(comma-separated), or the origin of `APP_BASE_URL` when that is empty; invalid entries, wildcards
and (in production) non-loopback `http` origins are dropped. CORS applies to `/api` paths only (the
mounted MCP endpoint validates `Origin` itself). No wildcard origins, `Access-Control-Allow-Credentials` is not sent (no cookies), allowed methods are
`GET`, `POST` and `OPTIONS`, allowed request headers are `Authorization`, `Content-Type`,
`X-Request-Id` and `X-Workspace-Id`, and preflight results may be cached for 600 seconds.

**Other headers.** Every `/api` response is `Cache-Control: no-store` and
`Content-Type: application/json`. A client may send `X-Request-Id` (printable ASCII, at most 200
characters); otherwise the server generates one. It is echoed as `request_id` and as the error
`correlation_id`. Requests are rate limited per authenticated principal (separate token buckets for
mutations and reads; in memory per process) with `429 RATE_LIMITED` and `Retry-After`. Request
bodies are `application/json` only (`415` otherwise) and at most 64 KiB (`413`). Routes without
query parameters refuse any query parameter (`422`), and repeated parameters are refused.

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
paths are `404 NOT_FOUND`. A `401` carries `WWW-Authenticate: Bearer realm="suv-deals"` (plus
`error="invalid_token"` when a token was presented); every invalid token gets the same message.

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
  `ready: false` when any check fails. Neither is authenticated, so neither returns versions of
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
`annotations`, `requiredScope`, `paginated`, `idempotencyOperation` and `errorSchema`).
`--check` verifies the snapshots without writing. `tests/contracts/test_schema_snapshots.py`
fails when a model change is not re-exported. Output documents use serialization schemas (what
the server emits); the event schema and tool inputs use validation schemas (what a producer or
client must send).
