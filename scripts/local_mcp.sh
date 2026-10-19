#!/usr/bin/env bash
# Connect dot (ChatGPT) to this system's MCP server, read-only, through the OpenAI Secure MCP
# Tunnel on the owner's WSL workstation (docs/connect_mcp.md section 8, docs/runbook.md 9.3).
#
# Usage (any directory; the script works in the repository root):
#   scripts/local_mcp.sh setup    once; idempotent, creates only what is missing: checks
#                                 tunnel-client, asks for the tunnel id and the OpenAI runtime API
#                                 key (hidden input), creates the viewer MCP credential file, writes
#                                 tunnel-client.yaml and runs `tunnel-client doctor --explain` (its
#                                 checks mcp_server_reachable and oauth_metadata fail until `start`
#                                 runs the API: expected, setup still succeeds)
#   scripts/local_mcp.sh start    doctor, the API on 127.0.0.1:LOCAL_MCP_PORT (background), its
#                                 /readyz, `suv-deals mcp probe`, `tunnel-client doctor` with the
#                                 API up, then `tunnel-client run` in THIS window (foreground; its own
#                                 output goes to its log file only). The window says when OpenAI's
#                                 control plane accepts the tunnel, and stops it when OpenAI rejects
#                                 the runtime API key or the tunnel id; Ctrl+C stops the tunnel and
#                                 the API
#   scripts/local_mcp.sh stop     from another window: SIGTERM to the tunnel, then to the API
#   scripts/local_mcp.sh status   which of the two run, the API's /readyz, the tunnel's /readyz and
#                                 control-plane state (its health socket), the credential's expiry,
#                                 log files and last lines
#   scripts/local_mcp.sh logs     follow the current API and tunnel logs (Ctrl+C stops following)
#   scripts/local_mcp.sh rotate   a new credential file (atomic rename), then revokes the old
#                                 credential; restart afterwards
#   scripts/local_mcp.sh --help
#
# Route (owner decision 2026-10-18): OpenAI's `tunnel-client` connects OUTBOUND only to
# api.openai.com:443 and forwards dot's MCP requests to http://127.0.0.1:LOCAL_MCP_PORT/mcp; no
# inbound port, no public URL. ChatGPT's plugin uses "No authentication"; tunnel-client adds our
# `Authorization` header from a file (`mcp.extra_headers: {Authorization: "file:..."}`). The
# credential is a viewer `suvmcp_` static bearer (deals:read, reviews:read, 30 days).
#
# The API for this route runs separately from any other API (the dashboard API may use port 8000)
# with MCP_AUTH_MODE=static_bearer, MCP_PUBLIC_URL=http://127.0.0.1:<port>/mcp,
# MCP_EVENTS_ENABLED=false and SOURCE_NETWORK_ENABLED=false (never true), and without proxy
# headers (only tunnel-client connects, from 127.0.0.1).
#
# Secrets: DATABASE_URL and the other settings stay in .env in the repository root, which
# `suv-deals` reads itself; this script never reads, prints or passes them. The runtime API key
# is read with `read -rs` (no echo) and written by the shell builtin `printf` into a new 0600
# file, so it never reaches a command line or another process's environment; the MCP token is
# written by `credentials create-mcp --authorization-file` and never shown. Both stay in
# TUNNEL_DIR, which tunnel-client.yaml references with `file:` (no secret value in the YAML).
# tunnel-client runs with a cleaned environment (PATH, HOME, USER, LOGNAME, TMPDIR, TZ, locale,
# terminal, proxy and SSL_CERT_* variables only), so no CONTROL_PLANE_*, OPENAI_*, MCP_* or LOG_*
# variable of the shell can change its configuration, and never with raw HTTP logging.
#
# TUNNEL_DIR (LOCAL_MCP_TUNNEL_DIR, default ~/.config/suv-deals/tunnel; mode 0700, yours, outside
# the repository and outside every git working tree, around the repository or around TUNNEL_DIR
# itself such as a dotfiles repository at ~/.config or ~; not a symlink, not under /mnt):
# control-plane-api-key (0600), tunnel-id, mcp-authorization (0600, exactly "Bearer <token>"),
# credential.json (credential id, workspace, expiry; no secret), tunnel-client.yaml (0600),
# tunnel-client.version (its --version output and sha256) and, while the tunnel runs, health.sock
# (its health and admin endpoints: /healthz, /readyz, /health, the UI; tunnel-client v0.0.16 binds
# no TCP port when it has this socket).
#
# State (var/ is git-ignored): var/local_mcp/<process>.pid, <process>.starttime (the API's start
# time, so a reused pid is never signalled), <process>.logfile (its current log), <process>.stopping
# (SIGTERM sent), api.port, api.lost (the API ended while the tunnel ran), tunnel.rejected (why the
# last start stopped the tunnel: OpenAI answered 401, 403 or 404; OpenAI's error code and words from
# the tunnel log, the cause and the fix), var/local_mcp/lock (one setup,
# start, stop or rotate at a time; `start` releases it once the tunnel runs) and
# var/local_mcp/logs/<process>-<UTC time>.log (process: api, tunnel), probe-<UTC time>.json,
# doctor-<UTC time>.txt. Separate from scripts/local_crawl.sh (var/local_crawl, its own lock).
#
# While the tunnel runs, the start window stops it as soon as the API is gone (tunnel-client would
# otherwise send the credential to whatever binds 127.0.0.1:LOCAL_MCP_PORT next), and `start` sends
# the credential (probe) only after checking that the API it launched owns the listening socket.
# The window reads the control-plane component of the tunnel's GET /health?details=true (/readyz
# answers 200 even while OpenAI refuses the key): "connected" only once it is ok; the same 401, 403
# or 404 on 3 polls in a row (other failures in between start the count again) stop the tunnel with
# "OpenAI says: <code>: <text>" (the last such error tunnel-client logged; printable characters
# only, at most 400, any unmasked sk- key masked) and the cause and the fix for that code (one
# status has several causes: 401 is a wrong key, invalid_api_key, or a valid key whose principal
# may not use the tunnel, tunnel_use_forbidden) (exit 1); other failures (network, 5xx) print one
# warning and the tunnel keeps retrying. Nothing read from the socket or the log is evaluated.
#
# Environment (optional): LOCAL_MCP_PORT API port (default 8010); LOCAL_MCP_TUNNEL_DIR;
# TUNNEL_CLIENT_BIN the tunnel-client binary (default: from PATH); LOCAL_MCP_WORKSPACE workspace id
# for the credential (default: the only active workspace); LOCAL_MCP_READY_TIMEOUT seconds `start`
# waits for /readyz (default 60); LOCAL_MCP_STOP_TIMEOUT seconds a stop waits per process (default
# 30); LOCAL_MCP_PROBE_TIMEOUT seconds per probe request (default 10); LOCAL_MCP_TAIL log lines
# `status` shows (default 5).
#
# Exit codes: 0 ok (for `start`: the tunnel was stopped with Ctrl+C or `stop`); 1 a step failed,
# setup is incomplete, the credential expired, a process is not running or not ready (`status`:
# also a control plane that is not ok), did not stop in time, another command holds the lock,
# OpenAI rejected the runtime API key or the tunnel id, or the tunnel ended any other way (the API
# died, a closed window); 2 usage; 129, 130 or 143 when `start` is interrupted (closed window,
# Ctrl+C, SIGTERM) before the tunnel runs. The API that `start` launched is stopped in every case.
set -euo pipefail

# The comment block above (from line 2 up to the first line that is not a comment).
usage() { awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; }

STATE_DIR=var/local_mcp
LOG_DIR=$STATE_DIR/logs
PORT=${LOCAL_MCP_PORT:-8010}
TUNNEL_DIR=${LOCAL_MCP_TUNNEL_DIR:-${HOME:-}/.config/suv-deals/tunnel}
WORKSPACE=${LOCAL_MCP_WORKSPACE:-}
READY_TIMEOUT=${LOCAL_MCP_READY_TIMEOUT:-60}
STOP_TIMEOUT=${LOCAL_MCP_STOP_TIMEOUT:-30}
PROBE_TIMEOUT=${LOCAL_MCP_PROBE_TIMEOUT:-10}
TAIL_LINES=${LOCAL_MCP_TAIL:-5}
EXPIRY_WARN_DAYS=5
# The same HTTP 401, 403 or 404 from OpenAI on this many control-plane polls in a row (counted by the
# start window from the first health sample that shows it, see watch_tunnel) stops the tunnel:
# retrying cannot help. tunnel-client v0.0.16 retries within seconds at first, so this takes a few.
REJECT_AFTER=3
# The checks of `tunnel-client doctor` that fail while the API is not running (setup, before start).
DOCTOR_EXPECTED_BEFORE_START=" mcp_server_reachable oauth_metadata "
CREDENTIAL_LABEL="dot read-only (tunnel)"
CREDENTIAL_ROLE=viewer
CREDENTIAL_SCOPES=deals:read,reviews:read
CREDENTIAL_DAYS=30
TUNNEL_ID_RE='^tunnel_([a-z0-9]{4}_)?[a-z0-9]{32}$'
UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
TUNNELS_PAGE=https://platform.openai.com/settings/organization/tunnels
API_KEYS_PAGE=https://platform.openai.com/settings/organization/api-keys
# Variables tunnel-client keeps (names only; everything else is unset before it starts).
TUNNEL_ENV_KEEP=" PATH HOME USER LOGNAME LANG LANGUAGE LC_ALL LC_CTYPE LC_MESSAGES TERM TZ TMPDIR \
HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR "

command=${1:-}
case "$command" in
  setup | start | stop | status | logs | rotate) ;;
  -h | --help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (($# > 1)); then
  usage >&2
  exit 2
fi

usage_error() {
  echo "error: $*" >&2
  exit 2
}
if [[ ! $PORT =~ ^[1-9][0-9]{3,4}$ ]] || ((PORT < 1024 || PORT > 65535)); then
  usage_error "LOCAL_MCP_PORT must be a port number from 1024 to 65535"
fi
for setting in "LOCAL_MCP_READY_TIMEOUT=$READY_TIMEOUT" "LOCAL_MCP_STOP_TIMEOUT=$STOP_TIMEOUT" \
  "LOCAL_MCP_PROBE_TIMEOUT=$PROBE_TIMEOUT"; do
  [[ ${setting#*=} =~ ^[1-9][0-9]{0,3}$ ]] || usage_error "${setting%%=*} must be a whole number of seconds (1-9999)"
done
[[ $TAIL_LINES =~ ^[0-9]{1,4}$ ]] || usage_error "LOCAL_MCP_TAIL must be a number of lines"
[[ -z $WORKSPACE || $WORKSPACE =~ $UUID_RE ]] || usage_error "LOCAL_MCP_WORKSPACE must be a workspace id (uuid)"
while [[ $TUNNEL_DIR == */ && $TUNNEL_DIR != / ]]; do TUNNEL_DIR=${TUNNEL_DIR%/}; done
[[ $TUNNEL_DIR == /?* ]] || usage_error "LOCAL_MCP_TUNNEL_DIR must be an absolute path"
# Plain characters only: the paths go into tunnel-client.yaml and a Unix socket name.
[[ $TUNNEL_DIR =~ ^/[A-Za-z0-9._/+@-]+$ ]] ||
  usage_error "LOCAL_MCP_TUNNEL_DIR may contain only letters, digits and . _ / + @ - (no spaces or quotes)"

cd "$(dirname "$0")/.."
REPO_ROOT=$(pwd -P)

API_KEY_FILE=$TUNNEL_DIR/control-plane-api-key
TUNNEL_ID_FILE=$TUNNEL_DIR/tunnel-id
AUTH_FILE=$TUNNEL_DIR/mcp-authorization
CREDENTIAL_FILE=$TUNNEL_DIR/credential.json
CONFIG_FILE=$TUNNEL_DIR/tunnel-client.yaml
VERSION_FILE=$TUNNEL_DIR/tunnel-client.version
HEALTH_SOCKET=$TUNNEL_DIR/health.sock
MCP_URL=http://127.0.0.1:$PORT/mcp
((${#HEALTH_SOCKET} <= 107)) || usage_error "LOCAL_MCP_TUNNEL_DIR is too long for a Unix socket path (health.sock)"

# The API environment of this route (process environment; it wins over .env).
API_ENV=(
  MCP_AUTH_MODE=static_bearer
  "MCP_PUBLIC_URL=$MCP_URL"
  MCP_EVENTS_ENABLED=false
  SOURCE_NETWORK_ENABLED=false
)

API_STARTED=0
WATCHER_PID=

fail() {
  echo "error: $*" >&2
  exit 1
}

pid_file() { printf '%s/%s.pid' "$STATE_DIR" "$1"; }
logfile_record() { printf '%s/%s.logfile' "$STATE_DIR" "$1"; }
# One SIGTERM per run: a second one would make uvicorn or tunnel-client skip their clean shutdown.
stopping_marker() { printf '%s/%s.stopping' "$STATE_DIR" "$1"; }

read_pid() {
  local file pid
  file=$(pid_file "$1")
  [[ -f $file ]] || return 1
  pid=$(<"$file")
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$pid"
}

# The words `ps` shows for PROCESS: `uv run suv-deals api serve --host 127.0.0.1 --port N ...` and
# `<tunnel-client> run --config <tunnel-client.yaml> ...` (whatever the binary's name or path).
command_words() {
  case "$1" in
    api) printf 'suv-deals api serve --host 127.0.0.1 --port' ;;
    tunnel) printf 'run --config %s' "$CONFIG_FILE" ;;
  esac
}

starttime_file() { printf '%s/%s.starttime' "$STATE_DIR" "$1"; }

# proc_starttime PID: the process's start time (field 22 of /proc/PID/stat, clock ticks since boot);
# it stays the same across exec and differs for a later process that reuses the pid.
proc_starttime() {
  local stat rest fields
  { IFS= read -r stat <"/proc/$1/stat"; } 2>/dev/null || return 1
  rest=${stat##*) } # after the command name, which may contain spaces and parentheses
  read -r -a fields <<<"$rest"
  [[ ${fields[19]:-} =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${fields[19]}"
}

# same_start PROCESS PID: PID started when PROCESS was launched (true when no start time was
# recorded, e.g. without /proc).
same_start() {
  local file current
  file=$(starttime_file "$1")
  [[ -f $file ]] || return 0
  current=$(proc_starttime "$2") || return 1
  [[ $current == "$(<"$file")" ]]
}

# pid_alive PID: the process exists and is no zombie.
pid_alive() {
  local state
  state=$(ps -p "$1" -o stat= 2>/dev/null) || return 1
  state=${state//[[:space:]]/}
  [[ -n $state && $state != Z* ]]
}

# process_args PID: its whole command line (-ww: never cut to $COLUMNS).
process_args() {
  kill -0 "$1" 2>/dev/null || return 1
  ps -ww -p "$1" -o args= 2>/dev/null
}

# running PROCESS: its pid is alive AND runs that command (not a reused pid).
running() {
  local pid args
  pid=$(read_pid "$1") || return 1
  args=$(process_args "$pid") || return 1
  [[ " $args " == *" $(command_words "$1") "* ]] || return 1
  same_start "$1" "$pid"
}

# alive PROCESS: its recorded pid exists, is no zombie and is still the process launched then (same
# start time). For the API this run launched: right after the launch `ps` may still show
# setsid/nohup/env instead of the final command, so the command words are not compared.
alive() {
  local pid
  pid=$(read_pid "$1") || return 1
  pid_alive "$pid" || return 1
  same_start "$1" "$pid"
}

# other_tunnel: tunnel.pid names a live tunnel-client run with ANOTHER configuration (started
# with another LOCAL_MCP_TUNNEL_DIR); prints its command line.
other_tunnel() {
  local pid args
  pid=$(read_pid tunnel) || return 1
  args=$(process_args "$pid") || return 1
  [[ " $args " == *" run --config "* && " $args " != *" $(command_words tunnel) "* ]] || return 1
  printf '%s' "$args"
}

current_log() {
  local record
  record=$(logfile_record "$1")
  [[ -f $record ]] || return 1
  cat "$record"
}

forget() {
  rm -f "$(pid_file "$1")" "$(stopping_marker "$1")" "$(starttime_file "$1")"
}

# One setup, start, stop or rotate at a time (fd 9 on var/local_mcp/lock). `start` releases it
# before the tunnel runs in the foreground, so `stop` works from another window.
take_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock (util-linux) is missing"
  exec 9>"$STATE_DIR/lock"
  flock -n 9 || fail "another scripts/local_mcp.sh setup, start, stop or rotate is running; let it finish, then try again"
}

release_lock() { exec 9>&-; }

# --------------------------------------------------------------------------------------------
# TUNNEL_DIR and its files (names and modes only; this script never prints a secret file)
# --------------------------------------------------------------------------------------------

# The checkout and every git working tree around it (like `credentials create-mcp`).
protected_roots() {
  local dir=$REPO_ROOT
  printf '%s\n' "$REPO_ROOT"
  while [[ -n $dir ]]; do
    if [[ -e $dir/.git ]]; then printf '%s\n' "$dir"; fi
    dir=${dir%/*}
  done
  if [[ -e /.git ]]; then printf '/\n'; fi
}

# git_tree_around PATH: the nearest directory at or above PATH (already resolved) with a `.git`
# entry, i.e. a git working tree that would list the tunnel's files as untracked (a dotfiles
# repository at ~/.config or ~, say). A separate git directory (`--git-dir ... --work-tree ~`)
# leaves no such entry and cannot be seen.
git_tree_around() {
  local dir=$1
  while [[ -n $dir ]]; do
    if [[ -e $dir/.git ]]; then
      printf '%s' "$dir"
      return 0
    fi
    dir=${dir%/*}
  done
  if [[ -e /.git ]]; then
    printf '/'
    return 0
  fi
  return 1
}

# check_tunnel_dir create|existing
check_tunnel_dir() {
  local real root uid mode tree
  real=$(realpath -m -- "$TUNNEL_DIR")
  while IFS= read -r root; do
    if [[ $real == "$root" || $real == "${root%/}"/* ]]; then
      fail "the tunnel directory $TUNNEL_DIR is inside the repository or a git working tree ($root);" \
        "use a directory outside it, e.g. the default ~/.config/suv-deals/tunnel (LOCAL_MCP_TUNNEL_DIR)"
    fi
  done < <(protected_roots)
  if tree=$(git_tree_around "$real"); then
    fail "the tunnel directory $TUNNEL_DIR is inside a git working tree ($tree has .git): a commit there" \
      "could publish the runtime API key and the MCP token. Use a directory outside every git working" \
      "tree (LOCAL_MCP_TUNNEL_DIR=/absolute/path)"
  fi
  if [[ $real == /mnt/* ]]; then
    fail "the tunnel directory must be on the Linux filesystem, not under /mnt (Windows drives have no Unix file modes)"
  fi
  if [[ -L $TUNNEL_DIR ]]; then
    fail "the tunnel directory $TUNNEL_DIR is a symlink; use a real directory"
  fi
  if [[ ! -e $TUNNEL_DIR ]]; then
    if [[ $1 != create ]]; then
      fail "setup is incomplete: $TUNNEL_DIR does not exist; run scripts/local_mcp.sh setup"
    fi
    (umask 077 && mkdir -p -- "$TUNNEL_DIR")
    echo "  created $TUNNEL_DIR (mode 0700)"
  fi
  if [[ ! -d $TUNNEL_DIR ]]; then
    fail "the tunnel directory $TUNNEL_DIR is not a directory"
  fi
  read -r uid mode < <(stat -c '%u %a' -- "$TUNNEL_DIR")
  if [[ $uid != "$(id -u)" ]]; then
    fail "the tunnel directory $TUNNEL_DIR is not owned by the current user"
  fi
  if (((8#$mode & 8#077) != 0)); then
    fail "the tunnel directory $TUNNEL_DIR is accessible to group or others (mode $mode); run: chmod 700 $TUNNEL_DIR"
  fi
}

# check_private_file PATH WHAT: a regular, non-empty file of the current user, mode 0600 or tighter.
check_private_file() {
  local path=$1 what=$2 uid mode
  if [[ -L $path ]]; then
    fail "$what ($path) is a symlink; delete it and run scripts/local_mcp.sh setup"
  fi
  if [[ ! -f $path ]]; then
    fail "$what ($path) is missing or not a regular file; run scripts/local_mcp.sh setup"
  fi
  read -r uid mode < <(stat -c '%u %a' -- "$path")
  if [[ $uid != "$(id -u)" ]]; then
    fail "$what ($path) is not owned by the current user"
  fi
  if (((8#$mode & 8#077) != 0)); then
    fail "$what ($path) is accessible to group or others (mode $mode); run: chmod 600 $path"
  fi
  if [[ ! -s $path ]]; then
    fail "$what ($path) is empty; delete it and run scripts/local_mcp.sh setup"
  fi
}

# write_new_private PATH VALUE: a NEW 0600 file holding exactly VALUE. `printf` is a shell
# builtin: the value never reaches a command line; mktemp, ln and rm only see paths. `ln` never
# replaces an existing file.
write_new_private() {
  local path=$1 tmp
  tmp=$(mktemp -- "$TUNNEL_DIR/.new.XXXXXX")
  if ! printf '%s' "$2" >"$tmp" || ! ln -- "$tmp" "$path"; then
    rm -f -- "$tmp"
    fail "could not write $path (nothing was kept)"
  fi
  rm -f -- "$tmp"
}

# replace_file PATH < content: write a temporary file, then rename it over PATH (atomic).
replace_file() {
  local path=$1 tmp
  tmp=$(mktemp -- "$TUNNEL_DIR/.new.XXXXXX")
  if ! cat >"$tmp" || ! mv -f -- "$tmp" "$path"; then
    rm -f -- "$tmp"
    fail "could not write $path"
  fi
}

# json_get KEY: a value of credential.json (written by this script, one key per line).
json_get() {
  [[ -f $CREDENTIAL_FILE ]] || return 0
  sed -nE "s/^  \"$1\": \"?([^\",]*)\"?,?\$/\1/p" "$CREDENTIAL_FILE" | head -n 1
}

# credential_json: credential.json's content for CRED_ID, CRED_WORKSPACE, CRED_EXPIRES, CRED_EPOCH
# and CRED_SOURCE (set by create_credential).
credential_json() {
  printf '{\n'
  printf '  "credential_id": "%s",\n' "$CRED_ID"
  printf '  "workspace_id": "%s",\n' "$CRED_WORKSPACE"
  printf '  "label": "%s",\n' "$CREDENTIAL_LABEL"
  printf '  "role": "%s",\n' "$CREDENTIAL_ROLE"
  printf '  "scopes": ["deals:read", "reviews:read"],\n'
  printf '  "expires_at": "%s",\n' "$CRED_EXPIRES"
  printf '  "expires_at_epoch": %s,\n' "$CRED_EPOCH"
  printf '  "expires_source": "%s",\n' "$CRED_SOURCE"
  printf '  "created_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "authorization_file": "%s"\n' "$AUTH_FILE"
  printf '}\n'
}

# stage_credential_json: the new credential.json in a temporary file of TUNNEL_DIR (STAGED); every
# write that can fail (a full disk, say) happens here, before anything is replaced. 1 on failure.
stage_credential_json() {
  local content
  STAGED=""
  content=$(credential_json) || return 1
  STAGED=$(mktemp -- "$TUNNEL_DIR/.new.XXXXXX" 2>/dev/null) || {
    STAGED=""
    return 1
  }
  if ! printf '%s\n' "$content" >"$STAGED" 2>/dev/null; then
    rm -f -- "$STAGED"
    STAGED=""
    return 1
  fi
}

# commit_credential_json: rename STAGED over credential.json; on failure STAGED is kept and the
# command that finishes the job is printed (1).
commit_credential_json() {
  if mv -f -- "$STAGED" "$CREDENTIAL_FILE"; then
    STAGED=""
    return 0
  fi
  {
    echo "error: $CREDENTIAL_FILE could not be replaced; it does not name the credential in use," \
      "$CRED_ID (workspace $CRED_WORKSPACE, expires $CRED_EXPIRES). Its new content is in $STAGED;"
    echo "  once the problem is fixed, run: mv -f $STAGED $CREDENTIAL_FILE"
  } >&2
  return 1
}

# revoke_credential ID WORKSPACE REASON: `credentials revoke` (an already revoked credential is fine).
revoke_credential() {
  local args=(credentials revoke "$1" --reason "$3")
  if [[ $2 =~ $UUID_RE ]]; then args+=(--workspace "$2"); fi
  args+=(--yes)
  uv run suv-deals "${args[@]}"
}

# revoke_command ID WORKSPACE: the command the owner runs when revoking failed.
revoke_command() {
  printf 'uv run suv-deals credentials revoke %s%s --reason "..." --yes' "$1" "${2:+ --workspace $2}"
}

find_tunnel_client() {
  if [[ -n ${TUNNEL_CLIENT_BIN:-} ]]; then
    [[ -f $TUNNEL_CLIENT_BIN && -x $TUNNEL_CLIENT_BIN ]] || return 1
    printf '%s' "$TUNNEL_CLIENT_BIN"
    return 0
  fi
  command -v tunnel-client
}

# scrub_tunnel_env: unset every exported variable except TUNNEL_ENV_KEEP (call it in a subshell
# right before `exec`ing tunnel-client; `unset` and `compgen` are builtins, no value is passed on).
scrub_tunnel_env() {
  local name
  while IFS= read -r name; do
    if [[ $TUNNEL_ENV_KEEP != *" $name "* ]]; then unset "$name" 2>/dev/null || true; fi
  done < <(compgen -e)
}

# tunnel_client ARGS...: tunnel-client with the cleaned environment (header comment).
tunnel_client() { (scrub_tunnel_env && exec "$TC" "$@"); }

# tunnel_doctor: `tunnel-client doctor --explain` for tunnel-client.yaml with the health socket that
# `start` gives the tunnel (cleaned environment, no stdin). Sets DOCTOR_OUT (its whole output: check
# names, URLs and errors, no secret value), DOCTOR_STATUS (its exit status) and DOCTOR_FAILED (the
# comma-separated names of its one `FAILED_CHECKS` line; empty without exactly one such line).
tunnel_doctor() {
  DOCTOR_OUT=$(tunnel_client doctor --config "$CONFIG_FILE" --health.unix-socket "$HEALTH_SOCKET" \
    --explain </dev/null 2>&1) && DOCTOR_STATUS=0 || DOCTOR_STATUS=$?
  DOCTOR_FAILED=$(printf '%s\n' "$DOCTOR_OUT" |
    awk '/^FAILED_CHECKS / { n++; v = substr($0, 15) } END { if (n == 1) print v }')
  [[ $DOCTOR_FAILED =~ ^[a-z0-9_]+(,[a-z0-9_]+)*$ ]] || DOCTOR_FAILED=""
}

# doctor_unexpected: the names in DOCTOR_FAILED that are not expected before `start` (empty: only
# mcp_server_reachable and/or oauth_metadata failed; setup also requires mcp_server_reachable among
# them, i.e. that nothing answers the MCP URL yet).
doctor_unexpected() {
  local name names=()
  IFS=, read -r -a names <<<"$DOCTOR_FAILED"
  for name in "${names[@]}"; do
    if [[ $DOCTOR_EXPECTED_BEFORE_START != *" $name "* ]]; then printf '%s ' "$name"; fi
  done
}

# remove_stale_socket: delete health.sock left by an earlier run (the caller made sure that no tunnel
# of this directory runs); tunnel-client would fail to bind it ("address already in use").
remove_stale_socket() {
  if [[ -e $HEALTH_SOCKET || -L $HEALTH_SOCKET ]]; then
    if [[ -S $HEALTH_SOCKET && ! -L $HEALTH_SOCKET ]]; then
      rm -f -- "$HEALTH_SOCKET"
    else
      fail "$HEALTH_SOCKET exists and is not a socket; remove it"
    fi
  fi
}

render_config() {
  cat <<EOF
# tunnel-client configuration for dot (docs/connect_mcp.md section 8), written by
# scripts/local_mcp.sh and rewritten when LOCAL_MCP_PORT changes. No secret value here: the
# runtime API key and the MCP Authorization header are file: references.
config_version: 1
control_plane:
  tunnel_id: "$TUNNEL_ID"
  api_key: "file:$API_KEY_FILE"
mcp:
  server_urls:
    - channel: main
      url: "$MCP_URL"
  extra_headers:
    Authorization: "file:$AUTH_FILE"
log:
  level: info
EOF
}

# ensure_config: write tunnel-client.yaml when it is missing or differs (port, paths, tunnel id).
ensure_config() {
  local tmp
  if [[ -L $CONFIG_FILE ]]; then
    fail "$CONFIG_FILE is a symlink; delete it and run scripts/local_mcp.sh setup"
  fi
  tmp=$(mktemp -- "$TUNNEL_DIR/.new.XXXXXX")
  render_config >"$tmp"
  if [[ -f $CONFIG_FILE ]] && cmp -s -- "$tmp" "$CONFIG_FILE"; then
    rm -f -- "$tmp"
    echo "  tunnel-client.yaml: unchanged ($CONFIG_FILE)"
    return 0
  fi
  mv -f -- "$tmp" "$CONFIG_FILE"
  echo "  tunnel-client.yaml: written ($CONFIG_FILE, mode 0600; MCP server $MCP_URL)"
}

# prompt_hidden VAR PROMPT: read one line without echo (bash builtins only).
prompt_hidden() {
  local value=""
  printf '%s' "$2" >&2
  IFS= read -rs value || true
  printf '\n' >&2
  value=${value%$'\r'}
  printf -v "$1" '%s' "$value"
}

# --------------------------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------------------------

# create_credential TARGET: `credentials create-mcp --authorization-file TARGET`; sets CRED_ID,
# CRED_WORKSPACE, CRED_EXPIRES, CRED_EPOCH and CRED_SOURCE from its (token-free) output.
create_credential() {
  local target=$1 out status
  local args=(
    credentials create-mcp --label "$CREDENTIAL_LABEL" --role "$CREDENTIAL_ROLE"
    --scopes "$CREDENTIAL_SCOPES" --expires "${CREDENTIAL_DAYS}d" --authorization-file "$target"
  )
  if [[ -n $WORKSPACE ]]; then args+=(--workspace "$WORKSPACE"); fi
  args+=(--yes)
  out=$(uv run suv-deals "${args[@]}" 2>&1) && status=0 || status=$?
  printf '%s\n' "$out" | sed 's/^/    /'
  if ((status != 0)); then
    return 1
  fi
  CRED_ID=$(printf '%s\n' "$out" | sed -nE 's/^Created credential ([0-9a-fA-F-]{36}) in workspace ([0-9a-fA-F-]{36})$/\1/p' | head -n 1)
  CRED_WORKSPACE=$(printf '%s\n' "$out" | sed -nE 's/^Created credential ([0-9a-fA-F-]{36}) in workspace ([0-9a-fA-F-]{36})$/\2/p' | head -n 1)
  CRED_EXPIRES=$(printf '%s\n' "$out" | sed -nE 's/^  expires : ([0-9T:.+-]+Z?)$/\1/p' | head -n 1)
  if [[ ! $CRED_ID =~ $UUID_RE ]]; then
    echo "error: the credential id is not in the output above; find it with" \
      "\`uv run suv-deals credentials list\`, revoke it and delete $target" >&2
    return 1
  fi
  if [[ -n $CRED_EXPIRES ]] && CRED_EPOCH=$(date -d "$CRED_EXPIRES" +%s 2>/dev/null); then
    CRED_SOURCE="create-mcp output"
  else
    CRED_EPOCH=$(($(date +%s) + CREDENTIAL_DAYS * 86400))
    CRED_EXPIRES=$(date -u -d "@$CRED_EPOCH" +%Y-%m-%dT%H:%M:%SZ)
    CRED_SOURCE="computed: now + ${CREDENTIAL_DAYS} days (no expiry in the create-mcp output)"
    echo "  note: create-mcp printed no expiry; recorded now + ${CREDENTIAL_DAYS} days ($CRED_EXPIRES)"
  fi
}

# expiry_check: refuse when the credential expired, warn within EXPIRY_WARN_DAYS days (S7).
expiry_check() {
  local epoch left
  epoch=$(json_get expires_at_epoch)
  [[ $epoch =~ ^[0-9]+$ ]] || fail "credential.json has no expiry ($CREDENTIAL_FILE); run scripts/local_mcp.sh rotate"
  left=$((epoch - $(date +%s)))
  if ((left <= 0)); then
    fail "the MCP credential $(json_get credential_id) expired at $(json_get expires_at);" \
      "run scripts/local_mcp.sh rotate, then start again"
  fi
  if ((left < EXPIRY_WARN_DAYS * 86400)); then
    echo "WARNING: the MCP credential expires in $((left / 86400)) day(s) ($(json_get expires_at));" \
      "run scripts/local_mcp.sh rotate soon" >&2
  fi
}

# require_setup: everything `setup` creates is present and private; sets TC and TUNNEL_ID.
require_setup() {
  check_tunnel_dir existing
  TC=$(find_tunnel_client) || fail "setup is incomplete: tunnel-client was not found (TUNNEL_CLIENT_BIN or PATH); run scripts/local_mcp.sh setup"
  local file
  for file in "$TUNNEL_ID_FILE" "$API_KEY_FILE" "$AUTH_FILE" "$CREDENTIAL_FILE"; do
    if [[ ! -e $file && ! -L $file ]]; then
      fail "setup is incomplete: $file is missing; run scripts/local_mcp.sh setup"
    fi
  done
  check_private_file "$TUNNEL_ID_FILE" "the tunnel id file"
  check_private_file "$API_KEY_FILE" "the runtime API key file"
  check_private_file "$AUTH_FILE" "the MCP authorization file"
  TUNNEL_ID=$(<"$TUNNEL_ID_FILE")
  [[ $TUNNEL_ID =~ $TUNNEL_ID_RE ]] || fail "$TUNNEL_ID_FILE holds no valid tunnel id; delete it and run scripts/local_mcp.sh setup"
  [[ $(json_get credential_id) =~ $UUID_RE ]] || fail "setup is incomplete: $CREDENTIAL_FILE names no credential; run scripts/local_mcp.sh setup"
}

# --------------------------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------------------------

setup() {
  umask 077
  local version digest tunnel_id_input api_key old_id old_workspace
  echo "1/6 tunnel-client"
  if ! TC=$(find_tunnel_client); then
    {
      echo "error: tunnel-client was not found (TUNNEL_CLIENT_BIN or PATH). Download the Linux x86-64"
      echo "  build from the Platform Tunnels page, signed in as the owner:"
      echo "    $TUNNELS_PAGE"
      echo "  unpack it and put the tunnel-client binary in ~/.local/bin, make it executable, e.g."
      echo "    mkdir -p ~/.local/bin && cp /mnt/c/Users/<you>/Downloads/<unpacked>/tunnel-client ~/.local/bin/"
      echo "    chmod +x ~/.local/bin/tunnel-client"
      echo "  (~/.local/bin is on Ubuntu's PATH once it exists, after a new login; or set"
      echo "  TUNNEL_CLIENT_BIN=/path/to/tunnel-client), then run scripts/local_mcp.sh setup again."
    } >&2
    exit 1
  fi
  version=$(tunnel_client --version 2>&1) || fail "\`$TC --version\` failed: $version"
  read -r digest _ < <(sha256sum -- "$TC") || fail "could not compute the sha256 of $TC"
  echo "  $TC: $version"
  echo "  sha256 $digest (compare it with the release you verified, docs/connect_mcp.md 8.3 step 1)"

  echo "2/6 tunnel directory $TUNNEL_DIR"
  check_tunnel_dir create
  printf '%s\nsha256 %s\n' "$version" "$digest" | replace_file "$VERSION_FILE"

  echo "3/6 tunnel id (from $TUNNELS_PAGE)"
  if [[ -e $TUNNEL_ID_FILE || -L $TUNNEL_ID_FILE ]]; then
    check_private_file "$TUNNEL_ID_FILE" "the tunnel id file"
    TUNNEL_ID=$(<"$TUNNEL_ID_FILE")
    [[ $TUNNEL_ID =~ $TUNNEL_ID_RE ]] || fail "$TUNNEL_ID_FILE holds no valid tunnel id; delete it and run setup again"
    echo "  kept: $TUNNEL_ID"
  else
    prompt_hidden tunnel_id_input "  tunnel id (tunnel_...; input hidden): "
    if [[ -z $tunnel_id_input ]]; then
      fail "no tunnel id entered; nothing was written"
    fi
    if [[ ! $tunnel_id_input =~ $TUNNEL_ID_RE ]]; then
      fail "that is not a tunnel id (tunnel_ and 32 lowercase letters or digits, or tunnel_, a" \
        "4-character namespace, _ and 32); the input is not shown; nothing was written"
    fi
    TUNNEL_ID=$tunnel_id_input
    write_new_private "$TUNNEL_ID_FILE" "$TUNNEL_ID"
    echo "  saved: $TUNNEL_ID ($TUNNEL_ID_FILE)"
  fi

  echo "4/6 runtime API key (a restricted key: Tunnels Read + Use; $API_KEYS_PAGE)"
  if [[ -e $API_KEY_FILE || -L $API_KEY_FILE ]]; then
    check_private_file "$API_KEY_FILE" "the runtime API key file"
    echo "  kept: $API_KEY_FILE (mode 0600; not shown)"
  else
    prompt_hidden api_key "  OpenAI runtime API key (input hidden): "
    if [[ -z $api_key ]]; then
      fail "no API key entered; nothing was written"
    fi
    if [[ ! $api_key =~ ^[[:graph:]]{20,512}$ ]]; then
      api_key=""
      fail "that does not look like an API key (20-512 visible characters, no spaces); nothing was written"
    fi
    case $api_key in
      tunnel_*) api_key=""; fail "that is the tunnel id, not the API key; nothing was written" ;;
      suvmcp_* | Bearer*) api_key=""; fail "that is an MCP credential, not the OpenAI runtime API key; nothing was written" ;;
      sk-admin-*)
        api_key=""
        fail "that looks like an admin key; the tunnel needs a restricted runtime key (Tunnels Read + Use only); nothing was written"
        ;;
      sk-*) ;;
      *) echo "  note: OpenAI API keys usually start with sk-; saving it anyway" ;;
    esac
    write_new_private "$API_KEY_FILE" "$api_key"
    api_key=""
    echo "  saved: $API_KEY_FILE (mode 0600; not shown)"
  fi

  echo "5/6 MCP credential (viewer; deals:read, reviews:read; ${CREDENTIAL_DAYS} days)"
  if [[ -e $AUTH_FILE || -L $AUTH_FILE ]]; then
    check_private_file "$AUTH_FILE" "the MCP authorization file"
    if [[ ! $(json_get credential_id) =~ $UUID_RE ]]; then
      fail "$AUTH_FILE exists but $CREDENTIAL_FILE does not name its credential: find it with" \
        "\`uv run suv-deals credentials list\`, revoke it (\`credentials revoke <id> --reason ... --yes\`)," \
        "delete $AUTH_FILE and run setup again"
    fi
    echo "  kept: credential $(json_get credential_id), expires $(json_get expires_at) ($AUTH_FILE; not shown)"
  else
    # A credential recorded without its token file (deleted, or a symlink that was removed): revoke
    # it first, so that no live token remains that nobody holds (a leaked copy stops working).
    old_id=$(json_get credential_id)
    old_workspace=$(json_get workspace_id)
    if [[ $old_id =~ $UUID_RE ]]; then
      echo "  $CREDENTIAL_FILE names credential $old_id, but $AUTH_FILE is gone: revoking it first"
      if ! revoke_credential "$old_id" "$old_workspace" "replaced by scripts/local_mcp.sh setup"; then
        fail "revoking the recorded credential $old_id failed (output above); nothing was created." \
          "Revoke it yourself: $(revoke_command "$old_id" "$old_workspace"); if it no longer exists" \
          "(\`uv run suv-deals credentials list --all\`), delete $CREDENTIAL_FILE. Then run setup again"
      fi
      rm -f -- "$CREDENTIAL_FILE"
    fi
    if ! create_credential "$AUTH_FILE"; then
      fail "credentials create-mcp failed (output above); nothing else was changed"
    fi
    if ! stage_credential_json; then
      echo "error: could not write $CREDENTIAL_FILE; taking the new credential $CRED_ID back" >&2
      if revoke_credential "$CRED_ID" "$CRED_WORKSPACE" "setup could not record it"; then
        rm -f -- "$AUTH_FILE"
        fail "the new credential was revoked and $AUTH_FILE deleted; fix $TUNNEL_DIR (space, permissions) and run setup again"
      fi
      fail "revoking it failed too: $(revoke_command "$CRED_ID" "$CRED_WORKSPACE"), then delete $AUTH_FILE"
    fi
    commit_credential_json || exit 1
    echo "  created credential $CRED_ID (expires $CRED_EXPIRES; $CRED_SOURCE); token in $AUTH_FILE, not shown"
  fi

  echo "6/6 tunnel-client.yaml and \`tunnel-client doctor --explain\`"
  ensure_config
  if running tunnel; then
    echo "  doctor skipped: the tunnel is running (pid $(read_pid tunnel)) and holds its health socket;" \
      "stop it and run setup again to check the configuration"
    echo "setup complete."
    return 0
  fi
  remove_stale_socket
  tunnel_doctor
  printf '%s\n' "$DOCTOR_OUT" | sed 's/^/    /'
  if ((DOCTOR_STATUS == 0)); then
    echo "  doctor: RESULT ok"
    if port_in_use "$PORT"; then
      echo "  note: something already answers $MCP_URL; start needs port $PORT free (it runs its own" \
        "API there and refuses a port in use)"
    fi
  elif [[ -z $DOCTOR_FAILED ]]; then
    fail "tunnel-client doctor failed (exit $DOCTOR_STATUS) and its output (above) has no FAILED_CHECKS" \
      "line naming the failed checks; fix what it reports, then run scripts/local_mcp.sh setup again"
  elif [[ -n $(doctor_unexpected) ]]; then
    fail "tunnel-client doctor failed (exit $DOCTOR_STATUS): failed checks other than" \
      "mcp_server_reachable and oauth_metadata: $(doctor_unexpected)(all: $DOCTOR_FAILED); the" \
      "explanation is above. Fix them, then run scripts/local_mcp.sh setup again"
  else
    # Expected before start only while nothing answers the MCP URL. oauth_metadata failing alone
    # means that something answers it (mcp_server_reachable passed), and it is not this route's API,
    # which runs only under start (v0.0.16 passes oauth_metadata on the API's 404s).
    local answering=""
    if [[ ,$DOCTOR_FAILED, != *,mcp_server_reachable,* ]]; then
      answering="mcp_server_reachable passed: a program answers $MCP_URL"
    elif port_in_use "$PORT"; then
      answering="a program listens on 127.0.0.1:$PORT"
    fi
    if [[ -n $answering ]]; then
      fail "tunnel-client doctor failed (exit $DOCTOR_STATUS; failed checks: $DOCTOR_FAILED) and" \
        "$answering, which is not this route's API (another program, or an API started separately with" \
        "other settings). start would refuse port $PORT: stop that program or choose a free port with" \
        "LOCAL_MCP_PORT=<port>, then run scripts/local_mcp.sh setup again"
    fi
    echo "  doctor: only ${DOCTOR_FAILED//,/ and } failed. That is expected before scripts/local_mcp.sh" \
      "start: the API is not running yet (nothing answers $MCP_URL); start runs doctor again with the API up."
  fi
  echo "setup complete. Next: scripts/local_mcp.sh start (and keep that window open)."
}

# --------------------------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------------------------

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

http_code() { curl --noproxy '*' -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true; }

tunnel_ready_code() {
  if [[ ! -S $HEALTH_SOCKET ]]; then
    printf '000'
    return 0
  fi
  curl -s -o /dev/null -m 3 -w '%{http_code}' --unix-socket "$HEALTH_SOCKET" http://localhost/readyz 2>/dev/null || true
}

# The control-plane component of the health JSON as five words (see control_plane), for python3 in
# isolated mode and for jq. Every value that is not a plain word becomes "-".
CP_PY='import json, re, sys
def word(value):
    text = str(value) if isinstance(value, (str, int, float)) else ""
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", text) else "-"
try:
    cp = json.load(sys.stdin)["components"]["control-plane"]
    details = cp.get("details") or {}
    values = (cp.get("status"), cp.get("state"), details.get("http_status"),
              details.get("consecutive_failures"), details.get("failure_category") or cp.get("reason_code"))
    print(" ".join(word(v) for v in values))
except Exception:
    print("- - - - -")'
# shellcheck disable=SC2016 # a jq program: $c is jq's variable, not the shell's
CP_JQ='.components["control-plane"] as $c
  | [$c.status, $c.state, $c.details.http_status, $c.details.consecutive_failures,
     ($c.details.failure_category // $c.reason_code)]
  | map(if type == "string" or type == "number" then tostring else "" end
        | if test("^[A-Za-z0-9_.:-]{1,64}$") then . else "-" end)
  | join(" ")'
CP_WORDS_RE='^[A-Za-z0-9_.:-]+( [A-Za-z0-9_.:-]+){4}$'

# cp_sed JSON: the conservative fallback without python3 and jq. It looks only at the control-plane
# object up to its first closing brace (its details) and recognises only "status":"ok" as its first
# key, http_status 401, 403 or 404 and consecutive_failures; everything else is "-".
cp_sed() {
  local segment status=- http=- failures=-
  segment=$(printf '%s' "$1" | sed -n 's/.*"control-plane":{\([^}]*\)}.*/\1/p' | head -n 1)
  if [[ $segment == '"status":"ok",'* || $segment == '"status":"ok"' ]]; then status=ok; fi
  if [[ $segment =~ \"http_status\":(401|403|404)(,|$) ]]; then http=${BASH_REMATCH[1]}; fi
  if [[ $segment =~ \"consecutive_failures\":([0-9]{1,6})(,|$) ]]; then failures=${BASH_REMATCH[1]}; fi
  printf '%s - %s %s -' "$status" "$http" "$failures"
}

# control_plane: the tunnel's GET /health?details=true on its health socket, reduced to the
# control-plane component's "status state http_status consecutive_failures failure_category" ("-"
# for a missing or unusual value); 1 without an answer. python3 -I if present, else jq, else cp_sed.
# The JSON is only parsed, never evaluated.
control_plane() {
  local json line=""
  [[ -S $HEALTH_SOCKET ]] || return 1
  json=$(curl -s -m 3 --unix-socket "$HEALTH_SOCKET" 'http://localhost/health?details=true' 2>/dev/null |
    head -c 262144) || true
  [[ -n $json ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    line=$(printf '%s' "$json" | python3 -I -c "$CP_PY" 2>/dev/null) || line=""
  fi
  if [[ ! $line =~ $CP_WORDS_RE ]] && command -v jq >/dev/null 2>&1; then
    line=$(printf '%s' "$json" | jq -r "$CP_JQ" 2>/dev/null) || line=""
  fi
  if [[ ! $line =~ $CP_WORDS_RE ]]; then line=$(cp_sed "$json"); fi
  printf '%s' "$line"
}

# OpenAI's own words for a rejection, from the tunnel log. tunnel-client v0.0.16 logs every failed
# control-plane request as a struct-text line with the attribute error="..." (Go quoting), e.g.
#   WARN poll failed; backing off ... component=controlplane error="controlplane client: unexpected
#     status 401: invalid_api_key: Incorrect API key provided: sk-proj-****...56cc. ..." ...
#   WARN tunnel metadata fetch failed ... error="controlplane client: unexpected metadata status
#     404: Tunnel not found"
# i.e. "unexpected [metadata ]status <HTTP>: [<code>: ]<text>" (the code and OpenAI's message;
# tunnel-client appends " mitigation: ..." for the codes it knows). OpenAI masks the key itself.
OA_LOG_BYTES=1048576 # only the end of the log is read
OA_TEXT_MAX=400      # characters of "<code>: <text>" shown
OA_ERROR_RE='(^| )error="(([^"\\]|\\.)*)"'
OA_CP_RE='^controlplane client: unexpected (metadata )?status ([1-5][0-9][0-9]): (.+)$'
OA_CODE_RE='^([a-z][a-z0-9]*(_[a-z0-9]+)+): '
# A key starts after a character that cannot be part of one, or right after an escape of Go's
# quoting (\n, \t, \xHH, \uHHHH, \UHHHHHHHH: a letter or digit just before the key).
OA_KEY_RE='(^|[^A-Za-z0-9_*-]|\\[A-Za-z]|\\x[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8})(sk-[A-Za-z0-9_*-]+)'
ROLES_PAGE=https://platform.openai.com/settings/organization/people/roles

# oa_clean TEXT: TEXT for the window and tunnel.rejected: printable ASCII only (every other byte
# becomes "?"), every sk-... run that holds no "*" (a key OpenAI did not mask) replaced by
# "sk-[masked]" (also right after a Go escape such as \n), at most OA_TEXT_MAX characters.
# Parameter expansion only: nothing is evaluated.
oa_clean() {
  local LC_ALL=C text=$1 out="" whole lead run
  text=${text//[^ -~]/?}
  while [[ $text =~ $OA_KEY_RE ]]; do
    whole=${BASH_REMATCH[0]} lead=${BASH_REMATCH[1]} run=${BASH_REMATCH[2]}
    out+=${text%%"$whole"*}$lead
    text=${text#*"$whole"}
    if [[ $run == *'*'* || ${#run} -lt 11 ]]; then out+=$run; else out+='sk-[masked]'; fi
  done
  text=$out$text
  if ((${#text} > OA_TEXT_MAX)); then text="${text:0:OA_TEXT_MAX-3}..."; fi
  printf '%s' "$text"
}

# openai_says HTTP LOG: OpenAI's words for HTTP in LOG (see above). Sets OA_SAYS ("<code>: <text>",
# or the text alone; oa_clean'ed), OA_CODE (the code, or empty) and OA_META404 (1 when LOG also
# holds a metadata 404 "Tunnel not found"). The last poll failure with status HTTP wins over a
# metadata line with it; 1 when LOG has neither. The lines are only matched, never evaluated.
openai_says() {
  local http=$1 log=$2 line value metadata status rest poll="" meta=""
  OA_SAYS="" OA_CODE="" OA_META404=0
  [[ -f $log ]] || return 1
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ $OA_ERROR_RE ]] || continue
    value=${BASH_REMATCH[2]}
    [[ $value =~ $OA_CP_RE ]] || continue
    metadata=${BASH_REMATCH[1]} status=${BASH_REMATCH[2]} rest=${BASH_REMATCH[3]}
    if [[ -n $metadata && $status == 404 && $rest == "Tunnel not found"* ]]; then OA_META404=1; fi
    if [[ $status != "$http" ]]; then continue; fi
    if [[ -n $metadata ]]; then meta=$rest; else poll=$rest; fi
  done < <(tail -c "$OA_LOG_BYTES" -- "$log" 2>/dev/null | grep -a -F 'error="controlplane client: unexpected ')
  rest=${poll:-$meta}
  [[ -n $rest ]] || return 1
  rest=${rest//\\\"/\"} # Go's quoting: \" is "
  if [[ $rest =~ $OA_CODE_RE ]]; then OA_CODE=${BASH_REMATCH[1]}; fi
  OA_SAYS=$(oa_clean "$rest")
}

# say WORDS...: one line (printf: no option or backslash in WORDS is interpreted).
say() { printf '%s\n' "$*"; }

# rejected_message HTTP FAILURES: why OpenAI's control plane keeps answering HTTP 401, 403 or 404
# (tunnel-client would retry for ever), for tunnel.rejected: a first line (the summary), then
# "OpenAI says: <code>: <text>" from the tunnel log (openai_says) and the cause and the fix, chosen
# by OpenAI's code (one HTTP status has several causes: a failed poll gets 401 for a wrong key,
# invalid_api_key, and, per the vendor's OpenAPI, for tunnel_use_forbidden, tunnel_missing_principals
# and tunnel_active_organization_required), else by the status. Sets OA_CODE. (The vendor's
# invalid_tunnel_id_format is HTTP 400 and never reaches this: v0.0.16 refuses a malformed id at
# startup, and the watcher does not stop on 400.)
rejected_message() {
  local http=$1 polls=$2 log="" said=0 new_key
  local polled="control plane HTTP $http on $polls polls in a row"
  log=$(current_log tunnel) || log=""
  if openai_says "$http" "$log"; then said=1; fi
  new_key="delete $API_KEY_FILE and run scripts/local_mcp.sh setup"
  case $OA_CODE in
    invalid_api_key)
      say "OpenAI rejected the runtime API key ($polled)"
      say "OpenAI says: $OA_SAYS"
      say "Cause: the key in $API_KEY_FILE is wrong, truncated or revoked (invalid_api_key)."
      say "Fix: compare the last 4 characters of the masked key above with the key list on" \
        "$API_KEYS_PAGE; $new_key with the right restricted key (Tunnels Read + Use)."
      ;;
    tunnel_use_forbidden)
      say "OpenAI denies the runtime API key's principal the use of the tunnel $TUNNEL_ID ($polled)"
      say "OpenAI says: $OA_SAYS"
      say "Cause: the key is valid, but OpenAI lets its principal (the person or service account that owns" \
        "the key) use no tunnel with the id $TUNNEL_ID (tunnel_use_forbidden): either that principal lacks" \
        "the tunnel permissions, or the id is not that of a tunnel in the key's organization (a typo, a" \
        "deleted and recreated tunnel, another organization's tunnel)."
      if ((OA_META404)); then
        say "The metadata 404 \"Tunnel not found\" in the same log fits both (a tunnel hidden from that" \
          "principal, or no tunnel with this id); only step 1 tells them apart."
      fi
      say "Fix, on the Platform (tunnel permissions are organization-level, not per project):"
      say "  1. first the id: compare $TUNNEL_ID with the tunnel's id on $TUNNELS_PAGE in the organization" \
        "of the key's project (organization switcher). Another id there (a typo, a deleted and recreated" \
        "tunnel): delete $TUNNEL_ID_FILE and run scripts/local_mcp.sh setup with that id (the key is kept)," \
        "then start again. The tunnel not there: it is in another organization (step 2). The same id: keep" \
        "$TUNNEL_ID_FILE and go on;"
      say "  2. the tunnel and the key's project are in the SAME organization (organization switcher);"
      say "  3. the owner (the key's principal) has Tunnels Read + Use through an organization role or" \
        "group, or is assigned the tunnel's own User role ($ROLES_PAGE; a role change can take up to 30" \
        "minutes);"
      say "  4. the key is Owned by: You (not a service account without that role) and Restricted with" \
        "Tunnels Read + Use ($API_KEYS_PAGE);"
      say "  5. after a change in step 2, 3 or 4, create a NEW key (after a role change the vendor advises" \
        "new keys), $new_key."
      ;;
    tunnel_missing_principals)
      say "OpenAI refused the runtime API key for the tunnel $TUNNEL_ID ($polled)"
      say "OpenAI says: $OA_SAYS"
      say "Cause: the key is not associated with the tunnel's organization or workspace" \
        "(tunnel_missing_principals)."
      say "Fix (tunnel-client's mitigation: use a runtime API key associated with the intended" \
        "organization or workspace): create a restricted key (Tunnels Read + Use) in a project of the" \
        "tunnel's organization ($API_KEYS_PAGE, organization switcher), $new_key."
      ;;
    tunnel_active_organization_required)
      say "OpenAI needs the organization id of the tunnel $TUNNEL_ID ($polled)"
      say "OpenAI says: $OA_SAYS"
      say "Cause: the request does not name the tunnel's organization (tunnel_active_organization_required)."
      say "Fix (tunnel-client's mitigation): set control_plane.organization_id to the tunnel's" \
        "organization ID. scripts/local_mcp.sh does not set an organization id (tunnel-client.yaml has" \
        "none, and start rewrites that file), so this needs a change to the script."
      ;;
    *)
      # No code in the log (or no log line), or a code this script does not know: the texts by
      # HTTP status, with OpenAI's own words when the log has them.
      case $http in
        401) say "OpenAI rejected the runtime API key ($polled)" ;;
        403) say "OpenAI refused the tunnel ($polled)" ;;
        *) say "OpenAI does not know the tunnel $TUNNEL_ID ($polled)" ;;
      esac
      if ((said)); then
        say "OpenAI says: $OA_SAYS"
        if [[ -n $OA_CODE ]]; then
          say "Note: scripts/local_mcp.sh does not know the code $OA_CODE; OpenAI's words above name the" \
            "cause (a \"mitigation:\" part is tunnel-client's advice for it). Generic advice for HTTP $http:"
        fi
      else
        say "The tunnel log${log:+ ($log)} holds no control-plane error with HTTP $http: OpenAI's own words" \
          "are not known."
      fi
      case $http in
        401)
          say "Cause: HTTP 401 comes with a wrong, truncated or revoked key (invalid_api_key) and, for a" \
            "valid key, with tunnel_use_forbidden (its principal may not use this tunnel, or the id is not" \
            "the tunnel's), tunnel_missing_principals (the key is not associated with the tunnel's" \
            "organization or workspace) and tunnel_active_organization_required (the request does not name" \
            "the tunnel's organization); docs/connect_mcp.md 8.7."
          say "Fix: for a wrong key, $new_key with the right restricted key (Tunnels Read + Use;" \
            "$API_KEYS_PAGE); for a valid key, compare $TUNNEL_ID with the id on $TUNNELS_PAGE (another id:" \
            "delete $TUNNEL_ID_FILE and run setup), check that the tunnel and the key's project are in the" \
            "same organization and that the key's principal has Tunnels Read + Use ($ROLES_PAGE), then" \
            "create a new key and run setup the same way."
          ;;
        403)
          say "Cause: the runtime API key's principal (its owner) lacks Tunnels Read + Use for $TUNNEL_ID," \
            "or the tunnel belongs to another organization."
          say "Fix: give the key's principal Tunnels Read + Use ($ROLES_PAGE) and create a new key, or" \
            "create the key in a project of the tunnel's organization ($API_KEYS_PAGE); $new_key. For a" \
            "wrong tunnel id (compare it with $TUNNELS_PAGE) delete $TUNNEL_ID_FILE instead."
          ;;
        *)
          say "Cause: a wrong tunnel id, or a tunnel hidden from the key's principal (another organization," \
            "no Tunnels Read + Use)."
          say "Fix: delete $TUNNEL_ID_FILE and run scripts/local_mcp.sh setup with the id from $TUNNELS_PAGE." \
            "If the id there is the same, keep it and check the organization and Tunnels Read + Use as for" \
            "tunnel_use_forbidden (docs/connect_mcp.md 8.7)."
          ;;
      esac
      ;;
  esac
}

# show_rejection INDENT FIRST_PREFIX FIRST_SUFFIX: tunnel.rejected for the window or `status`: its
# first line between the prefix and the suffix, every other line indented.
show_rejection() {
  local line first=1
  while IFS= read -r line || [[ -n $line ]]; do
    if ((first)); then
      printf '%s%s%s\n' "$2" "$line" "$3"
      first=0
    else
      printf '%s%s\n' "$1" "$line"
    fi
  done <"$STATE_DIR/tunnel.rejected"
}

# launch PROCESS LOGFILE COMMAND...: detached (own session, SIGHUP ignored), pid, start time and
# log recorded. The background child is no process group leader, so `setsid` does not fork: the
# pid stays the same through setsid, nohup, env and uv, and it is the id of the new session.
launch() {
  local proc=$1 log=$2 pid started
  shift 2
  setsid nohup "$@" >"$log" 2>&1 </dev/null 9>&- &
  pid=$!
  printf '%s\n' "$pid" >"$(pid_file "$proc")"
  if started=$(proc_starttime "$pid"); then
    printf '%s\n' "$started" >"$(starttime_file "$proc")"
  else
    rm -f "$(starttime_file "$proc")"
  fi
  printf '%s\n' "$log" >"$(logfile_record "$proc")"
  rm -f "$(stopping_marker "$proc")"
}

# api_owns_port: every socket listening on port PORT (IPv4 or IPv6) belongs to a process of the
# API's session (the API's pid is the session id; uv's python child, which binds the port, inherits
# it). Read from /proc, without ss or lsof. False when nothing listens there.
api_owns_port() {
  local api_pid hex table st local_addr inode pid inodes=() fds=""
  api_pid=$(read_pid api) || return 1
  printf -v hex '%04X' "$PORT"
  for table in /proc/net/tcp /proc/net/tcp6; do
    [[ -r $table ]] || continue
    # sl local_address rem_address st tx:rx tr:when retrnsmt uid timeout inode ...
    while read -r _ local_addr _ st _ _ _ _ _ inode _; do
      if [[ $st == 0A && $local_addr == *:"$hex" ]]; then inodes+=("$inode"); fi
    done <"$table"
  done
  ((${#inodes[@]})) || return 1
  for pid in $(ps -s "$api_pid" -o pid= 2>/dev/null); do
    fds+=$(ls -l "/proc/$pid/fd" 2>/dev/null)
  done
  for inode in "${inodes[@]}"; do
    [[ $fds == *"socket:[$inode]"* ]] || return 1
  done
}

# signal_once PROCESS: SIGTERM unless already sent to this pid (`stop` and the start window may
# both try; the marker is created atomically, so only one of them signals).
signal_once() {
  local proc=$1 pid marker
  pid=$(read_pid "$proc") || return 0
  marker=$(stopping_marker "$proc")
  if [[ -f $marker && $(<"$marker") != "$pid" ]]; then rm -f "$marker"; fi
  if (set -C && printf '%s\n' "$pid" >"$marker") 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    echo "$proc (pid $pid): SIGTERM sent"
  else
    echo "$proc (pid $pid): SIGTERM already sent, waiting"
  fi
}

# wait_stopped PROCESS [CHECK]: up to STOP_TIMEOUT seconds while CHECK (default `running`) holds;
# 1 if it still runs.
wait_stopped() {
  local waited=0 check=${2:-running}
  while "$check" "$1"; do
    if ((waited >= STOP_TIMEOUT * 10)); then return 1; fi
    sleep 0.1
    waited=$((waited + 1))
  done
  forget "$1"
}

# The start window's EXIT trap: stop the watcher and the API this run started.
on_exit() {
  local status=$?
  set +e # a closed window makes echo fail; the API must be stopped all the same
  if [[ -n $WATCHER_PID ]]; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
    WATCHER_PID=
  fi
  if ((API_STARTED)); then
    API_STARTED=0
    if alive api; then
      signal_once api
      if wait_stopped api alive; then
        echo "api: stopped"
      else
        echo "error: the API (pid $(read_pid api)) is still running after ${STOP_TIMEOUT}s; run scripts/local_mcp.sh stop" >&2
        status=1
      fi
    else
      forget api
    fi
  fi
  exit "$status"
}

# probe_summary FILE: the probe's --json report in a few lines (jq or python3; the raw file otherwise).
probe_summary() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '"  protocol \(.protocol_version // "-"); \(.tools | length) tool(s); tools/list sha256 \(.tools_sha256 // "-")",
      (.checks[] | "  \(if .ok then "PASS" else "FAIL" end)  \(.name): \(.detail)"),
      "  request ids: \(.request_ids | join(", "))"' "$1" 2>/dev/null && return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -I - "$1" <<'EOF' 2>/dev/null && return 0
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"  protocol {r.get('protocol_version') or '-'}; {len(r.get('tools') or [])} tool(s); "
      f"tools/list sha256 {r.get('tools_sha256') or '-'}")
for c in r.get("checks") or []:
    print(f"  {'PASS' if c.get('ok') else 'FAIL'}  {c.get('name')}: {c.get('detail')}")
print(f"  request ids: {', '.join(r.get('request_ids') or [])}")
EOF
  fi
  sed 's/^/  /' "$1"
}

# watch_tunnel (background job of the start window, ended by the EXIT trap):
# - stops the tunnel as soon as the API this run launched is gone, so that tunnel-client never sends
#   the credential to whatever binds 127.0.0.1:PORT next. The API is checked before the tunnel:
#   `stop` ends the API only after the tunnel, so "API gone, tunnel running" means it died;
# - reads the control-plane component of the tunnel's health (every second while it is not ok,
#   every 5 s while it is; /readyz answers 200 even while OpenAI refuses the key): says "connected"
#   once it is ok; on HTTP 401, 403 or 404 for REJECT_AFTER polls in a row records the cause in
#   tunnel.rejected and stops the tunnel (the window then exits 1); any other failure (network, 5xx,
#   backoff) gets one warning per kind and the tunnel keeps retrying; nothing decided within
#   READY_TIMEOUT: a hint.
# consecutive_failures counts failures of every kind (network errors, 5xx, 401 ...) and http_status
# names only the latest one, so a run of rejections is counted here: it starts at the first sample
# that shows the code (failures F0) and holds while every later sample shows the same code and
# failure category with a counter that did not go back; then failures - F0 + 1 polls in a row were
# rejected. Any other sample (another code or kind, ok, a reset counter, no answer) ends the run.
watch_tunnel() {
  local ticks=0 connected=0 healthy=0 hinted=0 warned="" tunnel_pid="" words status state http failures category
  local run_code="" run_category="" run_first=0 run_last=0 in_a_row=0
  while :; do
    sleep 0.5
    ticks=$((ticks + 1))
    if [[ -z $tunnel_pid ]]; then
      tunnel_pid=$(read_pid tunnel) || tunnel_pid=""
    fi
    if ! alive api; then
      if [[ -z $tunnel_pid ]]; then continue; fi # the tunnel is starting: stop it once it has a pid
      if pid_alive "$tunnel_pid"; then
        : >"$STATE_DIR/api.lost"
        echo "[local_mcp] error: the API exited while the tunnel ran; stopping the tunnel (it would send" \
          "the credential to whatever listens on 127.0.0.1:$PORT next)" >&2
        signal_once tunnel
      fi
      return 0
    fi
    if [[ -n $tunnel_pid ]] && ! pid_alive "$tunnel_pid"; then return 0; fi
    if ((ticks % (healthy ? 10 : 2) != 0)); then continue; fi
    status=- state=- http=- failures=- category=-
    if words=$(control_plane); then
      read -r status state http failures category <<<"$words"
    fi
    # (failures: at most 9 digits and no leading zero, so that bash arithmetic reads it as decimal)
    if [[ $status != ok && $http =~ ^(401|403|404)$ && $failures =~ ^(0|[1-9][0-9]{0,8})$ ]]; then
      if [[ $http != "$run_code" || $category != "$run_category" ]] || ((failures < run_last)); then
        run_code=$http run_category=$category run_first=$failures
      fi
      run_last=$failures
      in_a_row=$((failures - run_first + 1))
      if ((in_a_row >= REJECT_AFTER)); then
        rejected_message "$http" "$in_a_row" >"$STATE_DIR/tunnel.rejected"
        echo "[local_mcp] error: OpenAI's control plane answers HTTP $http${OA_CODE:+ ($OA_CODE)};" \
          "stopping the tunnel (cause and fix below)" >&2
        signal_once tunnel
        return 0
      fi
    else
      run_code=""
    fi
    healthy=0
    if [[ $status == ok ]]; then
      if [[ $state == - ]]; then state=ok; fi # the sed fallback reads the status only
      if ((!connected)); then
        echo "[local_mcp] connected to OpenAI (control plane $state); dot can use the plugin now"
      elif [[ -n $warned ]]; then
        echo "[local_mcp] control plane ok again ($state)"
      fi
      connected=1 healthy=1 warned=""
    elif [[ $status != - && ($failures =~ ^[1-9] || $category != -) && ! $http =~ ^(401|403|404)$ ]]; then
      if [[ $warned != "$category $http" ]]; then
        warned="$category $http"
        echo "[local_mcp] warning: OpenAI's control plane is not reachable yet (status $status, state" \
          "$state, failure $category, HTTP $http, $failures failure(s) in a row); tunnel-client keeps" \
          "retrying. Details: scripts/local_mcp.sh status, logs" >&2
      fi
    fi
    if ((!connected && !hinted && ticks >= READY_TIMEOUT * 2)); then
      hinted=1
      echo "[local_mcp] the tunnel has not connected to OpenAI within ${READY_TIMEOUT}s (control plane: status" \
        "$status, state $state); dot cannot use the plugin yet. Check scripts/local_mcp.sh status and logs"
    fi
  done
}

start() {
  local proc stamp api_log tunnel_log probe_file doctor_file status other
  if other=$(other_tunnel); then
    fail "a tunnel started with another LOCAL_MCP_TUNNEL_DIR is running (pid $(read_pid tunnel): $other);" \
      "stop it first: LOCAL_MCP_TUNNEL_DIR=<that directory> scripts/local_mcp.sh stop"
  fi
  for proc in tunnel api; do
    if running "$proc"; then
      fail "the $proc is already running (pid $(read_pid "$proc")); use the window where it runs, or scripts/local_mcp.sh stop"
    fi
  done
  require_setup
  expiry_check
  if port_in_use "$PORT"; then
    fail "port $PORT on 127.0.0.1 is already in use; choose a free one with LOCAL_MCP_PORT=<port>" \
      "(start rewrites tunnel-client.yaml for it; the dashboard API uses 8000 by default)"
  fi
  remove_stale_socket # left by an earlier run (the tunnel is not running)
  ensure_config
  mkdir -p "$LOG_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  api_log=$LOG_DIR/api-$stamp.log
  tunnel_log=$LOG_DIR/tunnel-$stamp.log
  probe_file=$LOG_DIR/probe-$stamp.json
  doctor_file=$LOG_DIR/doctor-$stamp.txt

  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  echo "1/6 doctor (the API's configuration with this route's MCP environment; names only)"
  env "${API_ENV[@]}" uv run suv-deals doctor --process api || fail "doctor reported an error; nothing was started"

  echo "2/6 API on 127.0.0.1:$PORT (background; log $api_log)"
  forget api # a stale pid file must never name another process to the EXIT trap
  rm -f "$STATE_DIR/api.lost" "$STATE_DIR/tunnel.rejected"
  API_STARTED=1
  launch api "$api_log" env "${API_ENV[@]}" \
    uv run suv-deals api serve --host 127.0.0.1 --port "$PORT" --no-proxy-headers
  printf '%s\n' "$PORT" >"$STATE_DIR/api.port"

  echo "3/6 waiting for http://127.0.0.1:$PORT/readyz (up to ${READY_TIMEOUT}s)"
  local waited=0 code
  while :; do
    if ! alive api; then
      echo "error: the API exited; last lines of $api_log:" >&2
      tail -n 20 "$api_log" >&2 || true
      exit 1
    fi
    code=$(http_code "http://127.0.0.1:$PORT/readyz")
    if [[ $code == 200 ]]; then
      echo "  ready (HTTP 200)"
      break
    fi
    if ((waited >= READY_TIMEOUT * 2)); then
      echo "error: /readyz answered HTTP $code after ${READY_TIMEOUT}s (503: the database, the schema or the" \
        "configuration is not ready; the API log names the check); last lines of $api_log:" >&2
      tail -n 20 "$api_log" >&2 || true
      exit 1
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  # The credential goes only to the API this run launched: not to another program that won the
  # race for the port between the check above and the API's bind.
  if ! api_owns_port; then
    fail "127.0.0.1:$PORT is not served by the API this start launched (pid $(read_pid api)): another" \
      "program listens there; the credential was not sent and the tunnel was not started"
  fi
  echo "  127.0.0.1:$PORT is the API's own socket"

  echo "4/6 suv-deals mcp probe (401 without the header; initialize, tools/list, deals_health with it)"
  uv run suv-deals mcp probe --url "$MCP_URL" --authorization-file "$AUTH_FILE" --json \
    --timeout "$PROBE_TIMEOUT" >"$probe_file" 2>"$probe_file.err" && status=0 || status=$?
  if [[ -s $probe_file ]]; then probe_summary "$probe_file"; fi
  if ((status != 0)); then
    sed 's/^/  /' "$probe_file.err" >&2 || true
    fail "the probe failed (exit $status; report $probe_file); the tunnel was not started"
  fi
  rm -f -- "$probe_file.err"
  echo "  report: $probe_file"

  echo "5/6 tunnel-client doctor with the API up (output $doctor_file)"
  tunnel_doctor
  printf '%s\n' "$DOCTOR_OUT" >"$doctor_file"
  if ((DOCTOR_STATUS != 0)); then
    printf '%s\n' "$DOCTOR_OUT" | sed 's/^/  /' >&2
    fail "tunnel-client doctor failed with the API up (exit $DOCTOR_STATUS; failed checks:" \
      "${DOCTOR_FAILED:-none named}); the explanation is above; the tunnel was not started"
  fi
  printf '%s\n' "$DOCTOR_OUT" | sed -n -E 's/^CHECK (mcp_server_reachable|oauth_metadata|health_listener) +/  \1: /p'
  echo "  RESULT ok"

  echo "6/6 tunnel-client run (this window; its own output goes to $tunnel_log)"
  echo "======================================================================================"
  echo "Keep this window open while dot uses the system; Ctrl+C stops the tunnel and the API."
  echo "  tunnel $TUNNEL_ID -> $MCP_URL (local only; the API log: $api_log)"
  echo "  In ChatGPT, once this window says \"connected to OpenAI\": Add custom MCP server,"
  echo "  Connection: Tunnel, this tunnel, No authentication."
  echo "  Another window: scripts/local_mcp.sh status | logs | stop"
  echo "======================================================================================"
  release_lock
  printf '%s\n' "$tunnel_log" >"$(logfile_record tunnel)"
  forget tunnel # a stale pid file must never name another process to the watcher
  watch_tunnel &
  WATCHER_PID=$!
  # The tunnel runs in the foreground of this window: Ctrl+C reaches it directly; the traps only
  # note the signal (bash runs them once the tunnel has exited), then the EXIT trap stops the API.
  # Its own output (some 150 start-up lines, one warning per failed poll) goes to its log only;
  # this window shows the watcher's lines (`logs` follows both logs).
  trap ':' INT TERM HUP
  set +e
  (
    trap - INT TERM HUP
    printf '%s\n' "$BASHPID" >"$(pid_file tunnel)" # this subshell becomes tunnel-client (exec)
    scrub_tunnel_env
    # Health and admin endpoints on the socket in the 0700 directory: tunnel-client v0.0.16 then
    # binds no TCP port (flag help and a real run, docs/connect_mcp.md 8.2). If another build kept
    # one anyway, 127.0.0.1:0 gives it an ephemeral loopback port instead of a fixed 8080.
    exec "$TC" run --config "$CONFIG_FILE" --health.unix-socket "$HEALTH_SOCKET" \
      --health.listen-addr 127.0.0.1:0
  ) >>"$tunnel_log" 2>&1
  status=$?
  set -e
  forget tunnel
  if [[ -S $HEALTH_SOCKET ]]; then rm -f -- "$HEALTH_SOCKET"; fi
  if [[ -f $STATE_DIR/api.lost ]]; then
    rm -f "$STATE_DIR/api.lost"
    echo "error: the API exited while the tunnel ran; the tunnel was stopped (exit $status). Last lines" \
      "of $api_log:" >&2
    tail -n 20 "$api_log" >&2 || true
    exit 1
  fi
  if [[ -f $STATE_DIR/tunnel.rejected ]]; then
    show_rejection "  " "error: " ". The tunnel was stopped (exit $status; its log: $tunnel_log); stopping the API" >&2
    exit 1
  fi
  case $status in
    0 | 130 | 143)
      echo "tunnel: stopped (exit $status); stopping the API"
      ;;
    *)
      echo "error: the tunnel exited with status $status; last lines of $tunnel_log:" >&2
      tail -n 20 "$tunnel_log" >&2 || true
      echo "(stopping the API)" >&2
      exit 1
      ;;
  esac
}

# --------------------------------------------------------------------------------------------
# stop, status, logs, rotate
# --------------------------------------------------------------------------------------------

stop() {
  local proc pid other
  # A tunnel of another LOCAL_MCP_TUNNEL_DIR: nothing is stopped (the API it uses neither) and its
  # pid file is kept, so that the right `stop` still finds it.
  if other=$(other_tunnel); then
    fail "$(pid_file tunnel) names pid $(read_pid tunnel), a tunnel started with another" \
      "LOCAL_MCP_TUNNEL_DIR ($other). Nothing was stopped: run" \
      "LOCAL_MCP_TUNNEL_DIR=<that directory> scripts/local_mcp.sh stop"
  fi
  # The tunnel first (dot loses access at once), then the API; the start window stops the API
  # too once its tunnel has exited, and only one of the two sends the SIGTERM.
  for proc in tunnel api; do
    if running "$proc"; then
      signal_once "$proc"
      if ! wait_stopped "$proc"; then
        fail "the $proc (pid $(read_pid "$proc")) is still running after ${STOP_TIMEOUT}s;" \
          "scripts/local_mcp.sh stop waits again without a second signal"
      fi
      echo "$proc: stopped"
    else
      if pid=$(read_pid "$proc") && pid_alive "$pid"; then
        echo "$proc: not running (its pid file named pid $pid, which is another program now; not signalled, file removed)"
      else
        echo "$proc: not running"
      fi
      forget "$proc"
    fi
  done
}

show_log() {
  local log
  if log=$(current_log "$1") && [[ -f $log ]]; then
    echo "  log: $log"
    tail -n "$TAIL_LINES" "$log" | sed 's/^/    /'
  else
    echo "  log: none yet"
  fi
}

status() {
  local problem=0 port code epoch left file other words cp_status cp_state cp_http cp_failures cp_category
  port=$PORT
  if [[ -f $STATE_DIR/api.port && $(<"$STATE_DIR/api.port") =~ ^[0-9]+$ ]]; then port=$(<"$STATE_DIR/api.port"); fi
  if running api; then
    code=$(http_code "http://127.0.0.1:$port/readyz")
    echo "api: running (pid $(read_pid api)), http://127.0.0.1:$port/mcp; /readyz HTTP $code"
    if [[ $code != 200 ]]; then problem=1; fi
  else
    echo "api: not running"
    problem=1
  fi
  show_log api
  if running tunnel; then
    code=$(tunnel_ready_code)
    echo "tunnel: running (pid $(read_pid tunnel)); health socket /readyz HTTP $code"
    if [[ $code != 200 ]]; then problem=1; fi
    if words=$(control_plane); then
      read -r cp_status cp_state cp_http cp_failures cp_category <<<"$words"
      echo "  control plane: status $cp_status, state $cp_state, http_status $cp_http," \
        "consecutive_failures $cp_failures, failure_category $cp_category"
      if [[ $cp_status != ok ]]; then problem=1; fi
    else
      echo "  control plane: no answer on the health socket (GET /health?details=true)"
      problem=1
    fi
  elif other=$(other_tunnel); then
    echo "tunnel: one started with another LOCAL_MCP_TUNNEL_DIR runs (pid $(read_pid tunnel): $other)"
    problem=1
  else
    echo "tunnel: not running"
    if [[ -f $STATE_DIR/tunnel.rejected ]]; then
      show_rejection "    " "  the last start stopped it: " ""
    fi
    problem=1
  fi
  show_log tunnel
  if [[ -f $VERSION_FILE ]]; then
    echo "tunnel-client: $(head -n 1 "$VERSION_FILE") ($(sed -n '2p' "$VERSION_FILE"))"
  fi
  epoch=$(json_get expires_at_epoch)
  if [[ $epoch =~ ^[0-9]+$ ]]; then
    left=$((epoch - $(date +%s)))
    if ((left <= 0)); then
      echo "credential $(json_get credential_id): EXPIRED at $(json_get expires_at); run scripts/local_mcp.sh rotate"
      problem=1
    elif ((left < EXPIRY_WARN_DAYS * 86400)); then
      echo "credential $(json_get credential_id): expires in $((left / 86400)) day(s) ($(json_get expires_at)); rotate soon"
    else
      echo "credential $(json_get credential_id): expires in $((left / 86400)) day(s) ($(json_get expires_at))"
    fi
  else
    echo "credential: none recorded ($CREDENTIAL_FILE); run scripts/local_mcp.sh setup"
    problem=1
  fi
  echo "tunnel directory $TUNNEL_DIR (names and modes only):"
  for file in "$TUNNEL_ID_FILE" "$API_KEY_FILE" "$AUTH_FILE" "$CREDENTIAL_FILE" "$CONFIG_FILE"; do
    if [[ -e $file ]]; then
      echo "  ${file##*/}: present, mode $(stat -c '%a' -- "$file")"
    else
      echo "  ${file##*/}: MISSING"
      problem=1
    fi
  done
  return "$problem"
}

logs() {
  local proc log files=()
  for proc in api tunnel; do
    if log=$(current_log "$proc") && [[ -f $log ]]; then
      files+=("$log")
    fi
  done
  ((${#files[@]})) || fail "no log file yet; run scripts/local_mcp.sh start first"
  exec tail -n 20 -F "${files[@]}"
}

rotate() {
  umask 077
  local old_id old_workspace new stamp recorded=1
  check_tunnel_dir existing
  old_id=$(json_get credential_id)
  old_workspace=$(json_get workspace_id)
  [[ $old_id =~ $UUID_RE ]] || fail "no credential recorded in $CREDENTIAL_FILE; nothing to rotate (run scripts/local_mcp.sh setup)"
  check_private_file "$AUTH_FILE" "the MCP authorization file"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  new=$AUTH_FILE.new-$stamp
  echo "1/3 a new credential into $new"
  if ! create_credential "$new"; then
    fail "creating the new credential failed (output above); $AUTH_FILE and credential $old_id are unchanged"
  fi
  # credential.json is written before anything is replaced: if that fails, the new credential is
  # taken back and nothing changed (no credential left that no file records).
  if ! stage_credential_json; then
    echo "error: could not write the new $CREDENTIAL_FILE; taking the new credential $CRED_ID back" >&2
    if revoke_credential "$CRED_ID" "$CRED_WORKSPACE" "rotate could not record it"; then
      rm -f -- "$new"
      fail "the new credential was revoked; $AUTH_FILE and credential $old_id are unchanged"
    fi
    fail "revoking it failed too: $(revoke_command "$CRED_ID" "$CRED_WORKSPACE"), then delete $new;" \
      "$AUTH_FILE and credential $old_id are unchanged"
  fi
  echo "2/3 the new file replaces $AUTH_FILE (atomic rename)"
  if ! mv -f -- "$new" "$AUTH_FILE"; then
    rm -f -- "$STAGED"
    fail "could not rename $new to $AUTH_FILE: the new credential $CRED_ID is in $new and the old" \
      "one ($old_id) is still in use; fix the file and revoke the credential you do not keep"
  fi
  # Two renames in one directory; if the second one fails, the old credential is revoked all the
  # same (its token is no longer in use) and the staged record is kept with the command to finish.
  commit_credential_json || recorded=0
  echo "  credential $CRED_ID, expires $CRED_EXPIRES"
  echo "3/3 revoking the old credential $old_id"
  if ! revoke_credential "$old_id" "$old_workspace" "rotated by scripts/local_mcp.sh rotate"; then
    {
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      echo "error: REVOKING THE OLD CREDENTIAL FAILED. $old_id may still be valid until it expires."
      echo "  The new credential $CRED_ID is in place ($AUTH_FILE). Revoke the old one now:"
      echo "    uv run suv-deals credentials revoke $old_id${old_workspace:+ --workspace $old_workspace} --reason \"rotated\" --yes"
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    } >&2
    exit 1
  fi
  if ((!recorded)); then
    fail "the old credential $old_id was revoked, but $CREDENTIAL_FILE still names it (above): finish" \
      "the rename before the next setup or rotate"
  fi
  echo "rotated. The running tunnel (if any) still sends the old, now revoked header: restart it"
  echo "(Ctrl+C in its window or scripts/local_mcp.sh stop, then scripts/local_mcp.sh start)."
}

mkdir -p "$STATE_DIR"
case "$command" in
  setup | start | stop | rotate) take_lock ;;
esac
"$command"
