"""Adversarial SSRF tests for the shared outbound destination guard."""

from __future__ import annotations

import ipaddress

import pytest

from suv_deals.netguard import (
    UnsafeDestination,
    is_public_address,
    parse_safe_url,
    resolve_public,
)

BLOCKED_URLS = [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://LOCALHOST./x",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://127.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:1::]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[fd00:ec2::254]/",
    "http://metadata.google.internal/",
    "http://10.0.0.5/",
    "http://172.16.3.4/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",
    "http://0.0.0.0/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "https://user:pass@example.com/",
    "https://example.com@127.0.0.1/",
    "file:///etc/passwd",
    "data:text/html,hi",
    "javascript:alert(1)",
    "gopher://example.com/",
    "https://example.com:8080/",
    "https://example.com:22/",
    "https://intranet/",
    "https://foo.internal/",
    "https://printer.local/",
    "https://exa mple.com/",
    "https://example.com/\r\nHost: evil",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_blocked_urls(url: str) -> None:
    with pytest.raises(UnsafeDestination):
        parse_safe_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://www.example.com/a?b=c", "http://dealer.example.org/vehicles/1", "https://93.184.215.14/"],
)
def test_allowed_urls(url: str) -> None:
    target = parse_safe_url(url)
    assert target.hostname == target.hostname.lower()


def test_https_only_option() -> None:
    with pytest.raises(UnsafeDestination):
        parse_safe_url("http://example.com/", allowed_schemes=("https",))


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("192.0.2.1", False),
        ("198.18.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("2001:db8::1", False),
        ("224.0.0.1", False),
    ],
)
def test_is_public_address(ip: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(ip)) is public


async def test_resolve_public_rejects_mixed_answers() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.215.14", "127.0.0.1"]

    with pytest.raises(UnsafeDestination):
        await resolve_public("rebind.example.com", 443, resolver)


async def test_resolve_public_accepts_public_answers() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    ips = await resolve_public("example.com", 443, resolver)
    assert [str(i) for i in ips] == ["93.184.215.14"]


async def test_resolve_public_rejects_resolution_failure() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(UnsafeDestination):
        await resolve_public("nope.example.com", 443, resolver)
