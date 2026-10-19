"""Swiss marketplace candidates (CarForYou, tutti.ch, comparis.ch): unimplemented placeholders.

Spec section 5: Swiss sources are verified separately from other markets. The candidate
domains recorded in the ``notes`` of ``config/sources/*.yaml`` are UNVERIFIED; nothing about
these sites (operator, hosts, routes, selectors, listing IDs, terms, robots directives) has been
verified, so none of it is encoded here. comparis.ch is a comparison site/aggregator: it may
republish listings of third parties, so its own terms and robots directives, the original
source's terms and the source attribution must all be checked before any use.
"""

from __future__ import annotations

from typing import ClassVar

from suv_deals.adapters._placeholder import PlaceholderAdapter


class CarForYouChPublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "carforyou_ch_public"
    DISPLAY_NAME: ClassVar[str] = "CarForYou (CH) public listings"
    COUNTRY: ClassVar[str | None] = "CH"


class TuttiChPublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "tutti_ch_public"
    DISPLAY_NAME: ClassVar[str] = "tutti.ch (CH) public classifieds"
    COUNTRY: ClassVar[str | None] = "CH"


class ComparisChPublicAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "comparis_ch_public"
    DISPLAY_NAME: ClassVar[str] = "comparis.ch (CH) car listings (aggregator)"
    COUNTRY: ClassVar[str | None] = "CH"


CH_MARKETPLACE_ADAPTERS: tuple[type[PlaceholderAdapter], ...] = (
    CarForYouChPublicAdapter,
    TuttiChPublicAdapter,
    ComparisChPublicAdapter,
)
