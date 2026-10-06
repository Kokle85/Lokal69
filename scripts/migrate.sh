#!/usr/bin/env bash
# =============================================================================
# scripts/migrate.sh -- apply pending forward-only migrations (spec section 29)
# =============================================================================
# Applies every supabase/migrations/<YYYYMMDDHHMMSS>_<name>.sql file that is
# not yet recorded in supabase_migrations.schema_migrations (the ledger the
# Supabase CLI also uses), each in its own transaction together with its ledger
# row, using psql -v ON_ERROR_STOP=1 against $DATABASE_URL.
#
# Safety:
#   * prints the target database, server address, port, role and version as
#     reported by the server itself (never the URL or password) and lists the
#     pending migrations before doing anything;
#   * requires typing the database name, or --yes for non-interactive use;
#   * refuses a database that has app/ops schemas but no ledger, or ledger
#     versions unknown to this checkout (wrong checkout or drifted database);
#   * never applies supabase/tests/supabase_emulation.sql (test-only) or
#     supabase/seed.sql (local development only).
#
# Before production: back up the database and evidence objects, run the full
# test suite on the exact release build, and apply expand-first migrations only
# (spec section 29). Rollback policy: scripts/rollback.sh (forward fixes only).
#
# Usage: DATABASE_URL=... scripts/migrate.sh [--dry-run] [--yes]
# Note: psql receives the URL as an argument; run only on a trusted host.
# =============================================================================
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: DATABASE_URL=postgresql://... scripts/migrate.sh [--dry-run] [--yes]

  --dry-run   show the target and pending migrations, change nothing
  --yes       apply without the interactive confirmation (CI/automation)
  -h, --help  show this help
EOF
}

assume_yes=0
dry_run=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes | -y) assume_yes=1 ;;
    --dry-run) dry_run=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "migrate.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ -z "${DATABASE_URL:-}" ]; then
  echo "migrate.sh: DATABASE_URL is not set (server-side secret; never commit it)." >&2
  exit 2
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "migrate.sh: psql is required." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
migrations_dir="$repo_root/supabase/migrations"

# Fail fast instead of queueing behind long locks; no idle open transactions.
export PGOPTIONS="${PGOPTIONS:-} -c lock_timeout=10s -c idle_in_transaction_session_timeout=120s -c client_min_messages=warning"
psql_base=(psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1)

target="$("${psql_base[@]}" -At -F '|' -c \
  "select current_database(), coalesce(host(inet_server_addr()), 'local socket'),
          coalesce(inet_server_port()::text, '-'), current_user, current_setting('server_version')")"
IFS='|' read -r db_name db_host db_port db_user db_version <<<"$target"

echo "Target database : $db_name"
echo "Server address  : $db_host:$db_port"
echo "Connected role  : $db_user"
echo "Server version  : $db_version"

ledger_exists="$("${psql_base[@]}" -At -c \
  "select to_regclass('supabase_migrations.schema_migrations') is not null")"
schemas_exist="$("${psql_base[@]}" -At -c \
  "select exists (select 1 from pg_namespace where nspname in ('app', 'ops'))")"

applied=""
if [ "$ledger_exists" = "t" ]; then
  applied="$("${psql_base[@]}" -At -c \
    "select version from supabase_migrations.schema_migrations order by version")"
elif [ "$schemas_exist" = "t" ]; then
  echo "migrate.sh: refusing: schemas app/ops exist but no migration ledger was found." >&2
  echo "            Reconcile supabase_migrations.schema_migrations manually first." >&2
  exit 3
fi

declare -a pending=()
declare -A known=()
shopt -s nullglob
for file in "$migrations_dir"/*.sql; do
  name="$(basename "$file")"
  if ! [[ "$name" =~ ^([0-9]{14})_([a-z0-9_]+)\.sql$ ]]; then
    echo "migrate.sh: refusing: unexpected migration file name: $name" >&2
    exit 3
  fi
  version="${BASH_REMATCH[1]}"
  known["$version"]=1
  if ! grep -qx -- "$version" <<<"$applied"; then
    pending+=("$file")
  fi
done

while IFS= read -r version; do
  [ -z "$version" ] && continue
  if [ -z "${known[$version]:-}" ]; then
    echo "migrate.sh: refusing: database has migration $version that this checkout does not contain." >&2
    exit 3
  fi
done <<<"$applied"

if [ "${#pending[@]}" -eq 0 ]; then
  echo "No pending migrations."
  exit 0
fi

echo "Pending migrations (${#pending[@]}):"
for file in "${pending[@]}"; do
  echo "  - $(basename "$file")"
done

if [ "$dry_run" -eq 1 ]; then
  echo "Dry run: nothing applied."
  exit 0
fi

if [ "$assume_yes" -ne 1 ]; then
  if [ ! -t 0 ]; then
    echo "migrate.sh: refusing: no terminal for confirmation; pass --yes to apply non-interactively." >&2
    exit 3
  fi
  read -r -p "Type the database name ($db_name) to apply these migrations: " answer
  if [ "$answer" != "$db_name" ]; then
    echo "Aborted; nothing applied."
    exit 3
  fi
fi

"${psql_base[@]}" \
  -c "create schema if not exists supabase_migrations" \
  -c "create table if not exists supabase_migrations.schema_migrations (version text not null primary key, statements text[], name text)"

for file in "${pending[@]}"; do
  name="$(basename "$file")"
  [[ "$name" =~ ^([0-9]{14})_([a-z0-9_]+)\.sql$ ]]
  version="${BASH_REMATCH[1]}"
  label="${BASH_REMATCH[2]}"
  echo "Applying $name ..."
  # File name parts were validated against ^[0-9]{14}_[a-z0-9_]+$, so they are
  # safe SQL literals. The migration and its ledger row commit atomically.
  "${psql_base[@]}" --single-transaction \
    -f "$file" \
    -c "insert into supabase_migrations.schema_migrations (version, name) values ('$version', '$label')"
done

echo "Applied ${#pending[@]} migration(s) to $db_name."
