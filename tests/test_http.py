from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from query_man.access import AccessPolicy
from query_man.app import build_app
from query_man.errors import (
    MutationNotFoundError,
    QueryInvalidError,
    QueryUnavailableError,
    SourceControlUnavailableError,
    SourceNotFoundError,
)
from query_man.mcp_server import MCP_PROTOCOL_VERSION
from query_man.models import (
    CatalogSnapshot,
    ResourceObservation,
    RuntimeCatalogProvider,
    SourceProfile,
)
from query_man.operations import operations
from query_man.query import RuntimeQueryExecutor
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
from query_man.source_admin import (
    CONTROL_SEQUENCE_MAX,
    MutationContext,
    PublishVerifiedQueryInput,
    VerifiedExpectedInput,
)
from query_man.sql_validation import SQL_POLICY_REVISION, ValidatedSql
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


class NeverCalledCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        raise RuntimeError("Catalog should not be called")

    async def close(self) -> None:
        pass

    async def invalidate(self, _source_id: str) -> None:
        pass

    async def observe_resources(
        self,
        _source: SourceProfile,
    ) -> ResourceObservation:
        raise RuntimeError("Resource observation should not be called")


class FailingCatalog(NeverCalledCatalog):
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        raise RuntimeError("connect to 10.20.30.40 with super-secret-password failed")


class ReturningCatalog(NeverCalledCatalog):
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot


class PartiallyHangingCatalog(ReturningCatalog):
    async def load(self, source: SourceProfile) -> CatalogSnapshot:
        if source.source_id == "development-issues":
            return self.snapshot
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FailingAccessPolicy:
    def authenticate(self, _token: str | None) -> None:
        raise RuntimeError("authentication dependency failed")


class RecordingQueryExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.budgets: list[object] = []
        self.cancel_calls: list[str] = []
        self.cancel_result = False
        self.lifecycle: list[str] = []

    async def execute(
        self,
        source: SourceProfile,
        _sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((source.source_id, validated.fingerprint, tenant_id))
        self.budgets.append(source.budget)
        return {
            "status": "ok",
            "query_id": query_id or "test-query-id",
            "metadata_revision": metadata_revision,
            "fingerprint": validated.fingerprint,
            "columns": ["issue_count"],
            "rows": [{"issue_count": 600}],
            "row_count": 1,
            "result_bytes": 21,
            "truncated": False,
            "queue_ms": 0,
            "elapsed_ms": 1,
            "plan_summary": {"total_cost": 10.0, "max_rows": 1, "node_count": 2},
        }

    async def close(self) -> None:
        self.lifecycle.append("close")

    def stop_accepting(self) -> None:
        self.lifecycle.append("stop_accepting")

    async def drain(self, grace_ms: int) -> None:
        self.stop_accepting()
        self.lifecycle.append(f"drain:{grace_ms}")

    async def invalidate(self, _source_id: str) -> None:
        pass

    async def cancel(self, query_id: str) -> bool:
        self.cancel_calls.append(query_id)
        return self.cancel_result


class UnavailableQueryExecutor(RecordingQueryExecutor):
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
        raise QueryUnavailableError from RuntimeError(
            "Unsupported PostgreSQL result type: private oid 3802"
        )


_QUERY_A_TOKEN = "query-a-token-value-with-at-least-32-characters"
_QUERY_B_TOKEN = "query-b-token-value-with-at-least-32-characters"
_ADMIN_TOKEN = "admin-token-value-with-at-least-32-characters"
_MUTATION_KEYS = (
    "4a3fcf7e-818f-4b68-9d31-21d392a28701",
    "4a3fcf7e-818f-4b68-9d31-21d392a28702",
    "4a3fcf7e-818f-4b68-9d31-21d392a28703",
    "4a3fcf7e-818f-4b68-9d31-21d392a28704",
    "4a3fcf7e-818f-4b68-9d31-21d392a28705",
    "4a3fcf7e-818f-4b68-9d31-21d392a28706",
)
_UNKNOWN_MUTATION_KEY = "d2e42ce6-c8f4-49e3-9107-e744db9c43bf"
_UNAVAILABLE_MUTATION_KEY = "ee4b5cc3-1688-4c6f-930c-9844be367c81"


def _operator_mutation_headers(
    idempotency_key: str,
    *,
    reason: str = "ctrl-205",
    expected_generation: int = 7,
    expected_state_version: int = 9,
    expected_metadata_revision: str | None = None,
) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {_ADMIN_TOKEN}",
        "idempotency-key": idempotency_key,
        "x-query-man-reason": reason,
        "x-expected-generation": str(expected_generation),
        "x-expected-state-version": str(expected_state_version),
    }
    if expected_metadata_revision is not None:
        headers["x-expected-metadata-revision"] = expected_metadata_revision
    return headers


def _shared_access_policy(tmp_path: Path) -> AccessPolicy:
    policy_path = tmp_path / "shared-access.yaml"
    policy_path.write_text(
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
        policy_path,
        {
            "QUERY_A_TOKEN": _QUERY_A_TOKEN,
            "QUERY_B_TOKEN": _QUERY_B_TOKEN,
            "ADMIN_TOKEN": _ADMIN_TOKEN,
        },
    )


def test_meta_openapi_declares_revisions_and_sql_capabilities() -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
    )

    schema = app.openapi()
    response_schema = schema["paths"]["/meta"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/GetContextSuccessOutput"}
    success = schema["components"]["schemas"]["GetContextSuccessOutput"]
    assert success["required"] == [
        "metadata_revision",
        "sql_policy_revision",
        "sql_capabilities",
    ]
    capabilities = schema["components"]["schemas"]["SqlCapabilitiesOutput"]
    assert capabilities["required"] == [
        "functions",
        "cast_types",
        "unqualified_cast_types",
    ]
    assert all(
        capabilities["properties"][name]["items"] == {"type": "string"}
        for name in capabilities["required"]
    )


def runtime_config(api_token: str | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=api_token,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )


def client(
    catalog: RuntimeCatalogProvider,
    api_token: str | None = None,
    query_executor: RuntimeQueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> httpx.AsyncClient:
    app = build_app(
        runtime_config(api_token),
        registry=load_test_registry(),
        catalog=catalog,
        query_executor=query_executor,
        access_policy=access_policy,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


class RecordingSourceAdmin:
    def __init__(self) -> None:
        self.list_calls: list[tuple[object, ...]] = []
        self.detail_calls: list[str] = []
        self.history_calls: list[tuple[object, ...]] = []
        self.replica_calls: list[tuple[object, ...]] = []
        self.usage_calls: list[str] = []
        self.receipt_calls: list[str] = []
        self.source_mutation_calls: list[tuple[object, ...]] = []
        self.mutation_calls: list[tuple[object, ...]] = []

    async def list_sources(
        self,
        limit: int = 50,
        after_source_id: str | None = None,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> dict[str, object]:
        self.list_calls.append(
            (
                limit,
                after_source_id,
                enabled,
                owner,
                environment,
                budget_profile,
            )
        )
        return {"sources": [], "next_after_source_id": None}

    async def get_source(self, source_id: str) -> dict[str, object]:
        self.detail_calls.append(source_id)
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private control database detail"
            )
        return {"source_id": source_id, "connection": {"ssl": True}}

    async def source_history(
        self,
        source_id: str,
        limit: int = 50,
        before_generation: int | None = None,
    ) -> dict[str, object]:
        self.history_calls.append((source_id, limit, before_generation))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        return {
            "source_id": source_id,
            "current": {"generation": 2},
            "generations": [],
            "next_before_generation": None,
        }

    async def source_replicas(
        self,
        source_id: str,
        limit: int = 50,
        after_replica_id: str | None = None,
    ) -> dict[str, object]:
        self.replica_calls.append((source_id, limit, after_replica_id))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private replica observation detail"
            )
        replicas: list[dict[str, object]] = []
        if source_id == "known-source":
            replicas.append(
                {
                    "replica_id": "replica-alpha",
                    "status": "available",
                    "source_health": "healthy",
                    "applied": {
                        "enabled": True,
                        "generation": 7,
                        "state_version": 9,
                        "metadata_revision": f"sha256:{'1' * 64}",
                    },
                    "drift": [],
                    "observed_at": "2026-08-25T12:00:00+00:00",
                    "fresh_until": "2026-08-25T12:00:15+00:00",
                    "stale_age_ms": 0,
                    "reason_code": None,
                }
            )
        elif source_id == "replica-status-source":
            replicas.extend(
                [
                    {
                        "replica_id": "replica-stale",
                        "status": "stale",
                        "source_health": "healthy",
                        "applied": {
                            "enabled": True,
                            "generation": 6,
                            "state_version": 8,
                            "metadata_revision": f"sha256:{'2' * 64}",
                        },
                        "drift": ["generation", "state_version", "metadata_revision"],
                        "observed_at": "2026-08-25T11:59:00+00:00",
                        "fresh_until": "2026-08-25T11:59:15+00:00",
                        "stale_age_ms": 45_000,
                        "reason_code": "HEARTBEAT_EXPIRED",
                    },
                    {
                        "replica_id": "replica-unavailable",
                        "status": "unavailable",
                        "source_health": None,
                        "applied": None,
                        "drift": ["not_applied"],
                        "observed_at": "2026-08-25T12:00:00+00:00",
                        "fresh_until": "2026-08-25T12:00:15+00:00",
                        "stale_age_ms": 0,
                        "reason_code": "CONTROL_SCAN_FAILED",
                    },
                ]
            )
        return {
            "source_id": source_id,
            "desired": {
                "enabled": True,
                "generation": 7,
                "state_version": 9,
                "metadata_revision": f"sha256:{'1' * 64}",
            },
            "replicas": replicas,
            "next_after_replica_id": None,
        }

    async def source_usage(self, source_id: str) -> dict[str, object]:
        self.usage_calls.append(source_id)
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private usage projection detail"
            )
        return {
            "source_id": source_id,
            "enabled": True,
            "read_at": "2026-08-25T12:00:00+00:00",
            "resource": {
                "status": "pending",
                "reason_code": "NOT_OBSERVED",
                "last_attempt": None,
                "fresh_until": None,
                "metrics": [],
            },
            "gateway": {
                "status": "pending",
                "reason_code": "NOT_REPORTED",
                "last_report_at": None,
                "fresh_until": None,
                "lower_bound": True,
                "window_start": "2026-07-25T12:00:00+00:00",
                "window_end": "2026-08-25T12:00:00+00:00",
                "rollups": [],
            },
            "monetary_cost": {
                "status": "not_configured",
                "reason_code": "PROVIDER_NOT_CONFIGURED",
                "last_attempt": None,
            },
        }

    async def get_mutation(self, idempotency_key: str) -> dict[str, object]:
        self.receipt_calls.append(idempotency_key)
        if idempotency_key == _UNKNOWN_MUTATION_KEY:
            raise MutationNotFoundError
        if idempotency_key == _UNAVAILABLE_MUTATION_KEY:
            raise SourceControlUnavailableError from RuntimeError(
                "private mutation receipt detail"
            )
        return {
            "idempotency_key": idempotency_key,
            "outcome": "succeeded",
        }

    async def source_mutations(
        self,
        source_id: str,
        limit: int = 50,
        before_event_id: int | None = None,
    ) -> dict[str, object]:
        self.source_mutation_calls.append((source_id, limit, before_event_id))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private source mutation detail"
            )
        return {
            "source_id": source_id,
            "mutations": [],
            "next_before_event_id": 41,
        }

    async def publish(
        self,
        source_id: str,
        manifest: object,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("publish", source_id, manifest, credential, mutation)
        )
        return {"status": "published", "source_id": source_id}

    async def rotate_credential(
        self,
        source_id: str,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("rotate_credential", source_id, credential, mutation)
        )
        return {"status": "credential_rotated", "source_id": source_id}

    async def publish_verified_query(
        self,
        query: PublishVerifiedQueryInput,
        tenant_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("publish_verified_query", query, tenant_id, mutation)
        )
        return {"status": "verified", "source_id": query.source_id}

    async def rollback(
        self,
        source_id: str,
        generation: int,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("rollback", source_id, generation, mutation)
        )
        return {"status": "rolled_back", "source_id": source_id}

    async def resume_automatic_publish(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(("resume", source_id, mutation))
        return {"status": "resumed", "source_id": source_id}

    async def deactivate(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(("deactivate", source_id, mutation))
        return {"status": "deactivated", "source_id": source_id}


@pytest.mark.asyncio
async def test_lists_sources_without_connection_information() -> None:
    async with client(NeverCalledCatalog()) as session:
        response = await session.get("/sources")
    assert response.status_code == 200
    assert len(response.json()["sources"]) == 2
    assert "password" not in response.text
    assert "development_issues_reader" not in response.text


@pytest.mark.asyncio
async def test_mcp_transport_rejects_untrusted_host_and_origin() -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as session:
            untrusted_host = await session.post(
                "/mcp",
                headers={
                    "host": "attacker.invalid",
                    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                    "mcp-method": "server/discover",
                },
                json={},
            )
            untrusted_origin = await session.post(
                "/mcp",
                headers={
                    "host": "127.0.0.1:3000",
                    "origin": "https://attacker.invalid",
                    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                    "mcp-method": "server/discover",
                },
                json={},
            )

    assert untrusted_host.status_code == 421
    assert untrusted_origin.status_code == 403


@pytest.mark.asyncio
async def test_mcp_transport_requires_exact_json_media_type() -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
    )
    discover = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "transport-test",
                    "version": "1",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    modern_headers = {
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        "mcp-method": "server/discover",
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:3000",
        ) as session:
            accepted = await session.post(
                "/mcp",
                headers={
                    **modern_headers,
                    "content-type": "application/json; charset=utf-8",
                },
                json=discover,
            )
            rejected = await session.post(
                "/mcp",
                headers={**modern_headers, "content-type": "application/json-evil"},
                content=b"{}",
            )
            duplicated = await session.post(
                "/mcp",
                headers=[
                    ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                    ("mcp-method", "server/discover"),
                    ("content-type", "application/json"),
                    ("content-type", "application/json-evil"),
                ],
                content=b"{}",
            )

    assert accepted.status_code == 200
    assert rejected.status_code == 400
    assert rejected.text == "Invalid Content-Type header"
    assert duplicated.status_code == 400
    assert duplicated.text == "Invalid Content-Type header"


@pytest.mark.asyncio
async def test_mcp_http_lifecycle_records_bounded_timing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_MCP_BODY_MARKER_DO_NOT_LOG"
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
    )
    discover = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": marker, "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    caplog.set_level(logging.INFO, logger="query_man.mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:3000",
        ) as session:
            response = await session.post(
                "/mcp",
                headers={
                    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                    "mcp-method": "server/discover",
                },
                json=discover,
            )
            rejected = await session.post(
                "/mcp",
                headers={
                    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                    "mcp-method": "server/discover",
                    "content-type": "text/plain",
                },
                content=marker,
            )
            await session.get("/ready")

    assert response.status_code == 200
    assert rejected.status_code == 400
    completed = [
        record
        for record in caplog.records
        if record.getMessage() == "mcp_http_request_completed"
    ]
    assert len(completed) == 2
    record = next(record for record in completed if record.status_code == 200)
    rejected_record = next(record for record in completed if record.status_code == 400)
    assert uuid.UUID(record.mcp_http_request_id).version == 4
    assert record.status_code == 200
    assert 0 <= record.response_started_ms <= record.duration_ms
    assert record.response_bytes == len(response.content)
    assert record.outcome == "success"
    assert rejected_record.outcome == "error"
    assert rejected_record.response_bytes == len(rejected.content)
    assert marker not in caplog.text
    metrics = {
        metric["name"]: metric["value"] for metric in operations.snapshot()["metrics"]
    }
    assert metrics["mcp_http_request_started"] == 2
    assert metrics["mcp_http_request_completed"] == 2
    assert metrics["mcp_http_request_failed"] == 1
    assert metrics["mcp_http_request_duration_ms_count"] == 2
    assert metrics["mcp_http_response_started_ms_count"] == 2
    assert metrics["mcp_http_response_bytes_sum"] == len(response.content) + len(rejected.content)


@pytest.mark.asyncio
async def test_mcp_http_lifecycle_includes_generated_internal_error_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=FailingAccessPolicy(),  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO, logger="query_man.mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:3000",
        ) as session:
            response = await session.post(
                "/mcp",
                headers={"mcp-protocol-version": MCP_PROTOCOL_VERSION},
                json={},
            )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred.",
        }
    }
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "mcp_http_request_completed"
    )
    assert record.status_code == 500
    assert 0 <= record.response_started_ms <= record.duration_ms
    assert record.response_bytes == len(response.content)
    assert record.outcome == "error"
    metrics = {
        metric["name"]: metric["value"] for metric in operations.snapshot()["metrics"]
    }
    assert metrics["mcp_http_request_failed"] == 1
    assert metrics["mcp_http_response_started_ms_count"] == 1
    assert metrics["mcp_http_response_bytes_sum"] == len(response.content)


@pytest.mark.asyncio
async def test_mcp_transport_requires_current_protocol_version() -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
    )
    discover = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "protocol-test",
                    "version": "1",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:3000",
        ) as session:
            accepted = await session.post(
                "/mcp",
                headers={
                    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                    "mcp-method": "server/discover",
                },
                json=discover,
            )
            responses = [
                await session.post("/mcp", json=discover),
                await session.post(
                    "/mcp",
                    headers={
                        "mcp-protocol-version": "2025-11-25",
                        "mcp-method": "server/discover",
                    },
                    json=discover,
                ),
                await session.post(
                    "/mcp",
                    headers={
                        "mcp-protocol-version": "2099-01-01",
                        "mcp-method": "server/discover",
                    },
                    json=discover,
                ),
                await session.post(
                    "/mcp",
                    headers=[
                        ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                        ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                        ("mcp-method", "server/discover"),
                        ("content-type", "application/json"),
                    ],
                    content=b"{}",
                ),
            ]

    assert accepted.status_code == 200
    assert accepted.json()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    assert all(response.status_code == 400 for response in responses)
    assert all(
        response.json() == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": [MCP_PROTOCOL_VERSION]},
            },
        }
        for response in responses
    )


@pytest.mark.asyncio
async def test_public_readiness_hides_inventory_and_operator_metrics_are_detailed(
    tmp_path: Path,
) -> None:
    access_policy = _shared_access_policy(tmp_path)
    query_headers = {"authorization": f"Bearer {_QUERY_A_TOKEN}"}
    admin_headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with client(
        ReturningCatalog(minimal_development_snapshot()),
        access_policy=access_policy,
    ) as session:
        initializing = await session.get("/ready")
        await session.post(
            "/meta",
            headers=query_headers,
            json={"source_id": "development-issues", "question": "최근 문제"},
        )
        degraded = await session.get("/ready")
        detailed = await session.get("/admin/health", headers=admin_headers)
        operations.increment("metadata_refresh_succeeded")
        metrics = await session.get("/admin/metrics", headers=admin_headers)

    assert initializing.status_code == 503
    assert initializing.json() == {"status": "initializing"}
    assert degraded.status_code == 200
    assert degraded.json() == {"status": "degraded"}
    assert "development-issues" not in degraded.text
    assert detailed.json()["status"] == "degraded"
    assert detailed.json()["sources"]["development-issues"] == "healthy"
    assert metrics.status_code == 200
    assert any(
        metric["name"] == "metadata_refresh_succeeded"
        for metric in metrics.json()["metrics"]
    )


@pytest.mark.asyncio
async def test_startup_probe_is_bounded_and_keeps_usable_gateway_degraded(
    tmp_path: Path,
) -> None:
    loaded = load_test_registry()
    registry = SourceRegistry(
        [
            replace(
                source,
                budget=replace(source.budget, metadata_statement_timeout_ms=20),
            )
            for source_id in ("development-issues", "market-voc")
            if (source := loaded.get(source_id)) is not None
        ]
    )
    app = build_app(
        runtime_config(),
        registry=registry,
        catalog=PartiallyHangingCatalog(minimal_development_snapshot()),
        access_policy=_shared_access_policy(tmp_path),
    )

    started = time.monotonic()
    async with app.router.lifespan_context(app):
        startup_elapsed = time.monotonic() - started
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as session:
            ready = await session.get("/ready")
            detailed = await session.get(
                "/admin/health",
                headers={"authorization": f"Bearer {_ADMIN_TOKEN}"},
            )

    assert startup_elapsed < 0.5
    assert ready.status_code == 200
    assert ready.json() == {"status": "degraded"}
    assert detailed.json()["sources"] == {
        "development-issues": "healthy",
        "market-voc": "unavailable",
    }


@pytest.mark.asyncio
async def test_startup_probe_returns_unavailable_when_every_source_fails() -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=FailingCatalog(),
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as session:
            ready = await session.get("/ready")

    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_bearer_token_is_required_when_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "test-token-with-at-least-thirty-two-characters"
    caplog.set_level(logging.WARNING, logger="query_man.audit")
    async with client(NeverCalledCatalog(), token) as session:
        unauthorized = await session.get("/sources")
        authorized = await session.get("/sources", headers={"authorization": f"Bearer {token}"})
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    assert authorized.status_code == 200
    assert "authentication_failed" in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_duplicate_authorization_headers_are_rejected() -> None:
    token = "test-token-with-at-least-thirty-two-characters"
    async with client(NeverCalledCatalog(), token) as session:
        response = await session.get(
            "/sources",
            headers=[
                ("authorization", f"Bearer {token}"),
                ("authorization", f"Bearer {token}"),
            ],
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_mcp_endpoint_uses_the_same_bearer_authentication() -> None:
    token = "test-token-with-at-least-thirty-two-characters"
    async with client(NeverCalledCatalog(), token) as session:
        response = await session.post("/mcp", content=b"{}")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rejects_client_connection_fields() -> None:
    async with client(NeverCalledCatalog()) as session:
        response = await session.post(
            "/meta",
            json={
                "source_id": "development-issues",
                "question": "최근 문제를 보여줘",
                "host": "attacker.invalid",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_rejects_client_supplied_tenant_context() -> None:
    async with client(NeverCalledCatalog()) as session:
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": "SELECT 1",
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": SQL_POLICY_REVISION,
                "tenant_id": "attacker-selected-tenant",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_unknown_source_has_non_disclosing_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_id = "not-registered"
    caplog.set_level(logging.WARNING, logger="query_man.audit")
    async with client(NeverCalledCatalog()) as session:
        response = await session.post(
            "/meta",
            json={"source_id": source_id, "question": "anything"},
        )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "SOURCE_NOT_FOUND",
            "message": "The requested source was not found.",
        }
    }
    assert "authorization_denied" in caplog.text
    assert source_id not in caplog.text


@pytest.mark.asyncio
async def test_raw_database_errors_are_not_disclosed() -> None:
    async with client(FailingCatalog()) as session:
        response = await session.post("/meta", json={"source_id": "development-issues", "question": "최근 문제"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "METADATA_UNAVAILABLE"
    assert "super-secret-password" not in response.text
    assert "10.20.30.40" not in response.text


@pytest.mark.asyncio
async def test_query_credentials_reject_every_admin_operation_and_cancel(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    credential = "must-not-appear-in-response"
    query_id = "30c7b03d-659d-47d4-b6f5-cb2ea9a9eaf0"
    executor = RecordingQueryExecutor()
    executor.cancel_result = True
    requests: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", "/admin/health", None),
        ("GET", "/admin/metrics", None),
        ("GET", "/admin/sources", None),
        ("GET", "/admin/sources?limit=0", None),
        ("GET", "/admin/sources/INVALID_SOURCE", None),
        ("GET", "/admin/sources/unknown-source", None),
        ("GET", "/admin/sources/unknown-source/history", None),
        ("GET", "/admin/sources/INVALID_SOURCE/replicas?limit=0", None),
        ("GET", "/admin/sources/INVALID_SOURCE/usage?unexpected=value", None),
        ("GET", "/admin/sources/unknown-source/mutations?before_event_id=0", None),
        ("GET", f"/admin/mutations/{_MUTATION_KEYS[0]}", None),
        ("GET", "/admin/mutations/not-a-canonical-uuid", None),
        (
            "PUT",
            "/admin/sources/new-source",
            {"manifest": {}, "credential": credential},
        ),
        (
            "POST",
            "/admin/sources/new-source/credential",
            {"credential": credential},
        ),
        (
            "POST",
            "/admin/sources/new-source/verified-queries",
            {
                "query_id": "admin-boundary-check",
                "question": "관리 경계를 검증해줘",
                "metadata_revision": f"sha256:{'0' * 64}",
                "relations": ["ai.issue_overview"],
                "sql": "SELECT issue_id FROM ai.issue_overview LIMIT 1",
                "expected": {
                    "columns": ["issue_id"],
                    "row_count": 0,
                    "result_hash": f"sha256:{'0' * 64}",
                },
            },
        ),
        ("POST", "/admin/sources/new-source/rollback/1", None),
        ("POST", "/admin/sources/new-source/metadata/resume", None),
        ("DELETE", "/admin/sources/new-source", None),
        ("DELETE", f"/queries/{query_id}", None),
        ("DELETE", "/queries/not-a-uuid", None),
    ]
    async with client(
        NeverCalledCatalog(),
        query_executor=executor,
        access_policy=policy,
    ) as session:
        for token in (_QUERY_A_TOKEN, _QUERY_B_TOKEN):
            headers = {"authorization": f"Bearer {token}"}
            for method, path, body in requests:
                options = {} if body is None else {"json": body}
                response = await session.request(
                    method,
                    path,
                    headers=headers,
                    **options,  # type: ignore[arg-type]
                )
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["error"]["code"] == "OPERATOR_REQUIRED"
                assert credential not in response.text

            mutation_headers = {
                **_operator_mutation_headers(_MUTATION_KEYS[0]),
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            }
            malformed_body = await session.put(
                "/admin/sources/new-source",
                headers=mutation_headers,
                content=b"{",
            )
            malformed_path = await session.post(
                "/admin/sources/new-source/rollback/not-a-generation",
                headers=mutation_headers,
            )
            duplicated_header = await session.post(
                "/admin/sources/new-source/credential",
                headers=[
                    ("authorization", f"Bearer {token}"),
                    ("idempotency-key", _MUTATION_KEYS[0]),
                    ("idempotency-key", _MUTATION_KEYS[1]),
                    ("x-query-man-reason", "ctrl-205"),
                    ("x-expected-generation", "7"),
                    ("x-expected-state-version", "9"),
                ],
                json={"credential": credential},
            )
            for response in (malformed_body, malformed_path, duplicated_header):
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "OPERATOR_REQUIRED"
                assert credential not in response.text

        admin_headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
        health = await session.get("/admin/health", headers=admin_headers)
        metrics = await session.get("/admin/metrics", headers=admin_headers)
        cancelled = await session.delete(
            f"/queries/{query_id}",
            headers=admin_headers,
        )
        malformed_cancel = await session.delete(
            "/queries/not-a-uuid",
            headers=admin_headers,
        )

    assert health.status_code == 200
    assert metrics.status_code == 200
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "cancel_requested", "query_id": query_id}
    assert malformed_cancel.status_code == 400
    assert malformed_cancel.json()["error"]["code"] == "INVALID_REQUEST"
    assert executor.cancel_calls == [query_id]


@pytest.mark.asyncio
async def test_admin_mutation_and_receipt_routes_require_authentication_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    requests = [
        ("PUT", "/admin/sources/INVALID_SOURCE"),
        ("POST", "/admin/sources/INVALID_SOURCE/credential"),
        ("POST", "/admin/sources/INVALID_SOURCE/verified-queries"),
        ("POST", "/admin/sources/INVALID_SOURCE/rollback/not-a-generation"),
        ("POST", "/admin/sources/INVALID_SOURCE/metadata/resume"),
        ("DELETE", "/admin/sources/INVALID_SOURCE"),
        ("GET", "/admin/mutations/not-a-canonical-uuid"),
        ("GET", "/admin/sources/INVALID_SOURCE/mutations?limit=0"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        responses = [
            await session.request(method, path, content=b"{")
            for method, path in requests
        ]

    assert all(response.status_code == 401 for response in responses)
    assert all(response.json()["error"]["code"] == "UNAUTHORIZED" for response in responses)
    assert admin.receipt_calls == []
    assert admin.source_mutation_calls == []
    assert admin.mutation_calls == []


@pytest.mark.asyncio
async def test_replica_admin_authenticates_and_authorizes_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    invalid_path = "/admin/sources/INVALID_SOURCE/replicas?limit=0&unexpected=value"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        unauthenticated = await session.get(invalid_path)
        query_caller = await session.get(
            invalid_path,
            headers={"authorization": f"Bearer {_QUERY_A_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert query_caller.status_code == 403
    assert query_caller.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert admin.replica_calls == []


@pytest.mark.asyncio
async def test_operator_replica_admin_forwards_and_preserves_projection(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        default_page = await session.get(
            "/admin/sources/known-source/replicas",
            headers=headers,
        )
        explicit_page = await session.get(
            "/admin/sources/known-source/replicas",
            headers=headers,
            params={"limit": "7", "after_replica_id": "replica-alpha"},
        )
        empty_page = await session.get(
            "/admin/sources/empty-source/replicas",
            headers=headers,
        )
        status_page = await session.get(
            "/admin/sources/replica-status-source/replicas",
            headers=headers,
        )

    assert all(
        response.status_code == 200
        for response in (default_page, explicit_page, empty_page, status_page)
    )
    assert admin.replica_calls == [
        ("known-source", 50, None),
        ("known-source", 7, "replica-alpha"),
        ("empty-source", 50, None),
        ("replica-status-source", 50, None),
    ]
    expected_available = {
        "source_id": "known-source",
        "desired": {
            "enabled": True,
            "generation": 7,
            "state_version": 9,
            "metadata_revision": f"sha256:{'1' * 64}",
        },
        "replicas": [
            {
                "replica_id": "replica-alpha",
                "status": "available",
                "source_health": "healthy",
                "applied": {
                    "enabled": True,
                    "generation": 7,
                    "state_version": 9,
                    "metadata_revision": f"sha256:{'1' * 64}",
                },
                "drift": [],
                "observed_at": "2026-08-25T12:00:00+00:00",
                "fresh_until": "2026-08-25T12:00:15+00:00",
                "stale_age_ms": 0,
                "reason_code": None,
            }
        ],
        "next_after_replica_id": None,
    }
    assert default_page.json() == explicit_page.json() == expected_available
    assert empty_page.json() == {
        "source_id": "empty-source",
        "desired": expected_available["desired"],
        "replicas": [],
        "next_after_replica_id": None,
    }
    assert status_page.json() == {
        "source_id": "replica-status-source",
        "desired": expected_available["desired"],
        "replicas": [
            {
                "replica_id": "replica-stale",
                "status": "stale",
                "source_health": "healthy",
                "applied": {
                    "enabled": True,
                    "generation": 6,
                    "state_version": 8,
                    "metadata_revision": f"sha256:{'2' * 64}",
                },
                "drift": ["generation", "state_version", "metadata_revision"],
                "observed_at": "2026-08-25T11:59:00+00:00",
                "fresh_until": "2026-08-25T11:59:15+00:00",
                "stale_age_ms": 45_000,
                "reason_code": "HEARTBEAT_EXPIRED",
            },
            {
                "replica_id": "replica-unavailable",
                "status": "unavailable",
                "source_health": None,
                "applied": None,
                "drift": ["not_applied"],
                "observed_at": "2026-08-25T12:00:00+00:00",
                "fresh_until": "2026-08-25T12:00:15+00:00",
                "stale_age_ms": 0,
                "reason_code": "CONTROL_SCAN_FAILED",
            },
        ],
        "next_after_replica_id": None,
    }


@pytest.mark.asyncio
async def test_operator_replica_admin_returns_bounded_errors(tmp_path: Path) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources/known-source/replicas?limit=0",
        "/admin/sources/known-source/replicas?limit=101",
        "/admin/sources/known-source/replicas?after_replica_id=",
        "/admin/sources/known-source/replicas?after_replica_id=Invalid_Replica",
        f"/admin/sources/known-source/replicas?after_replica_id={'a' * 81}",
        "/admin/sources/known-source/replicas?after_replica_id=%20replica-alpha%20",
        "/admin/sources/known-source/replicas?unexpected=value",
        "/admin/sources/known-source/replicas?limit=10&limit=20",
        (
            "/admin/sources/known-source/replicas?"
            "after_replica_id=replica-a&after_replica_id=replica-b"
        ),
        "/admin/sources/INVALID_SOURCE/replicas",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown = await session.get(
            "/admin/sources/unknown-source/replicas",
            headers=headers,
        )
        unavailable = await session.get(
            "/admin/sources/unavailable-source/replicas",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert all(len(response.content) < 4_096 for response in invalid)
    assert admin.replica_calls == [
        ("unknown-source", 50, None),
        ("unavailable-source", 50, None),
    ]
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
    assert "private replica observation detail" not in unavailable.text


@pytest.mark.asyncio
async def test_usage_admin_authenticates_and_authorizes_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    invalid_path = "/admin/sources/INVALID_SOURCE/usage?unexpected=value"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        unauthenticated = await session.get(invalid_path)
        query_caller = await session.get(
            invalid_path,
            headers={"authorization": f"Bearer {_QUERY_A_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert query_caller.status_code == 403
    assert query_caller.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert admin.usage_calls == []


@pytest.mark.asyncio
async def test_operator_usage_admin_preserves_control_projection(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        response = await session.get(
            "/admin/sources/known-source/usage",
            headers=headers,
        )

    assert response.status_code == 200
    assert admin.usage_calls == ["known-source"]
    assert response.json() == {
        "source_id": "known-source",
        "enabled": True,
        "read_at": "2026-08-25T12:00:00+00:00",
        "resource": {
            "status": "pending",
            "reason_code": "NOT_OBSERVED",
            "last_attempt": None,
            "fresh_until": None,
            "metrics": [],
        },
        "gateway": {
            "status": "pending",
            "reason_code": "NOT_REPORTED",
            "last_report_at": None,
            "fresh_until": None,
            "lower_bound": True,
            "window_start": "2026-07-25T12:00:00+00:00",
            "window_end": "2026-08-25T12:00:00+00:00",
            "rollups": [],
        },
        "monetary_cost": {
            "status": "not_configured",
            "reason_code": "PROVIDER_NOT_CONFIGURED",
            "last_attempt": None,
        },
    }


@pytest.mark.asyncio
async def test_operator_usage_admin_returns_bounded_errors(tmp_path: Path) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources/known-source/usage?unexpected=value",
        "/admin/sources/known-source/usage?limit=50",
        "/admin/sources/known-source/usage?unexpected=a&unexpected=b",
        "/admin/sources/INVALID_SOURCE/usage",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown = await session.get(
            "/admin/sources/unknown-source/usage",
            headers=headers,
        )
        unavailable = await session.get(
            "/admin/sources/unavailable-source/usage",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert all(len(response.content) < 4_096 for response in invalid)
    assert admin.usage_calls == ["unknown-source", "unavailable-source"]
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
    assert "private usage projection detail" not in unavailable.text


@pytest.mark.asyncio
async def test_operator_mutation_routes_forward_context_and_payload(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    manifest = {"version": 1, "source_id": "new-source"}
    credential = "postgresql://source-admin-secret"
    metadata_revision = f"sha256:{'1' * 64}"
    result_hash = f"sha256:{'2' * 64}"
    verified_payload = {
        "query_id": "known-source-check",
        "question": "Check the known source",
        "metadata_revision": metadata_revision,
        "relations": ["ai.issue_overview"],
        "sql": "SELECT issue_id FROM ai.issue_overview LIMIT 1",
        "expected": {
            "columns": ["issue_id"],
            "row_count": 1,
            "result_hash": result_hash,
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        published = await session.put(
            "/admin/sources/new-source",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[0],
                reason="ctrl-205.publish",
                expected_generation=0,
                expected_state_version=0,
            ),
            json={"manifest": manifest, "credential": credential},
        )
        rotated = await session.post(
            "/admin/sources/known-source/credential",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[1],
                reason="ctrl-205.rotate",
            ),
            json={"credential": credential},
        )
        verified = await session.post(
            "/admin/sources/known-source/verified-queries",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[2],
                reason="ctrl-205.verify",
            ),
            json=verified_payload,
        )
        rolled_back = await session.post(
            "/admin/sources/known-source/rollback/3",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[3],
                reason="ctrl-205.rollback",
            ),
        )
        resumed = await session.post(
            "/admin/sources/known-source/metadata/resume",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[4],
                reason="ctrl-205.resume",
                expected_metadata_revision=metadata_revision,
            ),
        )
        deactivated = await session.delete(
            "/admin/sources/known-source",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[5],
                reason="ctrl-205.deactivate",
            ),
        )

    assert all(
        response.status_code == 200
        for response in (
            published,
            rotated,
            verified,
            rolled_back,
            resumed,
            deactivated,
        )
    )
    assert admin.mutation_calls == [
        (
            "publish",
            "new-source",
            manifest,
            credential,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[0],
                actor="admin",
                reason="ctrl-205.publish",
                expected_generation=0,
                expected_state_version=0,
            ),
        ),
        (
            "rotate_credential",
            "known-source",
            credential,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[1],
                actor="admin",
                reason="ctrl-205.rotate",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "publish_verified_query",
            PublishVerifiedQueryInput(
                query_id="known-source-check",
                source_id="known-source",
                question="Check the known source",
                sql="SELECT issue_id FROM ai.issue_overview LIMIT 1",
                metadata_revision=metadata_revision,
                relations=("ai.issue_overview",),
                expected=VerifiedExpectedInput(
                    columns=("issue_id",),
                    row_count=1,
                    result_hash=result_hash,
                ),
            ),
            "operations",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[2],
                actor="admin",
                reason="ctrl-205.verify",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "rollback",
            "known-source",
            3,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[3],
                actor="admin",
                reason="ctrl-205.rollback",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "resume",
            "known-source",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[4],
                actor="admin",
                reason="ctrl-205.resume",
                expected_generation=7,
                expected_state_version=9,
                expected_metadata_revision=metadata_revision,
            ),
        ),
        (
            "deactivate",
            "known-source",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[5],
                actor="admin",
                reason="ctrl-205.deactivate",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_operator_mutation_routes_reject_invalid_contract_before_service(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    credential = "must-not-be-disclosed"
    metadata_revision = f"sha256:{'1' * 64}"
    existing_headers = _operator_mutation_headers(_MUTATION_KEYS[0])
    absent_headers = _operator_mutation_headers(
        _MUTATION_KEYS[0],
        expected_generation=0,
        expected_state_version=0,
    )
    missing_header = dict(existing_headers)
    del missing_header["x-query-man-reason"]
    malformed_key = {
        **existing_headers,
        "idempotency-key": "NOT-A-CANONICAL-UUID",
    }
    unsafe_reason = {
        **existing_headers,
        "x-query-man-reason": "ticket contains private detail",
    }
    mismatched_state = {
        **existing_headers,
        "x-expected-state-version": "0",
    }
    overflow_state = {
        **existing_headers,
        "x-expected-generation": str(CONTROL_SEQUENCE_MAX + 1),
    }
    unexpected_revision = {
        **existing_headers,
        "x-expected-metadata-revision": metadata_revision,
    }
    duplicate_key_headers = [
        *existing_headers.items(),
        ("idempotency-key", _MUTATION_KEYS[1]),
    ]
    duplicate_content_type_headers = [
        *existing_headers.items(),
        ("content-type", "application/json"),
        ("content-type", "application/json"),
    ]
    ambiguous_content_types = (
        "application/json; charset=utf-8, text/plain",
        "application/json; charset",
        'application/json; charset="utf-8',
        "application/json; charset=utf-8; charset=utf-8",
    )
    duplicate_json_body = (
        b'{"credential":"discarded-secret","credential":"selected-secret"}'
    )
    oversized_extra_key_body = (
        b'{"credential":"safe","embedded-secret-'
        + b"x" * 65_536
        + b'":true}'
    )
    excessive_member_body = (
        b'{"credential":"safe",'
        + b",".join(f'"field-{index}":0'.encode() for index in range(2_000))
        + b"}"
    )
    non_finite_number_body = (
        b'{"manifest":{"source_id":1e999},"credential":"must-not-be-disclosed"}'
    )
    deeply_nested_body = b"[" * 10_000 + b"]" * 10_000
    invalid_requests: list[tuple[str, str, dict[str, object]]] = [
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": missing_header, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": malformed_key, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": unsafe_reason, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": mismatched_state, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": overflow_state, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": _operator_mutation_headers(
                    _MUTATION_KEYS[0],
                    expected_generation=0,
                    expected_state_version=0,
                ),
                "json": {"credential": credential},
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": unexpected_revision, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": duplicate_key_headers, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "text/plain"},
                "content": credential.encode(),
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": duplicate_content_type_headers,
                "content": b'{"credential":"safe"}',
            },
        ),
        *(
            (
                "POST",
                "/admin/sources/known-source/credential",
                {
                    "headers": {
                        **existing_headers,
                        "content-type": content_type,
                    },
                    "content": b'{"credential":"safe"}',
                },
            )
            for content_type in ambiguous_content_types
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": duplicate_json_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": oversized_extra_key_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": excessive_member_body,
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": non_finite_number_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": deeply_nested_body,
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": b"{",
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": b"x" * 1_048_577,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/verified-queries",
            {"headers": existing_headers, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/rollback/3",
            {"headers": existing_headers, "content": b"unexpected"},
        ),
        (
            "POST",
            "/admin/sources/known-source/metadata/resume",
            {"headers": existing_headers},
        ),
        (
            "DELETE",
            "/admin/sources/known-source?unexpected=value",
            {"headers": existing_headers},
        ),
        (
            "PUT",
            "/admin/sources/INVALID_SOURCE",
            {
                "headers": absent_headers,
                "json": {"manifest": {}, "credential": credential},
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/rollback/0",
            {"headers": existing_headers},
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        responses = [
            await session.request(method, path, **options)  # type: ignore[arg-type]
            for method, path, options in invalid_requests
        ]

    assert all(response.status_code == 400 for response in responses)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in responses
    )
    assert all(len(response.content) < 4_096 for response in responses)
    assert all(credential not in response.text for response in responses)
    assert "private detail" not in "".join(response.text for response in responses)
    assert "embedded-secret" not in "".join(response.text for response in responses)
    assert "discarded-secret" not in "".join(response.text for response in responses)
    assert "selected-secret" not in "".join(response.text for response in responses)
    assert admin.mutation_calls == []


@pytest.mark.asyncio
async def test_operator_admin_source_gets_validate_and_forward_queries(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        listed = await session.get(
            "/admin/sources",
            headers=headers,
            params={
                "limit": "25",
                "after_source_id": "first-source",
                "enabled": "false",
                "owner": "data-platform",
                "environment": "production",
                "budget_profile": "interactive",
            },
        )
        detail = await session.get("/admin/sources/known-source", headers=headers)
        history = await session.get(
            "/admin/sources/known-source/history",
            headers=headers,
            params={"limit": "10", "before_generation": "7"},
        )
        receipt = await session.get(
            f"/admin/mutations/{_MUTATION_KEYS[0]}",
            headers=headers,
        )
        mutations = await session.get(
            "/admin/sources/known-source/mutations",
            headers=headers,
            params={"limit": "12", "before_event_id": "42"},
        )

    assert all(
        response.status_code == 200
        for response in (listed, detail, history, receipt, mutations)
    )
    assert admin.list_calls == [
        (
            25,
            "first-source",
            False,
            "data-platform",
            "production",
            "interactive",
        )
    ]
    assert admin.detail_calls == ["known-source"]
    assert admin.history_calls == [("known-source", 10, 7)]
    assert admin.receipt_calls == [_MUTATION_KEYS[0]]
    assert admin.source_mutation_calls == [("known-source", 12, 42)]
    assert history.json()["source_id"] == "known-source"
    assert receipt.json()["idempotency_key"] == _MUTATION_KEYS[0]
    assert mutations.json()["next_before_event_id"] == 41


@pytest.mark.asyncio
async def test_operator_admin_source_gets_return_bounded_errors(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources?limit=0",
        "/admin/sources?limit=101",
        "/admin/sources?after_source_id=Invalid_Source",
        "/admin/sources?owner=Invalid_Owner",
        "/admin/sources?environment=preview",
        "/admin/sources?budget_profile=invalid-profile",
        f"/admin/sources?budget_profile={'a' * 64}",
        "/admin/sources?unexpected=value",
        "/admin/sources?limit=10&limit=20",
        "/admin/sources/INVALID_SOURCE",
        "/admin/sources/known-source?unexpected=value",
        "/admin/sources/known-source/history?before_generation=0",
        (
            "/admin/sources/known-source/history?before_generation="
            f"{CONTROL_SEQUENCE_MAX + 1}"
        ),
        "/admin/mutations/not-a-canonical-uuid",
        f"/admin/mutations/{_MUTATION_KEYS[0]}?unexpected=value",
        "/admin/sources/INVALID_SOURCE/mutations",
        "/admin/sources/known-source/mutations?limit=0",
        "/admin/sources/known-source/mutations?limit=101",
        "/admin/sources/known-source/mutations?before_event_id=0",
        (
            "/admin/sources/known-source/mutations?before_event_id="
            f"{CONTROL_SEQUENCE_MAX + 1}"
        ),
        "/admin/sources/known-source/mutations?unexpected=value",
        "/admin/sources/known-source/mutations?limit=10&limit=20",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown_detail = await session.get(
            "/admin/sources/unknown-source",
            headers=headers,
        )
        unknown_history = await session.get(
            "/admin/sources/unknown-source/history",
            headers=headers,
        )
        unavailable_detail = await session.get(
            "/admin/sources/unavailable-source",
            headers=headers,
        )
        unknown_receipt = await session.get(
            f"/admin/mutations/{_UNKNOWN_MUTATION_KEY}",
            headers=headers,
        )
        unavailable_receipt = await session.get(
            f"/admin/mutations/{_UNAVAILABLE_MUTATION_KEY}",
            headers=headers,
        )
        unknown_mutations = await session.get(
            "/admin/sources/unknown-source/mutations",
            headers=headers,
        )
        unavailable_mutations = await session.get(
            "/admin/sources/unavailable-source/mutations",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert admin.list_calls == []
    assert admin.history_calls == [("unknown-source", 50, None)]
    assert admin.receipt_calls == [
        _UNKNOWN_MUTATION_KEY,
        _UNAVAILABLE_MUTATION_KEY,
    ]
    assert admin.source_mutation_calls == [
        ("unknown-source", 50, None),
        ("unavailable-source", 50, None),
    ]
    assert unknown_detail.status_code == unknown_history.status_code == 404
    assert unknown_detail.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unknown_history.json() == unknown_detail.json()
    assert unknown_receipt.status_code == 404
    assert unknown_receipt.json()["error"]["code"] == "MUTATION_NOT_FOUND"
    assert unknown_mutations.status_code == 404
    assert unknown_mutations.json() == unknown_detail.json()
    assert all(
        response.status_code == 503
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    assert all(
        response.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    unavailable_text = "".join(
        response.text
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    assert "private control database detail" not in unavailable_text
    assert "private mutation receipt detail" not in unavailable_text
    assert "private source mutation detail" not in unavailable_text


@pytest.mark.asyncio
async def test_schema_drift_details_are_not_disclosed() -> None:
    snapshot = minimal_development_snapshot()
    snapshot = replace(snapshot, relations=snapshot.relations[:1])
    async with client(ReturningCatalog(snapshot)) as session:
        response = await session.post("/meta", json={"source_id": "development-issues", "question": "최근 문제"})
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "METADATA_UNAVAILABLE",
        "message": "Metadata is temporarily unavailable for the requested source.",
    }
    assert "issue_overview" not in response.text


@pytest.mark.asyncio
async def test_executes_query_with_current_metadata_revision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = ReturningCatalog(minimal_development_snapshot())
    executor = RecordingQueryExecutor()
    caplog.set_level(logging.INFO, logger="query_man.audit")
    async with client(catalog, query_executor=executor) as session:
        context = await session.post(
            "/meta",
            json={"source_id": "development-issues", "question": "문제 수"},
        )
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
                "metadata_revision": context.json()["metadata_revision"],
                "sql_policy_revision": context.json()["sql_policy_revision"],
            },
        )

    assert response.status_code == 200
    assert response.json()["rows"] == [{"issue_count": 600}]
    assert executor.calls[0][0] == "development-issues"
    assert executor.calls[0][2] == "local-development"
    assert "query_started query_id=" in caplog.text
    assert "query_succeeded query_id=" in caplog.text
    assert "fingerprint=pg_query:" in caplog.text
    assert "elapsed_ms=1 row_count=1 result_bytes=21" in caplog.text
    assert "plan_total_cost=10.0" in caplog.text
    assert "plan_max_rows=1 plan_node_count=2" in caplog.text


@pytest.mark.asyncio
async def test_unsupported_result_is_details_free_http_503() -> None:
    catalog = ReturningCatalog(minimal_development_snapshot())
    async with client(catalog, query_executor=UnavailableQueryExecutor()) as session:
        context = await session.post(
            "/meta",
            json={"source_id": "development-issues", "question": "문제 수"},
        )
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
                "metadata_revision": context.json()["metadata_revision"],
                "sql_policy_revision": context.json()["sql_policy_revision"],
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "QUERY_UNAVAILABLE",
            "message": "The query could not be completed.",
        }
    }
    assert "3802" not in response.text
    assert "Unsupported PostgreSQL" not in response.text


@pytest.mark.asyncio
async def test_runtime_shutdown_drains_injected_executor_with_configured_grace() -> None:
    executor = RecordingQueryExecutor()
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=ReturningCatalog(minimal_development_snapshot()),
        query_executor=executor,
    )

    async with app.router.lifespan_context(app):
        pass

    assert executor.lifecycle == ["stop_accepting", "drain:10000", "close"]


@pytest.mark.asyncio
async def test_query_rejects_stale_revision_without_echoing_sql(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_literal = "private-customer-secret"
    caplog.set_level(logging.INFO, logger="query_man.audit")
    async with client(ReturningCatalog(minimal_development_snapshot())) as session:
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": f"SELECT '{secret_literal}' FROM ai.issue_overview",
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": SQL_POLICY_REVISION,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "METADATA_REVISION_MISMATCH"
    assert secret_literal not in response.text
    assert "query_failed query_id=" in caplog.text
    assert "error_code=METADATA_REVISION_MISMATCH" in caplog.text
    assert secret_literal not in caplog.text


@pytest.mark.asyncio
async def test_query_rejection_returns_only_bounded_construct_detail() -> None:
    sensitive_literal = "SENSITIVE_HTTP_SQL_LITERAL_DO_NOT_ECHO"
    async with client(ReturningCatalog(minimal_development_snapshot())) as session:
        context = await session.post(
            "/meta",
            json={"source_id": "development-issues", "question": "문제 번호 범위"},
        )
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": (
                    "SELECT issue_id FROM ai.issue_overview "
                    "WHERE issue_id NOT BETWEEN 1 AND 2 "
                    f"AND status <> '{sensitive_literal}'"
                ),
                "metadata_revision": context.json()["metadata_revision"],
                "sql_policy_revision": context.json()["sql_policy_revision"],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["details"] == {
        "reason_code": "SQL_OPERATOR_NOT_ALLOWED",
        "rejected_construct": "NOT BETWEEN",
    }
    assert sensitive_literal not in response.text


@pytest.mark.asyncio
async def test_query_invalid_returns_only_bounded_correction_reason() -> None:
    sensitive_literal = "SENSITIVE_HTTP_SQL_LITERAL_DO_NOT_ECHO"
    private_database_detail = "private_column at character 42"

    class InvalidQueryExecutor(RecordingQueryExecutor):
        async def execute(
            self,
            source: SourceProfile,
            _sql: str,
            _metadata_revision: str,
            validated: ValidatedSql,
            *,
            query_id: str | None = None,
            tenant_id: str | None = None,
        ) -> dict[str, object]:
            self.calls.append((source.source_id, validated.fingerprint, tenant_id))
            raise QueryInvalidError("QUERY_UNDEFINED_COLUMN") from RuntimeError(
                private_database_detail
            )

    async with client(
        ReturningCatalog(minimal_development_snapshot()),
        query_executor=InvalidQueryExecutor(),
    ) as session:
        context = await session.post(
            "/meta",
            json={"source_id": "development-issues", "question": "문제 컬럼"},
        )
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": (
                    "SELECT missing_column FROM ai.issue_overview "
                    f"WHERE status <> '{sensitive_literal}'"
                ),
                "metadata_revision": context.json()["metadata_revision"],
                "sql_policy_revision": context.json()["sql_policy_revision"],
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "QUERY_INVALID",
            "message": (
                "The query references a column PostgreSQL cannot resolve. Use a returned column "
                "sql_name or an alias declared in the query, then retry once."
            ),
            "details": {
                "reason_code": "QUERY_UNDEFINED_COLUMN",
                "action": "CORRECT_SQL",
                "retryable": True,
            },
        }
    }
    assert sensitive_literal not in response.text
    assert private_database_detail not in response.text


@pytest.mark.asyncio
async def test_query_identities_share_sources_and_source_resolved_budget(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    query_a_headers = {"authorization": f"Bearer {_QUERY_A_TOKEN}"}
    query_b_headers = {"authorization": f"Bearer {_QUERY_B_TOKEN}"}
    executor = RecordingQueryExecutor()
    async with client(
        ReturningCatalog(minimal_development_snapshot()),
        query_executor=executor,
        access_policy=policy,
    ) as session:
        listed_a = await session.get("/sources", headers=query_a_headers)
        listed_b = await session.get("/sources", headers=query_b_headers)
        context_a = await session.post(
            "/meta",
            headers=query_a_headers,
            json={"source_id": "development-issues", "question": "문제 수"},
        )
        context_b = await session.post(
            "/meta",
            headers=query_b_headers,
            json={"source_id": "development-issues", "question": "문제 수"},
        )
        query_payload = {
            "source_id": "development-issues",
            "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
            "metadata_revision": context_a.json()["metadata_revision"],
            "sql_policy_revision": context_a.json()["sql_policy_revision"],
        }
        queried_a = await session.post(
            "/query",
            headers=query_a_headers,
            json=query_payload,
        )
        queried_b = await session.post(
            "/query",
            headers=query_b_headers,
            json=query_payload,
        )
        override = await session.post(
            "/query",
            headers=query_a_headers,
            json={**query_payload, "budget_profile": "caller-selected-tier"},
        )
        unauthenticated = await session.get("/sources")
        invalid = await session.get(
            "/sources",
            headers={"authorization": "Bearer invalid-query-token"},
        )

    expected_sources = ["development-issues", "market-voc"]
    assert [source["source_id"] for source in listed_a.json()["sources"]] == expected_sources
    assert listed_b.json() == listed_a.json()
    assert context_a.status_code == context_b.status_code == 200
    assert context_b.json() == context_a.json()
    assert queried_a.status_code == queried_b.status_code == 200
    assert queried_b.json()["rows"] == queried_a.json()["rows"]
    assert [call[2] for call in executor.calls] == ["engineering", "quality"]
    assert len(executor.budgets) == 2
    assert executor.budgets[0] == executor.budgets[1]
    assert override.status_code == 400
    assert override.json()["error"]["code"] == "INVALID_REQUEST"
    assert len(executor.calls) == 2
    assert unauthenticated.status_code == invalid.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert invalid.json() == unauthenticated.json()
