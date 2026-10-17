"""Unit tests for RFC 9309 robots.txt handling (spec sections 5, 24)."""

from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import pytest

from suv_deals.clock import FrozenClock
from suv_deals.crawling.rate_limits import MAX_CRAWL_DELAY_SECONDS
from suv_deals.crawling.robots import (
    BODY_EXCERPT_CHARS,
    ROBOTS_MAX_BYTES,
    TRUNCATED_ERROR_KIND,
    RobotsAvailability,
    RobotsBasis,
    RobotsFetchResponse,
    RobotsRevision,
    crawl_delay,
    evaluate,
    fetch_robots,
    is_allowed,
    parse_robots_txt,
    pattern_matches,
    product_token,
    revision_changed,
    robots_url_for,
)
from suv_deals.crawling.url_policy import PolicyDenied, SourceUrlPolicy

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
HOST = "www.dealer-example.com"
UA = "SUVDealResearch/0.1 (+private research; contact owner)"
POLICY = SourceUrlPolicy("dealer_example", allowed_hosts=(HOST,), search_paths=(r"/search",))


def revision_from(text: str | bytes, *, status: int = 200) -> RobotsRevision:
    body = text.encode("utf-8") if isinstance(text, str) else text
    parsed = parse_robots_txt(body)
    return RobotsRevision(
        host=HOST,
        robots_url=f"https://{HOST}/robots.txt",
        fetched_at=NOW,
        http_status=status,
        availability=RobotsAvailability.AVAILABLE,
        content_hash=hashlib.sha256(body).hexdigest(),
        parse_ok=True,
        groups=parsed.groups,
    )


def url(path: str) -> str:
    return f"https://{HOST}{path}"


class Fetcher:
    """Scripted fetcher keyed by URL; records calls."""

    def __init__(
        self, responses: dict[str, RobotsFetchResponse | tuple[int | None, bytes | None, str | None]]
    ):
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(
        self, target: str
    ) -> RobotsFetchResponse | tuple[int | None, bytes | None, str | None]:
        self.calls.append(target)
        return self.responses[target]


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


async def fetch(responses: dict[str, object], **kwargs: object) -> RobotsRevision:
    fetcher = Fetcher(responses)  # type: ignore[arg-type]
    return await fetch_robots(
        POLICY,
        HOST,
        fetcher,
        resolver=public_resolver,
        clock=FrozenClock(NOW),
        **kwargs,  # type: ignore[arg-type]
    )


class TestParsing:
    def test_product_token(self) -> None:
        assert product_token(UA) == "suvdealresearch"
        assert product_token("  Googlebot/2.1") == "googlebot"
        assert product_token("123") == ""

    def test_groups_and_comments(self) -> None:
        parsed = parse_robots_txt(
            b"# comment\nUser-agent: SUVDealResearch\nUser-agent: OtherBot\nDisallow: /private # tail\n"
            b"Crawl-delay: 30\n\nuser-agent: *\nallow: /\nSitemap: https://x/s.xml\nfoo bar\nUnknown: 1\n"
        )
        assert len(parsed.groups) == 2
        assert parsed.groups[0].user_agents == ("suvdealresearch", "otherbot")
        assert parsed.groups[0].rules[0].pattern == "/private"
        assert parsed.groups[0].crawl_delay == Decimal(30)
        assert parsed.groups[1].user_agents == ("*",)
        assert parsed.invalid_lines == 2
        assert not parsed.truncated

    def test_rules_before_any_group_ignored(self) -> None:
        revision = revision_from("Disallow: /\nUser-agent: *\nDisallow: /x\n")
        assert is_allowed(revision, url("/search"), UA)
        assert not is_allowed(revision, url("/x"), UA)

    def test_bom_and_crlf(self) -> None:
        revision = revision_from(b"\xef\xbb\xbfUser-agent: *\r\nDisallow: /search\r\n")
        assert not is_allowed(revision, url("/search"), UA)


class TestMatching:
    def test_specific_group_beats_star(self) -> None:
        revision = revision_from(
            "User-agent: *\nDisallow: /\n\nUser-agent: suvdealresearch\nDisallow: /private\n"
        )
        assert is_allowed(revision, url("/search"), UA)
        assert not is_allowed(revision, url("/private/1"), UA)
        assert not is_allowed(revision, url("/search"), "SomeOtherBot/1.0")

    def test_matching_groups_are_combined(self) -> None:
        revision = revision_from(
            "User-agent: SUVDealResearch\nDisallow: /a\n\nUser-agent: suvdealresearch/9.9\nDisallow: /b\n"
        )
        assert not is_allowed(revision, url("/a"), UA)
        assert not is_allowed(revision, url("/b"), UA)

    def test_star_groups_combined_when_no_specific(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n")
        assert not is_allowed(revision, url("/a"), UA)
        assert not is_allowed(revision, url("/b"), UA)

    def test_no_groups_allows_everything(self) -> None:
        verdict = evaluate(revision_from(""), url("/anything"), UA)
        assert verdict.allowed
        assert verdict.basis == RobotsBasis.NO_ROBOTS_RESTRICTION

    def test_longest_match_wins(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /search\nAllow: /search/suv\n")
        assert is_allowed(revision, url("/search/suv?x=1"), UA)
        assert not is_allowed(revision, url("/search/sedan"), UA)
        verdict = evaluate(revision, url("/search/sedan"), UA)
        assert verdict.basis == RobotsBasis.DISALLOWED_BY_ROBOTS
        assert verdict.matched_rule == "Disallow: /search"

    def test_allow_wins_tie(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /page\nAllow: /page\n")
        verdict = evaluate(revision, url("/page"), UA)
        assert verdict.allowed
        assert verdict.basis == RobotsBasis.NO_ROBOTS_RESTRICTION
        assert verdict.matched_rule == "Allow: /page"

    def test_wildcards_and_end_anchor(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /*.pdf$\nDisallow: /fahrzeug/*/print\n")
        assert not is_allowed(revision, url("/docs/a.pdf"), UA)
        assert is_allowed(revision, url("/docs/a.pdf?x=1"), UA)
        assert is_allowed(revision, url("/docs/a.pdfx"), UA)
        assert not is_allowed(revision, url("/fahrzeug/123/print"), UA)
        assert is_allowed(revision, url("/fahrzeug/123"), UA)

    def test_query_is_part_of_match(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /search?sort=\n")
        assert not is_allowed(revision, url("/search?sort=price"), UA)
        assert is_allowed(revision, url("/search?page=2"), UA)

    def test_percent_encoding_normalised(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /%7Ejoe\nDisallow: /münchen\n")
        assert not is_allowed(revision, url("/~joe/x"), UA)
        assert not is_allowed(revision, url("/m%C3%BCnchen/1"), UA)
        assert not is_allowed(revision, url("/m%c3%bcnchen/1"), UA)

    def test_empty_disallow_means_no_restriction(self) -> None:
        revision = revision_from("User-agent: *\nDisallow:\n")
        assert is_allowed(revision, url("/anything"), UA)

    def test_robots_txt_always_allowed(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /\n")
        assert is_allowed(revision, url("/robots.txt"), UA)
        assert not is_allowed(revision, url("/"), UA)

    def test_host_mismatch(self) -> None:
        verdict = evaluate(revision_from(""), "https://other.example.com/x", UA)
        assert not verdict.allowed
        assert verdict.basis == RobotsBasis.HOST_MISMATCH
        assert not is_allowed(revision_from(""), "raw:<html/>", UA)

    def test_stale_revision_is_not_used(self) -> None:
        revision = revision_from("")
        assert is_allowed(revision, url("/x"), UA, now=NOW + timedelta(hours=23))
        verdict = evaluate(revision, url("/x"), UA, now=NOW + timedelta(hours=25))
        assert not verdict.allowed
        assert verdict.basis == RobotsBasis.ROBOTS_STALE

    def test_hostile_wildcard_pattern_cannot_stall_evaluation(self) -> None:
        # A backtracking regex needs exponential time here; the segment matcher is linear.
        hostile = "/" + "*a" * 900 + "*b"
        revision = revision_from(f"User-agent: *\nDisallow: {hostile}\nDisallow: /x\n")
        started = time.monotonic()
        for _ in range(20):
            assert is_allowed(revision, url("/" + "a" * 1900), UA)
        assert not is_allowed(revision, url("/x"), UA)
        assert time.monotonic() - started < 2.0

    def test_dollar_only_anchors_at_the_end(self) -> None:
        revision = revision_from("User-agent: *\nDisallow: /a$b\nDisallow: /exact$\n")
        assert not is_allowed(revision, url("/a$b/c"), UA)
        assert not is_allowed(revision, url("/exact"), UA)
        assert is_allowed(revision, url("/exact/more"), UA)

    def test_never_reported_as_permission(self) -> None:
        bases = {b.value for b in RobotsBasis}
        assert "permitted" not in bases and "allowed" not in bases


def _reference_match(pattern: str, target: str) -> bool:
    """RFC 9309 semantics as a (slow, backtracking) regex: the oracle for the fast matcher."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*")) + (r"\Z" if anchored else "")
    return re.match(regex, target, re.DOTALL) is not None


class TestPatternMatcher:
    @pytest.mark.parametrize(
        ("pattern", "target", "expected"),
        [
            ("/", "/anything", True),
            ("*", "/x", True),
            ("$", "/", False),
            ("/$", "/", True),
            ("/$", "/a", False),
            ("/a*$", "/a", True),
            ("/a*a$", "/a", False),
            ("/a*a$", "/aa", True),
            ("/*.php$", "/index.php", True),
            ("/*.php$", "/index.php?x=1", False),
            ("/*.php", "/index.php?x=1", True),
            ("/a**b", "/ab", True),
            ("/fish*.php", "/fishheads/catfish.php?parameters", True),
            ("/fish*.php", "/Fish.PHP", False),
        ],
    )
    def test_examples(self, pattern: str, target: str, expected: bool) -> None:
        assert pattern_matches(pattern, target) is expected
        assert _reference_match(pattern, target) is expected

    def test_agrees_with_reference_semantics(self) -> None:
        rng = random.Random(9309)
        alphabet = "/ab.$*?="
        for _ in range(20000):
            pattern = "/" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7)))
            target = "/" + "".join(rng.choice("/ab.$?=") for _ in range(rng.randint(0, 9)))
            assert pattern_matches(pattern, target) == _reference_match(pattern, target), (pattern, target)


class TestCrawlDelay:
    def test_specific_group(self) -> None:
        revision = revision_from(
            "User-agent: *\nCrawl-delay: 5\n\nUser-agent: SUVDealResearch\nCrawl-delay: 45\n"
        )
        assert crawl_delay(revision, UA) == Decimal(45)
        assert crawl_delay(revision, "OtherBot") == Decimal(5)

    def test_max_of_combined_and_invalid_ignored(self) -> None:
        revision = revision_from(
            "User-agent: *\nCrawl-delay: abc\nCrawl-delay: -3\nCrawl-delay: 2.5\n\n"
            "User-agent: *\nCrawl-delay: 10\n"
        )
        assert crawl_delay(revision, UA) == Decimal(10)

    @pytest.mark.parametrize("hostile", ["1e30", "1e999999999", "99999999999"])
    def test_absurd_values_capped_at_one_day(self, hostile: str) -> None:
        revision = revision_from(f"User-agent: *\nCrawl-delay: {hostile}\n")
        assert crawl_delay(revision, UA) == MAX_CRAWL_DELAY_SECONDS == Decimal(86_400)

    def test_none_without_directive_or_when_unavailable(self) -> None:
        assert crawl_delay(revision_from("User-agent: *\nDisallow: /x\n"), UA) is None
        unavailable = revision_from("User-agent: *\nCrawl-delay: 9\n").model_copy(
            update={"availability": RobotsAvailability.UNREACHABLE}
        )
        assert crawl_delay(unavailable, UA) is None


ROBOTS = url("/robots.txt")


class TestFetchSemantics:
    async def test_2xx_available(self) -> None:
        body = b"User-agent: *\nDisallow: /private\n"
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, body, None)})
        assert revision.availability == RobotsAvailability.AVAILABLE
        assert revision.content_hash == hashlib.sha256(body).hexdigest()
        assert revision.parse_ok
        assert revision.body == body.decode()
        assert revision.robots_url == ROBOTS
        assert revision.fetched_at == NOW
        assert not is_allowed(revision, url("/private"), UA)
        assert is_allowed(revision, url("/search"), UA)

    async def test_2xx_without_body_fails_closed(self) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, None, None)})
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "body_missing"
        assert not is_allowed(revision, url("/search"), UA)

    @pytest.mark.parametrize("kind", ["read_error", "connection_reset", "timeout"])
    async def test_2xx_with_incomplete_body_fails_closed(self, kind: str) -> None:
        # The unread remainder might hold `Disallow: /`: never treat it as "no restriction".
        partial = b"User-agent: *\nAllow: /public\n"
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, partial, kind)})
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == kind
        assert revision.content_hash is None
        verdict = evaluate(revision, url("/search"), UA)
        assert not verdict.allowed and verdict.basis == RobotsBasis.ROBOTS_UNREACHABLE

    async def test_2xx_cut_at_size_cap_by_fetcher_is_parsed(self) -> None:
        body = b"User-agent: *\nDisallow: /private\n"
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, body, TRUNCATED_ERROR_KIND)})
        assert revision.availability == RobotsAvailability.AVAILABLE
        assert revision.body_truncated
        assert revision.error_kind == TRUNCATED_ERROR_KIND
        assert not is_allowed(revision, url("/private"), UA)
        assert is_allowed(revision, url("/search"), UA)

    async def test_plain_tuple_responses_accepted(self) -> None:
        revision = await fetch({ROBOTS: (200, b"", None)})
        assert revision.availability == RobotsAvailability.AVAILABLE
        assert revision.parse_ok
        assert is_allowed(revision, url("/x"), UA)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
    async def test_4xx_unavailable_means_no_restriction(self, status: int) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(status, b"<html>denied</html>", None)})
        assert revision.availability == RobotsAvailability.UNAVAILABLE
        assert revision.content_hash is None
        verdict = evaluate(revision, url("/anything"), UA)
        assert verdict.allowed
        assert verdict.basis == RobotsBasis.NO_ROBOTS_RESTRICTION

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 100, 600])
    async def test_unreachable_means_complete_disallow(self, status: int) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(status, None, None)})
        assert revision.availability == RobotsAvailability.UNREACHABLE
        verdict = evaluate(revision, url("/search"), UA)
        assert not verdict.allowed
        assert verdict.basis == RobotsBasis.ROBOTS_UNREACHABLE
        assert is_allowed(revision, url("/robots.txt"), UA)

    async def test_network_error(self) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(None, None, "timeout")})
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "timeout"
        assert not revision.parse_ok

    async def test_fetcher_exception(self) -> None:
        async def broken(target: str) -> RobotsFetchResponse:
            raise ConnectionResetError("boom")

        revision = await fetch_robots(POLICY, HOST, broken, resolver=None, clock=FrozenClock(NOW))
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "fetch_exception"

    async def test_hanging_fetcher_is_unreachable(self) -> None:
        async def hanging(target: str) -> RobotsFetchResponse:
            await anyio.sleep(30)
            return RobotsFetchResponse(200, b"", None)

        with anyio.fail_after(5):
            revision = await fetch_robots(
                POLICY, HOST, hanging, resolver=None, clock=FrozenClock(NOW), fetch_timeout_s=0.05
            )
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "timeout"
        assert not is_allowed(revision, url("/search"), UA)

    async def test_invalid_fetch_timeout(self) -> None:
        with pytest.raises(ValueError):
            await fetch_robots(POLICY, HOST, Fetcher({}), resolver=None, fetch_timeout_s=0)

    async def test_same_host_redirects_followed(self) -> None:
        responses: dict[str, object] = {
            ROBOTS: RobotsFetchResponse(301, None, None, "/robots-v2.txt"),
            url("/robots-v2.txt"): RobotsFetchResponse(302, None, None, f"https://{HOST}/r/robots.txt"),
            url("/r/robots.txt"): RobotsFetchResponse(200, b"User-agent: *\nDisallow: /x\n", None),
        }
        revision = await fetch(responses)
        assert revision.availability == RobotsAvailability.AVAILABLE
        assert revision.redirect_count == 2
        assert not is_allowed(revision, url("/x"), UA)

    async def test_more_than_five_hops_unavailable(self) -> None:
        responses: dict[str, object] = {ROBOTS: RobotsFetchResponse(301, None, None, "/r1")}
        for i in range(1, 7):
            responses[url(f"/r{i}")] = RobotsFetchResponse(301, None, None, f"/r{i + 1}")
        fetcher = Fetcher(responses)  # type: ignore[arg-type]
        revision = await fetch_robots(POLICY, HOST, fetcher, resolver=None, clock=FrozenClock(NOW))
        assert revision.availability == RobotsAvailability.UNAVAILABLE
        assert revision.error_kind == "too_many_redirects"
        assert len(fetcher.calls) == 6  # the original plus five followed hops

    @pytest.mark.parametrize(
        "location",
        [
            "https://cdn.dealer-example.com/robots.txt",
            "https://evil.example.net/robots.txt",
            "http://127.0.0.1/robots.txt",
            "http://www.dealer-example.com/robots.txt",
            "https://www.dealer-example.com:8443/robots.txt",
            "https://www.dealer-example.com:80/robots.txt",
            "javascript:alert(1)",
        ],
    )
    async def test_off_host_or_downgrade_redirect_unreachable(self, location: str) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(301, None, None, location)})
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "redirect_off_host"

    async def test_redirect_without_location(self) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(302, None, None, None)})
        assert revision.availability == RobotsAvailability.UNREACHABLE

    async def test_dns_rebinding_before_fetch(self) -> None:
        async def private(host: str, port: int) -> list[str]:
            return ["10.1.2.3"]

        fetcher = Fetcher({ROBOTS: RobotsFetchResponse(200, b"", None)})
        revision = await fetch_robots(POLICY, HOST, fetcher, resolver=private, clock=FrozenClock(NOW))
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "dns_non_public"
        assert fetcher.calls == []

    async def test_dns_rebinding_between_hops(self) -> None:
        answers = iter([["93.184.215.14"], ["127.0.0.1"]])

        async def flipping(host: str, port: int) -> list[str]:
            return next(answers)

        fetcher = Fetcher(
            {
                ROBOTS: RobotsFetchResponse(301, None, None, "/robots-v2.txt"),
                url("/robots-v2.txt"): RobotsFetchResponse(200, b"", None),
            }
        )
        revision = await fetch_robots(POLICY, HOST, fetcher, resolver=flipping, clock=FrozenClock(NOW))
        assert revision.availability == RobotsAvailability.UNREACHABLE
        assert revision.error_kind == "dns_non_public"
        assert fetcher.calls == [ROBOTS]

    async def test_not_allowed_host_refused(self) -> None:
        with pytest.raises(PolicyDenied):
            await fetch_robots(POLICY, "evil.example.net", Fetcher({}), resolver=None)

    async def test_oversize_body_truncated_at_500_kib(self) -> None:
        head = b"User-agent: *\nDisallow: /early\n"
        filler = b"# " + b"x" * 1000 + b"\n"
        body = head + filler * 600 + b"Disallow: /late\n"
        assert len(body) > ROBOTS_MAX_BYTES
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, body, None)})
        assert revision.body_truncated
        assert not is_allowed(revision, url("/early"), UA)
        assert is_allowed(revision, url("/late"), UA)  # beyond the parse limit
        assert revision.content_hash == hashlib.sha256(body[:ROBOTS_MAX_BYTES]).hexdigest()
        assert revision.body is not None and len(revision.body.encode()) <= ROBOTS_MAX_BYTES
        assert revision.body_excerpt is not None and len(revision.body_excerpt) <= BODY_EXCERPT_CHARS

    async def test_html_body_parses_to_no_rules(self) -> None:
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, b"<html><body>Welcome</body></html>", None)})
        assert revision.availability == RobotsAvailability.AVAILABLE
        assert not revision.parse_ok
        assert revision.invalid_lines == 1
        assert is_allowed(revision, url("/x"), UA)

    async def test_excerpt_strips_control_characters(self) -> None:
        revision = await fetch(
            {ROBOTS: RobotsFetchResponse(200, b"User-agent: *\x1b[31m\nDisallow: /a\x00\n", None)}
        )
        assert revision.body_excerpt is not None
        assert "\x1b" not in revision.body_excerpt and "\x00" not in revision.body_excerpt
        assert revision.body is not None and "\x00" not in revision.body

    async def test_invalid_utf8_stays_bounded(self) -> None:
        body = b"User-agent: *\n" + b"\xff" * (ROBOTS_MAX_BYTES - 20) + b"\n"
        revision = await fetch({ROBOTS: RobotsFetchResponse(200, body, None)})
        assert revision.body is not None
        assert len(revision.body.encode("utf-8")) <= ROBOTS_MAX_BYTES


class TestRevisionChanges:
    def test_first_revision_counts_as_change(self) -> None:
        assert revision_changed(None, revision_from(""))

    def test_identical(self) -> None:
        a = revision_from("User-agent: *\nDisallow: /x\n")
        b = a.model_copy(update={"fetched_at": NOW + timedelta(hours=6)})
        assert not revision_changed(a, b)

    def test_content_status_and_availability_changes(self) -> None:
        a = revision_from("User-agent: *\nDisallow: /x\n")
        assert revision_changed(a, revision_from("User-agent: *\nDisallow: /y\n"))
        assert revision_changed(a, a.model_copy(update={"http_status": 203}))
        assert revision_changed(a, a.model_copy(update={"availability": RobotsAvailability.UNREACHABLE}))


class TestRobotsUrl:
    def test_allowed_host(self) -> None:
        assert robots_url_for(POLICY, "WWW.Dealer-Example.com") == ROBOTS

    @pytest.mark.parametrize("host", ["evil.example.net", "127.0.0.1", "", "localhost"])
    def test_other_hosts_refused(self, host: str) -> None:
        with pytest.raises(PolicyDenied):
            robots_url_for(POLICY, host)
