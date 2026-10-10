-- =============================================================================
-- 20261007000100_v11_integration_foundation
-- Spec v1.1 section 37 integration foundation (forward-only, expand):
--
--   1. Enum mirrors (CHECKs that list domain.enums values) gain the v1.1 runtime values:
--      * ops.jobs.job_type (JobType): seller_inquiry_plan, seller_inquiry_send,
--        seller_inquiry_reconcile, seller_reply_process;
--      * suppression reasons (SuppressionReason) on app.seller_inquiries and
--        ops.email_suppressions: authorization_revoked (a revoked standing
--        authorization is no longer recorded as the kill switch);
--      * app.availability_events.evidence_kind (AvailabilityEvidenceKind):
--        source_reserved_badge (-> reserved) and source_detail_not_found (-> unknown,
--        never removed or sold), replacing source_observation + a reason label.
--      Every constraint keeps its name; the swap is DROP + ADD ... NOT VALID +
--      VALIDATE (existing rows satisfy the widened checks).
--   2. Two read-path indexes, verified with EXPLAIN on a seeded database
--      (tests/integration/db/test_v11_integration_foundation.py):
--      * app.listings (workspace_id, created_at desc, id desc) for the candidate
--        list keyset (persistence.queries.candidates: ORDER BY l.created_at DESC,
--        l.id DESC LIMIT n) - without it every page scanned and sorted the whole
--        workspace and evaluated its per-row lateral joins;
--      * ops.outbox (workspace_id, event_created_at, id) for the dashboard outbox
--        attention list (persistence.queries.operations: uncertain, blocked,
--        dead_letter and retry_wait ordered by event_created_at, id); the existing
--        outbox_attention_idx excludes retry_wait, so that query used a sequential scan.
--
-- No table, column, grant or policy is added or removed; ops.apply_security_baseline()
-- runs again and the backend role safety check is re-verified.
-- =============================================================================

-- --- 1a. ops.jobs.job_type mirrors domain.enums.JobType ------------------------------------
alter table ops.jobs drop constraint if exists jobs_type_ck;
alter table ops.jobs
  add constraint jobs_type_ck check (job_type in (
    'discovery', 'detail', 'recheck', 'valuation', 'comparables', 'stale_sweep', 'reprocess',
    'seller_inquiry_plan', 'seller_inquiry_send', 'seller_inquiry_reconcile', 'seller_reply_process'))
  not valid;
alter table ops.jobs validate constraint jobs_type_ck;

-- --- 1b. suppression reasons mirror domain.enums.SuppressionReason --------------------------
alter table app.seller_inquiries drop constraint if exists seller_inquiries_suppression_ck;
alter table app.seller_inquiries
  add constraint seller_inquiries_suppression_ck check (
    (state = 'suppressed') = (suppression_reason is not null)
    and (suppression_reason is null or suppression_reason in (
      'hard_bounce', 'complaint', 'seller_opt_out', 'source_paused', 'sender_revoked',
      'unresolved_send_outcome', 'kill_switch', 'contradictory_availability', 'manual',
      'authorization_revoked')))
  not valid;
alter table app.seller_inquiries validate constraint seller_inquiries_suppression_ck;

alter table ops.email_suppressions drop constraint if exists email_suppressions_reason_ck;
alter table ops.email_suppressions
  add constraint email_suppressions_reason_ck check (reason in (
    'hard_bounce', 'complaint', 'seller_opt_out', 'source_paused', 'sender_revoked',
    'unresolved_send_outcome', 'kill_switch', 'contradictory_availability', 'manual',
    'authorization_revoked'))
  not valid;
alter table ops.email_suppressions validate constraint email_suppressions_reason_ck;

-- --- 1c. availability evidence mirrors domain.enums.AvailabilityEvidenceKind ----------------
alter table app.availability_events drop constraint if exists availability_events_evidence_kind_ck;
alter table app.availability_events
  add constraint availability_events_evidence_kind_ck check (evidence_kind in (
    'source_observation', 'source_sold_badge', 'source_removed_page', 'source_reserved_badge',
    'source_detail_not_found', 'seller_reported_sold', 'seller_reported_available',
    'seller_reported_reserved', 'complete_scan_absence', 'manual'))
  not valid;
alter table app.availability_events validate constraint availability_events_evidence_kind_ck;

-- Evidence determines the canonical value (spec 37.9): a reserved badge is "reserved"; a
-- missing detail page is "unknown" (never removed or sold); absence is never a sale.
alter table app.availability_events drop constraint if exists availability_events_mapping_ck;
alter table app.availability_events
  add constraint availability_events_mapping_ck check (
    case evidence_kind
      when 'source_observation' then new_availability in ('available', 'reserved', 'unknown')
      when 'source_sold_badge' then new_availability = 'sold_claimed'
      when 'source_removed_page' then new_availability = 'removed'
      when 'source_reserved_badge' then new_availability = 'reserved'
      when 'source_detail_not_found' then new_availability = 'unknown'
      when 'seller_reported_sold' then new_availability = 'sold_claimed'
      when 'seller_reported_available' then new_availability = 'available'
      when 'seller_reported_reserved' then new_availability = 'reserved'
      when 'complete_scan_absence' then new_availability = 'unknown'
      else true
    end)
  not valid;
alter table app.availability_events validate constraint availability_events_mapping_ck;

-- --- 2. read-path indexes (EXPLAIN-verified) ------------------------------------------------
create index if not exists listings_created_idx
  on app.listings (workspace_id, created_at desc, id desc);

create index if not exists outbox_attention_created_idx
  on ops.outbox (workspace_id, event_created_at, id)
  where state in ('uncertain', 'blocked', 'dead_letter', 'retry_wait');

-- --- security baseline (unchanged model; re-applied by every migration) --------------------
call ops.apply_security_baseline();

-- Fail closed if the cluster-global backend role became unsafe (migration 0100).
do $verify_role$
declare
  problems text[] := ops.backend_role_problems('suv_backend');
begin
  if pg_catalog.cardinality(problems) > 0 then
    raise exception using
      errcode = 'insufficient_privilege',
      message = 'role suv_backend is unsafe for tenant isolation: '
             || pg_catalog.array_to_string(problems, ', ');
  end if;
end
$verify_role$;
