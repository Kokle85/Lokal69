"""``scripts/local_mcp.sh``: dot through the OpenAI Secure MCP Tunnel (docs/connect_mcp.md section 8).

The script runs from a copy in a temporary "repository" (its own ``var/`` and ``.env``) against
stub executables on a fake PATH: ``uv`` (doctor, ``api serve``, ``mcp probe``, ``credentials
create-mcp`` / ``revoke``), ``tunnel-client`` (``--version``, ``doctor``, ``run``) and ``curl``
(``/readyz`` and the tunnel's health socket). Every stub records its argv and its whole
environment; one test also wraps every other external command the script runs (``mktemp``,
``ln``, ``env``, ``ps``, ...) to record its argv, and a static test forbids writing a secret with
anything but the shell builtin ``printf`` or calling a command by absolute path. Together they show
that the runtime API key and the MCP token never reach the argv of a command the script runs, the
stubs' environment, the output or the YAML. A pty test shows that both prompts read without echo.
No network, no database, no real tunnel; the real end-to-end chain is recorded in
docs/connect_mcp.md section 8.6. Only SYNTHETIC values.
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
    "awk", "bash", "cat", "chmod", "cmp", "cp", "date", "dd", "dirname", "env", "flock", "head",
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
      setsid python3 -I -c "$listen" "$port" &
      echo "$!" > "$knobs/foreign.pid"
    else
      python3 -I -c "$listen" "$port" &
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
    echo "doctor (stub): configuration valid"
    exit "$(cat "$knobs/doctor_exit" 2>/dev/null || echo 0)" ;;
  run)
    sock="" prev=""
    for arg in "$@"; do
      if [[ $prev == --health.unix-socket ]]; then sock=$arg; fi
      prev=$arg
    done
    python3 -I -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$sock"
    touch "$knobs/tunnel.running"
    trap 'rm -f "$knobs/tunnel.running"; echo "tunnel: TERM" >> "$calls"; exit 0' TERM
    trap 'rm -f "$knobs/tunnel.running"; echo "tunnel: INT" >> "$calls"; exit 130' INT
    echo "tunnel stub: polling"
    mode=$(cat "$knobs/run_mode" 2>/dev/null || echo wait)
    case $mode in
      exit:*)
        IFS=: read -r _ code after <<< "$mode"
        sleep "$after"
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
    if [[ -e $knobs/tunnel.running ]]; then printf '200'; else printf '000'; exit 7; fi ;;
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
    }
    for name, template in (("uv", _STUB_UV), ("tunnel-client", _STUB_TUNNEL_CLIENT), ("curl", _STUB_CURL)):
        body = template
        for key, value in replacements.items():
            body = body.replace(key, value)
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
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
        f"tunnel-client doctor --config {tunnel}/tunnel-client.yaml --explain",
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


def test_setup_reports_a_failed_doctor(world: World) -> None:
    world.knob("doctor_exit", "1")
    result = world.run("setup", stdin=f"{TUNNEL_ID}\n{API_KEY}\n")
    assert result.returncode == 1
    assert "tunnel-client doctor reported a problem" in result.stderr
    assert (world.tunnel_dir / "tunnel-client.yaml").is_file()  # everything else is in place


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
    run = _index(calls, "tunnel-client run")
    assert calls[run] == (
        f"tunnel-client run --config {tunnel}/tunnel-client.yaml --health.unix-socket {tunnel}/health.sock"
        " --health.listen-addr 127.0.0.1:0"
    )
    assert readyz < probe < run < calls.index("api: TERM")
    assert not any("http-raw-unsafe" in c or "--log." in c for c in world.calls_list())

    out = result.stdout
    assert "Keep this window open while dot uses the system; Ctrl+C stops the tunnel and the API" in out
    assert f"7 tool(s); tools/list sha256 {TOOLS_SHA}" in out and "protocol 2025-11-25" in out
    assert "tunnel stub: polling" in out  # the tunnel's output reaches this window ...
    tunnel_log = next((world.state / "logs").glob("tunnel-*.log"))
    assert "tunnel stub: polling" in tunnel_log.read_text(encoding="utf-8")  # ... and its log
    assert "api: stopped" in out
    assert f"127.0.0.1:{world.port} is the API's own socket" in out
    assert re.fullmatch(
        r"var/local_mcp/logs/api-\d{8}T\d{6}Z\.log", (world.state / "api.logfile").read_text().strip()
    )
    probe_report = json.loads(next((world.state / "logs").glob("probe-*.json")).read_text(encoding="utf-8"))
    assert probe_report["tools_sha256"] == TOOLS_SHA
    assert not list(world.state.glob("*.pid")) and not (tunnel / "health.sock").exists()
    _assert_no_secret(out, result.stderr, world.recorded(), tunnel_log.read_text(encoding="utf-8"))
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
    """Stop a backgrounded ``start`` and everything it launched (its tee keeps the pipe open)."""
    for pid_file in world.state.glob("*.pid"):
        with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
            os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=15)


def test_start_stops_the_api_when_the_tunnel_fails(world: World) -> None:
    world.setup()
    world.knob("run_mode", "exit:3:0")
    result = world.run("start")
    assert result.returncode == 1
    assert "the tunnel exited with status 3" in result.stderr
    assert "api: TERM" in world.calls_list() and "api: stopped" in result.stdout
    assert not list(world.state.glob("*.pid"))


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
