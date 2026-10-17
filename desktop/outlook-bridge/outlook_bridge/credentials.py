"""Revocable, narrow mail-worker credential in the OS protected credential store (spec 37.8).

The desktop worker holds exactly one secret: the mailbox-bound ``mail:ingest`` credential issued
by the backend during secure activation. It is stored in Windows Credential Manager (generic
credential, per user, not roaming; via pywin32 ``win32cred``) and never in the configuration
file, the SQLite store, logs or the command line. Supabase service-role/secret keys, database
URLs and Supabase JWTs are refused outright - they must never be put on the desktop.

``CredentialManager`` tracks usability: a credential the server rejected (401/revoked) or one past
its known expiry stops all transmission until a *different* credential is stored; the local
backlog is kept meanwhile.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from outlook_bridge.errors import CredentialMissing, CredentialUnusable, ForbiddenCredentialKind
from suv_deals.clock import ensure_utc

MIN_TOKEN_CHARS: Final = 24
MAX_TOKEN_CHARS: Final = 4096
CRED_TYPE_GENERIC: Final = 1
CRED_PERSIST_LOCAL_MACHINE: Final = 2
_TOKEN_RE: Final = re.compile(r"^[\x21-\x7e]+$")
_FORBIDDEN_PREFIXES: Final = (
    "postgres://",
    "postgresql://",
    "sb_secret_",
    "sb_publishable_",
    "sbp_",
    "service_role",
)
_FORBIDDEN_JWT_ROLES: Final = frozenset(
    {"service_role", "supabase_admin", "postgres", "anon", "authenticated"}
)


class CredentialState(StrEnum):
    MISSING = "missing"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class WorkerCredential:
    """The ingest credential; ``repr`` never shows the token."""

    token: str = field(repr=False)
    expires_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """Non-reversible short identifier for diagnostics and rejection tracking."""
        return credential_fingerprint(self.token)

    def __repr__(self) -> str:
        return f"WorkerCredential(fingerprint={self.fingerprint}, expires_at={self.expires_at})"


def credential_fingerprint(token: str) -> str:
    """Non-reversible short identifier of a token (never the token itself)."""
    return hashlib.sha256(("worker-credential\x00" + token).encode("utf-8")).hexdigest()[:16]


def _jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def validate_worker_token(token: str) -> None:
    """Refuse anything that is not plausibly a narrow, opaque worker credential."""
    if not isinstance(token, str) or not MIN_TOKEN_CHARS <= len(token) <= MAX_TOKEN_CHARS:
        raise ForbiddenCredentialKind("the worker credential has an invalid length")
    if not _TOKEN_RE.fullmatch(token):
        raise ForbiddenCredentialKind("the worker credential must be printable ASCII without spaces")
    lowered = token.lower()
    if lowered.startswith(_FORBIDDEN_PREFIXES) or "service_role" in lowered:
        raise ForbiddenCredentialKind("Supabase/database credentials are never stored on the desktop worker")
    payload = _jwt_payload(token)
    if payload is not None:
        role = str(payload.get("role", "")).lower()
        issuer = str(payload.get("iss", "")).lower()
        if role in _FORBIDDEN_JWT_ROLES or "supabase" in issuer:
            raise ForbiddenCredentialKind("Supabase JWTs/keys are never stored on the desktop worker")


class CredentialStore(Protocol):
    """OS-protected secret storage (injectable for tests)."""

    def load(self) -> WorkerCredential | None: ...

    def save(self, credential: WorkerCredential) -> None: ...

    def delete(self) -> None: ...


class InMemoryCredentialStore:
    """Test/dry-run store; holds the credential only in process memory."""

    def __init__(self, credential: WorkerCredential | None = None) -> None:
        self._credential = credential

    def load(self) -> WorkerCredential | None:
        return self._credential

    def save(self, credential: WorkerCredential) -> None:
        validate_worker_token(credential.token)
        self._credential = credential

    def delete(self) -> None:
        self._credential = None


class WindowsCredentialManagerStore:  # pragma: no cover - requires Windows/pywin32
    """Generic credential in Windows Credential Manager (per user, this machine only)."""

    def __init__(self, target_name: str) -> None:
        self._target = target_name
        self._win32cred: Any = importlib.import_module("win32cred")

    def load(self) -> WorkerCredential | None:
        try:
            raw = self._win32cred.CredRead(self._target, CRED_TYPE_GENERIC)
        except Exception:
            return None
        blob = raw.get("CredentialBlob")
        if isinstance(blob, bytes):
            token = blob.decode("utf-16-le", errors="strict") if len(blob) % 2 == 0 else blob.decode("utf-8")
        else:
            token = str(blob or "")
        expires_at: datetime | None = None
        comment = raw.get("Comment") or ""
        if isinstance(comment, str) and comment.startswith("expires_at="):
            try:
                expires_at = ensure_utc(datetime.fromisoformat(comment.removeprefix("expires_at=")))
            except ValueError:
                expires_at = None
        return WorkerCredential(token=token, expires_at=expires_at) if token else None

    def save(self, credential: WorkerCredential) -> None:
        validate_worker_token(credential.token)
        self._win32cred.CredWrite(
            {
                "Type": CRED_TYPE_GENERIC,
                "TargetName": self._target,
                "UserName": "outlook-bridge",
                "CredentialBlob": credential.token,
                "Persist": CRED_PERSIST_LOCAL_MACHINE,
                "Comment": f"expires_at={credential.expires_at.isoformat()}" if credential.expires_at else "",
            },
            0,
        )

    def delete(self) -> None:
        with contextlib.suppress(Exception):  # deleting an absent credential is a no-op
            self._win32cred.CredDelete(self._target, CRED_TYPE_GENERIC)


class RejectionMemory(Protocol):
    """Where the fingerprint of a server-rejected credential is remembered (local store)."""

    def get_runtime(self, key: str) -> str | None: ...

    def set_runtime(self, key: str, value: str) -> None: ...


_REJECTED_KEY: Final = "credential_rejected_fingerprint"


class CredentialManager:
    """Usability gate in front of the credential store."""

    def __init__(self, store: CredentialStore, memory: RejectionMemory) -> None:
        self._store = store
        self._memory = memory

    def state(self, now: datetime) -> CredentialState:
        credential = self._store.load()
        if credential is None:
            return CredentialState.MISSING
        if self._memory.get_runtime(_REJECTED_KEY) == credential.fingerprint:
            return CredentialState.REJECTED
        if credential.expires_at is not None and ensure_utc(now) >= credential.expires_at:
            return CredentialState.EXPIRED
        return CredentialState.ACTIVE

    def require(self, now: datetime) -> WorkerCredential:
        """The usable credential, or ``CredentialMissing``/``CredentialUnusable`` (no transmission)."""
        state = self.state(now)
        if state == CredentialState.MISSING:
            raise CredentialMissing("no worker credential is stored; run 'credential set'")
        if state != CredentialState.ACTIVE:
            raise CredentialUnusable(f"the worker credential is {state.value}; transmission is stopped")
        credential = self._store.load()
        if credential is None:  # pragma: no cover - raced with deletion
            raise CredentialMissing("no worker credential is stored")
        return credential

    def token(self, now: datetime) -> str:
        return self.require(now).token

    def mark_rejected(self, fingerprint: str | None = None) -> bool:
        """Remember that the server rejected the stored credential (transmission stops).

        ``fingerprint`` names the credential the rejected request actually used; when the stored
        credential has been replaced meanwhile, the new one is not marked. Returns whether the
        stored credential is now marked as rejected.
        """
        credential = self._store.load()
        if credential is None or (fingerprint is not None and fingerprint != credential.fingerprint):
            return False
        self._memory.set_runtime(_REJECTED_KEY, credential.fingerprint)
        return True

    def replace(self, credential: WorkerCredential) -> None:
        validate_worker_token(credential.token)
        self._store.save(credential)

    def delete(self) -> None:
        self._store.delete()


__all__ = [
    "CredentialManager",
    "CredentialState",
    "CredentialStore",
    "InMemoryCredentialStore",
    "RejectionMemory",
    "WindowsCredentialManagerStore",
    "WorkerCredential",
    "credential_fingerprint",
    "validate_worker_token",
]
