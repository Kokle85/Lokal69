/**
 * The per-source on/off switch of the Sources screen, built on pause and resume only.
 *
 * - On  = the server says new network work may start (`network_allowed`). Switching off opens the
 *   pause form (reason required): `POST /api/sources/{id}/pause` (`sources:pause`).
 * - Off because paused: switching on opens the resume confirmation (reason required):
 *   `POST /api/sources/{id}/resume` (owner only, `config:admin`), which also requeues the jobs
 *   that waited for the source. Without `config:admin` the switch is disabled with a short reason.
 * - Off because the configuration does not enable the source, or because its technical status is
 *   `access_blocked` / `parser_unhealthy`: the switch is disabled. Enabling stays a configuration
 *   revision (source YAML + `sources sync`) and a block is cleared only by the owner's explicit
 *   technical-status action; the switch can do neither.
 *
 * A real `<button role="switch" aria-checked>`: keyboard operable (Enter/Space), named by the
 * visible label, described by the visible state and help text. A disabled switch stays focusable
 * (`aria-disabled`), so a keyboard or screen-reader user still reaches the explanation. Both
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
  const [reason, setReason] = useState('')
  const switchRef = useRef<HTMLButtonElement>(null)
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
    setPanel(kind === 'running' ? 'pause' : 'resume')
  }
  const cancel = () => {
    setPanel(null)
    setReason('')
    switchRef.current?.focus()
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

  const pausedSince = source.paused ? (
    <>
      Paused{source.paused_at ? (
        <>
          {' '}
          since <Timestamp value={source.paused_at} timeZone={timeZone} />
        </>
      ) : null}
      : {source.pause_reason ?? 'no reason recorded'}.
    </>
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
          aria-controls={panel !== null ? panelId : undefined}
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
        {kind === 'running' ? (
          <p>
            New network work runs for this source.{' '}
            {canPause
              ? 'Switching off pauses it (a reason is required and audited); nothing already captured is removed.'
              : 'Your role cannot pause sources (it needs the sources:pause permission).'}
          </p>
        ) : null}
        {kind === 'paused' ? (
          <p>
            {pausedSince} No new network work runs for this source.{' '}
            {canResume
              ? 'Switching on resumes it (owner action, reason required) and requeues the jobs that waited for it; it never enables the source or changes its technical status.'
              : 'Resuming is owner-only (it needs the config:admin permission).'}
          </p>
        ) : null}
        {kind === 'not_enabled' ? (
          <p>
            Off in the configuration: a source is switched on only through its YAML file (activation step 7) and{' '}
            <code>suv-deals sources sync</code>, an auditable configuration revision. This switch never enables a source.
            {source.paused ? <> It is also {pausedSince}</> : null}
            {source.activation_problems.length ? ' The activation problems are listed below.' : null}
          </p>
        ) : null}
        {kind === 'technical_block' ? (
          <p>
            Technical status {label(source.technical.status)}: no network work runs. Report the block; it is cleared only by the
            owner&apos;s explicit technical-status action (<code>suv-deals sources set-technical-status</code>), never by this
            switch.{source.paused ? <> It is also {pausedSince}</> : null}
          </p>
        ) : null}
        {kind === 'not_allowed' ? (
          <p>Technical status {label(source.technical.status)}: no network work runs until it allows it. This switch cannot change it.</p>
        ) : null}
      </div>
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
