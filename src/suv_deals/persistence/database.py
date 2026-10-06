"""Async PostgreSQL access (psycopg 3) with workspace-scoped transactions.

Every transaction sets the transaction-local GUCs `app.workspace_id` and
`app.user_id` from the validated ActorContext (ADR 0001) plus short statement
and lock timeouts. No network I/O may happen while a transaction is open.

Pooling: persistent workers should use a direct/session connection. When a
transaction pooler is used, prepared statements are disabled
(`prepare_threshold=None`) and no session state (advisory locks, SET without
LOCAL) is relied on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from suv_deals.domain.actor import ActorContext
from suv_deals.errors import DependencyUnavailable

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Conn = AsyncConnection[DictRow]


class Database:
    def __init__(
        self,
        url: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        set_role: str | None = None,
        application_name: str = "suv-deals",
        statement_timeout_ms: int = 15_000,
        lock_timeout_ms: int = 3_000,
    ) -> None:
        if set_role is not None and not _ROLE_NAME.match(set_role):
            raise ValueError("invalid role name for SET ROLE")
        self._set_role = set_role
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._pool: AsyncConnectionPool[Conn] = AsyncConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "prepare_threshold": None,
                "application_name": application_name,
            },
            configure=self._configure,
            check=AsyncConnectionPool.check_connection,
            name=application_name,
        )

    async def _configure(self, conn: Conn) -> None:
        if self._set_role:
            await conn.execute(sql.SQL("set role {}").format(sql.Identifier(self._set_role)))

    async def open(self, wait: bool = True, open_timeout_s: float = 10.0) -> None:
        try:
            await self._pool.open(wait=wait, timeout=open_timeout_s)
        except psycopg.OperationalError as exc:
            raise DependencyUnavailable("database unavailable") from exc

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> Database:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @asynccontextmanager
    async def transaction(
        self,
        actor: ActorContext | None = None,
        *,
        workspace_id: UUID | None = None,
        user_id: UUID | None = None,
        statement_timeout_ms: int | None = None,
    ) -> AsyncIterator[Conn]:
        """Open a short transaction scoped to one workspace.

        `actor` supplies the workspace (and user, for human principals). System
        code that has not yet selected a workspace may pass neither; RLS then
        exposes no workspace rows to `suv_backend`.
        """
        ws = actor.workspace_id if actor is not None else workspace_id
        uid = user_id
        if uid is None and actor is not None and actor.principal_kind == "user":
            uid = actor.principal_id
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "select set_config('app.workspace_id', %s, true),"
                    " set_config('app.user_id', %s, true),"
                    " set_config('statement_timeout', %s, true),"
                    " set_config('lock_timeout', %s, true)",
                    (
                        str(ws) if ws else "",
                        str(uid) if uid else "",
                        str(statement_timeout_ms or self._statement_timeout_ms),
                        str(self._lock_timeout_ms),
                    ),
                )
                yield conn
        except psycopg.OperationalError as exc:
            raise DependencyUnavailable("database unavailable") from exc

    async def ping(self) -> bool:
        try:
            async with self._pool.connection() as conn:
                await conn.execute("select 1")
            return True
        except psycopg.Error:
            return False


async def fetch_one(conn: Conn, query: Any, params: Any = None) -> DictRow | None:
    cur = await conn.execute(query, params)
    return await cur.fetchone()


async def fetch_all(conn: Conn, query: Any, params: Any = None) -> list[DictRow]:
    cur = await conn.execute(query, params)
    return await cur.fetchall()


async def db_now(conn: Conn) -> Any:
    row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return row["now"]
