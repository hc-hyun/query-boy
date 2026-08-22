from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import Generator
from contextlib import suppress
from dataclasses import replace

import httpx
import httpx2
import pytest
import uvicorn
from dotenv import load_dotenv
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from query_man.app import build_app
from query_man.catalog import PostgresCatalog
from query_man.errors import QueryOverloadedError, QueryRejectedError, QueryTimeoutError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.query import PostgresQueryExecutor, QueryService
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
from query_man.sql_validation import DEFAULT_ALLOWED_FUNCTIONS, ValidatedSql, validate_sql
from query_man.verified import VerifiedQueryRegistry, create_result_hash
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


class BearerAuth(httpx2.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class DisconnectCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return minimal_development_snapshot()

    async def close(self) -> None:
        pass


class DisconnectExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(
        self,
        _source: SourceProfile,
        _sql: str,
        _metadata_revision: str,
        _validated: ValidatedSql,
        *,
        query_id: str | None = None,
    ) -> dict[str, object]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return {"query_id": query_id or "unreachable"}

    async def cancel(self, _query_id: str, _allowed_sources: frozenset[str]) -> bool:
        return False

    async def close(self) -> None:
        pass


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
        )
        assert counted["rows"] == [{"issue_count": 600}]
        assert counted["truncated"] is False
        assert counted["plan_summary"]["node_count"] > 0  # type: ignore[index]

        limited = await service.query(
            "development-issues",
            "SELECT * FROM ai.issue_comments ORDER BY comment_id",
            published.revision,
        )
        assert limited["row_count"] == 1000
        assert limited["truncated"] is True
        assert limited["result_bytes"] <= 1_048_576  # type: ignore[operator]

        with pytest.raises(QueryRejectedError) as caught:
            await service.query(
                "development-issues",
                "SELECT count(*) FROM ai.issue_overview AS a "
                "CROSS JOIN ai.issue_overview AS b CROSS JOIN ai.issue_overview AS c",
                published.revision,
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
                auth=BearerAuth("mcp-integration-token-at-least-thirty-two-characters")
            ) as authenticated_http,
            Client(
                streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=authenticated_http,
                )
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
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
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
        warmed = await executor.execute(
            warm_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert warmed["rows"] == [{"issue_count": 600}]
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
        assert await executor.cancel(operator_query_id, frozenset({"market-voc"})) is False
        assert await executor.cancel(
            operator_query_id,
            frozenset({"development-issues"}),
        )
        with pytest.raises(QueryTimeoutError):
            await slow_task

        recovered_after_operator_cancel = await executor.execute(
            limited_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert recovered_after_operator_cancel["rows"] == [{"issue_count": 600}]
    finally:
        if slow_task is not None:
            if not slow_task.done():
                slow_task.cancel()
            with suppress(asyncio.CancelledError, QueryOverloadedError, QueryTimeoutError):
                await slow_task
        await executor.close()
        await catalog.close()
