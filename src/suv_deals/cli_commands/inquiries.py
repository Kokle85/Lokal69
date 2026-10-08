"""Spec v1.1 seller-inquiry operator commands (spec 37; docs/runbook.md "Seller inquiries").

``mail-worker credential issue|revoke|list``
    Bind the Windows desktop worker to a sender binding's mailbox (``ops.mail_worker_bindings``)
    and mint its narrow, revocable ``mail:ingest`` credential (``suvmail_``). The token is printed
    exactly ONCE (only its hash is stored); store it on the desktop with
    ``python -m outlook_bridge credential set``. ``revoke`` permanently revokes the worker binding
    and its credential (the worker's next request is ``401``; it keeps its backlog).
``sender-binding create|status``
    Register the owner-authorized sending identity (``outlook_local`` default, ``gmail_api``
    optional). A provider secret is accepted only as an external REFERENCE (``scheme:path``),
    never as a value. ``status`` shows verification/alias/health without any address.
``inquiries status|pause|resume``
    Workspace controls. ``pause`` activates the kill switch (reason + expected version);
    ``resume`` clears it (owner action; optionally removing kill-switch/authorization-revoked
    suppressions, each audited, which needs ``--owner-user-id`` because suppressions are never
    removed by a system principal).
``evaluation report --days 15``
    The 15-day quality evaluation from stored evidence (zero is reported as zero).

Every state-changing command needs ``--yes``. Nothing here sends an e-mail, prints a token other
than the one just issued, or prints a mailbox address (only its domain).
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CliContext,
    echo,
    emit_json,
    fail,
    load_settings,
    pass_cli,
    require_yes,
    run_async,
    table,
    workspace_option,
)
from suv_deals.cli_commands.credentials import parse_expires

if TYPE_CHECKING:
    from suv_deals.domain.actor import ActorContext
    from suv_deals.persistence.database import Conn


def address_domain(address: str | None) -> str:
    """Only the domain of an address (``-`` without one): CLI output never shows a mailbox."""
    if not address or "@" not in address:
        return "-"
    return "@" + address.rsplit("@", 1)[1].lower()


# --------------------------------------------------------------------------------------------
# mail-worker credential
# --------------------------------------------------------------------------------------------


@click.group("mail-worker")
def mail_worker_group() -> None:
    """The Windows desktop mail worker's binding and credential (owner operation)."""


@mail_worker_group.group("credential")
def mail_worker_credential_group() -> None:
    """Issue, revoke and list mailbox-worker credentials (tokens are shown once)."""


@mail_worker_credential_group.command("issue")
@workspace_option
@click.option("--sender-binding", "sender_binding", type=click.UUID, required=True, help="Sender binding id.")
@click.option("--label", required=True, help="Human label, e.g. 'Owner laptop classic Outlook'.")
@click.option("--expires", default="90d", show_default=True, help="Lifetime in days, e.g. 30d (max 365d).")
@click.option("--yes", is_flag=True, help="Confirm issuing the credential.")
@pass_cli
def issue_mail_worker(
    cli: CliContext, *, workspace: UUID | None, sender_binding: UUID, label: str, expires: str, yes: bool
) -> None:
    """Bind a desktop worker to the sender binding's mailbox and print its token ONCE."""
    lifetime = parse_expires(expires)
    settings = load_settings(cli)
    echo("Mailbox-worker credential to issue:")
    echo(f"  sender binding : {sender_binding}")
    echo(f"  label          : {label}")
    echo("  scope          : mail:ingest only (bound to one workspace and one mailbox)")
    echo(f"  expires after  : {lifetime.days} day(s)")
    require_yes(yes, "mail-worker credential issue")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import mail_workers_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "mail-worker-issue")
            async with unit_of_work(db, actor) as conn:
                issued = await mail_workers_repo.issue_mail_worker(
                    conn, actor, sender_binding_id=sender_binding, label=label, lifetime=lifetime
                )
        credential = issued.credential
        echo(f"Issued mailbox worker {issued.mailbox_binding_id} (version {issued.mailbox_version})")
        echo(f"  credential : {credential.credential_id} (prefix {credential.token_prefix})")
        echo(f"  expires    : {credential.expires_at.isoformat()}")
        echo(f"  bindings   : {issued.published_bindings} published to this mailbox")
        echo("Token (shown ONCE; store it on the desktop now, never in a chat, file or ticket):")
        click.echo(credential.token.get_secret_value())
        echo(
            "On the Windows PC: python -m outlook_bridge credential set "
            f"--expires-at {credential.expires_at.isoformat()}"
        )
        echo(f"Set mailbox_binding_id = {issued.mailbox_binding_id} in the worker's config.toml.")
        return 0

    run_async(body)


@mail_worker_credential_group.command("revoke")
@click.argument("mailbox_binding_id", type=click.UUID)
@workspace_option
@click.option("--reason", required=True, help="Why (3-500 characters; audited).")
@click.option("--yes", is_flag=True, help="Confirm the revocation.")
@pass_cli
def revoke_mail_worker(
    cli: CliContext, mailbox_binding_id: UUID, workspace: UUID | None, reason: str, yes: bool
) -> None:
    """Revoke a mailbox worker and its credential (permanent; its next request is 401)."""
    settings = load_settings(cli)
    require_yes(yes, "mail-worker credential revoke")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import mail_workers_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "mail-worker-revoke")
            async with unit_of_work(db, actor) as conn:
                changed = await mail_workers_repo.revoke_mail_worker(
                    conn, actor, mailbox_binding_id, reason=reason
                )
        echo("Revoked." if changed else "Already revoked; nothing changed.")
        return 0

    run_async(body)


@mail_worker_credential_group.command("list")
@workspace_option
@click.option("--all", "show_all", is_flag=True, help="Include revoked mailbox workers.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def list_mail_workers(cli: CliContext, workspace: UUID | None, show_all: bool, as_json: bool) -> None:
    """List mailbox workers with their health dimensions (never tokens or addresses)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import mail_workers_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "mail-worker-list")
            async with unit_of_work(db, actor) as conn:
                boxes = await mail_workers_repo.list_mailbox_health(
                    conn, workspace_id, include_revoked=show_all
                )
        rows: list[dict[str, Any]] = [
            {
                "id": b.mailbox_binding_id,
                "sender_binding": b.sender_binding_id,
                "provider": b.provider.value,
                "label": b.worker_label,
                "state": b.binding_state,
                "heartbeat": b.heartbeat_status,
                "heartbeat_age_s": b.heartbeat_age_seconds,
                "account": b.account_status,
                "open_gaps": b.open_gap_count,
                "monitoring": b.monitoring_active,
            }
            for b in boxes
        ]
        if as_json:
            emit_json(rows)
        elif not rows:
            echo("No mailbox workers.")
        else:
            echo(table(rows, list(rows[0])))
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# sender-binding
# --------------------------------------------------------------------------------------------


@click.group("sender-binding")
def sender_binding_group() -> None:
    """The owner-authorized sending identity (secrets only by reference)."""


@sender_binding_group.command("create")
@workspace_option
@click.option(
    "--provider",
    type=click.Choice(["outlook_local", "gmail_api"]),
    default="outlook_local",
    show_default=True,
    help="outlook_local (classic Outlook desktop worker, default) or gmail_api (optional).",
)
@click.option("--account-id", required=True, help="Stable provider account id (not a secret).")
@click.option("--from-address", required=True, help="The verified From address of the account.")
@click.option("--display-name", required=True, help="Display name used as the signature.")
@click.option("--reply-to", default=None, help="Optional Reply-To alias (verified separately).")
@click.option(
    "--secret-reference",
    default=None,
    help="External secret REFERENCE (scheme:path) for gmail_api; never the secret itself.",
)
@click.option("--reason", required=True, help="Why (audited).")
@click.option("--yes", is_flag=True, help="Confirm creating the binding.")
@pass_cli
def create_sender_binding(
    cli: CliContext,
    *,
    workspace: UUID | None,
    provider: str,
    account_id: str,
    from_address: str,
    display_name: str,
    reply_to: str | None,
    secret_reference: str | None,
    reason: str,
    yes: bool,
) -> None:
    """Register an (unverified) sender binding; verification happens in the activation steps."""
    if secret_reference is not None and provider != "gmail_api":
        fail(
            "--secret-reference is only used by gmail_api (outlook_local holds no provider secret)",
            EXIT_USAGE,
        )
    settings = load_settings(cli)
    echo("Sender binding to create (unverified until the activation evidence is recorded):")
    echo(f"  provider     : {provider}")
    echo(f"  from domain  : {address_domain(from_address)}")
    echo(f"  reply-to     : {address_domain(reply_to)}")
    echo(f"  secret       : {'reference set' if secret_reference else 'none'}")
    require_yes(yes, "sender-binding create")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.enums import EmailProviderKind
        from suv_deals.persistence import sender_bindings_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sender-binding-create", admin=True)
            async with unit_of_work(db, actor) as conn:
                record = await sender_bindings_repo.create_binding(
                    conn,
                    actor,
                    provider=EmailProviderKind(provider),
                    account_id=account_id,
                    from_address=from_address,
                    display_name=display_name,
                    reply_to_address=reply_to,
                    reason=reason,
                )
                if secret_reference is not None:
                    record = await sender_bindings_repo.set_secret_reference(
                        conn,
                        actor,
                        record.id,
                        reference=secret_reference,
                        expected_version=record.version,
                        reason=reason,
                    )
        echo(f"Created sender binding {record.id} (version {record.version}, unverified).")
        echo("Next: record the verification evidence (docs/seller_email_activation.md).")
        return 0

    run_async(body)


@sender_binding_group.command("status")
@workspace_option
@click.option("--all", "show_all", is_flag=True, help="Include revoked bindings.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def sender_binding_status(cli: CliContext, workspace: UUID | None, show_all: bool, as_json: bool) -> None:
    """Sender bindings with verification, alias and health state (domains only, never secrets)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import sender_bindings_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sender-binding-status")
            async with unit_of_work(db, actor) as conn:
                records = await sender_bindings_repo.list_bindings(conn, actor, include_revoked=show_all)
        rows: list[dict[str, Any]] = [
            {
                "id": r.id,
                "provider": r.provider.value,
                "version": r.version,
                "from_domain": address_domain(r.from_address),
                "verified": r.verified_at is not None,
                "alias_verified": r.alias_verified,
                "health": r.health,
                "has_secret": r.has_secret,
                "revoked": r.revoked,
                "usable": r.usable,
            }
            for r in records
        ]
        if as_json:
            emit_json(rows)
        elif not rows:
            echo("No sender bindings.")
        else:
            echo(table(rows, list(rows[0])))
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# inquiries
# --------------------------------------------------------------------------------------------


@click.group("inquiries")
def inquiries_group() -> None:
    """Seller-inquiry controls (kill switch, mode, caps)."""


async def _status_data(conn: Conn, actor: ActorContext) -> dict[str, Any]:
    from suv_deals.api.inquiry_routes import removable_suppressions
    from suv_deals.clock import ensure_utc
    from suv_deals.persistence import inquiries_repo, mail_workers_repo, sender_bindings_repo
    from suv_deals.persistence.database import db_now

    now = ensure_utc(await db_now(conn))
    controls = await inquiries_repo.control_view(conn, actor)
    authorization = await inquiries_repo.current_authorization(conn, actor)
    sender = await sender_bindings_repo.active_binding(conn, actor)
    boxes = await mail_workers_repo.list_mailbox_health(conn, actor.workspace_id)
    removable = await removable_suppressions(conn, actor, now)
    problems = [] if authorization is None else list(authorization.authorization.problems_at(now))
    return {
        "version": controls.version,
        "mode": controls.mode,
        "kill_switch": controls.kill_switch,
        "max_per_24h": controls.max_per_24h,
        "max_per_15d": controls.max_per_15d,
        "used_24h": controls.used_24h,
        "used_15d": controls.used_15d,
        "authorization": "missing"
        if authorization is None
        else (
            "revoked"
            if authorization.revoked_at is not None
            else ("active" if not problems else "not_effective")
        ),
        "authorization_version": None if authorization is None else authorization.version,
        "sender_binding": "missing"
        if sender is None
        else ("usable" if sender.usable else ("verified" if sender.verified_at else "unverified")),
        "sender_provider": None if sender is None else sender.provider.value,
        "mail_workers": len(boxes),
        "mail_workers_monitoring": sum(1 for b in boxes if b.monitoring_active),
        "removable_suppressions": len(removable),
    }


@inquiries_group.command("status")
@workspace_option
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def inquiries_status(cli: CliContext, workspace: UUID | None, as_json: bool) -> None:
    """Mode, kill switch, caps/usage, authorization, sender and worker readiness (no addresses)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-status")
            async with unit_of_work(db, actor) as conn:
                data = await _status_data(conn, actor)
        if as_json:
            emit_json({"workspace_id": str(workspace_id), **data})
            return 0
        echo(f"Seller inquiries (workspace {workspace_id}):")
        for key, value in data.items():
            echo(f"  {key:<24}: {'-' if value is None else value}")
        return 0

    run_async(body)


@inquiries_group.command("pause")
@workspace_option
@click.option("--reason", required=True, help="Why (3-2000 characters; audited).")
@click.option("--expected-version", type=int, required=True, help="Control version from `inquiries status`.")
@click.option("--yes", is_flag=True, help="Confirm activating the kill switch.")
@pass_cli
def inquiries_pause(
    cli: CliContext, workspace: UUID | None, reason: str, expected_version: int, yes: bool
) -> None:
    """Activate the kill switch: untransmitted inquiries stop at the next guard (never resumes)."""
    settings = load_settings(cli)
    require_yes(yes, "inquiries pause")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import inquiries_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-pause", admin=True)
            async with unit_of_work(db, actor) as conn:
                result = await inquiries_repo.pause(
                    conn, actor, expected_version=expected_version, reason=reason
                )
        if result.already_paused:
            echo(f"Already paused (version {result.version}); nothing changed.")
        else:
            echo(f"Paused (version {result.version}). Resuming is a separate owner action.")
        return 0

    run_async(body)


async def _owner_actor(conn: Conn, workspace_id: UUID, user_id: UUID, purpose: str) -> ActorContext:
    """The signed-in owner's actor (``--owner-user-id`` must be an ACTIVE owner of the workspace)."""
    from uuid import uuid4

    from suv_deals.domain.actor import ActorContext
    from suv_deals.domain.enums import Role, Scope
    from suv_deals.persistence import workspaces

    membership = await workspaces.get_membership(conn, workspace_id, user_id)
    if membership is None or not membership.active or membership.role != Role.OWNER:
        fail("--owner-user-id is not an active owner of this workspace", EXIT_PROBLEMS)
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=user_id,
        principal_kind="user",
        role=Role.OWNER,
        scopes=frozenset(Scope) - {Scope.MAIL_INGEST},
        request_id=f"cli:{purpose}:{uuid4().hex[:12]}",
        display_name="owner via operator-cli",
    )


@inquiries_group.command("resume")
@workspace_option
@click.option("--reason", required=True, help="Why (3-2000 characters; audited).")
@click.option("--expected-version", type=int, required=True, help="Control version from `inquiries status`.")
@click.option(
    "--remove-suppressions",
    is_flag=True,
    help="Also remove kill-switch (and, while authorized, authorization-revoked) suppressions; each audited.",
)
@click.option(
    "--owner-user-id",
    type=click.UUID,
    default=None,
    help="The owner's user id (required with --remove-suppressions: never removed by a system principal).",
)
@click.option("--yes", is_flag=True, help="Confirm clearing the kill switch.")
@pass_cli
def inquiries_resume(
    cli: CliContext,
    *,
    workspace: UUID | None,
    reason: str,
    expected_version: int,
    remove_suppressions: bool,
    owner_user_id: UUID | None,
    yes: bool,
) -> None:
    """Clear the kill switch (owner action); nothing is sent by the resume itself."""
    if remove_suppressions and owner_user_id is None:
        fail(
            "--remove-suppressions needs --owner-user-id (suppressions are removed by the owner)", EXIT_USAGE
        )
    settings = load_settings(cli)
    require_yes(yes, "inquiries resume")

    async def body() -> int:
        from suv_deals.api.inquiry_routes import removable_suppressions
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.clock import ensure_utc
        from suv_deals.persistence import inquiries_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            system = operator_actor(workspace_id, "inquiries-resume", admin=True)
            removed = 0
            async with unit_of_work(db, system) as conn:
                actor = (
                    system
                    if owner_user_id is None
                    else await _owner_actor(conn, workspace_id, owner_user_id, "inquiries-resume")
                )
            async with unit_of_work(db, actor) as conn:
                result = await inquiries_repo.resume(
                    conn, actor, expected_version=expected_version, reason=reason
                )
                if remove_suppressions:
                    now = ensure_utc(await db_now(conn))
                    for row in await removable_suppressions(conn, actor, now):
                        await inquiries_repo.remove_suppression(
                            conn, actor, row.id, reason=f"resume: {reason}"
                        )
                        removed += 1
        echo(f"Resumed (version {result.version}, mode {result.mode}).")
        if remove_suppressions:
            echo(f"Removed {removed} suppression(s) (each audited).")
        return 0

    run_async(body)


# --------------------------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------------------------


@click.group("evaluation")
def evaluation_group() -> None:
    """The 15-day quality evaluation."""


@evaluation_group.command("report")
@workspace_option
@click.option(
    "--days", type=click.IntRange(15, 15), default=15, show_default=True, help="Window (fixed: 15)."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def evaluation_report(cli: CliContext, workspace: UUID | None, days: int, as_json: bool) -> None:
    """Report the 15-day evaluation from stored evidence (zero is reported as zero)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.money import Money
        from suv_deals.persistence import queries
        from suv_deals.persistence.transactions import unit_of_work

        threshold = (
            Money.of(settings.proposed_min_contribution_eur, "EUR")
            if settings.contribution_threshold_approved
            else None
        )
        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "evaluation-report")
            async with unit_of_work(db, actor) as conn:
                result = await queries.evaluation_report(conn, actor, approved_threshold=threshold)
        payload = result.data.model_dump(mode="json")
        warnings = [w.code.value for w in result.warnings]
        if as_json:
            emit_json(
                {"workspace_id": str(workspace_id), "days": days, "report": payload, "warnings": warnings}
            )
            return 0
        echo(f"15-day evaluation (workspace {workspace_id}; as of {result.as_of.isoformat()}):")
        for key, value in payload.items():
            if isinstance(value, dict | list):
                continue
            echo(f"  {key:<32}: {'-' if value is None else value}")
        for code in warnings:
            echo(f"  warning: {code}")
        echo("Use --json for the per-source coverage, candidates, inquiries and replies.")
        return 0

    run_async(body)


__all__ = [
    "address_domain",
    "evaluation_group",
    "inquiries_group",
    "mail_worker_group",
    "sender_binding_group",
]
