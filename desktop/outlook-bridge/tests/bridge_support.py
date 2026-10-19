"""Shared scenario helpers for the Outlook bridge tests (importable as ``bridge_support``).

Everything is synthetic: ``FakeOutlook`` instead of Outlook/COM, ``FakeBackend`` behind
``httpx.MockTransport`` instead of the backend, an in-memory credential store and
``example.invalid`` addresses. Nothing here touches a network, a mailbox or sends e-mail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from outlook_bridge.api_client import BridgeApiClient, ClientIdentity
from outlook_bridge.compatibility import (
    C2R_CONFIGURATION_KEY,
    NEW_OUTLOOK_PREFERENCES_KEY,
    NEW_OUTLOOK_TOGGLE_VALUE,
    OUTLOOK_PROGID,
    CompatibilityReport,
    Hive,
    RegistryValue,
    check_compatibility,
)
from outlook_bridge.config import BridgeConfig, parse_config
from outlook_bridge.credentials import (
    CredentialManager,
    InMemoryCredentialStore,
    WorkerCredential,
)
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.outlook_adapter import OutlookComSession
from outlook_bridge.testing import (
    AttachmentSpec,
    FakeAccount,
    FakeBackend,
    FakeFolder,
    FakeMailItem,
    FakeOutlook,
    inquiry_message_id,
)
from outlook_bridge.worker import BridgeWorker

from suv_deals.clock import FrozenClock

MAILBOX_ID = UUID("77777777-7777-4777-8777-777777777777")
OTHER_MAILBOX_ID = UUID("88888888-8888-4888-8888-888888888888")
OWNER = "owner@example.invalid"
SELLER = "seller@dealer.example.invalid"
SELLER_RELAY = "relay-4711@marketplace.example.invalid"
# Synthetic worker credentials for the fake backend only (never real secrets).
TOKEN = "mw_test_ingest_credential_0123456789abcdef"
TOKEN_2 = "mw_test_ingest_credential_rotated_fedcba9876"
LISTING_REF = "REF-1234"
LISTING_URL = "https://listing.example.invalid/ad/1234"
START = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


class FakeRegistry:
    """Read-only registry fake keyed by (hive, key, value-name)."""

    def __init__(self, values: dict[tuple[str, str, str | None], RegistryValue] | None = None) -> None:
        self.values = dict(values or {})
        self.reads: list[tuple[str, str, str | None]] = []

    def read_value(self, hive: Hive, key: str, name: str | None) -> RegistryValue:
        self.reads.append((hive, key, name))
        return self.values.get((hive, key, name))


def classic_registry(*, toggle: int | None = None) -> FakeRegistry:
    values: dict[tuple[str, str, str | None], RegistryValue] = {
        ("HKCR", rf"{OUTLOOK_PROGID}\CLSID", None): "{0006F03A-0000-0000-C000-000000000046}",
        ("HKCR", rf"{OUTLOOK_PROGID}\CurVer", None): "Outlook.Application.16",
        ("HKLM", C2R_CONFIGURATION_KEY, "VersionToReport"): "16.0.17928.20114",
    }
    if toggle is not None:
        values[("HKCU", NEW_OUTLOOK_PREFERENCES_KEY, NEW_OUTLOOK_TOGGLE_VALUE)] = toggle
    return FakeRegistry(values)


def classic_compat(now: datetime) -> CompatibilityReport:
    return check_compatibility(
        now=now,
        platform="win32",
        registry=classic_registry(),
        processes=lambda: frozenset({"outlook.exe", "explorer.exe"}),
    )


def make_config(data_dir: Path | None = None, **overrides: Any) -> BridgeConfig:
    raw: dict[str, Any] = {
        "api_base_url": "https://api.example.invalid",
        "mailbox_binding_id": str(MAILBOX_ID),
        "worker_id": "desktop-test-1",
        "account_smtp_address": OWNER,
        "folders": [{"role": "inbox"}, {"role": "junk"}, {"role": "rule_target", "path": "Inbox/Cars"}],
    }
    if data_dir is not None:
        raw["data_dir"] = str(data_dir)
    raw.update(overrides)
    return parse_config(raw)


class Harness:
    """One fake Outlook + fake backend + local store + worker wiring for a scenario."""

    def __init__(self, tmp_path: Path, *, config_overrides: dict[str, Any] | None = None) -> None:
        self.tmp_path = tmp_path
        self.clock = FrozenClock(START)
        self.outlook = FakeOutlook(self.clock.now)
        self.account: FakeAccount = self.outlook.add_account(OWNER, display_name="Synthetic Owner")
        self.other_account: FakeAccount = self.outlook.add_account("private@other.example.invalid")
        self.cars: FakeFolder = self.outlook.create_folder(self.inbox, "Cars")
        self.backend = FakeBackend(mailbox_binding_id=MAILBOX_ID, token=TOKEN, clock=self.clock.now)
        self.config = make_config(tmp_path / "data", **(config_overrides or {}))
        self.store = LocalStore.in_memory(MAILBOX_ID)
        self.cred_store = InMemoryCredentialStore(WorkerCredential(token=TOKEN))
        self.credentials = CredentialManager(self.cred_store, self.store)
        self.api_identity = ClientIdentity(MAILBOX_ID, self.config.worker_id, self.store.store_instance_id)
        self.api = BridgeApiClient(
            self.config.api_base_url,
            token_provider=lambda: self.credentials.token(self.clock.now()),
            identity=self.api_identity,
            transport=self.backend.transport(),
        )
        self.session = OutlookComSession(
            attach=self.outlook.attach,
            bind_events=self.outlook.bind_events,
            temp_dir=tmp_path / "tmp",
            now=self.clock.now,
        )
        self.compat = classic_compat(self.clock.now())
        self._worker: BridgeWorker | None = None

    # ------------------------------------------------------------------ folders

    @property
    def inbox(self) -> FakeFolder:
        return self.outlook.folder(self.account, "inbox")

    @property
    def junk(self) -> FakeFolder:
        return self.outlook.folder(self.account, "junk")

    @property
    def sent(self) -> FakeFolder:
        return self.outlook.folder(self.account, "sent")

    @property
    def outbox(self) -> FakeFolder:
        return self.outlook.folder(self.account, "outbox")

    # ------------------------------------------------------------------ worker

    def worker(self, *, dry_run: bool = False, compat: CompatibilityReport | None = None) -> BridgeWorker:
        self._worker = BridgeWorker(
            config=self.config,
            store=self.store,
            mailbox=self.session,
            api=self.api,
            credentials=self.credentials,
            clock=self.clock,
            compat=compat or self.compat,
            dry_run=dry_run,
        )
        return self._worker

    def advance(self, delta: timedelta) -> datetime:
        self.clock.advance(delta)
        return self.clock.now()

    # ------------------------------------------------------------------ scenario data

    def bind(self, inquiry_id: UUID, **overrides: Any) -> int:
        kwargs: dict[str, Any] = {
            "outbound_message_ids": [inquiry_message_id(inquiry_id)],
            "aliases": [SELLER],
            "listing_references": [LISTING_REF],
            "listing_urls": [LISTING_URL],
        }
        kwargs.update(overrides)
        return self.backend.publish_binding(inquiry_id, **kwargs)

    def deliver_reply(
        self,
        inquiry_id: UUID,
        *,
        folder: FakeFolder | None = None,
        message_id: str | None = None,
        sender: str = SELLER,
        body: str = "Guten Tag,\nja, das Fahrzeug ist noch verfügbar. Unterlagen schicke ich gerne.\n",
        subject: str = f"AW: Anfrage zu Synthetic SUV \u2013 {LISTING_REF}",
        received_at: datetime | None = None,
        fire_event: bool = True,
        attachments: tuple[AttachmentSpec, ...] = (),
        references: tuple[str, ...] | None = None,
        transport_headers: bool = True,
        extra_headers: dict[str, str] | None = None,
        message_class: str = "IPM.Note",
    ) -> FakeMailItem:
        outbound = inquiry_message_id(inquiry_id)
        return self.outlook.deliver(
            folder or self.inbox,
            subject=subject,
            body=body,
            sender=sender,
            message_id=message_id
            if message_id is not None
            else f"<reply-{uuid4().hex[:12]}@dealer.example.invalid>",
            received_at=received_at or (self.clock.now() - timedelta(minutes=1)),
            in_reply_to=outbound,
            references=references if references is not None else (outbound,),
            attachments=attachments,
            transport_headers=transport_headers,
            extra_headers=extra_headers,
            fire_event=fire_event,
            message_class=message_class,
        )

    def deliver_personal(
        self,
        *,
        subject: str = "Dinner on Friday?",
        body: str = "Hi, are you free on Friday evening? Private note, nothing about cars.",
        sender: str = "friend@private.example.invalid",
        folder: FakeFolder | None = None,
        fire_event: bool = True,
        message_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> FakeMailItem:
        return self.outlook.deliver(
            folder or self.inbox,
            subject=subject,
            body=body,
            sender=sender,
            message_id=message_id or f"<personal-{uuid4().hex[:12]}@private.example.invalid>",
            received_at=self.clock.now() - timedelta(minutes=1),
            in_reply_to=in_reply_to,
            references=(in_reply_to,) if in_reply_to else (),
            fire_event=fire_event,
        )

    def reply_posts(self) -> list[dict[str, Any]]:
        return self.backend.reply_posts

    def close(self) -> None:
        self.api.close()
        self.store.close()
