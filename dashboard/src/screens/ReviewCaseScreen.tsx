import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import type { ApiError } from '../api/errors'
import type {
  CandidateDetail,
  ClaimRequest,
  ClaimResult,
  ReleaseRequest,
  ReleaseResultView,
  ReviewCaseView,
  ReviewDecisionView,
  SubmitReviewRequest,
} from '../api/types'
import { Amount, Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, UntrustedText, ViewMeta, Warnings, useNow } from '../components/ui'
import { kmText, label } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useClaimStore, type ClaimHandle } from '../review/claimStore'
import {
  DASHBOARD_ACTIONS,
  EMPTY_DRAFT,
  OUTCOMES,
  SUGGESTED_REASON_CODES,
  ACTION_CODES,
  buildSubmission,
  draftForAction,
  type DecisionDraft,
} from '../review/decisionDraft'
import { MutationStatus } from '../review/MutationStatus'
import {
  clearPendingSubmission,
  readPendingSubmission,
  resolvePendingSubmission,
  writePendingSubmission,
  type PendingSubmission,
} from '../review/pendingMarker'
import { useIdempotentMutation } from '../review/useIdempotentMutation'
import { useWorkspace } from '../workspace/WorkspaceProvider'

const CONFLICT_CODES = new Set(['ALREADY_CLAIMED', 'CLAIM_EXPIRED', 'VERSION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'NOT_FOUND'])

export function ReviewCaseScreen() {
  const { caseId = '' } = useParams()
  const { timezone, can, role } = useWorkspace()
  const caseQuery = useApiQuery((client, signal) => client.reviewCase(caseId, { signal }), [caseId])
  const listingId = caseQuery.data?.listing_id ?? null
  const candidateQuery = useApiQuery(
    (client, signal) => (listingId ? client.candidate(listingId, null, { signal }) : Promise.reject(new Error('waiting for the case'))),
    [listingId],
  )
  const [draft, setDraft] = useState<DecisionDraft>(EMPTY_DRAFT)
  // The marker of an unconfirmed submission from BEFORE a page reload (read once on mount).
  const [earlier] = useState<PendingSubmission | null>(() => readPendingSubmission(caseId))
  const canWrite = can('reviews:write')

  if (caseQuery.status === 'loading') return <LoadingState label="Loading review case" />
  if (!caseQuery.data || !caseQuery.envelope) {
    return (
      <div className="screen">
        <p>
          <Link to="/reviews">← Review queue</Link>
        </p>
        {caseQuery.error ? <ErrorPanel error={caseQuery.error} onRetry={caseQuery.reload} /> : null}
      </div>
    )
  }
  const reviewCase = caseQuery.data
  const candidate = candidateQuery.data
  return (
    <div className="screen">
      <p>
        <Link to="/reviews">← Review queue</Link>
      </p>
      <h1>
        Review: {[reviewCase.candidate.make, reviewCase.candidate.model, reviewCase.candidate.generation].filter(Boolean).join(' ') || 'model unknown'}
      </h1>
      <p className="subtitle">
        <span className="muted">Listing title (seller text): </span>
        <UntrustedText text={reviewCase.candidate.title} />
      </p>
      <ViewMeta
        asOf={caseQuery.envelope.as_of}
        fetchedAt={caseQuery.fetchedAt}
        timeZone={timezone}
        onReload={caseQuery.reload}
        reloading={caseQuery.reloading}
      />
      {caseQuery.error ? <ErrorPanel error={caseQuery.error} onRetry={caseQuery.reload} /> : null}
      <Warnings warnings={caseQuery.envelope.warnings} />
      {earlier ? <EarlierSubmissionNotice marker={earlier} reviewCase={reviewCase} onReload={caseQuery.reload} timeZone={timezone} /> : null}

      <Section title="Case" id="case">
        <KeyValues
          items={[
            ['State', <Badge key="s" value={reviewCase.state} />],
            ['Case version', String(reviewCase.case_version)],
            ['Queue', reviewCase.queue_label],
            ['Readiness', label(reviewCase.readiness)],
            ['Listing revision', String(reviewCase.listing_revision)],
            ['Asking price', <Amount key="p" value={reviewCase.candidate.price.payable} />],
            ['EUR equivalent', <Amount key="e" value={reviewCase.candidate.price.eur_equivalent} showReason />],
            ['Mileage', kmText(reviewCase.candidate.mileage_km)],
            ['Seller country', reviewCase.candidate.seller_country ?? 'unknown'],
            ['Eligibility', <Badge key="g" value={reviewCase.candidate.eligibility ?? 'unknown'} />],
            [
              'Valuation',
              reviewCase.valuation ? (
                <Link key="v" to={`/valuations/${reviewCase.valuation.valuation_id}`}>
                  {label(reviewCase.valuation.state)} · base {reviewCase.valuation.contribution_label}:{' '}
                  <Amount value={reviewCase.valuation.base_contribution} />
                </Link>
              ) : (
                'none'
              ),
            ],
          ]}
        />
        <p>
          <Link to={`/candidates/${reviewCase.listing_id}`}>Candidate detail</Link>
          {reviewCase.is_fixture ? <Badge tone="muted">synthetic fixture</Badge> : null}
        </p>
      </Section>

      {canWrite ? (
        <ReviewActions
          reviewCase={reviewCase}
          candidate={candidate}
          draft={draft}
          setDraft={setDraft}
          reload={caseQuery.reload}
          timeZone={timezone}
        />
      ) : (
        <Notice tone="info">
          Read-only: your role ({role}) can view this case but cannot claim it or record a decision.
        </Notice>
      )}

      <DecisionHistory decisions={reviewCase.decisions} timeZone={timezone} />
    </div>
  )
}

function EarlierSubmissionNotice({
  marker,
  reviewCase,
  onReload,
  timeZone,
}: {
  marker: PendingSubmission
  reviewCase: ReviewCaseView
  onReload: () => void
  timeZone: string
}) {
  const resolution = resolvePendingSubmission(marker, reviewCase)
  useEffect(() => {
    if (resolution.kind !== 'not_recorded') clearPendingSubmission(marker.caseId)
  }, [resolution.kind, marker.caseId])
  return (
    <div className="notice notice-warn" role="status" data-testid="earlier-submission">
      <p className="panel-title">A decision was being submitted when this page was reloaded</p>
      <p>Nothing was resent automatically.</p>
      {resolution.kind === 'recorded' ? (
        <p data-testid="earlier-submission-recorded">
          The server recorded it: {label(resolution.decision.outcome)} at{' '}
          <Timestamp value={resolution.decision.decided_at} timeZone={timeZone} /> (confirmed by the server).
        </p>
      ) : resolution.kind === 'not_recorded' ? (
        <>
          <p data-testid="earlier-submission-not-recorded">
            As of this page load the server has not recorded it (the case is still at version {reviewCase.case_version}). The
            one-time claim handle is kept in memory only, so claim the case again to submit.
          </p>
          <button type="button" className="button secondary" onClick={onReload}>
            Check again
          </button>
        </>
      ) : (
        <p>The case has changed since (now version {reviewCase.case_version}); see the decision history below.</p>
      )}
    </div>
  )
}

function ReviewActions({
  reviewCase,
  candidate,
  draft,
  setDraft,
  reload,
  timeZone,
}: {
  reviewCase: ReviewCaseView
  candidate: CandidateDetail | null
  draft: DecisionDraft
  setDraft: (draft: DecisionDraft | ((previous: DecisionDraft) => DecisionDraft)) => void
  reload: () => void
  timeZone: string
}) {
  const { client } = useWorkspace()
  const claims = useClaimStore()
  const caseId = reviewCase.case_id
  const handle = claims.get(caseId)
  const now = useNow(1000)
  const [showProblems, setShowProblems] = useState(false)
  const autoReclaimed = useRef<string | null>(null)

  const claim = useIdempotentMutation<ClaimRequest, ClaimResult>('review-claim', (body) => client.claim(caseId, body), {
    settled: (_attempt, outcome) => {
      if ('envelope' in outcome) {
        const result = outcome.envelope.data
        if (result.claim_token) claims.remember({ ...result, claim_token: result.claim_token })
        reload()
      }
    },
  })
  const release = useIdempotentMutation<ReleaseRequest, ReleaseResultView>('review-release', (body) => client.release(caseId, body), {
    settled: (_attempt, outcome) => {
      if ('envelope' in outcome || outcome.error.code === 'CLAIM_EXPIRED') {
        claims.forget(caseId)
        reload()
      }
    },
  })
  const submit = useIdempotentMutation<SubmitReviewRequest, ReviewDecisionView>(
    'review-submit',
    (body) => client.submit(caseId, body),
    {
      beforeSend: (attempt) =>
        writePendingSubmission({
          caseId,
          idempotencyKey: attempt.key,
          outcome: attempt.body.outcome,
          expectedVersion: attempt.body.expected_version,
          startedAt: new Date().toISOString(),
        }),
      settled: (_attempt, outcome) => {
        clearPendingSubmission(caseId)
        if ('envelope' in outcome) {
          claims.forget(caseId)
          setDraft(EMPTY_DRAFT)
          setShowProblems(false)
          reload()
        } else if (outcome.error.code === 'CLAIM_EXPIRED') {
          claims.forget(caseId)
        }
      },
    },
  )

  // An idempotent replay of a claim does not repeat the one-time token: claim once more (same
  // principal) to rotate it. Done at most once per replayed attempt.
  useEffect(() => {
    const phase = claim.phase
    if (phase.kind !== 'confirmed' || phase.envelope.data.claim_token !== null) return
    if (autoReclaimed.current === phase.attempt.key) return
    autoReclaimed.current = phase.attempt.key
    void claim.submit({ expected_version: phase.envelope.data.case_version })
  }, [claim])

  const handleActive = handle !== null && new Date(handle.expiresAt).getTime() > now
  const secondsLeft = handle ? Math.max(0, Math.round((new Date(handle.expiresAt).getTime() - now) / 1000)) : 0
  const submitLocked = submit.phase.kind === 'pending' || submit.phase.kind === 'unconfirmed'
  const check = useMemo(() => buildSubmission(draft, handleActive ? handle : null), [draft, handle, handleActive])
  const closed = reviewCase.state === 'rejected' || reviewCase.state === 'superseded'

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    setShowProblems(true)
    if (submitLocked || !check.body) return
    void submit.submit(check.body)
  }

  return (
    <>
      <Section title="Claim" id="claim">
        <ClaimStateText reviewCase={reviewCase} handle={handle} handleActive={handleActive} secondsLeft={secondsLeft} timeZone={timeZone} />
        {handle && reviewCase.case_version > handle.caseVersion ? (
          <Notice tone="warn">
            The case changed after you claimed it (now version {reviewCase.case_version}, you claimed version {handle.caseVersion}
            {reviewCase.listing_revision !== handle.listingRevision ? `; listing revision ${reviewCase.listing_revision} is new` : ''}
            ). Review the new facts and claim again before deciding.
          </Notice>
        ) : null}
        <div className="form-actions">
          {!closed ? (
            <button
              type="button"
              className="button"
              disabled={claim.phase.kind === 'pending' || claim.phase.kind === 'unconfirmed'}
              onClick={() => void claim.submit({ expected_version: reviewCase.case_version })}
            >
              {handleActive ? 'Renew claim' : reviewCase.claim.held_by_caller ? 'Claim again' : 'Claim'}
            </button>
          ) : (
            <span className="muted">This case is {label(reviewCase.state)} and cannot be claimed.</span>
          )}
          {handle ? (
            <button
              type="button"
              className="button secondary"
              disabled={release.phase.kind === 'pending' || release.phase.kind === 'unconfirmed'}
              onClick={() => void release.submit({ claim_token: handle.token })}
            >
              Release claim
            </button>
          ) : null}
        </div>
        <MutationStatus
          phase={claim.phase}
          onRetry={() => void claim.retry()}
          onDiscard={() => {
            claim.reset()
            reload()
          }}
          pendingText="Claiming…"
          confirmed={(result) =>
            result.claim_token
              ? `Claimed until ${new Date(result.expires_at).toLocaleTimeString()} (case version ${result.case_version}).`
              : 'The claim was recorded earlier; fetching a fresh claim handle…'
          }
          extraOnRejected={
            claim.phase.kind === 'rejected' ? (
              <ConflictActions error={claim.phase.error} onReload={reload} hint="Reload to see who holds it and the current version." />
            ) : null
          }
        />
        <MutationStatus
          phase={release.phase}
          onRetry={() => void release.retry()}
          onDiscard={() => release.reset()}
          pendingText="Releasing…"
          confirmed={(result) => (result.released ? 'Claim released.' : `Nothing to release (${label(result.reason)}).`)}
        />
      </Section>

      <Section title="Decision" id="decision">
        <p className="muted">
          Decisions are evidence-grounded review outcomes; none of them buys a vehicle or contacts a seller.
        </p>
        <div className="form-actions" role="group" aria-label="Due-diligence actions">
          {DASHBOARD_ACTIONS.map((item) => (
            <button
              key={item.action}
              type="button"
              className="button secondary"
              disabled={submitLocked}
              onClick={() => setDraft((previous) => draftForAction(item.action, candidate?.due_diligence ?? null, previous))}
            >
              {item.label}
            </button>
          ))}
        </div>
        <DecisionForm
          draft={draft}
          setDraft={setDraft}
          candidate={candidate}
          handle={handleActive ? handle : null}
          locked={submitLocked}
          problems={showProblems ? check.problems : []}
          onSubmit={onSubmit}
          submitting={submit.phase.kind === 'pending'}
        />
        <MutationStatus
          phase={submit.phase}
          onRetry={() => void submit.retry()}
          onDiscard={() => {
            clearPendingSubmission(caseId)
            submit.reset()
            reload()
          }}
          pendingText="Submitting decision…"
          confirmed={(decision) => (
            <span data-testid="decision-saved">
              Decision saved: {label(decision.outcome)} (case is now {label(decision.case_state)}, version {decision.new_case_version}).
            </span>
          )}
          extraOnRejected={
            submit.phase.kind === 'rejected'
              ? <ConflictActions error={submit.phase.error} onReload={reload} hint="Your draft is kept; reload, claim again if needed, then resubmit." />
              : null
          }
        />
      </Section>
    </>
  )
}

function ConflictActions({ error, onReload, hint }: { error: ApiError; onReload: () => void; hint: string }) {
  if (!CONFLICT_CODES.has(error.code)) return null
  return (
    <div className="form-actions">
      <button type="button" className="button secondary" onClick={onReload}>
        Reload the case
      </button>
      <span className="muted">{hint}</span>
    </div>
  )
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value]
}

function ClaimStateText({
  reviewCase,
  handle,
  handleActive,
  secondsLeft,
  timeZone,
}: {
  reviewCase: ReviewCaseView
  handle: ClaimHandle | null
  handleActive: boolean
  secondsLeft: number
  timeZone: string
}) {
  if (handle && handleActive) {
    const minutes = Math.floor(secondsLeft / 60)
    const seconds = String(secondsLeft % 60).padStart(2, '0')
    return (
      <p data-testid="claim-state" className={secondsLeft < 60 ? 'warn-text' : undefined}>
        You hold the claim until <Timestamp value={handle.expiresAt} timeZone={timeZone} /> ({minutes}:{seconds} left)
        {secondsLeft < 60 ? '. It expires soon: submit or renew.' : '.'}
      </p>
    )
  }
  if (handle && !handleActive) {
    return (
      <p data-testid="claim-state" className="warn-text">
        Your claim expired at <Timestamp value={handle.expiresAt} timeZone={timeZone} />. Submitting now would fail; claim again
        (your draft is kept).
      </p>
    )
  }
  if (reviewCase.claim.claimed && reviewCase.claim.held_by_caller) {
    return (
      <p data-testid="claim-state">
        You hold a claim from an earlier page load, but its one-time handle is not available here (handles are kept in memory
        only). Claim again to continue.
      </p>
    )
  }
  if (reviewCase.claim.claimed) {
    return (
      <p data-testid="claim-state">
        Claimed by another reviewer until <Timestamp value={reviewCase.claim.expires_at} timeZone={timeZone} />.
      </p>
    )
  }
  return <p data-testid="claim-state">Not claimed. Claim the case to record a decision.</p>
}

function DecisionForm({
  draft,
  setDraft,
  candidate,
  handle,
  locked,
  problems,
  onSubmit,
  submitting,
}: {
  draft: DecisionDraft
  setDraft: (draft: DecisionDraft | ((previous: DecisionDraft) => DecisionDraft)) => void
  candidate: CandidateDetail | null
  handle: ClaimHandle | null
  locked: boolean
  problems: Array<{ field: keyof DecisionDraft; message: string }>
  onSubmit: (event: FormEvent) => void
  submitting: boolean
}) {
  const update = <K extends keyof DecisionDraft>(key: K, value: DecisionDraft[K]) => setDraft((previous) => ({ ...previous, [key]: value }))
  const errorFor = (field: keyof DecisionDraft) => problems.filter((problem) => problem.field === field)
  const evidenceOptions = useMemo(() => {
    const options = new Map<string, string>()
    for (const item of candidate?.field_provenance ?? []) {
      if (item.evidence_id) options.set(item.evidence_id, `${item.field_path} (${item.method})`)
    }
    for (const item of candidate?.due_diligence?.items ?? []) {
      for (const id of item.evidence_ids ?? []) if (!options.has(id)) options.set(id, `checklist: ${label(item.topic)}`)
    }
    return [...options.entries()]
  }, [candidate])

  return (
    <form className="form decision-form" onSubmit={onSubmit} noValidate aria-describedby="decision-problems">
      <fieldset disabled={locked}>
        <legend>Outcome</legend>
        {OUTCOMES.map((outcome) => (
          <label key={outcome.value} className="radio">
            <input
              type="radio"
              name="outcome"
              value={outcome.value}
              checked={draft.outcome === outcome.value}
              onChange={() =>
                setDraft((previous) => ({
                  ...previous,
                  outcome: outcome.value,
                  // Action codes only belong to needs_information decisions.
                  reasonCodes:
                    outcome.value === 'needs_information' ? previous.reasonCodes : previous.reasonCodes.filter((code) => !ACTION_CODES.has(code)),
                }))
              }
            />{' '}
            {outcome.label} <span className="muted small">{outcome.hint}</span>
          </label>
        ))}
        <FieldErrors errors={errorFor('outcome')} />
      </fieldset>

      <fieldset disabled={locked}>
        <legend>Reason codes (required)</legend>
        <div className="checkbox-grid">
          {SUGGESTED_REASON_CODES.map((code) => {
            const actionOnly = ACTION_CODES.has(code)
            const disabled = actionOnly && draft.outcome !== '' && draft.outcome !== 'needs_information'
            return (
              <label key={code} className="checkbox">
                <input
                  type="checkbox"
                  checked={draft.reasonCodes.includes(code)}
                  disabled={disabled}
                  onChange={() => update('reasonCodes', toggle(draft.reasonCodes, code))}
                />{' '}
                {label(code)}
              </label>
            )
          })}
        </div>
        <FieldErrors errors={errorFor('reasonCodes')} />
        <label htmlFor="extra-codes">Other reason codes (comma separated)</label>
        <input
          id="extra-codes"
          value={draft.extraReasonCodes}
          onChange={(event) => update('extraReasonCodes', event.target.value)}
          aria-invalid={errorFor('extraReasonCodes').length > 0}
        />
        <FieldErrors errors={errorFor('extraReasonCodes')} />
      </fieldset>

      <fieldset disabled={locked}>
        <legend>Summary and evidence</legend>
        <label htmlFor="decision-summary">Summary (required, 10 to 4,000 characters): the rationale and evidence trail</label>
        <textarea
          id="decision-summary"
          rows={4}
          maxLength={4000}
          value={draft.summary}
          onChange={(event) => update('summary', event.target.value)}
          aria-invalid={errorFor('summary').length > 0}
        />
        <FieldErrors errors={errorFor('summary')} />
        <p className="label-like">Evidence ids cited</p>
        {evidenceOptions.length ? (
          <div className="checkbox-grid">
            {evidenceOptions.map(([id, text]) => (
              <label key={id} className="checkbox">
                <input type="checkbox" checked={draft.evidenceIds.includes(id)} onChange={() => update('evidenceIds', toggle(draft.evidenceIds, id))} />{' '}
                {text}
              </label>
            ))}
          </div>
        ) : (
          <p className="muted small">No recorded evidence ids for this listing.</p>
        )}
        <FieldErrors errors={errorFor('evidenceIds')} />
        <label htmlFor="extra-evidence">Other evidence ids (UUIDs, comma separated)</label>
        <input
          id="extra-evidence"
          value={draft.extraEvidenceIds}
          onChange={(event) => update('extraEvidenceIds', event.target.value)}
          aria-invalid={errorFor('extraEvidenceIds').length > 0}
        />
        <FieldErrors errors={errorFor('extraEvidenceIds')} />
        <label htmlFor="missing-info">Missing information (one item per line; required for &quot;needs information&quot;)</label>
        <textarea
          id="missing-info"
          rows={3}
          value={draft.missingInformation}
          onChange={(event) => update('missingInformation', event.target.value)}
          aria-invalid={errorFor('missingInformation').length > 0}
        />
        <FieldErrors errors={errorFor('missingInformation')} />
        <label className="checkbox">
          <input
            type="checkbox"
            checked={draft.citeValuation}
            disabled={!handle?.valuationId}
            onChange={(event) => update('citeValuation', event.target.checked)}
          />{' '}
          Cite the case&apos;s current valuation {handle?.valuationId ? '' : '(none on the claimed version)'}
        </label>
        <FieldErrors errors={errorFor('citeValuation')} />
      </fieldset>

      <div id="decision-problems" className="sr-only">
        {problems.length ? `${problems.length} problems: ${problems.map((p) => p.message).join(' ')}` : ''}
      </div>
      <div className="form-actions">
        <button type="submit" className="button" disabled={locked || !handle} aria-disabled={locked || !handle}>
          {submitting ? 'Submitting…' : 'Submit decision'}
        </button>
        {!handle ? <span className="muted">Claim the case to submit.</span> : null}
      </div>
    </form>
  )
}

function FieldErrors({ errors }: { errors: Array<{ message: string }> }) {
  if (!errors.length) return null
  return (
    <ul className="field-errors" role="alert">
      {errors.map((error) => (
        <li key={error.message}>{error.message}</li>
      ))}
    </ul>
  )
}

function DecisionHistory({ decisions, timeZone }: { decisions: ReviewDecisionView[]; timeZone: string }) {
  const ordered = [...decisions].sort((a, b) => b.decided_at.localeCompare(a.decided_at))
  return (
    <Section title="Decision history" id="history">
      {ordered.length === 0 ? (
        <EmptyState>No decisions yet.</EmptyState>
      ) : (
        <ol className="item-list" data-testid="decision-history">
          {ordered.map((decision) => (
            <li key={decision.decision_id} data-testid="decision-entry">
              <Badge value={decision.outcome} /> <Timestamp value={decision.decided_at} timeZone={timeZone} />{' '}
              <span className="muted">
                by a {decision.actor.role} ({label(decision.actor.principal_kind)}) · version {decision.case_version} → {decision.new_case_version}
              </span>
              <div>
                Reasons: {decision.reason_codes.map((code) => <code key={code}>{code} </code>)}
              </div>
              <UntrustedText text={decision.summary} as="p" />
              {decision.missing_information.length ? (
                <ul>
                  {decision.missing_information.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              ) : null}
              {decision.evidence_ids.length ? <div className="muted small">{decision.evidence_ids.length} evidence ids cited</div> : null}
            </li>
          ))}
        </ol>
      )}
    </Section>
  )
}
