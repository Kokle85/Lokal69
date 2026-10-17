#!/usr/bin/env bash
# Owner-run readiness check: everything the build needs to know before the system runs for real.
#
# Usage (from the repository root, in WSL Ubuntu):
#   scripts/readiness_check.sh              all steps (asks for the sudo password once, for the
#                                           crawler firewall check)
#   scripts/readiness_check.sh --no-sudo    skip the firewall check
#   scripts/readiness_check.sh --no-probe   skip the source probes (no request to any marketplace)
#   scripts/readiness_check.sh --with-tests also run the fast test suite (about 2 minutes)
#   scripts/readiness_check.sh --help
#
# Steps: tools and repository state; dependencies; `suv-deals doctor` (configuration NAMES only,
# never values); the Crawl4AI container and its health; the crawler firewall rules (--check, adds
# nothing); and source probes. Each probe is one owner-run `capture-samples` for ONE source: robots.txt
# first, one reviewed page (a home page or one search page), at most one detail page, the first block
# or challenge stops it. Every source is tried twice: directly (plain HTTPS) and through the Crawl4AI
# browser (headless, honest User-Agent, no stealth), 25 seconds apart. Nothing is enabled, written to
# the database or published.
#
# Output: captures/readiness/<UTC>/report.txt (git-ignored). Send its CONTENT; it holds statuses and
# counts only. Saved pages stay on this machine. Database URLs, bearer tokens and keys are masked even
# though no step prints them.
set -uo pipefail

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

use_sudo=1
probe=1
tests=0
for arg in "$@"; do
  case "$arg" in
    --no-sudo) use_sudo=0 ;;
    --no-probe) probe=0 ;;
    --with-tests) tests=1 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="captures/readiness/$stamp"
mkdir -p "$out"
report="$out/report.txt"
crawler_url="http://127.0.0.1:11235"
probe_gap=25
declare -a summary=()

# Mask anything secret-shaped, should a tool ever print one.
scrub() {
  sed -E \
    -e 's#(postgres(ql)?://[^:/@[:space:]]+):[^@[:space:]]+@#\1:***@#g' \
    -e 's#eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}#<jwt masked>#g' \
    -e 's#(Bearer )[A-Za-z0-9._~+/=-]{8,}#\1***#g' \
    -e 's#\b(sk|suvmail|suvmcp|xox[abpr])[-_][A-Za-z0-9_-]{8,}#<secret masked>#g' \
    -e 's#([A-Z0-9_]*(TOKEN|SECRET|PASSWORD|KEY)[A-Z0-9_]*=)[^[:space:]]+#\1***#g'
}

section() { printf '\n==== %s\n' "$1" | tee -a "$report"; }

# step NAME CMD...: run, log scrubbed output, record the result for the summary.
step() {
  local name=$1
  shift
  printf '$ %s\n' "$*" | scrub >>"$report"
  "$@" 2>&1 | scrub | tee -a "$report"
  local rc=${PIPESTATUS[0]}
  summary+=("$(printf '%-44s %s' "$name" "$([[ $rc -eq 0 ]] && echo ok || echo "exit $rc")")")
  return 0
}

note() { printf '%s\n' "$*" | tee -a "$report"; }

{
  echo "readiness check $stamp (UTC)"
  echo "Send this file's content. It holds statuses and counts only; no secret values."
} >"$report"

section "1. Tools and repository"
note "kernel: $(uname -sr)"
if grep -qi microsoft /proc/version 2>/dev/null; then note "WSL: yes"; else note "WSL: no"; fi
note "branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null) commit: $(git rev-parse --short HEAD 2>/dev/null)"
note "uncommitted files: $(git status --porcelain 2>/dev/null | wc -l)"
if git fetch -q origin "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null; then
  note "behind origin: $(git rev-list --count HEAD..FETCH_HEAD 2>/dev/null) commit(s)"
else
  note "behind origin: unknown (git fetch failed)"
fi
for tool in python3 uv docker; do
  if command -v "$tool" >/dev/null 2>&1; then
    note "$tool: $("$tool" --version 2>&1 | head -1)"
  else
    note "$tool: MISSING"
  fi
done
if docker compose version >/dev/null 2>&1; then note "docker compose: $(docker compose version --short 2>&1)"; else note "docker compose: MISSING"; fi
note ".env present: $([[ -f .env ]] && echo yes || echo no)"

section "2. Dependencies"
step "uv sync (locked dependencies)" uv sync --frozen --quiet

section "3. Configuration and dependency health (doctor; names only, never values)"
step "doctor --crawler -v" env CRAWL4AI_BASE_URL="$crawler_url" uv run --quiet suv-deals doctor --crawler -v

section "4. Crawl4AI container"
if command -v docker >/dev/null 2>&1; then
  step "docker compose ps crawl4ai" docker compose ps crawl4ai
fi
step "crawler /health (no token sent)" curl -sS -m 10 "$crawler_url/health"
echo >>"$report"

section "5. Crawler firewall (runbook 3.2; --check adds nothing)"
if [[ $use_sudo -eq 1 ]]; then
  step "crawler_firewall.sh --check" sudo scripts/crawler_firewall.sh --check
else
  note "skipped (--no-sudo)"
  summary+=("$(printf '%-44s %s' "crawler_firewall.sh --check" "skipped")")
fi

# probe SOURCE PATH QUERY DETAILS: once directly, once through the crawler, $probe_gap s apart.
probe_source() {
  local source=$1 path=$2 query=$3 details=$4
  local -a extra=()
  [[ -n $query ]] && extra=(--query "$query")
  note "-- $source $path${query:+?$query} (detail pages: $details)"
  step "probe $source direct" env SOURCE_NETWORK_ENABLED=true uv run --quiet suv-deals capture-samples \
    --source "$source" --search-path "$path" "${extra[@]}" --detail-limit "$details" --yes
  sleep "$probe_gap"
  step "probe $source via crawl4ai" env SOURCE_NETWORK_ENABLED=true CRAWL4AI_BASE_URL="$crawler_url" \
    uv run --quiet suv-deals capture-samples --source "$source" --search-path "$path" "${extra[@]}" \
    --detail-limit "$details" --via crawl4ai --yes
}

section "6. Source probes (robots.txt + one reviewed page each; the first block stops a probe)"
if [[ $probe -eq 1 ]]; then
  # MK resale prices: can our own crawler read them, or only dot?
  probe_source reklama5_mk / "" 0
  probe_source pazar3_mk / "" 0
  # Purchase sources: the Italian route the worker will use, the German search page (needs a
  # browser?), and the Swiss sites the cloud build environment could not open.
  probe_source autoscout24_it /lst/renault/captur "body=4&kmto=200000" 1
  probe_source autoscout24_de /lst/renault/captur "" 0
  probe_source autoscout24_ch / "" 0
  probe_source carforyou_ch / "" 0
  probe_source tutti_ch / "" 0
else
  note "skipped (--no-probe)"
fi

if [[ $tests -eq 1 ]]; then
  section "7. Fast tests (no database, no network)"
  step "pytest -m 'not db and not live'" uv run --quiet pytest -q -m "not db and not live" -p no:cacheprovider
fi

section "Summary"
for line in "${summary[@]}"; do note "$line"; done
note ""
note "Report: $(pwd)/$report"
note "Send the content of that file. The saved pages under captures/ stay on this machine."
