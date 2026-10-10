"""Unit tests for domain.parsing (spec 31: Locale parsing, Mileage, Prices, Dates).

All inputs are SYNTHETIC test strings written for these tests; none is copied from a real listing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from suv_deals.domain.enums import OdometerClaim, Precision, PriceBasis, PriceType, Tristate, VatTreatment
from suv_deals.domain.parsing import (
    HP_TO_KW,
    MILES_TO_KM,
    PS_TO_KW,
    ParseWarning,
    locale_for_country,
    miles_to_km,
    parse_displacement,
    parse_first_registration,
    parse_mileage,
    parse_number,
    parse_power,
    parse_price,
    parse_source_datetime,
)
from suv_deals.errors import ValidationFailed

NBSP = "\u00a0"
NNBSP = "\u202f"
THIN = "\u2009"
RSQUO = "\u2019"
AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale", "expected"),
    [
        ("2.750", "de", "2750"),
        ("2.750,50", "de", "2750.50"),
        ("2.750,-", "de", "2750"),
        ("199.999", "de", "199999"),
        ("1.234.567,89", "de", "1234567.89"),
        ("0,750", "de", "0.750"),
        ("2,75", "de", "2.75"),
        ("2.750", "it", "2750"),
        ("2.750,50", "it", "2750.50"),
        ("2.750", "mk", "2750"),
        (f"2{NBSP}750,50", "mk", "2750.50"),
        ("2'750.00", "ch", "2750.00"),
        (f"2{RSQUO}750", "ch", "2750"),
        ("12'500.\u2013", "ch", "12500"),
        ("199'999", "ch", "199999"),
        (f"2{NBSP}750", "de", "2750"),
        (f"2{NNBSP}750", "it", "2750"),
        (f"2{THIN}750", "ch", "2750"),
        ("2 750", "ch", "2750"),
        ("2'750", "de", "2750"),  # apostrophes group in every locale
        ("2,750", "en", "2750"),
        ("2,750.50", "en", "2750.50"),
        ("1.5", "en", "1.5"),
        ("2750", "de", "2750"),
        ("2750", None, "2750"),
        ("1.234.567,89", None, "1234567.89"),
        ("1,234,567", None, "1234567"),
        ("2.5", None, "2.5"),
        ("2,5", None, "2.5"),
        ("0.750", None, "0.750"),
        ("2'750.00", None, "2750.00"),
        ("2.750,-", None, "2750"),  # the DE "no cents" marker identifies ',' as decimal
        ("199.999 km", "de", "199999"),
        ("Preis: 2.750 EUR", "de", "2750"),
    ],
)
def test_parse_number_locales(text: str, locale: str | None, expected: str) -> None:
    result = parse_number(text, locale)  # type: ignore[arg-type]
    assert result.value == Decimal(expected)
    assert not result.ambiguous
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("text", "locale", "warning"),
    [
        ("2,750", None, ParseWarning.AMBIGUOUS_SEPARATOR),
        ("2.750", None, ParseWarning.AMBIGUOUS_SEPARATOR),
        ("1.5", "de", ParseWarning.AMBIGUOUS_SEPARATOR),  # '.' groups in de; not a 3-digit group
        ("2,750", "de", ParseWarning.AMBIGUOUS_SEPARATOR),  # thousands or 3-digit fraction?
        ("2.750", "ch", ParseWarning.AMBIGUOUS_SEPARATOR),
        ("2.750", "en", ParseWarning.AMBIGUOUS_SEPARATOR),
        ("2,750.50", "de", ParseWarning.INVALID_GROUPING),  # never reinterpreted as en
        ("2,50", "ch", ParseWarning.LOCALE_MISMATCH),
        ("12'500.-", "de", ParseWarning.LOCALE_MISMATCH),
        ("27.50.00", "de", ParseWarning.AMBIGUOUS_SEPARATOR),
        ("1 234'567", None, ParseWarning.INVALID_GROUPING),
        ("12.34.567", "de", ParseWarning.AMBIGUOUS_SEPARATOR),
        # A leading zero never starts a thousands grouping: '0.750' is not 750 (likely 0.75).
        ("0.750", "de", ParseWarning.INVALID_GROUPING),
        ("0.750.000", "it", ParseWarning.INVALID_GROUPING),
        ("0'750", "ch", ParseWarning.INVALID_GROUPING),
        ("0,750", "en", ParseWarning.INVALID_GROUPING),
    ],
)
def test_parse_number_ambiguous_returns_none(text: str, locale: str | None, warning: str) -> None:
    result = parse_number(text, locale)  # type: ignore[arg-type]
    assert result.value is None
    assert result.ambiguous
    assert warning in result.warnings


@pytest.mark.parametrize(
    ("text", "warning"),
    [
        ("", ParseWarning.EMPTY_INPUT),
        ("   ", ParseWarning.EMPTY_INPUT),
        (None, ParseWarning.EMPTY_INPUT),
        ("auf Anfrage", ParseWarning.NO_NUMBER),
        ("150 - 160", ParseWarning.MULTIPLE_NUMBERS),
        ("-2.750", ParseWarning.NEGATIVE_VALUE),
        ("x" * 600, ParseWarning.INPUT_TOO_LONG),
        ("1" * 20, ParseWarning.INPUT_TOO_LONG),
    ],
)
def test_parse_number_rejects_non_numbers(text: str | None, warning: str) -> None:
    result = parse_number(text, "de")
    assert result.value is None
    assert warning in result.warnings


def test_parse_number_never_returns_float() -> None:
    result = parse_number("2.750,50", "de")
    assert isinstance(result.value, Decimal)


def test_zero_width_characters_are_ignored() -> None:
    assert parse_number("2.\u200b750", "de").value == Decimal("2750")


@pytest.mark.parametrize(
    ("country", "locale"),
    [
        ("DE", "de"),
        ("de", "de"),
        ("IT", "it"),
        ("CH", "ch"),
        ("MK", "mk"),
        ("AT", "de"),
        ("FR", None),
        (None, None),
    ],
)
def test_locale_for_country(country: str | None, locale: str | None) -> None:
    assert locale_for_country(country) == locale


# ---------------------------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale", "amount", "currency"),
    [
        ("2.750 €", "de", "2750", "EUR"),
        ("€ 2.750,00", "it", "2750.00", "EUR"),
        ("EUR 2.750,-", "de", "2750", "EUR"),
        ("2750EUR", "de", "2750", "EUR"),
        ("2.750 Euro", "de", "2750", "EUR"),
        ("CHF 2'750.00", "ch", "2750.00", "CHF"),
        ("Fr. 2'750.\u2013", "ch", "2750", "CHF"),
        ("SFr. 12'500.-", "ch", "12500", "CHF"),
        (f"CHF 2{RSQUO}750", "ch", "2750", "CHF"),
        ("2.750 ден", "mk", "2750", "MKD"),
        ("150.000 денари", "mk", "150000", "MKD"),
        ("MKD 150.000", "mk", "150000", "MKD"),
        (f"2{NBSP}750{NBSP}€", "de", "2750", "EUR"),
        ("2.750 € Euro 5", "de", "2750", "EUR"),  # emissions class is not an amount
    ],
)
def test_parse_price_amount_and_currency(text: str, locale: str, amount: str, currency: str) -> None:
    result = parse_price(text, locale)  # type: ignore[arg-type]
    assert result.amount == Decimal(amount)
    assert result.currency == currency
    assert result.price_type == PriceType.FULL_VEHICLE_ASKING


def test_parse_price_amount_minor_and_money() -> None:
    result = parse_price("2.750,00 €", "de")
    assert result.amount_minor == 275000
    money = result.money()
    assert money is not None and money.currency == "EUR"


def test_parse_price_default_currency_is_flagged() -> None:
    result = parse_price("2.750", "de", default_currency="EUR")
    assert result.currency == "EUR"
    assert ParseWarning.CURRENCY_DEFAULTED in result.warnings


def test_parse_price_missing_currency_is_unknown() -> None:
    result = parse_price("2.750", "de")
    assert result.amount == Decimal("2750")
    assert result.currency is None
    assert result.amount_minor is None
    assert ParseWarning.CURRENCY_MISSING in result.warnings


def test_parse_price_conflicting_currencies() -> None:
    result = parse_price("2.750 € / CHF 2'700", "de")
    assert result.currency is None
    assert ParseWarning.CONFLICTING_CURRENCIES in result.warnings


def test_parse_price_unsupported_default_currency_raises() -> None:
    with pytest.raises(ValidationFailed):
        parse_price("2.750", "de", default_currency="XYZ")


def test_parse_price_multiple_amounts_is_not_guessed() -> None:
    result = parse_price("2.750 € oder 2.500 € bar", "de")
    assert result.amount is None
    assert ParseWarning.MULTIPLE_AMOUNTS in result.warnings


def test_parse_price_ambiguous_amount() -> None:
    result = parse_price("2,750 €", "de")
    assert result.amount is None
    assert ParseWarning.AMBIGUOUS_SEPARATOR in result.warnings


def test_parse_price_sub_minor_precision_rejected() -> None:
    result = parse_price("2.750,555 €", "de")
    assert result.amount is None
    assert ParseWarning.SUB_MINOR_PRECISION in result.warnings


@pytest.mark.parametrize(
    ("text", "locale", "price_type"),
    [
        ("ab 99 €/Monat", "de", PriceType.INSTALMENT),
        ("99 € mtl.", "de", PriceType.INSTALMENT),
        ("99 € mtl", "de", PriceType.INSTALMENT),
        ("199 € pro Monat", "de", PriceType.INSTALMENT),
        ("Finanzierung ab 129 €", "de", PriceType.INSTALMENT),
        ("149 € al mese", "it", PriceType.INSTALMENT),
        ("149 €/mese", "it", PriceType.INSTALMENT),
        ("rata da 149 €", "it", PriceType.INSTALMENT),
        ("rate da 149 €", "it", PriceType.INSTALMENT),
        ("monatliche Rate 149 €", "de", PriceType.INSTALMENT),
        ("Leasing 189 €", "de", PriceType.LEASING),
        ("Anzahlung 500 €", "de", PriceType.DEPOSIT),
        ("anticipo 500 €", "it", PriceType.DEPOSIT),
        ("caparra 300 €", "it", PriceType.DEPOSIT),
        ("Startpreis 1.000 €", "de", PriceType.AUCTION_START),
        ("Mindestgebot 1.000 €", "de", PriceType.AUCTION_START),
        ("base d'asta 1.000 €", "it", PriceType.AUCTION_START),
        ("asta 1.000 €", "it", PriceType.AUCTION_START),
        ("Aktuelles Gebot 1.250 €", "de", PriceType.AUCTION_CURRENT_BID),
        ("3.000 € Export", "de", PriceType.EXPORT_NET),
        ("Exportpreis 2.400 €", "de", PriceType.EXPORT_NET),
        ("Händlerpreis 2.400 €", "de", PriceType.EXPORT_NET),
        ("2.500 € Bastlerfahrzeug", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € Bastler", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € defekt", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € Motorschaden", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € Getriebeschaden", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € Unfallwagen", "de", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € per ricambi", "it", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € incidentata", "it", PriceType.PARTS_OR_DAMAGED),
        ("2.500 € non marciante", "it", PriceType.PARTS_OR_DAMAGED),
        ("2,500 EUR for parts", "en", PriceType.PARTS_OR_DAMAGED),
        ("Preis auf Anfrage", "de", PriceType.PRICE_ON_REQUEST),
        ("auf Anfrage", "de", PriceType.PRICE_ON_REQUEST),
        ("prezzo su richiesta", "it", PriceType.PRICE_ON_REQUEST),
        ("price on request", "en", PriceType.PRICE_ON_REQUEST),
        ("trattativa riservata", "it", PriceType.PRICE_ON_REQUEST),
    ],
)
def test_parse_price_types(text: str, locale: str, price_type: PriceType) -> None:
    assert parse_price(text, locale).price_type == price_type  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "2.750 € Leasing möglich",
        "2.750 € Export möglich",
        "2.750 € unfallfrei",
        "2.750 € Angebot",  # 'Gebot' inside 'Angebot' is not an auction
        "2.750 € ohne Anzahlung",
        "2.750 € Leasing auf Anfrage",
        "2.750 € Finanzierung möglich",
        "2.750 EUR incl. VAT at the standard rate",
    ],
)
def test_parse_price_boundary_aware_words_do_not_reclassify(text: str) -> None:
    assert parse_price(text, "de").price_type == PriceType.FULL_VEHICLE_ASKING


@pytest.mark.parametrize(
    ("text", "locale"),
    [
        ("2.750 € keine Defekte", "de"),
        ("2.750 € nicht defekt", "de"),
        ("2.750 € kein Unfallwagen", "de"),
        ("2.750 € ohne Motorschaden", "de"),
        ("2.750 € keinen Getriebeschaden", "de"),
        ("2.750 € non incidentata", "it"),
        ("2.750 € senza incidenti, non incidentata", "it"),
    ],
)
def test_parse_price_negated_damage_wording_is_not_parts(text: str, locale: str) -> None:
    result = parse_price(text, locale)  # type: ignore[arg-type]
    assert result.price_type == PriceType.FULL_VEHICLE_ASKING
    assert result.amount == Decimal("2750")


def test_parse_price_parts_wins_over_instalment_wording() -> None:
    # The damage statement disqualifies the vehicle whatever the number means.
    result = parse_price("Finanzierung ab 99 €/Monat, Motorschaden", "de")
    assert result.price_type == PriceType.PARTS_OR_DAMAGED
    assert ParseWarning.MULTIPLE_PRICE_TYPES in result.warnings
    assert PriceType.INSTALMENT.value in result.markers


@pytest.mark.parametrize("text", ["0 €", "EUR 0,-", "0,00 €"])
def test_parse_price_zero_is_a_placeholder_not_a_price(text: str) -> None:
    result = parse_price(text, "de")
    assert result.amount is None
    assert result.amount_minor is None
    assert result.price_type == PriceType.UNKNOWN
    assert ParseWarning.PRICE_ZERO_PLACEHOLDER in result.warnings


def test_parse_price_zero_with_on_request_wording() -> None:
    assert parse_price("0 € auf Anfrage", "de").price_type == PriceType.PRICE_ON_REQUEST
    assert parse_price("0 € Preis auf Anfrage", "de").price_type == PriceType.PRICE_ON_REQUEST


def test_parse_price_bare_sfr_marker() -> None:
    result = parse_price("SFr 12'500", "ch")
    assert result.currency == "CHF"
    assert result.amount == Decimal("12500")


def test_parse_price_generic_on_request_ignored_when_amount_present() -> None:
    result = parse_price("2.750 € Besichtigung auf Anfrage", "de")
    assert result.price_type == PriceType.FULL_VEHICLE_ASKING


def test_parse_price_multiple_types_use_precedence() -> None:
    result = parse_price("Leasing ab 199 € mtl.", "de")
    assert result.price_type == PriceType.INSTALMENT
    assert ParseWarning.MULTIPLE_PRICE_TYPES in result.warnings


@pytest.mark.parametrize(
    ("text", "locale", "basis", "treatment", "reclaimable"),
    [
        ("2.311 € netto", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € Netto", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € zzgl. MwSt", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € zzgl. 19% MwSt.", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € ohne MwSt", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € + IVA", "it", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € IVA esclusa", "it", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € esclusa IVA", "it", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € oltre IVA", "it", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2,311 EUR excl. VAT", "en", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € + VAT", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.311 € zzgl. gesetzl. MwSt.", "de", PriceBasis.NET, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        (
            "2.311 € zzgl. 19 % gesetzl. MwSt.",
            "de",
            PriceBasis.NET,
            VatTreatment.NOT_STATED,
            Tristate.UNKNOWN,
        ),
        ("2.750 € inkl. gesetzl. MwSt.", "de", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.750 € inkl. ges. MwSt.", "de", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.750 € IVA 22% esposta", "it", PriceBasis.GROSS, VatTreatment.VAT_SHOWN, Tristate.YES),
        ("2.750 € inkl. MwSt", "de", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.750 € inkl. 19 % MwSt.", "de", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.750 € IVA inclusa", "it", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2,750 EUR incl. VAT", "en", PriceBasis.GROSS, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
        ("2.750 € MwSt. ausweisbar", "de", PriceBasis.GROSS, VatTreatment.VAT_SHOWN, Tristate.YES),
        ("2.750 € IVA esposta", "it", PriceBasis.GROSS, VatTreatment.VAT_SHOWN, Tristate.YES),
        ("2.750 € Differenzbesteuert", "de", PriceBasis.UNKNOWN, VatTreatment.MARGIN_SCHEME, Tristate.NO),
        ("2.750 € §25a", "de", PriceBasis.UNKNOWN, VatTreatment.MARGIN_SCHEME, Tristate.NO),
        ("2.750 € § 25a UStG", "de", PriceBasis.UNKNOWN, VatTreatment.MARGIN_SCHEME, Tristate.NO),
        ("2.750 € regime del margine", "it", PriceBasis.UNKNOWN, VatTreatment.MARGIN_SCHEME, Tristate.NO),
        ("2,750 EUR margin scheme", "en", PriceBasis.UNKNOWN, VatTreatment.MARGIN_SCHEME, Tristate.NO),
        ("2.750 € MwSt. nicht ausweisbar", "de", PriceBasis.UNKNOWN, VatTreatment.NOT_STATED, Tristate.NO),
        ("2.750 € Privatverkauf", "de", PriceBasis.UNKNOWN, VatTreatment.PRIVATE_SALE, Tristate.UNKNOWN),
        ("2.750 €", "de", PriceBasis.UNKNOWN, VatTreatment.NOT_STATED, Tristate.UNKNOWN),
    ],
)
def test_parse_price_vat_wording(
    text: str, locale: str, basis: PriceBasis, treatment: VatTreatment, reclaimable: Tristate
) -> None:
    result = parse_price(text, locale)  # type: ignore[arg-type]
    assert result.basis == basis
    assert result.vat_treatment == treatment
    assert result.vat_reclaimable == reclaimable
    assert result.amount is not None  # the § 25a / VAT % numbers are never taken as the amount


def test_parse_price_vat_rate_stated() -> None:
    assert parse_price("2.750 € inkl. 19% MwSt.", "de").vat_rate_stated == Decimal("19")
    assert parse_price("2.750 € IVA 22% inclusa", "it").vat_rate_stated == Decimal("22")
    assert parse_price("2.750 € 7,7 % MwSt. inkl.", "de").vat_rate_stated == Decimal("7.7")
    assert parse_price("2.750 € 19%", "de").vat_rate_stated is None  # no VAT word -> not a VAT rate
    assert parse_price("2.750 € MwSt. ausweisbar 19%", "de").vat_rate_stated == Decimal("19")
    assert parse_price("2.750 € (MwSt. 19%)", "de").vat_rate_stated == Decimal("19")


@pytest.mark.parametrize(
    ("text", "rate"),
    [
        ("0% Finanzierung, 2.750 € inkl. MwSt.", None),  # 0 % financing is not a VAT rate
        ("2.750 € inkl. 19% MwSt., 0% Zinsen", "19"),
        ("3% Rabatt, 2.750 € IVA inclusa", None),
    ],
)
def test_parse_price_vat_rate_needs_attached_vat_wording(text: str, rate: str | None) -> None:
    result = parse_price(text, "de")
    assert result.vat_rate_stated == (Decimal(rate) if rate else None)
    assert ParseWarning.MULTIPLE_VAT_RATES not in result.warnings


def test_parse_price_conflicting_basis() -> None:
    result = parse_price("2.750 € netto inkl. MwSt.", "de")
    assert result.basis == PriceBasis.UNKNOWN
    assert ParseWarning.CONFLICTING_PRICE_BASIS in result.warnings


def test_parse_price_export_is_net() -> None:
    result = parse_price("2.400 € Exportpreis", "de")
    assert result.price_type == PriceType.EXPORT_NET
    assert result.basis == PriceBasis.NET


@pytest.mark.parametrize(
    ("text", "locale", "negotiable"),
    [
        ("2.750 € VB", "de", Tristate.YES),
        ("2.750 € VHB", "de", Tristate.YES),
        ("2.750 € Verhandlungsbasis", "de", Tristate.YES),
        ("2.750 € verhandelbar", "de", Tristate.YES),
        ("2.750 € trattabile", "it", Tristate.YES),
        ("2,750 EUR negotiable", "en", Tristate.YES),
        ("2.750 € Festpreis", "de", Tristate.NO),
        ("2.750 € nicht verhandelbar", "de", Tristate.NO),
        ("2.750 € non trattabile", "it", Tristate.NO),
        ("2.750 € prezzo fisso", "it", Tristate.NO),
        ("2.750 €", "de", Tristate.UNKNOWN),
        ("2.750 € VBA-Auto", "de", Tristate.UNKNOWN),  # 'VB' inside another word
        ("2.750 € vb", "de", Tristate.UNKNOWN),  # abbreviation is case-sensitive
    ],
)
def test_parse_price_negotiable(text: str, locale: str, negotiable: Tristate) -> None:
    assert parse_price(text, locale).negotiable == negotiable  # type: ignore[arg-type]


def test_parse_price_conflicting_negotiability() -> None:
    result = parse_price("2.750 € VB Festpreis", "de")
    assert result.negotiable == Tristate.UNKNOWN
    assert ParseWarning.CONFLICTING_NEGOTIABILITY in result.warnings


@pytest.mark.parametrize("text", ["", None, "   "])
def test_parse_price_missing(text: str | None) -> None:
    result = parse_price(text, "de")
    assert result.amount is None
    assert result.price_type == PriceType.UNKNOWN
    assert ParseWarning.EMPTY_INPUT in result.warnings


def test_parse_price_without_number() -> None:
    result = parse_price("siehe Beschreibung", "de")
    assert result.amount is None
    assert result.price_type == PriceType.UNKNOWN
    assert ParseWarning.PRICE_MISSING in result.warnings


def test_parse_price_keeps_raw_text() -> None:
    assert parse_price("2.750 € VB", "de").raw_text == "2.750 € VB"


# ---------------------------------------------------------------------------------------------
# Mileage
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale", "km"),
    [
        ("199.999 km", "de", "199999"),
        ("200.000 km", "de", "200000"),
        ("187.500 Km", "it", "187500"),
        ("187.500 KM", "de", "187500"),
        ("187.500km", "de", "187500"),
        ("180.000 chilometri", "it", "180000"),
        ("120 000 км", "mk", "120000"),
        ("199'999 km", "ch", "199999"),
        (f"199{RSQUO}999 km", "ch", "199999"),
        (f"199{NBSP}999 km", "de", "199999"),
        ("150 Tkm", "de", "150000"),
        ("150,5 Tkm", "de", "150500"),
        ("199.999,9 km", "de", "199999.9"),
    ],
)
def test_parse_mileage_km(text: str, locale: str, km: str) -> None:
    result = parse_mileage(text, locale)  # type: ignore[arg-type]
    assert result.km == Decimal(km)
    assert result.claim == OdometerClaim.SELLER_REPORTED
    assert result.original.unit == "km"
    assert result.original.text == text


def test_parse_mileage_miles_exact_conversion_unrounded() -> None:
    result = parse_mileage("124.274 mi", "de")
    assert result.km == Decimal("124274") * Decimal("1.609344")
    assert result.km == Decimal("199999.616256")
    assert result.km < Decimal("200000")
    assert result.original.unit == "mi"
    assert result.original.amount == Decimal("124274")
    assert ParseWarning.MILES_CONVERTED in result.warnings


def test_parse_mileage_miles_boundary_fails_after_conversion() -> None:
    result = parse_mileage("124,275 miles", "en")
    assert result.km == Decimal("200001.225600")
    assert result.km >= Decimal("200000")


@pytest.mark.parametrize("text", ["124.274 Meilen", "124.274 mi.", "124.274 miles", "124.274 miglia"])
def test_parse_mileage_mile_units(text: str) -> None:
    assert parse_mileage(text, "de").original.unit == "mi"


def test_miles_factor_is_exact() -> None:
    assert Decimal("1.609344") == MILES_TO_KM
    assert miles_to_km(Decimal("1")) == Decimal("1.609344")


@pytest.mark.parametrize(
    "text",
    ["ca. 150.000 km", "circa 150.000 km", "ca 150.000 km", "approx. 150,000 km", "ungefähr 150.000 km"],
)
def test_parse_mileage_estimate(text: str) -> None:
    locale = "en" if "," in text else "de"
    result = parse_mileage(text, locale)  # type: ignore[arg-type]
    assert result.km == Decimal("150000")
    assert result.claim == OdometerClaim.ESTIMATED
    assert result.original.is_estimate
    assert ParseWarning.MILEAGE_ESTIMATE in result.warnings


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("150.000 - 160.000 km", "150000", "160000"),
        ("150.000 \u2013 160.000 km", "150000", "160000"),
        ("150-160 Tkm", "150000", "160000"),
        ("150.000 bis 160.000 km", "150000", "160000"),
        ("160.000-150.000 km", "150000", "160000"),
    ],
)
def test_parse_mileage_range_is_range_only(text: str, low: str, high: str) -> None:
    result = parse_mileage(text, "de")
    assert result.km is None
    assert result.claim == OdometerClaim.RANGE_ONLY
    assert result.original.range_low == Decimal(low)
    assert result.original.range_high == Decimal(high)


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("unter 150.000 km", None, "150000"),
        ("über 200.000 km", "200000", None),
        ("< 150.000 km", None, "150000"),
    ],
)
def test_parse_mileage_bound_only(text: str, low: str | None, high: str | None) -> None:
    result = parse_mileage(text, "de")
    assert result.km is None
    assert result.claim == OdometerClaim.RANGE_ONLY
    assert result.original.range_low == (Decimal(low) if low else None)
    assert result.original.range_high == (Decimal(high) if high else None)


@pytest.mark.parametrize("text", ["unbekannt", "n.d.", "k.A.", "unknown", None, ""])
def test_parse_mileage_unknown(text: str | None) -> None:
    result = parse_mileage(text, "de")
    assert result.km is None
    assert result.claim == OdometerClaim.UNKNOWN


def test_parse_mileage_zero_is_not_zero_km() -> None:
    result = parse_mileage("0 km", "de")
    assert result.km is None
    assert ParseWarning.MILEAGE_ZERO_TREATED_UNKNOWN in result.warnings


def test_parse_mileage_unit_missing_and_default() -> None:
    missing = parse_mileage("187.500", "de")
    assert missing.km is None
    assert ParseWarning.MILEAGE_UNIT_MISSING in missing.warnings
    defaulted = parse_mileage("187.500", "de", default_unit="km")
    assert defaulted.km == Decimal("187500")
    assert ParseWarning.MILEAGE_UNIT_DEFAULTED in defaulted.warnings


def test_parse_mileage_conflicting_units() -> None:
    result = parse_mileage("120.000 km / 74.565 mi", "de")
    assert result.km is None
    assert ParseWarning.CONFLICTING_UNITS in result.warnings


def test_parse_mileage_ambiguous_locale() -> None:
    result = parse_mileage("199.999 km", "ch")  # '.' is the CH decimal mark: thousands or fraction?
    assert result.km is None
    assert result.ambiguous


@pytest.mark.parametrize(("text", "stated"), [("2.500.000 km", "2500000"), ("150.000 Tkm", "150000000")])
def test_parse_mileage_implausible_is_unknown_but_visible(text: str, stated: str) -> None:
    # '150.000 Tkm' is a unit slip; acting on 150 million km would be a guess, so km is unknown,
    # while the stated figure stays visible in the original.
    result = parse_mileage(text, "de")
    assert result.km is None
    assert result.original.amount == Decimal(stated)
    assert ParseWarning.MILEAGE_IMPLAUSIBLE in result.warnings


def test_parse_mileage_negative_is_unknown() -> None:
    result = parse_mileage("-150.000 km", "de")
    assert result.km is None
    assert ParseWarning.MILEAGE_NEGATIVE in result.warnings
    # A range dash is not a minus sign.
    assert parse_mileage("150.000 - 160.000 km", "de").claim == OdometerClaim.RANGE_ONLY


def test_speed_in_km_per_hour_is_not_a_mileage_unit() -> None:
    result = parse_mileage("180 km/h", "de")
    assert result.km is None
    assert ParseWarning.MILEAGE_UNIT_MISSING in result.warnings


def test_parse_mileage_multiple_numbers() -> None:
    result = parse_mileage("2011 150 km", "de")
    assert result.km is None


# ---------------------------------------------------------------------------------------------
# Power and displacement
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kw", "derived"),
    [
        ("103 kW (140 PS)", 103, False),
        ("103kW", 103, False),
        ("103 KW", 103, False),
        ("140 PS", 103, True),
        ("140 CV", 103, True),
        ("140 hp", 104, True),
        ("100 PS", 74, True),  # 73.549875 -> 74 half-up
    ],
)
def test_parse_power(text: str, kw: int, derived: bool) -> None:
    result = parse_power(text)
    assert result.kw == kw
    assert result.kw_derived is derived
    assert not result.conflict


def test_power_factors_exact() -> None:
    assert Decimal("0.73549875") == PS_TO_KW
    assert Decimal("0.745699872") == HP_TO_KW


def test_parse_power_conflict_keeps_stated_kw() -> None:
    result = parse_power("103 kW (170 PS)")
    assert result.kw == 103
    assert result.conflict
    assert ParseWarning.POWER_CONFLICT in result.warnings


def test_parse_power_within_tolerance_no_conflict() -> None:
    assert not parse_power("104 kW (140 PS)").conflict  # 102.97 vs 104 -> within 2 kW
    assert parse_power("105 kW (140 PS)").conflict  # 2.03 kW apart -> conflict


def test_parse_power_missing_and_multiple() -> None:
    assert parse_power("2.0 TDI").kw is None
    assert ParseWarning.MULTIPLE_POWER_VALUES in parse_power("103 kW / 125 kW").warnings
    assert parse_power(None).kw is None


def test_parse_power_rounding_flag() -> None:
    result = parse_power("103,5 kW")
    assert result.kw == 104
    assert ParseWarning.POWER_ROUNDED in result.warnings


@pytest.mark.parametrize(
    ("text", "cm3"),
    [("1.995 cm³", 1995), ("1995 ccm", 1995), ("1995 cc", 1995), ("1995 cm3", 1995), ("2'987 cm³", 2987)],
)
def test_parse_displacement_cm3(text: str, cm3: int) -> None:
    result = parse_displacement(text)
    assert result.cm3 == cm3
    assert not result.from_litres


def test_parse_displacement_litres_flagged() -> None:
    result = parse_displacement("2.0 l")
    assert result.cm3 == 2000
    assert result.from_litres
    assert ParseWarning.DISPLACEMENT_FROM_LITRES in result.warnings


@pytest.mark.parametrize("text", ["2.0 TDI", "Tiguan 2.0", "7,5 l/100 km", "1995"])
def test_parse_displacement_never_inferred(text: str) -> None:
    result = parse_displacement(text)
    assert result.cm3 is None


def test_parse_displacement_implausible_and_multiple() -> None:
    assert parse_displacement("20 cc").cm3 is None
    assert ParseWarning.MULTIPLE_DISPLACEMENTS in parse_displacement("1995 ccm / 2148 ccm").warnings


# ---------------------------------------------------------------------------------------------
# First registration
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "precision"),
    [
        ("05/2011", "2011-05", Precision.MONTH),
        ("5/2011", "2011-05", Precision.MONTH),
        ("2011-05", "2011-05", Precision.MONTH),
        ("EZ 05/2011", "2011-05", Precision.MONTH),
        ("Erstzulassung 05/2011", "2011-05", Precision.MONTH),
        ("Immatricolazione 05/2011", "2011-05", Precision.MONTH),
        ("05.2011", "2011-05", Precision.MONTH),
        ("Mai 2011", "2011-05", Precision.MONTH),
        ("maggio 2011", "2011-05", Precision.MONTH),
        ("2011", "2011", Precision.YEAR),
        ("14.05.2011", "2011-05-14", Precision.DAY),
    ],
)
def test_parse_first_registration(text: str, value: str, precision: Precision) -> None:
    result = parse_first_registration(text, as_of=date(2026, 10, 6))
    assert result.value.value == value
    assert result.value.precision == precision


def test_first_registration_month_never_invents_a_day() -> None:
    result = parse_first_registration("05/2011")
    assert result.value.value == "2011-05"
    assert result.value.month == 5


@pytest.mark.parametrize(
    ("text", "warning"),
    [
        ("13/2011", ParseWarning.INVALID_DATE),
        ("00/2011", ParseWarning.INVALID_DATE),
        ("31.02.2011", ParseWarning.INVALID_DATE),
        ("05/11", ParseWarning.TWO_DIGIT_YEAR),
        ("12/2026", ParseWarning.FUTURE_DATE),
        ("2027", ParseWarning.FUTURE_DATE),
        ("1912", ParseWarning.IMPLAUSIBLE_YEAR),
        ("05/2011 oder 06/2012", ParseWarning.MULTIPLE_DATES),
        ("neu", ParseWarning.DATE_UNPARSEABLE),
    ],
)
def test_parse_first_registration_rejects(text: str, warning: str) -> None:
    result = parse_first_registration(text, as_of=date(2026, 10, 6))
    assert result.value.value is None
    assert warning in result.warnings


def test_first_registration_current_month_is_allowed() -> None:
    assert parse_first_registration("10/2026", as_of=date(2026, 10, 6)).value.value == "2026-10"


# ---------------------------------------------------------------------------------------------
# Source timestamps
# ---------------------------------------------------------------------------------------------


def test_source_datetime_with_offset_is_exact() -> None:
    result = parse_source_datetime("2026-10-06T10:00:00+02:00", "Europe/Berlin", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    assert not result.timestamp.zone_assumed
    assert result.timestamp.assumed_zone is None


def test_source_datetime_utc_z() -> None:
    result = parse_source_datetime("2026-10-06T09:59:59Z", "Europe/Rome", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, 6, 9, 59, 59, tzinfo=UTC)
    assert result.time_precision == "second"


@pytest.mark.parametrize(
    "text",
    ["2026-10-06 10:00", "06.10.2026, 10:00 Uhr", "06.10.2026 10:00", "06/10/2026 10:00", "2026-10-06T10:00"],
)
def test_source_datetime_zoneless_gets_source_zone(text: str) -> None:
    result = parse_source_datetime(text, "Europe/Berlin", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, 6, 8, 0, tzinfo=UTC)  # CEST, not UTC
    assert result.timestamp.zone_assumed
    assert result.timestamp.assumed_zone == "Europe/Berlin"
    assert ParseWarning.ZONE_ASSUMED in result.warnings
    assert result.time_precision == "minute"


def test_source_datetime_winter_offset() -> None:
    result = parse_source_datetime("2026-01-15 10:00", "Europe/Zurich", AS_OF)
    assert result.timestamp.value == datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def test_source_datetime_date_only_precision() -> None:
    result = parse_source_datetime("2026-10-05", "Europe/Rome", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, 4, 22, 0, tzinfo=UTC)
    assert result.timestamp.precision == Precision.DAY
    assert result.time_precision == "day"
    assert ParseWarning.DATE_ONLY_START_OF_DAY_ASSUMED in result.warnings


@pytest.mark.parametrize(("text", "day"), [("20261005", 5), ("2026-W41-1", 5)])
def test_source_datetime_other_iso_date_only_forms_have_day_precision(text: str, day: int) -> None:
    result = parse_source_datetime(text, "Europe/Berlin", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, day - 1, 22, 0, tzinfo=UTC)
    assert result.time_precision == "day"
    assert ParseWarning.DATE_ONLY_START_OF_DAY_ASSUMED in result.warnings


def test_source_datetime_dst_gap_is_not_guessed() -> None:
    result = parse_source_datetime("2026-03-29 02:30", "Europe/Berlin", AS_OF)
    assert result.timestamp.value is None
    assert ParseWarning.DST_GAP_NONEXISTENT_TIME in result.warnings


def test_source_datetime_dst_fold_is_flagged() -> None:
    as_of = datetime(2026, 11, 1, tzinfo=UTC)
    result = parse_source_datetime("2026-10-25 02:30", "Europe/Berlin", as_of)
    assert result.timestamp.value == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # earlier (CEST) instant
    assert ParseWarning.DST_FOLD_AMBIGUOUS_TIME in result.warnings


def test_source_datetime_future_is_rejected() -> None:
    result = parse_source_datetime("2026-10-08T10:00:00Z", "Europe/Berlin", AS_OF)
    assert result.timestamp.value is None
    assert ParseWarning.FUTURE_TIMESTAMP in result.warnings


def test_source_datetime_within_one_day_tolerance() -> None:
    result = parse_source_datetime("2026-10-07T09:00:00Z", "Europe/Berlin", AS_OF)
    assert result.timestamp.value == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["2026-02-30", "31.02.2026", "garbage", "2026-13-01 10:00", ""])
def test_source_datetime_invalid(text: str) -> None:
    result = parse_source_datetime(text, "Europe/Berlin", AS_OF)
    assert result.timestamp.value is None
    assert result.warnings


def test_source_datetime_unknown_zone_and_naive_as_of() -> None:
    with pytest.raises(ValidationFailed):
        parse_source_datetime("2026-10-06", "Mars/Olympus", AS_OF)
    with pytest.raises(ValidationFailed):
        parse_source_datetime("2026-10-06", "Europe/Berlin", datetime(2026, 10, 6))
