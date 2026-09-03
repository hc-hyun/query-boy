from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import query_man.metadata.catalog as catalog_module
from query_man.errors import MetadataUnavailableError
from query_man.metadata.catalog import PostgresCatalog, _apply_structures, _CatalogValidationError
from query_man.metadata.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogSnapshot,
    PreparedMetadata,
)
from query_man.metadata.service import MetadataService
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.reader_policy import (
    READER_CLIENT_ENCODING,
    READER_SESSION_TIMEZONE_SETTER,
    ReaderSessionPolicyError,
    require_reader_session_policy,
)
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import (
    column,
    load_test_registry,
    minimal_development_snapshot,
    relation,
)


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

def test_view_comment_contract_marker_is_parsed_and_not_disclosed() -> None:
    builders = catalog_module._rows_to_relations(
        [
            {
                "schema_name": "ai",
                "relation_name": "issue_overview",
                "relation_kind": "v",
                "relation_comment": (
                    "query-man:source=development-issues;view-contract=17\n"
                    "개발 문제 1건을 나타내는 공개 뷰"
                ),
                "view_definition_hash": "definition-digest",
                "security_invoker": True,
                "security_barrier": True,
                "estimated_rows": None,
                "ordinal": 1,
                "column_name": "issue_id",
                "data_type": "bigint",
                "is_not_null": True,
                "column_comment": None,
            }
        ]
    )

    relation = builders[0].freeze()

    assert relation.view_contract_source == "development-issues"
    assert relation.view_contract_version == 17
    assert relation.comment == "개발 문제 1건을 나타내는 공개 뷰"
    assert "query-man:" not in relation.comment
    assert relation.security_invoker is True
    assert relation.security_barrier is True


@pytest.mark.parametrize(
    "comment",
    [
        "human description only",
        "query-man:source=Development-Issues;view-contract=1",
        "query-man:source=development-issues;view-contract=0",
        "query-man:source=development-issues;view-contract=01",
        "query-man:source=development-issues;view-contract=1;extra=true",
        " query-man:source=development-issues;view-contract=1",
    ],
)
def test_view_comment_contract_marker_is_strict(comment: str) -> None:
    with pytest.raises(
        _CatalogValidationError,
        match="View comment has an invalid contract marker",
    ):
        catalog_module._parse_view_comment(comment)


@pytest.mark.parametrize(
    "comment",
    [
        "query-man:source=development-issues;view-contract=1",
        "query-man:source=development-issues;view-contract=1\n",
        "query-man:source=development-issues;view-contract=1\n   ",
    ],
)
def test_view_comment_requires_human_description(comment: str) -> None:
    with pytest.raises(
        _CatalogValidationError,
        match="View comment requires a human description",
    ):
        catalog_module._parse_view_comment(comment)


def test_catalog_query_collects_both_view_security_options() -> None:
    normalized_query = " ".join(catalog_module.CATALOG_QUERY.casefold().split())

    assert "security_invoker=true" in normalized_query
    assert "security_barrier=true" in normalized_query




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

    def accept_connection_policy(
        requested_connection: object,
        requested_sslmode: object,
    ) -> None:
        assert requested_connection is connection
        assert requested_sslmode == source.connection.sslmode
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


@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.asyncio
async def test_catalog_connection_policy_mismatch_closes_without_sql_or_rollback(
    monkeypatch: pytest.MonkeyPatch,
    close_fails: bool,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
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

    def reject_connection_policy(
        requested_connection: object,
        requested_sslmode: object,
    ) -> None:
        assert requested_connection is connection
        assert requested_sslmode == source.connection.sslmode
        raise marker

    catalog = PostgresCatalog()
    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        reject_connection_policy,
    )

    with pytest.raises(ReaderSessionPolicyError) as captured:
        await catalog.load(source)

    assert captured.value is marker
    assert connection.close_calls == 1
    assert connection.rollback_calls == 0
    assert connection.execute_calls == 0


@pytest.mark.asyncio
async def test_catalog_connection_info_failure_preserves_transient_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
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

    def fail_connection_policy(
        requested_connection: object,
        requested_sslmode: object,
    ) -> None:
        assert requested_connection is connection
        assert requested_sslmode == source.connection.sslmode
        raise failure

    catalog = PostgresCatalog()
    monkeypatch.setattr(catalog, "_get_pool", get_pool)
    monkeypatch.setattr(
        catalog_module,
        "require_reader_connection_policy",
        fail_connection_policy,
    )

    with pytest.raises(RuntimeError) as captured:
        await catalog.load(source)

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


@pytest.mark.parametrize("sslmode", ["disable", "require", "verify-full"])
@pytest.mark.asyncio
async def test_catalog_pool_passes_resolved_sslmode_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    sslmode: str,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        connection=replace(source.connection, sslmode=sslmode),
    )
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
    assert connection_kwargs["sslmode"] == sslmode
    assert connection_kwargs["gssencmode"] == "disable"


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

    def accept_connection_policy(
        requested_connection: object,
        requested_sslmode: object,
    ) -> None:
        assert requested_connection is connection
        assert requested_sslmode == source.connection.sslmode

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
                snapshot = minimal_development_snapshot()
                return replace(
                    snapshot,
                    relations=tuple(
                        replace(relation, comment="Description")
                        for relation in snapshot.relations
                    ),
                )
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
    await service.get_context(source.source_id)

    with pytest.raises(MetadataUnavailableError) as unavailable:
        await service.get_context(source.source_id)

    assert unavailable.value.__cause__ is not None
    assert isinstance(unavailable.value.__cause__, _CatalogValidationError)
    assert unavailable.value.__cause__.__cause__ is rollback_error
    assert provider.load_count == 2
    assert connection.rollback_attempted


@pytest.mark.asyncio
async def test_common_reader_policy_rejects_non_utc_timezone() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None

    class Cursor:
        async def fetchone(self) -> dict[str, object]:
            return {"utc_timezone": False}

    class Connection:
        def __init__(self) -> None:
            self.executions: list[str] = []

        async def execute(
            self,
            statement: str,
            _parameters: object | None = None,
        ) -> Cursor:
            self.executions.append(statement)
            return Cursor()

    connection = Connection()

    with pytest.raises(ReaderSessionPolicyError, match="session policy mismatch"):
        await require_reader_session_policy(connection, source)  # type: ignore[arg-type]

    assert "current_setting('TimeZone') = 'UTC'" in connection.executions[0]


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
