/**
 * SYNTHETIC recorded import values (`GET` / `POST /api/listings/{listing_id}/import-values`) shaped
 * exactly like `views.import_values`: the owner's example of decision 2026-10-13 (bought for EUR
 * 2,800.50, SchwackeNet catalogue EUR 4,200). `amount` is in whole euros as the server sends it;
 * `amount_exact` is what was recorded (the browser never shows or rounds it).
 */
import type { ImportValueRecordView, ImportValuesRecordResult, ImportValuesView } from '../api/types'
import { LISTING_ID } from './fixtures'
import { RECORDED_CUSTOMS_ID, RECORDED_PURCHASE_ID } from './importCostFixtures'

export const OWNER_PRINCIPAL = '11111111-1111-4111-8111-111111111111'
export const EARLIER_PURCHASE_ID = 'abababab-0000-4000-8000-000000000003'
export const RECOMPUTE_JOB_ID = 'abababab-0000-4000-8000-000000000009'
export const STALE_VALUATION_ID = 'dddddddd-0000-4000-8000-000000000001'

export const IMPORT_VALUES_NOTE =
  'Two separate values per vehicle (owner decision 2026-10-13): the real purchase price is used for the profit and the landed cost; the estimated customs value (without transport) for the import charges. Customs first applies the price actually paid (Customs Law art. 28); the calculator shows that alternative too.'
export const RECORD_EFFECT =
  "Recorded: the listing's valuations are marked stale and recomputed with the recorded values; the calculator's listing mode uses them unless a request overrides them."

export function purchaseRecord(overrides: Partial<ImportValueRecordView> = {}): ImportValueRecordView {
  return {
    id: RECORDED_PURCHASE_ID,
    category: 'purchase',
    kind: 'actual',
    amount: { status: 'known', amount: '2801', currency: 'EUR', reason: null },
    amount_exact: '2800.50',
    source: 'seller invoice',
    document_ref: 'INV-SYNTHETIC-1',
    supersedes_id: EARLIER_PURCHASE_ID,
    current: true,
    recorded_by_principal: OWNER_PRINCIPAL,
    recorded_at: '2026-10-13T09:30:00Z',
    is_fixture: false,
    ...overrides,
  }
}

export function customsRecord(overrides: Partial<ImportValueRecordView> = {}): ImportValueRecordView {
  return {
    id: RECORDED_CUSTOMS_ID,
    category: 'customs_value',
    kind: 'estimate',
    amount: { status: 'known', amount: '4200', currency: 'EUR', reason: null },
    amount_exact: '4200.00',
    source: 'SchwackeNet catalogue',
    document_ref: null,
    supersedes_id: null,
    current: true,
    recorded_by_principal: OWNER_PRINCIPAL,
    recorded_at: '2026-10-13T09:31:00Z',
    is_fixture: false,
    ...overrides,
  }
}

/** The earlier purchase price the current one superseded (a quote of EUR 2,750). */
export function earlierPurchaseRecord(): ImportValueRecordView {
  return purchaseRecord({
    id: EARLIER_PURCHASE_ID,
    kind: 'quote',
    amount: { status: 'known', amount: '2750', currency: 'EUR', reason: null },
    amount_exact: '2750.00',
    source: 'agreed with the seller',
    document_ref: null,
    supersedes_id: null,
    current: false,
    recorded_at: '2026-10-12T15:00:00Z',
  })
}

export function importValuesView(overrides: Partial<ImportValuesView> = {}): ImportValuesView {
  return {
    listing_id: LISTING_ID,
    purchase_price: purchaseRecord(),
    estimated_customs_value: customsRecord(),
    history: [customsRecord(), purchaseRecord(), earlierPurchaseRecord()],
    note: IMPORT_VALUES_NOTE,
    ...overrides,
  }
}

export function noImportValues(): ImportValuesView {
  return importValuesView({ purchase_price: null, estimated_customs_value: null, history: [] })
}

export function recordResult(values: ImportValuesView = importValuesView()): ImportValuesRecordResult {
  return {
    values,
    recorded_ids: [RECORDED_PURCHASE_ID, RECORDED_CUSTOMS_ID],
    superseded_ids: [EARLIER_PURCHASE_ID],
    stale_valuation_ids: [STALE_VALUATION_ID],
    recompute_job_id: RECOMPUTE_JOB_ID,
    production_effect: RECORD_EFFECT,
  }
}
