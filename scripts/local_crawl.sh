#!/usr/bin/env bash
# Run the enabled source (autoscout24_it) on the owner's WSL workstation (docs/runbook.md 9.2).
#
# Usage (any directory; the script works in the repository root):
#   scripts/local_crawl.sh start    crawler firewall (sudo), doctor, `sources sync --yes`, one
#                                   `reconcile` pass, then the scheduler, the worker and the
#                                   reconciler in the background (refused while any of them runs)
#   scripts/local_crawl.sh stop     SIGTERM to all three (once; a repeated stop only waits again);
#                                   the worker finishes its current job first
#   scripts/local_crawl.sh status   which of the three run, their log files and last lines, and the
#                                   source's runtime state (enabled, paused, state, requests_today)
#   scripts/local_crawl.sh logs     follow the three current logs (Ctrl+C stops following, not them)
#   scripts/local_crawl.sh --help
#
# The reconciler (`suv-deals reconcile --loop`, a pass every 5 minutes) returns a job whose lease
# ran out to the queue: after the PC slept or the database was unreachable for longer than the
# 5-minute lease, the worker gives the job up and, without it, the job would stay `running` and the
# source's later slots would all be skipped as `backlog`. It also closes orphaned crawl runs and
# runs the valuation and housekeeping sweeps.
#
# Secrets: DATABASE_URL and CRAWL4AI_API_TOKEN stay in .env in the repository root, which
# `suv-deals` reads itself. This script never reads, prints or passes them. The scheduler and the
# worker get SOURCE_NETWORK_ENABLED=true (the scheduler queues no real source's run without it);
# only the worker (and doctor) also get CRAWL4AI_BASE_URL (default http://127.0.0.1:11235). The
# reconciler gets neither, like the reconcile pass of `start`: it makes no source request and
# queues no stale-detail refresh or watch recheck of a real source.
#
# State (var/ is git-ignored): var/local_crawl/<process>.pid, var/local_crawl/<process>.logfile
# (the path of its current log), var/local_crawl/<process>.stopping (SIGTERM sent),
# var/local_crawl/lock (one `start` or `stop` at a time; a second one is refused) and
# var/local_crawl/logs/<process>-<UTC time>.log. The processes run in their own session (setsid +
# nohup): closing this terminal does not stop them, but closing every WSL window can stop WSL and
# them with it.
#
# Environment (optional): LOCAL_CRAWL_STOP_TIMEOUT seconds `stop` waits (default 600: a 12-page
# discovery run takes about 5 minutes); LOCAL_CRAWL_TAIL log lines `status` shows (default 5);
# LOCAL_CRAWL_SETTLE_SECONDS seconds `start` waits before checking all three still run (default 5).
#
# Exit codes: 0 ok; 1 a step failed, a process is not running (`status`), did not stop in time,
# or another `start`/`stop` holds the lock; 2 usage.
set -euo pipefail

# The comment block above (from line 2 up to the first line that is not a comment).
usage() { awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; }

SOURCE_KEY=autoscout24_it
PROCESSES=(scheduler worker reconciler)
STATE_DIR=var/local_crawl
LOG_DIR=$STATE_DIR/logs
CRAWLER_URL=${CRAWL4AI_BASE_URL:-http://127.0.0.1:11235}
STOP_TIMEOUT=${LOCAL_CRAWL_STOP_TIMEOUT:-600}
TAIL_LINES=${LOCAL_CRAWL_TAIL:-5}
SETTLE_SECONDS=${LOCAL_CRAWL_SETTLE_SECONDS:-5}

command=${1:-}
case "$command" in
  start | stop | status | logs) ;;
  -h | --help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (($# > 1)); then
  usage >&2
  exit 2
fi

cd "$(dirname "$0")/.."

pid_file() { printf '%s/%s.pid' "$STATE_DIR" "$1"; }
logfile_record() { printf '%s/%s.logfile' "$STATE_DIR" "$1"; }
# A second SIGTERM would end the worker at once, mid-job: `stop` sends one per process run.
stopping_marker() { printf '%s/%s.stopping' "$STATE_DIR" "$1"; }

read_pid() {
  local file pid
  file=$(pid_file "$1")
  [[ -f $file ]] || return 1
  pid=$(<"$file")
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$pid"
}

# The command words of PROCESS as `ps` shows them (`uv run <these>`).
command_words() {
  case "$1" in
    reconciler) printf 'suv-deals reconcile --loop' ;;
    *) printf 'suv-deals %s' "$1" ;;
  esac
}

# running PROCESS: its pid is alive AND runs `... suv-deals PROCESS` (not a reused pid).
running() {
  local pid args
  pid=$(read_pid "$1") || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  args=$(ps -p "$pid" -o args= 2>/dev/null) || return 1
  [[ " $args " == *" $(command_words "$1") "* ]]
}

current_log() {
  local record
  record=$(logfile_record "$1")
  [[ -f $record ]] || return 1
  cat "$record"
}

fail() {
  echo "error: $*" >&2
  exit 1
}

# One `start` or `stop` at a time (fd 9 on var/local_crawl/lock, released when this script exits):
# two overlapping starts (a second WSL window, or a re-run while the first waits at the sudo
# prompt) would otherwise both launch, and `stop` would then reach only the second set.
take_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock (util-linux) is missing"
  exec 9>"$STATE_DIR/lock"
  flock -n 9 || fail "another scripts/local_crawl.sh start or stop is running; let it finish, then try again"
}

# launch PROCESS LOGFILE COMMAND...: detached (own session, SIGHUP ignored), pid and log recorded.
# The process does not inherit the lock (9>&-): it would otherwise hold it as long as it runs.
launch() {
  local proc=$1 log=$2
  shift 2
  setsid nohup "$@" >"$log" 2>&1 </dev/null 9>&- &
  printf '%s\n' "$!" >"$(pid_file "$proc")"
  printf '%s\n' "$log" >"$(logfile_record "$proc")"
  rm -f "$(stopping_marker "$proc")"
}

start() {
  local proc
  for proc in "${PROCESSES[@]}"; do
    if running "$proc"; then
      fail "the $proc is already running (pid $(read_pid "$proc")); run scripts/local_crawl.sh stop first"
    fi
  done

  echo "1/5 crawler firewall (sudo: adds missing rules, verifies the crawler's isolation)"
  sudo scripts/crawler_firewall.sh || fail "the crawler firewall check failed; nothing was started"

  echo "2/5 doctor (worker configuration and the crawler's health and contract; names only)"
  env SOURCE_NETWORK_ENABLED=true CRAWL4AI_BASE_URL="$CRAWLER_URL" \
    uv run suv-deals doctor --process worker --crawler ||
    fail "doctor reported an error; nothing was started"

  echo "3/5 sources sync (the committed configuration into the runtime registry)"
  uv run suv-deals sources sync --yes || fail "sources sync failed; nothing was started"

  echo "4/5 one reconcile pass (returns jobs whose lease ended while nothing ran; no network)"
  uv run suv-deals reconcile || fail "the reconcile pass failed; nothing was started"

  echo "5/5 scheduler, worker and reconciler (background)"
  mkdir -p "$LOG_DIR"
  local stamp
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  launch scheduler "$LOG_DIR/scheduler-$stamp.log" \
    env SOURCE_NETWORK_ENABLED=true uv run suv-deals scheduler
  launch worker "$LOG_DIR/worker-$stamp.log" \
    env SOURCE_NETWORK_ENABLED=true CRAWL4AI_BASE_URL="$CRAWLER_URL" uv run suv-deals worker
  launch reconciler "$LOG_DIR/reconciler-$stamp.log" uv run suv-deals reconcile --loop
  sleep "$SETTLE_SECONDS"
  local failed=0
  for proc in "${PROCESSES[@]}"; do
    if running "$proc"; then
      echo "  $proc: running (pid $(read_pid "$proc")), log $(current_log "$proc")"
    else
      echo "  $proc: NOT running; last lines of $(current_log "$proc"):" >&2
      tail -n 20 "$(current_log "$proc")" >&2 || true
      failed=1
    fi
  done
  if ((failed)); then
    echo "error: a process exited at once; run scripts/local_crawl.sh stop, fix the cause, start again" >&2
    exit 1
  fi
  echo "started. Keep a WSL window open. Check: scripts/local_crawl.sh status; stop: scripts/local_crawl.sh stop"
}

stop() {
  local proc pid signalled=()
  # The scheduler first, so it queues nothing new while the worker finishes its job; the
  # reconciler ends after its current pass.
  for proc in "${PROCESSES[@]}"; do
    if running "$proc"; then
      pid=$(read_pid "$proc")
      if [[ -f $(stopping_marker "$proc") && $(<"$(stopping_marker "$proc")") == "$pid" ]]; then
        echo "$proc (pid $pid): SIGTERM already sent, waiting"
      else
        kill -TERM "$pid"
        printf '%s\n' "$pid" >"$(stopping_marker "$proc")"
        echo "$proc (pid $pid): SIGTERM sent"
      fi
      signalled+=("$proc")
    else
      echo "$proc: not running"
      rm -f "$(pid_file "$proc")" "$(stopping_marker "$proc")"
    fi
  done
  local waited=0 left=()
  while :; do
    left=()
    for proc in "${signalled[@]}"; do
      running "$proc" && left+=("$proc")
    done
    ((${#left[@]} == 0)) && break
    if ((waited >= STOP_TIMEOUT)); then
      echo "error: still running after ${STOP_TIMEOUT}s: ${left[*]} (the worker finishes its current job;" \
        "scripts/local_crawl.sh stop waits again without a second signal; see scripts/local_crawl.sh status)" >&2
      exit 1
    fi
    if ((waited > 0 && waited % 30 == 0)); then
      echo "  waiting for: ${left[*]} (${waited}s; the worker finishes its current job)"
    fi
    sleep 1
    waited=$((waited + 1))
  done
  for proc in "${signalled[@]}"; do
    rm -f "$(pid_file "$proc")" "$(stopping_marker "$proc")"
    echo "$proc: stopped"
  done
}

source_state() {
  local out errors
  errors="$STATE_DIR/inspect.err"
  if ! out=$(uv run --quiet suv-deals sources inspect "$SOURCE_KEY" --from-db 2>"$errors"); then
    echo "  sources inspect failed:"
    tail -n 3 "$errors" | sed 's/^/    /'
    return 1
  fi
  if command -v jq >/dev/null 2>&1; then
    printf '%s\n' "$out" | jq -r \
      '"  enabled=\(.enabled) paused=\(.paused) state=\(.state) requests_today=\(.rate_budget.requests_today)"'
  else
    printf '%s\n' "$out" |
      grep -E '^[[:space:]]*"(enabled|paused|state|requests_today)":' |
      sed -E 's/^[[:space:]]*"([a-z_]+)":[[:space:]]*"?([^",]*)"?,?[[:space:]]*$/\1=\2/' |
      paste -sd' ' - | sed 's/^/  /'
  fi
}

status() {
  local proc log problem=0
  for proc in "${PROCESSES[@]}"; do
    if running "$proc"; then
      echo "$proc: running (pid $(read_pid "$proc"))"
    else
      echo "$proc: not running"
      problem=1
    fi
    if log=$(current_log "$proc") && [[ -f $log ]]; then
      echo "  log: $log"
      tail -n "$TAIL_LINES" "$log" | sed 's/^/    /'
    else
      echo "  log: none yet"
    fi
  done
  echo "source $SOURCE_KEY (runtime registry):"
  source_state || problem=1
  return "$problem"
}

logs() {
  local proc log files=()
  for proc in "${PROCESSES[@]}"; do
    if log=$(current_log "$proc") && [[ -f $log ]]; then
      files+=("$log")
    fi
  done
  ((${#files[@]})) || fail "no log file yet; run scripts/local_crawl.sh start first"
  exec tail -n 20 -F "${files[@]}"
}

mkdir -p "$STATE_DIR"
case "$command" in
  start | stop) take_lock ;;
esac
"$command"
