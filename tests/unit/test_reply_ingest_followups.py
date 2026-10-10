"""Unit checks of the reply-ingest work package follow-ups (no database).

- every availability evidence kind has an ``observed_via`` in the candidate history;
- detail-page evidence kinds: not-found -> ``source_detail_not_found``, an explicit reserved badge
  -> ``source_reserved_badge`` (each kind maps to the availability the database accepts);
- bare ``suvmcp_``/``suvdev_``/``suvmail_`` credentials are masked in free text;
- migration 20261007000400 keeps the guard message that ``errors_map`` maps, is ASCII and has no
  transaction control;
- the seller-reply signal payload check and the worker gap codes.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from suv_deals.domain.enums import Availability, AvailabilityEvidenceKind
from suv_deals.observability.logging import redact
from suv_deals.persistence import availability_repo, listings_repo, mail_workers_repo, replies_repo
from suv_deals.persistence.errors_map import guard_rule_for
from suv_deals.persistence.queries import candidates

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "supabase" / "migrations" / "20261007000400_mail_worker_credential_kind.sql"


# --- candidate availability history ------------------------------------------------------------


def test_every_evidence_kind_has_an_observed_via() -> None:
    assert set(candidates._EVIDENCE_VIA) == set(AvailabilityEvidenceKind)
    assert candidates._EVIDENCE_VIA[AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE] == "detail"
    assert candidates._EVIDENCE_VIA[AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND] == "detail"


@pytest.mark.parametrize("kind", list(AvailabilityEvidenceKind))
def test_history_renders_every_evidence_kind(kind: AvailabilityEvidenceKind) -> None:
    event: dict[str, Any] = {
        "metadata": {
            "new": "unknown",
            "evidence_kind": kind.value,
            "observed_at": "2026-10-01T00:00:00+00:00",
        },
        "occurred_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    (point,) = candidates._availability_history([], [event])
    assert point.observed_via == candidates._EVIDENCE_VIA[kind]


# --- detail evidence kinds ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "availability", "reason", "kind"),
    [
        (
            "not_found",
            Availability.UNKNOWN,
            "detail_not_found",
            AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND,
        ),
        (
            "listing",
            Availability.RESERVED,
            "source_reserved_badge",
            AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE,
        ),
        (
            "removed",
            Availability.REMOVED,
            "source_removed_page",
            AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE,
        ),
        (
            "listing",
            Availability.REMOVED,
            "source_removed_page",
            AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE,
        ),
        (
            "listing",
            Availability.SOLD_CLAIMED,
            "source_sold_badge",
            AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
        ),
        (
            "listing",
            Availability.AVAILABLE,
            "detail_observation",
            AvailabilityEvidenceKind.SOURCE_OBSERVATION,
        ),
        ("listing", Availability.UNKNOWN, "detail_observation", AvailabilityEvidenceKind.SOURCE_OBSERVATION),
    ],
)
def test_detail_evidence_kinds(
    detail: listings_repo.DetailKind, availability: Availability, reason: str, kind: AvailabilityEvidenceKind
) -> None:
    assert listings_repo._evidence_kind(detail, availability) == (reason, kind)
    # The pair is always one the database mapping accepts (availability_events_mapping_ck).
    assert availability in availability_repo._KIND_VALUES[kind]


def test_a_not_found_page_is_never_removal_or_sale_evidence() -> None:
    """``ingest_detail`` turns a healthy source's 404 into ``unknown`` (never removed or sold)."""
    reason, kind = listings_repo._evidence_kind("not_found", Availability.UNKNOWN)
    assert (reason, kind) == ("detail_not_found", AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND)
    assert availability_repo._KIND_VALUES[kind] == frozenset({Availability.UNKNOWN})


# --- bare credential redaction -----------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["suvmcp", "suvdev", "suvmail"])
def test_bare_credentials_are_masked(prefix: str) -> None:
    token = f"{prefix}_{secrets.token_hex(32)}"
    assert redact(f"worker said {token}.") == f"worker said {prefix}_[REDACTED]."
    # A token glued to a word character (``credential_suvmail_...``) is masked too (regression:
    # the leading word boundary let it through).
    glued = (f"credential_{token}", f"Bearer{token}", f"9{token}")
    for text in (token, f"token={token}", f"worker said {token}.", f"[{token}]", f"x:{token}\n", *glued):
        masked = redact(text)
        assert token[len(prefix) + 1 :] not in masked
        assert "[REDACTED]" in masked  # a key=value rule may mask the whole value
        assert redact(masked) == masked  # idempotent


def test_short_display_prefixes_stay_readable() -> None:
    text = "credential suvmail_0a1b2c issued; mcp suvmcp_abcdef0123 rotated"
    assert redact(text) == text


def test_long_runs_of_token_like_text_are_linear() -> None:
    text = "suvmail_" + "a" * 50_000
    assert redact(text).startswith("suvmail_[REDACTED]")


# --- migration 20261007000400 ------------------------------------------------------------------


def test_the_credential_kind_migration_keeps_the_mapped_guard_message() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.isascii()
    assert not re.search(r"(?im)^\s*(begin|commit|rollback)\s*;", sql)
    messages = re.findall(r"errcode = '(SV00[0-9])',\s*message = '((?:[^']|'')*)'", sql)
    assert messages, "no guard messages found"
    for state, message in messages:
        text = message.replace("''", "'")
        if state in ("SV002", "SV003"):
            assert guard_rule_for(state, text) is not None, text
    assert ("SV003", "a mailbox worker binding needs a live credential carrying only mail:ingest") in messages
    assert "credential_kind" in sql and "'mail_worker'" in sql


# --- reply signal payload and worker gap codes -------------------------------------------------


def test_signal_payload_check_refuses_text_and_addresses() -> None:
    minimal = {
        "schema_version": "1.0",
        "event_id": "e",
        "type": "seller.reply.received.v1",
        "inquiry_id": "i",
        "reply_id": "r",
        "dashboard_url": "https://dashboard.example.invalid/inquiries/i",
        "status": "received",
    }
    assert replies_repo.signal_payload_is_minimal(minimal)
    assert not replies_repo.signal_payload_is_minimal({**minimal, "body": "Synthetic text"})
    assert not replies_repo.signal_payload_is_minimal({**minimal, "status": "seller@example.invalid"})


def test_gap_codes_round_trip_in_whole_seconds() -> None:
    start = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    codes = [
        mail_workers_repo._gap_code("outlook_closed", start, None),
        mail_workers_repo._gap_code("outlook_closed", start - timedelta(hours=1), end - timedelta(hours=1)),
        "gap:bad",
        "account:verified",
        "matching_gaps:2",
    ]
    gaps = mail_workers_repo.parse_gap_codes(codes)
    assert [(g.kind, g.open) for g in gaps] == [("outlook_closed", True), ("outlook_closed", False)]
    closing = mail_workers_repo.ReportedGap(
        kind="outlook_closed", started_at=start, ended_at=end, open=False, detected_by="worker"
    )
    merged, changed = mail_workers_repo._merge_gaps(codes, [closing])
    assert changed == [closing]
    assert sum(1 for c in merged if c.startswith(f"gap:outlook_closed:{int(start.timestamp())}:")) == 1
