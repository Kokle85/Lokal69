"""U+0085/U+2028/U+2029 stay rejected after the migration was made ASCII-only.

Migration 20261006001000 writes these characters as regex escapes (``\\u2028``) so the hosted
Supabase connector cannot turn them into spaces. The checks must still refuse the real characters
and must still accept an ordinary space.
"""

from __future__ import annotations

import psycopg
import pytest

SEPARATORS = [chr(0x85), chr(0x2028), chr(0x2029)]


@pytest.mark.parametrize("ch", SEPARATORS, ids=["nel", "ls", "ps"])
def test_sender_display_name_rejects_unicode_line_separators(db_conn: psycopg.Connection, ch: str) -> None:
    row = db_conn.execute("select app.sender_display_name_ok(%s)", (f"Synthetic{ch}Sender",)).fetchone()
    assert row is not None and row[0] is False


def test_sender_display_name_accepts_an_ordinary_space(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute("select app.sender_display_name_ok(%s)", ("Synthetic Sender",)).fetchone()
    assert row is not None and row[0] is True


def test_every_line_separator_check_lists_all_three_escapes(db_conn: psycopg.Connection) -> None:
    """Each CHECK that refuses U+0085 also refuses U+2028 and U+2029, written as escapes."""
    rows = db_conn.execute(
        """
        select c.conname, pg_get_constraintdef(c.oid) as def
          from pg_constraint c
          join pg_namespace n on n.oid = c.connamespace
         where n.nspname in ('app', 'ops') and c.contype = 'c'
           and pg_get_constraintdef(c.oid) like '%u0085%'
        """
    ).fetchall()
    assert rows, "expected CHECK constraints guarding against U+0085"
    for name, definition in rows:
        assert "\\u2028" in definition and "\\u2029" in definition, name
        assert chr(0x2028) not in definition and chr(0x2029) not in definition, name


@pytest.mark.parametrize("ch", SEPARATORS, ids=["nel", "ls", "ps"])
def test_reply_subject_pattern_rejects_unicode_line_separators(db_conn: psycopg.Connection, ch: str) -> None:
    """The exact pattern used by the subject/body CHECKs matches each separator, not a space."""
    pattern = "[[:cntrl:]\\u0085\\u2028\\u2029]"
    row = db_conn.execute("select %s ~ %s, %s ~ %s", (f"a{ch}b", pattern, "a b", pattern)).fetchone()
    assert row == (True, False)
