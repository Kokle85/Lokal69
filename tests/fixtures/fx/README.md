# FX fixtures (SYNTHETIC)

| File | Designation | Purpose |
|---|---|---|
| `ecb_daily_synthetic.xml` | synthetic, format-faithful | Exercises `integrations.fx.parse_ecb_daily_xml` against the ECB `eurofxref-daily.xml` structure (gesmes `Envelope`, nested `Cube` elements, `1 EUR = rate CCY`). |

Every rate and date in these files is invented for tests. They are not real ECB
observations and must never be loaded into a non-fixture database. The ECB publishes
no MKD rate, so no fixture contains one.
