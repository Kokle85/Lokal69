"""Crawl4AI 0.9.4 self-hosted REST client (spec sections 4, 8, 24, 27).

The verified contract is docs/research/crawl4ai_rest_contract.md; this module follows it:

- Every non-health call carries `Authorization: Bearer <token>` when a token is configured.
  The token is never logged, never part of `repr()` and never sent to `/health`.
- The `/crawl` payload is built by hand (research section 12): typed BrowserConfig and
  CrawlerRunConfig, headless, `enable_stealth: false`, an honest configurable
  User-Agent, typed `CacheMode` bypass (discovery always sees fresh results),
  `page_timeout` <= 60000 ms, `wait_until: domcontentloaded`, `check_robots_txt: true`
  and optional per-source `locale`/`timezone_id`/`wait_for` (CSS only). It never contains
  a field from the server's forbidden list (research section 5) nor `stream`; this is
  checked on every request.
- Results are matched by URL, and each result's own `success`/`status_code`/`error_message`
  is classified. Only application-owned `RawDocument`/`FetchOutcome` leave this module.
- Deadlines: HTTP read timeout `page_timeout/1000 + 30 s` (plus the wait_for budget when a
  source sets one; <= 310 s) and an overall `anyio.fail_after` deadline (<= 330 s, inside
  the rate limiter's per-host navigation lease). Cancellation (job cancelled, lease lost)
  propagates and closes the HTTP request; the crawler's own 300 s wall clock bounds
  server-side work. Redirects from the crawler endpoint are never followed.
- A CSS `wait_for` that never matched is UNEXPECTED_CONTENT (drift or app shell), not a
  transient navigation failure, so it is neither retried nor counted against the host.
- Size caps: the whole HTTP response is streamed with a byte cap, and the page HTML is
  capped at `max_response_bytes` (oversize bodies are dropped, never kept).

Crawler-side failures that say nothing about the target host use `error_code` values
starting with `crawler_`; the rate limiter does not count them against the host.
Credential and request-shape failures are raised as typed errors instead of being
reported as source access problems: `CrawlerAuthFailed` (401: OUR credential to the
crawler, not a source block) and `CrawlerProtocolError` (413/422/other 4xx: a client bug).

`health()` and `inspect_contract()` are read-only: they never restart, reconfigure or
upgrade the crawler, and a version mismatch is reported, not "fixed".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, get_args
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from suv_deals.adapters.base import FetchOutcome, FetchPurpose, RawDocument
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.crawling.rate_limits import parse_retry_after
from suv_deals.domain.enums import AccessState
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode
from suv_deals.netguard import UnsafeDestination, normalize_hostname, parse_safe_url
from suv_deals.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT: Final = "SUVDealResearch/0.1 (+private research; contact owner)"
EXPECTED_CRAWLER_VERSION: Final = "0.9.4"
MAX_PAGE_TIMEOUT_MS: Final = 60_000  # server clamp (CRAWL4AI_MAX_TIMEOUT_MS default)
MIN_PAGE_TIMEOUT_MS: Final = 1_000
SERVER_WALL_CLOCK_S: Final = 300
READ_TIMEOUT_MARGIN_S: Final = 30.0
MAX_READ_TIMEOUT_S: Final = 310.0
# Upper bound for the overall per-request deadline. It must stay within the rate limiter's
# per-host navigation lease (BackoffPolicy.in_flight_lease_seconds, 330 s) so a second
# navigation can never start while this one may still be running.
MAX_OVERALL_DEADLINE_S: Final = 330.0
MAX_RESPONSE_BYTES_LIMIT: Final = 50_000_000
DEFAULT_MIN_HTML_BYTES: Final = 200
_HEALTH_BODY_LIMIT: Final = 64 * 1024
_ERROR_BODY_LIMIT: Final = 64 * 1024
_MAX_RECORDED_RETRY_AFTER: Final = 30 * 86_400
_PURPOSES: Final = frozenset(get_args(FetchPurpose))

RESPONSE_HEADER_ALLOWLIST: Final = frozenset(
    {"content-type", "last-modified", "etag", "retry-after", "x-robots-tag", "x-robots-status"}
)

# Research section 5: presence of any of these gives HTTP 400, even with a falsy value.
FORBIDDEN_CRAWLER_RUN_FIELDS: Final = frozenset(
    {
        "js_code",
        "js_code_before_wait",
        "c4a_script",
        "deep_crawl_strategy",
        "proxy_config",
        "proxy_rotation_strategy",
        "fallback_fetch_function",
        "experimental",
        "base_url",
        "simulate_user",
        "override_navigator",
        "magic",
        "process_in_browser",
        "shared_data",
        "session_id",
    }
)
FORBIDDEN_BROWSER_FIELDS: Final = frozenset(
    {
        "proxy",
        "proxy_config",
        "extra_args",
        "user_data_dir",
        "channel",
        "chrome_channel",
        "cdp_url",
        "debugging_port",
        "host",
        "storage_state",
        "cookies",
        "headers",
        "init_scripts",
        "browser_context_id",
        "target_id",
    }
)
FORBIDDEN_ANY_TYPE_FIELDS: Final = frozenset(
    {
        "image_save_dir",
        "save_images_locally",
        "downloads_path",
        "output_path",
        "save_path",
        "file_path",
        "local_path",
        "code",
        "command",
        "hook",
        "hooks",
    }
)
# Not rejected by the server but never sent by us: `stream` silently switches /crawl to
# NDJSON, `user_agent_mode` could randomise the UA, `webhook_config`/`crawler_configs`
# are outside our minimal request.
OWN_FORBIDDEN_FIELDS: Final = frozenset({"stream", "user_agent_mode", "webhook_config", "crawler_configs"})
FORBIDDEN_FIELDS: Final = (
    FORBIDDEN_CRAWLER_RUN_FIELDS | FORBIDDEN_BROWSER_FIELDS | FORBIDDEN_ANY_TYPE_FIELDS | OWN_FORBIDDEN_FIELDS
)
FORBIDDEN_FIELD_PREFIXES: Final = ("proxy_session_",)
# BrowserConfig dumps always contain a computed `headers` (sec-ch-ua) entry (research rule 4).
SERVER_COMPUTED_BROWSER_FIELDS: Final = frozenset({"headers"})

_LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_WAIT_FOR = re.compile(r"^css:[^\x00-\x1f\x7f]{1,200}$")
_UA_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")
_VERSION = re.compile(r"^[0-9A-Za-z.+_-]{1,40}$")
_NET_ERROR = re.compile(r"net::ERR_[A-Z_]{2,60}")
_SAFE_TEXT = re.compile(r"[^\x20-\x7e]+")


class FetchErrorCode(StrEnum):
    """`FetchOutcome.error_code` values produced by this client."""

    # Our crawler infrastructure (never counted against the target host).
    CRAWLER_UNREACHABLE = "crawler_unreachable"
    CRAWLER_SERVER_ERROR = "crawler_server_error"
    CRAWLER_RATE_LIMITED = "crawler_rate_limited"
    CRAWLER_REJECTED_CONFIG = "crawler_rejected_config"
    CRAWLER_URL_BLOCKED = "crawler_url_blocked"
    CRAWLER_BAD_RESPONSE = "crawler_bad_response"
    CRAWLER_RESULT_MISSING = "crawler_result_missing"
    CRAWLER_REPORTED_FAILURE = "crawler_reported_failure"
    # Target fetch.
    URL_REJECTED = "url_rejected"
    FETCH_TIMEOUT = "fetch_timeout"
    CRAWL_TIME_LIMIT = "crawl_time_limit"
    ANTI_BOT_BLOCK = "anti_bot_block"
    ROBOTS_DISALLOWED = "robots_disallowed"
    NAVIGATION_FAILED = "navigation_failed"
    WAIT_CONDITION_FAILED = "wait_condition_failed"
    HTTP_ACCESS_DENIED = "http_access_denied"
    HTTP_NOT_FOUND = "http_not_found"
    HTTP_GONE = "http_gone"
    HTTP_RATE_LIMITED = "http_rate_limited"
    HTTP_SERVER_ERROR = "http_server_error"
    HTTP_STATUS_UNEXPECTED = "http_status_unexpected"
    MISSING_STATUS = "missing_status"
    EMPTY_BODY = "empty_body"
    RESPONSE_TOO_LARGE = "response_too_large"
    FINAL_URL_HOST_MISMATCH = "final_url_host_mismatch"
    FINAL_URL_INVALID = "final_url_invalid"
    CACHE_HIT_NOT_FRESH = "cache_hit_not_fresh"


class CrawlerAuthFailed(DependencyUnavailable):
    """The crawler rejected OUR credential (HTTP 401). Not a source access block."""

    kind: Final = "crawler_auth_failed"

    def __init__(self, *, token_configured: bool) -> None:
        hint = "check CRAWL4AI_API_TOKEN" if token_configured else "no CRAWL4AI_API_TOKEN is configured"
        super().__init__(f"Crawler rejected our credential (crawler_auth_failed); {hint}")
        self.retryable = False
        self.details = {"kind": self.kind}


class CrawlerProtocolError(AppError):
    """The crawler rejected the shape of our request (client bug or misconfigured base URL)."""

    def __init__(self, http_status: int, kind: str = "crawler_client_error") -> None:
        super().__init__(
            ErrorCode.INTERNAL_ERROR,
            f"Crawler rejected the request ({kind}, HTTP {http_status})",
            retryable=False,
            details={"kind": kind, "http_status": http_status},
        )
        self.http_status = http_status
        self.kind = kind


class SourceCrawlOptions(BaseModel):
    """Optional per-source run settings. `wait_for` accepts CSS only: JavaScript is never sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    locale: str | None = None
    timezone_id: str | None = None
    wait_for: str | None = None

    @field_validator("locale")
    @classmethod
    def _locale(cls, value: str | None) -> str | None:
        if value is not None and not _LOCALE.match(value):
            raise ValueError("locale must look like 'de-DE'")
        return value

    @field_validator("timezone_id")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown IANA timezone") from exc
        return value

    @field_validator("wait_for")
    @classmethod
    def _wait_for(cls, value: str | None) -> str | None:
        if value is not None and not _WAIT_FOR.match(value):
            raise ValueError("wait_for must be 'css:<selector>' (JavaScript conditions are not allowed)")
        return value


class CrawlerHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool
    status: str | None = None
    version: str | None = None
    checked_at: datetime
    http_status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.status == "ok"


class ConfigDumpCheck(BaseModel):
    """Result of dry-running one of our configs through POST /config/dump."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_type: Literal["CrawlerRunConfig", "BrowserConfig"]
    http_status: int | None
    accepted: bool
    echoed_forbidden_fields: tuple[str, ...] = ()
    server_computed_fields: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    detail: str | None = Field(default=None, max_length=300)


class ContractReport(BaseModel):
    """Read-only runtime contract inspection (spec sections 8 and 27, research section 14)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    base_url: str
    health: CrawlerHealth
    expected_version: str | None
    version_matches: bool | None
    token_configured: bool
    auth_probe_status: int | None
    auth_enforced: bool | None
    payload_forbidden_fields: tuple[str, ...]
    crawler_config: ConfigDumpCheck | None
    browser_config: ConfigDumpCheck | None
    # Dry-run of each source's own CrawlerRunConfig (locale/timezone_id/wait_for), by source key.
    source_crawler_configs: dict[str, ConfigDumpCheck] = Field(default_factory=dict)
    problems: tuple[str, ...]
    read_only: Literal[True] = True

    @property
    def ok(self) -> bool:
        return not self.problems


class CrawlFetchResult(BaseModel):
    """`RawDocument` plus crawler telemetry that the shared `FetchOutcome` has no field for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document: RawDocument
    server_processing_time_s: float | None = None
    cache_status: str | None = None
    redirected_status_code: int | None = None


# ---------------------------------------------------------------------------
# Payload construction


def find_forbidden_fields(value: object) -> tuple[str, ...]:
    """Every forbidden key anywhere in a JSON-like structure (sorted, unique)."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if isinstance(key, str) and (
                    key in FORBIDDEN_FIELDS or key.startswith(FORBIDDEN_FIELD_PREFIXES)
                ):
                    found.add(key)
                walk(child)
        elif isinstance(node, list | tuple):
            for child in node:
                walk(child)

    walk(value)
    return tuple(sorted(found))


def validate_user_agent(user_agent: str) -> str:
    ua = user_agent.strip()
    if not ua or len(ua) > 256 or _UA_FORBIDDEN.search(ua) or not ua.isascii():
        raise ValueError("user_agent must be 1-256 printable ASCII characters")
    if ua.lower().startswith("mozilla/"):
        raise ValueError("user_agent must identify this crawler honestly, not impersonate a browser")
    return ua


def build_browser_config(user_agent: str) -> dict[str, Any]:
    return {
        "type": "BrowserConfig",
        "params": {"headless": True, "enable_stealth": False, "user_agent": validate_user_agent(user_agent)},
    }


def build_crawler_config(page_timeout_ms: int, options: SourceCrawlOptions | None = None) -> dict[str, Any]:
    if not MIN_PAGE_TIMEOUT_MS <= page_timeout_ms <= MAX_PAGE_TIMEOUT_MS:
        raise ValueError(f"page_timeout_ms must be between {MIN_PAGE_TIMEOUT_MS} and {MAX_PAGE_TIMEOUT_MS}")
    params: dict[str, Any] = {
        "cache_mode": {"type": "CacheMode", "params": "bypass"},
        "page_timeout": page_timeout_ms,
        "wait_until": "domcontentloaded",
        "check_robots_txt": True,
    }
    if options is not None:
        if options.locale:
            params["locale"] = options.locale
        if options.timezone_id:
            params["timezone_id"] = options.timezone_id
        if options.wait_for:
            params["wait_for"] = options.wait_for
    return {"type": "CrawlerRunConfig", "params": params}


def build_crawl_payload(
    url: str,
    *,
    user_agent: str,
    page_timeout_ms: int,
    options: SourceCrawlOptions | None = None,
) -> dict[str, Any]:
    payload = {
        "urls": [url],
        "browser_config": build_browser_config(user_agent),
        "crawler_config": build_crawler_config(page_timeout_ms, options),
    }
    forbidden = find_forbidden_fields(payload)
    if forbidden:  # invariant: unreachable unless this module is edited incorrectly
        raise CrawlerProtocolError(0, "forbidden_field_in_payload")
    return payload


def expected_version_from_image(image: str) -> str | None:
    """'unclecode/crawl4ai:0.9.4[@sha256:...]' -> '0.9.4'; floating tags give None."""
    reference = image.split("@", 1)[0]
    last = reference.rsplit("/", 1)[-1]
    if ":" not in last:
        return None
    tag = last.rsplit(":", 1)[1]
    if tag in {"latest", "0", "0.9"} or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", tag):
        return None
    return tag.split("-", 1)[0]


# ---------------------------------------------------------------------------
# Small helpers


def _url_key(url: str) -> str | None:
    """Comparable form: lowercase scheme/host, default port dropped, empty path '/', no fragment."""
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        if not parts.scheme or not hostname:
            return None
        scheme = parts.scheme.lower()
        host = normalize_hostname(hostname)
        port = parts.port
    except (ValueError, UnsafeDestination):
        return None
    netloc = host if port is None or port == {"https": 443, "http": 80}.get(scheme) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        return normalize_hostname(parts.hostname)
    except (ValueError, UnsafeDestination):
        return None


def _safe_text(value: object, limit: int = 200) -> str:
    text = _SAFE_TEXT.sub(" ", str(value)).strip()
    return text[:limit]


def _allowlisted_headers(raw: object) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return headers
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        name = key.strip().lower()
        if name in RESPONSE_HEADER_ALLOWLIST and isinstance(value, str | int | float):
            headers[name] = _safe_text(value, 500)
    return headers


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _bounded_retry_after(value: str | None, now: datetime) -> int | None:
    seconds = parse_retry_after(value, now)
    return None if seconds is None else min(seconds, _MAX_RECORDED_RETRY_AFTER)


def _markdown_text(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        text = raw.get("raw_markdown")
        return text if isinstance(text, str) else None
    return None


def _status_classification(status: int) -> tuple[AccessState, FetchErrorCode] | None:
    if status in (401, 403, 407, 451):
        return AccessState.ACCESS_BLOCKED, FetchErrorCode.HTTP_ACCESS_DENIED
    if status == 404:
        return AccessState.NOT_FOUND, FetchErrorCode.HTTP_NOT_FOUND
    if status == 410:
        return AccessState.REMOVED, FetchErrorCode.HTTP_GONE
    if status == 429:
        return AccessState.RATE_LIMITED, FetchErrorCode.HTTP_RATE_LIMITED
    if 500 <= status <= 599:
        return AccessState.TRANSIENT_ERROR, FetchErrorCode.HTTP_SERVER_ERROR
    return None


class _RawResponse:
    __slots__ = ("body", "headers", "read_bytes", "status", "too_large")

    def __init__(
        self, status: int, headers: httpx.Headers, body: bytes | None, *, too_large: bool, read_bytes: int
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.too_large = too_large
        self.read_bytes = read_bytes

    def json(self) -> object:
        if self.body is None:
            return None
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None


# ---------------------------------------------------------------------------
# Client


class Crawl4AIClient:
    """`CrawlClient` implementation for the Crawl4AI 0.9.4 REST server."""

    def __init__(
        self,
        base_url: str,
        api_token: SecretStr | None,
        user_agent: str = DEFAULT_USER_AGENT,
        *,
        http: httpx.AsyncClient | None = None,
        page_timeout_ms: int = 30_000,
        max_response_bytes: int = 8_000_000,
        crawler_version_hint: str | None = None,
        expected_version: str | None = EXPECTED_CRAWLER_VERSION,
        source_options: Mapping[str, SourceCrawlOptions] | None = None,
        clock: Clock | None = None,
        min_html_bytes: int = DEFAULT_MIN_HTML_BYTES,
        max_envelope_bytes: int | None = None,
        overall_deadline_s: float | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self._base_url = self._validate_base_url(base_url)
        if api_token is not None and not api_token.get_secret_value():
            api_token = None
        self._token = api_token
        self._user_agent = validate_user_agent(user_agent)
        build_crawler_config(page_timeout_ms)  # validates the range
        self._page_timeout_ms = page_timeout_ms
        if not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES_LIMIT:
            raise ValueError(f"max_response_bytes must be between 1 and {MAX_RESPONSE_BYTES_LIMIT}")
        self._max_response_bytes = max_response_bytes
        # The JSON envelope repeats the page (html, cleaned_html, markdown variants, links, media).
        self._max_envelope_bytes = (
            max_envelope_bytes if max_envelope_bytes is not None else 6 * max_response_bytes + 2 * 1024 * 1024
        )
        if self._max_envelope_bytes < 1:
            raise ValueError("max_envelope_bytes must be positive")
        if min_html_bytes < 0:
            raise ValueError("min_html_bytes must be >= 0")
        self._min_html_bytes = min_html_bytes
        self._version_hint = crawler_version_hint
        self._expected_version = expected_version
        self._observed_version: str | None = None
        self._source_options = dict(source_options or {})
        self._clock: Clock = clock or SystemClock()
        if not 0 < connect_timeout_s <= 60:
            raise ValueError("connect_timeout_s must be in (0, 60]")
        if overall_deadline_s is not None and not 0 < overall_deadline_s <= MAX_OVERALL_DEADLINE_S:
            raise ValueError(f"overall_deadline_s must be in (0, {MAX_OVERALL_DEADLINE_S}]")
        self._connect_timeout_s = connect_timeout_s
        self._deadline_override = overall_deadline_s
        self._timeout, self._deadline_s = self._timing(None)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(follow_redirects=False, trust_env=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        http: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> Crawl4AIClient:
        return cls(
            settings.crawl4ai_base_url,
            settings.crawl4ai_api_token,
            user_agent,
            http=http,
            expected_version=expected_version_from_image(settings.crawl4ai_image),
            **kwargs,
        )

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        # Infrastructure exception (spec section 24): the trusted crawler endpoint is configured,
        # not user supplied, and may be a private/loopback address. It is NOT checked by the
        # public-target URL policy, but it must be a plain http(s) origin without credentials.
        try:
            parts = urlsplit(base_url.strip())
            _ = parts.port
        except ValueError as exc:
            raise ValueError("invalid crawler base URL") from exc
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("crawler base URL must be http(s)://host[:port]")
        if "@" in parts.netloc or parts.query or parts.fragment:
            raise ValueError("crawler base URL must not contain credentials, query or fragment")
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    def __repr__(self) -> str:
        token = "set" if self._token is not None else "missing"
        return f"Crawl4AIClient(base_url={self._base_url!r}, token={token})"

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def crawler_version(self) -> str | None:
        return self._observed_version or self._version_hint

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Crawl4AIClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- HTTP plumbing -----------------------------------------------------

    def _timing(self, options: SourceCrawlOptions | None) -> tuple[httpx.Timeout, float]:
        """HTTP timeouts and overall deadline for one /crawl request.

        Read timeout = page_timeout/1000 + 30 s (research rule 6). With a `wait_for`
        condition, Crawl4AI's wait_for_timeout defaults to page_timeout, so that wait is
        budgeted too. Never above 310 s (the server's 300 s wall clock plus margin).
        """
        page_s = self._page_timeout_ms / 1000
        crawl_s = page_s * 2 if options is not None and options.wait_for else page_s
        read = min(crawl_s + READ_TIMEOUT_MARGIN_S, MAX_READ_TIMEOUT_S)
        timeout = httpx.Timeout(connect=self._connect_timeout_s, read=read, write=10.0, pool=5.0)
        deadline = min(read + self._connect_timeout_s + 10.0, MAX_OVERALL_DEADLINE_S)
        return timeout, self._deadline_override if self._deadline_override is not None else deadline

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        return headers

    async def _send_bounded(self, request: httpx.Request, limit: int) -> _RawResponse:
        # Never follow a redirect from the crawler endpoint, even with an injected client
        # configured to: the request body names the target and must not go elsewhere.
        response = await self._http.send(request, stream=True, follow_redirects=False)
        try:
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                return _RawResponse(
                    response.status_code, response.headers, None, too_large=True, read_bytes=int(declared)
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    return _RawResponse(
                        response.status_code, response.headers, None, too_large=True, read_bytes=total
                    )
                chunks.append(chunk)
            return _RawResponse(
                response.status_code, response.headers, b"".join(chunks), too_large=False, read_bytes=total
            )
        finally:
            # Release the connection even when the job is being cancelled (lease lost).
            with anyio.CancelScope(shield=True), anyio.move_on_after(5.0):
                await response.aclose()

    # -- Fetch --------------------------------------------------------------

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        return (await self.fetch_detailed(url, purpose=purpose, source_key=source_key)).document

    async def fetch_detailed(self, url: str, *, purpose: FetchPurpose, source_key: str) -> CrawlFetchResult:
        fetched_at = ensure_utc(self._clock.now())
        if purpose not in _PURPOSES:
            raise ValueError("unknown fetch purpose")
        rejection = self._reject_target(url)
        if rejection is not None:
            outcome = self._outcome(
                url,
                fetched_at,
                AccessState.POLICY_DENIED,
                FetchErrorCode.URL_REJECTED,
                f"client refused the target URL: {rejection}",
            )
            return CrawlFetchResult(document=self._document(url, outcome))

        options = self._source_options.get(source_key)
        payload = build_crawl_payload(
            url,
            user_agent=self._user_agent,
            page_timeout_ms=self._page_timeout_ms,
            options=options,
        )
        timeout, deadline_s = self._timing(options)
        request = self._http.build_request(
            "POST",
            f"{self._base_url}/crawl",
            json=payload,
            headers=self._auth_headers(),
            timeout=timeout,
        )
        started = time.monotonic()
        error: tuple[AccessState, FetchErrorCode, str] | None = None
        raw: _RawResponse | None = None
        try:
            with anyio.fail_after(deadline_s):
                raw = await self._send_bounded(request, self._max_envelope_bytes)
        except TimeoutError:
            error = (
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.FETCH_TIMEOUT,
                "overall crawl deadline exceeded",
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            error = (
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_UNREACHABLE,
                "crawler connection failed",
            )
        except httpx.TimeoutException:
            error = (AccessState.TRANSIENT_ERROR, FetchErrorCode.FETCH_TIMEOUT, "crawler response timed out")
        except httpx.TransportError:
            error = (
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_UNREACHABLE,
                "crawler connection broke",
            )
        except httpx.HTTPError:
            error = (
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_BAD_RESPONSE,
                "crawler response unreadable",
            )
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))

        if error is not None or raw is None:
            state, code, message = error or (
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_BAD_RESPONSE,
                "no response",
            )
            logger.warning(
                "crawl4ai request failed", extra={"error_code": code.value, "source_key": source_key}
            )
            outcome = self._outcome(url, fetched_at, state, code, message, elapsed_ms=elapsed_ms)
            return CrawlFetchResult(document=self._document(url, outcome))
        return self._map_server_response(url, raw, fetched_at, elapsed_ms)

    @staticmethod
    def _reject_target(url: str) -> str | None:
        """Defence in depth below the policy layer: never forward raw:, javascript:, private targets."""
        if not isinstance(url, str):
            return "not a string"
        try:
            parse_safe_url(url, allowed_schemes=("https", "http"), allowed_ports=(443, 80))
        except UnsafeDestination as exc:
            return _safe_text(exc, 100)
        return None

    def _outcome(
        self,
        url: str,
        fetched_at: datetime,
        state: AccessState,
        code: FetchErrorCode | None,
        message: str | None,
        *,
        final_url: str | None = None,
        http_status: int | None = None,
        elapsed_ms: int | None = None,
        size: int = 0,
        redirect_count: int = 0,
        retry_after: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchOutcome:
        return FetchOutcome(
            requested_url=url[:2048],
            final_url=final_url[:2048] if final_url else None,
            http_status=http_status,
            success=state == AccessState.OK,
            access_state=state,
            error_code=code.value if code else None,
            error_message=message[:500] if message else None,
            elapsed_ms=elapsed_ms,
            bytes=size,
            redirect_count=redirect_count,
            retry_after_seconds=retry_after,
            response_headers=headers or {},
            crawler_version=self.crawler_version,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _document(
        url: str,
        outcome: FetchOutcome,
        *,
        html: str | None = None,
        text: str | None = None,
        content_type: str | None = None,
    ) -> RawDocument:
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest() if html is not None else None
        return RawDocument(
            url=url[:2048],
            final_url=outcome.final_url,
            fetched_at=outcome.fetched_at,
            content_type=content_type,
            html=html,
            text=text,
            raw_content_hash=content_hash,
            fetch=outcome,
        )

    def _map_server_response(
        self, url: str, raw: _RawResponse, fetched_at: datetime, elapsed_ms: int
    ) -> CrawlFetchResult:
        status = raw.status

        def failure(state: AccessState, code: FetchErrorCode, message: str, **kw: Any) -> CrawlFetchResult:
            outcome = self._outcome(url, fetched_at, state, code, message, elapsed_ms=elapsed_ms, **kw)
            return CrawlFetchResult(document=self._document(url, outcome))

        if status == 401:
            raise CrawlerAuthFailed(token_configured=self._token is not None)
        if status == 400:
            detail = raw.json()
            text = detail.get("detail") if isinstance(detail, Mapping) else None
            text = text if isinstance(text, str) else ""
            if text.startswith("URL blocked"):
                return failure(
                    AccessState.POLICY_DENIED,
                    FetchErrorCode.CRAWLER_URL_BLOCKED,
                    "crawler SSRF protection blocked the URL",
                )
            if text.startswith(("Rejected config", "Rejected request")):
                return failure(
                    AccessState.POLICY_DENIED,
                    FetchErrorCode.CRAWLER_REJECTED_CONFIG,
                    "crawler rejected the request configuration",
                )
            raise CrawlerProtocolError(400, "crawler_bad_request")
        if status == 429:
            retry_after = _bounded_retry_after(raw.headers.get("retry-after"), fetched_at)
            return failure(
                AccessState.RATE_LIMITED,
                FetchErrorCode.CRAWLER_RATE_LIMITED,
                "crawler request rate limit reached",
                retry_after=retry_after,
            )
        if status == 504:
            return failure(
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWL_TIME_LIMIT,
                "crawl exceeded the crawler time limit",
            )
        if 500 <= status <= 599:
            return failure(
                AccessState.TRANSIENT_ERROR, FetchErrorCode.CRAWLER_SERVER_ERROR, f"crawler HTTP {status}"
            )
        if status == 413:
            raise CrawlerProtocolError(413, "crawler_request_too_large")
        if status == 422:
            raise CrawlerProtocolError(422, "crawler_request_invalid")
        if 400 <= status <= 499:
            raise CrawlerProtocolError(status)
        if status != 200:
            return failure(
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_BAD_RESPONSE,
                f"unexpected crawler HTTP {status}",
            )
        if raw.too_large:
            return failure(
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.RESPONSE_TOO_LARGE,
                "crawler response exceeded the size cap",
                size=raw.read_bytes,
            )
        body = raw.json()
        if not isinstance(body, Mapping):
            return failure(
                AccessState.TRANSIENT_ERROR, FetchErrorCode.CRAWLER_BAD_RESPONSE, "crawler returned non-JSON"
            )
        server_time = body.get("server_processing_time_s")
        server_time_s = float(server_time) if isinstance(server_time, int | float) else None
        if body.get("success") is not True:
            result = failure(
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_REPORTED_FAILURE,
                "crawler reported request failure",
            )
            return result.model_copy(update={"server_processing_time_s": server_time_s})
        results = body.get("results")
        match = self._match_result(url, results)
        if match is None:
            result = failure(
                AccessState.TRANSIENT_ERROR,
                FetchErrorCode.CRAWLER_RESULT_MISSING,
                "crawler response had no result for the URL",
            )
            return result.model_copy(update={"server_processing_time_s": server_time_s})
        mapped = self._map_result(url, match, fetched_at, elapsed_ms)
        return mapped.model_copy(update={"server_processing_time_s": server_time_s})

    @staticmethod
    def _match_result(url: str, results: object) -> Mapping[str, Any] | None:
        if not isinstance(results, list):
            return None
        candidates = [r for r in results if isinstance(r, Mapping)]
        for result in candidates:
            if result.get("url") == url:
                return result
        wanted = _url_key(url)
        if wanted is None:
            return None
        for result in candidates:
            got = result.get("url")
            if isinstance(got, str) and _url_key(got) == wanted:
                return result
        return None

    def _map_result(
        self, url: str, result: Mapping[str, Any], fetched_at: datetime, elapsed_ms: int
    ) -> CrawlFetchResult:
        success = result.get("success") is True
        status = _int_or_none(result.get("status_code"))
        error_message = result.get("error_message")
        error_text = error_message if isinstance(error_message, str) else ""
        raw_headers = result.get("response_headers")
        headers = _allowlisted_headers(raw_headers)
        redirected = result.get("redirected_url")
        final_candidate = redirected if isinstance(redirected, str) and redirected else url
        final_host = _host(final_candidate)
        requested_host = _host(url)
        final_valid = final_host is not None
        same_host = final_valid and final_host == requested_host
        final_url = final_candidate if final_valid else None
        redirect_count = 1 if final_url is not None and _url_key(final_url) != _url_key(url) else 0
        retry_after = _bounded_retry_after(headers.get("retry-after"), fetched_at)
        cache_status = result.get("cache_status")
        cache_text = cache_status if isinstance(cache_status, str) else None
        redirected_status = _int_or_none(result.get("redirected_status_code"))

        html_raw = result.get("html")
        html = html_raw if isinstance(html_raw, str) else None
        html_bytes = html.encode("utf-8") if html is not None else b""
        oversize = len(html_bytes) > self._max_response_bytes

        state: AccessState
        code: FetchErrorCode | None
        message: str | None
        retain = True
        if not success:
            lowered = error_text.lower()
            # Crawl4AI's own robots denial sets X-Robots-Status: "Blocked by robots.txt". The value
            # is checked too, so a target that happens to send that header with a real 403 is
            # still an access block (route pause), not our own policy refusal.
            header_items = raw_headers.items() if isinstance(raw_headers, Mapping) else ()
            robots_header = any(
                str(k).lower() == "x-robots-status" and "robots" in str(v).lower() for k, v in header_items
            )
            if "blocked by anti-bot protection" in lowered:
                state, code = AccessState.ACCESS_BLOCKED, FetchErrorCode.ANTI_BOT_BLOCK
                message = _safe_text(error_text, 300) or "Blocked by anti-bot protection"
            elif "access denied by robots.txt" in lowered or (status == 403 and robots_header):
                state, code, message = (
                    AccessState.POLICY_DENIED,
                    FetchErrorCode.ROBOTS_DISALLOWED,
                    ("Access denied by robots.txt"),
                )
                retain = False
            elif status is not None and (classified := _status_classification(status)) is not None:
                state, code = classified
                message = f"target HTTP {status}"
            elif "wait condition failed" in lowered:
                # The page loaded but the source's expected element never appeared: drift,
                # an app shell or an undetected interstitial, not a connectivity failure.
                state, code = AccessState.UNEXPECTED_CONTENT, FetchErrorCode.WAIT_CONDITION_FAILED
                message = "expected page element did not appear (wait_for)"
            else:
                state, code = AccessState.TRANSIENT_ERROR, FetchErrorCode.NAVIGATION_FAILED
                net_error = _NET_ERROR.search(error_text)
                if net_error:
                    message = f"navigation failed ({net_error.group(0)})"
                elif "timeout" in lowered:
                    message = "navigation failed (timeout)"
                else:
                    message = "navigation failed"
        elif not final_valid:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.FINAL_URL_INVALID,
                ("final URL is not an http(s) URL"),
            )
            retain = False
        elif not same_host:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.FINAL_URL_HOST_MISMATCH,
                ("final URL is on another host"),
            )
            retain = False
        elif cache_text is not None and cache_text.startswith("hit"):
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.CACHE_HIT_NOT_FRESH,
                ("crawler served cached content despite cache bypass"),
            )
            retain = False
        elif status is None:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.MISSING_STATUS,
                ("crawler result has no HTTP status"),
            )
        elif (classified := _status_classification(status)) is not None:
            state, code = classified
            message = f"target HTTP {status}"
        elif not 200 <= status <= 299:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.HTTP_STATUS_UNEXPECTED,
                (f"target HTTP {status}"),
            )
        elif oversize:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.RESPONSE_TOO_LARGE,
                ("page exceeded max_response_bytes"),
            )
        elif len(html_bytes.strip()) < self._min_html_bytes:
            state, code, message = (
                AccessState.UNEXPECTED_CONTENT,
                FetchErrorCode.EMPTY_BODY,
                ("empty or tiny HTML body"),
            )
        else:
            state, code, message = AccessState.OK, None, None

        if oversize or not same_host:
            retain = False  # never keep an oversize body or content served by another host
        kept_html = html if retain and html is not None else None
        text: str | None = None
        if kept_html is not None:
            markdown = _markdown_text(result.get("markdown"))
            if markdown is not None and len(markdown.encode("utf-8")) <= self._max_response_bytes:
                text = markdown

        outcome = self._outcome(
            url,
            fetched_at,
            state,
            code,
            message,
            final_url=final_url,
            http_status=status,
            elapsed_ms=elapsed_ms,
            size=len(html_bytes),
            redirect_count=redirect_count,
            retry_after=retry_after if state == AccessState.RATE_LIMITED else None,
            headers=headers,
        )
        document = self._document(
            url, outcome, html=kept_html, text=text, content_type=headers.get("content-type")
        )
        return CrawlFetchResult(
            document=document,
            cache_status=_safe_text(cache_text, 40) if cache_text else None,
            redirected_status_code=redirected_status,
        )

    # -- Read-only runtime inspection -----------------------------------------

    async def health(self) -> CrawlerHealth:
        """Public GET /health. Never sends the token; never raises for an unreachable crawler."""
        checked_at = ensure_utc(self._clock.now())
        request = self._http.build_request(
            "GET",
            f"{self._base_url}/health",
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(5.0),
        )
        request.headers.pop("authorization", None)
        try:
            with anyio.fail_after(10.0):
                raw = await self._send_bounded(request, _HEALTH_BODY_LIMIT)
        except (TimeoutError, httpx.HTTPError):
            return CrawlerHealth(reachable=False, checked_at=checked_at, error="unreachable")
        if raw.status != 200:
            return CrawlerHealth(
                reachable=True, checked_at=checked_at, http_status=raw.status, error=f"http_{raw.status}"
            )
        body = raw.json()
        if not isinstance(body, Mapping):
            return CrawlerHealth(
                reachable=True, checked_at=checked_at, http_status=200, error="non_json_body"
            )
        status = body.get("status")
        version = body.get("version")
        status_text = _safe_text(status, 40) if isinstance(status, str) else None
        version_text = version if isinstance(version, str) and _VERSION.match(version) else None
        if version_text is not None:
            self._observed_version = version_text
        return CrawlerHealth(
            reachable=True, status=status_text, version=version_text, checked_at=checked_at, http_status=200
        )

    async def _unauthenticated_probe(self) -> int | None:
        """POST /crawl without credentials and with an invalid body: 401 proves auth is on.

        `urls: []` fails validation (422) on a server without auth, so the probe can never
        trigger a crawl even on a misconfigured server.
        """
        request = self._http.build_request(
            "POST",
            f"{self._base_url}/crawl",
            json={"urls": []},
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(10.0),
        )
        request.headers.pop("authorization", None)
        try:
            with anyio.fail_after(15.0):
                raw = await self._send_bounded(request, _ERROR_BODY_LIMIT)
        except (TimeoutError, httpx.HTTPError):
            return None
        return raw.status

    async def _config_dump(self, config: dict[str, Any]) -> ConfigDumpCheck:
        config_type: Literal["CrawlerRunConfig", "BrowserConfig"] = config["type"]
        request = self._http.build_request(
            "POST",
            f"{self._base_url}/config/dump",
            json=config,
            headers=self._auth_headers(),
            timeout=httpx.Timeout(15.0),
        )
        try:
            with anyio.fail_after(20.0):
                raw = await self._send_bounded(request, _HEALTH_BODY_LIMIT * 4)
        except (TimeoutError, httpx.HTTPError):
            return ConfigDumpCheck(
                config_type=config_type,
                http_status=None,
                accepted=False,
                problems=("config_dump_unreachable",),
            )
        body = raw.json()
        if raw.status != 200 or not isinstance(body, Mapping):
            detail = body.get("detail") if isinstance(body, Mapping) else None
            return ConfigDumpCheck(
                config_type=config_type,
                http_status=raw.status,
                accepted=False,
                problems=(f"config_dump_rejected_http_{raw.status}",),
                detail=_safe_text(detail, 300) if isinstance(detail, str) else None,
            )
        params_raw = body.get("params")
        params: Mapping[str, Any] = params_raw if isinstance(params_raw, Mapping) else body
        echoed = set(find_forbidden_fields(body))
        computed: set[str] = set()
        if config_type == "BrowserConfig":
            computed = {f for f in echoed if f in SERVER_COMPUTED_BROWSER_FIELDS}
            echoed -= computed
        problems: list[str] = [f"forbidden_field_echoed:{f}" for f in sorted(echoed)]
        if config_type == "CrawlerRunConfig":
            problems.extend(_crawler_echo_problems(params))
        else:
            problems.extend(_browser_echo_problems(params, self._user_agent))
        return ConfigDumpCheck(
            config_type=config_type,
            http_status=200,
            accepted=True,
            echoed_forbidden_fields=tuple(sorted(echoed)),
            server_computed_fields=tuple(sorted(computed)),
            problems=tuple(problems),
        )

    async def inspect_contract(self) -> ContractReport:
        """Read-only activation check: /health, unauthenticated probe, /config/dump dry-runs."""
        checked_at = ensure_utc(self._clock.now())
        health = await self.health()
        problems: list[str] = []
        if not health.reachable:
            problems.append("crawler_unreachable")
        elif health.status != "ok":
            # 'healthy' is the pre-0.9.3 /health body (research section 13).
            problems.append("health_status_not_ok")
        version_matches: bool | None = None
        if self._expected_version is None:
            problems.append("expected_version_unpinned")
        elif health.version is not None:
            version_matches = health.version == self._expected_version
            if not version_matches:
                problems.append("version_mismatch")
        elif health.reachable:
            problems.append("version_unknown")

        auth_status = await self._unauthenticated_probe()
        auth_enforced = None if auth_status is None else auth_status == 401
        if auth_status is None:
            problems.append("auth_probe_failed")
        elif not auth_enforced:
            problems.append("auth_not_enforced")

        crawler_cfg = build_crawler_config(self._page_timeout_ms)
        browser_cfg = build_browser_config(self._user_agent)
        sample_payload = build_crawl_payload(
            "https://example.com/", user_agent=self._user_agent, page_timeout_ms=self._page_timeout_ms
        )
        payload_forbidden = find_forbidden_fields(sample_payload) + find_forbidden_fields(
            [o.model_dump() for o in self._source_options.values()]
        )
        if payload_forbidden:
            problems.append("payload_contains_forbidden_fields")

        crawler_check: ConfigDumpCheck | None = None
        browser_check: ConfigDumpCheck | None = None
        source_checks: dict[str, ConfigDumpCheck] = {}
        if self._token is None:
            problems.append("api_token_missing")
        else:
            crawler_check = await self._config_dump(crawler_cfg)
            browser_check = await self._config_dump(browser_cfg)
            for check in (crawler_check, browser_check):
                problems.extend(f"{check.config_type}:{p}" for p in check.problems)
            # Sources with their own options send a different "exact" config: dry-run each one.
            for source_key, options in sorted(self._source_options.items()):
                if not options.model_dump(exclude_none=True):
                    continue
                check = await self._config_dump(build_crawler_config(self._page_timeout_ms, options))
                source_checks[source_key] = check
                problems.extend(f"{source_key}:{check.config_type}:{p}" for p in check.problems)
        return ContractReport(
            checked_at=checked_at,
            base_url=self._base_url,
            health=health,
            expected_version=self._expected_version,
            version_matches=version_matches,
            token_configured=self._token is not None,
            auth_probe_status=auth_status,
            auth_enforced=auth_enforced,
            payload_forbidden_fields=tuple(sorted(set(payload_forbidden))),
            crawler_config=crawler_check,
            browser_config=browser_check,
            source_crawler_configs=source_checks,
            problems=tuple(problems),
        )


def _typed_value(value: object) -> object:
    if isinstance(value, Mapping) and "params" in value:
        return value.get("params")
    return value


def _crawler_echo_problems(params: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if params.get("check_robots_txt") is not True:
        problems.append("check_robots_txt_not_enabled")
    cache_mode = _typed_value(params.get("cache_mode", "bypass"))  # absent = 0.9.4 default BYPASS
    if not (isinstance(cache_mode, str) and cache_mode.lower().endswith("bypass")):
        problems.append("cache_mode_not_bypass")
    timeout = params.get("page_timeout", MAX_PAGE_TIMEOUT_MS)
    if not isinstance(timeout, int | float) or timeout > MAX_PAGE_TIMEOUT_MS:
        problems.append("page_timeout_not_clamped")
    return problems


def _browser_echo_problems(params: Mapping[str, Any], user_agent: str) -> list[str]:
    problems: list[str] = []
    if params.get("enable_stealth", False) is not False:
        problems.append("stealth_enabled")
    if params.get("user_agent") not in (None, user_agent):
        problems.append("user_agent_not_ours")
    if params.get("headless", True) is not True:
        problems.append("not_headless")
    if params.get("user_agent_mode") not in (None, ""):
        problems.append("user_agent_mode_set")
    return problems
