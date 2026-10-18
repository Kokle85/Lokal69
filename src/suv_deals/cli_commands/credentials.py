"""``suv-deals credentials create-mcp|revoke|list`` (spec 20, 24, 28; owner operations).

``create-mcp`` mints one scoped MCP credential (``static_bearer``, or ``dev_local`` in
development/test) through ``persistence.credentials_repo.issue_credential``: only the SHA-256 hash
is stored, the token is printed exactly ONCE and cannot be shown again. ``list`` and ``revoke`` use
the same repository (``list_credentials`` never returns tokens or hashes). Scopes narrow the role
and never include ``config:admin`` or ``mail:ingest``. A static bearer credential is a scoped
private credential, not OAuth compliance. Nothing is created without ``--yes``.

``create-mcp --authorization-file PATH`` writes the token as exactly ``Bearer <token>`` into a NEW
0600 file outside the repository instead of printing it (`_authorization_file`: every refusal is a
usage error before anything is created). It is meant only for a client that cannot hold a
secret-store entry itself but reads a static header from a file (docs/connect_mcp.md section 3).
The credential is inserted and the file written in ONE transaction: a failed write rolls the
credential back, and when the COMMIT fails after the write (its outcome unknown) the credential is
revoked if it exists, so no live token remains that nobody holds. An interrupt (Ctrl-C) after the
write keeps the file and names the credential that may be live, with its revoke command.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID, uuid4

import click

from suv_deals.cli_commands._authorization_file import (
    AuthorizationTarget,
    ReservedFile,
    prepare_target,
)
from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CliContext,
    describe_error,
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
    warn,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.domain.actor import ActorContext
    from suv_deals.persistence.credentials_repo import IssuedCredential
    from suv_deals.persistence.database import Conn, Database

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
@click.option(
    "--authorization-file",
    "authorization_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the token as exactly 'Bearer <token>' (no newline) into this NEW file (mode 0600,"
    " outside the repository, parent directory 0700 and yours) instead of printing it. Only for a"
    " client that cannot hold a secret-store entry itself but reads a static header from a file;"
    " owner-approved.",
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
    authorization_file: Path | None,
    yes: bool,
) -> None:
    """Create one scoped MCP credential and print its token ONCE (only the hash is stored).

    With --authorization-file the token is written to that new 0600 file instead and not printed.
    """
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
    target = prepare_target(authorization_file) if authorization_file is not None else None
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
    if target is not None:
        echo(f"  token goes to  : {target.path} (new file, mode 0600, 'Bearer <token>'; not printed)")
    require_yes(yes, "credentials create-mcp")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.enums import Role
        from suv_deals.persistence.credentials_repo import issue_credential
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-create")

            async def issue(conn: Conn) -> IssuedCredential:
                return await issue_credential(
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

            if target is None:
                async with unit_of_work(db, actor) as conn:
                    issued = await issue(conn)
            else:
                issued = await _issue_into_file(db, actor, target, issue)
        echo(f"Created credential {issued.credential_id} in workspace {issued.workspace_id}")
        echo(f"  prefix  : {issued.token_prefix}")
        echo(f"  expires : {issued.expires_at.isoformat()}")
        if target is None:
            echo("Token (shown ONCE; store it in the client's secret store now, never in a chat or file):")
            click.echo(issued.token.get_secret_value())
        else:
            echo(f"Authorization header written to {target.path}")
            echo("  (mode 0600, exactly 'Bearer <token>' without a newline). The token is not shown and")
            echo("  cannot be shown again; only the client that reads this file may hold it.")
        revoke_hint = f"Revoke with: suv-deals credentials revoke {issued.credential_id} --reason '...' --yes"
        echo(revoke_hint if target is None else f"{revoke_hint} (then delete the file)")
        return 0

    run_async(body)


async def _issue_into_file(
    db: Database,
    actor: ActorContext,
    target: AuthorizationTarget,
    issue: Callable[[Conn], Awaitable[IssuedCredential]],
) -> IssuedCredential:
    """Insert the credential and write its header file in ONE transaction (module docstring).

    The file is created (``O_EXCL``, empty) before the transaction; it holds the token only once
    the insert succeeded, and the transaction commits only after the token is on disk.
    """
    from suv_deals.persistence.transactions import unit_of_work

    reserved = ReservedFile(target)  # a path that appeared meanwhile is refused: nothing created yet
    issued: IssuedCredential | None = None
    written = False
    try:
        async with unit_of_work(db, actor) as conn:
            issued = await issue(conn)
            try:
                reserved.write(f"Bearer {issued.token.get_secret_value()}")
            except OSError as exc:
                raise _WriteFailed(exc.strerror or type(exc).__name__) from None
            written = True
    except _WriteFailed as exc:
        reserved.discard()
        fail(
            f"could not write the authorization file {target.path} ({exc}); the transaction was"
            " rolled back, so no credential was created",
            EXIT_PROBLEMS,
        )
    except Exception as exc:
        if not written or issued is None:
            reserved.discard()  # the insert itself failed: nothing was committed
            raise
        await _settle_uncertain_commit(db, actor, issued, reserved, exc)
    except BaseException:
        # Interrupted: before the write nothing was committed; after it the token may be live,
        # so the file that holds it is kept and the credential named (no database work after an
        # interrupt: the owner asked to stop, and the COMMIT itself may be what hangs).
        if written and issued is not None:
            reserved.close()
            warn(
                f"interrupted after the token was written, so the commit outcome is unknown and"
                f" credential {issued.credential_id} may be live. The authorization file"
                f" {target.path} was KEPT because its token may be live: check it with `suv-deals"
                f" mcp probe --authorization-file {target.path}` or `suv-deals credentials list`;"
                f" if it is not wanted, run `suv-deals credentials revoke {issued.credential_id}"
                " --reason '...' --yes`, then delete the file."
            )
        else:
            reserved.discard()
        raise
    reserved.close()
    assert issued is not None
    return issued


class _WriteFailed(Exception):
    """Writing the authorization file failed inside the transaction (it rolls back)."""


async def _settle_uncertain_commit(
    db: Database,
    actor: ActorContext,
    issued: IssuedCredential,
    reserved: ReservedFile,
    error: BaseException,
) -> NoReturn:
    """The file holds the token but the COMMIT failed or its outcome is unknown (for example the
    connection dropped during COMMIT): revoke the credential if it exists and remove the file."""
    from suv_deals.errors import NotFound
    from suv_deals.persistence.credentials_repo import revoke_credential
    from suv_deals.persistence.transactions import unit_of_work

    cause = describe_error(error)
    credential_id = issued.credential_id
    try:
        async with unit_of_work(db, actor) as conn:
            await revoke_credential(
                conn, actor, credential_id, reason="authorization file not confirmed: commit outcome unknown"
            )
    except NotFound:
        reserved.discard()
        fail(
            f"the credential was not committed ({cause}); none exists and the authorization file was"
            " removed. Run the command again.",
            EXIT_PROBLEMS,
        )
    except Exception as exc:
        reserved.close()
        fail(
            f"the commit outcome is unknown ({cause}) and credential {credential_id} could not be"
            f" revoked ({describe_error(exc)}). The authorization file {reserved.target.path} was KEPT"
            f" because its token may be live: run `suv-deals credentials revoke {credential_id}"
            " --reason '...' --yes`, then delete the file.",
            EXIT_PROBLEMS,
        )
    reserved.discard()
    fail(
        f"the commit outcome was unknown ({cause}); credential {credential_id} existed and was revoked,"
        " and the authorization file was removed. Run the command again.",
        EXIT_PROBLEMS,
    )


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
        from suv_deals.persistence.credentials_repo import revoke_credential
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-revoke")
            async with unit_of_work(db, actor) as conn:
                changed = await revoke_credential(conn, actor, credential_id, reason=reason)
        echo("Revoked." if changed else "Already revoked; nothing changed.")
        return 0

    run_async(body)


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
        from suv_deals.persistence.credentials_repo import list_credentials
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "credential-list")
            async with unit_of_work(db, actor) as conn:
                records = await list_credentials(conn, actor, include_inactive=show_all)
        rows: list[dict[str, Any]] = [
            {
                "id": r.credential_id,
                "label": r.label,
                "principal_kind": r.principal_kind,
                "role": r.role.value,
                "credential_kind": r.credential_kind,
                "token_prefix": r.token_prefix,
                "scopes": list(r.scopes),
                "expires_at": r.expires_at,
                "revoked_at": r.revoked_at,
                "last_used_at": r.last_used_at,
                "created_at": r.created_at,
            }
            for r in records
        ]
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
