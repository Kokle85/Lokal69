"""``suv-deals import-costs estimate | values | record-values`` (fit design 6; docs/tax_rule_approval.md).

``estimate`` is the import-cost calculator for one vehicle: customs duty, motor-vehicle tax and
import VAT on import into North Macedonia, from a listing's facts (``--listing-id``), the facts
given here, or both (a fact given here overrides the listing's). The read service is the one behind
the MCP tool ``deals_estimate_import_costs`` and the dashboard (``persistence.queries.import_costs``).
``values`` / ``record-values`` show and record a listing's two values (owner decision 2026-10-13,
below) through ``persistence.import_values_repo``, the store behind
``GET/POST /api/listings/{listing_id}/import-values``.

- ``estimate`` is read-only (scope ``deals:read``, the operator's system actor): no valuation, cost
  line, alert, rate or audit row is written, so there is no ``--yes``. In listing mode the
  listing's CURRENT recorded values are used automatically (fact source ``recorded``) unless a
  value is given here.
- Rules: an ACTIVE, owner-approved rule set when one applies (``mode: active``); otherwise an
  INDICATIVE PREVIEW of the best version that is not ACTIVE (approved, under review, then draft),
  shown with the MK/EN banner naming its status, totals named
  ``indicative_total`` and never production-ready; otherwise ``unavailable``.
- Unknown is never zero: a missing fact leaves the dependent charges unknown and is listed under
  "missing facts". Nothing is inferred: no proof of origin unless ``--origin-proof`` names one
  (standard duty), no tariff code unless ``--hs-code`` is given, no WLTP <-> NEDC conversion.
- Amounts are exact decimals (``3000`` or ``3000.50``, never ``3000,50``). No rate, coefficient or
  threshold lives here; this module only collects facts and prints the shared view.
- Two values (owner decision 2026-10-13): ``--price`` is the REAL purchase price (реална набавна
  цена; the profit and every landed cost use it); ``--estimated-customs-value-eur`` with
  ``--estimated-customs-value-source`` is the ESTIMATED customs value of the vehicle without
  transport (проценета царинска вредност, e.g. the SchwackeNet catalogue value), the value the
  primary charges are computed on (plus the transport to the MK place of entry); the
  ``transaction_value`` scenario shows the charges on the price paid (Customs Law art. 28, the
  primary method). Results are printed in whole units (MKD and EUR, half-up); a fact typed with a
  fraction is shown whole with its exact value next to it (calculations use the exact value).
- ``values`` is read-only (``deals:read``, the operator's system actor): the current real purchase
  price and estimated customs value of a listing and a short history.
- ``record-values`` is an OWNER action: ``--owner-user-id`` must be an ACTIVE owner of the
  workspace (verified; the rows and the audit event ``import_values.record`` name that owner) and
  ``--yes`` confirms the write. Each value supersedes the listing's current value of the same kind
  (append-only history); the listing's valuations are marked stale and one recomputation is
  queued. Amounts are EUR with at most two decimals and more than zero; nothing is converted here.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
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
    require_yes,
    run_async,
    safe,
    workspace_option,
)
from suv_deals.domain.enums import Co2Cycle, Fuel

if TYPE_CHECKING:
    from suv_deals.persistence.import_values_repo import RecordedCustomsValue, RecordedPurchase
    from suv_deals.persistence.queries.import_costs import ImportCostRequest
    from suv_deals.views.common import AmountView, ResponseWarning
    from suv_deals.views.import_costs import ImportCostEstimateView, ImportScenarioView
    from suv_deals.views.import_values import (
        ImportValueRecordView,
        ImportValuesRecordResult,
        ImportValuesView,
    )

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
#: How firm a recorded real purchase price is (``import_values_repo.RecordedKind``).
PURCHASE_KINDS: Final = ("actual", "quote", "estimate")
#: History rows ``values`` shows by default / at most (``import_values_repo``).
DEFAULT_HISTORY: Final = 10
MAX_HISTORY: Final = 50

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
    "estimated_customs_value_eur",
    "estimated_customs_value_source",
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


def whole(text: str, unit: str = "") -> str:
    """A decimal in whole units, half-up (owner decision 2026-10-13: "без децимали"):
    ``whole("3000.00", "EUR")`` is ``3000 EUR``; an amount with a fraction keeps its exact value
    next to it, ``2801 EUR (exact 2800.50)``, since calculations always use the exact value."""
    exact = Decimal(text)
    rounded = exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    shown = f"{rounded} {unit}" if unit else f"{rounded}"
    return shown if rounded == exact else f"{shown} (exact {text})"


def _fact_amount(view: AmountView) -> str:
    """A fact echoed as typed (``"3000.00"``), shown in whole units."""
    if view.status == "known" and view.amount is not None:
        return whole(view.amount, view.currency or "")
    return _amount(view)


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
        tagged("real purchase price", f"{_fact_amount(facts.price)} ({facts.price_basis})", "price"),
        tagged("fuel", facts.fuel.value, "fuel"),
        tagged("CO2", co2, "co2"),
        tagged("CO2 document", facts.co2_document or "not stated", "co2_document"),
        tagged("origin proof", proof, "origin_proof"),
        tagged("HS code", facts.hs_code or "not given (never inferred)", "hs_code"),
        tagged("first registration", facts.first_registration or "unknown", "first_registration"),
        tagged("emissions class", facts.emissions_class or "unknown", "emissions_class"),
    ]

    def eur(value: str | None) -> str | None:
        return None if value is None else whole(value, "EUR")

    optional = (
        ("inland leg (separate)", eur(facts.transport_after_entry_eur), "", "transport_after_entry_eur"),
        (
            "estimated customs value",
            eur(facts.estimated_customs_value_eur),
            f", without transport ({facts.estimated_customs_value_source})",
            "estimated_customs_value_eur",
        ),
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
    kind = f" [{scenario.customs_value_kind}]"
    lines.append(
        _line("customs value", _pair(scenario.customs_value, scenario.customs_value_eur) + kind + basis)
    )
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
    price = view.purchase_price
    lines.append(
        _line("real purchase price", f"{_amount(price.amount)}  ({price.basis or 'none'}: {price.source})")
    )
    duties = view.customs_value_for_duties
    lines.append(
        _line("customs value for duties", f"{_amount(duties.amount)}  ({duties.basis}: {duties.source})")
    )
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
    """Import-cost calculator (read-only) and a listing's recorded real purchase price and
    estimated customs value (recording is an owner action)."""


@import_costs_group.command("estimate")
@workspace_option
@click.option(
    "--listing-id", type=click.UUID, default=None, help="Use this listing's facts (current revision)."
)
@click.option(
    "--price",
    type=DECIMAL,
    default=None,
    help="Real purchase price (exact decimal); the landed cost uses it.",
)
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
    "--estimated-customs-value-eur",
    type=DECIMAL,
    default=None,
    help="Estimated customs value of the vehicle in EUR, without transport (e.g. SchwackeNet); the "
    "primary charges use it, the landed cost keeps --price. Needs --estimated-customs-value-source.",
)
@click.option(
    "--estimated-customs-value-source",
    default=None,
    help="Where the estimated customs value comes from, e.g. 'SchwackeNet catalogue' (3-120 characters).",
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
    result is an INDICATIVE PREVIEW of a rule set that is not ACTIVE (never production-ready, never stored,
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


# --------------------------------------------------------------------------- recorded values

#: Option names of each repository input field (usage errors name options, never values).
_PURCHASE_OPTIONS: Final = {
    "amount": "--purchase-price",
    "kind": "--purchase-kind",
    "source": "--purchase-source",
    "document_ref": "--document-ref",
}
_CUSTOMS_OPTIONS: Final = {
    "amount_eur": "--estimated-customs-value-eur",
    "source": "--estimated-customs-value-source",
    "document_ref": "--document-ref",
}


def _invalid(what: str, exc: Exception, options: Mapping[str, str]) -> str:
    from pydantic import ValidationError

    assert isinstance(exc, ValidationError)
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = err["loc"]
        option = options.get(str(loc[0]), what) if loc else what
        lines.append(f"{option}: {err['msg']}")
    return f"invalid {what}:\n  " + "\n  ".join(lines[:10])


def recorded_inputs(
    *,
    purchase_price: Decimal | None,
    purchase_kind: str | None,
    purchase_source: str | None,
    estimated_customs_value_eur: Decimal | None,
    estimated_customs_value_source: str | None,
    document_ref: str | None,
) -> tuple[RecordedPurchase | None, RecordedCustomsValue | None]:
    """The repository inputs of ``record-values`` (validated before any database work; a usage
    error names the options). A purchase price needs its kind and source; an estimated customs
    value needs its source; ``--document-ref`` goes with every value of the call."""
    from pydantic import ValidationError

    from suv_deals.persistence.import_values_repo import RecordedCustomsValue, RecordedPurchase

    price_parts = {
        "--purchase-price": purchase_price,
        "--purchase-kind": purchase_kind,
        "--purchase-source": purchase_source,
    }
    customs_parts = {
        "--estimated-customs-value-eur": estimated_customs_value_eur,
        "--estimated-customs-value-source": estimated_customs_value_source,
    }
    for parts, what in (
        (price_parts, "a real purchase price"),
        (customs_parts, "an estimated customs value"),
    ):
        given = [name for name, value in parts.items() if value is not None]
        if given and len(given) != len(parts):
            missing = ", ".join(name for name in parts if name not in given)
            fail(f"{what} needs {', '.join(parts)} together (missing: {missing})", EXIT_USAGE)
    if purchase_price is None and estimated_customs_value_eur is None:
        fail(
            "record --purchase-price (with --purchase-kind and --purchase-source), "
            "--estimated-customs-value-eur (with --estimated-customs-value-source) or both",
            EXIT_USAGE,
        )
    purchase: RecordedPurchase | None = None
    customs: RecordedCustomsValue | None = None
    if purchase_price is not None:
        try:
            purchase = RecordedPurchase.model_validate(
                {
                    "amount": purchase_price,
                    "currency": "EUR",
                    "kind": purchase_kind,
                    "source": purchase_source,
                    "document_ref": document_ref,
                }
            )
        except ValidationError as exc:
            fail(_invalid("real purchase price", exc, _PURCHASE_OPTIONS), EXIT_USAGE)
    if estimated_customs_value_eur is not None:
        try:
            customs = RecordedCustomsValue.model_validate(
                {
                    "amount_eur": estimated_customs_value_eur,
                    "source": estimated_customs_value_source,
                    "document_ref": document_ref,
                }
            )
        except ValidationError as exc:
            fail(_invalid("estimated customs value", exc, _CUSTOMS_OPTIONS), EXIT_USAGE)
    return purchase, customs


def _recorded_amount(record: ImportValueRecordView) -> str:
    """Whole euros (half-up), with the exact recorded amount when it has a fraction."""
    amount = record.amount
    if amount.status != "known" or amount.amount is None:  # pragma: no cover - recorded values are known
        return _amount(amount)
    shown = f"{amount.amount} {amount.currency}"
    return (
        shown
        if Decimal(record.amount_exact) == Decimal(amount.amount)
        else f"{shown} (exact {record.amount_exact})"
    )


def _describe_record(record: ImportValueRecordView) -> str:
    what = record.kind if record.category == "purchase" else "estimate, without transport"
    parts = [f"{_recorded_amount(record)}  ({what}; source: {record.source}"]
    if record.document_ref:
        parts.append(f"; document {record.document_ref}")
    parts.append(f"; recorded {record.recorded_at.strftime('%Y-%m-%d %H:%M')} UTC")
    if record.recorded_by_principal is not None:
        parts.append(f" by {record.recorded_by_principal}")
    parts.append(f")  [{record.id}]")
    return "".join(parts)


_RECORD_LABELS: Final = {
    "purchase": "real purchase price",
    "customs_value": "estimated customs value",
}


def render_values(view: ImportValuesView) -> list[str]:
    """The human-readable current values and history (``import-costs values``)."""
    lines = [f"IMPORT VALUES  listing {view.listing_id}"]
    price, customs = view.purchase_price, view.estimated_customs_value
    lines.append(
        _line(
            "real purchase price",
            _describe_record(price)
            if price is not None
            else "not recorded (the calculator and valuations use the advertised price)",
        )
    )
    lines.append(
        _line(
            "estimated customs value",
            _describe_record(customs)
            if customs is not None
            else "not recorded (the charges use the transaction value: price + transport)",
        )
    )
    if view.history:
        lines.append("History (newest first):")
        for record in view.history:
            state = "current" if record.current else "superseded"
            lines.append(
                f"  {record.recorded_at.strftime('%Y-%m-%d %H:%M')}  {_RECORD_LABELS[record.category]:<24}"
                f" {record.kind:<9} {_recorded_amount(record):<12} {state:<10} {safe(record.source)}"
                f"  [{record.id}]"
            )
    lines.append(f"Note: {safe(view.note)}")
    return lines


def render_recorded(result: ImportValuesRecordResult, owner_user_id: UUID) -> list[str]:
    """The human-readable outcome of ``import-costs record-values``."""
    values = result.values
    superseded = set(result.superseded_ids)
    lines = [
        f"Recorded for listing {values.listing_id} (owner {owner_user_id}; audited import_values.record):"
    ]
    current = {r.id: r for r in (values.purchase_price, values.estimated_customs_value) if r is not None}
    for record_id in result.recorded_ids:
        record = current.get(record_id)
        if record is None:  # pragma: no cover - a recorded value is the current one in its transaction
            continue
        previous = record.supersedes_id
        chain = f", supersedes {previous}" if previous in superseded else ", the first value of its kind"
        lines.append(
            _line(_RECORD_LABELS[record.category], f"{_recorded_amount(record)}  ({record.source}{chain})")
        )
    lines.append(_line("valuations marked stale", str(len(result.stale_valuation_ids))))
    lines.append(_line("recomputation queued", f"job {result.recompute_job_id}"))
    lines.append(f"  {safe(result.production_effect)}")
    lines += ["", *render_values(values)]
    return lines


@import_costs_group.command("values")
@workspace_option
@click.option(
    "--listing-id", type=click.UUID, required=True, help="The listing whose recorded values to show."
)
@click.option(
    "--history",
    type=click.IntRange(0, MAX_HISTORY),
    default=DEFAULT_HISTORY,
    show_default=True,
    help="History rows of both values (newest first).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (the API view).")
@pass_cli
def values(
    cli: CliContext, /, *, workspace: UUID | None, listing_id: UUID, history: int, as_json: bool
) -> None:
    """Show a listing's recorded REAL purchase price and ESTIMATED customs value (read-only).

    The real purchase price (реална набавна цена) is what is, or will be, actually paid: the profit
    and the landed cost use it. The estimated customs value (проценета царинска вредност, e.g. the
    SchwackeNet catalogue value) is the vehicle's value for customs without transport: the import
    charges use it. `import-costs estimate --listing-id` and the valuations use the current values.
    """
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.api.import_values_routes import values_view
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.persistence import import_values_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work

        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            actor = operator_actor(workspace_id, "import-costs-values")
            async with unit_of_work(db, actor) as conn:
                recorded = await import_values_repo.current_import_values(
                    conn, actor, listing_id, history_limit=history
                )
                as_of = await db_now(conn)
        view = values_view(recorded)
        if as_json:
            emit_json({"as_of": as_of.isoformat(), "data": view.model_dump(mode="json")})
        else:
            for line in render_values(view):
                echo(line)
        return 0

    run_async(body)


@import_costs_group.command("record-values")
@workspace_option
@click.option("--listing-id", type=click.UUID, required=True, help="The listing the values belong to.")
@click.option(
    "--owner-user-id",
    type=click.UUID,
    required=True,
    help="The owner's user id: an ACTIVE owner of the workspace records the values (audited as that owner).",
)
@click.option(
    "--purchase-price",
    type=DECIMAL,
    default=None,
    help="REAL purchase price in EUR (at most two decimals): what is, or will be, actually paid; the "
    "profit and the landed cost use it. Needs --purchase-kind and --purchase-source.",
)
@click.option(
    "--purchase-kind",
    type=click.Choice(PURCHASE_KINDS),
    default=None,
    help="actual (paid), quote (offered or agreed, not paid) or estimate (the owner's estimate).",
)
@click.option(
    "--purchase-source",
    default=None,
    help="Where the price comes from, e.g. 'seller invoice' (3-120 characters).",
)
@click.option(
    "--estimated-customs-value-eur",
    type=DECIMAL,
    default=None,
    help="ESTIMATED customs value of the vehicle in EUR WITHOUT transport (e.g. the SchwackeNet catalogue "
    "value); the import charges use it. Needs --estimated-customs-value-source.",
)
@click.option(
    "--estimated-customs-value-source",
    default=None,
    help="Where the estimate comes from, e.g. 'SchwackeNet catalogue' (3-120 characters).",
)
@click.option(
    "--document-ref",
    default=None,
    help="Reference of the supporting document (e.g. an invoice number), stored with each value recorded "
    "by this call (redacted; one plain line).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (the API view).")
@click.option("--yes", is_flag=True, help="Confirm the database write.")
@pass_cli
def record_values(
    cli: CliContext,
    /,
    *,
    workspace: UUID | None,
    listing_id: UUID,
    owner_user_id: UUID,
    purchase_price: Decimal | None,
    purchase_kind: str | None,
    purchase_source: str | None,
    estimated_customs_value_eur: Decimal | None,
    estimated_customs_value_source: str | None,
    document_ref: str | None,
    as_json: bool,
    yes: bool,
) -> None:
    """Record a listing's REAL purchase price and/or ESTIMATED customs value (owner; audited; --yes).

    Two separate values per vehicle (owner decision 2026-10-13): the real purchase price
    (реална набавна цена) for the profit and the landed cost, the estimated customs value
    (проценета царинска вредност, without transport) for the import charges. Each supersedes the
    listing's current value of its kind (the history keeps both); the listing's valuations are
    marked stale and recomputed with the new values, and `import-costs estimate --listing-id` uses
    them. Nothing approves tax rules or contacts anyone.
    """
    purchase, customs = recorded_inputs(
        purchase_price=purchase_price,
        purchase_kind=purchase_kind,
        purchase_source=purchase_source,
        estimated_customs_value_eur=estimated_customs_value_eur,
        estimated_customs_value_source=estimated_customs_value_source,
        document_ref=document_ref,
    )
    if not as_json:
        echo(f"Listing                  : {listing_id}")
        if purchase is not None:
            echo(
                f"  real purchase price    : {whole(str(purchase.amount), 'EUR')} "
                f"({purchase.kind}; {safe(purchase.source)})"
            )
        if customs is not None:
            echo(
                f"  estimated customs value: {whole(str(customs.amount_eur), 'EUR')}, without transport "
                f"({safe(customs.source)})"
            )
        stored_ref = next((v.document_ref for v in (purchase, customs) if v is not None), None)
        if stored_ref is not None:
            echo(f"  document reference     : {safe(stored_ref)}")
        echo("  effect                 : supersedes the current value of each kind; the listing's")
        echo("                           valuations are marked stale and recomputed")
    require_yes(yes, "import-costs record-values")
    settings = load_settings(cli)

    async def body() -> int:
        from suv_deals.api.import_values_routes import record_result
        from suv_deals.cli_commands._common import open_database, operator_actor, resolve_workspace
        from suv_deals.cli_commands.inquiries import owner_actor_for
        from suv_deals.persistence import import_values_repo
        from suv_deals.persistence.database import db_now
        from suv_deals.persistence.transactions import unit_of_work

        purpose = "import-costs-record-values"
        async with open_database(settings, application_name="suv-deals-cli") as db:
            workspace_id = await resolve_workspace(db, workspace)
            operator = operator_actor(workspace_id, purpose, admin=True)
            async with unit_of_work(db, operator) as conn:
                owner = await owner_actor_for(conn, workspace_id, owner_user_id, purpose)
            async with unit_of_work(db, owner) as conn:
                recorded = await import_values_repo.record_import_values(
                    conn, owner, listing_id, purchase=purchase, customs_value=customs
                )
                as_of = await db_now(conn)
        result = record_result(recorded)
        if as_json:
            emit_json({"as_of": as_of.isoformat(), "data": result.model_dump(mode="json")})
        else:
            for line in render_recorded(result, owner_user_id):
                echo(line)
        return 0

    run_async(body)


__all__ = [
    "CO2_DOCUMENTS",
    "DEFAULT_HISTORY",
    "IMPORTER_STATUSES",
    "MAX_HISTORY",
    "ORIGIN_PROOF_KINDS",
    "PRICE_BASES",
    "PRICE_CURRENCIES",
    "PURCHASE_KINDS",
    "build_request",
    "estimate",
    "import_costs_group",
    "record_values",
    "recorded_inputs",
    "render_estimate",
    "render_recorded",
    "render_values",
    "values",
    "vehicle_facts",
    "whole",
]
