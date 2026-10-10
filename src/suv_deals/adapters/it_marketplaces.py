"""Italian marketplace candidates (Subito, automobile.it): unimplemented placeholders.

Spec section 5: candidate adapters; exact domains, terms and public routes must be
verified first. Nothing about these sites (hosts, routes, selectors, IDs, terms) has
been verified, so none is encoded.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class SubitoPublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "subito_it_public"
    DISPLAY_NAME: ClassVar[str] = "Subito (IT) public listings"
    COUNTRY: ClassVar[str | None] = "IT"


class AutomobileItPublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "automobile_it_public"
    DISPLAY_NAME: ClassVar[str] = "automobile.it public listings"
    COUNTRY: ClassVar[str | None] = "IT"
