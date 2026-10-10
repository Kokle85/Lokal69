/** A tiny fetch router for component tests: `"METHOD /path/:param"` -> handler. Records every call. */
import { vi, type Mock } from 'vitest'
import type { ResponseEnvelope, ResponseWarning } from '../api/types'

export interface RecordedCall {
  method: string
  path: string
  url: URL
  headers: Headers
  body: unknown
  rawBody: string | null
}

export type Handler = (call: RecordedCall, params: Record<string, string>) => Response | Promise<Response>

/** A promise plus its resolver, for holding a fake response until the test releases it. */
export function deferred(): { promise: Promise<void>; release: () => void } {
  const holder: { resolve?: () => void } = {}
  const promise = new Promise<void>((resolve) => {
    holder.resolve = resolve
  })
  return { promise, release: () => holder.resolve?.() }
}

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

let requestCounter = 0

export function envelope<T>(data: T, extra: { warnings?: ResponseWarning[]; next_cursor?: string | null } = {}): ResponseEnvelope<T> {
  requestCounter += 1
  return {
    schema_version: '1.0',
    request_id: `req-synthetic-${requestCounter}`,
    as_of: '2026-10-07T10:00:00Z',
    data,
    warnings: extra.warnings ?? [],
    next_cursor: extra.next_cursor ?? null,
  }
}

export function ok<T>(data: T, status = 200, extra: { warnings?: ResponseWarning[]; next_cursor?: string | null } = {}): Response {
  return json(status, envelope(data, extra))
}

export function apiError(
  status: number,
  code: string,
  message: string,
  extra: { details?: Record<string, unknown> | null; retryable?: boolean; correlation?: string } = {},
): Response {
  const correlation = extra.correlation ?? `req-synthetic-error-${status}`
  return json(status, {
    schema_version: '1.0',
    request_id: correlation,
    as_of: '2026-10-07T10:00:00Z',
    error: {
      code,
      message,
      retryable: extra.retryable ?? false,
      retry_after_seconds: null,
      correlation_id: correlation,
      details: extra.details ?? null,
    },
  })
}

function compile(pattern: string): { method: string; regex: RegExp; names: string[] } {
  const [method = 'GET', path = '/'] = pattern.split(' ')
  const names: string[] = []
  const source = path.replace(/:[a-z_]+/gi, (match) => {
    names.push(match.slice(1))
    return '([^/]+)'
  })
  return { method, regex: new RegExp(`^${source}$`), names }
}

export interface FakeApi {
  fetch: Mock<typeof fetch>
  calls: RecordedCall[]
  callsTo(pattern: string): RecordedCall[]
  /** Replace or add a route handler at runtime. */
  on(pattern: string, handler: Handler): void
}

export function fakeApi(routes: Record<string, Handler>): FakeApi {
  const table = new Map<string, Handler>(Object.entries(routes))
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn<typeof fetch>(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, 'http://dashboard.test')
    const method = (init?.method ?? 'GET').toUpperCase()
    const headers = new Headers(init?.headers)
    const rawBody = typeof init?.body === 'string' ? init.body : null
    const call: RecordedCall = {
      method,
      path: url.pathname,
      url,
      headers,
      rawBody,
      body: rawBody ? JSON.parse(rawBody) : undefined,
    }
    calls.push(call)
    for (const [pattern, handler] of table) {
      const compiled = compile(pattern)
      if (compiled.method !== method) continue
      const match = compiled.regex.exec(url.pathname)
      if (!match) continue
      const params: Record<string, string> = {}
      compiled.names.forEach((name, index) => {
        params[name] = decodeURIComponent(match[index + 1] ?? '')
      })
      return handler(call, params)
    }
    return apiError(404, 'NOT_FOUND', `no fake route for ${method} ${url.pathname}`)
  })
  return {
    fetch: fetchMock,
    calls,
    callsTo(pattern: string) {
      const compiled = compile(pattern)
      return calls.filter((call) => call.method === compiled.method && compiled.regex.test(call.path))
    },
    on(pattern: string, handler: Handler) {
      table.set(pattern, handler)
    },
  }
}
