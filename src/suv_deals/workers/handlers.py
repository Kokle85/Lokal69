"""Job-type -> handler registry (spec 13): the worker's only dispatch table.

Every entry declares the payload versions it understands; the worker passes them to `jobs.claim`, so
a job written by a newer producer is moved to ``blocked`` with ``incompatible_payload_version``
instead of being misread or retried forever.

The default registry also carries the spec v1.1 section 37 runtime: the bounded automatic seller
inquiry (``seller_inquiry_plan`` / ``seller_inquiry_send`` / ``seller_inquiry_reconcile``,
`workers.inquiry_handlers`) and the processing of stored seller replies (``seller_reply_process``,
`workers.reply_handlers`). None of them has an approval wait.

Extension point: later packages register additional job types without touching the runner::

    registry = default_registry()
    registry.register(JobType.REPROCESS, handle_reprocess, payload_versions=(1,))

(a new ``JobType`` value also needs the ``ops.jobs.jobs_type_ck`` migration). Replacing an existing
handler is refused unless ``replace=True`` is passed explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from suv_deals.crawling.detail import handle_detail
from suv_deals.crawling.discovery import handle_discovery
from suv_deals.crawling.valuation_pipeline import handle_valuation
from suv_deals.domain.enums import JobType
from suv_deals.errors import NotFound, ValidationFailed
from suv_deals.workers.inquiry_handlers import (
    handle_seller_inquiry_plan,
    handle_seller_inquiry_reconcile,
    handle_seller_inquiry_send,
)
from suv_deals.workers.reply_handlers import handle_seller_reply_process
from suv_deals.workers.runtime import JobHandler


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    job_type: JobType
    handler: JobHandler
    payload_versions: frozenset[int]
    description: str


class HandlerRegistry:
    """Mutable during process start-up, read-only while the worker runs."""

    def __init__(self) -> None:
        self._specs: dict[JobType, HandlerSpec] = {}

    def register(
        self,
        job_type: JobType,
        handler: JobHandler,
        *,
        payload_versions: Iterable[int] = (1,),
        description: str = "",
        replace: bool = False,
    ) -> HandlerSpec:
        job_type = JobType(job_type)
        versions = frozenset(int(v) for v in payload_versions)
        if not versions or any(v < 1 for v in versions):
            raise ValidationFailed("a handler must understand at least one positive payload version")
        if job_type in self._specs and not replace:
            raise ValidationFailed(f"a handler for {job_type.value} is already registered")
        spec = HandlerSpec(job_type, handler, versions, description)
        self._specs[job_type] = spec
        return spec

    def get(self, job_type: JobType) -> HandlerSpec:
        try:
            return self._specs[JobType(job_type)]
        except KeyError:
            raise NotFound(f"no handler is registered for {job_type}") from None

    def __contains__(self, job_type: object) -> bool:
        return job_type in self._specs

    def job_types(self) -> tuple[JobType, ...]:
        return tuple(sorted(self._specs, key=lambda t: t.value))

    def payload_versions(
        self, job_types: Iterable[JobType] | None = None
    ) -> Mapping[JobType, frozenset[int]]:
        wanted = self.job_types() if job_types is None else tuple(job_types)
        return {t: self.get(t).payload_versions for t in wanted}


def default_registry() -> HandlerRegistry:
    """Core pipeline handlers plus the spec v1.1 seller inquiry / seller reply runtime."""
    registry = HandlerRegistry()
    registry.register(JobType.DISCOVERY, handle_discovery, description="search pages -> observations")
    registry.register(JobType.DETAIL, handle_detail, description="detail page -> revision and screening")
    registry.register(JobType.RECHECK, handle_detail, description="bounded availability/price recheck")
    registry.register(JobType.VALUATION, handle_valuation, description="valuation, ranking, review case")
    registry.register(
        JobType.SELLER_INQUIRY_PLAN,
        handle_seller_inquiry_plan,
        description="inquiry readiness; bounded automatic reservation",
    )
    registry.register(
        JobType.SELLER_INQUIRY_SEND,
        handle_seller_inquiry_send,
        description="guarded dispatch: outlook_local intent or provider send",
    )
    registry.register(
        JobType.SELLER_INQUIRY_RECONCILE,
        handle_seller_inquiry_reconcile,
        description="resolve an uncertain send with positive evidence only",
    )
    registry.register(
        JobType.SELLER_REPLY_PROCESS,
        handle_seller_reply_process,
        description="seller reply: escalations, re-evaluation, owner alert",
    )
    return registry


__all__ = ["HandlerRegistry", "HandlerSpec", "default_registry"]
