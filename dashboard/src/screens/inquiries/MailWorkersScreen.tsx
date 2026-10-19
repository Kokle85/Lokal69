import { useState, type ReactNode } from 'react'
import type { ApiError } from '../../api/errors'
import type {
  CanaryEvidenceState,
  ComponentStatus,
  CredentialStatus,
  InquiryControlView,
  LagView,
  MailboxHealthView,
  MailCoverageGapItem,
  MailWorkerCredentialView,
  MailWorkerHealthView,
  ReplySignalSummaryView,
} from '../../api/types'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../../components/ui'
import { countText, durationText, label, shortHash, yesNo } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import { authorizationText, InquiryAreaNav, LagValue, RequireScope, senderReadinessLabel, senderReadinessText } from './shared'

export function MailWorkersScreen() {
  return (
    <RequireScope scope="inquiries:read" what="mail-worker health">
      <MailWorkersContent />
    </RequireScope>
  )
}

/** The credential the server lists for one worker (`null`: not reported). */
function credentialOf(health: MailWorkerHealthView, box: MailboxHealthView): MailWorkerCredentialView | null {
  return health.credentials.find((item) => item.mailbox_binding_id === box.mailbox_binding_id) ?? null
}

/**
 * Whether a worker is monitoring NOW. The server's `monitoring_active` rests on fresh reports, and a
 * heartbeat stays fresh for a while after its credential expired or was revoked although the worker
 * can no longer upload a reply or claim a send: that worker is a coverage gap, never "monitoring".
 */
function monitoringNow(box: MailboxHealthView, credential: MailWorkerCredentialView | null): boolean {
  return box.monitoring_active && !(box.binding_state === 'active' && credential !== null && credentialDead(credential.credential_status))
}

function anyMonitoringNow(health: MailWorkerHealthView): boolean {
  return health.mailboxes.some((box) => monitoringNow(box, credentialOf(health, box)))
}

function MailWorkersContent() {
  const { timezone, can } = useWorkspace()
  const [includeRevoked, setIncludeRevoked] = useState(false)
  const health = useApiQuery((api, signal) => api.mailWorkerHealth({ include_revoked: includeRevoked }, { signal }), [includeRevoked])
  const gaps = useApiQuery((api, signal) => api.mailCoverageGaps({ include_revoked: includeRevoked }, { signal }), [includeRevoked])
  const control = useApiQuery((api, signal) => api.inquiryControl({ signal }), [])
  return (
    <div className="screen">
      <h1>Mail workers and coverage</h1>
      <InquiryAreaNav />
      <p className="muted small">
        Seller replies are detected by the reply worker on the owner&apos;s PC (classic Outlook). While the PC is off, asleep or
        offline, replies are not detected: that time is a coverage gap, never shown as healthy. The worker catches up from its
        checkpoints when it returns.
      </p>
      <label className="inline-check">
        <input type="checkbox" checked={includeRevoked} onChange={(event) => setIncludeRevoked(event.target.checked)} /> include revoked
        workers
      </label>
      {health.status === 'loading' ? <LoadingState label="Loading mail-worker health" /> : null}
      {health.error ? <ErrorPanel error={health.error} onRetry={health.reload} /> : null}
      {health.data && health.envelope ? (
        <>
          <ViewMeta asOf={health.envelope.as_of} fetchedAt={health.fetchedAt} timeZone={timezone} onReload={health.reload} reloading={health.reloading} />
          <Warnings warnings={health.envelope.warnings} />
          {health.data.any_monitoring_active && anyMonitoringNow(health.data) ? (
            <Notice tone="ok">
              <span data-testid="monitoring-summary">
                At least one mailbox is monitored right now. {health.data.open_gap_count} open coverage gap(s).
              </span>
            </Notice>
          ) : (
            <Notice tone="bad">
              <span data-testid="monitoring-summary">
                No mailbox is monitored right now: seller replies are not being detected. {health.data.open_gap_count} open coverage
                gap(s).
              </span>
            </Notice>
          )}
          {health.data.notes.length ? (
            <ul className="small">
              {health.data.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          ) : null}
          {health.data.mailboxes.length === 0 ? (
            <EmptyState>No mail worker is registered for this workspace, so no replies can be detected.</EmptyState>
          ) : (
            health.data.mailboxes.map((box) => (
              <MailboxCard
                key={box.mailbox_binding_id}
                box={box}
                credential={health.data ? credentialOf(health.data, box) : null}
                timeZone={timezone}
              />
            ))
          )}
          <CredentialsSection health={health.data} includeRevoked={includeRevoked} timeZone={timezone} />
          <ReplySignalsSection signals={health.data.reply_signals} />
        </>
      ) : null}
      <Section title="Coverage gaps" id="coverage-gaps">
        {gaps.status === 'loading' ? <LoadingState label="Loading coverage gaps" /> : null}
        {gaps.error ? <ErrorPanel error={gaps.error} onRetry={gaps.reload} /> : null}
        {gaps.data ? <GapTable items={gaps.data.items} timeZone={timezone} /> : null}
      </Section>
      <ActivationSection
        control={control.data}
        controlError={control.error}
        controlLoading={control.status === 'loading'}
        health={health.data}
        isOwner={can('config:admin')}
        timeZone={timezone}
      />
    </div>
  )
}

const CREDENTIAL_TONE: Record<CredentialStatus, string> = { active: 'ok', expiring: 'warn', expired: 'bad', revoked: 'bad' }

const CREDENTIAL_TEXT: Record<CredentialStatus, string> = {
  active: 'active',
  expiring: 'expiring within 14 days',
  expired: 'expired',
  revoked: 'revoked',
}

/** An expired or revoked credential can no longer upload replies or claim sends (the worker keeps its backlog). */
function credentialDead(status: CredentialStatus): boolean {
  return status === 'expired' || status === 'revoked'
}

function CredentialBadge({ status }: { status: CredentialStatus }) {
  return (
    <span data-testid="credential-status" data-status={status}>
      <Badge tone={CREDENTIAL_TONE[status] ?? 'warn'}>{CREDENTIAL_TEXT[status] ?? status}</Badge>
    </span>
  )
}

/**
 * Each listed worker's credential (never a token, hash or prefix) and the revoked workers, which are
 * counted even when they are not listed.
 */
function CredentialsSection({
  health,
  includeRevoked,
  timeZone,
}: {
  health: MailWorkerHealthView
  includeRevoked: boolean
  timeZone: string
}) {
  const dead = health.credentials.filter((item) => item.binding_state === 'active' && credentialDead(item.credential_status))
  return (
    <Section title="Worker credentials" id="worker-credentials">
      {dead.length ? (
        <Notice tone="bad">
          <span data-testid="credentials-not-live">
            {dead.length} active worker(s) hold an expired or revoked credential: they can no longer upload replies or claim sends
            (they keep their backlog) until the owner issues a new credential (<code>suv-deals mail-worker credential</code>). That
            time is a coverage gap.
          </span>
        </Notice>
      ) : null}
      {health.revoked_mailboxes > 0 ? (
        <p className="muted small" data-testid="revoked-mailboxes">
          {health.revoked_mailboxes} revoked mail worker(s){includeRevoked ? '' : ' are not listed (tick “include revoked workers” to show them)'}. A
          revoked worker can never upload or send again.
        </p>
      ) : null}
      {health.credentials.length === 0 ? (
        <EmptyState>No worker credential is listed.</EmptyState>
      ) : (
        <table className="responsive-table" aria-label="Worker credentials">
          <thead>
            <tr>
              <th scope="col">Worker</th>
              <th scope="col">Worker binding</th>
              <th scope="col">Credential</th>
              <th scope="col">Expires</th>
              <th scope="col">Revoked</th>
            </tr>
          </thead>
          <tbody>
            {health.credentials.map((item: MailWorkerCredentialView) => (
              <tr key={item.mailbox_binding_id} data-testid="credential-row" data-status={item.credential_status}>
                <td data-label="Worker">{item.worker_label}</td>
                <td data-label="Worker binding">
                  <Badge tone={item.binding_state === 'active' ? 'ok' : 'muted'}>{item.binding_state}</Badge>
                </td>
                <td data-label="Credential">
                  <CredentialBadge status={item.credential_status} />
                </td>
                <td data-label="Expires">
                  <Timestamp value={item.expires_at} timeZone={timeZone} />
                </td>
                <td data-label="Revoked">
                  {item.revoked_at ? <Timestamp value={item.revoked_at} timeZone={timeZone} /> : <span className="muted">no</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

/** Reply-signal flood control of the rolling window (counts only; never message content). */
function ReplySignalsSection({ signals }: { signals: ReplySignalSummaryView | null }) {
  return (
    <Section title="Reply signals to dot" id="reply-signals">
      {signals === null ? (
        <p className="muted">The server reported no reply-signal summary.</p>
      ) : (
        <div data-testid="reply-signals">
          {signals.rate_limited > 0 ? (
            <Notice tone="warn">
              <span data-testid="signals-rate-limited">
                {signals.rate_limited} reply signal(s) hit the per-inquiry cap in the last {signals.window_hours} h: those replies are
                stored and shown in Seller replies, but started no new dot activation. Review them there.
              </span>
            </Notice>
          ) : null}
          <KeyValues
            items={[
              ['Window', `last ${signals.window_hours} h`],
              ['Signals emitted', String(signals.emitted)],
              ['Coalesced into a pending signal', String(signals.coalesced)],
              ['Stopped by the per-inquiry cap', String(signals.rate_limited)],
              ['Inquiries at the cap', String(signals.inquiries_at_cap)],
              [
                'Cap per inquiry',
                <span key="c" data-testid="signal-cap">
                  {signals.cap_per_inquiry} per {signals.window_hours} h <Badge tone="warn">PROPOSED</Badge>
                </span>,
              ],
            ]}
          />
        </div>
      )}
    </Section>
  )
}

type EvidenceState = 'done' | 'open' | 'unknown'

const EVIDENCE_TONE: Record<EvidenceState, string> = { done: 'ok', open: 'bad', unknown: 'warn' }

const CANARY_EVIDENCE_TEXT: Record<CanaryEvidenceState, string> = {
  complete: 'complete: a correlated test reply was recorded',
  prepared: 'prepared: not sent yet',
  accepted: 'accepted: no correlated test reply yet',
  uncertain: 'uncertain: reconcile its Message-ID, never re-send',
  failed: 'failed: prepare a new canary',
  cancelled: 'cancelled',
  stale: 'stale: only canaries of an older sender-binding version',
  none: 'none: no canary recorded for the configured sender',
  no_sender: 'no configured sender binding',
}

/**
 * Rows 4-6 (owner-controlled canary, receipt reconciliation, correlated test reply) as the
 * owner-only, read-only `GET /api/activation/canary-evidence` reports them: the same state as
 * `suv-deals canary status`. Met only when the server says `complete`; anything it cannot report is
 * unknown, never met. No canary is prepared or sent from here (the owner's CLI step).
 */
function CanaryEvidenceRow({ timeZone }: { timeZone: string }) {
  const evidence = useApiQuery((api, signal) => api.canaryEvidence({ signal }), [])
  const data = evidence.data
  const state: EvidenceState = data === null ? 'unknown' : data.evidence === 'complete' ? 'done' : 'open'
  return (
    <tr data-testid="activation-row" data-evidence="canary" data-state={state}>
      <td data-label="Evidence">4-6. Owner-controlled canary, receipt reconciliation, correlated test reply</td>
      <td data-label="State here">
        <Badge tone={EVIDENCE_TONE[state]}>{state === 'done' ? 'shown as met' : state === 'open' ? 'open' : 'unknown'}</Badge>
      </td>
      <td data-label="Detail">
        {evidence.status === 'loading' ? <span className="muted">loading the canary evidence</span> : null}
        {evidence.error ? (
          <span data-testid="canary-evidence" data-evidence-state="unknown">
            unknown: the canary evidence could not be loaded ({evidence.error.code}); read it with <code>suv-deals canary status</code>
          </span>
        ) : null}
        {data ? (
          <>
            <span data-testid="canary-evidence" data-evidence-state={data.evidence}>
              {CANARY_EVIDENCE_TEXT[data.evidence] ?? label(data.evidence)} ({data.detail})
              {data.sender_provider ? ` · ${label(data.sender_provider)} sender binding v${data.sender_binding_version ?? '?'}` : ''}
            </span>
            {data.canaries.length ? (
              <ul className="small plain-list">
                {data.canaries.map((item) => (
                  <li key={item.id} data-testid="canary-item" data-state={item.state}>
                    <code>{shortHash(item.id)}</code> <Badge value={item.state} /> binding v{item.sender_binding_version}
                    {item.current_sender_version ? '' : ' (older version)'} · prepared{' '}
                    <Timestamp value={item.created_at} timeZone={timeZone} />
                    {item.accepted_at ? (
                      <>
                        {' '}
                        · accepted <Timestamp value={item.accepted_at} timeZone={timeZone} />
                      </>
                    ) : null}
                    {item.reply_recorded_at ? (
                      <>
                        {' '}
                        · test reply <Timestamp value={item.reply_recorded_at} timeZone={timeZone} />
                      </>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            <span className="muted small">
              {' '}
              A canary is prepared and sent only by the owner on the command line (<code>suv-deals canary</code>).
            </span>
          </>
        ) : null}
      </td>
    </tr>
  )
}

/**
 * The activation evidence of docs/seller_email_activation.md section 8, READ-ONLY. The dashboard
 * shows what its API reports (sender readiness, worker monitoring, standing authorization and, for
 * the owner, the canary rows 4-6 from `GET /api/activation/canary-evidence`); other roles see that
 * the canary evidence is the owner's. There is no canary, test or send control here.
 */
function ActivationSection({
  control,
  controlError,
  controlLoading,
  health,
  isOwner,
  timeZone,
}: {
  control: InquiryControlView | null
  controlError: ApiError | null
  controlLoading: boolean
  health: MailWorkerHealthView | null
  isOwner: boolean
  timeZone: string
}) {
  // A worker whose credential expired or was revoked is not monitoring, however fresh its last report.
  const monitored = health !== null && health.any_monitoring_active && anyMonitoringNow(health)
  const rows: Array<{ key: string; evidence: string; state: EvidenceState; detail: ReactNode }> = [
    {
      key: 'sender',
      evidence: '1. Sender binding verified',
      state: control === null ? 'unknown' : control.sender_readiness === 'ready' ? 'done' : 'open',
      detail:
        control === null ? (
          'unknown: the inquiry controls could not be loaded'
        ) : (
          <>
            {senderReadinessLabel(control.sender_readiness)}: {senderReadinessText(control.sender_readiness)}
            {control.sender_provider ? ` · ${label(control.sender_provider)}` : ''}
          </>
        ),
    },
    {
      key: 'runtime',
      evidence: '2. Configured runtime monitoring',
      state: health === null ? 'unknown' : monitored && health.open_gap_count === 0 ? 'done' : 'open',
      detail:
        health === null
          ? 'unknown: the worker health could not be loaded'
          : monitored
            ? `a mailbox is monitored · ${health.open_gap_count} open coverage gap(s)`
            : `no mailbox is monitored · ${health.open_gap_count} open coverage gap(s)`,
    },
    {
      key: 'authorization',
      evidence: '3. Standing authorization active',
      state: control === null ? 'unknown' : control.authorization_status === 'active' ? 'done' : 'open',
      detail:
        control === null
          ? 'unknown: the inquiry controls could not be loaded'
          : `${authorizationText(control.authorization_status)}${control.authorization_version !== null ? ` (version ${control.authorization_version})` : ''}`,
    },
  ]
  return (
    <Section title="Activation evidence (read-only)" id="activation-evidence">
      <p className="muted small">
        The one-time technical activation checklist of the sending route (docs/seller_email_activation.md section 8). Each row is a
        technical check, never a message approval. This page only shows evidence; it has no canary, test or send control.
      </p>
      {controlLoading ? <LoadingState label="Loading the sending readiness" /> : null}
      {controlError && controlError.code !== 'NOT_FOUND' ? <ErrorPanel error={controlError} /> : null}
      <table className="responsive-table" aria-label="Activation evidence">
        <thead>
          <tr>
            <th scope="col">Evidence</th>
            <th scope="col">State here</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} data-testid="activation-row" data-evidence={row.key} data-state={row.state}>
              <td data-label="Evidence">{row.evidence}</td>
              <td data-label="State here">
                <Badge tone={EVIDENCE_TONE[row.state]}>{row.state === 'done' ? 'shown as met' : row.state === 'open' ? 'open' : 'unknown'}</Badge>
              </td>
              <td data-label="Detail">{row.detail}</td>
            </tr>
          ))}
          {isOwner ? (
            <CanaryEvidenceRow timeZone={timeZone} />
          ) : (
            <tr data-testid="activation-row" data-evidence="canary" data-state="owner_only">
              <td data-label="Evidence">4-6. Owner-controlled canary, receipt reconciliation, correlated test reply</td>
              <td data-label="State here">
                <Badge tone="warn">owner only</Badge>
              </td>
              <td data-label="Detail">
                <span data-testid="canary-evidence" data-evidence-state="owner_only">
                  The canary evidence is shown to the owner only and is never assumed here. The owner reads it on this page or with{' '}
                  <code>suv-deals canary status</code>; a canary is sent only by the owner on the command line.
                </span>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </Section>
  )
}

/** A component is shown healthy only when the server says so; anything else is not monitored. */
function ComponentBadge({ status }: { status: ComponentStatus }) {
  return <Badge value={status} tone={status === 'healthy' ? 'ok' : status === 'unknown' ? 'warn' : 'bad'} />
}

/** A lag as one short phrase (for a last-report note). */
function lagText(lag: LagView): string {
  return lag.status === 'measured' && lag.value_seconds !== null ? durationText(lag.value_seconds) : lag.status
}

/**
 * A dimension the worker reports about itself (mailbox sync, backlog, matching gaps). The server
 * keeps the LAST report when the heartbeat stops (e.g. the PC is off), so without a fresh heartbeat
 * the value is unknown NOW: it is never shown as current health, only as the last report.
 */
function Reported({ fresh, last, children }: { fresh: boolean; last: string; children: ReactNode }) {
  if (fresh) return <>{children}</>
  return (
    <span data-testid="last-report">
      <span className="status-unknown">unknown now</span> <span className="muted small">(last report: {last})</span>
    </span>
  )
}

function MailboxCard({
  box,
  credential,
  timeZone,
}: {
  box: MailboxHealthView
  credential: MailWorkerCredentialView | null
  timeZone: string
}) {
  const monitoring = monitoringNow(box, credential)
  const fresh = box.heartbeat_status === 'healthy'
  const credentialNotLive = box.binding_state === 'active' && credential !== null && credentialDead(credential.credential_status)
  const syncOk = box.mailbox_sync_ok === null ? 'unknown' : yesNo(box.mailbox_sync_ok)
  return (
    <Section
      title={box.worker_label}
      id={`mailbox-${box.mailbox_binding_id}`}
      actions={
        monitoring ? (
          <Badge tone="ok">monitoring</Badge>
        ) : (
          <Badge tone="bad">not monitoring: coverage gap</Badge>
        )
      }
    >
      <div data-testid="mailbox-card" data-monitoring={monitoring ? 'yes' : 'no'}>
        {box.binding_state === 'revoked' ? (
          <Notice tone="bad">
            <span data-testid="mailbox-revoked">
              This worker was revoked: it can never upload replies or claim sends again. Its last reports are shown for the record.
            </span>
          </Notice>
        ) : null}
        {credentialNotLive && credential ? (
          <Notice tone="bad">
            <span data-testid="mailbox-credential-not-live">
              Its credential is {CREDENTIAL_TEXT[credential.credential_status]}: the worker cannot upload replies or claim sends (it
              keeps its backlog) until the owner issues a new credential. This is a coverage gap.
            </span>
          </Notice>
        ) : null}
        {!monitoring ? (
          <Notice tone="bad">
            <span data-testid="mailbox-not-monitoring">
              {credentialNotLive
                ? 'Not monitoring: its last reports may still look fresh, but it can no longer upload replies with this credential.'
                : box.heartbeat_status === 'healthy'
                ? 'Not monitoring: a health dimension below is not fresh.'
                : box.last_heartbeat_at
                  ? 'No fresh heartbeat: the PC may be off, asleep or offline, or Outlook is not running. Replies are not detected until it returns.'
                  : 'This worker has never reported a heartbeat.'}{' '}
              {fresh
                ? null
                : 'What the worker reports about itself (mailbox sync, backlog, matching gaps) is unknown until it reports again; its last report is shown for context only.'}
            </span>
          </Notice>
        ) : null}
        <KeyValues
          items={[
            ['Route', `${label(box.provider)} · binding ${box.binding_state}`],
            [
              'Worker credential',
              credential ? (
                <span key="c">
                  <CredentialBadge status={credential.credential_status} /> expires{' '}
                  <Timestamp value={credential.expires_at} timeZone={timeZone} />
                </span>
              ) : (
                <span key="c" className="muted">
                  not reported
                </span>
              ),
            ],
            [
              'Heartbeat',
              <span key="h">
                <ComponentBadge status={box.heartbeat_status} />{' '}
                {box.last_heartbeat_at ? (
                  <>
                    <Timestamp value={box.last_heartbeat_at} timeZone={timeZone} /> (
                    {box.heartbeat_age_seconds === null ? 'age unknown' : `${durationText(box.heartbeat_age_seconds)} ago`})
                  </>
                ) : (
                  <span className="muted">never</span>
                )}
              </span>,
            ],
            ['Outlook connection', <ComponentBadge key="o" status={box.outlook_status} />],
            [
              'Mailbox synchronising',
              <Reported key="so" fresh={fresh} last={syncOk}>
                {syncOk}
              </Reported>,
            ],
            [
              'Mailbox sync lag',
              <Reported key="s" fresh={fresh} last={lagText(box.mailbox_sync_lag)}>
                <LagValue lag={box.mailbox_sync_lag} />
              </Reported>,
            ],
            [
              'Last successful reconciliation',
              <span key="r">
                <ComponentBadge status={box.reconciliation_status} />{' '}
                <Timestamp value={box.last_successful_reconciliation_at} timeZone={timeZone} />
              </span>,
            ],
            [
              'Upload backlog',
              <Reported key="bc" fresh={fresh} last={countText(box.backlog_count)}>
                {countText(box.backlog_count)}
              </Reported>,
            ],
            [
              'Backlog age',
              <Reported key="b" fresh={fresh} last={lagText(box.backlog_age)}>
                <LagValue lag={box.backlog_age} />
              </Reported>,
            ],
            [
              'Unresolved matching gaps',
              <Reported key="mg" fresh={fresh} last={countText(box.unresolved_matching_gaps)}>
                {countText(box.unresolved_matching_gaps)}
              </Reported>,
            ],
            ['Account verification (classic Outlook)', <Badge key="a" value={box.account_status} />],
            ['Open coverage gaps', String(box.open_gap_count)],
          ]}
        />
        {box.reasons.length ? (
          <>
            <h3>Why</h3>
            <ul className="small">
              {box.reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </>
        ) : null}
        {box.folders.length ? (
          <>
            <h3>Folder checkpoints (hashed identities{fresh ? '' : '; as last reported, not current'})</h3>
            <table className="responsive-table" aria-label="Folder checkpoints">
              <thead>
                <tr>
                  <th scope="col">Folder</th>
                  <th scope="col">Last complete scan</th>
                  <th scope="col">Overlap watermark</th>
                  <th scope="col">Backlog</th>
                  <th scope="col">Gaps</th>
                </tr>
              </thead>
              <tbody>
                {box.folders.map((folder) => (
                  <tr key={`${folder.store_id_hash}-${folder.folder_id_hash}`}>
                    <td data-label="Folder">
                      {label(folder.folder_role)} <code title={folder.folder_id_hash}>{shortHash(folder.folder_id_hash)}</code>
                    </td>
                    <td data-label="Last complete scan">
                      <Timestamp value={folder.last_complete_scan_at} timeZone={timeZone} />
                    </td>
                    <td data-label="Overlap watermark">
                      <Timestamp value={folder.overlap_watermark} timeZone={timeZone} />
                    </td>
                    <td data-label="Backlog">{countText(folder.backlog_count)}</td>
                    <td data-label="Gaps">{folder.gap_reasons.length ? folder.gap_reasons.join('; ') : <span className="muted">none</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : null}
      </div>
    </Section>
  )
}

function GapTable({ items, timeZone }: { items: MailCoverageGapItem[]; timeZone: string }) {
  if (items.length === 0) return <EmptyState>No mailbox coverage gaps recorded.</EmptyState>
  return (
    <table className="responsive-table" aria-label="Mailbox coverage gaps">
      <thead>
        <tr>
          <th scope="col">Worker</th>
          <th scope="col">Gap</th>
          <th scope="col">Started</th>
          <th scope="col">Ended</th>
          <th scope="col">Detected by</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item, index) => (
          <tr key={`${item.mailbox_binding_id}-${item.gap.started_at}-${index}`} data-testid="coverage-gap" data-open={item.gap.open ? 'yes' : 'no'}>
            <td data-label="Worker">
              {item.worker_label}
              {item.binding_state === 'revoked' ? <span className="muted small"> (revoked)</span> : null}
            </td>
            <td data-label="Gap">
              <Badge tone={item.gap.open ? 'bad' : 'muted'}>{item.gap.open ? 'open' : 'closed'}</Badge> {label(item.gap.kind)}
            </td>
            <td data-label="Started">
              <Timestamp value={item.gap.started_at} timeZone={timeZone} />
            </td>
            <td data-label="Ended">
              {item.gap.ended_at ? <Timestamp value={item.gap.ended_at} timeZone={timeZone} /> : <span className="muted">still open</span>}
            </td>
            <td data-label="Detected by">{item.gap.detected_by}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
