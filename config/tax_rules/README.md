# Import-tax rule sets (`config/tax_rules/`)

Spec section 16. Engine: `src/suv_deals/domain/tax_engine.py`. Approval procedure:
`docs/tax_rule_approval.md`.

**A DRAFT ships under `config/tax_rules/drafts/`; nothing is approved or active.**

- `example_unapproved.json` (top level) has the exact spec section 16 shape with no
  components and status `unapproved`; it can never be selected and calculates nothing.
  The top level holds no rule set with rates (a test enforces it).
- `drafts/mk_passenger_car_import_2026.10-draft.3.json` is the North Macedonian draft
  (customs duty, motor-vehicle tax and import VAT on an HS 8703 passenger car) built from
  the research fact sheet of 2026-10-13. Status `draft`, no approver, no sealed hash;
  every point still to confirm is marked `UNCERTAIN - confirm`. Production selection
  (`select_rule_set`) never selects it. The import-cost calculator
  (`domain/import_estimate.py`) uses it only as a clearly labelled **indicative preview**
  (`select_preview_rule_set`): never production-ready, totals named `indicative_total`,
  never persisted to valuations, cost lines or alerts. Golden cases:
  `tests/unit/test_mk_rule_draft.py`. draft.3 (2026-10-13) encodes two owner decisions: the
  ДМВ is part of the import VAT base (ЗДДВ чл. 21(2)(1)), and every MKD amount and the total
  are rounded to whole denars, half-up (`rounding_rules` quantum `1`, `before_dependents`).

No rate, tariff, coefficient or VAT percentage is in `src/`; they exist only in rule-set
JSON. Until an owner-approved rule set is ACTIVE in the database, every valuation keeps
import costs `unknown` and is `incomplete` (a research candidate, never a quantified
opportunity). Approving the draft follows `docs/tax_rule_approval.md` (review against the
sources, re-hash every source, record the review, approve, activate).

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
`power_kw` (kW) (numbers); `invoice_price`, `customs_value`, `included_costs_total`,
`vat_incidental_costs_total` (money); `customs_fx_rate` (customs-purpose FX rate).

`included_costs_total` is the sum of costs included in the customs value;
`vat_incidental_costs_total` is the sum of costs added to the import VAT base only (not
to the customs value). Each cost carries a legal basis; an unknown list is unknown, an
empty list is a known zero, and a cost in another currency is converted with the
customs rate.

Seller and dispatch countries never imply origin. A German purchase is not EU
preferential origin; Switzerland is never grouped with EU states (rules list countries
explicitly); pending/unknown proof is unknown, not "no duty".

### Components (restricted declarative language)

Nothing in a rule file is executed. Each component has `id`, `kind`, `label`,
`category` (`import_duty` | `motor_vehicle_tax` | `import_vat` |
`other_import_charges`), `currency`, optional `depends_on`, `applies_when`,
`alternative_group`, `rounding`, `legal_reference` and `intermediate` (default `false`).

| `kind` | Fields | Amount |
|---|---|---|
| `percentage` | `rate` (fraction, 0.05 = 5 %), `base` (list of money inputs and earlier component ids) | `rate x sum(base)` |
| `fixed` | `amount` | `amount` |
| `bracket` | either `input` (a numeric input) or `key_terms` (money key, below); `tables` (CO2 only, keyed by `nedc` / `nedc_correlated` / `wltp`) or `brackets`; optional `base`, `contiguous`, `integer_values_only`, `key_currency` | the matching bracket gives `amount`, `rate_of_base x sum(base)`, `amount_per_unit_above_lower x (value - lower bound)` or `amount_per_unit x value` |
| `per_unit` | `input`, `amount_per_unit`, optional `threshold` | `amount_per_unit x max(0, value - threshold)` |

Bracket rows use one of two bound forms, the same form for every row of a table:

- `lower_inclusive` / `upper_exclusive`: `lower_inclusive <= value < upper_exclusive`
  (`upper_exclusive: null` = open-ended, last row only); shown as `[100, 150)`.
- `over` / `up_to` (law style "over X up to Y"): `over < value <= up_to`. Only the first
  row may have `over: null` (no lower bound) and only the last `up_to: null`
  (open-ended); shown as `(10000, 12000]`. A value equal to `up_to` belongs to that row.
  For `amount_per_unit_above_lower` the lower bound is `over` (0 when null).

Each row has exactly one outcome: `amount`, `rate_of_base` (at most 1),
`amount_per_unit_above_lower` (marginal: only the units above the lower bound) or
`amount_per_unit` (the whole value times the amount; numeric-input brackets only).

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

Language features added in engine 1.1 (`tax-engine/1.1.0`):

- **Intermediate components** (`"intermediate": true`) are helpers such as a customs
  value converted to the rule currency or a VAT base. They are evaluated, reported
  (flagged `intermediate` in the result) and may feed later components through `base`
  or `key_terms`, but they are never counted in `total_import_cost`, `known_subtotal`,
  the category amounts or the cost lines. An unknown helper still makes the calculation
  incomplete and its category `unknown` (never `not_applicable`).
- **Brackets keyed on money** (`key_terms`, 1-20 money inputs or earlier component ids
  also listed in `depends_on`): the key is their sum in the rule currency, converted to
  `key_currency` (default: the rule currency) with the customs rate under the same
  checks as money inputs (customs purpose, exact currency pair, rate dated no later
  than the declaration date, inside the recorded effective period). Otherwise the key
  is unknown and so is the component. Only `brackets` (no per-cycle `tables`) and the
  `amount` / `rate_of_base` outcomes are allowed; the result shows the key and the rate
  under `inputs_used.key`.
- **Integer guard** (`"integer_values_only": true`, numeric-input brackets only): a
  non-integer value (e.g. CO2 `190.5` against bands printed for whole g/km) makes the
  component unknown with warning `VALUE_NOT_INTEGER_NO_BAND_RULE:<input>=<value>`
  instead of silently falling into a band.

### Validation (`validate_rule_set`)

Unique ids; ids never shadow inputs; references only to declared inputs and earlier
components (listed in `depends_on`); unit declarations; brackets sorted,
non-overlapping, contiguous when declared, only the last open-ended (and, in the
`over`/`up_to` form, only the first without a lower bound); one bound form per table and
per row; non-negative amounts/rates; rates at most 1; matching component currency;
per-cycle CO2 tables need `co2_cycle` declared as an input; predicate values are text,
numbers or ISO dates (never booleans); JSON `NaN`/`Infinity` are refused.

A bracket has exactly one of `input` and `key_terms`. With `key_terms`: no `tables`, no
`integer_values_only`, no per-unit outcomes, a supported `key_currency`, and
`customs_fx_rate` declared when `key_currency` differs from the rule currency;
`key_currency` without `key_terms` is refused. Every intermediate component must be a
`base` or `key_terms` term of a later component (`depends_on` alone is not enough, and a
bracket's `base` counts only when one of its rows has `rate_of_base`), except a `fixed`
member of amount 0 in an `alternative_group` in which another member is used (such a
member only gives the group full coverage; a non-zero one would vanish from the total);
all members of a group are intermediate or none is. A `base` or `key_terms` list never
repeats a term (it would be summed twice).

Approved/active/superseded/expired versions need sources, approver, approval time,
review record, valid_from, currency, categories, components (at least one of them not
intermediate), explicit rounding and a matching `sha256`. Fixture rule sets can only be
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

The engine 1.1 fields are left out of the hash while they hold their defaults
(component: `intermediate: false`, `key_terms: null`, `key_currency: null`,
`integer_values_only: false`; bracket row: `over`, `up_to`, `amount_per_unit` null), so
every rule set written before they existed keeps its hash, with or without those keys
spelled out.

### Selection

Valuations use `select_rule_set` only: one ACTIVE, approved, non-fixture rule set
effective on the declaration date, or none (import costs unknown); overlapping
candidates are an error. `select_preview_rule_set` exists for a clearly labelled
**indicative preview** when no ACTIVE set applies. It considers `approved` (not yet
active), `under_review` and `draft` sets that are not fixtures, have components, validate
and are effective on the date (a null `valid_from` is accepted), preferring approved,
then under review, then draft, then the latest `valid_from`, then the newest stored
`created_at`; a tie it cannot break is an error. Copies of one version must have the
same content, and a version closed (superseded, expired, revoked) in any copy is never
previewed. The selection carries `mode: "indicative_preview"`; a calculation on such a
set is never production-ready and must never reach valuations, cost lines or alerts.
