from __future__ import annotations

import httpx
import pytest

from query_man.app import build_app
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.runtime_config import RuntimeConfig
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


def runtime_config(api_token: str | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=api_token,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
    )


def client(catalog: object, api_token: str | None = None) -> httpx.AsyncClient:
    app = build_app(
        runtime_config(api_token),
        registry=load_test_registry(),
        catalog=catalog,  # type: ignore[arg-type]
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
async def test_bearer_token_is_required_when_configured() -> None:
    token = "test-token-with-at-least-thirty-two-characters"
    async with client(NeverCalledCatalog(), token) as session:
        unauthorized = await session.get("/sources")
        authorized = await session.get("/sources", headers={"authorization": f"Bearer {token}"})
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    assert authorized.status_code == 200


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
