from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

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
from psycopg.rows import dict_row

from query_man.assurance.verified import VerifiedQueryRegistry, create_result_hash
from query_man.delivery.access import AccessPolicy
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
from query_man.metadata.models import CatalogSnapshot, ResourceObservation
from query_man.metadata.service import MetadataService
from query_man.runtime.composition import build_app
from query_man.runtime.config import RuntimeConfig
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


_QUERY_A_TOKEN = "integration-query-a-token-with-at-least-32-characters"
_QUERY_B_TOKEN = "integration-query-b-token-with-at-least-32-characters"
_ADMIN_TOKEN = "integration-admin-token-with-at-least-32-characters"


def _mutation_headers(
    reason: str,
    generation: int,
    state_version: int,
    *,
    metadata_revision: str | None = None,
) -> dict[str, str]:
    headers = {
        "Idempotency-Key": str(uuid.uuid4()),
        "X-Query-Man-Reason": reason,
        "X-Expected-Generation": str(generation),
        "X-Expected-State-Version": str(state_version),
    }
    if metadata_revision is not None:
        headers["X-Expected-Metadata-Revision"] = metadata_revision
    return headers


def _successful_mutation(
    response: httpx.Response,
) -> tuple[dict[str, Any], int, int]:
    body = response.json()
    assert body["outcome"] == "succeeded"
    assert body["http_status"] == 200
    result = body["result"]
    resulting_state = body["resulting_state"]
    assert isinstance(result, dict)
    assert isinstance(resulting_state, dict)
    generation = resulting_state["generation"]
    state_version = resulting_state["state_version"]
    assert isinstance(generation, int)
    assert isinstance(state_version, int)
    return result, generation, state_version


def _shared_access_policy(path: Path) -> AccessPolicy:
    path.write_text(
        """
version: 2
callers:
  - caller_id: query-a
    tenant_id: engineering
    token_env: QUERY_A_TOKEN
  - caller_id: query-b
    tenant_id: quality
    token_env: QUERY_B_TOKEN
  - caller_id: admin
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""".strip(),
        encoding="utf-8",
    )
    return AccessPolicy.load(
        path,
        {
            "QUERY_A_TOKEN": _QUERY_A_TOKEN,
            "QUERY_B_TOKEN": _QUERY_B_TOKEN,
            "ADMIN_TOKEN": _ADMIN_TOKEN,
        },
    )


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

    async def invalidate(self, _source_id: str) -> None:
        pass

    async def observe_resources(
        self,
        _source: SourceProfile,
    ) -> ResourceObservation:
        raise RuntimeError("Resource observation should not be called")


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

    async def cancel(self, _query_id: str) -> bool:
        return False

    async def close(self) -> None:
        pass

    def stop_accepting(self) -> None:
        pass

    async def drain(self, _grace_ms: int) -> None:
        self.stop_accepting()

    async def invalidate(self, _source_id: str) -> None:
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
async def test_onboards_third_source_without_runtime_restart(
    tmp_path: Path,
    disposable_control_dsn: str,
) -> None:
    from query_man.managed.runtime import build_app as build_managed_app

    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL credentials are not configured")
    control_dsn = disposable_control_dsn
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
        source_mode="managed",
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        source_reload_interval_ms=250,
        replica_id="onboarding-runtime",
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
    access_policy = _shared_access_policy(tmp_path / "support-access.yaml")
    app = build_managed_app(runtime, access_policy=access_policy)
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

        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
            ) as admin_session,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {_QUERY_A_TOKEN}"},
            ) as query_session,
        ):
            published = await admin_session.put(
                "/admin/sources/support-tickets",
                json={"manifest": manifest, "credential": credential},
                headers=_mutation_headers("acceptance/support-l0", 0, 0),
            )
            assert published.status_code == 200
            publish_result, generation, state_version = _successful_mutation(published)
            assert publish_result["status"] == "published"
            assert publish_result["quality_level"] == "L0"
            assert credential not in published.text

            listed = await query_session.get("/sources")
            assert "support-tickets" in {
                source["source_id"] for source in listed.json()["sources"]
            }
            context = await query_session.post(
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
            semantic_published = await admin_session.put(
                "/admin/sources/support-tickets",
                json={"manifest": l2_manifest, "credential": credential},
                headers=_mutation_headers(
                    "acceptance/support-l1",
                    generation,
                    state_version,
                ),
            )
            assert semantic_published.status_code == 200
            semantic_result, generation, state_version = _successful_mutation(
                semantic_published
            )
            assert semantic_result["quality_level"] in {"L1", "L2"}
            semantic_context = await query_session.post(
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
            verified = await admin_session.post(
                "/admin/sources/support-tickets/verified-queries",
                json=verified_contract,
                headers=_mutation_headers(
                    "acceptance/support-contract",
                    generation,
                    state_version,
                ),
            )
            assert verified.status_code == 200
            verified_result, verified_generation, verified_state_version = (
                _successful_mutation(verified)
            )
            assert verified_result["status"] == "verified"
            assert (verified_generation, verified_state_version) == (
                generation,
                state_version,
            )

            l2_manifest["minimum_quality_level"] = "L2"
            l2_published = await admin_session.put(
                "/admin/sources/support-tickets",
                json={"manifest": l2_manifest, "credential": credential},
                headers=_mutation_headers(
                    "acceptance/support-l2",
                    generation,
                    state_version,
                ),
            )
            assert l2_published.status_code == 200
            l2_result, generation, state_version = _successful_mutation(l2_published)
            assert l2_result["quality_level"] == "L2"
            context = await query_session.post(
                "/meta",
                json={
                    "source_id": "support-tickets",
                    "question": "지원 queue별 ticket 수를 보여줘",
                },
            )
            assert context.status_code == 200
            assert context.json()["quality_level"] == "L2"
            queried = await query_session.post(
                "/query",
                json={
                    "source_id": "support-tickets",
                    "metadata_revision": context.json()["metadata_revision"],
                    "sql_policy_revision": context.json()["sql_policy_revision"],
                    "sql": (
                        "SELECT queue_name, count(*) AS ticket_count "
                        "FROM ai.ticket_overview GROUP BY queue_name ORDER BY queue_name"
                    ),
                },
            )
            assert queried.status_code == 200
            assert queried.json()["row_count"] == 3

            async with (
                httpx2.AsyncClient(
                    auth=BearerAuth(_QUERY_B_TOKEN),
                    trust_env=False,
                ) as mcp_http,
                Client(
                    streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp",
                        http_client=mcp_http,
                    ),
                    mode=MCP_PROTOCOL_VERSION,
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
                        "sql_policy_revision": mcp_context.structured_content[  # type: ignore[index,union-attr]
                            "sql_policy_revision"
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
                rotated = await admin_session.post(
                    "/admin/sources/support-tickets/credential",
                    json={"credential": rotated_credential},
                    headers=_mutation_headers(
                        "acceptance/support-rotate",
                        generation,
                        state_version,
                    ),
                )
                assert rotated.status_code == 200
                rotated_result, generation, state_version = _successful_mutation(
                    rotated
                )
                assert rotated_result["generation"] > publish_result["generation"]
                assert rotated_credential not in rotated.text
                after_rotation = await query_session.post(
                    "/query",
                    json={
                        "source_id": "support-tickets",
                        "metadata_revision": context.json()["metadata_revision"],
                        "sql_policy_revision": context.json()["sql_policy_revision"],
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
                restored = await admin_session.post(
                    "/admin/sources/support-tickets/credential",
                    json={"credential": credential},
                    headers=_mutation_headers(
                        "acceptance/support-restore",
                        generation,
                        state_version,
                    ),
                )
                assert restored.status_code == 200
                _restored_result, generation, state_version = _successful_mutation(
                    restored
                )
                await admin_connection.close()

            deactivated = await admin_session.delete(
                "/admin/sources/support-tickets",
                headers=_mutation_headers(
                    "acceptance/support-deactivate",
                    generation,
                    state_version,
                ),
            )
            assert deactivated.status_code == 200
            listed_after = await query_session.get("/sources")
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
    disposable_control_dsn: str,
) -> None:
    from query_man.managed.runtime import build_app as build_managed_app

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

    control_dsn = disposable_control_dsn
    encryption_key = base64.urlsafe_b64encode(b"acceptance-source-key-material!!").decode(
        "ascii"
    )
    assert len(base64.urlsafe_b64decode(encryption_key)) == 32
    access_policy = _shared_access_policy(tmp_path / "commerce-access.yaml")
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
        source_mode="managed",
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        source_reload_interval_ms=250,
        replica_id="commerce-runtime",
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
    verified_contract["sql"] = verified_contract["sql"].replace(
        'orders."OrderID" AS order_id',
        'orders."OrderID"::pg_catalog.text AS order_id',
    ).replace(
        'orders."Attributes" AS attributes',
        'orders."Attributes"::pg_catalog.text AS attributes',
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
            "net_amount": "100.00",
            "attributes": '{"gift": false, "campaign": "summer"}',
            "line_count": 2,
            "returned_line_count": 1,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000002",
            "placed_at": "2026-08-02T02:00:00+00:00",
            "promised_on": None,
            "discount_amount": "5.50",
            "net_amount": "74.50",
            "attributes": '{"gift": true, "campaign": null}',
            "line_count": 1,
            "returned_line_count": 1,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000003",
            "placed_at": "2026-08-03T03:00:00+00:00",
            "promised_on": "2026-08-03",
            "discount_amount": "0.00",
            "net_amount": "50.25",
            "attributes": '{"store": "서울"}',
            "line_count": 0,
            "returned_line_count": 0,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000004",
            "placed_at": "2026-08-04T04:00:00+00:00",
            "promised_on": "2026-08-10",
            "discount_amount": "20.00",
            "net_amount": "100.00",
            "attributes": '{"tags": ["bulk", "priority"], "partner": "alpha"}',
            "line_count": 3,
            "returned_line_count": 0,
        },
    ]
    verified_contract["expected"]["result_hash"] = (
        "sha256:ea0e6ab0c5d498f960968b5e7dca5d164f7b6e0c4616d1ea89415808c527e3e7"
    )

    replica_a = build_managed_app(
        replace(runtime, replica_id="commerce-runtime-a"),
        access_policy=access_policy,
    )
    replica_b = build_managed_app(
        replace(runtime, replica_id="commerce-runtime-b"),
        access_policy=access_policy,
    )
    admin_connection = await AsyncConnection.connect(control_dsn)
    source_active = False
    reader_restricted = True
    expected_generation = 0
    expected_state_version = 0
    try:
        async with (
            _serve_test_app(replica_a) as replica_a_url,
            _serve_test_app(replica_b) as replica_b_url,
        ):
            async with (
                httpx.AsyncClient(
                    base_url=replica_a_url,
                    headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                ) as admin_session,
                httpx.AsyncClient(
                    base_url=replica_a_url,
                    headers={"Authorization": f"Bearer {_QUERY_A_TOKEN}"},
                ) as query_session,
            ):
                async with (
                    httpx2.AsyncClient(
                        auth=BearerAuth(_QUERY_A_TOKEN),
                        trust_env=False,
                    ) as query_a_http,
                    Client(
                        streamable_http_client(
                            f"{replica_b_url}/mcp",
                            http_client=query_a_http,
                        ),
                        mode=MCP_PROTOCOL_VERSION,
                    ) as query_a_mcp,
                    httpx2.AsyncClient(
                        auth=BearerAuth(_QUERY_B_TOKEN),
                        trust_env=False,
                    ) as query_b_http,
                    Client(
                        streamable_http_client(
                            f"{replica_b_url}/mcp",
                            http_client=query_b_http,
                        ),
                        mode=MCP_PROTOCOL_VERSION,
                    ) as query_b_mcp,
                ):
                    try:
                        initial_sources = await query_session.get("/sources")
                        assert initial_sources.status_code == 200
                        if "commerce-edges" in {
                            source["source_id"]
                            for source in initial_sources.json()["sources"]
                        }:
                            current = await admin_session.get(
                                "/admin/sources/commerce-edges"
                            )
                            assert current.status_code == 200, current.text
                            expected_generation = current.json()["generation"]
                            expected_state_version = current.json()["state_version"]
                            initial_deactivate = await admin_session.delete(
                                "/admin/sources/commerce-edges",
                                headers=_mutation_headers(
                                    "acceptance/commerce-initial-deactivate",
                                    expected_generation,
                                    expected_state_version,
                                ),
                            )
                            assert initial_deactivate.status_code == 200
                            (
                                _initial_result,
                                expected_generation,
                                expected_state_version,
                            ) = _successful_mutation(initial_deactivate)
                            for _ in range(100):
                                listed = await query_a_mcp.call_tool("list_sources", {})
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
                                headers=_mutation_headers(
                                    "acceptance/commerce-reader-check",
                                    expected_generation,
                                    expected_state_version,
                                ),
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
                            headers=_mutation_headers(
                                "acceptance/commerce-l0",
                                expected_generation,
                                expected_state_version,
                            ),
                        )
                        assert published.status_code == 200, published.text
                        source_active = True
                        (
                            published_result,
                            expected_generation,
                            expected_state_version,
                        ) = _successful_mutation(published)
                        assert published_result["quality_level"] == "L0"
                        old_revision = published_result["metadata_revision"]
                        l0_generation = published_result["generation"]

                        l0_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            l0_context = await query_a_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            candidate = l0_context.structured_content
                            if (
                                isinstance(candidate, dict)
                                and candidate.get("metadata_revision") == old_revision
                                and replica_b.state.registry.get(
                                    "commerce-edges"
                                ).control_generation
                                == l0_generation
                            ):
                                l0_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert l0_context_body is not None, (
                            "replica B did not apply the L0 source generation"
                        )
                        assert l0_context_body["quality_level"] == "L0"

                        query_a_sources = await query_a_mcp.call_tool("list_sources", {})
                        query_b_sources = await query_b_mcp.call_tool("list_sources", {})
                        query_a_sources_body = query_a_sources.structured_content
                        query_b_sources_body = query_b_sources.structured_content
                        assert isinstance(query_a_sources_body, dict)
                        assert isinstance(query_b_sources_body, dict)
                        assert query_b_sources_body == query_a_sources_body
                        assert "commerce-edges" in {
                            source["source_id"]
                            for source in query_b_sources_body["sources"]
                        }
                        query_b_context = await query_b_mcp.call_tool(
                            "get_context",
                            {
                                "source_id": "commerce-edges",
                                "question": verified_contract["question"],
                            },
                        )
                        assert isinstance(query_b_context.structured_content, dict)
                        assert query_b_context.structured_content == l0_context_body
                        assert replica_a.state.registry.get(
                            "commerce-edges"
                        ).budget == replica_b.state.registry.get("commerce-edges").budget

                        semantic_manifest["minimum_quality_level"] = "L1"
                        semantic_published = await admin_session.put(
                            "/admin/sources/commerce-edges",
                            json={
                                "manifest": semantic_manifest,
                                "credential": credential,
                            },
                            headers=_mutation_headers(
                                "acceptance/commerce-l1",
                                expected_generation,
                                expected_state_version,
                            ),
                        )
                        assert semantic_published.status_code == 200, semantic_published.text
                        (
                            semantic_result,
                            expected_generation,
                            expected_state_version,
                        ) = _successful_mutation(semantic_published)
                        semantic_revision = semantic_result["metadata_revision"]
                        semantic_generation = semantic_result["generation"]
                        assert semantic_revision != old_revision

                        semantic_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            semantic_context = await query_a_mcp.call_tool(
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
                                and replica_b.state.registry.get(
                                    "commerce-edges"
                                ).control_generation
                                == semantic_generation
                            ):
                                semantic_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert semantic_context_body is not None, (
                            "replica B did not apply the semantic source generation"
                        )

                        stale_query = await query_a_mcp.call_tool(
                            "query",
                            {
                                "source_id": "commerce-edges",
                                "sql": verified_contract["sql"],
                                "metadata_revision": old_revision,
                                "sql_policy_revision": semantic_context_body[
                                    "sql_policy_revision"
                                ],
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
                            headers=_mutation_headers(
                                "acceptance/commerce-contract",
                                expected_generation,
                                expected_state_version,
                            ),
                        )
                        assert verified.status_code == 200, verified.text
                        verified_result, verified_generation, verified_state_version = (
                            _successful_mutation(verified)
                        )
                        assert verified_result["result_hash"] == verified_contract[
                            "expected"
                        ]["result_hash"]
                        assert (verified_generation, verified_state_version) == (
                            expected_generation,
                            expected_state_version,
                        )

                        semantic_manifest["minimum_quality_level"] = "L2"
                        l2_published = await admin_session.put(
                            "/admin/sources/commerce-edges",
                            json={
                                "manifest": semantic_manifest,
                                "credential": credential,
                            },
                            headers=_mutation_headers(
                                "acceptance/commerce-l2",
                                expected_generation,
                                expected_state_version,
                            ),
                        )
                        assert l2_published.status_code == 200, l2_published.text
                        (
                            l2_result,
                            expected_generation,
                            expected_state_version,
                        ) = _successful_mutation(l2_published)
                        assert l2_result["metadata_revision"] == semantic_revision
                        assert l2_result["quality_level"] == "L2"
                        l2_generation = l2_result["generation"]

                        final_context_body: dict[str, object] | None = None
                        for _ in range(100):
                            final_context = await query_a_mcp.call_tool(
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
                                and replica_b.state.registry.get(
                                    "commerce-edges"
                                ).control_generation
                                == l2_generation
                            ):
                                final_context_body = candidate
                                break
                            await asyncio.sleep(0.1)
                        assert final_context_body is not None, (
                            "replica B did not apply the verified L2 generation"
                        )
                        assert final_context_body["metadata_revision"] == semantic_revision
                        assert final_context_body["quality_level"] == "L2"

                        replica_projection: dict[str, object] | None = None
                        for _ in range(150):
                            projected = await admin_session.get(
                                "/admin/sources/commerce-edges/replicas"
                            )
                            if projected.status_code == 200:
                                candidate_projection = projected.json()
                                projected_replicas = {
                                    item["replica_id"]: item
                                    for item in candidate_projection["replicas"]
                                }
                                if set(projected_replicas) == {
                                    "commerce-runtime-a",
                                    "commerce-runtime-b",
                                } and all(
                                    item["status"] == "available"
                                    and item["drift"] == []
                                    and item["applied"]
                                    == {
                                        "enabled": True,
                                        "generation": l2_generation,
                                        "state_version": expected_state_version,
                                        "metadata_revision": semantic_revision,
                                    }
                                    for item in projected_replicas.values()
                                ):
                                    replica_projection = candidate_projection
                                    break
                            await asyncio.sleep(0.1)
                        assert replica_projection is not None, (
                            "both runtime replica observations did not converge to L2"
                        )
                        assert replica_projection["desired"] == {
                            "enabled": True,
                            "generation": l2_generation,
                            "state_version": expected_state_version,
                            "metadata_revision": semantic_revision,
                        }
                        query_b_final_context = await query_b_mcp.call_tool(
                            "get_context",
                            {
                                "source_id": "commerce-edges",
                                "question": verified_contract["question"],
                            },
                        )
                        assert query_b_final_context.structured_content == final_context_body
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

                        query_payload = {
                            "source_id": "commerce-edges",
                            "sql": verified_contract["sql"],
                            "metadata_revision": final_context_body["metadata_revision"],
                            "sql_policy_revision": final_context_body[
                                "sql_policy_revision"
                            ],
                        }
                        result = await query_a_mcp.call_tool("query", query_payload)
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

                        query_b_result = await query_b_mcp.call_tool(
                            "query", query_payload
                        )
                        query_b_result_body = query_b_result.structured_content
                        assert isinstance(query_b_result_body, dict)
                        for key in [
                            "metadata_revision",
                            "fingerprint",
                            "columns",
                            "rows",
                            "row_count",
                            "result_bytes",
                            "truncated",
                        ]:
                            assert query_b_result_body[key] == result_body[key]

                        http_result = await query_session.post(
                            "/query",
                            json=query_payload,
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

                        duplicate = await query_a_mcp.call_tool(
                            "query",
                            {
                                "source_id": "commerce-edges",
                                "sql": (
                                    'SELECT "OrderID" AS duplicate, '
                                    '"Status" AS duplicate FROM ai."Order" LIMIT 1'
                                ),
                                "metadata_revision": semantic_revision,
                                "sql_policy_revision": final_context_body[
                                    "sql_policy_revision"
                                ],
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
                            "/admin/sources/commerce-edges",
                            headers=_mutation_headers(
                                "acceptance/commerce-deactivate",
                                expected_generation,
                                expected_state_version,
                            ),
                        )
                        assert deactivated.status_code == 200, deactivated.text
                        (
                            _deactivated_result,
                            expected_generation,
                            expected_state_version,
                        ) = _successful_mutation(deactivated)
                        source_active = False
                        for _ in range(100):
                            listed_a = await query_a_mcp.call_tool("list_sources", {})
                            listed_b = await query_b_mcp.call_tool("list_sources", {})
                            missing_a = await query_a_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            missing_b = await query_b_mcp.call_tool(
                                "get_context",
                                {
                                    "source_id": "commerce-edges",
                                    "question": verified_contract["question"],
                                },
                            )
                            listed_a_body = listed_a.structured_content
                            listed_b_body = listed_b.structured_content
                            missing_a_body = missing_a.structured_content
                            missing_b_body = missing_b.structured_content
                            if (
                                isinstance(listed_a_body, dict)
                                and isinstance(listed_b_body, dict)
                                and listed_a_body == listed_b_body
                                and "commerce-edges"
                                not in {
                                    source["source_id"]
                                    for source in listed_a_body["sources"]
                                }
                                and isinstance(missing_a_body, dict)
                                and isinstance(missing_b_body, dict)
                                and missing_a_body == missing_b_body
                                and missing_a_body.get("error", {}).get("code")
                                == "SOURCE_NOT_FOUND"
                            ):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            pytest.fail(
                                "the open replica B MCP client retained the deactivated source"
                            )

                        disabled_projection: dict[str, object] | None = None
                        for _ in range(150):
                            projected = await admin_session.get(
                                "/admin/sources/commerce-edges/replicas"
                            )
                            if projected.status_code == 200:
                                candidate_projection = projected.json()
                                projected_replicas = candidate_projection["replicas"]
                                if len(projected_replicas) == 2 and all(
                                    item["status"] == "available"
                                    and item["drift"] == []
                                    and item["source_health"] is None
                                    and item["applied"]
                                    == {
                                        "enabled": False,
                                        "generation": expected_generation,
                                        "state_version": expected_state_version,
                                        "metadata_revision": None,
                                    }
                                    for item in projected_replicas
                                ):
                                    disabled_projection = candidate_projection
                                    break
                            await asyncio.sleep(0.1)
                        assert disabled_projection is not None, (
                            "both runtime replica observations did not converge to disabled"
                        )
                        assert disabled_projection["desired"] == {
                            "enabled": False,
                            "generation": expected_generation,
                            "state_version": expected_state_version,
                            "metadata_revision": None,
                        }
                    finally:
                        if source_active:
                            cleanup = await admin_session.delete(
                                "/admin/sources/commerce-edges",
                                headers=_mutation_headers(
                                    "acceptance/commerce-cleanup",
                                    expected_generation,
                                    expected_state_version,
                                ),
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
