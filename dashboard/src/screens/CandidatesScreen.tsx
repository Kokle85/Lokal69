import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { isApiError, type ApiError } from '../api/errors'
import {
  LIMITS,
  type CandidateListQuery,
  type CandidateStatus,
  type CandidateSummary,
  type ProfileKey,
  type ResponseEnvelope,
  type CandidateListView,
} from '../api/types'
import { Amount, Badge, EmptyState, ErrorPanel, LoadingState, ViewMeta, Warnings } from '../components/ui'
import { ageText, kmText, label, localInputToRfc3339, partialDateText, rfc3339ToLocalInput } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'

export interface MorePages<T> {
  /** Request id of the first page these pages continue. */
  base: string
  items: T[]
  cursor: string | null
  error: ApiError | null
}

const PROFILES: ProfileKey[] = ['primary', 'manual_4000', 'below_target_watch']
const STATUSES: CandidateStatus[] = ['pending', 'needs_information', 'watch', 'shortlisted', 'rejected']

function filtersFrom(params: URLSearchParams): CandidateListQuery {
  const query: CandidateListQuery = { limit: 25 }
  const profile = params.get('profile')
  if (profile && (PROFILES as string[]).includes(profile)) query.profile = profile as ProfileKey
  const status = params.get('status')
  if (status && (STATUSES as string[]).includes(status)) query.status = status as CandidateStatus
  const country = params.get('country')
  if (country && LIMITS.countryPattern.test(country)) query.country = country
  const changed = params.get('changed_since')
  if (changed) query.changed_since = changed
  return query
}

export function modelText(item: Pick<CandidateSummary, 'make' | 'model' | 'generation'>): string {
  const parts = [item.make, item.model, item.generation].filter((part): part is string => Boolean(part))
  return parts.length ? parts.join(' ') : 'model unknown'
}

export function CandidatesScreen() {
  const { timezone } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const filters = filtersFrom(params)
  const page = useApiQuery((client, signal) => client.candidates(filters, { signal }), [filters])
  const { client } = useWorkspace()
  // Further pages belong to the first page they continue (keyed by its request id), so a new
  // first page (other filters, reload) starts clean without an effect.
  const [more, setMore] = useState<MorePages<CandidateSummary> | null>(null)
  const firstPage = page.envelope?.request_id ?? null
  const current = more && more.base === firstPage ? more : null
  const cursor = current ? current.cursor : (page.envelope?.next_cursor ?? null)
  const [loadingMore, setLoadingMore] = useState(false)

  async function loadMore() {
    if (!cursor || loadingMore || !firstPage) return
    setLoadingMore(true)
    try {
      const { envelope }: { envelope: ResponseEnvelope<CandidateListView> } = await client.candidates({ ...filters, cursor })
      setMore({
        base: firstPage,
        items: [...(current?.items ?? []), ...envelope.data.items],
        cursor: envelope.next_cursor,
        error: null,
      })
    } catch (error) {
      if (isApiError(error)) setMore({ base: firstPage, items: current?.items ?? [], cursor, error })
    } finally {
      setLoadingMore(false)
    }
  }

  const moreError = current?.error ?? null
  const items = [...(page.data?.items ?? []), ...(current?.items ?? [])]

  return (
    <div className="screen">
      <h1>Candidate queue</h1>
      <FilterForm
        key={JSON.stringify(filters)}
        initial={filters}
        onApply={(next) => {
          const search = new URLSearchParams()
          if (next.profile) search.set('profile', next.profile)
          if (next.status) search.set('status', next.status)
          if (next.country) search.set('country', next.country)
          if (next.changed_since) search.set('changed_since', next.changed_since)
          setParams(search)
        }}
      />
      {page.status === 'loading' ? <LoadingState label="Loading candidates" /> : null}
      {page.error ? <ErrorPanel error={page.error} onRetry={page.reload} /> : null}
      {page.envelope ? (
        <>
          <ViewMeta
            asOf={page.envelope.as_of}
            fetchedAt={page.fetchedAt}
            timeZone={timezone}
            onReload={page.reload}
            reloading={page.reloading}
          />
          <Warnings warnings={page.envelope.warnings} />
          <p className="muted">
            Order is the server&apos;s stable queue order (ranking score orders the queue; it is not a probability of profit).
          </p>
          {items.length === 0 ? (
            <EmptyState>No candidates match these filters.</EmptyState>
          ) : (
            <table className="responsive-table candidates" aria-label="Candidates">
              <thead>
                <tr>
                  <th scope="col">Vehicle</th>
                  <th scope="col">Price (original)</th>
                  <th scope="col">EUR equivalent</th>
                  <th scope="col">Mileage</th>
                  <th scope="col">Country</th>
                  <th scope="col">Eligibility</th>
                  <th scope="col">Review</th>
                  <th scope="col">Freshness</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <CandidateRow key={item.listing_id} item={item} />
                ))}
              </tbody>
            </table>
          )}
          {moreError ? <ErrorPanel error={moreError} onRetry={page.reload} retryLabel="Restart from the first page" /> : null}
          {cursor ? (
            <button type="button" className="button secondary" onClick={() => void loadMore()} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          ) : items.length ? (
            <p className="muted">End of the list.</p>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function CandidateRow({ item }: { item: CandidateSummary }) {
  return (
    <tr>
      <td data-label="Vehicle">
        <Link to={`/candidates/${item.listing_id}`} className="row-link">
          {modelText(item)}
        </Link>
        <div className="muted small">
          <span className="untrusted">{item.title ?? 'untitled'}</span> · first registration{' '}
          {partialDateText(item.first_registration)} · {item.source_key}
        </div>
        {item.is_fixture ? <Badge tone="muted">synthetic fixture</Badge> : null}
        {item.research_candidate ? <Badge tone="warn">research candidate</Badge> : null}
        {item.quarantined ? <Badge tone="bad">quarantined</Badge> : null}
      </td>
      <td data-label="Price (original)">
        <Amount value={item.price.payable} />
        <div className="muted small">
          {item.price.original_currency ?? 'currency unknown'} · {label(item.price.basis)} · {label(item.price.price_type)}
        </div>
      </td>
      <td data-label="EUR equivalent">
        <Amount value={item.price.eur_equivalent} />
      </td>
      <td data-label="Mileage">
        {kmText(item.mileage_km)}
        <div className="muted small">{label(item.mileage_claim)}</div>
      </td>
      <td data-label="Country">
        {item.seller_country ?? 'unknown'}
        {item.seller_country !== item.source_country ? <div className="muted small">source {item.source_country}</div> : null}
      </td>
      <td data-label="Eligibility">
        <Badge value={item.eligibility ?? 'unknown'} />
        {item.queue_label ? <div className="muted small">{item.queue_label}</div> : null}
      </td>
      <td data-label="Review">
        {item.review_state ? <Badge value={item.review_state} /> : <span className="muted">no case</span>}
        <div className="muted small">valuation {label(item.valuation_state)}</div>
      </td>
      <td data-label="Freshness">
        {item.freshness.stale ? <Badge tone="warn">stale</Badge> : <Badge tone="ok">fresh</Badge>}
        <div className="muted small">seen {ageText(item.freshness.last_seen_at)}</div>
        {item.freshness.flags.length ? (
          <div className="muted small">{item.freshness.flags.map((flag) => label(flag)).join(', ')}</div>
        ) : null}
      </td>
    </tr>
  )
}

function FilterForm({ initial, onApply }: { initial: CandidateListQuery; onApply(query: CandidateListQuery): void }) {
  const [profile, setProfile] = useState(initial.profile ?? '')
  const [status, setStatus] = useState(initial.status ?? '')
  const [country, setCountry] = useState(initial.country ?? '')
  const [changed, setChanged] = useState(rfc3339ToLocalInput(initial.changed_since ?? null))
  const [problem, setProblem] = useState<string | null>(null)

  function submit(event: FormEvent) {
    event.preventDefault()
    const normalizedCountry = country.trim().toUpperCase()
    if (normalizedCountry && !LIMITS.countryPattern.test(normalizedCountry)) {
      setProblem('Country must be a two-letter code such as DE.')
      return
    }
    const changedSince = changed ? localInputToRfc3339(changed) : null
    if (changed && !changedSince) {
      setProblem('Changed since is not a valid date and time.')
      return
    }
    setProblem(null)
    const query: CandidateListQuery = {}
    if (profile) query.profile = profile as ProfileKey
    if (status) query.status = status as CandidateStatus
    if (normalizedCountry) query.country = normalizedCountry
    if (changedSince) query.changed_since = changedSince
    onApply(query)
  }

  return (
    <form className="filters" onSubmit={submit} aria-label="Candidate filters">
      <div className="field">
        <label htmlFor="filter-profile">Profile</label>
        <select id="filter-profile" value={profile} onChange={(event) => setProfile(event.target.value)}>
          <option value="">Any</option>
          {PROFILES.map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="filter-status">Status</label>
        <select id="filter-status" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">Any</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="filter-country">Seller country</label>
        <input
          id="filter-country"
          value={country}
          maxLength={2}
          placeholder="DE"
          autoComplete="off"
          onChange={(event) => setCountry(event.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="filter-changed">Changed since</label>
        <input id="filter-changed" type="datetime-local" value={changed} onChange={(event) => setChanged(event.target.value)} />
      </div>
      <div className="field field-actions">
        <button type="submit" className="button">
          Apply filters
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => {
            setProfile('')
            setStatus('')
            setCountry('')
            setChanged('')
            onApply({})
          }}
        >
          Clear
        </button>
      </div>
      {problem ? (
        <p className="form-error" role="alert">
          {problem}
        </p>
      ) : null}
    </form>
  )
}
