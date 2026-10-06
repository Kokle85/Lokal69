"""Structured JSON logging with context fields and secret/PII redaction (spec 24, 30).

- `configure_logging(settings)` installs one JSON handler on the root logger.
  Every record passes `RedactionFilter` before formatting, including records
  from third-party libraries.
- Context fields (request_id, run_id, job_id, case_id, event_id, source_key,
  adapter_version, build_id) come from contextvars; bind them with
  `log_context(...)`.
- `redact()` masks: Authorization/Bearer credentials, JWTs, credentials in
  PostgreSQL URLs (host kept, everything else dropped), credentials in other
  URLs, URL query parameters named like token/key/secret/signature/code/password,
  `whsec_`/`whsk_`/`whpk_` secrets, Slack tokens (`xox?-`, `xapp-`) and incoming
  webhook URLs, Supabase `sb_secret_`/`sb_publishable_` keys, `sk-...` provider API
  keys, key=value secrets (including `*_SECRET_KEY`/`*_ACCESS_KEY` assignments),
  cookies, PEM private keys, e-mail addresses and phone numbers (international
  and DE/IT/CH/MK national formats). Exception messages and tracebacks are
  redacted as text, so a failing connect never leaks DATABASE_URL.

Redaction is deliberately conservative (it may over-mask); it is idempotent and
runs in linear time on untrusted text (no nested optional quantifiers).
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import IO, Any, Final
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretBytes, SecretStr

from suv_deals.settings import Settings

REDACTED: Final = "[REDACTED]"
HANDLER_NAME: Final = "suv_deals_json"
CONTEXT_FIELDS: Final = (
    "request_id",
    "run_id",
    "job_id",
    "case_id",
    "event_id",
    "source_key",
    "adapter_version",
    "build_id",
)
_MAX_CONTEXT_VALUE: Final = 200
_MAX_DEPTH: Final = 8
_MAX_STRING: Final = 20_000

_CONTEXT_VARS: Final[dict[str, ContextVar[str | None]]] = {
    name: ContextVar(f"suv_deals_log_{name}", default=None) for name in CONTEXT_FIELDS
}
_default_build_id: str | None = None

# --------------------------------------------------------------------------- text redaction

_SENSITIVE_PARAM = re.compile(
    r"^(?:[\w.-]*[_.-])?(token|key|secret|signature|sig|code|password|passwd|pwd|auth|apikey|jwt|"
    r"credential|credentials|session)$",
    re.IGNORECASE,
)
_SENSITIVE_KV_NAMES = (
    r"password|passwd|pwd|secret|client[_-]?secret|signing[_-]?secret|api[_-]?key|x-api-key|apikey|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|bot[_-]?token|token|private[_-]?key|"
    r"webhook[_-]?secret|encryption[_-]?key|secret[_-]?key|access[_-]?key|signing[_-]?key|"
    r"service[_-]?role[_-]?key|master[_-]?key|session[_-]?key|credentials?"
)

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_PG_URL = re.compile(
    r"\b(postgres(?:ql)?(?:\+[a-z0-9]+)?)://[^\s'\"<>]*@(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)[^\s'\"<>,;]*",
    re.IGNORECASE,
)
_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]{1,15})://[^\s/@'\"<>]+@", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"\b((?:proxy-)?authorization)([\"']?\s*[:=]\s*[\"']?)(?:(bearer|basic|token|digest)\s+)?[^\s\"',;]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_WHSEC = re.compile(r"\b(whsec|whsk|whpk)_[A-Za-z0-9+/=_-]{4,}")
_SLACK_TOKEN = re.compile(r"\b(xox[abeoprs]|xapp)-[A-Za-z0-9-]{4,}")
_SLACK_HOOK = re.compile(r"(hooks\.slack\.com)(?:/|%2F)[^\s\"'<>&]+", re.IGNORECASE)
_SUPABASE_KEY = re.compile(r"\b(sb_secret|sb_publishable)_[A-Za-z0-9_-]{4,}")
# Provider API keys that may appear without a key name (LLM_API_KEY values: sk-..., sk-proj-..., sk-ant-...).
_PROVIDER_API_KEY = re.compile(r"\bsk-(?:[a-z0-9]{2,8}-){0,2}[A-Za-z0-9_-]{16,}")
_QUERY_PARAM = re.compile(r"([?&;])([\w.\-\[\]]{1,64})=([^&\s#'\"<>]*)")
_KV_SECRET = re.compile(
    rf"(?<![A-Za-z0-9])({_SENSITIVE_KV_NAMES})([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)([^\s\"'&,;}}]+)",
    re.IGNORECASE,
)
_COOKIE = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*).+$")
_EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
# Phone patterns are written so that every digit run has exactly one way to be split into
# groups (a group always starts with a separator or a parenthesised prefix). Optional
# separators between bounded digit groups backtrack exponentially on long digit runs,
# and log lines can contain untrusted seller text. The digit count is checked afterwards.
_PHONE_INTL = re.compile(
    r"(?<![\w+])(?:\+|00)[1-9]\d{0,14}"
    r"(?:(?:[ ./-]|[ ./-]?\(\d{1,5}\)[ ./-]?)\d{1,15}){0,7}"
    r"(?![\w])"
)
_PHONE_NATIONAL = re.compile(
    r"(?<![\w.:/+-])(?:\(0[1-9]\d{0,4}\)[ /-]?\d{2,12}|0[1-9]\d{0,12})"
    r"(?:[ /-]\d{2,8}){0,4}"
    r"(?![\w.:/-])"
)
_PHONE_IT_MOBILE = re.compile(r"(?<![\w.:/+-])3\d{2}[ -]\d{3}[ -]?\d{3,4}(?![\w.:/-])")


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def _phone_sub(min_digits: int, max_digits: int) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        count = _digits(text)
        return "[REDACTED_PHONE]" if min_digits <= count <= max_digits else text

    return replace


def _query_sub(match: re.Match[str]) -> str:
    sep, name, value = match.group(1), match.group(2), match.group(3)
    if value and value != REDACTED and _SENSITIVE_PARAM.fullmatch(name):
        return f"{sep}{name}={REDACTED}"
    return match.group(0)


def _provider_key_sub(match: re.Match[str]) -> str:
    # Real keys are high-entropy (upper + lower case + digits); a lowercase URL slug such as
    # `sk-skoda-octavia-2015` is not a key and stays readable.
    text = match.group(0)
    tail = text[3:]
    if any(c.isupper() for c in tail) and any(c.islower() for c in tail) and any(c.isdigit() for c in tail):
        return f"sk-{REDACTED}"
    return text


def _auth_sub(match: re.Match[str]) -> str:
    scheme = f"{match.group(3)} " if match.group(3) else ""
    return f"{match.group(1)}{match.group(2)}{scheme}{REDACTED}"


_RULES: Final[tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]] = (
    (_PEM, "[REDACTED_PRIVATE_KEY]"),
    (_PG_URL, rf"\1://{REDACTED}@\2"),
    (_URL_USERINFO, rf"\1://{REDACTED}@"),
    (_SLACK_HOOK, rf"\1/{REDACTED}"),
    (_AUTH_HEADER, _auth_sub),
    (_BEARER, rf"\1 {REDACTED}"),
    (_JWT, "[REDACTED_JWT]"),
    (_WHSEC, rf"\1_{REDACTED}"),
    (_SLACK_TOKEN, rf"\1-{REDACTED}"),
    (_SUPABASE_KEY, rf"\1_{REDACTED}"),
    (_PROVIDER_API_KEY, _provider_key_sub),
    (_QUERY_PARAM, _query_sub),
    (_KV_SECRET, rf"\1\2{REDACTED}"),
    (_COOKIE, rf"\1{REDACTED}"),
    (_EMAIL, "[REDACTED_EMAIL]"),
    (_PHONE_INTL, _phone_sub(7, 15)),
    (_PHONE_NATIONAL, _phone_sub(9, 13)),
    (_PHONE_IT_MOBILE, _phone_sub(9, 10)),
)


def redact(text: str) -> str:
    """Mask secrets and contact details in free text. Idempotent and never raises."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > _MAX_STRING:
        text = text[:_MAX_STRING] + "...[TRUNCATED]"
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def safe_url_for_log(url: str) -> str:
    """Scheme and host only (callback/webhook URLs can carry capability paths)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme or not parts.hostname:
        return REDACTED
    return f"{parts.scheme}://{parts.hostname}/{REDACTED}"


# --------------------------------------------------------------------------- structured redaction

_SENSITIVE_KEYS: Final = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "authorization",
        "apikey",
        "cookie",
        "signature",
        "dsn",
        "credential",
        "credentials",
        "whsec",
        "jwt",
        "key",
    }
)
_SENSITIVE_FULL_KEYS: Final = frozenset(
    {
        "api_key",
        "x_api_key",
        "private_key",
        "database_url",
        "connection_string",
        "set_cookie",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "signing_secret",
        "bot_token",
        "webhook_secret",
        "encryption_key",
        "callback_url",
        "webhook_url",
        "response_url",
        "proxy_authorization",
    }
)


# Identifier fields whose names end in `key` but carry no secret. They are mandated log
# context (spec 30: source key) or deduplication references and must stay readable.
_NON_SECRET_KEYS: Final = frozenset(
    {
        "source_key",
        "profile_key",
        "dedup_key",
        "deduplication_key",
        "cache_key",
        "verification_cache_key",
        "sort_key",
        "partition_key",
        "business_key",
        "event_key",
        "slot_key",
        "metric_key",
    }
)


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in _NON_SECRET_KEYS:
        return False
    if normalized in _SENSITIVE_FULL_KEYS:
        return True
    last = normalized.rsplit("_", 1)[-1]
    return last in _SENSITIVE_KEYS


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure; secret-named keys lose their values entirely."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"[BYTES {len(bytes(value))}]"
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return redact_value(value.value, _depth=_depth + 1)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)[:100]
            out[key] = REDACTED if is_sensitive_key(key) else redact_value(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(item, _depth=_depth + 1) for item in list(value)[:200]]
    return redact(repr(value))


# --------------------------------------------------------------------------- context


def _check_fields(fields: Mapping[str, object]) -> None:
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise TypeError(f"unknown log context field(s): {', '.join(sorted(unknown))}")


def bind_log_context(**fields: object) -> dict[str, Token[str | None]]:
    _check_fields(fields)
    tokens: dict[str, Token[str | None]] = {}
    for name, value in fields.items():
        text = None if value is None else redact(str(value))[:_MAX_CONTEXT_VALUE]
        tokens[name] = _CONTEXT_VARS[name].set(text)
    return tokens


def reset_log_context(tokens: Mapping[str, Token[str | None]]) -> None:
    for name, token in tokens.items():
        _CONTEXT_VARS[name].reset(token)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    tokens = bind_log_context(**fields)
    try:
        yield
    finally:
        reset_log_context(tokens)


def get_log_context() -> dict[str, str]:
    context = {name: var.get() for name, var in _CONTEXT_VARS.items()}
    if context.get("build_id") is None and _default_build_id is not None:
        context["build_id"] = _default_build_id
    return {name: value for name, value in context.items() if value is not None}


# --------------------------------------------------------------------------- logging integration

_STD_ATTRS: Final = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)
_RESERVED_OUTPUT: Final = frozenset(
    {"ts", "level", "logger", "message", "exc_type", "exc_message", "exc_traceback", *CONTEXT_FIELDS}
)
_EXC_FORMATTER = logging.Formatter()


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STD_ATTRS and not key.startswith("_") and key != "redacted"
    }


class RedactionFilter(logging.Filter):
    """Rewrites the record in place so every downstream handler sees redacted data."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: never fail the log call
            message = f"{record.msg!r} (unformattable arguments)"
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            text = record.exc_text or _EXC_FORMATTER.formatException(record.exc_info)
            record.exc_text = redact(text)
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for key, value in _record_extras(record).items():
            setattr(record, key, REDACTED if is_sensitive_key(key) else redact_value(value))
        record.redacted = True
        return True


def _utc_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, context, extras, exception."""

    def __init__(self, *, include_traceback: bool = True) -> None:
        super().__init__()
        self._include_traceback = include_traceback

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        payload.update(get_log_context())
        for key, value in _record_extras(record).items():
            out_key = f"extra_{key}" if key in _RESERVED_OUTPUT else key
            payload[out_key] = REDACTED if is_sensitive_key(key) else redact_value(value)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exc_type"] = type(exc).__name__
            payload["exc_message"] = redact(str(exc))
            if self._include_traceback:
                text = record.exc_text or self.formatException(record.exc_info)
                payload["exc_traceback"] = redact(text)
        elif record.exc_text and self._include_traceback:
            payload["exc_traceback"] = redact(record.exc_text)
        if record.stack_info and self._include_traceback:
            payload["stack"] = redact(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=lambda o: redact(repr(o)))


def _level(name: str) -> int:
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    return level if isinstance(level, int) else logging.INFO


def configure_logging(
    settings: Settings,
    *,
    stream: IO[str] | None = None,
    include_traceback: bool | None = None,
) -> logging.Handler:
    """Install (or replace) the JSON handler on the root logger. Idempotent."""
    global _default_build_id  # noqa: PLW0603 - process-wide default for the build_id field
    _default_build_id = settings.build_id or None
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.set_name(HANDLER_NAME)
    traceback = include_traceback if include_traceback is not None else settings.app_env != "production"
    handler.setFormatter(JsonFormatter(include_traceback=traceback))
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(_level(settings.log_level))
    # httpx/httpcore log full request URLs (callback paths, query strings) at INFO/DEBUG.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return handler
