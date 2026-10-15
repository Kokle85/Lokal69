# Changelog

The build waves in order, derived from `git log --oneline` and the wave documentation
(docs/schema.md sections 9 to 11, docs/decisions/). Dates are commit dates. "WIP checkpoint"
commits are omitted; they only preserved interrupted work. **No wave activated anything against
a real service**: no live crawl, no real e-mail, no Slack, dot or Outlook on a real PC.

## Unreleased (working tree on top of `7f7e187`, not committed)

Owner re-confirmation of 2026-10-14 ("ги одобрувам mobile.de, AutoScout24 да се скенираат") and a
robots.txt re-check of the same day. Nothing is enabled, fetched or activated.

- **`suv-deals capture-samples` (owner-run).** Saves a few reviewed pages of one source (one search
  path, at most `--detail-limit` detail pages) to the git-ignored `captures/` folder, so parsers are
  written from real structure. Guard rails: `--yes`, `SOURCE_NETWORK_ENABLED=true` for that command
  only, reviewed path and detail patterns, robots.txt on every URL, same-host redirects only through the same checks (two hops at most),
  20 s spacing, and
  the first block, challenge page or error stops the run. It prints counts and statuses only; no
  database, no source flag or gate changes. Tests use fake fetchers (no network).

- **mobile.de robots.txt changed.** `suchen.mobile.de` now disallows `/fahrzeuge/search.html`,
  `/fahrzeuge/searchresults.html`, `isSearchRequest=true`, `pageNumber=` and
  `/fahrzeuge/details.html` for every generic crawler; only `details.html?id=<n>&vip=1` stays
  allowed. `mobile_de_public` therefore has an empty search allow-list and cannot discover listings;
  the owner's approval does not override robots.txt. Remaining routes: the official Search API
  (`mobile_de_api`) or listing links the owner supplies.
- **AutoScout24 DE/IT/CH robots.txt unchanged** since 2026-10-06 (re-check dates and file hashes
  recorded in `config/sources/autoscout24_*.yaml`).
- **Owner re-confirmation recorded** in the four `terms_decision_note`s (an audit record, not legal
  permission).
- **Adapters still unbuilt.** Fetching one AutoScout24 page to build the parser was denied again by
  the automated policy classifier of the build environment, after the owner's re-confirmation. The
  parsers need page captures saved by the owner, or the owner's permission rule in the Claude Code
  settings.
- Tests: `test_mobile_de_public_search_is_robots_disallowed`; the CH robots date moved to
  2026-10-14.

Owner decision of 2026-10-14 ("сакам сите да го вклучиш": include every source) and the terms and
robots.txt review behind it (docs/research/source_terms_review_2026-10-14.md; one research agent per
source pair plus an independent verifier; only robots.txt, terms, imprint and home pages were
requested). Nothing is enabled.

- `subito_it`, `automobile_it`, `comparis_ch`, `carforyou_ch`, `tutti_ch`, `pazar3_mk` and
  `reklama5_mk` now carry `terms_decision: proceed_acknowledged` (owner, 2026-10-14) and a robots
  record of 2026-10-14.
- **Subito** (Subito.it S.r.l., Milano) and **automobile.it** (Marktplaats B.V., Amsterdam): terms
  **restricted**; both expressly prohibit robots, scrapers and automated extraction without consent.
  Terms URL, review date and canonical host recorded; routes stay unverified.
- **Comparis**: robots.txt disallows the car-marketplace result and detail endpoints.
- **AutoScout24.ch, Comparis, CarForYou, Tutti, Pazar3, Reklama5**: the build environment met
  Cloudflare/DataDome challenges, HTTP 403 or TLS resets on their HTML pages, so their terms stay
  unreviewed and keep the gate closed; operators are only leads, not recorded facts.
- Docs: source access register, Swiss market page, status and activation-gate pages.
- Tests: the owner-decided source set now covers all eleven marketplace and MK sources;
  `test_italian_marketplace_terms_reviewed`; the Swiss candidate test reads the 2026-10-14 record.

## Owner decisions D1-D3 and recorded import values (2026-10-14, `e5b0ec9`, `7f7e187`)

Owner decisions of 2026-10-13, given in Macedonian (ADR 0003 decisions 11-14; fact sheet sections
3.4, 6.3, 10.4, 11 and 13). Nothing is approved or activated; the rule set stays an unapproved
DRAFT and every calculator result an indicative preview.

- **D1 "ДМВ влегува во основицата за ДДВ":** the motor-vehicle tax is part of the import VAT base
  (ЗДДВ чл. 21: customs, taxes and other import charges except VAT itself): VAT = (customs value
  + duty + ДМВ + other applicable costs) x 18%. Open question resolved; no longer `UNCERTAIN`.
- **D2 SchwackeNet vs the invoice:** Customs officially uses the SchwackeNet catalogue for used
  vehicles, but Царински закон чл. 28 makes the transaction value (the price actually paid) the
  primary method; the other methods apply in the legal order and only on founded doubt. Each
  vehicle therefore keeps two separate values: the REAL purchase price (реална набавна цена;
  profit, landed cost) and the ESTIMATED customs value (проценета царинска вредност; import
  charges). Example: bought for EUR 2,800, catalogue EUR 4,200: `primary` 185,803 MKD on EUR 4,900
  and landed cost EUR 6,520; `transaction_value` 165,213 MKD on EUR 3,500 and EUR 6,185.
- **D3 "заокружи го износот без децимали":** every MKD amount and the total in whole denars
  (half-up), every amount the calculator shows without decimals (EUR in whole euros, half-up).

### D1 and D3: ДМВ in the VAT base, whole denars (rule set)

- Draft rule set re-versioned to `mk-passenger-car-import@2026.10-draft.3` (draft.2 is no longer
  shipped; content hash `4fe7f064...`): the ДМВ is part of the import VAT base (owner
  confirmation, ЗДДВ чл. 21(2)(1); the `UNCERTAIN` marker on `vat_base_mkd` is gone), and every MKD
  amount and the total are rounded to whole denars, half-up, before they feed the next charge
  (`rounding_rules` quantum `1`). Totals move by under 1 MKD (D1 168,154.45 -> 168,155 MKD); at the
  EUR 10,000 value-part edge the key is the assessed whole-denar customs value + duty (new golden
  case G17c). Every golden case was recomputed with an independent evaluator before the tests
  changed. Fact sheet open questions 1 and 4 and ADR 0003 (decisions 11 and 12) record it.
  Showing every amount without decimals (the rest of D3) is the calculator change below.

### D2 and D3: two values per vehicle, whole units (calculator)

- `domain/import_estimate.py`: the vehicle's `price` is the REAL purchase price (landed cost,
  profit); the new `estimated_customs_value_eur` + `estimated_customs_value_source` (together;
  positive, two decimals, within the engine bound, source 3-120 characters) carry the `primary`
  scenario's charges (+ transport to the MK place of entry, UNCERTAIN whether Customs adds it);
  with an estimate a `transaction_value` scenario shows the charges on the price paid
  (Царински закон чл. 28). `catalogue_value_eur` and the `customs_revaluation_risk` scenario are
  removed; `legal_base` is renamed `primary`. The result carries `purchase_price` and
  `customs_value_for_duties`; purchase-price bases `recorded_actual` / `recorded_quote` /
  `recorded_estimate` are ready for recorded values. EUR amounts are whole euros (half-up, from the
  exact figures); the calculator view shows every amount without a fraction.
- MCP `deals_estimate_import_costs`, `GET /api/import-costs/estimate` and
  `suv-deals import-costs estimate` take the new inputs (`--estimated-customs-value-eur`,
  `--estimated-customs-value-source`); snapshots `schemas/tools/deals_estimate_import_costs.json` and
  `schemas/api/import-costs.estimate.get.json` regenerated; docs/connect_mcp.md, docs/api_contract.md,
  ADR 0003 (decision 13) and the fact sheet updated. Dashboard test id `totals-legal_base` is now
  `totals-primary`.

### D2: recorded import values per listing (migration `20261013000100`)

- Migration `supabase/migrations/20261013000100_import_values.sql` (forward-only, expand; applied
  to the hosted project on 2026-10-14 as version 20261014001359): `app.cost_evidence` category `customs_value` (the estimated
  customs value; `cost_evidence_customs_value_ck`: listing-scoped, EUR, kind `estimate`) and two
  partial unique indexes that keep each listing's `purchase` / `customs_value` rows one linear
  supersede chain (docs/schema.md 11.16).
- `persistence/import_values_repo.py`: `record_import_values` (signed-in owner with `config:admin`
  only; listing lock, supersedes the current row of each category, audit `import_values.record`,
  invalidates the listing's valuations and queues ONE deduplicated recomputation via the new
  `valuation_repo.invalidate_listing` / `DependencyChange.listing_ids`) and
  `current_import_values` (`deals:read`, current values + short history).
  `valuation_repo.get_cost_evidence` never returns these rows; the cost profile is unchanged.
- API (never an MCP tool): `GET /api/listings/{listing_id}/import-values` (`deals:read`) and
  `POST /api/listings/{listing_id}/import-values` (`config:admin`, body idempotency key bound to the
  listing); snapshots `schemas/api/listings.listing_id.import-values.{get,post}.json`.
- Calculator listing mode: the current recorded purchase price replaces the advertised price
  (basis `recorded_<kind>`) and the recorded estimate is used, fact source `recorded`, unless the
  request sets that fact; notes `RECORDED_*_USED|OVERRIDDEN:<id>`, `listing.recorded_*_id`.
- Valuations: the recorded purchase price is the purchase (`actual` / `quoted` / `estimated`), the
  recorded estimate feeds the shared builder (ACTIVE rules only); both rows are cited (purchase
  line; `tax_cost_lines(input_evidence_ids=...)`) and tracked in `cost_evidence_ids`.

### D2: dashboard, CLI and dot

- Dashboard: the calculator form groups "Two values per vehicle · Две вредности по возило" (real
  purchase price with currency and basis; estimated customs value with its source); listing mode
  shows the recorded or the listing's values as hints; the result shows `purchase_price` and
  `customs_value_for_duties` side by side and a "Scenarios side by side" table (`primary`,
  `transaction_value`). The browser never rounds money; it shows the server's whole-unit strings.
  New "Import values" panel on the candidate detail page (`screens/importValues/`): both current
  values in whole euros with kind, source, document and history, a link to the calculator, and an
  owner-only recording form with a confirmation that it changes the listing's valuation; client
  `importValues` / `recordImportValues`.
- CLI: `suv-deals import-costs record-values` (verified ACTIVE owner, audited, `--yes`; usage
  errors name the options, never the values) and `import-costs values` (read-only); `import-costs
  estimate` prints whole units (an input typed with cents shows its exact value next to it).
- dot: the tool and field descriptions and the server instructions explain the two values, the
  automatic use of a listing's recorded values and that no MCP tool records them (snapshot
  regenerated; description 598 of 600 characters).
- Tests: `tests/integration/import_costs/test_import_values.py`, `tests/api/test_import_values_routes.py`,
  `tests/unit/test_import_values.py`, additions to the calculator, MCP, contract, CLI, constraint and
  valuation-pipeline suites; vitest `importValues.test.tsx` and two-value cases in
  `importCosts.test.tsx`; Playwright `importValues.spec.ts` and the owner's example in
  `importCosts.spec.ts`. `e2e/inquiries.spec.ts` now tolerates "6 h 1 min ago" when a long run
  crosses a minute boundary.

### Review fixes (owner decisions D1-D3)

- A request `price_basis` without a price no longer relabels the owner's recorded purchase price
  (an unpaid estimate shown as an invoice, without its unverified flag): `merge_facts` refuses it
  with `VALIDATION_ERROR` on `price_basis` (a recorded price keeps its recorded kind, as in the
  valuation); the dashboard locks the Price basis select until a price is entered and never sends
  a leftover basis for a recorded price.
- Valuation commit: under the listing lock the pipeline re-reads the listing's current recorded
  import values and refuses to persist a calculation that read others (`VersionConflict`, the job
  is retried with fresh reads), so a purchase price recorded while a queued recomputation was being
  claimed is never lost (`valuation_pipeline._check_import_values`).
- Each scenario carries `customs_value_eur` (whole euros at the customs rate); the dashboard's
  "Scenarios side by side" column "Customs value (EUR)" shows it, so an MKD transaction value no
  longer appears under the EUR header (the scenario section shows the MKD value with the EUR
  equivalent; the CLI prints both). Snapshots regenerated.
- draft.3's `customs_value_mkd` label no longer says "invoice + transport": it names the
  scenario's customs value (transaction value or estimated customs value) incl. transport; content
  hash `4fe7f064...`.
- The calculator's warning for an entered estimate without a source now says what happens: the
  recorded source is not reused and the request is refused without one.

### Integration and documentation

- The calculator's basis, assumption and warning texts and the recorded-value source labels quote
  amounts without a needless fraction (`import_estimate.plain_amount_text`: "invoice 3000 EUR +
  ... transport to MK place of entry 700 EUR", "TRANSPORT_LINE:700 EUR per vehicle", "recorded
  actual purchase price EUR 2800 (...)"); a real fraction stays exact ("2800.50") because the text
  names the figure the calculation used. The same texts appear in valuation warnings.
- Stale names removed across code, snapshots, dashboard test ids and docs (`legal_base` ->
  `primary`; `catalogue_value_eur` / `customs_revaluation_risk` -> the estimated customs value and
  the `transaction_value` scenario; draft.2 -> draft.3). The remaining mentions are history or
  tests that assert the old fields are refused.
- README "Customs calculator", docs/tax_rule_approval.md, docs/connect_mcp.md section 7,
  docs/api_contract.md, docs/runbook.md (`import-costs values` / `record-values`),
  IMPLEMENTATION_STATUS.md, ACTIVATION_GATES.md (sections 3 and 6), docs/schema.md 11.16 (the
  migration, applied to the hosted project on 2026-10-14), the fact sheet and ADR 0003.
- Verification: docs/qa_evidence.md, "Owner decisions D1-D3".

## Customs calculator and dot access (2026-10-13, `787ad7c`, `7280bd9`)

### Customs calculator and dot access (2026-10-13, `787ad7c`)

Owner decisions of 2026-10-13 (docs/decisions/0003-transport-and-customs-calculator.md): transport
is a fixed EUR 700 per vehicle (committed in `da4c5e6`) and a customs/import-tax calculator for
North Macedonia that dot can use. Nothing is approved or activated; no migration.

- Tax engine 1.1 (`tax-engine/1.1.0`, `domain/tax_engine.py`): intermediate (helper) components
  never counted in totals, brackets keyed on money (`key_terms`, `key_currency`) under the customs
  rate checks, law-style `over`/`up_to` bounds, `amount_per_unit`, an integer guard
  (`VALUE_NOT_INTEGER_NO_BAND_RULE`), VAT-only incidental costs (`vat_incidental_costs_total`),
  and `select_preview_rule_set` for a labelled indicative preview. Existing rule sets keep their
  hash. Production selection (`select_rule_set`, `allow_unapproved=False`) is unchanged.
- The North Macedonian DRAFT rule set
  `config/tax_rules/drafts/mk_passenger_car_import_2026.10-draft.1.json` (customs duty, motor-vehicle
  tax ДМВ, import VAT on an HS 8703 car; status `draft`, every open point `UNCERTAIN - confirm`),
  golden-tested to the denar (`tests/unit/test_mk_rule_draft.py`), and the evidence pack
  docs/tax/mk_import_fact_sheet_2026-10.md. `config validate`, `doctor` and `make smoke-local`
  check the drafts directory.
- `domain/import_estimate.py`: vehicle facts, the builder from facts to engine inputs (origin,
  tariff code and VAT base never inferred; CO2 cycle as printed, never converted; the Euro 4 import
  gate as a warning) and the estimate (scenarios: legal base, standard duty if the proof is
  rejected, customs revaluation risk (both replaced in the Unreleased entry above); `indicative_total`
  in preview mode; MK/EN banner; uncertain
  items). `costs.tax_cost_lines` refuses anything but an engine calculation.
- Read service `persistence/queries/import_costs.py` and view `views/import_costs.py`
  (`ImportCostEstimateView`), served as the MCP tool `deals_estimate_import_costs` (dot,
  `deals:read`), `GET /api/import-costs/estimate` with the dashboard "Customs calculator" screen,
  and `suv-deals import-costs estimate`. Read-only: nothing is persisted.
- Owner tooling: the dashboard's Settings section "Tax rules and cost profile"
  (`/api/settings/tax-rules*`, `/api/settings/cost-profiles*`, `config:admin`) stores a repository
  draft, submits it for review, approves it (approver + review record bound to the content hash),
  activates or revokes it, and approves the cost profile; `suv-deals tax-rules list` and
  `tax-rules store`. `valuation_repo.transition_tax_rule_set` gained optional
  `expected_status`/`expected_sha256` checks under the row lock.
- Valuations build their tax inputs with the same builder (still ACTIVE rule sets only): builder
  notes become valuation warnings (`TAX_INPUT_*`, `IMPORT_GATE:`), a customs rate counts only in
  its own month, tax lines resting on advertised prices or caller claims are never
  `rule_supported`, and helper components are flagged `intermediate` in the valuation view
  (snapshots `schemas/valuation.schema.json`, `schemas/tools/deals_get_valuation.json`).
- Docs: README "Customs calculator", ADR 0003, config/tax_rules/README.md, docs/tax_rule_approval.md,
  docs/api_contract.md, docs/connect_mcp.md section 7, docs/runbook.md, docs/architecture.md,
  docs/schema.md 11.15, SECURITY.md, IMPLEMENTATION_STATUS.md, ACTIVATION_GATES.md (`tax_rules` and
  `cost_assumptions` stay `blocked`).
- Verification (docs/qa_evidence.md, "Customs calculator wave"): full suite 8,505 passed + 1
  skipped on PostgreSQL 16 and 8,506 passed on 17; desktop 317; dashboard 208 unit tests, build;
  browser E2E 48 on PostgreSQL 16 and 17 (and the harness's 36).

### Customs calculator review fixes (2026-10-13, `7280bd9`)

- Draft rule set re-versioned to `mk-passenger-car-import@2026.10-draft.2` (draft.1 is no longer
  shipped): a plug-in hybrid without a CO2 document now gets the deemed 400 g/km (ЗДМВ чл. 12(2)),
  halved by чл. 9(3) (`dmv_co2_deemed_phev_petrol` / `_diesel`, column choice UNCERTAIN - confirm).
- Builder: a CO2 figure under 1 g/km for a combustion or hybrid car is "not established" (never
  the Table 2 zero row; a listing's such figure is no fact); an attested HS code that contradicts
  the fuel leaves the fuel-keyed charges unknown (`HS_CODE_FUEL_MISMATCH`, fuel and hs_code
  missing); an implausible first registration (year 0000, age over 200) is a labelled unknown,
  never a 500; a customs rate not valid on the declaration date is named missing
  (`customs_fx_rate`), never shown as the rate used (`customs_fx_source`
  `recorded_not_effective`); a CHF price is missing as `price_in_eur_or_mkd`, not as `price`;
  an alternative group no member covers is named (`RULE_SET_DOES_NOT_COVER_VEHICLE`).
- Engine validation: an unreferenced alternative-group member must be a fixed 0; a bracket base
  counts as a use only with a `rate_of_base` row; repeated `base`/`key_terms` are refused;
  scientific display of extreme decimal exponents (no 100 MB strings); a version closed in any
  copy is excluded from the preview before the content check. JSON-LD numbers with more than 12
  fraction digits are no value.
- Valuations: a customs rate's dependency is recorded even when it was not effective, valuations
  expire when their customs rate's month ends, and the reconciler re-values valuations that read an
  older customs rate or none once a newer one is recorded.
- Labels: the calculator's cost-profile note follows the STORED approval; dot's description and
  instructions describe a preview by rule status, not as "UNAPPROVED draft rules"; the dashboard no
  longer promises to keep the listing's CO2 document for an entered CO2 figure; `doctor` reports the
  stored ACTIVE rule set per workspace and no longer warns on the draft's `TAX_RULE_SET_ID`.

## Owner decisions (2026-10-10 to 2026-10-13, `20fddf8`, `da4c5e6`)

- `outlook_local` is the send and reply path; mobile.de and AutoScout24 are included as
  acquisition sources with `terms_decision: proceed_acknowledged` (sources stay disabled; ADR 0002
  addendum).
- Transport is a fixed EUR 700 per vehicle: cost profile `default` version 3, still unapproved.

## Wave D: final review fixes and handoff documentation (2026-10-10, `cee1a15`, `1e157af`)

`1e157af` recorded the exact-build release verification of `cee1a15`
(docs/qa/cee1a15aaa87583d8e2847bb14253932273855d4/).

### Final verification pass (2026-10-10)

- Every gate re-run on the working tree with fresh counts (docs/qa_evidence.md): lint, format,
  types, schema snapshots, both full Python suites, desktop worker, E2E harness and browser E2E on
  PostgreSQL 16 and 17, dashboard `npm ci`/build/test/lint/audit, `make -n` for every target,
  `--help` for every CLI command the documents name, migration ASCII/`DROP TRIGGER` checks and the
  privacy grep.
- Test fix: the flaky D3 assertion in `tests/integration/v11_runtime/test_reply_escalation.py`
  (a random UUID or millisecond timestamp containing `500`; a non-ASCII token that `json.dumps`
  escaped so it could never fail) now checks a view with ids/timestamps blanked and non-ASCII
  kept, with a test showing both old failure modes. No product code changed.
- Wrong claims corrected: README and docs/runbook.md section 1 said every state-changing CLI
  command needs `--yes` (`crawl once` and the process commands run without it);
  IMPLEMENTATION_STATUS.md said the source register's terms/robots reviews are dated 2026-10-06
  (only three sources have a terms review and only one a recorded robots.txt reading).

### Wave D3: final handoff documentation (2026-10-10)

- README.md, IMPLEMENTATION_STATUS.md (milestones M0-M8 and M7a, U1-U12, what is not done or not
  verified, known limitations, review findings and open items, spec 34 contents and final
  status), ACTIVATION_GATES.md (every gate with prerequisites, owner/operator steps, verification
  and evidence; tooling gaps; PROPOSED values; evidence log), SECURITY.md, this changelog,
  docs/acceptance_matrix.md (spec 31 rows plus the 37.10 delta tests) and docs/qa_evidence.md
  (exact commands and measured counts) written from the code and fresh test runs.
- No source code changed.

### Wave D2: final review findings (2026-10-10)

- Seller-contact evidence producer: detail ingest links the seller, verifies the ad-shown
  recipient and resolves the inquiry language (F1).
- MK market evidence: `suv-deals market import` and `mk_comparable` detail routing (F2).
- Activation canary transport on `outlook_local` (desktop worker list/claim/send/report/reply,
  `/v1/mail-workers/canary-intents`) and the reservation gate `activation_canary_incomplete`,
  shown by `inquiries status`, `doctor` and the dashboard (F3, OPS-04).
- v1.1 activation gates `seller_email_sender`, `seller_inquiry`, `seller_reply_slack_route`
  seeded as `blocked` (F4).
- First handoff documents (F5); the release verifier covers the desktop worker, the dashboard and
  the browser E2E (`--with-e2e`, `--skip-dashboard`; F6, OPS-08).
- Tests for scheduler downtime catch-up (F7) and for a new listing revision arriving before a
  review submit in the browser (F8); the live smoke documented as the gated `crawl once`
  procedure (F9); the vehicle-documents limitation documented (F10).
- One claimant per Outlook send intent and a per-store desktop worker id (SEC-1); crawler INPUT
  firewall rules and fixed bridge names (SEC-3, OPS-07).
- Worker default queues cover every job type (OPS-01); blank optional settings mean unset
  (OPS-02); "not activatable (no adapter)" (OPS-03); doctor requires the Slack settings for reply
  signals (OPS-05); ECB FX refresh by the reconciler, `suv-deals fx refresh` and `fx record`
  (OPS-06); `sender-binding store-secret` for the optional `gmail_api` grant (OPS-09, partial).

### Wave D1: open wave C review items (2026-10-10)

- A resume that removes suppressions must carry the count the owner saw
  (`expected_removable_suppressions`).
- Mailbox health and monitoring account for expired or revoked worker credentials.
- Atomic canary claim (`canaries_repo.claim_for_send`) re-checking every database gate in the
  committing statement.
- A muted coalesced reply signal (its carrier ended `dead_letter`/`cancelled`) is re-emitted once.
- Fixture-lineage refusal at reservation and dispatch in the persistence layer.
- Read-only `GET /api/activation/canary-evidence` and the dashboard rows 4-6.
- The inquiry control view reports the process-level gate; `automatic_inquiries_possible` and
  `sending_possible` also need room under both rolling caps (`caps_leave_room`).

## Wave C: v1.1 review items (2026-10-10, `fa283cc`, `e5d47f4`)

- Migration `20261008000200_inquiry_hardening` (applied to the hosted project).
- C1 persistence: fixture lineage refused at the worker's claim; 7-day seller cooldown floor in
  domain, repository and a CHECK; automatic mode needs a verified configured sender and an active
  authorization; a refusal after a granted claim is never proof of non-submission; reply-signal
  flood control (one undelivered signal per inquiry, per-inquiry 24 h cap, per-mailbox hourly
  ingest cap); credential revocation tombstones mailbox bindings; quarantined text owner-only;
  typed waiting reasons; `jobs resolve-blocked`; the owner-controlled activation canary table.
- C2 interfaces: resume bound to the suppressions the owner saw, candidates screening-rejected
  filter, per-credential reply upload limit, CLI `jobs` and `canary`, doctor checks.
- C3 dashboard: waiting reasons, readiness, signal and credential state, canary evidence.

## Wave B2: v1.1 runtime and interfaces (2026-10-08, `19b709b`, `42f9c4b`)

- Migration `20261008000100_inquiry_quota_backstop` (quota counted at the latest hand-over).
- Jobs `seller_inquiry_plan` / `_send` / `_reconcile` and `seller_reply_process`; `outlook_local`
  intents only to a live, fresh desktop worker; `gmail_api` sends outside transactions; uncertain
  results never resent; `seller.reply.received` to its own Slack category only.
- `/v1/mail-workers/*` for the desktop worker; dashboard inquiry, reply, control, worker health,
  lifecycle and 15-day evaluation routes; MCP `seller_inquiries_get`, `seller_replies_get`,
  `seller_inquiries_pause`; CLI for mail-worker credentials, sender bindings and inquiry controls.
- Dashboard inquiry screens without any approve or send control.

## Wave B1: v1.1 persistence (2026-10-07, `a3b1b79`, `ed55313`, `09a193c`, `8e81128`)

- Contracts and consolidation: inquiry caps bounded at start-up, 85 database guard refusals mapped
  to typed errors, mail-worker wire models matching the desktop worker field by field, fixture
  lineage frozen at ingest, `migrate.sh` without a password in argv.
- Repositories for sellers, inquiries, sender bindings, send intents, mail workers, replies and
  availability; one reservation per real vehicle/seller pair across sites; caps 2/24 h and
  5/15 days; uncertain sends blocked, never requeued.
- Migrations `20261007000100`, `20261007000200`, `20261007000400`; migration 1000 made pure ASCII
  and migration 0200 free of `DROP TRIGGER` for the Supabase connector; the v1.1 migrations
  applied to the hosted project (docs/schema.md section 9).

## Wave 4: dashboard (2026-10-07, `5711c6c`)

- Private React 19 + Vite 8 dashboard: overview, candidates, detail, economics, review queue and
  case, sources, settings; same-origin `/api`; CSP; no secret key in the bundle; idempotent review
  mutations; mock Supabase Auth and a real backend for Playwright E2E.

## Wave 3: runtime, API, MCP, CLI and operations (2026-10-07, `5d311e7`)

- Scheduler slots, discovery/detail/valuation handlers, runner, reconciliation and the outbox
  dispatcher with lease fencing; the dashboard API with Supabase JWT auth; the MCP server
  (Streamable HTTP, read and review tools, native MCP Events); the `suv-deals` CLI; Dockerfile,
  Compose files, backup/restore/verify scripts and the runbook.

## Spec v1.1 wave A: seller-inquiry domain (2026-10-07, `670be85`)

- Inquiry readiness separate from investment readiness, versioned DE/IT/FR/EN templates and the
  Macedonian preview with a scope validator, evidence-based language, exact-listing recipient
  verification, cross-site identity, state machine, rate caps, dispatch preflight.
- Reply classification and correlation by Message-ID headers, claim extraction, Macedonian
  summary, lifecycle and the 15-day evaluation model.
- Migration `20261006001000_seller_inquiries`; MIME builder; Gmail API, Microsoft Graph and
  `outlook_local` providers; the desktop Outlook worker core.

## Wave 2b: repositories and read queries (2026-10-07, `86e41d0`)

- Workspaces, configuration, sources, listings ingest, clusters, evidence, market, FX, tax rule
  sets, cost profiles, valuations, review cases with same-transaction outbox events, notes,
  destination bindings, event subscriptions; read queries for every dashboard and MCP view.

## Switzerland (2026-10-06, `21977a1`)

- CH as an acquisition market: MFK/TUV/revisione wording, MWST/TVA/export wording, CH cost lines,
  disabled CH candidate sources and a CH dealer template (docs/markets/switzerland.md).

## Wave 2a: persistence core and read models (2026-10-06, `0c555bb`)

- Durable jobs with SKIP LOCKED claims, fenced heartbeats and reaper; transactional outbox with
  uncertain handling and fixture blocking; idempotency; audit; frozen query snapshots; host budget
  gate; activation gates; the 12 MCP tool input models and resolved JSON schemas; migration 0950.

## Spec v1.1 adoption (2026-10-06, `e58c688`, `fb65749`)

- Specification v1.1 added; ADR 0002 (additive adoption; the owner's sender mailbox and the
  default `outlook_local` path); inquiry/reply enums, scopes and settings.

## Wave 1b: crawl infrastructure, adapters and integrations (2026-10-06, `d888637`, `b965dc8`)

- Crawl4AI 0.9.4 REST client, URL policy, RFC 9309 robots, rate limits, parser health.
- Generic schema.org dealer adapter, fixture crawl client, adapter registry with activation gates,
  synthetic DE/IT/CH fixture suites, placeholder adapters for unverified marketplaces and the
  optional mobile.de API; MCP Events provider, safe HTTP, secret box, Slack fallback; logging with
  redaction, metrics, audit; docs/source_access_register.md and docs/notification_bridge.md.

## Wave 1a: deterministic domain and database (2026-10-06, `e2c8248`, `3af592e`, `956613a`)

- Parsing, identity, screening, comparables, tax engine, costs, valuation, ranking, reviews,
  notifications, pagination and FX with unit and property tests.
- Migrations 0100 to 0900 with RLS, grants and the backend role; database tests on PostgreSQL 16
  and on 17.11 with a Supabase-like non-superuser owner; `.env.example`; migrations 0100 to 0900
  applied to the hosted project.

## Foundation (2026-10-06, `c6996fe`, `4ccfa24`, `ba6fd67`, `f727e4a`)

- Project skeleton, typed core domain contracts, business-rule profiles, real-PostgreSQL test
  harness, the architecture contract and ADR 0001; the shared SSRF guard and async database
  access; the specification and verified research notes. (`fbb9a8f` initialised the empty
  repository.)
