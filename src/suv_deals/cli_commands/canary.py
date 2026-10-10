"""Owner-controlled activation canary commands (spec 37.10; docs/seller_email_activation.md section 8).

A canary is the one-time activation evidence of the configured sending route: one synthetic
message from the configured sender to an OWNER-CONTROLLED test address (never a seller), its
receipt reconciliation and a correlated test reply. It is not a seller inquiry: no listing,
seller, reservation or quota debit, never one of the 15-day deals (`persistence.canaries_repo`).

``canary prepare --purpose TEXT --yes``
    Record a ``prepared`` canary for the CONFIGURED sender binding (``SELLER_EMAIL_PROVIDER`` /
    ``_ACCOUNT_ID`` / ``_FROM`` / ``_REPLY_TO``). The owner-controlled target address is read from
    the environment variable ``SUV_CANARY_TARGET_ADDRESS`` (set it for this one command only) or
    from a hidden prompt; it is never echoed, never logged and stored only as its SHA-256. Nothing
    is sent. The repository refuses while the kill switch is on, after
    ``canaries_repo.MAX_CANARIES_PER_24H`` canaries in 24 hours, for a seller's address or an
    unverified/revoked sender.
``canary status [--json]``
    The canaries (ids, states, times; never the address or its hash) and the activation-evidence
    state of the configured sender binding (`canary_evidence`, also reported by ``doctor``).
``canary cancel CANARY_ID --reason ... --yes``
    Cancel a ``prepared`` canary that will not be sent (it still counts against the 24-hour cap).
``canary send CANARY_ID --i-confirm-owner-controlled-address --yes``
    THE ACTIVATION STEP THE OWNER RUNS (never run by an operator script, a test or an agent). It is
    impossible unless EVERY activation switch is on (`canary_send_blockers`):
    ``SELLER_EMAIL_CANARY_SEND_ENABLED=true``, ``SELLER_INQUIRY_MODE=automatic``,
    ``SELLER_INQUIRY_KILL_SWITCH=false``, ``SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL=false``, the
    workspace controls ``automatic`` with the kill switch off, the standing authorization active,
    the configured sender binding usable and exactly the configured identity, the canary
    ``prepared`` for that binding's current version, AND the separate flag
    ``--i-confirm-owner-controlled-address`` is given (and an ``outlook_local`` canary's desktop
    mailbox worker is still active). The target address is entered again (same variable or
    prompt) and must hash to the stored one; immediately before the transport the controls row is
    locked, EVERY gate is read again and the canary is committed ``uncertain`` (spec 37.5: the
    attempt is durable BEFORE the external I/O, so a crash or a second ``canary send`` can never
    transmit it twice). Only then is the provider's canary transport (`CANARY_TRANSPORTS`) called
    once and its outcome recorded. No provider has a canary transport
    yet (the seller-inquiry providers only send registered seller templates, and the desktop
    worker only claims inquiry intents), so today the command ends with the precise blocker
    ``CANARY_TRANSPORT_UNAVAILABLE`` after every other gate: nothing is sent and nothing changes.

Every state-changing command needs ``--yes``. Output never contains an address, a hash or a token.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, NoReturn
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
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
    stdin_is_tty,
    table,
    workspace_option,
)

if TYPE_CHECKING:
    from suv_deals.domain.actor import ActorContext
    from suv_deals.persistence.canaries_repo import CanaryOutcome, CanaryRecord
    from suv_deals.persistence.database import Conn
    from suv_deals.persistence.inquiries_repo import InquiryControls
    from suv_deals.persistence.sender_bindings_repo import SenderBindingRecord
    from suv_deals.settings import Settings

#: The only place the owner-controlled test address is read from (besides a hidden prompt). It is
#: deliberately NOT a `Settings` field, so it is never loaded from (or written to) ``.env``.
CANARY_TARGET_ENV: Final = "SUV_CANARY_TARGET_ADDRESS"
CONFIRM_FLAG: Final = "--i-confirm-owner-controlled-address"
MAX_STATUS_ROWS: Final = 200

#: ``(canary, target address) -> (outcome, evidence)``. The command has already checked every gate
#: and committed the canary ``uncertain`` before calling it; the transport re-checks nothing itself
#: and must never log or return the address.
CanaryTransport = Callable[["CanaryRecord", str], Awaitable[tuple["CanaryOutcome", dict[str, Any]]]]
#: Canary transports per provider (``EmailProviderKind`` value). EMPTY on purpose: the
#: seller-inquiry providers refuse anything but a registered seller template
#: (``mime_builder.inquiry_scope_problems``) and the desktop worker only claims inquiry intents, so
#: a canary cannot be transmitted yet (``CANARY_TRANSPORT_UNAVAILABLE``).
CANARY_TRANSPORTS: dict[str, CanaryTransport] = {}


# --------------------------------------------------------------------------------------------
# Pure helpers (shared with doctor)
# --------------------------------------------------------------------------------------------


def canary_evidence(canaries: Sequence[CanaryRecord], sender: SenderBindingRecord | None) -> tuple[str, str]:
    """``(state, detail)`` of the activation-canary evidence for the configured sender binding.

    ``complete``: a correlated test reply was recorded for a canary of the binding's CURRENT
    version (an identity change needs a new canary); otherwise the newest canary's state of that
    version (``prepared`` / ``accepted`` / ``uncertain`` / ``failed`` / ``cancelled``), ``stale``
    (only older versions), ``none`` or ``no_sender``. ``canaries`` are newest first.
    """
    if sender is None:
        return "no_sender", "no configured sender binding"
    mine = [c for c in canaries if c.sender_binding_id == sender.id]
    if not mine:
        return "none", "no canary recorded for the configured sender binding"
    current = [c for c in mine if c.sender_binding_version == sender.version]
    if any(c.correlated for c in current):
        return "complete", f"correlated test reply recorded (sender binding v{sender.version})"
    if not current:
        return (
            "stale",
            f"canaries exist only for older versions of the sender binding (now v{sender.version})",
        )
    latest = current[0]
    return (
        latest.state,
        f"newest canary {latest.state} (sender binding v{sender.version}); no correlated reply",
    )


def canary_send_blockers(
    settings: Settings,
    *,
    confirmed: bool,
    controls: InquiryControls | None,
    authorization_problems: Sequence[str],
    sender: SenderBindingRecord | None,
    identity_problems: Sequence[str],
    canary: CanaryRecord | None,
    mailbox_active: bool | None = None,
) -> list[str]:
    """Every reason ``canary send`` must not transmit (codes only; empty = every gate is open).

    ``mailbox_active``: for a canary bound to a desktop mailbox worker (``outlook_local``), whether
    that worker is still an ACTIVE mailbox of the configured sender binding (``None``: no mailbox
    or not checked). A revoked worker can never claim it, so the canary would only be stranded
    ``uncertain``.
    """
    from suv_deals.domain.inquiries import requires_message_approval
    from suv_deals.persistence.inquiries_repo import sender_binding_problems

    codes: list[str] = []
    if not confirmed:
        codes.append("OWNER_CONTROLLED_ADDRESS_NOT_CONFIRMED")
    if not settings.seller_email_canary_send_enabled:
        codes.append("CANARY_SEND_SWITCH_OFF")
    if settings.seller_inquiry_mode != "automatic":
        codes.append("SELLER_INQUIRY_MODE_NOT_AUTOMATIC")
    if settings.seller_inquiry_kill_switch:
        codes.append("SELLER_INQUIRY_KILL_SWITCH_ON")
    if requires_message_approval(settings):
        codes.append("MESSAGE_APPROVAL_SETTING_ON")
    if controls is None:
        codes.append("CONTROLS_MISSING")
    else:
        if controls.mode != "automatic":
            codes.append("CONTROLS_MODE_NOT_AUTOMATIC")
        if controls.kill_switch:
            codes.append("KILL_SWITCH_ACTIVE")
    codes.extend(code.upper() for code in authorization_problems)
    codes.extend(code.upper() for code in sender_binding_problems(sender))
    codes.extend(f"SENDER_IDENTITY_{code}" for code in identity_problems if code != "SENDER_BINDING_MISSING")
    if canary is None:
        codes.append("CANARY_NOT_FOUND")
    else:
        if canary.state != "prepared":
            codes.append("CANARY_NOT_PREPARED")
        if sender is not None and canary.sender_binding_id != sender.id:
            codes.append("CANARY_SENDER_NOT_CONFIGURED")
        elif sender is not None and canary.sender_binding_version != sender.version:
            codes.append("CANARY_SENDER_VERSION_CHANGED")
        if canary.mailbox_binding_id is not None and mailbox_active is False:
            codes.append("CANARY_MAILBOX_NOT_ACTIVE")
    return list(dict.fromkeys(codes))


async def _send_gates(
    conn: Conn, actor: ActorContext, settings: Settings, canary_id: UUID, *, confirmed: bool
) -> tuple[list[str], CanaryRecord | None]:
    """Read every ``canary send`` gate in ONE transaction: ``(blockers, canary)``.

    ``canary send`` runs it twice: once before the target address is (re-)entered, and again in the
    claim transaction right after the controls row is locked, so a pause, a mode change, a revoked
    standing authorization, a revoked or changed sender binding, a revoked desktop mailbox or a
    cancelled canary committed in between always stops the send (time of check = time of use).
    """
    from suv_deals.clock import ensure_utc
    from suv_deals.errors import NotFound
    from suv_deals.persistence import canaries_repo, inquiries_repo, mail_workers_repo
    from suv_deals.persistence.database import db_now
    from suv_deals.workers.inquiry_handlers import configured_sender_binding, configured_sender_problems

    now = ensure_utc(await db_now(conn))
    controls = await inquiries_repo.get_controls(conn, actor)
    authorization = await inquiries_repo.current_authorization(conn, actor)
    sender = await configured_sender_binding(conn, actor, settings)
    try:
        canary: CanaryRecord | None = await canaries_repo.get_canary(conn, actor, canary_id)
    except NotFound:
        canary = None
    mailbox_active: bool | None = None
    if canary is not None and canary.mailbox_binding_id is not None:
        boxes = await mail_workers_repo.list_mailbox_health(
            conn, actor.workspace_id, mailbox_binding_id=canary.mailbox_binding_id
        )
        mailbox_active = any(
            box.binding_state == "active" and sender is not None and box.sender_binding_id == sender.id
            for box in boxes
        )
    blockers = canary_send_blockers(
        settings,
        confirmed=confirmed,
        controls=controls,
        authorization_problems=inquiries_repo.authorization_problems(authorization, now),
        sender=sender,
        identity_problems=configured_sender_problems(settings, sender) if sender else [],
        canary=canary,
        mailbox_active=mailbox_active,
    )
    return blockers, canary


def read_target_address(environ: Mapping[str, str] | None = None) -> str:
    """The owner-controlled test address: ``SUV_CANARY_TARGET_ADDRESS`` or a hidden prompt.

    Never echoed (``hide_input``) and never part of any output or error message.
    """
    env = os.environ if environ is None else environ
    value = (env.get(CANARY_TARGET_ENV) or "").strip()
    if value:
        return value
    if stdin_is_tty():
        prompted = click.prompt(
            "Owner-controlled test address (input hidden; never a seller)",
            hide_input=True,
            confirmation_prompt=True,
        )
        if isinstance(prompted, str) and prompted.strip():
            return prompted.strip()
    fail(
        f"no target address: set {CANARY_TARGET_ENV} for this one command (then unset it) or run the"
        " command interactively",
        EXIT_USAGE,
    )


def _refuse(codes: Sequence[str], summary: str) -> NoReturn:
    """Print every blocker code, then stop with exit code 3 (nothing sent, nothing changed)."""
    for code in codes:
        echo(f"  blocked: {code}")
    fail(f"refused: {summary}; nothing was sent and nothing changed ({', '.join(codes)})", EXIT_REFUSED)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


@click.group("canary")
def canary_group() -> None:
    """Owner-controlled activation canary (prepare, status, cancel; send is the owner's step)."""


@canary_group.command("prepare")
@workspace_option
@click.option("--purpose", required=True, help="Why (3-500 characters, no address; stored and shown).")
@click.option(
    "--mailbox-binding",
    "mailbox_binding",
    type=click.UUID,
    default=None,
    help="outlook_local: the sender's mailbox worker (default: its only active one).",
)
@click.option("--yes", is_flag=True, help="Confirm recording the canary (nothing is sent).")
@pass_cli
def canary_prepare(
    cli: CliContext, *, workspace: UUID | None, purpose: str, mailbox_binding: UUID | None, yes: bool
) -> None:
    """Record a prepared canary for the configured sender (target stored as a hash only)."""
    settings = load_settings(cli)
    echo("Activation canary to prepare (nothing is sent by this command):")
    echo("  sender  : the configured sender binding (SELLER_EMAIL_PROVIDER/_ACCOUNT_ID/_FROM)")
    echo(f"  target  : read from {CANARY_TARGET_ENV} or a hidden prompt; stored as SHA-256 only")
    require_yes(yes, "canary prepare")
    target = read_target_address()

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.errors import RateLimited, ValidationFailed
        from suv_deals.persistence import canaries_repo
        from suv_deals.persistence.transactions import unit_of_work
        from suv_deals.workers.inquiry_handlers import configured_sender_binding, configured_sender_problems

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "canary-prepare", admin=True)
            try:
                async with unit_of_work(db, actor) as conn:
                    sender = await configured_sender_binding(conn, actor, settings)
                    problems = configured_sender_problems(settings, sender)
                    if sender is None or problems:
                        fail(
                            "no configured sender binding to test (nothing recorded): "
                            + ", ".join(problems or ["SENDER_BINDING_MISSING"]),
                            EXIT_REFUSED,
                        )
                    record = await canaries_repo.create_canary(
                        conn,
                        actor,
                        sender_binding_id=sender.id,
                        target_address=target,
                        purpose=purpose,
                        mailbox_binding_id=mailbox_binding,
                    )
            # The repository's safety refusals are refusals (exit 3, nothing recorded), never a
            # generic problem: the kill switch, and the rolling 24-hour canary cap with its wait.
            except RateLimited as exc:
                cap = canaries_repo.MAX_CANARIES_PER_24H
                wait = "the window" if exc.retry_after_seconds is None else f"{exc.retry_after_seconds} s"
                fail(
                    f"refused: {exc.message} (at most {cap} per rolling 24 hours; retry after {wait});"
                    " nothing recorded",
                    EXIT_REFUSED,
                )
            except ValidationFailed as exc:
                if (exc.details or {}).get("reason") != "kill_switch_active":
                    raise
                fail(f"refused: {exc.message}; nothing recorded (resume first)", EXIT_REFUSED)
        binding = f"sender binding v{record.sender_binding_version}"
        echo(f"Prepared canary {record.id} ({record.provider.value}, {binding}).")
        echo(f"  Message-ID : {record.rfc_message_id}")
        echo("  target     : stored as a SHA-256 hash only (never shown)")
        echo(
            "Next (the owner's activation step, docs/seller_email_activation.md section 8): "
            f"`suv-deals canary send {record.id} {CONFIRM_FLAG} --yes`."
        )
        return 0

    run_async(body)


@canary_group.command("status")
@workspace_option
@click.option("--limit", type=click.IntRange(1, MAX_STATUS_ROWS), default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def canary_status(cli: CliContext, workspace: UUID | None, limit: int, as_json: bool) -> None:
    """Canaries and the activation-evidence state (never the address or its hash)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import canaries_repo
        from suv_deals.persistence.transactions import unit_of_work
        from suv_deals.workers.inquiry_handlers import configured_sender_binding, configured_sender_problems

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "canary-status")
            async with unit_of_work(db, actor) as conn:
                records = await canaries_repo.list_canaries(conn, actor, limit=limit)
                sender = await configured_sender_binding(conn, actor, settings)
        usable = sender is not None and not configured_sender_problems(settings, sender)
        state, detail = canary_evidence(records, sender if usable else None)
        rows: list[dict[str, Any]] = [
            {
                "id": str(r.id),
                "provider": r.provider.value,
                "binding_version": r.sender_binding_version,
                "state": r.state,
                "created_at": r.created_at.isoformat(),
                "accepted_at": None if r.accepted_at is None else r.accepted_at.isoformat(),
                "reply_recorded_at": None if r.reply_recorded_at is None else r.reply_recorded_at.isoformat(),
            }
            for r in records
        ]
        if as_json:
            emit_json({"evidence": state, "detail": detail, "canaries": rows})
            return 0
        echo(f"Activation canary evidence: {state} ({detail})")
        echo(table(rows, list(rows[0])) if rows else "No canaries.")
        return 0

    run_async(body)


@canary_group.command("cancel")
@click.argument("canary_id", type=click.UUID)
@workspace_option
@click.option("--reason", required=True, help="Why (3-500 characters, no address; audited).")
@click.option("--yes", is_flag=True, help="Confirm cancelling the prepared canary.")
@pass_cli
def canary_cancel(
    cli: CliContext, canary_id: UUID, *, workspace: UUID | None, reason: str, yes: bool
) -> None:
    """Cancel a prepared canary that will not be sent (it still counts against the 24-hour cap)."""
    settings = load_settings(cli)
    require_yes(yes, "canary cancel")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import canaries_repo
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "canary-cancel", admin=True)
            async with unit_of_work(db, actor) as conn:
                record = await canaries_repo.cancel_canary(conn, actor, canary_id, reason=reason)
        echo(f"Canary {record.id}: {record.state} (version {record.version}).")
        return 0

    run_async(body)


@canary_group.command("send")
@click.argument("canary_id", type=click.UUID)
@workspace_option
@click.option(
    "--i-confirm-owner-controlled-address",
    "confirmed",
    is_flag=True,
    help="Required: the target is an address the OWNER controls (never a seller).",
)
@click.option("--yes", is_flag=True, help="Confirm the one-time activation send.")
@pass_cli
def canary_send(
    cli: CliContext, canary_id: UUID, *, workspace: UUID | None, confirmed: bool, yes: bool
) -> None:
    """The owner's activation step: send ONE prepared canary when every activation gate is open."""
    if not confirmed:
        fail(
            f"canary send needs {CONFIRM_FLAG} (the target must be an owner-controlled address)",
            EXIT_REFUSED,
        )
    settings = load_settings(cli)
    echo(f"Activation canary to send: {canary_id} (one message to the owner-controlled test address)")
    require_yes(yes, "canary send")

    async def body() -> int:
        from uuid import uuid4

        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.errors import VersionConflict
        from suv_deals.persistence import canaries_repo
        from suv_deals.persistence.sellers_repo import lock_controls
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "canary-send", admin=True)
            async with unit_of_work(db, actor) as conn:
                blockers, canary = await _send_gates(conn, actor, settings, canary_id, confirmed=confirmed)
            if blockers or canary is None:
                _refuse(blockers, "an activation gate is closed")
            # Outside any transaction (a hidden prompt may wait for the owner).
            target = read_target_address()
            if canaries_repo.target_address_hash(target) != canary.target_address_hash:
                _refuse(["CANARY_TARGET_MISMATCH"], "the address is not the canary's target")
            # Immediately before the transport, in ONE committed transaction: the controls row is
            # locked and EVERY gate is read again (`_send_gates`): a pause, a mode change, a revoked
            # authorization, sender binding or desktop mailbox, or a cancel committed since the
            # first read stops it; a pause or authorization change racing this transaction waits
            # for the lock. Then the canary moves ``prepared -> uncertain`` with a one-time send
            # token (spec 37.5: the attempt is durably committed BEFORE the external I/O). A crash
            # in or after the hand-over therefore leaves it ``uncertain`` (never re-sent: only a
            # ``prepared`` canary passes the gates), and a second ``canary send`` racing this one
            # finds another token and is refused.
            send_token = uuid4().hex
            async with unit_of_work(db, actor) as conn:
                if await lock_controls(conn, workspace_id) is None:
                    _refuse(["CONTROLS_MISSING"], "the inquiry controls are missing")
                again, fresh = await _send_gates(conn, actor, settings, canary_id, confirmed=confirmed)
                if again or fresh is None:
                    _refuse(again or ["CANARY_NOT_FOUND"], "an activation gate closed meanwhile")
                if fresh.target_address_hash != canary.target_address_hash:  # pragma: no cover - frozen
                    _refuse(["CANARY_TARGET_MISMATCH"], "the canary changed meanwhile")
                transport = CANARY_TRANSPORTS.get(canary.provider.value)
                if transport is None:
                    _refuse(
                        ["CANARY_TRANSPORT_UNAVAILABLE"],
                        f"every other gate is open, but {canary.provider.value} has no canary transport"
                        " yet (docs/seller_email_activation.md section 8)",
                    )
                try:
                    claimed: CanaryRecord | None = await canaries_repo.record_canary_outcome(
                        conn,
                        actor,
                        canary.id,
                        outcome="uncertain",
                        evidence={"phase": "transport_started", "send_token": send_token},
                        expected_version=canary.version,
                    )
                except VersionConflict:
                    claimed = None  # cancelled or changed since the gates were read
                if claimed is None or claimed.outcome_evidence.get("send_token") != send_token:
                    _refuse(["CANARY_NOT_PREPARED"], "the canary changed meanwhile (another send, a cancel)")
            try:
                outcome, evidence = await transport(claimed, target)
            except Exception:
                # The transport may have handed the message over before failing: never "failed".
                # The canary simply stays ``uncertain`` (committed above) for reconciliation.
                outcome, evidence = "uncertain", {"transport_error": "exception"}
            async with unit_of_work(db, actor) as conn:
                record = await canaries_repo.record_canary_outcome(
                    conn, actor, canary.id, outcome=outcome, evidence=evidence
                )
        echo(f"Canary {record.id}: {record.state} (recorded; reconcile its Message-ID next).")
        return 0

    run_async(body)


__all__ = [
    "CANARY_TARGET_ENV",
    "CANARY_TRANSPORTS",
    "CONFIRM_FLAG",
    "CanaryTransport",
    "canary_evidence",
    "canary_group",
    "canary_send_blockers",
    "read_target_address",
]
