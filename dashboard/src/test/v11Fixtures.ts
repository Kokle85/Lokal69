/** SYNTHETIC spec v1.1 API fixtures for component tests (shapes follow src/api/types.ts). */
import type {
  CoverageLagsView,
  EvaluationReport,
  InquiryControlView,
  InquirySummaryView,
  InquiryView,
  LagView,
  ListingLifecycleView,
  MailboxHealthView,
  MailCoverageGapListView,
  MailWorkerHealthView,
  ReplySummaryView,
  ReplyView,
} from '../api/types'
import { known, LISTING_ID, REVISION_ID, SOURCE_ID } from './fixtures'

export const INQUIRY_ID = '66666666-6666-4666-8666-666666666666'
export const INQUIRY_ID_2 = '66666666-6666-4666-8666-666666666667'
export const INQUIRY_ID_3 = '66666666-6666-4666-8666-666666666668'
export const INQUIRY_ID_4 = '66666666-6666-4666-8666-666666666669'
export const REPLY_ID = '77777777-7777-4777-8777-777777777777'
export const SELLER_ID = '88888888-8888-4888-8888-888888888888'
export const MAILBOX_ID = '99999999-9999-4999-8999-999999999999'
export const SENDER_ID = '99999999-9999-4999-8999-999999999990'
export const SELLER_ADDRESS = 'verkauf@synthetic-dealer.example'

const vehicle = {
  vehicle_kind: 'listing_incarnation' as const,
  vehicle_cluster_id: null,
  listing_id: LISTING_ID,
  source_key: 'synthetic_source',
  listing_reference: 'SYN-TIGUAN-1',
  listing_url: 'https://synthetic-dealer.example/vehicles/SYN-TIGUAN-1',
}

export function inquirySummary(overrides: Partial<InquirySummaryView> = {}): InquirySummaryView {
  return {
    inquiry_id: INQUIRY_ID,
    seller_entity_id: SELLER_ID,
    vehicle,
    state: 'accepted',
    language: 'de',
    recipient_status: 'verified',
    delivery_uncertain: false,
    suppression_reason: null,
    reply_count: 0,
    state_changed_at: '2026-10-07T09:00:00Z',
    reserved_at: '2026-10-07T08:00:00Z',
    send_attempted_at: '2026-10-07T08:30:00Z',
    accepted_at: '2026-10-07T08:31:00Z',
    row_version: 4,
    ...overrides,
  }
}

export function inquiry(overrides: Partial<InquiryView> = {}): InquiryView {
  return {
    inquiry_id: INQUIRY_ID,
    identity_key: 'a'.repeat(64),
    purpose: 'initial_availability_documents_price',
    seller_entity_id: SELLER_ID,
    vehicle,
    state: 'accepted',
    state_reasons: [],
    qualification: {
      listing_revision_id: REVISION_ID,
      revision_number: 2,
      semantic_hash: 'b'.repeat(64),
      asking_price: known('27500.00'),
      availability: 'available',
      readiness: 'inquiry_ready',
      readiness_reasons: ['DOCUMENTS_UNKNOWN_ASKED'],
      rules_version: 'inquiry-readiness@1.0.0',
      evaluated_at: '2026-10-07T07:59:00Z',
    },
    authorization: { authorization_id: '55555555-5555-4555-8555-555555555555', version: 1, fingerprint: 'c'.repeat(64) },
    template: {
      template_id: 'seller_initial_de_v1',
      template_version: 1,
      template_set_version: 'templates@1',
      scope_hash: 'd'.repeat(64),
      body_hash: 'e'.repeat(64),
    },
    language: 'de',
    recipient: {
      contact_id: '44444444-4444-4444-8444-444444444444',
      verification_status: 'verified',
      contact_kind: 'ad_email',
      address: null,
      address_domain: 'synthetic-dealer.example',
      address_redacted: true,
      language: 'de',
      language_status: 'resolved',
      verified_at: '2026-10-07T07:58:00Z',
    },
    sender: { sender_binding_id: SENDER_ID, binding_version: 2, provider: 'outlook_local', display_name: 'Synthetic Sender' },
    rfc_message_id: '<synthetic-inquiry@example.invalid>',
    send_attempts: {
      count: 1,
      last_outcome: 'accepted',
      uncertain: false,
      attempts: [
        {
          attempt_number: 1,
          provider: 'outlook_local',
          outcome: 'accepted',
          send_intent_committed_at: '2026-10-07T08:29:00Z',
          finished_at: '2026-10-07T08:31:00Z',
          reconciled_outcome: null,
          reconciled_at: null,
          submission_uncertain: false,
          error_code: null,
        },
      ],
    },
    delivery_uncertain: false,
    suppression_reason: null,
    message: {
      original_subject: 'Anfrage zu Volkswagen Tiguan – SYN-TIGUAN-1',
      original_body: 'Guten Tag,\n\nIst das Fahrzeug noch verfügbar?\n\nFreundliche Grüße\nSynthetic Sender',
      mk_preview_subject: 'Прашање за Volkswagen Tiguan – SYN-TIGUAN-1',
      mk_preview_body: 'Здраво,\n\nДали возилото е сè уште достапно?\n\nПоздрав\nSynthetic Sender',
      preview_is_informational: true,
    },
    reply_count: 1,
    latest_reply_id: REPLY_ID,
    timestamps: {
      created_at: '2026-10-07T07:58:00Z',
      state_changed_at: '2026-10-07T08:31:00Z',
      reserved_at: '2026-10-07T08:00:00Z',
      queued_at: '2026-10-07T08:01:00Z',
      send_attempted_at: '2026-10-07T08:29:00Z',
      accepted_at: '2026-10-07T08:31:00Z',
      replied_at: null,
      updated_at: '2026-10-07T08:31:00Z',
    },
    row_version: 4,
    approval_required: false,
    ...overrides,
  }
}

export function replySummary(overrides: Partial<ReplySummaryView> = {}): ReplySummaryView {
  return {
    reply_id: REPLY_ID,
    inquiry_id: INQUIRY_ID,
    vehicle,
    message_type: 'seller_reply',
    original_language: 'de',
    availability: 'available',
    quarantined: false,
    processing_state: 'stored',
    received_at: '2026-10-07T12:00:00Z',
    ingested_at: '2026-10-07T12:00:05Z',
    ...overrides,
  }
}

export function reply(overrides: Partial<ReplyView> = {}): ReplyView {
  return {
    reply_id: REPLY_ID,
    inquiry_id: INQUIRY_ID,
    seller_entity_id: SELLER_ID,
    vehicle,
    message_type: 'seller_reply',
    original_language: 'de',
    subject: 'AW: Anfrage zu Volkswagen Tiguan (synthetic)',
    sanitized_body:
      'Guten Tag, das Fahrzeug ist noch verfügbar. Der letzte Preis ist 26.500 EUR. Bitte überweisen Sie 500 EUR Anzahlung zur Reservierung.',
    mk_summary: 'Возилото е достапно. Последна цена: 26.500 EUR (непотврдена понуда). Продавачот бара депозит од 500 EUR.',
    mk_summary_version: 'mk-summary@1',
    mk_summary_generated_at: '2026-10-07T12:00:06Z',
    sender: {
      address: null,
      address_domain: 'synthetic-dealer.example',
      address_redacted: true,
      matches_verified_recipient: true,
      correlation_status: 'matched',
      correlation_reasons: ['IN_REPLY_TO_MATCH', 'SENDER_MATCHES_RECIPIENT'],
      header_linked: true,
      thread_linked: false,
    },
    received_at: '2026-10-07T12:00:00Z',
    observed_at: '2026-10-07T12:00:02Z',
    ingested_at: '2026-10-07T12:00:05Z',
    processing_state: 'stored',
    processed_at: null,
    claims: {
      claims_version: 'claims@1',
      availability: 'available',
      price_quotes: [
        {
          kind: 'single',
          amount: '26500',
          low: null,
          high: null,
          currency: 'EUR',
          conditions: ['final_or_lowest'],
          status: 'unaccepted_seller_quote',
          accepted: false,
          excerpt: 'Der letzte Preis ist 26.500 EUR',
        },
      ],
      documents: [{ kind: 'registration', status: 'available', excerpt: 'Zulassungsbescheinigung liegt vor' }],
      requests: ['payment', 'reservation'],
      escalations: ['payment', 'reservation'],
      unanswered_questions: ['documents'],
    },
    quarantined: false,
    quarantine_reason: null,
    attachments: [
      {
        filename: 'zulassung_geschwaerzt.pdf',
        mime_type: 'application/pdf',
        byte_size: 120_400,
        sha256: 'f'.repeat(64),
        action: 'allow_vehicle_document',
        document_kind: 'registration',
      },
    ],
    withheld_sensitive_attachments: 1,
    valuation: { valuation_id: null, state: 'stale', stale_reason: 'seller reply received', recalculation_pending: true },
    content_withheld: false,
    ...overrides,
  }
}

export function control(overrides: Partial<InquiryControlView> = {}): InquiryControlView {
  return {
    version: 3,
    mode: 'automatic',
    kill_switch: false,
    kill_switch_reason: null,
    kill_switch_set_at: null,
    max_per_24h: 2,
    max_per_15d: 5,
    ceiling_per_24h: 2,
    ceiling_per_15d: 5,
    seller_cooldown_seconds: 7 * 86_400,
    used_24h: 1,
    used_15d: 3,
    updated_at: '2026-10-07T08:00:00Z',
    approval_required: false,
    removable_suppressions: 0,
    ...overrides,
  }
}

export function lag(overrides: Partial<LagView> = {}): LagView {
  return {
    name: 'mailbox_sync_lag',
    status: 'unknown',
    value_seconds: null,
    reason: 'no heartbeat',
    configured_interval_seconds: null,
    note: '',
    ...overrides,
  }
}

export function mailbox(overrides: Partial<MailboxHealthView> = {}): MailboxHealthView {
  return {
    mailbox_binding_id: MAILBOX_ID,
    sender_binding_id: SENDER_ID,
    provider: 'outlook_local',
    worker_label: 'SYNTHETIC desktop worker',
    binding_state: 'active',
    generated_at: '2026-10-07T10:00:00Z',
    last_heartbeat_at: '2026-10-07T09:59:30Z',
    heartbeat_age_seconds: 30,
    heartbeat_status: 'healthy',
    outlook_status: 'healthy',
    mailbox_sync_ok: true,
    mailbox_sync_lag: lag({ status: 'measured', value_seconds: 3, reason: null }),
    last_successful_reconciliation_at: '2026-10-07T09:59:00Z',
    reconciliation_status: 'healthy',
    backlog_count: 0,
    backlog_age: lag({ name: 'backlog_age', status: 'measured', value_seconds: 0, reason: null }),
    unresolved_matching_gaps: 0,
    account_status: 'verified',
    coverage_gaps: [],
    open_gap_count: 0,
    folders: [],
    monitoring_active: true,
    reasons: [],
    ...overrides,
  }
}

export function health(mailboxes: MailboxHealthView[]): MailWorkerHealthView {
  return {
    generated_at: '2026-10-07T10:00:00Z',
    mailboxes,
    any_monitoring_active: mailboxes.some((box) => box.monitoring_active),
    open_gap_count: mailboxes.reduce((total, box) => total + box.open_gap_count, 0),
    notes: [],
  }
}

export function gaps(items: MailCoverageGapListView['items']): MailCoverageGapListView {
  return {
    generated_at: '2026-10-07T10:00:00Z',
    open_gap_count: items.filter((item) => item.gap.open).length,
    items,
  }
}

export function coverageLags(overrides: Partial<CoverageLagsView> = {}): CoverageLagsView {
  return {
    sources: [
      {
        source_id: SOURCE_ID,
        source_key: 'synthetic_source',
        state: 'running',
        last_successful_scan_at: '2026-10-07T09:00:00Z',
        last_complete_scan_at: '2026-10-07T09:00:00Z',
        source_scan_lag: lag({ name: 'source_scan_lag', status: 'measured', value_seconds: 3_600, reason: null, configured_interval_seconds: 900 }),
      },
    ],
    notification_processing_lag: lag({ name: 'notification_processing_lag', status: 'unknown', reason: 'no delivered notifications yet' }),
    mail_reply_detection_lag: lag({ name: 'mail_reply_detection_lag', status: 'inconsistent', reason: 'received after ingest' }),
    v11_tables: [{ relation: 'app.seller_replies', present: true }],
    notes: [],
    ...overrides,
  }
}

export function listingLifecycle(overrides: Partial<ListingLifecycleView> = {}): ListingLifecycleView {
  return {
    listing_id: LISTING_ID,
    source_id: SOURCE_ID,
    source_key: 'synthetic_source',
    source_state: 'running',
    first_seen_at: '2026-10-05T09:00:00Z',
    last_seen_on_search_at: '2026-10-07T09:00:00Z',
    last_detail_success_at: '2026-10-07T08:00:00Z',
    last_availability_check_at: null,
    last_complete_source_scan_at: '2026-10-07T09:00:00Z',
    source_published_at: null,
    source_published_trusted: false,
    availability: 'available',
    detail_freshness: lag({ name: 'detail_freshness', status: 'measured', value_seconds: 7_200, reason: null }),
    detection_delay: lag({ name: 'detection_delay', status: 'unknown', reason: 'source publication time not provided' }),
    availability_history_source: 'availability_events',
    notes: [],
    ...overrides,
  }
}

export function evaluation(overrides: Partial<EvaluationReport> = {}): EvaluationReport {
  return {
    version: 'evaluation/1.0.0',
    generated_at: '2026-10-07T10:00:00Z',
    window_status: 'in_progress',
    window_start: '2026-10-01T00:00:00Z',
    window_end: '2026-10-16T00:00:00Z',
    coverage: [
      {
        source_key: 'synthetic_source',
        window_start: '2026-10-01T00:00:00Z',
        window_end: '2026-10-16T00:00:00Z',
        intervals: [{ source_key: 'synthetic_source', start: '2026-10-01T00:00:00Z', end: '2026-10-07T00:00:00Z', scans: 40 }],
        gaps: [{ subject: 'synthetic_source', start: '2026-10-07T00:00:00Z', end: '2026-10-07T10:00:00Z', reason: 'scan_gap' }],
        healthy_seconds: 518_400,
        coverage_ratio: '0.93525179856',
      },
    ],
    sources_with_healthy_coverage: 1,
    eligible_vehicles: 0,
    well_matched_vehicles: 0,
    small_sample_matches: 0,
    inquiries: {
      attempted: 0,
      accepted: 0,
      uncertain: 0,
      failed_definite: 0,
      suppressed: 0,
      held_for_facts: 0,
      cancelled: 0,
      in_progress: 0,
      with_seller_reply: 0,
      suppression_reasons: [],
    },
    seller_replies: 0,
    auto_replies: 0,
    bounces: 0,
    delivery_notices: 0,
    missing_documents_resolved: 0,
    best_supported_economics: null,
    vehicles_with_incomplete_economics: 0,
    most_common_unknowns: [],
    qualifying_deal_ids: [],
    owner_judgement_candidate_ids: [],
    outcome: 'no_suitable_deal_yet',
    reasons: ['no eligible vehicles were found yet'],
    excluded_synthetic_records: 0,
    optimises_for_volume: false,
    ...overrides,
  }
}
