"""Per-source URL canonicalisation, host/path policy and identity material.

Structural destination safety (schemes, credentials, private/numeric hosts, ports)
is delegated to the shared guard in `suv_deals.netguard`; this module adds the
per-source host allow-list and anchored path regexes from `SourceConfig`
(spec sections 10 and 24). DNS/redirect validation at connection time belongs to
the crawl client and `crawling/url_policy.py`.

Canonicalisation only applies transformations that are safe for every source:
lower-case scheme and host, default port removal, fragment removal and removal of
the configured tracking parameters (plus any `utm_*`). Other query parameters and
their order are kept byte-for-byte because they may carry the listing identity.

Scope note: `domain/identity.py` (owned by another work package) will own the
system-wide identity/alias rules; this is a minimal local implementation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Literal
from urllib.parse import unquote, unquote_plus, urljoin, urlsplit, urlunsplit

from suv_deals.adapters.base import CanonicalIdentity
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import ValidationFailed
from suv_deals.netguard import UnsafeDestination, normalize_hostname, parse_safe_url

IdentityMethod = Literal["provider_id", "canonical_url"]

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_ID = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def canonicalize_url(url: str, *, tracking_params: Iterable[str], base: str | None = None) -> str:
    """Canonical form of an absolute (or `base`-relative) http(s) URL.

    Raises ValidationFailed for unsafe or malformed URLs (never returns them).
    """
    candidate = url.strip()
    if base is not None:
        candidate = urljoin(base, candidate)
    try:
        target = parse_safe_url(candidate)
    except UnsafeDestination as exc:
        raise ValidationFailed(f"URL rejected by destination policy: {exc}") from None
    parts = urlsplit(candidate)
    scheme = target.scheme
    netloc = target.hostname
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{parts.port}"
    drop = {p.lower() for p in tracking_params}
    kept: list[str] = []
    for segment in parts.query.split("&"):
        if not segment:
            continue
        key = unquote_plus(segment.split("=", 1)[0]).strip().lower()
        if key in drop or key.startswith("utm_"):
            continue
        kept.append(segment)
    return urlunsplit((scheme, netloc, parts.path or "/", "&".join(kept), ""))


def clean_provider_id(raw: object) -> str | None:
    """A provider listing ID usable as identity material, or None."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    if not value or len(value) > _MAX_ID or _CONTROL.search(value):
        return None
    return value


def build_identity(source_key: str, canonical_url: str, provider_id: str | None) -> CanonicalIdentity:
    """Source-scoped identity. `identity_hash` = sha256 of the exact `identity_material`."""
    method: IdentityMethod
    if provider_id:
        method, value, listing_id = "provider_id", provider_id, provider_id
    else:
        method, value = "canonical_url", canonical_url
        listing_id = (
            canonical_url
            if len(canonical_url) <= _MAX_ID
            else "url-sha256:" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        )
    material = f"{source_key}|{method}|{value}"
    if len(material) > 2048:
        raise ValidationFailed("identity material exceeds 2048 characters")
    return CanonicalIdentity(
        source_key=source_key,
        source_listing_id=listing_id,
        canonical_url=canonical_url,
        identity_method=method,
        identity_material=material,
        identity_hash=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )


_ENCODED_SEPARATOR = re.compile(r"%(2f|5c)", re.IGNORECASE)


def path_is_ambiguous(path: str) -> bool:
    """True for paths a browser/server could resolve differently from the regex check.

    `/fahrzeug/../admin`, `/fahrzeug/%2e%2e/admin` and encoded separators (`%2F`, `%5C`)
    would let a permissive pattern such as `/fahrzeug/.+` reach other paths on the host.
    """
    if "\\" in path or _ENCODED_SEPARATOR.search(path):
        return True
    return any(unquote(segment) in {".", ".."} for segment in path.split("/"))


def _compile(patterns: Iterable[str], field: str, source_key: str) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValidationFailed(f"source {source_key}: invalid {field} pattern: {exc}") from None
    return tuple(compiled)


class UrlPolicy:
    """Host/path allow-lists of one source. Paths are matched with `re.fullmatch` (anchored)."""

    def __init__(self, config: SourceConfig) -> None:
        self.source_key = config.source_key
        hosts: set[str] = set()
        for host in config.allowed_hosts:
            try:
                hosts.add(normalize_hostname(host))
            except UnsafeDestination:
                raise ValidationFailed(f"source {config.source_key}: invalid allowed host") from None
        self.hosts = frozenset(hosts)
        self.search_paths = _compile(config.allowed_search_paths, "allowed_search_paths", config.source_key)
        self.detail_paths = _compile(config.allowed_detail_paths, "allowed_detail_paths", config.source_key)
        self.tracking_params = tuple(config.tracking_params)

    def canonical(self, url: str, base: str | None = None) -> str:
        return canonicalize_url(url, tracking_params=self.tracking_params, base=base)

    def host_allowed(self, url: str) -> bool:
        try:
            host = parse_safe_url(url.strip()).hostname
        except UnsafeDestination:
            return False
        return host in self.hosts

    def _path_allowed(self, url: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
        if not patterns or not self.host_allowed(url):
            return False
        path = urlsplit(url.strip()).path or "/"
        if path_is_ambiguous(path):
            return False
        return any(p.fullmatch(path) for p in patterns)

    def is_search_url(self, url: str) -> bool:
        return self._path_allowed(url, self.search_paths)

    def is_detail_url(self, url: str) -> bool:
        return self._path_allowed(url, self.detail_paths)
