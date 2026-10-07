# SUV deal system: developer and operator targets (spec section 27; docs/runbook.md).
#
#   cp .env.example .env      # fill secrets with the approved secure method, outside source control
#   make doctor install test-unit db-local-start db-migrate-local test-db test-integration dev smoke-local
#
# Safety rules:
#   * nothing here touches a production database: the db-* targets only accept loopback targets and
#     never read DATABASE_URL from .env (they use LOCAL_DATABASE_URL);
#   * `dev` forces SOURCE_NETWORK_ENABLED=false and ALLOW_EXTERNAL_NOTIFICATIONS=false and uses the
#     local database only;
#   * the destructive `db-reset-local` is separately named, needs CONFIRM_DB_RESET=yes-drop-<db>
#     and is never called by another target.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

UV ?= uv
CLI := $(UV) run suv-deals
# Local PostgreSQL clusters used by the tests (16 = default, 17 = the Supabase project version).
# These are the local test superuser credentials of the development clusters, not secrets.
PG16_ADMIN_URL ?= postgresql://suv:suv@127.0.0.1:5432/postgres
PG17_ADMIN_URL ?= postgresql://suv:suv@127.0.0.1:5433/postgres
PG17_MIGRATOR_ROLE ?= suv_migrator
LOCAL_DB ?= suv_dev
LOCAL_ADMIN_URL ?= $(PG16_ADMIN_URL)
LOCAL_DATABASE_URL ?= postgresql://suv:suv@127.0.0.1:5432/$(LOCAL_DB)
DEV_API_PORT ?= 8000
PYTEST ?= $(UV) run pytest
NOT_LIVE := not live and not e2e

# Local-target guard used before anything touches LOCAL_ADMIN_URL / LOCAL_DATABASE_URL. It parses
# the connection string like libpq does (every comma-separated host, ?host= / ?hostaddr=
# parameters, PGHOST / PGHOSTADDR / PGSERVICE defaults), prints the target without the password and
# exits 3 unless all of it stays on this machine: a text match on "@127.0.0.1" is not enough.
LOCAL_GUARD = $(CLI) --no-env-file db target --local-only --url-env

# Every process of `make dev` runs with these overrides (environment beats .env).
DEV_ENV := APP_ENV=development SOURCE_NETWORK_ENABLED=false ALLOW_EXTERNAL_NOTIFICATIONS=false \
	EVENT_BRIDGE_ENABLED=false MCP_EVENTS_ENABLED=false NOTIFICATION_PROVIDER=disabled \
	DATABASE_URL="$(LOCAL_DATABASE_URL)" DATABASE_SET_ROLE=suv_backend

.PHONY: help doctor install test-unit db-local-start db-migrate-local test-db test-integration dev \
	dev-fixtures smoke-local lint format typecheck test test-all schemas schemas-check \
	dashboard-install dashboard-build dashboard-test dashboard-lint outlook-bridge-test \
	verify-release backup restore-check db-reset-local

help: ## List the targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort \
	  | awk 'BEGIN {FS = ":.*## "} {printf "  %-22s %s\n", $$1, $$2}'

# --------------------------------------------------------------------------------------------
# Spec 27 setup sequence
# --------------------------------------------------------------------------------------------

doctor: ## Presence-only readiness report (never prints values; exit 1 on errors)
	$(CLI) doctor

install: ## Install the locked dependencies (uv.lock; never upgrades a system runtime)
	$(UV) sync --frozen

test-unit: ## Tests that need no database or network
	$(PYTEST) -q -m "not db and $(NOT_LIVE)"

db-local-start: ## Start the local PostgreSQL 16/17 test clusters (or print how)
	@if command -v pg_ctlcluster >/dev/null 2>&1 && command -v pg_lsclusters >/dev/null 2>&1; then \
	  for version in 16 17; do \
	    if pg_lsclusters --no-header 2>/dev/null | awk '{print $$1" "$$2" "$$4}' | grep -q "^$$version main online"; then \
	      echo "PostgreSQL $$version/main is already running."; \
	    elif pg_lsclusters --no-header 2>/dev/null | awk '{print $$1" "$$2}' | grep -q "^$$version main"; then \
	      pg_ctlcluster $$version main start || echo "Could not start $$version/main (try: sudo pg_ctlcluster $$version main start)"; \
	    else \
	      echo "No local PostgreSQL $$version cluster. Create one (e.g. sudo pg_createcluster $$version main --port $$((5416 + version)) --start)."; \
	    fi; \
	  done; \
	else \
	  echo "pg_ctlcluster is not available. Start isolated local databases, for example:"; \
	  echo "  docker run -d --name suv-pg16 -p 127.0.0.1:5432:5432 -e POSTGRES_USER=suv -e POSTGRES_PASSWORD=suv postgres:16"; \
	  echo "  docker run -d --name suv-pg17 -p 127.0.0.1:5433:5432 -e POSTGRES_USER=suv -e POSTGRES_PASSWORD=suv postgres:17"; \
	  echo "  or a full local Supabase stack: supabase start (supabase/config.toml)"; \
	fi
	@echo "These are local development/test clusters only; nothing here touches a production database."

db-migrate-local: ## Create/migrate the LOCAL development database (prints the target first)
	@[[ "$(LOCAL_DB)" =~ ^[a-z_][a-z0-9_]{0,62}$$ ]] || { echo "refusing: LOCAL_DB must be a plain database name"; exit 3; }
	@LOCAL_ADMIN_URL="$(LOCAL_ADMIN_URL)" $(LOCAL_GUARD) LOCAL_ADMIN_URL
	@LOCAL_DATABASE_URL="$(LOCAL_DATABASE_URL)" $(LOCAL_GUARD) LOCAL_DATABASE_URL
	@echo "Local target database: $(LOCAL_DB) (loopback only)"
	@if [ "$$(psql "$(LOCAL_ADMIN_URL)" -XAtc "select 1 from pg_database where datname = '$(LOCAL_DB)'")" != "1" ]; then \
	  psql "$(LOCAL_ADMIN_URL)" -Xq -v ON_ERROR_STOP=1 -c 'create database "$(LOCAL_DB)"'; \
	  echo "Created local database $(LOCAL_DB)."; fi
	@if [ "$$(psql "$(LOCAL_DATABASE_URL)" -XAtc "select exists (select 1 from pg_namespace where nspname = 'auth')")" != "t" ]; then \
	  echo "Plain PostgreSQL: applying the TEST-ONLY Supabase emulation (supabase/tests/supabase_emulation.sql)."; \
	  psql "$(LOCAL_DATABASE_URL)" -Xq -v ON_ERROR_STOP=1 -f supabase/tests/supabase_emulation.sql; fi
	@DATABASE_URL="$(LOCAL_DATABASE_URL)" $(CLI) --no-env-file db migrate --local-only --yes

test-db: ## Database tests on PostgreSQL 16 AND 17 (Supabase-like migrator role on 17)
	@echo "== database tests on PostgreSQL 16 (PG16_ADMIN_URL)"
	@TEST_DATABASE_ADMIN_URL="$(PG16_ADMIN_URL)" $(PYTEST) -q -m "db and $(NOT_LIVE)"
	@echo "== database tests on PostgreSQL 17 (PG17_ADMIN_URL, migrator role $(PG17_MIGRATOR_ROLE))"
	@TEST_DATABASE_ADMIN_URL="$(PG17_ADMIN_URL)" TEST_DATABASE_MIGRATOR_ROLE=$(PG17_MIGRATOR_ROLE) \
	  $(PYTEST) -q -m "db and $(NOT_LIVE)"

test-integration: ## Integration, API, MCP and CLI tests against the local PostgreSQL 16 cluster
	@TEST_DATABASE_ADMIN_URL="$(PG16_ADMIN_URL)" $(PYTEST) -q -m "$(NOT_LIVE)" \
	  tests/integration tests/api tests/mcp tests/cli

dev-fixtures: ## Register the SYNTHETIC fixture sources in the LOCAL database (they stay gated)
	@LOCAL_DATABASE_URL="$(LOCAL_DATABASE_URL)" $(LOCAL_GUARD) LOCAL_DATABASE_URL
	@$(DEV_ENV) $(CLI) sources sync --fixture-sources --yes

dev: dev-fixtures ## api + worker + scheduler on the local database, network and notifications OFF
	@echo "api on http://127.0.0.1:$(DEV_API_PORT); SOURCE_NETWORK_ENABLED=false; ALLOW_EXTERNAL_NOTIFICATIONS=false"
	@$(DEV_ENV) bash -c 'trap "kill 0" EXIT INT TERM; \
	  $(CLI) api serve --host 127.0.0.1 --port $(DEV_API_PORT) & \
	  $(CLI) worker & \
	  $(CLI) scheduler & \
	  wait'

smoke-local: ## Offline smoke: CLI, configuration, tax rules, fixture E2E pipeline (no network)
	$(CLI) --no-env-file --help >/dev/null
	$(CLI) --no-env-file config validate
	$(CLI) --no-env-file tax-rules validate config/tax_rules
	$(CLI) --no-env-file tax-rules validate tests/fixtures/tax/synthetic_rule_set.json
	@TEST_DATABASE_ADMIN_URL="$(PG16_ADMIN_URL)" $(PYTEST) -q \
	  tests/integration/pipeline/test_e2e_fixture_pipeline.py tests/cli/test_crawl_once.py

# --------------------------------------------------------------------------------------------
# Quality gates
# --------------------------------------------------------------------------------------------

lint: ## ruff check + format check
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts

format: ## Apply ruff formatting
	$(UV) run ruff format src tests scripts

typecheck: ## mypy --strict over src/suv_deals
	$(UV) run mypy

test: ## Every non-live test on the default PostgreSQL (16)
	@TEST_DATABASE_ADMIN_URL="$(PG16_ADMIN_URL)" $(PYTEST) -q -m "$(NOT_LIVE)"

test-all: lint typecheck schemas-check test-unit test-db outlook-bridge-test dashboard-test ## All gates

schemas: ## Re-export the JSON schemas (schemas/)
	$(UV) run python scripts/export_schemas.py

schemas-check: ## Fail when a committed schema snapshot is stale
	$(UV) run python scripts/export_schemas.py --check

dashboard-install: ## npm ci in dashboard/ (skipped while dashboard/ has no package.json)
	@if [ -f dashboard/package.json ]; then npm --prefix dashboard ci; else echo "dashboard/package.json not present; skipping"; fi

dashboard-build: ## Build the dashboard
	@if [ -f dashboard/package.json ]; then npm --prefix dashboard run build; else echo "dashboard/package.json not present; skipping"; fi

dashboard-test: ## Dashboard unit tests
	@if [ -f dashboard/package.json ]; then npm --prefix dashboard test; else echo "dashboard/package.json not present; skipping"; fi

dashboard-lint: ## Dashboard lint
	@if [ -f dashboard/package.json ]; then npm --prefix dashboard run lint; else echo "dashboard/package.json not present; skipping"; fi

outlook-bridge-test: ## Local Outlook bridge tests (in-memory fakes; no Outlook needed)
	@if [ -d desktop/outlook-bridge/tests ]; then $(PYTEST) desktop/outlook-bridge/tests -q; \
	else echo "desktop/outlook-bridge/tests not present; skipping"; fi

verify-release: ## Release verification report (lint, types, tests, schemas, migrations, commit)
	scripts/verify_release.sh

backup: ## pg_dump app/ops schemas of BACKUP_DATABASE_URL into var/backups (see docs/runbook.md)
	scripts/backup.sh

restore-check: ## Restore BACKUP into an ISOLATED local database and verify it (see docs/runbook.md)
	scripts/restore_check.sh "$(BACKUP)"

# --------------------------------------------------------------------------------------------
# Destructive (separately named; never called by any other target)
# --------------------------------------------------------------------------------------------

db-reset-local: ## DROP and recreate the LOCAL dev database (needs CONFIRM_DB_RESET=yes-drop-<db>)
ifneq ($(CONFIRM_DB_RESET),yes-drop-$(LOCAL_DB))
	$(error refusing: set CONFIRM_DB_RESET=yes-drop-$(LOCAL_DB) to drop the LOCAL database $(LOCAL_DB))
endif
	@case "$(LOCAL_DB)" in suv_dev|suv_dev_*) ;; *) echo "refusing: only suv_dev* databases can be reset"; exit 3;; esac
	@[[ "$(LOCAL_DB)" =~ ^[a-z_][a-z0-9_]{0,62}$$ ]] || { echo "refusing: LOCAL_DB must be a plain database name"; exit 3; }
	@LOCAL_ADMIN_URL="$(LOCAL_ADMIN_URL)" $(LOCAL_GUARD) LOCAL_ADMIN_URL
	@psql "$(LOCAL_ADMIN_URL)" -Xq -v ON_ERROR_STOP=1 -c 'drop database if exists "$(LOCAL_DB)" with (force)'
	@echo "Dropped $(LOCAL_DB). Recreate it with: make db-migrate-local"
