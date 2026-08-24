from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import UNSUPPORTED_PROTOCOL_VERSION
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from query_man.access import AccessPolicy, CallerContext
from query_man.catalog import PostgresCatalog
from query_man.errors import AppError, QueryTimeoutError
from query_man.gateway import GatewayService
from query_man.http_validation import is_json_content_type
from query_man.mcp_server import (
    MCP_PROTOCOL_VERSION,
    GetContextSuccessOutput,
    create_mcp_server,
)
from query_man.metadata import MetadataService
from query_man.metadata_store import PostgresMetadataStore
from query_man.models import CatalogProvider
from query_man.operations import operations
from query_man.query import PostgresQueryExecutor, QueryExecutor, QueryService
from query_man.registry import SourceRegistry, load_budget_profiles
from query_man.runtime_config import RuntimeConfig
from query_man.secrets import SourceSecretCipher
from query_man.source_admin import SourceAdminService, SourcePoolInvalidator, SourceReloader
from query_man.source_admin_routes import register_source_admin_routes, require_operator
from query_man.source_store import PostgresSourceStore
from query_man.verified import VerifiedQueryRegistry

logger = logging.getLogger("query_man")
audit_logger = logging.getLogger("query_man.audit")
mcp_logger = logging.getLogger("query_man.mcp")
_current_caller: contextvars.ContextVar[CallerContext | None] = contextvars.ContextVar(
    "query_man_current_caller",
    default=None,
)


def _unexpected_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred.",
            }
        },
    )


def _bounded_validation_details(
    error: RequestValidationError,
) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for issue in error.errors()[:32]:
        location = [part for part in issue.get("loc", ()) if part != "body"]
        issue_type = issue.get("type")
        if issue_type == "extra_forbidden" and location:
            location[-1] = "extra"
        safe_location: list[str] = []
        for part in location:
            if isinstance(part, int) and 0 <= part <= 1_000_000:
                safe_location.append(str(part))
            elif (
                isinstance(part, str)
                and 1 <= len(part) <= 64
                and all(
                    character.isascii()
                    and (character.isalnum() or character in {"_", "-"})
                    for character in part
                )
            ):
                safe_location.append(part)
            else:
                safe_location.append("value")
        details.append(
            {
                "path": ".".join(safe_location)[:256] or "request",
                "code": (
                    issue_type
                    if isinstance(issue_type, str)
                    and 1 <= len(issue_type) <= 64
                    and issue_type.isascii()
                    else "value_error"
                ),
                "message": "Invalid request value.",
            }
        )
    return details


class _MCPRequestLifecycleMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self._app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["mcp_http_request_id"] = request_id
        started = time.monotonic()
        response_started_ms: int | None = None
        response_bytes = 0
        status_code: int | None = None
        response_finished = False
        outcome = "error"
        operations.increment("mcp_http_request_started")

        async def send_with_timing(message: Message) -> None:
            nonlocal response_bytes, response_finished, response_started_ms, status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started_ms = round((time.monotonic() - started) * 1_000)
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True

        try:
            await self._app(scope, receive, send_with_timing)
            if response_finished and status_code is not None:
                outcome = "success" if status_code < 400 else "error"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            if response_started_ms is not None:
                raise
            logger.exception("Unhandled request error", exc_info=error)
            try:
                await _unexpected_error_response()(scope, receive, send_with_timing)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
        finally:
            duration_ms = round((time.monotonic() - started) * 1_000)
            extra: dict[str, object] = {
                "mcp_http_request_id": request_id,
                "duration_ms": duration_ms,
                "response_bytes": response_bytes,
                "outcome": outcome,
            }
            if response_started_ms is not None:
                extra["response_started_ms"] = response_started_ms
            if status_code is not None:
                extra["status_code"] = status_code
            mcp_logger.info("mcp_http_request_completed", extra=extra)
            operations.increment("mcp_http_request_completed")
            operations.observe("mcp_http_request_duration_ms", duration_ms)
            operations.observe("mcp_http_response_bytes", response_bytes)
            if response_started_ms is not None:
                operations.observe("mcp_http_response_started_ms", response_started_ms)
            if outcome == "cancelled":
                operations.increment("mcp_http_request_cancelled")
            elif outcome != "success":
                operations.increment("mcp_http_request_failed")


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
    sql_policy_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def build_app(
    runtime_config: RuntimeConfig,
    *,
    registry: SourceRegistry | None = None,
    catalog: CatalogProvider | None = None,
    query_executor: QueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> FastAPI:
    operations.reset()
    if runtime_config.source_mode == "managed":
        if registry is not None:
            raise ValueError("Managed source mode does not accept a bootstrap registry")
        registry = SourceRegistry([])
    elif registry is None:
        registry = SourceRegistry.load(runtime_config.source_directory, runtime_config.budget_file)
    operations.reconcile_sources(registry.source_ids())
    catalog = catalog or PostgresCatalog()
    source_ids = [source["source_id"] for source in registry.list()]
    verified_revisions = (
        VerifiedQueryRegistry.load(
            runtime_config.source_directory.parent / "verified-queries.yaml",
            set(source_ids),
        ).revision_map()
        if runtime_config.source_mode == "bootstrap"
        else {}
    )
    metadata_store: PostgresMetadataStore | None = None
    if runtime_config.source_mode == "managed":
        control_dsn = runtime_config.control_dsn
        if control_dsn is None:
            raise ValueError("Managed source mode requires a Control DB DSN")
        metadata_store = PostgresMetadataStore(control_dsn)
    metadata = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=runtime_config.metadata_cache_ttl_ms,
        max_stale_ms=runtime_config.metadata_max_stale_ms,
        refresh_retry_ms=runtime_config.metadata_retry_delay_ms,
        store=metadata_store,
        verified_revisions=verified_revisions,
    )
    query_executor = query_executor or PostgresQueryExecutor()
    query_service = QueryService(registry, metadata, query_executor)
    if access_policy is None:
        if runtime_config.access_policy_file is not None:
            access_policy = AccessPolicy.load(runtime_config.access_policy_file)
        elif runtime_config.api_token is not None:
            access_policy = AccessPolicy.legacy(runtime_config.api_token)
        else:
            access_policy = AccessPolicy.local()
    if runtime_config.source_mode == "managed":
        access_policy.require_shared_access()
    gateway = GatewayService(registry, metadata, query_service)
    source_store: PostgresSourceStore | None = None
    source_reloader: SourceReloader | None = None
    source_admin: SourceAdminService | None = None
    if runtime_config.source_mode == "managed":
        control_dsn = runtime_config.control_dsn
        encryption_key = runtime_config.source_encryption_key
        if control_dsn is None or encryption_key is None or metadata_store is None:
            raise ValueError("Managed source mode configuration is incomplete")
        source_store = PostgresSourceStore(control_dsn)
        cipher = SourceSecretCipher.from_base64(encryption_key)
        invalidators = tuple(
            cast(SourcePoolInvalidator, candidate)
            for candidate in (catalog, query_executor)
            if callable(getattr(candidate, "invalidate", None))
        )
        budgets = load_budget_profiles(runtime_config.budget_file)
        source_reloader = SourceReloader(
            registry,
            metadata,
            metadata_store,
            source_store,
            cipher,
            budgets,
            verified_revisions,
            invalidators,
        )
        source_admin = SourceAdminService(
            source_store,
            source_reloader,
            metadata,
            query_service,
            cipher,
            budgets,
            verified_revisions,
            PostgresCatalog,
        )
    mcp_server = create_mcp_server(gateway, _mcp_caller)
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1_048_576,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(runtime_config.mcp_allowed_hosts),
            allowed_origins=list(runtime_config.mcp_allowed_origins),
        ),
        host=runtime_config.host,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        reload_task: asyncio.Task[None] | None = None

        async def cleanup_step(
            step: str,
            cleanup: Callable[[], Awaitable[None]],
        ) -> None:
            try:
                await cleanup()
            except BaseException:
                logger.warning("startup_cleanup_step_failed step=%s", step)

        async def cleanup_failed_startup() -> None:
            if reload_task is not None:
                task = reload_task

                async def cancel_reload_task() -> None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

                await cleanup_step("reload_task", cancel_reload_task)

            cleanup_steps: list[tuple[str, object, Callable[[], Awaitable[None]]]] = [
                ("query_executor", query_executor, query_executor.close),
                ("catalog", catalog, catalog.close),
                ("metadata", metadata, metadata.close),
            ]
            if source_store is not None:
                cleanup_steps.append(("source_store", source_store, source_store.close))
            attempted_resources: set[int] = set()
            for step, resource, cleanup in cleanup_steps:
                resource_id = id(resource)
                if resource_id in attempted_resources:
                    continue
                attempted_resources.add(resource_id)
                await cleanup_step(step, cleanup)

        if source_reloader is not None:
            operations.set_component_health("source_reload", "initializing")
            await source_reloader.sync()
        operations.reconcile_sources(registry.source_ids())
        await _probe_registered_sources(registry, metadata)
        if source_reloader is not None:
            reload_task = asyncio.create_task(
                _reload_sources(source_reloader, runtime_config.source_reload_interval_ms)
            )
        child_entered = False
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                child_entered = True
                try:
                    yield
                finally:
                    operations.set_accepting(False)
                    if reload_task is not None:
                        reload_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await reload_task
                    drain = getattr(query_executor, "drain", None)
                    if callable(drain):
                        await drain(runtime_config.shutdown_grace_ms)
                    try:
                        await query_executor.close()
                    finally:
                        try:
                            await catalog.close()
                        finally:
                            try:
                                await metadata.close()
                            finally:
                                if source_store is not None:
                                    await source_store.close()
        except BaseException:
            if not child_entered:
                await cleanup_failed_startup()
            raise

    app = FastAPI(title="query-man", lifespan=lifespan)
    app.state.registry = registry
    app.state.catalog = catalog
    app.state.metadata = metadata
    app.state.query_executor = query_executor
    app.state.query_service = query_service
    app.state.access_policy = access_policy
    app.state.gateway = gateway
    app.state.mcp_server = mcp_server
    app.state.source_admin = source_admin
    app.state.source_reloader = source_reloader

    @app.middleware("http")
    async def authenticate(request: Request, call_next: object) -> Response:
        if (
            operations.public_status() == "shutting_down"
            and request.url.path not in {"/health", "/ready"}
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "SERVICE_SHUTTING_DOWN",
                        "message": "The service is shutting down.",
                    }
                },
            )
        if request.url.path not in {"/health", "/ready"}:
            authorizations = request.headers.getlist("authorization")
            authorization = authorizations[0] if len(authorizations) == 1 else None
            received = (
                authorization[7:]
                if authorization is not None and authorization.startswith("Bearer ")
                else None
            )
            caller = access_policy.authenticate(received) if len(authorizations) <= 1 else None
            if caller is None:
                audit_logger.warning(
                    "authentication_failed method=%s",
                    request.method,
                )
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
                content_types = request.headers.getlist("content-type")
                if (
                    request.url.path == "/mcp"
                    and request.method == "POST"
                    and (
                        len(content_types) != 1
                        or not is_json_content_type(content_types[0])
                    )
                ):
                    return Response(
                        "Invalid Content-Type header",
                        status_code=400,
                        media_type="text/plain",
                    )
                protocol_versions = request.headers.getlist("mcp-protocol-version")
                if (
                    request.url.path == "/mcp"
                    and request.method == "POST"
                    and protocol_versions != [MCP_PROTOCOL_VERSION]
                ):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": UNSUPPORTED_PROTOCOL_VERSION,
                                "message": "Unsupported protocol version",
                                "data": {"supported": [MCP_PROTOCOL_VERSION]},
                            },
                        },
                    )
                return await call_next(request)  # type: ignore[operator, no-any-return]
            finally:
                _current_caller.reset(context_token)
        return await call_next(request)  # type: ignore[operator, no-any-return]

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "The request body is invalid.",
                    "details": _bounded_validation_details(error),
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
        return _unexpected_error_response()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness() -> JSONResponse:
        status = operations.public_status()
        return JSONResponse(
            status_code=200 if status in {"ready", "degraded"} else 503,
            content={"status": status},
        )

    @app.get("/admin/health")
    async def detailed_health(request: Request) -> dict[str, object]:
        require_operator(request)
        snapshot = operations.snapshot()
        return {
            "status": operations.public_status(),
            "accepting": snapshot["accepting"],
            "sources": snapshot["sources"],
            "components": snapshot["components"],
        }

    @app.get("/admin/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        require_operator(request)
        return operations.snapshot()

    @app.get("/sources")
    async def sources(request: Request) -> dict[str, object]:
        return gateway.list_sources(_caller(request))

    @app.post("/meta")
    async def meta(
        payload: MetadataRequest,
        request: Request,
    ) -> GetContextSuccessOutput:
        return GetContextSuccessOutput.model_validate(
            await gateway.get_context(
                _caller(request),
                payload.source_id,
                payload.question,
                payload.max_objects,
            )
        )

    @app.post("/query")
    async def query(payload: QueryRequest, request: Request) -> dict[str, object]:
        pending = gateway.query(
            _caller(request),
            payload.source_id,
            payload.sql,
            payload.metadata_revision,
            payload.sql_policy_revision,
        )
        return await _until_disconnect(request, pending)

    @app.delete("/queries/{query_id}")
    async def cancel_query(query_id: str, request: Request) -> dict[str, str]:
        caller = require_operator(request)
        try:
            parsed_query_id = uuid.UUID(query_id)
        except ValueError as error:
            raise RequestValidationError(
                [
                    {
                        "type": "uuid_parsing",
                        "loc": ("path", "query_id"),
                        "msg": "Invalid UUID.",
                        "input": query_id,
                    }
                ]
            ) from error
        return await gateway.cancel_query(caller, str(parsed_query_id))

    register_source_admin_routes(app)
    app.add_middleware(_MCPRequestLifecycleMiddleware)
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


async def _reload_sources(reloader: SourceReloader, interval_ms: int) -> None:
    while True:
        await asyncio.sleep(interval_ms / 1_000)
        await reloader.sync()


async def _probe_registered_sources(
    registry: SourceRegistry,
    metadata: MetadataService,
) -> None:
    async def probe(source_id: str) -> None:
        source = registry.get(source_id)
        if source is None:
            return
        try:
            async with asyncio.timeout(
                max(1, source.budget.metadata_statement_timeout_ms) / 1_000
            ):
                await metadata.get_published(source_id)
        except Exception:
            operations.increment("startup_metadata_probe_failed", source_id)
            operations.set_source_health(source_id, "unavailable")
            logger.exception("startup_metadata_probe_failed source_id=%s", source_id)

    await asyncio.gather(*(probe(source_id) for source_id in registry.source_ids()))
