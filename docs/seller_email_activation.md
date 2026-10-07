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
and `Content-Transfer-Encoding` (quoted-printable or base64). The final bytes are re-parsed and
verified before every submission. The client Message-ID is a correlation/search key; reusing it
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
| `SELLER_REPLY_INGEST_MODE` | `local_classic_outlook` (chosen route) or `provider_api` |

`seller_email.build_sender_provider` refuses to construct a sending provider unless the mode is
`automatic`, the kill switch is off, the binding is verified (provider, stable account id, From,
display name, alias status, healthy, not revoked) and exactly matches these settings. That is a
technical prerequisite, not an approval. A live kill-switch probe is re-read immediately before
every transmission and fails closed.

## 3. Route A (default): classic Outlook on the owner's Windows machine

The backend never sends itself: it stores an `OutlookSendIntent` that the desktop worker pulls and
submits with `MailItem.Send` from the configured account only.

1. Install **classic Outlook for Windows**. New Outlook does not support the Outlook Object Model
   and is refused (`NEW_OUTLOOK_UNSUPPORTED`). UNVERIFIED: installed edition/version.
2. In Outlook, add the selected sender mailbox (ADR 0002: the owner's personal Gmail) through
   Outlook's own sign-in flow. No password or token is copied anywhere else.
3. Install and start the desktop worker (separate package) under the signed-in interactive user;
   do not disable Trust Center, Object Model Guard or antivirus checks (the worker reports
   `security_settings_unchanged`; a weakened setting blocks verification).
4. The worker reports the account (`OutlookAccountReport`): classic flavour, SMTP address, stable
   account key, display name. Set `SELLER_EMAIL_ACCOUNT_ID` to the reported stable key (the SMTP
   address is accepted with a warning) and `SELLER_EMAIL_FROM` to that SMTP address. Outlook
   offers no send-as alias verification here, so only the account's own address verifies.
5. UNVERIFIED for the account type: whether classic Outlook stores sent Gmail/IMAP messages in
   Sent Items (Gmail may save them server-side instead). Receipt reconciliation relies on the
   worker's Sent Items evidence; record what the test in section 6 shows.

Outcome semantics: until the worker reports, a send is `uncertain` (`local_worker_handoff`);
`submitted_to_outbox` is a *local submission*, still `uncertain` with `outbox_pending=yes`;
`sent_items_confirmed` is `accepted` (no provider id or SMTP receipt exists and none is invented);
a worker refusal before `.Send` is a proven pre-submission failure. While the laptop or Outlook is
offline, inquiries wait in the backend queue and the offline time is reported as a coverage gap.

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
redirects, read timeouts, write interruptions and connection resets are `uncertain`. An inquiry
always starts a new Gmail thread (no `threadId`, no `In-Reply-To`/`References`).

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
2. **Test message** (where the owner authorizes it): a synthetic canary inquiry addressed to an
   **owner-controlled test address** (never a seller), marked as canary so it does not count as
   a seller inquiry or one of the 15-day deals.
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

* Pause everything: `SELLER_INQUIRY_KILL_SWITCH=true` or the MCP `seller_inquiries_pause` tool
  (scope `inquiries:pause`). Untransmitted work stops at once; reconciliation keeps running.
* Revoke access: remove the OAuth grant at the provider (the next token refresh reports
  `CREDENTIALS_REVOKED`, health turns `credentials_revoked`, sending stops) or remove the Outlook
  account; never switch to another account.
* Uncertain sends: the inquiry stays `uncertain`, keeps its reservation and quota debit, and is
  reconciled by Message-ID. Only positive evidence (found in Sent Items/provider, or a correlated
  reply/bounce) resolves it; `not_found_yet` never releases anything. An automatic retry happens
  only after a proven pre-submission failure with no possibly running earlier attempt, and only
  through the same account.
