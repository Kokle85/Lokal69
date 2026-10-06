# Switzerland (CH) as an acquisition market

Status date: 2026-10-06. Spec sections 3, 5, 7, 16, 17, 18, 19 and 31.

This page is written in plain English with short sentences. It says what the system does with
Swiss listings, what is still unknown, and what the owner must decide. It is not legal, tax or
customs advice. Every rate, fee and legal condition below must be confirmed by the owner, the
seller, a forwarder or a customs broker before it is used.

## 1. Role of Switzerland

- Switzerland is one of the three **initial acquisition countries**: Germany (DE), Italy (IT) and
  Switzerland (CH). The primary profile has `source_countries: [DE, IT, CH]`
  (`config/profiles/primary.yaml`). The resale research market is North Macedonia (MK).
- The hard rules are the same as for DE and IT. The full-vehicle payable amount must be between
  EUR 2,500.00 and EUR 3,000.00 inclusive, after FX conversion. Mileage must be strictly below
  200,000 km.
- **No Swiss source is enabled, verified or active.** See section 7.

## 2. CHF prices and FX

- The ECB reference feed publishes **1 EUR = x CHF**. A CHF amount becomes EUR by **division**:
  `EUR = CHF / x`. `FxRate.convert` stores and honours this direction (`domain/money.py`,
  `integrations/fx.py`).
- **No CHF/EUR parity is ever assumed.** CHF 2,990 is not EUR 2,990.
- The comparison with the EUR band uses the unrounded Decimal EUR value. Rounding is only for
  display.
- SYNTHETIC example (not a real rate): with 1 EUR = 0.9400 CHF,
  - CHF 2,990.00 / 0.9400 = EUR 3,180.85..., so this car is above the primary band;
  - EUR 3,000.00 is CHF 2,820.00 and EUR 2,500.00 is CHF 2,350.00.
- **Stale or missing rate** (`domain/filters.py`):
  - no usable rate: the result is `needs_facts` with the missing fact `fx:CHF/EUR`;
  - a rate older than `fx_max_age_days` (7 in the primary profile) makes the result `needs_facts`
    when the EUR value is within `fx_boundary_margin_pct` (3 %) of a band boundary. Far from a
    boundary it is only a warning.
- **Search ceilings on Swiss sources.** If a Swiss source is ever searched with a CHF price
  filter, the filter must never drop a car that is EUR 3,000 or less after conversion. Compute
  the CHF ceiling from the current rate **with headroom** (at least the boundary margin), round it
  up, and recompute it when the rate moves. Never use parity or an old, lower rate. In the
  synthetic example: EUR 3,000 x 0.9400 = CHF 2,820, plus 3 % = CHF 2,904.60, so the ceiling is at
  least CHF 2,905. A filter that is too high only costs a few extra results, which the EUR
  screening then rejects. A filter that is too low silently loses candidates. The final decision is
  always the EUR comparison in `domain/filters.py`, never the site filter. The same applies to a
  lower bound around EUR 2,500.

## 3. Swiss listing wording

All of this is **seller wording**. It is stored with provenance and is never a verified fact.

| Wording | Meaning | What the system does |
|---|---|---|
| `CHF 2'990.–`, `Fr. 2'990.-`, `SFr. 2'990.--`, `2’990.—` | CHF 2,990.00; apostrophes group thousands; the dash means "no cents" | Parsed exactly (`parse_price`, locale `ch`, and the dealer adapter) |
| `inkl. MWST` / `exkl. MWST` / `ohne MWST` | VAT included / excluded / without VAT (MWST is the Swiss spelling) | Basis `gross` / `net` / `net` |
| `TVA incluse`, `TTC` / `hors TVA`, `TVA en sus` | The same in French (Romandie) | Basis `gross` / `net` |
| `IVA inclusa` / `IVA esclusa` | The same in Italian (Ticino) | Basis `gross` / `net` |
| `8.1% MWST` | A VAT rate written by the seller | Recorded in `vat_rate_stated` exactly as written. **The code never contains or assumes a Swiss VAT rate.** |
| `Exportpreis`, `Export ohne MWST`, `Händlerpreis`, `prix export` | Export or dealer-trade price | `export_net`. This is not an ordinary payable price. It cannot pass as a confirmed target price until the seller confirms the payable amount for this buyer (spec 17). |
| `Occasion` | A used car | No price effect |
| `ab Platz` | Sold as seen, collected at the dealer's yard | No price effect. The buyer arranges collection and transport. |
| `ab MFK`, `frisch ab MFK`, `MFK neu`, `frisch vorgeführt` | Freshly passed the Swiss periodic inspection (MFK, Motorfahrzeugkontrolle) | `condition.roadworthy = seller_claimed` (never `verified`) |
| `vor MFK`, `nicht ab MFK`, `ohne MFK`, `muss zur MFK` | Sold before or without a fresh inspection | **Never positive.** `roadworthy` stays `unknown`, warning `INSPECTION_NOT_FRESH` |
| `MFK abgelaufen` | The inspection is overdue | `seller_denied`, warning `INSPECTION_EXPIRED` |
| `MFK bis 06/2026`, `nächste MFK 06/2028` | Next inspection due | `documentation.inspection_expiry`. A date before the observation month is `INSPECTION_EXPIRED` and never positive. |
| `letzte MFK 2025`, `ab MFK 03.2025` | Date of the last inspection | Recorded as the last inspection, not as an expiry. Not positive by itself. |
| `MFK 05.2024` (bare date) | Ambiguous: Swiss ads use this for the last inspection | No date is stored. Warning `INSPECTION_DATE_AMBIGUOUS`. |
| `expertisé(e)`, `expertisée du jour` / `sans expertise`, `non expertisé` / `dernière expertise 2025` | The same in French (expertise = MFK) | Fresh / never positive / last inspection |
| `collaudata` / `senza collaudo` | The same in Ticino Italian | Fresh / never positive (MFK wording when the locale is `ch`) |
| `ab MFK auf Wunsch`, `MFK neu (+ CHF 500.-)`, `sur demande` | An offer, not the current state | **Not positive**. Warning `INSPECTION_CONDITIONAL` |

The same parser also reads DE wording (`HU/AU neu`, `TÜV neu`, `HU bis 05/2027`, `TÜV 05/2027`,
`HU 05/27`, `ohne TÜV`) and IT wording (`revisionata`, `revisione fino a 05/2027`,
`revisione scaduta`, `senza revisione`). A two-digit year (`05/27`) becomes 20xx inside a sanity
window around the observation date: at most 5 years ahead and 30 years back. Otherwise the date is
`INSPECTION_DATE_IMPLAUSIBLE` and is not stored.

Code: `domain/parsing.py` (`parse_inspection`, `parse_price`). The generic dealer adapter
(`adapters/dealer_inventory.py`, `schemaorg_dealer@1.1.0`) fills `condition.roadworthy` and
`documentation.inspection_expiry` from the visible page text with provenance (method `regex`,
confidence `medium`). It adds `INSPECTION_EXPIRED` when the stated expiry is before `observed_at`.
A seller's inspection wording is never an inspection report (spec 19: "needs inspection" and
"needs documents" stay open until the owner holds the report).

## 4. Language regions

- Switzerland has German-speaking (most cantons, locale `de-CH`), French-speaking (Romandie, west,
  `fr-CH`) and Italian-speaking (Ticino, south, `it-CH`) regions. A dealer or marketplace can show
  any of them.
- Numbers use the Swiss format in every region: apostrophe (or space) thousands groups and a point
  for decimals (`2'990.50`). `COUNTRY_LOCALES` maps `CH` to the `ch` number locale.
- The wording rules above cover DE, FR and IT. A source's `crawl_locale` is set per source. The
  Swiss dealer template uses `de-CH`. Change it to `fr-CH` or `it-CH` for a dealer in those regions.
- Zone-less Swiss timestamps are read in `Europe/Zurich` (`source_timezone`).

## 5. Export from a non-EU country, transit, origin and MK import evidence

Switzerland is **not** in the EU. Buying there differs from buying in DE or IT.

1. **Export declaration.** The car leaves Switzerland under an export declaration
   (Ausfuhrdeklaration) lodged with Swiss customs (BAZG / FOCBS). The dealer, a forwarder or the
   buyer lodges it. Who does it, the fee and the documents must come from a quote.
2. **Swiss VAT.** A Swiss dealer's price normally includes Swiss VAT (MWST / TVA / IVA). The seller
   may invoice an export sale **without** Swiss VAT only under the seller's own conditions, usually
   against customs export evidence. Some sellers charge the VAT and refund it after the export
   evidence arrives. That is a cash deposit with refund risk (spec 17). The owner must get the
   arrangement **in writing from the seller**. It is never assumed from "Export" or "exkl. MWST"
   wording, and the Swiss VAT rate is never assumed.
3. **Transit.** Every road route from Switzerland to North Macedonia leaves Switzerland through an EU
   member state (DE, FR, IT or AT). A car bought in Switzerland is not EU goods, so it normally
   travels under a transit procedure (for example a T1 transit declaration with a guarantee) to the
   MK border office, as the forwarder arranges. The route may also cross non-EU states, for example
   Serbia. The forwarder or customs broker must confirm the procedure, the guarantee and the fee.
4. **Truck or driven.** A truck needs a transport quote from the Swiss origin city. Driving needs
   Swiss export plates and insurance that cover every transit country and the whole period. The
   canton decides whether a valid MFK is required for export plates. A car sold `vor MFK` or
   `ohne MFK` may have to go by truck.
5. **Origin.** The purchase country is never proof of origin. **A Swiss purchase does not give EU
   preferential origin for the MK import.** The tax engine (`domain/tax_engine.py`) takes origin
   only from an accepted origin proof (`TaxInputs.origin_proof`). Seller and dispatch countries never
   imply origin. Switzerland is never grouped with EU states, because rules list countries
   explicitly. Tests: `test_swiss_purchase_without_proof_never_implies_eu_preferential_origin` and
   `test_swiss_origin_is_not_interchangeable_with_listed_countries` in
   `tests/unit/test_tax_engine.py`.
6. **MK import evidence.** The owner's customs broker decides what the MK import needs. Check at
   least: the invoice, the Swiss vehicle registration document, the Swiss export declaration, the
   transit document, the origin evidence and the CoC/homologation documents. A vehicle built for the
   Swiss market may not come with an EU CoC. Confirm what MK homologation accepts before buying.

## 6. Cost lines for a Swiss purchase

`config/cost_profiles/default_unapproved.yaml` (version 2, **unapproved, no amounts**) adds four
lines scoped to `origin_country: CH` (marker `ch_purchase`, labels start with `CH purchase:`):

| Category | Line | Note |
|---|---|---|
| `customs_broker` | Swiss export declaration (Ausfuhrdeklaration) via forwarder/customs broker | In addition to the MK import clearance line |
| `customs_broker` | Transit through the EU (e.g. T1 transit declaration and guarantee) | Forwarder quote for the whole route |
| `export_plates_insurance` | Swiss export plates and insurance (only if driven out) | The Swiss form of the general line. Record one of the two `not_applicable` so nothing is counted twice. Record `not_applicable` (reason: truck) when the car goes by truck. |
| `refundable_deposit` | Swiss VAT charged by the seller pending export evidence | The Swiss form of the general deposit line. The amount and refund conditions come from the seller. |

All four are `unknown`. An unknown line keeps the dependent totals unknown, never zero.
`CostProfile.lines(target_scope=...)` leaves these lines out for a DE or IT purchase, so they never
make a German valuation unknown. No CH line uses an import-tax category: import duty, VAT and
other charges come only from an ACTIVE, owner-approved tax rule set.

## 7. Swiss sources and their gate status

Every Swiss source is `enabled: false`. Public accessibility is not permission, and a stored owner
acknowledgement is not legal permission. See `docs/source_access_register.md`.

| Source key | What it is | Status |
|---|---|---|
| `autoscout24_ch` | AutoScout24 Switzerland public listings | Placeholder (`unimplemented`). Terms **unreviewed**: the Swiss operator and terms are not identified. Robots: robots.txt checked 2026-10-06 has **no `User-agent: *` group**. This is recorded as a fact (`robots_checked_at`, `robots_summary`), **not as permission**. The robots module must still fetch and store the revision, and named groups must be checked against our user agent. Adapter not implemented. |
| `carforyou_ch` | Swiss used-car marketplace candidate | Placeholder. Candidate domain `www.carforyou.ch` **unverified** (nothing fetched). Terms unreviewed. |
| `tutti_ch` | Swiss general classifieds candidate | Placeholder. Candidate domain `www.tutti.ch` **unverified**. Private sellers: store no seller contact data. |
| `comparis_ch` | Swiss comparison site / aggregator candidate | Placeholder. Candidate domain `www.comparis.ch` **unverified**. An aggregator may republish third-party listings, so its own terms and robots.txt **and** the original source's terms must be checked. Record the source attribution and prefer the original source. |
| `example_dealer_template_ch` | Template for a permitted Swiss dealer website | Generic `schemaorg_dealer@1.1.0` adapter. `crawl_locale: de-CH`, `source_timezone: Europe/Zurich`, no hosts. Untested for any real dealer. |
| `fixture_dealer_ch` | Synthetic test source (`tests/adapters/fixtures/`) | Tests only. Hand-written pages on `garage.example`. Never fetched from a network. |

## 8. What the owner must decide

1. **Sources.** Which Swiss sources to pursue. For each one: identify and review the Swiss terms,
   then record a terms decision (`proceed_permitted` needs permission or an agreement on file). For
   `comparis_ch`, decide whether aggregator use is acceptable at all.
2. **Is CH worth it?** Get forwarder quotes for the export declaration and the transit (T1 and
   guarantee), and transport quotes from typical Swiss origin cities. Compare them with DE/IT
   purchases before choosing any Swiss car.
3. **Truck or drive.** If driving, get the canton's rules for export plates and insurance (is a
   valid MFK required?) and insurance that covers every transit country.
4. **Swiss VAT, per seller.** Get written confirmation: an export invoice without Swiss VAT (and its
   conditions), or the VAT deposit amount, refund conditions and timing.
5. **MK import.** With the MK customs broker: which documents MK needs for a vehicle bought in
   Switzerland (CoC/homologation, origin evidence, transit document). Approve a tax rule set before
   any import amount is shown as production-ready.
6. **Payment in CHF.** The bank route and charges for paying in CHF (`bank_fx_charges` line).
7. **Search ceilings.** Accept CHF search ceilings with headroom (section 2), if a Swiss source ever
   uses a CHF price filter.
8. **Cost assumptions.** Approve or replace the unknown cost lines. Nothing in the profile is
   approved.

## 9. Where it lives

| Topic | Code / config | Tests |
|---|---|---|
| Swiss prices, inspection wording | `src/suv_deals/domain/parsing.py` | `tests/unit/test_parsing_inspection.py`, `tests/property/test_inspection_properties.py` |
| Dealer adapter (roadworthy, inspection expiry, Swiss export wording) | `src/suv_deals/adapters/dealer_inventory.py` | `tests/adapters/test_dealer_inspection.py`, `tests/adapters/test_dealer_detail.py` |
| CHF/EUR FX and boundary staleness | `src/suv_deals/domain/filters.py`, `src/suv_deals/integrations/fx.py` | `tests/unit/test_filters.py`, `tests/unit/test_fx.py` |
| CH cost lines | `config/cost_profiles/default_unapproved.yaml`, `src/suv_deals/domain/costs.py` | `tests/unit/test_switzerland_market.py` |
| Origin (non-EU) | `src/suv_deals/domain/tax_engine.py` | `tests/unit/test_tax_engine.py` |
| Swiss sources | `config/sources/*_ch.yaml`, `src/suv_deals/adapters/ch_marketplaces.py` | `tests/adapters/test_registry_and_configs.py`, `tests/adapters/test_placeholders.py` |
