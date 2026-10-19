/**
 * The Sources screen's per-source on/off switch (SourceSwitch): every switch state, the pause and
 * resume flows, the pause in advance of a source that runs no network work now, keyboard
 * operation, permissions, conflicts, the unknown-outcome retry and the switch's explanation while
 * a mutation is in flight. SYNTHETIC data only.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { MeView, SourceStatusView } from '../api/types'
import { switchKind } from '../screens/SourceSwitch'
import { apiError, deferred, fakeApi, ok } from './fakeApi'
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

/** Off in the configuration (enabled=false), not paused. */
function disabledInConfig(overrides: Partial<SourceStatusView> = {}): SourceStatusView {
  return sourceStatus({
    display_name: 'SYNTHETIC disabled source',
    enabled: false,
    network_allowed: false,
    state: 'disabled',
    activation_problems: ['source is disabled in configuration'],
    ...overrides,
  })
}

/** Technically blocked (which also disables it), not paused unless overridden. */
function blockedSource(status: 'access_blocked' | 'parser_unhealthy', overrides: Partial<SourceStatusView> = {}): SourceStatusView {
  const base = sourceStatus({ display_name: 'SYNTHETIC blocked source', enabled: false, network_allowed: false, state: 'blocked', ...overrides })
  return { ...base, technical: { ...base.technical, status } }
}

function pauseResult(version: number, reason = 'SYNTHETIC pause'): Response {
  return ok({
    source_id: SOURCE_ID,
    source_key: 'synthetic_source',
    paused: true,
    already_paused: false,
    version,
    paused_at: '2026-10-07T10:00:00Z',
    reason,
    notice: 'Paused.',
  })
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
    // "may start": the process-level SOURCE_NETWORK_ENABLED setting is outside this per-source switch.
    expect(toggle).toHaveAccessibleDescription(
      /Running New network work may start for this source \(the process-level SOURCE_NETWORK_ENABLED setting still applies\)/,
    )
    expect(screen.queryByRole('button', { name: 'Pause in advance' })).toBeNull() // the switch itself pauses it
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
    expect(within(section).queryByRole('button', { name: 'Pause in advance' })).toBeNull()
  })

  it.each([
    ['off in the configuration', pausedSource({ enabled: false, state: 'disabled' })],
    ['access_blocked', blockedSource('access_blocked', { display_name: 'SYNTHETIC paused source', paused: true, pause_reason: 'SYNTHETIC owner pause for maintenance', paused_at: '2026-10-07T08:00:00Z' })],
    ['parser_unhealthy', blockedSource('parser_unhealthy', { display_name: 'SYNTHETIC paused source', paused: true, pause_reason: 'SYNTHETIC owner pause for maintenance', paused_at: '2026-10-07T08:00:00Z' })],
  ])('a paused source that is also %s reads "It is also paused" mid-sentence', async (_label, source) => {
    renderSources(source)
    const toggle = await findSwitch('SYNTHETIC paused source')
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(/\. It is also paused since .*: SYNTHETIC owner pause for maintenance\. It stays paused/)
    expect(toggle).not.toHaveAccessibleDescription(/also Paused/)
    expect(screen.queryByRole('button', { name: 'Pause in advance' })).toBeNull() // already paused
  })

  it('a paused source that is also off in the configuration, without a pause time', async () => {
    renderSources(pausedSource({ enabled: false, state: 'disabled', paused_at: null }))
    const toggle = await findSwitch('SYNTHETIC paused source')
    expect(toggle).toHaveAccessibleDescription(/This switch never enables a source\. It is also paused: SYNTHETIC owner pause for maintenance\./)
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
    // It can still be paused in advance (a separate action; the switch stays off).
    expect(screen.getByRole('button', { name: 'Pause in advance' })).toBeInTheDocument()
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

describe('pausing in advance (a source that runs no network work now)', () => {
  const kinds: Array<[string, (overrides?: Partial<SourceStatusView>) => SourceStatusView, string]> = [
    ['off in the configuration', disabledInConfig, 'SYNTHETIC disabled source'],
    ['access_blocked', (overrides) => blockedSource('access_blocked', overrides), 'SYNTHETIC blocked source'],
    ['parser_unhealthy', (overrides) => blockedSource('parser_unhealthy', overrides), 'SYNTHETIC blocked source'],
  ]

  it.each(kinds)('%s: the owner pauses it in advance; the switch stays off and disabled', async (_label, make, name) => {
    const reason = 'SYNTHETIC hold until the source is understood'
    let paused = false
    const api = renderSources(() =>
      paused ? make({ paused: true, pause_reason: reason, paused_at: '2026-10-07T10:00:00Z', version: 5 }) : make(),
    )
    api.on('POST /api/sources/:id/pause', () => {
      paused = true
      return pauseResult(5, reason)
    })
    const user = userEvent.setup()
    const toggle = await findSwitch(name)
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(/You can still pause it in advance/)
    const ahead = within(card(name)).getByRole('button', { name: 'Pause in advance' })
    expect(ahead).toHaveAttribute('aria-expanded', 'false')
    expect(ahead).toHaveAccessibleDescription(/the pause stays when the source would otherwise start again, until the owner resumes it/)
    await user.click(ahead)
    expect(ahead).toHaveAttribute('aria-expanded', 'true')
    expect(ahead).toHaveAttribute('aria-controls', screen.getByRole('form', { name: 'Pause this source' }).id)
    expect(toggle).not.toHaveAttribute('aria-controls') // the switch did not open it
    expect(screen.getByRole('form', { name: 'Pause this source' })).toHaveTextContent(
      /keeps it off after a later enable by the configuration or a cleared technical block, until the owner resumes it/,
    )
    const input = screen.getByLabelText(/Pause reason/)
    expect(input).toHaveFocus()
    await user.type(input, reason)
    await user.click(screen.getByRole('button', { name: 'Pause this source' }))
    expect(await screen.findByTestId('source-paused')).toHaveTextContent('Source paused.')
    await waitFor(() => expect(within(card(name)).queryByRole('button', { name: 'Pause in advance' })).toBeNull())
    const after = screen.getByRole('switch', { name: `Collect from ${name}` })
    expect(after).toHaveAttribute('aria-checked', 'false')
    expect(after).toHaveAttribute('aria-disabled', 'true') // a pause never enables or unblocks a source
    expect(after).toHaveAccessibleDescription(new RegExp(`It is also paused since .*: ${reason}\\.`))
    const [call] = api.callsTo('POST /api/sources/:id/pause')
    expect(call?.body).toMatchObject({ expected_version: 4, reason })
    const sent = call?.body as { idempotency_key: string } | undefined
    expect(String(sent?.idempotency_key)).toMatch(/^source-pause:/)
    expect(api.callsTo('POST /api/sources/:id/resume')).toEqual([])
  })

  it('is keyboard operable: Enter opens the form, Escape cancels back to the button', async () => {
    const api = renderSources(disabledInConfig())
    const user = userEvent.setup()
    await findSwitch('SYNTHETIC disabled source')
    const ahead = screen.getByRole('button', { name: 'Pause in advance' })
    ahead.focus()
    await user.keyboard('{Enter}')
    expect(screen.getByLabelText(/Pause reason/)).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByLabelText(/Pause reason/)).toBeNull()
    expect(ahead).toHaveFocus()
    await user.keyboard(' ')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('form')).toBeNull()
    expect(ahead).toHaveFocus()
    expect(api.calls.filter((call) => call.method === 'POST')).toEqual([])
  })

  it('needs sources:pause only (not config:admin); roles without it get no pause button', async () => {
    const pauser: MeView = { ...me('owner'), scopes: ['deals:read', 'reviews:read', 'sources:pause'] }
    renderSources(blockedSource('access_blocked'), pauser)
    await findSwitch('SYNTHETIC blocked source')
    expect(screen.getByRole('button', { name: 'Pause in advance' })).toBeInTheDocument()
  })

  it.each(['reviewer', 'viewer'] as const)('a %s gets no pause button for a source that runs nothing', async (role) => {
    renderSources(disabledInConfig(), me(role))
    const toggle = await findSwitch('SYNTHETIC disabled source')
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).not.toHaveAccessibleDescription(/pause it in advance/)
    expect(screen.queryByRole('button', { name: 'Pause in advance' })).toBeNull()
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

  it('while the pause is being sent or unconfirmed, the disabled switch says why', async () => {
    const held = deferred()
    const api = renderSources(sourceStatus())
    api.on('POST /api/sources/:id/pause', async () => {
      await held.promise
      throw new TypeError('network connection lost') // the answer never arrived
    })
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC running source')
    await user.click(toggle)
    await user.type(screen.getByLabelText(/Pause reason/), 'SYNTHETIC drift')
    await user.click(screen.getByRole('button', { name: 'Pause this source' }))
    expect(await screen.findByTestId('mutation-pending')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(toggle).toHaveAccessibleDescription(/The switch is unavailable while the pause is being sent: wait for the server's answer below\./)
    held.release()
    expect(await screen.findByTestId('mutation-unconfirmed')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle).toHaveAccessibleDescription(
      /The switch is unavailable until the unconfirmed pause is resolved: retry the same request or discard it below\./,
    )
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
    expect(toggle).toHaveAccessibleDescription(
      /The switch is unavailable until the unconfirmed resume is resolved: retry the same request or discard it below\./,
    )
    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('source-resumed')).toHaveTextContent('No waiting job needed requeueing.')
    expect(toggle).not.toHaveAccessibleDescription(/The switch is unavailable/)
    const [first, second] = api.callsTo('POST /api/sources/:id/resume')
    expect(second?.rawBody).toBe(first?.rawBody)
    expect(second?.headers.get('Idempotency-Key')).toBe(first?.headers.get('Idempotency-Key'))
  })
})

describe('switching direction', () => {
  it('an older result of the other direction leaves the screen when the switch is used again', async () => {
    let paused = true
    let version = 4
    const api = renderSources(() =>
      paused ? pausedSource({ version }) : sourceStatus({ display_name: 'SYNTHETIC paused source', version }),
    )
    api.on('POST /api/sources/:id/resume', () => {
      paused = false
      version += 1
      return ok(resumeResult({ requeued_jobs: 2, version }))
    })
    api.on('POST /api/sources/:id/pause', () => {
      paused = true
      version += 1
      return pauseResult(version, 'SYNTHETIC drift again')
    })
    const user = userEvent.setup()
    const toggle = await findSwitch('SYNTHETIC paused source')

    await user.click(toggle) // on
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC maintenance done')
    await user.click(screen.getByRole('button', { name: 'Resume this source' }))
    expect(await screen.findByTestId('source-resumed')).toHaveTextContent('Source resumed. 2 waiting jobs were requeued.')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))

    await user.click(toggle) // off: the resume result goes as soon as the pause form opens
    expect(screen.getByLabelText(/Pause reason/)).toBeInTheDocument()
    expect(screen.queryByTestId('source-resumed')).toBeNull()
    await user.type(screen.getByLabelText(/Pause reason/), 'SYNTHETIC drift again')
    await user.click(screen.getByRole('button', { name: 'Pause this source' }))
    expect(await screen.findByTestId('source-paused')).toHaveTextContent('Source paused.')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    expect(screen.queryByTestId('source-resumed')).toBeNull()

    await user.click(toggle) // on again: the pause result goes
    expect(screen.getByLabelText(/Resume reason/)).toBeInTheDocument()
    expect(screen.queryByTestId('source-paused')).toBeNull()
    expect(api.calls.filter((call) => call.method === 'POST').map((call) => call.path.split('/').pop())).toEqual(['resume', 'pause'])
  })
})
