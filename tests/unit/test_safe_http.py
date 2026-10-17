"""SSRF-safe outbound client: IP pinning with SNI/Host, no redirects, bounds, typed failures."""

from __future__ import annotations

import asyncio
import ipaddress
import pathlib
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import anyio
import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from suv_deals.integrations.safe_http import (
    HttpLimits,
    SafeHttpClient,
    SafeHttpError,
    SafeHttpFailure,
    _SendTracer,
)
from suv_deals.netguard import SafeTarget

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_B = "93.184.216.35"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
URL = "https://receiver.example.com/mcp-events/callback_123?x=1"

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def resolver_for(*answers: str) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, port: int) -> list[str]:
        return list(answers)

    return resolve


def client(handler: Handler, *answers: str, limits: HttpLimits | None = None) -> SafeHttpClient:
    return SafeHttpClient(
        resolver=resolver_for(*(answers or (PUBLIC_V4,))),
        transport=httpx.MockTransport(handler),
        limits=limits,
    )


async def test_connects_to_validated_ip_with_hostname_for_sni_and_host() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "a=b", "retry-after": "5"})

    async with client(handler) as http:
        response = await http.post(URL, content=b"{}", headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.is_success
    assert response.body == b'{"ok":true}'
    assert response.pinned_ip == PUBLIC_V4
    assert response.headers == {
        "content-type": "application/json",
        "content-length": "11",
        "retry-after": "5",
    }
    request = seen[0]
    assert request.url.host == PUBLIC_V4
    assert request.url.raw_path == b"/mcp-events/callback_123?x=1"
    assert request.headers["host"] == "receiver.example.com"
    assert request.extensions["sni_hostname"] == "receiver.example.com"
    assert request.headers["accept-encoding"] == "identity"
    assert "trace" in request.extensions


async def test_ipv6_answer_is_bracketed() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with client(handler, PUBLIC_V6) as http:
        response = await http.post(URL, content=b"{}", headers={})
    assert response.status_code == 204
    assert seen[0].url.host == PUBLIC_V6
    assert response.pinned_ip == PUBLIC_V6


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_refused_not_followed(status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    async with client(handler) as http:
        with pytest.raises(SafeHttpError) as info:
            await http.post(URL, content=b"{}", headers={})
    assert info.value.failure is SafeHttpFailure.REDIRECT_REFUSED
    assert info.value.status_code == status
    assert calls == 1
    assert "169.254" not in str(info.value) and "receiver" not in repr(info.value)


async def test_response_body_is_bounded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10_000)

    async with client(handler, limits=HttpLimits(max_response_bytes=100)) as http:
        response = await http.get(URL)
    assert len(response.body) == 100
    assert response.truncated
    async with client(handler) as http:
        small = await http.get(URL, max_response_bytes=10_000)
    assert len(small.body) == 10_000
    assert not small.truncated


@pytest.mark.parametrize("bad", [0, -1])
async def test_explicit_non_positive_response_limit_is_rejected_not_defaulted(bad: int) -> None:
    # Regression: `max_response_bytes=0` used to fall back silently to the 64 KiB default.
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, content=b"x" * 100)

    async with client(handler) as http:
        with pytest.raises(ValueError):
            await http.post(URL, content=b"{}", headers={}, max_response_bytes=bad)
        with pytest.raises(ValueError):
            await http.post(URL, content=b"{}", headers={}, timeout_s=0)


async def test_request_body_is_bounded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200)

    async with client(handler, limits=HttpLimits(max_request_bytes=10)) as http:
        with pytest.raises(SafeHttpError) as info:
            await http.post(URL, content=b"x" * 11, headers={})
    assert info.value.failure is SafeHttpFailure.REQUEST_TOO_LARGE


@pytest.mark.parametrize("name", ["Host", "host", "Connection", "Transfer-Encoding", "Proxy-Authorization"])
async def test_caller_cannot_override_host_or_hop_by_hop_headers(name: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    async with client(handler) as http:
        with pytest.raises(ValueError):
            await http.post(URL, content=b"{}", headers={name: "evil.example"})


async def test_unsupported_method_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    async with client(handler) as http:
        with pytest.raises(ValueError):
            await http.request("DELETE", URL)


@pytest.mark.parametrize(
    ("exc", "failure", "possibly_delivered"),
    [
        (httpx.ConnectTimeout("t"), SafeHttpFailure.TIMEOUT_BEFORE_SEND, False),
        (httpx.PoolTimeout("t"), SafeHttpFailure.TIMEOUT_BEFORE_SEND, False),
        (httpx.WriteTimeout("t"), SafeHttpFailure.TIMEOUT_BEFORE_SEND, False),
        (httpx.ReadTimeout("t"), SafeHttpFailure.TIMEOUT_AFTER_SEND, True),
        (httpx.ConnectError("refused"), SafeHttpFailure.CONNECT_FAILED, False),
        (httpx.WriteError("broken"), SafeHttpFailure.CONNECT_FAILED, False),
        (httpx.ReadError("reset"), SafeHttpFailure.CONNECTION_LOST, True),
        (httpx.RemoteProtocolError("bad"), SafeHttpFailure.PROTOCOL_ERROR, True),
        (httpx.UnsupportedProtocol("x"), SafeHttpFailure.DESTINATION_REJECTED, False),
    ],
)
async def test_transport_errors_are_classified(
    exc: Exception, failure: SafeHttpFailure, possibly_delivered: bool
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with client(handler) as http:
        with pytest.raises(SafeHttpError) as info:
            await http.post(URL, content=b"{}", headers={})
    assert info.value.failure is failure
    assert info.value.possibly_delivered is possibly_delivered


async def test_tls_failure_is_distinguished_from_connection_refused() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: Hostname mismatch")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]") from cause

    async with client(handler) as http:
        with pytest.raises(SafeHttpError) as info:
            await http.post(URL, content=b"{}", headers={})
    assert info.value.failure is SafeHttpFailure.TLS_FAILED


async def test_falls_back_to_next_validated_ip_only_before_sending() -> None:
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == PUBLIC_V4:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    async with client(handler, PUBLIC_V4, PUBLIC_V4_B) as http:
        response = await http.post(URL, content=b"{}", headers={})
    assert hosts == [PUBLIC_V4, PUBLIC_V4_B]
    assert response.pinned_ip == PUBLIC_V4_B


async def test_no_fallback_after_request_may_have_been_sent() -> None:
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        raise httpx.ReadTimeout("slow")

    async with client(handler, PUBLIC_V4, PUBLIC_V4_B) as http:
        with pytest.raises(SafeHttpError):
            await http.post(URL, content=b"{}", headers={})
    assert hosts == [PUBLIC_V4]


async def test_total_deadline_without_send_evidence_is_conservatively_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(5)
        return httpx.Response(200)  # pragma: no cover

    async with client(handler) as http:
        with pytest.raises(SafeHttpError) as info:
            await http.post(URL, content=b"{}", headers={}, timeout_s=0.05)
    assert info.value.failure is SafeHttpFailure.TIMEOUT_AFTER_SEND
    assert info.value.possibly_delivered


async def test_slow_dns_counts_as_timeout_before_send() -> None:
    async def slow_resolver(host: str, port: int) -> list[str]:
        await anyio.sleep(5)
        return [PUBLIC_V4]  # pragma: no cover

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    http = SafeHttpClient(resolver=slow_resolver, transport=httpx.MockTransport(handler))
    with pytest.raises(SafeHttpError) as info:
        await http.post(URL, content=b"{}", headers={}, timeout_s=0.05)
    await http.aclose()
    assert info.value.failure is SafeHttpFailure.TIMEOUT_BEFORE_SEND
    assert not info.value.possibly_delivered


def test_limits_validation() -> None:
    with pytest.raises(ValueError):
        HttpLimits(connect_timeout_s=0)
    with pytest.raises(ValueError):
        HttpLimits(max_response_bytes=0)


async def test_default_transport_ignores_environment_proxy_and_verifies_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    http = SafeHttpClient()
    try:
        inner = http._client
        assert inner.trust_env is False
        assert inner.follow_redirects is False
        transport = inner._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        pool = transport._pool
        assert type(pool) is httpcore.AsyncConnectionPool  # not an HTTP/SOCKS proxy pool
        assert pool._max_keepalive_connections == 0
        ctx = pool._ssl_context
        assert ctx is not None
        assert ctx.verify_mode is ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
    finally:
        await http.aclose()


async def test_explicit_proxy_is_only_used_when_passed() -> None:
    http = SafeHttpClient(proxy="http://egress-proxy.example.net:3128")
    try:
        pool = http._client._transport._pool  # type: ignore[attr-defined]
        assert type(pool) is httpcore.AsyncHTTPProxy
    finally:
        await http.aclose()


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, size: int) -> None:
        self.chunks = chunks
        self.size = size
        self.yielded = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(self.chunks):
            self.yielded += 1
            yield b"y" * self.size


async def test_streaming_body_stops_reading_at_limit() -> None:
    stream = _ChunkedStream(chunks=100, size=1000)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with client(handler, limits=HttpLimits(max_response_bytes=2500)) as http:
        response = await http.get(URL)
    assert len(response.body) == 2500
    assert response.truncated
    assert stream.yielded == 3


# --------------------------------------------------------------------------- real TLS: SNI pinning


def _self_signed(hostname: str) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert_pem, key_pem


async def _tls_attempt(cert_host: str, target_host: str, tmp_path: object) -> tuple[object, list[str]]:
    """Run one real TLS request to 127.0.0.1 through the private pinned-IP attempt path."""
    assert isinstance(tmp_path, pathlib.Path)
    cert_pem, key_pem = _self_signed(cert_host)
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    sni_seen: list[str] = []
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(cert_file, key_file)
    server_ctx.sni_callback = lambda sock, name, ctx: sni_seen.append(name or "")  # type: ignore[assignment]

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    client_ctx = ssl.create_default_context(cadata=cert_pem.decode())
    http = SafeHttpClient(ssl_context=client_ctx)
    target = SafeTarget(
        url=f"https://{target_host}:{port}/cb",
        scheme="https",
        hostname=target_host,
        port=port,
        path_query="/cb",
    )
    try:
        result: object = await http._attempt(
            "POST",
            f"https://127.0.0.1:{port}/cb",
            ip=ipaddress.ip_address("127.0.0.1"),
            target=target,
            content=b"{}",
            headers={"Host": target_host},
            limit=1000,
            started=time.monotonic(),
            tracer=_SendTracer(),
        )
    except SafeHttpError as exc:
        result = exc
    finally:
        await http.aclose()
        server.close()
        await server.wait_closed()
    return result, sni_seen


async def test_real_tls_uses_hostname_for_sni_and_certificate_while_dialing_ip(tmp_path: object) -> None:
    result, sni_seen = await _tls_attempt("receiver.example.com", "receiver.example.com", tmp_path)
    assert not isinstance(result, SafeHttpError), result
    assert result.status_code == 200  # type: ignore[attr-defined]
    assert result.body == b"ok"  # type: ignore[attr-defined]
    assert result.pinned_ip == "127.0.0.1"  # type: ignore[attr-defined]
    assert sni_seen == ["receiver.example.com"]


async def test_real_tls_rejects_certificate_for_another_hostname(tmp_path: object) -> None:
    result, sni_seen = await _tls_attempt("attacker.example.org", "receiver.example.com", tmp_path)
    assert isinstance(result, SafeHttpError)
    assert result.failure is SafeHttpFailure.TLS_FAILED
    assert sni_seen == ["receiver.example.com"]


# --------------------------------------------------------------------------- real sockets: send evidence


async def _plain_server_attempt(
    behaviour: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limits: HttpLimits,
    timeout_s: float | None = None,
) -> SafeHttpError:
    """POST over a real local socket (plain HTTP) so httpcore emits its real trace events.

    DNS is pinned to 127.0.0.1 by patching the module's resolver hook; everything else
    (URL policy, tracer, timeouts, classification) runs unmodified.
    """

    async def loopback(host: str, port: int, resolver: object = None) -> list[ipaddress.IPv4Address]:
        return [ipaddress.IPv4Address("127.0.0.1")]

    monkeypatch.setattr("suv_deals.integrations.safe_http.resolve_public", loopback)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            await reader.readexactly(2)  # the b"{}" body: the request is completely received
            if behaviour == "status_then_reset":
                writer.write(b"HTTP/1.1 410 Gone\r\nContent-Length: 100\r\n\r\npartial")
                await writer.drain()
                writer.transport.abort()
            elif behaviour == "silent":
                await reader.read()  # never answer; return once the client gives up and closes
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    http = SafeHttpClient(allowed_schemes=("http",), allowed_ports=(port,), limits=limits)
    try:
        with pytest.raises(SafeHttpError) as info:
            await http.post(
                f"http://receiver.example.com:{port}/cb", content=b"{}", headers={}, timeout_s=timeout_s
            )
    finally:
        await http.aclose()
        server.close()
        await server.wait_closed()
    return info.value


async def test_real_read_timeout_after_complete_send_is_possibly_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = await _plain_server_attempt("silent", monkeypatch, limits=HttpLimits(read_timeout_s=0.2))
    assert err.failure is SafeHttpFailure.TIMEOUT_AFTER_SEND
    assert err.possibly_delivered
    assert err.status_code is None


async def test_real_total_deadline_after_send_uses_trace_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    err = await _plain_server_attempt(
        "silent", monkeypatch, limits=HttpLimits(read_timeout_s=5), timeout_s=0.2
    )
    assert err.failure is SafeHttpFailure.TIMEOUT_AFTER_SEND
    assert err.possibly_delivered
    assert err.detail == "total_deadline"


async def test_real_status_line_then_reset_keeps_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # The receiver's answer (410) is known even though the body never completed; callers
    # classify by it instead of treating the delivery as uncertain.
    err = await _plain_server_attempt("status_then_reset", monkeypatch, limits=HttpLimits())
    assert err.status_code == 410
    assert err.possibly_delivered
    assert err.failure in {SafeHttpFailure.PROTOCOL_ERROR, SafeHttpFailure.CONNECTION_LOST}
