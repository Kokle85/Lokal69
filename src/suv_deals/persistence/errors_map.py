"""Translate PostgreSQL errors into typed, safely reportable `AppError`s (spec 21, docs/schema.md).

Every persistence function runs its SQL inside `mapped_errors()`, so callers only ever see
`AppError` subclasses. Messages are fixed, safe strings: they never contain SQL, parameter
values, row contents or server details. The stable constraint name (asserted by the schema
tests) is the only database identifier exposed, in `details`, for check/not-null violations.

| SQLSTATE | Meaning | AppError |
|---|---|---|
| `SV001` | append-only history UPDATE/DELETE | `ValidationFailed` (guard `append_only`) |
| `SV002` | transition not permitted / refused flow | guard table below, else `VersionConflict` |
| `SV003` | dangling / cross-workspace / fixture ref. | guard table below, else `ValidationFailed` |
| `SV004` | frozen column modified | `ValidationFailed` (guard `frozen`) |
| `SV005` | monotonic version would decrease | `VersionConflict` |
| `SV006` | detail generation never allocated | `ValidationFailed` (guard `generation`) |
| `23505` unique_violation | concurrent duplicate | `VersionConflict` (override per constraint) |
| `23503` foreign_key_violation | referenced row missing or in another workspace | `NotFound` |
| `23514` check / `23502` not null / `22xxx` data | invalid value | `ValidationFailed` |
| `42501` insufficient_privilege (RLS/grants) | row outside the workspace or missing grant | `Forbidden` |
| `40001` / `40P01` serialization / deadlock | retry the whole transaction | `TransientConflict` (retryable) |
| `55P03` lock_not_available (lock_timeout) | row busy | `TransientConflict` (retryable) |
| `57014` query_canceled (statement_timeout) | too slow | `StatementTimeout` (retryable) |
| `25P02` in_failed_sql_transaction | an earlier error was swallowed | `TransactionAborted` (not retryable) |
| `08xxx` / OperationalError | connection lost | `DependencyUnavailable` (retryable) |
| anything else | unexpected | `AppError(INTERNAL_ERROR)` |

Only `TransientConflict` and `StatementTimeout` prove that the transaction was rolled back, so
only they may re-run a whole unit of work automatically (`transactions.retry_transient`). A lost
connection (`DependencyUnavailable`) is ambiguous when it happens during COMMIT: the commit may
have succeeded, so it is never retried blindly.

Seller-inquiry guards (migration ``20261006001000_seller_inquiries``) raise ``SV002`` (refused
flow) and ``SV003`` (inconsistent reference, evidence missing at COMMIT) with a fixed MESSAGE
and no DETAIL/HINT. `GUARD_RULES` matches that MESSAGE (anchored regular expressions; captured
values are enum-like tokens only) and returns a typed error whose ``details["reason"]`` is a
stable machine reason, so workers can hold, cancel or suppress without parsing text:

Guard (examples) -> AppError, ``details["reason"]``:

- kill switch / mode / controls missing -> `VersionConflict`, ``inquiry_kill_switch`` /
  ``inquiry_mode_not_automatic`` / ``inquiry_controls_missing``;
- rolling cap at reserve or dispatch -> `RateLimited`, ``inquiry_cap_reached`` (+ ``phase``,
  ``window``, ``limit``); seller cooldown -> `RateLimited`, ``seller_cooldown`` (+ ``phase``);
- active suppression -> `VersionConflict`, ``inquiry_suppressed`` (+ ``suppressions``: the
  ``scope:reason`` codes); one inquiry per vehicle/seller pair -> `VersionConflict`,
  ``inquiry_vehicle_seller_conflict``;
- stale listing / qualification -> `VersionConflict` (``inquiry_listing_stale``,
  ``inquiry_availability_stale``, ``inquiry_qualification_mismatch``, ...) or `SourcePaused`
  (``inquiry_source_paused``);
- revoked authorization / sender -> `Forbidden`, ``inquiry_authorization_revoked`` /
  ``sender_binding_revoked``; changed sender -> `VersionConflict`, ``sender_binding_changed``;
- an earlier attempt running/accepted/unresolved, an expired attempt lease, a retry without
  proof -> `EmailDeliveryUncertain` (``send_attempt_unresolved``, ``send_attempt_lease_expired``,
  ``retry_without_proof``);
- revoked or expired worker credential -> `Unauthenticated`, ``mail_worker_credential_revoked``;
  revoked mailbox, tombstoned binding, cross-mailbox reply -> `Forbidden`
  (``mailbox_binding_revoked``, ``inquiry_binding_tombstoned``, ``mailbox_binding_mismatch``);
- evidence missing at COMMIT (deferred constraint triggers) -> `ValidationFailed`,
  ``inquiry_evidence_missing`` (+ ``evidence``, ``state``).

An unknown ``SV002``/``SV003`` message keeps the generic mapping above.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from suv_deals.errors import (
    AppError,
    DependencyUnavailable,
    EmailDeliveryUncertain,
    ErrorCode,
    Forbidden,
    NotFound,
    RateLimited,
    SourcePaused,
    Unauthenticated,
    ValidationFailed,
    VersionConflict,
)

SV_APPEND_ONLY: Final = "SV001"
SV_TRANSITION: Final = "SV002"
SV_REFERENCE: Final = "SV003"
SV_FROZEN: Final = "SV004"
SV_MONOTONIC: Final = "SV005"
SV_GENERATION: Final = "SV006"

UNIQUE_VIOLATION: Final = "23505"
FOREIGN_KEY_VIOLATION: Final = "23503"
CHECK_VIOLATION: Final = "23514"
NOT_NULL_VIOLATION: Final = "23502"
EXCLUSION_VIOLATION: Final = "23P01"
INSUFFICIENT_PRIVILEGE: Final = "42501"
SERIALIZATION_FAILURE: Final = "40001"
DEADLOCK_DETECTED: Final = "40P01"
LOCK_NOT_AVAILABLE: Final = "55P03"
QUERY_CANCELED: Final = "57014"
IN_FAILED_TRANSACTION: Final = "25P02"

_TRANSIENT: Final = frozenset({SERIALIZATION_FAILURE, DEADLOCK_DETECTED, LOCK_NOT_AVAILABLE})

ErrorFactory = Callable[[], AppError]


class LeaseLost(AppError):
    """The worker no longer holds the job/event lease: roll back the WHOLE transaction.

    Raised by fenced updates (heartbeat, completion, failure, outbox lifecycle) that matched
    zero rows, and by `transactions.lock_job` when revalidation fails. A newer lease holder may
    already own the work; a late worker must never commit domain writes (spec 13 fencing).
    """

    def __init__(self, message: str = "The work lease was lost; nothing was committed") -> None:
        super().__init__(ErrorCode.VERSION_CONFLICT, message, retryable=False)


class TransactionAborted(AppError):
    """A statement failed earlier and the caller swallowed its error: nothing was committed.

    PostgreSQL turns COMMIT of an aborted transaction into a silent ROLLBACK; the unit-of-work
    helpers raise this instead so a partial "success" can never be reported.
    """

    def __init__(self, message: str = "An earlier statement failed; nothing was committed") -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, retryable=False)


class TransientConflict(AppError):
    """Serialization failure, deadlock or lock timeout: retry the whole transaction."""

    def __init__(self, message: str = "Concurrent update; retry the request") -> None:
        super().__init__(ErrorCode.VERSION_CONFLICT, message, retryable=True, retry_after_seconds=1)


class StatementTimeout(AppError):
    """A statement inside the transaction hit ``statement_timeout`` (57014): the transaction
    was aborted, so nothing was committed and the whole unit of work may be re-run."""

    def __init__(self, message: str = "The database did not answer in time") -> None:
        super().__init__(ErrorCode.DEPENDENCY_UNAVAILABLE, message, retryable=True, retry_after_seconds=2)


# --------------------------------------------------------------------------------------------
# Seller-inquiry guard messages (migration 20261006001000) -> typed errors with a stable reason
# --------------------------------------------------------------------------------------------

GuardBuilder = Callable[[Mapping[str, str]], AppError]


@dataclass(frozen=True, slots=True)
class GuardRule:
    """One guard MESSAGE pattern of the seller-inquiry migration and the error it maps to."""

    sqlstate: str
    pattern: re.Pattern[str]
    reason: str
    build: GuardBuilder


_SUPPRESSION_HIT_RE: Final = re.compile(r"^[a-z_]{1,40}:[a-z_]{1,40}$")
_WINDOWS: Final = {"24 hours": "24h", "15 days": "15d"}


def _conflict(message: str, reason: str, **extra: Any) -> GuardBuilder:
    return lambda groups: VersionConflict(message, reason=reason, **extra, **dict(groups))


def _forbidden(message: str, reason: str) -> GuardBuilder:
    return lambda _groups: Forbidden(message, details={"reason": reason})


def _uncertain(message: str, reason: str) -> GuardBuilder:
    return lambda _groups: EmailDeliveryUncertain(message, details={"reason": reason})


def _invalid(message: str, reason: str, **extra: str) -> GuardBuilder:
    return lambda groups: ValidationFailed(message, details={"reason": reason, **extra, **dict(groups)})


def _cap(phase: str) -> GuardBuilder:
    def build(groups: Mapping[str, str]) -> AppError:
        return RateLimited(
            "The seller inquiry rate cap is reached; the inquiry waits for the rolling window",
            details={
                "reason": "inquiry_cap_reached",
                "phase": phase,
                "window": _WINDOWS[groups["window"]],
                "limit": int(groups["limit"]),
            },
        )

    return build


def _cooldown(phase: str) -> GuardBuilder:
    return lambda _groups: RateLimited(
        "The seller was contacted recently; the inquiry waits for the seller cooldown",
        details={"reason": "seller_cooldown", "phase": phase},
    )


def _suppressed(groups: Mapping[str, str]) -> AppError:
    hits = sorted({h.strip() for h in groups["hits"].split(",") if _SUPPRESSION_HIT_RE.fullmatch(h.strip())})
    return VersionConflict(
        "An active suppression applies to this inquiry", reason="inquiry_suppressed", suppressions=hits[:20]
    )


def _evidence(evidence: str) -> GuardBuilder:
    return lambda groups: ValidationFailed(
        "The inquiry state lacks its required evidence; nothing was committed",
        details={
            "guard": "evidence",
            "reason": "inquiry_evidence_missing",
            "evidence": evidence,
            **dict(groups),
        },
    )


def _rule(sqlstate: str, pattern: str, reason: str, build: GuardBuilder) -> GuardRule:
    return GuardRule(sqlstate, re.compile(pattern), reason, build)


_STATE: Final = r"(?P<state>[a-z_]{3,40})"
_A_SENT: Final = "an earlier send attempt is running, accepted or unresolved; "

#: Every SV002/SV003 MESSAGE raised by the seller-inquiry guards, in migration order. The tests
#: check that each message of the migration matches exactly one rule.
GUARD_RULES: Final[tuple[GuardRule, ...]] = (
    # --- reservation / dispatch preflight (app.seller_inquiry_preflight) ---------------------
    _rule(
        SV_TRANSITION,
        r"^seller inquiry controls are not initialised for this workspace$",
        "inquiry_controls_missing",
        _conflict("Seller inquiries are not configured for this workspace", "inquiry_controls_missing"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the seller inquiry kill switch is active$",
        "inquiry_kill_switch",
        _conflict("The seller inquiry kill switch is active", "inquiry_kill_switch"),
    ),
    _rule(
        SV_TRANSITION,
        r"^automatic seller inquiries are not enabled"
        r" \(mode (?P<mode>disabled_until_sender_ready|paused|automatic)\)$",
        "inquiry_mode_not_automatic",
        _conflict("Automatic seller inquiries are not enabled", "inquiry_mode_not_automatic"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the inquiry is not bound to the current seller inquiry authorization version$",
        "inquiry_authorization_changed",
        _conflict(
            "The standing authorization changed; re-qualify the inquiry", "inquiry_authorization_changed"
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the seller inquiry authorization is revoked$",
        "inquiry_authorization_revoked",
        _forbidden("The standing seller inquiry authorization is revoked", "inquiry_authorization_revoked"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the seller inquiry authorization is not yet effective$",
        "inquiry_authorization_not_effective",
        _conflict(
            "The standing seller inquiry authorization is not yet effective",
            "inquiry_authorization_not_effective",
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the inquiry language is not covered by the authorization$",
        "inquiry_language_not_authorized",
        _conflict(
            "The inquiry language is not covered by the authorization", "inquiry_language_not_authorized"
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^this seller already has an inquiry about this vehicle \(possibly under another listing, cluster"
        r" or merged seller identity\); one initial inquiry per vehicle/seller pair$",
        "inquiry_vehicle_seller_conflict",
        _conflict("This seller already has an inquiry about this vehicle", "inquiry_vehicle_seller_conflict"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the qualifying listing is quarantined or has an identity conflict$",
        "inquiry_listing_quarantined",
        _conflict("The listing is quarantined or has an identity conflict", "inquiry_listing_quarantined"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the vehicle is reported sold or removed$",
        "inquiry_vehicle_unavailable",
        _conflict("The vehicle is reported sold or removed", "inquiry_vehicle_unavailable"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the qualifying listing is not eligible under a profile covered by the authorization$",
        "inquiry_profile_out_of_scope",
        _conflict(
            "The listing is not eligible under a profile covered by the authorization",
            "inquiry_profile_out_of_scope",
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the listing changed since qualification; cancel the stale inquiry$",
        "inquiry_listing_stale",
        _conflict("The listing changed since qualification; the inquiry is stale", "inquiry_listing_stale"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the listing availability changed since qualification; cancel the stale inquiry$",
        "inquiry_availability_stale",
        _conflict(
            "The listing availability changed since qualification; the inquiry is stale",
            "inquiry_availability_stale",
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the listing source is disabled or paused$",
        "inquiry_source_paused",
        lambda _g: SourcePaused(
            "The listing's source is disabled or paused", details={"reason": "inquiry_source_paused"}
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the sender binding is revoked$",
        "sender_binding_revoked",
        _forbidden("The sending account binding is revoked", "sender_binding_revoked"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the sender binding is not verified and healthy$",
        "sender_binding_not_ready",
        _conflict("The sending account is not verified and healthy", "sender_binding_not_ready"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the sender binding changed since the inquiry was bound; never switch accounts silently$",
        "sender_binding_changed",
        _conflict("The sending account changed since the inquiry was bound", "sender_binding_changed"),
    ),
    _rule(
        SV_TRANSITION,
        r"^an official dealer contact is a recipient only for a dealer seller$",
        "recipient_not_dealer",
        _conflict(
            "An official dealer contact is a recipient only for a dealer seller", "recipient_not_dealer"
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the recipient contact is no longer verified$",
        "recipient_unverified",
        _conflict("The recipient contact is no longer verified", "recipient_unverified"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the recipient address differs from the verified contact$",
        "recipient_changed",
        _conflict("The recipient address differs from the verified contact", "recipient_changed"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the inquiry language is not the positively resolved seller/advertisement language$",
        "inquiry_language_unresolved",
        _conflict("The inquiry language is not the resolved seller language", "inquiry_language_unresolved"),
    ),
    _rule(
        SV_TRANSITION,
        r"^an active suppression applies: (?P<hits>[a-z_:, ]{1,2000})$",
        "inquiry_suppressed",
        _suppressed,
    ),
    _rule(
        SV_TRANSITION,
        r"^seller cooldown: this seller was contacted or reserved recently$",
        "seller_cooldown",
        _cooldown("reserve"),
    ),
    _rule(
        SV_TRANSITION,
        r"^seller inquiry cap reached at dispatch:"
        r" (?P<limit>[0-9]{1,4}) per rolling (?P<window>24 hours|15 days)$",
        "inquiry_cap_reached",
        _cap("dispatch"),
    ),
    _rule(
        SV_TRANSITION,
        r"^seller cooldown: another inquiry to this seller was transmitted recently$",
        "seller_cooldown",
        _cooldown("dispatch"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the inquiry holds no quota debit$",
        "inquiry_quota_debit_missing",
        _conflict("The inquiry holds no quota debit", "inquiry_quota_debit_missing"),
    ),
    _rule(
        SV_TRANSITION,
        "^" + re.escape(_A_SENT) + r"(it must be reconciled first|never resend blindly)$",
        "send_attempt_unresolved",
        _uncertain(
            "An earlier send attempt is running, accepted or unresolved; it must be reconciled first",
            "send_attempt_unresolved",
        ),
    ),
    # --- inquiry state machine (app.seller_inquiries_guard) ---------------------------------
    _rule(
        SV_TRANSITION,
        r"^a seller inquiry is created as candidate, qualifying or held_facts and reserved by a transition$",
        "inquiry_initial_state_invalid",
        _conflict(
            "A seller inquiry starts as candidate, qualifying or held_facts", "inquiry_initial_state_invalid"
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^seller inquiry state (?P<from_state>[a-z_]{3,40}) -> (?P<to_state>[a-z_]{3,40}) is not permitted$",
        "inquiry_transition_not_permitted",
        _conflict("This seller inquiry state change is not permitted", "inquiry_transition_not_permitted"),
    ),
    _rule(
        SV_TRANSITION,
        r"^a \(possibly\) transmitted inquiry can never be re-qualified$",
        "inquiry_requalification_refused",
        _conflict(
            "A (possibly) transmitted inquiry can never be re-qualified", "inquiry_requalification_refused"
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^leaving suppression needs a new audit event about this inquiry \(requalification_audit_id\)$",
        "inquiry_requalification_audit_missing",
        _conflict(
            "Leaving suppression needs a new audit event about this inquiry",
            "inquiry_requalification_audit_missing",
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^send attempts are exhausted$",
        "send_attempts_exhausted",
        _conflict("The send attempts of this inquiry are exhausted", "send_attempts_exhausted"),
    ),
    _rule(
        SV_TRANSITION,
        r"^a retry needs proof that the previous attempt never reached the provider$",
        "retry_without_proof",
        _uncertain(
            "A retry needs proof that the previous attempt never reached the provider", "retry_without_proof"
        ),
    ),
    # --- send attempts (ops.email_delivery_attempts_guard) ----------------------------------
    _rule(
        SV_TRANSITION,
        r"^(a send intent is recorded only for an inquiry that just moved to sending"
        r"|a send attempt is recorded as a running send intent before any external I/O"
        r"|a send intent needs a live lease"
        r"|send attempt numbers are consecutive \(expected [0-9]{1,4}\))$",
        "send_intent_invalid",
        _conflict("The send intent is not valid for the inquiry's current state", "send_intent_invalid"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the attempt lease expired: finalise it as uncertain and reconcile with evidence$",
        "send_attempt_lease_expired",
        _uncertain(
            "The send attempt lease expired; it is uncertain and must be reconciled with evidence",
            "send_attempt_lease_expired",
        ),
    ),
    # --- quota ledger (ops.inquiry_quota_ledger_guard) --------------------------------------
    _rule(
        SV_TRANSITION,
        r"^(a quota debit is recorded unreleased|a quota debit cannot be backdated"
        r"|a quota debit is taken only when the inquiry is reserved)$",
        "quota_debit_invalid",
        _conflict("The quota debit is not valid for the inquiry's current state", "quota_debit_invalid"),
    ),
    _rule(
        SV_TRANSITION,
        r"^seller inquiry cap reached: (?P<limit>[0-9]{1,4}) per rolling (?P<window>24 hours|15 days)$",
        "inquiry_cap_reached",
        _cap("reserve"),
    ),
    _rule(
        SV_TRANSITION,
        r"^only a never-transmitted cancelled or suppressed inquiry releases its quota debit$",
        "quota_release_refused",
        _conflict(
            "Only a never-transmitted cancelled or suppressed inquiry releases its quota debit",
            "quota_release_refused",
        ),
    ),
    # --- contacts, mailbox route ------------------------------------------------------------
    _rule(
        SV_TRANSITION,
        r"^a changed \(superseded\) seller contact cannot become current again$",
        "seller_contact_superseded",
        _conflict("A superseded seller contact cannot become current again", "seller_contact_superseded"),
    ),
    _rule(
        SV_TRANSITION,
        r"^(the mailbox worker binding is revoked"
        r"|bindings are published only to an active mailbox worker binding)$",
        "mailbox_binding_revoked",
        _forbidden("The mailbox worker binding is revoked", "mailbox_binding_revoked"),
    ),
    _rule(
        SV_TRANSITION,
        r"^the mailbox worker credential is revoked or expired; the local backlog waits for a new one$",
        "mail_worker_credential_revoked",
        lambda _g: Unauthenticated(
            "The mailbox worker credential is revoked or expired",
            details={"reason": "mail_worker_credential_revoked"},
        ),
    ),
    _rule(
        SV_TRANSITION,
        r"^the inquiry binding was revoked \(tombstoned\) for this mailbox$",
        "inquiry_binding_tombstoned",
        _forbidden("The inquiry binding was revoked for this mailbox", "inquiry_binding_tombstoned"),
    ),
    _rule(
        SV_TRANSITION,
        r"^a tombstoned inquiry binding is never re-published$",
        "inquiry_binding_tombstoned",
        _conflict("A revoked inquiry binding is never re-published", "inquiry_binding_tombstoned"),
    ),
    _rule(
        SV_TRANSITION,
        r"^a reply is stored before any quarantine release$",
        "reply_release_invalid",
        _conflict("A reply is stored before any quarantine release", "reply_release_invalid"),
    ),
    _rule(
        SV_TRANSITION,
        r"^(idempotency conflicts and spam stay quarantined"
        r"|releasing a quarantine needs a recorded verification)$",
        "reply_quarantine_release_refused",
        _conflict("This reply cannot leave quarantine", "reply_quarantine_release_refused"),
    ),
    # --- SV003: identity, references and bindings -------------------------------------------
    _rule(
        SV_REFERENCE,
        r"^(a seller entity can only be merged into an unmerged root entity"
        r"|a seller entity that absorbed other entities cannot be merged itself)$",
        "seller_merge_invalid",
        _invalid("The seller entity merge is not permitted", "seller_merge_invalid"),
    ),
    _rule(
        SV_REFERENCE,
        r"^the inquiry seller entity was merged; the identity must use the surviving entity$",
        "seller_entity_merged",
        _conflict("The inquiry's seller entity was merged into another entity", "seller_entity_merged"),
    ),
    _rule(
        SV_REFERENCE,
        r"^(a cluster identity needs a confirmed vehicle cluster containing the qualifying listing"
        r"|the listing belongs to a confirmed vehicle cluster; the inquiry identity must use the cluster)$",
        "inquiry_identity_not_canonical",
        _conflict(
            "The inquiry does not use the canonical vehicle identity", "inquiry_identity_not_canonical"
        ),
    ),
    _rule(
        SV_REFERENCE,
        r"^the qualification snapshot is not the bound listing revision \(number, semantic hash, price\)$",
        "inquiry_qualification_mismatch",
        _conflict(
            "The qualification snapshot is not the bound listing revision", "inquiry_qualification_mismatch"
        ),
    ),
    _rule(
        SV_REFERENCE,
        r"^a send attempt must use exactly the bound sender account \(never another account\)$",
        "sender_binding_mismatch",
        _conflict("A send attempt must use exactly the bound sender account", "sender_binding_mismatch"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a send attempt must reuse the inquiry's stable Message-ID$",
        "message_id_mismatch",
        _conflict("A send attempt must reuse the inquiry's stable Message-ID", "message_id_mismatch"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a suppression removal needs an audit event about this suppression$",
        "suppression_removal_audit_missing",
        _invalid("A suppression removal needs its audit event", "suppression_removal_audit_missing"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a mailbox worker binding needs a live credential carrying only mail:ingest$",
        "mail_worker_credential_invalid",
        _invalid(
            "A mailbox worker binding needs a live mail:ingest-only credential",
            "mail_worker_credential_invalid",
        ),
    ),
    _rule(
        SV_REFERENCE,
        r"^replies are stored only for an inquiry that was \(possibly\) transmitted$",
        "inquiry_not_transmitted",
        _conflict(
            "Replies are stored only for an inquiry that was (possibly) transmitted",
            "inquiry_not_transmitted",
        ),
    ),
    _rule(
        SV_REFERENCE,
        r"^(the reply mailbox is not the mailbox the inquiry was sent from"
        r"|an inquiry binding is published only to the mailbox it was sent from)$",
        "mailbox_binding_mismatch",
        _forbidden("The mailbox is not the mailbox the inquiry was sent from", "mailbox_binding_mismatch"),
    ),
    _rule(
        SV_REFERENCE,
        r"^the binding version was never published to this mailbox \(or is a tombstone\)$",
        "binding_version_unpublished",
        _conflict("The binding version was never published to this mailbox", "binding_version_unpublished"),
    ),
    _rule(
        SV_REFERENCE,
        r"^ingest is recorded only through the active mailbox binding's own credential$",
        "mail_worker_credential_mismatch",
        _forbidden(
            "Ingest is recorded only through the mailbox's own credential", "mail_worker_credential_mismatch"
        ),
    ),
    _rule(
        SV_REFERENCE,
        r"^(absence is availability evidence only for a finished complete scan"
        r"|a seller availability statement needs a verified, non-quarantined seller reply"
        r"|the seller reply is about another vehicle than this listing)$",
        "availability_evidence_invalid",
        _invalid("The availability evidence is not valid", "availability_evidence_invalid"),
    ),
    # --- SV003 at COMMIT: evidence each state needs (app.seller_inquiry_assert_evidence) ------
    _rule(
        SV_REFERENCE,
        rf"^a {_STATE} seller inquiry must hold a quota debit \(ops\.inquiry_quota_ledger\)$",
        "inquiry_evidence_missing",
        _evidence("quota_debit"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a never-transmitted cancelled or suppressed inquiry must release its quota debit$",
        "inquiry_evidence_missing",
        _evidence("quota_release"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a sending inquiry needs its committed send intent \(a running ops\.email_delivery_attempts row\)$",
        "inquiry_evidence_missing",
        _evidence("send_intent"),
    ),
    _rule(
        SV_REFERENCE,
        rf"^a {_STATE} seller inquiry cannot keep a running send attempt$",
        "inquiry_evidence_missing",
        _evidence("running_attempt"),
    ),
    _rule(
        SV_REFERENCE,
        r"^an uncertain inquiry needs its unresolved uncertain send attempt$",
        "inquiry_evidence_missing",
        _evidence("uncertain_attempt"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a definite failure needs proof of non-submission or a definite provider rejection$",
        "inquiry_evidence_missing",
        _evidence("non_submission_proof"),
    ),
    _rule(
        SV_REFERENCE,
        r"^provider acceptance needs an accepted attempt or a correlated inbound message$",
        "inquiry_evidence_missing",
        _evidence("acceptance"),
    ),
    _rule(
        SV_REFERENCE,
        r"^a replied inquiry needs a correlated seller reply$",
        "inquiry_evidence_missing",
        _evidence("seller_reply"),
    ),
)


def guard_rule_for(sqlstate: str, message: str) -> GuardRule | None:
    """The guard rule whose pattern matches this SQLSTATE and MESSAGE, if any."""
    for rule in GUARD_RULES:
        if rule.sqlstate == sqlstate and rule.pattern.fullmatch(message):
            return rule
    return None


def _guard_error(state: str, exc: BaseException) -> AppError | None:
    if not isinstance(exc, psycopg.Error):
        return None
    message = exc.diag.message_primary
    if not isinstance(message, str):
        return None
    for rule in GUARD_RULES:
        if rule.sqlstate != state:
            continue
        match = rule.pattern.fullmatch(message)
        if match is not None:
            groups = {k: v for k, v in match.groupdict().items() if v is not None}
            return rule.build(groups)
    return None


def guard_reason(error: AppError) -> str | None:
    """The stable machine ``reason`` of a mapped guard error (``None`` for other errors)."""
    reason = error.details.get("reason")
    return reason if isinstance(reason, str) else None


def sqlstate_of(exc: BaseException) -> str | None:
    return exc.sqlstate if isinstance(exc, psycopg.Error) else None


def constraint_of(exc: BaseException) -> str | None:
    if not isinstance(exc, psycopg.Error):
        return None
    name = exc.diag.constraint_name
    return name if isinstance(name, str) and name else None


def is_rerunnable(exc: BaseException) -> bool:
    """True only for failures that prove the transaction rolled back (safe to re-run)."""
    return isinstance(exc, TransientConflict | StatementTimeout)


def is_retryable_db_error(exc: BaseException) -> bool:
    if isinstance(exc, AppError):
        return exc.retryable
    if isinstance(exc, psycopg.OperationalError):
        return True
    state = sqlstate_of(exc)
    return state is not None and (state in _TRANSIENT or state == QUERY_CANCELED)


def map_db_error(
    exc: BaseException,
    *,
    unique: Mapping[str, ErrorFactory] | None = None,
    foreign_key: Mapping[str, ErrorFactory] | None = None,
) -> AppError:
    """Typed AppError for a database exception. `AppError`s pass through unchanged.

    `unique` / `foreign_key` map constraint names to caller-specific errors (for example an
    idempotency key -> `IdempotencyConflict`); unlisted constraints use the defaults above.
    """
    if isinstance(exc, AppError):
        return exc
    state = sqlstate_of(exc)
    constraint = constraint_of(exc)
    if state is None:
        if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
            return DependencyUnavailable("The database is unavailable")
        return AppError(ErrorCode.INTERNAL_ERROR, "Database operation failed")
    if state == SV_APPEND_ONLY:
        return ValidationFailed(
            "History records are append-only; write a superseding record", details={"guard": "append_only"}
        )
    if state in (SV_TRANSITION, SV_REFERENCE):
        guarded = _guard_error(state, exc)
        if guarded is not None:
            return guarded
    if state == SV_TRANSITION:
        return VersionConflict("The state transition is not permitted from the current state")
    if state == SV_REFERENCE:
        return ValidationFailed(
            "A referenced record is missing or not eligible for this record", details={"guard": "reference"}
        )
    if state == SV_FROZEN:
        return ValidationFailed("These fields are immutable once written", details={"guard": "frozen"})
    if state == SV_MONOTONIC:
        return VersionConflict("A newer version was already recorded; reload and retry")
    if state == SV_GENERATION:
        return ValidationFailed(
            "The observation generation was never allocated", details={"guard": "generation"}
        )
    if state == UNIQUE_VIOLATION:
        if unique and constraint is not None and constraint in unique:
            return unique[constraint]()
        return VersionConflict("A conflicting record was written concurrently; reload and retry")
    if state == FOREIGN_KEY_VIOLATION:
        if foreign_key and constraint is not None and constraint in foreign_key:
            return foreign_key[constraint]()
        # Missing and foreign-workspace references are indistinguishable by design.
        return NotFound("A referenced record was not found")
    if state in (CHECK_VIOLATION, NOT_NULL_VIOLATION, EXCLUSION_VIOLATION):
        details = {"constraint": constraint} if constraint else None
        return ValidationFailed("The value violates a data constraint", details=details)
    if state.startswith("22"):
        return ValidationFailed("A value has an invalid format or size")
    if state == INSUFFICIENT_PRIVILEGE:
        return Forbidden("The operation is not permitted for this workspace")
    if state in _TRANSIENT:
        if state == LOCK_NOT_AVAILABLE:
            return TransientConflict("The record is busy; retry shortly")
        return TransientConflict()
    if state == QUERY_CANCELED:
        return StatementTimeout()
    if state == IN_FAILED_TRANSACTION:
        return TransactionAborted()
    if state.startswith("08") or isinstance(exc, psycopg.OperationalError):
        return DependencyUnavailable("The database is unavailable")
    return AppError(ErrorCode.INTERNAL_ERROR, "Database operation failed")


@asynccontextmanager
async def mapped_errors(
    *,
    unique: Mapping[str, ErrorFactory] | None = None,
    foreign_key: Mapping[str, ErrorFactory] | None = None,
) -> AsyncIterator[None]:
    """Convert psycopg errors raised in the block (including a deferred-constraint failure at
    COMMIT when it wraps `Database.transaction`) into typed `AppError`s."""
    try:
        yield
    except psycopg.Error as exc:
        raise map_db_error(exc, unique=unique, foreign_key=foreign_key) from exc
