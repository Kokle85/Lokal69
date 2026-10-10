-- =============================================================================
-- 20261008000200_inquiry_hardening
-- Spec v1.1 section 37 (forward-only, expand): open items of the v1.1 reviews
-- (work package C1). docs/schema.md section 11.9.
--
-- 1. Seller cooldown floor. The owner decision fixes the seller cooldown at AT
--    LEAST 7 days (domain.inquiries.SELLER_COOLDOWN); the owner may widen it, never
--    shorten it. seller_inquiry_controls_cooldown_ck is tightened from 1..365 to
--    7..365 days (name kept; DROP + ADD NOT VALID + VALIDATE). Every existing row
--    is at the 7-day default unless an owner shortened it through the repository
--    before this migration; such a row is raised to the 7-day floor first (the
--    stricter value, version + 1 as the controls guard requires, reason recorded
--    in update_reason), so VALIDATE cannot fail.
--
-- 2. Seller-reply signal flood control. app.seller_replies.signal_status records,
--    at ingest and never afterwards, what happened to the reply's minimal
--    seller.reply.received signal: 'emitted' (an outbox signal was written),
--    'coalesced' (an undelivered signal of the same inquiry was still pending: dot
--    reads every reply of the inquiry through MCP), 'rate_limited' (the
--    per-inquiry cap of signal-emitting replies per rolling 24 hours was reached)
--    or 'not_applicable' (no signal for this reply: quarantined, no status, or
--    signals disabled). NULL for replies stored before this migration. The reply
--    itself is always stored and visible. A partial expression index serves the
--    "pending signal of this inquiry" lookup on ops.outbox.
--
-- 3. Owner-controlled activation canaries (spec 37.10 activation evidence;
--    docs/schema.md 10.10). ops.inquiry_activation_canaries is NOT an inquiry:
--    it has no listing, seller or quota debit (ops.inquiry_quota_ledger references
--    app.seller_inquiries only), so a canary can never count against the caps or
--    as one of the 15-day deals. It stores the bound sender (binding id + version
--    + provider, optional desktop mailbox), the SHA-256 of the owner-controlled
--    target address (never the address), the canary's own Message-ID, the
--    provider/worker outcome and a correlated test reply. Identity is frozen
--    (SV004), the states move only along prepared -> accepted | uncertain |
--    failed | cancelled, uncertain -> accepted | failed | reply_correlated,
--    accepted -> reply_correlated (SV002), every change advances version by one
--    (SV005), no delete (SV001). RLS + tenant_isolation through the security
--    baseline; suv_backend gets SELECT, INSERT and a column UPDATE of the
--    lifecycle columns only.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Seller cooldown floor (7..365 days)
-- -----------------------------------------------------------------------------

update app.seller_inquiry_controls
   set seller_cooldown = interval '7 days',
       version = version + 1,
       update_reason = 'migration 20261008000200: seller cooldown raised to the 7-day floor'
 where seller_cooldown < interval '7 days';

alter table app.seller_inquiry_controls drop constraint if exists seller_inquiry_controls_cooldown_ck;
alter table app.seller_inquiry_controls add constraint seller_inquiry_controls_cooldown_ck check (
  seller_cooldown >= interval '7 days' and seller_cooldown <= interval '365 days') not valid;
alter table app.seller_inquiry_controls validate constraint seller_inquiry_controls_cooldown_ck;

comment on column app.seller_inquiry_controls.seller_cooldown is
  'Seller cooldown (domain.inquiries.SELLER_COOLDOWN is the 7-day floor): 7..365 days; the owner may widen it, never shorten it.';

-- -----------------------------------------------------------------------------
-- 2. Seller-reply signal status (flood control)
-- -----------------------------------------------------------------------------

alter table app.seller_replies add column if not exists signal_status text;
alter table app.seller_replies drop constraint if exists seller_replies_signal_status_ck;
alter table app.seller_replies add constraint seller_replies_signal_status_ck check (
  signal_status is null
  or signal_status in ('emitted', 'coalesced', 'rate_limited', 'not_applicable'));

comment on column app.seller_replies.signal_status is
  'What happened to the reply''s seller.reply.received signal at ingest: emitted, coalesced (an undelivered signal of the inquiry was pending), rate_limited (per-inquiry 24 h cap) or not_applicable. Set once at insert (no UPDATE grant).';

create index if not exists seller_replies_signal_window_idx
  on app.seller_replies (workspace_id, inquiry_id, ingested_at)
  where signal_status = 'emitted';

create index if not exists outbox_reply_signal_inquiry_idx
  on ops.outbox (workspace_id, (payload ->> 'inquiry_id'))
  where event_type = 'seller.reply.received';

-- -----------------------------------------------------------------------------
-- 3. Owner-controlled activation canaries
-- -----------------------------------------------------------------------------

create table if not exists ops.inquiry_activation_canaries (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references app.workspaces (id),
  sender_binding_id uuid not null,
  sender_binding_version bigint not null,
  provider text not null,
  mailbox_binding_id uuid,
  target_address_hash text not null,
  rfc_message_id text not null,
  purpose text not null,
  state text not null default 'prepared',
  outcome_evidence jsonb not null default '{}'::jsonb,
  outcome_recorded_at timestamptz,
  accepted_at timestamptz,
  reply_message_id text,
  reply_received_at timestamptz,
  reply_recorded_at timestamptz,
  reply_evidence jsonb not null default '{}'::jsonb,
  created_by uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  version bigint not null default 1,
  constraint inquiry_activation_canaries_workspace_id_uk unique (workspace_id, id),
  constraint inquiry_activation_canaries_message_uk unique (workspace_id, rfc_message_id),
  constraint inquiry_activation_canaries_sender_fk foreign key (workspace_id, sender_binding_id)
    references ops.email_sender_bindings (workspace_id, id),
  constraint inquiry_activation_canaries_mailbox_fk foreign key (workspace_id, mailbox_binding_id)
    references ops.mail_worker_bindings (workspace_id, id),
  constraint inquiry_activation_canaries_provider_ck check (
    provider in ('outlook_local', 'gmail_api', 'microsoft_graph')),
  constraint inquiry_activation_canaries_outlook_mailbox_ck check (
    provider <> 'outlook_local' or mailbox_binding_id is not null),
  constraint inquiry_activation_canaries_sender_version_ck check (sender_binding_version > 0),
  constraint inquiry_activation_canaries_target_hash_ck check (target_address_hash ~ '^[0-9a-f]{64}$'),
  constraint inquiry_activation_canaries_message_id_ck check (app.rfc_message_id_ok(rfc_message_id)),
  constraint inquiry_activation_canaries_reply_message_id_ck check (
    reply_message_id is null or app.rfc_message_id_ok(reply_message_id)),
  constraint inquiry_activation_canaries_purpose_ck check (
    pg_catalog.length(purpose) between 3 and 500 and purpose !~ '[[:cntrl:]]'),
  constraint inquiry_activation_canaries_state_ck check (
    state in ('prepared', 'accepted', 'uncertain', 'failed', 'reply_correlated', 'cancelled')),
  constraint inquiry_activation_canaries_outcome_ck check (
    (state in ('prepared', 'cancelled')) = (outcome_recorded_at is null)),
  constraint inquiry_activation_canaries_accepted_ck check (
    state <> 'accepted' or accepted_at is not null),
  constraint inquiry_activation_canaries_reply_ck check (
    (state = 'reply_correlated')
    = (reply_message_id is not null and reply_received_at is not null and reply_recorded_at is not null)),
  constraint inquiry_activation_canaries_outcome_evidence_ck check (
    pg_catalog.jsonb_typeof(outcome_evidence) = 'object'
    and pg_catalog.octet_length(outcome_evidence::text) <= 8192),
  constraint inquiry_activation_canaries_reply_evidence_ck check (
    pg_catalog.jsonb_typeof(reply_evidence) = 'object'
    and pg_catalog.octet_length(reply_evidence::text) <= 8192),
  constraint inquiry_activation_canaries_version_ck check (version > 0)
);

comment on table ops.inquiry_activation_canaries is
  'Owner-controlled activation canaries (spec 37.10): a wiring test message to an owner-controlled address. Never a seller inquiry, never a quota debit, never part of the 15-day evaluation. Stores the target address hash only.';

create index if not exists inquiry_activation_canaries_recent_idx
  on ops.inquiry_activation_canaries (workspace_id, created_at desc, id);
create index if not exists inquiry_activation_canaries_sender_idx
  on ops.inquiry_activation_canaries (workspace_id, sender_binding_id);
create index if not exists inquiry_activation_canaries_mailbox_idx
  on ops.inquiry_activation_canaries (workspace_id, mailbox_binding_id)
  where mailbox_binding_id is not null;

create or replace function ops.inquiry_activation_canaries_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_binding record;
  v_mailbox record;
begin
  if tg_op = 'INSERT' then
    if new.state <> 'prepared' or new.version <> 1 then
      raise exception using errcode = 'SV002', message = 'an activation canary starts prepared';
    end if;
    select b.version, b.provider, b.revoked_at, b.verified_at into v_binding
      from ops.email_sender_bindings b
     where b.workspace_id = new.workspace_id and b.id = new.sender_binding_id;
    if v_binding.version is distinct from new.sender_binding_version
       or v_binding.provider is distinct from new.provider
       or v_binding.revoked_at is not null or v_binding.verified_at is null then
      raise exception using
        errcode = 'SV003',
        message = 'an activation canary binds the current version of a verified, unrevoked sender';
    end if;
    if new.mailbox_binding_id is not null then
      select m.state, m.sender_binding_id into v_mailbox
        from ops.mail_worker_bindings m
       where m.workspace_id = new.workspace_id and m.id = new.mailbox_binding_id;
      if v_mailbox.state is distinct from 'active'
         or v_mailbox.sender_binding_id is distinct from new.sender_binding_id then
        raise exception using
          errcode = 'SV003',
          message = 'an activation canary names the active mailbox worker of its sender';
      end if;
    end if;
    return new;
  end if;
  if (new.workspace_id, new.id, new.sender_binding_id, new.sender_binding_version, new.provider,
      new.target_address_hash, new.rfc_message_id, new.purpose, new.created_by, new.created_at)
     is distinct from
     (old.workspace_id, old.id, old.sender_binding_id, old.sender_binding_version, old.provider,
      old.target_address_hash, old.rfc_message_id, old.purpose, old.created_by, old.created_at)
     or new.mailbox_binding_id is distinct from old.mailbox_binding_id then
    raise exception using errcode = 'SV004', message = 'activation canary identity is immutable';
  end if;
  if new.state is distinct from old.state and not (
       (old.state = 'prepared' and new.state in ('accepted', 'uncertain', 'failed', 'cancelled'))
       or (old.state = 'uncertain' and new.state in ('accepted', 'failed', 'reply_correlated'))
       or (old.state = 'accepted' and new.state = 'reply_correlated')) then
    raise exception using
      errcode = 'SV002',
      message = pg_catalog.format('activation canary state cannot change from %s to %s', old.state, new.state);
  end if;
  if old.state in ('failed', 'reply_correlated', 'cancelled')
     and (pg_catalog.to_jsonb(new) - array['updated_at']) is distinct from (pg_catalog.to_jsonb(old) - array['updated_at']) then
    raise exception using errcode = 'SV004', message = 'a finished activation canary is frozen';
  end if;
  if (pg_catalog.to_jsonb(new) - array['updated_at', 'version'])
     is distinct from (pg_catalog.to_jsonb(old) - array['updated_at', 'version'])
     and new.version <> old.version + 1 then
    raise exception using errcode = 'SV005', message = 'an activation canary change advances its version by one';
  end if;
  if new.version < old.version then
    raise exception using errcode = 'SV005', message = 'activation canary version must not decrease';
  end if;
  return new;
end
$$;

comment on function ops.inquiry_activation_canaries_guard() is
  'Activation canary guard: bound to a verified, unrevoked sender (current version) and its active mailbox; frozen identity; legal state steps only; finished canaries frozen; version advances by one.';

create or replace trigger inquiry_activation_canaries_guard
  before insert or update on ops.inquiry_activation_canaries
  for each row execute function ops.inquiry_activation_canaries_guard();
create or replace trigger inquiry_activation_canaries_touch
  before update on ops.inquiry_activation_canaries
  for each row execute function app.touch_updated_at();
create or replace trigger inquiry_activation_canaries_no_delete
  before delete on ops.inquiry_activation_canaries
  for each row execute function app.reject_history_mutation();

-- --- security baseline (RLS + tenant_isolation for the new table), then grants ----------------
call ops.apply_security_baseline();

grant select, insert,
  update (state, outcome_evidence, outcome_recorded_at, accepted_at, reply_message_id, reply_received_at,
          reply_recorded_at, reply_evidence, version)
  on table ops.inquiry_activation_canaries to suv_backend;

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
