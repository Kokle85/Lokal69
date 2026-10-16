# Source access register

Status date: 2026-10-06; robots.txt of mobile.de and AutoScout24 re-checked 2026-10-14; terms and robots.txt of every other marketplace and MK source reviewed 2026-10-14 (docs/research/source_terms_review_2026-10-14.md). MK terms read by the owner and the owner-network reachability probe recorded 2026-10-16 (Reklama5, Pazar3, Swiss sources). Spec sections 5, 8, 24, 25, 31 and 32.

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

## Register

Every source below is `enabled: false`. No source is live-verified or active. On 2026-10-14 the owner decided to include every marketplace and MK comparable source ("сакам сите да го вклучиш"); each carries `terms_decision: proceed_acknowledged` with the owner as actor, which is an audit record and opens no gate by itself.

**Consequence for valuations (F2, wave D2).** Both MK comparable sources are placeholders, so no
MK market evidence is crawled. Until the owner records some (`suv-deals market import FILE
--evidence-kind asking_price|owner_estimate`, docs/runbook.md) or an MK adapter is implemented
and activated, EVERY valuation is `insufficient_comparables` and no listing can become
`inquiry_ready` (spec 37.2 check 2). An activated `mk_comparable` source would be scheduled and its
detail pages stored as `asking_price` market observations, never as acquisition listings.

| Source key | Country | Role | Mode | Adapter and status | Terms status | Terms decision | Terms URL | Terms reviewed | Technical status | Robots | What activation requires |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `mobile_de_public` | DE | acquisition | public_html | `mobile_de_public`: placeholder (`unimplemented`, raises `AdapterUnimplemented`) | restricted: the public terms contain a scraping restriction in section 11 | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14: include; terms risk accepted - an audit record, not permission) | https://www.mobile.de/service/agbPublic | 2026-10-06 | untested | obey. robots.txt **changed**: the 2026-10-14 re-check shows the `*` group now disallows `/fahrzeuge/search.html`, `/fahrzeuge/searchresults.html`, `isSearchRequest=true`, `pageNumber=` and `/fahrzeuge/details.html` except the single form `details.html?id=<n>&vip=1`. The search allow-list is therefore empty | **Discovery is blocked by robots.txt**; the owner's approval does not override it. Routes that remain: the official Search API (`mobile_de_api`, needs an agreement with mobile.de) or listing links the owner supplies (a detail page only in the robots-allowed `?id=<n>&vip=1` form, through a feature that does not exist yet). Access blocks stop the source; no evasion. |
| `autoscout24_de` | DE | acquisition | public_html | `autoscout24_public_de`: **implemented** (`autoscout24@1.0.0`, 2026-10-16, from owner captures; search cards only) and **fixture-tested** on its SYNTHETIC saved fixture set (`tests/adapters/fixtures/autoscout24_de/`, 2026-10-16); not live-tested | restricted: the consumer terms, sections 8.2-8.3, restrict automated queries and the creation or commercial use of a derived database | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14) | https://www.autoscout24.com/company/agb/ | 2026-10-06 | fixture_tested (step 5 done 2026-10-16 on synthetic saved fixtures; not a live verification) | obey. robots.txt checked 2026-10-10 and re-checked 2026-10-14 (unchanged): the `*` group disallows `/angebote/` (detail pages), `/lst?`, `/lst/?` and `cat=`; `/lst/<make>/<model>` search pages are allowed, so the source is **card_only** | **Step 5 done 2026-10-16**: saved SYNTHETIC fixtures under `tests/adapters/fixtures/autoscout24_de/` with `MANIFEST.yaml` (search-side spec-31 set; the detail categories do not apply to a card_only source and card-level equivalents stand in for them), then `technical_status: fixture_tested`. The only gate left closed is `enabled: false` (`crawl once` refuses a disabled source). Next, **steps 6-7**, after `autoscout24_it`: the owner's live smoke (docs/runbook.md section 9.1 with this key; one model page) to confirm the search filters (`pricefrom`/`priceto`/`kmto` are unverified on .de) and the unverified model slugs, then enabling. No detail fetches ever. Scheduled every 90 minutes (16 runs x 12 pages within the 200-request daily budget). |
| `autoscout24_it` | IT | acquisition | public_html | `autoscout24_public_it`: **implemented** (`autoscout24@1.0.0`, 2026-10-16, from owner captures; search cards and `/annunci/` detail pages) and **fixture-tested** on its SYNTHETIC saved fixture set (`tests/adapters/fixtures/autoscout24_it/`, 2026-10-16); not live-tested | restricted (same sections 8.2-8.3) | **proceed_acknowledged** (owner, 2026-10-10, re-confirmed 2026-10-14) | https://www.autoscout24.com/company/agb/ | 2026-10-06 | fixture_tested (step 5 done 2026-10-16 on synthetic saved fixtures; not a live verification) | obey. robots.txt checked 2026-10-10 and re-checked 2026-10-14 (unchanged): `/lst/<make>/<model>` search and `/annunci/` detail pages are not disallowed for `*` | **Step 5 done 2026-10-16**: saved SYNTHETIC fixtures under `tests/adapters/fixtures/autoscout24_it/` with `MANIFEST.yaml` covering the spec-31 set (normal, second-model and paginated search, empty result, normal, missing-price, contradictory-mileage, net/gross, conditional, damaged and removed detail, login wall, CAPTCHA/block, malformed and changed markup, injection), then `technical_status: fixture_tested`. The only gate left closed is `enabled: false` (`crawl once` refuses a disabled source). Next, **steps 6-7**: the owner's live smoke, this source first (docs/runbook.md section 9.1: a local uncommitted `enabled: true`, `sources sync --yes`, `crawl once --max-pages 1`, `worker --drain --queues detail`, then switching it off again) to confirm the unverified model slugs (config notes) and the live page structure, then enabling. One search page per configured model per run (12 models, `max_search_pages_per_run: 12`, 20 s spacing), scheduled every 3 hours (8 runs x 12 pages, half of the 200-request daily budget left for detail pages). |
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
| live_verified | Passed a permitted low-volume live smoke with recorded evidence. **No source has this.** |
| active | Enabled, every gate satisfied and running on schedule. **No source is active.** |
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
   the source's rate budget; then let the worker fetch the detail pages that one search page
   queued (`suv-deals worker --drain --queues detail`: at most `max_detail_jobs_per_run`; add
   `--max-jobs 1` to stop after the first). Confirm `access_state: ok` and healthy parser
   outcomes (`suv-deals sources inspect <key> --from-db`, `doctor`). For AutoScout24 the exact
   owner commands, including the local uncommitted `enabled: true` and switching the source off
   again, are in docs/runbook.md section 9.1. Record the spec 31 evidence fields:
   commit SHA and image digest, configuration revision and `adapter_version`, source URL,
   actual observation timestamp, crawl run and job ids with the redacted fetch outcome, and the
   persisted listing/revision/evidence ids. Then set `technical_status: live_smoke_passed`.
7. **Enable.** Set `enabled: true` through an auditable configuration revision.

After activation:
- A technical denial pauses the route and creates one deduplicated operational review item.
- Parser-health tripwires pause alerts and quarantine affected revisions.
- A change to the terms or to robots sends the source back to step 1.
