/**
 * The Sources screen's per-source on/off switch (SourceSwitch): every switch state, the pause and
 * resume flows, keyboard operation, permissions, conflicts and the unknown-outcome retry.
 * SYNTHETIC data only.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { MeView, SourceStatusView } from '../api/types'
import { switchKind } from '../screens/SourceSwitch'
import { apiError, fakeApi, ok } from './fakeApi'
import { me, pausedSource, resumeResult, SOURCE_ID, sourceStatus } from './fixtures'
import { renderApp } from './renderApp'

function card(name: string): HTMLElement {
  const heading = screen.getByRole('heading', { name, level: 2 })
  const section = heading.closest('section')
  if (!section) throw new Error(`no section for ${name}`)
  return section
}

function renderSources(source: SourceStatusView | (() => SourceStatusView), who: MeView = me('owner')) {
  const api = fakeApi({
    'GET /api/me': () => ok(who),
    'GET /api/sources': () => ok({ items: [typeof source === 'function' ? source() : source] }),
  })
  renderApp('/sources', { api })
  return api
}

async function findSwitch(displayName: string): Promise<HTMLElement> {
  return screen.findByRole('switch', { name: `Collect from ${displayName}` })
}

describe('switch state', () => {
  it('derives the state from the server fields, technical block first', () => {
    expect(switchKind(sourceStatus())).toBe('running')
    expect(switchKind(pausedSource())).toBe('paused')
    expect(switchKind(sourceStatus({ enabled: false, network_allowed: false, state: 'disabled' }))).toBe('not_enabled')
    expect(switchKind(pausedSource({ enabled: false }))).toBe('not_enabled')
    const blocked = sourceStatus({ enabled: false, network_allowed: false, state: 'blocked' })
    expect(switchKind({ ...blocked, technical: { ...blocked.technical, status: 'access_blocked' } })).toBe('technical_block')
    expect(switchKind({ ...blocked, paused: true, technical: { ...blocked.technical, status: 'parser_unhealthy' } })).toBe(
      'technical_block',
    )
    // Never "on" unless the server says new network work may start.
    expect(switchKind(sourceStatus({ network_allowed: false }))).toBe('not_allowed')
  })

  it('ON: a running source is checked and named, and its state and help are announced', async () => {
    renderSources(sourceStatus())
    const toggle = await findSwitch('SYNTHETIC running source')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(toggle).not.toHaveAttribute('aria-disabled')
    expect(toggle.tagName).toBe('BUTTON')
    expect(toggle).toHaveAccessibleDescription(/Running New network work runs for this source/)
    expect(within(card('SYNTHETIC running source')).getByText('Running')).toBeInTheDocument()
  })

  it('OFF because paused: shows the reason and time; an owner may switch it on', async () => {
    renderSources(pausedSource())
    const toggle = await findSwitch('SYNTHETIC paused source')
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle).not.toHaveAttribute('aria-disabled')
    const section = card('SYNTHETIC paused source')
    expect(within(section).getByText('Paused')).toBeInTheDocument()
    expect(section).toHaveTextContent(/Paused since .*: SYNTHETIC owner pause for maintenance\./)
    expect(section).toHaveTextContent(/requeues the jobs that waited for it/)
  })

  it('OFF because paused, without config:admin: disabled with a short explanation', async () => {
    const pauser: MeView = { ...me('owner'), scopes: ['deals:read', 'reviews:read', 'sources:pause'] }
    renderSources(pausedSource(), pauser)
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC paused source')
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(/Resuming is owner-only \(it needs the config:admin permission\)/)
    await user.click(toggle)
    expect(screen.queryByLabelText(/Resume reason/)).toBeNull()
  })

  it('OFF because not enabled in the configuration: disabled; enabling is a YAML revision', async () => {
    const api = renderSources(
      sourceStatus({
        display_name: 'SYNTHETIC disabled source',
        enabled: false,
        network_allowed: false,
        state: 'disabled',
        activation_problems: ['source is disabled in configuration'],
      }),
    )
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC disabled source')
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(/switched on only through its YAML file \(activation step 7\) and suv-deals sources sync/)
    expect(toggle).toHaveAccessibleDescription(/This switch never enables a source/)
    await user.click(toggle)
    await user.keyboard(' ')
    expect(screen.queryByRole('form')).toBeNull()
    expect(api.calls.filter((call) => call.method === 'POST')).toEqual([])
  })

  it.each(['access_blocked', 'parser_unhealthy'] as const)(
    'OFF because %s: disabled; the block is cleared only by the owner, never by the switch',
    async (status) => {
      const base = sourceStatus({ display_name: 'SYNTHETIC blocked source', enabled: false, network_allowed: false, state: 'blocked' })
      const api = renderSources({ ...base, technical: { ...base.technical, status } })
      const user = userEvent.setup()
      const toggle = await findSwitch('SYNTHETIC blocked source')
      expect(toggle).toHaveAttribute('aria-checked', 'false')
      expect(toggle).toHaveAttribute('aria-disabled', 'true')
      expect(within(card('SYNTHETIC blocked source')).getByText('Blocked')).toBeInTheDocument()
      expect(toggle).toHaveAccessibleDescription(/Report the block; it is cleared only by the owner's explicit technical-status action/)
      expect(toggle).toHaveAccessibleDescription(/never by this switch/)
      await user.click(toggle)
      expect(api.calls.filter((call) => call.method === 'POST')).toEqual([])
    },
  )

  it('a viewer sees the state read-only', async () => {
    renderSources(sourceStatus(), me('viewer'))
    const toggle = await findSwitch('SYNTHETIC running source')
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(/Your role cannot pause sources/)
  })
})

describe('switching off (pause)', () => {
  it('is keyboard operable: Space opens the reason form, Escape cancels back to the switch', async () => {
    const api = renderSources(sourceStatus())
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC running source')
    toggle.focus()
    await user.keyboard(' ')
    const reason = screen.getByLabelText(/Pause reason/)
    expect(reason).toHaveFocus()
    expect(toggle).toHaveAttribute('aria-controls')
    expect(screen.getByRole('button', { name: 'Pause this source' })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(screen.queryByLabelText(/Pause reason/)).toBeNull()
    expect(toggle).toHaveFocus()
    await user.keyboard('{Enter}')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByLabelText(/Pause reason/)).toBeNull()
    expect(api.callsTo('POST /api/sources/:id/pause')).toEqual([])
  })

  it('pauses with the reason and expected version, then reloads and shows the switch off', async () => {
    let paused = false
    const api = renderSources(() => (paused ? pausedSource({ display_name: 'SYNTHETIC running source', version: 5 }) : sourceStatus()))
    api.on('POST /api/sources/:id/pause', () => {
      paused = true
      return ok({ source_id: SOURCE_ID, source_key: 'synthetic_source', paused: true, already_paused: false, version: 5, paused_at: '2026-10-07T10:00:00Z', reason: 'SYNTHETIC drift', notice: 'Paused.' })
    })
    const user = userEvent.setup()
    await user.click(await findSwitch('SYNTHETIC running source'))
    await user.type(screen.getByLabelText(/Pause reason/), 'SYNTHETIC drift')
    await user.click(screen.getByRole('button', { name: 'Pause this source' }))
    expect(await screen.findByTestId('source-paused')).toHaveTextContent('Source paused.')
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Collect from SYNTHETIC running source' })).toHaveAttribute('aria-checked', 'false'))
    const [call] = api.callsTo('POST /api/sources/:id/pause')
    expect(call?.body).toMatchObject({ expected_version: 4, reason: 'SYNTHETIC drift' })
    const sent = call?.body as { idempotency_key: string } | undefined
    expect(call?.headers.get('Idempotency-Key')).toBe(sent?.idempotency_key)
    expect(api.callsTo('GET /api/sources')).toHaveLength(2)
  })
})

describe('switching on (resume)', () => {
  it('resumes with a reason, then reloads and says how many waiting jobs were requeued', async () => {
    let resumed = false
    const api = renderSources(() =>
      resumed ? sourceStatus({ display_name: 'SYNTHETIC paused source', version: 5 }) : pausedSource(),
    )
    api.on('POST /api/sources/:id/resume', () => {
      resumed = true
      return ok(resumeResult({ requeued_jobs: 2 }))
    })
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC paused source')
    await user.click(toggle)
    const submit = screen.getByRole('button', { name: 'Resume this source' })
    expect(submit).toBeDisabled()
    expect(screen.getByRole('form', { name: 'Resume this source' })).toHaveTextContent(/never enables the source/)
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC maintenance done')
    await user.click(submit)
    expect(await screen.findByTestId('source-resumed')).toHaveTextContent('Source resumed. 2 waiting jobs were requeued.')
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Collect from SYNTHETIC paused source' })).toHaveAttribute('aria-checked', 'true'))
    const [call] = api.callsTo('POST /api/sources/:id/resume')
    expect(call?.body).toMatchObject({ expected_version: 4, reason: 'SYNTHETIC maintenance done' })
    const sent = call?.body as { idempotency_key: string } | undefined
    expect(String(sent?.idempotency_key)).toMatch(/^source-resume:/)
    expect(api.callsTo('POST /api/sources/:id/pause')).toEqual([])
  })

  it('shows the server warnings of a resume that leaves the source off', async () => {
    const api = renderSources(pausedSource())
    api.on('POST /api/sources/:id/resume', () =>
      ok(resumeResult({ requeued_jobs: 1, enabled: false, network_allowed: false }), 200, {
        warnings: [{ code: 'ACTIVATION_BLOCKED', message: 'SYNTHETIC: not enabled by its configuration; `sources sync` enables.' }],
      }),
    )
    const user = userEvent.setup()
    await user.click(await findSwitch('SYNTHETIC paused source'))
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC resume')
    await user.click(screen.getByRole('button', { name: 'Resume this source' }))
    expect(await screen.findByTestId('source-resumed')).toHaveTextContent('1 waiting job was requeued.')
    expect(screen.getByText(/not enabled by its configuration/)).toBeInTheDocument()
  })

  it('a version conflict is shown with the conflict UI and a reload', async () => {
    const api = renderSources(pausedSource())
    api.on('POST /api/sources/:id/resume', () =>
      apiError(409, 'VERSION_CONFLICT', 'The source changed; reload and retry', { details: { current_version: 6 } }),
    )
    const user = userEvent.setup()
    await user.click(await findSwitch('SYNTHETIC paused source'))
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC resume')
    await user.click(screen.getByRole('button', { name: 'Resume this source' }))
    const rejected = await screen.findByTestId('mutation-rejected')
    expect(rejected).toHaveTextContent(/The source details changed/)
    expect(rejected).toHaveTextContent(/Nothing was saved/)
    await user.click(within(rejected).getByRole('button', { name: 'Reload sources' }))
    await waitFor(() => expect(api.callsTo('GET /api/sources')).toHaveLength(2))
  })

  it('an unknown outcome is retried with the same key and body, never a new attempt', async () => {
    let attempts = 0
    const api = renderSources(pausedSource())
    api.on('POST /api/sources/:id/resume', () => {
      attempts += 1
      if (attempts === 1) throw new TypeError('network connection lost') // committed; the answer never arrived
      return ok(resumeResult({ requeued_jobs: 0 }))
    })
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC paused source')
    await user.click(toggle)
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC resume')
    await user.click(screen.getByRole('button', { name: 'Resume this source' }))
    expect(await screen.findByTestId('mutation-unconfirmed')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-disabled', 'true') // nothing new while the outcome is unknown
    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('source-resumed')).toHaveTextContent('No waiting job needed requeueing.')
    const [first, second] = api.callsTo('POST /api/sources/:id/resume')
    expect(second?.rawBody).toBe(first?.rawBody)
    expect(second?.headers.get('Idempotency-Key')).toBe(first?.headers.get('Idempotency-Key'))
  })
})
