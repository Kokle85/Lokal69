"""Detail job handler for detail, recheck and stale-detail sweep jobs (spec 8, 9, 10, 13, 25, 30).

One handler serves every listing refresh purpose; the job payload's ``reason`` says why it exists
(``new_listing``, ``card_changed``, ``stale_detail_sweep``, an MCP/dashboard recheck, a retry). The
job is bound to the listing incarnation and a pre-allocated detail generation (never to a semantic
hash), so A -> B -> A changes and late completions are handled by `listings_repo.ingest_detail`.

Phases (no transaction is open during network I/O):

1. **Preflight** (short read): the listing and its source; a ``card_only`` source never fetches detail
   pages (the job completes as skipped); otherwise the source must allow network work
   (`lock_source(for_network=True)`, activation gate) and ``SOURCE_NETWORK_ENABLED`` must be true for
   a real source (`RuntimeContext.crawl_session`).
2. **Network**: ``adapter.fetch_detail`` through the policy-enforcing client and the persistent budget
   gate, then ``adapter.parse_detail``. The optional raw snapshot is written to the snapshot store
   (disabled by default) before the commit.
3. **Commit** (job locked first; `LeaseLost` rolls everything back): the redacted fetch outcome and the
   snapshot metadata are recorded, then

   - an explicit listing page, a removed page or a not-found page goes to `ingest_detail` (detail
     observation, semantic revision on change, field evidence, screening, valuation job, guarded
     completion in the same transaction);
   - an access block (401/403/CAPTCHA/login) pauses the source route (`record_access_block`) and
     blocks the job without retry;
   - 429 and transient failures release the job at the budget gate's Retry-After-respecting time;
   - our own URL-policy refusal and unparseable/unexpected pages are dead letters that stay visible
     (retrying cannot help; parser-health metrics record them).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from suv_deals.adapters.base import ParsedListing, RawDocument
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import AccessState, JobState
from suv_deals.errors import SourcePaused, ValidationFailed
from suv_deals.observability.logging import log_context
from suv_deals.persistence import listings_repo, sources_repo, storage
from suv_deals.persistence.database import Conn
from suv_deals.persistence.listings_repo import DetailSnapshotRef, IngestDetailResult, ListingRecord
from suv_deals.persistence.sources_repo import SourceRecord, SourceRoute
from suv_deals.persistence.storage import StoredObject
from suv_deals.persistence.transactions import job_unit_of_work, lock_source, retry_transient, unit_of_work
from suv_deals.workers.runtime import (
    CrawlSession,
    Disposition,
    JobExecution,
    JobOutcome,
    RuntimeContext,
    apply_disposition,
    backoff_delay,
    call_with_budget,
)

logger = logging.getLogger(__name__)

CARD_ONLY_SKIP: Final = "card_only_source"
_INGESTIBLE: Final = frozenset({AccessState.OK, AccessState.REMOVED, AccessState.NOT_FOUND})


class DetailPayload(BaseModel):
    """The payload `listings_repo` writes for detail/recheck jobs (unknown keys are ignored)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    listing_id: UUID | None = None
    reason: str | None = None


def _ingestible(parsed: ParsedListing) -> bool:
    if parsed.page_type == "removed" or parsed.access_state in (AccessState.REMOVED, AccessState.NOT_FOUND):
        return True
    return (
        parsed.access_state == AccessState.OK and parsed.page_type == "detail" and parsed.listing is not None
    )


def _route(document: RawDocument) -> SourceRoute:
    parts = urlsplit(document.fetch.requested_url)
    return SourceRoute(host=parts.hostname or "unknown.invalid", purpose="detail", path=parts.path or None)


def _retry_at(
    session: CrawlSession, source: SourceRecord, document: RawDocument, attempt: int
) -> datetime | timedelta:
    host = urlsplit(document.fetch.requested_url).hostname
    result = session.gate.last_result(source.source_key, host) if host else None
    if result is not None and result.plan.retry_at is not None:
        return result.plan.retry_at
    if document.fetch.retry_after_seconds is not None:
        return timedelta(seconds=document.fetch.retry_after_seconds)
    return backoff_delay(attempt)


def classify(
    session: CrawlSession, source: SourceRecord, document: RawDocument, parsed: ParsedListing, attempt: int
) -> Disposition | None:
    """``None`` = ingest the parse; otherwise how the job ends without a detail observation."""
    states = {document.fetch.access_state, parsed.access_state}
    if AccessState.ACCESS_BLOCKED in states:
        return Disposition.blocked("access_blocked", (parsed.errors[0] if parsed.errors else None))
    if AccessState.RATE_LIMITED in states:
        return Disposition.retry("RATE_LIMITED", _retry_at(session, source, document, attempt))
    if AccessState.TRANSIENT_ERROR in states:
        code = document.fetch.error_code or "TRANSIENT_ERROR"
        return Disposition.retry(code, _retry_at(session, source, document, attempt))
    if _ingestible(parsed):
        return None
    if AccessState.POLICY_DENIED in states:
        return Disposition.dead("policy_denied", document.fetch.error_code)
    detail = "; ".join(parsed.errors[:3]) or parsed.access_state.value
    return Disposition.dead("detail_parse_failed", detail)


async def handle_detail(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    job = execution.job
    actor = execution.actor
    payload = DetailPayload.model_validate(job.payload)
    listing_id = job.listing_id or payload.listing_id
    if listing_id is None or job.generation is None:
        raise ValidationFailed("a detail job needs its listing and generation")
    listing, source = await _preflight(ctx, actor, listing_id)
    if source.detail_mode == "card_only":
        return await _finish(ctx, execution, Disposition.complete({"skipped": CARD_ONLY_SKIP}))
    if source.activation_problems():
        raise SourcePaused(f"source {source.source_key} is gated")
    session = await ctx.open_crawl_session(job.workspace_id, source)
    adapter = session.adapter
    with log_context(source_key=source.source_key, adapter_version=adapter.adapter_version):
        identity = adapter.canonicalize(listing.canonical_url)
        document = await call_with_budget(
            ctx, execution, partial(adapter.fetch_detail, identity, session.client)
        )
        parsed = adapter.parse_detail(document)
        ctx.metrics.record_fetch(
            source.source_key,
            page_type="detail",
            outcome=document.fetch.access_state,
            response_bytes=document.fetch.bytes,
        )
        disposition = classify(session, source, document, parsed, job.attempts)
        if disposition is not None and disposition.code == "detail_parse_failed":
            ctx.metrics.record_parser_failure(source.source_key, page_type="detail")
        stored = await _store_snapshot(ctx, job.workspace_id, document) if disposition is None else None
        observation_id = uuid4()  # fixed per fetch: a re-run commit is idempotent

        async def commit() -> tuple[Disposition | None, JobState, IngestDetailResult | None]:
            execution.check_lease()
            async with job_unit_of_work(ctx.db, job) as (conn, _locked):
                if disposition is not None and disposition.code == "access_blocked":
                    await sources_repo.record_access_block(
                        conn,
                        actor,
                        source.id,
                        _route(document),
                        {
                            "http_status": document.fetch.http_status,
                            "error_code": document.fetch.error_code,
                            "page_type": parsed.page_type,
                        },
                    )
                snapshot_id = await _record_snapshot(conn, actor, source, document, stored)
                fetch_id = await _record_fetch(
                    conn, actor, source, document, job_id=job.id, snapshot_id=snapshot_id
                )
                if disposition is not None:
                    # The applied state, not the requested kind: an exhausted retry is a dead letter.
                    return disposition, await apply_disposition(conn, job, disposition), None
                context = (
                    await listings_repo.load_screening_context(conn, actor, taxonomy=ctx.taxonomy)
                    if ctx.taxonomy is not None
                    else None
                )
                result = await listings_repo.ingest_detail(
                    conn,
                    actor,
                    job,
                    listing.id,
                    parsed,
                    DetailSnapshotRef(
                        parser_version=adapter.adapter_version,
                        observation_id=observation_id,
                        snapshot_id=snapshot_id,
                        fetch_attempt_id=fetch_id,
                        raw_content_hash=document.raw_content_hash,
                        crawler_version=document.fetch.crawler_version,
                        observed_at=document.fetched_at,
                    ),
                    screening_context=context,
                    complete_job=True,
                )
                return None, JobState.SUCCEEDED, result

        applied, state, result = await retry_transient(commit)
    if applied is not None:
        return JobOutcome(state=state, code=applied.code, details={"listing_id": str(listing.id)})
    assert result is not None
    return JobOutcome(
        state=JobState.SUCCEEDED,
        details={
            "listing_id": str(result.listing_id),
            "outcome": str(result.outcome),
            "revision_id": None if result.revision_id is None else str(result.revision_id),
            "eligibility": None if result.eligibility_state is None else result.eligibility_state.value,
            "valuation_job_id": None if result.valuation_job_id is None else str(result.valuation_job_id),
        },
    )


async def _preflight(
    ctx: RuntimeContext, actor: ActorContext, listing_id: UUID
) -> tuple[ListingRecord, SourceRecord]:
    async with unit_of_work(ctx.db, actor) as conn:
        listing = await listings_repo.get_listing(conn, actor, listing_id)
        source = await sources_repo.get_source_record(conn, actor, listing.source_id)
        if source.detail_mode != "card_only":
            await lock_source(conn, actor.workspace_id, source.id, for_network=True)
    return listing, source


async def _finish(ctx: RuntimeContext, execution: JobExecution, disposition: Disposition) -> JobOutcome:
    async def commit() -> JobState:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            return await apply_disposition(conn, execution.job, disposition)

    state = await retry_transient(commit)
    return JobOutcome(state=state, code=disposition.code, details=dict(disposition.result or {}))


async def _store_snapshot(
    ctx: RuntimeContext, workspace_id: UUID, document: RawDocument
) -> StoredObject | None:
    """Write the raw page to the snapshot store when retention is enabled (outside any transaction)."""
    store = ctx.snapshot_store
    if store is None or store.backend == "disabled" or not document.html:
        return None
    return await store.put(
        workspace_id=workspace_id, content=document.html.encode("utf-8"), mime_type=document.content_type
    )


async def _record_snapshot(
    conn: Conn, actor: ActorContext, source: SourceRecord, document: RawDocument, stored: StoredObject | None
) -> UUID | None:
    if stored is None:
        return None
    record = await storage.record_snapshot(
        conn,
        actor,
        source_id=source.id,
        url=document.final_url or document.url,
        stored=stored,
        fetched_at=document.fetched_at,
    )
    return record.id


async def _record_fetch(
    conn: Conn,
    actor: ActorContext,
    source: SourceRecord,
    document: RawDocument,
    *,
    job_id: UUID,
    snapshot_id: UUID | None,
) -> UUID | None:
    try:
        return await sources_repo.record_fetch_attempt(
            conn,
            actor,
            source_id=source.id,
            purpose="detail",
            outcome=document.fetch,
            job_id=job_id,
            snapshot_id=snapshot_id,
        )
    except ValidationFailed:
        logger.info("fetch outcome without a host was not recorded")
        return None


__all__ = ["CARD_ONLY_SKIP", "DetailPayload", "classify", "handle_detail"]
