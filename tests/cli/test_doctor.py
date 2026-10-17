"""``suv-deals doctor``: presence-only report; never prints values; read-only (spec 26, 27).

All secrets below are FAKE placeholders created for these tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from tests.cli.conftest import Cli

from suv_deals.cli_commands import doctor as doctor_module

FAKE_SECRETS = {
    "DATABASE_URL": "postgresql://fakeuser:FAKE-db-pass-4711@db-fake.invalid:5432/fakedb",
    "SUPABASE_SECRET_KEY": "sb_secret_FAKEFAKEFAKEFAKEFAKE0001",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_FAKEPUBLISHABLE0002",
    "CRAWL4AI_API_TOKEN": "fake-crawler-token-0003-zz",
    "MCP_OAUTH_CLIENT_SECRET": "fake-oauth-client-secret-0004",
    "MCP_CURSOR_SIGNING_SECRET": "fake-cursor-signing-secret-0005",
    "SLACK_BOT_TOKEN": "xoxb-0000-FAKE-0006",
    "SLACK_SIGNING_SECRET": "fake-slack-signing-0007",
    "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": "RkFLRUZBS0VGQUtFRkFLRUZBS0VGQUtFRkFLRUZBS0U=",
    "LLM_API_KEY": "sk-fake-llm-key-0008",
    "MOBILE_DE_API_CREDENTIALS": "fake-mobile-credentials-0009",
}
FAKE_URLS = {
    "SUPABASE_URL": "https://fake-project-0010.supabase.invalid",
    "MCP_PUBLIC_URL": "https://deals-fake-0011.example.invalid/mcp",
    "MCP_OAUTH_ISSUER": "https://auth-fake-0012.example.invalid",
    "MCP_OAUTH_JWKS_URL": "https://auth-fake-0012.example.invalid/.well-known/jwks.json",
    "CRAWL4AI_BASE_URL": "http://crawler-fake-0013.invalid:11235",
}
NEEDLES = [
    "FAKE-db-pass-4711",
    "db-fake.invalid",
    "fakedb",
    "FAKEFAKEFAKE",
    "FAKEPUBLISHABLE",
    "fake-crawler-token",
    "fake-oauth-client-secret",
    "fake-cursor-signing",
    "xoxb-0000",
    "fake-slack-signing",
    "RkFLRUZBS0",
    "sk-fake-llm",
    "fake-mobile-credentials",
    "fake-project-0010",
    "deals-fake-0011",
    "auth-fake-0012",
    "crawler-fake-0013",
]


def _env(**extra: str) -> dict[str, str]:
    return {**FAKE_SECRETS, **FAKE_URLS, "DATABASE_SET_ROLE": "suv_backend", **extra}


def test_doctor_never_prints_secret_values(run_cli: Cli) -> None:
    for args in (("doctor", "--no-db"), ("doctor", "--no-db", "--json"), ("doctor", "--no-db", "-v")):
        result = run_cli(*args, env=_env())
        for needle in NEEDLES:
            assert needle not in result.output, (args, needle)
        assert "set" in result.output


def test_doctor_reports_presence_and_required_per_process(run_cli: Cli) -> None:
    result = run_cli("doctor", "--no-db", "--json", env=_env())
    data = json.loads(result.output)
    findings = data["findings"]
    by_name = {(f["area"], f["name"], f["detail"].split(":")[0]): f for f in findings}
    assert by_name[("config", "api", "DATABASE_URL")]["status"] == "ok"
    assert by_name[("config", "worker", "DATABASE_URL")]["status"] == "ok"
    # Conditional requirements: the crawler token is required by the worker only when the
    # source network is enabled.
    assert ("config", "worker", "CRAWL4AI_API_TOKEN") not in by_name
    networked = json.loads(
        run_cli("doctor", "--no-db", "--json", env=_env(SOURCE_NETWORK_ENABLED="true")).output
    )
    assert any(
        f["detail"].startswith("CRAWL4AI_API_TOKEN: set")
        for f in networked["findings"]
        if f["name"] == "worker"
    )


def test_doctor_missing_required_configuration_fails(run_cli: Cli) -> None:
    result = run_cli("doctor", "--no-db", "--process", "worker")
    assert result.exit_code == 1
    assert "DATABASE_URL: missing (required" in result.output
    assert "database/connection" not in result.output  # --no-db


def test_doctor_without_database_url_skips_database(run_cli: Cli) -> None:
    result = run_cli("doctor", "--process", "crawler", env={"CRAWL4AI_API_TOKEN": "fake-token-x1"})
    assert "SKIP  database/connection" in result.output
    assert "fake-token-x1" not in result.output


def test_doctor_unknown_process_is_a_usage_error(run_cli: Cli) -> None:
    result = run_cli("doctor", "--no-db", "--process", "api,mailer")
    assert result.exit_code == 2
    assert "unknown process" in result.output


def test_doctor_flags_baseline_and_production_problems(run_cli: Cli) -> None:
    result = run_cli(
        "doctor",
        "--no-db",
        "--process",
        "scheduler",
        env=_env(APP_ENV="production", MCP_AUTH_MODE="dev_local", PRIMARY_MAX_PRICE_EUR="4000"),
    )
    assert result.exit_code == 1
    assert "PRIMARY_MAX_PRICE_EUR differs from the confirmed baseline" in result.output
    assert "dev_local authentication in production" in result.output
    assert "4000" not in result.output.replace("EUR 4,000", "")


def test_doctor_reports_notification_mode_and_conflicts(run_cli: Cli) -> None:
    ok = run_cli("doctor", "--no-db", "--process", "dispatcher", env=_env())
    assert "NOTIFICATION_PROVIDER=disabled" in ok.output
    assert "bridge_status unavailable" in ok.output
    conflict = run_cli(
        "doctor",
        "--no-db",
        "--process",
        "dispatcher",
        env=_env(MCP_EVENTS_ENABLED="true", NOTIFICATION_PROVIDER="slack"),
    )
    assert conflict.exit_code == 1
    assert "conflicting activation route" in conflict.output


def test_doctor_oauth_metadata_is_checked_offline(run_cli: Cli) -> None:
    result = run_cli(
        "doctor",
        "--no-db",
        "--process",
        "api",
        env=_env(
            MCP_PUBLIC_URL="http://deals-fake-0011.example.invalid/api", MCP_OAUTH_ISSUER="ftp://x.invalid"
        ),
    )
    assert result.exit_code == 1
    assert (
        "MCP_PUBLIC_URL must use https" in result.output
        or "MCP_PUBLIC_URL must end with /mcp" in result.output
    )
    assert "MCP_OAUTH_ISSUER must be an absolute http(s) URL" in result.output
    assert "deals-fake-0011" not in result.output


# --------------------------------------------------------------------------------------------
# --crawler: read-only health/contract inspection against a mocked crawler
# --------------------------------------------------------------------------------------------


@pytest.fixture
def mocked_crawler(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.9.4"})
        if request.url.path == "/crawl":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if request.url.path == "/config/dump":
            body = json.loads(request.content)
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    monkeypatch.setattr(
        doctor_module, "_make_crawler_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    yield seen


def test_doctor_crawler_check_is_read_only_and_redacted(
    run_cli: Cli, mocked_crawler: list[httpx.Request]
) -> None:
    result = run_cli("doctor", "--no-db", "--process", "crawler", "--crawler", env=_env())
    assert "OK    crawler/health" in result.output
    assert "OK    crawler/auth" in result.output
    assert "crawler/topology" in result.output
    for needle in NEEDLES:
        assert needle not in result.output
    paths = {r.url.path for r in mocked_crawler}
    assert paths <= {"/health", "/crawl", "/config/dump"}
    crawl_posts = [r for r in mocked_crawler if r.url.path == "/crawl"]
    assert crawl_posts and all("authorization" not in r.headers for r in crawl_posts)
    assert all(json.loads(r.content) == {"urls": []} for r in crawl_posts)  # never a real crawl


def test_doctor_without_crawler_flag_never_contacts_the_crawler(
    run_cli: Cli, mocked_crawler: list[httpx.Request]
) -> None:
    run_cli("doctor", "--no-db", env=_env())
    assert mocked_crawler == []


# --------------------------------------------------------------------------------------------
# Database checks (marker db)
# --------------------------------------------------------------------------------------------


@pytest.mark.db
def test_doctor_database_checks(run_cli: Cli, db_env: dict[str, str], db_url: str) -> None:
    result = run_cli("doctor", "--process", "scheduler", env=db_env)
    out = result.output
    assert "OK    database/connection" in out
    assert "OK    database/schema" in out
    assert "OK    database/set_role" in out
    assert "database/server_version" in out
    assert "database/backend_role" in out
    dbname = str(psycopg.conninfo.conninfo_to_dict(db_url)["dbname"])
    assert dbname not in out


@pytest.mark.db
def test_doctor_reports_an_unreachable_database(run_cli: Cli) -> None:
    result = run_cli(
        "doctor",
        "--process",
        "scheduler",
        env={
            "DATABASE_URL": "postgresql://nobody:FAKE-pw-777@127.0.0.1:1/none",
            "DATABASE_SET_ROLE": "suv_backend",
        },
    )
    assert result.exit_code == 1
    assert "ERROR database/connection" in result.output
    assert "FAKE-pw-777" not in result.output
