/**
 * The customs calculator's form state <-> the flat query parameters of
 * `GET /api/import-costs/estimate` (`ImportCostEstimateQuery`). The URL carries exactly these
 * parameters, so a calculation can be reloaded, shared and navigated back to.
 *
 * Nothing here interprets an amount: values are passed through as the strings the user typed and
 * the server validates them (failures name the parameters, which the form marks). An empty field
 * is "not given": on the listing route the listing's fact (or the owner's RECORDED import value)
 * then applies, otherwise it stays unknown.
 *
 * Two values per vehicle (owner decision 2026-10-13): `price_amount` is the REAL purchase price
 * (реална набавна цена: the landed cost and the profit use it) and `estimated_customs_value_eur`
 * (with its source) the ESTIMATED customs value (проценета царинска вредност: the import charges
 * of the primary scenario are computed on it plus the transport to the MK border).
 */
import type { AmountView, ImportCostEstimateQuery, ImportCostEstimateView, PurchasePriceBasis } from '../../api/types'
import { amountText, enteredAmountText, enteredDecimalText, label } from '../../format'

/** Every vehicle/request parameter the form can send (`listing_id` comes from the route). */
export const QUERY_KEYS = [
  'price_amount',
  'price_currency',
  'price_basis',
  'declaration_date',
  'fuel',
  'co2_wltp_g_km',
  'co2_nedc_g_km',
  'co2_nedc_correlated_g_km',
  'co2_unknown_cycle_g_km',
  'co2_clear',
  'co2_document',
  'co2_round_up_confirmed',
  'first_registration',
  'emissions_class',
  'origin_proof_kind',
  'origin_proof_country',
  'origin_proof_evidence_ref',
  'hs_code',
  'hs_evidence_ref',
  'transport_after_entry_eur',
  'transport_after_entry_separately_invoiced',
  'estimated_customs_value_eur',
  'estimated_customs_value_source',
  'importer_status',
  'mileage_km',
  'seller_country',
  'engine_displacement_cm3',
  'power_kw',
  'customs_eur_mkd_rate',
  'accept_previous_month_fx',
] as const

export type QueryKey = (typeof QUERY_KEYS)[number]

/** Checkbox parameters: sent only as `true` (an unticked box is "not given"). */
export const BOOLEAN_KEYS: ReadonlySet<QueryKey> = new Set<QueryKey>([
  'co2_clear',
  'co2_round_up_confirmed',
  'transport_after_entry_separately_invoiced',
  'accept_previous_month_fx',
])

/** The raw form: every parameter as the typed text (`''` = not given; checkboxes `'true'` or `''`). */
export type FormState = Record<QueryKey, string>

/** Human labels of the parameters (also used to name the fields a validation error points at). */
export const FIELD_LABELS: Record<QueryKey | 'listing_id', string> = {
  listing_id: 'Listing',
  price_amount: 'Purchase price (real, for profit)',
  price_currency: 'Price currency',
  price_basis: 'Price basis',
  declaration_date: 'Customs declaration date',
  fuel: 'Fuel / drive',
  co2_wltp_g_km: 'CO2 WLTP (g/km)',
  co2_nedc_g_km: 'CO2 NEDC (g/km)',
  co2_nedc_correlated_g_km: 'CO2 NEDC-correlated (g/km)',
  co2_unknown_cycle_g_km: 'CO2, cycle not stated (g/km)',
  co2_clear: 'Ignore the listing CO2 figure',
  co2_document: 'CO2 document',
  co2_round_up_confirmed: 'Round a non-integer CO2 figure up',
  first_registration: 'First registration',
  emissions_class: 'Euro class',
  origin_proof_kind: 'Proof of origin',
  origin_proof_country: 'Origin country on the proof',
  origin_proof_evidence_ref: 'Proof reference',
  hs_code: 'Tariff code (HS)',
  hs_evidence_ref: 'Tariff code evidence',
  transport_after_entry_eur: 'Inland transport after entry (EUR)',
  transport_after_entry_separately_invoiced: 'Inland leg separately invoiced',
  estimated_customs_value_eur: 'Estimated customs value (for duties)',
  estimated_customs_value_source: 'Source of the estimated customs value',
  importer_status: 'Importer',
  mileage_km: 'Mileage (km)',
  seller_country: 'Seller country',
  engine_displacement_cm3: 'Engine (cm3)',
  power_kw: 'Power (kW)',
  customs_eur_mkd_rate: 'Customs rate EUR/MKD',
  accept_previous_month_fx: "Accept last month's customs rate (preview only)",
}

/** The Macedonian labels shown next to the English ones of the two values (owner decision 2026-10-13). */
export const FIELD_LABELS_MK: Partial<Record<QueryKey, string>> = {
  price_amount: 'Реална набавна цена (за профит)',
  estimated_customs_value_eur: 'Проценета царинска вредност (за давачки)',
  estimated_customs_value_source: 'Извор на проценетата вредност',
}

export function emptyForm(): FormState {
  return Object.fromEntries(QUERY_KEYS.map((key) => [key, ''])) as FormState
}

/** The form as the URL describes it (unknown parameters are ignored). */
export function formFromSearch(search: URLSearchParams): FormState {
  const form = emptyForm()
  for (const key of QUERY_KEYS) {
    const value = search.get(key)
    if (value === null) continue
    form[key] = BOOLEAN_KEYS.has(key) ? (value === 'true' ? 'true' : '') : value
  }
  return form
}

/** The parameters the form GIVES (trimmed, non-empty; checkboxes only when ticked), in a fixed order. */
export function searchFromForm(form: FormState): URLSearchParams {
  const search = new URLSearchParams()
  for (const key of QUERY_KEYS) {
    const value = form[key].trim()
    if (!value) continue
    if (BOOLEAN_KEYS.has(key)) {
      if (value === 'true') search.set(key, 'true')
    } else {
      search.set(key, value)
    }
  }
  return search
}

/** The API query of a URL: the listing (from the route) plus the given parameters. */
export function queryFromSearch(search: URLSearchParams, listingId: string | undefined): ImportCostEstimateQuery {
  const query: Record<string, string | boolean> = {}
  for (const [key, value] of searchFromForm(formFromSearch(search))) {
    query[key] = BOOLEAN_KEYS.has(key as QueryKey) ? true : value
  }
  if (listingId) query.listing_id = listingId
  return query as ImportCostEstimateQuery
}

/** The form keys a server-side validation error names (flat parameter names, never values). */
export function invalidKeys(fields: string[]): Set<QueryKey | 'listing_id'> {
  const known = new Set<string>([...QUERY_KEYS, 'listing_id'])
  return new Set(fields.filter((field) => known.has(field)) as Array<QueryKey | 'listing_id'>)
}

/** Where a hint's value comes from: the listing itself or the owner's recorded import value. */
export type HintOrigin = 'listing' | 'recorded'

/** What an empty field keeps on the listing route, and where that value comes from. */
export interface Hint {
  text: string
  origin: HintOrigin
}

/** A form parameter, or `co2` for the CO2 figures as a group. */
export type HintKey = QueryKey | 'co2'

/** How a price basis reads in a hint ("recorded: actual (paid)"). */
export const PRICE_BASIS_TEXT: Record<PurchasePriceBasis, string> = {
  invoice: 'invoice / contract price',
  advertised: 'advertised price (a claim)',
  recorded_actual: 'recorded: actual (paid)',
  recorded_quote: 'recorded: quote (not paid yet)',
  recorded_estimate: 'recorded: estimate',
}

function isKnown(value: AmountView): boolean {
  return value.status === 'known' && value.amount !== null
}

/**
 * The purchase price an empty price field keeps: the server's whole-euro purchase price when the
 * price is in EUR (no rounding in the browser), otherwise the price as given (an MKD price).
 */
function priceText(view: ImportCostEstimateView): string | null {
  const price = view.facts.price
  if (!isKnown(price)) return null
  if (price.currency === 'EUR' && isKnown(view.purchase_price.amount)) return amountText(view.purchase_price.amount)
  return enteredAmountText(price)
}

/**
 * What the LISTING established, or the owner RECORDED for it, per form parameter (shown next to
 * the empty fields of the listing route: leaving a field empty keeps exactly this value). Only facts
 * whose source is the listing or a recorded import value.
 */
export function listingHints(view: ImportCostEstimateView | null): Partial<Record<HintKey, Hint>> {
  if (!view) return {}
  const origins = new Map<string, HintOrigin>()
  for (const { fact, source } of view.fact_sources) {
    if (source === 'listing' || source === 'recorded') origins.set(fact, source)
  }
  const facts = view.facts
  const hints: Partial<Record<HintKey, Hint>> = {}
  const set = (fact: string, key: HintKey, value: string | null | undefined) => {
    const origin = origins.get(fact)
    if (origin && value) hints[key] = { text: value, origin }
  }
  set('price', 'price_amount', priceText(view))
  set('price_basis', 'price_basis', PRICE_BASIS_TEXT[facts.price_basis] ?? label(facts.price_basis))
  const estimate = facts.estimated_customs_value_eur
  set('estimated_customs_value_eur', 'estimated_customs_value_eur', estimate ? `EUR ${enteredDecimalText(estimate)}` : null)
  set('estimated_customs_value_source', 'estimated_customs_value_source', facts.estimated_customs_value_source)
  set('fuel', 'fuel', label(facts.fuel))
  const readings = facts.co2.map((reading) => `${reading.g_km} g/km (${label(reading.cycle)})`).join(', ')
  set('co2', 'co2', readings)
  set('co2_document', 'co2_document', facts.co2_document ? label(facts.co2_document) : null)
  set('first_registration', 'first_registration', facts.first_registration)
  set('emissions_class', 'emissions_class', facts.emissions_class)
  set('mileage_km', 'mileage_km', facts.mileage_km)
  set('seller_country', 'seller_country', facts.seller_country)
  set('engine_displacement_cm3', 'engine_displacement_cm3', facts.engine_displacement_cm3)
  set('power_kw', 'power_kw', facts.power_kw)
  return hints
}
