"""North Macedonian comparable-source candidates (Pazar3, Reklama5): unimplemented placeholders.

Spec sections 5 and 15: these are comparable-evidence candidates (role
`mk_comparable`), never acquisition sources. Exact public routes and terms must be
verified before any implementation. Asking prices from these sources are asking
prices, not realized sales. No route, selector or ID pattern is encoded.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class MkComparableAdapter(PlaceholderAdapter):
    ROLE: ClassVar[str] = "mk_comparable"
    COUNTRY: ClassVar[str | None] = "MK"


class Pazar3Adapter(MkComparableAdapter):
    ADAPTER_KEY: ClassVar[str] = "pazar3_mk"
    DISPLAY_NAME: ClassVar[str] = "Pazar3 (MK) public listings"


class Reklama5Adapter(MkComparableAdapter):
    ADAPTER_KEY: ClassVar[str] = "reklama5_mk"
    DISPLAY_NAME: ClassVar[str] = "Reklama5 (MK) public listings"
