"""Unit tests for the per-source URL policy (spec sections 8, 24)."""

from __future__ import annotations

import anyio
import pytest

from suv_deals.crawling.url_policy import (
    DenialReason,
    PolicyDenied,
    SourceUrlPolicy,
    host_of,
    redact_url,
)
from suv_deals.domain.enums import SourceMode
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import ErrorCode

HOST = "www.dealer-example.com"


def make_policy(**overrides: object) -> SourceUrlPolicy:
    kwargs: dict[str, object] = {
        "allowed_hosts": (HOST, "*.cdn-example.com"),
        "search_paths": (r"/suche(/[a-z0-9-]+)*", r"/search"),
        "detail_paths": (r"/fahrzeug/[0-9]{1,12}", r"/vehicles/[a-z0-9-]{1,80}"),
    }
    kwargs.update(overrides)
    return SourceUrlPolicy("dealer_example", **kwargs)  # type: ignore[arg-type]


def denied(policy: SourceUrlPolicy, url: str, purpose: str = "search") -> DenialReason:
    with pytest.raises(PolicyDenied) as info:
        policy.check(url, purpose)  # type: ignore[arg-type]
    return info.value.reason


class TestAllowed:
    def test_search_with_query(self) -> None:
        decision = make_policy().check(f"https://{HOST}/suche/suv?price_to=3000&sort=new", "search")
        assert decision.host == HOST
        assert decision.scheme == "https"
        assert decision.port == 443
        assert decision.path == "/suche/suv"
        assert decision.query == "price_to=3000&sort=new"
        assert decision.matched_rule.startswith("search:")
        assert decision.resolved_addresses == ()

    def test_detail(self) -> None:
        decision = make_policy().check(f"https://{HOST}/fahrzeug/12345", "detail")
        assert decision.matched_rule == "detail:/fahrzeug/[0-9]{1,12}"

    def test_host_case_and_trailing_dot_normalised(self) -> None:
        decision = make_policy().check("https://WWW.Dealer-Example.COM./search", "search")
        assert decision.host == HOST

    def test_explicit_default_port_accepted(self) -> None:
        assert make_policy().check(f"https://{HOST}:443/search", "search").port == 443

    def test_matrix_parameters_on_normal_segments_allowed(self) -> None:
        policy = make_policy(search_paths=(r"/suche/[a-z0-9;=]+",))
        assert policy.check(f"https://{HOST}/suche/suv;page=2", "search").path == "/suche/suv;page=2"

    def test_fragment_does_not_affect_path(self) -> None:
        assert make_policy().check(f"https://{HOST}/search#top", "search").path == "/search"

    def test_robots(self) -> None:
        assert make_policy().check(f"https://{HOST}/robots.txt", "robots").matched_rule == "robots"

    def test_wildcard_subdomains(self) -> None:
        policy = make_policy()
        assert policy.check("https://img.cdn-example.com/search", "search").host == "img.cdn-example.com"
        assert policy.check("https://a.b.cdn-example.com/search", "search").host == "a.b.cdn-example.com"

    @pytest.mark.parametrize("path", ["/suche/x", "/fahrzeug/1", "/robots.txt"])
    def test_diagnostic_allows_reviewed_routes(self, path: str) -> None:
        assert make_policy().check(f"https://{HOST}{path}", "diagnostic").purpose == "diagnostic"

    def test_http_only_when_configured(self) -> None:
        policy = make_policy(allowed_schemes=("https", "http"))
        decision = policy.check(f"http://{HOST}/search", "search")
        assert (decision.scheme, decision.port) == ("http", 80)

    def test_from_source_config(self) -> None:
        cfg = SourceConfig(
            source_key="dealer_example",
            display_name="Dealer example",
            country="DE",
            role="acquisition",
            mode=SourceMode.PUBLIC_HTML,
            adapter="dealer_inventory",
            adapter_version="0.1.0",
            allowed_hosts=(HOST,),
            allowed_search_paths=(r"/search",),
            allowed_detail_paths=(r"/fahrzeug/[0-9]+",),
        )
        policy = SourceUrlPolicy.from_source(cfg)
        assert policy.source_key == "dealer_example"
        assert policy.check(f"https://{HOST}/fahrzeug/9", "detail").source_key == "dealer_example"
        assert policy.allowed_schemes == ("https",)


class TestDenied:
    def test_wrong_purpose_path(self) -> None:
        assert (
            denied(make_policy(), f"https://{HOST}/fahrzeug/12345", "search") == DenialReason.PATH_NOT_ALLOWED
        )

    @pytest.mark.parametrize(
        "path",
        ["/fahrzeug/123/extra", "/x/fahrzeug/123", "/fahrzeug/", "/fahrzeug/1234567890123", "/admin"],
    )
    def test_patterns_are_anchored(self, path: str) -> None:
        assert denied(make_policy(), f"https://{HOST}{path}", "detail") == DenialReason.PATH_NOT_ALLOWED

    @pytest.mark.parametrize("url", [f"https://{HOST}/robots.txt?x=1", f"https://{HOST}/robots.txt/"])
    def test_robots_only_exact(self, url: str) -> None:
        assert denied(make_policy(), url, "robots") == DenialReason.PATH_NOT_ALLOWED

    def test_robots_path_not_a_search(self) -> None:
        assert denied(make_policy(), f"https://{HOST}/robots.txt", "search") == DenialReason.PATH_NOT_ALLOWED

    def test_diagnostic_other_path(self) -> None:
        assert denied(make_policy(), f"https://{HOST}/admin", "diagnostic") == DenialReason.PATH_NOT_ALLOWED

    def test_http_denied_by_default(self) -> None:
        assert denied(make_policy(), f"http://{HOST}/search") == DenialReason.SCHEME_NOT_ALLOWED

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://www.dealer-example.com/search",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "raw:<html></html>",
            "raw://<html></html>",
            "//www.dealer-example.com/search",
            "www.dealer-example.com/search",
        ],
    )
    def test_unsupported_schemes(self, url: str) -> None:
        assert denied(make_policy(), url) == DenialReason.SCHEME_NOT_ALLOWED

    @pytest.mark.parametrize(
        "url",
        [
            f"https://user:pass@{HOST}/search",
            f"https://user@{HOST}/search",
            f"https://{HOST}@evil.example.net/search",
            f"https://:@{HOST}/search",
        ],
    )
    def test_embedded_credentials(self, url: str) -> None:
        assert denied(make_policy(), url) == DenialReason.EMBEDDED_CREDENTIALS

    @pytest.mark.parametrize("port", ["80", "8443", "22", "0", "99999", "abc"])
    def test_non_default_ports(self, port: str) -> None:
        assert denied(make_policy(), f"https://{HOST}:{port}/search") == DenialReason.PORT_NOT_ALLOWED

    @pytest.mark.parametrize(
        "host",
        [
            "dealer-example.com",
            "www.dealer-example.com.evil.example.net",
            "evilwww.dealer-example.com",
            "cdn-example.com",
            "xcdn-example.com",
            "www.dealer-example.co",
        ],
    )
    def test_host_not_allowed(self, host: str) -> None:
        assert denied(make_policy(), f"https://{host}/search") == DenialReason.HOST_NOT_ALLOWED

    def test_public_ip_literal_never_allowed(self) -> None:
        assert denied(make_policy(), "https://93.184.215.14/search") == DenialReason.IP_LITERAL_HOST
        assert denied(make_policy(), "https://[2606:4700:4700::1111]/search") == DenialReason.IP_LITERAL_HOST

    def test_too_long(self) -> None:
        url = f"https://{HOST}/search?q=" + "a" * 2100
        assert denied(make_policy(), url) == DenialReason.URL_TOO_LONG

    def test_custom_max_length(self) -> None:
        policy = make_policy(max_url_length=100)
        assert denied(policy, f"https://{HOST}/search?q=" + "a" * 80) == DenialReason.URL_TOO_LONG

    @pytest.mark.parametrize(
        "path",
        [
            "/suche/../admin",
            "/suche/%2e%2e/admin",
            "/suche/%2E%2e/admin",
            "/suche/./x",
            "/suche/%2e/x",
            "/suche%2fadmin",
            "/suche%5Cadmin",
            "/suche/%00",
            "/suche/..;/admin",
            "/suche/..;jsessionid=x/admin",
            "/suche/.;/x",
            "/suche/%2e%2e;/admin",
        ],
    )
    def test_ambiguous_paths(self, path: str) -> None:
        assert denied(make_policy(), f"https://{HOST}{path}") == DenialReason.AMBIGUOUS_PATH

    @pytest.mark.parametrize(
        "url",
        [
            f"https://{HOST}/search q",
            f"https://{HOST}/search\n",
            f"https://{HOST}/search\t",
            f"https://{HOST}/süche",
            "https://www.déaler-example.com/search",
            f"https://{HOST}\\search",
            "",
        ],
    )
    def test_malformed(self, url: str) -> None:
        assert denied(make_policy(), url) == DenialReason.MALFORMED_URL

    def test_unknown_purpose(self) -> None:
        assert denied(make_policy(), f"https://{HOST}/search", "admin") == DenialReason.PURPOSE_NOT_ALLOWED

    def test_no_hosts_configured_denies_everything(self) -> None:
        policy = SourceUrlPolicy("dealer_example", allowed_hosts=(), search_paths=(r"/search",))
        assert denied(policy, f"https://{HOST}/search") == DenialReason.HOST_NOT_ALLOWED

    def test_no_search_paths_denies_search(self) -> None:
        policy = make_policy(search_paths=())
        assert denied(policy, f"https://{HOST}/search") == DenialReason.PATH_NOT_ALLOWED

    def test_error_is_typed_and_does_not_echo_url(self) -> None:
        with pytest.raises(PolicyDenied) as info:
            make_policy().check("https://secret-user:secret-pass@www.dealer-example.com/search", "search")
        error = info.value
        assert error.code == ErrorCode.FORBIDDEN
        assert error.retryable is False
        assert not error.transient
        assert "secret" not in error.message
        assert error.to_payload()["details"] == {"reason": "embedded_credentials", "stage": "request"}


class TestConfigValidation:
    @pytest.mark.parametrize(
        "hosts",
        [
            ("93.184.215.14",),
            ("localhost",),
            ("*.com",),
            ("*",),
            ("www.example.com/path",),
            ("www.example.com:8443",),
            ("user@www.example.com",),
            ("a.*.example.com",),
            ("",),
            ("intranet",),
            ("printer.local",),
        ],
    )
    def test_unsafe_host_entries(self, hosts: tuple[str, ...]) -> None:
        with pytest.raises(ValueError):
            make_policy(allowed_hosts=hosts)

    def test_invalid_regex(self) -> None:
        with pytest.raises(ValueError, match="invalid detail path pattern"):
            make_policy(detail_paths=("/fahrzeug/[0-9",))

    def test_empty_pattern(self) -> None:
        with pytest.raises(ValueError, match="empty search path pattern"):
            make_policy(search_paths=("  ",))

    @pytest.mark.parametrize("schemes", [(), ("ftp",), ("https", "file")])
    def test_invalid_schemes(self, schemes: tuple[str, ...]) -> None:
        with pytest.raises(ValueError):
            make_policy(allowed_schemes=schemes)

    def test_invalid_max_length(self) -> None:
        with pytest.raises(ValueError):
            make_policy(max_url_length=5000)

    def test_repr_has_no_patterns_or_secrets(self) -> None:
        assert "dealer_example" in repr(make_policy())


class TestRedirects:
    def test_same_url(self) -> None:
        url = f"https://{HOST}/fahrzeug/1"
        assert make_policy().validate_redirect(url, url, "detail").url == url
        assert make_policy().validate_redirect(url, None, "detail").url == url

    def test_same_host_allowed_path(self) -> None:
        final = make_policy().validate_redirect(
            f"https://{HOST}/fahrzeug/1", f"https://{HOST}/fahrzeug/2", "detail"
        )
        assert final.path == "/fahrzeug/2"

    def test_cross_host_denied_even_when_allowed(self) -> None:
        policy = make_policy(allowed_hosts=(HOST, "dealer-example.com"))
        with pytest.raises(PolicyDenied) as info:
            policy.validate_redirect(f"https://{HOST}/search", "https://dealer-example.com/search", "search")
        assert info.value.reason == DenialReason.CROSS_HOST_REDIRECT
        assert info.value.stage == "redirect"

    @pytest.mark.parametrize(
        "final",
        [
            "https://evil.example.net/search",
            "http://127.0.0.1/search",
            "http://169.254.169.254/latest/meta-data/",
            "https://[::1]/search",
        ],
    )
    def test_redirect_to_other_or_private_host(self, final: str) -> None:
        with pytest.raises(PolicyDenied) as info:
            make_policy().validate_redirect(f"https://{HOST}/search", final, "search")
        assert info.value.reason == DenialReason.CROSS_HOST_REDIRECT

    def test_redirect_path_outside_allowlist(self) -> None:
        with pytest.raises(PolicyDenied) as info:
            make_policy().validate_redirect(f"https://{HOST}/fahrzeug/1", f"https://{HOST}/login", "detail")
        assert info.value.reason == DenialReason.PATH_NOT_ALLOWED
        assert info.value.stage == "redirect"

    def test_scheme_downgrade(self) -> None:
        policy = make_policy(allowed_schemes=("https", "http"))
        with pytest.raises(PolicyDenied) as info:
            policy.validate_redirect(f"https://{HOST}/search", f"http://{HOST}/search", "search")
        assert info.value.reason == DenialReason.SCHEME_DOWNGRADE

    def test_redirect_to_raw_or_javascript(self) -> None:
        for final in ("javascript:alert(1)", "raw:<html/>", "chrome-error://chromewebdata/"):
            with pytest.raises(PolicyDenied):
                make_policy().validate_redirect(f"https://{HOST}/search", final, "search")

    def test_original_must_pass(self) -> None:
        with pytest.raises(PolicyDenied):
            make_policy().validate_redirect(
                "https://evil.example.net/search", f"https://{HOST}/search", "search"
            )


class TestDns:
    async def test_public_answers_recorded(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            assert (host, port) == (HOST, 443)
            return ["93.184.215.14", "2606:4700:4700::1111"]

        decision = await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver)
        assert decision.resolved_addresses == ("93.184.215.14", "2606:4700:4700::1111")

    @pytest.mark.parametrize(
        "answers",
        [
            ["127.0.0.1"],
            ["10.0.0.8"],
            ["93.184.215.14", "192.168.1.1"],
            ["::1"],
            ["169.254.169.254"],
            ["fe80::1"],
        ],
    )
    async def test_rebinding_to_private_rejected(self, answers: list[str]) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            return answers

        with pytest.raises(PolicyDenied) as info:
            await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver)
        assert info.value.reason == DenialReason.DNS_NON_PUBLIC
        assert info.value.stage == "dns"
        assert not info.value.transient

    async def test_lookup_failure_is_transient(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            raise OSError("SERVFAIL")

        with pytest.raises(PolicyDenied) as info:
            await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver)
        assert info.value.reason == DenialReason.DNS_RESOLUTION_FAILED
        assert info.value.transient
        assert info.value.retryable

    async def test_empty_answer_is_lookup_failure(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            return []

        with pytest.raises(PolicyDenied) as info:
            await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver)
        assert info.value.reason == DenialReason.DNS_RESOLUTION_FAILED

    async def test_non_ip_answer_rejected(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            return ["not-an-ip"]

        with pytest.raises(PolicyDenied) as info:
            await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver)
        assert info.value.reason == DenialReason.DNS_NON_PUBLIC

    async def test_slow_resolver_times_out(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            await anyio.sleep(5)
            return ["93.184.215.14"]

        with pytest.raises(PolicyDenied) as info:
            await make_policy().resolve_check(f"https://{HOST}/search", "search", resolver, timeout_s=0.05)
        assert info.value.reason == DenialReason.DNS_RESOLUTION_FAILED

    async def test_structural_check_runs_before_dns(self) -> None:
        called = False

        async def resolver(host: str, port: int) -> list[str]:
            nonlocal called
            called = True
            return ["93.184.215.14"]

        with pytest.raises(PolicyDenied):
            await make_policy().resolve_check("https://evil.example.net/search", "search", resolver)
        assert called is False


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://user:pw@www.example.com/a?b=1", "https://www.example.com/a?b=1"),
            ("https://www.example.com/a\r\nb", "https://www.example.com/ab"),
            (None, "[invalid-url]"),
            ("", "[empty-url]"),
        ],
    )
    def test_redact_url(self, url: object, expected: str) -> None:
        assert redact_url(url) == expected

    def test_redact_url_bounded(self) -> None:
        assert len(redact_url("https://www.example.com/" + "a" * 5000)) == 2048

    def test_host_of(self) -> None:
        assert host_of("https://WWW.Example.com/x") == "www.example.com"
        assert host_of("raw:<html/>") is None
