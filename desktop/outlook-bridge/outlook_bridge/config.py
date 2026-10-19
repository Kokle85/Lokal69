"""Worker configuration (a TOML file in the user's protected application-data directory).

The configuration names exactly one mailbox binding, one Outlook account (by SMTP address) and the
folders of *that account's* store that may be scanned: the inbox, optionally the junk folder and
any folders that mailbox rules move mail into (``rule_target``, given as a path below the store
root such as ``"Inbox/Cars"``). Nothing else in the profile is ever enumerated. It never contains
a secret: the ingest credential lives in the operating system's protected credential store
(``credentials``).
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from outlook_bridge.errors import ConfigError

ReplyFolderRole = Literal["inbox", "junk", "rule_target"]
CONFIG_FILENAME: Final = "config.toml"
STORE_FILENAME: Final = "bridge-state.sqlite3"
APP_DIR_WINDOWS: Final = ("SUVDeals", "OutlookBridge")
APP_DIR_POSIX: Final = "suv-deals-outlook-bridge"
MAX_FOLDERS: Final = 20  # ops.mail_worker_bindings.folder_scope holds at most 20 folder hashes

_ADDRESS_RE: Final = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[A-Za-z0-9.-]{1,253}$")
_FOLDER_SEGMENT_FORBIDDEN: Final = re.compile(r"[\x00-\x1f\x7f\\/]")
_LOOPBACK_NAMES: Final = frozenset({"localhost"})


class FolderSpec(BaseModel):
    """One folder of the configured account's store that reply reconciliation may scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ReplyFolderRole
    path: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _path_rules(self) -> FolderSpec:
        if self.role == "rule_target":
            if not self.path:
                raise ValueError(
                    "a rule_target folder needs its path below the store root, e.g. 'Inbox/Cars'"
                )
            segments = self.path.split("/")
            if any(not s.strip() or s != s.strip() or _FOLDER_SEGMENT_FORBIDDEN.search(s) for s in segments):
                raise ValueError("folder path segments must be non-empty plain names separated by '/'")
            if any(s in {".", ".."} for s in segments) or len(segments) > 16:
                raise ValueError("folder path must not contain '.'/'..' segments or be deeper than 16 levels")
        elif self.path is not None:
            raise ValueError("inbox and junk are resolved as the account store's default folders; omit path")
        return self

    def segments(self) -> tuple[str, ...]:
        return tuple(self.path.split("/")) if self.path else ()


def _default_folders() -> tuple[FolderSpec, ...]:
    return (FolderSpec(role="inbox"), FolderSpec(role="junk"))


class BridgeConfig(BaseModel):
    """Validated worker configuration; see ``config.example.toml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    api_base_url: str = Field(min_length=8, max_length=512)
    allow_insecure_loopback: bool = False
    mailbox_binding_id: UUID
    worker_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    account_smtp_address: str = Field(min_length=3, max_length=254)
    folders: tuple[FolderSpec, ...] = Field(
        default_factory=_default_folders, min_length=1, max_length=MAX_FOLDERS
    )
    reconcile_interval_seconds: int = Field(default=120, ge=30, le=3600)
    tick_seconds: float = Field(default=2.0, ge=0.2, le=30.0)
    overlap_minutes: int = Field(default=30, ge=5, le=1440)
    sync_settle_minutes: int = Field(default=10, ge=0, le=720)
    initial_lookback_days: int = Field(default=15, ge=1, le=60)
    max_catchup_days: int = Field(default=30, ge=1, le=90)
    unmatched_retry_hours: int = Field(default=24, ge=1, le=72)
    max_pending_locators: int = Field(default=2000, ge=10, le=20000)
    max_scan_items: int = Field(default=20000, ge=50, le=200000)
    binding_page_limit: int = Field(default=100, ge=1, le=100)
    upload_batch_limit: int = Field(default=25, ge=1, le=200)
    http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    send_intents_enabled: bool = True
    start_outlook_if_not_running: bool = False
    data_dir: Path | None = None
    #: The OWNER-CONTROLLED test address of the one-time activation canary (F3, wave D2): another
    #: mailbox of the owner (never a seller, never the sending account). Only this machine knows
    #: it; the backend keeps its SHA-256 and the worker sends a canary only when they match.
    canary_target_address: str | None = Field(default=None, min_length=3, max_length=254)

    @field_validator("account_smtp_address")
    @classmethod
    def _address(cls, value: str) -> str:
        text = value.strip()
        if not _ADDRESS_RE.fullmatch(text) or ".." in text:
            raise ValueError("account_smtp_address must be a plain e-mail address")
        local, domain = text.split("@", 1)
        return f"{local}@{domain.lower().rstrip('.')}"

    @field_validator("canary_target_address")
    @classmethod
    def _canary_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._address(value)

    @field_validator("api_base_url")
    @classmethod
    def _url_shape(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("api_base_url must be an absolute https URL")
        if parts.username or parts.password or "@" in parts.netloc:
            raise ValueError("api_base_url must not embed credentials")
        if parts.query or parts.fragment:
            raise ValueError("api_base_url must not carry a query or fragment")
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"

    @model_validator(mode="after")
    def _rules(self) -> BridgeConfig:
        parts = urlsplit(self.api_base_url)
        if parts.scheme == "http" and not (
            self.allow_insecure_loopback and _is_loopback(parts.hostname or "")
        ):
            raise ValueError("plain http is allowed only for a loopback development backend")
        seen: set[tuple[str, str | None]] = set()
        roles: dict[str, int] = {}
        for spec in self.folders:
            key = (spec.role, spec.path.casefold() if spec.path else None)
            if key in seen:
                raise ValueError("duplicate folder entry")
            seen.add(key)
            roles[spec.role] = roles.get(spec.role, 0) + 1
        if roles.get("inbox", 0) != 1:
            raise ValueError("exactly one inbox folder must be configured")
        if roles.get("junk", 0) > 1:
            raise ValueError("at most one junk folder may be configured")
        if self.max_catchup_days * 24 < self.unmatched_retry_hours:
            raise ValueError("max_catchup_days must cover the unmatched retry window")
        if (
            self.canary_target_address is not None
            and self.canary_target_address.casefold() == self.account_smtp_address.casefold()
        ):
            raise ValueError("canary_target_address must be another mailbox than the sending account")
        return self

    # ------------------------------------------------------------------ derived values

    @property
    def overlap(self) -> timedelta:
        return timedelta(minutes=self.overlap_minutes)

    @property
    def sync_settle(self) -> timedelta:
        return timedelta(minutes=self.sync_settle_minutes)

    @property
    def initial_lookback(self) -> timedelta:
        return timedelta(days=self.initial_lookback_days)

    @property
    def max_catchup(self) -> timedelta:
        return timedelta(days=self.max_catchup_days)

    @property
    def unmatched_retry_window(self) -> timedelta:
        return timedelta(hours=self.unmatched_retry_hours)

    @property
    def reconcile_interval(self) -> timedelta:
        return timedelta(seconds=self.reconcile_interval_seconds)

    def resolved_data_dir(self) -> Path:
        return self.data_dir if self.data_dir is not None else default_data_dir()

    def store_path(self) -> Path:
        return self.resolved_data_dir() / STORE_FILENAME

    def credential_target(self) -> str:
        """Name of the entry in the OS credential store (no secret in the name)."""
        return f"SUVDeals.OutlookBridge/{self.mailbox_binding_id}"


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_data_dir(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Per-user, non-roaming application data directory.

    Windows: ``%LOCALAPPDATA%\\SUVDeals\\OutlookBridge`` (protected by the user profile ACL).
    Elsewhere (development/tests): ``$XDG_STATE_HOME/suv-deals-outlook-bridge``.
    """
    environ = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        base = environ.get("LOCALAPPDATA")
        if not base:
            raise ConfigError("LOCALAPPDATA is not set; cannot locate the protected data directory")
        return Path(base).joinpath(*APP_DIR_WINDOWS)
    state = environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path(environ.get("HOME", str(Path.home()))) / ".local" / "state"
    return root / APP_DIR_POSIX


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (owner-only on POSIX; Windows relies on the per-user profile ACL)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, stat.S_IRWXU)
    return path


def load_config(path: Path) -> BridgeConfig:
    """Load and validate the TOML configuration. Errors never echo file contents."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file ({type(exc).__name__})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigError("configuration file is not valid UTF-8 TOML") from None
    return parse_config(data)


def parse_config(data: Mapping[str, object]) -> BridgeConfig:
    try:
        return BridgeConfig.model_validate(dict(data))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "config" for err in exc.errors()})
        raise ConfigError("invalid configuration fields: " + ", ".join(fields[:20])) from None


__all__ = [
    "CONFIG_FILENAME",
    "MAX_FOLDERS",
    "STORE_FILENAME",
    "BridgeConfig",
    "FolderSpec",
    "ReplyFolderRole",
    "default_data_dir",
    "ensure_private_dir",
    "load_config",
    "parse_config",
]
