"""MCP Events subscriptions and deliveries (spec 22; ``integrations/event_bridge.py``).

Tables: ``ops.event_subscriptions`` and ``ops.event_deliveries`` (+ ``ops.audit_events``).

Subscriptions
    - **Identity** comes from the authenticated principal (never request fields), the callback
      URL, the event name and the canonical arguments. The wire id is
      ``event_bridge.subscription_identity`` (``sub_<32 hex>``, includes the workspace); the row
      is unique on ``(principal_id, callback_url, event_name, filter_hash)`` where
      ``filter_hash = sha256({"workspace": ..., "arguments": ...})``, so the same principal in
      two workspaces never collides. Subscribing again REFRESHES the row (new expiry/refresh
      deadline, ``version + 1``); it never duplicates it.
    - **Secrets** are sealed with ``integrations.secret_box`` (AES-256-GCM) using the AAD
      ``subscription_secret_aad(workspace, wire id, secret_version)``, so a ciphertext copied to
      another row or version never opens. A refresh with a DIFFERENT secret rotates: the old
      envelope moves to ``previous_encrypted_secret`` (sealed for ``secret_version - 1``) until
      ``previous_secret_valid_until`` (policy ``secret_rotation_overlap``), the version
      increments and verification restarts (``pending``). Plaintext is never logged or stored.
    - **Lifecycle**: verification state ``pending`` -> ``verified``/``failed``
      (`record_verification` checks the verified secret is still the current one); finite
      ``expires_at`` with ``refresh_deadline``; `unsubscribe` (idempotent, reports whether
      anything matched) and `revoke_subscription` set ``revoked_at``/``revoke_reason`` and cancel
      waiting deliveries, so new deliveries stop immediately.
    - `list_active_for_event` (system dispatcher) returns ``event_bridge.DeliveryTarget``s with
      decrypted signing secrets and rechecks membership/scope first (``check_subscriber_access`` or a
      supplied hook); a subscriber that lost access is revoked in the same transaction.

Deliveries
    One row per ``(subscription_id, event_id)`` (unique; the event id stays stable across
    attempts), only for a committed, non-fixture event that is not blocked or cancelled.
    `claim_due_deliveries` leases due rows with ``FOR UPDATE SKIP LOCKED`` (fresh token,
    ``attempts + 1``) only for active, verified, unexpired subscriptions whose subscriber still
    has access (membership/credential recheck in SQL), cancelling waiting rows of
    revoked/expired ones. `record_delivery_outcome` is fenced on id + token +
    owner + unexpired lease (database time); zero rows -> ``LeaseLost``. Outcomes: 2xx ->
    ``accepted`` (receipt only), 410/413 and other terminal failures -> ``failed``, retryable ->
    ``retry_wait`` (or ``dead_letter`` when attempts are exhausted), timeout after send ->
    ``uncertain`` (never blindly resent; `requeue_uncertain` re-sends the SAME event id once).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.integrations.event_bridge import (
    DEFAULT_POLICY,
    DeliveryOutcome,
    DeliveryOutcomeKind,
    DeliveryTarget,
    SubscriptionPolicy,
    SubscriptionRequest,
    SubscriptionStatus,
    UnsubscribeRequest,
    VerificationResult,
    check_subscription_quota,
    signing_secrets,
    subscription_identity,
)
from suv_deals.integrations.secret_box import SecretBox, SecretDecryptionFailed, subscription_secret_aad
from suv_deals.integrations.webhook_signing import (
    InvalidWebhookSecret,
    WebhookSecret,
    canonical_json,
    parse_whsec,
)
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, db_now, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors

UNSUBSCRIBED_REASON: Final = "unsubscribed"
ACCESS_REVOKED_REASON: Final = "access_revoked"
_REQUIRED_SCOPES: Final = frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE})
#: Member roles whose scopes include both required scopes (owner, reviewer).
_SUBSCRIBER_ROLES: Final = tuple(sorted(r.value for r in Role if ROLE_SCOPES[r] >= _REQUIRED_SCOPES))
_UNDELIVERABLE_EVENT_STATES: Final = frozenset({"blocked", "cancelled"})
_FROZEN = ConfigDict(frozen=True, extra="forbid")
DeliveryState = Literal[
    "pending", "sending", "retry_wait", "accepted", "uncertain", "failed", "dead_letter", "cancelled"
]


def filter_hash_for(workspace_id: UUID, arguments: Mapping[str, Any]) -> str:
    """Hash of the canonical filter, scoped to the workspace (part of the unique identity)."""
    payload = canonical_json({"workspace": str(workspace_id), "arguments": dict(arguments)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_subscriber(actor: ActorContext) -> None:
    if actor.principal_kind == "system":
        raise Forbidden("Subscriptions belong to authenticated users or clients")
    actor.require(Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE)


def _require_system(actor: ActorContext) -> None:
    if actor.principal_kind != "system":
        raise Forbidden("Only the event dispatcher performs this operation")


class SubscriptionRecord(BaseModel):
    """One subscription row (never the secret)."""

    model_config = _FROZEN

    id: UUID
    subscription_id: str
    workspace_id: UUID
    principal_id: UUID
    credential_id: UUID | None
    event_name: str
    arguments: dict[str, str]
    filter_hash: str
    callback_url: str
    secret_version: int
    has_previous_secret: bool
    previous_secret_valid_until: datetime | None
    verification_state: Literal["pending", "verified", "failed"]
    verified_at: datetime | None
    expires_at: datetime
    refresh_deadline: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def status(self) -> SubscriptionStatus:
        if self.revoked_at is not None:
            if self.revoke_reason == UNSUBSCRIBED_REASON:
                return SubscriptionStatus.UNSUBSCRIBED
            return SubscriptionStatus.REVOKED
        if self.verification_state != "verified" or self.verified_at is None:
            return SubscriptionStatus.PENDING_VERIFICATION
        return SubscriptionStatus.ACTIVE


class SubscribeOutcome(BaseModel):
    model_config = _FROZEN

    record: SubscriptionRecord
    created: bool
    refreshed: bool
    secret_rotated: bool
    reactivated: bool

    @property
    def needs_verification(self) -> bool:
        return self.record.status is SubscriptionStatus.PENDING_VERIFICATION


class UnsubscribeOutcome(BaseModel):
    """``matched``: a subscription with this identity exists; ``changed``: it was active."""

    model_config = _FROZEN

    matched: bool
    changed: bool
    subscription_id: str | None


@dataclass(frozen=True, slots=True)
class ActiveSubscriptions:
    targets: tuple[DeliveryTarget, ...]
    records: tuple[SubscriptionRecord, ...]
    revoked: tuple[UUID, ...]  # subscriptions revoked by the access recheck
    undecryptable: tuple[UUID, ...]  # sealed with a key that is no longer configured


AccessHook = Callable[[Conn, SubscriptionRecord], Awaitable[bool]]


_SUB_COLUMNS: Final = (
    "id, workspace_id, principal_id, credential_id, event_name, canonical_filter, filter_hash, callback_url,"
    " encrypted_secret, secret_version, previous_encrypted_secret, previous_secret_valid_until,"
    " verification_state, verified_at, expires_at, refresh_deadline, revoked_at, revoke_reason, version,"
    " created_at, updated_at"
)


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def _record(row: Mapping[str, Any]) -> SubscriptionRecord:
    arguments = {str(k): str(v) for k, v in dict(row["canonical_filter"] or {}).items()}
    return SubscriptionRecord(
        id=row["id"],
        subscription_id=subscription_identity(
            row["principal_id"],
            row["callback_url"],
            row["event_name"],
            arguments,
            workspace_id=row["workspace_id"],
        ),
        workspace_id=row["workspace_id"],
        principal_id=row["principal_id"],
        credential_id=row["credential_id"],
        event_name=row["event_name"],
        arguments=arguments,
        filter_hash=row["filter_hash"],
        callback_url=row["callback_url"],
        secret_version=row["secret_version"],
        has_previous_secret=row["previous_encrypted_secret"] is not None,
        previous_secret_valid_until=_utc(row["previous_secret_valid_until"]),
        verification_state=row["verification_state"],
        verified_at=_utc(row["verified_at"]),
        expires_at=ensure_utc(row["expires_at"]),
        refresh_deadline=_utc(row["refresh_deadline"]),
        revoked_at=_utc(row["revoked_at"]),
        revoke_reason=row["revoke_reason"],
        version=row["version"],
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
    )


def _aad(workspace_id: UUID, wire_id: str, version: int) -> bytes:
    return subscription_secret_aad(workspace_id=workspace_id, subscription_id=wire_id, secret_version=version)


def _seal(box: SecretBox, secret: WebhookSecret, workspace_id: UUID, wire_id: str, version: int) -> bytes:
    return box.seal(secret.to_whsec().encode("ascii"), aad=_aad(workspace_id, wire_id, version))


def _open(box: SecretBox, envelope: bytes, workspace_id: UUID, wire_id: str, version: int) -> WebhookSecret:
    try:
        plaintext = box.open(bytes(envelope), aad=_aad(workspace_id, wire_id, version))
        return parse_whsec(plaintext.decode("ascii"))
    except (UnicodeDecodeError, InvalidWebhookSecret) as exc:
        raise SecretDecryptionFailed() from exc


# --------------------------------------------------------------------------------------------
# Subscribe / refresh / verify / unsubscribe / revoke
# --------------------------------------------------------------------------------------------


async def create_or_refresh_subscription(
    conn: Conn,
    actor: ActorContext,
    request: SubscriptionRequest,
    box: SecretBox,
    *,
    policy: SubscriptionPolicy = DEFAULT_POLICY,
    credential_id: UUID | None = None,
) -> SubscribeOutcome:
    """Persist a validated ``events/subscribe`` for the authenticated principal."""
    _require_subscriber(actor)
    if request.workspace_id != actor.workspace_id or request.principal_id != actor.principal_id:
        raise Forbidden("The subscription must belong to the authenticated principal")
    ws = actor.workspace_id
    fh = filter_hash_for(ws, request.arguments)
    wire = request.subscription_id
    expected_wire = subscription_identity(
        actor.principal_id, request.callback_url, request.event_name, request.arguments, workspace_id=ws
    )
    if wire != expected_wire:
        raise ValidationFailed("subscription identity does not match the request")
    expires_at = ensure_utc(request.expires_at)
    identity = {
        "ws": ws,
        "principal": actor.principal_id,
        "url": request.callback_url,
        "name": request.event_name,
        "filter_hash": fh,
    }
    async with mapped_errors():
        existing = await fetch_one(
            conn,
            f"select {_SUB_COLUMNS} from ops.event_subscriptions where workspace_id = %(ws)s"  # noqa: S608
            " and principal_id = %(principal)s and callback_url = %(url)s and event_name = %(name)s"
            " and filter_hash = %(filter_hash)s for update",
            identity,
        )
        active = await fetch_one(
            conn,
            "select count(*) as n from ops.event_subscriptions where workspace_id = %(ws)s"
            " and principal_id = %(principal)s and revoked_at is null and expires_at > clock_timestamp()"
            " and not (callback_url = %(url)s and event_name = %(name)s and filter_hash = %(filter_hash)s)",
            identity,
        )
        assert active is not None
        now = ensure_utc(await db_now(conn))
        is_refresh = (
            existing is not None
            and existing["revoked_at"] is None
            and ensure_utc(existing["expires_at"]) > now
        )
        check_subscription_quota(int(active["n"]), is_refresh=is_refresh, policy=policy)
        if expires_at <= now:
            raise ValidationFailed("the granted subscription lifetime has already ended")
        if existing is None:
            row = await fetch_one(
                conn,
                "insert into ops.event_subscriptions (workspace_id, principal_id, credential_id, event_name,"  # noqa: S608
                " canonical_filter, filter_hash, callback_url, encrypted_secret, secret_version,"
                " verification_state, expires_at, refresh_deadline) values (%(ws)s, %(principal)s,"
                " %(credential)s, %(name)s, %(filter)s, %(filter_hash)s, %(url)s, %(secret)s, 1, 'pending',"
                f" %(expires_at)s, %(expires_at)s) returning {_SUB_COLUMNS}",
                {
                    **identity,
                    "credential": credential_id,
                    "filter": _jsonb(request.arguments),
                    "secret": _seal(box, request.secret, ws, wire, 1),
                    "expires_at": expires_at,
                },
            )
            assert row is not None
            await audit.record(
                conn,
                actor,
                "event_subscription.create",
                "event_subscription",
                row["id"],
                None,
                1,
                metadata={"event_name": request.event_name, "subscription": wire},
            )
            return SubscribeOutcome(
                record=_record(row), created=True, refreshed=False, secret_rotated=False, reactivated=False
            )
        current = _record(existing)
        rotated = False
        previous_envelope: bytes | None = existing["previous_encrypted_secret"]
        previous_until: datetime | None = _utc(existing["previous_secret_valid_until"])
        version = current.secret_version
        envelope: bytes = bytes(existing["encrypted_secret"])
        try:
            stored_secret: WebhookSecret | None = _open(box, envelope, ws, wire, version)
        except SecretDecryptionFailed:
            stored_secret = None
        if stored_secret is None or not stored_secret.matches(request.secret):
            rotated = True
            if stored_secret is not None:
                previous_envelope = envelope  # stays sealed for its own (old) version
                previous_until = now + policy.secret_rotation_overlap
            else:
                previous_envelope, previous_until = None, None
            version += 1
            envelope = _seal(box, request.secret, ws, wire, version)
        elif previous_until is not None and previous_until <= now:
            previous_envelope, previous_until = None, None  # the rotation window has closed
        reactivated = current.revoked_at is not None
        reset_verification = rotated or reactivated
        row = await fetch_one(
            conn,
            "update ops.event_subscriptions set expires_at = %(expires_at)s,"  # noqa: S608
            " refresh_deadline = %(expires_at)s,"
            " credential_id = coalesce(%(credential)s, credential_id), encrypted_secret = %(secret)s,"
            " secret_version = %(version)s, previous_encrypted_secret = %(previous)s,"
            " previous_secret_valid_until = %(previous_until)s,"
            " verification_state = case when %(reset)s then 'pending' else verification_state end,"
            " verified_at = case when %(reset)s then null else verified_at end,"
            " verification_challenge_hash = case when %(reset)s then null"
            "   else verification_challenge_hash end,"
            " challenge_expires_at = case when %(reset)s then null else challenge_expires_at end,"
            " revoked_at = null, revoke_reason = null, version = version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_SUB_COLUMNS}",
            {
                "ws": ws,
                "id": current.id,
                "expires_at": expires_at,
                "credential": credential_id,
                "secret": envelope,
                "version": version,
                "previous": previous_envelope,
                "previous_until": previous_until,
                "reset": reset_verification,
            },
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "event_subscription.refresh",
            "event_subscription",
            current.id,
            current.version,
            row["version"],
            metadata={"subscription": wire, "reactivated": reactivated},
        )
        if rotated:
            await audit.record(
                conn,
                actor,
                "secret.rotate",
                "event_subscription",
                current.id,
                current.secret_version,
                version,
                metadata={"subscription": wire},
            )
    return SubscribeOutcome(
        record=_record(row), created=False, refreshed=True, secret_rotated=rotated, reactivated=reactivated
    )


def _jsonb(value: Mapping[str, Any]) -> Jsonb:
    return Jsonb(dict(value))


async def _load_row(conn: Conn, actor: ActorContext, row_id: UUID, *, lock: bool) -> Any:
    query = (
        f"select {_SUB_COLUMNS} from ops.event_subscriptions"  # noqa: S608 - fixed column list
        " where workspace_id = %(ws)s and id = %(id)s" + (" for update" if lock else "")
    )
    async with mapped_errors():
        row = await fetch_one(conn, query, {"ws": actor.workspace_id, "id": row_id})
    if row is None:
        raise NotFound("Subscription not found")
    return row


def _visible_to(actor: ActorContext, record: SubscriptionRecord) -> bool:
    return (
        actor.principal_kind == "system"
        or record.principal_id == actor.principal_id
        or actor.has(Scope.CONFIG_ADMIN)
    )


async def get_subscription(conn: Conn, actor: ActorContext, row_id: UUID) -> SubscriptionRecord:
    """A subscription of the caller (owner/system see every subscription of the workspace)."""
    if actor.principal_kind != "system":
        actor.require(Scope.REVIEWS_READ)
    record = _record(await _load_row(conn, actor, row_id, lock=False))
    if not _visible_to(actor, record):
        raise NotFound("Subscription not found")
    return record


async def list_subscriptions(
    conn: Conn, actor: ActorContext, *, include_revoked: bool = False
) -> list[SubscriptionRecord]:
    """The caller's subscriptions (all of the workspace for the owner/system)."""
    if actor.principal_kind != "system":
        actor.require(Scope.REVIEWS_READ)
    everyone = actor.principal_kind == "system" or actor.has(Scope.CONFIG_ADMIN)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_SUB_COLUMNS} from ops.event_subscriptions where workspace_id = %(ws)s"  # noqa: S608
            " and (%(everyone)s or principal_id = %(principal)s)"
            " and (%(revoked)s or revoked_at is null) order by created_at, id",
            {
                "ws": actor.workspace_id,
                "everyone": everyone,
                "principal": actor.principal_id,
                "revoked": include_revoked,
            },
        )
    return [_record(r) for r in rows]


async def record_verification(
    conn: Conn, actor: ActorContext, row_id: UUID, result: VerificationResult, box: SecretBox
) -> SubscriptionRecord:
    """Store the outcome of the callback challenge for the CURRENT secret version. An outcome
    of an attempt older than the recorded verification is ignored (late result)."""
    if actor.principal_kind != "system":
        actor.require(Scope.EVENTS_SUBSCRIBE)
    row = await _load_row(conn, actor, row_id, lock=True)
    record = _record(row)
    if actor.principal_kind != "system" and record.principal_id != actor.principal_id:
        raise NotFound("Subscription not found")
    secret = _open(
        box,
        bytes(row["encrypted_secret"]),
        record.workspace_id,
        record.subscription_id,
        record.secret_version,
    )
    if secret.fingerprint != result.secret_fingerprint:
        raise VersionConflict("The subscription secret changed during verification; verify again")
    if record.revoked_at is not None:
        raise VersionConflict("The subscription is no longer active")
    if record.verified_at is not None and ensure_utc(result.attempted_at) < record.verified_at:
        # A late outcome of an attempt that started before the recorded successful verification
        # (attempts can finish out of order): it never overrides the newer fact.
        return record
    verified_at = result.verified_at or result.attempted_at
    async with mapped_errors():
        updated = await fetch_one(
            conn,
            "update ops.event_subscriptions set verification_state = %(state)s,"  # noqa: S608
            " verified_at = case when %(ok)s then %(verified_at)s else verified_at end,"
            " verification_challenge_hash = null, challenge_expires_at = null, version = version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_SUB_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "id": row_id,
                "state": "verified" if result.ok else "failed",
                "ok": result.ok,
                "verified_at": ensure_utc(verified_at),
            },
        )
        assert updated is not None
        await audit.record(
            conn,
            actor,
            "event_subscription.verify",
            "event_subscription",
            row_id,
            record.version,
            updated["version"],
            outcome="succeeded" if result.ok else "failed",
            metadata={"reason": None if result.reason is None else result.reason.value},
        )
    return _record(updated)


async def unsubscribe(conn: Conn, actor: ActorContext, request: UnsubscribeRequest) -> UnsubscribeOutcome:
    """Idempotent ``events/unsubscribe`` for the authenticated principal.

    ``matched=False`` when no subscription has this identity (nothing changes; the wire result
    stays ``{}``); a second call reports ``matched=True, changed=False``.
    """
    if actor.principal_kind == "system":
        raise Forbidden("Unsubscribe is performed by the subscriber")
    actor.require(Scope.EVENTS_SUBSCRIBE)
    if request.workspace_id != actor.workspace_id or request.principal_id != actor.principal_id:
        raise Forbidden("The subscription must belong to the authenticated principal")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_SUB_COLUMNS} from ops.event_subscriptions"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and principal_id = %(principal)s order by id for update",
            {"ws": actor.workspace_id, "principal": actor.principal_id},
        )
    match = next((r for r in rows if _record(r).subscription_id == request.subscription_id), None)
    if match is None:
        return UnsubscribeOutcome(matched=False, changed=False, subscription_id=None)
    record = _record(match)
    if record.revoked_at is not None:
        return UnsubscribeOutcome(matched=True, changed=False, subscription_id=record.subscription_id)
    await _revoke(conn, actor, record, UNSUBSCRIBED_REASON, "event_subscription.unsubscribe")
    return UnsubscribeOutcome(matched=True, changed=True, subscription_id=record.subscription_id)


async def revoke_subscription(
    conn: Conn, actor: ActorContext, row_id: UUID, reason: str
) -> SubscriptionRecord:
    """Stop a subscription now (owner, the dispatcher after a failed access recheck, or the
    subscriber itself). Waiting deliveries are cancelled. Idempotent."""
    if actor.principal_kind != "system" and not actor.scopes & {Scope.EVENTS_SUBSCRIBE, Scope.CONFIG_ADMIN}:
        raise Forbidden("Missing scope: events:subscribe")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 200:
        raise ValidationFailed("reason must be 3-200 characters")
    row = await _load_row(conn, actor, row_id, lock=True)
    record = _record(row)
    if not _visible_to(actor, record):
        raise NotFound("Subscription not found")
    if record.revoked_at is not None:
        return record
    return await _revoke(conn, actor, record, reason.strip(), "event_subscription.revoke")


async def _revoke(
    conn: Conn, actor: ActorContext, record: SubscriptionRecord, reason: str, action: str
) -> SubscriptionRecord:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update ops.event_subscriptions set revoked_at = clock_timestamp(),"  # noqa: S608
            " revoke_reason = %(reason)s,"
            f" version = version + 1 where workspace_id = %(ws)s and id = %(id)s returning {_SUB_COLUMNS}",
            {"ws": record.workspace_id, "id": record.id, "reason": reason},
        )
        assert row is not None
        await conn.execute(
            "update ops.event_deliveries set state = 'cancelled', safe_error = 'subscription_inactive',"
            " lease_owner = null, lease_token = null, lease_expires_at = null"
            " where workspace_id = %(ws)s and subscription_id = %(id)s"
            " and state in ('pending', 'retry_wait')",
            {"ws": record.workspace_id, "id": record.id},
        )
        await audit.record(
            conn,
            actor,
            action,
            "event_subscription",
            record.id,
            record.version,
            row["version"],
            reason=reason,
            metadata={"subscription": record.subscription_id},
        )
    return _record(row)


# --------------------------------------------------------------------------------------------
# Dispatch support
# --------------------------------------------------------------------------------------------


async def check_subscriber_access(conn: Conn, actor: ActorContext, record: SubscriptionRecord) -> bool:
    """Membership/scope recheck before dispatch (system dispatcher): the subscriber still holds
    ``reviews:read`` and ``events:subscribe``: an active membership whose role grants both for
    users (also when they subscribed with a static credential, which must then be unrevoked,
    unexpired and carry both scopes); for MCP-client credentials the credential alone.
    `claim_due_deliveries` applies the same rule in SQL."""
    _require_system(actor)
    if record.workspace_id != actor.workspace_id:
        return False
    params = {"ws": record.workspace_id, "principal": record.principal_id, "credential": record.credential_id}
    async with mapped_errors():
        if record.credential_id is not None:
            credential = await fetch_one(
                conn,
                "select scopes, principal_kind from ops.api_credentials where workspace_id = %(ws)s"
                " and id = %(credential)s and principal_id = %(principal)s and revoked_at is null"
                " and expires_at > clock_timestamp()",
                params,
            )
            if credential is None or not set(credential["scopes"]) >= _REQUIRED_SCOPES:
                return False
            if credential["principal_kind"] != "user":
                return True
            # A user's static credential never outlives the user's membership or role.
        row = await fetch_one(
            conn,
            "select role from app.memberships where workspace_id = %(ws)s and user_id = %(principal)s"
            " and active",
            params,
        )
    return row is not None and ROLE_SCOPES[Role(row["role"])] >= _REQUIRED_SCOPES


async def list_active_for_event(
    conn: Conn,
    actor: ActorContext,
    event_name: str,
    box: SecretBox,
    *,
    access_check: AccessHook | None = None,
) -> ActiveSubscriptions:
    """Verified, unrevoked, unexpired subscriptions of ``event_name`` with their signing
    secrets (newest first, previous one only inside its rotation window). Subscribers that
    fail the access recheck are revoked here (same transaction) and excluded."""
    _require_system(actor)

    async def default_hook(c: Conn, r: SubscriptionRecord) -> bool:
        return await check_subscriber_access(c, actor, r)

    hook = access_check or default_hook
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_SUB_COLUMNS} from ops.event_subscriptions where workspace_id = %(ws)s"  # noqa: S608
            " and event_name = %(name)s and revoked_at is null and verification_state = 'verified'"
            " and expires_at > clock_timestamp() order by id for update",
            {"ws": actor.workspace_id, "name": event_name},
        )
    now = ensure_utc(await db_now(conn))
    targets: list[DeliveryTarget] = []
    records: list[SubscriptionRecord] = []
    revoked: list[UUID] = []
    broken: list[UUID] = []
    for row in rows:
        record = _record(row)
        if not await hook(conn, record):
            await _revoke(conn, actor, record, ACCESS_REVOKED_REASON, "event_subscription.revoke")
            revoked.append(record.id)
            continue
        try:
            current = _open(
                box,
                bytes(row["encrypted_secret"]),
                record.workspace_id,
                record.subscription_id,
                record.secret_version,
            )
            previous = None
            if row["previous_encrypted_secret"] is not None and record.secret_version > 1:
                try:
                    previous = _open(
                        box,
                        bytes(row["previous_encrypted_secret"]),
                        record.workspace_id,
                        record.subscription_id,
                        record.secret_version - 1,
                    )
                except SecretDecryptionFailed:
                    previous = None
        except SecretDecryptionFailed:
            broken.append(record.id)
            continue
        targets.append(
            DeliveryTarget(
                subscription_id=record.subscription_id,
                workspace_id=record.workspace_id,
                principal_id=record.principal_id,
                event_name=record.event_name,
                arguments=dict(record.arguments),
                callback_url=record.callback_url,
                secrets=signing_secrets(current, previous, record.previous_secret_valid_until, now=now),
                status=record.status,
                expires_at=record.expires_at,
                verified_at=record.verified_at,
            )
        )
        records.append(record)
    return ActiveSubscriptions(
        targets=tuple(targets), records=tuple(records), revoked=tuple(revoked), undecryptable=tuple(broken)
    )


class DeliveryRecord(BaseModel):
    model_config = _FROZEN

    id: UUID
    workspace_id: UUID
    subscription_id: UUID
    event_id: UUID
    state: DeliveryState
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_token: UUID | None
    lease_expires_at: datetime | None
    last_response_code: int | None
    safe_error: str | None
    accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClaimedDelivery(DeliveryRecord):
    lease_owner: str
    lease_token: UUID
    lease_expires_at: datetime


_DELIVERY_COLUMNS: Final = (
    "id, workspace_id, subscription_id, event_id, state, attempts, max_attempts, next_attempt_at,"
    " lease_owner,"
    " lease_token, lease_expires_at, last_response_code, safe_error, accepted_at, created_at, updated_at"
)


def _delivery(row: Mapping[str, Any]) -> DeliveryRecord:
    return DeliveryRecord.model_validate(dict(row))


async def create_deliveries(
    conn: Conn,
    actor: ActorContext,
    event_id: UUID,
    subscription_ids: Sequence[UUID],
    *,
    max_attempts: int = 5,
) -> list[tuple[UUID, bool]]:
    """One delivery per (subscription, event); an existing pair returns ``created=False``."""
    _require_system(actor)
    if not 1 <= max_attempts <= 50:
        raise ValidationFailed("max_attempts must be between 1 and 50")
    results: list[tuple[UUID, bool]] = []
    async with mapped_errors():
        event = await fetch_one(
            conn,
            "select is_fixture, state from ops.outbox where workspace_id = %(ws)s and event_id = %(event)s",
            {"ws": actor.workspace_id, "event": event_id},
        )
        if event is None:
            raise NotFound("Event not found")
        if event["is_fixture"] or event["state"] in _UNDELIVERABLE_EVENT_STATES:
            # Fixtures never produce external notifications (spec 18); blocked/cancelled events
            # stay visible in the outbox but are never delivered.
            raise ValidationFailed("this event is not deliverable", details={"state": event["state"]})
        for subscription_id in dict.fromkeys(subscription_ids):
            params = {
                "ws": actor.workspace_id,
                "sub": subscription_id,
                "event": event_id,
                "max": max_attempts,
            }
            row = await fetch_one(
                conn,
                "insert into ops.event_deliveries (workspace_id, subscription_id, event_id, max_attempts)"
                " values (%(ws)s, %(sub)s, %(event)s, %(max)s)"
                " on conflict (subscription_id, event_id) do nothing returning id",
                params,
            )
            if row is not None:
                results.append((row["id"], True))
                continue
            existing = await fetch_one(
                conn,
                "select id from ops.event_deliveries where workspace_id = %(ws)s"
                " and subscription_id = %(sub)s"
                " and event_id = %(event)s",
                params,
            )
            if existing is None:
                raise NotFound("Subscription not found")
            results.append((existing["id"], False))
    return results


_CANCEL_INACTIVE_SQL: Final = """
update ops.event_deliveries d set state = 'cancelled', safe_error = 'subscription_inactive'
  from ops.event_subscriptions s
 where d.workspace_id = %(ws)s and s.workspace_id = d.workspace_id and s.id = d.subscription_id
   and d.state in ('pending', 'retry_wait')
   and (s.revoked_at is not null or s.expires_at <= clock_timestamp())
"""

# The subscriber still has access (same rule as `check_subscriber_access`): deliveries of a
# subscriber that lost it stay waiting (never leased) until the access recheck in
# `list_active_for_event` revokes the subscription or the access is restored.
_MEMBER_PREDICATE: Final = (
    "exists (select 1 from app.memberships m where m.workspace_id = s.workspace_id"
    " and m.user_id = s.principal_id and m.active and m.role = any(%(roles)s::text[]))"
)
_ACCESS_PREDICATE: Final = (
    f"((s.credential_id is null and {_MEMBER_PREDICATE})"  # noqa: S608 - fixed SQL fragments
    " or (s.credential_id is not null and exists (select 1 from ops.api_credentials c"
    "   where c.workspace_id = s.workspace_id and c.id = s.credential_id"
    "     and c.principal_id = s.principal_id and c.revoked_at is null"
    "     and c.expires_at > clock_timestamp() and c.scopes @> %(required)s::text[]"
    f"     and (c.principal_kind <> 'user' or {_MEMBER_PREDICATE}))))"
)

_CLAIM_DELIVERIES_SQL: Final = f"""
with picked as (
  select d.id
    from ops.event_deliveries d
    join ops.event_subscriptions s on s.workspace_id = d.workspace_id and s.id = d.subscription_id
   where d.workspace_id = %(ws)s
     and d.state in ('pending', 'retry_wait')
     and d.next_attempt_at <= now()
     and d.attempts < d.max_attempts
     and s.revoked_at is null and s.verification_state = 'verified'
     and s.expires_at > clock_timestamp()
     and {_ACCESS_PREDICATE}
   order by d.next_attempt_at, d.id
   for update of d skip locked
   limit %(limit)s
)
update ops.event_deliveries d
   set state = 'sending', lease_owner = %(owner)s, lease_token = gen_random_uuid(),
       lease_expires_at = now() + %(lease)s::interval, attempts = d.attempts + 1
  from picked
 where d.id = picked.id and d.workspace_id = %(ws)s
returning {", ".join("d." + c.strip() for c in _DELIVERY_COLUMNS.split(","))}
"""  # noqa: S608 - fixed column list


async def claim_due_deliveries(
    conn: Conn,
    actor: ActorContext,
    dispatcher_id: str,
    *,
    lease_seconds: float = 60.0,
    limit: int = 10,
) -> list[ClaimedDelivery]:
    """Lease due deliveries of active subscriptions (``SKIP LOCKED``; fresh token per lease)."""
    _require_system(actor)
    if not isinstance(dispatcher_id, str) or not 1 <= len(dispatcher_id) <= 200:
        raise ValidationFailed("dispatcher_id must be 1-200 characters")
    if not 0.05 <= float(lease_seconds) <= 3600 or not 1 <= limit <= 100:
        raise ValidationFailed("invalid lease or limit")
    params = {
        "ws": actor.workspace_id,
        "owner": dispatcher_id,
        "lease": timedelta(seconds=float(lease_seconds)),
        "limit": limit,
        "roles": list(_SUBSCRIBER_ROLES),
        "required": sorted(s.value for s in _REQUIRED_SCOPES),
    }
    async with mapped_errors():
        await conn.execute(_CANCEL_INACTIVE_SQL, params)
        rows = await fetch_all(conn, _CLAIM_DELIVERIES_SQL, params)
    claimed = [ClaimedDelivery.model_validate(dict(r)) for r in rows]
    return sorted(claimed, key=lambda d: (d.next_attempt_at, str(d.id)))


_FENCE: Final = (
    " where workspace_id = %(ws)s and id = %(id)s and state = 'sending' and lease_token = %(token)s"
    " and lease_owner = %(owner)s and lease_expires_at > clock_timestamp()"
)


async def record_delivery_outcome(
    conn: Conn, actor: ActorContext, delivery: ClaimedDelivery, outcome: DeliveryOutcome
) -> DeliveryRecord:
    """Apply one ``event_bridge.deliver`` outcome to the leased delivery (fenced)."""
    _require_system(actor)
    if delivery.workspace_id != actor.workspace_id:
        raise LeaseLost()
    kind = outcome.kind
    error = None if outcome.reason is None else outcome.reason.value
    params: dict[str, Any] = {
        "ws": actor.workspace_id,
        "id": delivery.id,
        "token": delivery.lease_token,
        "owner": delivery.lease_owner,
        "code": outcome.status_code,
        "error": None if error is None else error[:500],
        "next_at": None if outcome.next_attempt_at is None else ensure_utc(outcome.next_attempt_at),
        "accepted_at": None
        if outcome.provider_accepted_at is None
        else ensure_utc(outcome.provider_accepted_at),
    }
    release = "lease_owner = null, lease_token = null, lease_expires_at = null"
    if kind is DeliveryOutcomeKind.DELIVERED:
        assignments = (
            "state = 'accepted', accepted_at = coalesce(%(accepted_at)s::timestamptz, clock_timestamp()),"
            " last_response_code = %(code)s, safe_error = null, " + release
        )
    elif kind is DeliveryOutcomeKind.RETRY or (
        kind is DeliveryOutcomeKind.SKIPPED
        and not outcome.revoke_subscription
        and outcome.block_reason is None
    ):
        assignments = (
            "state = case when attempts < max_attempts then 'retry_wait' else 'dead_letter' end,"
            " next_attempt_at = case when attempts < max_attempts then greatest(clock_timestamp(),"
            "   coalesce(%(next_at)s::timestamptz, clock_timestamp() + interval '30 seconds'))"
            "   else next_attempt_at end,"
            " last_response_code = %(code)s, safe_error = %(error)s, " + release
        )
    elif kind is DeliveryOutcomeKind.UNCERTAIN:
        assignments = "state = 'uncertain', last_response_code = %(code)s, safe_error = %(error)s, " + release
    elif kind is DeliveryOutcomeKind.DEAD_LETTER:
        assignments = (
            "state = 'dead_letter', last_response_code = %(code)s, safe_error = %(error)s, " + release
        )
    elif kind is DeliveryOutcomeKind.FAILED:
        assignments = "state = 'failed', last_response_code = %(code)s, safe_error = %(error)s, " + release
    else:  # SKIPPED because the subscription is inactive or its access was revoked
        assignments = (
            "state = 'cancelled', safe_error = coalesce(%(error)s, 'subscription_inactive'), " + release
        )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"update ops.event_deliveries set {assignments}{_FENCE} returning {_DELIVERY_COLUMNS}",  # noqa: S608
            params,
        )
    if row is None:
        raise LeaseLost()
    if outcome.revoke_subscription:
        sub = await _load_row(conn, actor, delivery.subscription_id, lock=True)
        record = _record(sub)
        if record.revoked_at is None:
            await _revoke(conn, actor, record, ACCESS_REVOKED_REASON, "event_subscription.revoke")
    return _delivery(row)


async def reap_expired_deliveries(conn: Conn, actor: ActorContext, *, limit: int = 500) -> list[UUID]:
    """A dispatcher that lost its lease may already have sent the request: ``uncertain``."""
    _require_system(actor)
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid reaper limit")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "with expired as (select id from ops.event_deliveries where workspace_id = %(ws)s"
            " and state = 'sending' and lease_expires_at <= clock_timestamp()"
            " order by lease_expires_at, id for update skip locked limit %(limit)s)"
            " update ops.event_deliveries d set state = 'uncertain', safe_error = 'dispatcher_lost_lease',"
            " lease_owner = null, lease_token = null, lease_expires_at = null"
            " from expired where d.id = expired.id and d.workspace_id = %(ws)s returning d.id",
            {"ws": actor.workspace_id, "limit": limit},
        )
    return [r["id"] for r in rows]


async def requeue_uncertain(conn: Conn, actor: ActorContext, delivery_id: UUID) -> DeliveryRecord:
    """The documented conservative rule: re-send an uncertain delivery with the SAME event id
    (the receiver deduplicates on ``webhook-id``). The dispatcher decides when, via
    ``event_bridge.decide_uncertain_followup``; attempts stay bounded by ``max_attempts``."""
    _require_system(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update ops.event_deliveries set state = case when attempts < max_attempts"  # noqa: S608
            " then 'retry_wait'"
            " else 'dead_letter' end, next_attempt_at = clock_timestamp()"
            " where workspace_id = %(ws)s and id = %(id)s and state = 'uncertain'"
            f" returning {_DELIVERY_COLUMNS}",
            {"ws": actor.workspace_id, "id": delivery_id},
        )
    if row is None:
        raise VersionConflict("Only an uncertain delivery can be re-queued")
    return _delivery(row)


async def list_deliveries(
    conn: Conn, actor: ActorContext, *, event_id: UUID | None = None, subscription_id: UUID | None = None
) -> list[DeliveryRecord]:
    if actor.principal_kind != "system":
        actor.require(Scope.REVIEWS_READ)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_DELIVERY_COLUMNS} from ops.event_deliveries where workspace_id = %(ws)s"  # noqa: S608
            " and (%(event)s::uuid is null or event_id = %(event)s)"
            " and (%(sub)s::uuid is null or subscription_id = %(sub)s)"
            " order by created_at, id limit 500",
            {"ws": actor.workspace_id, "event": event_id, "sub": subscription_id},
        )
    return [_delivery(r) for r in rows]


__all__ = [
    "ACCESS_REVOKED_REASON",
    "UNSUBSCRIBED_REASON",
    "AccessHook",
    "ActiveSubscriptions",
    "ClaimedDelivery",
    "DeliveryRecord",
    "SubscribeOutcome",
    "SubscriptionRecord",
    "UnsubscribeOutcome",
    "check_subscriber_access",
    "claim_due_deliveries",
    "create_deliveries",
    "create_or_refresh_subscription",
    "filter_hash_for",
    "get_subscription",
    "list_active_for_event",
    "list_deliveries",
    "list_subscriptions",
    "reap_expired_deliveries",
    "record_delivery_outcome",
    "record_verification",
    "requeue_uncertain",
    "revoke_subscription",
    "unsubscribe",
]
