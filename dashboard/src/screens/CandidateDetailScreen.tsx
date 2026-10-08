import { useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { LIMITS, type AddNoteRequest, type CandidateDetail, type NoteView, type RecheckRequest, type RecheckRequestResult } from '../api/types'
import {
  Amount,
  Badge,
  EmptyState,
  ErrorPanel,
  ExternalLink,
  KeyValues,
  LoadingState,
  Notice,
  Section,
  Timestamp,
  UntrustedText,
  ViewMeta,
  Warnings,
} from '../components/ui'
import { kmText, label, partialDateText } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { MutationStatus } from '../review/MutationStatus'
import { useIdempotentMutation } from '../review/useIdempotentMutation'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import { modelText } from './CandidatesScreen'

export function CandidateDetailScreen() {
  const { listingId = '' } = useParams()
  const [params] = useSearchParams()
  const revisionParam = Number.parseInt(params.get('revision') ?? '', 10)
  const revision = Number.isInteger(revisionParam) && revisionParam > 0 ? revisionParam : null
  const { timezone } = useWorkspace()
  const detail = useApiQuery((client, signal) => client.candidate(listingId, revision, { signal }), [listingId, revision])

  return (
    <div className="screen">
      <p>
        <Link to="/candidates">← Candidate queue</Link>
      </p>
      {detail.status === 'loading' ? <LoadingState label="Loading candidate" /> : null}
      {detail.error ? <ErrorPanel error={detail.error} onRetry={detail.reload} /> : null}
      {detail.data && detail.envelope ? (
        <>
          <ViewMeta
            asOf={detail.envelope.as_of}
            fetchedAt={detail.fetchedAt}
            timeZone={timezone}
            onReload={detail.reload}
            reloading={detail.reloading}
          />
          <Warnings warnings={detail.envelope.warnings} />
          <CandidateBody detail={detail.data} timeZone={timezone} onChanged={detail.reload} />
        </>
      ) : null}
    </div>
  )
}

function CandidateBody({ detail, timeZone, onChanged }: { detail: CandidateDetail; timeZone: string; onChanged: () => void }) {
  const { summary, revision, normalized } = detail
  const vehicle = normalized.vehicle
  return (
    <>
      <h1>{modelText(summary)}</h1>
      <p className="subtitle">
        <span className="muted">Listing title (seller text): </span>
        <UntrustedText text={detail.seller_text.title ?? summary.title} />
      </p>
      <div className="badges">
        <Badge value={summary.eligibility ?? 'unknown'} />
        {summary.review_state ? <Badge value={summary.review_state} /> : null}
        <Badge value={summary.availability} />
        {summary.is_fixture ? <Badge tone="muted">synthetic fixture</Badge> : null}
        {summary.research_candidate ? <Badge tone="warn">research candidate</Badge> : null}
        {summary.freshness.stale ? <Badge tone="warn">stale data</Badge> : null}
      </div>
      {summary.freshness.flags.includes('source_paused') ? (
        <Notice tone="warn">The source of this listing is paused; it is not being rechecked.</Notice>
      ) : null}
      {!revision.is_current ? (
        <Notice tone="warn">
          You are viewing revision {revision.revision_number}; the current revision is {revision.current_revision_number}.{' '}
          <Link to={`/candidates/${summary.listing_id}`}>Show the current revision</Link>
        </Notice>
      ) : null}

      <nav className="related-links" aria-label="Related views">
        <ExternalLink href={detail.source_link.url}>Open the source listing ({detail.source_link.source_key})</ExternalLink>
        {detail.latest_valuation ? (
          <Link to={`/candidates/${summary.listing_id}/economics`}>Economics</Link>
        ) : (
          <span className="muted">No valuation yet</span>
        )}
        {detail.review_case ? <Link to={`/reviews/${detail.review_case.case_id}`}>Review case</Link> : null}
      </nav>
      <p className="muted small">{detail.source_link.notice}</p>

      <Section title="Price and availability" id="price">
        <KeyValues
          items={[
            ['Asking price (original currency)', <Amount key="p" value={summary.price.payable} showReason />],
            ['Original currency', summary.price.original_currency ?? 'unknown'],
            ['EUR equivalent', <Amount key="e" value={summary.price.eur_equivalent} showReason />],
            [
              'FX rate used',
              summary.price.fx_rate ? (
                `${summary.price.fx_rate.direction} (${summary.price.fx_rate.provider}, ${summary.price.fx_rate.rate_date})`
              ) : (
                <span key="n" className="muted">none</span>
              ),
            ],
            ['Price basis / type', `${label(summary.price.basis)} · ${label(summary.price.price_type)}`],
            ['Negotiable', label(summary.price.negotiable)],
            ['Availability', <Badge key="a" value={summary.availability} />],
          ]}
        />
        <h3>Price history</h3>
        {detail.price_history.length === 0 ? (
          <EmptyState>No price observations.</EmptyState>
        ) : (
          <table className="responsive-table">
            <thead>
              <tr>
                <th scope="col">Revision</th>
                <th scope="col">Observed</th>
                <th scope="col">Payable</th>
                <th scope="col">Change</th>
              </tr>
            </thead>
            <tbody>
              {detail.price_history.map((point) => (
                <tr key={point.revision_number}>
                  <td data-label="Revision">{point.revision_number}</td>
                  <td data-label="Observed">
                    <Timestamp value={point.observed_at} timeZone={timeZone} />
                  </td>
                  <td data-label="Payable">
                    <Amount value={point.payable} />
                  </td>
                  <td data-label="Change">{label(point.change)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <h3>Availability history</h3>
        {detail.availability_history.length === 0 ? (
          <EmptyState>No availability observations.</EmptyState>
        ) : (
          <table className="responsive-table">
            <thead>
              <tr>
                <th scope="col">Observed</th>
                <th scope="col">Availability</th>
                <th scope="col">Observed via</th>
              </tr>
            </thead>
            <tbody>
              {detail.availability_history.map((point, index) => (
                <tr key={`${point.observed_at}-${index}`}>
                  <td data-label="Observed">
                    <Timestamp value={point.observed_at} timeZone={timeZone} />
                  </td>
                  <td data-label="Availability">
                    <Badge value={point.availability} />
                  </td>
                  <td data-label="Observed via">{label(point.observed_via)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small">
          A missing search result, a reorder or an inaccessible page is not a sale; uncertain disappearance is shown as
          unknown.
        </p>
      </Section>

      <Section title="Lifecycle and freshness" id="lifecycle">
        <KeyValues
          items={[
            ['First seen by this system', <Timestamp key="f" value={summary.freshness.first_seen_at} timeZone={timeZone} withAge />],
            ['Last seen on a search page', <Timestamp key="l" value={summary.freshness.last_seen_at} timeZone={timeZone} withAge />],
            ['Last successful detail check', <Timestamp key="d" value={summary.freshness.last_detail_success_at} timeZone={timeZone} withAge />],
            ['Last availability check', <Timestamp key="c" value={summary.freshness.last_availability_check_at} timeZone={timeZone} withAge />],
            [
              'Source-reported publication',
              normalized.source_published_at.value ? (
                <Timestamp key="p" value={normalized.source_published_at.value} timeZone={timeZone} />
              ) : (
                <span key="pn" className="muted">not provided by the source (true detection delay unknown)</span>
              ),
            ],
            [
              'Source-reported modification',
              normalized.source_modified_at.value ? (
                <Timestamp key="m" value={normalized.source_modified_at.value} timeZone={timeZone} />
              ) : (
                <span key="mn" className="muted">not provided</span>
              ),
            ],
            ['Revision', `${revision.revision_number} of ${revision.current_revision_number} (parser ${revision.parser_version})`],
            ['Freshness flags', summary.freshness.flags.length ? summary.freshness.flags.map(label).join(', ') : 'none'],
          ]}
        />
        <p className="muted small">
          &quot;First seen by this system&quot; is not the advert&apos;s age: an older listing can be noticed late.{' '}
          <Link to={`/candidates/${summary.listing_id}/lifecycle`}>Detail freshness and detection delay</Link>
        </p>
      </Section>

      <Section title="Normalized specification" id="spec">
        <p className="muted small">{normalized.claims_notice}</p>
        <KeyValues
          items={[
            ['Make / model / generation', modelText(summary)],
            ['Trim', vehicle.trim ?? 'unknown'],
            ['First registration', partialDateText(vehicle.first_registration ?? summary.first_registration)],
            ['Model year', vehicle.model_year?.toString() ?? 'unknown'],
            ['Body type', label(vehicle.body_type)],
            ['Fuel', label(vehicle.fuel)],
            ['Engine', `${vehicle.engine_displacement_cm3 ? `${vehicle.engine_displacement_cm3} cm³` : 'displacement unknown'} · ${vehicle.power_kw ? `${vehicle.power_kw} kW` : 'power unknown'}`],
            ['Gearbox', label(vehicle.gearbox)],
            ['Drive', label(vehicle.drive)],
            ['Mileage', `${kmText(summary.mileage_km)} (${label(summary.mileage_claim)})`],
            ['Seller type', label(normalized.seller_type)],
            ['Location', [normalized.location.city, normalized.location.region, normalized.location.country].filter(Boolean).join(', ') || 'unknown'],
            ['VIN', normalized.documentation.vin ?? 'not provided'],
            ['Registration documents', label(normalized.documentation.registration_documents)],
            ['CoC available', label(normalized.documentation.coc_available)],
            ['Accident free', label(normalized.condition.accident_free)],
            ['Running', label(normalized.condition.running)],
            ['Service history', label(normalized.condition.full_service_history)],
            ['CO₂', normalized.co2.g_per_km ? `${normalized.co2.g_per_km} g/km (${label(normalized.co2.cycle)})` : 'unknown'],
          ]}
        />
        {normalized.condition.mechanical_faults?.length ? (
          <>
            <h3>Mechanical faults (seller claims)</h3>
            <ul>
              {normalized.condition.mechanical_faults.map((fault, index) => (
                <li key={index}>
                  <UntrustedText text={fault} />
                </li>
              ))}
            </ul>
          </>
        ) : null}
        {normalized.warnings.length ? (
          <>
            <h3>Normalization warnings</h3>
            <ul>
              {normalized.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </>
        ) : null}
      </Section>

      <Section title="Seller text (untrusted)" id="seller-text">
        <Notice tone="warn">{detail.seller_text.notice}</Notice>
        <UntrustedText text={detail.seller_text.description_excerpt} as="blockquote" />
      </Section>

      <Section title="Evidence and provenance" id="provenance">
        <p className="muted">
          Confidence below is <strong>extraction confidence, not truth</strong>: it says how reliably a value was read from
          the page, not whether the seller&apos;s claim is correct.
        </p>
        {detail.field_provenance.length === 0 ? (
          <EmptyState>No provenance recorded.</EmptyState>
        ) : (
          <table className="responsive-table">
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Method</th>
                <th scope="col">Extraction confidence (not truth)</th>
                <th scope="col">Claim status</th>
                <th scope="col">Raw text</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {detail.field_provenance.map((item) => (
                <tr key={item.field_path}>
                  <td data-label="Field">
                    <code>{item.field_path}</code>
                  </td>
                  <td data-label="Method">{label(item.method)}</td>
                  <td data-label="Extraction confidence (not truth)">{item.confidence}</td>
                  <td data-label="Claim status">{label(item.claim_status)}</td>
                  <td data-label="Raw text">
                    <UntrustedText text={item.raw_text} />
                  </td>
                  <td data-label="Source">
                    {item.source_url ? <ExternalLink href={item.source_url}>page</ExternalLink> : <span className="muted">none</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <h3>Conflicts</h3>
        {detail.conflicts.length === 0 ? (
          <EmptyState>No conflicting values.</EmptyState>
        ) : (
          <ul className="item-list">
            {detail.conflicts.map((conflict, index) => (
              <li key={`${conflict.field}-${index}`}>
                <code>{conflict.field}</code>: {conflict.values.map((value, i) => (
                  <span key={i}>
                    {i > 0 ? ' vs ' : ''}
                    <UntrustedText text={value} />
                  </span>
                ))}
                {conflict.locations?.length ? <span className="muted"> (in {conflict.locations.join(', ')})</span> : null}
                {conflict.resolution ? <div className="muted">Resolution: {conflict.resolution}</div> : null}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Screening" id="screening">
        {detail.screening ? (
          <>
            <KeyValues
              items={[
                ['State', <Badge key="s" value={detail.screening.state} />],
                ['Profile / queue', `${label(detail.screening.profile)} · ${detail.screening.queue_label ?? 'no queue'}`],
                ['Payable in EUR', <Amount key="e" value={detail.screening.payable_eur} showReason />],
                ['Missing facts', detail.screening.missing_facts.length ? detail.screening.missing_facts.join(', ') : 'none'],
                ['Screening version', detail.screening.screening_version],
              ]}
            />
            <ul className="item-list">
              {detail.screening.reasons.map((reason, index) => (
                <li key={`${reason.code}-${index}`}>
                  <Badge value={reason.severity} tone={reason.severity === 'reject' ? 'bad' : reason.severity === 'needs_facts' ? 'warn' : 'neutral'} />{' '}
                  <code>{reason.code}</code> {reason.message}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <EmptyState>Not screened yet.</EmptyState>
        )}
      </Section>

      <Section title="Due-diligence checklist" id="checklist">
        {detail.due_diligence ? (
          <>
            <p>
              {detail.due_diligence.ready ? (
                <Badge tone="ok">checklist complete</Badge>
              ) : (
                <Badge tone="warn">open items</Badge>
              )}{' '}
              {detail.due_diligence.needs_inspection ? <Badge tone="warn">needs inspection</Badge> : null}{' '}
              {detail.due_diligence.needs_documents ? <Badge tone="warn">needs documents</Badge> : null}{' '}
              {detail.due_diligence.price_confirmation_needed ? <Badge tone="warn">price confirmation needed</Badge> : null}
            </p>
            {detail.due_diligence.photo_limitation ? <p className="muted small">{detail.due_diligence.photo_limitation}</p> : null}
            <ul className="checklist">
              {detail.due_diligence.items.map((item) => (
                <li key={item.topic}>
                  <Badge value={item.status} tone={item.status === 'answered_by_evidence' ? 'ok' : 'warn'} /> {item.question}
                  {item.actions.length ? <span className="muted"> · actions: {item.actions.map(label).join(', ')}</span> : null}
                </li>
              ))}
            </ul>
            {detail.review_case ? (
              <p>
                Record &quot;needs inspection&quot;, &quot;needs documents&quot; or &quot;price confirmation needed&quot; on the{' '}
                <Link to={`/reviews/${detail.review_case.case_id}`}>review case</Link>.
              </p>
            ) : null}
          </>
        ) : (
          <EmptyState>No checklist yet.</EmptyState>
        )}
      </Section>

      <Section title="Valuation and comparables" id="valuation-ref">
        {detail.latest_valuation ? (
          <KeyValues
            items={[
              ['Valuation state', <Badge key="v" value={detail.latest_valuation.state} />],
              [
                `Conservative ${detail.latest_valuation.contribution_label}`,
                <Amount key="c" value={detail.latest_valuation.conservative_contribution} showReason />,
              ],
              [`Base ${detail.latest_valuation.contribution_label}`, <Amount key="b" value={detail.latest_valuation.base_contribution} showReason />],
              ['Expires', <Timestamp key="x" value={detail.latest_valuation.expires_at} timeZone={timeZone} />],
            ]}
          />
        ) : (
          <EmptyState>No valuation yet.</EmptyState>
        )}
        {detail.comparable_set ? (
          <p>
            MK comparables: {detail.comparable_set.sample_size} ({label(detail.comparable_set.sample_quality)} sample), band fit{' '}
            {label(detail.comparable_set.mk_band_fit)}
            {detail.comparable_set.research_needed ? ' · research needed' : ''}.
          </p>
        ) : null}
        {detail.rank ? (
          <p className="muted">
            Ranking score {detail.rank.score} ({detail.rank.label}).
          </p>
        ) : null}
      </Section>

      <NotesSection listingId={summary.listing_id} notes={detail.notes} timeZone={timeZone} onChanged={onChanged} />
      <RecheckSection listingId={summary.listing_id} />
    </>
  )
}

function NotesSection({ listingId, notes, timeZone, onChanged }: { listingId: string; notes: NoteView[]; timeZone: string; onChanged: () => void }) {
  const { client, can } = useWorkspace()
  const [text, setText] = useState('')
  const mutation = useIdempotentMutation<AddNoteRequest, NoteView>('note', (body) => client.addNote(listingId, body), {
    settled: (_attempt, outcome) => {
      if ('envelope' in outcome) {
        setText('')
        onChanged()
      }
    },
  })
  const locked = mutation.phase.kind === 'pending' || mutation.phase.kind === 'unconfirmed'

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!text.trim() || text.length > LIMITS.noteMax) return
    void mutation.submit({ note: text })
  }

  return (
    <Section title="Private notes" id="notes">
      {notes.length === 0 ? (
        <EmptyState>No notes yet.</EmptyState>
      ) : (
        <ul className="item-list">
          {notes.map((note) => (
            <li key={note.note_id}>
              <Badge tone="neutral">{note.label}</Badge> <Timestamp value={note.created_at} timeZone={timeZone} />
              <UntrustedText text={note.body} as="p" />
            </li>
          ))}
        </ul>
      )}
      {can('notes:write') ? (
        <form onSubmit={submit} className="form">
          <label htmlFor="note-text">Add a private note</label>
          <textarea
            id="note-text"
            value={text}
            maxLength={LIMITS.noteMax}
            rows={3}
            onChange={(event) => setText(event.target.value)}
            disabled={locked}
          />
          <div className="form-actions">
            <button type="submit" className="button" disabled={locked || !text.trim()}>
              Add note
            </button>
          </div>
          <MutationStatus
            phase={mutation.phase}
            onRetry={() => void mutation.retry()}
            onDiscard={() => {
              mutation.reset()
              onChanged()
            }}
            confirmed={() => 'Note saved.'}
          />
        </form>
      ) : null}
    </Section>
  )
}

function RecheckSection({ listingId }: { listingId: string }) {
  const { client, can } = useWorkspace()
  const [reason, setReason] = useState('')
  const mutation = useIdempotentMutation<RecheckRequest, RecheckRequestResult>('recheck', (body) => client.recheck(listingId, body))
  if (!can('rechecks:request')) return null
  const locked = mutation.phase.kind === 'pending' || mutation.phase.kind === 'unconfirmed'
  const valid = reason.trim().length >= LIMITS.reasonMin
  return (
    <Section title="Recheck" id="recheck">
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault()
          if (valid) void mutation.submit({ reason: reason.trim() })
        }}
      >
        <label htmlFor="recheck-reason">Reason for a budget-controlled recheck of this registered listing</label>
        <input
          id="recheck-reason"
          value={reason}
          minLength={LIMITS.reasonMin}
          maxLength={LIMITS.reasonMax}
          onChange={(event) => setReason(event.target.value)}
          disabled={locked}
        />
        <div className="form-actions">
          <button type="submit" className="button secondary" disabled={locked || !valid}>
            Request recheck
          </button>
        </div>
        <MutationStatus
          phase={mutation.phase}
          onRetry={() => void mutation.retry()}
          onDiscard={() => mutation.reset()}
          confirmed={(result) =>
            result.deduplicated ? 'A recheck was already queued for this listing.' : `Recheck queued (job state ${label(result.state)}).`
          }
        />
      </form>
    </Section>
  )
}
