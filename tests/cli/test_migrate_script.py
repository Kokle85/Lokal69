"""scripts/migrate.sh never passes the database password on a command line (spec 29).

A fake ``psql`` on ``PATH`` records every argument vector and the ``PGPASSWORD`` it received
(JSON lines) and answers the script's few queries, so no database is needed. The password must
travel only in ``PGPASSWORD``; psql gets the same connection string without it. SYNTHETIC
credentials and ``.invalid`` hosts only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "scripts" / "migrate.sh"
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

_FAKE_PSQL = """\
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_PSQL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": args, "pgpassword": os.environ.get("PGPASSWORD"),
                          "database_url": os.environ.get("DATABASE_URL")}) + "\\n")
sql = " ".join(args)
if "current_database()" in sql:
    print("suv_synthetic|192.0.2.10|5432|suv_migrator|17.0")
elif "to_regclass" in sql:
    print("f")
elif "pg_namespace" in sql:
    print("f")
"""


def _fake_psql(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "psql"
    script.write_text(f"#!{sys.executable}\n{_FAKE_PSQL}", encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "psql.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.pop("DATABASE_URL", None)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["FAKE_PSQL_LOG"] = str(log)
    return env, log


def _migrate(
    tmp_path: Path, url: str, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    env, log = _fake_psql(tmp_path)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        ["bash", str(MIGRATE), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    )
    return result, calls


@pytest.mark.parametrize(
    ("url", "secret", "conninfo"),
    [
        (
            "postgresql://suv_migrator:FAKE%2Fsecret%40pw-81@db.example.invalid:5432/suvdb?sslmode=require",
            "FAKE/secret@pw-81",
            "postgresql://suv_migrator@db.example.invalid:5432/suvdb?sslmode=require",
        ),
        (
            "postgres://suv_migrator@db.example.invalid/suvdb?password=FAKE-query%26pw&sslmode=require",
            "FAKE-query&pw",
            "postgres://suv_migrator@db.example.invalid/suvdb?sslmode=require",
        ),
        (
            r"host=db.example.invalid port=5432 dbname=suvdb user=suv_migrator password='FAKE it\'s spaced' "
            "sslmode=require",
            "FAKE it's spaced",
            "host=db.example.invalid port=5432 dbname=suvdb user=suv_migrator sslmode=require",
        ),
        (
            r"host=db.example.invalid password=FAKE\ unquoted dbname=suvdb",
            "FAKE unquoted",
            "host=db.example.invalid dbname=suvdb",
        ),
    ],
    ids=["url-userinfo", "url-query", "kv-quoted", "kv-unquoted"],
)
def test_migrate_passes_the_password_only_in_pgpassword(
    tmp_path: Path, url: str, secret: str, conninfo: str
) -> None:
    result, calls = _migrate(tmp_path, url, "--yes")
    assert result.returncode == 0, result.stderr
    assert f"Applied {len(MIGRATIONS)} migration(s) to suv_synthetic." in result.stdout
    # target, ledger, schemas, ledger table, then one call per migration
    assert len(calls) == 4 + len(MIGRATIONS)
    for call in calls:
        argv = call["argv"]
        assert argv[0] == conninfo
        assert all("FAKE" not in arg for arg in argv)
        assert call["pgpassword"] == secret
        assert call["database_url"] is None  # the full URL is not exported to psql either
    assert "FAKE" not in result.stdout + result.stderr


def test_migrate_without_a_password_leaves_pgpassword_unset(tmp_path: Path) -> None:
    result, calls = _migrate(tmp_path, "postgresql://suv_migrator@db.example.invalid/suvdb", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Dry run: nothing applied." in result.stdout
    assert calls and all(c["pgpassword"] is None for c in calls)
    assert {c["argv"][0] for c in calls} == {"postgresql://suv_migrator@db.example.invalid/suvdb"}
    bare, bare_calls = _migrate(tmp_path / "bare", "suvdb", "--dry-run")
    assert bare.returncode == 0, bare.stderr
    assert {c["argv"][0] for c in bare_calls} == {"suvdb"}


def test_migrate_refuses_an_unparseable_connection_string(tmp_path: Path) -> None:
    result, calls = _migrate(tmp_path, "host=db.example.invalid password='FAKE-unterminated", "--dry-run")
    assert result.returncode == 2
    assert "cannot be parsed" in result.stderr
    assert calls == []  # psql never ran
    assert "FAKE" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "POSTGRESQL://suv_migrator:FAKE-upper@db.example.invalid/suvdb",  # libpq: not a URI -> dbname
        "postgresql://suv_migrator:FAKE/slash@db.example.invalid/suvdb",  # '/' ends the authority
        "postgresql://suv_migrator:FAKE?mark@db.example.invalid/suvdb",  # '?' starts the query
        "postgresql://suv_migrator:FAKE#hash@db.example.invalid/suvdb",  # '#' starts a fragment
        "postgresql://suv_migrator:1234/FAKE@db.example.invalid/suvdb",  # digits look like a port
        "postgresql://suv_migrator@db.example.invalid/suvdb?PASSWORD=FAKE-case",
        "host=db.example.invalid PASSWORD=FAKE-case dbname=suvdb",
        "suv_migrator:FAKE-noscheme@db.example.invalid/suvdb",
        "mysql://suv_migrator:FAKE-scheme@db.example.invalid/suvdb",
    ],
    ids=[
        "upper-scheme",
        "slash",
        "question",
        "hash",
        "digits-slash",
        "url-key-case",
        "kv-key-case",
        "no-scheme",
        "other-scheme",
    ],
)
def test_migrate_refuses_strings_that_could_leak_the_password_into_argv(tmp_path: Path, url: str) -> None:
    """A malformed string is refused BEFORE psql runs instead of being passed through as a
    "database name" or a URL whose password spilled into the host, path or query."""
    result, calls = _migrate(tmp_path, url, "--dry-run")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot be parsed" in result.stderr
    assert calls == []  # psql never ran
    assert "FAKE" not in result.stdout + result.stderr


def test_migrate_accepts_encoded_passwords_multi_host_and_socket_urls(tmp_path: Path) -> None:
    result, calls = _migrate(
        tmp_path,
        "postgresql://suv_migrator:FAKE%2Fok%23%3F@[::1]:5432,db2.example.invalid:6543/suvdb?sslmode=require",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert {c["argv"][0] for c in calls} == {
        "postgresql://suv_migrator@[::1]:5432,db2.example.invalid:6543/suvdb?sslmode=require"
    }
    assert {c["pgpassword"] for c in calls} == {"FAKE/ok#?"}
    socket, socket_calls = _migrate(
        tmp_path / "socket", "postgresql://%2Fvar%2Frun%2Fpostgresql/suvdb", "--dry-run"
    )
    assert socket.returncode == 0, socket.stderr
    assert {c["argv"][0] for c in socket_calls} == {"postgresql://%2Fvar%2Frun%2Fpostgresql/suvdb"}
