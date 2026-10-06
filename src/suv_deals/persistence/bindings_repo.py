"""Approved notification destinations and the activation route per event category (spec 22, 32).

Tables: ``app.destination_bindings`` and ``app.notification_preferences`` (one per binding).

- **Owner only.** Every change needs an authenticated owner (``config:admin``); system workers
  can read routes but never create, approve or enable one. External ids are identifiers
  (Slack team/channel ids, an MCP Events app id), never secrets or webhook URLs.
- **Approval before enabling.** A binding or preference can only be enabled once an approval is
  recorded: ``approval_reference`` (where/how the owner approved, 3-500 characters) plus the
  approving principal and time, all from the authenticated actor (CHECKs enforce it too).
- **Quiet hours** are stored as ``domain.notifications.QuietHours``; PROPOSED values
  (``approved=false``) are kept but never applied by the dispatcher.
- **One activation route per event category.** A route is an enabled preference whose binding
  is enabled and lists the category. Every change that could activate a route locks the
  workspace's preferences (ordered by id) and refuses a second active route for a category, so
  two concurrent enables cannot both win. Category policy (spec 22, v1.1 ADR 0002):

  | Category | Allowed providers | Preferred |
  |---|---|---|
  | ``candidate_discovery`` (``review.pending``) | ``mcp_events``, ``slack`` (fallback) | ``mcp_events`` |
  | ``owner_alert`` (``review.shortlisted``) | ``slack``, ``mcp_events`` | ``slack`` |
  | ``seller_reply`` | ``slack`` only | ``slack`` |

Optimistic concurrency: every mutation takes ``expected_version`` (``row_version``); a stale
value is ``VERSION_CONFLICT``. Foreign or missing rows are ``NOT_FOUND``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.domain.notifications import QuietHours
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

Provider = Literal["slack", "mcp_events"]
EventCategory = Literal["candidate_discovery", "owner_alert", "seller_reply"]

CATEGORY_PROVIDERS: Final[dict[str, tuple[Provider, ...]]] = {
    "candidate_discovery": ("mcp_events", "slack"),
    "owner_alert": ("slack", "mcp_events"),
    "seller_reply": ("slack",),
}
PREFERRED_PROVIDER: Final[dict[str, Provider]] = {
    "candidate_discovery": "mcp_events",
    "owner_alert": "slack",
    "seller_reply": "slack",
}
_FROZEN = ConfigDict(frozen=True, extra="forbid")
_EXTERNAL_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_CONTROL_RE: Final = re.compile("[\\x00-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]")


class DestinationBinding(BaseModel):
    model_config = _FROZEN

    id: UUID
    provider: Provider
    label: str
    external_workspace_id: str | None
    external_channel_id: str | None
    external_app_id: str | None
    approval_reference: str | None
    approved_by: UUID | None
    approved_at: datetime | None
    enabled: bool
    verified_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class NotificationPreference(BaseModel):
    model_config = _FROZEN

    id: UUID
    destination_binding_id: UUID
    event_categories: tuple[EventCategory, ...]
    quiet_hours: QuietHours | None
    urgency_policy: dict[str, Any] | None
    enabled: bool
    approval_reference: str | None
    approved_by: UUID | None
    approved_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ActivationRouteSelection(BaseModel):
    """The single active route of one event category."""

    model_config = _FROZEN

    category: EventCategory
    provider: Provider
    binding_id: UUID
    preference_id: UUID
    preferred: bool
    quiet_hours: QuietHours | None


def _require_owner(actor: ActorContext) -> None:
    if actor.principal_kind == "system":
        raise Forbidden("Notification destinations are approved by the owner, never by system workers")
    actor.require(Scope.CONFIG_ADMIN)


def _require_reader(actor: ActorContext) -> None:
    if actor.principal_kind != "system":
        actor.require(Scope.DEALS_READ)


def _approval_text(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 3 <= len(text) <= 500 or _CONTROL_RE.search(text):
        raise ValidationFailed("approval_reference must be 3-500 printable characters")
    if redact(text) != text:
        raise ValidationFailed("approval_reference must not contain credentials or contact data")
    return text


def _external_id(value: str | None, name: str) -> str | None:
    if value is not None and not _EXTERNAL_ID_RE.fullmatch(value):
        raise ValidationFailed(f"{name} must be an identifier (never a secret or URL)")
    return value


def _categories(values: Sequence[str]) -> list[str]:
    result = list(dict.fromkeys(values))
    unknown = [c for c in result if c not in CATEGORY_PROVIDERS]
    if unknown or not result:
        raise ValidationFailed("event_categories must name known categories", details={"fields": ["event_categories"]})
    return result


_BINDING_COLUMNS: Final = (
    "id, provider, label, external_workspace_id, external_channel_id, external_app_id, approval_reference,"
    " approved_by, approved_at, enabled, verified_at, row_version, created_at, updated_at"
)
_PREFERENCE_COLUMNS: Final = (
    "id, destination_binding_id, event_categories, quiet_hours, urgency_policy, enabled, approval_reference,"
    " approved_by, approved_at, row_version, created_at, updated_at"
)


def _binding(row: Mapping[str, Any]) -> DestinationBinding:
    return DestinationBinding.model_validate(dict(row))


def _preference(row: Mapping[str, Any]) -> NotificationPreference:
    data = dict(row)
    try:
        data["quiet_hours"] = None if row["quiet_hours"] is None else QuietHours.model_validate(row["quiet_hours"])
    except ValidationError as exc:
        raise ValidationFailed("stored quiet hours are invalid") from exc
    data["event_categories"] = tuple(row["event_categories"] or ())
    return NotificationPreference.model_validate(data)


# --------------------------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------------------------


async def create_binding(
    conn: Conn,
    actor: ActorContext,
    *,
    provider: Provider,
    label: str,
    external_workspace_id: str | None = None,
    external_channel_id: str | None = None,
    external_app_id: str | None = None,
) -> DestinationBinding:
    """A new destination, always disabled and unapproved."""
    _require_owner(actor)
    if provider not in ("slack", "mcp_events"):
        raise ValidationFailed("unknown provider")
    text = label.strip() if isinstance(label, str) else ""
    if not 3 <= len(text) <= 120 or _CONTROL_RE.search(text):
        raise ValidationFailed("label must be 3-120 printable characters")
    if provider == "slack" and (external_workspace_id is None or external_channel_id is None):
        raise ValidationFailed("a Slack destination needs the team and channel ids")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.destination_bindings (workspace_id, provider, label, external_workspace_id,"
            " external_channel_id, external_app_id) values (%(ws)s, %(provider)s, %(label)s, %(team)s,"
            f" %(channel)s, %(app)s) returning {_BINDING_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "provider": provider,
                "label": text,
                "team": _external_id(external_workspace_id, "external_workspace_id"),
                "channel": _external_id(external_channel_id, "external_channel_id"),
                "app": _external_id(external_app_id, "external_app_id"),
            },
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.binding_create", "destination_binding", row["id"], None, 1,
            metadata={"provider": provider},
        )
    return _binding(row)


async def get_binding(conn: Conn, actor: ActorContext, binding_id: UUID) -> DestinationBinding:
    _require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_BINDING_COLUMNS} from app.destination_bindings"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id},
        )
    if row is None:
        raise NotFound("Destination binding not found")
    return _binding(row)


async def list_bindings(conn: Conn, actor: ActorContext) -> list[DestinationBinding]:
    _require_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_BINDING_COLUMNS} from app.destination_bindings"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s order by created_at, id",
            {"ws": actor.workspace_id},
        )
    return [_binding(r) for r in rows]


async def _lock_binding(conn: Conn, actor: ActorContext, binding_id: UUID, expected_version: int) -> Any:
    row = await fetch_one(
        conn,
        f"select {_BINDING_COLUMNS} from app.destination_bindings"  # noqa: S608 - fixed list
        " where workspace_id = %(ws)s and id = %(id)s for update",
        {"ws": actor.workspace_id, "id": binding_id},
    )
    if row is None:
        raise NotFound("Destination binding not found")
    if row["row_version"] != expected_version:
        raise VersionConflict(expected_version=expected_version, current_version=row["row_version"])
    return row


async def approve_binding(
    conn: Conn, actor: ActorContext, binding_id: UUID, *, approval_reference: str, expected_version: int
) -> DestinationBinding:
    """Record the owner's approval of a destination (approver = authenticated owner)."""
    _require_owner(actor)
    reference = _approval_text(approval_reference)
    async with mapped_errors():
        current = await _lock_binding(conn, actor, binding_id, expected_version)
        if current["approved_at"] is not None:
            raise VersionConflict("The destination is already approved; create a new binding to change it")
        row = await fetch_one(
            conn,
            "update app.destination_bindings set approval_reference = %(reference)s, approved_by = %(principal)s,"
            " approved_at = clock_timestamp(), row_version = row_version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_BINDING_COLUMNS}",
            {"ws": actor.workspace_id, "id": binding_id, "reference": reference, "principal": actor.principal_id},
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.binding_approve", "destination_binding", binding_id,
            expected_version, row["row_version"],
        )
    return _binding(row)


async def set_binding_enabled(
    conn: Conn, actor: ActorContext, binding_id: UUID, enabled: bool, *, expected_version: int
) -> DestinationBinding:
    """Enable (needs a recorded approval; keeps one route per category) or disable a binding."""
    _require_owner(actor)
    async with mapped_errors():
        current = await _lock_binding(conn, actor, binding_id, expected_version)
        if enabled and current["approved_at"] is None:
            raise ValidationFailed("record the owner's approval before enabling a destination")
        if enabled:
            await _check_exclusive(conn, actor, binding_overrides={binding_id: True})
        row = await fetch_one(
            conn,
            "update app.destination_bindings set enabled = %(enabled)s, row_version = row_version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_BINDING_COLUMNS}",
            {"ws": actor.workspace_id, "id": binding_id, "enabled": enabled},
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.route_change", "destination_binding", binding_id,
            expected_version, row["row_version"], metadata={"enabled": enabled},
        )
    return _binding(row)


async def mark_binding_verified(
    conn: Conn, actor: ActorContext, binding_id: UUID, *, expected_version: int
) -> DestinationBinding:
    """Record that the destination passed its verification (e.g. channel/app identity check)."""
    _require_owner(actor)
    async with mapped_errors():
        await _lock_binding(conn, actor, binding_id, expected_version)
        row = await fetch_one(
            conn,
            "update app.destination_bindings set verified_at = clock_timestamp(), row_version = row_version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_BINDING_COLUMNS}",
            {"ws": actor.workspace_id, "id": binding_id},
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.binding_verify", "destination_binding", binding_id,
            expected_version, row["row_version"],
        )
    return _binding(row)


# --------------------------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------------------------


async def get_preferences(conn: Conn, actor: ActorContext, binding_id: UUID) -> NotificationPreference | None:
    _require_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_PREFERENCE_COLUMNS} from app.notification_preferences"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s and destination_binding_id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id},
        )
    return None if row is None else _preference(row)


async def list_preferences(conn: Conn, actor: ActorContext) -> list[NotificationPreference]:
    _require_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_PREFERENCE_COLUMNS} from app.notification_preferences"  # noqa: S608 - fixed list
            " where workspace_id = %(ws)s order by created_at, id",
            {"ws": actor.workspace_id},
        )
    return [_preference(r) for r in rows]


async def upsert_preferences(  # noqa: PLR0913 - explicit preference fields
    conn: Conn,
    actor: ActorContext,
    binding_id: UUID,
    *,
    event_categories: Sequence[EventCategory],
    quiet_hours: QuietHours | None = None,
    urgency_policy: Mapping[str, Any] | None = None,
    expected_version: int | None = None,
) -> NotificationPreference:
    """Create (``expected_version=None``) or change the preferences of one binding.

    The provider must be allowed for every category; an ENABLED preference keeps the
    one-route-per-category rule after the change.
    """
    _require_owner(actor)
    categories = _categories(event_categories)
    async with mapped_errors():
        binding = await fetch_one(
            conn,
            "select provider from app.destination_bindings where workspace_id = %(ws)s and id = %(id)s for share",
            {"ws": actor.workspace_id, "id": binding_id},
        )
        if binding is None:
            raise NotFound("Destination binding not found")
        disallowed = [c for c in categories if binding["provider"] not in CATEGORY_PROVIDERS[c]]
        if disallowed:
            raise ValidationFailed(
                "this provider is not an allowed route for the category", details={"categories": disallowed}
            )
        params = {
            "ws": actor.workspace_id,
            "binding": binding_id,
            "categories": categories,
            "quiet": None if quiet_hours is None else Jsonb(quiet_hours.model_dump(mode="json")),
            "urgency": None if urgency_policy is None else Jsonb(dict(urgency_policy)),
        }
        if expected_version is None:
            row = await fetch_one(
                conn,
                "insert into app.notification_preferences (workspace_id, destination_binding_id,"
                " event_categories, quiet_hours, urgency_policy) values (%(ws)s, %(binding)s,"
                f" %(categories)s::text[], %(quiet)s, %(urgency)s) returning {_PREFERENCE_COLUMNS}",
                params,
            )
            prior: int | None = None
        else:
            locked = await _lock_preferences(conn, actor)
            current = next((r for r in locked if r["destination_binding_id"] == binding_id), None)
            if current is None:
                raise NotFound("Notification preferences not found")
            if current["row_version"] != expected_version:
                raise VersionConflict(expected_version=expected_version, current_version=current["row_version"])
            if current["enabled"]:
                await _check_exclusive(
                    conn, actor, preference_overrides={current["id"]: (True, tuple(categories))}, locked=locked
                )
            row = await fetch_one(
                conn,
                "update app.notification_preferences set event_categories = %(categories)s::text[],"
                " quiet_hours = %(quiet)s, urgency_policy = %(urgency)s, row_version = row_version + 1"
                " where workspace_id = %(ws)s and destination_binding_id = %(binding)s"
                f" returning {_PREFERENCE_COLUMNS}",
                params,
            )
            prior = expected_version
        assert row is not None
        await audit.record(
            conn, actor, "notification.preferences_change", "notification_preference", row["id"],
            prior, row["row_version"], metadata={"categories": categories},
        )
    return _preference(row)


async def _lock_preference(
    conn: Conn, actor: ActorContext, preference_id: UUID, expected_version: int
) -> tuple[Any, list[Any]]:
    locked = await _lock_preferences(conn, actor)
    current = next((r for r in locked if r["id"] == preference_id), None)
    if current is None:
        raise NotFound("Notification preferences not found")
    if current["row_version"] != expected_version:
        raise VersionConflict(expected_version=expected_version, current_version=current["row_version"])
    return current, locked


async def approve_preferences(
    conn: Conn, actor: ActorContext, preference_id: UUID, *, approval_reference: str, expected_version: int
) -> NotificationPreference:
    _require_owner(actor)
    reference = _approval_text(approval_reference)
    async with mapped_errors():
        await _lock_preference(conn, actor, preference_id, expected_version)
        row = await fetch_one(
            conn,
            "update app.notification_preferences set approval_reference = %(reference)s,"
            " approved_by = %(principal)s, approved_at = clock_timestamp(), row_version = row_version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_PREFERENCE_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "id": preference_id,
                "reference": reference,
                "principal": actor.principal_id,
            },
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.preferences_approve", "notification_preference", preference_id,
            expected_version, row["row_version"],
        )
    return _preference(row)


async def set_preferences_enabled(
    conn: Conn, actor: ActorContext, preference_id: UUID, enabled: bool, *, expected_version: int
) -> NotificationPreference:
    """Enable (approval required; one active route per category) or disable preferences."""
    _require_owner(actor)
    async with mapped_errors():
        current, locked = await _lock_preference(conn, actor, preference_id, expected_version)
        if enabled and current["approved_at"] is None:
            raise ValidationFailed("record the owner's approval before enabling these preferences")
        if enabled:
            await _check_exclusive(
                conn,
                actor,
                preference_overrides={preference_id: (True, tuple(current["event_categories"] or ()))},
                locked=locked,
            )
        row = await fetch_one(
            conn,
            "update app.notification_preferences set enabled = %(enabled)s, row_version = row_version + 1"
            f" where workspace_id = %(ws)s and id = %(id)s returning {_PREFERENCE_COLUMNS}",
            {"ws": actor.workspace_id, "id": preference_id, "enabled": enabled},
        )
        assert row is not None
        await audit.record(
            conn, actor, "notification.route_change", "notification_preference", preference_id,
            expected_version, row["row_version"], metadata={"enabled": enabled},
        )
    return _preference(row)


# --------------------------------------------------------------------------------------------
# Route selection and exclusivity
# --------------------------------------------------------------------------------------------


async def _lock_preferences(conn: Conn, actor: ActorContext) -> list[Any]:
    """Serialize every route change of the workspace (rows locked in id order)."""
    return await fetch_all(
        conn,
        f"select {_PREFERENCE_COLUMNS} from app.notification_preferences"  # noqa: S608 - fixed list
        " where workspace_id = %(ws)s order by id for update",
        {"ws": actor.workspace_id},
    )


async def _check_exclusive(
    conn: Conn,
    actor: ActorContext,
    *,
    binding_overrides: Mapping[UUID, bool] | None = None,
    preference_overrides: Mapping[UUID, tuple[bool, tuple[str, ...]]] | None = None,
    locked: list[Any] | None = None,
) -> None:
    preferences = locked if locked is not None else await _lock_preferences(conn, actor)
    bindings = await fetch_all(
        conn,
        "select id, provider, enabled from app.destination_bindings where workspace_id = %(ws)s",
        {"ws": actor.workspace_id},
    )
    enabled_binding = {b["id"]: bool(b["enabled"]) for b in bindings}
    provider = {b["id"]: b["provider"] for b in bindings}
    enabled_binding.update(binding_overrides or {})
    active: dict[str, list[UUID]] = {}
    for pref in preferences:
        is_enabled, categories = (preference_overrides or {}).get(
            pref["id"], (bool(pref["enabled"]), tuple(pref["event_categories"] or ()))
        )
        if not is_enabled or not enabled_binding.get(pref["destination_binding_id"], False):
            continue
        for category in categories:
            if provider.get(pref["destination_binding_id"]) not in CATEGORY_PROVIDERS.get(category, ()):
                raise ValidationFailed("this provider is not an allowed route for the category")
            active.setdefault(category, []).append(pref["id"])
    conflicts = sorted(c for c, ids in active.items() if len(ids) > 1)
    if conflicts:
        raise ValidationFailed(
            "an event category can have only one active activation route",
            details={"categories": conflicts},
        )


async def active_routes(conn: Conn, actor: ActorContext) -> list[ActivationRouteSelection]:
    """Every active route (enabled + approved binding and preferences)."""
    _require_reader(actor)
    return await routes_for_workspace(conn, actor.workspace_id)


async def routes_for_workspace(conn: Conn, workspace_id: UUID) -> list[ActivationRouteSelection]:
    """Internal read used inside other repositories' transactions (caller already authorized)."""
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select p.id as preference_id, p.event_categories, p.quiet_hours, b.id as binding_id, b.provider"
            " from app.notification_preferences p join app.destination_bindings b"
            "   on b.workspace_id = p.workspace_id and b.id = p.destination_binding_id"
            " where p.workspace_id = %(ws)s and p.enabled and b.enabled"
            "   and p.approved_at is not null and b.approved_at is not null"
            " order by p.id",
            {"ws": workspace_id},
        )
    routes: list[ActivationRouteSelection] = []
    for row in rows:
        quiet = None if row["quiet_hours"] is None else QuietHours.model_validate(row["quiet_hours"])
        for category in row["event_categories"] or ():
            if category not in CATEGORY_PROVIDERS:
                continue
            routes.append(
                ActivationRouteSelection(
                    category=category,
                    provider=row["provider"],
                    binding_id=row["binding_id"],
                    preference_id=row["preference_id"],
                    preferred=PREFERRED_PROVIDER[category] == row["provider"],
                    quiet_hours=quiet,
                )
            )
    return routes


async def selected_route(conn: Conn, actor: ActorContext, category: EventCategory) -> ActivationRouteSelection | None:
    """The single active route for ``category`` (``None``: no external activation)."""
    _require_reader(actor)
    return await route_for(conn, actor.workspace_id, category)


async def route_for(conn: Conn, workspace_id: UUID, category: EventCategory) -> ActivationRouteSelection | None:
    if category not in CATEGORY_PROVIDERS:
        raise ValidationFailed("unknown event category")
    matches = [r for r in await routes_for_workspace(conn, workspace_id) if r.category == category]
    if len(matches) > 1:  # pragma: no cover - prevented by _check_exclusive under row locks
        raise ValidationFailed("more than one active route for this category; disable one")
    return matches[0] if matches else None


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


__all__ = [
    "CATEGORY_PROVIDERS",
    "PREFERRED_PROVIDER",
    "ActivationRouteSelection",
    "DestinationBinding",
    "EventCategory",
    "NotificationPreference",
    "Provider",
    "active_routes",
    "approve_binding",
    "approve_preferences",
    "create_binding",
    "get_binding",
    "get_preferences",
    "list_bindings",
    "list_preferences",
    "mark_binding_verified",
    "route_for",
    "routes_for_workspace",
    "selected_route",
    "set_binding_enabled",
    "set_preferences_enabled",
    "upsert_preferences",
]
