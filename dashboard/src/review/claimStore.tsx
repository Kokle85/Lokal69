/**
 * In-memory claim handles. A claim token is a short-lived capability returned ONCE by the server;
 * it is kept only in memory (never in web storage), scoped to the signed-in user and workspace
 * (the provider is remounted when either changes) and forgotten on sign-out or page reload. After a
 * reload the reviewer simply claims again, which rotates the token server-side.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import type { ClaimResult } from '../api/types'

export interface ClaimHandle {
  caseId: string
  token: string
  expiresAt: string
  caseVersion: number
  listingRevision: number
  revisionId: string
  valuationId: string | null
}

interface ClaimStore {
  get(caseId: string): ClaimHandle | null
  remember(result: ClaimResult & { claim_token: string }): ClaimHandle
  forget(caseId: string): void
}

const ClaimContext = createContext<ClaimStore | null>(null)

export function ClaimStoreProvider({ children }: { children: ReactNode }) {
  const [handles, setHandles] = useState<ReadonlyMap<string, ClaimHandle>>(() => new Map())

  const remember = useCallback((result: ClaimResult & { claim_token: string }) => {
    const handle: ClaimHandle = {
      caseId: result.case_id,
      token: result.claim_token,
      expiresAt: result.expires_at,
      caseVersion: result.case_version,
      listingRevision: result.listing_revision,
      revisionId: result.revision_id,
      valuationId: result.valuation_id,
    }
    setHandles((previous) => new Map(previous).set(result.case_id, handle))
    return handle
  }, [])
  const forget = useCallback((caseId: string) => {
    setHandles((previous) => {
      if (!previous.has(caseId)) return previous
      const next = new Map(previous)
      next.delete(caseId)
      return next
    })
  }, [])

  const store = useMemo<ClaimStore>(
    () => ({ get: (caseId: string) => handles.get(caseId) ?? null, remember, forget }),
    [handles, remember, forget],
  )
  return <ClaimContext.Provider value={store}>{children}</ClaimContext.Provider>
}

export function useClaimStore(): ClaimStore {
  const store = useContext(ClaimContext)
  if (!store) throw new Error('useClaimStore must be used inside <ClaimStoreProvider>')
  return store
}
