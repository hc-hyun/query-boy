from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from typing import get_type_hints

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

import query_man.metadata.catalog as catalog_module
from query_man.errors import MetadataUnavailableError
from query_man.metadata.catalog import PostgresCatalog, _apply_structures, _CatalogValidationError
from query_man.metadata.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogProvider,
    CatalogSnapshot,
    PreparedMetadata,
    ResourceObservation,
    RuntimeCatalogProvider,
)
from query_man.metadata.service import MetadataService
from query_man.source_catalog.models import (
    RepresentativeRecordsTarget,
    ResourceObservationDefinition,
    SourceProfile,
)
from query_man.source_catalog.reader_policy import (
    READER_CLIENT_ENCODING,
    READER_SESSION_TIMEZONE_SETTER,
    ReaderSessionPolicyError,
    require_reader_session_policy,
)
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import (
    ROOT_DIRECTORY,
    column,
    load_test_registry,
    minimal_development_snapshot,
    relation,
)


def test_runtime_catalog_provider_protocol_has_exact_lifecycle_shape() -> None:
    application_methods = {
        name
        for name, value in vars(CatalogProvider).items()
        if not name.startswith("_") and callable(value)
    }
    runtime_methods = {
        name
        for name, value in vars(RuntimeCatalogProvider).items()
        if not name.startswith("_") and callable(value)
    }

    assert application_methods == {"close", "load"}
    assert CatalogProvider in RuntimeCatalogProvider.__mro__
    assert runtime_methods == {"invalidate", "observe_resources"}
    assert get_type_hints(RuntimeCatalogProvider.invalidate) == {
        "source_id": str,
        "return": type(None),
    }
    assert inspect.iscoroutinefunction(RuntimeCatalogProvider.invalidate)
    assert get_type_hints(RuntimeCatalogProvider.observe_resources) == {
        "source": SourceProfile,
        "return": ResourceObservation,
    }
    assert inspect.iscoroutinefunction(RuntimeCatalogProvider.observe_resources)
    for method, names in (
        (RuntimeCatalogProvider.invalidate, ("self", "source_id")),
        (RuntimeCatalogProvider.observe_resources, ("self", "source")),
    ):
        parameters = tuple(inspect.signature(method).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in parameters
        )
    assert get_type_hints(MetadataService.__init__)["catalog"] is CatalogProvider


def test_static_launch_domain_guard_uses_declared_catalog_type_kind() -> None:
    normalized_query = " ".join(catalog_module.CATALOG_QUERY.casefold().split())
    assert "join pg_catalog.pg_type as type_row" in normalized_query
    assert "type_row.typtype::text as type_kind" in normalized_query

    catalog_module._require_supported_catalog_types([{"type_kind": "b"}])
    with pytest.raises(
        _CatalogValidationError,
        match="Catalog contains an unsupported domain column",
    ):
        catalog_module._require_supported_catalog_types([{"type_kind": "d"}])

    assert PostgresCatalog()._reject_domain_columns is False
    assert PostgresCatalog(reject_domain_columns=True)._reject_domain_columns is True


class _ResourceCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, object] | None:
        return self._row


class _ResourceConnection:
    def __init__(
        self,
        row: dict[str, object] | None,
        *,
        query_error: Exception | None = None,
        settings_error: Exception | None = None,
        timezone_error: Exception | None = None,
    ) -> None:
        self._row = row
        self._query_error = query_error
        self._settings_error = settings_error
        self._timezone_error = timezone_error
        self.executions: list[tuple[str, object | None]] = []
        self.events: list[str] = []
        self.rolled_back = False

    async def execute(
        self,
        statement: str,
        parameters: object | None = None,
    ) -> object:
        self.executions.append((statement, parameters))
        self.events.append(statement)
        if (
            statement == READER_SESSION_TIMEZONE_SETTER
            and self._timezone_error is not None
        ):
            raise self._timezone_error
        if (
            statement == catalog_module._CATALOG_SESSION_SETTINGS
            and self._settings_error is not None
        ):
            raise self._settings_error
        if statement == catalog_module.RESOURCE_OBSERVATION_QUERY:
            if self._query_error is not None:
                raise self._query_error
            return _ResourceCursor(self._row)
        if "current_setting('transaction_read_only')" in statement:
            return _ResourceCursor(self._row)
        return object()

    async def rollback(self) -> None:
        self.rolled_back = True


class _ResourceConnectionContext:
    def __init__(self, connection: _ResourceConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _ResourceConnection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        pass


class _ResourcePool:
    def __init__(self, connection: _ResourceConnection) -> None:
        self._connection = connection

    def connection(self) -> _ResourceConnectionContext:
        return _ResourceConnectionContext(self._connection)


def _source_with_observability(
    *,
    representative_relation: str = "development.issues",
    storage_relations: tuple[str, ...] = (
        "development.users",
        "development.issues",
    ),
    environment: Mapping[str, str] | None = None,
) -> SourceProfile:
    registry = load_test_registry() if environment is None else load_test_registry(environment)
    source = registry.get("development-issues")
    assert source is not None
    return replace(
        source,
        observability=ResourceObservationDefinition(
            representative_records=RepresentativeRecordsTarget(
                grain="development_issue",
                physical_relation=representative_relation,
            ),
            storage_relations=storage_relations,
        ),
    )


def _catalog_with_resource_connection(
    monkeypatch: pytest.MonkeyPatch,
    source: SourceProfile,
    connection: _ResourceConnection,
) -> PostgresCatalog:
    catalog = PostgresCatalog()

    async def get_pool(requested_source: SourceProfile) -> _ResourcePool:
        assert requested_source is source
        return _ResourcePool(connection)

    async def accept_reader_policy(
        _connection: object,
        requested_source: SourceProfile,
    ) -> None:
        assert _connection is connection
        assert requested_source is source
        connection.events.append("session-policy")

    def accept_connection_policy(_connection: object) -> None:
        assert _connection is connection
        connection.events.append("connection-policy")

    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_session_policy",
        accept_reader_policy,
    )
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        accept_connection_policy,
    )
    return catalog


@pytest.mark.asyncio
async def test_catalog_load_checks_connection_before_existing_transaction_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    events: list[str] = []

    class Cursor:
        async def fetchall(self) -> list[dict[str, object]]:
            return []

    class Connection:
        rolled_back = False

        async def execute(
            self,
            statement: str,
            _parameters: object | None = None,
        ) -> object:
            events.append(statement)
            if statement in {
                catalog_module.CATALOG_QUERY,
                catalog_module.STRUCTURE_QUERY,
            }:
                return Cursor()
            return object()

        async def rollback(self) -> None:
            self.rolled_back = True

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class Pool:
        def connection(self) -> ConnectionContext:
            return ConnectionContext()

    async def get_pool(requested_source: SourceProfile) -> Pool:
        assert requested_source is source
        return Pool()

    def accept_connection_policy(requested_connection: object) -> None:
        assert requested_connection is connection
        events.append("connection-policy")

    async def accept_session_policy(
        requested_connection: object,
        requested_source: SourceProfile,
    ) -> None:
        assert requested_connection is connection
        assert requested_source is source
        events.append("session-policy")

    catalog = PostgresCatalog()
    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        accept_connection_policy,
    )
    monkeypatch.setattr(
        catalog_module,
        "require_reader_session_policy",
        accept_session_policy,
    )

    snapshot = await catalog.load(source)

    assert snapshot.relations == ()
    assert events == [
        "connection-policy",
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        READER_SESSION_TIMEZONE_SETTER,
        catalog_module._CATALOG_SESSION_SETTINGS,
        "session-policy",
        catalog_module.CATALOG_QUERY,
        catalog_module.STRUCTURE_QUERY,
        "COMMIT",
    ]
    assert not connection.rolled_back


@pytest.mark.parametrize("operation", ["load", "observe_resources"])
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.asyncio
async def test_catalog_connection_policy_mismatch_closes_without_sql_or_rollback(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    close_fails: bool,
) -> None:
    source = _source_with_observability()
    marker = ReaderSessionPolicyError("Source reader connection policy mismatch")

    class Connection:
        execute_calls = 0
        rollback_calls = 0
        close_calls = 0

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            self.execute_calls += 1
            return object()

        async def rollback(self) -> None:
            self.rollback_calls += 1

        async def close(self) -> None:
            self.close_calls += 1
            if close_fails:
                raise RuntimeError("private close failure")

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class Pool:
        def connection(self) -> ConnectionContext:
            return ConnectionContext()

    async def get_pool(requested_source: SourceProfile) -> Pool:
        assert requested_source is source
        return Pool()

    def reject_connection_policy(requested_connection: object) -> None:
        assert requested_connection is connection
        raise marker

    catalog = PostgresCatalog()
    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        reject_connection_policy,
    )

    with pytest.raises(ReaderSessionPolicyError) as captured:
        if operation == "load":
            await catalog.load(source)
        else:
            await catalog.observe_resources(source)

    assert captured.value is marker
    assert connection.close_calls == 1
    assert connection.rollback_calls == 0
    assert connection.execute_calls == 0


@pytest.mark.parametrize("operation", ["load", "observe_resources"])
@pytest.mark.asyncio
async def test_catalog_connection_info_failure_preserves_transient_exception(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = _source_with_observability()
    failure = RuntimeError("private connection info failure")

    class Connection:
        execute_calls = 0
        rollback_calls = 0
        close_calls = 0

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            self.execute_calls += 1
            return object()

        async def rollback(self) -> None:
            self.rollback_calls += 1

        async def close(self) -> None:
            self.close_calls += 1

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class Pool:
        def connection(self) -> ConnectionContext:
            return ConnectionContext()

    async def get_pool(requested_source: SourceProfile) -> Pool:
        assert requested_source is source
        return Pool()

    def fail_connection_policy(requested_connection: object) -> None:
        assert requested_connection is connection
        raise failure

    catalog = PostgresCatalog()
    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        fail_connection_policy,
    )

    with pytest.raises(RuntimeError) as captured:
        if operation == "load":
            await catalog.load(source)
        else:
            await catalog.observe_resources(source)

    assert captured.value is failure
    assert connection.close_calls == 0
    assert connection.rollback_calls == 0
    assert connection.execute_calls == 0


@pytest.mark.asyncio
async def test_catalog_pool_requests_approved_client_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    configuration: dict[str, object] = {}

    class Pool:
        def __init__(self, **values: object) -> None:
            configuration.update(values)

        async def open(self) -> None:
            pass

    monkeypatch.setattr(catalog_module, "AsyncConnectionPool", Pool)

    catalog = PostgresCatalog()
    await catalog._get_pool(source)

    connection_kwargs = configuration["kwargs"]
    assert isinstance(connection_kwargs, dict)
    assert connection_kwargs["client_encoding"] == READER_CLIENT_ENCODING


@pytest.mark.asyncio
async def test_resource_observation_uses_exact_bounded_catalog_query_without_new_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability()
    connection = _ResourceConnection(
        {
            "representative_records": 12.6,
            "table_bytes": 1_000,
            "index_bytes": 250,
            "total_storage_bytes": 1_250,
        }
    )
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    observation = await catalog.observe_resources(source)

    assert observation == ResourceObservation(
        representative_records=13,
        table_bytes=1_000,
        index_bytes=250,
        total_storage_bytes=1_250,
    )
    assert set(vars(observation)) == {
        "representative_records",
        "table_bytes",
        "index_bytes",
        "total_storage_bytes",
    }
    assert connection.executions[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        None,
    )
    assert connection.executions[1] == (READER_SESSION_TIMEZONE_SETTER, None)
    settings_statement, settings_parameters = connection.executions[2]
    assert settings_statement == catalog_module._CATALOG_SESSION_SETTINGS
    assert isinstance(settings_parameters, tuple)
    assert settings_parameters[:2] == (
        f"{source.budget.metadata_statement_timeout_ms}ms",
        f"{source.budget.lock_timeout_ms}ms",
    )
    assert connection.executions[3] == (
        catalog_module.RESOURCE_OBSERVATION_QUERY,
        (
            ["development", "development"],
            ["users", "issues"],
            2,
        ),
    )
    assert connection.executions[4] == ("COMMIT", None)
    assert not connection.rolled_back
    assert connection.events == [
        "connection-policy",
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        READER_SESSION_TIMEZONE_SETTER,
        catalog_module._CATALOG_SESSION_SETTINGS,
        "session-policy",
        catalog_module.RESOURCE_OBSERVATION_QUERY,
        "COMMIT",
    ]

    normalized_query = " ".join(
        catalog_module.RESOURCE_OBSERVATION_QUERY.casefold().split()
    )
    assert "pg_catalog.pg_class" in normalized_query
    assert "pg_catalog.pg_namespace" in normalized_query
    assert "relation.relkind in ('r', 'm')" in normalized_query
    assert "pg_catalog.has_schema_privilege" not in normalized_query
    assert "pg_catalog.has_table_privilege" not in normalized_query
    assert "pg_catalog.pg_table_size" in normalized_query
    assert "pg_catalog.pg_indexes_size" in normalized_query
    assert "pg_catalog.pg_total_relation_size" in normalized_query
    assert "count(" not in normalized_query
    assert "explain" not in normalized_query
    assert "development.issues" not in normalized_query


@pytest.mark.asyncio
async def test_catalog_limit_with_failed_rollback_never_serves_warm_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        budget=replace(source.budget, max_metadata_columns=1),
    )
    catalog = PostgresCatalog()
    rollback_error = RuntimeError("private rollback failure")

    class Cursor:
        async def fetchall(self) -> list[dict[str, object]]:
            return [{}, {}]

    class Connection:
        rollback_attempted = False

        async def execute(
            self,
            statement: str,
            parameters: object | None = None,
        ) -> Cursor:
            assert statement == catalog_module.CATALOG_QUERY
            assert parameters is not None
            return Cursor()

        async def rollback(self) -> None:
            self.rollback_attempted = True
            raise rollback_error

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            pass

    class Pool:
        def connection(self) -> ConnectionContext:
            return ConnectionContext()

    async def get_pool(requested_source: SourceProfile) -> Pool:
        assert requested_source is source
        return Pool()

    async def begin_catalog_transaction(
        requested_connection: object,
        requested_source: SourceProfile,
    ) -> None:
        assert requested_connection is connection
        assert requested_source is source

    def accept_connection_policy(requested_connection: object) -> None:
        assert requested_connection is connection

    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        accept_connection_policy,
    )
    monkeypatch.setattr(
        catalog_module,
        "_begin_catalog_transaction",
        begin_catalog_transaction,
    )

    class WarmThenPostgresCatalog:
        load_count = 0

        async def load(self, requested_source: SourceProfile) -> CatalogSnapshot:
            self.load_count += 1
            if self.load_count == 1:
                return minimal_development_snapshot()
            return await catalog.load(requested_source)

        async def close(self) -> None:
            pass

    provider = WarmThenPostgresCatalog()
    service = MetadataService(
        SourceRegistry([source]),
        provider,
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )
    await service.get_context(source.source_id, "최근 문제")

    with pytest.raises(MetadataUnavailableError) as unavailable:
        await service.get_context(source.source_id, "최근 문제")

    assert unavailable.value.__cause__ is not None
    assert isinstance(unavailable.value.__cause__, _CatalogValidationError)
    assert unavailable.value.__cause__.__cause__ is rollback_error
    assert provider.load_count == 2
    assert connection.rollback_attempted


@pytest.mark.asyncio
async def test_resource_observation_allows_absent_representative_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability(
        storage_relations=("development.issues",),
    )
    connection = _ResourceConnection(
        {
            "representative_records": None,
            "table_bytes": 800,
            "index_bytes": 200,
            "total_storage_bytes": 1_000,
        }
    )
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    observation = await catalog.observe_resources(source)

    assert observation.representative_records is None
    assert observation.table_bytes == 800
    assert connection.executions[-1] == ("COMMIT", None)


@pytest.mark.asyncio
async def test_resource_observation_accepts_sixteen_storage_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_relations = tuple(f"application.table_{index}" for index in range(16))
    source = _source_with_observability(
        representative_relation=storage_relations[-1],
        storage_relations=storage_relations,
    )
    connection = _ResourceConnection(
        {
            "representative_records": 1,
            "table_bytes": 16,
            "index_bytes": 0,
            "total_storage_bytes": 16,
        }
    )
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    await catalog.observe_resources(source)

    query_parameters = connection.executions[3][1]
    assert isinstance(query_parameters, tuple)
    assert query_parameters == (
        ["application"] * 16,
        [f"table_{index}" for index in range(16)],
        16,
    )


@pytest.mark.parametrize(
    ("representative_relation", "storage_relations"),
    [
        ("application.events", ()),
        (
            "application.table_0",
            tuple(f"application.table_{index}" for index in range(17)),
        ),
        (
            "application.events",
            ("application.events", "application.events"),
        ),
        ("application.missing", ("application.events",)),
        ("pg_catalog.pg_class", ("pg_catalog.pg_class",)),
        (
            "application.events",
            ("application.events", "information_schema.tables"),
        ),
        ("application.events.extra", ("application.events.extra",)),
    ],
)
@pytest.mark.asyncio
async def test_resource_observation_rejects_invalid_definition_before_db_access(
    monkeypatch: pytest.MonkeyPatch,
    representative_relation: str,
    storage_relations: tuple[str, ...],
) -> None:
    source = _source_with_observability(
        representative_relation=representative_relation,
        storage_relations=storage_relations,
    )
    catalog = PostgresCatalog()

    async def unexpected_pool(_source: SourceProfile) -> object:
        raise AssertionError("invalid resource definition reached the database")

    monkeypatch.setattr(catalog, "_get_pool", unexpected_pool)

    with pytest.raises(RuntimeError, match="definition is invalid"):
        await catalog.observe_resources(source)


@pytest.mark.asyncio
async def test_resource_observation_rejects_unconfigured_source_before_db_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = replace(_source_with_observability(), observability=None)
    catalog = PostgresCatalog()

    async def unexpected_pool(_source: SourceProfile) -> object:
        raise AssertionError("unconfigured resource observation reached the database")

    monkeypatch.setattr(catalog, "_get_pool", unexpected_pool)

    with pytest.raises(RuntimeError, match="not configured"):
        await catalog.observe_resources(source)


@pytest.mark.asyncio
async def test_resource_observation_rolls_back_unavailable_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability()
    connection = _ResourceConnection(None)
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    with pytest.raises(RuntimeError, match="targets are unavailable"):
        await catalog.observe_resources(source)

    assert connection.rolled_back
    assert not any(statement == "COMMIT" for statement, _ in connection.executions)


@pytest.mark.asyncio
async def test_resource_observation_rolls_back_storage_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability()
    failure = RuntimeError("private database failure")
    connection = _ResourceConnection(None, query_error=failure)
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    with pytest.raises(RuntimeError) as caught:
        await catalog.observe_resources(source)

    assert caught.value is failure
    assert connection.rolled_back
    assert not any(statement == "COMMIT" for statement, _ in connection.executions)


@pytest.mark.asyncio
async def test_resource_observation_rolls_back_session_budget_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability()
    connection = _ResourceConnection(
        None,
        settings_error=RuntimeError("private session failure"),
    )
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    with pytest.raises(
        ReaderSessionPolicyError,
        match="Source reader session budget could not be applied",
    ):
        await catalog.observe_resources(source)

    assert connection.rolled_back
    assert not any(
        statement == catalog_module.RESOURCE_OBSERVATION_QUERY
        for statement, _ in connection.executions
    )


@pytest.mark.asyncio
async def test_resource_observation_stops_after_timezone_setter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_observability()
    connection = _ResourceConnection(
        None,
        timezone_error=RuntimeError("private timezone failure"),
    )
    catalog = _catalog_with_resource_connection(monkeypatch, source, connection)

    with pytest.raises(
        ReaderSessionPolicyError,
        match="Source reader session budget could not be applied",
    ):
        await catalog.observe_resources(source)

    assert connection.rolled_back
    assert connection.executions == [
        ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", None),
        (READER_SESSION_TIMEZONE_SETTER, None),
    ]


@pytest.mark.asyncio
async def test_common_reader_policy_rejects_non_utc_timezone() -> None:
    source = _source_with_observability()
    connection = _ResourceConnection({"utc_timezone": False})

    with pytest.raises(ReaderSessionPolicyError, match="session policy mismatch"):
        await require_reader_session_policy(connection, source)  # type: ignore[arg-type]

    assert "current_setting('TimeZone') = 'UTC'" in connection.executions[0][0]


def test_published_catalog_graph_is_recursively_immutable_and_alias_free() -> None:
    base = relation("ai.example", [column("id")])
    columns = list(base.columns)
    primary_key = ["id"]
    foreign_key_columns = ["id"]
    referenced_columns = ["id"]
    index_columns = ["id"]
    foreign_keys = [
        CatalogForeignKey(
            foreign_key_columns,  # type: ignore[arg-type]
            "ai.example",
            referenced_columns,  # type: ignore[arg-type]
        )
    ]
    indexes = [
        CatalogIndex(index_columns, unique=True, primary=True)  # type: ignore[arg-type]
    ]
    published_relation = replace(  # type: ignore[arg-type]
        base,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        indexes=indexes,
    )
    relations = [published_relation]
    snapshot = CatalogSnapshot(relations)  # type: ignore[arg-type]
    prepared = PreparedMetadata(snapshot, f"sha256:{'0' * 64}")

    columns.append(column("mutated"))
    primary_key.append("mutated")
    foreign_key_columns.append("mutated")
    referenced_columns.append("mutated")
    index_columns.append("mutated")
    foreign_keys.clear()
    indexes.clear()
    relations.clear()

    assert isinstance(snapshot.relations, tuple)
    assert isinstance(published_relation.columns, tuple)
    assert published_relation.primary_key == ("id",)
    assert published_relation.foreign_keys[0].columns == ("id",)
    assert published_relation.foreign_keys[0].referenced_columns == ("id",)
    assert published_relation.indexes[0].columns == ("id",)
    assert prepared.snapshot is snapshot
    with pytest.raises(FrozenInstanceError):
        snapshot.relations = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        published_relation.comment = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        published_relation.columns[0].name = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        prepared.revision = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        published_relation.columns.append(column("mutated"))  # type: ignore[attr-defined]


def test_applies_primary_foreign_key_and_index_structures() -> None:
    relations = minimal_development_snapshot().relations
    relations = _apply_structures(
        relations,
        [
            {
                "structure_kind": "primary_key",
                "schema_name": "ai",
                "relation_name": "issue_overview",
                "column_names": ["issue_id"],
                "referenced_relation": None,
                "referenced_columns": None,
                "is_unique": None,
                "is_primary": True,
            },
            {
                "structure_kind": "foreign_key",
                "schema_name": "ai",
                "relation_name": "issue_comments",
                "column_names": ["issue_id"],
                "referenced_relation": "ai.issue_overview",
                "referenced_columns": ["issue_id"],
                "is_unique": None,
                "is_primary": False,
            },
            {
                "structure_kind": "index",
                "schema_name": "ai",
                "relation_name": "issue_overview",
                "column_names": ["discovered_at"],
                "referenced_relation": None,
                "referenced_columns": None,
                "is_unique": False,
                "is_primary": False,
            },
        ],
    )
    by_name = {relation.qualified_name: relation for relation in relations}
    assert by_name["ai.issue_overview"].primary_key == ("issue_id",)
    assert by_name["ai.issue_comments"].foreign_keys[0].referenced_relation == (
        "ai.issue_overview"
    )
    assert by_name["ai.issue_overview"].indexes[0].columns == ("discovered_at",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_catalog_collects_only_simple_visible_table_structures() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DEVELOPMENT_ISSUES_READER_PASSWORD",
        "MARKET_VOC_READER_PASSWORD",
    ]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL administrator credentials are not configured")
    source = load_test_registry(os.environ).get("development-issues")
    assert source is not None
    source = replace(
        source,
        allowed_schemas=("development",),
        allowed_relation_kinds=("table",),
    )
    admin = await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname="development_issues",
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        )
    )
    catalog = PostgresCatalog()
    try:
        await admin.execute(
            "GRANT USAGE ON SCHEMA development TO development_issues_reader"
        )
        await admin.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA development "
            "TO development_issues_reader"
        )
        await admin.commit()
        snapshot = await catalog.load(source)
    finally:
        await catalog.close()
        await admin.rollback()
        await admin.execute(
            "REVOKE SELECT ON ALL TABLES IN SCHEMA development "
            "FROM development_issues_reader"
        )
        await admin.execute(
            "REVOKE USAGE ON SCHEMA development FROM development_issues_reader"
        )
        await admin.commit()
        await admin.close()

    by_name = {relation.qualified_name: relation for relation in snapshot.relations}
    issues = by_name["development.issues"]
    assert issues.primary_key == ("id",)
    assert {
        (tuple(key.columns), key.referenced_relation, tuple(key.referenced_columns))
        for key in issues.foreign_keys
    } == {
        (("assignee_id",), "development.users", ("id",)),
        (("reporter_id",), "development.users", ("id",)),
        (("test_unit_id",), "development.test_units", ("id",)),
    }
    index_columns = {tuple(index.columns) for index in issues.indexes}
    assert ("id",) in index_columns
    assert ("discovered_at",) in index_columns
    assert ("status", "discovered_at") in index_columns
    assert ("assignee_id", "status") not in index_columns


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_resource_observation_needs_no_physical_table_reader_grant() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DEVELOPMENT_ISSUES_READER_PASSWORD",
        "MARKET_VOC_READER_PASSWORD",
    ]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL administrator credentials are not configured")
    source = _source_with_observability(
        storage_relations=("development.issues",),
        environment=os.environ,
    )
    admin = await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname="development_issues",
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        )
    )
    catalog = PostgresCatalog()
    try:
        privileges = await admin.execute(
            "SELECT "
            "has_schema_privilege('development_issues_reader', 'development', 'USAGE'), "
            "has_table_privilege("
            "'development_issues_reader', 'development.issues', 'SELECT'"
            ")"
        )
        privilege_row = await privileges.fetchone()
        assert privilege_row == (False, False)

        observation = await catalog.observe_resources(source)
    finally:
        await catalog.close()
        await admin.close()

    assert observation.representative_records is None or observation.representative_records >= 0
    assert observation.table_bytes > 0
    assert observation.index_bytes >= 0
    assert observation.total_storage_bytes >= observation.table_bytes
    assert observation.total_storage_bytes >= observation.index_bytes
