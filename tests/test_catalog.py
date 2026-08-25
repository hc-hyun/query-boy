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

import query_man.catalog as catalog_module
from query_man.catalog import PostgresCatalog, _apply_structures
from query_man.metadata import MetadataService
from query_man.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogProvider,
    CatalogSnapshot,
    PreparedMetadata,
    RepresentativeRecordsTarget,
    ResourceObservation,
    ResourceObservationDefinition,
    RuntimeCatalogProvider,
    SourceProfile,
)
from query_man.reader_policy import (
    READER_SESSION_TIMEZONE_SETTER,
    ReaderSessionPolicyError,
    require_reader_session_policy,
)
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
        self.rolled_back = False

    async def execute(
        self,
        statement: str,
        parameters: object | None = None,
    ) -> object:
        self.executions.append((statement, parameters))
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
        assert requested_source is source

    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_session_policy",
        accept_reader_policy,
    )
    return catalog


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
