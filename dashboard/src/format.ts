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

/**
 * A duration from whole seconds ("42 s", "5 min 10 s", "3 h 5 min", "2 days 4 h"). Display only;
 * `null`/negative/non-finite input is "unknown" (never zero).
 */
export function durationText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return 'unknown'
  const total = Math.floor(seconds)
  if (total < 60) return `${total} s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) {
    const rest = total % 60
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`
  }
  const hours = Math.floor(minutes / 60)
  if (hours < 48) {
    const rest = minutes % 60
    return rest ? `${hours} h ${rest} min` : `${hours} h`
  }
  const days = Math.floor(hours / 24)
  const rest = hours % 24
  return rest ? `${days} days ${rest} h` : `${days} days`
}

const RATIO = /^\+?(\d*)(?:\.(\d*))?$/

/**
 * A decimal ratio string ("0.8333") as a percentage ("83.3 %") by moving the decimal point in the
 * digit string (no float arithmetic). Extra digits are cut, so coverage is never overstated.
 */
export function ratioPercentText(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'not applicable'
  const match = RATIO.exec(value)
  if (!match || (!match[1] && !match[2])) return value
  const integer = match[1] ?? ''
  const fraction = match[2] ?? ''
  const shifted = `${integer}${fraction.padEnd(2, '0').slice(0, 2)}`.replace(/^0+(?=\d)/, '')
  const tenth = fraction.slice(2, 3) || '0'
  return `${shifted || '0'}.${tenth} %`
}

/** A byte count for attachment metadata ("820 B", "12.4 kB", "3.1 MB"); display only. */
export function bytesText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return 'unknown'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(Math.floor((value * 10) / 1024) / 10).toFixed(1)} kB`
  return `${(Math.floor((value * 10) / (1024 * 1024)) / 10).toFixed(1)} MB`
}

/** The first 12 hex digits of a hash or fingerprint, for display next to the full value's title. */
export function shortHash(value: string | null | undefined): string {
  if (!value) return 'none'
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

/** A count that is `null` when unknown: shown as "unknown", a real zero as "0". */
export function countText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'unknown'
  return String(value)
}

/**
 * Orders two RFC 3339 instants by time (for `sort`). The server omits a zero fraction
 * (`...:00Z` next to `...:00.25Z`), so comparing the strings would put a whole second AFTER its
 * own fractions; unparsable values sort last, in string order.
 */
export function compareInstants(a: string, b: string): number {
  const left = Date.parse(a)
  const right = Date.parse(b)
  if (Number.isNaN(left) || Number.isNaN(right)) {
    if (Number.isNaN(left) !== Number.isNaN(right)) return Number.isNaN(left) ? 1 : -1
    return a < b ? -1 : a > b ? 1 : 0
  }
  return left - right
}
