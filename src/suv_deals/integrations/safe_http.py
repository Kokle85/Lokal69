"""SSRF-safe outbound HTTP for callbacks (and later robots/FX fetches).

Every request:

1. validates the URL structurally with `netguard.parse_safe_url` (callbacks:
   `https` on port 443 only; no fragments);
2. resolves DNS at connection time with `netguard.resolve_public` (injectable
   resolver) and requires that *every* answer is public, which defeats
   mixed-answer DNS rebinding;
3. connects to the validated IP while keeping the original hostname for TLS
   SNI/certificate verification (`extensions={"sni_hostname": host}`) and the
   `Host` header. TLS is always verified; there is no `verify=False` path;
4. never follows redirects: any 3xx raises `SafeHttpError(REDIRECT_REFUSED)`;
5. applies connect/read/write/pool timeouts plus a total deadline, bounds the
   request body and reads at most `max_response_bytes` of the response;
6. disables connection keep-alive so a TLS session established for one
   hostname is never reused for another hostname on the same IP.

Environment proxies are ignored (`trust_env=False`). The dev container sets an
HTTPS proxy in the environment; callback delivery must not silently route
through it. A proxy is used only when passed explicitly. With an explicit HTTP
CONNECT proxy, httpcore 1.0.9 uses the URL host for SNI, so IP pinning is not
possible: the request is sent to the hostname, DNS is still validated locally
before sending (defence in depth, not a pin), and the proxy itself MUST enforce
the egress policy. `SafeResponse.pinned_ip` is None in that mode.

Failures raise `SafeHttpError` with a `failure` kind and `possibly_delivered`,
which callers use to separate retryable failures from "the receiver may have
accepted the request" (spec section 13: uncertain, do not blindly resend).
Error messages never contain the URL, headers or body.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Protocol

import anyio
import httpx

from suv_deals.netguard import (
    IPAddress,
    Resolver,
    SafeTarget,
    UnsafeDestination,
    parse_safe_url,
    resolve_public,
    system_resolver,
)

DEFAULT_USER_AGENT: Final = "suv-deals/0.1 (private deal research; callback delivery)"
_RESPONSE_HEADER_ALLOWLIST: Final = frozenset({"content-type", "content-length", "retry-after", "date"})
_FORBIDDEN_REQUEST_HEADERS: Final = frozenset(
    {"host", "connection", "transfer-encoding", "upgrade", "proxy-authorization", "te", "keep-alive"}
)
_MAX_IPS_TRIED: Final = 3


class SafeHttpFailure(StrEnum):
    DESTINATION_REJECTED = "destination_rejected"  # URL/DNS/IP policy refused the destination
    CONNECT_FAILED = "connect_failed"  # refused/unreachable before any request bytes were sent
    TLS_FAILED = "tls_failed"
    TIMEOUT_BEFORE_SEND = "timeout_before_send"
    TIMEOUT_AFTER_SEND = "timeout_after_send"  # request fully sent: the receiver may have accepted it
    CONNECTION_LOST = "connection_lost"
    REDIRECT_REFUSED = "redirect_refused"
    PROTOCOL_ERROR = "protocol_error"
    REQUEST_TOO_LARGE = "request_too_large"


class SafeHttpError(Exception):
    """A safe, URL-free description of an outbound failure."""

    def __init__(
        self,
        failure: SafeHttpFailure,
        *,
        possibly_delivered: bool = False,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"outbound request failed: {failure.value}")
        self.failure = failure
        self.possibly_delivered = possibly_delivered
        self.status_code = status_code
        self.detail = detail  # short internal classification only; never a URL or body

    def __repr__(self) -> str:
        return (
            f"SafeHttpError({self.failure.value}, possibly_delivered={self.possibly_delivered}, "
            f"status_code={self.status_code})"
        )


@dataclass(frozen=True, slots=True)
class HttpLimits:
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 10.0
    write_timeout_s: float = 10.0
    pool_timeout_s: float = 5.0
    total_timeout_s: float = 15.0
    max_request_bytes: int = 262_144
    max_response_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        values = (
            self.connect_timeout_s,
            self.read_timeout_s,
            self.write_timeout_s,
            self.pool_timeout_s,
            self.total_timeout_s,
        )
        if any(v <= 0 for v in values) or self.max_request_bytes <= 0 or self.max_response_bytes <= 0:
            raise ValueError("HTTP limits must be positive")


@dataclass(frozen=True, slots=True)
class SafeResponse:
    status_code: int
    headers: Mapping[str, str]  # allow-listed, lower-case names only
    body: bytes  # at most max_response_bytes
    truncated: bool
    elapsed: timedelta
    pinned_ip: str | None  # the validated IP actually connected to (None in explicit proxy mode)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class SafeHttp(Protocol):
    """What integrations need from an outbound client (SafeHttpClient implements it)."""

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str],
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse: ...

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse: ...


@dataclass
class _SendTracer:
    """Collects httpcore trace events to know whether the request was fully sent."""

    events: int = 0
    connected: bool = False
    request_started: bool = False
    request_sent: bool = False
    response_started: bool = False
    status: int | None = None
    names: list[str] = field(default_factory=list)

    async def __call__(self, name: str, info: Mapping[str, Any]) -> None:
        self.events += 1
        if len(self.names) < 32:
            self.names.append(name)
        if name.endswith(("connect_tcp.complete", "start_tls.complete")):
            self.connected = True
        elif name.endswith("send_request_headers.started"):
            self.request_started = True
        elif name.endswith("send_request_body.complete"):
            self.request_sent = True
        elif name.endswith("receive_response_headers.started"):
            self.request_sent = True
            self.response_started = True

    @property
    def possibly_delivered(self) -> bool:
        # Without trace events (custom transports) we cannot prove the request was not sent.
        return self.request_sent or self.events == 0


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError | ssl.CertificateError):
            return True
        text = str(current)
        if "CERTIFICATE_VERIFY_FAILED" in text or "[SSL" in text or "TLSV1_ALERT" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _ip_url(target: SafeTarget, ip: IPAddress) -> str:
    host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    return f"{target.scheme}://{host}:{target.port}{target.path_query}"


def _host_header(target: SafeTarget) -> str:
    default_port = 443 if target.scheme == "https" else 80
    return target.hostname if target.port == default_port else f"{target.hostname}:{target.port}"


class SafeHttpClient:
    """Outbound client enforcing the netguard policy at connection time."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        limits: HttpLimits | None = None,
        allowed_schemes: Sequence[str] = ("https",),
        allowed_ports: Sequence[int] = (443,),
        proxy: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._resolver = resolver
        self._limits = limits or HttpLimits()
        self._schemes = tuple(allowed_schemes)
        self._ports = tuple(allowed_ports)
        self._proxy = proxy
        self._user_agent = user_agent
        if transport is None:
            transport = httpx.AsyncHTTPTransport(
                verify=ssl_context if ssl_context is not None else True,
                trust_env=False,
                retries=0,
                proxy=proxy,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=0, keepalive_expiry=0),
            )
        timeout = httpx.Timeout(
            connect=self._limits.connect_timeout_s,
            read=self._limits.read_timeout_s,
            write=self._limits.write_timeout_s,
            pool=self._limits.pool_timeout_s,
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
        )

    @property
    def limits(self) -> HttpLimits:
        return self._limits

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str],
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        return await self.request(
            "POST",
            url,
            content=content,
            headers=headers,
            timeout_s=timeout_s,
            max_response_bytes=max_response_bytes,
        )

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        return await self.request(
            "GET", url, headers=headers, timeout_s=timeout_s, max_response_bytes=max_response_bytes
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        if method not in {"GET", "POST", "HEAD"}:
            raise ValueError("unsupported method")
        if content is not None and len(content) > self._limits.max_request_bytes:
            raise SafeHttpError(SafeHttpFailure.REQUEST_TOO_LARGE)
        request_headers = dict(headers or {})
        if any(name.lower() in _FORBIDDEN_REQUEST_HEADERS for name in request_headers):
            raise ValueError("caller may not set Host or hop-by-hop headers")
        limit = max_response_bytes or self._limits.max_response_bytes
        total = timeout_s if timeout_s is not None else self._limits.total_timeout_s
        if total <= 0:
            raise ValueError("timeout must be positive")
        if "#" in url:
            raise SafeHttpError(SafeHttpFailure.DESTINATION_REJECTED, detail="fragment")
        try:
            target = parse_safe_url(url, allowed_schemes=self._schemes, allowed_ports=self._ports)
        except UnsafeDestination:
            raise SafeHttpError(SafeHttpFailure.DESTINATION_REJECTED, detail="url_policy") from None
        started = time.monotonic()
        current: _SendTracer | None = None
        try:
            with anyio.fail_after(total):
                try:
                    ips = await resolve_public(target.hostname, target.port, self._resolver)
                except UnsafeDestination:
                    raise SafeHttpError(SafeHttpFailure.DESTINATION_REJECTED, detail="dns_policy") from None
                request_headers["Host"] = _host_header(target)
                if self._proxy is not None:
                    current = _SendTracer()
                    return await self._attempt(
                        method,
                        target.url,
                        ip=None,
                        target=target,
                        content=content,
                        headers=request_headers,
                        limit=limit,
                        started=started,
                        tracer=current,
                    )
                last_error: SafeHttpError | None = None
                for ip in ips[:_MAX_IPS_TRIED]:
                    current = _SendTracer()
                    try:
                        return await self._attempt(
                            method,
                            _ip_url(target, ip),
                            ip=ip,
                            target=target,
                            content=content,
                            headers=request_headers,
                            limit=limit,
                            started=started,
                            tracer=current,
                        )
                    except SafeHttpError as exc:
                        if exc.failure is SafeHttpFailure.CONNECT_FAILED and not exc.possibly_delivered:
                            last_error = exc
                            continue
                        raise
                assert last_error is not None
                raise last_error
        except TimeoutError:
            # The total deadline cancelled the attempt; decide from what was observed on the wire.
            if current is None:
                raise SafeHttpError(SafeHttpFailure.TIMEOUT_BEFORE_SEND, detail="dns") from None
            if current.status is not None or current.possibly_delivered:
                raise SafeHttpError(
                    SafeHttpFailure.TIMEOUT_AFTER_SEND,
                    possibly_delivered=True,
                    status_code=current.status,
                    detail="total_deadline",
                ) from None
            raise SafeHttpError(SafeHttpFailure.TIMEOUT_BEFORE_SEND, detail="total_deadline") from None

    async def _attempt(
        self,
        method: str,
        request_url: str,
        *,
        ip: IPAddress | None,
        target: SafeTarget,
        content: bytes | None,
        headers: dict[str, str],
        limit: int,
        started: float,
        tracer: _SendTracer,
    ) -> SafeResponse:
        extensions: dict[str, Any] = {"trace": tracer}
        if ip is not None:
            extensions["sni_hostname"] = target.hostname
        try:
            async with self._client.stream(
                method, request_url, content=content, headers=headers, extensions=extensions
            ) as response:
                tracer.status = response.status_code
                if 300 <= response.status_code < 400:
                    raise SafeHttpError(
                        SafeHttpFailure.REDIRECT_REFUSED,
                        possibly_delivered=True,
                        status_code=response.status_code,
                    )
                body, truncated = await _read_bounded(response, limit)
                kept = {
                    name.lower(): value[:256]
                    for name, value in response.headers.items()
                    if name.lower() in _RESPONSE_HEADER_ALLOWLIST
                }
                status = response.status_code
        except SafeHttpError:
            raise
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            raise SafeHttpError(SafeHttpFailure.TIMEOUT_BEFORE_SEND) from None
        except httpx.WriteTimeout:
            # The body was not completely written, so the receiver cannot have a complete request.
            raise SafeHttpError(SafeHttpFailure.TIMEOUT_BEFORE_SEND, detail="write") from None
        except httpx.ReadTimeout:
            raise SafeHttpError(
                SafeHttpFailure.TIMEOUT_AFTER_SEND, possibly_delivered=True, status_code=tracer.status
            ) from None
        except httpx.ConnectError as exc:
            failure = SafeHttpFailure.TLS_FAILED if _is_tls_error(exc) else SafeHttpFailure.CONNECT_FAILED
            raise SafeHttpError(failure) from None
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            failure = (
                SafeHttpFailure.CONNECTION_LOST
                if isinstance(exc, httpx.ReadError)
                else SafeHttpFailure.PROTOCOL_ERROR
            )
            raise SafeHttpError(
                failure,
                possibly_delivered=tracer.status is not None or tracer.possibly_delivered,
                status_code=tracer.status,
            ) from None
        except httpx.WriteError:
            raise SafeHttpError(SafeHttpFailure.CONNECT_FAILED, detail="write") from None
        except (httpx.UnsupportedProtocol, httpx.InvalidURL):
            raise SafeHttpError(SafeHttpFailure.DESTINATION_REJECTED, detail="url") from None
        except httpx.HTTPError:
            raise SafeHttpError(
                SafeHttpFailure.PROTOCOL_ERROR,
                possibly_delivered=tracer.status is not None or tracer.possibly_delivered,
                status_code=tracer.status,
            ) from None
        return SafeResponse(
            status_code=status,
            headers=kept,
            body=body,
            truncated=truncated,
            elapsed=timedelta(seconds=time.monotonic() - started),
            pinned_ip=ip.compressed if ip is not None else None,
        )


async def _read_bounded(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` raw bytes (identity encoding requested; no decompression bombs)."""
    buffer = bytearray()
    truncated = False
    async for chunk in response.aiter_raw():
        remaining = limit - len(buffer)
        if len(chunk) > remaining:
            buffer.extend(chunk[:remaining])
            truncated = True
            break
        buffer.extend(chunk)
    return bytes(buffer), truncated
