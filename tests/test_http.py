from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from query_man.access import AccessPolicy
from query_man.app import build_app
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.query import QueryExecutor
from query_man.runtime_config import RuntimeConfig
from query_man.sql_validation import ValidatedSql
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


class NeverCalledCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        raise RuntimeError("Catalog should not be called")

    async def close(self) -> None:
        pass


class FailingCatalog(NeverCalledCatalog):
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        raise RuntimeError("connect to 10.20.30.40 with super-secret-password failed")


class ReturningCatalog(NeverCalledCatalog):
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot


class RecordingQueryExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

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
        pass

    async def cancel(self, _query_id: str, _allowed_sources: frozenset[str]) -> bool:
        return False


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
    catalog: object,
    api_token: str | None = None,
    query_executor: QueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> httpx.AsyncClient:
    app = build_app(
        runtime_config(api_token),
        registry=load_test_registry(),
        catalog=catalog,  # type: ignore[arg-type]
        query_executor=query_executor,
        access_policy=access_policy,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_lists_sources_without_connection_information() -> None:
    async with client(NeverCalledCatalog()) as session:
        response = await session.get("/sources")
    assert response.status_code == 200
    assert len(response.json()["sources"]) == 2
    assert "password" not in response.text
    assert "development_issues_reader" not in response.text


@pytest.mark.asyncio
async def test_public_readiness_hides_inventory_and_operator_metrics_are_detailed() -> None:
    async with client(ReturningCatalog(minimal_development_snapshot())) as session:
        ready = await session.get("/ready")
        await session.post(
            "/meta",
            json={"source_id": "development-issues", "question": "최근 문제"},
        )
        detailed = await session.get("/admin/health")
        metrics = await session.get("/admin/metrics")

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert "development-issues" not in ready.text
    assert detailed.json()["sources"]["development-issues"] == "healthy"
    assert any(
        metric["name"] == "metadata_refresh_succeeded"
        for metric in metrics.json()["metrics"]
    )


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
                "tenant_id": "attacker-selected-tenant",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_unknown_source_has_non_disclosing_error() -> None:
    async with client(NeverCalledCatalog()) as session:
        response = await session.post("/meta", json={"source_id": "not-registered", "question": "anything"})
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "SOURCE_NOT_FOUND",
            "message": "The requested source was not found.",
        }
    }


@pytest.mark.asyncio
async def test_raw_database_errors_are_not_disclosed() -> None:
    async with client(FailingCatalog()) as session:
        response = await session.post("/meta", json={"source_id": "development-issues", "question": "최근 문제"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "METADATA_UNAVAILABLE"
    assert "super-secret-password" not in response.text
    assert "10.20.30.40" not in response.text


@pytest.mark.asyncio
async def test_source_admin_requires_operator_and_hides_credential() -> None:
    policy_path = ROOT_DIRECTORY / "config" / "access-policies.example.yaml"
    token = "development-analyst-token-at-least-32-characters"
    policy = AccessPolicy.load(
        policy_path,
        ["development-issues", "market-voc"],
        {
            "DEVELOPMENT_ANALYST_API_TOKEN": token,
            "QUALITY_OPERATOR_API_TOKEN": "operator-token-at-least-thirty-two-characters",
        },
    )
    credential = "must-not-appear-in-response"
    async with client(NeverCalledCatalog(), access_policy=policy) as session:
        response = await session.put(
            "/admin/sources/new-source",
            headers={"authorization": f"Bearer {token}"},
            json={"manifest": {}, "credential": credential},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert credential not in response.text


@pytest.mark.asyncio
async def test_schema_drift_details_are_not_disclosed() -> None:
    snapshot = minimal_development_snapshot()
    snapshot.relations = snapshot.relations[:1]
    async with client(ReturningCatalog(snapshot)) as session:
        response = await session.post("/meta", json={"source_id": "development-issues", "question": "최근 문제"})
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "METADATA_UNAVAILABLE",
        "message": "Metadata is temporarily unavailable for the requested source.",
    }
    assert "issue_overview" not in response.text


@pytest.mark.asyncio
async def test_executes_query_with_current_metadata_revision() -> None:
    catalog = ReturningCatalog(minimal_development_snapshot())
    executor = RecordingQueryExecutor()
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
            },
        )

    assert response.status_code == 200
    assert response.json()["rows"] == [{"issue_count": 600}]
    assert executor.calls[0][0] == "development-issues"
    assert executor.calls[0][2] == "local-development"


@pytest.mark.asyncio
async def test_query_rejects_stale_revision_without_echoing_sql() -> None:
    secret_literal = "private-customer-secret"
    async with client(ReturningCatalog(minimal_development_snapshot())) as session:
        response = await session.post(
            "/query",
            json={
                "source_id": "development-issues",
                "sql": f"SELECT '{secret_literal}' FROM ai.issue_overview",
                "metadata_revision": f"sha256:{'0' * 64}",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "METADATA_REVISION_MISMATCH"
    assert secret_literal not in response.text


@pytest.mark.asyncio
async def test_caller_policy_filters_and_hides_unauthorized_sources(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy_path = tmp_path / "access.yaml"
    policy_path.write_text(
        """
version: 1
callers:
  - caller_id: development-analyst
    tenant_id: engineering
    token_env: DEVELOPMENT_ANALYST_TOKEN
    allowed_sources:
      - development-issues
""".strip(),
        encoding="utf-8",
    )
    token = "development-analyst-token-at-least-32-characters"
    policy = AccessPolicy.load(
        policy_path,
        ["development-issues", "market-voc"],
        {"DEVELOPMENT_ANALYST_TOKEN": token},
    )
    headers = {"authorization": f"Bearer {token}"}
    executor = RecordingQueryExecutor()
    caplog.set_level(logging.WARNING, logger="query_man.audit")
    async with client(
        NeverCalledCatalog(),
        query_executor=executor,
        access_policy=policy,
    ) as session:
        listed = await session.get("/sources", headers=headers)
        denied = await session.post(
            "/meta",
            headers=headers,
            json={"source_id": "market-voc", "question": "VOC 수"},
        )
        unknown = await session.post(
            "/meta",
            headers=headers,
            json={"source_id": "not-registered", "question": "anything"},
        )
        denied_query = await session.post(
            "/query",
            headers=headers,
            json={
                "source_id": "market-voc",
                "sql": "SELECT 1",
                "metadata_revision": f"sha256:{'0' * 64}",
            },
        )
        unauthenticated = await session.get("/sources")

    assert [source["source_id"] for source in listed.json()["sources"]] == [
        "development-issues"
    ]
    assert denied.status_code == 404
    assert denied.json() == unknown.json()
    assert denied_query.json() == unknown.json()
    assert executor.calls == []
    assert unauthenticated.status_code == 401
    assert "authorization_denied" in caplog.text
    assert "market-voc" not in caplog.text
    assert "not-registered" not in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_only_operator_can_request_query_cancellation(tmp_path: Path) -> None:
    policy_path = tmp_path / "access.yaml"
    policy_path.write_text(
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
    allowed_sources: [development-issues]
    operator: false
  - caller_id: operator
    tenant_id: operations
    token_env: OPERATOR_TOKEN
    allowed_sources: [development-issues]
    operator: true
""".strip(),
        encoding="utf-8",
    )
    analyst_token = "analyst-token-value-with-at-least-32-characters"
    operator_token = "operator-token-value-with-at-least-32-characters"
    policy = AccessPolicy.load(
        policy_path,
        ["development-issues", "market-voc"],
        {"ANALYST_TOKEN": analyst_token, "OPERATOR_TOKEN": operator_token},
    )
    query_id = "30c7b03d-659d-47d4-b6f5-cb2ea9a9eaf0"
    async with client(NeverCalledCatalog(), access_policy=policy) as session:
        forbidden = await session.delete(
            f"/queries/{query_id}",
            headers={"authorization": f"Bearer {analyst_token}"},
        )
        missing = await session.delete(
            f"/queries/{query_id}",
            headers={"authorization": f"Bearer {operator_token}"},
        )

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "QUERY_NOT_FOUND"
