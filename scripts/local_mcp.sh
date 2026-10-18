#!/usr/bin/env bash
# Connect dot (ChatGPT) to this system's MCP server, read-only, through the OpenAI Secure MCP
# Tunnel on the owner's WSL workstation (docs/connect_mcp.md section 8, docs/runbook.md 9.3).
#
# Usage (any directory; the script works in the repository root):
#   scripts/local_mcp.sh setup    once; idempotent, creates only what is missing: checks
#                                 tunnel-client, asks for the tunnel id and the OpenAI runtime API
#                                 key (hidden input), creates the viewer MCP credential file, writes
#                                 tunnel-client.yaml and runs `tunnel-client doctor --explain`
#   scripts/local_mcp.sh start    doctor, the API on 127.0.0.1:LOCAL_MCP_PORT (background), its
#                                 /readyz, `suv-deals mcp probe`, then `tunnel-client run` in THIS
#                                 window (foreground); Ctrl+C stops the tunnel and the API
#   scripts/local_mcp.sh stop     from another window: SIGTERM to the tunnel, then to the API
#   scripts/local_mcp.sh status   which of the two run, the API's /readyz, the tunnel's /readyz (its
#                                 health socket), the credential's expiry, log files and last lines
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
# the repository and any git working tree around it, not a symlink, not under /mnt):
# control-plane-api-key (0600), tunnel-id, mcp-authorization (0600, exactly "Bearer <token>"),
# credential.json (credential id, workspace, expiry; no secret), tunnel-client.yaml (0600),
# tunnel-client.version and, while the tunnel runs, health.sock (its /healthz and /readyz).
#
# State (var/ is git-ignored): var/local_mcp/<process>.pid, <process>.logfile (its current log),
# <process>.stopping (SIGTERM sent), api.port, var/local_mcp/lock (one setup, start, stop or
# rotate at a time; `start` releases it once the tunnel runs) and
# var/local_mcp/logs/<process>-<UTC time>.log (process: api, tunnel), probe-<UTC time>.json.
# Separate from scripts/local_crawl.sh (var/local_crawl, its own lock).
#
# Environment (optional): LOCAL_MCP_PORT API port (default 8010); LOCAL_MCP_TUNNEL_DIR;
# TUNNEL_CLIENT_BIN the tunnel-client binary (default: from PATH); LOCAL_MCP_WORKSPACE workspace id
# for the credential (default: the only active workspace); LOCAL_MCP_READY_TIMEOUT seconds `start`
# waits for /readyz (default 60); LOCAL_MCP_STOP_TIMEOUT seconds a stop waits per process (default
# 30); LOCAL_MCP_PROBE_TIMEOUT seconds per probe request (default 10); LOCAL_MCP_TAIL log lines
# `status` shows (default 5).
#
# Exit codes: 0 ok; 1 a step failed, setup is incomplete, the credential expired, a process is not
# running or not ready (`status`), did not stop in time, or another command holds the lock;
# 2 usage.
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

# running PROCESS: its pid is alive AND runs that command (not a reused pid).
running() {
  local pid args
  pid=$(read_pid "$1") || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  args=$(ps -p "$pid" -o args= 2>/dev/null) || return 1
  [[ " $args " == *" $(command_words "$1") "* ]]
}

# alive PROCESS: its recorded pid exists and is no zombie. For the API this run launched: right
# after the launch `ps` may still show setsid/nohup/env instead of the final command.
alive() {
  local pid state
  pid=$(read_pid "$1") || return 1
  state=$(ps -p "$pid" -o stat= 2>/dev/null) || return 1
  state=${state//[[:space:]]/}
  [[ -n $state && $state != Z* ]]
}

current_log() {
  local record
  record=$(logfile_record "$1")
  [[ -f $record ]] || return 1
  cat "$record"
}

forget() {
  rm -f "$(pid_file "$1")" "$(stopping_marker "$1")"
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

# check_tunnel_dir create|existing
check_tunnel_dir() {
  local real root uid mode
  real=$(realpath -m -- "$TUNNEL_DIR")
  while IFS= read -r root; do
    if [[ $real == "$root" || $real == "${root%/}"/* ]]; then
      fail "the tunnel directory $TUNNEL_DIR is inside the repository or a git working tree ($root);" \
        "use a directory outside it, e.g. the default ~/.config/suv-deals/tunnel (LOCAL_MCP_TUNNEL_DIR)"
    fi
  done < <(protected_roots)
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

# write_credential_json ID WORKSPACE EXPIRES_AT EPOCH SOURCE
write_credential_json() {
  {
    printf '{\n'
    printf '  "credential_id": "%s",\n' "$1"
    printf '  "workspace_id": "%s",\n' "$2"
    printf '  "label": "%s",\n' "$CREDENTIAL_LABEL"
    printf '  "role": "%s",\n' "$CREDENTIAL_ROLE"
    printf '  "scopes": ["deals:read", "reviews:read"],\n'
    printf '  "expires_at": "%s",\n' "$3"
    printf '  "expires_at_epoch": %s,\n' "$4"
    printf '  "expires_source": "%s",\n' "$5"
    printf '  "created_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "authorization_file": "%s"\n' "$AUTH_FILE"
    printf '}\n'
  } | replace_file "$CREDENTIAL_FILE"
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
  local version tunnel_id_input api_key
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
  echo "  $TC: $version"

  echo "2/6 tunnel directory $TUNNEL_DIR"
  check_tunnel_dir create
  printf '%s\n' "$version" | replace_file "$VERSION_FILE"

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
    if ! create_credential "$AUTH_FILE"; then
      fail "credentials create-mcp failed (output above); nothing else was changed"
    fi
    write_credential_json "$CRED_ID" "$CRED_WORKSPACE" "$CRED_EXPIRES" "$CRED_EPOCH" "$CRED_SOURCE"
    echo "  created credential $CRED_ID (expires $CRED_EXPIRES; $CRED_SOURCE); token in $AUTH_FILE, not shown"
  fi

  echo "6/6 tunnel-client.yaml and \`tunnel-client doctor --explain\`"
  ensure_config
  if ! tunnel_client doctor --config "$CONFIG_FILE" --explain; then
    echo "error: tunnel-client doctor reported a problem (above). If it names only the MCP server at" \
      "$MCP_URL as unreachable, that is expected before scripts/local_mcp.sh start runs the API." >&2
    exit 1
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

# launch PROCESS LOGFILE COMMAND...: detached (own session, SIGHUP ignored), pid and log recorded.
launch() {
  local proc=$1 log=$2
  shift 2
  setsid nohup "$@" >"$log" 2>&1 </dev/null 9>&- &
  printf '%s\n' "$!" >"$(pid_file "$proc")"
  printf '%s\n' "$log" >"$(logfile_record "$proc")"
  rm -f "$(stopping_marker "$proc")"
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

watch_tunnel_ready() {
  local waited=0
  while ((waited < READY_TIMEOUT)); do
    sleep 1
    waited=$((waited + 1))
    if [[ $(tunnel_ready_code) == 200 ]]; then
      echo "[local_mcp] tunnel ready (health socket /readyz: HTTP 200 after ${waited}s); dot can use the plugin now"
      return 0
    fi
  done
  echo "[local_mcp] the tunnel did not report ready within ${READY_TIMEOUT}s; check the lines above or scripts/local_mcp.sh status"
}

start() {
  local proc stamp api_log tunnel_log probe_file status
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
  if [[ -e $HEALTH_SOCKET || -L $HEALTH_SOCKET ]]; then
    if [[ -S $HEALTH_SOCKET && ! -L $HEALTH_SOCKET ]]; then
      rm -f -- "$HEALTH_SOCKET" # left by an earlier run (the tunnel is not running)
    else
      fail "$HEALTH_SOCKET exists and is not a socket; remove it"
    fi
  fi
  ensure_config
  mkdir -p "$LOG_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  api_log=$LOG_DIR/api-$stamp.log
  tunnel_log=$LOG_DIR/tunnel-$stamp.log
  probe_file=$LOG_DIR/probe-$stamp.json

  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  echo "1/5 doctor (the API's configuration with this route's MCP environment; names only)"
  env "${API_ENV[@]}" uv run suv-deals doctor --process api || fail "doctor reported an error; nothing was started"

  echo "2/5 API on 127.0.0.1:$PORT (background; log $api_log)"
  forget api # a stale pid file must never name another process to the EXIT trap
  API_STARTED=1
  launch api "$api_log" env "${API_ENV[@]}" \
    uv run suv-deals api serve --host 127.0.0.1 --port "$PORT" --no-proxy-headers
  printf '%s\n' "$PORT" >"$STATE_DIR/api.port"

  echo "3/5 waiting for http://127.0.0.1:$PORT/readyz (up to ${READY_TIMEOUT}s)"
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

  echo "4/5 suv-deals mcp probe (401 without the header; initialize, tools/list, deals_health with it)"
  uv run suv-deals mcp probe --url "$MCP_URL" --authorization-file "$AUTH_FILE" --json \
    --timeout "$PROBE_TIMEOUT" >"$probe_file" 2>"$probe_file.err" && status=0 || status=$?
  if [[ -s $probe_file ]]; then probe_summary "$probe_file"; fi
  if ((status != 0)); then
    sed 's/^/  /' "$probe_file.err" >&2 || true
    fail "the probe failed (exit $status; report $probe_file); the tunnel was not started"
  fi
  rm -f -- "$probe_file.err"
  echo "  report: $probe_file"

  echo "5/5 tunnel-client run (this window; log $tunnel_log)"
  echo "======================================================================================"
  echo "Keep this window open while dot uses the system; Ctrl+C stops the tunnel and the API."
  echo "  tunnel $TUNNEL_ID -> $MCP_URL (local only; the API log: $api_log)"
  echo "  In ChatGPT: Add custom MCP server, Connection: Tunnel, this tunnel, No authentication."
  echo "  Another window: scripts/local_mcp.sh status | logs | stop"
  echo "======================================================================================"
  release_lock
  printf '%s\n' "$tunnel_log" >"$(logfile_record tunnel)"
  rm -f "$(stopping_marker tunnel)"
  watch_tunnel_ready &
  WATCHER_PID=$!
  # The tunnel runs in the foreground of this window: Ctrl+C reaches it directly; the traps only
  # note the signal (bash runs them once the tunnel has exited), then the EXIT trap stops the API.
  trap ':' INT TERM HUP
  set +e
  (
    trap - INT TERM HUP
    printf '%s\n' "$BASHPID" >"$(pid_file tunnel)" # this subshell becomes tunnel-client (exec)
    scrub_tunnel_env
    exec "$TC" run --config "$CONFIG_FILE" --health.unix-socket "$HEALTH_SOCKET"
  ) 2>&1 | tee -i -a "$tunnel_log"
  status=${PIPESTATUS[0]}
  set -e
  forget tunnel
  if [[ -S $HEALTH_SOCKET ]]; then rm -f -- "$HEALTH_SOCKET"; fi
  case $status in
    0 | 130 | 143)
      echo "tunnel: stopped (exit $status); stopping the API"
      ;;
    *)
      echo "error: the tunnel exited with status $status (log $tunnel_log); stopping the API" >&2
      exit 1
      ;;
  esac
}

# --------------------------------------------------------------------------------------------
# stop, status, logs, rotate
# --------------------------------------------------------------------------------------------

stop() {
  local proc
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
      echo "$proc: not running"
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
  local problem=0 port code epoch left file
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
  else
    echo "tunnel: not running"
    problem=1
  fi
  show_log tunnel
  if [[ -f $VERSION_FILE ]]; then echo "tunnel-client: $(head -n 1 "$VERSION_FILE")"; fi
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
  local old_id old_workspace new stamp args
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
  echo "2/3 the new file replaces $AUTH_FILE (atomic rename)"
  if ! mv -f -- "$new" "$AUTH_FILE"; then
    fail "could not rename $new to $AUTH_FILE: the new credential $CRED_ID is in $new and the old" \
      "one ($old_id) is still in use; fix the file and revoke the credential you do not keep"
  fi
  write_credential_json "$CRED_ID" "$CRED_WORKSPACE" "$CRED_EXPIRES" "$CRED_EPOCH" "$CRED_SOURCE"
  echo "  credential $CRED_ID, expires $CRED_EXPIRES"
  echo "3/3 revoking the old credential $old_id"
  args=(credentials revoke "$old_id" --reason "rotated by scripts/local_mcp.sh rotate")
  if [[ $old_workspace =~ $UUID_RE ]]; then args+=(--workspace "$old_workspace"); fi
  args+=(--yes)
  if ! uv run suv-deals "${args[@]}"; then
    {
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      echo "error: REVOKING THE OLD CREDENTIAL FAILED. $old_id may still be valid until it expires."
      echo "  The new credential $CRED_ID is in place ($AUTH_FILE). Revoke the old one now:"
      echo "    uv run suv-deals credentials revoke $old_id${old_workspace:+ --workspace $old_workspace} --reason \"rotated\" --yes"
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    } >&2
    exit 1
  fi
  echo "rotated. The running tunnel (if any) still sends the old, now revoked header: restart it"
  echo "(Ctrl+C in its window or scripts/local_mcp.sh stop, then scripts/local_mcp.sh start)."
}

mkdir -p "$STATE_DIR"
case "$command" in
  setup | start | stop | rotate) take_lock ;;
esac
"$command"
