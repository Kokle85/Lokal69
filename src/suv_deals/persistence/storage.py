"""Raw source snapshots: private object storage plus ``ops.source_snapshots`` metadata (spec 4, 11, 24).

Flow (no network or disk I/O inside a database transaction):

1. ``stored = await store.put(workspace_id=..., content=..., mime_type=...)`` -- outside any
   transaction;
2. ``await record_snapshot(conn, actor, source_id=..., url=..., stored=stored, ...)`` -- inside the
   short ingestion transaction.

Stores:

- `DisabledSnapshotStore`: retention not permitted; only the content hash is recorded
  (``retention_policy = 'hash_only'``).
- `LocalSnapshotStore`: content-addressed files under ``var/snapshots`` (owner-only permissions),
  ``<workspace_id>/<sha[:2]>/<sha>``; identical content is written once; reads verify the hash.
- `SupabaseSnapshotStore`: a PRIVATE Supabase Storage bucket through the Storage REST API with the
  server-side secret key (never sent to a browser, logged or stored). Only exercised with mocked
  HTTP in tests; errors never contain the URL, key or body.

Object keys are generated here and validated on every use (UUID / two hex chars / SHA-256), so a
key can never be a URL, an absolute path or a traversal (the ``source_snapshots_object_key_ck``
CHECK enforces the same in the database). The raw URL of a snapshot is never stored; only its
SHA-256. Retention: ``retain_until`` = fetch time + the retention days; due objects are listed by
`snapshots_due_for_purge`, deleted from the store, then marked with `mark_snapshot_purged`.
Content that may hold personal data is recorded with ``redaction_status = 'pending'``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import httpx
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.errors import DependencyUnavailable, Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

Backend = Literal["disabled", "local", "supabase"]
DEFAULT_MAX_SNAPSHOT_BYTES: Final = 8_000_000
_KEY_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{2}/[0-9a-f]{64}$"
)
_BUCKET_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_MIME_RE: Final = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,100}/[A-Za-z0-9!#$&^_.+-]{1,100}$")
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_COLUMNS: Final = (
    "id",
    "workspace_id",
    "source_id",
    "url_hash",
    "content_hash",
    "mime_type",
    "bytes",
    "fetched_at",
    "storage_backend",
    "object_key",
    "retention_policy",
    "retain_until",
    "redaction_status",
    "redacted_at",
    "purged_at",
    "created_at",
)


class StoredObject(BaseModel):
    """Result of `SnapshotStore.put` (``object_key`` is None when nothing was retained)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Backend
    object_key: str | None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    mime_type: str | None = None


class RetentionPolicy(BaseModel):
    """How long a retained snapshot may be kept; ``days=None`` keeps only the hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    days: int | None = Field(default=30, ge=1, le=3650)
    redaction_required: bool = False


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    source_id: UUID
    url_hash: str
    content_hash: str
    mime_type: str | None = None
    bytes: int
    fetched_at: datetime
    storage_backend: Backend
    object_key: str | None = None
    retention_policy: Literal["hash_only", "retain_until"]
    retain_until: datetime | None = None
    redaction_status: Literal["not_required", "pending", "redacted", "failed"]
    redacted_at: datetime | None = None
    purged_at: datetime | None = None
    created_at: datetime

    @field_validator("fetched_at", "retain_until", "redacted_at", "purged_at", "created_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def snapshot_object_key(workspace_id: UUID, content_hash: str) -> str:
    """Content-addressed private key ``<workspace>/<sha[:2]>/<sha>`` (validated)."""
    return validate_object_key(f"{workspace_id}/{content_hash[:2]}/{content_hash}")


def validate_object_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValidationFailed("invalid snapshot object key")
    if key.split("/")[1] != key.split("/")[2][:2]:
        raise ValidationFailed("invalid snapshot object key")
    return key


def _mime(mime_type: str | None) -> str | None:
    if mime_type is None:
        return None
    base = mime_type.split(";", 1)[0].strip().lower()
    return base if _MIME_RE.fullmatch(base) else None


def _check_size(content: bytes, limit: int) -> None:
    if not isinstance(content, bytes | bytearray):
        raise ValidationFailed("snapshot content must be bytes")
    if len(content) > limit:
        raise ValidationFailed("snapshot exceeds the configured size limit")


@runtime_checkable
class SnapshotStore(Protocol):
    backend: Backend

    async def put(self, *, workspace_id: UUID, content: bytes, mime_type: str | None) -> StoredObject: ...

    async def get(self, object_key: str) -> bytes: ...

    async def delete(self, object_key: str) -> None: ...


class DisabledSnapshotStore:
    """Retention not permitted: nothing is written, only the hash is recorded."""

    backend: Backend = "disabled"

    def __init__(self, max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> None:
        self._max_bytes = max_bytes

    async def put(self, *, workspace_id: UUID, content: bytes, mime_type: str | None) -> StoredObject:
        _check_size(content, self._max_bytes)
        return StoredObject(
            backend="disabled",
            object_key=None,
            content_hash=content_sha256(content),
            bytes=len(content),
            mime_type=_mime(mime_type),
        )

    async def get(self, object_key: str) -> bytes:
        validate_object_key(object_key)
        raise NotFound("Snapshot content is not retained")

    async def delete(self, object_key: str) -> None:
        validate_object_key(object_key)


class LocalSnapshotStore:
    """Content-addressed files below ``root`` (default ``var/snapshots``), mode 0600."""

    backend: Backend = "local"

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._root = Path(root)
        self._max_bytes = max_bytes

    def _path(self, key: str) -> Path:
        validate_object_key(key)
        root = self._root.resolve()
        path = (root / key).resolve()
        if root not in path.parents:
            raise ValidationFailed("invalid snapshot object key")
        return path

    async def put(self, *, workspace_id: UUID, content: bytes, mime_type: str | None) -> StoredObject:
        _check_size(content, self._max_bytes)
        digest = content_sha256(bytes(content))
        key = snapshot_object_key(workspace_id, digest)
        path = self._path(key)
        await anyio.to_thread.run_sync(_write_atomic, path, bytes(content))
        return StoredObject(
            backend="local",
            object_key=key,
            content_hash=digest,
            bytes=len(content),
            mime_type=_mime(mime_type),
        )

    async def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        try:
            data = await anyio.to_thread.run_sync(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFound("Snapshot content not found") from exc
        if content_sha256(data) != object_key.rsplit("/", 1)[1]:
            raise ValidationFailed("snapshot content does not match its hash")
        return data

    async def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))


def _write_atomic(path: Path, content: bytes) -> None:
    if path.exists() and content_sha256(path.read_bytes()) == path.name:
        return  # content-addressed: identical bytes already stored
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SupabaseSnapshotStore:
    """Private Supabase Storage bucket via the Storage REST API and the server secret key."""

    backend: Backend = "supabase"

    def __init__(
        self,
        base_url: str,
        secret_key: SecretStr | str,
        bucket: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        timeout_seconds: float = 30.0,
    ) -> None:
        parts = urlsplit(base_url)
        local = parts.hostname in _LOCAL_HOSTS
        if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local):
            raise ValueError("Supabase URL must use https (http only for a local stack)")
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Supabase URL must be a plain origin")
        if not _BUCKET_RE.fullmatch(bucket):
            raise ValueError("invalid storage bucket name")
        secret = secret_key.get_secret_value() if isinstance(secret_key, SecretStr) else secret_key
        if not secret:
            raise ValueError("the server secret key is required")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._bucket = bucket
        self._secret = SecretStr(secret)
        self._client = client
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds

    def __repr__(self) -> str:  # never reveal the key
        return f"SupabaseSnapshotStore(bucket={self._bucket!r})"

    def _headers(self) -> dict[str, str]:
        secret = self._secret.get_secret_value()
        return {"Authorization": f"Bearer {secret}", "apikey": secret}

    def _object_url(self, key: str) -> str:
        return f"{self._base}/storage/v1/object/{self._bucket}/{validate_object_key(key)}"

    async def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False, trust_env=False
        )
        try:
            return await client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise DependencyUnavailable("Snapshot storage is unavailable") from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def put(self, *, workspace_id: UUID, content: bytes, mime_type: str | None) -> StoredObject:
        _check_size(content, self._max_bytes)
        data = bytes(content)
        digest = content_sha256(data)
        key = snapshot_object_key(workspace_id, digest)
        mime = _mime(mime_type)
        response = await self._send(
            "POST",
            self._object_url(key),
            content=data,
            headers={
                **self._headers(),
                "Content-Type": mime or "application/octet-stream",
                "x-upsert": "false",
            },
        )
        # 409 = the content-addressed object already exists (identical bytes).
        if response.status_code not in (200, 201, 409):
            raise DependencyUnavailable(f"Snapshot storage refused the upload (HTTP {response.status_code})")
        return StoredObject(
            backend="supabase", object_key=key, content_hash=digest, bytes=len(data), mime_type=mime
        )

    async def get(self, object_key: str) -> bytes:
        response = await self._send("GET", self._object_url(object_key), headers=self._headers())
        if response.status_code in (400, 404):
            raise NotFound("Snapshot content not found")
        if response.status_code != 200:
            raise DependencyUnavailable(f"Snapshot storage read failed (HTTP {response.status_code})")
        data = response.content
        if len(data) > self._max_bytes or content_sha256(data) != object_key.rsplit("/", 1)[1]:
            raise ValidationFailed("snapshot content does not match its hash")
        return data

    async def delete(self, object_key: str) -> None:
        key = validate_object_key(object_key)
        response = await self._send(
            "DELETE",
            f"{self._base}/storage/v1/object/{self._bucket}",
            json={"prefixes": [key]},
            headers=self._headers(),
        )
        if response.status_code not in (200, 204, 404):
            raise DependencyUnavailable(f"Snapshot storage delete failed (HTTP {response.status_code})")


def snapshot_store_from_settings(
    *,
    mode: Backend,
    local_dir: Path,
    supabase_url: str | None = None,
    secret_key: SecretStr | None = None,
    bucket: str = "source-evidence-private",
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> SnapshotStore:
    """Build the configured store (``Settings.snapshot_storage`` and friends)."""
    if mode == "local":
        return LocalSnapshotStore(local_dir, max_bytes=max_bytes)
    if mode == "supabase":
        if not supabase_url or secret_key is None:
            raise ValidationFailed("supabase snapshot storage needs SUPABASE_URL and SUPABASE_SECRET_KEY")
        return SupabaseSnapshotStore(supabase_url, secret_key, bucket, max_bytes=max_bytes)
    return DisabledSnapshotStore(max_bytes=max_bytes)


# --------------------------------------------------------------------------------------------
# ops.source_snapshots
# --------------------------------------------------------------------------------------------

_SELECT: Final = sql.SQL("select {columns} from ops.source_snapshots").format(
    columns=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)
)
_RETURNING: Final = sql.SQL(" returning {columns}").format(
    columns=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)
)


def _require_system(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only the ingestion pipeline or the owner manages snapshots")


async def record_snapshot(
    conn: Conn,
    actor: ActorContext,
    *,
    source_id: UUID,
    url: str,
    stored: StoredObject,
    fetched_at: datetime,
    retention: RetentionPolicy | None = None,
) -> SnapshotRecord:
    """Insert the snapshot metadata row (URL hash only; retention and redaction state)."""
    _require_system(actor)
    policy = retention or RetentionPolicy()
    fetched = ensure_utc(fetched_at)
    retained = stored.backend != "disabled" and stored.object_key is not None and policy.days is not None
    if stored.object_key is not None:
        key = validate_object_key(stored.object_key)
        if not key.startswith(f"{actor.workspace_id}/"):
            raise ValidationFailed("the snapshot object belongs to another workspace")
    params = {
        "workspace_id": actor.workspace_id,
        "source_id": source_id,
        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "content_hash": stored.content_hash,
        "mime_type": stored.mime_type,
        "bytes": stored.bytes,
        "fetched_at": fetched,
        "backend": stored.backend if retained else "disabled",
        "object_key": stored.object_key if retained else None,
        "policy": "retain_until" if retained else "hash_only",
        "retain_until": fetched + timedelta(days=policy.days) if retained and policy.days else None,
        "redaction": "pending" if retained and policy.redaction_required else "not_required",
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "insert into ops.source_snapshots (workspace_id, source_id, url_hash, content_hash,"
                " mime_type, bytes, fetched_at, storage_backend, object_key, retention_policy, retain_until,"
                " redaction_status) values (%(workspace_id)s, %(source_id)s, %(url_hash)s, %(content_hash)s,"
                " %(mime_type)s, %(bytes)s, %(fetched_at)s, %(backend)s, %(object_key)s, %(policy)s,"
                " %(retain_until)s, %(redaction)s)"
            )
            + _RETURNING,
            params,
        )
    assert row is not None
    return SnapshotRecord.model_validate(row)


async def get_snapshot(conn: Conn, actor: ActorContext, snapshot_id: UUID) -> SnapshotRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + sql.SQL(" where workspace_id = %(workspace_id)s and id = %(id)s"),
            {"workspace_id": actor.workspace_id, "id": snapshot_id},
        )
    if row is None:
        raise NotFound("Snapshot not found")
    return SnapshotRecord.model_validate(row)


async def mark_snapshot_redacted(conn: Conn, actor: ActorContext, snapshot_id: UUID) -> SnapshotRecord:
    _require_system(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "update ops.source_snapshots set redaction_status = 'redacted',"
                " redacted_at = clock_timestamp()"
                " where workspace_id = %(workspace_id)s and id = %(id)s and redaction_status <> 'redacted'"
            )
            + _RETURNING,
            {"workspace_id": actor.workspace_id, "id": snapshot_id},
        )
        if row is None:
            return await get_snapshot(conn, actor, snapshot_id)
        await audit.record(conn, actor, "snapshot.redacted", "source_snapshot", snapshot_id)
    return SnapshotRecord.model_validate(row)


async def mark_snapshot_purged(conn: Conn, actor: ActorContext, snapshot_id: UUID) -> SnapshotRecord:
    """Record that the stored object was deleted (call after `SnapshotStore.delete`)."""
    _require_system(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "update ops.source_snapshots set purged_at = clock_timestamp()"
                " where workspace_id = %(workspace_id)s and id = %(id)s and purged_at is null"
            )
            + _RETURNING,
            {"workspace_id": actor.workspace_id, "id": snapshot_id},
        )
        if row is None:
            return await get_snapshot(conn, actor, snapshot_id)
        await audit.record(conn, actor, "snapshot.purged", "source_snapshot", snapshot_id)
    return SnapshotRecord.model_validate(row)


async def snapshots_due_for_purge(
    conn: Conn, actor: ActorContext, *, limit: int = 100
) -> list[SnapshotRecord]:
    """Retained snapshots past ``retain_until`` that were not purged yet (oldest first)."""
    _require_system(actor)
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SELECT
            + sql.SQL(
                " where workspace_id = %(workspace_id)s and retain_until is not null and purged_at is null"
                " and retain_until <= now() order by retain_until, id limit %(limit)s"
            ),
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return [SnapshotRecord.model_validate(r) for r in rows]


__all__ = [
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "DisabledSnapshotStore",
    "LocalSnapshotStore",
    "RetentionPolicy",
    "SnapshotRecord",
    "SnapshotStore",
    "StoredObject",
    "SupabaseSnapshotStore",
    "content_sha256",
    "get_snapshot",
    "mark_snapshot_purged",
    "mark_snapshot_redacted",
    "record_snapshot",
    "snapshot_object_key",
    "snapshot_store_from_settings",
    "snapshots_due_for_purge",
    "validate_object_key",
]
