# Source access register

Status date: 2026-10-06. Spec sections 5, 8, 24, 25, 31 and 32.

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

Every source below is `enabled: false`. No source is live-verified or active.

| Source key | Country | Role | Mode | Adapter and status | Terms status | Terms decision | Terms URL | Terms reviewed | Technical status | Robots | What activation requires |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `mobile_de_public` | DE | acquisition | public_html | `mobile_de_public`: placeholder (`unimplemented`, raises `AdapterUnimplemented`) | restricted: the public terms contain a scraping restriction in section 11 | pending | https://www.mobile.de/service/agbPublic | 2026-10-06 | untested | obey | Steps 1 to 7. Establish which terms apply to this use first. Permission or an agreement is the lower-risk route. The optional official API is tracked separately as `mobile_de_api`. |
| `autoscout24_de` | DE | acquisition | public_html | `autoscout24_public_de`: placeholder | restricted: the consumer terms, sections 8.2-8.3, restrict automated queries and the creation or commercial use of a derived database | pending | https://www.autoscout24.com/company/agb/ | 2026-10-06 | untested | obey | Steps 1 to 7. Verify whether the .com terms or country-specific (autoscout24.de) and business-use terms apply. |
| `autoscout24_it` | IT | acquisition | public_html | `autoscout24_public_it`: placeholder | restricted (same sections 8.2-8.3) | pending | https://www.autoscout24.com/company/agb/ | 2026-10-06 | untested | obey | Steps 1 to 7. Verify whether the .com terms or Italy-specific (autoscout24.it) and business-use terms apply. |
| `autoscout24_ch` | CH | acquisition | public_html | `autoscout24_public_ch`: placeholder | unreviewed: the Swiss site's own operator and terms have not been identified, so CH is reviewed separately from the EU markets | pending | not identified | not reviewed | untested | obey. robots.txt checked 2026-10-06: it has no `User-agent: *` group. This is recorded as a fact, not as permission (`robots_checked_at`/`robots_summary` in the YAML). | Identify and review the Swiss terms, then steps 1 to 7. The robots module must still fetch and store the revision, and named groups must be checked against our user agent. |
| `subito_it` | IT | acquisition | public_html | `subito_it_public`: placeholder | unreviewed | pending | not identified | not reviewed | untested | obey | Verify the exact domain(s), terms and public routes, then steps 1 to 7. |
| `automobile_it` | IT | acquisition | public_html | `automobile_it_public`: placeholder | unreviewed | pending | not identified | not reviewed | untested | obey | Verify the exact domain(s), terms and public routes, then steps 1 to 7. |
| `pazar3_mk` | MK | mk_comparable | public_html | `pazar3_mk`: placeholder | unreviewed | pending | not identified | not reviewed | untested | obey | Steps 1 to 7. This is comparable evidence only, never an acquisition source. Asking prices are not realized sales. Avoid seller contact data. |
| `reklama5_mk` | MK | mk_comparable | public_html | `reklama5_mk`: placeholder | unreviewed | pending | not identified | not reviewed | untested | obey | Same as `pazar3_mk`. |
| `mobile_de_api` | DE | acquisition | official_api | `mobile_de_api`: feature-flagged skeleton that raises `DependencyUnavailable('mobile.de Search API entitlement not verified')` | unreviewed: no API agreement has been obtained | pending | none. Documentation: https://services.mobile.de/docs/search-api.html | not reviewed | untested | obey | Owner-approved account or agreement and a recorded entitlement reference. Server-side credentials (marketplace login credentials are not API credentials). An implementation written from the official documentation, recorded fixtures and a low-volume test. This must never block the public-page framework. |
| `example_dealer_template_de` | DE | acquisition | public_html | `schemaorg_dealer` (`schemaorg_dealer@1.1.0`): the generic adapter is implemented and fixture-verified on synthetic sources | unreviewed | pending | none (template) | not reviewed | untested: no pages of a real dealer have been captured | obey | This is a template, not a source. Copy it for a permitted dealer and complete steps 1 to 7 for that dealer. |
| `carforyou_ch` | CH | acquisition | public_html | `carforyou_ch_public`: placeholder (`unimplemented`) | unreviewed | pending | not identified | not reviewed | untested | obey (not checked) | Candidate domain `www.carforyou.ch` is **unverified**: nothing was fetched. Verify the exact domain(s), operator, applicable Swiss terms, robots.txt and public routes, then steps 1 to 7. |
| `tutti_ch` | CH | acquisition | public_html | `tutti_ch_public`: placeholder (`unimplemented`) | unreviewed | pending | not identified | not reviewed | untested | obey (not checked) | Candidate domain `www.tutti.ch` is **unverified**. General classifieds with private sellers: same verification, then steps 1 to 7. Store no seller contact data and record the seller type. |
| `comparis_ch` | CH | acquisition | public_html | `comparis_ch_public`: placeholder (`unimplemented`) | unreviewed | pending | not identified | not reviewed | untested | obey (not checked) | Candidate domain `www.comparis.ch` is **unverified**. It is an aggregator that may republish third-party listings, so check its own terms and robots.txt **and** the original source's terms. Record the original source attribution, prefer the original source where permitted and deduplicate. Then steps 1 to 7. |
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

### Switzerland

Switzerland is an initial acquisition market. Its sources, wording, CHF handling, export/transit
costs and open owner decisions are described in [docs/markets/switzerland.md](markets/switzerland.md).
No Swiss source is enabled, verified or active.

## Adapter status vocabulary (spec section 32)

| State | Meaning here |
|---|---|
| implemented | Code exists and passes unit tests. Placeholders are **not** implemented. |
| fixture_verified | Passes the saved-fixture suite (`tests/adapters`). This is true for the generic `schemaorg_dealer` adapter on synthetic fixtures only. |
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
6. **Live low-volume smoke.** Run this with `SOURCE_NETWORK_ENABLED` and within the source's
   rate budget, using the `live` pytest marker (it never runs automatically). Fetch one search
   page and one detail page. Confirm `access_state: ok` and healthy parser outcomes. Record the
   commit, configuration and parser versions, source URL, observation time and the redacted
   fetch outcome. Then set `technical_status: live_smoke_passed`.
7. **Enable.** Set `enabled: true` through an auditable configuration revision.

After activation:
- A technical denial pauses the route and creates one deduplicated operational review item.
- Parser-health tripwires pause alerts and quarantine affected revisions.
- A change to the terms or to robots sends the source back to step 1.
