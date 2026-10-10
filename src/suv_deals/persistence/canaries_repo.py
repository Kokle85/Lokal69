"""Owner-controlled activation canaries (``ops.inquiry_activation_canaries``; spec 37.10; C1 item 12).

A canary is the activation-evidence test message of docs/seller_email_activation.md section 8
(rows 4-6): one synthetic message from the configured sender to an OWNER-CONTROLLED test address,
its receipt reconciliation and a correlated test reply. It is deliberately NOT an
``app.seller_inquiries`` row: it has no listing, no seller, no reservation and no quota debit
(``ops.inquiry_quota_ledger`` references inquiries only), so it can never count against the caps,
never block a vehicle/seller pair and never be one of the 15-day deals (the evaluation inputs
list canaries as ``is_canary`` records, which ``domain.evaluation`` excludes and only counts).

This module records evidence; it never sends anything (the sending wiring is the C2 CLI). Rules:

- Only the owner (``config:admin``) or a system principal writes; ``inquiries:read`` reads.
- The sender binding must be verified and unrevoked (its CURRENT version is bound; the database
  guard checks the same, ``SV003``). Alias verification and health are what the canary helps to
  prove, so they are not prerequisites. ``outlook_local`` names the sender's active mailbox
  worker (default: the one active mailbox of that sender).
- The target address is stored only as the SHA-256 of its canonical lower-case form; it is never
  returned, logged or audited, and the hash itself is returned to the owner (``config:admin``) and
  system principals only (an unsalted hash of an address can confirm a guessed address, so an
  ``inquiries:read`` reader, e.g. over MCP, gets ``None``). A target that equals a stored seller
  contact address of the workspace is refused (``canary_target_is_seller_contact``): a canary
  never goes to a seller.
- A canary is an e-mail path outside the seller caps, so it is bounded on its own: no canary is
  prepared while the workspace kill switch is on (``kill_switch_active``), and at most
  ``MAX_CANARIES_PER_24H`` (PROPOSED) are prepared per workspace in any rolling 24 hours (every
  canary counts, a cancelled one too: it may have been sent before it was cancelled;
  ``RATE_LIMITED``, ``activation_canary_volume``). The controls row is locked first (the inquiry
  lock order), so concurrent preparations are counted one after the other.
- Free text (purpose, cancel reason) never carries an address (no ``@``).
- The canary Message-ID is generated here as ``<canary-<id>@<sender domain>>``. It can never be
  parsed as an inquiry Message-ID, so a reply to it never correlates to a seller inquiry.
- Outcomes follow the database state machine: ``prepared`` -> ``accepted`` | ``uncertain`` |
  ``failed`` | ``cancelled``; ``uncertain`` -> ``accepted`` | ``failed`` | ``reply_correlated``;
  ``accepted`` -> ``reply_correlated``. Recording the same outcome again is an idempotent replay.
- A test reply is recorded only when its ``In-Reply-To``/``References`` name the canary's
  Message-ID (and, when the sender address is given, it hashes to the target).
- Evidence is a small allow-listed object: lower-case keys, scalar values, no ``@`` (no address),
  no control characters.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.replies import normalize_message_id, parse_message_id_list
from suv_deals.domain.seller_contacts import AddressError, canonicalize_address
from suv_deals.errors import NotFound, RateLimited, ValidationFailed, VersionConflict
from suv_deals.integrations.mime_builder import parse_inquiry_message_id
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.sellers_repo import require_inquiry_reader, require_inquiry_writer
from suv_deals.persistence.sender_bindings_repo import get_binding
from suv_deals.views.inquiries import recipient_address_visible

CanaryState = Literal["prepared", "accepted", "uncertain", "failed", "reply_correlated", "cancelled"]
CanaryOutcome = Literal["accepted", "uncertain", "failed"]

AUDIT_TARGET: Final = "inquiry_activation_canary"
CANARY_MESSAGE_ID_PREFIX: Final = "canary-"
MAX_EVIDENCE_KEYS: Final = 20
MAX_EVIDENCE_TEXT: Final = 200
MAX_LIST_LIMIT: Final = 200
#: Clock skew tolerated for a reported reply time.
REPLY_CLOCK_SKEW: Final = timedelta(minutes=5)
FINISHED_STATES: Final = frozenset({"failed", "reply_correlated", "cancelled"})
#: PROPOSED engineering default (C1 security review r2): canaries prepared per workspace in any
#: rolling 24 hours. Activation needs one per configured route plus a retry or two; a canary is an
#: e-mail outside the seller caps, so it is never unbounded.
MAX_CANARIES_PER_24H: Final = 5
CANARY_WINDOW: Final = timedelta(hours=24)

_EVIDENCE_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DOMAIN_RE: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_OUTCOME_FROM: Final[Mapping[CanaryOutcome, frozenset[str]]] = {
    "accepted": frozenset({"prepared", "uncertain"}),
    "uncertain": frozenset({"prepared"}),
    "failed": frozenset({"prepared", "uncertain"}),
}

_COLUMNS: Final = (
    "id, sender_binding_id, sender_binding_version, provider, mailbox_binding_id, target_address_hash,"
    " rfc_message_id, purpose, state, outcome_evidence, outcome_recorded_at, accepted_at,"
    " reply_message_id, reply_received_at, reply_recorded_at, reply_evidence, created_by, created_at,"
    " updated_at, version"
)
_SELECT: Final = f"select {_COLUMNS} from ops.inquiry_activation_canaries"  # noqa: S608 - fixed columns
_INSERT_SQL: Final = (
    "insert into ops.inquiry_activation_canaries (id, workspace_id, sender_binding_id,"  # noqa: S608
    " sender_binding_version, provider, mailbox_binding_id, target_address_hash, rfc_message_id,"
    " purpose, created_by) values (%(id)s, %(ws)s, %(sender)s, %(version)s, %(provider)s,"
    f" %(mailbox)s, %(hash)s, %(mid)s, %(purpose)s, %(by)s) returning {_COLUMNS}"
)
_OUTCOME_SQL: Final = (
    "update ops.inquiry_activation_canaries set state = %(state)s, outcome_evidence = %(evidence)s,"  # noqa: S608
    " outcome_recorded_at = %(now)s, accepted_at = coalesce(%(accepted)s, accepted_at),"
    f" version = version + 1 where workspace_id = %(ws)s and id = %(id)s returning {_COLUMNS}"
)
_REPLY_SQL: Final = (
    "update ops.inquiry_activation_canaries set state = 'reply_correlated',"  # noqa: S608
    " reply_message_id = %(mid)s, reply_received_at = %(received)s, reply_recorded_at = %(now)s,"
    " reply_evidence = %(evidence)s, version = version + 1"
    f" where workspace_id = %(ws)s and id = %(id)s returning {_COLUMNS}"
)
_CANCEL_SQL: Final = (
    "update ops.inquiry_activation_canaries set state = 'cancelled', version = version + 1"  # noqa: S608
    f" where workspace_id = %(ws)s and id = %(id)s returning {_COLUMNS}"
)


class CanaryRecord(BaseModel):
    """One activation canary (the target address is represented by its hash only)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    sender_binding_id: UUID
    sender_binding_version: int
    provider: EmailProviderKind
    mailbox_binding_id: UUID | None = None
    #: ``None`` for a reader that is neither the owner nor a system principal (`for_scopes`).
    target_address_hash: str | None
    rfc_message_id: str
    purpose: str
    state: CanaryState
    outcome_evidence: dict[str, Any]
    outcome_recorded_at: datetime | None = None
    accepted_at: datetime | None = None
    reply_message_id: str | None = None
    reply_received_at: datetime | None = None
    reply_recorded_at: datetime | None = None
    reply_evidence: dict[str, Any]
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    version: int

    @field_validator(
        "outcome_recorded_at",
        "accepted_at",
        "reply_received_at",
        "reply_recorded_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    @property
    def correlated(self) -> bool:
        """The full activation evidence: provider acceptance AND a correlated test reply."""
        return self.state == "reply_correlated"

    def for_actor(self, actor: ActorContext) -> CanaryRecord:
        """The record as ``actor`` may read it: the target hash for the owner and system only."""
        if actor.principal_kind == "system" or recipient_address_visible(actor.scopes):
            return self
        return self.model_copy(update={"target_address_hash": None})


# =============================================================================================
# Pure helpers
# =============================================================================================


def target_address_hash(address: str) -> str:
    """SHA-256 (hex) of the canonical, lower-cased address; raises ``ValidationFailed``."""
    try:
        canonical = canonicalize_address(address).canonical
    except AddressError as exc:
        raise ValidationFailed(
            "The canary target address is invalid", details={"reason": "canary_target_invalid"}
        ) from exc
    return hashlib.sha256(canonical.lower().encode("utf-8")).hexdigest()


def canary_message_id(canary_id: UUID, sender_from_address: str) -> str:
    """``<canary-<uuid>@<sender domain>>``: never parseable as an inquiry Message-ID."""
    domain = sender_from_address.rsplit("@", 1)[-1].strip().lower()
    if "@" not in sender_from_address or not _DOMAIN_RE.fullmatch(domain):
        raise ValidationFailed("The sender address has no usable domain", details={"reason": "sender_domain"})
    value = f"<{CANARY_MESSAGE_ID_PREFIX}{canary_id}@{domain}>"
    if normalize_message_id(value) != value or parse_inquiry_message_id(value) is not None:
        raise ValidationFailed("The canary Message-ID is invalid", details={"reason": "canary_message_id"})
    return value


def sanitize_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """An allow-listed evidence object: ids, codes, counts and flags only (never an address)."""
    if evidence is None:
        return {}
    if not isinstance(evidence, Mapping):
        raise ValidationFailed("Canary evidence must be an object", details={"reason": "evidence_shape"})
    if len(evidence) > MAX_EVIDENCE_KEYS:
        raise ValidationFailed("Too many canary evidence keys", details={"reason": "evidence_too_large"})
    clean: dict[str, Any] = {}
    for key, value in evidence.items():
        if not isinstance(key, str) or not _EVIDENCE_KEY_RE.fullmatch(key):
            raise ValidationFailed(
                "Canary evidence keys are lower-case names", details={"reason": "evidence_key"}
            )
        if value is None or isinstance(value, bool | int):
            clean[key] = value
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124 - NaN check
                raise ValidationFailed(
                    "Canary evidence numbers are finite", details={"reason": "evidence_value"}
                )
            clean[key] = value
        elif isinstance(value, str):
            if (
                len(value) > MAX_EVIDENCE_TEXT
                or "@" in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise ValidationFailed(
                    "Canary evidence text is a short code without addresses",
                    details={"reason": "evidence_value"},
                )
            clean[key] = value
        else:
            raise ValidationFailed("Canary evidence values are scalars", details={"reason": "evidence_value"})
    return clean


def _purpose(text: str) -> str:
    value = text.strip() if isinstance(text, str) else ""
    if (
        not 3 <= len(value) <= 500
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or "@" in value  # stored and shown to inquiries:read readers: never an address
    ):
        raise ValidationFailed(
            "The canary purpose must be 3-500 printable characters without an address",
            details={"reason": "purpose"},
        )
    return value


# =============================================================================================
# Reads
# =============================================================================================


async def get_canary(conn: Conn, actor: ActorContext, canary_id: UUID) -> CanaryRecord:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": canary_id},
        )
    if row is None:
        raise NotFound("Activation canary not found")
    return CanaryRecord.model_validate(row).for_actor(actor)


async def list_canaries(
    conn: Conn,
    actor: ActorContext,
    *,
    sender_binding_id: UUID | None = None,
    limit: int = 50,
) -> list[CanaryRecord]:
    """The newest canaries first (the activation evidence log)."""
    require_inquiry_reader(actor)
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SELECT + " where workspace_id = %(ws)s"
            " and (%(sender)s::uuid is null or sender_binding_id = %(sender)s::uuid)"
            " order by created_at desc, id desc limit %(limit)s",
            {"ws": actor.workspace_id, "sender": sender_binding_id, "limit": limit},
        )
    return [CanaryRecord.model_validate(r).for_actor(actor) for r in rows]


async def _locked(conn: Conn, actor: ActorContext, canary_id: UUID) -> CanaryRecord:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": actor.workspace_id, "id": canary_id},
        )
    if row is None:
        raise NotFound("Activation canary not found")
    return CanaryRecord.model_validate(row)


def _expect(record: CanaryRecord, expected_version: int | None) -> None:
    if expected_version is not None and record.version != expected_version:
        raise VersionConflict(
            "The activation canary changed; reload and retry", current_version=record.version
        )


async def _now(conn: Conn) -> datetime:
    async with mapped_errors():
        row = await fetch_one(conn, "select now() as now")
    assert row is not None
    return ensure_utc(row["now"])


# =============================================================================================
# Writes
# =============================================================================================


async def _seller_contact_hashes(conn: Conn, workspace_id: UUID, target_hash: str) -> bool:
    """True when a stored seller contact address of the workspace hashes to ``target_hash``."""
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select exists (select 1 from app.seller_contacts where workspace_id = %(ws)s"
            " and address is not null"
            " and encode(sha256(convert_to(lower(address), 'UTF8')), 'hex') = %(hash)s) as hit",
            {"ws": workspace_id, "hash": target_hash},
        )
    return bool(row and row["hit"])


_CANARY_VOLUME_SQL: Final = """
select count(*) as prepared, min(created_at) as oldest, now() as now
  from ops.inquiry_activation_canaries
 where workspace_id = %(ws)s and created_at > now() - %(window)s::interval
"""


async def _require_canary_room(conn: Conn, workspace_id: UUID) -> None:
    """Kill switch off and fewer than ``MAX_CANARIES_PER_24H`` canaries in the rolling window.

    Locks the controls row first (``FOR UPDATE``, the first inquiry lock), so two concurrent
    preparations are counted one after the other and a concurrent pause is seen or waits.
    """
    async with mapped_errors():
        controls = await fetch_one(
            conn,
            "select kill_switch from app.seller_inquiry_controls where workspace_id = %(ws)s for update",
            {"ws": workspace_id},
        )
        volume = await fetch_one(conn, _CANARY_VOLUME_SQL, {"ws": workspace_id, "window": CANARY_WINDOW})
    if controls is None or controls["kill_switch"]:
        raise ValidationFailed(
            "No activation canary while the inquiry kill switch is on",
            details={"reason": "kill_switch_active"},
        )
    assert volume is not None
    if int(volume["prepared"]) >= MAX_CANARIES_PER_24H:
        oldest = ensure_utc(volume["oldest"])
        wait = max(1, int((oldest + CANARY_WINDOW - ensure_utc(volume["now"])).total_seconds()) + 1)
        raise RateLimited(
            "Too many activation canaries in the last 24 hours",
            wait,
            details={"reason": "activation_canary_volume"},
        )


async def _active_mailbox(conn: Conn, workspace_id: UUID, sender_binding_id: UUID) -> UUID | None:
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select id from ops.mail_worker_bindings where workspace_id = %(ws)s"
            " and sender_binding_id = %(sender)s and state = 'active' order by created_at desc, id",
            {"ws": workspace_id, "sender": sender_binding_id},
        )
    return rows[0]["id"] if len(rows) == 1 else None


async def create_canary(
    conn: Conn,
    actor: ActorContext,
    *,
    sender_binding_id: UUID,
    target_address: str,
    purpose: str,
    mailbox_binding_id: UUID | None = None,
) -> CanaryRecord:
    """Record a ``prepared`` canary for the configured sender (nothing is sent here)."""
    require_inquiry_writer(actor)
    text = _purpose(purpose)
    target_hash = target_address_hash(target_address)
    await _require_canary_room(conn, actor.workspace_id)
    binding = await get_binding(conn, actor, sender_binding_id)
    problems: list[str] = []
    if binding.revoked:
        problems.append("sender_binding_revoked")
    elif binding.verified_at is None:
        problems.append("sender_binding_unverified")
    if problems:
        raise ValidationFailed(
            "The sender binding cannot carry an activation canary",
            details={"reason": "sender_not_ready", "problems": problems},
        )
    if await _seller_contact_hashes(conn, actor.workspace_id, target_hash):
        raise ValidationFailed(
            "A canary goes to an owner-controlled address, never to a seller",
            details={"reason": "canary_target_is_seller_contact"},
        )
    if binding.provider == EmailProviderKind.OUTLOOK_LOCAL and mailbox_binding_id is None:
        mailbox_binding_id = await _active_mailbox(conn, actor.workspace_id, binding.id)
        if mailbox_binding_id is None:
            raise ValidationFailed(
                "An outlook_local canary needs the sender's active mailbox worker",
                details={"reason": "canary_mailbox_missing"},
            )
    canary_id = uuid4()
    message_id = canary_message_id(canary_id, binding.from_address)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _INSERT_SQL,
            {
                "id": canary_id,
                "ws": actor.workspace_id,
                "sender": binding.id,
                "version": binding.version,
                "provider": binding.provider.value,
                "mailbox": mailbox_binding_id,
                "hash": target_hash,
                "mid": message_id,
                "purpose": text,
                "by": actor.principal_id,
            },
        )
    assert row is not None
    record = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "inquiry_canary.create",
        AUDIT_TARGET,
        record.id,
        new_version=record.version,
        reason="activation canary prepared",
        metadata={
            "sender_binding_id": str(binding.id),
            "sender_binding_version": binding.version,
            "provider": binding.provider.value,
            "has_mailbox": mailbox_binding_id is not None,
        },
    )
    return record


async def record_canary_outcome(
    conn: Conn,
    actor: ActorContext,
    canary_id: UUID,
    *,
    outcome: CanaryOutcome,
    evidence: Mapping[str, Any] | None = None,
    accepted_at: datetime | None = None,
    expected_version: int | None = None,
) -> CanaryRecord:
    """Record the provider/worker outcome of the canary send (idempotent for the same outcome)."""
    require_inquiry_writer(actor)
    if outcome not in _OUTCOME_FROM:
        raise ValidationFailed("Unknown canary outcome", details={"reason": "outcome"})
    clean = sanitize_evidence(evidence)
    record = await _locked(conn, actor, canary_id)
    if record.state == outcome:
        return record  # replay of the same outcome
    _expect(record, expected_version)
    if record.state not in _OUTCOME_FROM[outcome]:
        raise VersionConflict(
            f"The canary cannot move from {record.state} to {outcome}",
            current_version=record.version,
            reason="canary_state",
        )
    now = await _now(conn)
    accepted = None
    if outcome == "accepted":
        accepted = ensure_utc(accepted_at) if accepted_at is not None else now
        if accepted < record.created_at - REPLY_CLOCK_SKEW or accepted > now + REPLY_CLOCK_SKEW:
            raise ValidationFailed(
                "accepted_at is outside the canary's lifetime", details={"reason": "accepted_at"}
            )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _OUTCOME_SQL,
            {
                "state": outcome,
                "evidence": Jsonb({**clean, "outcome": outcome}),
                "now": now,
                "accepted": accepted,
                "ws": actor.workspace_id,
                "id": record.id,
            },
        )
    assert row is not None
    updated = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "inquiry_canary.outcome",
        AUDIT_TARGET,
        updated.id,
        prior_version=record.version,
        new_version=updated.version,
        reason=f"activation canary {outcome}",
        metadata={"from_state": record.state, "to_state": updated.state},
    )
    return updated


async def record_canary_reply(
    conn: Conn,
    actor: ActorContext,
    canary_id: UUID,
    *,
    reply_message_id: str,
    in_reply_to: str | Sequence[str] | None,
    references: str | Sequence[str] | None = None,
    received_at: datetime,
    from_address: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    expected_version: int | None = None,
) -> CanaryRecord:
    """Record the correlated test reply (``accepted``/``uncertain`` -> ``reply_correlated``).

    The reply must name the canary's Message-ID in ``In-Reply-To`` or ``References``; when
    ``from_address`` is given it must be the canary's target (compared by hash, never stored).
    """
    require_inquiry_writer(actor)
    normalized = normalize_message_id(reply_message_id)
    if normalized is None:
        raise ValidationFailed("The reply Message-ID is invalid", details={"reason": "reply_message_id"})
    clean = sanitize_evidence(evidence)
    record = await _locked(conn, actor, canary_id)
    if record.state == "reply_correlated":
        if record.reply_message_id == normalized:
            return record  # replay
        raise VersionConflict(
            "The canary already has a correlated reply", current_version=record.version, reason="canary_state"
        )
    _expect(record, expected_version)
    if record.state not in ("accepted", "uncertain"):
        raise VersionConflict(
            f"The canary cannot take a reply while {record.state}",
            current_version=record.version,
            reason="canary_state",
        )
    linked = set(parse_message_id_list(in_reply_to)) | set(parse_message_id_list(references))
    if record.rfc_message_id not in linked:
        raise ValidationFailed(
            "The reply does not reference the canary Message-ID",
            details={"reason": "canary_reply_not_correlated"},
        )
    if normalized == record.rfc_message_id:
        raise ValidationFailed(
            "The reply is the canary itself", details={"reason": "canary_reply_not_correlated"}
        )
    from_matches: bool | None = None
    if from_address is not None:
        from_matches = target_address_hash(from_address) == record.target_address_hash
        if not from_matches:
            raise ValidationFailed(
                "The reply does not come from the canary target",
                details={"reason": "canary_reply_from_mismatch"},
            )
    now = await _now(conn)
    received = ensure_utc(received_at)
    if received < record.created_at - REPLY_CLOCK_SKEW or received > now + REPLY_CLOCK_SKEW:
        raise ValidationFailed(
            "received_at is outside the canary's lifetime", details={"reason": "received_at"}
        )
    stored = {**clean, "correlated_by": "message_id"}
    if from_matches is not None:
        stored["from_matches_target"] = from_matches
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _REPLY_SQL,
            {
                "mid": normalized,
                "received": received,
                "now": now,
                "evidence": Jsonb(stored),
                "ws": actor.workspace_id,
                "id": record.id,
            },
        )
    assert row is not None
    updated = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "inquiry_canary.reply",
        AUDIT_TARGET,
        updated.id,
        prior_version=record.version,
        new_version=updated.version,
        reason="activation canary reply correlated",
        metadata={"from_state": record.state, "from_matches_target": from_matches},
    )
    return updated


async def cancel_canary(
    conn: Conn, actor: ActorContext, canary_id: UUID, *, reason: str, expected_version: int | None = None
) -> CanaryRecord:
    """Cancel a ``prepared`` canary that will not be sent (idempotent)."""
    require_inquiry_writer(actor)
    text = _purpose(reason)
    record = await _locked(conn, actor, canary_id)
    if record.state == "cancelled":
        return record
    _expect(record, expected_version)
    if record.state != "prepared":
        raise VersionConflict(
            "Only a prepared canary can be cancelled", current_version=record.version, reason="canary_state"
        )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _CANCEL_SQL,
            {"ws": actor.workspace_id, "id": record.id},
        )
    assert row is not None
    updated = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "inquiry_canary.cancel",
        AUDIT_TARGET,
        updated.id,
        prior_version=record.version,
        new_version=updated.version,
        reason=text,
    )
    return updated


__all__ = [
    "AUDIT_TARGET",
    "CANARY_MESSAGE_ID_PREFIX",
    "CANARY_WINDOW",
    "FINISHED_STATES",
    "MAX_CANARIES_PER_24H",
    "MAX_EVIDENCE_KEYS",
    "CanaryOutcome",
    "CanaryRecord",
    "CanaryState",
    "canary_message_id",
    "cancel_canary",
    "create_canary",
    "get_canary",
    "list_canaries",
    "record_canary_outcome",
    "record_canary_reply",
    "sanitize_evidence",
    "target_address_hash",
]
