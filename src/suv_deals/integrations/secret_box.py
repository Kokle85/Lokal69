"""Authenticated encryption for secrets at rest (MCP Events callback secrets).

AES-256-GCM (`cryptography`), one random 96-bit nonce per seal. Envelope layout:

    byte 0      format version (0x01)
    byte 1      key id (1..255) of the key that sealed it
    bytes 2-13  nonce
    bytes 14-   ciphertext || 16-byte GCM tag

The associated data is bound to the caller's context (for subscription secrets:
workspace, subscription id and secret version) *and* to the envelope header, so
a ciphertext copied into another row, or with an edited key id, fails to open.

Key configuration (`MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY`):

- `<base64 of 32 bytes>`: a single key with key id 1, or
- `<id>:<base64>,<id>:<base64>,...`: a keyring. The FIRST entry is the current
  key used for new seals; the others remain decrypt-only. Rotate by prepending a
  new entry, re-wrapping stored rows (`needs_rewrap`/`rewrap`), then removing
  the old entry once nothing references it.

Plaintext is never logged, never placed in exception messages and never stored
in reprs.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from suv_deals.errors import AppError, ErrorCode
from suv_deals.settings import Settings

FORMAT_VERSION: Final = 1
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16
HEADER_BYTES: Final = 2
MAX_PLAINTEXT_BYTES: Final = 4096
MAX_AAD_BYTES: Final = 1024
TEXT_PREFIX: Final = "sbx1:"
_AAD_DOMAIN: Final = b"suv_deals/secret_box/v1\x00"
_MIN_ENVELOPE: Final = HEADER_BYTES + NONCE_BYTES + TAG_BYTES
_B64_STD = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64_URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_KEY_ENTRY = re.compile(r"^([0-9]{1,3}):(.+)$")


class SecretBoxError(AppError):
    """Base class; messages are generic and never include key or plaintext material."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, retryable=False)


class SecretBoxConfigError(SecretBoxError):
    pass


class SecretDecryptionFailed(SecretBoxError):
    def __init__(self, message: str = "Stored secret could not be decrypted") -> None:
        super().__init__(message)


def _decode_key(value: str) -> bytes:
    value = value.strip()
    raw: bytes | None = None
    padded = value + "=" * (-len(value) % 4)
    try:
        if _B64_STD.fullmatch(value):
            raw = base64.b64decode(padded, validate=True)
        elif _B64_URL.fullmatch(value):
            raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        raw = None
    if raw is None:
        raise SecretBoxConfigError("Encryption key is not valid base64")
    if len(raw) != KEY_BYTES:
        raise SecretBoxConfigError("Encryption key must decode to exactly 32 bytes")
    return raw


def parse_keyring(value: str) -> tuple[dict[int, bytes], int]:
    """Parse the configured key value into ({key_id: key}, current_key_id)."""
    text = value.strip()
    if not text:
        raise SecretBoxConfigError("Encryption key is empty")
    if "," not in text and ":" not in text:
        return {1: _decode_key(text)}, 1
    keys: dict[int, bytes] = {}
    current: int | None = None
    for entry in text.split(","):
        match = _KEY_ENTRY.fullmatch(entry.strip())
        if match is None:
            raise SecretBoxConfigError("Keyring entries must look like <id>:<base64>")
        key_id = int(match.group(1))
        if not 1 <= key_id <= 255:
            raise SecretBoxConfigError("Key ids must be between 1 and 255")
        if key_id in keys:
            raise SecretBoxConfigError("Duplicate key id in keyring")
        keys[key_id] = _decode_key(match.group(2))
        if current is None:
            current = key_id
    if current is None:  # pragma: no cover - guarded by the non-empty check above
        raise SecretBoxConfigError("Keyring is empty")
    return keys, current


def subscription_secret_aad(*, workspace_id: UUID, subscription_id: str, secret_version: int) -> bytes:
    """Associated data binding an encrypted callback secret to exactly one subscription row/version."""
    if not subscription_id or len(subscription_id) > 128 or "\x00" in subscription_id:
        raise ValueError("invalid subscription id for AAD")
    if secret_version < 1:
        raise ValueError("secret version must be >= 1")
    return f"mcp_event_subscription\x00{workspace_id}\x00{subscription_id}\x00{secret_version}".encode()


class SecretBox:
    def __init__(self, keys: Mapping[int, bytes], current_key_id: int) -> None:
        if not keys:
            raise SecretBoxConfigError("At least one encryption key is required")
        for key_id, key in keys.items():
            if not 1 <= key_id <= 255:
                raise SecretBoxConfigError("Key ids must be between 1 and 255")
            if len(key) != KEY_BYTES:
                raise SecretBoxConfigError("Encryption key must be exactly 32 bytes")
        if current_key_id not in keys:
            raise SecretBoxConfigError("Current key id is not in the keyring")
        self._ciphers = {key_id: AESGCM(bytes(key)) for key_id, key in keys.items()}
        self._current = current_key_id

    def __repr__(self) -> str:
        return f"SecretBox(current_key_id={self._current}, key_ids={sorted(self._ciphers)})"

    @classmethod
    def from_config_value(cls, value: str | SecretStr) -> SecretBox:
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        keys, current = parse_keyring(text)
        return cls(keys, current)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretBox:
        configured = settings.mcp_event_subscription_secret_encryption_key
        if configured is None or not configured.get_secret_value().strip():
            raise SecretBoxConfigError("MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY is not configured")
        return cls.from_config_value(configured)

    @property
    def current_key_id(self) -> int:
        return self._current

    @property
    def key_ids(self) -> frozenset[int]:
        return frozenset(self._ciphers)

    @staticmethod
    def _full_aad(header: bytes, aad: bytes) -> bytes:
        if not isinstance(aad, bytes) or not aad:
            raise ValueError("associated data is required")
        if len(aad) > MAX_AAD_BYTES:
            raise ValueError("associated data too long")
        return _AAD_DOMAIN + header + b"\x00" + aad

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if not plaintext or len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise ValueError("plaintext must be 1..4096 bytes")
        header = bytes((FORMAT_VERSION, self._current))
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._ciphers[self._current].encrypt(nonce, plaintext, self._full_aad(header, aad))
        return header + nonce + ciphertext

    @staticmethod
    def key_id_of(envelope: bytes) -> int:
        if len(envelope) < _MIN_ENVELOPE or envelope[0] != FORMAT_VERSION:
            raise SecretDecryptionFailed("Stored secret envelope is malformed")
        return envelope[1]

    def open(self, envelope: bytes, *, aad: bytes) -> bytes:
        if not isinstance(envelope, bytes | bytearray | memoryview):
            raise SecretDecryptionFailed("Stored secret envelope is malformed")
        data = bytes(envelope)
        key_id = self.key_id_of(data)
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise SecretDecryptionFailed("Stored secret uses a key that is not configured")
        header = data[:HEADER_BYTES]
        nonce = data[HEADER_BYTES : HEADER_BYTES + NONCE_BYTES]
        ciphertext = data[HEADER_BYTES + NONCE_BYTES :]
        try:
            return cipher.decrypt(nonce, ciphertext, self._full_aad(header, aad))
        except InvalidTag:
            raise SecretDecryptionFailed() from None

    def needs_rewrap(self, envelope: bytes) -> bool:
        return self.key_id_of(bytes(envelope)) != self._current

    def rewrap(self, envelope: bytes, *, aad: bytes) -> bytes:
        """Decrypt with whichever configured key sealed it and re-seal with the current key."""
        return self.seal(self.open(envelope, aad=aad), aad=aad)

    def seal_text(self, plaintext: str, *, aad: bytes) -> str:
        """Seal a text secret into a text-column-safe token (`sbx1:<base64url>`)."""
        envelope = self.seal(plaintext.encode("utf-8"), aad=aad)
        return TEXT_PREFIX + base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")

    def open_text(self, token: str, *, aad: bytes) -> SecretStr:
        if not isinstance(token, str) or not token.startswith(TEXT_PREFIX):
            raise SecretDecryptionFailed("Stored secret envelope is malformed")
        body = token[len(TEXT_PREFIX) :]
        if not _B64_URL.fullmatch(body):
            raise SecretDecryptionFailed("Stored secret envelope is malformed")
        try:
            envelope = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except (binascii.Error, ValueError):
            raise SecretDecryptionFailed("Stored secret envelope is malformed") from None
        plaintext = self.open(envelope, aad=aad)
        try:
            return SecretStr(plaintext.decode("utf-8"))
        except UnicodeDecodeError:
            raise SecretDecryptionFailed() from None
