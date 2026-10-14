# ADR 0003: Fixed EUR 700 transport and the customs/import-tax calculator

Status: accepted (owner decisions of 2026-10-13). Date: 2026-10-13.

## Context

The owner asked, on 2026-10-13: „Транспортот стави го фиксен 700 евра, царина направи калкулатор
за царина и истиот треба да има пристап и dot за да може да пресметува царина и данок согласно
податоци кои ќе ги добие од возилото" (make transport a fixed EUR 700; build a customs calculator;
dot must have access to it so it can compute customs duty and tax from the data it gets about the
vehicle).

Later the same day the owner settled two of the fact sheet's open questions: „ДМВ влегува во
основицата за ДДВ" (ДМВ is part of the import VAT base, ЗДДВ Art. 21: customs duty, taxes and other
import charges except VAT itself; VAT = (customs value + duty + ДМВ + other applicable costs and
charges) × 18%) and „заокружи го износот без децимали" (round the amounts without decimals).
They are decisions 11 and 12 below.

Before this decision every cost line except the purchase was `unknown`, and the tax engine
(spec section 16) had no North Macedonian rule set at all, so import charges were `unknown` in
every valuation. The research behind the calculator is the evidence pack
[docs/tax/mk_import_fact_sheet_2026-10.md](../tax/mk_import_fact_sheet_2026-10.md).

## Decisions

1. **Transport is a fixed EUR 700 per vehicle**, from any purchase country (DE, IT, CH) and for
   either vehicle condition: `config/cost_profiles/default_unapproved.yaml` version 3, line
   `transport`, `estimated` with low = base = high = 700.00 EUR. A transporter quote for a specific
   vehicle replaces it in that valuation. The profile version itself stays **unapproved** until the
   owner approves it (dashboard, Settings, "Tax rules and cost profile").
2. **How the 700 enters the import charges.** By default all 700 is in the customs value (the
   legal outcome for a lump-sum price that does not separate the inland leg: Царински закон
   Arts. 35(1)(д), 36(а)). A separately invoiced inland leg after the MK border
   (`transport_after_entry_eur` with `separately_invoiced`) leaves the customs value and enters
   only the import VAT base (ЗДДВ Art. 21(2)(2)), so the VAT base always contains all 700.
3. **One calculator, three surfaces, one service.** The read-only service
   `persistence/queries/import_costs.py` (scope `deals:read`) computes customs duty, motor-vehicle
   tax (ДМВ) and import VAT for one vehicle, from a listing's facts, from facts the caller states,
   or both, plus the landed cost (the REAL purchase price + EUR 700 + charges; decision 13). It
   persists nothing (no valuation, cost line, rate, audit, outbox or job row); recording a
   listing's purchase price and estimated customs value is a separate owner act (decision 14). It
   is served to:
   - **dot** through the MCP tool `deals_estimate_import_costs` (docs/connect_mcp.md section 7);
   - the **dashboard** through `GET /api/import-costs/estimate` and the "Customs calculator"
     screen (`/import-costs`, `/candidates/<id>/import-costs`);
   - the **operator** through `suv-deals import-costs estimate`.

   All three return the same view (`views/import_costs.py`, `ImportCostEstimateView`).
4. **No rate in code.** Rates, coefficients, bands and thresholds live only in the rule-set JSON.
   The engine gained six small, generic language extensions (engine `tax-engine/1.1.0`:
   intermediate components, brackets keyed on money, `over`/`up_to` bounds, `amount_per_unit`,
   an integer guard, VAT-only incidental costs; config/tax_rules/README.md). Arithmetic is exact
   `Decimal`.
5. **The North Macedonian rule set ships as an unapproved DRAFT**:
   `config/tax_rules/drafts/mk_passenger_car_import_2026.10-draft.3.json`
   (`mk-passenger-car-import@2026.10-draft.3`, status `draft`, no approver, no sealed hash, 21
   components, every open point marked `UNCERTAIN - confirm`). It reproduces the fact sheet's
   worked examples to the denar (`tests/unit/test_mk_rule_draft.py`: G01-G19 with G17b/G17c,
   U1-U10, the ДМВ set #1-#19). draft.3 replaced draft.2 on 2026-10-13 for decisions 11 and 12
   (a content change is a new version, never an edit in place).
6. **Valuations and alerts are unchanged in what they trust.** They use only an ACTIVE,
   owner-approved rule set (`select_rule_set`, `allow_unapproved=False`). Until one exists, import
   charges stay `unknown` and every valuation stays `incomplete`. Valuations now build their tax
   inputs with the calculator's builder (`import_estimate.build_tax_inputs`), so once a rule set is
   ACTIVE both compute the same charges.
7. **Indicative preview.** Without an ACTIVE rule set the calculator previews the newest
   approved-but-inactive, under-review or draft version (`select_preview_rule_set`): mode
   `indicative_preview`, `production_ready: false`, totals named `indicative_total`, and a banner
   on every surface: „ИНДИКАТИВНА ПРОЦЕНКА – неодобрени нацрт-правила (…). Не е царинска пресметка;
   не се користи за вреднувања ни известувања." / "INDICATIVE PREVIEW – unapproved draft rules;
   not a customs assessment; never used for valuations or alerts." A preview never reaches
   valuations, cost lines (`costs.tax_cost_lines` refuses an estimate) or alerts. With no rule set
   at all the mode is `unavailable` and every import charge is unknown, never zero.
8. **Nothing is inferred.** No proof of origin means the standard 5% duty (the 0% branches need
   a proof the caller states; the standard-duty alternative is always shown). The seller or
   dispatch country never implies origin; the tariff code is never inferred (a code the caller
   states is used as an unverified caller attestation, HS 8703 only; a plug-in hybrid needs one,
   since its HS code decides the 50% ДМВ); the CO2 figure is used in the cycle printed on the document and
   WLTP and NEDC are never converted; the VAT base is the rule set's, never a guess. A missing fact
   makes the affected charges unknown with an explicit reason. An estimated customs value (e.g.
   SchwackeNet) is a separate value with its source, never a max() of it and the price (decision 13).
9. **The customs rate is data.** The EUR/MKD rate is the Customs monthly list rate for the
   declaration month, recorded with `suv-deals fx record ... --purpose customs`; a caller may supply
   it for one estimate (labelled `CALLER_SUPPLIED_CUSTOMS_RATE`, never stored). Without one, the
   customs value in MKD stays unknown.
10. **No tool approves anything.** dot reads only. Storing, reviewing, approving and activating a
    rule set and approving the cost profile are owner acts (dashboard Settings, `config:admin`;
    `suv-deals tax-rules store` only stores a draft). Nothing is approved or activated
    automatically.
11. **ДМВ is part of the import VAT base** (owner decision D1 of 2026-10-13: „ДМВ влегува во
    основицата за ДДВ"). ЗДДВ Art. 21(2)(1) adds to the base „царината, даноците ... другите
    увозни давачки, освен данокот на додадена вредност", and ДМВ is an import levy assessed and
    collected by Customs with the other import charges (ЗДМВ Arts. 8(1)(3), 9(10), 13(6)). So
    VAT = 18% × (customs value + duty + ДМВ + incidental costs to the first destination not already
    in the customs value). The draft always encoded this; its `UNCERTAIN - confirm` marker on
    `vat_base_mkd` is removed and the component cites the owner confirmation and Art. 21(2)(1)
    (draft.3). The fact sheet keeps the original evidence (section 6.3) and the history of open
    question 1.
12. **Amounts are rounded to whole denars** (owner decision D3 of 2026-10-13: „заокружи го износот
    без децимали"). The rule set rounds every MKD component, the intermediate helpers included,
    and the total to quantum 1, half-up, before the amount feeds the next charge
    (`rounding_rules` `component_default` and `total`: `{"quantum": "1", "mode": "half_up",
    "stage": "before_dependents"}`), as the declaration assesses the customs value, duty, ДМВ and
    VAT and as the Customs ДМВ guide prints 38,123.8 as 38,124. Effects: totals move by at most a
    few denars (D1 168,154.45 -> 168,155 MKD; fact sheet section 10.4), and the ДМВ value-part key
    (customs value + duty, in EUR at the customs rate) is computed from the assessed whole-denar
    amounts, so at the EUR 10,000 edge a car can fall into the 0% band where 2-decimal rounding put
    it just above (golden case G17c: CV EUR 9,523.81 gives exactly 615,337 MKD = EUR 10,000).
    The second half of the decision: the calculator presents EUR amounts (EUR equivalents,
    purchase price, customs value for duties, landed cost) as whole euros, half-up, rounded from
    the exact figures (a landed cost is summed exactly and rounded once, so it may differ by EUR 1
    from the sum of rounded parts); the calculator view shows every amount, MKD included, without
    a fraction (`"168155"`, `"2733"`), and the `facts` echo the inputs as given. Texts that quote
    an input amount (the customs-value basis, assumptions, warnings, a recorded value's source)
    drop a zero fraction ("invoice 3000 EUR + ... 700 EUR") and keep a real one exact
    ("2800.50"), never a rounded figure the calculation did not use.
13. **Two values per vehicle** (owner decision D2 of 2026-10-13). Customs officially uses the
    SchwackeNet catalogue to assess used cars, but Царински закон Art. 28 makes the transaction
    value (the price actually paid) the primary method; the other methods apply in the legal order
    and only on founded doubt. So the vehicle facts keep the REAL purchase price (`price`, basis
    `invoice`, `advertised` or a recorded `recorded_actual` / `recorded_quote` /
    `recorded_estimate`) apart from an optional ESTIMATED customs value
    (`estimated_customs_value_eur`, the vehicle without transport, always with
    `estimated_customs_value_source`). The `primary` scenario's charges are computed on the estimate
    + the transport to the MK place of entry (whether Customs adds that transport to a catalogue
    value stays UNCERTAIN), otherwise on the transaction value; with an estimate the
    `transaction_value` scenario shows the charges if Customs accepts the price paid; the
    `standard_duty_if_proof_rejected` scenario stays. Every scenario's landed cost, and so the
    profit, uses the REAL purchase price. The result names both values (`purchase_price`,
    `customs_value_for_duties`); an estimate is listed in `unverified_inputs`, and one below the
    price is flagged (`ESTIMATED_CUSTOMS_VALUE_BELOW_PRICE`). Example: bought for EUR 2,800,
    catalogue EUR 4,200: duties on EUR 4,900 (185,803 MKD), landed cost EUR 6,520; on the invoice
    (EUR 3,500) 165,213 MKD and EUR 6,185. The old `catalogue_value_eur` input and its
    `customs_revaluation_risk` scenario are removed; `legal_base` is renamed `primary`.
14. **The two values are recorded per listing** (owner decision D2, continued). The owner records
    a listing's real purchase price (EUR, kind `actual` / `quote` / `estimate`, source, optional
    document reference) and its estimated customs value (EUR without transport, source) in the
    append-only `app.cost_evidence` (categories `purchase` and the new `customs_value`, migration
    `20261013000100_import_values`; neither is a cost-profile category). A new value supersedes the
    current one; recording is owner-only (`POST /api/listings/{listing_id}/import-values`, never an
    MCP tool) and marks the listing's valuations stale with one queued recomputation. The
    calculator's listing mode and the valuations use the current values (sources labelled; a
    request's own value wins in the calculator). Valuations use the recorded purchase price as the
    purchase (so the profit) and the recorded estimate for the import charges, still under ACTIVE
    rules only, and cite both rows so superseding either re-values the listing.

## What the owner must confirm before approval

The `UNCERTAIN - confirm` markers of the draft, from the fact sheet's section 13, ideally with a
licensed customs broker:

- Resolved by the owner on 2026-10-13 (decisions 11 and 12, no longer open): ДМВ inside the
  import VAT base, and rounding (whole denars, half-up; was 0.01 MKD half-up in draft.2).
- The СКД rate for the ДМВ value part (customs monthly list or the daily NBRSM rate), and how a CHF
  price is converted.
- CO2 band edges (printed lower figures read as inclusive), non-integer CO2, the fuel column for
  hybrids and plug-in hybrids, `nedc_correlated` figures.
- How Customs treats the EUR 700 lump sum (and any broker/terminal fees before the first
  destination) in the customs value and the VAT base.
- When Customs values a used car on the SchwackeNet catalogue instead of the invoice, and whether
  it adds the transport to the MK place of entry on top of a catalogue value (decision 13 keeps
  both values and plans the `primary` charges on the owner's estimate + transport; the
  transport part is marked UNCERTAIN in every result that uses an estimate).
- Which non-EU preferential proofs are accepted in practice, and what evidence Customs accepts for
  US origin (final text of Сл. весник 130/25).
- The legal act behind the Euro 4 import minimum; that no Euro 5 rule or later ДМВ amendment was
  adopted; that no customs processing fee applies.
- Every source re-fetched from its official URL and re-hashed (the draft's hashes are of local
  research copies).

## What the owner must do to activate

1. Review the fact sheet and the draft with the adviser; resolve each `UNCERTAIN` marker. A change
   to the rules is a new draft version (content is versioned, never edited in place).
2. Record the customs EUR/MKD rate each month:
   `suv-deals fx record --base EUR --quote MKD --rate <rate> --date YYYY-MM-01 --purpose customs
   --provider <list> --source-ref <reference> --reason "<why>" --yes`.
3. In the dashboard, Settings, "Tax rules and cost profile" (owner only): **Store draft** (or
   `suv-deals tax-rules store <file> --owner-user-id <owner> --yes`), **Submit for review**,
   **Approve** with the approver's name and the review record bound to the content hash, then
   **Activate** (docs/tax_rule_approval.md).
4. In the same section, approve the cost profile (transport EUR 700).
5. Record the evidence for the `tax_rules` and `cost_assumptions` gates (ACTIVATION_GATES.md
   sections 6 and 7). Both gates stay `blocked` until then.

## Consequences

- dot and the owner can price a vehicle's import today, clearly labelled as an indicative preview
  of unapproved rules; landed cost uses the fixed EUR 700.
- Valuations still keep import charges unknown and stay `incomplete` until the owner activates a
  rule set; nothing about alerts changes.
- Valuations built with the shared builder record its notes as warnings (`TAX_INPUT_*`,
  `IMPORT_GATE:`), and a customs rate from a previous month no longer counts for the declaration
  month (the monthly list is valid in its own month).
- Tax lines that rest on an advertised price, a listing CO2 figure or a caller-claimed proof or
  tariff code are never `rule_supported`, so such a valuation is never `quote_supported`.
- No database migration was needed.
