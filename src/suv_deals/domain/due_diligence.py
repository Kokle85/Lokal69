"""Manual due-diligence checklist and seller-question drafts (spec section 19).

Pure domain code. Before a shortlisted vehicle is treated as actionable the owner works
through a compact checklist covering every spec 19 topic. Each item has one status:

- ``answered_by_evidence``: a qualifying owner-held record (document, inspection report or
  owner verification with at least medium confidence) answers it,
- ``seller_claim_only``: only the seller's statement supports it,
- ``unknown``: nothing is known,
- ``needs_inspection`` / ``needs_documents``: a physical inspection or documents are required,
- ``price_confirmation_needed``: the payable price/availability must be confirmed.

Items also carry dashboard action flags (``needs_inspection``, ``needs_documents``,
``price_confirmation_needed``, spec 19 last paragraph). A model's shortlist never substitutes
for these checks: the checklist is ``ready`` only when every item is answered by evidence.

Rules: photos can reveal visible issues and support an inspection checklist but cannot certify
mechanical condition or hidden damage, so ``photo_observation`` evidence never answers an item.
Nothing here infers a person's identity or sensitive attributes. Seller questions are DRAFTS
only: never sent by the system, explicitly marked as requiring the owner's approval for that
seller and purpose (spec 3, 19).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Co2Cycle,
    Confidence,
    Drive,
    Fuel,
    Gearbox,
    OdometerClaim,
    PriceBasis,
    PriceType,
    Tristate,
    ValuationState,
)
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.notifications import sanitize_seller_text
from suv_deals.errors import ValidationFailed

CHECKLIST_VERSION: Final = "due-diligence@1.0.0"
DRAFT_MARKER: Final = "DRAFT - requires owner approval to send. Not sent by the system."
PHOTO_LIMITATION: Final = (
    "Photos can reveal visible issues and support an inspection; they cannot certify mechanical "
    "condition or establish hidden damage."
)
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ChecklistTopic(StrEnum):
    """Every required review topic of spec 19, in spec order."""

    AVAILABILITY_PRICE = "availability_price"
    VIN = "vin"
    SPEC_MATCH = "spec_match"
    ODOMETER_RECORDS = "odometer_records"
    MECHANICAL_FAULTS = "mechanical_faults"
    ACCIDENT_DAMAGE = "accident_damage"
    WEAR_ITEMS = "wear_items"
    RUNNING_TRANSPORT = "running_transport"
    REGISTRATION_EXPORT_DOCS = "registration_export_docs"
    CO2_ORIGIN = "co2_origin"
    INSPECTION = "inspection"
    EXPORT_PLATES_INSURANCE = "export_plates_insurance"
    OWNERSHIP_PAYMENT = "ownership_payment"


TOPIC_QUESTIONS: Final[dict[ChecklistTopic, str]] = {
    ChecklistTopic.AVAILABILITY_PRICE: "Is the vehicle still available at the stated payable price?",
    ChecklistTopic.VIN: (
        "Is the full VIN provided, and does it match photographs/documents where lawfully available?"
    ),
    ChecklistTopic.SPEC_MATCH: (
        "Does exact generation/facelift/engine/gearbox/drive match the comparison set?"
    ),
    ChecklistTopic.ODOMETER_RECORDS: "Are odometer, service invoices and inspection records consistent?",
    ChecklistTopic.MECHANICAL_FAULTS: (
        "Are there known engine, transmission, turbo, DPF, injector, AWD, suspension or electrical faults?"
    ),
    ChecklistTopic.ACCIDENT_DAMAGE: (
        "Are accident damage, structural repair, flood history, corrosion or warning lights disclosed?"
    ),
    ChecklistTopic.WEAR_ITEMS: "Are tyres/brakes and immediate service items included in repair estimates?",
    ChecklistTopic.RUNNING_TRANSPORT: (
        "Does the vehicle run and load normally, and does the transport quote reflect its condition?"
    ),
    ChecklistTopic.REGISTRATION_EXPORT_DOCS: "Are registration/export documents and CoC available?",
    ChecklistTopic.CO2_ORIGIN: "What supports CO2 value, measurement cycle and origin claims?",
    ChecklistTopic.INSPECTION: (
        "What does a current HU/TUV or equivalent inspection actually cover, and when does it expire?"
    ),
    ChecklistTopic.EXPORT_PLATES_INSURANCE: (
        "Are export plates/insurance required for the planned transport method, and what exact "
        "period/destination is covered?"
    ),
    ChecklistTopic.OWNERSHIP_PAYMENT: (
        "Are ownership, seller identity and payment instructions independently checked by the buyer?"
    ),
}


class ItemStatus(StrEnum):
    ANSWERED_BY_EVIDENCE = "answered_by_evidence"
    SELLER_CLAIM_ONLY = "seller_claim_only"
    UNKNOWN = "unknown"
    NEEDS_INSPECTION = "needs_inspection"
    NEEDS_DOCUMENTS = "needs_documents"
    PRICE_CONFIRMATION_NEEDED = "price_confirmation_needed"


class DashboardAction(StrEnum):
    NEEDS_INSPECTION = "needs_inspection"
    NEEDS_DOCUMENTS = "needs_documents"
    PRICE_CONFIRMATION_NEEDED = "price_confirmation_needed"


class VerificationKind(StrEnum):
    DOCUMENT = "document"
    INSPECTION = "inspection"
    OWNER_VERIFIED = "owner_verified"
    PHOTO_OBSERVATION = "photo_observation"  # supports an inspection; never certifies condition


_CONDITION_TOPICS: Final = frozenset(
    {
        ChecklistTopic.MECHANICAL_FAULTS,
        ChecklistTopic.ACCIDENT_DAMAGE,
        ChecklistTopic.WEAR_ITEMS,
        ChecklistTopic.RUNNING_TRANSPORT,
    }
)
_QUALIFYING: Final[dict[ChecklistTopic, frozenset[VerificationKind]]] = {
    topic: (
        frozenset({VerificationKind.INSPECTION, VerificationKind.OWNER_VERIFIED})
        if topic in _CONDITION_TOPICS
        else frozenset({VerificationKind.OWNER_VERIFIED})
        if topic == ChecklistTopic.OWNERSHIP_PAYMENT
        else frozenset(
            {VerificationKind.DOCUMENT, VerificationKind.INSPECTION, VerificationKind.OWNER_VERIFIED}
        )
    )
    for topic in ChecklistTopic
}


class TopicEvidence(BaseModel):
    """An owner-held evidence record linked to one checklist topic."""

    model_config = _FROZEN

    topic: ChecklistTopic
    evidence_id: UUID
    kind: VerificationKind
    confidence: Confidence
    note: str | None = Field(default=None, max_length=300)


class ChecklistItem(BaseModel):
    model_config = _FROZEN

    topic: ChecklistTopic
    question: str
    status: ItemStatus
    actions: tuple[DashboardAction, ...]
    notes: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()


class Checklist(BaseModel):
    model_config = _FROZEN

    version: str = CHECKLIST_VERSION
    source_key: str
    source_listing_id: str
    items: tuple[ChecklistItem, ...]
    needs_inspection: bool
    needs_documents: bool
    price_confirmation_needed: bool
    ready: bool  # every item answered by evidence; a shortlist alone never makes it ready
    photo_limitation: str = PHOTO_LIMITATION

    def item(self, topic: ChecklistTopic) -> ChecklistItem:
        for entry in self.items:
            if entry.topic == topic:
                return entry
        raise KeyError(topic)


def build_checklist(
    listing: NormalizedListing,
    valuation_state: ValuationState | None,
    comparable_status: str | None,
    *,
    evidence: Sequence[TopicEvidence] = (),
) -> Checklist:
    """Build the spec 19 checklist for one listing revision (see module docstring)."""
    by_topic: dict[ChecklistTopic, list[TopicEvidence]] = {}
    for record in evidence:
        by_topic.setdefault(record.topic, []).append(record)
    items = tuple(
        _with_evidence(
            topic, _RULES[topic](listing, valuation_state, comparable_status), by_topic.get(topic, [])
        )
        for topic in ChecklistTopic
    )
    flags = {action: any(action in i.actions for i in items) for action in DashboardAction}
    return Checklist(
        source_key=listing.source_key,
        source_listing_id=listing.source_listing_id,
        items=items,
        needs_inspection=flags[DashboardAction.NEEDS_INSPECTION],
        needs_documents=flags[DashboardAction.NEEDS_DOCUMENTS],
        price_confirmation_needed=flags[DashboardAction.PRICE_CONFIRMATION_NEEDED],
        ready=all(i.status == ItemStatus.ANSWERED_BY_EVIDENCE for i in items),
    )


# ---------------------------------------------------------------------------------------------
# Topic rules: each returns (status, actions, notes) from listing facts alone.
# ---------------------------------------------------------------------------------------------

_Draft = tuple[ItemStatus, tuple[DashboardAction, ...], tuple[str, ...]]
_Rule = Callable[[NormalizedListing, ValuationState | None, str | None], _Draft]
_INSPECT: Final = (DashboardAction.NEEDS_INSPECTION,)
_DOCS: Final = (DashboardAction.NEEDS_DOCUMENTS,)


def _availability_price(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    p = listing.price
    notes = [f"listing availability: {listing.availability.value}"]
    if listing.availability in (Availability.REMOVED, Availability.SOLD_CLAIMED, Availability.RESERVED):
        notes.append("listing is not shown as available; confirm before any further step")
    if p.amount_minor is None:
        notes.append("advertised price unknown")
    if p.type != PriceType.FULL_VEHICLE_ASKING:
        notes.append(f"price type {p.type.value}: not an ordinary payable vehicle price")
    if p.basis != PriceBasis.GROSS:
        notes.append(f"price basis {p.basis.value}: confirm the gross payable amount for this buyer")
    if p.required_seller_fees_known != Tristate.NO:
        notes.append("mandatory seller fees not confirmed absent")
    if p.refundable_deposit_minor is not None:
        notes.append("refundable deposit stated: confirm cash outlay, refund terms and timing")
    if p.negotiable == Tristate.YES:
        notes.append("seller marks the price as negotiable")
    return ItemStatus.PRICE_CONFIRMATION_NEEDED, (DashboardAction.PRICE_CONFIRMATION_NEEDED,), tuple(notes)


def _vin(listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None) -> _Draft:
    doc = listing.documentation
    privacy = (
        "do not upload seller documents or VIN reports to third-party AI providers without authorization"
    )
    if doc.vin is None:
        return ItemStatus.NEEDS_DOCUMENTS, _DOCS, ("full VIN not provided", privacy)
    return (
        ItemStatus.SELLER_CLAIM_ONLY,
        _DOCS,
        ("VIN provided by the seller; match it against the registration document and vehicle plate", privacy),
    )


def _spec_match(
    listing: NormalizedListing, _valuation: ValuationState | None, comparable_status: str | None
) -> _Draft:
    v = listing.vehicle
    unknown = [
        name
        for name, missing in (
            ("generation", v.generation is None),
            ("facelift", v.facelift == Tristate.UNKNOWN),
            ("fuel", v.fuel == Fuel.UNKNOWN),
            ("gearbox", v.gearbox == Gearbox.UNKNOWN),
            ("drive", v.drive == Drive.UNKNOWN),
            ("engine displacement", v.engine_displacement_cm3 is None),
            ("power", v.power_kw is None),
            ("engine code", v.engine_code is None),
        )
        if missing
    ]
    notes = [f"unknown: {', '.join(unknown)}"] if unknown else []
    if comparable_status != "adequate":
        notes.append(f"MK comparison set: {comparable_status or 'not computed'}; research needed")
    core_unknown = {"generation", "fuel", "gearbox", "drive"} & set(unknown)
    if core_unknown or comparable_status in (None, "insufficient_comparables"):
        return ItemStatus.UNKNOWN, _DOCS, tuple(notes)
    notes.append("specification is seller-stated; confirm with the registration document or CoC")
    return ItemStatus.SELLER_CLAIM_ONLY, _DOCS, tuple(notes)


def _odometer(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    claim = listing.vehicle.mileage_claim
    notes = [f"odometer claim: {claim.value}"]
    if listing.condition.full_service_history != ClaimStatus.UNKNOWN:
        notes.append(f"full service history: {listing.condition.full_service_history.value}")
    if claim == OdometerClaim.VERIFIED:
        return ItemStatus.ANSWERED_BY_EVIDENCE, (), ("odometer marked owner-verified in the listing record",)
    if claim == OdometerClaim.CONFLICTING:
        notes.append("conflicting odometer statements; resolve with records before relying on mileage")
    if claim == OdometerClaim.DOCUMENTED:
        notes.append("seller references records; obtain copies")
        return ItemStatus.SELLER_CLAIM_ONLY, _DOCS, tuple(notes)
    return ItemStatus.NEEDS_DOCUMENTS, _DOCS, tuple(notes)


def _mechanical(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    c = listing.condition
    notes = [f"seller-listed fault: {f}" for f in (_safe(x) for x in c.mechanical_faults[:5]) if f]
    if c.warning_lights_off != ClaimStatus.UNKNOWN:
        notes.append(f"warning lights off: {c.warning_lights_off.value}")
    notes.append("a seller statement is not an inspection result")
    return ItemStatus.NEEDS_INSPECTION, _INSPECT, tuple(notes)


def _accident(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    c = listing.condition
    notes = [f"accident-free claim: {c.accident_free.value}"]
    if c.corrosion_free != ClaimStatus.UNKNOWN:
        notes.append(f"corrosion-free claim: {c.corrosion_free.value}")
    if c.damaged_vehicle in (ClaimStatus.SELLER_CLAIMED, ClaimStatus.VERIFIED):
        notes.append("vehicle described as damaged")
    if c.accident_free == ClaimStatus.VERIFIED and c.damaged_vehicle not in (
        ClaimStatus.SELLER_CLAIMED,
        ClaimStatus.VERIFIED,
    ):
        return ItemStatus.ANSWERED_BY_EVIDENCE, (), tuple(notes)
    if c.accident_free == ClaimStatus.SELLER_CLAIMED and c.damaged_vehicle not in (
        ClaimStatus.SELLER_CLAIMED,
        ClaimStatus.VERIFIED,
    ):
        return ItemStatus.SELLER_CLAIM_ONLY, _INSPECT, tuple(notes)
    if c.accident_free == ClaimStatus.UNKNOWN and c.damaged_vehicle == ClaimStatus.UNKNOWN:
        return ItemStatus.UNKNOWN, _INSPECT, tuple(notes)
    return ItemStatus.NEEDS_INSPECTION, _INSPECT, tuple(notes)


def _wear(
    listing: NormalizedListing, valuation_state: ValuationState | None, _comparables: str | None
) -> _Draft:
    if valuation_state in (ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED):
        note = "check that tyres, brakes and immediate service items are in the repair estimate"
    else:
        note = "repair estimate incomplete or not started; tyres/brakes/service items unknown"
    return ItemStatus.NEEDS_INSPECTION, _INSPECT, (note,)


def _running(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    running = listing.condition.running
    notes = [f"running claim: {running.value}"]
    if running == ClaimStatus.VERIFIED:
        return ItemStatus.ANSWERED_BY_EVIDENCE, (), tuple(notes)
    if running == ClaimStatus.SELLER_CLAIMED:
        return ItemStatus.SELLER_CLAIM_ONLY, _INSPECT, tuple(notes)
    if running == ClaimStatus.SELLER_DENIED:
        notes.append("non-running: the transport quote must include loading/non-running surcharges")
        return ItemStatus.NEEDS_INSPECTION, _INSPECT, tuple(notes)
    return ItemStatus.UNKNOWN, _INSPECT, tuple(notes)


def _registration(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    d = listing.documentation
    notes = (f"registration documents: {d.registration_documents.value}", f"CoC: {d.coc_available.value}")
    if d.registration_documents == ClaimStatus.VERIFIED and d.coc_available == ClaimStatus.VERIFIED:
        return ItemStatus.ANSWERED_BY_EVIDENCE, (), notes
    return ItemStatus.NEEDS_DOCUMENTS, _DOCS, notes


def _co2_origin(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    co2 = listing.co2
    notes: list[str] = []
    if co2.g_per_km is None:
        notes.append("CO2 value unknown")
    else:
        source = "with evidence record" if co2.evidence_id else "seller-stated"
        notes.append(f"CO2 {co2.g_per_km} g/km, cycle {co2.cycle.value} ({source})")
    if co2.cycle == Co2Cycle.UNKNOWN:
        notes.append("measurement cycle unknown; never converted between NEDC and WLTP by a multiplier")
    origin = _safe(listing.documentation.origin_evidence)
    notes.append(f"origin evidence: {origin}" if origin else "origin evidence missing")
    notes.append("purchase country does not prove preferential origin")
    return ItemStatus.NEEDS_DOCUMENTS, _DOCS, tuple(notes)


def _inspection(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    expiry = listing.documentation.inspection_expiry
    if expiry.value is None:
        return ItemStatus.NEEDS_DOCUMENTS, _DOCS, ("inspection expiry unknown",)
    return (
        ItemStatus.SELLER_CLAIM_ONLY,
        _DOCS,
        (f"seller-stated inspection expiry {expiry.value}; obtain the report to see what it covered",),
    )


def _export_plates(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    return (
        ItemStatus.UNKNOWN,
        _DOCS,
        ("depends on the planned transport method and route; confirm period and destination coverage",),
    )


def _ownership(
    listing: NormalizedListing, _valuation: ValuationState | None, _comparables: str | None
) -> _Draft:
    return (
        ItemStatus.NEEDS_DOCUMENTS,
        _DOCS,
        (
            "the buyer checks ownership, seller identity and payment instructions independently",
            "never rely on payment details received only by message",
        ),
    )


_RULES: Final[dict[ChecklistTopic, _Rule]] = {
    ChecklistTopic.AVAILABILITY_PRICE: _availability_price,
    ChecklistTopic.VIN: _vin,
    ChecklistTopic.SPEC_MATCH: _spec_match,
    ChecklistTopic.ODOMETER_RECORDS: _odometer,
    ChecklistTopic.MECHANICAL_FAULTS: _mechanical,
    ChecklistTopic.ACCIDENT_DAMAGE: _accident,
    ChecklistTopic.WEAR_ITEMS: _wear,
    ChecklistTopic.RUNNING_TRANSPORT: _running,
    ChecklistTopic.REGISTRATION_EXPORT_DOCS: _registration,
    ChecklistTopic.CO2_ORIGIN: _co2_origin,
    ChecklistTopic.INSPECTION: _inspection,
    ChecklistTopic.EXPORT_PLATES_INSURANCE: _export_plates,
    ChecklistTopic.OWNERSHIP_PAYMENT: _ownership,
}


def _with_evidence(topic: ChecklistTopic, draft: _Draft, records: list[TopicEvidence]) -> ChecklistItem:
    status, actions, notes = draft
    qualifying = [
        r
        for r in records
        if r.kind in _QUALIFYING[topic] and r.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    ]
    extra: list[str] = []
    for record in records:
        if record.kind == VerificationKind.PHOTO_OBSERVATION:
            text = _safe(record.note) or "see evidence"
            extra.append(
                f"photo observation ({record.confidence.value} confidence): {text}; not a certification"
            )
        elif record not in qualifying:
            extra.append(
                f"{record.kind.value} evidence ({record.confidence.value} confidence) "
                "does not answer this topic"
            )
    if qualifying:
        return ChecklistItem(
            topic=topic,
            question=TOPIC_QUESTIONS[topic],
            status=ItemStatus.ANSWERED_BY_EVIDENCE,
            actions=(),
            notes=(*notes, *extra),
            evidence_ids=tuple(r.evidence_id for r in qualifying),
        )
    photo_on_condition = topic in _CONDITION_TOPICS and any(
        r.kind == VerificationKind.PHOTO_OBSERVATION for r in records
    )
    if photo_on_condition and DashboardAction.NEEDS_INSPECTION not in actions:
        actions = (*actions, DashboardAction.NEEDS_INSPECTION)
    return ChecklistItem(
        topic=topic,
        question=TOPIC_QUESTIONS[topic],
        status=status,
        actions=tuple(actions),
        notes=(*notes, *extra),
        evidence_ids=tuple(r.evidence_id for r in records),
    )


def _safe(text: str | None) -> str | None:
    return sanitize_seller_text(text, max_length=160)


# ---------------------------------------------------------------------------------------------
# Seller question drafts
# ---------------------------------------------------------------------------------------------

Language = Literal["de", "it", "en"]

#: Topics that are buyer-side planning, not questions for the seller.
BUYER_SIDE_TOPICS: Final = frozenset({ChecklistTopic.EXPORT_PLATES_INSURANCE})

_GREETING: Final[dict[str, str]] = {"de": "Guten Tag,", "it": "Buongiorno,", "en": "Hello,"}
_INTRO: Final[dict[str, str]] = {
    "de": "ich interessiere mich für Ihr Fahrzeug (Inserat {ref}) und habe einige Fragen:",
    "it": "sono interessato al Suo veicolo (annuncio {ref}) e avrei alcune domande:",
    "en": "I am interested in your vehicle (listing {ref}) and have a few questions:",
}
_CLOSING: Final[dict[str, str]] = {
    "de": "Vielen Dank im Voraus.",
    "it": "Grazie in anticipo.",
    "en": "Thank you in advance.",
}
_LOCAL_MARKER: Final[dict[str, str]] = {
    "de": "ENTWURF - Versand nur nach Freigabe durch den Eigentümer.",
    "it": "BOZZA - invio solo dopo approvazione del proprietario.",
    "en": "DRAFT - send only after the owner's approval.",
}
SELLER_QUESTIONS: Final[Mapping[ChecklistTopic, Mapping[str, str]]] = {
    ChecklistTopic.AVAILABILITY_PRICE: {
        "en": "Is the vehicle still available, and is the advertised price the full price I would pay "
        "(including any mandatory fees)?",
        "de": "Ist das Fahrzeug noch verfügbar, und ist der angegebene Preis der vollständige Preis, den ich "
        "zahlen würde (inklusive aller Pflichtgebühren)?",
        "it": "Il veicolo è ancora disponibile e il prezzo indicato è il prezzo totale che pagherei "
        "(incluse eventuali spese obbligatorie)?",
    },
    ChecklistTopic.VIN: {
        "en": "Could you share the full VIN and a photo of the registration document showing it?",
        "de": "Können Sie mir die vollständige Fahrgestellnummer (FIN) und ein Foto der "
        "Zulassungsbescheinigung mit dieser Nummer senden?",
        "it": "Potrebbe indicarmi il numero di telaio completo (VIN) e inviarmi una foto del libretto "
        "di circolazione in cui compare?",
    },
    ChecklistTopic.SPEC_MATCH: {
        "en": "Can you confirm the exact engine (engine code, displacement and power), gearbox and drive "
        "type (2WD or 4x4), ideally as shown in the registration document or CoC?",
        "de": "Können Sie den genauen Motor (Motorcode, Hubraum und Leistung), das Getriebe und die "
        "Antriebsart (2WD oder 4x4) bestätigen, idealerweise laut Zulassungsbescheinigung oder CoC?",
        "it": "Può confermare il motore esatto (codice motore, cilindrata e potenza), il cambio e il tipo "
        "di trazione (2WD o 4x4), possibilmente come indicato nel libretto o nel CoC?",
    },
    ChecklistTopic.ODOMETER_RECORDS: {
        "en": "Are service invoices and inspection reports available that confirm the mileage history?",
        "de": "Gibt es Serviceheft-Einträge, Rechnungen oder Prüfberichte, die den Kilometerstand belegen?",
        "it": "Sono disponibili fatture di manutenzione o verbali di revisione che confermano i chilometri?",
    },
    ChecklistTopic.MECHANICAL_FAULTS: {
        "en": "Are there any known faults with the engine, gearbox, turbo, DPF, injectors, 4x4 system, "
        "suspension or electrics?",
        "de": "Sind Mängel an Motor, Getriebe, Turbolader, Partikelfilter (DPF), Einspritzdüsen, "
        "Allradantrieb, Fahrwerk oder Elektrik bekannt?",
        "it": "Ci sono difetti noti a motore, cambio, turbo, filtro antiparticolato (DPF), iniettori, "
        "trazione integrale, sospensioni o impianto elettrico?",
    },
    ChecklistTopic.ACCIDENT_DAMAGE: {
        "en": "Has the vehicle had any accident damage, structural repairs or flood damage, and is there "
        "any corrosion or are any warning lights on?",
        "de": "Hatte das Fahrzeug Unfallschäden, Reparaturen an der Karosseriestruktur oder Wasserschäden, "
        "und gibt es Rost oder leuchtende Warnlampen?",
        "it": "Il veicolo ha subito incidenti, riparazioni strutturali o danni da allagamento, e ci sono "
        "ruggine o spie accese sul cruscotto?",
    },
    ChecklistTopic.WEAR_ITEMS: {
        "en": "What condition are the tyres and brakes in, and when was the last service?",
        "de": "In welchem Zustand sind Reifen und Bremsen, und wann war der letzte Service?",
        "it": "In che stato sono pneumatici e freni, e quando è stato fatto l'ultimo tagliando?",
    },
    ChecklistTopic.RUNNING_TRANSPORT: {
        "en": "Does the vehicle start and drive normally, so that it can be driven onto a transport trailer?",
        "de": "Springt das Fahrzeug normal an und fährt es so, dass es selbst auf einen Transporter "
        "fahren kann?",
        "it": "Il veicolo si avvia e marcia normalmente, in modo da poter salire da solo su una bisarca?",
    },
    ChecklistTopic.REGISTRATION_EXPORT_DOCS: {
        "en": "Are the registration documents and the CoC available, and would you provide the documents "
        "needed for export?",
        "de": "Sind die Zulassungsbescheinigungen Teil I und II sowie die CoC-Bescheinigung vorhanden, und "
        "würden Sie die für den Export nötigen Unterlagen bereitstellen?",
        "it": "Sono disponibili il libretto di circolazione, il certificato di proprietà e il CoC, e "
        "fornirebbe i documenti necessari per l'esportazione?",
    },
    ChecklistTopic.CO2_ORIGIN: {
        "en": "Can you confirm the CO2 value and its measurement cycle (NEDC or WLTP) from the CoC or "
        "registration document?",
        "de": "Können Sie den CO2-Wert und den Messzyklus (NEFZ oder WLTP) laut CoC oder "
        "Zulassungsbescheinigung bestätigen?",
        "it": "Può confermare il valore di CO2 e il ciclo di misura (NEDC o WLTP) secondo il CoC "
        "o il libretto?",
    },
    ChecklistTopic.INSPECTION: {
        "en": "When does the current technical inspection expire, and is the latest inspection report "
        "available?",
        "de": "Wann läuft die aktuelle Hauptuntersuchung (HU/TÜV) ab, und liegt der letzte Prüfbericht vor?",
        "it": "Quando scade la revisione attuale ed è disponibile l'ultimo verbale di revisione?",
    },
    ChecklistTopic.OWNERSHIP_PAYMENT: {
        "en": "Are you the registered owner or authorised to sell, and does the name on the registration "
        "document match the seller on the purchase contract?",
        "de": "Sind Sie der eingetragene Halter oder zum Verkauf berechtigt, und stimmt der Name in der "
        "Zulassungsbescheinigung mit dem Verkäufer im Kaufvertrag überein?",
        "it": "Lei è il proprietario registrato o è autorizzato a vendere, e il nome sui documenti "
        "corrisponde al venditore nel contratto?",
    },
}


class SellerQuestionDraft(BaseModel):
    """A draft message for the owner. The system never sends it (spec 3, 19)."""

    model_config = _FROZEN

    language: Language
    status: Literal["draft_not_sent"] = "draft_not_sent"
    requires_owner_approval: Literal[True] = True
    marker: str = DRAFT_MARKER
    topics: tuple[ChecklistTopic, ...]
    body: str  # the message itself, for the owner to review/copy after approving
    text: str  # marker lines + body


def draft_seller_questions(
    listing: NormalizedListing,
    language: Language,
    *,
    checklist: Checklist | None = None,
    topics: Sequence[ChecklistTopic] | None = None,
) -> SellerQuestionDraft:
    """Draft neutral seller questions for every open checklist topic (DRAFT only, never sent).

    Questions are fixed templates; seller-provided text is never echoed except the sanitised
    listing reference. No personal data about the seller is requested or inferred.
    """
    if language not in _GREETING:
        raise ValidationFailed("language must be de, it or en")
    if checklist is not None and (checklist.source_key, checklist.source_listing_id) != (
        listing.source_key,
        listing.source_listing_id,
    ):
        raise ValidationFailed("the checklist belongs to a different listing")
    checklist = checklist or build_checklist(listing, None, None)
    open_topics = [
        item.topic
        for item in checklist.items
        if item.status != ItemStatus.ANSWERED_BY_EVIDENCE and item.topic not in BUYER_SIDE_TOPICS
    ]
    if topics is not None:
        wanted = set(topics)
        open_topics = [t for t in open_topics if t in wanted]
    reference = sanitize_seller_text(listing.source_listing_id, max_length=60) or "-"
    lines = [_GREETING[language], "", _INTRO[language].format(ref=reference), ""]
    lines.extend(f"{n}. {SELLER_QUESTIONS[t][language]}" for n, t in enumerate(open_topics, start=1))
    lines.extend(["", _CLOSING[language]])
    body = "\n".join(lines)
    header = [f"[{DRAFT_MARKER}]"]
    if language != "en":
        header.append(f"[{_LOCAL_MARKER[language]}]")
    return SellerQuestionDraft(
        language=language,
        topics=tuple(open_topics),
        body=body,
        text="\n".join([*header, "", body]),
    )
