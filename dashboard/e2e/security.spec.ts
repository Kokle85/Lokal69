/**
 * The mock Supabase Auth server must not make the REAL backend accept tokens Supabase would never
 * issue, and the dashboard must not leak tokens into URLs or web storage outside supabase-js'.
 * All requests go to the real backend (through the same-origin `/api` proxy of `vite preview`).
 */
import { generateKeyPairSync, sign, type KeyObject } from 'node:crypto'
import { expect, test, type APIRequestContext } from '@playwright/test'
import { guard, manifest, MOCK_AUTH, PUBLISHABLE_KEY, signIn } from './helpers.ts'

const b64url = (value: Buffer | string) => Buffer.from(value).toString('base64url')

function es256(claims: Record<string, unknown>, kid: string, key: KeyObject): string {
  const signingInput = `${b64url(JSON.stringify({ alg: 'ES256', typ: 'JWT', kid }))}.${b64url(JSON.stringify(claims))}`
  const signature = sign('sha256', Buffer.from(signingInput), { key, dsaEncoding: 'ieee-p1363' })
  return `${signingInput}.${b64url(signature)}`
}

async function passwordSession(request: APIRequestContext, email: string): Promise<{ access_token: string }> {
  const response = await request.post(`${MOCK_AUTH}/auth/v1/token?grant_type=password`, {
    headers: { apikey: PUBLISHABLE_KEY },
    data: { email, password: manifest().password },
  })
  expect(response.ok()).toBe(true)
  return (await response.json()) as { access_token: string }
}

async function apiMe(request: APIRequestContext, authorization: string | null, query = '') {
  return request.get(`/api/me${query}`, { headers: authorization ? { Authorization: authorization } : {} })
}

test.describe('token handling against the real backend', () => {
  test('the backend accepts a genuine mock token and refuses forged, tampered, unsigned and misplaced ones', async ({ request }) => {
    const data = manifest()
    const genuine = (await passwordSession(request, data.users.reviewer.email)).access_token
    const control = await apiMe(request, `Bearer ${genuine}`)
    expect(control.status()).toBe(200)

    const jwks = (await (await request.get(`${MOCK_AUTH}/auth/v1/.well-known/jwks.json`)).json()) as { keys: Array<{ kid: string }> }
    const kid = jwks.keys[0]!.kid
    const now = Math.floor(Date.now() / 1000)
    const claims = {
      iss: `${MOCK_AUTH}/auth/v1`,
      aud: 'authenticated',
      sub: data.users.owner.user_id,
      role: 'authenticated',
      iat: now,
      exp: now + 600,
      session_id: 'synthetic-forged-session',
    }
    const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' })
    const forged = es256(claims, kid, privateKey) // a P-256 key that is NOT the mock's, claiming its kid
    const [head, payload, signature] = genuine.split('.') as [string, string, string]
    const edited = { ...(JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as Record<string, unknown>), sub: data.users.owner.user_id }
    const tampered = `${head}.${b64url(JSON.stringify(edited))}.${signature}`
    const unsigned = `${b64url(JSON.stringify({ alg: 'none', typ: 'JWT' }))}.${b64url(JSON.stringify(claims))}.`

    for (const [label, authorization, query] of [
      ['forged signature with the mock kid', `Bearer ${forged}`, ''],
      ['payload edited after signing', `Bearer ${tampered}`, ''],
      ['unsigned (alg none)', `Bearer ${unsigned}`, ''],
      ['publishable key instead of a token', `Bearer ${PUBLISHABLE_KEY}`, ''],
      ['token in the query string', null, `?access_token=${genuine}`],
      ['no token', null, ''],
    ] as const) {
      const response = await apiMe(request, authorization, query)
      expect(response.status(), label).not.toBe(200)
      expect([401, 422], label).toContain(response.status())
      const body = (await response.json()) as { error: { code: string; message: string } }
      if (response.status() === 401) expect(body.error.code, label).toBe('UNAUTHENTICATED')
      expect(JSON.stringify(body), label).not.toContain(genuine)
    }
  })

  test('the dashboard keeps tokens out of URLs and out of web storage other than supabase-js own key', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    const urls: string[] = []
    page.on('request', (request) => urls.push(request.url()))
    page.on('framenavigated', (frame) => urls.push(frame.url()))
    await signIn(page, 'reviewer')
    for (const path of ['/candidates', `/candidates/${data.listings.alpha}`, `/reviews/${data.cases.alpha}`, '/sources', '/settings']) {
      await page.goto(path)
      await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
    }
    const storage = await page.evaluate(() => ({
      local: Object.fromEntries(Object.keys(window.localStorage).map((key) => [key, window.localStorage.getItem(key) ?? ''])),
      session: Object.fromEntries(Object.keys(window.sessionStorage).map((key) => [key, window.sessionStorage.getItem(key) ?? ''])),
    }))
    const session = Object.entries(storage.local).find(([key]) => key.startsWith('sb-') && key.endsWith('-auth-token'))
    expect(session, 'supabase-js session key').toBeDefined()
    const { access_token: access, refresh_token: refresh } = JSON.parse(session![1]) as { access_token: string; refresh_token: string }
    expect(access.split('.')).toHaveLength(3)
    // Only supabase-js keys and the per-user workspace preference live in localStorage.
    for (const key of Object.keys(storage.local)) expect(key).toMatch(/^(sb-|suvdash:workspace:)/)
    for (const [key, value] of [...Object.entries(storage.local), ...Object.entries(storage.session)]) {
      if (key.startsWith('sb-')) continue
      expect(value, key).not.toContain(access)
      expect(value, key).not.toContain(refresh)
    }
    for (const url of urls) {
      expect(url).not.toContain(access)
      expect(url).not.toContain(refresh)
      expect(url).not.toMatch(/access_token=|refresh_token=/)
    }
    expect(problems).toEqual([])
  })
})
