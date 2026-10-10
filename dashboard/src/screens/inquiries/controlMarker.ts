/**
 * A NON-SECRET marker that an inquiry pause/resume was sent but not yet confirmed (the same pattern
 * as the review submission marker, `review/pendingMarker.ts`). It survives a page reload
 * (sessionStorage, this tab only) so the reloaded page can say what the server state shows,
 * WITHOUT resending anything. It holds no token: the workspace, the user (Supabase `sub`), the
 * action, the idempotency key, the expected control version and the start time. A marker belongs
 * to the user who sent it; another user signed in later in the same tab never sees it.
 */
import type { InquiryControlView } from '../../api/types'

export type ControlAction = 'pause' | 'resume'

export interface PendingControlAction {
  workspaceId: string
  userId: string
  action: ControlAction
  idempotencyKey: string
  expectedVersion: number
  startedAt: string
}

const PREFIX = 'suvdash:pending-control:'
const MAX_AGE_MS = 30 * 60 * 1000

export function writePendingControl(marker: PendingControlAction): void {
  try {
    window.sessionStorage.setItem(PREFIX + marker.workspaceId, JSON.stringify(marker))
  } catch {
    // storage unavailable: the reloaded page just cannot explain the earlier attempt
  }
}

export function clearPendingControl(workspaceId: string): void {
  try {
    window.sessionStorage.removeItem(PREFIX + workspaceId)
  } catch {
    // ignore
  }
}

export function readPendingControl(workspaceId: string, userId: string | null): PendingControlAction | null {
  if (!userId) return null
  try {
    const raw = window.sessionStorage.getItem(PREFIX + workspaceId)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PendingControlAction>
    if (typeof value.userId === 'string' && value.userId !== userId) return null
    if (
      value.workspaceId !== workspaceId ||
      value.userId !== userId ||
      (value.action !== 'pause' && value.action !== 'resume') ||
      typeof value.idempotencyKey !== 'string' ||
      typeof value.expectedVersion !== 'number' ||
      typeof value.startedAt !== 'string'
    ) {
      clearPendingControl(workspaceId)
      return null
    }
    if (Date.now() - new Date(value.startedAt).getTime() > MAX_AGE_MS) {
      clearPendingControl(workspaceId)
      return null
    }
    return value as PendingControlAction
  } catch {
    return null
  }
}

export type ControlResolution = 'applied' | 'not_applied' | 'changed_otherwise' | 'indeterminate'

/**
 * What the current controls say about an unconfirmed action sent against `expectedVersion`:
 * - `applied`: the controls are in the requested state (paused for a pause, unpaused for a resume)
 *   at that version or later - by this request or another one;
 * - `not_applied`: the controls are still at that version and not in the requested state;
 * - `changed_otherwise`: the controls moved on but are not in the requested state;
 * - `indeterminate`: a resume sent while the kill switch was already OFF (it only re-qualifies
 *   and removes suppressions and leaves the control version unchanged), and the controls are still
 *   at that version with the kill switch off: they look the same whether or not it was applied.
 *   (A resume of a PAUSED workspace bumps the version when applied, so it is never ambiguous.)
 */
export function resolvePendingControl(marker: PendingControlAction, current: InquiryControlView): ControlResolution {
  if (marker.action === 'resume' && current.version === marker.expectedVersion && !current.kill_switch) return 'indeterminate'
  const wanted = marker.action === 'pause'
  if (current.kill_switch === wanted && current.version >= marker.expectedVersion) return 'applied'
  if (current.version === marker.expectedVersion) return 'not_applied'
  return 'changed_otherwise'
}
