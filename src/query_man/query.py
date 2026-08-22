from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from fastapi.encoders import jsonable_encoder
from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from query_man.errors import (
    MetadataRevisionMismatchError,
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
    SourceNotFoundError,
)
from query_man.metadata import MetadataService
from query_man.models import SourceProfile
from query_man.registry import SourceRegistry
from query_man.sql_validation import SqlValidationError, ValidatedSql, validate_sql


@dataclass(frozen=True)
class PlanSummary:
    total_cost: float
    max_rows: int
    node_count: int


class QueryExecutor(Protocol):
    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
    ) -> dict[str, object]: ...

    async def close(self) -> None: ...


class QueryService:
    def __init__(
        self,
        registry: SourceRegistry,
        metadata: MetadataService,
        executor: QueryExecutor,
    ) -> None:
        self._registry = registry
        self._metadata = metadata
        self._executor = executor

    async def query(self, source_id: str, sql: str, metadata_revision: str) -> dict[str, object]:
        source = self._registry.get(source_id)
        if source is None:
            raise SourceNotFoundError
        published = await self._metadata.get_published(source_id)
        if metadata_revision != published.revision:
            raise MetadataRevisionMismatchError
        try:
            validated = validate_sql(
                sql,
                allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
                max_sql_bytes=source.budget.max_sql_bytes,
            )
        except SqlValidationError as error:
            raise QueryRejectedError(error.code) from error
        return await self._executor.execute(source, sql, published.revision, validated)


class PostgresQueryExecutor:
    def __init__(self) -> None:
        self._pools: dict[str, AsyncConnectionPool[Any]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._pool_lock = asyncio.Lock()

    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
    ) -> dict[str, object]:
        # ponytail: process-local limit; use a distributed limiter when replicas share a source quota.
        semaphore = self._semaphores.setdefault(
            source.source_id,
            asyncio.Semaphore(source.budget.max_concurrent_queries),
        )
        queued_at = time.monotonic()
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=source.budget.query_queue_timeout_ms / 1000,
            )
        except TimeoutError as error:
            raise QueryOverloadedError from error

        queue_ms = round((time.monotonic() - queued_at) * 1000)
        try:
            pool = await self._get_pool(source)
            try:
                async with asyncio.timeout(source.budget.query_transaction_timeout_ms / 1000):
                    async with pool.connection(
                        timeout=source.budget.query_queue_timeout_ms / 1000
                    ) as connection:
                        return await self._execute_connection(
                            connection,
                            source,
                            sql,
                            metadata_revision,
                            validated,
                            queue_ms,
                        )
            except PoolTimeout as error:
                raise QueryOverloadedError from error
            except TimeoutError as error:
                raise QueryTimeoutError from error
            except errors.QueryCanceled as error:
                raise QueryTimeoutError from error
            except (QueryRejectedError, QueryOverloadedError, QueryTimeoutError):
                raise
            except Exception as error:
                raise QueryUnavailableError from error
        finally:
            semaphore.release()

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    async def _execute_connection(
        self,
        connection: AsyncConnection[dict[str, Any]],
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        queue_ms: int,
    ) -> dict[str, object]:
        query_id = str(uuid.uuid4())
        started_at = time.monotonic()
        try:
            await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await connection.execute(
                "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
                "pg_catalog.set_config('transaction_timeout', %s, true), "
                "pg_catalog.set_config('lock_timeout', %s, true), "
                "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, true)",
                (
                    f"{source.budget.query_statement_timeout_ms}ms",
                    f"{source.budget.query_transaction_timeout_ms}ms",
                    f"{source.budget.lock_timeout_ms}ms",
                    f"{source.budget.query_transaction_timeout_ms}ms",
                ),
            )
            identity_cursor = await connection.execute(
                "SELECT pg_catalog.current_database() = %s AS database_matches, "
                "session_user = %s AS user_matches, "
                "pg_catalog.current_setting('transaction_read_only') = 'on' AS read_only",
                (source.connection.database, source.connection.user),
            )
            identity = await identity_cursor.fetchone()
            if not identity or not all(identity.values()):
                raise RuntimeError("Source session identity or read-only policy mismatch")

            plan_cursor = await connection.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            plan_row = await plan_cursor.fetchone()
            plan = _summarize_plan(_extract_plan(plan_row))
            _admit_plan(source, plan)

            rows: list[object] = []
            result_bytes = 2  # JSON array brackets.
            truncated = False
            cursor = connection.cursor(name=f"qm_{query_id.replace('-', '')}", row_factory=dict_row)
            try:
                await cursor.execute(sql)
                columns = [column.name for column in cursor.description or ()]
                while True:
                    row = await cursor.fetchone()
                    if row is None:
                        break
                    if len(rows) >= source.budget.max_result_rows:
                        truncated = True
                        break
                    encoded_row = jsonable_encoder(row)
                    row_bytes = len(
                        json.dumps(encoded_row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    )
                    separator_bytes = 1 if rows else 0
                    if result_bytes + separator_bytes + row_bytes > source.budget.max_result_bytes:
                        truncated = True
                        break
                    rows.append(encoded_row)
                    result_bytes += separator_bytes + row_bytes
            finally:
                try:
                    await cursor.close()
                except Exception:
                    pass
            await connection.execute("COMMIT")
        except asyncio.CancelledError:
            await connection.cancel_safe(timeout=1)
            await _rollback_quietly(connection)
            raise
        except Exception:
            await _rollback_quietly(connection)
            raise

        return {
            "status": "ok",
            "query_id": query_id,
            "metadata_revision": metadata_revision,
            "fingerprint": validated.fingerprint,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "result_bytes": result_bytes,
            "truncated": truncated,
            "queue_ms": queue_ms,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            "plan_summary": asdict(plan),
        }

    async def _get_pool(self, source: SourceProfile) -> AsyncConnectionPool[Any]:
        existing = self._pools.get(source.source_id)
        if existing is not None:
            return existing
        async with self._pool_lock:
            existing = self._pools.get(source.source_id)
            if existing is not None:
                return existing
            connection = source.connection
            pool = AsyncConnectionPool(
                conninfo="",
                kwargs={
                    "host": connection.host,
                    "port": connection.port,
                    "dbname": connection.database,
                    "user": connection.user,
                    "password": connection.password,
                    "sslmode": "verify-full" if connection.ssl else "disable",
                    "application_name": f"query-man-query:{source.source_id}",
                    "connect_timeout": 2,
                    "row_factory": dict_row,
                },
                min_size=0,
                max_size=source.budget.max_pool_size,
                timeout=source.budget.query_queue_timeout_ms / 1000,
                max_idle=10,
                open=False,
            )
            await pool.open()
            self._pools[source.source_id] = pool
            return pool


def _extract_plan(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        raise RuntimeError("EXPLAIN returned no plan")
    payload = next(iter(row.values()))
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError("EXPLAIN returned an invalid plan")
    plan = payload[0].get("Plan")
    if not isinstance(plan, dict):
        raise RuntimeError("EXPLAIN returned an invalid root plan")
    return plan


def _summarize_plan(root: dict[str, Any]) -> PlanSummary:
    node_count = 0
    max_rows = 0
    pending = [root]
    while pending:
        node = pending.pop()
        node_count += 1
        rows = _non_negative_number(node, "Plan Rows")
        max_rows = max(max_rows, int(rows))
        children = node.get("Plans", [])
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise RuntimeError("EXPLAIN returned invalid child plans")
        pending.extend(children)
    return PlanSummary(
        total_cost=_non_negative_number(root, "Total Cost"),
        max_rows=max_rows,
        node_count=node_count,
    )


def _non_negative_number(node: dict[str, Any], key: str) -> float:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("EXPLAIN returned an invalid numeric estimate")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise RuntimeError("EXPLAIN returned an invalid numeric estimate")
    return number


def _admit_plan(source: SourceProfile, plan: PlanSummary) -> None:
    if plan.total_cost > source.budget.max_plan_total_cost:
        raise QueryRejectedError("QUERY_PLAN_COST_EXCEEDED")
    if plan.max_rows > source.budget.max_plan_rows:
        raise QueryRejectedError("QUERY_PLAN_ROWS_EXCEEDED")
    if plan.node_count > source.budget.max_plan_nodes:
        raise QueryRejectedError("QUERY_PLAN_NODES_EXCEEDED")


async def _rollback_quietly(connection: AsyncConnection[Any]) -> None:
    try:
        await connection.rollback()
    except Exception:
        pass
