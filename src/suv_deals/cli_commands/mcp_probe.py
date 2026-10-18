"""``suv-deals mcp probe``: check a running MCP endpoint the way a remote client sees it.

The evidence of docs/connect_mcp.md section 4 step 8, from the operator's machine; no settings and
no database are read:

(a) an unauthenticated ``initialize`` POST must answer ``401`` with a ``WWW-Authenticate: Bearer``
    challenge (its parameters are reported; they carry no secret);
(b) with ``--authorization-file`` (exactly ``Bearer suvmcp_...`` or ``Bearer <JWT>``; a regular
    file without group/other access, never a symlink; `_authorization_file.read_authorization`):
    the official ``mcp`` SDK client over Streamable HTTP runs the ``initialize`` handshake and
    ``tools/list`` and calls ``deals_health``;
(c) it reports the negotiated protocol version, the server name and version, the tool names, the
    SHA-256 of the canonical ``tools/list`` result (`tools_sha256`), the ``deals_health`` status
    fields (`health_summary`) and every request id the server returned (``X-Request-Id`` response
    headers and the tool envelope's ``request_id``).

The token and the ``Authorization`` header are never printed or logged: the header lives only in
the authenticated HTTP client (the unauthenticated check uses a separate client without it);
redirects are not followed, so the header never reaches another location; the file is sent only
over https or to a loopback address; while the probe runs, the ``httpx``/``httpcore``/``mcp``
loggers stay at WARNING with `observability.logging.RedactionFilter` attached (it masks ``Bearer``
values and ``suvmcp_`` tokens); every error text passes through ``redact``.

Exit codes: ``0`` every check passed; ``1`` a check failed (the output names it); ``2`` usage
(``--url``, ``--timeout`` or the authorization file).
"""

# ruff: noqa: PLC0415 - the SDK and the HTTP client are imported lazily so `--help` stays fast

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import click

from suv_deals.cli_commands._authorization_file import read_authorization
from suv_deals.cli_commands._common import EXIT_PROBLEMS, EXIT_USAGE, echo, emit_json, exit_with, fail, safe

if TYPE_CHECKING:
    import httpx2
    from mcp import Client

DEFAULT_URL: Final = "http://127.0.0.1:8000/mcp"
DEFAULT_TIMEOUT_S: Final = 10.0
PROBE_CLIENT_NAME: Final = "suv-deals-mcp-probe"
HEALTH_TOOL: Final = "deals_health"
MAX_TOOL_PAGES: Final = 20
#: Library loggers held at WARNING (and redacted) while the probe runs; their children inherit it.
LIBRARY_LOGGERS: Final = (
    "httpx",
    "httpx2",
    "httpcore",
    "hpack",
    "h11",
    "h2",
    "mcp",
    "mcp_types",
    "sse_starlette",
)

CHECK_UNAUTHENTICATED: Final = "unauthenticated initialize"
CHECK_INITIALIZE: Final = "initialize"
CHECK_TOOLS: Final = "tools/list"
CHECK_HEALTH: Final = f"tools/call {HEALTH_TOOL}"
AUTHENTICATED_CHECKS: Final = (CHECK_INITIALIZE, CHECK_TOOLS, CHECK_HEALTH)

_STATUS_MEANING: Final[Mapping[int, str]] = {
    401: "the server refused the credential (unknown, revoked or expired token, or the server runs"
    " another MCP_AUTH_MODE)",
    403: "forbidden (insufficient scope, an Origin that is not allowed, or loopback-only access)",
    404: "not found (is the URL the MCP endpoint, ending in /mcp?)",
    405: "method not allowed (is the URL the MCP endpoint?)",
    421: "the Host is not allowed (the URL's host must match MCP_PUBLIC_URL)",
    429: "rate limited (too many failed authentications from this address; wait and retry)",
    503: "unavailable (the MCP endpoint is not configured, or its database or identity provider is down)",
}


# --------------------------------------------------------------------------------------------
# Target and report
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    url: str
    https: bool
    loopback: bool


def parse_target(url: str) -> ProbeTarget:
    """``--url``: a plain http(s) URL without credentials, query or fragment (usage error)."""
    text = url.strip()
    if not text or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        fail("--url must be a plain http(s) URL", EXIT_USAGE)
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        fail("--url must be an http or https URL", EXIT_USAGE)
    if "@" in parts.netloc:
        fail("--url must not contain credentials (user or password)", EXIT_USAGE)
    host = parts.hostname
    if not host:
        fail("--url needs a host", EXIT_USAGE)
    if parts.query or parts.fragment:
        fail("--url must be a plain URL without query or fragment", EXIT_USAGE)
    try:
        _ = parts.port
    except ValueError:
        fail("--url has an invalid port", EXIT_USAGE)
    return ProbeTarget(url=text, https=scheme == "https", loopback=is_loopback_host(host))


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class ProbeReport:
    url: str
    sdk_version: str
    authenticated: bool
    checks: list[Check] = field(default_factory=list)
    challenge: dict[str, str] | None = None
    protocol_version: str | None = None
    server: dict[str, str | None] | None = None
    tools: list[str] = field(default_factory=list)
    tools_sha256: str | None = None
    health: dict[str, Any] | None = None
    request_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, safe(detail)))

    def note_request_id(self, value: object) -> None:
        if isinstance(value, str) and value and value not in self.request_ids:
            self.request_ids.append(safe(value)[:200])

    def as_dict(self) -> dict[str, Any]:
        failed = [check.name for check in self.checks if not check.ok]
        return {
            "url": self.url,
            "passed": self.passed,
            "failed_checks": failed,
            "authenticated_checks": "run" if self.authenticated else "skipped (no --authorization-file)",
            "checks": [check.as_dict() for check in self.checks],
            "challenge": self.challenge,
            "protocol_version": self.protocol_version,
            "server": self.server,
            "sdk": {"name": "mcp", "version": self.sdk_version},
            "tools": self.tools,
            "tools_sha256": self.tools_sha256,
            "deals_health": self.health,
            "request_ids": self.request_ids,
        }


# --------------------------------------------------------------------------------------------
# Pure helpers (tested directly)
# --------------------------------------------------------------------------------------------


def tools_sha256(tools: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 (hex) of the canonical ``tools/list`` result: ``{"tools": [...]}`` with the tools
    sorted by name, serialized with ``json.dumps(sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)`` and encoded as UTF-8. Each tool is its wire object (the SDK's parsed
    tool with null fields omitted equals it)."""
    canonical = {"tools": sorted(tools, key=lambda tool: str(tool.get("name", "")))}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_challenge(value: str) -> dict[str, str]:
    """The auth-params of a ``WWW-Authenticate: Bearer k="v", k2=v2`` challenge (no secrets in it)."""
    _, _, rest = value.strip().partition(" ")
    params: dict[str, str] = {}
    index, length = 0, len(rest)
    while index < length:
        while index < length and rest[index] in " ,":
            index += 1
        start = index
        while index < length and rest[index] not in "=, ":
            index += 1
        key = rest[start:index].strip().lower()
        if index >= length or rest[index] != "=":
            if key:
                params.setdefault(key, "")
            continue
        index += 1
        if index < length and rest[index] == '"':
            index += 1
            chars: list[str] = []
            while index < length and rest[index] != '"':
                if rest[index] == "\\" and index + 1 < length:
                    index += 1
                chars.append(rest[index])
                index += 1
            index += 1  # closing quote
            params[key] = "".join(chars)
        else:
            start = index
            while index < length and rest[index] != ",":
                index += 1
            params[key] = rest[start:index].strip()
    return {k: safe(v)[:500] for k, v in params.items() if k}


def health_summary(structured: Mapping[str, Any]) -> dict[str, Any]:
    """The status fields of a ``deals_health`` envelope (no details, gate texts or source rows)."""
    data = structured.get("data")
    data = data if isinstance(data, Mapping) else {}
    readiness = data.get("readiness")
    sources = data.get("sources")
    blockers = data.get("activation_blockers")
    warnings = structured.get("warnings")
    states = Counter(
        str(source.get("state"))
        for source in (sources if isinstance(sources, list) else [])
        if isinstance(source, Mapping)
    )
    return {
        "ready": data.get("ready"),
        "readiness": {
            str(check.get("name")): check.get("status")
            for check in (readiness if isinstance(readiness, list) else [])
            if isinstance(check, Mapping)
        },
        "source_network_enabled": data.get("source_network_enabled"),
        "bridge_status": data.get("bridge_status"),
        "notification_route": data.get("notification_route"),
        "sources_by_state": dict(sorted(states.items())),
        "activation_blockers": len(blockers) if isinstance(blockers, list) else None,
        "warnings": [
            str(w.get("code"))
            for w in (warnings if isinstance(warnings, list) else [])
            if isinstance(w, Mapping)
        ],
        "request_id": structured.get("request_id"),
    }


def _health_text(health: Mapping[str, Any]) -> str:
    def word(value: object) -> str:
        return "true" if value is True else "false" if value is False else str(value)

    readiness = ", ".join(f"{k}={v}" for k, v in sorted((health.get("readiness") or {}).items())) or "-"
    states = ", ".join(f"{k}={v}" for k, v in (health.get("sources_by_state") or {}).items()) or "-"
    warnings = ", ".join(health.get("warnings") or []) or "-"
    return (
        f"ready={word(health.get('ready'))} ({readiness}); "
        f"source_network_enabled={word(health.get('source_network_enabled'))}; "
        f"bridge_status={health.get('bridge_status')}; "
        f"notification_route={health.get('notification_route')}; "
        f"sources by state: {states}; activation blockers: {health.get('activation_blockers')}; "
        f"warnings: {warnings}"
    )


def _first_leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _failure_text(error: BaseException, last_status: int | None, timeout: float) -> str:
    """A clear, redacted reason for a failed step (the HTTP status the server sent wins)."""
    leaf = _first_leaf(error)
    if last_status is not None and last_status >= 400:
        meaning = _STATUS_MEANING.get(last_status, "the server answered with an error")
        return f"HTTP {last_status}: {meaning}"
    if isinstance(leaf, TimeoutError):
        return f"no answer within {timeout:g} s"
    name = type(leaf).__name__
    if name in ("ConnectError", "ConnectTimeout"):
        return f"cannot connect ({name}): is the server running at this URL?"
    message = str(leaf).strip()
    return f"{name}: {message[:300]}" if message else name


# --------------------------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------------------------


@contextmanager
def guarded_library_logs() -> Iterator[None]:
    """Hold the HTTP/SDK loggers at WARNING and redact what they still emit (module docstring)."""
    from suv_deals.observability.logging import RedactionFilter

    redaction = RedactionFilter()
    names = set(LIBRARY_LOGGERS)
    names.update(n for n in logging.Logger.manager.loggerDict if n.split(".", 1)[0] in LIBRARY_LOGGERS)
    saved: list[tuple[logging.Logger, int]] = []
    for name in sorted(names):
        logger = logging.getLogger(name)
        saved.append((logger, logger.level))
        if name in LIBRARY_LOGGERS or logging.NOTSET < logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)
        logger.addFilter(redaction)
    try:
        yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)
            logger.removeFilter(redaction)


def _client_info_version() -> str:
    import suv_deals

    return suv_deals.__version__


async def _check_unauthenticated(
    http: httpx2.AsyncClient, target: ProbeTarget, report: ProbeReport, request_timeout_s: float
) -> None:
    import anyio
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": HANDSHAKE_PROTOCOL_VERSIONS[-1],
            "capabilities": {},
            "clientInfo": {"name": PROBE_CLIENT_NAME, "version": _client_info_version()},
        },
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    try:
        with anyio.fail_after(request_timeout_s):
            response = await http.post(target.url, json=body, headers=headers)
    except Exception as exc:
        report.add(
            CHECK_UNAUTHENTICATED,
            False,
            f"no answer from {target.url}: {_failure_text(exc, None, request_timeout_s)}",
        )
        return
    challenge = response.headers.get("www-authenticate")
    scheme = challenge.strip().split(" ", 1)[0].lower() if challenge else ""
    if response.status_code == 401 and scheme == "bearer":
        report.challenge = parse_challenge(challenge or "")
        report.add(CHECK_UNAUTHENTICATED, True, "HTTP 401 with WWW-Authenticate: Bearer")
        return
    if response.status_code < 400:
        detail = f"HTTP {response.status_code} WITHOUT authentication: the endpoint is open"
    elif response.status_code == 401:
        detail = "HTTP 401 but without a WWW-Authenticate: Bearer challenge"
    else:
        meaning = _STATUS_MEANING.get(response.status_code, "unexpected answer")
        detail = f"HTTP {response.status_code}, expected 401 ({meaning})"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping) and isinstance(payload.get("error_description"), str):
        detail += f"; server says: {str(payload['error_description'])[:200]}"
    report.add(CHECK_UNAUTHENTICATED, False, detail)


async def _list_all_tools(client: Client) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        page = await client.list_tools(cursor=cursor)
        tools.extend(t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise RuntimeError(f"tools/list did not finish within {MAX_TOOL_PAGES} pages")


async def _check_authenticated(
    target: ProbeTarget, authorization: str, report: ProbeReport, request_timeout_s: float, *, trust_env: bool
) -> None:
    import anyio
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import Implementation

    statuses: list[int] = []

    async def remember(response: httpx2.Response) -> None:
        statuses.append(response.status_code)
        report.note_request_id(response.headers.get("x-request-id"))

    step = CHECK_INITIALIZE
    done: set[str] = set()
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": authorization},
            timeout=request_timeout_s,
            follow_redirects=False,
            trust_env=trust_env,
            event_hooks={"response": [remember]},
        ) as http:
            with anyio.fail_after(request_timeout_s * 4):
                async with Client(
                    streamable_http_client(target.url, http_client=http, terminate_on_close=False),
                    mode="legacy",
                    client_info=Implementation(name=PROBE_CLIENT_NAME, version=_client_info_version()),
                    read_timeout_seconds=request_timeout_s,
                    cache=None,
                ) as client:
                    info = client.server_info
                    report.protocol_version = client.protocol_version
                    report.server = {
                        "name": info.name if info else None,
                        "title": info.title if info else None,
                        "version": info.version if info else None,
                    }
                    server_text = (
                        f"{info.name} {info.version}" + (f" ({info.title})" if info.title else "")
                        if info
                        else "unnamed server"
                    )
                    report.add(
                        CHECK_INITIALIZE, True, f"protocol {client.protocol_version}; server {server_text}"
                    )
                    done.add(CHECK_INITIALIZE)

                    step = CHECK_TOOLS
                    statuses.clear()
                    tools = await _list_all_tools(client)
                    report.tools = sorted(str(t.get("name")) for t in tools)
                    report.tools_sha256 = tools_sha256(tools)
                    if HEALTH_TOOL not in report.tools:
                        report.add(
                            CHECK_TOOLS,
                            False,
                            f"{len(tools)} tool(s) but no {HEALTH_TOOL} (the credential lacks deals:read)",
                        )
                        done.add(CHECK_TOOLS)
                        return
                    report.add(CHECK_TOOLS, True, f"{len(tools)} tool(s)")
                    done.add(CHECK_TOOLS)

                    step = CHECK_HEALTH
                    statuses.clear()
                    result = await client.call_tool(HEALTH_TOOL, {})
                    structured = (
                        result.structured_content if isinstance(result.structured_content, Mapping) else {}
                    )
                    if result.is_error:
                        code = structured.get("code", "unknown")
                        message = str(structured.get("message", ""))[:200]
                        report.note_request_id(structured.get("correlation_id"))
                        report.add(CHECK_HEALTH, False, f"tool error {code}: {message}")
                    else:
                        report.health = health_summary(structured)
                        report.note_request_id(report.health.get("request_id"))
                        report.add(CHECK_HEALTH, True, _health_text(report.health))
                    done.add(CHECK_HEALTH)
    except Exception as exc:
        if step not in done:
            last = statuses[-1] if statuses else None
            report.add(step, False, _failure_text(exc, last, request_timeout_s))


async def run_probe(
    target: ProbeTarget, authorization: str | None, *, request_timeout_s: float = DEFAULT_TIMEOUT_S
) -> ProbeReport:
    """Run the checks of the module docstring against ``target`` and return the report."""
    import importlib.metadata

    import httpx2

    # Every module that logs during the probe is imported BEFORE the guard collects the loggers,
    # so the redaction filter is attached to each of them (children included).
    import mcp.client.streamable_http
    import mcp.shared.jsonrpc_dispatcher  # noqa: F401

    report = ProbeReport(
        url=target.url,
        sdk_version=importlib.metadata.version("mcp"),
        authenticated=authorization is not None,
    )
    trust_env = not target.loopback  # never route loopback traffic through a proxy
    with guarded_library_logs():

        async def remember(response: httpx2.Response) -> None:
            report.note_request_id(response.headers.get("x-request-id"))

        async with httpx2.AsyncClient(
            timeout=request_timeout_s,
            follow_redirects=False,
            trust_env=trust_env,
            event_hooks={"response": [remember]},
        ) as plain:
            await _check_unauthenticated(plain, target, report, request_timeout_s)
        if authorization is not None:
            await _check_authenticated(target, authorization, report, request_timeout_s, trust_env=trust_env)
    return report


# --------------------------------------------------------------------------------------------
# Output and command
# --------------------------------------------------------------------------------------------


def render_text(report: ProbeReport) -> list[str]:
    lines = [f"MCP probe of {report.url} (official mcp SDK {report.sdk_version})"]
    for check in report.checks:
        lines.append(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
        if check.name == CHECK_UNAUTHENTICATED and check.ok and report.challenge is not None:
            params = " ".join(f'{k}="{v}"' for k, v in report.challenge.items()) or "(no parameters)"
            lines.append(f"        challenge: {params}")
        if check.name == CHECK_TOOLS and report.tools:
            lines.append(f"        tools  : {', '.join(report.tools)}")
            lines.append(f"        sha256 : {report.tools_sha256} (canonical tools/list result)")
    if not report.authenticated:
        skipped = ", ".join(AUTHENTICATED_CHECKS)
        lines.append(f"SKIP  {skipped}: no --authorization-file (only the unauthenticated check ran)")
    lines.append(f"Request ids: {', '.join(report.request_ids) or '-'}")
    failed = [check.name for check in report.checks if not check.ok]
    if failed:
        lines.append(f"Result: FAILED ({len(failed)} of {len(report.checks)} checks): {', '.join(failed)}")
    else:
        suffix = "" if report.authenticated else " (authenticated checks skipped)"
        lines.append(f"Result: all {len(report.checks)} check(s) passed{suffix}")
    return lines


@click.group("mcp")
def mcp_group() -> None:
    """Checks of a running MCP endpoint (no settings or database needed)."""


@mcp_group.command("probe")
@click.option(
    "--url",
    default=DEFAULT_URL,
    show_default=True,
    help="The MCP endpoint (http or https; no credentials, query or fragment).",
)
@click.option(
    "--authorization-file",
    "authorization_file",
    type=click.Path(path_type=Path),
    default=None,
    help="File holding exactly 'Bearer suvmcp_...' or 'Bearer <JWT>' (mode 0600, not a symlink). Without"
    " it only the unauthenticated check runs.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report.")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.5, max=120.0),
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Seconds per request.",
)
def probe(url: str, authorization_file: Path | None, as_json: bool, timeout: float) -> None:
    """Probe the MCP endpoint: 401 without a token, then initialize, tools/list and deals_health.

    Prints the evidence of docs/connect_mcp.md step 8 (protocol version, server, tool names and
    the tools/list SHA-256, the deals_health status, request ids); never the token.
    """
    import anyio

    target = parse_target(url)
    authorization = read_authorization(authorization_file) if authorization_file is not None else None
    if authorization is not None and not (target.https or target.loopback):
        fail(
            "--authorization-file is sent only over https or to a loopback address (127.0.0.1, ::1,"
            " localhost); use an https URL",
            EXIT_USAGE,
        )

    async def main() -> ProbeReport:
        return await run_probe(target, authorization, request_timeout_s=timeout)

    report = anyio.run(main)
    if as_json:
        emit_json(report.as_dict())
    else:
        for line in render_text(report):
            echo(line)
    exit_with(0 if report.passed else EXIT_PROBLEMS)


__all__ = [
    "DEFAULT_URL",
    "ProbeReport",
    "ProbeTarget",
    "health_summary",
    "mcp_group",
    "parse_challenge",
    "parse_target",
    "run_probe",
    "tools_sha256",
]
