from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from psycopg import AsyncConnection, OperationalError, errors, sql
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
from query_man.source_catalog.reader_policy import reader_connection_kwargs
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import QUERY_CAVE_CONFIG_DIRECTORY

pytestmark = pytest.mark.integration

_QUERY_CAVE_SOURCE_DIRECTORY = (
    QUERY_CAVE_CONFIG_DIRECTORY / "sources"
)
_BUDGET_FILE = QUERY_CAVE_CONFIG_DIRECTORY / "budget-profiles.yaml"
_EXPECTED_COLUMNS = [
    "case_id",
    "priority",
    "response_code",
    "summary",
    "reported_on",
    "reported_at",
    "risk_score",
]
_EXPECTED_OIDS = (20, 21, 23, 25, 1082, 1184, 1700)


def _query_cave_source() -> SourceProfile:
    state_directory = os.environ.get("QUERY_CAVE_STATE_DIRECTORY")
    if not state_directory:
        pytest.skip("Query Cave is not running")
    environment = dict(os.environ)
    environment["QUERY_MAN_POSTGRES_HOST"] = "127.0.0.1"
    environment["QUERY_CAVE_POSTGRES_PORT"] = os.environ.get(
        "QUERY_CAVE_POSTGRES_PORT", "55432"
    )
    registry = SourceRegistry.load(
        _QUERY_CAVE_SOURCE_DIRECTORY,
        _BUDGET_FILE,
        QUERY_CAVE_CONFIG_DIRECTORY / "database-profiles.yaml",
        Path(state_directory) / "host",
        environment,
    )
    source = registry.get("query-cave")
    assert source is not None
    return source


def _connection_dsn(source: SourceProfile) -> str:
    return make_conninfo(**reader_connection_kwargs(source, "query-man-integration"))


async def _admin_connection() -> AsyncConnection[tuple[object, ...]]:
    state_directory = os.environ.get("QUERY_CAVE_STATE_DIRECTORY")
    if not state_directory:
        pytest.skip("Query Cave is not running")
    credentials = Path(state_directory) / "admin"
    return await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("QUERY_CAVE_POSTGRES_PORT", "55432"),
            dbname="query_cave",
            user="query_cave_admin",
            sslmode="verify-full",
            sslrootcert=str(credentials / "ca.crt"),
            sslcert=str(credentials / "admin.crt"),
            sslkey=str(credentials / "admin.key"),
        ),
        autocommit=True,
    )


def _source_for_relation(source: SourceProfile, qualified_name: str) -> SourceProfile:
    schema, _name = qualified_name.split(".", 1)
    return replace(source, allowed_schemas=(schema,))


@pytest.mark.parametrize(
    "probe",
    ["missing-certificate", "untrusted-ca", "unmapped-dn", "hostname-mismatch"],
)
@pytest.mark.asyncio
async def test_client_certificate_admission_fails_closed(probe: str) -> None:
    source = _query_cave_source()
    parameters = reader_connection_kwargs(source, "query-man-certificate-negative")
    state_directory = Path(os.environ["QUERY_CAVE_STATE_DIRECTORY"])
    probes = state_directory / "probes"
    if probe == "missing-certificate":
        parameters.pop("sslcert")
        parameters.pop("sslkey")
    elif probe == "untrusted-ca":
        parameters["sslcert"] = str(probes / "untrusted.crt")
        parameters["sslkey"] = str(probes / "untrusted.key")
    elif probe == "unmapped-dn":
        parameters["sslcert"] = str(probes / "unmapped.crt")
        parameters["sslkey"] = str(probes / "unmapped.key")
    else:
        parameters["host"] = "localhost"

    with pytest.raises(OperationalError):
        await AsyncConnection.connect(make_conninfo(**parameters))


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
            sql.SQL("GRANT USAGE ON TYPE {}.positive_integer TO query_cave_reader").format(
                sql.Identifier(schema)
            )
        )
    else:
        await admin.execute(
            sql.SQL("CREATE TABLE {}.base_records (case_id bigint, label text)").format(
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
                "AS SELECT case_id, label FROM {}.base_records"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
    await admin.execute(
        sql.SQL("COMMENT ON VIEW {}.records IS {}").format(
            sql.Identifier(schema),
            sql.Literal(
                "query-man:source=query-cave;view-contract=1\n"
                "Temporary PostgreSQL safety-kernel view."
            ),
        )
    )
    await admin.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO query_cave_reader").format(
            sql.Identifier(schema)
        )
    )
    await admin.execute(
        sql.SQL("GRANT SELECT ON {}.records TO query_cave_reader").format(
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
    source = _query_cave_source()
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
                "INSERT INTO gotham_schema.incidents_table "
                "(case_id, priority, response_code, summary, reported_on, "
                "reported_at, risk_score) VALUES "
                "(9, 9, 9, 'denied', DATE '2026-01-09', "
                "TIMESTAMPTZ '2026-01-09 00:00:00+00', 9.00)"
            )
        assert denied.value.sqlstate in {"25006", "42501"}
        await reader.rollback()
        recovered = await reader.execute(
            "SELECT count(*) FROM signal_schema.case_files_view"
        )
        assert await recovered.fetchone() == (3,)

        policy_sql = (
            "SELECT case_id, "
            "pg_catalog.current_setting('transaction_read_only')::text AS read_only, "
            "pg_catalog.current_setting('transaction_isolation')::text AS isolation, "
            "pg_catalog.current_setting('TimeZone')::text AS timezone, "
            "pg_catalog.current_setting('search_path')::text AS search_path "
            "FROM signal_schema.case_files_view ORDER BY case_id LIMIT 1"
        )
        validated = validate_sql(
            policy_sql,
            allowed_relations=("signal_schema.case_files_view",),
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"current_setting"},
        )
        result = await executor.execute(source, policy_sql, "session-policy", validated)
        assert result["rows"] == [
            {
                "case_id": 1,
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
    source = _query_cave_source()
    catalog = PostgresCatalog()
    reader = await AsyncConnection.connect(_connection_dsn(source), autocommit=True)
    try:
        snapshot = await catalog.load(source)
        assert len(snapshot.relations) == 1
        relation = snapshot.relations[0]
        assert relation.qualified_name == "signal_schema.case_files_view"
        assert relation.kind == "view"
        assert relation.view_contract_source == "query-cave"
        assert relation.view_contract_version == 1
        assert relation.definition_hash is not None
        assert len(relation.definition_hash) == 32
        assert relation.security_barrier is True
        assert [column.name for column in relation.columns] == _EXPECTED_COLUMNS

        privileges = await reader.execute(
            "SELECT "
            "pg_catalog.has_table_privilege(session_user, 'signal_schema.case_files_view', 'SELECT'), "
            "(SELECT pg_catalog.has_table_privilege(session_user, base.oid, 'SELECT') "
            "FROM pg_catalog.pg_class AS base "
            "JOIN pg_catalog.pg_namespace AS base_namespace "
            "ON base_namespace.oid = base.relnamespace "
            "WHERE base_namespace.nspname = 'gotham_schema' "
            "AND base.relname = 'incidents_table'), "
            "pg_catalog.has_schema_privilege(session_user, 'signal_schema', 'CREATE'), "
            "owner.rolname "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner "
            "WHERE namespace.nspname = 'signal_schema' "
            "AND relation.relname = 'case_files_view'"
        )
        assert await privileges.fetchone() == (
            True,
            False,
            False,
            "query_cave_view_owner",
        )
        with pytest.raises(errors.InsufficientPrivilege):
            await reader.execute("SELECT * FROM gotham_schema.incidents_table")
        visible = await reader.execute(
            "SELECT count(*) FROM signal_schema.case_files_view"
        )
        assert await visible.fetchone() == (3,)
    finally:
        await reader.close()
        await catalog.close()


@pytest.mark.asyncio
async def test_live_view_definition_drift_fails_closed() -> None:
    source = _query_cave_source()
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
        assert original.snapshot.relations[0].view_contract_source == "query-cave"
        assert original.snapshot.relations[0].definition_hash is not None

        await admin.execute(
            sql.SQL(
                "CREATE OR REPLACE VIEW {}.records WITH (security_barrier = true) "
                "AS SELECT case_id, label || '-drift'::text AS label "
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
    source = _query_cave_source()
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
    source = _query_cave_source()
    registry = SourceRegistry([source])
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog)
    executor = PostgresQueryExecutor()
    query = QueryService(registry, metadata, executor)
    reader = await AsyncConnection.connect(_connection_dsn(source), autocommit=True)
    try:
        oid_cursor = await reader.execute(
            "SELECT case_id, priority, response_code, summary, reported_on, "
            "reported_at, risk_score FROM signal_schema.case_files_view ORDER BY case_id"
        )
        assert oid_cursor.description is not None
        assert tuple(column.type_code for column in oid_cursor.description) == _EXPECTED_OIDS

        published = await metadata.get_published(source.source_id)
        result = await query.query(
            source.source_id,
            "SELECT case_id, priority, response_code, summary, reported_on, "
            "reported_at, risk_score FROM signal_schema.case_files_view ORDER BY case_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert result["columns"] == _EXPECTED_COLUMNS
        assert result["rows"] == [
            {
                "case_id": 1,
                "priority": 10,
                "response_code": 100,
                "summary": "Rooftop signal inspection",
                "reported_on": "2026-01-01",
                "reported_at": "2026-01-01T00:00:00+00:00",
                "risk_score": "1.25",
            },
            {
                "case_id": 2,
                "priority": 20,
                "response_code": 200,
                "summary": "Museum alarm review",
                "reported_on": "2026-01-02",
                "reported_at": "2026-01-02T00:00:00+00:00",
                "risk_score": "2.50",
            },
            {
                "case_id": 3,
                "priority": 30,
                "response_code": 300,
                "summary": "Harbor patrol report",
                "reported_on": "2026-01-03",
                "reported_at": "2026-01-03T00:00:00+00:00",
                "risk_score": "3.75",
            },
        ]

        with pytest.raises(QueryUnavailableError) as unavailable:
            await query.query(
                source.source_id,
                "SELECT case_id = 1 AS unsupported_bool "
                "FROM signal_schema.case_files_view ORDER BY case_id",
                published.revision,
                SQL_POLICY_REVISION,
            )
        assert unavailable.value.details is None

        recovered = await query.query(
            source.source_id,
            "SELECT case_id FROM signal_schema.case_files_view ORDER BY case_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert recovered["rows"] == [
            {"case_id": 1},
            {"case_id": 2},
            {"case_id": 3},
        ]
    finally:
        await reader.close()
        await executor.close()
        await catalog.close()


@pytest.mark.asyncio
async def test_timeout_task_cancel_and_multibyte_limit_restore_pooled_connection() -> None:
    source = _query_cave_source()
    catalog = PostgresCatalog()
    metadata = MetadataService(SourceRegistry([source]), catalog)
    executor = PostgresQueryExecutor()
    admin = await _admin_connection()
    slow_task: asyncio.Task[dict[str, object]] | None = None
    try:
        await admin.execute("ANALYZE gotham_schema.incidents_table")
        published = await metadata.get_published(source.source_id)
        aliases = tuple(f"r{index}" for index in range(18))
        slow_sql = "SELECT count(*) AS total FROM " + ", ".join(
            f"signal_schema.case_files_view AS {alias}" for alias in aliases
        )
        slow_validated = validate_sql(
            slow_sql,
            allowed_relations=("signal_schema.case_files_view",),
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

        fast_sql = "SELECT case_id FROM signal_schema.case_files_view ORDER BY case_id"
        fast_validated = validate_sql(
            fast_sql,
            allowed_relations=("signal_schema.case_files_view",),
        )
        recovered = await executor.execute(
            source,
            fast_sql,
            published.revision,
            fast_validated,
        )
        assert recovered["rows"] == [
            {"case_id": 1},
            {"case_id": 2},
            {"case_id": 3},
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
        slow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_task
        slow_task = None

        recovered_after_cancel = await executor.execute(
            source,
            fast_sql,
            published.revision,
            fast_validated,
        )
        assert recovered_after_cancel["row_count"] == 3

        first_row = {"case_id": 1, "summary": "한글🙂"}
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
            "SELECT case_id, '한글🙂'::text AS summary "
            "FROM signal_schema.case_files_view ORDER BY case_id"
        )
        multibyte = await executor.execute(
            limited_source,
            multibyte_sql,
            published.revision,
            validate_sql(
                multibyte_sql,
                allowed_relations=("signal_schema.case_files_view",),
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
