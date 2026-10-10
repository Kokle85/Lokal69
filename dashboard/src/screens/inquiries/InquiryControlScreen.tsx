import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { suppressionsChanged } from '../../api/errors'
import {
  LIMITS,
  type InquiryControlView,
  type InquiryPauseRequest,
  type InquiryPauseResult,
  type InquiryResumeRequest,
  type InquiryResumeResult,
} from '../../api/types'
import { useAuth } from '../../auth/AuthProvider'
import { Badge, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../../components/ui'
import { durationText, label } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { MutationStatus } from '../../review/MutationStatus'
import { useIdempotentMutation } from '../../review/useIdempotentMutation'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import {
  clearPendingControl,
  readPendingControl,
  resolvePendingControl,
  writePendingControl,
  type PendingControlAction,
} from './controlMarker'
import {
  authorizationText,
  InquiryAreaNav,
  RequireScope,
  sendingReadinessProblems,
  SenderProblemList,
  senderReadinessLabel,
  senderReadinessText,
  StandingAuthorizationNote,
} from './shared'

const SUBJECT = 'inquiry controls'

export function InquiryControlScreen() {
  return (
    <RequireScope scope="inquiries:read" what="the seller-inquiry controls">
      <InquiryControlContent />
    </RequireScope>
  )
}

function InquiryControlContent() {
  const { timezone, workspaceId } = useWorkspace()
  const { userId } = useAuth()
  const control = useApiQuery((api, signal) => api.inquiryControl({ signal }), [])
  // The marker of an unconfirmed pause/resume from BEFORE a page reload (read once on mount).
  const [earlier] = useState<PendingControlAction | null>(() => readPendingControl(workspaceId, userId))

  return (
    <div className="screen">
      <h1>Inquiry control</h1>
      <InquiryAreaNav />
      <StandingAuthorizationNote />
      {control.status === 'loading' ? <LoadingState label="Loading the inquiry controls" /> : null}
      {control.error?.code === 'NOT_FOUND' ? (
        <Notice tone="info">
          <span data-testid="controls-missing">
            The seller-inquiry controls are not set up for this workspace yet, so nothing can be sent. The owner records the
            standing authorization with <code>suv-deals inquiries authorize</code>, which creates them.
          </span>
        </Notice>
      ) : control.error ? (
        <ErrorPanel error={control.error} onRetry={control.reload} />
      ) : null}
      {control.data && control.envelope ? (
        <>
          <ViewMeta
            asOf={control.envelope.as_of}
            fetchedAt={control.fetchedAt}
            timeZone={timezone}
            onReload={control.reload}
            reloading={control.reloading}
          />
          <Warnings warnings={control.envelope.warnings} />
          {earlier ? <EarlierActionNotice marker={earlier} control={control.data} onReload={control.reload} /> : null}
          <ControlState control={control.data} timeZone={timezone} />
          <ReadinessState control={control.data} />
          <ControlActions control={control.data} onChanged={control.reload} userId={userId ?? ''} />
        </>
      ) : null}
    </div>
  )
}

function sendingStopped(control: InquiryControlView): boolean {
  return control.kill_switch || control.mode !== 'automatic'
}

function ControlState({ control, timeZone }: { control: InquiryControlView; timeZone: string }) {
  const notReady = sendingReadinessProblems(control)
  return (
    <Section title="Current controls (read-only)" id="control-state">
      {sendingStopped(control) ? (
        <Notice tone="warn">
          <span data-testid="sending-state">
            Sending is stopped:{' '}
            {control.kill_switch ? 'the inquiry kill switch is on' : `the mode is ${label(control.mode)}, not automatic`}. Untransmitted
            work stays unsent; nothing already sent is recalled.
          </span>
        </Notice>
      ) : notReady.length ? (
        <Notice tone="warn">
          <span data-testid="sending-state">
            These controls allow automatic inquiries, but nothing can be sent now: {notReady.join('; ')}. This is technical setup,
            never a message approval; see the readiness below.
          </span>
        </Notice>
      ) : (
        <Notice tone="info">
          <span data-testid="sending-state">
            These controls allow automatic inquiries: the standing authorization is active and the configured sender is ready. Each
            send still passes the rolling caps, the seller cooldown and every guard again immediately before transmission.
          </span>
        </Notice>
      )}
      <KeyValues
        items={[
          ['Mode', <Badge key="m" value={control.mode} />],
          [
            'Kill switch',
            control.kill_switch ? (
              <span key="k">
                <Badge tone="bad">on</Badge> since <Timestamp value={control.kill_switch_set_at} timeZone={timeZone} />
              </span>
            ) : (
              <Badge key="k" tone="ok">
                off
              </Badge>
            ),
          ],
          ['Kill switch reason', control.kill_switch_reason ? <span key="r" className="untrusted">{control.kill_switch_reason}</span> : 'none'],
          [
            'Approval',
            <span key="a" data-testid="control-approval">
              none: standing authorization, no per-message approval
            </span>,
          ],
          ['Inquiries in the last 24 hours', `${control.used_24h} used of ${control.max_per_24h} (hard ceiling ${control.ceiling_per_24h})`],
          ['Inquiries in the rolling 15 days', `${control.used_15d} used of ${control.max_per_15d} (hard ceiling ${control.ceiling_per_15d})`],
          ['Seller cooldown', durationText(control.seller_cooldown_seconds)],
          ['Suppressions a resume could remove', String(control.removable_suppressions)],
          ['Control version', String(control.version)],
          ['Updated', <Timestamp key="u" value={control.updated_at} timeZone={timeZone} />],
        ]}
      />
      <p className="muted small">
        The caps are ceilings, not targets. The owner can lower them (CLI) but never raise them above 2 per 24 hours and 5 per
        rolling 15 days.
      </p>
    </Section>
  )
}

/**
 * The technical prerequisites of automatic sending (read-only): the standing authorization and the
 * CONFIGURED sending identity's readiness. Neither is a message approval; the owner sets them up
 * with the CLI.
 */
function ReadinessState({ control }: { control: InquiryControlView }) {
  const authorized = control.authorization_status === 'active'
  const senderReady = control.sender_readiness === 'ready'
  return (
    <Section title="Sending readiness (read-only)" id="control-readiness">
      <KeyValues
        items={[
          [
            'Standing authorization',
            <span key="a" data-testid="authorization-status" data-status={control.authorization_status}>
              <Badge tone={authorized ? 'ok' : 'bad'}>{authorizationText(control.authorization_status)}</Badge>
              {control.authorization_version !== null ? <span className="muted"> version {control.authorization_version}</span> : null}
            </span>,
          ],
          [
            'Configured sender',
            <span key="s" data-testid="sender-readiness" data-readiness={control.sender_readiness}>
              <Badge tone={senderReady ? 'ok' : 'bad'}>{senderReadinessLabel(control.sender_readiness)}</Badge>
              <span className="muted">
                {' '}
                {senderReadinessText(control.sender_readiness)} · {control.sender_provider ? label(control.sender_provider) : 'no provider'}
                {control.sender_binding_version !== null ? ` · binding version ${control.sender_binding_version}` : ''}
              </span>
            </span>,
          ],
          [
            'Sender problems',
            control.sender_problems.length ? <SenderProblemList key="p" codes={control.sender_problems} /> : 'none',
          ],
        ]}
      />
      <p className="muted small">
        These are technical prerequisites, never a message approval: the standing authorization covers one automatic initial
        inquiry per verified vehicle/seller pair. The owner records the authorization with <code>suv-deals inquiries authorize</code>{' '}
        and sets up the sending identity with <code>suv-deals sender-binding</code> (no address is shown here).
      </p>
    </Section>
  )
}

function EarlierActionNotice({
  marker,
  control,
  onReload,
}: {
  marker: PendingControlAction
  control: InquiryControlView
  onReload: () => void
}) {
  const resolution = resolvePendingControl(marker, control)
  useEffect(() => {
    if (resolution !== 'not_applied') clearPendingControl(marker.workspaceId)
  }, [resolution, marker.workspaceId])
  const what = marker.action === 'pause' ? 'pause' : 'resume'
  return (
    <div className="notice notice-warn" role="status" data-testid="earlier-control-action">
      <p className="panel-title">A {what} was being sent when this page was reloaded</p>
      <p>Nothing was resent automatically.</p>
      {resolution === 'applied' ? (
        <p data-testid="earlier-control-applied">
          The controls are now {marker.action === 'pause' ? 'paused' : 'resumed'} (version {control.version}): the server applied a{' '}
          {what} at or after that request.
        </p>
      ) : resolution === 'not_applied' ? (
        <>
          <p data-testid="earlier-control-not-applied">
            As of this page load the server has not applied it (the controls are still at version {control.version}). You can
            send it again below.
          </p>
          <button type="button" className="button secondary" onClick={onReload}>
            Check again
          </button>
        </>
      ) : resolution === 'indeterminate' ? (
        <p data-testid="earlier-control-indeterminate">
          The kill switch was already off, so the controls look the same either way (still version {control.version}): this page
          cannot tell whether that re-qualification was applied. Repeating it below is safe: it only re-qualifies inquiries that
          are still suppressed, each audited, and never sends anything itself.
        </p>
      ) : (
        <p>The controls have changed since (now version {control.version}); review the current state below.</p>
      )}
    </div>
  )
}

function busy(phase: { kind: string }): boolean {
  return phase.kind === 'pending' || phase.kind === 'unconfirmed'
}

function ControlActions({ control, onChanged, userId }: { control: InquiryControlView; onChanged: () => void; userId: string }) {
  const { client, can, workspaceId } = useWorkspace()
  const [pauseReason, setPauseReason] = useState('')
  const [resumeReason, setResumeReason] = useState('')
  // The removable-suppression count the owner CONFIRMED by ticking the box (`null`: no removal).
  // It is sent as `expected_removable_suppressions`, so the server removes only the set the owner
  // saw; when the shown count moves away from it, the owner must confirm the new count first.
  const [confirmedRemoval, setConfirmedRemoval] = useState<number | null>(null)
  // The tick no longer stands for what is removable now: the shown count moved away from it (even
  // if it later comes back: the same NUMBER need not be the same suppressions), or the server
  // refused the resume because the removable set changed. Only a new tick confirms again.
  const [confirmationVoid, setConfirmationVoid] = useState(false)
  // After a `suppressions_changed` refusal: the controls object that was refused. The resume stays
  // locked until the reloaded controls (a new object) are on screen, so nothing is retried against
  // the stale count.
  const [refusedControl, setRefusedControl] = useState<InquiryControlView | null>(null)
  const [refusedCounts, setRefusedCounts] = useState<{ expected: number | null; current: number | null } | null>(null)
  const controlRef = useRef(control)
  useEffect(() => {
    controlRef.current = control
  })
  const marker = (action: 'pause' | 'resume') => ({
    beforeSend: (attempt: { key: string; body: { expected_version: number } }) =>
      writePendingControl({
        workspaceId,
        userId,
        action,
        idempotencyKey: attempt.key,
        expectedVersion: attempt.body.expected_version,
        startedAt: new Date().toISOString(),
      }),
  })
  const pause = useIdempotentMutation<InquiryPauseRequest, InquiryPauseResult>('inquiry-pause', (body) => client.pauseInquiries(body), {
    ...marker('pause'),
    settled: (_attempt, outcome) => {
      clearPendingControl(workspaceId)
      if ('envelope' in outcome) {
        setPauseReason('')
        onChanged()
      }
    },
  })
  const resume = useIdempotentMutation<InquiryResumeRequest, InquiryResumeResult>(
    'inquiry-resume',
    (body) => client.resumeInquiries(body),
    {
      ...marker('resume'),
      settled: (_attempt, outcome) => {
        clearPendingControl(workspaceId)
        if ('envelope' in outcome) {
          setResumeReason('')
          setConfirmedRemoval(null)
          setConfirmationVoid(false)
          setRefusedCounts(null)
          setRefusedControl(null)
          onChanged()
          return
        }
        const changed = suppressionsChanged(outcome.error)
        if (changed) {
          // Nothing changed on the server: reload and show the CURRENT set before any new attempt.
          // The owner's tick confirmed a set the server says is gone: it never counts again.
          setConfirmationVoid(true)
          setRefusedCounts(changed)
          setRefusedControl(controlRef.current)
          onChanged()
        }
      },
    },
  )
  const locked = busy(pause.phase) || busy(resume.phase)
  const canPause = can('inquiries:pause')
  const canResume = can('config:admin')
  const pauseValid = pauseReason.trim().length >= LIMITS.reasonMin
  const resumeValid = resumeReason.trim().length >= LIMITS.reasonMin
  const awaitingReload = refusedControl !== null && refusedControl === control
  const removalCount = control.removable_suppressions
  const countMoved = confirmedRemoval !== null && confirmedRemoval !== removalCount
  // Sticky (state adjusted while rendering, guarded): a count that moves away and back is not the
  // set the owner ticked.
  if (countMoved && !confirmationVoid) setConfirmationVoid(true)
  // The tick no longer confirms what is shown (a reload, a refused resume, a count that moved).
  const removalMoved = confirmedRemoval !== null && (countMoved || confirmationVoid)
  const resumeBlocked = locked || awaitingReload || removalMoved
  const reloadOnConflict = (error: { code: string }) =>
    error.code === 'VERSION_CONFLICT' || error.code === 'IDEMPOTENCY_CONFLICT' ? (
      <button type="button" className="button secondary" onClick={onChanged}>
        Reload the controls
      </button>
    ) : null

  // The owner resumes a paused workspace, and can also resume while the kill switch is already off
  // when suppressions a resume removes are still active (e.g. after a CLI resume without removal, or
  // a re-authorization): the view counts them, so the action must exist here too.
  const showResume = canResume && (control.kill_switch || control.removable_suppressions > 0)
  const resumeLabel = control.kill_switch ? 'Resume seller inquiries' : 'Re-qualify suppressed inquiries'

  return (
    <Section title="Pause and resume" id="control-actions">
      {!control.kill_switch ? (
        canPause ? (
          <form
            className="form pause-form"
            onSubmit={(event) => {
              event.preventDefault()
              if (pauseValid && !locked) {
                resume.reset() // an older resume result must not stay on screen next to this pause
                void pause.submit({ expected_version: control.version, reason: pauseReason.trim() })
              }
            }}
          >
            <p className="muted small">
              Pausing turns the inquiry kill switch on: untransmitted work stops at the next guard. Resuming is a separate owner
              decision.
            </p>
            <label htmlFor="inquiry-pause-reason">Pause reason (required; recorded in the audit trail)</label>
            <input
              id="inquiry-pause-reason"
              value={pauseReason}
              minLength={LIMITS.reasonMin}
              maxLength={LIMITS.reasonMax}
              onChange={(event) => setPauseReason(event.target.value)}
              disabled={locked}
            />
            <div className="form-actions">
              <button type="submit" className="button danger" disabled={locked || !pauseValid}>
                Pause seller inquiries
              </button>
            </div>
          </form>
        ) : (
          <p className="muted">Your role cannot pause seller inquiries (it needs the inquiries:pause permission).</p>
        )
      ) : null}
      {showResume ? (
        <form
          className="form pause-form"
          data-testid="resume-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (resumeValid && !resumeBlocked) {
              pause.reset() // an older pause result must not stay on screen next to this resume
              const body: Omit<InquiryResumeRequest, 'idempotency_key'> = {
                expected_version: control.version,
                reason: resumeReason.trim(),
                remove_suppressions: confirmedRemoval !== null,
              }
              // Exactly the count the owner was shown and confirmed (compared under the controls lock).
              if (confirmedRemoval !== null) body.expected_removable_suppressions = confirmedRemoval
              setRefusedCounts(null)
              void resume.submit(body)
            }
          }}
        >
          {control.kill_switch ? (
            <p className="muted small">
              Resuming turns the kill switch off (owner only, dashboard only). The mode stays {label(control.mode)}. Nothing is
              sent by the resume itself: inquiries are re-qualified and pass every guard again.
            </p>
          ) : (
            <p className="muted small">
              The kill switch is already off, but {control.removable_suppressions} suppression(s) from the kill switch or an
              earlier authorization revocation are still active. This owner action (a resume) re-qualifies the inquiries they
              stopped, each audited, and can remove them. Nothing is sent by it: every inquiry passes every guard again.
            </p>
          )}
          <label htmlFor="inquiry-resume-reason">Resume reason (required; recorded in the audit trail)</label>
          <input
            id="inquiry-resume-reason"
            value={resumeReason}
            minLength={LIMITS.reasonMin}
            maxLength={LIMITS.reasonMax}
            onChange={(event) => setResumeReason(event.target.value)}
            disabled={locked}
          />
          {awaitingReload ? (
            <p className="muted small" role="status" data-testid="resume-awaiting-reload">
              Reloading the controls to show the current suppressions before anything can be resumed…
            </p>
          ) : null}
          {refusedCounts && !awaitingReload ? (
            <Notice tone="warn">
              <span data-testid="suppressions-changed">
                The suppressions a resume would remove changed
                {refusedCounts.expected !== null ? `: you confirmed ${refusedCounts.expected}` : ''}, the current count is{' '}
                {removalCount}. Nothing was resumed or removed. Review the current count below and confirm it again.
              </span>
            </Notice>
          ) : null}
          {removalMoved && !awaitingReload ? (
            <p className="form-error" role="alert" data-testid="removal-count-moved">
              {countMoved
                ? `You ticked the removal for ${confirmedRemoval} suppression(s); the current count is ${removalCount}.`
                : `The removable suppressions changed after you ticked the removal; the current count is ${removalCount} again, but it may not be the same set.`}{' '}
              Untick and tick the box again to confirm the current count
              {removalCount === 0 ? ' (or untick it: there is nothing left to remove)' : ''}.
            </p>
          ) : null}
          {removalCount > 0 || confirmedRemoval !== null ? (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={confirmedRemoval !== null}
                onChange={(event) => {
                  setConfirmedRemoval(event.target.checked ? removalCount : null)
                  setConfirmationVoid(false)
                }}
                disabled={locked || awaitingReload}
              />{' '}
              Also remove the {removalCount} kill-switch / revoked-authorization suppression(s), each with its own audit record.
              Opt-outs, bounces, complaints and other suppressions are never removed here.
            </label>
          ) : (
            <p className="muted small">No suppression can be removed with this resume.</p>
          )}
          <div className="form-actions">
            <button type="submit" className="button" disabled={resumeBlocked || !resumeValid}>
              {resumeLabel}
            </button>
          </div>
        </form>
      ) : control.kill_switch ? (
        <p className="muted" data-testid="resume-owner-only">
          Seller inquiries are paused. Resuming is an owner-only action on this dashboard.
        </p>
      ) : null}
      <MutationStatus
        phase={pause.phase}
        subject={SUBJECT}
        onRetry={() => void pause.retry()}
        onDiscard={() => {
          clearPendingControl(workspaceId)
          pause.reset()
          onChanged()
        }}
        pendingText="Pausing…"
        confirmed={(result) => (
          <span data-testid="pause-confirmed">
            {result.already_paused ? 'Seller inquiries were already paused.' : 'Seller inquiries paused.'} Control version{' '}
            {result.version}. {result.notice}
          </span>
        )}
        extraOnRejected={pause.phase.kind === 'rejected' ? reloadOnConflict(pause.phase.error) : null}
      />
      <MutationStatus
        phase={resume.phase}
        subject={SUBJECT}
        onRetry={() => void resume.retry()}
        onDiscard={() => {
          clearPendingControl(workspaceId)
          resume.reset()
          onChanged()
        }}
        pendingText="Resuming…"
        confirmed={(result) => (
          <span data-testid="resume-confirmed">
            Seller inquiries resumed (control version {result.version}, mode {label(result.mode)}).{' '}
            {result.suppressions_removed > 0
              ? `${result.suppressions_removed} suppression(s) removed, each audited.`
              : 'No suppression was removed.'}
          </span>
        )}
        extraOnRejected={resume.phase.kind === 'rejected' ? reloadOnConflict(resume.phase.error) : null}
      />
      <p className="muted small">
        See <Link to="/inquiries?attention_only=true">inquiries that need attention</Link> for suppressed and held inquiries.
      </p>
    </Section>
  )
}
