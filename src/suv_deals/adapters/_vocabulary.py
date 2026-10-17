"""Small exact-word maps for localised vehicle labels (Italian and German marketplace wording).

Marketplaces publish drive, gearbox and fuel as their own UI labels ("Anteriore", "Schaltgetriebe",
"Elettrica/Benzina") instead of schema.org enumeration members. These maps hold only labels seen on
saved pages or in the sites' own filter vocabularies (AutoScout24 Italy and Germany captures,
2026-10-15/16); anything else stays unknown here and falls through to the callers' wording rules.
A lookup is an exact match after case folding and whitespace collapsing, never a substring guess.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Final

from suv_deals.domain.enums import Drive, Fuel, Gearbox

_WS = re.compile(r"\s+")

#: "4x4" is all-wheel drive unless the wording says selectable/part-time (callers' rules decide that).
DRIVE_WORDS: Final = MappingProxyType(
    {
        # Italian
        "anteriore": Drive.FWD,
        "trazione anteriore": Drive.FWD,
        "posteriore": Drive.RWD,
        "trazione posteriore": Drive.RWD,
        "integrale": Drive.AWD,
        "trazione integrale": Drive.AWD,
        "4x4": Drive.AWD,
        # German
        "front": Drive.FWD,
        "frontantrieb": Drive.FWD,
        "vorderradantrieb": Drive.FWD,
        "heck": Drive.RWD,
        "heckantrieb": Drive.RWD,
        "hinterradantrieb": Drive.RWD,
        "allrad": Drive.AWD,
        "allradantrieb": Drive.AWD,
    }
)

GEARBOX_WORDS: Final = MappingProxyType(
    {
        # Italian
        "manuale": Gearbox.MANUAL,
        "automatico": Gearbox.AUTOMATIC,
        "semiautomatico": Gearbox.SEMI_AUTOMATIC,
        # German
        "schaltgetriebe": Gearbox.MANUAL,
        "automatik": Gearbox.AUTOMATIC,
        "halbautomatik": Gearbox.SEMI_AUTOMATIC,
        # Language-neutral raw values (AutoScout24 ``transmissionType.raw``)
        "manual": Gearbox.MANUAL,
        "automatic": Gearbox.AUTOMATIC,
        "semiautomatic": Gearbox.SEMI_AUTOMATIC,
        "semi-automatic": Gearbox.SEMI_AUTOMATIC,
    }
)

#: Fuel labels. "Elettrica/Benzina" / "Elektro/Benzin" cover full AND plug-in hybrids on
#: AutoScout24 cards; callers that know more (a plug-in flag) refine it.
FUEL_WORDS: Final = MappingProxyType(
    {
        # Italian
        "benzina": Fuel.PETROL,
        "diesel": Fuel.DIESEL,
        "gpl": Fuel.LPG,
        "metano": Fuel.CNG,
        "elettrica": Fuel.ELECTRIC,
        "elettrica/benzina": Fuel.HYBRID_PETROL,
        "elettrica/diesel": Fuel.HYBRID_DIESEL,
        "idrogeno": Fuel.OTHER,
        "etanolo": Fuel.OTHER,
        "altro": Fuel.OTHER,
        # German
        "benzin": Fuel.PETROL,
        "autogas (lpg)": Fuel.LPG,
        "erdgas (cng)": Fuel.CNG,
        "elektro": Fuel.ELECTRIC,
        "elektro/benzin": Fuel.HYBRID_PETROL,
        "elektro/diesel": Fuel.HYBRID_DIESEL,
        "wasserstoff": Fuel.OTHER,
        "ethanol": Fuel.OTHER,
        "sonstige": Fuel.OTHER,
    }
)


def word_key(text: str) -> str:
    """Lookup key: case-folded, whitespace collapsed, surrounding blanks removed."""
    return _WS.sub(" ", text).strip().casefold()


def drive_word(text: str | None) -> Drive | None:
    return None if not text else DRIVE_WORDS.get(word_key(text))


def gearbox_word(text: str | None) -> Gearbox | None:
    return None if not text else GEARBOX_WORDS.get(word_key(text))


def fuel_word(text: str | None) -> Fuel | None:
    return None if not text else FUEL_WORDS.get(word_key(text))
