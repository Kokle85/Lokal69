"""``persistence.credentials_repo`` (marker ``db``): every ``ops.api_credentials`` operation.

Covers issuing (token shown once, SHA-256 stored only, audited without token material), the
narrow ``mail_worker`` kind (exactly ``mail:ingest``, owner role, machine principal; enforced in
the repository AND by the migration's CHECK), authentication by hash (kind, revocation, expiry,
required scopes, expected workspace/principal, inactive workspace), the mailbox binding of a
worker credential, revocation and the metadata listing (never hashes). SYNTHETIC data only.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_db.support import inquiry_world, mailbox

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import credentials_repo
from suv_deals.persistence.credentials_repo import CredentialRejected, IssuedCredential
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def system(workspace: uuid.UUID) -> ActorContext:
    return ActorContext.system(workspace, request_id="req-synthetic-credentials")


async def run[T](db: Database, actor: ActorContext, work: Callable[[Conn], Awaitable[T]]) -> T:
    async with unit_of_work(db, actor) as conn:
        return await work(conn)


async def authenticate(db: Database, token: str, **kw: Any) -> credentials_repo.VerifiedCredential:
    async with db.transaction() as conn:
        return await credentials_repo.authenticate(conn, token, **kw)


async def issue(db: Database, workspace: uuid.UUID, **kw: Any) -> IssuedCredential:
    actor = system(workspace)
    values: dict[str, Any] = {
        "principal_id": uuid.uuid4(),
        "principal_kind": "mcp_client",
        "role": Role.OWNER,
        "scopes": [Scope.MAIL_INGEST],
        "label": "SYNTHETIC mailbox worker",
        "kind": "mail_worker",
    }
    values.update(kw)
    return await run(db, actor, lambda c: credentials_repo.issue_credential(c, actor, **values))


async def test_mail_worker_credential_is_narrow_hashed_and_bound_to_one_mailbox(
    db: Database, seed: Seed
) -> None:
    world = inquiry_world(seed, "Credentials mail worker")
    issued = await issue(db, world.workspace_id)
    token = issued.token.get_secret_value()
    assert token.startswith("suvmail_") and issued.token_prefix == token[:14]
    assert issued.kind == "mail_worker" and issued.scopes == (Scope.MAIL_INGEST,)
    assert token not in repr(issued)
    stored = seed.scalar(
        "select row_to_json(c)::text from ops.api_credentials c where id = %s", (issued.credential_id,)
    )
    assert token not in stored and hashlib.sha256(token.encode()).hexdigest() in stored
    audit = seed.scalar(
        "select metadata::text from ops.audit_events where workspace_id = %s and action = 'credential.create'"
        " and target_id = %s",
        (world.workspace_id, issued.credential_id),
    )
    assert audit is not None and token not in audit and issued.token_prefix in audit

    verified = await authenticate(
        db, token, kinds={"mail_worker"}, required_scopes={Scope.MAIL_INGEST}, workspace_id=world.workspace_id
    )
    assert verified.credential_kind == "mail_worker" and verified.scopes == frozenset({Scope.MAIL_INGEST})
    assert verified.principal_id == issued.principal_id and verified.role == Role.OWNER
    async with db.transaction() as conn:
        found = await credentials_repo.authenticate(conn, token, kinds={"mail_worker"})
        assert await credentials_repo.active_mailbox_binding_id(conn, found) is None  # not bound yet
    binding = mailbox(seed, world, credential_id=issued.credential_id)
    async with db.transaction() as conn:
        found = await credentials_repo.authenticate(conn, token, kinds={"mail_worker"})
        assert await credentials_repo.active_mailbox_binding_id(conn, found) == binding
    # A mail worker token is never an MCP credential, and an MCP token never a mail worker.
    with pytest.raises(CredentialRejected) as caught:
        await authenticate(db, token, kinds={"static_bearer", "dev_local"})
    assert caught.value.reason == "invalid_token"


async def test_mail_worker_shape_is_enforced_by_repository_and_database(db: Database, seed: Seed) -> None:
    workspace = seed.workspace("Credentials mail worker shape")
    member = seed.user()
    seed.membership(workspace, member, "owner")
    bad: list[tuple[dict[str, Any], str]] = [
        ({"scopes": [Scope.MAIL_INGEST, Scope.DEALS_READ]}, "scopes"),
        ({"scopes": [Scope.DEALS_READ]}, "scopes"),
        ({"role": Role.REVIEWER}, "scopes"),
        ({"principal_kind": "user", "principal_id": member}, "principal_kind"),
        ({"kind": "oauth"}, "kind"),
        ({"scopes": [Scope.MAIL_INGEST], "kind": "static_bearer"}, "scopes"),
        ({"principal_id": member}, "principal_id"),
    ]
    for overrides, field in bad:
        with pytest.raises(ValidationFailed) as caught:
            await issue(db, workspace, **overrides)
        assert caught.value.details == {"fields": [field]}, overrides
    assert seed.scalar("select count(*) from ops.api_credentials where workspace_id = %s", (workspace,)) == 0
    # The migration's CHECK refuses a wider mail_worker row even for a privileged writer.
    for scopes, role, kind in (
        (["mail:ingest", "deals:read"], "owner", "mcp_client"),
        (["deals:read"], "owner", "mcp_client"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation), seed.conn.transaction():
            seed.insert(
                "ops.api_credentials",
                workspace_id=workspace,
                principal_id=uuid.uuid4(),
                principal_kind=kind,
                role=role,
                credential_kind="mail_worker",
                token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                token_prefix="suvmail_abcdef",
                scopes=scopes,
                label="SYNTHETIC wide worker",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )


async def test_authenticate_checks_kind_revocation_expiry_scopes_workspace_and_principal(
    db: Database, seed: Seed
) -> None:
    workspace = seed.workspace("Credentials authenticate")
    other = seed.workspace("Credentials authenticate other")
    machine = uuid.uuid4()
    issued = await issue(
        db,
        workspace,
        principal_id=machine,
        role=Role.VIEWER,
        scopes=[Scope.DEALS_READ],
        kind="static_bearer",
        label="SYNTHETIC viewer client",
    )
    token = issued.token.get_secret_value()
    ok = await authenticate(db, token, kinds={"static_bearer"}, required_scopes={Scope.DEALS_READ})
    assert ok.workspace_id == workspace and ok.credential_kind == "static_bearer"

    async def rejected(presented: str, **kw: Any) -> str:
        with pytest.raises(CredentialRejected) as caught:
            await authenticate(db, presented, **{"kinds": {"static_bearer"}, **kw})
        return caught.value.reason

    assert await rejected("not-a-token") == "invalid_token"
    assert await rejected("suvmcp_" + "0" * 64) == "invalid_token"
    assert await rejected(token, kinds={"dev_local"}) == "invalid_token"
    assert await rejected(token, required_scopes={Scope.REVIEWS_WRITE}) == "insufficient_scope"
    assert await rejected(token, workspace_id=other) == "wrong_workspace"
    assert await rejected(token, principal_id=uuid.uuid4()) == "wrong_principal"
    assert (
        await authenticate(db, token, kinds={"static_bearer"}, principal_id=machine)
    ).principal_id == machine

    # Use is recorded at most once per interval.
    async with db.transaction() as conn:
        verified = await credentials_repo.authenticate(conn, token, kinds={"static_bearer"})
        await credentials_repo.touch_last_used(conn, verified)
    first = seed.scalar("select last_used_at from ops.api_credentials where id = %s", (issued.credential_id,))
    async with db.transaction() as conn:
        verified = await credentials_repo.authenticate(conn, token, kinds={"static_bearer"})
        await credentials_repo.touch_last_used(conn, verified)
    assert first is not None
    assert (
        seed.scalar("select last_used_at from ops.api_credentials where id = %s", (issued.credential_id,))
        == first
    )

    seed.conn.execute("update app.workspaces set active = false where id = %s", (workspace,))
    try:
        assert await rejected(token) == "workspace_inactive"
    finally:
        seed.conn.execute("update app.workspaces set active = true where id = %s", (workspace,))
    actor = system(workspace)
    assert await run(
        db,
        actor,
        lambda c: credentials_repo.revoke_credential(c, actor, issued.credential_id, reason="SYNTHETIC"),
    )
    assert not await run(
        db,
        actor,
        lambda c: credentials_repo.revoke_credential(c, actor, issued.credential_id, reason="again"),
    )
    assert await rejected(token) == "revoked"
    expired = "suvmcp_" + "e" * 64
    seed.insert(
        "ops.api_credentials",
        workspace_id=workspace,
        principal_id=uuid.uuid4(),
        principal_kind="mcp_client",
        role="viewer",
        credential_kind="static_bearer",
        token_hash=hashlib.sha256(expired.encode()).hexdigest(),
        scopes=["deals:read"],
        label="SYNTHETIC expired",
        created_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert await rejected(expired) == "expired_token"


async def test_listing_shows_metadata_only_and_administration_is_owner_only(db: Database, seed: Seed) -> None:
    workspace = seed.workspace("Credentials listing")
    owner = seed.user()
    seed.membership(workspace, owner, "owner")
    viewer_client = await issue(
        db,
        workspace,
        role=Role.VIEWER,
        scopes=[Scope.DEALS_READ],
        kind="static_bearer",
        label="SYNTHETIC listed client",
    )
    worker = await issue(db, workspace)
    actor = system(workspace)
    records = await run(db, actor, lambda c: credentials_repo.list_credentials(c, actor))
    assert {r.credential_id for r in records} == {viewer_client.credential_id, worker.credential_id}
    dumped = str([r.model_dump(mode="json") for r in records])
    for issued in (viewer_client, worker):
        token = issued.token.get_secret_value()
        assert token not in dumped and hashlib.sha256(token.encode()).hexdigest() not in dumped
    only_workers = await run(
        db, actor, lambda c: credentials_repo.list_credentials(c, actor, kind="mail_worker")
    )
    assert [r.credential_id for r in only_workers] == [worker.credential_id]
    await run(
        db,
        actor,
        lambda c: credentials_repo.revoke_credential(c, actor, worker.credential_id, reason="SYNTHETIC"),
    )
    active = await run(db, actor, lambda c: credentials_repo.list_credentials(c, actor))
    assert [r.credential_id for r in active] == [viewer_client.credential_id]
    everything = await run(
        db, actor, lambda c: credentials_repo.list_credentials(c, actor, include_inactive=True)
    )
    assert len(everything) == 2
    with pytest.raises(NotFound):
        await run(
            db,
            actor,
            lambda c: credentials_repo.revoke_credential(c, actor, uuid.uuid4(), reason="SYNTHETIC"),
        )
    with pytest.raises(ValidationFailed) as caught:
        await run(
            db,
            actor,
            lambda c: credentials_repo.revoke_credential(c, actor, worker.credential_id, reason="x"),
        )
    assert caught.value.details == {"fields": ["reason"]}
    reviewer = ActorContext(
        workspace_id=workspace,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.REVIEWER,
        scopes=frozenset({Scope.DEALS_READ, Scope.REVIEWS_READ}),
        request_id="req-synthetic-reviewer",
    )
    with pytest.raises(Forbidden):
        await run(db, reviewer, lambda c: credentials_repo.list_credentials(c, reviewer))
    signed_in_owner = ActorContext(
        workspace_id=workspace,
        principal_id=owner,
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset({Scope.CONFIG_ADMIN, Scope.DEALS_READ}),
        request_id="req-synthetic-owner",
    )
    listed = await run(db, signed_in_owner, lambda c: credentials_repo.list_credentials(c, signed_in_owner))
    assert [r.credential_id for r in listed] == [viewer_client.credential_id]
