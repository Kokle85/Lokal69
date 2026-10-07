import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ApiClient } from '../api/client'
import { ApiError, isApiError } from '../api/errors'
import type { MeView, MembershipView, Role, Scope } from '../api/types'
import { useAuth } from '../auth/AuthProvider'
import { ErrorPanel, LoadingState } from '../components/ui'
import { ClaimStoreProvider } from '../review/claimStore'

export interface WorkspaceContextValue {
  client: ApiClient
  me: MeView
  workspaceId: string
  workspaceName: string
  timezone: string
  role: Role
  memberships: MembershipView[]
  can(scope: Scope): boolean
  switchWorkspace(): void
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

/** Per-viewer convenience only (not a token): the last chosen workspace per user. */
function preferenceKey(userId: string): string {
  return `suvdash:workspace:${userId}`
}

function readPreference(userId: string): string | null {
  try {
    return window.localStorage.getItem(preferenceKey(userId))
  } catch {
    return null
  }
}

function writePreference(userId: string, workspaceId: string | null): void {
  try {
    if (workspaceId) window.localStorage.setItem(preferenceKey(userId), workspaceId)
    else window.localStorage.removeItem(preferenceKey(userId))
  } catch {
    // storage unavailable (private window): the picker simply shows again next time
  }
}

/** Holds the workspace sent as X-Workspace-Id; read when a request is sent, set while resolving. */
class WorkspaceSelection {
  private workspaceId: string | null = null
  get(): string | null {
    return this.workspaceId
  }
  set(workspaceId: string | null): void {
    this.workspaceId = workspaceId
  }
}

type Phase =
  | { kind: 'loading' }
  | { kind: 'choose'; memberships: MembershipView[] }
  | { kind: 'ready'; me: MeView }
  | { kind: 'error'; error: ApiError }

/**
 * Bootstraps `GET /api/me`, resolves the workspace (picker when several memberships are active) and
 * provides the API client bound to it. Remounted per user (see `App`), so nothing leaks between
 * sessions.
 */
export function WorkspaceGate({ children, client: injected }: { children: ReactNode; client?: ApiClient }) {
  const { tokens, userId, signOut } = useAuth()
  // The selected workspace for X-Workspace-Id: a mutable holder read only when a request is sent.
  const [selection] = useState(() => new WorkspaceSelection())
  const client = useMemo(
    () => injected ?? new ApiClient({ tokens, getWorkspaceId: () => selection.get() }),
    [injected, tokens, selection],
  )
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' })
  // `chosen` is the workspace picked in this session (null: resolve from memberships); `nonce`
  // forces a reload (retry, switch).
  const [request, setRequest] = useState<{ chosen: string | null; nonce: number }>({ chosen: null, nonce: 0 })

  useEffect(() => {
    const controller = new AbortController()
    const signal = controller.signal
    const resolve = async () => {
      if (request.chosen) {
        selection.set(request.chosen)
        const { envelope } = await client.me({ signal })
        return { kind: 'ready', me: envelope.data } satisfies Phase
      }
      selection.set(null)
      const { envelope } = await client.me({ signal, withoutWorkspace: true })
      const me = envelope.data
      const active = me.memberships.filter((m) => m.active)
      const preferred = userId ? readPreference(userId) : null
      const target =
        active.length === 1
          ? (active[0]?.workspace_id ?? null)
          : (active.find((m) => m.workspace_id === preferred)?.workspace_id ?? null)
      if (!target) return { kind: 'choose', memberships: active } satisfies Phase
      selection.set(target)
      if (target === me.workspace.workspace_id) return { kind: 'ready', me } satisfies Phase
      const selected = await client.me({ signal })
      return { kind: 'ready', me: selected.envelope.data } satisfies Phase
    }
    resolve()
      .then((next: Phase) => {
        if (!signal.aborted) setPhase(next)
      })
      .catch((error: unknown) => {
        if (signal.aborted) return
        if (isApiError(error) && error.code === 'FORBIDDEN' && selection.get() && userId) {
          // A remembered workspace is no longer ours: forget it and ask again.
          writePreference(userId, null)
        }
        setPhase({
          kind: 'error',
          error: isApiError(error)
            ? error
            : new ApiError({ code: 'BAD_RESPONSE', message: String(error), status: null, retryable: true }),
        })
      })
    return () => controller.abort()
  }, [client, request, selection, userId])

  const choose = useCallback(
    (workspaceId: string) => {
      if (userId) writePreference(userId, workspaceId)
      setPhase({ kind: 'loading' })
      setRequest((previous) => ({ chosen: workspaceId, nonce: previous.nonce + 1 }))
    },
    [userId],
  )

  const retry = useCallback(() => {
    setPhase({ kind: 'loading' })
    setRequest((previous) => ({ ...previous, nonce: previous.nonce + 1 }))
  }, [])

  const switchWorkspace = useCallback(() => {
    if (userId) writePreference(userId, null)
    setPhase({ kind: 'loading' })
    setRequest((previous) => ({ chosen: null, nonce: previous.nonce + 1 }))
  }, [userId])

  const value = useMemo<WorkspaceContextValue | null>(() => {
    if (phase.kind !== 'ready') return null
    const me = phase.me
    const scopes = new Set(me.scopes)
    return {
      client,
      me,
      workspaceId: me.workspace.workspace_id,
      workspaceName: me.workspace.name,
      timezone: me.workspace.display_timezone || 'UTC',
      role: me.role,
      memberships: me.memberships,
      can: (scope: Scope) => scopes.has(scope),
      switchWorkspace,
    }
  }, [phase, client, switchWorkspace])

  if (phase.kind === 'loading') return <LoadingState label="Loading your workspace" />
  if (phase.kind === 'error') {
    const noMembership = phase.error.code === 'FORBIDDEN'
    return (
      <main className="centered-page" id="main" tabIndex={-1}>
        <h1>Workspace unavailable</h1>
        {noMembership ? (
          <p>This account has no active membership in a workspace (or the selected one is no longer available).</p>
        ) : null}
        <ErrorPanel error={phase.error} onRetry={retry} />
        <p>
          <button type="button" className="button secondary" onClick={() => void signOut()}>
            Sign out
          </button>
        </p>
      </main>
    )
  }
  if (phase.kind === 'choose') {
    return (
      <main className="centered-page" id="main" tabIndex={-1}>
        <h1>Choose a workspace</h1>
        <p>Your account is a member of several workspaces. Pick the one to review.</p>
        <ul className="choice-list" aria-label="Workspaces">
          {phase.memberships.map((membership) => (
            <li key={membership.workspace_id}>
              <button type="button" className="button choice" onClick={() => choose(membership.workspace_id)}>
                <span className="choice-title">{membership.workspace_name}</span>
                <span className="badge">{membership.role}</span>
              </button>
            </li>
          ))}
        </ul>
        <p>
          <button type="button" className="button secondary" onClick={() => void signOut()}>
            Sign out
          </button>
        </p>
      </main>
    )
  }
  return (
    <WorkspaceContext.Provider value={value}>
      <ClaimStoreProvider key={phase.me.workspace.workspace_id}>{children}</ClaimStoreProvider>
    </WorkspaceContext.Provider>
  )
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('useWorkspace must be used inside <WorkspaceGate>')
  return value
}
