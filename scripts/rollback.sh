#!/usr/bin/env bash
# =============================================================================
# scripts/rollback.sh -- database rollback policy: FORWARD FIXES ONLY
# =============================================================================
# This script never runs a down-migration, never drops tables, schemas or data
# and never deletes historical listing, review or valuation records (spec
# section 29: "Prefer forward fixes for database migrations; do not assume
# destructive down-migrations are safe").
#
#   scripts/rollback.sh            print the rollback policy (exit 0)
#   scripts/rollback.sh --status   read-only: show the target and the migration
#                                  ledger (needs DATABASE_URL; never prints it)
#   anything else (a version, --down, --force, ...) is refused (exit 2).
# =============================================================================
set -euo pipefail

policy() {
  cat <<'EOF'
Database rollback policy (forward fixes only)

1. Roll back the APPLICATION, not the schema: redeploy the previous known-good
   image digest. Migrations are expand/contract compatible, so the previous
   release runs against the newer schema.
2. Fix forward: write a NEW migration
   supabase/migrations/<YYYYMMDDHHMMSS>_fix_<topic>.sql that corrects the
   problem (for example re-adds a column or relaxes a constraint), test it with
   the database test suite, and apply it with scripts/migrate.sh.
3. Never delete history as a deployment repair. Append-only tables are
   protected by grants and triggers; the documented maintenance bypass
   (app.history_maintenance) is reserved for the owner-approved privacy
   deletion process, not for rollbacks.
4. For a breaking change, document restore or compensation steps and expected
   downtime BEFORE applying it. A destructive recovery means restoring a
   backup into an ISOLATED environment with crawling and outbound
   notifications disabled, then verifying schema, row counts, latest
   revisions, queue integrity, memberships and evidence hashes (spec 29).
5. After an application rollback verify: running digest, migration
   compatibility, queue leases, source pauses and notification dedup. Old
   workers must reject unknown payload versions as an incompatible-version
   blocker.
EOF
}

case "${1:-}" in
  "" | --policy | -h | --help)
    policy
    exit 0
    ;;
  --status)
    if [ -z "${DATABASE_URL:-}" ]; then
      echo "rollback.sh: DATABASE_URL is not set." >&2
      exit 2
    fi
    psql_ro=(psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1 -At)
    export PGOPTIONS="${PGOPTIONS:-} -c default_transaction_read_only=on"
    "${psql_ro[@]}" -F ' | ' -c \
      "select 'target: ' || current_database(), coalesce(host(inet_server_addr()), 'local socket'), current_user"
    if [ "$("${psql_ro[@]}" -c "select to_regclass('supabase_migrations.schema_migrations') is not null")" = "t" ]; then
      "${psql_ro[@]}" -F ' | ' -c \
        "select version, coalesce(name, '') from supabase_migrations.schema_migrations order by version"
    else
      echo "no migration ledger found"
    fi
    exit 0
    ;;
  *)
    echo "rollback.sh: refusing '$1': destructive down-migrations are not supported." >&2
    echo "Use a forward-fix migration and/or an application image rollback:" >&2
    echo >&2
    policy >&2
    exit 2
    ;;
esac
