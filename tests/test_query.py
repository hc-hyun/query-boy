from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import Any, get_type_hints

import pytest
from psycopg import errors

import query_man.query as query_module
from query_man.app import _until_disconnect
from query_man.errors import (
    MetadataRevisionMismatchError,
    QueryInvalidError,
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
    SourceNotFoundError,
)
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.operations import operations
from query_man.query import (
    PlanSummary,
    PostgresQueryExecutor,
    QueryExecutor,
    QueryService,
    RuntimeQueryExecutor,
    _summarize_plan,
)
from query_man.registry import SourceRegistry
from query_man.sql_validation import SQL_POLICY_REVISION, ValidatedSql
from tests.helpers import load_test_registry, minimal_development_snapshot


def test_runtime_query_executor_protocol_has_exact_lifecycle_shape() -> None:
    application_methods = {
        name
        for name, value in vars(QueryExecutor).items()
        if not name.startswith("_") and callable(value)
    }
    runtime_methods = {
        name
        for name, value in vars(RuntimeQueryExecutor).items()
        if not name.startswith("_") and callable(value)
    }

    assert application_methods == {"cancel", "close", "execute"}
    assert QueryExecutor in RuntimeQueryExecutor.__mro__
    assert runtime_methods == {"drain", "invalidate", "stop_accepting"}
    assert get_type_hints(RuntimeQueryExecutor.stop_accepting) == {
        "return": type(None),
    }
    assert get_type_hints(RuntimeQueryExecutor.drain) == {
        "grace_ms": int,
        "return": type(None),
    }
    assert get_type_hints(RuntimeQueryExecutor.invalidate) == {
        "source_id": str,
        "return": type(None),
    }
    assert not inspect.iscoroutinefunction(RuntimeQueryExecutor.stop_accepting)
    assert inspect.iscoroutinefunction(RuntimeQueryExecutor.drain)
    assert inspect.iscoroutinefunction(RuntimeQueryExecutor.invalidate)
    for method, names in (
        (RuntimeQueryExecutor.stop_accepting, ("self",)),
        (RuntimeQueryExecutor.drain, ("self", "grace_ms")),
        (RuntimeQueryExecutor.invalidate, ("self", "source_id")),
    ):
        parameters = tuple(inspect.signature(method).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in parameters
        )
    assert get_type_hints(QueryService.__init__)["executor"] is QueryExecutor


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


class FailingExecutor(RecordingExecutor):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

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
        raise self.error


class _PlanCursor:
    async def fetchone(self) -> dict[str, object]:
        return {
            "QUERY PLAN": [
                {
                    "Plan": {
                        "Total Cost": 1.0,
                        "Plan Rows": 1,
                    }
                }
            ]
        }


class _ResultCursor:
    description: tuple[object, ...] = ()

    def __init__(
        self,
        phase: str,
        database_error: errors.DatabaseError,
        events: list[str],
    ) -> None:
        self._phase = phase
        self._database_error = database_error
        self._events = events

    async def execute(self, _sql: str) -> None:
        self._events.append("query")
        if self._phase == "cursor_execute":
            raise self._database_error

    async def fetchmany(self, _size: int) -> list[object]:
        if self._phase == "cursor_fetch":
            raise self._database_error
        return []

    async def close(self) -> None:
        pass


class _PhaseConnection:
    def __init__(self, phase: str, database_error: errors.DatabaseError) -> None:
        self.phase = phase
        self.database_error = database_error
        self.rolled_back = False
        self.events: list[str] = []

    async def execute(
        self,
        statement: str,
        _parameters: object | None = None,
    ) -> object:
        if statement == "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY":
            self.events.append("begin")
        elif statement == query_module.READER_SESSION_TIMEZONE_SETTER:
            self.events.append("timezone")
        elif statement == query_module._QUERY_SESSION_SETTINGS:
            self.events.append("settings")
        elif statement.startswith("EXPLAIN"):
            self.events.append("explain")
        elif statement == "COMMIT":
            self.events.append("commit")
        if (
            self.phase == "timezone"
            and statement == query_module.READER_SESSION_TIMEZONE_SETTER
        ):
            raise self.database_error
        if self.phase == "session" and "set_config('statement_timeout'" in statement:
            raise self.database_error
        if self.phase == "explain" and statement.startswith("EXPLAIN"):
            raise self.database_error
        if self.phase == "commit" and statement == "COMMIT":
            raise self.database_error
        if statement.startswith("EXPLAIN"):
            return _PlanCursor()
        return object()

    def cursor(self, **_options: object) -> _ResultCursor:
        return _ResultCursor(self.phase, self.database_error, self.events)

    async def rollback(self) -> None:
        self.rolled_back = True


class _ConnectionContext:
    def __init__(self, connection: _PhaseConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _PhaseConnection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        pass


class _PhasePool:
    def __init__(self, connection: _PhaseConnection) -> None:
        self._connection = connection

    def connection(self, *, timeout: float) -> _ConnectionContext:
        assert timeout > 0
        return _ConnectionContext(self._connection)


def _stub_internal_query_checks(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    database_error: errors.DatabaseError,
    events: list[str] | None = None,
) -> None:
    async def require_reader_policy(*_args: object) -> None:
        if events is not None:
            events.append("reader_policy")
        if phase == "reader_policy":
            raise database_error

    async def validate_resolved_objects(*_args: object) -> None:
        if events is not None:
            events.append("resolved_object")
        if phase == "resolved_object":
            raise database_error

    monkeypatch.setattr(query_module, "require_reader_session_policy", require_reader_policy)
    monkeypatch.setattr(query_module, "_validate_resolved_objects", validate_resolved_objects)


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
async def test_success_records_one_gateway_terminal_with_success_only_sums() -> None:
    service, metadata, _executor = query_service()
    published = await metadata.get_published("development-issues")
    source = load_test_registry().get("development-issues")
    assert source is not None
    operations.reset()
    try:
        response = await service.query(
            source.source_id,
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
            SQL_POLICY_REVISION,
        )

        snapshot = operations.gateway_usage_report_snapshot()
        assert snapshot is not None
        assert len(snapshot.deltas) == 1
        delta = snapshot.deltas[0]
        assert delta.source_id == source.source_id
        assert delta.budget_profile == source.budget.name
        assert delta.metadata_revision == published.revision
        assert delta.query_count == 1
        assert (
            delta.success_count,
            delta.rejected_count,
            delta.timeout_count,
            delta.overloaded_count,
            delta.cancelled_count,
            delta.failed_count,
        ) == (1, 0, 0, 0, 0, 0)
        assert delta.queue_ms_sum == response["queue_ms"] == 0
        assert delta.elapsed_ms_sum == response["elapsed_ms"] == 1
        assert delta.returned_rows_sum == response["row_count"] == 1
        assert delta.result_bytes_sum == response["result_bytes"] == 19
        assert delta.truncated_count == 0
    finally:
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_error", "counts"),
    [
        (QueryRejectedError("QUERY_PLAN_COST_EXCEEDED"), (0, 1, 0, 0, 0, 0)),
        (QueryInvalidError("QUERY_INVALID_CAST"), (0, 1, 0, 0, 0, 0)),
        (QueryOverloadedError(), (0, 0, 0, 1, 0, 0)),
        (QueryTimeoutError(), (0, 0, 1, 0, 0, 0)),
        (query_module._QueryCancelledTimeoutError(), (0, 0, 0, 0, 1, 0)),
        (query_module._QueryCancelledUnavailableError(), (0, 0, 0, 0, 1, 0)),
        (asyncio.CancelledError(), (0, 0, 0, 0, 1, 0)),
        (QueryUnavailableError(), (0, 0, 0, 0, 0, 1)),
        (RuntimeError("bounded internal failure"), (0, 0, 0, 0, 0, 1)),
    ],
    ids=[
        "rejected",
        "invalid-user-sql",
        "overloaded",
        "timeout",
        "operator-or-shutdown-cancelled",
        "queued-shutdown-cancelled",
        "disconnect-cancelled",
        "unavailable",
        "unexpected-failure",
    ],
)
async def test_query_service_maps_each_terminal_to_exactly_one_gateway_category(
    terminal_error: BaseException,
    counts: tuple[int, int, int, int, int, int],
) -> None:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    published = await metadata.get_published("development-issues")
    service = QueryService(registry, metadata, FailingExecutor(terminal_error))
    operations.reset()
    try:
        with pytest.raises(type(terminal_error)):
            await service.query(
                "development-issues",
                "SELECT count(*) FROM ai.issue_overview",
                published.revision,
                SQL_POLICY_REVISION,
            )

        snapshot = operations.gateway_usage_report_snapshot()
        assert snapshot is not None
        assert len(snapshot.deltas) == 1
        delta = snapshot.deltas[0]
        assert delta.query_count == 1
        assert (
            delta.success_count,
            delta.rejected_count,
            delta.timeout_count,
            delta.overloaded_count,
            delta.cancelled_count,
            delta.failed_count,
        ) == counts
        assert (
            delta.queue_ms_sum,
            delta.elapsed_ms_sum,
            delta.returned_rows_sum,
            delta.result_bytes_sum,
            delta.truncated_count,
        ) == (0, 0, 0, 0, 0)
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_revision_and_ast_rejections_are_each_recorded_once() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")
    operations.reset()
    try:
        with pytest.raises(MetadataRevisionMismatchError):
            await service.query(
                "development-issues",
                "SELECT 1",
                f"sha256:{'0' * 64}",
                SQL_POLICY_REVISION,
            )
        with pytest.raises(QueryRejectedError):
            await service.query(
                "development-issues",
                "DELETE FROM ai.issue_overview",
                published.revision,
                SQL_POLICY_REVISION,
            )

        snapshot = operations.gateway_usage_report_snapshot()
        assert snapshot is not None
        assert {delta.metadata_revision for delta in snapshot.deltas} == {
            published.revision
        }
        assert sum(delta.query_count for delta in snapshot.deltas) == 2
        assert sum(delta.rejected_count for delta in snapshot.deltas) == 2
        assert all(
            (
                delta.success_count,
                delta.timeout_count,
                delta.overloaded_count,
                delta.cancelled_count,
                delta.failed_count,
                delta.queue_ms_sum,
                delta.elapsed_ms_sum,
                delta.returned_rows_sum,
                delta.result_bytes_sum,
                delta.truncated_count,
            )
            == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            for delta in snapshot.deltas
        )
        assert executor.calls == []
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_gateway_usage_excludes_failures_before_trusted_revision_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, metadata, _executor = query_service()
    operations.reset()
    try:
        with pytest.raises(SourceNotFoundError):
            await service.query(
                "unknown-source",
                "SELECT 1",
                "revision",
                SQL_POLICY_REVISION,
            )
        assert operations.gateway_usage_report_snapshot() is None

        async def fail_active_revision(_source_id: str) -> object:
            raise RuntimeError("bounded active revision failure")

        monkeypatch.setattr(metadata, "get_published", fail_active_revision)
        with pytest.raises(RuntimeError, match="active revision"):
            await service.query(
                "development-issues",
                "SELECT 1",
                "revision",
                SQL_POLICY_REVISION,
            )
        assert operations.gateway_usage_report_snapshot() is None
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_gateway_usage_recorder_failure_never_changes_query_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, metadata, _executor = query_service()
    published = await metadata.get_published("development-issues")
    operations.reset()

    def fail_recorder(**_values: object) -> None:
        raise RuntimeError("bounded recorder failure")

    try:
        monkeypatch.setattr(operations, "record_gateway_usage", fail_recorder)
        response = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert response["status"] == "ok"

        failed_service = QueryService(
            load_test_registry(),
            metadata,
            FailingExecutor(QueryOverloadedError()),
        )
        with pytest.raises(QueryOverloadedError):
            await failed_service.query(
                "development-issues",
                "SELECT count(*) FROM ai.issue_overview",
                published.revision,
                SQL_POLICY_REVISION,
            )
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_rejects_stale_revision_before_execution() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")
    old_metadata_revision = (
        "sha256:753f2d1e3f1e5f62de423e9180cb71dc2aed1869d5e4a9b5bd8da9955bad632b"
    )
    assert published.revision != old_metadata_revision

    with pytest.raises(MetadataRevisionMismatchError):
        await service.query(
            "development-issues",
            "SELECT 1",
            old_metadata_revision,
            SQL_POLICY_REVISION,
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_rejects_stale_sql_policy_revision_before_execution() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")
    old_sql_policy_revision = (
        "sha256:83729139d7ccedbe8e299b0c4a8bdefb97d42ca870d5fc3b9c227578c65855d9"
    )
    assert SQL_POLICY_REVISION != old_sql_policy_revision

    with pytest.raises(MetadataRevisionMismatchError):
        await service.query(
            "development-issues",
            "SELECT 1",
            published.revision,
            old_sql_policy_revision,
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
            errors.NumericValueOutOfRange("private numeric argument"),
            "QUERY_NUMERIC_VALUE_OUT_OF_RANGE",
        ),
        (
            errors.InvalidTextRepresentation("invalid input syntax for private type"),
            "QUERY_INVALID_CAST",
        ),
        (errors.InvalidDatetimeFormat("private date literal"), "QUERY_INVALID_CAST"),
        (errors.DatetimeFieldOverflow("private timestamp literal"), "QUERY_INVALID_CAST"),
        (errors.CannotCoerce("private source and target types"), "QUERY_INVALID_CAST"),
        (errors.DivisionByZero("private expression at character 42"), "QUERY_DIVISION_BY_ZERO"),
        (
            errors.InvalidRegularExpression("private regular expression"),
            "QUERY_INVALID_REGULAR_EXPRESSION",
        ),
        (
            errors.InvalidRowCountInLimitClause("private negative limit"),
            "QUERY_INVALID_LIMIT",
        ),
        (
            errors.InvalidRowCountInResultOffsetClause("private negative offset"),
            "QUERY_INVALID_LIMIT",
        ),
        (
            errors.InvalidParameterValue("private function argument"),
            "QUERY_INVALID_FUNCTION_ARGUMENT",
        ),
        (
            errors.WrongObjectType("private aggregate usage"),
            "QUERY_INVALID_FUNCTION_USAGE",
        ),
        (
            errors.UndefinedFunction("private function signature"),
            "QUERY_FUNCTION_SIGNATURE_MISMATCH",
        ),
    ],
)
async def test_executor_maps_correctable_database_errors_to_bounded_query_invalid(
    database_error: errors.DatabaseError,
    reason_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()
    connection = _PhaseConnection("explain", database_error)

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, "explain", database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
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
        assert caught.value.message == {
            "QUERY_DIVISION_BY_ZERO": (
                "The query can divide by zero. Guard the denominator with NULLIF or exclude "
                "zero values, then retry once."
            ),
            "QUERY_FUNCTION_SIGNATURE_MISMATCH": (
                "A function or operator call has unsupported argument types or count. Use a "
                "supported built-in signature and only advertised casts, then retry once."
            ),
            "QUERY_INVALID_CAST": (
                "The query casts a value to an incompatible type. Use an advertised compatible "
                "cast or filter invalid values, then retry once."
            ),
            "QUERY_INVALID_FUNCTION_ARGUMENT": (
                "A function argument has an invalid value. Correct the argument while preserving "
                "the requested calculation, then retry once."
            ),
            "QUERY_INVALID_FUNCTION_USAGE": (
                "An aggregate or window function is used in an unsupported form. Use its "
                "supported call form, then retry once."
            ),
            "QUERY_INVALID_LIMIT": (
                "LIMIT and OFFSET must use non-negative values. Correct them and retry once."
            ),
            "QUERY_INVALID_REGULAR_EXPRESSION": (
                "The regular expression is invalid. Correct its pattern or flags, then retry once."
            ),
            "QUERY_NUMERIC_VALUE_OUT_OF_RANGE": (
                "A numeric value is outside its supported range. For percentile fractions use a "
                "value from 0 through 1, then retry once."
            ),
            "QUERY_UNDEFINED_COLUMN": (
                "The query references a column PostgreSQL cannot resolve. Use a returned column "
                "sql_name or an alias declared in the query, then retry once."
            ),
        }[reason_code]
        assert caught.value.details == {
            "reason_code": reason_code,
            "action": "CORRECT_SQL",
            "retryable": True,
        }
        assert "private" not in str(caught.value)
        assert any(
            metric["name"] == "query_invalid"
            and metric.get("source_id") == source.source_id
            and metric["value"] == 1
            for metric in operations.snapshot()["metrics"]
        )
        assert connection.rolled_back
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cursor_execute", "cursor_fetch"])
async def test_executor_maps_result_cursor_database_errors_to_query_invalid(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("private function argument")
    connection = _PhaseConnection(phase, database_error)
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, phase, database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
    operations.reset()
    try:
        with pytest.raises(QueryInvalidError) as caught:
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert caught.value.details == {
            "reason_code": "QUERY_INVALID_FUNCTION_ARGUMENT",
            "action": "CORRECT_SQL",
            "retryable": True,
        }
        assert connection.rolled_back
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["timezone", "session", "reader_policy", "resolved_object", "commit"],
)
async def test_executor_keeps_internal_invalid_parameter_errors_unavailable(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("private internal setting")
    connection = _PhaseConnection(phase, database_error)
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, phase, database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
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
        assert connection.rolled_back
        assert not any(
            metric["name"] == "query_invalid"
            for metric in operations.snapshot()["metrics"]
        )
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_executor_applies_timezone_and_policy_before_planning_and_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("unused")
    connection = _PhaseConnection("success", database_error)
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(
        monkeypatch,
        "success",
        database_error,
        connection.events,
    )
    executor._get_pool = get_pool  # type: ignore[method-assign]
    operations.reset()
    try:
        result = await executor.execute(
            source,
            "SELECT 1",
            "test-revision",
            ValidatedSql("private-fingerprint", (), (), ()),
        )

        assert result["rows"] == []
        assert connection.events == [
            "begin",
            "timezone",
            "settings",
            "reader_policy",
            "resolved_object",
            "explain",
            "query",
            "commit",
        ]
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()
    connection = _PhaseConnection("explain", database_error)

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, "explain", database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
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
        assert connection.rolled_back
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
        with pytest.raises(QueryTimeoutError) as operator_cancelled:
            await pending
        assert type(operator_cancelled.value) is query_module._QueryCancelledTimeoutError
        assert operator_cancelled.value.code == "QUERY_TIMEOUT"

        wait_for_operator = False
        with pytest.raises(QueryTimeoutError) as timed_out:
            await executor.execute(
                source,
                "SELECT 1",
                "test-revision",
                validated,
            )
        assert type(timed_out.value) is QueryTimeoutError

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

    with pytest.raises(QueryTimeoutError) as active_cancelled:
        await active
    assert type(active_cancelled.value) is query_module._QueryCancelledTimeoutError
    assert active_cancelled.value.code == "QUERY_TIMEOUT"
    with pytest.raises(QueryUnavailableError) as queued_cancelled:
        await queued
    assert type(queued_cancelled.value) is query_module._QueryCancelledUnavailableError
    assert queued_cancelled.value.code == "QUERY_UNAVAILABLE"
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

        with pytest.raises(QueryUnavailableError) as queued_cancelled:
            await asyncio.wait_for(pending, 1)
        assert type(queued_cancelled.value) is query_module._QueryCancelledUnavailableError
        assert queued_cancelled.value.code == "QUERY_UNAVAILABLE"
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
