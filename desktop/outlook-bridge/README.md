# Outlook bridge: local classic-Outlook reply worker

Desktop worker for the seller-reply route chosen in spec v1.1 section 37.6:

```text
configured classic Outlook mailbox -> this local worker (local correlation only)
  -> POST /v1/mail-workers/replies (authenticated, mailbox-bound credential)
  -> backend stores the reply + outbox -> private Slack signal -> dot -> MCP reply tool
```

It also executes backend send intents for `SELLER_EMAIL_PROVIDER=outlook_local` (ADR 0002 addendum):
the bounded initial inquiry only, through `MailItem.Send` of the bound account.

Status: code and Linux tests are complete. **Nothing here has been run against a real Outlook,
mailbox or backend.** Activation (U7/U8) needs the owner's Windows machine; see "Activation" below.

## What the worker does and never does

| Does | Never does |
|---|---|
| Runs as the signed-in interactive Windows user next to classic Outlook | Run as a Windows service, as SYSTEM, in session 0 or headless (refused) |
| Uses one dedicated STA thread with a live message pump for every Outlook call | Call the Outlook Object Model from any other thread |
| Treats `NewMailEx` as a prompt and reconciles the configured folders at startup and every 120 s (configurable) | Rely on events alone, scan other accounts/stores, read OST/PST files or extract credentials |
| Correlates every message **locally** with the shared domain rules (`suv_deals.domain.replies`) | Upload unrelated personal mail: it never leaves the machine (only a hashed key and an outcome code are kept locally) |
| Uploads only replies correlated to one of this system's inquiries (or a single-candidate possible match, flagged as quarantined) | Upload multi-inquiry ambiguity, the owner's own messages, meetings/sharing/non-mail items, attachment bytes or identity documents |
| Keeps a durable local backlog (SQLite, WAL) and uploads idempotently after outages | Advance an acknowledged checkpoint before the server acknowledged the upload |
| Executes one validated backend intent once, reporting local submission and Sent Items evidence separately | Compose, follow up, reply, offer, accept a price, reserve, resend an uncertain message or switch accounts |
| Proves locally, before `.Send`, that the intent is byte-for-byte one registered spec 37.4 template rendering (DE/IT/FR/EN) inside the three-question scope, signed with the verified sender display name | Send free text, an edited template, the Macedonian preview, extra questions/URLs/personal data, CC/BCC, attachments, read/delivery receipts or "on behalf of" mail |
| Checks the address Outlook actually resolved the recipient (and Reply-To) to | Send to an address-book contact's other address |
| Records honest coverage gaps (sleep, worker offline, Outlook closed/disconnected, backend/credential problems) | Claim 24-hour monitoring or change Outlook, Trust Center, registry or antivirus settings |

## Requirements

- Windows with **classic Outlook for Windows** (Microsoft 365/Office). *New Outlook* has no Outlook
  Object Model/COM and is reported as unsupported by `check` (spec 37.6; Microsoft feature comparison).
  If the "New Outlook" toggle is on, switch it off yourself; the worker never changes it.
- The configured mailbox account added to the classic Outlook profile and synchronising.
- Python 3.12 or 3.13 (64-bit, same bitness as Office is not required for out-of-process COM).
- `requirements-windows.txt` (pywin32 is Windows-only and imported lazily) and the pure shared
  domain package `suv_deals` installed with `--no-deps` from this repository. Install the **same
  revision as the deployed backend**: the worker re-proves every send intent against the registered
  seller templates and correlates replies with the shared rules, so a template version the desktop
  does not know is refused (`intent_invalid`) instead of being sent.

## Installation (Windows, at activation)

```bat
py -3.13 -m venv %LOCALAPPDATA%\SUVDeals\OutlookBridge\venv
set VENV=%LOCALAPPDATA%\SUVDeals\OutlookBridge\venv\Scripts
%VENV%\pip install -r desktop\outlook-bridge\requirements-windows.txt
%VENV%\pip install --no-deps .
copy desktop\outlook-bridge\config.example.toml %LOCALAPPDATA%\SUVDeals\OutlookBridge\config.toml
```

Edit `config.toml` (no secrets in it): backend URL, the mailbox binding id issued at activation,
the account's SMTP address and every folder a mailbox rule moves mail into (`rule_target`). If any
configured folder cannot be resolved (a typo in a rule-target path, an IMAP store without a default
Junk folder) the worker scans nothing and keeps an open `folder_scope_invalid` gap until the
configuration or the folder is fixed - it never silently scans a partial folder set. Run the
worker from `desktop\outlook-bridge` (`%VENV%\python -m outlook_bridge ...`) or put that directory
on `PYTHONPATH`.

Store the narrow, revocable worker credential issued by the backend (input hidden; Windows
Credential Manager, per user, this machine only):

```bat
%VENV%\python -m outlook_bridge credential set --expires-at 2026-12-31T00:00:00+00:00
```

Supabase service-role/secret/publishable keys, Supabase JWTs and database URLs are refused: they
must never be on the desktop. A rejected (401) or expired credential stops all transmission, keeps
the local backlog, and transmission resumes only after a *different* credential is stored.

## Commands

```text
python -m outlook_bridge [--config PATH] check                 read-only environment report (exit 0 = ready)
python -m outlook_bridge [--config PATH] run [--dry-run]       the worker loop
python -m outlook_bridge [--config PATH] reconcile-once [--dry-run]
python -m outlook_bridge [--config PATH] status                local health and coverage gaps
python -m outlook_bridge [--config PATH] credential set|status|delete
```

Exit codes: 0 ok, 2 configuration, 3 unsupported environment (new Outlook, not Windows,
non-interactive session), 4 credential, 5 runtime (for example the shared domain package missing).
`--dry-run` uses a throw-away in-memory store, may fetch bindings (GET) and correlates locally, but
never uploads, reports, sends heartbeats or calls `MailItem.Send`. All output is JSON without mail
content; logs are content-free JSON lines in `%LOCALAPPDATA%\SUVDeals\OutlookBridge\logs`.

Start the worker **at logon of the owner** (Task Scheduler: trigger "At log on", "Run only when user
is logged on"), never as a service. The computer must be awake, the user signed in, Outlook open and
the mailbox synchronising for timely detection; every other interval is a reported gap.

## Backend contract (client side)

| Endpoint | Status |
|---|---|
| `GET /v1/mail-workers/inquiry-bindings?cursor=&limit=100` | spec 37.8 (normative) |
| `POST /v1/mail-workers/replies` (+ `Idempotency-Key`) | spec 37.8 (normative), v1.0 JSON |
| `GET /v1/mail-workers/send-intents?limit=` | served (`api.mail_worker_routes`); pending intents plus reaped, never-claimed ones flagged `expired: true` (refused locally as `intent_expired`, without a claim, so the backend can reconcile the inquiry) |
| `POST /v1/mail-workers/send-intents/{id}/claim` | served: revalidation immediately before `.Send`; every claim carries a new `claim_attempt_id` (and Idempotency-Key) and is evaluated fresh, never answered from an idempotency replay. Refusals: `kill_switch` (reported as a retryable pre-submission refusal), `not_now` (rolling caps, seller cooldown, source pause: nothing is reported, the intent waits and is claimed again after 10 minutes while valid), or a final reason. The claim's `worker_id` is `<worker_id>.<store instance id>`: the instance id is random, written once when the local SQLite store is created, so a reinstall, a wiped store or a second `data_dir` claims as a different worker, and the backend grants a running intent to ONE worker id only (`intent_invalid` / `ALREADY_CLAIMED` for any other; that intent is then held `uncertain`, never resent). Choose a `worker_id` that starts with a letter |
| `POST /v1/mail-workers/send-intents/{id}/report` | served: `OutlookSendReport` mirror (`Idempotency-Key: report-<intent>-<state>`); the observed Internet Message-ID of the sent copy is published back in the binding so replies to a rewritten header still correlate |
| `POST /v1/mail-workers/heartbeat` | served: health, checkpoints, gaps (a gap never ends before it starts); answer carries Slack/MCP health |
| `POST /v1/mail-workers/account-report` | served: `OutlookAccountReport` mirror (`security_settings_unchanged` is always `true`); a refused report is `409` |

The worker never names a workspace and refuses client-side any binding page, upload or intent for
another mailbox binding. A matched seller reply is sent in exactly the spec v1.0 shape; the optional
domain extensions (`message_type`, `correlation_status`/`correlation_reasons`,
`withheld_sensitive_attachments`, and for a bounce/delivery notice `returned_message_ids`: the
returned original's Message-IDs read from the RAW report before sanitising) appear only for
auto-replies/bounces, quarantined possible matches and withheld attachments. The backend and this
client ship together from one repository revision; `tests/contracts/test_mail_worker_contract.py`
keeps both sides field-for-field identical and `tests/integration/mail_worker_e2e` runs this worker
against the real backend app and PostgreSQL. Limits: request <= 128 KiB, body <= 64 KiB, subject <= 512 characters,
<= 20 attachment metadata entries (name, MIME type, size, SHA-256, opaque local reference; never bytes
or paths). Typed failures: 400/422 rejected locally, 401 stops transmission, 403/404/409 force a
binding resync first, `IDEMPOTENCY_CONFLICT` is final (server quarantine), 429 honours `Retry-After`,
5xx/transport errors back off (30 s doubling, max 1 h) and keep the backlog. A binding page that is
rejected locally (an item of another mailbox, invalid content) or a sync that cannot progress stops
all uploads (fail closed: the page may carry a revocation) and keeps an open
`binding_sync_incomplete` gap until a later sync reads the change log to its end.

## Local state (protected)

`%LOCALAPPDATA%\SUVDeals\OutlookBridge\bridge-state.sqlite3` (WAL, `synchronous=FULL`, per-user
profile ACL; owner-only file mode on POSIX). One store belongs to one mailbox binding. It holds the
binding cache with its sync cursor (page and cursor committed atomically; server tombstones are
final; two payloads under one version are tombstoned locally until a strictly newer version
arrives, and replies to such a binding wait as bounded pending locators meanwhile),
per-folder scan watermarks and last complete scans, hashed processed keys with EntryID/StoreID
locators and locator history, bounded reply-before-binding locators (metadata only, 24 h window,
at most 2000), the upload backlog of correlated replies, send intents and coverage gaps. Unrelated
mail leaves no subject, body, sender or header locally. Old unrelated keys are pruned after the
look-back horizon and acknowledged uploads after 180 days.

Checkpoints: the scan watermark advances only after a complete scan whose items were committed and
stays behind open/recent detection gaps; the acknowledged watermark never passes an item that the
server has not acknowledged (or a reply still waiting for its binding). An item that fails to be
read three times is given up once, surfaced as an `item_unreadable` gap and not re-read on every
overlapping scan.

Send intents only move forward (`received -> attempting -> send_failed/submitted -> confirmed`;
`refused` only from `received`, or from `attempting` when Outlook refused the item before `.Send`).
The `attempting` commit re-checks, in the same SQLite write transaction, that no other intent of the
inquiry was attempted and that the local ceilings (2 per 24 h, 5 per 15 days; refusals before
`.Send` do not count) still hold, so two worker processes on one store can never both send.

## Tests (Linux, fakes only)

Everything Outlook/COM-related sits behind interfaces with in-memory fakes (`outlook_bridge.testing`:
`FakeOutlook`, `FakeComApi`, `FakeBackend` over `httpx.MockTransport`). No real Outlook, network,
mailbox or e-mail is used. The repository root `testpaths` is `["tests"]`, so run these explicitly:

```make
outlook-bridge-test:
	cd /home/user/Lokal69 && uv run pytest desktop/outlook-bridge/tests -q

outlook-bridge-lint:
	cd /home/user/Lokal69 && uv run ruff check desktop/outlook-bridge && uv run ruff format --check desktop/outlook-bridge
	cd /home/user/Lokal69 && MYPYPATH=src:desktop/outlook-bridge:desktop/outlook-bridge/tests \
	  uv run mypy --strict desktop/outlook-bridge/outlook_bridge desktop/outlook-bridge/tests
```

The suite covers the spec 37.10 delta tests for this package: NewMailEx startup gaps recovered by
reconciliation; moved messages deduplicated (fingerprint excludes locators); sleep/offline outages
show gaps and recover the backlog; backend outage and replay; duplicate message events;
revoked/tombstoned bindings stop matching; cross-mailbox injection rejected client-side;
reply-before-binding-sync race; unrelated personal mail never uploaded; checkpoints advance only
after acknowledgement; credential expiry/rejection stops transmission and keeps the backlog; the STA
guard refuses SYSTEM/service/non-interactive contexts; the compatibility check reports new Outlook as
unsupported; no blind resend after an uncertain or interrupted `.Send`; no automatic reply or
follow-up.

## Activation checklist (U7/U8; not yet performed)

1. `check` on the owner's PC reports `classic`, `supported`, an interactive session and an active
   credential. Record the Outlook build it reports. Registry locations used (read-only):
   `HKCR\Outlook.Application\CLSID`, `HKLM\...\ClickToRun\Configuration\VersionToReport`,
   `HKCU\Software\Microsoft\Office\16.0\Outlook\Preferences\UseNewOutlook` - re-verify on the machine.
2. `reconcile-once --dry-run` resolves the configured account and folders and shows plausible counts.
3. With the backend endpoints deployed, `reconcile-once` then `run`; verify heartbeats, checkpoints
   and gaps in the backend.
4. For `outlook_local` sending, the activation canary (docs/seller_email_activation.md section 8;
   the owner's one-time step): put the owner-controlled test address in `config.toml` as
   `canary_target_address` (never the sending account itself; the backend keeps only its SHA-256,
   and the worker sends a canary only to an address with exactly that hash) and keep
   `send_intents_enabled = true`. After the owner's `suv-deals canary send`, the worker lists the
   canary (`GET /v1/mail-workers/canary-intents`), claims it once, sends the fixed canary text once
   and reports Outbox -> Sent Items evidence. It refuses before any `.Send` (and reports it) when the
   target is missing, differs or is the sending account, the canary names another mailbox or
   account, or its window ended; the kill switch only defers it. Reply to the canary from the test
   mailbox: the worker uploads only that reply's headers and the sender's SHA-256 for a canary it
   sent itself (`POST /v1/mail-workers/canary-intents/{id}/reply`); `suv-deals canary status` then
   shows `complete`. A canary is never a seller inquiry and its reply never a seller reply, so the
   Slack -> dot -> MCP leg is verified separately (gate `seller_reply_slack_route`). Comment
   `canary_target_address` out again afterwards.

Unverified until activation (UNKNOWN, not assumed): whether Outlook keeps a Message-ID set through
`PropertyAccessor` on an unsent item for the account type in use (the worker then observes the
actual one instead), where an IMAP/Gmail account's sent copies appear, how promptly `NewMailEx`
fires for non-Exchange stores (reconciliation covers missing events), and Outlook security prompts
(left to the user; never disabled).
