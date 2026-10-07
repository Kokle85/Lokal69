import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'
import type { ApiClient, ApiResponse } from '../api/client'
import { ApiError, isApiError } from '../api/errors'
import type { ResponseEnvelope } from '../api/types'
import { useWorkspace } from '../workspace/WorkspaceProvider'

export interface QueryState<T> {
  status: 'loading' | 'success' | 'error'
  envelope: ResponseEnvelope<T> | null
  data: T | null
  error: ApiError | null
  /** Client time (ms) of the last successful load, for the stale-view warning. */
  fetchedAt: number | null
  reloading: boolean
  reload(): void
}

/**
 * Loads one API resource with loading/error states and cancellation. A reload keeps the previous
 * data on screen (marked as reloading); a change of `deps` (another resource) starts clean.
 */
export function useApiQuery<T>(
  load: (client: ApiClient, signal: AbortSignal) => Promise<ApiResponse<T>>,
  deps: DependencyList,
): QueryState<T> {
  const { client } = useWorkspace()
  const [nonce, setNonce] = useState(0)
  const [state, setState] = useState<Omit<QueryState<T>, 'reload' | 'data'>>({
    status: 'loading',
    envelope: null,
    error: null,
    fetchedAt: null,
    reloading: false,
  })
  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load
  })
  const depsKey = JSON.stringify(deps)
  const lastKey = useRef<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const sameResource = lastKey.current === depsKey
    lastKey.current = depsKey
    setState((previous) =>
      sameResource && previous.envelope
        ? { ...previous, reloading: true }
        : { status: 'loading', envelope: null, error: null, fetchedAt: null, reloading: false },
    )
    loadRef
      .current(client, controller.signal)
      .then(({ envelope }) => {
        if (controller.signal.aborted) return
        setState({ status: 'success', envelope, error: null, fetchedAt: Date.now(), reloading: false })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        const apiError = isApiError(error)
          ? error
          : new ApiError({ code: 'BAD_RESPONSE', message: String(error), status: null, retryable: true })
        setState((previous) => ({
          status: 'error',
          envelope: previous.envelope,
          error: apiError,
          fetchedAt: previous.fetchedAt,
          reloading: false,
        }))
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, depsKey, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { ...state, data: state.envelope?.data ?? null, reload }
}
