"""Snapshot stores and ``ops.source_snapshots`` (spec 4, 11, 24).

The Supabase store is exercised ONLY through `httpx.MockTransport`; no network call is made.
Store tests need no database; the metadata tests are marked ``db``.
"""

from __future__ import annotations

import json
import stat
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.db.helpers import Seed
from tests.integration.persistence_core.support import member
from tests.integration.repos_sources_listings.support import T0, Env

from suv_deals.domain.enums import Role
from suv_deals.errors import DependencyUnavailable, Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import storage
from suv_deals.persistence.database import Database
from suv_deals.persistence.storage import (
    DisabledSnapshotStore,
    LocalSnapshotStore,
    RetentionPolicy,
    SupabaseSnapshotStore,
    content_sha256,
    snapshot_object_key,
    validate_object_key,
)
from suv_deals.persistence.transactions import unit_of_work

WS = uuid.UUID("11111111-1111-4111-8111-111111111111")
BODY = b"<html><body>synthetic listing page</body></html>"
SECRET = "sb_secret_synthetic_test_value"


# --------------------------------------------------------------------------------------------
# Keys and local / disabled stores
# --------------------------------------------------------------------------------------------


def test_object_keys_are_content_addressed_and_safe() -> None:
    digest = content_sha256(BODY)
    key = snapshot_object_key(WS, digest)
    assert key == f"{WS}/{digest[:2]}/{digest}"
    for bad in (
        "../etc/passwd",
        f"/{key}",
        f"https://evil.example/{key}",
        f"{WS}/zz/{digest}",
        f"{WS}/{digest[:2]}/{digest}/..",
        f"{WS}/ab/{digest}",
    ):
        with pytest.raises(ValidationFailed):
            validate_object_key(bad)


async def test_local_store_round_trip(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path / "snapshots", max_bytes=1000)
    stored = await store.put(workspace_id=WS, content=BODY, mime_type="text/html; charset=utf-8")
    assert stored.backend == "local" and stored.mime_type == "text/html" and stored.bytes == len(BODY)
    assert stored.object_key is not None and await store.get(stored.object_key) == BODY
    path = tmp_path / "snapshots" / stored.object_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = await store.put(workspace_id=WS, content=BODY, mime_type=None)
    assert again.object_key == stored.object_key  # identical content stored once
    path.write_bytes(b"tampered")
    with pytest.raises(ValidationFailed):
        await store.get(stored.object_key)
    await store.delete(stored.object_key)
    await store.delete(stored.object_key)  # idempotent
    with pytest.raises(NotFound):
        await store.get(stored.object_key)
    with pytest.raises(ValidationFailed):
        await store.put(workspace_id=WS, content=b"x" * 1001, mime_type=None)
    with pytest.raises(ValidationFailed):
        await store.get("../../outside")


async def test_disabled_store_keeps_only_the_hash() -> None:
    stored = await DisabledSnapshotStore().put(workspace_id=WS, content=BODY, mime_type="text/html")
    assert stored.object_key is None and stored.content_hash == content_sha256(BODY)
    with pytest.raises(NotFound):
        await DisabledSnapshotStore().get(snapshot_object_key(WS, stored.content_hash))


# --------------------------------------------------------------------------------------------
# Supabase store (mocked HTTP only)
# --------------------------------------------------------------------------------------------


def _supabase(handler: Any, **kwargs: Any) -> SupabaseSnapshotStore:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseSnapshotStore(
        "https://synthetic-project.supabase.example",
        SecretStr(SECRET),
        "source-evidence-private",
        client=client,
        **kwargs,
    )


async def test_supabase_store_uses_private_bucket_and_secret_key() -> None:
    seen: list[httpx.Request] = []
    objects: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST":
            key = path.removeprefix("/storage/v1/object/source-evidence-private/")
            if key in objects:
                return httpx.Response(
                    400, json={"statusCode": "409", "error": "Duplicate", "message": "exists"}
                )
            objects[key] = request.content
            return httpx.Response(200, json={"Key": key})
        if request.method == "GET":
            key = path.removeprefix("/storage/v1/object/source-evidence-private/")
            if key not in objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, content=objects[key])
        if request.method == "DELETE":
            for key in json.loads(request.content)["prefixes"]:
                objects.pop(key, None)
            return httpx.Response(200, json=[])
        return httpx.Response(405)

    store = _supabase(handler)
    stored = await store.put(workspace_id=WS, content=BODY, mime_type="text/html")
    duplicate = await store.put(workspace_id=WS, content=BODY, mime_type="text/html")
    assert stored == duplicate and stored.backend == "supabase" and stored.object_key is not None
    assert await store.get(stored.object_key) == BODY
    await store.delete(stored.object_key)
    with pytest.raises(NotFound):
        await store.get(stored.object_key)
    upload = seen[0]
    assert upload.url.host == "synthetic-project.supabase.example" and upload.url.scheme == "https"
    assert upload.url.path == f"/storage/v1/object/source-evidence-private/{stored.object_key}"
    # An sb_secret_ key is not a JWT: it goes in the apikey header only, never as a Bearer token.
    assert upload.headers["apikey"] == SECRET and "authorization" not in upload.headers
    assert upload.headers["x-upsert"] == "false"
    assert SECRET not in repr(store)


async def test_supabase_store_sends_a_legacy_jwt_key_as_bearer_too() -> None:
    legacy = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c3ludGhldGljLXNpZ25hdHVyZQ"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Key": "x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseSnapshotStore(
        "https://synthetic-project.supabase.example", legacy, "bucket", client=client
    )
    await store.put(workspace_id=WS, content=BODY, mime_type=None)
    assert seen[0].headers["apikey"] == legacy and seen[0].headers["authorization"] == f"Bearer {legacy}"


async def test_supabase_store_only_reads_a_wrapped_404_as_not_found() -> None:
    key = snapshot_object_key(WS, content_sha256(BODY))

    def wrapped_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "message": "Object not found"}
        )

    def bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"statusCode": "403", "error": "Unauthorized", "message": "bad key"})

    with pytest.raises(NotFound):
        await _supabase(wrapped_404).get(key)
    with pytest.raises(DependencyUnavailable):  # an auth/config failure is never "content missing"
        await _supabase(bad_request).get(key)


async def test_supabase_store_errors_are_safe() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"internal error for {request.url}")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    for handler in (refuse, broken):
        with pytest.raises(DependencyUnavailable) as caught:
            await _supabase(handler).put(workspace_id=WS, content=BODY, mime_type=None)
        assert SECRET not in str(caught.value) and "supabase.example" not in caught.value.message

    def tampered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not the stored bytes")

    with pytest.raises(ValidationFailed):
        await _supabase(tampered).get(snapshot_object_key(WS, content_sha256(BODY)))


def test_supabase_store_configuration_is_validated() -> None:
    for url in (
        "http://project.supabase.example",
        "https://user:pw@project.supabase.example",
        "ftp://x.example",
    ):
        with pytest.raises(ValueError):
            SupabaseSnapshotStore(url, SECRET, "bucket")
    with pytest.raises(ValueError):
        SupabaseSnapshotStore("https://project.supabase.example", "", "bucket")
    with pytest.raises(ValueError):
        SupabaseSnapshotStore("https://project.supabase.example", SECRET, "../bucket")
    assert SupabaseSnapshotStore("http://127.0.0.1:54321", SECRET, "bucket").backend == "supabase"
    with pytest.raises(ValidationFailed):
        storage.snapshot_store_from_settings(mode="supabase", local_dir=Path("var/snapshots"))
    assert isinstance(
        storage.snapshot_store_from_settings(mode="disabled", local_dir=Path("x")), DisabledSnapshotStore
    )


# --------------------------------------------------------------------------------------------
# ops.source_snapshots metadata
# --------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_snapshot_rows_record_retention_and_redaction(
    db: Database, env: Env, seed: Seed, tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    stored = await store.put(workspace_id=env.workspace_id, content=BODY, mime_type="text/html")
    url = "https://dealer-a.synthetic.example/vehicles/1?session=secret"
    async with unit_of_work(db, env.system) as conn:
        retained = await storage.record_snapshot(
            conn,
            env.system,
            source_id=env.source_id,
            url=url,
            stored=stored,
            fetched_at=T0,
            retention=RetentionPolicy(days=14, redaction_required=True),
        )
        hashed = await storage.record_snapshot(
            conn,
            env.system,
            source_id=env.source_id,
            url=url,
            stored=await DisabledSnapshotStore().put(
                workspace_id=env.workspace_id, content=BODY, mime_type=None
            ),
            fetched_at=T0,
        )
        redacted = await storage.mark_snapshot_redacted(conn, env.system, retained.id)
    assert retained.retention_policy == "retain_until" and retained.retain_until == T0 + timedelta(days=14)
    assert retained.object_key == stored.object_key and retained.redaction_status == "pending"
    assert retained.url_hash == content_sha256(url.encode()) and "secret" not in str(retained.model_dump())
    assert (
        hashed.retention_policy == "hash_only"
        and hashed.object_key is None
        and hashed.storage_backend == "disabled"
    )
    assert redacted.redaction_status == "redacted" and redacted.redacted_at is not None
    with pytest.raises(Exception) as frozen:
        seed.conn.execute(
            "update ops.source_snapshots set content_hash = %s where id = %s", ("0" * 64, retained.id)
        )
    assert getattr(frozen.value, "sqlstate", None) == "SV004"


@pytest.mark.db
async def test_purge_never_deletes_a_shared_object_still_retained(
    db: Database, env: Env, seed: Seed, tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    stored = await store.put(workspace_id=env.workspace_id, content=BODY, mime_type="text/html")
    async with unit_of_work(db, env.system) as conn:
        old = await storage.record_snapshot(
            conn,
            env.system,
            source_id=env.source_id,
            url="https://dealer-a.synthetic.example/a",
            stored=stored,
            fetched_at=T0 - timedelta(days=40),
            retention=RetentionPolicy(days=30),
        )
        fresh = await storage.record_snapshot(
            conn,
            env.system,
            source_id=env.source_id,
            url="https://dealer-a.synthetic.example/b",
            stored=stored,
            fetched_at=T0 + timedelta(days=3650),
            retention=RetentionPolicy(days=30),
        )
        assert await storage.snapshots_due_for_purge(conn, env.system) == []  # still needed by `fresh`
    seed.conn.execute("update ops.source_snapshots set purged_at = now() where id = %s", (fresh.id,))
    async with unit_of_work(db, env.system) as conn:
        due = await storage.snapshots_due_for_purge(conn, env.system)
        assert [s.id for s in due] == [old.id]
        assert due[0].object_key is not None
        await store.delete(due[0].object_key)
        purged = await storage.mark_snapshot_purged(conn, env.system, old.id)
    assert purged.purged_at is not None


@pytest.mark.db
async def test_snapshot_access_rules(db: Database, env: Env, env_b: Env, tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    foreign = await store.put(workspace_id=env_b.workspace_id, content=BODY, mime_type=None)
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(ValidationFailed):
            await storage.record_snapshot(
                conn,
                env.system,
                source_id=env.source_id,
                url="https://x.example/",
                stored=foreign,
                fetched_at=T0,
            )
        own = await storage.record_snapshot(
            conn,
            env.system,
            source_id=env.source_id,
            url="https://x.example/",
            stored=await store.put(workspace_id=env.workspace_id, content=BODY, mime_type=None),
            fetched_at=T0,
        )
    async with unit_of_work(db, env_b.owner) as conn:
        with pytest.raises(NotFound):
            await storage.get_snapshot(conn, env_b.owner, own.id)
    async with unit_of_work(db, env.owner) as conn:
        assert (await storage.get_snapshot(conn, env.owner, own.id)).id == own.id
    reviewer = member(env.workspace_id, Role.REVIEWER)
    async with unit_of_work(db, reviewer) as conn:
        with pytest.raises(Forbidden):
            await storage.snapshots_due_for_purge(conn, reviewer)
