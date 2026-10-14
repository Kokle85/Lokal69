# Source terms and robots.txt review (2026-10-14)

Owner decision of 2026-10-14: "сакам сите да го вклучиш" (include every remaining source). This page
records what was checked before that decision was written into `config/sources/*.yaml`. A stored
owner decision (`proceed_acknowledged`) is an audit record of an accepted terms risk, never legal
permission, and it opens no gate by itself (docs/source_access_register.md).

Method. One research agent per pair of sources, then an independent verifier that re-fetched
robots.txt (comparing sha256) and re-read every quoted clause. Only robots.txt, terms, imprint
and home pages were requested; no search, listing, detail, sitemap or API URL. User agent
`SUVDealResearch/0.1 (+private research; contact owner)`, TLS verification on, at least 2 s between
requests to a host, no retries around blocks. Raw copies stay outside the repository (session
scratchpad).

All access results were observed from the build environment, a cloud data-centre network. The
owner's network may be answered differently. Wherever the crawler meets a block or challenge, it
records `access_blocked` and stops the source; nothing solves CAPTCHAs or evades bot protection.

## Results

| Source | Host and operator | Terms | robots.txt for our user agent | Access from the build environment |
|---|---|---|---|---|
| `subito_it` | `www.subito.it` (apex redirects). Subito.it S.r.l. con socio unico, Milano, P.IVA 05526340962, Adevinta group (imprint `info.subito.it/about.htm`) | **restricted**. Condizioni Generali, updated 2026-09-22 (quotes below) | `*` group: allows `/`, disallows `/vi/`, `/vim/`, `/li?`, `/lim?`, `/utente/`, `/annunciutente/`, checkout and pagination `*/?o=*`, `*&o=*`. Header comment forbids automatic access without permission. sha256 `bf3e48cb...` | home page HTTP 403 (Akamai "Access Denied") |
| `automobile_it` | `www.automobile.it` (apex redirects). Marktplaats B.V., Amsterdam, KvK 28058712, Adevinta group (`/policies/chi-siamo`) | **restricted**. Condizioni, in force since 2021-06-29 (quotes below) | `*` group: disallows `/api/*`, `/rest/*`, `*?b=*`, `*?d=*`, `*/catalogo-dealer*`, `/area-privata*`, `/mappa-province/*`, `/fb/*`, `*/automobile/mbl.it.*`, `/post/*/flag/*`, `/post/*/_share`. sha256 `0fc830ad...` | home page HTTP 200 |
| `autoscout24_ch` | `www.autoscout24.ch`. Operator unverified (search-listing lead: SMG Swiss Marketplace Group AG, Zurich) | **unreviewed**: GTC `/de/legal/gtc` not readable | no `*` group, no group for our token (silence is not permission). sha256 `ac07ed51...` (unchanged) | every HTML page HTTP 403, Cloudflare challenge |
| `comparis_ch` | `www.comparis.ch`. Operator unverified (lead: comparis.ch AG, Zürich) | **unreviewed**: `/info/copyrights` not readable | `*` group (227 rules) **disallows the car-marketplace result and detail endpoints**: `/carfinder/marktplatz/result.aspx`, `/carfinder/marktplatz/details/show/`, `/marktplatz/details/show`, `/carfinder/searchservice/`, `/carfinder/detailsservice/`, `/carfinder/api/`, `/comparis/searchresult*`. sha256 `94d20caa...` | HTML pages HTTP 403, DataDome CAPTCHA |
| `carforyou_ch` | not verified | **unreviewed** | not retrievable (unreachable = complete disallow under the robots module) | TLS connection reset in 8 of 9 attempts, once a certificate not matching the host name (verification never bypassed) |
| `tutti_ch` | not verified | **unreviewed** | robots.txt answered HTTP 403 (HTML) | every request HTTP 403 |
| `pazar3_mk` | `www.pazar3.mk` (apex redirects). Operator unverified (lead: PAZAR3.KOM DOOEL, Resen) | **unreviewed** | not readable (Cloudflare block) | every request HTTP 403, Cloudflare "Sorry, you have been blocked" |
| `reklama5_mk` | `www.reklama5.mk` (apex `reklama5.mk` serves the same, no redirect). Operator unverified (lead: Bigmedia DOOEL) | **unreviewed** | no `*` group, no group for our token (silence is not permission). sha256 `f82b0477...` | HTML pages HTTP 403, Cloudflare managed challenge |

Operator "leads" come from search-engine listings, not from fetched pages, and are not recorded as
facts. The verifier confirmed every robots hash and every quoted clause below; it added no clause
that changes a status.

## Quoted clauses

### Subito (https://info.subito.it/policies/condizioni-generali.htm)

- Art. 5: "Resta infine inteso che ogni eventuale utilizzo di robot, spider, scraper e/o ulteriori
  strumenti automatici per accedere al sito e/o per estrapolare i relativi dati, contenuti,
  informazioni è espressamente vietato." (Any use of robots, spiders, scrapers or other automated
  tools to access the site or extract its data is expressly prohibited.)
- Art. 10: "Subito.it S.r.l. vieta l'utilizzo da parte di software/applicazioni/siti cd. aggregatori
  e/o terzi, non espressamente e previamente autorizzati, di qualunque contenuto afferente al
  Servizio." (Unauthorised aggregators and third parties may not use any Service content.)
- Art. 3 applies the terms to users who only consult listings; art. 4 forbids misuse of contact
  details found on the platform.
- robots.txt header: "It is expressively forbidden to use search robots or other automatic methods
  to access Subito.it. Only if Subito.it has given such permission can be accepted."

### automobile.it (https://www.automobile.it/policies/condizioni)

- Art. 4 (users undertake not to): "utilizzare qualsiasi robot, spider, scraper o altri strumenti
  automatici per accedere al Sito e/o alla relativa applicazione mobile e raccogliere contenuti ed
  informazioni per qualsiasi scopo senza il nostro previo consenso scritto"; "aggirare le misure
  adottate al fine di prevenire o limitare l'accesso al Sito"; "copiare, riprodurre, alterare,
  modificare o diffondere le inserzioni degli Utenti registrati ovvero dei Concessionari senza il
  loro consenso espresso".
- Art. 11: "Gli Utenti possono effettuare la ricerca dei veicoli nei database del Servizio
  Marktplaats esclusivamente tramite la barra di ricerca disponibile sul Servizio Marktplaats. Non è
  permesso cercare veicoli aggirando l'uso delle barre di ricerca, in particolare usando searchware
  per accedere ai database Marktplaats."
- Art. 14: site content is protected "come un'opera collettiva e/o una raccolta di opere"; no
  copying or use of protected content "senza espresso consenso scritto".

## Consequences recorded in the configuration

- Every source above now carries `terms_decision: proceed_acknowledged`, actor "owner (decision
  recorded 2026-10-14)", and stays `enabled: false` with an unimplemented adapter.
- `subito_it` and `automobile_it`: `terms_status: restricted`, terms URL and review date recorded,
  `allowed_hosts` set to the canonical host; search and detail paths stay empty until routes are
  verified from permitted page captures.
- The other six keep `terms_status: unreviewed` (the gate "terms not reviewed" stays closed) until
  the owner reads the terms in a normal browser and they are recorded.
- `comparis_ch`: the known car-search result and detail routes are disallowed by robots.txt.
- Sources whose pages answered blocks or challenges cannot be crawled from the build environment;
  the crawler stops on the same answer anywhere.
