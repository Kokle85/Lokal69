#!/usr/bin/env bash
# =============================================================================
# scripts/verify_release.sh -- verify the EXACT release build and write a release report
# (spec sections 29, 31 "Tests must run on the exact final commit/build", 34)
# =============================================================================
# Steps (each recorded as passed / failed / not run in var/releases/<sha>_<ts>.txt):
#   1. commit SHA, dirty-tree flag, uv.lock / dependency lock hashes, uv and Python versions;
#   2. ruff check + ruff format --check;
#   3. mypy --strict (pyproject [tool.mypy]);
#   4. JSON schema snapshot check (scripts/export_schemas.py --check);
#   5. migration list with per-file sha256 (supabase/migrations);
#   6. tests without a database, then database tests on PostgreSQL 16 and 17
#      (TEST_DATABASE_ADMIN_URL / PG17_ADMIN_URL; --skip-db records them as NOT RUN);
#   7. optional: the image digest you deploy (RELEASE_IMAGE_DIGEST) and the crawler digest
#      (CRAWL4AI_IMAGE_DIGEST) are recorded, never invented.
#
# A dirty working tree is reported and makes the result "not releasable": release from a commit.
#
# Usage: scripts/verify_release.sh [--skip-db] [--plan]
#   --plan     print the steps without running anything
#   --skip-db  do not run the database tests (recorded as NOT RUN; the release is then not verified)
# Exit codes: 0 every executed step passed and nothing was skipped, 1 otherwise, 2 usage.
# =============================================================================
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
skip_db=0
plan=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-db) skip_db=1 ;;
    --plan) plan=1 ;;
    -h | --help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify_release.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

PG16_ADMIN_URL="${TEST_DATABASE_ADMIN_URL:-postgresql://suv:suv@127.0.0.1:5432/postgres}"
PG17_ADMIN_URL="${PG17_ADMIN_URL:-postgresql://suv:suv@127.0.0.1:5433/postgres}"
PG17_MIGRATOR_ROLE="${PG17_MIGRATOR_ROLE:-suv_migrator}"

steps=(
  "lint|uv run --frozen ruff check src tests scripts && uv run --frozen ruff format --check src tests scripts"
  "typecheck|uv run --frozen mypy"
  "schemas|uv run --frozen python scripts/export_schemas.py --check"
  "tests_without_db|uv run --frozen pytest -q -m 'not db and not live and not e2e'"
  "tests_db_pg16|TEST_DATABASE_ADMIN_URL='$PG16_ADMIN_URL' uv run --frozen pytest -q -m 'db and not live and not e2e'"
  "tests_db_pg17|TEST_DATABASE_ADMIN_URL='$PG17_ADMIN_URL' TEST_DATABASE_MIGRATOR_ROLE='$PG17_MIGRATOR_ROLE' uv run --frozen pytest -q -m 'db and not live and not e2e'"
)

if [ "$plan" -eq 1 ]; then
  echo "verify_release.sh plan (nothing is run):"
  echo "  record: commit SHA, dirty flag, lock hashes, tool versions, migration hashes"
  for step in "${steps[@]}"; do
    name="${step%%|*}"
    if [ "$skip_db" -eq 1 ] && [[ "$name" == tests_db_* ]]; then
      echo "  $name: NOT RUN (--skip-db)"
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
mkdir -p var/releases
report="var/releases/${sha:0:12}_$ts.txt"
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
  echo "python_version=$(uv run --frozen python -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
  echo "app_version=$(uv run --frozen python -c 'import suv_deals; print(suv_deals.__version__)' 2>/dev/null || echo unknown)"
  echo "release_image_digest=${RELEASE_IMAGE_DIGEST:-not recorded}"
  echo "crawl4ai_image_digest=${CRAWL4AI_IMAGE_DIGEST:-not recorded}"
  echo "migrations:"
  for file in supabase/migrations/*.sql; do
    echo "  $(basename "$file") sha256=$(sha256sum "$file" | cut -d' ' -f1)"
  done
} >"$report"

for step in "${steps[@]}"; do
  name="${step%%|*}"
  command="${step#*|}"
  if [ "$skip_db" -eq 1 ] && [[ "$name" == tests_db_* ]]; then
    results+=("$name=NOT RUN (--skip-db)")
    skipped=1
    echo "== $name: NOT RUN (--skip-db)"
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
