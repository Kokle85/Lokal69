# ADR 0003: Fixed EUR 700 transport and the customs/import-tax calculator

Status: accepted (owner decisions of 2026-10-13). Date: 2026-10-13.

## Context

The owner asked, on 2026-10-13: „Транспортот стави го фиксен 700 евра, царина направи калкулатор
за царина и истиот треба да има пристап и dot за да може да пресметува царина и данок согласно
податоци кои ќе ги добие од возилото" (make transport a fixed EUR 700; build a customs calculator;
dot must have access to it so it can compute customs duty and tax from the data it gets about the
vehicle).

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
   or both, plus the landed cost (price + EUR 700 + charges). It persists nothing (no valuation,
   cost line, rate, audit, outbox or job row). It is served to:
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
   `config/tax_rules/drafts/mk_passenger_car_import_2026.10-draft.1.json`
   (`mk-passenger-car-import@2026.10-draft.1`, status `draft`, no approver, no sealed hash, 19
   components, every open point marked `UNCERTAIN - confirm`). It reproduces the fact sheet's
   worked examples to the denar (`tests/unit/test_mk_rule_draft.py`: G01-G19, U1-U10, the ДМВ set
   #1-#19).
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
   makes the affected charges unknown with an explicit reason. A catalogue (SchwackeNet) value only
   adds a separate "customs revaluation risk" scenario, never a max().
9. **The customs rate is data.** The EUR/MKD rate is the Customs monthly list rate for the
   declaration month, recorded with `suv-deals fx record ... --purpose customs`; a caller may supply
   it for one estimate (labelled `CALLER_SUPPLIED_CUSTOMS_RATE`, never stored). Without one, the
   customs value in MKD stays unknown.
10. **No tool approves anything.** dot reads only. Storing, reviewing, approving and activating a
    rule set and approving the cost profile are owner acts (dashboard Settings, `config:admin`;
    `suv-deals tax-rules store` only stores a draft). Nothing is approved or activated
    automatically.

## What the owner must confirm before approval

The `UNCERTAIN - confirm` markers of the draft, from the fact sheet's section 13, ideally with a
licensed customs broker:

- ДМВ inside the import VAT base (statutory reading; no explicit administrative text).
- The СКД rate for the ДМВ value part (customs monthly list or the daily NBRSM rate), and how a CHF
  price is converted.
- Rounding of duty, ДМВ and VAT (0.01 MKD half-up in the draft, or whole denars).
- CO2 band edges (printed lower figures read as inclusive), non-integer CO2, the fuel column for
  hybrids and plug-in hybrids, `nedc_correlated` figures.
- How Customs treats the EUR 700 lump sum (and any broker/terminal fees before the first
  destination) in the customs value and the VAT base.
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
