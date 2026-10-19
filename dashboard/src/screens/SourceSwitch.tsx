/**
 * The per-source on/off switch of the Sources screen, built on pause and resume only.
 *
 * - On  = the server says new network work may start (`network_allowed`; the process-level
 *   `SOURCE_NETWORK_ENABLED` setting still applies). Switching off opens the pause form (reason
 *   required): `POST /api/sources/{id}/pause` (`sources:pause`).
 * - Off because paused: switching on opens the resume confirmation (reason required):
 *   `POST /api/sources/{id}/resume` (owner only, `config:admin`), which also requeues the jobs
 *   that waited for the source. Without `config:admin` the switch is disabled with a short reason.
 * - Off because the configuration does not enable the source, or because its technical status is
 *   `access_blocked` / `parser_unhealthy`: the switch is disabled. Enabling stays a configuration
 *   revision (source YAML + `sources sync`) and a block is cleared only by the owner's explicit
 *   technical-status action; the switch can do neither. Such a source that is not paused yet can
 *   still be paused in advance ("Pause in advance", same pause form and route, `sources:pause`):
 *   the pause survives a later enable by the configuration or a cleared block, so the source stays
 *   off until a deliberate resume.
 *
 * A real `<button role="switch" aria-checked>`: keyboard operable (Enter/Space), named by the
 * visible label, described by the visible state and help text. A disabled switch stays focusable
 * (`aria-disabled`), so a keyboard or screen-reader user still reaches the explanation, including
 * while a pause or resume is being sent or its outcome is not confirmed. Both
 * mutations are idempotent (one key per attempt, same-key retry when the outcome is unknown) and a
 * version conflict is shown with the shared conflict UI.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import {
  LIMITS,
  type PauseSourceRequest,
  type ResumeSourceRequest,
  type SourcePauseResult,
  type SourceResumeResult,
  type SourceStatusView,
  type TechnicalStatus,
} from '../api/types'
import { Timestamp, Warnings } from '../components/ui'
import { label } from '../format'
import { MutationStatus } from '../review/MutationStatus'
import { useIdempotentMutation } from '../review/useIdempotentMutation'
import { useWorkspace } from '../workspace/WorkspaceProvider'

/** Technical statuses that stop every network request until the owner's explicit action clears them. */
const TECHNICAL_BLOCKS: ReadonlySet<TechnicalStatus> = new Set<TechnicalStatus>(['access_blocked', 'parser_unhealthy'])

export type SwitchKind = 'running' | 'paused' | 'not_enabled' | 'technical_block' | 'not_allowed'

/** Why the switch is where it is (the first reason that applies wins). */
export function switchKind(source: SourceStatusView): SwitchKind {
  if (TECHNICAL_BLOCKS.has(source.technical.status)) return 'technical_block'
  if (!source.enabled) return 'not_enabled'
  if (source.paused) return 'paused'
  if (source.network_allowed) return 'running'
  return 'not_allowed'
}

const STATE_LABEL: Record<SwitchKind, string> = {
  running: 'Running',
  paused: 'Paused',
  not_enabled: 'Off (configuration)',
  technical_block: 'Blocked',
  not_allowed: 'Off',
}

const SUBJECT = 'source details'

type Panel = 'pause' | 'resume' | null

/** Which control opened the panel (focus goes back to it on cancel). */
type Opener = 'switch' | 'ahead'

function busy(phase: { kind: string }): boolean {
  return phase.kind === 'pending' || phase.kind === 'unconfirmed'
}

function requeuedText(count: number): string {
  if (count === 0) return 'No waiting job needed requeueing.'
  return `${count} waiting job${count === 1 ? ' was' : 's were'} requeued.`
}

export function resumeConfirmation(result: SourceResumeResult): string {
  if (!result.was_paused) return 'The source was not paused; nothing changed.'
  return `Source resumed. ${requeuedText(result.requeued_jobs)}`
}

export function SourceSwitch({
  source,
  timeZone,
  onChanged,
}: {
  source: SourceStatusView
  timeZone: string
  onChanged: () => void
}) {
  const { client, can } = useWorkspace()
  const id = useId()
  const [panel, setPanel] = useState<Panel>(null)
  const [opener, setOpener] = useState<Opener>('switch')
  const [reason, setReason] = useState('')
  const switchRef = useRef<HTMLButtonElement>(null)
  const aheadRef = useRef<HTMLButtonElement>(null)
  const reasonRef = useRef<HTMLInputElement>(null)
  const settled = (_attempt: unknown, outcome: { envelope: unknown } | { error: unknown }) => {
    if ('envelope' in outcome) {
      setPanel(null)
      setReason('')
      onChanged()
    }
  }
  const pause = useIdempotentMutation<PauseSourceRequest, SourcePauseResult>(
    'source-pause',
    (body) => client.pauseSource(source.source_id, body),
    { settled },
  )
  const resume = useIdempotentMutation<ResumeSourceRequest, SourceResumeResult>(
    'source-resume',
    (body) => client.resumeSource(source.source_id, body),
    { settled },
  )
  useEffect(() => {
    if (panel !== null) reasonRef.current?.focus()
  }, [panel])

  const kind = switchKind(source)
  const on = kind === 'running'
  const locked = busy(pause.phase) || busy(resume.phase)
  const canPause = can('sources:pause')
  const canResume = can('config:admin')
  const permitted = (kind === 'running' && canPause) || (kind === 'paused' && canResume)
  const actionable = permitted && !locked
  // A source that runs no network work now (off in the configuration, technically blocked, or a
  // status that does not allow it) can still be paused: the pause survives a later enable or a
  // cleared block, and only a deliberate resume lifts it. The switch itself stays off for it.
  const canPauseAhead = !source.paused && kind !== 'running' && canPause
  const valid = reason.trim().length >= LIMITS.reasonMin

  const nameId = `${id}-name`
  const stateId = `${id}-state`
  const helpId = `${id}-help`
  const panelId = `${id}-panel`
  const inputId = `${id}-reason`

  const toggle = () => {
    if (!actionable) return
    if (panel !== null) {
      setPanel(null)
      setReason('')
      return
    }
    // An older result of the other direction must not stay on screen next to this one.
    if (kind === 'running') resume.reset()
    else pause.reset()
    setReason('')
    setOpener('switch')
    setPanel(kind === 'running' ? 'pause' : 'resume')
  }
  const pauseAhead = () => {
    if (!canPauseAhead || locked) return
    if (panel !== null) {
      setPanel(null)
      setReason('')
      return
    }
    resume.reset()
    setReason('')
    setOpener('ahead')
    setPanel('pause')
  }
  const cancel = () => {
    setPanel(null)
    setReason('')
    const back = opener === 'ahead' && aheadRef.current ? aheadRef.current : switchRef.current
    back?.focus()
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!valid || locked) return
    const body = { expected_version: source.version, reason: reason.trim() }
    if (panel === 'pause') void pause.submit(body)
    else if (panel === 'resume') void resume.submit(body)
  }
  const reloadOnConflict = (phase: { kind: string; error?: { code: string } }) =>
    phase.kind === 'rejected' && (phase.error?.code === 'VERSION_CONFLICT' || phase.error?.code === 'IDEMPOTENCY_CONFLICT') ? (
      <button type="button" className="button secondary" onClick={onChanged}>
        Reload sources
      </button>
    ) : null

  // " since <time>: <reason>." after "Paused" (sentence start) or "It is also paused" (mid-text).
  const pauseDetail = source.paused ? (
    <>
      {source.paused_at ? (
        <>
          {' '}
          since <Timestamp value={source.paused_at} timeZone={timeZone} />
        </>
      ) : null}
      : {source.pause_reason ?? 'no reason recorded'}.
    </>
  ) : null
  const alsoPaused = source.paused ? (
    <>
      {' '}
      It is also paused{pauseDetail} It stays paused after the source would otherwise start again, until the owner resumes it.
    </>
  ) : null
  const pauseAheadHelp = canPauseAhead ? (
    <>
      {' '}
      You can still pause it in advance (a reason is required and audited): the pause stays when the source would otherwise
      start again, until the owner resumes it.
    </>
  ) : null
  const busyVerb = busy(pause.phase) ? 'pause' : 'resume'
  const busyPhase = busy(pause.phase) ? pause.phase : resume.phase
  const lockedHelp = locked ? (
    <p data-testid="source-switch-locked">
      {busyPhase.kind === 'pending'
        ? `The switch is unavailable while the ${busyVerb} is being sent: wait for the server's answer below.`
        : `The switch is unavailable until the unconfirmed ${busyVerb} is resolved: retry the same request or discard it below.`}
    </p>
  ) : null

  return (
    <div className={`source-switch source-switch-${kind}`} data-testid={`source-switch-${source.source_key}`}>
      <div className="switch-row">
        <button
          ref={switchRef}
          type="button"
          role="switch"
          className="switch"
          aria-checked={on}
          aria-labelledby={nameId}
          aria-describedby={`${stateId} ${helpId}`}
          aria-disabled={actionable ? undefined : true}
          aria-controls={panel !== null && opener === 'switch' ? panelId : undefined}
          onClick={toggle}
        >
          <span className="switch-thumb" aria-hidden="true" />
        </button>
        <span id={nameId} className="switch-name">
          Collect from {source.display_name}
        </span>
        <span id={stateId} className={`badge switch-state switch-state-${kind}`}>
          {STATE_LABEL[kind]}
        </span>
      </div>
      <div id={helpId} className="switch-help small">
        {lockedHelp}
        {kind === 'running' ? (
          <p>
            New network work may start for this source (the process-level <code>SOURCE_NETWORK_ENABLED</code> setting still
            applies).{' '}
            {canPause
              ? 'Switching off pauses it (a reason is required and audited); nothing already captured is removed.'
              : 'Your role cannot pause sources (it needs the sources:pause permission).'}
          </p>
        ) : null}
        {kind === 'paused' ? (
          <p>
            Paused{pauseDetail} No new network work runs for this source.{' '}
            {canResume
              ? 'Switching on resumes it (owner action, reason required) and requeues the jobs that waited for it; it never enables the source or changes its technical status.'
              : 'Resuming is owner-only (it needs the config:admin permission).'}
          </p>
        ) : null}
        {kind === 'not_enabled' ? (
          <p>
            Off in the configuration: a source is switched on only through its YAML file (activation step 7) and{' '}
            <code>suv-deals sources sync</code>, an auditable configuration revision. This switch never enables a source.
            {alsoPaused}
            {pauseAheadHelp}
            {source.activation_problems.length ? ' The activation problems are listed below.' : null}
          </p>
        ) : null}
        {kind === 'technical_block' ? (
          <p>
            Technical status {label(source.technical.status)}: no network work runs. Report the block; it is cleared only by the
            owner&apos;s explicit technical-status action (<code>suv-deals sources set-technical-status</code>), never by this
            switch.{alsoPaused}
            {pauseAheadHelp}
          </p>
        ) : null}
        {kind === 'not_allowed' ? (
          <p>
            Technical status {label(source.technical.status)}: no network work runs until it allows it. This switch cannot change
            it.{pauseAheadHelp}
          </p>
        ) : null}
      </div>
      {canPauseAhead ? (
        <div className="form-actions">
          <button
            ref={aheadRef}
            type="button"
            className="button secondary"
            aria-expanded={panel === 'pause'}
            aria-controls={panel === 'pause' ? panelId : undefined}
            aria-describedby={helpId}
            onClick={pauseAhead}
            disabled={locked}
          >
            Pause in advance
          </button>
        </div>
      ) : null}
      {panel !== null ? (
        <form
          className="form pause-form switch-panel"
          id={panelId}
          onSubmit={submit}
          aria-label={panel === 'pause' ? 'Pause this source' : 'Resume this source'}
        >
          {panel === 'pause' ? (
            <p className="muted small">
              Pausing stops new network work: the scheduler skips this source and a worker blocks a job of it as{' '}
              <code>source_paused</code>. Resuming is a separate owner action.
              {kind !== 'running'
                ? ' This source runs no network work right now; the pause keeps it off after a later enable by the configuration or a cleared technical block, until the owner resumes it.'
                : null}
            </p>
          ) : (
            <p className="muted small">
              Resuming clears the pause (audited). The jobs a worker blocked as <code>source_paused</code> while it was paused
              are requeued. It never enables the source (that stays a configuration revision) and never changes its technical
              status.
            </p>
          )}
          <label htmlFor={inputId}>
            {panel === 'pause' ? 'Pause reason' : 'Resume reason'} (required; recorded in the audit trail)
          </label>
          <input
            id={inputId}
            ref={reasonRef}
            value={reason}
            minLength={LIMITS.reasonMin}
            maxLength={LIMITS.reasonMax}
            onChange={(event) => setReason(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !locked) cancel()
            }}
            disabled={locked}
          />
          <div className="form-actions">
            <button type="submit" className={panel === 'pause' ? 'button danger' : 'button'} disabled={locked || !valid}>
              {panel === 'pause' ? 'Pause this source' : 'Resume this source'}
            </button>
            <button type="button" className="button secondary" onClick={cancel} disabled={locked}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <MutationStatus
        phase={pause.phase}
        subject={SUBJECT}
        onRetry={() => void pause.retry()}
        onDiscard={() => {
          pause.reset()
          onChanged()
        }}
        pendingText="Pausing…"
        confirmed={(result) => <span data-testid="source-paused">{result.already_paused ? 'The source was already paused.' : 'Source paused.'}</span>}
        extraOnRejected={reloadOnConflict(pause.phase)}
      />
      <MutationStatus
        phase={resume.phase}
        subject={SUBJECT}
        onRetry={() => void resume.retry()}
        onDiscard={() => {
          resume.reset()
          onChanged()
        }}
        pendingText="Resuming…"
        confirmed={(result) => <span data-testid="source-resumed">{resumeConfirmation(result)}</span>}
        extraOnRejected={reloadOnConflict(resume.phase)}
      />
      {resume.phase.kind === 'confirmed' ? <Warnings warnings={resume.phase.envelope.warnings} /> : null}
    </div>
  )
}
