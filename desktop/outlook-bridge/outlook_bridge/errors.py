"""Typed worker errors.

Messages are safe by construction: they never contain mail content, addresses, subjects, tokens or
file paths of the mailbox; only stable codes and short explanations.
"""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for expected worker failures."""

    code: str = "BRIDGE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code}: {self})"


class ConfigError(BridgeError):
    code = "CONFIG_INVALID"


class UnsupportedEnvironment(BridgeError):
    """Not classic Outlook for Windows, or not an interactive user session."""

    code = "UNSUPPORTED_ENVIRONMENT"


class NonInteractiveSession(UnsupportedEnvironment):
    """SYSTEM/service/session-0/non-interactive window station: Outlook OOM is refused."""

    code = "NON_INTERACTIVE_SESSION"

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(
            "Outlook automation needs the signed-in interactive user; refused: " + ", ".join(reasons)
        )


class StaError(BridgeError):
    code = "STA_ERROR"


class StaNotRunning(StaError):
    code = "STA_NOT_RUNNING"


class StaTimeout(StaError):
    code = "STA_TIMEOUT"


class ComObjectLeak(StaError):
    """A COM object (or other non-plain value) tried to leave the STA thread."""

    code = "COM_OBJECT_LEAK"


class OutlookUnavailable(BridgeError):
    """Outlook is not running/attached, or the configured account is not in the profile."""

    code = "OUTLOOK_UNAVAILABLE"


class FolderScopeError(BridgeError):
    """A configured folder cannot be resolved inside the configured account's store."""

    code = "FOLDER_SCOPE_INVALID"


class LocalStoreError(BridgeError):
    code = "LOCAL_STORE_ERROR"


class MailboxMismatch(LocalStoreError):
    """Data for another mailbox binding (cross-mailbox injection or a mixed-up local store)."""

    code = "MAILBOX_MISMATCH"


class CredentialError(BridgeError):
    code = "CREDENTIAL_ERROR"


class CredentialMissing(CredentialError):
    code = "CREDENTIAL_MISSING"


class CredentialUnusable(CredentialError):
    """Rejected by the server (401/revoked) or past its known expiry: transmission stops."""

    code = "CREDENTIAL_UNUSABLE"


class ForbiddenCredentialKind(CredentialError):
    """A Supabase/database/service credential was offered; the desktop never stores those."""

    code = "FORBIDDEN_CREDENTIAL_KIND"


class SemanticsUnavailable(BridgeError):
    """The shared reply-correlation semantics (``suv_deals.domain.replies``) cannot be imported."""

    code = "REPLY_SEMANTICS_UNAVAILABLE"


class UploadBuildError(BridgeError):
    """A correlated reply could not be turned into a valid 37.8 ingest request."""

    code = "UPLOAD_BUILD_FAILED"


class IntentInvalid(BridgeError):
    """A send intent failed local validation (it is refused before any ``.Send``)."""

    code = "INTENT_INVALID"
