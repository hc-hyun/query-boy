from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo

from query_man.errors import (
    MetadataUnavailableError,
    QueryTimeoutError,
    QueryUnavailableError,
)
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    SQL_POLICY_REVISION,
    validate_sql,
)
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY

pytestmark = pytest.mark.integration

_FIXTURE_SOURCE_DIRECTORY = (
    ROOT_DIRECTORY / "tests" / "fixtures" / "config" / "sources"
)
_BUDGET_FILE = ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
_EXPECTED_COLUMNS = [
    "record_id",
    "small_value",
    "integer_value",
    "text_value",
    "date_value",
    "timestamp_value",
    "numeric_value",
]
_EXPECTED_OIDS = (20, 21, 23, 25, 1082, 1184, 1700)


def _fixture_source() -> SourceProfile:
    load_dotenv(ROOT_DIRECTORY / ".env")
    reader_password = os.environ.get("DEVELOPMENT_ISSUES_READER_PASSWORD")
    if not reader_password:
        pytest.skip("local fixture reader credentials are not configured")
    environment = dict(os.environ)
    environment["FIXTURE_SOURCE_READER_PASSWORD"] = reader_password
    environment["QUERY_MAN_POSTGRES_HOST"] = "127.0.0.1"
    environment["POSTGRES_PORT"] = os.environ.get("POSTGRES_PORT", "5432")
    registry = SourceRegistry.load(
        _FIXTURE_SOURCE_DIRECTORY,
        _BUDGET_FILE,
        environment,
    )
    source = registry.get("fixture-source")
    assert source is not None
    return source


def _connection_dsn(source: SourceProfile) -> str:
    return make_conninfo(
        host=source.connection.host,
        port=source.connection.port,
        dbname=source.connection.database,
        user=source.connection.user,
        password=source.connection.password,
        sslmode=source.connection.sslmode,
    )


async def _admin_connection() -> AsyncConnection[tuple[object, ...]]:
    load_dotenv(ROOT_DIRECTORY / ".env")
    if not os.environ.get("POSTGRES_USER") or not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("local PostgreSQL administrator credentials are not configured")
    return await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname="query_man",
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        ),
        autocommit=True,
    )


def _source_for_relation(source: SourceProfile, qualified_name: str) -> SourceProfile:
    schema, _name = qualified_name.split(".", 1)
    return replace(source, allowed_schemas=(schema,))


async def _create_test_view(
    admin: AsyncConnection[tuple[object, ...]],
    schema: str,
    *,
    domain_column: bool = False,
) -> None:
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    if domain_column:
        await admin.execute(
            sql.SQL("CREATE DOMAIN {}.positive_integer AS integer CHECK (VALUE > 0)").format(
                sql.Identifier(schema)
            )
        )
        await admin.execute(
            sql.SQL("CREATE VIEW {}.records AS SELECT 1::{}.positive_integer AS value").format(
                sql.Identifier(schema),
                sql.Identifier(schema),
            )
        )
        await admin.execute(
            sql.SQL("GRANT USAGE ON TYPE {}.positive_integer TO query_man_fixture_reader").format(
                sql.Identifier(schema)
            )
        )
    else:
        await admin.execute(
            sql.SQL("CREATE TABLE {}.base_records (record_id bigint, label text)").format(
                sql.Identifier(schema)
            )
        )
        await admin.execute(
            sql.SQL("INSERT INTO {}.base_records VALUES (1, 'alpha'), (2, 'beta')").format(
                sql.Identifier(schema)
            )
        )
        await admin.execute(
            sql.SQL(
                "CREATE VIEW {}.records WITH (security_barrier = true) "
                "AS SELECT record_id, label FROM {}.base_records"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
    await admin.execute(
        sql.SQL("COMMENT ON VIEW {}.records IS {}").format(
            sql.Identifier(schema),
            sql.Literal(
                "query-man:source=fixture-source;view-contract=1\n"
                "Temporary PostgreSQL safety-kernel view."
            ),
        )
    )
    await admin.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO query_man_fixture_reader").format(
            sql.Identifier(schema)
        )
    )
    await admin.execute(
        sql.SQL("GRANT SELECT ON {}.records TO query_man_fixture_reader").format(
            sql.Identifier(schema)
        )
    )


async def _drop_test_schema(
    admin: AsyncConnection[tuple[object, ...]], schema: str
) -> None:
    await admin.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
    )


@pytest.mark.asyncio
async def test_pg18_utf8_session_policy_and_database_write_denial() -> None:
    source = _fixture_source()
    reader = await AsyncConnection.connect(_connection_dsn(source))
    executor = PostgresQueryExecutor()
    try:
        assert 180_000 <= reader.info.server_version < 190_000
        assert reader.info.parameter_status("server_encoding") == "UTF8"
        assert reader.info.parameter_status("client_encoding") == "UTF8"
        assert reader.info.encoding == "utf-8"

        settings = await reader.execute(
            "SELECT pg_catalog.current_setting('default_transaction_read_only'), "
            "pg_catalog.current_setting('search_path')"
        )
        assert await settings.fetchone() == ("on", "pg_catalog")
        with pytest.raises(errors.DatabaseError) as denied:
            await reader.execute(
                "INSERT INTO fixture.fixture_records "
                "(record_id, small_value, integer_value, text_value, date_value, "
                "timestamp_value, numeric_value) VALUES "
                "(9, 9, 9, 'denied', DATE '2026-01-09', "
                "TIMESTAMPTZ '2026-01-09 00:00:00+00', 9.00)"
            )
        assert denied.value.sqlstate in {"25006", "42501"}
        await reader.rollback()
        recovered = await reader.execute("SELECT count(*) FROM ai.fixture_records")
        assert await recovered.fetchone() == (3,)

        policy_sql = (
            "SELECT record_id, "
            "pg_catalog.current_setting('transaction_read_only')::text AS read_only, "
            "pg_catalog.current_setting('transaction_isolation')::text AS isolation, "
            "pg_catalog.current_setting('TimeZone')::text AS timezone, "
            "pg_catalog.current_setting('search_path')::text AS search_path "
            "FROM ai.fixture_records ORDER BY record_id LIMIT 1"
        )
        validated = validate_sql(
            policy_sql,
            allowed_relations=("ai.fixture_records",),
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"current_setting"},
        )
        result = await executor.execute(source, policy_sql, "session-policy", validated)
        assert result["rows"] == [
            {
                "record_id": 1,
                "read_only": "on",
                "isolation": "repeatable read",
                "timezone": "UTC",
                "search_path": "pg_catalog",
            }
        ]
    finally:
        await reader.rollback()
        await reader.close()
        await executor.close()


@pytest.mark.asyncio
async def test_live_view_contract_and_base_privilege_boundary() -> None:
    source = _fixture_source()
    catalog = PostgresCatalog()
    reader = await AsyncConnection.connect(_connection_dsn(source), autocommit=True)
    try:
        snapshot = await catalog.load(source)
        assert len(snapshot.relations) == 1
        relation = snapshot.relations[0]
        assert relation.qualified_name == "ai.fixture_records"
        assert relation.kind == "view"
        assert relation.view_contract_source == "fixture-source"
        assert relation.view_contract_version == 1
        assert relation.definition_hash is not None
        assert len(relation.definition_hash) == 32
        assert relation.security_barrier is True
        assert [column.name for column in relation.columns] == _EXPECTED_COLUMNS

        privileges = await reader.execute(
            "SELECT "
            "pg_catalog.has_table_privilege(session_user, 'ai.fixture_records', 'SELECT'), "
            "(SELECT pg_catalog.has_table_privilege(session_user, base.oid, 'SELECT') "
            "FROM pg_catalog.pg_class AS base "
            "JOIN pg_catalog.pg_namespace AS base_namespace "
            "ON base_namespace.oid = base.relnamespace "
            "WHERE base_namespace.nspname = 'fixture' "
            "AND base.relname = 'fixture_records'), "
            "pg_catalog.has_schema_privilege(session_user, 'ai', 'CREATE'), "
            "owner.rolname "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner "
            "WHERE namespace.nspname = 'ai' AND relation.relname = 'fixture_records'"
        )
        assert await privileges.fetchone() == (
            True,
            False,
            False,
            "query_man_fixture_owner",
        )
        with pytest.raises(errors.InsufficientPrivilege):
            await reader.execute("SELECT * FROM fixture.fixture_records")
        visible = await reader.execute("SELECT count(*) FROM ai.fixture_records")
        assert await visible.fetchone() == (3,)
    finally:
        await reader.close()
        await catalog.close()


@pytest.mark.asyncio
async def test_live_view_definition_drift_fails_closed() -> None:
    source = _fixture_source()
    admin = await _admin_connection()
    schema = f"kernel_drift_{uuid.uuid4().hex}"
    catalog = PostgresCatalog()
    try:
        await _create_test_view(admin, schema)
        drift_source = _source_for_relation(source, f"{schema}.records")
        metadata = MetadataService(
            SourceRegistry([drift_source]),
            catalog,
            cache_ttl_ms=0,
            max_stale_ms=0,
        )
        original = await metadata.get_published(drift_source.source_id)
        assert original.snapshot.relations[0].view_contract_source == "fixture-source"
        assert original.snapshot.relations[0].definition_hash is not None

        await admin.execute(
            sql.SQL(
                "CREATE OR REPLACE VIEW {}.records WITH (security_barrier = true) "
                "AS SELECT record_id, label || '-drift'::text AS label "
                "FROM {}.base_records"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
        with pytest.raises(MetadataUnavailableError) as unavailable:
            await metadata.get_published(drift_source.source_id)
        assert unavailable.value.details == {
            "contract_violations": [
                "View structure changed without a view contract version change."
            ]
        }
    finally:
        await catalog.close()
        await _drop_test_schema(admin, schema)
        await admin.close()


@pytest.mark.asyncio
async def test_domain_output_is_rejected_during_metadata_admission() -> None:
    source = _fixture_source()
    admin = await _admin_connection()
    schema = f"kernel_domain_{uuid.uuid4().hex}"
    catalog = PostgresCatalog()
    try:
        await _create_test_view(admin, schema, domain_column=True)
        domain_source = _source_for_relation(source, f"{schema}.records")
        metadata = MetadataService(SourceRegistry([domain_source]), catalog)
        with pytest.raises(MetadataUnavailableError) as unavailable:
            await metadata.get_published(domain_source.source_id)
        assert unavailable.value.details is None
        assert unavailable.value.__cause__ is not None
    finally:
        await catalog.close()
        await _drop_test_schema(admin, schema)
        await admin.close()


@pytest.mark.asyncio
async def test_exact_seven_result_oids_and_unsupported_oid_recovery() -> None:
    source = _fixture_source()
    registry = SourceRegistry([source])
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog)
    executor = PostgresQueryExecutor()
    query = QueryService(registry, metadata, executor)
    reader = await AsyncConnection.connect(_connection_dsn(source), autocommit=True)
    try:
        oid_cursor = await reader.execute(
            "SELECT record_id, small_value, integer_value, text_value, date_value, "
            "timestamp_value, numeric_value FROM ai.fixture_records ORDER BY record_id"
        )
        assert oid_cursor.description is not None
        assert tuple(column.type_code for column in oid_cursor.description) == _EXPECTED_OIDS

        published = await metadata.get_published(source.source_id)
        result = await query.query(
            source.source_id,
            "SELECT record_id, small_value, integer_value, text_value, date_value, "
            "timestamp_value, numeric_value FROM ai.fixture_records ORDER BY record_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert result["columns"] == _EXPECTED_COLUMNS
        assert result["rows"] == [
            {
                "record_id": 1,
                "small_value": 10,
                "integer_value": 100,
                "text_value": "alpha",
                "date_value": "2026-01-01",
                "timestamp_value": "2026-01-01T00:00:00+00:00",
                "numeric_value": "1.25",
            },
            {
                "record_id": 2,
                "small_value": 20,
                "integer_value": 200,
                "text_value": "beta",
                "date_value": "2026-01-02",
                "timestamp_value": "2026-01-02T00:00:00+00:00",
                "numeric_value": "2.50",
            },
            {
                "record_id": 3,
                "small_value": 30,
                "integer_value": 300,
                "text_value": "gamma",
                "date_value": "2026-01-03",
                "timestamp_value": "2026-01-03T00:00:00+00:00",
                "numeric_value": "3.75",
            },
        ]

        with pytest.raises(QueryUnavailableError) as unavailable:
            await query.query(
                source.source_id,
                "SELECT record_id = 1 AS unsupported_bool "
                "FROM ai.fixture_records ORDER BY record_id",
                published.revision,
                SQL_POLICY_REVISION,
            )
        assert unavailable.value.details is None

        recovered = await query.query(
            source.source_id,
            "SELECT record_id FROM ai.fixture_records ORDER BY record_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert recovered["rows"] == [
            {"record_id": 1},
            {"record_id": 2},
            {"record_id": 3},
        ]
    finally:
        await reader.close()
        await executor.close()
        await catalog.close()


@pytest.mark.asyncio
async def test_timeout_cancel_and_multibyte_limit_restore_pooled_connection() -> None:
    source = _fixture_source()
    catalog = PostgresCatalog()
    metadata = MetadataService(SourceRegistry([source]), catalog)
    executor = PostgresQueryExecutor()
    admin = await _admin_connection()
    slow_task: asyncio.Task[dict[str, object]] | None = None
    try:
        await admin.execute("ANALYZE fixture.fixture_records")
        published = await metadata.get_published(source.source_id)
        aliases = tuple(f"r{index}" for index in range(18))
        slow_sql = "SELECT count(*) AS total FROM " + ", ".join(
            f"ai.fixture_records AS {alias}" for alias in aliases
        )
        slow_validated = validate_sql(
            slow_sql,
            allowed_relations=("ai.fixture_records",),
        )
        unbounded_plan = replace(
            source.budget,
            max_plan_total_cost=2_147_483_647,
            max_plan_rows=2_147_483_647,
        )
        timeout_source = replace(
            source,
            budget=replace(
                unbounded_plan,
                query_statement_timeout_ms=1,
                query_transaction_timeout_ms=2_000,
            ),
        )
        with pytest.raises(QueryTimeoutError):
            await executor.execute(
                timeout_source,
                slow_sql,
                published.revision,
                slow_validated,
            )

        fast_sql = "SELECT record_id FROM ai.fixture_records ORDER BY record_id"
        fast_validated = validate_sql(
            fast_sql,
            allowed_relations=("ai.fixture_records",),
        )
        recovered = await executor.execute(
            source,
            fast_sql,
            published.revision,
            fast_validated,
        )
        assert recovered["rows"] == [
            {"record_id": 1},
            {"record_id": 2},
            {"record_id": 3},
        ]

        query_id = str(uuid.uuid4())
        cancel_source = replace(
            source,
            budget=replace(
                unbounded_plan,
                query_statement_timeout_ms=10_000,
                query_transaction_timeout_ms=12_000,
            ),
        )
        slow_task = asyncio.create_task(
            executor.execute(
                cancel_source,
                slow_sql,
                published.revision,
                slow_validated,
                query_id=query_id,
            )
        )
        for _ in range(200):
            activity = await admin.execute(
                "SELECT state FROM pg_catalog.pg_stat_activity "
                "WHERE application_name = %s",
                (f"query-man:{query_id}",),
            )
            row = await activity.fetchone()
            if row == ("active",):
                break
            if slow_task.done():
                await slow_task
            await asyncio.sleep(0.01)
        else:
            pytest.fail("slow query did not become active")
        assert await executor.cancel(query_id)
        with pytest.raises(QueryTimeoutError):
            await slow_task
        slow_task = None

        recovered_after_cancel = await executor.execute(
            source,
            fast_sql,
            published.revision,
            fast_validated,
        )
        assert recovered_after_cancel["row_count"] == 3

        first_row = {"record_id": 1, "text_value": "한글🙂"}
        first_row_bytes = len(
            json.dumps(
                first_row,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        limited_source = replace(
            source,
            budget=replace(source.budget, max_result_bytes=2 + first_row_bytes),
        )
        multibyte_sql = (
            "SELECT record_id, '한글🙂'::text AS text_value "
            "FROM ai.fixture_records ORDER BY record_id"
        )
        multibyte = await executor.execute(
            limited_source,
            multibyte_sql,
            published.revision,
            validate_sql(
                multibyte_sql,
                allowed_relations=("ai.fixture_records",),
            ),
        )
        assert multibyte["rows"] == [first_row]
        assert multibyte["result_bytes"] == 2 + first_row_bytes
        assert multibyte["truncated"] is True
    finally:
        if slow_task is not None:
            if not slow_task.done():
                slow_task.cancel()
            await asyncio.gather(slow_task, return_exceptions=True)
        await admin.close()
        await executor.close()
        await catalog.close()
