#!/usr/bin/env bash
# =============================================================================
# scripts/backup.sh -- logical backup of the application schemas (spec section 29)
# =============================================================================
# Writes, with a UTC timestamp, into --output-dir (default var/backups, mode 0700, files 0600):
#
#   suv-deals_<ts>.dump          pg_dump custom format of schemas app, ops (+ supabase_migrations)
#   suv-deals_<ts>.members       ids of the auth users that hold memberships (ids ONLY: no e-mail,
#                                no password hash); restore_check.sh needs them for the FK
#   suv-deals_<ts>.manifest      server version, sha256 of both files, row counts per key table,
#                                job/outbox states and latest timestamps, all taken in the SAME
#                                snapshot as the dump (pg_export_snapshot + pg_dump --snapshot)
#   suv-deals_<ts>.snapshots.tar.gz   only with --with-local-snapshots DIR (local evidence store)
#
# Database backups do NOT include Supabase Storage objects (retained evidence snapshots). Back
# them up separately with their own retention (docs/runbook.md "Backup"); a provider's managed
# backup/PITR must be checked on the actual project plan, never assumed.
#
# Safety: read-only on the source. Prints the target as reported by the server (database,
# address, role, version), never the connection string or password. Use the schema-owner role
# (it bypasses RLS) through a DIRECT or SESSION connection (exported snapshots do not work through
# a transaction pooler). psql/pg_dump receive the connection string WITHOUT its password (process
# lists show arguments); libpq reads the password from PGPASSWORD. A key=value DSN with an inline
# password cannot be split here: use the URL form, PGPASSWORD or ~/.pgpass instead.
#
# Usage: BACKUP_DATABASE_URL=postgresql://... scripts/backup.sh [--output-dir DIR]
#            [--with-local-snapshots DIR]
#        (falls back to DATABASE_URL when BACKUP_DATABASE_URL is unset)
# Exit codes: 0 ok, 1 failure, 2 usage.
# =============================================================================
set -euo pipefail
umask 077

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="$repo_root/var/backups"
snapshot_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir) output_dir="${2:?--output-dir needs a directory}"; shift ;;
    --with-local-snapshots) snapshot_dir="${2:?--with-local-snapshots needs a directory}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "backup.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

url="${BACKUP_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -z "$url" ]; then
  echo "backup.sh: set BACKUP_DATABASE_URL (schema-owner role, direct/session connection)." >&2
  exit 2
fi
for tool in psql sha256sum python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "backup.sh: $tool is required." >&2; exit 2; }
done

# Keep the password out of every child's argument list: print the URL without its password
# (line 1) and the password (line 2). The URL travels in the environment, never as an argument.
split_password() {
  SUV_PG_URL="$1" python3 - <<'EOF'
import os
from urllib.parse import unquote, urlsplit

url = os.environ["SUV_PG_URL"]
password = None
if "://" in url:
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, hosts = netloc.rpartition("@")
        user, sep, secret = userinfo.partition(":")
        if sep:
            password = unquote(secret)
        netloc = f"{user}@{hosts}" if user else hosts
    kept = []
    for item in parts.query.split("&") if parts.query else []:
        key, _, value = item.partition("=")
        if unquote(key) == "password":
            password = unquote(value)
        else:
            kept.append(item)
    # Rebuilt by hand: urlunsplit would turn "postgresql:///db" (no authority) into "postgresql:/db".
    query = "&".join(kept)
    url = f"{parts.scheme}://{netloc}{parts.path}" + (f"?{query}" if query else "")
print(url)
print(password or "")
EOF
}
split="$(split_password "$url")" || { echo "backup.sh: the connection string cannot be parsed." >&2; exit 2; }
url="$(sed -n 1p <<<"$split")"
url_password="$(sed -n 2p <<<"$split")"
unset split
if [ -n "$url_password" ]; then export PGPASSWORD="$url_password"; fi
unset url_password
case "$url" in
  *password=*) echo "backup.sh: warning: the key=value DSN keeps its inline password (use a URL or PGPASSWORD)." >&2 ;;
esac

psql_q=(psql "$url" -X -q -A -t -v ON_ERROR_STOP=1)
target="$("${psql_q[@]}" -F '|' -c \
  "select current_database(), coalesce(host(inet_server_addr()), 'local socket'), current_user,
          current_setting('server_version'), current_setting('server_version_num')::int / 10000,
          (select rolsuper or rolbypassrls from pg_roles where rolname = current_user)")" \
  || { echo "backup.sh: cannot connect to the backup source (check BACKUP_DATABASE_URL)." >&2; exit 1; }
IFS='|' read -r db_name db_host db_user server_version server_major can_read_all <<<"$target"
[[ "$server_major" =~ ^[0-9]+$ ]] || { echo "backup.sh: unexpected answer from the backup source." >&2; exit 1; }

# pg_dump must not be older than the server: prefer the matching Debian/Ubuntu binary.
pg_bin=""
for candidate in "${PG_BIN_DIR:-}" "/usr/lib/postgresql/$server_major/bin"; do
  if [ -n "$candidate" ] && [ -x "$candidate/pg_dump" ]; then pg_bin="$candidate"; break; fi
done
pg_dump_cmd="${pg_bin:+$pg_bin/}pg_dump"
command -v "$pg_dump_cmd" >/dev/null 2>&1 || { echo "backup.sh: pg_dump is required." >&2; exit 2; }
dump_major="$("$pg_dump_cmd" --version | sed -E 's/.* ([0-9]+)(\.[0-9]+)?.*/\1/')"
if [ "$dump_major" -lt "$server_major" ]; then
  echo "backup.sh: pg_dump $dump_major is older than server $server_major; set PG_BIN_DIR." >&2
  exit 2
fi

echo "Backup source (as reported by the server; the URL is never printed):"
echo "  database : $db_name"
echo "  address  : $db_host"
echo "  role     : $db_user"
echo "  version  : $server_version"
if [ "$can_read_all" != "t" ]; then
  echo "backup.sh: role $db_user is subject to RLS; use the schema-owner role so nothing is filtered." >&2
  exit 2
fi

ts="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$output_dir"
chmod 700 "$output_dir"
prefix="$output_dir/suv-deals_$ts"

tables=(app.workspaces app.memberships app.config_revisions app.search_profiles app.sources app.listings
  app.listing_revisions app.listing_observations app.field_evidence app.valuations app.review_cases
  app.review_decisions app.owner_notes ops.jobs ops.outbox ops.audit_events ops.source_snapshots
  ops.api_credentials ops.event_subscriptions ops.crawl_runs)
parts=()
for t in "${tables[@]}"; do
  parts+=("select 'count.$t', count(*)::text from $t")
done
latest() { # name, table
  parts+=("select 'latest.$1', coalesce(to_char(max(created_at) at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'none') from $2")
}
latest listing_revision app.listing_revisions
latest config_revision app.config_revisions
latest review_decision app.review_decisions
latest audit_event ops.audit_events
parts+=("select 'jobs.state.' || state, count(*)::text from ops.jobs group by state")
parts+=("select 'outbox.state.' || state, count(*)::text from ops.outbox group by state")
parts+=("select 'workspaces.active_without_owner', count(*)::text from app.workspaces w where w.active
  and not exists (select 1 from app.memberships m where m.workspace_id = w.id and m.active and m.role = 'owner')")
union="$(printf '%s union all ' "${parts[@]}")"
summary_sql="select string_agg(k || '=' || v, ' ' order by k) from (${union% union all }) as t(k, v);"
members_sql="select coalesce(string_agg(distinct user_id::text, ',' order by user_id::text), '') from app.memberships;"
ledger_sql="select to_regclass('supabase_migrations.schema_migrations') is not null;"

# One REPEATABLE READ snapshot for the dump, the counts and the member ids.
coproc PSQL { psql "$url" -X -q -A -t -v ON_ERROR_STOP=1 2>&1; }
psql_in="${PSQL[1]}"
psql_out="${PSQL[0]}"
ask() { # one query -> exactly one output line
  printf '%s\n' "$1" >&"$psql_in"
  local line
  IFS= read -r -t 120 line <&"$psql_out" || { echo "backup.sh: snapshot session failed" >&2; exit 1; }
  printf '%s' "$line"
}
printf '%s\n' "begin isolation level repeatable read read only;" >&"$psql_in"
snapshot="$(ask "select pg_export_snapshot();")"
[[ "$snapshot" =~ ^[0-9A-F-]+$ ]] || { echo "backup.sh: could not export a snapshot ($snapshot)" >&2; exit 1; }
has_ledger="$(ask "$ledger_sql")"

schema_args=(--schema=app --schema=ops)
[ "$has_ledger" = "t" ] && schema_args+=(--schema=supabase_migrations)
started=$(date +%s)
"$pg_dump_cmd" --format=custom --snapshot="$snapshot" "${schema_args[@]}" --file="$prefix.dump" "$url"
summary="$(ask "$summary_sql")"
members="$(ask "$members_sql")"
printf '%s\n' "commit;" >&"$psql_in"
exec {psql_in}>&-
wait "$PSQL_PID" 2>/dev/null || true
# The snapshot session answers on one pipe with stderr merged: an error line must never end up
# in the manifest or the member-id file.
[[ "$summary" =~ ^count\.[a-z_.]+=[0-9]+( [a-z_.]+=[^[:space:]]+)*$ ]] \
  || { echo "backup.sh: unexpected row-count answer from the snapshot session" >&2; exit 1; }
[[ -z "$members" || "$members" =~ ^[0-9a-f-]{36}(,[0-9a-f-]{36})*$ ]] \
  || { echo "backup.sh: unexpected member-id answer from the snapshot session" >&2; exit 1; }

if [ -n "$members" ]; then tr ',' '\n' <<<"$members" >"$prefix.members"; else : >"$prefix.members"; fi

{
  echo "format=suv-deals-backup/1"
  echo "created_at=$ts"
  echo "database=$db_name"
  echo "server_version=$server_version"
  echo "server_major=$server_major"
  echo "pg_dump_major=$dump_major"
  echo "schemas=app,ops$([ "$has_ledger" = "t" ] && echo ",supabase_migrations")"
  echo "dump_file=$(basename "$prefix.dump")"
  echo "dump_sha256=$(sha256sum "$prefix.dump" | cut -d' ' -f1)"
  echo "members_file=$(basename "$prefix.members")"
  echo "members_sha256=$(sha256sum "$prefix.members" | cut -d' ' -f1)"
  tr ' ' '\n' <<<"$summary"
} >"$prefix.manifest"

if [ -n "$snapshot_dir" ]; then
  if [ -d "$snapshot_dir" ]; then
    tar -C "$snapshot_dir" -czf "$prefix.snapshots.tar.gz" .
    echo "snapshots_sha256=$(sha256sum "$prefix.snapshots.tar.gz" | cut -d' ' -f1)" >>"$prefix.manifest"
  else
    echo "backup.sh: --with-local-snapshots directory does not exist" >&2
    exit 2
  fi
fi

echo "Wrote (in $(($(date +%s) - started)) s):"
echo "  $prefix.dump"
echo "  $prefix.members"
echo "  $prefix.manifest"
[ -n "$snapshot_dir" ] && echo "  $prefix.snapshots.tar.gz"
echo "Reminder: Storage objects (retained evidence) are NOT in this backup; back them up separately."
echo "Prove it: RESTORE_ADMIN_URL=<local admin URL> scripts/restore_check.sh $prefix.manifest"
