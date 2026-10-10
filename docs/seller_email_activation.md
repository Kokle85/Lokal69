# Seller email: one-time technical activation

Spec v1.1 sections 37.1, 37.3, 37.5, 37.8 and 37.10; ADR 0002 (including the 2026-10-06
sender-mailbox addendum). Code: `src/suv_deals/integrations/seller_email.py`,
`src/suv_deals/integrations/mime_builder.py`, `src/suv_deals/integrations/email_providers/`.

> **These are one-time technical setup steps, not message approvals.** Vasko's bounded standing
> authorization (spec 37.1) covers one automatic initial inquiry per actual vehicle/seller pair,
> asking only availability, vehicle documents (registration documents, CoC) and the seller's
> lowest/final price. Once the steps below are complete and `SELLER_INQUIRY_MODE=automatic` is
> set, qualifying inquiries are sent without any per-message, first-message or first-template
> approval. The Macedonian preview is an informational audit view. Nothing in this document is a
> recurring "may I send this email?" gate.

Status at the time of writing: code and tests are complete; **no account has been connected, no
OAuth consent has been granted, no Outlook has been inspected and no email has been sent**. Every
item marked UNVERIFIED must be checked during activation and recorded as evidence.

## 1. What the system will and will not do

| Will (automatically, after activation) | Will never |
|---|---|
| Send one plain-text inquiry per vehicle/seller pair, in the verified advertisement/seller language (DE/IT/FR; EN only with positive English evidence) | Send follow-ups, replies, offers, price acceptance, reservations, deposits, purchases |
| Use exactly the verified sender display name/address and, if configured, a verified Reply-To alias | Add CC/BCC, a second recipient, attachments, HTML, tracking or extra personal data |
| Address exactly one recipient tied to the exact listing's seller | Guess addresses, use contact forms or switch to another account after a failure |
| Stop immediately on the kill switch / `seller_inquiries_pause` | Retry an uncertain send blindly or treat a missing Sent Items entry as proof of non-sending |
| Respect the ceilings of 2 inquiries per rolling 24 h and 5 per rolling 15 days (ceilings, not targets) | Move unrelated personal mail out of the mailbox |

Each message is built by `mime_builder` with exactly these headers: `From` (verified display
name and address), `To` (one address, no display name), optional `Reply-To`, `Subject`
(RFC 2047 encoded when non-ASCII), `Date`, `Message-ID`
`<inquiry-<inquiry uuid>.<attempt>@<sender domain>>`, `X-SUV-Inquiry-Ref: inquiry-<uuid>`
(the inquiry id only, no secret), `MIME-Version`, `Content-Type: text/plain; charset="utf-8"`
and `Content-Transfer-Encoding` (quoted-printable or base64). Only the exact rendering of a
registered seller template (`seller_templates.rendering_problems`) can be built, and every
provider re-checks the final bytes and that template scope (`mime_builder.inquiry_scope_problems`)
before any I/O: free-text or edited wording, the Macedonian preview, a Reply-To equal to the
seller, extra recipients, Cc/Bcc or attachments never reach a provider. The client Message-ID is a correlation/search key; reusing it
is **not** a universal de-duplication guarantee (providers may rewrite it or accept duplicates),
so uncertain sends are reconciled, never resent on that assumption.

## 2. Runtime settings

All values live in the runtime `.env`/secret store, never in source control or chat.

| Setting | Value |
|---|---|
| `SELLER_INQUIRY_MODE` | `disabled_until_sender_ready` during setup; `automatic` only after section 6 passes; `paused` to stop |
| `SELLER_INQUIRY_KILL_SWITCH` | `false` (set `true`, or use the MCP `seller_inquiries_pause` tool, to stop all untransmitted work) |
| `SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL` | `false` (standing authorization). If the owner ever sets `true`, automatic sending is disabled (`OWNER_REQUIRES_MESSAGE_APPROVAL`); no approval queue exists |
| `SELLER_INQUIRY_MAX_PER_24H` / `SELLER_INQUIRY_MAX_PER_ROLLING_15D` | `2` / `5` or lower; higher values are refused |
| `SELLER_EMAIL_PROVIDER` | `outlook_local` (default route, ADR 0002 addendum), `gmail_api` or `microsoft_graph` |
| `SELLER_EMAIL_ACCOUNT_ID` | stable account id: Outlook worker account key, the Gmail account address, or the Graph user `id` (GUID) |
| `SELLER_EMAIL_FROM` | the verified From address (the account's primary address or a provider-verified alias) |
| `SELLER_EMAIL_REPLY_TO` | optional; must be a verified alias of the **same** account so replies reach the watched mailbox |
| `SELLER_EMAIL_OAUTH_SECRET_REFERENCE` | API providers only: a *reference* such as `secretbox:ops.email_sender_bindings/<id>`; values that look like tokens (`ya29.`, `1//`, JWTs, `Bearer ...`) are refused |
| `SELLER_REPLY_INGEST_MODE` | `local_classic_outlook` (chosen route) or `provider_api`; Gmail/Graph reply retrieval through the provider API runs **only** with `provider_api` (otherwise the backend never reads the mailbox through the API, so there is no second consumer) |
| `SELLER_EMAIL_CANARY_SEND_ENABLED` | `false`. Only the owner sets it `true` for the one-time canary step of section 8 and back to `false` afterwards; `suv-deals canary send` refuses without it (and without every switch above) |

`suv-deals doctor` checks, for an API provider, that `SELLER_EMAIL_OAUTH_SECRET_REFERENCE` names
the ACTIVE binding that is the configured sender (`SELLER_EMAIL_FROM`) and holds a sealed grant
(`seller_inquiry/secret_reference`: codes such as `SECRET_REFERENCE_FROM_MISMATCH`,
`SECRET_REFERENCE_BINDING_REVOKED`, `SECRET_REFERENCE_NOT_THE_CONFIGURED_BINDING`,
`SECRET_REFERENCE_NO_SEALED_GRANT`; an error in automatic mode). It never prints the reference or an
address.

`seller_email.build_sender_provider` refuses to construct a sending provider unless the mode is
`automatic`, the kill switch is off, the binding is verified (provider, stable account id, From,
display name, alias status, healthy, not revoked) and exactly matches these settings. The
`outlook_local` route builds no provider, so the runtime applies the same identity rule itself
(`workers.inquiry_handlers.configured_sender_problems`): only the binding that is exactly
`SELLER_EMAIL_PROVIDER`/`_ACCOUNT_ID`/`_FROM`/`_REPLY_TO` is ever reserved with or published to the
desktop worker (otherwise the plan records `sender_identity_not_configured` and a queued send job
is blocked `SENDER_SETUP_INCOMPLETE`; docs/runbook.md 10.5). That is a technical prerequisite,
not an approval. A live kill-switch probe must be supplied
(`KILL_SWITCH_PROBE_MISSING` otherwise); it is re-read immediately before every transmission and
fails closed. A kill-switch refusal is a proven pre-submission failure marked retryable: it stops
that transmission only, and the domain retry policy plus the dispatch preflight (which suppresses
while the switch is on) decide what happens to the inquiry - it is never failed permanently.

## 3. Route A (default): classic Outlook on the owner's Windows machine

The backend never sends itself: it stores an `OutlookSendIntent` that the desktop worker pulls and
submits with `MailItem.Send` from the configured account only.

1. Install **classic Outlook for Windows**. New Outlook does not support the Outlook Object Model
   and is refused (`NEW_OUTLOOK_UNSUPPORTED`). UNVERIFIED: installed edition/version.
2. In Outlook, add the selected sender mailbox (ADR 0002: the owner's personal Gmail) through
   Outlook's own sign-in flow. No password or token is copied anywhere else.
3. Install and start the desktop worker (`desktop/outlook-bridge`; docs/runbook.md section 10.3)
   under the signed-in interactive user with the mailbox-bound credential issued by
   `suv-deals mail-worker credential issue` (runbook 10.2); do not disable Trust Center, Object
   Model Guard or antivirus checks (`security_settings_unchanged` is `Literal[true]` on the wire: a
   report claiming otherwise is refused).
4. The worker reports the account (`OutlookAccountReport`): classic flavour, SMTP address, stable
   account key, display name. Set `SELLER_EMAIL_ACCOUNT_ID` to the reported stable key (the SMTP
   address is accepted with a warning) and `SELLER_EMAIL_FROM` to that SMTP address. Outlook
   offers no send-as alias verification here, so only the account's own address verifies. Record
   the verification with `suv-deals sender-binding verify <binding id> --reason ... --yes`: it
   checks the worker's account report (bound address, classic Outlook), a fresh heartbeat and the
   stable account key (by its SHA-256 in the audit trail) and refuses with problem codes otherwise.
5. UNVERIFIED for the account type: whether classic Outlook stores sent Gmail/IMAP messages in
   Sent Items (Gmail may save them server-side instead). Receipt reconciliation relies on the
   worker's Sent Items evidence; record what the test in section 6 shows.

Outcome semantics: until the worker reports, a send is `uncertain` (`local_worker_handoff`);
`submitted_to_outbox` is a *local submission*, still `uncertain` with `outbox_pending=yes`;
`sent_items_confirmed` is `accepted` (no provider id or SMTP receipt exists and none is invented);
a worker refusal before `.Send` is a proven pre-submission failure. While the laptop or Outlook is
offline, inquiries wait in the backend queue and the offline time is reported as a coverage gap.

Because a hand-over is `uncertain`, the inquiry leaves `uncertain` again only on evidence:
`reconcile` returns `found_sent` on Sent Items evidence, and `proven_not_submitted` only when every
stored intent of the inquiry (covering every searched Message-ID) carries a definitive
`refused_before_send` report from its own mailbox-bound worker and no report suggests `.Send` was
called or a copy sits in the Outbox. The common case is an intent that expired (default TTL 6 h)
while the laptop was off: the worker refuses it when it comes back, the inquiry becomes
`failed_definite` through reconciliation, and the guarded retry sends a *new* intent from the same
account after the dispatch preflight. A `duplicate_intent` refusal, a missing report or an
unknown intent never counts as proof. The persistence gateway therefore implements
`intents_for(inquiry_id)` besides `publish_intent`, `reports_for` and the heartbeat/account reads.

## 4. Route B (optional): Gmail API

Use only if sending must work while the laptop is off.

1. Create a Google Cloud project owned by the owner; enable the Gmail API.
2. Configure the OAuth consent screen with the **minimum scopes**:
   * `https://www.googleapis.com/auth/gmail.send` - send only;
   * `https://www.googleapis.com/auth/gmail.readonly` - account binding (`users.getProfile`),
     alias verification (`users.settings.sendAs.list`), receipt reconciliation
     (`rfc822msgid:` search, not available with `gmail.metadata`) and, only with
     `SELLER_REPLY_INGEST_MODE=provider_api`, reading correlated reply bodies.
   Do not grant `gmail.modify`, `gmail.compose` or `https://mail.google.com/` (reported as
   `SCOPES_BROADER_THAN_NEEDED`). UNVERIFIED: `gmail.readonly` is a restricted scope; check the
   publishing status requirements and the refresh-token lifetime for a personal project in
   "Testing" status before relying on it.
3. Complete the consent flow through the backend's secure server-side flow (another package);
   the refresh token is stored encrypted in `ops.email_sender_bindings`, and only its reference
   goes into `SELLER_EMAIL_OAUTH_SECRET_REFERENCE`. Tokens are never logged or shown.
4. Set `SELLER_EMAIL_ACCOUNT_ID` to the Gmail account address. The adapter always calls
   `users/<that address>/...` (never `me`) and pre-checks `getProfile` with the same token
   before every send, so a token for another account cannot send.
5. A From alias must be a send-as alias with `verificationStatus=accepted` (or the primary
   address); `pending` blocks (`FROM_ALIAS_PENDING`).

Send semantics: 2xx is accepted (Gmail `id`/`threadId` recorded only as returned; the stored
Message-ID is read back because Gmail may rewrite it); 400/401/403 (non-rate-limit)/404/413 are
definite and nothing was sent (401 is retryable after a token refresh); connection failures
before any request byte are retryable pre-submission failures; 429, rate-limit 403s, 5xx,
redirects, read timeouts, write interruptions and connection resets are `uncertain`. Revoked or
unavailable credentials (including a failing secret store) are retryable pre-submission failures,
so the preflight suppresses the inquiry while access stays revoked instead of failing it for good.
An inquiry always starts a new Gmail thread (no `threadId`, no `In-Reply-To`/`References`).

Reply retrieval (`provider_api` only) never skips a message: a history pull that stops early
(message or page budget, transient read failure) returns the id of the last fully processed
history record as the next cursor (UNVERIFIED offline that Gmail accepts a record id as
`startHistoryId`; if it ever answers 404 the window re-sync runs with an explicit gap); a window
pull (first run or after a gap) moves to the `historyId` read before listing and hands every
listed but uninspected message back in `pending_retry_locators` for `recheck_locators`; a listing
beyond the page budget is an explicit `WINDOW_TRUNCATED` gap. Messages from the owner's own
addresses are never treated as seller replies.

## 5. Route C (optional skeleton): Microsoft Graph (Outlook.com / Microsoft 365)

1. Register an application; delegated permissions `Mail.Send`, `User.Read`, `Mail.ReadBasic`
   (reconciliation) and `Mail.Read` only for `provider_api` reply ingestion; `offline_access`
   for refresh tokens. Do not grant `Mail.ReadWrite`, `Mail.Send.Shared` or `*.All`.
2. Set `SELLER_EMAIL_ACCOUNT_ID` to the mailbox user's Graph `id` (from `GET /me`).
3. Only the account's own primary address verifies as From/Reply-To (Graph offers no alias list
   for personal accounts).

UNVERIFIED offline (must be confirmed with the test in section 6): whether Exchange keeps the
client Message-ID from MIME, whether `$filter=internetMessageId eq '...'` works for the account
type, and reply-header retrieval on list queries. `sendMail` returns `202 Accepted` without any
message id; Sent Items may lag (the message first sits in Drafts), so absence is never proof.

## 6. Verification, test message and correlated test reply

Run once per sender binding (and again after any account, alias or credential change).

1. **Verify the binding** with `seller_email.build_account_verifier(...)` while the mode is still
   `disabled_until_sender_ready`; it has no send method. `SenderVerification.verified` must be
   true. Typical blocking codes: `MISSING_SCOPES`, `ACCOUNT_MISMATCH`, `FROM_NOT_VERIFIED`,
   `FROM_ALIAS_PENDING`, `REPLY_TO_NOT_VERIFIED`, `CREDENTIALS_REVOKED`, `WORKER_REPORT_STALE`,
   `NEW_OUTLOOK_UNSUPPORTED`, `WORKER_OFFLINE`. Store the result via
   `sender_status_from_verification` (binding id/version, verified time, alias status, health).
2. **Test message** (where the owner authorizes it): the activation canary of section 8 - one
   synthetic message to an **owner-controlled test address** (never a seller). It is recorded in
   `ops.inquiry_activation_canaries`, never as a seller inquiry, so it never counts as a seller
   inquiry or one of the 15-day deals.
3. **Receipt reconciliation**: run `reconcile(...)` for the canary's Message-ID and confirm
   `found_sent` (Gmail `SENT` label, Graph non-draft copy, or Outlook Sent Items evidence).
   Record whether the provider kept the client Message-ID.
4. **Correlated test reply**: reply from the owner-controlled address to the canary. Confirm the
   full route: local Outlook worker (or provider API pull) correlates it by In-Reply-To/References
   -> authenticated ingest API stores it -> minimal private Slack signal
   `seller.reply.received.v1` -> dot -> MCP `seller_replies_get` returns it. Confirm that an
   unrelated personal message sent at the same time is **not** uploaded.
5. Record the evidence (sender verification, runtime, test message, reconciliation, correlated
   reply) in the activation log. Then set `SELLER_INQUIRY_MODE=automatic`.

From that point, actual seller inquiries use the standing authorization and do not wait for
any further approval. If a credential, the classic-Outlook runtime or the verified Slack trigger
is unavailable, the remaining code/tests stay complete and the precise blocker is reported; no
monitoring or sending is claimed.

## 7. Operating and stopping

* Pause everything: the dashboard pause, the MCP `seller_inquiries_pause` tool (scope
  `inquiries:pause`), `suv-deals inquiries pause --reason ... --expected-version N --yes` or
  `SELLER_INQUIRY_KILL_SWITCH=true`. Untransmitted work stops at the next guard (reservation,
  dispatch, the desktop worker's claim right before `.Send`); reconciliation keeps running.
  Resuming is an owner action (dashboard or `suv-deals inquiries resume`), never an MCP tool.
* Revoke access: remove the OAuth grant at the provider (the next token refresh reports
  `CREDENTIALS_REVOKED`, health turns `credentials_revoked`, sending stops and pending inquiries
  are suppressed with `sender_revoked` at the next preflight) or remove the Outlook account; never
  switch to another account.
* Uncertain sends: the inquiry stays `uncertain`, keeps its reservation and quota debit, and is
  reconciled by Message-ID. Only positive evidence (found in Sent Items/provider, or a correlated
  reply/bounce) resolves it; `not_found_yet` never releases anything. An automatic retry happens
  only after a proven pre-submission failure with no possibly running earlier attempt, and only
  through the same account.

## 8. Activation evidence checklist

Collect every row for the ACTUAL account and client before `SELLER_INQUIRY_MODE=automatic`; keep
the evidence in the activation log (ids, timestamps, codes; never an address, token or body). Each
row is a one-time technical check, not a message approval.

| # | Evidence | How | Done when |
|---|---|---|---|
| 1 | Sender binding verified | `suv-deals sender-binding create` (runbook 10.1), desktop worker account report, `suv-deals sender-binding verify` (outlook_local) or the provider check (gmail_api, section 4) | `suv-deals sender-binding status` shows `verified`, `usable`, `healthy`; `suv-deals doctor` lists `seller_inquiry/sender_binding` OK |
| 2 | Configured runtime | classic Outlook version (`python -m outlook_bridge check`), worker revision equals the backend revision, `suv-deals doctor` v1.1 readiness lines | heartbeat fresh, `monitoring active`, no open coverage gap (`GET /api/mail-workers/coverage-gaps`) |
| 3 | Standing authorization active | `suv-deals inquiries status` | `authorization: active`, caps 2/24 h and 5/15 days (or lower), kill switch off only at the end |
| 4 | Owner-controlled canary | `suv-deals canary prepare` records it for the configured sender (target from `SUV_CANARY_TARGET_ADDRESS` or a hidden prompt, stored as a SHA-256 only); the owner then runs `suv-deals canary send <id> --i-confirm-owner-controlled-address --yes` (the canary flow below) | the canary never reserves a vehicle/seller pair, never debits the quota ledger and never counts as a seller inquiry or one of the 15-day deals (`domain.evaluation` excludes canary records and reports only their count); `suv-deals canary status` shows `accepted`. **Blocked today:** the record, the gates and the commands exist, but no route has a canary transport yet (`CANARY_TRANSPORT_UNAVAILABLE`, see below), so this row cannot be completed yet |
| 5 | Receipt reconciliation | the canary's Message-ID found in Sent Items (outlook_local) or by the provider search (gmail_api) | `found_sent`; record whether the provider kept the client Message-ID (a rewritten one is published to the worker as an observed Message-ID) |
| 6 | Correlated test reply | reply from the owner-controlled address; the desktop worker correlates it locally -> `POST /v1/mail-workers/replies` -> outbox `seller.reply.received.v1` -> private Slack signal -> dot -> MCP `seller_replies_get` | the reply is stored (`ingest_status: stored`), exactly one Slack signal, dot's tool call recorded; an unrelated personal message sent at the same time is NOT uploaded |
| 7 | Stop works | dashboard/MCP/CLI pause, then the worker's next claim | the claim answers `kill_switch`; nothing leaves Outlook; resume by the owner |

### Canary flow (rows 4-6; the owner's activation step)

A canary is an e-mail outside the seller caps, so it has its own bounds: no canary is prepared
while the kill switch is on, at most `MAX_CANARIES_PER_24H` (PROPOSED 5) per rolling 24 hours
(cancelled ones count), its purpose never contains an address, and its target must not be a
stored seller contact. Its Message-ID is `<canary-<id>@<sender domain>>` and can never correlate to
a seller inquiry.

1. Prepare (nothing is sent): `SUV_CANARY_TARGET_ADDRESS=<owner test address> suv-deals canary
   prepare --purpose "activation route check" --yes`, or run it interactively and type the address
   at the hidden prompt. The address is never echoed, logged or stored (SHA-256 only, shown to
   nobody); unset the variable afterwards. `suv-deals canary status` / `suv-deals doctor`
   (`seller_inquiry/activation_canary`) show the evidence state (`prepared`, `accepted`,
   `uncertain`, `failed`, `complete`; `stale` after a sender-binding change).
2. Send - THE OWNER runs this himself, once; an operator script, a test or an agent never does:
   `suv-deals canary send <id> --i-confirm-owner-controlled-address --yes`. It refuses (exit 3,
   every closed gate listed by code, nothing sent, nothing changed) unless ALL of these hold:
   `SELLER_EMAIL_CANARY_SEND_ENABLED=true`, `SELLER_INQUIRY_MODE=automatic`,
   `SELLER_INQUIRY_KILL_SWITCH=false`, `SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL=false`, the workspace
   controls `automatic` with the kill switch off, the standing authorization active, the configured
   sender binding usable and exactly the configured identity, the canary `prepared` for that
   binding's current version, and the separate flag `--i-confirm-owner-controlled-address`. The
   target is entered again and must hash to the stored one. Right before the transport the
   controls row is locked, EVERY gate above is read again (a pause, a mode change, a revoked
   authorization, sender binding or desktop worker committed after the first check stops it) and
   the canary is committed `uncertain` (the attempt is durable before the external I/O, spec
   37.5), so a crash or a second `canary send` can never send it twice; an `uncertain` canary is reconciled (row 5), never re-sent. While the switches are
   on, hold real seller inquiries with `suv-deals inquiries set-limits --max-per-24h 0
   --max-per-15d 0` (the caps never apply to a canary) and restore the caps after the evidence is
   recorded.
3. Reconcile the canary's Message-ID (row 5) and record the correlated test reply (row 6); `canary
   status` then reports `complete` for the binding's current version.

**Current blocker (precise):** no route has a canary transport. The seller-inquiry providers
(`gmail_api`, `microsoft_graph`) refuse anything but the exact rendering of a registered seller
template (`mime_builder.inquiry_scope_problems`), and the desktop worker (`outlook_local`) claims
only inquiry send intents; correlating a canary reply on the desktop also needs a canary binding
on the wire. Step 2 therefore ends with `CANARY_TRANSPORT_UNAVAILABLE` after every other gate, and
rows 4-6 stay open. Keep `SELLER_EMAIL_CANARY_SEND_ENABLED=false`.

If row 4-6 cannot be completed (no canary transport, no classic-Outlook runtime, no verified Slack
trigger), report the precise blocker and keep the mode at `disabled_until_sender_ready`; never
claim monitoring or sending. After activation, real seller inquiries use the standing
authorization without any further approval.

