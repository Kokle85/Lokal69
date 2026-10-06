"""Authenticated actor context required by every repository and domain operation.

There is no unscoped repository access (spec section 12). The workspace in an
ActorContext has already been validated against the principal's membership by
the API/MCP auth layer; workers use `ActorContext.system` for their own
workspace-scoped jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden

PrincipalKind = Literal["user", "mcp_client", "system"]

ROLE_SCOPES: dict[Role, frozenset[Scope]] = {
    Role.VIEWER: frozenset({Scope.DEALS_READ, Scope.REVIEWS_READ}),
    Role.REVIEWER: frozenset(
        {
            Scope.DEALS_READ,
            Scope.REVIEWS_READ,
            Scope.REVIEWS_WRITE,
            Scope.EVENTS_SUBSCRIBE,
            Scope.RECHECKS_REQUEST,
            Scope.NOTES_WRITE,
        }
    ),
    Role.OWNER: frozenset(Scope),
}


@dataclass(frozen=True, slots=True)
class ActorContext:
    workspace_id: UUID
    principal_id: UUID
    principal_kind: PrincipalKind
    role: Role
    scopes: frozenset[Scope]
    request_id: str
    display_name: str | None = None
    client_id: str | None = None  # OAuth client / MCP credential identifier, never a secret
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A credential can narrow but never widen what the member role allows.
        allowed = ROLE_SCOPES[self.role]
        if not self.scopes <= allowed:
            raise Forbidden("Credential scopes exceed the member role")

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    def require(self, *scopes: Scope) -> None:
        missing = [s for s in scopes if s not in self.scopes]
        if missing:
            raise Forbidden(f"Missing scope: {', '.join(s.value for s in missing)}")

    @classmethod
    def system(cls, workspace_id: UUID, request_id: str, principal_id: UUID | None = None) -> ActorContext:
        """Context for scheduler/worker/dispatcher processes acting for one workspace."""
        return cls(
            workspace_id=workspace_id,
            principal_id=principal_id or UUID(int=0),
            principal_kind="system",
            role=Role.OWNER,
            scopes=frozenset(Scope) - {Scope.CONFIG_ADMIN},
            request_id=request_id,
            display_name="system",
        )
