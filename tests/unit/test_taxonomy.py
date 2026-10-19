"""Unit tests for domain.taxonomy and config/vehicle_taxonomy.yaml.

The taxonomy file is unverified engineering reference data; these tests check the matcher's
behaviour and the file's structural rules, not the historical accuracy of each year range.
Inline taxonomies below are SYNTHETIC ("Example" make) and exist only for these tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from suv_deals.domain.enums import BodyType, Confidence
from suv_deals.domain.taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    SUV_CLASSES,
    TaxonomyWarning,
    VehicleTaxonomy,
    default_taxonomy,
    load_taxonomy,
    match_vehicle,
    name_tokens,
    parse_taxonomy,
)
from suv_deals.errors import ValidationFailed


@pytest.fixture(scope="module")
def taxonomy() -> VehicleTaxonomy:
    return default_taxonomy()


def synthetic_doc(**model_overrides: Any) -> dict[str, Any]:
    model: dict[str, Any] = {
        "canonical": "Trail",
        "aliases": ["Trail X"],
        "class": "suv",
        "generations": [
            {"code": "G1", "label": "Trail I", "from_year": 2000, "to_year": 2006},
            {"code": "G2", "label": "Trail II", "from_year": 2007, "to_year": 2014, "facelift_year": 2011},
        ],
    }
    model.update(model_overrides)
    return {
        "verification": "unverified_reference",
        "version": "synthetic-1",
        "makes": [{"canonical": "Example", "aliases": ["EX"], "models": [model]}],
    }


# ---------------------------------------------------------------------------------------------
# File structure
# ---------------------------------------------------------------------------------------------


def test_repository_taxonomy_is_marked_unverified() -> None:
    raw = yaml.safe_load(DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8"))
    assert raw["verification"] == "unverified_reference"
    assert "owner" in raw["note"].lower() or "review" in raw["note"].lower()


def test_repository_taxonomy_size_and_classes(taxonomy: VehicleTaxonomy) -> None:
    models = [m for make in taxonomy.document.makes for m in make.models]
    suv_models = [m for m in models if m.vehicle_class in SUV_CLASSES]
    assert len(suv_models) >= 45
    excluded = {m.canonical: m.vehicle_class for m in models if m.vehicle_class not in SUV_CLASSES}
    assert excluded == {
        "Navara": "pickup",
        "Hilux": "pickup",
        "L200": "pickup",
        "Ranger": "pickup",
        "Amarok": "pickup",
        "D-Max": "pickup",
        "BT-50": "pickup",
        "Outback": "estate",
        "XC70": "estate",
        "A6 allroad": "estate",
        "A4 allroad": "estate",
        "Octavia Scout": "estate",
    }
    # Exclusions never carry (unverifiable) generation data.
    assert all(
        not m.generations for make in taxonomy.document.makes for m in make.models if m.canonical in excluded
    )


def test_repository_taxonomy_generations_are_consistent(taxonomy: VehicleTaxonomy) -> None:
    for make in taxonomy.document.makes:
        for model in make.models:
            for generation in model.generations:
                assert generation.to_year is None or generation.from_year <= generation.to_year
                assert 1990 <= generation.from_year <= 2026


def test_load_taxonomy_errors(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        load_taxonomy(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("makes: [", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_taxonomy(bad)
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text("verification: whatever\nversion: x\nmakes: []\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_taxonomy(wrong)


@pytest.mark.parametrize(
    "overrides",
    [
        {"generations": [{"code": "G1", "label": "x", "from_year": 2010, "to_year": 2005}]},
        {
            "generations": [
                {"code": "G1", "label": "x", "from_year": 2000, "to_year": 2005, "facelift_year": 2009}
            ]
        },
        {
            "generations": [
                {"code": "G1", "label": "x", "from_year": 2000, "to_year": 2005},
                {"code": "G1", "label": "y", "from_year": 2006, "to_year": 2009},
            ]
        },
        {"class": "pickup"},  # non-SUV entries must say why they are excluded
        {"class": "sedan"},
    ],
)
def test_parse_taxonomy_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        parse_taxonomy(synthetic_doc(**overrides))


def test_parse_taxonomy_rejects_ambiguous_aliases() -> None:
    doc = synthetic_doc()
    doc["makes"].append({"canonical": "Other", "aliases": ["EX"], "models": []})
    with pytest.raises(ValidationFailed):
        parse_taxonomy(doc)
    doc = synthetic_doc()
    doc["makes"][0]["models"].append({"canonical": "Peak", "aliases": ["Trail"], "class": "suv"})
    with pytest.raises(ValidationFailed):
        parse_taxonomy(doc)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("ML320", ("ml", "320")),
        ("X-Trail", ("x", "trail")),
        ("Škoda", ("skoda",)),
        ("Citroën C-Crosser", ("citroen", "c", "crosser")),
        ("RAV 4", ("rav", "4")),
        ("RAV4", ("rav", "4")),
        ("", ()),
        (None, ()),
    ],
)
def test_name_tokens(text: str | None, tokens: tuple[str, ...]) -> None:
    assert name_tokens(text) == tokens


# ---------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "model", "canonical_make", "canonical_model"),
    [
        ("VW", "Tiguan", "Volkswagen", "Tiguan"),
        ("Volkswagen", "Tiguan 2.0 TDI 4Motion", "Volkswagen", "Tiguan"),
        ("Mercedes", "ML 320 CDI", "Mercedes-Benz", "M-Class"),
        ("MB", "ML320", "Mercedes-Benz", "M-Class"),
        ("Mercedes-Benz", "GLK 220 CDI", "Mercedes-Benz", "GLK"),
        ("Toyota", "RAV 4", "Toyota", "RAV4"),
        ("Toyota", "RAV-4 2.2 D-4D", "Toyota", "RAV4"),
        ("Nissan", "X Trail", "Nissan", "X-Trail"),
        ("Skoda", "Yeti", "Škoda", "Yeti"),
        ("Citroen", "C-Crosser", "Citroën", "C-Crosser"),
        ("Land Rover", "Freelander 2", "Land Rover", "Freelander"),
        ("Range Rover", "Sport", "Land Rover", "Range Rover Sport"),
        ("Range Rover", "Range Rover Sport TDV6", "Land Rover", "Range Rover Sport"),
        ("Land Rover", "Range Rover Evoque", "Land Rover", "Range Rover Evoque"),
        ("Land Rover", "Discovery Sport", "Land Rover", "Discovery Sport"),
        ("Toyota", "Land Cruiser V8", "Toyota", "Land Cruiser V8"),
        ("Jeep", "Grand Cherokee 3.0 CRD", "Jeep", "Grand Cherokee"),
        ("Jeep", "Cherokee 2.8 CRD", "Jeep", "Cherokee"),
        ("Honda", "CRV", "Honda", "CR-V"),
        ("Hyundai", "ix35", "Hyundai", "ix35"),
        ("VW Tiguan", None, "Volkswagen", "Tiguan"),
    ],
)
def test_match_fields(
    taxonomy: VehicleTaxonomy, make: str, model: str | None, canonical_make: str, canonical_model: str
) -> None:
    result = taxonomy.match_vehicle(make, model, None, 2010)
    assert (result.make, result.model) == (canonical_make, canonical_model)
    assert result.is_suv is True
    assert result.matched_via == "fields"
    assert result.confidence == Confidence.HIGH
    assert TaxonomyWarning.TAXONOMY_UNVERIFIED in result.warnings


def test_model_contained_in_field_has_medium_confidence(taxonomy: VehicleTaxonomy) -> None:
    result = taxonomy.match_vehicle("Volkswagen", "Neuer Tiguan Sport", None, 2010)
    assert result.model == "Tiguan"
    assert result.confidence == Confidence.MEDIUM


@pytest.mark.parametrize(
    ("make", "model", "body"),
    [
        ("Nissan", "Navara", BodyType.PICKUP),
        ("Nissan", "NP300 Navara 2.5 dCi", BodyType.PICKUP),
        ("Toyota", "Hilux 2.5 D-4D", BodyType.PICKUP),
        ("Mitsubishi", "L200 Double Cab", BodyType.PICKUP),
        ("Ford", "Ranger", BodyType.PICKUP),
        ("VW", "Amarok", BodyType.PICKUP),
        ("Isuzu", "D-Max", BodyType.PICKUP),
        ("Mazda", "BT-50", BodyType.PICKUP),
        ("Subaru", "Outback", BodyType.ESTATE),
        ("Volvo", "XC70 D5 AWD", BodyType.ESTATE),
        ("Audi", "A6 Allroad quattro", BodyType.ESTATE),
        ("Audi", "allroad quattro 2.5 TDI", BodyType.ESTATE),
        ("Audi", "A4 allroad", BodyType.ESTATE),
        ("Skoda", "Octavia Scout", BodyType.ESTATE),
    ],
)
def test_excluded_models_are_not_suv(
    taxonomy: VehicleTaxonomy, make: str, model: str, body: BodyType
) -> None:
    result = taxonomy.match_vehicle(make, model, None, 2010)
    assert result.is_suv is False
    assert result.body_type == body
    assert result.exclusion_reason


@pytest.mark.parametrize(
    ("make", "model", "warning"),
    [
        ("VW", "Golf", TaxonomyWarning.MODEL_UNKNOWN),
        ("Lada", "Niva", TaxonomyWarning.MAKE_UNKNOWN),
        ("Example", "Trail", TaxonomyWarning.MAKE_UNKNOWN),
    ],
)
def test_unknown_is_none_not_false(taxonomy: VehicleTaxonomy, make: str, model: str, warning: str) -> None:
    result = taxonomy.match_vehicle(make, model, None, 2010)
    assert result.is_suv is None
    assert result.model is None
    assert warning in result.warnings


def test_title_fallback_only_when_fields_missing(taxonomy: VehicleTaxonomy) -> None:
    fallback = taxonomy.match_vehicle(None, None, "Toyota RAV 4 2.2 D-4D 4x4", 2009)
    assert (fallback.make, fallback.model) == ("Toyota", "RAV4")
    assert fallback.matched_via == "title"
    assert fallback.confidence == Confidence.LOW
    assert TaxonomyWarning.TITLE_FALLBACK in fallback.warnings
    # A present but unknown model field is not overridden by the title.
    no_fallback = taxonomy.match_vehicle("Toyota", "Corolla", "Toyota RAV4 look-alike", 2009)
    assert no_fallback.model is None
    assert no_fallback.is_suv is None


def test_title_fallback_with_make_field(taxonomy: VehicleTaxonomy) -> None:
    result = taxonomy.match_vehicle("Kia", None, "Kia Sportage 2.0 CRDi AWD", 2012)
    assert result.model == "Sportage"
    assert result.confidence == Confidence.LOW


def test_title_ambiguity_is_not_guessed(taxonomy: VehicleTaxonomy) -> None:
    two_makes = taxonomy.match_vehicle(None, None, "Toyota RAV4 or Honda CR-V", 2010)
    assert two_makes.make is None and two_makes.is_suv is None
    assert TaxonomyWarning.TITLE_AMBIGUOUS in two_makes.warnings
    two_models = taxonomy.match_vehicle("Nissan", None, "Nissan Qashqai X-Trail parts", 2010)
    assert two_models.model is None
    assert TaxonomyWarning.TITLE_AMBIGUOUS in two_models.warnings


def test_nothing_known(taxonomy: VehicleTaxonomy) -> None:
    result = taxonomy.match_vehicle(None, None, None, None)
    assert result.is_suv is None
    assert result.matched_via == "none"


def test_canonical_make(taxonomy: VehicleTaxonomy) -> None:
    assert taxonomy.canonical_make("vw") == "Volkswagen"
    assert taxonomy.canonical_make("Mercedes Benz") == "Mercedes-Benz"
    assert taxonomy.canonical_make("Unknown Motors") is None
    assert taxonomy.canonical_make(None) is None


# ---------------------------------------------------------------------------------------------
# Generations
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "generation", "candidates", "warning"),
    [
        (2003, "G1", ("G1",), None),
        (2010, "G2", ("G2",), None),
        (2012, "G2", ("G2",), None),
        (2006, None, ("G1", "G2"), TaxonomyWarning.GENERATION_AMBIGUOUS),  # boundary year
        (2007, None, ("G1", "G2"), TaxonomyWarning.GENERATION_AMBIGUOUS),
        (2005, None, ("G1",), TaxonomyWarning.GENERATION_AMBIGUOUS),  # within 1 year of G1's end
        (2008, None, ("G2",), TaxonomyWarning.GENERATION_AMBIGUOUS),  # within 1 year of 2007
        (2001, None, ("G1",), TaxonomyWarning.GENERATION_AMBIGUOUS),  # an unlisted predecessor is possible
        (2014, None, ("G2",), TaxonomyWarning.GENERATION_AMBIGUOUS),
        (2015, None, ("G2",), TaxonomyWarning.GENERATION_AMBIGUOUS),
        (2016, None, (), TaxonomyWarning.GENERATION_OUT_OF_RANGE),
        (1990, None, (), TaxonomyWarning.GENERATION_OUT_OF_RANGE),
        (None, None, ("G1", "G2"), TaxonomyWarning.GENERATION_YEAR_UNKNOWN),
    ],
)
def test_generation_rules(
    year: int | None, generation: str | None, candidates: tuple[str, ...], warning: str | None
) -> None:
    taxonomy = parse_taxonomy(synthetic_doc())
    result = taxonomy.match_vehicle("EX", "Trail X", None, year)
    assert result.model == "Trail"
    assert result.generation == generation
    assert result.generation_candidates == candidates
    if warning is None:
        assert not {
            TaxonomyWarning.GENERATION_AMBIGUOUS,
            TaxonomyWarning.GENERATION_OUT_OF_RANGE,
            TaxonomyWarning.GENERATION_YEAR_UNKNOWN,
        } & set(result.warnings)
    else:
        assert warning in result.warnings


@pytest.mark.parametrize(
    ("model", "canonical"),
    [
        ("Pajero Pinin 2.0 GDI", "Pajero Pinin"),
        ("Pajero Sport 2.5 DI-D", "Pajero Sport"),
        ("Pajero 3.2", "Pajero"),
    ],
)
def test_distinct_pajero_models_never_inherit_pajero_generations(
    taxonomy: VehicleTaxonomy, model: str, canonical: str
) -> None:
    result = taxonomy.match_vehicle("Mitsubishi", model, None, 2003)
    assert result.model == canonical
    assert result.is_suv is True
    if canonical != "Pajero":
        assert result.generation is None
        assert TaxonomyWarning.GENERATION_NOT_IN_TAXONOMY in result.warnings


@pytest.mark.parametrize(
    ("make", "model"), [("Audi", "A6 Avant"), ("Skoda", "Octavia Combi"), ("Volvo", "V70")]
)
def test_plain_estates_stay_unknown(taxonomy: VehicleTaxonomy, make: str, model: str) -> None:
    # Only the named raised estates are excluded; other models remain unknown (never False).
    assert taxonomy.match_vehicle(make, model, None, 2010).is_suv is None


def test_model_without_generations(taxonomy: VehicleTaxonomy) -> None:
    result = taxonomy.match_vehicle("Opel", "Antara", None, 2010)
    assert result.is_suv is True
    assert result.generation is None
    assert TaxonomyWarning.GENERATION_NOT_IN_TAXONOMY in result.warnings


@pytest.mark.parametrize(
    ("make", "model", "year", "generation"),
    [
        ("VW", "Tiguan", 2011, "5N"),
        ("Toyota", "RAV4", 2009, "XA30"),
        ("BMW", "X5", 2009, "E70"),
        ("Mercedes-Benz", "ML 320", 2008, "W164"),
        ("Hyundai", "Tucson", 2012, None),  # no Tucson generation 2010-2015 in the reference data
    ],
)
def test_repository_generation_examples(
    taxonomy: VehicleTaxonomy, make: str, model: str, year: int, generation: str | None
) -> None:
    assert taxonomy.match_vehicle(make, model, None, year).generation == generation


def test_unverified_flag_absent_for_owner_verified() -> None:
    doc = synthetic_doc()
    doc["verification"] = "owner_verified"
    result = parse_taxonomy(doc).match_vehicle("Example", "Trail", None, 2010)
    assert TaxonomyWarning.TAXONOMY_UNVERIFIED not in result.warnings
    assert result.verification == "owner_verified"


def test_module_level_match_vehicle_uses_default() -> None:
    result = match_vehicle("VW", "Touareg", None, 2008)
    assert result.model == "Touareg"
    assert result.taxonomy_version == default_taxonomy().version
