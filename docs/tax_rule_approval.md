# Tax rule approval and activation

Spec sections 16 (versioned import-tax engine) and 32 (activation gate "Tax rules").
Engine: `src/suv_deals/domain/tax_engine.py`; file format: `config/tax_rules/README.md`.

## Current status (honest completion state)

| Capability | State |
|---|---|
| Engine, validation, versioning, lifecycle, selection, missing-data handling | `implemented`, `fixture_verified` (synthetic golden/boundary tests) |
| Production North Macedonian rule set | `blocked` - no owner-approved, source-supported rule set exists |

Until an ACTIVE rule set exists, every non-fixture valuation keeps import duty,
motor-vehicle tax, import VAT and other import charges `unknown`; the valuation is
`incomplete` and the listing can only be a research candidate.

**Smallest owner action:** supply (or commission from a qualified customs broker/tax
professional) the current official rules for passenger-car imports into MK, then approve
a rule-set version through the procedure below. The Customs Administration's vehicle-tax
calculator is indicative only and is a cross-check, not a source of binding rates.

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
   jurisdiction/category and period (ambiguous applicability). Record the configuration
   change (`TAX_RULE_SET_ID`, `config_revisions`).
7. **Supersede / expire / revoke** when the law or evidence changes. New valuations then
   select the new version or stay incomplete; existing valuations become stale through
   their dependency fingerprint (tax rule status, approval and validity are part of it).
   Prior calculations keep their exact rule versions for audit.

## Selection rules

`select_rule_set(rule_sets, jurisdiction, category, on_date)` considers only ACTIVE,
non-fixture, hash-verified versions effective on the declaration date. More than one
candidate is an error, never a silent choice. `allow_unapproved=True` exists only for
labelled synthetic fixtures; such results carry their status and are never
production-ready (`TaxCalculation.production_ready` is false and the valuation cannot
notify).

## What the engine will not do

- Execute anything from a rule file (no `eval`, code or SQL).
- Infer the VAT base, a tariff code, origin or an exemption.
- Treat pending/unknown origin proof, a German or Swiss seller, or physical location in
  Europe as preferential origin or zero duty.
- Substitute the invoice price for a missing customs value, or a reference/payment
  exchange rate for the customs rate.
- Report a partial sum as `total_import_cost` (it is labelled `known_subtotal`).
