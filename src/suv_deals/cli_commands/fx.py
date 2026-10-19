"""``suv-deals fx refresh | record | status`` (spec 18 "FX rules"; OPS-06, wave D2).

- ``refresh``: one gated fetch of the ECB daily reference rates (``FX_FETCH_ENABLED=true``; HTTPS
  to the allow-listed ECB host only) and the EUR->CHF rate recorded in each active workspace.
  The reconciler does the same automatically at most every 6 hours while the switch is on.
- ``record``: an owner-entered rate (for example from the bank or an approved MKD source) with
  its date, provider, purpose and source reference; stored once and audited (``fx_rate.record``).
  ``ECB`` is reserved for fetched rows. Exact decimals only.
- ``status``: the newest stored rate per pair/purpose and its age (read-only).

Every state-changing command needs ``--yes``. Nothing here assumes a CHF or MKD parity.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final
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

_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")
_RATE_RE: Final = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,12})?$")
_PROVIDER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
#: Pairs ``status`` reports (both purposes the valuation uses are shown when present).
STATUS_PAIRS: Final = (("EUR", "CHF"), ("EUR", "MKD"))


@click.group("fx")
def fx_group() -> None:
    """Exchange rates: gated ECB refresh, owner-entered rates, status (spec 18)."""


@fx_group.command("refresh")
@workspace_option
@click.option("--yes", is_flag=True, help="Confirm the fetch and the database write.")
@pass_cli
def fx_refresh(cli: CliContext, workspace: UUID | None, yes: bool) -> None:
    """Fetch the ECB daily reference rates once and record EUR->CHF (needs FX_FETCH_ENABLED=true)."""
    settings = load_settings(cli)
    if not settings.fx_fetch_enabled:
        fail("FX fetching is disabled (FX_FETCH_ENABLED=false); nothing was fetched", EXIT_PROBLEMS)
    require_yes(yes, "fx refresh")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, resolve_workspaces
        from suv_deals.workers.fx_refresh import default_fx_client, refresh_ecb_rates

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspaces = await resolve_workspaces(db, workspace)
            result = await refresh_ecb_rates(settings, db, default_fx_client(), workspaces)
        echo(f"ECB rate date(s): {', '.join(result.rate_dates)} (sha256 {result.sha256[:12]}...)")
        for workspace_id, count in result.recorded.items():
            echo(f"  {workspace_id}: {count} new rate(s) recorded")
        return 0

    run_async(body)


def _currency(value: str, name: str) -> str:
    code = value.strip().upper()
    if not _CURRENCY_RE.fullmatch(code):
        fail(f"{name} must be a three-letter ISO currency code", EXIT_USAGE)
    return code


def _rate(value: str) -> Decimal:
    text = value.strip()
    if not _RATE_RE.fullmatch(text):
        fail("--rate must be a positive decimal such as 0.9412 (at most 12 decimals)", EXIT_USAGE)
    try:
        rate = Decimal(text)
    except InvalidOperation:  # pragma: no cover - guarded by the pattern
        fail("--rate must be a decimal", EXIT_USAGE)
    if rate <= 0:
        fail("--rate must be positive", EXIT_USAGE)
    return rate


@fx_group.command("record")
@workspace_option
@click.option("--base", required=True, help="Base currency: 1 BASE = RATE QUOTE (e.g. EUR).")
@click.option("--quote", required=True, help="Quote currency (e.g. CHF).")
@click.option("--rate", "rate_text", required=True, help="Exact decimal, e.g. 0.9412.")
@click.option("--date", "rate_date", required=True, type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option(
    "--purpose",
    type=click.Choice(["reference", "payment", "customs"]),
    default="reference",
    show_default=True,
    help="Reference (estimation), payment (bank) or customs (prescribed) rate; never mixed.",
)
@click.option("--provider", default="owner", show_default=True, help="Who published the rate (not ECB).")
@click.option("--source-ref", required=True, help="Where the rate comes from (document, URL or note).")
@click.option("--reason", required=True, help="Why it is entered by hand (audited).")
@click.option("--yes", is_flag=True, help="Confirm the database write.")
@pass_cli
def fx_record(
    cli: CliContext,
    *,
    workspace: UUID | None,
    base: str,
    quote: str,
    rate_text: str,
    rate_date: Any,
    purpose: str,
    provider: str,
    source_ref: str,
    reason: str,
    yes: bool,
) -> None:
    """Record one owner-entered FX rate (audited; the same date/provider/purpose is stored once)."""
    settings = load_settings(cli)
    base_code, quote_code = _currency(base, "--base"), _currency(quote, "--quote")
    if base_code == quote_code:
        fail("--base and --quote must differ", EXIT_USAGE)
    rate = _rate(rate_text)
    day: date = rate_date.date()
    if day > datetime.now(UTC).date():
        fail("--date must not be in the future", EXIT_USAGE)
    label = provider.strip()
    if not _PROVIDER_RE.fullmatch(label):
        fail("--provider must be 1-100 letters, digits, spaces, '.', '_' or '-'", EXIT_USAGE)
    if label.upper() == "ECB":
        fail("ECB is reserved for fetched rates; use `suv-deals fx refresh`", EXIT_USAGE)
    reference = source_ref.strip()
    if not 1 <= len(reference) <= 500:
        fail("--source-ref must be 1-500 characters", EXIT_USAGE)
    why = reason.strip()
    if not 3 <= len(why) <= 500:
        fail("--reason must be 3-500 characters", EXIT_USAGE)
    require_yes(yes, "fx record")

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.domain.enums import FxPurpose
        from suv_deals.domain.money import FxRate
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work
        from suv_deals.workers.fx_refresh import record_rates

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "fx-record", admin=True)
            async with unit_of_work(db, actor) as conn:
                observation = FxRate(
                    base=base_code,
                    quote=quote_code,
                    rate=rate,
                    rate_date=day,
                    retrieved_at=await db_now(conn),
                    provider=label,
                    purpose=FxPurpose(purpose),
                )
                created = await record_rates(conn, actor, [observation], source_ref=reference, reason=why)
        state = "recorded" if created else "already recorded (identical observation)"
        echo(f"1 {base_code} = {rate} {quote_code} ({purpose}, {label}, {day.isoformat()}): {state}")
        return 0

    run_async(body)


@fx_group.command("status")
@workspace_option
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_cli
def fx_status(cli: CliContext, workspace: UUID | None, as_json: bool) -> None:
    """The newest stored rate per pair and purpose, with its age in days (read-only)."""
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspaces
        from suv_deals.domain.enums import FxPurpose
        from suv_deals.persistence import valuation_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work

        rows: list[dict[str, Any]] = []
        async with open_database(settings, application_name="suv-deals-cli") as db:
            for workspace_id in await resolve_workspaces(db, workspace):
                actor = operator_actor(workspace_id, "fx-status")
                async with unit_of_work(db, actor) as conn:
                    today = (await db_now(conn)).date()
                    for base_code, quote_code in STATUS_PAIRS:
                        for purpose in FxPurpose:
                            found = await valuation_repo.latest_fx_rates(
                                conn,
                                actor,
                                base=base_code,
                                quote=quote_code,
                                purpose=purpose,
                                on_or_before=today,
                                limit=1,
                            )
                            for stored in found:
                                rows.append(
                                    {
                                        "workspace_id": str(workspace_id),
                                        "pair": f"{base_code}/{quote_code}",
                                        "purpose": purpose.value,
                                        "rate": str(stored.rate.rate),
                                        "rate_date": stored.rate.rate_date.isoformat(),
                                        "age_days": (today - stored.rate.rate_date).days,
                                        "provider": stored.rate.provider,
                                    }
                                )
        if as_json:
            emit_json({"rates": rows, "fx_fetch_enabled": settings.fx_fetch_enabled})
        else:
            echo(f"FX_FETCH_ENABLED={'true' if settings.fx_fetch_enabled else 'false'}")
            columns = ["workspace_id", "pair", "purpose", "rate", "rate_date", "age_days", "provider"]
            echo(
                table(rows, columns)
                if rows
                else "No (non-fixture) rate stored: CH valuations need fx:CHF/EUR."
            )
        return 0

    run_async(body)


__all__ = ["fx_group"]
