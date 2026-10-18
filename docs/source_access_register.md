# Source access register

Status date: 2026-10-06; robots.txt of mobile.de and AutoScout24 re-checked 2026-10-14; terms and robots.txt of every other marketplace and MK source reviewed 2026-10-14 (docs/research/source_terms_review_2026-10-14.md). MK terms read by the owner and the owner-network reachability probe recorded 2026-10-16 (Reklama5, Pazar3, Swiss sources). The owner's live smoke of `autoscout24_it` recorded 2026-10-17; its enabling (activation step 7) recorded 2026-10-18. Spec sections 5, 8, 24, 25, 31 and 32.

The machine-readable source of truth is `config/sources/*.yaml` (validated by
`suv_deals.domain.sources.SourceConfig`). Current gate status is computed by
`suv_deals.adapters.registry.load_registry(Path("config")).gates()`, which combines
`activation_problems()` with registry checks (registered adapter, matching adapter version,
supported mode). This page explains the records. If it disagrees with the YAML, the YAML wins
and this page must be corrected.

## Principles

- **Terms and technical status are recorded independently.** A source can have working
  parser code and still be blocked by its terms decision, or have a recorded decision and
  still be technically blocked.
- **A stored owner acknowledgement is an audit of a decision, not legal permission.**
  `terms_decision: proceed_acknowledged` records that the owner saw a restriction and decided
  anyway. It does not grant permission and does not remove any right the provider holds.
  Permission or a suitable agreement (`proceed_permitted`, with the agreement on file) is the
  lower-risk activation route.
- **Public accessibility is not permission.** A page that loads without a login is not a
  licence to crawl it, and the absence of a robots.txt prohibition is not a licence either.
- **Technical denials stop the request path.** HTTP 401/403, CAPTCHA or challenge pages,
  explicit automated-access denial, login walls and paywalls are classified `access_blocked`.
  The affected route stops and is reported (`technical_denial_policy: stop_and_report`).
  HTTP 429 means back off and honour `Retry-After`. Nothing in this project solves CAPTCHAs,
  bypasses logins, rotates proxies, fakes fingerprints or probes hidden endpoints.
- **No guessed routes.** Hosts, search paths, detail paths, selectors, ID patterns and API
  endpoints are only added after they are verified on permitted pages. Until then the
  allow-lists stay empty and the adapter stays an explicit placeholder.
- **Robots directives are obeyed by default** (`robots_policy: obey`). Each fetched robots
  revision is stored with its time. Any change to the terms or to robots invalidates earlier
  activation evidence and triggers a new review.
  In the worker this is enforced before every page request (`crawling.robots_guard`, since
  2026-10-17): the host's robots.txt is read first through the robots module (reused within its
  UTC day, so at most 24 hours and fetched again by the first request of each day; an
  unreachable robots.txt is asked again after 30 minutes), stored with its time and hash, logged
  as a `robots` fetch attempt and counted in the host's budget. Every search and detail URL is
  checked against it for our product token; a disallowed URL is never requested, an unreachable
  robots.txt means no request at all (complete disallow), a 401/403 answer to robots.txt is an
  access block, and a robots `Crawl-delay` widens the host's spacing. A changed robots.txt is
  audited (`source.robots_changed`) for this review; nothing is switched on or off by it.
  Crawl4AI's own robots check stays on as a second line.

## Register

Every source below is `enabled: false` except `autoscout24_it`, enabled on 2026-10-18 (activation step 7, the owner's decision "вклучи ја трајно на секои 15 мин", "switch it on permanently, every 15 minutes"). It is the only live-verified source (the owner's live smoke, step 6, 2026-10-17: "Live smoke evidence" below), and it runs only while the owner's scheduler and worker run on his workstation (docs/runbook.md section 9.2); no other source is active. On 2026-10-14 the owner decided to include every marketplace and MK comparable source ("сакам сите да го вклучиш"); each carries `terms_decision: proceed_acknowledged` with the owner as actor, which is an audit record and opens no gate by itself.

**Consequence for valuations (F2, wave D2).** Both MK comparable sources are placeholders, so no
MK market evidence is crawled. Until the owner records some (`suv-deals market import FILE
--evidence-kind asking_price|owner_estimate`, docs/runbook.md) or an MK adapter is implemented
and activated, EVERY valuation is `insufficient_comparables` and no listing can become
`inquiry_ready` (spec 37.2 check 2). An activated `mk_comparable` source would be scheduled and its
detail pages stored as `asking_price` market observations, never as acquisition listings.

| Source key | Country | Role | Mode | Adapter and status | Terms status | Terms decision | Terms URL | Terms reviewed | Technical status | Robots | What activation requires |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `mobile_de_public` | DE | acquisition | public_html | `mobile_de_public`: placeholder (`unimplemented`, raises `AdapterUnimplemented`) | restricted: the public terms contain a scraping restriction in section 11 | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14: include; terms risk accepted - an audit record, not permission) | https://www.mobile.de/service/agbPublic | 2026-10-06 | untested | obey. robots.txt **changed**: the 2026-10-14 re-check shows the `*` group now disallows `/fahrzeuge/search.html`, `/fahrzeuge/searchresults.html`, `isSearchRequest=true`, `pageNumber=` and `/fahrzeuge/details.html` except the single form `details.html?id=<n>&vip=1`. The search allow-list is therefore empty | **Discovery is blocked by robots.txt**; the owner's approval does not override it. Routes that remain: the official Search API (`mobile_de_api`, needs an agreement with mobile.de) or listing links the owner supplies (a detail page only in the robots-allowed `?id=<n>&vip=1` form, through a feature that does not exist yet). Access blocks stop the source; no evasion. |
| `autoscout24_de` | DE | acquisition | public_html | `autoscout24_public_de`: **implemented** (`autoscout24@1.0.0`, 2026-10-16, from owner captures; search cards only) and **fixture-tested** on its SYNTHETIC saved fixture set (`tests/adapters/fixtures/autoscout24_de/`, 2026-10-16); not live-tested | restricted: the consumer terms, sections 8.2-8.3, restrict automated queries and the creation or commercial use of a derived database | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14) | https://www.autoscout24.com/company/agb/ | 2026-10-06 | fixture_tested (step 5 done 2026-10-16 on synthetic saved fixtures; not a live verification) | obey. robots.txt checked 2026-10-10 and re-checked 2026-10-14 (unchanged): the `*` group disallows `/angebote/` (detail pages), `/lst?`, `/lst/?` and `cat=`; `/lst/<make>/<model>` search pages are allowed, so the source is **card_only**. A query on `/lst/<make>/<model>` (including the seller-country filter `cy=D`) is not disallowed | **Step 5 done 2026-10-16**: saved SYNTHETIC fixtures under `tests/adapters/fixtures/autoscout24_de/` with `MANIFEST.yaml` (search-side spec-31 set; the detail categories do not apply to a card_only source and card-level equivalents stand in for them), then `technical_status: fixture_tested`. The only gate left closed is `enabled: false` (`crawl once` refuses a disabled source). Next, **steps 6-7**, after `autoscout24_it`: the owner's live smoke (docs/runbook.md section 9.1 with this key; one model page, `renault/captur`, the slug already verified on the capture) to confirm the live page structure, then enabling. That smoke cannot confirm the search filters (`pricefrom`/`priceto`/`kmto` and `cy` stay unverified on .de: the page's `pageQuery` is not recorded, and the stored card prices, mileages and seller countries are only an indirect check) nor the unverified model slugs (only a full 12-model walk requests them; a wrong slug answers 404 and is skipped). No detail fetches ever. Search parameters: the primary profile's price band (`pricefrom`/`priceto`), `kmto`, `sort=age&desc=1` and, since 2026-10-17, the seller-country filter `cy=D` (`search.cy`). `D` is the value of the site's `pageQuery` on the capture, but that request sent no `cy`, so it was the server's default there; the owner's 12-model live smoke of `autoscout24_it` that day, sent without `cy`, got listings from other countries. Whether an explicit `cy=D` narrows the results is **unverified** until a live 12-model walk of this source shows only DE seller countries among the ingested cards. Cadence (owner decision 2026-10-16, "сакам на секои 30 минуту да го наместиш само од 08:00 до 22:00"): every 30 minutes from 08:00 to 22:00 Europe/Skopje (slots 08:00 ... 21:30, `schedule` block), 28 runs x 12 pages = 336 search requests a day within the 350-request daily budget. |
| `autoscout24_it` | IT | acquisition | public_html | `autoscout24_public_it`: **implemented** (`autoscout24@1.0.0`, 2026-10-16, from owner captures; search cards and `/annunci/` detail pages), **fixture-tested** on its SYNTHETIC saved fixture set (`tests/adapters/fixtures/autoscout24_it/`, 2026-10-16) and **live smoke passed** (the owner's, 2026-10-17) | restricted (same sections 8.2-8.3) | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14) | https://www.autoscout24.com/company/agb/ | 2026-10-06 | live_smoke_passed (step 6 done 2026-10-17, the owner's live smoke; step 5 done 2026-10-16 on synthetic saved fixtures) | obey. robots.txt checked 2026-10-10 and re-checked 2026-10-14 (unchanged): `/lst/<make>/<model>` search and `/annunci/` detail pages are not disallowed for `*`; a query is disallowed only on the bare `/lst?` and `/lst/?` paths, so `/lst/<make>/<model>?...&cy=I` stays allowed. Fetched and stored by the worker itself on 2026-10-17 (HTTP 200, SHA-256 `7c78475c...`, unchanged since 2026-10-06) | **Steps 5-7 done.** Step 5 (2026-10-16): saved SYNTHETIC fixtures under `tests/adapters/fixtures/autoscout24_it/` with `MANIFEST.yaml` covering the spec-31 set (normal, second-model and paginated search, empty result, normal, missing-price, contradictory-mileage, net/gross, conditional, damaged and removed detail, login wall, CAPTCHA/block, malformed and changed markup, injection), then `technical_status: fixture_tested`. Step 6 (2026-10-17): the owner's live smoke (docs/runbook.md section 9.1, a 12-model `crawl once --max-pages 12` and `worker --drain --queues detail --max-jobs 5` at commit `25f43f8`: robots.txt fetched by the worker, 12 search pages HTTP 200, `complete`, 15 cards, 5 detail pages ingested, no block; evidence in "Live smoke evidence" below), then `technical_status: live_smoke_passed`. Step 7 (2026-10-18): enabled through a committed configuration revision (`enabled: true`), the owner's decision of 2026-10-18 (verbatim "вклучи ја трајно на секои 15 мин", "switch it on permanently, every 15 minutes"), which takes effect on the owner's machine with `sources sync --yes` (audited); since step 7 every gate passes. It runs only while the owner's `suv-deals scheduler` and `worker` run (docs/runbook.md section 9.2, `scripts/local_crawl.sh`); an access block stops it by itself (`access_blocked`, `technical_denial_policy: stop_and_report`) and is reported, never worked around. Model slugs: the owner's two 12-model walks of 2026-10-17 requested all twelve and every one answered HTTP 200; `renault/captur` (verified on the capture), `opel/mokka`, `opel/mokka-x` and `suzuki/sx4-s-cross` returned no card in either walk, and whether the site answers an unknown slug with 404 has never been observed, so no result has confirmed the last three yet. One search page per configured model per run (12 models, `max_search_pages_per_run: 12`, 20 s spacing) with the primary profile's price band (`pricefrom`/`priceto`), `kmto`, `sort=age&desc=1`, `ustate=U` and, since 2026-10-17, the seller-country filter `cy=I` (`search.cy`). **Live finding 2026-10-17** (the owner's first 12-model walk, `crawl once --max-pages 12`: 12 search pages HTTP 200, `complete`, 49 cards, 5 detail pages ingested), sent without a country filter: three of the five ingested listings came from other countries, a Dutch and a Belgian one (rejected by screening as `COUNTRY_NOT_IN_PROFILE`) and a German one (accepted by the country rule, screened `needs_facts`), so foreign cards spent the request budget and could hide Italian ones. **`cy=I` strongly indicated the same day**, not yet confirmed by seller country: the second walk (the step-6 smoke, with `cy=I`) returned 15 cards instead of 49, every one already seen by the unfiltered walk; of the listings whose seller country is known, the 2 the filtered walk returned are Italian and the 7 it no longer returned are foreign (NL, BE, DE from the morning; BE, AT, BE, FR from the step-6 drain); a further one was removed from the site (HTTP 410). The seller countries of the other 13 cards (their detail jobs are queued) settle it. If it holds, German sellers come in only through the `card_only` `autoscout24_de` (no detail page) and Swiss sellers through neither AutoScout24 source (`autoscout24_ch` is blocked). Cadence (owner decision 2026-10-18, "вклучи ја трајно на секои 15 мин"; the 08:00-22:00 window is the owner's decision of 2026-10-16, which set every 30 minutes and a 600-request budget): every 15 minutes from 08:00 to 22:00 Europe/Skopje (slots 08:00 ... 21:45, `schedule` block), 56 runs x 12 pages = 672 search requests a day of the 800-request daily budget (800 = 672 + 128); the other 128 are for detail pages and the daily robots.txt request, and the budget gate keeps detail requests from using what the day's remaining runs still need. |
| `autoscout24_ch` | CH | acquisition | public_html | `autoscout24_public_ch`: placeholder | unreviewed: the Swiss site's GTC (`/de/legal/gtc`) and every other HTML page answered a Cloudflare challenge to our user agent on 2026-10-14, so the terms could not be read; operator lead (unverified): SMG Swiss Marketplace Group AG, Zurich | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14); terms still unreviewed, which keeps it gated | not readable (`https://www.autoscout24.ch/de/legal/gtc`) | not reviewed | untested | obey. robots.txt re-checked 2026-10-10 and 2026-10-14 (unchanged): no `User-agent: *` group (a fact, not permission); routes not verified. Owner network 2026-10-16: robots.txt HTTP 200 (unchanged), home page HTTP 403 directly and an unfollowed redirect via Crawl4AI, so the probe stopped: **blocked, stays off** | The owner reads the GTC in a normal browser and the terms are recorded; verify routes; build from real captures. A challenge page met by the crawler stops it (no evasion). |
| `subito_it` | IT | acquisition | public_html | `subito_it_public`: placeholder | **restricted** (reviewed 2026-10-14): the general terms (updated 2026-09-22) bind users who only consult listings (art. 3) and expressly prohibit robots, spiders, scrapers and other automated tools to access the site or extract data (art. 5) and use of content by unauthorised aggregators or third parties (art. 10). Operator: Subito.it S.r.l., Milano (Adevinta) | **proceed_acknowledged** (owner, 2026-10-14) | https://info.subito.it/policies/condizioni-generali.htm | 2026-10-14 | untested | obey. robots.txt checked 2026-10-14: the `*` group allows `/` but disallows `/vi/`, `/li?`, `/utente/` and the pagination forms `*/?o=*`, `*&o=*`; its header comment forbids automatic access without permission. The home page answered HTTP 403 (Akamai) to our user agent | Verify routes and build from real captures; the site currently refuses the crawler technically, which stops the source (no evasion). Permission from Subito is the lower-risk route. |
| `automobile_it` | IT | acquisition | public_html | `automobile_it_public`: placeholder | **restricted** (reviewed 2026-10-14): the terms (in force since 2021-06-29) prohibit robots, spiders, scrapers and automated tools without prior written consent and circumventing access limits (art. 4), searching other than via the site's search bar or with 'searchware' (art. 11) and using protected content (art. 14). Operator: Marktplaats B.V., Amsterdam (Adevinta) | **proceed_acknowledged** (owner, 2026-10-14) | https://www.automobile.it/policies/condizioni | 2026-10-14 | untested | obey. robots.txt checked 2026-10-14: the `*` group disallows `/api/*`, `/rest/*`, `*?b=*`, `*?d=*`, dealer-catalogue and private-area paths; nothing else. The home page answered HTTP 200 | Verify routes and build from real captures. |
| `pazar3_mk` | MK | mk_comparable | public_html | `pazar3_mk`: placeholder | **no_restriction_found** (read by the owner on 2026-10-16): there is no separate terms page (the link to the advertising rules opens the privacy policy). The privacy policy (last changed 28.05.2025; operator ПАЗАР3.КОМ ДООЕЛ, Ресен) refers to terms of use but has no clause on automated access, robots, scraping or reuse of ad content. Not a licence | **proceed_acknowledged** (owner, 2026-10-14) and **facts only** (owner, 2026-10-16, "Само факти"): make, model, year, km, fuel, gearbox, asking price and ad URL; no ad text, photos, seller names or contact data, no retained page snapshots | https://www.pazar3.mk/korisnicka-poddrska/politika-na-privatnost | 2026-10-16 | untested. Owner network 2026-10-16: home page HTTP 200 directly and via Crawl4AI; the rendered page lists ads as JSON-LD Product + Offer (EUR or MKD; 123456789 is a placeholder price) | obey. robots.txt HTTP 200 from the owner's network on 2026-10-16 (sha256 8633e9afc044...), the home page allowed; rules to be recorded with the first search capture. Cloudflare block (403) to the build environment on 2026-10-14 | Owner-run captures of a per-model car search (`/oglasi/vozila/avtomobili/...`) and ad pages, then the facts-only adapter (daily, per reference model) and steps 2 to 7. Comparable evidence only, never acquisition. A block met by the crawler stops it. |
| `reklama5_mk` | MK | mk_comparable | public_html | `reklama5_mk`: placeholder | **restricted** (read by the owner on 2026-10-16): the terms of use (updated 03.07.2026; operator Бигмедиа ДООЕЛ, Радолишта, Струга) have no clause on automated access, but forbid collecting other users' personal information (e-mail addresses, phone numbers) and copying, altering or extending content of other people's ads | **proceed_acknowledged** (owner, 2026-10-14) and **facts only** (owner, 2026-10-16, "Само факти"), same rule as `pazar3_mk` | https://reklama5.mk/Help/TermsOfService | 2026-10-16 | untested. Owner network 2026-10-16: home page HTTP 200 directly and via Crawl4AI; links /Search?... and /AdDetails?ad=<id>; no JSON-LD | obey. robots.txt checked 2026-10-14 and re-checked 2026-10-16 from the owner's network (HTTP 200, unchanged, sha256 f82b047730fd...): no `User-agent: *` group and no group for our token (a fact, not permission). Cloudflare challenge to the build environment on 2026-10-14 | Owner-run captures of a per-model search and ad pages (the car category id is not verified yet), then the facts-only adapter (daily, per reference model) and steps 2 to 7. A block met by the crawler stops it. |
| `mobile_de_api` | DE | acquisition | official_api | `mobile_de_api`: feature-flagged skeleton that raises `DependencyUnavailable('mobile.de Search API entitlement not verified')` | unreviewed: no API agreement has been obtained | pending | none. Documentation: https://services.mobile.de/docs/search-api.html | not reviewed | untested | obey | Owner-approved account or agreement and a recorded entitlement reference. Server-side credentials (marketplace login credentials are not API credentials). An implementation written from the official documentation, recorded fixtures and a low-volume test. This must never block the public-page framework. **Deferred by the owner on 2026-10-15**: mobile.de quoted EUR 990 per month for Search API access. |
| `example_dealer_template_de` | DE | acquisition | public_html | `schemaorg_dealer` (`schemaorg_dealer@1.1.0`): the generic adapter is implemented and fixture-verified on synthetic sources | unreviewed | pending | none (template) | not reviewed | untested: no pages of a real dealer have been captured | obey | This is a template, not a source. Copy it for a permitted dealer and complete steps 1 to 7 for that dealer. |
| `carforyou_ch` | CH | acquisition | public_html | `carforyou_ch_public`: placeholder (`unimplemented`) | unreviewed: nothing could be fetched on 2026-10-14 (TLS connection resets; once a certificate that did not match the host name; TLS verification is never bypassed) | **proceed_acknowledged** (owner, 2026-10-14); terms unreviewed, which keeps it gated | not identified | not reviewed | untested | obey. robots.txt not retrievable on 2026-10-14 (unreachable means complete disallow). Owner network 2026-10-16: robots.txt gave no response, directly or via Crawl4AI, so nothing else was requested: **stays off** | Domain `www.carforyou.ch` and operator still **unverified**. Verify domain, operator, terms, robots.txt and routes, then steps 1 to 7. |
| `tutti_ch` | CH | acquisition | public_html | `tutti_ch_public`: placeholder (`unimplemented`) | unreviewed: every request (robots.txt and home page) answered HTTP 403 to our user agent on 2026-10-14 | **proceed_acknowledged** (owner, 2026-10-14); terms unreviewed, which keeps it gated | not identified | not reviewed | untested | obey. robots.txt answered HTTP 403 (HTML) on 2026-10-14, no rules readable. Owner network 2026-10-16: robots.txt and home page HTTP 403 directly, an unfollowed redirect via Crawl4AI, so the probe stopped: **blocked, stays off** | Domain `www.tutti.ch` and operator still **unverified**. General classifieds with private sellers: same verification, then steps 1 to 7. Store no seller contact data and record the seller type. |
| `comparis_ch` | CH | acquisition | public_html | `comparis_ch_public`: placeholder (`unimplemented`) | unreviewed: the legal pages answered a DataDome CAPTCHA (HTTP 403) to our user agent on 2026-10-14; operator lead (unverified): comparis.ch AG, Zürich | **proceed_acknowledged** (owner, 2026-10-14); terms unreviewed, which keeps it gated | not readable (`/info/copyrights`) | not reviewed | untested | obey. robots.txt checked 2026-10-14: the `*` group **disallows the car-marketplace result and detail endpoints** (`/carfinder/marktplatz/result.aspx`, `/carfinder/marktplatz/details/show/`, `/marktplatz/details/show`, `/carfinder/searchservice/`, `/carfinder/api/`) | An aggregator: its own terms **and** the original source's terms apply. With the known car-search routes disallowed by robots.txt, the source cannot discover listings through them; prefer the original source. |
| `example_dealer_template_ch` | CH | acquisition | public_html | `schemaorg_dealer` (`schemaorg_dealer@1.1.0`): generic adapter, fixture-verified on synthetic sources incl. `fixture_dealer_ch` | unreviewed | pending | none (template) | not reviewed | untested: no pages of a real dealer have been captured | obey | Template, not a source (`crawl_locale: de-CH`, `source_timezone: Europe/Zurich`). Copy it per permitted Swiss dealer, set `crawl_locale` to `de-CH`, `fr-CH` or `it-CH` and complete steps 1 to 7. |

### Live smoke evidence (activation step 6)

Spec 31 exact-build evidence, one record per source (one source never proves another). Ids,
versions, hashes and codes only: no page, body or listing text is kept (`SNAPSHOT_STORAGE=disabled`).
Sources of each fact: the `sources smoke-evidence` and `sources inspect --from-db` JSON of the run
(the owner's files) and, where marked, the owner's terminal output.

**`autoscout24_it`, 2026-10-17: passed, evidence record complete** (completed the same day; the
ids are also in [docs/evidence/autoscout24_it_live_smoke_2026-10-17.json](evidence/autoscout24_it_live_smoke_2026-10-17.json)).

- Build: commit `25f43f8a1451f534cb49e1b93659f904f263742a`, `build_id: dev` (run from the checkout
  with `uv`, no application image). Crawler: the owner's Crawl4AI on the host
  (`http://127.0.0.1:11235`; the worker's crawler contract check logged `ready: true`, terminal
  output); image `unclecode/crawl4ai@sha256:9021b3cb5c6f12570bbcd5395638495e0a06969b3148e377b953d174af2ebc9b`
  (recorded the same day, with the crawler gate evidence: ACTIVATION_GATES.md evidence log).
- Configuration: revision 1 (`360d0a9d-b46d-4442-bb8b-309751cde88e`, hash `ebf0d4bc...`),
  `adapter_version` and `parser_version` `autoscout24@1.0.0`, registry version 6 during the smoke (a
  local, uncommitted `enabled: true`); switched off again afterwards (`enabled: false`, `state:
  disabled`, terminal output).
- Commands (terminal output): `crawl once --source autoscout24_it --max-pages 12`, then `worker
  --drain --queues detail --max-jobs 5`; no `suv-deals scheduler` among them.
- Discovery: job `d3c60927-9dc7-426b-8a5a-20cab1087c32`, crawl run
  `46f4bdec-1a91-4702-8ecb-1dff3f5a7255`, 11:50:56-11:55:32 UTC, outcome `complete`, `access_state:
  ok`, 12 pages, 15 cards, no gap reason; no new or changed listing (every card had been seen by the
  unfiltered walk that morning), so the run queued no detail job.
- robots.txt: requested by the worker before the first page (fetch
  `03976780-d703-4d0c-bcb3-c6838b10e05d`, `purpose: robots`, HTTP 200, `access_state: ok`, 11:50:56
  UTC), revision `dad6209a-1e23-4f87-a88f-e140e6da7633` (`availability: available`, parsed, not
  truncated, SHA-256 `7c78475c525adef6bf88187d53e75e9eb7b4ae9fdbe4f05215fb5ddafb396478`: the file of
  the 2026-10-06/10/14 readings, `robots_changed: false`), user agent `SUVDealResearch/0.1`.
- Search: 12 fetches `purpose: search`, HTTP 200, `access_state: ok`, 20-23 s apart, one per
  configured model. No URL is stored; the adapter builds each as
  `https://www.autoscout24.it/lst/<make>/<model>?pricefrom=2500&priceto=3000&kmto=200000&sort=age&desc=1&ustate=U&cy=I`
  from the configuration and the primary profile.
- Detail pages: 5 detail jobs succeeded under the robots revision above. The unfiltered morning
  walk had queued them (run `ac1a09b6-...`, so its `smoke-evidence --run-id` lists them), for five
  listings this run no longer returned; fetches 11:55:42-11:57:08 UTC, 20-23 s apart:
  - job `18d1926b-b0b8-4846-85ae-199881d2e6fd`: HTTP 200, listing
    `7f85e4f0-6ef4-408e-9630-76b057ec177b`, revision `faf012b2-325d-4f22-9dfc-f638cecc273c` (13
    field-evidence ids), seller country BE, `rejected`;
  - job `25b5786b-5581-4358-ba14-327a9f7b8f65`: HTTP 200, listing
    `eb5543ed-8a37-4c3e-af63-31362bb7da4e`, revision `d0f0ca62-27a5-4d5f-9991-b1bf11bff2a4` (16),
    AT, `rejected` (also 200,000 km);
  - job `2b6d3ec8-7d98-46e8-a26f-b74269540d18`: HTTP 200, listing
    `07b6b259-57bc-4721-9ed1-f5a88e5951c2`, revision `7ce1c9bb-f233-40ad-895f-afe861d2ef43` (15), BE,
    `rejected`;
  - job `3130e66f-77cb-49dc-9eb3-514820b0a8a3`: HTTP 410, `access_state: removed` (`http_gone`),
    listing `f083eea6-f7e1-4708-8699-2e66ab02991a`: removed from the site, no revision (the
    removed-listing path works live);
  - job `36096ba2-f3fe-4689-8aa1-f6cd335b056a`: HTTP 200, listing
    `3ec9139c-b332-45e5-bd23-0561bb6f66f1`, revision `4cde316a-040a-4c72-8be9-b3a05e4722a2` (14), FR,
    `rejected`.
  Field-evidence ids per revision: the evidence file above.
- Persisted revisions: this run created none (no new or changed card). The revisions on record for
  its cards come from the morning walk's detail jobs: listing `19651894-7f11-4049-b6f1-4b14cfa2bded`,
  revision `3170f0e5-c12c-4d61-8a1a-149f891bbe7c` (200,000 km: `rejected`); listing
  `620f99bb-d563-4a37-b56c-6ffcc522fc8c`, revision `2c03793a-2d66-46cc-b8d5-e5af9675de72` (199,999
  km, EUR 3,000: `eligible_primary`); both seller country IT.
- Budget after the smoke: `requests_today` 36 of 600, the day's runs together (the one-page smoke 1,
  the morning walk 12 + 5, this smoke 1 robots + 12 + 5); 22.0 MB of 200 MB; circuit closed.
- Filters, indirect checks only (the page's `pageQuery` is not recorded): every priced card is
  within EUR 2,500-3,000 and at most 200,000 km (five at exactly 200,000; `kmto` is inclusive and
  screening rejects exactly 200,000); one card carries no price and is decided by its detail page.
- Seller-country filter `cy=I`: strongly indicated, not yet confirmed for every card. This run
  returned 15 cards against the unfiltered walk's 49, all 15 among those 49. Every listing whose
  seller country is known fits: the 2 this run returned are IT, the 7 it no longer returned are
  NL, BE, DE (morning ingests) and BE, AT, BE, FR (this drain), and one more was removed from the
  site. The seller country is known for 2 of the 15 cards; the queued detail jobs of the other 13
  settle it.
- Model slugs: all twelve answered HTTP 200 in both walks of the day. `renault/captur` (verified on
  the owner's capture), `opel/mokka`, `opel/mokka-x` and `suzuki/sx4-s-cross` returned no card in
  either; whether the site answers an unknown slug with 404 has never been observed, so a 0-card
  answer confirms nothing. `hyundai/ix35` returned cards only in the unfiltered walk.
- Parser: no parse failure (12 of 12 search pages complete without a gap, all 10 detail jobs of the
  day succeeded: 9 pages parsed, 1 HTTP 410 recorded as removed); the parser-health sample has not
  run yet (`parser_health.status: unknown`).
- Earlier attempts the same day, not counted for step 6 because they predate the worker's robots
  check: the one-page smoke (run `6c2e6770-d6b4-4c1c-9fdc-1ee482014536`, 09:19 UTC, `renault/captur`
  only, 0 cards in the band) and the unfiltered 12-model walk (run
  `ac1a09b6-6899-4b80-a3ba-7d617e95bd84`, 09:28 UTC, 49 cards, 49 detail jobs queued, 5 detail pages
  ingested: the 200,000 km Qashqai (IT) rejected, the 199,999 km Yeti (IT) eligible, a Juke (NL)
  and a Sportage (BE) rejected as `COUNTRY_NOT_IN_PROFILE`, a Yeti (DE) `needs_facts`).
- Left over: 39 detail jobs queued by the unfiltered walk (13 for cards this run returned as well,
  26 for listings it no longer returns, most likely foreign like the 7 known ones). They stay queued only while no worker claims them: a
  worker that reaches one while the source is disabled blocks it (`source_paused`; `suv-deals jobs
  unblock` puts it back). Since the source is enabled (step 7, 2026-10-18) the owner's worker
  fetches them first, oldest first (once: 39 of the 128 requests a day left after the search pages
  for detail pages and robots.txt; about 13 minutes at the 20 s spacing; docs/runbook.md section
  9.2).
- Completed the same day (after the smoke): the five ingests' ids and seller countries above (the
  owner's `smoke-evidence --run-id ac1a09b6-...`), the crawler gate evidence (firewall rules
  already present, none added; isolation verified, 9 host targets blocked; `doctor --process worker
  --crawler`: healthy 0.9.4, unauthenticated requests refused, read-only contract passed; image
  digest above) and the status in the database (`sources set-technical-status`, `last_live_smoke_at`
  stamped, audited).

### Synthetic fixture sources (tests only)

`fixture_dealer_de`, `fixture_dealer_it` and `fixture_dealer_ch` live in
`tests/adapters/fixtures/<source_key>/` (`source.yaml` and `MANIFEST.yaml`). Their pages are
hand-written **synthetic** HTML on the reserved example-style hosts `dealer.example`,
`concessionario.example` and `garage.example`. They are served by the offline
`FixtureCrawlClient` and never fetched from a network. They are `mode: fixture`,
`technical_status: fixture_tested` and `enabled: false` in the files. Tests enable them only
in memory, with a terms record that is visibly labelled synthetic. They prove deterministic
parser behaviour. They do not prove access to any real website. `fixture_dealer_ch` also covers
Swiss wording: `Export ohne MWST`, `Occasion`, `Frisch ab MFK` and `ohne MFK`.

`tests/adapters/fixtures/autoscout24_it/` and `autoscout24_de/` (2026-10-16) are the step-5 saved
fixtures of two REAL sources: `MANIFEST.yaml` with `designation: synthetic`, created 2026-10-16,
`parser_version: autoscout24@1.0.0` and a description per page, no `source.yaml` (the sources are
configured only in `config/sources/`). The pages carry the STRUCTURE of the owner's captures with
invented values only (no real listing id, price, mileage, date, slug, seller, phone, address or
e-mail) and are generated by `tests/adapters/autoscout24_fixture_set.py`; their routes use the
source's own host (`www.autoscout24.it` / `www.autoscout24.de`) so the real URL policy applies, and
`tests/adapters/test_fixture_manifests.py` refuses any other real host in them. They are served
offline only. `autoscout24_de` has no detail page or route: it is card_only, so its MANIFEST lists
the card-level fixture that stands in for each spec-31 detail category.

### Switzerland

Switzerland is an initial acquisition market. Its sources, wording, CHF handling, export/transit
costs and open owner decisions are described in [docs/markets/switzerland.md](markets/switzerland.md).
No Swiss source is enabled, verified or active.

## Adapter status vocabulary (spec section 32)

| State | Meaning here |
|---|---|
| implemented | Code exists and passes unit tests. Placeholders are **not** implemented. |
| fixture_verified | Passes the saved-fixture suite (`tests/adapters`). This is true for the generic `schemaorg_dealer` adapter on synthetic fixtures and, since 2026-10-16, for the AutoScout24 adapter on its synthetic saved fixture sets (`technical_status: fixture_tested`). |
| live_verified | Passed a permitted low-volume live smoke with recorded evidence. Only `autoscout24_it` (2026-10-17, "Live smoke evidence"; enabled since 2026-10-18, step 7). |
| active | Enabled, every gate satisfied and running on schedule: `autoscout24_it` (enabled 2026-10-18, step 7) once the owner's scheduler and worker run (docs/runbook.md section 9.2). No other source is active. |
| blocked | A specific dependency (terms decision, entitlement, route verification) prevents activation. |

## Activation checklist

The steps run in this order. Code enforces the result: `SourceConfig` refuses
`enabled: true` while `activation_problems()` is non-empty. `registry.build_active_adapter()`
raises `SourcePaused` unless every gate passes. Placeholders raise `AdapterUnimplemented`.

1. **Terms decision record.** Identify the exact terms that apply: country-specific, consumer
   or business, and website or API. Record `terms_status`, `terms_url` and
   `terms_reviewed_at`. The owner then records `terms_decision` with `terms_decision_actor`
   and `terms_decision_note`.
   - `proceed_permitted` requires permission or an agreement on file.
   - `proceed_acknowledged` is an audit record only, not permission.
   - `do_not_use` blocks the source permanently.
2. **Permitted route verification.** Verify the exact public result and detail routes that may
   be used, from the provider's documentation or permission and from permitted pages. Record
   them as `allowed_hosts` (exact hostnames) and as anchored path regexes in
   `allowed_search_paths` and `allowed_detail_paths`. For the generic dealer adapter, also
   record `search.search_url` and, optionally, `search.page_param` and a verified
   `search.detail_id_regex`. Never guess them.
3. **Robots check.** Fetch robots.txt through the robots module, store the revision hash and
   time, and confirm that the configured paths are allowed. Re-check whenever robots changes.
   Before activation this is the owner's `capture-samples` reading; once a source runs, the worker
   records a revision per host each UTC day itself (`ops.robots_revisions`; `sources inspect
   --from-db` shows status, time and hash), and the live smoke of step 6 must show it.
4. **Adapter implementation.** For marketplaces, replace the placeholder with an implementation
   built on the verified routes, set a real `adapter_version` and bump it on every parser
   change. For dealers, confirm that the permitted pages publish schema.org JSON-LD:
   Car/Vehicle/Product with offers on detail pages and an ItemList on result pages.
5. **Saved fixtures.** Store only content you are permitted to keep, with unnecessary personal
   data redacted, under `tests/adapters/fixtures/<source_key>/` with a `MANIFEST.yaml`. The
   manifest records the source, date, parser version and real or synthetic designation. Cover
   the spec section 31 set:
   - a normal search page, a paginated search and an empty result
   - a normal detail page, a missing-price detail and contradictory mileage
   - net/gross wording and a removed listing
   - a login wall, a CAPTCHA/block page and malformed or changed markup

   Once these tests pass, set `technical_status: fixture_tested`.
6. **Live low-volume smoke.** The owner runs it by hand, never automatically (there is no
   `live` pytest test; the marker is only declared): with `SOURCE_NETWORK_ENABLED=true` and the
   crawler reachable, `suv-deals crawl once --source <key> --max-pages 1` runs ONE bounded
   discovery through every source and runtime gate (it refuses while any gate is closed), within
   the source's rate budget; then let the worker fetch some of the detail pages that one search
   page queued (`suv-deals worker --drain --queues detail --source <key> --max-jobs N`). The
   search page queues one detail job per new or changed card and `max_detail_jobs_per_run` does
   not bound a drain (each detail job is its own run), so `--max-jobs N` is what limits the
   detail requests; `--source <key>` keeps the drain on this source, so detail jobs another
   source left queued are not claimed: a disabled source's would be blocked as `source_paused`,
   an enabled source's (`autoscout24_it` since 2026-10-18) fetched on its budget.
   Confirm `access_state: ok` and healthy parser outcomes (`suv-deals sources inspect <key>
   --from-db`, `doctor`). For AutoScout24 the exact owner commands, including the local
   uncommitted `enabled: true` and switching the source off again, are in docs/runbook.md
   section 9.1. Record the spec 31 evidence fields: commit SHA and image digest, configuration
   revision and `adapter_version`, source URL, actual observation timestamp, crawl run and job
   ids with the redacted fetch outcome, the robots.txt request of the run (`purpose: robots`) and
   the robots revision in effect (time, HTTP status, hash), and the persisted
   listing/revision/evidence ids (`suv-deals sources smoke-evidence <key>` prints all of them
   except the commit SHA and image digest). Then set `technical_status: live_smoke_passed`.
7. **Enable.** Set `enabled: true` through an auditable configuration revision (a commit, applied
   with `suv-deals sources sync --yes`). Done for `autoscout24_it` on 2026-10-18 (the owner's
   decision); the source then runs while a scheduler and a worker run (docs/runbook.md section 9.2).

After activation:
- A technical denial pauses the route and creates one deduplicated operational review item.
- Parser-health tripwires pause alerts and quarantine affected revisions.
- A change to the terms or to robots sends the source back to step 1.
