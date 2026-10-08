"""The desktop worker wired to the real backend app (see ``conftest.py``).

Threading mirrors the real desktop design: every desktop call (worker cycle, local store, API
client) runs on ONE dedicated thread (the Outlook STA stand-in) while the backend app and its
database pool run on the test's event loop. `LoopTransport` is the synchronous ``httpx``
transport of the desktop ``BridgeApiClient``: it hands each request to the app's ASGI interface on
the event loop (``asyncio.run_coroutine_threadsafe``) and can simulate a backend outage.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

import httpx
from outlook_bridge.api_client import BridgeApiClient, ClientIdentity
from outlook_bridge.compatibility import (
    C2R_CONFIGURATION_KEY,
    OUTLOOK_PROGID,
    CompatibilityReport,
    Hive,
    RegistryValue,
    check_compatibility,
)
from outlook_bridge.config import parse_config
from outlook_bridge.credentials import CredentialManager, InMemoryCredentialStore, WorkerCredential
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.outlook_adapter import OutlookComSession
from outlook_bridge.testing import FakeAccount, FakeFolder, FakeMailItem, FakeOutlook
from outlook_bridge.worker import BridgeWorker, CycleReport

from suv_deals.clock import FrozenClock

T = TypeVar("T")
API_BASE = "https://dashboard.synthetic.example"  # an allowed Host of the test app
WORKER_ID = "desktop-e2e-1"


class LoopTransport(httpx.BaseTransport):
    """Synchronous transport into an ASGI app running on ``loop`` (no network)."""

    def __init__(self, app: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._asgi = httpx.ASGITransport(app=app)
        self._loop = loop
        self.down = False
        self.calls: list[tuple[str, str, int]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("synthetic backend outage", request=request)
        body = request.read()

        async def forward() -> tuple[int, list[tuple[bytes, bytes]], bytes]:
            inner = httpx.Request(request.method, request.url, headers=request.headers, content=body)
            response = await self._asgi.handle_async_request(inner)
            content = await response.aread()
            return response.status_code, list(response.headers.raw), content

        status, headers, content = asyncio.run_coroutine_threadsafe(forward(), self._loop).result(timeout=60)
        self.calls.append((request.method, request.url.path, status))
        return httpx.Response(status, headers=headers, content=content, request=request)


class _Registry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str | None], RegistryValue] = {
            ("HKCR", rf"{OUTLOOK_PROGID}\CLSID", None): "{0006F03A-0000-0000-C000-000000000046}",
            ("HKCR", rf"{OUTLOOK_PROGID}\CurVer", None): "Outlook.Application.16",
            ("HKLM", C2R_CONFIGURATION_KEY, "VersionToReport"): "16.0.17928.20114",
        }

    def read_value(self, hive: Hive, key: str, name: str | None) -> RegistryValue:
        return self.values.get((hive, key, name))


def classic_compat(now: datetime) -> CompatibilityReport:
    return check_compatibility(
        now=now,
        platform="win32",
        registry=_Registry(),
        processes=lambda: frozenset({"outlook.exe", "explorer.exe"}),
    )


class Desktop:
    """One fake classic Outlook + the real desktop worker for one mailbox binding."""

    def __init__(
        self,
        *,
        app: Any,
        loop: asyncio.AbstractEventLoop,
        mailbox_binding_id: UUID,
        token: str,
        account_address: str,
        tmp_path: Path,
        send_intents_enabled: bool = True,
    ) -> None:
        self.loop = loop
        self.mailbox_binding_id = mailbox_binding_id
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outlook-sta")
        self.clock = FrozenClock(datetime.now(UTC).replace(microsecond=0))
        self.outlook = FakeOutlook(self.clock.now)
        self.account: FakeAccount = self.outlook.add_account(account_address, display_name="Synthetic Sender")
        self.transport = LoopTransport(app, loop)
        self.config = parse_config(
            {
                "api_base_url": API_BASE,
                "mailbox_binding_id": str(mailbox_binding_id),
                "worker_id": WORKER_ID,
                "account_smtp_address": account_address,
                "folders": [{"role": "inbox"}, {"role": "junk"}],
                "send_intents_enabled": send_intents_enabled,
            }
        )
        self.token = token
        self.tmp_path = tmp_path
        self.store: LocalStore | None = None
        self.api: BridgeApiClient | None = None
        self.credentials: CredentialManager | None = None
        self.worker: BridgeWorker | None = None

    # ------------------------------------------------------------------ thread plumbing

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` on the desktop thread (never on the event loop)."""
        return await self.loop.run_in_executor(self.executor, partial(fn, *args))

    def _build(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)  # owned by the desktop thread
        store = LocalStore(connection, self.mailbox_binding_id)
        store._initialise(wal=False)
        self.store = store
        self.credentials = CredentialManager(
            InMemoryCredentialStore(WorkerCredential(token=self.token)), store
        )
        credentials = self.credentials
        self.api = BridgeApiClient(
            self.config.api_base_url,
            token_provider=lambda: credentials.token(self.clock.now()),
            identity=ClientIdentity(self.mailbox_binding_id, WORKER_ID),
            transport=self.transport,
        )
        session = OutlookComSession(
            attach=self.outlook.attach,
            bind_events=self.outlook.bind_events,
            temp_dir=self.tmp_path / "outlook-tmp",
            now=self.clock.now,
        )
        self.worker = BridgeWorker(
            config=self.config,
            store=store,
            mailbox=session,
            api=self.api,
            credentials=credentials,
            clock=self.clock,
            compat=classic_compat(self.clock.now()),
        )

    async def build(self) -> None:
        await self.run(self._build)

    def _worker(self) -> BridgeWorker:
        assert self.worker is not None
        return self.worker

    async def start(self) -> CycleReport:
        return await self.run(lambda: self._worker().start())

    async def tick(self, advance: timedelta = timedelta(0)) -> CycleReport:
        self.clock.advance(advance)
        return await self.run(lambda: self._worker().tick())

    async def close(self) -> None:
        def shutdown() -> None:
            if self.api is not None:
                self.api.close()
            if self.store is not None:
                self.store.close()

        await self.run(shutdown)
        self.executor.shutdown(wait=True)

    # ------------------------------------------------------------------ mailbox scenario

    @property
    def inbox(self) -> FakeFolder:
        return self.outlook.folder(self.account, "inbox")

    def deliver_reply(
        self,
        *,
        sender: str,
        in_reply_to: str,
        body: str = "Guten Tag,\nja, das Fahrzeug ist noch verfuegbar. Die Unterlagen schicke ich gerne.\n",
        subject: str = "AW: Anfrage zu Ihrem Fahrzeug (synthetic)",
        fire_event: bool = True,
    ) -> FakeMailItem:
        return self.outlook.deliver(
            self.inbox,
            subject=subject,
            body=body,
            sender=sender,
            message_id=f"<reply-{uuid.uuid4().hex[:16]}@synthetic-dealer.example>",
            received_at=self.clock.now() - timedelta(minutes=1),
            in_reply_to=in_reply_to,
            references=(in_reply_to,),
            fire_event=fire_event,
        )

    def deliver_personal(self) -> FakeMailItem:
        """Unrelated personal mail: it must never leave the machine."""
        return self.outlook.deliver(
            self.inbox,
            subject="Dinner on Friday? (synthetic)",
            body="Private note, nothing about cars. Synthetic.",
            sender="friend@private.example.invalid",
            message_id=f"<personal-{uuid.uuid4().hex[:16]}@private.example.invalid>",
            received_at=self.clock.now() - timedelta(minutes=1),
            fire_event=True,
        )

    async def backlog(self) -> list[Any]:
        return await self.run(lambda: self.store.backlog_rows() if self.store else [])


__all__ = ["API_BASE", "WORKER_ID", "Desktop", "LoopTransport", "classic_compat"]
