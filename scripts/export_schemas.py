#!/usr/bin/env python3
"""Export the committed JSON schemas from the typed backend models (spec 6, 21, 34).

Writes ``schemas/listing.schema.json``, ``review.schema.json``, ``valuation.schema.json``,
``event.schema.json``, one ``schemas/tools/<tool>.json`` per MCP tool (the twelve served spec
21 tools and the three prepared spec 37.8 ``V11_TOOLS``) and one ``schemas/api/<route>.json`` per
spec 37 API route (the mailbox-worker API and the dashboard inquiry routes). Output is
deterministic (sorted keys, two-space indent, trailing newline), so the files are stable
snapshots that ``tests/contracts/test_schema_snapshots.py`` compares byte for byte.

Usage::

    uv run python scripts/export_schemas.py            # (re)write the snapshots
    uv run python scripts/export_schemas.py --check    # exit 1 if any snapshot is stale

Stale ``schemas/tools/*.json`` and ``schemas/api/*.json`` files (tools or routes that no longer
exist) are removed when writing and reported by ``--check``. Nothing outside ``schemas/`` is
touched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from suv_deals.api.schemas import exported_api_schema_documents
from suv_deals.mcp.schemas import exported_schema_documents, render_schema_document

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "schemas"


#: Generated subdirectories: every ``*.json`` inside them must be an expected snapshot.
GENERATED_DIRS = ("tools", "api")


def build() -> dict[str, str]:
    """Relative path -> rendered file content."""
    documents = {**exported_schema_documents(), **exported_api_schema_documents()}
    return {path: render_schema_document(doc) for path, doc in documents.items()}


def stale_tool_files(output: Path, expected: dict[str, str]) -> list[Path]:
    wanted = {output / rel for rel in expected}
    stale: list[Path] = []
    for name in GENERATED_DIRS:
        directory = output / name
        if directory.is_dir():
            stale.extend(p for p in directory.glob("*.json") if p not in wanted)
    return sorted(stale)


def check(output: Path) -> list[str]:
    """Problems (missing, different or stale files); empty when every snapshot is current."""
    expected = build()
    problems: list[str] = []
    for rel, content in sorted(expected.items()):
        target = output / rel
        if not target.is_file():
            problems.append(f"missing: {rel}")
        elif target.read_text(encoding="utf-8") != content:
            problems.append(f"out of date: {rel}")
    problems.extend(f"stale: {p.relative_to(output)}" for p in stale_tool_files(output, expected))
    return problems


def write(output: Path) -> list[str]:
    expected = build()
    changed: list[str] = []
    for rel, content in sorted(expected.items()):
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8", newline="\n")
            changed.append(rel)
    for stale in stale_tool_files(output, expected):
        stale.unlink()
        changed.append(f"removed {stale.relative_to(output)}")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="verify snapshots; do not write")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="schemas directory")
    args = parser.parse_args(argv)
    output: Path = args.output.resolve()
    if args.check:
        problems = check(output)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("Schema snapshots are stale; run: uv run python scripts/export_schemas.py", file=sys.stderr)
            return 1
        print("schemas up to date")
        return 0
    changed = write(output)
    for item in changed:
        print(f"wrote {item}")
    if not changed:
        print("schemas up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
