/**
 * The dashboard's only HTTP client. It calls ONLY the routes of docs/api_contract.md.
 *
 * - Every request carries `Authorization: Bearer <Supabase access token>` obtained from the
 *   supabase-js session at call time (never cached here, never stored elsewhere). The Supabase
 *   publishable key is never sent to the backend.
 * - `X-Workspace-Id` is sent once a workspace is selected (validated server-side).
 * - A `401` triggers ONE session refresh and ONE retry of the identical request (same body, same
 *   idempotency key), so a token that expires mid-review recovers without a duplicate write.
 * - Mutations send the body's `idempotency_key` also as the `Idempotency-Key` header.
 * - Errors become `ApiError` with the server's code and correlation id; a mutation that fails at
 *   the network level or with a 5xx is marked `outcomeUnknown` (the server may have committed it).
 */
import { ApiError, parseErrorBody } from './errors'
import {
  LIMITS,
  type AddNoteRequest,
  type CandidateDetail,
  type CandidateListQuery,
  type CandidateListView,
  type ClaimRequest,
  type ClaimResult,
  type ComparableSetView,
  type ComparablesQuery,
  type MeView,
  type NoteView,
  type OutboxPage,
  type OutboxQuery,
  type OverviewView,
  type PauseSourceRequest,
  type RecheckRequest,
  type RecheckRequestResult,
  type ReleaseRequest,
  type ReleaseResultView,
  type ResponseEnvelope,
  type ReviewCaseView,
  type ReviewDecisionView,
  type ReviewQueuePage,
  type ReviewQueueQuery,
  type SettingsView,
  type SourceListView,
  type SourcePauseResult,
  type SubmitReviewRequest,
  type ValuationView,
} from './types'

/** Supplies the CURRENT access token; implemented over supabase-js (`auth/tokens.ts`). */
export interface TokenSource {
  /** The current (auto-refreshed) access token, or `null` when signed out. */
  getAccessToken(): Promise<string | null>
  /** Force a refresh (after a `401`); the new token, or `null` when the session is gone. */
  refreshAccessToken(): Promise<string | null>
  /** Called when the backend still refuses the session after a refresh. */
  onUnauthenticated(): void
}

export interface ApiClientOptions {
  tokens: TokenSource
  /** The selected workspace (`X-Workspace-Id`), or `null` while bootstrapping. */
  getWorkspaceId: () => string | null
  /** Same-origin by default: `/api/...` (Vite proxy in development, the same host in production). */
  baseUrl?: string
  fetchImpl?: typeof fetch
  /** Per-attempt request id sent as `X-Request-Id` (echoed by the server as the correlation id). */
  newRequestId?: () => string
}

export interface ApiResponse<T> {
  status: number
  envelope: ResponseEnvelope<T>
}

export interface RequestOptions {
  signal?: AbortSignal
  /** Send without `X-Workspace-Id` (only `GET /api/me` during bootstrap). */
  withoutWorkspace?: boolean
}

type Method = 'GET' | 'POST'
type QueryValue = string | number | boolean | undefined | null

export function randomId(prefix: string): string {
  const uuid =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${uuid}`
}

/** A fresh idempotency key for one logical mutation attempt (`<operation>:<uuid>`). */
export function newIdempotencyKey(operation: string): string {
  const key = `${operation}:${randomId('k')}`
  if (!LIMITS.idempotencyKeyPattern.test(key)) {
    throw new Error('generated idempotency key does not match the contract pattern')
  }
  return key
}

function pathId(value: string, name: string): string {
  if (!LIMITS.uuidPattern.test(value)) {
    throw new ApiError({
      code: 'VALIDATION_ERROR',
      message: `${name} must be a UUID`,
      status: null,
      retryable: false,
      details: { fields: [name] },
    })
  }
  return encodeURIComponent(value)
}

export function buildQuery(query: Record<string, QueryValue> | undefined): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, typeof value === 'boolean' ? (value ? 'true' : 'false') : String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

function isEnvelope(body: unknown): body is ResponseEnvelope<unknown> {
  if (typeof body !== 'object' || body === null) return false
  const candidate = body as Record<string, unknown>
  return (
    candidate.schema_version === '1.0' &&
    typeof candidate.request_id === 'string' &&
    typeof candidate.as_of === 'string' &&
    'data' in candidate &&
    Array.isArray(candidate.warnings)
  )
}

export class ApiClient {
  private readonly tokens: TokenSource
  private readonly getWorkspaceId: () => string | null
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private readonly newRequestId: () => string
  private refreshing: Promise<string | null> | null = null

  constructor(options: ApiClientOptions) {
    this.tokens = options.tokens
    this.getWorkspaceId = options.getWorkspaceId
    this.baseUrl = options.baseUrl ?? ''
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
    this.newRequestId = options.newRequestId ?? (() => randomId('dash'))
  }

  // ------------------------------------------------------------------ routes (contract section 6)

  async me(options: RequestOptions = {}) {
    return this.get<MeView>('/api/me', undefined, options)
  }
  async overview(options: RequestOptions = {}) {
    return this.get<OverviewView>('/api/overview', undefined, options)
  }
  async candidates(query: CandidateListQuery, options: RequestOptions = {}) {
    return this.get<CandidateListView>('/api/candidates', { ...query }, options)
  }
  async candidate(listingId: string, revision: number | null, options: RequestOptions = {}) {
    return this.get<CandidateDetail>(
      `/api/candidates/${pathId(listingId, 'listing_id')}`,
      { revision: revision ?? undefined },
      options,
    )
  }
  async comparables(setId: string, query: ComparablesQuery, options: RequestOptions = {}) {
    return this.get<ComparableSetView>(`/api/comparables/${pathId(setId, 'set_id')}`, { ...query }, options)
  }
  async valuation(valuationId: string, options: RequestOptions = {}) {
    return this.get<ValuationView>(`/api/valuations/${pathId(valuationId, 'valuation_id')}`, undefined, options)
  }
  async reviews(query: ReviewQueueQuery, options: RequestOptions = {}) {
    return this.get<ReviewQueuePage>('/api/reviews', { ...query }, options)
  }
  async reviewCase(caseId: string, options: RequestOptions = {}) {
    return this.get<ReviewCaseView>(`/api/reviews/${pathId(caseId, 'case_id')}`, undefined, options)
  }
  async claim(caseId: string, body: ClaimRequest, options: RequestOptions = {}) {
    return this.post<ClaimResult>(`/api/reviews/${pathId(caseId, 'case_id')}/claim`, body, options)
  }
  async release(caseId: string, body: ReleaseRequest, options: RequestOptions = {}) {
    return this.post<ReleaseResultView>(`/api/reviews/${pathId(caseId, 'case_id')}/release`, body, options)
  }
  async submit(caseId: string, body: SubmitReviewRequest, options: RequestOptions = {}) {
    return this.post<ReviewDecisionView>(`/api/reviews/${pathId(caseId, 'case_id')}/submit`, body, options)
  }
  async addNote(listingId: string, body: AddNoteRequest, options: RequestOptions = {}) {
    return this.post<NoteView>(`/api/listings/${pathId(listingId, 'listing_id')}/notes`, body, options)
  }
  async recheck(listingId: string, body: RecheckRequest, options: RequestOptions = {}) {
    return this.post<RecheckRequestResult>(`/api/listings/${pathId(listingId, 'listing_id')}/recheck`, body, options)
  }
  async sources(options: RequestOptions = {}) {
    return this.get<SourceListView>('/api/sources', undefined, options)
  }
  async pauseSource(sourceId: string, body: PauseSourceRequest, options: RequestOptions = {}) {
    return this.post<SourcePauseResult>(`/api/sources/${pathId(sourceId, 'source_id')}/pause`, body, options)
  }
  async settings(options: RequestOptions = {}) {
    return this.get<SettingsView>('/api/settings', undefined, options)
  }
  async outbox(query: OutboxQuery, options: RequestOptions = {}) {
    return this.get<OutboxPage>('/api/outbox', { ...query }, options)
  }

  // ------------------------------------------------------------------ transport

  get<T>(path: string, query: Record<string, QueryValue> | undefined, options: RequestOptions = {}) {
    return this.send<T>('GET', `${path}${buildQuery(query)}`, undefined, options)
  }

  post<T>(path: string, body: { idempotency_key: string }, options: RequestOptions = {}) {
    if (!LIMITS.idempotencyKeyPattern.test(body.idempotency_key)) {
      return Promise.reject(
        new ApiError({
          code: 'VALIDATION_ERROR',
          message: 'idempotency_key does not match the contract pattern',
          status: null,
          retryable: false,
          details: { fields: ['idempotency_key'] },
        }),
      )
    }
    return this.send<T>('POST', path, body, options)
  }

  private async refreshOnce(): Promise<string | null> {
    // Concurrent 401s share one refresh (supabase-js also deduplicates internally).
    this.refreshing ??= this.tokens.refreshAccessToken().finally(() => {
      this.refreshing = null
    })
    return this.refreshing
  }

  private async send<T>(
    method: Method,
    path: string,
    body: { idempotency_key: string } | undefined,
    options: RequestOptions,
  ): Promise<ApiResponse<T>> {
    let token = await this.tokens.getAccessToken()
    if (!token) {
      this.tokens.onUnauthenticated()
      throw new ApiError({ code: 'UNAUTHENTICATED', message: 'Not signed in', status: 401, retryable: false })
    }
    // The JSON text is serialized ONCE, so a retry after a refresh is byte-identical.
    const payload = body === undefined ? undefined : JSON.stringify(body)
    let refreshed = false
    for (;;) {
      const response = await this.attempt(method, path, payload, body?.idempotency_key, token, options)
      if (response.status === 401 && !refreshed) {
        refreshed = true
        const next = await this.refreshOnce()
        if (next) {
          token = next
          continue
        }
      }
      if (response.status === 401) this.tokens.onUnauthenticated()
      return this.parse<T>(response, method)
    }
  }

  private async attempt(
    method: Method,
    path: string,
    payload: string | undefined,
    idempotencyKey: string | undefined,
    token: string,
    options: RequestOptions,
  ): Promise<Response> {
    const headers = new Headers()
    headers.set('Accept', 'application/json')
    headers.set('Authorization', `Bearer ${token}`)
    headers.set('X-Request-Id', this.newRequestId())
    const workspace = options.withoutWorkspace ? null : this.getWorkspaceId()
    if (workspace) headers.set('X-Workspace-Id', workspace)
    if (payload !== undefined) {
      headers.set('Content-Type', 'application/json')
      if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey)
    }
    const init: RequestInit = {
      method,
      headers,
      body: payload,
      credentials: 'omit', // the API is bearer-only; never send cookies
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
    }
    if (options.signal) init.signal = options.signal
    try {
      return await this.fetchImpl(`${this.baseUrl}${path}`, init)
    } catch (error) {
      if (options.signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        throw new ApiError({ code: 'ABORTED', message: 'Request cancelled', status: null, retryable: true })
      }
      throw new ApiError({
        code: 'NETWORK_ERROR',
        message: 'The request could not be completed',
        status: null,
        retryable: true,
        outcomeUnknown: method === 'POST',
      })
    }
  }

  private async parse<T>(response: Response, method: Method): Promise<ApiResponse<T>> {
    let body: unknown = undefined
    try {
      const text = await response.text()
      body = text ? JSON.parse(text) : undefined
    } catch {
      body = undefined
    }
    const mutation = method === 'POST'
    if (response.ok) {
      if (isEnvelope(body)) return { status: response.status, envelope: body as ResponseEnvelope<T> }
      throw new ApiError({
        code: 'BAD_RESPONSE',
        message: 'The server returned an unexpected body',
        status: response.status,
        retryable: true,
        outcomeUnknown: mutation,
        correlationId: response.headers.get('X-Request-Id'),
      })
    }
    const payload = parseErrorBody(body)
    if (payload) {
      throw new ApiError({
        code: payload.code,
        message: payload.message,
        status: response.status,
        retryable: payload.retryable,
        retryAfterSeconds: payload.retry_after_seconds,
        correlationId: payload.correlation_id,
        details: payload.details,
        // A 5xx may have happened after a commit; a 4xx (including 429) was not applied.
        outcomeUnknown: mutation && response.status >= 500,
      })
    }
    throw new ApiError({
      code: 'BAD_RESPONSE',
      message: `Unexpected HTTP ${response.status}`,
      status: response.status,
      retryable: response.status >= 500 || response.status === 429,
      outcomeUnknown: mutation && response.status >= 500,
      correlationId: response.headers.get('X-Request-Id'),
    })
  }
}
