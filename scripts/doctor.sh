#!/usr/bin/env bash
# =============================================================================
# scripts/doctor.sh -- presence-only readiness report (spec sections 26, 27, 30)
# =============================================================================
# Thin wrapper around `suv-deals doctor`: configuration per process (set/missing only), the
# confirmed business baseline, database schema/role checks, source gates, offline OAuth checks
# and the notification/event mode. It never prints secret values, never creates accounts, keys
# or data. Pass --crawler for the read-only crawler health/contract inspection.
#
# Usage: scripts/doctor.sh [doctor options]      e.g. scripts/doctor.sh --process worker --crawler
# Exit codes: 0 ok, 1 problems found, 2 usage, 4 dependency unavailable.
# =============================================================================
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if command -v uv >/dev/null 2>&1 && [ -f uv.lock ]; then
  exec uv run --frozen suv-deals doctor "$@"
elif command -v suv-deals >/dev/null 2>&1; then
  exec suv-deals doctor "$@"
else
  echo "doctor.sh: neither uv nor the suv-deals command is available (run: make install)" >&2
  exit 2
fi
