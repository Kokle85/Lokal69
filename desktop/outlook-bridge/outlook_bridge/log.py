"""Content-free structured logging for the worker.

The worker never logs mail content: no bodies, subjects, addresses, attachment names, headers or
credentials. ``event`` accepts only scalar fields and replaces any string that is not a short,
token-like value (ids, codes, hashes) with ``<redacted>``, so content cannot slip into a log by
accident. The formatter additionally masks bearer tokens and e-mail addresses in free text such
as exception messages.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final
from uuid import UUID

LOGGER_ROOT: Final = "outlook_bridge"
_SAFE_STRING_RE: Final = re.compile(r"^[A-Za-z0-9._:/=+-]{0,128}$")
#: ``Authorization: Bearer <x>``, ``bearer <x>``, ``token=<x>``, ``api_key: <x>``: the value (after an
#: optional auth scheme) is masked, never only the scheme word.
_BEARER_RE: Final = re.compile(
    r"(?i)\b(authorization|bearer|token|api[_-]?key|credential|secret|password)\b"
    r"(\s*[:=]\s*|\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|(?:(?:bearer|basic|token)\s+)?[^\s,;\"']+)"
)
_JWT_RE: Final = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}")
REDACTED: Final = "<redacted>"

FieldValue = bool | int | float | str | UUID | Enum | datetime | None


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_ROOT}.{name}")


def safe_text(text: str) -> str:
    """Mask bearer tokens and e-mail addresses in free text (exception messages)."""
    masked = _JWT_RE.sub(REDACTED, text[:4000])  # bounded input; output is capped below
    masked = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", masked)
    return _EMAIL_RE.sub(REDACTED, masked)[:500]


def _field(value: FieldValue) -> bool | int | float | str | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Enum):
        return _field(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds") if value.tzinfo else REDACTED
    if isinstance(value, str) and _SAFE_STRING_RE.fullmatch(value) and "@" not in value:
        return value
    return REDACTED


def event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: FieldValue) -> None:
    """Log one structured event with scalar, content-free fields only."""
    if not _SAFE_STRING_RE.fullmatch(name):
        name = "invalid_event_name"
    payload = {key: _field(value) for key, value in sorted(fields.items()) if _SAFE_STRING_RE.fullmatch(key)}
    logger.log(level, name, extra={"bridge_fields": payload})


class SafeJsonFormatter(logging.Formatter):
    """One JSON object per line; free text is masked."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "bridge_fields", None)
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": safe_text(record.getMessage()),
        }
        if isinstance(fields, dict):
            payload["fields"] = fields
        if record.exc_info and record.exc_info[1] is not None:
            payload["error_type"] = type(record.exc_info[1]).__name__
            payload["error"] = safe_text(str(record.exc_info[1]))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_dir: Path | None, *, level: int = logging.INFO, to_stderr: bool = True) -> None:
    """Rotating JSON log file in the protected data directory (plus stderr when interactive)."""
    root = logging.getLogger(LOGGER_ROOT)
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    formatter = SafeJsonFormatter()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "outlook-bridge.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if to_stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


__all__ = [
    "LOGGER_ROOT",
    "REDACTED",
    "SafeJsonFormatter",
    "configure_logging",
    "event",
    "get_logger",
    "safe_text",
]
