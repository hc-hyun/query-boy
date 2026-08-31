from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from query_man.assurance.verified import VerifiedQueryRegistry, create_result_hash
from query_man.delivery.mcp_server import MCP_PROTOCOL_VERSION
from query_man.errors import (
    QueryInvalidError,
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
)
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    SQL_POLICY_REVISION,
    ValidatedSql,
    validate_sql,
)
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.models import CatalogSnapshot
from query_man.metadata.service import MetadataService
from query_man.runtime.composition import build_app
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import SafeJsonFormatter, operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.reader_policy import (
    ReaderSessionPolicyError,
    require_reader_session_policy,
)
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot

_SESSION_BUDGET_SELECT = """
  SELECT
    pg_catalog.pg_size_bytes(pg_catalog.current_setting('work_mem')) / 1024
      AS work_mem_kb,
    pg_catalog.pg_size_bytes(pg_catalog.current_setting('temp_file_limit')) / 1024
      AS temp_file_limit_kb,
    pg_catalog.current_setting('max_parallel_workers_per_gather')::integer
      AS max_parallel_workers_per_gather,
    pg_catalog.current_setting('jit')::pg_catalog.text AS jit_enabled,
    pg_catalog.current_setting('transaction_isolation') AS transaction_isolation,
    pg_catalog.current_setting('transaction_read_only')::pg_catalog.text
      AS transaction_read_only
"""


class BearerAuth(httpx2.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


@asynccontextmanager
async def _serve_test_app(app: FastAPI) -> AsyncIterator[str]:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        for _ in range(500):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server.force_exit = True
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task
        finally:
            server_socket.close()


class DisconnectCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return minimal_development_snapshot()

    async def close(self) -> None:
        pass

class DisconnectExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.accepting = True

    async def execute(
        self,
        _source: SourceProfile,
        _sql: str,
        _metadata_revision: str,
        _validated: ValidatedSql,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return {"query_id": query_id or "unreachable"}

    async def cancel(self, _query_id: str) -> bool:
        return False

    async def close(self) -> None:
        pass

    def stop_accepting(self) -> None:
        self.accepting = False

    async def drain(self, _grace_ms: int) -> None:
        assert self.accepting is False

@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_catalog_maps_golden_questions() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    service = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    cases = [
        (
            "development-issues",
            "최근 90일 동안 모델별 개발 문제 건수와 미해결 건수를 보여줘.",
            ["ai.issue_overview"],
        ),
        (
            "development-issues",
            "사용자별 등록 문제 수, 담당 문제 수와 작성 댓글 수를 비교해줘.",
            ["ai.issue_overview", "ai.issue_comments"],
        ),
        (
            "development-issues",
            "원인이 아직 입력되지 않은 Critical 또는 High 문제를 찾아줘.",
            ["ai.issue_overview"],
        ),
        (
            "development-issues",
            "HW/SW version 조합별로 가장 많이 발생한 문제 유형은 무엇인가?",
            ["ai.issue_overview"],
        ),
        (
            "market-voc",
            "모델별 기기 수, VOC 수와 기기당 VOC 수를 높은 순서로 보여줘.",
            ["ai.device_overview"],
        ),
        ("market-voc", "VOC가 한 번도 없는 기기는 몇 대인가?", ["ai.device_overview"]),
        ("market-voc", "NURI 세대별 힌지 VOC 수를 비교해줘.", ["ai.voc_overview"]),
        (
            "market-voc",
            "제조 lot별 전체 VOC 중 배터리 및 과열 VOC 비율을 비교해줘.",
            ["ai.voc_overview"],
        ),
        (
            "market-voc",
            "지역과 월별 미해결 VOC 추이를 보여줘.",
            ["ai.voc_overview"],
        ),
    ]
    try:
        for source_id, question, expected in cases:
            response = await service.get_context(source_id, question)
            assert [item["name"] for item in response["relations"]] == expected
            assert all(not item["primary_key"] for item in response["relations"])
            assert all(not item["foreign_keys"] for item in response["relations"])
            assert all(not item["indexes"] for item in response["relations"])
            assert str(response["metadata_revision"]).startswith("sha256:")
    finally:
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_verified_queries_match_revision_relations_and_results() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    verified = VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        {source["source_id"] for source in registry.list()},
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    try:
        results = await verified.verify_all(metadata, service)
        assert len(results) == 9
    finally:
        await executor.close()
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_source_is_quarantined_for_every_tenant_before_pool_access() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, tenant_isolation="rls")
    executor = PostgresQueryExecutor()

    pool_calls = 0

    async def unexpected_pool(_source: SourceProfile) -> object:
        nonlocal pool_calls
        pool_calls += 1
        raise AssertionError("RLS quarantine must precede database access")

    executor._get_pool = unexpected_pool  # type: ignore[method-assign]
    try:
        for tenant_id in (None, "engineering", "quality"):
            with pytest.raises(QueryUnavailableError) as unavailable:
                await executor.execute(
                    source,
                    "SELECT 1",
                    "rls-test-revision",
                    ValidatedSql("pg_query:rls-quarantine", (), (), ()),
                    tenant_id=tenant_id,
                )

            assert unavailable.value.status_code == 503
            assert unavailable.value.code == "QUERY_UNAVAILABLE"
            assert unavailable.value.details is None
            assert unavailable.value.__cause__ is None
        assert pool_calls == 0
        assert executor._pools == {}
        assert executor._semaphores == {}
        assert executor._inflight == set()
    finally:
        await executor.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closed_database_port_is_unavailable_and_redacts_dependency_log() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None

    closed_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_socket.bind(("127.0.0.1", 0))
    closed_port = int(closed_socket.getsockname()[1])
    source = replace(
        source,
        connection=replace(
            source.connection,
            host="127.0.0.1",
            port=closed_port,
            database="leak-database-marker",
            user="leak-user-marker",
            password="leak-password-marker",
            sslmode="disable",
        ),
        budget=replace(
            source.budget,
            query_queue_timeout_ms=250,
            max_pool_size=1,
            max_concurrent_queries=1,
        ),
    )
    executor = PostgresQueryExecutor()
    log_stream = io.StringIO()
    log_handler = logging.StreamHandler(log_stream)
    log_handler.setFormatter(SafeJsonFormatter())
    dependency_logger = logging.getLogger("psycopg.pool")
    previous_handlers = dependency_logger.handlers[:]
    previous_level = dependency_logger.level
    previous_propagate = dependency_logger.propagate
    previous_disabled = dependency_logger.disabled
    dependency_logger.handlers = [log_handler]
    dependency_logger.setLevel(logging.WARNING)
    dependency_logger.propagate = False
    dependency_logger.disabled = False

    operations.reset()
    operations.reconcile_sources([source.source_id])
    operations.set_source_health(source.source_id, "healthy")
    operations.set_source_query_health(source.source_id, "healthy")
    try:
        assert operations.public_status() == "ready"

        with pytest.raises(QueryUnavailableError) as caught:
            await executor.execute(
                source,
                "SELECT 1",
                "closed-port-revision",
                ValidatedSql("pg_query:closed-port", (), (), ()),
            )

        assert caught.value.status_code == 503
        assert caught.value.code == "QUERY_UNAVAILABLE"
        assert caught.value.message == "The query could not be completed."
        assert caught.value.details is None
        assert isinstance(caught.value.__cause__, PoolTimeout)
        assert operations.snapshot()["sources"] == {source.source_id: "unavailable"}
        assert operations.public_status() == "unavailable"
        assert not executor._semaphores[source.source_id].locked()
    finally:
        try:
            await executor.close()
        finally:
            closed_socket.close()
            log_handler.flush()
            dependency_logger.handlers = previous_handlers
            dependency_logger.setLevel(previous_level)
            dependency_logger.propagate = previous_propagate
            dependency_logger.disabled = previous_disabled
            log_handler.close()
            operations.reset()

    assert executor._pools == {}
    assert executor._active == {}
    assert executor._inflight == set()
    log_payloads = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert log_payloads
    for payload in log_payloads:
        assert payload == {
            "event": "database_dependency_log",
            "level": "warning",
            "logger": "psycopg.pool",
            "timestamp": payload["timestamp"],
        }
    bounded_payloads = [
        {key: value for key, value in payload.items() if key != "timestamp"}
        for payload in log_payloads
    ]
    bounded_log = json.dumps(bounded_payloads)
    for sensitive in (
        "127.0.0.1",
        str(closed_port),
        "leak-database-marker",
        "leak-user-marker",
        "leak-password-marker",
        "connection refused",
        "connection failed",
        "error connecting",
    ):
        assert sensitive.casefold() not in bounded_log.casefold()


@pytest.mark.integration
def test_injected_rls_registry_is_rejected_before_app_provider_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    registry = SourceRegistry([replace(source, tenant_isolation="rls")])
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token=None,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=300_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )
    provider_calls = 0

    def unexpected_provider(*_args: object, **_kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("RLS launch rejection must precede provider composition")

    monkeypatch.setattr(
        "query_man.runtime.composition.PostgresCatalog",
        unexpected_provider,
    )
    monkeypatch.setattr(
        "query_man.runtime.composition.PostgresQueryExecutor",
        unexpected_provider,
    )

    with pytest.raises(
        ValueError,
        match="Runtime launch inventory does not accept RLS sources",
    ):
        build_app(runtime, registry=registry)

    assert provider_calls == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_catalog_and_query_enforce_versioned_session_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    expected_settings = {
        "work_mem_kb": source.budget.work_mem_kb,
        "temp_file_limit_kb": source.budget.temp_file_limit_kb,
        "max_parallel_workers_per_gather": (
            source.budget.max_parallel_workers_per_gather
        ),
        "jit_enabled": "on" if source.budget.jit_enabled else "off",
        "transaction_isolation": "repeatable read",
        "transaction_read_only": "on",
    }
    unsafe_settings = {
        "work_mem_kb": 32_768,
        "temp_file_limit_kb": 131_072,
        "max_parallel_workers_per_gather": 2,
        "jit_enabled": "on",
        "transaction_isolation": "read committed",
        "transaction_read_only": "on",
    }
    admin = await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=source.connection.database,
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        )
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    reader: AsyncConnection[dict[str, Any]] | None = None
    try:
        await admin.execute(
            "ALTER ROLE development_issues_reader IN DATABASE development_issues "
            "SET work_mem = '32MB'"
        )
        await admin.execute(
            "ALTER ROLE development_issues_reader IN DATABASE development_issues "
            "SET temp_file_limit = '128MB'"
        )
        await admin.execute(
            "ALTER ROLE development_issues_reader IN DATABASE development_issues "
            "SET max_parallel_workers_per_gather = 2"
        )
        await admin.execute(
            "ALTER ROLE development_issues_reader IN DATABASE development_issues "
            "SET jit = on"
        )
        await admin.commit()

        role_cursor = await admin.execute(
            "SELECT role.rolconnlimit AS connection_limit, "
            "pg_catalog.has_parameter_privilege("
            "role.rolname, 'temp_file_limit', 'SET') AS can_set_temp_limit "
            "FROM pg_catalog.pg_roles AS role "
            "WHERE role.rolname = 'development_issues_reader'"
        )
        role_policy = await role_cursor.fetchone()
        assert role_policy == (7, True)

        reader = await AsyncConnection.connect(
            make_conninfo(
                host=source.connection.host,
                port=source.connection.port,
                dbname=source.connection.database,
                user=source.connection.user,
                password=source.connection.password,
                sslmode="disable",
            ),
            autocommit=True,
            row_factory=dict_row,
        )
        drift_cursor = await reader.execute(_SESSION_BUDGET_SELECT)
        assert await drift_cursor.fetchone() == unsafe_settings
        await reader.execute(
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        with pytest.raises(ReaderSessionPolicyError):
            await require_reader_session_policy(reader, source)
        await reader.execute("ROLLBACK")
        await reader.close()
        reader = None

        catalog_settings: list[dict[str, Any]] = []

        async def record_catalog_settings(
            connection: AsyncConnection[Any],
            profile: SourceProfile,
        ) -> None:
            settings_cursor = await connection.execute(_SESSION_BUDGET_SELECT)
            settings = await settings_cursor.fetchone()
            assert settings is not None
            catalog_settings.append(settings)
            await require_reader_session_policy(connection, profile)

        monkeypatch.setattr(
            "query_man.metadata.catalog.require_reader_session_policy",
            record_catalog_settings,
        )
        snapshot = await catalog.load(source)
        assert snapshot.relations
        assert catalog_settings == [expected_settings]

        query_sql = (
            f"{_SESSION_BUDGET_SELECT} "
            "FROM ai.issue_overview ORDER BY issue_id LIMIT 1"
        )
        validated = validate_sql(
            query_sql,
            allowed_relations=["ai.issue_overview"],
            allowed_functions=(
                DEFAULT_ALLOWED_FUNCTIONS | {"current_setting", "pg_size_bytes"}
            ),
        )
        result = await executor.execute(
            source,
            query_sql,
            "session-budget-revision",
            validated,
        )
        assert result["rows"] == [expected_settings]
    finally:
        try:
            if reader is not None:
                await reader.close()
            await executor.close()
            await catalog.close()
        finally:
            await admin.rollback()
            await admin.execute(
                "ALTER ROLE development_issues_reader IN DATABASE development_issues "
                "SET work_mem = '8MB'"
            )
            await admin.execute(
                "ALTER ROLE development_issues_reader IN DATABASE development_issues "
                "SET temp_file_limit = '64MB'"
            )
            await admin.execute(
                "ALTER ROLE development_issues_reader IN DATABASE development_issues "
                "SET max_parallel_workers_per_gather = 0"
            )
            await admin.execute(
                "ALTER ROLE development_issues_reader IN DATABASE development_issues "
                "SET jit = off"
            )
            await admin.commit()
            await admin.close()
@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_guarded_query_enforces_plan_and_result_limits() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    if not os.environ.get("DEVELOPMENT_ISSUES_READER_PASSWORD"):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    try:
        published = await metadata.get_published("development-issues")
        counted = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert counted["rows"] == [{"issue_count": 600}]
        assert counted["truncated"] is False
        assert counted["plan_summary"]["node_count"] > 0  # type: ignore[index]

        date_between = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview "
            "WHERE discovered_on BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'",
            published.revision,
            SQL_POLICY_REVISION,
        )
        explicit_comparisons = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview "
            "WHERE discovered_on >= DATE '2026-05-01' "
            "AND discovered_on <= DATE '2026-05-31'",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert date_between["rows"] == explicit_comparisons["rows"]

        analytics = await service.query(
            "development-issues",
            "SELECT issue_id, issue_id::text AS issue_id_text, "
            "CAST(discovered_at AS date) AS discovered_on, "
            "extract(year FROM discovered_at) AS discovered_year, "
            "rank() OVER (ORDER BY discovered_at, issue_id) AS discovered_rank, "
            "lag(issue_id) OVER (ORDER BY issue_id) AS previous_issue_id, "
            "lead(issue_id) OVER (ORDER BY issue_id) AS next_issue_id "
            "FROM ai.issue_overview ORDER BY issue_id LIMIT 3",
            published.revision,
            SQL_POLICY_REVISION,
        )
        analytics_rows = analytics["rows"]
        assert isinstance(analytics_rows, list)
        assert len(analytics_rows) == 3
        for index, row in enumerate(analytics_rows):
            assert isinstance(row, dict)
            assert row["issue_id_text"] == str(row["issue_id"])
            assert row["discovered_year"] == row["discovered_on"][:4]
            assert row["discovered_rank"] >= 1
            if index == 0:
                assert row["previous_issue_id"] is None
            else:
                assert row["previous_issue_id"] == analytics_rows[index - 1]["issue_id"]
            if index < len(analytics_rows) - 1:
                assert row["next_issue_id"] == analytics_rows[index + 1]["issue_id"]

        extended_analytics = await service.query(
            "development-issues",
            "WITH summary AS ("
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY comment_count) "
            "::pg_catalog.numeric AS median_comments FROM ai.issue_overview"
            "), samples AS ("
            "SELECT issue_id, issue_no, status, severity, title, "
            "dense_rank() OVER (ORDER BY severity) AS severity_rank "
            "FROM ai.issue_overview ORDER BY issue_id LIMIT 3"
            ") "
            "SELECT regexp_replace(title, '[0-9]+', '#', 'g') AS normalized_title, "
            "position('오류' IN title) AS error_position, "
            "jsonb_build_object('no', issue_no, 'status', status)::pg_catalog.text "
            "AS issue_json, to_jsonb(issue_no)::pg_catalog.text AS issue_no_json, "
            "severity_rank, median_comments "
            "FROM samples CROSS JOIN summary ORDER BY issue_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        extended_rows = extended_analytics["rows"]
        assert isinstance(extended_rows, list)
        assert len(extended_rows) == 3
        for row in extended_rows:
            assert isinstance(row, dict)
            assert isinstance(row["normalized_title"], str)
            assert row["error_position"] >= 0
            assert row["severity_rank"] >= 1
            assert isinstance(row["issue_json"], str)
            issue_json = json.loads(row["issue_json"])
            assert json.loads(row["issue_no_json"]) == issue_json["no"]
            assert isinstance(row["median_comments"], str)

        for invalid_sql, reason_code in [
            (
                "SELECT missing_column FROM ai.issue_overview",
                "QUERY_UNDEFINED_COLUMN",
            ),
            ("SELECT 'not-a-date'::date", "QUERY_INVALID_CAST"),
            ("SELECT 1 / 0", "QUERY_DIVISION_BY_ZERO"),
            (
                "SELECT issue_id FROM ai.issue_overview LIMIT -1",
                "QUERY_INVALID_LIMIT",
            ),
            (
                "SELECT regexp_replace(title, '[', '#', 'g') "
                "FROM ai.issue_overview LIMIT 1",
                "QUERY_INVALID_REGULAR_EXPRESSION",
            ),
            (
                "SELECT percentile_cont(1.1) WITHIN GROUP (ORDER BY comment_count) "
                "::pg_catalog.numeric FROM ai.issue_overview",
                "QUERY_NUMERIC_VALUE_OUT_OF_RANGE",
            ),
            (
                "SELECT jsonb_build_object(NULL, issue_no)::pg_catalog.text "
                "FROM ai.issue_overview LIMIT 1",
                "QUERY_INVALID_FUNCTION_ARGUMENT",
            ),
            (
                "SELECT dense_rank(1) OVER () FROM ai.issue_overview LIMIT 1",
                "QUERY_INVALID_FUNCTION_USAGE",
            ),
            ("SELECT to_jsonb()", "QUERY_FUNCTION_SIGNATURE_MISMATCH"),
        ]:
            with pytest.raises(QueryInvalidError) as invalid:
                await service.query(
                    "development-issues",
                    invalid_sql,
                    published.revision,
                    SQL_POLICY_REVISION,
                )
            assert invalid.value.details == {
                "reason_code": reason_code,
                "action": "CORRECT_SQL",
                "retryable": True,
            }
            assert "retry once" in invalid.value.message
            assert invalid_sql not in str(invalid.value)

        exact_numeric = await service.query(
            "development-issues",
            "SELECT 12345678901234567890.1234567890::pg_catalog.numeric "
            "AS exact_numeric FROM ai.issue_overview LIMIT 1",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert exact_numeric["rows"] == [
            {"exact_numeric": "12345678901234567890.1234567890"}
        ]
        assert exact_numeric["result_bytes"] == len(
            json.dumps(
                exact_numeric["rows"],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

        for unsupported_sql in (
            "SELECT jsonb_build_object('no', issue_no) AS unsupported_jsonb "
            "FROM ai.issue_overview LIMIT 1",
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY comment_count) "
            "AS unsupported_float8 FROM ai.issue_overview",
            "SELECT '\\xff00'::pg_catalog.bytea AS unsupported_bytea "
            "FROM ai.issue_overview LIMIT 1",
        ):
            with pytest.raises(QueryUnavailableError) as unavailable:
                await service.query(
                    "development-issues",
                    unsupported_sql,
                    published.revision,
                    SQL_POLICY_REVISION,
                )
            assert unavailable.value.status_code == 503
            assert unavailable.value.code == "QUERY_UNAVAILABLE"
            assert unavailable.value.message == "The query could not be completed."
            assert unavailable.value.details is None

        limited = await service.query(
            "development-issues",
            "SELECT * FROM ai.issue_comments ORDER BY comment_id",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert limited["row_count"] == 1000
        assert limited["truncated"] is True
        assert limited["result_bytes"] <= 1_048_576  # type: ignore[operator]

        with pytest.raises(QueryRejectedError) as duplicate_columns:
            await service.query(
                "development-issues",
                "SELECT issue_id AS value, status AS value "
                "FROM ai.issue_overview LIMIT 1",
                published.revision,
                SQL_POLICY_REVISION,
            )
        assert duplicate_columns.value.details == {
            "reason_code": "QUERY_DUPLICATE_RESULT_COLUMN"
        }

        with pytest.raises(QueryRejectedError) as caught:
            await service.query(
                "development-issues",
                "SELECT count(*) FROM ai.issue_overview AS a "
                "CROSS JOIN ai.issue_overview AS b CROSS JOIN ai.issue_overview AS c",
                published.revision,
                SQL_POLICY_REVISION,
            )
        assert caught.value.details["reason_code"] in {  # type: ignore[index]
            "QUERY_PLAN_COST_EXCEEDED",
            "QUERY_PLAN_ROWS_EXCEEDED",
        }

        source = registry.get("development-issues")
        assert source is not None
        slow_sql = (
            "SELECT count(*) FROM ai.issue_overview AS a "
            "CROSS JOIN ai.issue_overview AS b CROSS JOIN ai.issue_overview AS c"
        )
        timeout_source = replace(
            source,
            budget=replace(
                source.budget,
                query_statement_timeout_ms=1,
                max_plan_total_cost=2_147_483_647,
                max_plan_rows=2_147_483_647,
            ),
        )
        validated = validate_sql(
            slow_sql,
            allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
        )
        with pytest.raises(QueryTimeoutError):
            await executor.execute(timeout_source, slow_sql, published.revision, validated)

        recovered = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
            SQL_POLICY_REVISION,
        )
        assert recovered["rows"] == [{"issue_count": 600}]

        for forbidden_sql, reason_code in [
            ("SELECT * FROM development.issues", "SQL_RELATION_NOT_ALLOWED"),
            ("DELETE FROM ai.issue_overview", "SQL_STATEMENT_NOT_ALLOWED"),
            ("SELECT pg_catalog.pg_sleep(0.01)", "SQL_FUNCTION_NOT_ALLOWED"),
        ]:
            with pytest.raises(QueryRejectedError) as forbidden:
                await service.query(
                    "development-issues",
                    forbidden_sql,
                    published.revision,
                    SQL_POLICY_REVISION,
                )
            assert forbidden.value.details == {"reason_code": reason_code}

        volatile_sql = "SELECT pg_catalog.random() FROM ai.issue_overview LIMIT 1"
        volatile = validate_sql(
            volatile_sql,
            allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"random"},
        )
        with pytest.raises(QueryRejectedError) as volatile_error:
            await executor.execute(source, volatile_sql, published.revision, volatile)
        assert volatile_error.value.details == {
            "reason_code": "QUERY_RESOLVED_FUNCTION_NOT_ALLOWED"
        }
    finally:
        await executor.close()
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_socket_disconnect_cancels_http_query() -> None:
    executor = DisconnectExecutor()
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token=None,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )
    app = build_app(
        runtime,
        registry=load_test_registry(),
        catalog=DisconnectCatalog(),
        query_executor=executor,
    )
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as session:
            context = await session.post(
                "/meta",
                json={"source_id": "development-issues", "question": "문제 수"},
            )
        body = json.dumps(
            {
                "source_id": "development-issues",
                "sql": "SELECT count(*) FROM ai.issue_overview",
                "metadata_revision": context.json()["metadata_revision"],
                "sql_policy_revision": context.json()["sql_policy_revision"],
            }
        ).encode()
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /query HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(executor.cancelled.wait(), timeout=2)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=3)
        except TimeoutError:
            server.force_exit = True
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task
        finally:
            server_socket.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_modern_mcp_disconnect_cancels_query_before_client_session_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = DisconnectExecutor()
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token=None,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )
    app = build_app(
        runtime,
        registry=load_test_registry(),
        catalog=DisconnectCatalog(),
        query_executor=executor,
    )
    caplog.set_level(logging.DEBUG, logger="query_man.mcp")

    async with _serve_test_app(app) as server_url:
        async with (
            httpx2.AsyncClient(timeout=15, trust_env=False) as mcp_http,
            Client(
                streamable_http_client(
                    f"{server_url}/mcp",
                    http_client=mcp_http,
                ),
                mode=MCP_PROTOCOL_VERSION,
                read_timeout_seconds=15,
            ) as client,
        ):
            assert client.protocol_version == MCP_PROTOCOL_VERSION
            context = await client.call_tool(
                "get_context",
                {"source_id": "development-issues", "question": "문제 수"},
            )
            query = asyncio.create_task(
                client.call_tool(
                    "query",
                    {
                        "source_id": "development-issues",
                        "sql": "SELECT count(*) FROM ai.issue_overview",
                        "metadata_revision": context.structured_content[  # type: ignore[index]
                            "metadata_revision"
                        ],
                        "sql_policy_revision": context.structured_content[  # type: ignore[index]
                            "sql_policy_revision"
                        ],
                    },
                )
            )
            await asyncio.wait_for(executor.started.wait(), timeout=2)
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query
            await asyncio.wait_for(executor.cancelled.wait(), timeout=2)
            sources = await client.call_tool("list_sources", {})
            assert sources.structured_content["sources"]  # type: ignore[index]

    cancelled = [
        record
        for record in caplog.records
        if record.getMessage() == "mcp_tool_completed"
        and getattr(record, "outcome", None) == "cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].cancel_reason == "client_disconnected"
    request_ids = {
        record.mcp_http_request_id
        for record in caplog.records
        if record.getMessage() == "mcp_http_request_completed"
    }
    assert cancelled[0].mcp_http_request_id in request_ids
    assert not any(
        record.getMessage() == "Unhandled request error" for record in caplog.records
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_streamable_http_mcp_runs_all_golden_queries() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    verified = VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        {source["source_id"] for source in registry.list()},
    )
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token="mcp-integration-token-at-least-thirty-two-characters",
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )
    app = build_app(runtime, registry=registry)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        async with httpx.AsyncClient(
            headers={
                "Authorization": "Bearer mcp-integration-token-at-least-thirty-two-characters"
            }
        ) as probe:
            authenticated = await probe.get(f"http://127.0.0.1:{port}/sources")
            assert authenticated.status_code == 200, authenticated.text

        async with (
            httpx2.AsyncClient(
                auth=BearerAuth("mcp-integration-token-at-least-thirty-two-characters"),
                trust_env=False,
            ) as authenticated_http,
            Client(
                streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=authenticated_http,
                ),
                mode=MCP_PROTOCOL_VERSION,
            ) as client,
        ):
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "list_sources",
                "get_context",
                "query",
            ]
            for contract in verified.queries:
                source = registry.get(contract.source_id)
                assert source is not None
                context = await client.call_tool(
                    "get_context",
                    {"source_id": contract.source_id, "question": contract.question},
                )
                context_body = context.structured_content
                assert context_body is not None
                assert context_body["quality_level"] == "L2"
                assert {"extract", "lag", "lead", "rank"} <= set(
                    context_body["sql_capabilities"]["functions"]  # type: ignore[index]
                )
                assert len(json.dumps(context_body, default=str).encode()) <= (
                    source.budget.max_metadata_response_bytes
                )
                assert context_body["metadata_revision"] == contract.metadata_revision
                assert [relation["name"] for relation in context_body["relations"]] == list(  # type: ignore[index]
                    contract.relations
                )
                result = await client.call_tool(
                    "query",
                    {
                        "source_id": contract.source_id,
                        "sql": contract.sql,
                        "metadata_revision": contract.metadata_revision,
                        "sql_policy_revision": context_body["sql_policy_revision"],
                    },
                )
                result_body = result.structured_content
                assert result_body is not None
                assert result_body["result_bytes"] <= source.budget.max_result_bytes
                assert result_body["row_count"] <= source.budget.max_result_rows
                assert result_body["row_count"] == contract.expected.row_count
                columns = tuple(result_body["columns"])  # type: ignore[arg-type]
                assert create_result_hash(columns, result_body["rows"]) == (  # type: ignore[arg-type]
                    contract.expected.result_hash
                )

            unsupported = await client.call_tool(
                "get_context",
                {"source_id": "market-voc", "question": "전체 사용자 수"},
            )
            assert unsupported.structured_content["answerability"]["status"] == (  # type: ignore[index]
                "unsupported"
            )
            clarification = await client.call_tool(
                "get_context",
                {"source_id": "market-voc", "question": "모델별 불량률을 비교해줘"},
            )
            assert clarification.structured_content["answerability"]["status"] == (  # type: ignore[index]
                "needs_clarification"
            )

            mismatch = await client.call_tool(
                "query",
                {
                    "source_id": verified.queries[0].source_id,
                    "sql": verified.queries[0].sql,
                    "metadata_revision": f"sha256:{'0' * 64}",
                    "sql_policy_revision": SQL_POLICY_REVISION,
                },
            )
            assert mismatch.structured_content["error"]["code"] == (  # type: ignore[index]
                "METADATA_REVISION_MISMATCH"
            )
            refreshed = await client.call_tool(
                "get_context",
                {
                    "source_id": verified.queries[0].source_id,
                    "question": verified.queries[0].question,
                },
            )
            assert refreshed.structured_content["metadata_revision"] == (  # type: ignore[index]
                verified.queries[0].metadata_revision
            )
            retried = await client.call_tool(
                "query",
                {
                    "source_id": verified.queries[0].source_id,
                    "sql": verified.queries[0].sql,
                    "metadata_revision": refreshed.structured_content["metadata_revision"],  # type: ignore[index]
                    "sql_policy_revision": refreshed.structured_content["sql_policy_revision"],  # type: ignore[index]
                },
            )
            assert retried.structured_content["row_count"] == (  # type: ignore[index]
                verified.queries[0].expected.row_count
            )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server.force_exit = True
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task
        finally:
            server_socket.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_query_concurrency_cancel_and_source_isolation() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = [
        "DEVELOPMENT_ISSUES_READER_PASSWORD",
        "MARKET_VOC_READER_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    executor = PostgresQueryExecutor()
    slow_task: asyncio.Task[dict[str, object]] | None = None
    try:
        development = registry.get("development-issues")
        market = registry.get("market-voc")
        assert development is not None
        assert market is not None
        development_metadata = await metadata.get_published(development.source_id)
        market_metadata = await metadata.get_published(market.source_id)
        limited_development = replace(
            development,
            budget=replace(
                development.budget,
                query_statement_timeout_ms=2_000,
                query_transaction_timeout_ms=3_000,
                query_queue_timeout_ms=20,
                max_pool_size=1,
                max_concurrent_queries=1,
                max_plan_total_cost=10**15,
                max_plan_rows=10**15,
            ),
        )
        warm_development = replace(
            limited_development,
            budget=replace(
                limited_development.budget,
                query_queue_timeout_ms=development.budget.query_queue_timeout_ms,
            ),
        )
        slow_sql = (
            "SELECT count(*) FROM ai.issue_overview AS a "
            "CROSS JOIN ai.issue_overview AS b "
            "CROSS JOIN ai.issue_overview AS c "
            "CROSS JOIN ai.issue_overview AS d"
        )
        slow_validated = validate_sql(
            slow_sql,
            allowed_relations=(
                relation.qualified_name for relation in development_metadata.snapshot.relations
            ),
        )
        dev_count_sql = "SELECT count(*) AS issue_count FROM ai.issue_overview"
        dev_count_validated = validate_sql(
            dev_count_sql,
            allowed_relations=(
                relation.qualified_name for relation in development_metadata.snapshot.relations
            ),
        )

        async def pooled_reader_timezone() -> str:
            pool = await executor._get_pool(limited_development)
            async with pool.connection(
                timeout=development.budget.query_queue_timeout_ms / 1000
            ) as connection:
                cursor = await connection.execute(
                    "SELECT pg_catalog.current_setting('TimeZone') AS timezone"
                )
                row = await cursor.fetchone()
            assert row is not None
            return str(row["timezone"])

        default_reader_timezone = await pooled_reader_timezone()
        warmed = await executor.execute(
            warm_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert warmed["rows"] == [{"issue_count": 600}]
        assert await pooled_reader_timezone() == default_reader_timezone
        slow_task = asyncio.create_task(
            executor.execute(
                limited_development,
                slow_sql,
                development_metadata.revision,
                slow_validated,
            )
        )
        await asyncio.sleep(0.05)

        with pytest.raises(QueryOverloadedError):
            await executor.execute(
                limited_development,
                dev_count_sql,
                development_metadata.revision,
                dev_count_validated,
            )

        market_sql = "SELECT count(*) AS voc_count FROM ai.voc_overview"
        market_validated = validate_sql(
            market_sql,
            allowed_relations=(relation.qualified_name for relation in market_metadata.snapshot.relations),
        )
        market_result = await executor.execute(
            market,
            market_sql,
            market_metadata.revision,
            market_validated,
        )
        assert market_result["rows"] == [{"voc_count": 1200}]

        slow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_task
        assert await pooled_reader_timezone() == default_reader_timezone

        recovered = await executor.execute(
            limited_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert recovered["rows"] == [{"issue_count": 600}]

        operator_query_id = str(uuid.uuid4())
        slow_task = asyncio.create_task(
            executor.execute(
                limited_development,
                slow_sql,
                development_metadata.revision,
                slow_validated,
                query_id=operator_query_id,
            )
        )
        await asyncio.sleep(0.05)
        observer = await AsyncConnection.connect(
            make_conninfo(
                host="127.0.0.1",
                port=os.environ.get("POSTGRES_PORT", "5432"),
                dbname="development_issues",
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
                sslmode="disable",
            )
        )
        try:
            activity = await observer.execute(
                "SELECT application_name FROM pg_catalog.pg_stat_activity "
                "WHERE application_name = %s",
                (f"query-man:{operator_query_id}",),
            )
            assert await activity.fetchone() == (f"query-man:{operator_query_id}",)
        finally:
            await observer.close()
        assert await executor.cancel(operator_query_id)
        with pytest.raises(QueryTimeoutError):
            await slow_task
        assert await pooled_reader_timezone() == default_reader_timezone

        recovered_after_operator_cancel = await executor.execute(
            limited_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert recovered_after_operator_cancel["rows"] == [{"issue_count": 600}]

        slow_task = asyncio.create_task(
            executor.execute(
                limited_development,
                slow_sql,
                development_metadata.revision,
                slow_validated,
            )
        )
        await asyncio.sleep(0.05)
        await executor.drain(20)
        with pytest.raises(QueryTimeoutError):
            await slow_task
        with pytest.raises(QueryUnavailableError):
            await executor.execute(
                limited_development,
                dev_count_sql,
                development_metadata.revision,
                dev_count_validated,
            )
    finally:
        if slow_task is not None:
            if not slow_task.done():
                slow_task.cancel()
            with suppress(asyncio.CancelledError, QueryOverloadedError, QueryTimeoutError):
                await slow_task
        await executor.close()
        await catalog.close()
