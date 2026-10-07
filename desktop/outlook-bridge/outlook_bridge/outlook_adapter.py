"""Outlook Object Model adapter (spec 37.6): scoped folder access, item copies and sending.

``OutlookComSession`` talks to classic Outlook through COM and must only be used on the STA
thread; ``StaMailbox`` is the thread-safe facade the worker uses, marshalling every call through
``StaExecutor`` and returning plain dataclasses only.

Scope and privacy rules:

- Only the configured account (matched by SMTP address among ``Session.Accounts``) and only its
  delivery store are used. Folders are the store's default Inbox/Junk folders and explicitly
  configured rule-target paths below that store's root; Sent Items/Outbox are used only as send
  evidence. Other accounts and stores are never enumerated; an item outside the configured
  folders (for example a ``NewMailEx`` notification of another account) is ignored *before* its
  content is read.
- ``NewMailEx`` is only a prompt: the handler copies the comma-separated EntryIDs (plain
  strings) and returns; reconciliation is the source of truth.
- Items are identified by the Internet Message-ID (``PR_INTERNET_MESSAGE_ID`` through
  ``PropertyAccessor``); EntryID/StoreID are secondary locators that may change after a move.
  ``In-Reply-To``/``References`` come from ``PR_TRANSPORT_MESSAGE_HEADERS`` with the MAPI
  ``PR_IN_REPLY_TO_ID``/``PR_INTERNET_REFERENCES`` properties as fallback. Received times use
  ``PR_MESSAGE_DELIVERY_TIME`` (UTC through ``PropertyAccessor``).
- Meetings, sharing invitations and other non-mail items are skipped by the caller using the
  shared ``is_processable_item`` rule before any reply processing.
- Sending (``outlook_local`` intents) creates exactly one plain-text ``MailItem`` with one
  ``To`` recipient, the bound ``SendUsingAccount`` (verified by reading it back), no CC/BCC and
  no attachments, then calls ``.Send``. A successful call is only *local submission* (Outbox);
  Sent Items evidence is looked up separately and never fabricated.
- Nothing here changes Outlook, Trust Center or antivirus settings; an Outlook security prompt
  is left to the user and a refused/failed ``.Send`` is reported as uncertain.
"""

from __future__ import annotations

import email.header
import email.parser
import email.policy
import hashlib
import mimetypes
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from uuid import uuid4

from outlook_bridge.config import FolderSpec
from outlook_bridge.errors import FolderScopeError, OutlookUnavailable
from outlook_bridge.sta_runtime import StaExecutor
from outlook_bridge.wire import AccountType, WorkerSendIntent

# --------------------------------------------------------------------------------------------
# Outlook constants (documented OOM enumeration values)
# --------------------------------------------------------------------------------------------
OL_MAIL_ITEM: Final = 0
OL_FOLDER_OUTBOX: Final = 4
OL_FOLDER_SENT_MAIL: Final = 5
OL_FOLDER_INBOX: Final = 6
OL_FOLDER_JUNK: Final = 23
OL_TO: Final = 1
OL_FORMAT_PLAIN: Final = 1
OL_DISCARD: Final = 1
OL_BY_VALUE: Final = 1
OL_BY_REFERENCE: Final = 4
OL_EMBEDDED_ITEM: Final = 5
OL_OLE: Final = 6
DISPID_SEND_USING_ACCOUNT: Final = 64209
DISPATCH_PROPERTYPUTREF: Final = 8

_PROPTAG: Final = "http://schemas.microsoft.com/mapi/proptag/"
PR_INTERNET_MESSAGE_ID: Final = _PROPTAG + "0x1035001F"
PR_TRANSPORT_MESSAGE_HEADERS: Final = _PROPTAG + "0x007D001F"
PR_IN_REPLY_TO_ID: Final = _PROPTAG + "0x1042001F"
PR_INTERNET_REFERENCES: Final = _PROPTAG + "0x1039001F"
PR_MESSAGE_DELIVERY_TIME: Final = _PROPTAG + "0x0E060040"
PR_CLIENT_SUBMIT_TIME: Final = _PROPTAG + "0x00390040"
PR_SMTP_ADDRESS: Final = _PROPTAG + "0x39FE001F"
PR_ATTACH_MIME_TAG: Final = _PROPTAG + "0x370E001F"
PR_ATTACHMENT_HIDDEN: Final = _PROPTAG + "0x7FFE000B"
#: ``X-SUV-Inquiry-Ref`` as an internet header named property (PS_INTERNET_HEADERS).
INQUIRY_REF_PROPERTY: Final = (
    "http://schemas.microsoft.com/mapi/string/{00020386-0000-0000-C000-000000000046}/X-SUV-Inquiry-Ref"
)

MAX_HEADER_BLOCK_CHARS: Final = 256 * 1024
MAX_BODY_CHARS: Final = 1024 * 1024
MAX_ATTACHMENTS_READ: Final = 200
MAX_EVENT_IDS: Final = 500
MAX_SENT_LOOKUP_ITEMS: Final = 500
_NO_DATE_YEAR: Final = 4500  # Outlook encodes "none" dates as 4501-01-01

FolderRole = Literal["inbox", "junk", "rule_target", "sent_items", "outbox"]
AttachmentKind = Literal["file", "embedded_message", "ole", "link", "other"]
_ACCOUNT_TYPES: Final[dict[int, AccountType]] = {0: "exchange", 1: "imap", 2: "pop3", 3: "http", 4: "other", 5: "other"}
_ATTACHMENT_KINDS: Final[dict[int, AttachmentKind]] = {
    OL_BY_VALUE: "file",
    OL_BY_REFERENCE: "link",
    OL_EMBEDDED_ITEM: "embedded_message",
    OL_OLE: "ole",
}


def identity_hash(kind: str, *parts: str) -> str:
    """Hashed identity for reporting store/folder ids to the backend (never the raw ids)."""
    material = "\x00".join((kind, *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


# --------------------------------------------------------------------------------------------
# Plain data copied off the STA thread
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountInfo:
    smtp_address: str
    display_name: str | None
    account_type: AccountType
    stable_account_key: str
    store_id_hash: str
    outlook_version: str | None


@dataclass(frozen=True, slots=True)
class FolderRef:
    role: FolderRole
    store_id: str
    entry_id: str
    display_path: str  # local diagnostics only; never uploaded

    @property
    def store_id_hash(self) -> str:
        return identity_hash("store", self.store_id)

    @property
    def folder_id_hash(self) -> str:
        return identity_hash("folder", self.store_id, self.entry_id)

    @property
    def key(self) -> str:
        return self.folder_id_hash


@dataclass(frozen=True, slots=True)
class ItemRef:
    entry_id: str
    store_id: str
    folder_entry_id: str
    received_at: datetime | None
    message_class: str | None


@dataclass(frozen=True, slots=True)
class ItemListing:
    refs: tuple[ItemRef, ...]
    truncated: bool
    unreadable: int


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    index: int  # 0-based position in ``MailItem.Attachments`` (COM ``Item(index + 1)``)
    filename: str
    mime_type: str
    byte_size: int
    kind: AttachmentKind


@dataclass(frozen=True, slots=True)
class AttachmentDigest:
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class MailSnapshot:
    """A plain copy of one mail item, made on the STA thread (local only)."""

    ref: ItemRef
    internet_message_id: str | None
    headers: dict[str, tuple[str, ...]]
    subject: str
    sender_smtp: str | None
    body_text: str
    attachments: tuple[AttachmentInfo, ...]
    body_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionState:
    outlook_running: bool
    connected: bool | None
    offline: bool | None = None
    exchange_mode: int | None = None
    last_send_receive_end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    intent_id: str
    from_address: str
    to_address: str
    reply_to_address: str | None
    subject: str
    body_text: str
    rfc_message_id: str
    inquiry_ref: str

    @classmethod
    def from_intent(cls, intent: WorkerSendIntent) -> OutgoingMail:
        return cls(
            intent_id=str(intent.intent_id),
            from_address=intent.from_address,
            to_address=intent.to_address,
            reply_to_address=intent.reply_to_address,
            subject=intent.subject,
            body_text=intent.body_text,
            rfc_message_id=intent.rfc_message_id,
            inquiry_ref=intent.inquiry_ref,
        )


@dataclass(frozen=True, slots=True)
class SubmitResult:
    outcome: Literal["refused", "send_call_failed", "submitted"]
    refusal: Literal["account_mismatch", "intent_invalid", "mailbox_unavailable"] | None = None
    error_code: str | None = None
    account_used: str | None = None
    message_id_property_set: bool = False


@dataclass(frozen=True, slots=True)
class SentLookup:
    location: Literal["sent_items", "outbox", "not_found"]
    internet_message_id: str | None = None
    sent_at: datetime | None = None
    matches: int = 0


# --------------------------------------------------------------------------------------------
# Interface used by the worker
# --------------------------------------------------------------------------------------------


class MailboxAdapter(Protocol):
    """Thread-safe mailbox operations; every value returned is plain data."""

    def connect(self, account_smtp: str) -> AccountInfo: ...

    def resolve_folders(self, specs: Sequence[FolderSpec]) -> tuple[FolderRef, ...]: ...

    def list_items(self, folder: FolderRef, *, since: datetime, max_items: int) -> ItemListing: ...

    def locate_item(self, entry_id: str, store_id: str | None) -> ItemRef | None: ...

    def read_item(self, ref: ItemRef) -> MailSnapshot | None: ...

    def find_by_internet_message_id(self, internet_message_id: str) -> ItemRef | None: ...

    def attachment_digests(self, ref: ItemRef, indices: Sequence[int]) -> dict[int, AttachmentDigest]: ...

    def subscribe_new_mail(self, sink: Callable[[tuple[str, ...]], None]) -> None: ...

    def connection_state(self) -> ConnectionState: ...

    def submit(self, mail: OutgoingMail) -> SubmitResult: ...

    def lookup_sent(self, mail: OutgoingMail, *, since: datetime) -> SentLookup: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------------------------
# Small COM helpers (tolerant reads; COM raises for missing properties)
# --------------------------------------------------------------------------------------------


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _prop(accessor: Any, tag: str) -> Any:
    if accessor is None:
        return None
    try:
        return accessor.GetProperty(tag)
    except Exception:
        return None


def _str(value: Any, limit: int = 4096) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip("\x00")
    return text[:limit] if text else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    return bool(value) if value is not None else None


def com_time_as_utc(value: Any) -> datetime | None:
    """A ``PropertyAccessor`` PT_SYSTIME value: its wall-clock fields are UTC."""
    if not isinstance(value, datetime) or value.year >= _NO_DATE_YEAR:
        return None
    return datetime(
        value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond, tzinfo=UTC
    )


def com_time_local(value: Any) -> datetime | None:
    """An OOM date property (local time; pywin32 may or may not attach a zone)."""
    if not isinstance(value, datetime) or value.year >= _NO_DATE_YEAR:
        return None
    return value.astimezone(UTC)


def _decode_header_value(raw: str) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return raw


def parse_header_block(raw: str | None) -> dict[str, tuple[str, ...]]:
    """Untrusted RFC 5322 header block -> lower-case name -> decoded values (bounded)."""
    if not raw:
        return {}
    text = raw[:MAX_HEADER_BLOCK_CHARS]
    try:
        message = email.parser.HeaderParser(policy=email.policy.compat32).parsestr(text, headersonly=True)
    except Exception:
        return {}
    headers: dict[str, list[str]] = {}
    for name, value in message.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        key = name.strip().lower()
        if not key or (key not in headers and len(headers) >= 200):
            continue
        bucket = headers.setdefault(key, [])
        if len(bucket) < 50:
            bucket.append(_decode_header_value(value))
    return {k: tuple(v) for k, v in headers.items()}


def _guess_mime(filename: str, kind: AttachmentKind) -> str:
    if kind == "embedded_message":
        return "message/rfc822"
    guessed, _encoding = mimetypes.guess_type(filename, strict=False)
    return guessed or "application/octet-stream"


# --------------------------------------------------------------------------------------------
# Event sinks (instantiated by pywin32 ``WithEvents``; called on the STA thread)
# --------------------------------------------------------------------------------------------


class NewMailEventHandler:
    """``Application.NewMailEx`` sink: copies EntryIDs (plain strings) and returns at once."""

    _bridge_sink: Callable[[tuple[str, ...]], None] | None = None

    def OnNewMailEx(self, received_items_ids: object) -> None:  # noqa: N802 - COM event name
        sink = self._bridge_sink
        if sink is None:
            return
        text = received_items_ids if isinstance(received_items_ids, str) else str(received_items_ids)
        ids = tuple(part.strip() for part in text[:65536].split(",") if part.strip())[:MAX_EVENT_IDS]
        if ids:
            sink(ids)


class SyncEventHandler:
    """``SyncObject.SyncEnd`` sink: records the end time of a Send/Receive group."""

    _bridge_on_end: Callable[[], None] | None = None

    def OnSyncEnd(self) -> None:  # noqa: N802 - COM event name
        callback = self._bridge_on_end
        if callback is not None:
            callback()


# --------------------------------------------------------------------------------------------
# COM session (STA thread only)
# --------------------------------------------------------------------------------------------


@dataclass
class _SessionState:
    app: Any = None
    account: Any = None
    account_smtp: str | None = None
    store_id: str | None = None
    folders: dict[str, FolderRef] = field(default_factory=dict)
    sent_entry_id: str | None = None
    outbox_entry_id: str | None = None
    events: list[Any] = field(default_factory=list)
    last_sync_end: datetime | None = None


class OutlookComSession:
    """All Outlook Object Model access. Must only be called on the STA thread."""

    def __init__(
        self,
        *,
        attach: Callable[[], Any],
        bind_events: Callable[[Any, type], Any],
        temp_dir: Path,
        now: Callable[[], datetime],
    ) -> None:
        self._attach = attach
        self._bind_events = bind_events
        self._temp_dir = temp_dir
        self._now = now
        self._state = _SessionState()

    # ------------------------------------------------------------------ connection

    def _application(self) -> Any:
        if self._state.app is None:
            try:
                self._state.app = self._attach()
            except Exception:
                raise OutlookUnavailable("classic Outlook is not running in this user session") from None
        return self._state.app

    def _namespace(self) -> Any:
        try:
            return self._application().Session
        except OutlookUnavailable:
            raise
        except Exception:
            self._reset()
            raise OutlookUnavailable("Outlook session is not available") from None

    def _reset(self) -> None:
        self._state.app = None

    def _require_store_id(self) -> str:
        if self._state.store_id is None:
            raise OutlookUnavailable("the configured account is not connected")
        return self._state.store_id

    def connect(self, account_smtp: str) -> AccountInfo:
        app = self._application()
        namespace = self._namespace()
        try:
            accounts = namespace.Accounts
            count = _int(accounts.Count) or 0
            found = []
            for index in range(1, count + 1):
                account = accounts.Item(index)
                smtp = _str(_get(account, "SmtpAddress"), 320)
                if smtp is not None and smtp.casefold() == account_smtp.casefold():
                    found.append(account)
        except Exception:
            self._reset()
            raise OutlookUnavailable("cannot enumerate Outlook accounts") from None
        if len(found) != 1:
            raise OutlookUnavailable("the configured account is not (uniquely) present in the Outlook profile")
        account = found[0]
        store = _get(account, "DeliveryStore")
        store_id = _str(_get(store, "StoreID"), 16384)
        if store_id is None:
            raise OutlookUnavailable("the configured account has no delivery store")
        self._state.account = account
        self._state.account_smtp = _str(_get(account, "SmtpAddress"), 320)
        self._state.store_id = store_id
        self._subscribe_sync(namespace)
        version = _str(_get(app, "Version"), 64)
        return AccountInfo(
            smtp_address=self._state.account_smtp or account_smtp,
            display_name=_str(_get(account, "DisplayName"), 256),
            account_type=_ACCOUNT_TYPES.get(_int(_get(account, "AccountType")) or -1, "unknown"),
            stable_account_key="store:" + identity_hash("store", store_id)[:40],
            store_id_hash=identity_hash("store", store_id),
            outlook_version=version,
        )

    def _store(self) -> Any:
        account = self._state.account
        store = _get(account, "DeliveryStore")
        if store is None or _str(_get(store, "StoreID"), 16384) != self._require_store_id():
            raise OutlookUnavailable("the configured account's store is not available")
        return store

    def _subscribe_sync(self, namespace: Any) -> None:
        try:
            sync_objects = namespace.SyncObjects
            count = _int(sync_objects.Count) or 0
        except Exception:
            return
        for index in range(1, min(count, 20) + 1):
            try:
                handler = self._bind_events(sync_objects.Item(index), SyncEventHandler)
            except Exception:  # noqa: S112 - sync events are optional diagnostics
                continue
            handler._bridge_on_end = self._on_sync_end
            self._state.events.append(handler)

    def _on_sync_end(self) -> None:
        self._state.last_sync_end = self._now()

    # ------------------------------------------------------------------ folders

    def resolve_folders(self, specs: Sequence[FolderSpec]) -> tuple[FolderRef, ...]:
        store = self._store()
        store_id = self._require_store_id()
        refs: list[FolderRef] = []
        for spec in specs:
            if spec.role == "inbox":
                folder = self._default_folder(store, OL_FOLDER_INBOX)
            elif spec.role == "junk":
                folder = self._default_folder(store, OL_FOLDER_JUNK)
            else:
                folder = self._folder_by_path(store, spec.segments())
            entry_id = _str(_get(folder, "EntryID"), 4096)
            if entry_id is None or _str(_get(folder, "StoreID"), 16384) != store_id:
                raise FolderScopeError("a configured folder is outside the configured account's store")
            if any(ref.entry_id == entry_id for ref in refs):
                raise FolderScopeError("two configured folders resolve to the same Outlook folder")
            refs.append(
                FolderRef(
                    role=spec.role,
                    store_id=store_id,
                    entry_id=entry_id,
                    display_path=_str(_get(folder, "FolderPath"), 1024) or spec.role,
                )
            )
        sent = self._default_folder(store, OL_FOLDER_SENT_MAIL)
        outbox = self._default_folder(store, OL_FOLDER_OUTBOX)
        self._state.sent_entry_id = _str(_get(sent, "EntryID"), 4096)
        self._state.outbox_entry_id = _str(_get(outbox, "EntryID"), 4096)
        self._state.folders = {ref.entry_id: ref for ref in refs}
        return tuple(refs)

    @staticmethod
    def _default_folder(store: Any, folder_type: int) -> Any:
        try:
            return store.GetDefaultFolder(folder_type)
        except Exception:
            raise FolderScopeError("a default folder of the configured store is not available") from None

    @staticmethod
    def _folder_by_path(store: Any, segments: Sequence[str]) -> Any:
        try:
            current = store.GetRootFolder()
        except Exception:
            raise FolderScopeError("the configured store has no root folder") from None
        for segment in segments:
            exact = None
            folded: list[Any] = []
            children = _get(current, "Folders")
            for index in range(1, (_int(_get(children, "Count")) or 0) + 1):
                try:
                    child = children.Item(index)
                except Exception:  # noqa: S112 - an unreadable sibling folder is skipped
                    continue
                name = _str(_get(child, "Name"), 512)
                if name == segment:
                    exact = child
                    break
                if name is not None and name.casefold() == segment.casefold():
                    folded.append(child)
            if exact is None and len(folded) != 1:
                raise FolderScopeError("a configured rule-target folder is missing or ambiguous")
            current = exact if exact is not None else folded[0]
        return current

    def _folder_object(self, folder: FolderRef) -> Any:
        if folder.store_id != self._require_store_id():
            raise FolderScopeError("folder belongs to another store")
        try:
            return self._namespace().GetFolderFromID(folder.entry_id, folder.store_id)
        except OutlookUnavailable:
            raise
        except Exception:
            raise FolderScopeError("a configured folder is no longer available") from None

    # ------------------------------------------------------------------ items

    def _received_at(self, item: Any) -> datetime | None:
        value = com_time_as_utc(_prop(_get(item, "PropertyAccessor"), PR_MESSAGE_DELIVERY_TIME))
        return value if value is not None else com_time_local(_get(item, "ReceivedTime"))

    def list_items(self, folder: FolderRef, *, since: datetime, max_items: int) -> ItemListing:
        """Items received at or after ``since`` (ascending), newest-first scan with early stop.

        Sorting by ``[ReceivedTime]`` avoids locale-dependent date filters; the loop stops at the
        first item older than ``since``.
        """
        target = self._folder_object(folder)
        try:
            items = target.Items
            items.Sort("[ReceivedTime]", True)
            item = items.GetFirst()
        except Exception:
            raise FolderScopeError("cannot list a configured folder") from None
        refs: list[ItemRef] = []
        truncated = False
        unreadable = 0
        while item is not None:
            received = self._received_at(item)
            if received is not None and received < since:
                break
            if len(refs) >= max_items:
                truncated = True
                break
            entry_id = _str(_get(item, "EntryID"), 4096)
            if entry_id is None:
                unreadable += 1
            else:
                refs.append(
                    ItemRef(
                        entry_id=entry_id,
                        store_id=folder.store_id,
                        folder_entry_id=folder.entry_id,
                        received_at=received,
                        message_class=_str(_get(item, "MessageClass"), 255),
                    )
                )
            try:
                item = items.GetNext()
            except Exception:
                truncated = True
                break
        refs.reverse()
        return ItemListing(refs=tuple(refs), truncated=truncated, unreadable=unreadable)

    def _item(self, entry_id: str) -> Any | None:
        try:
            return self._namespace().GetItemFromID(entry_id, self._require_store_id())
        except OutlookUnavailable:
            raise
        except Exception:
            return None

    def _scoped_ref(self, item: Any, fallback_entry_id: str) -> ItemRef | None:
        parent = _get(item, "Parent")
        parent_id = _str(_get(parent, "EntryID"), 4096)
        parent_store = _str(_get(parent, "StoreID"), 16384)
        store_id = self._require_store_id()
        if parent_id is None or parent_store != store_id or parent_id not in self._state.folders:
            return None
        return ItemRef(
            entry_id=_str(_get(item, "EntryID"), 4096) or fallback_entry_id,
            store_id=store_id,
            folder_entry_id=parent_id,
            received_at=self._received_at(item),
            message_class=_str(_get(item, "MessageClass"), 255),
        )

    def locate_item(self, entry_id: str, store_id: str | None) -> ItemRef | None:
        """Scope check without reading content: ``None`` unless the item is in a configured folder."""
        if store_id is not None and store_id != self._require_store_id():
            return None
        item = self._item(entry_id)
        return None if item is None else self._scoped_ref(item, entry_id)

    def read_item(self, ref: ItemRef) -> MailSnapshot | None:
        if ref.store_id != self._require_store_id():
            return None
        item = self._item(ref.entry_id)
        if item is None:
            return None
        scoped = self._scoped_ref(item, ref.entry_id)
        if scoped is None:
            return None  # moved out of the configured folders: never read
        return self._snapshot(item, scoped)

    def _snapshot(self, item: Any, ref: ItemRef) -> MailSnapshot:
        accessor = _get(item, "PropertyAccessor")
        internet_message_id = _str(_prop(accessor, PR_INTERNET_MESSAGE_ID), 998)
        headers = parse_header_block(_str(_prop(accessor, PR_TRANSPORT_MESSAGE_HEADERS), MAX_HEADER_BLOCK_CHARS))
        subject = _str(_get(item, "Subject"), 4096)
        sender = self._sender_smtp(item)
        if subject is not None:
            headers["subject"] = (subject,)
        if "from" not in headers and sender:
            headers["from"] = (sender,)
        if "message-id" not in headers and internet_message_id:
            headers["message-id"] = (internet_message_id,)
        if "in-reply-to" not in headers:
            in_reply_to = _str(_prop(accessor, PR_IN_REPLY_TO_ID), 16384)
            if in_reply_to:
                headers["in-reply-to"] = (in_reply_to,)
        if "references" not in headers:
            references = _str(_prop(accessor, PR_INTERNET_REFERENCES), 65536)
            if references:
                headers["references"] = (references,)
        body = _str(_get(item, "Body"), MAX_BODY_CHARS + 1) or ""
        truncated = len(body) > MAX_BODY_CHARS
        return MailSnapshot(
            ref=ref,
            internet_message_id=internet_message_id,
            headers=headers,
            subject=subject or "",
            sender_smtp=sender,
            body_text=body[:MAX_BODY_CHARS],
            attachments=self._attachments(item),
            body_truncated=truncated,
        )

    @staticmethod
    def _sender_smtp(item: Any) -> str | None:
        sender_type = _str(_get(item, "SenderEmailType"), 16)
        if sender_type is not None and sender_type.upper() == "EX":
            sender = _get(item, "Sender")
            try:
                user = sender.GetExchangeUser() if sender is not None else None
                address = _str(_get(user, "PrimarySmtpAddress"), 320)
            except Exception:
                address = None
            if address:
                return address
            return _str(_prop(_get(sender, "PropertyAccessor"), PR_SMTP_ADDRESS), 320)
        return _str(_get(item, "SenderEmailAddress"), 320)

    @staticmethod
    def _attachments(item: Any) -> tuple[AttachmentInfo, ...]:
        attachments = _get(item, "Attachments")
        count = _int(_get(attachments, "Count")) or 0
        result: list[AttachmentInfo] = []
        for position in range(1, min(count, MAX_ATTACHMENTS_READ) + 1):
            try:
                attachment = attachments.Item(position)
            except Exception:  # noqa: S112 - an unreadable attachment is skipped (never uploaded)
                continue
            accessor = _get(attachment, "PropertyAccessor")
            if bool(_prop(accessor, PR_ATTACHMENT_HIDDEN)):
                continue  # inline/hidden parts (signature images) are not documents
            kind = _ATTACHMENT_KINDS.get(_int(_get(attachment, "Type")) or -1, "other")
            name = _str(_get(attachment, "FileName"), 1024) or _str(_get(attachment, "DisplayName"), 1024)
            filename = name or "attachment"
            mime = _str(_prop(accessor, PR_ATTACH_MIME_TAG), 191) or _guess_mime(filename, kind)
            result.append(
                AttachmentInfo(
                    index=position - 1,
                    filename=filename,
                    mime_type=mime.lower(),
                    byte_size=max(0, _int(_get(attachment, "Size")) or 0),
                    kind=kind,
                )
            )
        return tuple(result)

    def find_by_internet_message_id(self, internet_message_id: str) -> ItemRef | None:
        """Re-find a moved item inside the configured folders (exact string comparison)."""
        literal = internet_message_id.replace("'", "''")
        query = f"@SQL=\"{PR_INTERNET_MESSAGE_ID}\" = '{literal}'"
        for folder in self._state.folders.values():
            try:
                found = self._folder_object(folder).Items.Find(query)
            except FolderScopeError:
                continue
            except Exception:  # noqa: S112 - a folder that cannot be searched is skipped
                continue
            if found is not None:
                ref = self._scoped_ref(found, _str(_get(found, "EntryID"), 4096) or "")
                if ref is not None:
                    return ref
        return None

    def attachment_digests(self, ref: ItemRef, indices: Sequence[int]) -> dict[int, AttachmentDigest]:
        """SHA-256 and exact size of permitted file attachments (temporary private copy)."""
        item = self._item(ref.entry_id)
        if item is None or self._scoped_ref(item, ref.entry_id) is None:
            return {}
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        attachments = _get(item, "Attachments")
        result: dict[int, AttachmentDigest] = {}
        for index in sorted(set(indices)):
            try:
                attachment = attachments.Item(index + 1)
            except Exception:  # noqa: S112 - a missing attachment simply gets no digest
                continue
            if _ATTACHMENT_KINDS.get(_int(_get(attachment, "Type")) or -1) != "file":
                continue
            path = self._temp_dir / f"att-{uuid4().hex}.bin"
            try:
                attachment.SaveAsFile(str(path))
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                result[index] = AttachmentDigest(sha256=digest.hexdigest(), byte_size=size)
            except Exception:  # noqa: S112 - no digest means the attachment is not uploaded
                continue
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return result

    # ------------------------------------------------------------------ events and health

    def subscribe_new_mail(self, sink: Callable[[tuple[str, ...]], None]) -> None:
        handler = self._bind_events(self._application(), NewMailEventHandler)
        handler._bridge_sink = sink
        self._state.events.append(handler)

    def connection_state(self) -> ConnectionState:
        try:
            namespace = self._namespace()
            offline = _bool(_get(namespace, "Offline"))
            mode = _int(_get(namespace, "ExchangeConnectionMode"))
        except OutlookUnavailable:
            return ConnectionState(
                outlook_running=False, connected=False, last_send_receive_end_at=self._state.last_sync_end
            )
        connected: bool | None
        if mode is not None and mode != 0:
            # olOffline=100, olCachedOffline=200, olDisconnected=300, olCachedDisconnected=400
            connected = mode >= 500
        else:
            connected = None if offline is None else not offline
        return ConnectionState(
            outlook_running=True,
            connected=connected,
            offline=offline,
            exchange_mode=mode,
            last_send_receive_end_at=self._state.last_sync_end,
        )

    # ------------------------------------------------------------------ sending

    def _account_for(self, address: str) -> Any | None:
        configured = self._state.account_smtp
        if configured is None or configured.casefold() != address.casefold():
            return None
        return self._state.account

    def submit(self, mail: OutgoingMail) -> SubmitResult:
        account = self._account_for(mail.from_address)
        if account is None:
            return SubmitResult(outcome="refused", refusal="account_mismatch", error_code="ACCOUNT_NOT_BOUND")
        try:
            item = self._application().CreateItem(OL_MAIL_ITEM)
        except Exception:
            return SubmitResult(outcome="refused", refusal="mailbox_unavailable", error_code="CREATE_ITEM_FAILED")
        used: str | None = None
        message_id_set = False
        try:
            self._set_send_account(item, account)
            used = _str(_get(_get(item, "SendUsingAccount"), "SmtpAddress"), 320)
            if used is None or used.casefold() != mail.from_address.casefold():
                self._discard(item)
                return SubmitResult(
                    outcome="refused", refusal="account_mismatch", error_code="SEND_ACCOUNT_NOT_SET", account_used=used
                )
            recipient = item.Recipients.Add(mail.to_address)
            recipient.Type = OL_TO
            resolved = bool(recipient.Resolve())
            if mail.reply_to_address is not None:
                item.ReplyRecipients.Add(mail.reply_to_address).Resolve()
            item.Subject = mail.subject
            item.BodyFormat = OL_FORMAT_PLAIN
            item.Body = mail.body_text.replace("\r\n", "\n").replace("\n", "\r\n")
            accessor = item.PropertyAccessor
            accessor.SetProperty(INQUIRY_REF_PROPERTY, mail.inquiry_ref)
            try:
                accessor.SetProperty(PR_INTERNET_MESSAGE_ID, mail.rfc_message_id)
                message_id_set = True
            except Exception:
                message_id_set = False  # Outlook may assign its own Message-ID; observed later
            problems = self._envelope_problems(item, mail, resolved=resolved)
            if problems:
                self._discard(item)
                return SubmitResult(
                    outcome="refused", refusal="intent_invalid", error_code=problems[0], account_used=used
                )
        except Exception as exc:
            self._discard(item)
            return SubmitResult(
                outcome="refused",
                refusal="mailbox_unavailable",
                error_code=f"PREPARE_{type(exc).__name__}"[:64],
                account_used=used,
            )
        try:
            item.Send()
        except Exception as exc:
            return SubmitResult(
                outcome="send_call_failed",
                error_code=f"SEND_{type(exc).__name__}"[:64],
                account_used=used,
                message_id_property_set=message_id_set,
            )
        return SubmitResult(outcome="submitted", account_used=used, message_id_property_set=message_id_set)

    @staticmethod
    def _set_send_account(item: Any, account: Any) -> None:
        try:
            item.SendUsingAccount = account
            return
        except Exception:  # noqa: S110 - late-bound pywin32 needs the PROPERTYPUTREF fallback
            pass
        item._oleobj_.Invoke(DISPID_SEND_USING_ACCOUNT, 0, DISPATCH_PROPERTYPUTREF, False, account)

    @staticmethod
    def _envelope_problems(item: Any, mail: OutgoingMail, *, resolved: bool) -> tuple[str, ...]:
        problems: list[str] = []
        if not resolved:
            problems.append("RECIPIENT_UNRESOLVED")
        if (_int(_get(_get(item, "Recipients"), "Count")) or 0) != 1:
            problems.append("EXTRA_RECIPIENT")
        if _str(_get(item, "CC")) or _str(_get(item, "BCC")):
            problems.append("CC_BCC")
        expected_reply = 1 if mail.reply_to_address is not None else 0
        if (_int(_get(_get(item, "ReplyRecipients"), "Count")) or 0) != expected_reply:
            problems.append("EXTRA_REPLY_TO")
        if (_int(_get(_get(item, "Attachments"), "Count")) or 0) != 0:
            problems.append("ATTACHMENT")
        if _get(item, "Subject") != mail.subject:
            problems.append("SUBJECT_CHANGED")
        if _int(_get(item, "BodyFormat")) != OL_FORMAT_PLAIN:
            problems.append("NOT_PLAIN_TEXT")
        return tuple(problems)

    @staticmethod
    def _discard(item: Any) -> None:
        try:
            item.Close(OL_DISCARD)
        except Exception:  # noqa: S110 - an unsaved item that cannot be closed is simply dropped
            pass

    def lookup_sent(self, mail: OutgoingMail, *, since: datetime) -> SentLookup:
        """Sent Items (then Outbox) evidence for an intent; never inferred from absence."""
        namespace = self._namespace()
        store_id = self._require_store_id()
        for location, entry_id in (
            ("sent_items", self._state.sent_entry_id),
            ("outbox", self._state.outbox_entry_id),
        ):
            if entry_id is None:
                continue
            try:
                items = namespace.GetFolderFromID(entry_id, store_id).Items
                if location == "sent_items":
                    items.Sort("[SentOn]", True)
                item = items.GetFirst()
            except Exception:  # noqa: S112 - an unreadable evidence folder yields no evidence
                continue
            matches: list[tuple[str | None, datetime | None]] = []
            seen = 0
            while item is not None and seen < MAX_SENT_LOOKUP_ITEMS:
                seen += 1
                accessor = _get(item, "PropertyAccessor")
                submitted = com_time_as_utc(_prop(accessor, PR_CLIENT_SUBMIT_TIME))
                if location == "sent_items" and submitted is not None and submitted < since:
                    break
                if self._is_our_message(item, accessor, mail):
                    matches.append((_str(_prop(accessor, PR_INTERNET_MESSAGE_ID), 998), submitted))
                try:
                    item = items.GetNext()
                except Exception:
                    break
            if matches:
                message_id, sent_at = matches[-1]  # earliest match after ``since``
                return SentLookup(
                    location="sent_items" if location == "sent_items" else "outbox",
                    internet_message_id=message_id,
                    sent_at=sent_at if location == "sent_items" else None,
                    matches=len(matches),
                )
        return SentLookup(location="not_found")

    @staticmethod
    def _is_our_message(item: Any, accessor: Any, mail: OutgoingMail) -> bool:
        message_id = _str(_prop(accessor, PR_INTERNET_MESSAGE_ID), 998)
        if message_id is not None and message_id.strip() == mail.rfc_message_id:
            return True
        if _str(_prop(accessor, INQUIRY_REF_PROPERTY), 128) != mail.inquiry_ref:
            return False
        if _get(item, "Subject") != mail.subject:
            return False
        recipients = _get(item, "Recipients")
        if (_int(_get(recipients, "Count")) or 0) != 1:
            return False
        try:
            recipient = recipients.Item(1)
        except Exception:
            return False
        address = _str(_prop(_get(recipient, "PropertyAccessor"), PR_SMTP_ADDRESS), 320) or _str(
            _get(recipient, "Address"), 320
        )
        return address is not None and address.casefold() == mail.to_address.casefold()

    # ------------------------------------------------------------------ shutdown

    def close(self) -> None:
        for handler in self._state.events:
            close = getattr(handler, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: S110 - disconnecting event sinks must not block shutdown
                    pass
            handler._bridge_sink = None
            handler._bridge_on_end = None
        self._state = _SessionState()


# --------------------------------------------------------------------------------------------
# Thread-safe facade
# --------------------------------------------------------------------------------------------


class StaMailbox:
    """``MailboxAdapter`` that marshals every call onto the STA thread."""

    def __init__(self, executor: StaExecutor, session: OutlookComSession, *, timeout: float = 120.0) -> None:
        self._executor = executor
        self._session = session
        self._timeout = timeout
        executor.add_shutdown_hook(session.close)

    def connect(self, account_smtp: str) -> AccountInfo:
        return self._executor.call(lambda: self._session.connect(account_smtp), timeout=self._timeout)

    def resolve_folders(self, specs: Sequence[FolderSpec]) -> tuple[FolderRef, ...]:
        return self._executor.call(lambda: self._session.resolve_folders(specs), timeout=self._timeout)

    def list_items(self, folder: FolderRef, *, since: datetime, max_items: int) -> ItemListing:
        return self._executor.call(
            lambda: self._session.list_items(folder, since=since, max_items=max_items), timeout=self._timeout
        )

    def locate_item(self, entry_id: str, store_id: str | None) -> ItemRef | None:
        return self._executor.call(lambda: self._session.locate_item(entry_id, store_id), timeout=self._timeout)

    def read_item(self, ref: ItemRef) -> MailSnapshot | None:
        return self._executor.call(lambda: self._session.read_item(ref), timeout=self._timeout)

    def find_by_internet_message_id(self, internet_message_id: str) -> ItemRef | None:
        return self._executor.call(
            lambda: self._session.find_by_internet_message_id(internet_message_id), timeout=self._timeout
        )

    def attachment_digests(self, ref: ItemRef, indices: Sequence[int]) -> dict[int, AttachmentDigest]:
        wanted = tuple(indices)
        return self._executor.call(lambda: self._session.attachment_digests(ref, wanted), timeout=self._timeout)

    def subscribe_new_mail(self, sink: Callable[[tuple[str, ...]], None]) -> None:
        self._executor.call(lambda: self._session.subscribe_new_mail(sink), timeout=self._timeout)

    def connection_state(self) -> ConnectionState:
        return self._executor.call(self._session.connection_state, timeout=self._timeout)

    def submit(self, mail: OutgoingMail) -> SubmitResult:
        return self._executor.call(lambda: self._session.submit(mail), timeout=self._timeout)

    def lookup_sent(self, mail: OutgoingMail, *, since: datetime) -> SentLookup:
        return self._executor.call(lambda: self._session.lookup_sent(mail, since=since), timeout=self._timeout)

    def close(self) -> None:
        if self._executor.running:
            self._executor.call(self._session.close, timeout=self._timeout)


def real_com_bindings(*, start_if_not_running: bool) -> tuple[Callable[[], Any], Callable[[Any, type], Any]]:
    """``(attach, bind_events)`` over pywin32 (imported lazily; Windows only).

    ``attach`` connects to the classic Outlook already running in this user session
    (``GetActiveObject``); it starts Outlook only when the configuration explicitly allows it.
    """

    def attach() -> Any:  # pragma: no cover - requires Windows
        import importlib

        client = importlib.import_module("win32com.client")
        try:
            return client.Dispatch(client.GetActiveObject("Outlook.Application"))
        except Exception:
            if not start_if_not_running:
                raise
            return client.Dispatch("Outlook.Application")

    def bind_events(obj: Any, handler: type) -> Any:  # pragma: no cover - requires Windows
        import importlib

        client = importlib.import_module("win32com.client")
        return client.WithEvents(obj, handler)

    return attach, bind_events


def private_temp_dir(data_dir: Path) -> Path:
    path = data_dir / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


__all__ = [
    "INQUIRY_REF_PROPERTY",
    "PR_INTERNET_MESSAGE_ID",
    "PR_TRANSPORT_MESSAGE_HEADERS",
    "AccountInfo",
    "AttachmentDigest",
    "AttachmentInfo",
    "ConnectionState",
    "FolderRef",
    "ItemListing",
    "ItemRef",
    "MailSnapshot",
    "MailboxAdapter",
    "NewMailEventHandler",
    "OutgoingMail",
    "OutlookComSession",
    "SentLookup",
    "StaMailbox",
    "SubmitResult",
    "SyncEventHandler",
    "com_time_as_utc",
    "com_time_local",
    "identity_hash",
    "parse_header_block",
    "private_temp_dir",
    "real_com_bindings",
]
