"""Spec v1.1 seller-inquiry operator commands (spec 37; docs/runbook.md "Seller inquiries").

``mail-worker credential issue|revoke|list``
    Bind the Windows desktop worker to a sender binding's mailbox (``ops.mail_worker_bindings``)
    and mint its narrow, revocable ``mail:ingest`` credential (``suvmail_``). The token is printed
    exactly ONCE (only its hash is stored); store it on the desktop with
    ``python -m outlook_bridge credential set``. ``revoke`` permanently revokes the worker binding
    and its credential (the worker's next request is ``401``; it keeps its backlog).
``sender-binding create|verify|status``
    Register the owner-authorized sending identity (``outlook_local`` default, ``gmail_api``
    optional). A provider secret is accepted only as an external REFERENCE (``scheme:path``),
    never as a value. ``verify`` records the technical verification of an ``outlook_local``
    binding from the desktop worker's evidence (classic-Outlook account report of the bound
    address, fresh heartbeat, stable account key); it is a prerequisite, never a message approval.
    ``status`` shows verification/alias/health without any address.
``inquiries authorize|status|set-mode|set-limits|pause|resume``
    Workspace controls. ``authorize`` creates the controls row and records the owner's versioned
    standing authorization (an audit record, never a message approval); ``set-mode`` and
    ``set-limits`` change mode and the owner-reducible ceilings (expected version, reason;
    ``automatic`` only with an active authorization and a usable sender binding that is exactly
    the configured identity);
    ``pause`` activates the kill switch (reason + expected version);
    ``resume`` clears it (owner action; optionally removing kill-switch/authorization-revoked
    suppressions, each audited, which needs ``--owner-user-id`` because suppressions are never
    removed by a system principal, and ``--expected-suppressions N``: the count the owner saw in
    ``inquiries status``; a different current count refuses the whole resume, so only the
    suppressions the owner saw are removed).
``evaluation report --days 15``
    The 15-day quality evaluation from stored evidence (zero is reported as zero).

Every state-changing command needs ``--yes``. Nothing here sends an e-mail, prints a token other
than the one just issued, or prints a mailbox address (only its domain).
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_PROBLEMS,
    EXIT_REFUSED,
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

#: Upper bound of ``inquiries resume --expected-suppressions`` (equal to
#: ``api.schemas.MAX_REMOVABLE_SUPPRESSIONS``; kept local so ``--help`` stays fast, a test pins it).
MAX_REMOVABLE_SUPPRESSIONS: Final = 5000

if TYPE_CHECKING:
    from suv_deals.domain.actor import ActorContext
    from suv_deals.domain.inquiries import SenderMode
    from suv_deals.persistence.database import Conn
    from suv_deals.settings import Settings


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
    "--vault-ref",
    "vault_ref",
    default=None,
    help="gmail_api only: an external secret-store REFERENCE (scheme:path), never the secret itself.",
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
    vault_ref: str | None,
    reason: str,
    yes: bool,
) -> None:
    """Register an (unverified) sender binding; verification happens in the activation steps."""
    if vault_ref is not None and provider != "gmail_api":
        fail("--vault-ref is only used by gmail_api (outlook_local holds no provider secret)", EXIT_USAGE)
    settings = load_settings(cli)
    echo("Sender binding to create (unverified until the activation evidence is recorded):")
    echo(f"  provider     : {provider}")
    echo(f"  from domain  : {address_domain(from_address)}")
    echo(f"  reply-to     : {address_domain(reply_to)}")
    echo(f"  secret       : {'external reference set' if vault_ref else 'none'}")
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
                if vault_ref is not None:
                    record = await sender_bindings_repo.set_secret_reference(
                        conn,
                        actor,
                        record.id,
                        reference=vault_ref,
                        expected_version=record.version,
                        reason=reason,
                    )
        echo(f"Created sender binding {record.id} (version {record.version}, unverified).")
        echo("Next: record the verification evidence (docs/seller_email_activation.md).")
        return 0

    run_async(body)


async def outlook_verification_problems(
    conn: Conn, actor: ActorContext, binding_id: UUID
) -> tuple[list[str], list[str], Any]:
    """Why an ``outlook_local`` sender binding cannot be verified from the desktop worker's evidence
    (``(problems, warnings, record)``; no network, no values in the codes).

    Evidence (spec 37.3, docs/seller_email_activation.md section 3): an ACTIVE mailbox worker of
    this binding whose latest account report matched the bound address on classic Outlook, a fresh
    heartbeat, the binding's account id equal to the reported stable account key (compared by its
    SHA-256 in the audit trail; the SMTP address itself is accepted with a warning) and no Reply-To
    other than the account's own address (classic Outlook offers no alias verification).
    """
    import hashlib

    from suv_deals.persistence import audit, mail_workers_repo, sender_bindings_repo

    record = await sender_bindings_repo.get_binding(conn, actor, binding_id)
    problems: list[str] = []
    warnings: list[str] = []
    if record.provider.value != "outlook_local":
        return ["PROVIDER_NOT_OUTLOOK_LOCAL"], warnings, record
    if record.revoked_at is not None:
        return ["BINDING_REVOKED"], warnings, record
    reply_to = (record.reply_to_address or "").casefold()
    if reply_to and reply_to != record.from_address.casefold():
        problems.append("REPLY_TO_NOT_VERIFIABLE_ON_OUTLOOK_LOCAL")
    boxes = [
        b
        for b in await mail_workers_repo.list_mailbox_health(conn, actor.workspace_id)
        if b.sender_binding_id == binding_id and b.binding_state == "active"
    ]
    if not boxes:
        problems.append("NO_ACTIVE_MAIL_WORKER")
        return problems, warnings, record
    box = boxes[0]
    if box.account_status != "verified":
        problems.append(f"WORKER_ACCOUNT_{box.account_status.upper()}")
    if box.heartbeat_status != "healthy":
        problems.append(f"WORKER_HEARTBEAT_{box.heartbeat_status.upper()}")
    reports = [
        e
        for e in await audit.list_for_target(conn, actor, "mail_worker_binding", box.mailbox_binding_id)
        if e.action == "mail_worker.account_report" and e.metadata.get("status") == "verified"
    ]
    if record.account_id.strip().casefold() == record.from_address.casefold():
        warnings.append("ACCOUNT_ID_IS_THE_SMTP_ADDRESS")
    elif not reports:
        problems.append("NO_VERIFIED_ACCOUNT_REPORT")
    else:
        latest = max(reports, key=lambda e: e.occurred_at)
        expected = hashlib.sha256(record.account_id.encode("utf-8")).hexdigest()
        if latest.metadata.get("stable_account_key_sha256") != expected:
            problems.append("ACCOUNT_KEY_MISMATCH")
    return problems, warnings, record


@sender_binding_group.command("verify")
@click.argument("binding_id", type=click.UUID)
@workspace_option
@click.option("--reason", required=True, help="Why (audited).")
@click.option("--yes", is_flag=True, help="Confirm recording the verification.")
@pass_cli
def verify_sender_binding(
    cli: CliContext, binding_id: UUID, workspace: UUID | None, reason: str, yes: bool
) -> None:
    """outlook_local: record the technical verification from the desktop worker's evidence.

    A technical prerequisite of automatic sending (spec 37.3), never a message approval. gmail_api
    is verified through the provider check of docs/seller_email_activation.md instead.
    """
    settings = load_settings(cli)
    require_yes(yes, "sender-binding verify")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import sender_bindings_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "sender-binding-verify", admin=True)
            async with unit_of_work(db, actor) as conn:
                problems, warnings, record = await outlook_verification_problems(conn, actor, binding_id)
                if not problems:
                    record = await sender_bindings_repo.record_verification(
                        conn,
                        actor,
                        binding_id,
                        expected_version=record.version,
                        verified=True,
                        alias_verified=True,
                        health="healthy",
                        reason=reason,
                    )
        for code in warnings:
            echo(f"  warning: {code}")
        if problems:
            for code in problems:
                echo(f"  problem: {code}")
            fail("the sender binding cannot be verified yet (nothing changed)", EXIT_REFUSED)
        echo(f"Verified sender binding {record.id} (version {record.version}, healthy).")
        echo("Sending still needs mode automatic, the kill switch off and an active authorization.")
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


async def _status_data(conn: Conn, actor: ActorContext, settings: Settings) -> dict[str, Any]:
    from suv_deals.api.inquiry_routes import removable_suppressions
    from suv_deals.clock import ensure_utc
    from suv_deals.persistence import inquiries_repo, mail_workers_repo
    from suv_deals.persistence.database import db_now
    from suv_deals.workers.inquiry_handlers import (
        automatic_sending_enabled,
        configured_sender_binding,
        configured_sender_problems,
    )

    now = ensure_utc(await db_now(conn))
    present = await inquiries_repo.get_controls(conn, actor)
    controls = None if present is None else await inquiries_repo.control_view(conn, actor)
    authorization = await inquiries_repo.current_authorization(conn, actor)
    # The binding the runtime would use: the configured provider's binding that IS the configured
    # identity (SELLER_EMAIL_PROVIDER/_ACCOUNT_ID/_FROM/_REPLY_TO); another verified identity is
    # never used (`workers.inquiry_handlers.configured_sender_binding`).
    sender = await configured_sender_binding(conn, actor, settings)
    identity_problems = configured_sender_problems(settings, sender)
    boxes = await mail_workers_repo.list_mailbox_health(conn, actor.workspace_id)
    removable = await removable_suppressions(conn, actor, now)
    problems = [] if authorization is None else list(authorization.authorization.problems_at(now))
    # The process settings are part of the gate (the mail-worker claim refuses while they are
    # closed; the dispatcher plans nothing): SELLER_INQUIRY_MODE, SELLER_INQUIRY_KILL_SWITCH and
    # the owner's SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL switch (which disables sending).
    process_open = automatic_sending_enabled(settings)
    sending = (
        process_open
        and controls is not None
        and controls.mode == "automatic"
        and not controls.kill_switch
        and authorization is not None
        and authorization.revoked_at is None
        and not problems
        and sender is not None
        and sender.usable
        and not identity_problems
    )
    return {
        # Nothing is sent unless ALL of these hold (process settings, mode, kill switch,
        # authorization, sender).
        "sending_possible": sending,
        "process_mode": settings.seller_inquiry_mode,
        "process_kill_switch": settings.seller_inquiry_kill_switch,
        "version": None if controls is None else controls.version,
        "mode": "no_controls_yet" if controls is None else controls.mode,
        "kill_switch": None if controls is None else controls.kill_switch,
        "max_per_24h": None if controls is None else controls.max_per_24h,
        "max_per_15d": None if controls is None else controls.max_per_15d,
        "seller_cooldown_days": None if controls is None else controls.seller_cooldown_seconds // 86400,
        "used_24h": None if controls is None else controls.used_24h,
        "used_15d": None if controls is None else controls.used_15d,
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
        # Codes only (never an address): empty when the binding is the configured identity.
        "sender_identity_problems": identity_problems,
        "mail_workers": len(boxes),
        "mail_workers_monitoring": sum(1 for b in boxes if b.monitoring_active),
        "removable_suppressions": len(removable),
    }


@inquiries_group.command("status")
@workspace_option
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def inquiries_status(cli: CliContext, workspace: UUID | None, as_json: bool) -> None:
    """Process settings, mode, kill switch, caps/usage, authorization, sender and worker readiness
    (no addresses)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-status")
            async with unit_of_work(db, actor) as conn:
                data = await _status_data(conn, actor, settings)
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


@inquiries_group.command("authorize")
@workspace_option
@click.option(
    "--file",
    "path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Authorization YAML (default: config/seller_inquiry_authorization.yaml).",
)
@click.option("--reason", required=True, help="Why (audited).")
@click.option("--yes", is_flag=True, help="Confirm recording the standing authorization.")
@pass_cli
def inquiries_authorize(
    cli: CliContext, workspace: UUID | None, path: Path | None, reason: str, yes: bool
) -> None:
    """Create the workspace controls (mode disabled_until_sender_ready) and record the owner's
    versioned standing authorization (spec 37.1: an audit record, never a message approval)."""
    settings = load_settings(cli)
    require_yes(yes, "inquiries authorize")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import inquiries_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-authorize", admin=True)
            async with unit_of_work(db, actor) as conn:
                controls = await inquiries_repo.ensure_controls(conn, actor)
                record = await inquiries_repo.record_authorization_file(conn, actor, path, reason=reason)
        echo(f"Controls version {controls.version}, mode {controls.mode} (nothing is sent in this mode).")
        echo(f"Standing authorization version {record.version} recorded (no per-message approval).")
        return 0

    run_async(body)


@inquiries_group.command("set-mode")
@click.argument("mode", type=click.Choice(["disabled_until_sender_ready", "automatic", "paused"]))
@workspace_option
@click.option("--reason", required=True, help="Why (audited).")
@click.option("--expected-version", type=int, required=True, help="Control version from `inquiries status`.")
@click.option("--yes", is_flag=True, help="Confirm the mode change.")
@pass_cli
def inquiries_set_mode(
    cli: CliContext, mode: str, *, workspace: UUID | None, reason: str, expected_version: int, yes: bool
) -> None:
    """Workspace mode. ``automatic`` lets the pipeline send under the standing authorization once
    every technical prerequisite holds (the kill switch is a separate control).

    ``automatic`` is checked against the CONFIGURED sender binding (``SELLER_EMAIL_PROVIDER`` /
    ``_ACCOUNT_ID`` / ``_FROM`` / ``_REPLY_TO``; `workers.inquiry_handlers.configured_sender_binding`),
    never merely the newest binding: without a binding of the configured provider, or when that
    provider's binding is not exactly the configured identity, it is refused (the identity codes are
    the control view's ``sender_identity_*`` codes, never a value). Every missing technical
    prerequisite is listed by code (`inquiries_repo.AutomaticModePrerequisitesMissing`; exit 3,
    nothing changed)."""
    settings = load_settings(cli)
    require_yes(yes, "inquiries set-mode")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.clock import ensure_utc
        from suv_deals.persistence import inquiries_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work
        from suv_deals.workers.inquiry_handlers import configured_sender_binding, configured_sender_problems

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-set-mode", admin=True)
            try:
                async with unit_of_work(db, actor) as conn:
                    sender = await configured_sender_binding(conn, actor, settings)
                    if mode == "automatic":
                        # No binding of the CONFIGURED provider (never fall back to another one), or
                        # its binding is not exactly the configured identity (the runtime never
                        # sends from it): refused like any other missing prerequisite, all named.
                        identity = (
                            ["sender_binding_missing"]
                            if sender is None
                            else [
                                f"sender_identity_{code.lower()}"
                                for code in configured_sender_problems(settings, sender)
                                if code != "SENDER_BINDING_MISSING"
                            ]
                        )
                        if identity:
                            now = ensure_utc(await db_now(conn))
                            authorization = await inquiries_repo.current_authorization(conn, actor)
                            others = [
                                *([] if sender is None else inquiries_repo.sender_binding_problems(sender)),
                                *inquiries_repo.authorization_problems(authorization, now),
                            ]
                            raise inquiries_repo.AutomaticModePrerequisitesMissing(
                                list(dict.fromkeys([*identity, *others]))
                            )
                    controls = await inquiries_repo.set_mode(
                        conn,
                        actor,
                        expected_version=expected_version,
                        mode=cast("SenderMode", mode),
                        reason=reason,
                        sender_binding_id=None if sender is None else sender.id,
                    )
            except inquiries_repo.AutomaticModePrerequisitesMissing as exc:
                missing = [str(code) for code in (exc.details or {}).get("missing", [])]
                for code in missing:
                    echo(f"  missing: {code}")
                fail(
                    "automatic needs every technical prerequisite first (nothing changed): "
                    + ", ".join(missing),
                    EXIT_REFUSED,
                )
        switch = "on" if controls.kill_switch else "off"
        echo(f"Mode {controls.mode} (version {controls.version}); kill switch {switch}.")
        if controls.mode == "automatic" and settings.seller_inquiry_mode != "automatic":
            echo(
                "Note: the process setting SELLER_INQUIRY_MODE is not automatic; nothing is sent until it is."
            )
        return 0

    run_async(body)


@inquiries_group.command("set-limits")
@workspace_option
@click.option("--max-per-24h", type=click.IntRange(0, 2), required=True, help="0..2 (never higher).")
@click.option("--max-per-15d", type=click.IntRange(0, 5), required=True, help="0..5 (never higher).")
@click.option(
    "--cooldown-days",
    type=click.IntRange(7, 365),
    default=None,
    help="Seller cooldown in days, 7..365 (never shorter than 7; default: keep the current one).",
)
@click.option("--reason", required=True, help="Why (audited).")
@click.option("--expected-version", type=int, required=True, help="Control version from `inquiries status`.")
@click.option("--yes", is_flag=True, help="Confirm the new ceilings.")
@pass_cli
def inquiries_set_limits(
    cli: CliContext,
    *,
    workspace: UUID | None,
    max_per_24h: int,
    max_per_15d: int,
    cooldown_days: int | None,
    reason: str,
    expected_version: int,
    yes: bool,
) -> None:
    """Owner-reducible ceilings (spec 37.5: at most 2 per rolling 24 h and 5 per rolling 15 days).

    The seller cooldown only ever widens beyond the v1.1 default of 7 days (a shorter one would
    allow a burst of e-mails to one dealer); without ``--cooldown-days`` the current cooldown is
    kept, so lowering the caps never silently shortens a longer cooldown."""
    settings = load_settings(cli)
    require_yes(yes, "inquiries set-limits")

    async def body() -> int:
        from datetime import timedelta

        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.inquiries import SELLER_COOLDOWN
        from suv_deals.persistence import inquiries_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "inquiries-set-limits", admin=True)
            async with unit_of_work(db, actor) as conn:
                current = await inquiries_repo.get_controls(conn, actor)
                if current is None:
                    fail("no seller-inquiry controls yet; run `inquiries authorize` first", EXIT_REFUSED)
                requested = (
                    current.seller_cooldown if cooldown_days is None else timedelta(days=cooldown_days)
                )
                cooldown = max(requested, SELLER_COOLDOWN)  # a stored value below the default is not kept
                controls = await inquiries_repo.set_limits(
                    conn,
                    actor,
                    expected_version=expected_version,
                    max_per_24h=max_per_24h,
                    max_per_15d=max_per_15d,
                    seller_cooldown=cooldown,
                    reason=reason,
                )
        echo(
            f"Ceilings {controls.max_per_24h}/24h and {controls.max_per_15d}/15d, cooldown "
            f"{controls.seller_cooldown.days} day(s) (version {controls.version})."
        )
        return 0

    run_async(body)


async def owner_actor_for(conn: Conn, workspace_id: UUID, user_id: UUID, purpose: str) -> ActorContext:
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
    "--expected-suppressions",
    "expected_suppressions",
    type=click.IntRange(0, MAX_REMOVABLE_SUPPRESSIONS),
    default=None,
    help=(
        "Required with --remove-suppressions: the removable_suppressions count shown by"
        " `inquiries status`. A different current count refuses the whole resume (nothing changes)."
    ),
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
    expected_suppressions: int | None,
    owner_user_id: UUID | None,
    yes: bool,
) -> None:
    """Clear the kill switch (owner action); nothing is sent by the resume itself.

    With ``--remove-suppressions`` the owner names the count of removable suppressions they saw
    (``--expected-suppressions``, from ``inquiries status``): the resume, the count check and every
    removal run in ONE transaction after the controls lock (`api.inquiry_routes.resume_inquiries`,
    the dashboard's rule), so only the suppressions the owner saw are ever removed."""
    if remove_suppressions and owner_user_id is None:
        fail(
            "--remove-suppressions needs --owner-user-id (suppressions are removed by the owner)", EXIT_USAGE
        )
    if remove_suppressions and expected_suppressions is None:
        fail(
            "--remove-suppressions needs --expected-suppressions N (the removable_suppressions count"
            " shown by `inquiries status`)",
            EXIT_USAGE,
        )
    if expected_suppressions is not None and not remove_suppressions:
        fail("--expected-suppressions only applies with --remove-suppressions", EXIT_USAGE)
    settings = load_settings(cli)
    require_yes(yes, "inquiries resume")

    async def body() -> int:
        from suv_deals.api.inquiry_routes import SuppressionsChanged, resume_inquiries
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            system = operator_actor(workspace_id, "inquiries-resume", admin=True)
            async with unit_of_work(db, system) as conn:
                actor = (
                    system
                    if owner_user_id is None
                    else await owner_actor_for(conn, workspace_id, owner_user_id, "inquiries-resume")
                )
            try:
                async with unit_of_work(db, actor) as conn:
                    result = await resume_inquiries(
                        conn,
                        actor,
                        expected_version=expected_version,
                        reason=reason,
                        remove_suppressions=remove_suppressions,
                        expected_removable=expected_suppressions,
                    )
            except SuppressionsChanged as exc:
                current = (exc.details or {}).get("current_removable_suppressions")
                fail(
                    f"refused: {current} removable suppression(s) now, {expected_suppressions} expected;"
                    " nothing changed (check `inquiries status` and decide again)",
                    EXIT_PROBLEMS,
                )
        echo(f"Resumed (version {result.version}, mode {result.mode}).")
        if remove_suppressions:
            echo(f"Removed {result.suppressions_removed} suppression(s) (each audited).")
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
    "MAX_REMOVABLE_SUPPRESSIONS",
    "address_domain",
    "evaluation_group",
    "inquiries_group",
    "mail_worker_group",
    "owner_actor_for",
    "sender_binding_group",
]
