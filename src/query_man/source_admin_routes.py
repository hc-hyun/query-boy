from __future__ import annotations

import json
import logging
import math
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from query_man.access import CallerContext
from query_man.errors import OperatorRequiredError, SourceControlUnavailableError
from query_man.http_validation import is_json_content_type
from query_man.models import SourceEnvironment
from query_man.registry import Identifier, StableSlug
from query_man.source_admin import (
    CONTROL_SEQUENCE_MAX,
    MutationContext,
    PublishVerifiedQueryInput,
    SourceAdminService,
    VerifiedExpectedInput,
)

audit_logger = logging.getLogger("query_man.audit")
_router = APIRouter()
_MAX_MUTATION_BODY_BYTES = 1_048_576
_MAX_JSON_OBJECT_MEMBERS = 1_024
_MAX_VALIDATION_ISSUES = 32
_IDEMPOTENCY_KEY_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_REASON_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class SourcePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, object]
    credential: SecretStr = Field(min_length=1, max_length=1_024)


class SourceCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: SecretStr = Field(min_length=1, max_length=1_024)


class VerifiedExpectedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1, max_length=1_600)
    row_count: int = Field(ge=0, le=100_000)
    result_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class VerifiedQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    question: str = Field(min_length=1, max_length=2_000)
    metadata_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    relations: list[str] = Field(min_length=1, max_length=100)
    sql: str = Field(min_length=1, max_length=100_000)
    expected: VerifiedExpectedRequest


class SourceListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    limit: int = Field(50, ge=1, le=100)
    after_source_id: StableSlug | None = None
    enabled: bool | None = None
    owner: StableSlug | None = None
    environment: SourceEnvironment | None = None
    budget_profile: Identifier | None = None


class SourceHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    limit: int = Field(50, ge=1, le=100)
    before_generation: int | None = Field(None, ge=1, le=CONTROL_SEQUENCE_MAX)


class SourceDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMutationHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    limit: int = Field(50, ge=1, le=100)
    before_event_id: int | None = Field(None, ge=1, le=CONTROL_SEQUENCE_MAX)


class SourcePathParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: StableSlug


class MutationPathParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)


class RollbackPathParameters(SourcePathParameters):
    generation: int = Field(ge=1, le=CONTROL_SEQUENCE_MAX)


class MutationHeaders(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    reason: str = Field(pattern=_REASON_PATTERN)
    expected_generation: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,18})$")
    expected_state_version: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,18})$")
    expected_metadata_revision: str | None = Field(
        None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )


def _parse_query_parameters[QueryParameters: BaseModel](
    request: Request,
    model: type[QueryParameters],
) -> QueryParameters:
    values: dict[str, object] = {}
    for key, value in request.query_params.multi_items():
        previous = values.get(key)
        values[key] = [previous, value] if previous is not None else value
    try:
        return model.model_validate(values)
    except ValidationError as error:
        issues = [
            {**issue, "loc": ("query", *issue["loc"])}
            for issue in error.errors()
        ]
        raise RequestValidationError(issues) from error


def _parse_source_id(source_id: str) -> str:
    try:
        return SourcePathParameters.model_validate({"source_id": source_id}).source_id
    except ValidationError as error:
        issues = [
            {**issue, "loc": ("path", *issue["loc"])}
            for issue in error.errors()
        ]
        raise RequestValidationError(issues) from error


def _parse_mutation_key(idempotency_key: str) -> str:
    try:
        return MutationPathParameters.model_validate(
            {"idempotency_key": idempotency_key}
        ).idempotency_key
    except ValidationError as error:
        raise _request_validation_error(error, "path") from error


def _parse_rollback_path(source_id: str, generation: str) -> tuple[str, int]:
    try:
        parsed = RollbackPathParameters.model_validate(
            {"source_id": source_id, "generation": generation}
        )
    except ValidationError as error:
        raise _request_validation_error(error, "path") from error
    return parsed.source_id, parsed.generation


def _parse_mutation_headers(
    request: Request,
    caller: CallerContext,
    *,
    allow_absent_source: bool = False,
    require_metadata_revision: bool = False,
) -> MutationContext:
    header_names = {
        "idempotency_key": "idempotency-key",
        "reason": "x-query-man-reason",
        "expected_generation": "x-expected-generation",
        "expected_state_version": "x-expected-state-version",
        "expected_metadata_revision": "x-expected-metadata-revision",
    }
    values: dict[str, object] = {}
    for field, header_name in header_names.items():
        received = request.headers.getlist(header_name)
        if len(received) == 1:
            values[field] = received[0]
        elif received:
            values[field] = received
    try:
        parsed = MutationHeaders.model_validate(values)
    except ValidationError as error:
        raise _request_validation_error(error, "header") from error
    generation = int(parsed.expected_generation)
    state_version = int(parsed.expected_state_version)
    if generation > CONTROL_SEQUENCE_MAX or state_version > CONTROL_SEQUENCE_MAX:
        raise _invalid_request("header", "expected_state", "value_error")
    if (generation == 0) != (state_version == 0):
        raise _invalid_request("header", "expected_state", "value_error")
    if not allow_absent_source and generation == 0:
        raise _invalid_request("header", "expected_state", "value_error")
    if require_metadata_revision != (parsed.expected_metadata_revision is not None):
        raise _invalid_request(
            "header",
            "x-expected-metadata-revision",
            "value_error",
        )
    return MutationContext(
        idempotency_key=parsed.idempotency_key,
        actor=caller.caller_id,
        reason=parsed.reason,
        expected_generation=generation,
        expected_state_version=state_version,
        expected_metadata_revision=parsed.expected_metadata_revision,
    )


async def _parse_json_body[RequestModel: BaseModel](
    request: Request,
    model: type[RequestModel],
) -> RequestModel:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or not is_json_content_type(content_types[0]):
        raise _invalid_request("header", "content-type", "value_error")
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_MUTATION_BODY_BYTES - len(body):
            raise _invalid_request("body", "body", "too_long")
        body.extend(chunk)
    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
        return model.model_validate(document)
    except ValidationError as error:
        raise _request_validation_error(error, "body") from error
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise _invalid_request("body", "body", "json_invalid") from error


async def _require_empty_body(request: Request) -> None:
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size:
            raise _invalid_request("body", "body", "value_error")


def _request_validation_error(error: ValidationError, location: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {**issue, "loc": (location, *issue["loc"])}
            for issue in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )[:_MAX_VALIDATION_ISSUES]
        ]
    )


def _invalid_request(location: str, field: str, code: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": code,
                "loc": (location, field),
                "msg": "Invalid request value",
                "input": None,
            }
        ]
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON numbers must be finite")
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > _MAX_JSON_OBJECT_MEMBERS:
        raise ValueError("JSON object has too many members")
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("JSON object member names must be unique")
        document[key] = value
    return document


def _source_admin(request: Request) -> SourceAdminService:
    admin = cast(SourceAdminService | None, request.app.state.source_admin)
    if admin is None:
        raise SourceControlUnavailableError
    return admin


def require_operator(request: Request) -> CallerContext:
    caller: CallerContext = request.state.caller
    if not caller.operator:
        audit_logger.warning(
            "authorization_denied caller_id=%s tenant_id=%s operation=source_admin",
            caller.caller_id,
            caller.tenant_id,
        )
        raise OperatorRequiredError
    return caller


@_router.get("/admin/sources")
async def list_admin_sources(request: Request) -> dict[str, object]:
    require_operator(request)
    parameters = _parse_query_parameters(request, SourceListQuery)
    return await _source_admin(request).list_sources(
        limit=parameters.limit,
        after_source_id=parameters.after_source_id,
        enabled=parameters.enabled,
        owner=parameters.owner,
        environment=parameters.environment,
        budget_profile=parameters.budget_profile,
    )


@_router.get("/admin/sources/{source_id}/history")
async def admin_source_history(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    require_operator(request)
    source_id = _parse_source_id(source_id)
    parameters = _parse_query_parameters(request, SourceHistoryQuery)
    return await _source_admin(request).source_history(
        source_id,
        limit=parameters.limit,
        before_generation=parameters.before_generation,
    )


@_router.get("/admin/sources/{source_id}/mutations")
async def admin_source_mutations(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    require_operator(request)
    source_id = _parse_source_id(source_id)
    parameters = _parse_query_parameters(request, SourceMutationHistoryQuery)
    return await _source_admin(request).source_mutations(
        source_id,
        limit=parameters.limit,
        before_event_id=parameters.before_event_id,
    )


@_router.get("/admin/mutations/{idempotency_key}")
async def get_admin_mutation(
    idempotency_key: str,
    request: Request,
) -> dict[str, object]:
    require_operator(request)
    idempotency_key = _parse_mutation_key(idempotency_key)
    _parse_query_parameters(request, SourceDetailQuery)
    return await _source_admin(request).get_mutation(idempotency_key)


@_router.get("/admin/sources/{source_id}")
async def get_admin_source(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    return await _source_admin(request).get_source(source_id)


@_router.put("/admin/sources/{source_id}")
async def publish_source(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    caller = require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(
        request,
        caller,
        allow_absent_source=True,
    )
    payload = await _parse_json_body(request, SourcePublishRequest)
    return await _source_admin(request).publish(
        source_id,
        payload.manifest,
        payload.credential.get_secret_value(),
        mutation,
    )


@_router.post("/admin/sources/{source_id}/credential")
async def rotate_source_credential(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    caller = require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(request, caller)
    payload = await _parse_json_body(request, SourceCredentialRequest)
    return await _source_admin(request).rotate_credential(
        source_id,
        payload.credential.get_secret_value(),
        mutation,
    )


@_router.post("/admin/sources/{source_id}/verified-queries")
async def publish_verified_query(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    caller = require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(request, caller)
    payload = await _parse_json_body(request, VerifiedQueryRequest)
    return await _source_admin(request).publish_verified_query(
        PublishVerifiedQueryInput(
            query_id=payload.query_id,
            source_id=source_id,
            question=payload.question,
            sql=payload.sql,
            metadata_revision=payload.metadata_revision,
            relations=tuple(payload.relations),
            expected=VerifiedExpectedInput(
                columns=tuple(payload.expected.columns),
                row_count=payload.expected.row_count,
                result_hash=payload.expected.result_hash,
            ),
        ),
        caller.tenant_id,
        mutation,
    )


@_router.post("/admin/sources/{source_id}/rollback/{generation}")
async def rollback_source(
    source_id: str,
    generation: str,
    request: Request,
) -> dict[str, object]:
    caller = require_operator(request)
    source_id, parsed_generation = _parse_rollback_path(source_id, generation)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(request, caller)
    await _require_empty_body(request)
    return await _source_admin(request).rollback(
        source_id,
        parsed_generation,
        mutation,
    )


@_router.post("/admin/sources/{source_id}/metadata/resume")
async def resume_source_metadata_publish(
    source_id: str,
    request: Request,
) -> dict[str, object]:
    caller = require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(
        request,
        caller,
        require_metadata_revision=True,
    )
    await _require_empty_body(request)
    return await _source_admin(request).resume_automatic_publish(source_id, mutation)


@_router.delete("/admin/sources/{source_id}")
async def deactivate_source(source_id: str, request: Request) -> dict[str, object]:
    caller = require_operator(request)
    source_id = _parse_source_id(source_id)
    _parse_query_parameters(request, SourceDetailQuery)
    mutation = _parse_mutation_headers(request, caller)
    await _require_empty_body(request)
    return await _source_admin(request).deactivate(source_id, mutation)


def register_source_admin_routes(app: FastAPI) -> None:
    app.include_router(_router)
