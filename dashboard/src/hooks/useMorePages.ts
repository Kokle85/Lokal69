import { useCallback, useState } from 'react'
import type { ApiResponse } from '../api/client'
import { isApiError, type ApiError } from '../api/errors'

interface MorePages<T> {
  /** Request id of the first page these pages continue. */
  base: string
  items: T[]
  cursor: string | null
  error: ApiError | null
}

export interface MorePagesState<T> {
  /** Items of the pages loaded after the first one. */
  items: T[]
  /** The cursor of the next page (`null` on the last page). */
  cursor: string | null
  loading: boolean
  error: ApiError | null
  loadMore(): Promise<void>
}

/**
 * Keyset "load more" pages after a first page (same filters, opaque server cursor). Further pages
 * belong to the first page they continue (keyed by its request id), so a new first page (other
 * filters, reload) starts clean without an effect.
 */
export function useMorePages<T>(
  firstRequestId: string | null,
  firstNextCursor: string | null,
  loadPage: (cursor: string) => Promise<ApiResponse<{ items: T[] }>>,
): MorePagesState<T> {
  const [more, setMore] = useState<MorePages<T> | null>(null)
  const [loading, setLoading] = useState(false)
  const current = more && more.base === firstRequestId ? more : null
  const cursor = current ? current.cursor : firstNextCursor

  const loadMore = useCallback(async () => {
    if (!cursor || loading || !firstRequestId) return
    setLoading(true)
    try {
      const { envelope } = await loadPage(cursor)
      setMore({
        base: firstRequestId,
        items: [...(current?.items ?? []), ...envelope.data.items],
        cursor: envelope.next_cursor,
        error: null,
      })
    } catch (error) {
      if (isApiError(error)) setMore({ base: firstRequestId, items: current?.items ?? [], cursor, error })
    } finally {
      setLoading(false)
    }
  }, [cursor, loading, firstRequestId, loadPage, current])

  return { items: current?.items ?? [], cursor, loading, error: current?.error ?? null, loadMore }
}
