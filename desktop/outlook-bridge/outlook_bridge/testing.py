"""In-memory fakes so the worker runs and is tested on Linux (no Outlook, no network, no email).

- ``FakeOutlook`` builds a small Outlook Object Model graph (Application, Namespace, Accounts,
  Stores, Folders, Items, MailItems, PropertyAccessor, Attachments, Recipients) with the subset of
  behaviour ``OutlookComSession`` relies on: ``Items.Sort/GetFirst/GetNext/Find``,
  ``GetItemFromID``/``GetFolderFromID`` scoped by StoreID, EntryIDs that change on move,
  ``NewMailEx`` event sinks, ``CreateItem``/``Send`` into Outbox and a later move to Sent Items.
  With ``strict_sta`` every COM access off the STA thread is recorded as a violation and raises.
- ``FakeComApi`` stands in for ``pythoncom`` and runs posted event callbacks inside
  ``PumpWaitingMessages`` - i.e. on the STA thread, like real COM events.
- ``FakeBackend`` implements the mailbox-worker API semantics over ``httpx.MockTransport`` using
  the shared domain rules (binding change log with opaque cursors and tombstones, 37.8 reply
  validation, ``decide_ingest`` idempotency, send intents with claims and reports) plus fault
  injection (outage, timeouts after storing, 401/403/409/429/503).

All addresses and identifiers are synthetic (``example.invalid``).
"""

from __future__ import annotations

import itertools
import json
import queue
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any, Final
from uuid import UUID

import httpx
from pydantic import ValidationError

from outlook_bridge.outlook_adapter import (
    OL_FOLDER_INBOX,
    OL_FOLDER_JUNK,
    OL_FOLDER_OUTBOX,
    OL_FOLDER_SENT_MAIL,
    PR_ATTACH_MIME_TAG,
    PR_ATTACHMENT_HIDDEN,
    PR_CLIENT_SUBMIT_TIME,
    PR_IN_REPLY_TO_ID,
    PR_INTERNET_MESSAGE_ID,
    PR_INTERNET_REFERENCES,
    PR_MESSAGE_DELIVERY_TIME,
    PR_SMTP_ADDRESS,
    PR_TRANSPORT_MESSAGE_HEADERS,
)
from outlook_bridge.sta_runtime import SessionContext
from outlook_bridge.wire import ReplyUpload, WorkerSendIntent, message_body_hash
from suv_deals.domain.replies import (
    InquiryBindingState,
    StoredReplyIngest,
    decide_ingest,
)
from suv_deals.domain.seller_templates import build_vehicle_label, render

INTERACTIVE_SESSION: Final = SessionContext(
    platform="win32",
    user_sid="S-1-5-21-1000-1000-1000-1001",
    session_id=1,
    window_station="WinSta0",
    window_station_visible=True,
)


class FakeComError(Exception):
    """Stands in for ``pywintypes.com_error``."""


# =============================================================================================
# COM apartment fake
# =============================================================================================


class FakeComApi:
    """``pythoncom`` stand-in: records apartment calls, runs posted callbacks while pumping."""

    def __init__(self, *, fail_initialize: bool = False) -> None:
        self.initialized_threads: list[int] = []
        self.uninitialized = 0
        self.pumps = 0
        self._fail = fail_initialize
        self._posted: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    def co_initialize(self) -> None:
        if self._fail:
            raise FakeComError("CoInitializeEx failed")
        self.initialized_threads.append(threading.get_ident())

    def co_uninitialize(self) -> None:
        self.uninitialized += 1

    def pump_waiting_messages(self) -> None:
        self.pumps += 1
        while True:
            try:
                callback = self._posted.get_nowait()
            except queue.Empty:
                return
            callback()

    def post(self, callback: Callable[[], None]) -> None:
        self._posted.put(callback)


# =============================================================================================
# Outlook Object Model fakes
# =============================================================================================


class _Root:
    """Shared state of one fake Outlook instance."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock
        self.sta_ident: int | None = None
        self.violations: list[str] = []
        self.entry_ids = itertools.count(0x1000)
        self.running = True
        self.offline = False
        self.exchange_mode = 0
        self.send_mode = "ok"  # ok | raise | raise_after_queue
        self.message_id_settable = True
        #: Address-book resolution: a recipient string added to a new item resolves to this address.
        self.resolve_overrides: dict[str, str] = {}
        self.send_calls: list[str] = []  # subjects of every .Send() call
        self.item_reads: list[str] = []  # EntryIDs whose Body was read
        self.handlers: list[Any] = []
        self.com_api: FakeComApi | None = None
        self.items: dict[str, FakeMailItem] = {}
        self.local = threading.local()

    def touch(self, name: str) -> None:
        if (
            self.sta_ident is not None
            and threading.get_ident() != self.sta_ident
            and not getattr(self.local, "controller", False)
        ):
            self.violations.append(name)
            raise FakeComError(f"COM access to {name} off the STA thread")

    @contextmanager
    def controller(self) -> Iterator[None]:
        """Test-controller access (scenario set-up) is exempt from the STA check."""
        previous = getattr(self.local, "controller", False)
        self.local.controller = True
        try:
            yield
        finally:
            self.local.controller = previous

    def new_entry_id(self) -> str:
        return f"00000000{next(self.entry_ids):024X}"


class _FakeCom:
    """Base: every public attribute access is checked against the STA thread."""

    _root: _Root

    def __init__(self, root: _Root) -> None:
        object.__setattr__(self, "_root", root)

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            object.__getattribute__(self, "_root").touch(name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_"):
            self._root.touch(name)
        object.__setattr__(self, name, value)


class FakeCollection(_FakeCom):
    def __init__(self, root: _Root, items: list[Any]) -> None:
        super().__init__(root)
        self._items = items

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, index: int) -> Any:
        if not 1 <= index <= len(self._items):
            raise FakeComError("index out of range")
        return self._items[index - 1]

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._items))


class FakePropertyAccessor(_FakeCom):
    def __init__(self, root: _Root, props: dict[str, Any]) -> None:
        super().__init__(root)
        self._props = props

    def GetProperty(self, tag: str) -> Any:
        if tag not in self._props:
            raise FakeComError("property not found")
        return self._props[tag]

    def SetProperty(self, tag: str, value: Any) -> None:
        if tag == PR_INTERNET_MESSAGE_ID and not self._root.message_id_settable:
            raise FakeComError("property is read-only")
        self._props[tag] = value


@dataclass(frozen=True)
class AttachmentSpec:
    filename: str
    content: bytes
    mime_type: str | None = None
    hidden: bool = False
    kind: int = 1  # olByValue


class FakeAttachment(_FakeCom):
    def __init__(self, root: _Root, spec: AttachmentSpec) -> None:
        super().__init__(root)
        self._spec = spec
        props: dict[str, Any] = {}
        if spec.mime_type:
            props[PR_ATTACH_MIME_TAG] = spec.mime_type
        if spec.hidden:
            props[PR_ATTACHMENT_HIDDEN] = True
        self.PropertyAccessor = FakePropertyAccessor(root, props)
        self.FileName = spec.filename
        self.DisplayName = spec.filename
        self.Size = len(spec.content) + 120  # Outlook's Size includes MAPI overhead
        self.Type = spec.kind

    def SaveAsFile(self, path: str) -> None:
        with open(path, "wb") as handle:
            handle.write(self._spec.content)


class FakeRecipient(_FakeCom):
    def __init__(self, root: _Root, address: str) -> None:
        super().__init__(root)
        self.Address = address
        self.Type = 1
        self.PropertyAccessor = FakePropertyAccessor(root, {PR_SMTP_ADDRESS: address})

    def Resolve(self) -> bool:
        return "@" in self.Address and " " not in self.Address


class FakeRecipients(FakeCollection):
    def Add(self, address: str) -> FakeRecipient:
        recipient = FakeRecipient(self._root, self._root.resolve_overrides.get(address, address))
        self._items.append(recipient)
        return recipient


class FakeExchangeUser(_FakeCom):
    def __init__(self, root: _Root, smtp: str) -> None:
        super().__init__(root)
        self.PrimarySmtpAddress = smtp


class FakeAddressEntry(_FakeCom):
    def __init__(self, root: _Root, smtp: str | None) -> None:
        super().__init__(root)
        self._smtp = smtp
        self.PropertyAccessor = FakePropertyAccessor(root, {PR_SMTP_ADDRESS: smtp} if smtp else {})

    def GetExchangeUser(self) -> FakeExchangeUser | None:
        return FakeExchangeUser(self._root, self._smtp) if self._smtp else None


class FakeMailItem(_FakeCom):
    def __init__(self, root: _Root, folder: FakeFolder | None) -> None:
        super().__init__(root)
        self._folder = folder
        self._props: dict[str, Any] = {}
        self._discarded = False
        self._body = ""
        self.EntryID = root.new_entry_id()
        self.MessageClass = "IPM.Note"
        self.Subject = ""
        self.SenderEmailAddress = ""
        self.SenderEmailType = "SMTP"
        self.Sender: FakeAddressEntry | None = None
        self.ReceivedTime: datetime | None = None
        self.PropertyAccessor = FakePropertyAccessor(root, self._props)
        self.Attachments = FakeCollection(root, [])
        self.Recipients = FakeRecipients(root, [])
        self.ReplyRecipients = FakeRecipients(root, [])
        self.CC = ""
        self.BCC = ""
        self.BodyFormat = 2
        self.SendUsingAccount: FakeAccount | None = None
        root.items[self.EntryID] = self

    @property
    def Body(self) -> str:
        self._root.item_reads.append(self.EntryID)
        return self._body

    @Body.setter
    def Body(self, value: str) -> None:
        self._body = value

    @property
    def Parent(self) -> FakeFolder:
        if self._folder is None:
            raise FakeComError("item has no parent")
        return self._folder

    def Send(self) -> None:
        root = self._root
        root.send_calls.append(self.Subject)
        if root.send_mode == "raise":
            raise FakeComError("simulated Send failure")
        account = self.SendUsingAccount
        if account is None:
            raise FakeComError("no sending account")
        outbox = account._store._defaults[OL_FOLDER_OUTBOX]
        if PR_INTERNET_MESSAGE_ID not in self._props:
            self._props[PR_INTERNET_MESSAGE_ID] = f"<{uuid.uuid4().hex}@outlook.example.invalid>"
        self._props[PR_CLIENT_SUBMIT_TIME] = root.clock().astimezone(UTC).replace(tzinfo=None)
        outbox._attach(self)
        if root.send_mode == "raise_after_queue":
            raise FakeComError("simulated Send failure after queueing")

    def Close(self, mode: int) -> None:
        self._discarded = True


class FakeItems(_FakeCom):
    _FIND_RE: Final = re.compile(r"^@SQL=\"(?P<tag>[^\"]+)\" = '(?P<value>(?:[^']|'')*)'$")

    def __init__(self, root: _Root, folder: FakeFolder) -> None:
        super().__init__(root)
        self._folder = folder
        self._order: list[FakeMailItem] = list(folder._items)
        self._cursor = 0

    @property
    def Count(self) -> int:
        return len(self._folder._items)

    def Item(self, index: int) -> FakeMailItem:
        return self._folder._items[index - 1]

    def Sort(self, prop: str, descending: bool = False) -> None:
        def key(item: FakeMailItem) -> datetime:
            if prop == "[SentOn]":
                value = item._props.get(PR_CLIENT_SUBMIT_TIME)
            else:
                value = item._props.get(PR_MESSAGE_DELIVERY_TIME)
            return value if isinstance(value, datetime) else datetime(1601, 1, 1)  # noqa: DTZ001

        self._order = sorted(self._folder._items, key=key, reverse=descending)

    def GetFirst(self) -> FakeMailItem | None:
        self._cursor = 0
        return self.GetNext()

    def GetNext(self) -> FakeMailItem | None:
        if self._cursor >= len(self._order):
            return None
        item = self._order[self._cursor]
        self._cursor += 1
        return item

    def Find(self, query: str) -> FakeMailItem | None:
        match = self._FIND_RE.fullmatch(query)
        if match is None:
            raise FakeComError("unsupported filter")
        value = match.group("value").replace("''", "'")
        for item in self._folder._items:
            if item._props.get(match.group("tag")) == value:
                return item
        return None


class FakeFolder(_FakeCom):
    def __init__(self, root: _Root, store: FakeStore, name: str, parent: FakeFolder | None) -> None:
        super().__init__(root)
        self._store = store
        self._items: list[FakeMailItem] = []
        self._children: list[FakeFolder] = []
        self.EntryID = root.new_entry_id()
        self.StoreID = store.StoreID
        self.Name = name
        self.FolderPath: str = (parent.FolderPath if parent else "\\\\" + store._display) + "\\" + name
        store._folders[self.EntryID] = self

    @property
    def Folders(self) -> FakeCollection:
        return FakeCollection(self._root, self._children)

    @property
    def Items(self) -> FakeItems:
        return FakeItems(self._root, self)

    def _attach(self, item: FakeMailItem) -> None:
        if item._folder is not None and item in item._folder._items:
            item._folder._items.remove(item)
        item._folder = self
        self._items.append(item)


class FakeStore(_FakeCom):
    def __init__(self, root: _Root, display: str) -> None:
        super().__init__(root)
        self._display = display
        self._folders: dict[str, FakeFolder] = {}
        self.StoreID = "0000000038A1BB1005E5101AA1BB08002B2A56C2" + uuid.uuid4().hex.upper()
        self._root_folder = FakeFolder(root, self, "Root", None)
        self._defaults: dict[int, FakeFolder] = {}
        for number, name in (
            (OL_FOLDER_INBOX, "Inbox"),
            (OL_FOLDER_JUNK, "Junk Email"),
            (OL_FOLDER_SENT_MAIL, "Sent Items"),
            (OL_FOLDER_OUTBOX, "Outbox"),
        ):
            folder = FakeFolder(root, self, name, self._root_folder)
            self._root_folder._children.append(folder)
            self._defaults[number] = folder

    def GetDefaultFolder(self, number: int) -> FakeFolder:
        if number not in self._defaults:
            raise FakeComError("no such default folder")
        return self._defaults[number]

    def GetRootFolder(self) -> FakeFolder:
        return self._root_folder


class FakeAccount(_FakeCom):
    def __init__(self, root: _Root, smtp: str, display_name: str, account_type: int) -> None:
        super().__init__(root)
        self._store = FakeStore(root, smtp)
        self.SmtpAddress = smtp
        self.DisplayName = display_name
        self.AccountType = account_type
        self.DeliveryStore = self._store


class FakeSyncObject(_FakeCom):
    pass


class FakeNamespace(_FakeCom):
    def __init__(self, root: _Root, accounts: list[FakeAccount]) -> None:
        super().__init__(root)
        self._accounts = accounts
        self.SyncObjects = FakeCollection(root, [FakeSyncObject(root)])

    @property
    def Accounts(self) -> FakeCollection:
        return FakeCollection(self._root, self._accounts)

    @property
    def Offline(self) -> bool:
        return self._root.offline

    @property
    def ExchangeConnectionMode(self) -> int:
        return self._root.exchange_mode

    def _store(self, store_id: str) -> FakeStore:
        for account in self._accounts:
            if account._store.StoreID == store_id:
                return account._store
        raise FakeComError("store not found")

    def GetItemFromID(self, entry_id: str, store_id: str | None = None) -> FakeMailItem:
        item = self._root.items.get(entry_id)
        if item is None or item._folder is None:
            raise FakeComError("item not found")
        if store_id is not None and item._folder._store.StoreID != store_id:
            raise FakeComError("item not found in store")
        return item

    def GetFolderFromID(self, entry_id: str, store_id: str) -> FakeFolder:
        folder = self._store(store_id)._folders.get(entry_id)
        if folder is None:
            raise FakeComError("folder not found")
        return folder


class FakeOutlookApp(_FakeCom):
    def __init__(self, root: _Root, namespace: FakeNamespace) -> None:
        super().__init__(root)
        self.Version = "16.0.17928.20114"
        self._namespace = namespace

    @property
    def Session(self) -> FakeNamespace:
        if not self._root.running:
            raise FakeComError("RPC server unavailable")
        return self._namespace

    def CreateItem(self, kind: int) -> FakeMailItem:
        if kind != 0:
            raise FakeComError("only mail items are supported")
        return FakeMailItem(self._root, None)


class FakeOutlook:
    """Test controller around the fake object model (not itself a COM object)."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.root = _Root(clock)
        self._accounts: list[FakeAccount] = []
        self.namespace = FakeNamespace(self.root, self._accounts)
        self.app = FakeOutlookApp(self.root, self.namespace)

    # ------------------------------------------------------------------ wiring for OutlookComSession

    def attach(self) -> FakeOutlookApp:
        if not self.root.running:
            raise FakeComError("Outlook is not running")
        return self.app

    def bind_events(self, obj: Any, handler_class: type) -> Any:
        handler = handler_class()
        if obj is self.app:
            self.root.handlers.append(handler)
        return handler

    def use_com_api(self, com_api: FakeComApi) -> None:
        self.root.com_api = com_api

    def strict_sta(self, thread_ident: int | None) -> None:
        self.root.sta_ident = thread_ident

    # ------------------------------------------------------------------ scenario helpers

    def add_account(
        self, smtp: str, *, display_name: str = "Synthetic Sender", account_type: int = 1
    ) -> FakeAccount:
        account = FakeAccount(self.root, smtp, display_name, account_type)
        self._accounts.append(account)
        return account

    def folder(self, account: FakeAccount, role: str) -> FakeFolder:
        numbers = {
            "inbox": OL_FOLDER_INBOX,
            "junk": OL_FOLDER_JUNK,
            "sent": OL_FOLDER_SENT_MAIL,
            "outbox": OL_FOLDER_OUTBOX,
        }
        return account._store._defaults[numbers[role]]

    def create_folder(self, parent: FakeFolder, name: str) -> FakeFolder:
        folder = FakeFolder(self.root, parent._store, name, parent)
        parent._children.append(folder)
        return folder

    def deliver(
        self,
        folder: FakeFolder,
        *,
        subject: str,
        body: str,
        sender: str,
        message_id: str | None,
        received_at: datetime,
        in_reply_to: str | None = None,
        references: Sequence[str] = (),
        attachments: Sequence[AttachmentSpec] = (),
        message_class: str = "IPM.Note",
        extra_headers: Mapping[str, str] | None = None,
        transport_headers: bool = True,
        fire_event: bool = True,
    ) -> FakeMailItem:
        """Put a received item into ``folder`` (optionally firing ``NewMailEx``)."""
        root = self.root
        item = FakeMailItem(root, None)
        item.MessageClass = message_class
        item.Subject = subject
        item.Body = body
        item.SenderEmailAddress = sender
        item.Sender = FakeAddressEntry(root, sender)
        item.ReceivedTime = received_at
        props = item._props
        props[PR_MESSAGE_DELIVERY_TIME] = received_at.astimezone(UTC).replace(tzinfo=None)
        if message_id is not None:
            props[PR_INTERNET_MESSAGE_ID] = message_id
        if transport_headers:
            lines = [f"From: {sender}", "To: owner@example.invalid", f"Subject: {subject}"]
            lines.append(f"Date: {format_datetime(received_at.astimezone(UTC))}")
            if message_id is not None:
                lines.append(f"Message-ID: {message_id}")
            if in_reply_to is not None:
                lines.append(f"In-Reply-To: {in_reply_to}")
            if references:
                lines.append("References: " + " ".join(references))
            for name, value in (extra_headers or {}).items():
                lines.append(f"{name}: {value}")
            props[PR_TRANSPORT_MESSAGE_HEADERS] = "\r\n".join(lines) + "\r\n\r\n"
        else:
            if in_reply_to is not None:
                props[PR_IN_REPLY_TO_ID] = in_reply_to
            if references:
                props[PR_INTERNET_REFERENCES] = " ".join(references)
        object.__setattr__(
            item, "Attachments", FakeCollection(root, [FakeAttachment(root, a) for a in attachments])
        )
        folder._attach(item)
        if fire_event:
            self.fire_new_mail((item.EntryID,))
        return item

    def move(self, item: FakeMailItem, target: FakeFolder, *, new_entry_id: bool = True) -> FakeMailItem:
        """Move an item; like Exchange/PST moves, the EntryID changes by default."""
        root = self.root
        if new_entry_id:
            del root.items[item.EntryID]
            object.__setattr__(item, "EntryID", root.new_entry_id())
            root.items[item.EntryID] = item
        target._attach(item)
        return item

    def delete(self, item: FakeMailItem) -> None:
        if item._folder is not None:
            item._folder._items.remove(item)
        item._folder = None

    def fire_new_mail(self, entry_ids: Sequence[str]) -> None:
        text = ",".join(entry_ids)

        def deliver() -> None:
            for handler in list(self.root.handlers):
                handler.OnNewMailEx(text)

        if self.root.com_api is not None:
            self.root.com_api.post(deliver)
        else:
            deliver()

    def deliver_outbox(self, account: FakeAccount) -> int:
        outbox = self.folder(account, "outbox")
        sent = self.folder(account, "sent")
        moved = 0
        for item in list(outbox._items):
            sent._attach(item)
            moved += 1
        return moved

    def items_in(self, folder: FakeFolder) -> list[FakeMailItem]:
        return list(folder._items)

    def set_running(self, running: bool) -> None:
        self.root.running = running

    def set_offline(self, offline: bool) -> None:
        self.root.offline = offline

    @property
    def send_calls(self) -> list[str]:
        return self.root.send_calls

    @property
    def violations(self) -> list[str]:
        return self.root.violations


# =============================================================================================
# Fake backend (mailbox-worker API)
# =============================================================================================


@dataclass
class _Fault:
    status: int | None = None
    exception: type[Exception] | None = None
    code: str | None = None
    retry_after: int | None = None


class FakeBackend:
    """Mailbox-worker API with the backend's validation and idempotency semantics."""

    def __init__(self, *, mailbox_binding_id: UUID, token: str, clock: Callable[[], datetime]) -> None:
        self.mailbox = mailbox_binding_id
        self.tokens = {token}
        self.clock = clock
        self.binding_log: list[dict[str, Any]] = []
        self.binding_versions: dict[UUID, int] = {}
        self.binding_states: dict[UUID, InquiryBindingState] = {}
        self.by_dedup: dict[str, StoredReplyIngest] = {}
        self.by_idempotency: dict[str, StoredReplyIngest] = {}
        self.stored_replies: dict[UUID, dict[str, Any]] = {}
        self.reply_posts: list[dict[str, Any]] = []
        self.reply_headers: list[dict[str, str]] = []
        self.locator_history: list[tuple[UUID, str | None]] = []
        self.requests: list[tuple[str, str]] = []
        self.faults: dict[str, list[_Fault]] = {}
        self.down = False
        self.forbid_worker = False
        self.store_then_timeout = 0
        self.intents: dict[UUID, WorkerSendIntent] = {}
        self.open_intents: list[UUID] = []
        #: Open intents the server lists with ``expired: true`` (reaped before any claim).
        self.expired_intents: set[UUID] = set()
        self.kill_switch = False
        self.claims: list[UUID] = []
        self.claim_refusal: str | None = None
        self.reports: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.account_reports: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ scenario helpers

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def publish_binding(
        self,
        inquiry_id: UUID,
        *,
        state: str = "active",
        outbound_message_ids: Sequence[str] = (),
        send_intent_message_ids: Sequence[str] = (),
        aliases: Sequence[str] = (),
        listing_references: Sequence[str] = (),
        listing_urls: Sequence[str] = (),
        provider: str = "outlook_local",
        mailbox_binding_id: UUID | None = None,
        version: int | None = None,
    ) -> int:
        number = version if version is not None else self.binding_versions.get(inquiry_id, 0) + 1
        item: dict[str, Any] = {
            "inquiry_id": str(inquiry_id),
            "binding_version": number,
            "mailbox_binding_id": str(mailbox_binding_id or self.mailbox),
            "state": state,
        }
        if state != "tombstoned":
            item.update(
                {
                    "provider": provider,
                    "outbound_message_ids": list(outbound_message_ids),
                    "send_intent_message_ids": list(send_intent_message_ids),
                    "verified_seller_aliases": list(aliases),
                    "listing_references": list(listing_references),
                    "listing_urls": list(listing_urls),
                }
            )
        self.binding_log.append(item)
        if (mailbox_binding_id or self.mailbox) == self.mailbox:
            self.binding_versions[inquiry_id] = max(number, self.binding_versions.get(inquiry_id, 0))
            self.binding_states[inquiry_id] = InquiryBindingState(state)
        return number

    def tombstone(self, inquiry_id: UUID) -> int:
        return self.publish_binding(inquiry_id, state="tombstoned")

    def fail(self, path_prefix: str, *faults: _Fault) -> None:
        self.faults.setdefault(path_prefix, []).extend(faults)

    @staticmethod
    def status(status: int, *, code: str | None = None, retry_after: int | None = None) -> _Fault:
        return _Fault(status=status, code=code, retry_after=retry_after)

    @staticmethod
    def raise_(exception: type[Exception]) -> _Fault:
        return _Fault(exception=exception)

    def add_intent(self, intent: WorkerSendIntent) -> None:
        self.intents[intent.intent_id] = intent
        self.open_intents.append(intent.intent_id)

    def expire_intent(self, intent_id: UUID) -> None:
        """List an open intent as ``expired`` (the backend reaped it before any worker claimed it)."""
        self.expired_intents.add(intent_id)

    def revoke_token(self, token: str) -> None:
        self.tokens.discard(token)

    # ------------------------------------------------------------------ HTTP

    def _error(self, status: int, code: str, retry_after: int | None = None) -> httpx.Response:
        body = {
            "schema_version": "1.0",
            "request_id": f"req-{uuid.uuid4().hex[:12]}",
            "as_of": self.clock().isoformat(),
            "error": {
                "code": code,
                "message": code.lower(),
                "retryable": status in (429, 503),
                "retry_after_seconds": retry_after,
                "correlation_id": None,
            },
        }
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return httpx.Response(status, json=body, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if self.down:
            raise httpx.ConnectError("backend unreachable", request=request)
        for prefix, faults in self.faults.items():
            if path.startswith(prefix) and faults:
                fault = faults.pop(0)
                if fault.exception is not None:
                    raise fault.exception("injected fault")
                assert fault.status is not None
                return self._error(fault.status, fault.code or "INJECTED", fault.retry_after)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in self.tokens:
            return self._error(401, "UNAUTHENTICATED")
        if self.forbid_worker:
            return self._error(403, "FORBIDDEN")
        if request.method == "GET" and path == "/v1/mail-workers/inquiry-bindings":
            return self._bindings(request)
        if request.method == "POST" and path == "/v1/mail-workers/replies":
            return self._reply(request)
        if request.method == "GET" and path == "/v1/mail-workers/send-intents":
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.0",
                    "intents": [
                        {
                            **json.loads(self.intents[i].model_dump_json()),
                            "expired": i in self.expired_intents,
                        }
                        for i in self.open_intents
                    ],
                    "kill_switch_active": self.kill_switch,
                },
            )
        if request.method == "POST" and path.endswith("/claim"):
            intent_id = UUID(path.split("/")[-2])
            self.claims.append(intent_id)
            refusal = "kill_switch" if self.kill_switch else self.claim_refusal
            if intent_id not in self.open_intents and refusal is None:
                refusal = "intent_invalid"
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.0",
                    "intent_id": str(intent_id),
                    "proceed": refusal is None,
                    "refusal_reason": refusal,
                },
            )
        if request.method == "POST" and path.endswith("/report"):
            report = json.loads(request.content)
            self.reports.append(report)
            intent_id = UUID(report["intent_id"])
            if intent_id in self.open_intents:
                self.open_intents.remove(intent_id)
            return httpx.Response(200, json={"schema_version": "1.0", "accepted": True})
        if request.method == "POST" and path == "/v1/mail-workers/heartbeat":
            self.heartbeats.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.0",
                    "received_at": self.clock().isoformat(),
                    "downstream": {"slack_signal": "unverified", "mcp": "ok"},
                },
            )
        if request.method == "POST" and path == "/v1/mail-workers/account-report":
            self.account_reports.append(json.loads(request.content))
            return httpx.Response(200, json={"schema_version": "1.0", "accepted": True})
        return self._error(404, "NOT_FOUND")

    def _bindings(self, request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        limit = int(request.url.params.get("limit", "100"))
        start = int(cursor[1:]) if cursor else 0
        items = self.binding_log[start : start + limit]
        end = start + len(items)
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "items": items,
                "next_cursor": f"c{end}",
                "has_more": end < len(self.binding_log),
            },
        )

    def _reply(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        body = json.loads(request.content)
        self.reply_posts.append(body)
        self.reply_headers.append(dict(request.headers))
        if not key:
            return self._error(400, "VALIDATION_ERROR")
        try:
            ingest = ReplyUpload.model_validate(body)
        except ValidationError:
            return self._error(422, "VALIDATION_ERROR")
        if ingest.mailbox_binding_id != self.mailbox:
            return self._error(403, "FORBIDDEN")
        state = self.binding_states.get(ingest.inquiry_id)
        if state is None or state == InquiryBindingState.TOMBSTONED:
            return self._error(403, "FORBIDDEN")
        if ingest.binding_version > self.binding_versions.get(ingest.inquiry_id, 0):
            return self._error(409, "VERSION_CONFLICT")
        dedup = ingest.dedup_key().as_string()
        fingerprint = ingest.fingerprint()
        locator = ingest.locator()
        decision = decide_ingest(
            dedup_key=dedup,
            idempotency_key=key,
            fingerprint=fingerprint,
            locator=locator,
            existing_by_dedup_key=self.by_dedup.get(dedup),
            existing_by_idempotency_key=self.by_idempotency.get(key),
        )
        if decision.kind.value == "conflict":
            return self._error(409, "IDEMPOTENCY_CONFLICT")
        if decision.kind.value == "new":
            reply_id = uuid.uuid4()
            stored = StoredReplyIngest(
                reply_id=reply_id,
                dedup_key=dedup,
                idempotency_key=key,
                fingerprint=fingerprint,
                locators=(locator,) if locator else (),
            )
            self.by_dedup[dedup] = stored
            self.by_idempotency[key] = stored
            self.stored_replies[reply_id] = body
        else:
            assert decision.reply_id is not None
            reply_id = decision.reply_id
            if decision.locator_changed and decision.record_locator is not None:
                existing = self.by_dedup[dedup]
                updated = existing.model_copy(
                    update={"locators": (*existing.locators, decision.record_locator)}
                )
                self.by_dedup[dedup] = updated
                self.by_idempotency[existing.idempotency_key] = updated
                self.locator_history.append((reply_id, decision.record_locator.outlook_entry_id))
        if self.store_then_timeout > 0:
            self.store_then_timeout -= 1
            raise httpx.ReadTimeout("acknowledgement lost", request=request)
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "reply_id": str(reply_id),
                "inquiry_id": str(ingest.inquiry_id),
                "ingest_status": "quarantined" if ingest.correlation_status == "quarantined" else "stored",
                "duplicate": decision.duplicate,
                "request_id": f"req-{uuid.uuid4().hex[:12]}",
                "ingested_at": self.clock().isoformat(),
            },
        )


# =============================================================================================
# Builders
# =============================================================================================


def inquiry_message_id(inquiry_id: UUID, attempt: int = 1, domain: str = "sender.example.invalid") -> str:
    return f"<inquiry-{inquiry_id}.{attempt}@{domain}>"


SYNTHETIC_LISTING_URL: Final = "https://listing.example.invalid/ad/1234"
SYNTHETIC_SENDER_NAME: Final = "Synthetic Sender"


def rendered_inquiry(
    template_id: str = "seller_initial_de_v1",
    *,
    make: str = "Synthetic",
    model: str = "SUV",
    listing_reference: str = "REF-1234",
    listing_url: str = SYNTHETIC_LISTING_URL,
    sender_display_name: str = SYNTHETIC_SENDER_NAME,
) -> tuple[str, str]:
    """``(subject, body)`` of an exact spec 37.4 template rendering (shared domain renderer)."""
    message = render(
        template_id,
        build_vehicle_label(make, model),
        listing_reference,
        listing_url,
        sender_display_name,
        verified_listing_url=listing_url,
    )
    return message.subject, message.body


def make_intent(
    *,
    inquiry_id: UUID,
    mailbox_binding_id: UUID,
    from_address: str,
    to_address: str = "seller@dealer.example.invalid",
    created_at: datetime,
    ttl: timedelta = timedelta(hours=6),
    subject: str | None = None,
    body: str | None = None,
    from_display_name: str = SYNTHETIC_SENDER_NAME,
    attempt_number: int = 1,
    intent_id: UUID | None = None,
    body_hash: str | None = None,
) -> WorkerSendIntent:
    """A synthetic send intent; subject/body default to the exact German template rendering."""
    default_subject, default_body = rendered_inquiry()
    subject = default_subject if subject is None else subject
    body = default_body if body is None else body
    return WorkerSendIntent(
        intent_id=intent_id or uuid.uuid4(),
        inquiry_id=inquiry_id,
        attempt_number=attempt_number,
        idempotency_key=f"send-{inquiry_id}-{attempt_number}",
        mailbox_binding_id=mailbox_binding_id,
        binding_id=uuid.uuid4(),
        binding_version=1,
        account_id=from_address,
        from_address=from_address,
        from_display_name=from_display_name,
        to_address=to_address,
        subject=subject,
        body_text=body,
        rfc_message_id=inquiry_message_id(inquiry_id, attempt_number),
        inquiry_ref=f"inquiry-{inquiry_id}",
        body_hash=body_hash or message_body_hash(subject, body),
        mime_sha256="a" * 64,
        created_at=created_at,
        not_after=created_at + ttl,
    )


__all__ = [
    "INTERACTIVE_SESSION",
    "SYNTHETIC_LISTING_URL",
    "SYNTHETIC_SENDER_NAME",
    "AttachmentSpec",
    "FakeAccount",
    "FakeBackend",
    "FakeComApi",
    "FakeComError",
    "FakeFolder",
    "FakeMailItem",
    "FakeOutlook",
    "inquiry_message_id",
    "make_intent",
    "rendered_inquiry",
]
