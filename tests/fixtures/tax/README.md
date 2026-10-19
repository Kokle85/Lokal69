# Tax rule fixtures (SYNTHETIC)

| File | Designation | Purpose |
|---|---|---|
| `synthetic_rule_set.json` | synthetic, `is_fixture: true`, status `unapproved` | Golden/boundary tests for `domain.tax_engine`: brackets per CO2 cycle, per-unit and age brackets, origin alternatives, explicit VAT base, rounding. |

Jurisdiction `XX` is an ISO 3166 user-assigned code, not a country. Every rate, bracket
boundary and amount is invented to exercise the engine; none is a real tax, tariff,
coefficient or VAT rate of North Macedonia or anywhere else. The file carries its
canonical content hash (`sha256`); edit it only through a regenerate step that
recomputes the hash with `tax_engine.compute_rule_set_sha256`, otherwise loading fails
(tamper detection).
