-- =============================================================================
-- 20261008000100_inquiry_quota_backstop
-- Spec v1.1 section 37.5 (forward-only, expand): the database backstop of the
-- rolling inquiry caps counts every unreleased quota debit at the LATEST moment
-- its message may have been (or may still be) handed over, exactly like the
-- repository (persistence.inquiries_repo._debits, docs/schema.md section 11.5).
--
-- Before this migration ops.inquiry_quota_usage counted a debit at
-- greatest(debited_at, send_attempted_at). On the outlook_local route the
-- hand-over happens when the desktop worker claims the committed send intent,
-- up to the intent TTL after the attempt was committed, and a guarded retry
-- adds a later attempt while send_attempted_at keeps the FIRST attempt. A debit
-- now occupies the rolling windows at the later of:
--   * its reservation (debited_at);
--   * the inquiry's send attempt (send_attempted_at);
--   * the end (finished_at) of every finished attempt of the inquiry;
--   * the current wall clock while an attempt of the inquiry is still running
--     (the worker may hand it over at any moment until the intent expires).
-- So intents committed while the laptop was offline can never leave together
-- with later ones above the configured caps, also when the repository's own
-- check is bypassed (defence in depth under the same controls lock).
--
-- Guard semantics are unchanged: the function is still evaluated on every
-- ledger insert and before every transmission (queued -> sending, excluding the
-- inquiry's own debit), under app.seller_inquiry_controls FOR UPDATE; released
-- debits never count; future-dated values still count. The function now reads
-- clock_timestamp() for running attempts and is therefore declared VOLATILE.
-- Only the function body and its volatility change: its signature, owner,
-- grants (EXECUTE for suv_backend only) and search_path stay as they were.
-- =============================================================================

create or replace function ops.inquiry_quota_usage(
  p_workspace_id uuid, p_exclude_inquiry_id uuid, out count_24h integer, out count_15d integer)
language sql
volatile
set search_path = ''
as $$
  select (pg_catalog.count(*) filter (where u.counted_at > pg_catalog.now() - interval '24 hours'))::integer,
         (pg_catalog.count(*) filter (where u.counted_at > pg_catalog.now() - interval '15 days'))::integer
    from (select greatest(
                   q.debited_at,
                   i.send_attempted_at,
                   (select pg_catalog.max(case when a.outcome = 'running' then pg_catalog.clock_timestamp()
                                               else a.finished_at end)
                      from ops.email_delivery_attempts a
                     where a.workspace_id = i.workspace_id and a.inquiry_id = i.id)) as counted_at
            from ops.inquiry_quota_ledger q
            join app.seller_inquiries i on i.workspace_id = q.workspace_id and i.id = q.inquiry_id
           where q.workspace_id = p_workspace_id
             and q.released_at is null
             and q.inquiry_id is distinct from p_exclude_inquiry_id) as u
$$;
comment on function ops.inquiry_quota_usage(uuid, uuid) is
  'Unreleased quota debits in the rolling 24 h / 15 day windows, each counted at the latest possible hand-over: max(reservation, send attempt, end of every finished attempt, now while an attempt runs).';

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
