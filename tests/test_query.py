from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from psycopg import errors

from query_man.app import _until_disconnect
from query_man.errors import (
    MetadataRevisionMismatchError,
    QueryInvalidError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
)
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.operations import operations
from query_man.query import PlanSummary, PostgresQueryExecutor, QueryService, _summarize_plan
from query_man.registry import SourceRegistry
from query_man.sql_validation import SQL_POLICY_REVISION, ValidatedSql
from tests.helpers import load_test_registry, minimal_development_snapshot


class StaticCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return minimal_development_snapshot()

    async def close(self) -> None:
        pass


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[SourceProfile, str, str, ValidatedSql]] = []

    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((source, sql, metadata_revision, validated))
        return {
            "status": "ok",
            "query_id": query_id or "test-query-id",
            "metadata_revision": metadata_revision,
            "fingerprint": validated.fingerprint,
            "columns": ["issue_count"],
            "rows": [{"issue_count": 3}],
            "row_count": 1,
            "result_bytes": 19,
            "truncated": False,
            "queue_ms": 0,
            "elapsed_ms": 1,
            "plan_summary": {"total_cost": 1.0, "max_rows": 1, "node_count": 1},
        }

    async def close(self) -> None:
        pass

    async def cancel(self, _query_id: str) -> bool:
        return False


def query_service() -> tuple[QueryService, MetadataService, RecordingExecutor]:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = RecordingExecutor()
    return QueryService(registry, metadata, executor), metadata, executor


@pytest.mark.asyncio
async def test_validates_revision_and_sql_before_execution() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    response = await service.query(
        "development-issues",
        "SELECT count(*) AS issue_count FROM ai.issue_overview",
        published.revision,
        SQL_POLICY_REVISION,
    )

    assert response["status"] == "ok"
    assert len(executor.calls) == 1
    assert executor.calls[0][3].relations == ("ai.issue_overview",)


@pytest.mark.asyncio
async def test_rejects_stale_revision_before_execution() -> None:
    service, _metadata, executor = query_service()

    with pytest.raises(MetadataRevisionMismatchError):
        await service.query(
            "development-issues",
            "SELECT 1",
            f"sha256:{'0' * 64}",
            SQL_POLICY_REVISION,
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_rejects_stale_sql_policy_revision_before_execution() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    with pytest.raises(MetadataRevisionMismatchError):
        await service.query(
            "development-issues",
            "SELECT 1",
            published.revision,
            f"sha256:{'0' * 64}",
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_maps_ast_rejection_to_stable_query_error() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    with pytest.raises(QueryRejectedError) as caught:
        await service.query(
            "development-issues",
            "DELETE FROM ai.issue_overview",
            published.revision,
            SQL_POLICY_REVISION,
        )

    assert caught.value.details == {"reason_code": "SQL_STATEMENT_NOT_ALLOWED"}
    assert executor.calls == []


@pytest.mark.asyncio
async def test_maps_forbidden_between_variant_to_bounded_query_details() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    with pytest.raises(QueryRejectedError) as caught:
        await service.query(
            "development-issues",
            "SELECT issue_id FROM ai.issue_overview WHERE issue_id NOT BETWEEN 1 AND 2",
            published.revision,
            SQL_POLICY_REVISION,
        )

    assert caught.value.details == {
        "reason_code": "SQL_OPERATOR_NOT_ALLOWED",
        "rejected_construct": "NOT BETWEEN",
    }
    assert executor.calls == []


def test_query_rejection_details_do_not_reflect_unknown_construct() -> None:
    error = QueryRejectedError(
        "SQL_OPERATOR_NOT_ALLOWED",
        rejected_construct="OPERATOR <= DATE 'private-literal' AT LOCATION 42",
    )

    assert error.details == {"reason_code": "SQL_OPERATOR_NOT_ALLOWED"}


@pytest.mark.asyncio
async def test_rls_source_requires_server_supplied_tenant_context() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    registry = SourceRegistry([replace(source, tenant_isolation="rls")])
    metadata = MetadataService(registry, StaticCatalog())
    executor = RecordingExecutor()
    service = QueryService(registry, metadata, executor)

    with pytest.raises(QueryRejectedError) as captured:
        await service.query(
            source.source_id,
            "SELECT count(*) FROM ai.issue_overview",
            f"sha256:{'0' * 64}",
            SQL_POLICY_REVISION,
        )

    assert captured.value.details == {"reason_code": "TENANT_CONTEXT_REQUIRED"}
    assert executor.calls == []


def test_summarizes_nested_explain_plan() -> None:
    plan: dict[str, Any] = {
        "Total Cost": 42.25,
        "Plan Rows": 2,
        "Plans": [
            {"Total Cost": 10, "Plan Rows": 100},
            {"Total Cost": 20, "Plan Rows": 5, "Plans": [{"Plan Rows": 300}]},
        ],
    }

    assert _summarize_plan(plan) == PlanSummary(total_cost=42.25, max_rows=300, node_count=4)


def test_rejects_plan_without_required_estimates() -> None:
    with pytest.raises(RuntimeError):
        _summarize_plan({"Plan Rows": 1})


def test_query_invalid_error_rejects_nonpublic_reason() -> None:
    with pytest.raises(ValueError, match="Query invalid reason is not public"):
        QueryInvalidError("private_column DATE 'private-literal' AT LOCATION 42")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_error", "reason_code"),
    [
        (errors.UndefinedColumn("private_column at character 42"), "QUERY_UNDEFINED_COLUMN"),
        (
            errors.InvalidTextRepresentation("invalid input syntax for private type"),
            "QUERY_INVALID_CAST",
        ),
        (errors.InvalidDatetimeFormat("private date literal"), "QUERY_INVALID_CAST"),
        (errors.DatetimeFieldOverflow("private timestamp literal"), "QUERY_INVALID_CAST"),
        (errors.CannotCoerce("private source and target types"), "QUERY_INVALID_CAST"),
        (errors.DivisionByZero("private expression at character 42"), "QUERY_DIVISION_BY_ZERO"),
        (
            errors.InvalidRowCountInLimitClause("private negative limit"),
            "QUERY_INVALID_LIMIT",
        ),
        (
            errors.InvalidRowCountInResultOffsetClause("private negative offset"),
            "QUERY_INVALID_LIMIT",
        ),
    ],
)
async def test_executor_maps_correctable_database_errors_to_bounded_query_invalid(
    database_error: errors.DatabaseError,
    reason_code: str,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()

    class ConnectionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakePool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout > 0
            return ConnectionContext()

    async def get_pool(_source: SourceProfile) -> FakePool:
        return FakePool()

    async def execute_connection(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise database_error

    executor._get_pool = get_pool  # type: ignore[method-assign]
    executor._execute_connection = execute_connection  # type: ignore[method-assign]
    operations.reset()
    try:
        with pytest.raises(QueryInvalidError) as caught:
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert caught.value.status_code == 400
        assert caught.value.code == "QUERY_INVALID"
        assert caught.value.message == "The query must be corrected before it can run."
        assert caught.value.details == {"reason_code": reason_code}
        assert "private" not in str(caught.value)
        assert any(
            metric["name"] == "query_invalid"
            and metric.get("source_id") == source.source_id
            and metric["value"] == 1
            for metric in operations.snapshot()["metrics"]
        )
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_error",
    [
        errors.InsufficientPrivilege("private relation"),
        errors.AdminShutdown("private server details"),
        errors.DatabaseError("private unknown database failure"),
    ],
)
async def test_executor_keeps_noncorrectable_database_errors_unavailable(
    database_error: errors.DatabaseError,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()

    class ConnectionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakePool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout > 0
            return ConnectionContext()

    async def get_pool(_source: SourceProfile) -> FakePool:
        return FakePool()

    async def execute_connection(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise database_error

    executor._get_pool = get_pool  # type: ignore[method-assign]
    executor._execute_connection = execute_connection  # type: ignore[method-assign]
    operations.reset()
    try:
        with pytest.raises(QueryUnavailableError) as caught:
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert caught.value.status_code == 503
        assert caught.value.code == "QUERY_UNAVAILABLE"
        assert caught.value.details is None
        assert "private" not in str(caught.value)
        assert not any(
            metric["name"] == "query_invalid"
            for metric in operations.snapshot()["metrics"]
        )
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_executor_rejects_new_queries_after_drain_starts() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()
    await executor.drain(0)

    with pytest.raises(QueryUnavailableError):
        await executor.execute(
            source,
            "SELECT 1",
            "test-revision",
            ValidatedSql("fingerprint", (), (), ()),
        )


@pytest.mark.asyncio
async def test_executor_distinguishes_operator_cancel_from_statement_timeout() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    wait_for_operator = True

    class FakeConnection:
        async def cancel_safe(self, **options: int) -> None:
            assert options == {"timeout": 1}
            cancelled.set()

    connection = FakeConnection()

    class ConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakePool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout > 0
            return ConnectionContext()

    async def get_pool(_source: SourceProfile) -> FakePool:
        return FakePool()

    async def execute_connection(*_args: object, **_kwargs: object) -> dict[str, object]:
        if wait_for_operator:
            started.set()
            await cancelled.wait()
        raise errors.QueryCanceled

    executor._get_pool = get_pool  # type: ignore[method-assign]
    executor._execute_connection = execute_connection  # type: ignore[method-assign]
    validated = ValidatedSql("pg_query:test", (), (), ())
    query_id = "00000000-0000-0000-0000-000000000001"
    operations.reset()
    try:
        pending = asyncio.create_task(
            executor.execute(
                source,
                "SELECT 1",
                "test-revision",
                validated,
                query_id=query_id,
            )
        )
        await started.wait()
        assert await executor.cancel(query_id)
        with pytest.raises(QueryTimeoutError):
            await pending

        wait_for_operator = False
        with pytest.raises(QueryTimeoutError):
            await executor.execute(
                source,
                "SELECT 1",
                "test-revision",
                validated,
            )

        metrics = {
            (metric["name"], metric.get("source_id")): metric["value"]
            for metric in operations.snapshot()["metrics"]
        }
        assert metrics[("query_cancel_requested", source.source_id)] == 1
        assert metrics[("query_cancelled", source.source_id)] == 1
        assert metrics[("query_timeout", source.source_id)] == 1
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_drain_cancels_active_and_queued_admitted_queries() -> None:
    operations.reset()
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        budget=replace(
            source.budget,
            max_pool_size=1,
            max_concurrent_queries=1,
        ),
    )
    executor = PostgresQueryExecutor()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeConnection:
        async def cancel_safe(self, **options: int) -> None:
            assert options == {"timeout": 1}
            cancelled.set()

    connection = FakeConnection()

    class ConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakePool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout > 0
            return ConnectionContext()

    async def get_pool(_source: SourceProfile) -> FakePool:
        return FakePool()

    async def execute_connection(*_args: object, **_kwargs: object) -> dict[str, object]:
        started.set()
        await cancelled.wait()
        raise errors.QueryCanceled

    executor._get_pool = get_pool  # type: ignore[method-assign]
    executor._execute_connection = execute_connection  # type: ignore[method-assign]
    validated = ValidatedSql("fingerprint", (), (), ())
    active = asyncio.create_task(
        executor.execute(source, "SELECT 1", "test-revision", validated)
    )
    await started.wait()
    queued = asyncio.create_task(
        executor.execute(source, "SELECT 1", "test-revision", validated)
    )
    for _attempt in range(100):
        if len(executor._inflight) == 2:
            break
        await asyncio.sleep(0)
    assert len(executor._inflight) == 2

    await executor.drain(0)

    with pytest.raises(QueryTimeoutError):
        await active
    with pytest.raises(QueryUnavailableError):
        await queued
    assert executor._inflight == set()
    assert any(
        metric["name"] == "query_shutdown_cancelled"
        and metric.get("source_id") == source.source_id
        and metric["value"] == 1
        for metric in operations.snapshot()["metrics"]
    )
    operations.reset()


@pytest.mark.asyncio
async def test_drain_cancels_admitted_query_waiting_for_pool_connection() -> None:
    operations.reset()
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        budget=replace(source.budget, max_concurrent_queries=1),
    )
    executor = PostgresQueryExecutor()
    lease_waiting = asyncio.Event()
    lease_released = asyncio.Event()

    class FakeConnection:
        pass

    class ConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            lease_waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                lease_released.set()
            raise AssertionError("pool lease unexpectedly acquired")

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakePool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout > 0
            return ConnectionContext()

    async def get_pool(_source: SourceProfile) -> FakePool:
        return FakePool()

    executor._get_pool = get_pool  # type: ignore[method-assign]
    pending = asyncio.create_task(
        executor.execute(
            source,
            "SELECT 1",
            "test-revision",
            ValidatedSql("fingerprint", (), (), ()),
        )
    )
    try:
        await asyncio.wait_for(lease_waiting.wait(), 1)
        await asyncio.wait_for(executor.drain(1), 2)

        with pytest.raises(QueryUnavailableError):
            await asyncio.wait_for(pending, 1)
        assert lease_released.is_set()
        assert executor._inflight == set()
        assert not executor._semaphores[source.source_id].locked()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        operations.reset()


@pytest.mark.asyncio
async def test_client_disconnect_cancels_active_query() -> None:
    cancelled = asyncio.Event()

    async def pending() -> dict[str, object]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class DisconnectedRequest:
        async def receive(self) -> dict[str, str]:
            return {"type": "http.disconnect"}

    with pytest.raises(QueryTimeoutError):
        await _until_disconnect(DisconnectedRequest(), pending())  # type: ignore[arg-type]
    assert cancelled.is_set()
