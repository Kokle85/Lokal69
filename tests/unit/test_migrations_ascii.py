"""Migration files must be pure ASCII.

The hosted Supabase project receives migrations through a connector that does not carry every
Unicode character faithfully (U+2028/U+2029 arrived as plain spaces), which would silently change
CHECK semantics. Non-ASCII characters belong in SQL as escapes (e.g. regex ``\\u2028``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*.sql"))


def test_there_are_migrations() -> None:
    assert MIGRATIONS


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_is_ascii_only(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    offenders = [
        (line_no, f"U+{ord(ch):04X}")
        for line_no, line in enumerate(text.splitlines(), start=1)
        for ch in line
        if ord(ch) > 127
    ]
    assert offenders == [], f"{path.name}: non-ASCII characters at {offenders[:10]}"
