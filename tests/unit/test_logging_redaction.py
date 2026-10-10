"""Structured logging and redaction (spec 24, 30): every listed secret type and contact format.

All secrets, e-mail addresses and phone numbers below are SYNTHETIC test values.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from suv_deals.observability import logging as slog
from suv_deals.observability.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    get_log_context,
    log_context,
    redact,
    redact_value,
    safe_url_for_log,
)
from suv_deals.settings import Settings

REPO = Path(__file__).resolve().parents[2]

# (input, substrings that must disappear, substring that must remain)
SECRET_CASES: list[tuple[str, list[str], str]] = [
    ("Authorization: Bearer abc.def-ghi", ["abc.def-ghi"], "Authorization: Bearer [REDACTED]"),
    ("authorization=Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"], "Basic [REDACTED]"),
    ("{'Authorization': 'Token tok123'}", ["tok123"], "Authorization"),
    ("proxy-authorization: Bearer ptok", ["ptok"], "proxy-authorization"),
    ("sent bearer sk.live-123_456", ["sk.live-123_456"], "bearer [REDACTED]"),
    (
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl here",
        ["eyJzdWIiOiIxMjMifQ", "c2lnbmF0dXJl"],
        "[REDACTED_JWT]",
    ),
    (
        "connect failed: postgresql://postgres:s3cr3t@db.abcd.supabase.co:5432/postgres?sslmode=require",
        ["s3cr3t", "postgres:", "5432", "sslmode"],
        "postgresql://[REDACTED]@db.abcd.supabase.co",
    ),
    ("DATABASE_URL=postgres://user:p%40ss@db.example.com/app", ["p%40ss", "user:"], "@db.example.com"),
    ("postgresql+psycopg://u:p@ss@host.example.com/db", ["p@ss", "u:p"], "@host.example.com"),
    ('{"dsn":"postgresql://u:pw@h.example.com/db","n":1}', ["u:pw"], '"n":1}'),
    ("redis://:secretpw@cache.example.com:6379/0", ["secretpw"], "redis://[REDACTED]@cache.example.com"),
    ("https://user:pw@example.com/x", ["user:pw"], "https://[REDACTED]@example.com/x"),
    (
        "GET https://api.example.com/x?page=2&token=abc&keyword=suv&api_key=k1&X-Amz-Signature=deadbeef"
        "&code=123&client_secret=zz&password=pw&sig=1&secret=s",
        ["token=abc", "k1", "deadbeef", "code=123", "=zz", "password=pw", "sig=1", "secret=s"],
        "page=2&token=[REDACTED]&keyword=suv",
    ),
    (
        "secret whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw end",
        ["MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"],
        "whsec_[REDACTED]",
    ),
    ("sig key whsk_abcdEFGH1234", ["abcdEFGH1234"], "whsk_[REDACTED]"),
    ("bot xoxb-1234-5678-abcdEFGH", ["1234-5678-abcdEFGH"], "xoxb-[REDACTED]"),
    ("user xoxp-1-2-3-abc", ["1-2-3-abc"], "xoxp-[REDACTED]"),
    ("app xapp-1-A1-123-abc", ["A1-123-abc"], "xapp-[REDACTED]"),
    ("refresh xoxe-1-abcd", ["1-abcd"], "xoxe-[REDACTED]"),
    ("hook https://hooks.slack.com/services/T000/B000/XXXXXXXX done", ["T000/B000", "XXXXXXXX"], "done"),
    (
        "response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN",
        ["96rGlfmibIGlgcZRskXaIFfN"],
        "hooks.slack.com",
    ),
    ("keys sb_secret_abcdef123456", ["abcdef123456"], "sb_secret_[REDACTED]"),
    ("pub sb_publishable_xyz987654321", ["xyz987654321"], "sb_publishable_[REDACTED]"),
    ("password=hunter2 passwd: x9 pwd=y8", ["hunter2", "x9", "y8"], "password=[REDACTED]"),
    ('{"api_key": "abc123", "client_secret": "xyz"}', ["abc123", '"xyz"'], '"api_key": "[REDACTED]"'),
    ("X-Api-Key: abc123", ["abc123"], "X-Api-Key: [REDACTED]"),
    ("refresh_token=rt-1 access_token: at-2", ["rt-1", "at-2"], "refresh_token=[REDACTED]"),
    ("Cookie: session=abc; other=1", ["session=abc"], "Cookie: [REDACTED]"),
    ("Set-Cookie: sid=abc; HttpOnly", ["sid=abc"], "Set-Cookie: [REDACTED]"),
    (
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----",
        ["MIIEvQIBADANBg"],
        "[REDACTED_PRIVATE_KEY]",
    ),
    ("contact vasko.synthetic+deals@example.com please", ["vasko.synthetic"], "[REDACTED_EMAIL] please"),
    # Environment-style assignments whose names end in SECRET_KEY / ACCESS_KEY and whose value
    # matches no value pattern (too short for sb_secret_, not a JWT).
    ("SUPABASE_SECRET_KEY=sb_secret_xx9", ["sb_secret_xx9"], "SUPABASE_SECRET_KEY=[REDACTED]"),
    ("SECRET_KEY: abc123", ["abc123"], "SECRET_KEY: [REDACTED]"),
    ("aws_secret_access_key=AKIAxyz", ["AKIAxyz"], "aws_secret_access_key=[REDACTED]"),
    ('{"service_role_key": "srk-123"}', ["srk-123"], '"service_role_key": "[REDACTED]"'),
    ("credentials=user:pw", ["user:pw"], "credentials=[REDACTED]"),
    # LLM provider keys logged without a key name (LLM_API_KEY values).
    ("llm call failed with sk-proj-SYNTHETIC1234567890abcd", ["SYNTHETIC1234567890abcd"], "sk-[REDACTED]"),
    ("anthropic sk-ant-api03-SYNTHETICabcdef123456", ["SYNTHETICabcdef123456"], "sk-[REDACTED]"),
]

PHONES = [
    "+49 151 23456789",
    "+49 (0)30 1234567",
    "+49-30-1234567",
    "0049 30 1234567",
    "+39 333 1234567",
    "+39 06 1234 5678",
    "+41 79 123 45 67",
    "+41 44 668 18 00",
    "+389 70 123 456",
    "+389 2 3123 456",
    "00389 70 123 456",
    "030 1234567",
    "(030) 123 4567",
    "0151/23456789",
    "0151-23456789",
    "01512345678",
    "06 1234 5678",
    "333 123 4567",
    "347-1234567",
    "079 123 45 67",
    "044 668 18 00",
    "070 123 456",
    "02 3123 456",
    "075/123-456",
]

NOT_PHONES = [
    "55555555-5555-4555-8555-555555555555",
    "03333333-3333-4333-8333-333333333333",
    "2026-10-06T10:05:00+02:00",
    "1531420618.000200",
    "06.10.2026",
    "06-10-2026",
    "EUR 2 500",
    "199.999 km",
    "199 999 km",
    "bytes=3221225472",
    "10.0.0.5",
    "version 0.9.4",
    "case_version=3 listing_revision=12",
    "+1 day",
    "price 12500.00",
    "port 5432",
]


@pytest.mark.parametrize(("text", "gone", "kept"), SECRET_CASES)
def test_secret_types_are_redacted(text: str, gone: list[str], kept: str) -> None:
    out = redact(text)
    for fragment in gone:
        assert fragment not in out, (fragment, out)
    assert kept in out, out
    assert redact(out) == out  # idempotent


@pytest.mark.parametrize("phone", PHONES)
def test_phone_numbers_are_redacted(phone: str) -> None:
    out = redact(f"Seller phone: {phone}, call after 18:00")
    assert "[REDACTED_PHONE]" in out, out
    assert phone not in out
    assert "18:00" in out


@pytest.mark.parametrize("text", NOT_PHONES)
def test_ids_dates_prices_are_not_mistaken_for_phones(text: str) -> None:
    assert redact(text) == text


def test_non_sensitive_names_are_kept() -> None:
    text = (
        "token_count=5 tokens: 7 secret_version=2 keyword=suv page=2 risk-adjusted task-queue-name-long "
        "https://www.example.sk/sk-skoda-octavia-combi-2015-diesel"
    )
    assert redact(text) == text


@pytest.mark.parametrize(
    "line",
    [
        ("+1 " + "1" * 34 + "a ") * 600,  # long digit runs after an international prefix
        (" 0" + "1" * 60 + "x") * 400,  # long national-looking digit runs
        ("+49" + " 12" * 40 + "x ") * 300,
        ("(030)" + "1" * 40 + "x ") * 500,
        "a@" + "b." * 12_000 + "!",
    ],
)
def test_redaction_is_linear_on_adversarial_seller_text(line: str) -> None:
    # Regression: the previous phone patterns backtracked exponentially (about 2 s for one
    # 20 kB line of digit runs). Logging is synchronous, so this was a denial-of-service path.
    started = time.perf_counter()
    redact(line)
    assert time.perf_counter() - started < 0.5


def test_identifier_keys_ending_in_key_are_not_secrets() -> None:
    out = redact_value(
        {
            "source_key": "mobile_de",
            "dedup_key": "review.pending:44444444-4444-4444-8444-444444444444:1",
            "deduplication_key": "x",
            "verification_cache_key": "vrf_0123",
            "api_key": "k",
            "secret_key": "s",
            "key": "raw",
        }
    )
    assert out["source_key"] == "mobile_de"
    assert out["dedup_key"].startswith("review.pending:")
    assert out["deduplication_key"] == "x"
    assert out["verification_cache_key"] == "vrf_0123"
    assert out["api_key"] == out["secret_key"] == out["key"] == REDACTED
    # Values of identifier keys are still text-redacted.
    assert redact_value({"source_key": "token=abc"})["source_key"] == f"token={REDACTED}"


def test_redact_never_raises_and_bounds_length() -> None:
    assert redact(12345) == "12345"  # type: ignore[arg-type]
    long = "a" * 50_000
    assert redact(long).endswith("...[TRUNCATED]")
    assert len(redact(long)) < 21_000


def test_safe_url_for_log() -> None:
    assert safe_url_for_log("https://receiver.example.com/mcp-events/cb_123?x=1") == (
        "https://receiver.example.com/[REDACTED]"
    )
    assert safe_url_for_log("not a url") == REDACTED
    assert safe_url_for_log("http://[::1") == REDACTED


# --------------------------------------------------------------------------- structured values


def test_redact_value_masks_sensitive_keys_and_nested_values() -> None:
    value = {
        "password": "x",
        "Authorization": "Bearer y",
        "client_secret": "z",
        "callback_url": "https://receiver.example.com/cb",
        "secret_version": 2,
        "token_count": 9,
        "nested": {"api-key": "k", "note": "mail me at a.b@example.com", "list": ["whsec_abcdEFGH"]},
        "secret_str": SecretStr("hidden"),
        "raw": b"\x00\x01",
        "amount": Decimal("2500.00"),
        "id": UUID("11111111-1111-4111-8111-111111111111"),
        "ok": True,
        "n": None,
        "f": float("nan"),
    }
    out = redact_value(value)
    assert out["password"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["client_secret"] == REDACTED
    assert out["callback_url"] == REDACTED
    assert out["secret_version"] == 2
    assert out["token_count"] == 9
    assert out["nested"]["api-key"] == REDACTED
    assert "a.b@example.com" not in out["nested"]["note"]
    assert out["nested"]["list"] == ["whsec_[REDACTED]"]
    assert out["secret_str"] == REDACTED
    assert out["raw"] == "[BYTES 2]"
    assert out["amount"] == "2500.00"
    assert out["id"] == "11111111-1111-4111-8111-111111111111"
    assert out["ok"] is True and out["n"] is None
    assert out["f"] == "nan"
    json.dumps(out)


def test_redact_value_depth_limit() -> None:
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(20):
        cursor["x"] = {}
        cursor = cursor["x"]
    text = json.dumps(redact_value(deep))
    assert "[TRUNCATED]" in text


# --------------------------------------------------------------------------- logging integration


@pytest.fixture
def json_log() -> Any:
    stream = io.StringIO()
    root = logging.getLogger()
    previous_level = root.level
    settings = Settings(_env_file=None, log_level="DEBUG", build_id="build-test-1")  # type: ignore[call-arg]
    handler = configure_logging(settings, stream=stream)
    yield stream
    root.removeHandler(handler)
    handler.close()
    root.setLevel(previous_level)
    logging.captureWarnings(False)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_lines_with_context_and_build_id(json_log: io.StringIO) -> None:
    logger = logging.getLogger("suv_deals.test")
    with log_context(request_id="req-1", job_id="job-9", source_key="mobile_de", event_id="evt-1"):
        logger.info("processed %s pages", 3, extra={"outcome": "ok", "elapsed_ms": 12})
    logger.info("outside")
    first, second = _lines(json_log)
    assert first["message"] == "processed 3 pages"
    assert first["level"] == "INFO"
    assert first["logger"] == "suv_deals.test"
    assert first["ts"].endswith("Z")
    assert first["request_id"] == "req-1"
    assert first["job_id"] == "job-9"
    assert first["source_key"] == "mobile_de"
    assert first["event_id"] == "evt-1"
    assert first["build_id"] == "build-test-1"
    assert first["outcome"] == "ok" and first["elapsed_ms"] == 12
    assert "request_id" not in second
    assert second["build_id"] == "build-test-1"


def test_messages_args_and_extras_are_redacted(json_log: io.StringIO) -> None:
    logger = logging.getLogger("suv_deals.test")
    logger.warning(
        "callback %s failed for %s",
        "https://receiver.example.com/cb?token=abc",
        "owner@example.com",
        extra={"secret": "whsec_abcdEFGH", "detail": "Bearer zzz", "callback_url": "https://r.example.com/x"},
    )
    (line,) = _lines(json_log)
    raw = json.dumps(line)
    for leaked in ("token=abc", "owner@example.com", "whsec_abcdEFGH", "zzz", "r.example.com/x"):
        assert leaked not in raw
    assert line["secret"] == REDACTED
    assert line["callback_url"] == REDACTED


def test_exceptions_do_not_leak_database_url(json_log: io.StringIO) -> None:
    logger = logging.getLogger("suv_deals.test")
    dsn = "postgresql://postgres:sup3r-s3cret@db.abcd.supabase.co:5432/postgres"
    try:
        try:
            raise ConnectionError(f"could not connect to {dsn}")
        except ConnectionError as inner:
            raise RuntimeError(f"pool init failed (DATABASE_URL={dsn})") from inner
    except RuntimeError:
        logger.exception("startup failed with %s", dsn)
    (line,) = _lines(json_log)
    raw = json.dumps(line)
    assert "sup3r-s3cret" not in raw
    assert "postgres:sup3r" not in raw
    assert line["exc_type"] == "RuntimeError"
    assert "db.abcd.supabase.co" in line["exc_message"]
    assert "ConnectionError" in line["exc_traceback"]


def test_reserved_extra_names_do_not_override_core_fields(json_log: io.StringIO) -> None:
    logging.getLogger("suv_deals.test").info("real", extra={"level": "FAKE", "ts": "x"})
    (line,) = _lines(json_log)
    assert line["level"] == "INFO"
    assert line["extra_level"] == "FAKE"


def test_unformattable_args_do_not_break_logging(json_log: io.StringIO) -> None:
    logging.getLogger("suv_deals.test").info("value %d", "not-a-number")
    (line,) = _lines(json_log)
    assert "unformattable" in line["message"]


def test_configure_is_idempotent_and_quiets_url_loggers() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    first = configure_logging(settings, stream=io.StringIO())
    second = configure_logging(settings, stream=io.StringIO())
    try:
        names = [h.get_name() for h in logging.getLogger().handlers]
        assert names.count(slog.HANDLER_NAME) == 1
        assert first not in logging.getLogger().handlers
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        logging.getLogger().removeHandler(second)
        second.close()


def test_production_omits_tracebacks() -> None:
    stream = io.StringIO()
    settings = Settings(_env_file=None, app_env="production")  # type: ignore[call-arg]
    handler = configure_logging(settings, stream=stream)
    try:
        try:
            raise ValueError("boom token=abc")
        except ValueError:
            logging.getLogger("suv_deals.test").exception("failed")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    (line,) = _lines(stream)
    assert "exc_traceback" not in line
    assert line["exc_message"] == "boom token=[REDACTED]"


def test_filter_protects_plain_text_handlers_too() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("suv_deals.plain")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise OSError("auth failed for xoxb-1234-secret")
        except OSError:
            logger.exception("Authorization: Bearer %s", "tok-123")
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    text = stream.getvalue()
    assert "tok-123" not in text
    assert "1234-secret" not in text
    assert "xoxb-[REDACTED]" in text


def test_context_rejects_unknown_fields_and_redacts_values() -> None:
    with pytest.raises(TypeError), log_context(user_email="x"):
        pass  # pragma: no cover
    with log_context(case_id="case owner@example.com"):
        assert get_log_context()["case_id"] == "case [REDACTED_EMAIL]"
    assert "case_id" not in get_log_context()


def test_formatter_handles_records_without_filter() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pw %s", ("password=abc",), None)
    out = json.loads(JsonFormatter().format(record))
    assert out["message"] == "pw password=[REDACTED]"


# --------------------------------------------------------------------------- scripts/redact_logs.py


def _run_script(args: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "redact_logs.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )


def test_script_redacts_stdin() -> None:
    result = _run_script([], "Authorization: Bearer abc\nmail a@example.com\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Authorization: Bearer [REDACTED]\nmail [REDACTED_EMAIL]\n"


def test_script_redacts_files_and_json_lines(tmp_path: Path) -> None:
    log = tmp_path / "app.jsonl"
    log.write_text(
        json.dumps({"message": "x", "password": "p1", "note": "call +49 151 23456789"})
        + "\nplain postgresql://u:pw@db.example.com/x\n",
        encoding="utf-8",
    )
    result = _run_script(["--json-lines", str(log)])
    assert result.returncode == 0, result.stderr
    first, second = result.stdout.splitlines()
    parsed = json.loads(first)
    assert parsed["password"] == REDACTED
    assert parsed["note"] == "call [REDACTED_PHONE]"
    assert second == "plain postgresql://[REDACTED]@db.example.com"


def test_script_reports_unreadable_files(tmp_path: Path) -> None:
    result = _run_script([str(tmp_path / "missing.log")])
    assert result.returncode == 2
    assert "cannot read" in result.stderr
