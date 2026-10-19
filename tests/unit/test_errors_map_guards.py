"""Seller-inquiry guard refusals map to typed errors with a stable machine reason (pure tests).

The real-trigger counterparts (PostgreSQL 16 and 17) are in
tests/integration/db/test_v11_integration_foundation.py. Here every ``SV002``/``SV003`` MESSAGE
of migration 20261006001000 is extracted from the SQL source and checked against
``errors_map.GUARD_RULES``: each message matches exactly one rule, every rule is reachable, and
the mapped error never echoes anything but enum-like tokens.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from suv_deals.errors import (
    HTTP_STATUS,
    AppError,
    EmailDeliveryUncertain,
    ErrorCode,
    Forbidden,
    RateLimited,
    SourcePaused,
    Unauthenticated,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.persistence.errors_map import GUARD_RULES, guard_reason, guard_rule_for, map_db_error

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "supabase" / "migrations" / "20261006001000_seller_inquiries.sql"

_RAISE = re.compile(
    r"raise exception using\s+errcode = '(?P<code>SV00[23])',\s+"
    r"message = (?P<expr>(?:'(?:[^']|'')*'|[^;'])*);",
    re.S,
)
#: Sample values for the ``pg_catalog.format`` placeholders (keyed by a fragment of the format).
_SAMPLES: dict[str, tuple[str, ...]] = {
    "(mode %s)": ("paused",),
    "state %s -> %s": ("queued", "candidate"),
    "per rolling": ("2",),
    "a %s seller inquiry": ("reserved",),
    "(expected %s)": ("2",),
}
_SUPPRESSION_SAMPLE = "seller:manual, address:hard_bounce"


def _render(expr: str) -> str:
    """The MESSAGE a RAISE expression produces (format placeholders filled with samples)."""
    formatted = re.match(r"pg_catalog\.format\(\s*'((?:[^']|'')*)'", expr.strip())
    if formatted:
        text = formatted.group(1).replace("''", "'")
        for fragment, values in _SAMPLES.items():
            if fragment in text:
                for value in values:
                    text = text.replace("%s", value, 1)
        assert "%s" not in text, text
        return text
    expr = re.sub(r"pg_catalog\.array_to_string\([^)]*\)", f"'{_SUPPRESSION_SAMPLE}'", expr)
    return "".join(part.replace("''", "'") for part in re.findall(r"'((?:[^']|'')*)'", expr))


def _guard_messages() -> list[tuple[str, str]]:
    text = MIGRATION.read_text(encoding="utf-8")
    return [(m.group("code"), _render(m.group("expr"))) for m in _RAISE.finditer(text)]


class _Diag:
    def __init__(self, message: str) -> None:
        self.message_primary = message
        self.constraint_name = None


class _GuardError(psycopg.Error):
    """A psycopg error as raised by a guard trigger (SQLSTATE + MESSAGE, no DETAIL/HINT)."""

    def __init__(self, sqlstate: str, message: str) -> None:
        self._guard_state = sqlstate  # set first: psycopg reads ``sqlstate`` while initialising
        self._guard_diag = _Diag(message)
        super().__init__(message)

    @property  # type: ignore[override]
    def sqlstate(self) -> str:
        return self._guard_state

    @property
    def diag(self) -> Any:
        return self._guard_diag


GUARD_MESSAGES = _guard_messages()


def test_the_migration_has_the_expected_guard_messages() -> None:
    assert len(GUARD_MESSAGES) >= 80
    assert {code for code, _ in GUARD_MESSAGES} == {"SV002", "SV003"}


@pytest.mark.parametrize(("sqlstate", "message"), GUARD_MESSAGES, ids=[m for _, m in GUARD_MESSAGES])
def test_every_guard_message_matches_exactly_one_rule(sqlstate: str, message: str) -> None:
    matching = [r for r in GUARD_RULES if r.sqlstate == sqlstate and r.pattern.fullmatch(message)]
    assert len(matching) == 1, (message, [r.reason for r in matching])
    assert guard_rule_for(sqlstate, message) is matching[0]


def test_every_rule_is_reachable_from_the_migration() -> None:
    reached = {id(guard_rule_for(code, message)) for code, message in GUARD_MESSAGES}
    unreachable = [rule.reason for rule in GUARD_RULES if id(rule) not in reached]
    assert unreachable == []


@pytest.mark.parametrize(("sqlstate", "message"), GUARD_MESSAGES, ids=[m for _, m in GUARD_MESSAGES])
def test_mapped_errors_are_typed_and_carry_a_stable_reason(sqlstate: str, message: str) -> None:
    error = map_db_error(_GuardError(sqlstate, message))
    rule = guard_rule_for(sqlstate, message)
    assert rule is not None
    assert guard_reason(error) == rule.reason
    assert re.fullmatch(r"[a-z][a-z0-9_]{2,60}", rule.reason)
    assert error.code in HTTP_STATUS
    # The database message is never echoed (fixed, safe messages only).
    assert error.message != message
    for value in error.details.values():
        if isinstance(value, str):
            assert re.fullmatch(r"[a-z0-9_:]{1,60}", value), (rule.reason, value)


_EXPECTED: list[tuple[str, str, type[AppError], dict[str, Any]]] = [
    ("SV002", "the seller inquiry kill switch is active", VersionConflict, {"reason": "inquiry_kill_switch"}),
    (
        "SV002",
        "automatic seller inquiries are not enabled (mode paused)",
        VersionConflict,
        {"reason": "inquiry_mode_not_automatic", "mode": "paused"},
    ),
    (
        "SV002",
        "seller inquiry controls are not initialised for this workspace",
        VersionConflict,
        {"reason": "inquiry_controls_missing"},
    ),
    (
        "SV002",
        "seller inquiry cap reached: 2 per rolling 24 hours",
        RateLimited,
        {"reason": "inquiry_cap_reached", "phase": "reserve", "window": "24h", "limit": 2},
    ),
    (
        "SV002",
        "seller inquiry cap reached at dispatch: 5 per rolling 15 days",
        RateLimited,
        {"reason": "inquiry_cap_reached", "phase": "dispatch", "window": "15d", "limit": 5},
    ),
    (
        "SV002",
        "seller cooldown: this seller was contacted or reserved recently",
        RateLimited,
        {"reason": "seller_cooldown", "phase": "reserve"},
    ),
    (
        "SV002",
        "seller cooldown: another inquiry to this seller was transmitted recently",
        RateLimited,
        {"reason": "seller_cooldown", "phase": "dispatch"},
    ),
    (
        "SV002",
        "an active suppression applies: vehicle:complaint, seller:seller_opt_out, seller:seller_opt_out",
        VersionConflict,
        {"reason": "inquiry_suppressed", "suppressions": ["seller:seller_opt_out", "vehicle:complaint"]},
    ),
    (
        "SV002",
        "this seller already has an inquiry about this vehicle (possibly under another listing, cluster"
        " or merged seller identity); one initial inquiry per vehicle/seller pair",
        VersionConflict,
        {"reason": "inquiry_vehicle_seller_conflict"},
    ),
    (
        "SV002",
        "the listing changed since qualification; cancel the stale inquiry",
        VersionConflict,
        {"reason": "inquiry_listing_stale"},
    ),
    (
        "SV003",
        "the qualification snapshot is not the bound listing revision (number, semantic hash, price)",
        VersionConflict,
        {"reason": "inquiry_qualification_mismatch"},
    ),
    (
        "SV002",
        "the listing source is disabled or paused",
        SourcePaused,
        {"reason": "inquiry_source_paused"},
    ),
    (
        "SV002",
        "the seller inquiry authorization is revoked",
        Forbidden,
        {"reason": "inquiry_authorization_revoked"},
    ),
    (
        "SV002",
        "the mailbox worker credential is revoked or expired; the local backlog waits for a new one",
        Unauthenticated,
        {"reason": "mail_worker_credential_revoked"},
    ),
    (
        "SV003",
        "the reply mailbox is not the mailbox the inquiry was sent from",
        Forbidden,
        {"reason": "mailbox_binding_mismatch"},
    ),
    (
        "SV002",
        "an earlier send attempt is running, accepted or unresolved; never resend blindly",
        EmailDeliveryUncertain,
        {"reason": "send_attempt_unresolved"},
    ),
    (
        "SV002",
        "seller inquiry state queued -> candidate is not permitted",
        VersionConflict,
        {"reason": "inquiry_transition_not_permitted", "from_state": "queued", "to_state": "candidate"},
    ),
    (
        "SV003",
        "a reserved seller inquiry must hold a quota debit (ops.inquiry_quota_ledger)",
        ValidationFailed,
        {
            "guard": "evidence",
            "reason": "inquiry_evidence_missing",
            "evidence": "quota_debit",
            "state": "reserved",
        },
    ),
]


@pytest.mark.parametrize(
    ("sqlstate", "message", "kind", "details"), _EXPECTED, ids=[e[3]["reason"] for e in _EXPECTED]
)
def test_named_guards_map_to_the_documented_error(
    sqlstate: str, message: str, kind: type[AppError], details: dict[str, Any]
) -> None:
    error = map_db_error(_GuardError(sqlstate, message))
    assert type(error) is kind
    for key, value in details.items():
        assert error.details[key] == value, key


def test_email_delivery_uncertain_is_a_409_that_is_never_retried() -> None:
    error = map_db_error(
        _GuardError(
            "SV002", "the attempt lease expired: finalise it as uncertain and reconcile with evidence"
        )
    )
    assert error.code == ErrorCode.EMAIL_DELIVERY_UNCERTAIN
    assert HTTP_STATUS[error.code] == 409
    assert error.retryable is False
    assert guard_reason(error) == "send_attempt_lease_expired"


def test_cap_reached_reports_an_owner_lowered_limit() -> None:
    error = map_db_error(_GuardError("SV002", "seller inquiry cap reached: 0 per rolling 24 hours"))
    assert isinstance(error, RateLimited)
    assert error.details["limit"] == 0


@pytest.mark.parametrize(
    ("sqlstate", "message", "kind"),
    [
        ("SV002", "some future guard message", VersionConflict),
        ("SV003", "some future reference message", ValidationFailed),
        ("SV002", "the seller inquiry kill switch is active (with a suffix)", VersionConflict),
        ("SV002", "an active suppression applies: <script>", VersionConflict),
    ],
)
def test_unknown_messages_fall_back_to_the_generic_mapping(
    sqlstate: str, message: str, kind: type[AppError]
) -> None:
    error = map_db_error(_GuardError(sqlstate, message))
    assert type(error) is kind
    assert guard_reason(error) is None


def test_guard_messages_from_another_sqlstate_are_not_mapped_as_guards() -> None:
    # The same text under SV003 is not the SV002 kill-switch guard.
    error = map_db_error(_GuardError("SV003", "the seller inquiry kill switch is active"))
    assert isinstance(error, ValidationFailed)
    assert guard_reason(error) is None
    assert guard_rule_for("SV003", "the seller inquiry kill switch is active") is None


def test_suppression_hits_are_filtered_to_scope_reason_codes() -> None:
    error = map_db_error(
        _GuardError("SV002", "an active suppression applies: seller:manual, bad token, address:hard_bounce")
    )
    assert error.details["suppressions"] == ["address:hard_bounce", "seller:manual"]
