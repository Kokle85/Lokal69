/**
 * Presentation helpers. NO arithmetic on money: decimal strings from the backend are only
 * re-grouped for display (thousands separators are inserted into the digit string). The browser
 * never parses amounts into floats, sums them or derives tax/contribution figures.
 */
import type { AmountView, DateTimeString, PartialDate } from './api/types'

const DECIMAL = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?$/

/** "1234567.5" -> "1,234,567.5"; anything that is not a plain decimal string is returned as-is. */
export function groupDecimal(value: string): string {
  const match = DECIMAL.exec(value)
  if (!match) return value
  const [, sign = '', integer = '', fraction] = match
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${sign}${grouped}${fraction !== undefined ? `.${fraction}` : ''}`
}

/** Display text for an `AmountView`: unknown is the word "unknown", never "0.00". */
export function amountText(value: AmountView | null | undefined): string {
  if (!value) return 'unknown'
  if (value.status === 'known' && value.amount !== null) {
    return `${value.currency ?? ''} ${groupDecimal(value.amount)}`.trim()
  }
  if (value.status === 'not_applicable') return 'not applicable'
  return 'unknown'
}

/** A bare decimal string with its currency (cost-line bounds); `null` is shown as "unknown". */
export function decimalText(value: string | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === '') return 'unknown'
  return `${currency ? `${currency} ` : ''}${groupDecimal(value)}`
}

export function kmText(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'unknown'
  return `${groupDecimal(value)} km`
}

export function partialDateText(value: PartialDate | null | undefined): string {
  if (!value?.value) return 'unknown'
  return value.value
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatter(timeZone: string): Intl.DateTimeFormat {
  let cached = formatters.get(timeZone)
  if (!cached) {
    try {
      cached = new Intl.DateTimeFormat('en-GB', {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone,
      })
    } catch {
      cached = new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
    }
    formatters.set(timeZone, cached)
  }
  return cached
}

export function dateTimeText(value: DateTimeString | null | undefined, timeZone = 'UTC'): string {
  if (!value) return 'never'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return formatter(timeZone).format(date)
}

/** "5 min ago" style age for freshness hints (display only). */
export function ageText(value: DateTimeString | null | undefined, now: number = Date.now()): string {
  if (!value) return 'never'
  const time = new Date(value).getTime()
  if (Number.isNaN(time)) return 'unknown'
  const seconds = Math.round((now - time) / 1000)
  if (seconds < 0) return 'in the future'
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.round(hours / 24)} days ago`
}

/** `snake_case` machine labels to readable words ("needs_information" -> "needs information"). */
export function label(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'unknown'
  return value.replace(/_/g, ' ')
}

export function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return 'unknown'
  return value ? 'yes' : 'no'
}

/** Converts a `datetime-local` input value (viewer's local time) to RFC 3339 UTC. */
export function localInputToRfc3339(value: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

const pad = (n: number) => String(n).padStart(2, '0')

export function rfc3339ToLocalInput(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** Only absolute http(s) URLs without embedded credentials are ever rendered as links. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null
  if (!parsed.hostname || parsed.username || parsed.password) return null
  return parsed.href
}

/** Same-origin in-app path for a `?next=` redirect: must be a single-slash absolute path. */
export function safeNextPath(value: string | null | undefined): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) return '/'
  for (const char of value) {
    if (char.charCodeAt(0) < 0x20) return '/'
  }
  return value
}
