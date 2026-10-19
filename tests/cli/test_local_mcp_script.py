"""``scripts/local_mcp.sh``: dot through the OpenAI Secure MCP Tunnel (docs/connect_mcp.md section 8).

The script runs from a copy in a temporary "repository" (its own ``var/`` and ``.env``) against
stub executables on a fake PATH: ``uv`` (doctor, ``api serve``, ``mcp probe``, ``credentials
create-mcp`` / ``revoke``), ``tunnel-client`` (``--version``; ``doctor`` in the output format of the
real v0.0.16 binary, whose MCP checks fail while nothing listens on the API port and whose
health_listener check fails on a leftover socket file; ``run``, which writes start-up noise like the
real one) and ``curl`` (``/readyz``, and the tunnel's health socket: ``/readyz`` and ``GET
/health?details=true``, which answers knob-controlled JSON, one answer per request from a sequence
if given: real answers of the v0.0.16 binary from tests/fixtures/tunnel_client_v0.0.16, or
synthetic ones in their shape with the control-plane states the real binary reports). Every
stub records its argv and its whole environment; one test also wraps every other external command
the script runs (``mktemp``, ``ln``, ``env``, ``ps``, ...) to record its argv, and a static test
forbids writing a secret with anything but the shell builtin ``printf`` or calling a command by
absolute path. Together they show that the runtime API key and the MCP token never reach the argv
of a command the script runs, the stubs' environment, the output or the YAML. A pty test shows that
both prompts read without echo. No network, no database, no real tunnel; the real end-to-end runs
(with the real tunnel-client v0.0.16) are recorded in docs/connect_mcp.md section 8.6. Only
SYNTHETIC values.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
LOCAL_MCP = REPO / "scripts" / "local_mcp.sh"

TUNNEL_ID = "tunnel_" + "0123456789abcdef" * 2
NAMESPACED_TUNNEL_ID = "tunnel_ab12_" + "fedcba9876543210" * 2
API_KEY = "sk-proj-FAKE-runtime-key-" + "7f3a9c" * 6
TOKEN_1 = "suvmcp_" + "a1" * 32
TOKEN_2 = "suvmcp_" + "b2" * 32
CREDENTIAL_1 = "11111111-1111-4111-8111-111111111111"
CREDENTIAL_2 = "22222222-2222-4222-8222-222222222222"
WORKSPACE = "33333333-3333-4333-8333-333333333333"
SHELL_SECRET = "FAKE-shell-openai-key-91c2d4"
DOTENV_SECRET = "FAKE-dotenv-secret-5e8b10"
TOOLS_SHA = "ab" * 32
VIEWER_TOOLS = [
    "deals_estimate_import_costs",
    "deals_get_candidate",
    "deals_get_comparables",
    "deals_get_valuation",
    "deals_health",
    "deals_list_candidates",
    "reviews_list_pending",
]
SECRETS = (API_KEY, TOKEN_1, TOKEN_2, DOTENV_SECRET)
# Every external command scripts/local_mcp.sh runs (besides the uv, tunnel-client and curl stubs),
# plus a few a regression could add (sh -c, printf, install, tee ...).
WRAPPED_COMMANDS = (
    "awk", "bash", "cat", "chmod", "cmp", "cp", "date", "dd", "dirname", "env", "flock", "grep", "head",
    "id", "install", "jq", "ln", "ls", "mkdir", "mktemp", "mv", "nohup", "printf", "ps",
    "python3", "realpath", "rm", "sed", "setsid", "sh", "sha256sum", "sleep", "stat", "tail",
    "tee", "touch", "tr", "xargs",
)  # fmt: skip

needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("setsid", "flock", "realpath", "sha256sum", "python3"))
    or not Path("/proc/net/tcp").exists(),
    reason="needs setsid, flock (util-linux), realpath and sha256sum (coreutils), python3 and /proc",
)
pytestmark = needs_tools

# --------------------------------------------------------------------------------------------
# Stubs (placeholders @NAME@ are replaced; the tunnel-client stub gets a cleaned environment, so
# every path is baked in)
# --------------------------------------------------------------------------------------------

_STUB_UV = r"""#!/usr/bin/env bash
calls=@CALLS@
knobs=@KNOBS@
printf 'uv %s|auth=%s|public=%s|events=%s|net=%s\n' "$*" "${MCP_AUTH_MODE:-}" "${MCP_PUBLIC_URL:-}" \
  "${MCP_EVENTS_ENABLED:-}" "${SOURCE_NETWORK_ENABLED:-}" >> "$calls"
{ echo "== uv $*"; env; } >> "$knobs/env-uv.txt"
case " $* " in
  *" suv-deals api serve "*)
    port="" prev=""
    for arg in "$@"; do
      if [[ $prev == --port ]]; then port=$arg; fi
      prev=$arg
    done
    # A real listener on the port (it ends with this stub). knob api_listener=foreign: it runs in
    # another session, like a program that won the race for the port.
    listen='import os, socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(16)
parent = os.getppid()
while os.getppid() == parent:
    time.sleep(0.1)'
    if [[ $(cat "$knobs/api_listener" 2>/dev/null) == foreign ]]; then
      setsid @PYTHON@ -I -c "$listen" "$port" &
      echo "$!" > "$knobs/foreign.pid"
    else
      @PYTHON@ -I -c "$listen" "$port" &
    fi
    trap 'echo "api: TERM" >> "$calls"; exit 0' TERM
    echo "stub api started"
    while :; do sleep 0.1; done ;;
  *" suv-deals doctor "*)
    echo "stub doctor: ok"
    exit "${STUB_DOCTOR_EXIT:-0}" ;;
  *" suv-deals mcp probe "*)
    if [[ ${STUB_PROBE_EXIT:-0} != 0 ]]; then
      echo '{"passed": false, "protocol_version": null, "tools": [], "tools_sha256": null, "request_ids": [],'
      echo ' "checks": [{"name": "initialize", "ok": false,'
      echo '   "detail": "HTTP 401: the server refused the credential"}]}'
      exit "$STUB_PROBE_EXIT"
    fi
    cat <<'JSON'
{"passed": true, "protocol_version": "2025-11-25", "tools_sha256": "@TOOLS_SHA@",
 "tools": @TOOLS@, "request_ids": ["req-0001", "req-0002"],
 "checks": [{"name": "unauthenticated initialize", "ok": true,
             "detail": "HTTP 401 with WWW-Authenticate: Bearer"},
            {"name": "initialize", "ok": true, "detail": "protocol 2025-11-25; server suv-deals 0.1.0"},
            {"name": "tools/list", "ok": true, "detail": "7 tool(s)"},
            {"name": "tools/call deals_health", "ok": true, "detail": "ready=true"}]}
JSON
    ;;
  *" credentials create-mcp "*)
    target="" prev=""
    for arg in "$@"; do
      if [[ $prev == --authorization-file ]]; then target=$arg; fi
      prev=$arg
    done
    if [[ ${STUB_CREATE_EXIT:-0} != 0 ]]; then
      echo "error: stub create-mcp failed" >&2
      exit "$STUB_CREATE_EXIT"
    fi
    if [[ -e $target ]]; then echo "error: --authorization-file already exists" >&2; exit 2; fi
    # knob mktemp_fails_after_create: from now on the mktemp wrapper fails (a full disk, say).
    if [[ -e $knobs/mktemp_fails_after_create ]]; then touch "$knobs/mktemp_fail"; fi
    n=$(( $(cat "$knobs/create_count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$knobs/create_count"
    if ((n == 1)); then id=@CRED1@ token=@TOKEN1@; else id=@CRED2@ token=@TOKEN2@; fi
    (umask 077 && printf 'Bearer %s' "$token" > "$target")
    echo "MCP credential to create:"
    echo "  token goes to  : $target (new file, mode 0600, 'Bearer <token>'; not printed)"
    echo "Created credential $id in workspace @WORKSPACE@"
    echo "  prefix  : ${token:0:13}"
    echo "  expires : $(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%S.123456+00:00)"
    echo "Authorization header written to $target" ;;
  *" credentials revoke "*)
    if [[ -e $knobs/revoke_fail ]]; then echo "error: stub revoke failed" >&2; exit 1; fi
    if [[ ${STUB_REVOKE_EXIT:-0} != 0 ]]; then
      echo "error: stub revoke failed" >&2
      exit "$STUB_REVOKE_EXIT"
    fi
    echo "Revoked." ;;
  *) echo "stub ok" ;;
esac
"""

_STUB_TUNNEL_CLIENT = r"""#!/usr/bin/env bash
calls=@CALLS@
knobs=@KNOBS@
printf 'tunnel-client %s\n' "$*" >> "$calls"
{ echo "== tunnel-client $*"; env; } >> "$knobs/env-tunnel.txt"
case "$1" in
  --version) echo "tunnel-client v0.0.0-stub (linux/amd64)" ;;
  doctor)
    # The output format of the real v0.0.16 doctor: its MCP checks fail while nothing listens on
    # the configured URL. Knobs: doctor_extra_failed (one more failed check), doctor_mode (garbage:
    # no machine-readable result; no_failed_line: RESULT fail without FAILED_CHECKS).
    cfg="" sock="" prev=""
    for arg in "$@"; do
      if [[ $prev == --config ]]; then cfg=$arg; fi
      if [[ $prev == --health.unix-socket ]]; then sock=$arg; fi
      prev=$arg
    done
    mode=$(cat "$knobs/doctor_mode" 2>/dev/null)
    if [[ $mode == garbage ]]; then echo "panic: runtime error: invalid memory address"; exit 2; fi
    url=$(sed -nE 's/^ +url: "(.*)"$/\1/p' "$cfg" | head -n 1)
    hostport=${url#http://}
    hostport=${hostport%%/*}
    failed=""
    printf 'CHECK %-24s PASS %s\n' config_source "$cfg" profile_load "$cfg"
    printf 'CHECK %-24s PASS %s\n' tunnel_id "$(sed -nE 's/^ +tunnel_id: "(.*)"$/\1/p' "$cfg")"
    printf 'CHECK %-24s PASS configured\n' control_plane_api_key
    printf 'CHECK %-24s PASS %s\n' mcp_target "$url"
    # Knobs: doctor_oauth=invalid (another program answers the URL: v0.0.16 passes
    # mcp_server_reachable on any TCP connect and fails oauth_metadata unless every discovery
    # candidate answers 404 like the API); doctor_mcp=fail (it reports the URL unreachable anyway).
    if [[ $(cat "$knobs/doctor_mcp" 2>/dev/null) != fail ]] &&
      (exec 3<>"/dev/tcp/${hostport%%:*}/${hostport##*:}") 2>/dev/null; then
      printf 'CHECK %-24s PASS HTTP 405 from %s\n' mcp_server_reachable "$url"
      if [[ $(cat "$knobs/doctor_oauth" 2>/dev/null) == invalid ]]; then
        printf 'CHECK %-24s FAIL oauth discovery empty body from http://%s (status 405)\n' oauth_metadata \
          "$hostport"
        failed=oauth_metadata
      else
        printf 'CHECK %-24s PASS OAuth metadata not advertised; %s\n' oauth_metadata \
          "all candidates returned HTTP 404"
      fi
    else
      printf 'CHECK %-24s FAIL dial tcp %s: connect: connection refused\n' mcp_server_reachable "$hostport"
      printf 'CHECK %-24s FAIL oauth discovery GET http://%s: connection refused\n' oauth_metadata "$hostport"
      failed=mcp_server_reachable,oauth_metadata
    fi
    # Like the real one: a socket file left at the path (by a killed tunnel) cannot be bound.
    if [[ -e $sock || -L $sock ]]; then
      printf 'CHECK %-24s FAIL listen unix %s: bind: address already in use\n' health_listener "$sock"
      failed=${failed:+$failed,}health_listener
    else
      printf 'CHECK %-24s PASS will bind unix socket %s\n' health_listener "$sock"
    fi
    extra=$(cat "$knobs/doctor_extra_failed" 2>/dev/null)
    if [[ -n $extra ]]; then
      printf 'CHECK %-24s FAIL stub: %s failed\n' "$extra" "$extra"
      failed=${failed:+$failed,}$extra
    fi
    printf 'CHECK %-24s SKIP Codex not detected locally\n\n' codex_plugin
    if [[ -z $failed ]]; then
      echo "RESULT ok"
      echo "NEXT   tunnel-client run --config $cfg"
      exit 0
    fi
    echo "RESULT fail"
    if [[ $mode != no_failed_line ]]; then echo "FAILED_CHECKS $failed"; fi
    echo "EXIT_CODE 2"
    echo
    echo "CHECK ${failed%%,*}   FAIL"
    echo "Why this matters:"
    echo "  tunnel-client should be able to reach the main MCP target before the daemon starts polling."
    exit 2 ;;
  run)
    sock="" prev=""
    for arg in "$@"; do
      if [[ $prev == --health.unix-socket ]]; then sock=$arg; fi
      prev=$arg
    done
    @PYTHON@ -I -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$sock"
    touch "$knobs/tunnel.running"
    trap 'rm -f "$knobs/tunnel.running"; echo "tunnel: TERM" >> "$calls"; exit 0' TERM
    trap 'rm -f "$knobs/tunnel.running"; echo "tunnel: INT" >> "$calls"; exit 130' INT
    # Start-up noise like the real binary's dependency-injection lines.
    for i in $(seq 1 25); do echo "INFO provided constructor=stub.fx$i()"; done
    echo "tunnel stub: polling"
    # knobs/run_log: lines the real binary logs (failed polls with OpenAI's error), as they are.
    if [[ -e $knobs/run_log ]]; then cat "$knobs/run_log"; fi
    mode=$(cat "$knobs/run_mode" 2>/dev/null || echo wait)
    case $mode in
      exit:*)
        IFS=: read -r _ code after <<< "$mode"
        for ((i = 0; i < after * 10; i++)); do sleep 0.1; done # short sleeps: traps run at once
        rm -f "$knobs/tunnel.running"
        echo "tunnel stub: exiting with $code"
        exit "$code" ;;
      *) while :; do sleep 0.1; done ;;
    esac ;;
  *) echo "unknown command" >&2; exit 2 ;;
esac
"""

_STUB_CURL = r"""#!/usr/bin/env bash
calls=@CALLS@
knobs=@KNOBS@
printf 'curl %s\n' "$*" >> "$calls"
{ echo "== curl $*"; env; } >> "$knobs/env-curl.txt"
case " $* " in
  *" --unix-socket "*)
    # The tunnel's health socket: /readyz answers 200 while it runs (also while OpenAI refuses the
    # key, like the real one); /health?details=true answers the JSON in knobs/health_json or, when
    # knobs/health_seq exists, its next line (one JSON answer per line; the last one repeats).
    if [[ ! -e $knobs/tunnel.running ]]; then
      if [[ " $* " != *"/health?details=true "* ]]; then printf '000'; fi
      exit 7
    fi
    case " $* " in
      *"/health?details=true "*)
        if [[ -s $knobs/health_seq ]]; then
          n=$(( $(cat "$knobs/health_seq_n" 2>/dev/null || echo 0) + 1 ))
          echo "$n" > "$knobs/health_seq_n"
          total=$(grep -c '' "$knobs/health_seq")
          if ((n > total)); then n=$total; fi
          sed -n "${n}p" "$knobs/health_seq"
        else
          cat "$knobs/health_json"
        fi ;;
      *) printf '200' ;;
    esac ;;
  *"/readyz "*)
    # Like the real curl: no answer unless something listens on the port.
    url=${*: -1}
    hostport=${url#http://}
    hostport=${hostport%%/*}
    if ! (exec 3<>"/dev/tcp/${hostport%%:*}/${hostport##*:}") 2>/dev/null; then printf '000'; exit 7; fi
    printf '%s' "$(cat "$knobs/readyz_code" 2>/dev/null || echo 200)" ;;
  *) printf '000'; exit 7 ;;
esac
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _health(
    status: str,
    state: str,
    *,
    http_status: object = None,
    failures: object = 0,
    category: str | None = None,
) -> str:
    """``GET /health?details=true`` of tunnel-client v0.0.16 in its shape (one line; a subset of the
    keys of the real answers in tests/fixtures/tunnel_client_v0.0.16, in their order), with this
    control-plane state. The real binary reports unknown/polling right after the start, then
    degraded with state backoff or polling (alternating between retries) while polls fail, the
    category http_error (with http_status) or network_error, and ok/polling once accepted."""
    details: dict[str, object] = {
        "last_attempt": "2026-10-18T19:36:26.473975398Z",
        "consecutive_failures": failures,
        "configured_wait_seconds": 30,
    }
    control_plane: dict[str, object] = {"status": status, "state": state}
    if category is not None:
        details["failure_category"] = category
        control_plane["reason_code"] = category
    if http_status is not None:
        details["http_status"] = http_status
    control_plane.update(observed_at="2026-10-18T19:36:26.575277868Z", limited=False, details=details)
    return json.dumps(
        {
            "schema_version": 1,
            "live": True,
            "ready": True,
            "snapshot_at": "2026-10-18T19:36:26.954526732Z",
            "runtime": {"version": "0.0.16+stub", "flavor": "full", "lifecycle": "running"},
            "components": {
                "cloudflared": {"status": "disabled", "state": "disabled", "limited": False},
                "control-plane": control_plane,
                "dispatcher": {"status": "ok", "state": "accepting", "limited": False},
                "mcp": {"status": "unknown", "state": "not_observed", "limited": False},
                "oauth": {"status": "ok", "state": "not_advertised", "limited": False},
            },
        },
        separators=(",", ":"),
    )


HEALTHY = _health("ok", "polling")
STARTING = _health("unknown", "polling")
NETWORK_BACKOFF = _health("degraded", "backoff", failures=5, category="network_error")
SERVER_ERROR = _health("degraded", "backoff", http_status=503, failures=10, category="http_error")


def _rejected(http_status: int, failures: int = 10, state: str = "backoff") -> str:
    return _health("degraded", state, http_status=http_status, failures=failures, category="http_error")


def _network(failures: int) -> str:
    return _health("degraded", "backoff", failures=failures, category="network_error")


def _rejected_polls(http_status: int, first: int, last: int) -> list[str]:
    """One answer per failure count from ``first`` to ``last``, the state alternating between
    backoff and polling like the real binary's between retries."""
    return [
        _rejected(http_status, failures, "backoff" if failures % 2 else "polling")
        for failures in range(first, last + 1)
    ]


def _rejections(http_status: int, upto: int = 10) -> list[str]:
    """The answers the watcher reads one per sample while OpenAI keeps rejecting: the start, then
    one more failed poll per sample."""
    return [STARTING, *_rejected_polls(http_status, 1, upto)]


# Real answers of tunnel-client v0.0.16 (linux-amd64, git sha 5f99daab...) on its health socket,
# 2026-10-18, clean environment, FAKE runtime key, SYNTHETIC tunnel id; every sample whose
# control-plane state changed: a run against api.openai.com (HTTP 401), a run whose proxy refused
# the connection (network_error) and the vendor's local control plane (`dev proxy`: ok).
REAL_HEALTH = REPO / "tests" / "fixtures" / "tunnel_client_v0.0.16"


def _real_health(name: str) -> list[str]:
    return (REAL_HEALTH / f"health_{name}.jsonl").read_text(encoding="utf-8").splitlines()


CONNECTED = "[local_mcp] connected to OpenAI (control plane polling); dot can use the plugin now"

# The error="..." values of tunnel-client v0.0.16's failed control-plane requests, as they are:
# - the owner's first live start (2026-10-19, the real api.openai.com, a valid sk-proj- key): its
#   tunnel log held exactly these three (x4, x1 and x1);
# - a FAKE key against api.openai.com (build container, 2026-10-18; OpenAI masked the key itself).
TUNNEL_USE_FORBIDDEN = (
    "controlplane client: unexpected status 401: tunnel_use_forbidden: Access denied: caller does not"
    " have permission to use this tunnel. mitigation: use credentials for a principal that has access to"
    " this tunnel, or recreate/connect the runtime in the owning org/workspace"
)
TUNNEL_NOT_FOUND = "controlplane client: unexpected metadata status 404: Tunnel not found"
OAUTH_DISCOVERY = (
    "oauth discovery invalid metadata from http://127.0.0.1:8010: decode protected resource metadata:"
    " invalid character 'N' looking for beginning of value"
)
MASKED_KEY = "sk-proj-" + "*" * 37 + "56cc"
INCORRECT_KEY = (
    f"Incorrect API key provided: {MASKED_KEY}. You can find your API key at"
    " https://platform.openai.com/account/api-keys."
)
INVALID_API_KEY = f"controlplane client: unexpected status 401: invalid_api_key: {INCORRECT_KEY}"


def _log_line(message: str, error: str, *, component: str = "controlplane", tail: str = "") -> str:
    """A WARN line in the struct-text format of tunnel-client v0.0.16 (Go quoting of the value;
    the other attributes SYNTHETIC)."""
    return (
        f"2026/10/19 08:48:31 WARN {message} client_instance_id={'3c' * 16} tunnel_id={TUNNEL_ID}"
        f" component={component} error={json.dumps(error)}{tail}"
    )


def _poll_failed(error: str, status: str = "401 Unauthorized", code: str = "") -> str:
    details = f" error_code={code}" if code else ""
    return _log_line(
        "poll failed; backing off",
        error,
        tail=f" retry_in_ms=200 tunnel_request_id=req_{'5e' * 16} status_code={status[:3]}"
        f" status={json.dumps(status)}{details}",
    )


def _metadata_failed(error: str, status: str) -> str:
    return _log_line("tunnel metadata fetch failed", error, tail=f" status_code={status[:3]}")


# The owner's tunnel log of 2026-10-19 (its three error values; the order of a real start: OAuth
# discovery, the first failed poll, the metadata fetch, more failed polls).
OWNER_LOG = [
    _log_line("OAuth discovery failed", OAUTH_DISCOVERY, component="oauth"),
    _poll_failed(TUNNEL_USE_FORBIDDEN, code="tunnel_use_forbidden"),
    _metadata_failed(TUNNEL_NOT_FOUND, "404"),
    *[_poll_failed(TUNNEL_USE_FORBIDDEN, code="tunnel_use_forbidden")] * 3,
]
FAKE_KEY_LOG = [
    _poll_failed(INVALID_API_KEY, code="invalid_api_key"),
    _metadata_failed(INVALID_API_KEY.replace("unexpected status", "unexpected metadata status"), "401"),
    *[_poll_failed(INVALID_API_KEY, code="invalid_api_key")] * 3,
]


@dataclass
class World:
    root: Path
    repo: Path
    home: Path
    bin: Path
    knobs: Path
    calls: Path
    env: dict[str, str]
    port: int

    @property
    def tunnel_dir(self) -> Path:
        return self.home / ".config" / "suv-deals" / "tunnel"

    @property
    def state(self) -> Path:
        return self.repo / "var" / "local_mcp"

    def run(
        self, *args: str, stdin: str = "", timeout: float = 120, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "local_mcp.sh"), *args],
            cwd=self.root,  # any directory: the script works in its repository root
            env={**self.env, **extra},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def popen(self, *args: str, new_session: bool = False, **extra: str) -> subprocess.Popen[str]:
        """``new_session``: its own process group, which a test can send Ctrl+C (SIGINT) to."""
        return subprocess.Popen(
            ["bash", str(self.repo / "scripts" / "local_mcp.sh"), *args],
            cwd=self.root,
            env={**self.env, **extra},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=new_session,
        )

    def wrap(self, name: str, body: str) -> None:
        """Put a wrapper for the real ``name`` first on PATH (``@REAL@`` is its path)."""
        real = shutil.which(name, path=os.environ["PATH"])
        assert real is not None, name
        wrapper = self.bin / name
        wrapper.write_text(
            "#!/bin/sh\n" + body.replace("@REAL@", real).replace("@KNOBS@", str(self.knobs)),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def record_every_command(self) -> list[str]:
        """Wrap every external command the script runs: each records its argv, then runs the real one."""
        wrapped = []
        for name in WRAPPED_COMMANDS:
            if shutil.which(name, path=os.environ["PATH"]) is None:
                continue
            self.wrap(name, f'printf "%s\\n" "{name} $*" >> "@KNOBS@/argv-commands.txt"\nexec @REAL@ "$@"\n')
            wrapped.append(name)
        return wrapped

    def calls_list(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def uv_calls(self) -> list[str]:
        return [c for c in self.calls_list() if c.startswith("uv ")]

    def knob(self, name: str, value: str) -> None:
        (self.knobs / name).write_text(value, encoding="utf-8")

    def health_sequence(self, *answers: str) -> None:
        """The tunnel's health JSON: one answer per request, in this order; the last one repeats.
        No answers: back to knobs/health_json."""
        (self.knobs / "health_seq_n").unlink(missing_ok=True)
        seq = self.knobs / "health_seq"
        if answers:
            seq.write_text("".join(f"{answer}\n" for answer in answers), encoding="utf-8")
        else:
            seq.unlink(missing_ok=True)

    def recorded(self) -> str:
        """Every argv and environment any stub saw (and every wrapped command's argv)."""
        parts = [self.calls.read_text(encoding="utf-8")] if self.calls.exists() else []
        parts += [p.read_text(encoding="utf-8") for p in self.knobs.glob("env-*.txt")]
        parts += [p.read_text(encoding="utf-8") for p in self.knobs.glob("argv-*.txt")]
        return "\n".join(parts)

    def setup(self, **extra: str) -> subprocess.CompletedProcess[str]:
        result = self.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n", **extra)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def credential(self) -> dict[str, object]:
        text = (self.tunnel_dir / "credential.json").read_text(encoding="utf-8")
        data: dict[str, object] = json.loads(text)
        return data

    def set_expiry(self, seconds_from_now: int) -> None:
        path = self.tunnel_dir / "credential.json"
        text = path.read_text(encoding="utf-8")
        epoch = int(time.time()) + seconds_from_now
        text = re.sub(r'"expires_at_epoch": \d+', f'"expires_at_epoch": {epoch}', text)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LOCAL_MCP, repo / "scripts" / "local_mcp.sh")
    (repo / ".env").write_text(
        f"DATABASE_URL=postgresql://owner:{DOTENV_SECRET}@db.example.invalid/x\n", encoding="utf-8"
    )
    # A short home: health.sock must fit a Unix socket path (107 bytes).
    short = Path(tempfile.mkdtemp(prefix="lmcp-", dir="/tmp"))
    home = short / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    knobs = tmp_path / "knobs"
    knobs.mkdir()
    calls = tmp_path / "calls.log"
    replacements = {
        "@CALLS@": str(calls),
        "@KNOBS@": str(knobs),
        "@TOOLS_SHA@": TOOLS_SHA,
        "@TOOLS@": json.dumps(VIEWER_TOOLS),
        "@CRED1@": CREDENTIAL_1,
        "@CRED2@": CREDENTIAL_2,
        "@TOKEN1@": TOKEN_1,
        "@TOKEN2@": TOKEN_2,
        "@WORKSPACE@": WORKSPACE,
        # The stubs' own python (so that a test can take python3 away from the script only).
        "@PYTHON@": shutil.which("python3") or "python3",
    }
    for name, template in (("uv", _STUB_UV), ("tunnel-client", _STUB_TUNNEL_CLIENT), ("curl", _STUB_CURL)):
        body = template
        for key, value in replacements.items():
            body = body.replace(key, value)
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    (knobs / "health_json").write_text(HEALTHY, encoding="utf-8")
    port = _free_port()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LOCAL_MCP_", "TUNNEL_CLIENT_", "MCP_", "CONTROL_PLANE_", "STUB_"))
    }
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HOME=str(home),
        LOCAL_MCP_PORT=str(port),
        LOCAL_MCP_READY_TIMEOUT="10",
        LOCAL_MCP_STOP_TIMEOUT="10",
        # Variables of the owner's shell that must never reach tunnel-client.
        OPENAI_API_KEY=SHELL_SECRET,
        CONTROL_PLANE_API_KEY=SHELL_SECRET,
        LOG_HTTP_RAW_UNSAFE="true",
        MCP_SERVER_URL="https://elsewhere.example.invalid/mcp",
    )
    w = World(tmp_path, repo, home, bin_dir, knobs, calls, env, port)
    try:
        yield w
    finally:
        for pid_file in w.state.glob("*.pid"):
            with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
        shutil.rmtree(short, ignore_errors=True)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^State:\s+Z", status, re.M) is None


def _assert_no_secret(*texts: str) -> None:
    blob = "\n".join(texts)
    for secret in SECRETS:
        assert secret not in blob
    assert TOKEN_1[7:] not in blob and TOKEN_2[7:] not in blob  # not even without the prefix


# --------------------------------------------------------------------------------------------
# Usage and static properties
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [("bogus",), (), ("start", "now"), ("--yes",)])
def test_unknown_subcommands_and_extra_arguments_are_usage_errors(args: tuple[str, ...]) -> None:
    result = subprocess.run(
        ["bash", str(LOCAL_MCP), *args], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 2
    assert "Usage" in result.stderr and result.stdout == ""


def test_help_names_every_subcommand_the_route_and_the_state_paths() -> None:
    result = subprocess.run(
        ["bash", str(LOCAL_MCP), "--help"], capture_output=True, text=True, check=True, timeout=30
    )
    for word in (
        "setup",
        "start",
        "stop",
        "status",
        "logs",
        "rotate",
        "var/local_mcp/",
        "MCP_AUTH_MODE=static_bearer",
        "SOURCE_NETWORK_ENABLED=false",
        "~/.config/suv-deals/tunnel",
        "read -rs",
        "never reads, prints or passes them",
        "130",
        "outside every git working tree",
    ):
        assert word in result.stdout, word


@pytest.mark.parametrize(
    "variables",
    [
        {"LOCAL_MCP_PORT": "80"},
        {"LOCAL_MCP_PORT": "8o10"},
        {"LOCAL_MCP_PORT": "70000"},
        {"LOCAL_MCP_TUNNEL_DIR": "relative/tunnel"},
        {"LOCAL_MCP_TUNNEL_DIR": "/home/someone/my tunnel"},
        {"LOCAL_MCP_WORKSPACE": "not-a-uuid"},
        {"LOCAL_MCP_READY_TIMEOUT": "0"},
    ],
)
def test_bad_environment_values_are_usage_errors(world: World, variables: dict[str, str]) -> None:
    result = world.run("status", **variables)
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stderr.startswith("error: ")


def test_the_script_never_touches_the_env_file_raw_logging_or_exports() -> None:
    text = LOCAL_MCP.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert ".env" not in code  # neither sourced, read, grepped nor echoed
    assert "http-raw-unsafe" not in text and "HTTP_RAW" not in text  # never enabled, not even named
    for forbidden in ("set -a", "printenv", "declare -x", "export ", "DATABASE_URL", "eval "):
        assert forbidden not in code, forbidden
    assert re.search(r"\benv\s*\|", code) is None and re.search(r"\benv\s*$", code, re.M) is None
    assert "set -euo pipefail" in text
    assert "read -rs" in code  # the API key and the tunnel id are read without echo
    assert "STATE_DIR=var/local_mcp" in code and "LOG_DIR=$STATE_DIR/logs" in code
    assert "var/local_crawl" not in code  # separate from local_crawl.sh (state and lock)
    # The secret files are referenced, never read: no `cat`/`$(<...)` of the key or the token file.
    assert re.search(r"\$\(<\s*\"\$(API_KEY_FILE|AUTH_FILE)\"\)", code) is None
    assert re.search(r"\bcat[ \t]+[^|\n]*\$\{?(API_KEY_FILE|AUTH_FILE)", code) is None


def test_both_prompts_are_hidden_and_secrets_are_written_by_the_builtin_printf_only() -> None:
    text = LOCAL_MCP.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    # Both values are read by prompt_hidden (`read -rs`); no other `read` touches them.
    assert re.findall(r"\bprompt_hidden (\w+) ", code) == ["tunnel_id_input", "api_key"]
    body = re.search(r"\nprompt_hidden\(\) \{\n(.*?)\n\}", code, re.S)
    assert body is not None and "IFS= read -rs value" in body.group(1)
    for line in re.findall(r"\bread\b[^\n]*", code):
        assert "api_key" not in line and "tunnel_id_input" not in line, line
    # The secret files are written by the builtin printf only: never an external printf (its argv
    # is world-readable in /proc), never a command by absolute path (it would bypass the wrapped
    # PATH of test_secrets_never_reach_the_argv_of_any_command).
    assert 'if ! printf \'%s\' "$2" >"$tmp"' in code
    assert re.search(r"\b(command|builtin|env|exec|xargs|nohup|setsid)\s+printf\b", code) is None
    assert re.search(r"/printf\b", code) is None
    assert re.search(r"(^|[\s;|&(!])/(usr/)?s?bin/", code, re.M) is None


# --------------------------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------------------------


def test_setup_writes_private_files_and_a_reference_only_yaml(world: World) -> None:
    result = world.setup()
    tunnel = world.tunnel_dir
    assert _mode(tunnel) == 0o700
    assert _mode(tunnel.parent) == 0o700  # created under umask 077
    key = tunnel / "control-plane-api-key"
    auth = tunnel / "mcp-authorization"
    assert _mode(key) == 0o600 and key.read_text(encoding="utf-8") == API_KEY  # exact, no newline
    assert _mode(auth) == 0o600 and auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_1}"
    assert (tunnel / "tunnel-id").read_text(encoding="utf-8") == TUNNEL_ID
    assert _mode(tunnel / "tunnel-client.yaml") == 0o600
    digest = hashlib.sha256((world.bin / "tunnel-client").read_bytes()).hexdigest()
    version = (tunnel / "tunnel-client.version").read_text(encoding="utf-8")
    assert version == f"tunnel-client v0.0.0-stub (linux/amd64)\nsha256 {digest}\n"
    assert f"sha256 {digest}" in result.stdout
    assert not list(tunnel.glob(".new.*"))  # no temporary file left

    credential = world.credential()
    assert credential["credential_id"] == CREDENTIAL_1
    assert credential["workspace_id"] == WORKSPACE
    assert credential["scopes"] == ["deals:read", "reviews:read"]
    assert credential["role"] == "viewer" and credential["expires_source"] == "create-mcp output"
    days_left = (int(str(credential["expires_at_epoch"])) - time.time()) / 86400
    assert 29.9 < days_left < 30.1

    config_text = (tunnel / "tunnel-client.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    assert config == {
        "config_version": 1,
        "control_plane": {"tunnel_id": TUNNEL_ID, "api_key": f"file:{key}"},
        "mcp": {
            "server_urls": [{"channel": "main", "url": f"http://127.0.0.1:{world.port}/mcp"}],
            "extra_headers": {"Authorization": f"file:{auth}"},
        },
        "log": {"level": "info"},
    }
    assert "raw" not in config_text.lower()

    tunnel_calls = [c for c in world.calls_list() if c.startswith("tunnel-client ")]
    assert tunnel_calls == [
        "tunnel-client --version",
        f"tunnel-client doctor --config {tunnel}/tunnel-client.yaml --health.unix-socket"
        f" {tunnel}/health.sock --explain",
    ]
    assert world.uv_calls() == [
        "uv run suv-deals credentials create-mcp --label dot read-only (tunnel) --role viewer"
        f" --scopes deals:read,reviews:read --expires 30d --authorization-file {auth} --yes"
        "|auth=|public=|events=|net="
    ]
    # Neither secret reaches the output, an argument, an environment or the YAML.
    _assert_no_secret(result.stdout, result.stderr, world.recorded(), config_text)
    # tunnel-client never sees the shell's CONTROL_PLANE_*/OPENAI_*/MCP_*/LOG_* variables.
    tunnel_env = (world.knobs / "env-tunnel.txt").read_text(encoding="utf-8")
    for name in ("OPENAI_API_KEY", "CONTROL_PLANE_API_KEY", "LOG_HTTP_RAW_UNSAFE", "MCP_SERVER_URL"):
        assert f"\n{name}=" not in tunnel_env, name
    assert SHELL_SECRET not in tunnel_env
    assert f"\nPATH={world.env['PATH']}" in tunnel_env and f"\nHOME={world.home}" in tunnel_env
    assert "setup complete" in result.stdout


def _read_until(fd: int, buffer: bytearray, needle: bytes | None, timeout: float = 60) -> None:
    """Read the pty into ``buffer`` until ``needle`` appears (``None``: until the other side closes)."""
    deadline = time.monotonic() + timeout
    while needle is None or needle not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {needle!r}; got {bytes(buffer)!r}")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # EIO: every writer of the terminal has exited
            chunk = b""
        if not chunk:
            if needle is None:
                return
            raise AssertionError(f"the terminal closed before {needle!r}; got {bytes(buffer)!r}")
        buffer.extend(chunk)


def _type_at_hidden_prompt(fd: int, text: bytes) -> None:
    """Type once `read -s` has turned the terminal's echo off (at most 5 s: a prompt that keeps the
    echo on gets the text anyway, and then the terminal shows it)."""
    deadline = time.monotonic() + 5
    while termios.tcgetattr(fd)[3] & termios.ECHO and time.monotonic() < deadline:
        time.sleep(0.01)
    os.write(fd, text)


def test_setup_reads_the_tunnel_id_and_the_api_key_without_echo(world: World) -> None:
    """On a real terminal: neither the tunnel id nor the key appears on the screen (or in its
    scrollback) while it is typed."""
    master, slave = os.openpty()
    process = subprocess.Popen(
        ["bash", str(world.repo / "scripts" / "local_mcp.sh"), "setup"],
        cwd=world.root,
        env=world.env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    screen = bytearray()
    try:
        _read_until(master, screen, b"tunnel id (tunnel_...; input hidden): ")
        _type_at_hidden_prompt(master, TUNNEL_ID.encode() + b"\n")
        _read_until(master, screen, b"OpenAI runtime API key (input hidden): ")
        _type_at_hidden_prompt(master, API_KEY.encode() + b"\n")
        _read_until(master, screen, None)
        assert process.wait(timeout=60) == 0, bytes(screen)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()
    text = screen.decode("utf-8", errors="replace")
    assert "setup complete" in text
    assert API_KEY not in text and API_KEY[-12:] not in text
    typed = text.split("tunnel id (tunnel_...; input hidden): ", 1)[1].split("saved: ", 1)[0]
    assert TUNNEL_ID not in typed  # shown only once it is saved (not a secret)
    assert (world.tunnel_dir / "control-plane-api-key").read_text(encoding="utf-8") == API_KEY


def test_secrets_never_reach_the_argv_of_any_command(world: World) -> None:
    """Every external command the script runs is wrapped and records its argv (world-readable in
    /proc while it runs); the static test above covers builtins and absolute paths."""
    wrapped = world.record_every_command()
    assert {"mktemp", "ln", "mv", "env", "setsid", "ps", "sha256sum", "python3"} <= set(wrapped)
    setup = world.setup()
    world.knob("run_mode", "exit:0:1")
    started = world.run("start")
    assert started.returncode == 0, started.stdout + started.stderr
    rotated = world.run("rotate")
    assert rotated.returncode == 0, rotated.stdout + rotated.stderr
    argv = (world.knobs / "argv-commands.txt").read_text(encoding="utf-8")
    for seen in ("mktemp -- ", "ln -- ", "sha256sum -- ", "setsid nohup env MCP_AUTH_MODE=static_bearer"):
        assert seen in argv, seen  # the wrappers really ran
    outputs = (setup.stdout, setup.stderr, started.stdout, started.stderr, rotated.stdout, rotated.stderr)
    _assert_no_secret(*outputs, world.recorded())


def test_setup_accepts_a_namespaced_tunnel_id(world: World) -> None:
    result = world.run("setup", stdin=f"{NAMESPACED_TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 0, result.stderr
    assert (world.tunnel_dir / "tunnel-id").read_text(encoding="utf-8") == NAMESPACED_TUNNEL_ID


def test_setup_is_idempotent_and_rewrites_the_yaml_for_a_new_port(world: World) -> None:
    world.setup()
    tunnel = world.tunnel_dir
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tunnel.iterdir() if p.is_file()}
    again = world.run("setup")  # no input needed: everything exists
    assert again.returncode == 0, again.stderr
    assert "tunnel-client.yaml: unchanged" in again.stdout
    assert "kept: " + TUNNEL_ID in again.stdout and "kept: credential " + CREDENTIAL_1 in again.stdout
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tunnel.iterdir() if p.is_file()}
    for name in ("control-plane-api-key", "mcp-authorization", "tunnel-id", "credential.json"):
        assert after[name] == before[name], name
    assert len([c for c in world.uv_calls() if "create-mcp" in c]) == 1

    other = _free_port()
    moved = world.run("setup", LOCAL_MCP_PORT=str(other))
    assert moved.returncode == 0, moved.stderr
    assert "tunnel-client.yaml: written" in moved.stdout
    config = yaml.safe_load((tunnel / "tunnel-client.yaml").read_text(encoding="utf-8"))
    assert config["mcp"]["server_urls"] == [{"channel": "main", "url": f"http://127.0.0.1:{other}/mcp"}]
    assert len([c for c in world.uv_calls() if "create-mcp" in c]) == 1
    _assert_no_secret(again.stdout, again.stderr, moved.stdout, moved.stderr)


def _unsafe_inside_repo(world: World) -> dict[str, str]:
    return {"LOCAL_MCP_TUNNEL_DIR": str(world.repo / "var" / "tunnel")}


def _unsafe_group_readable(world: World) -> dict[str, str]:
    world.tunnel_dir.mkdir(parents=True)
    world.tunnel_dir.chmod(0o750)
    return {}


def _unsafe_symlink(world: World) -> dict[str, str]:
    target = world.root / "elsewhere"
    target.mkdir(mode=0o700)
    world.tunnel_dir.parent.mkdir(parents=True)
    world.tunnel_dir.symlink_to(target)
    return {}


def _unsafe_windows_drive(world: World) -> dict[str, str]:
    return {"LOCAL_MCP_TUNNEL_DIR": "/mnt/c/Users/owner/tunnel"}


def _unsafe_link_into_repo(world: World) -> dict[str, str]:
    link = world.root / "link-to-repo"
    link.symlink_to(world.repo)
    return {"LOCAL_MCP_TUNNEL_DIR": str(link / "tunnel")}


def _unsafe_dotfiles_at_config(world: World) -> dict[str, str]:
    """A dotfiles repository at ~/.config: `git add -A` there would take the key and the token."""
    (world.home / ".config" / ".git").mkdir(parents=True)
    return {}


def _unsafe_dotfiles_at_home(world: World) -> dict[str, str]:
    (world.home / ".git").mkdir()
    (world.tunnel_dir).mkdir(parents=True, mode=0o700)  # an existing directory is refused too
    return {}


@pytest.mark.parametrize(
    ("arrange", "message"),
    [
        (_unsafe_inside_repo, "inside the repository"),
        (_unsafe_link_into_repo, "inside the repository"),
        (_unsafe_dotfiles_at_config, "inside a git working tree"),
        (_unsafe_dotfiles_at_home, "inside a git working tree"),
        (_unsafe_group_readable, "chmod 700"),
        (_unsafe_symlink, "is a symlink"),
        (_unsafe_windows_drive, "not under /mnt"),
    ],
)
def test_setup_refuses_an_unsafe_tunnel_directory(world: World, arrange: object, message: str) -> None:
    extra = arrange(world)  # type: ignore[operator]
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n", **extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert not list(world.root.rglob("control-plane-api-key"))
    assert not world.uv_calls()  # no credential was created
    _assert_no_secret(result.stdout, result.stderr, world.recorded())


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to give the directory another owner")
def test_setup_refuses_a_directory_owned_by_someone_else(world: World) -> None:
    world.tunnel_dir.mkdir(parents=True, mode=0o700)
    os.chown(world.tunnel_dir, 4242, 4242)
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1
    assert "not owned by the current user" in result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "tunnel_abc",
        "tunnel_" + "0123456789ABCDEF" * 2,
        "tunnel_abc_" + "0" * 32,
        API_KEY,  # the key pasted into the tunnel id prompt
        TUNNEL_ID + " ",
    ],
)
def test_setup_validates_the_tunnel_id_without_echoing_it(world: World, bad: str) -> None:
    result = world.run("setup", stdin=f"{bad}\n{API_KEY}\n")
    assert result.returncode == 1
    assert "nothing was written" in result.stderr
    assert not (world.tunnel_dir / "tunnel-id").exists()
    assert not (world.tunnel_dir / "control-plane-api-key").exists()
    if bad:
        assert bad.strip() not in result.stdout + result.stderr
    _assert_no_secret(result.stdout, result.stderr, world.recorded())


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("", "no API key entered"),
        ("sk-short", "does not look like an API key"),
        ("sk-proj-FAKE key with spaces 0123456789", "does not look like an API key"),
        ("sk-admin-FAKE-" + "0" * 30, "admin key"),
        (TUNNEL_ID, "tunnel id, not the API key"),
        (TOKEN_1, "MCP credential"),
    ],
)
def test_setup_validates_the_api_key_without_echoing_it(world: World, bad: str, message: str) -> None:
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{bad}\n")
    assert result.returncode == 1
    assert message in result.stderr
    assert not (world.tunnel_dir / "control-plane-api-key").exists()
    assert not world.uv_calls()
    if len(bad) > 8 and bad != TUNNEL_ID:  # the tunnel id itself is shown once it is saved (step 3)
        assert bad not in result.stdout + result.stderr


def test_setup_without_tunnel_client_says_where_to_get_it(world: World) -> None:
    (world.bin / "tunnel-client").unlink()
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1
    for words in (
        "https://platform.openai.com/settings/organization/tunnels",
        "~/.local/bin",
        "chmod +x",
        "TUNNEL_CLIENT_BIN",
    ):
        assert words in result.stderr, words
    assert not world.tunnel_dir.exists()  # checked before anything is created


def test_setup_uses_tunnel_client_bin(world: World) -> None:
    moved = world.root / "vendor" / "tc"
    moved.parent.mkdir()
    (world.bin / "tunnel-client").rename(moved)
    world.setup(TUNNEL_CLIENT_BIN=str(moved))
    assert f"{moved}: tunnel-client v0.0.0-stub" in world.run("setup", TUNNEL_CLIENT_BIN=str(moved)).stdout


def test_setup_stops_when_the_credential_file_is_not_recorded(world: World) -> None:
    world.tunnel_dir.mkdir(parents=True, mode=0o700)
    orphan = world.tunnel_dir / "mcp-authorization"
    orphan.write_text(f"Bearer {TOKEN_1}", encoding="utf-8")
    orphan.chmod(0o600)
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1
    assert "credentials list" in result.stderr and "revoke" in result.stderr
    assert not [c for c in world.uv_calls() if "create-mcp" in c]
    _assert_no_secret(result.stdout, result.stderr)


@pytest.mark.parametrize("how", ["deleted", "symlink"])
def test_setup_revokes_the_recorded_credential_before_it_creates_another(world: World, how: str) -> None:
    world.setup()
    auth = world.tunnel_dir / "mcp-authorization"
    if how == "symlink":
        moved = world.root / "moved-authorization"
        auth.rename(moved)
        auth.symlink_to(moved)
        refused = world.run("start")
        assert refused.returncode == 1 and "is a symlink; delete it" in refused.stderr
    auth.unlink()  # what the script's messages say to do
    again = world.run("setup")
    assert again.returncode == 0, again.stdout + again.stderr
    uv = world.uv_calls()
    revoke = _index(uv, "uv run suv-deals credentials revoke")
    assert uv[revoke] == (
        f"uv run suv-deals credentials revoke {CREDENTIAL_1} --reason replaced by scripts/local_mcp.sh"
        f" setup --workspace {WORKSPACE} --yes|auth=|public=|events=|net="
    )
    creates = [i for i, c in enumerate(uv) if "create-mcp" in c]
    assert len(creates) == 2 and creates[0] < revoke < creates[1]
    assert f"names credential {CREDENTIAL_1}, but {auth} is gone: revoking it first" in again.stdout
    assert world.credential()["credential_id"] == CREDENTIAL_2
    assert auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_2}"
    _assert_no_secret(again.stdout, again.stderr)


def test_setup_creates_nothing_when_revoking_the_recorded_credential_fails(world: World) -> None:
    world.setup()
    (world.tunnel_dir / "mcp-authorization").unlink()
    result = world.run("setup", STUB_REVOKE_EXIT="1")
    assert result.returncode == 1
    assert f"revoking the recorded credential {CREDENTIAL_1} failed" in result.stderr
    assert f"credentials revoke {CREDENTIAL_1} --workspace {WORKSPACE}" in result.stderr
    assert len([c for c in world.uv_calls() if "create-mcp" in c]) == 1
    assert world.credential()["credential_id"] == CREDENTIAL_1  # still recorded
    assert not (world.tunnel_dir / "mcp-authorization").exists()


_FAILING_MKTEMP = (
    'if [ -e "@KNOBS@/mktemp_fail" ]; then echo "mktemp: No space left on device" >&2; exit 1; fi\n'
    'exec @REAL@ "$@"\n'
)


def test_setup_takes_the_new_credential_back_when_it_cannot_record_it(world: World) -> None:
    world.setup()
    tunnel = world.tunnel_dir
    (tunnel / "mcp-authorization").unlink()
    (tunnel / "credential.json").unlink()
    world.wrap("mktemp", _FAILING_MKTEMP)
    world.knob("mktemp_fails_after_create", "1")  # the disk fills up right after create-mcp
    result = world.run("setup")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"taking the new credential {CREDENTIAL_2} back" in result.stderr
    assert [c for c in world.uv_calls() if "credentials revoke" in c] == [
        f"uv run suv-deals credentials revoke {CREDENTIAL_2} --reason setup could not record it"
        f" --workspace {WORKSPACE} --yes|auth=|public=|events=|net="
    ]
    assert not (tunnel / "mcp-authorization").exists() and not (tunnel / "credential.json").exists()
    assert not list(tunnel.glob(".new.*"))


def test_setup_accepts_a_doctor_that_fails_only_the_checks_that_need_the_api(world: World) -> None:
    """Before `start` nothing answers the MCP URL: the real doctor (v0.0.16) then fails exactly
    mcp_server_reachable and oauth_metadata (exit 2); setup shows its output and succeeds."""
    result = world.setup()
    out = result.stdout
    assert "    CHECK mcp_server_reachable     FAIL dial tcp 127.0.0.1:" in out  # the output, indented
    sock = world.tunnel_dir / "health.sock"
    assert f"    CHECK health_listener          PASS will bind unix socket {sock}" in out
    assert "    FAILED_CHECKS mcp_server_reachable,oauth_metadata" in out
    assert (
        "doctor: only mcp_server_reachable and oauth_metadata failed. That is expected before"
        " scripts/local_mcp.sh start: the API is not running yet" in out
    )
    assert "start runs doctor again with the API up" in out and "setup complete" in out
    assert "error" not in result.stderr


def test_setup_reports_doctor_ok_when_the_mcp_url_answers(world: World) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", world.port))
        listener.listen(4)
        result = world.setup()
    assert "    RESULT ok" in result.stdout and "  doctor: RESULT ok" in result.stdout
    assert "expected before" not in result.stdout
    assert (
        f"note: something already answers http://127.0.0.1:{world.port}/mcp; start needs port {world.port}"
        " free" in result.stdout
    )


@pytest.mark.parametrize(
    ("knob", "answering"),
    [
        # v0.0.16 with another program on the port (one answering 405, HTML or nothing at all):
        # mcp_server_reachable passes on the TCP connect, oauth_metadata fails.
        ("doctor_oauth", "mcp_server_reachable passed: a program answers http://127.0.0.1:{port}/mcp"),
        # doctor reports the URL unreachable, but something listens on the port all the same.
        ("doctor_mcp", "a program listens on 127.0.0.1:{port}"),
    ],
)
def test_setup_refuses_when_something_else_answers_the_mcp_url(
    world: World, knob: str, answering: str
) -> None:
    """Before `start` only "nothing answers the MCP URL" makes doctor's failures expected; another
    program there would make `start` refuse the port."""
    world.knob(knob, "invalid" if knob == "doctor_oauth" else "fail")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", world.port))
        listener.listen(4)
        result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1, result.stdout + result.stderr
    failed = "oauth_metadata" if knob == "doctor_oauth" else "mcp_server_reachable,oauth_metadata"
    assert (
        f"error: tunnel-client doctor failed (exit 2; failed checks: {failed}) and"
        f" {answering.format(port=world.port)}, which is not this route's API" in result.stderr
    )
    assert f"start would refuse port {world.port}: stop that program or choose a free port" in result.stderr
    assert "nothing answers" not in result.stdout and "setup complete" not in result.stdout
    assert (world.tunnel_dir / "tunnel-client.yaml").is_file()  # everything else is in place
    _assert_no_secret(result.stdout, result.stderr)


@pytest.mark.parametrize(
    ("knobs", "message"),
    [
        (
            {"doctor_extra_failed": "control_plane_api_key"},
            "failed checks other than mcp_server_reachable and oauth_metadata: control_plane_api_key"
            " (all: mcp_server_reachable,oauth_metadata,control_plane_api_key)",
        ),
        (
            {"doctor_mode": "no_failed_line"},
            "its output (above) has no FAILED_CHECKS line naming the failed checks",
        ),
        ({"doctor_mode": "garbage"}, "its output (above) has no FAILED_CHECKS line naming the failed checks"),
    ],
)
def test_setup_fails_on_any_other_doctor_result(world: World, knobs: dict[str, str], message: str) -> None:
    for name, value in knobs.items():
        world.knob(name, value)
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "error: tunnel-client doctor failed (exit 2)" in result.stderr
    assert message in result.stderr
    assert "setup complete" not in result.stdout
    assert (world.tunnel_dir / "tunnel-client.yaml").is_file()  # everything else is in place
    _assert_no_secret(result.stdout, result.stderr)


def test_setup_removes_a_stale_health_socket_before_doctor(world: World) -> None:
    """A socket left by a killed tunnel would make doctor's health_listener check fail (the real
    v0.0.16: "listen unix ...: bind: address already in use"; the stub doctor does the same)."""
    world.setup()
    stale = world.tunnel_dir / "health.sock"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(stale))
    result = world.run("setup")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not stale.exists()
    assert f"    CHECK health_listener          PASS will bind unix socket {stale}" in result.stdout
    assert "doctor: only mcp_server_reachable and oauth_metadata failed" in result.stdout


def test_setup_refuses_a_world_readable_secret_file(world: World) -> None:
    world.setup()
    (world.tunnel_dir / "control-plane-api-key").chmod(0o644)
    result = world.run("setup")
    assert result.returncode == 1
    assert "chmod 600" in result.stderr


def test_a_second_command_is_refused_while_one_holds_the_lock(world: World) -> None:
    world.state.mkdir(parents=True)
    with (world.state / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for command in ("setup", "start", "stop", "rotate"):
            result = world.run(command, stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
            assert result.returncode == 1, command
            assert "another scripts/local_mcp.sh setup, start, stop or rotate is running" in result.stderr
    assert not world.calls_list()


# --------------------------------------------------------------------------------------------
# start, status, stop
# --------------------------------------------------------------------------------------------


def _index(calls: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


def test_start_runs_the_steps_in_order_and_stops_the_api_after_the_tunnel(world: World) -> None:
    world.setup()
    world.knob("run_mode", "exit:0:1")  # the tunnel stops by itself after 1 s
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    tunnel = world.tunnel_dir
    calls = [c for c in world.calls_list() if "create-mcp" not in c and "--version" not in c]
    calls = calls[_index(calls, "uv run suv-deals doctor") :]
    route_env = f"|auth=static_bearer|public=http://127.0.0.1:{world.port}/mcp|events=false|net=false"
    assert calls[0] == "uv run suv-deals doctor --process api" + route_env
    assert calls[1] == (
        f"uv run suv-deals api serve --host 127.0.0.1 --port {world.port} --no-proxy-headers" + route_env
    )
    readyz = _index(calls, "curl ")
    assert f"http://127.0.0.1:{world.port}/readyz" in calls[readyz] and "--noproxy *" in calls[readyz]
    probe = _index(calls, "uv run suv-deals mcp probe")
    assert calls[probe] == (
        f"uv run suv-deals mcp probe --url http://127.0.0.1:{world.port}/mcp --authorization-file"
        f" {tunnel}/mcp-authorization --json --timeout 10|auth=|public=|events=|net="
    )
    doctor = _index(calls, "tunnel-client doctor")
    assert calls[doctor] == (
        f"tunnel-client doctor --config {tunnel}/tunnel-client.yaml --health.unix-socket"
        f" {tunnel}/health.sock --explain"
    )
    run = _index(calls, "tunnel-client run")
    assert calls[run] == (
        f"tunnel-client run --config {tunnel}/tunnel-client.yaml --health.unix-socket {tunnel}/health.sock"
        " --health.listen-addr 127.0.0.1:0"
    )
    # tunnel-client's doctor runs after the probe, with the API up, and before the tunnel.
    assert readyz < probe < doctor < run < calls.index("api: TERM")
    assert not any("http-raw-unsafe" in c or "--log." in c for c in world.calls_list())

    out = result.stdout
    assert "Keep this window open while dot uses the system; Ctrl+C stops the tunnel and the API" in out
    assert f"7 tool(s); tools/list sha256 {TOOLS_SHA}" in out and "protocol 2025-11-25" in out
    assert "5/6 tunnel-client doctor with the API up" in out and "6/6 tunnel-client run" in out
    assert f"  mcp_server_reachable: PASS HTTP 405 from http://127.0.0.1:{world.port}/mcp" in out
    assert f"  health_listener: PASS will bind unix socket {tunnel}/health.sock" in out
    assert "  RESULT ok" in out
    doctor_file = next((world.state / "logs").glob("doctor-*.txt")).read_text(encoding="utf-8")
    assert "RESULT ok" in doctor_file and "CHECK mcp_server_reachable" in doctor_file
    tunnel_log = next((world.state / "logs").glob("tunnel-*.log"))
    assert "tunnel stub: polling" in tunnel_log.read_text(encoding="utf-8")  # its log, not the window
    assert "api: stopped" in out
    assert f"127.0.0.1:{world.port} is the API's own socket" in out
    assert re.fullmatch(
        r"var/local_mcp/logs/api-\d{8}T\d{6}Z\.log", (world.state / "api.logfile").read_text().strip()
    )
    probe_report = json.loads(next((world.state / "logs").glob("probe-*.json")).read_text(encoding="utf-8"))
    assert probe_report["tools_sha256"] == TOOLS_SHA
    assert not list(world.state.glob("*.pid")) and not (tunnel / "health.sock").exists()
    _assert_no_secret(
        out, result.stderr, world.recorded(), tunnel_log.read_text(encoding="utf-8"), doctor_file
    )
    tunnel_env = (world.knobs / "env-tunnel.txt").read_text(encoding="utf-8")
    assert SHELL_SECRET not in tunnel_env and "LOG_HTTP_RAW_UNSAFE" not in tunnel_env


def _wait_for(predicate: object, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.05)


def test_status_and_stop_from_another_window(world: World) -> None:
    world.setup()
    started = world.popen("start")
    try:
        _wait_for(lambda: (world.knobs / "tunnel.running").exists() and (world.state / "tunnel.pid").exists())
        _wait_for((world.tunnel_dir / "health.sock").is_socket)
        api_pid = int((world.state / "api.pid").read_text(encoding="utf-8"))
        tunnel_pid = int((world.state / "tunnel.pid").read_text(encoding="utf-8"))
        assert _alive(api_pid) and _alive(tunnel_pid)

        status = world.run("status")
        assert status.returncode == 0, status.stdout + status.stderr
        out = status.stdout
        assert f"api: running (pid {api_pid}), http://127.0.0.1:{world.port}/mcp; /readyz HTTP 200" in out
        assert f"tunnel: running (pid {tunnel_pid}); health socket /readyz HTTP 200" in out
        assert (
            "  control plane: status ok, state polling, http_status -, consecutive_failures 0,"
            " failure_category -" in out
        )
        assert re.search(rf"credential {CREDENTIAL_1}: expires in (29|30) day\(s\)", out), out
        assert "control-plane-api-key: present, mode 600" in out
        assert "mcp-authorization: present, mode 600" in out
        assert "log: var/local_mcp/logs/api-" in out and "stub api started" in out
        assert "log: var/local_mcp/logs/tunnel-" in out and "tunnel stub: polling" in out
        assert "tunnel-client: tunnel-client v0.0.0-stub (linux/amd64) (sha256 " in out
        _assert_no_secret(out, status.stderr)

        again = world.run("start")
        assert again.returncode == 1 and "already running" in again.stderr

        stopped = world.run("stop")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert stopped.stdout.index("tunnel (pid") < stopped.stdout.index("tunnel: stopped")
        assert stopped.stdout.index("tunnel: stopped") < stopped.stdout.index("api")
        out_start, err_start = started.communicate(timeout=60)
        assert started.returncode == 0, out_start + err_start
        calls = world.calls_list()
        assert calls.index("tunnel: TERM") < calls.index("api: TERM")
        assert calls.count("api: TERM") == 1 and calls.count("tunnel: TERM") == 1  # one signal each
        _wait_for(lambda: not _alive(api_pid) and not _alive(tunnel_pid))
        assert not list(world.state.glob("*.pid")) and not list(world.state.glob("*.stopping"))

        after = world.run("status")
        assert after.returncode == 1
        assert "api: not running" in after.stdout and "tunnel: not running" in after.stdout
    finally:
        _end(world, started)


def _end(world: World, process: subprocess.Popen[str]) -> None:
    """Stop a backgrounded ``start`` and everything it launched (the API and the watcher keep the
    pipes open)."""
    for pid_file in world.state.glob("*.pid"):
        with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
            os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=15)


def test_start_stops_the_api_when_the_tunnel_fails_and_prints_the_last_log_lines(world: World) -> None:
    world.setup()
    world.knob("run_mode", "exit:3:0")
    result = world.run("start")
    assert result.returncode == 1
    tunnel_log = next((world.state / "logs").glob("tunnel-*.log"))
    heading = f"the tunnel exited with status 3; last lines of var/local_mcp/logs/{tunnel_log.name}:"
    assert heading in result.stderr
    # The last 20 lines of the 27 the stub wrote: its exit message and the last start-up lines ...
    assert "tunnel stub: exiting with 3" in result.stderr
    assert "INFO provided constructor=stub.fx25()" in result.stderr
    assert "constructor=stub.fx7()" not in result.stderr  # ... not the first ones
    assert "tunnel stub" not in result.stdout  # never in the window otherwise
    assert "api: TERM" in world.calls_list() and "api: stopped" in result.stdout
    assert not list(world.state.glob("*.pid"))


def test_tunnel_output_goes_to_its_log_only_and_the_window_stays_concise(world: World) -> None:
    world.setup()
    world.knob("run_mode", "exit:0:2")
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    window = result.stdout + result.stderr
    stub_lines = (
        "INFO provided constructor=stub.fx1()",
        "tunnel stub: polling",
        "tunnel stub: exiting with 0",
    )
    for line in stub_lines:
        assert line not in window, line
    tunnel_log = next((world.state / "logs").glob("tunnel-*.log"))
    log = tunnel_log.read_text(encoding="utf-8")
    assert log.splitlines()[0] == "INFO provided constructor=stub.fx1()"
    assert "tunnel stub: polling" in log and log.endswith("tunnel stub: exiting with 0\n")
    assert f"its own output goes to var/local_mcp/logs/{tunnel_log.name}" in window
    assert CONNECTED in result.stdout  # the watcher's lines are what the window shows
    assert "tunnel: stopped (exit 0); stopping the API" in result.stdout


def _wait_for_tunnel(world: World) -> tuple[int, int]:
    _wait_for(lambda: (world.knobs / "tunnel.running").exists() and (world.state / "tunnel.pid").exists())
    _wait_for((world.tunnel_dir / "health.sock").is_socket)
    api_pid = int((world.state / "api.pid").read_text(encoding="utf-8"))
    tunnel_pid = int((world.state / "tunnel.pid").read_text(encoding="utf-8"))
    return api_pid, tunnel_pid


def test_start_stops_the_tunnel_when_the_api_dies(world: World) -> None:
    """Otherwise tunnel-client would send the credential to whatever binds the port next."""
    world.setup()
    started = world.popen("start")
    try:
        api_pid, tunnel_pid = _wait_for_tunnel(world)
        os.killpg(api_pid, signal.SIGKILL)  # the API's session: the stub and its listener
        out, err = started.communicate(timeout=60)
        assert started.returncode == 1, out + err
        assert "the API exited while the tunnel ran" in err
        calls = world.calls_list()
        assert "tunnel: TERM" in calls and "api: TERM" not in calls
        assert f"tunnel (pid {tunnel_pid}): SIGTERM sent" in out
        _wait_for(lambda: not _alive(tunnel_pid))
        assert not list(world.state.glob("*.pid")) and not (world.tunnel_dir / "health.sock").exists()
        assert not (world.state / "api.lost").exists()
    finally:
        _end(world, started)


def test_start_sends_the_credential_only_to_the_api_it_launched(world: World) -> None:
    world.setup()
    world.knob("api_listener", "foreign")  # another program owns the port the API should have
    try:
        result = world.run("start")
    finally:
        foreign = world.knobs / "foreign.pid"
        if foreign.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(foreign.read_text(encoding="utf-8")), signal.SIGKILL)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"127.0.0.1:{world.port} is not served by the API this start launched" in result.stderr
    calls = world.calls_list()
    assert not any("mcp probe" in c for c in calls)  # the credential was never sent
    assert not any(c.startswith("tunnel-client run") for c in calls)
    assert "api: TERM" in calls
    assert not list(world.state.glob("*.pid"))


def test_the_start_window_never_signals_a_reused_api_pid(world: World) -> None:
    world.setup()
    marker = world.root / "decoy-got-term"
    decoy = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import pathlib, signal, sys, time\n"
            "def term(*_):\n"
            "    pathlib.Path(sys.argv[1]).write_text('TERM')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\n"
            "time.sleep(600)",
            str(marker),
        ]
    )
    started = world.popen("start", new_session=True)
    try:
        api_pid, _ = _wait_for_tunnel(world)
        os.killpg(api_pid, signal.SIGKILL)
        # The API's pid now names another process (pid reuse, simulated).
        (world.state / "api.pid").write_text(f"{decoy.pid}\n", encoding="utf-8")
        try:
            out, err = started.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(started.pid, signal.SIGINT)
            out, err = started.communicate(timeout=30)
        assert f"api (pid {decoy.pid}): SIGTERM sent" not in out
        assert not marker.exists() and decoy.poll() is None
        assert started.returncode == 1, out + err  # the watcher saw the API gone
    finally:
        decoy.kill()
        decoy.wait()
        _end(world, started)


def test_ctrl_c_in_the_start_window_stops_the_tunnel_then_the_api(world: World) -> None:
    world.setup()
    started = world.popen("start", new_session=True)
    try:
        _wait_for_tunnel(world)
        os.killpg(started.pid, signal.SIGINT)  # Ctrl+C reaches the window's foreground group
        out, err = started.communicate(timeout=60)
        assert started.returncode == 0, out + err
        calls = world.calls_list()
        assert calls.index("tunnel: INT") < calls.index("api: TERM")
        assert calls.count("api: TERM") == 1
        assert "tunnel: stopped (exit 130); stopping the API" in out and "api: stopped" in out
        assert not list(world.state.glob("*.pid")) and not (world.tunnel_dir / "health.sock").exists()
    finally:
        _end(world, started)


def test_ctrl_c_before_the_tunnel_runs_exits_130_and_stops_the_api(world: World) -> None:
    world.setup()
    world.knob("readyz_code", "503")  # start waits in step 3
    started = world.popen("start", new_session=True, LOCAL_MCP_READY_TIMEOUT="60")
    try:
        _wait_for(lambda: any("api serve" in c for c in world.calls_list()))
        _wait_for(lambda: any(c.startswith("curl ") for c in world.calls_list()))
        os.killpg(started.pid, signal.SIGINT)
        out, err = started.communicate(timeout=60)
        assert started.returncode == 130, out + err
        calls = world.calls_list()
        assert "api: TERM" in calls and "api: stopped" in out
        assert not any(c.startswith("tunnel-client run") or "mcp probe" in c for c in calls)
        assert not list(world.state.glob("*.pid"))
    finally:
        _end(world, started)


def test_status_and_stop_read_whole_command_lines_whatever_columns_says(world: World) -> None:
    """procps cuts `ps -o args=` to an exported COLUMNS unless -ww is given."""
    world.setup()
    started = world.popen("start")
    try:
        _wait_for_tunnel(world)
        status = world.run("status", COLUMNS="80")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "api: running" in status.stdout and "tunnel: running" in status.stdout
        stopped = world.run("stop", COLUMNS="80")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "tunnel: stopped" in stopped.stdout  # the API: by `stop` or by the start window
        out, err = started.communicate(timeout=60)
        assert started.returncode == 0, out + err
        assert "tunnel: TERM" in world.calls_list() and "api: TERM" in world.calls_list()
        assert not list(world.state.glob("*.pid"))
    finally:
        _end(world, started)


def test_another_tunnel_directory_never_stops_or_forgets_the_running_tunnel(world: World) -> None:
    world.setup()
    started = world.popen("start")
    other = {"LOCAL_MCP_TUNNEL_DIR": str(world.home / "other-tunnel")}
    try:
        api_pid, tunnel_pid = _wait_for_tunnel(world)
        stopped = world.run("stop", **other)
        assert stopped.returncode == 1, stopped.stdout + stopped.stderr
        assert "a tunnel started with another" in stopped.stderr and "Nothing was stopped" in stopped.stderr
        assert _alive(api_pid) and _alive(tunnel_pid)
        assert (world.state / "tunnel.pid").exists() and (world.state / "api.pid").exists()
        status = world.run("status", **other)
        assert status.returncode == 1
        expected = f"tunnel: one started with another LOCAL_MCP_TUNNEL_DIR runs (pid {tunnel_pid}"
        assert expected in status.stdout
        refused = world.run("start", **other)
        assert refused.returncode == 1
        assert "a tunnel started with another LOCAL_MCP_TUNNEL_DIR is running" in refused.stderr
        assert world.run("stop").returncode == 0  # the right directory stops it
        out, err = started.communicate(timeout=60)
        assert started.returncode == 0, out + err
        assert not list(world.state.glob("*.pid"))
    finally:
        _end(world, started)


def test_start_refuses_without_setup(world: World) -> None:
    result = world.run("start")
    assert result.returncode == 1
    assert "setup is incomplete" in result.stderr
    assert not world.calls_list()


def test_start_refuses_an_expired_credential(world: World) -> None:
    world.setup()
    world.set_expiry(-60)
    result = world.run("start")
    assert result.returncode == 1
    assert "expired" in result.stderr and "scripts/local_mcp.sh rotate" in result.stderr
    assert not [c for c in world.uv_calls() if "create-mcp" not in c]


def test_start_warns_when_the_credential_expires_within_five_days(world: World) -> None:
    world.setup()
    world.set_expiry(2 * 86400 + 3600)
    world.knob("run_mode", "exit:0:0")
    result = world.run("start")
    assert result.returncode == 0, result.stderr
    assert "WARNING: the MCP credential expires in 2 day(s)" in result.stderr
    status = world.run("status")
    assert "expires in 2 day(s)" in status.stdout and "rotate soon" in status.stdout


def test_start_refuses_a_port_in_use(world: World) -> None:
    world.setup()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", world.port))
        listener.listen(1)
        result = world.run("start")
    assert result.returncode == 1
    assert f"port {world.port} on 127.0.0.1 is already in use" in result.stderr
    assert not [c for c in world.uv_calls() if "create-mcp" not in c]


def test_start_refuses_while_the_api_runs(world: World) -> None:
    world.setup()
    fake = subprocess.Popen(
        [
            "bash",
            str(world.bin / "uv"),
            "run",
            "suv-deals",
            "api",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "1",
        ],
        env=world.env,
        stdout=subprocess.DEVNULL,
    )
    try:
        world.state.mkdir(parents=True, exist_ok=True)
        (world.state / "api.pid").write_text(f"{fake.pid}\n", encoding="utf-8")
        cmdline = Path(f"/proc/{fake.pid}/cmdline")
        _wait_for(lambda: "suv-deals api serve" in cmdline.read_text().replace("\0", " "))
        result = world.run("start")
        assert result.returncode == 1
        assert f"the api is already running (pid {fake.pid})" in result.stderr
        # A pid file pointing to another program is not "running".
        (world.state / "api.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        world.knob("run_mode", "exit:0:0")
        assert world.run("start").returncode == 0
    finally:
        fake.kill()
        fake.wait()


@pytest.mark.parametrize(
    ("extra", "message", "reached"),
    [
        ({"STUB_DOCTOR_EXIT": "1"}, "doctor reported an error; nothing was started", "doctor"),
        ({"STUB_PROBE_EXIT": "1"}, "the probe failed", "mcp probe"),
    ],
)
def test_start_aborts_at_a_failed_check(
    world: World, extra: dict[str, str], message: str, reached: str
) -> None:
    world.setup()
    result = world.run("start", **extra)
    assert result.returncode == 1
    assert message in result.stderr
    assert not any(c.startswith("tunnel-client run") for c in world.calls_list())
    if reached == "mcp probe":
        assert "FAIL  initialize: HTTP 401" in result.stdout
        assert "api: TERM" in world.calls_list()  # the API this start launched is stopped again
    else:
        assert not any("api serve" in c for c in world.calls_list())
    assert not list(world.state.glob("*.pid"))


def test_start_stops_the_api_when_it_never_gets_ready(world: World) -> None:
    world.setup()
    world.knob("readyz_code", "503")
    result = world.run("start", LOCAL_MCP_READY_TIMEOUT="1")
    assert result.returncode == 1
    assert "/readyz answered HTTP 503" in result.stderr
    assert "api: TERM" in world.calls_list()
    assert not any("mcp probe" in c for c in world.calls_list())


def test_start_rewrites_the_yaml_for_another_port(world: World) -> None:
    world.setup()
    other = _free_port()
    world.knob("run_mode", "exit:0:0")
    result = world.run("start", LOCAL_MCP_PORT=str(other))
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((world.tunnel_dir / "tunnel-client.yaml").read_text(encoding="utf-8"))
    assert config["mcp"]["server_urls"][0]["url"] == f"http://127.0.0.1:{other}/mcp"
    assert any(f"--port {other} " in c for c in world.uv_calls())


def test_start_refuses_when_tunnel_client_doctor_fails_with_the_api_up(world: World) -> None:
    world.setup()
    world.knob("doctor_extra_failed", "control_plane_api_key")
    world.knob("run_mode", "exit:0:1")
    result = world.run("start")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "tunnel-client doctor failed with the API up (exit 2; failed checks: control_plane_api_key);"
        " the explanation is above; the tunnel was not started" in result.stderr
    )
    assert "  CHECK control_plane_api_key " in result.stderr  # its output is shown
    calls = world.calls_list()
    assert _index(calls, "uv run suv-deals mcp probe") < max(
        i for i, c in enumerate(calls) if c.startswith("tunnel-client doctor")
    )
    assert not any(c.startswith("tunnel-client run") for c in calls)
    assert "api: TERM" in calls and "api: stopped" in result.stdout  # the EXIT trap stopped the API
    assert not list(world.state.glob("*.pid"))


# --------------------------------------------------------------------------------------------
# Readiness: the control-plane component of the tunnel's health, not /readyz alone
# --------------------------------------------------------------------------------------------


def test_connected_is_announced_once_the_control_plane_is_ok(world: World) -> None:
    world.setup()
    world.knob("run_mode", "exit:0:7")  # long enough for a few health polls after "connected"
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(CONNECTED) == 1
    assert "warning" not in result.stderr and "has not connected" not in result.stdout
    assert any("--unix-socket" in c and "/health?details=true" in c for c in world.calls_list())


def test_readyz_200_alone_is_not_connected(world: World) -> None:
    """/readyz answers 200 while the control plane is not ok (the real binary does so even while
    OpenAI rejects the key) and the state is "polling" right after the start: no "dot can use the
    plugin", and the READY_TIMEOUT hint."""
    world.setup()
    world.knob("health_json", STARTING)
    world.knob("run_mode", "exit:0:4")
    result = world.run("start", LOCAL_MCP_READY_TIMEOUT="2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dot can use the plugin" not in result.stdout + result.stderr
    assert (
        "[local_mcp] the tunnel has not connected to OpenAI within 2s (control plane: status unknown,"
        " state polling); dot cannot use the plugin yet" in result.stdout
    )


# Without OpenAI's words in the tunnel log (the stub logs no failed poll): the texts by HTTP status.
_REJECTIONS = {
    401: (
        "OpenAI rejected the runtime API key (control plane HTTP 401 on 3 polls in a row)",
        "Fix: for a wrong key, delete {tunnel}/control-plane-api-key and run scripts/local_mcp.sh setup"
        " with the right restricted key (Tunnels Read + Use;"
        " https://platform.openai.com/settings/organization/api-keys)",
    ),
    403: (
        "OpenAI refused the tunnel (control plane HTTP 403 on 3 polls in a row)",
        "Cause: the runtime API key lacks Tunnels Read + Use for {tunnel_id}, or the tunnel belongs to"
        " another organization or project.",
    ),
    404: (
        "OpenAI does not know the tunnel {tunnel_id} (control plane HTTP 404 on 3 polls in a row)",
        "Fix: delete {tunnel}/tunnel-id and run scripts/local_mcp.sh setup",
    ),
}
REJECTED_401 = "error: OpenAI rejected the runtime API key (control plane HTTP 401 on 3 polls in a row)"


@pytest.mark.parametrize("http_status", [401, 403, 404])
def test_openai_rejecting_the_tunnel_stops_it_and_the_api(world: World, http_status: int) -> None:
    """Three rejections in a row stop it, and the message says 3: one more failed poll per health
    sample (STARTING, then 1, 2, 3, ... with the state alternating between backoff and polling)."""
    world.setup()
    world.health_sequence(*_rejections(http_status))
    world.knob("run_mode", "exit:0:20")  # would keep "running" for 20 s if nobody stopped it
    started = time.monotonic()
    result = world.run("start")
    assert time.monotonic() - started < 15
    assert result.returncode == 1, result.stdout + result.stderr
    first, detail = (t.format(tunnel=world.tunnel_dir, tunnel_id=TUNNEL_ID) for t in _REJECTIONS[http_status])
    stopping = f"[local_mcp] error: OpenAI's control plane answers HTTP {http_status}; stopping the tunnel"
    assert stopping in result.stderr
    assert f"error: {first}. The tunnel was stopped (exit 0; its log:" in result.stderr
    assert f"\n  {detail}" in result.stderr
    assert f"holds no control-plane error with HTTP {http_status}: OpenAI's own words" in result.stderr
    assert "OpenAI says" not in result.stderr
    assert "dot can use the plugin" not in result.stdout  # not at "unknown/polling" nor "degraded/polling"
    calls = world.calls_list()
    assert calls.index("tunnel: TERM") < calls.index("api: TERM")
    assert calls.count("tunnel: TERM") == 1 and calls.count("api: TERM") == 1
    assert "api: stopped" in result.stdout
    assert not list(world.state.glob("*.pid")) and not (world.tunnel_dir / "health.sock").exists()
    recorded = (world.state / "tunnel.rejected").read_text(encoding="utf-8")
    assert recorded.startswith(f"{first}\n") and detail in recorded
    status = world.run("status")
    assert status.returncode == 1
    assert f"tunnel: not running\n  the last start stopped it: {first}\n" in status.stdout
    assert f"\n    {detail}" in status.stdout
    _assert_no_secret(result.stdout, result.stderr, status.stdout)


def _synthetic(http_status: int, code: str, words: str) -> list[str]:
    """A failed poll with an OpenAI code the owner has not met yet (SYNTHETIC words; the mitigation
    is tunnel-client v0.0.16's own, pkg/controlplane/apierror)."""
    reason = {401: "401 Unauthorized", 403: "403 Forbidden", 404: "404 Not Found"}[http_status]
    error = f"controlplane client: unexpected status {http_status}: {code}: {words}"
    return [_poll_failed(error, reason, code)] * 3


_USE_FORBIDDEN_SAYS = TUNNEL_USE_FORBIDDEN.split("status 401: ", 1)[1]
_SAID = [
    pytest.param(
        OWNER_LOG,
        401,
        "tunnel_use_forbidden",
        "OpenAI denies the runtime API key's principal the use of the tunnel {tunnel_id} (control plane"
        " HTTP 401 on 3 polls in a row)",
        _USE_FORBIDDEN_SAYS,
        [
            'The metadata 404 "Tunnel not found" in the same log has the same cause (the tunnel is hidden'
            " from that principal), not a wrong tunnel id.",
            "Fix, on the Platform (tunnel permissions are organization-level, not per project):",
            "  1. the tunnel (https://platform.openai.com/settings/organization/tunnels) and the key's"
            " project are in the SAME organization (organization switcher);",
            "  2. the owner's organization role, or the tunnel's own User role, has Tunnels Read + Use",
            "  3. the key is Owned by: You (not a service account without that role) and Restricted with"
            " Tunnels Read + Use",
            "  4. then create a NEW key (after a role change the vendor advises new keys), delete"
            " {tunnel}/control-plane-api-key and run scripts/local_mcp.sh setup. Keep {tunnel}/tunnel-id:"
            " the tunnel id is not the problem.",
        ],
        ["delete {tunnel}/tunnel-id", "wrong, truncated or revoked", "invalid character", "oauth"],
        id="owner-2026-10-19-tunnel_use_forbidden",
    ),
    pytest.param(
        FAKE_KEY_LOG,
        401,
        "invalid_api_key",
        "OpenAI rejected the runtime API key (control plane HTTP 401 on 3 polls in a row)",
        f"invalid_api_key: {INCORRECT_KEY}",
        [
            "Cause: the key in {tunnel}/control-plane-api-key is wrong, truncated or revoked"
            " (invalid_api_key).",
            "Fix: compare the last 4 characters of the masked key above with the key list on"
            " https://platform.openai.com/settings/organization/api-keys; delete"
            " {tunnel}/control-plane-api-key and run scripts/local_mcp.sh setup with the right restricted"
            " key (Tunnels Read + Use).",
        ],
        ["{tunnel}/tunnel-id", "Owned by", "sk-[masked]"],
        id="fake-key-invalid_api_key",
    ),
    pytest.param(
        _synthetic(
            403,
            "tunnel_missing_principals",
            "SYNTHETIC no principal of this key matches the tunnel. mitigation: use a runtime API key"
            " associated with the intended organization or workspace",
        ),
        403,
        "tunnel_missing_principals",
        "OpenAI refused the runtime API key for the tunnel {tunnel_id} (control plane HTTP 403 on 3 polls"
        " in a row)",
        "tunnel_missing_principals: SYNTHETIC no principal of this key matches the tunnel. mitigation: use"
        " a runtime API key associated with the intended organization or workspace",
        [
            "Cause: the key is not associated with the tunnel's organization or workspace"
            " (tunnel_missing_principals).",
            "create a restricted key (Tunnels Read + Use) in a project of the tunnel's organization",
        ],
        ["{tunnel}/tunnel-id"],
        id="tunnel_missing_principals",
    ),
    pytest.param(
        _synthetic(
            403,
            "tunnel_active_organization_required",
            "SYNTHETIC an organization is required. mitigation: set --control-plane.organization-id,"
            " CONTROL_PLANE_ORGANIZATION_ID, or control_plane.organization_id to the tunnel's"
            " organization ID",
        ),
        403,
        "tunnel_active_organization_required",
        "OpenAI needs the organization id of the tunnel {tunnel_id} (control plane HTTP 403 on 3 polls in a"
        " row)",
        "tunnel_active_organization_required: SYNTHETIC an organization is required. mitigation: set"
        " --control-plane.organization-id, CONTROL_PLANE_ORGANIZATION_ID, or control_plane.organization_id"
        " to the tunnel's organization ID",
        [
            "Fix (tunnel-client's mitigation): set control_plane.organization_id to the tunnel's"
            " organization ID. scripts/local_mcp.sh does not set an organization id (tunnel-client.yaml"
            " has none, and start rewrites that file), so this needs a change to the script.",
        ],
        ["{tunnel}/tunnel-id"],
        id="tunnel_active_organization_required",
    ),
    pytest.param(
        _synthetic(403, "tunnel_paused_synthetic", "SYNTHETIC this tunnel is paused by an administrator"),
        403,
        "tunnel_paused_synthetic",
        "OpenAI refused the tunnel (control plane HTTP 403 on 3 polls in a row)",
        "tunnel_paused_synthetic: SYNTHETIC this tunnel is paused by an administrator",
        [
            "Note: scripts/local_mcp.sh does not know the code tunnel_paused_synthetic; OpenAI's words"
            " above name the cause",
            "Cause: the runtime API key lacks Tunnels Read + Use for {tunnel_id}, or the tunnel belongs to"
            " another organization or project.",
        ],
        [],
        id="unknown-code",
    ),
    pytest.param(
        [_poll_failed("controlplane client: unexpected status 404: Tunnel not found", "404 Not Found")] * 3,
        404,
        "",
        "OpenAI does not know the tunnel {tunnel_id} (control plane HTTP 404 on 3 polls in a row)",
        "Tunnel not found",
        [
            "Fix: delete {tunnel}/tunnel-id and run scripts/local_mcp.sh setup with the id from"
            " https://platform.openai.com/settings/organization/tunnels. If the id there is the same, keep"
            " it and check the organization and Tunnels Read + Use as for tunnel_use_forbidden",
        ],
        ["Note:"],
        id="no-code-404",
    ),
]


@pytest.mark.parametrize(("log", "http_status", "code", "first", "says", "expected", "forbidden"), _SAID)
def test_the_window_and_status_show_openai_s_words_and_the_fix_for_its_code(
    world: World,
    log: list[str],
    http_status: int,
    code: str,
    first: str,
    says: str,
    expected: list[str],
    forbidden: list[str],
) -> None:
    """One HTTP status, several causes: the owner's live start got 401 tunnel_use_forbidden for a
    valid key, which the status alone called a wrong key. The window and `status` show OpenAI's
    words from the tunnel log ("OpenAI says: <code>: <text>") and the cause and fix for the code."""
    world.setup()
    world.knob("run_log", "".join(f"{line}\n" for line in log))
    world.health_sequence(*_rejections(http_status))
    world.knob("run_mode", "exit:0:20")
    result = world.run("start")
    assert result.returncode == 1, result.stdout + result.stderr

    def fill(text: str) -> str:
        return text.format(tunnel=world.tunnel_dir, tunnel_id=TUNNEL_ID)

    first = fill(first)
    stopping = f"[local_mcp] error: OpenAI's control plane answers HTTP {http_status}"
    stopping += f" ({code}); stopping" if code else "; stopping"
    assert stopping in result.stderr
    assert f"error: {first}. The tunnel was stopped (exit 0; its log:" in result.stderr
    assert f"\n  OpenAI says: {says}\n" in result.stderr
    status = world.run("status", LOCAL_MCP_TAIL="0")  # no raw log lines
    assert status.returncode == 1
    assert f"  the last start stopped it: {first}\n    OpenAI says: {says}\n" in status.stdout
    for text in map(fill, expected):
        assert text in result.stderr and text in status.stdout, text
    for text in map(fill, forbidden):
        assert text not in result.stderr and text not in status.stdout, text
    recorded = (world.state / "tunnel.rejected").read_text(encoding="utf-8")
    assert recorded.startswith(f"{first}\nOpenAI says: {says}\n")
    _assert_no_secret(result.stdout, result.stderr, status.stdout, recorded)


def test_a_key_openai_did_not_mask_is_masked_and_its_masked_form_kept(world: World) -> None:
    """OpenAI masks the key in its message; any sk- key-looking run without "*" is masked here too,
    in the window, in tunnel.rejected and in `status`."""
    other_key = "sk-svcacct-" + "Ab1" * 20
    error = (
        "controlplane client: unexpected status 401: invalid_api_key: Incorrect API key provided:"
        f" {API_KEY}. Earlier: {MASKED_KEY}; key={other_key}; risk-assessment-12345 and sk-short"
    )
    world.setup()
    world.knob("run_log", _poll_failed(error, code="invalid_api_key") + "\n")
    world.health_sequence(*_rejections(401))
    world.knob("run_mode", "exit:0:20")
    result = world.run("start")
    assert result.returncode == 1, result.stdout + result.stderr
    status = world.run("status", LOCAL_MCP_TAIL="0")  # no raw log lines
    recorded = (world.state / "tunnel.rejected").read_text(encoding="utf-8")
    says = (
        "OpenAI says: invalid_api_key: Incorrect API key provided: sk-[masked]. Earlier:"
        f" {MASKED_KEY}; key=sk-[masked]; risk-assessment-12345 and sk-short"
    )
    for text in (result.stderr, status.stdout, recorded):
        assert says in text
        assert other_key not in text and other_key[11:] not in text and API_KEY[8:] not in text
    _assert_no_secret(result.stdout, result.stderr, status.stdout, recorded)


def test_openai_s_words_are_sanitised_bounded_and_never_evaluated(world: World) -> None:
    """Whatever the log holds (Go would escape control characters, a log written otherwise might
    not): printable ASCII only, at most 400 characters, \\" unquoted, nothing evaluated."""
    marker = world.root / "evaluated"
    words = (
        f'Incorrect \\"key\\" $(touch {marker}) `touch {marker}` \x1b[31mred\x1b[0m\x07 é 中 ' + "x" * 2000
    )
    line = (
        f'2026/10/19 08:48:31 WARN poll failed; backing off component=controlplane error="controlplane'
        f' client: unexpected status 401: invalid_api_key: {words}" status_code=401\n'
    )
    world.setup()
    (world.knobs / "run_log").write_bytes(line.encode("utf-8"))
    world.health_sequence(*_rejections(401))
    world.knob("run_mode", "exit:0:20")
    result = world.run("start")
    assert result.returncode == 1, result.stdout + result.stderr
    status = world.run("status", LOCAL_MCP_TAIL="0")  # no raw log lines
    assert not marker.exists()
    for text in (result.stderr, status.stdout):
        said = re.search(r"OpenAI says: (.*)\n", text)
        assert said is not None, text
        shown = said.group(1)
        assert len(shown) == 400 and shown.endswith("x...")
        assert re.fullmatch(r"[ -~]*", shown), shown
        assert shown.startswith(
            f'invalid_api_key: Incorrect "key" $(touch {marker}) `touch {marker}` ?[31mred?[0m? ?? ??? xxx'
        )
        assert "\x1b" not in text and "\x07" not in text
        assert "is wrong, truncated or revoked (invalid_api_key)" in text  # the code still counts


def test_fewer_than_three_rejections_in_a_row_do_not_stop_the_tunnel(world: World) -> None:
    """Two rejections (the second in state "polling", as the real binary reports between retries)
    neither stop the tunnel nor count as connected."""
    world.setup()
    world.health_sequence(STARTING, _rejected(401, 1, "backoff"), _rejected(401, 2, "polling"))
    world.knob("run_mode", "exit:0:5")
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    assert int((world.knobs / "health_seq_n").read_text(encoding="utf-8")) >= 3  # every answer was read
    assert "tunnel: TERM" not in world.calls_list()
    assert "OpenAI rejected" not in result.stderr and "warning" not in result.stderr
    assert "dot can use the plugin" not in result.stdout  # undecided: neither connected nor rejected


def test_a_single_rejection_after_other_failures_does_not_stop_the_tunnel(world: World) -> None:
    """consecutive_failures counts every kind of failure (the real binary: network errors, 5xx and
    401s in one counter) and http_status names the latest one only: one 404 after a short outage is
    not 3 rejections in a row."""
    world.setup()
    world.health_sequence(
        _network(1),
        _network(2),
        _rejected(503, 3),
        _rejected(404, 4),
        _rejected(503, 5, "polling"),
        _network(6),
    )
    world.knob("run_mode", "exit:0:9")
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    assert int((world.knobs / "health_seq_n").read_text(encoding="utf-8")) >= 6  # every answer was read
    assert "tunnel: TERM" not in world.calls_list() and not (world.state / "tunnel.rejected").exists()
    assert "OpenAI does not know the tunnel" not in result.stderr
    assert "tunnel: stopped (exit 0); stopping the API" in result.stdout


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param(
            [
                _network(1),
                _network(2),
                _network(3),
                _rejected(401, 4),
                _network(5),
                *_rejected_polls(401, 6, 11),
            ],
            id="network-errors-before-and-between",
        ),
        pytest.param(
            # 2 rejections, then a poll succeeded (the counter went back to 0), then 4 in a row
            [*_rejected_polls(401, 5, 6), *_rejected_polls(401, 1, 4)],
            id="the-counter-went-back",
        ),
    ],
)
def test_rejections_are_counted_from_the_first_one_in_a_row(world: World, answers: list[str]) -> None:
    """The tunnel stops after 3 rejections in a row, and the message says 3, not the failure count
    (which includes the network errors before them)."""
    world.setup()
    world.health_sequence(*answers)
    world.knob("run_mode", "exit:0:20")
    result = world.run("start")
    assert result.returncode == 1, result.stdout + result.stderr
    assert REJECTED_401 in result.stderr
    assert "polls in a row" not in result.stderr.replace(REJECTED_401, "")  # no other count
    assert "tunnel: TERM" in world.calls_list()


def test_a_rejection_after_connecting_stops_the_tunnel(world: World) -> None:
    """A key revoked while dot uses the tunnel: connected first, then 3 rejections in a row."""
    world.setup()
    world.health_sequence(HEALTHY, *_rejections(401)[1:])
    world.knob("run_mode", "exit:0:30")
    started = time.monotonic()
    result = world.run("start")
    assert time.monotonic() - started < 25
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.count(CONNECTED) == 1
    assert REJECTED_401 in result.stderr
    calls = world.calls_list()
    assert calls.count("tunnel: TERM") == 1 and calls.index("tunnel: TERM") < calls.index("api: TERM")
    assert (world.state / "tunnel.rejected").exists()


def test_ok_again_after_a_warning_is_announced_once(world: World) -> None:
    world.setup()
    world.health_sequence(HEALTHY, NETWORK_BACKOFF, HEALTHY)
    world.knob("run_mode", "exit:0:9")
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(CONNECTED) == 1
    warning = "[local_mcp] warning: OpenAI's control plane is not reachable yet (status degraded"
    assert result.stderr.count(warning) == 1, result.stderr
    assert result.stdout.count("[local_mcp] control plane ok again (polling)") == 1, result.stdout
    assert result.stdout.index(CONNECTED) < result.stdout.index("control plane ok again")
    assert "tunnel: TERM" not in world.calls_list()


def test_the_next_start_clears_the_rejection_of_the_last_one(world: World) -> None:
    """tunnel.rejected names why the LAST start stopped the tunnel: a start after the fix that
    connects and ends normally is no rejection, and `status` no longer repeats it."""
    world.setup()
    world.health_sequence(*_rejections(401))
    world.knob("run_mode", "exit:0:20")
    rejected = world.run("start")
    assert rejected.returncode == 1 and REJECTED_401 in rejected.stderr
    assert (world.state / "tunnel.rejected").exists()
    world.health_sequence()  # healthy from now on (knobs/health_json)
    world.knob("run_mode", "exit:0:3")
    again = world.run("start")
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout.count(CONNECTED) == 1
    assert "OpenAI rejected" not in again.stdout + again.stderr
    assert "tunnel: stopped (exit 0); stopping the API" in again.stdout
    assert not (world.state / "tunnel.rejected").exists()
    status = world.run("status")
    assert "tunnel: not running" in status.stdout and "the last start stopped it" not in status.stdout


@pytest.mark.parametrize(
    ("health", "described"),
    [
        (
            _real_health("network_error"),
            "status degraded, state backoff, failure network_error, HTTP -, 1 failure(s) in a row",
        ),
        (
            [SERVER_ERROR],
            "status degraded, state backoff, failure http_error, HTTP 503, 10 failure(s) in a row",
        ),
    ],
    ids=["real-network-error", "server-error"],
)
def test_other_control_plane_failures_warn_once_and_the_tunnel_keeps_running(
    world: World, health: list[str], described: str
) -> None:
    world.setup()
    world.health_sequence(*health)
    world.knob("run_mode", "exit:0:5")
    result = world.run("start")
    assert result.returncode == 0, result.stdout + result.stderr
    warning = (
        f"[local_mcp] warning: OpenAI's control plane is not reachable yet ({described});"
        " tunnel-client keeps retrying"
    )
    assert result.stderr.count(warning) == 1, result.stderr
    assert result.stderr.count("[local_mcp] warning") == 1, result.stderr
    assert "tunnel: TERM" not in world.calls_list()  # not stopped: it ended by itself after 5 s
    assert "dot can use the plugin" not in result.stdout
    assert "tunnel: stopped (exit 0); stopping the API" in result.stdout


def test_status_shows_a_degraded_control_plane(world: World) -> None:
    world.setup()
    world.knob("health_json", NETWORK_BACKOFF)
    started = world.popen("start")
    try:
        _wait_for_tunnel(world)
        status = world.run("status")
        assert status.returncode == 1, status.stdout + status.stderr  # running, but not connected
        assert (
            "  control plane: status degraded, state backoff, http_status -, consecutive_failures 5,"
            " failure_category network_error" in status.stdout
        )
        assert world.run("stop").returncode == 0
        out, err = started.communicate(timeout=60)
        assert started.returncode == 0, out + err
    finally:
        _end(world, started)


_NO_PYTHON = 'if [ -e "@KNOBS@/no_python" ]; then exit 127; fi\nexec @REAL@ "$@"\n'
_NO_JQ = 'if [ -e "@KNOBS@/no_jq" ]; then exit 127; fi\nexec @REAL@ "$@"\n'


def _parser(world: World, parser: str) -> None:
    """Make the script parse the health JSON with jq (python3 fails) or its sed fallback (both fail)."""
    if parser in ("jq", "sed"):
        world.wrap("python3", _NO_PYTHON)
        world.knob("no_python", "1")
    if parser == "sed":
        world.wrap("jq", _NO_JQ)
        world.knob("no_jq", "1")


_PARSERS = [
    "python3",
    pytest.param("jq", marks=pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")),
    pytest.param("sed", marks=pytest.mark.skipif(shutil.which("jq") is None, reason="wraps jq")),
]


@pytest.mark.parametrize("parser", _PARSERS)
def test_every_parser_recognises_the_real_answers_connected_and_rejected(world: World, parser: str) -> None:
    """The real v0.0.16 answers (tests/fixtures/tunnel_client_v0.0.16): accepted by the vendor's
    local control plane, then OpenAI rejecting a FAKE key (unknown/polling, then backoff and polling
    with 401 and 1, 1, 2, 2, 3, ... failures)."""
    world.setup()
    _parser(world, parser)
    world.health_sequence(*_real_health("ok"))
    world.knob("run_mode", "exit:0:3")
    connected = world.run("start")
    assert connected.returncode == 0, connected.stdout + connected.stderr
    # The sed fallback reads the status only, not the state.
    expected = CONNECTED
    if parser == "sed":
        expected = CONNECTED.replace("(control plane polling)", "(control plane ok)")
    assert connected.stdout.count(expected) == 1
    world.health_sequence(*_real_health("rejected_401"))
    world.knob("run_mode", "exit:0:20")
    rejected = world.run("start")
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert REJECTED_401 in rejected.stderr
    assert "dot can use the plugin" not in rejected.stdout
    assert "tunnel: TERM" in world.calls_list()


@pytest.mark.parametrize("parser", _PARSERS)
def test_health_values_are_parsed_never_evaluated(world: World, parser: str) -> None:
    marker = world.root / "evaluated"
    hostile = _health(
        f"$(touch {marker})",
        f"`touch {marker}`; ok",
        http_status=f"401 $(touch {marker})",
        failures="9; touch",
        category=f"x\ntouch {marker}",
    )
    world.setup()
    _parser(world, parser)
    world.knob("health_json", hostile)
    world.knob("run_mode", "exit:0:4")
    result = world.run("start", LOCAL_MCP_READY_TIMEOUT="2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert "touch" not in result.stdout + result.stderr
    assert "has not connected to OpenAI within 2s (control plane: status -, state -)" in result.stdout
    assert "tunnel: TERM" not in world.calls_list()


def test_setup_skips_doctor_while_the_tunnel_runs(world: World) -> None:
    """The running tunnel holds health.sock; doctor would report its health listener as failed."""
    world.setup()
    started = world.popen("start")
    try:
        _wait_for_tunnel(world)
        doctors = len([c for c in world.calls_list() if c.startswith("tunnel-client doctor")])
        again = world.run("setup")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "doctor skipped: the tunnel is running (pid" in again.stdout
        assert len([c for c in world.calls_list() if c.startswith("tunnel-client doctor")]) == doctors
        assert (world.tunnel_dir / "health.sock").is_socket()  # not removed as "stale"
        assert world.run("stop").returncode == 0
        out, err = started.communicate(timeout=60)
        assert started.returncode == 0, out + err
    finally:
        _end(world, started)


def test_stop_and_logs_without_a_run(world: World) -> None:
    stopped = world.run("stop")
    assert stopped.returncode == 0
    assert "tunnel: not running" in stopped.stdout and "api: not running" in stopped.stdout
    logs = world.run("logs")
    assert logs.returncode == 1 and "no log file yet" in logs.stderr


# --------------------------------------------------------------------------------------------
# rotate
# --------------------------------------------------------------------------------------------


def _revokes(world: World) -> list[str]:
    return [c for c in world.uv_calls() if "credentials revoke" in c]


def test_rotate_replaces_the_file_and_revokes_the_old_credential(world: World) -> None:
    world.setup()
    auth = world.tunnel_dir / "mcp-authorization"
    result = world.run("rotate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_2}" and _mode(auth) == 0o600
    assert world.credential()["credential_id"] == CREDENTIAL_2
    create = [c for c in world.uv_calls() if "create-mcp" in c][-1]
    assert re.search(r"--authorization-file \S+/mcp-authorization\.new-\d{8}T\d{6}Z --yes", create), create
    assert _revokes(world) == [
        f"uv run suv-deals credentials revoke {CREDENTIAL_1} --reason rotated by scripts/local_mcp.sh rotate"
        f" --workspace {WORKSPACE} --yes|auth=|public=|events=|net="
    ]
    assert not list(world.tunnel_dir.glob("mcp-authorization.new-*"))
    assert "restart" in result.stdout
    _assert_no_secret(result.stdout, result.stderr, world.recorded())


def test_rotate_keeps_everything_when_creating_fails(world: World) -> None:
    world.setup()
    auth = world.tunnel_dir / "mcp-authorization"
    credential = (world.tunnel_dir / "credential.json").read_bytes()
    result = world.run("rotate", STUB_CREATE_EXIT="1")
    assert result.returncode == 1
    assert "are unchanged" in result.stderr
    assert auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_1}"
    assert (world.tunnel_dir / "credential.json").read_bytes() == credential
    assert not _revokes(world)
    assert not list(world.tunnel_dir.glob("mcp-authorization.new-*"))


def test_rotate_reports_a_failed_revocation_loudly_and_keeps_the_new_file(world: World) -> None:
    world.setup()
    result = world.run("rotate", STUB_REVOKE_EXIT="1")
    assert result.returncode == 1
    assert "REVOKING THE OLD CREDENTIAL FAILED" in result.stderr
    assert f"credentials revoke {CREDENTIAL_1} --workspace {WORKSPACE}" in result.stderr
    assert (world.tunnel_dir / "mcp-authorization").read_text(encoding="utf-8") == f"Bearer {TOKEN_2}"
    assert world.credential()["credential_id"] == CREDENTIAL_2
    _assert_no_secret(result.stdout, result.stderr)


def test_rotate_takes_the_new_credential_back_when_it_cannot_record_it(world: World) -> None:
    world.setup()
    auth = world.tunnel_dir / "mcp-authorization"
    credential = (world.tunnel_dir / "credential.json").read_bytes()
    world.wrap("mktemp", _FAILING_MKTEMP)
    world.knob("mktemp_fails_after_create", "1")
    result = world.run("rotate")
    assert result.returncode == 1, result.stdout + result.stderr
    unchanged = f"the new credential was revoked; {auth} and credential {CREDENTIAL_1} are unchanged"
    assert unchanged in result.stderr
    assert _revokes(world) == [
        f"uv run suv-deals credentials revoke {CREDENTIAL_2} --reason rotate could not record it"
        f" --workspace {WORKSPACE} --yes|auth=|public=|events=|net="
    ]
    assert auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_1}"
    assert (world.tunnel_dir / "credential.json").read_bytes() == credential
    assert not list(world.tunnel_dir.glob("mcp-authorization.new-*"))


def test_rotate_revokes_the_old_credential_and_keeps_the_record_when_its_rename_fails(world: World) -> None:
    world.setup()
    auth = world.tunnel_dir / "mcp-authorization"
    world.wrap(
        "mv",
        "for last; do :; done\n"
        'case $last in */credential.json) if [ -e "@KNOBS@/mv_fail" ]; then\n'
        '  echo "mv: Input/output error" >&2; exit 1; fi ;; esac\n'
        'exec @REAL@ "$@"\n',
    )
    world.knob("mv_fail", "1")
    result = world.run("rotate")
    assert result.returncode == 1, result.stdout + result.stderr
    assert auth.read_text(encoding="utf-8") == f"Bearer {TOKEN_2}"
    assert world.credential()["credential_id"] == CREDENTIAL_1  # not replaced ...
    assert _revokes(world) == [  # ... but the old credential is revoked all the same
        f"uv run suv-deals credentials revoke {CREDENTIAL_1} --reason rotated by scripts/local_mcp.sh rotate"
        f" --workspace {WORKSPACE} --yes|auth=|public=|events=|net="
    ]
    assert f"does not name the credential in use, {CREDENTIAL_2}" in result.stderr
    finish = re.search(r"run: mv -f (\S+) (\S+)", result.stderr)
    assert finish is not None and Path(finish.group(2)) == world.tunnel_dir / "credential.json"
    (world.knobs / "mv_fail").unlink()
    subprocess.run(["mv", "-f", finish.group(1), finish.group(2)], check=True)  # the printed command
    assert world.credential()["credential_id"] == CREDENTIAL_2
    _assert_no_secret(result.stdout, result.stderr)


def test_rotate_without_setup_has_nothing_to_rotate(world: World) -> None:
    world.tunnel_dir.mkdir(parents=True, mode=0o700)
    result = world.run("rotate")
    assert result.returncode == 1
    assert "nothing to rotate" in result.stderr
    assert not world.uv_calls()
