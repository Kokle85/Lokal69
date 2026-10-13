"""``suv-deals import-costs estimate`` (fit design 6; docs/tax_rule_approval.md).

The import-cost calculator for one vehicle: customs duty, motor-vehicle tax and import VAT on
import into North Macedonia, from a listing's facts (``--listing-id``), the facts given here, or
both (a fact given here overrides the listing's). The read service is the one behind the MCP
tool ``deals_estimate_import_costs`` and the dashboard (``persistence.queries.import_costs``).

- Read-only (scope ``deals:read``, the operator's system actor): no valuation, cost line, alert,
  rate or audit row is written, so there is no ``--yes``.
- Rules: an ACTIVE, owner-approved rule set when one applies (``mode: active``); otherwise an
  INDICATIVE PREVIEW of the newest unapproved version, shown with the MK/EN banner, totals named
  ``indicative_total`` and never production-ready; otherwise ``unavailable``.
- Unknown is never zero: a missing fact leaves the dependent charges unknown and is listed under
  "missing facts". Nothing is inferred: no proof of origin unless ``--origin-proof`` names one
  (standard duty), no tariff code unless ``--hs-code`` is given, no WLTP <-> NEDC conversion.
- Amounts are exact decimals (``3000`` or ``3000.50``, never ``3000,50``). No rate, coefficient or
  threshold lives here; this module only collects facts and prints the shared view.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import click

from suv_deals.cli_commands._common import (
    EXIT_USAGE,
    CliContext,
    echo,
    emit_json,
    fail,
    load_settings,
    pass_cli,
    run_async,
    safe,
    workspace_option,
)
from suv_deals.domain.enums import Co2Cycle, Fuel

if TYPE_CHECKING:
    from suv_deals.persistence.queries.import_costs import ImportCostRequest
    from suv_deals.views.common import AmountView, ResponseWarning
    from suv_deals.views.import_costs import ImportCostEstimateView, ImportScenarioView

#: The closed vocabularies of ``domain.import_estimate`` (kept equal by tests/cli; repeated here
#: so ``--help`` does not import the domain).
PRICE_CURRENCIES: Final = ("EUR", "MKD", "CHF")
PRICE_BASES: Final = ("invoice", "advertised")
CO2_DOCUMENTS: Final = (
    "coc",
    "registration_consent",
    "manufacturer_certificate",
    "technical_assessment_certificate",
    "listing_claim_unverified",
    "none_available",
)
ORIGIN_PROOF_KINDS: Final = ("none", "unknown", "eu", "efta", "tr", "uk", "cefta", "ua", "us")
IMPORTER_STATUSES: Final = ("private", "non_vat_business", "vat_dealer")

_DECIMAL_RE: Final = re.compile(r"^[0-9]{1,15}(\.[0-9]{1,10})?$")
_PARTIAL_DATE_RE: Final = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_PRECISION_BY_LENGTH: Final = {4: "year", 7: "month", 10: "day"}
#: Vehicle facts passed through unchanged when given (option name == fact name).
_PLAIN_FACTS: Final = (
    "price_basis",
    "fuel",
    "co2_document",
    "emissions_class",
    "hs_code",
    "hs_evidence_ref",
    "transport_after_entry_eur",
    "catalogue_value_eur",
    "importer_status",
    "mileage_km",
    "engine_displacement_cm3",
    "power_kw",
)
_CATEGORY_LABELS: Final = {
    "import_duty": "customs duty",
    "motor_vehicle_tax": "motor-vehicle tax (ДМВ)",
    "import_vat": "import VAT",
    "other_import_charges": "other import charges",
}
_LABEL_WIDTH: Final = 30


class _Decimal(click.ParamType[Decimal]):
    """An exact, non-negative decimal (digits with an optional ``.`` fraction)."""

    name = "DECIMAL"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Decimal:
        if isinstance(value, Decimal):
            return value
        text = str(value).strip()
        if not _DECIMAL_RE.fullmatch(text):
            self.fail(f"{text!r} is not a plain decimal (e.g. 3000 or 3000.50; never a comma)", param, ctx)
        return Decimal(text)


class _Co2Reading(click.ParamType[tuple[str, Decimal]]):
    """``CYCLE:G_KM`` exactly as printed on the document (e.g. ``nedc:189``)."""

    name = "CYCLE:G_KM"

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> tuple[str, Decimal]:
        cycle, separator, figure = str(value).partition(":")
        cycle, figure = cycle.strip().lower(), figure.strip()
        cycles = [c.value for c in Co2Cycle]
        if not separator or cycle not in cycles or not _DECIMAL_RE.fullmatch(figure):
            self.fail(
                f"{value!r} is not CYCLE:G_KM with CYCLE one of {', '.join(cycles)} (e.g. nedc:189)",
                param,
                ctx,
            )
        return cycle, Decimal(figure)


DECIMAL: Final = _Decimal()
CO2_READING: Final = _Co2Reading()


def _country(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise click.BadParameter("a two-letter country code (e.g. DE)")
    return code


def _partial_date(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not _PARTIAL_DATE_RE.fullmatch(value.strip()):
        raise click.BadParameter("YYYY, YYYY-MM or YYYY-MM-DD (never invented beyond what is known)")
    return None if value is None else value.strip()


# --------------------------------------------------------------------------- request


def vehicle_facts(options: Mapping[str, Any]) -> dict[str, Any]:
    """The request's ``vehicle`` object: only the facts given on the command line, so a fact left
    out keeps the listing's value or stays unknown (never a default figure)."""
    vehicle: dict[str, Any] = {}
    if options["price"] is not None:
        vehicle["price"] = {"amount": options["price"], "currency": options["price_currency"] or "EUR"}
    elif options["price_currency"] is not None:
        fail("--price-currency needs --price", EXIT_USAGE)
    for name in _PLAIN_FACTS:
        if options[name] is not None:
            vehicle[name] = options[name]
    if options["declaration_date"] is not None:
        day: datetime = options["declaration_date"]
        vehicle["declaration_date"] = day.date()
    if options["co2"]:
        vehicle["co2"] = [{"cycle": cycle, "g_km": g_km} for cycle, g_km in options["co2"]]
    if options["co2_round_up_confirmed"]:
        vehicle["co2_round_up_confirmed"] = True
    if options["separately_invoiced"]:
        vehicle["transport_after_entry_separately_invoiced"] = True
    if options["first_registration"] is not None:
        value: str = options["first_registration"]
        vehicle["first_registration"] = {"value": value, "precision": _PRECISION_BY_LENGTH[len(value)]}
    if options["seller_country"] is not None:
        vehicle["seller_country"] = options["seller_country"]
    proof = options["origin_proof"]
    if proof is not None:
        vehicle["origin_proof"] = {
            "kind": proof,
            "country": options["origin_country"],
            "evidence_ref": options["origin_evidence_ref"],
        }
    elif options["origin_country"] is not None or options["origin_evidence_ref"] is not None:
        fail("--origin-country / --origin-evidence-ref need --origin-proof KIND", EXIT_USAGE)
    return vehicle


def build_request(options: Mapping[str, Any]) -> ImportCostRequest:
    """The service's ``ImportCostRequest`` (validated; a usage error names each bad field)."""
    from pydantic import ValidationError

    from suv_deals.persistence.queries.import_costs import ImportCostRequest as Request

    vehicle = vehicle_facts(options)
    data: dict[str, Any] = {
        "listing_id": options["listing_id"],
        "vehicle": vehicle or None,
        "customs_eur_mkd_rate": options["customs_eur_mkd_rate"],
        "accept_previous_month_fx": bool(options["accept_previous_month_fx"]),
    }
    try:
        return Request.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in err['loc']) or 'request'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
        fail("invalid estimate request:\n  " + "\n  ".join(lines[:20]), EXIT_USAGE)


# --------------------------------------------------------------------------- output


def _amount(view: AmountView) -> str:
    if view.status == "known":
        return f"{view.amount} {view.currency}"
    if view.status == "not_applicable":
        return "not applicable"
    return "unknown" + (f" ({view.reason})" if view.reason else "")


def _pair(local: AmountView, eur: AmountView | None) -> str:
    """``<amount> MKD  (<amount> EUR)``; an unknown amount is shown once, with its reason."""
    if local.status != "known" or eur is None or local.currency == "EUR":
        return _amount(local)
    return f"{_amount(local)}  ({eur.amount} EUR)" if eur.status == "known" else _amount(local)


def _line(label: str, value: str, indent: int = 2) -> str:
    return f"{' ' * indent}{label:<{_LABEL_WIDTH - indent}}: {safe(value)}"


def _bullets(title: str, items: Sequence[str]) -> list[str]:
    return [f"{title}:", *(f"  - {safe(item)}" for item in items)] if items else []


def _facts_lines(view: ImportCostEstimateView) -> list[str]:
    facts = view.facts
    sources = {s.fact: s.source for s in view.fact_sources}

    def tagged(label: str, value: str, *facts_used: str) -> str:
        origin = sorted({sources[f] for f in facts_used if f in sources})
        return _line(label, value + (f"  [{', '.join(origin)}]" if origin else ""), indent=4)

    co2 = ", ".join(f"{r.cycle} {r.g_km} g/km" for r in facts.co2) or "unknown"
    proof = facts.origin_proof_kind + (
        f" ({facts.origin_proof_country})" if facts.origin_proof_country else ""
    )
    if facts.origin_proof_evidence_ref:
        proof += f", evidence {facts.origin_proof_evidence_ref}"
    lines = [
        "  facts used [source]:",
        tagged("price", f"{_amount(facts.price)} ({facts.price_basis})", "price"),
        tagged("fuel", facts.fuel.value, "fuel"),
        tagged("CO2", co2, "co2"),
        tagged("CO2 document", facts.co2_document or "not stated", "co2_document"),
        tagged("origin proof", proof, "origin_proof"),
        tagged("HS code", facts.hs_code or "not given (never inferred)", "hs_code"),
        tagged("first registration", facts.first_registration or "unknown", "first_registration"),
        tagged("emissions class", facts.emissions_class or "unknown", "emissions_class"),
    ]
    optional = (
        ("inland leg (separate)", facts.transport_after_entry_eur, " EUR", "transport_after_entry_eur"),
        ("catalogue value", facts.catalogue_value_eur, " EUR", "catalogue_value_eur"),
        ("importer status", facts.importer_status, "", "importer_status"),
        ("mileage", facts.mileage_km, " km", "mileage_km"),
        ("seller country", facts.seller_country, "", "seller_country"),
        ("engine displacement", facts.engine_displacement_cm3, " cm3", "engine_displacement_cm3"),
        ("power", facts.power_kw, " kW", "power_kw"),
    )
    lines += [tagged(label, f"{value}{unit}", name) for label, value, unit, name in optional if value]
    return lines


def _scenario_lines(scenario: ImportScenarioView, *, active: bool, verbose: bool) -> list[str]:
    lines = [f"Scenario {scenario.name}: {safe(scenario.label)}"]
    basis = f"  ({scenario.customs_value_basis})" if scenario.customs_value_basis else ""
    lines.append(_line("customs value", _amount(scenario.customs_value) + basis))
    categories = {c.category.value: c for c in scenario.categories}
    for key, label in _CATEGORY_LABELS.items():
        row = categories.get(key)
        if row is not None:
            lines.append(_line(label, _pair(row.amount, row.amount_eur)))
    prefix = "" if active else "indicative_"
    total = scenario.total if active else scenario.indicative_total
    total_eur = scenario.total_eur if active else scenario.indicative_total_eur
    landed = scenario.landed_cost_eur if active else scenario.indicative_landed_cost_eur
    if total is not None:
        lines.append(_line(f"{prefix}total", _pair(total, total_eur)))
    if landed is not None:
        lines.append(_line(f"{prefix}landed_cost_eur", _amount(landed)))
    if scenario.known_subtotal is not None:
        lines.append(_line("known_subtotal", _amount(scenario.known_subtotal) + "  (NOT a total)"))
    lines.append(_line("complete", "yes" if scenario.complete else "no"))
    if scenario.unknown_components:
        lines.append(_line("unknown components", ", ".join(scenario.unknown_components)))
    if scenario.missing_inputs:
        lines.append(_line("missing inputs", ", ".join(scenario.missing_inputs)))
    lines += [f"  {line}" for line in _bullets("assumptions", scenario.assumptions)]
    if verbose:
        lines.append("  components (helpers are shown, never counted in a total):")
        for comp in scenario.components:
            helper = "  [helper, not counted]" if comp.intermediate else ""
            lines.append(f"    {comp.component_id:<28} {comp.status:<15} {_amount(comp.amount)}{helper}")
    return lines


def render_estimate(
    view: ImportCostEstimateView, warnings: Sequence[ResponseWarning] = (), *, verbose: bool = False
) -> list[str]:
    """The human-readable estimate (``import-costs estimate`` without ``--json``)."""
    active = view.mode == "active"
    ready = "yes" if view.production_ready else "no"
    lines = [f"IMPORT-COST ESTIMATE  mode: {view.mode}  production-ready: {ready}"]
    if view.banner is not None:
        lines += [f"  !! {view.banner.mk}", f"  !! {view.banner.en}"]
    lines.append(f"  {view.usage_note}")
    if warnings:
        lines.append(_line("warning codes", ", ".join(dict.fromkeys(w.code.value for w in warnings))))
    rule = view.rule
    if rule is not None:
        hashed = "recorded" if rule.sha256_recorded else "computed; an unsealed draft has no recorded hash"
        lines.append(_line("rule set", f"{rule.rule_set_id}@{rule.version} ({rule.status.value})"))
        lines.append(_line("valid", f"{rule.valid_from or 'unset'} to {rule.valid_to or 'open'}"))
        lines.append(_line("content sha256", f"{rule.content_sha256} ({hashed})"))
    lines.append(_line("selection", view.selection_reason))
    lines.append(_line("jurisdiction", f"{view.jurisdiction} {view.vehicle_category}"))
    lines.append(_line("declaration date", view.declaration_date))
    rate = view.customs_fx_rate
    fx = "none" if rate is None else f"{rate.direction} ({rate.rate_date}, {rate.provider})"
    lines.append(_line("customs FX", f"{fx}; source {view.customs_fx_source}"))
    if view.listing is not None:
        listing = view.listing
        label = f"{listing.listing_id} revision {listing.revision_number} ({listing.source_key})"
        label += "" if listing.screened else ", not screened"
        lines.append(
            _line("listing", label + (f"  {listing.fixture_label}" if listing.fixture_label else ""))
        )
    gates = view.gates
    lines.append(_line("import gate", f"importable {gates.importable}: {gates.reason}"))
    lines += _facts_lines(view)
    for scenario in view.scenarios:
        lines += ["", *_scenario_lines(scenario, active=active, verbose=verbose)]
    if view.scenarios:
        lines += ["", f"EUR amounts: {safe(view.eur_basis)}", f"Landed cost: {safe(view.landed_cost_note)}"]
    lines.append("")
    if view.missing_facts:
        lines.append(f"Missing facts: {', '.join(view.missing_facts)}")
    lines += _bullets("Unverified inputs (claims, not evidence)", view.unverified_inputs)
    lines += _bullets("Assumptions", view.assumptions)
    lines += _bullets("Warnings", view.warnings)
    applying = [item for item in view.uncertain_items if item.applies]
    shown = list(view.uncertain_items) if verbose else applying
    if shown:
        lines.append("Uncertain items (UNCERTAIN - confirm before approval):")
        for item in shown:
            where = item.component_id or "rule set"
            lines.append(f"  - [{'applies' if item.applies else 'n/a'}] {where}: {safe(item.text)}")
    hidden = len(view.uncertain_items) - len(shown)
    if hidden:
        lines.append(f"  (+{hidden} uncertain item(s) not applying to this vehicle; -v lists them)")
    lines += _bullets("Notes", view.notes)
    return lines


# --------------------------------------------------------------------------- command


@click.group("import-costs")
def import_costs_group() -> None:
    """Import-cost calculator: customs duty, motor-vehicle tax and import VAT (read-only)."""


@import_costs_group.command("estimate")
@workspace_option
@click.option(
    "--listing-id", type=click.UUID, default=None, help="Use this listing's facts (current revision)."
)
@click.option("--price", type=DECIMAL, default=None, help="Purchase price (exact decimal).")
@click.option(
    "--price-currency",
    type=click.Choice(PRICE_CURRENCIES),
    default=None,
    help="Currency of --price (EUR when omitted; a CHF price leaves the customs value unknown).",
)
@click.option(
    "--price-basis",
    type=click.Choice(PRICE_BASES),
    default=None,
    help="invoice (contract price) or advertised (the default for a price given here).",
)
@click.option(
    "--declaration-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="YYYY-MM-DD (default: today).",
)
@click.option("--fuel", type=click.Choice([f.value for f in Fuel]), default=None, help="Fuel type.")
@click.option(
    "--co2",
    type=CO2_READING,
    multiple=True,
    help="CYCLE:G_KM as printed on the document, e.g. nedc:189 (repeat per cycle; never converted).",
)
@click.option(
    "--co2-document",
    type=click.Choice(CO2_DOCUMENTS),
    default=None,
    help="Where the CO2 figure comes from (none_available only when no document exists).",
)
@click.option(
    "--co2-round-up-confirmed",
    is_flag=True,
    help="A non-integer CO2 figure may be rounded up (otherwise its ДМВ part stays unknown).",
)
@click.option(
    "--first-registration", default=None, callback=_partial_date, help="YYYY, YYYY-MM or YYYY-MM-DD."
)
@click.option("--emissions-class", default=None, help="As stated, e.g. 'Euro 5' (drives the import gate).")
@click.option(
    "--origin-proof",
    type=click.Choice(ORIGIN_PROOF_KINDS),
    default=None,
    help="Proof of origin HELD (default none: standard duty; unknown leaves the duty unknown).",
)
@click.option(
    "--origin-country",
    default=None,
    callback=_country,
    help="Origin country the proof states (required for efta/cefta).",
)
@click.option("--origin-evidence-ref", default=None, help="Reference of the proof document.")
@click.option(
    "--hs-code", default=None, help="Tariff code from a broker or ruling (HS 8703 only; never inferred)."
)
@click.option("--hs-evidence-ref", default=None, help="Evidence for --hs-code.")
@click.option(
    "--transport-after-entry-eur",
    type=DECIMAL,
    default=None,
    help="Inland leg after the MK border, only when separately invoiced (needs --separately-invoiced).",
)
@click.option(
    "--separately-invoiced",
    is_flag=True,
    help="Confirms the inland leg is invoiced separately (it then leaves the customs value).",
)
@click.option(
    "--catalogue-value-eur",
    type=DECIMAL,
    default=None,
    help="Catalogue value; adds the customs revaluation RISK scenario.",
)
@click.option("--importer-status", type=click.Choice(IMPORTER_STATUSES), default=None, help="Who imports.")
@click.option("--mileage-km", type=DECIMAL, default=None, help="Odometer reading in km.")
@click.option(
    "--seller-country", default=None, callback=_country, help="Seller country (never implies origin)."
)
@click.option("--engine-displacement-cm3", type=DECIMAL, default=None, help="Engine displacement in cm3.")
@click.option("--power-kw", type=DECIMAL, default=None, help="Engine power in kW.")
@click.option(
    "--customs-eur-mkd-rate",
    type=DECIMAL,
    default=None,
    help="1 EUR = RATE MKD from the Customs monthly list; used only when none is recorded for the "
    "declaration month (labelled, never stored).",
)
@click.option(
    "--accept-previous-month-fx",
    is_flag=True,
    help="Preview only: accept last month's recorded customs rate (labelled assumption).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (the MCP/API view).")
@click.option("-v", "--verbose", is_flag=True, help="Also list every rule component and uncertain item.")
@pass_cli
def estimate(
    cli: CliContext, /, *, workspace: UUID | None, as_json: bool, verbose: bool, **options: Any
) -> None:
    """Estimate customs duty, motor-vehicle tax (ДМВ) and import VAT for one vehicle (read-only).

    Give --listing-id, vehicle facts or both. Without an ACTIVE, owner-approved rule set the
    result is an INDICATIVE PREVIEW of the unapproved draft (never production-ready, never stored,
    never used for valuations or alerts). Missing facts stay unknown, never zero.
    """
    request = build_request(options)
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence.queries.import_costs import get_import_cost_estimate
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "import-costs-estimate")
            async with unit_of_work(db, actor) as conn:
                result = await get_import_cost_estimate(conn, actor, request, config_dir=settings.config_dir)
        if as_json:
            emit_json(
                {
                    "as_of": result.as_of.isoformat(),
                    "data": result.data.model_dump(mode="json"),
                    "warnings": [w.model_dump(mode="json") for w in result.warnings],
                }
            )
        else:
            for line in render_estimate(result.data, result.warnings, verbose=verbose):
                echo(line)
        return 0

    run_async(body)


__all__ = [
    "CO2_DOCUMENTS",
    "IMPORTER_STATUSES",
    "ORIGIN_PROOF_KINDS",
    "PRICE_BASES",
    "PRICE_CURRENCIES",
    "build_request",
    "estimate",
    "import_costs_group",
    "render_estimate",
    "vehicle_facts",
]
