"""Outbound destination safety shared by crawling, robots fetches and webhook delivery.

SSRF rules (spec section 24): only public unicast destinations. Reject loopback,
private, link-local, CGNAT, multicast, reserved, unspecified, documentation and
metadata addresses (IPv4 and IPv6, including IPv4-mapped/compatible/6to4/NAT64
forms), unusual numeric host spellings, embedded credentials and non-HTTP(S)
schemes. DNS answers are validated at connection time by the caller using
`resolve_public()`, and the caller connects to the validated IP while keeping
the hostname for TLS SNI/certificate verification.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

import anyio

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_EXTRA_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "169.254.0.0/16",  # link-local incl. 169.254.169.254 cloud metadata
    )
)
_EXTRA_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",  # IPv4-mapped: checked via embedded v4 as well
        "64:ff9b::/96",  # NAT64: checked via embedded v4
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",  # 6to4: checked via embedded v4
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
        "fd00:ec2::254/128",  # AWS IMDS over IPv6
    )
)

# Hostnames that must never be contacted regardless of DNS.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class UnsafeDestination(ValueError):
    """The destination violates the outbound safety policy."""


@dataclass(frozen=True, slots=True)
class SafeTarget:
    url: str
    scheme: str
    hostname: str  # lowercase IDNA hostname
    port: int
    path_query: str


def is_public_address(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_address(ip.sixtofour)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_public_address(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.teredo is not None:
            return False
        if any(ip in net for net in _EXTRA_BLOCKED_V6):
            return False
    elif any(ip in net for net in _EXTRA_BLOCKED_V4):
        return False
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _looks_numeric_host(host: str) -> bool:
    """True for any purely numeric/hex/octal spelling that some resolvers treat as an IP."""
    labels = host.split(".")
    return all(re.fullmatch(r"(0x[0-9a-f]*|[0-9]+)", label or "x") for label in labels)


def normalize_hostname(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UnsafeDestination("empty host")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeDestination("invalid internationalised host") from exc
    return ascii_host


def check_host_literal(host: str) -> IPAddress | None:
    """Return the IP for a literal-address host (after safety checks) or None for a DNS name."""
    raw = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        if _looks_numeric_host(raw.lower()):
            # 2130706433, 0177.0.0.1, 0x7f.1 and similar alternative IPv4 spellings.
            raise UnsafeDestination("unusual numeric host representation") from None
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        raise UnsafeDestination("scoped IPv6 address")
    if not is_public_address(ip):
        raise UnsafeDestination("non-public IP address")
    return ip


def parse_safe_url(
    url: str,
    *,
    allowed_schemes: Iterable[str] = ("https", "http"),
    allowed_ports: Iterable[int] = (80, 443),
) -> SafeTarget:
    """Structural URL validation (no DNS). Raises UnsafeDestination."""
    if len(url) > 2048:
        raise UnsafeDestination("URL too long")
    if any(ch in url for ch in ("\r", "\n", "\t", "\x00", " ", "\\")):
        raise UnsafeDestination("URL contains forbidden characters")
    try:
        parts: SplitResult = urlsplit(url)
    except ValueError as exc:
        raise UnsafeDestination("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in set(allowed_schemes):
        raise UnsafeDestination(f"scheme {scheme or '(none)'} not allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeDestination("embedded credentials are not allowed")
    if parts.hostname is None:
        raise UnsafeDestination("missing host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeDestination("invalid port") from exc
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in set(allowed_ports):
        raise UnsafeDestination(f"port {effective_port} not allowed")
    hostname = normalize_hostname(parts.hostname)
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith((".localhost", ".internal", ".local")):
        raise UnsafeDestination("blocked hostname")
    literal = check_host_literal(hostname)
    if literal is None:
        labels = hostname.split(".")
        if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
            raise UnsafeDestination("host is not a fully qualified public DNS name")
    path_query = parts.path or "/"
    if parts.query:
        path_query = f"{path_query}?{parts.query}"
    return SafeTarget(url=url, scheme=scheme, hostname=hostname, port=effective_port, path_query=path_query)


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def resolve_public(host: str, port: int, resolver: Resolver = system_resolver) -> list[IPAddress]:
    """Resolve and require that EVERY answer is public (defeats mixed-answer rebinding)."""
    literal = check_host_literal(host)
    if literal is not None:
        return [literal]
    try:
        answers = await resolver(host, port)
    except OSError as exc:
        raise UnsafeDestination("DNS resolution failed") from exc
    if not answers:
        raise UnsafeDestination("DNS returned no addresses")
    ips: list[IPAddress] = []
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeDestination("resolver returned a non-IP answer") from exc
        if not is_public_address(ip):
            raise UnsafeDestination("host resolves to a non-public address")
        ips.append(ip)
    return ips
