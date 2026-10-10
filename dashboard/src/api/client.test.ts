import { describe, expect, it, vi } from 'vitest'
import { apiError, fakeApi, json, ok } from '../test/fakeApi'
import { CASE_ID, CLAIM_TOKEN, me } from '../test/fixtures'
import { ApiClient, buildQuery, newIdempotencyKey, type TokenSource } from './client'
import { ApiError } from './errors'

const PUBLISHABLE_KEY = 'sb_publishable_SYNTHETIC_unit_test_key'

function tokens(overrides: Partial<TokenSource> = {}): TokenSource & { refreshes: number; unauthenticated: number } {
  let current = 'token-1'
  const source = {
    refreshes: 0,
    unauthenticated: 0,
    async getAccessToken() {
      return current
    },
    async refreshAccessToken() {
      source.refreshes += 1
      current = `token-${source.refreshes + 1}`
      return current
    },
    onUnauthenticated() {
      source.unauthenticated += 1
    },
    ...overrides,
  }
  return source
}

function submitBody(key = newIdempotencyKey('review-submit')) {
  return {
    claim_token: CLAIM_TOKEN,
    expected_version: 2,
    listing_revision: 2,
    valuation_id: null,
    outcome: 'watch' as const,
    reason_codes: ['price_in_band'],
    summary: 'SYNTHETIC: watch for a price drop.',
    evidence_ids: [],
    missing_information: [],
    idempotency_key: key,
  }
}

describe('ApiClient headers', () => {
  it('sends the bearer token, workspace and request id; never the publishable key or cookies', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()) })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => 'ws-1', fetchImpl: api.fetch })
    await client.me()
    const [call] = api.calls
    expect(call?.headers.get('Authorization')).toBe('Bearer token-1')
    expect(call?.headers.get('X-Workspace-Id')).toBe('ws-1')
    expect(call?.headers.get('X-Request-Id')).toMatch(/^dash-/)
    expect(call?.headers.get('apikey')).toBeNull()
    for (const [, value] of call!.headers) expect(value).not.toContain(PUBLISHABLE_KEY)
    const init = api.fetch.mock.calls[0]?.[1] as RequestInit
    expect(init.credentials).toBe('omit')
    expect(call?.path).toBe('/api/me')
  })

  it('omits X-Workspace-Id while bootstrapping /api/me', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()) })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => 'ws-1', fetchImpl: api.fetch })
    await client.me({ withoutWorkspace: true })
    expect(api.calls[0]?.headers.get('X-Workspace-Id')).toBeNull()
  })

  it('sends the idempotency key in the body and as Idempotency-Key, with JSON content type', async () => {
    const api = fakeApi({ 'POST /api/reviews/:id/submit': () => ok({}, 201) })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    const body = submitBody('review-submit:k-fixed-key-123')
    await client.submit(CASE_ID, body)
    const [call] = api.calls
    expect(call?.headers.get('Content-Type')).toBe('application/json')
    expect(call?.headers.get('Idempotency-Key')).toBe('review-submit:k-fixed-key-123')
    expect(call?.body).toEqual(body)
  })

  it('refuses a non-UUID path id without calling the network', async () => {
    const api = fakeApi({})
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.reviewCase('../../admin')).rejects.toMatchObject({ code: 'VALIDATION_ERROR' })
    expect(api.calls).toHaveLength(0)
  })

  it('does not call the API when signed out', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()) })
    const source = tokens({ getAccessToken: async () => null })
    const client = new ApiClient({ tokens: source, getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.me()).rejects.toMatchObject({ code: 'UNAUTHENTICATED' })
    expect(api.calls).toHaveLength(0)
    expect(source.unauthenticated).toBe(1)
  })

  it('builds query strings without empty values', () => {
    expect(buildQuery({ a: 'x', b: undefined, c: null, d: '', e: false, f: 25 })).toBe('?a=x&e=false&f=25')
    expect(buildQuery({})).toBe('')
  })

  it('generates contract-conforming, unique idempotency keys', () => {
    const a = newIdempotencyKey('review-submit')
    const b = newIdempotencyKey('review-submit')
    expect(a).toMatch(/^[A-Za-z0-9._:-]{8,128}$/)
    expect(a).not.toBe(b)
  })
})

describe('ApiClient token expiry', () => {
  it('refreshes once on 401 and retries the identical request with the same idempotency key', async () => {
    let attempts = 0
    const api = fakeApi({
      'POST /api/reviews/:id/submit': (call) => {
        attempts += 1
        return call.headers.get('Authorization') === 'Bearer token-2'
          ? ok({ ok: true }, 201)
          : apiError(401, 'UNAUTHENTICATED', 'The access token is invalid or expired')
      },
    })
    const source = tokens()
    const client = new ApiClient({ tokens: source, getWorkspaceId: () => null, fetchImpl: api.fetch })
    const body = submitBody()
    const result = await client.submit(CASE_ID, body)
    expect(result.status).toBe(201)
    expect(attempts).toBe(2)
    expect(source.refreshes).toBe(1)
    const [first, second] = api.calls
    expect(first?.rawBody).toBe(second?.rawBody)
    expect(first?.headers.get('Idempotency-Key')).toBe(second?.headers.get('Idempotency-Key'))
    expect(second?.headers.get('Authorization')).toBe('Bearer token-2')
  })

  it('signals sign-out when the refreshed session is still refused', async () => {
    const api = fakeApi({ 'GET /api/overview': () => apiError(401, 'UNAUTHENTICATED', 'expired') })
    const source = tokens()
    const client = new ApiClient({ tokens: source, getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.overview()).rejects.toMatchObject({ code: 'UNAUTHENTICATED', status: 401 })
    expect(api.calls).toHaveLength(2)
    expect(source.unauthenticated).toBe(1)
  })

  it('signals sign-out when no refreshed session exists', async () => {
    const api = fakeApi({ 'GET /api/overview': () => apiError(401, 'UNAUTHENTICATED', 'expired') })
    const source = tokens({ refreshAccessToken: async () => null })
    const client = new ApiClient({ tokens: source, getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.overview()).rejects.toBeInstanceOf(ApiError)
    expect(api.calls).toHaveLength(1)
    expect(source.unauthenticated).toBe(1)
  })

  it('shares one refresh between concurrent 401s', async () => {
    const api = fakeApi({
      'GET /api/overview': (call) =>
        call.headers.get('Authorization') === 'Bearer token-1' ? apiError(401, 'UNAUTHENTICATED', 'expired') : ok({}),
    })
    let refreshes = 0
    const source = tokens({
      refreshAccessToken: () => {
        refreshes += 1
        return new Promise((resolve) => setTimeout(() => resolve('token-2'), 5))
      },
    })
    const client = new ApiClient({ tokens: source, getWorkspaceId: () => null, fetchImpl: api.fetch })
    await Promise.all([client.overview(), client.overview(), client.overview()])
    expect(refreshes).toBe(1)
  })
})

describe('ApiClient error mapping', () => {
  it('maps the error envelope with code, correlation id and details', async () => {
    const api = fakeApi({
      'POST /api/reviews/:id/claim': () =>
        apiError(409, 'VERSION_CONFLICT', 'The object changed; reload and retry', {
          details: { current_version: 4 },
          correlation: 'req-corr-123',
        }),
    })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    const error = await client.claim(CASE_ID, { expected_version: 1, idempotency_key: newIdempotencyKey('claim') }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      code: 'VERSION_CONFLICT',
      status: 409,
      correlationId: 'req-corr-123',
      details: { current_version: 4 },
      outcomeUnknown: false,
      isConflict: true,
    })
  })

  it('marks a mutation whose response was lost as outcome-unknown, but not a read', async () => {
    const failing = vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: failing })
    await expect(client.submit(CASE_ID, submitBody())).rejects.toMatchObject({ code: 'NETWORK_ERROR', outcomeUnknown: true })
    await expect(client.overview()).rejects.toMatchObject({ code: 'NETWORK_ERROR', outcomeUnknown: false })
  })

  it('treats a 5xx on a mutation as outcome-unknown and a 429 as not applied', async () => {
    const api = fakeApi({
      'POST /api/reviews/:id/submit': () => apiError(500, 'INTERNAL_ERROR', 'Unexpected', { retryable: true }),
      'POST /api/reviews/:id/claim': () =>
        json(429, {
          schema_version: '1.0',
          request_id: 'req-429',
          as_of: '2026-10-07T10:00:00Z',
          error: { code: 'RATE_LIMITED', message: 'slow down', retryable: true, retry_after_seconds: 7, correlation_id: 'req-429', details: null },
        }),
    })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.submit(CASE_ID, submitBody())).rejects.toMatchObject({ code: 'INTERNAL_ERROR', outcomeUnknown: true })
    await expect(client.claim(CASE_ID, { expected_version: 1, idempotency_key: newIdempotencyKey('claim') })).rejects.toMatchObject({
      code: 'RATE_LIMITED',
      retryAfterSeconds: 7,
      outcomeUnknown: false,
    })
  })

  it('maps a non-JSON gateway error to BAD_RESPONSE', async () => {
    const api = fakeApi({ 'GET /api/overview': () => new Response('<html>Bad gateway</html>', { status: 502 }) })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.overview()).rejects.toMatchObject({ code: 'BAD_RESPONSE', status: 502, retryable: true })
  })

  it('refuses a 2xx body that is not an envelope', async () => {
    const api = fakeApi({ 'GET /api/overview': () => json(200, { hello: 'world' }) })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.overview()).rejects.toMatchObject({ code: 'BAD_RESPONSE' })
  })

  it('drops a malformed correlation id from an error body', async () => {
    const api = fakeApi({
      'GET /api/overview': () =>
        json(403, {
          schema_version: '1.0',
          request_id: 'x',
          as_of: '2026-10-07T10:00:00Z',
          error: { code: 'FORBIDDEN', message: 'no', retryable: false, retry_after_seconds: null, correlation_id: 'bad id with spaces', details: null },
        }),
    })
    const client = new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch })
    await expect(client.overview()).rejects.toMatchObject({ code: 'FORBIDDEN', correlationId: null })
  })
})

function v11Client(api: ReturnType<typeof fakeApi>) {
  return new ApiClient({ tokens: tokens(), getWorkspaceId: () => null, fetchImpl: api.fetch, newRequestId: () => 'req-test' })
}

describe('ApiClient v1.1 routes', () => {
  it('calls exactly the contract paths with their query parameters', async () => {
    const api = fakeApi({
      'GET /api/inquiries': () => ok({ items: [] }),
      'GET /api/inquiries/:id': () => ok({}),
      'GET /api/replies': () => ok({ items: [] }),
      'GET /api/replies/:id': () => ok({}),
      'GET /api/inquiry-control': () => ok({}),
      'GET /api/mail-workers/health': () => ok({}),
      'GET /api/mail-workers/coverage-gaps': () => ok({}),
      'GET /api/lifecycle/lags': () => ok({}),
      'GET /api/listings/:id/lifecycle': () => ok({}),
      'GET /api/evaluation': () => ok({}),
    })
    const c = v11Client(api)
    const id = '66666666-6666-4666-8666-666666666666'
    await c.inquiries({ attention_only: true, limit: 10 })
    await c.inquiry(id)
    await c.replies({ inquiry_id: id, quarantined_only: true })
    await c.reply(id)
    await c.inquiryControl()
    await c.mailWorkerHealth({ include_revoked: true })
    await c.mailCoverageGaps()
    await c.lifecycleLags()
    await c.listingLifecycle(id)
    await c.evaluation({ days: 15 })
    expect(api.calls.map((call) => `${call.method} ${call.path}${call.url.search}`)).toEqual([
      'GET /api/inquiries?attention_only=true&limit=10',
      `GET /api/inquiries/${id}`,
      `GET /api/replies?inquiry_id=${id}&quarantined_only=true`,
      `GET /api/replies/${id}`,
      'GET /api/inquiry-control',
      'GET /api/mail-workers/health?include_revoked=true',
      'GET /api/mail-workers/coverage-gaps',
      'GET /api/lifecycle/lags',
      `GET /api/listings/${id}/lifecycle`,
      'GET /api/evaluation?days=15',
    ])
  })

  it('sends pause and resume with the idempotency key in the body and header, and refuses bad ids locally', async () => {
    const api = fakeApi({
      'POST /api/inquiry-control/pause': () => ok({}),
      'POST /api/inquiry-control/resume': () => ok({}),
    })
    const c = v11Client(api)
    const key = newIdempotencyKey('inquiry-pause')
    await c.pauseInquiries({ expected_version: 3, reason: 'SYNTHETIC pause', idempotency_key: key })
    await c.resumeInquiries({ expected_version: 4, reason: 'SYNTHETIC resume', idempotency_key: newIdempotencyKey('inquiry-resume'), remove_suppressions: true })
    const [pause, resume] = api.calls
    expect(pause?.headers.get('Idempotency-Key')).toBe(key)
    expect(pause?.body).toEqual({ expected_version: 3, reason: 'SYNTHETIC pause', idempotency_key: key })
    expect(resume?.body).toMatchObject({ remove_suppressions: true })
    await expect(c.inquiry('../inquiry-control')).rejects.toMatchObject({ code: 'VALIDATION_ERROR' })
    await expect(c.replies({ inquiry_id: 'nope' })).rejects.toMatchObject({ code: 'VALIDATION_ERROR' })
    expect(api.calls).toHaveLength(2)
  })

  it('maps EMAIL_DELIVERY_UNCERTAIN to a typed error', async () => {
    const api = fakeApi({
      'POST /api/inquiry-control/pause': () =>
        apiError(409, 'EMAIL_DELIVERY_UNCERTAIN', 'reconcile first', { details: { reason: 'send_attempt_unresolved' } }),
    })
    const error = await v11Client(api)
      .pauseInquiries({ expected_version: 1, reason: 'SYNTHETIC', idempotency_key: newIdempotencyKey('inquiry-pause') })
      .catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('EMAIL_DELIVERY_UNCERTAIN')
    expect((error as ApiError).outcomeUnknown).toBe(false)
  })
})
