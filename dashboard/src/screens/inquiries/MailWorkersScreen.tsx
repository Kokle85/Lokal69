import { useState, type ReactNode } from 'react'
import type { ComponentStatus, LagView, MailboxHealthView, MailCoverageGapItem } from '../../api/types'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../../components/ui'
import { countText, durationText, label, shortHash, yesNo } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import { InquiryAreaNav, LagValue, RequireScope } from './shared'

export function MailWorkersScreen() {
  return (
    <RequireScope scope="inquiries:read" what="mail-worker health">
      <MailWorkersContent />
    </RequireScope>
  )
}

function MailWorkersContent() {
  const { timezone } = useWorkspace()
  const [includeRevoked, setIncludeRevoked] = useState(false)
  const health = useApiQuery((api, signal) => api.mailWorkerHealth({ include_revoked: includeRevoked }, { signal }), [includeRevoked])
  const gaps = useApiQuery((api, signal) => api.mailCoverageGaps({ include_revoked: includeRevoked }, { signal }), [includeRevoked])
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
          {health.data.any_monitoring_active ? (
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
            health.data.mailboxes.map((box) => <MailboxCard key={box.mailbox_binding_id} box={box} timeZone={timezone} />)
          )}
        </>
      ) : null}
      <Section title="Coverage gaps" id="coverage-gaps">
        {gaps.status === 'loading' ? <LoadingState label="Loading coverage gaps" /> : null}
        {gaps.error ? <ErrorPanel error={gaps.error} onRetry={gaps.reload} /> : null}
        {gaps.data ? <GapTable items={gaps.data.items} timeZone={timezone} /> : null}
      </Section>
    </div>
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

function MailboxCard({ box, timeZone }: { box: MailboxHealthView; timeZone: string }) {
  const monitoring = box.monitoring_active
  const fresh = box.heartbeat_status === 'healthy'
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
        {!monitoring ? (
          <Notice tone="bad">
            <span data-testid="mailbox-not-monitoring">
              {box.heartbeat_status === 'healthy'
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
