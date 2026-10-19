/**
 * Settings: the owner's "Tax rules and cost profile" section (docs/api_contract.md 10.2, owner
 * approval path; `config:admin` only).
 *
 * - Lists every stored tax rule-set version and every repository draft not stored yet, with a status
 *   chip, the content hash, the sources and components, and the UNCERTAIN items that are the open
 *   questions to resolve before an approval.
 * - Each owner act (store a draft, submit for review, approve with a named approver and a review
 *   record, activate, revoke, approve the cost profile) is one explicit request: the owner first
 *   reads what it does to PRODUCTION valuations and ticks the confirmation. Nothing is approved or
 *   activated automatically; the server checks the status, the content hash and the lifecycle again.
 * - Requests go through `useIdempotentMutation` (one key per attempt, same-key retry after an
 *   unknown outcome), like every other dashboard mutation.
 * - Non-owners see a short read-only note and no request is made for them.
 */
import { useState } from 'react'
import {
  LIMITS,
  type CostProfileApproveRequest,
  type CostProfileApproveResult,
  type CostProfileSummaryView,
  type CostProfilesView,
  type TaxRuleAction,
  type TaxRuleChangeResult,
  type TaxRuleSetSummaryView,
  type TaxRuleSetsView,
  type TaxRuleStatus,
  type TaxRuleStoreRequest,
  type TaxRuleTransitionRequest,
} from '../../api/types'
import { Badge, EmptyState, ErrorPanel, LoadingState, Notice, Section, Timestamp, useNow } from '../../components/ui'
import { groupDecimal, label, localInputToRfc3339, rfc3339ToLocalInput, shortHash } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { MutationStatus } from '../../review/MutationStatus'
import { useIdempotentMutation, type MutationPhase } from '../../review/useIdempotentMutation'
import { useWorkspace } from '../../workspace/WorkspaceProvider'

const TITLE = 'Tax rules and cost profile'
const SUBJECT = 'tax rule set'
/** The server allows a review time up to 5 minutes ahead of its clock (browser clocks drift). */
const REVIEW_CLOCK_SKEW_MS = 5 * 60 * 1000

/** Status chip tones: only an ACTIVE, owner-approved version is used for production valuations. */
const STATUS_TONES: Record<TaxRuleStatus, string> = {
  draft: 'warn',
  under_review: 'info',
  approved: 'info',
  active: 'ok',
  superseded: 'muted',
  expired: 'muted',
  revoked: 'muted',
  unapproved: 'muted',
}

const ACTION_LABELS: Record<TaxRuleAction, string> = {
  store: 'Store draft',
  under_review: 'Submit for review',
  approved: 'Approve',
  active: 'Activate',
  revoked: 'Revoke',
}

/** What each act does to PRODUCTION valuations: shown before the owner can confirm it. */
const PRODUCTION_EFFECT: Record<TaxRuleAction, string> = {
  store:
    'Stores this file in the workspace as status draft. It is NOT approved: production valuations and alerts do not change; the import-cost calculator may show it only as an indicative preview.',
  under_review:
    'Submits it for review: its content hash is sealed and its content frozen. Production valuations do not change yet; nothing is approved.',
  approved:
    'Records your approval under the approver name and review record below, bound to this content hash. Production valuations do not change until it is activated, and activating it WILL change them.',
  active:
    'Activates it: from now on PRODUCTION valuations, and the alerts built on them, compute customs duty, motor-vehicle tax (ДМВ) and import VAT with this rule set for its jurisdiction, vehicle categories and period.',
  revoked:
    'Revokes it: it is never selected again. Production valuations that used it are marked stale and recomputed; their import charges stay unknown until another rule set is active.',
}

export function TaxRulesSection() {
  const { can } = useWorkspace()
  if (!can('config:admin')) {
    return (
      <Section title={TITLE} id="tax-rules">
        <p className="muted" data-testid="tax-rules-owner-only">
          Tax rule sets and the cost profile are reviewed, approved and activated by the workspace owner only. Valuations use
          only an ACTIVE, owner-approved rule set.
        </p>
      </Section>
    )
  }
  return <OwnerTaxRules />
}

function busy(phase: { kind: string }): boolean {
  return phase.kind === 'pending' || phase.kind === 'unconfirmed'
}

interface Selected {
  ruleSet: TaxRuleSetSummaryView
  action: TaxRuleAction
}

function OwnerTaxRules() {
  const { client, timezone } = useWorkspace()
  const rules = useApiQuery((api, signal) => api.taxRuleSets({ signal }), [])
  const profiles = useApiQuery((api, signal) => api.costProfiles({ signal }), [])
  const [selected, setSelected] = useState<Selected | null>(null)
  const settle = {
    settled: (_attempt: unknown, outcome: { envelope: unknown } | { error: unknown }) => {
      if ('envelope' in outcome) setSelected(null)
      rules.reload()
      profiles.reload()
    },
  }
  const store = useIdempotentMutation<TaxRuleStoreRequest, TaxRuleChangeResult>(
    'tax-rules-store',
    (body) => client.storeTaxRuleDraft(body),
    settle,
  )
  const move = useIdempotentMutation<TaxRuleTransitionRequest, TaxRuleChangeResult>(
    'tax-rules-transition',
    (body) => client.transitionTaxRuleSet(body),
    settle,
  )
  const locked = busy(store.phase) || busy(move.phase)

  return (
    <Section title={TITLE} id="tax-rules">
      <Notice tone="warn">
        <span data-testid="tax-rules-reminder">
          Owner only. Before you approve a rule set, resolve every UNCERTAIN item listed with it (with a qualified customs
          broker or tax adviser), re-fetch and re-hash every source, and run the golden cases (docs/tax_rule_approval.md).
          Nothing is approved or activated automatically.
        </span>
      </Notice>
      {rules.status === 'loading' ? <LoadingState label="Loading the tax rule sets" /> : null}
      {rules.error ? <ErrorPanel error={rules.error} onRetry={rules.reload} /> : null}
      {rules.data ? (
        <RuleSets
          view={rules.data}
          timeZone={timezone}
          locked={locked}
          selected={selected}
          onSelect={(next) => {
            store.reset()
            move.reset()
            setSelected(next)
          }}
        />
      ) : null}
      {selected ? (
        <ActionForm
          key={`${selected.ruleSet.label}:${selected.action}`}
          selected={selected}
          locked={locked}
          onCancel={() => setSelected(null)}
          onStore={(body) => void store.submit(body)}
          onMove={(body) => void move.submit(body)}
        />
      ) : null}
      <ResultStatus phase={store.phase} onRetry={() => void store.retry()} onDiscard={() => store.reset()} />
      <ResultStatus phase={move.phase} onRetry={() => void move.retry()} onDiscard={() => move.reset()} />
      <h3>Cost profile</h3>
      {profiles.status === 'loading' ? <LoadingState label="Loading the cost profile" /> : null}
      {profiles.error ? <ErrorPanel error={profiles.error} onRetry={profiles.reload} /> : null}
      {profiles.data ? <CostProfileCard view={profiles.data} timeZone={timezone} onApproved={profiles.reload} /> : null}
    </Section>
  )
}

function ResultStatus({
  phase,
  onRetry,
  onDiscard,
}: {
  phase: MutationPhase<{ idempotency_key: string }, TaxRuleChangeResult>
  onRetry: () => void
  onDiscard: () => void
}) {
  if (phase.kind === 'idle') return null
  return (
    <MutationStatus
      phase={phase}
      subject={SUBJECT}
      onRetry={onRetry}
      onDiscard={onDiscard}
      confirmed={(result) => (
        <span data-testid="tax-rule-result">
          {result.label}: {result.previous_status ? `${label(result.previous_status)} → ` : ''}
          {label(result.status)}. {result.production_effect}
        </span>
      )}
    />
  )
}

function RuleSets({
  view,
  timeZone,
  locked,
  selected,
  onSelect,
}: {
  view: TaxRuleSetsView
  timeZone: string
  locked: boolean
  selected: Selected | null
  onSelect: (selected: Selected) => void
}) {
  const calculator = view.calculator
  return (
    <>
      <p className="muted small">{view.production_note}</p>
      <p data-testid="calculator-selection">
        Import-cost calculator today ({calculator.jurisdiction} {label(calculator.vehicle_category)}):{' '}
        <Badge tone={calculator.mode === 'active' ? 'ok' : calculator.mode === 'unavailable' ? 'bad' : 'warn'}>
          {label(calculator.mode)}
        </Badge>{' '}
        {calculator.rule_set ?? 'no rule set'}
        <span className="muted small"> · {calculator.reason}</span>
      </p>
      {view.rule_sets.length === 0 ? (
        <EmptyState>No stored rule set and no draft file.</EmptyState>
      ) : (
        <table className="responsive-table" aria-label="Tax rule sets">
          <thead>
            <tr>
              <th scope="col">Rule set</th>
              <th scope="col">Status</th>
              <th scope="col">Period</th>
              <th scope="col">Content hash</th>
              <th scope="col">Open questions</th>
              <th scope="col">Owner actions</th>
            </tr>
          </thead>
          <tbody>
            {view.rule_sets.map((ruleSet) => (
              <tr key={`${ruleSet.origin}:${ruleSet.label}`} data-testid={`rule-set-${ruleSet.label}`}>
                <td data-label="Rule set">
                  <code>{ruleSet.label}</code>
                  <div className="muted small">
                    {ruleSet.jurisdiction} {ruleSet.vehicle_categories.map(label).join(', ')} ·{' '}
                    {ruleSet.origin === 'draft_file' ? `repository draft ${ruleSet.file_name ?? ''} (not stored)` : 'stored'}
                    {ruleSet.is_fixture ? ' · SYNTHETIC fixture' : ''}
                  </div>
                </td>
                <td data-label="Status">
                  <Badge tone={STATUS_TONES[ruleSet.status]}>{label(ruleSet.status)}</Badge>
                  {ruleSet.production_selectable ? <div className="small">used for production valuations</div> : null}
                  {ruleSet.approval ? (
                    <div className="small" data-testid="rule-set-approval">
                      approved by {ruleSet.approval.approved_by} on{' '}
                      <Timestamp value={ruleSet.approval.approved_at} timeZone={timeZone} />
                    </div>
                  ) : null}
                </td>
                <td data-label="Period">
                  {ruleSet.valid_from ?? 'unset'} – {ruleSet.valid_to ?? 'open'} {ruleSet.currency ?? ''}
                </td>
                <td data-label="Content hash">
                  <code title={ruleSet.content_sha256}>{shortHash(ruleSet.content_sha256)}</code>
                  <div className="muted small">{ruleSet.sha256_sealed ? 'sealed' : 'computed (unsealed draft)'}</div>
                </td>
                <td data-label="Open questions">{ruleSet.uncertain_items.length} UNCERTAIN</td>
                <td data-label="Owner actions">
                  {ruleSet.allowed_actions.length === 0 ? <span className="muted">none</span> : null}
                  {ruleSet.allowed_actions.map((action) => (
                    <button
                      key={action}
                      type="button"
                      className={`button small ${action === 'revoked' ? 'danger' : 'secondary'}`}
                      disabled={locked}
                      aria-pressed={selected?.ruleSet.label === ruleSet.label && selected.action === action}
                      onClick={() => onSelect({ ruleSet, action })}
                    >
                      {ACTION_LABELS[action]}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {view.rule_sets.map((ruleSet) => (
        <RuleSetDetails key={`details:${ruleSet.origin}:${ruleSet.label}`} ruleSet={ruleSet} timeZone={timeZone} />
      ))}
      {view.file_notes.length ? (
        <Notice tone="warn">
          <span data-testid="draft-file-notes">Draft files not offered: {view.file_notes.join(' · ')}</span>
        </Notice>
      ) : null}
    </>
  )
}

function RuleSetDetails({ ruleSet, timeZone }: { ruleSet: TaxRuleSetSummaryView; timeZone: string }) {
  const record = ruleSet.approval?.review_record ?? null
  return (
    <details className="rule-set-details" data-testid={`rule-set-details-${ruleSet.label}`}>
      <summary>
        {ruleSet.label}: {ruleSet.uncertain_items.length} UNCERTAIN item(s), {ruleSet.sources.length} source(s),{' '}
        {ruleSet.components.length} component(s)
      </summary>
      {ruleSet.description ? <p className="small">{ruleSet.description}</p> : null}
      {ruleSet.uncertain_items.length ? (
        <>
          <p className="panel-title">Open questions to resolve before an approval</p>
          <ul data-testid={`uncertain-${ruleSet.label}`}>
            {ruleSet.uncertain_items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </>
      ) : null}
      <p className="panel-title">Sources</p>
      <ul>
        {ruleSet.sources.map((source) => (
          <li key={source.url}>
            {source.title} <span className="muted small">(retrieved <Timestamp value={source.retrieved_at} timeZone={timeZone} />,
            document hash <code title={source.sha256}>{shortHash(source.sha256)}</code>; {source.url})</span>
          </li>
        ))}
      </ul>
      <p className="panel-title">Components</p>
      <ul>
        {ruleSet.components.map((component) => (
          <li key={component.id}>
            <code>{component.id}</code> {component.label}{' '}
            <span className="muted small">
              ({label(component.category)}, {component.kind}
              {component.intermediate ? ', helper: never counted in a total' : ''}
              {component.uncertain ? ', UNCERTAIN' : ''})
            </span>
          </li>
        ))}
      </ul>
      {record ? (
        <p className="small" data-testid={`review-record-${ruleSet.label}`}>
          Review by {record.reviewer} on <Timestamp value={record.reviewed_at} timeZone={timeZone} />: {record.scope}
          {record.professional_review ? ` · professional review ${record.professional_review}` : ''}
          {record.evidence_refs.length ? ` · evidence ${record.evidence_refs.join(', ')}` : ''}
        </p>
      ) : null}
    </details>
  )
}

/** The canonical approval record the server stores (`valuation_repo.approval_reference`), for the length hint. */
function approvalRecordLength(approver: string, record: Record<string, unknown>): number {
  const sortedRecord = Object.fromEntries(Object.keys(record).sort().map((name) => [name, record[name]]))
  return JSON.stringify({ approver, review_record: sortedRecord }).length
}

function ActionForm({
  selected,
  locked,
  onCancel,
  onStore,
  onMove,
}: {
  selected: Selected
  locked: boolean
  onCancel: () => void
  onStore: (body: Omit<TaxRuleStoreRequest, 'idempotency_key'>) => void
  onMove: (body: Omit<TaxRuleTransitionRequest, 'idempotency_key'>) => void
}) {
  const { ruleSet, action } = selected
  const [confirmed, setConfirmed] = useState(false)
  const [approver, setApprover] = useState('')
  const [reviewer, setReviewer] = useState('')
  const [reviewedAt, setReviewedAt] = useState(() => rfc3339ToLocalInput(new Date().toISOString()))
  const [scope, setScope] = useState('')
  const [professional, setProfessional] = useState('')
  const [evidence, setEvidence] = useState('')
  const [notes, setNotes] = useState('')
  const [reviewedHash, setReviewedHash] = useState(ruleSet.content_sha256)
  const now = useNow()

  const approving = action === 'approved'
  const reviewedAtValue = localInputToRfc3339(reviewedAt)
  const evidenceRefs = evidence
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  const record = {
    content_sha256: reviewedHash.trim(),
    evidence_refs: evidenceRefs,
    notes: notes.trim() || null,
    professional_review: professional.trim() || null,
    reviewed_at: reviewedAtValue,
    reviewer: reviewer.trim(),
    scope: scope.trim(),
  }
  const recordLength = approving ? approvalRecordLength(approver.trim(), record) : 0
  const problems: string[] = []
  if (approving) {
    if (!approver.trim()) problems.push('Name the approver.')
    if (!reviewer.trim()) problems.push('Name the reviewer.')
    if (!reviewedAtValue) problems.push('Give the review time.')
    else if (Date.parse(reviewedAtValue) > now + REVIEW_CLOCK_SKEW_MS) problems.push('The review time is in the future.')
    if (scope.trim().length < LIMITS.reasonMin) problems.push('Describe what the review verified.')
    if (!LIMITS.sha256Pattern.test(record.content_sha256)) problems.push('The reviewed content hash must be 64 lower-case hex digits.')
    else if (record.content_sha256 !== ruleSet.content_sha256)
      problems.push('The reviewed content hash differs from this rule set’s content hash: the review does not cover this content.')
    if (evidenceRefs.length > 20) problems.push('At most 20 evidence references.')
    if (recordLength > LIMITS.approvalReferenceMax)
      problems.push(`The approval record has about ${recordLength} characters; at most ${LIMITS.approvalReferenceMax} fit. Shorten it.`)
  }
  const ready = confirmed && problems.length === 0 && !locked

  return (
    <form
      className="form"
      data-testid="tax-rule-action-form"
      aria-label={`${ACTION_LABELS[action]} ${ruleSet.label}`}
      onSubmit={(event) => {
        event.preventDefault()
        if (!ready) return
        if (action === 'store') {
          if (!ruleSet.file_name) return
          onStore({ file_name: ruleSet.file_name, content_sha256: ruleSet.content_sha256 })
          return
        }
        const body: Omit<TaxRuleTransitionRequest, 'idempotency_key'> = {
          rule_set_id: ruleSet.rule_set_id,
          version: ruleSet.version,
          expected_status: ruleSet.status,
          target: action,
          content_sha256: ruleSet.content_sha256,
        }
        if (approving && reviewedAtValue) {
          body.approved_by = approver.trim()
          body.review_record = {
            reviewer: record.reviewer,
            reviewed_at: reviewedAtValue,
            scope: record.scope,
            evidence_refs: evidenceRefs,
            content_sha256: record.content_sha256,
            ...(record.professional_review ? { professional_reference: record.professional_review } : {}),
            ...(record.notes ? { notes: record.notes } : {}),
          }
        }
        onMove(body)
      }}
    >
      <p className="panel-title">
        {ACTION_LABELS[action]}: <code>{ruleSet.label}</code> ({label(ruleSet.status)})
      </p>
      <Notice tone={action === 'active' || action === 'revoked' ? 'bad' : 'warn'}>
        <span data-testid="production-effect">Effect on production valuations: {PRODUCTION_EFFECT[action]}</span>
      </Notice>
      <p className="small">
        Content hash <code data-testid="action-content-hash">{ruleSet.content_sha256}</code>
        {ruleSet.uncertain_items.length ? ` · ${ruleSet.uncertain_items.length} UNCERTAIN item(s) still marked in this rule set` : ''}
      </p>
      {approving ? (
        <fieldset>
          <legend>Approval and review record</legend>
          <label htmlFor="approval-approver">Approver name (recorded with the approval)</label>
          <input id="approval-approver" value={approver} maxLength={200} onChange={(e) => setApprover(e.target.value)} disabled={locked} />
          <label htmlFor="review-reviewer">Reviewer</label>
          <input id="review-reviewer" value={reviewer} maxLength={200} onChange={(e) => setReviewer(e.target.value)} disabled={locked} />
          <label htmlFor="review-reviewed-at">Reviewed at</label>
          <input
            id="review-reviewed-at"
            type="datetime-local"
            value={reviewedAt}
            onChange={(e) => setReviewedAt(e.target.value)}
            disabled={locked}
          />
          <label htmlFor="review-scope">What the review verified (components, sources, golden cases)</label>
          <textarea id="review-scope" value={scope} maxLength={2000} onChange={(e) => setScope(e.target.value)} disabled={locked} />
          <label htmlFor="review-professional">Professional review reference (customs broker or tax adviser; optional)</label>
          <input
            id="review-professional"
            value={professional}
            maxLength={200}
            onChange={(e) => setProfessional(e.target.value)}
            disabled={locked}
          />
          <label htmlFor="review-evidence">Evidence references (one per line; optional)</label>
          <textarea id="review-evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} disabled={locked} />
          <label htmlFor="review-notes">Notes (optional)</label>
          <textarea id="review-notes" value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} disabled={locked} />
          <label htmlFor="review-hash">Reviewed content hash (must be the hash above)</label>
          <input
            id="review-hash"
            value={reviewedHash}
            maxLength={64}
            onChange={(e) => setReviewedHash(e.target.value)}
            disabled={locked}
          />
          <p className="muted small" data-testid="approval-record-length">
            Approval record: about {recordLength} of {LIMITS.approvalReferenceMax} characters.
          </p>
        </fieldset>
      ) : null}
      {problems.length ? (
        <ul className="form-error" data-testid="action-problems">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}
      <label className="checkbox">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
          disabled={locked}
        />{' '}
        I have read the effect on production valuations above and confirm this owner action.
      </label>
      <div className="form-actions">
        <button type="submit" className={`button ${action === 'revoked' || action === 'active' ? 'danger' : ''}`} disabled={!ready}>
          {ACTION_LABELS[action]}
        </button>
        <button type="button" className="button secondary" onClick={onCancel} disabled={locked}>
          Cancel
        </button>
      </div>
    </form>
  )
}

// ----------------------------------------------------------------------------------- cost profile

function lineAmount(value: string | null): string {
  return value === null ? 'unknown' : groupDecimal(value)
}

function CostProfileCard({ view, timeZone, onApproved }: { view: CostProfilesView; timeZone: string; onApproved: () => void }) {
  const { client } = useWorkspace()
  const [confirmed, setConfirmed] = useState(false)
  const approve = useIdempotentMutation<CostProfileApproveRequest, CostProfileApproveResult>(
    'cost-profile-approve',
    (body) => client.approveCostProfile(body),
    {
      settled: () => {
        setConfirmed(false)
        onApproved()
      },
    },
  )
  const configured = view.configured
  const locked = busy(approve.phase)
  return (
    <div className="panel" data-testid="cost-profile-card">
      {view.configured_problem ? <Notice tone="bad">{view.configured_problem}</Notice> : null}
      {configured ? (
        <ProfileSummary profile={configured} file={view.configured_file} timeZone={timeZone} />
      ) : (
        <EmptyState>No configured cost profile.</EmptyState>
      )}
      <p className="muted small">{view.note}</p>
      {configured?.can_approve ? (
        <form
          className="form"
          data-testid="cost-profile-approve-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (!confirmed || locked) return
            void approve.submit({
              profile_key: configured.profile_key,
              version: configured.version,
              content_sha256: configured.content_sha256,
            })
          }}
        >
          <Notice tone="warn">
            <span data-testid="cost-profile-effect">
              Effect on production valuations: approving records your approval of these cost assumptions (transport{' '}
              {configured.transport ? `${configured.transport.currency} ${lineAmount(configured.transport.base)}` : 'unknown'} per
              vehicle). Production valuations that used the unapproved version are marked stale and recomputed with the approved
              assumptions; unknown lines stay unknown.
              {configured.approval_status === 'not_stored' ? ' This version is stored first, then approved.' : ''}
            </span>
          </Notice>
          <label className="checkbox">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} disabled={locked} />{' '}
            I have read the effect on production valuations above and approve cost profile {configured.profile_key} version{' '}
            {configured.version}.
          </label>
          <div className="form-actions">
            <button type="submit" className="button" disabled={!confirmed || locked}>
              Approve cost profile
            </button>
          </div>
        </form>
      ) : null}
      {approve.phase.kind !== 'idle' ? (
        <MutationStatus
          phase={approve.phase}
          subject="cost profile"
          onRetry={() => void approve.retry()}
          onDiscard={() => {
            approve.reset()
            onApproved()
          }}
          confirmed={(result) => (
            <span data-testid="cost-profile-result">
              Cost profile {result.profile_key} version {result.version} approved. {result.production_effect}
            </span>
          )}
        />
      ) : null}
      {view.profiles.length ? (
        <table className="responsive-table" aria-label="Stored cost profiles">
          <thead>
            <tr>
              <th scope="col">Stored version</th>
              <th scope="col">Approval</th>
              <th scope="col">Transport</th>
              <th scope="col">Content hash</th>
            </tr>
          </thead>
          <tbody>
            {view.profiles.map((profile) => (
              <tr key={profile.id ?? `${profile.profile_key}:${profile.version}`}>
                <td data-label="Stored version">
                  {profile.profile_key} v{profile.version}
                  {profile.is_fixture ? <span className="muted small"> (SYNTHETIC fixture)</span> : null}
                </td>
                <td data-label="Approval">
                  <Badge tone={profile.approval_status === 'approved' ? 'ok' : 'warn'}>{label(profile.approval_status)}</Badge>{' '}
                  {profile.approved_at ? <Timestamp value={profile.approved_at} timeZone={timeZone} /> : null}
                </td>
                <td data-label="Transport">
                  {profile.transport ? `${profile.transport.currency} ${lineAmount(profile.transport.base)}` : 'unknown'}
                </td>
                <td data-label="Content hash">
                  <code title={profile.content_sha256}>{shortHash(profile.content_sha256)}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">No cost-profile version is stored in this workspace yet.</p>
      )}
    </div>
  )
}

function ProfileSummary({ profile, file, timeZone }: { profile: CostProfileSummaryView; file: string; timeZone: string }) {
  const transport = profile.transport
  return (
    <>
      <p>
        Configured cost profile <strong>{profile.profile_key}</strong> version {profile.version}{' '}
        <span className="muted small">({file})</span>{' '}
        <Badge tone={profile.approval_status === 'approved' ? 'ok' : 'warn'} >
          {profile.approval_status === 'not_stored' ? 'not stored yet (unapproved)' : label(profile.approval_status)}
        </Badge>
        {profile.approved_at ? (
          <span className="small">
            {' '}
            on <Timestamp value={profile.approved_at} timeZone={timeZone} />
          </span>
        ) : null}
      </p>
      <p data-testid="cost-profile-transport">
        Transport:{' '}
        {transport ? (
          <>
            <strong>
              {transport.currency} {lineAmount(transport.base)}
            </strong>{' '}
            per vehicle ({label(transport.status)}; low {lineAmount(transport.low)}, high {lineAmount(transport.high)})
          </>
        ) : (
          'unknown'
        )}
      </p>
      {transport ? <p className="muted small">{transport.note}</p> : null}
      {profile.matches_stored === false ? (
        <Notice tone="bad">
          The stored copy of this version has different content: bump the version in the file before approving it.
        </Notice>
      ) : null}
      <details>
        <summary>All {profile.lines.length} cost lines</summary>
        <ul>
          {profile.lines.map((line) => (
            <li key={`${line.category}:${line.label}`}>
              {line.label} <span className="muted small">({label(line.status)}{line.scope ? `; only ${line.scope}` : ''})</span>
              {line.base !== null ? ` ${line.currency} ${lineAmount(line.base)}` : ''}
            </li>
          ))}
        </ul>
      </details>
      <p className="muted small">
        Content hash <code title={profile.content_sha256}>{shortHash(profile.content_sha256)}</code>
      </p>
    </>
  )
}
