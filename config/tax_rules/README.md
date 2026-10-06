# Import-tax rule sets (`config/tax_rules/`)

Spec section 16. Engine: `src/suv_deals/domain/tax_engine.py`. Approval procedure:
`docs/tax_rule_approval.md`.

**No real tax rule set ships with this repository.** `example_unapproved.json` has the
exact spec section 16 shape with no components and status `unapproved`; it can never be
selected for production and it calculates nothing. There are no North Macedonian rates,
tariffs, coefficients or VAT percentages anywhere in `src/` or `config/`. Until an
owner-approved rule set is ACTIVE, every valuation keeps import costs `unknown` and is
`incomplete` (a research candidate, never a quantified opportunity).

## File format

One JSON object per rule-set version. JSON numbers are parsed as exact decimals (never
binary floats); quoting decimals as strings (`"0.20"`) is recommended. Duplicate keys
are rejected.

| Field | Meaning |
|---|---|
| `rule_set_id`, `version` | Identity. A changed rule is a new version, never an edit of an approved one. |
| `jurisdiction` | ISO country code, e.g. `MK`. |
| `status` | `draft`, `under_review`, `approved`, `active`, `superseded`, `expired`, `revoked`, `unapproved` (examples/fixtures). |
| `valid_from`, `valid_to` | Effective period by declaration date: `valid_from <= date < valid_to`. |
| `currency` | Currency of every component amount (e.g. `MKD`). |
| `vehicle_categories` | Categories this version applies to (e.g. `passenger_car`). |
| `sources` | `[{url (https), title, retrieved_at, sha256 of the retrieved copy}]`. |
| `approved_by`, `approved_at`, `review_record` | Named approval and the review that binds the content hash. |
| `required_inputs`, `optional_inputs` | Input names the rule uses (closed vocabulary below). A missing required input makes the result incomplete. |
| `input_units` | Unit declaration for every numeric input used (must equal the engine unit). |
| `components` | Ordered component list (below). |
| `rounding_rules` | `{component_default, total}`; each `{quantum, mode, stage}`. |
| `missing_input_behavior` | Always `return_incomplete`. |
| `sha256` | Canonical content hash (see Hashing). |
| `is_fixture` | `true` only for synthetic test rule sets (never approvable). |

### Input vocabulary

`declaration_date` (date); `jurisdiction`, `classification` (approved tariff code only),
`vehicle_category`, `vehicle_condition` (`new`/`used`), `seller_country`,
`dispatch_country`, `origin_country`, `origin_evidence`
(`accepted`/`rejected`/`not_available`/`invalid_period`), `preferential_origin_country`
(country from an accepted preferential proof, or `none`), `customs_value_basis`,
`co2_cycle`, `co2_source_document`, `emissions_class`, `fuel`, `importer_status`,
`exemption_status:<code>` (`accepted`/`rejected`/`not_claimed`) (text);
`vehicle_age_years` (years), `co2_g_km` (g/km), `engine_displacement_cm3` (cm3),
`power_kw` (kW) (numbers); `invoice_price`, `customs_value`, `included_costs_total`
(money); `customs_fx_rate` (customs-purpose FX rate).

Seller and dispatch countries never imply origin. A German purchase is not EU
preferential origin; Switzerland is never grouped with EU states (rules list countries
explicitly); pending/unknown proof is unknown, not "no duty".

### Components (restricted declarative language)

Nothing in a rule file is executed. Each component has `id`, `kind`, `label`,
`category` (`import_duty` | `motor_vehicle_tax` | `import_vat` |
`other_import_charges`), `currency`, optional `depends_on`, `applies_when`,
`alternative_group`, `rounding`, `legal_reference`.

| `kind` | Fields | Amount |
|---|---|---|
| `percentage` | `rate` (fraction, 0.05 = 5 %), `base` (list of money inputs and earlier component ids) | `rate x sum(base)` |
| `fixed` | `amount` | `amount` |
| `bracket` | `input`, `tables` (CO2 only, keyed by `nedc` / `nedc_correlated` / `wltp`) or `brackets`, optional `base`, `contiguous` | first bracket with `lower_inclusive <= value < upper_exclusive` gives `amount`, `rate_of_base x sum(base)` or `amount_per_unit_above_lower x (value - lower)` |
| `per_unit` | `input`, `amount_per_unit`, optional `threshold` | `amount_per_unit x max(0, value - threshold)` |

- `applies_when`: list of `{input, op, value}` with `op` in `eq`, `ne`, `in`, `not_in`
  (text), `lt`, `le`, `gt`, `ge` (numbers/dates). All must hold. A predicate on a missing
  input makes the component unknown; a false predicate makes it not applicable.
- `alternative_group`: members are alternatives (e.g. preferential vs standard duty);
  exactly one must apply, otherwise every member is unknown (no silent coverage gap).
- The taxable base is exactly the listed `base` terms; the engine never infers what VAT
  includes. A not-applicable dependency contributes nothing; an unknown one makes the
  dependant unknown.
- CO2: a missing or unsupported cycle makes the component unknown. WLTP and NEDC values
  are never converted into each other.
- Money in another currency is converted only with a customs-purpose rate for exactly
  that pair, never with a rate observed after the declaration date. A rule naming
  `customs_value` never falls back to the invoice price.
- Rounding `stage`: `before_dependents` (rounded value feeds later components) or
  `reported_only` (later components use the unrounded value). Totals sum reported
  amounts, then apply `rounding_rules.total`.

### Validation (`validate_rule_set`)

Unique ids; ids never shadow inputs; references only to declared inputs and earlier
components (listed in `depends_on`); unit declarations; brackets sorted,
non-overlapping, contiguous when declared, only the last open-ended; non-negative
amounts/rates; rates at most 1; matching component currency; per-cycle CO2 tables need
`co2_cycle` declared as an input; predicate values are text, numbers or ISO dates (never
booleans); JSON `NaN`/`Infinity` are refused; approved/active/superseded/expired versions
need sources, approver, approval time, review record, valid_from, currency, categories,
components, explicit rounding and a matching `sha256`. Fixture rule sets can only be
`draft`, `unapproved` or `revoked` and never enter review.

A calculation is refused when the inputs name another jurisdiction, or an approved
classification names a vehicle category the rule set does not cover. Money inputs above
the engine's sanity bound are rejected as input errors.

### Turning a calculation into cost lines

`costs.tax_cost_lines` makes one line per import category. A category the rule set
defines no component for is `not_applicable` only when the calculation is complete;
otherwise it is `unknown` (never zero). Every line carries the rule identity and the
`tax_calculation:<sha256>` of the exact calculation, which the valuation checks.

### Hashing

`sha256` = SHA-256 of the canonical JSON (sorted keys, compact separators, decimals as
strings) of every field except `sha256`, `status`, `approved_by`, `approved_at` and
`review_record`. Lifecycle moves therefore keep the hash; any content change breaks it
and loading fails. Compute it with `tax_engine.compute_rule_set_sha256` /
`seal_rule_set`.
