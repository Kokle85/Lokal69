"""Mailbox-worker identity, inquiry-binding sync, checkpoints and health (spec 37.6-37.8).

The local classic-Outlook reply worker (``desktop/outlook-bridge``) talks to the backend through
one revocable, narrow identity. This module owns everything that identity touches except the
reply ingest itself (``persistence.replies_repo``).

Worker identity (`issue_mail_worker`, `resolve_worker`, `rotate_mail_worker_credential`,
`revoke_mail_worker`)
    A worker is one ``ops.mail_worker_bindings`` row (its id is the stable ``mailbox_binding_id``)
    bound to exactly one ``mail_worker`` credential of ``persistence.credentials_repo``
    (``suvmail_`` token, exactly ``mail:ingest``, owner role, machine principal; the token is
    returned ONCE and only its SHA-256 is stored; migration ``20261007000400`` refuses any other
    credential kind for an active binding). The mailbox is the owner-authorized sender binding's
    own account (``ops.email_sender_bindings``): the request never names a workspace, a mailbox or
    an account. `resolve_worker` authenticates the bearer token (in a transaction opened WITHOUT a
    workspace; the token selects it) and returns the `WorkerIdentity`; a revoked or expired
    credential is ``UNAUTHENTICATED`` (``details.reason = mail_worker_credential_revoked``: the
    worker stops transmitting and keeps its backlog), an unknown token is a plain
    ``UNAUTHENTICATED``, a revoked mailbox binding is ``FORBIDDEN`` (``mailbox_binding_revoked``).
    Every body id that names a mailbox must equal ``WorkerIdentity.mailbox_binding_id``
    (`WorkerIdentity.require_mailbox`): a mismatch is ``FORBIDDEN``
    (``mailbox_binding_mismatch``), never a silent reassignment.

Binding sync (`publish_inquiry_binding`, `publish_mailbox_bindings`, `list_binding_changes`)
    ``ops.mail_binding_sync`` is the per-mailbox, append-only change log the worker reads with
    ``GET /v1/mail-workers/inquiry-bindings``. `publish_inquiry_binding` derives the binding of one
    (possibly) transmitted inquiry from the database (stable outbound Message-IDs, send-intent
    Message-IDs of unaccepted attempts so a reply can resolve an uncertain send, provider ids,
    verified seller addresses, listing references/URLs, vehicle ids) and appends a new
    ``binding_version`` only when the state or payload changed (idempotent); the sequence is
    allocated by the database in commit order. Inquiry state -> binding state: sending, accepted,
    no_reply_yet, replied -> ``active``; uncertain -> ``uncertain``; bounced, seller_opted_out,
    failed_definite -> ``suppressed``; an explicit `tombstone_inquiry_binding` -> ``tombstoned``
    (final: never re-published). `list_binding_changes` returns the changes after an opaque,
    HMAC-signed cursor (``mbs1.<sequence>.<mac>``, bound to workspace and mailbox; it never
    expires because the worker persists it across long offline periods). Items follow the desktop
    wire contract exactly (`api.schemas.MailWorkerBindingItem`); a tombstone carries identity,
    version and state only. ``next_cursor`` names the last RETURNED sequence (the cursor advances
    only to what was returned); with no new changes the request cursor is echoed.

Heartbeats, checkpoints and the account report (`record_heartbeat`, `record_account_report`)
    Per-folder checkpoints (hashed store/folder identities) go to ``ops.mail_worker_checkpoints``
    (the last complete scan time never moves backwards; reported times are clamped to the database
    clock, so a fast worker clock cannot pin a future scan time). Worker-level health has no
    folder: it is the checkpoint row whose store and folder hashes are both `HEALTH_ROW_HASH`
    (``folder_role = 'other'``). Its ``heartbeat_at`` is the time the SERVER received the
    heartbeat; ``outlook_connected``/``mailbox_sync_ok``/``mailbox_last_sync_at``, the upload
    backlog, the last successful reconciliation (``last_complete_scan_at``) and the coverage gaps
    are stored there. Coverage gaps are never hidden: reported gaps are kept in ``gap_reasons``
    (``gap:<kind>:<start epoch>:<end epoch|open>``, newest 30), unresolved matching gaps as
    ``matching_gaps:<n>`` and the account check as ``account:<code>``; every gap that is first
    reported or newly closed is also appended to the audit trail (``mail_worker.coverage_gap``),
    so closed gaps survive the row's bounded window. The account report (no credentials) is
    checked against the bound mailbox address and classic Outlook; problems are recorded on the
    health row and audited (``mail_worker.account_report``, never the address) and returned so
    the API answers ``409`` AFTER the transaction committed (`AccountReportOutcome`).

Health (`mailbox_health`, `list_mailbox_health`)
    Heartbeat age and status, Outlook connection, mailbox sync lag, last reconciliation, backlog
    age, unresolved matching gaps, account check, per-folder checkpoints and every coverage gap
    (reported gaps plus a server-detected ``worker_offline`` gap since the last heartbeat) via
    ``domain.lifecycle.mail_worker_coverage``. ``monitoring_active`` is never claimed without a
    fresh heartbeat, a connected Outlook and a fresh reconciliation.

Lock order: ``app.seller_inquiries`` -> ``ops.mail_worker_bindings`` (taken by the sync
sequence allocator) -> ``ops.mail_binding_sync``; checkpoints are written in their own short
transactions. No network I/O happens here; tokens, addresses and message text are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suv_deals.api.schemas import (
    MailWorkerAccountReport,
    MailWorkerBindingItem,
    MailWorkerBindingPage,
    MailWorkerHeartbeatAck,
    MailWorkerHeartbeatRequest,
)
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import EmailProviderKind, InquiryState, Role, Scope
from suv_deals.domain.lifecycle import (
    CoverageGap,
    LagMeasurement,
    MailCoverageReport,
    MailWorkerHeartbeat,
    mail_worker_coverage,
)
from suv_deals.domain.pagination import MIN_SECRET_BYTES
from suv_deals.domain.replies import (
    InquiryBinding,
    InquiryBindingState,
    canonical_address,
    normalize_message_id,
)
from suv_deals.errors import (
    AppError,
    ErrorCode,
    Forbidden,
    NotFound,
    Unauthenticated,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.persistence import audit, credentials_repo
from suv_deals.persistence.credentials_repo import (
    DEFAULT_CREDENTIAL_LIFETIME,
    MAIL_WORKER_SCOPES,
    CredentialKind,
    CredentialRejected,
    IssuedCredential,
)
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors

MAILBOX_PROVIDER_DEFAULT: Final = EmailProviderKind.OUTLOOK_LOCAL
#: Store and folder hash of the worker-level health checkpoint row (no real folder hashes to it).
HEALTH_ROW_HASH: Final = hashlib.sha256(b"suv-deals/mail-worker/worker-health/v1").hexdigest()
SYNC_CURSOR_PREFIX: Final = "mbs1"
MAX_BINDING_PAGE: Final = 100
MAX_LIST_ITEMS: Final = 20
MAX_GAP_ENTRIES: Final = 30
MAX_GAP_TEXT: Final = 120
DEFAULT_HEARTBEAT_INTERVAL: Final = timedelta(seconds=60)
DEFAULT_RECONCILE_INTERVAL: Final = timedelta(seconds=120)
DEFAULT_HEALTH_WINDOW: Final = timedelta(hours=24)
CLOCK_TOLERANCE: Final = timedelta(minutes=5)

#: Inquiry state -> published binding state (``None``: nothing to publish, never transmitted).
BINDING_STATE_FOR: Final[Mapping[InquiryState, InquiryBindingState]] = {
    InquiryState.SENDING: InquiryBindingState.ACTIVE,
    InquiryState.ACCEPTED: InquiryBindingState.ACTIVE,
    InquiryState.NO_REPLY_YET: InquiryBindingState.ACTIVE,
    InquiryState.REPLIED: InquiryBindingState.ACTIVE,
    InquiryState.UNCERTAIN: InquiryBindingState.UNCERTAIN,
    InquiryState.BOUNCED: InquiryBindingState.SUPPRESSED,
    InquiryState.SELLER_OPTED_OUT: InquiryBindingState.SUPPRESSED,
    InquiryState.FAILED_DEFINITE: InquiryBindingState.SUPPRESSED,
}

AccountStatus = Literal["unknown", "verified", "mismatch", "not_classic"]
ComponentStatus = Literal["healthy", "stale", "down", "unknown"]

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CURSOR_RE: Final = re.compile(rf"^{SYNC_CURSOR_PREFIX}\.(0|[1-9][0-9]{{0,17}})\.([0-9a-f]{{32}})$")
_GAP_RE: Final = re.compile(r"^gap:([a-z0-9_]{1,64}):([0-9]{1,12}):([0-9]{1,12}|open)$")
_CONTROL_RE: Final = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")
_REF_TEXT_RE: Final = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_REASON_MIN, _REASON_MAX = 3, 500


# --------------------------------------------------------------------------------------------
# Worker identity
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """An authenticated mailbox worker. Workspace and mailbox come from the credential only."""

    workspace_id: UUID
    mailbox_binding_id: UUID
    credential_id: UUID
    principal_id: UUID
    sender_binding_id: UUID
    provider: EmailProviderKind
    mailbox_version: int
    credential_expires_at: datetime
    #: The bound mailbox account (canonical). Never logged or returned in a view.
    account_address: str = field(repr=False)

    def actor(self, request_id: str) -> ActorContext:
        """The worker as an audit principal (``mail:ingest`` only; machine principal)."""
        return ActorContext(
            workspace_id=self.workspace_id,
            principal_id=self.principal_id,
            principal_kind="mcp_client",
            role=Role.OWNER,
            scopes=MAIL_WORKER_SCOPES,
            request_id=request_id,
            display_name="mailbox worker",
            client_id=f"mail_worker:{self.credential_id}",
        )

    def system_actor(self, request_id: str) -> ActorContext:
        """Server-side processing triggered by this worker (outbox, jobs, valuations)."""
        return ActorContext.system(self.workspace_id, request_id)

    def require_mailbox(self, mailbox_binding_id: UUID) -> None:
        """A body may only name the worker's own mailbox (never a silent reassignment)."""
        if mailbox_binding_id != self.mailbox_binding_id:
            raise mailbox_mismatch()


def mailbox_mismatch() -> Forbidden:
    """The single refusal for another mailbox's (or an unknown) inquiry/mailbox: no existence leak."""
    return Forbidden(
        "The request names a mailbox or inquiry outside this worker's mailbox",
        details={"reason": "mailbox_binding_mismatch"},
    )


def _revoked_mailbox() -> Forbidden:
    return Forbidden("The mailbox worker binding is revoked", details={"reason": "mailbox_binding_revoked"})


_WORKER_KINDS: Final[tuple[CredentialKind, ...]] = ("mail_worker",)
_WORKER_SQL: Final = (
    "select m.id, m.sender_binding_id, m.provider, m.account_address, m.version, m.state"
    " from ops.mail_worker_bindings m"
    " where m.workspace_id = %(ws)s and m.credential_id = %(credential)s"
    " order by (m.state = 'active') desc, m.updated_at desc, m.id limit 1"
)


async def resolve_worker(conn: Conn, token: str) -> WorkerIdentity:
    """Authenticate a worker bearer token and return its identity (see module docstring).

    Run in a transaction opened WITHOUT a workspace (``db.transaction()``); on success the
    transaction's ``app.workspace_id`` names the worker's workspace.
    """
    try:
        verified = await credentials_repo.authenticate(
            conn, token, kinds=_WORKER_KINDS, required_scopes=(Scope.MAIL_INGEST,)
        )
    except CredentialRejected as exc:
        if exc.reason in ("revoked", "expired_token"):
            raise Unauthenticated(
                "The mailbox worker credential is revoked or expired",
                details={"reason": "mail_worker_credential_revoked"},
            ) from None
        raise Unauthenticated() from None
    if verified.scopes != MAIL_WORKER_SCOPES or verified.role != Role.OWNER:
        raise Unauthenticated()
    async with mapped_errors():
        row = await fetch_one(
            conn, _WORKER_SQL, {"ws": verified.workspace_id, "credential": verified.credential_id}
        )
    if row is None or row["state"] != "active":
        raise _revoked_mailbox()
    await credentials_repo.touch_last_used(conn, verified)
    return WorkerIdentity(
        workspace_id=verified.workspace_id,
        mailbox_binding_id=row["id"],
        credential_id=verified.credential_id,
        principal_id=verified.principal_id,
        sender_binding_id=row["sender_binding_id"],
        provider=EmailProviderKind(row["provider"]),
        mailbox_version=int(row["version"]),
        credential_expires_at=verified.expires_at,
        account_address=str(row["account_address"]),
    )


async def require_active_mailbox(conn: Conn, worker: WorkerIdentity) -> None:
    """Re-check inside the caller's transaction that the worker's binding is still active."""
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select state, credential_id from ops.mail_worker_bindings where workspace_id = %(ws)s"
            " and id = %(id)s",
            {"ws": worker.workspace_id, "id": worker.mailbox_binding_id},
        )
    if row is None or row["state"] != "active":
        raise _revoked_mailbox()
    if row["credential_id"] != worker.credential_id:
        # Rotated since the request authenticated: the old token no longer speaks for the mailbox.
        raise Unauthenticated(
            "The mailbox worker credential was replaced", details={"reason": "mail_worker_credential_revoked"}
        )


# --------------------------------------------------------------------------------------------
# Administration: issue, rotate, revoke
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedMailWorker:
    """A newly bound worker. ``credential.token`` is shown exactly once and never stored."""

    mailbox_binding_id: UUID
    mailbox_version: int
    credential: IssuedCredential
    published_bindings: int


def _bounded(value: object, field_name: str, *, minimum: int, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not minimum <= len(text) <= maximum or _CONTROL_RE.search(text):
        raise ValidationFailed(
            f"{field_name} must be {minimum}-{maximum} printable characters", details={"fields": [field_name]}
        )
    return text


def _hashes(values: Iterable[str], field_name: str, limit: int) -> list[str]:
    result = list(dict.fromkeys(values))
    if len(result) > limit or not all(_HEX64_RE.fullmatch(v) for v in result):
        raise ValidationFailed(
            f"{field_name} must be at most {limit} lower-case sha256 digests",
            details={"fields": [field_name]},
        )
    return result


async def issue_mail_worker(
    conn: Conn,
    actor: ActorContext,
    *,
    sender_binding_id: UUID,
    label: str,
    store_id_hash: str | None = None,
    folder_scope: Sequence[str] = (),
    provider: EmailProviderKind = MAILBOX_PROVIDER_DEFAULT,
    lifetime: timedelta = DEFAULT_CREDENTIAL_LIFETIME,
) -> IssuedMailWorker:
    """Bind a worker to the sender binding's own mailbox and issue its narrow credential.

    Operator CLI (system) or the signed-in owner only. The mailbox account is the sender binding's
    From address (never caller-supplied). The token is returned ONCE. Already transmitted
    inquiries of that sender are published to the new mailbox (`publish_mailbox_bindings`).
    Audited as ``mail_worker.create`` (plus ``credential.create``; never the token).
    """
    credentials_repo.require_credential_admin(actor)
    text = _bounded(label, "label", minimum=1, maximum=120)
    folders = _hashes(folder_scope, "folder_scope", 20)
    if store_id_hash is not None:
        _hashes([store_id_hash], "store_id_hash", 1)
    async with mapped_errors():
        sender = await fetch_one(
            conn,
            "select id, from_address, revoked_at from ops.email_sender_bindings"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": sender_binding_id},
        )
    if sender is None:
        raise NotFound("Sender binding not found")
    if sender["revoked_at"] is not None:
        raise ValidationFailed(
            "A revoked sender binding cannot get a mailbox worker", details={"fields": ["sender_binding_id"]}
        )
    credential = await credentials_repo.issue_credential(
        conn,
        actor,
        principal_id=uuid4(),
        principal_kind="mcp_client",
        role=Role.OWNER,
        scopes=(Scope.MAIL_INGEST,),
        label=text,
        kind="mail_worker",
        lifetime=lifetime,
    )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.mail_worker_bindings (workspace_id, sender_binding_id, credential_id, provider,"
            " account_address, store_id_hash, folder_scope, worker_label, created_by)"
            " values (%(ws)s, %(sender)s, %(credential)s, %(provider)s, %(account)s, %(store)s,"
            " %(folders)s, %(label)s, %(by)s) returning id, version",
            {
                "ws": actor.workspace_id,
                "sender": sender_binding_id,
                "credential": credential.credential_id,
                "provider": EmailProviderKind(provider).value,
                "account": sender["from_address"],
                "store": store_id_hash,
                "folders": folders,
                "label": text,
                "by": actor.principal_id,
            },
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "mail_worker.create",
            "mail_worker_binding",
            row["id"],
            None,
            int(row["version"]),
            metadata={
                "sender_binding_id": str(sender_binding_id),
                "provider": EmailProviderKind(provider).value,
                "credential_id": str(credential.credential_id),
                "token_prefix": credential.token_prefix,
            },
        )
    published = await publish_mailbox_bindings(conn, actor, row["id"])
    return IssuedMailWorker(
        mailbox_binding_id=row["id"],
        mailbox_version=int(row["version"]),
        credential=credential,
        published_bindings=len(published),
    )


async def _lock_mailbox(conn: Conn, actor: ActorContext, mailbox_binding_id: UUID) -> Mapping[str, Any]:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id, credential_id, state, version, sender_binding_id from ops.mail_worker_bindings"
            " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": actor.workspace_id, "id": mailbox_binding_id},
        )
    if row is None:
        raise NotFound("Mailbox worker binding not found")
    return row


async def rotate_mail_worker_credential(
    conn: Conn,
    actor: ActorContext,
    mailbox_binding_id: UUID,
    *,
    reason: str,
    label: str | None = None,
    lifetime: timedelta = DEFAULT_CREDENTIAL_LIFETIME,
) -> IssuedMailWorker:
    """Issue a new credential for the same mailbox identity and revoke the old one (version +1)."""
    credentials_repo.require_credential_admin(actor)
    text = _bounded(reason, "reason", minimum=_REASON_MIN, maximum=_REASON_MAX)
    row = await _lock_mailbox(conn, actor, mailbox_binding_id)
    if row["state"] != "active":
        raise _revoked_mailbox()
    credential = await credentials_repo.issue_credential(
        conn,
        actor,
        principal_id=uuid4(),
        principal_kind="mcp_client",
        role=Role.OWNER,
        scopes=(Scope.MAIL_INGEST,),
        label=_bounded(label or "Mailbox worker (rotated)", "label", minimum=1, maximum=120),
        kind="mail_worker",
        lifetime=lifetime,
    )
    async with mapped_errors():
        updated = await fetch_one(
            conn,
            "update ops.mail_worker_bindings set credential_id = %(credential)s, version = version + 1"
            " where workspace_id = %(ws)s and id = %(id)s returning version",
            {"ws": actor.workspace_id, "id": mailbox_binding_id, "credential": credential.credential_id},
        )
        assert updated is not None
    await credentials_repo.revoke_credential(conn, actor, row["credential_id"], reason=text)
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "mail_worker.rotate",
            "mail_worker_binding",
            mailbox_binding_id,
            int(row["version"]),
            int(updated["version"]),
            reason=text,
            metadata={
                "credential_id": str(credential.credential_id),
                "token_prefix": credential.token_prefix,
            },
        )
    return IssuedMailWorker(
        mailbox_binding_id=mailbox_binding_id,
        mailbox_version=int(updated["version"]),
        credential=credential,
        published_bindings=0,
    )


async def revoke_mail_worker(
    conn: Conn, actor: ActorContext, mailbox_binding_id: UUID, *, reason: str
) -> bool:
    """Revoke the worker binding (permanent) and its credential; ``False`` if already revoked."""
    credentials_repo.require_credential_admin(actor)
    text = _bounded(reason, "reason", minimum=_REASON_MIN, maximum=_REASON_MAX)
    row = await _lock_mailbox(conn, actor, mailbox_binding_id)
    if row["state"] != "active":
        return False
    async with mapped_errors():
        await conn.execute(
            "update ops.mail_worker_bindings set state = 'revoked', revoked_at = clock_timestamp(),"
            " revoked_by = %(by)s, revoke_reason = %(reason)s where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": mailbox_binding_id, "by": actor.principal_id, "reason": text},
        )
    await credentials_repo.revoke_credential(conn, actor, row["credential_id"], reason=text)
    async with mapped_errors():
        await audit.record(
            conn, actor, "mail_worker.revoke", "mail_worker_binding", mailbox_binding_id, reason=text
        )
    return True


# --------------------------------------------------------------------------------------------
# Binding publication
# --------------------------------------------------------------------------------------------


class PublishedBinding(BaseModel):
    """One ``ops.mail_binding_sync`` row (``created=False``: the latest row already matched)."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    inquiry_id: UUID
    binding_version: int
    state: InquiryBindingState
    sequence: int
    created: bool


def _require_publisher(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system workers or the owner publish mailbox bindings")


_INQUIRY_BINDING_SQL: Final = """
select i.id, i.state, i.sender_binding_id, i.sender_provider, i.rfc_message_id, i.provider_message_id,
       i.provider_thread_id, i.recipient_address, i.recipient_contact_id, i.seller_entity_id,
       i.qualification_listing_id, i.vehicle_cluster_id,
       c.listing_reference as contact_reference, c.listing_url as contact_url,
       l.source_listing_id, l.canonical_url
  from app.seller_inquiries i
  join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
  left join app.seller_contacts c on c.workspace_id = i.workspace_id and c.id = i.recipient_contact_id
 where i.workspace_id = %(ws)s and i.id = %(id)s
"""
_ATTEMPTS_SQL: Final = """
select rfc_message_id, outcome, reconciled_outcome, provider_message_id, provider_thread_id
  from ops.email_delivery_attempts
 where workspace_id = %(ws)s and inquiry_id = %(id)s
 order by attempt_number
"""
_SELLER_ADDRESSES_SQL: Final = """
select c.address
  from app.seller_contacts c
  join app.seller_entities e on e.workspace_id = c.workspace_id and e.id = c.seller_entity_id
 where c.workspace_id = %(ws)s and c.status = 'verified' and c.address is not null
   and (e.id = %(seller)s or e.merged_into_id = %(seller)s)
 order by c.verified_at, c.id
 limit 50
"""
_LATEST_SYNC_SQL: Final = """
select sequence, binding_version, binding_state, payload
  from ops.mail_binding_sync
 where workspace_id = %(ws)s and mailbox_binding_id = %(box)s and inquiry_id = %(inquiry)s
 order by binding_version desc
 limit 1
"""
_INSERT_SYNC_SQL: Final = """
insert into ops.mail_binding_sync (workspace_id, mailbox_binding_id, inquiry_id, binding_version,
                                   binding_state, payload)
values (%(ws)s, %(box)s, %(inquiry)s, %(version)s, %(state)s, %(payload)s)
returning sequence
"""


def _add(target: list[str], value: object) -> None:
    if isinstance(value, str) and value and value not in target and len(target) < MAX_LIST_ITEMS:
        target.append(value)


def _message_ids(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        _add(result, normalize_message_id(value) if isinstance(value, str) else None)
    return result


def _texts(values: Iterable[object], *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if _REF_TEXT_RE.fullmatch(text) and text not in result and len(result) < limit:
                result.append(text)
    return result


async def binding_payload(conn: Conn, workspace_id: UUID, inquiry: Mapping[str, Any]) -> dict[str, Any]:
    """The wire payload (``api.schemas.MailWorkerBindingItem`` minus its identity) of an inquiry."""
    params = {"ws": workspace_id, "id": inquiry["id"], "seller": inquiry["seller_entity_id"]}
    async with mapped_errors():
        attempts = await fetch_all(conn, _ATTEMPTS_SQL, params)
        addresses = await fetch_all(conn, _SELLER_ADDRESSES_SQL, params)
    accepted = [
        a["rfc_message_id"]
        for a in attempts
        if a["outcome"] == "accepted" or a["reconciled_outcome"] == "accepted"
    ]
    outbound = _message_ids([inquiry["rfc_message_id"], *accepted])
    intents = [m for m in _message_ids(a["rfc_message_id"] for a in attempts) if m not in outbound]
    provider_ids = _texts([inquiry["provider_message_id"], *(a["provider_message_id"] for a in attempts)])
    threads = _texts([inquiry["provider_thread_id"], *(a["provider_thread_id"] for a in attempts)])
    aliases: list[str] = []
    for value in [inquiry["recipient_address"], *(r["address"] for r in addresses)]:
        if isinstance(value, str) and len(aliases) < 10:
            _add(aliases, canonical_address(value))
    return {
        "provider": EmailProviderKind(inquiry["sender_provider"]).value,
        "outbound_message_ids": outbound,
        "send_intent_message_ids": intents,
        "provider_message_ids": provider_ids,
        "provider_thread_ids": threads,
        "verified_seller_aliases": aliases,
        "listing_references": _texts([inquiry["contact_reference"], inquiry["source_listing_id"]]),
        "listing_urls": _texts([inquiry["contact_url"], inquiry["canonical_url"]]),
        "listing_id": str(inquiry["qualification_listing_id"]),
        "vehicle_cluster_id": None
        if inquiry["vehicle_cluster_id"] is None
        else str(inquiry["vehicle_cluster_id"]),
        "is_canary": False,
    }


def binding_item(
    mailbox_binding_id: UUID,
    inquiry_id: UUID,
    binding_version: int,
    state: InquiryBindingState | str,
    payload: Mapping[str, Any],
) -> MailWorkerBindingItem:
    """The wire item of one change-log row (a tombstone carries identity, version and state only)."""
    binding_state = InquiryBindingState(state)
    body: dict[str, Any] = {} if binding_state == InquiryBindingState.TOMBSTONED else dict(payload)
    return MailWorkerBindingItem.model_validate(
        {
            "inquiry_id": inquiry_id,
            "binding_version": binding_version,
            "mailbox_binding_id": mailbox_binding_id,
            "state": binding_state,
            **body,
        }
    )


def domain_binding(item: MailWorkerBindingItem) -> InquiryBinding:
    """The shared domain view of a non-tombstone wire item (used by server-side correlation)."""
    if item.state == InquiryBindingState.TOMBSTONED:
        raise ValidationFailed("a tombstone grants no binding")
    return InquiryBinding.model_validate(item.model_dump())


async def _active_mailbox_for_sender(conn: Conn, workspace_id: UUID, sender_binding_id: UUID) -> UUID | None:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id from ops.mail_worker_bindings where workspace_id = %(ws)s"
            " and sender_binding_id = %(sender)s and state = 'active'",
            {"ws": workspace_id, "sender": sender_binding_id},
        )
    return None if row is None else row["id"]


def _published(
    box: UUID, inquiry_id: UUID, row: Mapping[str, Any], *, created: bool, version: int | None = None
) -> PublishedBinding:
    return PublishedBinding(
        mailbox_binding_id=box,
        inquiry_id=inquiry_id,
        binding_version=int(version if version is not None else row["binding_version"]),
        state=InquiryBindingState(row["binding_state"]),
        sequence=int(row["sequence"]),
        created=created,
    )


async def publish_inquiry_binding(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, *, tombstone: bool = False
) -> PublishedBinding | None:
    """Append the inquiry's current binding to its sender mailbox's change log when it changed.

    Returns ``None`` when there is nothing to publish (no active mailbox worker for the inquiry's
    sender, or an inquiry that was never transmitted and never published). A tombstone is final:
    later calls return it unchanged. The inquiry row is locked first (serialises publishers).
    """
    _require_publisher(actor)
    params = {"ws": actor.workspace_id, "id": inquiry_id}
    async with mapped_errors():
        locked = await fetch_one(
            conn,
            "select id from app.seller_inquiries where workspace_id = %(ws)s and id = %(id)s for update",
            params,
        )
        inquiry = await fetch_one(conn, _INQUIRY_BINDING_SQL, params) if locked is not None else None
    if inquiry is None:
        raise NotFound("Inquiry not found")
    if inquiry["sender_binding_id"] is None:
        return None
    box = await _active_mailbox_for_sender(conn, actor.workspace_id, inquiry["sender_binding_id"])
    if box is None:
        return None
    async with mapped_errors():
        latest = await fetch_one(conn, _LATEST_SYNC_SQL, {**params, "box": box, "inquiry": inquiry_id})
    if latest is not None and latest["binding_state"] == InquiryBindingState.TOMBSTONED.value:
        return _published(box, inquiry_id, latest, created=False)
    state: InquiryBindingState | None
    payload: dict[str, Any]
    if tombstone:
        state, payload = InquiryBindingState.TOMBSTONED, {}
    else:
        state = BINDING_STATE_FOR.get(InquiryState(inquiry["state"]))
        if state is None:
            return None if latest is None else _published(box, inquiry_id, latest, created=False)
        payload = await binding_payload(conn, actor.workspace_id, inquiry)
    version = 1 if latest is None else int(latest["binding_version"]) + 1
    item = binding_item(box, inquiry_id, version, state, payload)  # wire-valid before it is stored
    stored = {} if state == InquiryBindingState.TOMBSTONED else _payload_of(item)
    if latest is not None and latest["binding_state"] == state.value and dict(latest["payload"]) == stored:
        return _published(box, inquiry_id, latest, created=False)
    async with mapped_errors(unique={"mail_binding_sync_version_uk": _concurrent_publish}):
        row = await fetch_one(
            conn,
            _INSERT_SYNC_SQL,
            {
                **params,
                "box": box,
                "inquiry": inquiry_id,
                "version": version,
                "state": state.value,
                "payload": Jsonb(stored),
            },
        )
    assert row is not None
    return PublishedBinding(
        mailbox_binding_id=box,
        inquiry_id=inquiry_id,
        binding_version=version,
        state=state,
        sequence=int(row["sequence"]),
        created=True,
    )


def _concurrent_publish() -> AppError:
    return TransientConflict("The mailbox binding changed concurrently; retry")


def _payload_of(item: MailWorkerBindingItem) -> dict[str, Any]:
    data = item.model_dump(
        mode="json", exclude={"inquiry_id", "binding_version", "mailbox_binding_id", "state"}
    )
    return data


async def publish_mailbox_bindings(
    conn: Conn, actor: ActorContext, mailbox_binding_id: UUID, *, limit: int = 500
) -> list[PublishedBinding]:
    """Publish (or refresh) the binding of every (possibly) transmitted inquiry of the mailbox's
    sender binding, oldest first. Used when a worker is bound and by reconciliation jobs."""
    _require_publisher(actor)
    if not 1 <= limit <= 5000:
        raise ValidationFailed("limit must be between 1 and 5000")
    async with mapped_errors():
        box = await fetch_one(
            conn,
            "select sender_binding_id, state from ops.mail_worker_bindings where workspace_id = %(ws)s"
            " and id = %(id)s",
            {"ws": actor.workspace_id, "id": mailbox_binding_id},
        )
        if box is None:
            raise NotFound("Mailbox worker binding not found")
        if box["state"] != "active":
            raise _revoked_mailbox()
        rows = await fetch_all(
            conn,
            "select id from app.seller_inquiries where workspace_id = %(ws)s"
            " and sender_binding_id = %(sender)s and send_attempted_at is not null"
            " order by created_at, id limit %(limit)s",
            {"ws": actor.workspace_id, "sender": box["sender_binding_id"], "limit": limit},
        )
    published: list[PublishedBinding] = []
    for row in rows:
        result = await publish_inquiry_binding(conn, actor, row["id"])
        if result is not None and result.created:
            published.append(result)
    return published


async def tombstone_inquiry_binding(
    conn: Conn, actor: ActorContext, inquiry_id: UUID, *, reason: str
) -> PublishedBinding | None:
    """Revoke the inquiry's binding for its mailbox (final); audited ``mail_binding.tombstone``."""
    text = _bounded(reason, "reason", minimum=_REASON_MIN, maximum=_REASON_MAX)
    result = await publish_inquiry_binding(conn, actor, inquiry_id, tombstone=True)
    if result is not None and result.created:
        async with mapped_errors():
            await audit.record(
                conn,
                actor,
                "mail_binding.tombstone",
                "seller_inquiry",
                inquiry_id,
                reason=text,
                metadata={
                    "mailbox_binding_id": str(result.mailbox_binding_id),
                    "version": result.binding_version,
                },
            )
    return result


# --------------------------------------------------------------------------------------------
# Binding sync (worker read)
# --------------------------------------------------------------------------------------------


def _secret_keys(secret: object) -> tuple[bytes, ...]:
    if isinstance(secret, bytes | bytearray):
        keys: tuple[object, ...] = (secret,)
    elif isinstance(secret, Sequence) and not isinstance(secret, str):
        keys = tuple(secret)
    else:
        keys = ()
    if not keys or any(not isinstance(k, bytes | bytearray) or len(k) < MIN_SECRET_BYTES for k in keys):
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "Binding sync cursor signing is not configured", retryable=False
        )
    return tuple(bytes(k) for k in keys if isinstance(k, bytes | bytearray))


def _cursor_mac(key: bytes, worker: WorkerIdentity, sequence: int) -> str:
    message = f"{SYNC_CURSOR_PREFIX}|{worker.workspace_id}|{worker.mailbox_binding_id}|{sequence}"
    return hmac.new(key, message.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def encode_sync_cursor(worker: WorkerIdentity, sequence: int, secret: bytes | Sequence[bytes]) -> str:
    """Opaque signed position after ``sequence`` (bound to the worker's workspace and mailbox)."""
    keys = _secret_keys(secret)
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    return f"{SYNC_CURSOR_PREFIX}.{sequence}.{_cursor_mac(keys[0], worker, sequence)}"


def decode_sync_cursor(worker: WorkerIdentity, cursor: str, secret: bytes | Sequence[bytes]) -> int:
    """The sequence of a cursor issued for THIS worker's mailbox (any current or previous key)."""
    keys = _secret_keys(secret)
    match = _CURSOR_RE.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValidationFailed(
            "Invalid binding sync cursor; restart the sync", details={"cursor": "malformed"}
        )
    sequence = int(match.group(1))
    presented = match.group(2)
    if not any(hmac.compare_digest(_cursor_mac(key, worker, sequence), presented) for key in keys):
        raise ValidationFailed(
            "Invalid binding sync cursor; restart the sync", details={"cursor": "tampered"}
        )
    return sequence


_SYNC_PAGE_SQL: Final = """
select sequence, inquiry_id, binding_version, binding_state, payload
  from ops.mail_binding_sync
 where workspace_id = %(ws)s and mailbox_binding_id = %(box)s and sequence > %(after)s
 order by sequence
 limit %(limit)s
"""


async def list_binding_changes(
    conn: Conn,
    worker: WorkerIdentity,
    *,
    cursor: str | None,
    limit: int = MAX_BINDING_PAGE,
    secret: bytes | Sequence[bytes],
) -> MailWorkerBindingPage:
    """``GET /v1/mail-workers/inquiry-bindings``: the worker's own mailbox changes after ``cursor``."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_BINDING_PAGE:
        raise ValidationFailed("limit must be between 1 and 100", details={"fields": ["limit"]})
    after = 0 if cursor is None else decode_sync_cursor(worker, cursor, secret)
    await require_active_mailbox(conn, worker)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SYNC_PAGE_SQL,
            {"ws": worker.workspace_id, "box": worker.mailbox_binding_id, "after": after, "limit": limit + 1},
        )
    page, more = rows[:limit], len(rows) > limit
    try:
        items = tuple(
            binding_item(
                worker.mailbox_binding_id,
                r["inquiry_id"],
                int(r["binding_version"]),
                r["binding_state"],
                r["payload"],
            )
            for r in page
        )
    except (ValidationError, ValueError):
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "A stored mailbox binding could not be rendered", retryable=False
        ) from None
    if page:
        next_cursor: str | None = encode_sync_cursor(worker, int(page[-1]["sequence"]), secret)
    else:
        next_cursor = cursor
    return MailWorkerBindingPage(schema_version="1.0", items=items, next_cursor=next_cursor, has_more=more)


async def binding_for(
    conn: Conn, worker: WorkerIdentity, inquiry_id: UUID, binding_version: int
) -> tuple[MailWorkerBindingItem | None, MailWorkerBindingItem | None]:
    """``(requested version, latest version)`` of one inquiry's binding in the worker's mailbox."""
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select binding_version, binding_state, payload from ops.mail_binding_sync"
            " where workspace_id = %(ws)s and mailbox_binding_id = %(box)s and inquiry_id = %(inquiry)s"
            " and (binding_version = %(version)s or binding_version = (select max(s.binding_version)"
            "   from ops.mail_binding_sync s where s.workspace_id = %(ws)s and s.mailbox_binding_id = %(box)s"
            "   and s.inquiry_id = %(inquiry)s))",
            {
                "ws": worker.workspace_id,
                "box": worker.mailbox_binding_id,
                "inquiry": inquiry_id,
                "version": binding_version,
            },
        )
    items = {
        int(r["binding_version"]): binding_item(
            worker.mailbox_binding_id, inquiry_id, int(r["binding_version"]), r["binding_state"], r["payload"]
        )
        for r in rows
    }
    latest = items[max(items)] if items else None
    return items.get(binding_version), latest


async def latest_mailbox_bindings(
    conn: Conn, worker: WorkerIdentity, *, limit: int = 500
) -> list[InquiryBinding]:
    """The newest non-tombstoned binding of every inquiry in the worker's mailbox (domain shape)."""
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select distinct on (inquiry_id) inquiry_id, binding_version, binding_state, payload"
            " from ops.mail_binding_sync where workspace_id = %(ws)s and mailbox_binding_id = %(box)s"
            " order by inquiry_id, binding_version desc limit %(limit)s",
            {"ws": worker.workspace_id, "box": worker.mailbox_binding_id, "limit": limit},
        )
    result: list[InquiryBinding] = []
    for row in rows:
        if row["binding_state"] == InquiryBindingState.TOMBSTONED.value:
            continue
        item = binding_item(
            worker.mailbox_binding_id,
            row["inquiry_id"],
            int(row["binding_version"]),
            row["binding_state"],
            row["payload"],
        )
        result.append(domain_binding(item))
    return result


# --------------------------------------------------------------------------------------------
# Heartbeats, checkpoints and the account report
# --------------------------------------------------------------------------------------------


def _clamp(value: datetime | None, now: datetime, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        aware = ensure_utc(value)
    except ValueError:
        raise ValidationFailed(
            f"{field_name} must be timezone-aware", details={"fields": [field_name]}
        ) from None
    return min(aware, now)


def _gap_code(kind: str, started_at: datetime, ended_at: datetime | None) -> str:
    end = "open" if ended_at is None else str(int(ended_at.timestamp()))
    return f"gap:{kind}:{int(started_at.timestamp())}:{end}"


class ReportedGap(BaseModel):
    """A monitored coverage gap of the mailbox worker (reported by it or detected here)."""

    model_config = _FROZEN

    kind: str = Field(max_length=64)
    started_at: datetime
    ended_at: datetime | None
    open: bool
    detected_by: Literal["worker", "server"]


def parse_gap_codes(codes: Iterable[str]) -> list[ReportedGap]:
    """Decode the ``gap:`` entries of a health row (malformed entries are skipped)."""
    gaps: list[ReportedGap] = []
    for code in codes:
        match = _GAP_RE.fullmatch(code)
        if match is None:
            continue
        start = datetime.fromtimestamp(int(match.group(2)), tz=UTC)
        end = None if match.group(3) == "open" else datetime.fromtimestamp(int(match.group(3)), tz=UTC)
        if end is not None and end < start:
            continue
        gaps.append(
            ReportedGap(
                kind=match.group(1), started_at=start, ended_at=end, open=end is None, detected_by="worker"
            )
        )
    return gaps


def _merge_gaps(
    previous: Sequence[str], reported: Sequence[ReportedGap]
) -> tuple[list[str], list[ReportedGap]]:
    """New gap code list (newest ``MAX_GAP_ENTRIES``) and the gaps that are new or newly closed."""
    known = {(g.kind, g.started_at): g for g in parse_gap_codes(previous)}
    changed: list[ReportedGap] = []
    for gap in reported:
        key = (gap.kind, gap.started_at)
        before = known.get(key)
        if before is None or (before.ended_at is None and gap.ended_at is not None):
            changed.append(gap)
            known[key] = gap
    ordered = sorted(known.values(), key=lambda g: (g.started_at, g.kind))[-MAX_GAP_ENTRIES:]
    return [_gap_code(g.kind, g.started_at, g.ended_at) for g in ordered], changed


def _folder_reasons(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _CONTROL_RE.sub(" ", value).strip()[:MAX_GAP_TEXT]
        if text and text not in result and len(result) < MAX_GAP_ENTRIES:
            result.append(text)
    return result


_HEALTH_ROW_SQL: Final = """
select id, gap_reasons, last_complete_scan_at
  from ops.mail_worker_checkpoints
 where workspace_id = %(ws)s and mailbox_binding_id = %(box)s and store_id_hash = %(hash)s
   and folder_id_hash = %(hash)s
 for update
"""
_UPSERT_CHECKPOINT_SQL: Final = """
insert into ops.mail_worker_checkpoints as c (
  workspace_id, mailbox_binding_id, store_id_hash, folder_id_hash, folder_role, cursor, overlap_watermark,
  last_complete_scan_at, last_scan_started_at, heartbeat_at, backlog_count, backlog_oldest_at,
  outlook_connected, mailbox_sync_ok, mailbox_last_sync_at, gap_reasons)
values (%(ws)s, %(box)s, %(store)s, %(folder)s, %(role)s, %(cursor)s, %(overlap)s, %(complete)s,
        %(started)s, clock_timestamp(), %(backlog)s, %(oldest)s, %(outlook)s, %(sync_ok)s, %(last_sync)s,
        %(gaps)s)
on conflict (workspace_id, mailbox_binding_id, store_id_hash, folder_id_hash) do update set
  folder_role = excluded.folder_role,
  cursor = coalesce(excluded.cursor, c.cursor),
  overlap_watermark = coalesce(excluded.overlap_watermark, c.overlap_watermark),
  last_complete_scan_at = greatest(c.last_complete_scan_at, excluded.last_complete_scan_at),
  last_scan_started_at = coalesce(excluded.last_scan_started_at, c.last_scan_started_at),
  heartbeat_at = excluded.heartbeat_at,
  backlog_count = excluded.backlog_count,
  backlog_oldest_at = excluded.backlog_oldest_at,
  outlook_connected = excluded.outlook_connected,
  mailbox_sync_ok = excluded.mailbox_sync_ok,
  mailbox_last_sync_at = coalesce(excluded.mailbox_last_sync_at, c.mailbox_last_sync_at),
  gap_reasons = excluded.gap_reasons,
  row_version = c.row_version + 1
returning heartbeat_at
"""


async def _db_now(conn: Conn) -> datetime:
    row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return ensure_utc(row["now"])


async def record_heartbeat(
    conn: Conn, worker: WorkerIdentity, request: MailWorkerHeartbeatRequest, *, request_id: str
) -> MailWorkerHeartbeatAck:
    """``POST /v1/mail-workers/heartbeat``: worker health, folder checkpoints and coverage gaps.

    Run in a ``unit_of_work`` of the worker's workspace (``WorkerIdentity.actor``). The body's
    mailbox must be the worker's own. Gaps are merged (never dropped from the bounded window
    without an audit record); a revoked mailbox binding is refused by the database guard.
    """
    worker.require_mailbox(request.heartbeat.mailbox_binding_id)
    await require_active_mailbox(conn, worker)
    async with mapped_errors():
        now = await _db_now(conn)
        health = await fetch_one(
            conn,
            _HEALTH_ROW_SQL,
            {"ws": worker.workspace_id, "box": worker.mailbox_binding_id, "hash": HEALTH_ROW_HASH},
        )
    previous: list[str] = list(health["gap_reasons"]) if health is not None else []
    reported = [
        ReportedGap(
            kind=g.kind,
            started_at=_clamp(g.started_at, now, "gaps.started_at") or now,
            ended_at=_clamp(g.ended_at, now, "gaps.ended_at"),
            open=g.ended_at is None,
            detected_by="worker",
        )
        for g in request.gaps
    ]
    gap_codes, changed = _merge_gaps(previous, reported)
    account = [c for c in previous if c.startswith("account:")][-1:]
    codes = [
        *gap_codes[-(MAX_GAP_ENTRIES - 2) :],
        *account,
        f"matching_gaps:{request.unresolved_matching_gaps}",
    ]
    backlog = request.backlog_count
    oldest = (
        now - timedelta(seconds=request.backlog_oldest_age_seconds)
        if backlog > 0 and request.backlog_oldest_age_seconds is not None
        else None
    )
    beat = request.heartbeat
    common = {"ws": worker.workspace_id, "box": worker.mailbox_binding_id}
    async with mapped_errors():
        await fetch_one(
            conn,
            _UPSERT_CHECKPOINT_SQL,
            {
                **common,
                "store": HEALTH_ROW_HASH,
                "folder": HEALTH_ROW_HASH,
                "role": "other",
                "cursor": None,
                "overlap": None,
                "complete": _clamp(
                    request.last_successful_reconciliation_at, now, "last_successful_reconciliation_at"
                ),
                "started": None,
                "backlog": backlog,
                "oldest": oldest,
                "outlook": beat.outlook_running,
                "sync_ok": beat.mailbox_connected,
                "last_sync": _clamp(request.mailbox_last_sync_at, now, "mailbox_last_sync_at"),
                "gaps": codes,
            },
        )
        for checkpoint in request.checkpoints:
            if checkpoint.store_id_hash == HEALTH_ROW_HASH and checkpoint.folder_id_hash == HEALTH_ROW_HASH:
                raise ValidationFailed("checkpoint hashes are reserved", details={"fields": ["checkpoints"]})
            ack = _clamp(checkpoint.acknowledged_watermark, now, "checkpoints.acknowledged_watermark")
            count = checkpoint.backlog_count
            await fetch_one(
                conn,
                _UPSERT_CHECKPOINT_SQL,
                {
                    **common,
                    "store": checkpoint.store_id_hash,
                    "folder": checkpoint.folder_id_hash,
                    "role": checkpoint.folder_role,
                    "cursor": None if ack is None else f"ack:{ack.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}",
                    "overlap": _clamp(checkpoint.overlap_watermark, now, "checkpoints.overlap_watermark"),
                    "complete": _clamp(
                        checkpoint.last_complete_scan_at, now, "checkpoints.last_complete_scan_at"
                    ),
                    "started": _clamp(
                        checkpoint.last_scan_started_at, now, "checkpoints.last_scan_started_at"
                    ),
                    "backlog": count,
                    "oldest": _clamp(checkpoint.backlog_oldest_at, now, "checkpoints.backlog_oldest_at")
                    if count > 0
                    else None,
                    "outlook": beat.outlook_running,
                    "sync_ok": beat.mailbox_connected,
                    "last_sync": _clamp(request.mailbox_last_sync_at, now, "mailbox_last_sync_at"),
                    "gaps": _folder_reasons(checkpoint.gap_reasons),
                },
            )
        actor = worker.actor(request_id)
        for gap in changed:
            await audit.record(
                conn,
                actor,
                "mail_worker.coverage_gap",
                "mail_worker_binding",
                worker.mailbox_binding_id,
                metadata={
                    "kind": gap.kind,
                    "started_at": gap.started_at.isoformat(),
                    "ended_at": None if gap.ended_at is None else gap.ended_at.isoformat(),
                },
            )
        signal = await fetch_one(
            conn,
            "select state from ops.outbox where workspace_id = %(ws)s"
            " and event_type = 'seller.reply.received' order by event_created_at desc, id desc limit 1",
            {"ws": worker.workspace_id},
        )
    downstream = {"backend": "ok", "seller_reply_signal": "none" if signal is None else str(signal["state"])}
    return MailWorkerHeartbeatAck(schema_version="1.0", received_at=now, downstream=downstream)


class AccountReportOutcome(BaseModel):
    """Result of an account report. ``problems`` empty: verified. Raise AFTER the commit."""

    model_config = _FROZEN

    accepted: bool
    status: AccountStatus
    problems: tuple[str, ...] = ()

    def raise_for_problems(self) -> None:
        """``VERSION_CONFLICT`` (``mail_worker_account_mismatch``) for a refused report."""
        if not self.accepted:
            raise VersionConflict(
                "The reported Outlook account is not the bound mailbox account or not classic Outlook",
                reason="mail_worker_account_mismatch",
                problems=list(self.problems),
            )


async def record_account_report(
    conn: Conn, worker: WorkerIdentity, report: MailWorkerAccountReport, *, request_id: str
) -> AccountReportOutcome:
    """``POST /v1/mail-workers/account-report``: verify the Outlook account (no credentials).

    The reported SMTP address must be the bound mailbox account (exact canonical match; no
    provider-specific folding) and Outlook must be classic. The result is recorded on the health
    row (``account:<status>``) and audited WITHOUT the address; a refused report is returned (the
    caller raises `AccountReportOutcome.raise_for_problems` after the commit, so the refusal stays
    visible on the dashboard).
    """
    worker.require_mailbox(report.mailbox_binding_id)
    await require_active_mailbox(conn, worker)
    problems: list[str] = []
    reported = canonical_address(report.account_smtp_address)
    if reported is None or reported != canonical_address(worker.account_address):
        problems.append("ACCOUNT_MISMATCH")
    if report.outlook_flavour != "classic":
        problems.append("OUTLOOK_NOT_CLASSIC")
    status: AccountStatus = (
        "mismatch" if "ACCOUNT_MISMATCH" in problems else "not_classic" if problems else "verified"
    )
    common = {"ws": worker.workspace_id, "box": worker.mailbox_binding_id}
    async with mapped_errors():
        health = await fetch_one(conn, _HEALTH_ROW_SQL, {**common, "hash": HEALTH_ROW_HASH})
        previous: list[str] = list(health["gap_reasons"]) if health is not None else []
        codes = [c for c in previous if not c.startswith("account:")][-(MAX_GAP_ENTRIES - 1) :]
        codes.append(f"account:{status}")
        if health is None:
            await conn.execute(
                "insert into ops.mail_worker_checkpoints (workspace_id, mailbox_binding_id, store_id_hash,"
                " folder_id_hash, folder_role, gap_reasons) values (%(ws)s, %(box)s, %(hash)s, %(hash)s,"
                " 'other', %(codes)s)",
                {**common, "hash": HEALTH_ROW_HASH, "codes": codes},
            )
        else:
            await conn.execute(
                "update ops.mail_worker_checkpoints set gap_reasons = %(codes)s,"
                " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s",
                {"ws": worker.workspace_id, "id": health["id"], "codes": codes},
            )
        await audit.record(
            conn,
            worker.actor(request_id),
            "mail_worker.account_report",
            "mail_worker_binding",
            worker.mailbox_binding_id,
            metadata={
                "status": status,
                "outlook_flavour": report.outlook_flavour,
                "outlook_version": report.outlook_version,
                "account_type": report.account_type,
                "stable_account_key_sha256": hashlib.sha256(
                    report.stable_account_key.encode("utf-8")
                ).hexdigest(),
            },
            outcome="succeeded" if not problems else "denied",
        )
    return AccountReportOutcome(accepted=not problems, status=status, problems=tuple(problems))


# --------------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------------


class FolderCheckpointHealth(BaseModel):
    """One hashed store/folder checkpoint as reported by the worker."""

    model_config = _FROZEN

    store_id_hash: str
    folder_id_hash: str
    folder_role: str
    last_complete_scan_at: datetime | None
    last_scan_started_at: datetime | None
    overlap_watermark: datetime | None
    heartbeat_at: datetime | None
    backlog_count: int | None
    backlog_oldest_at: datetime | None
    gap_reasons: tuple[str, ...]


class MailboxHealth(BaseModel):
    """Separate health dimensions of one mailbox worker (spec 37.6); coverage gaps never hidden."""

    model_config = _FROZEN

    mailbox_binding_id: UUID
    sender_binding_id: UUID
    provider: EmailProviderKind
    worker_label: str
    binding_state: Literal["active", "revoked"]
    generated_at: datetime
    last_heartbeat_at: datetime | None
    heartbeat_age_seconds: int | None
    heartbeat_status: ComponentStatus
    outlook_status: ComponentStatus
    mailbox_sync_ok: bool | None
    mailbox_sync_lag: LagMeasurement
    last_successful_reconciliation_at: datetime | None
    reconciliation_status: ComponentStatus
    backlog_count: int | None
    backlog_age: LagMeasurement
    unresolved_matching_gaps: int | None
    account_status: AccountStatus
    coverage_gaps: tuple[ReportedGap, ...]
    open_gap_count: int
    folders: tuple[FolderCheckpointHealth, ...]
    monitoring_active: bool
    reasons: tuple[str, ...]


_MAILBOXES_SQL: Final = """
select m.id, m.sender_binding_id, m.provider, m.worker_label, m.state, m.created_at, m.revoked_at
  from ops.mail_worker_bindings m
 where m.workspace_id = %(ws)s and (%(box)s::uuid is null or m.id = %(box)s::uuid)
   and (%(include_revoked)s or m.state = 'active')
 order by (m.state = 'active') desc, m.created_at desc, m.id
 limit 50
"""
_CHECKPOINTS_SQL: Final = """
select mailbox_binding_id, store_id_hash, folder_id_hash, folder_role, last_complete_scan_at,
       last_scan_started_at, overlap_watermark, heartbeat_at, backlog_count, backlog_oldest_at,
       outlook_connected, mailbox_sync_ok, mailbox_last_sync_at, gap_reasons
  from ops.mail_worker_checkpoints
 where workspace_id = %(ws)s and mailbox_binding_id = any(%(boxes)s::uuid[])
 order by mailbox_binding_id, folder_role, store_id_hash, folder_id_hash
"""


def _utc(value: Any) -> datetime | None:
    return None if value is None else ensure_utc(value)


_ACCOUNT_STATUSES: Final[Mapping[str, AccountStatus]] = {
    "verified": "verified",
    "mismatch": "mismatch",
    "not_classic": "not_classic",
}


def _server_gap(gap: CoverageGap, now: datetime) -> ReportedGap:
    still_open = gap.end >= now
    return ReportedGap(
        kind=gap.reason[:64],
        started_at=gap.start,
        ended_at=None if still_open else gap.end,
        open=still_open,
        detected_by="server",
    )


def _code_value(codes: Sequence[str], prefix: str) -> str | None:
    for code in reversed(codes):
        if code.startswith(prefix):
            return code[len(prefix) :]
    return None


def _health(
    mailbox: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    heartbeat_interval: timedelta,
    reconcile_interval: timedelta,
    window: timedelta,
) -> MailboxHealth:
    health = next(
        (r for r in rows if r["store_id_hash"] == HEALTH_ROW_HASH and r["folder_id_hash"] == HEALTH_ROW_HASH),
        None,
    )
    folders = [r for r in rows if r is not health]
    codes: list[str] = list(health["gap_reasons"]) if health is not None else []
    last_beat = _utc(health["heartbeat_at"]) if health is not None else None
    created = ensure_utc(mailbox["created_at"])
    window_start = max(created, now - window) if last_beat is None else min(last_beat, now)
    beats = []
    if health is not None and last_beat is not None:
        beats.append(
            MailWorkerHeartbeat(
                observed_at=last_beat,
                outlook_connected=health["outlook_connected"],
                mailbox_sync_ok=health["mailbox_sync_ok"],
                mailbox_last_sync_at=_utc(health["mailbox_last_sync_at"]),
                last_reconciliation_completed_at=_utc(health["last_complete_scan_at"]),
                backlog_count=health["backlog_count"],
                backlog_oldest_at=_utc(health["backlog_oldest_at"]),
            )
        )
    report: MailCoverageReport = mail_worker_coverage(
        mailbox["id"],
        beats,
        now=now,
        window_start=window_start,
        heartbeat_interval=heartbeat_interval,
        reconcile_interval=reconcile_interval,
    )
    worker_gaps = parse_gap_codes(codes)
    server_gaps = [_server_gap(g, now) for g in report.gaps]
    gaps = tuple(sorted([*worker_gaps, *server_gaps], key=lambda g: (g.started_at, g.kind)))
    matching = _code_value(codes, "matching_gaps:")
    account = _code_value(codes, "account:")
    account_status = _ACCOUNT_STATUSES.get(account or "", "unknown")
    reasons = list(report.reasons)
    open_gaps = sum(1 for g in gaps if g.open)
    if open_gaps and not any("coverage gap" in r for r in reasons):
        reasons.append(f"{open_gaps} open coverage gap(s)")
    unresolved = int(matching) if matching is not None and matching.isdigit() else None
    if unresolved:
        reasons.append(f"{unresolved} unresolved matching gap(s)")
    if account_status in ("mismatch", "not_classic"):
        reasons.append(f"account check: {account_status}")
    revoked = mailbox["state"] != "active"
    if revoked:
        reasons.append("mailbox worker binding revoked")
    return MailboxHealth(
        mailbox_binding_id=mailbox["id"],
        sender_binding_id=mailbox["sender_binding_id"],
        provider=EmailProviderKind(mailbox["provider"]),
        worker_label=str(mailbox["worker_label"]),
        binding_state="revoked" if revoked else "active",
        generated_at=now,
        last_heartbeat_at=last_beat,
        heartbeat_age_seconds=None if last_beat is None else max(0, int((now - last_beat).total_seconds())),
        heartbeat_status=report.heartbeat_status,
        outlook_status=report.outlook_status,
        mailbox_sync_ok=None if health is None else health["mailbox_sync_ok"],
        mailbox_sync_lag=report.mailbox_sync_lag,
        last_successful_reconciliation_at=report.last_successful_reconciliation_at,
        reconciliation_status=report.reconciliation_status,
        backlog_count=report.backlog_count,
        backlog_age=report.backlog_age,
        unresolved_matching_gaps=unresolved,
        account_status=account_status,
        coverage_gaps=gaps,
        open_gap_count=open_gaps,
        folders=tuple(
            FolderCheckpointHealth(
                store_id_hash=r["store_id_hash"],
                folder_id_hash=r["folder_id_hash"],
                folder_role=r["folder_role"],
                last_complete_scan_at=_utc(r["last_complete_scan_at"]),
                last_scan_started_at=_utc(r["last_scan_started_at"]),
                overlap_watermark=_utc(r["overlap_watermark"]),
                heartbeat_at=_utc(r["heartbeat_at"]),
                backlog_count=r["backlog_count"],
                backlog_oldest_at=_utc(r["backlog_oldest_at"]),
                gap_reasons=tuple(r["gap_reasons"]),
            )
            for r in folders[:20]
        ),
        monitoring_active=report.monitoring_active
        and not revoked
        and open_gaps == 0
        and account_status != "mismatch",
        reasons=tuple(reasons),
    )


async def list_mailbox_health(
    conn: Conn,
    workspace_id: UUID,
    *,
    mailbox_binding_id: UUID | None = None,
    include_revoked: bool = False,
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
    reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
    window: timedelta = DEFAULT_HEALTH_WINDOW,
) -> list[MailboxHealth]:
    """Health of the workspace's mailbox workers (newest active first; at most 50).

    Callers check scopes (``queries.inquiries.mail_worker_health_view``) and run this inside the
    workspace's transaction. Times are database time.
    """
    async with mapped_errors():
        now = await _db_now(conn)
        mailboxes = await fetch_all(
            conn,
            _MAILBOXES_SQL,
            {"ws": workspace_id, "box": mailbox_binding_id, "include_revoked": include_revoked},
        )
        rows = (
            await fetch_all(
                conn, _CHECKPOINTS_SQL, {"ws": workspace_id, "boxes": [m["id"] for m in mailboxes]}
            )
            if mailboxes
            else []
        )
    by_box: dict[UUID, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_box.setdefault(row["mailbox_binding_id"], []).append(row)
    return [
        _health(
            m,
            by_box.get(m["id"], []),
            now=now,
            heartbeat_interval=heartbeat_interval,
            reconcile_interval=reconcile_interval,
            window=window,
        )
        for m in mailboxes
    ]


async def mailbox_health(conn: Conn, worker: WorkerIdentity, **kwargs: Any) -> MailboxHealth:
    """Health of the worker's own mailbox (for the worker/doctor)."""
    reports = await list_mailbox_health(
        conn,
        worker.workspace_id,
        mailbox_binding_id=worker.mailbox_binding_id,
        include_revoked=True,
        **kwargs,
    )
    if not reports:
        raise _revoked_mailbox()
    return reports[0]


__all__ = [
    "BINDING_STATE_FOR",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_RECONCILE_INTERVAL",
    "HEALTH_ROW_HASH",
    "MAX_BINDING_PAGE",
    "SYNC_CURSOR_PREFIX",
    "AccountReportOutcome",
    "AccountStatus",
    "FolderCheckpointHealth",
    "IssuedMailWorker",
    "MailboxHealth",
    "PublishedBinding",
    "ReportedGap",
    "WorkerIdentity",
    "binding_for",
    "binding_item",
    "binding_payload",
    "decode_sync_cursor",
    "domain_binding",
    "encode_sync_cursor",
    "issue_mail_worker",
    "latest_mailbox_bindings",
    "list_binding_changes",
    "list_mailbox_health",
    "mailbox_health",
    "mailbox_mismatch",
    "parse_gap_codes",
    "publish_inquiry_binding",
    "publish_mailbox_bindings",
    "record_account_report",
    "record_heartbeat",
    "require_active_mailbox",
    "resolve_worker",
    "revoke_mail_worker",
    "rotate_mail_worker_credential",
    "tombstone_inquiry_binding",
]
