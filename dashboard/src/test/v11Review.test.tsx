/**
 * Regression tests of the B2c review (spec 23 + 37.6-37.10 dashboard screens). Each one failed on
 * the code before the fix it guards.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { InquiryControlView } from '../api/types'
import { compareInstants } from '../format'
import { resolvePendingControl } from '../screens/inquiries/controlMarker'
import { fakeApi, ok, type FakeApi, type Handler } from './fakeApi'
import { TEST_USER_ID } from './fakeAuth'
import { me, WORKSPACE_ID } from './fixtures'
import { renderApp } from './renderApp'
import {
  control,
  evaluation,
  gaps,
  health,
  inquiry,
  INQUIRY_ID,
  inquirySummary,
  lag,
  mailbox,
  replySummary,
} from './v11Fixtures'

function api(extra: Record<string, Handler> = {}, role: 'owner' | 'reviewer' = 'reviewer'): FakeApi {
  return fakeApi({
    'GET /api/me': () => ok(me(role)),
    'GET /api/inquiries': () => ok({ items: [inquirySummary()] }),
    'GET /api/inquiry-control': () => ok(control()),
    'GET /api/inquiries/:id': () => ok(inquiry()),
    'GET /api/replies': () => ok({ items: [replySummary()] }),
    ...extra,
  })
}

/** The `<dd>` value next to the `<dt>` named `name` inside `container`. */
function valueOf(container: HTMLElement, name: string): HTMLElement {
  const term = within(container).getByText(name, { selector: 'dt' })
  const value = term.nextElementSibling
  if (!(value instanceof HTMLElement)) throw new Error(`no value for ${name}`)
  return value
}

describe('mail workers: a powered-off PC as the REAL server reports it', () => {
  it("never shows the worker's last report (sync ok, 10 s lag, empty backlog) as current health", async () => {
    // What `GET /api/mail-workers/health` really returns 6 h after the PC was shut down: the
    // statuses go down/stale, but the worker-reported dimensions keep their last values.
    const off = mailbox({
      worker_label: 'SYNTHETIC powered-off PC',
      last_heartbeat_at: '2026-10-07T04:00:00Z',
      heartbeat_age_seconds: 21_600,
      heartbeat_status: 'down',
      outlook_status: 'stale',
      mailbox_sync_ok: true,
      mailbox_sync_lag: lag({ name: 'mailbox_sync_lag', status: 'measured', value_seconds: 10, reason: null }),
      reconciliation_status: 'down',
      backlog_count: 0,
      backlog_age: lag({ name: 'backlog_age', status: 'measured', value_seconds: 0, reason: null }),
      unresolved_matching_gaps: 0,
      account_status: 'unknown',
      monitoring_active: false,
      open_gap_count: 1,
      coverage_gaps: [{ kind: 'worker_offline', started_at: '2026-10-07T04:00:00Z', ended_at: null, open: true, detected_by: 'server' }],
      reasons: ['worker heartbeat down', 'Outlook connection stale', 'reconciliation down'],
    })
    const fake = api({
      'GET /api/mail-workers/health': () => ok(health([off])),
      'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
    })
    renderApp('/mail-workers', { api: fake })
    const card = await screen.findByTestId('mailbox-card')
    for (const name of ['Mailbox synchronising', 'Mailbox sync lag', 'Upload backlog', 'Backlog age', 'Unresolved matching gaps']) {
      const value = valueOf(card, name)
      expect(value, name).toHaveTextContent(/^unknown now/)
      expect(within(value).getByTestId('last-report')).toBeInTheDocument()
    }
    expect(valueOf(card, 'Mailbox synchronising')).toHaveTextContent('(last report: yes)')
    expect(valueOf(card, 'Mailbox sync lag')).toHaveTextContent('(last report: 10 s)')
    // A fresh worker still shows its current values.
  })

  it('a fresh, healthy worker shows its values as current', async () => {
    const fake = api({
      'GET /api/mail-workers/health': () => ok(health([mailbox()])),
      'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
    })
    renderApp('/mail-workers', { api: fake })
    const card = await screen.findByTestId('mailbox-card')
    expect(valueOf(card, 'Mailbox synchronising')).toHaveTextContent(/^yes$/)
    expect(within(card).queryByTestId('last-report')).toBeNull()
  })
})

describe('filters follow back/forward navigation', () => {
  it('the inquiry filter form shows the filters of the current URL after Back', async () => {
    const fake = api()
    const { router } = renderApp('/inquiries', { api: fake })
    const user = userEvent.setup()
    await screen.findByRole('table', { name: 'Seller inquiries' })
    await user.selectOptions(screen.getByLabelText('State'), 'uncertain')
    await user.click(screen.getByLabelText(/uncertain sends only/))
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(router.state.location.search).toBe('?state=uncertain&uncertain_only=true'))
    await router.navigate(-1)
    await waitFor(() => expect(router.state.location.search).toBe(''))
    // The list shows every inquiry again, so the form must not keep claiming a filter is applied.
    await waitFor(() => expect(screen.getByLabelText('State')).toHaveValue(''))
    expect(screen.getByLabelText(/uncertain sends only/)).not.toBeChecked()
  })

  it('the reply filter checkbox shows the filter of the current URL after Back', async () => {
    const fake = api()
    const { router } = renderApp('/replies', { api: fake })
    const user = userEvent.setup()
    await screen.findByRole('table', { name: 'Seller replies' })
    await user.click(screen.getByLabelText(/quarantined only/))
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(router.state.location.search).toBe('?quarantined_only=true'))
    await router.navigate(-1)
    await waitFor(() => expect(router.state.location.search).toBe(''))
    await waitFor(() => expect(screen.getByLabelText(/quarantined only/)).not.toBeChecked())
  })
})

describe('inquiry detail: replies are paginated, never silently truncated', () => {
  it('offers the next page of this inquiry\'s replies with the same inquiry filter', async () => {
    const fake = api({
      'GET /api/replies': (call) =>
        call.url.searchParams.get('cursor')
          ? ok({ items: [replySummary({ reply_id: '77777777-7777-4777-8777-777777777778' })] })
          : ok({ items: [replySummary()] }, 200, { next_cursor: 'replies-page-2' }),
    })
    renderApp(`/inquiries/${INQUIRY_ID}`, { api: fake })
    const user = userEvent.setup()
    const button = await screen.findByRole('button', { name: 'Load more replies' })
    await user.click(button)
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Replies' })).getAllByTestId('reply-row')).toHaveLength(2))
    const next = fake.callsTo('GET /api/replies').find((call) => call.url.searchParams.get('cursor') === 'replies-page-2')
    expect(next?.url.searchParams.get('inquiry_id')).toBe(INQUIRY_ID)
  })
})

describe('attention section: waiting inquiries beyond the first page', () => {
  it('links to the full list of qualified inquiries when more are waiting', async () => {
    const fake = api({
      'GET /api/inquiries': (call) =>
        call.url.searchParams.get('state') === 'qualifying'
          ? ok({ items: [inquirySummary({ state: 'qualifying', send_attempted_at: null, accepted_at: null })] }, 200, { next_cursor: 'more-waiting' })
          : ok({ items: [inquirySummary()] }),
    })
    renderApp('/inquiries', { api: fake })
    const waiting = await screen.findByTestId('attention-waiting')
    expect(within(waiting).getByRole('link', { name: 'show them all' })).toHaveAttribute('href', '/inquiries?state=qualifying')
  })
})

function controlWorld(initial: InquiryControlView) {
  const current = { value: initial }
  const fake = api(
    {
      'GET /api/inquiry-control': () => ok(current.value),
      'POST /api/inquiry-control/pause': (call) => {
        const body = call.body as { expected_version: number; reason: string }
        current.value = { ...current.value, version: body.expected_version + 1, kill_switch: true, kill_switch_reason: body.reason, kill_switch_set_at: '2026-10-07T10:00:00Z' }
        return ok({ version: current.value.version, kill_switch: true, already_paused: false, kill_switch_set_at: '2026-10-07T10:00:00Z', mode: 'automatic', notice: 'Seller inquiries paused. Resuming requires a separate owner action.' })
      },
      'POST /api/inquiry-control/resume': (call) => {
        const body = call.body as { expected_version: number; remove_suppressions: boolean }
        const removed = body.remove_suppressions ? current.value.removable_suppressions : 0
        const bump = current.value.kill_switch ? 1 : 0
        current.value = { ...current.value, version: body.expected_version + bump, kill_switch: false, kill_switch_reason: null, kill_switch_set_at: null, removable_suppressions: current.value.removable_suppressions - removed }
        return ok({ version: current.value.version, kill_switch: false, mode: 'automatic', resumed_at: '2026-10-07T10:05:00Z', suppressions_removed: removed })
      },
    },
    'owner',
  )
  return { fake, current }
}

describe('inquiry control', () => {
  it('lets the owner remove resumable suppressions while the kill switch is already off', async () => {
    // E.g. after `suv-deals inquiries resume` without the removal, or a re-authorization: the
    // control view counts suppressions a resume can remove, so the owner needs that action here.
    const { fake } = controlWorld(control({ removable_suppressions: 2 }))
    renderApp('/inquiry-control', { api: fake })
    const user = userEvent.setup()
    expect(await screen.findByRole('button', { name: 'Pause seller inquiries' })).toBeInTheDocument()
    await user.type(screen.getByLabelText(/Resume reason/), 'SYNTHETIC: re-qualify after re-authorization')
    expect(screen.getByTestId('resume-form')).toHaveTextContent('The kill switch is already off')
    await user.click(screen.getByLabelText(/Also remove the 2 kill-switch/))
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toHaveTextContent('2 suppression(s) removed, each audited.')
    expect(fake.callsTo('POST /api/inquiry-control/resume')[0]?.body).toMatchObject({ expected_version: 3, remove_suppressions: true })
    // Nothing is left to remove: the action disappears again (the pause stays available).
    await waitFor(() => expect(screen.queryByTestId('resume-form')).toBeNull())
    expect(screen.getByRole('button', { name: 'Pause seller inquiries' })).toBeInTheDocument()
  })

  it('a reviewer never gets the re-qualify action, and nothing is offered without removable suppressions', async () => {
    const reviewer = api({ 'GET /api/inquiry-control': () => ok(control({ removable_suppressions: 2 })) })
    renderApp('/inquiry-control', { api: reviewer })
    expect(await screen.findByText(/Your role cannot pause seller inquiries/)).toBeInTheDocument()
    expect(screen.queryByTestId('resume-form')).toBeNull()
  })

  it('after a reload, a re-qualify sent while the kill switch was off is reported as undeterminable, not as applied', () => {
    const marker = { workspaceId: WORKSPACE_ID, userId: TEST_USER_ID, action: 'resume' as const, idempotencyKey: 'inquiry-resume:k-1', expectedVersion: 3, startedAt: new Date().toISOString() }
    expect(resolvePendingControl(marker, control({ version: 3, kill_switch: false }))).toBe('indeterminate')
    // A resume of a paused workspace stays decidable from the version.
    expect(resolvePendingControl(marker, control({ version: 3, kill_switch: true }))).toBe('not_applied')
    expect(resolvePendingControl(marker, control({ version: 4, kill_switch: false }))).toBe('applied')
  })

  it('does not keep showing "paused" after the owner resumed (and vice versa)', async () => {
    const { fake } = controlWorld(control())
    renderApp('/inquiry-control', { api: fake })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: pause for a check')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    expect(await screen.findByTestId('pause-confirmed')).toBeInTheDocument()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: check finished')
    await user.click(screen.getByRole('button', { name: 'Resume seller inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toBeInTheDocument()
    expect(screen.queryByTestId('pause-confirmed')).toBeNull()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: pause again')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    expect(await screen.findByTestId('pause-confirmed')).toBeInTheDocument()
    expect(screen.queryByTestId('resume-confirmed')).toBeNull()
  })
})

describe('timestamps are ordered as instants, not as strings', () => {
  it('compareInstants orders a whole second before its fractions', () => {
    expect(compareInstants('2026-10-07T08:00:00Z', '2026-10-07T08:00:00.250000Z')).toBeLessThan(0)
    expect(compareInstants('2026-10-07T08:00:00.250000Z', '2026-10-07T08:00:00Z')).toBeGreaterThan(0)
    expect(compareInstants('2026-10-07T08:00:00Z', '2026-10-07T08:00:00Z')).toBe(0)
  })

  it('the inquiry timeline starts with "Created" when it shares its second with the reservation', async () => {
    const base = inquiry()
    const fake = api({
      'GET /api/inquiries/:id': () =>
        ok(
          inquiry({
            timestamps: {
              ...base.timestamps,
              created_at: '2026-10-07T08:00:00Z',
              reserved_at: '2026-10-07T08:00:00.250000Z',
              queued_at: '2026-10-07T08:00:00.500000Z',
            },
          }),
        ),
    })
    renderApp(`/inquiries/${INQUIRY_ID}`, { api: fake })
    const timeline = await screen.findByTestId('inquiry-timeline')
    const items = within(timeline).getAllByRole('listitem').map((item) => item.textContent ?? '')
    expect(items[0]).toContain('Created')
    expect(items[1]).toContain('Reserved')
    expect(items[2]).toContain('Queued')
  })
})

describe('15-day evaluation: healthy coverage intervals', () => {
  it('lists each healthy coverage interval (not only how many there are)', async () => {
    const fake = api({ 'GET /api/evaluation': () => ok(evaluation()) })
    renderApp('/evaluation', { api: fake })
    const intervals = await screen.findByTestId('coverage-intervals')
    const [item] = within(intervals).getAllByRole('listitem')
    expect(item).toHaveTextContent('40 healthy scans')
    expect(item?.querySelectorAll('time')).toHaveLength(2)
    expect(item?.querySelector('time')).toHaveAttribute('dateTime', '2026-10-01T00:00:00Z')
  })
})
