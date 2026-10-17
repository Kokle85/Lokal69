"""Database side of the worker's robots.txt guard (`crawling.robots_guard`; spec sections 5, 24).

`DbRobotsStore` implements `crawling.robots_guard.RobotsStore` over ``ops.robots_revisions``:

- `latest` reads the newest stored revision of a host (any source of the workspace may have
  fetched it; robots.txt belongs to the host) and rebuilds a `robots.RobotsRevision` from it. The
  table has no availability column; the stored row determines it exactly as
  `robots.fetch_robots` wrote it: a body means ``available`` (the rules are parsed again from the
  stored body), no body with ``parse_ok`` means ``unavailable`` (4xx or too many redirects: no
  robots restriction), no body without ``parse_ok`` means ``unreachable`` (complete disallow).
- `record` stores one fetched revision (`sources_repo.record_robots_revision`: bounded body,
  SHA-256, the ``source.robots_changed`` audit for every source on the host when robots.txt
  changed; nothing is enabled or disabled automatically) and its fetch attempt
  (`sources_repo.record_fetch_attempt`, ``purpose: robots``, linked to the job and crawl run that
  made it, so ``sources smoke-evidence`` lists it) in one short transaction, never across network
  I/O.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from suv_deals.adapters.base import FetchOutcome
from suv_deals.clock import ensure_utc
from suv_deals.crawling.robots import (
    ROBOTS_MAX_BYTES,
    RobotsAvailability,
    RobotsRevision,
    parse_robots_txt,
)
from suv_deals.crawling.url_policy import ROBOTS_PATH
from suv_deals.domain.actor import ActorContext
from suv_deals.persistence import sources_repo
from suv_deals.persistence.database import Conn, Database, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.transactions import retry_transient, unit_of_work

_LATEST_SQL: Final = (
    "select fetched_at, http_status, content_hash, body, body_truncated, parse_ok"
    " from ops.robots_revisions where workspace_id = %(workspace_id)s and host = %(host)s"
    " order by fetched_at desc, id desc limit 1"
)


def revision_from_row(host: str, row: dict[str, Any]) -> RobotsRevision:
    """A stored ``ops.robots_revisions`` row as the robots module's revision (see module docstring)."""
    body = row["body"]
    if body is not None:
        availability = RobotsAvailability.AVAILABLE
    elif row["parse_ok"]:
        availability = RobotsAvailability.UNAVAILABLE
    else:
        availability = RobotsAvailability.UNREACHABLE
    parsed = parse_robots_txt(body.encode("utf-8")) if body is not None else None
    content_hash = row["content_hash"]
    return RobotsRevision(
        host=host,
        robots_url=f"https://{host}{ROBOTS_PATH}",
        fetched_at=ensure_utc(row["fetched_at"]),
        http_status=row["http_status"],
        availability=availability,
        content_hash=content_hash if isinstance(content_hash, str) and len(content_hash) == 64 else None,
        parse_ok=bool(row["parse_ok"]),
        body_truncated=bool(row["body_truncated"]) or (parsed is not None and parsed.truncated),
        groups=parsed.groups if parsed is not None else (),
        invalid_lines=parsed.invalid_lines if parsed is not None else 0,
    )


async def latest_revision(conn: Conn, actor: ActorContext, host: str) -> RobotsRevision | None:
    """The newest stored robots revision of ``host`` in the actor's workspace (None: never fetched)."""
    async with mapped_errors():
        row = await fetch_one(conn, _LATEST_SQL, {"workspace_id": actor.workspace_id, "host": host})
    return None if row is None else revision_from_row(host, row)


class DbRobotsStore:
    """`RobotsStore` of one crawl session (one job of one source in one workspace)."""

    def __init__(
        self,
        db: Database,
        actor: ActorContext,
        *,
        source_id: UUID,
        user_agent: str,
        job_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self._db = db
        self._actor = actor
        self.source_id = source_id
        self.user_agent = user_agent
        self.job_id = job_id
        #: Set once the discovery run is open (`workers.runtime.CrawlSession.bind_run`).
        self.run_id = run_id

    async def latest(self, host: str) -> RobotsRevision | None:
        async with unit_of_work(self._db, self._actor) as conn:
            return await latest_revision(conn, self._actor, host)

    async def record(self, revision: RobotsRevision, outcome: FetchOutcome) -> bool:
        body = revision.body
        if body is not None:
            body = body.encode("utf-8")[:ROBOTS_MAX_BYTES].decode("utf-8", errors="ignore")

        async def once() -> bool:
            async with unit_of_work(self._db, self._actor) as conn:
                result = await sources_repo.record_robots_revision(
                    conn,
                    self._actor,
                    host=revision.host,
                    fetched_at=revision.fetched_at,
                    http_status=revision.http_status,
                    body=body,
                    parse_ok=revision.parse_ok,
                    user_agent=self.user_agent,
                )
                await sources_repo.record_fetch_attempt(
                    conn,
                    self._actor,
                    source_id=self.source_id,
                    purpose="robots",
                    outcome=outcome,
                    job_id=self.job_id,
                    crawl_run_id=self.run_id,
                )
                return result.changed

        return await retry_transient(once)


__all__ = ["DbRobotsStore", "latest_revision", "revision_from_row"]
