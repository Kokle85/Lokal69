"""``suv-deals bootstrap owner`` (spec 12, 27; ADR 0001).

Links an EXISTING Supabase Auth user (by id or e-mail) to a workspace as its owner:

- ``--workspace-name NAME`` creates a new workspace with that user as its first owner
  (`persistence.workspaces.create_workspace`), or
- ``--workspace ID`` adds/reactivates the owner membership in an existing active workspace.

It never creates, invites or changes an Auth user, password or key: a missing user is an error
("create or invite the user in Supabase Auth first"). ``suv_backend`` cannot create workspaces, so
this runs on the PRIVILEGED maintenance connection named by ``--url-env`` (default
``MAINTENANCE_DATABASE_URL``; never a command-line argument). The target is printed without the
password, and nothing is written without ``--yes``. Every write is audited.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import os
import re
from typing import Any
from uuid import UUID, uuid4

import click

from suv_deals.cli_commands._common import (
    EXIT_USAGE,
    MAINTENANCE_URL_ENV,
    CliContext,
    database_target,
    echo,
    fail,
    load_settings,
    pass_cli,
    require_yes,
    run_async,
)

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


@click.group("bootstrap")
def bootstrap_group() -> None:
    """One-time setup steps on a privileged maintenance connection."""


@bootstrap_group.command("owner")
@click.option("--user-id", type=click.UUID, default=None, help="Existing auth.users id.")
@click.option("--email", default=None, help="Existing auth.users e-mail (exactly one match required).")
@click.option("--workspace", type=click.UUID, default=None, help="Existing active workspace to own.")
@click.option("--workspace-name", default=None, help="Create a new workspace with this name.")
@click.option("--timezone", "display_timezone", default="Europe/Skopje", show_default=True)
@click.option(
    "--url-env",
    default=MAINTENANCE_URL_ENV,
    show_default=True,
    help="Environment variable holding the privileged maintenance connection string.",
)
@click.option("--yes", is_flag=True, help="Confirm the write.")
@pass_cli
def owner(
    cli: CliContext,
    *,
    user_id: UUID | None,
    email: str | None,
    workspace: UUID | None,
    workspace_name: str | None,
    display_timezone: str,
    url_env: str,
    yes: bool,
) -> None:
    """Make an existing Supabase Auth user the owner of a (new or existing) workspace."""
    if (user_id is None) == (email is None):
        fail("pass exactly one of --user-id or --email", EXIT_USAGE)
    if (workspace is None) == (workspace_name is None):
        fail("pass exactly one of --workspace or --workspace-name", EXIT_USAGE)
    if email is not None and not _EMAIL_RE.fullmatch(email.strip()):
        fail("--email is not an e-mail address", EXIT_USAGE)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", url_env):
        fail("--url-env must name an environment variable (upper case)", EXIT_USAGE)
    settings = load_settings(cli)
    url = os.environ.get(url_env, "")
    if not url:
        fail(f"{url_env} is not set (privileged maintenance connection; never commit it)", EXIT_USAGE)
    echo("Maintenance target (password never shown):")
    for line in database_target(url).lines():
        echo(line)
    echo(f"  APP_ENV  : {settings.app_env}")

    async def body() -> int:
        import psycopg
        from psycopg.rows import dict_row

        from suv_deals.domain.actor import ActorContext
        from suv_deals.persistence import audit, workspaces
        from suv_deals.persistence.errors_map import mapped_errors

        try:
            conn = await psycopg.AsyncConnection.connect(
                url,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=10,
                application_name="suv-deals-bootstrap",
            )
        except (psycopg.Error, OSError):
            fail("the maintenance database is not reachable", 4)
        async with conn:
            if user_id is not None:
                cur = await conn.execute("select id from auth.users where id = %s", (user_id,))
            else:
                assert email is not None
                cur = await conn.execute(
                    "select id from auth.users where lower(email) = lower(%s) limit 2", (email.strip(),)
                )
            users: list[dict[str, Any]] = await cur.fetchall()
            if not users:
                fail(
                    "no such Supabase Auth user; create or invite the user in Supabase Auth first "
                    "(this command never creates users)"
                )
            if len(users) > 1:
                fail("the e-mail matches several Auth users; pass --user-id")
            owner_id: UUID = users[0]["id"]
            echo(f"Auth user found  : {owner_id}")
            if workspace is not None:
                cur = await conn.execute(
                    "select name, active from app.workspaces where id = %s", (workspace,)
                )
                found = await cur.fetchone()
                if found is None or not found["active"]:
                    fail("the workspace is unknown or inactive")
                echo(f"Workspace        : {workspace} (existing)")
            else:
                echo(f"Workspace        : new, named {workspace_name!r}")
            require_yes(yes, "bootstrap owner")
            request_id = f"cli:bootstrap-owner:{uuid4().hex[:12]}"
            if workspace_name is not None:
                created = await workspaces.create_workspace(
                    conn,
                    name=workspace_name,
                    owner_user_id=owner_id,
                    request_id=request_id,
                    display_timezone=display_timezone,
                )
                echo(f"Created workspace {created.workspace_id} with owner {owner_id}.")
                target_workspace = created.workspace_id
            else:
                assert workspace is not None
                async with mapped_errors(), conn.transaction():
                    await conn.execute("select set_config('app.workspace_id', %s, true)", (str(workspace),))
                    cur = await conn.execute(
                        "select role, active from app.memberships where workspace_id = %s and user_id = %s"
                        " for update",
                        (workspace, owner_id),
                    )
                    prior = await cur.fetchone()
                    await conn.execute(
                        "insert into app.memberships (workspace_id, user_id, role, active)"
                        " values (%s, %s, 'owner', true)"
                        " on conflict (workspace_id, user_id) do update set role = 'owner', active = true",
                        (workspace, owner_id),
                    )
                    await audit.record(
                        conn,
                        ActorContext.system(workspace, request_id=request_id),
                        "membership.bootstrap_owner",
                        "membership",
                        owner_id,
                        reason="operator bootstrap of the workspace owner",
                        metadata={
                            "role": "owner",
                            "prior_role": None if prior is None else prior["role"],
                            "prior_active": None if prior is None else bool(prior["active"]),
                        },
                    )
                echo(f"User {owner_id} is now an active owner of workspace {workspace}.")
                target_workspace = workspace
        echo(
            "Next: `suv-deals config apply --workspace "
            + str(target_workspace)
            + " --reason ... --yes`, then"
        )
        echo("      `suv-deals sources sync --workspace " + str(target_workspace) + " --yes`.")
        return 0

    run_async(body)
