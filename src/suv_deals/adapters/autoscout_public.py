"""AutoScout24 country-specific public listings: unimplemented placeholders.

One class parameterised per country; each country is registered and gated
separately (spec section 5: "verify CH separately from other AutoScout24 markets").
The consumer terms at https://www.autoscout24.com/company/agb/ (sections 8.2-8.3,
checked 2026-10-06) restrict automated queries and the creation/commercial use of a
derived database; which country-specific and business-use terms actually apply must
be determined per country. No route, selector or ID pattern is encoded.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class AutoScout24PublicAdapter(PlaceholderAdapter):
    """Country-parameterised base. Use the per-country subclasses."""

    DISPLAY_NAME: ClassVar[str] = "AutoScout24 public listings"


class AutoScout24DePublicAdapter(AutoScout24PublicAdapter):
    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_de"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Germany public listings"
    COUNTRY: ClassVar[str | None] = "DE"


class AutoScout24ItPublicAdapter(AutoScout24PublicAdapter):
    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_it"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Italy public listings"
    COUNTRY: ClassVar[str | None] = "IT"


class AutoScout24ChPublicAdapter(AutoScout24PublicAdapter):
    ADAPTER_KEY: ClassVar[str] = "autoscout24_public_ch"
    DISPLAY_NAME: ClassVar[str] = "AutoScout24 Switzerland public listings"
    COUNTRY: ClassVar[str | None] = "CH"


AUTOSCOUT24_ADAPTERS: tuple[type[AutoScout24PublicAdapter], ...] = (
    AutoScout24DePublicAdapter,
    AutoScout24ItPublicAdapter,
    AutoScout24ChPublicAdapter,
)
