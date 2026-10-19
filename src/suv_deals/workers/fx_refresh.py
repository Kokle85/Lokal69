"""ECB reference-rate refresh and owner-entered FX rates (spec 18 "FX rules"; OPS-06, wave D2).

Before this module ``integrations.fx.fetch_ecb_daily`` and ``valuation_repo.upsert_fx_rate`` had
no caller: ``FX_FETCH_ENABLED`` / ``SUV_DEALS_ENABLE_FX_FETCH`` were inert and no command could
store a rate, so every CH listing screened ``needs_facts`` (``fx:CHF/EUR``) forever.

- `refresh_ecb_rates`: ONE gated fetch of the ECB daily file (``FX_FETCH_ENABLED=true``, HTTPS
  on the allow-listed ECB host only; ``fetch_ecb_daily`` refuses otherwise), then the
  ``EUR -> CHF`` reference rate (`REFRESH_CURRENCIES`) is recorded in every active workspace.
  Recording is idempotent (the same date/provider/purpose is stored once; a DIFFERENT value for an
  already stored observation is a conflict, never an overwrite). The reconciler calls it at most
  every `REFRESH_EVERY` (`FxRefresher`); ``suv-deals fx refresh`` runs it once.
- `record_rates`: stores observations and audits each new row (``fx_rate.record``: pair, date,
  provider, purpose; never more). ``suv-deals fx record`` uses it for an owner-entered rate
  (``provider=owner`` by default), e.g. during the "stale tax/FX rules" incident.

Reference rates support estimation only (spec 18); payment and customs rates stay separate
purposes. The ECB publishes no MKD rate and nothing here invents one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import httpx

from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.money import FxRate
from suv_deals.errors import AppError, DependencyUnavailable
from suv_deals.integrations.fx import ECB_PROVIDER, EcbFetchResult, FxHttpClient, fetch_ecb_daily
from suv_deals.integrations.safe_http import SafeHttpClient, SafeHttpError
from suv_deals.persistence import audit, valuation_repo
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings

logger = logging.getLogger(__name__)

#: Quote currencies recorded from the ECB file (1 EUR = x CCY): the CH market only. DE/IT are EUR.
REFRESH_CURRENCIES: Final = frozenset({"CHF"})
#: The ECB publishes once per working day; the reconciler re-fetches at most this often ...
REFRESH_EVERY: Final = timedelta(hours=6)
#: ... and waits this long after a failed fetch.
RETRY_AFTER_FAILURE: Final = timedelta(hours=1)
FX_AUDIT_ACTION: Final = "fx_rate.record"


@dataclass(frozen=True, slots=True)
class FxRefreshResult:
    """One refresh: the fetched file's provenance and the new rows per workspace."""

    rate_dates: tuple[str, ...]
    sha256: str
    recorded: dict[UUID, int] = field(default_factory=dict)


def ecb_source_ref(result: EcbFetchResult) -> str:
    return f"{result.source_url}#sha256={result.sha256}"[:500]


async def record_rates(
    conn: Conn,
    actor: ActorContext,
    rates: Iterable[FxRate],
    *,
    source_ref: str | None,
    reason: str | None = None,
) -> int:
    """Store each observation once and audit every NEW row; returns how many were new."""
    created = 0
    for rate in rates:
        stored, new = await valuation_repo.upsert_fx_rate(conn, actor, rate, source_ref=source_ref)
        if not new:
            continue
        created += 1
        await audit.record(
            conn,
            actor,
            FX_AUDIT_ACTION,
            "fx_rate",
            stored.id,
            reason=reason,
            metadata={
                "base": rate.base,
                "quote": rate.quote,
                "rate": str(stored.rate.rate),
                "rate_date": rate.rate_date.isoformat(),
                "provider": rate.provider,
                "purpose": rate.purpose.value,
            },
        )
    return created


def default_fx_client() -> FxHttpClient:
    """The netguard-enforcing HTTPS client (no proxy; public ECB host only)."""
    return SafeHttpClient()


async def refresh_ecb_rates(
    settings: Settings,
    db: Database,
    client: FxHttpClient,
    workspace_ids: Sequence[UUID],
    *,
    clock: Clock | None = None,
) -> FxRefreshResult:
    """Fetch the ECB daily file once (gated) and record its CHF rate in each workspace.

    Refuses (``DEPENDENCY_UNAVAILABLE``) when ``FX_FETCH_ENABLED`` is false, without any network
    call. A transport failure is ``DEPENDENCY_UNAVAILABLE`` too (never a traceback).
    """
    try:
        fetched = await fetch_ecb_daily(settings, client, clock=clock)
    except (SafeHttpError, httpx.HTTPError, OSError) as exc:
        raise DependencyUnavailable("ECB reference rates could not be fetched") from exc
    rates = [r for r in fetched.rates if r.base == "EUR" and r.quote in REFRESH_CURRENCIES]
    result = FxRefreshResult(
        rate_dates=tuple(d.isoformat() for d in fetched.rate_dates), sha256=fetched.sha256
    )
    for workspace_id in workspace_ids:
        actor = ActorContext.system(workspace_id, request_id=f"fx-refresh:{uuid4().hex[:12]}")
        async with unit_of_work(db, actor) as conn:
            result.recorded[workspace_id] = await record_rates(
                conn, actor, rates, source_ref=ecb_source_ref(fetched), reason="ECB daily reference rates"
            )
    return result


class FxRefresher:
    """The reconciler's bounded schedule for `refresh_ecb_rates` (in-process memory only).

    Does nothing while ``FX_FETCH_ENABLED`` is false (no client is ever built or called). With
    the switch on it fetches at most every `REFRESH_EVERY`, and after a failure waits
    `RETRY_AFTER_FAILURE`; a failure is logged and reported, never raised into the loop.
    """

    def __init__(self, client: FxHttpClient | None = None, *, clock: Clock | None = None) -> None:
        self._client = client
        self._clock = clock or SystemClock()
        self._next_attempt: datetime | None = None
        self.last_error: str | None = None

    def due(self, settings: Settings) -> bool:
        if not settings.fx_fetch_enabled:
            return False
        return self._next_attempt is None or self._clock.now() >= self._next_attempt

    async def run(
        self, settings: Settings, db: Database, workspace_ids: Sequence[UUID]
    ) -> FxRefreshResult | None:
        if not self.due(settings) or not workspace_ids:
            return None
        if self._client is None:
            self._client = default_fx_client()
        try:
            result = await refresh_ecb_rates(settings, db, self._client, workspace_ids, clock=self._clock)
        except AppError as exc:
            self.last_error = exc.code.value
            self._next_attempt = self._clock.now() + RETRY_AFTER_FAILURE
            logger.warning("ECB FX refresh failed", extra={"error_code": exc.code.value})
            return None
        self.last_error = None
        self._next_attempt = self._clock.now() + REFRESH_EVERY
        logger.info(
            "ECB FX refresh finished",
            extra={"rate_dates": list(result.rate_dates), "recorded": sum(result.recorded.values())},
        )
        return result


__all__ = [
    "ECB_PROVIDER",
    "FX_AUDIT_ACTION",
    "REFRESH_CURRENCIES",
    "REFRESH_EVERY",
    "RETRY_AFTER_FAILURE",
    "FxRefreshResult",
    "FxRefresher",
    "default_fx_client",
    "ecb_source_ref",
    "record_rates",
    "refresh_ecb_rates",
]
