"""mobile.de public listings (DE): unimplemented placeholder.

Status (spec section 5): disabled pending a documented source decision and a
successful live smoke. mobile.de's public terms contain a scraping restriction in
section 11 (https://www.mobile.de/service/agbPublic, checked 2026-10-06). No search
URL, selector or listing-ID pattern is verified, so none is encoded here.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class MobileDePublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "mobile_de_public"
    DISPLAY_NAME: ClassVar[str] = "mobile.de public listings (DE)"
    COUNTRY: ClassVar[str | None] = "DE"
