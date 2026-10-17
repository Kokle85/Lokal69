-- =============================================================================
-- 20261013000100_import_values
-- Owner decision 2026-10-13 (ADR 0003 decision 13, "two separate values"): each
-- vehicle keeps its REAL purchase price (profit and landed cost) apart from its
-- ESTIMATED customs value (import charges; e.g. the SchwackeNet catalogue value
-- Customs uses for used vehicles, while Customs Law art. 28 keeps the price
-- actually paid as the primary method).
--
-- Forward-only, expand. The owner records both per listing in the append-only
-- app.cost_evidence (persistence.import_values_repo):
--   * category 'purchase' (already allowed): the real purchase price, kind
--     actual / quote / estimate;
--   * category 'customs_value' (NEW): the estimated customs value of the vehicle
--     in EUR, without transport, always kind 'estimate'. It is not a cost
--     category: domain.enums.CostCategory, the cost profiles and their term
--     mapping stay unchanged.
-- A new value supersedes the listing's previous row of the same category
-- (supersedes_id); rows are never updated or deleted (cost_evidence_append_only).
--
-- 1. cost_evidence_category_ck accepts 'customs_value' (DROP + ADD of the same
--    name; a widening, so every existing row still satisfies it).
-- 2. cost_evidence_customs_value_ck: a customs_value row belongs to a listing,
--    is in EUR and is an estimate.
-- 3. Partial unique indexes keep each listing's recorded import values one
--    linear chain per category (one root, each row superseded at most once), so
--    exactly one row is current. The repository serialises recorders on the
--    listing row lock; these indexes are the backstop (a duplicate is SQLSTATE
--    23505 -> VersionConflict). Other categories are not affected.
--
-- Pure ASCII, no DROP TRIGGER (tests/unit/test_migrations_ascii.py).
-- =============================================================================

alter table app.cost_evidence drop constraint cost_evidence_category_ck;
alter table app.cost_evidence add constraint cost_evidence_category_ck check (category in (
  'purchase', 'bank_fx_charges', 'travel_inspection', 'transport', 'export_plates_insurance',
  'customs_broker', 'import_duty', 'motor_vehicle_tax', 'import_vat', 'other_import_charges',
  'homologation_registration', 'repairs', 'preparation', 'risk_reserve', 'storage_holding',
  'selling_costs', 'refundable_deposit', 'customs_value'));

alter table app.cost_evidence add constraint cost_evidence_customs_value_ck check (
  category <> 'customs_value'
  or (listing_id is not null and currency = 'EUR' and kind = 'estimate'));

comment on constraint cost_evidence_category_ck on app.cost_evidence is
  'CostCategory values plus customs_value (an estimated customs value recorded per listing; not a cost category).';
comment on constraint cost_evidence_customs_value_ck on app.cost_evidence is
  'An estimated customs value belongs to a listing, is in EUR and is an estimate (owner decision 2026-10-13).';

create unique index cost_evidence_import_value_root_uk
  on app.cost_evidence (workspace_id, listing_id, category)
  where supersedes_id is null and category in ('purchase', 'customs_value');
create unique index cost_evidence_import_value_chain_uk
  on app.cost_evidence (workspace_id, supersedes_id)
  where supersedes_id is not null and category in ('purchase', 'customs_value');

comment on index app.cost_evidence_import_value_root_uk is
  'Recorded import values (purchase, customs_value): one chain root per listing and category.';
comment on index app.cost_evidence_import_value_chain_uk is
  'Recorded import values (purchase, customs_value): each row is superseded at most once (linear chain).';

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
