"""robots.txt handling per RFC 9309 with a stored revision (spec sections 5, 24).

`urllib.robotparser` is not RFC 9309 compliant (first-match instead of longest-match,
no `*`/`$` wildcards), so this module carries a small compliant parser:

- Groups start with one or more `user-agent` lines; rules before any group are ignored.
  The crawler's product token (letters, `_`, `-`, e.g. `SUVDealResearch`) is matched
  case-insensitively; every matching group is combined, otherwise the `*` groups are
  combined, otherwise nothing applies.
- The longest matching `allow`/`disallow` pattern wins; on a tie `allow` wins.
  `*` matches any sequence and a trailing `$` anchors the end. `/robots.txt` itself is
  always allowed. Paths and patterns are compared after percent-encoding normalisation.
  Matching is a linear greedy segment search, never a backtracking regex: robots.txt
  is untrusted input and `/*a*a*a...` patterns must not stall a worker (ReDoS).
- At most 500 KiB is parsed (RFC minimum); anything beyond is ignored and flagged.
- `Crawl-delay` (non-standard, honoured) is capped at one day: untrusted absurd values
  must not overflow scheduling arithmetic.

Fetch status semantics (RFC 9309 section 2.3.1):

- 2xx with a complete body: parse; `available`. A 2xx whose body is missing or was not
  read completely (the fetcher reports an `error_kind` other than `truncated`) is
  `unreachable`: rules we could not read must not turn into "no restriction".
- 3xx: follow at most five hops, only to the same allowed host and never from HTTPS
  to HTTP. More than five hops: `unavailable`. An off-host redirect cannot be followed
  under our host policy and is treated as `unreachable` (conservative).
- 4xx except 429: `unavailable` -> no robots restriction.
- 429, 5xx, network errors and anything else: `unreachable` -> complete disallow until
  a later successful fetch.

The absence of a prohibition is recorded as `no_robots_restriction`, never as
permission: robots.txt is an operational policy, not a licence (spec section 5).
Any change in content hash, HTTP status or availability between revisions
invalidates stale activation evidence (`revision_changed`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from typing import Final, NamedTuple
from urllib.parse import urljoin, urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.crawling.rate_limits import MAX_CRAWL_DELAY_SECONDS
from suv_deals.crawling.url_policy import (
    ROBOTS_PATH,
    DenialReason,
    PolicyDenied,
    SourceUrlPolicy,
)
from suv_deals.netguard import Resolver, UnsafeDestination, normalize_hostname, parse_safe_url

ROBOTS_MAX_BYTES: Final = 500 * 1024
MAX_REDIRECT_HOPS: Final = 5
DEFAULT_FETCH_TIMEOUT_S: Final = 30.0  # per hop; a fetcher that hangs counts as unreachable
BODY_EXCERPT_CHARS: Final = 2000
ROBOTS_MAX_AGE: Final = timedelta(hours=24)  # RFC 9309 2.4: do not use a cached copy longer
# A fetcher that cut an oversize body at the parse limit reports this error_kind with the
# first bytes as body; that body is still parsed (RFC 9309 2.5). Any other error_kind on a
# 2xx means the body is incomplete or unreliable.
TRUNCATED_ERROR_KIND: Final = "truncated"
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")
_EXCERPT_STRIP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class RobotsAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"  # 4xx: no restriction applies
    UNREACHABLE = "unreachable"  # 5xx/429/network: complete disallow


class RobotsBasis(StrEnum):
    NO_ROBOTS_RESTRICTION = "no_robots_restriction"  # not permission, only absence of a prohibition
    DISALLOWED_BY_ROBOTS = "disallowed_by_robots"
    ROBOTS_UNREACHABLE = "robots_unreachable"
    ROBOTS_STALE = "robots_stale"
    HOST_MISMATCH = "host_mismatch"


class RobotsRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allow: bool
    pattern: str = Field(min_length=1, max_length=2048)


class RobotsGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...] = ()
    crawl_delay: Decimal | None = None


class ParsedRobots(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: tuple[RobotsGroup, ...]
    recognized_lines: int
    invalid_lines: int
    truncated: bool


class RobotsFetchResponse(NamedTuple):
    """What an injected fetcher returns. It must NOT follow redirects itself.

    `error_kind` is None for a complete response, `"truncated"` when the body was cut at
    the size cap (the returned prefix is parsed), and anything else for a network or read
    failure (a 2xx with such an error is treated as unreachable).
    """

    status: int | None
    body: bytes | None
    error_kind: str | None
    location: str | None = None


RobotsFetchResult = RobotsFetchResponse | tuple[int | None, bytes | None, str | None]
RobotsFetcher = Callable[[str], Awaitable[RobotsFetchResult]]


class RobotsRevision(BaseModel):
    """Stored robots.txt revision for one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    robots_url: str
    fetched_at: datetime
    http_status: int | None
    availability: RobotsAvailability
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    parse_ok: bool
    # Retained text (at most ROBOTS_MAX_BYTES of UTF-8 input) for ops.robots_revisions.body;
    # `groups` can always be rebuilt from it with parse_robots_txt().
    body: str | None = Field(default=None, max_length=ROBOTS_MAX_BYTES)
    body_excerpt: str | None = Field(default=None, max_length=BODY_EXCERPT_CHARS)
    body_truncated: bool = False
    redirect_count: int = Field(default=0, ge=0)
    error_kind: str | None = Field(default=None, max_length=80)
    groups: tuple[RobotsGroup, ...] = ()
    invalid_lines: int = Field(default=0, ge=0)

    @field_validator("fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RobotsVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    basis: RobotsBasis
    availability: RobotsAvailability
    matched_rule: str | None = None


# ---------------------------------------------------------------------------
# Parsing


def _normalize_octets(value: str) -> str:
    """Percent-encode non-ASCII/space/control octets, decode escaped unreserved characters."""
    encoded = "".join(
        f"%{byte:02X}" if byte <= 0x20 or byte >= 0x7F else chr(byte) for byte in value.encode("utf-8")
    )

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT.sub(fix, encoded)


def _parse_delay(value: str) -> Decimal | None:
    try:
        delay = Decimal(value)
    except InvalidOperation:
        return None
    if not delay.is_finite() or delay < 0:
        return None
    return min(delay, MAX_CRAWL_DELAY_SECONDS)


def product_token(user_agent: str) -> str:
    """'SUVDealResearch/0.1 (+...)' -> 'suvdealresearch' (RFC 9309 2.2.1, case-insensitive)."""
    match = _PRODUCT_TOKEN.match(user_agent.strip())
    return match.group(0).lower() if match else ""


def parse_robots_txt(data: bytes) -> ParsedRobots:
    truncated = len(data) > ROBOTS_MAX_BYTES
    if truncated:
        data = data[:ROBOTS_MAX_BYTES]
        cut = max(data.rfind(b"\n"), data.rfind(b"\r"))
        data = data[: cut + 1] if cut >= 0 else b""
    text = data.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]

    groups: list[RobotsGroup] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []
    delay: Decimal | None = None
    in_rules = False
    recognized = 0
    invalid = 0

    def flush() -> None:
        if agents:
            groups.append(RobotsGroup(user_agents=tuple(agents), rules=tuple(rules), crawl_delay=delay))

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            invalid += 1
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                flush()
                agents, rules, delay, in_rules = [], [], None, False
            token = product_token(value) if value != "*" else "*"
            if token:
                agents.append(token)
            recognized += 1
        elif key in {"allow", "disallow"}:
            recognized += 1
            if not agents:
                continue  # rules outside a group are ignored
            in_rules = True
            if value:  # an empty pattern disallows/allows nothing
                rules.append(RobotsRule(allow=key == "allow", pattern=_normalize_octets(value)[:2048]))
        elif key == "crawl-delay":
            recognized += 1
            if agents:
                in_rules = True
                parsed = _parse_delay(value)
                if parsed is not None:
                    delay = parsed if delay is None else max(delay, parsed)
        elif key == "sitemap":
            recognized += 1
        else:
            invalid += 1
    flush()
    return ParsedRobots(
        groups=tuple(groups), recognized_lines=recognized, invalid_lines=invalid, truncated=truncated
    )


@lru_cache(maxsize=4096)
def _pattern_segments(pattern: str) -> tuple[tuple[str, ...], bool]:
    """('/a*b$') -> (('/a', 'b'), True). Only a trailing `$` anchors; `*` separates segments."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return tuple(body.split("*")), anchored


def pattern_matches(pattern: str, target: str) -> bool:
    """RFC 9309 pattern match (`*` wildcard, trailing `$` end anchor) from the path start.

    Greedy leftmost search of each literal segment is exact for `*`-only patterns and
    runs in linear passes (`str.find`), so hostile patterns cannot cause backtracking.
    """
    segments, anchored = _pattern_segments(pattern)
    first = segments[0]
    if not target.startswith(first):
        return False
    if len(segments) == 1:
        return target == first if anchored else True
    position = len(first)
    for segment in segments[1:-1]:
        if not segment:
            continue
        found = target.find(segment, position)
        if found < 0:
            return False
        position = found + len(segment)
    last = segments[-1]
    if anchored:
        return len(target) - len(last) >= position and target.endswith(last)
    return not last or target.find(last, position) >= 0


def _applicable_groups(groups: Sequence[RobotsGroup], user_agent: str) -> list[RobotsGroup]:
    token = product_token(user_agent)
    specific = [g for g in groups if token and token in g.user_agents]
    if specific:
        return specific
    return [g for g in groups if "*" in g.user_agents]


def _match_target(url: str) -> str:
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return _normalize_octets(target)


# ---------------------------------------------------------------------------
# Evaluation


def evaluate(
    revision: RobotsRevision,
    url: str,
    user_agent: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = ROBOTS_MAX_AGE,
) -> RobotsVerdict:
    """Decide whether `url` may be fetched under `revision` for `user_agent`."""
    availability = revision.availability

    def verdict(allowed: bool, basis: RobotsBasis, rule: str | None = None) -> RobotsVerdict:
        return RobotsVerdict(allowed=allowed, basis=basis, availability=availability, matched_rule=rule)

    try:
        hostname = urlsplit(url).hostname
        host = normalize_hostname(hostname) if hostname else None
    except (ValueError, UnsafeDestination):
        host = None
    if host is None or host != revision.host:
        return verdict(False, RobotsBasis.HOST_MISMATCH)
    if urlsplit(url).path == ROBOTS_PATH:
        return verdict(True, RobotsBasis.NO_ROBOTS_RESTRICTION)
    if now is not None and ensure_utc(now) - revision.fetched_at > max_age:
        return verdict(False, RobotsBasis.ROBOTS_STALE)
    if availability == RobotsAvailability.UNREACHABLE:
        return verdict(False, RobotsBasis.ROBOTS_UNREACHABLE)
    if availability == RobotsAvailability.UNAVAILABLE:
        return verdict(True, RobotsBasis.NO_ROBOTS_RESTRICTION)

    target = _match_target(url)
    best: RobotsRule | None = None
    for group in _applicable_groups(revision.groups, user_agent):
        for rule in group.rules:
            if not pattern_matches(rule.pattern, target):
                continue
            longer = best is None or len(rule.pattern) > len(best.pattern)
            tie_allow = best is not None and len(rule.pattern) == len(best.pattern) and rule.allow
            if longer or tie_allow:
                best = rule
    if best is not None and not best.allow:
        return verdict(False, RobotsBasis.DISALLOWED_BY_ROBOTS, f"Disallow: {best.pattern}")
    rule_text = f"Allow: {best.pattern}" if best is not None else None
    return verdict(True, RobotsBasis.NO_ROBOTS_RESTRICTION, rule_text)


def is_allowed(revision: RobotsRevision, url: str, user_agent: str, *, now: datetime | None = None) -> bool:
    return evaluate(revision, url, user_agent, now=now).allowed


def crawl_delay(revision: RobotsRevision, user_agent: str) -> Decimal | None:
    """Largest crawl-delay among the applicable groups (non-standard, but honoured)."""
    if revision.availability != RobotsAvailability.AVAILABLE:
        return None
    delays = [
        g.crawl_delay for g in _applicable_groups(revision.groups, user_agent) if g.crawl_delay is not None
    ]
    return max(delays) if delays else None


def revision_changed(previous: RobotsRevision | None, new: RobotsRevision) -> bool:
    """True when stale activation evidence must be invalidated (spec section 5)."""
    if previous is None:
        return True
    return (
        previous.host != new.host
        or previous.content_hash != new.content_hash
        or previous.http_status != new.http_status
        or previous.availability != new.availability
    )


# ---------------------------------------------------------------------------
# Fetching


def robots_url_for(policy: SourceUrlPolicy, host: str, *, scheme: str = "https") -> str:
    """The robots.txt URL for an allowed host (raises PolicyDenied otherwise)."""
    try:
        normalized = normalize_hostname(host)
    except UnsafeDestination:
        raise PolicyDenied(DenialReason.UNSAFE_DESTINATION) from None
    url = f"{scheme}://{normalized}{ROBOTS_PATH}"
    return policy.check(url, "robots").url


def _coerce(result: RobotsFetchResult) -> RobotsFetchResponse:
    if isinstance(result, RobotsFetchResponse):
        return result
    status, body, error_kind = result
    return RobotsFetchResponse(status, body, error_kind)


def _excerpt(text: str) -> str:
    return _EXCERPT_STRIP.sub("", text)[:BODY_EXCERPT_CHARS]


def _redirect_target(current: str, location: str, host: str) -> str | None:
    try:
        target = urljoin(current, location.strip())
        safe = parse_safe_url(target)
    except (ValueError, UnsafeDestination):
        return None
    if safe.hostname != host or safe.port != (443 if safe.scheme == "https" else 80):
        return None
    if urlsplit(current).scheme == "https" and safe.scheme != "https":
        return None
    return target


async def fetch_robots(
    policy: SourceUrlPolicy,
    host: str,
    fetcher: RobotsFetcher,
    *,
    resolver: Resolver | None,
    clock: Clock | None = None,
    scheme: str = "https",
    max_hops: int = MAX_REDIRECT_HOPS,
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
) -> RobotsRevision:
    """Fetch and classify robots.txt for an allowed host.

    `resolver` runs the policy's fetch-time public-DNS check before every hop; pass
    `None` only when the fetcher itself pins connections to validated addresses. Each
    hop is bounded by `fetch_timeout_s`; a timeout is `unreachable` (complete disallow).
    """
    if fetch_timeout_s <= 0:
        raise ValueError("fetch_timeout_s must be positive")
    clock = clock or SystemClock()
    robots_url = robots_url_for(policy, host, scheme=scheme)
    normalized_host = normalize_hostname(host)
    current = robots_url
    hops = 0

    def revision(
        availability: RobotsAvailability,
        *,
        status: int | None,
        error_kind: str | None = None,
        body: bytes | None = None,
        cut_by_fetcher: bool = False,
    ) -> RobotsRevision:
        content_hash: str | None = None
        excerpt: str | None = None
        retained_text: str | None = None
        parsed: ParsedRobots | None = None
        parse_ok = availability != RobotsAvailability.UNREACHABLE
        if body is not None:
            parsed = parse_robots_txt(body)
            retained = body[:ROBOTS_MAX_BYTES]
            text = retained.decode("utf-8", errors="replace").lstrip("\ufeff")
            content_hash = hashlib.sha256(retained).hexdigest()
            excerpt = _excerpt(text)
            # Replacement characters can expand invalid input; keep the stored text byte-bounded.
            retained_text = (
                text.replace("\x00", "").encode("utf-8")[:ROBOTS_MAX_BYTES].decode("utf-8", errors="ignore")
            )
            parse_ok = parsed.recognized_lines > 0 or not text.strip()
        return RobotsRevision(
            host=normalized_host,
            robots_url=robots_url,
            fetched_at=ensure_utc(clock.now()),
            http_status=status,
            availability=availability,
            content_hash=content_hash,
            parse_ok=parse_ok,
            body=retained_text,
            body_excerpt=excerpt,
            body_truncated=cut_by_fetcher or (parsed.truncated if parsed else False),
            redirect_count=hops,
            error_kind=error_kind[:80] if error_kind else None,
            groups=parsed.groups if parsed else (),
            invalid_lines=parsed.invalid_lines if parsed else 0,
        )

    while True:
        if resolver is not None:
            # Redirect targets are pinned to the same host, so the allowed robots URL is
            # re-resolved before every hop (DNS rebinding between hops is caught too).
            try:
                await policy.resolve_check(robots_url, "robots", resolver)
            except PolicyDenied as exc:
                return revision(RobotsAvailability.UNREACHABLE, status=None, error_kind=exc.reason.value)
        try:
            with anyio.fail_after(fetch_timeout_s):
                response = _coerce(await fetcher(current))
        except TimeoutError:
            return revision(RobotsAvailability.UNREACHABLE, status=None, error_kind="timeout")
        except Exception:  # any fetcher failure is a network failure; cancellation is not caught
            return revision(RobotsAvailability.UNREACHABLE, status=None, error_kind="fetch_exception")
        status = response.status
        if status is None:
            return revision(
                RobotsAvailability.UNREACHABLE, status=None, error_kind=response.error_kind or "network"
            )
        if 300 <= status <= 399:
            if not response.location:
                return revision(
                    RobotsAvailability.UNREACHABLE, status=status, error_kind="redirect_without_location"
                )
            hops += 1
            if hops > max_hops:
                return revision(
                    RobotsAvailability.UNAVAILABLE, status=status, error_kind="too_many_redirects"
                )
            target = _redirect_target(current, response.location, normalized_host)
            if target is None:
                return revision(RobotsAvailability.UNREACHABLE, status=status, error_kind="redirect_off_host")
            current = target
            continue
        if 200 <= status <= 299:
            # Fail closed: a body we could not read completely might hide Disallow rules.
            if response.body is None:
                return revision(
                    RobotsAvailability.UNREACHABLE,
                    status=status,
                    error_kind=response.error_kind or "body_missing",
                )
            cut = response.error_kind == TRUNCATED_ERROR_KIND
            if response.error_kind is not None and not cut:
                return revision(RobotsAvailability.UNREACHABLE, status=status, error_kind=response.error_kind)
            return revision(
                RobotsAvailability.AVAILABLE,
                status=status,
                body=response.body,
                error_kind=response.error_kind,
                cut_by_fetcher=cut,
            )
        if status == 429 or 500 <= status <= 599:
            return revision(RobotsAvailability.UNREACHABLE, status=status, error_kind=f"http_{status}")
        if 400 <= status <= 499:
            return revision(RobotsAvailability.UNAVAILABLE, status=status, error_kind=f"http_{status}")
        return revision(RobotsAvailability.UNREACHABLE, status=status, error_kind=f"http_{status}")
