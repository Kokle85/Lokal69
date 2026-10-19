"""Async PostgreSQL access (psycopg 3) with workspace-scoped transactions.

Every transaction sets the transaction-local GUCs `app.workspace_id` and
`app.user_id` from the validated ActorContext (ADR 0001) plus short statement
and lock timeouts. No network I/O may happen while a transaction is open.

Pooling: persistent workers should use a direct/session connection. When a
transaction pooler is used, prepared statements are disabled
(`prepare_threshold=None`) and no session state (advisory locks, SET without
LOCAL) is relied on. ``pool_timeout_s`` bounds how long a caller waits for a pooled
connection (``DATABASE_POOL_TIMEOUT_S``, default 5 s): during a database outage requests fail
fast with ``DependencyUnavailable`` (503) instead of hanging for psycopg_pool's 30 s default.

Errors: inside the block, statement errors propagate unchanged (callers map them with
``persistence.errors_map.mapped_errors``); a connection-class failure becomes
``DependencyUnavailable``. A failure raised by COMMIT itself - a deferred constraint trigger
such as the seller-inquiry evidence check (``SV003``), a deferred foreign key or a
serialization failure - is mapped to its typed ``AppError`` here (``errors_map.map_db_error``),
so ``Database.transaction`` never leaks a raw psycopg error from COMMIT.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import psycopg_pool
from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from suv_deals.domain.actor import ActorContext
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode
from suv_deals.persistence.errors_map import map_db_error

if TYPE_CHECKING:
    from suv_deals.settings import Settings

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Conn = AsyncConnection[DictRow]


class TransactionFailed(AppError):
    """The transaction block finished while PostgreSQL had already aborted it."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, retryable=False)


def _is_connection_error(exc: psycopg.Error) -> bool:
    """Connection-class failures (SQLSTATE 08xxx, admin shutdown, or no SQLSTATE at all)."""
    state = exc.sqlstate
    return state is None or state.startswith("08") or state in {"57P01", "57P02", "57P03"}


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
        pool_timeout_s: float = 5.0,
    ) -> None:
        if set_role is not None and not _ROLE_NAME.match(set_role):
            raise ValueError("invalid role name for SET ROLE")
        if not 0 < pool_timeout_s <= 120:
            raise ValueError("pool_timeout_s must be in (0, 120] seconds")
        self._set_role = set_role
        self._pool_timeout_s = pool_timeout_s
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
            timeout=pool_timeout_s,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        application_name: str,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> Database:
        """The pool every process builds from ``Settings``: ``DATABASE_URL``, pool sizes,
        ``DATABASE_SET_ROLE`` and ``DATABASE_POOL_TIMEOUT_S``. ``ValueError`` without a URL."""
        if settings.database_url is None or not settings.database_url.get_secret_value():
            raise ValueError("DATABASE_URL is not configured")
        return cls(
            settings.database_url.get_secret_value(),
            min_size=settings.database_pool_min if min_size is None else min_size,
            max_size=settings.database_pool_max if max_size is None else max_size,
            set_role=settings.database_set_role,
            application_name=application_name,
            pool_timeout_s=settings.database_pool_timeout_s,
        )

    @property
    def pool_timeout_s(self) -> float:
        return self._pool_timeout_s

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
        committing = False
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
                if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    # PostgreSQL would turn COMMIT into a silent ROLLBACK; never hide that.
                    raise TransactionFailed("transaction aborted by an earlier error")
                committing = True  # leaving the block commits; deferred checks run now
        except psycopg_pool.PoolTimeout as exc:
            raise DependencyUnavailable("database unavailable") from exc  # no pooled connection in time
        except psycopg.OperationalError as exc:
            if _is_connection_error(exc):
                raise DependencyUnavailable("database unavailable") from exc
            if committing:
                raise map_db_error(exc) from exc
            raise  # lock/serialization/timeout errors are mapped by the caller (errors_map)
        except psycopg.Error as exc:
            if committing:  # e.g. SV003 from a deferred evidence trigger, a deferred FK
                raise map_db_error(exc) from exc
            raise

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
