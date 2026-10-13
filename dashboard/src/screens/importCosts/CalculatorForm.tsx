/**
 * The customs calculator's vehicle-facts form. It only collects text: every value goes to the
 * server as typed (`GET /api/import-costs/estimate`), which validates it; fields a validation
 * error names are marked. On the listing route an empty field keeps the listing's fact (shown as
 * "From the listing: ..."), except a fact the server ties to one you enter: a CO2 figure you enter
 * never takes the listing's CO2 document, and a price you enter counts as advertised unless you
 * state its basis (`merge_facts`). Nothing is inferred here (no origin, no tariff code, no CO2
 * conversion).
 */
import { useState, type ReactNode } from 'react'
import { FIELD_LABELS, type FormState, type HintKey, type QueryKey } from './form'

type Option = readonly [value: string, text: string]

const CURRENCIES: Option[] = [
  ['', 'Not given'],
  ['EUR', 'EUR'],
  ['MKD', 'MKD'],
  ['CHF', 'CHF (customs value stays unknown)'],
]
const PRICE_BASES: Option[] = [
  ['', 'Not given (counts as advertised)'],
  ['advertised', 'Advertised price (a claim)'],
  ['invoice', 'Invoice / contract price'],
]
const FUELS: Option[] = [
  ['', 'Not given'],
  ['diesel', 'Diesel'],
  ['petrol', 'Petrol'],
  ['hybrid_petrol', 'Full hybrid (petrol)'],
  ['hybrid_diesel', 'Full hybrid (diesel)'],
  ['plugin_hybrid', 'Plug-in hybrid (needs the tariff code)'],
  ['electric', 'Electric'],
  ['lpg', 'LPG'],
  ['cng', 'CNG'],
  ['other', 'Other'],
  ['unknown', 'Unknown'],
]
const CO2_DOCUMENTS: Option[] = [
  ['', 'Not given'],
  ['coc', 'Certificate of conformity (COC)'],
  ['registration_consent', 'Registration consent'],
  ['manufacturer_certificate', 'Manufacturer certificate'],
  ['technical_assessment_certificate', 'Technical assessment certificate'],
  ['listing_claim_unverified', 'Listing claim only (unverified)'],
  ['none_available', 'No document exists (the deemed charge applies)'],
]
const ORIGIN_PROOFS: Option[] = [
  ['', 'Not given (no proof: standard duty)'],
  ['none', 'No proof held (standard duty)'],
  ['unknown', 'Unknown (duty stays unknown)'],
  ['eu', 'EU: EUR.1 or origin declaration'],
  ['efta', 'EFTA (state the country)'],
  ['tr', 'Turkey'],
  ['uk', 'United Kingdom'],
  ['cefta', 'CEFTA (state the country)'],
  ['ua', 'Ukraine'],
  ['us', 'US-origin evidence'],
]
const IMPORTERS: Option[] = [
  ['', 'Not given'],
  ['private', 'Private person'],
  ['non_vat_business', 'Business, not VAT-registered'],
  ['vat_dealer', 'VAT-registered dealer'],
]

const fieldId = (key: QueryKey) => `calc-${key.replace(/_/g, '-')}`

const CO2_FIGURE_KEYS: readonly QueryKey[] = [
  'co2_wltp_g_km',
  'co2_nedc_g_km',
  'co2_nedc_correlated_g_km',
  'co2_unknown_cycle_g_km',
]

/**
 * Facts the server derives from another fact you set, never from the listing (mirrors
 * `queries.import_costs.merge_facts`): with a CO2 figure (or the "without CO2" box) the CO2
 * document is only what you state, and a price you enter counts as advertised unless you state
 * its basis. For these fields "Keep the listing's" would be untrue.
 */
const COUPLED: Partial<Record<QueryKey, { given(form: FormState): boolean; empty: string; hint: string }>> = {
  co2_document: {
    given: (form) => CO2_FIGURE_KEYS.some((key) => form[key].trim() !== '') || form.co2_clear === 'true',
    empty: 'Not given (a CO2 figure you enter needs its own document)',
    hint: "The listing's CO2 document is not applied to a CO2 figure you enter.",
  },
  price_basis: {
    given: (form) => form.price_amount.trim() !== '' || form.price_currency !== '',
    empty: 'Not given (counts as advertised)',
    hint: "The listing's price basis is not applied to a price you enter.",
  },
}

export interface CalculatorFormProps {
  initial: FormState
  /** The listing route: empty fields keep the listing's facts. */
  listingMode: boolean
  /** What the listing established, per field (listing route only). */
  hints: Partial<Record<HintKey, string>>
  /** Fields the last server-side validation named. */
  invalid: ReadonlySet<string>
  busy: boolean
  onSubmit(form: FormState): void
}

export function CalculatorForm({ initial, listingMode, hints, invalid, busy, onSubmit }: CalculatorFormProps) {
  const [form, setForm] = useState<FormState>(initial)
  const set = (key: QueryKey, value: string) => setForm((previous) => ({ ...previous, [key]: value }))

  /** The listing's fact no longer applies because the user set the fact it is tied to. */
  const decoupled = (key: HintKey): boolean =>
    listingMode && key !== 'co2' && Boolean(COUPLED[key]?.given(form))

  const hint = (key: HintKey): ReactNode => {
    if (decoupled(key)) return <span className="field-hint">{COUPLED[key as QueryKey]?.hint}</span>
    return listingMode && hints[key] ? <span className="field-hint">From the listing: {hints[key]}</span> : null
  }

  const emptyLabel = (key: QueryKey, fallback: string): string => {
    if (decoupled(key)) return COUPLED[key]?.empty ?? fallback
    return listingMode && hints[key] ? `Keep the listing's (${hints[key]})` : fallback
  }

  const text = (key: QueryKey, options: { help?: string; inputMode?: 'decimal' | 'numeric' | 'text'; type?: 'text' | 'date' } = {}) => (
    <div className="field" key={key}>
      <label htmlFor={fieldId(key)}>{FIELD_LABELS[key]}</label>
      <input
        id={fieldId(key)}
        name={key}
        type={options.type ?? 'text'}
        inputMode={options.inputMode ?? 'text'}
        autoComplete="off"
        value={form[key]}
        placeholder={listingMode ? hints[key] : undefined}
        aria-invalid={invalid.has(key) || undefined}
        onChange={(event) => set(key, event.target.value)}
      />
      {hint(key)}
      {options.help ? <span className="field-hint">{options.help}</span> : null}
    </div>
  )

  const select = (key: QueryKey, choices: Option[], help?: string) => (
    <div className="field" key={key}>
      <label htmlFor={fieldId(key)}>{FIELD_LABELS[key]}</label>
      <select
        id={fieldId(key)}
        name={key}
        value={form[key]}
        aria-invalid={invalid.has(key) || undefined}
        onChange={(event) => set(key, event.target.value)}
      >
        {choices.map(([value, label]) => (
          <option key={value} value={value}>
            {value === '' ? emptyLabel(key, label) : label}
          </option>
        ))}
      </select>
      {hint(key)}
      {help ? <span className="field-hint">{help}</span> : null}
    </div>
  )

  const check = (key: QueryKey, help?: string) => (
    <div className="field" key={key}>
      <label className="checkbox" htmlFor={fieldId(key)}>
        <input
          id={fieldId(key)}
          name={key}
          type="checkbox"
          checked={form[key] === 'true'}
          aria-invalid={invalid.has(key) || undefined}
          onChange={(event) => set(key, event.target.checked ? 'true' : '')}
        />{' '}
        {FIELD_LABELS[key]}
      </label>
      {help ? <span className="field-hint">{help}</span> : null}
    </div>
  )

  return (
    <form
      className="form calc-form"
      aria-label="Vehicle facts"
      onSubmit={(event) => {
        event.preventDefault()
        if (!busy) onSubmit(form)
      }}
    >
      <p className="muted small">
        {listingMode
          ? "Every field you fill in replaces the listing's fact; an empty field keeps the listing's value (or stays unknown, never zero). A CO2 figure you enter does not take the listing's CO2 document."
          : 'Fill in what you know. An empty field stays unknown (never zero), and the result says what is missing.'}{' '}
        Decimals use a dot (e.g. 3000.00). Nothing you enter is stored.
      </p>
      <fieldset>
        <legend>Price</legend>
        <div className="calc-grid">
          {text('price_amount', { inputMode: 'decimal' })}
          {select('price_currency', CURRENCIES)}
          {select('price_basis', PRICE_BASES)}
        </div>
      </fieldset>
      <fieldset>
        <legend>Vehicle</legend>
        <div className="calc-grid">
          {select('fuel', FUELS)}
          {text('first_registration', { help: 'YYYY, YYYY-MM or YYYY-MM-DD' })}
          {text('emissions_class', { help: 'e.g. Euro 6 (import gate only)' })}
          {text('mileage_km', { inputMode: 'decimal' })}
          {text('engine_displacement_cm3', { inputMode: 'decimal' })}
          {text('power_kw', { inputMode: 'decimal' })}
          {text('seller_country', { help: 'ISO code, e.g. DE (never implies the origin)' })}
        </div>
      </fieldset>
      <fieldset>
        <legend>CO2 (exactly as printed on the document)</legend>
        {hint('co2')}
        <p className="field-hint">WLTP is used when given, otherwise NEDC; figures are never converted between cycles.</p>
        <div className="calc-grid">
          {text('co2_wltp_g_km', { inputMode: 'decimal' })}
          {text('co2_nedc_g_km', { inputMode: 'decimal' })}
          {text('co2_nedc_correlated_g_km', { inputMode: 'decimal' })}
          {text('co2_unknown_cycle_g_km', { inputMode: 'decimal' })}
          {select('co2_document', CO2_DOCUMENTS)}
        </div>
        {check('co2_round_up_confirmed', 'Otherwise a non-integer figure leaves the motor-vehicle tax unknown.')}
        {listingMode ? check('co2_clear', "Estimate without the listing's CO2 claim (takes no CO2 figure).") : null}
      </fieldset>
      <fieldset>
        <legend>Origin and tariff</legend>
        <div className="calc-grid">
          {select('origin_proof_kind', ORIGIN_PROOFS, 'Only a proof you HOLD; the seller country never implies origin.')}
          {text('origin_proof_country', { help: 'ISO code or EU; required for EFTA and CEFTA' })}
          {text('origin_proof_evidence_ref')}
          {text('hs_code', { help: 'From a broker or ruling; never inferred (HS 8703 only)' })}
          {text('hs_evidence_ref')}
        </div>
      </fieldset>
      <fieldset>
        <legend>Customs value, transport and rate</legend>
        <div className="calc-grid">
          {text('declaration_date', { type: 'date', help: 'Default: today' })}
          {text('customs_eur_mkd_rate', {
            inputMode: 'decimal',
            help: 'Customs monthly list rate; used only when none is recorded for the month, never stored',
          })}
          {text('catalogue_value_eur', { inputMode: 'decimal', help: 'Adds the customs-revaluation risk scenario' })}
          {text('transport_after_entry_eur', { inputMode: 'decimal', help: 'Part of the fixed transport after the MK border' })}
          {select('importer_status', IMPORTERS)}
        </div>
        {check(
          'transport_after_entry_separately_invoiced',
          'Only then the inland leg leaves the customs value (it still enters the VAT base).',
        )}
        {check('accept_previous_month_fx')}
      </fieldset>
      <div className="form-actions">
        <button type="submit" className="button" disabled={busy}>
          {busy ? 'Calculating…' : 'Calculate'}
        </button>
        <button type="button" className="button secondary" onClick={() => setForm(initial)} disabled={busy}>
          Undo changes
        </button>
      </div>
    </form>
  )
}
