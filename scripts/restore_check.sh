#!/usr/bin/env bash
# =============================================================================
# scripts/restore_check.sh -- prove a backup by restoring it into an ISOLATED local database
# (spec section 29 "Backup and restoration"; a backup that was never restored is not proof)
# =============================================================================
# Steps (the elapsed time is recorded in the report):
#   1. verify the sha256 of the dump and member-id files against the manifest;
#   2. create a NEW database suv_restore_check_<ts> on a LOOPBACK cluster (RESTORE_ADMIN_URL must
#      be 127.0.0.1/localhost/::1 or a local socket; anything else is refused);
#   3. prepare it like a Supabase project for these schemas: the test-only Supabase emulation
#      (auth schema, roles, pgcrypto), btree_gist in schema extensions, role suv_backend, and the
#      member ids as minimal auth.users rows (no e-mail, no credentials);
#   4. pg_restore --no-owner --no-acl, then re-apply only the GRANT/REVOKE statements for
#      suv_backend, PUBLIC, anon, authenticated and service_role;
#   5. verify: schema markers, every row count / job and outbox state / latest timestamp in the
#      manifest, queue lease invariants, workspaces with an active owner, the migration ledger,
#      and revision evidence hashes through `suv-deals evidence verify --skip-objects` run as
#      suv_backend (which also proves the restored grants and RLS work);
#   6. drop the database (unless --keep) and write <prefix>.restore_report.txt.
#
# Isolation: no worker, scheduler or dispatcher is ever started against the restored copy, and the
# evidence check runs with SOURCE_NETWORK_ENABLED=false and ALLOW_EXTERNAL_NOTIFICATIONS=false.
# Restored jobs, subscriptions and outbox rows can act externally if a process is pointed at the
# copy: never do that with network or notification switches on. Storage objects are not part of a
# database backup; check their separate backup by hash (docs/runbook.md).
#
# Usage: RESTORE_ADMIN_URL=postgresql://user:pw@127.0.0.1:5433/postgres \
#          scripts/restore_check.sh var/backups/suv-deals_<ts>.manifest [--keep]
# Exit codes: 0 every check passed, 1 a check failed, 2 usage, 3 refused (non-local target).
# =============================================================================
set -euo pipefail
umask 077

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest=""
keep=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --keep) keep=1 ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "restore_check.sh: unknown option: $1" >&2; exit 2 ;;
    *) manifest="$1" ;;
  esac
  shift
done
if [ -z "$manifest" ]; then
  echo "restore_check.sh: pass the backup manifest (var/backups/suv-deals_<ts>.manifest)." >&2
  exit 2
fi
[ -f "$manifest" ] || manifest="${manifest%.manifest}.manifest"
[ -f "$manifest" ] || { echo "restore_check.sh: manifest not found" >&2; exit 2; }
admin_url="${RESTORE_ADMIN_URL:-}"
[ -n "$admin_url" ] || { echo "restore_check.sh: set RESTORE_ADMIN_URL (local cluster admin URL)." >&2; exit 2; }

# --- 2a. the target must be a local, isolated cluster -------------------------------------
target_host="$(python3 - "$admin_url" <<'EOF'
import sys
from urllib.parse import urlsplit
url = sys.argv[1]
if "://" in url:
    print(urlsplit(url).hostname or "local socket")
else:
    parts = dict(p.split("=", 1) for p in url.split() if "=" in p)
    print(parts.get("host", "local socket"))
EOF
)"
case "$target_host" in
  127.0.0.1 | localhost | ::1 | "local socket" | /*) ;;
  *) echo "restore_check.sh: refused: RESTORE_ADMIN_URL must point to a loopback/local cluster." >&2; exit 3 ;;
esac

value() { { grep -E "^$1=" "$manifest" || true; } | head -1 | cut -d= -f2-; }
dir="$(dirname "$manifest")"
prefix="${manifest%.manifest}"
dump="$dir/$(value dump_file)"
members="$dir/$(value members_file)"
report="$prefix.restore_report.txt"
started=$(date +%s)
failures=()
lines=()
note() { lines+=("$1"); echo "$1"; }
check() { # name, ok(0/1), detail
  if [ "$2" = "0" ]; then note "PASS  $1: $3"; else note "FAIL  $1: $3"; failures+=("$1"); fi
}
expect() { # name, detail, command... (the command's status decides; safe under set -e)
  local name="$1" detail="$2"
  shift 2
  if "$@"; then check "$name" 0 "$detail"; else check "$name" 1 "$detail"; fi
}

# --- 1. file integrity ---------------------------------------------------------------------
expect "dump_sha256" "dump file matches the manifest hash" \
  [ "$(sha256sum "$dump" | cut -d' ' -f1)" = "$(value dump_sha256)" ]
expect "members_sha256" "member-id file matches the manifest hash" \
  [ "$(sha256sum "$members" | cut -d' ' -f1)" = "$(value members_sha256)" ]
if [ "${#failures[@]}" -gt 0 ]; then
  echo "restore_check.sh: backup files do not match the manifest; nothing restored." >&2
  exit 1
fi

psql_admin=(psql "$admin_url" -X -q -A -t -v ON_ERROR_STOP=1)
restore_major="$("${psql_admin[@]}" -c "select current_setting('server_version_num')::int / 10000")"
source_major="$(value server_major)"
if [ "$restore_major" -lt "$source_major" ]; then
  echo "restore_check.sh: refused: the local cluster ($restore_major) is older than the source ($source_major)." >&2
  exit 3
fi
pg_bin=""
for candidate in "${PG_BIN_DIR:-}" "/usr/lib/postgresql/$restore_major/bin"; do
  if [ -n "$candidate" ] && [ -x "$candidate/pg_restore" ]; then pg_bin="$candidate"; break; fi
done
pg_restore_cmd="${pg_bin:+$pg_bin/}pg_restore"

# --- 2b. isolated database -------------------------------------------------------------------
db="suv_restore_check_$(date -u +%Y%m%d%H%M%S)_$RANDOM"
restore_url="$(python3 - "$admin_url" "$db" <<'EOF'
import sys
from urllib.parse import urlsplit, urlunsplit
url, db = sys.argv[1], sys.argv[2]
if "://" in url:
    parts = urlsplit(url)
    print(urlunsplit((parts.scheme, parts.netloc, "/" + db, parts.query, parts.fragment)))
else:
    kept = [p for p in url.split() if not p.startswith("dbname=")]
    print(" ".join([*kept, f"dbname={db}"]))
EOF
)"
"${psql_admin[@]}" -c "create database \"$db\""
cleanup() {
  if [ "$keep" -eq 0 ]; then
    "${psql_admin[@]}" -c "drop database if exists \"$db\" with (force)" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
note "Restoring into isolated local database $db (PostgreSQL $restore_major; source $source_major)."
psql_r=(psql "$restore_url" -X -q -A -t -v ON_ERROR_STOP=1)

# --- 3. Supabase-like prerequisites -----------------------------------------------------------
"${psql_r[@]}" -f "$repo_root/supabase/tests/supabase_emulation.sql" >/dev/null
"${psql_r[@]}" -c "create extension if not exists btree_gist with schema extensions" >/dev/null
"${psql_r[@]}" -c "do \$\$ begin
  if not exists (select 1 from pg_roles where rolname = 'suv_backend') then
    create role suv_backend nologin noinherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
  end if; end \$\$" >/dev/null
if [ -s "$members" ]; then
  while IFS= read -r member; do
    [[ "$member" =~ ^[0-9a-f-]{36}$ ]] || { echo "restore_check.sh: invalid member id in backup" >&2; exit 1; }
    "${psql_r[@]}" -c "insert into auth.users (id) values ('$member') on conflict do nothing" >/dev/null
  done <"$members"
fi

# --- 4. restore ---------------------------------------------------------------------------------
"$pg_restore_cmd" --no-owner --no-acl --exit-on-error --dbname="$restore_url" "$dump"
"$pg_restore_cmd" --no-owner --file=- "$dump" \
  | grep -E '^(GRANT|REVOKE) .* (TO|FROM) (suv_backend|PUBLIC|anon|authenticated|service_role);$' \
  | "${psql_r[@]}" >/dev/null
note "Restored $(value dump_file) (+ grants for suv_backend/PUBLIC/anon/authenticated/service_role)."

# --- 5. verification ----------------------------------------------------------------------------
q() { "${psql_r[@]}" -c "$1" </dev/null; }
missing_markers="$(q "select coalesce(string_agg(t, ', '), '') from unnest(array['app.search_profiles',
  'ops.source_schedules', 'app.field_evidence', 'app.valuations', 'app.notification_preferences',
  'ops.activation_gates', 'ops.host_budgets', 'ops.idempotency_records']) as t
  where to_regclass(t) is null")"
expect "schema" "required tables present${missing_markers:+ (missing: $missing_markers)}" [ -z "$missing_markers" ]

while IFS='=' read -r key expected; do
  case "$key" in
    count.*)
      table="${key#count.}"
      [[ "$table" =~ ^(app|ops)\.[a-z_]+$ ]] || { check "$key" 1 "invalid table name in manifest"; continue; }
      actual="$(q "select count(*) from $table")"
      expect "$key" "restored $actual, manifest $expected" [ "$actual" = "$expected" ]
      ;;
    latest.*)
      case "${key#latest.}" in
        listing_revision) table=app.listing_revisions ;;
        config_revision) table=app.config_revisions ;;
        review_decision) table=app.review_decisions ;;
        audit_event) table=ops.audit_events ;;
        *) check "$key" 1 "unknown latest key"; continue ;;
      esac
      actual="$(q "select coalesce(to_char(max(created_at) at time zone 'UTC',
        'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'none') from $table")"
      expect "$key" "restored $actual, manifest $expected" [ "$actual" = "$expected" ]
      ;;
    workspaces.active_without_owner)
      actual="$(q "select count(*) from app.workspaces w where w.active and not exists (select 1
        from app.memberships m where m.workspace_id = w.id and m.active and m.role = 'owner')")"
      expect "memberships" "active workspaces without an active owner: restored $actual, manifest $expected" \
        [ "$actual" = "$expected" ]
      ;;
  esac
done <"$manifest"

norm() { tr ' ' '\n' | sed '/^$/d' | sort | paste -sd' ' -; }
for queue in jobs outbox; do
  table="ops.$queue"
  expected="$({ grep -E "^$queue\.state\." "$manifest" || true; } | sed -E "s/^$queue\.state\.//" | norm)"
  actual="$(q "select coalesce(string_agg(state || '=' || n, ' ' order by state), '') from
    (select state, count(*) as n from $table group by state) s" | norm)"
  expect "$queue.states" "restored [$actual], manifest [$expected]" [ "$actual" = "$expected" ]
done
bad_leases="$(q "select (select count(*) from ops.jobs where state = 'running'
  and (lease_owner is null or lease_token is null or lease_expires_at is null))
  + (select count(*) from ops.outbox where state = 'sending' and lease_owner is null)")"
expect "queue.leases" "running jobs / sending events without a complete lease: $bad_leases" [ "$bad_leases" = "0" ]
running="$(q "select count(*) from ops.jobs where state = 'running'")"
note "INFO  queue.running: $running restored job lease(s) would expire and be reaped (never resumed blindly)"

if grep -q "supabase_migrations" <<<"$(value schemas)"; then
  ledger="$(q "select count(*) from supabase_migrations.schema_migrations")"
  expect "migration_ledger" "$ledger ledger row(s) restored" [ "$ledger" -gt 0 ]
fi

cli=(suv-deals)
if command -v uv >/dev/null 2>&1 && [ -f "$repo_root/uv.lock" ]; then cli=(uv run --frozen --project "$repo_root" suv-deals); fi
set +e
evidence_output="$(env DATABASE_URL="$restore_url" DATABASE_SET_ROLE=suv_backend APP_ENV=test LOG_LEVEL=WARNING \
  SOURCE_NETWORK_ENABLED=false ALLOW_EXTERNAL_NOTIFICATIONS=false \
  "${cli[@]}" --no-env-file evidence verify --skip-objects 2>&1)"
evidence_status=$?
set -e
summary_line="$(grep -E 'revisions: ' <<<"$evidence_output" | tr -s ' ' | paste -sd ';' - || true)"
check "evidence_hashes" "$([ "$evidence_status" -eq 0 ] && echo 0 || echo 1)" \
  "revision semantic hashes as suv_backend: ${summary_line:-no active workspace} (exit $evidence_status)"

elapsed=$(($(date +%s) - started))
note "INFO  elapsed: ${elapsed} s (integrity check, isolated restore and verification)"
if [ "$keep" -eq 1 ]; then
  note "INFO  kept database $db: never point workers/dispatcher at it with network or notifications on"
fi
result="PASS"
[ "${#failures[@]}" -eq 0 ] || result="FAIL (${failures[*]})"
{
  echo "suv-deals restore check"
  echo "manifest=$(basename "$manifest")"
  echo "checked_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "restore_cluster_major=$restore_major"
  echo "elapsed_seconds=$elapsed"
  echo "result=$result"
  printf '%s\n' "${lines[@]}"
} >"$report"
echo "Result: $result (report: $report)"
[ "${#failures[@]}" -eq 0 ]
