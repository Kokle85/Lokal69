/**
 * SYNTHETIC import-cost estimates (`GET /api/import-costs/estimate`) shaped exactly like
 * `views.import_costs.ImportCostEstimateView`. The amounts are the backend's own golden case G01 of
 * the shipped DRAFT rule set (fact sheet D1: diesel, NEDC 189 on the COC, EUR 3,000 invoice + the
 * fixed transport, no proof of origin, customs rate 61.5337), in whole units as the view presents
 * them (owner decision 2026-10-13); the browser only displays them.
 */
import type {
  AmountView,
  ImportCategoryView,
  ImportCostEstimateView,
  ImportScenarioView,
  TaxComponentView,
} from '../api/types'
import { LISTING_ID } from './fixtures'

export const BANNER_MK =
  'ИНДИКАТИВНА ПРОЦЕНКА – неодобрени нацрт-правила (mk-passenger-car-import@2026.10-draft.3). Не е царинска пресметка; не се користи за вреднувања ни известувања.'
export const BANNER_EN =
  'INDICATIVE PREVIEW – unapproved draft rules; not a customs assessment; never used for valuations or alerts.'
export const DRAFT_SHA = '4fe7f064f46ca8b91a90c5d799aa7c3c905973f6ea9a5dadb697490d91c83752'

const mkd = (amount: string): AmountView => ({ status: 'known', amount, currency: 'MKD', reason: null })
const eur = (amount: string): AmountView => ({ status: 'known', amount, currency: 'EUR', reason: null })
const unknownAmount = (reason: string, currency = 'MKD'): AmountView => ({ status: 'unknown', amount: null, currency, reason })
const notApplicable = (currency = 'MKD'): AmountView => ({
  status: 'not_applicable',
  amount: null,
  currency,
  reason: 'rule predicate not met',
})

function component(
  component_id: string,
  label: string,
  category: TaxComponentView['category'],
  amount: AmountView,
  intermediate = false,
): TaxComponentView {
  return {
    component_id,
    label,
    category,
    kind: 'percentage',
    status: amount.status === 'known' ? 'resolved' : amount.status === 'not_applicable' ? 'not_applicable' : 'unknown',
    intermediate,
    amount,
    inputs_used: amount.status === 'known' ? [{ name: 'rate', value: '1' }] : [],
    missing_inputs: amount.status === 'unknown' ? ['co2_g_km'] : [],
    warnings: [],
  }
}

const COMPONENTS: TaxComponentView[] = [
  component('customs_value_mkd', 'Customs value in MKD (intermediate)', 'import_duty', mkd('227675'), true),
  component('duty_preferential_eu', 'Customs duty 0% - accepted EU proof', 'import_duty', notApplicable()),
  component('duty_standard', 'Customs duty 5% - no accepted preferential proof (default)', 'import_duty', mkd('11384')),
  component('vmv_mkd', 'ВМВ: customs value + customs duty (intermediate, MKD)', 'motor_vehicle_tax', mkd('239059'), true),
  component('dmv_co2_diesel', 'ДМВ CO2 part, diesel column (intermediate)', 'motor_vehicle_tax', mkd('96390'), true),
  component('dmv_full', 'Motor-vehicle tax (ДМВ), full', 'motor_vehicle_tax', mkd('96390')),
  component('vat_base_mkd', 'Import VAT base (intermediate)', 'import_vat', mkd('335449'), true),
  component('vat_import', 'Import VAT 18%', 'import_vat', mkd('60381')),
]

function categoryRow(
  name: ImportCategoryView['category'],
  amount: AmountView,
  amountEur: AmountView,
  ids: string[],
): ImportCategoryView {
  const status = amount.status === 'known' ? 'resolved' : amount.status === 'not_applicable' ? 'not_applicable' : 'unknown'
  return { category: name, status, amount, amount_eur: amountEur, component_ids: ids }
}

export function previewScenario(overrides: Partial<ImportScenarioView> = {}): ImportScenarioView {
  return {
    name: 'primary',
    label: 'Primary: transaction value, invoice + transport to the MK place of entry (Царински закон чл. 28)',
    complete: true,
    production_ready: false,
    customs_value: eur('3700'),
    customs_value_eur: eur('3700'),
    customs_value_basis: 'Царински закон чл. 28: invoice 3000 EUR + чл. 35(1)(д) transport to MK place of entry 700 EUR',
    customs_value_kind: 'transaction_value',
    assumptions: [],
    components: COMPONENTS,
    categories: [
      categoryRow('import_duty', mkd('11384'), eur('185'), ['customs_value_mkd', 'duty_standard']),
      categoryRow('import_vat', mkd('60381'), eur('981'), ['vat_base_mkd', 'vat_import']),
      categoryRow('motor_vehicle_tax', mkd('96390'), eur('1566'), ['vmv_mkd', 'dmv_co2_diesel', 'dmv_full']),
      categoryRow('other_import_charges', notApplicable(), notApplicable('EUR'), []),
    ],
    total: null,
    total_eur: null,
    landed_cost_eur: null,
    indicative_total: mkd('168155'),
    indicative_total_eur: eur('2733'),
    indicative_landed_cost_eur: eur('6433'),
    known_subtotal: null,
    known_subtotal_label: 'known_subtotal',
    unknown_components: [],
    missing_inputs: [],
    warnings: ['RULE_SET_NOT_ACTIVE:draft: result is not production-ready'],
    effective_date: '2026-10-13',
    customs_fx_rate: {
      base: 'EUR',
      quote: 'MKD',
      rate: '61.5337',
      rate_date: '2026-10-01',
      provider: 'SYNTHETIC customs monthly list',
      purpose: 'customs',
      direction: '1 EUR = 61.5337 MKD',
    },
    engine_version: 'tax-engine/1.1.0',
    calculation_sha256: '13f16460eda7e265d43519b50eb6c6843d68ca02898b0e1e22945755ced0c9f5',
    ...overrides,
  }
}

/** The same vehicle without any CO2 figure: the motor-vehicle tax and VAT stay unknown (never zero). */
export function incompleteScenario(): ImportScenarioView {
  const unknownReason = 'the calculation is incomplete: dmv_co2_diesel, dmv_full, vat_base_mkd, vat_import'
  return previewScenario({
    complete: false,
    components: COMPONENTS.map((item) =>
      ['dmv_co2_diesel', 'dmv_full', 'vat_base_mkd', 'vat_import'].includes(item.component_id)
        ? component(item.component_id, item.label, item.category, unknownAmount('missing co2_g_km'), item.intermediate)
        : item,
    ),
    categories: [
      categoryRow('import_duty', mkd('11384'), eur('185'), ['customs_value_mkd', 'duty_standard']),
      categoryRow('import_vat', unknownAmount('a component of this category is unknown'), unknownAmount('unknown', 'EUR'), [
        'vat_base_mkd',
        'vat_import',
      ]),
      categoryRow(
        'motor_vehicle_tax',
        unknownAmount('a component of this category is unknown'),
        unknownAmount('unknown', 'EUR'),
        ['vmv_mkd', 'dmv_co2_diesel', 'dmv_full'],
      ),
      categoryRow('other_import_charges', notApplicable(), notApplicable('EUR'), []),
    ],
    indicative_total: unknownAmount(unknownReason),
    indicative_total_eur: unknownAmount(unknownReason, 'EUR'),
    indicative_landed_cost_eur: unknownAmount(unknownReason, 'EUR'),
    known_subtotal: mkd('11384'),
    unknown_components: ['dmv_co2_diesel', 'dmv_full', 'vat_base_mkd', 'vat_import'],
    missing_inputs: ['co2_g_km'],
  })
}

export function importEstimate(overrides: Partial<ImportCostEstimateView> = {}): ImportCostEstimateView {
  return {
    estimator_version: 'import-estimate/1.0.0',
    mode: 'indicative_preview',
    production_ready: false,
    usage_note:
      'Display only: an import-cost estimate is never stored as a valuation, a cost line or an alert. Not a customs assessment; Customs decides the customs value, origin and tariff code.',
    banner: { mk: BANNER_MK, en: BANNER_EN },
    rule: {
      rule_set_id: 'mk-passenger-car-import',
      version: '2026.10-draft.3',
      status: 'draft',
      jurisdiction: 'MK',
      currency: 'MKD',
      valid_from: '2025-07-01',
      valid_to: null,
      content_sha256: DRAFT_SHA,
      sha256_recorded: false,
    },
    selection_reason: 'INDICATIVE PREVIEW: selected mk-passenger-car-import@2026.10-draft.3 (draft)',
    considered: ['mk-passenger-car-import@2026.10-draft.3: not active for production (draft, fixture=False)'],
    jurisdiction: 'MK',
    vehicle_category: 'passenger_car',
    declaration_date: '2026-10-13',
    currency: 'MKD',
    customs_fx_rate: previewScenario().customs_fx_rate,
    customs_fx_source: 'recorded',
    eur_basis: 'EUR equivalents at the customs rate EUR/MKD 61.5337 (2026-10-01); not a payment or reference rate',
    landed_cost_note:
      'real purchase price + transport + import charges (customs duty, motor-vehicle tax, import VAT), in EUR at the customs rate, rounded to whole euros',
    listing: null,
    purchase_price: { amount: eur('3000'), basis: 'invoice', source: 'invoice or contract price' },
    customs_value_for_duties: { amount: eur('3700'), basis: 'transaction_value', source: 'invoice or contract price' },
    facts: {
      price: eur('3000.00'),
      price_basis: 'invoice',
      declaration_date: '2026-10-13',
      fuel: 'diesel',
      co2: [{ cycle: 'nedc', g_km: '189' }],
      co2_document: 'coc',
      co2_round_up_confirmed: false,
      first_registration: '2012-05',
      emissions_class: 'Euro 5',
      origin_proof_kind: 'none',
      origin_proof_country: null,
      origin_proof_evidence_ref: null,
      hs_code: null,
      hs_evidence_ref: null,
      transport_after_entry_eur: null,
      transport_after_entry_separately_invoiced: false,
      estimated_customs_value_eur: null,
      estimated_customs_value_source: null,
      importer_status: null,
      mileage_km: '180000',
      seller_country: 'DE',
      engine_displacement_cm3: null,
      power_kw: null,
    },
    fact_sources: [
      { fact: 'price', source: 'request' },
      { fact: 'fuel', source: 'request' },
      { fact: 'co2', source: 'request' },
      { fact: 'origin_proof', source: 'default' },
    ],
    scenarios: [previewScenario()],
    assumptions: [
      'TRANSPORT_LINE:700 EUR per vehicle (estimated: Transport to North Macedonia)',
      'NO_ORIGIN_PROOF_STANDARD_DUTY: no proof of origin, so the standard duty',
    ],
    warnings: [],
    notes: ['COST_PROFILE:default@v3 (unapproved)'],
    unverified_inputs: [],
    uncertain_items: [
      { source: 'rule_set', component_id: null, text: 'UNCERTAIN - confirm: СКД = customs monthly EUR/MKD rate, CO2 band edges and non-integer CO2.', applies: true },
      { source: 'component', component_id: 'dmv_plugin_hybrid_half', text: 'PHEV half rate (UNCERTAIN - confirm).', applies: false },
    ],
    gates: {
      importable: 'yes',
      reason: 'Euro 5 meets the Euro 4 minimum',
      euro_class: 5,
      min_euro: 4,
      legal_basis: 'UNCERTAIN - confirm: legal act and gazette number of the Euro minimum',
    },
    missing_inputs: [],
    missing_facts: [],
    ...overrides,
  }
}

/** The listing route's first estimate: facts from the listing (advertised price, listed CO2 claim). */
export function listingEstimate(): ImportCostEstimateView {
  const base = importEstimate()
  return {
    ...base,
    listing: {
      listing_id: LISTING_ID,
      revision_id: 'cccccccc-0000-4000-8000-000000000002',
      revision_number: 2,
      source_key: 'synthetic_source',
      is_fixture: false,
      fixture_label: null,
      screened: true,
    },
    facts: { ...base.facts, price_basis: 'advertised', co2_document: 'listing_claim_unverified' },
    fact_sources: [
      { fact: 'price', source: 'listing' },
      { fact: 'price_basis', source: 'listing' },
      { fact: 'fuel', source: 'listing' },
      { fact: 'co2', source: 'listing' },
      { fact: 'co2_document', source: 'listing' },
      { fact: 'first_registration', source: 'listing' },
      { fact: 'origin_proof', source: 'default' },
    ],
    unverified_inputs: ['price: advertised price, not an invoice or contract price'],
  }
}

// ------------------------------------------------------------------ two values per vehicle

/** The G01 vehicle's components for another customs value (whole denars, as the view sends them). */
function componentsFor(customsValueMkd: string, duty: string, vmv: string, vatBase: string, vat: string): TaxComponentView[] {
  return [
    component('customs_value_mkd', 'Customs value in MKD (intermediate)', 'import_duty', mkd(customsValueMkd), true),
    component('duty_preferential_eu', 'Customs duty 0% - accepted EU proof', 'import_duty', notApplicable()),
    component('duty_standard', 'Customs duty 5% - no accepted preferential proof (default)', 'import_duty', mkd(duty)),
    component('vmv_mkd', 'ВМВ: customs value + customs duty (intermediate, MKD)', 'motor_vehicle_tax', mkd(vmv), true),
    component('dmv_co2_diesel', 'ДМВ CO2 part, diesel column (intermediate)', 'motor_vehicle_tax', mkd('96390'), true),
    component('dmv_full', 'Motor-vehicle tax (ДМВ), full', 'motor_vehicle_tax', mkd('96390')),
    component('vat_base_mkd', 'Import VAT base (intermediate)', 'import_vat', mkd(vatBase), true),
    component('vat_import', 'Import VAT 18%', 'import_vat', mkd(vat)),
  ]
}

interface ScenarioFigures {
  customsValueEur: string
  customsValueMkd: string
  duty: [string, string]
  vat: [string, string]
  vmv: string
  vatBase: string
  total: [string, string]
  landed: string
}

function twoValueScenario(
  name: 'primary' | 'transaction_value',
  label: string,
  basis: string,
  figures: ScenarioFigures,
): ImportScenarioView {
  return previewScenario({
    name,
    label,
    customs_value: eur(figures.customsValueEur),
    customs_value_eur: eur(figures.customsValueEur),
    customs_value_basis: basis,
    customs_value_kind: name === 'primary' ? 'estimated' : 'transaction_value',
    components: componentsFor(figures.customsValueMkd, figures.duty[0], figures.vmv, figures.vatBase, figures.vat[0]),
    categories: [
      categoryRow('import_duty', mkd(figures.duty[0]), eur(figures.duty[1]), ['customs_value_mkd', 'duty_standard']),
      categoryRow('import_vat', mkd(figures.vat[0]), eur(figures.vat[1]), ['vat_base_mkd', 'vat_import']),
      categoryRow('motor_vehicle_tax', mkd('96390'), eur('1566'), ['vmv_mkd', 'dmv_co2_diesel', 'dmv_full']),
      categoryRow('other_import_charges', notApplicable(), notApplicable('EUR'), []),
    ],
    indicative_total: mkd(figures.total[0]),
    indicative_total_eur: eur(figures.total[1]),
    indicative_landed_cost_eur: eur(figures.landed),
    assumptions:
      name === 'primary'
        ? [
            'ESTIMATED_CUSTOMS_VALUE_TRANSPORT_ADDED: UNCERTAIN - confirm whether Customs adds the transport to the MK place of entry to an estimated (catalogue) customs value',
          ]
        : [],
  })
}

const PRIMARY_ON_ESTIMATE: ScenarioFigures = {
  customsValueEur: '4900',
  customsValueMkd: '301515',
  duty: ['15076', '245'],
  vat: ['74337', '1208'],
  vmv: '316591',
  vatBase: '412981',
  total: ['185803', '3020'],
  landed: '6520',
}

const PRIMARY_LABEL = 'Primary: estimated customs value (SchwackeNet catalogue) + transport to the MK place of entry'
const PRIMARY_BASIS =
  'ESTIMATED customs value (SchwackeNet catalogue) 4200 EUR + чл. 35(1)(д) transport to MK place of entry 700 EUR (UNCERTAIN whether Customs adds it)'
const TRANSACTION_LABEL = 'Alternative: Customs accepts the invoice as the transaction value (Царински закон чл. 28)'

/**
 * The owner's example (decision 2026-10-13), computed by the backend on the draft rules (unit test
 * `test_view_carries_the_estimated_customs_value_apart_from_the_real_price`): the G01 vehicle bought
 * for EUR 2,800 (invoice) while the SchwackeNet catalogue says EUR 4,200. The primary charges follow
 * the estimate, every landed cost the price paid.
 */
export function ownerExampleEstimate(): ImportCostEstimateView {
  const base = importEstimate()
  return {
    ...base,
    purchase_price: { amount: eur('2800'), basis: 'invoice', source: 'invoice or contract price' },
    customs_value_for_duties: { amount: eur('4900'), basis: 'estimated', source: 'SchwackeNet catalogue' },
    facts: {
      ...base.facts,
      price: eur('2800.00'),
      estimated_customs_value_eur: '4200',
      estimated_customs_value_source: 'SchwackeNet catalogue',
    },
    fact_sources: [
      { fact: 'price', source: 'request' },
      { fact: 'estimated_customs_value_eur', source: 'request' },
      { fact: 'estimated_customs_value_source', source: 'request' },
      { fact: 'fuel', source: 'request' },
      { fact: 'co2', source: 'request' },
      { fact: 'origin_proof', source: 'default' },
    ],
    scenarios: [
      twoValueScenario('primary', PRIMARY_LABEL, PRIMARY_BASIS, PRIMARY_ON_ESTIMATE),
      twoValueScenario(
        'transaction_value',
        TRANSACTION_LABEL,
        'Царински закон чл. 28: invoice 2800 EUR + чл. 35(1)(д) transport to MK place of entry 700 EUR',
        {
          customsValueEur: '3500',
          customsValueMkd: '215368',
          duty: ['10768', '175'],
          vat: ['58055', '943'],
          vmv: '226136',
          vatBase: '322526',
          total: ['165213', '2685'],
          landed: '6185',
        },
      ),
    ],
    warnings: [
      'TRANSACTION_VALUE_ALTERNATIVE: Customs first applies the transaction value, the price actually paid (Царински закон чл. 28)',
    ],
    unverified_inputs: ['customs_value: estimated customs value (SchwackeNet catalogue), not a Customs assessment or the price paid'],
  }
}

export const RECORDED_PURCHASE_ID = 'abababab-0000-4000-8000-000000000001'
export const RECORDED_CUSTOMS_ID = 'abababab-0000-4000-8000-000000000002'

/**
 * The listing route with the owner's RECORDED values (EUR 2,800.50 actually paid, SchwackeNet EUR
 * 4,200): the server shows the purchase price in whole euros (2801) while `facts` echo it as
 * recorded; the browser never rounds.
 */
export function recordedListingEstimate(): ImportCostEstimateView {
  const base = listingEstimate()
  const example = ownerExampleEstimate()
  return {
    ...example,
    listing: { ...base.listing!, recorded_purchase_price_id: RECORDED_PURCHASE_ID, recorded_customs_value_id: RECORDED_CUSTOMS_ID },
    purchase_price: {
      amount: eur('2801'),
      basis: 'recorded_actual',
      source: 'recorded actual purchase price EUR 2800.50 (seller invoice, 2026-10-13)',
    },
    customs_value_for_duties: {
      amount: eur('4900'),
      basis: 'estimated',
      source: 'recorded estimated customs value EUR 4200 (SchwackeNet catalogue, 2026-10-13)',
    },
    facts: {
      ...base.facts,
      price: eur('2800.50'),
      price_basis: 'recorded_actual',
      estimated_customs_value_eur: '4200.00',
      estimated_customs_value_source: 'SchwackeNet catalogue',
    },
    fact_sources: [
      { fact: 'price', source: 'recorded' },
      { fact: 'price_basis', source: 'recorded' },
      { fact: 'estimated_customs_value_eur', source: 'recorded' },
      { fact: 'estimated_customs_value_source', source: 'recorded' },
      { fact: 'fuel', source: 'listing' },
      { fact: 'co2', source: 'listing' },
      { fact: 'co2_document', source: 'listing' },
      { fact: 'origin_proof', source: 'default' },
    ],
    scenarios: [
      twoValueScenario('primary', PRIMARY_LABEL, PRIMARY_BASIS, PRIMARY_ON_ESTIMATE),
      twoValueScenario(
        'transaction_value',
        TRANSACTION_LABEL,
        'Царински закон чл. 28: invoice 2800.50 EUR + чл. 35(1)(д) transport to MK place of entry 700 EUR',
        {
          customsValueEur: '3501',
          customsValueMkd: '215399',
          duty: ['10770', '175'],
          vat: ['58061', '944'],
          vmv: '226169',
          vatBase: '322559',
          total: ['165221', '2685'],
          landed: '6186',
        },
      ),
    ],
    notes: [
      `RECORDED_PURCHASE_PRICE_USED:${RECORDED_PURCHASE_ID}: recorded actual purchase price EUR 2800.50 (seller invoice, 2026-10-13)`,
      `RECORDED_CUSTOMS_VALUE_USED:${RECORDED_CUSTOMS_ID}: recorded estimated customs value EUR 4200 (SchwackeNet catalogue, 2026-10-13)`,
    ],
  }
}
