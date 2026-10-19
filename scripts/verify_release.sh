#!/usr/bin/env bash
# =============================================================================
# scripts/verify_release.sh -- verify the EXACT release build and write a release report
# (spec sections 29, 31 "Tests must run on the exact final commit/build", 34)
# =============================================================================
# Steps (each recorded as passed / FAILED / NOT RUN in var/releases/<sha>_<ts>.txt):
#   1. commit SHA, dirty-tree flag, uv.lock / dashboard lock hashes, uv, Python, node and npm
#      versions, the YAML business-configuration hash, every source's adapter version/mode/enabled
#      flag and the MCP SDK / protocol revision (spec 29 release record; the configuration revision
#      STORED in a database is per environment: record it from `suv-deals config apply --dry-run`);
#   2. lint: ruff check + ruff format --check over src, tests, scripts AND desktop/outlook-bridge;
#   3. typecheck: mypy --strict (suv_deals), the E2E harness (tests/e2e) and the desktop worker
#      (-p outlook_bridge);
#   4. JSON schema snapshot check (scripts/export_schemas.py --check);
#   5. migration list with per-file sha256 (supabase/migrations);
#   6. tests without a database, then database tests on PostgreSQL 16 and 17
#      (TEST_DATABASE_ADMIN_URL / PG17_ADMIN_URL; --skip-db records them as NOT RUN);
#   7. the desktop worker's tests (desktop/outlook-bridge/tests; fakes only, no Outlook);
#   8. dashboard: npm ci, build, vitest, lint (+ static security check), npm audit (production
#      dependencies); --skip-dashboard records them (and the browser E2E) as NOT RUN. The build
#      is the deployable bundle: export the target's VITE_SUPABASE_URL and
#      VITE_SUPABASE_PUBLISHABLE_KEY first (the production build guard refuses without them);
#   9. browser E2E (`make e2e`: harness self-test + Playwright) on PostgreSQL 16 and 17, only with
#      --with-e2e; otherwise NOT RUN, so the result is never "verified" without it;
#  10. optional: the image digest you deploy (RELEASE_IMAGE_DIGEST) and the crawler digest
#      (CRAWL4AI_IMAGE_DIGEST) are recorded, never invented.
#
# A dirty working tree is reported and makes the result "not releasable": release from a commit.
# RELEASE_REPORT_DIR overrides the report directory (default var/releases).
#
# Usage: scripts/verify_release.sh [--skip-db] [--skip-dashboard] [--with-e2e] [--plan]
#   --plan            print the steps without running anything
#   --skip-db         do not run the database tests (recorded as NOT RUN; not verified then)
#   --skip-dashboard  do not run the dashboard steps or the browser E2E (NOT RUN; not verified)
#   --with-e2e        run the browser E2E on PostgreSQL 16 and 17 (needs the local clusters and the
#                     preinstalled Playwright chromium; without it the E2E is NOT RUN)
# Exit codes: 0 every executed step passed and nothing was skipped, 1 otherwise, 2 usage.
# =============================================================================
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
skip_db=0
skip_dashboard=0
with_e2e=0
plan=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-db) skip_db=1 ;;
    --skip-dashboard) skip_dashboard=1 ;;
    --with-e2e) with_e2e=1 ;;
    --plan) plan=1 ;;
    -h | --help) sed -n '2,/^# Exit codes/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify_release.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

PG16_ADMIN_URL="${TEST_DATABASE_ADMIN_URL:-postgresql://suv:suv@127.0.0.1:5432/postgres}"
PG17_ADMIN_URL="${PG17_ADMIN_URL:-postgresql://suv:suv@127.0.0.1:5433/postgres}"
PG17_MIGRATOR_ROLE="${PG17_MIGRATOR_ROLE:-suv_migrator}"

steps=(
  "lint|uv run --frozen ruff check src tests scripts desktop/outlook-bridge && uv run --frozen ruff format --check src tests scripts desktop/outlook-bridge"
  "typecheck|uv run --frozen mypy && uv run --frozen mypy --strict tests/e2e && MYPYPATH=desktop/outlook-bridge uv run --frozen mypy --strict -p outlook_bridge"
  "schemas|uv run --frozen python scripts/export_schemas.py --check"
  "tests_without_db|uv run --frozen pytest -q -m 'not db and not live and not e2e'"
  "tests_db_pg16|TEST_DATABASE_ADMIN_URL='$PG16_ADMIN_URL' uv run --frozen pytest -q -m 'db and not live and not e2e'"
  "tests_db_pg17|TEST_DATABASE_ADMIN_URL='$PG17_ADMIN_URL' TEST_DATABASE_MIGRATOR_ROLE='$PG17_MIGRATOR_ROLE' uv run --frozen pytest -q -m 'db and not live and not e2e'"
  "desktop_tests|uv run --frozen pytest -q desktop/outlook-bridge/tests"
  "dashboard_ci|npm --prefix dashboard ci"
  "dashboard_build|npm --prefix dashboard run build"
  "dashboard_test|npm --prefix dashboard test"
  "dashboard_lint|npm --prefix dashboard run lint"
  "dashboard_audit|npm --prefix dashboard audit --omit=dev"
  "e2e_pg16|make e2e PG16_ADMIN_URL='$PG16_ADMIN_URL'"
  "e2e_pg17|make e2e PG16_ADMIN_URL='$PG17_ADMIN_URL'"
)

# Why a step is not run ("" = run it).
skip_reason() {
  case "$1" in
    tests_db_*) [ "$skip_db" -eq 1 ] && echo "--skip-db" ;;
    dashboard_*) [ "$skip_dashboard" -eq 1 ] && echo "--skip-dashboard" ;;
    e2e_*)
      if [ "$skip_dashboard" -eq 1 ]; then echo "--skip-dashboard"
      elif [ "$with_e2e" -eq 0 ]; then echo "pass --with-e2e"
      fi ;;
  esac
  return 0
}

if [ "$plan" -eq 1 ]; then
  echo "verify_release.sh plan (nothing is run):"
  echo "  record: commit SHA, dirty flag, lock hashes, tool versions (uv, Python, node, npm),"
  echo "          configuration hash, source adapter versions, MCP SDK/protocol, migration hashes"
  for step in "${steps[@]}"; do
    name="${step%%|*}"
    reason="$(skip_reason "$name")"
    if [ -n "$reason" ]; then
      echo "  $name: NOT RUN ($reason)"
    else
      echo "  $name"
    fi
  done
  exit 0
fi

sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
dirty="no"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then dirty="yes"; fi
ts="$(date -u +%Y%m%dT%H%M%SZ)"
report_dir="${RELEASE_REPORT_DIR:-var/releases}"
mkdir -p "$report_dir"
report="$report_dir/${sha:0:12}_$ts.txt"
results=()
failed=0
skipped=0

{
  echo "suv-deals release verification"
  echo "commit=$sha"
  echo "dirty_working_tree=$dirty"
  echo "verified_at=$ts"
  echo "uv_lock_sha256=$(sha256sum uv.lock | cut -d' ' -f1)"
  [ -f dashboard/package-lock.json ] && echo "dashboard_lock_sha256=$(sha256sum dashboard/package-lock.json | cut -d' ' -f1)"
  echo "uv_version=$(uv --version 2>/dev/null || echo missing)"
  echo "node_version=$(node --version 2>/dev/null || echo missing)"
  echo "npm_version=$(npm --version 2>/dev/null || echo missing)"
  echo "python_version=$(uv run --frozen python -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
  echo "app_version=$(uv run --frozen python -c 'import suv_deals; print(suv_deals.__version__)' 2>/dev/null || echo unknown)"
  echo "release_image_digest=${RELEASE_IMAGE_DIGEST:-not recorded}"
  echo "crawl4ai_image_digest=${CRAWL4AI_IMAGE_DIGEST:-not recorded}"
  echo "release_facts:"
  uv run --frozen python - <<'EOF' 2>/dev/null || echo "  (could not be computed)"
import importlib.metadata as md

import mcp_types

from suv_deals.adapters.registry import load_registry
from suv_deals.domain.profiles import load_business_config
from suv_deals.persistence.config_repo import config_hash
from suv_deals.settings import REPO_ROOT

config_dir = REPO_ROOT / "config"
print(f"  config_yaml_sha256={config_hash(load_business_config(config_dir))}")
print(f"  mcp_sdk={md.version('mcp')} mcp_protocol={mcp_types.LATEST_PROTOCOL_VERSION}")
for c in sorted(load_registry(config_dir).configs, key=lambda c: c.source_key):
    enabled = str(c.enabled).lower()
    print(f"  source {c.source_key} adapter={c.adapter}@{c.adapter_version} mode={c.mode.value} enabled={enabled}")
EOF
  echo "  stored_configuration_revision=record per environment (suv-deals config apply --dry-run)"
  echo "migrations:"
  for file in supabase/migrations/*.sql; do
    echo "  $(basename "$file") sha256=$(sha256sum "$file" | cut -d' ' -f1)"
  done
} >"$report"

for step in "${steps[@]}"; do
  name="${step%%|*}"
  command="${step#*|}"
  reason="$(skip_reason "$name")"
  if [ -n "$reason" ]; then
    results+=("$name=NOT RUN ($reason)")
    skipped=1
    echo "== $name: NOT RUN ($reason)"
    continue
  fi
  echo "== $name"
  if bash -c "$command"; then
    results+=("$name=passed")
  else
    results+=("$name=FAILED")
    failed=1
  fi
done

{
  echo "steps:"
  printf '  %s\n' "${results[@]}"
  if [ "$failed" -eq 0 ] && [ "$skipped" -eq 0 ] && [ "$dirty" = "no" ]; then
    echo "result=verified"
  elif [ "$failed" -eq 0 ] && [ "$dirty" = "no" ]; then
    echo "result=incomplete (steps not run)"
  else
    echo "result=not releasable"
  fi
} >>"$report"

echo "Release report: $report"
tail -n "$(( ${#results[@]} + 2 ))" "$report"
[ "$failed" -eq 0 ] && [ "$skipped" -eq 0 ]
