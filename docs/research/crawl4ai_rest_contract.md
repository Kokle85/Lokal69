# Crawl4AI 0.9.4 self-hosted REST server: verified contract

Research date: 2026-10-06. The user's server (`http://127.0.0.1:11235`, reported as 0.9.4) could not be reached
from here, so this file describes the contract from the **v0.9.4 tag source**. Most of it was also confirmed by
**running that source**: the upstream `deploy/docker/server.py` app ran under FastAPI `TestClient` in a scratch venv
with `crawl4ai==0.9.4`, its `deploy/docker/requirements.txt` and Playwright headless Chromium. That run did real
crawls of `raw:` HTML through `/crawl` and `/crawl/stream`. Anything not confirmed this way is marked **UNVERIFIED**.
Check everything again at activation (see section 14).

| Source | How read | Version |
|---|---|---|
| `github.com/unclecode/crawl4ai` tag `v0.9.4` | `git clone --depth 1 --branch v0.9.4`, commit `133e1d92` (2026-09-23) | server: `deploy/docker/{server,api,schemas,auth,auth_gate,governor,crawler_pool,utils,job,egress_broker}.py`, `config.yml`, `MIGRATION.md`, `Dockerfile`, `docker-compose.yml`, `CHANGELOG.md` |
| PyPI `crawl4ai==0.9.4` | wheel installed, sdist unpacked (uploaded 2026-09-23) | the library package matches the tag byte-for-byte. The sdist/wheel does **not** contain the server (`deploy/docker` exists only in the repo and the image) |
| Docker Hub `unclecode/crawl4ai` | Hub API tag list | tags `0.9.4`, `0.9`, `0`, `latest` (all pushed 2026-09-23, amd64 + arm64) |
| docs.crawl4ai.com `/core/self-hosting/`, `/core/browser-crawler-config/`, `/core/cache-modes/` | WebFetch | the live site is labelled "v0.9.x" and still says **0.9.2** (see Drift, section 13) |
| Server runtime deps (from `requirements.txt`) | installed | fastapi 0.136.3, slowapi 0.1.9, PyJWT 2.10.1, `mcp>=1.18,<2` (1.30.0 resolved), playwright 1.63.0 |

---

## 0. Rules for our client (Lokal69)

1. **Always send `Authorization: Bearer <CRAWL4AI_API_TOKEN>`.** In 0.9.x every path except `GET /health`,
   `POST /token`, `/`, `/monitor` and the UI static prefixes (`/playground`, `/dashboard`, `/static`) returns
   **401** without a token. That includes `/openapi.json`, `/docs` and `/schema`. This was verified.
2. **Never send `magic`, `simulate_user` or `override_navigator`, not even as `false`.** Their mere presence gives
   **400** `Rejected config: field 'magic' is not permitted on CrawlerRunConfig from an untrusted request`
   (verified). They already default to `False`. The `config.yml` `base_config: {simulate_user: true}` is merged in
   only when the field is `None` or `""`, and the default is `False`, so it **never applies**. This was verified
   by emulating the merge.
3. `enable_stealth` (BrowserConfig) **is** allowed. Send `false` explicitly or leave it out (the default is
   `False`). Never set `user_agent_mode: "random"`.
4. **Do not send BrowserConfig `headers`, `cookies`, `proxy`/`proxy_config` or `extra_args`.** Each gives a 400.
   Note that `BrowserConfig(...).dump()` from the Python library **always** includes a computed `headers`
   (`sec-ch-ua`) entry, so a dumped BrowserConfig is **rejected**. Build the JSON by hand.
5. Send `cache_mode` as the typed enum `{"type": "CacheMode", "params": "bypass"}`. A bare string is stored
   as a `str` and never equals a `CacheMode` member (section 7).
6. Client timeouts: the server clamps `page_timeout`/`wait_for_timeout` to **≤ 60000 ms** and kills a request at
   **`limits.wall_clock_s` = 300 s** (HTTP 504). Use an HTTP client read timeout of about `page_timeout/1000 + 30` s
   for one URL, and never more than about 310 s.
7. The top-level `success: true` only means the request was handled. **Check `results[i].success`,
   `status_code` and `error_message` for each result**, and match results by `url`. Multi-URL results arrive in
   completion order, not input order.

---

## 1. Image, deployment and environment variables

- Image: **`unclecode/crawl4ai:0.9.4`**. The tag format is `LIBRARY_VERSION[-SUFFIX]`, and the floating tags are
  `0.9`, `0` and `latest`. The image is multi-arch (amd64, arm64). The Dockerfile uses `ARG C4AI_VER=0.9.4`, Python 3.12
  and a non-root `appuser`, and starts with `CMD ["bash","entrypoint.sh"]`, which runs supervisord (redis +
  gunicorn).
- gunicorn: `--workers 1 --threads 4 --timeout 1800 --keep-alive 300 --limit-request-line 8190
  --limit-request-fields 100 --worker-class uvicorn.workers.UvicornWorker server:app`.
- Port **11235**, from `CRAWL4AI_PORT` (default 11235). `HEALTHCHECK` runs `curl -f http://localhost:11235/health`.
- **Bind and auth posture** (`entrypoint.sh` together with `server._resolve_auth`):
  - `CRAWL4AI_API_TOKEN` set (or the `/run/secrets/api_token` file) → binds `[::]:11235` and enforces the token.
  - No token → binds the container's own **127.0.0.1**. A published `-p 11235:11235` port then gets
    *connection reset*, and the server logs a one-off ephemeral token. A host-reachable server on
    127.0.0.1:11235 therefore almost certainly **has a token configured**.
- Recommended run, from the docs and compose: `docker run -d -p 127.0.0.1:11235:11235 --shm-size=1g -e
  CRAWL4AI_API_TOKEN="$CRAWL4AI_API_TOKEN" unclecode/crawl4ai:0.9.4`. The `127.0.0.1:` host prefix is our own
  hardening.
- Other server environment variables:

| Variable | Effect | Default |
|---|---|---|
| `CRAWL4AI_API_TOKEN` | static operator bearer token (admin scope) | none |
| `SECRET_KEY` | HS256 JWT key, at least 32 chars, required when `jwt_enabled` | ephemeral |
| `CRAWL4AI_JWT_ENABLED` | entrypoint bind decision only | false |
| `CRAWL4AI_MAX_TIMEOUT_MS` | ceiling for request `page_timeout`/`wait_for_timeout`/`body_visibility_timeout` (new in 0.9.4) | 60000 |
| `CRAWL4AI_HOOKS_ENABLED` | declarative hooks (otherwise any `hooks` field gives 403) | false |
| `CRAWL4AI_EXECUTE_JS_ENABLED` | `/execute_js` (otherwise 403) | false |
| `CRAWL4AI_ALLOW_INTERNAL_URLS` | disables SSRF blocking of private/loopback targets | false |
| `CRAWL4AI_ALLOW_INSECURE_TLS` | disables TLS verification for crawl targets | false |

## 2. Authentication (`auth_gate.py`, `auth.py`)

- `AuthGateMiddleware` is the outermost ASGI layer and **fails closed**. It reads the token only from
  `Authorization: Bearer <tok>` (case-insensitive `bearer `). Any other scheme counts as no token. WebSocket
  connections may instead pass `?token=`.
- Accepted credentials:
  1. The static `CRAWL4AI_API_TOKEN`, compared in constant time. It gets principal `{"sub":"operator","scope":"admin"}`.
  2. An HS256 JWT minted by this server, which gets `scope: "data"`. JWTs last 60 minutes.
- Rejection: **401** with body `{"detail": "Authentication required"}` and header `WWW-Authenticate: Bearer`.
  WebSocket connections are closed with code 4401. Verified.
- `POST /token` takes `{"email": "...", "api_token": "..."}`. It works only when **`security.api_token` is set in
  `config.yml`**; the env variable does not count. Otherwise it returns **403** `Token issuance is disabled: no
  api_token is configured on the server.` (verified). It also checks that the email domain has MX records. The
  response is `{"email", "access_token", "token_type": "bearer"}`. **We do not need `/token`: send the static token
  as the Bearer directly.**
- `security.jwt_enabled` defaults to `false`. With `true`, `SECRET_KEY` is mandatory or startup fails.

## 3. Endpoint inventory (from `/openapi.json` of the running 0.9.4 app)

Paths in the spec: `/`, `/artifacts/{artifact_id}`, `/ask`, `/config/dump`, `/crawl`, `/crawl/job`,
`/crawl/job/{task_id}`, `/crawl/stream`, `/execute_js`, `/health`, `/hooks/info`, `/html`, `/llm/job`,
`/llm/job/{task_id}`, `/llm/{url}`, `/mcp/schema`, `/md`, `/metrics`, `/monitor/*` (health, requests, browsers,
endpoints/stats, timeline, logs/janitor, logs/errors, actions/*, stats/reset), `/pdf`, `/schema`, `/screenshot`,
`/token`. These are mounted but not in the spec: `/mcp/sse`, `/mcp/messages`, `/mcp/ws`, `/monitor/ws`,
`/playground`, `/dashboard`, `/docs`, `/redoc`.

| Endpoint | Auth | Body / params | Response (success) |
|---|---|---|---|
| `GET /health` | **public** | none | `{"status":"ok","timestamp":<float epoch>,"version":"0.9.4"}` (verified) |
| `POST /crawl` | Bearer | `CrawlRequestWithHooks` (section 4) | `{"success":true,"results":[...],"server_processing_time_s","server_memory_delta_mb","server_peak_memory_mb"}` |
| `POST /crawl/stream` | Bearer | same body | NDJSON stream (section 9) |
| `POST /md` | Bearer | `{"url", "f":"fit"\|"raw"\|"bm25"\|"llm", "q":null, "c":"0", "provider", "temperature"}` | `{"url","filter","query","cache","markdown":<str>,"success":true}`. Note that `c:"1"` gives `CacheMode.ENABLED` and anything else gives `WRITE_ONLY` |
| `POST /html` | Bearer | `{"url"}` | `{"html","url","success"}`. The `html` is **`preprocess_html_for_schema()` output** (truncated, max_size 100000), **not** the raw page |
| `POST /screenshot`, `/pdf` | Bearer | `{"url", ...}` | base64 data plus `artifact_id`/`url`. Not needed by us |
| `POST /execute_js` | Bearer | | 403 unless `CRAWL4AI_EXECUTE_JS_ENABLED=true` |
| `GET /schema` | Bearer | none | `{"browser": BrowserConfig().dump(), "crawler": CrawlerRunConfig().dump()}`: the default config dumps, **not** an API schema |
| `POST /config/dump` | Bearer | `{"type":"CrawlerRunConfig"\|"BrowserConfig","params":{...}}` | the normalized config after the untrusted gate. **Use this at activation to dry-run our payload** (verified: forbidden field gives 400, unknown field is dropped, timeout is clamped) |
| `GET /hooks/info` | Bearer | none | declarative hook actions (`block_resources`, `add_cookies`, `set_headers`, `scroll_to_bottom`, `wait_for_timeout`) |
| `POST /crawl/job`, `GET /crawl/job/{task_id}` | Bearer | `{"urls":[HttpUrl], "browser_config":{}, "crawler_config":{}, "webhook_config"?}` | 202 plus task id. Results live in Redis with a TTL of 3600 s. Bounded queue: 503 with `Retry-After: 5` when full |
| `GET /openapi.json`, `/docs` | Bearer | | FastAPI defaults. **Auth-gated**, verified (401 without the token) |
| `/mcp/sse`, `/mcp/ws`, `/mcp/schema` | Bearer or `?token=` | | the server's own MCP bridge (uses `mcp<2`). Not used by us |

## 4. `POST /crawl` request body (`schemas.py`)

```python
class CrawlRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=100)     # 422 if [] or >100
    browser_config: Optional[Dict] = Field(default_factory=dict)
    crawler_config: Optional[Dict] = Field(default_factory=dict)
    crawler_configs: Optional[List[Dict]] = None   # per-URL configs (with url_matcher) for arun_many
class CrawlRequestWithHooks(CrawlRequest):
    hooks: Optional[HookConfig] = None              # any value gives 403 unless CRAWL4AI_HOOKS_ENABLED
```

- URLs without a scheme are prefixed with `https://`. `raw:`/`raw://` inline HTML is accepted.
  Each URL is validated for SSRF: private, loopback or link-local targets give **400** `URL blocked (SSRF
  protection): URL blocked` (verified).
- **Two accepted config shapes**, both loaded with `Provenance.UNTRUSTED`:
  - The typed shape `{"type": "CrawlerRunConfig", "params": {...}}`, which is what the docs use.
  - A plain kwargs dict `{"page_timeout": 30000, ...}`, which passes through the same gate (`_enforce_untrusted`).
- Serialization grammar (`to_serializable_dict`/`from_serializable_dict`):
  - Objects are written as `{"type": "<ClassName>", "params": {...}}`.
  - Enums are written as `{"type": "CacheMode", "params": "bypass"}`.
  - Plain dicts are written as `{"type": "dict", "value": {...}}`. In 0.9.4, wrapping a typed object inside
    `dict` is rejected.
  - Dicts without `params`/`value` (for example JsonCss field specs) pass through as data.
- If `crawler_config.params.stream == true` on `/crawl`, the server silently switches to the NDJSON stream
  response. **Never set `stream` on `/crawl`.**
- With one URL the server calls `arun`. With several it calls `arun_many` with a `MemoryAdaptiveDispatcher` and
  `RateLimiter(base_delay=(1.0, 2.0))`, which adds per-domain delays and backs off on 429/503, up to 3 retries and
  60 s max delay.

## 5. Trust boundary (`crawl4ai/async_configs.py`, 0.9.4)

**Forbidden fields (presence gives 400, even when the value is falsy):**
- CrawlerRunConfig: `js_code`, `js_code_before_wait`, `c4a_script`, `deep_crawl_strategy`, `proxy_config`,
  `proxy_rotation_strategy`, `proxy_session_*`, `fallback_fetch_function`, `experimental`, `base_url`,
  **`simulate_user`, `override_navigator`, `magic`**, `process_in_browser`, `shared_data`, `session_id`.
- BrowserConfig: `proxy`, `proxy_config`, `extra_args`, `user_data_dir`, `channel`, `chrome_channel`, `cdp_url`,
  `debugging_port`, `host`, `storage_state`, `cookies`, **`headers`**, `init_scripts`, `browser_context_id`, `target_id`.
- Forbidden on every type: `image_save_dir`, `save_images_locally`, `downloads_path`, `output_path`, `save_path`,
  `file_path`, `local_path`, `code`, `command`, `hook`, `hooks` and the js/script fields above.

**Allowlisted fields (anything else is silently dropped):**
- BrowserConfig: `browser_type`, `headless`, `browser_mode`, `viewport_width/height`, `viewport`,
  `device_scale_factor`, `accept_downloads`, `java_script_enabled`, `text_mode`, `light_mode`, `enable_stealth`,
  `avoid_ads`, `avoid_css`, `user_agent`, `user_agent_mode`, `user_agent_generator_config`, `verbose`,
  `memory_saving_mode`, `max_pages_before_recycle`.
- CrawlerRunConfig, the fields relevant to us: `cache_mode`, `page_timeout`, `wait_until`, `wait_for`,
  `wait_for_timeout`, `delay_before_return_html`, `mean_delay`, `max_range`, `scan_full_page`, `scroll_delay`,
  `max_scroll_steps`, `remove_overlay_elements`, `remove_consent_popups`, `css_selector`, `target_elements`,
  `excluded_tags`, `excluded_selector`, `word_count_threshold`, `only_text`, `locale`, `timezone_id`,
  `check_robots_txt`, `user_agent`, `max_retries`, `exclude_external_links`, `exclude_external_images`,
  `extraction_strategy` (only `JsonCss`/`JsonXPath`/`JsonLxml`/`Regex`/`Cosine`), `markdown_generator`,
  `scraping_strategy`, `table_extraction`, `capture_network_requests`, `capture_console_messages`, `stream`,
  `url_matcher`, `match_mode`, `verbose`.

**Clamps:**
- `page_timeout`, `wait_for_timeout` and `body_visibility_timeout` are capped by `min(v, CRAWL4AI_MAX_TIMEOUT_MS)`.
  A value ≤0 or non-numeric becomes 60000. Verified: 120000 becomes 60000.
- `max_scroll_steps` ≤ 1000.
- viewport between 1 and 4000.

## 6. Response shape of `/crawl` (verified by a real crawl through the 0.9.4 server code)

Top level: `success`, `results`, `server_processing_time_s`, `server_memory_delta_mb`, `server_peak_memory_mb`,
plus `hooks` when hooks were used. Each `results[i]` is `CrawlResult.model_dump()` with exactly these keys:

```
cache_status, cached_at, cleaned_html, console_messages, crawl_stats, dispatch_result, downloaded_files,
error_message, extracted_content, fit_html, head_fingerprint, html, js_execution_result, links, markdown,
media, metadata, mhtml, network_requests, pdf, redirected_status_code, redirected_url, response_headers,
screenshot, session_id, ssl_certificate, status_code, success, tables, url
```

Observed values on success:
- `"success": true`, `"status_code": 200`, `"error_message": ""` (an empty string, not null).
- `"cache_status": "miss"`. Other values are `hit`, `hit_validated` and `hit_fallback`.
- `metadata`: `{"title","description","keywords","author", ...}`. Pages can add og/twitter keys.
- `markdown` is an **object**: `{raw_markdown, markdown_with_citations, references_markdown, fit_markdown,
  fit_html}`. `fit_*` is null unless a content filter is configured.
- `links`: `{"internal":[...], "external":[...]}`. Each link is `{href, text, title, base_domain, head_data,
  head_extraction_status, head_extraction_error, intrinsic_score, contextual_score, total_score}`.
- `media`: `{"images":[...], "videos":[...], "audios":[...]}`. Each image is `{src, data, alt, desc, score, type,
  group_id, format, width}`.
- `crawl_stats`: `{"attempts":1,"retries":0,"proxies_used":[{"proxy":null,"status_code":200,"blocked":false,
  "reason":""}],"fallback_fetch_used":false,"resolved_by":"direct"}`.
- `dispatch_result` is null for a single URL. For `arun_many` it is `{task_id, memory_usage, peak_memory,
  start_time, end_time, error_message}`.
- `response_headers` was `{}` and `redirected_url` was null for `raw:` input. For http(s) input the source sets
  `redirected_url = page.url` after `goto` (the final URL), falling back to the requested url. It sets
  `redirected_status_code = response.status` of the response that Playwright `goto` returns. Both are from
  source; live values are **UNVERIFIED**.
- `html` is the raw page HTML. `cleaned_html` is the sanitized HTML.

## 7. Cache modes (`crawl4ai/cache_context.py`)

The values are `CacheMode.ENABLED="enabled"`, `DISABLED="disabled"`, `READ_ONLY="read_only"`,
`WRITE_ONLY="write_only"` and `BYPASS="bypass"`.

- **Default in 0.9.4 is `CacheMode.BYPASS`.** An empty `crawler_config` loads as BYPASS (verified).
- `cache_mode=None` becomes ENABLED inside `arun`.
- Reads happen only for ENABLED/READ_ONLY, and writes only for ENABLED/WRITE_ONLY.
- **The docs' bare-string form `"cache_mode": "bypass"` stays a Python `str` (verified).** `CacheContext` then
  matches no enum member, so the result is no read and no write. That happens to equal bypass, but
  `"enabled"` as a string would also silently *not* cache. **Use the typed enum form.**
- The legacy booleans `bypass_cache`, `disable_cache`, `no_cache_read` and `no_cache_write` are allowlisted but
  deprecated.

## 8. Failure semantics and status codes

| Situation | Result |
|---|---|
| Missing or bad token | 401 `{"detail":"Authentication required"}` |
| Pydantic validation (for example `urls: []`) | 422 FastAPI detail list |
| Forbidden config field | 400 `{"detail":"Rejected config: ..."}`. BrowserConfig problems give `"Rejected request: ..."` |
| SSRF-blocked URL | 400 `URL blocked (SSRF protection): ...` |
| `hooks` present while hooks are disabled | 403 |
| Content-Length > `limits.max_body_bytes` (10 MiB) | 413 `{"detail": "Request body too large"}` (verified) |
| slowapi limit (`1000/minute` per client IP on `/crawl`, `/crawl/stream`, `/md`, `/html`, `/screenshot`, `/pdf`, `/execute_js`, `/ask`) | 429 `{"detail":"1000 per 1 minute"}` (source-derived) |
| Crawl longer than `limits.wall_clock_s` (300 s in the shipped `config.yml`) | 504 `Crawl exceeded the time limit` |
| Unhandled server error | 500 `{"error":"Internal server error","correlation_id":"<12 hex>"}`. No detail is leaked |
| `/md` target failure | 502 with `error_message` |
| Navigation timeout, DNS error or other per-URL error | HTTP 200 with `results[i].success=false` and `error_message` set |
| Anti-bot page detected | HTTP 200 with `success=false` and `error_message="Blocked by anti-bot protection: <reason>"`. **Treat as blocked and do not retry with evasion** |
| `check_robots_txt=true` and disallowed | `success=false`, `status_code=403`, `error_message="Access denied by robots.txt"`, `response_headers={"X-Robots-Status":"Blocked by robots.txt"}` |
| Target returns 404/410 | probably `success=true` with `status_code` 404. `is_blocked` needs corroborating content signals. **UNVERIFIED live: always check `status_code`** |

## 9. `POST /crawl/stream` (verified)

- Response type `application/x-ndjson`, with headers `X-Stream-Status: active` and `Cache-Control: no-cache`.
- Each line is one `CrawlResult` dict (the same keys as section 6) plus `server_memory_mb`.
- The stream ends with a line `{"status": "completed"}` and no trailing newline.
- A per-result serialization error gives a line `{"error": "...", "url": "..."}`.
- The stream always uses the dispatcher `RateLimiter`. Rate-limit and auth errors happen before streaming starts.

## 10. Timeouts and waiting

- `page_timeout` is in ms (library default `PAGE_TIMEOUT = 60000`). It is the timeout for `goto` and is clamped
  as in section 5.
- `wait_until` defaults to `"domcontentloaded"`. Other values are Playwright's `load`, `networkidle` and `commit`.
- `wait_for` (default None) takes `"css:<selector>"` or `"js:<expr returning bool>"`. A bare string is
  auto-detected.
- `wait_for_timeout` (default None) falls back to `page_timeout`.
- `delay_before_return_html` defaults to 0.1 s. In `arun_many`, `mean_delay` defaults to 0.1 and `max_range` to 0.3.
- Server-side ceilings: `limits.wall_clock_s` (300 s, gives 504), gunicorn `--timeout 1800`, and
  `crawler.timeouts.batch_process` (300). `batch_process` is only defined in the default config; no server code
  reads it in 0.9.4.
- There is **no per-request override of `wall_clock_s`**. Raising the 60 s cap needs the server env variable
  `CRAWL4AI_MAX_TIMEOUT_MS`.

## 11. User-Agent, stealth and robots

- BrowserConfig default UA: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36`.
  `user_agent_mode` defaults to `""`, and `enable_stealth` to `False`.
- Set our own UA in **BrowserConfig `user_agent`**. A CrawlerRunConfig `user_agent` *mutates the pooled browser's*
  `browser_config.user_agent` (sticky across later requests on that browser) and re-syncs `sec-ch-ua`.
- `magic=True` or `user_agent_mode="random"` would randomize the UA. Both stay off: `magic` is forbidden anyway.
- Custom request headers (for example `Accept-Language`) **cannot** be sent: `headers` is forbidden, and the
  `set_headers` hook is disabled by default. Use `locale` (for example `"de-DE"`) and `timezone_id` instead.
  Both are allowed.
- `check_robots_txt` (default False) evaluates robots.txt against `browser_config.user_agent`. In 0.9.4 the robots
  fetch goes through the server's pinning egress proxy (fix for GHSA-f77g-77vp-r96v).
- Pool: browsers are pooled by a SHA1 of `BrowserConfig.to_dict()`. A config is promoted to the hot pool after 3
  uses, and a browser context is recycled after 200 pages. Server-wide concurrency is capped by
  `crawler.pool.max_pages: 40` (a global semaphore around `arun`).

## 12. Recommended payload (validated against the 0.9.4 server code under TestClient)

```python
import httpx

def crawl_payload(urls: list[str], ua: str, page_timeout_ms: int = 30000, wait_for: str | None = None) -> dict:
    params = {
        "cache_mode": {"type": "CacheMode", "params": "bypass"},
        "page_timeout": min(page_timeout_ms, 60000),
        "wait_until": "domcontentloaded",
        "check_robots_txt": True,
    }
    if wait_for:                       # e.g. "css:.listing-card"
        params["wait_for"] = wait_for
    return {
        "urls": urls,                  # 1..100
        "browser_config": {"type": "BrowserConfig",
                           "params": {"headless": True, "enable_stealth": False, "user_agent": ua}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": params},
        # NEVER include: magic / simulate_user / override_navigator / headers / proxy / js_code / stream
    }

async def crawl_one(base: str, token: str, url: str, ua: str) -> dict:
    timeout = httpx.Timeout(connect=5.0, read=30 + 30, write=10.0, pool=5.0)
    async with httpx.AsyncClient(base_url=base, timeout=timeout,
                                 headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("/crawl", json=crawl_payload([url], ua))
        r.raise_for_status()                      # 400/401/403/413/422/429/5xx
        body = r.json()
        res = next(x for x in body["results"] if x["url"] == url)   # match by url
        return res   # check res["success"], res["status_code"], res["error_message"]
```

The payload dict above was confirmed by running it through the server code. The httpx wrapper is
illustrative.

## 13. Drift between the docs (live site, 0.9.2) and the 0.9.4 tag source

| Topic | Docs say | 0.9.4 source does |
|---|---|---|
| Version shown | 0.9.2, `unclecode/crawl4ai:0.9.2` | 0.9.4 (Docker Hub tag exists) |
| `/health` body | `{"status":"healthy","version":"0.9.2"}` | `{"status":"ok","timestamp":<float>,"version":"0.9.4"}` |
| REST examples | send no auth header (commented "If JWT is enabled") | every non-health call needs a Bearer token (0.9.0+) |
| `/schema` | "Full API schema", listed as if public | default config dumps, auth required |
| `cache_mode` example | `"cache_mode": "bypass"` (bare string) | works only by accident; use the typed enum |
| `config.yml` sample in docs | `host 0.0.0.0`, `port 8020`, `security.enabled false` (labelled as 0.8.x) | `host 127.0.0.1`, `port 11235`, auth on, `limits:` block |
| `limits.wall_clock_s` | MIGRATION.md table says default `0` | shipped `config.yml` has **300** (since 0.9.3). `governor.py` falls back to 0 only when the key is absent |
| Default `cache_mode` | the cache-modes page states none; the config page says BYPASS | BYPASS, which agrees with the config page |
| `GET /job/{task_id}` | in both the live docs and the tag's `self-hosting.md` (line 753) | no such route. The actual paths are `/crawl/job/{task_id}` and `/llm/job/{task_id}` |
| `PruningContentFilterLXML` | absent | new in 0.9.4, the default for `/md` `f=fit` |
| `CRAWL4AI_MAX_TIMEOUT_MS` | absent from the self-hosting page | new in 0.9.4 |

## 14. Activation checklist (run against the real server)

1. `curl -s http://127.0.0.1:11235/health` should give `status == "ok"` and `version == "0.9.4"`. If `status` is
   `"healthy"`, the server is an older build.
2. `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:11235/crawl -H 'content-type: application/json' -d '{"urls":["https://example.com"]}'`
   should give **401**, which proves auth is on.
3. `POST /config/dump` with our exact CrawlerRunConfig and BrowserConfig payloads and the Bearer token should give
   200, with the echoed config showing no forbidden fields.
4. `POST /crawl` for one public listing page should give `results[0].success`, `status_code` 200 and a non-empty
   `html`/`markdown.raw_markdown`. Also check whether `redirected_url` and `response_headers` are populated for http(s).
5. Note the server's `limits.wall_clock_s` and whether `CRAWL4AI_MAX_TIMEOUT_MS` was changed, by asking the
   operator. Neither can be read through the API.

## 15. UNVERIFIED

- Live http(s) crawl fields: `response_headers` contents, `redirected_url`/`redirected_status_code` on a real
  redirect, and the `success`/`status_code` combination for a 404 page. Only `raw:` input was crawled here,
  because the sandbox has no direct egress for Chromium.
- The exact 429 body wording. It comes from slowapi source (`detail=str(limit.limit)`) and was not triggered.
- Whether the user's server runs the stock `config.yml`: a custom mount could change `wall_clock_s`, the rate
  limit, `api_token` or `jwt_enabled`.
- `dispatch_result` JSON serialization for multi-URL `/crawl` (datetime vs float `start_time`). This path was not
  exercised.
