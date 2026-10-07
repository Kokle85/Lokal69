/**
 * Typed dashboard BFF contract (docs/api_contract.md, section 6).
 *
 * Written by hand from the contract and the serialization schemas of the backend views
 * (src/suv_deals/views/*, src/suv_deals/api/schemas.py; snapshots in schemas/). This is the ONLY
 * module that declares API shapes; screens import from here.
 *
 * Conventions mirrored from the contract:
 * - money and financial decimals are strings ("2750.00"), never numbers; the browser never does
 *   arithmetic on them (all tax/contribution maths stays on the backend);
 * - unknown money is `{status: "unknown", amount: null}`, never zero;
 * - every timestamp is an RFC 3339 UTC string;
 * - views are closed objects whose declared keys are always present.
 */

// --------------------------------------------------------------------------- scalars

/** Canonical UUID string (8-4-4-4-12). */
export type Uuid = string
/** RFC 3339 date-time (UTC, `Z`). */
export type DateTimeString = string
/** Finite decimal as a string, e.g. "2750.00" or "-120.5". */
export type DecimalString = string
/** ISO 4217 code, e.g. "EUR". */
export type CurrencyCode = string
/** ISO 3166-1 alpha-2, e.g. "DE". */
export type CountryCode = string

// --------------------------------------------------------------------------- enums

export type Role = 'owner' | 'reviewer' | 'viewer'
export type Scope =
  | 'deals:read'
  | 'reviews:read'
  | 'reviews:write'
  | 'events:subscribe'
  | 'rechecks:request'
  | 'notes:write'
  | 'sources:pause'
  | 'config:admin'
  | 'inquiries:read'
  | 'inquiries:pause'
  | 'mail:ingest'

export type ErrorCode =
  | 'VALIDATION_ERROR'
  | 'UNAUTHENTICATED'
  | 'FORBIDDEN'
  | 'NOT_FOUND'
  | 'VERSION_CONFLICT'
  | 'ALREADY_CLAIMED'
  | 'CLAIM_EXPIRED'
  | 'IDEMPOTENCY_CONFLICT'
  | 'SOURCE_PAUSED'
  | 'ACCESS_BLOCKED'
  | 'RATE_LIMITED'
  | 'INSUFFICIENT_DATA'
  | 'DEPENDENCY_UNAVAILABLE'
  | 'INTERNAL_ERROR'

export const ERROR_CODES: readonly ErrorCode[] = [
  'VALIDATION_ERROR',
  'UNAUTHENTICATED',
  'FORBIDDEN',
  'NOT_FOUND',
  'VERSION_CONFLICT',
  'ALREADY_CLAIMED',
  'CLAIM_EXPIRED',
  'IDEMPOTENCY_CONFLICT',
  'SOURCE_PAUSED',
  'ACCESS_BLOCKED',
  'RATE_LIMITED',
  'INSUFFICIENT_DATA',
  'DEPENDENCY_UNAVAILABLE',
  'INTERNAL_ERROR',
]

export type WarningCode =
  | 'STALE_DATA'
  | 'SOURCE_PAUSED'
  | 'SOURCE_BLOCKED'
  | 'COVERAGE_GAP'
  | 'FIXTURE_DATA'
  | 'RESEARCH_CANDIDATE'
  | 'VALUATION_INCOMPLETE'
  | 'VALUATION_STALE'
  | 'UNKNOWN_COSTS'
  | 'INSUFFICIENT_COMPARABLES'
  | 'SMALL_COMPARABLE_SAMPLE'
  | 'ASKING_NOT_SALE'
  | 'THRESHOLD_PROPOSED'
  | 'TAX_RULES_UNAPPROVED'
  | 'FX_STALE'
  | 'SELLER_CLAIMS_UNVERIFIED'
  | 'SCORE_NOT_PROBABILITY'
  | 'FROZEN_QUEUE_PROJECTION'
  | 'REVISION_NOT_CURRENT'
  | 'NEW_REVISION_AVAILABLE'
  | 'CLAIM_EXPIRING'
  | 'ACTIVATION_BLOCKED'
  | 'PARTIAL_RESULTS'
  | 'DEPENDENCY_DEGRADED'

export type ProfileKey = 'primary' | 'manual_4000' | 'below_target_watch'
export type CandidateStatus = 'pending' | 'needs_information' | 'watch' | 'shortlisted' | 'rejected'
export type ReviewState =
  | 'pending'
  | 'claimed'
  | 'needs_information'
  | 'watch'
  | 'shortlisted'
  | 'rejected'
  | 'superseded'
export type ReviewOutcome = 'needs_information' | 'watch' | 'shortlisted' | 'rejected'
export type EligibilityState = 'eligible_primary' | 'eligible_manual_profile' | 'needs_facts' | 'rejected'
export type ValuationState = 'not_started' | 'incomplete' | 'estimated' | 'quote_supported' | 'stale' | 'invalid'
export type Availability = 'available' | 'reserved' | 'removed' | 'sold_claimed' | 'unknown'
export type PriceBasis = 'gross' | 'net' | 'unknown'
export type PriceType =
  | 'full_vehicle_asking'
  | 'instalment'
  | 'leasing'
  | 'deposit'
  | 'auction_start'
  | 'auction_current_bid'
  | 'export_net'
  | 'parts_or_damaged'
  | 'price_on_request'
  | 'unknown'
export type Tristate = 'yes' | 'no' | 'unknown'
export type OdometerClaim =
  | 'seller_reported'
  | 'documented'
  | 'verified'
  | 'estimated'
  | 'range_only'
  | 'conflicting'
  | 'unknown'
export type ClaimStatus = 'verified' | 'seller_claimed' | 'seller_denied' | 'conflicting' | 'unknown'
export type Confidence = 'high' | 'medium' | 'low'
export type ExtractionMethod =
  | 'json_ld'
  | 'microdata'
  | 'css'
  | 'xpath'
  | 'regex'
  | 'api_field'
  | 'llm_fallback'
  | 'manual'
  | 'derived'
export type SellerType = 'dealer' | 'private' | 'unknown'
export type Precision = 'day' | 'month' | 'year' | 'unknown'
export type BodyType =
  | 'suv'
  | 'offroad'
  | 'crossover'
  | 'pickup'
  | 'estate'
  | 'sedan'
  | 'hatchback'
  | 'van'
  | 'other'
  | 'unknown'
export type Fuel =
  | 'diesel'
  | 'petrol'
  | 'hybrid_petrol'
  | 'hybrid_diesel'
  | 'plugin_hybrid'
  | 'lpg'
  | 'cng'
  | 'electric'
  | 'other'
  | 'unknown'
export type Gearbox = 'manual' | 'automatic' | 'semi_automatic' | 'unknown'
export type Drive = 'fwd' | 'rwd' | 'awd' | '4wd' | 'unknown'
export type SteeringSide = 'left' | 'right' | 'unknown'
export type FreshnessFlag =
  | 'detail_never_fetched'
  | 'detail_stale'
  | 'not_seen_recently'
  | 'availability_unchecked'
  | 'source_paused'
export type CostLineStatus = 'quoted' | 'estimated' | 'actual' | 'not_applicable' | 'unknown'
export type CostCategory =
  | 'purchase'
  | 'bank_fx_charges'
  | 'travel_inspection'
  | 'transport'
  | 'export_plates_insurance'
  | 'customs_broker'
  | 'import_duty'
  | 'motor_vehicle_tax'
  | 'import_vat'
  | 'other_import_charges'
  | 'homologation_registration'
  | 'repairs'
  | 'preparation'
  | 'risk_reserve'
  | 'storage_holding'
  | 'selling_costs'
  | 'refundable_deposit'
export type EvidenceKind = 'asking_price' | 'seller_reported_sale' | 'verified_sale' | 'owner_estimate'
export type ScenarioName = 'conservative' | 'base' | 'upside'
export type TaxRuleStatus =
  | 'draft'
  | 'under_review'
  | 'approved'
  | 'active'
  | 'superseded'
  | 'expired'
  | 'revoked'
  | 'unapproved'
export type SourceRunState = 'running' | 'paused' | 'disabled' | 'blocked' | 'parser_unhealthy' | 'not_scanned'
export type TermsStatus = 'unreviewed' | 'permitted' | 'no_restriction_found' | 'restricted'
export type TermsDecision = 'pending' | 'proceed_acknowledged' | 'proceed_permitted' | 'do_not_use'
export type TechnicalStatus =
  | 'untested'
  | 'fixture_tested'
  | 'live_smoke_passed'
  | 'degraded'
  | 'parser_unhealthy'
  | 'access_blocked'
export type SourceMode = 'public_html' | 'official_api' | 'fixture'
export type GateStatus =
  | 'not_requested'
  | 'implemented'
  | 'fixture_verified'
  | 'integration_verified'
  | 'live_verified'
  | 'active'
  | 'blocked'
export type OutboxState =
  | 'pending'
  | 'sending'
  | 'retry_wait'
  | 'delivered'
  | 'uncertain'
  | 'blocked'
  | 'dead_letter'
  | 'cancelled'
export type OutboxAttentionState = 'uncertain' | 'blocked' | 'dead_letter' | 'retry_wait'
export type JobState = 'queued' | 'running' | 'succeeded' | 'retry_wait' | 'blocked' | 'dead_letter' | 'cancelled'
export type DashboardAction = 'needs_inspection' | 'needs_documents' | 'price_confirmation_needed'
export type ChecklistTopic =
  | 'availability_price'
  | 'vin'
  | 'spec_match'
  | 'odometer_records'
  | 'mechanical_faults'
  | 'accident_damage'
  | 'wear_items'
  | 'running_transport'
  | 'registration_export_docs'
  | 'co2_origin'
  | 'inspection'
  | 'export_plates_insurance'
  | 'ownership_payment'
export type ChecklistItemStatus =
  | 'answered_by_evidence'
  | 'seller_claim_only'
  | 'unknown'
  | 'needs_inspection'
  | 'needs_documents'
  | 'price_confirmation_needed'

// --------------------------------------------------------------------------- envelope and errors

export interface ResponseWarning {
  code: WarningCode
  message: string
}

/** Every `/api/*` success body. */
export interface ResponseEnvelope<T> {
  schema_version: '1.0'
  request_id: string
  as_of: DateTimeString
  data: T
  warnings: ResponseWarning[]
  next_cursor: string | null
}

export type ErrorDetailValue = string | number | boolean | null | string[]

export interface ErrorPayload {
  code: ErrorCode
  message: string
  retryable: boolean
  retry_after_seconds: number | null
  correlation_id: string | null
  details: Record<string, ErrorDetailValue> | null
}

/** Every failed `/api/*` body. */
export interface ApiErrorResponse {
  schema_version: '1.0'
  request_id: string
  as_of: DateTimeString
  error: ErrorPayload
}

// --------------------------------------------------------------------------- shared views

export interface AmountView {
  status: 'known' | 'unknown' | 'not_applicable'
  amount: DecimalString | null
  currency: CurrencyCode | null
  reason: string | null
}

export interface FxRateView {
  base: CurrencyCode
  quote: CurrencyCode
  rate: DecimalString
  rate_date: string
  provider: string
  purpose: 'reference' | 'customs' | 'payment'
  direction: string
}

export interface PartialDate {
  value?: string | null
  precision?: Precision
}

export interface SourceTimestamp {
  value?: DateTimeString | null
  raw?: string | null
  zone_assumed?: boolean
  assumed_zone?: string | null
  precision?: Precision
}

// --------------------------------------------------------------------------- identity

export interface WorkspaceView {
  workspace_id: Uuid
  name: string
  display_timezone: string
}

export interface MembershipView {
  workspace_id: Uuid
  workspace_name: string
  role: Role
  active: boolean
}

export interface MeView {
  principal_id: Uuid
  principal_kind: 'user' | 'mcp_client'
  display_name: string | null
  role: Role
  scopes: Scope[]
  workspace: WorkspaceView
  memberships: MembershipView[]
}

// --------------------------------------------------------------------------- overview

export interface OverviewSourceItem {
  source_id: Uuid
  source_key: string
  display_name: string
  country: CountryCode
  state: SourceRunState
  last_successful_scan_at: DateTimeString | null
  pause_reason: string | null
}

export interface CoverageGapView {
  source_key: string
  profile: ProfileKey | null
  partition_key: string | null
  kind: 'not_scanned' | 'incomplete_scan' | 'budget_limited' | 'blocked' | 'parser_unhealthy' | 'paused'
  since: DateTimeString | null
  reasons: string[]
}

export interface QueueCount {
  profile: ProfileKey
  queue_label: string
  pending: number
  claimed: number
  needs_information: number
}

export interface ReviewCounts {
  pending: number
  claimed: number
  needs_information: number
  watch: number
  shortlisted: number
  by_queue: QueueCount[]
}

export interface DeliveryCounts {
  uncertain: number
  blocked: number
  dead_letter: number
  retry_wait: number
}

export interface GateView {
  capability: string
  dependency: string
  required_evidence: string
  status: GateStatus
  owner: string | null
  next_action: string | null
  checked_at: DateTimeString | null
}

export interface OverviewView {
  sources: OverviewSourceItem[]
  running_sources: number
  paused_sources: number
  last_successful_scan_at: DateTimeString | null
  coverage_gaps: CoverageGapView[]
  pending_reviews: ReviewCounts
  failed_deliveries: DeliveryCounts
  activation_blockers: GateView[]
  bridge_status: 'unavailable' | 'configured' | 'verified'
  coverage_note: string
}

// --------------------------------------------------------------------------- candidates

export interface PriceSummary {
  payable: AmountView
  original_currency: CurrencyCode | null
  eur_equivalent: AmountView
  fx_rate: FxRateView | null
  basis: PriceBasis
  price_type: PriceType
  negotiable: Tristate
}

export interface FreshnessView {
  first_seen_at: DateTimeString | null
  last_seen_at: DateTimeString | null
  last_detail_success_at: DateTimeString | null
  last_availability_check_at: DateTimeString | null
  stale: boolean
  flags: FreshnessFlag[]
}

export interface RankSummary {
  score: DecimalString
  scoring_version: string
  is_probability: false
  label: string
}

export interface FeatureContributionView {
  feature: string
  points: DecimalString
  min_points: DecimalString
  max_points: DecimalString
  explanation: string
}

export interface RankView extends RankSummary {
  contributions: FeatureContributionView[]
}

export interface CandidateSummary {
  listing_id: Uuid
  revision_id: Uuid | null
  revision_number: number | null
  source_id: Uuid
  source_key: string
  source_country: CountryCode
  seller_country: CountryCode | null
  title: string | null
  make: string | null
  model: string | null
  generation: string | null
  price: PriceSummary
  mileage_km: DecimalString | null
  mileage_claim: OdometerClaim
  first_registration: PartialDate
  availability: Availability
  eligibility: EligibilityState | null
  eligibility_profile: ProfileKey | null
  queue_label: string | null
  valuation_id: Uuid | null
  valuation_state: ValuationState
  case_id: Uuid | null
  review_state: ReviewState | null
  freshness: FreshnessView
  rank: RankSummary | null
  research_candidate: boolean
  quarantined: boolean
  is_fixture: boolean
}

export interface CandidateListView {
  items: CandidateSummary[]
}

export interface RevisionView {
  revision_id: Uuid
  revision_number: number
  current_revision_number: number
  is_current: boolean
  observed_at: DateTimeString
  semantic_hash: string
  parser_version: string
  schema_version: '1.0'
}

export interface LocationInfo {
  country?: CountryCode | null
  region?: string | null
  city?: string | null
  approx_lat?: string | null
  approx_lon?: string | null
  coordinates_source?: string | null
}

export interface MileageOriginal {
  amount?: string | null
  unit?: 'km' | 'mi' | 'unknown'
  text?: string | null
  is_estimate?: boolean
  range_low?: string | null
  range_high?: string | null
}

export interface VehicleSpec {
  make?: string | null
  model?: string | null
  generation?: string | null
  facelift?: Tristate
  trim?: string | null
  model_year?: number | null
  first_registration?: PartialDate
  production_year?: number | null
  body_type?: BodyType
  steering_side?: SteeringSide
  seats?: number | null
  fuel?: Fuel
  engine_code?: string | null
  engine_displacement_cm3?: number | null
  power_kw?: number | null
  gearbox?: Gearbox
  gearbox_subtype?: string | null
  drive?: Drive
  mileage_km?: string | null
  mileage_original?: MileageOriginal
  mileage_claim?: OdometerClaim
}

export interface PriceInfo {
  raw_text?: string | null
  amount_minor?: number | null
  currency?: CurrencyCode | null
  basis?: PriceBasis
  type?: PriceType
  negotiable?: Tristate
  vat_treatment?: 'vat_shown' | 'margin_scheme' | 'private_sale' | 'not_stated' | 'unknown'
  vat_rate_stated?: string | null
  vat_amount_minor?: number | null
  vat_reclaimable?: Tristate
  gross_amount_minor?: number | null
  net_amount_minor?: number | null
  export_net_price_minor?: number | null
  refundable_deposit_minor?: number | null
  required_seller_fees_minor?: number | null
  required_seller_fees_known?: Tristate
}

export interface ConditionClaims {
  accident_free?: ClaimStatus
  roadworthy?: ClaimStatus
  running?: ClaimStatus
  warning_lights_off?: ClaimStatus
  corrosion_free?: ClaimStatus
  full_service_history?: ClaimStatus
  damaged_vehicle?: ClaimStatus
  mechanical_faults?: string[]
}

export interface Documentation {
  vin?: string | null
  vin_format_valid?: Tristate
  registration_documents?: ClaimStatus
  coc_available?: ClaimStatus
  emissions_class?: string | null
  origin_evidence?: string | null
  inspection_expiry?: PartialDate
  previous_owners?: number | null
}

export interface Co2Info {
  g_per_km?: string | null
  cycle?: 'nedc' | 'nedc_correlated' | 'wltp' | 'unknown'
  evidence_id?: Uuid | null
}

export interface NormalizedFieldsView {
  seller_type: SellerType
  location: LocationInfo
  vehicle: VehicleSpec
  price: PriceInfo
  availability: Availability
  condition: ConditionClaims
  documentation: Documentation
  co2: Co2Info
  language: string | null
  source_published_at: SourceTimestamp
  source_modified_at: SourceTimestamp
  warnings: string[]
  claims_notice: string
}

export interface SellerTextView {
  trust: 'untrusted_seller_text'
  notice: string
  title: string | null
  description_excerpt: string | null
}

export interface FieldProvenanceView {
  field_path: string
  method: ExtractionMethod
  confidence: Confidence
  confidence_meaning: 'extraction_reliability_not_truth'
  claim_status: ClaimStatus | null
  selector: string | null
  raw_text: string | null
  transformation: string | null
  source_url: string | null
  snapshot_id: Uuid | null
  evidence_id: Uuid | null
  observed_at: DateTimeString
}

export interface FieldConflict {
  field: string
  values: string[]
  locations?: string[]
  resolution?: string
  note?: string | null
}

export interface AvailabilityPoint {
  observed_at: DateTimeString
  availability: Availability
  observed_via: 'detail' | 'search_card' | 'recheck' | 'reconciliation'
  revision_number: number | null
}

export interface PricePoint {
  revision_number: number
  observed_at: DateTimeString
  payable: AmountView
  price_type: PriceType
  basis: PriceBasis
  change: 'initial' | 'decrease' | 'increase' | 'unchanged' | 'not_comparable'
}

export interface ScreeningReason {
  code: string
  message: string
  field?: string | null
  severity: 'reject' | 'needs_facts' | 'warning' | 'info'
  profile?: ProfileKey | null
}

export interface ProfileEvaluation {
  profile: ProfileKey
  enabled: boolean
  outcome: 'eligible' | 'needs_facts' | 'rejected'
  reasons: ScreeningReason[]
}

export interface ScreeningView {
  state: EligibilityState
  profile: ProfileKey | null
  queue_label: string | null
  payable_eur: AmountView
  fx_rate: FxRateView | null
  reasons: ScreeningReason[]
  missing_facts: string[]
  profile_evaluations: ProfileEvaluation[]
  screening_version: string
  screened_at: DateTimeString | null
}

export interface ValuationRef {
  valuation_id: Uuid
  state: ValuationState
  research_candidate: boolean
  is_fixture: boolean
  created_at: DateTimeString
  expires_at: DateTimeString | null
  dependency_fingerprint: string
  conservative_contribution: AmountView
  base_contribution: AmountView
  contribution_label: 'estimated contribution before business tax'
}

export interface ComparableSetRef {
  comparable_set_id: Uuid
  sample_quality: 'adequate' | 'small' | 'insufficient'
  sample_size: number
  mk_band_fit: 'below' | 'within' | 'above' | 'unknown'
  criteria_version: string
  as_of: DateTimeString
  research_needed: boolean
}

export interface ReviewCaseRef {
  case_id: Uuid
  case_version: number
  state: ReviewState
  profile: ProfileKey
  queue_label: string
}

export interface ChecklistItem {
  topic: ChecklistTopic
  question: string
  status: ChecklistItemStatus
  actions: DashboardAction[]
  notes?: string[]
  evidence_ids?: Uuid[]
}

export interface Checklist {
  version?: string
  source_key: string
  source_listing_id: string
  items: ChecklistItem[]
  needs_inspection: boolean
  needs_documents: boolean
  price_confirmation_needed: boolean
  ready: boolean
  photo_limitation?: string
}

export interface NoteView {
  note_id: Uuid
  listing_id: Uuid
  case_id: Uuid | null
  label: 'owner' | 'reviewer' | 'assistant'
  author_kind: 'user' | 'mcp_client' | 'system'
  author_principal_id: Uuid
  body: string
  created_at: DateTimeString
  updated_at: DateTimeString
  row_version: number
  notice: string
}

export interface SourceLink {
  url: string
  source_key: string
  external: true
  rel: 'noopener noreferrer'
  notice: string
}

export interface CandidateDetail {
  summary: CandidateSummary
  revision: RevisionView
  normalized: NormalizedFieldsView
  seller_text: SellerTextView
  field_provenance: FieldProvenanceView[]
  conflicts: FieldConflict[]
  availability_history: AvailabilityPoint[]
  price_history: PricePoint[]
  screening: ScreeningView | null
  latest_valuation: ValuationRef | null
  comparable_set: ComparableSetRef | null
  review_case: ReviewCaseRef | null
  due_diligence: Checklist | null
  notes: NoteView[]
  rank: RankView | null
  source_link: SourceLink
}

// --------------------------------------------------------------------------- comparables

export interface MatchDifference {
  dimension: string
  code: string
  severity: 'info' | 'context' | 'unverified' | 'differs' | 'widened'
  target?: string | null
  comparable?: string | null
}

export interface ComparableMemberView {
  ordinal: number
  observation_id: Uuid
  role: 'selected' | 'excluded'
  evidence_kind: EvidenceKind
  evidence_note: string
  match_level: 'exact' | 'close' | 'widened' | null
  weight: DecimalString | null
  widened_dimensions: Array<'mileage' | 'year' | 'facelift'>
  differences: MatchDifference[]
  exclusion_reasons: string[]
  duplicate_of: Uuid | null
  duplicate_cluster_id: Uuid | null
  exclusion_details: string[]
  advertised: AmountView
  amount_eur: AmountView
  price_basis: PriceBasis | null
  observed_at: DateTimeString | null
  source_key: string | null
  url: string | null
  make: string | null
  model: string | null
  generation: string | null
  mileage_km: DecimalString | null
  local_registration_status: 'locally_registered' | 'imported_unregistered' | 'unknown' | null
  seller_type: SellerType | null
  availability: Availability | null
  is_fixture: boolean | null
}

export interface StatPointView {
  observation_id: Uuid
  amount_eur: DecimalString
  match_level: 'exact' | 'close' | 'widened'
  observed_at: DateTimeString
  difference_codes: string[]
}

export interface EvidenceStatsView {
  evidence_kind: EvidenceKind
  evidence_note: string
  currency: 'EUR'
  n: number
  sample_label: 'single_observation' | 'small_sample' | 'adequate' | 'unverified_claims'
  min: DecimalString
  max: DecimalString
  median: DecimalString
  q1: DecimalString | null
  q3: DecimalString | null
  date_from: DateTimeString
  date_to: DateTimeString
  match_quality: Array<{ match_level: string; count: number }>
  points: StatPointView[]
}

export interface WideningStep {
  step: number
  dimension: 'mileage' | 'year' | 'facelift'
  label: string
  selected_count_after: number
  adequacy_count_after: number
}

export interface MkBandView {
  min_eur: DecimalString
  max_eur: DecimalString
  fit: 'below' | 'within' | 'above' | 'unknown'
  basis: string
  meaning: string
}

export interface MatchingCriteria {
  criteria_version?: string
  year_window: number
  mileage_window_km: string
  max_year_window: number
  max_mileage_window_km: string
  max_age_days: number
  min_sample: number
  displacement_tolerance?: string
  power_tolerance?: string
  widening_order?: Array<'mileage' | 'year' | 'facelift'>
}

export interface ComparableSetView {
  comparable_set_id: Uuid
  listing_id: Uuid
  target_revision_id: Uuid
  criteria_version: string
  as_of: DateTimeString
  status: 'adequate' | 'small_sample' | 'insufficient_comparables'
  sample_quality: 'adequate' | 'small' | 'insufficient'
  research_needed: boolean
  is_fixture: boolean
  criteria: MatchingCriteria
  widening_steps: WideningStep[]
  mk_band: MkBandView
  asking_price_stats: EvidenceStatsView | null
  seller_reported_sale_stats: EvidenceStatsView | null
  verified_sale_stats: EvidenceStatsView | null
  asking_vs_sale_notice: string
  selected_count: number
  excluded_count: number
  include_excluded: boolean
  members: ComparableMemberView[]
  warnings: string[]
}

// --------------------------------------------------------------------------- valuations

export interface ContributionsView {
  conservative: AmountView
  base: AmountView
  upside: AmountView
  label: 'estimated contribution before business tax'
}

export interface ScenarioTermView {
  term: string
  label: string
  amount: AmountView
}

export interface UnknownLineView {
  item: string
  label: string
  reason: string
}

export interface ScenarioView {
  scenario: ScenarioName
  complete: boolean
  currency: CurrencyCode
  proceeds_label: string
  expected_realized_proceeds: AmountView
  components: ScenarioTermView[]
  /** Present only when the scenario is complete. */
  totals: ScenarioTermView[] | null
  /** Present when incomplete: the sum of KNOWN lines only, never a total. */
  known_subtotal: AmountView | null
  known_subtotal_label: 'known_subtotal'
  unknown_lines: UnknownLineView[]
  contribution_before_business_tax: AmountView
  contribution_label: 'estimated contribution before business tax'
  assumptions: string[]
}

export interface CostScope {
  listing_id?: string | null
  origin_country?: CountryCode | null
  origin_city?: string | null
  destination_city?: string | null
  vehicle_running?: 'running' | 'non_running' | null
  note?: string | null
}

export interface CostLineView {
  category: CostCategory
  label: string
  status: CostLineStatus
  declared_status: CostLineStatus
  currency: CurrencyCode
  low: DecimalString | null
  base: DecimalString | null
  high: DecimalString | null
  evidence_ids: string[]
  provider: string | null
  expires_at: DateTimeString | null
  scope: CostScope | null
  reason: string | null
  cash_before_sale: boolean
  refundable: boolean
  refund_confirmed: boolean
  assumption_approved: boolean
  rule_supported: boolean
  correlation_group: string | null
}

export interface PurchaseView {
  label: string
  status: CostLineStatus
  amount: AmountView
  basis: string | null
  evidence_ids: string[]
  included_refundable_deposit: AmountView
  deposit_refund_confirmed: boolean
  assumption_approved: boolean
}

export interface ProceedsView {
  status: CostLineStatus
  basis: string
  label: string
  currency: CurrencyCode
  low: AmountView
  base: AmountView
  high: AmountView
  evidence_kind: EvidenceKind | null
  sample_size: number | null
  negotiation_discount_pct: DecimalString | null
  discount_status: 'unknown' | 'unapproved_assumption' | 'approved'
  comparable_set_id: string | null
  evidence_ids: string[]
  notice: string
}

export interface TaxInputUsed {
  name: string
  value: string
}

export interface TaxComponentView {
  component_id: string
  label: string
  category: CostCategory
  kind: string
  status: 'resolved' | 'unknown' | 'not_applicable'
  amount: AmountView
  inputs_used: TaxInputUsed[]
  missing_inputs: string[]
  warnings: string[]
}

export interface TaxView {
  rule_set_id: string
  version: string
  rule_status: TaxRuleStatus
  rule_sha256: string | null
  is_fixture: boolean
  production_ready: boolean
  approval_label: string
  jurisdiction: string
  currency: CurrencyCode
  effective_date: string
  engine_version: string
  complete: boolean
  total_import_cost: AmountView | null
  known_subtotal: AmountView | null
  known_subtotal_label: 'known_subtotal'
  unknown_components: string[]
  missing_inputs: string[]
  components: TaxComponentView[]
  warnings: string[]
}

export interface ScenarioCheck {
  scenario: ScenarioName
  would_meet: boolean | null
}

export interface ThresholdView {
  threshold: AmountView
  approval_status: 'unapproved' | 'approved'
  label: 'PROPOSED' | 'APPROVED'
  proposed_only: boolean
  would_meet: boolean | null
  would_meet_by_scenario: ScenarioCheck[]
  alert_eligible: boolean
  blockers: string[]
}

export interface ComparableReference {
  comparable_set_id: string
  content_hash: string
  sample_size: number
  quality: 'adequate' | 'small' | 'insufficient_comparables'
  fresh_until?: DateTimeString | null
  is_fixture?: boolean
}

export interface VersionsView {
  calculation_version: string
  cost_model_version: string | null
  tax_engine_version: string | null
  tax_rule: string | null
  cost_profile: string | null
  config_revision_id: string
}

export interface ValuationDependencies {
  calculation_version?: string
  listing_revision_id: string
  comparable_set_id?: string | null
  comparable_hash?: string | null
  tax_rule?: {
    rule_set_id: string
    version: string
    sha256: string | null
    status: TaxRuleStatus
    valid_from: string | null
    valid_to: string | null
    is_fixture: boolean
  } | null
  fx?: Array<{
    base: string
    quote: string
    rate: string
    rate_date: string
    provider: string
    purpose: 'reference' | 'customs' | 'payment'
  }>
  cost_profile?: {
    profile_key: string
    version: number
    sha256: string
    approval_status: 'unapproved' | 'approved'
    is_fixture: boolean
  } | null
  cost_evidence_ids?: string[]
  cost_inputs_sha256?: string | null
  config_revision_id: string
  evidence_ids?: string[]
}

export interface ValuationView {
  valuation_id: Uuid
  listing_id: Uuid
  listing_revision_id: string
  listing_revision: number | null
  state: ValuationState
  research_candidate: boolean
  is_fixture: boolean
  fixture_label: string | null
  alert_eligible: boolean
  currency: CurrencyCode
  created_at: DateTimeString
  expires_at: DateTimeString | null
  stale_at: DateTimeString | null
  stale_reason: string | null
  contribution_label: 'estimated contribution before business tax'
  terminology_note: string
  contributions: ContributionsView
  scenarios: ScenarioView[]
  purchase: PurchaseView | null
  proceeds: ProceedsView | null
  cost_lines: CostLineView[]
  tax: TaxView | null
  threshold: ThresholdView | null
  material_support: 'quote_supported' | 'approved_assumptions' | 'estimated' | 'incomplete' | null
  unsupported_material: string[]
  eligibility: EligibilityState
  eligibility_profile: ProfileKey | null
  payable_eur: AmountView
  comparable: ComparableReference | null
  unknowns: string[]
  assumptions: string[]
  warnings: string[]
  correlation_notes: string[]
  dependency_fingerprint: string
  dependencies: ValuationDependencies
  versions: VersionsView
}

// --------------------------------------------------------------------------- reviews

export interface ClaimStateView {
  claimed: boolean
  held_by_caller: boolean
  expires_at: DateTimeString | null
}

export interface ReviewQueueItem {
  case_id: Uuid
  case_version: number
  listing_id: Uuid
  revision_id: Uuid
  listing_revision: number
  valuation_id: Uuid | null
  profile: ProfileKey
  queue_label: string
  state: ReviewState
  eligibility: EligibilityState | null
  readiness: string
  valuation_state: ValuationState
  priority: number
  rank: RankSummary | null
  claim: ClaimStateView
  title: string | null
  make: string | null
  model: string | null
  seller_country: CountryCode | null
  payable: AmountView
  payable_eur: AmountView
  mileage_km: DecimalString | null
  research_candidate: boolean
  is_fixture: boolean
  created_at: DateTimeString
  updated_at: DateTimeString
}

export interface ReviewQueuePage {
  items: ReviewQueueItem[]
  total: number
  snapshot_created_at: DateTimeString
  snapshot_expires_at: DateTimeString
  include_needs_information: boolean
  notice: string
}

export interface DecisionActorView {
  principal_id: Uuid
  principal_kind: 'user' | 'mcp_client' | 'system'
  role: Role
}

export interface ReviewDecisionView {
  decision_id: Uuid
  case_id: Uuid
  /** The case version the decision was made against. */
  case_version: number
  case_state: ReviewState
  new_case_version: number
  listing_id: Uuid
  listing_revision_id: Uuid
  listing_revision: number
  valuation_id: Uuid | null
  outcome: ReviewOutcome
  reason_codes: string[]
  summary: string
  evidence_ids: Uuid[]
  missing_information: string[]
  actor: DecisionActorView
  model_name: string | null
  model_version: string | null
  model_run_id: string | null
  prompt_template_version: string | null
  tool_request_id: string
  input_hash: string
  decided_at: DateTimeString
  supersedes_decision_id: Uuid | null
  is_fixture: boolean
  notice: string
}

export interface ReviewCaseView {
  case_id: Uuid
  case_version: number
  state: ReviewState
  listing_id: Uuid
  revision_id: Uuid
  listing_revision: number
  profile: ProfileKey
  queue_label: string
  readiness: string
  priority: number
  claim: ClaimStateView
  candidate: CandidateSummary
  valuation: ValuationRef | null
  latest_decision_id: Uuid | null
  decisions: ReviewDecisionView[]
  superseded_by_id: Uuid | null
  reason: string | null
  is_fixture: boolean
  created_at: DateTimeString
  updated_at: DateTimeString
}

export interface ClaimResult {
  case_id: Uuid
  /** Shown once; `null` on an idempotent replay (`claim_token_redacted: true`). */
  claim_token: string | null
  claim_token_redacted: boolean
  token_notice: string
  expires_at: DateTimeString
  case_version: number
  listing_id: Uuid
  revision_id: Uuid
  listing_revision: number
  valuation_id: Uuid | null
  rotated: boolean
  took_over_expired: boolean
}

export interface ReleaseResultView {
  case_id: Uuid
  released: boolean
  reason: 'released' | 'expired' | 'not_claimed' | 'not_held'
  state: ReviewState
  case_version: number
}

// --------------------------------------------------------------------------- notes and rechecks

export interface RecheckRequestResult {
  job_id: Uuid
  listing_id: Uuid
  job_type: 'recheck'
  state: JobState
  deduplicated: boolean
  available_at: DateTimeString | null
  notice: string
}

// --------------------------------------------------------------------------- sources

export interface TermsView {
  status: TermsStatus
  decision: TermsDecision
  decision_actor: string | null
  decision_note: string | null
  reviewed_at: DateTimeString | null
  terms_url: string | null
  meaning: string
}

export interface ParserHealthView {
  status: 'healthy' | 'degraded' | 'unhealthy' | 'insufficient_sample' | 'unknown'
  sample_size: number
  reasons: string[]
  recommended_actions: string[]
  checked_at: DateTimeString | null
}

export interface TechnicalView {
  status: TechnicalStatus
  mode: SourceMode
  adapter: string
  adapter_version: string
  detail_mode: 'fetch' | 'card_only'
  last_live_smoke_at: DateTimeString | null
  parser_health: ParserHealthView
}

export interface RobotsView {
  policy: 'obey'
  last_checked_at: DateTimeString | null
  revision_hash: string | null
  fetch_status: number | null
  summary: string | null
}

export interface RateBudget {
  max_concurrency_per_host?: number
  min_delay_seconds?: number
  max_search_pages_per_run?: number
  max_detail_jobs_per_run?: number
  daily_request_budget?: number
  daily_byte_budget?: number
  request_timeout_seconds?: number
  max_response_bytes?: number
  max_redirects?: number
}

export interface RateBudgetView {
  budget: RateBudget
  budget_label: 'engineering_default' | 'owner_approved'
  requests_today: number | null
  bytes_today: number | null
  circuit_state: 'closed' | 'open' | 'half_open' | 'unknown'
  next_request_not_before: DateTimeString | null
  retry_after_until: DateTimeString | null
}

export interface CrawlRunView {
  run_id: Uuid
  profile: ProfileKey | null
  partition_key: string
  coverage_mode: 'watermark' | 'rolling_pages'
  started_at: DateTimeString
  finished_at: DateTimeString | null
  outcome: 'running' | 'complete' | 'budget_limited' | 'partial' | 'failed' | 'blocked' | 'cancelled'
  pages_fetched: number
  cards_seen: number
  new_listings: number
  changed_listings: number
  detail_jobs_enqueued: number
  detail_jobs_deduplicated: number
  access_state: string | null
  error_code: string | null
  gap_reasons: string[]
  adapter_version: string
  parser_version: string | null
}

export interface SourceStatusView {
  source_id: Uuid
  source_key: string
  display_name: string
  country: CountryCode
  role: 'acquisition' | 'mk_comparable'
  state: SourceRunState
  enabled: boolean
  paused: boolean
  pause_reason: string | null
  paused_at: DateTimeString | null
  version: number
  terms: TermsView
  technical: TechnicalView
  robots: RobotsView
  rate_budget: RateBudgetView
  last_runs: CrawlRunView[]
  activation_problems: string[]
}

export interface SourceListView {
  items: SourceStatusView[]
}

export interface SourcePauseResult {
  source_id: Uuid
  source_key: string
  paused: true
  already_paused: boolean
  version: number
  paused_at: DateTimeString
  reason: string
  notice: string
}

// --------------------------------------------------------------------------- settings

export interface ConfigRevisionRef {
  config_revision_id: Uuid
  revision: number
  created_at: DateTimeString
  reason: string | null
}

export interface ProfileView {
  profile_key: ProfileKey
  label: string
  queue_label: string
  enabled: boolean
  optional: boolean
  /** Starts with "ENABLED" or "DISABLED" (e.g. "DISABLED - optional manual-review profile ..."). */
  status_label: string
  min_price_eur: DecimalString | null
  max_price_eur: DecimalString
  max_price_inclusive: boolean
  max_mileage_km_exclusive: DecimalString
  source_countries: CountryCode[]
  config_revision_id: Uuid | null
  row_version: number | null
}

export interface MkBandSettingView {
  min_eur: DecimalString
  max_eur: DecimalString
  meaning: string
}

export interface ThresholdSettingView {
  amount_eur: DecimalString
  approval_status: 'unapproved' | 'approved'
  label: 'PROPOSED' | 'APPROVED'
  approved_by: string | null
  approved_at: string | null
  note: string
}

export interface RealertPolicyView {
  abs_eur: DecimalString
  pct: DecimalString
  approved: boolean
  label: 'PROPOSED' | 'APPROVED'
}

export interface DestinationBindingView {
  binding_id: Uuid
  provider: 'slack' | 'mcp_events'
  label: string
  enabled: boolean
  approval_recorded: boolean
  approved_at: DateTimeString | null
  verified_at: DateTimeString | null
  external_workspace_id: string | null
  external_channel_id: string | null
  row_version: number
}

export interface SettingsView {
  config_revision: ConfigRevisionRef | null
  profiles: ProfileView[]
  mk_resale_band: MkBandSettingView
  contribution_threshold: ThresholdSettingView
  price_realert_policy: RealertPolicyView
  destination_bindings: DestinationBindingView[]
  gates: GateView[]
  can_administer: boolean
  administration_note: string
}

// --------------------------------------------------------------------------- outbox

export interface OutboxItemView {
  outbox_id: Uuid
  event_id: Uuid
  event_type: string
  aggregate_type: string
  aggregate_id: Uuid
  aggregate_version: number | null
  state: OutboxState
  attempts: number
  max_attempts: number
  destination_binding_id: Uuid | null
  last_error_code: string | null
  blocker_code: string | null
  event_created_at: DateTimeString
  send_attempted_at: DateTimeString | null
  provider_accepted_at: DateTimeString | null
  owner_seen_at: DateTimeString | null
  available_at: DateTimeString
  is_fixture: boolean
  uncertain_notice: string | null
}

export interface OutboxPage {
  items: OutboxItemView[]
}

// --------------------------------------------------------------------------- queries and bodies

export interface CandidateListQuery {
  cursor?: string
  limit?: number
  profile?: ProfileKey
  country?: CountryCode
  status?: CandidateStatus
  /** RFC 3339 with an offset, e.g. `2026-10-06T10:00:00Z`. */
  changed_since?: DateTimeString
}

export interface ComparablesQuery {
  include_excluded?: boolean
  cursor?: string
  limit?: number
}

export interface ReviewQueueQuery {
  cursor?: string
  limit?: number
  include_needs_information?: boolean
}

export interface OutboxQuery {
  cursor?: string
  limit?: number
  state?: OutboxAttentionState
}

/** 8-128 characters of `A-Z a-z 0-9 . _ : -`. */
export type IdempotencyKey = string

export interface ClaimRequest {
  expected_version: number
  idempotency_key: IdempotencyKey
}

export interface ReleaseRequest {
  claim_token: string
  idempotency_key: IdempotencyKey
}

export interface SubmitReviewRequest {
  claim_token: string
  expected_version: number
  listing_revision: number
  valuation_id: Uuid | null
  outcome: ReviewOutcome
  reason_codes: string[]
  summary: string
  evidence_ids: Uuid[]
  missing_information: string[]
  idempotency_key: IdempotencyKey
}

export interface AddNoteRequest {
  note: string
  idempotency_key: IdempotencyKey
}

export interface RecheckRequest {
  reason: string
  idempotency_key: IdempotencyKey
}

export interface PauseSourceRequest {
  expected_version: number
  reason: string
  idempotency_key: IdempotencyKey
}

/** Request-body rules mirrored from the backend for early feedback (the server stays authoritative). */
export const LIMITS = {
  idempotencyKeyPattern: /^[A-Za-z0-9._:-]{8,128}$/,
  reasonCodePattern: /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/,
  uuidPattern: /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/,
  countryPattern: /^[A-Z]{2}$/,
  summaryMin: 10,
  summaryMax: 4000,
  reasonCodesMax: 20,
  evidenceIdsMax: 100,
  missingInformationMax: 30,
  missingInformationItemMax: 300,
  reasonMin: 3,
  reasonMax: 2000,
  noteMax: 4000,
  pageLimitMax: 100,
} as const

/**
 * Control and bidirectional-override characters refused by the backend in free text
 * (C0 except tab/newline/CR, DEL, zero-width and bidi override/isolate characters). Written with
 * escapes only: no invisible characters in the source (same set as `domain/reviews.py`).
 */
// eslint-disable-next-line no-control-regex
export const FORBIDDEN_TEXT_CHARS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/
