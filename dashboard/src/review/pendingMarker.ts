/**
 * A NON-SECRET marker that a review submission was sent but not yet confirmed. It survives a page
 * reload (sessionStorage, this tab only) so the reloaded page can tell the reviewer what happened,
 * WITHOUT resending anything. It never contains the claim token or any auth token; the claim token
 * is only in memory, so a reloaded page cannot resubmit on its own. A marker belongs to the user
 * who sent it: another user signed in later in the same tab never sees it.
 */
import type { ReviewCaseView, ReviewDecisionView, ReviewOutcome } from '../api/types'

export interface PendingSubmission {
  caseId: string
  /** The signed-in user (Supabase `sub`) who sent the submission. */
  userId: string
  idempotencyKey: string
  outcome: ReviewOutcome
  /** The case version the decision was submitted against. */
  expectedVersion: number
  startedAt: string
}

const PREFIX = 'suvdash:pending-submit:'
const MAX_AGE_MS = 30 * 60 * 1000

export function writePendingSubmission(marker: PendingSubmission): void {
  try {
    window.sessionStorage.setItem(PREFIX + marker.caseId, JSON.stringify(marker))
  } catch {
    // storage unavailable: the reloaded page just cannot explain the earlier attempt
  }
}

export function clearPendingSubmission(caseId: string): void {
  try {
    window.sessionStorage.removeItem(PREFIX + caseId)
  } catch {
    // ignore
  }
}

export function readPendingSubmission(caseId: string, userId: string | null): PendingSubmission | null {
  if (!userId) return null
  try {
    const raw = window.sessionStorage.getItem(PREFIX + caseId)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PendingSubmission>
    // Another user's marker is neither shown nor removed.
    if (typeof value.userId === 'string' && value.userId !== userId) return null
    if (
      value.caseId !== caseId ||
      value.userId !== userId ||
      typeof value.idempotencyKey !== 'string' ||
      typeof value.outcome !== 'string' ||
      typeof value.expectedVersion !== 'number' ||
      typeof value.startedAt !== 'string'
    ) {
      clearPendingSubmission(caseId)
      return null
    }
    if (Date.now() - new Date(value.startedAt).getTime() > MAX_AGE_MS) {
      clearPendingSubmission(caseId)
      return null
    }
    return value as PendingSubmission
  } catch {
    return null
  }
}

export type PendingResolution =
  | { kind: 'recorded'; decision: ReviewDecisionView }
  | { kind: 'not_recorded' }
  | { kind: 'decided_by_other'; decision: ReviewDecisionView }
  | { kind: 'superseded' }

/**
 * What the server state says about an earlier, unconfirmed submission. Only a decision that the
 * server attributes to THIS caller (`decided_by_caller`), made against exactly the submitted case
 * version with the submitted outcome, is that submission: a decision another reviewer recorded on
 * the same version is never mistaken for ours, even with the same outcome.
 */
export function resolvePendingSubmission(marker: PendingSubmission, current: ReviewCaseView): PendingResolution {
  const atVersion = current.decisions.filter((item) => item.case_version === marker.expectedVersion)
  const mine = atVersion.find((item) => item.decided_by_caller && item.outcome === marker.outcome)
  if (mine) return { kind: 'recorded', decision: mine }
  const other = atVersion.find((item) => !item.decided_by_caller)
  if (other) return { kind: 'decided_by_other', decision: other }
  if (current.case_version === marker.expectedVersion) return { kind: 'not_recorded' }
  return { kind: 'superseded' }
}
