#!/usr/bin/env python3
"""Redact secrets and contact details from log files before sharing them.

Usage:
    uv run python scripts/redact_logs.py < app.log > app.redacted.log
    uv run python scripts/redact_logs.py app.log other.log > combined.redacted.log
    uv run python scripts/redact_logs.py --json-lines app.jsonl

Uses exactly the same `redact()`/`redact_value()` functions as the runtime
logging filter. In `--json-lines` mode each line that parses as JSON is
redacted structurally (secret-named keys lose their values) and re-serialized;
other lines fall back to text redaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from typing import TextIO

from suv_deals.observability.logging import redact, redact_value


def redact_line(line: str, *, json_lines: bool) -> str:
    newline = "\n" if line.endswith("\n") else ""
    body = line[:-1] if newline else line
    if json_lines and body.strip():
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict | list):
            return json.dumps(redact_value(parsed), ensure_ascii=False) + newline
    return redact(body) + newline


def redact_stream(lines: Iterable[str], out: TextIO, *, json_lines: bool) -> int:
    count = 0
    for line in lines:
        out.write(redact_line(line, json_lines=json_lines))
        count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="*", help="log files to read (default: stdin)")
    parser.add_argument("--json-lines", action="store_true", help="redact JSON lines structurally")
    args = parser.parse_args(argv)
    if not args.paths:
        redact_stream(sys.stdin, sys.stdout, json_lines=args.json_lines)
        return 0
    status = 0
    for path in args.paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                redact_stream(handle, sys.stdout, json_lines=args.json_lines)
        except OSError as exc:
            print(f"redact_logs: cannot read {path}: {exc.strerror}", file=sys.stderr)
            status = 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())
