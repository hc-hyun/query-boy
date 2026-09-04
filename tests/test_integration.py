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
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg_pool import PoolTimeout

from query_man.delivery.access import AccessPolicy
from query_man.delivery.app import build_http_app
from query_man.delivery.gateway import GatewayService
from query_man.errors import QueryUnavailableError
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.guarded_query.sql_validation import ValidatedSql
from query_man.metadata.models import CatalogSnapshot
from query_man.metadata.service import MetadataService
from query_man.runtime.operations import SafeJsonFormatter, operations
from query_man.source_catalog.models import SourceProfile
from tests.helpers import load_test_registry, minimal_development_snapshot


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
        snapshot = minimal_development_snapshot()
        return replace(
            snapshot,
            relations=tuple(
                replace(relation, comment="Description")
                for relation in snapshot.relations
            ),
        )

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

@asynccontextmanager
async def _empty_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


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


@pytest.mark.asyncio
async def test_socket_disconnect_cancels_http_query() -> None:
    registry = load_test_registry()
    catalog = DisconnectCatalog()
    metadata = MetadataService(registry, catalog)  # type: ignore[arg-type]
    executor = DisconnectExecutor()
    queries = QueryService(registry, metadata, executor)  # type: ignore[arg-type]
    gateway = GatewayService(registry, metadata, queries)
    app = build_http_app(
        access_policy=AccessPolicy.local(),
        gateway=gateway,
        lifespan=_empty_lifespan,
    )

    async with _serve_test_app(app) as server_url:
        async with httpx.AsyncClient(base_url=server_url) as session:
            context = await session.post(
                "/meta",
                json={"source_id": "development-issues"},
            )
            context.raise_for_status()
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
