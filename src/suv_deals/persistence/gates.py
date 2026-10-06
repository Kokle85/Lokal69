"""Activation gates and honest completion states (spec section 32, ``ops.activation_gates``).

States: ``not_requested``, ``implemented``, ``fixture_verified``, ``integration_verified``,
``live_verified``, ``active``, ``blocked``. A completed codebase can have blocked production
capabilities; these states are never collapsed into one optimistic "done". ``live_verified``
and ``active`` require recorded evidence and a check time (enforced here and by a CHECK).

`seed_spec_gates` inserts the spec 32 gate list with honest initial statuses (``implemented``,
``blocked`` or ``not_requested``; never ``active``) and never overwrites a gate that already
exists. `list_gates` feeds ``doctor``, readiness/health and the MCP ``deals_health`` tool.
Evidence is redacted (no secrets) before it is stored; every change is audited.

System checks may record verification progress (``fixture_verified`` ... ``live_verified``) or a
``blocked`` state, but ``active`` is an activation decision (spec 32: owner approval is part of
the evidence) and needs ``config:admin``, which system principals never hold.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import GateStatus, Scope
from suv_deals.errors import Forbidden, ValidationFailed
from suv_deals.observability.audit import redact_metadata
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

_CAPABILITY_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{2,79}$")
_EVIDENCE_REQUIRED: Final = frozenset({GateStatus.LIVE_VERIFIED, GateStatus.ACTIVE})
_COLUMNS: Final = sql.SQL(
    "id, workspace_id, capability, dependency, required_evidence, status, owner, next_action,"
    " evidence, checked_at, row_version, created_at, updated_at"
)
_UPSERT_SQL: Final = sql.SQL(
    "insert into ops.activation_gates (workspace_id, capability, dependency, required_evidence,"
    " status, owner, next_action, evidence, checked_at)"
    " values (%(workspace_id)s, %(capability)s, %(dependency)s, %(required_evidence)s,"
    " %(status)s, %(owner)s, %(next_action)s, %(evidence)s, now())"
    " on conflict (workspace_id, capability) do update set"
    " dependency = excluded.dependency, required_evidence = excluded.required_evidence,"
    " status = excluded.status, owner = excluded.owner, next_action = excluded.next_action,"
    " evidence = excluded.evidence, checked_at = excluded.checked_at,"
    " row_version = ops.activation_gates.row_version + 1"
    " returning {columns}"
).format(columns=_COLUMNS)
_LIST_SQL: Final = sql.SQL(
    "select {columns} from ops.activation_gates where workspace_id = %(workspace_id)s order by capability"
).format(columns=_COLUMNS)
_SEED_SQL: Final = sql.SQL(
    "insert into ops.activation_gates (workspace_id, capability, dependency,"
    " required_evidence, status, owner, next_action, evidence, checked_at)"
    " values (%(workspace_id)s, %(capability)s, %(dependency)s, %(required_evidence)s,"
    " %(status)s, %(owner)s, %(next_action)s, '{{}}'::jsonb, now())"
    " on conflict (workspace_id, capability) do nothing"
    " returning {columns}"
).format(columns=_COLUMNS)


class GateDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    dependency: str
    required_evidence: str
    status: GateStatus
    owner: str
    next_action: str


class GateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    capability: str
    dependency: str
    required_evidence: str
    status: GateStatus
    owner: str | None = None
    next_action: str | None = None
    evidence: dict[str, Any]
    checked_at: datetime | None = None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_validator("checked_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


# Spec 32 gate list. Statuses are honest for this build: code exists for the first two;
# everything that needs an owner decision, account or live evidence is blocked; optional
# capabilities nobody asked for are not_requested. Nothing starts active.
SPEC_GATES: Final[tuple[GateDefinition, ...]] = (
    GateDefinition(
        capability="implementation_environment",
        dependency="Authorized repository/path and actual access",
        required_evidence="Authorized repository/path and actual access",
        status=GateStatus.IMPLEMENTED,
        owner="engineering",
        next_action="None for code work; keep the authorized repository as the single source.",
    ),
    GateDefinition(
        capability="existing_crawler",
        dependency="Running Crawl4AI service reachable from the worker",
        required_evidence="Runtime version, health, topology and supported auth/request contract",
        status=GateStatus.IMPLEMENTED,
        owner="owner",
        next_action="Provide the crawler endpoint and token so a live health/version check can run.",
    ),
    GateDefinition(
        capability="supabase",
        dependency="Approved Supabase project and server credentials",
        required_evidence=(
            "Approved project/organization, server credentials, schema/RLS tests, backup choice"
        ),
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Approve the Supabase project/region, provide server credentials and choose backups.",
    ),
    GateDefinition(
        capability="source_access",
        dependency="Per-source terms decision and live smoke",
        required_evidence=(
            "Exact source configuration, terms decision, robots handling and unblocked live smoke"
        ),
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Record a terms decision per source; then run the low-rate live smoke.",
    ),
    GateDefinition(
        capability="credentials_api_accounts",
        dependency="Owner-approved provider accounts (e.g. optional mobile.de Search API)",
        required_evidence="Owner-approved account/entitlement, correct scope, successful test",
        status=GateStatus.NOT_REQUESTED,
        owner="owner",
        next_action="Only if wanted: apply for the API account and provide credentials securely.",
    ),
    GateDefinition(
        capability="tax_rules",
        dependency="Current, source-supported MK import tax rule set",
        required_evidence="Current source-supported rule set, applicability and recorded approval",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Supply and approve a sourced tax rule set (see docs/tax_rule_approval.md).",
    ),
    GateDefinition(
        capability="cost_assumptions",
        dependency="Owner-selected business assumptions and quotes",
        required_evidence="Owner-selected business assumptions/quotes and currency treatment",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Choose logistics/inspection/reserve assumptions or provide quotes.",
    ),
    GateDefinition(
        capability="contribution_threshold",
        dependency="Explicit owner choice of the alert threshold",
        required_evidence="Explicit choice or approval; EUR1500 remains a proposal until then",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Confirm or change the proposed EUR 1,500 contribution threshold.",
    ),
    GateDefinition(
        capability="hosting",
        dependency="Approved hosting provider, region, budget and domain",
        required_evidence="Approved provider, region, budget, domain/TLS and deployment authority",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Approve a provider, region, monthly budget and domain.",
    ),
    GateDefinition(
        capability="mcp_authentication",
        dependency="Approved persistent MCP access for the dot client",
        required_evidence="Approved persistent access, issuer/audience/scopes, real client success",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Approve the MCP auth method and connect the real client once.",
    ),
    GateDefinition(
        capability="slack_destination",
        dependency="Verified private Slack channel (optional fallback)",
        required_evidence="Verified private channel and approved event data/audience",
        status=GateStatus.NOT_REQUESTED,
        owner="owner",
        next_action="Only if wanted: name the private channel and approve the event data.",
    ),
    GateDefinition(
        capability="native_mcp_events",
        dependency="dot support for MCP Events discovery/subscription",
        required_evidence=(
            "Actual dot supports discovery/subscription, approved scope, callback security,"
            " stored lifecycle and successful canary/unsubscribe"
        ),
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Confirm MCP Events are enabled for the workspace; then run the canary.",
    ),
    GateDefinition(
        capability="automatic_dot_activation",
        dependency="A selected native event or fallback trigger route",
        required_evidence="Selected native event or fallback trigger route and correlated end-to-end canary",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Select one activation route (native events or verified Slack).",
    ),
    GateDefinition(
        capability="production_notifications",
        dependency="Correct destination plus tested dedup and uncertainty handling",
        required_evidence="Correct destination, dedup and uncertainty handling tested",
        status=GateStatus.BLOCKED,
        owner="owner",
        next_action="Approve a destination binding; then run the delivery canary.",
    ),
)


def _require_admin(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only the owner or system checks may change activation gates")


def _text(value: str | None, limit: int, name: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValidationFailed(f"{name} is required")
        return None
    cleaned = redact(str(value)).strip()
    if required and not cleaned:
        raise ValidationFailed(f"{name} is required")
    if len(cleaned) > limit:
        raise ValidationFailed(f"{name} is at most {limit} characters")
    return cleaned or None


async def upsert_gate(  # noqa: PLR0917 - positional public contract (WP7a API)
    conn: Conn,
    actor: ActorContext,
    capability: str,
    dependency: str,
    required_evidence: str,
    status: GateStatus,
    evidence: Mapping[str, Any] | None = None,
    *,
    owner: str | None = None,
    next_action: str | None = None,
) -> GateRecord:
    """Create or update one gate (owner/system), recording ``checked_at`` and an audit event."""
    _require_admin(actor)
    if not isinstance(capability, str) or not _CAPABILITY_RE.fullmatch(capability):
        raise ValidationFailed("capability must be a lower-case name")
    status = GateStatus(status)
    if status == GateStatus.ACTIVE:
        actor.require(Scope.CONFIG_ADMIN)  # activation is the owner's decision, never a system's
    clean_evidence = redact_metadata(evidence)
    if status in _EVIDENCE_REQUIRED and not clean_evidence:
        raise ValidationFailed("live_verified and active gates require recorded evidence")
    params = {
        "workspace_id": actor.workspace_id,
        "capability": capability,
        "dependency": _text(dependency, 500, "dependency", required=True),
        "required_evidence": _text(required_evidence, 2000, "required_evidence", required=True),
        "status": status.value,
        "owner": _text(owner, 200, "owner", required=False),
        "next_action": _text(next_action, 2000, "next_action", required=False),
        "evidence": Jsonb(clean_evidence),
    }
    async with mapped_errors():
        prior = await fetch_one(
            conn,
            "select status, row_version from ops.activation_gates"
            " where workspace_id = %(workspace_id)s and capability = %(capability)s for update",
            params,
        )
        row = await fetch_one(conn, _UPSERT_SQL, params)
        assert row is not None
        record = GateRecord.model_validate(row)
        await audit.record(
            conn,
            actor,
            "activation_gate.update",
            "activation_gate",
            record.id,
            prior_version=None if prior is None else int(prior["row_version"]),
            new_version=record.row_version,
            metadata={
                "capability": capability,
                "prior_status": None if prior is None else prior["status"],
                "status": status.value,
            },
        )
    return record


async def list_gates(conn: Conn, actor: ActorContext) -> list[GateRecord]:
    """All gates of the workspace, ordered by capability (for doctor/health/deals_health)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(conn, _LIST_SQL, {"workspace_id": actor.workspace_id})
    return [GateRecord.model_validate(r) for r in rows]


async def seed_spec_gates(conn: Conn, actor: ActorContext) -> list[GateRecord]:
    """Insert the spec 32 gates that do not exist yet (existing gates are left untouched)."""
    _require_admin(actor)
    created: list[GateRecord] = []
    async with mapped_errors():
        for gate in SPEC_GATES:
            if gate.status in _EVIDENCE_REQUIRED:  # pragma: no cover - guarded by the constant
                raise ValidationFailed("seed statuses are never live_verified or active")
            row = await fetch_one(
                conn,
                _SEED_SQL,
                {
                    "workspace_id": actor.workspace_id,
                    "capability": gate.capability,
                    "dependency": gate.dependency,
                    "required_evidence": gate.required_evidence,
                    "status": gate.status.value,
                    "owner": gate.owner,
                    "next_action": gate.next_action,
                },
            )
            if row is not None:
                record = GateRecord.model_validate(row)
                created.append(record)
                await audit.record(
                    conn,
                    actor,
                    "activation_gate.seed",
                    "activation_gate",
                    record.id,
                    new_version=record.row_version,
                    metadata={"capability": gate.capability, "status": gate.status.value},
                )
    return created
