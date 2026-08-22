from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from query_man.access import AccessPolicy
from query_man.app import build_app
from query_man.catalog import PostgresCatalog
from query_man.errors import (
    QueryOverloadedError,
    QueryRejectedError,
    QueryTimeoutError,
    QueryUnavailableError,
)
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
async def test_rls_tenant_context_is_transaction_local_across_pool_reuse() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    if not os.environ.get("DEVELOPMENT_ISSUES_READER_PASSWORD"):
        pytest.skip("local reader credentials are not configured")
    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    development = registry.get("development-issues")
    assert development is not None
    source = replace(
        development,
        source_id="rls-tenant-fixture",
        allowed_schemas=["tenant_ai"],
        allowed_relation_kinds=["view"],
        tenant_isolation="rls",
        budget=replace(
            development.budget,
            max_pool_size=1,
            max_concurrent_queries=1,
        ),
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    sql_text = "SELECT record_id, label FROM tenant_ai.record_overview ORDER BY label"
    validated = validate_sql(
        sql_text,
        allowed_relations=["tenant_ai.record_overview"],
    )
    try:
        snapshot = await catalog.load(source)
        assert [relation.qualified_name for relation in snapshot.relations] == [
            "tenant_ai.record_overview"
        ]
        assert snapshot.relations[0].security_invoker is True

        with pytest.raises(QueryRejectedError) as missing:
            await executor.execute(source, sql_text, "rls-test-revision", validated)
        assert missing.value.details == {"reason_code": "TENANT_CONTEXT_REQUIRED"}

        engineering = await executor.execute(
            source,
            sql_text,
            "rls-test-revision",
            validated,
            tenant_id="engineering",
        )
        assert [row["label"] for row in engineering["rows"]] == [
            "engineering-alpha",
            "engineering-beta",
        ]

        without_context = await executor.execute(
            replace(source, tenant_isolation="none"),
            sql_text,
            "rls-test-revision",
            validated,
        )
        assert without_context["rows"] == []

        quality = await executor.execute(
            source,
            sql_text,
            "rls-test-revision",
            validated,
            tenant_id="quality",
        )
        assert [row["label"] for row in quality["rows"]] == ["quality-alpha"]
    finally:
        await executor.close()
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_onboards_third_source_without_runtime_restart() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL credentials are not configured")
    control_dsn = make_conninfo(
        host="127.0.0.1",
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode="disable",
    )
    encryption_key = base64.urlsafe_b64encode(b"acceptance-source-key-material!!").decode(
        "ascii"
    )
    assert len(base64.urlsafe_b64decode(encryption_key)) == 32
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        source_reload_interval_ms=250,
    )
    manifest = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets.yaml").read_text(
            encoding="utf-8"
        )
    )
    l2_manifest = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets-l2.yaml").read_text(
            encoding="utf-8"
        )
    )
    verified_contract = yaml.safe_load(
        (
            ROOT_DIRECTORY
            / "config"
            / "onboarding"
            / "support-tickets-verified-query.yaml"
        ).read_text(encoding="utf-8")
    )
    credential = os.environ.get(
        "SUPPORT_TICKETS_READER_PASSWORD",
        "support-tickets-local-secret",
    )
    app = build_app(runtime)
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
            base_url=f"http://127.0.0.1:{port}",
        ) as session:
            published = await session.put(
                "/admin/sources/support-tickets",
                json={"manifest": manifest, "credential": credential},
            )
            assert published.status_code == 200
            publish_body = published.json()
            assert publish_body["status"] == "published"
            assert publish_body["quality_level"] == "L0"
            assert credential not in published.text

            listed = await session.get("/sources")
            assert "support-tickets" in {
                source["source_id"] for source in listed.json()["sources"]
            }
            context = await session.post(
                "/meta",
                json={
                    "source_id": "support-tickets",
                    "question": "지원 queue별 ticket 수를 보여줘",
                },
            )
            assert context.status_code == 200
            assert context.json()["quality_level"] == "L0"
            assert [relation["name"] for relation in context.json()["relations"]] == [
                "ai.ticket_overview"
            ]

            l2_manifest["minimum_quality_level"] = "L1"
            semantic_published = await session.put(
                "/admin/sources/support-tickets",
                json={"manifest": l2_manifest, "credential": credential},
            )
            assert semantic_published.status_code == 200
            assert semantic_published.json()["quality_level"] in {"L1", "L2"}
            semantic_context = await session.post(
                "/meta",
                json={
                    "source_id": "support-tickets",
                    "question": "지원 queue별 ticket 수를 보여줘",
                },
            )
            assert semantic_context.status_code == 200

            verified_contract["metadata_revision"] = semantic_context.json()[
                "metadata_revision"
            ]
            verified = await session.post(
                "/admin/sources/support-tickets/verified-queries",
                json=verified_contract,
            )
            assert verified.status_code == 200
            assert verified.json()["status"] == "verified"

            l2_manifest["minimum_quality_level"] = "L2"
            l2_published = await session.put(
                "/admin/sources/support-tickets",
                json={"manifest": l2_manifest, "credential": credential},
            )
            assert l2_published.status_code == 200
            assert l2_published.json()["quality_level"] == "L2"
            context = await session.post(
                "/meta",
                json={
                    "source_id": "support-tickets",
                    "question": "지원 queue별 ticket 수를 보여줘",
                },
            )
            assert context.status_code == 200
            assert context.json()["quality_level"] == "L2"
            queried = await session.post(
                "/query",
                json={
                    "source_id": "support-tickets",
                    "metadata_revision": context.json()["metadata_revision"],
                    "sql": (
                        "SELECT queue_name, count(*) AS ticket_count "
                        "FROM ai.ticket_overview GROUP BY queue_name ORDER BY queue_name"
                    ),
                },
            )
            assert queried.status_code == 200
            assert queried.json()["row_count"] == 3

            async with (
                httpx2.AsyncClient() as mcp_http,
                Client(
                    streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp",
                        http_client=mcp_http,
                    )
                ) as mcp_client,
            ):
                mcp_sources = await mcp_client.call_tool("list_sources", {})
                assert "support-tickets" in {
                    source["source_id"]
                    for source in mcp_sources.structured_content["sources"]  # type: ignore[index,union-attr]
                }
                mcp_context = await mcp_client.call_tool(
                    "get_context",
                    {
                        "source_id": "support-tickets",
                        "question": "지원 queue별 ticket 수를 보여줘",
                    },
                )
                assert mcp_context.structured_content["quality_level"] == "L2"  # type: ignore[index]
                mcp_result = await mcp_client.call_tool(
                    "query",
                    {
                        "source_id": "support-tickets",
                        "metadata_revision": mcp_context.structured_content[  # type: ignore[index,union-attr]
                            "metadata_revision"
                        ],
                        "sql": (
                            "SELECT queue_name, count(*) AS ticket_count "
                            "FROM ai.ticket_overview "
                            "GROUP BY queue_name ORDER BY queue_name"
                        ),
                    },
                )
                assert mcp_result.structured_content["row_count"] == 3  # type: ignore[index]

            rotated_credential = "support-tickets-rotated-acceptance-secret"
            admin_connection = await AsyncConnection.connect(control_dsn)
            try:
                await admin_connection.execute(
                    sql.SQL("ALTER ROLE support_tickets_reader PASSWORD {}").format(
                        sql.Literal(rotated_credential)
                    )
                )
                await admin_connection.commit()
                rotated = await session.post(
                    "/admin/sources/support-tickets/credential",
                    json={"credential": rotated_credential},
                )
                assert rotated.status_code == 200
                assert rotated.json()["generation"] > publish_body["generation"]
                assert rotated_credential not in rotated.text
                after_rotation = await session.post(
                    "/query",
                    json={
                        "source_id": "support-tickets",
                        "metadata_revision": context.json()["metadata_revision"],
                        "sql": "SELECT count(*) AS ticket_count FROM ai.ticket_overview",
                    },
                )
                assert after_rotation.status_code == 200
                assert after_rotation.json()["rows"] == [{"ticket_count": 120}]
            finally:
                await admin_connection.rollback()
                await admin_connection.execute(
                    sql.SQL("ALTER ROLE support_tickets_reader PASSWORD {}").format(
                        sql.Literal(credential)
                    )
                )
                await admin_connection.commit()
                restored = await session.post(
                    "/admin/sources/support-tickets/credential",
                    json={"credential": credential},
                )
                assert restored.status_code == 200
                await admin_connection.close()

            deactivated = await session.delete("/admin/sources/support-tickets")
            assert deactivated.status_code == 200
            listed_after = await session.get("/sources")
            assert "support-tickets" not in {
                source["source_id"] for source in listed_after.json()["sources"]
            }
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
async def test_onboards_commerce_edges_across_authenticated_mcp_replicas(
    tmp_path: Path,
) -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "DEVELOPMENT_ISSUES_READER_PASSWORD",
        "MARKET_VOC_READER_PASSWORD",
    ]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL credentials are not configured")

    control_dsn = make_conninfo(
        host="127.0.0.1",
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode="disable",
    )
    encryption_key = base64.urlsafe_b64encode(b"acceptance-source-key-material!!").decode(
        "ascii"
    )
    assert len(base64.urlsafe_b64decode(encryption_key)) == 32
    operator_token = "commerce-operator-token-with-at-least-32-characters"
    restricted_token = "commerce-restricted-token-with-at-least-32-characters"
    policy_file = tmp_path / "commerce-access.yaml"
    policy_file.write_text(
        """
version: 1
callers:
  - caller_id: commerce-operator
    tenant_id: operations
    token_env: COMMERCE_OPERATOR_TOKEN
    all_sources: true
    operator: true
  - caller_id: development-reader
    tenant_id: engineering
    token_env: COMMERCE_RESTRICTED_TOKEN
    allowed_sources: [development-issues]
    operator: false
""".strip(),
        encoding="utf-8",
    )
    access_policy = AccessPolicy.load(
        policy_file,
        {"development-issues", "market-voc"},
        {
            "COMMERCE_OPERATOR_TOKEN": operator_token,
            "COMMERCE_RESTRICTED_TOKEN": restricted_token,
        },
    )
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
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        source_reload_interval_ms=250,
    )
    l0_manifest = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "commerce-edges.yaml").read_text(
            encoding="utf-8"
        )
    )
    l0_manifest["connection"]["port"] = int(  # type: ignore[index]
        os.environ.get("POSTGRES_PORT", "5432")
    )
    semantic_manifest = yaml.safe_load(
        (
            ROOT_DIRECTORY / "config" / "onboarding" / "commerce-edges-l2.yaml"
        ).read_text(encoding="utf-8")
    )
    semantic_manifest["connection"]["port"] = int(  # type: ignore[index]
        os.environ.get("POSTGRES_PORT", "5432")
    )
    semantic_manifest["semantic_overlay"]["relations"][0]["description"] += (  # type: ignore[index,operator]
        f" [{uuid.uuid4().hex}]"
    )
    verified_contract = yaml.safe_load(
        (
            ROOT_DIRECTORY
            / "config"
            / "onboarding"
            / "commerce-edges-verified-query.yaml"
        ).read_text(encoding="utf-8")
    )
    credential = os.environ.get(
        "COMMERCE_EDGES_READER_PASSWORD",
        "commerce-edges-local-secret",
    )
    expected_rows = [
        {
            "order_id": "00000000-0000-0000-0000-000000000001",
            "placed_at": "2026-08-01T01:00:00+00:00",
            "promised_on": "2026-08-05",
            "discount_amount": None,
            "net_amount": 100.0,
            "attributes": {"campaign": "summer", "gift": False},
            "line_count": 2,
            "returned_line_count": 1,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000002",
            "placed_at": "2026-08-02T02:00:00+00:00",
            "promised_on": None,
            "discount_amount": 5.5,
            "net_amount": 74.5,
            "attributes": {"campaign": None, "gift": True},
            "line_count": 1,
            "returned_line_count": 1,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000003",
            "placed_at": "2026-08-03T03:00:00+00:00",
            "promised_on": "2026-08-03",
            "discount_amount": 0.0,
            "net_amount": 50.25,
            "attributes": {"store": "서울"},
            "line_count": 0,
            "returned_line_count": 0,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000004",
            "placed_at": "2026-08-04T04:00:00+00:00",
            "promised_on": "2026-08-10",
            "discount_amount": 20.0,
            "net_amount": 100.0,
            "attributes": {"partner": "alpha", "tags": ["bulk", "priority"]},
            "line_count": 3,
            "returned_line_count": 0,
        },
    ]

    replica_a = build_app(runtime, access_policy=access_policy)
    replica_b = build_app(runtime, access_policy=access_policy)
    admin_connection = await AsyncConnection.connect(control_dsn)
    source_active = False
    reader_restricted = True
    try:
        async with (
            _serve_test_app(replica_a) as replica_a_url,
            _serve_test_app(replica_b) as replica_b_url,
        ):
            async with httpx.AsyncClient(
                base_url=replica_a_url,
                headers={"Authorization": f"Bearer {operator_token}"},
            ) as admin_session:
                async with (
                    httpx2.AsyncClient(auth=BearerAuth(operator_token)) as operator_http,
                    Client(
                        streamable_http_client(
                            f"{replica_b_url}/mcp",
                            http_client=operator_http,
                        )
                    ) as operator_mcp,
                    httpx2.AsyncClient(auth=BearerAuth(restricted_token)) as restricted_http,
                    Client(
                        streamable_http_client(
                            f"{replica_b_url}/mcp",
                            http_client=restricted_http,
                        )
                    ) as restricted_mcp,
                ):
                    try:
                        initial_sources = await admin_session.get("/sources")
                        assert initial_sources.status_code == 200
                        if "commerce-edges" in {
                            source["source_id"]
                            for source in initial_sources.json()["sources"]
                        }:
                            initial_deactivate = await admin_session.delete(
                                "/admin/sources/commerce-edges"
                            )
                            assert initial_deactivate.status_code == 200
                            for _ in range(100):
                                listed = await operator_mcp.call_tool("list_sources", {})
                                listed_body = listed.structured_content
                                if isinstance(listed_body, dict) and "commerce-edges" not in {
                                    source["source_id"] for source in listed_body["sources"]
                                }:
                                    break
                                await asyncio.sleep(0.1)
                            else:
                                pytest.fail("replica B did not remove the initially active source")

                        await admin_connection.execute(
                            "ALTER ROLE commerce_edges_reader CREATEDB"
                        )
                        await admin_connection.commit()
                        reader_restricted = False
                        try:
                            rejected = await admin_session.put(
                                "/admin/sources/commerce-edges",
                                json={"manifest": l0_manifest, "credential": credential},
                            )
                            assert rejected.status_code == 400, rejected.text
                            assert rejected.json()["error"]["code"] == (
                                "SOURCE_VALIDATION_FAILED"
                            )
                        finally:
                            await admin_connection.rollback()
                            await admin_connection.execute(
                                "ALTER ROLE commerce_edges_reader NOCREATEDB"
                            )
                            await admin_connection.commit()
                            reader_restricted = True

                        published = await admin_session.put(
                            "/admin/sources/commerce-edges",
                            json={"manifest": l0_manifest, "credential": credential},
                        )
                        assert published.status_code == 200, published.text
                        source_active = True
                        published_body = published.json()
                        assert published_body["quality_level"] == "L0"
                        old_revision = published_body["metadata_revision"]

                        l0_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            l0_context = await operator_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": "commerce order",
                                },
                            )
                            candidate = l0_context.structured_content
                            if (
                                isinstance(candidate, dict)
                                and candidate.get("metadata_revision") == old_revision
                            ):
                                l0_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert l0_context_body is not None, (
                            "replica B did not apply the L0 source generation"
                        )
                        assert l0_context_body["quality_level"] == "L0"

                        restricted_sources = await restricted_mcp.call_tool(
                            "list_sources", {}
                        )
                        restricted_sources_body = restricted_sources.structured_content
                        assert isinstance(restricted_sources_body, dict)
                        assert "commerce-edges" not in {
                            source["source_id"]
                            for source in restricted_sources_body["sources"]
                        }
                        restricted_context = await restricted_mcp.call_tool(
                            "get_context",
                            {
                                "source_id": "commerce-edges",
                                "question": verified_contract["question"],
                            },
                        )
                        assert restricted_context.structured_content == {
                            "error": {
                                "code": "SOURCE_NOT_FOUND",
                                "message": "The requested source was not found.",
                            }
                        }

                        semantic_manifest["minimum_quality_level"] = "L1"
                        semantic_published = await admin_session.put(
                            "/admin/sources/commerce-edges",
                            json={
                                "manifest": semantic_manifest,
                                "credential": credential,
                            },
                        )
                        assert semantic_published.status_code == 200, semantic_published.text
                        semantic_revision = semantic_published.json()["metadata_revision"]
                        assert semantic_revision != old_revision

                        semantic_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            semantic_context = await operator_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            candidate = semantic_context.structured_content
                            if (
                                isinstance(candidate, dict)
                                and candidate.get("metadata_revision")
                                == semantic_revision
                            ):
                                semantic_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert semantic_context_body is not None, (
                            "replica B did not apply the semantic source generation"
                        )

                        stale_query = await operator_mcp.call_tool(
                            "query",
                            {
                                "source_id": "commerce-edges",
                                "sql": verified_contract["sql"],
                                "metadata_revision": old_revision,
                            },
                        )
                        assert stale_query.structured_content is not None
                        assert stale_query.structured_content["error"]["code"] == (  # type: ignore[index]
                            "METADATA_REVISION_MISMATCH"
                        )

                        verified_contract["metadata_revision"] = semantic_revision
                        verified = await admin_session.post(
                            "/admin/sources/commerce-edges/verified-queries",
                            json=verified_contract,
                        )
                        assert verified.status_code == 200, verified.text
                        assert verified.json()["result_hash"] == verified_contract[
                            "expected"
                        ]["result_hash"]

                        semantic_manifest["minimum_quality_level"] = "L2"
                        l2_published = await admin_session.put(
                            "/admin/sources/commerce-edges",
                            json={
                                "manifest": semantic_manifest,
                                "credential": credential,
                            },
                        )
                        assert l2_published.status_code == 200, l2_published.text
                        assert l2_published.json()["metadata_revision"] == semantic_revision
                        assert l2_published.json()["quality_level"] == "L2"

                        final_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            final_context = await operator_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            candidate = final_context.structured_content
                            if (
                                isinstance(candidate, dict)
                                and candidate.get("metadata_revision")
                                == semantic_revision
                                and candidate.get("quality_level") == "L2"
                            ):
                                final_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert final_context_body is not None, (
                            "replica B did not apply the verified L2 generation"
                        )
                        assert final_context_body["metadata_revision"] == semantic_revision
                        assert final_context_body["quality_level"] == "L2"
                        relations = {
                            relation["name"]: relation
                            for relation in final_context_body["relations"]  # type: ignore[union-attr]
                        }
                        assert set(relations) == {"ai.Order", "ai.OrderLine"}
                        assert relations["ai.Order"]["sql_name"] == '"ai"."Order"'
                        assert relations["ai.OrderLine"]["sql_name"] == (
                            '"ai"."OrderLine"'
                        )
                        assert relations["ai.Order"]["grain"]["key_columns"] == [  # type: ignore[index]
                            "OrderID"
                        ]
                        assert relations["ai.OrderLine"]["grain"]["key_columns"] == [  # type: ignore[index]
                            "OrderID",
                            "LineNo",
                        ]
                        order_columns = {
                            column["name"]: column
                            for column in relations["ai.Order"]["columns"]
                        }
                        assert {
                            name: (
                                order_columns[name]["sql_name"],
                                order_columns[name]["data_type"],
                                order_columns[name]["nullable"],
                            )
                            for name in [
                                "OrderID",
                                "PlacedAt",
                                "DiscountAmount",
                                "PromisedOn",
                                "Attributes",
                            ]
                        } == {
                            "OrderID": ('"OrderID"', "uuid", "unknown"),
                            "PlacedAt": (
                                '"PlacedAt"',
                                "timestamp with time zone",
                                "unknown",
                            ),
                            "DiscountAmount": (
                                '"DiscountAmount"',
                                "numeric(12,2)",
                                "unknown",
                            ),
                            "PromisedOn": ('"PromisedOn"', "date", "unknown"),
                            "Attributes": ('"Attributes"', "jsonb", "unknown"),
                        }
                        assert final_context_body["joins"] == [
                            {
                                "left_relation": "ai.Order",
                                "right_relation": "ai.OrderLine",
                                "column_pairs": [
                                    {"left": "OrderID", "right": "OrderID"}
                                ],
                                "cardinality": "one_to_many",
                                "fanout": True,
                                "guidance": (
                                    "주문 라인을 OrderID별로 먼저 집계한 뒤 주문에 결합해야 "
                                    "주문 건수와 금액이 중복되지 않는다."
                                ),
                            }
                        ]

                        result = await operator_mcp.call_tool(
                            "query",
                            {
                                "source_id": "commerce-edges",
                                "sql": verified_contract["sql"],
                                "metadata_revision": final_context_body[
                                    "metadata_revision"
                                ],
                            },
                        )
                        result_body = result.structured_content
                        assert isinstance(result_body, dict)
                        assert result_body["columns"] == verified_contract["expected"][
                            "columns"
                        ]
                        assert result_body["row_count"] == verified_contract["expected"][
                            "row_count"
                        ]
                        assert result_body["rows"] == expected_rows
                        assert result_body["metadata_revision"] == semantic_revision
                        assert result_body["truncated"] is False
                        assert result_body["result_bytes"] == len(
                            json.dumps(
                                expected_rows,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                        assert create_result_hash(
                            tuple(result_body["columns"]),  # type: ignore[arg-type]
                            result_body["rows"],
                        ) == verified_contract["expected"]["result_hash"]

                        http_result = await admin_session.post(
                            "/query",
                            json={
                                "source_id": "commerce-edges",
                                "sql": verified_contract["sql"],
                                "metadata_revision": semantic_revision,
                            },
                        )
                        assert http_result.status_code == 200, http_result.text
                        http_result_body = http_result.json()
                        for key in [
                            "metadata_revision",
                            "fingerprint",
                            "columns",
                            "rows",
                            "row_count",
                            "result_bytes",
                            "truncated",
                        ]:
                            assert http_result_body[key] == result_body[key]

                        duplicate = await operator_mcp.call_tool(
                            "query",
                            {
                                "source_id": "commerce-edges",
                                "sql": (
                                    'SELECT "OrderID" AS duplicate, '
                                    '"Status" AS duplicate FROM ai."Order" LIMIT 1'
                                ),
                                "metadata_revision": semantic_revision,
                            },
                        )
                        assert duplicate.structured_content is not None
                        assert duplicate.structured_content["error"]["code"] == (  # type: ignore[index]
                            "QUERY_REJECTED"
                        )
                        assert duplicate.structured_content["error"]["details"] == {  # type: ignore[index]
                            "reason_code": "QUERY_DUPLICATE_RESULT_COLUMN"
                        }

                        deactivated = await admin_session.delete(
                            "/admin/sources/commerce-edges"
                        )
                        assert deactivated.status_code == 200, deactivated.text
                        source_active = False
                        for _ in range(100):
                            listed = await operator_mcp.call_tool("list_sources", {})
                            missing = await operator_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            listed_body = listed.structured_content
                            missing_body = missing.structured_content
                            if (
                                isinstance(listed_body, dict)
                                and "commerce-edges"
                                not in {
                                    source["source_id"]
                                    for source in listed_body["sources"]
                                }
                                and isinstance(missing_body, dict)
                                and missing_body.get("error", {}).get("code")
                                == "SOURCE_NOT_FOUND"
                            ):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            pytest.fail(
                                "the open replica B MCP client retained the deactivated source"
                            )
                    finally:
                        if source_active:
                            cleanup = await admin_session.delete(
                                "/admin/sources/commerce-edges"
                            )
                            assert cleanup.status_code == 200, cleanup.text
                            source_active = False
    finally:
        if not reader_restricted:
            await admin_connection.rollback()
            await admin_connection.execute(
                "ALTER ROLE commerce_edges_reader NOCREATEDB"
            )
            await admin_connection.commit()
        await admin_connection.close()


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

        with pytest.raises(QueryRejectedError) as duplicate_columns:
            await service.query(
                "development-issues",
                "SELECT issue_id AS value, status AS value "
                "FROM ai.issue_overview LIMIT 1",
                published.revision,
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
                assert context_body["quality_level"] == "L2"
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
