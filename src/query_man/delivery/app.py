from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from query_man.delivery.access import AccessPolicy, CallerContext, caller_audit_fields
from query_man.delivery.authentication import (
    AccessPolicyBearerAuthenticator,
    InvalidBearerTokenError,
)
from query_man.delivery.gateway import GatewayService
from query_man.errors import AppError, OperatorRequiredError, QueryTimeoutError
from query_man.runtime.operations import operations

logger = logging.getLogger("query_man")
audit_logger = logging.getLogger("query_man.audit")


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


class MetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=80)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=80)
    sql: str = Field(min_length=1, max_length=100_000)
    metadata_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sql_policy_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def build_http_app(
    *,
    access_policy: AccessPolicy,
    gateway: GatewayService,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
) -> FastAPI:
    authenticator = AccessPolicyBearerAuthenticator(access_policy)
    app = FastAPI(title="query-man", lifespan=lifespan)

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
            received = _bearer_token(authorization) if len(authorizations) == 1 else None
            try:
                if len(authorizations) > 1:
                    raise InvalidBearerTokenError
                caller = await authenticator.authenticate(received)
            except InvalidBearerTokenError:
                audit_logger.warning(
                    "authentication_failed method=%s",
                    request.method,
                )
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                    content={
                        "error": {
                            "code": "UNAUTHORIZED",
                            "message": "A valid bearer token is required.",
                        }
                    },
                )
            request.state.caller = caller
            return await call_next(request)  # type: ignore[operator, no-any-return]
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
        headers = (
            {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}
            if isinstance(error, OperatorRequiredError)
            else None
        )
        return JSONResponse(status_code=error.status_code, headers=headers, content={"error": body})

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
        _require_operator(request)
        snapshot = operations.snapshot()
        return {
            "status": operations.public_status(),
            "accepting": snapshot["accepting"],
            "sources": snapshot["sources"],
        }

    @app.get("/admin/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        _require_operator(request)
        return operations.snapshot()

    @app.get("/sources")
    async def sources(request: Request) -> dict[str, object]:
        return gateway.list_sources(_caller(request))

    @app.post("/meta")
    async def meta(
        payload: MetadataRequest,
        request: Request,
    ) -> dict[str, object]:
        return await gateway.get_context(_caller(request), payload.source_id)

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
        caller = _require_operator(request)
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

def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token or any(character.isspace() for character in token):
        return None
    return token


def _require_operator(request: Request) -> CallerContext:
    caller = _caller(request)
    if not caller.operator:
        audit_logger.warning(
            "authorization_denied",
            extra={**caller_audit_fields(caller), "operation": "source_admin"},
        )
        raise OperatorRequiredError
    return caller
