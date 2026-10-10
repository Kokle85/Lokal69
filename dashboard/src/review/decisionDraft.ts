/**
 * The decision form's draft and its mapping to `POST /api/reviews/{case_id}/submit`.
 *
 * Spec 19 dashboard actions ("needs inspection", "needs documents", "price confirmation needed")
 * are, per docs/api_contract.md, a `needs_information` decision whose `reason_codes` include the
 * action code and whose `missing_information` lists the open items. Those codes are never allowed
 * with another outcome (the server refuses that with 422; the form prevents it first).
 */
import { FORBIDDEN_TEXT_CHARS, LIMITS, type Checklist, type DashboardAction, type ReviewOutcome, type SubmitReviewRequest } from '../api/types'
import type { ClaimHandle } from './claimStore'

export const DASHBOARD_ACTIONS: Array<{ action: DashboardAction; label: string; summary: string }> = [
  {
    action: 'needs_inspection',
    label: 'Needs inspection',
    summary: 'Needs inspection: an independent physical inspection is required before any decision.',
  },
  {
    action: 'needs_documents',
    label: 'Needs documents',
    summary: 'Needs documents: the vehicle documents must be obtained and checked before any decision.',
  },
  {
    action: 'price_confirmation_needed',
    label: 'Price confirmation needed',
    summary: 'Price confirmation needed: the payable price and availability must be confirmed before any decision.',
  },
]

export const ACTION_CODES = new Set<string>(DASHBOARD_ACTIONS.map((item) => item.action))

export const OUTCOMES: Array<{ value: ReviewOutcome; label: string; hint: string }> = [
  { value: 'needs_information', label: 'Needs information', hint: 'List the open questions below.' },
  { value: 'watch', label: 'Watch', hint: 'Keep an eye on it; no action yet.' },
  { value: 'shortlisted', label: 'Shortlist', hint: 'Needs the current valuation and at least one evidence id.' },
  { value: 'rejected', label: 'Reject', hint: 'Not suitable.' },
]

export const SUGGESTED_REASON_CODES: string[] = [
  'price_in_band',
  'price_too_high',
  'mileage_acceptable',
  'mileage_too_high',
  'spec_mismatch',
  'condition_risk',
  'documents_incomplete',
  'seller_unresponsive',
  'awaiting_seller',
  'comparables_weak',
  'costs_unknown',
  'needs_inspection',
  'needs_documents',
  'price_confirmation_needed',
]

export interface DecisionDraft {
  outcome: ReviewOutcome | ''
  reasonCodes: string[]
  extraReasonCodes: string
  summary: string
  evidenceIds: string[]
  extraEvidenceIds: string
  missingInformation: string
  citeValuation: boolean
}

export const EMPTY_DRAFT: DecisionDraft = {
  outcome: '',
  reasonCodes: [],
  extraReasonCodes: '',
  summary: '',
  evidenceIds: [],
  extraEvidenceIds: '',
  missingInformation: '',
  citeValuation: true,
}

function splitList(text: string, separator: RegExp): string[] {
  return text
    .split(separator)
    .map((item) => item.trim())
    .filter(Boolean)
}

/** A draft pre-filled for one spec 19 dashboard action (open checklist items become questions). */
export function draftForAction(action: DashboardAction, checklist: Checklist | null, previous: DecisionDraft): DecisionDraft {
  const definition = DASHBOARD_ACTIONS.find((item) => item.action === action)
  const open = (checklist?.items ?? [])
    .filter((item) => item.actions.includes(action) || item.status === action)
    .map((item) => item.question.slice(0, LIMITS.missingInformationItemMax))
  const missing = open.length ? open : [definition?.summary ?? action]
  return {
    ...previous,
    outcome: 'needs_information',
    reasonCodes: [action],
    extraReasonCodes: '',
    summary: definition?.summary ?? previous.summary,
    missingInformation: missing.slice(0, LIMITS.missingInformationMax).join('\n'),
  }
}

export interface DraftCheck {
  problems: Array<{ field: keyof DecisionDraft; message: string }>
  body: Omit<SubmitReviewRequest, 'idempotency_key'> | null
}

/** Client-side mirror of the server rules (the server stays authoritative). */
export function buildSubmission(draft: DecisionDraft, handle: ClaimHandle | null): DraftCheck {
  const problems: DraftCheck['problems'] = []
  if (!draft.outcome) problems.push({ field: 'outcome', message: 'Choose an outcome.' })
  const codes = [...draft.reasonCodes, ...splitList(draft.extraReasonCodes, /[,\s]+/)]
  const uniqueCodes = [...new Set(codes)]
  if (uniqueCodes.length === 0) problems.push({ field: 'reasonCodes', message: 'Give at least one reason code.' })
  if (uniqueCodes.length > LIMITS.reasonCodesMax) {
    problems.push({ field: 'reasonCodes', message: `At most ${LIMITS.reasonCodesMax} reason codes.` })
  }
  const badCode = uniqueCodes.find((code) => !LIMITS.reasonCodePattern.test(code))
  if (badCode !== undefined) {
    problems.push({
      field: 'extraReasonCodes',
      message: 'Reason codes are short identifiers: letters, digits and _ . : - (max 80).',
    })
  }
  if (draft.outcome && draft.outcome !== 'needs_information' && uniqueCodes.some((code) => ACTION_CODES.has(code.toLowerCase().replace(/[-.:]/g, '_')))) {
    problems.push({
      field: 'reasonCodes',
      message: 'Inspection, document and price-confirmation actions are recorded as "needs information" decisions.',
    })
  }
  const summary = draft.summary.trim()
  if (summary.length < LIMITS.summaryMin) {
    problems.push({ field: 'summary', message: `The summary needs at least ${LIMITS.summaryMin} characters.` })
  }
  if (draft.summary.length > LIMITS.summaryMax) {
    problems.push({ field: 'summary', message: `The summary is limited to ${LIMITS.summaryMax} characters.` })
  }
  if (FORBIDDEN_TEXT_CHARS.test(draft.summary)) {
    problems.push({ field: 'summary', message: 'The summary contains control or bidirectional-override characters.' })
  }
  const evidence = [...new Set([...draft.evidenceIds, ...splitList(draft.extraEvidenceIds, /[,\s]+/)])]
  if (evidence.some((id) => !LIMITS.uuidPattern.test(id))) {
    problems.push({ field: 'extraEvidenceIds', message: 'Evidence ids must be UUIDs.' })
  }
  if (evidence.length > LIMITS.evidenceIdsMax) {
    problems.push({ field: 'extraEvidenceIds', message: `At most ${LIMITS.evidenceIdsMax} evidence ids.` })
  }
  const missing = splitList(draft.missingInformation, /\n+/)
  if (draft.outcome === 'needs_information' && missing.length === 0) {
    problems.push({ field: 'missingInformation', message: 'List the missing information (one item per line).' })
  }
  if (missing.length > LIMITS.missingInformationMax) {
    problems.push({ field: 'missingInformation', message: `At most ${LIMITS.missingInformationMax} items.` })
  }
  if (missing.some((item) => item.length > LIMITS.missingInformationItemMax || FORBIDDEN_TEXT_CHARS.test(item))) {
    problems.push({ field: 'missingInformation', message: `Each item is plain text of at most ${LIMITS.missingInformationItemMax} characters.` })
  }
  if (draft.outcome === 'shortlisted') {
    if (!handle?.valuationId || !draft.citeValuation) {
      problems.push({ field: 'citeValuation', message: 'A shortlist must cite the current valuation.' })
    }
    if (evidence.length === 0) {
      problems.push({ field: 'evidenceIds', message: 'A shortlist must cite at least one evidence id.' })
    }
  }
  if (!handle) problems.push({ field: 'outcome', message: 'Claim the case before submitting a decision.' })
  if (problems.length || !handle || !draft.outcome) return { problems, body: null }
  return {
    problems,
    body: {
      claim_token: handle.token,
      expected_version: handle.caseVersion,
      listing_revision: handle.listingRevision,
      valuation_id: draft.citeValuation ? handle.valuationId : null,
      outcome: draft.outcome,
      reason_codes: uniqueCodes,
      summary,
      evidence_ids: evidence.map((id) => id.toLowerCase()),
      missing_information: missing,
    },
  }
}
