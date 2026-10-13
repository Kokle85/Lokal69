# North Macedonia: import charges on a used passenger car (HS 8703), as of 2026-10-13

The evidence pack behind the import-cost calculator and the DRAFT rule set
`config/tax_rules/drafts/mk_passenger_car_import_2026.10-draft.2.json`
(`mk-passenger-car-import@2026.10-draft.2`). The owner, with a qualified customs broker or tax
professional, reviews it **before** approving that rule set (docs/tax_rule_approval.md;
decision record docs/decisions/0003-transport-and-customs-calculator.md). Per the owner's
decision of 2026-10-13, transport to MK is fixed at EUR 700 per vehicle.

> **Status: research, not approved, not legal advice.** It was compiled on 2026-10-13 from the
> primary texts and secondary sources cited in section 14. Nothing here is an approved rate. The
> draft rule set encodes sections 1 to 7 and 11; every point still open is marked
> `UNCERTAIN - confirm` in that JSON and listed in section 13. Until the owner approves and
> activates a rule set, valuations keep import charges `unknown` and the calculator shows only a
> labelled **indicative preview** (ИНДИКАТИВНА ПРОЦЕНКА).

How the sections map to the repository:

| Fact sheet | Repository |
|---|---|
| Sections 1, 4, 5, 6, 7 (pipeline, duty, ДМВ, VAT) | the draft's 21 components: `customs_value_mkd`, `duty_*`, `vmv_mkd`, `dmv_*`, `vat_base_mkd`, `vat_import` (helpers are `intermediate` and never counted in a total) |
| Section 2 (inputs) and section 11 (modelling rules) | the builder `src/suv_deals/domain/import_estimate.py` (`VehicleImportFacts` -> `build_tax_inputs`): origin never inferred (no proof = standard duty), CO2 cycle as printed (WLTP when the COC has it, never converted), 400 g/km deemed only when no document exists, the Euro 4 import gate as a warning |
| Section 3.3 (the EUR 700) | the cost profile `config/cost_profiles/default_unapproved.yaml` (transport line EUR 700) and the builder's single transport line: all 700 in the customs value unless a separately invoiced inland leg is given |
| Section 3.4 (SchwackeNet) | the optional `catalogue_value_eur`: a separate "customs revaluation risk" scenario, never a max() |
| Section 10 (golden examples) | `tests/unit/test_mk_rule_draft.py`: D1-D6 are G01-G06; the section 10.3 ДМВ set #1-#19 is checked one by one; tables 1 and 2 are typed in independently of the JSON |
| Section 13 (open questions) | the `UNCERTAIN - confirm` markers of the draft and the calculator's `uncertain_items` |

**Legend**
- **[C]**: confirmed against primary text by the verifier.
- **[C-snip]**: confirmed only through search-engine snippets of official pages. customs.gov.mk and carina.mk serve expired TLS certificates and were not fetched; TLS was never bypassed.
- **[M]**: medium confidence (secondary sources, or inferred from primary law).
- **[U]**: uncertain.
- **[D]**: derived by arithmetic or modelling from confirmed rules; not published by any authority.

**Research limits.** Three further checks could not be run during research (the search budget was
exhausted): whether the ЗДМВ was amended in 3/2025, the Euro 5 status after April 2026, and any
2026 change to the ДМВ decree. The remaining conflicts were resolved from primary texts already
downloaded and by reasoning (section 12):
- ЗДДВ consolidated text, Art. 21.
- The 2025 Customs Tariff draft, Arts. 1 and 3. Art. 3 says: "влегува во сила осмиот ден од денот на објавувањето ... а ќе отпочне да се применува од 1 јули 2025 година".

Every number below was recomputed.

---

## 1. Calculation pipeline (order of operations)

All charges are assessed by the Customs Administration on the import declaration (ЕЦД). They are paid together and must be paid before the car is released. The duty rate, customs value and exchange rate are those valid when the customs declaration is accepted (Царински закон Arts. 77, 216(2), 230(1)) [C].

| Step | Quantity | Formula | Legal basis |
|---|---|---|---|
| 1 | Customs value CV (MKD) | (transaction price + transport/insurance/handling up to the place of entry into MK) × FX | Царински закон Arts. 28, 35(1)(д), 36(а), 43 |
| 2 | Customs duty | rate × CV (rate 5%, 0% or 7%; see section 4) | Закон за Царинската тарифа Art. 3(1); Царински закон Art. 19 |
| 3 | ДМВ (motor-vehicle tax) | (ВМВ_EUR × пВВ) × СКД + CO2 × кCO2, where ВМВ = CV + duty | ЗДМВ Arts. 9, 10(2); Уредба 277/2019 Art. 2 as amended by 220/2023 |
| 4 | Import VAT base | CV + duty + ДМВ + other import charges/fees (excluding VAT) + incidental costs up to the first destination in MK not already in CV | ЗДДВ Art. 21(1)-(2) |
| 5 | Import VAT | 18% × VAT base | ЗДДВ Arts. 28-29 |
| 6 | Total import charges | duty + ДМВ + VAT | |

There is no circularity: VAT is not part of the ДМВ base (ЗДМВ Art. 10(2)), and VAT itself is excluded from the VAT base (ЗДДВ Art. 21(2)(1)) [C]. Excise on cars was abolished from 1.1.2020 (section 8).

---

## 2. Inputs the calculator needs per vehicle

| Input | Used for | Notes |
|---|---|---|
| Purchase price and currency (EUR/CHF) | CV | Invoice or sales contract is mandatory |
| Transport split of the EUR 700: part up to the MK place of entry vs. inland leg | CV vs. VAT base | Default: all 700 in CV (conservative); see section 3.3 |
| Optional customs/catalogue value (SchwackeNet) | Risk scenario only | Not the legal default; see section 3.4 |
| Country of manufacture (VIN WMI) and proof of origin held (EUR.1 / origin declaration / US-origin evidence / none) | Duty rate | Default: no proof, so 5% |
| HS subheading / drive type: ICE 8703 21-24, 31-33, 90; full hybrid 8703 40/50; plug-in 8703 60/70; BEV 8703 80; double-cab pick-up 8704 | ДМВ scope and reductions | Customs decides classification (ЗДМВ Art. 4(3)) |
| Fuel (petrol/LPG/CNG vs diesel) | кCO2 column | |
| CO2 g/km from the COC, flagged NEDC or WLTP (or both) | ДМВ | Missing CO2 means 400 g/km is deemed |
| Euro class | Import/registration gate (minimum Euro 4) | Not a ДМВ input for M1 |
| Year of manufacture / first registration | Oldtimer check (35+ yrs), eco fee at registration | Not a ДМВ input for M1 |
| Engine cm3, kW | Registration-stage fees (road fee, eco fee, MTPL) | Not ДМВ inputs for M1 |
| Importer status: private / non-VAT-registered business / VAT-registered dealer | VAT recoverability, deferral, resale VAT | |

---

## 3. Customs value and currency

### 3.1 Transaction value [C]
- **Basis:** Царински закон, Сл. весник РМ 39/2005, amended by 4/2008, 62/2009 and 117/2009 (Constitutional Court decisions), 35/2010, 48/2010, 158/2010, 44/2011, 53/2011, 11/2012, 171/2012, 187/2013, 180/2014, 15/2015, 129/2015, 154/2015, 192/2015, 23/2016, 144/2018, and Сл. весник РСМ 110/2021 (penal provisions, 19.5.2021).
- No new UCC-aligned Customs Law was found adopted by October 2026. A draft was announced in June 2025 [M]. If a new law has been adopted, the article numbers in this sheet will change.
- **Art. 28(1):** „Царинската вредност на увезената стока е трансакциската вредност, а тоа е вистински платена цена или цена која треба да се плати за стоката кога е продадена за извоз во царинското подрачје“. It is subject to conditions (а)-(г): no restrictions, no unquantifiable conditions, no proceeds to the seller, and parties not related.

### 3.2 Additions and exclusions [C]
- **Art. 35(1)(д)(1)-(2) adds:** „трошоците за транспорт и осигурување на увезената стока до местото на внесување во царинското подрачје“, plus loading, unloading and handling up to that point. Under Art. 35(2), additions must rest on objective and quantifiable data. Under Art. 35(3), nothing else may be added.
- **Art. 36(а) excludes:** transport after arrival at the place of entry, but only „кога посебно се одвоени“ (shown separately from the price).
- **Art. 36(ѓ) excludes:** import duties and other charges payable in MK.

### 3.3 Fixed EUR 700 transport: treatment [C law / D modelling]
- **Customs value** (and so the duty base and ДМВ base): includes the part of the EUR 700 incurred up to the MK place of entry. The inland leg can be excluded only if it is separately invoiced.
- **VAT base:** includes all transport and insurance up to „првото место на крајната дестинација на доброто во земјата“ (ЗДДВ Art. 21(2)(2)). So the whole EUR 700 is always in the VAT base, either through CV or as an incidental cost.
- **Calculator default:** put all 700 into CV. This is conservative and matches a door-to-door lump sum with no split. Offer a configurable `transport_after_entry_eur` (default 0). How Customs treats a lump-sum door-to-door price in practice is an open question.

### 3.4 Customs valuation practice for used cars: SchwackeNet [M practice, not law]
- **What was announced:** from mid-2018, Customs values used passenger cars, combined vehicles, light commercial vehicles and motorcycles using the electronic SchwackeNet catalogue, looked up by chassis number (VIN). Lider, 03.07.2018, quoting the Customs announcement: „Проценката на вредноста на употребуваните моторни возила се врши со користење на електронскиот каталог SchwackeNet“. There is no fee („нема да се наплаќа надоместок“), and compulsory photographing of vehicles was abolished.
- **What importers report:** Bloomberg Adria, 3.6.2024: „возило купено за 15.000 евра по каталог чини околу 19.000 евра“. delcevo365.mk, 2025: bought for 7,000 EUR, Schwacke value 9,500 EUR, all charges computed on 9,500.
- **What the law says:**
  - If the invoice is missing for justified reasons, or there are „основани сомневања“ about it, the value is set under Arts. 29-34 (Art. 42(3)) [C]. The importer may ask for a written explanation (Art. 42(4)).
  - Art. 34(2) forbids (б) „систем кој овозможува ... прифаќање на повисоката од две алтернативни вредности“, (ѓ) „минимални царински вредности“ and (е) „произволни или измислени вредности“ [C].
- **Calculator rule [D]:**
  - Legal base case: CV = invoice + transport to entry.
  - Optional risk case: if the user enters a catalogue value, compute a separate scenario with that value as CV. Label it "risk estimate (customs may revalue)". Never present max(invoice, catalogue) as the legal customs value.
- **Effect on ДМВ:** the value component only bites if CV + duty exceeds EUR 10,000.

### 3.5 Currency conversion and FX
- **Customs value [C]:** Art. 43: „странската валута се пресметува во домашна валута според средниот курс утврден од Народната банка ... Начинот на примена ... го пропишува министерот за финансии“. The method is set by the Правилник за начинот на примена на средниот курс во постапка на утврдување на царинска вредност (Сл. весник РМ 117/2009); its text was not read.
- **Monthly customs list [C-snip]:** Customs publishes a monthly „Курсна листа што се применува за утврдување на царинска вредност“, effective from the 1st of each month.

| List valid from | EUR | USD | GBP |
|---|---|---|---|
| 01.10.2026 | 61.5337 | 53.6803 | 71.7343 |
| 01.09.2026 | 61.4950 | 53.1228 (single source, not re-seen by verifier) | |
| 01.08.2026 | 61.4988 (single source) | | |

  The CHF rate was not seen.
- **ДМВ [C]:**
  - The ЗДМВ (Art. 9(1)) converts EUR values at the NBRSM middle rate „на денот на увозот“.
  - The Уредба (Art. 2) defines СКД as the NBRSM middle rate „на денот на пресметката“.
  - Neither refers to the monthly customs list.
  - For this project's cars (ВМВ ≤ EUR 10,000) the value component is 0, so this FX question does not change ДМВ. The CO2 component is fixed directly in MKD.
- **Calculator rule [D]:** keep FX as a dated table with its source. Default: the customs monthly list for CV, duty and VAT, with an override. Use the same rate for СКД unless a daily NBRSM rate is supplied.
- **Rounding [U]:** no statutory rounding rule was found for duty, ДМВ or VAT. The official Customs ДМВ guide (29.12.2019) shows 15,500 × 4% × 61.49 = 38,123.8 printed as 38,124 MKD, which suggests whole-denar rounding in practice. The derived examples below keep 2 decimals; rounding each charge to whole denars changes totals by less than 2 MKD.

---

## 4. Customs duty

### 4.1 Rates [C unless marked]

| Branch | Rate | Condition | Legal basis |
|---|---|---|---|
| Default (MFN / conventional), new and used, all 8703 subheadings (petrol, diesel, hybrid, EV) | **5%** of CV | No accepted preferential proof | Закон за Царинската тарифа Art. 3(1) and the annexed tariff. WITS/TRAINS reporter 807: min = max = 5 on every 8703 line. Customs: „царинската стапка за увоз на патнички моторни возила изнесува 5 отсто од царинската основица“ (Bloomberg Adria, 3.6.2024). trade.gov (2026-02-09): "new and used automobiles is five percent" |
| EU preferential origin | **0%** | Accepted EUR.1 or origin declaration; on the declarant's request | SAA (OJ L 84, 20.3.2004) Art. 18(3), Annex II (8703 listed), Art. 128; Царински закон Art. 19(3)(г), 19(4). Customs [C-snip]: „Доколку возилата се со потекло од земјите членки на Европска унија и за истите е добиен доказ за потекло - Уверение за движење ЕУР.1 или Изјава во фактура (Изјава за потекло) тогаш царинската стапка изнесува 0% (редовна 5%).“ |
| EFTA (incl. CH), Turkey, UK, CEFTA, Ukraine preferential origin | **0%** | That agreement's valid proof of origin | EFTA-MK FTA (in force 1.5.2002) Art. 4, Annex III; MK-Turkey FTA (in force 1.9.2000); UK-MK PTCA (2021, Protocol 4 amended by Decision 1/2026); CEFTA 2006 [M] |
| **US origin** (e.g. BMW X5/X6 built in Spartanburg, WMI 5UX/4US; Mercedes M/GL/GLE/GLS built in Tuscaloosa, WMI 4JG) | **0% („слободно“)** | On request, origin conditions met (Art. 19(4)) | New Art. 2(5) of the Закон за Царинската тарифа, added by Сл. весник РСМ 130/25 (adopted 24.6.2025; applied from 1.7.2025 per draft Art. 3): „По исклучок од став 1 на овој член, за стоката со потекло од Соединетите Американски Држави се применува царинска давачка „слободно“ („сл“) во согласност со член 19 став (3) точка д) од Царинскиот закон.“ Rule: high confidence (draft text read, adoption reported by press and trade.gov). **Practice for used US-built cars shipped from DE/IT: [U].** trade.gov (Feb 2026) says certificate-of-origin procedures were still being negotiated |
| Japan, Korea, China, Mexico and other non-FTA origins | **5%** | Buying in the EU does not confer EU origin | Origin rules; Царински закон Arts. 19, 23 [M] |
| Collector vehicles of historical/ethnographic interest (HS 9705) | **7%** | Not relevant to 10-20-year-old SUVs | Customs [C-snip] |
| Government-listed non-WTO/non-MFN origins | Tariff rate +70% (30% on „слободно“ lines) | Country list not found | Закон за Царинската тарифа Art. 2(4); low relevance |

### 4.2 Legal basis and effective dates
- **Закон за Царинската тарифа:** Сл. весник РМ 23/03, 69/04, 10/08, 160/08, 35/10, 11/12, 93/13, 44/15, 81/15, 192/15, 124/19; Сл. весник РСМ 39/22, 130/25, 161/2026. One consolidated text also lists 17/2008.
- **Art. 5:** the yearly Government nomenclature decision „не може да се менуваат стапките на царина“, so only an amending law can change the 5% [C].
- **Сл. весник РСМ 161/2026** (in force 16.07.2026): changes about 70 raw-material and input lines and does **not** touch 8703 [M]. The final text was not read; the June 2026 draft and news reports were. The President had refused to sign it over retroactivity, and Parliament re-adopted it 71-11 on 15.07.2026.
- **5% for used cars:** used cars were cut from 10% to 5% on 4.1.2010 (Global Trade Alert). The legal act is not identified; the earlier attribution to 35/2010 is unsupported and has been dropped.
- **EU 0%:** derived from SAA Annex II read with Art. 128, with the Interim Agreement in force on 1.6.2001. The timetable gives 20% of the basic duty from 1.1.2010 (1%) and 0% from 1.1.2011 [C treaty reading; date derived]. Older Customs texts saying "1% (regular 5%)" date from 2010.

### 4.3 Preferential origin and proof [C, with updated references]
- **"Origin" means preferential origin, not brand or country of purchase.**
  - SAA Protocol 4 was replaced by SA Council Decision No 1/2021 (OJ L 406, 16.11.2021). Its Art. 1 refers dynamically to the PEM Convention.
  - Since **1.1.2025** the operative rules are the revised PEM Convention ("2023 rules", Decision 1/2023 of the PEM Joint Committee, OJ L 2024/390).
  - The transitional Appendix A of Decision 1/2021 lapsed under Protocol 4 Art. 2(2). Its chapter 87 rule ("non-originating materials ≤ 45% of ex-works price") is therefore not the confirmed current rule for 8703; the 2023-rules product-specific rule was not verified.
- **Proofs:** EUR.1, or an origin declaration on a commercial document.
  - Any exporter may make the declaration for consignments up to **EUR 6,000**; an approved exporter may make it for any value.
  - Validity: **10 months**.
  - A EUR.1 can be issued retrospectively within 2 years of exportation.
  - The exporter must be able to produce the supporting evidence on request.
  - The no-proof exemptions (EUR 500 small consignments / EUR 1,200 traveller luggage, non-commercial) do not cover these imports.
- **On request only:** Царински закон Art. 19(4): „Наместо царинската стапка ... на барање на декларантот се применуваат мерките од ставот 3 точки г), д) и ѓ) ... кога предметната стока ги исполнува пропишаните услови.“ Without accepted proof the rate is 5%.
- **Practicality [U]:** one German export dealer reports that almost all makers (BMW, Mercedes, VW, Audi) stopped issuing supplier declarations, so private sellers and ordinary dealers usually cannot support a proof. The same page also offers to obtain a EUR.1. No official confirmation was found.
- **Calculator default:** 5%. Apply 0% only when the user marks a held proof (EU/EFTA/TR/UK/CEFTA/UA) or US-origin evidence. Show the 5% alternative next to it.
- **Swiss purchases:** an EU-built car bought in CH needs a valid PEM proof (cumulation or retained origin); otherwise 5%. Feasibility in practice is unverified [U].
- **Recovery risk:** if origin verification fails later, the 5% duty can be recovered.

---

## 5. Motor-vehicle tax (Данок на моторни возила, ДМВ)

### 5.1 Legal basis [C]

**Law: Закон за данок на моторни возила (ЗДМВ), Сл. весник РСМ 261/2019 of 18.12.2019.**
- In force on publication; applied from **1.1.2020** (Art. 42): „Овој закон влегува во сила со денот на објавувањето ... а ќе започне да се применува од 1 јануари 2020 година.“
- Amended by **Сл. весник РСМ 3/2025** of 03.01.2025 (adopted 27.12.2024). Art. 1 only renames the ministry in Art. 15(2), the diplomatic-exemption certificate: „Министерството за надворешни работи“ becomes „Министерството за надворешни работи и надворешна трговија“ [C-snip/M]. Its entry-into-force date was not read.
- The other_charges verifier doubted this amendment and suspected confusion with the VAT 3/25 amendment. Section 12 explains the resolution. Either way it does not affect the calculation.

**Decree: Уредба за начинот на пресметка на данокот на моторни возила и на износите потребни за пресметка на данокот на моторни возила, Сл. весник РСМ 277/2019 of 28.12.2019.**
- In force 29.12.2019; applied from 1.1.2020 (Art. 5).

**Amending decree: Сл. весник РСМ 220/2023 of 19.10.2023, item 3831 (Government no. 41-6435/10, session 17.10.2023).**
- In force **27.10.2023** (8th day after publication).
- Replaced Table 2 and Art. 2(2). The old Art. 2(2) raised the CO2 coefficients by 25% in 2021 and 50% from 1.1.2022; it was replaced by the WLTP/NEDC column rule.
- No later amendment to the law or the decree was found as of Oct 2026. 2026 broker calculators still cite 277/19 + 220/23 [M: absence not verifiable in the gazette index].

**Return form:** Правилник, Сл. весник РСМ 279/2019 (applied from 1.1.2020). It is a white A4 paper form; electronic filing is possible under Art. 26.

### 5.2 Taxable event, payer, collection [C]
- **Scope:** new and used vehicles imported, first released for free circulation, or first registered (Art. 6(1)). This covers 8703 21, 22, 23, 24, 31, 32, 33 and 90 (except ambulances, vans and hearses), hybrids 8703 40/50/60/70, and double-cab pick-ups whatever their code. A used vehicle is any vehicle other than a new, never-registered one (Art. 3).
- **Taxable event:** at import, „во момент на настанување на царинскиот долг“ (Art. 8(1)(3)).
- **Payer:** the importer (Art. 7(1)(3)). Several liable persons are jointly and severally liable.
- **Assessment and payment:** Customs „го утврдува и наплатува“ the tax (Art. 9(10)). The return is attached to the import declaration, and payment is made „исто како и за останатите увозни давачки“ (Art. 13(6)).
- **Registration:** the car cannot be registered without proof of payment or exemption (Art. 13(7)).

### 5.3 Formula [C]
Law, Art. 9(2): ДМВ is the sum of a value component and a specific (CO2) component.

Decree, Art. 2(1): **ДМВ = (ВМВ × пВВ) × СКД + CO2 × кCO2**

| Symbol | Meaning |
|---|---|
| ДМВ | tax in MKD |
| ВМВ | vehicle value in EUR; at import, customs value + customs duty (ЗДМВ Art. 10(2): „царинската вредност ... зголемена за износот на царината“) |
| пВВ | Table 1 percentage |
| СКД | NBRSM middle EUR/MKD rate |
| CO2 | average combined-cycle CO2 in g/km |
| кCO2 | MKD per g/km from Table 2, by band and fuel |

- One band coefficient multiplies the full CO2 value; it is not marginal.
- For M1 cars there is **no** element for age, depreciation, engine displacement, power or Euro class, and **no** minimum or maximum. The Art. 9(8) flat 5,000 MKD applies only to motorcycles. Sites that claim ДМВ depends on age or kW for cars contradict the law.

### 5.4 Table 1: value component (unchanged since 2019) [C]
The percentage applies to the **whole** ВМВ. The bands are printed as „над / до“.

| ВМВ (EUR) | пВВ |
|---|---|
| up to and including 10,000 | 0% |
| over 10,000 to 12,000 | 1% |
| 12,000-14,000 | 2% |
| 14,000-16,000 | 4% |
| 16,000-18,000 | 6% |
| 18,000-20,000 | 8% |
| 20,000-23,000 | 10% |
| 23,000-26,000 | 12% |
| 26,000-29,000 | 14% |
| 29,000-32,000 | 16% |
| 32,000-35,000 | 17% |
| 35,000-40,000 | 18% |
| 40,000-50,000 | 19% |
| over 50,000 | 20% |

- **Project segment [D]:** the value component is 0 unless CV + duty exceeds EUR 10,000. At 5% duty that means CV above EUR 9,523.81; at 0% duty, CV above EUR 10,000.
- An October 2023 proposal to charge 1% on cars up to EUR 10,000 was **not adopted** [C].

### 5.5 Table 2: CO2 coefficients in force since 27.10.2023 (Сл. весник 220/2023) [C]
кCO2 is in MKD per g CO2/km.

| WLTP band (g/km) | NEDC band (g/km) | Petrol / LPG / CNG | Diesel |
|---|---|---|---|
| 0-1 | 0-1 | 0 | 0 |
| 1-63 | 1-50 | 8 | 15 |
| 64-94 | 51-75 | 15 | 30 |
| 95-113 | 76-90 | 30 | 60 |
| 114-123 | 91-100 | 45 | 90 |
| 124-135 | 101-110 | 60 | 120 |
| 136-153 | 111-130 | 75 | 150 |
| 154-160 | 131-140 | 105 | 195 |
| 161-172 | 141-150 | 180 | 270 |
| 173-188 | 151-170 | 265 | 375 |
| 189-210 | 171-190 | 390 | 510 |
| 211-249 | 191-225 | 495 | 605 |
| 250-282 | 226-255 | 590 | 750 |
| ≥283 | ≥256 | 695 | 880 |

**Which column to use (Art. 2(2) as replaced in 2023) [C].** The rule is based on what the COC states:

| COC carries | Use |
|---|---|
| WLTP only | WLTP bands |
| NEDC only | NEDC bands (typical for cars first registered about 2006-2016) |
| Both | WLTP bands: „ќе се користи податокот ... според WLTP“ |

**Band boundaries [C text / D interpretation].**
- The bands are printed as „над X до Y“, and each lower figure is the previous upper figure + 1.
- Read literally, some integer values fall in no band:
  - NEDC: 51, 76, 91, 101, 111, 131, 141, 151, 171, 191, 226, 256.
  - WLTP: 64, 95, 114, 124, 136, 154, 161, 173, 189, 211, 250, 283.
- **Calculator rule:** treat each printed lower figure as inclusive. For example, NEDC 191 diesel falls in band 191-225 at 605. Flag this as an interpretation.
- Non-integer CO2 values (e.g. 190.5) have no rule. Flag them and require user confirmation; rounding up is the conservative option.

**Missing CO2 [C].** If CO2 cannot be established from the documents listed in Art. 12(1), it is deemed to be 400 g/km (Art. 12(2): „се смета дека износот на емисија на јаглерод диоксид - CO2 ќе изнесува 400 гр./км“). This gives 400 × 880 = **352,000 MKD** for diesel and 400 × 695 = **278,000 MKD** for petrol.
- The documents Art. 12(1) accepts are: COC; consent for registration; a certificate from the manufacturer or its authorised representative; or a certificate from the identification and technical-assessment body.
- The figure required is combined-cycle CO2 under NEDC (or correlated NEDC). US vehicles also need EPA Tier 2 proof.
- An unknown Euro class defaults to Euro 1 (Art. 12(3)); this is irrelevant to M1 ДМВ.

**Historical tables (regression of old examples only; not for 2026) [C].** All are NEDC.
- 2019 bands: 0-0, 1-50, 51-75, 76-90, 91-100, 101-110, 111-130, 131-140, 141-150, 151-170, 171-190, 191-225, 226-255, 256+.
- Petrol coefficients: 0, 5, 10, 20, 30, 40, 50, 70, 100, 130, 160, 190, 220, 250.
- Diesel coefficients: 0, 10, 20, 40, 60, 80, 100, 130, 160, 190, 220, 250, 280, 320.
- 2021: ×1.25. 01.01.2022-26.10.2023: ×1.5.

### 5.6 Special cases [C]

| Vehicle | ДМВ | Basis |
|---|---|---|
| Plug-in hybrid 8703 60/70 | 50% of the calculated tax | Art. 9(3) |
| Full / mild hybrid 8703 40/50 | Full tax, no reduction | Art. 6(1)(2) al. 2. Fuel column = ICE fuel: inferred [U] |
| Battery-electric 8703 80 | Not subject (0) | Art. 6(3)(1) |
| Oldtimer status (35+ years, certified by Customs within the import procedure) | Exempt | Art. 17(1)(3), (4)-(6) |
| Double-cab pick-up classified in 8704 | kW × М (Уредба Table 3). М in MKD/kW by Euro level (>Euro IV / III / II / I): 0-80 kW 120/180/240/300; 80-140 kW 150/210/270/330; >140 kW 180/240/300/360 | Art. 9(4)-(5); Уредба Art. 3 |
| Other exemptions and refunds | Diplomatic (Art. 15), donor/IPA (16), sport and ≤30-day temporary import (17), export of new vehicles within 120 days (19), refunds for new taxis 25%, 8-9 seats 80%, motor homes 80% (21). **No exemption for disabled persons or returning citizens** | Arts. 15-22 |

**Post-clearance [C].** Customs re-assesses the difference by decision if the declared data (e.g. CO2) prove untrue (Art. 11(4)). Untrue data is also a misdemeanour (Art. 34).

---

## 6. Import VAT (ДДВ)

### 6.1 Legal basis [C]
- **Law:** Закон за данокот на додадена вредност, Сл. весник РМ 44/99 ... 198/18; Сл. весник РСМ 98/19, 124/19, 275/19, 267/20, 163/21, 57/22, 199/23, 3/25, 247/25, 267/25. The UJP consolidated text is valid from 30.12.2025. No 2026 amendment was found; slvesnik.com.mk could not be checked.
- **2025-2026 changes that do not affect cars:**
  - 247/25: EUR 22 low-value exemption limited to gifts between individuals.
  - 267/25: 5% housing rate extended to 31.12.2028.
  - 2026 fuel-VAT decisions under Art. 30-в cover HS 2710 only.
- **Taxable event:** Art. 2 т. 2 „Увозот на добра“; Art. 15(1): goods brought across the customs line.

### 6.2 Rate and base [C]
- **Rate: 18%** (Arts. 28-29: „Општата даночна стапка од 18% се применува врз целокупниот промет и увоз, освен ...“). No reduced rate covers 8703.
- **Base, Art. 21(1):** „Даночна основа за увозот на добра е вредноста на увезеното добро утврдена според царинските прописи.“
- **Art. 21(2) adds:**
  1. „Царината, даноците, вклучувајќи ги акцизите, таксите, другите увозни давачки, освен данокот на додадена вредност“
  2. „Зависните трошоци, вклучувајќи ги трошоците за провизии, пакување, превоз и осигурување кои настануваат до првото место на крајната дестинација на доброто во земјата ...“

### 6.3 ДМВ is in the VAT base [C statutory reading; no explicit administrative text]
- **Why it is included:**
  - ЗДДВ Art. 21(2)(1) includes „даноците ... другите увозни давачки“.
  - ЗДМВ makes ДМВ an import levy: it arises at the customs debt (Art. 8(1)(3)), Customs assesses and collects it (Art. 9(10)), and it is paid „исто како и за останатите увозни давачки“ (Art. 13(6)).
  - The excise it replaced was explicitly in the base.
- **Numerical check:** all 30 rows of the automedia.mk January 2020 table reproduce as (customs value + ДМВ/61.5) × 1.18 within ±0.8 EUR. Without ДМВ, the rows miss by up to about 328 EUR.
- **The apparent conflict:** an older Customs FAQ gives the base as „царинската основица зголемена за износот на пресметаната царина“. That FAQ covers EVs and historic vehicles „за наведениот тип на моторни возила“, which pay no ДМВ, so it is not counter-evidence.

### 6.4 Timing and collection [C]
- **Tax debt:** arises on the day the customs debt arises (Art. 31(6)(1)).
- **Customs calculates it:** Art. 40(4).
- **Payment:** at the same time as the customs duties (Art. 43(5)).
- **On the declaration:** shown separately on the customs declaration (Art. 43(6)).
- **Release:** goods are released only after VAT is paid (Art. 43(7)).
- **Same for every importer:** the debtor is the person importing (Art. 32 т. 2). Base and rate are the same for a private person and a company.

### 6.5 Recovery and business rules [C unless marked]
- **Deduction:** a VAT-registered person may deduct import VAT shown on the customs declaration (Arts. 33(2)(3), 34(1)).
- **M1 bar and the dealer exception (Art. 35 т. 2):**
  - Import VAT on M1 cars is **not deductible**.
  - **Exception:** the car is used in the calendar year exclusively for „а) промет со наведените превозни средства“ (trading), or for rental, transport, driver training, or maintenance and repair.
  - So a dealer buying exclusively for resale can deduct.
  - Import VAT is a final cost for private persons, for non-registered persons (Art. 51(3)), and for companies using the car otherwise.
- **Deferral [M]:** under Art. 43(9) and the Правилник за спроведување на ЗДДВ (Сл. весник РСМ 18/2024, Art. 14 per secondary sources), an entitled importer may defer payment to the 15th day after the end of the tax period. Conditions: a request, a bank guarantee, and a clean 2-year import-duty record.
- **VAT registration:** mandatory above **2,000,000 MKD** turnover (Art. 51(1)), with a minimum of 3 calendar years registered (Art. 51(8)). A private person who regularly buys and resells is a taxable person (Art. 9(1)).
- **Resale: margin scheme.** Art. 19(1)(4): used goods including used motor vehicles are taxed on the margin (selling − purchase price) where no VAT was owed on the purchase. The margin is VAT-inclusive (Art. 19(3)), so the calculation rate is 18/118 = 15.2542%.
- **Resale: floor for imported used cars.** **Art. 19(7)**, added by Сл. весник 199/23, **in force 25.09.2023**: „првиот и секој понатамошен промет со увезени употребувани патнички моторни возила извршен во рок од три години од денот на увозот, даночната основа не може да биде пониска од вредноста што ја одредил надлежниот царински орган при увозот“. It does not apply to cars imported before 25.09.2023.
- **Temporary import:** VAT-exempt only while the car is under temporary admission (Art. 27-в). This cannot be used to zero VAT on free-circulation imports.

---

## 7. Worked formula summary (single vehicle, MKD)

```
CV        = (price_EUR + transport_to_entry_EUR) × FX_customs
duty      = rate × CV                      rate ∈ {0.05 default, 0 with accepted proof / US-origin evidence, 0.07 for 9705}
VMV_EUR   = (price_EUR + transport_to_entry_EUR) × (1 + rate)
pVV       = Table1(VMV_EUR)                0 if VMV_EUR ≤ 10,000
kCO2      = Table2(CO2, column = WLTP if COC has WLTP else NEDC, fuel)   CO2 := 400 if undocumented
DMV       = VMV_EUR × pVV × SKD + CO2 × kCO2
DMV       = 0.5 × DMV if plug-in hybrid; 0 if BEV or oldtimer exemption
VAT_base  = CV + duty + DMV + other_import_charges + transport_after_entry_EUR × FX
VAT       = 0.18 × VAT_base
TOTAL     = duty + DMV + VAT
```

---

## 8. Other charges

| Charge | Status | Amount | Basis / confidence |
|---|---|---|---|
| Excise on passenger cars | **Abolished** for declarations registered from 1.1.2020 | 0 | Закон за акцизи Сл. весник 108/2019 („Патничките автомобили не се предмет на оданочување со акциза“); ЗДМВ Arts. 41-42 [C]. Pre-2020 value bands are historical only |
| Customs processing fee | None found for ordinary vehicle clearance | Model 0, configurable | Царински закон Art. 12 (fee ≤ actual cost); Правилник за видот и износот на царинските надоместоци, Сл. весник 89/2007, 107/2008, 192/2014, 46/2015 (amounts not read) [U] |
| SchwackeNet lookup | Free | 0 | Customs 2018 announcement [C] |
| Eco fee (еколошка такса) | **At registration, not at customs** | M1: 150-700 MKD by age and cm3 bands (≤750, 751-1,400, 1,401-2,000, >2,000). Up to 5 yrs: 250/300/400 MKD for 751-1,400 / 1,401-2,000 / >2,000; 6-10 yrs 250-500; 11-20 yrs 350-500; over 30 yrs 450-700; oldtimers 300 | Закон за животна средина amendment, Сл. весник РСМ 89/2022 (11.04.2022), Art. 46 [U]. Libertas reports 400-700. The full table and in-force date were not verified |
| Road fee (патна такса) | Annual, at registration | 2009 base (Сл. весник 118/2009, Art. 4): ≤900 cm3 730; 901-1,350 1,060; 1,351-1,800 1,530; 1,801-2,500 2,040; 2,501-3,150 3,040; >3,150 4,050 MKD. Reported July 2014 increase: 1,520 / 2,180 / 2,910 for the 901-1,350 / 1,351-1,800 / 1,801-2,500 bands | Base [C]; 2014 [U] |
| MTPL insurance | At registration | From 25.04.2024 (+15.4%): 44-55 kW 6,292; 55-66 kW 7,115; 66-84 kW 8,233; 84-110 kW 9,820; >110 kW 11,878 MKD | National Insurance Bureau via press [M]. 2025-26 changes unknown |
| Conformity / compliance confirmation (new regime from 1.9.2025) | Before first registration | Authorised representative's confirmation 40-50 EUR (reported) | Ministry of Energy, Mining and Mineral Resources rulebook; gazette number not found [U] |
| Identification and technical-condition assessment, technical inspection, plates | Before first registration | About 2,000 MKD inspection (estimate); dealer list: inspection 90 EUR, plates and stamps 30 EUR | [U] market |
| Customs broker / spedition / terminal | Market | Transit document 20-30 EUR; terminal about 20 EUR; DE export declaration about 75 EUR; EUR.1 issuance about 50 EUR; homologation + admin + spedition (incl. WVTA) about 200-300 EUR (Racin, 30.04.2026) | [U] undated market prices. Model as configurable; suggested default about 150 EUR broker/terminal + about 50 EUR conformity |
| Old "eco fee 3,000-5,000 MKD at customs" (old Customs FAQ) and a 2005 Government decision under Закон за животна средина Art. 179 listing used vehicles 8702/8703 for an environmental fee on import (FAOLEX mac209035) | **Unverified / probably outdated** | None modelled | Open question. If such a fee is collected at import, it enters the VAT base (Art. 21(2)(1)) |

---

## 9. Import and registration conditions (separate from charges)

| Condition | Rule | Basis / confidence |
|---|---|---|
| **Minimum Euro 4** for used cars | Euro 3 and below cannot be imported/registered since **1.7.2016**. Customs (2024): „постои законска одредба која не дозволува да се регистрираат возила со норма пониска од ЕУРО 4“. RFE (27.01.2026): still Euro 4 | Practice high confidence; **legal act and gazette number not found**. It is not in the Закон за возила, which has only the optional eco-sticker of Art. 5-б [U legal basis]. Calculator: configurable `min_euro = 4`; block Euro ≤ 3 |
| Euro 5 minimum / 10-year age ban | Announced repeatedly (2021, Sept 2025) and **not adopted** as of Jan-Apr 2026 (Racin 30.04.2026: „очигледно, тоа е одложено“). Status May-Oct 2026 not checked | [M] |
| Age limit | None: „Старосна граница за увоз на патнички возила нема“ (Customs, RFE 25.03.2024) | [C] |
| Mileage limit | None found | [M] |
| Documents at clearance | Original foreign registration document; original invoice or sales contract; proof of origin if claiming 0%; COC or other Art. 12(1) document for CO2; ДМВ return (Правилник 279/2019) attached to the ЕЦД; customs declaration lodged by a customs representative. Usually a transit declaration at the border, then clearance at an inland office (used vehicles: Skopje, Kumanovo, Štip, Gevgelija, Bitola) | ЗДМВ Arts. 11-13 [C]; Customs page [C-snip] |
| Before first registration | EU-type-approved or EU-registered vehicles: identification and technical-condition assessment at an authorised station (Закон за возила Art. 23). Vehicles without EU/MK type approval: individual approval via the Bureau of Metrology. Registration requires proof of origin, ownership, „регулирани царински и даночни обврски“, a conformity certificate or consent, technical roadworthiness (Art. 31(2)), and paid ДМВ (ЗДМВ Art. 13(7)) | Закон за возила Сл. весник РМ 140/2008 ... РСМ 161/19, 308/20, 87/23, 148/25, 160/25, 258/25 [C]. The competent ministry since 148/2025 (in force 31.7.2025) is the Министерство за енергетика, рударство и минерални суровини. New rulebook applied from 1.9.2025 [M] |
| **Used-car trader duties (owner imports for resale)** | Enter the Customs „Регистар на производители и трговци со моторни возила“ **before** trading (ЗДМВ Art. 23); per-vehicle records (Art. 24); monthly sales report by the 15th (Art. 25); resale invoice must state the ДМВ paid and the return number (Art. 14(1)); fines under Arts. 34-35, including confiscation | [C] |
| VAT registration | Mandatory above 2,000,000 MKD turnover | ЗДДВ Art. 51 [C] |
| Swiss-sourced cars | No EU COC may be available. CO2 from another Art. 12(1) document, otherwise 400 g/km. EU-built cars are EU origin, not EFTA origin | [M]/[U] |

---

## 10. Golden worked examples

### 10.1 Sourced, official, 2020 rule set (ДМВ only; historical coefficients) [C-snip]
Source: Customs guide „Како да се пресмета данокот на моторни возила“, 29.12.2019 (customs.gov.mk/images/documents/motorniVozila/PresmetkaDMV.pdf, via snippets). СКД = 61.49. CO2 = 145 g/km is in the NEDC 141-150 band (2020: petrol 100, diesel 160).

| Case | Inputs | ДМВ (MKD) |
|---|---|---|
| Petrol, ВМВ 5,500 EUR | (5,500 × 0%) × 61.49 + 145 × 100 | 14,500 |
| Diesel, ВМВ 5,500 EUR | + 145 × 160 | 23,200 |
| Petrol, ВМВ 15,500 EUR (4%) | 15,500 × 4% × 61.49 = 38,123.8 → 38,124; + 14,500 | 52,624 |
| Diesel, ВМВ 15,500 EUR (4%) | 38,124 + 23,200 (the source prints "145 × 190", a typo) | 61,324 |

Use these only to test the formula structure and the Table 1 lookup against the 2020 table.

### 10.2 Sourced, VAT-includes-ДМВ structural test (automedia.mk, 04.01.2020; 2020 coefficients, duty 0, rate 61.5) [C arithmetic re-run]
Final = (VMV + ДМВ/61.5) × 1.18, rounded to whole EUR.

| Vehicle | VMV EUR | ДМВ MKD (2020) | Published final EUR | Recomputed |
|---|---|---|---|---|
| Jeep Grand Cherokee 3.0 CRD, 263 g diesel | 9,328 | 84,160 | 12,622 | 12,621.8 |
| BMW X5 3.0d E70, 231 g (incl. 1% value part 6,279) | 10,210 | 70,959 | 13,409 | 13,409.3 |
| VW Touareg V6 TDI, 275 g | 6,303 | 88,000 | 9,126 | 9,126.0 |
| BMW X3 xDrive20d, 172 g | 4,916 | 37,840 | 6,527 | 6,526.9 |
| Mercedes ML 320 CDI, 249 g | 9,580 | 69,720 | 12,642 | 12,642.1 |
| VW Golf 2.0 TDI, 145 g | 3,487 | 23,200 | 4,560 | 4,559.8 |
| Ford Focus 1.6 TDCi, 118 g | 2,437 | 11,800 | 3,102 | 3,102.1 |

- These are **not** valid for 2026 ДМВ amounts, because of the 2021/2022 escalation and the 2023 table.
- Known source anomaly: the Land Rover Discovery row (8,908 EUR) wrongly applies 1%. Exclude it.
- Other sourced figures (davackizavozila 2020 totals; Customs 2024 "Golf 4 about 1,000 EUR vs Golf 6 about 300 EUR"; Racin 2026 ranges) lack complete inputs. Use them only as sanity ranges. Racin's 400-600 EUR ДМВ for a 135 g diesel Golf 5 matches the 2023 table: 135 × 195 = 26,325 MKD, about 428 EUR.

### 10.3 Derived, ДМВ only, current rules (Table 2 per 220/2023; ВМВ ≤ EUR 10,000; NEDC unless stated) [D]

| # | Case | ДМВ (MKD) |
|---|---|---|
| 1 | Diesel 189 | 189 × 510 = 96,390 |
| 2 | Diesel 231 | 231 × 750 = 173,250 |
| 3 | Diesel 263 | 263 × 880 = 231,440 |
| 4 | Diesel 145 | 145 × 270 = 39,150 |
| 5 | Diesel 118 | 118 × 150 = 17,700 |
| 6 | Diesel 170 | 170 × 375 = 63,750 |
| 7 | Diesel 190 | 190 × 510 = 96,900 |
| 8 | Diesel 191 (lower bound inclusive) | 191 × 605 = 115,555 |
| 9 | Diesel 260 | 260 × 880 = 228,800 |
| 10 | Petrol 245 | 245 × 590 = 144,550 |
| 11 | Petrol 130 | 130 × 75 = 9,750 |
| 12 | Petrol full hybrid 118 | 118 × 75 = 8,850 |
| 13 | Petrol plug-in 50 | 50 × 8 = 400 × 50% = 200 |
| 14 | Diesel, CO2 undocumented | 400 × 880 = 352,000 |
| 15 | Petrol, CO2 undocumented | 400 × 695 = 278,000 |
| 16 | Diesel, WLTP-only COC 160 | 160 × 195 = 31,200 |
| 17 | Petrol, COC with NEDC 130 and WLTP 155, so WLTP | 155 × 105 = 16,275 (not 9,750) |
| 18 | ВМВ 12,500 EUR (2%), СКД 61.5, diesel NEDC 150 | 15,375 + 40,500 = 55,875 |
| 19 | BEV 8703 80 | 0 |

Correction applied: diesel NEDC 170 g/km uses 375, not 510+. A 170-260 g/km diesel range gives **63,750-228,800 MKD**.

### 10.4 Derived, full pipeline, current rules (FX 61.5337 = customs list from 01.10.2026; 2-decimal rounding; transport EUR 700) [D]

| | D1 | D2 | D3 | D4 | D5 | D6 (risk) |
|---|---|---|---|---|---|---|
| Vehicle | Diesel SUV, NEDC 189, Euro 4 | Same as D1 | Petrol SUV, NEDC 245 | Diesel, CO2 undocumented | Diesel, NEDC 170 | Diesel, NEDC 189 |
| Price EUR | 3,000 | 3,000 | 2,500 | 2,800 | 3,000 | customs-set value 6,000 (assumption) |
| Transport in CV / inland leg (EUR) | 700 / 0 | 700 / 0 | 700 / 0 | 700 / 0 | 550 / 150 (separately invoiced) | 700 / 0 |
| Origin proof | none, 5% | EU proof accepted, 0% | none, 5% | none, 5% | none, 5% | none, 5% |
| CV (EUR) | 3,700 | 3,700 | 3,200 | 3,500 | 3,550 | 6,700 |
| CV (MKD) | 227,674.69 | 227,674.69 | 196,907.84 | 215,367.95 | 218,444.64 | 412,275.79 |
| Duty | 11,383.73 | 0.00 | 9,845.39 | 10,768.40 | 10,922.23 | 20,613.79 |
| ВМВ EUR (value part 0%) | 3,885.00 | 3,700.00 | 3,360.00 | 3,675.00 | 3,727.50 | 7,035.00 |
| кCO2 × CO2 = ДМВ | 510 × 189 = 96,390 | 96,390 | 590 × 245 = 144,550 | 880 × 400 = 352,000 | 375 × 170 = 63,750 | 96,390 |
| Inland leg added to VAT base | 0 | 0 | 0 | 0 | 9,230.06 | 0 |
| VAT base | 335,448.42 | 324,064.69 | 351,303.23 | 578,136.35 | 302,346.93 | 529,279.58 |
| VAT 18% | 60,380.72 | 58,331.64 | 63,234.58 | 104,064.54 | 54,422.45 | 95,270.32 |
| **Total import charges (MKD)** | **168,154.45** | **154,721.64** | **217,629.97** | **466,832.94** | **129,094.68** | **212,274.11** |
| Total in EUR at 61.5337 | 2,732.72 | 2,514.42 | 3,536.76 | 7,586.62 | 2,097.95 | 3,449.72 |
| Price + 700 + charges (EUR) | 6,432.72 | 6,214.42 | 6,736.76 | 11,086.62 | 5,797.95 | 10,149.72 |

Landed cost excludes broker/terminal, conformity confirmation, registration fees and MTPL.

Notes:
- D1's CV and duty match the duty researcher's derived test: 227,674.69 / 11,383.73.
- D4 shows that a missing CO2 document is decisive: about +4,850 EUR versus D1.
- D6 is a risk scenario, not the legal value (Art. 34(2)(б)). It assumes Customs adopts 6,000 EUR + transport as CV.
- In the calculator, D6 is the `customs_revaluation_risk` scenario of a car bought for 3,000 EUR with a catalogue value of 6,000 EUR: the charges are the same (212,274.11 MKD), but its landed cost uses the price actually paid (3,000 + 700 + 3,449.72 = 7,149.72 EUR), not the 10,149.72 EUR above, which treats 6,000 EUR as the price.

---

## 11. Calculator modelling rules (choices, not law)

1. Default duty is 5%. Use 0% only on a user-confirmed held proof (EU/EFTA/TR/UK/CEFTA/UA) or US-origin evidence, and always display the 5% alternative.
2. Put all EUR 700 into CV unless the user enters a separately invoiced inland leg. The VAT base always contains all 700.
3. Compute a legal base case on invoice + transport. If a catalogue value is entered, show a separate "customs revaluation risk" case; never a max() of the two.
4. CO2 column: WLTP if the COC has WLTP (alone or with NEDC); NEDC if NEDC only. If CO2 is missing, compute with 400 g/km and raise a blocking warning.
5. Band lookup uses inclusive printed lower figures. Flag non-integer CO2.
6. Block Euro ≤ 3 (configurable, `min_euro = 4`). Flag BEV (ДМВ 0), plug-in hybrid (50%), and 8704 pick-ups (kW table).
7. Keep FX, the rate tables and the threshold in a versioned rule set with effective dates and source citations:
   - customs FX list by month;
   - Table 1;
   - Table 2 effective 2023-10-27;
   - duty branches effective 2025-07-01 (US) and 2011-01-01 (EU).
8. Rounding is configurable. Default 2 decimals, with a whole-denar option.
9. Registration-stage fees and broker fees are separate, configurable, labelled estimates. They are not import charges.

---

## 12. Conflicts resolved and corrections applied

| Topic | Resolution |
|---|---|
| US-origin duty | **Corrected:** the researcher said the 2025-26 amendments do not touch 8703 and that the USA gets no preference. Сл. весник 130/25 added Art. 2(5) with US-origin goods at „слободно“ from 1.7.2025 (the draft's Arts. 1 and 3 were re-read locally) |
| ЗДМВ amendment 3/2025 | Two verifiers disagreed. Kept as **existing, Art. 15(2) wording only (medium confidence)**. The ДМВ verifier saw a snippet quoting its Art. 1 wording precisely, and one gazette issue can carry several amending laws (VAT 3/25 as well). The 2024 ministry renaming required such changes. No calculation impact either way |
| Effective date of decree 220/2023 | **27.10.2023**, from the gazette issue of 19.10.2023 (Art. 2: 8th day). The "about 29.10" figure was dropped |
| 2023 decree content | Replaced Table 2 **and** Art. 2(2) (removed the +25%/+50% escalation). Table 1 is unchanged; exactly EUR 10,000 is 0% |
| 2019 Table 2 first band | "0 до 0" (the "0-1" band dates from 2023) |
| Practical-impact range | Diesel 170-260 g/km gives 63,750-228,800 MKD (not "510-880 bands") |
| Origin and proof references | The revised PEM Convention (2023 rules, from 1.1.2025) via Protocol 4 Art. 1, not the lapsed Appendix A. The values (EUR 6,000; 10 months; 2 years) stand. The 45% ch. 87 rule is not verified for the 2023 rules |
| Customs Law article for transport | Arts. 35(1)(д) and 36(а)/(ѓ), not Art. 33 (computed value) |
| Customs Law amendment list | Adds 62/2009, 117/2009, 35/2010, 180/2014, 110/2021 |
| ЗДДВ Art. 19(7) date | 25.09.2023 (199/23, Art. 24), not 24.09.2023 |
| MTPL tariff | From 25.04.2024, not "2025"; >110 kW band is 11,878 MKD |
| Закон за возила chain | Adds 160/25 and 258/25; neither contains an import restriction |
| 2010 duty cut act | Attribution to 35/2010 dropped as unsupported |
| Old Customs FAQ VAT base | Not a conflict: it covers EV and historic vehicles, which pay no ДМВ |
| Broker claim "kW × 120-360 MKD when CO2 is missing" | Misapplication of the 8704 pick-up Table 3. For M1 the 400 g/km default applies |
| max(invoice, Schwacke) as customs value | Rejected as a legal rule (Art. 34(2)(б)); kept only as a risk scenario |

---

## 13. Open questions (owner / licensed customs broker to confirm before approval)

1. **ДМВ in the VAT base:** the statutory reading is strong, but no Customs or UJP text says so explicitly. Confirm against box 47 of a real ЕЦД for a used M1 car, or the Customs explainer of 03.02.2020 (PojasnuvanjeZakonDMV.pdf).
2. **Customs valuation in 2026:** is the SchwackeNet value applied to every used car or only when the invoice is doubted? On what legal basis, given Art. 34(2)? Is transport then added on top of the catalogue value?
3. **FX:**
   - Which NBRSM day feeds the monthly customs list (Правилник 117/2009, unread)?
   - Does Customs use the monthly list or the daily NBRSM rate for СКД (ЗДМВ "day of import" vs Уредба "day of calculation")?
   - How is a CHF price converted?
   - The figure EUR 61.5337 (from 01.10.2026) was seen only in snippets.
4. **Rounding** of duty, ДМВ and VAT: whole denars or 2 decimals, and which method.
5. **CO2 bands:** how Customs treats boundary integers and non-integer CO2; which fuel column applies to hybrids; whether non-COC documents (manufacturer certificate, technical-assessment body) are accepted for used and Swiss cars, and in which column.
6. **EUR 700 split:** how Customs treats a lump-sum door-to-door transport price; what document separates the inland leg; whether broker, terminal and handling fees before the first destination count as Art. 21(2)(2) incidental costs in the VAT base.
7. **Origin proofs in practice:** can a buyer get a EUR.1 or a valid origin declaration for a used car from a DE/IT private seller or dealer? Can Swiss exporters issue PEM proofs for EU-built cars?
8. **US-origin 0%:** what evidence MK Customs accepts for a used US-built car shipped from the EU. Read the final gazette text of 130/25 (only the draft and press were read).
9. **Euro 4 minimum:** identify the legal act and gazette number, and check whether the 1.9.2025 rulebook restates it. Confirm no Euro 5 rule was adopted May-October 2026; this was not checked because the search budget was exhausted.
10. **New Customs Law:** confirm none was adopted by October 2026. If one was, the article numbers change.
11. **ДМВ law and decree:** confirm no amendment after 220/2023; confirm the content and entry-into-force date of 3/2025; check the Customs ДМВ calculator output against the section 10.3 tests (the calculator is unreachable over valid TLS).
12. **Customs fee rulebook** (89/2007 ... 46/2015): confirm no fee applies to ordinary vehicle clearance.
13. **Environmental fee:** check whether any import-stage fee (2005 Government decision under Закон за животна средина Art. 179; old FAQ 3,000-5,000 MKD) is still collected. Get the full registration eco-fee table under 89/2022 and its in-force date.
14. **Registration costs:** current road-fee amounts (post-2014, and bands above 2,500 cm3), registration administrative fees, and MTPL 2025-26.
15. **Vehicle rulebook from 1.9.2025:** gazette number, and the Bureau of Metrology and identification/assessment fee schedules.
16. **Margin scheme:** can it apply to a car the dealer imported itself (import VAT was paid), or only to cars bought from private persons? Ask UJP for an opinion. Separately, verify the VAT-deferral provision (Правилник 18/2024, Art. 14).
17. **Customs Tariff 2026:** gazette number of the nomenclature decision (cannot change rates under Art. 5); the final text of 161/2026, checked line by line for 8703.
18. **Trader register:** confirm the owner's business model triggers the ЗДМВ Art. 23 Customs registration, and what proof of technical and storage conditions is required.
19. **VAT law:** confirm no 2026 amendment that UJP has not yet consolidated.

---

## 14. Key sources
- **Tariff and duty:**
  - WITS/TRAINS, reporter 807, 2023: https://wits.worldbank.org/API/V1/SDMX/V21/datasource/TRN/reporter/807/partner/000/product/870323/year/2023/datatype/reported
  - trade.gov, North Macedonia import tariffs: https://www.trade.gov/country-commercial-guides/north-macedonia-import-tariffs
  - Bloomberg Adria, 3.6.2024: https://mk.bloombergadria.com/ekonomija/makedonija/59654/makedonija-ima-najvisoki-davacki-za-uvoz-na-avtomobili-a-uvozot-uriva-rekordi/news
  - Customs vehicle page (snippets): https://customs.gov.mk/uvoz-na-vozila-cpp.nspx
  - 2025 Customs Tariff amendment draft (mchamber.mk); 360stepeni.mk, 24.06.2025; dejure.mk (161/2026)
- **Customs Law (consolidated text):** https://www.eurokonsaltplus.com.mk/wp-content/uploads/2020/03/Царински-закон.pdf. Customs FX list (snippets): https://customs.gov.mk/kursna-lista.nspx
- **Origin:**
  - SAA, OJ L 84, 20.3.2004: https://www.gisti.org/IMG/pdf/accord-ce-macedonia-jo-20-mars-2004-english.pdf
  - Decision 1/2021, OJ L 406: https://www.bmf.gv.at/dam/jcr:74c083a7-5685-4a0f-ab4b-c69bef3f957d/L406_Nord%20Mazedonien_alternative%20Ursprungsregeln.pdf
  - EC guidance on the revised PEM rules: https://taxation-customs.ec.europa.eu/document/download/720d8a85-52a8-4d7c-8b0b-751d92153a1d_en?filename=Guidance-Revised-PEM-rules-of-origin.pdf
  - EFTA-North Macedonia FTA: https://www.efta.int/free-trade/free-trade-agreements/macedonia.aspx
- **ДМВ:**
  - Law 261/2019 (Ministry of Finance copy): https://portal.mdt.gov.mk/post-body-files/propisi-od-oblasta-na-danocite-taksite-i-drugite-javni-prixodi-file-7DcP.pdf
  - Уредба 277/2019: https://portal.mdt.gov.mk/post-body-files/propisi-od-oblasta-na-danocite-taksite-i-drugite-javni-prixodi-file-UXof.pdf
  - Правилник 279/2019: https://portal.mdt.gov.mk/post-body-files/propisi-od-oblasta-na-danocite-taksite-i-drugite-javni-prixodi-file-5gDc.pdf
  - Сл. весник 220/2023: https://slvesnik.com.mk/Issues/7206fb85a0f2488f9d578cb13e14f890.pdf and https://mchamber.mk/Upload/Documents/Уредба_за_изменување_на_уредбата_за_начинот_на_пресметка_на_данокот.pdf
  - automedia.mk used-car table: https://automedia.mk/objavi/danok-vozila-koristeni/
- **VAT:**
  - UJP consolidated text to 267/25: https://www.ujp.gov.mk/files/attachment/0000/0986/______________________________________.____________267____30.12.2025.pdf
  - UJP announcement 979 (199/23 publication): https://www.ujp.gov.mk/mk/javnost/soopstenija/pogledni/979
- **Conditions:**
  - Закон за возила (consolidated): https://portal.mdt.gov.mk/post-body-files/zakon-za-vozila-file-BKW4.pdf
  - RFE, 25.03.2024: https://www.slobodnaevropa.mk/a/old-cars-import-pollution-air-euro-standard-custom/32876471.html
  - RFE, 27.01.2026: https://www.slobodnaevropa.mk/a/vozila-polovni-uvoz/33661011.html
  - Racin.mk, 30.04.2026: https://racin.mk/ekonomija/presmetavme-za-vas-kolku-ke-ve-chini-da-uvezete-golf-5-so-euro-4-standard-pred-novite-promeni/
  - Lider.mk, 03.07.2018: https://lider.com.mk/makedonija/novi-pravila-za-uvoz-na-polovni-avtomobili-vo-makedonija/
  - plusinfo.mk, 25.08.2025: https://plusinfo.mk/od-1-septemvri-novi-pravila-za-uvoz-na-vozila-poevtino-pobrzo-i-1-3-milioni-evra-nazad-ka-makedonski-kompanii/
  - Road fee decision 118/2009: https://roads.org.mk/wp-content/uploads/2023/01/odluka-za-patna-taksa-118-09.pdf

The section 10 examples were checked twice during research (hand arithmetic and a reference
evaluator over the draft JSON) and are now checked on every test run through the real engine by
`tests/unit/test_mk_rule_draft.py`. The primary texts downloaded during research were local
working copies and are not in the repository: the `sha256` values in the draft's `sources` are of
those copies. The reviewer must re-fetch every source from its official URL and record its own
retrieval time and hash before approval (docs/tax_rule_approval.md, step 4).