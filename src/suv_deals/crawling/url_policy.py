"""Per-source URL policy: SSRF guard plus reviewed host/path allow-lists (spec sections 8, 24).

Only adapter-generated URLs that pass this policy may reach the crawler. Users and
MCP clients supply registered listing IDs, never fetch URLs, so a URL that fails
here is a bug or an attack and is refused with a typed `PolicyDenied`.

Layers, in order:

1. Structural checks: length, ASCII-only, no whitespace/control characters,
   scheme allowed for the source (HTTPS only unless configured), no embedded
   credentials, default port only, and the shared `netguard.parse_safe_url`
   rejection of numeric/private/metadata/unusual hosts.
2. IP-literal hosts are always refused: sources are configured by DNS name.
3. Host allow-list: exact host match. Subdomains are only accepted for an
   explicit `*.example.com` rule (which does not match `example.com` itself).
4. Path allow-list per purpose, using the source's anchored regexes
   (`re.fullmatch` on the raw path). Dot segments (including `..;` matrix forms) and
   encoded separators are refused before matching so `/search/../admin` can never
   satisfy `/search/.*`.
   The robots purpose only allows exactly `/robots.txt` without a query.
5. `resolve_check()` resolves the host at fetch time with `netguard.resolve_public`,
   requiring every DNS answer to be public (DNS rebinding defence).
6. `validate_redirect()` re-applies the same policy to the final URL and refuses
   cross-host redirects and HTTPS->HTTP downgrades.

The trusted worker->Crawl4AI connection (for example `http://crawl4ai:11235`) is
an explicit infrastructure exception configured from settings; it is never checked
by, and never passes, this public-target policy.

Limitation: the DNS check here and the crawler's own resolution are separate
lookups. Crawl4AI's built-in SSRF guard and network-level egress isolation of the
crawler container (spec section 24) remain mandatory; this policy does not replace them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Final, Literal, get_args
from urllib.parse import unquote, urlsplit, urlunsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field

from suv_deals.adapters.base import FetchPurpose
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import AppError, ErrorCode
from suv_deals.netguard import (
    Resolver,
    UnsafeDestination,
    check_host_literal,
    normalize_hostname,
    parse_safe_url,
    resolve_public,
    system_resolver,
)

DEFAULT_MAX_URL_LENGTH: Final = 2048
DEFAULT_DNS_TIMEOUT_SECONDS: Final = 5.0
ROBOTS_PATH: Final = "/robots.txt"
DEFAULT_PORTS: Final[dict[str, int]] = {"https": 443, "http": 80}
_PURPOSES: Final = frozenset(get_args(FetchPurpose))
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c|00)", re.IGNORECASE)
_WILDCARD_PREFIX = "*."


class DenialReason(StrEnum):
    UNKNOWN_SOURCE = "unknown_source"
    PURPOSE_NOT_ALLOWED = "purpose_not_allowed"
    MALFORMED_URL = "malformed_url"
    URL_TOO_LONG = "url_too_long"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    EMBEDDED_CREDENTIALS = "embedded_credentials"
    PORT_NOT_ALLOWED = "port_not_allowed"
    UNSAFE_DESTINATION = "unsafe_destination"
    IP_LITERAL_HOST = "ip_literal_host"
    HOST_NOT_ALLOWED = "host_not_allowed"
    AMBIGUOUS_PATH = "ambiguous_path"
    PATH_NOT_ALLOWED = "path_not_allowed"
    CROSS_HOST_REDIRECT = "cross_host_redirect"
    SCHEME_DOWNGRADE = "scheme_downgrade"
    DNS_NON_PUBLIC = "dns_non_public"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"


DenialStage = Literal["request", "redirect", "dns"]


class PolicyDenied(AppError):
    """Our own URL policy refused a URL. The message never echoes the URL."""

    def __init__(self, reason: DenialReason, *, stage: DenialStage = "request") -> None:
        super().__init__(
            ErrorCode.FORBIDDEN,
            f"URL policy denied the request: {reason.value}",
            retryable=reason == DenialReason.DNS_RESOLUTION_FAILED,
            details={"reason": reason.value, "stage": stage},
        )
        self.reason = reason
        self.stage = stage

    @property
    def transient(self) -> bool:
        """DNS lookup failures are connectivity problems, not security refusals."""
        return self.reason == DenialReason.DNS_RESOLUTION_FAILED


class PolicyDecision(BaseModel):
    """A URL that passed the source policy for one purpose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str
    url: str
    purpose: FetchPurpose
    scheme: str
    host: str
    port: int
    path: str
    query: str = ""
    matched_rule: str = Field(max_length=500)
    resolved_addresses: tuple[str, ...] = ()


def redact_url(url: object, *, max_length: int = DEFAULT_MAX_URL_LENGTH) -> str:
    """A URL safe to store or log: no userinfo, no control characters, bounded length."""
    if not isinstance(url, str):
        return "[invalid-url]"
    cleaned = "".join(ch for ch in url if 0x20 < ord(ch) < 0x7F)
    try:
        parts = urlsplit(cleaned)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        cleaned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return "[unparseable-url]"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned or "[empty-url]"


def host_of(url: str) -> str | None:
    """Best-effort normalised hostname of a URL, or None when it has none."""
    try:
        hostname = urlsplit(url).hostname
        return normalize_hostname(hostname) if hostname else None
    except (ValueError, UnsafeDestination):
        return None


def _path_is_ambiguous(path: str) -> bool:
    if _ENCODED_SEPARATOR.search(path):
        return True
    # `..;x` is a dot segment for servlet containers that strip matrix parameters.
    return any(unquote(segment.split(";", 1)[0]) in {".", ".."} for segment in path.split("/"))


def _compile_patterns(patterns: Iterable[str], kind: str) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"empty {kind} path pattern")
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"invalid {kind} path pattern: {exc}") from exc
    return tuple(compiled)


def _parse_host_rule(entry: str) -> tuple[str, bool]:
    """Return (normalised host, is_wildcard). Raises ValueError for unsafe entries."""
    raw = entry.strip().lower()
    wildcard = raw.startswith(_WILDCARD_PREFIX)
    base = raw[len(_WILDCARD_PREFIX) :] if wildcard else raw
    if not base or "*" in base or "/" in base or ":" in base or "@" in base:
        raise ValueError(f"invalid allowed host entry {entry!r}")
    try:
        host = normalize_hostname(base)
        if check_host_literal(host) is not None:
            raise ValueError(f"allowed host {entry!r} is an IP literal; use a DNS name")
        parse_safe_url(f"https://{host}/", allowed_schemes=("https",), allowed_ports=(443,))
    except UnsafeDestination as exc:
        raise ValueError(f"allowed host {entry!r} is not a safe public DNS name: {exc}") from exc
    if wildcard and len(host.split(".")) < 2:
        raise ValueError(f"wildcard host rule {entry!r} is too broad")
    return host, wildcard


class SourceUrlPolicy:
    """Immutable URL policy for one registered source."""

    def __init__(
        self,
        source_key: str,
        *,
        allowed_hosts: Sequence[str],
        search_paths: Sequence[str] = (),
        detail_paths: Sequence[str] = (),
        allowed_schemes: Sequence[str] = ("https",),
        max_url_length: int = DEFAULT_MAX_URL_LENGTH,
    ) -> None:
        schemes = tuple(dict.fromkeys(s.lower() for s in allowed_schemes))
        if not schemes or any(s not in DEFAULT_PORTS for s in schemes):
            raise ValueError("allowed_schemes must be a non-empty subset of {'https', 'http'}")
        if not 64 <= max_url_length <= DEFAULT_MAX_URL_LENGTH:
            raise ValueError(f"max_url_length must be between 64 and {DEFAULT_MAX_URL_LENGTH}")
        exact: set[str] = set()
        wildcard_bases: set[str] = set()
        for entry in allowed_hosts:
            host, is_wildcard = _parse_host_rule(entry)
            (wildcard_bases if is_wildcard else exact).add(host)
        self.source_key = source_key
        self._schemes = schemes
        self._max_url_length = max_url_length
        self._exact_hosts = frozenset(exact)
        self._wildcard_bases = frozenset(wildcard_bases)
        self._search = _compile_patterns(search_paths, "search")
        self._detail = _compile_patterns(detail_paths, "detail")

    @classmethod
    def from_source(
        cls,
        cfg: SourceConfig,
        *,
        allowed_schemes: Sequence[str] = ("https",),
        max_url_length: int = DEFAULT_MAX_URL_LENGTH,
    ) -> SourceUrlPolicy:
        return cls(
            cfg.source_key,
            allowed_hosts=cfg.allowed_hosts,
            search_paths=cfg.allowed_search_paths,
            detail_paths=cfg.allowed_detail_paths,
            allowed_schemes=allowed_schemes,
            max_url_length=max_url_length,
        )

    @property
    def allowed_schemes(self) -> tuple[str, ...]:
        return self._schemes

    def __repr__(self) -> str:
        return (
            f"SourceUrlPolicy({self.source_key!r}, hosts={sorted(self._exact_hosts)}, "
            f"wildcards={sorted(self._wildcard_bases)})"
        )

    def host_allowed(self, host: str) -> bool:
        try:
            normalized = normalize_hostname(host)
        except UnsafeDestination:
            return False
        if normalized in self._exact_hosts:
            return True
        return any(normalized.endswith(f".{base}") for base in self._wildcard_bases)

    def check(self, url: str, purpose: FetchPurpose) -> PolicyDecision:
        """Structural + host/path policy (no DNS). Raises PolicyDenied."""
        if purpose not in _PURPOSES:
            raise PolicyDenied(DenialReason.PURPOSE_NOT_ALLOWED)
        if not isinstance(url, str) or not url:
            raise PolicyDenied(DenialReason.MALFORMED_URL)
        if len(url) > self._max_url_length:
            raise PolicyDenied(DenialReason.URL_TOO_LONG)
        if any(not 0x20 < ord(ch) < 0x7F for ch in url) or "\\" in url:
            # Whitespace, control and non-ASCII characters: adapters emit percent-encoded,
            # punycode URLs only, so anything else is ambiguous between parsers.
            raise PolicyDenied(DenialReason.MALFORMED_URL)
        try:
            parts = urlsplit(url)
        except ValueError:
            raise PolicyDenied(DenialReason.MALFORMED_URL) from None
        scheme = parts.scheme.lower()
        if scheme not in self._schemes:
            raise PolicyDenied(DenialReason.SCHEME_NOT_ALLOWED)
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            raise PolicyDenied(DenialReason.EMBEDDED_CREDENTIALS)
        try:
            port = parts.port
        except ValueError:
            raise PolicyDenied(DenialReason.PORT_NOT_ALLOWED) from None
        default_port = DEFAULT_PORTS[scheme]
        if port is not None and port != default_port:
            raise PolicyDenied(DenialReason.PORT_NOT_ALLOWED)
        try:
            target = parse_safe_url(url, allowed_schemes=(scheme,), allowed_ports=(default_port,))
            literal = check_host_literal(target.hostname)
        except UnsafeDestination:
            raise PolicyDenied(DenialReason.UNSAFE_DESTINATION) from None
        if literal is not None:
            raise PolicyDenied(DenialReason.IP_LITERAL_HOST)
        if not self.host_allowed(target.hostname):
            raise PolicyDenied(DenialReason.HOST_NOT_ALLOWED)
        path = parts.path or "/"
        if _path_is_ambiguous(path):
            raise PolicyDenied(DenialReason.AMBIGUOUS_PATH)
        rule = self._match_path(path, parts.query, purpose)
        if rule is None:
            raise PolicyDenied(DenialReason.PATH_NOT_ALLOWED)
        return PolicyDecision(
            source_key=self.source_key,
            url=url,
            purpose=purpose,
            scheme=scheme,
            host=target.hostname,
            port=default_port,
            path=path,
            query=parts.query,
            matched_rule=rule[:500],
        )

    def _match_path(self, path: str, query: str, purpose: FetchPurpose) -> str | None:
        if purpose == "robots":
            return "robots" if path == ROBOTS_PATH and not query else None
        if purpose == "search":
            groups: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (("search", self._search),)
        elif purpose == "detail":
            groups = (("detail", self._detail),)
        else:  # diagnostic: any reviewed search/detail route or robots.txt
            if path == ROBOTS_PATH and not query:
                return "robots"
            groups = (("search", self._search), ("detail", self._detail))
        for kind, patterns in groups:
            for pattern in patterns:
                if pattern.fullmatch(path):
                    return f"{kind}:{pattern.pattern}"
        return None

    def validate_redirect(self, original: str, final: str | None, purpose: FetchPurpose) -> PolicyDecision:
        """Apply the same policy to a redirect target; cross-host redirects are refused."""
        original_decision = self.check(original, purpose)
        if final is None or final == original:
            return original_decision
        final_host = host_of(final) if isinstance(final, str) else None
        if final_host is not None and final_host != original_decision.host:
            raise PolicyDenied(DenialReason.CROSS_HOST_REDIRECT, stage="redirect")
        try:
            final_decision = self.check(final, purpose)
        except PolicyDenied as exc:
            raise PolicyDenied(exc.reason, stage="redirect") from None
        if final_decision.host != original_decision.host:
            raise PolicyDenied(DenialReason.CROSS_HOST_REDIRECT, stage="redirect")
        if original_decision.scheme == "https" and final_decision.scheme != "https":
            raise PolicyDenied(DenialReason.SCHEME_DOWNGRADE, stage="redirect")
        return final_decision

    async def resolve_check(
        self,
        url: str,
        purpose: FetchPurpose,
        resolver: Resolver = system_resolver,
        *,
        timeout_s: float = DEFAULT_DNS_TIMEOUT_SECONDS,
    ) -> PolicyDecision:
        """`check()` plus a fetch-time DNS check: every answer must be a public address."""
        decision = self.check(url, purpose)
        lookup_failed = False

        async def tracking_resolver(host: str, port: int) -> list[str]:
            nonlocal lookup_failed
            try:
                answers = await resolver(host, port)
            except OSError:
                lookup_failed = True
                raise
            if not answers:
                lookup_failed = True
            return answers

        try:
            with anyio.fail_after(timeout_s):
                addresses = await resolve_public(decision.host, decision.port, tracking_resolver)
        except TimeoutError:
            raise PolicyDenied(DenialReason.DNS_RESOLUTION_FAILED, stage="dns") from None
        except UnsafeDestination:
            reason = DenialReason.DNS_RESOLUTION_FAILED if lookup_failed else DenialReason.DNS_NON_PUBLIC
            raise PolicyDenied(reason, stage="dns") from None
        return decision.model_copy(update={"resolved_addresses": tuple(str(ip) for ip in addresses)})
