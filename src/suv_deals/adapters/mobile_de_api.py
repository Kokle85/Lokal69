"""Feature-flagged skeleton for the official mobile.de Search API (spec section 5).

Documentation: https://services.mobile.de/docs/search-api.html (checked 2026-10-06).
The documentation describes authentication and search/detail endpoints, but no
account, contract, price, quota or entitlement has been verified for this project,
and marketplace login credentials are not API credentials. This module therefore
encodes no endpoint paths, parameters or response fields.

Behaviour:
- Without an explicit feature flag, configured credentials AND a recorded, verified
  entitlement, every network/parsing operation raises
  `DependencyUnavailable('mobile.de Search API entitlement not verified')`.
- Even when all of that is supplied, the client is still unimplemented
  (`AdapterUnimplemented`): it must be written from the official documentation after
  entitlement is verified, with recorded fixtures and a live smoke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, NoReturn

from suv_deals.adapters._placeholder import ACTIVATION_CHECKLIST, PlaceholderAdapter
from suv_deals.adapters.base import AdapterUnimplemented
from suv_deals.domain.enums import SourceMode
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import DependencyUnavailable

DOCUMENTATION_URL = "https://services.mobile.de/docs/search-api.html"
ENTITLEMENT_ERROR = "mobile.de Search API entitlement not verified"


@dataclass(frozen=True, slots=True)
class MobileDeApiAccess:
    """Operator-supplied access facts. Secrets themselves are never stored here."""

    credentials_configured: bool = False
    entitlement_verified: bool = False
    entitlement_reference: str | None = None  # e.g. contract/agreement reference recorded by the owner


class MobileDeSearchApiAdapter(PlaceholderAdapter):
    ADAPTER_KEY: ClassVar[str] = "mobile_de_api"
    DISPLAY_NAME: ClassVar[str] = "mobile.de Search API (official)"
    COUNTRY: ClassVar[str | None] = "DE"
    SUPPORTED_MODES: ClassVar[frozenset[SourceMode]] = frozenset({SourceMode.OFFICIAL_API})

    def __init__(
        self,
        config: SourceConfig,
        *,
        feature_enabled: bool = False,
        access: MobileDeApiAccess | None = None,
    ) -> None:
        super().__init__(config)
        self._feature_enabled = feature_enabled
        self._access = access or MobileDeApiAccess()

    @property
    def access_ready(self) -> bool:
        return (
            self._feature_enabled
            and self._access.credentials_configured
            and self._access.entitlement_verified
            and bool(self._access.entitlement_reference)
        )

    def _unimplemented(self, operation: str) -> NoReturn:
        if not self.access_ready:
            raise DependencyUnavailable(ENTITLEMENT_ERROR)
        raise AdapterUnimplemented(
            f"{self.ADAPTER_KEY}.{operation}: the API client is not implemented. Implement it from "
            f"{DOCUMENTATION_URL} after entitlement is verified, then follow {ACTIVATION_CHECKLIST}."
        )
