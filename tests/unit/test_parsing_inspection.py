"""Unit tests for technical-inspection wording and Swiss price wording in domain.parsing.

All inputs are SYNTHETIC test strings written for these tests; none is copied from a real listing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from suv_deals.domain.enums import ClaimStatus, Precision, PriceBasis, PriceType, Tristate
from suv_deals.domain.parsing import (
    MAX_INSPECTION_TEXT_LENGTH,
    InspectionParse,
    ParseWarning,
    parse_inspection,
    parse_price,
)
from suv_deals.errors import ValidationFailed

AS_OF = date(2026, 10, 6)
CLAIMED = ClaimStatus.SELLER_CLAIMED
DENIED = ClaimStatus.SELLER_DENIED
UNKNOWN = ClaimStatus.UNKNOWN


# ---------------------------------------------------------------------------------------------
# Fresh inspection wording (positive seller claims)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale", "kind"),
    [
        ("HU/AU neu", "de", "hu_tuv"),
        ("TÜV neu", "de", "hu_tuv"),
        ("Tüv NEU!", "de", "hu_tuv"),
        ("neuer TÜV", "de", "hu_tuv"),
        ("HU + AU frisch", "de", "hu_tuv"),
        ("ab MFK", "ch", "mfk"),
        ("Frisch ab MFK und Service", "ch", "mfk"),
        ("MFK neu", "ch", "mfk"),
        ("frisch vorgeführt", "ch", "mfk"),
        ("revisionata", "it", "revisione"),
        ("revisione appena fatta", "it", "revisione"),
        ("Véhicule expertisé", "ch", "mfk"),
        ("expertisée du jour", "ch", "mfk"),
        ("collaudata", "ch", "mfk"),  # Ticino wording for the Swiss MFK
        ("collaudata", "it", "revisione"),
    ],
)
def test_fresh_inspection_wording_is_a_seller_claim(text: str, locale: str, kind: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.roadworthy_claim == CLAIMED
    assert result.fresh_inspection is True
    assert result.inspection_kind == kind
    assert result.inspection_expiry.value is None
    assert result.evidence


@pytest.mark.parametrize(
    ("text", "expiry"),
    [
        ("HU bis 05/2027", "2027-05"),
        ("TÜV 05/2027", "2027-05"),
        ("TÜV: 05.2027", "2027-05"),
        ("HU/AU bis 5/2027", "2027-05"),
        ("TÜV bis Ende 2027", "2027"),
        ("TÜV bis Juli 2027", "2027-07"),
        ("HU fällig 05/2027", "2027-05"),
        ("HU bis 14.05.2027", "2027-05-14"),
    ],
)
def test_de_due_dates_are_expiries(text: str, expiry: str) -> None:
    result = parse_inspection(text, "de", AS_OF)
    assert result.inspection_expiry.value == expiry
    assert result.roadworthy_claim == CLAIMED
    assert result.fresh_inspection is False  # a valid expiry is not a *fresh* inspection
    assert result.inspection_kind == "hu_tuv"


def test_two_digit_year_maps_to_20xx_with_warning() -> None:
    result = parse_inspection("HU 05/27", "de", AS_OF)
    expiry = result.inspection_expiry
    assert (expiry.value, expiry.precision) == ("2027-05", Precision.MONTH)
    assert ParseWarning.TWO_DIGIT_YEAR_EXPANDED in result.warnings
    assert result.roadworthy_claim == CLAIMED


def test_two_digit_year_outside_the_sanity_window_is_unknown() -> None:
    # '05/40' cannot be 2040 (more than 5 years ahead) and 1940 is implausible: no guess.
    result = parse_inspection("HU 05/40", "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert ParseWarning.INSPECTION_DATE_IMPLAUSIBLE in result.warnings
    assert result.roadworthy_claim == UNKNOWN


def test_two_digit_past_year_is_an_expired_inspection() -> None:
    result = parse_inspection("HU 05/19", "de", AS_OF)
    assert result.inspection_expiry.value == "2019-05"
    assert result.roadworthy_claim == DENIED
    assert ParseWarning.INSPECTION_EXPIRED in result.warnings


def test_new_hu_with_expiry_is_fresh_and_dated() -> None:
    result = parse_inspection("TÜV neu bis 05/2028", "de", AS_OF)
    assert (result.roadworthy_claim, result.fresh_inspection) == (CLAIMED, True)
    assert result.inspection_expiry.value == "2028-05"


def test_expiry_in_the_current_month_is_still_valid() -> None:
    result = parse_inspection("HU 10/2026", "de", AS_OF)
    assert result.roadworthy_claim == CLAIMED
    assert ParseWarning.INSPECTION_EXPIRED not in result.warnings


def test_year_only_expiry_in_the_current_year_is_imprecise() -> None:
    result = parse_inspection("TÜV bis 2026", "de", AS_OF)
    assert result.roadworthy_claim == UNKNOWN
    assert result.inspection_expiry.value == "2026"
    assert ParseWarning.INSPECTION_EXPIRY_IMPRECISE in result.warnings


@pytest.mark.parametrize(
    ("text", "locale", "expiry"),
    [
        ("MFK bis 06/2028", "ch", "2028-06"),
        ("nächste MFK 06/2028", "ch", "2028-06"),
        ("revisione fino a 05/2027", "it", "2027-05"),
        ("revisione valida fino al 05/2027", "it", "2027-05"),
        ("scadenza revisione 05/2027", "it", "2027-05"),
        ("prossima revisione maggio 2027", "it", "2027-05"),
        ("prochaine expertise 06/2027", "ch", "2027-06"),
        ("prochaine expertise juin 2027", "ch", "2027-06"),
    ],
)
def test_ch_it_fr_expiries(text: str, locale: str, expiry: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.inspection_expiry.value == expiry
    assert result.roadworthy_claim == CLAIMED


# ---------------------------------------------------------------------------------------------
# Negations, expiry in the past, conditional offers: never positive
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["vor MFK", "nicht ab MFK", "nicht frisch ab MFK", "nicht mehr ab MFK", "ohne MFK", "keine MFK",
     "Export ohne MFK", "muss zur MFK", "nicht vorgeführt", "MFK fällig", "sans expertise",
     "non expertisé", "pas encore expertisée", "à expertiser", "senza collaudo", "kein neuer TÜV"],
)  # fmt: skip
def test_not_freshly_inspected_is_never_positive(text: str) -> None:
    result = parse_inspection(text, "ch", AS_OF)
    assert result.roadworthy_claim == UNKNOWN
    assert result.fresh_inspection is False
    assert ParseWarning.INSPECTION_NOT_FRESH in result.warnings


@pytest.mark.parametrize(
    ("text", "warning"),
    [
        ("ohne TÜV", ParseWarning.INSPECTION_NOT_VALID),
        ("kein TÜV", ParseWarning.INSPECTION_NOT_VALID),
        ("ohne gültige HU", ParseWarning.INSPECTION_NOT_VALID),
        ("TÜV abgelaufen", ParseWarning.INSPECTION_EXPIRED),
        ("HU ist fällig", ParseWarning.INSPECTION_EXPIRED),
        ("MFK abgelaufen", ParseWarning.INSPECTION_EXPIRED),
        ("revisione scaduta", ParseWarning.INSPECTION_EXPIRED),
        ("scaduta la revisione", ParseWarning.INSPECTION_EXPIRED),
        ("senza revisione", ParseWarning.INSPECTION_NOT_VALID),
        ("non è ancora revisionata", ParseWarning.INSPECTION_NOT_VALID),
        ("da revisionare", ParseWarning.INSPECTION_NOT_VALID),
        ("expertise échue", ParseWarning.INSPECTION_EXPIRED),
    ],
)
def test_explicitly_invalid_inspection_is_denied(text: str, warning: ParseWarning) -> None:
    result = parse_inspection(text, "de", AS_OF)
    assert result.roadworthy_claim == DENIED
    assert result.fresh_inspection is False
    assert warning in result.warnings


@pytest.mark.parametrize(
    ("text", "locale", "expiry"),
    [
        ("HU bis 05/2026", "de", "2026-05"),
        ("TÜV 09/2026", "de", "2026-09"),
        ("MFK bis 06/2026", "ch", "2026-06"),
        ("revisione fino a 05/2026", "it", "2026-05"),
        ("prochaine expertise 2025", "ch", "2025"),
    ],
)
def test_stated_past_expiry_is_never_positive(text: str, locale: str, expiry: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.inspection_expiry.value == expiry  # the stated fact is kept ...
    assert result.roadworthy_claim == DENIED  # ... but it is never a positive claim
    assert ParseWarning.INSPECTION_EXPIRED in result.warnings


def test_new_wording_with_past_date_is_conflicting_not_positive() -> None:
    result = parse_inspection("TÜV neu 05/2025", "de", AS_OF)
    assert result.roadworthy_claim == ClaimStatus.CONFLICTING
    assert {ParseWarning.INSPECTION_EXPIRED, ParseWarning.INSPECTION_CONFLICTING} <= set(result.warnings)


@pytest.mark.parametrize(
    "text",
    [
        "ab MFK auf Wunsch",
        "Auf Wunsch frisch ab MFK",
        "ab MFK gegen Aufpreis",
        "MFK neu (+ CHF 500.-)",
        "ab MFK möglich",
        "TÜV neu nach Absprache",
        "revisionata su richiesta",
        "expertisée sur demande",
    ],
)
def test_conditional_fresh_inspection_is_an_offer_not_a_claim(text: str) -> None:
    result = parse_inspection(text, "ch", AS_OF)
    assert result.roadworthy_claim == UNKNOWN
    assert result.fresh_inspection is False
    assert ParseWarning.INSPECTION_CONDITIONAL in result.warnings


def test_unrelated_possible_wording_does_not_hide_a_claim() -> None:
    result = parse_inspection("Frisch ab MFK & Service. Probefahrt jederzeit möglich.", "ch", AS_OF)
    assert result.roadworthy_claim == CLAIMED


def test_positive_and_negative_wording_conflict() -> None:
    result = parse_inspection("Preis vor MFK CHF 2'500.-, Preis ab MFK CHF 3'200.-", "ch", AS_OF)
    assert result.roadworthy_claim == ClaimStatus.CONFLICTING
    assert ParseWarning.INSPECTION_CONFLICTING in result.warnings
    assert set(result.evidence) == {"vor MFK", "ab MFK"}


# ---------------------------------------------------------------------------------------------
# Last inspection and ambiguous dates
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale", "last"),
    [
        ("letzte MFK 2025", "ch", "2025"),
        ("Letzte MFK: 03.2025", "ch", "2025-03"),
        ("ab MFK 03.2025", "ch", "2025-03"),
        ("ultima revisione 2025", "it", "2025"),
        ("dernière expertise 2025", "ch", "2025"),
        ("expertisée le 03.2025", "ch", "2025-03"),
    ],
)
def test_last_inspection_date_is_not_an_expiry(text: str, locale: str, last: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.last_inspection.value == last
    assert result.inspection_expiry.value is None
    assert result.roadworthy_claim == UNKNOWN  # a past inspection date says nothing about today
    assert result.fresh_inspection is False


def test_last_inspection_in_the_future_is_dropped() -> None:
    result = parse_inspection("letzte MFK 2027", "ch", AS_OF)
    assert result.last_inspection.value is None
    assert ParseWarning.FUTURE_DATE in result.warnings


@pytest.mark.parametrize("text", ["MFK 05.2024", "MFK: 2023", "revisione 05/2027", "expertise 2025"])
def test_bare_swiss_and_italian_dates_are_ambiguous(text: str) -> None:
    result = parse_inspection(text, "ch", AS_OF)
    assert result.inspection_expiry.value is None
    assert result.last_inspection.value is None
    assert result.roadworthy_claim == UNKNOWN
    assert ParseWarning.INSPECTION_DATE_AMBIGUOUS in result.warnings


def test_different_stated_expiries_are_not_guessed() -> None:
    result = parse_inspection("HU bis 05/2027 ... TÜV 08/2028", "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert ParseWarning.MULTIPLE_DATES in result.warnings
    assert result.roadworthy_claim == UNKNOWN


def test_any_stated_past_expiry_blocks_a_positive_claim() -> None:
    # Regression (found by the property tests): a second, different expiry must not hide a past one.
    result = parse_inspection("TÜV neu 2027, HU bis 12/2025", "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert {ParseWarning.MULTIPLE_DATES, ParseWarning.INSPECTION_EXPIRED} <= set(result.warnings)
    assert result.roadworthy_claim == ClaimStatus.CONFLICTING
    assert result.fresh_inspection is False


def test_repeated_identical_expiry_is_one_fact() -> None:
    result = parse_inspection("HU bis 05/2027. Nochmals: TÜV 05/2027", "de", AS_OF)
    assert result.inspection_expiry.value == "2027-05"


@pytest.mark.parametrize("text", ["HU 13/2027", "HU 31.02.2027"])
def test_invalid_dates_are_unknown(text: str) -> None:
    result = parse_inspection(text, "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert ParseWarning.INVALID_DATE in result.warnings


# ---------------------------------------------------------------------------------------------
# No false positives, input handling and model invariants
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Occasion, 150'000 km, Klimaanlage, Anhängerkupplung",
        "Profitez de notre expertise",
        "Auto non fumatori",
        "HU-Bericht vorhanden",
        "TÜV-Siegel Gebrauchtwagen",
        "Hu",
        "MFK 150'000 km",
        "kein TÜV-Bericht",
    ],
)
def test_unrelated_text_is_unknown(text: str) -> None:
    result = parse_inspection(text, "ch", AS_OF)
    assert result.roadworthy_claim == UNKNOWN
    assert result.inspection_expiry.value is None


def test_whitespace_and_case_are_normalised() -> None:
    result = parse_inspection("Fahrzeug\u00a0frisch\n ab\u202fMFK", "ch", AS_OF)
    assert result.roadworthy_claim == CLAIMED


def test_empty_and_oversized_input() -> None:
    assert ParseWarning.EMPTY_INPUT in parse_inspection(None, "de", AS_OF).warnings
    assert ParseWarning.EMPTY_INPUT in parse_inspection("  ", "de", AS_OF).warnings
    big = parse_inspection("ab MFK " * (MAX_INSPECTION_TEXT_LENGTH // 7 + 1), "ch", AS_OF)
    assert big.roadworthy_claim == UNKNOWN
    assert ParseWarning.INPUT_TOO_LONG in big.warnings


def test_as_of_datetime_must_be_aware() -> None:
    aware = parse_inspection("HU 10/2026", "de", datetime(2026, 10, 6, 10, tzinfo=UTC))
    assert aware.roadworthy_claim == CLAIMED
    with pytest.raises(ValidationFailed):
        parse_inspection("HU 10/2026", "de", datetime(2026, 10, 6, 10))


def test_inspection_parse_can_never_be_verified() -> None:
    with pytest.raises(ValidationError, match="never a verified"):
        InspectionParse(roadworthy_claim=ClaimStatus.VERIFIED)
    with pytest.raises(ValidationError, match="fresh_inspection"):
        InspectionParse(roadworthy_claim=UNKNOWN, fresh_inspection=True)


# ---------------------------------------------------------------------------------------------
# Swiss price wording (spec 17)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "price_type", "basis"),
    [
        ("CHF 2'990.- exkl. MWST", PriceType.FULL_VEHICLE_ASKING, PriceBasis.NET),
        ("CHF 2'990.- inkl. MWST", PriceType.FULL_VEHICLE_ASKING, PriceBasis.GROSS),
        ("Fr. 2'990.\u2013 inkl. MWST", PriceType.FULL_VEHICLE_ASKING, PriceBasis.GROSS),
        ("CHF 2'990.- ohne MWST", PriceType.FULL_VEHICLE_ASKING, PriceBasis.NET),
        ("Exportpreis CHF 2'990.-", PriceType.EXPORT_NET, PriceBasis.NET),
        ("Export ohne MWST CHF 2'990.-", PriceType.EXPORT_NET, PriceBasis.NET),
        ("Händlerpreis Fr. 2'990.\u2013", PriceType.EXPORT_NET, PriceBasis.NET),
        ("Occasion CHF 2'990.-", PriceType.FULL_VEHICLE_ASKING, PriceBasis.UNKNOWN),
        ("CHF 2'990.\u2013 ab Platz", PriceType.FULL_VEHICLE_ASKING, PriceBasis.UNKNOWN),
        ("CHF 2'990.- TVA incluse", PriceType.FULL_VEHICLE_ASKING, PriceBasis.GROSS),
        ("CHF 2'990.- TTC", PriceType.FULL_VEHICLE_ASKING, PriceBasis.GROSS),
        ("CHF 2'990.- hors TVA", PriceType.FULL_VEHICLE_ASKING, PriceBasis.NET),
        ("Prix export CHF 2'990.-", PriceType.EXPORT_NET, PriceBasis.NET),
        ("Prix à l'export CHF 2'990.-", PriceType.EXPORT_NET, PriceBasis.NET),
    ],
)
def test_swiss_price_wording(text: str, price_type: PriceType, basis: PriceBasis) -> None:
    result = parse_price(text, "ch")
    assert (result.amount, result.currency) == (Decimal(2990), "CHF")
    assert result.price_type == price_type
    assert result.basis == basis


@pytest.mark.parametrize(
    "text",
    [
        "Fr. 2'990.\u2013",
        "CHF 2'990.-",
        "CHF 2'990.--",
        "Fr. 2\u2019990.\u2014",
        "2'990.- Fr.",
        "SFr. 2'990.-",
    ],
)
def test_swiss_dash_cents(text: str) -> None:
    result = parse_price(text, "ch")
    assert (result.amount, result.currency, result.amount_minor) == (Decimal(2990), "CHF", 299000)


def test_swiss_vat_rate_is_recorded_as_stated_only() -> None:
    result = parse_price("CHF 2'990.\u2013 inkl. 8.1% MWST", "ch")
    assert result.vat_rate_stated == Decimal("8.1")
    assert result.basis == PriceBasis.GROSS
    assert parse_price("CHF 2'990.- inkl. MWST", "ch").vat_rate_stated is None  # never assumed
    assert parse_price("CHF 2'990.- TVA 8.1% incluse", "ch").vat_rate_stated == Decimal("8.1")


def test_occasion_wording_has_no_price_effect() -> None:
    plain = parse_price("CHF 2'990.-", "ch")
    occasion = parse_price("Occasion CHF 2'990.-", "ch")
    assert occasion.model_dump(exclude={"raw_text"}) == plain.model_dump(exclude={"raw_text"})


@pytest.mark.parametrize(
    ("text", "negotiable"),
    [
        ("CHF 2'990.- à discuter", Tristate.YES),
        ("CHF 2'990.- prix ferme", Tristate.NO),
        ("CHF 2'990.- non négociable", Tristate.NO),
    ],
)
def test_swiss_french_negotiability(text: str, negotiable: Tristate) -> None:
    assert parse_price(text, "ch").negotiable == negotiable


# ---------------------------------------------------------------------------------------------
# Independent review regressions
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "locale"),
    [
        # Spec tables and forms: visible text joins the label and its value with a space.
        ("Ab MFK: Nein", "ch"),
        ("Ab MFK Nein", "ch"),
        ("Ab MFK? Nein", "ch"),
        ("Frisch ab MFK: nein", "ch"),
        ("Ab MFK / nein", "ch"),
        ("MFK neu: nein", "ch"),
        ("Frisch vorgeführt: nein", "ch"),
        ("TÜV neu: nein", "de"),
        ("HU/AU neu: Nein", "de"),
        ("Neuer TÜV: nein", "de"),
        ("Expertisé: non", "ch"),
        ("Expertisée - non", "ch"),
        ("Revisionata: no", "it"),
        ("Collaudata: no", "ch"),
        ("ab MFK nicht möglich", "ch"),
        ("TÜV neu ist nicht möglich", "de"),
    ],
)
def test_negative_answer_after_fresh_wording_is_never_positive(text: str, locale: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.roadworthy_claim == UNKNOWN
    assert result.fresh_inspection is False
    assert ParseWarning.INSPECTION_NOT_FRESH in result.warnings


@pytest.mark.parametrize(
    ("text", "locale"),
    [
        ("Ab MFK: Ja", "ch"),
        ("TÜV neu: ja", "de"),
        ("Expertisé: oui", "ch"),
        ("Ab MFK, non fumeur", "ch"),
        ("Expertisée non-fumeur", "ch"),
        ("Auto revisionata, non fumatori", "it"),
        ("Revisionata non fumatori", "it"),
        ("Véhicule expertisé non accidenté", "ch"),
        ("Revisionata non incidentata", "it"),
        ("Expertisée non négociable", "ch"),
    ],
)
def test_positive_answers_and_non_smoker_wording_stay_claims(text: str, locale: str) -> None:
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.roadworthy_claim == CLAIMED
    assert result.fresh_inspection is True


@pytest.mark.parametrize(
    "text",
    [
        "motore revisionato",
        "Motore completamente revisionato a 180000 km",
        "cambio automatico revisionato",
        "turbo revisionato, auto in ordine",
        "testata revisionata",
        "frizione appena revisionata",
        "motore revisionato nel 2024",
        "revisione completa del motore",
        "revisione fatta al cambio",
        "revisione motore",
    ],
)
def test_italian_component_overhaul_is_not_inspection_wording(text: str) -> None:
    # "revisionato" with a component means "overhauled", not "passed the periodic inspection".
    result = parse_inspection(text, "it", AS_OF)
    assert result.roadworthy_claim == UNKNOWN
    assert result.inspection_kind == "unknown"
    assert result.last_inspection.value is None
    assert result.evidence == ()


def test_component_overhaul_does_not_hide_a_real_inspection_statement() -> None:
    both = parse_inspection("Motore revisionato, auto revisionata", "it", AS_OF)
    assert (both.roadworthy_claim, both.evidence) == (CLAIMED, ("revisionata",))
    dated = parse_inspection("Cambio revisionato, revisione fino a 05/2027", "it", AS_OF)
    assert dated.inspection_expiry.value == "2027-05"
    assert dated.roadworthy_claim == CLAIMED


@pytest.mark.parametrize("text", ["TÜV 2000 km", "HU 2025 km", "TÜV 2030 kW", "HU 2028 €", "MFK 2024 CHF"])
def test_year_followed_by_a_unit_is_a_quantity_not_a_date(text: str) -> None:
    result = parse_inspection(text, "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert result.roadworthy_claim == UNKNOWN
    assert ParseWarning.INSPECTION_EXPIRED not in result.warnings


def test_two_digit_year_past_the_window_ahead_is_a_past_expiry_not_a_lost_date() -> None:
    # '99' cannot be 2099 (more than 5 years ahead): it is read as 1999, a past expiry. Reading it
    # as an implausible 2099 instead would drop the date and let other fresh wording win (found by
    # the property test with "ab MFK HU 12/99").
    alone = parse_inspection("HU 05/99", "de", AS_OF)
    assert (alone.inspection_expiry.value, alone.roadworthy_claim) == ("1999-05", DENIED)
    with_fresh = parse_inspection("ab MFK, HU 12/99", "ch", AS_OF)
    assert with_fresh.roadworthy_claim == ClaimStatus.CONFLICTING
    assert ParseWarning.INSPECTION_EXPIRED in with_fresh.warnings


@pytest.mark.parametrize(
    "text",
    ["HU bis 05/1990", "TÜV neu, HU bis 05/1990", "MFK bis 1980", "revisione fino a 05/1985, revisionata"],
)
def test_stated_expiry_older_than_the_window_is_still_a_past_expiry(text: str) -> None:
    # Too old to store as a date, but a stated expiry in the past is never a positive claim.
    result = parse_inspection(text, "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert ParseWarning.INSPECTION_DATE_IMPLAUSIBLE in result.warnings
    assert ParseWarning.INSPECTION_EXPIRED in result.warnings
    assert result.roadworthy_claim in (DENIED, ClaimStatus.CONFLICTING)
    assert result.fresh_inspection is False


def test_implausible_future_expiry_is_dropped_without_a_denial() -> None:
    result = parse_inspection("HU bis 05/2040", "de", AS_OF)
    assert result.inspection_expiry.value is None
    assert result.roadworthy_claim == UNKNOWN
    assert ParseWarning.INSPECTION_EXPIRED not in result.warnings


@pytest.mark.parametrize(
    ("text", "locale"),
    [
        ("ab MFK vor 3 Jahren", "ch"),
        ("Ab MFK seit 2 Jahren", "ch"),
        ("ab MFK vor ca. 2 Jahren", "ch"),
        ("letzte MFK vor 4 Jahren", "ch"),
        ("TÜV neu vor 2 Jahren", "de"),
        ("HU/AU neu seit 2 Wochen", "de"),
        ("expertisée il y a 3 ans", "ch"),
        ("dernière expertise il y a 2 ans", "ch"),
        ("revisionata 2 anni fa", "it"),
        ("ultima revisione tre anni fa", "it"),
        ("collaudata un anno fa", "ch"),
    ],
)
def test_last_inspection_relative_to_an_unknown_writing_date_is_not_fresh(text: str, locale: str) -> None:
    # "3 years ago" relative to when the ad was written: never a fresh inspection, no derived date.
    result = parse_inspection(text, locale, AS_OF)  # type: ignore[arg-type]
    assert result.roadworthy_claim == UNKNOWN
    assert result.fresh_inspection is False
    assert result.last_inspection.value is None and result.inspection_expiry.value is None
    assert ParseWarning.INSPECTION_DATE_RELATIVE in result.warnings


@pytest.mark.parametrize(
    "text", ["ab MFK vor Auslieferung", "TÜV neu vor Übergabe", "Frisch ab MFK, Probefahrt vor 2 Tagen"]
)
def test_before_delivery_promises_and_unrelated_relative_dates_keep_the_claim(text: str) -> None:
    assert parse_inspection(text, "ch", AS_OF).roadworthy_claim == CLAIMED


@pytest.mark.parametrize("text", ["TÜV seit 2 Jahren abgelaufen", "MFK seit 3 Monaten überfällig"])
def test_expired_since_wording_is_denied(text: str) -> None:
    result = parse_inspection(text, "ch", AS_OF)
    assert result.roadworthy_claim == DENIED
    assert ParseWarning.INSPECTION_EXPIRED in result.warnings
