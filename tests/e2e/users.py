"""SYNTHETIC end-to-end users shared by the mock Supabase Auth server and the seed script.

Everything here is test-only: the ``.invalid`` addresses cannot receive mail, the password is a
published test value (never a real credential) and the user ids exist only in throwaway databases.
The Playwright specs mirror these values in ``dashboard/e2e/fixtures.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

#: Test-only password of every SYNTHETIC user (not a secret; the mock server is loopback-only).
PASSWORD: Final = "synthetic-e2e-password"
#: The publishable key baked into the E2E build; the mock server requires it like Supabase does.
PUBLISHABLE_KEY: Final = "sb_publishable_SYNTHETIC_e2e_key"
#: Normal access-token lifetime (seconds).
DEFAULT_TTL: Final = 3600


@dataclass(frozen=True, slots=True)
class E2EUser:
    key: str
    user_id: UUID
    email: str
    #: Real JWT lifetime. The token response still ADVERTISES ``DEFAULT_TTL`` to the client, which
    #: emulates a token the backend rejects mid-session (expiry/revocation the browser cannot see).
    access_ttl: int = DEFAULT_TTL


USERS: Final[dict[str, E2EUser]] = {
    user.key: user
    for user in (
        E2EUser("owner", UUID("00000000-0000-4000-8e2e-0000000000a1"), "owner@e2e.invalid"),
        E2EUser("reviewer", UUID("00000000-0000-4000-8e2e-0000000000a2"), "reviewer@e2e.invalid"),
        E2EUser("reviewer2", UUID("00000000-0000-4000-8e2e-0000000000a3"), "reviewer2@e2e.invalid"),
        E2EUser("viewer", UUID("00000000-0000-4000-8e2e-0000000000a4"), "viewer@e2e.invalid"),
        E2EUser(
            "expiring", UUID("00000000-0000-4000-8e2e-0000000000a5"), "expiring@e2e.invalid", access_ttl=6
        ),
        E2EUser("multi", UUID("00000000-0000-4000-8e2e-0000000000a6"), "multi@e2e.invalid"),
        E2EUser("stranger", UUID("00000000-0000-4000-8e2e-0000000000a7"), "stranger@e2e.invalid"),
    )
}

USERS_BY_EMAIL: Final[dict[str, E2EUser]] = {user.email: user for user in USERS.values()}

#: Role of each user in the main SYNTHETIC workspace (``stranger`` has no membership at all).
MAIN_ROLES: Final[dict[str, str]] = {
    "owner": "owner",
    "reviewer": "reviewer",
    "reviewer2": "reviewer",
    "viewer": "viewer",
    "expiring": "reviewer",
    "multi": "reviewer",
}
#: Role of each user in the second SYNTHETIC workspace (workspace-selection test).
SECOND_ROLES: Final[dict[str, str]] = {"multi": "viewer"}
