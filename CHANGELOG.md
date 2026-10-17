# Changelog

The build waves in order, derived from `git log --oneline` and the wave documentation
(docs/schema.md sections 9 to 11, docs/decisions/). Dates are commit dates. "WIP checkpoint"
commits are omitted; they only preserved interrupted work. **No wave activated anything against
a real service**: no live crawl, no real e-mail, no Slack, dot or Outlook on a real PC.

## Unreleased (on top of `7f7e187`)

Implausibly low mileage (live finding of 2026-10-17). The owner's `autoscout24_it` smoke stored a
search card "Nissan Qashqai 2.0 dCi DPF Tekna" (a 2007-2013 model) at EUR 3,000 with a card
mileage of 16 km: the seller almost certainly left out the thousands, so the real reading is
unknown. Screening treated only exactly 0 km as a placeholder, so the card would have screened
`eligible_primary`.

- **`MILEAGE_IMPLAUSIBLY_LOW`** (`domain.filters`, `needs_facts`, field `vehicle.mileage_km`): a
  positive reading below 1,000 km (`MIN_PLAUSIBLE_USED_MILEAGE_KM`), or below 2,000 km
  (`MIN_PLAUSIBLE_KM_PER_YEAR`) per full year since first registration once the car is at least
  3 full years old (`MIN_AGE_YEARS_FOR_KM_FLOOR`) on the screening date. Full years count from the
  last day of a month-only registration (31 December of a year-only one), so a missing month never
  makes a car look older. Checked after the 0 km placeholder and before the estimate branch (an
  estimated 150 km is just as implausible); never at or above the mileage ceiling, so
  `MILEAGE_TOO_HIGH` still rejects, and a reject from any other rule still wins. No exemption for
  new, "km 0" or demo offers: `NormalizedListing` carries no offer-condition field.
- `schemas/tools/deals_get_candidate.json` regenerated (new reason code). No fixture, pipeline
  test or migration changed.
- Tests: `tests/unit/test_filters.py` (the live card's shape, both floors and their boundaries with
  and without a registration month, estimates, reject precedence, `MILEAGE_TOO_HIGH`, 0 km),
  `tests/property/test_filters_properties.py` (below either floor is never eligible).

**Activation step 6 for `autoscout24_it` (2026-10-17).** The owner's live smoke at `25f43f8`
(docs/runbook.md 9.1: a 12-model `crawl once --max-pages 12`, then `worker --drain --queues detail
--max-jobs 5`) passed: the worker fetched and stored robots.txt itself (HTTP 200, unchanged since
2026-10-06), 12 search pages HTTP 200, `complete`, `access_state: ok`, 15 cards, 5 detail pages
ingested, no block; the source was switched off again. `config/sources/autoscout24_it.yaml` is now
`technical_status: live_smoke_passed` and stays `enabled: false` (enabling is step 7, the owner's
decision). The same walk strongly indicates that the seller-country filter `cy=I` works (15 cards
instead of the unfiltered walk's 49; its three foreign listings gone, its two Italian ones kept;
the seller countries of the other cards confirm it once their detail pages are read) and
requested all twelve model slugs (every one HTTP 200). `sources smoke-evidence` now prints each
revision's seller country. The record was completed the same day: the five step-6 detail ingests
(BE, AT, BE, FR rejected; one HTTP 410 recorded as removed), the status in the database and the
crawler gate (`existing_crawler` `live_verified` on the owner's workstation: firewall rules
present, isolation verified, `doctor --crawler` passed, image digest recorded); the ids are in
docs/evidence/autoscout24_it_live_smoke_2026-10-17.json. Evidence: docs/source_access_register.md "Live smoke
evidence" and the ACTIVATION_GATES.md evidence log; runbook 9.1 now says how the owner writes the
status into the database with its time (`sources set-technical-status`, before the next sync).

A smoke drain claims only its own source (2026-10-17). After the `autoscout24_it` smokes, 39 of
its detail jobs are still queued (the source is disabled again); the `autoscout24_de` smoke's
`worker --drain` would have claimed them first and blocked each one `source_paused` (no retry
timer, `jobs unblock` one by one) while `--max-jobs` was spent on the wrong source.

- **`suv-deals worker --drain --source KEY`.** `jobs.claim(source_key=...)` adds `source_id =
  (select id from app.sources where workspace_id = ... and source_key = ...)` to the same claim
  statement (`for update skip locked`, priority order, the incompatible-payload loop stays within
  the source); jobs of other sources and source-less jobs (valuation, seller inquiries) stay
  queued, untouched, and an unknown key claims nothing. Without `--source` the statement is
  unchanged. `Worker(source_key=...)` passes it to every claim. The key must be a source of the
  YAML registry (checked before the database is opened) and `--source` needs `--drain` (exit 2
  otherwise); a malformed key is `ValidationFailed`. An empty drain prints `No due jobs of source
  KEY.`
- **Docs.** Runbook 9.1 drains with `--source autoscout24_it` (`--source autoscout24_de` for the
  .de smoke) and says why; the register and ACTIVATION_GATES step 6 name `--source <key>`;
  schema.md section 7. Runbook 9.1 also says what `--source` does not do: the source's OWN older
  queued detail jobs still come first (the drain after the 2026-10-17 `cy=I` walk requested 5 of
  the earlier walk's pages; such fetches are not in that run's `smoke-evidence`), and on the
  `card_only` `autoscout24_de` a card job left queued is not blocked after the switch-off but
  ingested by the next worker, so the .de drain runs without `--max-jobs`.
- **Tests.** A database CLI test runs `worker --drain --source KEY` itself: only that source's
  jobs are claimed, the other source's and a source-less job stay queued, and an empty drain
  prints `No due jobs of source KEY.`

Finding of the owner's first live smoke (2026-10-17, `autoscout24_it`, `crawl once --max-pages 1`;
the search page answered 200, access ok): the live worker path never recorded robots.txt
(`sources inspect --from-db` showed the robots view empty, `sources smoke-evidence` listed only the
search fetch, `requests_today` was 1). robots.txt was obeyed only inside Crawl4AI
(`check_robots_txt: true`); the application's own robots module ran only in `capture-samples`.

- **robots.txt before every page request of the worker** (`crawling.robots_guard`, wired into
  `PolicyEnforcingCrawlClient` by `workers.runtime.crawl_session` for discovery, detail and recheck
  jobs alike). Before the first page request to a host the worker holds a current robots revision:
  reused while fresh, otherwise fetched through `robots.fetch_robots` (URL policy `robots` purpose:
  exactly `/robots.txt`; same-host redirects only, at most five hops, public DNS before every hop,
  fail closed) as a request of its own through the persistent budget gate (`purpose: robots`,
  counted in `requests_today`, spaced like any request, including the Crawl-delay of the revision
  it replaces at the daily refresh), stored with
  `sources_repo.record_robots_revision` and logged as a fetch attempt (`purpose: robots`, linked to
  the job and the crawl run) before the gate is released. Every search and detail URL (and a
  same-host redirect target) is evaluated with `robots.evaluate` for our product token
  (`SUVDealResearch`).
- **Outcomes.** A disallowed URL is never requested: a POLICY_DENIED page `robots_disallowed`
  (discovery: failed/partial traversal with the gap, job completed; detail: a visible
  `policy_denied` dead letter; never retried). An unreachable robots.txt (5xx, 429, network, an
  answer not read completely) is a complete disallow: no page request, `RobotsUnavailable`
  (`DenyReason.ROBOTS_UNREACHABLE`), the job released like a budget refusal without consuming an
  attempt, robots.txt asked again after 30 minutes. A 401/403/407 answer to the robots request is
  an access denial: the page is reported `access_blocked` without being requested (source route
  paused, job blocked). A robots `Crawl-delay` travels with every page request
  (`BudgetRequest.crawl_delay`) into the gate's effective spacing (capped at one day).
- **Freshness.** A revision is reused only within the UTC day it was fetched in (so at most
  24 hours, and always fetched again by the first request of a UTC day: one robots request per host
  and day in normal operation); an unreachable one for 30 minutes; an access denial never.
- **How robots.txt is fetched.** Directly by the worker through the netguard `SafeHttpClient`
  (`SafeHttpRobotsTransport`: the validated public IP pinned per connection, TLS verified,
  environment proxies ignored, no redirect followed by the client, 500 KiB cap, identity encoding
  only, the crawler's honest User-Agent; no stealth, proxy or CAPTCHA handling), built only with
  `SOURCE_NETWORK_ENABLED=true`; a real source without it fetches nothing
  (`DependencyUnavailable`). `SafeHttpError` carries the refused `Location` of a 3xx for the
  robots module. Crawl4AI's own `check_robots_txt` stays on as a second line.
- **Changes are audited, nothing switches.** A changed revision raises the existing
  `source.robots_changed` audit for every source on the host; nothing is enabled or disabled. A
  robots outage (no body, not parsed) and an access denial (401/403/407 without a body, e.g. a WAF
  block) are stored but compared with nothing, so neither the outage or block nor the recovery
  raises a review item, while a robots.txt that disappears does. A non-standard status (outside
  100..599, e.g. 999) is stored without a status: unreachable, the 30-minute retry, never a
  validation dead letter.
- **Evidence.** `sources smoke-evidence` prints the robots fetch among the fetches and
  `robots_revisions` (per host: time, HTTP status, SHA-256, availability incl. `access_denied`;
  no body). The stored SHA-256 and truncation flag are the robots module's own (bytes as fetched,
  the hash `capture-samples` reports);
  `sources inspect --from-db` shows the stored revision. A robots refusal is not counted as a
  parser failure.
- **Fixtures.** Every saved fixture set serves a SYNTHETIC `robots.txt` route (dealer sets;
  AutoScout24 IT/DE from the rules recorded in their `robots_summary`, regenerated); a missing
  route answers 404 (no robots.txt). No migration: `ops.robots_revisions` and the `robots` purpose
  of `ops.fetch_attempts` already existed.
- Tests: `tests/unit/test_robots_guard.py` (freshness, order, reuse, disallow, unreachable, access
  denial, crawl-delay, transports), `tests/integration/pipeline/test_robots_live_path.py` (real
  discovery/detail handlers on PostgreSQL), the AutoScout24 fixture pipeline and smoke-evidence
  checks, and the repository's change rule (`test_sources_repo.py`).

AutoScout24 seller-country filter (live finding of 2026-10-17). The owner's 12-model smoke of
`autoscout24_it` (`crawl once --max-pages 12`: 12 search pages HTTP 200, `complete`, 49 cards,
5 detail pages ingested) sent a search URL (`pricefrom`, `priceto`, `kmto`, `sort`, `desc`,
`ustate`) with no country filter, and three of the five ingested listings came from other
countries: a Dutch and a Belgian one (seller countries NL and BE, read back on 2026-10-17 from the
revisions; first reported as two Dutch dealers), rejected by screening (primary profile `source_countries` DE,
IT, CH -> `COUNTRY_NOT_IN_PROFILE`), and one was a German listing, which the country rule accepts
(screened `needs_facts`). With one page of 20 newest cards per model, foreign listings spend the
request budget and can hide every Italian or German one.

- **`search.cy` for AutoScout24** (`autoscout24.COUNTRY_FILTER_CODES`): the sites' own
  seller-country filter key. The codes (`I` on autoscout24.it, `D` on autoscout24.de) are the
  values the server-normalised `pageQuery` carried on the owner's captures, but those requests
  sent no `cy` (their own query has none): there the value was the server's default. The
  crawler's requests without `cy` were evidently answered with a different default (the foreign
  listings above), so whether an explicit `cy` narrows the results is **unverified on both
  sites** until the next live 12-model walk shows it: every ingested listing's seller country is
  the source's own (IT on `autoscout24_it`, DE on `autoscout24_de`). Validated when the adapter
  is built: only the source's own code is accepted (`I` for IT, `D` for DE); any other value
  (another country, lower case, a list, extra text) is refused. It is not implied by the source
  country: a source without `cy` sends exactly the URLs it sent before (documented in the adapter
  module).
- **Trade-off, if `cy` works as intended**: German sellers no longer come in through
  `autoscout24_it` (which fetches `/annunci/` detail pages); they are reached only through
  `autoscout24_de`, which is `card_only` (robots.txt disallows `/angebote/`), so with card facts
  and no detail page. Swiss sellers (CH is in the profiles' `source_countries`) are reached
  through neither AutoScout24 source while `autoscout24_ch` stays blocked.
- **Configuration.** `autoscout24_it` sets `cy: I` and `autoscout24_de` `cy: D`, each with a
  comment recording the finding; the search URLs now end in `&cy=I` / `&cy=D`. robots.txt
  disallows a query only on the bare `/lst?` and `/lst/?` paths (and, on .de, any `cat=` URL),
  not on `/lst/<make>/<model>`, so the URLs still pass the search path policy and robots.txt
  (tested with the evaluator against the recorded rules). Both sources stay `enabled: false`.
- **Fixtures regenerated** (`uv run python -m tests.adapters.autoscout24_fixture_set`): the
  routes of the 12 adapter-built model searches of each source (and the Italian
  redirect-to-list target) carry `cy`; no page changed.
- Tests: the built URL of every model carries the source's `cy` and passes the URL policy and the
  robots evaluator (which still refuses a `cat=` query on a .de model page), an unsupported `cy`
  is refused, a source without `cy` builds the old URL, and the cards of a filtered page are
  still parsed; the saved model-search routes carry `cy`.
- Docs: runbook 9.1 (the smoke URL now includes `cy`; after the change, any ingested listing
  whose seller country is not the source's own, including a DE or CH listing that screening
  accepts, means the filter was not applied; `cy` joins the filters that stay unverified until
  live evidence is recorded), the source access register rows and the source configuration
  notes.

Owner decision of 2026-10-16 („сакам на секои 30 минуту да го наместиш само од 08:00 до 22:00"):
discovery of AutoScout24 Italy and Germany every 30 minutes, only from 08:00 to 22:00 (the
owner's local time, Europe/Skopje).

- **Discovery windows (`schedule` in a source's YAML).** `domain.sources.DiscoverySchedule`
  (`interval_minutes` dividing the day, quoted `window_start`/`window_end` with an exclusive end,
  an IANA `timezone`; frozen, extra keys refused): run slots are wall-clock times in that zone,
  computed with zoneinfo from database time (no migration, no new column): 08:00, 08:30, ...,
  21:30, 28 a day in CET and CEST alike (the DST changes of 2026-10-25 and 2027-03-28 are
  tested; a repeated or skipped hour inside a window is run once at most). A slot may start until
  the next slot or the window end; outside the window the schedule waits for the next day's start.
  After downtime ONE job runs for the slot that is live now and the missed slots are a recorded
  gap (never a burst). A source without the block keeps the budget-stretched global interval.
- **The window holds when a run starts, not only when it is queued.** The discovery handler
  re-checks the job's slot against database time before any request: a job claimed after its
  slot's run window (released by the budget gate to 00:00 UTC, retried after a 429 or a transient
  failure, reclaimed after a lost lease, or queued too long) fetches nothing, completes with a
  `cancelled` run and the gap `slot_expired: <slot> ...` on the schedule. A first-page refusal or
  failure whose retry would start at or after the run-window end ends the job the same way
  instead of deferring it (a 429 still backs the schedule off as a failed run).
- **Budget fit, never stretched.** Loading the YAML refuses a window whose day of runs (the most
  slots in any UTC budget day) x `max_search_pages_per_run` exceeds the search share of
  `daily_request_budget` (all of it for `card_only`; otherwise all but one run's detail allowance,
  `max_detail_jobs_per_run`), naming the numbers; the registry load error now carries the
  validator messages. The YAML cannot know the enabled search profiles, so that check counts one;
  the scheduler re-checks with the real count: `ensure_schedules` refuses the source
  (`WorkspaceTick.schedules_over_budget`, a warning log, no row created or changed) and
  `advance_schedule` skips the run of an existing row (`slot_skipped: schedule_over_budget`).
- **The remaining runs' search pages are reserved at runtime.** Detail pages are not capped per
  run (one job per new or changed card, plus stale refreshes and rechecks), and search and detail
  share one daily counter. For a source with a window the budget gate now refuses a detail request
  once the day's requests plus what the day's remaining runs still need (runs left x search pages
  x enabled profiles, `rate_limits.SearchReserve`) reach the budget, until the last run window of
  the day ends (or the next UTC day); search requests are not affected. A busy detail day (the
  first day after enabling, a backlog released at 00:00 UTC) can no longer starve the evening runs.
- **Slots the scheduler cannot reach are refused.** The scheduler wakes only on its
  `SCHEDULER_INTERVAL_SECONDS` UTC grid, so a slot whose run window holds no tick would be lost
  silently (e.g. a 10-minute interval, or `SCHEDULER_INTERVAL_SECONDS=3600` with 30-minute slots).
  `ensure_schedules` refuses such a window (`WorkspaceTick.schedules_off_tick`, a warning log, no
  row created or changed) and the tick leaves its due schedules unadvanced (skipped
  `schedule_off_tick`); `sources inspect` names the problem.
- **Existing schedule rows follow the configuration.** `ensure_schedule` moves a row whose
  interval or future due time no longer matches the configured cadence (a changed or removed
  window, a changed interval) to the new interval and next slot, under a row lock and audited
  (`schedule.reconfigure`, before/after values); an unchanged row is not touched, and a slot
  already run is never run again. Before, a row kept the interval it was created with.
- **AutoScout24 IT/DE configuration.** Both get the window (30 min, 08:00-22:00,
  Europe/Skopje): 28 runs x 12 search pages = 336 search requests a day. `autoscout24_de`
  (card_only) `daily_request_budget` 200 -> 350. `autoscout24_it` (fetches detail pages)
  200 -> 600 (the owner's figure): 336 for search, 264 for detail pages, which the reserve above
  keeps from the runs up to 21:30 (a day with more detail pages leaves the rest for the next UTC
  day). Spacing (20 s), one connection per host, robots and every gate are unchanged; both stay
  disabled.
- `tzdata` is a runtime dependency so zoneinfo works in a container image without system zone
  files; `sources inspect` shows the discovery cadence.
- Tests: slot math (window edges, DST, downtime, UTC budget day), configuration validation, row
  reconciliation, tick reachability, the search reserve over a simulated busy day and slot expiry
  (unit); windowed schedules on PostgreSQL with a frozen clock, a moved window with the same
  interval, the gate's reserve
  (`tests/integration/repos_sources_listings/test_schedule_windows.py`), and expired or
  past-window jobs fetching nothing, the off-tick refusal (pipeline scheduler tests).

Owner decisions of 2026-10-16 („не треба да бидат строги правилата ... само за да имаме
орјентација", „Цените се извадени од Reklama5"; ADR 0005).

- **MK price orientation framework.** When the spec-15 comparable set gives no proceeds, a
  separate relaxed orientation (`orient_comparables`, `mk-orientation@1.0.0`) uses the 5 nearest
  same-model MK points: only make/model, market, lineage, price/FX, freshness and condition stay
  hard; every other difference is labelled with a PROPOSED distance penalty. Fallback order: a
  close asking-price orientation (distance <= 4), the strictly matched owner estimates, any
  asking-price orientation, the relaxed owner-estimate orientation. Warnings
  `PROCEEDS_FROM_MK_ORIENTATION` and `MK_ORIENTATION:<kind>`; never alert eligible, never inquiry
  evidence (it cannot make a candidate `KNOWN_COSTS_EXCEED_PROCEEDS`); orientation and
  owner-estimate proceeds expire with their oldest point. The candidate read covers ±10 years.
- **Duplicates by ad URL.** The same ad recorded on several days counts once (`DUPLICATE_OF_URL`,
  query string kept as the ad id). The newest row that passes the mode's hard checks is kept, so
  a newer re-record that lost its mileage, has a model typo or an out-of-window year never hides
  the valid older one. Criteria version `mk-comparables@1.3.0`.
- **Orientation closeness and labels.** "Close" (fallback step 1) is tested on the verified
  distance: an unknown fuel, gearbox or drive counts as the mismatch it may hide, so an ad of
  unknown fuel never beats a strictly matched owner estimate. The orientation's own labels
  (`ORIENTATION_ONLY`, `ORIENTATION_DISTANT`, `ORIENTATION_EMPTY`, FX notes) reach the valuation
  warnings.
- **Fallback valuations stay research candidates.** A valuation whose proceeds come from any
  fallback step keeps its figures but stays `research_candidate`; the candidate list/detail, the
  review queue and the MCP typed warnings read the stored flag, and the 15-day evaluation's best
  supported economics counts only well-matched candidates.

AutoScout24 step-5 review fixes (2026-10-16). Both sources stay `enabled: false` and
`fixture_tested`; nothing was fetched.

- **Fixtures** (now IT 37 pages, DE 23 pages, regenerated). `detail_price_missing.html` shows no
  amount anywhere (no `priceRaw`, no `Offer.price` and no formatted price; `detail_page(price=None)`
  replaces the `ld_price` option). The German '+' slug is invented
  (`renault-captur-synthetic+edition-tce-140-benzin-grau`). New: last-model pages reporting 45
  results next to 3 cards with `numberOfPages` 1 or missing (the walk ends PARTIAL, never
  COMPLETE), an empty listings array without `numberOfResults` (drift), instruction wording
  without markup and markup without wording (card and detail; each flags
  `SELLER_TEXT_SUSPICIOUS` on its own), and an Italian detail page whose JSON-LD `Offer.price`
  disagrees with `priceRaw` (`detail_page(ld_offer_price=...)`). The German card test pins that
  the displayed `priceRaw`, not `tracking.price`, is the card's asking price.
- **Phone check.** The marketplace-page check also finds national numbers with a two-digit area
  code followed by a separator (`06 12345678`, `02 1234 5678`).
- **`suv-deals sources smoke-evidence KEY [--run-id ID]`** (read-only): the spec-31 evidence of a
  crawl run (run and job ids, the detail jobs it queued, redacted fetch outcomes, observed
  listings with card price/mileage, revision and field-evidence ids, the configuration revision).
- **Docs.** Runbook 9.1, the register and ACTIVATION_GATES: `max_detail_jobs_per_run` does not
  bound a detail drain (each detail job is its own run), so the smoke uses
  `worker --drain --queues detail --max-jobs 5`; the one-page smoke checks only `renault/captur`
  (unverified slugs need a full 12-model walk); it cannot show whether .de applies
  `pricefrom`/`priceto`/`kmto` (`pageQuery` is not recorded; card prices are only an indirect
  check); the report-back includes the `sources smoke-evidence` JSON. Config notes corrected.

AutoScout24 activation step 5 (2026-10-16): saved SYNTHETIC fixtures, `fixture_tested`. Not a
live verification; both sources stay `enabled: false`, and the next step is the owner's live smoke.

- **Saved fixture sets** `tests/adapters/fixtures/autoscout24_it/` (31 pages) and
  `autoscout24_de/` (20 pages), each with a `MANIFEST.yaml` (`source_key` of the real source,
  `designation: synthetic`, created 2026-10-16, `parser_version: autoscout24@1.0.0`, a
  description per page) and routes on the source's own host that pass its URL policy (the
  adapter's own primary-profile search URL for each of the 12 models, `?fixture=<case>` variants
  for the rest). Generated deterministically by `tests/adapters/autoscout24_fixture_set.py` from
  the existing synthetic builders (structure of the owner's captures, invented values only, every
  file starting with the synthetic marker); a test keeps the committed files equal to the
  generator output. Italy covers the spec 31 set: normal search, the second model page of the
  walk, a paginated result, an empty result, non-organic results only, normal detail,
  missing-price (on request, no amount), contradictory mileage, net and gross wording,
  conditional price, damaged vehicle, removed listing (200, 410, 404 with wording), plain 404,
  a detail redirected to the result list, login wall, CAPTCHA (200/403/503), 429, 503, empty
  shell, malformed and changed markup, a detail of another listing, a non-active status and
  prompt injection/XSS. Germany is `card_only`, so the detail categories do not apply: the
  MANIFEST names the card-level stand-in for each (missing-price, contradictory-mileage,
  net/gross and damaged cards, a removed card, 404 model pages) next to the same search, block
  and drift cases.
- **Tests.** `tests/adapters/test_autoscout24_fixtures.py` runs the real adapter over every route
  through the offline `FixtureCrawlClient` (cards and facts, price types, VAT basis, warnings,
  conflicts, drift/`unexpected_content`, access blocks, removal, the 404 model skip, injection
  flagged and stripped and never obeyed, no seller data in any model, the full 12-model walk,
  Germany never requesting a detail page, required coverage for both sources).
  `tests/integration/pipeline/test_autoscout24_fixture_pipeline.py` (DB) runs one capped
  discovery (what `crawl once --max-pages 1` enqueues) and its detail jobs through the worker over
  the saved pages, with the real configuration switched to `mode: fixture` and enabled IN MEMORY
  for a throwaway workspace. `test_fixture_manifests.py`: a marketplace fixture directory may name
  only its own source's configured host(s); the marker, no other real host, `.example`-only
  e-mail addresses and no phone numbers are checked as before (dealer fixture checks unchanged).
- **Configuration.** `config/sources/autoscout24_it.yaml` and `autoscout24_de.yaml`:
  `technical_status: fixture_tested` (comment names the fixture set and date); `enabled: false`
  unchanged, so `gate_status` lists exactly one problem ("source is disabled in configuration"),
  `doctor` reports "not active: 1 gate problem(s)" and `crawl once` still refuses both.
- **Docs.** docs/runbook.md section 9.1 has the owner's live smoke commands (a local, uncommitted
  `enabled: true` for ONE source, `sources sync --yes`, `crawl once --source autoscout24_it
  --max-pages 1` and `worker --drain --queues detail` with `SOURCE_NETWORK_ENABLED=true` and
  `CRAWL4AI_BASE_URL`, `sources inspect --from-db`, `doctor`, and switching it off again);
  ACTIVATION_GATES.md, the source register and IMPLEMENTATION_STATUS.md record step 5.
- Test builders: the card's placeholder image moved to the reserved host `images.example`, cards
  carry German `ariaLabel`s on .de, `search_page(page_query=...)`, and `detail_page` gained
  `on_request`, `ld_price` and verbatim script overrides; the JSON-LD block escapes `<` like the
  application state.

AutoScout24 Italy and Germany adapter (2026-10-16), built from owner-captured pages (structure
only; no captured page is committed). Both sources stayed `enabled: false` and `untested` at that
point; step 5 (above) followed the same day.

- **`adapters/autoscout24.py` (`autoscout24@1.0.0`).** One adapter for `autoscout24_public_it` and
  `autoscout24_public_de`. Search pages are read from `__NEXT_DATA__` `props.pageProps.listings`
  (Organic/Main cards only): listing UUID (must equal the URL's trailing UUID), canonical URL,
  bounded title, make/model, first registration, km, EUR price, fuel (code cross-checked with the
  localised label), gearbox (IT/DE labels), kW, cm3, seller type and country. Seller names, phones,
  links, streets and coordinates are never read into a model. Missing or invalid `__NEXT_DATA__`,
  missing listings while results are reported, or a page of unreadable cards is drift
  (`unexpected_content`); challenge pages and 401/403 are access blocks. Placeholder and absurd
  prices carry no amount. Italian `/annunci/` detail pages are parsed from `listingDetails.vehicle`
  (drive, kW, cm3, first registration, production year, fuel incl. plug-in flag, gearbox, km,
  body) with the JSON-LD Car as fallback and cross-check (disagreements are field conflicts; a
  JSON-LD Car naming another listing is refused). Net-price seller wording ("al netto dell'IVA",
  "escluso IVA", "IVA 22% esclusa", "IVA non inclusa", "senza IVA", "prezzo netto", "zzgl."/"exkl."/
  "ohne MwSt.") sets `price.basis = net` without storing the description, so screening treats
  the amount as a lower bound and the listing becomes `needs_facts` (the site's "IVA deducibile"
  flag on a final price stays VAT shown, gross); commercial (N1, "AUTOCARRO") registrations are
  flagged. A conditional price ("Prezzo con condizioni", `isConditionalPrice`)
  is never a full asking price, on cards and on detail pages alike (`PRICE_CONDITIONAL`, type
  `unknown`); `prices.isFinalPrice: false` warns `PRICE_NOT_FINAL`. The Euro class the listing
  states (`environmentEuDirective`, never a model-data fallback) fills
  `documentation.emissions_class`, so the MK import gate can check the Euro minimum. Germany stays
  `card_only`: its detail URLs are identity only and `fetch_detail`/`parse_detail` refuse without
  any request.
- **Card-sourced revisions for `card_only` sources.** `SearchObservation.card_listing` carries a
  card_only card's own facts (make/model, first registration, km, price, fuel, gearbox, kW, cm3,
  seller type, country; `FACTS_FROM_SEARCH_CARD`). `listings_repo.ingest_search_page` puts them
  in the payload of the new/changed-card detail job (a waiting job takes over newer facts) and
  `crawling.detail` ingests them through `ingest_detail` WITHOUT any request, so German listings
  get revisions and are screened (no drive or body type on cards: those stay unknown). Jobs
  without card facts still complete as `card_only_source` skips. An unchanged card whose facts
  differ from the listing's current revision (facts outside the card hash such as damage or first
  registration, a parser fix, or a revision never promoted) queues a `card_facts_changed` job; a
  waiting job that a worker claims before the newer facts are patched in no longer swallows them
  (a new job is queued).
- **Per-model searches.** `search.models` lists the owner's twelve models (Captur, Qashqai, Juke,
  Sportage, Mokka, Mokka X, Duster, 2008, ix35, Antara, Yeti, SX4 S-Cross); one discovery run walks
  them with an opaque `model:<n>` cursor through the existing traversal, one page per model,
  newest first (`sort=age&desc=1`) with the profile's price band and km ceiling
  (`pricefrom`/`priceto`/`kmto`) and `ustate=U` on Italy. `max_search_pages_per_run: 12`, 20 s
  spacing. A model slug answering a genuine 404 is skipped (counted in the cursor, `:nf<k>`); a
  policy refusal is never a skipped model; a 404 of the captured `renault/captur` route or of
  every model, and a page whose listings have no Organic/Main card while results are reported,
  are drift (`unexpected_content`). Any incomplete model page makes the run partial, so absence
  marking only follows a complete walk. Unverified slugs are named in the config notes.
  The scheduler sizes a source's cadence to its daily request budget
  (`domain.sources.discovery_interval_seconds`): a source whose search pages per run (times its
  enabled profiles) do not fit at `SCHEDULER_INTERVAL_SECONDS` runs less often instead of
  exhausting the budget in the first hours of each UTC day (Germany every 90 minutes, 192 of 200
  requests; Italy every 3 hours, half of the budget left for detail pages); with the one enabled
  profile every other shipped source keeps the global interval. Superseded for AutoScout24 by the
  owner's 08:00-22:00 discovery window (entry above).
- **Fix: capture redaction broke JSON escapes.** `sample_capture.redact_html` swallowed the letters
  of a `\u003e`/`\n` escape before an e-mail address, leaving `\[redacted-email]` (invalid JSON
  in saved pages); escapes are now matched as whole tokens and kept intact, in linear time (long
  escape runs such as escaped CJK text no longer rescan quadratically). International phone
  numbers right after an escape (`\u003cbr\u003e+39 ...`, `\n+39 ...`) are masked too, and a
  `tel:` value no longer takes the backslash of an escaped closing quote (`href=\"tel:...\"`).
- **Fix: the schema.org reader missed `Product.offers.itemOffered`.** `_extract` now yields the
  offered Car with its Offer (only from a non-vehicle node: a Car naming itself again as its
  offered item stays one vehicle), so the generic dealer adapter reads model, km, fuel, gearbox,
  drive, cm3, kW (KWT preferred) and body on such pages; localised drive/gearbox labels use small
  exact-word maps (`adapters/_vocabulary.py`). The generic reader keeps `productionDate` as the
  production year, never a first registration; only the AutoScout24 adapter reads Italy's
  `productionDate` (which holds the registration date) as first registration.
- Tests: synthetic AutoScout24 pages with the captured structure and invented values
  (`tests/adapters/`; only generic engine specifications match a real car), covering IT
  and DE search cards, the IT detail page, drift and blocks, model mismatch, prices, the model walk,
  query building and that Germany never fetches a detail page.

Owner decision of 2026-10-16 ("Моја проценка": the MK prices of the owner's model list are the
owner's own estimates; ADR 0004).

- **Owner estimates as the MK resale fallback.** A valuation whose MK comparable set has no asking
  price or sale uses the owner's matching `owner_estimate` rows as proceeds (basis
  `owner_estimate`, labelled as the owner's assumption, no negotiation discount, never approved,
  research still needed, warning `PROCEEDS_FROM_OWNER_ESTIMATE`). Real MK evidence always wins.
- **Canonical model names.** Comparables compare make/model by taxonomy names (`Mokka X` ->
  `Mokka`, `Skoda` -> `Škoda`), and the candidate lookup reads both spellings. Criteria version
  `mk-comparables@1.2.0`.
- **Taxonomy `2026-10-16.1`.** Renault Captur, Nissan Juke, Opel Mokka (alias Mokka X) and
  Peugeot 2008 added as crossovers.
- **Estimate files.** `data/market/owner_estimates_mk_2026-10-16.json` (nine confirmed models) and
  `..._to_verify.json` (four models, not to be imported until confirmed).
- Tests: owner-estimate selection and proceeds, canonical names, the estimate files, and a pipeline
  test on PostgreSQL (fallback used without MK ads; MK asking prices win).
- **`scripts/readiness_check.sh` (owner-run).** One run reports tools, `doctor`, the crawler
  container, the firewall check and a reachability probe of Reklama5, Pazar3, AutoScout24 IT/DE/CH,
  CarForYou and Tutti, directly and through Crawl4AI (`capture-samples --via crawl4ai`: headless,
  honest User-Agent, no stealth). Report: `captures/readiness/<UTC>/report.txt`.
- **Home-page probe paths.** Reklama5, Pazar3, AutoScout24 CH, CarForYou and Tutti now list their
  host and `^/$` only, for that probe; they stay disabled with no detail path.

Owner decisions of 2026-10-16 on the MK sources ("Само факти"; the owner read both sites'
pages, which answer 403 to the build environment). Nothing is enabled.

- **Reklama5 terms recorded as `restricted`.** The terms of use (updated 03.07.2026) have no
  automated-access clause but forbid collecting users' personal information and copying content of
  other people's ads.
- **Pazar3 recorded as `no_restriction_found`.** Only a privacy policy (28.05.2025) is published;
  the advertising-rules link opens it. Not a licence.
- **Facts only for both.** Make, model, year, km, fuel, gearbox, asking price and the ad URL; no ad
  text, photos, seller names or contact data, and no retained page snapshots.
- **Swiss sources blocked from the owner's network too** (2026-10-16 probe: AutoScout24 CH and
  Tutti 403, CarForYou robots.txt unreachable). They stay off; `docs/source_access_register.md`
  updated.

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
