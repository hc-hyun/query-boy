from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

import pytest
from psycopg import errors

import query_man.guarded_query.query as query_module
from query_man.errors import (
    MetadataRevisionMismatchError,
    MetadataUnavailableError,
    QueryInvalidError,
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
)
from query_man.guarded_query.query import (
    PlanSummary,
    PostgresQueryExecutor,
    QueryService,
    _summarize_plan,
)
from query_man.guarded_query.result_encoding import ResultEncodingError
from query_man.guarded_query.sql_validation import SQL_POLICY_REVISION, ValidatedSql
from query_man.metadata.models import CatalogSnapshot
from query_man.metadata.service import MetadataService
from query_man.runtime.operations import operations
from query_man.source_catalog.models import SourceProfile, SSLMode
from query_man.source_catalog.reader_policy import ReaderSessionPolicyError
from tests.helpers import load_test_registry, minimal_development_snapshot


class StaticCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        snapshot = minimal_development_snapshot()
        return replace(
            snapshot,
            relations=tuple(
                replace(relation, comment=f"Description for {relation.qualified_name}")
                for relation in snapshot.relations
            ),
        )

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


class _ConnectionInfo:
    def __init__(
        self,
        *,
        server_version: int = 180_006,
        server_encoding: str | None = "UTF8",
        client_encoding: str | None = "UTF8",
        encoding: str = "utf-8",
    ) -> None:
        self.server_version = server_version
        self.encoding = encoding
        self._parameters = {
            "server_encoding": server_encoding,
            "client_encoding": client_encoding,
        }

    def parameter_status(self, name: str) -> str | None:
        return self._parameters.get(name)


class _PGConnection:
    ssl_in_use = False


@dataclass(frozen=True)
class _ResultColumn:
    name: str
    type_code: object


class _ResultCursor:
    def __init__(
        self,
        phase: str,
        database_error: errors.DatabaseError,
        events: list[str],
        description: tuple[object, ...],
    ) -> None:
        self._phase = phase
        self._database_error = database_error
        self._events = events
        self.description = description
        self.fetch_calls = 0
        self.closed = False

    async def execute(self, _sql: str) -> None:
        self._events.append("query")
        if self._phase == "cursor_execute":
            raise self._database_error

    async def fetchmany(self, _size: int) -> list[object]:
        self.fetch_calls += 1
        if self._phase == "cursor_fetch":
            raise self._database_error
        return []

    async def close(self) -> None:
        self.closed = True


class _PhaseConnection:
    def __init__(
        self,
        phase: str,
        database_error: errors.DatabaseError,
        *,
        description: tuple[object, ...] | None = None,
        info: _ConnectionInfo | None = None,
    ) -> None:
        self.phase = phase
        self.database_error = database_error
        self.rolled_back = False
        self.closed = False
        self.events: list[str] = []
        self.info = info or _ConnectionInfo()
        self.pgconn = _PGConnection()
        self.result_description = (_ResultColumn("value", 20),) if description is None else description
        self.result_cursor: _ResultCursor | None = None
        self.context_exit_error: BaseException | None = None

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
        if self.phase == "timezone" and statement == query_module.READER_SESSION_TIMEZONE_SETTER:
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
        self.result_cursor = _ResultCursor(
            self.phase,
            self.database_error,
            self.events,
            self.result_description,
        )
        return self.result_cursor

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True
        self.events.append("close")


class _ConnectionContext:
    def __init__(self, connection: _PhaseConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _PhaseConnection:
        return self._connection

    async def __aexit__(
        self,
        _exception_type: object,
        exception: BaseException | None,
        _traceback: object,
    ) -> None:
        self._connection.context_exit_error = exception


class _PhasePool:
    def __init__(self, connection: _PhaseConnection) -> None:
        self._connection = connection

    def connection(self, *, timeout: float) -> _ConnectionContext:
        assert timeout > 0
        return _ConnectionContext(self._connection)


def _record_query_health(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        operations,
        "set_source_query_health",
        lambda source_id, status: events.append((source_id, status)),
    )
    return events


def _stub_internal_query_checks(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    database_error: errors.DatabaseError,
    events: list[str] | None = None,
) -> None:
    original_connection_policy = query_module.require_reader_connection_policy

    def require_connection_policy(
        connection: object,
        sslmode: SSLMode,
    ) -> None:
        if events is not None:
            events.append("connection_policy")
        original_connection_policy(connection, sslmode)  # type: ignore[arg-type]

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

    monkeypatch.setattr(
        query_module,
        "require_reader_connection_policy",
        require_connection_policy,
    )
    monkeypatch.setattr(query_module, "require_reader_session_policy", require_reader_policy)
    monkeypatch.setattr(query_module, "_validate_resolved_objects", validate_resolved_objects)


def query_service() -> tuple[QueryService, MetadataService, RecordingExecutor]:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = RecordingExecutor()
    return (
        QueryService(registry, metadata, executor),
        metadata,
        executor,
    )


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
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")
    old_metadata_revision = "sha256:753f2d1e3f1e5f62de423e9180cb71dc2aed1869d5e4a9b5bd8da9955bad632b"
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
    old_sql_policy_revision = "sha256:6b68458319a21416e51bf4be059fc55c4e053b45e38e7219956c4ac3725637a6"
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
async def test_query_service_maps_reader_policy_metadata_failure_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = RecordingExecutor()
    service = QueryService(registry, metadata, executor)

    async def reject_metadata(_source_id: str) -> object:
        try:
            raise ReaderSessionPolicyError("private reader policy value")
        except ReaderSessionPolicyError as error:
            raise MetadataUnavailableError from error

    monkeypatch.setattr(metadata, "get_published", reject_metadata)

    with pytest.raises(QueryUnavailableError) as captured:
        await service.query(
            "development-issues",
            "SELECT count(*) FROM ai.issue_overview",
            f"sha256:{'0' * 64}",
            SQL_POLICY_REVISION,
        )

    assert captured.value.status_code == 503
    assert captured.value.code == "QUERY_UNAVAILABLE"
    assert captured.value.details is None
    assert captured.value.__cause__ is None
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
async def test_executor_maps_user_sql_error_to_bounded_query_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("private function argument")
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
        assert caught.value.details == {
            "reason_code": "QUERY_INVALID_FUNCTION_ARGUMENT",
            "action": "CORRECT_SQL",
            "retryable": True,
        }
        assert "private" not in str(caught.value)
        assert any(
            metric["name"] == "query_invalid" and metric.get("source_id") == source.source_id and metric["value"] == 1
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
    ["session", "commit"],
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
        assert not any(metric["name"] == "query_invalid" for metric in operations.snapshot()["metrics"])
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_executor_discards_connection_policy_mismatch_before_begin_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("unused")
    mismatch = _PhaseConnection(
        "success",
        database_error,
        info=_ConnectionInfo(server_version=170_000),
    )
    recovered = _PhaseConnection("success", database_error)
    connections = [mismatch, recovered]
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connections.pop(0))

    _stub_internal_query_checks(monkeypatch, "success", database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
    health_events = _record_query_health(monkeypatch)
    operations.reset()
    try:
        with pytest.raises(QueryUnavailableError) as captured:
            await executor.execute(
                source,
                "SELECT 1",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert captured.value.status_code == 503
        assert captured.value.details is None
        assert isinstance(captured.value.__cause__, ReaderSessionPolicyError)
        assert mismatch.closed
        assert not mismatch.rolled_back
        assert mismatch.events == ["close"]
        assert mismatch.result_cursor is None
        assert mismatch.context_exit_error is captured.value.__cause__
        assert executor._active == {}

        result = await executor.execute(
            source,
            "SELECT 1",
            "test-revision",
            ValidatedSql("private-fingerprint", (), (), ()),
        )

        assert result["rows"] == []
        assert recovered.events[0] == "begin"
        assert recovered.events[-1] == "commit"
        assert health_events == [
            (source.source_id, "unavailable"),
            (source.source_id, "healthy"),
        ]
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_pool_timeout_is_unavailable_and_lowers_query_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()

    class TimeoutConnectionContext:
        async def __aenter__(self) -> object:
            raise query_module.PoolTimeout("private database endpoint")

        async def __aexit__(self, *_args: object) -> None:
            pass

    class TimeoutPool:
        def connection(self, *, timeout: float) -> TimeoutConnectionContext:
            assert timeout > 0
            return TimeoutConnectionContext()

    async def get_pool(_source: SourceProfile) -> TimeoutPool:
        return TimeoutPool()

    executor._get_pool = get_pool  # type: ignore[method-assign]
    health_events = _record_query_health(monkeypatch)
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
        assert isinstance(caught.value.__cause__, query_module.PoolTimeout)
        assert health_events == [(source.source_id, "unavailable")]
        assert not executor._semaphores[source.source_id].locked()
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_error",
    [
        query_module.PoolTimeout("private pool startup failure"),
        errors.OperationalError("private pool connection failure"),
        errors.InterfaceError("private pool lifecycle failure"),
    ],
)
async def test_pool_creation_dependency_errors_are_mapped_and_lower_query_health(
    database_error: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> object:
        raise database_error

    executor._get_pool = get_pool  # type: ignore[method-assign]
    health_events = _record_query_health(monkeypatch)
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
        assert caught.value.details is None
        assert caught.value.__cause__ is database_error
        assert health_events == [(source.source_id, "unavailable")]
        assert not executor._semaphores[source.source_id].locked()
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_error",
    [
        errors.OperationalError("private database endpoint"),
        errors.InterfaceError("private connection state"),
    ],
)
async def test_connection_errors_lower_query_health(
    database_error: errors.Error,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    connection = _PhaseConnection("session", database_error)  # type: ignore[arg-type]
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(
        monkeypatch,
        "success",
        errors.InvalidParameterValue("unused"),
    )
    executor._get_pool = get_pool  # type: ignore[method-assign]
    health_events = _record_query_health(monkeypatch)
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
        assert caught.value.details is None
        assert caught.value.__cause__ is database_error
        assert health_events == [(source.source_id, "unavailable")]
        assert connection.rolled_back
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (QueryInvalidError("QUERY_INVALID_CAST"), QueryInvalidError),
        (QueryRejectedError("QUERY_PLAN_COST_EXCEEDED"), QueryRejectedError),
        (ResultEncodingError("Unsupported PostgreSQL result type"), QueryUnavailableError),
        (errors.InsufficientPrivilege("private relation"), QueryUnavailableError),
        (TimeoutError(), QueryTimeoutError),
        (errors.QueryCanceled(), QueryTimeoutError),
    ],
)
async def test_caller_triggerable_failures_do_not_lower_query_health(
    failure: Exception,
    expected_error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    connection = _PhaseConnection(
        "success",
        errors.InvalidParameterValue("unused"),
    )
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    async def fail_execution(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise failure

    executor._get_pool = get_pool  # type: ignore[method-assign]
    executor._execute_connection = fail_execution  # type: ignore[method-assign]
    health_events = _record_query_health(monkeypatch)
    operations.reset()
    try:
        with pytest.raises(expected_error):
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert health_events == []
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_semaphore_admission_timeout_remains_overloaded_without_health_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        budget=replace(source.budget, query_queue_timeout_ms=1),
    )
    executor = PostgresQueryExecutor()
    executor._semaphores[source.source_id] = asyncio.Semaphore(0)
    health_events = _record_query_health(monkeypatch)
    operations.reset()
    try:
        with pytest.raises(QueryOverloadedError) as caught:
            await executor.execute(
                source,
                "SELECT 1",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert caught.value.status_code == 429
        assert caught.value.code == "QUERY_OVERLOADED"
        assert health_events == []
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
            "connection_policy",
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
async def test_executor_rejects_unsupported_result_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("unused")
    connection = _PhaseConnection(
        "success",
        database_error,
        description=(_ResultColumn("value", 16),),
    )
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, "success", database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
    operations.reset()
    try:
        with pytest.raises(QueryUnavailableError) as captured:
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert captured.value.status_code == 503
        assert captured.value.code == "QUERY_UNAVAILABLE"
        assert captured.value.details is None
        assert isinstance(captured.value.__cause__, ResultEncodingError)
        assert str(captured.value.__cause__) == "Unsupported PostgreSQL result type"
        assert connection.result_cursor is not None
        assert connection.result_cursor.fetch_calls == 0
        assert connection.result_cursor.closed
        assert connection.rolled_back
        assert "commit" not in connection.events
        assert not any(metric["name"] == "query_execution_succeeded" for metric in operations.snapshot()["metrics"])
    finally:
        await executor.close()
        operations.reset()


@pytest.mark.asyncio
async def test_executor_rejects_duplicate_result_column_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    database_error = errors.InvalidParameterValue("unused")
    connection = _PhaseConnection(
        "success",
        database_error,
        description=(
            _ResultColumn("duplicate", 20),
            _ResultColumn("duplicate", 23),
        ),
    )
    executor = PostgresQueryExecutor()

    async def get_pool(_source: SourceProfile) -> _PhasePool:
        return _PhasePool(connection)

    _stub_internal_query_checks(monkeypatch, "success", database_error)
    executor._get_pool = get_pool  # type: ignore[method-assign]
    try:
        with pytest.raises(QueryRejectedError) as captured:
            await executor.execute(
                source,
                "SELECT private_sql_literal",
                "test-revision",
                ValidatedSql("private-fingerprint", (), (), ()),
            )

        assert captured.value.details == {"reason_code": "QUERY_DUPLICATE_RESULT_COLUMN"}
        assert connection.result_cursor is not None
        assert connection.result_cursor.fetch_calls == 0
        assert connection.result_cursor.closed
        assert connection.rolled_back
    finally:
        await executor.close()


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
        assert not any(metric["name"] == "query_invalid" for metric in operations.snapshot()["metrics"])
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


@pytest.mark.parametrize("sslmode", ("disable", "require", "verify-full"))
@pytest.mark.asyncio
async def test_query_pool_requests_approved_connection_policy(
    monkeypatch: pytest.MonkeyPatch,
    sslmode: str,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        connection=replace(source.connection, sslmode=sslmode),
    )
    created: dict[str, object] = {}

    class FakePool:
        def __init__(self, **options: object) -> None:
            created.update(options)

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(query_module, "AsyncConnectionPool", FakePool)
    executor = PostgresQueryExecutor()
    try:
        await executor._get_pool(source)

        kwargs = created["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["client_encoding"] == "UTF8"
        assert kwargs["sslmode"] == sslmode
        assert kwargs["gssencmode"] == "disable"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_executor_distinguishes_operator_cancel_from_statement_timeout() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    executor = PostgresQueryExecutor()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    wait_for_operator = True

    class FakeConnection:
        info = _ConnectionInfo()
        pgconn = _PGConnection()

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
            (metric["name"], metric.get("source_id")): metric["value"] for metric in operations.snapshot()["metrics"]
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
        info = _ConnectionInfo()
        pgconn = _PGConnection()

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
    active = asyncio.create_task(executor.execute(source, "SELECT 1", "test-revision", validated))
    await started.wait()
    queued = asyncio.create_task(executor.execute(source, "SELECT 1", "test-revision", validated))
    for _attempt in range(100):
        if len(executor._inflight) == 2:
            break
        await asyncio.sleep(0)
    assert len(executor._inflight) == 2

    await executor.drain(0)

    with pytest.raises(QueryTimeoutError) as active_cancelled:
        await active
    assert active_cancelled.value.code == "QUERY_TIMEOUT"
    with pytest.raises(QueryUnavailableError) as queued_cancelled:
        await queued
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
        assert queued_cancelled.value.code == "QUERY_UNAVAILABLE"
        assert lease_released.is_set()
        assert executor._inflight == set()
        assert not executor._semaphores[source.source_id].locked()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        operations.reset()
