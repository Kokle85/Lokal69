"""Sender bindings, the server-side Gmail token provider and contact evidence (spec 37.3, 37.5, 37.8).

- ``SELLER_EMAIL_OAUTH_SECRET_REFERENCE`` = ``secretbox:ops.email_sender_bindings/<id>``: the OAuth
  grant is sealed (AES-GCM, AAD bound to workspace/binding/account), opened only by a system
  principal, exchanged at the token endpoint through the injected client (MockTransport here:
  no network) and cached; ``invalid_grant`` marks the binding unhealthy and is ``revoked=True``;
- a sender binding change or revocation after the reservation cancels/suppresses the queued
  inquiry at dispatch (the bound version no longer matches);
- contact evidence: an identical fresh re-verification only records ``last_rechecked_at``; a
  material change supersedes the verified contact.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.v11_inquiries.support import (
    LANGUAGE_DE,
    World,
    alias,
    dispatch,
    owner,
    readiness,
    record_contact,
    reserve_and_queue,
    scalar,
    seller_address,
    system,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryReadiness
from suv_deals.domain.seller_contacts import RecipientEvidenceKind
from suv_deals.errors import ValidationFailed
from suv_deals.integrations.email_providers.base import TokenUnavailable
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.persistence import sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sender_bindings_repo import (
    GOOGLE_TOKEN_URL,
    BindingTokenProvider,
    OAuthRefreshGrant,
    SenderBindingRecord,
)
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

REFRESH = "synthetic-refresh-token-0001"
CLIENT_SECRET = "synthetic-client-secret-0001"


def _box() -> SecretBox:
    return SecretBox({1: os.urandom(32)}, 1)


async def _gmail_binding(db: Database, world: World, box: SecretBox) -> SenderBindingRecord:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        binding = await sender_bindings_repo.create_binding(
            conn,
            actor,
            provider=EmailProviderKind.GMAIL_API,
            account_id="synthetic-gmail-account",
            from_address="owner-alias@example.invalid",
            display_name="Synthetic Owner",
            reason="owner-authorized Gmail alias (synthetic)",
        )
        return await sender_bindings_repo.store_secret(
            conn,
            actor,
            binding.id,
            grant=OAuthRefreshGrant(
                client_id="synthetic-client.apps.example.invalid",
                client_secret=SecretStr(CLIENT_SECRET),
                refresh_token=SecretStr(REFRESH),
            ),
            box=box,
            expected_version=binding.version,
            reason="sealed OAuth grant (synthetic)",
        )


def _transport(
    calls: list[dict[str, list[str]]], respond: Callable[[], httpx.Response]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GOOGLE_TOKEN_URL and request.method == "POST"
        calls.append(parse_qs(request.content.decode()))
        return respond()

    return httpx.MockTransport(handler)


async def test_gmail_token_provider_resolves_the_sealed_reference(db: Database, world: World) -> None:
    box = _box()
    binding = await _gmail_binding(db, world, box)
    assert binding.has_secret and binding.version == 2
    assert REFRESH not in repr(binding) and REFRESH not in binding.model_dump_json()
    envelope = await scalar(
        db,
        world,
        "select secret_envelope from ops.email_sender_bindings where id = %(id)s",
        {"id": binding.id},
    )
    assert REFRESH.encode() not in bytes(envelope)

    reference = sender_bindings_repo.secret_reference_for(binding.id)
    assert sender_bindings_repo.parse_secret_reference(reference) == binding.id
    calls: list[dict[str, list[str]]] = []
    ok = httpx.Response(
        200, json={"access_token": "synthetic-access-1", "expires_in": 3600, "scope": "gmail.send"}
    )
    async with httpx.AsyncClient(transport=_transport(calls, lambda: ok)) as http:
        provider = BindingTokenProvider(
            db=db, workspace_id=world.workspace_id, reference=reference, box=box, http=http
        )
        token = await provider.access_token()
        assert token.token.get_secret_value() == "synthetic-access-1" and "gmail.send" in token.scopes
        assert (await provider.access_token()).token.get_secret_value() == "synthetic-access-1"
        assert len(calls) == 1  # cached until shortly before expiry
        await provider.access_token(force_refresh=True)
        assert len(calls) == 2
    assert calls[0]["grant_type"] == ["refresh_token"] and calls[0]["refresh_token"] == [REFRESH]
    assert "synthetic-access-1" not in repr(provider)

    # Another key (or another binding's AAD) cannot open the envelope.
    async with httpx.AsyncClient(transport=_transport([], lambda: ok)) as http:
        wrong = BindingTokenProvider(
            db=db, workspace_id=world.workspace_id, reference=reference, box=_box(), http=http
        )
        with pytest.raises(TokenUnavailable) as exc:
            await wrong.access_token()
    assert not exc.value.revoked


async def test_invalid_grant_marks_the_binding_unhealthy(db: Database, world: World) -> None:
    box = _box()
    binding = await _gmail_binding(db, world, box)
    calls: list[dict[str, list[str]]] = []
    revoked = httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "Token has been expired"}
    )
    async with httpx.AsyncClient(transport=_transport(calls, lambda: revoked)) as http:
        provider = BindingTokenProvider(
            db=db,
            workspace_id=world.workspace_id,
            reference=sender_bindings_repo.secret_reference_for(binding.id),
            box=box,
            http=http,
        )
        with pytest.raises(TokenUnavailable) as exc:
            await provider.access_token()
    assert exc.value.revoked and exc.value.code == "invalid_grant"
    async with unit_of_work(db, system(world.workspace_id)) as conn:
        after = await sender_bindings_repo.get_binding(conn, system(world.workspace_id), binding.id)
    assert after.health == "unhealthy" and after.health_detail == "CREDENTIALS_REVOKED" and not after.usable


async def test_secret_rules_for_the_local_outlook_route(db: Database, world: World) -> None:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        outlook = await sender_bindings_repo.get_binding(conn, actor, world.sender_binding_id)
    with pytest.raises(ValidationFailed):
        async with unit_of_work(db, actor) as conn:
            await sender_bindings_repo.store_secret(
                conn,
                actor,
                outlook.id,
                grant=OAuthRefreshGrant(
                    client_id="x", client_secret=SecretStr("y"), refresh_token=SecretStr("z")
                ),
                box=_box(),
                expected_version=outlook.version,
                reason="never for outlook_local",
            )
    for bad in ("vault:secret/x", "secretbox:other_table/123", REFRESH):
        with pytest.raises(ValidationFailed):
            sender_bindings_repo.parse_secret_reference(bad)
    with pytest.raises(ValidationFailed):
        async with unit_of_work(db, actor) as conn:
            await sender_bindings_repo.open_sealed_grant(
                conn, owner(world.workspace_id), outlook.id, box=_box()
            )


async def test_sender_change_after_reservation_cancels_the_queued_inquiry(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        current = await sender_bindings_repo.get_binding(conn, actor, world.sender_binding_id)
        changed = await sender_bindings_repo.update_identity(
            conn,
            actor,
            current.id,
            expected_version=current.version,
            display_name="Synthetic Sender Renamed",
            reply_to_address=None,
            reason="display name changed (synthetic)",
        )
    assert changed.version == current.version + 1 and not changed.usable
    result = await dispatch(db, world, record.id)
    assert result.outcome in ("cancelled", "suppressed") and result.attempt is None
    assert (
        await scalar(
            db, world, "select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s", {}
        )
        == 0
    )


async def test_revoked_sender_suppresses_the_queued_inquiry(db: Database, world: World) -> None:
    record = await reserve_and_queue(db, world)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        revoked = await sender_bindings_repo.revoke_binding(
            conn, actor, world.sender_binding_id, reason="credentials revoked (synthetic)"
        )
    assert revoked.revoked
    result = await dispatch(db, world, record.id)
    assert result.outcome in ("cancelled", "suppressed") and result.attempt is None
    _, decision = await readiness(db, world)
    assert decision.readiness != InquiryReadiness.INQUIRY_READY


async def test_contact_reverification_and_supersession(db: Database, world: World) -> None:
    own = [alias("marketplace_seller_id", f"dealer-{world.vehicle.reference}", world.vehicle.source_key)]
    same = await record_contact(
        db,
        world.workspace_id,
        world.vehicle,
        world.seller_entity_id,
        address=world.vehicle.address,
        aliases=own,
    )
    assert same.id == world.vehicle.contact_id and same.last_rechecked_at is not None

    new_address = seller_address()
    changed = await record_contact(
        db,
        world.workspace_id,
        world.vehicle,
        world.seller_entity_id,
        address=new_address,
        aliases=own,
        language=LANGUAGE_DE,
    )
    assert changed.id != world.vehicle.contact_id and changed.status == "verified"
    assert changed.address == new_address
    old = await scalar(
        db,
        world,
        "select status || ':' || superseded_by_id::text from app.seller_contacts where id = %(id)s",
        {"id": world.vehicle.contact_id},
    )
    assert old == f"changed:{changed.id}"
    with pytest.raises(ValidationFailed):
        await record_contact(
            db,
            world.workspace_id,
            world.vehicle,
            world.seller_entity_id,
            address="relay-123@relay.synthetic-dealer.example",
            aliases=own,
            kind=RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING,
        )
