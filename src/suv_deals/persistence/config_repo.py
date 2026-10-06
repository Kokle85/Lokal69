"""Immutable business-configuration revisions and their search profiles (spec sections 3, 26).

- Every business change is a new ``app.config_revisions`` row (append-only): monotonically
  increasing ``revision`` per workspace, the validated config, its SHA-256, the ``before``
  config, the verified author (principal, kind, label), the reason and the effective time.
- Optimistic concurrency: the caller passes the configuration it edited (``before``); it must be
  the current revision's config (hash compared), otherwise `VersionConflict`. Two concurrent
  writers both computing ``max + 1`` collide on ``config_revisions_revision_uk`` and the loser gets
  `VersionConflict` (nothing of its transaction commits).
- Recording an identical configuration creates nothing (``created=False``).
- ``app.search_profiles`` is synchronised in the same transaction and every profile row is bound
  to the new revision. A profile missing from the configuration is disabled (rows are never
  deleted). Enabling ``manual_4000`` or ``below_target_watch`` therefore always is a new auditable
  revision, and its queue label is distinct (`domain.profiles.validate_baseline` and the
  ``search_profiles_queue_label_uk`` constraint).
- Writes need ``config:admin`` (owner). Reads need ``deals:read``.

Lock order: ``app.search_profiles`` rows (``FOR UPDATE`` in profile-key order) after the revision
insert; the audit event is written last.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import ProfileKey, Scope
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.profiles import BusinessConfig, SearchProfile
from suv_deals.errors import NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

_PROFILE_ORDER: Final = (ProfileKey.PRIMARY, ProfileKey.MANUAL_4000, ProfileKey.BELOW_TARGET_WATCH)
_REVISION_COLUMNS: Final = sql.SQL(
    "id, workspace_id, revision, config, config_hash, before, author_principal_id, author_kind,"
    " author_label, reason, created_at, effective_at"
)
_PROFILE_COLUMNS: Final = sql.SQL(
    "id, workspace_id, profile_key, label, queue_label, enabled, min_price_eur, max_price_eur,"
    " max_price_inclusive, max_mileage_km_exclusive, criteria, config_revision_id, row_version,"
    " created_at, updated_at"
)


class ConfigRevisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    revision: int
    config: dict[str, Any]
    config_hash: str
    before: dict[str, Any] | None = None
    author_principal_id: UUID
    author_kind: Literal["user", "mcp_client", "system"]
    author_label: str | None = None
    reason: str
    created_at: datetime
    effective_at: datetime

    @field_validator("created_at", "effective_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def business_config(self) -> BusinessConfig:
        try:
            return BusinessConfig.model_validate(self.config)
        except ValidationError as exc:
            raise ValidationFailed("the stored configuration is not a valid business config") from exc


class ProfileRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    profile_key: ProfileKey
    label: str
    queue_label: str
    enabled: bool
    min_price_eur: Decimal | None = None
    max_price_eur: Decimal
    max_price_inclusive: bool
    max_mileage_km_exclusive: Decimal
    criteria: dict[str, Any]
    config_revision_id: UUID
    row_version: int
    created_at: datetime
    updated_at: datetime


class ConfigRevisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: ConfigRevisionRecord
    created: bool
    profiles: tuple[ProfileRecord, ...]
    enabled_changes: dict[str, bool]


def config_hash(config: BusinessConfig) -> str:
    """SHA-256 of the canonical JSON of a validated configuration."""
    return sha256_json(config.model_dump(mode="json"))


def _reason(reason: str) -> str:
    cleaned = audit.safe_reason(reason) or ""
    if not 3 <= len(cleaned.strip()) <= 2000:
        raise ValidationFailed("reason must be 3-2000 characters")
    return cleaned


def _criteria(profile: SearchProfile) -> dict[str, Any]:
    return {
        "source_countries": list(profile.source_countries),
        "body_types": [b.value for b in profile.body_types],
        "require_taxonomy_match": profile.require_taxonomy_match,
        "fx_max_age_days": profile.fx_max_age_days,
        "fx_boundary_margin_pct": str(profile.fx_boundary_margin_pct),
        "notes": profile.notes,
    }


async def _current(conn: Conn, workspace_id: UUID) -> ConfigRevisionRecord | None:
    row = await fetch_one(
        conn,
        sql.SQL(
            "select {columns} from app.config_revisions where workspace_id = %(workspace_id)s"
            " order by revision desc limit 1"
        ).format(columns=_REVISION_COLUMNS),
        {"workspace_id": workspace_id},
    )
    return None if row is None else ConfigRevisionRecord.model_validate(row)


async def record_config_revision(
    conn: Conn,
    actor: ActorContext,
    config: BusinessConfig,
    reason: str,
    before: BusinessConfig | None,
    *,
    effective_at: datetime | None = None,
) -> ConfigRevisionResult:
    """Append a configuration revision (owner, ``config:admin``) and resync search profiles."""
    actor.require(Scope.CONFIG_ADMIN)
    if not isinstance(config, BusinessConfig):
        raise ValidationFailed("config must be a validated BusinessConfig")
    clean_reason = _reason(reason)
    new_hash = config_hash(config)
    async with mapped_errors():
        current = await _current(conn, actor.workspace_id)
        if current is None:
            if before is not None:
                raise VersionConflict("There is no current configuration to replace")
        else:
            if before is None or config_hash(before) != current.config_hash:
                raise VersionConflict(
                    "The configuration changed since it was loaded; reload and retry",
                    current_revision=current.revision,
                )
            if new_hash == current.config_hash:
                profiles = await _profiles(conn, actor.workspace_id)
                return ConfigRevisionResult(
                    revision=current, created=False, profiles=tuple(profiles), enabled_changes={}
                )
        row = await fetch_one(
            conn,
            sql.SQL(
                "insert into app.config_revisions (workspace_id, revision, config, config_hash, before,"
                " author_principal_id, author_kind, author_label, reason, effective_at)"
                " values (%(workspace_id)s,"
                " (select coalesce(max(revision), 0) + 1 from app.config_revisions"
                "   where workspace_id = %(workspace_id)s),"
                " %(config)s, %(hash)s, %(before)s, %(principal)s, %(kind)s, %(label)s, %(reason)s,"
                " coalesce(%(effective_at)s::timestamptz, now()))"
                " returning {columns}"
            ).format(columns=_REVISION_COLUMNS),
            {
                "workspace_id": actor.workspace_id,
                "config": Jsonb(config.model_dump(mode="json")),
                "hash": new_hash,
                "before": None if before is None else Jsonb(before.model_dump(mode="json")),
                "principal": actor.principal_id,
                "kind": actor.principal_kind,
                "label": None if actor.display_name is None else actor.display_name[:200],
                "reason": clean_reason,
                "effective_at": None if effective_at is None else ensure_utc(effective_at),
            },
        )
        assert row is not None
        record = ConfigRevisionRecord.model_validate(row)
        enabled_changes = await _sync_profiles(conn, actor, config, record.id)
        profiles = await _profiles(conn, actor.workspace_id)
        await audit.record(
            conn,
            actor,
            "config.revision",
            "config_revision",
            record.id,
            prior_version=None if current is None else current.revision,
            new_version=record.revision,
            reason=clean_reason,
            metadata={
                "config_hash": new_hash,
                "prior_config_hash": None if current is None else current.config_hash,
                "profile_enabled_changes": enabled_changes,
            },
        )
    return ConfigRevisionResult(
        revision=record, created=True, profiles=tuple(profiles), enabled_changes=enabled_changes
    )


async def _sync_profiles(
    conn: Conn, actor: ActorContext, config: BusinessConfig, revision_id: UUID
) -> dict[str, bool]:
    """Upsert every profile and bind it to ``revision_id``; returns enabled-state changes."""
    changes: dict[str, bool] = {}
    rows = await fetch_all(
        conn,
        "select profile_key, enabled from app.search_profiles"
        " where workspace_id = %(workspace_id)s order by profile_key for update",
        {"workspace_id": actor.workspace_id},
    )
    existing = {r["profile_key"]: bool(r["enabled"]) for r in rows}
    for key in _PROFILE_ORDER:
        profile = config.profiles.get(key)
        if profile is None:
            if key.value in existing:
                await conn.execute(
                    "update app.search_profiles set enabled = false, config_revision_id = %(revision)s,"
                    " row_version = row_version + 1"
                    " where workspace_id = %(workspace_id)s and profile_key = %(key)s",
                    {"workspace_id": actor.workspace_id, "key": key.value, "revision": revision_id},
                )
                if existing[key.value]:
                    changes[key.value] = False
            continue
        params = {
            "workspace_id": actor.workspace_id,
            "key": key.value,
            "label": profile.label,
            "queue_label": profile.queue_label,
            "enabled": profile.enabled,
            "min_price": profile.min_price_eur,
            "max_price": profile.max_price_eur,
            "inclusive": profile.max_price_inclusive,
            "mileage": profile.max_mileage_km_exclusive,
            "criteria": Jsonb(_criteria(profile)),
            "revision": revision_id,
        }
        await conn.execute(
            "insert into app.search_profiles (workspace_id, profile_key, label, queue_label, enabled,"
            " min_price_eur, max_price_eur, max_price_inclusive, max_mileage_km_exclusive, criteria,"
            " config_revision_id)"
            " values (%(workspace_id)s, %(key)s, %(label)s, %(queue_label)s, %(enabled)s, %(min_price)s,"
            " %(max_price)s, %(inclusive)s, %(mileage)s, %(criteria)s, %(revision)s)"
            " on conflict (workspace_id, profile_key) do update set"
            " label = excluded.label, queue_label = excluded.queue_label, enabled = excluded.enabled,"
            " min_price_eur = excluded.min_price_eur, max_price_eur = excluded.max_price_eur,"
            " max_price_inclusive = excluded.max_price_inclusive,"
            " max_mileage_km_exclusive = excluded.max_mileage_km_exclusive,"
            " criteria = excluded.criteria, config_revision_id = excluded.config_revision_id,"
            " row_version = app.search_profiles.row_version + 1",
            params,
        )
        if existing.get(key.value) != profile.enabled and (key.value in existing or profile.enabled):
            changes[key.value] = profile.enabled
    return changes


async def _profiles(conn: Conn, workspace_id: UUID) -> list[ProfileRecord]:
    rows = await fetch_all(
        conn,
        sql.SQL(
            "select {columns} from app.search_profiles where workspace_id = %(workspace_id)s"
            " order by profile_key"
        ).format(columns=_PROFILE_COLUMNS),
        {"workspace_id": workspace_id},
    )
    return [ProfileRecord.model_validate(r) for r in rows]


async def current_config(conn: Conn, actor: ActorContext) -> tuple[ConfigRevisionRecord, BusinessConfig]:
    """The latest revision and its validated configuration (`NotFound` when none exists)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        record = await _current(conn, actor.workspace_id)
    if record is None:
        raise NotFound("No business configuration has been recorded")
    return record, record.business_config()


async def get_config_revision(conn: Conn, actor: ActorContext, revision_id: UUID) -> ConfigRevisionRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "select {columns} from app.config_revisions"
                " where workspace_id = %(workspace_id)s and id = %(id)s"
            ).format(columns=_REVISION_COLUMNS),
            {"workspace_id": actor.workspace_id, "id": revision_id},
        )
    if row is None:
        raise NotFound("Configuration revision not found")
    return ConfigRevisionRecord.model_validate(row)


async def list_config_revisions(
    conn: Conn, actor: ActorContext, *, limit: int = 20
) -> list[ConfigRevisionRecord]:
    """Most recent revisions first (settings history)."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 200:
        raise ValidationFailed("limit must be between 1 and 200")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            sql.SQL(
                "select {columns} from app.config_revisions where workspace_id = %(workspace_id)s"
                " order by revision desc limit %(limit)s"
            ).format(columns=_REVISION_COLUMNS),
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return [ConfigRevisionRecord.model_validate(r) for r in rows]


async def list_profiles(conn: Conn, actor: ActorContext) -> list[ProfileRecord]:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        return await _profiles(conn, actor.workspace_id)


__all__ = [
    "ConfigRevisionRecord",
    "ConfigRevisionResult",
    "ProfileRecord",
    "config_hash",
    "current_config",
    "get_config_revision",
    "list_config_revisions",
    "list_profiles",
    "record_config_revision",
]
