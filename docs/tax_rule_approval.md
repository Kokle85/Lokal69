# Tax rule approval and activation

Spec sections 16 (versioned import-tax engine) and 32 (activation gate "Tax rules").
Engine: `src/suv_deals/domain/tax_engine.py`; file format: `config/tax_rules/README.md`.

## Current status (honest completion state)

| Capability | State |
|---|---|
| Engine, validation, versioning, lifecycle, selection, missing-data handling | `implemented`, `fixture_verified` (synthetic golden/boundary tests) |
| North Macedonian draft rule set | `draft` - `config/tax_rules/drafts/mk_passenger_car_import_2026.10-draft.2.json`, golden-tested (`tests/unit/test_mk_rule_draft.py`), **not approved** |
| Production North Macedonian rule set | `blocked` - no owner-approved, ACTIVE rule set exists |
| Import-cost calculator (`domain/import_estimate.py`; read service `persistence/queries/import_costs.py`, view `views/import_costs.py`) | indicative preview on the draft only; never production-ready, never used for valuations or alerts |

Until an ACTIVE rule set exists, every non-fixture valuation keeps import duty,
motor-vehicle tax, import VAT and other import charges `unknown`; the valuation is
`incomplete` and the listing can only be a research candidate. The draft changes nothing
here: production selection never picks a draft.

**Smallest owner action:** have the draft reviewed (with a qualified customs broker/tax
professional) against the evidence pack
[docs/tax/mk_import_fact_sheet_2026-10.md](tax/mk_import_fact_sheet_2026-10.md): resolve every
`UNCERTAIN - confirm` marker (listed in
[ADR 0003](decisions/0003-transport-and-customs-calculator.md), "What the owner must confirm"),
re-fetch and re-hash every source, run the golden cases, then approve and activate a rule-set
version through the procedure below. The Customs Administration's vehicle-tax calculator is indicative only and
is a cross-check, not a source of binding rates.

## Lifecycle

```text
draft -> under_review -> approved -> active -> superseded | expired | revoked
under_review -> draft            (rework)
draft | under_review | approved | active -> revoked
unapproved -> revoked            (examples / fixtures)
```

The same transitions are enforced in code (`transition_rule_set`) and in PostgreSQL
(`app.tax_rule_sets_guard`). Content is frozen once a version leaves `draft`; a change is
a new version. Approval never activates automatically.

## Procedure

1. **Collect evidence.** For every legal source: official HTTPS URL, title, retrieval
   time and the SHA-256 of the retrieved copy (store the copy in private evidence
   storage). Check historical PDFs for amendments and current applicability.
2. **Author a draft** (`status: draft`). Declare every input the rule needs
   (`required_inputs` / `optional_inputs`) with `input_units` for numeric inputs. Write
   each component explicitly: taxable bases are listed term by term (state whether VAT
   includes duty and vehicle tax by listing them), CO2 tables per measurement cycle,
   origin/preference alternatives with explicit country lists, explicit rounding for
   every component and the total, effective dates (`valid_from`, exclusive `valid_to`)
   and the vehicle categories it covers. Never encode a WLTP<->NEDC conversion.
3. **Validate.** `parse_rule_set_json` must succeed (no problems), then compute the
   content hash with `compute_rule_set_sha256`.
4. **Review** (`draft -> under_review`). The reviewer checks every component against the
   sources, runs golden cases (at least every bracket boundary, each CO2 cycle, origin
   with and without accepted proof, customs value different from invoice price, rounding
   order) and records a `review_record`: reviewer, time, scope of what was verified,
   optional professional review reference, evidence references and the
   `content_sha256` that was reviewed.
5. **Approve** (`under_review -> approved`) with `transition_rule_set(...,
   approved_by=<named owner>, review_record=...)`. The engine refuses approval unless the
   review binds the current content hash, sources exist and the rule set validates.
   Fixture rule sets can never be approved.
6. **Activate** (`approved -> active`) per jurisdiction and vehicle category. Activation
   re-verifies the rule set and refuses overlap with another ACTIVE version of the same
   jurisdiction/category and period (ambiguous applicability). Record the stored rule set
   id, version and content hash (`suv-deals tax-rules list`); `suv-deals doctor` then reports
   the ACTIVE set valuations select per workspace (`tax_rules/production`).
7. **Supersede / expire / revoke** when the law or evidence changes. New valuations then
   select the new version or stay incomplete; existing valuations become stale through
   their dependency fingerprint (tax rule status, approval and validity are part of it).
   Prior calculations keep their exact rule versions for audit.

**Where the owner does steps 4-7.** The dashboard's Settings screen has an owner-only "Tax rules
and cost profile" section (`config:admin`; API `/api/settings/tax-rules*`, handler
`api.owner_config_routes`, docs/api_contract.md 10.2). It lists the stored versions and the
repository drafts with their content hash, sources, components and every `UNCERTAIN` item, and
offers only the acts the lifecycle permits: **Store draft** (a file NAME under
`config/tax_rules/drafts`, never a path or an upload), **Submit for review** (seals the hash),
**Approve** (the approver's name and the review record of step 4, bound to the content hash; the
whole approval record must fit in 500 characters), **Activate** and **Revoke**. Each act names the
status and content hash the owner saw (a changed status is refused with `status_changed`, a
different hash with `422`), states its effect on production valuations and needs an explicit
confirmation; each is audited and idempotent. The same section approves the configured cost
profile (transport EUR 700 per vehicle). Nothing is approved or activated automatically.

**From the command line** (docs/runbook.md, CLI table):

| Step | Command |
|---|---|
| 3 Validate the drafts | `uv run suv-deals tax-rules validate config/tax_rules/drafts` (also part of `make smoke-local`) |
| See what is stored, what ships as a draft and what the calculator uses today | `uv run suv-deals tax-rules list [--jurisdiction MK] [--json]` (read-only) |
| Store a draft in the workspace | `uv run suv-deals tax-rules store config/tax_rules/drafts/<file>.json --owner-user-id <owner> --yes` (status `draft`, audited as that owner; an identical stored version is left alone, a different one under the same id and version is refused) |
| Record the month's customs rate | `uv run suv-deals fx record --base EUR --quote MKD --rate <rate> --date YYYY-MM-01 --purpose customs --provider <list> --source-ref <ref> --reason "<why>" --yes` |
| Check a calculation | `uv run suv-deals import-costs estimate --listing-id <id>` (or vehicle facts); mode `indicative_preview` before activation, `active` after |

Review, approval, activation and revocation have no CLI command: they are owner acts in the
dashboard section above.

## Selection rules

`select_rule_set(rule_sets, jurisdiction, category, on_date)` considers only ACTIVE,
non-fixture, hash-verified versions effective on the declaration date. More than one
candidate is an error, never a silent choice. `allow_unapproved=True` exists only for
labelled synthetic fixtures; such results carry their status and are never
production-ready (`TaxCalculation.production_ready` is false and the valuation cannot
notify).

`select_preview_rule_set` serves only the import-cost calculator when no ACTIVE set
applies: it picks an approved-but-inactive, under-review or draft set (never a fixture,
example or closed set) for an **indicative preview**. The preview carries a Macedonian and
English banner, names its totals `indicative_total`, is never production-ready and is
never persisted to valuations, cost lines or alerts (`costs.tax_cost_lines` refuses an
estimate). The valuation pipeline never calls it.

The calculator's read service (`persistence/queries/import_costs.py`, scope `deals:read`,
persists nothing) runs the production selection over the STORED rule sets first (mode
`active`); only without an ACTIVE set does it preview the stored approved / under-review /
draft versions together with the DRAFT files in `config/tax_rules/drafts` (a file is never a
production candidate, a non-draft file is ignored, and a file and a stored row of one version
with different content leave the result `unavailable`). The customs rate is the latest
recorded EUR/MKD `customs` rate for the declaration month; otherwise a caller-supplied rate
for that estimate only (assumption `CALLER_SUPPLIED_CUSTOMS_RATE`, never stored); otherwise the
customs value in MKD stays unknown and the warning names
`suv-deals fx record --base EUR --quote MKD ... --purpose customs`.

### Valuation inputs

Valuations and the calculator build the engine inputs with the same builder
(`import_estimate.build_tax_inputs` over `facts_from_listing`), so once a rule set is ACTIVE a
valuation computes the duty, motor-vehicle tax and import VAT the calculator shows: the
advertised payable amount as the transaction value (assumption), the cost profile's transport
line in the customs value, no proof of origin (standard duty), a listing CO2 figure as
`listing_claim_unverified`, the customs monthly rate valid in its month. Lines computed from
such claims (`BuiltTaxInputs.unverified_inputs`) are estimates on unapproved assumptions,
never `rule_supported`, so a valuation built on them is never `quote_supported`. The builder's
assumptions and warnings and the Euro import gate are recorded as valuation warnings
(`TAX_INPUT_ASSUMPTION:`, `TAX_INPUT_WARNING:`, `TAX_INPUT_UNVERIFIED:`, `IMPORT_GATE:`).
Helper components are flagged `intermediate` in the valuation's tax view and never counted.

## What the engine will not do

- Execute anything from a rule file (no `eval`, code or SQL).
- Infer the VAT base, a tariff code, origin or an exemption.
- Treat pending/unknown origin proof, a German or Swiss seller, or physical location in
  Europe as preferential origin or zero duty.
- Substitute the invoice price for a missing customs value, or a reference/payment
  exchange rate for the customs rate.
- Report a partial sum as `total_import_cost` (it is labelled `known_subtotal`).
