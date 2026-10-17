"""SUV make/model/generation taxonomy matching (spec sections 3, 14, 15).

The reference data lives in ``config/vehicle_taxonomy.yaml`` and is explicitly
``verification: unverified_reference`` -- engineering reference data for the owner to review,
not manufacturer-verified facts.

Business rules:

- An unknown make or model gives ``is_suv=None`` (unknown), never ``False``. Only models listed
  with a non-SUV class (``pickup``, ``estate``) are positively ``is_suv=False``.
- Make/model *fields* are matched first (high confidence for a leading match, medium when the
  model name only appears inside the field). The *title* is consulted only when the make or model
  field is missing, with low confidence.
- A generation code is returned only when exactly one generation's year range contains the
  first-registration year *and* that year is more than one year away from both of that
  generation's boundaries. Otherwise ``generation=None``, the plausible ``generation_candidates``
  and the warning ``GENERATION_AMBIGUOUS`` (registration year and production generation can differ
  around a model change). Comparable selection must never assume a guessed generation (spec 15).

The loader is the only function touching the file system; matching is pure.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from suv_deals.domain.enums import BodyType, Confidence
from suv_deals.errors import ValidationFailed

VehicleClass = Literal["suv", "offroad", "crossover", "pickup", "estate"]
SUV_CLASSES: frozenset[str] = frozenset({"suv", "offroad", "crossover"})
CLASS_BODY_TYPES: dict[str, BodyType] = {
    "suv": BodyType.SUV,
    "offroad": BodyType.OFFROAD,
    "crossover": BodyType.CROSSOVER,
    "pickup": BodyType.PICKUP,
    "estate": BodyType.ESTATE,
}
GENERATION_BOUNDARY_MARGIN_YEARS = 1
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "config" / "vehicle_taxonomy.yaml"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class TaxonomyWarning:
    MAKE_MISSING = "MAKE_MISSING"
    MAKE_UNKNOWN = "MAKE_UNKNOWN"
    MODEL_MISSING = "MODEL_MISSING"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    TITLE_FALLBACK = "TAXONOMY_TITLE_FALLBACK"
    TITLE_AMBIGUOUS = "TAXONOMY_TITLE_AMBIGUOUS"
    GENERATION_AMBIGUOUS = "GENERATION_AMBIGUOUS"
    GENERATION_OUT_OF_RANGE = "GENERATION_OUT_OF_RANGE"
    GENERATION_YEAR_UNKNOWN = "GENERATION_YEAR_UNKNOWN"
    GENERATION_NOT_IN_TAXONOMY = "GENERATION_NOT_IN_TAXONOMY"
    TAXONOMY_UNVERIFIED = "TAXONOMY_UNVERIFIED"


# ---------------------------------------------------------------------------------------------
# Document model (validated YAML)
# ---------------------------------------------------------------------------------------------


class Generation(BaseModel):
    model_config = _FROZEN

    code: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=80)
    from_year: int = Field(ge=1950, le=2100)
    to_year: int | None = Field(default=None, ge=1950, le=2100)  # None = still in production
    facelift_year: int | None = Field(default=None, ge=1950, le=2100)

    @model_validator(mode="after")
    def _years(self) -> Generation:
        if self.to_year is not None and self.to_year < self.from_year:
            raise ValueError(f"generation {self.code}: to_year before from_year")
        if self.facelift_year is not None and not (
            self.from_year <= self.facelift_year <= (self.to_year or self.facelift_year)
        ):
            raise ValueError(f"generation {self.code}: facelift_year outside the generation")
        return self

    def contains(self, year: int) -> bool:
        return self.from_year <= year and (self.to_year is None or year <= self.to_year)

    def near(self, year: int, margin: int) -> bool:
        return self.from_year - margin <= year and (self.to_year is None or year <= self.to_year + margin)

    def distance_to_boundary(self, year: int) -> int:
        distances = [abs(year - self.from_year)]
        if self.to_year is not None:
            distances.append(abs(year - self.to_year))
        return min(distances)


class ModelEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    canonical: str = Field(min_length=1, max_length=80)
    aliases: tuple[str, ...] = ()
    vehicle_class: VehicleClass = Field(alias="class")
    exclusion_reason: str | None = Field(default=None, max_length=200)
    generations: tuple[Generation, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> ModelEntry:
        codes = [g.code for g in self.generations]
        if len(codes) != len(set(codes)):
            raise ValueError(f"model {self.canonical}: duplicate generation codes")
        if self.vehicle_class not in SUV_CLASSES and not self.exclusion_reason:
            raise ValueError(f"model {self.canonical}: non-SUV entries need an exclusion_reason")
        return self

    @property
    def is_suv(self) -> bool:
        return self.vehicle_class in SUV_CLASSES


class MakeEntry(BaseModel):
    model_config = _FROZEN

    canonical: str = Field(min_length=1, max_length=80)
    aliases: tuple[str, ...] = ()
    # Make-field values that really name a model line, e.g. "Range Rover" for Land Rover.
    model_prefix_aliases: tuple[str, ...] = ()
    models: tuple[ModelEntry, ...]


class TaxonomyDocument(BaseModel):
    model_config = _FROZEN

    verification: Literal["unverified_reference", "owner_verified"]
    version: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=2000)
    makes: tuple[MakeEntry, ...] = Field(min_length=1)


# ---------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------

Tokens = tuple[str, ...]
_LETTER_DIGIT = re.compile(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")


def name_tokens(text: str | None) -> Tokens:
    """Normalise a name for matching: strip accents, casefold, split on punctuation and at
    letter/digit boundaries (``'ML320'`` -> ``('ml', '320')``, ``'X-Trail'`` -> ``('x', 'trail')``)."""
    if not text:
        return ()
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    spaced = re.sub(r"[^0-9a-z]+", " ", ascii_only)
    spaced = _LETTER_DIGIT.sub(" ", spaced)
    return tuple(spaced.split())


def _find(haystack: Tokens, needle: Tokens) -> int:
    """Index of the first contiguous occurrence of ``needle`` in ``haystack`` or -1."""
    n = len(needle)
    for i in range(len(haystack) - n + 1):
        if haystack[i : i + n] == needle:
            return i
    return -1


class TaxonomyMatch(BaseModel):
    model_config = _FROZEN

    make: str | None = None
    model: str | None = None
    vehicle_class: VehicleClass | None = None
    is_suv: bool | None = None
    body_type: BodyType = BodyType.UNKNOWN
    exclusion_reason: str | None = None
    generation: str | None = None
    generation_label: str | None = None
    generation_candidates: tuple[str, ...] = ()
    confidence: Confidence | None = None
    matched_via: Literal["fields", "title", "none"] = "none"
    warnings: tuple[str, ...] = ()
    taxonomy_version: str
    verification: str


class VehicleTaxonomy:
    """Indexed, immutable view of a validated ``TaxonomyDocument``."""

    def __init__(self, document: TaxonomyDocument) -> None:
        self.document = document
        self.version = document.version
        self.verification = document.verification
        self._makes: list[tuple[Tokens, MakeEntry, Tokens]] = []
        self._models: dict[str, list[tuple[Tokens, ModelEntry]]] = {}
        seen_make_aliases: dict[Tokens, str] = {}
        for make in document.makes:
            no_prefix: Tokens = ()
            names = [(name_tokens(a), no_prefix) for a in (make.canonical, *make.aliases)]
            names += [(name_tokens(a), name_tokens(a)) for a in make.model_prefix_aliases]
            for toks, prefix in names:
                owner = seen_make_aliases.get(toks)
                if owner is not None and owner != make.canonical:
                    raise ValidationFailed(f"taxonomy make alias {' '.join(toks)!r} is ambiguous")
                seen_make_aliases[toks] = make.canonical
                self._makes.append((toks, make, prefix))
            model_index: list[tuple[Tokens, ModelEntry]] = []
            seen_model: dict[Tokens, str] = {}
            for model in make.models:
                for alias in (model.canonical, *model.aliases):
                    toks = name_tokens(alias)
                    owner = seen_model.get(toks)
                    if owner is not None and owner != model.canonical:
                        raise ValidationFailed(
                            f"taxonomy model alias {alias!r} is ambiguous in {make.canonical}"
                        )
                    seen_model[toks] = model.canonical
                    model_index.append((toks, model))
            model_index.sort(key=lambda item: len(item[0]), reverse=True)
            self._models[make.canonical] = model_index
        self._makes.sort(key=lambda item: len(item[0]), reverse=True)

    # -- make ----------------------------------------------------------------------------------

    def _resolve_make(self, tokens: Tokens) -> tuple[MakeEntry, Tokens, Tokens] | None:
        """Longest make alias that is a prefix of ``tokens``: (make, implied model prefix, rest)."""
        for alias, make, prefix in self._makes:
            if alias and tokens[: len(alias)] == alias:
                return make, prefix, tokens[len(alias) :]
        return None

    def canonical_make(self, make: str | None) -> str | None:
        resolved = self._resolve_make(name_tokens(make))
        return resolved[0].canonical if resolved else None

    # -- model ---------------------------------------------------------------------------------

    def _model_prefix(self, make: MakeEntry, tokens: Tokens) -> ModelEntry | None:
        for alias, model in self._models[make.canonical]:
            if tokens[: len(alias)] == alias:
                return model
        return None

    def _model_contained(self, make: MakeEntry, tokens: Tokens) -> tuple[ModelEntry | None, bool]:
        """Model whose alias occurs anywhere in ``tokens``.

        Matches lying inside a longer match of another model are ignored ("Cherokee" inside
        "Grand Cherokee", "Range Rover" inside "Range Rover Sport"); two or more remaining distinct
        models are ambiguous and return ``(None, True)``.
        """
        spans: list[tuple[int, int, ModelEntry]] = []
        for alias, model in self._models[make.canonical]:
            n = len(alias)
            for i in range(len(tokens) - n + 1):
                if tokens[i : i + n] == alias:
                    spans.append((i, i + n, model))
        kept = [
            (start, end, model)
            for start, end, model in spans
            if not any(
                other is not model and o_start <= start and end <= o_end and (o_end - o_start) > (end - start)
                for o_start, o_end, other in spans
            )
        ]
        found = {model.canonical: model for _, _, model in kept}
        if len(found) > 1:
            return None, True
        return (next(iter(found.values())), False) if found else (None, False)

    def _strip_make(self, make: MakeEntry, tokens: Tokens) -> Tokens:
        resolved = self._resolve_make(tokens)
        if resolved and resolved[0].canonical == make.canonical and not resolved[1]:
            return resolved[2]
        return tokens

    # -- generation ----------------------------------------------------------------------------

    @staticmethod
    def _generation(
        model: ModelEntry, year: int | None
    ) -> tuple[Generation | None, tuple[str, ...], list[str]]:
        if not model.generations:
            return None, (), [TaxonomyWarning.GENERATION_NOT_IN_TAXONOMY]
        if year is None:
            return None, tuple(g.code for g in model.generations), [TaxonomyWarning.GENERATION_YEAR_UNKNOWN]
        containing = [g for g in model.generations if g.contains(year)]
        if (
            len(containing) == 1
            and containing[0].distance_to_boundary(year) > GENERATION_BOUNDARY_MARGIN_YEARS
        ):
            return containing[0], (containing[0].code,), []
        candidates = tuple(
            g.code for g in model.generations if g.near(year, GENERATION_BOUNDARY_MARGIN_YEARS)
        )
        if not candidates:
            return None, (), [TaxonomyWarning.GENERATION_OUT_OF_RANGE]
        return None, candidates, [TaxonomyWarning.GENERATION_AMBIGUOUS]

    # -- public --------------------------------------------------------------------------------

    def match_vehicle(
        self,
        make: str | None,
        model: str | None,
        title: str | None,
        first_registration_year: int | None,
    ) -> TaxonomyMatch:
        """Match make/model (falling back to the title only when a field is missing)."""
        warnings: list[str] = []
        if self.verification != "owner_verified":
            warnings.append(TaxonomyWarning.TAXONOMY_UNVERIFIED)
        make_tokens = name_tokens(make)
        model_tokens = name_tokens(model)
        title_tokens = name_tokens(title)
        via: Literal["fields", "title", "none"] = "fields"

        make_entry: MakeEntry | None = None
        implied_prefix: Tokens = ()
        if make_tokens:
            resolved = self._resolve_make(make_tokens)
            if resolved is None:
                return self._result([*warnings, TaxonomyWarning.MAKE_UNKNOWN])
            make_entry, implied_prefix, rest = resolved
            if rest and not model_tokens:
                model_tokens = rest  # e.g. make field "VW Tiguan"
        else:
            warnings.append(TaxonomyWarning.MAKE_MISSING)
            via = "title"
            found = {
                (entry.canonical, prefix)
                for alias, entry, prefix in self._makes
                if alias and _find(title_tokens, alias) >= 0
            }
            makes_found = {name for name, _ in found}
            if len(makes_found) != 1:
                if len(makes_found) > 1:
                    warnings.append(TaxonomyWarning.TITLE_AMBIGUOUS)
                return self._result(warnings)
            name = next(iter(makes_found))
            make_entry = next(m for m in self.document.makes if m.canonical == name)

        assert make_entry is not None
        model_entry: ModelEntry | None = None
        confidence: Confidence
        if model_tokens:
            model_tokens = self._strip_make(make_entry, model_tokens)
            candidates = [model_tokens]
            if implied_prefix and model_tokens[: len(implied_prefix)] != implied_prefix:
                candidates.insert(0, implied_prefix + model_tokens)
            for tokens in candidates:
                model_entry = self._model_prefix(make_entry, tokens)
                if model_entry is not None:
                    break
            confidence = Confidence.HIGH if via == "fields" else Confidence.LOW
            if model_entry is None:
                model_entry, ambiguous = self._model_contained(make_entry, model_tokens)
                if ambiguous:
                    warnings.append(TaxonomyWarning.TITLE_AMBIGUOUS)
                confidence = Confidence.MEDIUM if via == "fields" else Confidence.LOW
        else:
            warnings.append(TaxonomyWarning.MODEL_MISSING)
            if via == "fields":
                via = "title"
            search = title_tokens
            if implied_prefix and _find(search, implied_prefix) < 0:
                search = implied_prefix + search
            model_entry, ambiguous = self._model_contained(make_entry, search)
            if ambiguous:
                warnings.append(TaxonomyWarning.TITLE_AMBIGUOUS)
            confidence = Confidence.LOW
        if via == "title":
            warnings.append(TaxonomyWarning.TITLE_FALLBACK)

        if model_entry is None:
            if TaxonomyWarning.TITLE_AMBIGUOUS not in warnings:
                warnings.append(TaxonomyWarning.MODEL_UNKNOWN)
            return self._result(warnings, make=make_entry.canonical, via=via)

        generation, candidates_codes, gen_warnings = self._generation(model_entry, first_registration_year)
        warnings.extend(gen_warnings)
        return TaxonomyMatch(
            make=make_entry.canonical,
            model=model_entry.canonical,
            vehicle_class=model_entry.vehicle_class,
            is_suv=model_entry.is_suv,
            body_type=CLASS_BODY_TYPES[model_entry.vehicle_class],
            exclusion_reason=model_entry.exclusion_reason,
            generation=generation.code if generation else None,
            generation_label=generation.label if generation else None,
            generation_candidates=candidates_codes,
            confidence=confidence,
            matched_via=via,
            warnings=tuple(dict.fromkeys(warnings)),
            taxonomy_version=self.version,
            verification=self.verification,
        )

    def _result(
        self,
        warnings: list[str],
        *,
        make: str | None = None,
        via: Literal["fields", "title", "none"] = "none",
    ) -> TaxonomyMatch:
        return TaxonomyMatch(
            make=make,
            matched_via=via if make else "none",
            warnings=tuple(dict.fromkeys(warnings)),
            taxonomy_version=self.version,
            verification=self.verification,
        )


def parse_taxonomy(data: object) -> VehicleTaxonomy:
    """Validate an already-loaded YAML mapping into a ``VehicleTaxonomy``."""
    try:
        document = TaxonomyDocument.model_validate(data)
    except ValueError as exc:
        raise ValidationFailed(f"invalid vehicle taxonomy: {exc}") from exc
    return VehicleTaxonomy(document)


def load_taxonomy(path: Path | None = None) -> VehicleTaxonomy:
    """Load and validate ``config/vehicle_taxonomy.yaml`` (the only I/O in this module)."""
    target = path or DEFAULT_TAXONOMY_PATH
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationFailed(f"missing vehicle taxonomy file {target.name}") from exc
    except yaml.YAMLError as exc:
        raise ValidationFailed("vehicle taxonomy is not valid YAML") from exc
    return parse_taxonomy(data)


@lru_cache(maxsize=1)
def default_taxonomy() -> VehicleTaxonomy:
    """The repository taxonomy, loaded once per process."""
    return load_taxonomy(DEFAULT_TAXONOMY_PATH)


def match_vehicle(
    make: str | None,
    model: str | None,
    title: str | None,
    first_registration_year: int | None,
    *,
    taxonomy: VehicleTaxonomy | None = None,
) -> TaxonomyMatch:
    """Convenience wrapper over ``VehicleTaxonomy.match_vehicle`` using the default taxonomy."""
    return (taxonomy or default_taxonomy()).match_vehicle(make, model, title, first_registration_year)
