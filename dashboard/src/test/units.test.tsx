import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Amount, ExternalLink, STALE_AFTER_MS, ViewMeta } from '../components/ui'
import { validateConfig } from '../config'
import { amountText, decimalText, groupDecimal, kmText, localInputToRfc3339, safeHttpUrl, safeNextPath } from '../format'
import { buildSubmission, draftForAction, EMPTY_DRAFT } from '../review/decisionDraft'
import { readPendingSubmission, resolvePendingSubmission, writePendingSubmission } from '../review/pendingMarker'
import { candidateDetail, CASE_ID, CLAIM_TOKEN, decision, EVIDENCE_ID, reviewCase } from './fixtures'

describe('money formatting', () => {
  it('never turns unknown into zero', () => {
    expect(amountText({ status: 'unknown', amount: null, currency: 'EUR', reason: 'no quote' })).toBe('unknown')
    expect(amountText(null)).toBe('unknown')
    expect(amountText({ status: 'not_applicable', amount: null, currency: 'EUR', reason: 'n/a' })).toBe('not applicable')
    expect(decimalText(null, 'EUR')).toBe('unknown')
    expect(kmText(null)).toBe('unknown')
  })

  it('only regroups digits (no float arithmetic)', () => {
    expect(groupDecimal('1234567.89')).toBe('1,234,567.89')
    expect(groupDecimal('-1500.5')).toBe('-1,500.5')
    expect(groupDecimal('0.10')).toBe('0.10')
    expect(groupDecimal('12345678901234567890.123456789')).toBe('12,345,678,901,234,567,890.123456789')
    expect(groupDecimal('not a number')).toBe('not a number')
    expect(amountText({ status: 'known', amount: '2750.00', currency: 'EUR', reason: null })).toBe('EUR 2,750.00')
  })

  it('renders an unknown amount with its reason and an explicit status', () => {
    render(<Amount value={{ status: 'unknown', amount: null, currency: 'EUR', reason: 'no transport quote' }} showReason />)
    const node = screen.getByText('unknown')
    expect(node).toHaveAttribute('data-amount-status', 'unknown')
    expect(node).toHaveTextContent('unknown (no transport quote)')
    expect(node.textContent).not.toMatch(/0\.00/)
  })
})

describe('stale views', () => {
  it('warns when a view was loaded long ago and offers a reload', () => {
    let reloads = 0
    const { rerender } = render(
      <ViewMeta asOf="2026-10-07T10:00:00Z" fetchedAt={Date.now()} timeZone="Europe/Skopje" onReload={() => (reloads += 1)} reloading={false} />,
    )
    expect(screen.queryByText(/may be stale/)).toBeNull()
    rerender(
      <ViewMeta
        asOf="2026-10-07T10:00:00Z"
        fetchedAt={Date.now() - STALE_AFTER_MS - 1_000}
        timeZone="Europe/Skopje"
        onReload={() => (reloads += 1)}
        reloading={false}
      />,
    )
    expect(screen.getByText(/may be stale. Reload before acting on it/)).toBeInTheDocument()
    screen.getByRole('button', { name: 'Reload' }).click()
    expect(reloads).toBe(1)
  })
})

describe('links and redirects', () => {
  it('accepts only absolute http(s) links without credentials', () => {
    expect(safeHttpUrl('https://synthetic-dealer.example/a?b=1')).toBe('https://synthetic-dealer.example/a?b=1')
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl(' JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(safeHttpUrl('https://user:pass@synthetic-dealer.example/')).toBeNull()
    expect(safeHttpUrl('/relative')).toBeNull()
    expect(safeHttpUrl(null)).toBeNull()
  })

  it('renders external links with noopener noreferrer and withholds unsafe ones', () => {
    const { container } = render(
      <>
        <ExternalLink href="https://synthetic-dealer.example/1">safe</ExternalLink>
        <ExternalLink href="javascript:alert(1)">unsafe</ExternalLink>
      </>,
    )
    const anchors = container.querySelectorAll('a')
    expect(anchors).toHaveLength(1)
    expect(anchors[0]).toHaveAttribute('rel', 'noopener noreferrer')
    expect(anchors[0]).toHaveAttribute('target', '_blank')
    expect(screen.getByText(/link withheld/)).toBeInTheDocument()
  })

  it('keeps ?next= same-origin', () => {
    expect(safeNextPath('/reviews/x?y=1')).toBe('/reviews/x?y=1')
    expect(safeNextPath('//evil.example')).toBe('/')
    expect(safeNextPath('/\\evil.example')).toBe('/')
    expect(safeNextPath('https://evil.example')).toBe('/')
    expect(safeNextPath(null)).toBe('/')
  })

  it('converts datetime-local input to RFC 3339 UTC', () => {
    expect(localInputToRfc3339('')).toBeNull()
    expect(localInputToRfc3339('2026-10-06T10:00')).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
  })
})

describe('configuration', () => {
  it('accepts a publishable key and a project URL', () => {
    const result = validateConfig({ VITE_SUPABASE_URL: 'https://project.supabase.co/', VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_x' })
    expect(result).toEqual({ ok: true, config: { supabaseUrl: 'https://project.supabase.co', publishableKey: 'sb_publishable_x' } })
  })

  it('refuses secret keys, plain http outside loopback and missing values', () => {
    const serviceRoleJwt = `x.${btoa(JSON.stringify({ role: 'service_role' }))}.y`
    expect(validateConfig({ VITE_SUPABASE_URL: 'https://p.supabase.co', VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_secret_abc' }).ok).toBe(false)
    expect(validateConfig({ VITE_SUPABASE_URL: 'https://p.supabase.co', VITE_SUPABASE_PUBLISHABLE_KEY: serviceRoleJwt }).ok).toBe(false)
    expect(validateConfig({ VITE_SUPABASE_URL: 'http://p.supabase.co', VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_x' }).ok).toBe(false)
    expect(validateConfig({ VITE_SUPABASE_URL: 'http://127.0.0.1:54399', VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_x' }).ok).toBe(true)
    expect(validateConfig({}).ok).toBe(false)
  })
})

describe('decision draft', () => {
  const handle = {
    caseId: CASE_ID,
    token: CLAIM_TOKEN,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    caseVersion: 2,
    listingRevision: 2,
    revisionId: 'r',
    valuationId: null,
  }

  it('maps a dashboard action to a needs_information decision with open items', () => {
    const draft = draftForAction('needs_documents', candidateDetail().due_diligence, EMPTY_DRAFT)
    const { body, problems } = buildSubmission(draft, handle)
    expect(problems).toEqual([])
    expect(body).toMatchObject({
      outcome: 'needs_information',
      reason_codes: ['needs_documents'],
      missing_information: ['Are registration documents and CoC available?'],
      expected_version: 2,
      listing_revision: 2,
      claim_token: CLAIM_TOKEN,
    })
  })

  it('refuses action codes with another outcome and a shortlist without valuation/evidence', () => {
    const rejected = buildSubmission({ ...EMPTY_DRAFT, outcome: 'rejected', reasonCodes: ['needs_inspection'], summary: 'SYNTHETIC long enough' }, handle)
    expect(rejected.body).toBeNull()
    expect(rejected.problems.map((p) => p.field)).toContain('reasonCodes')
    const shortlist = buildSubmission({ ...EMPTY_DRAFT, outcome: 'shortlisted', reasonCodes: ['price_in_band'], summary: 'SYNTHETIC long enough' }, handle)
    expect(shortlist.problems.map((p) => p.field)).toEqual(expect.arrayContaining(['citeValuation', 'evidenceIds']))
    const ok = buildSubmission(
      { ...EMPTY_DRAFT, outcome: 'shortlisted', reasonCodes: ['price_in_band'], summary: 'SYNTHETIC long enough', evidenceIds: [EVIDENCE_ID] },
      { ...handle, valuationId: 'dddddddd-0000-4000-8000-000000000001' },
    )
    expect(ok.problems).toEqual([])
    expect(ok.body?.valuation_id).toBe('dddddddd-0000-4000-8000-000000000001')
  })

  it('needs a claim, valid codes, UUID evidence and plain text', () => {
    expect(buildSubmission({ ...EMPTY_DRAFT, outcome: 'watch', reasonCodes: ['a'], summary: 'SYNTHETIC long enough' }, null).body).toBeNull()
    const bad = buildSubmission(
      { ...EMPTY_DRAFT, outcome: 'watch', extraReasonCodes: '<script>', summary: 'bad‮text here', extraEvidenceIds: 'not-a-uuid' },
      handle,
    )
    expect(bad.problems.map((p) => p.field)).toEqual(expect.arrayContaining(['extraReasonCodes', 'summary', 'extraEvidenceIds']))
  })
})

describe('pending submission marker', () => {
  it('stores no token and resolves against the server state', () => {
    writePendingSubmission({ caseId: CASE_ID, idempotencyKey: 'review-submit:k-1', outcome: 'watch', expectedVersion: 2, startedAt: new Date().toISOString() })
    const raw = sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`) ?? ''
    expect(raw).not.toContain(CLAIM_TOKEN)
    expect(raw).not.toMatch(/access|refresh|Bearer/i)
    const marker = readPendingSubmission(CASE_ID)!
    expect(resolvePendingSubmission(marker, reviewCase({ case_version: 2 })).kind).toBe('not_recorded')
    expect(resolvePendingSubmission(marker, reviewCase({ case_version: 3, decisions: [decision({ case_version: 2 })] })).kind).toBe('recorded')
    expect(resolvePendingSubmission(marker, reviewCase({ case_version: 5, decisions: [] })).kind).toBe('superseded')
  })

  it('drops malformed or old markers', () => {
    sessionStorage.setItem(`suvdash:pending-submit:${CASE_ID}`, '{"caseId":"other"}')
    expect(readPendingSubmission(CASE_ID)).toBeNull()
    writePendingSubmission({ caseId: CASE_ID, idempotencyKey: 'k-12345678', outcome: 'watch', expectedVersion: 2, startedAt: '2020-01-01T00:00:00Z' })
    expect(readPendingSubmission(CASE_ID)).toBeNull()
  })
})

describe('static security checks', () => {
  const root = join(process.cwd(), 'src')
  const files: string[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) walk(path)
      else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) && !path.includes(join('src', 'test'))) files.push(path)
    }
  }
  walk(root)
  const sinks = [
    ['dangerously', 'SetInnerHTML'],
    ['.inner', 'HTML ='],
    ['.inner', 'HTML='],
    ['insertAdjacent', 'HTML'],
    ['document.', 'write('],
    ['eval', '('],
    ['new Func', 'tion('],
  ].map((parts) => parts.join(''))

  it('has no raw-HTML or dynamic-code sinks anywhere in the source', () => {
    expect(files.length).toBeGreaterThan(20)
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      for (const sink of sinks) expect(text.includes(sink), `${file} contains ${sink}`).toBe(false)
    }
  })

  it('references only the two allowed browser variables', () => {
    for (const file of files) {
      for (const match of readFileSync(file, 'utf8').matchAll(/import\.meta\.env\.(VITE_[A-Z0-9_]+)/g)) {
        expect(['VITE_SUPABASE_URL', 'VITE_SUPABASE_PUBLISHABLE_KEY']).toContain(match[1])
      }
    }
  })

  it('a production build (when present) has a CSP and no inline scripts', () => {
    const index = join(process.cwd(), 'dist', 'index.html')
    if (!existsSync(index)) return
    const html = readFileSync(index, 'utf8')
    expect(html).toContain('http-equiv="Content-Security-Policy"')
    for (const tag of html.matchAll(/<script\b[^>]*>/gi)) expect(tag[0]).toMatch(/\bsrc=/)
    expect(html).not.toMatch(/\son[a-z]+\s*=/i)
  })
})
