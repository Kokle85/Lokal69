"""AutoScout24 Switzerland public listings: an unimplemented placeholder.

Each AutoScout24 country is registered and gated separately (spec section 5: "verify CH separately
from other AutoScout24 markets"). Italy and Germany have a real adapter in
`suv_deals.adapters.autoscout24`, built from owner-captured pages. The Swiss site answered every
HTML page with a challenge on 2026-10-14, so its terms, routes and page structure are unknown and
no route, selector or ID pattern is encoded here. The consumer terms at
https://www.autoscout24.com/company/agb/ (sections 8.2-8.3, checked 2026-10-06) restrict automated
queries and the creation/commercial use of a derived database; which country-specific and
business-use terms actually apply must be determined per country.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class AutoScout24PublicAdapter(PlaceholderAdapter):
    """Country-parameterised placeholder base (only Switzerland remains a placeholder)."""

    DISPLAY_NAME: ClassVar[str] = "AutoScout24 public listings"


class AutoScout24ChPublicAdapter(AutoScout24PublicAdapter):
    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_ch"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Switzerland public listings"
    COUNTRY: ClassVar[str | None] = "CH"


AUTOSCOUT24_PLACEHOLDERS: tuple[type[AutoScout24PublicAdapter], ...] = (AutoScout24ChPublicAdapter,)
