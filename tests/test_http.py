from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from query_man.delivery.access import AccessPolicy
from query_man.delivery.mcp_server import MCP_PROTOCOL_VERSION
from query_man.errors import QueryInvalidError, QueryUnavailableError
from query_man.guarded_query.query import DeliveryQueryExecutor
from query_man.guarded_query.sql_validation import SQL_POLICY_REVISION, ValidatedSql
from query_man.metadata.models import (
    CatalogProvider,
    CatalogSnapshot,
    ResourceObservation,
)
from query_man.runtime.composition import build_app
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.diagnostic_capture import decrypt_diagnostic_records
from query_man.runtime.operations import operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import SourceRegistry
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
_DIAGNOSTIC_KEY = base64.urlsafe_b64encode(b"http-diagnostic-capture-key-32b!").decode("ascii")


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
    catalog: CatalogProvider,
    api_token: str | None = None,
    query_executor: DeliveryQueryExecutor | None = None,
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


@pytest.mark.asyncio
async def test_static_composition_does_not_register_source_admin_routes(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    requests = (
        ("GET", "/admin/sources"),
        ("GET", "/admin/sources/source-a/history"),
        ("GET", "/admin/sources/source-a/replicas"),
        ("GET", "/admin/sources/source-a/usage"),
        ("GET", "/admin/sources/source-a/mutations"),
        ("GET", "/admin/mutations/4a3fcf7e-818f-4b68-9d31-21d392a28701"),
        ("GET", "/admin/sources/source-a"),
        ("PUT", "/admin/sources/source-a"),
        ("POST", "/admin/sources/source-a/credential"),
        ("POST", "/admin/sources/source-a/verified-queries"),
        ("POST", "/admin/sources/source-a/rollback/1"),
        ("POST", "/admin/sources/source-a/metadata/resume"),
        ("DELETE", "/admin/sources/source-a"),
    )
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        responses = [
            await session.request(method, path, headers=headers, json={})
            for method, path in requests
        ]

    assert len(responses) == 13
    assert all(response.status_code == 404 for response in responses)


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
    records = {record.getMessage(): record for record in caplog.records}
    assert uuid.UUID(records["query_started"].query_id).version == 4
    succeeded = records["query_succeeded"]
    assert succeeded.query_id == records["query_started"].query_id
    assert succeeded.fingerprint.startswith("pg_query:")
    assert (succeeded.elapsed_ms, succeeded.row_count, succeeded.result_bytes) == (1, 1, 21)
    assert (
        succeeded.plan_total_cost,
        succeeded.plan_max_rows,
        succeeded.plan_node_count,
    ) == (10.0, 1, 2)


@pytest.mark.asyncio
async def test_consented_http_requests_use_encrypted_capture_and_pseudonymous_audit(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    token = "consented-user-token-value-with-at-least-32-characters"
    policy_path = tmp_path / "access.yaml"
    expires_at = datetime.now(UTC) + timedelta(days=30)
    policy_path.write_text(
        f"""
version: 2
callers:
  - caller_id: consented-user
    tenant_id: engineering
    token_env: CONSENTED_TOKEN
    diagnostic_consent:
      version: 1
      receipt_id: consent-http-001
      expires_at: {expires_at.isoformat()}
""".strip(),
        encoding="utf-8",
    )
    access_policy = AccessPolicy.load(policy_path, {"CONSENTED_TOKEN": token})
    database = tmp_path / "capture.sqlite3"
    config = replace(
        runtime_config(),
        diagnostic_capture_database=database,
        diagnostic_capture_key=_DIAGNOSTIC_KEY,
        diagnostic_capture_key_id="http-key-2026-08",
    )
    app = build_app(
        config,
        registry=load_test_registry(),
        catalog=ReturningCatalog(minimal_development_snapshot()),
        query_executor=RecordingQueryExecutor(),
        access_policy=access_policy,
    )
    headers = {"authorization": f"Bearer {token}"}
    caplog.set_level(logging.INFO, logger="query_man.audit")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as session:
            context = await session.post(
                "/meta",
                headers=headers,
                json={"source_id": "development-issues", "question": "고객 private-name 문제"},
            )
            response = await session.post(
                "/query",
                headers=headers,
                json={
                    "source_id": "development-issues",
                    "sql": "SELECT 'private-literal' FROM ai.issue_overview",
                    "metadata_revision": context.json()["metadata_revision"],
                    "sql_policy_revision": context.json()["sql_policy_revision"],
                },
            )

    assert response.status_code == 200
    records = decrypt_diagnostic_records(database, _DIAGNOSTIC_KEY, "http-key-2026-08")
    assert len(records) == 2
    assert all(record["subject_id"] for record in records)
    audit = {record.getMessage(): record for record in caplog.records}
    assert audit["query_started"].subject_id == records[0]["subject_id"]
    assert not hasattr(audit["query_started"], "caller_id")
    assert not hasattr(audit["query_started"], "tenant_id")
    assert "consented-user" not in caplog.text
    assert "engineering" not in caplog.text
    assert "private-name" not in caplog.text
    assert "private-literal" not in caplog.text


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
    records = {record.getMessage(): record for record in caplog.records}
    assert records["query_failed"].query_id == records["query_started"].query_id
    assert records["query_failed"].error_code == "METADATA_REVISION_MISMATCH"
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
