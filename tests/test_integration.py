from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

import httpx
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg_pool import PoolTimeout

from query_man.delivery.mcp_server import MCP_PROTOCOL_VERSION
from query_man.errors import QueryUnavailableError
from query_man.guarded_query.query import PostgresQueryExecutor
from query_man.guarded_query.sql_validation import ValidatedSql
from query_man.metadata.models import CatalogSnapshot
from query_man.runtime.composition import build_app
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import SafeJsonFormatter, operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


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
        del tenant_id
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


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
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


@pytest.mark.asyncio
async def test_rls_source_is_quarantined_before_pool_access() -> None:
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
    bounded_log = json.dumps(
        [
            {key: value for key, value in payload.items() if key != "timestamp"}
            for payload in log_payloads
        ]
    )
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


def test_injected_rls_registry_is_rejected_before_app_provider_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    registry = SourceRegistry([replace(source, tenant_isolation="rls")])
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
        build_app(_runtime_config(), registry=registry)

    assert provider_calls == 0


@pytest.mark.asyncio
async def test_socket_disconnect_cancels_http_query() -> None:
    executor = DisconnectExecutor()
    app = build_app(
        _runtime_config(),
        registry=load_test_registry(),
        catalog=DisconnectCatalog(),
        query_executor=executor,
    )

    async with _serve_test_app(app) as server_url:
        async with httpx.AsyncClient(base_url=server_url) as session:
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
        port = int(server_url.rsplit(":", 1)[1])
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


@pytest.mark.asyncio
async def test_mcp_disconnect_cancels_query_before_client_session_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = DisconnectExecutor()
    app = build_app(
        _runtime_config(),
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
