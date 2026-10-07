"""Makefile, container files and operational scripts (spec 27, 29, 34).

- ``make -n`` smoke for the spec 27 targets; the destructive reset is guarded;
- Dockerfile / compose files parse, contain no secret values, bind ports to 127.0.0.1 only, give
  the crawler no database/Supabase credentials and never mount the Docker socket or a home dir;
- scripts: help/syntax, the restore check refuses a non-local target, and (marker ``db``) a real
  backup -> isolated restore -> verification round trip on SYNTHETIC data.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from tests.db_harness import admin_url, create_migrated_database, drop_database
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import add_revision

from suv_deals.domain.enums import Drive, Fuel, Gearbox, Precision
from suv_deals.domain.listings import NormalizedListing, PartialDate, PriceInfo, VehicleSpec

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"sb_secret_[A-Za-z0-9]",
        r"xox[abp]-[0-9A-Za-z]",
        r"whsec_[A-Za-z0-9+/=]{8,}",
        r"eyJ[A-Za-z0-9_-]{10,}\.",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"postgres(?:ql)?://[^:\s/]+:[^@\s$]+@(?!127\.0\.0\.1|localhost)",
        r"(?i)(password|secret|token|api_key)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{12,}",
    )
]

pytestmark_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def make_n(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-n", "-C", str(REPO), *args], capture_output=True, text=True, timeout=60, check=False
    )


# --------------------------------------------------------------------------------------------
# Makefile
# --------------------------------------------------------------------------------------------


@pytestmark_make
@pytest.mark.parametrize(
    "target",
    [
        "doctor",
        "install",
        "test-unit",
        "db-local-start",
        "db-migrate-local",
        "test-db",
        "test-integration",
        "dev",
        "smoke-local",
        "lint",
        "typecheck",
        "test",
        "test-all",
        "schemas",
        "dashboard-test",
        "outlook-bridge-test",
    ],
)
def test_make_targets_exist(target: str) -> None:
    result = make_n(target)
    assert result.returncode == 0, result.stderr


@pytestmark_make
def test_make_key_targets_run_the_documented_commands() -> None:
    assert "uv sync --frozen" in make_n("install").stdout
    assert "suv-deals doctor" in make_n("doctor").stdout
    test_db = make_n("test-db").stdout
    assert "5432" in test_db and "5433" in test_db and "TEST_DATABASE_MIGRATOR_ROLE=suv_migrator" in test_db
    migrate_local = make_n("db-migrate-local").stdout
    assert "--local-only --yes" in migrate_local and "--no-env-file" in migrate_local
    dev = make_n("dev").stdout
    assert "SOURCE_NETWORK_ENABLED=false" in dev and "ALLOW_EXTERNAL_NOTIFICATIONS=false" in dev
    assert "api serve --host 127.0.0.1" in dev
    assert "desktop/outlook-bridge/tests" in make_n("outlook-bridge-test").stdout


_GUARD = "db target --local-only --url-env"


@pytestmark_make
def test_make_local_targets_run_the_libpq_aware_guard_first() -> None:
    for target, variables in (
        ("db-migrate-local", ("LOCAL_ADMIN_URL", "LOCAL_DATABASE_URL")),
        ("dev-fixtures", ("LOCAL_DATABASE_URL",)),
        ("db-reset-local", ("LOCAL_ADMIN_URL",)),
    ):
        lines = make_n(target, "CONFIRM_DB_RESET=yes-drop-suv_dev").stdout.splitlines()
        guard_lines = [i for i, line in enumerate(lines) if _GUARD in line]
        assert {lines[i].rsplit(" ", 1)[-1] for i in guard_lines} == set(variables), target
        touching = [i for i, line in enumerate(lines) if "psql " in line or "sources sync" in line]
        assert touching and max(guard_lines) < min(touching), target


@pytestmark_make
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
@pytest.mark.parametrize(
    ("target", "variable", "url"),
    [
        (
            "db-migrate-local",
            "LOCAL_ADMIN_URL",
            "postgresql://suv:suv@127.0.0.1:5432/postgres?host=db.example.invalid",
        ),
        (
            "db-migrate-local",
            "LOCAL_DATABASE_URL",
            "postgresql://suv:suv@127.0.0.1:5432/suv_dev?hostaddr=10.9.8.7",
        ),
        (
            "db-reset-local",
            "LOCAL_ADMIN_URL",
            "postgresql://suv:suv@127.0.0.1:5432,db.example.invalid:5432/postgres",
        ),
        ("dev-fixtures", "LOCAL_DATABASE_URL", "postgresql://suv:suv@localhost/suv_dev?service=production"),
    ],
)
def test_make_local_targets_refuse_urls_that_leave_the_machine(target: str, variable: str, url: str) -> None:
    """Really runs the target: the guard must stop it before psql or the CLI touches anything."""
    result = subprocess.run(
        ["make", "-C", str(REPO), target, f"{variable}={url}", "CONFIRM_DB_RESET=yes-drop-suv_dev"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode != 0
    assert f"{variable} is not a loopback address or a local socket" in result.stderr
    assert "Created local database" not in result.stdout and "Dropped" not in result.stdout


@pytestmark_make
def test_destructive_reset_is_guarded_and_never_called_by_other_targets() -> None:
    refused = make_n("db-reset-local")
    assert refused.returncode != 0
    assert "CONFIRM_DB_RESET" in refused.stderr
    allowed = make_n("db-reset-local", "CONFIRM_DB_RESET=yes-drop-suv_dev")
    assert allowed.returncode == 0
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    for line in makefile.splitlines():
        if re.match(r"^[a-zA-Z0-9_-]+:", line) and not line.startswith("db-reset-local:"):
            assert "db-reset-local" not in line.split("##")[0], line
    for target in ("test-all", "dev", "smoke-local", "db-migrate-local"):
        assert "drop database" not in make_n(target).stdout


# --------------------------------------------------------------------------------------------
# Container files
# --------------------------------------------------------------------------------------------


def _compose(name: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _no_secrets(text: str, name: str) -> None:
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), (name, pattern.pattern)


@pytest.mark.parametrize("name", ["compose.yaml", "compose.production.yaml"])
def test_compose_files_are_safe(name: str) -> None:
    text = (REPO / name).read_text(encoding="utf-8")
    _no_secrets(text, name)
    data = _compose(name)
    services: dict[str, Any] = data["services"]
    assert {"api", "worker", "scheduler", "dispatcher", "crawl4ai"} <= set(services)
    images = {s.get("image") for k, s in services.items() if k != "crawl4ai"}
    assert len(images) == 1  # the same application image, different commands
    for service_name, service in services.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, service_name, port)
        for volume in service.get("volumes", []):
            source = str(volume.get("source") if isinstance(volume, dict) else volume).split(":")[0]
            assert "docker.sock" not in source and not source.startswith(("~", "/home", "/root")), volume
        assert not service.get("privileged", False)
        assert "no-new-privileges:true" in service.get("security_opt", [])
        assert service.get("network_mode") != "host"
        image = str(service.get("image", ""))
        assert not image.endswith(":latest") and "latest" not in image.rsplit(":", maxsplit=1)[-1]
    crawler = services["crawl4ai"]
    assert crawler["image"].startswith("unclecode/crawl4ai:0.9.4")
    assert crawler["shm_size"] == "1g"
    assert crawler["deploy"]["resources"]["limits"]["memory"]
    crawler_env = crawler.get("environment", {}) or {}
    assert set(crawler_env) <= {"CRAWL4AI_API_TOKEN"}
    assert "env_file" not in crawler
    assert "app" not in crawler["networks"]
    assert data["networks"]["crawler"]["internal"] is True
    for service_name in ("api", "scheduler", "dispatcher"):
        assert "crawler" not in services[service_name].get("networks", ["app"]), service_name
    assert "crawler" in services["worker"]["networks"]


#: Application switches; Compose must never interpolate them from these names, because it also
#: reads the project .env (the application's own configuration file) for interpolation.
_SWITCHES = (
    "SOURCE_NETWORK_ENABLED",
    "ALLOW_EXTERNAL_NOTIFICATIONS",
    "EVENT_BRIDGE_ENABLED",
    "MCP_EVENTS_ENABLED",
    "NOTIFICATION_PROVIDER",
    "FX_FETCH_ENABLED",
    "SELLER_INQUIRY_MODE",
)


def test_compose_switches_default_off() -> None:
    for name in ("compose.yaml", "compose.production.yaml"):
        text = (REPO / name).read_text(encoding="utf-8")
        for switch in _SWITCHES:
            assert "${" + switch not in text, (name, switch)
        api_env = _compose(name)["services"]["api"]["environment"]
        assert api_env["SOURCE_NETWORK_ENABLED"] == "${SUV_DEALS_ENABLE_SOURCE_NETWORK:-false}"
        assert api_env["ALLOW_EXTERNAL_NOTIFICATIONS"] == "${SUV_DEALS_ENABLE_EXTERNAL_NOTIFICATIONS:-false}"
    production = _compose("compose.production.yaml")
    env = production["services"]["worker"]["environment"]
    assert env["APP_ENV"] == "production"
    assert env["EVENT_BRIDGE_ENABLED"] == "${SUV_DEALS_ENABLE_EVENT_BRIDGE:-false}"
    assert env["MCP_EVENTS_ENABLED"] == "${SUV_DEALS_ENABLE_MCP_EVENTS:-false}"
    assert env["NOTIFICATION_PROVIDER"] == "${SUV_DEALS_NOTIFICATION_PROVIDER:-disabled}"
    assert env["SELLER_INQUIRY_MODE"] == "${SUV_DEALS_SELLER_INQUIRY_MODE:-disabled_until_sender_ready}"
    crawler = production["services"]["crawl4ai"]
    assert "@${CRAWL4AI_IMAGE_DIGEST:?" in crawler["image"]  # refuses to run without the tested digest
    assert crawler["secrets"][0]["target"] == "api_token"
    assert "ports" not in crawler


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose is not installed")
def test_project_env_file_cannot_enable_compose_switches(tmp_path: Path) -> None:
    """Compose reads ./.env for ${...} interpolation; the application's switches there are inert."""
    if subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0:
        pytest.skip("docker compose is not available")
    shutil.copy(REPO / "compose.yaml", tmp_path / "compose.yaml")
    (tmp_path / ".env").write_text(
        "".join(f"{s}=true\n" for s in _SWITCHES[:2]) + "CRAWL4AI_API_TOKEN=fake-token-for-config\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SUV_DEALS_", *_SWITCHES))}

    def rendered(extra: dict[str, str]) -> dict[str, Any]:
        result = subprocess.run(
            ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
            cwd=tmp_path,
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        services: dict[str, Any] = yaml.safe_load(result.stdout)["services"]
        return services

    for service in ("api", "worker", "scheduler"):
        environment = rendered({})[service]["environment"]
        assert environment["SOURCE_NETWORK_ENABLED"] == "false"
        assert environment["ALLOW_EXTERNAL_NOTIFICATIONS"] == "false"
    enabled = rendered({"SUV_DEALS_ENABLE_SOURCE_NETWORK": "true"})["worker"]["environment"]
    assert enabled["SOURCE_NETWORK_ENABLED"] == "true"  # only the explicit, dedicated switch


def test_worker_evidence_volume_exists_for_the_read_only_root() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p /app/var/snapshots && chown -R app:app /app/var" in dockerfile
    for name in ("compose.yaml", "compose.production.yaml"):
        data = _compose(name)
        worker = data["services"]["worker"]
        assert worker["read_only"] is True
        assert "snapshots:/app/var/snapshots" in worker["volumes"]
        assert "snapshots" in data["volumes"]


def test_dockerfile_and_dockerignore() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    _no_secrets(dockerfile, "Dockerfile")
    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.13-slim$", dockerfile, re.M)
    assert "ghcr.io/astral-sh/uv" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert re.search(r"^USER app:app$", dockerfile, re.M)
    assert "HEALTHCHECK" in dockerfile and "/healthz" in dockerfile
    for line in dockerfile.splitlines():
        if line.startswith(("ENV", "ARG")) or line.startswith("    "):
            assert not re.search(r"(SECRET|TOKEN|PASSWORD|DATABASE_URL|API_KEY)\w*=", line), line
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert copies and all(not line.startswith("COPY . ") and ".env" not in line for line in copies)
    ignore = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", ".env.*", ".git", "var/", "tests/", "*.pem", "*.key"):
        assert entry in ignore, entry


# --------------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script", ["doctor.sh", "backup.sh", "restore_check.sh", "verify_release.sh", "migrate.sh", "rollback.sh"]
)
def test_shell_scripts_parse(script: str) -> None:
    path = SCRIPTS / script
    assert os.access(path, os.X_OK), f"{script} is not executable"
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("script", ["backup.sh", "restore_check.sh", "verify_release.sh"])
def test_scripts_have_help(script: str) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPTS / script), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "Usage" in result.stdout or "usage" in result.stdout.lower()


def test_verify_release_plan_runs_nothing() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPTS / "verify_release.sh"), "--plan", "--skip-db"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert "tests_db_pg17: NOT RUN (--skip-db)" in result.stdout
    assert "lint" in result.stdout and "schemas" in result.stdout


def test_verify_release_records_the_spec_29_release_facts(tmp_path: Path) -> None:
    """The embedded Python block of verify_release.sh runs and records config/adapter/MCP facts."""
    text = (SCRIPTS / "verify_release.sh").read_text(encoding="utf-8")
    match = re.search(r"uv run --frozen python - <<'EOF'.*?\n(.*?)\nEOF\n", text, re.S)
    assert match is not None
    script = tmp_path / "facts.py"
    script.write_text(match.group(1), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)], cwd=REPO, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"^  config_yaml_sha256=[0-9a-f]{64}$", result.stdout, re.M)
    assert "mcp_sdk=2.3.0 mcp_protocol=2026-07-28" in result.stdout
    assert re.search(
        r"^  source autoscout24_de adapter=\S+@\S+ mode=\S+ enabled=(true|false)$", result.stdout, re.M
    )


def test_backup_requires_a_url() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("BACKUP_DATABASE_URL", "DATABASE_URL")}
    result = subprocess.run(
        ["bash", str(SCRIPTS / "backup.sh")], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "BACKUP_DATABASE_URL" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:FAKE-pw@db.example.invalid:5432/postgres",
        # A hostname check on the URL text would accept these; libpq would not stay local.
        "postgresql://u:FAKE-pw@127.0.0.1:5432/postgres?host=db.example.invalid",
        "postgresql://u:FAKE-pw@127.0.0.1:5432/postgres?hostaddr=10.9.8.7",
        "postgresql://u:FAKE-pw@127.0.0.1:5432,db.example.invalid:5432/postgres",
    ],
)
def test_restore_check_refuses_a_non_local_target(tmp_path: Path, url: str) -> None:
    manifest = tmp_path / "suv-deals_x.manifest"
    manifest.write_text("format=suv-deals-backup/1\n", encoding="utf-8")
    env = {**os.environ, "RESTORE_ADMIN_URL": url}
    result = subprocess.run(
        ["bash", str(SCRIPTS / "restore_check.sh"), str(manifest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "loopback" in result.stderr
    assert "FAKE-pw" not in result.stdout + result.stderr


def _fake_pg_tools(tmp_path: Path, log: Path) -> dict[str, str]:
    """psql/pg_dump stand-ins that record their arguments and whether PGPASSWORD was set."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("psql", "pg_dump"):
        script = bin_dir / tool
        script.write_text(
            "#!/usr/bin/env bash\n"
            'args="$*"\n'
            "args=\"${args//$'\\n'/ }\"\n"
            f'printf "%s argv=%s pgpassword=%s\\n" "{tool}" "$args" "${{PGPASSWORD:-}}" >> "{log}"\n'
            "exit 2\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_backup_never_passes_the_password_as_an_argument(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    env = _fake_pg_tools(tmp_path, log)
    env.pop("DATABASE_URL", None)
    env["BACKUP_DATABASE_URL"] = (
        "postgresql://owner:FAKE%2Fsecret-81@db.example.invalid:5432/prod?sslmode=require"
    )
    result = subprocess.run(
        ["bash", str(SCRIPTS / "backup.sh"), "--output-dir", str(tmp_path / "out")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # The fake psql fails: the script stops at once instead of continuing with empty answers.
    assert result.returncode == 1
    assert "cannot connect to the backup source" in result.stderr
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls and all(line.startswith("psql ") for line in calls)
    for line in calls:
        argv, _, pgpassword = line.partition(" pgpassword=")
        assert "FAKE" not in argv and "secret" not in argv
        assert "postgresql://owner@db.example.invalid:5432/prod?sslmode=require" in argv
        assert pgpassword == "FAKE/secret-81"
    assert "FAKE" not in result.stdout + result.stderr


def test_bootstrap_owner_wrapper_help() -> None:
    result = subprocess.run(
        ["uv", "run", "--frozen", "python", str(SCRIPTS / "bootstrap_owner.py"), "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--workspace-name" in result.stdout


# --------------------------------------------------------------------------------------------
# Backup -> isolated restore -> verification (marker db)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def source_database(db_url: str) -> Iterator[str]:
    """A separate migrated database with SYNTHETIC rows (never the shared test database)."""
    del db_url  # ensures PostgreSQL is reachable (skips otherwise)
    name, url = create_migrated_database("suv_test_backup")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            seed = Seed(conn)
            ws = seed.workspace("Backup source")
            user = seed.user()
            seed.membership(ws, user, "owner")
            source = seed.source(ws)
            listing = seed.listing(ws, source)
            normalized = NormalizedListing(
                source_key="fixture_dealer_de",
                source_listing_id="SYN-BACKUP-1",
                canonical_url="https://dealer.example/fahrzeug/SYN-BACKUP-1",
                observed_at=datetime(2026, 10, 6, 10, 0, tzinfo=UTC),
                vehicle=VehicleSpec(
                    make="Example",
                    model="Trail",
                    first_registration=PartialDate(value="2011-05", precision=Precision.MONTH),
                    fuel=Fuel.DIESEL,
                    gearbox=Gearbox.MANUAL,
                    drive=Drive.AWD,
                    mileage_km=Decimal("187500"),
                ),
                price=PriceInfo(amount_minor=275000, currency="EUR"),
                parser_version="fixture@1.0.0",
            )
            add_revision(
                seed,
                ws,
                listing,
                1,
                normalized=normalized.model_dump(mode="json"),
                semantic_hash=normalized.semantic_hash(),
            )
        yield url
    finally:
        drop_database(name)


def _restore_databases() -> set[str]:
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        rows = admin.execute(
            "select datname from pg_database where datname like 'suv_restore_check_%'"
        ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.db
@pytest.mark.skipif(shutil.which("pg_dump") is None or shutil.which("psql") is None, reason="needs pg tools")
def test_backup_and_isolated_restore_round_trip(source_database: str, tmp_path: Path) -> None:
    env = {**os.environ, "BACKUP_DATABASE_URL": source_database}
    env.pop("DATABASE_URL", None)
    backup = subprocess.run(
        ["bash", str(SCRIPTS / "backup.sh"), "--output-dir", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert backup.returncode == 0, backup.stderr
    password = str(psycopg.conninfo.conninfo_to_dict(source_database).get("password") or "")
    manifests = list(tmp_path.glob("suv-deals_*.manifest"))
    assert len(manifests) == 1
    manifest = manifests[0].read_text(encoding="utf-8")
    assert "count.app.listing_revisions=1" in manifest
    assert "count.app.memberships=1" in manifest
    assert "workspaces.active_without_owner=0" in manifest
    members = next(tmp_path.glob("suv-deals_*.members")).read_text(encoding="utf-8")
    assert "@" not in members  # ids only, never e-mail addresses
    assert oct(next(tmp_path.glob("suv-deals_*.dump")).stat().st_mode & 0o777) == "0o600"

    before = _restore_databases()
    restore_env = {**os.environ, "RESTORE_ADMIN_URL": admin_url()}
    restore = subprocess.run(
        ["bash", str(SCRIPTS / "restore_check.sh"), str(manifests[0])],
        env=restore_env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert restore.returncode == 0, restore.stdout + restore.stderr
    out = restore.stdout
    assert "Result: PASS" in out
    assert "PASS  evidence_hashes" in out and "revisions: 1 ok" in out
    assert "PASS  count.app.listing_revisions: restored 1, manifest 1" in out
    assert "elapsed:" in out
    if len(password) >= 6:
        assert password not in out + restore.stderr + backup.stdout + backup.stderr
    report = next(tmp_path.glob("suv-deals_*.restore_report.txt")).read_text(encoding="utf-8")
    assert "result=PASS" in report and "elapsed_seconds=" in report
    assert _restore_databases() == before  # the isolated database was dropped again

    # A tampered dump is refused before anything is restored.
    dump = next(tmp_path.glob("suv-deals_*.dump"))
    dump.write_bytes(dump.read_bytes() + b"tampered")
    tampered = subprocess.run(
        ["bash", str(SCRIPTS / "restore_check.sh"), str(manifests[0])],
        env=restore_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert tampered.returncode == 1
    assert "FAIL  dump_sha256" in tampered.stdout
    assert _restore_databases() == before
