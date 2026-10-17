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
- A send path claims the canary with `claim_for_send` (D1 item 3) BEFORE any transport:
  ``prepared -> uncertain`` in one guarded statement after the controls lock, re-checking the
  kill switch, the mode, the standing authorization, the bound sender binding (unrevoked,
  verified, same version) and the desktop mailbox; a refusal (`CanaryClaimRefused`) names every
  reason and changes nothing.
- A test reply is recorded only when its ``In-Reply-To``/``References`` name the canary's
  Message-ID (and, when the sender address is given, it hashes to the target).
- Evidence is a small allow-listed object: lower-case keys, scalar values, no ``@`` (no address),
  no control characters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.canary import (
    CANARY_INTENT_TTL,
    CANARY_MESSAGE_ID_PREFIX,
    MAX_CANARY_INTENTS_PAGE,
    CanaryClaimDecision,
    CanaryIntent,
    CanaryIntentBatch,
    CanaryRefusalReason,
    CanaryReplyReport,
    CanaryReport,
    canary_body,
    canary_body_hash,
    canary_subject,
    canary_target_hash,
)
from suv_deals.domain.canary import canary_message_id as domain_canary_message_id
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.replies import normalize_message_id, parse_message_id_list
from suv_deals.domain.seller_contacts import AddressError
from suv_deals.errors import NotFound, RateLimited, ValidationFailed, VersionConflict
from suv_deals.integrations.mime_builder import parse_inquiry_message_id
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.mail_workers_repo import WorkerIdentity, mailbox_mismatch, require_active_mailbox
from suv_deals.persistence.sellers_repo import lock_controls, require_inquiry_reader, require_inquiry_writer
from suv_deals.persistence.sender_bindings_repo import SenderBindingRecord, get_binding
from suv_deals.views.inquiries import CanaryEvidenceState, recipient_address_visible

CanaryState = Literal["prepared", "accepted", "uncertain", "failed", "reply_correlated", "cancelled"]
CanaryOutcome = Literal["accepted", "uncertain", "failed"]

AUDIT_TARGET: Final = "inquiry_activation_canary"
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
#: The atomic claim (D1 item 3): ONE statement re-checks every database gate and moves the canary
#: ``prepared -> uncertain``. Runs after the controls row is locked (``FOR UPDATE``, the first
#: inquiry lock: a pause, a mode change or an authorization change waits for the claim or is seen
#: by it). The sender binding and the desktop mailbox rows are held ``FOR SHARE`` by the same
#: statement, so a revocation of either either committed before (and is seen: the row no longer
#: qualifies) or waits until the claim committed; it can never land between check and transition.
_CLAIM_SQL: Final = (
    "with sender as (select b.id from ops.email_sender_bindings b"  # noqa: S608 - fixed columns
    " where b.workspace_id = %(ws)s and b.id = %(sender)s and b.version = %(sender_version)s"
    " and b.revoked_at is null and b.verified_at is not null for share),"
    " mailbox as (select m.id from ops.mail_worker_bindings m"
    " join ops.api_credentials c on c.workspace_id = m.workspace_id and c.id = m.credential_id"
    " where m.workspace_id = %(ws)s and m.id = %(mailbox)s::uuid and m.sender_binding_id = %(sender)s"
    " and m.state = 'active' and c.revoked_at is null and c.expires_at > clock_timestamp()"
    " for share of m)"
    " update ops.inquiry_activation_canaries k set state = 'uncertain', outcome_evidence = %(evidence)s,"
    " outcome_recorded_at = now(), version = k.version + 1"
    " where k.workspace_id = %(ws)s and k.id = %(id)s and k.state = 'prepared' and k.version = %(version)s"
    " and k.sender_binding_id = %(sender)s and k.sender_binding_version = %(sender_version)s"
    " and k.mailbox_binding_id is not distinct from %(mailbox)s::uuid"
    " and exists (select 1 from app.seller_inquiry_controls s where s.workspace_id = %(ws)s"
    " and not s.kill_switch and s.mode = 'automatic')"
    " and exists (select 1 from app.seller_inquiry_authorizations a where a.workspace_id = %(ws)s"
    " and a.revoked_at is null and a.version = (select max(v.version)"
    " from app.seller_inquiry_authorizations v where v.workspace_id = %(ws)s))"
    " and %(authorization_effective)s"
    " and exists (select 1 from sender)"
    " and (k.mailbox_binding_id is null or exists (select 1 from mailbox))"
    f" returning {_COLUMNS.replace('id, ', 'k.id, ', 1)}"
)
_CANCEL_SQL: Final = (
    "update ops.inquiry_activation_canaries set state = 'cancelled', version = version + 1"  # noqa: S608
    f" where workspace_id = %(ws)s and id = %(id)s returning {_COLUMNS}"
)


class CanaryClaimRefused(VersionConflict):
    """`claim_for_send` refused: nothing was claimed and nothing changed (``409``,
    ``details.reason = canary_claim_refused``, ``details.problems`` = the codes)."""

    def __init__(self, problems: Sequence[str], *, current_version: int | None) -> None:
        codes = list(dict.fromkeys(problems)) or ["CANARY_NOT_PREPARED"]
        super().__init__(
            "The activation canary cannot be claimed for sending; nothing was claimed",
            reason="canary_claim_refused",
            problems=codes,
            current_version=current_version,
        )
        self.problems: tuple[str, ...] = tuple(codes)


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
    """SHA-256 (hex) of the canonical, lower-cased address (`domain.canary.canary_target_hash`);
    raises ``ValidationFailed``."""
    try:
        return canary_target_hash(address)
    except AddressError as exc:
        raise ValidationFailed(
            "The canary target address is invalid", details={"reason": "canary_target_invalid"}
        ) from exc


def canary_message_id(canary_id: UUID, sender_from_address: str) -> str:
    """``<canary-<uuid>@<sender domain>>``: never parseable as an inquiry Message-ID."""
    try:
        value = domain_canary_message_id(canary_id, sender_from_address)
    except ValueError:
        raise ValidationFailed(
            "The sender address has no usable domain", details={"reason": "sender_domain"}
        ) from None
    if parse_inquiry_message_id(value) is not None:  # pragma: no cover - the formats differ
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


def evidence_state(
    canaries: Sequence[CanaryRecord], sender: SenderBindingRecord | None
) -> tuple[CanaryEvidenceState, str]:
    """``(state, detail)`` of the activation-canary evidence for the configured sender binding.

    ``complete``: a correlated test reply was recorded for a canary of the binding's CURRENT
    version (an identity change needs a new canary); otherwise the newest canary's state of that
    version (``prepared`` / ``accepted`` / ``uncertain`` / ``failed`` / ``cancelled``), ``stale``
    (only older versions), ``none`` or ``no_sender``. ``canaries`` are newest first. Shared by
    ``suv-deals canary status``, ``doctor`` and ``GET /api/activation/canary-evidence``.
    """
    if sender is None:
        return "no_sender", "no configured sender binding"
    mine = [c for c in canaries if c.sender_binding_id == sender.id]
    if not mine:
        return "none", "no canary recorded for the configured sender binding"
    current = [c for c in mine if c.sender_binding_version == sender.version]
    if any(c.correlated for c in current):
        return "complete", f"correlated test reply recorded (sender binding v{sender.version})"
    if not current:
        return (
            "stale",
            f"canaries exist only for older versions of the sender binding (now v{sender.version})",
        )
    latest = current[0]
    detail = f"newest canary {latest.state} (sender binding v{sender.version}); no correlated reply"
    if latest.state == "reply_correlated":  # pragma: no cover - a correlated one is complete above
        return "complete", detail
    return latest.state, detail


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


def _hash_or_none(address: str) -> str | None:
    try:
        return canary_target_hash(address)
    except AddressError:
        return None


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
    own = [a for a in (binding.from_address, binding.reply_to_address) if a]
    if any(_hash_or_none(address) == target_hash for address in own):
        # A message to the sending mailbox itself proves neither delivery nor a reply path.
        raise ValidationFailed(
            "A canary goes to another owner-controlled address than the sender itself",
            details={"reason": "canary_target_is_sender"},
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
    from_address_hash: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    expected_version: int | None = None,
) -> CanaryRecord:
    """Record the correlated test reply (``accepted``/``uncertain`` -> ``reply_correlated``).

    The reply must name the canary's Message-ID in ``In-Reply-To`` or ``References``; when
    ``from_address`` (or, from the desktop worker, its ``from_address_hash``) is given it must be
    the canary's target (compared by hash, never stored).
    """
    require_inquiry_writer(actor)
    if from_address is not None and from_address_hash is not None:
        raise ValidationFailed("Give the reply sender once", details={"reason": "reply_from"})
    if from_address_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", from_address_hash):
        raise ValidationFailed("The reply sender hash is invalid", details={"reason": "reply_from"})
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
    sender_hash = target_address_hash(from_address) if from_address is not None else from_address_hash
    if sender_hash is not None:
        from_matches = sender_hash == record.target_address_hash
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


_CLAIM_DIAGNOSIS_SQL: Final = """
select s.kill_switch, s.mode,
       b.revoked_at is not null as sender_revoked, b.verified_at is null as sender_unverified,
       b.version as sender_version,
       m.state as mailbox_state, m.sender_binding_id as mailbox_sender,
       coalesce(c.revoked_at is null and c.expires_at > clock_timestamp(), false) as credential_live
  from (select 1) one
  left join app.seller_inquiry_controls s on s.workspace_id = %(ws)s
  left join ops.email_sender_bindings b on b.workspace_id = %(ws)s and b.id = %(sender)s
  left join ops.mail_worker_bindings m on m.workspace_id = %(ws)s and m.id = %(mailbox)s::uuid
  left join ops.api_credentials c on c.workspace_id = m.workspace_id and c.id = m.credential_id
"""


async def _claim_problems(
    conn: Conn,
    actor: ActorContext,
    record: CanaryRecord,
    *,
    expected_version: int,
    authorization: Sequence[str],
) -> list[str]:
    """Why `claim_for_send` refused (codes in the ``canary send`` vocabulary; empty = none found)."""
    problems: list[str] = []
    if record.state != "prepared":
        problems.append("CANARY_NOT_PREPARED")
    if record.version != expected_version:
        problems.append("CANARY_VERSION_CHANGED")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _CLAIM_DIAGNOSIS_SQL,
            {
                "ws": actor.workspace_id,
                "sender": record.sender_binding_id,
                "mailbox": record.mailbox_binding_id,
            },
        )
    assert row is not None
    if row["kill_switch"] is None:
        problems.append("CONTROLS_MISSING")
    else:
        if row["kill_switch"]:
            problems.append("KILL_SWITCH_ACTIVE")
        if row["mode"] != "automatic":
            problems.append("CONTROLS_MODE_NOT_AUTOMATIC")
    problems.extend(code.upper() for code in authorization)
    if row["sender_version"] is None:
        problems.append("SENDER_BINDING_MISSING")
    else:
        if row["sender_revoked"]:
            problems.append("SENDER_BINDING_REVOKED")
        elif row["sender_unverified"]:
            problems.append("SENDER_BINDING_UNVERIFIED")
        if row["sender_version"] != record.sender_binding_version:
            problems.append("CANARY_SENDER_VERSION_CHANGED")
    if record.mailbox_binding_id is not None and not (
        row["mailbox_state"] == "active"
        and row["mailbox_sender"] == record.sender_binding_id
        and row["credential_live"]
    ):
        problems.append("CANARY_MAILBOX_NOT_ACTIVE")
    return problems


async def claim_for_send(
    conn: Conn, actor: ActorContext, canary_id: UUID, *, expected_version: int, send_token: str
) -> CanaryRecord:
    """Claim a ``prepared`` canary for its ONE transmission: ``prepared -> uncertain`` (D1 item 3).

    Spec 37.5: the attempt is durable BEFORE the external I/O, so a crash, or a second
    ``canary send``, can never transmit it twice; the transport's outcome then follows with
    `record_canary_outcome` (``uncertain -> accepted | failed``). Lock order: the controls row
    (``FOR UPDATE``) first, then ONE guarded statement (`_CLAIM_SQL`) that re-checks the kill
    switch, the mode (``automatic``), the latest standing authorization (unrevoked; its effective
    window is evaluated under the controls lock, which every authorization change takes), the
    bound sender binding (unrevoked, verified, the canary's version; ``FOR SHARE``) and, for a
    canary bound to a desktop mailbox, that mailbox (active, of the same sender, its credential
    neither expired nor revoked; ``FOR SHARE``), and moves the canary in the same statement.

    The process-level gates (``SELLER_INQUIRY_MODE``, the process kill switch, the canary send
    switch) are the caller's (``cli_commands.canary``). Replaying the same ``send_token`` returns
    the claimed record; anything else raises `CanaryClaimRefused` naming every reason, and nothing
    changes. Audited ``inquiry_canary.claim``. Nothing is sent here.
    """
    require_inquiry_writer(actor)
    if not isinstance(send_token, str) or not re.fullmatch(r"[0-9a-f]{16,64}", send_token):
        raise ValidationFailed("The send token is a hex string", details={"reason": "send_token"})
    evidence = sanitize_evidence({"phase": "transport_started", "send_token": send_token})
    await lock_controls(conn, actor.workspace_id)
    record = await _locked(conn, actor, canary_id)
    if record.state == "uncertain" and record.outcome_evidence.get("send_token") == send_token:
        return record  # replay of this claim
    from suv_deals.persistence import inquiries_repo  # noqa: PLC0415 - avoids an import cycle at load

    authorization = inquiries_repo.authorization_problems(
        await inquiries_repo.current_authorization(conn, actor), await _now(conn)
    )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _CLAIM_SQL,
            {
                "ws": actor.workspace_id,
                "id": record.id,
                "version": expected_version,
                "sender": record.sender_binding_id,
                "sender_version": record.sender_binding_version,
                "mailbox": record.mailbox_binding_id,
                "authorization_effective": not authorization,
                "evidence": Jsonb({**evidence, "outcome": "uncertain"}),
            },
        )
    if row is None:
        problems = await _claim_problems(
            conn, actor, record, expected_version=expected_version, authorization=authorization
        )
        raise CanaryClaimRefused(problems, current_version=record.version)
    claimed = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        "inquiry_canary.claim",
        AUDIT_TARGET,
        claimed.id,
        prior_version=record.version,
        new_version=claimed.version,
        reason="activation canary claimed for its one send",
        metadata={"from_state": record.state, "to_state": claimed.state},
    )
    return claimed


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


# =============================================================================================
# The outlook_local canary transport: the desktop mailbox worker (F3, wave D2)
# =============================================================================================
#
# ``canary send`` (the owner's step) claims the canary (``prepared -> uncertain``, `claim_for_send`)
# and, for ``outlook_local``, publishes it to the canary's desktop mailbox worker in the SAME
# transaction (`publish_for_desktop`). The worker then
#
# 1. lists it (`desktop_canary_intents`: the fixed canary rendering with the sender binding's
#    identity and the target HASH; never the address),
# 2. claims it immediately before ``.Send`` (`claim_desktop_canary`: always evaluated fresh,
#    granted to ONE worker id only; the kill switch, the mode, the standing authorization, the
#    sender binding version and the publication window are re-checked under the controls lock),
# 3. sends it once to the owner-controlled address configured on the owner's machine whose hash
#    is the target hash, and reports Sent Items evidence (`report_desktop_canary`:
#    ``sent_items_confirmed`` -> ``accepted``; ``refused_before_send`` / ``transport_rejected`` ->
#    ``failed``; ``submitted_to_outbox`` / ``send_call_failed`` stay ``uncertain``),
# 4. reports the owner's reply that names the canary Message-ID (`record_desktop_canary_reply`:
#    headers only, the sender as its hash) -> ``reply_correlated`` = complete activation evidence.
#
# The transport phase lives in ``outcome_evidence`` (``phase``: ``transport_started`` ->
# ``published`` -> ``desktop_claimed`` -> ``submitted`` | ``send_call_failed``), so no column
# changes; every step advances the canary version by one (the database guard).

PHASE_TRANSPORT_STARTED: Final = "transport_started"
PHASE_PUBLISHED: Final = "published"
PHASE_DESKTOP_CLAIMED: Final = "desktop_claimed"
PHASE_SUBMITTED: Final = "submitted"
PHASE_SEND_CALL_FAILED: Final = "send_call_failed"
_LISTED_PHASES: Final = (PHASE_PUBLISHED, PHASE_DESKTOP_CLAIMED)
_CLAIMED_PHASES: Final = frozenset({PHASE_DESKTOP_CLAIMED, PHASE_SUBMITTED, PHASE_SEND_CALL_FAILED})
DESKTOP_CLAIM_AUDIT: Final = "inquiry_canary.desktop_claim"

_EVIDENCE_SQL: Final = (
    "update ops.inquiry_activation_canaries set outcome_evidence = %(evidence)s,"  # noqa: S608
    " outcome_recorded_at = now(), version = version + 1"
    f" where workspace_id = %(ws)s and id = %(id)s and state = 'uncertain' returning {_COLUMNS}"
)
_DESKTOP_LIST_SQL: Final = (
    _SELECT
    + " where workspace_id = %(ws)s and mailbox_binding_id = %(mailbox)s and provider = 'outlook_local'"
    " and state = 'uncertain' and outcome_evidence ->> 'phase' = any(%(phases)s)"
    " order by created_at, id limit %(limit)s"
)


def _phase(record: CanaryRecord) -> str | None:
    value = record.outcome_evidence.get("phase")
    return value if isinstance(value, str) else None


def published_at(record: CanaryRecord) -> datetime | None:
    """When ``canary send`` handed the canary to the desktop worker (``None``: not published)."""
    value = record.outcome_evidence.get("published_at")
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


async def _set_evidence(
    conn: Conn, actor: ActorContext, record: CanaryRecord, evidence: Mapping[str, Any], *, action: str
) -> CanaryRecord:
    clean = sanitize_evidence({**evidence, "outcome": "uncertain"})
    async with mapped_errors():
        row = await fetch_one(
            conn, _EVIDENCE_SQL, {"evidence": Jsonb(clean), "ws": actor.workspace_id, "id": record.id}
        )
    if row is None:
        raise VersionConflict(
            "The activation canary is no longer uncertain",
            current_version=record.version,
            reason="canary_state",
        )
    updated = CanaryRecord.model_validate(row)
    await audit.record(
        conn,
        actor,
        action,
        AUDIT_TARGET,
        updated.id,
        prior_version=record.version,
        new_version=updated.version,
        reason=f"activation canary {clean.get('phase')}",
        metadata={"phase": clean.get("phase")},
    )
    return updated


async def publish_for_desktop(
    conn: Conn, actor: ActorContext, canary_id: UUID, *, send_token: str
) -> CanaryRecord:
    """Hand a claimed ``outlook_local`` canary to its desktop mailbox worker (``canary send``).

    Run in the transaction of `claim_for_send` (same ``send_token``): the canary must be
    ``uncertain`` in phase ``transport_started`` of that token. Idempotent for the same token.
    """
    require_inquiry_writer(actor)
    record = await _locked(conn, actor, canary_id)
    if record.provider != EmailProviderKind.OUTLOOK_LOCAL or record.mailbox_binding_id is None:
        raise ValidationFailed(
            "Only an outlook_local canary is sent by the desktop mailbox worker",
            details={"reason": "canary_not_desktop"},
        )
    if record.state != "uncertain" or record.outcome_evidence.get("send_token") != send_token:
        raise VersionConflict(
            "The canary is not claimed by this send", current_version=record.version, reason="canary_state"
        )
    if _phase(record) == PHASE_PUBLISHED:
        return record  # replay
    if _phase(record) != PHASE_TRANSPORT_STARTED:
        raise VersionConflict(
            "The canary transport already moved on", current_version=record.version, reason="canary_state"
        )
    now = await _now(conn)
    return await _set_evidence(
        conn,
        actor,
        record,
        {"phase": PHASE_PUBLISHED, "send_token": send_token, "published_at": now.isoformat()},
        action="inquiry_canary.publish",
    )


async def _controls_gate(conn: Conn, actor: ActorContext) -> list[str]:
    """Kill switch, mode and standing authorization (codes; empty = open). Controls locked first."""
    from suv_deals.persistence import inquiries_repo  # noqa: PLC0415 - avoids an import cycle at load

    controls = await lock_controls(conn, actor.workspace_id)
    codes: list[str] = []
    if controls is None:
        codes.append("CONTROLS_MISSING")
    else:
        async with mapped_errors():
            row = await fetch_one(
                conn,
                "select kill_switch, mode from app.seller_inquiry_controls where workspace_id = %(ws)s",
                {"ws": actor.workspace_id},
            )
        assert row is not None
        if row["kill_switch"]:
            codes.append("KILL_SWITCH_ACTIVE")
        if row["mode"] != "automatic":
            codes.append("CONTROLS_MODE_NOT_AUTOMATIC")
    authorization = await inquiries_repo.current_authorization(conn, actor)
    codes.extend(c.upper() for c in inquiries_repo.authorization_problems(authorization, await _now(conn)))
    return codes


async def _worker_canary(
    conn: Conn, worker: WorkerIdentity, actor: ActorContext, canary_id: UUID
) -> CanaryRecord:
    """The worker's OWN outlook_local canary, locked (anything else: the mailbox-mismatch 403)."""
    try:
        record = await _locked(conn, actor, canary_id)
    except NotFound:
        raise mailbox_mismatch() from None
    if (
        record.provider != EmailProviderKind.OUTLOOK_LOCAL
        or record.mailbox_binding_id != worker.mailbox_binding_id
    ):
        raise mailbox_mismatch()
    return record


async def desktop_canary_intents(
    conn: Conn, worker: WorkerIdentity, *, request_id: str, process_closed: bool
) -> CanaryIntentBatch:
    """Published canaries of the worker's mailbox (``GET /v1/mail-workers/canary-intents``).

    A canary whose publication window ended, or whose sender binding is no longer the bound
    unrevoked version, is listed ``expired`` (the worker refuses it ``intent_expired`` and the
    canary fails honestly: it was never sent). ``process_closed``: this process's settings forbid
    sending (reported as ``kill_switch_active``).
    """
    await require_active_mailbox(conn, worker)
    actor = worker.system_actor(request_id)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _DESKTOP_LIST_SQL,
            {
                "ws": worker.workspace_id,
                "mailbox": worker.mailbox_binding_id,
                "phases": list(_LISTED_PHASES),
                "limit": MAX_CANARY_INTENTS_PAGE,
            },
        )
        controls = await fetch_one(
            conn,
            "select kill_switch from app.seller_inquiry_controls where workspace_id = %(ws)s",
            {"ws": worker.workspace_id},
        )
    now = await _now(conn)
    intents: list[CanaryIntent] = []
    for row in rows:
        record = CanaryRecord.model_validate(row)
        start = published_at(record)
        if start is None:
            continue
        binding = await get_binding(conn, actor, record.sender_binding_id)
        stale = (
            binding.revoked or binding.verified_at is None or binding.version != record.sender_binding_version
        )
        intents.append(
            CanaryIntent(
                canary_id=record.id,
                mailbox_binding_id=worker.mailbox_binding_id,
                binding_id=binding.id,
                binding_version=record.sender_binding_version,
                account_id=binding.account_id,
                from_address=binding.from_address,
                from_display_name=binding.display_name,
                reply_to_address=binding.reply_to_address,
                target_address_hash=record.target_address_hash or "",
                subject=canary_subject(record.id),
                body_text=canary_body(record.id),
                rfc_message_id=record.rfc_message_id,
                body_hash=canary_body_hash(record.id),
                created_at=start,
                not_after=start + CANARY_INTENT_TTL,
                expired=stale or now >= start + CANARY_INTENT_TTL,
            )
        )
    kill = controls is None or bool(controls["kill_switch"]) or process_closed
    return CanaryIntentBatch(intents=tuple(intents), kill_switch_active=kill)


async def claim_desktop_canary(
    conn: Conn,
    worker: WorkerIdentity,
    *,
    canary_id: UUID,
    claim_attempt_id: str,
    worker_id: str,
    request_id: str,
    process_gate: str | None = None,
) -> CanaryClaimDecision:
    """Fresh revalidation immediately before ``.Send`` of a published canary (never replayed).

    Lock order: controls (``FOR UPDATE``) -> canary. Granted to ONE worker id only (a second
    worker id is refused ``intent_invalid``, ``ALREADY_CLAIMED``); the same worker id may claim
    again (a lost response). Audited (``inquiry_canary.desktop_claim``) either way.
    """
    await require_active_mailbox(conn, worker)
    actor = worker.system_actor(request_id)
    gate = await _controls_gate(conn, actor)
    record = await _worker_canary(conn, worker, actor, canary_id)
    phase = _phase(record)
    claimant = worker_id[:128]
    refusal: tuple[CanaryRefusalReason, str] | None = None
    start = published_at(record)
    if process_gate is not None:
        refusal = ("kill_switch", process_gate)
    elif record.state != "uncertain" or phase not in _LISTED_PHASES or start is None:
        refusal = ("intent_invalid", "CANARY_NOT_PUBLISHED")
    elif phase == PHASE_DESKTOP_CLAIMED and record.outcome_evidence.get("claimed_by") != claimant:
        refusal = ("intent_invalid", "ALREADY_CLAIMED")
    elif await _now(conn) >= start + CANARY_INTENT_TTL:
        refusal = ("intent_expired", "CANARY_EXPIRED")
    elif "KILL_SWITCH_ACTIVE" in gate or "CONTROLS_MISSING" in gate or "CONTROLS_MODE_NOT_AUTOMATIC" in gate:
        refusal = ("kill_switch", gate[0])
    elif gate:
        refusal = ("intent_invalid", gate[0])
    else:
        binding = await get_binding(conn, actor, record.sender_binding_id)
        if binding.revoked or binding.verified_at is None or binding.version != record.sender_binding_version:
            refusal = ("intent_invalid", "SENDER_BINDING_CHANGED")
    if refusal is None and phase != PHASE_DESKTOP_CLAIMED:
        record = await _set_evidence(
            conn,
            actor,
            record,
            {
                **record.outcome_evidence,
                "phase": PHASE_DESKTOP_CLAIMED,
                "claimed_by": claimant,
                "claim_attempt_id": claim_attempt_id[:64],
            },
            action="inquiry_canary.desktop_claimed",
        )
    await audit.record(
        conn,
        worker.actor(request_id),
        DESKTOP_CLAIM_AUDIT,
        AUDIT_TARGET,
        record.id,
        reason="canary claim refused" if refusal else "canary claim granted",
        metadata={
            "proceed": refusal is None,
            "detail": refusal[1] if refusal else None,
            "claim_attempt_id": claim_attempt_id[:64],
            "worker_id": claimant,
        },
        outcome="denied" if refusal else "succeeded",
    )
    if refusal is None:
        return CanaryClaimDecision(canary_id=record.id, proceed=True)
    return CanaryClaimDecision(canary_id=record.id, proceed=False, refusal_reason=refusal[0])


async def report_desktop_canary(
    conn: Conn, worker: WorkerIdentity, *, report: CanaryReport, request_id: str
) -> CanaryRecord:
    """Record the desktop worker's submission evidence of a canary (see the section comment).

    Evidence of a transmission (``submitted_to_outbox``, ``send_call_failed``,
    ``sent_items_confirmed``) is accepted only after a granted claim; a refusal or a transport
    rejection fails the canary (it proves nothing either way: the owner prepares a new one). A
    report for a finished canary changes nothing.
    """
    await require_active_mailbox(conn, worker)
    worker.require_mailbox(report.mailbox_binding_id)
    actor = worker.system_actor(request_id)
    record = await _worker_canary(conn, worker, actor, report.canary_id)
    if record.state in ("failed", "reply_correlated", "cancelled", "prepared"):
        return record  # finished (or never published): nothing to change
    phase = _phase(record)
    base: dict[str, Any] = {
        "send_token": record.outcome_evidence.get("send_token"),
        "published_at": record.outcome_evidence.get("published_at"),
        "claimed_by": record.outcome_evidence.get("claimed_by"),
        "reported_state": report.state,
        "error_code": report.error_code,
    }
    if report.state in ("refused_before_send", "transport_rejected"):
        if record.state == "accepted":
            return record  # Sent Items evidence already exists: a late refusal changes nothing
        return await record_canary_outcome(
            conn,
            actor,
            record.id,
            outcome="failed",
            evidence={**base, "phase": report.state, "refusal_reason": report.refusal_reason},
        )
    if phase not in _CLAIMED_PHASES and record.state == "uncertain":
        raise VersionConflict(
            "No claim was granted for this canary",
            current_version=record.version,
            reason="canary_not_claimed",
        )
    if report.state == "sent_items_confirmed":
        if record.state == "accepted":
            return record
        observed = normalize_message_id(report.observed_internet_message_id)
        return await record_canary_outcome(
            conn,
            actor,
            record.id,
            outcome="accepted",
            evidence={
                **base,
                "phase": "sent_items_confirmed",
                "message_id_matches": observed == record.rfc_message_id,
            },
            accepted_at=report.sent_at or report.reported_at,
        )
    if record.state != "uncertain":
        return record
    new_phase = PHASE_SUBMITTED if report.state == "submitted_to_outbox" else PHASE_SEND_CALL_FAILED
    if phase == new_phase or (phase == PHASE_SUBMITTED and new_phase == PHASE_SEND_CALL_FAILED):
        return record
    return await _set_evidence(
        conn, actor, record, {**base, "phase": new_phase}, action="inquiry_canary.report"
    )


async def record_desktop_canary_reply(
    conn: Conn, worker: WorkerIdentity, *, reply: CanaryReplyReport, request_id: str
) -> CanaryRecord:
    """The owner's correlated test reply, seen by the desktop worker (headers only)."""
    await require_active_mailbox(conn, worker)
    worker.require_mailbox(reply.mailbox_binding_id)
    actor = worker.system_actor(request_id)
    record = await _worker_canary(conn, worker, actor, reply.canary_id)
    return await record_canary_reply(
        conn,
        actor,
        record.id,
        reply_message_id=reply.internet_message_id,
        in_reply_to=list(reply.in_reply_to),
        references=list(reply.references),
        received_at=reply.received_at,
        from_address_hash=reply.from_address_hash,
        evidence={"source": "desktop_worker"},
    )


__all__ = [
    "AUDIT_TARGET",
    "CANARY_MESSAGE_ID_PREFIX",
    "CANARY_WINDOW",
    "DESKTOP_CLAIM_AUDIT",
    "FINISHED_STATES",
    "MAX_CANARIES_PER_24H",
    "MAX_EVIDENCE_KEYS",
    "PHASE_DESKTOP_CLAIMED",
    "PHASE_PUBLISHED",
    "PHASE_SEND_CALL_FAILED",
    "PHASE_SUBMITTED",
    "PHASE_TRANSPORT_STARTED",
    "CanaryClaimRefused",
    "CanaryOutcome",
    "CanaryRecord",
    "CanaryState",
    "canary_message_id",
    "cancel_canary",
    "claim_desktop_canary",
    "claim_for_send",
    "create_canary",
    "desktop_canary_intents",
    "evidence_state",
    "get_canary",
    "list_canaries",
    "publish_for_desktop",
    "published_at",
    "record_canary_outcome",
    "record_canary_reply",
    "record_desktop_canary_reply",
    "report_desktop_canary",
    "sanitize_evidence",
    "target_address_hash",
]
