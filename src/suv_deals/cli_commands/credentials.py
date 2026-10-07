"""``suv-deals credentials create-mcp|revoke|list`` (spec 20, 24, 28; owner operations).

``create-mcp`` mints one scoped MCP credential (``static_bearer``, or ``dev_local`` in
development/test) through `mcp.auth.issue_api_credential`: only the SHA-256 hash is stored, the
token is printed exactly ONCE and cannot be shown again. Scopes narrow the role and never include
``config:admin`` or ``mail:ingest``. A static bearer credential is a scoped private credential,
not OAuth compliance. Nothing is created without ``--yes``.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from datetime import timedelta
from uuid import UUID, uuid4

import click

from suv_deals.cli_commands._common import (
    EXIT_USAGE,
    CliContext,
    echo,
    emit_json,
    fail,
    load_settings,
    parse_csv,
    pass_cli,
    refuse,
    require_yes,
    run_async,
    table,
    workspace_option,
)

_EXPIRES_RE = re.compile(r"^([0-9]{1,3})d$")
MAX_DAYS = 365


def parse_expires(value: str) -> timedelta:
    match = _EXPIRES_RE.fullmatch(value.strip())
    if match is None or not 1 <= int(match.group(1)) <= MAX_DAYS:
        fail(f"--expires must look like 90d (1-{MAX_DAYS} days)", EXIT_USAGE)
    return timedelta(days=int(match.group(1)))


@click.group("credentials")
def credentials_group() -> None:
    """Scoped MCP credentials (owner operation; tokens are shown once)."""


@credentials_group.command("create-mcp")
@workspace_option
@click.option("--label", required=True, help="Human label, e.g. 'dot read-only'.")
@click.option(
    "--scopes", "scopes_csv", required=True, help="Comma-separated MCP scopes, e.g. deals:read,reviews:read."
)
@click.option("--expires", default="90d", show_default=True, help="Lifetime in days, e.g. 30d (max 365d).")
@click.option(
    "--role",
    type=click.Choice(["viewer", "reviewer", "owner"]),
    default="reviewer",
    show_default=True,
    help="Member role the credential acts as (scopes can only narrow it).",
)
@click.option(
    "--principal-kind",
    type=click.Choice(["mcp_client", "user"]),
    default="mcp_client",
    show_default=True,
)
@click.option("--principal-id", type=click.UUID, default=None, help="Required for --principal-kind user.")
@click.option(
    "--kind",
    type=click.Choice(["static_bearer", "dev_local"]),
    default="static_bearer",
    show_default=True,
    help="dev_local only in development/test against a loopback URL.",
)
@click.option("--yes", is_flag=True, help="Confirm creating the credential.")
@pass_cli
def create_mcp(
    cli: CliContext,
    *,
    workspace: UUID | None,
    label: str,
    scopes_csv: str,
    expires: str,
    role: str,
    principal_kind: str,
    principal_id: UUID | None,
    kind: str,
    yes: bool,
) -> None:
    """Create one scoped MCP credential and print its token ONCE (only the hash is stored)."""
    from suv_deals.domain.enums import Scope

    lifetime = parse_expires(expires)
    names = parse_csv(scopes_csv)
    valid = {s.value for s in Scope}
    unknown = [n for n in names if n not in valid]
    if unknown or not names:
        fail(f"unknown scope(s): {', '.join(unknown) or '-'}", EXIT_USAGE)
    forbidden = [n for n in names if n in (Scope.CONFIG_ADMIN.value, Scope.MAIL_INGEST.value)]
    if forbidden:
        refuse(f"{', '.join(forbidden)} is never granted to an MCP credential")
    if principal_kind == "user" and principal_id is None:
        fail("--principal-kind user needs --principal-id (an existing member's user id)", EXIT_USAGE)
    settings = load_settings(cli)
    if kind == "dev_local" and settings.app_env not in ("development", "test"):
        refuse("dev_local credentials exist only in development/test")
    principal = principal_id or uuid4()
    echo("MCP credential to create:")
    echo(f"  label          : {label}")
    echo(
        f"  kind           : {kind}"
        + ("  (scoped private credential, not OAuth)" if kind == "static_bearer" else "")
    )
    echo(f"  principal      : {principal_kind} {principal}")
    echo(f"  role / scopes  : {role} / {', '.join(names)}")
    echo(f"  expires after  : {lifetime.days} day(s)")
    require_yes(yes, "credentials create-mcp")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.enums import Role
        from suv_deals.mcp.auth import issue_api_credential
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-create")
            async with unit_of_work(db, actor) as conn:
                issued = await issue_api_credential(
                    conn,
                    actor,
                    principal_id=principal,
                    principal_kind="user" if principal_kind == "user" else "mcp_client",
                    role=Role(role),
                    scopes=[Scope(n) for n in names],
                    label=label,
                    kind="dev_local" if kind == "dev_local" else "static_bearer",
                    lifetime=lifetime,
                )
        echo(f"Created credential {issued.credential_id} in workspace {issued.workspace_id}")
        echo(f"  prefix  : {issued.token_prefix}")
        echo(f"  expires : {issued.expires_at.isoformat()}")
        echo("Token (shown ONCE; store it in the client's secret store now, never in a chat or file):")
        click.echo(issued.token.get_secret_value())
        echo(f"Revoke with: suv-deals credentials revoke {issued.credential_id} --reason '...' --yes")
        return 0

    run_async(body)


@credentials_group.command("revoke")
@click.argument("credential_id", type=click.UUID)
@workspace_option
@click.option("--reason", required=True, help="Why (3-500 characters; audited).")
@click.option("--yes", is_flag=True, help="Confirm the revocation.")
@pass_cli
def revoke(cli: CliContext, credential_id: UUID, workspace: UUID | None, reason: str, yes: bool) -> None:
    """Revoke one credential immediately (its next request is 401)."""
    settings = load_settings(cli)
    require_yes(yes, "credentials revoke")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.mcp.auth import revoke_api_credential
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-revoke")
            async with unit_of_work(db, actor) as conn:
                changed = await revoke_api_credential(conn, actor, credential_id, reason=reason)
        echo("Revoked." if changed else "Already revoked; nothing changed.")
        return 0

    run_async(body)


_LIST_SQL = """
select id, label, principal_kind, role, credential_kind, token_prefix, scopes, expires_at, revoked_at,
       last_used_at, created_at
  from ops.api_credentials
 where workspace_id = %(ws)s and (%(all)s or (revoked_at is null and expires_at > clock_timestamp()))
 order by created_at desc, id
 limit 200
"""


@credentials_group.command("list")
@workspace_option
@click.option("--all", "show_all", is_flag=True, help="Include revoked and expired credentials.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def list_credentials(cli: CliContext, workspace: UUID | None, show_all: bool, as_json: bool) -> None:
    """List credential metadata (never tokens or hashes)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.database import fetch_all
        from suv_deals.persistence.errors_map import mapped_errors
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-list")
            async with unit_of_work(db, actor) as conn, mapped_errors():
                rows = await fetch_all(conn, _LIST_SQL, {"ws": workspace_id, "all": show_all})
        if as_json:
            emit_json(rows)
            return 0
        if not rows:
            echo("No credentials.")
            return 0
        for row in rows:
            row["scopes"] = ",".join(row["scopes"])
        columns = [
            "id",
            "label",
            "credential_kind",
            "role",
            "token_prefix",
            "scopes",
            "expires_at",
            "revoked_at",
        ]
        echo(table(rows, columns))
        return 0

    run_async(body)
