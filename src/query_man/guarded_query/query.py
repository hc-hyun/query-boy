from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from query_man.errors import (
    AppError,
    MetadataRevisionMismatchError,
    MetadataUnavailableError,
    QueryInvalidError,
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
    SourceNotFoundError,
)
from query_man.guarded_query.result_encoding import (
    ResultEncodingError,
    _require_supported_result_oids,
    encode_result_value,
)
from query_man.guarded_query.sql_validation import (
    SQL_POLICY_REVISION,
    SqlValidationError,
    ValidatedSql,
    validate_sql,
)
from query_man.metadata.service import MetadataService
from query_man.runtime.operations import operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.reader_policy import (
    READER_SESSION_BUDGET_SETTERS,
    READER_SESSION_TIMEZONE_SETTER,
    ReaderSessionPolicyError,
    reader_connection_kwargs,
    reader_session_budget_values,
    require_reader_connection_policy,
    require_reader_session_policy,
)
from query_man.source_catalog.registry import SourceRegistry

audit_logger = logging.getLogger("query_man.audit")
_RESULT_CURSOR_NAME = "query_man_result"
_RESULT_FETCH_BATCH_ROWS = 16

_QUERY_INVALID_REASON_BY_SQLSTATE = {
    "22003": "QUERY_NUMERIC_VALUE_OUT_OF_RANGE",
    "22007": "QUERY_INVALID_CAST",  # invalid_datetime_format
    "22008": "QUERY_INVALID_CAST",  # datetime_field_overflow
    "22012": "QUERY_DIVISION_BY_ZERO",
    "2201B": "QUERY_INVALID_REGULAR_EXPRESSION",
    "2201W": "QUERY_INVALID_LIMIT",
    "2201X": "QUERY_INVALID_LIMIT",  # invalid_row_count_in_result_offset_clause
    "22023": "QUERY_INVALID_FUNCTION_ARGUMENT",
    "22P02": "QUERY_INVALID_CAST",  # invalid_text_representation
    "42703": "QUERY_UNDEFINED_COLUMN",
    "42809": "QUERY_INVALID_FUNCTION_USAGE",
    "42846": "QUERY_INVALID_CAST",  # cannot_coerce
    "42883": "QUERY_FUNCTION_SIGNATURE_MISMATCH",
}

_QUERY_SESSION_SETTINGS = (
    "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
    "pg_catalog.set_config('transaction_timeout', %s, true), "
    "pg_catalog.set_config('lock_timeout', %s, true), "
    "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, true), "
    "pg_catalog.set_config('search_path', 'pg_catalog', true), "
    "pg_catalog.set_config('row_security', 'on', true), "
    "pg_catalog.set_config('query_man.tenant_id', %s, true), "
    "pg_catalog.set_config('application_name', %s, true), " + READER_SESSION_BUDGET_SETTERS
)

_FUNCTION_POLICY_QUERY = """
  SELECT
    routine.proname::text AS object_name,
    pg_catalog.bool_and(
      namespace.nspname = 'pg_catalog'
      AND routine.provolatile <> 'v'
      AND NOT routine.prosecdef
      AND pg_catalog.has_function_privilege(session_user, routine.oid, 'EXECUTE')
    ) AS is_safe
  FROM pg_catalog.pg_proc AS routine
  JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
  WHERE routine.proname::text = ANY(%s::text[])
    AND pg_catalog.pg_function_is_visible(routine.oid)
  GROUP BY routine.proname
"""

_OPERATOR_POLICY_QUERY = """
  SELECT
    operator_row.oprname::text AS object_name,
    pg_catalog.bool_and(
      operator_namespace.nspname = 'pg_catalog'
      AND implementation_namespace.nspname = 'pg_catalog'
      AND implementation.provolatile <> 'v'
      AND NOT implementation.prosecdef
      AND pg_catalog.has_function_privilege(
        session_user,
        implementation.oid,
        'EXECUTE'
      )
    ) AS is_safe
  FROM pg_catalog.pg_operator AS operator_row
  JOIN pg_catalog.pg_namespace AS operator_namespace
    ON operator_namespace.oid = operator_row.oprnamespace
  JOIN pg_catalog.pg_proc AS implementation ON implementation.oid = operator_row.oprcode
  JOIN pg_catalog.pg_namespace AS implementation_namespace
    ON implementation_namespace.oid = implementation.pronamespace
  WHERE operator_row.oprname::text = ANY(%s::text[])
    AND pg_catalog.pg_operator_is_visible(operator_row.oid)
  GROUP BY operator_row.oprname
"""


@dataclass(frozen=True)
class PlanSummary:
    total_cost: float
    max_rows: int
    node_count: int


@dataclass
class _ActiveQuery:
    connection: AsyncConnection[dict[str, Any]]
    task: asyncio.Task[Any]
    cancel_reason: Literal["shutdown"] | None = None


class QueryService:
    def __init__(
        self,
        registry: SourceRegistry,
        metadata: MetadataService,
        executor: PostgresQueryExecutor,
    ) -> None:
        self._registry = registry
        self._metadata = metadata
        self._executor = executor

    async def query(
        self,
        source_id: str,
        sql: str,
        metadata_revision: str,
        sql_policy_revision: str,
        *,
        query_id: str | None = None,
    ) -> dict[str, object]:
        source = self._registry.get(source_id)
        if source is None:
            raise SourceNotFoundError
        try:
            published = await self._metadata.get_published(source_id)
        except MetadataUnavailableError as error:
            if isinstance(error.__cause__, ReaderSessionPolicyError):
                raise QueryUnavailableError from None
            raise
        if metadata_revision != published.revision or sql_policy_revision != SQL_POLICY_REVISION:
            operations.increment("query_revision_rejected", source.source_id)
            raise MetadataRevisionMismatchError
        try:
            validated = validate_sql(
                sql,
                allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
                max_sql_bytes=source.budget.max_sql_bytes,
            )
        except SqlValidationError as error:
            operations.increment("query_rejected", source.source_id)
            raise QueryRejectedError(
                error.code,
                rejected_construct=error.rejected_construct,
            ) from error
        result = await self._executor.execute(
            source,
            sql,
            published.revision,
            validated,
            query_id=query_id,
        )
        result["sql_policy_revision"] = SQL_POLICY_REVISION
        return result


class PostgresQueryExecutor:
    def __init__(self) -> None:
        self._pools: dict[str, AsyncConnectionPool[Any]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._pool_lock = asyncio.Lock()
        self._active_lock = asyncio.Lock()
        self._active: dict[str, _ActiveQuery] = {}
        self._inflight: set[asyncio.Task[Any]] = set()
        self._accepting = True

    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        *,
        query_id: str | None = None,
    ) -> dict[str, object]:
        task = asyncio.current_task()
        if task is None:
            raise QueryUnavailableError
        effective_query_id = query_id or str(uuid.uuid4())
        async with self._active_lock:
            if not self._accepting:
                raise QueryUnavailableError
            self._inflight.add(task)
        try:
            return await self._execute_admitted(
                source,
                sql,
                metadata_revision,
                validated,
                task,
                query_id=effective_query_id,
            )
        except asyncio.CancelledError as error:
            if not self._accepting:
                audit_logger.info(
                    "query_execution_failed query_id=%s source_id=%s fingerprint=%s error_code=QUERY_UNAVAILABLE",
                    effective_query_id,
                    source.source_id,
                    validated.fingerprint,
                    extra={
                        "query_id": effective_query_id,
                        "source_id": source.source_id,
                        "fingerprint": validated.fingerprint,
                        "error_code": "QUERY_UNAVAILABLE",
                    },
                )
                raise QueryUnavailableError from error
            operations.increment("query_interrupted", source.source_id)
            audit_logger.info(
                "query_execution_interrupted query_id=%s source_id=%s fingerprint=%s",
                effective_query_id,
                source.source_id,
                validated.fingerprint,
                extra={
                    "query_id": effective_query_id,
                    "source_id": source.source_id,
                    "fingerprint": validated.fingerprint,
                    "cancel_reason": "interrupted",
                },
            )
            raise
        except AppError as error:
            audit_logger.info(
                "query_execution_failed query_id=%s source_id=%s fingerprint=%s error_code=%s",
                effective_query_id,
                source.source_id,
                validated.fingerprint,
                error.code,
                extra={
                    "query_id": effective_query_id,
                    "source_id": source.source_id,
                    "fingerprint": validated.fingerprint,
                    "error_code": error.code,
                },
            )
            raise
        finally:
            async with self._active_lock:
                self._inflight.discard(task)

    async def _execute_admitted(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        task: asyncio.Task[Any],
        *,
        query_id: str,
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
            operations.increment("query_queue_rejected", source.source_id)
            raise QueryOverloadedError from error

        queue_ms = round((time.monotonic() - queued_at) * 1000)
        operations.observe("query_queue_ms", queue_ms, source.source_id)
        operations.increment("query_execution_started", source.source_id)
        active_query: _ActiveQuery | None = None
        try:
            try:
                pool = await self._get_pool(source)
                async with asyncio.timeout(source.budget.query_transaction_timeout_ms / 1000):
                    async with pool.connection(timeout=source.budget.query_queue_timeout_ms / 1000) as connection:
                        try:
                            require_reader_connection_policy(
                                connection,
                                source.connection.sslmode,
                            )
                        except ReaderSessionPolicyError:
                            try:
                                await connection.close()
                            except Exception:
                                pass
                            raise
                        async with self._active_lock:
                            active_query = _ActiveQuery(
                                connection,
                                task,
                            )
                            self._active[query_id] = active_query
                        try:
                            return await self._execute_connection(
                                connection,
                                source,
                                sql,
                                metadata_revision,
                                validated,
                                queue_ms,
                                query_id,
                            )
                        finally:
                            async with self._active_lock:
                                self._active.pop(query_id, None)
            except PoolTimeout as error:
                operations.increment("query_pool_exhausted", source.source_id)
                operations.set_source_query_health(source.source_id, "unavailable")
                raise QueryUnavailableError from error
            except TimeoutError as error:
                operations.increment("query_timeout", source.source_id)
                raise QueryTimeoutError from error
            except errors.QueryCanceled as error:
                reason = active_query.cancel_reason if active_query is not None else None
                metric = "query_shutdown_cancelled" if reason == "shutdown" else "query_timeout"
                operations.increment(metric, source.source_id)
                raise QueryTimeoutError from error
            except (
                errors.OperationalError,
                errors.InterfaceError,
                ReaderSessionPolicyError,
            ) as error:
                operations.increment("query_failed", source.source_id)
                operations.set_source_query_health(source.source_id, "unavailable")
                raise QueryUnavailableError from error
            except QueryRejectedError:
                operations.increment("query_rejected", source.source_id)
                raise
            except (QueryInvalidError, QueryOverloadedError, QueryTimeoutError):
                raise
            except Exception as error:
                operations.increment("query_failed", source.source_id)
                raise QueryUnavailableError from error
        finally:
            semaphore.release()

    def stop_accepting(self) -> None:
        self._accepting = False

    async def close(self) -> None:
        if self._accepting:
            await self.drain(0)
        else:
            async with self._active_lock:
                inflight = list(self._inflight)
                active = list(self._active.values())
            if inflight:
                await self._cancel_inflight(inflight, active)
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    async def drain(self, grace_ms: int) -> None:
        self.stop_accepting()
        operations.increment("shutdown_started")
        deadline = time.monotonic() + grace_ms / 1_000
        while True:
            async with self._active_lock:
                inflight = list(self._inflight)
                active = list(self._active.values())
            if not inflight:
                operations.increment("shutdown_drained")
                return
            if time.monotonic() >= deadline:
                await self._cancel_inflight(inflight, active)
                operations.increment("shutdown_forced_cancel", value=len(inflight))
                return
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    async def _cancel_inflight(
        self,
        inflight: list[asyncio.Task[Any]],
        active: list[_ActiveQuery],
    ) -> None:
        active_tasks = {query.task for query in active}
        for task in inflight:
            if task not in active_tasks:
                task.cancel()
        for query in active:
            if query.cancel_reason is None:
                query.cancel_reason = "shutdown"
        await asyncio.gather(
            *(query.connection.cancel_safe(timeout=1) for query in active),
            return_exceptions=True,
        )
        _done, pending = await asyncio.wait(inflight, timeout=1)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1)

    async def _execute_connection(
        self,
        connection: AsyncConnection[dict[str, Any]],
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        queue_ms: int,
        query_id: str,
    ) -> dict[str, object]:
        started_at = time.monotonic()
        try:
            await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await connection.execute(READER_SESSION_TIMEZONE_SETTER)
            await connection.execute(
                _QUERY_SESSION_SETTINGS,
                (
                    f"{source.budget.query_statement_timeout_ms}ms",
                    f"{source.budget.query_transaction_timeout_ms}ms",
                    f"{source.budget.lock_timeout_ms}ms",
                    f"{source.budget.query_transaction_timeout_ms}ms",
                    "",
                    f"query-man:{query_id}",
                    *reader_session_budget_values(source),
                ),
            )
            await require_reader_session_policy(connection, source, "")

            await _validate_resolved_objects(connection, validated)
            try:
                plan_cursor = await connection.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                plan_row = await plan_cursor.fetchone()
            except errors.DatabaseError as error:
                _raise_query_invalid_for_user_sql(error, source.source_id)
                raise
            plan = _summarize_plan(_extract_plan(plan_row))
            try:
                _admit_plan(source, plan)
            except QueryRejectedError as error:
                reason_code = error.details.get("reason_code") if isinstance(error.details, dict) else "QUERY_REJECTED"
                audit_logger.info(
                    "query_plan_rejected query_id=%s source_id=%s fingerprint=%s "
                    "reason_code=%s plan_total_cost=%s plan_max_rows=%s plan_node_count=%s "
                    "plan_cost_limit=%s plan_rows_limit=%s plan_nodes_limit=%s",
                    query_id,
                    source.source_id,
                    validated.fingerprint,
                    reason_code,
                    plan.total_cost,
                    plan.max_rows,
                    plan.node_count,
                    source.budget.max_plan_total_cost,
                    source.budget.max_plan_rows,
                    source.budget.max_plan_nodes,
                    extra={
                        "query_id": query_id,
                        "source_id": source.source_id,
                        "fingerprint": validated.fingerprint,
                        "reason_code": reason_code,
                        "plan_total_cost": plan.total_cost,
                        "plan_max_rows": plan.max_rows,
                        "plan_node_count": plan.node_count,
                        "plan_cost_limit": source.budget.max_plan_total_cost,
                        "plan_rows_limit": source.budget.max_plan_rows,
                        "plan_nodes_limit": source.budget.max_plan_nodes,
                    },
                )
                raise

            rows: list[object] = []
            result_bytes = 2  # JSON array brackets.
            truncated = False
            # ponytail: a pool lease serializes use of one connection; a fixed name avoids
            # one pg_stat_statements entry per request UUID.
            cursor = connection.cursor(name=_RESULT_CURSOR_NAME, row_factory=dict_row)
            try:
                await cursor.execute(sql)
                description = cursor.description
                try:
                    result_columns = tuple(description or ())
                    columns = [column.name for column in result_columns]
                    if any(not isinstance(column, str) for column in columns):
                        raise ResultEncodingError("Unsupported PostgreSQL result type")
                except Exception:
                    raise ResultEncodingError("Unsupported PostgreSQL result type") from None
                if len(columns) != len(set(columns)):
                    raise QueryRejectedError("QUERY_DUPLICATE_RESULT_COLUMN")
                _require_supported_result_oids(column.type_code for column in result_columns)
                while not truncated:
                    remaining_with_sentinel = source.budget.max_result_rows - len(rows) + 1
                    batch = await cursor.fetchmany(min(_RESULT_FETCH_BATCH_ROWS, remaining_with_sentinel))
                    if not batch:
                        break
                    # ponytail: a small fixed batch bounds the row count before byte accounting,
                    # not the size of an individual value, and avoids one round trip per row.
                    for row in batch:
                        if len(rows) >= source.budget.max_result_rows:
                            truncated = True
                            break
                        encoded_row = encode_result_value(row)
                        row_bytes = len(
                            json.dumps(
                                encoded_row,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                        )
                        separator_bytes = 1 if rows else 0
                        if result_bytes + separator_bytes + row_bytes > source.budget.max_result_bytes:
                            truncated = True
                            break
                        rows.append(encoded_row)
                        result_bytes += separator_bytes + row_bytes
            except errors.DatabaseError as error:
                _raise_query_invalid_for_user_sql(error, source.source_id)
                raise
            finally:
                try:
                    await cursor.close()
                except Exception:
                    pass
            await connection.execute("COMMIT")
            operations.set_source_query_health(source.source_id, "healthy")
        except asyncio.CancelledError:
            await connection.cancel_safe(timeout=1)
            await _rollback_quietly(connection)
            raise
        except Exception:
            await _rollback_quietly(connection)
            raise

        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        operations.increment("query_execution_succeeded", source.source_id)
        operations.observe("query_elapsed_ms", elapsed_ms, source.source_id)
        if truncated:
            operations.increment("query_truncated", source.source_id)
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
            "elapsed_ms": elapsed_ms,
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
            pool = AsyncConnectionPool(
                conninfo="",
                kwargs={
                    **reader_connection_kwargs(
                        source,
                        f"query-man-query:{source.source_id}",
                    ),
                    # ponytail: explicit BEGIN must not be preceded by psycopg's implicit BEGIN.
                    "autocommit": True,
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


def _raise_query_invalid_for_user_sql(
    error: errors.DatabaseError,
    source_id: str,
) -> None:
    reason_code = _QUERY_INVALID_REASON_BY_SQLSTATE.get(error.sqlstate or "")
    if reason_code is None:
        return
    operations.increment("query_invalid", source_id)
    raise QueryInvalidError(reason_code) from error


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


async def _validate_resolved_objects(
    connection: AsyncConnection[dict[str, Any]],
    validated: ValidatedSql,
) -> None:
    functions = {name.removeprefix("pg_catalog.") for name in validated.functions}
    operators = set(validated.operators)
    if functions:
        cursor = await connection.execute(_FUNCTION_POLICY_QUERY, (sorted(functions),))
        rows = await cursor.fetchall()
        if {str(row["object_name"]) for row in rows if row["is_safe"]} != functions:
            raise QueryRejectedError("QUERY_RESOLVED_FUNCTION_NOT_ALLOWED")
    if operators:
        cursor = await connection.execute(_OPERATOR_POLICY_QUERY, (sorted(operators),))
        rows = await cursor.fetchall()
        if {str(row["object_name"]) for row in rows if row["is_safe"]} != operators:
            raise QueryRejectedError("QUERY_RESOLVED_OPERATOR_NOT_ALLOWED")
