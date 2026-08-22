from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from query_man.access import AccessPolicy, CallerContext
from query_man.catalog import PostgresCatalog
from query_man.errors import AppError, QueryTimeoutError
from query_man.gateway import GatewayService
from query_man.mcp_server import create_mcp_server
from query_man.metadata import MetadataService
from query_man.metadata_store import PostgresMetadataStore
from query_man.models import CatalogProvider
from query_man.query import PostgresQueryExecutor, QueryExecutor, QueryService
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig

logger = logging.getLogger("query_man")
_current_caller: contextvars.ContextVar[CallerContext | None] = contextvars.ContextVar(
    "query_man_current_caller",
    default=None,
)


class MetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=2_000)
    max_objects: int = Field(2, ge=1, le=4)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=80)
    sql: str = Field(min_length=1, max_length=100_000)
    metadata_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def build_app(
    runtime_config: RuntimeConfig,
    *,
    registry: SourceRegistry | None = None,
    catalog: CatalogProvider | None = None,
    query_executor: QueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> FastAPI:
    registry = registry or SourceRegistry.load(runtime_config.source_directory, runtime_config.budget_file)
    catalog = catalog or PostgresCatalog()
    metadata = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=runtime_config.metadata_cache_ttl_ms,
        max_stale_ms=runtime_config.metadata_max_stale_ms,
        refresh_retry_ms=runtime_config.metadata_retry_delay_ms,
        store=(
            PostgresMetadataStore(runtime_config.control_dsn)
            if runtime_config.control_dsn is not None
            else None
        ),
    )
    query_executor = query_executor or PostgresQueryExecutor()
    query_service = QueryService(registry, metadata, query_executor)
    source_ids = [source["source_id"] for source in registry.list()]
    if access_policy is None:
        if runtime_config.access_policy_file is not None:
            access_policy = AccessPolicy.load(runtime_config.access_policy_file, source_ids)
        elif runtime_config.api_token is not None:
            access_policy = AccessPolicy.legacy(runtime_config.api_token, source_ids)
        else:
            access_policy = AccessPolicy.local(source_ids)
    gateway = GatewayService(registry, metadata, query_service, access_policy)
    mcp_server = create_mcp_server(gateway, _mcp_caller)
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1_048_576,
        host=runtime_config.host,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
            try:
                await query_executor.close()
            finally:
                try:
                    await catalog.close()
                finally:
                    await metadata.close()

    app = FastAPI(title="query-man", lifespan=lifespan)
    app.state.registry = registry
    app.state.catalog = catalog
    app.state.metadata = metadata
    app.state.query_executor = query_executor
    app.state.query_service = query_service
    app.state.access_policy = access_policy
    app.state.gateway = gateway
    app.state.mcp_server = mcp_server

    @app.middleware("http")
    async def authenticate(request: Request, call_next: object) -> JSONResponse:
        if request.url.path != "/health":
            authorization = request.headers.get("authorization")
            received = (
                authorization[7:]
                if authorization is not None and authorization.startswith("Bearer ")
                else None
            )
            caller = access_policy.authenticate(received)
            if caller is None:
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                    content={
                        "error": {
                            "code": "UNAUTHORIZED",
                            "message": "A valid bearer token is required.",
                        }
                    },
                )
            request.state.caller = caller
            context_token = _current_caller.set(caller)
            try:
                return await call_next(request)  # type: ignore[operator, no-any-return]
            finally:
                _current_caller.reset(context_token)
        return await call_next(request)  # type: ignore[operator, no-any-return]

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {
                "path": ".".join(str(part) for part in issue["loc"] if part != "body"),
                "code": issue["type"],
                "message": issue["msg"],
            }
            for issue in error.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "The request body is invalid.",
                    "details": details,
                }
            },
        )

    @app.exception_handler(AppError)
    async def app_error(_request: Request, error: AppError) -> JSONResponse:
        if error.status_code >= 500:
            logger.error("%s: %s", error.code, error.message, exc_info=error)
        body: dict[str, object] = {"code": error.code, "message": error.message}
        if error.status_code < 500 and error.details is not None:
            body["details"] = error.details
        return JSONResponse(status_code=error.status_code, content={"error": body})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", exc_info=error)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An internal error occurred.",
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sources")
    async def sources(request: Request) -> dict[str, object]:
        return gateway.list_sources(_caller(request))

    @app.post("/meta")
    async def meta(payload: MetadataRequest, request: Request) -> dict[str, object]:
        return await gateway.get_context(
            _caller(request),
            payload.source_id,
            payload.question,
            payload.max_objects,
        )

    @app.post("/query")
    async def query(payload: QueryRequest, request: Request) -> dict[str, object]:
        pending = gateway.query(
            _caller(request),
            payload.source_id,
            payload.sql,
            payload.metadata_revision,
        )
        return await _until_disconnect(request, pending)

    @app.delete("/queries/{query_id}")
    async def cancel_query(query_id: uuid.UUID, request: Request) -> dict[str, str]:
        return await gateway.cancel_query(_caller(request), str(query_id))

    app.mount("/", mcp_app)

    return app


async def _until_disconnect(
    request: Request,
    pending: Coroutine[object, object, dict[str, object]],
) -> dict[str, object]:
    task: asyncio.Task[dict[str, object]] = asyncio.create_task(pending)
    disconnected = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _pending = await asyncio.wait(
            {task, disconnected},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise QueryTimeoutError
    finally:
        disconnected.cancel()
        with suppress(asyncio.CancelledError):
            await disconnected
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _caller(request: Request) -> CallerContext:
    caller: CallerContext = request.state.caller
    return caller


def _mcp_caller() -> CallerContext:
    caller = _current_caller.get()
    if caller is None:
        raise RuntimeError("MCP caller context is unavailable")
    return caller
